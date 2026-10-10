"""Shared adversarial checks for C6 -- users, memberships and assignment.

A mixin, not a TestCase, so the same assertions run unchanged on SQLite
(``tests/test_c6.py``) and on real PostgreSQL (``tests/test_c6_postgres.py``,
where the composite FKs to ``memberships`` are real).  The concrete class
supplies ``self.ta`` / ``self.tb`` and calls ``build_c5_world()`` then
``build_c6_world()``.

Policy under test (see the C6 task):

* an assignee is an ACTIVE member of the row's tenant with role agent/owner;
* viewers, managers, non-members, platform admins who are not members, members of
  other tenants and deactivated users are refused -- all with one 404;
* removing/demoting/deactivating a member clears their assignments (owner<->agent
  keeps them);
* platform-admin takeover never writes the admin into ``assigned_user_id``;
* platform-admin access requires a verified email;
* membership is gained only by accepting an invitation;
* strict boolean parsing, last-owner protection, OAuth callback revalidation and
  malformed-UUID handling.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from smartdesk.extensions import db
from smartdesk.models import (
    AuditLog,
    CalendarConnection,
    Channel,
    Conversation,
    Invitation,
    Lead,
    Membership,
    Message,
    Tenant,
    User,
    utcnow,
)
from smartdesk.services import calendar as calendar_service
from tests.cross_tenant_checks import C5Fixtures
from tests.test_multitenant import make_token

IS_EMAIL_CONFIRMED = "smartdesk.security.rbac._is_email_confirmed"


class C6Fixtures(C5Fixtures):
    def build_c6_world(self) -> None:
        """Extend the C5 world.

        Tenant A: owner_a (owner), member_a (agent), viewer_a (viewer),
        manager_a (manager), inactive_a (agent, account deactivated).
        agent2 is an agent in BOTH tenants (to prove clearing is tenant-scoped).
        owner2_a and target exist as users but belong to no tenant.
        """
        mk = lambda email, **kw: User(  # noqa: E731
            supabase_user_id=str(uuid.uuid4()), email=email, **kw)
        self.viewer_a = mk("viewer@a.c6.test")
        self.manager_a = mk("manager@a.c6.test")
        self.agent2 = mk("agent2@ab.c6.test")
        self.inactive_a = mk("inactive@a.c6.test", is_active=False)
        self.owner2_a = mk("owner2@a.c6.test")
        self.target = mk("target@platform.c6.test", is_platform_admin=True)
        db.session.add_all([self.viewer_a, self.manager_a, self.agent2,
                            self.inactive_a, self.owner2_a, self.target])
        db.session.flush()
        db.session.add_all([
            Membership(tenant_id=self.ta_id, user_id=self.viewer_a.id, role="viewer"),
            Membership(tenant_id=self.ta_id, user_id=self.manager_a.id, role="manager"),
            Membership(tenant_id=self.ta_id, user_id=self.agent2.id, role="agent"),
            Membership(tenant_id=self.tb_id, user_id=self.agent2.id, role="agent"),
            Membership(tenant_id=self.ta_id, user_id=self.inactive_a.id, role="agent"),
        ])
        # owner_a must be tenant A's ONLY owner so last-owner rules are testable
        # (the base multi-tenant fixture also seeds a second owner).
        Membership.query.filter(
            Membership.tenant_id == self.ta_id, Membership.role == "owner",
            Membership.user_id != self.owner_a_id,
        ).delete(synchronize_session=False)
        self.chan_a = Channel(tenant_id=self.ta_id, kind="whatsapp", address="+26779990011")
        db.session.add(self.chan_a)
        db.session.flush()
        conv = db.session.get(Conversation, self.conv_a_id)
        conv.channel_id = self.chan_a.id
        conv.channel_kind = "whatsapp"
        db.session.commit()
        for name in ("viewer_a", "manager_a", "agent2", "inactive_a", "owner2_a", "target"):
            setattr(self, f"{name}_id", getattr(self, name).id)
        db.session.remove()

    # -- helpers ---------------------------------------------------------
    def client_(self):
        return self.app.test_client()

    def hdr(self, user_id_attr, tenant_id=None):
        user = db.session.get(User, getattr(self, user_id_attr))
        headers = self.headers(user, tenant_id)
        db.session.remove()
        return headers

    def membership_id(self, tenant_id, user_id) -> str:
        db.session.remove()
        value = Membership.query.filter_by(tenant_id=tenant_id, user_id=user_id).one().id
        db.session.remove()
        return value

    def add_membership(self, tenant_id, user_id, role) -> str:
        db.session.remove()
        membership = Membership(tenant_id=tenant_id, user_id=user_id, role=role)
        db.session.add(membership)
        db.session.commit()
        value = membership.id
        db.session.remove()
        return value

    def set_assignee(self, model, row_id, user_id) -> None:
        """Write an assignment directly (bypassing the API), as a fixture."""
        db.session.remove()
        row = db.session.get(model, row_id)
        row.assigned_user_id = user_id
        db.session.commit()
        db.session.remove()

    def assignee(self, model, row_id):
        db.session.remove()
        value = db.session.execute(
            db.select(model.assigned_user_id).where(model.id == row_id)
        ).scalar_one()
        db.session.remove()
        return value

    def role_of(self, tenant_id, user_id):
        db.session.remove()
        row = Membership.query.filter_by(tenant_id=tenant_id, user_id=user_id).one_or_none()
        value = row.role if row else None
        db.session.remove()
        return value

    def unknown_headers(self, email, subject=None) -> dict:
        return {"Authorization": f"Bearer {make_token(subject or str(uuid.uuid4()), email)}"}


class C6Checks(C6Fixtures):
    # ============================ ASSIGNMENT (1-6, 8) ====================
    def test_01_tenant_a_cannot_assign_tenant_b_user(self):
        response = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.owner_b_id)
        self.assert_rejected(response, "assigned_user_id")
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))

    def test_02_non_member_cannot_be_assigned(self):
        for outsider in (self.owner2_a_id, self.admin_id, self.target_id):
            with self.subTest(user=outsider):
                self.assert_rejected(
                    self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=outsider),
                    "assigned_user_id")
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))

    def test_03_inactive_member_cannot_be_assigned(self):
        self.assert_rejected(
            self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.inactive_a_id),
            "assigned_user_id")
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))

    def test_04_viewer_cannot_be_assigned_and_neither_is_manager(self):
        for user_id in (self.viewer_a_id, self.manager_a_id):
            with self.subTest(user=user_id):
                self.assert_rejected(
                    self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=user_id),
                    "assigned_user_id")
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))

    def test_05_active_agent_can_be_assigned(self):
        for user_id in (self.member_a_id, self.agent2_id):
            with self.subTest(user=user_id):
                ok = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=user_id)
                self.assertEqual(ok.status_code, 200, ok.get_data())
                self.assertEqual(self.assignee(Lead, self.lead_a_id), user_id)

    def test_06_active_owner_can_be_assigned(self):
        ok = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=self.owner_a_id)
        self.assertEqual(ok.status_code, 200, ok.get_data())
        self.assertEqual(self.assignee(Lead, self.lead_a_id), self.owner_a_id)

    def test_every_refusal_is_the_same_404(self):
        bodies = set()
        for user_id in (self.owner_b_id, self.viewer_a_id, self.inactive_a_id,
                        self.admin_id, str(uuid.uuid4())):
            r = self.patch_lead(self.hdr_a, self.lead_a_id, assigned_user_id=user_id)
            bodies.add((r.status_code, str(r.get_json())))
        self.assertEqual(len(bodies), 1, bodies)

    def test_08_malformed_uuid_paths_are_a_controlled_4xx(self):
        client = self.client_()
        admin = self.hdr_admin_a
        bad = ("not-a-uuid", "1' OR '1'='1", "%00", "x" * 200, "123")
        routes = (
            ("get", "/api/v1/conversations/{}", self.hdr_a),
            ("post", "/api/v1/conversations/{}/takeover", self.hdr_a),
            ("get", "/api/v1/customers/{}", self.hdr_a),
            ("patch", "/api/v1/leads/{}", self.hdr_a),
            ("post", "/api/v1/bookings/{}/status", self.hdr_a),
            ("patch", "/api/v1/business/members/{}", self.hdr_a),
            ("delete", "/api/v1/business/members/{}", self.hdr_a),
            ("delete", "/api/v1/business/invitations/{}", self.hdr_a),
            ("patch", "/api/v1/admin/users/{}", admin),
            ("get", "/api/v1/admin/tenants/{}", admin),
            ("post", "/api/v1/admin/tenants/{}/owner", admin),
        )
        for method, template, headers in routes:
            for value in bad:
                with self.subTest(route=template, value=value):
                    response = getattr(client, method)(
                        template.format(value), headers=headers, json={})
                    self.assertIn(response.status_code, (400, 404, 405), response.get_data())
                    self.assertLess(response.status_code, 500)

    def test_uppercase_uuid_path_is_normalised_not_rejected(self):
        response = self.client_().get(
            f"/api/v1/conversations/{self.conv_a_id.upper()}", headers=self.hdr_a)
        self.assertEqual(response.status_code, 200, response.get_data())

    # ========================= MEMBERSHIP LIFECYCLE (9-15) ==============
    def _assign_agent2_everywhere(self):
        self.set_assignee(Lead, self.lead_a_id, self.agent2_id)
        self.set_assignee(Conversation, self.conv_a_id, self.agent2_id)
        self.set_assignee(Lead, self.lead_b_id, self.agent2_id)
        self.set_assignee(Conversation, self.conv_b_id, self.agent2_id)

    def test_09_removing_a_member_clears_their_lead_assignments(self):
        self._assign_agent2_everywhere()
        r = self.client_().delete(
            f"/api/v1/business/members/{self.membership_id(self.ta_id, self.agent2_id)}",
            headers=self.hdr_a)
        self.assertEqual(r.status_code, 204, r.get_data())
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))
        # Tenant-scoped: the SAME user's assignments in tenant B are untouched.
        self.assertEqual(self.assignee(Lead, self.lead_b_id), self.agent2_id)

    def test_10_removing_a_member_clears_their_conversation_assignments(self):
        self._assign_agent2_everywhere()
        self.client_().delete(
            f"/api/v1/business/members/{self.membership_id(self.ta_id, self.agent2_id)}",
            headers=self.hdr_a)
        self.assertIsNone(self.assignee(Conversation, self.conv_a_id))
        self.assertEqual(self.assignee(Conversation, self.conv_b_id), self.agent2_id)

    def test_11_deactivating_a_user_clears_assignments_everywhere(self):
        self._assign_agent2_everywhere()
        r = self.client_().patch(
            f"/api/v1/admin/users/{self.agent2_id}", headers=self.hdr_admin_a,
            json={"is_active": False})
        self.assertEqual(r.status_code, 200, r.get_data())
        for model, row in ((Lead, self.lead_a_id), (Lead, self.lead_b_id),
                           (Conversation, self.conv_a_id), (Conversation, self.conv_b_id)):
            self.assertIsNone(self.assignee(model, row))

    def test_12_demoting_agent_to_viewer_clears_assignments(self):
        self._assign_agent2_everywhere()
        r = self.client_().patch(
            f"/api/v1/business/members/{self.membership_id(self.ta_id, self.agent2_id)}",
            headers=self.hdr_a, json={"role": "viewer"})
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))
        self.assertIsNone(self.assignee(Conversation, self.conv_a_id))
        self.assertEqual(self.assignee(Lead, self.lead_b_id), self.agent2_id)  # other tenant

    def test_demoting_to_manager_also_clears_because_manager_is_not_assignable(self):
        self._assign_agent2_everywhere()
        self.client_().patch(
            f"/api/v1/business/members/{self.membership_id(self.ta_id, self.agent2_id)}",
            headers=self.hdr_a, json={"role": "manager"})
        self.assertIsNone(self.assignee(Lead, self.lead_a_id))

    def test_13_owner_to_agent_preserves_assignments_and_back(self):
        mid = self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        self.set_assignee(Lead, self.lead_a_id, self.owner2_a_id)
        self.set_assignee(Conversation, self.conv_a_id, self.owner2_a_id)
        for new_role in ("agent", "owner"):
            with self.subTest(to=new_role):
                r = self.client_().patch(
                    f"/api/v1/business/members/{mid}", headers=self.hdr_a,
                    json={"role": new_role})
                self.assertEqual(r.status_code, 200, r.get_data())
                self.assertEqual(self.assignee(Lead, self.lead_a_id), self.owner2_a_id)
                self.assertEqual(self.assignee(Conversation, self.conv_a_id), self.owner2_a_id)

    def test_14_cannot_remove_or_demote_the_last_active_owner(self):
        mid = self.membership_id(self.ta_id, self.owner_a_id)
        for response in (
            self.client_().delete(f"/api/v1/business/members/{mid}", headers=self.hdr_a),
            self.client_().patch(f"/api/v1/business/members/{mid}", headers=self.hdr_a,
                                 json={"role": "agent"}),
        ):
            self.assertEqual(response.status_code, 400, response.get_data())
            self.assertEqual(response.get_json()["code"], "last_owner")
        self.assertEqual(self.role_of(self.ta_id, self.owner_a_id), "owner")

    def test_14_an_inactive_owner_does_not_count_as_an_owner(self):
        db.session.remove()
        db.session.get(User, self.owner2_a_id).is_active = False
        db.session.commit()
        db.session.remove()
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        mid = self.membership_id(self.ta_id, self.owner_a_id)
        r = self.client_().patch(f"/api/v1/business/members/{mid}", headers=self.hdr_a,
                                 json={"role": "agent"})
        self.assertEqual(r.status_code, 400, r.get_data())
        self.assertEqual(self.role_of(self.ta_id, self.owner_a_id), "owner")

    def test_14_a_second_active_owner_allows_the_change(self):
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        mid = self.membership_id(self.ta_id, self.owner_a_id)
        r = self.client_().delete(f"/api/v1/business/members/{mid}", headers=self.hdr_a)
        self.assertEqual(r.status_code, 204, r.get_data())

    def test_14_admin_cannot_deactivate_the_sole_active_owner(self):
        r = self.client_().patch(f"/api/v1/admin/users/{self.owner_a_id}",
                                 headers=self.hdr_admin_a, json={"is_active": False})
        self.assertEqual(r.status_code, 400, r.get_data())
        self.assertEqual(r.get_json()["code"], "last_owner")
        db.session.remove()
        self.assertTrue(db.session.get(User, self.owner_a_id).is_active)

    def test_14_admin_cannot_deactivate_an_owner_whose_co_owner_is_already_inactive(self):
        db.session.remove()
        db.session.get(User, self.owner2_a_id).is_active = False
        db.session.commit()
        db.session.remove()
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        r = self.client_().patch(f"/api/v1/admin/users/{self.owner_a_id}",
                                 headers=self.hdr_admin_a, json={"is_active": False})
        self.assertEqual(r.status_code, 400, r.get_data())

    def test_15_last_owner_check_reads_the_database_not_a_stale_session(self):
        """The check must see another request's committed change.  The session
        below believes a second owner exists; the database says otherwise."""
        from smartdesk.services import membership_lifecycle

        second = self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        db.session.remove()
        mine = Membership.query.filter_by(tenant_id=self.ta_id, user_id=self.owner_a_id).one()
        self.assertEqual(Membership.query.filter_by(id=second).one().role, "owner")  # cached
        with db.engine.begin() as conn:                      # "another request"
            conn.execute(db.text("DELETE FROM memberships WHERE id = :i"), {"i": second})
        with self.assertRaises(membership_lifecycle.LastOwnerError):
            membership_lifecycle.change_role(mine, "agent")
        db.session.rollback()
        self.assertEqual(self.role_of(self.ta_id, self.owner_a_id), "owner")

    # ============================ PLATFORM ADMIN (16-21) =================
    def test_16_unverified_platform_admin_cannot_use_admin_endpoints(self):
        paths = ("/api/v1/admin/overview", "/api/v1/admin/users", "/api/v1/admin/tenants",
                 "/api/v1/tenants", "/api/v1/audit-logs")
        with patch(IS_EMAIL_CONFIRMED, return_value=False):
            for path in paths:
                with self.subTest(path=path):
                    r = self.client_().get(path, headers=self.hdr_admin_a)
                    self.assertEqual(r.status_code, 403, r.get_data())
                    self.assertNotIn("code", r.get_json())
            r = self.client_().patch(f"/api/v1/admin/users/{self.target_id}",
                                     headers=self.hdr_admin_a, json={"is_active": False})
            self.assertEqual(r.status_code, 403)
        for path in paths:  # ...and the verified admin is unaffected
            self.assertEqual(self.client_().get(path, headers=self.hdr_admin_a).status_code, 200)

    def test_unverified_admin_message_is_the_same_as_for_a_non_admin(self):
        with patch(IS_EMAIL_CONFIRMED, return_value=False):
            unverified = self.client_().get("/api/v1/admin/overview", headers=self.hdr_admin_a)
        plain = self.client_().get("/api/v1/admin/overview", headers=self.hdr_a)
        self.assertEqual((unverified.status_code, unverified.get_json()),
                         (plain.status_code, plain.get_json()))

    def _takeover(self, headers, enabled=True):
        return self.client_().post(f"/api/v1/conversations/{self.conv_a_id}/takeover",
                                   headers=headers, json={"enabled": enabled})

    def test_18_non_member_platform_admin_takeover_does_not_populate_assignee(self):
        r = self._takeover(self.hdr_admin_a)
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertTrue(r.get_json()["human_takeover"])
        self.assertIsNone(self.assignee(Conversation, self.conv_a_id))

    def test_18_admin_takeover_preserves_an_existing_valid_assignment(self):
        self.set_assignee(Conversation, self.conv_a_id, self.member_a_id)
        self.assertEqual(self._takeover(self.hdr_admin_a).status_code, 200)
        self.assertEqual(self.assignee(Conversation, self.conv_a_id), self.member_a_id)

    def test_19_platform_admin_takeover_still_works_and_reply_is_authorised(self):
        fx = self.side_effects()
        self.assertEqual(self._takeover(self.hdr_admin_a).status_code, 200)
        r = self.client_().post(f"/api/v1/conversations/{self.conv_a_id}/reply",
                                headers=self.hdr_admin_a, json={"body": "Hello from support"})
        self.assertEqual(r.status_code, 201, r.get_data())
        fx["send"].assert_called_once()

    def test_20_release_still_works_for_admin_and_member(self):
        self._takeover(self.hdr_admin_a)
        r = self._takeover(self.hdr_admin_a, enabled=False)
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertFalse(r.get_json()["human_takeover"])
        self.assertIsNone(self.assignee(Conversation, self.conv_a_id))
        # a real member: takeover assigns them, release clears it
        self.assertEqual(self._takeover(self.hdr_a).status_code, 200)
        self.assertEqual(self.assignee(Conversation, self.conv_a_id), self.owner_a_id)
        self.assertEqual(self._takeover(self.hdr_a, enabled=False).status_code, 200)
        self.assertIsNone(self.assignee(Conversation, self.conv_a_id))

    def test_21_audit_and_timeline_record_the_admin_as_actor(self):
        self._takeover(self.hdr_admin_a)
        db.session.remove()
        audit = AuditLog.query.filter_by(action="conversation.takeover").one()
        self.assertEqual(audit.actor_user_id, self.admin_id)
        self.assertEqual(audit.actor_email, "admin@platform.c5.test")
        self.assertEqual(audit.tenant_id, self.ta_id)
        self.assertTrue(audit.meta["acting_as_platform_admin"])
        self.assertIsNone(audit.meta["assigned_user_id"])
        event = Message.query.filter_by(
            conversation_id=self.conv_a_id, event_type="takeover").one()
        self.assertEqual(event.role, "event")
        self.assertIn("admin@platform.c5.test", event.body)
        self.assertIn("SmartDesk support", event.body)
        self.assertEqual(event.meta["actor_user_id"], self.admin_id)
        self.assertTrue(event.meta["acting_as_platform_admin"])

    def test_member_takeover_is_audited_as_a_member_not_support(self):
        self._takeover(self.hdr_a)
        db.session.remove()
        audit = AuditLog.query.filter_by(action="conversation.takeover").one()
        self.assertFalse(audit.meta["acting_as_platform_admin"])
        self.assertEqual(audit.meta["assigned_user_id"], self.owner_a_id)

    def test_takeover_enabled_flag_is_parsed_strictly(self):
        self.assertEqual(self._takeover(self.hdr_a).status_code, 200)
        r = self.client_().post(f"/api/v1/conversations/{self.conv_a_id}/takeover",
                                headers=self.hdr_a, json={"enabled": "false"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["human_takeover"])    # "false" really means false
        r = self.client_().post(f"/api/v1/conversations/{self.conv_a_id}/takeover",
                                headers=self.hdr_a, json={"enabled": "maybe"})
        self.assertEqual(r.status_code, 400)

    # ============================ INVITATIONS (22-28) ====================
    def _invite(self, headers, email, role="agent", path="/api/v1/business/invitations"):
        return self.client_().post(path, headers=headers, json={"email": email, "role": role})

    def _accept(self, headers, code, **extra):
        return self.client_().post("/api/v1/invitations/accept", headers=headers,
                                   json={"invitation_code": code, **extra})

    def test_22_creating_an_invitation_does_not_add_the_user(self):
        db.session.remove()
        before = Membership.query.count()
        r = self._invite(self.hdr_a, "owner2@a.c6.test")          # a REGISTERED user
        self.assertEqual(r.status_code, 201, r.get_data())
        self.assertEqual(r.get_json()["invitation"]["status"], "pending")
        db.session.remove()
        self.assertEqual(Membership.query.count(), before)
        self.assertIsNone(self.role_of(self.ta_id, self.owner2_a_id))

    def test_23_responses_do_not_reveal_whether_an_email_is_registered(self):
        registered = self._invite(self.hdr_a, B_REGISTERED := "owner-b-secret@b.test")
        unknown = self._invite(self.hdr_a, "never-heard-of-them@nowhere.test")
        via_members = self._invite(self.hdr_a, "someone@nowhere.test",
                                   path="/api/v1/business/members")
        for r in (registered, unknown, via_members):
            self.assertEqual(r.status_code, 201, r.get_data())
        shape = lambda r: (r.status_code, sorted(r.get_json()), sorted(r.get_json()["invitation"]),  # noqa: E731
                           r.get_json()["note"], r.get_json()["invitation"]["status"])
        self.assertEqual(shape(registered), shape(unknown))
        self.assertEqual(shape(registered), shape(via_members))
        self.assertNotIn(b"Business B", registered.get_data())  # nothing about tenant B
        db.session.remove()
        self.assertIsNone(User.query.filter_by(email="never-heard-of-them@nowhere.test").one_or_none())

    def test_invitation_validation_and_permissions(self):
        self.assertEqual(self._invite(self.hdr_a, "x@y.test", role="superuser").status_code, 400)
        for bad in ("", "nope", None, 5, "a b@c.test", "@c.test", "a@b"):
            with self.subTest(email=bad):
                self.assertEqual(self._invite(self.hdr_a, bad).status_code, 400)
        agent = self.hdr("member_a_id", self.ta_id)
        self.assertEqual(self._invite(agent, "x@y.test").status_code, 403)
        self.assertEqual(
            self._invite(self.hdr_a, "owner@a.c5.test").status_code, 409)  # own-tenant member

    def test_token_is_stored_only_as_a_hash(self):
        r = self._invite(self.hdr_a, "hash@x.test")
        code = r.get_json()["invitation_code"]
        db.session.remove()
        row = Invitation.query.one()
        self.assertNotIn(code, (row.token_hash, row.email))
        self.assertEqual(len(row.token_hash), 64)
        self.assertNotIn(code, str(self.client_().get(
            "/api/v1/business/invitations", headers=self.hdr_a).get_json()))

    def test_24_intended_user_can_accept(self):
        email = "new.person@x.test"
        code = self._invite(self.hdr_a, email, "agent").get_json()["invitation_code"]
        r = self._accept(self.unknown_headers(email), code)
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertEqual(r.get_json()["role"], "agent")
        self.assertEqual(r.get_json()["tenant"]["id"], self.ta_id)
        db.session.remove()
        user = User.query.filter_by(email=email).one()
        self.assertEqual(self.role_of(self.ta_id, user.id), "agent")
        self.assertEqual(Invitation.query.one().status, "accepted")
        self.assertEqual(Invitation.query.one().accepted_by_user_id, user.id)
        self.assertEqual(AuditLog.query.filter_by(action="invitation.accept").count(), 1)

    def test_24_an_existing_registered_user_can_accept_too(self):
        code = self._invite(self.hdr_a, "owner2@a.c6.test", "owner").get_json()["invitation_code"]
        r = self._accept(self.hdr("owner2_a_id"), code)
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertEqual(self.role_of(self.ta_id, self.owner2_a_id), "owner")

    def test_25_wrong_user_cannot_accept_and_the_invitation_survives(self):
        code = self._invite(self.hdr_a, "intended@x.test").get_json()["invitation_code"]
        db.session.remove()
        before = Membership.query.filter_by(tenant_id=self.ta_id).count()
        db.session.remove()
        for wrong in (self.hdr("owner_b_id"), self.hdr("admin_id"),
                      self.unknown_headers("someone-else@x.test")):
            r = self._accept(wrong, code)
            self.assertEqual(r.status_code, 404, r.get_data())
        db.session.remove()
        self.assertEqual(Membership.query.filter_by(tenant_id=self.ta_id).count(), before)
        self.assertEqual(Invitation.query.one().status, "pending")
        self.assertEqual(self._accept(self.unknown_headers("intended@x.test"), code).status_code, 200)

    def test_25_unverified_email_cannot_accept(self):
        email = "unverified@x.test"
        code = self._invite(self.hdr_a, email).get_json()["invitation_code"]
        with patch(IS_EMAIL_CONFIRMED, return_value=False):
            r = self._accept(self.unknown_headers(email), code)
        self.assertEqual(r.status_code, 403, r.get_data())
        db.session.remove()
        self.assertEqual(Invitation.query.one().status, "pending")

    def test_26_expired_invitation_cannot_be_accepted(self):
        email = "late@x.test"
        code = self._invite(self.hdr_a, email).get_json()["invitation_code"]
        db.session.remove()
        Invitation.query.one().expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        db.session.remove()
        r = self._accept(self.unknown_headers(email), code)
        self.assertEqual(r.status_code, 404, r.get_data())
        self.assertEqual(r.get_json(), {"error": "This invitation is invalid or has expired."})
        db.session.remove()
        created = User.query.filter_by(email=email).one_or_none()
        if created is not None:
            self.assertEqual(Membership.query.filter_by(user_id=created.id).count(), 0)
        self.assertEqual(Invitation.query.one().status, "pending")

    def test_27_invitation_cannot_be_replayed(self):
        email = "once@x.test"
        code = self._invite(self.hdr_a, email).get_json()["invitation_code"]
        headers = self.unknown_headers(email)
        self.assertEqual(self._accept(headers, code).status_code, 200)
        self.assertEqual(self._accept(headers, code).status_code, 404)
        # ...even after the member is removed, the old code stays dead.
        db.session.remove()
        user = User.query.filter_by(email=email).one()
        mid = self.membership_id(self.ta_id, user.id)
        self.client_().delete(f"/api/v1/business/members/{mid}", headers=self.hdr_a)
        self.assertEqual(self._accept(headers, code).status_code, 404)
        self.assertIsNone(self.role_of(self.ta_id, user.id))

    def test_replayed_accept_cannot_change_an_existing_members_role(self):
        code = self._invite(self.hdr_a, "owner2@a.c6.test", "viewer").get_json()["invitation_code"]
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        r = self._accept(self.hdr("owner2_a_id"), code)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["role"], "owner")          # not lowered to viewer
        self.assertEqual(self.role_of(self.ta_id, self.owner2_a_id), "owner")

    def test_a_new_invitation_replaces_the_previous_one(self):
        email = "again@x.test"
        first = self._invite(self.hdr_a, email).get_json()["invitation_code"]
        second = self._invite(self.hdr_a, email).get_json()["invitation_code"]
        self.assertNotEqual(first, second)
        headers = self.unknown_headers(email)
        self.assertEqual(self._accept(headers, first).status_code, 404)
        self.assertEqual(self._accept(headers, second).status_code, 200)

    def test_28_cross_tenant_invitation_abuse_is_blocked(self):
        email = "victim@x.test"
        r = self._invite(self.hdr_a, email, "owner")
        code, inv_id = r.get_json()["invitation_code"], r.get_json()["invitation"]["id"]
        # Tenant B's owner can neither see nor revoke tenant A's invitation.
        listing = self.client_().get("/api/v1/business/invitations", headers=self.hdr_b)
        self.assertEqual(listing.get_json()["items"], [])
        revoke = self.client_().delete(f"/api/v1/business/invitations/{inv_id}", headers=self.hdr_b)
        self.assertEqual(revoke.status_code, 404)
        # Tenant B cannot invite INTO tenant A by naming it, nor via the header.
        sneaky = self.client_().post("/api/v1/business/invitations", headers=self.hdr_b,
                                     json={"email": email, "role": "owner", "tenant_id": self.ta_id})
        self.assertEqual(sneaky.status_code, 201)
        db.session.remove()
        self.assertEqual(Invitation.query.filter_by(tenant_id=self.ta_id).count(), 1)
        self.assertEqual(Invitation.query.filter_by(tenant_id=self.tb_id).count(), 1)
        # Claiming a different tenant while accepting does nothing: the tenant
        # comes from the invitation row.
        r = self._accept(self.unknown_headers(email), code, tenant_id=self.tb_id)
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertEqual(r.get_json()["tenant"]["id"], self.ta_id)
        db.session.remove()
        user = User.query.filter_by(email=email).one()
        self.assertEqual(self.role_of(self.ta_id, user.id), "owner")
        self.assertIsNone(self.role_of(self.tb_id, user.id))
        # A's invitation does not grant anything in B, and B's code is separate.
        self.assertEqual(Invitation.query.filter_by(tenant_id=self.tb_id).one().status, "pending")

    def test_revoked_invitation_cannot_be_accepted(self):
        email = "revoked@x.test"
        r = self._invite(self.hdr_a, email)
        code, inv_id = r.get_json()["invitation_code"], r.get_json()["invitation"]["id"]
        self.assertEqual(self.client_().delete(
            f"/api/v1/business/invitations/{inv_id}", headers=self.hdr_a).status_code, 204)
        self.assertEqual(self._accept(self.unknown_headers(email), code).status_code, 404)

    def test_invitation_to_a_suspended_tenant_cannot_be_accepted(self):
        email = "suspended@x.test"
        code = self._invite(self.hdr_a, email).get_json()["invitation_code"]
        with db.engine.begin() as conn:
            conn.execute(db.text("UPDATE tenants SET status='suspended' WHERE id=:t"),
                         {"t": self.ta_id})
        db.session.remove()
        self.assertEqual(self._accept(self.unknown_headers(email), code).status_code, 404)

    def test_garbage_codes_are_refused_uniformly(self):
        for bad in (None, "", "   ", 5, [], {"a": 1}, "a" * 5000, "' OR 1=1 --"):
            with self.subTest(code=bad):
                r = self.client_().post("/api/v1/invitations/accept",
                                        headers=self.unknown_headers("g@x.test"),
                                        json={"invitation_code": bad})
                self.assertEqual(r.status_code, 404, r.get_data())

    # ============================ ADMIN PATCH BOOLEANS (29-31) ===========
    def _patch_user(self, **payload):
        return self.client_().patch(f"/api/v1/admin/users/{self.target_id}",
                                    headers=self.hdr_admin_a, json=payload)

    def _target(self):
        db.session.remove()
        user = db.session.get(User, self.target_id)
        value = (user.is_platform_admin, user.is_active)
        db.session.remove()
        return value

    def test_29_string_false_is_false(self):
        r = self._patch_user(is_platform_admin="false")
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertEqual(self._target(), (False, True))
        r = self._patch_user(is_active="FALSE")
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertEqual(self._target(), (False, False))

    def test_30_string_true_is_true(self):
        self._patch_user(is_platform_admin=False)
        self.assertEqual(self._patch_user(is_platform_admin="true").status_code, 200)
        self.assertEqual(self._target(), (True, True))
        self._patch_user(is_active=False)
        self.assertEqual(self._patch_user(is_active=" True ").status_code, 200)
        self.assertEqual(self._target(), (True, True))

    def test_31_invalid_booleans_are_rejected_and_change_nothing(self):
        for bad in ("maybe", "yes", "no", "", "0", "1", 0, 1, 2, None, [], {}, ["false"], 1.0):
            for field in ("is_platform_admin", "is_active"):
                with self.subTest(field=field, value=bad):
                    r = self._patch_user(**{field: bad})
                    self.assertEqual(r.status_code, 400, r.get_data())
                    self.assertEqual(r.get_json()["field"], field)
        self.assertEqual(self._target(), (True, True))
        # One bad value among good ones changes NOTHING.
        r = self._patch_user(is_platform_admin=False, is_active="banana")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._target(), (True, True))

    # ============================ CALENDAR OAUTH (32-35) =================
    def _configure_google(self):
        self.app.config["GOOGLE_CLIENT_ID"] = "id"
        self.app.config["GOOGLE_CLIENT_SECRET"] = "secret"
        self.app.config["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost/cb"

    def _callback(self, state):
        """Run the callback with Google mocked; returns (response, flow_mock)."""
        self._configure_google()
        credentials = MagicMock(token="tok", refresh_token="ref", expiry=None)
        flow = MagicMock(credentials=credentials)
        with patch("google_auth_oauthlib.flow.Flow.from_client_config",
                   return_value=flow) as factory, \
                patch("smartdesk.services.calendar._fetch_account_email",
                      return_value="cal@a.test"):
            response = self.client_().get(f"/calendar/oauth/callback?code=c&state={state}")
        return response, factory

    def _connections(self):
        db.session.remove()
        n = CalendarConnection.query.filter_by(tenant_id=self.ta_id).count()
        db.session.remove()
        return n

    def test_35_legitimate_owner_still_connects_and_is_recorded(self):
        state = calendar_service.sign_state(self.ta_id, self.owner_a_id)
        response, factory = self._callback(state)
        self.assertIn("calendar=connected", response.headers["Location"])
        factory.assert_called_once()
        db.session.remove()
        connection = CalendarConnection.query.filter_by(tenant_id=self.ta_id).one()
        self.assertEqual(connection.connected_by_user_id, self.owner_a_id)

    def test_35_platform_admin_support_connection_is_allowed(self):
        state = calendar_service.sign_state(self.ta_id, self.admin_id)
        response, _ = self._callback(state)
        self.assertIn("calendar=connected", response.headers["Location"])

    def test_32_removed_member_cannot_complete_the_callback(self):
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        state = calendar_service.sign_state(self.ta_id, self.owner2_a_id)
        with db.engine.begin() as conn:
            conn.execute(db.text("DELETE FROM memberships WHERE user_id = :u AND tenant_id = :t"),
                         {"u": self.owner2_a_id, "t": self.ta_id})
        db.session.remove()
        response, factory = self._callback(state)
        self.assertIn("calendar=error", response.headers["Location"])
        factory.assert_not_called()                 # no code exchange
        self.assertEqual(self._connections(), 0)

    def test_32_demoted_owner_cannot_complete_the_callback(self):
        mid = self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        state = calendar_service.sign_state(self.ta_id, self.owner2_a_id)
        self.client_().patch(f"/api/v1/business/members/{mid}", headers=self.hdr_a,
                             json={"role": "viewer"})
        response, factory = self._callback(state)
        self.assertIn("calendar=error", response.headers["Location"])
        factory.assert_not_called()
        self.assertEqual(self._connections(), 0)

    def test_33_inactive_user_cannot_complete_the_callback(self):
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        state = calendar_service.sign_state(self.ta_id, self.owner2_a_id)
        db.session.remove()
        db.session.get(User, self.owner2_a_id).is_active = False
        db.session.commit()
        db.session.remove()
        response, factory = self._callback(state)
        self.assertIn("calendar=error", response.headers["Location"])
        factory.assert_not_called()
        self.assertEqual(self._connections(), 0)

    def test_34_a_deleted_user_is_handled_safely(self):
        self.add_membership(self.ta_id, self.owner2_a_id, "owner")
        state = calendar_service.sign_state(self.ta_id, self.owner2_a_id)
        db.session.remove()
        db.session.delete(db.session.get(User, self.owner2_a_id))
        db.session.commit()
        db.session.remove()
        response, factory = self._callback(state)
        self.assertEqual(response.status_code, 302)            # not a 500
        self.assertIn("calendar=error", response.headers["Location"])
        factory.assert_not_called()
        self.assertEqual(self._connections(), 0)

    def test_35_a_state_cannot_be_replayed(self):
        state = calendar_service.sign_state(self.ta_id, self.owner_a_id)
        first, factory = self._callback(state)
        self.assertIn("calendar=connected", first.headers["Location"])
        with db.engine.begin() as conn:
            conn.execute(db.text("DELETE FROM calendar_connections"))
        second, factory2 = self._callback(state)
        self.assertIn("calendar=expired", second.headers["Location"])
        factory2.assert_not_called()
        self.assertEqual(self._connections(), 0)

    def test_35_starting_a_new_flow_supersedes_the_old_state(self):
        old = calendar_service.sign_state(self.ta_id, self.owner_a_id)
        new = calendar_service.sign_state(self.ta_id, self.owner_a_id)
        stale, factory = self._callback(old)
        self.assertIn("calendar=expired", stale.headers["Location"])
        factory.assert_not_called()
        fresh, _ = self._callback(new)
        self.assertIn("calendar=connected", fresh.headers["Location"])

    def test_35_a_state_without_a_nonce_is_refused(self):
        import base64, hashlib, hmac, json, time  # noqa: E401

        self._configure_google()
        body = base64.urlsafe_b64encode(json.dumps(
            {"tenant_id": self.ta_id, "user_id": self.owner_a_id,
             "issued_at": time.time()}).encode()).decode("ascii")
        sig = hmac.new(calendar_service._state_secret(), body.encode("ascii"),
                       hashlib.sha256).hexdigest()
        response, factory = self._callback(f"{body}.{sig}")
        self.assertIn("calendar=expired", response.headers["Location"])
        factory.assert_not_called()

    def test_oauth_state_is_not_exposed_through_the_tenant_api(self):
        calendar_service.sign_state(self.ta_id, self.owner_a_id)
        for path in ("/api/v1/business", "/api/v1/me", "/api/v1/overview"):
            body = self.client_().get(path, headers=self.hdr_a).get_data(as_text=True)
            self.assertNotIn("nonce", body)
            self.assertNotIn("calendar_oauth_pending", body)

    # ============================ C5 stays intact ========================
    def test_c5_customer_reference_checks_are_unchanged(self):
        r = self.post_booking(self.hdr_a, customer_id=self.cust_b_id)
        self.assert_rejected(r, "customer_id")
