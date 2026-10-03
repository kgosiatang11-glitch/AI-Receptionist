"""Atomic monthly AI-usage foundation: usage periods and reservations.

Revision ID: 0006_usage_reservations
Revises: 0005_inbound_message_idempotency

Phase 3.3A.  Two new tenant-owned tables back the reservation service in
``smartdesk/services/usage.py``:

``tenant_usage_periods``
    One row per tenant per UTC calendar month holding ``used_units``.  The
    service claims capacity with a single conditional UPDATE
    (``... WHERE used_units + :n <= <tenant limit>``) so PostgreSQL decides
    whether capacity remains.  ``CHECK (used_units >= 0)`` is the backstop that
    makes a negative counter impossible.

``usage_reservations``
    One row per reserved unit (one OpenAI-requiring reply).  Lifecycle
    ``reserved`` -> ``committed`` | ``released``.
    ``UNIQUE (tenant_id, kind, idempotency_key)`` makes retries idempotent in
    the database.  A composite FK ``(tenant_id, usage_period_id)`` ->
    ``tenant_usage_periods (tenant_id, id)`` guarantees a reservation can never
    reference another tenant's period.

The limit is NOT stored here: it stays on ``tenants.monthly_conversation_limit``.
Nothing in this migration touches existing tables, rows or limit values, and no
application code reads these tables yet (WhatsApp/Voice wiring is a later
phase), so it is safe to deploy ahead of that wiring.

Both tables get ENABLE + FORCE ROW LEVEL SECURITY and the same
``<table>_tenant_isolation`` policy shape as 0001 (``tenant_id`` must equal the
transaction-bound ``app.current_tenant_id``).  0001 defines no separate
platform-admin policy; roles that bypass RLS (owner/superuser, as migrations
and platform maintenance run) are unaffected, exactly as for the other tables.

Everything is plain DDL inside Alembic's transaction (PostgreSQL DDL is
transactional), so a failure rolls the whole revision back.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_usage_reservations"
down_revision = "0005_inbound_message_idempotency"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=False)
TS = sa.DateTime(timezone=True)

NEW_TABLES = ("tenant_usage_periods", "usage_reservations")


def _timestamps():
    return (
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
    )


def upgrade() -> None:
    op.create_table(
        "tenant_usage_periods",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("period_start", TS, nullable=False),
        sa.Column("period_end", TS, nullable=False),
        sa.Column("used_units", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "period_start", "period_end", name="uq_usage_period_tenant_window"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_usage_period_tenant_id"),
        sa.CheckConstraint("used_units >= 0", name="ck_usage_period_used_nonneg"),
        sa.CheckConstraint("period_start < period_end", name="ck_usage_period_window"),
    )

    op.create_table(
        "usage_reservations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("usage_period_id", UUID, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("reserved_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("committed_at", TS),
        sa.Column("released_at", TS),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "usage_period_id"],
            ["tenant_usage_periods.tenant_id", "tenant_usage_periods.id"],
            ondelete="CASCADE",
            name="fk_usage_reservations_period_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "kind", "idempotency_key", name="uq_usage_reservation_idempotency"
        ),
        sa.CheckConstraint("units > 0", name="ck_usage_reservation_units_positive"),
        sa.CheckConstraint(
            "status IN ('reserved', 'committed', 'released')",
            name="ck_usage_reservation_status",
        ),
        sa.CheckConstraint(
            "(status = 'reserved' AND committed_at IS NULL AND released_at IS NULL)"
            " OR (status = 'committed' AND committed_at IS NOT NULL AND released_at IS NULL)"
            " OR (status = 'released' AND released_at IS NOT NULL AND committed_at IS NULL)",
            name="ck_usage_reservation_status_timestamps",
        ),
        sa.CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0)"
            " AND (completion_tokens IS NULL OR completion_tokens >= 0)"
            " AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="ck_usage_reservation_tokens_nonneg",
        ),
    )
    # Stale-reservation scans touch only unfinished rows, so index only those.
    op.create_index(
        "ix_usage_reservations_stale",
        "usage_reservations",
        ["reserved_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )
    op.create_index("ix_usage_reservations_period", "usage_reservations", ["usage_period_id"])

    # The (tenant_id, period_start, period_end) unique constraint already
    # provides the index for "this tenant's period for this month", and
    # (tenant_id, kind, idempotency_key) for reservation lookup, so no further
    # indexes are needed on the hot paths.

    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id::text = current_setting('app.current_tenant_id', true))
            WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', true))
            """
        )


def downgrade() -> None:
    # Dropping the tables discards any usage data recorded since 0006.  That is
    # inherent to reverting the revision; the monthly limit lives on tenants and
    # is untouched.
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table("usage_reservations")
    op.drop_table("tenant_usage_periods")
