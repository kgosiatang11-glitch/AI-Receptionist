"""Tests for tenant-facing member management (smartdesk/api/config_api.py:
POST/PATCH/DELETE /business/members).

Covers: owner-only access, adding an existing registered user by email
(never inventing an account or sending a fake invite), changing a member's
role, removing a member, and the two protections the task required:

* the last owner of a tenant can never be demoted or removed, so a tenant
  can never end up with zero owners;
* a manager/agent/viewer cannot manage the team at all -- only an owner can.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Membership, User  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402


class AddMemberTests(MultiTenantTestCase):
    def test_owner_can_add_an_existing_user_by_email(self):
        response = self.client.post(
            "/api/v1/business/members",
            headers=self.auth(self.padel_user, self.padel),
            json={"email": self.salon_user.email, "role": "manager"},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["role"], "manager")
        membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.salon_user.id
        ).one()
        self.assertEqual(membership.role, "manager")

    def test_adding_an_unregistered_email_is_a_clean_404_not_a_fake_invite(self):
        response = self.client.post(
            "/api/v1/business/members",
            headers=self.auth(self.padel_user, self.padel),
            json={"email": "nobody@example.com", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(User.query.filter_by(email="nobody@example.com").one_or_none())

    def test_cannot_add_the_same_person_twice(self):
        response = self.client.post(
            "/api/v1/business/members",
            headers=self.auth(self.padel_user, self.padel),
            json={"email": self.viewer_user.email, "role": "viewer"},
        )
        self.assertEqual(response.status_code, 409)

    def test_invalid_role_is_rejected(self):
        response = self.client.post(
            "/api/v1/business/members",
            headers=self.auth(self.padel_user, self.padel),
            json={"email": self.salon_user.email, "role": "superuser"},
        )
        self.assertEqual(response.status_code, 400)

    def test_manager_cannot_add_members(self):
        db.session.add(
            Membership(tenant_id=self.padel.id, user_id=self.salon_user.id, role="manager")
        )
        db.session.commit()
        response = self.client.post(
            "/api/v1/business/members",
            headers=self.auth(self.salon_user, self.padel),
            json={"email": self.viewer_user.email, "role": "viewer"},
        )
        self.assertEqual(response.status_code, 403)

    def test_a_business_member_cannot_reach_another_tenants_members(self):
        response = self.client.post(
            "/api/v1/business/members",
            headers=self.auth(self.padel_user, self.salon),
            json={"email": self.viewer_user.email, "role": "viewer"},
        )
        self.assertEqual(response.status_code, 403)


class UpdateMemberRoleTests(MultiTenantTestCase):
    def _viewer_membership_id(self):
        return Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).one().id

    def test_owner_can_change_a_members_role(self):
        membership_id = self._viewer_membership_id()
        response = self.client.patch(
            f"/api/v1/business/members/{membership_id}",
            headers=self.auth(self.padel_user, self.padel),
            json={"role": "manager"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.get(Membership, membership_id).role, "manager")

    def test_cannot_demote_the_last_owner(self):
        owner_membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.padel_user.id, role="owner"
        ).one()
        response = self.client.patch(
            f"/api/v1/business/members/{owner_membership.id}",
            headers=self.auth(self.padel_user, self.padel),
            json={"role": "manager"},
        )
        self.assertEqual(response.status_code, 400)
        db.session.refresh(owner_membership)
        self.assertEqual(owner_membership.role, "owner")

    def test_can_demote_an_owner_once_a_second_owner_exists(self):
        second_owner = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).one()
        second_owner.role = "owner"
        db.session.commit()
        owner_membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.padel_user.id, role="owner"
        ).one()

        response = self.client.patch(
            f"/api/v1/business/members/{owner_membership.id}",
            headers=self.auth(self.padel_user, self.padel),
            json={"role": "manager"},
        )
        self.assertEqual(response.status_code, 200)


class RemoveMemberTests(MultiTenantTestCase):
    def test_owner_can_remove_a_member(self):
        membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).one()
        response = self.client.delete(
            f"/api/v1/business/members/{membership.id}",
            headers=self.auth(self.padel_user, self.padel),
        )
        self.assertEqual(response.status_code, 204)
        self.assertIsNone(db.session.get(Membership, membership.id))

    def test_cannot_remove_the_last_owner(self):
        owner_membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.padel_user.id, role="owner"
        ).one()
        response = self.client.delete(
            f"/api/v1/business/members/{owner_membership.id}",
            headers=self.auth(self.padel_user, self.padel),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(db.session.get(Membership, owner_membership.id))

    def test_viewer_cannot_remove_members(self):
        membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).one()
        response = self.client.delete(
            f"/api/v1/business/members/{membership.id}",
            headers=self.auth(self.viewer_user, self.padel),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(db.session.get(Membership, membership.id))


if __name__ == "__main__":
    unittest.main()
