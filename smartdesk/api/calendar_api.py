"""Per-tenant Google Calendar connection endpoints.

``/calendar/oauth/callback`` is intentionally outside ``/api/v1`` and outside
``require_tenant``: Google redirects the browser here directly, with no
Authorization header and no way to attach one. Trust instead comes entirely
from the signed, expiring ``state`` parameter minted in ``/connect`` — that
is what binds this callback to the tenant and user who started the flow.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify, redirect, request

from smartdesk.extensions import db
from smartdesk.models import ROLE_OWNER, Tenant
from smartdesk.security.rbac import record_audit, require_tenant
from smartdesk.services import calendar as calendar_service

calendar_api = Blueprint("calendar_api", __name__)
calendar_oauth = Blueprint("calendar_oauth", __name__)


@calendar_api.get("/calendar/status")
@require_tenant()
def calendar_status():
    connection = calendar_service.get_connection(g.tenant.id)
    return jsonify(
        {
            "configured_on_server": bool(
                g.tenant and _server_has_google_config()
            ),
            "connection": connection.to_dict() if connection else None,
        }
    )


def _server_has_google_config() -> bool:
    from flask import current_app

    return bool(
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
        and current_app.config.get("GOOGLE_OAUTH_REDIRECT_URI")
    )


@calendar_api.get("/calendar/connect")
@require_tenant(ROLE_OWNER)
def calendar_connect():
    try:
        url = calendar_service.build_authorize_url(g.tenant.id, g.principal.user.id)
    except calendar_service.CalendarError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"authorize_url": url})


@calendar_api.post("/calendar/disconnect")
@require_tenant(ROLE_OWNER)
def calendar_disconnect():
    calendar_service.disconnect(g.tenant.id)
    record_audit("calendar.disconnect", "tenant", g.tenant.id)
    db.session.commit()
    return jsonify({"status": "disconnected"})


@calendar_oauth.get("/calendar/oauth/callback")
def calendar_oauth_callback():
    error = request.args.get("error")
    code = request.args.get("code")
    state = request.args.get("state", "")

    dashboard_origins = None
    from flask import current_app

    origins = current_app.config.get("DASHBOARD_ORIGINS") or ()
    redirect_base = origins[0] if origins else ""

    if error:
        return redirect(f"{redirect_base}/settings?calendar=denied")

    if not code or not state:
        return redirect(f"{redirect_base}/settings?calendar=error")

    try:
        connection = calendar_service.complete_oauth_callback(code, state)
    except calendar_service.InvalidOAuthState:
        return redirect(f"{redirect_base}/settings?calendar=expired")
    except calendar_service.CalendarError:
        return redirect(f"{redirect_base}/settings?calendar=error")

    return redirect(f"{redirect_base}/settings?calendar=connected")
