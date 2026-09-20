"""Tests for the conversational booking layer.

Numbered comments map each test to the corresponding item in the feature
spec's 20-point test list. Every external call (Google Calendar) is mocked;
this environment cannot reach Google's API and shouldn't from a unit test
regardless -- what's under test is SmartDesk's own state machine, parsing,
and tenant isolation.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-value")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURE", "false")

from smartdesk.extensions import db  # noqa: E402
from smartdesk.models import (  # noqa: E402
    Booking,
    BookingConversationState,
    CalendarConnection,
    Conversation,
    Customer,
    ReceptionistProfile,
)
from smartdesk.services import booking_engine as be  # noqa: E402
from smartdesk.services import calendar as calendar_service  # noqa: E402
from smartdesk.services import nlu_datetime as ndt  # noqa: E402
from tests.test_multitenant import MultiTenantTestCase  # noqa: E402


def _available(*_args, **_kwargs):
    return True


def _unavailable(*_args, **_kwargs):
    return False


class BookingEngineTestCase(MultiTenantTestCase):
    """Common fixtures: padel tenant is Africa/Gaborone, calendar mode."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = ReceptionistProfile.query.filter_by(tenant_id=self.padel.id).one()
        self.profile.booking_mode = "calendar"
        self.profile.booking_assistance_enabled = True
        self.profile.default_booking_duration_minutes = 60
        db.session.add(
            CalendarConnection(
                tenant_id=self.padel.id, access_token="tok", status="connected"
            )
        )
        db.session.commit()

    def send(self, body: str) -> str | None:
        return be.handle_booking_message(
            self.padel, self.padel_conversation, "whatsapp", body
        )


# ---------------------------------------------------------------------------
# 1-3: complete info, missing time, missing date
# ---------------------------------------------------------------------------


class SlotCollectionTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_1_complete_information_goes_straight_to_confirmation(self, _mock):
        reply = self.send("I'd like to book Saturday at 6pm please")
        self.assertIn("Just to confirm", reply)
        self.assertIn("6:00 PM", reply)
        state = be.get_active_state(self.padel_conversation.id)
        self.assertEqual(state.status, "confirming")

    def test_2_missing_time_is_asked_for(self):
        reply = self.send("I want to book Saturday")
        self.assertIn("what time", reply.lower())
        state = be.get_active_state(self.padel_conversation.id)
        self.assertEqual(state.status, "collecting")
        self.assertIsNotNone(state.requested_date)
        self.assertIsNone(state.requested_time)

    def test_3_missing_date_is_asked_for(self):
        reply = self.send("I'd like to book at 6pm")
        self.assertIn("what date", reply.lower())
        state = be.get_active_state(self.padel_conversation.id)
        self.assertIsNone(state.requested_date)

    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_only_asks_one_question_at_a_time(self, _mock):
        """Not a robotic multi-field interrogation."""
        reply = self.send("I want to book")
        self.assertIn("date", reply.lower())
        self.assertNotIn("name", reply.lower())
        self.assertNotIn("phone", reply.lower())
        self.assertNotIn("duration", reply.lower())


# ---------------------------------------------------------------------------
# 4: natural-language date parsing (see also test_nlu_datetime for units)
# ---------------------------------------------------------------------------


class NaturalLanguageDateTests(BookingEngineTestCase):
    def test_4_various_phrasings_all_resolve_to_a_date(self):
        for phrase in ["today", "tomorrow", "Saturday", "this Saturday",
                       "next Saturday", "19 September", "6:30 on Friday"]:
            self.assertIsNotNone(
                ndt.extract_date(phrase, "Africa/Gaborone"),
                f"expected a date from {phrase!r}",
            )

    def test_ambiguous_relative_phrase_does_not_produce_a_date(self):
        self.assertIsNone(ndt.extract_date("sometime soonish", "Africa/Gaborone"))


# ---------------------------------------------------------------------------
# 5-7: availability success / conflict / alternatives
# ---------------------------------------------------------------------------


class AvailabilityTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_5_availability_success_proceeds_to_confirmation(self, mock_check):
        reply = self.send("Book Saturday at 6pm")
        mock_check.assert_called_once()
        self.assertIn("Just to confirm", reply)

    @patch("smartdesk.services.calendar.check_availability", side_effect=_unavailable)
    def test_6_availability_conflict_does_not_confirm(self, _mock):
        reply = self.send("Book Saturday at 6pm")
        self.assertNotIn("Just to confirm", reply)
        self.assertIn("isn't available", reply)
        state = be.get_active_state(self.padel_conversation.id)
        self.assertEqual(state.status, "collecting")  # never silently confirmed

    @patch("smartdesk.services.calendar.check_availability")
    def test_7_alternative_time_is_offered_and_actually_checked(self, mock_check):
        # Only the exact requested slot is busy; a +30min candidate is free.
        def side_effect(tenant, start, end):
            return start.time() != time(18, 0)

        mock_check.side_effect = side_effect
        reply = self.send("Book Saturday at 6pm")
        self.assertIn("6:30 PM", reply)
        self.assertGreater(mock_check.call_count, 1)  # alternatives were verified, not guessed


# ---------------------------------------------------------------------------
# 8: confirmation -> creation
# ---------------------------------------------------------------------------


class ConfirmationTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar._calendar_service")
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_8_confirming_creates_a_real_booking_and_calendar_event(
        self, _mock_avail, mock_service
    ):
        mock_service.return_value.events.return_value.insert.return_value.execute.return_value = {
            "id": "gcal-abc"
        }
        self.send("Book Saturday at 6pm")
        reply = self.send("yes")
        self.assertIn("booked for", reply)
        booking = Booking.query.filter_by(tenant_id=self.padel.id).one()
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(booking.external_reference, "gcal-abc")
        state = be.get_active_state(self.padel_conversation.id)
        self.assertIsNone(state)  # no longer active; it's terminal now


# ---------------------------------------------------------------------------
# 9: cancellation
# ---------------------------------------------------------------------------


class CancellationTests(BookingEngineTestCase):
    def _make_confirmed_booking(self):
        booking = Booking(
            tenant_id=self.padel.id, customer_id=self.padel_customer.id,
            conversation_id=self.padel_conversation.id, service="Court hire",
            starts_at=datetime(2026, 9, 26, 18, 0, tzinfo=ZoneInfo("Africa/Gaborone")).astimezone(timezone.utc),
            ends_at=datetime(2026, 9, 26, 19, 0, tzinfo=ZoneInfo("Africa/Gaborone")).astimezone(timezone.utc),
            status="confirmed", source="ai", external_system="google_calendar",
            external_reference="gcal-xyz",
        )
        db.session.add(booking)
        db.session.flush()
        state = BookingConversationState(
            tenant_id=self.padel.id, conversation_id=self.padel_conversation.id,
            customer_id=self.padel_customer.id, status="confirmed", booking_id=booking.id,
        )
        db.session.add(state)
        db.session.commit()
        return booking

    @patch("smartdesk.services.calendar._calendar_service")
    def test_9_cancelling_an_existing_booking(self, mock_service):
        booking = self._make_confirmed_booking()
        reply = self.send("I need to cancel my booking please")
        self.assertIn("cancelled", reply.lower())
        db.session.refresh(booking)
        self.assertEqual(booking.status, "cancelled")
        mock_service.return_value.events.return_value.delete.assert_called_once()

    def test_cancelling_with_no_existing_booking_is_handled_gracefully(self):
        reply = self.send("cancel my booking")
        self.assertIn("couldn't find", reply.lower())

    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_cancelling_an_in_progress_request_does_not_touch_calendar(self, _mock):
        self.send("Book Saturday at 6pm")  # now 'confirming', no Booking row yet
        with patch("smartdesk.services.calendar._calendar_service") as mock_service:
            reply = self.send("actually cancel that")
            mock_service.assert_not_called()
        self.assertIn("cancelled that request", reply.lower())
        self.assertEqual(Booking.query.filter_by(tenant_id=self.padel.id).count(), 0)


