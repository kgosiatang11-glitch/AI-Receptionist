"""PostgreSQL proof of the Phase 3.3B WhatsApp quota wiring.

Real concurrent requests (separate threads, separate connections, real
transactions) against real PostgreSQL.  Skipped without ``TEST_DATABASE_URL``.
The invariant under test: OpenAI calls == successful reservations, always,
and OpenAI is never called while a database transaction is open.
"""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text

from smartdesk.extensions import db
from smartdesk.models import Channel, ReceptionistProfile, Tenant, UsageReservation
from smartdesk.services import usage as usage_service
from tests.test_whatsapp_idempotency_postgres import (
    CUSTOMER,
    NUMBER,
    QUESTION,
    PostgresWebhookTestCase,
    _sid,
)

LIMIT_TEXT = b"monthly conversation limit"
SECOND_NUMBER = "+26770000002"


class QuotaPgCase(PostgresWebhookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ai_delay = 0.0
        self.in_txn_at_call: list[bool] = []
        self.idle_in_txn_seen: list[int] = []
        self.successful_ai_calls = 0
        self.fail_openai = False
        outer = self

        class _Completions:
            def create(self, model, messages, **kwargs):  # noqa: ARG002
                try:
                    in_txn = db.session().in_transaction()
                except RuntimeError:
                    in_txn = False
                with outer._lock:
                    outer.openai_calls += 1
                    outer.in_txn_at_call.append(in_txn)
                # Other connections: is anything (this request) idle in a transaction?
                with db.engine.connect() as probe:
                    n = probe.execute(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND state = 'idle in transaction' "
                        "AND query ILIKE '%usage_reservations%'")).scalar()
                with outer._lock:
                    outer.idle_in_txn_seen.append(n)
                time.sleep(outer.ai_delay)
                if outer.fail_openai:
                    raise RuntimeError("simulated OpenAI failure")
                with outer._lock:
                    outer.successful_ai_calls += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="Fake AI reply."))],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                )

        fake = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        p = patch("smartdesk.services.receptionist.openai_client", return_value=fake)
        p.start()
        self.addCleanup(p.stop)

    def set_limit(self, limit, tenant_id=None):
        db.session.execute(
            text("UPDATE tenants SET monthly_conversation_limit = :l WHERE id = :t"),
            {"l": limit, "t": tenant_id or self.tenant_id})
        db.session.commit()
        db.session.remove()

    def second_tenant(self):
        t = Tenant(slug="b", name="Business B")
        db.session.add(t)
        db.session.flush()
        db.session.add_all([
            Channel(tenant_id=t.id, kind="whatsapp", address=SECOND_NUMBER),
            ReceptionistProfile(tenant_id=t.id, sales_mode_enabled=False),
        ])
        db.session.commit()
        tid = t.id
        db.session.remove()
        return tid

    def fire(self, jobs):
        """jobs: list of (to_number, sid, sender). Returns [(status, body)]."""
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(i):
            to, sid, sender = jobs[i]
            client = self.app.test_client()
            data = {"From": f"whatsapp:{sender}", "To": f"whatsapp:{to}",
                    "Body": QUESTION, "MessageSid": sid}
            barrier.wait()
            r = client.post("/whatsapp", data=data)
            results[i] = (r.status_code, r.data)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(len(jobs))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=90)
        self.assertTrue(all(r is not None for r in results), "a request never finished")
        return results

    def reservation_states(self, tenant_id=None):
        db.session.remove()
        rows = UsageReservation.query.filter_by(tenant_id=tenant_id or self.tenant_id).all()
        out = {}
        for r in rows:
            out[r.status] = out.get(r.status, 0) + 1
        db.session.remove()
        return out

    def used(self, tenant_id=None):
        db.session.remove()
        v = db.session.execute(text(
            "SELECT coalesce(sum(used_units),0) FROM tenant_usage_periods WHERE tenant_id=:t"),
            {"t": tenant_id or self.tenant_id}).scalar()
        db.session.remove()
        return v


