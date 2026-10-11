"""Platform-administrator invariant under REAL concurrency (PostgreSQL).

SQLite cannot run two transactions at once, so this is where "a simple
count-before-update check is insufficient" is actually demonstrated -- and where
the platform-wide advisory lock is shown to fix it.  Skipped without
``TEST_DATABASE_URL`` (the database is wiped; its name must contain "test"),
exactly like the other ``*_postgres`` modules.

Unlike the other C6 Postgres tests these do NOT patch ``_is_email_confirmed``:
a stub ``public.is_email_confirmed(uuid)`` backed by a stub table is created in
``setUp`` so the real SQL path (including its SAVEPOINT) is exercised.

Determinism: most tests hold the platform lock in the test's own connection, so
both requests are provably queued behind it before either may proceed.
"""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import text

from smartdesk.extensions import db
from smartdesk.models import AuditLog, User
from smartdesk.security import rbac
from smartdesk.services import platform_admins
from tests.test_multitenant import make_token
from tests.test_whatsapp_idempotency_postgres import PostgresWebhookTestCase


class PgAdminCase(PostgresWebhookTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS public.stub_auth_users (
                id uuid PRIMARY KEY, email_confirmed_at timestamptz)"""))
        db.session.execute(text("TRUNCATE public.stub_auth_users"))
        db.session.execute(text("""
            CREATE OR REPLACE FUNCTION public.is_email_confirmed(sub uuid)
            RETURNS timestamptz LANGUAGE sql SECURITY DEFINER STABLE AS
            $$ SELECT email_confirmed_at FROM public.stub_auth_users WHERE id = sub $$"""))
        db.session.commit()
        self.a = self.mk_user("a@platform.pg.test")
        self.b = self.mk_user("b@platform.pg.test")
        # Exactly TWO administrators by default; C is a plain user until a test
        # promotes it (a third eligible administrator would legitimately survive).
        self.c = self.mk_user("c@platform.pg.test", admin=False)

    # -- fixtures / helpers ---------------------------------------------
    def mk_user(self, email, *, admin=True, active=True, confirmed=True) -> dict:
        sub = str(uuid.uuid4())
        user = User(supabase_user_id=sub, email=email,
                    is_platform_admin=admin, is_active=active)
        db.session.add(user)
        db.session.flush()
        if confirmed:
            db.session.execute(text(
                "INSERT INTO public.stub_auth_users VALUES (:s, now())"), {"s": sub})
        db.session.commit()
        info = {"id": user.id, "sub": sub, "email": email}
        db.session.remove()
        return info

    @staticmethod
    def headers(info) -> dict:
        return {"Authorization": f"Bearer {make_token(info['sub'], info['email'])}"}

    def request_async(self, actor, target, results, key, **payload):
        def run():
            with self.app.app_context():
                try:
                    r = self.app.test_client().patch(
                        f"/api/v1/admin/users/{target['id']}",
                        headers=self.headers(actor), json=payload)
                    results[key] = (r.status_code, r.get_json())
                finally:
                    db.session.remove()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def patch_now(self, actor, target, **payload):
        r = self.app.test_client().patch(
            f"/api/v1/admin/users/{target['id']}", headers=self.headers(actor), json=payload)
        db.session.remove()
        return r.status_code, r.get_json()

    @contextmanager
    def holding_platform_lock(self):
        """The test itself is "the request that got the lock first"."""
        db.session.rollback()
        db.session.remove()
        conn = db.engine.connect()
        txn = conn.begin()
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                     {"k": platform_admins.PLATFORM_ADMIN_LOCK_KEY})
        try:
            yield conn, txn
        finally:
            if txn.is_active:
                txn.rollback()
            conn.close()

    def scalar(self, sql, **params):
        db.session.remove()
        value = db.session.execute(text(sql), params).scalar()
        db.session.remove()
        return value

    def eligible_admins(self) -> int:
        return self.scalar("""
            SELECT count(*) FROM users u
            WHERE u.is_platform_admin AND u.is_active
              AND public.is_email_confirmed(u.supabase_user_id) IS NOT NULL""")

    def flags(self, info):
        db.session.remove()
        row = db.session.execute(
            text("SELECT is_platform_admin, is_active FROM users WHERE id = :i"),
            {"i": info["id"]}).one()
        db.session.remove()
        return tuple(row)

    def advisory_locks_held(self) -> int:
        return self.scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted")

    def make_admin(self, info) -> None:
        db.session.remove()
        db.session.execute(text("UPDATE users SET is_platform_admin = true WHERE id = :i"),
                           {"i": info["id"]})
        db.session.commit()
        db.session.remove()

    def audits(self) -> int:
        db.session.remove()
        n = AuditLog.query.filter_by(action="user.update").count()
        db.session.remove()
        return n

    def join_all(self, threads):
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), "a request never finished")

    def assert_loser(self, status, body):
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "last_platform_admin")


class ConcurrentRemovalTests(PgAdminCase):
    def run_queued_race(self, first, second):
        """Both requests queue on the held lock; the test then releases it."""
        results: dict = {}
        with self.holding_platform_lock() as (_conn, txn):
            threads = [
                self.request_async(first[0], first[1], results, "first", **first[2]),
                self.request_async(second[0], second[1], results, "second", **second[2]),
            ]
            time.sleep(1.0)
            self.assertTrue(all(t.is_alive() for t in threads),
                            "both requests must be waiting on the platform lock")
            self.assertEqual(results, {}, "nothing may be decided before the lock is free")
            txn.commit()
        self.join_all(threads)
        return results

    def test_two_admins_demoting_each_other_exactly_one_succeeds(self):
        results = self.run_queued_race((self.a, self.b, {"is_platform_admin": False}),
                                       (self.b, self.a, {"is_platform_admin": False}))
        statuses = sorted(s for s, _ in results.values())
        self.assertEqual(statuses, [200, 400], results)
        loser = next(v for v in results.values() if v[0] == 400)
        self.assert_loser(*loser)
        self.assertEqual(self.eligible_admins(), 1)
        self.assertEqual(self.audits(), 1, "only the applied change is audited")

    def test_two_admins_deactivating_each_other_exactly_one_succeeds(self):
        results = self.run_queued_race((self.a, self.b, {"is_active": False}),
                                       (self.b, self.a, {"is_active": False}))
        self.assertEqual(sorted(s for s, _ in results.values()), [200, 400], results)
        self.assert_loser(*next(v for v in results.values() if v[0] == 400))
        self.assertEqual(self.eligible_admins(), 1)

    def test_demotion_racing_a_deactivation_exactly_one_succeeds(self):
        results = self.run_queued_race((self.a, self.b, {"is_platform_admin": False}),
                                       (self.b, self.a, {"is_active": False}))
        self.assertEqual(sorted(s for s, _ in results.values()), [200, 400], results)
        self.assertEqual(self.eligible_admins(), 1)

    def test_three_admins_removing_each_other_in_a_cycle_never_empties_the_platform(self):
        self.make_admin(self.c)
        results: dict = {}
        with self.holding_platform_lock() as (_conn, txn):
            threads = [
                self.request_async(self.a, self.b, results, "a>b", is_platform_admin=False),
                self.request_async(self.b, self.c, results, "b>c", is_platform_admin=False),
                self.request_async(self.c, self.a, results, "c>a", is_platform_admin=False),
            ]
            time.sleep(1.0)
            self.assertTrue(all(t.is_alive() for t in threads))
            txn.commit()
        self.join_all(threads)
        statuses = sorted(s for s, _ in results.values())
        self.assertIn(400, statuses, results)
        self.assertNotIn(500, statuses, results)
        self.assertGreaterEqual(self.eligible_admins(), 1)

    def test_a_change_that_cannot_reduce_the_set_does_not_wait_for_the_lock(self):
        results: dict = {}
        with self.holding_platform_lock() as (_conn, _txn):
            started = time.monotonic()
            self.request_async(self.a, self.b, results, "promote", is_platform_admin=True).join(10)
            self.assertLess(time.monotonic() - started, 5.0)
            self.assertEqual(results["promote"][0], 200, results)

    def test_the_lock_is_released_after_every_outcome(self):
        self.assertEqual(self.patch_now(self.a, self.b, is_platform_admin=False)[0], 200)
        self.assertEqual(self.advisory_locks_held(), 0, "after a success")
        self.assertEqual(self.patch_now(self.a, self.a, is_platform_admin=False)[0], 400)
        self.assertEqual(self.advisory_locks_held(), 0, "after a self-lockout refusal")
        # B is no longer an administrator -> refused at authentication, before the lock.
        self.assertEqual(self.patch_now(self.b, self.a, is_platform_admin=False)[0], 403)
        self.assertEqual(self.advisory_locks_held(), 0, "after an authorization refusal")

    def test_free_running_mutual_removal_never_leaves_zero_administrators_or_errors(self):
        """No forced ordering: the requests overlap however the scheduler decides."""
        for iteration in range(12):
            with self.subTest(iteration=iteration):
                db.session.remove()
                db.session.execute(text(
                    "UPDATE users SET is_platform_admin = true, is_active = true "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": [self.a["id"], self.b["id"]]})
                db.session.commit()
                db.session.remove()
                barrier = threading.Barrier(2)
                results: dict = {}

                def go(actor, target, key):
                    barrier.wait()
                    with self.app.app_context():
                        try:
                            r = self.app.test_client().patch(
                                f"/api/v1/admin/users/{target['id']}",
                                headers=self.headers(actor),
                                json={"is_platform_admin": False})
                            results[key] = r.status_code
                        finally:
                            db.session.remove()

                threads = [
                    threading.Thread(target=go, args=(self.a, self.b, "a>b"), daemon=True),
                    threading.Thread(target=go, args=(self.b, self.a, "b>a"), daemon=True),
                ]
                for t in threads:
                    t.start()
                self.join_all(threads)
                self.assertEqual(sorted(results.values())[0], 200, results)
                self.assertEqual(sum(1 for s in results.values() if s == 200), 1, results)
                self.assertTrue(all(s < 500 for s in results.values()),
                                f"a request failed with a server error: {results}")
                self.assertEqual(self.eligible_admins(), 1)


class WhyTheLockIsNeededTests(PgAdminCase):
    """Negative control: the very same interleaving, with and without the lock.

    ``other_eligible_admin_exists`` is wrapped so each request pauses AFTER it has
    counted the other administrator and BEFORE it writes -- the window a bare
    count-before-update check leaves open.
    """

    def interleave(self, *, with_lock: bool):
        barrier = threading.Barrier(2)
        real_check = platform_admins.other_eligible_admin_exists

        def check_then_pause(user_id):
            answer = real_check(user_id)
            try:
                barrier.wait(timeout=2.5)
            except threading.BrokenBarrierError:
                pass          # the other request is parked on the lock: carry on
            return answer

        patches = [patch.object(platform_admins, "other_eligible_admin_exists", check_then_pause)]
        if not with_lock:
            patches.append(patch.object(platform_admins, "lock_platform_admins", lambda: None))
        results: dict = {}
        for p in patches:
            p.start()
        try:
            threads = [
                self.request_async(self.a, self.b, results, "a>b", is_platform_admin=False),
                self.request_async(self.b, self.a, results, "b>a", is_platform_admin=False),
            ]
            self.join_all(threads)
        finally:
            for p in patches:
                p.stop()
        return results

    def test_without_the_lock_both_demotions_succeed_and_nobody_is_left(self):
        results = self.interleave(with_lock=False)
        self.assertEqual(sorted(s for s, _ in results.values()), [200, 200], results)
        self.assertEqual(self.eligible_admins(), 0,
                         "control: a count-before-update check alone empties the platform")

    def test_with_the_lock_the_same_interleaving_is_safe(self):
        results = self.interleave(with_lock=True)
        self.assertEqual(sorted(s for s, _ in results.values()), [200, 400], results)
        self.assertEqual(self.eligible_admins(), 1)


class EligibilityOnRealPostgresTests(PgAdminCase):
    def stale_actor_scenario(self):
        """A (caller) asks to demote B; while A waits, another request demotes A."""
        results: dict = {}
        with self.holding_platform_lock() as (conn, txn):
            thread = self.request_async(self.a, self.b, results, "r", is_platform_admin=False)
            time.sleep(1.0)
            self.assertTrue(thread.is_alive())
            conn.execute(text("UPDATE users SET is_platform_admin = false WHERE id = :i"),
                         {"i": self.a["id"]})
            txn.commit()
        self.join_all([thread])
        return results["r"]

    def test_an_administrator_with_an_unconfirmed_email_is_not_a_survivor(self):
        self.mk_user("d@platform.pg.test", confirmed=False)      # admin, active, NOT confirmed
        status, body = self.stale_actor_scenario()
        self.assert_loser(status, body)
        self.assertEqual(self.flags(self.b), (True, True))

    def test_an_inactive_administrator_is_not_a_survivor(self):
        self.mk_user("d@platform.pg.test", active=False)          # admin, confirmed, INACTIVE
        status, body = self.stale_actor_scenario()
        self.assert_loser(status, body)

    def test_a_confirmed_active_administrator_is_a_survivor(self):
        self.make_admin(self.c)                                   # confirmed and active
        status, body = self.stale_actor_scenario()
        self.assertEqual(status, 200, body)
        self.assertEqual(self.flags(self.b), (False, True))
        self.assertEqual(self.flags(self.c), (True, True))

    def test_email_confirmed_later_makes_the_administrator_a_survivor(self):
        unconfirmed = self.mk_user("d@platform.pg.test", confirmed=False)
        db.session.execute(text("INSERT INTO public.stub_auth_users VALUES (:s, now())"),
                           {"s": unconfirmed["sub"]})
        db.session.commit()
        db.session.remove()
        status, body = self.stale_actor_scenario()
        self.assertEqual(status, 200, body)


class IsolationAssumptionTests(PgAdminCase):
    def test_transactions_run_at_read_committed(self):
        """The recount after the lock only sees the previous holder's commit under
        READ COMMITTED (see platform_admins module docstring).  Tripwire: fails if
        someone sets a stricter ``isolation_level`` on the engine or database."""
        db.session.remove()
        level = db.session.execute(text("SHOW transaction_isolation")).scalar()
        db.session.remove()
        self.assertEqual(level, "read committed")
        self.assertNotIn("isolation_level", self.app.config["SQLALCHEMY_ENGINE_OPTIONS"])


class FailClosedAndRollbackTests(PgAdminCase):
    def test_missing_confirmation_function_fails_closed_without_poisoning_the_transaction(self):
        db.session.execute(text("DROP FUNCTION public.is_email_confirmed(uuid)"))
        db.session.commit()
        self.assertFalse(rbac.email_confirmed_for_subject(self.b["sub"]))
        # Before the SAVEPOINT fix this raised InFailedSqlTransaction.
        self.assertEqual(db.session.execute(text("SELECT 1")).scalar(), 1)
        db.session.remove()

    def test_with_the_function_missing_requests_get_a_clean_403_not_a_500(self):
        db.session.execute(text("DROP FUNCTION public.is_email_confirmed(uuid)"))
        db.session.commit()
        db.session.remove()
        status, body = self.patch_now(self.a, self.b, is_platform_admin=False)
        self.assertEqual(status, 403, body)
        self.assertEqual(self.flags(self.b), (True, True))

    def test_an_unexpected_failure_after_the_change_rolls_everything_back(self):
        with patch("smartdesk.api.admin_api.record_audit", side_effect=RuntimeError("boom")):
            status, _body = self.patch_now(self.a, self.b, is_platform_admin=False, is_active=False)
        self.assertEqual(status, 500)
        self.assertEqual(self.flags(self.b), (True, True), "nothing may persist")
        self.assertEqual(self.audits(), 0)
        self.assertEqual(self.advisory_locks_held(), 0)
        # The platform is fully usable afterwards.
        self.assertEqual(self.patch_now(self.a, self.b, is_platform_admin=False)[0], 200)

    def test_a_refusal_leaves_no_partial_state_and_releases_the_lock(self):
        results: dict = {}
        with self.holding_platform_lock() as (conn, txn):
            thread = self.request_async(self.a, self.b, results, "r",
                                        is_platform_admin=False, is_active=False)
            time.sleep(1.0)
            conn.execute(text(
                "UPDATE users SET is_platform_admin = false WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": [self.a["id"]]})
            txn.commit()
        self.join_all([thread])
        self.assert_loser(*results["r"])
        self.assertEqual(self.flags(self.b), (True, True))
        self.assertEqual(self.audits(), 0)
        self.assertEqual(self.advisory_locks_held(), 0)


if __name__ == "__main__":
    unittest.main()
