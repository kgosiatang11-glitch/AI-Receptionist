"""Shared adversarial checks for C5 -- cross-tenant WRITE isolation.

A mixin, not a TestCase, so the same assertions run unchanged on SQLite
(``tests/test_cross_tenant_writes.py``) and on real PostgreSQL
(``tests/test_cross_tenant_writes_postgres.py``).  The concrete class supplies
``self.ta`` / ``self.tb`` (two Tenant rows) and calls ``build_c5_world()``.

Only real attack surfaces are exercised: every route below exists in
``smartdesk/api`` and takes the reference from the request.  For every rejected
request the checks assert more than the status code -- no row created or
changed, no audit/usage row, no calendar or WhatsApp side effect, and no
identifying data of the other tenant in the response body.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from smartdesk.extensions import db
from smartdesk.models import (
    AuditLog,
    Booking,
    Channel,
    Conversation,
    Customer,
    Lead,
    Membership,
    Message,
    Tenant,
    UsageEvent,
    User,
)
from tests.test_multitenant import make_token

B_NAME, B_PHONE = "Zanele Secret-B", "+26779990001"
B_EMAIL = "owner-b-secret@b.test"
STARTS, ENDS = "2026-11-01T10:00:00+02:00", "2026-11-01T11:00:00+02:00"


class C5Fixtures:
    # -- fixtures --------------------------------------------------------
    def build_c5_world(self) -> None:
        """Tenant A and B, each with an owner, a customer, a lead, a booking,
        a conversation; A also has a plain member; plus a platform admin who
        belongs to neither."""
        mk = lambda email, **kw: User(  # noqa: E731
            supabase_user_id=str(uuid.uuid4()), email=email, **kw)
        self.owner_a, self.member_a = mk("owner@a.c5.test"), mk("member@a.c5.test")
        self.owner_b = mk(B_EMAIL, full_name=B_NAME)
        self.admin = mk("admin@platform.c5.test", is_platform_admin=True)
        db.session.add_all([self.owner_a, self.member_a, self.owner_b, self.admin])
        db.session.flush()
        db.session.add_all([
            Membership(tenant_id=self.ta.id, user_id=self.owner_a.id, role="owner"),
            Membership(tenant_id=self.ta.id, user_id=self.member_a.id, role="agent"),
            Membership(tenant_id=self.tb.id, user_id=self.owner_b.id, role="owner"),
        ])
        self.cust_a = Customer(tenant_id=self.ta.id, phone="+26773000001",
                               full_name="Customer A")
        self.cust_b = Customer(tenant_id=self.tb.id, phone=B_PHONE, full_name=B_NAME,
                               email="b-customer@b.test")
        db.session.add_all([self.cust_a, self.cust_b])
        db.session.flush()
        self.lead_a = Lead(tenant_id=self.ta.id, customer_id=self.cust_a.id, interest="A lead")
        self.lead_b = Lead(tenant_id=self.tb.id, customer_id=self.cust_b.id, interest="B lead")
        self.booking_b = Booking(tenant_id=self.tb.id, customer_id=self.cust_b.id,
                                 service="B service", status="confirmed")
        self.conv_a = Conversation(tenant_id=self.ta.id, session_key="c5:a",
                                   customer_id=self.cust_a.id, channel_kind="whatsapp")
        self.conv_b = Conversation(tenant_id=self.tb.id, session_key="c5:b",
                                   customer_id=self.cust_b.id, channel_kind="whatsapp")
        self.chan_b = Channel(tenant_id=self.tb.id, kind="whatsapp", address="+26779990099")
        db.session.add_all([self.lead_a, self.lead_b, self.booking_b,
                            self.conv_a, self.conv_b, self.chan_b])
        db.session.commit()
        # Plain strings: survive db.session.remove() between request and check.
        for name in ("ta", "tb", "owner_a", "member_a", "owner_b", "admin", "cust_a",
                     "cust_b", "lead_a", "lead_b", "booking_b", "conv_a", "conv_b",
                     "chan_b"):
            setattr(self, f"{name}_id", getattr(self, name).id)
        self.hdr_a = self.headers(self.owner_a, self.ta_id)
        self.hdr_b = self.headers(self.owner_b, self.tb_id)
        self.hdr_admin_a = self.headers(self.admin, self.ta_id)
        db.session.remove()

    @staticmethod
    def headers(user, tenant_id=None) -> dict:
        h = {"Authorization": f"Bearer {make_token(user.supabase_user_id, user.email)}"}
        if tenant_id:
            h["X-Tenant-Id"] = tenant_id
        return h

    # -- helpers ---------------------------------------------------------
    def post_booking(self, headers, **payload):
        body = {"service": "Court", "starts_at": STARTS, "ends_at": ENDS, **payload}
        return self.app.test_client().post("/api/v1/bookings", headers=headers, json=body)

    def patch_lead(self, headers, lead_id, **payload):
        return self.app.test_client().patch(
            f"/api/v1/leads/{lead_id}", headers=headers, json=payload)

    def footprint(self) -> dict:
        """Everything a rejected request must leave untouched."""
        db.session.remove()
        lead = lambda i: tuple(db.session.execute(  # noqa: E731
            db.select(Lead.status, Lead.interest, Lead.assigned_user_id).where(Lead.id == i)
        ).one())
        count = lambda m, **f: m.query.filter_by(**f).count()  # noqa: E731
        snap = {
            "bookings_a": count(Booking, tenant_id=self.ta_id),
            "bookings_b": count(Booking, tenant_id=self.tb_id),
            "audit": AuditLog.query.count(),
            "usage": UsageEvent.query.count(),
            "messages": Message.query.count(),
            "lead_a": lead(self.lead_a_id),
            "lead_b": lead(self.lead_b_id),
            "booking_b": tuple(db.session.execute(
                db.select(Booking.status, Booking.service, Booking.customer_id)
                .where(Booking.id == self.booking_b_id)).one()),
        }
        db.session.remove()
        return snap

    def assert_no_leak(self, response, *extra) -> None:
        body = response.get_data()
        for secret in (B_NAME, B_PHONE, B_EMAIL, "b-customer@b.test", *extra):
            self.assertNotIn(secret.encode(), body, f"leaked {secret!r}")

    def assert_rejected(self, response, field) -> None:
        self.assertEqual(response.status_code, 404, response.get_data())
        data = response.get_json()
        self.assertEqual(data["code"], "reference_not_found")
        self.assertEqual(data["field"], field)
        self.assert_no_leak(response)

    def side_effects(self):
        """Patch the outbound edges and hand back the mocks."""
        from smartdesk.api import dashboard
        cal = patch.object(dashboard.calendar_service, "get_connection",
                           return_value=object())
        create = patch.object(dashboard.calendar_service, "create_event_for_booking",
                              return_value="evt")
        cancel = patch.object(dashboard.calendar_service, "cancel_event_for_booking")
        send = patch.object(dashboard, "send_whatsapp_message", return_value="SMx")
        mocks = []
        for p in (cal, create, cancel, send):
            mocks.append(p.start())
            self.addCleanup(p.stop)
        return {"get_connection": mocks[0], "create": mocks[1],
                "cancel": mocks[2], "send": mocks[3]}


class CrossTenantWriteChecks(C5Fixtures):
    # ====================== BOOKINGS: customer_id =======================
    def test_booking_same_tenant_customer_is_created(self):
        fx = self.side_effects()
        response = self.post_booking(self.hdr_a, customer_id=self.cust_a_id)
        self.assertEqual(response.status_code, 201, response.get_data())
        self.assertEqual(response.get_json()["customer"]["id"], self.cust_a_id)
        fx["create"].assert_called_once()  # same-tenant sync still happens
        db.session.remove()
        row = Booking.query.filter_by(tenant_id=self.ta_id).one()
        self.assertEqual(row.customer_id, self.cust_a_id)

    def test_booking_cross_tenant_customer_is_refused_without_any_effect(self):
        fx = self.side_effects()
        before = self.footprint()
        response = self.post_booking(self.hdr_a, customer_id=self.cust_b_id)
        self.assert_rejected(response, "customer_id")
        self.assertEqual(self.footprint(), before)   # no row, audit, usage, message
        fx["create"].assert_not_called()             # no calendar side effect
        fx["send"].assert_not_called()
        db.session.remove()
        self.assertEqual(Booking.query.filter_by(customer_id=self.cust_b_id,
                                                 tenant_id=self.ta_id).count(), 0)

    def test_unknown_and_cross_tenant_customer_are_indistinguishable(self):
        unknown = self.post_booking(self.hdr_a, customer_id=str(uuid.uuid4()))
        cross = self.post_booking(self.hdr_a, customer_id=self.cust_b_id)
        self.assertEqual(unknown.status_code, cross.status_code)
        self.assertEqual(unknown.get_json(), cross.get_json())  # no existence oracle

    def test_malformed_customer_ids_are_a_stable_404_not_a_500(self):
        before = self.footprint()
        for bad in ("not-a-uuid", "1' OR '1'='1", 12345, {"id": self.cust_b_id},
                    [self.cust_b_id], True):
            with self.subTest(customer_id=bad):
                self.assert_rejected(self.post_booking(self.hdr_a, customer_id=bad),
                                     "customer_id")
        self.assertEqual(self.footprint(), before)

    def test_booking_without_a_customer_still_works(self):
        for kwargs in ({}, {"customer_id": None}, {"customer_id": ""}):
            with self.subTest(kwargs=kwargs):
                response = self.post_booking(self.hdr_a, **kwargs)
                self.assertEqual(response.status_code, 201, response.get_data())
                self.assertIsNone(response.get_json()["customer"])

    def test_tenant_b_can_still_use_its_own_customer(self):
        response = self.post_booking(self.hdr_b, customer_id=self.cust_b_id)
        self.assertEqual(response.status_code, 201, response.get_data())
        self.assertEqual(response.get_json()["customer"]["id"], self.cust_b_id)

    def test_tenant_b_cannot_use_tenant_a_customer(self):
        response = self.post_booking(self.hdr_b, customer_id=self.cust_a_id)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Customer A", response.get_data())

    def test_booking_ignores_client_supplied_tenant_and_other_foreign_keys(self):
        """tenant_id / conversation_id / channel_id / booking_id in the body are
        not accepted fields: they must not steer where or how the row is written."""
        response = self.post_booking(
            self.hdr_a, customer_id=self.cust_a_id, tenant_id=self.tb_id,
            conversation_id=self.conv_b_id, channel_id=self.chan_b_id,
            booking_id=self.booking_b_id, id=self.booking_b_id)
        self.assertEqual(response.status_code, 201, response.get_data())
        db.session.remove()
        row = db.session.get(Booking, response.get_json()["id"])
        self.assertEqual(row.tenant_id, self.ta_id)
        self.assertIsNone(row.conversation_id)
        self.assertNotEqual(row.id, self.booking_b_id)
        self.assertEqual(Booking.query.filter_by(tenant_id=self.tb_id).count(), 1)

    def test_platform_admin_in_tenant_a_cannot_reference_tenant_b_customer(self):
        fx = self.side_effects()
        before = self.footprint()
        response = self.post_booking(self.hdr_admin_a, customer_id=self.cust_b_id)
        self.assert_rejected(response, "customer_id")
        self.assertEqual(self.footprint(), before)
        fx["create"].assert_not_called()
        ok = self.post_booking(self.hdr_admin_a, customer_id=self.cust_a_id)
        self.assertEqual(ok.status_code, 201, ok.get_data())

    # ====================== LEADS: assigned_user_id =====================
    def test_lead_same_tenant_user_can_be_assigned_and_unassigned(self):
        ok = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.member_a_id,
                             status="contacted")
        self.assertEqual(ok.status_code, 200, ok.get_data())
        self.assertEqual(ok.get_json()["assigned_user"]["id"], self.member_a_id)
        for clear in (None, ""):
            with self.subTest(clear=clear):
                self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.member_a_id)
                cleared = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=clear)
                self.assertEqual(cleared.status_code, 200)
                self.assertIsNone(cleared.get_json()["assigned_user"])

    def test_lead_cross_tenant_assignee_is_refused_with_no_partial_write(self):
        before = self.footprint()
        response = self.patch_lead(
            self.hdr_a, self.lead_a_id, assigned_user_id=self.owner_b_id,
            status="converted", interest="changed by attacker")
        self.assert_rejected(response, "assigned_user_id")
        self.assertEqual(self.footprint(), before)   # status/interest NOT applied either
        self.assertEqual(before["lead_b"], self.footprint()["lead_b"])

    def test_lead_unknown_and_cross_tenant_assignee_are_indistinguishable(self):
        a = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=str(uuid.uuid4()))
        b = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.owner_b_id)
        self.assertEqual((a.status_code, a.get_json()), (b.status_code, b.get_json()))

    def test_lead_malformed_assignee_is_a_stable_404_not_a_500(self):
        before = self.footprint()
        for bad in ("nope", 7, {"x": 1}, ["a"], False, True):
            with self.subTest(assigned_user_id=bad):
                self.assert_rejected(
                    self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=bad),
                    "assigned_user_id")
        self.assertEqual(self.footprint(), before)

    def test_lead_update_without_assignee_key_leaves_assignment_alone(self):
        self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.member_a_id)
        ok = self.patch_lead(self.hdr_a, self.lead_a_id, status="qualified")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["assigned_user"]["id"], self.member_a_id)

    def test_lead_invalid_status_plus_cross_tenant_assignee_changes_nothing(self):
        before = self.footprint()
        response = self.patch_lead(self.hdr_a, self.lead_a_id, status="bogus",
                                   assigned_user_id=self.owner_b_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.footprint(), before)

    def test_platform_admin_is_not_assignable_unless_a_member_of_the_tenant(self):
        response = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.admin_id)
        self.assert_rejected(response, "assigned_user_id")

    def test_platform_admin_in_tenant_a_cannot_assign_tenant_b_user(self):
        before = self.footprint()
        response = self.patch_lead(self.hdr_admin_a, self.lead_a_id,
                                   assigned_user_id=self.owner_b_id)
        self.assert_rejected(response, "assigned_user_id")
        self.assertEqual(self.footprint(), before)
        ok = self.patch_lead(self.hdr_admin_a, self.lead_a_id,
                             assigned_user_id=self.member_a_id)
        self.assertEqual(ok.status_code, 200, ok.get_data())

    def test_tenant_b_is_unaffected_and_can_assign_its_own_user(self):
        before_b = self.footprint()["lead_b"]
        self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.owner_b_id)
        self.assertEqual(self.footprint()["lead_b"], before_b)
        ok = self.patch_lead(self.hdr_b, self.lead_b_id, assigned_user_id=self.owner_b_id)
        self.assertEqual(ok.status_code, 200, ok.get_data())
        # ...and B still cannot assign A's member to its lead.
        bad = self.patch_lead(self.hdr_b, self.lead_b_id, assigned_user_id=self.member_a_id)
        self.assertEqual(bad.status_code, 404)

    # ============ other client-controlled IDs (URL path references) ======
    def test_lead_id_from_another_tenant_is_not_writable(self):
        before = self.footprint()
        response = self.patch_lead(self.hdr_a, self.lead_b_id, status="lost",
                                   assigned_user_id=self.owner_a_id)
        self.assertEqual(response.status_code, 404)
        self.assert_no_leak(response)
        self.assertEqual(self.footprint(), before)

    def test_booking_id_from_another_tenant_is_not_writable_and_not_cancelled(self):
        fx = self.side_effects()
        before = self.footprint()
        response = self.app.test_client().post(
            f"/api/v1/bookings/{self.booking_b_id}/status",
            headers=self.hdr_a, json={"status": "cancelled"})
        self.assertEqual(response.status_code, 404)
        self.assert_no_leak(response, "B service")
        self.assertEqual(self.footprint(), before)
        fx["cancel"].assert_not_called()             # B's calendar event untouched

    def test_conversation_id_from_another_tenant_is_not_writable(self):
        fx = self.side_effects()
        before = self.footprint()
        client = self.app.test_client()
        for path, body in (("takeover", {"enabled": True}), ("status", {"status": "closed"}),
                           ("reply", {"body": "hello from the wrong tenant"})):
            with self.subTest(route=path):
                response = client.post(f"/api/v1/conversations/{self.conv_b_id}/{path}",
                                       headers=self.hdr_a, json=body)
                self.assertEqual(response.status_code, 404)
                self.assert_no_leak(response)
        self.assertEqual(self.footprint(), before)
        fx["send"].assert_not_called()               # nothing sent to B's customer
        db.session.remove()
        conv = db.session.get(Conversation, self.conv_b_id)
        self.assertFalse(conv.human_takeover)
        self.assertEqual(conv.status, "active")

    def test_channel_of_another_tenant_is_never_used_to_send(self):
        """channel_id comes from our own conversation row, not the client, but
        the send is an irreversible side effect: a conversation pointing at
        another tenant's channel must be refused BEFORE sending."""
        fx = self.side_effects()
        conv = db.session.get(Conversation, self.conv_a_id)
        conv.channel_id, conv.human_takeover = self.chan_b_id, True
        db.session.commit()
        db.session.remove()
        response = self.app.test_client().post(
            f"/api/v1/conversations/{self.conv_a_id}/reply",
            headers=self.hdr_a, json={"body": "hi"})
        self.assertEqual(response.status_code, 409)
        self.assert_no_leak(response, "+26779990099")
        fx["send"].assert_not_called()

    def test_membership_id_from_another_tenant_is_not_writable(self):
        db.session.remove()
        b_membership = Membership.query.filter_by(
            tenant_id=self.tb_id, user_id=self.owner_b_id).one().id
        db.session.remove()
        client = self.app.test_client()
        patch_r = client.patch(f"/api/v1/business/members/{b_membership}",
                               headers=self.hdr_a, json={"role": "viewer"})
        delete_r = client.delete(f"/api/v1/business/members/{b_membership}",
                                 headers=self.hdr_a)
        self.assertEqual((patch_r.status_code, delete_r.status_code), (404, 404))
        db.session.remove()
        self.assertEqual(db.session.get(Membership, b_membership).role, "owner")

    # ================================ C6 ================================
    def test_c6_suspended_tenant_is_still_blocked_before_any_reference_check(self):
        with db.engine.begin() as conn:
            conn.execute(db.text("UPDATE tenants SET status='suspended' WHERE id=:t"),
                         {"t": self.ta_id})
        db.session.remove()
        before = self.footprint()
        for response in (
            self.post_booking(self.hdr_a, customer_id=self.cust_a_id),
            self.post_booking(self.hdr_a, customer_id=self.cust_b_id),
            self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.member_a_id),
            self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.owner_b_id),
        ):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()["code"], "tenant_suspended")
        self.assertEqual(self.footprint(), before)
