"""Control Center data API.

Every route is wrapped in ``require_tenant``, which authenticates the caller,
validates the requested tenant against their memberships, enforces a minimum
role and binds the RLS session variable.  Every query then goes through
``tenant_query``.  A route cannot read cross-tenant data by omission.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request
from sqlalchemy import func

from smartdesk.extensions import db
from smartdesk.models import (
    ROLE_AGENT,
    ROLE_MANAGER,
    Booking,
    Conversation,
    Customer,
    Lead,
    Message,
    UsageEvent,
    utcnow,
)
from smartdesk.security.rbac import record_audit, require_tenant
from smartdesk.services import calendar as calendar_service
from smartdesk.services import conversations as conversation_service
from smartdesk.services.receptionist import OutboundSendError, send_whatsapp_message
from smartdesk.tenancy import tenant_query

dashboard_api = Blueprint("dashboard_api", __name__)

MAX_PAGE_SIZE = 100


def _page_args() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = min(MAX_PAGE_SIZE, max(1, int(request.args.get("page_size", 25))))
    except ValueError:
        size = 25
    return page, size


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@dashboard_api.get("/overview")
@require_tenant()
def overview():
    tenant = g.tenant
    day, month = _day_start(), _month_start()

    conversations_today = tenant_query(Conversation).filter(
        Conversation.created_at >= day
    ).count()
    conversations_month = tenant_query(Conversation).filter(
        Conversation.created_at >= month
    ).count()
    leads_captured = tenant_query(Lead).filter(Lead.created_at >= month).count()
    bookings_count = tenant_query(Booking).filter(
        Booking.starts_at.isnot(None), Booking.starts_at >= utcnow()
    ).count()
    handed_off = tenant_query(Conversation).filter(
        Conversation.status == "needs_human", Conversation.created_at >= month
    ).count()

    # AI response rate: share of customer messages this month that O'Brien
    # answered rather than escalating. Returns null (not a fake 100%) when
    # there is nothing to measure yet.
    inbound = tenant_query(Message).filter(
        Message.role == "customer", Message.created_at >= month
    ).count()
    outbound = tenant_query(Message).filter(
        Message.role == "assistant", Message.created_at >= month
    ).count()
    response_rate = round(min(outbound / inbound, 1.0) * 100, 1) if inbound else None

    recent_conversations = (
        tenant_query(Conversation)
        .order_by(Conversation.last_message_at.desc())
        .limit(6)
        .all()
    )
    recent_leads = (
        tenant_query(Lead).order_by(Lead.created_at.desc()).limit(6).all()
    )
    upcoming = (
        tenant_query(Booking)
        .filter(Booking.starts_at >= utcnow(), Booking.status != "cancelled")
        .order_by(Booking.starts_at.asc())
        .limit(6)
        .all()
    )

    from smartdesk.services.knowledge import get_profile
    from smartdesk.models import Channel

    profile = get_profile(tenant.id)
    channels = tenant_query(Channel).all()

    return jsonify(
        {
            "tenant": tenant.to_dict(),
            "metrics": {
                "conversations_today": conversations_today,
                "conversations_month": conversations_month,
                "leads_captured": leads_captured,
                "bookings": bookings_count,
                "handed_off": handed_off,
                "ai_response_rate": response_rate,
            },
            "recent_conversations": [c.to_dict() for c in recent_conversations],
            "recent_leads": [lead.to_dict() for lead in recent_leads],
            "upcoming_bookings": [b.to_dict() for b in upcoming],
            "receptionist": {
                "name": profile.display_name if profile else "O'Brien",
                "online": bool(profile and profile.is_active),
            },
            "channels": {
                kind: any(c.kind == kind and c.is_active for c in channels)
                for kind in ("whatsapp", "voice")
            },
            "ai_engine_operational": True,
        }
    )


# ---------------------------------------------------------------------------
# Conversations (inbox)
# ---------------------------------------------------------------------------

_CONVERSATION_FILTERS = {
    "all": lambda q: q,
    "active": lambda q: q.filter(Conversation.status == "active"),
    "unread": lambda q: q.filter(Conversation.is_unread.is_(True)),
    "needs_human": lambda q: q.filter(Conversation.status == "needs_human"),
    "closed": lambda q: q.filter(Conversation.status == "closed"),
}


@dashboard_api.get("/conversations")
@require_tenant()
def list_conversations():
    page, size = _page_args()
    key = request.args.get("filter", "all")
    query = tenant_query(Conversation)

    if key in ("leads", "booking"):
        model = Lead if key == "leads" else Booking
        ids = [
            row.conversation_id
            for row in tenant_query(model).filter(model.conversation_id.isnot(None))
        ]
        query = query.filter(Conversation.id.in_(ids or [""]))
    else:
        query = _CONVERSATION_FILTERS.get(key, _CONVERSATION_FILTERS["all"])(query)

    search = (request.args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.outerjoin(Customer, Conversation.customer_id == Customer.id).filter(
            db.or_(
                Conversation.last_message_preview.ilike(like),
                Conversation.session_key.ilike(like),
                Customer.full_name.ilike(like),
                Customer.phone.ilike(like),
            )
        )

    total = query.count()
    rows = (
        query.order_by(Conversation.last_message_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return jsonify(
        {"items": [c.to_dict() for c in rows], "total": total, "page": page}
    )


@dashboard_api.get("/conversations/<conversation_id>")
@require_tenant()
def get_conversation(conversation_id: str):
    conversation = tenant_query(Conversation).filter(
        Conversation.id == conversation_id
    ).one_or_none()
    if conversation is None:
        return jsonify({"error": "Conversation not found"}), 404

    messages = (
        tenant_query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    if conversation.is_unread:
        conversation.is_unread = False
        db.session.commit()

    return jsonify(
        {
            "conversation": conversation.to_dict(),
            "messages": [m.to_dict() for m in messages],
        }
    )


@dashboard_api.post("/conversations/<conversation_id>/takeover")
@require_tenant(ROLE_AGENT)
def takeover(conversation_id: str):
    """Human takeover. O'Brien stops replying while this is set."""
    conversation = tenant_query(Conversation).filter(
        Conversation.id == conversation_id
    ).one_or_none()
    if conversation is None:
        return jsonify({"error": "Conversation not found"}), 404

    enabled = bool((request.get_json(silent=True) or {}).get("enabled", True))
    conversation.human_takeover = enabled
    conversation.assigned_user_id = g.principal.user.id if enabled else None
    conversation_service.record_event(
        conversation,
        "takeover",
        f"{g.principal.user.email} {'took over' if enabled else 'released'} this conversation",
    )
    record_audit(
        "conversation.takeover", "conversation", conversation.id, enabled=enabled
    )
    db.session.commit()
    return jsonify(conversation.to_dict())


