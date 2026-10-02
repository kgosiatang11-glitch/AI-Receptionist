"""Super Admin tenant management.

Every route here requires ``is_platform_admin`` (via ``require_platform_admin``)
-- the same flag every other admin-only route in this codebase already uses,
not a new authorization mechanism. Linking an owner reuses the existing
Membership model exactly as the CLI ``flask grant`` command does; this module
is that same operation exposed as a real endpoint, nothing more.

No automated invitation email is sent anywhere in this module -- deliberately.
The owner is expected to sign up themselves through the existing Supabase
Auth login page; a Super Admin then links that account with POST
.../owner once it exists. See the module docstring in smartdesk/app.py's
Future Improvements note (and the delivery report) for what a real
invitation-email flow would need: the Supabase service role key, which is
NOT used anywhere in this codebase and is not introduced here.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from smartdesk.extensions import db
from smartdesk.models import (
    ROLE_OWNER,
    Booking,
    Channel,
    Conversation,
    Membership,
    ReceptionistProfile,
    Tenant,
    User,
)
from smartdesk.security.rbac import record_audit, require_platform_admin
from smartdesk.services.tenant_provisioning import (
    VALID_PLANS,
    VALID_STATUSES,
    provision_new_tenant,
)

admin_api = Blueprint("admin_api", __name__)

#: Valid range for a tenant's monthly conversation limit when set through the
#: API.  The schema column is a plain NOT NULL integer with a default of 500
#: (see Tenant.monthly_conversation_limit), and enforcement currently treats a
#: stored 0 as "no limit".  The API therefore refuses 0 so that "unlimited"
#: can never be set by accident or typo; the minimum is 1.  The maximum is
#: 200x the default -- generous for any real small business, far below the
#: INTEGER column ceiling, and low enough that a stray extra digit is caught.
#: Changing either bound is a business decision, not a schema change.
MIN_MONTHLY_CONVERSATION_LIMIT = 1
MAX_MONTHLY_CONVERSATION_LIMIT = 100_000


def _parse_monthly_conversation_limit(value) -> int:
    """Return ``value`` as a valid limit or raise ``ValueError`` (message is
    safe to show to the caller).  Only genuine integers are accepted: no
    booleans (``True`` is an int in Python), strings, floats or null."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("monthly_conversation_limit must be a whole number")
    if value < MIN_MONTHLY_CONVERSATION_LIMIT or value > MAX_MONTHLY_CONVERSATION_LIMIT:
        raise ValueError(
            "monthly_conversation_limit must be between "
            f"{MIN_MONTHLY_CONVERSATION_LIMIT} and {MAX_MONTHLY_CONVERSATION_LIMIT}"
        )
    return value


def _owner_linked(tenant_id: str) -> dict | None:
    """Derive link status from Membership -- no separate state to drift out
    of sync. Returns the linked owner's {email, user_id} or None."""
    row = (
        db.session.query(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .filter(Membership.tenant_id == tenant_id, Membership.role == ROLE_OWNER)
        .order_by(Membership.created_at.asc())
        .first()
    )
    if row is None:
        return None
    user, _membership = row
    return {"email": user.email, "user_id": user.id}


def _tenant_summary(tenant: Tenant) -> dict:
    profile = ReceptionistProfile.query.filter_by(tenant_id=tenant.id).one_or_none()
    channels = Channel.query.filter_by(tenant_id=tenant.id).all()
    linked = _owner_linked(tenant.id)

    return {
        **tenant.to_dict(),
        "receptionist": {
            "exists": profile is not None,
            "is_active": bool(profile.is_active) if profile else False,
            "display_name": profile.display_name if profile else None,
        },
        "channels": {
            "whatsapp": any(c.kind == "whatsapp" and c.is_active for c in channels),
            "voice": any(c.kind == "voice" and c.is_active for c in channels),
        },
        "owner_account": (
            {"linked": True, **linked} if linked else {"linked": False}
        ),
    }


# ---------------------------------------------------------------------------
# Tenant listing / creation
# ---------------------------------------------------------------------------


@admin_api.get("/admin/tenants")
@require_platform_admin
def list_all_tenants():
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    return jsonify({"items": [_tenant_summary(t) for t in tenants]})


@admin_api.get("/admin/tenants/<tenant_id>")
@require_platform_admin
def get_tenant_detail(tenant_id: str):
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return jsonify({"error": "Tenant not found"}), 404
    return jsonify(_tenant_summary(tenant))


@admin_api.post("/admin/tenants")
@require_platform_admin
def create_tenant():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("business_name") or "").strip()
    if not name:
        return jsonify({"error": "business_name is required"}), 400

    plan = payload.get("plan", "basic")
    if plan not in VALID_PLANS:
        return jsonify({"error": f"plan must be one of {VALID_PLANS}"}), 400

    status = payload.get("status", "active")
    if status not in VALID_STATUSES:
        return jsonify({"error": f"status must be one of {VALID_STATUSES}"}), 400

    owner_email = (payload.get("owner_email") or "").strip() or None

    tenant = provision_new_tenant(
        name=name,
        business_type=payload.get("business_category") or "other",
        status=status,
        plan=plan,
        owner_name=(payload.get("owner_name") or "").strip() or None,
        owner_email=owner_email,
        owner_phone=(payload.get("phone") or "").strip() or None,
    )

    record_audit(
        "tenant.create", "tenant", tenant.id, tenant_id=tenant.id,
        name=name, owner_email=owner_email,
    )
    db.session.commit()
    return jsonify(_tenant_summary(tenant)), 201


