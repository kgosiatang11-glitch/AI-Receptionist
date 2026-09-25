"""Authentication and authorization decorators for the Control Center API.

Every rule here is enforced server-side.  The React app hides navigation the
user cannot use, but hiding is cosmetic: an unauthorized request fails at the
API regardless of what the frontend sends.

Tenant selection comes from the ``X-Tenant-Id`` header (or ``?tenant_id=``).
It is *always* validated against the caller's memberships, so supplying
another tenant's id yields 403, never that tenant's data.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from flask import current_app, g, request

from smartdesk.extensions import db
from smartdesk.models import (
    ROLE_MANAGER,
    ROLE_ORDER,
    ROLE_OWNER,
    ROLE_VIEWER,
    AuditLog,
    Membership,
    Tenant,
    User,
    utcnow,
)
from smartdesk.security.jwt_auth import AuthError, bearer_token_from_header, decode_token


@dataclass
class Principal:
    user: User
    is_platform_admin: bool
    # Whether Supabase has actually confirmed this identity's email. See
    # ``_is_email_confirmed`` below for how this is determined and why the
    # token itself is not trusted for it.
    email_verified: bool = True
    # tenant_id -> role, for tenants this user belongs to.
    roles: dict = field(default_factory=dict)

    def role_for(self, tenant_id: str) -> str | None:
        if not self.email_verified:
            # No tenant access -- not even platform-admin access -- until
            # the email is confirmed. See requirement: unverified email
            # must never be treated as trusted.
            return None
        if self.is_platform_admin:
            # Platform admins act with owner-level rights on any tenant.
            return ROLE_OWNER
        return self.roles.get(tenant_id)

    def can_access(self, tenant_id: str) -> bool:
        return self.role_for(tenant_id) is not None


def role_at_least(role: str | None, minimum: str) -> bool:
    if role is None:
        return False
    try:
        return ROLE_ORDER.index(role) >= ROLE_ORDER.index(minimum)
    except ValueError:
        return False


def _sync_user(claims) -> User:
    """Find or create the platform-side profile for a Supabase subject.

    Users are matched by the verified Supabase subject id (``sub``) only.
    A previous version of this function also matched by email when no
    subject match was found, and silently repointed that row's
    ``supabase_user_id`` to the new subject. That is an account takeover:
    any Supabase identity presenting a matching email string -- not
    necessarily the same person -- would inherit the existing user's
    memberships on its very first request.

    Nothing in this codebase actually relies on that rebind. The only way
    a Membership is ever granted is ``flask grant`` / the admin "link
    owner" endpoint, and both require the user to already have signed in
    via Supabase at least once (i.e. to already have a matching
    ``supabase_user_id`` row) -- see ``smartdesk/seeds.py``. So there is no
    legitimate "invited before first login" case for this to handle.

    The fix is therefore simply: never rebind. An email collision with a
    different subject is treated as a conflict, not an invitation, and is
    refused rather than resolved by guessing.
    """
    user = User.query.filter_by(supabase_user_id=claims.subject).one_or_none()

    if user is None:
        if not claims.email:
            raise AuthError("Token has no email claim; cannot provision user")

        conflicting = User.query.filter_by(email=claims.email).one_or_none()
        if conflicting is not None:
            current_app.logger.warning(
                "Refusing to rebind user %s: email %s is already bound to "
                "supabase_user_id=%s, but this token's subject is %s",
                conflicting.id,
                claims.email,
                conflicting.supabase_user_id,
                claims.subject,
            )
            raise AuthError(
                "This email is already associated with a different "
                "account. Contact SmartDesk support.",
                status=409,
            )

        user = User(
            supabase_user_id=claims.subject,
            email=claims.email,
            full_name=(claims.raw.get("user_metadata") or {}).get("full_name"),
        )
        db.session.add(user)
        # Column defaults (is_active=True, id=uuid4()) are only applied by
        # SQLAlchemy at flush time, not on construction. Flushing here --
        # before the is_active check below -- avoids rejecting every
        # brand-new signup with a false "This account has been deactivated"
        # (the in-memory attribute would otherwise still read None).
        db.session.flush()

    # Bootstrap: emails listed in PLATFORM_ADMIN_EMAILS are platform staff.
    # The flag is persisted in our DB and is the only source of truth for it.
    if claims.email and claims.email in current_app.config.get(
        "PLATFORM_ADMIN_EMAILS", ()
    ):
        user.is_platform_admin = True

    if not user.is_active:
        raise AuthError("This account has been deactivated", status=403)

    user.last_seen_at = utcnow()
    db.session.flush()
    return user


def _is_email_confirmed(claims) -> bool:
    """Whether Supabase has actually confirmed this identity's email.

    The access token itself is not a safe source for this. The only
    verification-shaped field Supabase puts in the JWT is
    ``user_metadata.email_verified``, and ``user_metadata`` can be edited
    by the signed-in user themselves via ``supabase.auth.updateUser({data:
    {...}})`` -- so trusting it would let anyone flip their own flag to
    true. Nothing else in the default token is authoritative either.

    The real, authoritative value is the ``email_confirmed_at`` column on
    Supabase's own ``auth.users`` table. This function reads that value
    through the ``public.is_email_confirmed`` SECURITY DEFINER function,
    so the application does not need direct access to the ``auth`` schema.

    On the SQLite database used in tests (and any non-Postgres backend)
    there is no ``auth`` schema, so this returns True there by default;
    tests that need to exercise the unverified path patch this function
    directly (see tests/test_email_verification.py).
    """
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not db_uri.startswith("postgres"):
        return True
    try:
        row = db.session.execute(
            db.text("SELECT public.is_email_confirmed(:sub)"),
            {"sub": claims.subject},
        ).first()
    except Exception:
        # Fail closed: if we cannot prove the email is confirmed, treat it
        # as unconfirmed rather than trusting it. See the Phase 1 delivery
        # notes for what this means operationally and how to verify it
        # works against your project before relying on it.
        current_app.logger.exception(
            "Could not verify email confirmation for subject %s; "
            "denying tenant access until this is resolved",
            claims.subject,
        )
        return False
    return bool(row and row[0] is not None)


def load_principal() -> Principal:
    token = bearer_token_from_header(request.headers.get("Authorization"))
    claims = decode_token(token)
    user = _sync_user(claims)
    roles = {
        m.tenant_id: m.role
        for m in Membership.query.filter_by(user_id=user.id).all()
    }
    return Principal(
        user=user,
        is_platform_admin=user.is_platform_admin,
        email_verified=_is_email_confirmed(claims),
        roles=roles,
    )


def require_auth(view):
    """Authenticate the caller and attach ``g.principal``."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        g.principal = load_principal()
        return view(*args, **kwargs)

    return wrapper