class ConcurrentQuotaTests(QuotaPgCase):
    def test_two_concurrent_different_sids_one_unit_left(self):
        self.set_limit(1)
        self.ai_delay = 0.3
        results = self.fire([(NUMBER, _sid(), "+26773000001"), (NUMBER, _sid(), "+26773000002")])
        self.assertEqual([r[0] for r in results], [200, 200])
        limited = [r for r in results if LIMIT_TEXT in r[1].lower()]
        answered = [r for r in results if b"Fake AI reply." in r[1]]
        self.assertEqual((len(limited), len(answered)), (1, 1))
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(self.reservation_states(), {"committed": 1})
        self.assertEqual(self.used(), 1)

    def test_many_concurrent_requests_never_exceed_the_limit(self):
        self.set_limit(3)
        self.ai_delay = 0.2
        jobs = [(NUMBER, _sid(), f"+2677310{i:04d}") for i in range(10)]
        results = self.fire(jobs)
        self.assertTrue(all(r[0] == 200 for r in results))
        answered = sum(1 for r in results if b"Fake AI reply." in r[1])
        limited = sum(1 for r in results if LIMIT_TEXT in r[1].lower())
        self.assertEqual((answered, limited), (3, 7))
        # successful AI calls == successful reservations == limit
        self.assertEqual(self.successful_ai_calls, 3)
        self.assertEqual(self.openai_calls, 3)
        self.assertEqual(self.reservation_states(), {"committed": 3})
        self.assertEqual(self.used(), 3)

    def test_repeated_races_for_the_last_unit(self):
        for round_ in range(5):
            self.set_limit(1)
            db.session.execute(text("TRUNCATE usage_reservations, tenant_usage_periods CASCADE"))
            db.session.commit()
            db.session.remove()
            before = self.openai_calls
            results = self.fire([(NUMBER, _sid(), f"+2677320{round}{i:03d}") for i in range(4)])
            self.assertEqual(sum(1 for r in results if b"Fake AI reply." in r[1]), 1, round_)
            self.assertEqual(self.openai_calls - before, 1, round_)
            self.assertEqual(self.reservation_states(), {"committed": 1}, round_)

    def test_concurrent_identical_sids_reserve_and_call_once(self):
        self.set_limit(5)
        self.engine_delay = 0.3
        sid = _sid()
        results = self.fire([(NUMBER, sid, CUSTOMER)] * 3)
        self.assertTrue(all(r[0] == 200 for r in results))
        self.assertEqual(sum(1 for r in results if b"<Message>" in r[1]), 1)
        self.assertEqual(self.openai_calls, 1)
        self.assertEqual(self.reservation_states(), {"committed": 1})
        self.assertEqual(self.used(), 1)

    def test_failed_openai_calls_release_capacity_for_others(self):
        self.set_limit(1)
        self.fail_openai = True
        r = self.fire([(NUMBER, _sid(), "+26773300001")])
        self.assertEqual(r[0][0], 200)
        self.assertEqual(self.reservation_states(), {"released": 1})
        self.assertEqual(self.used(), 0)
        self.fail_openai = False
        r = self.fire([(NUMBER, _sid(), "+26773300002")])
        self.assertIn(b"Fake AI reply.", r[0][1])
        self.assertEqual(self.used(), 1)


class TenantIsolationPgTests(QuotaPgCase):
    def test_exhausted_tenant_does_not_block_another_and_vice_versa(self):
        other = self.second_tenant()
        self.set_limit(1)
        self.set_limit(2, other)
        self.ai_delay = 0.2
        jobs = ([(NUMBER, _sid(), f"+2677340{i:04d}") for i in range(3)]
                + [(SECOND_NUMBER, _sid(), f"+2677350{i:04d}") for i in range(3)])
        results = self.fire(jobs)
        a = results[:3]
        b = results[3:]
        self.assertEqual(sum(1 for r in a if b"Fake AI reply." in r[1]), 1)
        self.assertEqual(sum(1 for r in b if b"Fake AI reply." in r[1]), 2)
        self.assertEqual(self.reservation_states(), {"committed": 1})
        self.assertEqual(self.reservation_states(other), {"committed": 2})
        self.assertEqual(self.openai_calls, 3)


class TransactionBoundaryPgTests(QuotaPgCase):
    def test_no_transaction_is_open_while_openai_runs(self):
        self.set_limit(5)
        self.ai_delay = 0.5
        r = self.fire([(NUMBER, _sid(), CUSTOMER)])
        self.assertIn(b"Fake AI reply.", r[0][1])
        self.assertEqual(self.in_txn_at_call, [False])
        self.assertEqual(self.idle_in_txn_seen, [0],
                         "a backend was idle in a transaction touching usage_reservations during OpenAI")

    def test_concurrent_requests_hold_no_transactions_during_openai(self):
        self.set_limit(10)
        self.ai_delay = 0.4
        self.fire([(NUMBER, _sid(), f"+2677360{i:04d}") for i in range(5)])
        self.assertEqual(self.in_txn_at_call, [False] * 5)

    def test_slow_openai_does_not_block_other_tenants_quota_row(self):
        """While one request is inside OpenAI, another can reserve (no row lock held)."""
        self.set_limit(5)
        self.ai_delay = 1.0
        t0 = time.time()
        self.fire([(NUMBER, _sid(), f"+2677370{i:04d}") for i in range(4)])
        # Serialised-by-lock would take ~4s; concurrent takes ~1s plus overhead.
        self.assertLess(time.time() - t0, 3.5)
        self.assertEqual(self.reservation_states(), {"committed": 4})

    def test_rls_binding_is_restored_after_openai(self):
        """Finalisation writes through RLS after the commit dropped the GUC."""
        self.set_limit(5)
        r = self.fire([(NUMBER, _sid(), CUSTOMER)])
        self.assertIn(b"Fake AI reply.", r[0][1])
        db.session.remove()
        rows = UsageReservation.query.filter_by(tenant_id=self.tenant_id).all()
        self.assertEqual([x.status for x in rows], ["committed"])
        self.assertEqual(rows[0].total_tokens, 15)
        db.session.remove()

    def test_stale_sweep_recovers_a_reservation_left_by_a_crash(self):
        self.set_limit(1)
        with patch.object(usage_service, "commit_reservation", side_effect=RuntimeError("down")):
            r = self.fire([(NUMBER, _sid(), CUSTOMER)])
        self.assertEqual(r[0][0], 200)
        self.assertIn(b"Fake AI reply.", r[0][1])
        self.assertEqual(self.reservation_states(), {"reserved": 1})
        self.assertEqual(self.used(), 1)
        from datetime import datetime, timedelta, timezone
        usage_service.release_stale_reservations(now=datetime.now(timezone.utc) + timedelta(hours=1))
        db.session.commit()
        db.session.remove()
        self.assertEqual(self.reservation_states(), {"released": 1})
        self.assertEqual(self.used(), 0)


if __name__ == "__main__":
    unittest.main()