# ---------------------------------------------------------------------------
# 10: modification
# ---------------------------------------------------------------------------


class ModificationTests(BookingEngineTestCase):
    def _make_confirmed_booking(self):
        booking = Booking(
            tenant_id=self.padel.id, customer_id=self.padel_customer.id,
            conversation_id=self.padel_conversation.id, service="Court hire",
            starts_at=datetime(2026, 9, 26, 17, 0, tzinfo=ZoneInfo("Africa/Gaborone")).astimezone(timezone.utc),
            ends_at=datetime(2026, 9, 26, 18, 0, tzinfo=ZoneInfo("Africa/Gaborone")).astimezone(timezone.utc),
            status="confirmed", source="ai", external_system="google_calendar",
            external_reference="gcal-old",
        )
        db.session.add(booking)
        db.session.flush()
        state = BookingConversationState(
            tenant_id=self.padel.id, conversation_id=self.padel_conversation.id,
            customer_id=self.padel_customer.id, status="confirmed", booking_id=booking.id,
        )
        db.session.add(state)
        db.session.commit()
        return booking

    @patch("smartdesk.services.calendar._calendar_service")
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_10_moving_a_booking_to_a_new_time(self, _mock_avail, mock_service):
        mock_service.return_value.events.return_value.insert.return_value.execute.return_value = {
            "id": "gcal-new"
        }
        booking = self._make_confirmed_booking()
        reply = self.send("Can I move my booking to 7pm instead?")
        self.assertIn("Just to confirm", reply)
        reply = self.send("yes")
        self.assertIn("moved to", reply.lower())

        db.session.refresh(booking)
        self.assertEqual(
            be.ensure_aware_utc(booking.starts_at).astimezone(ZoneInfo("Africa/Gaborone")).hour,
            19,
        )
        self.assertEqual(booking.external_reference, "gcal-new")
        # Old event was cancelled, not left dangling.
        mock_service.return_value.events.return_value.delete.assert_called_once()
        # Still exactly one Booking row -- a reschedule, not a duplicate.
        self.assertEqual(Booking.query.filter_by(tenant_id=self.padel.id).count(), 1)


# ---------------------------------------------------------------------------
# 11: external booking mode
# ---------------------------------------------------------------------------


