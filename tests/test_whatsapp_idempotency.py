"""Phase 2 regression tests: WhatsApp webhook reliability and idempotency.

Locks in the fixes for:

* C1 -- a redelivered Twilio webhook (same ``MessageSid``) must not be
  processed twice (no second AI call, reply, usage event or lead);
* C2 -- one inbound message is stored exactly once, and the model is not shown
  the same customer message twice.

These run on SQLite so they are part of the normal suite.  The properties that
genuinely need PostgreSQL (true concurrency, the migration) live in
``test_whatsapp_idempotency_postgres.py``.
"""

from __future__ import annotations

import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from sqlalchemy.exc import IntegrityError  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

from ai.receptionist import ReceptionistEngine  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Conversation,
    Customer,
    Lead,
    Message,
    UsageEvent,
)
from smartdesk.services import conversations as conversation_service  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402

PADEL_NUMBER = "+26770000001"
SALON_NUMBER = "+26770000002"
CUSTOMER = "+26772222222"

#: Not answerable from knowledge and not a handoff/greeting/booking message, so
#: it always reaches the (faked) OpenAI call.  It also triggers lead capture.
AI_QUESTION = "How much does a court cost per hour?"


def new_sid() -> str:
    return f"SM{uuid.uuid4().hex}"


class FakeOpenAI:
    """Counts chat-completion calls and records the prompts it was sent."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        outer = self

        class _Completions:
            def create(self, model, messages, **kwargs):  # noqa: ARG002 - mimics SDK
                outer.calls.append(messages)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Fake AI reply."))]
                )

        self.chat = SimpleNamespace(completions=_Completions())


class WhatsAppWebhookTestCase(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.openai = FakeOpenAI()
        self.engine_calls = 0
        self.fail_engine_times = 0

        real_reply = ReceptionistEngine.reply
        outer = self

        def counting_reply(engine, *args, **kwargs):
            outer.engine_calls += 1
            if outer.fail_engine_times > 0:
                outer.fail_engine_times -= 1
                raise RuntimeError("simulated failure inside the engine")
            return real_reply(engine, *args, **kwargs)

        for patcher in (
            patch("smartdesk.services.receptionist.openai_client", return_value=self.openai),
            patch.object(ReceptionistEngine, "reply", counting_reply),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    def post(self, sid=None, body=AI_QUESTION, sender=CUSTOMER, to=PADEL_NUMBER, **extra):
        data = {"From": f"whatsapp:{sender}", "To": f"whatsapp:{to}", "Body": body}
        if sid is not None:
            data["MessageSid"] = sid
        data.update(extra)
        return self.client.post("/whatsapp", data=data)

    def conversation(self, tenant=None, sender=CUSTOMER):
        return Conversation.query.filter_by(
            tenant_id=(tenant or self.padel).id, session_key=f"whatsapp:{sender}"
        ).one_or_none()

    def rows(self, conversation, role):
        return Message.query.filter_by(conversation_id=conversation.id, role=role).all()

    def usage(self, kind):
        return UsageEvent.query.filter_by(tenant_id=self.padel.id, kind=kind).count()

    @staticmethod
    def has_reply(response) -> bool:
        return b"<Message>" in response.data


class NormalAndDuplicateDeliveryTests(WhatsAppWebhookTestCase):
    def test_normal_message_is_processed_exactly_once(self):
        """Test 1: new valid MessageSid -> 1 customer message, 1 AI call, 1 reply."""
        sid = new_sid()
        response = self.post(sid)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Fake AI reply.", response.data)
        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        self.assertEqual(len(self.rows(conv, "assistant")), 1)
        self.assertEqual(self.engine_calls, 1)
        self.assertEqual(len(self.openai.calls), 1)
        self.assertEqual(self.usage("message_in"), 1)
        self.assertEqual(self.usage("message_out"), 1)

    def test_duplicate_webhook_is_harmless(self):
        """Test 2: same MessageSid twice -> still 1 message, 1 AI call, 1 reply."""
        sid = new_sid()
        first = self.post(sid)
        second = self.post(sid)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(self.has_reply(first))
        self.assertEqual(second.status_code, 200)
        self.assertFalse(self.has_reply(second), "a duplicate must never be answered again")

        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        self.assertEqual(len(self.rows(conv, "assistant")), 1)
        self.assertEqual(self.engine_calls, 1)
        self.assertEqual(len(self.openai.calls), 1)
        self.assertEqual(Customer.query.filter_by(tenant_id=self.padel.id, phone=CUSTOMER).count(), 1)
        self.assertEqual(
            Conversation.query.filter_by(tenant_id=self.padel.id, session_key=f"whatsapp:{CUSTOMER}").count(),
            1,
        )

    def test_many_redeliveries_still_one_of_everything(self):
        sid = new_sid()
        for _ in range(5):
            self.post(sid)
        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        self.assertEqual(len(self.rows(conv, "assistant")), 1)
        self.assertEqual(self.engine_calls, 1)

    def test_different_message_sids_are_both_processed(self):
        """Test 3: two messages, two SIDs -> 2 customer messages, 2 AI calls."""
        self.post(new_sid(), body=AI_QUESTION)
        self.post(new_sid(), body="And do you rent rackets?")

        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 2)
        self.assertEqual(len(self.rows(conv, "assistant")), 2)
        self.assertEqual(self.engine_calls, 2)
        self.assertEqual(len(self.openai.calls), 2)

    def test_duplicate_does_not_double_count_usage(self):
        """Test 5."""
        sid = new_sid()
        self.post(sid)
        self.post(sid)
        self.post(sid)
        self.assertEqual(self.usage("message_in"), 1)
        self.assertEqual(self.usage("message_out"), 1)

    def test_duplicate_does_not_create_a_second_lead(self):
        """Test 6."""
        sid = new_sid()
        self.post(sid)
        conv = self.conversation()
        self.assertEqual(Lead.query.filter_by(conversation_id=conv.id).count(), 1)
        self.post(sid)
        self.post(sid)
        self.assertEqual(Lead.query.filter_by(conversation_id=conv.id).count(), 1)

    def test_duplicate_of_an_over_limit_message_adds_nothing(self):
        self.padel.monthly_conversation_limit = 1
        db.session.commit()
        sid = new_sid()
        first = self.post(sid, sender="+26773000009")
        self.assertIn(b"monthly conversation limit", first.data.lower())
        conv = self.conversation(sender="+26773000009")
        events_before = len(self.rows(conv, "event"))
        second = self.post(sid, sender="+26773000009")
        self.assertFalse(self.has_reply(second))
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        self.assertEqual(len(self.rows(conv, "event")), events_before)


class InboundPersistenceOwnershipTests(WhatsAppWebhookTestCase):
    """C2: the webhook is the single owner of the inbound message write."""

    def test_inbound_message_stored_once_on_every_engine_path(self):
        paths = {
            "ai": AI_QUESTION,
            "handoff": "I want to speak to a manager",
            "greeting": "Dumela",
        }
        for i, (name, body) in enumerate(paths.items()):
            sender = f"+2677300010{i}"
            self.post(new_sid(), body=body, sender=sender)
            conv = self.conversation(sender=sender)
            customer_rows = self.rows(conv, "customer")
            self.assertEqual(len(customer_rows), 1, f"{name} path stored the inbound message twice")
            self.assertEqual(customer_rows[0].body, body)
            self.assertEqual(len(self.rows(conv, "assistant")), 1, name)

    def test_model_is_not_shown_the_current_message_twice(self):
        self.post(new_sid(), body=AI_QUESTION)
        self.post(new_sid(), body="And do you rent rackets?")

        second_call = self.openai.calls[-1]
        occurrences = sum(
            m["content"].count("And do you rent rackets?") for m in second_call
        )
        self.assertEqual(occurrences, 1, "current message appears in history AND prompt")

        # ...while the PREVIOUS turn is still in the history, exactly once.
        history = [m for m in second_call if m["role"] in ("user", "assistant")][:-1]
        self.assertEqual(
            [(m["role"], m["content"]) for m in history],
            [("user", AI_QUESTION), ("assistant", "Fake AI reply.")],
        )

    def test_row_ownership_and_fields(self):
        sid = new_sid()
        self.post(sid)
        row = Message.query.filter_by(tenant_id=self.padel.id, provider_message_id=sid).one()
        conv = self.conversation()
        customer = Customer.query.filter_by(tenant_id=self.padel.id, phone=CUSTOMER).one()

        self.assertEqual(row.tenant_id, self.padel.id)
        self.assertEqual(row.conversation_id, conv.id)
        self.assertEqual(conv.tenant_id, self.padel.id)
        self.assertEqual(conv.customer_id, customer.id)
        self.assertEqual(row.role, "customer")          # inbound direction
        self.assertEqual(row.body, AI_QUESTION)
        self.assertEqual(row.provider_message_id, sid)
        self.assertIsNotNone(row.created_at)
        self.assertEqual(Message.query.filter_by(tenant_id=self.salon.id, provider_message_id=sid).count(), 0)

    def test_same_sid_at_another_tenant_is_independent_and_correctly_owned(self):
        """Uniqueness is per tenant: one tenant's SID can neither suppress nor
        attach to another tenant's message."""
        sid = new_sid()
        self.post(sid, to=PADEL_NUMBER)
        self.post(sid, to=SALON_NUMBER)

        padel_row = Message.query.filter_by(tenant_id=self.padel.id, provider_message_id=sid).one()
        salon_row = Message.query.filter_by(tenant_id=self.salon.id, provider_message_id=sid).one()
        self.assertNotEqual(padel_row.conversation_id, salon_row.conversation_id)
        self.assertEqual(
            db.session.get(Conversation, salon_row.conversation_id).tenant_id, self.salon.id
        )


