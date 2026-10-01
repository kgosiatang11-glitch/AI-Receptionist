"""PostgreSQL proof of WhatsApp idempotency (Phase 2).

SQLite cannot demonstrate true concurrency or exercise the real migration, so
these run against a real PostgreSQL database when one is provided:

    createdb smartdesk_test
    TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost/smartdesk_test \\
        python -m pytest tests/test_whatsapp_idempotency_postgres.py

Without ``TEST_DATABASE_URL`` the module is skipped (and says so).  The target
database is WIPED and rebuilt from the Alembic migrations, so its name must
contain "test".  Requests run in separate threads, each with its own app
context and therefore its own DB connection and transaction -- exactly the
situation of two gunicorn workers.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from ai.receptionist import ReceptionistEngine  # noqa: E402
from smartdesk.app import create_app  # noqa: E402
from smartdesk.config import TestConfig  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Channel,
    Conversation,
    Customer,
    Lead,
    Message,
    ReceptionistProfile,
    Tenant,
    UsageEvent,
    utcnow,
)

MIGRATIONS_DIR = str(Path(__file__).resolve().parent.parent / "migrations")
NUMBER = "+26770000001"
CUSTOMER = "+26772222222"
QUESTION = "How much does a court cost per hour?"


def _sid() -> str:
    return f"SM{uuid.uuid4().hex}"


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL not set: PostgreSQL idempotency tests were NOT run",
)
class PostgresWebhookTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dbname = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in dbname:
            raise RuntimeError(f"Refusing to wipe database {dbname!r}: name must contain 'test'")

        class PgConfig(TestConfig):
            SQLALCHEMY_DATABASE_URI = TEST_DATABASE_URL
            SUPABASE_JWT_SECRET = "test-secret-value"
            OPENAI_API_KEY = ""
            TWILIO_VALIDATE_SIGNATURE = False
            SQLALCHEMY_ENGINE_OPTIONS = {"pool_size": 10}

        cls.app = create_app(PgConfig)
        with cls.app.app_context():
            db.session.execute(text("DROP SCHEMA public CASCADE"))
            db.session.execute(text("CREATE SCHEMA public"))
            db.session.commit()
            db.session.remove()
            db.engine.dispose()
            cls._upgrade()

    @classmethod
    def _upgrade(cls, revision: str = "head") -> None:
        from flask_migrate import upgrade

        upgrade(directory=MIGRATIONS_DIR, revision=revision)

    def setUp(self) -> None:
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.execute(text("TRUNCATE tenants, users CASCADE"))
        db.session.commit()

        self.tenant = Tenant(slug="a", name="Business A")
        db.session.add(self.tenant)
        db.session.flush()
        db.session.add_all([
            Channel(tenant_id=self.tenant.id, kind="whatsapp", address=NUMBER),
            ReceptionistProfile(tenant_id=self.tenant.id, sales_mode_enabled=False),
        ])
        db.session.commit()
        self.tenant_id = self.tenant.id

        self.engine_calls = 0
        self.fail_next = 0
        self.engine_delay = 0.0
        self._lock = threading.Lock()
        self.openai_calls = 0

        outer = self
        real_reply = ReceptionistEngine.reply

        def counting_reply(engine, *args, **kwargs):
            with outer._lock:
                outer.engine_calls += 1
                fail = outer.fail_next > 0
                if fail:
                    outer.fail_next -= 1
            time.sleep(outer.engine_delay)   # keep request A in flight while B arrives
            if fail:
                raise RuntimeError("simulated engine failure")
            return real_reply(engine, *args, **kwargs)

        class _Completions:
            def create(self, model, messages):  # noqa: ARG002
                with outer._lock:
                    outer.openai_calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Fake AI reply."))]
                )

        fake = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        for patcher in (
            patch("smartdesk.services.receptionist.openai_client", return_value=fake),
            patch.object(ReceptionistEngine, "reply", counting_reply),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        db.session.remove()
        self.ctx.pop()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def form(sid, body=QUESTION, sender=CUSTOMER):
        return {"From": f"whatsapp:{sender}", "To": f"whatsapp:{NUMBER}",
                "Body": body, "MessageSid": sid}

    def fire_concurrently(self, forms):
        """POST all forms at the same instant from separate threads."""
        barrier = threading.Barrier(len(forms))
        results = [None] * len(forms)

        def worker(i):
            client = self.app.test_client()
            barrier.wait()
            response = client.post("/whatsapp", data=forms[i])
            results[i] = (response.status_code, b"<Message>" in response.data)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(forms))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertTrue(all(r is not None for r in results), "a request never finished")
        return results

    def counts(self):
        db.session.remove()
        q = lambda model, **kw: model.query.filter_by(tenant_id=self.tenant_id, **kw).count()
        return {
            "customers": q(Customer),
            "conversations": q(Conversation),
            "inbound": q(Message, role="customer"),
            "assistant": q(Message, role="assistant"),
            "message_in": q(UsageEvent, kind="message_in"),
            "message_out": q(UsageEvent, kind="message_out"),
            "leads": q(Lead),
        }


class ClaimPrimitiveTests(PostgresWebhookTestCase):
    """The unique index is the arbiter -- proven in isolation.

    The end-to-end tests above are ALSO serialised by unrelated row locks
    (``resolve_channel`` touches the channel row, which every request to one
    number shares until commit), so on their own they cannot show that the
    index is what guarantees exactly-once.  Here two independent transactions
    claim the same (tenant, MessageSid) through *different conversations*, so
    no shared row can serialise them: only the index can.
    """

    def _two_conversations(self):
        ids = []
        for phone in ("+26771110001", "+26771110002"):
            customer = Customer(tenant_id=self.tenant_id, phone=phone)
            db.session.add(customer)
            db.session.flush()
            conv = Conversation(tenant_id=self.tenant_id, session_key=f"whatsapp:{phone}",
                                customer_id=customer.id)
            db.session.add(conv)
            db.session.flush()
            ids.append(conv.id)
        db.session.commit()
        db.session.remove()
        return ids

    def _race(self, first_finishes: str):
        """Transaction A claims and stays open; B claims the same SID from another
        connection.  Returns (b_outcome, b_was_blocked_while_a_open)."""
        from smartdesk.models import Conversation as Conv
        from smartdesk.services import conversations as svc

        conv_a, conv_b = self._two_conversations()
        sid = _sid()
        a_claimed, release_a = threading.Event(), threading.Event()
        b_result = {}

        def tx_a():
            with self.app.app_context():
                svc.store_inbound_message(db.session.get(Conv, conv_a), "from A", sid)
                a_claimed.set()
                release_a.wait(30)
                db.session.commit() if first_finishes == "commit" else db.session.rollback()
                db.session.remove()

        def tx_b():
            with self.app.app_context():
                try:
                    svc.store_inbound_message(db.session.get(Conv, conv_b), "from B", sid)
                    db.session.commit()
                    b_result["outcome"] = "claimed"
                except svc.DuplicateInboundMessage:
                    db.session.rollback()
                    b_result["outcome"] = "duplicate"
                finally:
                    db.session.remove()

        ta = threading.Thread(target=tx_a)
        ta.start()
        self.assertTrue(a_claimed.wait(30))
        tb = threading.Thread(target=tx_b)
        tb.start()
        tb.join(timeout=1.0)                     # B must be waiting on A's uncommitted claim
        blocked = tb.is_alive()
        release_a.set()
        ta.join(30)
        tb.join(30)
        self.assertFalse(tb.is_alive())
        rows = Message.query.filter_by(tenant_id=self.tenant_id, provider_message_id=sid).count()
        db.session.remove()
        return b_result["outcome"], blocked, rows

    def test_second_claim_waits_then_loses_when_the_first_commits(self):
        outcome, blocked, rows = self._race("commit")
        self.assertTrue(blocked, "B did not wait for A's uncommitted claim: nothing arbitrates")
        self.assertEqual(outcome, "duplicate")
        self.assertEqual(rows, 1)

    def test_second_claim_wins_when_the_first_rolls_back(self):
        """A failed request must not leave a claim behind that eats the retry."""
        outcome, blocked, rows = self._race("rollback")
        self.assertTrue(blocked)
        self.assertEqual(outcome, "claimed")
        self.assertEqual(rows, 1)


class ConcurrentDuplicateTests(PostgresWebhookTestCase):
    def test_concurrent_duplicate_for_a_brand_new_customer(self):
        """Test 4: two simultaneous deliveries of one MessageSid, nothing
        exists yet (the customer/conversation race AND the claim race)."""
        self.engine_delay = 0.5
        sid = _sid()
        results = self.fire_concurrently([self.form(sid), self.form(sid)])

        self.assertEqual([r[0] for r in results], [200, 200], "no 5xx / unhandled unique error")
        self.assertEqual(sum(1 for r in results if r[1]), 1, "exactly one request may reply")
        self.assertEqual(self.engine_calls, 1)
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(
            self.counts(),
            {"customers": 1, "conversations": 1, "inbound": 1, "assistant": 1,
             "message_in": 1, "message_out": 1, "leads": 1},
        )

    def test_concurrent_duplicate_on_an_existing_conversation(self):
        """Both requests reach the claim at once (customer/conversation
        already exist), so the unique index alone arbitrates."""
        self.post_one(_sid(), body="Hello earlier message")      # creates customer + conversation
        self.engine_calls = self.openai_calls = 0
        self.engine_delay = 0.5
        before = self.counts()

        sid = _sid()
        results = self.fire_concurrently([self.form(sid, body=QUESTION)] * 3)

        self.assertEqual([r[0] for r in results], [200, 200, 200])
        self.assertEqual(sum(1 for r in results if r[1]), 1)
        self.assertEqual(self.engine_calls, 1)
        after = self.counts()
        self.assertEqual(after["inbound"], before["inbound"] + 1)
        self.assertEqual(after["assistant"], before["assistant"] + 1)
        self.assertEqual(after["message_in"], before["message_in"] + 1)
        self.assertEqual(after["message_out"], before["message_out"] + 1)
        self.assertEqual(after["customers"], 1)
        self.assertEqual(after["conversations"], 1)

    def test_concurrent_distinct_sids_from_a_new_customer_are_both_processed(self):
        """First-message race: distinct messages must both succeed (no
        unique-constraint 500 on customer/conversation) and stay separate."""
        self.engine_delay = 0.3
        results = self.fire_concurrently(
            [self.form(_sid(), body=QUESTION), self.form(_sid(), body="And do you rent rackets?")]
        )
        self.assertEqual([r[0] for r in results], [200, 200])
        self.assertEqual(self.engine_calls, 2)
        counts = self.counts()
        self.assertEqual(counts["customers"], 1)
        self.assertEqual(counts["conversations"], 1)
        self.assertEqual(counts["inbound"], 2)
        self.assertEqual(counts["assistant"], 2)

    def test_a_failed_first_attempt_does_not_block_its_concurrent_duplicate(self):
        """If the request holding the claim fails, PostgreSQL releases it and
        the waiting duplicate is processed -- the message is not lost."""
        self.engine_delay = 0.5
        self.fail_next = 1
        sid = _sid()
        results = self.fire_concurrently([self.form(sid), self.form(sid)])

        self.assertEqual(sorted(r[0] for r in results), [200, 500])
        self.assertEqual(self.engine_calls, 2)           # failed one + the one that took over
        counts = self.counts()
        self.assertEqual(counts["inbound"], 1)
        self.assertEqual(counts["assistant"], 1)
        self.assertEqual(counts["message_in"], 1)

    def test_retry_after_a_5xx_is_processed_exactly_once(self):
        sid = _sid()
        self.fail_next = 1
        self.assertEqual(self.post_one(sid).status_code, 500)
        self.assertEqual(self.counts()["inbound"], 0)
        self.assertEqual(self.post_one(sid).status_code, 200)
        self.assertEqual(self.post_one(sid).status_code, 200)   # redelivery after success
        self.assertEqual(self.counts()["inbound"], 1)
        self.assertEqual(self.counts()["assistant"], 1)

    def post_one(self, sid, body=QUESTION):
        return self.app.test_client().post("/whatsapp", data=self.form(sid, body=body))


class SchemaEnforcementTests(PostgresWebhookTestCase):
    def _conversation(self):
        customer = Customer(tenant_id=self.tenant_id, phone=CUSTOMER)
        db.session.add(customer)
        db.session.flush()
        conv = Conversation(tenant_id=self.tenant_id, session_key=f"whatsapp:{CUSTOMER}",
                            customer_id=customer.id)
        db.session.add(conv)
        db.session.flush()
        return conv

    def test_index_exists_and_is_partial_unique(self):
        row = db.session.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_messages_inbound_provider_id'"
        )).scalar_one()
        self.assertIn("UNIQUE", row)
        self.assertIn("tenant_id, provider_message_id", row)
        self.assertIn("role", row)

    def test_postgres_rejects_a_duplicate_inbound_row(self):
        conv = self._conversation()
        sid = _sid()
        db.session.add(Message(tenant_id=self.tenant_id, conversation_id=conv.id,
                               role="customer", body="a", provider_message_id=sid))
        db.session.commit()
        db.session.add(Message(tenant_id=self.tenant_id, conversation_id=conv.id,
                               role="customer", body="b", provider_message_id=sid))
        with self.assertRaises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_outbound_and_null_ids_are_not_constrained(self):
        conv = self._conversation()
        sid = _sid()
        for role, pid in (("customer", sid), ("human_agent", sid), ("human_agent", sid),
                          ("customer", None), ("customer", None), ("assistant", None)):
            db.session.add(Message(tenant_id=self.tenant_id, conversation_id=conv.id,
                                   role=role, body="x", provider_message_id=pid))
        db.session.commit()


class MigrationTests(PostgresWebhookTestCase):
    """Migration 0005 against a database that already contains duplicates."""

    def test_existing_duplicates_are_preserved_and_the_index_then_applies(self):
        from flask_migrate import downgrade

        customer = Customer(tenant_id=self.tenant_id, phone=CUSTOMER)
        db.session.add(customer)
        db.session.flush()
        conv = Conversation(tenant_id=self.tenant_id, session_key=f"whatsapp:{CUSTOMER}",
                            customer_id=customer.id)
        db.session.add(conv)
        db.session.flush()
        base = utcnow()
        # Seeded with distinct ids: the index exists at head and (correctly)
        # refuses duplicates.  They are made duplicates below, after the
        # downgrade, i.e. in the pre-migration world where C1 could store them.
        for i, (role, sid) in enumerate([("customer", "tmp0"), ("customer", None),
                                         ("customer", "tmp2"), ("customer", "tmp3"),
                                         ("customer", "UNIQUE1"), ("human_agent", "DUP1")]):
            db.session.add(Message(tenant_id=self.tenant_id, conversation_id=conv.id, role=role,
                                   body=f"m{i}", provider_message_id=sid,
                                   created_at=base + timedelta(seconds=i)))
        db.session.commit()
        db.session.remove()

        try:
            downgrade(directory=MIGRATIONS_DIR, revision="0004_tenant_admin_fields")
            # The pre-migration world had no index: prove that, then create the
            # duplicates C1 used to produce (three inbound rows sharing DUP1).
            self.assertIsNone(db.session.execute(text(
                "SELECT 1 FROM pg_indexes WHERE indexname='uq_messages_inbound_provider_id'"
            )).scalar())
            db.session.execute(text(
                "UPDATE messages SET provider_message_id = 'DUP1' "
                "WHERE body IN ('m0', 'm2', 'm3')"))
            db.session.commit()
            db.session.remove()

            self._upgrade()                                   # <- migration under test
            db.session.remove()

            rows = db.session.execute(text(
                "SELECT body, role, provider_message_id, meta->>'duplicate_provider_message_id' "
                "FROM messages ORDER BY created_at, id"
            )).all()
            self.assertEqual(len(rows), 6, "no customer history may be deleted")
            by_body = {r[0]: r for r in rows}
            self.assertEqual(by_body["m0"][2], "DUP1")                 # first keeps the id
            self.assertIsNone(by_body["m2"][2])                        # later ones give it up...
            self.assertEqual(by_body["m2"][3], "DUP1")                 # ...but it is preserved
            self.assertIsNone(by_body["m3"][2])
            self.assertEqual(by_body["m3"][3], "DUP1")
            self.assertEqual(by_body["m4"][2], "UNIQUE1")              # untouched
            self.assertEqual(by_body["m5"][2], "DUP1")                 # outbound row untouched
            self.assertIsNone(by_body["m5"][3])
            self.assertEqual(db.session.execute(text(
                "SELECT count(*) FROM pg_indexes WHERE indexname='uq_messages_inbound_provider_id'"
            )).scalar_one(), 1)
        finally:
            db.session.remove()
            self._upgrade()          # leave the shared test DB at head for other tests

    def test_upgrade_is_idempotent_and_downgrade_round_trips(self):
        from flask_migrate import downgrade

        downgrade(directory=MIGRATIONS_DIR, revision="0004_tenant_admin_fields")
        self._upgrade()
        self._upgrade()                                   # already at head: no-op
        downgrade(directory=MIGRATIONS_DIR, revision="0004_tenant_admin_fields")
        self._upgrade()
        db.session.remove()
        self.assertEqual(db.session.execute(text(
            "SELECT count(*) FROM pg_indexes WHERE indexname='uq_messages_inbound_provider_id'"
        )).scalar_one(), 1)


if __name__ == "__main__":
    unittest.main()
