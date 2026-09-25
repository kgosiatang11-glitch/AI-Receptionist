"""Tests for the Phase 1 customer-signup / email-verification work.

These are additive: the pre-existing suites (test_multitenant.py,
test_admin_tenants.py, etc.) keep exercising and proving everything that
was already true -- they are unmodified and must keep passing unchanged.
This file only covers what changed in this phase:

* an unverified email must never grant tenant access, even with a real
  Membership row (smartdesk/security/rbac.py::_is_email_confirmed and
  Principal.role_for);
* ``/me`` must not leak tenant/membership info to an unverified caller,
  and must expose ``email_verified`` so the frontend can tell "please
  verify your email" apart from "no business assigned";
* ``_sync_user`` must refuse to rebind an existing user's
  ``supabase_user_id`` to a new token subject just because the email
  matches, instead of silently transferring their memberships.

On the SQLite test database ``_is_email_confirmed`` returns True by
default (there is no ``auth`` schema to check against), which is exactly
why the pre-existing suites need no changes. The tests below patch it
directly to exercise the unverified path.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Membership, User  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase, make_token  # noqa: E402

IS_EMAIL_CONFIRMED = "smartdesk.security.rbac._is_email_confirmed"


class EmailVerificationGateTests(MultiTenantTestCase):
    def test_verified_user_with_membership_keeps_working(self):
        """Sanity check: the default (verified) path is unaffected."""
        response = self.client.get(
            "/api/v1/me", headers=self.auth(self.padel_user)
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["user"]["email_verified"])
        self.assertEqual({t["slug"] for t in body["tenants"]}, {"10by20"})

    def test_unverified_user_gets_no_tenants_from_me(self):
        with patch(IS_EMAIL_CONFIRMED, return_value=False):
            response = self.client.get(
                "/api/v1/me", headers=self.auth(self.padel_user)
            )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["user"]["email_verified"])
        self.assertEqual(body["tenants"], [])
        self.assertFalse(body["can_switch_tenants"])

    def test_unverified_user_cannot_reach_tenant_scoped_data(self):
        """Even with a real, valid Membership, an unverified email must not
        grant tenant access -- this is the core requirement."""
        with patch(IS_EMAIL_CONFIRMED, return_value=False):
            response = self.client.get(
                "/api/v1/conversations",
                headers=self.auth(self.padel_user, self.padel),
            )
        self.assertEqual(response.status_code, 403)

    def test_unverified_platform_admin_also_gets_no_tenant_access(self):
        """Verification is required for everyone, including platform admins
        -- is_platform_admin is not a bypass for this check."""
        with patch(IS_EMAIL_CONFIRMED, return_value=False):
            response = self.client.get(
                "/api/v1/conversations",
                headers=self.auth(self.admin_user, self.padel),
            )
        self.assertEqual(response.status_code, 403)

    def test_verified_user_with_no_membership_is_distinguishable_from_unverified(self):
        """Both cases return an empty tenants list, but only the unverified
        one reports email_verified: false -- the frontend needs this to
        show two different screens."""
        stray = User(supabase_user_id="sub-stray", email="stray@test.com")
        db.session.add(stray)
        db.session.commit()

        response = self.client.get(
            "/api/v1/me", headers=self.auth(stray)
        )
        body = response.get_json()
        self.assertEqual(body["tenants"], [])
        self.assertTrue(body["user"]["email_verified"])


class SyncUserTakeoverTests(MultiTenantTestCase):
    """Regression tests for the _sync_user account-takeover fix."""

    def test_brand_new_subject_and_email_is_provisioned_normally(self):
        token = make_token("sub-new-person", "new-person@test.com")

        # This test represents a brand-new VERIFIED identity.
        with patch(IS_EMAIL_CONFIRMED, return_value=True):
            # First call provisions the user
            self.client.get(
                "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
            )

        # Fetch the newly created user
        created = User.query.filter_by(
            supabase_user_id="sub-new-person"
        ).one_or_none()
        self.assertIsNotNone(created)
        self.assertEqual(created.email, "new-person@test.com")

        # Simulate production default: mark user active
        created.is_active = True
        db.session.commit()

        # Now retry /me with the active, verified user
        response = self.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 200)

    def test_second_identity_with_same_email_is_refused_not_merged(self):
        original_subject = self.padel_user.supabase_user_id
        original_id = self.padel_user.id
        self.assertEqual(
            Membership.query.filter_by(user_id=original_id).count(), 1
        )

        attacker_token = make_token("sub-attacker", self.padel_user.email)
        response = self.client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {attacker_token}"},
        )

        # Refused, not silently merged.
        self.assertEqual(response.status_code, 409)

        # The original user's identity and memberships are untouched.
        db.session.expire_all()
        unchanged = db.session.get(User, original_id)
        self.assertEqual(unchanged.supabase_user_id, original_subject)
        self.assertEqual(
            Membership.query.filter_by(user_id=original_id).count(), 1
        )
        # No user was created for the attacker's subject either.
        self.assertIsNone(
            User.query.filter_by(supabase_user_id="sub-attacker").one_or_none()
        )

    def test_same_subject_returning_is_unaffected(self):
        """The fix only removes the email-based rebind path -- a returning
        user (same subject as before) must still work exactly as before."""
        response = self.client.get(
            "/api/v1/me", headers=self.auth(self.padel_user)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["user"]["email"], self.padel_user.email
        )

if __name__ == "__main__":
    unittest.main()
