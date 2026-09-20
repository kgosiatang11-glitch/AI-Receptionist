"""Tests for the multi-tenant foundation.

These cover the properties that must never regress:

* a user of one tenant cannot read another tenant's data, even when they
  supply the other tenant's id explicitly;
* inbound traffic resolves to a tenant via the destination number, and is
  refused rather than misrouted when it does not;
* O'Brien's configuration is per tenant, so SmartDesk's sales behaviour does
  not apply to a client tenant;
* unauthenticated and under-privileged requests fail at the API, not just in
  the UI.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

import jwt  # noqa: E402

from ai.persona import TenantPersona  # noqa: E402
from smartdesk.app import create_app  # noqa: E402
from smartdesk.config import TestConfig  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Channel,
    Conversation,
    Customer,
    Lead,
    Membership,
    Message,
    ReceptionistProfile,
    Tenant,
    User,
)
from smartdesk.tenancy import (  # noqa: E402
    TenantResolutionError,
    normalize_address,
    resolve_channel,
)

SECRET = "test-secret-value"


class SQLiteTestConfig(TestConfig):
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SUPABASE_JWT_SECRET = SECRET
    SUPABASE_JWKS_URL = ""
    PLATFORM_ADMIN_EMAILS = ("admin@smartdesk.ai",)
    TWILIO_VALIDATE_SIGNATURE = False
    OPENAI_API_KEY = ""
    SQLALCHEMY_ENGINE_OPTIONS = {}


def make_token(subject: str, email: str) -> str:
    return jwt.encode(
        {"sub": subject, "email": email, "aud": "authenticated", "exp": 9999999999},
        SECRET,
        algorithm="HS256",
    )


class MultiTenantTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(SQLiteTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self._build_fixtures()

    def tearDown(self) -> None:
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _build_fixtures(self) -> None:
        self.padel = Tenant(slug="10by20", name="10by20 Padel Club",
                            business_type="sports")
        self.salon = Tenant(slug="salon", name="Salon (name pending)",
                            business_type="beauty")
        self.smartdesk = Tenant(slug="smartdesk", name="SmartDesk AI",
                                business_type="technology", is_internal=True)
        db.session.add_all([self.padel, self.salon, self.smartdesk])
        db.session.flush()

        db.session.add_all([
            ReceptionistProfile(tenant_id=self.padel.id, sales_mode_enabled=False),
            ReceptionistProfile(tenant_id=self.salon.id, sales_mode_enabled=False),
            ReceptionistProfile(tenant_id=self.smartdesk.id, sales_mode_enabled=True),
            Channel(tenant_id=self.padel.id, kind="whatsapp", address="+26770000001"),
            Channel(tenant_id=self.salon.id, kind="whatsapp", address="+26770000002"),
            Channel(tenant_id=self.padel.id, kind="voice", address="+26770000003"),
        ])

        # One customer per tenant, deliberately sharing a phone number to prove
        # that a shared customer does not create a shared record.
        self.padel_customer = Customer(tenant_id=self.padel.id, phone="+26771111111",
                                       full_name="Padel Customer")
        self.salon_customer = Customer(tenant_id=self.salon.id, phone="+26771111111",
                                       full_name="Salon Customer")
        db.session.add_all([self.padel_customer, self.salon_customer])
        db.session.flush()

        self.padel_conversation = Conversation(
            tenant_id=self.padel.id, session_key="whatsapp:+26771111111",
            customer_id=self.padel_customer.id, last_message_preview="Padel secret",
        )
        self.salon_conversation = Conversation(
            tenant_id=self.salon.id, session_key="whatsapp:+26771111111",
            customer_id=self.salon_customer.id, last_message_preview="Salon secret",
        )
        db.session.add_all([self.padel_conversation, self.salon_conversation])
        db.session.flush()
        db.session.add_all([
            Message(tenant_id=self.padel.id,
                    conversation_id=self.padel_conversation.id,
                    role="customer", body="Padel secret"),
            Message(tenant_id=self.salon.id,
                    conversation_id=self.salon_conversation.id,
                    role="customer", body="Salon secret"),
            Lead(tenant_id=self.padel.id, customer_id=self.padel_customer.id,
                 interest="Court hire"),
            Lead(tenant_id=self.salon.id, customer_id=self.salon_customer.id,
                 interest="Hair appointment"),
        ])

        self.padel_user = User(supabase_user_id="sub-padel",
                               email="owner@10by20.test")
        self.salon_user = User(supabase_user_id="sub-salon",
                               email="owner@salon.test")
        self.viewer_user = User(supabase_user_id="sub-viewer",
                                email="viewer@10by20.test")
        self.admin_user = User(supabase_user_id="sub-admin",
                               email="admin@smartdesk.ai", is_platform_admin=True)
        db.session.add_all([self.padel_user, self.salon_user, self.viewer_user,
                            self.admin_user])
        db.session.flush()
        db.session.add_all([
            Membership(tenant_id=self.padel.id, user_id=self.padel_user.id,
                       role="owner"),
            Membership(tenant_id=self.salon.id, user_id=self.salon_user.id,
                       role="owner"),
            Membership(tenant_id=self.padel.id, user_id=self.viewer_user.id,
                       role="viewer"),
        ])
        db.session.commit()

    def auth(self, user: User, tenant: Tenant | None = None) -> dict:
        headers = {"Authorization": f"Bearer {make_token(user.supabase_user_id, user.email)}"}
        if tenant is not None:
            headers["X-Tenant-Id"] = tenant.id
        return headers


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class AuthenticationTests(MultiTenantTestCase):
    def test_api_requires_a_token(self):
        self.assertEqual(self.client.get("/api/v1/conversations").status_code, 401)

    def test_garbage_token_is_rejected(self):
        response = self.client.get(
            "/api/v1/me", headers={"Authorization": "Bearer not-a-token"}
        )
        self.assertEqual(response.status_code, 401)

    def test_token_signed_with_wrong_secret_is_rejected(self):
        forged = jwt.encode(
            {"sub": "sub-padel", "email": "owner@10by20.test",
             "aud": "authenticated", "exp": 9999999999},
            "attacker-secret",
            algorithm="HS256",
        )
        response = self.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {forged}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_valid_token_returns_only_the_users_own_tenants(self):
        response = self.client.get("/api/v1/me", headers=self.auth(self.padel_user))
        self.assertEqual(response.status_code, 200)
        slugs = {t["slug"] for t in response.get_json()["tenants"]}
        self.assertEqual(slugs, {"10by20"})

    def test_platform_admin_sees_every_tenant(self):
        response = self.client.get("/api/v1/me", headers=self.auth(self.admin_user))
        slugs = {t["slug"] for t in response.get_json()["tenants"]}
        self.assertEqual(slugs, {"10by20", "salon", "smartdesk"})
        self.assertTrue(response.get_json()["can_switch_tenants"])

    def test_platform_admin_status_comes_from_database_not_token(self):
        """A user cannot escalate by putting a claim in their own token."""
        token = jwt.encode(
            {"sub": "sub-padel", "email": "owner@10by20.test", "aud": "authenticated",
             "exp": 9999999999, "is_platform_admin": True, "role": "service_role"},
            SECRET, algorithm="HS256",
        )
        response = self.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertFalse(response.get_json()["user"]["is_platform_admin"])


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TenantIsolationTests(MultiTenantTestCase):
    def test_conversations_are_scoped_to_the_active_tenant(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.padel_user, self.padel)
        )
        previews = [c["last_message_preview"] for c in response.get_json()["items"]]
        self.assertEqual(previews, ["Padel secret"])
        self.assertNotIn("Salon secret", previews)

    def test_supplying_another_tenants_id_is_refused(self):
        """The core guarantee: 10by20 data cannot appear in the salon dashboard."""
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.salon_user, self.padel)
        )
        self.assertEqual(response.status_code, 403)

    def test_customers_are_isolated_despite_a_shared_phone_number(self):
        response = self.client.get(
            "/api/v1/customers", headers=self.auth(self.salon_user, self.salon)
        )
        names = [c["full_name"] for c in response.get_json()["items"]]
        self.assertEqual(names, ["Salon Customer"])

    def test_direct_object_reference_across_tenants_is_not_found(self):
        """Guessing another tenant's record id must not return it."""
        response = self.client.get(
            f"/api/v1/customers/{self.padel_customer.id}",
            headers=self.auth(self.salon_user, self.salon),
        )
        self.assertEqual(response.status_code, 404)

    def test_conversation_detail_across_tenants_is_not_found(self):
        response = self.client.get(
            f"/api/v1/conversations/{self.padel_conversation.id}",
            headers=self.auth(self.salon_user, self.salon),
        )
        self.assertEqual(response.status_code, 404)

    def test_leads_are_isolated(self):
        response = self.client.get(
            "/api/v1/leads", headers=self.auth(self.padel_user, self.padel)
        )
        interests = [l["interest"] for l in response.get_json()["items"]]
        self.assertEqual(interests, ["Court hire"])

    def test_overview_counts_only_the_active_tenant(self):
        response = self.client.get(
            "/api/v1/overview", headers=self.auth(self.salon_user, self.salon)
        )
        self.assertEqual(response.get_json()["metrics"]["conversations_month"], 1)

    def test_platform_admin_switching_tenants_sees_each_separately(self):
        padel = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.admin_user, self.padel)
        ).get_json()["items"]
        salon = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.admin_user, self.salon)
        ).get_json()["items"]
        self.assertEqual([c["last_message_preview"] for c in padel], ["Padel secret"])
        self.assertEqual([c["last_message_preview"] for c in salon], ["Salon secret"])


