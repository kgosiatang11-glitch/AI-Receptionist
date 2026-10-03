"""PostgreSQL proof of atomic usage reservations (Phase 3.3A).

SQLite serialises every writer, so it cannot demonstrate the concurrency
guarantee, the real migration, or row-level security.  These tests run against a
real PostgreSQL database when one is provided:

    createdb smartdesk_test
    TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost/smartdesk_test \\
        python -m pytest tests/test_usage_postgres.py

Without ``TEST_DATABASE_URL`` the module is skipped (and says so).  The target
database is WIPED and rebuilt from the Alembic migrations, so its name must
contain "test".

Concurrency is real: every worker is a separate thread with its own Flask app
context, hence its own SQLAlchemy session, DB connection and transaction --
the situation of two gunicorn workers.  Nothing is mocked.  Timing luck is
removed where it matters by *gating*: the test holds an uncommitted reservation
(and therefore the period row lock) on one connection, starts the contenders,
waits until ``pg_stat_activity`` shows them blocked on that lock, and only then
releases it.  The assertions therefore describe what PostgreSQL did to
genuinely contending transactions.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError, IntegrityError  # noqa: E402

from smartdesk.app import create_app  # noqa: E402
from smartdesk.config import TestConfig  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import Tenant, TenantUsagePeriod, UsageReservation  # noqa: E402
from smartdesk.services import usage  # noqa: E402

MIGRATIONS_DIR = str(Path(__file__).resolve().parent.parent / "migrations")
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
PERIOD_START = datetime(2026, 10, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 11, 1, tzinfo=timezone.utc)
RLS_ROLE = "sd_usage_rls_tester"


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "TEST_DATABASE_URL not set: PostgreSQL usage-reservation tests were NOT run",
)
class PostgresUsageCase(unittest.TestCase):
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
            SQLALCHEMY_ENGINE_OPTIONS = {"pool_size": 20, "max_overflow": 10}

        cls.app = create_app(PgConfig)
        with cls.app.app_context():
            db.session.execute(text("DROP SCHEMA public CASCADE"))
            db.session.execute(text("CREATE SCHEMA public"))
            db.session.commit()
            db.session.remove()
            db.engine.dispose()
            cls._migrate("upgrade")

    @classmethod
    def _migrate(cls, direction: str, revision: str = "head") -> None:
        import flask_migrate

        getattr(flask_migrate, direction)(directory=MIGRATIONS_DIR, revision=revision)

    def setUp(self) -> None:
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.execute(text("TRUNCATE tenants, users CASCADE"))
        db.session.commit()

    def tearDown(self) -> None:
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    # -- fixtures ----------------------------------------------------------

    def make_tenant(self, slug: str, limit: int = 5) -> str:
        tenant = Tenant(slug=slug, name=slug, monthly_conversation_limit=limit)
        db.session.add(tenant)
        db.session.commit()
        return tenant.id

    def reserve(self, tenant_id, key, **kw):
        kw.setdefault("now", T0)
        return usage.reserve_ai_reply(tenant_id, key, **kw)

    def scalar(self, sql: str, **params):
        return db.session.execute(text(sql), params).scalar()

    def used(self, tenant_id) -> int:
        db.session.rollback()  # end any snapshot; read the latest committed state
        return self.scalar(
            "SELECT COALESCE(SUM(used_units), 0) FROM tenant_usage_periods WHERE tenant_id = :t",
            t=tenant_id,
        )

    def reservation_count(self, tenant_id, status=None) -> int:
        db.session.rollback()
        sql = "SELECT count(*) FROM usage_reservations WHERE tenant_id = :t"
        if status:
            return self.scalar(sql + " AND status = :s", t=tenant_id, s=status)
        return self.scalar(sql, t=tenant_id)

    def live_units(self, tenant_id) -> int:
        db.session.rollback()
        return self.scalar(
            "SELECT COALESCE(SUM(units), 0) FROM usage_reservations "
            "WHERE tenant_id = :t AND status <> 'released'", t=tenant_id,
        )

    # -- real concurrency --------------------------------------------------

    def start_workers(self, count: int, work):
        """Start ``count`` threads, each with its own app context/session/
        connection and its own transaction, released together by a barrier.
        ``work(i)`` must return plain data (the session is gone afterwards)."""
        barrier = threading.Barrier(count)
        results: list = [None] * count
        errors: list = []

        def runner(i: int) -> None:
            with self.app.app_context():
                try:
                    barrier.wait(timeout=30)
                    results[i] = work(i)
                    db.session.commit()
                except BaseException as exc:  # noqa: BLE001 - reported to the test
                    db.session.rollback()
                    errors.append((i, repr(exc)))
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=runner, args=(i,), daemon=True) for i in range(count)]
        for thread in threads:
            thread.start()
        return threads, results, errors

    @staticmethod
    def join_workers(threads, timeout: float = 60) -> None:
        for thread in threads:
            thread.join(timeout)
        assert not any(t.is_alive() for t in threads), "worker thread hung (deadlock?)"

    def run_workers(self, count: int, work):
        threads, results, errors = self.start_workers(count, work)
        self.join_workers(threads)
        self.assertEqual(errors, [], "workers raised")
        return results

    def wait_for_blocked(self, expected: int, timeout: float = 20.0) -> int:
        """Poll pg_stat_activity until ``expected`` backends are waiting on a
        lock -- proof the contenders are really blocked behind the holder."""
        deadline = time.monotonic() + timeout
        blocked = 0
        while time.monotonic() < deadline:
            with db.engine.connect() as conn:
                blocked = conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock'"
                )).scalar()
            if blocked >= expected:
                return blocked
            time.sleep(0.05)
        return blocked

    @staticmethod
    def plain(result) -> tuple:
        """Detach a ReservationResult into plain data inside the worker."""
        return (
            result.allowed, result.reason, result.created,
            result.reservation.id if result.reservation is not None else None,
        )


# ---------------------------------------------------------------------------
# 18. Real migration
# ---------------------------------------------------------------------------


class MigrationTests(PostgresUsageCase):
    TABLES = ("tenant_usage_periods", "usage_reservations")

    def test_18_migration_chain_applies_from_a_fresh_database(self):
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version"), "0006_usage_reservations"
        )
        for table in self.TABLES:
            self.assertTrue(self.scalar("SELECT to_regclass(:t) IS NOT NULL", t=f"public.{table}"))

    def test_18_schema_matches_the_models(self):
        """Every named constraint/index the models declare exists in PostgreSQL."""
        in_db = {
            row[0] for row in db.session.execute(text(
                "SELECT conname FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
                "WHERE r.relname IN ('tenant_usage_periods', 'usage_reservations') "
                "UNION SELECT indexname FROM pg_indexes "
                "WHERE tablename IN ('tenant_usage_periods', 'usage_reservations')"
            ))
        }
        declared = set()
        for model in (TenantUsagePeriod, UsageReservation):
            table = model.__table__
            declared |= {c.name for c in table.constraints if c.name}
            declared |= {i.name for i in table.indexes if i.name}
        self.assertTrue(declared, "models declare no named constraints?")
        self.assertEqual(sorted(declared - in_db), [])

    def test_18_column_types_and_defaults(self):
        rows = db.session.execute(text(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name IN ('tenant_usage_periods', 'usage_reservations')"
        )).all()
        cols = {(r[0], r[1]): (r[2], r[3]) for r in rows}
        self.assertEqual(cols[("tenant_usage_periods", "id")], ("uuid", "NO"))
        self.assertEqual(cols[("tenant_usage_periods", "period_start")][0], "timestamp with time zone")
        self.assertEqual(cols[("tenant_usage_periods", "used_units")], ("integer", "NO"))
        self.assertEqual(cols[("usage_reservations", "meta")], ("jsonb", "NO"))
        for nullable in ("committed_at", "released_at", "prompt_tokens",
                         "completion_tokens", "total_tokens"):
            self.assertEqual(cols[("usage_reservations", nullable)][1], "YES")
        for required in ("tenant_id", "usage_period_id", "kind", "idempotency_key",
                         "units", "status", "reserved_at"):
            self.assertEqual(cols[("usage_reservations", required)][1], "NO")

    def test_18_rls_is_enabled_forced_and_has_a_tenant_policy(self):
        for table in self.TABLES:
            enabled, forced = db.session.execute(text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
            ), {"t": table}).one()
            self.assertTrue(enabled, table)
            self.assertTrue(forced, table)
            policy = db.session.execute(text(
                "SELECT qual, with_check FROM pg_policies WHERE tablename = :t "
                "AND policyname = :p"
            ), {"t": table, "p": f"{table}_tenant_isolation"}).one()
            self.assertIn("app.current_tenant_id", policy.qual)
            self.assertIn("app.current_tenant_id", policy.with_check)

    def test_18_downgrade_and_reupgrade_are_clean_and_leave_tenants_untouched(self):
        tenant_id = self.make_tenant("keepme", limit=777)
        self.reserve(tenant_id, "a")
        db.session.commit()
        db.session.rollback()
        db.session.remove()
        db.engine.dispose()
        try:
            self._migrate("downgrade", "0005_inbound_message_idempotency")
            self.assertEqual(
                self.scalar("SELECT version_num FROM alembic_version"),
                "0005_inbound_message_idempotency",
            )
            for table in self.TABLES:
                self.assertFalse(
                    self.scalar("SELECT to_regclass(:t) IS NOT NULL", t=f"public.{table}")
                )
            self.assertEqual(self.scalar(
                "SELECT count(*) FROM pg_policies WHERE tablename IN "
                "('tenant_usage_periods', 'usage_reservations')"), 0)
            # The limit lives on tenants and is not touched by the revision.
            self.assertEqual(self.scalar(
                "SELECT monthly_conversation_limit FROM tenants WHERE id = :t", t=tenant_id), 777)
        finally:
            db.session.rollback()
            db.session.remove()
            db.engine.dispose()
            self._migrate("upgrade")
        self.assertEqual(
            self.scalar("SELECT version_num FROM alembic_version"), "0006_usage_reservations"
        )
        self.assertEqual(self.scalar(
            "SELECT monthly_conversation_limit FROM tenants WHERE id = :t", t=tenant_id), 777)

    def test_18_existing_tenant_limits_are_not_modified_by_the_schema(self):
        tenant_id = self.make_tenant("zero", limit=0)
        self.assertEqual(self.scalar(
            "SELECT monthly_conversation_limit FROM tenants WHERE id = :t", t=tenant_id), 0)


# ---------------------------------------------------------------------------
# 19. Database-enforced constraints
# ---------------------------------------------------------------------------


class ConstraintTests(PostgresUsageCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.make_tenant("a")
        self.b = self.make_tenant("b")
        self.period_a = self.insert_period(self.a)

    def insert_period(self, tenant_id, used=0, start=PERIOD_START, end=PERIOD_END) -> str:
        period_id = str(uuid.uuid4())
        db.session.execute(text(
            "INSERT INTO tenant_usage_periods (id, tenant_id, period_start, period_end, used_units) "
            "VALUES (:id, :t, :s, :e, :u)"
        ), {"id": period_id, "t": tenant_id, "s": start, "e": end, "u": used})
        db.session.commit()
        return period_id

    def insert_reservation(self, tenant_id, period_id, **over) -> None:
        values = dict(id=str(uuid.uuid4()), t=tenant_id, p=period_id, kind="ai_reply",
                      key="k", units=1, status="reserved", committed=None, released=None,
                      tokens=None)
        values.update(over)
        db.session.execute(text(
            "INSERT INTO usage_reservations (id, tenant_id, usage_period_id, kind, "
            "idempotency_key, units, status, reserved_at, committed_at, released_at, total_tokens) "
            "VALUES (:id, :t, :p, :kind, :key, :units, :status, now(), :committed, :released, :tokens)"
        ), values)

    def assertViolates(self, fn, constraint: str) -> None:
        with self.assertRaises(IntegrityError) as caught:
            fn()
            db.session.commit()
        db.session.rollback()
        self.assertIn(constraint, str(caught.exception))

    def test_19_unique_idempotency_key_is_enforced_by_the_database(self):
        self.insert_reservation(self.a, self.period_a)
        db.session.commit()
        self.assertViolates(
            lambda: self.insert_reservation(self.a, self.period_a), "uq_usage_reservation_idempotency"
        )
        # Same key under a different kind or tenant is a different identity.
        self.insert_reservation(self.a, self.period_a, kind="other")
        period_b = self.insert_period(self.b)
        self.insert_reservation(self.b, period_b)
        db.session.commit()
        self.assertEqual(self.reservation_count(self.a), 2)

    def test_19_unique_period_per_tenant_and_window(self):
        self.assertViolates(lambda: self.insert_period(self.a), "uq_usage_period_tenant_window")
        self.insert_period(self.b)  # other tenant, same window: fine
        self.insert_period(self.a, start=PERIOD_END, end=datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_19_check_constraints(self):
        self.assertViolates(lambda: self.insert_period(self.b, used=-1), "ck_usage_period_used_nonneg")
        self.assertViolates(
            lambda: self.insert_period(self.b, start=PERIOD_END, end=PERIOD_START),
            "ck_usage_period_window",
        )
        self.assertViolates(
            lambda: self.insert_period(self.b, start=PERIOD_START, end=PERIOD_START),
            "ck_usage_period_window",
        )
        for units in (0, -1):
            self.assertViolates(
                lambda units=units: self.insert_reservation(self.a, self.period_a, key="u", units=units),
                "ck_usage_reservation_units_positive",
            )
        self.assertViolates(
            lambda: self.insert_reservation(self.a, self.period_a, key="s", status="bogus"),
            "ck_usage_reservation_status",
        )
        self.assertViolates(  # committed without committed_at
            lambda: self.insert_reservation(self.a, self.period_a, key="c", status="committed"),
            "ck_usage_reservation_status_timestamps",
        )
        self.assertViolates(
            lambda: self.insert_reservation(self.a, self.period_a, key="t", tokens=-5),
            "ck_usage_reservation_tokens_nonneg",
        )

    def test_19_used_units_cannot_be_driven_negative(self):
        with self.assertRaises(IntegrityError):
            db.session.execute(text(
                "UPDATE tenant_usage_periods SET used_units = used_units - 1 WHERE id = :p"
            ), {"p": self.period_a})
            db.session.commit()
        db.session.rollback()
        self.assertEqual(self.used(self.a), 0)

    def test_19_a_reservation_cannot_reference_another_tenants_period(self):
        self.assertViolates(
            lambda: self.insert_reservation(self.b, self.period_a, key="x"),
            "fk_usage_reservations_period_tenant",
        )

    def test_19_unknown_tenant_or_period_is_rejected(self):
        with self.assertRaises(IntegrityError):
            self.insert_reservation(str(uuid.uuid4()), self.period_a)
            db.session.commit()
        db.session.rollback()

    def test_19_deleting_a_tenant_cascades_its_usage(self):
        self.insert_reservation(self.a, self.period_a)
        db.session.commit()
        db.session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": self.a})
        db.session.commit()
        self.assertEqual(self.scalar("SELECT count(*) FROM tenant_usage_periods"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM usage_reservations"), 0)


# ---------------------------------------------------------------------------
# Service behaviour on real PostgreSQL (sequential sanity)
# ---------------------------------------------------------------------------


class ServiceOnPostgresTests(PostgresUsageCase):
    def test_full_lifecycle(self):
        tenant = self.make_tenant("t", limit=2)
        first = self.reserve(tenant, "a")
        second = self.reserve(tenant, "b")
        self.assertTrue(first.allowed and second.allowed)
        self.assertFalse(self.reserve(tenant, "c").allowed)
        usage.commit_reservation(tenant, first.reservation.id, prompt_tokens=9,
                                 completion_tokens=3, total_tokens=12, now=T0)
        usage.release_reservation(tenant, second.reservation.id, now=T0)
        db.session.commit()
        snapshot = usage.get_current_usage(tenant, now=T0)
        self.assertEqual((snapshot.used_units, snapshot.available_units), (1, 1))
        self.assertTrue(self.reserve(tenant, "c").allowed)
        db.session.commit()
        self.assertEqual(self.used(tenant), 2)

    def test_zero_and_negative_limits_never_create_capacity(self):
        for limit in (0, -1, -100):
            tenant = self.make_tenant(f"lim{limit}".replace("-", "n"), limit=limit)
            for i in range(3):
                self.assertFalse(self.reserve(tenant, f"k{i}").allowed)
            db.session.commit()
            self.assertEqual(self.reservation_count(tenant), 0)
            self.assertEqual(self.used(tenant), 0)

    def test_same_key_retry_is_a_noop(self):
        tenant = self.make_tenant("t", limit=5)
        first = self.reserve(tenant, "k")
        db.session.commit()
        again = self.reserve(tenant, "k")
        db.session.commit()
        self.assertEqual((again.reason, again.created), ("duplicate", False))
        self.assertEqual(again.reservation.id, first.reservation.id)
        self.assertEqual(self.used(tenant), 1)


# ---------------------------------------------------------------------------
# 20-22. Real concurrency
# ---------------------------------------------------------------------------


class ConcurrencyTests(PostgresUsageCase):
    def attempt(self, tenant_id, key_prefix="k"):
        def work(i):
            return self.plain(self.reserve(tenant_id, f"{key_prefix}-{i}"))
        return work

    def test_20_concurrent_reservations_never_exceed_the_limit(self):
        tenant = self.make_tenant("t", limit=5)
        results = self.run_workers(24, self.attempt(tenant))
        allowed = [r for r in results if r[0]]
        self.assertEqual(len(allowed), 5)
        self.assertEqual(len([r for r in results if not r[0]]), 19)
        self.assertEqual({r[1] for r in results}, {"reserved", "quota_exceeded"})
        self.assertEqual(self.used(tenant), 5)
        self.assertEqual(self.reservation_count(tenant), 5)  # denials left no rows
        self.assertEqual(len({r[3] for r in allowed}), 5)

    def test_21_the_final_unit_cannot_be_reserved_twice(self):
        for round_no in range(25):
            tenant = self.make_tenant(f"final{round_no}", limit=2)
            self.assertTrue(self.reserve(tenant, "pre").allowed)
            db.session.commit()
            results = self.run_workers(2, self.attempt(tenant, "race"))
            self.assertEqual(sum(1 for r in results if r[0]), 1, f"round {round_no}: {results}")
            self.assertEqual(self.used(tenant), 2)
            self.assertEqual(self.reservation_count(tenant), 2)

    def test_21_gated_contention_exactly_one_waiter_gets_the_last_unit(self):
        """A holder keeps an uncommitted reservation (and the period row lock);
        six contenders are proven blocked behind it before it commits."""
        tenant = self.make_tenant("gate", limit=3)
        self.assertTrue(self.reserve(tenant, "seed").allowed)
        db.session.commit()
        self.assertTrue(self.reserve(tenant, "holder").allowed)  # uncommitted: used=2, row locked

        threads, results, errors = self.start_workers(6, self.attempt(tenant, "waiter"))
        try:
            blocked = self.wait_for_blocked(6)
            self.assertGreaterEqual(blocked, 6, "contenders were not actually blocked on the lock")
        finally:
            db.session.commit()  # holder commits -> contenders proceed
        self.join_workers(threads)
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in results if r[0]), 1)
        self.assertEqual(self.used(tenant), 3)
        self.assertEqual(self.reservation_count(tenant), 3)

    def test_23_gated_holder_rollback_frees_capacity_for_the_waiters(self):
        tenant = self.make_tenant("gate-rb", limit=3)
        self.assertTrue(self.reserve(tenant, "seed").allowed)
        db.session.commit()
        self.assertTrue(self.reserve(tenant, "holder").allowed)  # used=2, uncommitted

        threads, results, errors = self.start_workers(6, self.attempt(tenant, "waiter"))
        try:
            self.assertGreaterEqual(self.wait_for_blocked(6), 6)
        finally:
            db.session.rollback()  # holder aborts: its unit must NOT stay consumed
        self.join_workers(threads)
        self.assertEqual(errors, [])
        # seed + 2 waiters fill the limit of 3 -- the rolled-back unit was reusable.
        self.assertEqual(sum(1 for r in results if r[0]), 2)
        self.assertEqual(self.used(tenant), 3)
        self.assertEqual(self.reservation_count(tenant), 3)

    def test_same_idempotency_key_concurrently_consumes_exactly_one_unit(self):
        tenant = self.make_tenant("same", limit=5)
        results = self.run_workers(10, lambda i: self.plain(self.reserve(tenant, "one-key")))
        self.assertTrue(all(r[0] for r in results))
        self.assertEqual(sum(1 for r in results if r[2]), 1)       # exactly one creator
        self.assertEqual(len({r[3] for r in results}), 1)           # all see the same row
        self.assertEqual(self.used(tenant), 1)
        self.assertEqual(self.reservation_count(tenant), 1)

    def test_gated_same_key_retries_lose_the_unique_race_and_recover(self):
        """The holder's uncommitted INSERT makes four retries block on the unique
        index; when it commits they hit the constraint, roll back their
        savepoint and must return the holder's reservation without a new unit."""
        tenant = self.make_tenant("same-gate", limit=5)
        self.assertTrue(self.reserve(tenant, "seed").allowed)
        db.session.commit()
        held_id = self.reserve(tenant, "dup-key").reservation.id

        threads, results, errors = self.start_workers(
            4, lambda i: self.plain(self.reserve(tenant, "dup-key"))
        )
        try:
            self.assertGreaterEqual(self.wait_for_blocked(4), 4)
        finally:
            db.session.commit()
        self.join_workers(threads)
        self.assertEqual(errors, [])
        for allowed, reason, created, reservation_id in results:
            self.assertEqual((allowed, reason, created), (True, "duplicate", False))
            self.assertEqual(reservation_id, held_id)
        self.assertEqual(self.used(tenant), 2)
        self.assertEqual(self.reservation_count(tenant), 2)

    def test_22_tenants_do_not_consume_each_others_quota(self):
        a = self.make_tenant("a", limit=3)
        b = self.make_tenant("b", limit=3)
        tenants = [a, b] * 8

        def work(i):
            return (tenants[i], self.plain(self.reserve(tenants[i], f"k{i}")))

        results = self.run_workers(16, work)
        for tenant in (a, b):
            allowed = [r for t, r in results if t == tenant and r[0]]
            self.assertEqual(len(allowed), 3, tenant)
            self.assertEqual(self.used(tenant), 3)
        c = self.make_tenant("c", limit=1)
        self.assertTrue(self.reserve(c, "k").allowed)
        db.session.commit()

    def test_22_exhausted_tenant_does_not_block_another_tenants_waiter(self):
        a = self.make_tenant("hog", limit=1)
        b = self.make_tenant("free", limit=1)
        self.assertTrue(self.reserve(a, "seed").allowed)
        db.session.commit()
        self.assertFalse(self.reserve(a, "denied").allowed)
        db.session.commit()
        # Hold tenant A's period row lock; tenant B must still proceed.
        db.session.execute(
            text("SELECT 1 FROM tenant_usage_periods WHERE tenant_id = :t FOR UPDATE"), {"t": a}
        )
        results = self.run_workers(1, lambda i: self.plain(self.reserve(b, "b-key")))
        self.assertTrue(results[0][0])
        db.session.rollback()

    def test_mixed_reserve_commit_release_traffic_keeps_the_counter_exact(self):
        tenant = self.make_tenant("mixed", limit=10)

        def work(i):
            result = self.reserve(tenant, f"m{i}")
            if not result.allowed:
                return "denied"
            reservation_id = result.reservation.id
            db.session.commit()  # reserve is its own short transaction
            if i % 2 == 0:
                usage.commit_reservation(tenant, reservation_id, total_tokens=i, now=T0)
                return "committed"
            usage.release_reservation(tenant, reservation_id, now=T0)
            return "released"

        for wave in range(3):  # released capacity is reusable by later waves
            self.run_workers(30, lambda i, wave=wave: work(i + wave * 100))
            used = self.used(tenant)
            self.assertLessEqual(used, 10)
            self.assertEqual(used, self.live_units(tenant), f"wave {wave}: counter != live units")
            self.assertEqual(used, self.reservation_count(tenant, "committed")
                             + self.reservation_count(tenant, "reserved"))
            self.assertGreaterEqual(used, 0)
        self.assertEqual(self.reservation_count(tenant, "reserved"), 0)

    def test_concurrent_release_of_one_reservation_returns_capacity_once(self):
        tenant = self.make_tenant("rel", limit=5)
        self.reserve(tenant, "keep")
        target_id = self.reserve(tenant, "target").reservation.id
        db.session.commit()
        self.run_workers(8, lambda i: usage.release_reservation(tenant, target_id, now=T0).status)
        self.assertEqual(self.used(tenant), 1)
        self.assertEqual(self.reservation_count(tenant, "released"), 1)

    def test_commit_racing_release_exactly_one_wins(self):
        for round_no in range(10):
            tenant = self.make_tenant(f"race{round_no}", limit=3)
            reservation_id = self.reserve(tenant, "k").reservation.id
            db.session.commit()

            def work(i):
                try:
                    if i == 0:
                        usage.commit_reservation(tenant, reservation_id, total_tokens=1, now=T0)
                        return "committed"
                    usage.release_reservation(tenant, reservation_id, now=T0)
                    return "released"
                except usage.ReservationStateError:
                    return "refused"

            results = self.run_workers(2, work)
            self.assertEqual(sorted(results).count("refused"), 1, results)
            winner = next(r for r in results if r != "refused")
            self.assertEqual(self.reservation_count(tenant, winner), 1)
            self.assertEqual(self.used(tenant), 1 if winner == "committed" else 0)


