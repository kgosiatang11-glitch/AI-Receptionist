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
from sqlalchemy.exc import IntegrityError

from smartdesk.extensions import db
from smartdesk.models import Conversation, Message, UsageEvent, utcnow

logger = logging.getLogger(__name__)

#: Roles the model may see, mapped to the OpenAI chat roles.
_MODEL_ROLES = {"customer": "user", "assistant": "assistant", "human_agent": "assistant"}


def load_history(
    conversation: Conversation,
    limit: int | None = None,
    exclude_message_ids: tuple = (),
) -> list[dict]:
    """Return recent turns formatted for the model.

    ``exclude_message_ids`` lets the caller leave out the inbound message that
    is currently being answered: the engine already puts that message in its
    own prompt, so including it here too would show the model the same
    customer message twice.
    """
    limit = limit or current_app.config.get("MAX_CONVERSATION_HISTORY", 20)
    query = Message.query.filter(
        Message.conversation_id == conversation.id,
        Message.tenant_id == conversation.tenant_id,
        Message.role.in_(tuple(_MODEL_ROLES)),
    )
    if exclude_message_ids:
        query = query.filter(Message.id.notin_(exclude_message_ids))
    rows = query.order_by(Message.created_at.desc()).limit(limit).all()
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


class DuplicateInboundMessage(Exception):
    """The inbound provider message id was already recorded for this tenant."""


def find_inbound_message(tenant_id: str, provider_message_id: str) -> Message | None:
    """Return the stored inbound customer message for a provider message id."""
    return Message.query.filter_by(
        tenant_id=tenant_id,
        role="customer",
        provider_message_id=provider_message_id,
    ).first()


def store_inbound_message(
    conversation: Conversation, body: str, provider_message_id: str
) -> Message:
    """Persist THE inbound customer message -- the single owner of that write.

    The row doubles as the idempotency claim: the partial unique index
    ``uq_messages_inbound_provider_id`` lets exactly one transaction insert a
    given (tenant, provider_message_id).  The insert runs inside a SAVEPOINT so
    that losing the race surfaces as :class:`DuplicateInboundMessage` without
    poisoning the caller's transaction.

    On PostgreSQL a concurrent second insert of the same key *waits* for the
    first transaction: if that commits, the second raises (duplicate); if it
    rolls back (the first request failed before replying), the second insert
    succeeds and the message is processed exactly once.  Nothing here depends
    on process-local state, so it holds across gunicorn workers.
    """
    try:
        with db.session.begin_nested():
            message = append_message(
                conversation,
                "customer",
                body,
                provider_message_id=provider_message_id,
            )
            db.session.flush()
    except IntegrityError:
        # Only the idempotency index means "duplicate"; anything else is a
        # genuine error and must surface.
        if find_inbound_message(conversation.tenant_id, provider_message_id) is None:
            raise
        raise DuplicateInboundMessage(provider_message_id) from None
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


def engine_adapters(conversation: Conversation, inbound_message: Message | None = None):
    """Build the (loader, appender) pair the ReceptionistEngine expects.

    The engine passes a ``session_id`` string; it is ignored here because the
    conversation row is already resolved and is the authoritative scope. This
    keeps the engine's existing signature untouched.

    ``inbound_message`` is the customer message the caller has ALREADY
    persisted (see :func:`store_inbound_message`).  When given, the caller owns
    persistence of that turn, so:

    * the appender drops the engine's own "user" turn (otherwise the same
      message is stored twice -- defect C2), and
    * the loader leaves that message out of the history (the engine already
      includes it in its prompt, so the model would otherwise see it twice).

    Without it the adapters behave exactly as before (used by the voice path,
    which is out of scope for this change).
    """
    exclude = (inbound_message.id,) if inbound_message is not None else ()

    def loader(_session_id: str) -> list[dict]:
        return load_history(conversation, exclude_message_ids=exclude)

    def appender(_session_id: str, role: str, content: str) -> None:
        if inbound_message is not None and role == "user":
            return
        append_message(
            conversation, "customer" if role == "user" else "assistant", content
        )

    return loader, appender
