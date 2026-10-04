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

from ai.receptionist import AI_CALL_GRANTED, AIUsageDenied
from smartdesk.extensions import db
from smartdesk.models import Conversation
from smartdesk.security.rbac import bind_rls_tenant
from smartdesk.security.twilio_guard import twilio_webhook
from smartdesk.services import conversations as conversation_service
from smartdesk.services import usage as usage_service
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

#: Deterministic reply when a tenant has used all of its monthly AI replies
#: (``Tenant.monthly_conversation_limit``, counted per UTC calendar month).
#: Local text only: producing it never calls OpenAI or consumes quota.
LIMIT_REACHED_TEXT = (
    "Thanks for reaching out. This business has reached its monthly conversation "
    "limit — a team member will follow up with you directly."
)


class _AiUsageGate:
    """AI-usage quota for ONE inbound WhatsApp message.

    Handed to the engine as its ``before_ai_call`` / ``after_ai_call`` hooks, so
    it runs only when an OpenAI request is genuinely about to be made -- never
    for duplicates, human takeover, canned/handoff/booking replies, or tenants
    that are suspended or disabled (those return before the engine).

    The idempotency key is ``whatsapp:<MessageSid>``: stable across Twilio
    retries, unique per inbound message, and (with the tenant and kind) unique
    in the database.  It is NOT per tenant or per conversation, because one
    conversation legitimately produces many AI replies.

    Transaction choreography (OpenAI never runs inside a transaction):

        txn 1:  ... inbound claim, customer, conversation, usage, lead ...
                reserve_ai_reply  ->  COMMIT          (``before``)
        --- no transaction open: the OpenAI request happens here ---
        txn 2:  re-bind tenant (``after``), store the reply,
                commit_reservation | release_reservation, usage event -> COMMIT
    """

    def __init__(self, tenant_id: str, conversation_id: str, message_sid: str) -> None:
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.message_sid = message_sid
        self.key = f"whatsapp:{message_sid}"
        #: Set only once the reservation is durably committed (end of txn 1).
        self.reservation_id: str | None = None
        self.finalized = False

    def before(self) -> str:
        result = usage_service.reserve_ai_reply(
            self.tenant_id,
            self.key,
            metadata={"channel": "whatsapp", "conversation_id": self.conversation_id},
        )
        # Only a reservation THIS call created authorises an OpenAI request.
        # "duplicate"/"already_released" mean an earlier attempt owns this
        # message; "quota_exceeded" means no capacity.
        if not (result.allowed and result.created):
            return result.reason
        reservation_id = result.reservation.id
        db.session.commit()  # end txn 1: inbound claim + reservation are durable
        self.reservation_id = reservation_id
        return AI_CALL_GRANTED

    def after(self) -> None:
        # ``app.current_tenant_id`` is transaction-local, so it was reset by the
        # commit above.  Re-bind it first thing in the new transaction, before
        # any ORM object (all expired by the commit) is reloaded.
        bind_rls_tenant(self.tenant_id)

    def finalize(self, outcome: str | None, usage: dict | None) -> None:
        """Commit (success) or release (anything else) -- at most once."""
        if self.reservation_id is None or self.finalized:
            return
        self.finalized = True
        if outcome == "succeeded":
            usage = usage or {}
            usage_service.commit_reservation(
                self.tenant_id,
                self.reservation_id,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        else:
            usage_service.release_reservation(self.tenant_id, self.reservation_id)


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

    Flow:

        signature (decorator) -> channel + tenant -> MessageSid validation
          -> duplicate fast-path -> customer / conversation
          -> ATOMIC CLAIM: store the inbound message (unique per MessageSid)
          -> usage + lead capture -> human takeover check -> engine

    Replies that need no OpenAI (canned, handoff, booking, human takeover, ...)
    stay in ONE transaction committed at the end.  A reply that does need
    OpenAI is gated by the atomic AI-usage reservation (``_AiUsageGate``):

        txn 1: everything above + reserve 1 AI reply -> COMMIT
        OpenAI request, with NO database transaction open
        txn 2: store reply + commit (success) / release (failure) the
               reservation + usage event -> COMMIT

    No reservation means no OpenAI request: when the tenant is out of monthly
    AI replies the engine is stopped before it calls OpenAI and the customer
    gets ``LIMIT_REACHED_TEXT``.

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
    * 500  any unexpected failure BEFORE the reservation is committed
           (database error, bug).  The transaction is rolled back, which also
           releases the idempotency claim, so a Twilio retry of the same
           MessageSid is processed cleanly exactly once.  (The previous
           behaviour -- an apology with HTTP 200 -- told Twilio the message
           was handled when it was not.)
    * 200 + fallback  an unexpected failure AFTER the reservation committed.
           The inbound message is by then durable, so a retry would only be
           ignored as a duplicate; instead the reservation is released (OpenAI
           failed) or left for stale recovery (reply generated but accounting
           failed), nothing is retried, and the customer gets ``FALLBACK_TEXT``.
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
    # Plain value for use after the commit that ends txn 1 expires every ORM
    # object (reloading them requires the tenant to be re-bound first).
    tenant_id = tenant.id

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

    # ATOMIC CLAIM + the single authoritative write of the inbound message.
    # Losing the race to an identical MessageSid means the other request owns
    # (or already finished) this event: do nothing further.
    try:
        inbound = conversation_service.store_inbound_message(
            conversation, incoming, message_sid
        )
    except conversation_service.DuplicateInboundMessage:
        return _duplicate_response(tenant, message_sid)

    conversation_service.record_usage(tenant.id, "message_in", "whatsapp")

    persona = build_persona(tenant)
    maybe_capture_lead(tenant, conversation, customer, incoming, persona, "whatsapp")

    # A human agent has taken the conversation over; O'Brien stays silent.
    if conversation.human_takeover:
        db.session.commit()
        return _twiml()

    # The webhook owns persistence of the inbound turn; the engine only
    # persists its own reply (and does not re-show the model this message).
    gate = _AiUsageGate(tenant_id, conversation.id, message_sid)
    engine = build_engine(
        tenant, conversation, channel, inbound_message=inbound,
        before_ai_call=gate.before, after_ai_call=gate.after,
    )
    conversation_id = conversation.id
    try:
        reply = engine.reply(incoming, conversation.session_key, channel="whatsapp",
                             customer_reference=sender)
    except AIUsageDenied as denied:
        # The gate refused, so OpenAI was never called and the engine recorded
        # no turn.  Txn 1 is still open here (nothing was committed).
        return _ai_denied_response(tenant, conversation, message_sid, denied)
    except Exception:
        if gate.reservation_id is None:
            raise  # before the durable point: 500 + rollback, a retry is clean
        return _recover_after_reservation_failure(gate, engine)

    # Token usage rides in the existing UsageEvent.meta JSON (no schema
    # change). Absent when no OpenAI call was made; token values are None
    # when OpenAI returned no usage -- never estimated.
    usage_meta = {"openai_usage": engine.last_usage} if engine.last_usage else {}
    try:
        gate.finalize(engine.ai_outcome, engine.last_usage)
    except (usage_service.ReservationStateError, usage_service.ReservationNotFound):
        # Logical problem (e.g. the stale sweep already released it); the
        # session is healthy, so the reply and its records are still saved.
        logger.exception(
            "AI usage reservation %s (tenant %s) could not be finalised",
            gate.reservation_id, tenant_id,
        )
        usage_meta["quota_finalize_error"] = True
    except Exception:
        # Database failure while committing/releasing.  The reply WAS generated
        # (and billed), so deliver it; the reservation stays 'reserved' and is
        # returned to capacity by stale-reservation recovery.  Never retry
        # OpenAI and never reserve again.
        logger.exception(
            "AI usage reservation %s (tenant %s) left for stale recovery: "
            "finalise failed after OpenAI answered",
            gate.reservation_id, tenant_id,
        )
        _persist_reply_after_finalize_failure(
            tenant_id, conversation_id, reply, usage_meta
        )
        return _twiml(reply)

    conversation_service.record_usage(tenant_id, "message_out", "whatsapp", **usage_meta)
    db.session.commit()
    return _twiml(reply)


def _ai_denied_response(tenant, conversation, message_sid: str, denied: AIUsageDenied) -> Response:
    """Answer a message whose AI reservation was refused (OpenAI never called)."""
    if denied.reason != "quota_exceeded":
        # "duplicate"/"already_released": an earlier attempt already owns this
        # message's reservation.  Answering again would double-reply.
        return _duplicate_response(tenant, message_sid)
    # Out of monthly AI replies: hand the conversation to a human (the existing
    # limit behaviour) with a local, deterministic message.  A conversation
    # already waiting for a human is not given another handoff event per message.
    if conversation.status != "needs_human":
        conversation_service.mark_needs_human(conversation, "Monthly AI reply limit reached")
    db.session.commit()
    return _twiml(LIMIT_REACHED_TEXT)


def _recover_after_reservation_failure(gate: _AiUsageGate, engine) -> Response:
    """An unexpected error after the reservation was committed.

    Fresh transaction, tenant re-bound.  If OpenAI did not give a usable answer
    the reservation is released; if it did, commit is attempted once; if even
    that fails it stays 'reserved' for stale recovery.  OpenAI is never called
    again and no second reservation is made.
    """
    logger.exception(
        "Unexpected failure after AI reservation %s (tenant %s)",
        gate.reservation_id, gate.tenant_id,
    )
    try:
        db.session.rollback()
        bind_rls_tenant(gate.tenant_id)
        gate.finalize(engine.ai_outcome, engine.last_usage)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "AI usage reservation %s (tenant %s) left for stale recovery",
            gate.reservation_id, gate.tenant_id,
        )
    return _twiml(FALLBACK_TEXT)


def _persist_reply_after_finalize_failure(
    tenant_id: str, conversation_id: str, reply: str, usage_meta: dict
) -> None:
    """Best effort: keep the generated reply and its usage event.

    The failed transaction (which held the engine's assistant message) was
    rolled back, so store them again in a fresh one, WITHOUT touching the
    reservation.
    """
    try:
        db.session.rollback()
        bind_rls_tenant(tenant_id)
        conversation = db.session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation_service.append_message(conversation, "assistant", reply)
        conversation_service.record_usage(
            tenant_id, "message_out", "whatsapp", quota_commit_failed=True, **usage_meta
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Could not persist reply for conversation %s", conversation_id)


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