# ---------------------------------------------------------------------------
# 23. Rollback / retry
# ---------------------------------------------------------------------------


class RollbackRetryTests(PostgresUsageCase):
    def test_23_rolled_back_reservation_consumes_nothing_and_can_be_retried(self):
        tenant = self.make_tenant("t", limit=1)
        self.assertTrue(self.reserve(tenant, "k").allowed)
        db.session.rollback()
        self.assertEqual(self.used(tenant), 0)
        self.assertEqual(self.reservation_count(tenant), 0)
        retry = self.reserve(tenant, "k")  # same key after the abort: brand new, not a duplicate
        db.session.commit()
        self.assertEqual((retry.allowed, retry.reason, retry.created), (True, "reserved", True))
        self.assertEqual(self.used(tenant), 1)

    def test_23_a_failure_after_reserving_inside_the_transaction_leaks_nothing(self):
        tenant = self.make_tenant("t", limit=1)
        with self.assertRaises(RuntimeError):
            try:
                self.assertTrue(self.reserve(tenant, "k").allowed)
                raise RuntimeError("simulated crash before commit")
            except RuntimeError:
                db.session.rollback()
                raise
        self.assertEqual(self.used(tenant), 0)
        self.assertTrue(self.reserve(tenant, "other").allowed)
        db.session.commit()

    def test_23_denial_and_duplicate_paths_are_safe_after_rollback(self):
        tenant = self.make_tenant("t", limit=1)
        self.assertTrue(self.reserve(tenant, "a").allowed)
        db.session.commit()
        self.assertFalse(self.reserve(tenant, "b").allowed)  # denied: savepoint rolled back
        self.assertEqual(self.reserve(tenant, "a").reason, "duplicate")
        db.session.commit()  # the session is still healthy after both paths
        self.assertEqual(self.used(tenant), 1)
        self.assertEqual(self.reservation_count(tenant), 1)

    def test_23_release_rolled_back_keeps_the_unit_reserved(self):
        tenant = self.make_tenant("t", limit=2)
        reservation_id = self.reserve(tenant, "a").reservation.id
        db.session.commit()
        usage.release_reservation(tenant, reservation_id, now=T0)
        db.session.rollback()
        self.assertEqual(self.used(tenant), 1)
        self.assertEqual(self.reservation_count(tenant, "reserved"), 1)
        usage.release_reservation(tenant, reservation_id, now=T0)
        db.session.commit()
        self.assertEqual(self.used(tenant), 0)

    def test_23_a_new_period_created_in_an_aborted_transaction_is_recreated(self):
        tenant = self.make_tenant("t", limit=2)
        later = PERIOD_END + timedelta(days=1)
        self.assertTrue(self.reserve(tenant, "a", now=later).allowed)
        db.session.rollback()
        self.assertEqual(self.scalar("SELECT count(*) FROM tenant_usage_periods"), 0)
        self.assertTrue(self.reserve(tenant, "a", now=later).allowed)
        db.session.commit()
        self.assertEqual(self.scalar("SELECT count(*) FROM tenant_usage_periods"), 1)


