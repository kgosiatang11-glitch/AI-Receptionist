"""Multi-tenant data model for the SmartDesk AI Control Center.

Isolation strategy (defence in depth, three independent layers):

1. ``TenantScoped`` — every business-owned table carries a non-null
   ``tenant_id`` foreign key.
2. ``tenant_query`` in :mod:`smartdesk.tenancy` — application queries are
   filtered through a helper that *requires* an explicit tenant, so an
   unscoped read is a programming error rather than a silent leak.
3. Postgres row-level security — policies in the initial migration compare
   ``tenant_id`` against the ``app.current_tenant_id`` session GUC, which the
   request context sets per transaction.  Even a bug in layer 2 cannot return
   another tenant's rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from smartdesk.extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# Portable column types: native uuid/jsonb on Postgres (Supabase), plain
# string/json elsewhere so the suite can run without a Postgres instance.
UUIDType = sa.String(36).with_variant(UUID(as_uuid=False), "postgresql")
JSONType = sa.JSON().with_variant(JSONB, "postgresql")


class TimestampMixin:
    created_at = db.Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TenantScoped:
    """Marker mixin: presence of this class means the table is tenant-owned."""

    @classmethod
    def tenant_column(cls):
        return cls.tenant_id


# ---------------------------------------------------------------------------
# Tenancy and identity
# ---------------------------------------------------------------------------

TENANT_STATUSES = ("active", "development", "suspended")
TENANT_TYPES = ("technology", "sports", "beauty", "test", "other")
TENANT_PLANS = ("basic", "professional", "enterprise")


class Tenant(TimestampMixin, db.Model):
    """One business served by the platform.  Never one deployment per tenant."""

    __tablename__ = "tenants"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    slug = db.Column(String(64), nullable=False, unique=True)
    name = db.Column(String(160), nullable=False)
    business_type = db.Column(String(32), nullable=False, default="other")
    status = db.Column(String(16), nullable=False, default="active")
    plan = db.Column(String(20), nullable=False, default="basic")
    timezone = db.Column(String(64), nullable=False, default="Africa/Gaborone")
    # True for the internal SmartDesk tenant and the Test Business tenant.
    is_internal = db.Column(Boolean, nullable=False, default=False)
    is_test_data = db.Column(Boolean, nullable=False, default=False)
    monthly_conversation_limit = db.Column(Integer, nullable=False, default=500)
    # Where escalations go for THIS tenant. Never a platform-wide constant.
    escalation_whatsapp = db.Column(String(32))
    escalation_email = db.Column(String(160))
    # What the Super Admin typed in when creating this tenant, before any
    # real Supabase account exists for the owner. Purely informational --
    # the actual access grant is a Membership row (see POST /tenants/<id>/owner
    # in smartdesk/api/admin_api.py), never these fields.
    owner_name = db.Column(String(160))
    owner_email = db.Column(String(160))
    owner_phone = db.Column(String(32))
    settings = db.Column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "status in ('active','development','suspended')", name="ck_tenant_status"
        ),
        CheckConstraint(
            "plan in ('basic','professional','enterprise')", name="ck_tenant_plan"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "business_type": self.business_type,
            "status": self.status,
            "plan": self.plan,
            "timezone": self.timezone,
            "is_internal": self.is_internal,
            "is_test_data": self.is_test_data,
            "owner_name": self.owner_name,
            "owner_email": self.owner_email,
            "owner_phone": self.owner_phone,
            "created_at": _iso(self.created_at),
        }


class User(TimestampMixin, db.Model):
    """A dashboard user, mirrored from Supabase ``auth.users``.

    Passwords, sessions and refresh tokens live in Supabase.  This table only
    holds the platform-side profile and the link to the Supabase subject id.
    """

    __tablename__ = "users"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    supabase_user_id = db.Column(UUIDType, nullable=False, unique=True, index=True)
    email = db.Column(String(160), nullable=False, unique=True, index=True)
    full_name = db.Column(String(160))
    # Platform staff. Can read/manage every tenant and switch between them.
    is_platform_admin = db.Column(Boolean, nullable=False, default=False)
    is_active = db.Column(Boolean, nullable=False, default=True)
    last_seen_at = db.Column(DateTime(timezone=True))

    memberships = relationship(
        "Membership", back_populates="user", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "full_name": self.full_name,
            "is_platform_admin": self.is_platform_admin,
        }


# Ordered weakest -> strongest. Used by ``role_at_least``.
ROLE_VIEWER = "viewer"
ROLE_AGENT = "agent"
ROLE_MANAGER = "manager"
ROLE_OWNER = "owner"
ROLE_ORDER = (ROLE_VIEWER, ROLE_AGENT, ROLE_MANAGER, ROLE_OWNER)


class Membership(TimestampMixin, TenantScoped, db.Model):
    """Which user may access which tenant, and with what role."""

    __tablename__ = "memberships"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id = db.Column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role = db.Column(String(16), nullable=False, default=ROLE_VIEWER)

    user = relationship("User", back_populates="memberships")
    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        CheckConstraint(
            "role in ('viewer','agent','manager','owner')", name="ck_membership_role"
        ),
    )


# ---------------------------------------------------------------------------
# Channels — the tenant resolution table
# ---------------------------------------------------------------------------

CHANNEL_KINDS = ("whatsapp", "voice", "sms")


class Channel(TimestampMixin, TenantScoped, db.Model):
    """Maps an inbound Twilio destination number to its owning tenant.

    This is what makes one deployment serve every business: the number the
    customer contacted (Twilio's ``To``) identifies the tenant.
    """

    __tablename__ = "channels"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kind = db.Column(String(16), nullable=False)
    # Normalised E.164 without the "whatsapp:" prefix, e.g. "+26771234567".
    address = db.Column(String(32), nullable=False)
    display_name = db.Column(String(120))
    is_active = db.Column(Boolean, nullable=False, default=True)
    # Per-tenant Twilio subaccount SID. The auth token NEVER lives here; it is
    # resolved from the environment at send time.
    twilio_subaccount_sid = db.Column(String(64))
    provider_settings = db.Column(JSONType, nullable=False, default=dict)
    last_inbound_at = db.Column(DateTime(timezone=True))

    tenant = relationship("Tenant")

    __table_args__ = (
        UniqueConstraint("kind", "address", name="uq_channel_kind_address"),
        CheckConstraint("kind in ('whatsapp','voice','sms')", name="ck_channel_kind"),
        Index("ix_channels_tenant", "tenant_id"),
    )


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------


class Customer(TimestampMixin, TenantScoped, db.Model):
    __tablename__ = "customers"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    phone = db.Column(String(32))
    email = db.Column(String(160))
    full_name = db.Column(String(160))
    notes = db.Column(Text)
    is_test_data = db.Column(Boolean, nullable=False, default=False)
    first_contact_at = db.Column(DateTime(timezone=True), default=utcnow)
    last_contact_at = db.Column(DateTime(timezone=True), default=utcnow)

    tenant = relationship("Tenant")

    __table_args__ = (
        # Phone is unique per tenant, not globally: the same person may be a
        # customer of two different businesses on the platform.
        UniqueConstraint("tenant_id", "phone", name="uq_customer_tenant_phone"),
        Index("ix_customers_tenant", "tenant_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "full_name": self.full_name,
            "phone": self.phone,
            "email": self.email,
            "first_contact_at": _iso(self.first_contact_at),
            "last_contact_at": _iso(self.last_contact_at),
            "is_test_data": self.is_test_data,
        }


CONVERSATION_STATUSES = ("active", "needs_human", "closed")


class Conversation(TimestampMixin, TenantScoped, db.Model):
    __tablename__ = "conversations"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id = db.Column(UUIDType, ForeignKey("customers.id", ondelete="SET NULL"))
    channel_id = db.Column(UUIDType, ForeignKey("channels.id", ondelete="SET NULL"))
    channel_kind = db.Column(String(16), nullable=False, default="whatsapp")
    # Stable per-conversation key: the WhatsApp sender, or "voice:<CallSid>".
    session_key = db.Column(String(128), nullable=False)
    status = db.Column(String(16), nullable=False, default="active")
    is_unread = db.Column(Boolean, nullable=False, default=True)
    # Set when a human takes over; O'Brien stops replying while true.
    human_takeover = db.Column(Boolean, nullable=False, default=False)
    assigned_user_id = db.Column(UUIDType, ForeignKey("users.id", ondelete="SET NULL"))
    last_message_at = db.Column(DateTime(timezone=True), default=utcnow)
    last_message_preview = db.Column(String(280))
    is_test_data = db.Column(Boolean, nullable=False, default=False)

    customer = relationship("Customer")
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_key", name="uq_conversation_session"),
        CheckConstraint(
            "status in ('active','needs_human','closed')", name="ck_conversation_status"
        ),
        Index("ix_conversations_tenant_last", "tenant_id", "last_message_at"),
    )

    def to_dict(self, include_customer: bool = True) -> dict:
        data = {
            "id": self.id,
            "channel_kind": self.channel_kind,
            "session_key": self.session_key,
            "status": self.status,
            "is_unread": self.is_unread,
            "human_takeover": self.human_takeover,
            "last_message_at": _iso(self.last_message_at),
            "last_message_preview": self.last_message_preview,
            "is_test_data": self.is_test_data,
        }
        if include_customer:
            data["customer"] = self.customer.to_dict() if self.customer else None
        return data


# "event" rows record handoffs, lead capture and booking activity inline so the
# inbox can render a single unified timeline.
MESSAGE_ROLES = ("customer", "assistant", "human_agent", "event")


class Message(TimestampMixin, TenantScoped, db.Model):
    __tablename__ = "messages"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id = db.Column(
        UUIDType, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role = db.Column(String(16), nullable=False)
    body = db.Column(Text, nullable=False)
    # For role='event': 'handoff' | 'lead_captured' | 'booking' | 'takeover'.
    event_type = db.Column(String(32))
    provider_message_id = db.Column(String(64))
    meta = db.Column(JSONType, nullable=False, default=dict)

    conversation = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        CheckConstraint(
            "role in ('customer','assistant','human_agent','event')",
            name="ck_message_role",
        ),
        Index("ix_messages_conversation", "conversation_id", "created_at"),
        Index("ix_messages_tenant", "tenant_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "body": self.body,
            "event_type": self.event_type,
            "created_at": _iso(self.created_at),
        }


LEAD_STATUSES = ("new", "contacted", "qualified", "converted", "lost")


class Lead(TimestampMixin, TenantScoped, db.Model):
    __tablename__ = "leads"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id = db.Column(UUIDType, ForeignKey("customers.id", ondelete="SET NULL"))
    conversation_id = db.Column(
        UUIDType, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # 'whatsapp' | 'voice' | 'manual'
    source = db.Column(String(32), nullable=False, default="whatsapp")
    interest = db.Column(Text)
    status = db.Column(String(16), nullable=False, default="new")
    assigned_user_id = db.Column(UUIDType, ForeignKey("users.id", ondelete="SET NULL"))
    is_test_data = db.Column(Boolean, nullable=False, default=False)

    customer = relationship("Customer")
    assigned_user = relationship("User")

    __table_args__ = (
        CheckConstraint(
            "status in ('new','contacted','qualified','converted','lost')",
            name="ck_lead_status",
        ),
        Index("ix_leads_tenant_status", "tenant_id", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "customer": self.customer.to_dict() if self.customer else None,
            "source": self.source,
            "interest": self.interest,
            "status": self.status,
            "assigned_user": self.assigned_user.to_dict()
            if self.assigned_user
            else None,
            "conversation_id": self.conversation_id,
            "created_at": _iso(self.created_at),
            "is_test_data": self.is_test_data,
        }


BOOKING_STATUSES = ("pending", "confirmed", "cancelled", "completed")
# 'external' means the booking lives in a third-party system (e.g. 10by20's
# Playbypoint). SmartDesk records the reference only; it does NOT claim API
# access it does not have.
BOOKING_SOURCES = ("ai", "staff", "external")


class Booking(TimestampMixin, TenantScoped, db.Model):
    __tablename__ = "bookings"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    customer_id = db.Column(UUIDType, ForeignKey("customers.id", ondelete="SET NULL"))
    conversation_id = db.Column(
        UUIDType, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    service = db.Column(String(160))
    starts_at = db.Column(DateTime(timezone=True))
    ends_at = db.Column(DateTime(timezone=True))
    status = db.Column(String(16), nullable=False, default="pending")
    source = db.Column(String(16), nullable=False, default="ai")
    # Free-text reference into the external system, when source='external'.
    external_reference = db.Column(String(160))
    external_system = db.Column(String(80))
    notes = db.Column(Text)
    is_test_data = db.Column(Boolean, nullable=False, default=False)

    customer = relationship("Customer")

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','confirmed','cancelled','completed')",
            name="ck_booking_status",
        ),
        CheckConstraint("source in ('ai','staff','external')", name="ck_booking_source"),
        Index("ix_bookings_tenant_start", "tenant_id", "starts_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "customer": self.customer.to_dict() if self.customer else None,
            "service": self.service,
            "starts_at": _iso(self.starts_at),
            "ends_at": _iso(self.ends_at),
            "status": self.status,
            "source": self.source,
            "external_system": self.external_system,
            "external_reference": self.external_reference,
            "is_test_data": self.is_test_data,
        }


# ---------------------------------------------------------------------------
# Knowledge and receptionist configuration
# ---------------------------------------------------------------------------

KNOWLEDGE_SECTIONS = (
    "business_information",
    "services",
    "pricing",
    "opening_hours",
    "location",
    "faqs",
    "policies",
    "booking_information",
    "ai_instructions",
)


class KnowledgeDocument(TimestampMixin, TenantScoped, db.Model):
    """One structured knowledge section per tenant.

    Deliberately structured rather than a vector store: at this scale the whole
    tenant knowledge set fits comfortably in the prompt, so retrieval would add
    failure modes without adding accuracy.  ``body`` holds prose; ``data``
    holds structured entries (e.g. a list of services) when the section has
    them.
    """

    __tablename__ = "knowledge_documents"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    section = db.Column(String(40), nullable=False)
    title = db.Column(String(160))
    body = db.Column(Text)
    data = db.Column(JSONType, nullable=False, default=dict)
    is_published = db.Column(Boolean, nullable=False, default=True)
    updated_by_user_id = db.Column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "section", name="uq_knowledge_tenant_section"),
        Index("ix_knowledge_tenant", "tenant_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "section": self.section,
            "title": self.title,
            "body": self.body,
            "data": self.data or {},
            "is_published": self.is_published,
            "updated_at": _iso(self.updated_at),
        }


class ReceptionistProfile(TimestampMixin, TenantScoped, db.Model):
    """Per-tenant O'Brien configuration.

    The engine is shared; this is what makes it tenant-specific.  SmartDesk's
    own sales behaviour lives in the SmartDesk tenant's row and nowhere else.
    """

    __tablename__ = "receptionist_profiles"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    display_name = db.Column(String(80), nullable=False, default="O'Brien")
    is_active = db.Column(Boolean, nullable=False, default=True)
    greeting = db.Column(Text)
    voice_greeting = db.Column(Text)
    personality = db.Column(Text)
    business_instructions = db.Column(Text)
    handoff_enabled = db.Column(Boolean, nullable=False, default=True)
    handoff_message = db.Column(Text)
    lead_capture_enabled = db.Column(Boolean, nullable=False, default=True)
    booking_assistance_enabled = db.Column(Boolean, nullable=False, default=True)
    # BOOKING_MODE — see the module docstring on smartdesk/services/booking_engine.py
    # for the full flow each mode drives. 'calendar': SmartDesk owns the
    # booking, syncing to the tenant's connected Google Calendar. 'external':
    # the tenant already has its own booking system (e.g. 10by20's
    # Playbypoint) -- O'Brien only ever hands the customer that link.
    booking_mode = db.Column(String(16), nullable=False, default="calendar")
    external_booking_url = db.Column(String(500))
    default_booking_duration_minutes = db.Column(Integer, nullable=False, default=60)
    # Only the SmartDesk tenant sets this true. It is what stopped 10by20's
    # callers being pitched SmartDesk AI.
    sales_mode_enabled = db.Column(Boolean, nullable=False, default=False)
    sales_handoff_message = db.Column(Text)

    tenant = relationship("Tenant")

    __table_args__ = (
        CheckConstraint(
            "booking_mode in ('calendar','external')",
            name="ck_receptionist_profile_booking_mode",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "is_active": self.is_active,
            "greeting": self.greeting,
            "voice_greeting": self.voice_greeting,
            "personality": self.personality,
            "business_instructions": self.business_instructions,
            "handoff_enabled": self.handoff_enabled,
            "handoff_message": self.handoff_message,
            "lead_capture_enabled": self.lead_capture_enabled,
            "booking_assistance_enabled": self.booking_assistance_enabled,
            "sales_mode_enabled": self.sales_mode_enabled,
            "booking_mode": self.booking_mode,
            "external_booking_url": self.external_booking_url,
            "default_booking_duration_minutes": self.default_booking_duration_minutes,
        }


# ---------------------------------------------------------------------------
# Automations, usage, audit
# ---------------------------------------------------------------------------

BOOKING_STATE_STATUSES = ("collecting", "confirming", "confirmed", "cancelled", "failed")


class BookingConversationState(TimestampMixin, TenantScoped, db.Model):
    """Structured slot-filling state for one in-progress booking conversation.

    One conversation can produce several of these over time (book, then
    later book again) but only ever has one ACTIVE (non-terminal) row at a
    time -- see :func:`smartdesk.services.booking_engine.get_active_state`.
    Deliberately structured rather than re-deriving intent from the raw
    message history on every turn: a customer's "6pm" reply only makes
    sense combined with the date captured two turns earlier, and re-reading
    free text for that on every message is both slower and less reliable
    than reading a column.
    """

    __tablename__ = "booking_conversation_states"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id = db.Column(
        UUIDType, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    customer_id = db.Column(UUIDType, ForeignKey("customers.id", ondelete="SET NULL"))
    booking_id = db.Column(UUIDType, ForeignKey("bookings.id", ondelete="SET NULL"))

    status = db.Column(String(16), nullable=False, default="collecting")
    requested_date = db.Column(db.Date)
    requested_time = db.Column(db.Time)
    duration_minutes = db.Column(Integer)
    party_size = db.Column(Integer)
    service = db.Column(String(160))
    customer_name = db.Column(String(160))
    customer_phone = db.Column(String(32))
    timezone = db.Column(String(64))
    #: What the customer was last asked, so a short reply like "6pm" can be
    #: interpreted correctly even out of context.
    last_prompted_field = db.Column(String(32))
    last_failure_reason = db.Column(Text)

    conversation = relationship("Conversation")
    booking = relationship("Booking")

    __table_args__ = (
        CheckConstraint(
            "status in ('collecting','confirming','confirmed','cancelled','failed')",
            name="ck_booking_state_status",
        ),
        Index("ix_booking_states_conversation", "conversation_id", "status"),
        Index("ix_booking_states_tenant", "tenant_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "requested_date": self.requested_date.isoformat() if self.requested_date else None,
            "requested_time": self.requested_time.isoformat() if self.requested_time else None,
            "duration_minutes": self.duration_minutes,
            "party_size": self.party_size,
            "service": self.service,
            "customer_name": self.customer_name,
            "booking_id": self.booking_id,
        }


AUTOMATION_KINDS = (
    "appointment_reminder",
    "lead_alert",
    "booking_confirmation",
    "follow_up_message",
    "missed_call_follow_up",
    "whatsapp_notification",
    "sms_notification",
)
# 'pending' = data model and UI exist, delivery not yet wired.
AUTOMATION_STATUSES = ("pending", "enabled", "disabled")


class Automation(TimestampMixin, TenantScoped, db.Model):
    __tablename__ = "automations"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kind = db.Column(String(40), nullable=False)
    name = db.Column(String(160), nullable=False)
    status = db.Column(String(16), nullable=False, default="pending")
    # Delivery is not implemented in this phase; the UI shows this flag.
    is_implemented = db.Column(Boolean, nullable=False, default=False)
    trigger_config = db.Column(JSONType, nullable=False, default=dict)
    action_config = db.Column(JSONType, nullable=False, default=dict)
    last_run_at = db.Column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", name="uq_automation_tenant_kind"),
        Index("ix_automations_tenant", "tenant_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "is_implemented": self.is_implemented,
            "last_run_at": _iso(self.last_run_at),
        }


class UsageEvent(TimestampMixin, TenantScoped, db.Model):
    """Per-tenant metering. Replaces the global usage.txt counter."""

    __tablename__ = "usage_events"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # 'conversation_started' | 'message_in' | 'message_out' | 'call_started'
    kind = db.Column(String(40), nullable=False)
    channel_kind = db.Column(String(16))
    quantity = db.Column(Integer, nullable=False, default=1)
    occurred_at = db.Column(DateTime(timezone=True), nullable=False, default=utcnow)
    meta = db.Column(JSONType, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_usage_tenant_kind_time", "tenant_id", "kind", "occurred_at"),
    )


class AuditLog(TimestampMixin, db.Model):
    """Administrative actions. Deliberately NOT TenantScoped-restricted on read
    for platform admins, but every row records the tenant it affected."""

    __tablename__ = "audit_logs"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(UUIDType, ForeignKey("tenants.id", ondelete="SET NULL"))
    actor_user_id = db.Column(UUIDType, ForeignKey("users.id", ondelete="SET NULL"))
    actor_email = db.Column(String(160))
    action = db.Column(String(80), nullable=False)
    object_type = db.Column(String(80))
    object_id = db.Column(String(64))
    ip_address = db.Column(String(64))
    meta = db.Column(JSONType, nullable=False, default=dict)

    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", "created_at"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "actor_email": self.actor_email,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "created_at": _iso(self.created_at),
        }


class CalendarConnection(TimestampMixin, TenantScoped, db.Model):
    """A tenant's own Google Calendar OAuth connection.

    One connection per tenant, by design — bookings write to a single
    calendar the business already uses, never to a platform-wide account.
    Tokens are stored as-is in this column; production deployments should
    put this column behind column-level encryption (e.g. Supabase Vault)
    rather than relying on table access control alone.
    """

    __tablename__ = "calendar_connections"

    id = db.Column(UUIDType, primary_key=True, default=_uuid)
    tenant_id = db.Column(
        UUIDType,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    provider = db.Column(String(20), nullable=False, default="google")
    status = db.Column(String(16), nullable=False, default="connected")
    calendar_id = db.Column(String(255), nullable=False, default="primary")
    connected_email = db.Column(String(160))
    access_token = db.Column(Text, nullable=False)
    refresh_token = db.Column(Text)
    token_expiry = db.Column(DateTime(timezone=True))
    connected_by_user_id = db.Column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL")
    )
    last_sync_error = db.Column(Text)

    tenant = relationship("Tenant")

    __table_args__ = (
        CheckConstraint(
            "status in ('connected','disconnected','error')",
            name="ck_calendar_connection_status",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "status": self.status,
            "calendar_id": self.calendar_id,
            "connected_email": self.connected_email,
            "last_sync_error": self.last_sync_error,
            "connected_at": _iso(self.created_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


#: Every tenant-owned table, used by the RLS migration and the isolation tests.
TENANT_SCOPED_MODELS = (
    Membership,
    Channel,
    Customer,
    Conversation,
    Message,
    Lead,
    Booking,
    KnowledgeDocument,
    ReceptionistProfile,
    Automation,
    UsageEvent,
    CalendarConnection,
    BookingConversationState,
)
