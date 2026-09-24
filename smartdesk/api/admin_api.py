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
    AUTOMATION_KINDS,
    ROLE_OWNER,
    Automation,
    Booking,
    Channel,
    Conversation,
    Membership,
    ReceptionistProfile,
    Tenant,
    User,
)
from smartdesk.security.rbac import record_audit, require_platform_admin
from smartdesk.services.knowledge import seed_sections

admin_api = Blueprint("admin_api", __name__)

VALID_PLANS = ("basic", "professional", "enterprise")
VALID_STATUSES = ("active", "development", "suspended")


def _slugify(name: str) -> str:
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    while "--" in base:
        base = base.replace("--", "-")
    base = base.strip("-") or "tenant"
    slug = base
    suffix = 1
    # Guarantee uniqueness against the real unique constraint rather than
    # hoping the caller's chosen name never collides.
    while Tenant.query.filter_by(slug=slug).one_or_none() is not None:
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


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

    tenant = Tenant(
        slug=_slugify(name),
        name=name,
        business_type=payload.get("business_category") or "other",
        status=status,
        plan=plan,
        owner_name=(payload.get("owner_name") or "").strip() or None,
        owner_email=owner_email,
        owner_phone=(payload.get("phone") or "").strip() or None,
    )
    db.session.add(tenant)
    db.session.flush()

    # Same shape every other tenant gets -- an empty, honest starting point.
    # Never invents business facts for a tenant that hasn't supplied any.
    db.session.add(ReceptionistProfile(tenant_id=tenant.id, sales_mode_enabled=False))
    seed_sections(tenant, {})
    for kind in AUTOMATION_KINDS:
        db.session.add(
            Automation(
                tenant_id=tenant.id, kind=kind, name=kind.replace("_", " ").title()
            )
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
