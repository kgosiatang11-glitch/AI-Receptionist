"""Automatic lead capture from customer messages.

O'Brien does not decide on its own what counts as a lead — that would make
lead quality depend on prompt behaviour, which drifts. Instead this module
applies the same deterministic intent detection already used for routing
canned replies (see :mod:`intent_router`), so a lead is created from the
same signal a human reading the transcript would point to.

At most one lead is created per conversation: once a conversation has a
lead, later messages update it (via the interest field) rather than creating
duplicates, since a customer expressing interest twice is one enquiry, not two.
"""

from __future__ import annotations

from ai.persona import TenantPersona
from intent_router import detect_intent
from smartdesk.extensions import db
from smartdesk.models import Conversation, Customer, Lead, Tenant

#: Intents worth recording as a lead. "pricing" and "booking" are genuine
#: purchase/appointment signals; greetings and FAQs are not.
_LEAD_INTENTS = {"pricing", "booking", "human_handoff"}

_BUYING_PHRASES = (
    "i want it", "i want one", "let's do it", "lets do it",
    "get started", "start the setup", "proceed with the setup",
    "set this up for me", "set it up for me", "ready to proceed",
    "i'd like to book", "id like to book", "can i book",
)


def _is_buying_language(message: str) -> bool:
    text = " ".join(message.lower().split())
    return any(phrase in text for phrase in _BUYING_PHRASES)


def maybe_capture_lead(
    tenant: Tenant,
    conversation: Conversation,
    customer: Customer | None,
    message: str,
    persona: TenantPersona,
    channel_kind: str,
) -> Lead | None:
    """Create or update a lead from one inbound customer message.

    Returns the lead if one was created or touched, else ``None``. Silently
    does nothing when the tenant has lead capture turned off, so a business
    that disables it in the Control Center gets exactly that.
    """
    if not persona.lead_capture_enabled:
        return None

    intent, _ = detect_intent(message)
    signal = intent in _LEAD_INTENTS or _is_buying_language(message)
    if not signal:
        return None

    existing = Lead.query.filter_by(conversation_id=conversation.id).one_or_none()
    if existing is not None:
        # Same enquiry continuing; keep the most recent expression of interest
        # rather than piling up duplicate lead rows for one conversation.
        if message.strip():
            existing.interest = message.strip()[:500]
        return existing

    lead = Lead(
        tenant_id=tenant.id,
        customer_id=customer.id if customer else None,
        conversation_id=conversation.id,
        source=channel_kind,
        interest=message.strip()[:500],
        is_test_data=tenant.is_test_data,
    )
    db.session.add(lead)
    return lead
