"""Add calendar_connections for per-tenant Google Calendar OAuth.

Revision ID: 0002_calendar_connections
Revises: 0001_initial_multitenant
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_calendar_connections"
down_revision = "0001_initial_multitenant"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=False)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "calendar_connections",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("provider", sa.String(20), nullable=False, server_default="google"),
        sa.Column("status", sa.String(16), nullable=False, server_default="connected"),
        sa.Column("calendar_id", sa.String(255), nullable=False, server_default="primary"),
        sa.Column("connected_email", sa.String(160)),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text()),
        sa.Column("token_expiry", TS),
        sa.Column("connected_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("last_sync_error", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status in ('connected','disconnected','error')",
            name="ck_calendar_connection_status",
        ),
    )
    op.execute("ALTER TABLE calendar_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE calendar_connections FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY calendar_connections_tenant_isolation ON calendar_connections
        USING (tenant_id::text = current_setting('app.current_tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS calendar_connections_tenant_isolation ON calendar_connections"
    )
    op.drop_table("calendar_connections")
