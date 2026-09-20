"""Database-backed conversation memory.

Replaces ``conversation_history.json``.  The engine's injected
``history_loader`` / ``history_appender`` contract is unchanged, so this slots
in without touching :mod:`ai.receptionist`.

Every read and write is bound to one conversation row, which is itself bound
to one tenant, so history cannot cross tenants even if two businesses share a
customer phone number.
"""

from __future__ import annotations

import logging

from flask import current_app

from smartdesk.extensions import db
from smartdesk.models import Conversation, Message, UsageEvent, utcnow

logger = logging.getLogger(__name__)

#: Roles the model may see, mapped to the OpenAI chat roles.
_MODEL_ROLES = {"customer": "user", "assistant": "assistant", "human_agent": "assistant"}


def load_history(conversation: Conversation, limit: int | None = None) -> list[dict]:
    """Return recent turns formatted for the model."""
    limit = limit or current_app.config.get("MAX_CONVERSATION_HISTORY", 20)
    rows = (
        Message.query.filter(
            Message.conversation_id == conversation.id,
            Message.tenant_id == conversation.tenant_id,
            Message.role.in_(tuple(_MODEL_ROLES)),
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [{"role": _MODEL_ROLES[r.role], "content": r.body} for r in rows]


def append_message(
    conversation: Conversation,
    role: str,
    body: str,
    event_type: str | None = None,
    provider_message_id: str | None = None,
    **meta,
) -> Message | None:
    """Persist one turn and refresh the conversation summary fields."""
    if not body:
        return None

    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        role=role,
        body=body,
        event_type=event_type,
        provider_message_id=provider_message_id,
        meta=meta or {},
    )
    db.session.add(message)

    conversation.last_message_at = utcnow()
    conversation.last_message_preview = body[:280]
    if role == "customer":
        conversation.is_unread = True
    return message


def record_event(conversation: Conversation, event_type: str, body: str, **meta) -> None:
    """Record a handoff, lead or booking event into the conversation timeline."""
    append_message(conversation, "event", body, event_type=event_type, **meta)


def mark_needs_human(conversation: Conversation, reason: str) -> None:
    conversation.status = "needs_human"
    record_event(conversation, "handoff", reason)


def record_usage(
    tenant_id: str, kind: str, channel_kind: str | None = None, quantity: int = 1, **meta
) -> None:
    db.session.add(
        UsageEvent(
            tenant_id=tenant_id,
            kind=kind,
            channel_kind=channel_kind,
            quantity=quantity,
            meta=meta or {},
        )
    )


def engine_adapters(conversation: Conversation):
    """Build the (loader, appender) pair the ReceptionistEngine expects.

    The engine passes a ``session_id`` string; it is ignored here because the
    conversation row is already resolved and is the authoritative scope. This
    keeps the engine's existing signature untouched.
    """

    def loader(_session_id: str) -> list[dict]:
        return load_history(conversation)

    def appender(_session_id: str, role: str, content: str) -> None:
        append_message(
            conversation, "customer" if role == "user" else "assistant", content
        )

    return loader, appender
