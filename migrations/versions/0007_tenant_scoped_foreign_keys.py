"""Enforce tenant ownership on tenant-owned foreign-key relationships.

Revision ID: 0007_tenant_scoped_foreign_keys
Revises: 0006_usage_reservations

Tenant-owned child rows must only reference parent rows belonging to the
same tenant. Composite foreign keys make this invariant database-enforced,
rather than relying solely on application-level ownership checks.
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_tenant_scoped_foreign_keys"
down_revision = "0006_usage_reservations"
branch_labels = None
depends_on = None


# Parent tables referenced by composite FKs need a unique (tenant_id, id).
PARENT_UNIQUES = (
    ("customers", "uq_customers_tenant_id"),
    ("channels", "uq_channels_tenant_id"),
    ("conversations", "uq_conversations_tenant_id"),
    ("bookings", "uq_bookings_tenant_id"),
)


# Existing single-column FKs that are replaced by tenant-scoped FKs.
OLD_FKS = (
    ("conversations", "conversations_customer_id_fkey"),
    ("conversations", "conversations_channel_id_fkey"),
    ("messages", "messages_conversation_id_fkey"),
    ("leads", "leads_customer_id_fkey"),
    ("leads", "leads_conversation_id_fkey"),
    ("bookings", "bookings_customer_id_fkey"),
    ("bookings", "bookings_conversation_id_fkey"),
    ("booking_conversation_states", "booking_conversation_states_conversation_id_fkey"),
    ("booking_conversation_states", "booking_conversation_states_customer_id_fkey"),
    ("booking_conversation_states", "booking_conversation_states_booking_id_fkey"),
)


NEW_FKS = (
    (
        "conversations",
        "fk_conversations_customer_tenant",
        ["tenant_id", "customer_id"],
        ["customers.tenant_id", "customers.id"],
        "SET NULL",
    ),
    (
        "conversations",
        "fk_conversations_channel_tenant",
        ["tenant_id", "channel_id"],
        ["channels.tenant_id", "channels.id"],
        "SET NULL",
    ),
    (
        "messages",
        "fk_messages_conversation_tenant",
        ["tenant_id", "conversation_id"],
        ["conversations.tenant_id", "conversations.id"],
        "CASCADE",
    ),
    (
        "leads",
        "fk_leads_customer_tenant",
        ["tenant_id", "customer_id"],
        ["customers.tenant_id", "customers.id"],
        "SET NULL",
    ),
    (
        "leads",
        "fk_leads_conversation_tenant",
        ["tenant_id", "conversation_id"],
        ["conversations.tenant_id", "conversations.id"],
        "SET NULL",
    ),
    (
        "bookings",
        "fk_bookings_customer_tenant",
        ["tenant_id", "customer_id"],
        ["customers.tenant_id", "customers.id"],
        "SET NULL",
    ),
    (
        "bookings",
        "fk_bookings_conversation_tenant",
        ["tenant_id", "conversation_id"],
        ["conversations.tenant_id", "conversations.id"],
        "SET NULL",
    ),
    (
        "booking_conversation_states",
        "fk_booking_states_conversation_tenant",
        ["tenant_id", "conversation_id"],
        ["conversations.tenant_id", "conversations.id"],
        "CASCADE",
    ),
    (
        "booking_conversation_states",
        "fk_booking_states_customer_tenant",
        ["tenant_id", "customer_id"],
        ["customers.tenant_id", "customers.id"],
        "SET NULL",
    ),
    (
        "booking_conversation_states",
        "fk_booking_states_booking_tenant",
        ["tenant_id", "booking_id"],
        ["bookings.tenant_id", "bookings.id"],
        "SET NULL",
    ),
)


def upgrade() -> None:
    # Fail closed if data has somehow become cross-tenant before this
    # migration is applied. No automatic data repair is performed.
    checks = (
        """
        SELECT EXISTS (
            SELECT 1
            FROM conversations c
            JOIN customers p ON p.id = c.customer_id
            WHERE c.customer_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM conversations c
            JOIN channels p ON p.id = c.channel_id
            WHERE c.channel_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM messages c
            JOIN conversations p ON p.id = c.conversation_id
            WHERE c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM leads c
            JOIN customers p ON p.id = c.customer_id
            WHERE c.customer_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM leads c
            JOIN conversations p ON p.id = c.conversation_id
            WHERE c.conversation_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM bookings c
            JOIN customers p ON p.id = c.customer_id
            WHERE c.customer_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM bookings c
            JOIN conversations p ON p.id = c.conversation_id
            WHERE c.conversation_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM booking_conversation_states c
            JOIN conversations p ON p.id = c.conversation_id
            WHERE c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM booking_conversation_states c
            JOIN customers p ON p.id = c.customer_id
            WHERE c.customer_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
        """
        SELECT EXISTS (
            SELECT 1
            FROM booking_conversation_states c
            JOIN bookings p ON p.id = c.booking_id
            WHERE c.booking_id IS NOT NULL
              AND c.tenant_id <> p.tenant_id
        )
        """,
    )

    for check in checks:
        if op.get_bind().execute(sa.text(check)).scalar():
            raise RuntimeError(
                "0007 aborted: cross-tenant foreign-key data exists"
            )

    for table, constraint in PARENT_UNIQUES:
        op.create_unique_constraint(
            constraint,
            table,
            ["tenant_id", "id"],
        )

    for table, constraint in OLD_FKS:
        op.drop_constraint(constraint, table, type_="foreignkey")

    for table, constraint, local_cols, remote_cols, ondelete in NEW_FKS:
        op.create_foreign_key(
            constraint,
            table,
            remote_cols[0].split(".")[0],
            local_cols,
            [c.split(".")[1] for c in remote_cols],
            ondelete=ondelete,
        )


