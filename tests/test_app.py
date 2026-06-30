import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.assertIn("SmartDesk AI", response)
        self.assertIn("24 hours a day", response)
        self.assertIn("7 days a week", response)

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


if __name__ == "__main__":
    unittest.main()
