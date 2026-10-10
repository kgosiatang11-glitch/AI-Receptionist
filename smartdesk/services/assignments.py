"""Who may be stored in ``assigned_user_id`` -- and keeping it that way (C6).

Rules (one place, reused by every writer):

* The assignee must have a membership in THE row's tenant.  ``users`` is a
  global table; ``memberships`` is the tenant boundary, so the database-level
  guard is the composite FK ``(tenant_id, assigned_user_id) ->
  memberships (tenant_id, user_id)`` added by migration 0008.
* The membership role must be in :data:`~smartdesk.models.ASSIGNABLE_ROLES`
  (agent, owner) and the user must be active.  The database cannot see roles or
  ``is_active``, so those two rules live here and in the membership lifecycle
  helpers, which clear assignments whenever a member stops qualifying.
* A platform administrator who is not a member is never an assignee.  Their
  actions are attributed through the audit log / timeline instead.
"""

from __future__ import annotations

import sqlalchemy as sa

from smartdesk.extensions import db
from smartdesk.models import ASSIGNABLE_ROLES, Conversation, Lead, Membership, User


def assignable_user_query(tenant_id: str):
    """Select ``User`` rows that may currently be assigned work in ``tenant_id``."""
    return (
        db.select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.role.in_(ASSIGNABLE_ROLES),
            User.is_active.is_(True),
        )
    )


def is_assignable_member(user_id: str | None, tenant_id: str) -> bool:
    """True if ``user_id`` is an active agent/owner member of ``tenant_id``."""
    if not user_id:
        return False
    return (
        db.session.execute(
            assignable_user_query(tenant_id).where(User.id == user_id)
        ).scalar_one_or_none()
        is not None
    )


def clear_assignments(tenant_id: str, user_id: str) -> int:
    """Unassign ``user_id`` from every lead and conversation of ``tenant_id``.

    Tenant-scoped on purpose: the same user may legitimately hold assignments
    in another tenant, and those must not be touched.  Returns rows changed.
    """
    changed = 0
    for model in (Lead, Conversation):
        result = db.session.execute(
            sa.update(model)
            .where(model.tenant_id == tenant_id, model.assigned_user_id == user_id)
            .values(assigned_user_id=None)
        )
        changed += result.rowcount or 0
    return changed


def clear_assignments_everywhere(user_id: str) -> int:
    """Unassign ``user_id`` in every tenant (the account itself was deactivated)."""
    changed = 0
    for model in (Lead, Conversation):
        result = db.session.execute(
            sa.update(model)
            .where(model.assigned_user_id == user_id)
            .values(assigned_user_id=None)
        )
        changed += result.rowcount or 0
    return changed


def count_invalid_assignments() -> dict:
    """Assignments that violate the policy right now (diagnostic / tests)."""
    out = {}
    for model in (Lead, Conversation):
        valid = (
            sa.select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.tenant_id == model.tenant_id,
                Membership.user_id == model.assigned_user_id,
                Membership.role.in_(ASSIGNABLE_ROLES),
                User.is_active.is_(True),
            )
            .exists()
        )
        out[model.__tablename__] = db.session.scalar(
            sa.select(sa.func.count())
            .select_from(model)
            .where(model.assigned_user_id.isnot(None), ~valid)
        )
    return out