# ---------------------------------------------------------------------------
# Authorization (roles)
# ---------------------------------------------------------------------------


class AuthorizationTests(MultiTenantTestCase):
    def test_viewer_may_read(self):
        response = self.client.get(
            "/api/v1/conversations", headers=self.auth(self.viewer_user, self.padel)
        )
        self.assertEqual(response.status_code, 200)

    def test_viewer_may_not_take_over_a_conversation(self):
        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/takeover",
            headers=self.auth(self.viewer_user, self.padel),
            json={"enabled": True},
        )
        self.assertEqual(response.status_code, 403)

    def test_viewer_may_not_edit_knowledge(self):
        response = self.client.put(
            "/api/v1/knowledge/pricing",
            headers=self.auth(self.viewer_user, self.padel),
            json={"body": "Free"},
        )
        self.assertEqual(response.status_code, 403)

    def test_owner_may_edit_knowledge(self):
        response = self.client.put(
            "/api/v1/knowledge/pricing",
            headers=self.auth(self.padel_user, self.padel),
            json={"body": "P150 per court hour"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["body"], "P150 per court hour")

    def test_business_owner_cannot_enable_sales_mode(self):
        """A client tenant must not be able to adopt SmartDesk's sales persona."""
        response = self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.padel_user, self.padel),
            json={"sales_mode_enabled": True},
        )
        self.assertEqual(response.status_code, 403)

    def test_business_user_cannot_list_all_tenants(self):
        response = self.client.get("/api/v1/tenants",
                                   headers=self.auth(self.padel_user))
        self.assertEqual(response.status_code, 403)

    def test_platform_admin_can_list_all_tenants(self):
        response = self.client.get("/api/v1/tenants",
                                   headers=self.auth(self.admin_user))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


