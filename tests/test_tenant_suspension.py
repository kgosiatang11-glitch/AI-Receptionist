"""Phase 3.4 (audit finding C6): tenant suspension is enforced centrally.

Before this phase suspension was honoured only on inbound Twilio traffic
(``tenancy.resolve_channel``); every dashboard/API route behind
``require_tenant`` kept working for a suspended tenant's own users.  Now
``require_tenant`` applies the one status policy in ``tenancy`` to every
tenant-scoped route, reads and writes alike, while platform administrators
keep full access.

These are SQLite behaviour tests.  The properties that depend on real
PostgreSQL (RLS binding, fresh status reads under READ COMMITTED, a suspension
committed on another connection) live in ``test_tenant_suspension_postgres.py``.

Note on AI: no tenant dashboard route calls OpenAI.  The only OpenAI entry
points are the WhatsApp (and voice) webhooks, so "a suspended tenant cannot
invoke AI" is proven there, by counting the fake client's calls.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Booking,
    CalendarConnection,
    Channel,
    Conversation,
    Customer,
    Lead,
    Membership,
    Message,
    Tenant,
    UsageEvent,
    UsageReservation,
)
from smartdesk.services import calendar as calendar_service  # noqa: E402
from smartdesk.services import receptionist as receptionist_service  # noqa: E402
from smartdesk.tenancy import (  # noqa: E402
    TENANT_SUSPENDED,
    TENANT_UNAVAILABLE,
    tenant_access_denial,
    tenant_is_active_now,
)
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402
from tests.test_whatsapp_idempotency import (  # noqa: E402
    AI_QUESTION,
    CUSTOMER,
    PADEL_NUMBER,
    SALON_NUMBER,
    WhatsAppWebhookTestCase,
    new_sid,
)

PADEL_VOICE_NUMBER = "+26770000003"
SUSPENDED_MESSAGE = "This business account is suspended"


def set_status(tenant: Tenant, status: str) -> None:
    tenant.status = status
    db.session.commit()


class SuspensionCase(MultiTenantTestCase):
    """10by20 (``self.padel``) is suspended; the salon stays active."""

    def setUp(self) -> None:
        super().setUp()
        set_status(self.padel, "suspended")

    def owner(self, tenant=None) -> dict:
        return self.auth(self.padel_user, tenant or self.padel)

    def assertSuspended(self, response, msg=None) -> None:
        self.assertEqual(response.status_code, 403, msg or response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["code"], TENANT_SUSPENDED, msg)
        self.assertEqual(body["error"], SUSPENDED_MESSAGE, msg)


# ---------------------------------------------------------------------------
# The policy function
# ---------------------------------------------------------------------------


class StatusPolicyTests(unittest.TestCase):
    def test_active_and_development_are_allowed(self):
        self.assertIsNone(tenant_access_denial(SimpleNamespace(status="active")))
        self.assertIsNone(tenant_access_denial(SimpleNamespace(status="development")))

    def test_suspended_is_denied_with_a_stable_code(self):
        self.assertEqual(
            tenant_access_denial(SimpleNamespace(status="suspended")), TENANT_SUSPENDED
        )

    def test_unknown_missing_or_null_status_fails_closed(self):
        for tenant in (None, SimpleNamespace(status=None), SimpleNamespace(status=""),
                       SimpleNamespace(status="cancelled"), SimpleNamespace()):
            with self.subTest(tenant=tenant):
                self.assertEqual(tenant_access_denial(tenant), TENANT_UNAVAILABLE)


# ---------------------------------------------------------------------------
# A, B, C, L, R: HTTP behaviour
# ---------------------------------------------------------------------------


class SuspendedApiAccessTests(SuspensionCase):
    def test_a_active_tenant_can_use_normal_endpoints(self):
        for path in ("/api/v1/overview", "/api/v1/conversations", "/api/v1/customers",
                     "/api/v1/leads", "/api/v1/bookings", "/api/v1/business"):
            with self.subTest(path=path):
                response = self.client.get(
                    path, headers=self.auth(self.salon_user, self.salon)
                )
                self.assertEqual(response.status_code, 200)

    def test_b_suspended_tenant_gets_403_with_a_stable_machine_code(self):
        response = self.client.get("/api/v1/conversations", headers=self.owner())
        self.assertSuspended(response)

    def test_b_error_body_leaks_no_tenant_internals(self):
        body = self.client.get("/api/v1/overview", headers=self.owner()).get_json()
        self.assertEqual(set(body), {"error", "code"})
        text = str(body)
        for secret in (self.padel.id, self.padel.name, self.padel.slug, "Padel secret"):
            self.assertNotIn(secret, text)

    def test_c_suspended_tenant_cannot_read_business_data(self):
        reads = [
            "/api/v1/overview",
            "/api/v1/conversations",
            f"/api/v1/conversations/{self.padel_conversation.id}",
            "/api/v1/customers",
            f"/api/v1/customers/{self.padel_customer.id}",
            "/api/v1/leads",
            "/api/v1/bookings",
            "/api/v1/analytics",
            "/api/v1/business",
            "/api/v1/knowledge",
            "/api/v1/receptionist",
            "/api/v1/channels/whatsapp",
            "/api/v1/automations",
            "/api/v1/calendar/status",
            "/api/v1/calendar/connect",
        ]
        for path in reads:
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.owner())
                self.assertSuspended(response, path)
                self.assertNotIn(b"Padel secret", response.get_data())

    def test_reads_are_blocked_not_just_writes(self):
        """The intended behaviour is NO normal access, not read-only access."""
        self.assertSuspended(self.client.get("/api/v1/leads", headers=self.owner()))
        self.assertSuspended(self.client.get("/api/v1/business", headers=self.owner()))

    def test_every_tenant_scoped_route_is_guarded(self):
        """Enumerate the real URL map: every route wrapped in ``require_tenant``
        must refuse a suspended tenant's owner, whatever its method.  A route
        added later without the decorator is not covered here -- but one WITH
        it can never forget the suspension check."""
        checked = 0
        for rule in self.app.url_map.iter_rules():
            view = self.app.view_functions[rule.endpoint]
            if not getattr(view, "requires_tenant", False):
                continue
            path = rule.rule.replace("<conversation_id>", "x")
            for converter in rule.arguments:
                path = path.replace(f"<{converter}>", "x")
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                with self.subTest(method=method, rule=rule.rule):
                    response = self.client.open(
                        path, method=method, headers=self.owner(), json={}
                    )
                    self.assertSuspended(response, f"{method} {rule.rule}")
                    checked += 1
        self.assertGreaterEqual(checked, 25, "route enumeration looks incomplete")

    def test_l_active_tenant_is_unaffected_by_another_tenants_suspension(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.salon_user, self.salon)
        )
        self.assertEqual(response.status_code, 200)
        previews = [c["last_message_preview"] for c in response.get_json()["items"]]
        self.assertEqual(previews, ["Salon secret"])

    def test_development_tenants_are_still_allowed(self):
        set_status(self.padel, "development")
        response = self.client.get("/api/v1/overview", headers=self.owner())
        self.assertEqual(response.status_code, 200)

    def test_reactivated_tenant_recovers_immediately(self):
        set_status(self.padel, "active")
        response = self.client.get("/api/v1/overview", headers=self.owner())
        self.assertEqual(response.status_code, 200)

    def test_viewer_in_a_suspended_tenant_is_blocked_too(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.viewer_user, self.padel)
        )
        self.assertSuspended(response)

    def test_default_tenant_selection_is_also_guarded(self):
        """No X-Tenant-Id header: the caller's single tenant is chosen for
        them, and that path must not skip the check."""
        response = self.client.get("/api/v1/overview", headers=self.auth(self.padel_user))
        self.assertSuspended(response)

    def test_me_still_works_so_the_frontend_can_explain_the_suspension(self):
        """/me is identity + tenant switcher, not tenant application data."""
        response = self.client.get("/api/v1/me", headers=self.auth(self.padel_user))
        self.assertEqual(response.status_code, 200)
        statuses = {t["slug"]: t["status"] for t in response.get_json()["tenants"]}
        self.assertEqual(statuses, {"10by20": "suspended"})

    def test_r_unauthenticated_behaviour_is_unchanged(self):
        response = self.client.get(
            "/api/v1/conversations", headers={"X-Tenant-Id": self.padel.id}
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("code", response.get_json())
        response = self.client.get(
            "/api/v1/conversations",
            headers={"Authorization": "Bearer not-a-token", "X-Tenant-Id": self.padel.id},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("code", response.get_json())

    def test_existing_auth_errors_carry_no_code(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.salon_user, self.padel)
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "Tenant not found or access denied"})


# ---------------------------------------------------------------------------
# D, N, O, P, Q: mutations and bypass attempts
# ---------------------------------------------------------------------------


class SuspendedMutationTests(SuspensionCase):
    def test_d_suspended_tenant_cannot_mutate_tenant_data(self):
        lead = Lead.query.filter_by(tenant_id=self.padel.id).one()
        before_name = self.padel.name
        attempts = [
            ("patch", "/api/v1/business", {"business_name": "Hijacked"}),
            ("patch", f"/api/v1/leads/{lead.id}", {"status": "won"}),
            ("post", f"/api/v1/conversations/{self.padel_conversation.id}/takeover", {}),
            ("post", f"/api/v1/conversations/{self.padel_conversation.id}/status",
             {"status": "closed"}),
            ("put", "/api/v1/knowledge/about", {"content": "evil"}),
            ("patch", "/api/v1/receptionist", {"greeting": "evil"}),
            ("patch", "/api/v1/automations/follow_up", {"enabled": True}),
            ("post", "/api/v1/channels", {"kind": "whatsapp", "address": "+26779999999"}),
        ]
        for method, path, payload in attempts:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(
                    path, headers=self.owner(), json=payload
                )
                self.assertSuspended(response, f"{method} {path}")

        db.session.expire_all()
        self.assertEqual(db.session.get(Tenant, self.padel.id).name, before_name)
        self.assertFalse(self.padel_conversation.human_takeover)
        self.assertIsNone(Channel.query.filter_by(address="+26779999999").one_or_none())

    def test_p_suspended_tenant_cannot_create_or_change_bookings(self):
        existing = Booking(tenant_id=self.padel.id, service="Court", status="pending",
                           source="staff")
        db.session.add(existing)
        db.session.commit()

        response = self.client.post(
            "/api/v1/bookings", headers=self.owner(),
            json={"service": "New court", "source": "staff"},
        )
        self.assertSuspended(response)
        response = self.client.post(
            f"/api/v1/bookings/{existing.id}/status", headers=self.owner(),
            json={"status": "cancelled"},
        )
        self.assertSuspended(response)

        db.session.expire_all()
        self.assertEqual(Booking.query.filter_by(tenant_id=self.padel.id).count(), 1)
        self.assertEqual(db.session.get(Booking, existing.id).status, "pending")

    def test_q_suspended_tenant_cannot_trigger_calendar_actions(self):
        db.session.add(CalendarConnection(tenant_id=self.padel.id, status="connected",
                                          calendar_id="primary", access_token="tok",
                                          refresh_token="ref"))
        db.session.commit()
        with patch("smartdesk.services.calendar._calendar_service") as service:
            self.assertSuspended(
                self.client.post("/api/v1/calendar/disconnect", headers=self.owner())
            )
            self.assertSuspended(
                self.client.get("/api/v1/calendar/connect", headers=self.owner())
            )
            self.assertSuspended(self.client.post(
                "/api/v1/bookings", headers=self.owner(),
                json={"service": "x", "source": "staff",
                      "starts_at": "2030-01-01T10:00:00+02:00",
                      "ends_at": "2030-01-01T11:00:00+02:00"},
            ))
        service.assert_not_called()
        db.session.expire_all()
        self.assertEqual(
            CalendarConnection.query.filter_by(tenant_id=self.padel.id).one().status,
            "connected",
        )

    def test_q_oauth_callback_cannot_attach_a_calendar_to_a_suspended_tenant(self):
        """The callback is a Google redirect that never passes ``require_tenant``,
        so it has its own check.  Nothing is exchanged or stored."""
        state = calendar_service.sign_state(self.padel.id, self.padel_user.id)
        with patch("google_auth_oauthlib.flow.Flow.from_client_config") as flow, \
                patch("smartdesk.services.calendar._fetch_account_email") as email:
            self.app.config["GOOGLE_CLIENT_ID"] = "id"
            self.app.config["GOOGLE_CLIENT_SECRET"] = "secret"
            self.app.config["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost/cb"
            response = self.client.get(f"/calendar/oauth/callback?code=c&state={state}")
        self.assertEqual(response.status_code, 302)
        self.assertIn("calendar=error", response.headers["Location"])
        flow.assert_not_called()
        email.assert_not_called()
        self.assertEqual(CalendarConnection.query.filter_by(tenant_id=self.padel.id).count(), 0)

    def test_n_suspended_owner_cannot_reactivate_the_tenant(self):
        for method, path, payload in (
            ("patch", "/api/v1/business", {"status": "active"}),
            ("patch", f"/api/v1/admin/tenants/{self.padel.id}", {"status": "active"}),
            ("post", f"/api/v1/admin/tenants/{self.padel.id}/owner",
             {"email": self.padel_user.email}),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(
                    path, headers=self.owner(), json=payload
                )
                self.assertEqual(response.status_code, 403)
        db.session.expire_all()
        self.assertEqual(db.session.get(Tenant, self.padel.id).status, "suspended")

    def test_n_suspended_owner_cannot_change_roles_or_add_memberships(self):
        viewer_membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).one()
        before = Membership.query.count()
        responses = [
            self.client.post("/api/v1/business/members", headers=self.owner(),
                             json={"email": self.salon_user.email, "role": "owner"}),
            self.client.patch(f"/api/v1/business/members/{viewer_membership.id}",
                              headers=self.owner(), json={"role": "owner"}),
            self.client.delete(f"/api/v1/business/members/{viewer_membership.id}",
                               headers=self.owner()),
        ]
        for response in responses:
            self.assertSuspended(response)
        db.session.expire_all()
        self.assertEqual(Membership.query.count(), before)
        self.assertEqual(db.session.get(Membership, viewer_membership.id).role, "viewer")

    def test_o_suspended_user_cannot_pivot_to_another_tenants_api(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.padel_user, self.salon)
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "Tenant not found or access denied"})
        previews = response.get_data(as_text=True)
        self.assertNotIn("Salon secret", previews)

    def test_m_suspended_tenant_cannot_read_active_tenant_data_by_id(self):
        response = self.client.get(
            f"/api/v1/customers/{self.salon_customer.id}", headers=self.owner()
        )
        self.assertSuspended(response)  # blocked before any lookup
        response = self.client.get(
            f"/api/v1/customers/{self.salon_customer.id}",
            headers=self.auth(self.padel_user, self.salon),
        )
        self.assertEqual(response.status_code, 403)

    def test_m_active_tenant_cannot_reach_the_suspended_tenants_data(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.salon_user, self.padel)
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("code", response.get_json())  # reveals nothing about its status


# ---------------------------------------------------------------------------
# J, K, 4/5: platform admin
# ---------------------------------------------------------------------------


class PlatformAdminTests(SuspensionCase):
    def test_j_platform_admin_can_inspect_the_suspended_tenant(self):
        headers = self.auth(self.admin_user, self.padel)
        for path in ("/api/v1/overview", "/api/v1/conversations", "/api/v1/customers",
                     "/api/v1/bookings", "/api/v1/business"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 200)

        admin = self.auth(self.admin_user)
        detail = self.client.get(f"/api/v1/admin/tenants/{self.padel.id}", headers=admin)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["status"], "suspended")
        listing = self.client.get("/api/v1/admin/tenants", headers=admin)
        self.assertIn(self.padel.id, {t["id"] for t in listing.get_json()["items"]})

    def test_platform_admin_can_still_manage_a_suspended_tenant(self):
        admin = self.auth(self.admin_user)
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}", headers=admin,
            json={"business_name": "10by20 Renamed"},
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}/monthly-limit", headers=admin,
            json={"monthly_conversation_limit": 100},
        )
        self.assertEqual(response.status_code, 200)

    def test_k_platform_admin_can_change_a_suspended_tenants_status(self):
        admin = self.auth(self.admin_user)
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}", headers=admin,
            json={"status": "active"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "active")
        # ...and the tenant's own users are immediately served again.
        self.assertEqual(
            self.client.get("/api/v1/overview", headers=self.owner()).status_code, 200
        )

    def test_platform_admin_can_suspend_an_active_tenant_and_it_takes_effect(self):
        admin = self.auth(self.admin_user)
        self.assertEqual(
            self.client.get("/api/v1/overview",
                            headers=self.auth(self.salon_user, self.salon)).status_code, 200
        )
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.salon.id}", headers=admin,
            json={"status": "suspended"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertSuspended(
            self.client.get("/api/v1/overview", headers=self.auth(self.salon_user, self.salon))
        )

    def test_5_platform_admin_working_in_an_active_tenant_is_unaffected(self):
        response = self.client.get("/api/v1/overview", headers=self.auth(self.admin_user, self.salon))
        self.assertEqual(response.status_code, 200)

    def test_platform_admin_gate_is_not_weakened(self):
        for path in ("/api/v1/admin/tenants", "/api/v1/admin/users", "/api/v1/admin/overview"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.auth(self.salon_user))
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("code", response.get_json())

    def test_unverified_platform_admin_is_still_refused(self):
        """The exemption does not bypass the email-confirmation gate."""
        with patch("smartdesk.security.rbac._is_email_confirmed", return_value=False):
            response = self.client.get(
                "/api/v1/overview", headers=self.auth(self.admin_user, self.padel)
            )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("code", response.get_json())


# ---------------------------------------------------------------------------
# E: outbound messages
# ---------------------------------------------------------------------------


class SuspendedOutboundTests(SuspensionCase):
    def _prepare_takeover(self):
        channel = Channel.query.filter_by(tenant_id=self.padel.id, kind="whatsapp").one()
        self.padel_conversation.channel_id = channel.id
        self.padel_conversation.channel_kind = "whatsapp"
        self.padel_conversation.human_takeover = True
        db.session.commit()
        return channel

    def test_e_suspended_tenant_cannot_send_a_staff_reply(self):
        self._prepare_takeover()
        with patch("smartdesk.api.dashboard.send_whatsapp_message") as send:
            response = self.client.post(
                f"/api/v1/conversations/{self.padel_conversation.id}/reply",
                headers=self.owner(), json={"body": "hello"},
            )
        self.assertSuspended(response)
        send.assert_not_called()
        self.assertEqual(
            Message.query.filter_by(conversation_id=self.padel_conversation.id,
                                    role="human_agent").count(), 0
        )

    def test_service_layer_send_refuses_a_suspended_tenant_before_twilio(self):
        """Defence in depth: even if a caller reached the send primitive
        directly, nothing leaves for Twilio."""
        channel = Channel.query.filter_by(tenant_id=self.padel.id, kind="whatsapp").one()
        with patch.object(receptionist_service, "twilio_client") as client:
            with self.assertRaises(receptionist_service.OutboundSendError):
                receptionist_service.send_whatsapp_message(channel, "+26771111111", "hi")
        client.assert_not_called()

    def test_service_layer_send_still_works_for_an_active_tenant(self):
        channel = Channel.query.filter_by(tenant_id=self.salon.id, kind="whatsapp").one()
        fake = MagicMock()
        fake.messages.create.return_value = SimpleNamespace(sid="SMok")
        with patch.object(receptionist_service, "twilio_client", return_value=fake):
            sid = receptionist_service.send_whatsapp_message(channel, "+26771111111", "hi")
        self.assertEqual(sid, "SMok")

    def test_escalation_notification_is_not_sent_for_a_suspended_tenant(self):
        self.padel.escalation_whatsapp = "+26773333333"
        db.session.commit()
        channel = Channel.query.filter_by(tenant_id=self.padel.id, kind="whatsapp").one()
        with patch.object(receptionist_service, "twilio_client") as client:
            receptionist_service.notify_escalation(self.padel, channel, "urgent")
        client.assert_not_called()

    def test_race_status_changes_after_the_request_guard_passed(self):
        """Request passes the guard while ACTIVE, the tenant is suspended, then
        the send runs.  Simulated by making the request-level guard pass
        (stale read) while the database says suspended: the send re-check
        still stops the message, and the staff reply is not recorded."""
        self._prepare_takeover()
        with patch("smartdesk.security.rbac.tenant_access_denial", return_value=None), \
                patch.object(receptionist_service, "twilio_client") as client:
            response = self.client.post(
                f"/api/v1/conversations/{self.padel_conversation.id}/reply",
                headers=self.owner(), json={"body": "hello"},
            )
        self.assertEqual(response.status_code, 502)
        client.assert_not_called()
        self.assertEqual(
            Message.query.filter_by(conversation_id=self.padel_conversation.id,
                                    role="human_agent").count(), 0
        )

    def test_race_calendar_write_is_stopped_after_the_guard_passed(self):
        db.session.add(CalendarConnection(tenant_id=self.padel.id, status="connected",
                                          calendar_id="primary", access_token="tok",
                                          refresh_token="ref"))
        db.session.commit()
        with patch("smartdesk.security.rbac.tenant_access_denial", return_value=None), \
                patch("smartdesk.services.calendar._calendar_service") as service:
            response = self.client.post(
                "/api/v1/bookings", headers=self.owner(),
                json={"service": "x", "source": "staff",
                      "starts_at": "2030-01-01T10:00:00+02:00",
                      "ends_at": "2030-01-01T11:00:00+02:00"},
            )
        service.assert_not_called()
        # The DB row is the unavoidable part of the race (see the report); the
        # external write is what must not happen, and the caller is told.
        self.assertEqual(response.status_code, 201)
        self.assertIn("calendar_warning", response.get_json())

    def test_calendar_cancel_does_nothing_for_an_inactive_tenant(self):
        booking = Booking(tenant_id=self.padel.id, service="x", source="staff",
                          external_system="google_calendar", external_reference="evt")
        db.session.add(booking)
        db.session.add(CalendarConnection(tenant_id=self.padel.id, status="connected",
                                          calendar_id="primary", access_token="tok",
                                          refresh_token="ref"))
        db.session.commit()
        with patch("smartdesk.services.calendar._calendar_service") as service:
            calendar_service.cancel_event_for_booking(self.padel, booking)
        service.assert_not_called()

    def test_fresh_status_read_sees_a_change_the_session_has_cached(self):
        self.assertFalse(tenant_is_active_now(self.padel.id))
        self.assertTrue(tenant_is_active_now(self.salon.id))
        # Change the row behind the ORM object's back: the cached attribute is
        # stale, the fresh read is not.
        db.session.execute(db.text("UPDATE tenants SET status='active' WHERE id=:i"),
                           {"i": self.padel.id})
        self.assertTrue(tenant_is_active_now(self.padel.id))
        self.assertFalse(tenant_is_active_now("00000000-0000-0000-0000-000000000000"))


# ---------------------------------------------------------------------------
# F, G, H, I, S: inbound WhatsApp / voice
# ---------------------------------------------------------------------------


class SuspendedWebhookTests(WhatsAppWebhookTestCase):
    def setUp(self) -> None:
        super().setUp()
        set_status(self.padel, "suspended")

    def snapshot(self, tenant=None):
        tenant = tenant or self.padel
        return {
            "customers": Customer.query.filter_by(tenant_id=tenant.id).count(),
            "conversations": Conversation.query.filter_by(tenant_id=tenant.id).count(),
            "messages": Message.query.filter_by(tenant_id=tenant.id).count(),
            "reservations": UsageReservation.query.filter_by(tenant_id=tenant.id).count(),
            "usage_events": UsageEvent.query.filter_by(tenant_id=tenant.id).count(),
            "leads": Lead.query.filter_by(tenant_id=tenant.id).count(),
        }

    def test_f_h_suspended_inbound_never_reaches_openai_or_the_engine(self):
        response = self.post(sid=new_sid())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.openai.calls), 0)
        self.assertEqual(self.engine_calls, 0)

    def test_i_suspended_inbound_sends_no_ai_response(self):
        response = self.post(sid=new_sid())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"<Message>", response.get_data())
        self.assertNotIn(b"Fake AI reply", response.get_data())

    def test_g_suspended_inbound_reserves_and_consumes_no_usage(self):
        before = self.snapshot()
        self.post(sid=new_sid())
        self.post(sid=new_sid(), body="Hello again")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(UsageReservation.query.filter_by(tenant_id=self.padel.id).count(), 0)

    def test_suspended_inbound_leaves_no_trace_at_all(self):
        """No customer, conversation, message, lead or channel timestamp."""
        before = self.snapshot()
        channel = Channel.query.filter_by(tenant_id=self.padel.id, kind="whatsapp").one()
        self.post(sid=new_sid())
        db.session.expire_all()
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(db.session.get(Channel, channel.id).last_inbound_at)

    def test_s_duplicate_suspended_message_sid_remains_safe(self):
        sid = new_sid()
        before = self.snapshot()
        for _ in range(3):
            response = self.post(sid=sid)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"<Message>", response.get_data())
        self.assertEqual(len(self.openai.calls), 0)
        self.assertEqual(self.snapshot(), before)

    def test_suspension_is_checked_before_messagesid_validation(self):
        """Order: signature -> channel/tenant -> SUSPENSION -> MessageSid.
        A suspended tenant's unusable request is a quiet 200, not a 400."""
        response = self.post(sid=None)
        self.assertEqual(response.status_code, 200)
        response = self.post(sid="not-a-sid")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.openai.calls), 0)

    def test_reactivation_restores_whatsapp_service(self):
        set_status(self.padel, "active")
        response = self.post(sid=new_sid())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.openai.calls), 1)
        self.assertIn(b"Fake AI reply", response.get_data())

    def test_l_active_tenant_whatsapp_is_unaffected(self):
        response = self.post(sid=new_sid(), to=SALON_NUMBER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.openai.calls), 1)
        self.assertIn(b"Fake AI reply", response.get_data())
        self.assertEqual(UsageReservation.query.filter_by(tenant_id=self.salon.id).count(), 1)

    def test_suspended_voice_call_is_refused_without_ai_or_records(self):
        before = self.snapshot()
        response = self.client.post(
            "/voice",
            data={"From": "+26772222222", "To": PADEL_VOICE_NUMBER, "CallSid": "CA1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"not currently in service", response.get_data())
        self.assertEqual(self.snapshot(), before)
        response = self.client.post(
            "/voice/continue",
            data={"From": "+26772222222", "To": PADEL_VOICE_NUMBER, "CallSid": "CA1",
                  "SpeechResult": AI_QUESTION},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.openai.calls), 0)
        self.assertEqual(self.snapshot(), before)


class FailClosedTests(SuspensionCase):
    def test_unrecognised_status_is_never_treated_as_active(self):
        """The DB CHECK constraint prevents this in production; the guard must
        still not assume ACTIVE if it ever happened (e.g. a future status)."""
        set_status(self.salon, "active")
        with patch("smartdesk.security.rbac.tenant_access_denial",
                   wraps=lambda t: tenant_access_denial(SimpleNamespace(status="weird"))):
            response = self.client.get(
                "/api/v1/overview", headers=self.auth(self.salon_user, self.salon)
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "tenant_unavailable")

    def test_a_failing_status_check_denies_the_request_and_is_not_reported_as_suspended(self):
        """An internal error during the check is the app's normal unhandled-error
        500 -- the view never runs, and it is not mislabelled ``tenant_suspended``."""
        with patch("smartdesk.security.rbac.tenant_access_denial",
                   side_effect=RuntimeError("boom")):
            response = self.client.get(
                "/api/v1/overview", headers=self.auth(self.salon_user, self.salon)
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "Internal server error"})

    def test_missing_tenant_row_is_refused(self):
        response = self.client.get(
            "/api/v1/overview",
            headers={**self.auth(self.admin_user),
                     "X-Tenant-Id": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
