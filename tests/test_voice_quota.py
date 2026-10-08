"""Voice wiring for the shared atomic AI-usage reservation service.

Voice uses the same reservation lifecycle as WhatsApp: reserve -> OpenAI ->
commit | release.  Each voice turn is identified by ``voice:<CallSid>:<turn>``,
which is the reservation idempotency key AND the ``provider_message_id`` of the
turn's customer row (the claim) and assistant row (so a replay is provably the
reply to that exact turn).
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Conversation, Message, UsageReservation  # noqa: E402
from smartdesk.services import usage as usage_service  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402
from tests.test_whatsapp_idempotency import AI_QUESTION  # noqa: E402

VOICE_NUMBER = "+26770000003"
CALLER = "+26772222222"
SID = "CA-voice-1"


class VoiceOpenAI:
    """Fake OpenAI client.  Replies are numbered so tests can tell turns apart."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail: Exception | None = None
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.fail is not None:
                    raise outer.fail
                n = len(outer.calls)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=f"AI answer {n}."))],
                    usage=SimpleNamespace(
                        prompt_tokens=10 + n, completion_tokens=5 + n, total_tokens=15 + 2 * n
                    ),
                )

        self.chat = SimpleNamespace(completions=_Completions())


class VoiceQuotaCase(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ai = VoiceOpenAI()
        p = patch("smartdesk.services.receptionist.openai_client", return_value=self.ai)
        p.start()
        self.addCleanup(p.stop)

    def set_limit(self, limit: int) -> None:
        self.padel.monthly_conversation_limit = limit
        db.session.commit()

    def start(self, call_sid=SID):
        return self.client.post(
            "/voice", data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": call_sid}
        )

    def turn(self, turn=1, call_sid=SID, body=AI_QUESTION):
        return self.client.post(
            f"/voice/continue?turn={turn}",
            data={"To": VOICE_NUMBER, "From": CALLER, "CallSid": call_sid, "SpeechResult": body},
        )

    def reservations(self):
        db.session.expire_all()
        return (
            UsageReservation.query.filter_by(tenant_id=self.padel.id)
            .order_by(UsageReservation.idempotency_key)
            .all()
        )

    def reservation(self, turn=1, call_sid=SID):
        return usage_service.find_reservation(
            self.padel.id, usage_service.AI_REPLY_KIND, f"voice:{call_sid}:{turn}"
        )

    def conversation(self, call_sid=SID):
        db.session.expire_all()
        return Conversation.query.filter_by(
            tenant_id=self.padel.id, session_key=f"voice:{call_sid}"
        ).one()

    def messages(self, role=None, call_sid=SID):
        db.session.expire_all()
        query = Message.query.filter_by(conversation_id=self.conversation(call_sid).id)
        if role:
            query = query.filter_by(role=role)
        return query.order_by(Message.created_at, Message.id).all()

    def used(self):
        db.session.expire_all()
        return usage_service.get_current_usage(self.padel.id).used_units


class VoiceStartTests(VoiceQuotaCase):
    def test_voice_start_never_reserves_or_calls_openai(self):  # 1
        self.set_limit(1)
        start = self.start()
        self.assertEqual(start.status_code, 200)
        self.assertIn(b"turn=1", start.data)
        self.assertEqual(self.ai.calls, [])
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.used(), 0)


