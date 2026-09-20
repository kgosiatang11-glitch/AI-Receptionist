"""Tenant resolution and query scoping.

Inbound traffic path:

    Twilio request -> destination number -> Channel -> Tenant
        -> tenant configuration -> tenant knowledge
        -> customer/conversation -> O'Brien -> response

A production tenant is NEVER served from global business configuration.  If a
destination number matches no channel the request is refused rather than
answered from a default, because answering would mean replying to one
business's customer with another business's facts.
"""

from __future__ import annotations

import logging
import re

from flask import current_app, g

from smartdesk.extensions import db
from smartdesk.models import Channel, Conversation, Customer, Tenant, utcnow

logger = logging.getLogger(__name__)


class TenantResolutionError(Exception):
    """Raised when inbound traffic cannot be attributed to a tenant."""


def normalize_address(raw: str | None) -> str:
    """Normalise a Twilio address to bare E.164.

    ``whatsapp:+26771234567`` and ``+267 7123 4567`` both become
    ``+26771234567`` so that channel lookup is stable across channels and
    formatting variations.
    """
    if not raw:
        return ""
    value = raw.strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    value = re.sub(r"[^\d+]", "", value)
    if value and not value.startswith("+"):
        value = "+" + value
    return value


def resolve_channel(kind: str, destination: str) -> Channel:
    """Find the active channel for an inbound destination number."""
    address = normalize_address(destination)
    if not address:
        raise TenantResolutionError("Inbound request had no destination number")

    channel = (
        Channel.query.filter_by(kind=kind, address=address, is_active=True)
        .one_or_none()
    )
    if channel is None:
        raise TenantResolutionError(
            f"No active {kind} channel is configured for {address}"
        )

    tenant = db.session.get(Tenant, channel.tenant_id)
    if tenant is None or tenant.status == "suspended":
        raise TenantResolutionError(f"Tenant for {address} is unavailable")

    channel.last_inbound_at = utcnow()
    return channel


def bind_tenant(tenant: Tenant) -> None:
    """Make ``tenant`` the active tenant for this request."""
    g.tenant = tenant
    if current_app.config.get("SQLALCHEMY_DATABASE_URI"):
        try:
            db.session.execute(
                db.text("SELECT set_config('app.current_tenant_id', :tid, true)"),
                {"tid": str(tenant.id)},
            )
        except Exception:  # pragma: no cover
            logger.debug("Could not bind RLS tenant GUC", exc_info=True)


def current_tenant() -> Tenant:
    tenant = getattr(g, "tenant", None)
    if tenant is None:
        raise TenantResolutionError("No tenant is bound to this request")
    return tenant


def tenant_query(model, tenant_id: str | None = None):
    """Return a query for ``model`` filtered to one tenant.

    Application code should never call ``Model.query`` directly on a
    tenant-owned table.  Going through here means forgetting the filter is a
    ``TenantResolutionError``, not a data leak.
    """
    resolved = tenant_id or current_tenant().id
    if not hasattr(model, "tenant_id"):
        raise TenantResolutionError(f"{model.__name__} is not tenant-scoped")
    return model.query.filter(model.tenant_id == resolved)


# ---------------------------------------------------------------------------
# Customer / conversation upsert
# ---------------------------------------------------------------------------


def get_or_create_customer(tenant: Tenant, phone: str | None) -> Customer | None:
    if not phone:
        return None
    address = normalize_address(phone)
    customer = (
        Customer.query.filter_by(tenant_id=tenant.id, phone=address).one_or_none()
    )
    if customer is None:
        customer = Customer(
            tenant_id=tenant.id,
            phone=address,
            is_test_data=tenant.is_test_data,
            first_contact_at=utcnow(),
        )
        db.session.add(customer)
        db.session.flush()
    customer.last_contact_at = utcnow()
    return customer


def get_or_create_conversation(
    tenant: Tenant,
    channel: Channel | None,
    session_key: str,
    channel_kind: str,
    customer: Customer | None,
) -> Conversation:
    conversation = (
        Conversation.query.filter_by(
            tenant_id=tenant.id, session_key=session_key
        ).one_or_none()
    )
    if conversation is None:
        conversation = Conversation(
            tenant_id=tenant.id,
            channel_id=channel.id if channel else None,
            channel_kind=channel_kind,
            session_key=session_key,
            customer_id=customer.id if customer else None,
            is_test_data=tenant.is_test_data,
        )
        db.session.add(conversation)
        db.session.flush()
    elif conversation.customer_id is None and customer is not None:
        conversation.customer_id = customer.id
    return conversation
