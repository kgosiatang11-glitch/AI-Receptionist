"""Tests for the booking-mode dashboard configuration endpoints."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402


class BookingConfigApiTests(MultiTenantTestCase):
    def test_manager_can_switch_booking_mode_to_external(self):
        response = self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.padel_user, self.padel),
            json={"booking_mode": "external", "external_booking_url": "https://example.com/book"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["booking_mode"], "external")
        self.assertEqual(body["external_booking_url"], "https://example.com/book")

    def test_invalid_booking_mode_is_rejected(self):
        response = self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.padel_user, self.padel),
            json={"booking_mode": "not-a-real-mode"},
        )
        self.assertEqual(response.status_code, 400)

    def test_default_duration_can_be_updated(self):
        response = self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.padel_user, self.padel),
            json={"default_booking_duration_minutes": 90},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["default_booking_duration_minutes"], 90)

    def test_out_of_range_duration_is_rejected(self):
        response = self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.padel_user, self.padel),
            json={"default_booking_duration_minutes": 5000},
        )
        self.assertEqual(response.status_code, 400)

    def test_viewer_cannot_change_booking_mode(self):
        response = self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.viewer_user, self.padel),
            json={"booking_mode": "external"},
        )
        self.assertEqual(response.status_code, 403)

    def test_booking_config_is_tenant_isolated(self):
        self.client.patch(
            "/api/v1/receptionist",
            headers=self.auth(self.padel_user, self.padel),
            json={"booking_mode": "external", "external_booking_url": "https://10by20.example/book"},
        )
        salon_response = self.client.get(
            "/api/v1/receptionist", headers=self.auth(self.salon_user, self.salon)
        )
        self.assertEqual(salon_response.get_json()["booking_mode"], "calendar")
        self.assertIsNone(salon_response.get_json()["external_booking_url"])

    def test_new_tenants_default_to_calendar_mode(self):
        response = self.client.get(
            "/api/v1/receptionist", headers=self.auth(self.padel_user, self.padel)
        )
        self.assertEqual(response.get_json()["booking_mode"], "calendar")


if __name__ == "__main__":
    unittest.main()
