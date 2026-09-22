"""Construction of a tenant-specific O'Brien from the shared engine.

    Shared AI engine  +  tenant configuration  +  tenant knowledge
        +  conversation memory   =   tenant-specific O'Brien

The engine class itself is untouched; everything tenant-specific arrives
through its existing dependency-injection points.
"""

from __future__ import annotations

import logging

from flask import current_app
from openai import OpenAI

from ai.receptionist import ReceptionistEngine
from smartdesk.models import Channel, Conversation, Tenant
from smartdesk.services import conversations as conversation_service
from smartdesk.services.booking_engine import handle_booking_message
from smartdesk.services.knowledge import build_persona, knowledge_context, knowledge_dict

logger = logging.getLogger(__name__)

_openai_client = None


def openai_client():
    """Lazily build one shared OpenAI client for the process."""
    global _openai_client
    api_key = current_app.config.get("OPENAI_API_KEY")
    if not api_key:
        return None
    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=api_key,
            timeout=current_app.config.get("OPENAI_TIMEOUT_SECONDS", 10),
            max_retries=0,
        )
    return _openai_client


def twilio_client():
    from twilio.rest import Client as TwilioClient

    sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    token = current_app.config.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        return None
    return TwilioClient(sid, token)


class OutboundSendError(Exception):
    """Raised when a staff reply cannot actually be delivered."""


def send_whatsapp_message(channel: Channel, to_address: str, body: str) -> str:
    """Send a WhatsApp message from a tenant's own number.

    Returns the provider message SID. Voice has no equivalent outbound
    message primitive, so takeover replies are only supported on WhatsApp for
    now — the API surfaces this rather than silently failing.
    """
    client = twilio_client()
    if client is None:
        raise OutboundSendError("Twilio is not configured on this server")
    try:
        message = client.messages.create(
            body=body,
            from_=f"whatsapp:{channel.address}",
            to=f"whatsapp:{to_address}",
        )
    except Exception as exc:  # pragma: no cover - network/provider failure
        logger.exception("Outbound WhatsApp send failed for tenant channel %s", channel.id)
        raise OutboundSendError(str(exc)) from exc
    return message.sid


def notify_escalation(tenant: Tenant, channel: Channel | None, message: str) -> None:
    """Alert the tenant's own escalation contact.

    Critically, this is the *tenant's* contact, not a platform-wide owner
    number. One business's escalations never reach another's staff.
    """
    destination = tenant.escalation_whatsapp
    if not destination:
        logger.info(
            "Tenant %s has no escalation contact configured; skipping notify",
            tenant.slug,
        )
        return

    client = twilio_client()
    if client is None:
        return

    sender = None
    if channel is not None and channel.kind == "whatsapp":
        sender = f"whatsapp:{channel.address}"
    if not sender:
        return

    try:
        client.messages.create(
            body=f"[{tenant.name}] Escalation request:\n{message}",
            from_=sender,
            to=f"whatsapp:{destination.lstrip('whatsapp:')}",
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Escalation notification failed for tenant %s", tenant.slug)


def build_engine(
    tenant: Tenant, conversation: Conversation, channel: Channel | None
) -> ReceptionistEngine:
    """Return an engine bound to one tenant and one conversation."""
    loader, appender = conversation_service.engine_adapters(conversation)

    def escalation_notifier(_reference: str, message: str) -> None:
        conversation_service.mark_needs_human(
            conversation, f"Customer requested a human: {message[:200]}"
        )
        notify_escalation(tenant, channel, message)

    def booking_handler(message: str, _session_id: str) -> str | None:
        return handle_booking_message(
            tenant, conversation, "voice" if channel and channel.kind == "voice" else "whatsapp", message
        )

    return ReceptionistEngine(
        client_provider=openai_client,
        model=current_app.config.get("OPENAI_MODEL", "gpt-4o-mini"),
        history_loader=loader,
        history_appender=appender,
        escalation_notifier=escalation_notifier,
        persona_provider=lambda: build_persona(tenant),
        knowledge_provider=lambda: knowledge_context(tenant.id),
        knowledge_dict_provider=lambda: knowledge_dict(tenant.id),
        booking_handler=booking_handler,
    )