class VoiceSuccessTests(VoiceQuotaCase):
    def test_successful_turn_reserves_once_commits_and_records_tokens(self):  # 2, 3, 4
        self.set_limit(1)
        self.start()
        response = self.turn()
        self.assertIn(b"AI answer 1.", response.data)
        self.assertIn(b"turn=2", response.data)
        self.assertEqual(len(self.ai.calls), 1)
        reservations = self.reservations()
        self.assertEqual(len(reservations), 1)
        r = reservations[0]
        self.assertEqual(r.idempotency_key, "voice:CA-voice-1:1")
        self.assertEqual(r.status, "committed")
        self.assertEqual((r.prompt_tokens, r.completion_tokens, r.total_tokens), (11, 6, 17))
        self.assertEqual(self.used(), 1)

    def test_successful_turn_persists_one_customer_and_one_tagged_assistant_row(self):
        self.turn()
        customers = self.messages("customer")
        assistants = self.messages("assistant")
        self.assertEqual([m.body for m in customers], [AI_QUESTION])
        self.assertEqual([m.provider_message_id for m in customers], ["voice:CA-voice-1:1"])
        self.assertEqual([m.body for m in assistants], ["AI answer 1."])
        self.assertEqual([m.provider_message_id for m in assistants], ["voice:CA-voice-1:1"])

    def test_multiple_turns_use_one_committed_reservation_each_with_own_tokens(self):  # 16, 17
        self.set_limit(5)
        self.turn(turn=1)
        self.turn(turn=2)
        self.turn(turn=3)
        self.assertEqual(len(self.ai.calls), 3)
        rows = self.reservations()
        self.assertEqual(
            [r.idempotency_key for r in rows],
            ["voice:CA-voice-1:1", "voice:CA-voice-1:2", "voice:CA-voice-1:3"],
        )
        self.assertEqual([r.status for r in rows], ["committed"] * 3)
        self.assertEqual(
            [(r.prompt_tokens, r.completion_tokens, r.total_tokens) for r in rows],
            [(11, 6, 17), (12, 7, 19), (13, 8, 21)],
        )
        self.assertEqual(self.used(), 3)


class VoiceFailureTests(VoiceQuotaCase):
    def test_failed_openai_releases_capacity_and_stores_no_assistant_reply(self):  # 5, 6
        self.set_limit(1)
        self.ai.fail = RuntimeError("OpenAI unavailable")
        response = self.turn()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"I don't have that information available", response.data)
        self.assertIn(b"turn=2", response.data)
        r = self.reservation()
        self.assertEqual(r.status, "released")
        self.assertEqual((r.prompt_tokens, r.completion_tokens, r.total_tokens), (None,) * 3)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.messages("assistant"), [])
        self.assertEqual(len(self.messages("customer")), 1)

    def test_retry_after_released_reservation_never_replays_an_older_answer(self):  # 7
        self.set_limit(5)
        self.turn(turn=1)  # succeeds: "AI answer 1."
        self.ai.fail = RuntimeError("OpenAI unavailable")
        self.turn(turn=2)  # fails -> released
        self.assertEqual(self.reservation(turn=2).status, "released")
        calls = len(self.ai.calls)

        self.ai.fail = None
        retry = self.turn(turn=2)
        self.assertNotIn(b"AI answer 1.", retry.data)
        self.assertIn(b"I don't have that information available", retry.data)
        self.assertEqual(len(self.ai.calls), calls)
        self.assertEqual(self.reservation(turn=2).status, "released")
        self.assertEqual(len(self.reservations()), 2)
        self.assertEqual(self.used(), 1)

    def test_retry_after_stale_released_reservation_never_replays_an_older_answer(self):  # 7
        """Crash between reserve and finalize: the stale sweep releases the unit.
        The retry of that turn must not speak turn 1's answer."""
        self.set_limit(5)
        self.turn(turn=1)
        reserved = usage_service.reserve_ai_reply(self.padel.id, "voice:CA-voice-1:2")
        db.session.commit()
        usage_service.release_reservation(self.padel.id, reserved.reservation.id)
        db.session.commit()
        calls = len(self.ai.calls)

        retry = self.turn(turn=2)
        self.assertNotIn(b"AI answer 1.", retry.data)
        self.assertEqual(len(self.ai.calls), calls)
        self.assertEqual(len(self.reservations()), 2)


