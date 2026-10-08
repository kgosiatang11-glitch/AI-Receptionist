"""PostgreSQL concurrency proof for voice AI-usage reservations.

Requires TEST_DATABASE_URL, like the existing WhatsApp PostgreSQL quota suite.
"""

from __future__ import annotations

import threading
import unittest

from smartdesk.extensions import db
from smartdesk.models import Channel, Conversation, Message, UsageReservation
from tests.test_whatsapp_quota_postgres import LIMIT_TEXT, QuotaPgCase

VOICE_NUMBER = "+26770000003"
QUESTION = "How much does a court cost per hour?"


class VoiceQuotaPgCase(QuotaPgCase):
    def setUp(self) -> None:
        super().setUp()
        db.session.add(Channel(tenant_id=self.tenant_id, kind="voice", address=VOICE_NUMBER))
        db.session.commit()

    def start(self, call_sid: str) -> None:
        client = self.app.test_client()
        response = client.post(
            "/voice", data={"To": VOICE_NUMBER, "From": "+26772222222", "CallSid": call_sid}
        )
        self.assertEqual(response.status_code, 200)

    def fire_turns(self, call_sids: list[str], turn: int = 1):
        barrier = threading.Barrier(len(call_sids))
        results = [None] * len(call_sids)

        def worker(index: int) -> None:
            client = self.app.test_client()
            barrier.wait()
            response = client.post(
                f"/voice/continue?turn={turn}",
                data={
                    "To": VOICE_NUMBER,
                    "From": "+26772222222",
                    "CallSid": call_sids[index],
                    "SpeechResult": QUESTION,
                },
            )
            results[index] = (response.status_code, response.data)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(call_sids))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        self.assertTrue(all(result is not None for result in results), "a voice request never finished")
        return results


    def turn(self, call_sid: str, turn: int = 1):
        response = self.app.test_client().post(
            f"/voice/continue?turn={turn}",
            data={"To": VOICE_NUMBER, "From": "+26772222222",
                  "CallSid": call_sid, "SpeechResult": QUESTION},
        )
        self.assertEqual(response.status_code, 200)
        return response.data

    def voice_messages(self, call_sid: str, role: str):
        db.session.remove()
        conversation = Conversation.query.filter_by(
            tenant_id=self.tenant_id, session_key=f"voice:{call_sid}"
        ).one()
        rows = Message.query.filter_by(conversation_id=conversation.id, role=role).all()
        db.session.remove()
        return rows


class VoiceConcurrentQuotaTests(VoiceQuotaPgCase):
    def test_concurrent_voice_calls_cannot_take_the_same_last_unit(self):
        self.set_limit(1)
        self.ai_delay = 0.3
        self.start("CA-voice-a")
        self.start("CA-voice-b")

        results = self.fire_turns(["CA-voice-a", "CA-voice-b"])
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(sum(LIMIT_TEXT in result[1].lower() for result in results), 1)
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(self.reservation_states(), {"committed": 1})
        self.assertEqual(self.used(), 1)

    def test_concurrent_duplicate_voice_callback_has_one_reservation_and_one_ai_call(self):
        self.set_limit(3)
        self.ai_delay = 0.3
        self.start("CA-voice-duplicate")

        results = self.fire_turns(["CA-voice-duplicate", "CA-voice-duplicate"])
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(self.reservation_states(), {"committed": 1})
        self.assertEqual(self.used(), 1)
        db.session.remove()
        rows = UsageReservation.query.filter_by(tenant_id=self.tenant_id).all()
        self.assertEqual([row.idempotency_key for row in rows], ["voice:CA-voice-duplicate:1"])
        db.session.remove()
        # Exactly one customer row and one reply for the turn, tagged with its key.
        customers = self.voice_messages("CA-voice-duplicate", "customer")
        assistants = self.voice_messages("CA-voice-duplicate", "assistant")
        self.assertEqual([m.provider_message_id for m in customers], ["voice:CA-voice-duplicate:1"])
        self.assertEqual([m.provider_message_id for m in assistants], ["voice:CA-voice-duplicate:1"])

    def test_many_concurrent_duplicates_still_make_one_ai_call(self):
        self.set_limit(5)
        self.ai_delay = 0.3
        self.start("CA-voice-many")
        results = self.fire_turns(["CA-voice-many"] * 5)
        self.assertEqual([result[0] for result in results], [200] * 5)
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(self.reservation_states(), {"committed": 1})
        self.assertEqual(self.used(), 1)
        self.assertEqual(len(self.voice_messages("CA-voice-many", "customer")), 1)

    def test_duplicate_arriving_during_openai_does_not_double_spend(self):
        """RESERVED in the database while OpenAI runs: the duplicate is told the
        turn is in flight, with no second reservation and no second call."""
        self.set_limit(3)
        self.ai_delay = 0.6
        self.start("CA-voice-flight")
        results = self.fire_turns(["CA-voice-flight", "CA-voice-flight"])
        bodies = [result[1].lower() for result in results]
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(self.used(), 1)
        self.assertTrue(
            any(b"still processing" in b or b"fake ai reply" in b for b in bodies)
        )
        self.assertEqual(self.reservation_states(), {"committed": 1})

    def test_retry_after_commit_replays_the_exact_turn_with_no_new_quota(self):
        self.set_limit(3)
        self.start("CA-voice-replay")
        self.turn("CA-voice-replay", 1)
        self.turn("CA-voice-replay", 2)
        calls = self.openai_calls
        retry = self.turn("CA-voice-replay", 1)
        self.assertIn(b"fake ai reply", retry.lower())
        self.assertEqual(self.openai_calls, calls)
        self.assertEqual(self.reservation_states(), {"committed": 2})
        self.assertEqual(self.used(), 2)

    def test_failed_turn_is_released_and_its_retry_does_not_replay_an_older_reply(self):
        self.set_limit(3)
        self.start("CA-voice-fail")
        self.turn("CA-voice-fail", 1)
        self.fail_openai = True
        self.turn("CA-voice-fail", 2)
        self.fail_openai = False
        calls = self.openai_calls
        self.assertEqual(self.reservation_states(), {"committed": 1, "released": 1})
        self.assertEqual(self.used(), 1)
        self.assertEqual(len(self.voice_messages("CA-voice-fail", "assistant")), 1)

        retry = self.turn("CA-voice-fail", 2)
        self.assertNotIn(b"fake ai reply", retry.lower())
        self.assertEqual(self.openai_calls, calls)
        self.assertEqual(self.reservation_states(), {"committed": 1, "released": 1})
        self.assertEqual(self.used(), 1)

    def test_committed_voice_reservation_stores_reported_tokens(self):
        self.set_limit(2)
        self.start("CA-voice-tokens")
        self.turn("CA-voice-tokens", 1)
        db.session.remove()
        row = UsageReservation.query.filter_by(tenant_id=self.tenant_id).one()
        self.assertEqual(
            (row.status, row.prompt_tokens, row.completion_tokens, row.total_tokens),
            ("committed", 10, 5, 15),
        )
        db.session.remove()


if __name__ == "__main__":
    unittest.main()