# ---------------------------------------------------------------------------
# 24. Stale recovery
# ---------------------------------------------------------------------------


class StaleRecoveryTests(PostgresUsageCase):
    LATER = T0 + timedelta(hours=1)

    def test_24_stale_reservations_are_recovered_on_postgres(self):
        tenant = self.make_tenant("t", limit=3)
        for i in range(3):
            self.reserve(tenant, f"k{i}")
        db.session.commit()
        self.assertFalse(self.reserve(tenant, "blocked").allowed)

        result = usage.release_stale_reservations(stale_after=timedelta(minutes=10), now=self.LATER)
        db.session.commit()
        self.assertEqual((result.released, result.units), (3, 3))
        self.assertEqual(self.used(tenant), 0)
        self.assertEqual(self.reservation_count(tenant, "released"), 3)
        again = usage.release_stale_reservations(stale_after=timedelta(minutes=10), now=self.LATER)
        db.session.commit()
        self.assertEqual(again.released, 0)
        self.assertEqual(self.used(tenant), 0)
        self.assertTrue(self.reserve(tenant, "fits", now=self.LATER).allowed)
        db.session.commit()

    def test_24_committed_and_fresh_reservations_are_left_alone(self):
        tenant = self.make_tenant("t", limit=5)
        old = self.reserve(tenant, "old").reservation.id
        done = self.reserve(tenant, "done").reservation.id
        usage.commit_reservation(tenant, done, total_tokens=4, now=T0)
        fresh = self.reserve(tenant, "fresh", now=self.LATER - timedelta(minutes=1)).reservation.id
        db.session.commit()
        result = usage.release_stale_reservations(stale_after=timedelta(minutes=10), now=self.LATER)
        db.session.commit()
        self.assertEqual(result.released, 1)
        self.assertEqual(usage.get_reservation(tenant, old).status, "released")
        self.assertEqual(usage.get_reservation(tenant, done).status, "committed")
        self.assertEqual(usage.get_reservation(tenant, fresh).status, "reserved")
        self.assertEqual(self.used(tenant), 2)

    def test_24_concurrent_sweeps_release_each_reservation_exactly_once(self):
        tenant = self.make_tenant("t", limit=100)
        for i in range(40):
            self.reserve(tenant, f"k{i}")
        db.session.commit()
        self.assertEqual(self.used(tenant), 40)

        def sweep(i):
            total = 0
            while True:
                result = usage.release_stale_reservations(
                    stale_after=timedelta(minutes=10), now=self.LATER, batch_size=7
                )
                db.session.commit()
                if result.released == 0:
                    return total
                total += result.released

        results = self.run_workers(4, sweep)
        self.assertEqual(sum(results), 40)
        self.assertEqual(self.used(tenant), 0)
        self.assertEqual(self.reservation_count(tenant, "released"), 40)
        self.assertEqual(self.reservation_count(tenant, "reserved"), 0)

    def test_24_a_sweep_skips_rows_locked_by_in_flight_work_instead_of_waiting(self):
        tenant = self.make_tenant("t", limit=5)
        locked = self.reserve(tenant, "locked").reservation.id
        self.reserve(tenant, "free")
        db.session.commit()
        db.session.execute(
            text("SELECT 1 FROM usage_reservations WHERE id = :i FOR UPDATE"), {"i": locked}
        )  # an in-flight transaction holds this reservation

        started = time.monotonic()
        results = self.run_workers(1, lambda i: usage.release_stale_reservations(
            stale_after=timedelta(minutes=10), now=self.LATER).released)
        self.assertLess(time.monotonic() - started, 10, "sweep waited on a locked row")
        self.assertEqual(results, [1])  # only the unlocked row
        db.session.rollback()  # in-flight work ends
        self.assertEqual(self.used(tenant), 1)
        again = usage.release_stale_reservations(stale_after=timedelta(minutes=10), now=self.LATER)
        db.session.commit()
        self.assertEqual(again.released, 1)
        self.assertEqual(self.used(tenant), 0)

    def test_24_commit_after_the_sweep_released_it_is_refused(self):
        tenant = self.make_tenant("t", limit=2)
        reservation_id = self.reserve(tenant, "slow").reservation.id
        db.session.commit()
        usage.release_stale_reservations(stale_after=timedelta(minutes=10), now=self.LATER)
        db.session.commit()
        with self.assertRaises(usage.ReservationStateError):
            usage.commit_reservation(tenant, reservation_id, total_tokens=3, now=self.LATER)
        db.session.rollback()
        self.assertEqual(self.used(tenant), 0)


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------