class VoiceDuplicateTests(VoiceQuotaCase):
    def test_retry_after_committed_reservation_replays_without_new_ai_or_quota(self):  # 8, 9
        self.set_limit(2)
        first = self.turn(turn=1)
        duplicate = self.turn(turn=1)
        self.assertIn(b"AI answer 1.", first.data)
        self.assertIn(b"AI answer 1.", duplicate.data)
        self.assertIn(b"turn=2", duplicate.data)
        self.assertNotIn(b"<Hangup", duplicate.data)
        self.assertEqual(len(self.ai.calls), 1)
        self.assertEqual(len(self.reservations()), 1)
        self.assertEqual(self.used(), 1)
        self.assertEqual(len(self.messages("customer")), 1)
        self.assertEqual(len(self.messages("assistant")), 1)

    def test_late_retry_of_an_earlier_turn_replays_that_turns_answer_not_the_latest(self):
        self.set_limit(5)
        self.turn(turn=1)  # AI answer 1.
        self.turn(turn=2)  # AI answer 2.
        late = self.turn(turn=1)
        self.assertIn(b"AI answer 1.", late.data)
        self.assertNotIn(b"AI answer 2.", late.data)
        self.assertEqual(len(self.ai.calls), 2)
        self.assertEqual(self.used(), 2)

    def test_duplicate_while_reserved_does_not_call_openai_or_consume_quota(self):
        """The original delivery is mid-flight: reservation committed, no reply yet."""
        self.set_limit(3)
        self.start()
        conversation = self.conversation()
        from smartdesk.services import conversations as conversation_service

        conversation_service.store_inbound_message(
            conversation, AI_QUESTION, "voice:CA-voice-1:1"
        )
        usage_service.reserve_ai_reply(self.padel.id, "voice:CA-voice-1:1")
        db.session.commit()

        response = self.turn(turn=1)
        self.assertIn(b"still processing", response.data.lower())
        self.assertIn(b"turn=1", response.data)
        self.assertNotIn(b"<Hangup", response.data)
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservation().status, "reserved")
        self.assertEqual(self.used(), 1)
        self.assertEqual(len(self.messages("customer")), 1)

    def test_retried_handoff_turn_does_not_create_a_second_turn_or_notify_again(self):
        with patch("smartdesk.services.receptionist.notify_escalation") as notify:
            first = self.turn(turn=1, body="I want to speak to a manager")
            retry = self.turn(turn=1, body="I want to speak to a manager")
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(len(self.messages("customer")), 1)
        self.assertEqual(len(self.messages("assistant")), 1)
        self.assertEqual(self.reservations(), [])

    def test_missing_call_sid_is_refused_without_reserving(self):
        self.set_limit(3)
        response = self.client.post(
            "/voice/continue?turn=1",
            data={"To": VOICE_NUMBER, "From": CALLER, "SpeechResult": AI_QUESTION},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservations(), [])


class VoiceQuotaExhaustionTests(VoiceQuotaCase):
    def test_exhausted_quota_never_calls_openai_and_returns_the_limit_response(self):  # 11, 12
        self.set_limit(0)
        self.start()
        response = self.turn()
        self.assertIn(b"monthly conversation limit", response.data.lower())
        self.assertIn(b"<Hangup", response.data)
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.conversation().status, "needs_human")
        self.assertEqual(self.messages("assistant"), [])

    def test_customer_is_stopped_on_the_turn_after_the_last_unit_is_used(self):
        self.set_limit(1)
        self.turn(turn=1)
        response = self.turn(turn=2)
        self.assertIn(b"monthly conversation limit", response.data.lower())
        self.assertIn(b"<Hangup", response.data)
        self.assertEqual(len(self.ai.calls), 1)
        self.assertFalse(self.conversation().human_takeover)
        self.assertEqual(self.conversation().status, "needs_human")
        self.assertEqual(self.used(), 1)

    def test_retry_of_a_quota_refused_turn_repeats_the_limit_response_without_ai(self):
        self.set_limit(0)
        self.turn()
        retry = self.turn()
        self.assertIn(b"monthly conversation limit", retry.data.lower())
        self.assertIn(b"<Hangup", retry.data)
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(len(self.messages("customer")), 1)


class VoiceBypassTests(VoiceQuotaCase):
    def test_human_takeover_does_not_reserve_or_call_openai(self):  # 13
        self.start()
        conversation = self.conversation()
        conversation.human_takeover = True
        db.session.commit()
        response = self.turn()
        self.assertIn(b"team member will continue", response.data.lower())
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.messages("customer"), [])

    def test_suspended_tenant_does_not_reserve_or_call_openai(self):  # 14
        self.padel.status = "suspended"
        db.session.commit()
        response = self.turn()
        self.assertIn(b"could not process your call", response.data.lower())
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservations(), [])

    def test_canned_and_handoff_responses_do_not_consume_ai_quota(self):  # 15
        self.set_limit(1)
        self.turn(turn=1, body="Dumela")
        self.turn(turn=2, body="I want to speak to a manager")
        self.assertEqual(len(self.ai.calls), 0)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.used(), 0)


if __name__ == "__main__":
    unittest.main()
