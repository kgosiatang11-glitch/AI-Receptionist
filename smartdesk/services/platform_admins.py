"""Platform-administrator invariant (C6 follow-up).

**The platform always keeps at least one ELIGIBLE platform administrator.**

Eligible means all three of:

* ``users.is_platform_admin`` is true,
* ``users.is_active`` is true (a deactivated user is refused at authentication),
* the identity's email is confirmed by Supabase (``rbac.load_principal`` grants
  no administrator rights to an unconfirmed identity, so such a row is not a
  *usable* administrator).

Why a plain ``count()`` before the update is not enough
-------------------------------------------------------
Two administrators, A and B, each demote the other at the same moment.  Each
request counts "another administrator exists", each updates a *different* row,
so neither blocks the other and both commit: zero administrators.  The same
check-then-act race applies to deactivation.  Row locks on the two ``users``
rows do not help either (each request locks only its own target), and the
per-tenant lock used for the last-owner rule is irrelevant here: platform
administrators belong to no tenant, and an administrator may own no tenant at
all.

Strategy
--------
Every change that can *reduce* the eligible set (demote, deactivate) first takes
ONE platform-wide lock -- ``pg_advisory_xact_lock`` on a fixed key -- and only
then re-reads the world and counts the OTHER eligible administrators.  The lock
is transaction-scoped, so it is released by commit *and* by rollback, can never
leak, and works through Supabase's transaction pooler (a session-level lock
would not).  Under PostgreSQL's default READ COMMITTED isolation, every
statement after the lock is acquired sees whatever the previous holder
committed, so the second of two competing requests recounts against reality and
is refused.  Changes that cannot reduce the set (promote, activate) take no lock.

This relies on READ COMMITTED, PostgreSQL's default, which the application does
not override (``SQLALCHEMY_ENGINE_OPTIONS`` sets no ``isolation_level``).  Under
REPEATABLE READ the snapshot would be taken *before* the lock wait ends and the
recount would see stale data.  ``test_platform_admin_invariant_postgres`` fails if
the isolation level is ever changed.

Lock ordering is always: platform-admin lock -> tenant locks
(``membership_lifecycle.lock_tenants``) -> row writes.  Nothing takes them in the
other order, so the two locks cannot deadlock each other.

:func:`begin_exclusive_change` ends the request's transaction BEFORE locking.
Authentication (``rbac._sync_user``) writes ``users.last_seen_at`` for the caller,
which holds a row lock on the caller's own ``users`` row until the transaction
ends.  Two administrators demoting each other would then each hold their own row
lock while waiting for the platform lock the other holds, and PostgreSQL would
abort one of them with a deadlock error.  Starting a fresh transaction first means
nothing is held while waiting, so the loser gets a clean, deterministic refusal.

This is application-level enforcement: it protects every code path that goes
through :func:`ensure_admin_remains`.  It cannot stop someone editing the
``users`` table by hand in the Supabase SQL editor; see the C6 follow-up notes.
SQLite (tests, local dev) ignores the lock and serialises writers by itself.

Callers own the transaction and the final commit.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa

from smartdesk.extensions import db
from smartdesk.models import User
from smartdesk.security.rbac import email_confirmed_for_subject

#: Fixed 64-bit advisory-lock key for "the set of platform administrators".  Derived
#: from a name so it is stable across processes and obviously not a tenant/user id.
PLATFORM_ADMIN_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"smartdesk:platform-admin-set").digest()[:8], "big", signed=True
)


class LastPlatformAdminError(Exception):
    """The change would leave the platform without an eligible administrator."""


def lock_platform_admins() -> None:
    """Serialise every change that can reduce the eligible administrator set."""
    if db.engine.dialect.name == "postgresql":
        db.session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": PLATFORM_ADMIN_LOCK_KEY}
        )


def begin_exclusive_change() -> None:
    """Start a FRESH transaction that holds the platform-admin lock.

    Commits whatever the request has done so far (authentication bookkeeping such
    as ``last_seen_at``), then takes the lock -- see the module docstring for why
    nothing may be held while waiting.  Everything after this call is one atomic
    transaction: if it is rolled back, none of it persists.
    """
    db.session.commit()
    lock_platform_admins()


def other_eligible_admin_exists(exclude_user_id: str) -> bool:
    """Is there an active, verified platform administrator other than ``exclude_user_id``?

    Fails CLOSED: if email confirmation cannot be established for a candidate
    (Supabase function unavailable) that candidate does not count, so the change
    is refused rather than risking an unusable platform.
    """
    candidates = db.session.execute(
        sa.select(User.supabase_user_id)
        .where(
            User.is_platform_admin.is_(True),
            User.is_active.is_(True),
            User.id != exclude_user_id,
        )
        .order_by(User.created_at.asc())
    ).scalars().all()
    return any(email_confirmed_for_subject(str(subject)) for subject in candidates)


def ensure_admin_remains(
    user: User, *, new_admin: bool | None, new_active: bool | None
) -> None:
    """Raise :class:`LastPlatformAdminError` if applying the flags removes the last admin.

    Call only while holding the lock (:func:`begin_exclusive_change`).  ``user`` is
    re-read first, so the decision uses committed state, never what the request saw
    when it started.  ``None`` means "this flag is not being changed".

    The target counts as an administrator *now* when it is flagged and active
    (email confirmation is deliberately ignored for the target: when in doubt it is
    assumed to count, which can only make the check stricter).  A change that does
    not remove that standing -- or removes it from someone who never counted --
    cannot reduce the set and is allowed without looking at anyone else.
    """
    db.session.refresh(user)
    counts_now = bool(user.is_platform_admin) and bool(user.is_active)
    final_admin = user.is_platform_admin if new_admin is None else new_admin
    final_active = user.is_active if new_active is None else new_active
    counts_after = bool(final_admin) and bool(final_active)

    if counts_now and not counts_after and not other_eligible_admin_exists(user.id):
        raise LastPlatformAdminError()