@dashboard_api.post("/conversations/<conversation_id>/reply")
@require_tenant(ROLE_AGENT)
def send_reply(conversation_id: str):
    """Send a staff reply into a conversation that a human has taken over.

    Requires an active takeover so a staff message can never arrive in the
    same conversation O'Brien is still answering, and only WhatsApp
    conversations can be replied to this way — there is no outbound message
    primitive for an ended voice call.
    """
    conversation = tenant_query(Conversation).filter(
        Conversation.id == conversation_id
    ).one_or_none()
    if conversation is None:
        return jsonify({"error": "Conversation not found"}), 404
    if not conversation.human_takeover:
        return jsonify(
            {"error": "Take over this conversation before replying as staff"}
        ), 409
    if conversation.channel_kind != "whatsapp":
        return jsonify(
            {"error": "Staff replies are only available for WhatsApp conversations"}
        ), 400

    body = ((request.get_json(silent=True) or {}).get("body") or "").strip()
    if not body:
        return jsonify({"error": "Message body is required"}), 400
    if len(body) > 4096:
        return jsonify({"error": "Message is too long"}), 400

    from smartdesk.models import Channel

    channel = db.session.get(Channel, conversation.channel_id) if conversation.channel_id else None
    if channel is None:
        return jsonify({"error": "This conversation has no WhatsApp channel on file"}), 409

    customer = conversation.customer
    if not customer or not customer.phone:
        return jsonify({"error": "This conversation has no customer number on file"}), 409

    try:
        provider_id = send_whatsapp_message(channel, customer.phone, body)
    except OutboundSendError as exc:
        return jsonify({"error": f"Message could not be sent: {exc}"}), 502

    conversation_service.append_message(
        conversation, "human_agent", body, provider_message_id=provider_id
    )
    record_audit("conversation.staff_reply", "conversation", conversation.id)
    db.session.commit()
    return jsonify(conversation.to_dict()), 201