class MissingMessageSidTests(WhatsAppWebhookTestCase):
    def assertRejectedWithoutSideEffects(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.has_reply(response))
        self.assertEqual(self.engine_calls, 0)
        self.assertEqual(len(self.openai.calls), 0)
        self.assertEqual(Customer.query.filter_by(tenant_id=self.padel.id, phone=CUSTOMER).count(), 0)
        self.assertIsNone(self.conversation())
        self.assertEqual(self.usage("message_in"), 0)

    def test_missing_message_sid_is_rejected(self):
        """Test 8: a signed inbound message without a MessageSid is malformed."""
        self.assertRejectedWithoutSideEffects(self.post(sid=None))

    def test_blank_or_malformed_message_sid_is_rejected(self):
        for bad in ("", "   ", "has space", "x" * 65, "semi;colon"):
            with self.subTest(sid=bad):
                self.assertRejectedWithoutSideEffects(self.post(sid=bad))

    def test_unknown_destination_is_still_a_silent_200(self):
        """Unroutable traffic keeps its existing, deliberate behaviour."""
        response = self.post(sid=None, to="+26778888888")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.has_reply(response))


class TwilioSignatureTests(WhatsAppWebhookTestCase):
    TOKEN = "unit-test-auth-token"

    def setUp(self) -> None:
        super().setUp()
        self.app.config["TWILIO_VALIDATE_SIGNATURE"] = True
        self.app.config["TWILIO_AUTH_TOKEN"] = self.TOKEN

    def signed_post(self, sid, url="http://localhost/whatsapp", signature=None):
        params = {
            "From": f"whatsapp:{CUSTOMER}",
            "To": f"whatsapp:{PADEL_NUMBER}",
            "Body": AI_QUESTION,
            "MessageSid": sid,
        }
        if signature is None:
            signature = RequestValidator(self.TOKEN).compute_signature(url, params)
        return self.client.post("/whatsapp", data=params, headers={"X-Twilio-Signature": signature})

    def test_valid_signature_is_accepted(self):
        """Test 7a."""
        response = self.signed_post(new_sid())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.has_reply(response))

    def test_invalid_signature_is_rejected_and_has_no_side_effects(self):
        """Test 7b."""
        response = self.signed_post(new_sid(), signature="not-a-real-signature")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.engine_calls, 0)
        self.assertIsNone(self.conversation())

    def test_missing_signature_is_rejected(self):
        response = self.client.post(
            "/whatsapp",
            data={"From": f"whatsapp:{CUSTOMER}", "To": f"whatsapp:{PADEL_NUMBER}",
                  "Body": AI_QUESTION, "MessageSid": new_sid()},
        )
        self.assertEqual(response.status_code, 403)

    def test_signature_signed_for_another_url_is_rejected(self):
        response = self.signed_post(new_sid(), signature=RequestValidator(self.TOKEN).compute_signature(
            "http://localhost/voice", {}))
        self.assertEqual(response.status_code, 403)

    def test_public_base_url_behind_a_proxy_is_honoured(self):
        self.app.config["TWILIO_WEBHOOK_BASE_URL"] = "https://smartdesk.example"
        ok = self.signed_post(new_sid(), url="https://smartdesk.example/whatsapp")
        self.assertEqual(ok.status_code, 200)
        wrong_host = self.signed_post(new_sid(), url="http://localhost/whatsapp")
        self.assertEqual(wrong_host.status_code, 403)

    def test_forged_request_cannot_burn_a_real_message_sid(self):
        sid = new_sid()
        forged = self.signed_post(sid, signature="forged")
        self.assertEqual(forged.status_code, 403)
        genuine = self.signed_post(sid)
        self.assertEqual(genuine.status_code, 200)
        self.assertTrue(self.has_reply(genuine))
        self.assertEqual(self.engine_calls, 1)