class TenantResolutionTests(MultiTenantTestCase):
    def test_normalize_address_strips_channel_prefix_and_formatting(self):
        self.assertEqual(normalize_address("whatsapp:+26770000001"), "+26770000001")
        self.assertEqual(normalize_address("+267 7000 0001"), "+26770000001")
        self.assertEqual(normalize_address("26770000001"), "+26770000001")

    def test_destination_number_resolves_to_the_owning_tenant(self):
        channel = resolve_channel("whatsapp", "whatsapp:+26770000001")
        self.assertEqual(channel.tenant_id, self.padel.id)

    def test_a_different_number_resolves_to_a_different_tenant(self):
        channel = resolve_channel("whatsapp", "whatsapp:+26770000002")
        self.assertEqual(channel.tenant_id, self.salon.id)

    def test_unknown_number_is_refused_not_defaulted(self):
        """Refusing is correct: answering would use the wrong business's facts."""
        with self.assertRaises(TenantResolutionError):
            resolve_channel("whatsapp", "whatsapp:+26779999999")

    def test_channel_kind_is_part_of_resolution(self):
        with self.assertRaises(TenantResolutionError):
            resolve_channel("voice", "whatsapp:+26770000001")

    def test_webhook_routes_message_to_the_correct_tenant(self):
        response = self.client.post(
            "/whatsapp",
            data={"From": "whatsapp:+26775555555", "To": "whatsapp:+26770000002",
                  "Body": "Hello", "MessageSid": "SM1"},
        )
        self.assertEqual(response.status_code, 200)
        created = Conversation.query.filter_by(
            session_key="whatsapp:+26775555555"
        ).all()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].tenant_id, self.salon.id)

    def test_webhook_for_unregistered_number_creates_nothing(self):
        before = Conversation.query.count()
        response = self.client.post(
            "/whatsapp",
            data={"From": "whatsapp:+26776666666", "To": "whatsapp:+26778888888",
                  "Body": "Hello"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Conversation.query.count(), before)


# ---------------------------------------------------------------------------
# Tenant-specific O'Brien configuration
# ---------------------------------------------------------------------------


class TenantPersonaTests(MultiTenantTestCase):
    def test_client_tenant_persona_does_not_reference_smartdesk(self):
        from smartdesk.services.knowledge import build_persona

        persona = build_persona(self.padel)
        self.assertEqual(persona.business_name, "10by20 Padel Club")
        self.assertFalse(persona.sales_mode_enabled)
        self.assertNotIn("SmartDesk", persona.resolved_handoff_message())
        self.assertNotIn("SmartDesk", persona.resolved_setswana_greeting())

    def test_smartdesk_tenant_keeps_its_sales_persona(self):
        from smartdesk.services.knowledge import build_persona

        persona = build_persona(self.smartdesk)
        self.assertTrue(persona.sales_mode_enabled)
        self.assertIn("SmartDesk AI", persona.resolved_handoff_message())

    def test_sales_follow_up_is_suppressed_for_client_tenants(self):
        from ai.receptionist import ReceptionistEngine

        client_persona = TenantPersona(business_name="10by20 Padel Club",
                                       sales_mode_enabled=False)
        reply = ReceptionistEngine._sales_follow_up("pricing", "P150 per hour.",
                                                    client_persona)
        self.assertEqual(reply, "P150 per hour.")
        self.assertNotIn("setup", reply)

    def test_sales_follow_up_still_applies_to_smartdesk(self):
        from ai.receptionist import ReceptionistEngine

        reply = ReceptionistEngine._sales_follow_up(
            "pricing", "Contact sales.", TenantPersona(sales_mode_enabled=True)
        )
        self.assertIn("SmartDesk AI", reply)

    def test_tenant_without_a_profile_is_not_given_the_sales_persona(self):
        from smartdesk.services.knowledge import build_persona

        bare = Tenant(slug="bare", name="Bare Business")
        db.session.add(bare)
        db.session.flush()
        persona = build_persona(bare)
        self.assertFalse(persona.sales_mode_enabled)
        self.assertEqual(persona.business_name, "Bare Business")

    def test_knowledge_is_loaded_per_tenant(self):
        from smartdesk.services.knowledge import knowledge_dict
        from smartdesk.models import KnowledgeDocument

        db.session.add(KnowledgeDocument(tenant_id=self.padel.id, section="pricing",
                                         body="P150 per court hour",
                                         is_published=True))
        db.session.add(KnowledgeDocument(tenant_id=self.salon.id, section="pricing",
                                         body="P300 per treatment",
                                         is_published=True))
        db.session.commit()
        self.assertEqual(knowledge_dict(self.padel.id)["pricing"],
                         "P150 per court hour")
        self.assertEqual(knowledge_dict(self.salon.id)["pricing"],
                         "P300 per treatment")


# ---------------------------------------------------------------------------
# Test-data separation
# ---------------------------------------------------------------------------


class TestDataSeparationTests(MultiTenantTestCase):
    def test_records_inherit_the_test_flag_from_their_tenant(self):
        from smartdesk.tenancy import get_or_create_customer

        test_tenant = Tenant(slug="test-business", name="Test Business",
                             status="development", is_test_data=True)
        db.session.add(test_tenant)
        db.session.flush()
        customer = get_or_create_customer(test_tenant, "+26774444444")
        self.assertTrue(customer.is_test_data)

    def test_production_records_are_not_flagged_as_test_data(self):
        self.assertFalse(self.padel_customer.is_test_data)


# ---------------------------------------------------------------------------
# Webhook trust boundary
# ---------------------------------------------------------------------------


class TwilioSignatureTests(MultiTenantTestCase):
    def test_unsigned_request_is_rejected_when_validation_is_on(self):
        self.app.config["TWILIO_VALIDATE_SIGNATURE"] = True
        self.app.config["TWILIO_AUTH_TOKEN"] = "token"
        response = self.client.post(
            "/whatsapp",
            data={"From": "whatsapp:+26775555555", "To": "whatsapp:+26770000002",
                  "Body": "Hello"},
        )
        self.assertEqual(response.status_code, 403)

    def test_voice_unsigned_request_is_rejected(self):
        self.app.config["TWILIO_VALIDATE_SIGNATURE"] = True
        self.app.config["TWILIO_AUTH_TOKEN"] = "token"
        response = self.client.post("/voice", data={"To": "+26770000003"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
