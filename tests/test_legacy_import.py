"""Tests for smartdesk.legacy_import.

Fixtures reproduce the exact flat-file formats written by the original
app.py (see USAGE_FILE/SESSIONS_FILE/USERS_FILE/CONVERSATION_HISTORY_FILE),
not a simplified stand-in, so a format drift in either file would be caught
here.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from click.testing import CliRunner  # noqa: E402

from smartdesk.app import create_app  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.legacy_import import legacy_import_command  # noqa: E402
from smartdesk.models import Conversation, Customer, Message, Tenant  # noqa: E402
from tests.test_multitenant import SQLiteTestConfig  # noqa: E402


class LegacyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(SQLiteTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.test_tenant = Tenant(
            slug="test-business", name="Test Business", status="development",
            is_test_data=True,
        )
        self.production_tenant = Tenant(
            slug="10by20", name="10by20 Padel Club", business_type="sports",
        )
        db.session.add_all([self.test_tenant, self.production_tenant])
        db.session.commit()

        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmpdir.name)
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _write_legacy_files(
        self, sessions="", users="", history=None, usage="3", bot_state="on"
    ):
        (self.state_dir / "sessions.txt").write_text(sessions, encoding="utf-8")
        (self.state_dir / "users.txt").write_text(users, encoding="utf-8")
        (self.state_dir / "usage.txt").write_text(usage, encoding="utf-8")
        (self.state_dir / "bot_state.txt").write_text(bot_state, encoding="utf-8")
        (self.state_dir / "conversation_history.json").write_text(
            json.dumps(history or {}), encoding="utf-8"
        )

    def _invoke(self, *extra_args):
        return self.runner.invoke(
            legacy_import_command,
            ["--state-dir", str(self.state_dir), *extra_args],
            obj=None,
        )

    def test_import_creates_customer_conversation_and_messages(self):
        self._write_legacy_files(
            sessions="whatsapp:+26771111111|2026-01-01T10:00:00\n",
            users="whatsapp:+26771111111\n",
            history={
                "whatsapp:+26771111111": [
                    {"role": "user", "content": "Hi, are you open Saturday?"},
                    {"role": "assistant", "content": "Yes, 8am to 6pm."},
                ]
            },
        )
        result = self._invoke()
        self.assertEqual(result.exit_code, 0, result.output)

        customer = Customer.query.filter_by(
            tenant_id=self.test_tenant.id, phone="+26771111111"
        ).one()
        self.assertTrue(customer.is_test_data)

        conversation = Conversation.query.filter_by(
            tenant_id=self.test_tenant.id, session_key="whatsapp:+26771111111"
        ).one()
        self.assertTrue(conversation.is_test_data)
        self.assertEqual(conversation.customer_id, customer.id)

        messages = Message.query.filter_by(conversation_id=conversation.id).all()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "customer")
        self.assertEqual(messages[1].role, "assistant")

    def test_voice_sessions_do_not_get_a_customer_phone(self):
        self._write_legacy_files(
            history={"voice:CA123": [{"role": "user", "content": "What are your hours?"}]}
        )
        self._invoke()
        conversation = Conversation.query.filter_by(
            tenant_id=self.test_tenant.id, session_key="voice:CA123"
        ).one()
        self.assertIsNone(conversation.customer_id)
        self.assertEqual(conversation.channel_kind, "voice")

    def test_imported_data_never_lands_in_a_production_tenant(self):
        self._write_legacy_files(
            history={"whatsapp:+26772222222": [{"role": "user", "content": "Hello"}]}
        )
        self._invoke()
        self.assertEqual(
            Customer.query.filter_by(tenant_id=self.production_tenant.id).count(), 0
        )
        self.assertEqual(
            Conversation.query.filter_by(tenant_id=self.production_tenant.id).count(), 0
        )

    def test_rerunning_the_import_does_not_duplicate_records(self):
        self._write_legacy_files(
            history={
                "whatsapp:+26773333333": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello!"},
                ]
            }
        )
        self._invoke()
        self._invoke()  # re-run against the same files
        self.assertEqual(
            Customer.query.filter_by(
                tenant_id=self.test_tenant.id, phone="+26773333333"
            ).count(),
            1,
        )
        conversation = Conversation.query.filter_by(
            tenant_id=self.test_tenant.id, session_key="whatsapp:+26773333333"
        ).one()
        self.assertEqual(
            Message.query.filter_by(conversation_id=conversation.id).count(), 2
        )

    def test_rerunning_after_new_messages_appends_only_the_new_ones(self):
        self._write_legacy_files(
            history={"whatsapp:+26774444444": [{"role": "user", "content": "Hi"}]}
        )
        self._invoke()
        self._write_legacy_files(
            history={
                "whatsapp:+26774444444": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello, how can I help?"},
                ]
            }
        )
        self._invoke()
        conversation = Conversation.query.filter_by(
            tenant_id=self.test_tenant.id, session_key="whatsapp:+26774444444"
        ).one()
        self.assertEqual(
            Message.query.filter_by(conversation_id=conversation.id).count(), 2
        )

    def test_dry_run_writes_nothing(self):
        self._write_legacy_files(
            history={"whatsapp:+26775555555": [{"role": "user", "content": "Hi"}]}
        )
        result = self._invoke("--dry-run")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("would import", result.output)
        self.assertEqual(Customer.query.count(), 0)
        self.assertEqual(Conversation.query.count(), 0)

    def test_empty_legacy_state_imports_nothing(self):
        self._write_legacy_files()
        result = self._invoke()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(Customer.query.count(), 0)

    def test_missing_state_dir_is_a_clean_error(self):
        result = self.runner.invoke(
            legacy_import_command, ["--state-dir", "/no/such/path"]
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_missing_test_tenant_is_a_clean_error(self):
        db.session.delete(self.test_tenant)
        db.session.commit()
        self._write_legacy_files(
            history={"whatsapp:+26776666666": [{"role": "user", "content": "Hi"}]}
        )
        result = self._invoke()
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("test-business", result.output)


if __name__ == "__main__":
    unittest.main()
