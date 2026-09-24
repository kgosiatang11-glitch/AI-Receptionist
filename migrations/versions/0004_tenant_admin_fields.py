"""Reconcile tenants.plan / owner_* columns with the current Tenant model.

Revision ID: 0004_tenant_admin_fields
Revises: 0003_booking_conversation_state

These columns are already declared on the Tenant model (smartdesk/models.py)
and are read/written by the admin tenant-management API, but no prior
migration actually created them -- confirmed directly against the live
database via information_schema before writing this, rather than assumed.
Written with IF NOT EXISTS / defensive constraint creation so it is safe to
run whether or not a given environment already has these columns (e.g. a
database where they were added out-of-band, or a fresh one that never had
them at all).
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_tenant_admin_fields"
down_revision = "0003_booking_conversation_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan VARCHAR(20) "
        "NOT NULL DEFAULT 'basic'"
    )
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS owner_name VARCHAR(160)")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS owner_email VARCHAR(160)")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS owner_phone VARCHAR(32)")

    # Constraint creation guarded so re-running this migration, or applying it
    # to a database where the constraint was already added by hand, does not
    # fail with "constraint already exists".
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenant_plan'
            ) THEN
                ALTER TABLE tenants ADD CONSTRAINT ck_tenant_plan
                    CHECK (plan in ('basic','professional','enterprise'));
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenant_plan")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS owner_phone")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS owner_email")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS owner_name")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS plan")
