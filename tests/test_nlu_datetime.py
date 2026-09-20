"""Unit tests for smartdesk.services.nlu_datetime -- pure functions, no DB."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from smartdesk.services import nlu_datetime as ndt

_REF = datetime(2026, 9, 19, 10, 0, tzinfo=ZoneInfo("Africa/Gaborone"))  # a Saturday


class DateParsingTests(unittest.TestCase):
    def test_today(self):
        self.assertEqual(ndt.extract_date("today", "Africa/Gaborone", _REF), date(2026, 9, 19))

    def test_tomorrow(self):
        self.assertEqual(ndt.extract_date("tomorrow", "Africa/Gaborone", _REF), date(2026, 9, 20))

    def test_bare_weekday_same_as_today_returns_today(self):
        self.assertEqual(ndt.extract_date("saturday", "Africa/Gaborone", _REF), date(2026, 9, 19))

    def test_this_weekday(self):
        self.assertEqual(ndt.extract_date("this saturday", "Africa/Gaborone", _REF), date(2026, 9, 19))

    def test_next_weekday_is_a_full_week_later_than_the_nearest_one(self):
        self.assertEqual(ndt.extract_date("next saturday", "Africa/Gaborone", _REF), date(2026, 9, 26))

    def test_upcoming_weekday_before_today(self):
        # Wednesday from a Saturday reference -> the coming Wednesday, 4 days out.
        self.assertEqual(ndt.extract_date("wednesday", "Africa/Gaborone", _REF), date(2026, 9, 23))

    def test_explicit_day_month(self):
        self.assertEqual(ndt.extract_date("19 September", "Africa/Gaborone", _REF), date(2026, 9, 19))

    def test_explicit_month_day(self):
        self.assertEqual(ndt.extract_date("September 19", "Africa/Gaborone", _REF), date(2026, 9, 19))

    def test_explicit_date_already_passed_this_year_rolls_to_next_year(self):
        self.assertEqual(ndt.extract_date("3 January", "Africa/Gaborone", _REF), date(2027, 1, 3))

    def test_nonsense_returns_none(self):
        self.assertIsNone(ndt.extract_date("sometime nice", "Africa/Gaborone", _REF))

    def test_invalid_calendar_date_returns_none(self):
        self.assertIsNone(ndt.extract_date("31 February", "Africa/Gaborone", _REF))


class TimeParsingTests(unittest.TestCase):
    def test_explicit_pm(self):
        self.assertEqual(ndt.extract_time("6pm"), time(18, 0))

    def test_explicit_am(self):
        self.assertEqual(ndt.extract_time("9am"), time(9, 0))

    def test_with_minutes(self):
        self.assertEqual(ndt.extract_time("6:30pm"), time(18, 30))

    def test_24_hour_clock(self):
        self.assertEqual(ndt.extract_time("18:00"), time(18, 0))

    def test_ambiguous_low_hour_defaults_pm(self):
        self.assertEqual(ndt.extract_time("6"), time(18, 0))

    def test_ambiguous_higher_hour_defaults_am(self):
        self.assertEqual(ndt.extract_time("9"), time(9, 0))

    def test_around_a_number(self):
        self.assertEqual(ndt.extract_time("around 6"), time(18, 0))

    def test_noon(self):
        self.assertEqual(ndt.extract_time("noon"), time(12, 0))

    def test_midnight(self):
        self.assertEqual(ndt.extract_time("midnight"), time(0, 0))

    def test_vague_period_is_not_a_time(self):
        self.assertIsNone(ndt.extract_time("evening"))

    def test_nonsense_is_not_a_time(self):
        self.assertIsNone(ndt.extract_time("whenever works"))


class VaguePeriodTests(unittest.TestCase):
    def test_evening(self):
        self.assertEqual(ndt.extract_vague_period("tomorrow evening"), "evening")

    def test_tonight_maps_to_evening(self):
        self.assertEqual(ndt.extract_vague_period("can I come tonight"), "evening")

    def test_morning(self):
        self.assertEqual(ndt.extract_vague_period("some time in the morning"), "morning")

    def test_none_when_absent(self):
        self.assertIsNone(ndt.extract_vague_period("6pm"))


class DurationPartySizeTests(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(ndt.extract_duration_minutes("2 hours"), 120)

    def test_half_hour_phrase(self):
        self.assertEqual(ndt.extract_duration_minutes("half an hour"), 30)

    def test_minutes(self):
        self.assertEqual(ndt.extract_duration_minutes("90 minutes"), 90)

    def test_none_when_absent(self):
        self.assertIsNone(ndt.extract_duration_minutes("book saturday at 6"))

    def test_party_size_numeric(self):
        self.assertEqual(ndt.extract_party_size("for 4 people"), 4)

    def test_party_size_word(self):
        self.assertEqual(ndt.extract_party_size("for two people"), 2)

    def test_party_of_phrase(self):
        self.assertEqual(ndt.extract_party_size("party of 6"), 6)


class NameExtractionTests(unittest.TestCase):
    def test_my_name_is(self):
        self.assertEqual(ndt.extract_customer_name("my name is Thabo Molefe"), "Thabo Molefe")

    def test_its(self):
        self.assertEqual(ndt.extract_customer_name("it's John"), "John")

    def test_false_positive_guard(self):
        self.assertIsNone(ndt.extract_customer_name("I'm not sure yet"))

    def test_no_match_returns_none(self):
        self.assertIsNone(ndt.extract_customer_name("book saturday at 6pm"))


class AffirmativeNegativeTests(unittest.TestCase):
    def test_yes_variants(self):
        for phrase in ["yes", "yes please", "sounds good", "go ahead", "confirm"]:
            self.assertTrue(ndt.is_affirmative(phrase), phrase)

    def test_no_is_not_affirmative(self):
        self.assertFalse(ndt.is_affirmative("no thanks"))

    def test_change_request_is_negative(self):
        self.assertTrue(ndt.is_negative_or_change("actually, change it"))


class ServiceExtractionTests(unittest.TestCase):
    def test_matches_a_known_service(self):
        self.assertEqual(
            ndt.extract_service("I'd like the deluxe wash please", ["Deluxe Wash", "Basic Wash"]),
            "Deluxe Wash",
        )

    def test_never_invents_a_service_not_in_the_list(self):
        self.assertIsNone(
            ndt.extract_service("I'd like a haircut", ["Deluxe Wash", "Basic Wash"])
        )

    def test_empty_known_services_returns_none(self):
        self.assertIsNone(ndt.extract_service("anything", []))


if __name__ == "__main__":
    unittest.main()
