"""Per-tenant Google Calendar integration.

Design constraints from the original spec, carried through here:

* one Google account connection per tenant, never a platform-wide account —
  each business authorizes its own calendar;
* Calendar is the source of truth for bookings, so a booking made through
  O'Brien or staff always tries to write a real event, and a booking read
  back into the dashboard should reflect what the calendar actually has;
* nothing here invents an API for a business's *existing* external booking
  system (e.g. 10by20's Playbypoint) — this module only talks to Google
  Calendar, and only for a tenant that has explicitly connected one.

The OAuth "state" parameter is an HMAC-signed, expiring token binding the
callback to the tenant and user that started the flow — Google's redirect
carries no auth of its own, so without this a callback could be replayed
against the wrong tenant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy.exc import IntegrityError

from smartdesk.extensions import db
from smartdesk.models import (
    ROLE_OWNER,
    Booking,
    CalendarConnection,
    Membership,
    Tenant,
    User,
)
from smartdesk.tenancy import tenant_access_denial, tenant_is_active_now

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.readonly"]

STATE_TTL_SECONDS = 600  # 10 minutes to complete the Google consent screen.


class CalendarError(Exception):
    """Raised for any calendar operation that could not be completed."""


class InvalidOAuthState(CalendarError):
    pass


# ---------------------------------------------------------------------------
# OAuth state signing
# ---------------------------------------------------------------------------


def _state_secret() -> bytes:
    secret = (
        current_app.config.get("OAUTH_STATE_SECRET")
        or current_app.config.get("SUPABASE_JWT_SECRET")
        or ""
    )
    if not secret:
        raise CalendarError("No OAUTH_STATE_SECRET or SUPABASE_JWT_SECRET configured")
    return secret.encode("utf-8")


#: Key under ``Tenant.settings`` holding the single pending OAuth flow.
_PENDING_KEY = "calendar_oauth_pending"


def sign_state(tenant_id: str, user_id: str) -> str:
    """Mint the signed OAuth ``state`` for a flow started by ``user_id``.

    C6: the state also carries a random nonce, and the SAME nonce is stored
    server-side as the tenant's one pending flow.  The callback consumes it, so
    a captured state cannot be replayed, and starting a new flow supersedes any
    earlier one.  (No new table: it lives in ``Tenant.settings``.)
    """
    nonce = secrets.token_urlsafe(16)
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is not None:
        settings = dict(tenant.settings or {})
        settings[_PENDING_KEY] = {"nonce": nonce, "user_id": user_id}
        tenant.settings = settings
        db.session.commit()

    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "nonce": nonce,
            "issued_at": time.time(),
        }
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii")
    signature = hmac.new(_state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_state(state: str) -> dict:
    try:
        body, signature = state.split(".", 1)
    except ValueError as exc:
        raise InvalidOAuthState("Malformed state parameter") from exc

    expected = hmac.new(_state_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidOAuthState("State signature does not match")

    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidOAuthState("Could not decode state payload") from exc

    if time.time() - payload.get("issued_at", 0) > STATE_TTL_SECONDS:
        raise InvalidOAuthState("This connection link has expired; please try again")

    return payload


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def _client_config() -> dict:
    return {
        "web": {
            "client_id": current_app.config["GOOGLE_CLIENT_ID"],
            "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [current_app.config["GOOGLE_OAUTH_REDIRECT_URI"]],
        }
    }


def build_authorize_url(tenant_id: str, user_id: str) -> str:
    if not (
        current_app.config.get("GOOGLE_CLIENT_ID")
        and current_app.config.get("GOOGLE_CLIENT_SECRET")
        and current_app.config.get("GOOGLE_OAUTH_REDIRECT_URI")
    ):
        raise CalendarError("Google Calendar is not configured on this server")

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=current_app.config["GOOGLE_OAUTH_REDIRECT_URI"],
    )
    state = sign_state(tenant_id, user_id)
    authorize_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # forces a refresh_token on every (re)connect
        state=state,
    )
    return authorize_url


def _consume_pending_flow(tenant: Tenant, payload: dict) -> None:
    """Atomically take the tenant's pending flow, or raise ``InvalidOAuthState``.

    The tenant row is locked (``FOR NO KEY UPDATE``) so two simultaneous
    callbacks carrying the same state cannot both succeed.  The nonce is spent
    (and committed) BEFORE the code exchange: a failed exchange needs a fresh
    "Connect" click rather than leaving a reusable state behind.
    """
    db.session.execute(
        db.select(Tenant.id).where(Tenant.id == tenant.id).with_for_update(key_share=True)
    ).first()
    db.session.refresh(tenant)
    settings = dict(tenant.settings or {})
    pending = settings.get(_PENDING_KEY) or {}
    nonce = str(payload.get("nonce") or "")
    expected = str(pending.get("nonce") or "")
    if (
        not nonce
        or not expected
        or not hmac.compare_digest(nonce, expected)
        or pending.get("user_id") != payload.get("user_id")
    ):
        db.session.rollback()
        raise InvalidOAuthState("This connection link was already used or replaced")
    settings.pop(_PENDING_KEY, None)
    tenant.settings = settings
    db.session.commit()


def _revalidate_initiator(tenant_id: str, user_id) -> None:
    """The person who started the flow must STILL be allowed to finish it.

    The state is valid for ten minutes; in that window the user may have been
    removed from the tenant, demoted, deactivated or deleted.  Connecting a
    calendar needs an active owner of the tenant (as ``/calendar/connect``
    does) -- or an active platform administrator doing support work.
    """
    if not isinstance(user_id, str) or not user_id:
        raise CalendarError("This connection link is no longer valid")
    user = db.session.get(User, user_id)
    if user is None or not user.is_active:
        raise CalendarError("This connection link is no longer valid")
    if user.is_platform_admin:
        return
    membership = Membership.query.filter_by(
        tenant_id=tenant_id, user_id=user.id
    ).one_or_none()
    if membership is None or membership.role != ROLE_OWNER:
        raise CalendarError("This connection link is no longer valid")


def complete_oauth_callback(code: str, state: str) -> CalendarConnection:
    """Exchange the authorization code and persist the tenant's connection."""
    payload = verify_state(state)  # raises InvalidOAuthState if tampered/expired
    tenant_id = payload["tenant_id"]
    user_id = payload["user_id"]

    # This route is reached by Google's browser redirect, not through
    # ``require_tenant``, so it needs its own (same-policy) suspension check:
    # a tenant suspended after starting the flow must not gain a calendar
    # connection.  Nothing is exchanged or stored.
    tenant = db.session.get(Tenant, tenant_id)
    if tenant_access_denial(tenant) is not None:
        raise CalendarError("This business account is not active")

    # C6: single use, then re-check that the initiator is still entitled.
    _consume_pending_flow(tenant, payload)
    _revalidate_initiator(tenant_id, user_id)

    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=current_app.config["GOOGLE_OAUTH_REDIRECT_URI"],
    )
    flow.fetch_token(code=code)
    credentials = flow.credentials

    email = _fetch_account_email(credentials)

    connection = CalendarConnection.query.filter_by(tenant_id=tenant_id).one_or_none()
    if connection is None:
        connection = CalendarConnection(tenant_id=tenant_id)
        db.session.add(connection)

    connection.status = "connected"
    connection.access_token = credentials.token
    if credentials.refresh_token:
        # Google only returns a refresh_token on first consent (or when
        # prompt=consent forces re-issue, as above); keep the existing one
        # if this exchange didn't return a new one.
        connection.refresh_token = credentials.refresh_token
    connection.token_expiry = (
        credentials.expiry.replace(tzinfo=timezone.utc) if credentials.expiry else None
    )
    connection.connected_email = email
    connection.connected_by_user_id = user_id
    connection.last_sync_error = None
    try:
        db.session.commit()
    except IntegrityError as exc:  # e.g. the user was deleted mid-flow
        db.session.rollback()
        raise CalendarError("Could not save the calendar connection") from exc
    return connection