def downgrade() -> None:
    for table, constraint, *_ in NEW_FKS:
        op.drop_constraint(constraint, table, type_="foreignkey")

    # Restore the original single-column FKs.
    original_fks = (
        ("conversations", "conversations_customer_id_fkey", "customers", ["customer_id"], ["id"], "SET NULL"),
        ("conversations", "conversations_channel_id_fkey", "channels", ["channel_id"], ["id"], "SET NULL"),
        ("messages", "messages_conversation_id_fkey", "conversations", ["conversation_id"], ["id"], "CASCADE"),
        ("leads", "leads_customer_id_fkey", "customers", ["customer_id"], ["id"], "SET NULL"),
        ("leads", "leads_conversation_id_fkey", "conversations", ["conversation_id"], ["id"], "SET NULL"),
        ("bookings", "bookings_customer_id_fkey", "customers", ["customer_id"], ["id"], "SET NULL"),
        ("bookings", "bookings_conversation_id_fkey", "conversations", ["conversation_id"], ["id"], "SET NULL"),
        (
            "booking_conversation_states",
            "booking_conversation_states_conversation_id_fkey",
            "conversations",
            ["conversation_id"],
            ["id"],
            "CASCADE",
        ),
        (
            "booking_conversation_states",
            "booking_conversation_states_customer_id_fkey",
            "customers",
            ["customer_id"],
            ["id"],
            "SET NULL",
        ),
        (
            "booking_conversation_states",
            "booking_conversation_states_booking_id_fkey",
            "bookings",
            ["booking_id"],
            ["id"],
            "SET NULL",
        ),
    )

    for table, constraint, referred_table, local_cols, remote_cols, ondelete in original_fks:
        op.create_foreign_key(
            constraint,
            table,
            referred_table,
            local_cols,
            remote_cols,
            ondelete=ondelete,
        )

    for table, constraint in reversed(PARENT_UNIQUES):
        op.drop_constraint(constraint, table, type_="unique")
