"""Tests for Phase 2 additions: staff replies and automatic lead capture."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Channel,
    Conversation,
    Customer,
    Lead,
    Message,
    ReceptionistProfile,
)
from tests.test_multitenant import MultiTenantTestCase, make_token  # noqa: E402


class StaffReplyTests(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.whatsapp_channel = Channel.query.filter_by(
            tenant_id=self.padel.id, kind="whatsapp"
        ).one()
        self.padel_conversation.channel_id = self.whatsapp_channel.id
        db.session.commit()

    def test_reply_requires_takeover_first(self):
        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/reply",
            headers=self.auth(self.padel_user, self.padel),
            json={"body": "Hi, this is the manager"},
        )
        self.assertEqual(response.status_code, 409)

    @patch("smartdesk.api.dashboard.send_whatsapp_message")
    def test_reply_sends_and_records_a_human_agent_message(self, mock_send):
        mock_send.return_value = "SM_fake_sid"
        self.padel_conversation.human_takeover = True
        db.session.commit()

        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/reply",
            headers=self.auth(self.padel_user, self.padel),
            json={"body": "Yes, courts are available at 6pm"},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        mock_send.assert_called_once()

        message = Message.query.filter_by(
            conversation_id=self.padel_conversation.id, role="human_agent"
        ).one()
        self.assertEqual(message.body, "Yes, courts are available at 6pm")

    def test_viewer_cannot_send_staff_reply(self):
        self.padel_conversation.human_takeover = True
        db.session.commit()
        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/reply",
            headers=self.auth(self.viewer_user, self.padel),
            json={"body": "Hello"},
        )
        self.assertEqual(response.status_code, 403)

    def test_reply_is_refused_for_a_voice_conversation(self):
        voice_conversation = Conversation(
            tenant_id=self.padel.id, session_key="voice:CA999",
            channel_kind="voice", human_takeover=True,
        )
        db.session.add(voice_conversation)
        db.session.commit()
        response = self.client.post(
            f"/api/v1/conversations/{voice_conversation.id}/reply",
            headers=self.auth(self.padel_user, self.padel),
            json={"body": "Hello"},
        )
        self.assertEqual(response.status_code, 400)

    def test_empty_body_is_rejected(self):
        self.padel_conversation.human_takeover = True
        db.session.commit()
        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/reply",
            headers=self.auth(self.padel_user, self.padel),
            json={"body": "   "},
        )
        self.assertEqual(response.status_code, 400)

    def test_cross_tenant_reply_is_not_found(self):
        self.padel_conversation.human_takeover = True
        db.session.commit()
        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/reply",
            headers=self.auth(self.salon_user, self.salon),
            json={"body": "Hello"},
        )
        self.assertEqual(response.status_code, 404)

    @patch("smartdesk.api.dashboard.send_whatsapp_message")
    def test_send_failure_returns_a_clean_error_without_recording_a_message(
        self, mock_send
    ):
        from smartdesk.services.receptionist import OutboundSendError

        mock_send.side_effect = OutboundSendError("provider unavailable")
        self.padel_conversation.human_takeover = True
        db.session.commit()

        response = self.client.post(
            f"/api/v1/conversations/{self.padel_conversation.id}/reply",
            headers=self.auth(self.padel_user, self.padel),
            json={"body": "Hello"},
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            Message.query.filter_by(
                conversation_id=self.padel_conversation.id, role="human_agent"
            ).count(),
            0,
        )


class LeadCaptureTests(MultiTenantTestCase):
    """Fixtures already seed one unrelated lead per tenant (conversation_id is
    None), so assertions compare against that baseline rather than zero."""

    def setUp(self) -> None:
        super().setUp()
        ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).update(
            {"lead_capture_enabled": True}
        )
        db.session.commit()
        self.padel_baseline = Lead.query.filter_by(tenant_id=self.padel.id).count()
        self.salon_baseline = Lead.query.filter_by(tenant_id=self.salon.id).count()

    def _send(self, to_address="+26770000001", body="Hello", from_number="+26777777777"):
        return self.client.post(
            "/whatsapp",
            data={
                "From": f"whatsapp:{from_number}",
                "To": f"whatsapp:{to_address}",
                "Body": body,
                "MessageSid": "SM_test",
            },
        )

    def test_pricing_question_creates_a_lead(self):
        self._send(body="How much does a court cost per hour?")
        lead = Lead.query.filter_by(
            tenant_id=self.padel.id, conversation_id=None
        ).all()  # exclude the unrelated baseline lead by requiring a conversation
        new_lead = Lead.query.filter(
            Lead.tenant_id == self.padel.id, Lead.conversation_id.isnot(None)
        ).one()
        self.assertEqual(new_lead.source, "whatsapp")
        self.assertIn("cost", new_lead.interest)

    def test_greeting_does_not_create_a_lead(self):
        self._send(body="Hi there")
        self.assertEqual(
            Lead.query.filter_by(tenant_id=self.padel.id).count(), self.padel_baseline
        )

    def test_buying_language_creates_a_lead(self):
        self._send(body="Great, let's do it")
        self.assertEqual(
            Lead.query.filter_by(tenant_id=self.padel.id).count(),
            self.padel_baseline + 1,
        )

    def test_second_message_updates_rather_than_duplicates_the_lead(self):
        self._send(body="What's the price for a court?")
        self._send(body="Actually I'd like to book for Saturday")
        self.assertEqual(
            Lead.query.filter_by(tenant_id=self.padel.id).count(),
            self.padel_baseline + 1,
        )
        new_lead = Lead.query.filter(
            Lead.tenant_id == self.padel.id, Lead.conversation_id.isnot(None)
        ).one()
        self.assertIn("Saturday", new_lead.interest)

    def test_lead_capture_disabled_creates_nothing(self):
        ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).update(
            {"lead_capture_enabled": False}
        )
        db.session.commit()
        self._send(body="How much does it cost?")
        self.assertEqual(
            Lead.query.filter_by(tenant_id=self.padel.id).count(), self.padel_baseline
        )

    def test_lead_is_test_data_flag_matches_tenant(self):
        self.padel.is_test_data = True
        db.session.commit()
        self._send(body="What's the price?")
        new_lead = Lead.query.filter(
            Lead.tenant_id == self.padel.id, Lead.conversation_id.isnot(None)
        ).one()
        self.assertTrue(new_lead.is_test_data)

    def test_lead_does_not_leak_into_another_tenant(self):
        self._send(to_address="+26770000002", body="How much does a haircut cost?")
        self.assertEqual(
            Lead.query.filter_by(tenant_id=self.padel.id).count(), self.padel_baseline
        )
        self.assertEqual(
            Lead.query.filter_by(tenant_id=self.salon.id).count(),
            self.salon_baseline + 1,
        )


class UsageLimitTests(MultiTenantTestCase):
    def test_new_conversation_over_limit_is_handed_off_not_answered(self):
        self.padel.monthly_conversation_limit = 1
        db.session.commit()
        # One conversation already exists this month from fixtures (padel_conversation).
        response = self.client.post(
            "/whatsapp",
            data={
                "From": "whatsapp:+26779998887",
                "To": "whatsapp:+26770000001",
                "Body": "Hi, do you have courts available?",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"monthly conversation limit", response.data.lower())

        new_conversation = Conversation.query.filter_by(
            tenant_id=self.padel.id, session_key="whatsapp:+26779998887"
        ).one()
        self.assertEqual(new_conversation.status, "needs_human")

    def test_existing_conversation_is_never_cut_off_mid_thread(self):
        self.padel.monthly_conversation_limit = 1
        db.session.commit()
        # padel_conversation already has one message from fixtures; sending
        # another message on the SAME conversation must not be blocked.
        response = self.client.post(
            "/whatsapp",
            data={
                # Must match the fixture conversation's session_key exactly
                # (which includes the "whatsapp:" prefix) to land on the same,
                # already-existing conversation rather than creating a new one.
                "From": self.padel_conversation.session_key,
                "To": "whatsapp:+26770000001",
                "Body": "Following up on my earlier question",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"monthly conversation limit", response.data.lower())

    def test_zero_limit_means_unlimited(self):
        self.padel.monthly_conversation_limit = 0
        db.session.commit()
        response = self.client.post(
            "/whatsapp",
            data={
                "From": "whatsapp:+26779998886",
                "To": "whatsapp:+26770000001",
                "Body": "Hi",
            },
        )
        self.assertNotIn(b"monthly conversation limit", response.data.lower())


if __name__ == "__main__":
    unittest.main()
