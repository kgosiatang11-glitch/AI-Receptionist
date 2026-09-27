"""Tests for the platform-wide Users API (smartdesk/api/admin_api.py).

Covers: business-user rejection, platform-admin listing/search, promotion
and demotion of platform-admin status, activation/deactivation, and the
three protections the task explicitly required:

* a platform admin cannot remove their own platform-admin access or
  deactivate their own account (self-lockout);
* a user cannot be deactivated while they are the sole ``owner`` of a
  tenant (last-owner protection);
* a platform admin with zero tenant memberships can still reach every
  platform endpoint (the Control Center must not depend on membership).

Also locks in that a deactivated user is actually refused at the
authentication layer, not just flagged cosmetically in the database.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Membership, User  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase, make_token  # noqa: E402


class UserListingTests(MultiTenantTestCase):
    def test_business_user_cannot_list_users(self):
        response = self.client.get("/api/v1/admin/users", headers=self.auth(self.padel_user))
        self.assertEqual(response.status_code, 403)

    def test_platform_admin_can_list_users(self):
        response = self.client.get("/api/v1/admin/users", headers=self.auth(self.admin_user))
        self.assertEqual(response.status_code, 200)
        emails = {u["email"] for u in response.get_json()["items"]}
        self.assertIn(self.padel_user.email, emails)
        self.assertIn(self.salon_user.email, emails)

    def test_listing_includes_each_users_memberships_and_role(self):
        response = self.client.get("/api/v1/admin/users", headers=self.auth(self.admin_user))
        body = response.get_json()
        padel_row = next(u for u in body["items"] if u["email"] == self.padel_user.email)
        self.assertEqual(
            padel_row["memberships"],
            [{"tenant_id": self.padel.id, "tenant_name": self.padel.name, "role": "owner"}],
        )

    def test_search_filters_by_email(self):
        response = self.client.get(
            "/api/v1/admin/users?q=salon", headers=self.auth(self.admin_user)
        )
        emails = {u["email"] for u in response.get_json()["items"]}
        self.assertEqual(emails, {self.salon_user.email})

    def test_admin_with_zero_memberships_can_still_list_users(self):
        """The Control Center must not depend on the caller having any
        tenant membership -- a freshly promoted admin has none."""
        self.assertEqual(
            Membership.query.filter_by(user_id=self.admin_user.id).count(), 0
        )
        response = self.client.get("/api/v1/admin/users", headers=self.auth(self.admin_user))
        self.assertEqual(response.status_code, 200)


class PromoteDemoteTests(MultiTenantTestCase):
    def test_business_user_cannot_promote_themselves(self):
        response = self.client.patch(
            f"/api/v1/admin/users/{self.padel_user.id}",
            headers=self.auth(self.padel_user),
            json={"is_platform_admin": True},
        )
        self.assertEqual(response.status_code, 403)
        db.session.refresh(self.padel_user)
        self.assertFalse(self.padel_user.is_platform_admin)

    def test_platform_admin_can_promote_another_user(self):
        response = self.client.patch(
            f"/api/v1/admin/users/{self.padel_user.id}",
            headers=self.auth(self.admin_user),
            json={"is_platform_admin": True},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["is_platform_admin"])
        db.session.refresh(self.padel_user)
        self.assertTrue(self.padel_user.is_platform_admin)

    def test_platform_admin_cannot_demote_themselves(self):
        """Self-lockout protection: the caller must not be able to strip
        their own platform-admin access, which could leave nobody able to
        grant it back."""
        response = self.client.patch(
            f"/api/v1/admin/users/{self.admin_user.id}",
            headers=self.auth(self.admin_user),
            json={"is_platform_admin": False},
        )
        self.assertEqual(response.status_code, 400)
        db.session.refresh(self.admin_user)
        self.assertTrue(self.admin_user.is_platform_admin)

    def test_platform_admin_can_demote_someone_else(self):
        self.admin_user.is_platform_admin = True
        other = User(supabase_user_id="sub-other-admin", email="other-admin@smartdesk.ai",
                     is_platform_admin=True)
        db.session.add(other)
        db.session.commit()

        response = self.client.patch(
            f"/api/v1/admin/users/{other.id}",
            headers=self.auth(self.admin_user),
            json={"is_platform_admin": False},
        )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(other)
        self.assertFalse(other.is_platform_admin)


class ActivateDeactivateTests(MultiTenantTestCase):
    def test_platform_admin_cannot_deactivate_themselves(self):
        response = self.client.patch(
            f"/api/v1/admin/users/{self.admin_user.id}",
            headers=self.auth(self.admin_user),
            json={"is_active": False},
        )
        self.assertEqual(response.status_code, 400)
        db.session.refresh(self.admin_user)
        self.assertTrue(self.admin_user.is_active)

    def test_cannot_deactivate_the_sole_owner_of_a_tenant(self):
        response = self.client.patch(
            f"/api/v1/admin/users/{self.padel_user.id}",
            headers=self.auth(self.admin_user),
            json={"is_active": False},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(self.padel.name, response.get_json()["error"])
        db.session.refresh(self.padel_user)
        self.assertTrue(self.padel_user.is_active)

    def test_can_deactivate_a_tenant_owner_once_another_owner_exists(self):
        membership = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).one()
        membership.role = "owner"
        db.session.commit()

        response = self.client.patch(
            f"/api/v1/admin/users/{self.padel_user.id}",
            headers=self.auth(self.admin_user),
            json={"is_active": False},
        )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.padel_user)
        self.assertFalse(self.padel_user.is_active)

    def test_can_deactivate_a_non_owner(self):
        response = self.client.patch(
            f"/api/v1/admin/users/{self.viewer_user.id}",
            headers=self.auth(self.admin_user),
            json={"is_active": False},
        )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.viewer_user)
        self.assertFalse(self.viewer_user.is_active)

    def test_deactivated_user_is_refused_at_the_authentication_layer(self):
        """This is the real enforcement point: is_active is not merely a
        cosmetic flag. A deactivated user's token must be rejected before
        any tenant or admin data is ever touched."""
        self.viewer_user.is_active = False
        db.session.commit()

        response = self.client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {make_token(self.viewer_user.supabase_user_id, self.viewer_user.email)}"
            },
        )
        self.assertEqual(response.status_code, 403)

        # Also refused on a tenant-scoped, data-bearing route -- not just /me.
        response = self.client.get(
            "/api/v1/conversations",
            headers={
                "Authorization": f"Bearer {make_token(self.viewer_user.supabase_user_id, self.viewer_user.email)}",
                "X-Tenant-Id": self.padel.id,
            },
        )
        self.assertEqual(response.status_code, 403)


class ReceptionistsAndChannelsRollupTests(MultiTenantTestCase):
    def test_business_user_cannot_list_platform_receptionists(self):
        response = self.client.get(
            "/api/v1/admin/receptionists", headers=self.auth(self.padel_user)
        )
        self.assertEqual(response.status_code, 403)

    def test_platform_admin_sees_every_tenants_receptionist(self):
        response = self.client.get(
            "/api/v1/admin/receptionists", headers=self.auth(self.admin_user)
        )
        self.assertEqual(response.status_code, 200)
        names = {row["tenant_name"] for row in response.get_json()["items"]}
        self.assertIn(self.padel.name, names)
        self.assertIn(self.salon.name, names)

    def test_platform_admin_sees_every_tenants_channels(self):
        response = self.client.get(
            "/api/v1/admin/channels", headers=self.auth(self.admin_user)
        )
        self.assertEqual(response.status_code, 200)
        tenants = {row["tenant_name"] for row in response.get_json()["items"]}
        self.assertIn(self.padel.name, tenants)


if __name__ == "__main__":
    unittest.main()
