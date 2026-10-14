"""Regression tests for manual, tenant-scoped WhatsApp provisioning."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import AuditLog, Channel, ReceptionistProfile, WhatsAppOnboarding  # noqa: E402
from smartdesk.services import whatsapp_onboarding as service  # noqa: E402
from smartdesk.tenancy import TenantResolutionError, resolve_channel  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402


class OnboardingCase(MultiTenantTestCase):
    def request(self, user, phone, display_name="Main line"):
        return self.client.post("/api/v1/whatsapp-onboarding", headers=self.auth(user), json={
            "phone_number": phone, "display_name": display_name,
        })

    def row(self):
        return WhatsAppOnboarding.query.filter_by(tenant_id=self.padel.id).one()

    def admin_patch(self, row, action, **payload):
        return self.client.patch(
            f"/api/v1/admin/whatsapp-onboardings/{row.id}",
            headers=self.auth(self.admin_user), json={"action": action, **payload},
        )

    def ready(self, row):
        row.status = "ready_for_testing"
        row.twilio_configuration_recorded = True
        row.registration_status = "registered"
        row.phone_verification_status = "confirmed"
        row.meta_verification_status = "confirmed"
        row.outstanding_requirements = []
        row.last_provider_check_passed = True
        row.provider_check_source = "administrator_confirmation"
        db.session.commit()


class OwnerRequestTests(OnboardingCase):
    def test_first_request_is_normalized_and_same_number_is_idempotent(self):
        response = self.request(self.padel_user, "whatsapp:+267 7123 4567")
        self.assertEqual(response.status_code, 201, response.get_json())
        row = self.row()
        self.assertEqual(row.requested_number, "+26771234567")
        row.provisioning_error = "keep evidence"
        db.session.commit()
        response = self.request(self.padel_user, "+26771234567", "Changed label")
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all()
        self.assertEqual(self.row().provisioning_error, "keep evidence")
        self.assertEqual(self.row().display_name, "Main line")

    def test_permitted_number_change_invalidates_number_specific_evidence(self):
        self.request(self.padel_user, "+26771234567")
        row = self.row()
        row.status = "failed"
        row.twilio_sender_sid = "PNold"
        row.twilio_configuration_recorded = True
        row.phone_verification_status = "confirmed"
        row.meta_verification_status = "confirmed"
        row.registration_status = "registered"
        row.outstanding_requirements = ["old requirement"]
        row.last_provider_check_passed = True
        row.provider_check_source = "provider_console"
        row.provisioning_error = "old failure"
        db.session.commit()
        response = self.request(self.padel_user, "+26771234568")
        self.assertEqual(response.status_code, 201, response.get_json())
        db.session.expire_all(); row = self.row()
        self.assertEqual(row.status, "requested")
        self.assertIsNone(row.twilio_sender_sid)
        self.assertFalse(row.twilio_configuration_recorded)
        self.assertEqual(row.registration_status, "unverified")
        self.assertIsNone(row.last_provider_check_passed)
        self.assertEqual(row.outstanding_requirements, [])
        self.assertIsNone(row.provisioning_error)

    def test_number_change_is_rejected_during_active_provisioning(self):
        self.request(self.padel_user, "+26771234567")
        row = self.row(); row.status = "provisioning"; db.session.commit()
        response = self.request(self.padel_user, "+26771234568")
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.row().requested_number, "+26771234567")

    def test_owner_cannot_access_another_tenants_record_or_admin_actions(self):
        self.request(self.padel_user, "+26771234567")
        response = self.client.get("/api/v1/whatsapp-onboarding", headers=self.auth(self.salon_user, self.salon))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["onboarding"])
        response = self.admin_patch(self.row(), "review")
        self.assertEqual(response.status_code, 200)
        response = self.client.patch(
            f"/api/v1/admin/whatsapp-onboardings/{self.row().id}",
            headers=self.auth(self.padel_user), json={"action": "activate"},
        )
        self.assertEqual(response.status_code, 403)


class ActivationAndChannelTests(OnboardingCase):
    def test_each_activation_prerequisite_and_state_are_enforced_in_service(self):
        self.request(self.padel_user, "+26771234567")
        row = self.row(); row.status = "ready_for_testing"; db.session.commit()
        for field, value in (
            ("twilio_configuration_recorded", True),
            ("registration_status", "registered"),
            ("phone_verification_status", "confirmed"),
            ("meta_verification_status", "confirmed"),
            ("last_provider_check_passed", True),
        ):
            with self.subTest(field=field):
                db.session.refresh(row)
                with self.assertRaises(ValueError): service.activate(row)
                setattr(row, field, value); db.session.commit()
        self.assertIsNone(service.activation_error(row))
        row.status = "provisioning"; db.session.commit()
        with self.assertRaises(ValueError): service.activate(row)

    def test_activation_is_safe_and_does_not_reactivate_shared_profile(self):
        self.request(self.padel_user, "+26771234567")
        row = self.row(); self.ready(row)
        # Fixture profile is intentionally disabled independently of WhatsApp.
        profile = ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).one()
        profile.is_active = False; db.session.commit()
        channel = service.activate(row); db.session.commit()
        self.assertTrue(channel.is_active)
        self.assertFalse(profile.is_active)
        self.assertIs(service.activate(row), channel)

    def test_suspend_only_disables_whatsapp_channel_and_voice_stays_available(self):
        self.request(self.padel_user, "+26771234567")
        row = self.row(); self.ready(row); service.activate(row); db.session.commit()
        voice = Channel.query.filter_by(tenant_id=self.padel.id, kind="voice").one()
        profile = ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).one()
        profile.is_active = True; db.session.commit()
        service.suspend(row); db.session.commit()
        whatsapp = Channel.query.filter_by(tenant_id=self.padel.id, kind="whatsapp", address="+26771234567").one()
        self.assertFalse(whatsapp.is_active)
        self.assertTrue(voice.is_active)
        self.assertTrue(profile.is_active)
        with self.assertRaises(TenantResolutionError): resolve_channel("whatsapp", whatsapp.address)
        self.assertEqual(resolve_channel("voice", voice.address).id, voice.id)

    def test_active_number_mapped_to_another_tenant_cannot_activate(self):
        self.request(self.padel_user, "+26770000002")
        row = self.row(); self.ready(row)
        with self.assertRaisesRegex(ValueError, "another tenant"):
            service.activate(row)

    def test_admin_transition_actions_are_checked_and_audited(self):
        self.request(self.padel_user, "+26771234567")
        row = self.row()
        self.assertEqual(self.admin_patch(row, "provision").status_code, 409)
        self.assertEqual(self.admin_patch(row, "review").status_code, 200)
        self.assertEqual(self.admin_patch(row, "provision").status_code, 200)
        self.assertEqual(self.admin_patch(row, "ready_for_testing").status_code, 200)
        self.assertGreater(AuditLog.query.filter_by(action="whatsapp_onboarding.provision").count(), 0)


class AdminInputValidationTests(OnboardingCase):
    def setUp(self):
        super().setUp()
        self.request(self.padel_user, "+26771234567")

    def test_admin_rejects_non_object_json_without_mutating(self):
        row = self.row()
        response = self.client.patch(
            f"/api/v1/admin/whatsapp-onboardings/{row.id}",
            headers=self.auth(self.admin_user), json=["not", "an", "object"],
        )
        self.assertEqual(response.status_code, 400, response.get_json())
        db.session.expire_all()
        self.assertEqual(self.row().status, "requested")

    def test_admin_rejects_malformed_update_values_before_any_change(self):
        row = self.row()
        response = self.admin_patch(
            row, "update", twilio_sender_sid="not-a-sid",
            phone_verification_status=["confirmed"],
            outstanding_requirements="call the customer",
            provisioning_error={"not": "text"},
        )
        self.assertEqual(response.status_code, 400, response.get_json())
        db.session.expire_all(); row = self.row()
        self.assertIsNone(row.twilio_sender_sid)
        self.assertEqual(row.phone_verification_status, "not_applicable")
        self.assertEqual(row.outstanding_requirements, [])

    def test_admin_rejects_invalid_statuses_and_illegal_status_transition(self):
        row = self.row()
        self.assertEqual(self.admin_patch(row, "update", registration_status="complete").status_code, 400)
        self.assertEqual(self.admin_patch(row, "update", status="active").status_code, 400)
        self.assertEqual(self.admin_patch(row, "update", status="ready_for_testing").status_code, 409)

    def test_provider_check_validation_and_failure_are_recorded(self):
        row = self.row()
        self.assertEqual(self.admin_patch(row, "record_check", passed=True, source=[]).status_code, 400)
        self.assertEqual(self.admin_patch(row, "record_check", passed="yes").status_code, 400)
        self.assertEqual(self.admin_patch(row, "review").status_code, 200)
        response = self.admin_patch(row, "record_check", passed=False,
                                   source="provider_console", error="webhook was rejected")
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all(); row = self.row()
        self.assertEqual(row.status, "failed")
        self.assertFalse(row.last_provider_check_passed)
        self.assertEqual(row.provider_check_source, "provider_console")
        self.assertEqual(row.provisioning_error, "webhook was rejected")

    def test_activation_requires_complete_record_then_suspends_only_whatsapp(self):
        row = self.row()
        self.assertEqual(self.admin_patch(row, "activate").status_code, 409)
        self.assertEqual(self.admin_patch(row, "review").status_code, 200)
        self.assertEqual(self.admin_patch(row, "provision").status_code, 200)
        response = self.admin_patch(
            row, "update", twilio_sender_sid="PN" + "a" * 32,
            twilio_configuration_recorded=True, phone_verification_status="confirmed",
            meta_verification_status="confirmed", registration_status="registered",
            outstanding_requirements=[],
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.admin_patch(row, "record_check", passed=True,
                                         source="administrator_confirmation").status_code, 200)
        self.assertEqual(self.admin_patch(row, "ready_for_testing").status_code, 200)
        self.assertEqual(self.admin_patch(row, "activate").status_code, 200)
        db.session.expire_all(); self.assertEqual(self.row().status, "active")
        self.assertEqual(self.admin_patch(self.row(), "suspend").status_code, 200)
        db.session.expire_all()
        channel = Channel.query.filter_by(
            tenant_id=self.padel.id, kind="whatsapp", address="+26771234567"
        ).one()
        self.assertFalse(channel.is_active)


if __name__ == "__main__":
    unittest.main()
