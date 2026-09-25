"""Self-service customer signup.

This is deliberately the ONLY way a customer-facing tenant gets created.
There is no "Super Admin creates the tenant for you" step in this flow:

    business owner signs up (Supabase Auth, already existing)
        -> POST /api/v1/signup/business (this module)
        -> tenant is created automatically from what they typed
        -> they are granted the ``owner`` role on it via a normal
           Membership row -- the same mechanism the admin "link owner"
           endpoint and ``flask grant`` already use, nothing new
        -> they are redirected to their own tenant dashboard

The Super Admin "create tenant" / "link owner" endpoints in
``smartdesk/api/admin_api.py`` are left in place for platform-staff use
(fixing a stuck account, provisioning a tenant that predates self-service,
etc). Ordinary customers never need them.

This endpoint only requires ``require_auth`` (a real Supabase identity),
not an email-confirmed one. Creating the tenant here does not by itself
expose any data: ``Principal.role_for`` in smartdesk/security/rbac.py
already refuses tenant access to an unverified email regardless of any
Membership row that exists, so this matches the "still works during
development without SMTP" requirement without weakening that gate.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from smartdesk.extensions import db
from smartdesk.models import ROLE_OWNER, Membership
from smartdesk.security.rbac import record_audit, require_auth
from smartdesk.services.tenant_provisioning import provision_new_tenant

signup_api = Blueprint("signup_api", __name__)


@signup_api.post("/signup/business")
@require_auth
def create_my_business():
    """Create the caller's own tenant and make them its owner.

    Refuses if the caller already belongs to a tenant -- self-service
    signup provisions exactly one business per account holder; anything
    beyond that (e.g. one person legitimately owning two businesses) is a
    Super Admin operation, not an automatic one.
    """
    principal = g.principal
    user = principal.user

    already_has_membership = (
        Membership.query.filter_by(user_id=user.id).first() is not None
    )
    if already_has_membership:
        return jsonify(
            {
                "error": (
                    "This account is already linked to a business. "
                    "Sign in to your dashboard instead."
                )
            }
        ), 409

    payload = request.get_json(silent=True) or {}
    business_name = (payload.get("business_name") or "").strip()
    business_email = (payload.get("business_email") or "").strip()
    phone = (payload.get("phone") or "").strip()
    location = (payload.get("location") or "").strip()

    missing = [
        label
        for label, value in (
            ("business_name", business_name),
            ("business_email", business_email),
            ("phone", phone),
            ("location", location),
        )
        if not value
    ]
    if missing:
        return jsonify(
            {"error": f"Missing required field(s): {', '.join(missing)}"}
        ), 400

    owner_name = (payload.get("full_name") or user.full_name or "").strip() or None

    tenant = provision_new_tenant(
        name=business_name,
        business_type=payload.get("business_category") or "other",
        status="active",
        plan="basic",
        owner_name=owner_name,
        owner_email=user.email,
        owner_phone=phone,
        escalation_email=business_email,
        knowledge_seed={"location": {"body": location}},
    )
    db.session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=ROLE_OWNER))

    if owner_name and not user.full_name:
        user.full_name = owner_name

    record_audit(
        "tenant.self_signup", "tenant", tenant.id, tenant_id=tenant.id,
        name=business_name, owner_email=user.email,
    )
    db.session.commit()

    return jsonify({"tenant": tenant.to_dict(), "role": ROLE_OWNER}), 201
