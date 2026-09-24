"""Tests for the Super Admin tenant-management API (smartdesk/api/admin_api.py).

Covers every scenario explicitly required: business-user rejection at every
admin endpoint, platform-admin success, owner linking via the existing
Membership model (not a new auth system), and that a newly linked owner can
reach only their own tenant -- reusing the same isolation guarantees already
proven in test_multitenant.py, now exercised through the new endpoints.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Automation,
    KnowledgeDocument,
    Membership,
    ReceptionistProfile,
    Tenant,
    User,
)
from tests.test_multitenant import MultiTenantTestCase, make_token  # noqa: E402


class TenantCreationTests(MultiTenantTestCase):
    def test_business_user_cannot_create_a_tenant(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.padel_user),
            json={"business_name": "Rogue Tenant"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(Tenant.query.filter_by(name="Rogue Tenant").one_or_none())

    def test_platform_admin_can_create_a_tenant(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={
                "business_name": "New Salon Two",
                "business_category": "beauty",
                "owner_name": "Jane Doe",
                "owner_email": "jane@example.com",
                "phone": "+26770009999",
                "plan": "professional",
                "status": "active",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json()
        self.assertEqual(body["name"], "New Salon Two")
        self.assertEqual(body["plan"], "professional")
        self.assertEqual(body["owner_email"], "jane@example.com")
        self.assertFalse(body["owner_account"]["linked"])

    def test_created_tenant_gets_a_real_receptionist_profile(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={"business_name": "Profile Check Co"},
        )
        tenant_id = response.get_json()["id"]
        profile = ReceptionistProfile.query.filter_by(tenant_id=tenant_id).one()
        self.assertFalse(profile.sales_mode_enabled)  # never SmartDesk's persona

    def test_created_tenant_gets_blank_unpublished_knowledge_sections(self):
        """No business facts are ever invented for a brand-new tenant."""
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={"business_name": "Blank Knowledge Co"},
        )
        tenant_id = response.get_json()["id"]
        docs = KnowledgeDocument.query.filter_by(tenant_id=tenant_id).all()
        self.assertTrue(len(docs) > 0)
        self.assertTrue(all(not d.is_published for d in docs))

    def test_created_tenant_gets_automation_placeholders(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={"business_name": "Automations Check Co"},
        )
        tenant_id = response.get_json()["id"]
        self.assertTrue(Automation.query.filter_by(tenant_id=tenant_id).count() > 0)

    def test_business_name_is_required(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={"owner_email": "x@example.com"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_plan_is_rejected(self):
        response = self.client.post(
            "/api/v1/admin/tenants",
            headers=self.auth(self.admin_user),
            json={"business_name": "Bad Plan Co", "plan": "gold-tier"},
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_business_names_get_distinct_slugs(self):
        r1 = self.client.post(
            "/api/v1/admin/tenants", headers=self.auth(self.admin_user),
            json={"business_name": "Duplicate Co"},
        )
        r2 = self.client.post(
            "/api/v1/admin/tenants", headers=self.auth(self.admin_user),
            json={"business_name": "Duplicate Co"},
        )
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        t1 = db.session.get(Tenant, r1.get_json()["id"])
        t2 = db.session.get(Tenant, r2.get_json()["id"])
        self.assertNotEqual(t1.slug, t2.slug)


class TenantListAndDetailTests(MultiTenantTestCase):
    def test_business_user_cannot_list_all_tenants(self):
        response = self.client.get(
            "/api/v1/admin/tenants", headers=self.auth(self.padel_user)
        )
        self.assertEqual(response.status_code, 403)

    def test_platform_admin_sees_existing_fixture_tenants(self):
        """Confirms existing tenants (10by20/salon/smartdesk equivalents in
        the fixtures) appear correctly -- nothing duplicated, nothing lost."""
        response = self.client.get(
            "/api/v1/admin/tenants", headers=self.auth(self.admin_user)
        )
        self.assertEqual(response.status_code, 200)
        slugs = {item["slug"] for item in response.get_json()["items"]}
        self.assertIn("10by20", slugs)
        self.assertIn("salon", slugs)
        self.assertIn("smartdesk", slugs)

    def test_detail_includes_receptionist_and_channel_status(self):
        response = self.client.get(
            f"/api/v1/admin/tenants/{self.padel.id}", headers=self.auth(self.admin_user)
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("receptionist", body)
        self.assertIn("channels", body)
        self.assertTrue(body["channels"]["whatsapp"])  # padel fixture has a channel

    def test_business_user_cannot_view_tenant_detail(self):
        response = self.client.get(
            f"/api/v1/admin/tenants/{self.padel.id}", headers=self.auth(self.padel_user)
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_tenant_id_is_a_clean_404(self):
        response = self.client.get(
            "/api/v1/admin/tenants/00000000-0000-0000-0000-000000000000",
            headers=self.auth(self.admin_user),
        )
        self.assertEqual(response.status_code, 404)


class TenantUpdateTests(MultiTenantTestCase):
    def test_business_user_cannot_edit_another_tenant(self):
        """Explicit scenario from the spec: Tenant A cannot edit Tenant B."""
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.salon.id}",
            headers=self.auth(self.padel_user),
            json={"status": "suspended"},
        )
        self.assertEqual(response.status_code, 403)
        db.session.refresh(self.salon)
        self.assertEqual(self.salon.status, "active")

    def test_platform_admin_can_activate_and_deactivate(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}",
            headers=self.auth(self.admin_user),
            json={"status": "suspended"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "suspended")

    def test_invalid_status_is_rejected(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}",
            headers=self.auth(self.admin_user),
            json={"status": "on-fire"},
        )
        self.assertEqual(response.status_code, 400)

    def test_can_update_plan(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}",
            headers=self.auth(self.admin_user),
            json={"plan": "enterprise"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["plan"], "enterprise")


class OwnerLinkingTests(MultiTenantTestCase):
    def test_business_user_cannot_link_an_owner(self):
        response = self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.padel_user),
            json={"email": "someone@example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_linking_an_unregistered_email_is_a_clear_404_not_a_crash(self):
        response = self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.admin_user),
            json={"email": "nobody-has-signed-up@example.com"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("sign up", response.get_json()["error"])

    def test_platform_admin_can_link_an_existing_user_as_owner(self):
        new_user = User(supabase_user_id="sub-new-owner", email="newowner@example.com")
        db.session.add(new_user)
        db.session.commit()

        response = self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.admin_user),
            json={"email": "newowner@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["owner_account"]["linked"])

        membership = Membership.query.filter_by(
            tenant_id=self.salon.id, user_id=new_user.id
        ).one()
        self.assertEqual(membership.role, "owner")

    def test_linking_reuses_the_existing_membership_mechanism_not_a_new_one(self):
        """viewer_user already has a viewer membership on padel (fixtures);
        linking them as owner must UPDATE that row, not create a duplicate."""
        response = self.client.post(
            f"/api/v1/admin/tenants/{self.padel.id}/owner",
            headers=self.auth(self.admin_user),
            json={"email": self.viewer_user.email},
        )
        self.assertEqual(response.status_code, 200)
        memberships = Membership.query.filter_by(
            tenant_id=self.padel.id, user_id=self.viewer_user.id
        ).all()
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].role, "owner")

    def test_relinking_the_same_owner_is_idempotent(self):
        new_user = User(supabase_user_id="sub-idempotent", email="idempotent@example.com")
        db.session.add(new_user)
        db.session.commit()

        first = self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.admin_user), json={"email": "idempotent@example.com"},
        )
        second = self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.admin_user), json={"email": "idempotent@example.com"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            Membership.query.filter_by(
                tenant_id=self.salon.id, user_id=new_user.id
            ).count(),
            1,
        )

    def test_linked_owner_can_access_only_their_own_tenant(self):
        """The full loop: link an owner, then prove THEY are isolated
        exactly like every other business user already is."""
        new_user = User(supabase_user_id="sub-scoped-owner", email="scoped@example.com")
        db.session.add(new_user)
        db.session.commit()

        self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.admin_user), json={"email": "scoped@example.com"},
        )

        token = make_token("sub-scoped-owner", "scoped@example.com")
        own_tenant = self.client.get(
            "/api/v1/conversations",
            headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": self.salon.id},
        )
        other_tenant = self.client.get(
            "/api/v1/conversations",
            headers={"Authorization": f"Bearer {token}", "X-Tenant-Id": self.padel.id},
        )
        self.assertEqual(own_tenant.status_code, 200)
        self.assertEqual(other_tenant.status_code, 403)

    def test_newly_linked_owner_cannot_reach_admin_endpoints(self):
        new_user = User(supabase_user_id="sub-not-admin", email="notadmin@example.com")
        db.session.add(new_user)
        db.session.commit()
        self.client.post(
            f"/api/v1/admin/tenants/{self.salon.id}/owner",
            headers=self.auth(self.admin_user), json={"email": "notadmin@example.com"},
        )
        token = make_token("sub-not-admin", "notadmin@example.com")
        response = self.client.get(
            "/api/v1/admin/tenants", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 403)


class PlatformOverviewTests(MultiTenantTestCase):
    def test_business_user_cannot_access_platform_stats(self):
        response = self.client.get(
            "/api/v1/admin/overview", headers=self.auth(self.padel_user)
        )
        self.assertEqual(response.status_code, 403)

    def test_platform_admin_gets_real_counts(self):
        response = self.client.get(
            "/api/v1/admin/overview", headers=self.auth(self.admin_user)
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        # Fixtures create 3 tenants (padel, salon, smartdesk), all active.
        self.assertEqual(body["total_tenants"], 3)
        self.assertEqual(body["active_tenants"], 3)
        self.assertEqual(body["inactive_tenants"], 0)
        self.assertIsInstance(body["total_conversations"], int)

    def test_overview_reflects_a_deactivated_tenant(self):
        self.padel.status = "suspended"
        db.session.commit()
        response = self.client.get(
            "/api/v1/admin/overview", headers=self.auth(self.admin_user)
        )
        body = response.get_json()
        self.assertEqual(body["active_tenants"], 2)
        self.assertEqual(body["inactive_tenants"], 1)


if __name__ == "__main__":
    unittest.main()
