"""Add booking_mode config and booking_conversation_states for the
conversational booking layer.

Revision ID: 0003_booking_conversation_state
Revises: 0002_calendar_connections
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_booking_conversation_state"
down_revision = "0002_calendar_connections"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=False)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "receptionist_profiles",
        sa.Column("booking_mode", sa.String(16), nullable=False, server_default="calendar"),
    )
    op.add_column(
        "receptionist_profiles", sa.Column("external_booking_url", sa.String(500))
    )
    op.add_column(
        "receptionist_profiles",
        sa.Column("default_booking_duration_minutes", sa.Integer(), nullable=False, server_default="60"),
    )
    op.create_check_constraint(
        "ck_receptionist_profile_booking_mode",
        "receptionist_profiles",
        "booking_mode in ('calendar','external')",
    )

    op.create_table(
        "booking_conversation_states",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID, sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("booking_id", UUID, sa.ForeignKey("bookings.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="collecting"),
        sa.Column("requested_date", sa.Date()),
        sa.Column("requested_time", sa.Time()),
        sa.Column("duration_minutes", sa.Integer()),
        sa.Column("party_size", sa.Integer()),
        sa.Column("service", sa.String(160)),
        sa.Column("customer_name", sa.String(160)),
        sa.Column("customer_phone", sa.String(32)),
        sa.Column("timezone", sa.String(64)),
        sa.Column("last_prompted_field", sa.String(32)),
        sa.Column("last_failure_reason", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status in ('collecting','confirming','confirmed','cancelled','failed')",
            name="ck_booking_state_status",
        ),
    )
    op.create_index(
        "ix_booking_states_conversation", "booking_conversation_states",
        ["conversation_id", "status"],
    )
    op.create_index(
        "ix_booking_states_tenant", "booking_conversation_states", ["tenant_id"]
    )

    op.execute("ALTER TABLE booking_conversation_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE booking_conversation_states FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY booking_conversation_states_tenant_isolation
        ON booking_conversation_states
        USING (tenant_id::text = current_setting('app.current_tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS booking_conversation_states_tenant_isolation "
        "ON booking_conversation_states"
    )
    op.drop_table("booking_conversation_states")
    op.drop_constraint(
        "ck_receptionist_profile_booking_mode", "receptionist_profiles", type_="check"
    )
    op.drop_column("receptionist_profiles", "default_booking_duration_minutes")
    op.drop_column("receptionist_profiles", "external_booking_url")
    op.drop_column("receptionist_profiles", "booking_mode")
