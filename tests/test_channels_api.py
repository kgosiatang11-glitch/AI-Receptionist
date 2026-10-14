"""Tests for channel registration and status (smartdesk/api/config_api.py).

C8 -- WhatsApp ownership and provisioning.  Covers ``POST /api/v1/channels``
(successful registration, number normalisation, duplicate rejection, the
``(kind, address)`` uniqueness rule, owner-only access, audit logging) and the
channel payload served by ``GET /api/v1/channels/<kind>`` (the derived
``status`` and the ``webhook_url`` an owner pastes into Twilio).

What these tests deliberately do NOT claim: ``receiving`` only means a real
inbound request resolved to the channel.  Nothing here -- and nothing in the
application -- checks Twilio connectivity or outbound delivery.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from sqlalchemy.exc import IntegrityError  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import AuditLog, Channel, Membership, User, WhatsAppOnboarding  # noqa: E402
from smartdesk.services import whatsapp_onboarding  # noqa: E402
from smartdesk.tenancy import resolve_channel  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402

PADEL_WHATSAPP = "+26770000001"
SALON_WHATSAPP = "+26770000002"
PADEL_VOICE = "+26770000003"

# The exact keys the channel payload exposed before C8.  They must all remain.
LEGACY_PAYLOAD_KEYS = {
    "id",
    "kind",
    "address",
    "display_name",
    "is_active",
    "last_inbound_at",
    "conversations",
    "provider_configured",
    "signature_validation",
    "webhook_url",
}


class ChannelTestCase(MultiTenantTestCase):
    def post_channel(self, user, **payload):
        return self.client.post(
            "/api/v1/channels", headers=self.auth(user), json=payload
        )

    def request_whatsapp(self, user, **payload):
        return self.client.post(
            "/api/v1/whatsapp-onboarding", headers=self.auth(user), json=payload
        )

    def activate_whatsapp_for_test(self, address: str) -> Channel:
        """Create a trusted channel through the service's real activation gate.

        This preserves the channel-uniqueness coverage without restoring the
        unsafe owner-facing ``POST /channels`` WhatsApp path.
        """
        onboarding = WhatsAppOnboarding(
            tenant_id=self.padel.id, requested_number=address,
            status="ready_for_testing", twilio_configuration_recorded=True,
            registration_status="registered", phone_verification_status="confirmed",
            meta_verification_status="confirmed", last_provider_check_passed=True,
            provider_check_source="administrator_confirmation",
        )
        db.session.add(onboarding)
        db.session.flush()
        channel = whatsapp_onboarding.activate(onboarding)
        db.session.commit()
        return channel

    def list_channels(self, user, kind="whatsapp"):
        response = self.client.get(
            f"/api/v1/channels/{kind}", headers=self.auth(user)
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["items"]

    def padel_whatsapp_row(self) -> Channel:
        return Channel.query.filter_by(kind="whatsapp", address=PADEL_WHATSAPP).one()

    def member_with_role(self, role: str) -> User:
        user = User(
            supabase_user_id=f"sub-{role}-extra", email=f"{role}-extra@10by20.test"
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(Membership(tenant_id=self.padel.id, user_id=user.id, role=role))
        db.session.commit()
        return user


class ChannelRegistrationTests(ChannelTestCase):
    def test_owner_cannot_create_live_whatsapp_channel_and_can_request_onboarding(self):
        response = self.post_channel(
            self.padel_user,
            kind="whatsapp",
            address="+26771234567",
            display_name="Main line",
        )
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertIsNone(Channel.query.filter_by(kind="whatsapp", address="+26771234567").one_or_none())
        response = self.request_whatsapp(
            self.padel_user, phone_number="+26771234567", display_name="Main line"
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        onboarding = WhatsAppOnboarding.query.filter_by(tenant_id=self.padel.id).one()
        self.assertEqual(onboarding.requested_number, "+26771234567")
        self.assertEqual(onboarding.display_name, "Main line")

    def test_new_channel_is_visible_to_its_tenant_and_not_to_another(self):
        self.request_whatsapp(self.padel_user, phone_number="+26771234567")

        padel = {c["address"] for c in self.list_channels(self.padel_user)}
        salon = {c["address"] for c in self.list_channels(self.salon_user)}
        self.assertNotIn("+26771234567", padel, "requested numbers are not live channels")
        self.assertEqual(salon, {SALON_WHATSAPP})

    def test_tenant_always_comes_from_the_caller_never_from_the_payload(self):
        response = self.request_whatsapp(
            self.padel_user,
            phone_number="+26771234567",
            tenant_id=self.salon.id,
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        row = WhatsAppOnboarding.query.filter_by(requested_number="+26771234567").one()
        self.assertEqual(row.tenant_id, self.padel.id)

    def test_registration_is_audit_logged(self):
        before = AuditLog.query.filter_by(action="whatsapp_onboarding.request").count()
        response = self.request_whatsapp(
            self.padel_user, phone_number="+26771234567"
        )
        self.assertEqual(response.status_code, 201, response.get_json())

        entries = AuditLog.query.filter_by(action="whatsapp_onboarding.request").all()
        self.assertEqual(len(entries), before + 1)
        entry = entries[-1]
        self.assertEqual(entry.tenant_id, self.padel.id)
        self.assertEqual(entry.actor_email, self.padel_user.email)
        self.assertEqual(entry.object_type, "whatsapp_onboarding")
        self.assertEqual(entry.meta, {"requested_number": "+26771234567"})

    def test_existing_payload_fields_are_preserved_alongside_status(self):
        response = self.post_channel(self.padel_user, kind="voice", address="+26771234567")
        self.assertTrue(LEGACY_PAYLOAD_KEYS <= set(response.get_json()))
        for item in self.list_channels(self.padel_user):
            self.assertTrue(LEGACY_PAYLOAD_KEYS <= set(item))
            self.assertIn("status", item)


class ChannelNormalisationTests(ChannelTestCase):
    def test_whatsapp_requests_are_normalized_without_creating_live_channels(self):
        # Each case uses its own number so none collides with another.
        cases = [
            ("whatsapp:+267 7123 4501", "+26771234501"),
            ("26771234502", "+26771234502"),
            ("+267-7123-4503", "+26771234503"),
            ("  whatsapp:+26771234504  ", "+26771234504"),
            ("(267) 71234505", "+26771234505"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                response = self.request_whatsapp(self.padel_user, phone_number=raw)
                self.assertEqual(response.status_code, 201, response.get_json())
                self.assertEqual(response.get_json()["onboarding"]["requested_number"], expected)
                row = WhatsAppOnboarding.query.filter_by(requested_number=expected).one()
                self.assertEqual(row.tenant_id, self.padel.id)
                self.assertIsNone(
                    Channel.query.filter_by(kind="whatsapp", address=raw).one_or_none()
                )
                db.session.delete(row)
                db.session.commit()

    def test_inputs_that_normalise_to_nothing_are_rejected(self):
        cases = [
            {},
            {"kind": "whatsapp"},
            {"kind": "whatsapp", "address": ""},
            {"kind": "whatsapp", "address": "   "},
            {"kind": "whatsapp", "address": "not a number"},
            {"kind": "whatsapp", "address": None},
            {"address": "+26771234567"},
            {"kind": "telegram", "address": "+26771234567"},
        ]
        before = Channel.query.count()
        audits = AuditLog.query.filter_by(action="channel.create").count()
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.post_channel(self.padel_user, **payload)
                self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(Channel.query.count(), before)
        self.assertEqual(
            AuditLog.query.filter_by(action="channel.create").count(), audits
        )


class ChannelDuplicateTests(ChannelTestCase):
    def assert_direct_whatsapp_rejected_and_unchanged(self, response):
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertIn("setup request", response.get_json()["error"])
        # Still exactly one row, still owned by the original tenant.
        row = self.padel_whatsapp_row()
        self.assertEqual(row.tenant_id, self.padel.id)
        self.assertEqual(
            Channel.query.filter_by(kind="whatsapp", address=PADEL_WHATSAPP).count(), 1
        )

    def test_another_tenant_cannot_register_a_number_that_is_taken(self):
        response = self.post_channel(
            self.salon_user, kind="whatsapp", address=PADEL_WHATSAPP
        )
        self.assert_direct_whatsapp_rejected_and_unchanged(response)

    def test_duplicate_is_detected_after_normalisation(self):
        response = self.post_channel(
            self.salon_user, kind="whatsapp", address="whatsapp:+267 7000 0001"
        )
        self.assert_direct_whatsapp_rejected_and_unchanged(response)

    def test_a_tenant_cannot_register_its_own_number_twice(self):
        response = self.post_channel(
            self.padel_user, kind="whatsapp", address=PADEL_WHATSAPP
        )
        self.assert_direct_whatsapp_rejected_and_unchanged(response)

    def test_rejected_duplicate_is_not_audit_logged(self):
        before = AuditLog.query.filter_by(action="channel.create").count()
        self.post_channel(self.salon_user, kind="whatsapp", address=PADEL_WHATSAPP)
        self.assertEqual(
            AuditLog.query.filter_by(action="channel.create").count(), before
        )

    def test_database_constraint_rejects_a_duplicate_even_without_the_route(self):
        db.session.add(
            Channel(tenant_id=self.salon.id, kind="whatsapp", address=PADEL_WHATSAPP)
        )
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(self.padel_whatsapp_row().tenant_id, self.padel.id)


class ChannelKindUniquenessTests(ChannelTestCase):
    """Uniqueness is per ``(kind, address)``: one number may serve several
    channel kinds, but never the same kind for two tenants."""

    def assert_direct_whatsapp_rejected_and_unchanged(self, response):
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertIn("setup request", response.get_json()["error"])

    def test_same_address_can_be_registered_under_a_different_kind(self):
        # PADEL_VOICE exists as a voice channel; platform activation can add
        # the same address as WhatsApp without reopening the owner bypass.
        self.activate_whatsapp_for_test(PADEL_VOICE)

        rows = Channel.query.filter_by(address=PADEL_VOICE).all()
        self.assertEqual({r.kind for r in rows}, {"voice", "whatsapp"})
        self.assertEqual({r.tenant_id for r in rows}, {self.padel.id})

    def test_same_address_under_another_kind_resolves_per_kind(self):
        self.activate_whatsapp_for_test(PADEL_VOICE)
        self.assertEqual(resolve_channel("whatsapp", PADEL_VOICE).kind, "whatsapp")
        self.assertEqual(resolve_channel("voice", PADEL_VOICE).kind, "voice")

    def test_the_other_direction_also_works(self):
        # PADEL_WHATSAPP exists as WhatsApp; register it as voice.
        response = self.post_channel(
            self.padel_user, kind="voice", address=PADEL_WHATSAPP
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        kinds = {r.kind for r in Channel.query.filter_by(address=PADEL_WHATSAPP)}
        self.assertEqual(kinds, {"whatsapp", "voice"})

    def test_other_tenant_cannot_take_the_same_kind_after_a_second_kind_exists(self):
        self.post_channel(self.padel_user, kind="voice", address=PADEL_WHATSAPP)
        response = self.post_channel(
            self.salon_user, kind="voice", address=PADEL_WHATSAPP
        )
        self.assertEqual(response.status_code, 409, response.get_json())
        response = self.post_channel(self.salon_user, kind="whatsapp", address=PADEL_WHATSAPP)
        self.assert_direct_whatsapp_rejected_and_unchanged(response)


class ChannelOwnershipTests(ChannelTestCase):
    def test_only_an_owner_can_register_a_channel(self):
        before = Channel.query.count()
        for role in ("viewer", "agent", "manager"):
            with self.subTest(role=role):
                user = self.member_with_role(role)
                response = self.post_channel(
                    user, kind="whatsapp", address="+26771234567"
                )
                self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(Channel.query.count(), before)

    def test_unauthenticated_request_is_refused(self):
        response = self.client.post(
            "/api/v1/channels", json={"kind": "whatsapp", "address": "+26771234567"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(
            Channel.query.filter_by(address="+26771234567").one_or_none()
        )


class ChannelStatusTests(ChannelTestCase):
    def status_of_padel_whatsapp(self) -> str:
        items = {c["address"]: c for c in self.list_channels(self.padel_user)}
        return items[PADEL_WHATSAPP]["status"]

    def test_a_registered_channel_with_no_inbound_is_registered_not_receiving(self):
        self.assertIsNone(self.padel_whatsapp_row().last_inbound_at)
        self.assertEqual(self.status_of_padel_whatsapp(), "registered")

    def test_a_real_inbound_resolution_makes_the_channel_receiving(self):
        # resolve_channel is what the inbound webhook calls; it is the only
        # code that records last_inbound_at.
        resolve_channel("whatsapp", f"whatsapp:{PADEL_WHATSAPP}")
        db.session.commit()

        items = {c["address"]: c for c in self.list_channels(self.padel_user)}
        self.assertEqual(items[PADEL_WHATSAPP]["status"], "receiving")
        self.assertIsNotNone(items[PADEL_WHATSAPP]["last_inbound_at"])

    def test_receiving_does_not_expire(self):
        from datetime import datetime, timedelta, timezone

        row = self.padel_whatsapp_row()
        row.last_inbound_at = datetime.now(timezone.utc) - timedelta(days=400)
        db.session.commit()
        self.assertEqual(self.status_of_padel_whatsapp(), "receiving")

    def test_inactive_wins_over_a_previous_inbound(self):
        from datetime import datetime, timezone

        row = self.padel_whatsapp_row()
        row.last_inbound_at = datetime.now(timezone.utc)
        row.is_active = False
        db.session.commit()
        self.assertEqual(self.status_of_padel_whatsapp(), "inactive")

    def test_an_inactive_channel_is_not_resolved_so_it_never_becomes_receiving(self):
        row = self.padel_whatsapp_row()
        row.is_active = False
        db.session.commit()
        with self.assertRaises(Exception):
            resolve_channel("whatsapp", PADEL_WHATSAPP)
        db.session.rollback()
        self.assertEqual(self.status_of_padel_whatsapp(), "inactive")
        self.assertIsNone(self.padel_whatsapp_row().last_inbound_at)


class ChannelWebhookUrlTests(ChannelTestCase):
    BASE = "https://smartdesk.example"

    def url_for(self, kind: str):
        items = self.list_channels(self.padel_user, kind)
        self.assertEqual(len(items), 1)
        return items[0]["webhook_url"]

    def test_urls_are_built_from_the_configured_public_base(self):
        self.app.config["TWILIO_WEBHOOK_BASE_URL"] = self.BASE
        self.assertEqual(self.url_for("whatsapp"), f"{self.BASE}/whatsapp")
        self.assertEqual(self.url_for("voice"), f"{self.BASE}/voice")

    def test_trailing_slash_in_the_base_does_not_double_up(self):
        self.app.config["TWILIO_WEBHOOK_BASE_URL"] = f"{self.BASE}/"
        self.assertEqual(self.url_for("whatsapp"), f"{self.BASE}/whatsapp")

    def test_missing_base_gives_no_url_rather_than_a_relative_path(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.app.config["TWILIO_WEBHOOK_BASE_URL"] = value
                self.assertIsNone(self.url_for("whatsapp"))
                self.assertIsNone(self.url_for("voice"))

    def test_voice_registration_response_carries_the_same_url(self):
        self.app.config["TWILIO_WEBHOOK_BASE_URL"] = self.BASE
        response = self.post_channel(
            self.padel_user, kind="voice", address="+26771234567"
        )
        self.assertEqual(response.get_json()["webhook_url"], f"{self.BASE}/voice")

    def test_sms_has_no_webhook_route_so_no_url_is_shown(self):
        self.app.config["TWILIO_WEBHOOK_BASE_URL"] = self.BASE
        response = self.post_channel(
            self.padel_user, kind="sms", address="+26771234567"
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertIsNone(response.get_json()["webhook_url"])

    def test_the_displayed_url_is_the_one_twilio_signatures_are_checked_against(self):
        """What the owner pastes into Twilio must be what the app verifies."""
        self.app.config.update(
            TWILIO_VALIDATE_SIGNATURE=True,
            TWILIO_AUTH_TOKEN="token",
            TWILIO_WEBHOOK_BASE_URL=f"{self.BASE}/",
        )
        shown = self.url_for("whatsapp")
        params = {
            "From": "whatsapp:+26771111111",
            "To": f"whatsapp:{PADEL_WHATSAPP}",
            "Body": "",
            "MessageSid": "SM" + "a" * 32,
        }
        validator = RequestValidator("token")

        signed_for_shown_url = validator.compute_signature(shown, params)
        accepted = self.client.post(
            "/whatsapp", data=params, headers={"X-Twilio-Signature": signed_for_shown_url}
        )
        self.assertEqual(accepted.status_code, 200)

        # Control: a signature for any other URL is refused, so the check above
        # is not passing vacuously.
        signed_for_other_url = validator.compute_signature(f"{shown}x", params)
        refused = self.client.post(
            "/whatsapp", data=params, headers={"X-Twilio-Signature": signed_for_other_url}
        )
        self.assertEqual(refused.status_code, 403)


if __name__ == "__main__":
    unittest.main()
