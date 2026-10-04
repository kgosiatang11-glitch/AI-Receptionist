"""PostgreSQL proof of tenant suspension enforcement (Phase 3.4 / C6).

SQLite cannot show what these show: RLS-bound sessions, a suspension committed
on a *different* connection becoming visible mid-transaction (READ COMMITTED),
and concurrent suspended webhook deliveries.  Skipped without
``TEST_DATABASE_URL`` (and says so); the target database is WIPED and rebuilt
from the Alembic migrations, and the base class refuses any database whose name
does not contain "test".

    createdb smartdesk_test
    TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost/smartdesk_test \\
        python -m pytest tests/test_tenant_suspension_postgres.py

``rbac._is_email_confirmed`` is patched to True: on PostgreSQL it calls the
Supabase-side ``public.is_email_confirmed`` function, which does not exist in
this test database (that function is unrelated to suspension and is covered by
``tests/test_signup_and_verification.py``).
"""

from __future__ import annotations

import threading
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import text

from smartdesk.extensions import db
from smartdesk.models import (
    Channel,
    Conversation,
    Customer,
    Lead,
    Membership,
    Message,
    ReceptionistProfile,
    Tenant,
    UsageEvent,
    UsageReservation,
    User,
)
from smartdesk.services import receptionist as receptionist_service
from smartdesk.tenancy import tenant_is_active_now
from tests.test_multitenant import make_token
from tests.test_whatsapp_idempotency_postgres import (
    CUSTOMER,
    NUMBER,
    PostgresWebhookTestCase,
    _sid,
)

SECOND_NUMBER = "+26770000002"
CONFIRMED = patch("smartdesk.security.rbac._is_email_confirmed", return_value=True)


class SuspensionPgCase(PostgresWebhookTestCase):
    def setUp(self) -> None:
        super().setUp()
        confirmed = CONFIRMED.start()
        self.addCleanup(CONFIRMED.stop)
        del confirmed

        self.owner_a = User(supabase_user_id=str(uuid.uuid4()), email="owner@a.test")
        self.owner_b = User(supabase_user_id=str(uuid.uuid4()), email="owner@b.test")
        self.admin = User(supabase_user_id=str(uuid.uuid4()), email="admin@platform.test",
                          is_platform_admin=True)
        self.tenant_b = Tenant(slug="b", name="Business B")
        db.session.add_all([self.owner_a, self.owner_b, self.admin, self.tenant_b])
        db.session.flush()
        db.session.add_all([
            Membership(tenant_id=self.tenant_id, user_id=self.owner_a.id, role="owner"),
            Membership(tenant_id=self.tenant_b.id, user_id=self.owner_b.id, role="owner"),
            Channel(tenant_id=self.tenant_b.id, kind="whatsapp", address=SECOND_NUMBER),
            ReceptionistProfile(tenant_id=self.tenant_b.id, sales_mode_enabled=False),
        ])
        customer = Customer(tenant_id=self.tenant_id, phone="+26771111111",
                            full_name="A Customer")
        db.session.add(customer)
        db.session.flush()
        db.session.add(Lead(tenant_id=self.tenant_id, customer_id=customer.id,
                            interest="A secret"))
        db.session.commit()
        self.tenant_b_id = self.tenant_b.id
        self.owner_a_headers = self.headers(self.owner_a, self.tenant_id)
        self.owner_b_headers = self.headers(self.owner_b, self.tenant_b_id)
        self.admin_headers = self.headers(self.admin)
        db.session.remove()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def headers(user, tenant_id=None) -> dict:
        h = {"Authorization": f"Bearer {make_token(user.supabase_user_id, user.email)}"}
        if tenant_id:
            h["X-Tenant-Id"] = tenant_id
        return h

    def set_status(self, tenant_id, status) -> None:
        """Commit a status change from a connection of its own."""
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE tenants SET status = :s WHERE id = :t"),
                         {"s": status, "t": tenant_id})
        db.session.remove()

    def get(self, path, headers):
        return self.app.test_client().get(path, headers=headers)

    def usage_footprint(self, tenant_id):
        db.session.remove()
        q = lambda model: model.query.filter_by(tenant_id=tenant_id).count()
        return {m.__name__: q(m) for m in
                (Conversation, Message, UsageEvent, UsageReservation, Lead)}


