"""Consent-based membership invitations (C6).

Why this exists: "owner adds any registered user by email" attached people to a
tenant without their consent and answered "is this email registered?" with a
different status code.  An invitation instead:

* attaches nobody when created, and is created with the SAME response whether
  or not the address belongs to an account;
* is bound to one tenant, one email and one role, and expires;
* is accepted only by an authenticated user whose VERIFIED email equals the
  invited address AND who holds the secret token (shown once to the inviter,
  stored only as a SHA-256 hash);
* is single-use: acceptance is an atomic ``pending -> accepted`` UPDATE, so a
  replay (or two concurrent accepts) creates at most one membership.

Every failure to accept -- unknown token, wrong user, expired, revoked, used,
tenant unavailable -- raises the same :class:`InvitationInvalid`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from smartdesk.extensions import db
from smartdesk.models import Invitation, Membership, Tenant, User, utcnow
from smartdesk.tenancy import tenant_access_denial

INVITATION_TTL = timedelta(days=7)
MAX_EMAIL_LENGTH = 160


class InvitationInvalid(Exception):
    """The invitation cannot be accepted (deliberately without saying why)."""


class InvalidInvitationRequest(ValueError):
    """The request to create an invitation is malformed."""


class AlreadyMember(Exception):
    """The address already belongs to a member of THIS tenant."""


def normalize_email(value) -> str:
    if not isinstance(value, str):
        raise InvalidInvitationRequest("email is required")
    email = value.strip().lower()
    local, _, domain = email.partition("@")
    if not email or len(email) > MAX_EMAIL_LENGTH or not local or "." not in domain or " " in email:
        raise InvalidInvitationRequest("A valid email address is required")
    return email


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes for timezone-aware columns.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_invitation(tenant: Tenant, inviter: User, email, role: str) -> tuple[Invitation, str]:
    """Create a pending invitation; returns ``(invitation, plaintext_token)``.

    Any earlier pending invitation for the same tenant+email is revoked first,
    so only the newest token works.  Raises :class:`AlreadyMember` only for a
    member of ``tenant`` itself -- information the inviting owner already sees
    on their own team page, and nothing about any other tenant.
    """
    email = normalize_email(email)

    already = db.session.execute(
        sa.select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant.id, sa.func.lower(User.email) == email)
    ).first()
    if already is not None:
        raise AlreadyMember()

    db.session.execute(
        sa.update(Invitation)
        .where(
            Invitation.tenant_id == tenant.id,
            Invitation.email == email,
            Invitation.status == "pending",
        )
        .values(status="revoked")
    )
    db.session.flush()

    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        tenant_id=tenant.id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        status="pending",
        expires_at=utcnow() + INVITATION_TTL,
        invited_by_user_id=inviter.id,
    )
    db.session.add(invitation)
    db.session.flush()
    return invitation, token


def accept_invitation(user: User, token) -> tuple[Membership, Tenant]:
    """Consume ``token`` for ``user``; returns their membership and the tenant.

    The caller must already have required a *verified* email for ``user``.
    """
    if not isinstance(token, str) or not token.strip():
        raise InvitationInvalid()

    invitation = db.session.execute(
        sa.select(Invitation).where(Invitation.token_hash == hash_token(token.strip()))
    ).scalar_one_or_none()
    if (
        invitation is None
        or invitation.status != "pending"
        or _aware(invitation.expires_at) <= utcnow()
        or invitation.email != (user.email or "").strip().lower()
    ):
        raise InvitationInvalid()

    tenant = db.session.get(Tenant, invitation.tenant_id)
    if tenant_access_denial(tenant) is not None:
        raise InvitationInvalid()

    # The caller has no tenant yet; the membership row we are about to write
    # belongs to THIS invitation's tenant, so bind row-level security to it.
    from smartdesk.security.rbac import bind_rls_tenant

    bind_rls_tenant(tenant.id)

    # Atomic claim: of any number of concurrent/replayed accepts, one UPDATE
    # matches ``status = 'pending'``.
    claimed = db.session.execute(
        sa.update(Invitation)
        .where(Invitation.id == invitation.id, Invitation.status == "pending")
        .values(status="accepted", accepted_by_user_id=user.id, accepted_at=utcnow())
    )
    if claimed.rowcount != 1:
        raise InvitationInvalid()

    membership = Membership.query.filter_by(
        tenant_id=invitation.tenant_id, user_id=user.id
    ).one_or_none()
    if membership is None:
        try:
            with db.session.begin_nested():
                membership = Membership(
                    tenant_id=invitation.tenant_id, user_id=user.id, role=invitation.role
                )
                db.session.add(membership)
                db.session.flush()
        except IntegrityError:
            membership = Membership.query.filter_by(
                tenant_id=invitation.tenant_id, user_id=user.id
            ).one()
    # An existing member keeps their existing role: an invitation can never be
    # used to change (in particular, to lower) someone's current role.
    return membership, tenant
