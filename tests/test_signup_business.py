"""Tests for Phase 2: self-service customer signup.

These are additive, like tests/test_signup_and_verification.py -- the
pre-existing suites are unmodified and must keep passing unchanged. This
file only covers the new ``POST /api/v1/signup/business`` endpoint:

* an authenticated user with no existing membership can create their own
  tenant and is granted the ``owner`` role on it automatically, with no
  Super Admin step involved;
* business_name, business_email, phone and location are all required, each
  missing on its own is a clean 400, not a crash;
* business_email is stored as the tenant's escalation contact and location
  is published as the tenant's "location" knowledge section -- both reuse
  existing columns/tables, no schema change;
* a user who already belongs to a tenant cannot self-signup a second one;
* two different new users get two completely separate, isolated tenants;
* the new tenant gets the same default rows (receptionist profile,
  automation placeholders) that the admin-created flow gives, since both
  now share ``smartdesk/services/tenant_provisioning.py``;
* an unauthenticated request is refused;
* the Super Admin manual-create fallback keeps working for platform staff
  and stays out of reach of ordinary tenant owners.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    AUTOMATION_KINDS,
    Automation,
    KnowledgeDocument,
    Membership,
    ReceptionistProfile,
    Tenant,
    User,
)
from tests.test_multitenant import MultiTenantTestCase, make_token  # noqa: E402


def _payload(**overrides) -> dict:
    """A complete, valid signup payload, with individual fields overridable
    (or removable, by passing ``None``) so each validation test only
    changes the one field it's exercising."""
    base = {
        "business_name": "New Co Cafe",
        "business_email": "hello@newco.test",
        "phone": "+26770009999",
        "location": "Plot 123, Francistown, Botswana",
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


class SelfServiceSignupTests(MultiTenantTestCase):
    def _new_user_headers(self, subject: str, email: str) -> dict:
        return {"Authorization": f"Bearer {make_token(subject, email)}"}

    def test_new_user_can_create_their_own_business(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-newco", "owner@newco.test"),
            json=_payload(),
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["role"], "owner")
        self.assertEqual(body["tenant"]["name"], "New Co Cafe")
        self.assertEqual(body["tenant"]["plan"], "basic")
        self.assertEqual(body["tenant"]["status"], "active")

        user = User.query.filter_by(supabase_user_id="sub-newco").one()
        tenant = Tenant.query.filter_by(id=body["tenant"]["id"]).one()
        membership = Membership.query.filter_by(
            tenant_id=tenant.id, user_id=user.id
        ).one()
        self.assertEqual(membership.role, "owner")
        # owner_email is always the caller's real, verified account email --
        # never overridable from the form.
        self.assertEqual(tenant.owner_email, "owner@newco.test")
        self.assertEqual(tenant.owner_phone, "+26770009999")
        self.assertEqual(tenant.escalation_email, "hello@newco.test")

        location_doc = KnowledgeDocument.query.filter_by(
            tenant_id=tenant.id, section="location"
        ).one()
        self.assertEqual(location_doc.body, "Plot 123, Francistown, Botswana")
        self.assertTrue(location_doc.is_published)

    def test_no_super_admin_step_required_to_reach_the_dashboard(self):
        """The whole point of Phase 2: /me shows the tenant immediately,
        with no admin "link owner" call in between."""
        self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-solo", "solo@solobiz.test"),
            json=_payload(business_name="Solo Biz"),
        )
        me = self.client.get(
            "/api/v1/me",
            headers=self._new_user_headers("sub-solo", "solo@solobiz.test"),
        )
        self.assertEqual(me.status_code, 200)
        tenants = me.get_json()["tenants"]
        self.assertEqual(len(tenants), 1)
        self.assertEqual(tenants[0]["name"], "Solo Biz")
        self.assertEqual(tenants[0]["role"], "owner")

    def test_business_name_is_required(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-blank", "blank@test.com"),
            json=_payload(business_name=None),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("business_name", response.get_json()["error"])

    def test_business_email_is_required(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-blank2", "blank2@test.com"),
            json=_payload(business_email=None),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("business_email", response.get_json()["error"])

    def test_phone_is_required(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-blank3", "blank3@test.com"),
            json=_payload(phone=None),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("phone", response.get_json()["error"])

    def test_location_is_required(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-blank4", "blank4@test.com"),
            json=_payload(location=None),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("location", response.get_json()["error"])

    def test_empty_body_reports_every_missing_field(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-empty", "empty@test.com"),
            json={},
        )
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        for field in ("business_name", "business_email", "phone", "location"):
            self.assertIn(field, error)

    def test_unauthenticated_request_is_refused(self):
        response = self.client.post(
            "/api/v1/signup/business", json=_payload(business_name="Nope Inc")
        )
        self.assertEqual(response.status_code, 401)

    def test_user_who_already_has_a_business_cannot_self_signup_again(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self.auth(self.padel_user),
            json=_payload(business_name="A Second Business"),
        )
        self.assertEqual(response.status_code, 409)
        # No stray tenant was created for the rejected attempt.
        self.assertIsNone(
            Tenant.query.filter_by(name="A Second Business").one_or_none()
        )

    def test_created_tenant_gets_the_same_defaults_as_admin_created_ones(self):
        response = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-defaults", "defaults@test.com"),
            json=_payload(business_name="Defaults Test Co"),
        )
        tenant_id = response.get_json()["tenant"]["id"]

        profile = ReceptionistProfile.query.filter_by(tenant_id=tenant_id).one()
        self.assertFalse(profile.sales_mode_enabled)

        automations = Automation.query.filter_by(tenant_id=tenant_id).all()
        self.assertEqual({a.kind for a in automations}, set(AUTOMATION_KINDS))

    def test_duplicate_business_names_get_distinct_slugs(self):
        first = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-dup1", "dup1@test.com"),
            json=_payload(business_name="Corner Store"),
        ).get_json()
        second = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-dup2", "dup2@test.com"),
            json=_payload(business_name="Corner Store"),
        ).get_json()
        self.assertNotEqual(first["tenant"]["slug"], second["tenant"]["slug"])

    def test_two_new_signups_are_fully_isolated_from_each_other(self):
        biz_a = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-biza", "owner@biza.test"),
            json=_payload(business_name="Business A"),
        ).get_json()["tenant"]
        biz_b = self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-bizb", "owner@bizb.test"),
            json=_payload(business_name="Business B"),
        ).get_json()["tenant"]

        # Business A cannot read Business B's data (and vice versa) even
        # when explicitly asking for the other tenant's id.
        resp_a_into_b = self.client.get(
            "/api/v1/business",
            headers={
                **self._new_user_headers("sub-biza", "owner@biza.test"),
                "X-Tenant-Id": biz_b["id"],
            },
        )
        self.assertEqual(resp_a_into_b.status_code, 403)

        resp_b_into_a = self.client.get(
            "/api/v1/business",
            headers={
                **self._new_user_headers("sub-bizb", "owner@bizb.test"),
                "X-Tenant-Id": biz_a["id"],
            },
        )
        self.assertEqual(resp_b_into_a.status_code, 403)

        # Each owner can still reach their own tenant's data.
        resp_a = self.client.get(
            "/api/v1/business",
            headers={
                **self._new_user_headers("sub-biza", "owner@biza.test"),
                "X-Tenant-Id": biz_a["id"],
            },
        )
        self.assertEqual(resp_a.status_code, 200)
        self.assertEqual(resp_a.get_json()["tenant"]["name"], "Business A")

    def test_tenant_owner_cannot_reach_super_admin_endpoints(self):
        self.client.post(
            "/api/v1/signup/business",
            headers=self._new_user_headers("sub-notadmin", "notadmin@test.com"),
            json=_payload(business_name="Not An Admin Inc"),
        )
        response = self.client.get(
            "/api/v1/admin/tenants",
            headers=self._new_user_headers("sub-notadmin", "notadmin@test.com"),
        )
        self.assertEqual(response.status_code, 403)

    def test_platform_admin_can_still_use_the_manual_admin_create_endpoint(self):
        """The Super Admin manual-create fallback (smartdesk/api/admin_api.py)
        is untouched by this phase and must keep working for platform staff."""
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={"business_name": "Staff-Provisioned Co"},
        )
        self.assertEqual(response.status_code, 201)

    def test_business_user_still_cannot_use_the_manual_admin_create_endpoint(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.padel_user),
            json={"business_name": "Should Not Work"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(
            Tenant.query.filter_by(name="Should Not Work").one_or_none()
        )


if __name__ == "__main__":
    unittest.main()
