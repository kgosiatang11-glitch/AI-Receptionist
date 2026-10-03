"""Service-behaviour tests for atomic usage reservations (Phase 3.3A, SQLite).

IMPORTANT: these run on SQLite, which serialises all writers.  They prove the
service's *logic* (state machine, idempotency, limit arithmetic, period
handling) -- they do NOT prove the PostgreSQL concurrency guarantee.  That is
proven by ``tests/test_usage_postgres.py`` against a real PostgreSQL database
with real concurrent connections.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from sqlalchemy.exc import IntegrityError  # noqa: E402

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Tenant,
    TenantUsagePeriod,
    UsageEvent,
    UsageReservation,
)
from smartdesk.services import usage  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; they are UTC by construction."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class PeriodHelperTests(unittest.TestCase):
    def test_utc_calendar_month_bounds(self):
        start, end = usage.usage_period_for(T0)
        self.assertEqual(start, datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, tzinfo=timezone.utc))

    def test_december_rolls_into_next_year(self):
        start, end = usage.usage_period_for(datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_boundaries_start_inclusive_end_exclusive(self):
        self.assertEqual(
            usage.usage_period_for(datetime(2026, 11, 1, tzinfo=timezone.utc))[0],
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            usage.usage_period_for(datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=timezone.utc))[1],
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        )

    def test_non_utc_input_is_converted_not_read_as_local(self):
        minus_five = timezone(timedelta(hours=-5))  # 23:30 31 Oct = 04:30 1 Nov UTC
        start, _ = usage.usage_period_for(datetime(2026, 10, 31, 23, 30, tzinfo=minus_five))
        self.assertEqual(start, datetime(2026, 11, 1, tzinfo=timezone.utc))
        plus_two = timezone(timedelta(hours=2))  # Botswana: 00:30 1 Nov = 22:30 31 Oct UTC
        start, _ = usage.usage_period_for(datetime(2026, 11, 1, 0, 30, tzinfo=plus_two))
        self.assertEqual(start, datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_naive_datetime_is_defined_as_utc(self):
        start, _ = usage.usage_period_for(datetime(2026, 10, 31, 23, 30))
        self.assertEqual(start, datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_result_is_aware_utc(self):
        for value in usage.usage_period_for(T0):
            self.assertEqual(value.utcoffset(), timedelta(0))


class UsageServiceCase(MultiTenantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_limit(self.padel, 3)

    def set_limit(self, tenant: Tenant, limit: int) -> None:
        tenant.monthly_conversation_limit = limit
        db.session.commit()

    def reserve(self, key="k1", tenant=None, **kwargs):
        kwargs.setdefault("now", T0)
        result = usage.reserve_ai_reply((tenant or self.padel).id, key, **kwargs)
        db.session.commit()
        return result

    def used(self, tenant=None) -> int:
        return usage.get_current_usage((tenant or self.padel).id, now=T0).used_units

    def count(self, model, tenant=None) -> int:
        return model.query.filter_by(tenant_id=(tenant or self.padel).id).count()


class PeriodCreationTests(UsageServiceCase):
    def test_1_creates_usage_period(self):
        self.assertEqual(self.count(TenantUsagePeriod), 0)
        self.reserve()
        period = TenantUsagePeriod.query.filter_by(tenant_id=self.padel.id).one()
        self.assertEqual(aware(period.period_start), datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(aware(period.period_end), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(period.used_units, 1)

    def test_2_same_tenant_same_month_reuses_period(self):
        self.reserve("a")
        self.reserve("b", now=T0 + timedelta(days=5))
        self.assertEqual(self.count(TenantUsagePeriod), 1)
        self.assertEqual(TenantUsagePeriod.query.one().used_units, 2)

    def test_3_different_tenants_get_separate_periods(self):
        self.set_limit(self.salon, 3)
        self.reserve("a", tenant=self.padel)
        self.reserve("a", tenant=self.salon)
        periods = TenantUsagePeriod.query.all()
        self.assertEqual(len(periods), 2)
        self.assertEqual({p.tenant_id for p in periods}, {self.padel.id, self.salon.id})
        self.assertEqual([p.used_units for p in periods], [1, 1])

    def test_new_month_gets_a_fresh_period_and_fresh_capacity(self):
        for key in ("a", "b", "c"):
            self.assertTrue(self.reserve(key).allowed)
        self.assertFalse(self.reserve("d").allowed)
        self.assertTrue(self.reserve("e", now=T0 + timedelta(days=30)).allowed)
        self.assertEqual(self.count(TenantUsagePeriod), 2)


class ReserveTests(UsageServiceCase):
    def test_4_first_reservation_succeeds(self):
        result = self.reserve()
        self.assertTrue(result.allowed)
        self.assertTrue(result.created)
        self.assertEqual(result.reason, "reserved")
        self.assertEqual(result.reservation.status, "reserved")
        self.assertEqual(result.reservation.kind, usage.AI_REPLY_KIND)
        self.assertEqual(result.reservation.units, 1)
        self.assertEqual(self.used(), 1)

    def test_5_reservation_exactly_reaching_the_limit_succeeds(self):
        self.assertTrue(all(self.reserve(f"k{i}").allowed for i in range(3)))
        snapshot = usage.get_current_usage(self.padel.id, now=T0)
        self.assertEqual((snapshot.used_units, snapshot.available_units), (3, 0))

    def test_6_reservation_beyond_the_limit_fails_and_leaves_no_row(self):
        for i in range(3):
            self.reserve(f"k{i}")
        denied = self.reserve("k3")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "quota_exceeded")
        self.assertIsNone(denied.reservation)
        self.assertFalse(denied.created)
        self.assertEqual(self.used(), 3)
        self.assertEqual(self.count(UsageReservation), 3)

    def test_denied_attempt_can_succeed_later_once_capacity_returns(self):
        first = [self.reserve(f"k{i}") for i in range(3)]
        self.assertFalse(self.reserve("late").allowed)
        usage.release_reservation(self.padel.id, first[0].reservation.id, now=T0)
        db.session.commit()
        self.assertTrue(self.reserve("late").allowed)

    def test_multi_unit_reservation_respects_the_limit(self):
        self.assertTrue(self.reserve("big", units=3).allowed)
        self.assertFalse(self.reserve("one-more").allowed)
        self.assertEqual(self.used(), 3)
        self.assertFalse(self.reserve("too-big", tenant=self.salon, units=501).allowed)

    def test_7_duplicate_idempotency_key_consumes_no_extra_unit(self):
        first = self.reserve("same")
        again = self.reserve("same")
        self.assertTrue(again.allowed)
        self.assertFalse(again.created)
        self.assertEqual(again.reason, "duplicate")
        self.assertTrue(again.duplicate)
        self.assertEqual(again.reservation.id, first.reservation.id)
        self.assertEqual(self.used(), 1)
        self.assertEqual(self.count(UsageReservation), 1)

    def test_duplicate_is_recovered_even_when_the_tenant_is_at_its_limit(self):
        first = self.reserve("a")
        self.reserve("b")
        self.reserve("c")
        self.assertFalse(self.reserve("d").allowed)
        again = self.reserve("a")
        self.assertTrue(again.allowed)
        self.assertEqual(again.reservation.id, first.reservation.id)
        self.assertEqual(self.used(), 3)

    def test_retry_of_a_released_key_does_not_consume_or_reallocate(self):
        first = self.reserve("a")
        usage.release_reservation(self.padel.id, first.reservation.id, now=T0)
        db.session.commit()
        retry = self.reserve("a")
        self.assertFalse(retry.allowed)
        self.assertEqual(retry.reason, "already_released")
        self.assertEqual(retry.reservation.status, "released")
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.count(UsageReservation), 1)

    def test_8_different_idempotency_keys_consume_separate_units(self):
        a, b = self.reserve("a"), self.reserve("b")
        self.assertNotEqual(a.reservation.id, b.reservation.id)
        self.assertEqual(self.used(), 2)

    def test_same_key_is_independent_per_kind_and_per_tenant(self):
        self.set_limit(self.salon, 3)
        self.reserve("x", kind="ai_reply")
        self.assertTrue(self.reserve("x", kind="other_kind").created)
        self.assertTrue(self.reserve("x", tenant=self.salon).created)
        self.assertEqual(self.count(UsageReservation), 2)
        self.assertEqual(self.count(UsageReservation, self.salon), 1)

    def test_metadata_is_stored(self):
        result = self.reserve("m", metadata={"channel": "whatsapp"})
        self.assertEqual(result.reservation.meta, {"channel": "whatsapp"})

    def test_tenants_do_not_consume_each_others_quota(self):
        self.set_limit(self.salon, 1)
        for i in range(3):
            self.reserve(f"p{i}", tenant=self.padel)
        self.assertFalse(self.reserve("p3", tenant=self.padel).allowed)
        self.assertTrue(self.reserve("s0", tenant=self.salon).allowed)
        self.assertFalse(self.reserve("s1", tenant=self.salon).allowed)

    def test_reserving_does_not_touch_usage_events_or_the_limit(self):
        before = UsageEvent.query.count()
        self.reserve()
        self.assertEqual(UsageEvent.query.count(), before)
        db.session.refresh(self.padel)
        self.assertEqual(self.padel.monthly_conversation_limit, 3)

    def test_invalid_arguments_are_rejected(self):
        for bad in (dict(idempotency_key=""), dict(idempotency_key="   "), dict(idempotency_key=None),
                    dict(idempotency_key="k" * 129), dict(idempotency_key="k", units=0),
                    dict(idempotency_key="k", units=-1), dict(idempotency_key="k", units=True),
                    dict(idempotency_key="k", units=1.5), dict(idempotency_key="k", kind="")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    usage.reserve_ai_reply(self.padel.id, now=T0, **bad)
        self.assertEqual(self.count(UsageReservation), 0)

    def test_unknown_tenant_is_rejected(self):
        with self.assertRaises(usage.TenantNotFound):
            usage.reserve_ai_reply("00000000-0000-0000-0000-000000000000", "k", now=T0)


class CommitTests(UsageServiceCase):
    def test_9_commit_records_token_counts(self):
        reservation = self.reserve().reservation
        committed = usage.commit_reservation(
            self.padel.id, reservation.id,
            prompt_tokens=120, completion_tokens=30, total_tokens=150,
            now=T0 + timedelta(seconds=2),
        )
        db.session.commit()
        self.assertEqual(committed.status, "committed")
        self.assertEqual(
            (committed.prompt_tokens, committed.completion_tokens, committed.total_tokens),
            (120, 30, 150),
        )
        self.assertEqual(aware(committed.committed_at), T0 + timedelta(seconds=2))
        self.assertIsNone(committed.released_at)
        self.assertEqual(self.used(), 1)  # committing never changes the counter

    def test_commit_without_usage_keeps_tokens_unknown(self):
        reservation = self.reserve().reservation
        committed = usage.commit_reservation(self.padel.id, reservation.id, now=T0)
        self.assertEqual(committed.status, "committed")
        self.assertEqual(
            (committed.prompt_tokens, committed.completion_tokens, committed.total_tokens),
            (None, None, None),
        )

    def test_10_repeated_commit_is_safe_and_keeps_first_tokens(self):
        reservation = self.reserve().reservation
        usage.commit_reservation(self.padel.id, reservation.id, prompt_tokens=10,
                                 completion_tokens=5, total_tokens=15, now=T0)
        db.session.commit()
        again = usage.commit_reservation(self.padel.id, reservation.id, prompt_tokens=999,
                                         completion_tokens=999, total_tokens=999, now=T0)
        db.session.commit()
        self.assertEqual(again.status, "committed")
        self.assertEqual(again.total_tokens, 15)
        self.assertEqual(self.used(), 1)

    def test_14_released_reservation_cannot_be_committed(self):
        reservation = self.reserve().reservation
        usage.release_reservation(self.padel.id, reservation.id, now=T0)
        db.session.commit()
        with self.assertRaises(usage.ReservationStateError):
            usage.commit_reservation(self.padel.id, reservation.id, total_tokens=5, now=T0)
        fresh = usage.get_reservation(self.padel.id, reservation.id)
        self.assertEqual(fresh.status, "released")
        self.assertIsNone(fresh.total_tokens)
        self.assertEqual(self.used(), 0)

    def test_invalid_token_counts_are_rejected(self):
        reservation = self.reserve().reservation
        for bad in (-1, True, 1.5, "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    usage.commit_reservation(self.padel.id, reservation.id, prompt_tokens=bad)
        self.assertEqual(usage.get_reservation(self.padel.id, reservation.id).status, "reserved")

    def test_unknown_reservation_is_not_found(self):
        with self.assertRaises(usage.ReservationNotFound):
            usage.commit_reservation(self.padel.id, "no-such-id")

    def test_another_tenant_cannot_commit_a_reservation(self):
        reservation = self.reserve().reservation
        with self.assertRaises(usage.ReservationNotFound):
            usage.commit_reservation(self.salon.id, reservation.id)
        self.assertEqual(usage.get_reservation(self.padel.id, reservation.id).status, "reserved")


class ReleaseTests(UsageServiceCase):
    def test_11_release_returns_capacity(self):
        reservations = [self.reserve(f"k{i}").reservation for i in range(3)]
        self.assertFalse(self.reserve("extra").allowed)
        released = usage.release_reservation(self.padel.id, reservations[0].id, now=T0)
        db.session.commit()
        self.assertEqual(released.status, "released")
        self.assertEqual(aware(released.released_at), T0)
        self.assertEqual(self.used(), 2)
        self.assertTrue(self.reserve("extra").allowed)
        self.assertEqual(self.used(), 3)

    def test_12_repeated_release_is_safe_and_decrements_once(self):
        a = self.reserve("a").reservation
        self.reserve("b")
        self.assertEqual(self.used(), 2)
        for _ in range(3):
            usage.release_reservation(self.padel.id, a.id, now=T0)
            db.session.commit()
        self.assertEqual(self.used(), 1)

    def test_13_committed_reservation_cannot_be_released(self):
        reservation = self.reserve().reservation
        usage.commit_reservation(self.padel.id, reservation.id, total_tokens=7, now=T0)
        db.session.commit()
        with self.assertRaises(usage.ReservationStateError):
            usage.release_reservation(self.padel.id, reservation.id, now=T0)
        self.assertEqual(usage.get_reservation(self.padel.id, reservation.id).status, "committed")
        self.assertEqual(self.used(), 1)

    def test_used_units_never_goes_negative(self):
        reservation = self.reserve().reservation
        TenantUsagePeriod.query.update({"used_units": 0})  # corrupt below the reservation's units
        db.session.commit()
        usage.release_reservation(self.padel.id, reservation.id, now=T0)
        db.session.commit()
        self.assertEqual(self.used(), 0)

    def test_unknown_reservation_is_not_found(self):
        with self.assertRaises(usage.ReservationNotFound):
            usage.release_reservation(self.padel.id, "no-such-id")

    def test_another_tenant_cannot_release_a_reservation(self):
        reservation = self.reserve().reservation
        with self.assertRaises(usage.ReservationNotFound):
            usage.release_reservation(self.salon.id, reservation.id)
        self.assertEqual(self.used(), 1)


class StaleReservationTests(UsageServiceCase):
    def test_15_stale_reservations_are_recovered(self):
        for i in range(3):
            self.reserve(f"k{i}", now=T0)
        self.assertFalse(self.reserve("blocked", now=T0).allowed)
        result = usage.release_stale_reservations(
            stale_after=timedelta(minutes=10), now=T0 + timedelta(minutes=11)
        )
        db.session.commit()
        self.assertEqual((result.released, result.units), (3, 3))
        self.assertEqual(self.used(), 0)
        self.assertEqual({r.status for r in UsageReservation.query.all()}, {"released"})
        self.assertTrue(self.reserve("now-fits", now=T0 + timedelta(minutes=12)).allowed)

    def test_only_old_reserved_rows_are_released(self):
        old = self.reserve("old", now=T0).reservation
        committed = self.reserve("done", now=T0).reservation
        usage.commit_reservation(self.padel.id, committed.id, total_tokens=1, now=T0)
        fresh = self.reserve("fresh", now=T0 + timedelta(minutes=9)).reservation
        db.session.commit()
        result = usage.release_stale_reservations(
            stale_after=timedelta(minutes=10), now=T0 + timedelta(minutes=11)
        )
        db.session.commit()
        self.assertEqual(result.released, 1)
        self.assertEqual(usage.get_reservation(self.padel.id, old.id).status, "released")
        self.assertEqual(usage.get_reservation(self.padel.id, committed.id).status, "committed")
        self.assertEqual(usage.get_reservation(self.padel.id, fresh.id).status, "reserved")
        self.assertEqual(self.used(), 2)

    def test_safe_to_call_repeatedly(self):
        self.reserve("a", now=T0)
        args = dict(stale_after=timedelta(minutes=10), now=T0 + timedelta(minutes=11))
        self.assertEqual(usage.release_stale_reservations(**args).released, 1)
        db.session.commit()
        for _ in range(2):
            self.assertEqual(usage.release_stale_reservations(**args).released, 0)
            db.session.commit()
        self.assertEqual(self.used(), 0)

    def test_tenant_filter_and_batch_size(self):
        self.set_limit(self.salon, 3)
        for i in range(3):
            self.reserve(f"p{i}", now=T0)
        self.reserve("s0", tenant=self.salon, now=T0)
        later = T0 + timedelta(minutes=11)
        only_salon = usage.release_stale_reservations(
            stale_after=timedelta(minutes=10), now=later, tenant_id=self.salon.id
        )
        self.assertEqual(only_salon.released, 1)
        batch = usage.release_stale_reservations(
            stale_after=timedelta(minutes=10), now=later, batch_size=2
        )
        self.assertEqual(batch.released, 2)
        rest = usage.release_stale_reservations(stale_after=timedelta(minutes=10), now=later)
        self.assertEqual(rest.released, 1)
        db.session.commit()
        self.assertEqual(self.used(), 0)

    def test_default_timeout_is_configurable_and_invalid_values_fall_back(self):
        self.assertEqual(usage.stale_after_default(), timedelta(seconds=600))
        for bad in (0, -5, "abc", None, True, 10**9):
            with self.subTest(bad=bad):
                self.app.config["USAGE_RESERVATION_STALE_SECONDS"] = bad
                self.assertEqual(usage.stale_after_default(), timedelta(seconds=600))
        self.app.config["USAGE_RESERVATION_STALE_SECONDS"] = 60
        self.reserve("a", now=T0)
        self.assertEqual(usage.release_stale_reservations(now=T0 + timedelta(seconds=61)).released, 1)

    def test_non_positive_timeout_is_rejected(self):
        for bad in (timedelta(0), timedelta(seconds=-1)):
            with self.assertRaises(ValueError):
                usage.release_stale_reservations(stale_after=bad, now=T0)


class UsageReadTests(UsageServiceCase):
    def test_16_current_usage_is_correct(self):
        self.reserve("a")
        self.reserve("b")
        snapshot = usage.get_current_usage(self.padel.id, now=T0)
        self.assertEqual(snapshot.tenant_id, self.padel.id)
        self.assertEqual(snapshot.period_start, datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(snapshot.period_end, datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(snapshot.configured_limit, 3)
        self.assertEqual(snapshot.used_units, 2)
        self.assertEqual(snapshot.available_units, 1)

    def test_read_with_no_usage_does_not_create_a_period(self):
        snapshot = usage.get_current_usage(self.padel.id, now=T0)
        self.assertEqual((snapshot.used_units, snapshot.available_units), (0, 3))
        self.assertEqual(TenantUsagePeriod.query.count(), 0)

    def test_usage_is_per_month_and_per_tenant(self):
        self.reserve("a")
        self.assertEqual(usage.get_current_usage(self.padel.id, now=T0 + timedelta(days=30)).used_units, 0)
        self.assertEqual(usage.get_current_usage(self.salon.id, now=T0).used_units, 0)

    def test_unknown_tenant_is_rejected(self):
        with self.assertRaises(usage.TenantNotFound):
            usage.get_current_usage("00000000-0000-0000-0000-000000000000")


class ZeroAndNegativeLimitTests(UsageServiceCase):
    def test_17_zero_and_negative_limits_never_create_unlimited_capacity(self):
        for limit in (0, -1, -100, -(2**31) + 1):
            with self.subTest(limit=limit):
                self.set_limit(self.padel, limit)
                for i in range(3):
                    result = self.reserve(f"z{limit}-{i}")
                    self.assertFalse(result.allowed)
                    self.assertEqual(result.reason, "quota_exceeded")
                snapshot = usage.get_current_usage(self.padel.id, now=T0)
                self.assertEqual(snapshot.configured_limit, limit)  # stored value untouched
                self.assertEqual(snapshot.available_units, 0)
                self.assertEqual(snapshot.used_units, 0)
                self.assertEqual(self.count(UsageReservation), 0)

    def test_lowering_the_limit_below_current_usage_blocks_new_reservations(self):
        for i in range(3):
            self.reserve(f"k{i}")
        self.set_limit(self.padel, 1)
        self.assertFalse(self.reserve("new").allowed)
        snapshot = usage.get_current_usage(self.padel.id, now=T0)
        self.assertEqual((snapshot.used_units, snapshot.available_units), (3, 0))

    def test_raising_the_limit_makes_capacity_available(self):
        for i in range(3):
            self.reserve(f"k{i}")
        self.assertFalse(self.reserve("x").allowed)
        self.set_limit(self.padel, 5)
        self.assertTrue(self.reserve("x").allowed)


class ModelConstraintTests(UsageServiceCase):
    """The same CHECK/UNIQUE constraints the migration creates, on SQLite."""

    def _period(self, **kw) -> TenantUsagePeriod:
        values = dict(
            tenant_id=self.salon.id,
            period_start=datetime(2026, 10, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 11, 1, tzinfo=timezone.utc),
            used_units=0,
        )
        values.update(kw)
        return TenantUsagePeriod(**values)

    def _reservation(self, period_id, **kw) -> UsageReservation:
        values = dict(tenant_id=self.salon.id, usage_period_id=period_id, kind="ai_reply",
                      idempotency_key="k", units=1, status="reserved", reserved_at=T0)
        values.update(kw)
        return UsageReservation(**values)

    def assertRejected(self, obj):
        db.session.add(obj)
        with self.assertRaises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_period_constraints(self):
        self.assertRejected(self._period(used_units=-1))
        self.assertRejected(self._period(period_end=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        self.assertRejected(self._period(
            period_start=datetime(2026, 11, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        db.session.add(self._period())
        db.session.commit()
        self.assertRejected(self._period())  # unique tenant + window

    def test_reservation_constraints(self):
        period = self._period()
        db.session.add(period)
        db.session.commit()
        self.assertRejected(self._reservation(period.id, units=0))
        self.assertRejected(self._reservation(period.id, status="bogus"))
        self.assertRejected(self._reservation(period.id, status="committed"))  # no committed_at
        self.assertRejected(self._reservation(period.id, status="reserved", released_at=T0))
        self.assertRejected(self._reservation(period.id, total_tokens=-1))
        db.session.add(self._reservation(period.id))
        db.session.commit()
        self.assertRejected(self._reservation(period.id))  # unique tenant + kind + key


if __name__ == "__main__":
    unittest.main()