@dashboard_api.post("/conversations/<conversation_id>/status")
@require_tenant(ROLE_AGENT)
def set_status(conversation_id: str):
    conversation = tenant_query(Conversation).filter(
        Conversation.id == conversation_id
    ).one_or_none()
    if conversation is None:
        return jsonify({"error": "Conversation not found"}), 404
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ("active", "needs_human", "closed"):
        return jsonify({"error": "Invalid status"}), 400
    conversation.status = status
    db.session.commit()
    return jsonify(conversation.to_dict())


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@dashboard_api.get("/customers")
@require_tenant()
def list_customers():
    page, size = _page_args()
    query = tenant_query(Customer)
    search = (request.args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(
                Customer.full_name.ilike(like),
                Customer.phone.ilike(like),
                Customer.email.ilike(like),
            )
        )
    total = query.count()
    rows = (
        query.order_by(Customer.last_contact_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )

    counts = dict(
        db.session.query(Conversation.customer_id, func.count(Conversation.id))
        .filter(Conversation.tenant_id == g.tenant.id)
        .group_by(Conversation.customer_id)
        .all()
    )
    items = []
    for customer in rows:
        payload = customer.to_dict()
        payload["conversation_count"] = counts.get(customer.id, 0)
        items.append(payload)
    return jsonify({"items": items, "total": total, "page": page})


@dashboard_api.get("/customers/<customer_id>")
@require_tenant()
def get_customer(customer_id: str):
    customer = tenant_query(Customer).filter(Customer.id == customer_id).one_or_none()
    if customer is None:
        return jsonify({"error": "Customer not found"}), 404
    return jsonify(
        {
            "customer": customer.to_dict(),
            "conversations": [
                c.to_dict(include_customer=False)
                for c in tenant_query(Conversation)
                .filter(Conversation.customer_id == customer.id)
                .order_by(Conversation.last_message_at.desc())
                .all()
            ],
            "leads": [
                lead.to_dict()
                for lead in tenant_query(Lead).filter(Lead.customer_id == customer.id)
            ],
            "bookings": [
                b.to_dict()
                for b in tenant_query(Booking).filter(Booking.customer_id == customer.id)
            ],
        }
    )


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@dashboard_api.get("/leads")
@require_tenant()
def list_leads():
    page, size = _page_args()
    query = tenant_query(Lead)
    status = request.args.get("status")
    if status:
        query = query.filter(Lead.status == status)
    total = query.count()
    rows = (
        query.order_by(Lead.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return jsonify({"items": [l.to_dict() for l in rows], "total": total, "page": page})


@dashboard_api.patch("/leads/<lead_id>")
@require_tenant(ROLE_AGENT)
def update_lead(lead_id: str):
    lead = tenant_query(Lead).filter(Lead.id == lead_id).one_or_none()
    if lead is None:
        return jsonify({"error": "Lead not found"}), 404
    payload = request.get_json(silent=True) or {}
    if "status" in payload:
        if payload["status"] not in (
            "new", "contacted", "qualified", "converted", "lost"
        ):
            return jsonify({"error": "Invalid status"}), 400
        lead.status = payload["status"]
    if "interest" in payload:
        lead.interest = payload["interest"]
    if "assigned_user_id" in payload:
        lead.assigned_user_id = payload["assigned_user_id"] or None
    record_audit("lead.update", "lead", lead.id)
    db.session.commit()
    return jsonify(lead.to_dict())


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


@dashboard_api.get("/bookings")
@require_tenant()
def list_bookings():
    scope = request.args.get("scope", "upcoming")
    query = tenant_query(Booking)
    now = utcnow()
    if scope == "upcoming":
        query = query.filter(
            Booking.starts_at >= now, Booking.status != "cancelled"
        ).order_by(Booking.starts_at.asc())
    elif scope == "past":
        query = query.filter(Booking.starts_at < now).order_by(
            Booking.starts_at.desc()
        )
    elif scope == "cancelled":
        query = query.filter(Booking.status == "cancelled").order_by(
            Booking.starts_at.desc()
        )
    else:
        query = query.order_by(Booking.starts_at.desc())
    return jsonify({"items": [b.to_dict() for b in query.limit(MAX_PAGE_SIZE).all()]})


@dashboard_api.post("/bookings")
@require_tenant(ROLE_AGENT)
def create_booking():
    payload = request.get_json(silent=True) or {}
    source = payload.get("source", "staff")
    if source not in ("ai", "staff", "external"):
        return jsonify({"error": "Invalid booking source"}), 400
    booking = Booking(
        tenant_id=g.tenant.id,
        customer_id=payload.get("customer_id") or None,
        service=payload.get("service"),
        status=payload.get("status", "pending"),
        source=source,
        external_system=payload.get("external_system"),
        external_reference=payload.get("external_reference"),
        notes=payload.get("notes"),
        is_test_data=g.tenant.is_test_data,
    )
    starts_at = payload.get("starts_at")
    ends_at = payload.get("ends_at")
    if starts_at:
        try:
            parsed = datetime.fromisoformat(starts_at)
        except ValueError:
            return jsonify({"error": "starts_at must be ISO-8601"}), 400
        if parsed.tzinfo is None:
            return jsonify(
                {"error": "starts_at must include a UTC offset, e.g. +02:00 or Z"}
            ), 400
        # Always store UTC explicitly, matching the booking engine's own
        # write discipline -- see the comment in booking_engine.py's
        # _finalize_booking for why this matters even beyond tidiness.
        booking.starts_at = parsed.astimezone(timezone.utc)
    if ends_at:
        try:
            parsed = datetime.fromisoformat(ends_at)
        except ValueError:
            return jsonify({"error": "ends_at must be ISO-8601"}), 400
        if parsed.tzinfo is None:
            return jsonify(
                {"error": "ends_at must include a UTC offset, e.g. +02:00 or Z"}
            ), 400
        booking.ends_at = parsed.astimezone(timezone.utc)

    db.session.add(booking)
    db.session.flush()  # booking.id is needed before we can sync it

    calendar_warning = None
    # Only attempt a sync when the caller actually wants one -- a booking
    # against an external system (source='external', e.g. 10by20's existing
    # Playbypoint) must never be pushed into this tenant's own Google
    # Calendar as a side effect.
    if (
        source in ("ai", "staff")
        and booking.starts_at
        and booking.ends_at
        and payload.get("sync_to_calendar", True)
    ):
        connection = calendar_service.get_connection(g.tenant.id)
        if connection is not None:
            try:
                calendar_service.create_event_for_booking(g.tenant, booking)
            except calendar_service.CalendarError as exc:
                # The booking still exists -- it's just not on the calendar
                # yet. Say so plainly rather than reporting false success.
                calendar_warning = str(exc)

    record_audit("booking.create", "booking", booking.id)
    db.session.commit()
    response = booking.to_dict()
    if calendar_warning:
        response["calendar_warning"] = calendar_warning
    return jsonify(response), 201


@dashboard_api.post("/bookings/<booking_id>/status")
@require_tenant(ROLE_AGENT)
def update_booking_status(booking_id: str):
    booking = tenant_query(Booking).filter(Booking.id == booking_id).one_or_none()
    if booking is None:
        return jsonify({"error": "Booking not found"}), 404
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ("pending", "confirmed", "cancelled", "completed"):
        return jsonify({"error": "Invalid status"}), 400

    booking.status = status
    if status == "cancelled":
        # Best-effort: the booking record is authoritative even if the
        # calendar delete fails, so this never blocks the status change.
        calendar_service.cancel_event_for_booking(g.tenant, booking)

    record_audit("booking.status", "booking", booking.id, status=status)
    db.session.commit()
    return jsonify(booking.to_dict())


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@dashboard_api.get("/analytics")
@require_tenant()
def analytics():
    """Aggregates from real rows only.

    When a tenant has no data the series come back empty and the frontend
    renders an empty state. No synthetic performance figures are produced.
    """
    try:
        days = min(90, max(7, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    since = utcnow() - timedelta(days=days)

    def series(model, date_column, extra=None):
        query = db.session.query(
            func.date_trunc("day", date_column).label("day"),
            func.count(model.id),
        ).filter(model.tenant_id == g.tenant.id, date_column >= since)
        if extra is not None:
            query = query.filter(extra)
        rows = query.group_by("day").order_by("day").all()
        return [
            {"date": day.date().isoformat() if day else None, "value": count}
            for day, count in rows
        ]

    ai_messages = tenant_query(Message).filter(
        Message.role == "assistant", Message.created_at >= since
    ).count()
    human_messages = tenant_query(Message).filter(
        Message.role == "human_agent", Message.created_at >= since
    ).count()

    return jsonify(
        {
            "range_days": days,
            "conversations": series(Conversation, Conversation.created_at),
            "calls": series(
                Conversation,
                Conversation.created_at,
                Conversation.channel_kind == "voice",
            ),
            "leads": series(Lead, Lead.created_at),
            "bookings": series(Booking, Booking.created_at),
            "messages": series(Message, Message.created_at),
            "ai_vs_human": {"ai": ai_messages, "human": human_messages},
            "usage": series(UsageEvent, UsageEvent.occurred_at),
            "has_data": bool(ai_messages or human_messages),
        }
    )