def _fetch_account_email(credentials) -> str | None:
    try:
        from googleapiclient.discovery import build

        oauth2_service = build("oauth2", "v2", credentials=credentials)
        info = oauth2_service.userinfo().get().execute()
        return info.get("email")
    except Exception:  # pragma: no cover - best-effort only
        logger.warning("Could not fetch connected Google account email", exc_info=True)
        return None


def disconnect(tenant_id: str) -> None:
    connection = CalendarConnection.query.filter_by(tenant_id=tenant_id).one_or_none()
    if connection is not None:
        connection.status = "disconnected"
        db.session.commit()


# ---------------------------------------------------------------------------
# Using the connection
# ---------------------------------------------------------------------------


def _credentials_for(connection: CalendarConnection):
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials

    credentials = Credentials(
        token=connection.access_token,
        refresh_token=connection.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=current_app.config["GOOGLE_CLIENT_ID"],
        client_secret=current_app.config["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleRequest())
        connection.access_token = credentials.token
        connection.token_expiry = (
            credentials.expiry.replace(tzinfo=timezone.utc) if credentials.expiry else None
        )
        db.session.commit()
    return credentials


def _calendar_service(connection: CalendarConnection):
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_credentials_for(connection))


def get_connection(tenant_id: str) -> CalendarConnection | None:
    return CalendarConnection.query.filter_by(
        tenant_id=tenant_id, status="connected"
    ).one_or_none()


@dataclass(frozen=True)
class TimeSlot:
    start: datetime
    end: datetime


def check_availability(tenant: Tenant, start: datetime, end: datetime) -> bool:
    """True if the tenant's calendar has no conflicting event in [start, end)."""
    connection = get_connection(tenant.id)
    if connection is None:
        raise CalendarError("This business has not connected a calendar yet")

    service = _calendar_service(connection)
    try:
        response = (
            service.freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": [{"id": connection.calendar_id}],
                }
            )
            .execute()
        )
    except Exception as exc:
        connection.last_sync_error = str(exc)[:500]
        db.session.commit()
        raise CalendarError(f"Could not check calendar availability: {exc}") from exc

    busy = response.get("calendars", {}).get(connection.calendar_id, {}).get("busy", [])
    return len(busy) == 0


