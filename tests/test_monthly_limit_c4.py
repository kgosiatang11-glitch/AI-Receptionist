"""Phase 3.1 -- C4: a tenant owner must not control their own monthly limit.

Before this change an owner could ``PATCH /api/v1/business`` with
``monthly_conversation_limit`` set to 0 (which enforcement treats as "no
limit") or to 1,000,000,000, and it was stored.  The limit is now changeable
only by a platform admin through
``PATCH /api/v1/admin/tenants/<tenant_id>/monthly-limit``.

Everything here goes through the real application, real JWT authentication,
real role checks and the real database session -- nothing about authorization
is mocked.  Database assertions use ``db.session.expire_all()`` so they read
what was actually persisted, not a cached Python object.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.api.admin_api import (  # noqa: E402
    MAX_MONTHLY_CONVERSATION_LIMIT,
    MIN_MONTHLY_CONVERSATION_LIMIT,
)
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import AuditLog, Tenant  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402

PADEL_LIMIT = 123
SALON_LIMIT = 456


class _LimitCase(MultiTenantTestCase):
    """Gives the two business tenants distinct, recognisable limits so a
    change that lands on the wrong tenant (or on a default) is visible."""

    def setUp(self) -> None:
        super().setUp()
        self.padel.monthly_conversation_limit = PADEL_LIMIT
        self.salon.monthly_conversation_limit = SALON_LIMIT
        db.session.commit()

    # -- helpers ----------------------------------------------------------

    def limit_of(self, tenant: Tenant) -> int:
        db.session.expire_all()
        return db.session.get(Tenant, tenant.id).monthly_conversation_limit

    def audit_rows(self, action: str) -> list[AuditLog]:
        db.session.expire_all()
        return AuditLog.query.filter_by(action=action).all()

    def owner_patch(self, payload, user=None, tenant=None):
        user = user or self.padel_user
        tenant = tenant or self.padel
        return self.client.patch(
            "/api/v1/business", headers=self.auth(user, tenant), json=payload
        )

    def admin_set(self, tenant: Tenant, payload, user=None):
        return self.client.patch(
            f"/api/v1/admin/tenants/{tenant.id}/monthly-limit",
            headers=self.auth(user or self.admin_user),
            json=payload,
        )


# ---------------------------------------------------------------------------
# Owner
# ---------------------------------------------------------------------------


class OwnerCannotChangeLimitTests(_LimitCase):
    def test_owner_can_read_current_limit(self):
        response = self.client.get(
            "/api/v1/business", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["tenant"]["monthly_conversation_limit"], PADEL_LIMIT
        )

    def test_owner_cannot_set_limit_to_zero(self):
        response = self.owner_patch({"monthly_conversation_limit": 0})
        self.assertEqual(response.status_code, 403)
        self.assertIn("monthly_conversation_limit", response.get_json()["error"])
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_owner_cannot_set_huge_limit(self):
        response = self.owner_patch({"monthly_conversation_limit": 1_000_000_000})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_owner_cannot_set_an_ordinary_limit(self):
        for value in (1, 500, PADEL_LIMIT + 1, 100_000):
            with self.subTest(value=value):
                response = self.owner_patch({"monthly_conversation_limit": value})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_owner_cannot_set_limit_with_odd_value_types(self):
        for value in (None, "0", "999999", -1, 12.5, True, [], {}):
            with self.subTest(value=value):
                response = self.owner_patch({"monthly_conversation_limit": value})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_rejection_is_all_or_nothing(self):
        """A request that also carries allowed fields is refused whole, so a
        client cannot get a partial update by bundling the forbidden field."""
        original_name = self.padel.name
        response = self.owner_patch(
            {"name": "Renamed By Attacker", "monthly_conversation_limit": 0}
        )
        self.assertEqual(response.status_code, 403)
        db.session.expire_all()
        self.assertEqual(db.session.get(Tenant, self.padel.id).name, original_name)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_failed_owner_attempt_writes_nothing(self):
        before_audit = AuditLog.query.count()
        self.owner_patch({"monthly_conversation_limit": 0})
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)
        self.assertEqual(AuditLog.query.count(), before_audit)

    def test_owner_can_still_update_allowed_settings_and_limit_is_untouched(self):
        response = self.owner_patch(
            {
                "name": "10by20 Padel (renamed)",
                "timezone": "Africa/Johannesburg",
                "escalation_email": "front-desk@10by20.test",
                "escalation_whatsapp": "+26771234567",
            }
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all()
        tenant = db.session.get(Tenant, self.padel.id)
        self.assertEqual(tenant.name, "10by20 Padel (renamed)")
        self.assertEqual(tenant.timezone, "Africa/Johannesburg")
        self.assertEqual(tenant.escalation_email, "front-desk@10by20.test")
        self.assertEqual(tenant.escalation_whatsapp, "+26771234567")
        self.assertEqual(tenant.monthly_conversation_limit, PADEL_LIMIT)

    def test_existing_unlimited_value_is_preserved_not_normalised(self):
        """Existing data is never rewritten: a legacy 0 stays 0 until a
        platform admin deliberately changes it."""
        self.padel.monthly_conversation_limit = 0
        db.session.commit()
        response = self.owner_patch({"name": "Still Zero"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.limit_of(self.padel), 0)

    def test_alternate_fields_and_nested_payloads_cannot_change_limit_or_plan(self):
        response = self.owner_patch(
            {
                "limit": 0,
                "monthly_limit": 0,
                "conversation_limit": 10**9,
                "usage_limit": 0,
                "plan": "enterprise",
                "settings": {"monthly_conversation_limit": 0},
                "settings.monthly_conversation_limit": 0,
                "tenant": {"monthly_conversation_limit": 0, "plan": "enterprise"},
                "is_internal": True,
            }
        )
        # Unknown keys are never written (the owner endpoint uses an
        # allow-list), so the request is harmless; what matters is state.
        self.assertIn(response.status_code, (200, 400, 403))
        db.session.expire_all()
        tenant = db.session.get(Tenant, self.padel.id)
        self.assertEqual(tenant.monthly_conversation_limit, PADEL_LIMIT)
        self.assertEqual(tenant.plan, "basic")
        self.assertFalse(tenant.is_internal)
        self.assertEqual(tenant.settings or {}, {})

    def test_viewer_cannot_use_the_business_endpoint_at_all(self):
        response = self.owner_patch(
            {"monthly_conversation_limit": 0}, user=self.viewer_user
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_unauthenticated_request_is_refused(self):
        response = self.client.patch(
            "/api/v1/business", json={"monthly_conversation_limit": 0}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)


class TenantIsolationTests(_LimitCase):
    def test_owner_of_a_cannot_change_limit_of_b_via_business_endpoint(self):
        # Pointing the tenant header at someone else's tenant is refused by
        # the membership check, and the limit field is refused regardless.
        response = self.owner_patch(
            {"monthly_conversation_limit": 10**9},
            user=self.padel_user,
            tenant=self.salon,
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(self.limit_of(self.salon), SALON_LIMIT)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_owner_of_a_cannot_call_the_admin_endpoint_for_b(self):
        response = self.admin_set(
            self.salon, {"monthly_conversation_limit": 10}, user=self.padel_user
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.limit_of(self.salon), SALON_LIMIT)

    def test_owner_cannot_call_the_admin_endpoint_for_their_own_tenant(self):
        response = self.admin_set(
            self.padel, {"monthly_conversation_limit": 99_999}, user=self.padel_user
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)
        self.assertEqual(self.audit_rows("tenant.monthly_limit.update"), [])

    def test_owner_cannot_use_the_generic_admin_tenant_patch(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}",
            headers=self.auth(self.padel_user, self.padel),
            json={"monthly_conversation_limit": 0},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_body_cannot_redirect_the_admin_change_to_another_tenant(self):
        response = self.admin_set(
            self.padel,
            {
                "monthly_conversation_limit": 321,
                "tenant_id": self.salon.id,
                "id": self.salon.id,
                "slug": self.salon.slug,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.limit_of(self.padel), 321)
        self.assertEqual(self.limit_of(self.salon), SALON_LIMIT)


# ---------------------------------------------------------------------------
# Platform admin
# ---------------------------------------------------------------------------


class PlatformAdminLimitTests(_LimitCase):
    def test_platform_admin_can_change_a_tenants_limit(self):
        response = self.admin_set(self.padel, {"monthly_conversation_limit": 750})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertEqual(body["id"], self.padel.id)
        self.assertEqual(body["slug"], self.padel.slug)
        self.assertEqual(body["monthly_conversation_limit"], 750)
        self.assertEqual(body["previous_monthly_conversation_limit"], PADEL_LIMIT)

    def test_new_value_is_persisted_and_only_that_tenant_changes(self):
        self.admin_set(self.padel, {"monthly_conversation_limit": 750})
        self.assertEqual(self.limit_of(self.padel), 750)
        self.assertEqual(self.limit_of(self.salon), SALON_LIMIT)

    def test_owner_sees_the_admin_set_value_when_reading(self):
        self.admin_set(self.padel, {"monthly_conversation_limit": 750})
        response = self.client.get(
            "/api/v1/business", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(
            response.get_json()["tenant"]["monthly_conversation_limit"], 750
        )

    def test_old_and_new_values_are_audited(self):
        self.admin_set(self.padel, {"monthly_conversation_limit": 750})
        rows = self.audit_rows("tenant.monthly_limit.update")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.tenant_id, self.padel.id)
        self.assertEqual(row.object_type, "tenant")
        self.assertEqual(row.object_id, self.padel.id)
        self.assertEqual(row.actor_email, self.admin_user.email)
        self.assertEqual(row.meta["old_monthly_conversation_limit"], PADEL_LIMIT)
        self.assertEqual(row.meta["new_monthly_conversation_limit"], 750)

    def test_each_change_gets_its_own_audit_row(self):
        self.admin_set(self.padel, {"monthly_conversation_limit": 750})
        self.admin_set(self.padel, {"monthly_conversation_limit": 900})
        rows = sorted(
            self.audit_rows("tenant.monthly_limit.update"),
            key=lambda r: r.meta["new_monthly_conversation_limit"],
        )
        self.assertEqual(
            [(r.meta["old_monthly_conversation_limit"],
              r.meta["new_monthly_conversation_limit"]) for r in rows],
            [(PADEL_LIMIT, 750), (750, 900)],
        )

    def test_admin_can_replace_a_legacy_zero_with_a_real_limit(self):
        self.padel.monthly_conversation_limit = 0
        db.session.commit()
        response = self.admin_set(self.padel, {"monthly_conversation_limit": 500})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.limit_of(self.padel), 500)
        row = self.audit_rows("tenant.monthly_limit.update")[0]
        self.assertEqual(row.meta["old_monthly_conversation_limit"], 0)

    def test_negative_value_is_rejected(self):
        for value in (-1, -500, -(10**9)):
            with self.subTest(value=value):
                response = self.admin_set(self.padel, {"monthly_conversation_limit": value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_zero_is_rejected_and_never_means_unlimited(self):
        response = self.admin_set(self.padel, {"monthly_conversation_limit": 0})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_excessively_large_value_is_rejected(self):
        for value in (MAX_MONTHLY_CONVERSATION_LIMIT + 1, 10**9, 2**31, 2**63):
            with self.subTest(value=value):
                response = self.admin_set(self.padel, {"monthly_conversation_limit": value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_range_boundaries_are_inclusive(self):
        for value in (MIN_MONTHLY_CONVERSATION_LIMIT, MAX_MONTHLY_CONVERSATION_LIMIT):
            with self.subTest(value=value):
                response = self.admin_set(self.padel, {"monthly_conversation_limit": value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.limit_of(self.padel), value)

    def test_only_genuine_integers_are_accepted(self):
        for value in (None, True, False, "100", "", 12.5, 100.0, [], {}):
            with self.subTest(value=value):
                response = self.admin_set(self.padel, {"monthly_conversation_limit": value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_missing_field_or_bad_body_is_rejected(self):
        for payload in ({}, {"limit": 100}, [], "100", None):
            with self.subTest(payload=payload):
                response = self.admin_set(self.padel, payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_rejected_changes_leave_no_audit_trail_entry(self):
        for value in (0, -1, MAX_MONTHLY_CONVERSATION_LIMIT + 1, "5"):
            self.admin_set(self.padel, {"monthly_conversation_limit": value})
        self.assertEqual(self.audit_rows("tenant.monthly_limit.update"), [])

    def test_unknown_tenant_is_404(self):
        response = self.client.patch(
            "/api/v1/admin/tenants/00000000-0000-0000-0000-000000000000/monthly-limit",
            headers=self.auth(self.admin_user),
            json={"monthly_conversation_limit": 100},
        )
        self.assertEqual(response.status_code, 404)

    def test_generic_admin_tenant_patch_does_not_silently_accept_the_limit(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}",
            headers=self.auth(self.admin_user),
            json={"monthly_conversation_limit": 0},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_generic_admin_tenant_patch_still_works_for_other_fields(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}",
            headers=self.auth(self.admin_user),
            json={"plan": "professional"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all()
        tenant = db.session.get(Tenant, self.padel.id)
        self.assertEqual(tenant.plan, "professional")
        # Changing the plan does not touch the limit in this phase.
        self.assertEqual(tenant.monthly_conversation_limit, PADEL_LIMIT)

    def test_non_platform_admin_receives_403(self):
        for user in (self.padel_user, self.salon_user, self.viewer_user):
            with self.subTest(user=user.email):
                response = self.admin_set(
                    self.padel, {"monthly_conversation_limit": 100}, user=user
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)

    def test_unauthenticated_request_is_401(self):
        response = self.client.patch(
            f"/api/v1/admin/tenants/{self.padel.id}/monthly-limit",
            json={"monthly_conversation_limit": 100},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.limit_of(self.padel), PADEL_LIMIT)


if __name__ == "__main__":
    unittest.main()