class SuspendedApiPgTests(SuspensionPgCase):
    def test_suspended_owner_is_refused_with_the_stable_code_under_rls(self):
        self.set_status(self.tenant_id, "suspended")
        for path in ("/api/v1/overview", "/api/v1/leads", "/api/v1/customers",
                     "/api/v1/conversations", "/api/v1/bookings", "/api/v1/business"):
            with self.subTest(path=path):
                response = self.get(path, self.owner_a_headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["code"], "tenant_suspended")
                self.assertNotIn(b"A secret", response.get_data())

    def test_active_tenant_is_served_under_rls(self):
        response = self.get("/api/v1/leads", self.owner_a_headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A secret", response.get_data())

    def test_platform_admin_can_inspect_a_suspended_tenant_under_rls(self):
        """The admin's session is RLS-bound to the suspended tenant, so this
        also proves the exemption does not skip tenant binding."""
        self.set_status(self.tenant_id, "suspended")
        headers = self.headers(self.admin, self.tenant_id)
        response = self.get("/api/v1/leads", headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A secret", response.get_data())
        detail = self.get(f"/api/v1/admin/tenants/{self.tenant_id}", self.admin_headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["status"], "suspended")

    def test_platform_admin_can_reactivate_and_owner_recovers(self):
        self.set_status(self.tenant_id, "suspended")
        client = self.app.test_client()
        response = client.patch(f"/api/v1/admin/tenants/{self.tenant_id}",
                                headers=self.admin_headers, json={"status": "active"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get("/api/v1/overview", self.owner_a_headers).status_code, 200)

    def test_suspended_owner_cannot_reactivate_through_any_route(self):
        self.set_status(self.tenant_id, "suspended")
        client = self.app.test_client()
        for method, path in (("patch", "/api/v1/business"),
                             ("patch", f"/api/v1/admin/tenants/{self.tenant_id}")):
            response = getattr(client, method)(path, headers=self.owner_a_headers,
                                               json={"status": "active"})
            self.assertEqual(response.status_code, 403)
        db.session.remove()
        self.assertEqual(db.session.get(Tenant, self.tenant_id).status, "suspended")

    def test_tenant_a_suspended_leaves_tenant_b_fully_working_and_isolated(self):
        self.set_status(self.tenant_id, "suspended")
        self.assertEqual(self.get("/api/v1/overview", self.owner_b_headers).status_code, 200)
        leads = self.get("/api/v1/leads", self.owner_b_headers)
        self.assertEqual(leads.status_code, 200)
        self.assertNotIn(b"A secret", leads.get_data())
        # A's owner pointing at B: the generic membership refusal, no status leak.
        cross = self.get("/api/v1/leads", self.headers(self.owner_a, self.tenant_b_id))
        self.assertEqual(cross.status_code, 403)
        self.assertNotIn("code", cross.get_json())

    def test_unauthenticated_request_is_unchanged(self):
        self.set_status(self.tenant_id, "suspended")
        response = self.get("/api/v1/leads", {"X-Tenant-Id": self.tenant_id})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("code", response.get_json())


class StatusVisibilityPgTests(SuspensionPgCase):
    def test_suspension_committed_elsewhere_is_seen_inside_an_open_transaction(self):
        """READ COMMITTED: the second read in the SAME transaction sees a
        suspension another connection committed after the first read."""
        self.assertTrue(tenant_is_active_now(self.tenant_id))
        self.assertTrue(db.session().in_transaction())
        with db.engine.begin() as other:
            other.execute(text("UPDATE tenants SET status='suspended' WHERE id=:t"),
                          {"t": self.tenant_id})
        self.assertFalse(tenant_is_active_now(self.tenant_id))

    def test_send_is_refused_when_suspended_after_the_tenant_was_loaded(self):
        """The race from the brief: the tenant (and channel) were loaded while
        ACTIVE; the suspension commits on another connection; the send that
        follows must not reach Twilio even though the ORM object is stale."""
        channel = Channel.query.filter_by(tenant_id=self.tenant_id, kind="whatsapp").one()
        tenant = db.session.get(Tenant, self.tenant_id)
        self.assertEqual(tenant.status, "active")  # cached on the ORM object
        with db.engine.begin() as other:
            other.execute(text("UPDATE tenants SET status='suspended' WHERE id=:t"),
                          {"t": self.tenant_id})
        self.assertEqual(tenant.status, "active")  # still stale in this session
        with patch.object(receptionist_service, "twilio_client") as client:
            with self.assertRaises(receptionist_service.OutboundSendError):
                receptionist_service.send_whatsapp_message(channel, "+26771111111", "hi")
        client.assert_not_called()

    def test_next_request_after_suspension_is_refused_without_a_restart_or_cache(self):
        self.assertEqual(self.get("/api/v1/overview", self.owner_a_headers).status_code, 200)
        self.set_status(self.tenant_id, "suspended")
        self.assertEqual(self.get("/api/v1/overview", self.owner_a_headers).status_code, 403)


class SuspendedWebhookPgTests(SuspensionPgCase):
    def test_suspended_inbound_does_not_reserve_call_openai_or_reply(self):
        self.set_status(self.tenant_id, "suspended")
        before = self.usage_footprint(self.tenant_id)
        client = self.app.test_client()
        response = client.post("/whatsapp", data=self.form(_sid()))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"<Message>", response.data)
        self.assertEqual(self.openai_calls, 0)
        self.assertEqual(self.engine_calls, 0)
        self.assertEqual(self.usage_footprint(self.tenant_id), before)

    def test_concurrent_duplicate_suspended_deliveries_are_all_inert(self):
        self.set_status(self.tenant_id, "suspended")
        before = self.usage_footprint(self.tenant_id)
        sid = _sid()
        results = self.fire_concurrently([self.form(sid) for _ in range(6)])
        self.assertTrue(all(status == 200 and not replied for status, replied in results),
                        results)
        self.assertEqual(self.openai_calls, 0)
        self.assertEqual(self.usage_footprint(self.tenant_id), before)

    def test_suspension_takes_effect_between_two_messages(self):
        first = self.app.test_client().post("/whatsapp", data=self.form(_sid()))
        self.assertIn(b"<Message>", first.data)
        self.assertEqual(self.openai_calls, 1)

        self.set_status(self.tenant_id, "suspended")
        second = self.app.test_client().post(
            "/whatsapp", data=self.form(_sid(), body="Another question please?")
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotIn(b"<Message>", second.data)
        self.assertEqual(self.openai_calls, 1)  # unchanged

        self.set_status(self.tenant_id, "active")
        third = self.app.test_client().post(
            "/whatsapp", data=self.form(_sid(), body="Is it open on Sunday?")
        )
        self.assertIn(b"<Message>", third.data)

    def test_other_tenants_whatsapp_is_unaffected(self):
        self.set_status(self.tenant_id, "suspended")
        data = {"From": f"whatsapp:{CUSTOMER}", "To": f"whatsapp:{SECOND_NUMBER}",
                "Body": "How much does a court cost per hour?", "MessageSid": _sid()}
        response = self.app.test_client().post("/whatsapp", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<Message>", response.data)
        self.assertEqual(self.openai_calls, 1)
        db.session.remove()
        self.assertEqual(
            UsageReservation.query.filter_by(tenant_id=self.tenant_b_id).count(), 1
        )
        self.assertEqual(
            UsageReservation.query.filter_by(tenant_id=self.tenant_id).count(), 0
        )


if __name__ == "__main__":
    unittest.main()
