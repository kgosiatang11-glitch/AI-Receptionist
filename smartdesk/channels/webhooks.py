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
    incoming = _truncate(request.values.get("Body", "").strip())
    sender = request.values.get("From", "").strip()
    destination = request.values.get("To", "").strip()
    message_sid = request.values.get("MessageSid")

    if not sender:
        return _twiml("We could not identify your WhatsApp number. Please try again.")

    try:
        channel = resolve_channel("whatsapp", destination)
    except TenantResolutionError as exc:
        logger.warning("WhatsApp tenant resolution failed: %s", exc)
        return _twiml()

    tenant = channel.tenant
    bind_tenant(tenant)

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

    # Checked before the limit gate uses it, and before recording this
    # message, since the limit only ever applies to brand-new conversations.
    if _over_monthly_limit(tenant, conversation):
        conversation_service.append_message(
            conversation, "customer", incoming, provider_message_id=message_sid
        )
        conversation_service.mark_needs_human(
            conversation, "Monthly conversation limit reached"
        )
        db.session.commit()
        return _twiml(LIMIT_REACHED_TEXT)

    conversation_service.append_message(
        conversation, "customer", incoming, provider_message_id=message_sid
    )
    conversation_service.record_usage(tenant.id, "message_in", "whatsapp")

    persona = build_persona(tenant)
    maybe_capture_lead(tenant, conversation, customer, incoming, persona, "whatsapp")

    # A human agent has taken the conversation over; O'Brien stays silent.
    if conversation.human_takeover:
        db.session.commit()
        return _twiml()

    engine = build_engine(tenant, conversation, channel)
    reply = engine.reply(incoming, conversation.session_key, channel="whatsapp",
                         customer_reference=sender)
    conversation_service.record_usage(tenant.id, "message_out", "whatsapp")
    db.session.commit()
    return _twiml(reply)


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