def _resolve_requested_tenant(principal: Principal) -> Tenant:
    tenant_id = (
        request.headers.get("X-Tenant-Id")
        or request.args.get("tenant_id")
        or ""
    ).strip()

    if not tenant_id:
        # Default to the caller's single tenant when unambiguous.
        if not principal.is_platform_admin and len(principal.roles) == 1:
            tenant_id = next(iter(principal.roles))
        else:
            raise AuthError("A tenant must be selected for this request", status=400)

    if not principal.can_access(tenant_id):
        # Deliberately identical to the not-found response so that a probing
        # caller cannot enumerate which tenant ids exist on the platform.
        raise AuthError("Tenant not found or access denied", status=403)

    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        raise AuthError("Tenant not found or access denied", status=403)
    return tenant


def require_tenant(minimum_role: str = ROLE_VIEWER):
    """Authenticate, resolve the active tenant, and enforce a minimum role.

    Sets ``g.principal``, ``g.tenant`` and ``g.tenant_role``, and binds the
    Postgres RLS session variable for the transaction.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            principal = load_principal()
            tenant = _resolve_requested_tenant(principal)
            role = principal.role_for(tenant.id)
            if not role_at_least(role, minimum_role):
                raise AuthError(
                    "Your role does not permit this action", status=403
                )
            g.principal = principal
            g.tenant = tenant
            g.tenant_role = role
            bind_rls_tenant(tenant.id)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_platform_admin(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        principal = load_principal()
        if not principal.is_platform_admin:
            raise AuthError("Platform administrator access required", status=403)
        g.principal = principal
        return view(*args, **kwargs)

    return wrapper


def bind_rls_tenant(tenant_id: str) -> None:
    """Bind ``app.current_tenant_id`` for the current transaction.

    This is the third isolation layer: Postgres row-level security policies
    filter on this value, so even an unscoped query cannot cross tenants.
    """
    if not current_app.config.get("SQLALCHEMY_DATABASE_URI"):
        return
    try:
        db.session.execute(
            db.text("SELECT set_config('app.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
    except Exception:  # pragma: no cover - non-Postgres backends in tests
        current_app.logger.debug("Could not bind RLS tenant GUC", exc_info=True)


def record_audit(
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    tenant_id: str | None = None,
    **meta,
) -> None:
    """Write an audit row for an administrative action."""
    principal = getattr(g, "principal", None)
    tenant = getattr(g, "tenant", None)
    entry = AuditLog(
        tenant_id=tenant_id or (tenant.id if tenant else None),
        actor_user_id=principal.user.id if principal else None,
        actor_email=principal.user.email if principal else None,
        action=action,
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        meta=meta or {},
    )
    db.session.add(entry)


#: Convenience aliases used by the API modules.
require_manager = functools.partial(require_tenant, ROLE_MANAGER)
require_owner = functools.partial(require_tenant, ROLE_OWNER)
