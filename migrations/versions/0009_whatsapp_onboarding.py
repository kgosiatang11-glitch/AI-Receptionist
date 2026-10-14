"""Manual WhatsApp onboarding records.

Revision ID: 0009_whatsapp_onboarding
Revises: 0008_assignment_invitations
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_whatsapp_onboarding"
down_revision = "0008_assignment_invitations"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "whatsapp_onboardings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_number", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(120)),
        sa.Column("status", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("twilio_sender_sid", sa.String(64)),
        sa.Column("twilio_configuration_recorded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("phone_verification_status", sa.String(24), nullable=False, server_default="not_applicable"),
        sa.Column("meta_verification_status", sa.String(24), nullable=False, server_default="not_applicable"),
        sa.Column("registration_status", sa.String(24), nullable=False, server_default="unverified"),
        sa.Column("outstanding_requirements", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_provider_check_at", TS), sa.Column("last_provider_check_passed", sa.Boolean()),
        sa.Column("provider_check_source", sa.String(32)), sa.Column("provisioning_error", sa.Text()),
        sa.Column("receptionist_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", name="uq_whatsapp_onboarding_tenant"),
        sa.CheckConstraint("status in ('requested','under_review','provisioning','awaiting_verification','configuration_pending','ready_for_testing','active','failed','suspended','cancelled')", name="ck_whatsapp_onboarding_status"),
    )
    op.create_index("ix_whatsapp_onboarding_status", "whatsapp_onboardings", ["status"])
    op.execute("ALTER TABLE whatsapp_onboardings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE whatsapp_onboardings FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY whatsapp_onboardings_tenant_isolation ON whatsapp_onboardings
        USING (tenant_id::text = current_setting('app.current_tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true))""")


def downgrade():
    op.execute("DROP POLICY IF EXISTS whatsapp_onboardings_tenant_isolation ON whatsapp_onboardings")
    op.drop_table("whatsapp_onboardings")