@admin_api.patch("/admin/tenants/<tenant_id>")
@require_platform_admin
def update_tenant(tenant_id: str):
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return jsonify({"error": "Tenant not found"}), 404

    payload = request.get_json(silent=True) or {}

    # Never silently ignored, never applied without validation + a dedicated
    # audit entry: the limit has its own endpoint (below).
    if "monthly_conversation_limit" in payload:
        return jsonify(
            {
                "error": (
                    "monthly_conversation_limit is not changed here; use "
                    "PATCH /admin/tenants/<tenant_id>/monthly-limit"
                )
            }
        ), 400

    if "business_name" in payload:
        name = (payload["business_name"] or "").strip()
        if not name:
            return jsonify({"error": "business_name cannot be empty"}), 400
        tenant.name = name
    if "business_category" in payload:
        tenant.business_type = payload["business_category"] or "other"
    if "plan" in payload:
        if payload["plan"] not in VALID_PLANS:
            return jsonify({"error": f"plan must be one of {VALID_PLANS}"}), 400
        tenant.plan = payload["plan"]
    if "status" in payload:
        if payload["status"] not in VALID_STATUSES:
            return jsonify({"error": f"status must be one of {VALID_STATUSES}"}), 400
        tenant.status = payload["status"]
    for field in ("owner_name", "owner_email", "phone"):
        if field in payload:
            column = "owner_phone" if field == "phone" else field
            setattr(tenant, column, (payload[field] or "").strip() or None)

    record_audit("tenant.update", "tenant", tenant.id)
    db.session.commit()
    return jsonify(_tenant_summary(tenant))


@admin_api.patch("/admin/tenants/<tenant_id>/monthly-limit")
@require_platform_admin
def update_tenant_monthly_limit(tenant_id: str):
    """Set one tenant's monthly conversation limit (platform admin only).

    The tenant is addressed by id in the URL and nothing in the body can
    redirect the change to another tenant.  The old and new values are
    recorded in the audit log.
    """
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return jsonify({"error": "Tenant not found"}), 404

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "monthly_conversation_limit" not in payload:
        return jsonify({"error": "monthly_conversation_limit is required"}), 400

    try:
        new_limit = _parse_monthly_conversation_limit(
            payload["monthly_conversation_limit"]
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    old_limit = tenant.monthly_conversation_limit
    tenant.monthly_conversation_limit = new_limit
    record_audit(
        "tenant.monthly_limit.update",
        "tenant",
        tenant.id,
        tenant_id=tenant.id,
        old_monthly_conversation_limit=old_limit,
        new_monthly_conversation_limit=new_limit,
    )
    db.session.commit()
    return jsonify(
        {
            "id": tenant.id,
            "slug": tenant.slug,
            "monthly_conversation_limit": new_limit,
            "previous_monthly_conversation_limit": old_limit,
        }
    )


# ---------------------------------------------------------------------------
# Owner linking -- reuses Membership exactly as `flask grant` does
# ---------------------------------------------------------------------------


@admin_api.post("/admin/tenants/<tenant_id>/owner")
@require_platform_admin
def link_owner(tenant_id: str):
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        return jsonify({"error": "Tenant not found"}), 404

    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email is required"}), 400

    user = User.query.filter_by(email=email).one_or_none()
    if user is None:
        # This is the expected, normal case before an owner has signed up --
        # not a server error. The Super Admin needs to know to wait, not
        # get a 500.
        return jsonify(
            {
                "error": (
                    f"No account found for {email}. They need to sign up via "
                    "the login page first, then you can link them."
                )
            }
        ), 404

    existing = Membership.query.filter_by(
        tenant_id=tenant.id, user_id=user.id
    ).one_or_none()
    if existing is not None and existing.role == ROLE_OWNER:
        # Idempotent: linking an already-linked owner again is a success,
        # not an error -- the Super Admin's intent ("this person should be
        # the owner") is already satisfied.
        return jsonify(_tenant_summary(tenant))

    if existing is not None:
        existing.role = ROLE_OWNER
    else:
        db.session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=ROLE_OWNER))

    record_audit(
        "tenant.link_owner", "tenant", tenant.id, owner_email=email, user_id=user.id
    )
    db.session.commit()
    return jsonify(_tenant_summary(tenant))


# ---------------------------------------------------------------------------
# Platform-wide overview
# ---------------------------------------------------------------------------


