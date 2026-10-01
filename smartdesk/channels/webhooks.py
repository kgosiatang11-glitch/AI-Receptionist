"""Multi-tenant Twilio webhooks.

Both channels follow the same path:

    signature check -> destination number -> channel -> tenant
      -> customer/conversation -> tenant-specific O'Brien -> reply

If the destination number matches no configured channel the request is
refused.  Answering from a default would mean replying to one business's
customer using another business's facts, so refusing is the safe outcome.
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, Response, current_app, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from smartdesk.extensions import db
from smartdesk.security.twilio_guard import twilio_webhook
from smartdesk.services import conversations as conversation_service
from smartdesk.services.knowledge import build_persona, get_profile
from smartdesk.services.leads import maybe_capture_lead
from smartdesk.services.receptionist import build_engine
from smartdesk.tenancy import (
    TenantResolutionError,
    bind_tenant,
    get_or_create_conversation,
    get_or_create_customer,
    resolve_channel,
)

logger = logging.getLogger(__name__)

webhooks = Blueprint("webhooks", __name__)

#: Twilio SIDs are 34 alphanumeric characters; we only insist on something
#: that is safe to store (provider_message_id is VARCHAR(64)) and log.
_MESSAGE_SID_RE = re.compile(r"^[\w.-]{1,64}$")

FALLBACK_TEXT = (
    "Sorry, we could not process that message right now. Please try again shortly."
)

LIMIT_REACHED_TEXT = (
    "Thanks for reaching out. This business has reached its monthly conversation "
    "limit — a team member will follow up with you directly."
)


def _monthly_conversation_count(tenant_id: str) -> int:
    from datetime import datetime, timezone

    from smartdesk.models import Conversation

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return Conversation.query.filter(
        Conversation.tenant_id == tenant_id, Conversation.created_at >= month_start
    ).count()


def _is_new_conversation(conversation) -> bool:
    from smartdesk.models import Message

    return Message.query.filter_by(conversation_id=conversation.id).count() == 0


def _over_monthly_limit(tenant, conversation) -> bool:
    """Per-tenant limit, replacing the legacy global usage.txt counter.

    Only refuses a conversation that is NEW this call, matching the original
    semantics (a limit on new conversations, not on message volume), so an
    existing customer already mid-conversation is never cut off mid-thread.
    """
    limit = tenant.monthly_conversation_limit
    if not limit or not _is_new_conversation(conversation):
        return False
    return _monthly_conversation_count(tenant.id) > limit


def _twiml(body: str = "") -> Response:
    response = MessagingResponse()
    if body:
        response.message(body)
    return Response(str(response), status=200, mimetype="application/xml")


def _voice_twiml(response: VoiceResponse) -> Response:
    return Response(str(response), status=200, mimetype="application/xml")


def _truncate(text: str) -> str:
    limit = current_app.config.get("MAX_INBOUND_MESSAGE_CHARS", 1500)
    return text[:limit]


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


@webhooks.route("/whatsapp", methods=["POST"])
@twilio_webhook
def whatsapp() -> Response:
    """Inbound WhatsApp message.

    Flow (one database transaction, committed once at the end):

        signature (decorator) -> channel + tenant -> MessageSid validation
          -> duplicate fast-path -> customer / conversation
          -> ATOMIC CLAIM: store the inbound message (unique per MessageSid)
          -> usage + lead capture -> engine -> store assistant reply -> commit

    HTTP status policy (Twilio only retries what is not 2xx, and only when the
    webhook URL carries ``#rc=N&rp=5xx`` -- by default it retries connection
    failures only):

    * 403  bad/missing signature (decorator).  Not retryable, not ours.
    * 400  signed request with no usable MessageSid.  Malformed; a retry of the
           same request can never succeed, so we say so instead of guessing.
    * 200 + empty TwiML  unknown/suspended destination, disabled receptionist,
           human takeover, and DUPLICATE deliveries.  These are deliberate
           no-reply outcomes; retrying cannot change them.  A duplicate must
           never produce a second reply.
    * 200 + reply  everything answered, including an OpenAI outage -- the
           engine degrades to a fallback reply and that turn IS recorded, so
           retrying would only re-bill the model for the same answer.
    * 500  any unexpected failure (database error, bug).  The transaction is
           rolled back, which also releases the idempotency claim, so a Twilio
           retry of the same MessageSid is processed cleanly exactly once.
           (The previous behaviour -- an apology with HTTP 200 -- told Twilio
           the message was handled when it was not.)
    """
    incoming = _truncate(request.values.get("Body", "").strip())
    sender = request.values.get("From", "").strip()
    destination = request.values.get("To", "").strip()
    message_sid = (request.values.get("MessageSid") or "").strip()

    if not sender:
        return _twiml("We could not identify your WhatsApp number. Please try again.")

    try:
        channel = resolve_channel("whatsapp", destination)
    except TenantResolutionError as exc:
        logger.warning("WhatsApp tenant resolution failed: %s", exc)
        return _twiml()

    tenant = channel.tenant
    bind_tenant(tenant)

    # Every genuine Twilio inbound message has a MessageSid, and it is the
    # identity we de-duplicate on.  Without one we cannot be idempotent, so a
    # signed-but-malformed request is refused rather than silently processed.
    if not _MESSAGE_SID_RE.match(message_sid):
        logger.warning(
            "WhatsApp webhook for tenant %s rejected: missing or malformed MessageSid",
            tenant.slug,
        )
        db.session.rollback()
        return Response("Missing or invalid MessageSid", status=400, mimetype="text/plain")

    # Fast path for the common retry: already recorded, so do nothing at all.
    # This is only an optimisation -- the unique index below is what makes
    # concurrent duplicates safe.
    if conversation_service.find_inbound_message(tenant.id, message_sid) is not None:
        return _duplicate_response(tenant, message_sid)

    profile = get_profile(tenant.id)
    if profile is not None and not profile.is_active:
        return _twiml()

    customer = get_or_create_customer(tenant, sender)
    conversation = get_or_create_conversation(
        tenant, channel, sender, "whatsapp", customer
    )

    if not incoming:
        db.session.commit()
        return _twiml("Please send a message and I will be happy to help.")

    # The limit only ever applies to brand-new conversations, so it must be
    # evaluated BEFORE this call's inbound row exists (afterwards the
    # conversation is no longer empty).  Evaluating is read-only; acting on it
    # happens only if we win the claim below.
    over_limit = _over_monthly_limit(tenant, conversation)

    # ATOMIC CLAIM + the single authoritative write of the inbound message.
    # Losing the race to an identical MessageSid means the other request owns
    # (or already finished) this event: do nothing further.
    try:
        inbound = conversation_service.store_inbound_message(
            conversation, incoming, message_sid
        )
    except conversation_service.DuplicateInboundMessage:
        return _duplicate_response(tenant, message_sid)

    if over_limit:
        conversation_service.mark_needs_human(
            conversation, "Monthly conversation limit reached"
        )
        db.session.commit()
        return _twiml(LIMIT_REACHED_TEXT)

    conversation_service.record_usage(tenant.id, "message_in", "whatsapp")

    persona = build_persona(tenant)
    maybe_capture_lead(tenant, conversation, customer, incoming, persona, "whatsapp")

    # A human agent has taken the conversation over; O'Brien stays silent.
    if conversation.human_takeover:
        db.session.commit()
        return _twiml()

    # The webhook owns persistence of the inbound turn; the engine only
    # persists its own reply (and does not re-show the model this message).
    engine = build_engine(tenant, conversation, channel, inbound_message=inbound)
    reply = engine.reply(incoming, conversation.session_key, channel="whatsapp",
                         customer_reference=sender)
    conversation_service.record_usage(tenant.id, "message_out", "whatsapp")
    db.session.commit()
    return _twiml(reply)


def _duplicate_response(tenant, message_sid: str) -> Response:
    """Acknowledge a redelivered webhook without touching anything.

    Rolling back discards any incidental writes made before the duplicate was
    detected (e.g. the channel's last-inbound timestamp), so a duplicate leaves
    no trace.  The reply for the original delivery was produced by that
    delivery; answering again would double-message the customer.
    """
    db.session.rollback()
    logger.info(
        "Duplicate WhatsApp delivery ignored (tenant=%s, MessageSid=%s)",
        tenant.slug,
        message_sid,
    )
    return _twiml()


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------


def _gather(response: VoiceResponse, prompt: str, silence: int = 0) -> None:
    gather = response.gather(
        input="speech",
        timeout=max(1, min(current_app.config["VOICE_LISTEN_TIMEOUT_SECONDS"], 60)),
        speech_timeout=current_app.config["VOICE_SPEECH_TIMEOUT_SECONDS"],
        language=current_app.config["TWILIO_VOICE_LANGUAGE"],
        action=f"/voice/continue?silence={silence}",
        method="POST",
        action_on_empty_result=True,
    )
    gather.say(prompt, language=current_app.config["TWILIO_VOICE_LANGUAGE"])


@webhooks.route("/voice", methods=["POST"])
@twilio_webhook
def voice_answer() -> Response:
    destination = request.values.get("To") or request.values.get("Called") or ""
    caller = request.values.get("From", "")
    call_sid = request.values.get("CallSid", "unknown")

    response = VoiceResponse()
    try:
        channel = resolve_channel("voice", destination)
    except TenantResolutionError as exc:
        logger.warning("Voice tenant resolution failed: %s", exc)
        response.say("This number is not currently in service. Goodbye.")
        response.hangup()
        return _voice_twiml(response)

    tenant = channel.tenant
    bind_tenant(tenant)

    customer = get_or_create_customer(tenant, caller)
    conversation = get_or_create_conversation(
        tenant, channel, f"voice:{call_sid}", "voice", customer
    )
    conversation_service.record_usage(tenant.id, "call_started", "voice")

    profile = get_profile(tenant.id)
    greeting = (
        (profile.voice_greeting if profile else None)
        or (profile.greeting if profile else None)
        or f"Hello! Thank you for calling {tenant.name}. How can I help you today?"
    )
    _gather(response, greeting)
    db.session.commit()
    return _voice_twiml(response)


@webhooks.route("/voice/continue", methods=["POST"])
@twilio_webhook
def voice_continue() -> Response:
    destination = request.values.get("To") or request.values.get("Called") or ""
    caller = request.values.get("From", "")
    call_sid = request.values.get("CallSid", "unknown")
    transcript = _truncate(request.values.get("SpeechResult", "").strip())
    try:
        silence = max(0, int(request.args.get("silence", "0")))
    except ValueError:
        silence = 0

    response = VoiceResponse()
    try:
        channel = resolve_channel("voice", destination)
    except TenantResolutionError:
        response.say("Sorry, we could not process your call right now. Goodbye.")
        response.hangup()
        return _voice_twiml(response)

    tenant = channel.tenant
    bind_tenant(tenant)
    language = current_app.config["TWILIO_VOICE_LANGUAGE"]

    if not transcript:
        if silence >= current_app.config["VOICE_MAX_SILENCE_REPROMPTS"]:
            response.say(
                "I still can't hear you. Please call again when you're ready. Goodbye.",
                language=language,
            )
            response.hangup()
            return _voice_twiml(response)
        _gather(
            response,
            "I'm sorry, I didn't catch that. Please say that again.",
            silence=silence + 1,
        )
        return _voice_twiml(response)

    customer = get_or_create_customer(tenant, caller)
    conversation = get_or_create_conversation(
        tenant, channel, f"voice:{call_sid}", "voice", customer
    )
    conversation_service.append_message(conversation, "customer", transcript)

    persona = build_persona(tenant)
    maybe_capture_lead(tenant, conversation, customer, transcript, persona, "voice")

    engine = build_engine(tenant, conversation, channel)
    reply = engine.reply(
        transcript, conversation.session_key, channel="voice", customer_reference=caller
    )
    response.say(reply, language=language)
    _gather(response, "Is there anything else I can help you with?")
    db.session.commit()
    return _voice_twiml(response)