class RowLevelSecurityTests(PostgresUsageCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.rls_ready = True
        try:
            with cls.app.app_context():
                db.session.execute(text(
                    f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RLS_ROLE}') "
                    f"THEN CREATE ROLE {RLS_ROLE} NOLOGIN; END IF; END $$"
                ))
                db.session.execute(text(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}"))
                db.session.execute(text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_usage_periods, "
                    f"usage_reservations TO {RLS_ROLE}"
                ))
                db.session.execute(text(f"GRANT SELECT ON tenants TO {RLS_ROLE}"))
                db.session.commit()
        except Exception:  # pragma: no cover - restricted test database user
            cls.rls_ready = False

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.rls_ready:
            with cls.app.app_context():
                db.session.rollback()
                db.session.execute(text(f"DROP OWNED BY {RLS_ROLE}"))
                db.session.execute(text(f"DROP ROLE IF EXISTS {RLS_ROLE}"))
                db.session.commit()

    def setUp(self) -> None:
        super().setUp()
        if not self.rls_ready:
            self.skipTest("cannot create the non-superuser RLS test role")
        self.a = self.make_tenant("a", limit=5)
        self.b = self.make_tenant("b", limit=5)
        self.res_a = self.reserve(self.a, "ka").reservation.id
        self.res_b = self.reserve(self.b, "kb").reservation.id
        self.reserve(self.b, "kb2")
        db.session.commit()  # seeded as the owner/superuser, which bypasses RLS

    def as_tenant(self, tenant_id: str | None):
        """Switch the current transaction to the RLS-subject role and bind the
        tenant exactly like ``bind_tenant`` does."""
        db.session.execute(text(f"SET LOCAL ROLE {RLS_ROLE}"))
        if tenant_id is not None:
            db.session.execute(
                text("SELECT set_config('app.current_tenant_id', :t, true)"), {"t": tenant_id}
            )

    def test_a_tenant_sees_only_its_own_usage(self):
        self.as_tenant(self.a)
        periods = db.session.execute(text("SELECT tenant_id FROM tenant_usage_periods")).scalars().all()
        reservations = db.session.execute(text("SELECT id FROM usage_reservations")).scalars().all()
        self.assertEqual([str(p) for p in periods], [self.a])
        self.assertEqual([str(r) for r in reservations], [self.res_a])
        db.session.rollback()
        self.as_tenant(self.b)
        self.assertEqual(db.session.execute(text("SELECT count(*) FROM usage_reservations")).scalar(), 2)

    def test_an_unbound_session_sees_nothing(self):
        self.as_tenant(None)
        for table in ("tenant_usage_periods", "usage_reservations"):
            self.assertEqual(db.session.execute(text(f"SELECT count(*) FROM {table}")).scalar(), 0)

    def test_a_tenant_cannot_write_rows_for_another_tenant(self):
        self.as_tenant(self.a)
        with self.assertRaises(DBAPIError) as caught:
            db.session.execute(text(
                "INSERT INTO tenant_usage_periods (id, tenant_id, period_start, period_end) "
                "VALUES (gen_random_uuid(), :t, :s, :e)"
            ), {"t": self.b, "s": datetime(2027, 1, 1, tzinfo=timezone.utc),
                "e": datetime(2027, 2, 1, tzinfo=timezone.utc)})
        self.assertIn("row-level security", str(caught.exception))
        db.session.rollback()

    def test_a_tenant_cannot_modify_or_delete_another_tenants_usage(self):
        self.as_tenant(self.a)
        updated = db.session.execute(text(
            "UPDATE tenant_usage_periods SET used_units = 0 WHERE tenant_id = :t"), {"t": self.b})
        released = db.session.execute(text(
            "UPDATE usage_reservations SET status = 'released', released_at = now() "
            "WHERE tenant_id = :t"), {"t": self.b})
        deleted = db.session.execute(text("DELETE FROM usage_reservations WHERE tenant_id = :t"),
                                     {"t": self.b})
        self.assertEqual((updated.rowcount, released.rowcount, deleted.rowcount), (0, 0, 0))
        db.session.commit()
        self.assertEqual(self.used(self.b), 2)
        self.assertEqual(self.reservation_count(self.b, "reserved"), 2)

    def test_a_tenant_cannot_move_its_own_row_to_another_tenant(self):
        self.as_tenant(self.a)
        with self.assertRaises(DBAPIError):
            db.session.execute(text(
                "UPDATE tenant_usage_periods SET tenant_id = :b WHERE tenant_id = :a"
            ), {"a": self.a, "b": self.b})
        db.session.rollback()

    def test_the_service_works_for_the_bound_tenant_under_rls(self):
        self.as_tenant(self.a)
        result = self.reserve(self.a, "under-rls")
        self.assertEqual((result.allowed, result.reason), (True, "reserved"))
        usage.commit_reservation(self.a, result.reservation.id, total_tokens=3, now=T0)
        self.assertEqual(usage.get_current_usage(self.a, now=T0).used_units, 2)
        db.session.commit()
        self.assertEqual(self.used(self.a), 2)

    def test_the_service_cannot_reserve_against_another_tenant_under_rls(self):
        self.as_tenant(self.a)
        with self.assertRaises(DBAPIError):
            self.reserve(self.b, "intrusion")
        db.session.rollback()
        self.assertEqual(self.used(self.b), 2)
        self.assertEqual(self.reservation_count(self.b), 2)

    def test_another_tenants_usage_is_not_readable_through_the_service(self):
        self.as_tenant(self.a)
        # RLS hides B's rows: the read can neither see nor change them.
        self.assertEqual(usage.get_current_usage(self.b, now=T0).used_units, 0)
        self.assertIsNone(usage.get_reservation(self.b, self.res_b))
        with self.assertRaises(usage.ReservationNotFound):
            usage.release_reservation(self.b, self.res_b, now=T0)
        db.session.rollback()
        self.assertEqual(self.used(self.b), 2)


if __name__ == "__main__":
    unittest.main()