@admin_api.get("/admin/overview")
@require_platform_admin
def platform_overview():
    tenants = Tenant.query.all()
    active_tenants = sum(1 for t in tenants if t.status == "active")

    active_receptionists = ReceptionistProfile.query.filter_by(is_active=True).count()
    connected_channels = Channel.query.filter_by(is_active=True).count()
    total_conversations = Conversation.query.count()

    return jsonify(
        {
            "total_tenants": len(tenants),
            "active_tenants": active_tenants,
            "inactive_tenants": len(tenants) - active_tenants,
            "active_receptionists": active_receptionists,
            "connected_channels": connected_channels,
            "total_conversations": total_conversations,
        }
    )


# ---------------------------------------------------------------------------
# Platform-wide receptionists / channels -- read-only rollups for the
# Control Center sidebar. No numbers here are invented: each row is a real
# ReceptionistProfile/Channel joined back to its tenant.
# ---------------------------------------------------------------------------


@admin_api.get("/admin/receptionists")
@require_platform_admin
def list_all_receptionists():
    rows = (
        db.session.query(ReceptionistProfile, Tenant)
        .join(Tenant, Tenant.id == ReceptionistProfile.tenant_id)
        .order_by(Tenant.name)
        .all()
    )
    return jsonify(
        {
            "items": [
                {
                    "tenant_id": tenant.id,
                    "tenant_name": tenant.name,
                    "display_name": profile.display_name,
                    "is_active": profile.is_active,
                }
                for profile, tenant in rows
            ]
        }
    )


@admin_api.get("/admin/channels")
@require_platform_admin
def list_all_channels():
    rows = (
        db.session.query(Channel, Tenant)
        .join(Tenant, Tenant.id == Channel.tenant_id)
        .order_by(Tenant.name, Channel.kind)
        .all()
    )
    return jsonify(
        {
            "items": [
                {
                    "tenant_id": tenant.id,
                    "tenant_name": tenant.name,
                    "kind": channel.kind,
                    "address": channel.address,
                    "is_active": channel.is_active,
                }
                for channel, tenant in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# Platform-wide user management
# ---------------------------------------------------------------------------


def _last_owner_tenants(user_id: str) -> list[str]:
    """Tenant names where this user is the *only* owner.

    Used to block an action (deactivation) that would leave a tenant with
    no owner at all -- the same protection the tenant-facing member-removal
    endpoints apply, kept in sync here rather than duplicated ad hoc.
    """
    owner_rows = (
        db.session.query(Tenant.id, Tenant.name)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .filter(Membership.user_id == user_id, Membership.role == ROLE_OWNER)
        .all()
    )
    names = []
    for tenant_id, tenant_name in owner_rows:
        owner_count = Membership.query.filter_by(
            tenant_id=tenant_id, role=ROLE_OWNER
        ).count()
        if owner_count <= 1:
            names.append(tenant_name)
    return names


def _user_summary(user: User) -> dict:
    rows = (
        db.session.query(Membership, Tenant)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .filter(Membership.user_id == user.id)
        .order_by(Tenant.name)
        .all()
    )
    return {
        **user.to_dict(),
        "is_active": user.is_active,
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "memberships": [
            {"tenant_id": tenant.id, "tenant_name": tenant.name, "role": membership.role}
            for membership, tenant in rows
        ],
    }


@admin_api.get("/admin/users")
@require_platform_admin
def list_users():
    search = (request.args.get("q") or "").strip()
    query = User.query
    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(User.email.ilike(like), User.full_name.ilike(like))
        )
    users = query.order_by(User.created_at.desc()).all()
    return jsonify({"items": [_user_summary(u) for u in users]})


@admin_api.patch("/admin/users/<user_id>")
@require_platform_admin
def update_user(user_id: str):
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "User not found"}), 404

    payload = request.get_json(silent=True) or {}
    actor = g.principal.user
    changes = {}

    if "is_platform_admin" in payload:
        new_value = bool(payload["is_platform_admin"])
        if not new_value and user.id == actor.id:
            return jsonify(
                {"error": "You cannot remove your own platform administrator access."}
            ), 400
        user.is_platform_admin = new_value
        changes["is_platform_admin"] = new_value

    if "is_active" in payload:
        new_value = bool(payload["is_active"])
        if not new_value:
            if user.id == actor.id:
                return jsonify({"error": "You cannot deactivate your own account."}), 400
            blocking = _last_owner_tenants(user.id)
            if blocking:
                return jsonify(
                    {
                        "error": (
                            "This user is the only owner of "
                            f"{', '.join(blocking)}. Assign another owner "
                            "there first."
                        )
                    }
                ), 400
        user.is_active = new_value
        changes["is_active"] = new_value

    if not changes:
        return jsonify({"error": "Nothing to update"}), 400

    record_audit("user.update", "user", user.id, **changes)
    db.session.commit()
    return jsonify(_user_summary(user))
