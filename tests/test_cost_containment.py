"""Phase 3.2 tests: bounded OpenAI request cost.

Covers the output-token cap, token-usage capture, conversation-history bound,
knowledge/context bound, limit validation (an invalid value must never become
"unlimited"), and that the existing inbound-message limit still works.  All
OpenAI calls are faked; nothing here touches the network.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from ai.limits import (  # noqa: E402
    DEFAULT_MAX_HISTORY_MESSAGES,
    DEFAULT_MAX_KNOWLEDGE_CHARS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    KNOWLEDGE_TRUNCATION_NOTICE,
    bound_knowledge,
    safe_limit,
)
from ai.receptionist import ReceptionistEngine  # noqa: E402
from smartdesk.config import _limit  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import KnowledgeDocument, Message, UsageEvent  # noqa: E402
from smartdesk.services import conversations as conversation_service  # noqa: E402
from smartdesk.services.knowledge import knowledge_context  # noqa: E402
from tests.test_whatsapp_idempotency import (  # noqa: E402
    AI_QUESTION,
    CUSTOMER,
    WhatsAppWebhookTestCase,
    new_sid,
)

#: A message that is not a greeting/handoff/booking/canned-intent, so it always
#: reaches the (faked) OpenAI call.
AI_MESSAGE = "How much does a court cost per hour?"
KNOWLEDGE_MARK = "BUSINESS KNOWLEDGE (verified):\n"
CUSTOMER_MARK = "\n\nCustomer message: "
INTENT_MARK = "\n\nDetected intent:"


class RecordingOpenAI:
    """Fake OpenAI client that records every request's kwargs."""

    def __init__(self, usage="default") -> None:
        self.requests: list[dict] = []
        outer = self
        self._usage = usage

        class _Completions:
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                response = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Fake AI reply."))]
                )
                if outer._usage == "default":
                    response.usage = SimpleNamespace(
                        prompt_tokens=123, completion_tokens=45, total_tokens=168
                    )
                elif outer._usage is not None:
                    response.usage = outer._usage
                return response  # usage=None -> attribute absent entirely

        self.chat = SimpleNamespace(completions=_Completions())


def make_engine(client, history=(), knowledge="Courts are open daily.", **limits):
    return ReceptionistEngine(
        client_provider=lambda: client,
        model="gpt-4o-mini",
        history_loader=lambda _sid: list(history),
        history_appender=lambda *_a: None,
        escalation_notifier=lambda *_a: None,
        knowledge_provider=lambda: knowledge,
        knowledge_dict_provider=lambda: {},
        **limits,
    )


def history_of(n: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"history-{i:03d}"}
        for i in range(n)
    ]


def knowledge_in(request: dict) -> str:
    prompt = request["messages"][-1]["content"]
    return prompt.split(KNOWLEDGE_MARK, 1)[1].split(CUSTOMER_MARK, 1)[0]


def customer_message_in(request: dict) -> str:
    prompt = request["messages"][-1]["content"]
    return prompt.split(CUSTOMER_MARK, 1)[1].split(INTENT_MARK, 1)[0]


