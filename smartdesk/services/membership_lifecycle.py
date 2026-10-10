"""Membership changes that must keep two invariants (C6):

1. **A tenant always keeps at least one ACTIVE owner.**  "Active" means the
   owner's user account is active; a deactivated owner cannot sign in, so they
   do not count.  A bare ``count()`` before the write is a check-then-act race
   (two owners demoting each other concurrently both see "another owner
   exists"), so every owner-affecting change first takes a row lock on the
   tenant, re-reads the membership, and only then counts.  The lock is
   ``FOR NO KEY UPDATE``: it serialises these operations against each other
   without blocking the ordinary inserts that take ``FOR KEY SHARE`` on the
   tenant row.  (SQLite ignores row locks; it serialises writers anyway.)
2. **Nobody keeps an assignment they may no longer hold.**  Removing a
   member, demoting them to a non-assignable role, or deactivating the account
   clears their lead/conversation assignments.  Owner <-> agent keeps them.

Callers own the transaction (and the commit).
"""

from __future__ import annotations

import sqlalchemy as sa

from smartdesk.extensions import db
from smartdesk.models import (
    ASSIGNABLE_ROLES,
    ROLE_OWNER,
    Membership,
    Tenant,
    User,
)
from smartdesk.services.assignments import (
    clear_assignments,
    clear_assignments_everywhere,
)


class LastOwnerError(Exception):
    """The change would leave a tenant without an active owner."""

    def __init__(self, tenant_names: list[str] | None = None):
        super().__init__("last active owner")
        self.tenant_names = tenant_names or []


def lock_tenants(tenant_ids) -> None:
    """Serialise owner-affecting changes per tenant (sorted: no lock-order deadlocks)."""
    for tenant_id in sorted(set(tenant_ids)):
        db.session.execute(
            sa.select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(key_share=True)
        ).first()


def active_owner_count(tenant_id: str, exclude_user_id: str | None = None) -> int:
    query = (
        sa.select(sa.func.count(Membership.id))
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.role == ROLE_OWNER,
            User.is_active.is_(True),
        )
    )
    if exclude_user_id is not None:
        query = query.where(Membership.user_id != exclude_user_id)
    return db.session.scalar(query) or 0


def _is_active_owner(membership: Membership) -> bool:
    return membership.role == ROLE_OWNER and bool(membership.user.is_active)


def change_role(membership: Membership, new_role: str) -> None:
    """Set ``membership.role``; raises :class:`LastOwnerError` if it would orphan the tenant."""
    lock_tenants([membership.tenant_id])
    db.session.refresh(membership)  # re-read AFTER the lock: another request may have won
    if (
        _is_active_owner(membership)
        and new_role != ROLE_OWNER
        and active_owner_count(membership.tenant_id, exclude_user_id=membership.user_id) < 1
    ):
        raise LastOwnerError()
    membership.role = new_role
    if new_role not in ASSIGNABLE_ROLES:
        clear_assignments(membership.tenant_id, membership.user_id)


def remove_membership(membership: Membership) -> None:
    """Delete the membership and clear its assignments (before the row goes)."""
    lock_tenants([membership.tenant_id])
    db.session.refresh(membership)
    if (
        _is_active_owner(membership)
        and active_owner_count(membership.tenant_id, exclude_user_id=membership.user_id) < 1
    ):
        raise LastOwnerError()
    clear_assignments(membership.tenant_id, membership.user_id)
    db.session.delete(membership)
    db.session.flush()


def deactivate_user(user: User) -> None:
    """Deactivate the account everywhere; clear assignments in every tenant."""
    owner_rows = db.session.execute(
        sa.select(Membership.tenant_id).where(
            Membership.user_id == user.id, Membership.role == ROLE_OWNER
        )
    ).all()
    tenant_ids = [row[0] for row in owner_rows]
    lock_tenants(tenant_ids)
    db.session.refresh(user)
    if user.is_active:
        blocking = [
            name
            for tenant_id in tenant_ids
            if active_owner_count(tenant_id, exclude_user_id=user.id) < 1
            for name in [db.session.get(Tenant, tenant_id).name]
        ]
        if blocking:
            raise LastOwnerError(sorted(blocking))
    user.is_active = False
    clear_assignments_everywhere(user.id)
    db.session.flush()
