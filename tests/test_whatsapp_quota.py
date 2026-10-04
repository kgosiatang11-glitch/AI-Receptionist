"""Phase 3.3B: WhatsApp AI replies are gated by the atomic usage reservation.

Behaviour tests on SQLite.  SQLite serialises all writers, so these prove the
webhook's LOGIC (what is reserved, committed, released, refused, and that
OpenAI is never reached without a reservation).  They do NOT prove the
concurrency guarantee -- ``tests/test_whatsapp_quota_postgres.py`` does that
with real concurrent PostgreSQL connections.

Every test that matters measures whether OpenAI was actually called with the
fake client's own call counter, not just database state.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.channels import webhooks  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Channel,
    Message,
    ReceptionistProfile,
    TenantUsagePeriod,
    UsageEvent,
    UsageReservation,
)
from smartdesk.services import usage as usage_service  # noqa: E402
from tests.test_whatsapp_idempotency import (  # noqa: E402
    AI_QUESTION,
    CUSTOMER,
    PADEL_NUMBER,
    SALON_NUMBER,
    WhatsAppWebhookTestCase,
    new_sid,
)

LIMIT_TEXT = b"monthly conversation limit"
ENGINE_FALLBACK = b"i don't have that information available at the moment"


class QuotaOpenAI:
    """Fake OpenAI client that counts calls and records the DB state at the
    moment of each request (is a transaction open?)."""

    def __init__(self, usage="default", fail: Exception | None = None, content="AI reply.") -> None:
        self.calls: list[dict] = []
        self.transaction_open_at_call: list[bool] = []
        self.usage = usage
        self.fail = fail
        self.content = content
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                try:
                    outer.transaction_open_at_call.append(db.session().in_transaction())
                except RuntimeError:  # engine-only tests have no app context
                    outer.transaction_open_at_call.append(False)
                if outer.fail is not None:
                    raise outer.fail
                response = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=outer.content))]
                )
                if outer.usage == "default":
                    response.usage = SimpleNamespace(
                        prompt_tokens=123, completion_tokens=45, total_tokens=168
                    )
                elif outer.usage is not None:
                    response.usage = outer.usage
                return response

        self.chat = SimpleNamespace(completions=_Completions())

    @property
    def count(self) -> int:
        return len(self.calls)


class QuotaCase(WhatsAppWebhookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.set_limit(self.padel, 3)
        self.set_limit(self.salon, 3)
        self.ai = QuotaOpenAI()
        patcher = patch("smartdesk.services.receptionist.openai_client", return_value=self.ai)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    def set_limit(self, tenant, limit: int) -> None:
        tenant.monthly_conversation_limit = limit
        db.session.commit()

    def reservations(self, tenant=None):
        db.session.expire_all()
        return (
            UsageReservation.query.filter_by(tenant_id=(tenant or self.padel).id)
            .order_by(UsageReservation.created_at)
            .all()
        )

    def statuses(self, tenant=None) -> list[str]:
        return [r.status for r in self.reservations(tenant)]

    def used(self, tenant=None) -> int:
        db.session.expire_all()
        snapshot = usage_service.get_current_usage((tenant or self.padel).id)
        return snapshot.used_units

    def events(self, kind: str, tenant=None) -> list[UsageEvent]:
        db.session.expire_all()
        return UsageEvent.query.filter_by(
            tenant_id=(tenant or self.padel).id, kind=kind
        ).all()

    def ai_post(self, sender=CUSTOMER, sid=None, to=PADEL_NUMBER, body=AI_QUESTION):
        return self.post(sid or new_sid(), body=body, sender=sender, to=to)


# ---------------------------------------------------------------------------
# 1-3, 20-23: reserve -> OpenAI -> commit / release
# ---------------------------------------------------------------------------


class ReserveCommitReleaseTests(QuotaCase):
    def test_1_an_ai_reply_reserves_exactly_one_unit(self):
        self.ai_post()
        self.assertEqual(self.ai.count, 1)
        self.assertEqual(len(self.reservations()), 1)
        self.assertEqual(self.used(), 1)

    def test_2_a_successful_reply_commits_the_reservation(self):
        sid = new_sid()
        response = self.ai_post(sid=sid)
        self.assertIn(b"AI reply.", response.data)
        reservation = self.reservations()[0]
        self.assertEqual(reservation.status, "committed")
        self.assertIsNotNone(reservation.committed_at)
        self.assertIsNone(reservation.released_at)
        self.assertEqual(self.used(), 1)  # committing never changes the counter

    def test_idempotency_key_is_the_namespaced_message_sid(self):
        sid = new_sid()
        self.ai_post(sid=sid)
        reservation = self.reservations()[0]
        self.assertEqual(reservation.idempotency_key, f"whatsapp:{sid}")
        self.assertEqual(reservation.kind, usage_service.AI_REPLY_KIND)
        self.assertEqual(reservation.units, 1)
        self.assertEqual(reservation.meta["channel"], "whatsapp")
        self.assertEqual(reservation.meta["conversation_id"], self.conversation().id)

    def test_3_an_openai_failure_releases_the_reservation(self):
        self.ai.fail = TimeoutError("simulated timeout")
        response = self.ai_post()
        self.assertEqual(response.status_code, 200)
        self.assertIn(ENGINE_FALLBACK, response.data.lower())
        self.assertEqual(self.ai.count, 1)
        self.assertEqual(self.statuses(), ["released"])
        self.assertEqual(self.used(), 0)  # the failed call did not consume quota

    def test_23_every_kind_of_openai_failure_releases(self):
        failures = {
            "timeout": TimeoutError("t"),
            "connection": ConnectionError("c"),
            "api error": RuntimeError("500 from OpenAI"),
            "unexpected": ValueError("boom"),
        }
        for label, exc in failures.items():
            with self.subTest(label):
                self.ai.fail = exc
                response = self.ai_post()
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b"boom", response.data)  # no internals to the customer
                self.assertNotIn(b"500 from", response.data)
        self.assertEqual(self.statuses(), ["released"] * len(failures))
        self.assertEqual(self.used(), 0)

    def test_malformed_openai_response_releases(self):
        self.ai.content = None  # .strip() on None -> the reply is unusable
        response = self.ai_post()
        self.assertIn(ENGINE_FALLBACK, response.data.lower())
        self.assertEqual(self.statuses(), ["released"])
        self.assertEqual(self.used(), 0)

    def test_failure_then_success_does_not_leak_capacity(self):
        self.set_limit(self.padel, 1)
        self.ai.fail = RuntimeError("down")
        self.ai_post()
        self.ai.fail = None
        self.assertIn(b"AI reply.", self.ai_post().data)  # the single unit is still available
        self.assertEqual(self.statuses(), ["released", "committed"])
        self.assertEqual(self.used(), 1)

    def test_20_token_metadata_reaches_the_reservation(self):
        self.ai_post()
        reservation = self.reservations()[0]
        self.assertEqual(
            (reservation.prompt_tokens, reservation.completion_tokens, reservation.total_tokens),
            (123, 45, 168),
        )

    def test_21_missing_usage_is_safe_and_stays_unknown(self):
        for label, usage in (("absent", None), ("partial", SimpleNamespace(prompt_tokens=7))):
            with self.subTest(label):
                self.ai.usage = usage
                response = self.ai_post()
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"AI reply.", response.data)
        first, second = self.reservations()
        self.assertEqual(
            (first.status, first.prompt_tokens, first.completion_tokens, first.total_tokens),
            ("committed", None, None, None),
        )
        self.assertEqual(
            (second.status, second.prompt_tokens, second.completion_tokens, second.total_tokens),
            ("committed", 7, None, None),
        )

    def test_22_commit_is_attempted_exactly_once_and_release_never(self):
        with patch.object(
            usage_service, "commit_reservation", wraps=usage_service.commit_reservation
        ) as commit, patch.object(
            usage_service, "release_reservation", wraps=usage_service.release_reservation
        ) as release:
            self.ai_post()
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(release.call_count, 0)

    def test_release_is_attempted_exactly_once_on_failure_and_commit_never(self):
        self.ai.fail = RuntimeError("down")
        with patch.object(
            usage_service, "commit_reservation", wraps=usage_service.commit_reservation
        ) as commit, patch.object(
            usage_service, "release_reservation", wraps=usage_service.release_reservation
        ) as release:
            self.ai_post()
        self.assertEqual((commit.call_count, release.call_count), (0, 1))

    def test_a_successful_reply_is_persisted_once(self):
        self.ai_post()
        conv = self.conversation()
        self.assertEqual(len(self.rows(conv, "customer")), 1)
        assistant = self.rows(conv, "assistant")
        self.assertEqual([m.body for m in assistant], ["AI reply."])


# ---------------------------------------------------------------------------
# 4-8: exhaustion, limit response, existing/new customers, many replies
# ---------------------------------------------------------------------------


class ExhaustionTests(QuotaCase):
    def exhaust(self, tenant=None, to=PADEL_NUMBER, limit=1):
        self.set_limit(tenant or self.padel, limit)
        for i in range(limit):
            response = self.ai_post(sender=f"+2677400000{i}", to=to)
            self.assertIn(b"AI reply.", response.data)
        self.assertEqual(self.ai.count, limit)

    def test_4_exhausted_quota_prevents_the_openai_call(self):
        self.exhaust()
        response = self.ai_post(sender="+26774009999")
        self.assertEqual(self.ai.count, 1, "OpenAI was called although quota was exhausted")
        self.assertIn(LIMIT_TEXT, response.data.lower())

    def test_5_the_limit_response_is_deterministic_and_stores_no_ai_message(self):
        self.exhaust()
        sender = "+26774009999"
        response = self.ai_post(sender=sender)
        self.assertEqual(response.status_code, 200)
        self.assertIn(webhooks.LIMIT_REACHED_TEXT.encode().split(b" \xe2")[0][:30], response.data)
        conv = self.conversation(sender=sender)
        self.assertEqual(len(self.rows(conv, "customer")), 1)  # the inbound is still stored
        self.assertEqual(self.rows(conv, "assistant"), [])      # no AI message created
        self.assertEqual(conv.status, "needs_human")            # existing escalation behaviour
        self.assertEqual(self.ai.count, 1)
        self.assertEqual(len(self.reservations()), 1)            # no extra unit, no extra row
        self.assertEqual(self.used(), 1)

    def test_the_limit_response_does_not_use_the_engine_at_all(self):
        self.exhaust()
        from ai.receptionist import ReceptionistEngine
        with patch.object(ReceptionistEngine, "_generate_openai_reply") as generate:
            generate.side_effect = AssertionError("engine must not build an OpenAI request")
            # engine.reply still runs its local routing; the gate refuses at the call site.
            from ai.receptionist import AIUsageDenied
            generate.side_effect = AIUsageDenied("quota_exceeded")
            response = self.ai_post(sender="+26774009998")
        self.assertIn(LIMIT_TEXT, response.data.lower())
        self.assertEqual(self.ai.count, 1)

    def test_6_an_existing_customer_in_an_existing_conversation_is_still_controlled(self):
        self.set_limit(self.padel, 1)
        sender = "+26771111111"  # fixture customer with an existing conversation and message
        self.assertIn(b"AI reply.", self.ai_post(sender=sender).data)
        response = self.ai_post(sender=sender)
        self.assertIn(LIMIT_TEXT, response.data.lower())
        self.assertEqual(self.ai.count, 1)
        self.assertEqual(self.used(), 1)

    def test_7_a_new_customer_is_controlled(self):
        self.exhaust()
        response = self.ai_post(sender="+26774555555")  # never seen before
        self.assertIn(LIMIT_TEXT, response.data.lower())
        self.assertEqual(self.ai.count, 1)

    def test_8_many_replies_in_one_conversation_consume_many_units(self):
        self.set_limit(self.padel, 3)
        for i in range(3):
            self.assertIn(b"AI reply.", self.ai_post(body=f"{AI_QUESTION} #{i}").data)
        self.assertEqual(self.used(), 3)
        self.assertEqual(len({r.idempotency_key for r in self.reservations()}), 3)
        fourth = self.ai_post(body=f"{AI_QUESTION} #4")
        self.assertIn(LIMIT_TEXT, fourth.data.lower())
        self.assertEqual(self.ai.count, 3)
        self.assertEqual(Message.query.filter_by(
            conversation_id=self.conversation().id, role="assistant").count(), 3)

    def test_a_second_refused_message_adds_no_second_handoff_event(self):
        self.exhaust()
        sender = "+26774009997"
        self.ai_post(sender=sender)
        conv = self.conversation(sender=sender)
        events = len(self.rows(conv, "event"))
        self.assertEqual(events, 1)
        self.ai_post(sender=sender)
        self.assertEqual(len(self.rows(conv, "event")), events)
        self.assertEqual(len(self.rows(conv, "customer")), 2)  # both inbounds still stored

    def test_zero_and_negative_limits_mean_no_ai_capacity(self):
        for limit in (0, -1, -50):
            with self.subTest(limit=limit):
                self.set_limit(self.padel, limit)
                response = self.ai_post(sender=f"+267746{abs(limit):05d}")
                self.assertIn(LIMIT_TEXT, response.data.lower())
        self.assertEqual(self.ai.count, 0)
        self.assertEqual(self.reservations(), [])

    def test_raising_the_limit_restores_service_without_losing_history(self):
        self.exhaust()
        self.assertIn(LIMIT_TEXT, self.ai_post(sender="+26774009996").data.lower())
        self.set_limit(self.padel, 5)
        self.assertIn(b"AI reply.", self.ai_post(sender="+26774009995").data)
        self.assertEqual(self.ai.count, 2)

    def test_a_new_month_starts_with_fresh_capacity(self):
        self.exhaust()
        db.session.expire_all()
        period = TenantUsagePeriod.query.filter_by(tenant_id=self.padel.id).one()
        # Move the consumed period into the past: the current UTC month is empty.
        period.period_start = period.period_start - timedelta(days=62)
        period.period_end = period.period_start + timedelta(days=28)
        db.session.commit()
        self.assertIn(b"AI reply.", self.ai_post(sender="+26774009994").data)
        self.assertEqual(self.ai.count, 2)


# ---------------------------------------------------------------------------
# 9-12: idempotency and reservation interact correctly
# ---------------------------------------------------------------------------


class DuplicateMessageSidTests(QuotaCase):
    def test_9_10_11_a_retried_sid_reserves_nothing_calls_nothing_records_nothing(self):
        sid = new_sid()
        first = self.ai_post(sid=sid)
        self.assertIn(b"AI reply.", first.data)
        before = (len(self.reservations()), self.used(), self.ai.count,
                  len(self.events("message_in")), len(self.events("message_out")))
        for _ in range(3):
            retry = self.ai_post(sid=sid)
            self.assertEqual(retry.status_code, 200)
            self.assertFalse(self.has_reply(retry))
        after = (len(self.reservations()), self.used(), self.ai.count,
                 len(self.events("message_in")), len(self.events("message_out")))
        self.assertEqual(before, after)
        self.assertEqual(self.ai.count, 1, "OpenAI must run once across original + retries")
        self.assertEqual(len(self.rows(self.conversation(), "assistant")), 1)

    def test_a_retry_while_quota_is_exhausted_does_not_reserve_or_answer(self):
        self.set_limit(self.padel, 1)
        sid = new_sid()
        self.ai_post(sid=sid)
        retry = self.ai_post(sid=sid)
        self.assertFalse(self.has_reply(retry))
        self.assertEqual(self.used(), 1)
        self.assertEqual(self.ai.count, 1)

    def test_a_retry_of_a_failed_openai_attempt_is_a_duplicate_not_a_new_attempt(self):
        self.ai.fail = RuntimeError("down")
        sid = new_sid()
        first = self.ai_post(sid=sid)
        self.assertIn(ENGINE_FALLBACK, first.data.lower())
        self.ai.fail = None
        retry = self.ai_post(sid=sid)
        self.assertFalse(self.has_reply(retry))
        self.assertEqual(self.ai.count, 1)             # no second OpenAI call
        self.assertEqual(len(self.reservations()), 1)  # no second reservation
        self.assertEqual(self.used(), 0)

    def test_12_different_sids_consume_separate_units(self):
        self.ai_post(sid=new_sid())
        self.ai_post(sid=new_sid())
        self.assertEqual(self.used(), 2)
        self.assertEqual(self.ai.count, 2)
        self.assertEqual(len({r.idempotency_key for r in self.reservations()}), 2)

    def test_a_pre_existing_reservation_for_the_sid_never_authorises_openai(self):
        """If a reservation for this message already exists (an earlier attempt
        owns it), a new request must not call OpenAI nor answer."""
        sid = new_sid()
        usage_service.reserve_ai_reply(self.padel.id, f"whatsapp:{sid}")
        db.session.commit()
        before = self.used()
        response = self.ai_post(sid=sid)
        self.assertFalse(self.has_reply(response))
        self.assertEqual(self.ai.count, 0)
        self.assertEqual(self.used(), before)


# ---------------------------------------------------------------------------
# 15: tenants
# ---------------------------------------------------------------------------


class TenantIsolationTests(QuotaCase):
    def test_15_one_tenant_cannot_consume_anothers_quota(self):
        self.set_limit(self.padel, 1)
        self.set_limit(self.salon, 1)
        self.assertIn(b"AI reply.", self.ai_post(sender="+26774100001").data)
        self.assertIn(LIMIT_TEXT, self.ai_post(sender="+26774100002").data.lower())
        # The salon is untouched by the padel club's exhaustion.
        calls_before = self.ai.count
        salon = self.ai_post(sender="+26774100003", to=SALON_NUMBER)
        self.assertIn(b"AI reply.", salon.data)
        self.assertEqual(self.ai.count, calls_before + 1)
        self.assertEqual(self.used(self.padel), 1)
        self.assertEqual(self.used(self.salon), 1)
        self.assertIn(LIMIT_TEXT, self.ai_post(sender="+26774100004", to=SALON_NUMBER).data.lower())

    def test_reservations_are_recorded_against_the_right_tenant(self):
        self.ai_post(sender="+26774100005")
        self.ai_post(sender="+26774100006", to=SALON_NUMBER)
        self.assertEqual(len(self.reservations(self.padel)), 1)
        self.assertEqual(len(self.reservations(self.salon)), 1)
        keys = {r.idempotency_key for r in self.reservations(self.padel)}
        self.assertTrue(keys.isdisjoint({r.idempotency_key for r in self.reservations(self.salon)}))

    def test_the_same_sid_on_two_tenants_is_two_independent_messages(self):
        sid = new_sid()
        self.assertIn(b"AI reply.", self.ai_post(sid=sid, sender="+26774100007").data)
        self.assertIn(b"AI reply.", self.ai_post(sid=sid, sender="+26774100007", to=SALON_NUMBER).data)
        self.assertEqual((self.used(self.padel), self.used(self.salon)), (1, 1))


# ---------------------------------------------------------------------------
# 16-19: paths that must NOT reserve
# ---------------------------------------------------------------------------


class NoReservationPathTests(QuotaCase):
    def assertNoAiActivity(self):
        self.assertEqual(self.ai.count, 0)
        self.assertEqual(self.reservations(), [])
        self.assertEqual(self.reservations(self.salon), [])
        self.assertEqual(self.used(), 0)

    def test_16_a_suspended_tenant_does_not_reserve(self):
        self.padel.status = "suspended"
        db.session.commit()
        response = self.ai_post()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.has_reply(response))
        self.assertNoAiActivity()

    def test_17_a_disabled_channel_does_not_reserve(self):
        channel = Channel.query.filter_by(tenant_id=self.padel.id, kind="whatsapp").one()
        channel.is_active = False
        db.session.commit()
        response = self.ai_post()
        self.assertFalse(self.has_reply(response))
        self.assertNoAiActivity()

    def test_17_a_disabled_receptionist_does_not_reserve(self):
        profile = ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).one()
        profile.is_active = False
        db.session.commit()
        response = self.ai_post()
        self.assertFalse(self.has_reply(response))
        self.assertNoAiActivity()

    def test_18_human_takeover_does_not_reserve(self):
        self.padel_conversation.human_takeover = True
        db.session.commit()
        response = self.ai_post(sender="+26771111111")
        self.assertFalse(self.has_reply(response))
        self.assertNoAiActivity()
        self.assertEqual(self.events("message_out"), [])

    def test_19_local_replies_do_not_reserve(self):
        cases = {
            "setswana greeting": "Dumela",
            "handoff request": "I want to speak to a manager",
            "empty message": "   ",
        }
        for i, (label, body) in enumerate(cases.items()):
            with self.subTest(label):
                response = self.post(new_sid(), body=body, sender=f"+2677420000{i}")
                self.assertEqual(response.status_code, 200)
        self.assertNoAiActivity()

    def test_19_no_openai_client_configured_does_not_reserve(self):
        with patch("smartdesk.services.receptionist.openai_client", return_value=None):
            response = self.ai_post()
        self.assertIn(b"contact the business directly", response.data.lower())
        self.assertNoAiActivity()

    def test_a_missing_message_sid_does_not_reserve(self):
        response = self.client.post("/whatsapp", data={
            "From": f"whatsapp:{CUSTOMER}", "To": f"whatsapp:{PADEL_NUMBER}", "Body": AI_QUESTION})
        self.assertEqual(response.status_code, 400)
        self.assertNoAiActivity()

    def test_a_non_ai_reply_still_commits_in_one_transaction(self):
        with patch.object(db.session, "commit", wraps=db.session.commit) as commit:
            self.post(new_sid(), body="Dumela", sender="+26774200009")
        self.assertEqual(commit.call_count, 1)  # unchanged single-commit behaviour


# ---------------------------------------------------------------------------
# 24 and the transaction boundary
# ---------------------------------------------------------------------------


class NoOpenAIWithoutReservationTests(QuotaCase):
    def test_24_a_refused_reservation_means_zero_openai_calls(self):
        denied = usage_service.ReservationResult(False, "quota_exceeded", None, False)
        with patch.object(usage_service, "reserve_ai_reply", return_value=denied):
            response = self.ai_post()
        self.assertEqual(self.ai.count, 0)
        self.assertIn(LIMIT_TEXT, response.data.lower())

    def test_24_a_failing_reservation_system_fails_closed(self):
        with patch.object(usage_service, "reserve_ai_reply", side_effect=RuntimeError("db down")):
            sid = new_sid()
            response = self.ai_post(sid=sid)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.ai.count, 0)
        # Nothing was committed, so the idempotency claim was released ...
        self.assertEqual(Message.query.filter_by(provider_message_id=sid).count(), 0)
        # ... and a retry of the very same message is processed cleanly, once.
        retry = self.ai_post(sid=sid)
        self.assertIn(b"AI reply.", retry.data)
        self.assertEqual(self.ai.count, 1)
        self.assertEqual(self.statuses(), ["committed"])

    def test_24_only_a_reservation_this_request_created_authorises_openai(self):
        existing = SimpleNamespace(id="x", status="reserved")
        for result in (
            usage_service.ReservationResult(True, "duplicate", existing, created=False),
            usage_service.ReservationResult(False, "already_released", existing, created=False),
        ):
            with self.subTest(reason=result.reason):
                with patch.object(usage_service, "reserve_ai_reply", return_value=result):
                    response = self.ai_post()
                self.assertEqual(self.ai.count, 0)
                self.assertFalse(self.has_reply(response))

    def test_openai_is_called_with_no_database_transaction_open(self):
        self.ai_post()
        self.assertEqual(self.ai.transaction_open_at_call, [False])

    def test_the_reservation_is_durable_before_openai_runs(self):
        seen = {}
        original = self.ai.chat.completions.create

        def spying_create(**kwargs):
            db.session.rollback()  # anything uncommitted would vanish here
            seen["rows"] = [(r.status, r.idempotency_key) for r in UsageReservation.query.all()]
            seen["inbound"] = Message.query.filter_by(role="customer").count()
            return original(**kwargs)

        self.ai.chat.completions.create = spying_create
        sid = new_sid()
        self.ai_post(sid=sid)
        self.assertEqual(seen["rows"], [("reserved", f"whatsapp:{sid}")])
        self.assertGreaterEqual(seen["inbound"], 1)

    def test_the_tenant_binding_is_renewed_after_openai_not_before(self):
        events = []
        real_bind = webhooks.bind_rls_tenant
        original = self.ai.chat.completions.create

        def spying_create(**kwargs):
            events.append("openai")
            return original(**kwargs)

        def spying_bind(tenant_id):
            events.append("rebind")
            return real_bind(tenant_id)

        self.ai.chat.completions.create = spying_create
        with patch.object(webhooks, "bind_rls_tenant", spying_bind):
            self.ai_post()
        self.assertEqual(events, ["openai", "rebind"])

    def test_a_non_ai_message_never_rebinds(self):
        with patch.object(webhooks, "bind_rls_tenant") as bind:
            self.post(new_sid(), body="Dumela", sender="+26774300001")
        bind.assert_not_called()


# ---------------------------------------------------------------------------
# 7 (spec section): failures AFTER the reservation is durable
# ---------------------------------------------------------------------------


class FailureAfterReservationTests(QuotaCase):
    def test_a_failed_quota_commit_still_delivers_the_reply_and_never_retries(self):
        with patch.object(
            usage_service, "commit_reservation", side_effect=RuntimeError("db down at commit")
        ) as commit, patch.object(
            usage_service, "reserve_ai_reply", wraps=usage_service.reserve_ai_reply
        ) as reserve:
            response = self.ai_post()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AI reply.", response.data)       # the paid-for answer is delivered
        self.assertEqual(self.ai.count, 1)                # OpenAI is never re-called
        self.assertEqual(commit.call_count, 1)            # no automatic second commit
        self.assertEqual(reserve.call_count, 1)           # no second reservation
        # Not pretended to be committed: it stays 'reserved' for stale recovery.
        self.assertEqual(self.statuses(), ["reserved"])
        # The reply and its usage record survive, flagged.
        conv = self.conversation()
        self.assertEqual([m.body for m in self.rows(conv, "assistant")], ["AI reply."])
        (out,) = self.events("message_out")
        self.assertTrue(out.meta["quota_commit_failed"])
        self.assertEqual(out.meta["openai_usage"]["total_tokens"], 168)

    def test_a_reservation_left_by_a_failed_commit_is_recovered_by_the_stale_sweep(self):
        with patch.object(usage_service, "commit_reservation", side_effect=RuntimeError("down")):
            self.ai_post()
        self.assertEqual(self.used(), 1)
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        swept = usage_service.release_stale_reservations(now=later)
        db.session.commit()
        self.assertEqual(swept.released, 1)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.statuses(), ["released"])

    def test_a_logical_finalise_error_keeps_the_reply_and_records(self):
        with patch.object(
            usage_service, "commit_reservation",
            side_effect=usage_service.ReservationStateError("already released by a sweep"),
        ):
            response = self.ai_post()
        self.assertIn(b"AI reply.", response.data)
        (out,) = self.events("message_out")
        self.assertTrue(out.meta["quota_finalize_error"])
        self.assertEqual(len(self.rows(self.conversation(), "assistant")), 1)

    def test_an_unexpected_error_after_openai_finalises_once_and_answers_safely(self):
        calls = {"n": 0}
        real_bind = webhooks.bind_rls_tenant

        def failing_once(tenant_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection lost right after OpenAI")
            return real_bind(tenant_id)

        with patch.object(webhooks, "bind_rls_tenant", failing_once), patch.object(
            usage_service, "commit_reservation", wraps=usage_service.commit_reservation
        ) as commit:
            response = self.ai_post()
        self.assertEqual(response.status_code, 200)
        self.assertIn(webhooks.FALLBACK_TEXT.encode(), response.data)
        self.assertEqual(self.ai.count, 1)
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(self.statuses(), ["committed"])  # OpenAI had answered: usage is real
        self.assertEqual(len(self.reservations()), 1)

    def test_an_unexpected_error_after_a_failed_openai_call_releases(self):
        self.ai.fail = RuntimeError("openai down")
        calls = {"n": 0}
        real_bind = webhooks.bind_rls_tenant

        def failing_once(tenant_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("then the database went away")
            return real_bind(tenant_id)

        with patch.object(webhooks, "bind_rls_tenant", failing_once):
            response = self.ai_post()
        self.assertEqual(response.status_code, 200)
        self.assertIn(webhooks.FALLBACK_TEXT.encode(), response.data)
        self.assertEqual(self.statuses(), ["released"])
        self.assertEqual(self.used(), 0)

    def test_if_even_recovery_fails_the_reservation_is_left_for_the_sweep(self):
        self.ai.fail = RuntimeError("openai down")
        with patch.object(webhooks, "bind_rls_tenant", side_effect=RuntimeError("db gone")):
            response = self.ai_post()
        self.assertEqual(response.status_code, 200)
        self.assertIn(webhooks.FALLBACK_TEXT.encode(), response.data)
        self.assertEqual(self.statuses(), ["reserved"])
        self.assertEqual(self.ai.count, 1)

    def test_a_failure_before_the_reservation_is_still_a_clean_500(self):
        with patch.object(webhooks, "build_engine", side_effect=RuntimeError("bug")):
            sid = new_sid()
            response = self.ai_post(sid=sid)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(Message.query.filter_by(provider_message_id=sid).count(), 0)
        self.assertEqual(self.reservations(), [])


# ---------------------------------------------------------------------------
# Engine-level: the gate is the only way to the OpenAI request
# ---------------------------------------------------------------------------


class EngineGateTests(unittest.TestCase):
    def make_engine(self, client, **hooks):
        from ai.receptionist import ReceptionistEngine

        return ReceptionistEngine(
            client_provider=lambda: client,
            model="gpt-4o-mini",
            history_loader=lambda _sid: [],
            history_appender=lambda *_a: None,
            escalation_notifier=lambda *_a: None,
            knowledge_provider=lambda: "Open daily.",
            knowledge_dict_provider=lambda: {},
            **hooks,
        )

    def test_a_denying_gate_prevents_the_request_and_records_no_turn(self):
        from ai.receptionist import AIUsageDenied

        appended = []
        client = QuotaOpenAI()
        engine = self.make_engine(client, before_ai_call=lambda: "quota_exceeded")
        engine._history_appender = lambda *a: appended.append(a)
        with self.assertRaises(AIUsageDenied) as caught:
            engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp")
        self.assertEqual(caught.exception.reason, "quota_exceeded")
        self.assertEqual(client.count, 0)
        self.assertEqual(appended, [])
        self.assertIsNone(engine.ai_outcome)
        self.assertIsNone(engine.last_usage)

    def test_a_granting_gate_allows_one_request_then_the_after_hook_runs(self):
        order = []
        client = QuotaOpenAI()
        original = client.chat.completions.create

        def create(**kw):
            order.append("openai")
            return original(**kw)

        client.chat.completions.create = create
        engine = self.make_engine(
            client,
            before_ai_call=lambda: order.append("before") or "granted",
            after_ai_call=lambda: order.append("after"),
        )
        self.assertEqual(engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp"), "AI reply.")
        self.assertEqual(order, ["before", "openai", "after"])
        self.assertEqual(engine.ai_outcome, "succeeded")

    def test_the_after_hook_runs_even_when_openai_fails(self):
        order = []
        engine = self.make_engine(
            QuotaOpenAI(fail=RuntimeError("x")),
            before_ai_call=lambda: "granted",
            after_ai_call=lambda: order.append("after"),
        )
        engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp")
        self.assertEqual(order, ["after"])
        self.assertEqual(engine.ai_outcome, "failed")

    def test_without_hooks_the_engine_behaves_exactly_as_before(self):
        client = QuotaOpenAI()
        engine = self.make_engine(client)
        self.assertEqual(engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp"), "AI reply.")
        self.assertEqual(client.count, 1)
        self.assertEqual(engine.ai_outcome, "succeeded")

    def test_no_client_means_no_gate_call_and_no_outcome(self):
        gate_calls = []
        engine = self.make_engine(None, before_ai_call=lambda: gate_calls.append(1) or "granted")
        engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp")
        self.assertEqual(gate_calls, [])
        self.assertIsNone(engine.ai_outcome)

    def test_outcome_resets_between_replies(self):
        engine = self.make_engine(QuotaOpenAI())
        engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp")
        self.assertEqual(engine.ai_outcome, "succeeded")
        engine._client_provider = lambda: None
        engine.generate_openai_reply(AI_QUESTION, "s", "whatsapp")
        self.assertIsNone(engine.ai_outcome)


if __name__ == "__main__":
    unittest.main()
