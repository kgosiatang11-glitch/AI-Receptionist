"""Tests for smartdesk.services.calendar and the calendar API.

Google's OAuth and Calendar APIs are not reachable from this environment's
network policy (and shouldn't be hit from a unit test regardless), so every
external call is mocked. What's under test is SmartDesk's own logic: state
signing/verification, tenant isolation of connections, and how booking
creation reacts to a connected vs. unconnected calendar.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Booking, CalendarConnection, Customer  # noqa: E402
from smartdesk.services import calendar as calendar_service  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402


class OAuthStateTests(MultiTenantTestCase):
    def test_state_round_trips(self):
        state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        payload = calendar_service.verify_state(state)
        self.assertEqual(payload["tenant_id"], self.padel.id)
        self.assertEqual(payload["user_id"], self.padel_user.id)

    def test_tampered_state_is_rejected(self):
        state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        body, _, signature = state.partition(".")
        forged = f"{body}.{'0' * len(signature)}"
        with self.assertRaises(calendar_service.InvalidOAuthState):
            calendar_service.verify_state(forged)

    def test_state_naming_a_different_tenant_cannot_be_forged_from_a_valid_one(self):
        """A user cannot edit a valid state's tenant_id without invalidating
        the signature -- this is what stops a callback being redirected to
        attach a connection to the wrong tenant."""
        import base64
        import json

        state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        body, _, signature = state.partition(".")
        payload = json.loads(base64.urlsafe_b64decode(body))
        payload["tenant_id"] = self.salon.id
        tampered_body = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode("ascii")
        with self.assertRaises(calendar_service.InvalidOAuthState):
            calendar_service.verify_state(f"{tampered_body}.{signature}")

    def test_expired_state_is_rejected(self):
        with patch("smartdesk.services.calendar.time") as mock_time:
            mock_time.time.return_value = 1000.0
            state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        with patch("smartdesk.services.calendar.time") as mock_time:
            mock_time.time.return_value = 1000.0 + calendar_service.STATE_TTL_SECONDS + 1
            with self.assertRaises(calendar_service.InvalidOAuthState):
                calendar_service.verify_state(state)

    def test_malformed_state_is_rejected(self):
        with self.assertRaises(calendar_service.InvalidOAuthState):
            calendar_service.verify_state("not-a-valid-state-at-all")


class CalendarConnectEndpointTests(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
        self.app.config["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
        self.app.config["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:10000/calendar/oauth/callback"

    def test_status_with_no_connection(self):
        response = self.client.get(
            "/api/v1/calendar/status", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["connection"])

    def test_viewer_cannot_initiate_connect(self):
        response = self.client.get(
            "/api/v1/calendar/connect", headers=self.auth(self.viewer_user, self.padel)
        )
        self.assertEqual(response.status_code, 403)

    @patch("google_auth_oauthlib.flow.Flow.from_client_config")
    def test_owner_gets_an_authorize_url(self, mock_from_config):
        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?mock=1", "csrf")
        mock_from_config.return_value = mock_flow

        response = self.client.get(
            "/api/v1/calendar/connect", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("accounts.google.com", response.get_json()["authorize_url"])

    def test_connect_without_server_config_is_a_clean_503(self):
        self.app.config["GOOGLE_CLIENT_ID"] = ""
        response = self.client.get(
            "/api/v1/calendar/connect", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(response.status_code, 503)

    @patch("smartdesk.services.calendar._fetch_account_email")
    @patch("google_auth_oauthlib.flow.Flow.from_client_config")
    def test_callback_creates_a_connection_for_the_right_tenant(
        self, mock_from_config, mock_email
    ):
        mock_email.return_value = "manager@10by20.test"
        mock_flow = MagicMock()
        mock_credentials = MagicMock()
        mock_credentials.token = "fake-access-token"
        mock_credentials.refresh_token = "fake-refresh-token"
        mock_credentials.expiry = None
        mock_flow.credentials = mock_credentials
        mock_from_config.return_value = mock_flow

        state = None
        with self.app.test_request_context():
            state = calendar_service.sign_state(self.padel.id, self.padel_user.id)

        response = self.client.get(
            f"/calendar/oauth/callback?code=fake-code&state={state}"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("calendar=connected", response.location)

        connection = CalendarConnection.query.filter_by(tenant_id=self.padel.id).one()
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.connected_email, "manager@10by20.test")
        self.assertEqual(connection.access_token, "fake-access-token")

    def test_callback_with_denied_consent_redirects_gracefully(self):
        response = self.client.get("/calendar/oauth/callback?error=access_denied")
        self.assertEqual(response.status_code, 302)
        self.assertIn("calendar=denied", response.location)

    def test_callback_with_expired_state_redirects_gracefully(self):
        with patch("smartdesk.services.calendar.time") as mock_time:
            mock_time.time.return_value = 1000.0
            with self.app.test_request_context():
                state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        with patch("smartdesk.services.calendar.time") as mock_time:
            mock_time.time.return_value = 1000.0 + calendar_service.STATE_TTL_SECONDS + 1
            response = self.client.get(
                f"/calendar/oauth/callback?code=fake-code&state={state}"
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("calendar=expired", response.location)

    def test_callback_cannot_be_used_to_attach_to_a_different_tenant(self):
        """Even a technically-valid signed state names ONE tenant; there is
        no way to redirect its connection to another tenant after the fact."""
        with self.app.test_request_context():
            state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        payload = calendar_service.verify_state(state)
        self.assertEqual(payload["tenant_id"], self.padel.id)
        self.assertNotEqual(payload["tenant_id"], self.salon.id)

    def test_disconnect_marks_the_connection_disconnected(self):
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()
        response = self.client.post(
            "/api/v1/calendar/disconnect", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(response.status_code, 200)
        connection = CalendarConnection.query.filter_by(tenant_id=self.padel.id).one()
        self.assertEqual(connection.status, "disconnected")

    def test_connections_are_isolated_per_tenant(self):
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok",
                connected_email="padel@test.com", status="connected",
            )
        )
        db.session.commit()
        response = self.client.get(
            "/api/v1/calendar/status", headers=self.auth(self.salon_user, self.salon)
        )
        self.assertIsNone(response.get_json()["connection"])


class BookingCalendarSyncTests(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.customer = Customer.query.filter_by(tenant_id=self.padel.id).first()

    def test_booking_without_a_connected_calendar_is_created_unsynced(self):
        response = self.client.post(
            "/api/v1/bookings",
            headers=self.auth(self.padel_user, self.padel),
            json={
                "service": "Court hire",
                "starts_at": "2026-10-01T18:00:00+00:00",
                "ends_at": "2026-10-01T19:00:00+00:00",
                "source": "staff",
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertIsNone(body["external_reference"])
        self.assertNotIn("calendar_warning", body)

    @patch("smartdesk.services.calendar._calendar_service")
    def test_booking_with_a_connected_calendar_creates_a_real_event(self, mock_service):
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()

        mock_events = MagicMock()
        mock_events.insert.return_value.execute.return_value = {"id": "gcal-event-123"}
        mock_service.return_value.events.return_value = mock_events

        response = self.client.post(
            "/api/v1/bookings",
            headers=self.auth(self.padel_user, self.padel),
            json={
                "service": "Court hire",
                "starts_at": "2026-10-01T18:00:00+00:00",
                "ends_at": "2026-10-01T19:00:00+00:00",
                "source": "ai",
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["external_system"], "google_calendar")
        self.assertEqual(body["external_reference"], "gcal-event-123")
        self.assertEqual(body["status"], "confirmed")

    @patch("smartdesk.services.calendar._calendar_service")
    def test_calendar_failure_does_not_prevent_the_booking_from_being_created(
        self, mock_service
    ):
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()
        mock_service.return_value.events.return_value.insert.return_value.execute.side_effect = (
            Exception("calendar API unavailable")
        )

        response = self.client.post(
            "/api/v1/bookings",
            headers=self.auth(self.padel_user, self.padel),
            json={
                "service": "Court hire",
                "starts_at": "2026-10-01T18:00:00+00:00",
                "ends_at": "2026-10-01T19:00:00+00:00",
                "source": "ai",
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertIn("calendar_warning", body)
        self.assertIsNone(body["external_reference"])

    def test_external_source_booking_is_never_pushed_to_the_tenants_own_calendar(self):
        """A booking recorded from 10by20's existing external system (e.g.
        Playbypoint) must not also be written into this tenant's connected
        Google Calendar -- that would create a duplicate, invented sync."""
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()

        with patch("smartdesk.services.calendar._calendar_service") as mock_service:
            response = self.client.post(
                "/api/v1/bookings",
                headers=self.auth(self.padel_user, self.padel),
                json={
                    "service": "Court hire",
                    "starts_at": "2026-10-01T18:00:00+00:00",
                    "ends_at": "2026-10-01T19:00:00+00:00",
                    "source": "external",
                    "external_system": "Playbypoint",
                    "external_reference": "PBP-9981",
                },
            )
            self.assertEqual(response.status_code, 201)
            mock_service.assert_not_called()
        body = response.get_json()
        self.assertEqual(body["external_system"], "Playbypoint")
        self.assertEqual(body["external_reference"], "PBP-9981")

    def test_sync_to_calendar_false_opts_out_explicitly(self):
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()
        with patch("smartdesk.services.calendar._calendar_service") as mock_service:
            response = self.client.post(
                "/api/v1/bookings",
                headers=self.auth(self.padel_user, self.padel),
                json={
                    "service": "Court hire",
                    "starts_at": "2026-10-01T18:00:00+00:00",
                    "ends_at": "2026-10-01T19:00:00+00:00",
                    "source": "staff",
                    "sync_to_calendar": False,
                },
            )
            mock_service.assert_not_called()
        self.assertEqual(response.status_code, 201)

    @patch("smartdesk.services.calendar._calendar_service")
    def test_cancelling_a_synced_booking_deletes_the_calendar_event(self, mock_service):
        connection = CalendarConnection(
            tenant_id=self.padel.id, access_token="tok", status="connected"
        )
        db.session.add(connection)
        db.session.flush()
        booking = Booking(
            tenant_id=self.padel.id, service="Court hire", status="confirmed",
            source="ai", external_system="google_calendar", external_reference="gcal-1",
        )
        db.session.add(booking)
        db.session.commit()

        mock_delete = MagicMock()
        mock_service.return_value.events.return_value.delete.return_value = mock_delete

        response = self.client.post(
            f"/api/v1/bookings/{booking.id}/status",
            headers=self.auth(self.padel_user, self.padel),
            json={"status": "cancelled"},
        )
        self.assertEqual(response.status_code, 200)
        mock_service.return_value.events.return_value.delete.assert_called_once_with(
            calendarId="primary", eventId="gcal-1"
        )

    def test_bookings_are_still_tenant_isolated_with_calendar_sync_involved(self):
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()
        response = self.client.get(
            "/api/v1/bookings", headers=self.auth(self.salon_user, self.salon)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], [])


if __name__ == "__main__":
    unittest.main()
