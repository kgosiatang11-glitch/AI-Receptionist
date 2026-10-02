"""Configuration and administration endpoints.

Secret values (Twilio auth token, OpenAI key, Supabase service key) are never
returned by any route here.  Channel routes expose derived status only — a
number, whether it is active, and whether credentials are present — so the
frontend can render "Connected" without ever holding a credential.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from smartdesk.extensions import db
from smartdesk.models import (
    AUTOMATION_KINDS,
    KNOWLEDGE_SECTIONS,
    ROLE_AGENT,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_VIEWER,
    AuditLog,
    Automation,
    Channel,
    Conversation,
    KnowledgeDocument,
    Membership,
    Tenant,
    User,
)
from smartdesk.security.rbac import (
    record_audit,
    require_auth,
    require_platform_admin,
    require_tenant,
)
from smartdesk.services.knowledge import SECTION_LABELS, ensure_profile, get_profile
from smartdesk.tenancy import normalize_address, tenant_query

config_api = Blueprint("config_api", __name__)


# ---------------------------------------------------------------------------
# Account and tenant switching
# ---------------------------------------------------------------------------


@config_api.get("/me")
@require_auth
def me():
    """Identity plus the tenants this caller may open.

    A platform admin sees every tenant (this is the tenant switcher). A
    business user sees only their own memberships — the API simply never
    returns the others.
    """
    principal = g.principal
    user_payload = {**principal.user.to_dict(), "email_verified": principal.email_verified}

    if not principal.email_verified:
        # Never hand back membership info -- not even that memberships
        # exist -- until Supabase has confirmed this identity's email.
        # The frontend uses email_verified to show a distinct "check your
        # email" state rather than the "no business assigned" state.
        db.session.commit()
        return jsonify({"user": user_payload, "tenants": [], "can_switch_tenants": False})

    if principal.is_platform_admin:
        tenants = Tenant.query.order_by(Tenant.name).all()
        memberships = [
            {**t.to_dict(), "role": "platform_admin"} for t in tenants
        ]
    else:
        rows = (
            db.session.query(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .filter(Membership.user_id == principal.user.id)
            .order_by(Tenant.name)
            .all()
        )
        memberships = [{**t.to_dict(), "role": m.role} for t, m in rows]

    db.session.commit()
    return jsonify(
        {
            "user": user_payload,
            "tenants": memberships,
            "can_switch_tenants": principal.is_platform_admin
            or len(memberships) > 1,
        }
    )


@config_api.get("/tenants")
@require_platform_admin
def list_tenants():
    return jsonify(
        {"items": [t.to_dict() for t in Tenant.query.order_by(Tenant.name).all()]}
    )


@config_api.get("/audit-logs")
@require_platform_admin
def audit_logs():
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return jsonify({"items": [row.to_dict() for row in rows]})


# ---------------------------------------------------------------------------
# Business settings
# ---------------------------------------------------------------------------


@config_api.get("/business")
@require_tenant()
def get_business():
    tenant = g.tenant
    members = (
        db.session.query(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .filter(Membership.tenant_id == tenant.id)
        .all()
    )
    return jsonify(
        {
            "tenant": {
                **tenant.to_dict(),
                "escalation_whatsapp": tenant.escalation_whatsapp,
                "escalation_email": tenant.escalation_email,
                "monthly_conversation_limit": tenant.monthly_conversation_limit,
            },
            "members": [
                {**u.to_dict(), "role": m.role, "membership_id": m.id}
                for u, m in members
            ],
            "your_role": g.tenant_role,
        }
    )


#: Fields a tenant owner may change on their own business.  This is an
#: allow-list: anything else in the payload is never written to the tenant.
OWNER_EDITABLE_BUSINESS_FIELDS = (
    "name",
    "timezone",
    "escalation_whatsapp",
    "escalation_email",
)


@config_api.patch("/business")
@require_tenant(ROLE_OWNER)
def update_business():
    tenant = g.tenant
    payload = request.get_json(silent=True) or {}

    # The monthly conversation limit is a plan/cost control owned by the
    # platform, not by the tenant.  An owner who could raise (or zero) it
    # could remove their own spending cap, so the request is refused outright
    # -- before ANY field is applied -- rather than silently ignored.  It is
    # changed only through PATCH /admin/tenants/<id>/monthly-limit, which
    # requires platform-admin access.
    if "monthly_conversation_limit" in payload:
        return jsonify(
            {
                "error": (
                    "monthly_conversation_limit can only be changed by SmartDesk "
                    "support. Contact SmartDesk to change your plan limit."
                )
            }
        ), 403

    for field in OWNER_EDITABLE_BUSINESS_FIELDS:
        if field in payload:
            setattr(tenant, field, payload[field])
    record_audit("business.update", "tenant", tenant.id)
    db.session.commit()
    return jsonify(tenant.to_dict())


# ---------------------------------------------------------------------------
# Team / membership management -- owner-only.
#
# No invitation email is sent: the person must already have a SmartDesk
# account (i.e. have signed in via Supabase at least once) before an owner
# can add them. This mirrors the Super Admin owner-linking flow in
# smartdesk/api/admin_api.py rather than inventing a second mechanism.
# ---------------------------------------------------------------------------

MEMBER_ROLES = (ROLE_VIEWER, ROLE_AGENT, ROLE_MANAGER, ROLE_OWNER)


def _owner_count(tenant_id) -> int:
    return Membership.query.filter_by(tenant_id=tenant_id, role=ROLE_OWNER).count()


@config_api.post("/business/members")
@require_tenant(ROLE_OWNER)
def add_member():
    tenant = g.tenant
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    role = payload.get("role") or ROLE_VIEWER

    if not email:
        return jsonify({"error": "email is required"}), 400
    if role not in MEMBER_ROLES:
        return jsonify({"error": f"role must be one of {MEMBER_ROLES}"}), 400

    user = User.query.filter_by(email=email).one_or_none()
    if user is None:
        # Expected, normal case -- not a server error. They need to sign up
        # first; no invitation email is faked here.
        return jsonify(
            {
                "error": (
                    f"No account found for {email}. They need to sign up via "
                    "the login page first, then you can add them here."
                )
            }
        ), 404

    existing = Membership.query.filter_by(
        tenant_id=tenant.id, user_id=user.id
    ).one_or_none()
    if existing is not None:
        return jsonify({"error": "This person is already a member of this business."}), 409

    membership = Membership(tenant_id=tenant.id, user_id=user.id, role=role)
    db.session.add(membership)
    db.session.flush()
    record_audit(
        "member.add", "membership", membership.id, tenant_id=tenant.id,
        email=email, role=role,
    )
    db.session.commit()
    return jsonify(
        {**user.to_dict(), "role": membership.role, "membership_id": membership.id}
    ), 201


@config_api.patch("/business/members/<membership_id>")
@require_tenant(ROLE_OWNER)
def update_member(membership_id: str):
    tenant = g.tenant
    membership = Membership.query.filter_by(
        id=membership_id, tenant_id=tenant.id
    ).one_or_none()
    if membership is None:
        return jsonify({"error": "Member not found"}), 404

    payload = request.get_json(silent=True) or {}
    new_role = payload.get("role")
    if new_role not in MEMBER_ROLES:
        return jsonify({"error": f"role must be one of {MEMBER_ROLES}"}), 400

    if (
        membership.role == ROLE_OWNER
        and new_role != ROLE_OWNER
        and _owner_count(tenant.id) <= 1
    ):
        return jsonify(
            {
                "error": (
                    "This is the last owner of this business. Promote "
                    "another member to owner before changing this role."
                )
            }
        ), 400

    membership.role = new_role
    record_audit(
        "member.update_role", "membership", membership.id, tenant_id=tenant.id,
        role=new_role,
    )
    db.session.commit()
    return jsonify({"membership_id": membership.id, "role": membership.role})


@config_api.delete("/business/members/<membership_id>")
@require_tenant(ROLE_OWNER)
def remove_member(membership_id: str):
    tenant = g.tenant
    membership = Membership.query.filter_by(
        id=membership_id, tenant_id=tenant.id
    ).one_or_none()
    if membership is None:
        return jsonify({"error": "Member not found"}), 404

    if membership.role == ROLE_OWNER and _owner_count(tenant.id) <= 1:
        return jsonify(
            {"error": "This is the last owner of this business and cannot be removed."}
        ), 400

    record_audit(
        "member.remove", "membership", membership.id, tenant_id=tenant.id,
    )
    db.session.delete(membership)
    db.session.commit()
    return "", 204


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


@config_api.get("/knowledge")
@require_tenant()
def list_knowledge():
    documents = {
        d.section: d.to_dict() for d in tenant_query(KnowledgeDocument).all()
    }
    return jsonify(
        {
            "sections": [
                documents.get(
                    section,
                    {
                        "section": section,
                        "title": SECTION_LABELS[section],
                        "body": None,
                        "data": {},
                        "is_published": False,
                    },
                )
                for section in KNOWLEDGE_SECTIONS
            ]
        }
    )


@config_api.put("/knowledge/<section>")
@require_tenant(ROLE_MANAGER)
def update_knowledge(section: str):
    if section not in KNOWLEDGE_SECTIONS:
        return jsonify({"error": "Unknown knowledge section"}), 404
    payload = request.get_json(silent=True) or {}

    document = (
        tenant_query(KnowledgeDocument)
        .filter(KnowledgeDocument.section == section)
        .one_or_none()
    )
    if document is None:
        document = KnowledgeDocument(
            tenant_id=g.tenant.id, section=section, title=SECTION_LABELS[section]
        )
        db.session.add(document)

    document.body = payload.get("body", document.body)
    if "data" in payload and isinstance(payload["data"], dict):
        document.data = payload["data"]
    document.is_published = bool(payload.get("is_published", True)) and bool(
        document.body or document.data
    )
    document.updated_by_user_id = g.principal.user.id

    record_audit("knowledge.update", "knowledge_document", document.id, section=section)
    db.session.commit()
    return jsonify(document.to_dict())


# ---------------------------------------------------------------------------
# AI receptionist configuration
# ---------------------------------------------------------------------------


@config_api.get("/receptionist")
@require_tenant()
def get_receptionist():
    profile = ensure_profile(g.tenant)
    db.session.commit()
    return jsonify(profile.to_dict())


@config_api.patch("/receptionist")
@require_tenant(ROLE_MANAGER)
def update_receptionist():
    profile = ensure_profile(g.tenant)
    payload = request.get_json(silent=True) or {}

    for field in (
        "display_name",
        "greeting",
        "voice_greeting",
        "personality",
        "business_instructions",
        "handoff_message",
        "external_booking_url",
    ):
        if field in payload:
            setattr(profile, field, payload[field])

    if "booking_mode" in payload:
        if payload["booking_mode"] not in ("calendar", "external"):
            return jsonify({"error": "booking_mode must be 'calendar' or 'external'"}), 400
        profile.booking_mode = payload["booking_mode"]

    if "default_booking_duration_minutes" in payload:
        try:
            minutes = int(payload["default_booking_duration_minutes"])
        except (TypeError, ValueError):
            return jsonify({"error": "default_booking_duration_minutes must be a number"}), 400
        if minutes < 5 or minutes > 480:
            return jsonify(
                {"error": "default_booking_duration_minutes must be between 5 and 480"}
            ), 400
        profile.default_booking_duration_minutes = minutes

    for flag in (
        "is_active",
        "handoff_enabled",
        "lead_capture_enabled",
        "booking_assistance_enabled",
    ):
        if flag in payload:
            setattr(profile, flag, bool(payload[flag]))

    # sales_mode is platform-controlled: a business user must not be able to
    # turn their receptionist into a SmartDesk salesperson, and SmartDesk's
    # own sales behaviour must not leak into a client tenant by accident.
    if "sales_mode_enabled" in payload:
        if not g.principal.is_platform_admin:
            return jsonify(
                {"error": "Only a SmartDesk administrator can change sales mode"}
            ), 403
        profile.sales_mode_enabled = bool(payload["sales_mode_enabled"])
        record_audit(
            "receptionist.sales_mode", "receptionist_profile", profile.id,
            enabled=profile.sales_mode_enabled,
        )

    record_audit("receptionist.update", "receptionist_profile", profile.id)
    db.session.commit()
    return jsonify(profile.to_dict())


# ---------------------------------------------------------------------------
# Channels (WhatsApp / Voice)
# ---------------------------------------------------------------------------


def _channel_payload(channel: Channel, conversation_count: int) -> dict:
    return {
        "id": channel.id,
        "kind": channel.kind,
        "address": channel.address,
        "display_name": channel.display_name,
        "is_active": channel.is_active,
        "last_inbound_at": channel.last_inbound_at.isoformat()
        if channel.last_inbound_at
        else None,
        "conversations": conversation_count,
        # Derived status only. No credential ever crosses this boundary.
        "provider_configured": bool(current_app.config.get("TWILIO_ACCOUNT_SID")),
        "signature_validation": bool(
            current_app.config.get("TWILIO_VALIDATE_SIGNATURE")
        ),
        "webhook_url": (
            f"{current_app.config.get('TWILIO_WEBHOOK_BASE_URL', '')}"
            f"{'/whatsapp' if channel.kind == 'whatsapp' else '/voice'}"
        )
        or None,
    }


@config_api.get("/channels/<kind>")
@require_tenant()
def get_channels(kind: str):
    if kind not in ("whatsapp", "voice"):
        return jsonify({"error": "Unknown channel kind"}), 404
    channels = tenant_query(Channel).filter(Channel.kind == kind).all()
    items = []
    for channel in channels:
        count = tenant_query(Conversation).filter(
            Conversation.channel_id == channel.id
        ).count()
        items.append(_channel_payload(channel, count))
    return jsonify({"items": items})


@config_api.post("/channels")
@require_tenant(ROLE_OWNER)
def create_channel():
    """Register a number for this tenant.

    Uniqueness on (kind, address) is what guarantees an inbound number can
    only ever resolve to one tenant.
    """
    payload = request.get_json(silent=True) or {}
    kind = payload.get("kind")
    address = normalize_address(payload.get("address"))
    if kind not in ("whatsapp", "voice", "sms") or not address:
        return jsonify({"error": "A valid kind and address are required"}), 400

    existing = Channel.query.filter_by(kind=kind, address=address).one_or_none()
    if existing is not None:
        return jsonify(
            {"error": "That number is already registered on the platform"}
        ), 409

    channel = Channel(
        tenant_id=g.tenant.id,
        kind=kind,
        address=address,
        display_name=payload.get("display_name"),
    )
    db.session.add(channel)
    record_audit("channel.create", "channel", channel.id, kind=kind, address=address)
    db.session.commit()
    return jsonify(_channel_payload(channel, 0)), 201


# ---------------------------------------------------------------------------
# Automations
# ---------------------------------------------------------------------------


@config_api.get("/automations")
@require_tenant()
def list_automations():
    existing = {a.kind: a.to_dict() for a in tenant_query(Automation).all()}
    return jsonify(
        {
            "items": [
                existing.get(
                    kind,
                    {
                        "kind": kind,
                        "name": kind.replace("_", " ").title(),
                        "status": "pending",
                        "is_implemented": False,
                    },
                )
                for kind in AUTOMATION_KINDS
            ]
        }
    )


@config_api.patch("/automations/<kind>")
@require_tenant(ROLE_MANAGER)
def update_automation(kind: str):
    if kind not in AUTOMATION_KINDS:
        return jsonify({"error": "Unknown automation"}), 404
    automation = (
        tenant_query(Automation).filter(Automation.kind == kind).one_or_none()
    )
    if automation is None:
        automation = Automation(
            tenant_id=g.tenant.id, kind=kind, name=kind.replace("_", " ").title()
        )
        db.session.add(automation)

    payload = request.get_json(silent=True) or {}
    if "status" in payload:
        if payload["status"] not in ("pending", "enabled", "disabled"):
            return jsonify({"error": "Invalid status"}), 400
        # Delivery is not wired yet in this phase; refuse to claim otherwise.
        if payload["status"] == "enabled" and not automation.is_implemented:
            return jsonify(
                {"error": "This automation is not yet available to enable"}
            ), 409
        automation.status = payload["status"]
    if isinstance(payload.get("trigger_config"), dict):
        automation.trigger_config = payload["trigger_config"]
    if isinstance(payload.get("action_config"), dict):
        automation.action_config = payload["action_config"]

    record_audit("automation.update", "automation", automation.id, kind=kind)
    db.session.commit()
    return jsonify(automation.to_dict())
