"""Initial multi-tenant schema with row-level security.

Revision ID: 0001_initial_multitenant
Revises:
Create Date: Phase 1

This migration creates the schema and then enables Postgres row-level security
on every tenant-owned table.  RLS is the isolation layer that does not depend
on application code being correct: policies compare ``tenant_id`` against the
``app.current_tenant_id`` session setting, which the request context binds per
transaction.

Note on Supabase: the ``postgres`` role owns these tables and bypasses RLS by
default.  Run the application with a dedicated, non-superuser role (see the
``smartdesk_app`` role created below) so the policies actually apply.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_multitenant"
down_revision = None
branch_labels = None
depends_on = None


JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=False)
TS = sa.DateTime(timezone=True)


def _timestamps():
    return (
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
    )


#: Tables that carry tenant_id and therefore get RLS.
TENANT_TABLES = (
    "memberships",
    "channels",
    "customers",
    "conversations",
    "messages",
    "leads",
    "bookings",
    "knowledge_documents",
    "receptionist_profiles",
    "automations",
    "usage_events",
)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("business_type", sa.String(32), nullable=False, server_default="other"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Gaborone"),
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("monthly_conversation_limit", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("escalation_whatsapp", sa.String(32)),
        sa.Column("escalation_email", sa.String(160)),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.CheckConstraint("status in ('active','development','suspended')", name="ck_tenant_status"),
    )

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("supabase_user_id", UUID, nullable=False, unique=True),
        sa.Column("email", sa.String(160), nullable=False, unique=True),
        sa.Column("full_name", sa.String(160)),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", TS),
        *_timestamps(),
    )

    op.create_table(
        "memberships",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        sa.CheckConstraint("role in ('viewer','agent','manager','owner')", name="ck_membership_role"),
    )

    op.create_table(
        "channels",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("address", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(120)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("twilio_subaccount_sid", sa.String(64)),
        sa.Column("provider_settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_inbound_at", TS),
        *_timestamps(),
        sa.UniqueConstraint("kind", "address", name="uq_channel_kind_address"),
        sa.CheckConstraint("kind in ('whatsapp','voice','sms')", name="ck_channel_kind"),
    )
    op.create_index("ix_channels_tenant", "channels", ["tenant_id"])

    op.create_table(
        "customers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone", sa.String(32)),
        sa.Column("email", sa.String(160)),
        sa.Column("full_name", sa.String(160)),
        sa.Column("notes", sa.Text()),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_contact_at", TS),
        sa.Column("last_contact_at", TS),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "phone", name="uq_customer_tenant_phone"),
    )
    op.create_index("ix_customers_tenant", "customers", ["tenant_id"])

    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID, sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("channel_id", UUID, sa.ForeignKey("channels.id", ondelete="SET NULL")),
        sa.Column("channel_kind", sa.String(16), nullable=False, server_default="whatsapp"),
        sa.Column("session_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("is_unread", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("human_takeover", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("assigned_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("last_message_at", TS),
        sa.Column("last_message_preview", sa.String(280)),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "session_key", name="uq_conversation_session"),
        sa.CheckConstraint("status in ('active','needs_human','closed')", name="ck_conversation_status"),
    )
    op.create_index("ix_conversations_tenant_last", "conversations", ["tenant_id", "last_message_at"])

    op.create_table(
        "messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(32)),
        sa.Column("provider_message_id", sa.String(64)),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.CheckConstraint("role in ('customer','assistant','human_agent','event')", name="ck_message_role"),
    )
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])
    op.create_index("ix_messages_tenant", "messages", ["tenant_id"])

    op.create_table(
        "leads",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID, sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(32), nullable=False, server_default="whatsapp"),
        sa.Column("interest", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("assigned_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.CheckConstraint("status in ('new','contacted','qualified','converted','lost')", name="ck_lead_status"),
    )
    op.create_index("ix_leads_tenant_status", "leads", ["tenant_id", "status"])

    op.create_table(
        "bookings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID, sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("service", sa.String(160)),
        sa.Column("starts_at", TS),
        sa.Column("ends_at", TS),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(16), nullable=False, server_default="ai"),
        sa.Column("external_reference", sa.String(160)),
        sa.Column("external_system", sa.String(80)),
        sa.Column("notes", sa.Text()),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.CheckConstraint("status in ('pending','confirmed','cancelled','completed')", name="ck_booking_status"),
        sa.CheckConstraint("source in ('ai','staff','external')", name="ck_booking_source"),
    )
    op.create_index("ix_bookings_tenant_start", "bookings", ["tenant_id", "starts_at"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section", sa.String(40), nullable=False),
        sa.Column("title", sa.String(160)),
        sa.Column("body", sa.Text()),
        sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "section", name="uq_knowledge_tenant_section"),
    )
    op.create_index("ix_knowledge_tenant", "knowledge_documents", ["tenant_id"])

    op.create_table(
        "receptionist_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=False, server_default="O'Brien"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("greeting", sa.Text()),
        sa.Column("voice_greeting", sa.Text()),
        sa.Column("personality", sa.Text()),
        sa.Column("business_instructions", sa.Text()),
        sa.Column("handoff_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("handoff_message", sa.Text()),
        sa.Column("lead_capture_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("booking_assistance_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sales_mode_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sales_handoff_message", sa.Text()),
        *_timestamps(),
    )

    op.create_table(
        "automations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("is_implemented", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trigger_config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("action_config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_run_at", TS),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "kind", name="uq_automation_tenant_kind"),
    )
    op.create_index("ix_automations_tenant", "automations", ["tenant_id"])

    op.create_table(
        "usage_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("channel_kind", sa.String(16)),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("occurred_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
    )
    op.create_index("ix_usage_tenant_kind_time", "usage_events", ["tenant_id", "kind", "occurred_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="SET NULL")),
        sa.Column("actor_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("actor_email", sa.String(160)),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("object_type", sa.String(80)),
        sa.Column("object_id", sa.String(64)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
    )
    op.create_index("ix_audit_tenant_time", "audit_logs", ["tenant_id", "created_at"])

    _enable_rls()


def _enable_rls() -> None:
    """Enable row-level security on every tenant-owned table."""
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # current_setting(..., true) returns NULL rather than erroring when the
        # GUC is unset, so an unbound session simply sees nothing.
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id::text = current_setting('app.current_tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true))
            """
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for table in (
        "audit_logs",
        "usage_events",
        "automations",
        "receptionist_profiles",
        "knowledge_documents",
        "bookings",
        "leads",
        "messages",
        "conversations",
        "customers",
        "channels",
        "memberships",
        "users",
        "tenants",
    ):
        op.drop_table(table)