class FailureHandlingTests(WhatsAppWebhookTestCase):
    def test_unexpected_failure_is_a_5xx_not_a_fake_success(self):
        self.fail_engine_times = 1
        response = self.post(new_sid())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"<Message>", response.data)

    def test_failed_attempt_releases_the_claim_so_a_retry_is_processed_once(self):
        """The property that makes returning 5xx safe: nothing from the failed
        attempt survives, so Twilio's retry of the same MessageSid works."""
        sid = new_sid()
        self.fail_engine_times = 1
        failed = self.post(sid)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(Message.query.filter_by(provider_message_id=sid).count(), 0)
        self.assertEqual(self.usage("message_in"), 0)

        retried = self.post(sid)
        self.assertEqual(retried.status_code, 200)
        self.assertTrue(self.has_reply(retried))
        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        self.assertEqual(len(self.rows(conv, "assistant")), 1)
        self.assertEqual(self.usage("message_in"), 1)

        # ...and a further redelivery is now a harmless duplicate.
        again = self.post(sid)
        self.assertFalse(self.has_reply(again))
        self.assertEqual(len(self.rows(conv, "customer")), 1)


class DatabaseEnforcementTests(WhatsAppWebhookTestCase):
    """The database, not Python, is what makes duplicates impossible."""

    def _insert(self, tenant, conversation, role, sid):
        db.session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id,
                               role=role, body="x", provider_message_id=sid))
        db.session.flush()

    def test_unique_index_rejects_a_second_inbound_row(self):
        sid = new_sid()
        self._insert(self.padel, self.padel_conversation, "customer", sid)
        with self.assertRaises(IntegrityError):
            self._insert(self.padel, self.padel_conversation, "customer", sid)
        db.session.rollback()

    def test_index_is_scoped_partially_and_per_tenant(self):
        sid = new_sid()
        self._insert(self.padel, self.padel_conversation, "customer", sid)
        # other tenant: allowed
        self._insert(self.salon, self.salon_conversation, "customer", sid)
        # outbound staff reply reusing the string: allowed (different namespace)
        self._insert(self.padel, self.padel_conversation, "human_agent", sid)
        # NULL ids (legacy / assistant / voice): any number allowed
        self._insert(self.padel, self.padel_conversation, "customer", None)
        self._insert(self.padel, self.padel_conversation, "customer", None)
        db.session.commit()

    def test_lost_race_is_reported_as_duplicate_not_an_error(self):
        """Forces the concurrent-winner path on SQLite: the fast-path lookup
        misses, so only the unique index can stop the second request."""
        sid = new_sid()
        self.post(sid)
        self.assertEqual(self.engine_calls, 1)

        real_find = conversation_service.find_inbound_message
        misses = {"left": 1}

        def blind_once(tenant_id, provider_message_id):
            if misses["left"] > 0:
                misses["left"] -= 1
                return None
            return real_find(tenant_id, provider_message_id)

        with patch.object(conversation_service, "find_inbound_message", blind_once):
            response = self.post(sid)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.has_reply(response))
        self.assertEqual(self.engine_calls, 1)
        self.assertEqual(len(self.openai.calls), 1)
        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        self.assertEqual(len(self.rows(conv, "assistant")), 1)
        self.assertEqual(self.usage("message_in"), 1)


if __name__ == "__main__":
    unittest.main()
