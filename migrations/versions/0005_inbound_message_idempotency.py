"""Enforce one inbound customer message per provider message id.

Revision ID: 0005_inbound_message_idempotency
Revises: 0004_tenant_admin_fields

Twilio redelivers a webhook with the same MessageSid; the application treats
that SID as the identity of the inbound event.  Application-level checks alone
cannot be race-safe, so PostgreSQL enforces it:

    UNIQUE (tenant_id, provider_message_id)
    WHERE role = 'customer' AND provider_message_id IS NOT NULL

Why partial and tenant-scoped (checked against the schema, not assumed):

* ``messages.provider_message_id`` was nullable and had no index at all.
* It is written by exactly two code paths: the inbound webhook
  (role='customer', the inbound MessageSid) and the dashboard staff reply
  (role='human_agent', the id of an *outbound* Twilio send).  Those are
  different id namespaces, so a global unique index would conflate them.
* Assistant replies and event rows carry no id, so NULLs must stay legal.
* Voice inbound rows carry no id today; a future provider can reuse the
  column without colliding because the index is scoped per tenant.

Existing data: before this migration nothing prevented the same SID being
stored more than once (that is defect C1).  Rather than deleting customer
history, every duplicate after the first (by created_at, id) keeps its row but
gives up the id: the original value is preserved in ``meta`` under
``duplicate_provider_message_id``.  The operation is deterministic and a no-op
on a clean database.
"""

from alembic import op

revision = "0005_inbound_message_idempotency"
down_revision = "0004_tenant_admin_fields"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_messages_inbound_provider_id"


def upgrade() -> None:
    # messages has FORCE ROW LEVEL SECURITY.  A role that is subject to RLS
    # would silently see zero rows in the UPDATE below and the index creation
    # would then fail (or, worse, appear to succeed on an empty view).  With
    # row_security off, such a role errors out loudly instead; roles that
    # bypass RLS (superuser / BYPASSRLS, as migrations normally run) are
    # unaffected.
    op.execute("SET LOCAL row_security = off")

    op.execute(
        """
        UPDATE messages AS m
           SET meta = COALESCE(m.meta, '{}'::jsonb)
                      || jsonb_build_object(
                             'duplicate_provider_message_id', m.provider_message_id
                         ),
               provider_message_id = NULL
         WHERE m.id IN (
               SELECT d.id
                 FROM (
                       SELECT id,
                              row_number() OVER (
                                  PARTITION BY tenant_id, provider_message_id
                                  ORDER BY created_at, id
                              ) AS rn
                         FROM messages
                        WHERE role = 'customer'
                          AND provider_message_id IS NOT NULL
                      ) AS d
                WHERE d.rn > 1
         )
        """
    )

    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
            ON messages (tenant_id, provider_message_id)
         WHERE role = 'customer' AND provider_message_id IS NOT NULL
        """
    )


def downgrade() -> None:
    # The de-duplication of pre-existing rows is intentionally not reversed:
    # the original ids remain recoverable from meta.duplicate_provider_message_id.
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