class ExternalBookingModeTests(BookingEngineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile.booking_mode = "external"
        self.profile.external_booking_url = "https://playbypoint.com/10by20"
        db.session.commit()

    def test_11_external_mode_returns_the_configured_url(self):
        with patch("smartdesk.services.calendar._calendar_service") as mock_service:
            reply = self.send("I'd like to book a court for Saturday at 6pm")
            mock_service.assert_not_called()
        self.assertIn("https://playbypoint.com/10by20", reply)
        self.assertEqual(Booking.query.filter_by(tenant_id=self.padel.id).count(), 0)
        self.assertIsNone(be.get_active_state(self.padel_conversation.id))

    def test_external_mode_with_no_url_configured_declines_gracefully(self):
        self.profile.external_booking_url = None
        db.session.commit()
        reply = self.send("I'd like to book")
        self.assertNotIn("http", reply)
        self.assertIn("contact us directly", reply.lower())

    def test_external_mode_ignores_non_booking_messages(self):
        reply = self.send("What are your opening hours?")
        self.assertIsNone(reply)  # falls through to the normal knowledge/AI path

    def test_url_comes_from_tenant_configuration_not_hardcoded(self):
        """Different tenants get their OWN configured URL, never a shared one."""
        salon_profile = ReceptionistProfile.query.filter_by(tenant_id=self.salon.id).one()
        salon_profile.booking_mode = "external"
        salon_profile.external_booking_url = "https://booksy.com/some-salon"
        db.session.commit()

        salon_reply = be.handle_booking_message(
            self.salon, self.salon_conversation, "whatsapp", "I'd like to book an appointment"
        )
        self.assertIn("https://booksy.com/some-salon", salon_reply)
        self.assertNotIn("playbypoint", salon_reply.lower())


# ---------------------------------------------------------------------------
# 12: tenant isolation
# ---------------------------------------------------------------------------


class BookingTenantIsolationTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_12_booking_states_do_not_leak_across_tenants(self, _mock):
        self.send("Book Saturday at 6pm")  # padel conversation
        salon_state = be.get_active_state(self.salon_conversation.id)
        self.assertIsNone(salon_state)  # salon's conversation is untouched
        padel_state = be.get_active_state(self.padel_conversation.id)
        self.assertIsNotNone(padel_state)
        self.assertEqual(padel_state.tenant_id, self.padel.id)

    @patch("smartdesk.services.calendar._calendar_service")
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_bookings_created_are_tenant_scoped(self, _mock_avail, mock_service):
        mock_service.return_value.events.return_value.insert.return_value.execute.return_value = {
            "id": "gcal-1"
        }
        self.send("Book Saturday at 6pm")
        self.send("yes")
        self.assertEqual(Booking.query.filter_by(tenant_id=self.padel.id).count(), 1)
        self.assertEqual(Booking.query.filter_by(tenant_id=self.salon.id).count(), 0)

    def test_cancel_cannot_reach_another_tenants_booking(self):
        """Same customer phone number, different tenant -- must not cross."""
        other_booking = Booking(
            tenant_id=self.salon.id, customer_id=self.salon_customer.id,
            starts_at=datetime(2026, 9, 26, 10, 0, tzinfo=ZoneInfo("Africa/Gaborone")).astimezone(timezone.utc),
            ends_at=datetime(2026, 9, 26, 11, 0, tzinfo=ZoneInfo("Africa/Gaborone")).astimezone(timezone.utc),
            status="confirmed", source="ai",
        )
        db.session.add(other_booking)
        db.session.flush()
        db.session.add(BookingConversationState(
            tenant_id=self.salon.id, conversation_id=self.salon_conversation.id,
            customer_id=self.salon_customer.id, status="confirmed",
            booking_id=other_booking.id,
        ))
        db.session.commit()

        reply = self.send("cancel my booking")  # sent on the PADEL conversation
        self.assertIn("couldn't find", reply.lower())
        db.session.refresh(other_booking)
        self.assertEqual(other_booking.status, "confirmed")  # untouched


# ---------------------------------------------------------------------------
# 13 & 18: duplicate prevention / idempotency
# ---------------------------------------------------------------------------


class IdempotencyTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar._calendar_service")
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_13_repeated_confirmation_does_not_double_book(self, _mock_avail, mock_service):
        mock_service.return_value.events.return_value.insert.return_value.execute.return_value = {
            "id": "gcal-once"
        }
        self.send("Book Saturday at 6pm")
        first = self.send("yes")
        # A retried webhook delivering the same "yes" again.
        second = be.handle_booking_message(
            self.padel, self.padel_conversation, "whatsapp", "yes"
        )
        self.assertEqual(Booking.query.filter_by(tenant_id=self.padel.id).count(), 1)
        self.assertEqual(
            mock_service.return_value.events.return_value.insert.call_count, 1
        )
        self.assertIn("booked for", first.lower())
        # Second call finds no active state (already terminal) and falls
        # through -- the safety property is no duplicate booking, not a
        # specific reply to the stray message.
        self.assertIsNone(second)

    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_18_identical_message_sent_twice_while_collecting_is_stable(self, _mock):
        first = self.send("Book Saturday at 6pm")
        second = self.send("Book Saturday at 6pm")
        # Same resulting state either way -- re-processing doesn't diverge.
        self.assertEqual(first, second)
        self.assertEqual(
            BookingConversationState.query.filter_by(
                conversation_id=self.padel_conversation.id
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# 14: Google Calendar failure
# ---------------------------------------------------------------------------


class CalendarFailureTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar._calendar_service")
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_14_calendar_failure_never_produces_a_false_confirmation(
        self, _mock_avail, mock_service
    ):
        mock_service.return_value.events.return_value.insert.return_value.execute.side_effect = (
            Exception("calendar API down")
        )
        self.send("Book Saturday at 6pm")
        reply = self.send("yes")
        self.assertNotIn("booked for", reply.lower())
        self.assertNotIn("you're booked", reply.lower())

        booking = Booking.query.filter_by(tenant_id=self.padel.id).one()
        self.assertEqual(booking.status, "pending")  # never silently 'confirmed'

        state = be.get_active_state(self.padel_conversation.id)
        self.assertIsNotNone(state)  # preserved, so a retry is possible
        self.assertEqual(state.status, "confirming")

    @patch("smartdesk.services.calendar.check_availability")
    def test_availability_check_failure_is_reported_honestly(self, mock_check):
        mock_check.side_effect = calendar_service.CalendarError("no calendar connected")
        reply = self.send("Book Saturday at 6pm")
        self.assertIn("trouble checking availability", reply.lower())
        # 'failed' is terminal, so get_active_state() correctly returns None
        # here -- query the row directly to inspect the terminal state.
        state = BookingConversationState.query.filter_by(
            conversation_id=self.padel_conversation.id
        ).one()
        self.assertEqual(state.status, "failed")


# ---------------------------------------------------------------------------
# 15: invalid / ambiguous date
# ---------------------------------------------------------------------------


class AmbiguousInputTests(BookingEngineTestCase):
    def test_15_nonsense_date_is_not_guessed(self):
        reply = self.send("I want to book on blursday at some point")
        self.assertIn("what date", reply.lower())
        state = be.get_active_state(self.padel_conversation.id)
        self.assertIsNone(state.requested_date)

    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_vague_period_asks_for_a_specific_time_rather_than_guessing(self, _mock):
        self.send("I'd like to book a court")  # starts the flow, asks for a date
        reply = self.send("tomorrow evening")
        self.assertIn("evening", reply.lower())
        self.assertIn("what time", reply.lower())
        state = be.get_active_state(self.padel_conversation.id)
        self.assertIsNone(state.requested_time)  # not silently defaulted
        self.assertIsNotNone(state.requested_date)  # date WAS captured


# ---------------------------------------------------------------------------
# 16: timezone handling
# ---------------------------------------------------------------------------


class TimezoneTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar.check_availability")
    def test_16_availability_is_checked_in_the_tenants_timezone_not_utc(self, mock_check):
        captured = {}

        def capture(tenant, start, end):
            captured["start"] = start
            return True

        mock_check.side_effect = capture
        self.send("Book Saturday at 6pm")
        start = captured["start"]
        self.assertEqual(start.tzinfo.key if hasattr(start.tzinfo, "key") else str(start.tzinfo),
                         "Africa/Gaborone")
        self.assertEqual(start.hour, 18)  # 6pm LOCAL, not shifted to UTC first
        self.assertEqual(start.utcoffset(), timedelta(hours=2))

    def test_a_different_tenant_timezone_is_respected(self):
        self.padel.timezone = "America/New_York"
        db.session.commit()
        with patch("smartdesk.services.calendar.check_availability") as mock_check:
            captured = {}
            mock_check.side_effect = lambda t, s, e: captured.setdefault("start", s) or True
            self.send("Book Saturday at 6pm")
        self.assertEqual(
            captured["start"].tzinfo.key
            if hasattr(captured["start"].tzinfo, "key") else str(captured["start"].tzinfo),
            "America/New_York",
        )


# ---------------------------------------------------------------------------
# 17: conversation state persistence across turns
# ---------------------------------------------------------------------------


class StatePersistenceTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_17_date_and_time_given_in_separate_turns_are_combined(self, _mock):
        first = self.send("I want to book Saturday")
        self.assertIn("what time", first.lower())

        # Simulate a genuinely separate request/webhook call, not shared
        # in-memory state -- re-fetch from the database.
        second = self.send("6pm")
        self.assertIn("Just to confirm", second)
        self.assertIn("6:00 PM", second)

        state = BookingConversationState.query.filter_by(
            conversation_id=self.padel_conversation.id
        ).one()
        # Computed the same way the app resolves "Saturday", rather than a
        # hardcoded literal -- this test asserts persistence across turns,
        # not a fixed calendar date, so it shouldn't be brittle to whatever
        # day it happens to run on.
        expected_date = ndt.extract_date("Saturday", "Africa/Gaborone")
        self.assertEqual(state.requested_date, expected_date)
        self.assertEqual(state.requested_time, time(18, 0))


# ---------------------------------------------------------------------------
# 19: customer information persistence
# ---------------------------------------------------------------------------


class CustomerInfoTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_19_customer_name_given_mid_flow_is_captured(self, _mock):
        self.send("I want to book Saturday at 6pm, it's Thabo Molefe")
        state = be.get_active_state(self.padel_conversation.id)
        self.assertEqual(state.customer_name, "Thabo Molefe")

    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_party_size_is_captured_when_given(self, _mock):
        reply = self.send("Book me for two people at 6pm on Saturday")
        state = be.get_active_state(self.padel_conversation.id)
        self.assertEqual(state.party_size, 2)
        self.assertIn("2", reply)

    def test_customer_phone_is_available_via_the_linked_customer(self):
        self.assertEqual(self.padel_customer.phone, "+26771111111")
        state = be._create_state(self.padel, self.padel_conversation)
        self.assertEqual(state.customer_id, self.padel_customer.id)


# ---------------------------------------------------------------------------
# 20: WhatsApp integration (real webhook, not calling the engine directly)
# ---------------------------------------------------------------------------


class WhatsAppIntegrationTests(BookingEngineTestCase):
    @patch("smartdesk.services.calendar._calendar_service")
    @patch("smartdesk.services.calendar.check_availability", side_effect=_available)
    def test_20_booking_flows_end_to_end_through_the_real_webhook(
        self, _mock_avail, mock_service
    ):
        mock_service.return_value.events.return_value.insert.return_value.execute.return_value = {
            "id": "gcal-e2e"
        }
        r1 = self.client.post(
            "/whatsapp",
            data={"From": "whatsapp:+26779990001", "To": "whatsapp:+26770000001",
                  "Body": "Hi, can I book Saturday at 6pm?"},
        )
        self.assertEqual(r1.status_code, 200)
        self.assertIn(b"Just to confirm", r1.data)

        r2 = self.client.post(
            "/whatsapp",
            data={"From": "whatsapp:+26779990001", "To": "whatsapp:+26770000001",
                  "Body": "yes"},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertIn(b"booked for", r2.data)

        booking = Booking.query.filter_by(
            tenant_id=self.padel.id, external_reference="gcal-e2e"
        ).one()
        self.assertEqual(booking.status, "confirmed")

    def test_non_booking_whatsapp_messages_are_unaffected(self):
        """A plain question must not be swallowed by the booking engine."""
        r = self.client.post(
            "/whatsapp",
            data={"From": "whatsapp:+26779990002", "To": "whatsapp:+26770000001",
                  "Body": "What are your opening hours?"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            BookingConversationState.query.filter_by(tenant_id=self.padel.id).count(), 0
        )


# ---------------------------------------------------------------------------
# Booking assistance can be turned off entirely per tenant
# ---------------------------------------------------------------------------


class BookingAssistanceToggleTests(BookingEngineTestCase):
    def test_disabled_booking_assistance_never_engages(self):
        self.profile.booking_assistance_enabled = False
        db.session.commit()
        reply = self.send("Book me for Saturday at 6pm")
        self.assertIsNone(reply)
        self.assertIsNone(be.get_active_state(self.padel_conversation.id))


if __name__ == "__main__":
    unittest.main()
