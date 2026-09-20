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
    # tenant_id -> role, for tenants this user belongs to.
    roles: dict = field(default_factory=dict)

    def role_for(self, tenant_id: str) -> str | None:
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
    """Find or create the platform-side profile for a Supabase subject."""
    user = User.query.filter_by(supabase_user_id=claims.subject).one_or_none()
    if user is None and claims.email:
        # A user invited by email before their first login.
        user = User.query.filter_by(email=claims.email).one_or_none()
        if user is not None:
            user.supabase_user_id = claims.subject

    if user is None:
        if not claims.email:
            raise AuthError("Token has no email claim; cannot provision user")
        user = User(
            supabase_user_id=claims.subject,
            email=claims.email,
            full_name=(claims.raw.get("user_metadata") or {}).get("full_name"),
        )
        db.session.add(user)

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


def load_principal() -> Principal:
    token = bearer_token_from_header(request.headers.get("Authorization"))
    claims = decode_token(token)
    user = _sync_user(claims)
    roles = {
        m.tenant_id: m.role
        for m in Membership.query.filter_by(user_id=user.id).all()
    }
    return Principal(
        user=user, is_platform_admin=user.is_platform_admin, roles=roles
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
