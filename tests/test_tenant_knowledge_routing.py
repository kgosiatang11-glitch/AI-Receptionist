"""Tests for the tenant knowledge routing bug found via live testing.

Symptom observed in production: a WhatsApp message to the Test Business
sandbox channel replied with SmartDesk's own greeting ("Hello and welcome
to SmartDesk AI!...") instead of the greeting configured for Test Business,
even though the channel correctly resolved to the right tenant.

Root cause: ai/receptionist.py's reply() called route_message() without
passing tenant-specific knowledge, so it fell back to the single legacy
global config/smartdesk_config.json for every deterministic canned reply
(greeting, pricing, hours, features) -- regardless of which tenant's
channel the message came in on. Persona-based checks (sales_mode_enabled,
handoff, etc.) were already correctly tenant-scoped since Phase 1; only
the raw `knowledge` dict lookups were not.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Channel, KnowledgeDocument, ReceptionistProfile  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402

#: The exact legacy global greeting -- if this ever appears in a tenant's
#: reply, the bug (or a regression of the fix) is present.
LEGACY_SMARTDESK_GREETING = (
    "Hello and welcome to SmartDesk AI! I'm O'Brien, your AI Receptionist. "
    "How can I assist you today?"
)
LEGACY_PRICING = "Please contact sales for pricing plans tailored to your business size and needs."


class TenantKnowledgeRoutingTests(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Two distinct WhatsApp channels so each tenant is reached via its
        # own real number, exactly like the live sandbox scenario.
        db.session.add(
            Channel(tenant_id=self.padel.id, kind="whatsapp", address="+26770001111")
        )
        db.session.add(
            Channel(tenant_id=self.salon.id, kind="whatsapp", address="+26770002222")
        )

        padel_profile = ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).one()
        padel_profile.greeting = "Welcome to 10by20 Padel Club! Ready to book a court?"

        salon_profile = ReceptionistProfile.query.filter_by(tenant_id=self.salon.id).one()
        salon_profile.greeting = "SALON — CHANNEL IS WORKING"

        db.session.add(
            KnowledgeDocument(
                tenant_id=self.padel.id, section="pricing",
                body="P150 per court hour.", is_published=True,
            )
        )
        db.session.commit()

    def _send(self, to_address: str, body: str, from_number: str) -> bytes:
        response = self.client.post(
            "/whatsapp",
            data={"From": f"whatsapp:{from_number}", "To": f"whatsapp:{to_address}", "Body": body},
        )
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_greeting_uses_the_tenants_own_configured_greeting(self):
        reply = self._send("+26770002222", "Hi", "+26779991111")
        self.assertIn(b"SALON", reply)
        self.assertNotIn(LEGACY_SMARTDESK_GREETING.encode(), reply)

    def test_two_tenants_get_two_different_greetings_on_the_same_message(self):
        """The core reproduction: identical inbound text, different tenants,
        must produce different, correctly tenant-scoped replies."""
        padel_reply = self._send("+26770001111", "Hello", "+26779992222")
        salon_reply = self._send("+26770002222", "Hello", "+26779993333")

        self.assertIn(b"10by20", padel_reply)
        self.assertIn(b"SALON", salon_reply)
        self.assertNotEqual(padel_reply, salon_reply)
        self.assertNotIn(LEGACY_SMARTDESK_GREETING.encode(), padel_reply)
        self.assertNotIn(LEGACY_SMARTDESK_GREETING.encode(), salon_reply)

    def test_pricing_question_uses_the_tenants_own_pricing(self):
        reply = self._send("+26770001111", "How much does it cost?", "+26779994444")
        self.assertIn(b"P150", reply)
        self.assertNotIn(LEGACY_PRICING.encode(), reply)

    def test_smartdesk_tenant_itself_still_gets_its_own_correct_greeting(self):
        """The fix must not break the one tenant whose content legitimately
        matches the legacy default -- SmartDesk's own greeting IS that
        text, by design, and should still come through correctly post-fix."""
        db.session.add(
            Channel(tenant_id=self.smartdesk.id, kind="whatsapp", address="+26770003333")
        )
        db.session.add(
            KnowledgeDocument(
                tenant_id=self.smartdesk.id, section="business_information",
                body="ok", is_published=True,
            )
        )
        smartdesk_profile = ReceptionistProfile.query.filter_by(
            tenant_id=self.smartdesk.id
        ).one()
        smartdesk_profile.greeting = LEGACY_SMARTDESK_GREETING
        db.session.commit()

        reply = self._send("+26770003333", "Hi", "+26779995555")
        self.assertIn(LEGACY_SMARTDESK_GREETING.encode(), reply)

    def test_tenant_with_no_custom_greeting_gets_no_greeting_not_someone_elses(self):
        """A tenant that hasn't configured a greeting yet must get nothing
        (or a neutral fallback) -- never another tenant's, and never the
        legacy global default masquerading as theirs."""
        # Strip padel's greeting to simulate an unconfigured business.
        padel_profile = ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).one()
        padel_profile.greeting = None
        db.session.commit()

        reply = self._send("+26770001111", "Hi", "+26779996666")
        self.assertNotIn(LEGACY_SMARTDESK_GREETING.encode(), reply)


if __name__ == "__main__":
    unittest.main()
