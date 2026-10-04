"""Tests for Phase 2 additions: staff replies and automatic lead capture."""

from __future__ import annotations

import os
import uuid
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
                "MessageSid": f"SM{uuid.uuid4().hex}",
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
    """The monthly limit now caps AI replies per UTC month (Phase 3.3B).

    The old limit counted NEW conversations only, so an existing conversation
    could be answered without bound and a stored 0 meant "unlimited".  Both
    behaviours are gone; the full matrix is in ``tests/test_whatsapp_quota.py``.
    """

    #: Not a greeting/handoff/booking/canned intent, so it always needs OpenAI.
    AI_BODY = "How much does a court cost per hour?"

    def setUp(self) -> None:
        super().setUp()
        self.openai_calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.openai_calls.append(kwargs)
                return type("R", (), {
                    "choices": [type("C", (), {"message": type("M", (), {"content": "AI reply."})()})()],
                })()

        client = type("Client", (), {"chat": type("Chat", (), {"completions": _Completions()})()})()
        patcher = patch("smartdesk.services.receptionist.openai_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, sender: str, body: str | None = None):
        return self.client.post(
            "/whatsapp",
            data={
                "From": sender,
                "To": "whatsapp:+26770000001",
                "Body": body or self.AI_BODY,
                "MessageSid": f"SM{uuid.uuid4().hex}",
            },
        )

    def test_new_customer_over_limit_is_handed_off_not_answered(self):
        self.padel.monthly_conversation_limit = 1
        db.session.commit()
        self.assertIn(b"AI reply.", self._send("whatsapp:+26779998880").data)

        response = self._send("whatsapp:+26779998887")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"monthly conversation limit", response.data.lower())
        self.assertEqual(len(self.openai_calls), 1)  # the refused message never reached OpenAI

        new_conversation = Conversation.query.filter_by(
            tenant_id=self.padel.id, session_key="whatsapp:+26779998887"
        ).one()
        self.assertEqual(new_conversation.status, "needs_human")

    def test_existing_conversation_is_quota_controlled_too(self):
        """The old behaviour (never cut off mid-thread) was the quota bypass."""
        self.padel.monthly_conversation_limit = 1
        db.session.commit()
        # The fixture conversation already exists; the first AI reply on it
        # uses the only unit, the next one must be refused.
        sender = self.padel_conversation.session_key
        self.assertIn(b"AI reply.", self._send(sender).data)
        response = self._send(sender, "Following up on my earlier question")
        self.assertIn(b"monthly conversation limit", response.data.lower())
        self.assertEqual(len(self.openai_calls), 1)

    def test_zero_limit_means_no_ai_capacity_not_unlimited(self):
        self.padel.monthly_conversation_limit = 0
        db.session.commit()
        response = self._send("whatsapp:+26779998886")
        self.assertIn(b"monthly conversation limit", response.data.lower())
        self.assertEqual(self.openai_calls, [])


if __name__ == "__main__":
    unittest.main()
