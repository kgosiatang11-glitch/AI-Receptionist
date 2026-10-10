"""C6: tenant-scoped assignee ownership and membership invitations.

Revision ID: 0008_assignment_invitations
Revises: 0007_tenant_scoped_foreign_keys

1. ``leads`` and ``conversations`` may only be assigned to a user who is a MEMBER
   of the row's own tenant::

       (tenant_id, assigned_user_id) -> memberships (tenant_id, user_id)

   ``users`` is a global table with no ``tenant_id``, so a composite FK to it is
   impossible; ``memberships`` is the tenant boundary for users and already has
   ``UNIQUE (tenant_id, user_id)`` (``uq_membership_tenant_user``, 0001).

   ``ON DELETE SET NULL (assigned_user_id)`` clears only the assignee when a
   membership is deleted.  Plain ``SET NULL`` on a composite FK would try to NULL
   ``tenant_id`` as well (NOT NULL -> the membership delete would fail).  The
   column-list form needs PostgreSQL 15+; the migration refuses to run on an older
   server rather than silently installing something weaker.

   The existing single-column FKs ``assigned_user_id -> users.id`` are KEPT.

   The database enforces tenant membership only.  Role (agent/owner) and
   ``users.is_active`` cannot be expressed in an FK, so they are enforced by the
   application and by the membership lifecycle code (assignments are cleared when
   a member is removed, demoted, or deactivated).

2. ``membership_invitations``: consent-based, expiring, single-use invitations
   that replace "add any registered user by email".  Deliberately WITHOUT row
   level security, like ``tenants`` and ``users``: an invitation is looked up by
   the hash of an unguessable secret before any tenant is known.  Every
   tenant-facing endpoint filters by tenant explicitly.

Fail closed, never repair: before the FKs are created every existing assignment is
checked against the full policy.  If ANY row violates it the migration aborts with
counts and creates nothing -- data is never modified, nulled, or deleted here.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_assignment_invitations"
down_revision = "0007_tenant_scoped_foreign_keys"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=False)
TS = sa.DateTime(timezone=True)

# Keep in sync with smartdesk.models.ASSIGNABLE_ROLES at the time of writing.
ASSIGNABLE_ROLES_SQL = "('agent', 'owner')"

ASSIGNEE_FKS = (
    ("leads", "fk_leads_assignee_member"),
    ("conversations", "fk_conversations_assignee_member"),
)


def _preflight() -> None:
    bind = op.get_bind()

    version = int(bind.execute(sa.text("SHOW server_version_num")).scalar_one())
    if version < 150000:
        raise RuntimeError(
            "0008 aborted: ON DELETE SET NULL (column list) requires PostgreSQL 15 "
            f"or newer (server_version_num={version})"
        )

    problems = []
    for table, _name in ASSIGNEE_FKS:
        row = bind.execute(
            sa.text(
                f"""
                SELECT
                  count(*) FILTER (WHERE m.id IS NULL)                         AS no_membership,
                  count(*) FILTER (WHERE m.id IS NOT NULL
                                     AND m.role NOT IN {ASSIGNABLE_ROLES_SQL}) AS non_assignable_role,
                  count(*) FILTER (WHERE m.id IS NOT NULL
                                     AND u.is_active IS NOT TRUE)              AS inactive_user
                FROM {table} t
                LEFT JOIN memberships m
                       ON m.tenant_id = t.tenant_id AND m.user_id = t.assigned_user_id
                LEFT JOIN users u ON u.id = t.assigned_user_id
                WHERE t.assigned_user_id IS NOT NULL
                """
            )
        ).one()
        no_membership, bad_role, inactive = row
        if no_membership or bad_role or inactive:
            problems.append(
                f"{table}: {no_membership} assigned to a non-member (other tenant / no "
                f"membership), {bad_role} assigned to a member whose role is not "
                f"agent/owner, {inactive} assigned to a deactivated user"
            )

    if problems:
        raise RuntimeError(
            "0008 aborted: existing assignments violate the C6 policy; nothing was "
            "changed. Review and resolve them deliberately, then re-run. "
            + " | ".join(problems)
        )


def upgrade() -> None:
    _preflight()

    for table, name in ASSIGNEE_FKS:
        op.execute(
            f"""
            ALTER TABLE {table}
            ADD CONSTRAINT {name}
            FOREIGN KEY (tenant_id, assigned_user_id)
            REFERENCES memberships (tenant_id, user_id)
            ON DELETE SET NULL (assigned_user_id)
            """
        )

    op.create_table(
        "membership_invitations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(160), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("invited_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("accepted_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("accepted_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash", name="uq_membership_invitation_token"),
        sa.CheckConstraint(
            "role in ('viewer','agent','manager','owner')",
            name="ck_membership_invitation_role",
        ),
        sa.CheckConstraint(
            "status in ('pending','accepted','revoked')",
            name="ck_membership_invitation_status",
        ),
    )
    op.create_index(
        "ix_membership_invitations_tenant", "membership_invitations", ["tenant_id", "status"]
    )
    op.create_index(
        "uq_membership_invitation_pending",
        "membership_invitations",
        ["tenant_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("membership_invitations")
    for table, name in reversed(ASSIGNEE_FKS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
