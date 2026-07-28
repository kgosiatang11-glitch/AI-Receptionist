import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intent_router import detect_intent, route_message


class ReceptionistAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        state_dir = Path(self.temp_dir.name)

        patchers = [
            patch.dict(
                "os.environ",
                {
                    "STATE_DIR": str(state_dir),
                    "MONTHLY_CONVERSATION_LIMIT": "2",
                    "OPENAI_API_KEY": "",
                    "TWILIO_ACCOUNT_SID": "",
                    "TWILIO_AUTH_TOKEN": "",
                },
                clear=False,
            ),
        ]

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        import importlib
        import app

        self.app_module = importlib.reload(app)
        self.client = self.app_module.app.test_client()
        self.state_dir = state_dir

    def post_message(self, body, sender="whatsapp:+26770000000"):
        return self.client.post(
            "/whatsapp",
            data={"Body": body, "From": sender},
        ).data.decode("utf-8")

    def test_first_message_introduces_smartdesk_ai(self):
        response = self.post_message("Hello there")
        self.assertIn("Hello and welcome to SmartDesk AI! I'm O'Brien, your AI Receptionist. How can I assist you today?", response)

    def test_whatsapp_replies_with_xml_twiml(self):
        response = self.client.post(
            "/whatsapp",
            data={"Body": "Hello", "From": "whatsapp:+26770000009"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        self.assertIn("<Response>", response.data.decode("utf-8"))

    def test_openai_failure_returns_a_fallback_reply(self):
        failing_client = type(
            "FailingClient",
            (),
            {
                "chat": type(
                    "Chat",
                    (),
                    {
                        "completions": type(
                            "Completions",
                            (),
                            {"create": lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("API unavailable"))},
                        )()
                    },
                )()
            },
        )()

        with patch.object(self.app_module, "client", failing_client):
            response = self.post_message("Please explain your integrations.")

        self.assertIn("temporary issue", response)

    def test_services_request_lists_smartdesk_services(self):
        response = self.post_message("What services do you offer?")
        self.assertIn("AI WhatsApp Receptionists", response)
        self.assertIn("24/7 Automated Customer Support", response)
        self.assertIn("Appointment &amp; Booking Automation", response)

    def test_how_it_works_reply_lists_the_process(self):
        response = self.post_message("How does SmartDesk AI work?")
        self.assertIn("A business tells us about its services.", response)
        self.assertIn("We connect it to the business's WhatsApp number.", response)
        self.assertIn("never misses customer enquiries", response)

    def test_human_escalation_path_is_reachable(self):
        self.post_message("Hello there")
        response = self.post_message("I need a human please")
        self.assertIn("team member will contact you shortly", response)

    def test_limit_applies_only_after_new_conversations_exceed_cap(self):
        self.post_message("Hello there", sender="whatsapp:+26770000001")
        self.post_message("Hello there", sender="whatsapp:+26770000002")
        response = self.post_message("Hello there", sender="whatsapp:+26770000003")
        self.assertIn("temporarily unavailable", response)

    def test_existing_conversation_does_not_increment_usage(self):
        self.post_message("Hello there", sender="whatsapp:+26770000001")
        self.post_message("What are your hours?", sender="whatsapp:+26770000001")
        usage_count = (self.state_dir / "usage.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(usage_count, "1")

    def test_feature_requests_are_explicit_only(self):
        self.assertEqual(detect_intent("What features do you have?")[0], "features")
        self.assertEqual(detect_intent("What can SmartDesk AI do?")[0], "features")
        self.assertEqual(detect_intent("Show me the features.")[0], "features")
        self.assertEqual(detect_intent("I own a car wash. How can SmartDesk AI help me increase bookings?")[0], "ai")
        self.assertEqual(detect_intent("How would SmartDesk AI help a dental clinic?")[0], "ai")
        self.assertEqual(detect_intent("Compare SmartDesk AI with hiring a receptionist.")[0], "ai")

    def test_human_handoff_is_strict(self):
        self.assertEqual(detect_intent("I want to speak to a human.")[0], "human_handoff")
        self.assertEqual(detect_intent("Can someone call me?")[0], "human_handoff")
        self.assertEqual(detect_intent("Contact your team.")[0], "human_handoff")
        self.assertEqual(detect_intent("I need customer support")[0], "human_handoff")
        self.assertEqual(detect_intent("Can you help me with bookings?")[0], "ai")

    def test_setswana_greeting_and_follow_up_are_natural(self):
        greeting_response = self.post_message("Dumelang", sender="whatsapp:+26770000005")
        self.assertIn("Dumelang!", greeting_response)
        self.assertIn("O'Brien", greeting_response)

        follow_up_response = self.post_message("Ke batla thuso.", sender="whatsapp:+26770000005")
        self.assertIn("nka go thusa", follow_up_response.lower())
        self.assertIn("O batla thuso", follow_up_response)

    def test_ai_reply_uses_recent_conversation_history(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type(
                    "Response",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {
                                    "message": type(
                                        "Message",
                                        (),
                                        {"content": "I remember your earlier request."},
                                    )()
                                },
                            )
                        ]
                    },
                )()

        fake_completions = FakeCompletions()
        fake_client = type(
            "FakeClient",
            (),
            {
                "chat": type(
                    "Chat",
                    (),
                    {"completions": fake_completions},
                )()
            },
        )()

        with patch.object(self.app_module, "client", fake_client):
            self.app_module.generate_ai_reply("Can you help me with bookings?", sender="whatsapp:+26770000004")
            self.app_module.generate_ai_reply("Yes", sender="whatsapp:+26770000004")

        messages = fake_completions.calls[1]["messages"]
        self.assertTrue(any(msg["role"] == "user" and "Can you help me with bookings?" in msg["content"] for msg in messages))
        self.assertTrue(any(msg["role"] == "assistant" for msg in messages))


if __name__ == "__main__":
    unittest.main()