def create_event_for_booking(tenant: Tenant, booking: Booking) -> str:
    """Create the calendar event for a booking and store its event id.

    Raises CalendarError rather than silently leaving the booking
    unsynced, since a booking that looks confirmed in the dashboard but
    isn't on the actual calendar is worse than an explicit failure.
    """
    if not tenant_is_active_now(tenant.id):  # C6: re-check right before the external write
        raise CalendarError("This business account is not active")
    connection = get_connection(tenant.id)
    if connection is None:
        raise CalendarError("This business has not connected a calendar yet")
    if not booking.starts_at or not booking.ends_at:
        raise CalendarError("Booking is missing a start or end time")

    service = _calendar_service(connection)
    body = {
        "summary": f"{booking.service or 'Booking'} — {tenant.name}",
        "start": {"dateTime": booking.starts_at.isoformat()},
        "end": {"dateTime": booking.ends_at.isoformat()},
    }
    if booking.customer and booking.customer.full_name:
        body["description"] = f"Customer: {booking.customer.full_name}"
        if booking.customer.phone:
            body["description"] += f" ({booking.customer.phone})"

    try:
        event = (
            service.events()
            .insert(calendarId=connection.calendar_id, body=body)
            .execute()
        )
    except Exception as exc:
        connection.last_sync_error = str(exc)[:500]
        db.session.commit()
        raise CalendarError(f"Could not create calendar event: {exc}") from exc

    booking.external_system = "google_calendar"
    booking.external_reference = event["id"]
    booking.status = "confirmed"
    db.session.commit()
    return event["id"]


def cancel_event_for_booking(tenant: Tenant, booking: Booking) -> None:
    if not tenant_is_active_now(tenant.id):  # C6: no external calendar write when inactive
        return
    connection = get_connection(tenant.id)
    if connection is None or booking.external_system != "google_calendar":
        return
    service = _calendar_service(connection)
    try:
        service.events().delete(
            calendarId=connection.calendar_id, eventId=booking.external_reference
        ).execute()
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning("Could not delete calendar event for booking %s: %s", booking.id, exc)