class OutputTokenCapTests(unittest.TestCase):
    def test_request_carries_the_default_output_cap(self):  # A
        client = RecordingOpenAI()
        make_engine(client).generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertEqual(client.requests[0]["max_completion_tokens"], DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertEqual(DEFAULT_MAX_OUTPUT_TOKENS, 400)

    def test_request_carries_a_configured_output_cap(self):  # A
        client = RecordingOpenAI()
        make_engine(client, max_output_tokens=150).generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertEqual(client.requests[0]["max_completion_tokens"], 150)

    def test_invalid_engine_limits_fall_back_never_unlimited(self):
        for bad in (0, -1, "abc", True, 10**9, 1.5):
            with self.subTest(bad=bad):
                client = RecordingOpenAI()
                make_engine(client, max_output_tokens=bad).generate_openai_reply(
                    AI_MESSAGE, "s", "whatsapp"
                )
                self.assertEqual(
                    client.requests[0]["max_completion_tokens"], DEFAULT_MAX_OUTPUT_TOKENS
                )


class UsageCaptureTests(unittest.TestCase):
    def test_usage_is_captured_when_present(self):  # B
        engine = make_engine(RecordingOpenAI())
        engine.generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertEqual(
            engine.last_usage,
            {
                "model": "gpt-4o-mini",
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "total_tokens": 168,
            },
        )

    def test_missing_usage_does_not_crash_and_is_unknown_not_estimated(self):  # C
        class UsageNone(RecordingOpenAI):
            """Response object whose ``usage`` attribute exists but is None."""

            def __init__(self) -> None:
                super().__init__(usage=None)
                original = self.chat.completions.create

                def create(**kwargs):
                    response = original(**kwargs)
                    response.usage = None
                    return response

                self.chat.completions.create = create

        for label, client in (
            ("usage attribute absent", RecordingOpenAI(usage=None)),
            ("usage is None", UsageNone()),
        ):
            with self.subTest(label):
                engine = make_engine(client)
                reply = engine.generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
                self.assertEqual(reply, "Fake AI reply.")
                self.assertEqual(
                    engine.last_usage,
                    {
                        "model": "gpt-4o-mini",
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                    },
                )

    def test_partial_usage_keeps_unknown_fields_none(self):
        engine = make_engine(RecordingOpenAI(usage=SimpleNamespace(prompt_tokens=10)))
        engine.generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertEqual(engine.last_usage["prompt_tokens"], 10)
        self.assertIsNone(engine.last_usage["completion_tokens"])
        self.assertIsNone(engine.last_usage["total_tokens"])  # not computed

    def test_no_openai_call_means_no_usage(self):
        engine = make_engine(None)  # no client -> canned fallback reply
        engine.generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertIsNone(engine.last_usage)

    def test_usage_is_reset_between_replies(self):
        client = RecordingOpenAI()
        engine = make_engine(client)
        engine.generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertIsNotNone(engine.last_usage)
        engine._client_provider = lambda: None
        engine.generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        self.assertIsNone(engine.last_usage)


class HistoryBoundTests(unittest.TestCase):
    def test_history_is_bounded_to_the_configured_limit(self):  # D
        client = RecordingOpenAI()
        make_engine(client, history=history_of(50), max_history_messages=7).generate_openai_reply(
            AI_MESSAGE, "s", "whatsapp"
        )
        messages = client.requests[0]["messages"]
        # system prompt + 7 history + current user prompt
        self.assertEqual(len(messages), 1 + 7 + 1)
        self.assertEqual(sum(1 for m in messages if m["content"].startswith("history-")), 7)

    def test_default_limit_applies_when_not_configured(self):  # D
        client = RecordingOpenAI()
        make_engine(client, history=history_of(100)).generate_openai_reply(
            AI_MESSAGE, "s", "whatsapp"
        )
        self.assertEqual(len(client.requests[0]["messages"]), 1 + DEFAULT_MAX_HISTORY_MESSAGES + 1)

    def test_newest_messages_are_retained_in_order(self):  # E
        client = RecordingOpenAI()
        make_engine(client, history=history_of(50), max_history_messages=5).generate_openai_reply(
            AI_MESSAGE, "s", "whatsapp"
        )
        kept = [m["content"] for m in client.requests[0]["messages"][1:-1]]
        self.assertEqual(kept, [f"history-{i:03d}" for i in range(45, 50)])

    def test_system_prompt_and_current_message_survive_the_tightest_bound(self):  # F
        client = RecordingOpenAI()
        make_engine(client, history=history_of(50), max_history_messages=1).generate_openai_reply(
            "Is parking available?", "s", "whatsapp"
        )
        messages = client.requests[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("Customer message: Is parking available?", messages[-1]["content"])
        self.assertEqual(len(messages), 3)

    def test_short_history_is_untouched(self):
        client = RecordingOpenAI()
        make_engine(client, history=history_of(3), max_history_messages=10).generate_openai_reply(
            AI_MESSAGE, "s", "whatsapp"
        )
        self.assertEqual(len(client.requests[0]["messages"]), 1 + 3 + 1)


class KnowledgeBoundTests(unittest.TestCase):
    def test_oversized_knowledge_is_bounded_before_the_request(self):  # G
        client = RecordingOpenAI()
        make_engine(
            client, knowledge="K" * 100_000, max_knowledge_chars=1000
        ).generate_openai_reply(AI_MESSAGE, "s", "whatsapp")
        sent = knowledge_in(client.requests[0])
        self.assertLessEqual(len(sent), 1000)
        self.assertTrue(sent.endswith(KNOWLEDGE_TRUNCATION_NOTICE))

    def test_default_knowledge_limit_applies_when_not_configured(self):  # G
        client = RecordingOpenAI()
        make_engine(client, knowledge="K" * 500_000).generate_openai_reply(
            AI_MESSAGE, "s", "whatsapp"
        )
        self.assertLessEqual(len(knowledge_in(client.requests[0])), DEFAULT_MAX_KNOWLEDGE_CHARS)

    def test_small_knowledge_is_sent_unchanged(self):
        client = RecordingOpenAI()
        make_engine(client, knowledge="Open 9-5.", max_knowledge_chars=1000).generate_openai_reply(
            AI_MESSAGE, "s", "whatsapp"
        )
        self.assertEqual(knowledge_in(client.requests[0]), "Open 9-5.")

    def test_bound_knowledge_is_deterministic_and_never_exceeds_the_limit(self):
        text = "abcdefghij" * 500
        self.assertEqual(bound_knowledge(text, 400), bound_knowledge(text, 400))
        for limit in (1, 10, len(KNOWLEDGE_TRUNCATION_NOTICE), 400, 4999):
            self.assertLessEqual(len(bound_knowledge(text, limit)), limit)
        self.assertEqual(bound_knowledge(text, len(text)), text)


class LimitValidationTests(unittest.TestCase):
    def test_safe_limit_never_returns_unlimited(self):
        for bad in (0, -1, -999, "", "abc", "1.5", "0", "-5", True, False, 10**9, 2.5, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(safe_limit(bad, 40, 100), 40)

    def test_safe_limit_accepts_valid_values(self):
        self.assertEqual(safe_limit(7, 40, 100), 7)
        self.assertEqual(safe_limit(" 7 ", 40, 100), 7)
        self.assertEqual(safe_limit(100, 40, 100), 100)
        self.assertEqual(safe_limit(None, 40, 100), 40)

    def test_environment_variable_absent_or_invalid_uses_default(self):
        name = "RECEPTIONIST_MAX_OUTPUT_TOKENS"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(name, None)
            self.assertEqual(_limit(name, 400, 2000), 400)
        for bad in ("abc", "0", "-3", "", "99999999", "1e3"):
            with self.subTest(bad=bad), patch.dict(os.environ, {name: bad}):
                self.assertEqual(_limit(name, 400, 2000), 400)
        with patch.dict(os.environ, {name: "250"}):
            self.assertEqual(_limit(name, 400, 2000), 250)


class _WebhookCostCase(WhatsAppWebhookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.recording = RecordingOpenAI()
        patcher = patch(
            "smartdesk.services.receptionist.openai_client", return_value=self.recording
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def last_request(self) -> dict:
        return self.recording.requests[-1]


class WebhookIntegrationTests(_WebhookCostCase):
    def test_webhook_path_sends_the_output_cap(self):  # A
        self.post(new_sid())
        self.assertEqual(
            self.last_request()["max_completion_tokens"],
            self.app.config["RECEPTIONIST_MAX_OUTPUT_TOKENS"],
        )
        self.assertEqual(self.last_request()["max_completion_tokens"], DEFAULT_MAX_OUTPUT_TOKENS)

    def test_inbound_message_limit_still_enforced(self):  # H
        limit = self.app.config["MAX_INBOUND_MESSAGE_CHARS"]
        self.assertEqual(limit, 1500)
        self.post(new_sid(), body="How much does a court cost? " + "z" * 4000)
        sent = customer_message_in(self.last_request())
        self.assertEqual(len(sent), 1500)
        stored = Message.query.filter_by(
            conversation_id=self.conversation().id, role="customer"
        ).one()
        self.assertEqual(len(stored.body), 1500)

    def test_webhook_history_is_bounded_and_current_message_kept(self):  # D E F
        self.app.config["MAX_CONVERSATION_HISTORY"] = 4
        conv = None
        for i in range(8):
            self.post(new_sid(), body=f"{AI_QUESTION} variant {i}")
            conv = conv or self.conversation()
        # 8 turns -> 16 stored messages (+ nothing else); the last request must
        # hold system + at most 4 history + the current message.
        request = self.last_request()
        self.assertLessEqual(len(request["messages"]), 1 + 4 + 1)
        self.assertIn("variant 7", request["messages"][-1]["content"])
        history_text = " ".join(m["content"] for m in request["messages"][1:-1])
        self.assertNotIn("variant 0", history_text)
        self.assertIn("variant 6", history_text)  # newest prior turn retained

    def test_invalid_history_config_cannot_mean_unlimited(self):
        conv = None
        for i in range(30):
            self.post(new_sid(), body=f"{AI_QUESTION} variant {i}")
        conv = self.conversation()
        for bad in (-1, 0, "oops"):
            with self.subTest(bad=bad):
                self.app.config["MAX_CONVERSATION_HISTORY"] = bad
                loaded = conversation_service.load_history(conv)
                self.assertEqual(len(loaded), DEFAULT_MAX_HISTORY_MESSAGES)

    def test_webhook_knowledge_is_bounded_but_stored_records_are_not(self):  # G
        big = "Policy sentence. " * 5000  # ~85k chars
        db.session.add(
            KnowledgeDocument(
                tenant_id=self.padel.id, section="policies", title="Policies",
                body=big, is_published=True,
            )
        )
        db.session.commit()
        self.app.config["RECEPTIONIST_MAX_KNOWLEDGE_CHARS"] = 2000
        self.post(new_sid())
        self.assertLessEqual(len(knowledge_in(self.last_request())), 2000)

        stored = KnowledgeDocument.query.filter_by(
            tenant_id=self.padel.id, section="policies"
        ).one()
        self.assertEqual(stored.body, big)  # nothing dropped from the database
        self.assertGreater(len(knowledge_context(self.padel.id)), 2000)

    def test_knowledge_serialisation_is_reproducible(self):
        for section in ("services", "policies", "faqs"):
            db.session.add(
                KnowledgeDocument(
                    tenant_id=self.padel.id, section=section, title=section,
                    body=f"{section} body", is_published=True,
                )
            )
        db.session.commit()
        self.assertEqual(knowledge_context(self.padel.id), knowledge_context(self.padel.id))
        keys = list(__import__("json").loads(knowledge_context(self.padel.id)))
        self.assertEqual(keys, sorted(keys))


class UsagePersistenceTests(_WebhookCostCase):
    def message_out_meta(self) -> dict:
        event = UsageEvent.query.filter_by(tenant_id=self.padel.id, kind="message_out").one()
        return event.meta

    def test_usage_is_stored_in_the_existing_usage_event_meta(self):  # B
        self.post(new_sid())
        self.assertEqual(
            self.message_out_meta()["openai_usage"],
            {
                "model": self.app.config["OPENAI_MODEL"],
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "total_tokens": 168,
            },
        )

    def test_missing_usage_is_recorded_as_unknown_and_reply_still_sent(self):  # C
        self.recording._usage = None
        response = self.post(new_sid())
        self.assertTrue(self.has_reply(response))
        usage = self.message_out_meta()["openai_usage"]
        self.assertEqual(
            (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]),
            (None, None, None),
        )

    def test_no_openai_call_records_no_usage(self):
        self.post(new_sid(), body="Dumela")  # canned Setswana greeting, no AI call
        self.assertEqual(self.recording.requests, [])
        self.assertNotIn("openai_usage", self.message_out_meta())


class TenantIsolationUnderBoundsTests(_WebhookCostCase):
    def test_bounded_knowledge_and_history_never_leak_across_tenants(self):  # J
        db.session.add_all([
            KnowledgeDocument(
                tenant_id=self.padel.id, section="policies", title="Policies",
                body="PADEL-ONLY-FACT", is_published=True,
            ),
            KnowledgeDocument(
                tenant_id=self.salon.id, section="policies", title="Policies",
                body="SALON-ONLY-FACT", is_published=True,
            ),
        ])
        db.session.commit()
        self.post(new_sid(), body=f"{AI_QUESTION} padel", to="+26770000001")
        self.post(new_sid(), body=f"{AI_QUESTION} salon", to="+26770000002")
        padel_request, salon_request = self.recording.requests[-2:]
        padel_text = str(padel_request["messages"])
        salon_text = str(salon_request["messages"])
        self.assertIn("PADEL-ONLY-FACT", padel_text)
        self.assertNotIn("SALON-ONLY-FACT", padel_text)
        self.assertIn("SALON-ONLY-FACT", salon_text)
        self.assertNotIn("PADEL-ONLY-FACT", salon_text)
        self.assertNotIn("padel", " ".join(m["content"] for m in salon_request["messages"][1:-1]))


if __name__ == "__main__":
    unittest.main()
