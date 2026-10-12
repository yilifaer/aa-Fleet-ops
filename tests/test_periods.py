"""Tests for the EVE time (UTC) period and formatting helpers."""

import dataclasses
import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.db.models.functions import TruncDate
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone, translation

from fleetops.models import FleetOperation
from fleetops.services import periods
from fleetops.services.periods import (
    DISCORD_TIMESTAMP_STYLES,
    EVE_CLOCK_PATTERN,
    current_year_month,
    discord_timestamp,
    eve_now,
    format_eve,
    month_bounds,
    month_ref,
    parse_eve_clock,
    parse_eve_date,
    parse_eve_datetime,
    shift_month,
    to_utc,
    unix_timestamp,
    utc_day,
    year_bounds,
    year_month_of,
)

from . import factories as f

UTC = dt_timezone.utc

# Zones on both sides of UTC, with and without daylight saving time.
SITE_ZONES = ("Europe/Berlin", "Australia/Adelaide", "America/Los_Angeles", "Asia/Shanghai", "Pacific/Kiritimati")

# 2026-10-12 19:00 UTC
SAMPLE = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)
SAMPLE_UNIX = 1791831600


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def frozen_now(value):
    return mock.patch.object(periods.timezone, "now", return_value=value)


def local_zones():
    """One context per site ``TIME_ZONE`` and one per active time zone."""
    for name in SITE_ZONES:
        yield f"TIME_ZONE={name}", override_settings(TIME_ZONE=name)
        yield f"active zone {name}", timezone.override(ZoneInfo(name))


class MonthBoundsTests(SimpleTestCase):
    def test_regular_month(self):
        self.assertEqual(month_bounds(2026, 4), (utc(2026, 4, 1), utc(2026, 5, 1)))

    def test_january(self):
        self.assertEqual(month_bounds(2027, 1), (utc(2027, 1, 1), utc(2027, 2, 1)))

    def test_december_rolls_over_to_january_of_the_next_year(self):
        self.assertEqual(month_bounds(2026, 12), (utc(2026, 12, 1), utc(2027, 1, 1)))

    def test_february_in_a_leap_year(self):
        start, end = month_bounds(2028, 2)
        self.assertEqual((start, end), (utc(2028, 2, 1), utc(2028, 3, 1)))
        self.assertEqual(end - start, timedelta(days=29))

    def test_february_in_a_common_year(self):
        start, end = month_bounds(2026, 2)
        self.assertEqual(end - start, timedelta(days=28))

    def test_bounds_are_aware_utc(self):
        for value in month_bounds(2026, 10):
            self.assertIs(value.tzinfo, UTC)
            self.assertEqual(value.utcoffset(), timedelta(0))

    def test_consecutive_months_share_their_edge(self):
        for month in range(1, 13):
            with self.subTest(month=month):
                _, end = month_bounds(2026, month)
                next_start, _ = month_bounds(*shift_month(2026, month, 1))
                self.assertEqual(end, next_start)

    def test_invalid_month_is_rejected(self):
        for month in (0, 13, -1):
            with self.subTest(month=month), self.assertRaises(ValueError):
                month_bounds(2026, month)

    def test_bounds_ignore_site_and_active_time_zone(self):
        for label, zone in local_zones():
            with self.subTest(label), zone:
                self.assertEqual(month_bounds(2026, 4), (utc(2026, 4, 1), utc(2026, 5, 1)))
                self.assertEqual(month_bounds(2026, 12), (utc(2026, 12, 1), utc(2027, 1, 1)))
                # Daylight saving changes inside the month do not move the edges.
                self.assertEqual(month_bounds(2026, 10)[1] - month_bounds(2026, 10)[0], timedelta(days=31))


class YearBoundsTests(SimpleTestCase):
    def test_year(self):
        self.assertEqual(year_bounds(2026), (utc(2026, 1, 1), utc(2027, 1, 1)))

    def test_leap_year_length(self):
        start, end = year_bounds(2028)
        self.assertEqual(end - start, timedelta(days=366))

    def test_year_matches_first_and_last_month(self):
        self.assertEqual(year_bounds(2026)[0], month_bounds(2026, 1)[0])
        self.assertEqual(year_bounds(2026)[1], month_bounds(2026, 12)[1])

    def test_year_ignores_site_and_active_time_zone(self):
        for label, zone in local_zones():
            with self.subTest(label), zone:
                self.assertEqual(year_bounds(2026), (utc(2026, 1, 1), utc(2027, 1, 1)))


class ShiftMonthTests(SimpleTestCase):
    def test_shifts(self):
        cases = [
            ((2026, 10, 0), (2026, 10)),
            ((2026, 10, 1), (2026, 11)),
            ((2026, 12, 1), (2027, 1)),
            ((2026, 1, -1), (2025, 12)),
            ((2026, 10, 12), (2027, 10)),
            ((2026, 10, -12), (2025, 10)),
            ((2026, 3, -27), (2023, 12)),
            ((2026, 11, 14), (2028, 1)),
        ]
        for (year, month, delta), expected in cases:
            with self.subTest(year=year, month=month, delta=delta):
                self.assertEqual(shift_month(year, month, delta), expected)

    def test_invalid_month_is_rejected(self):
        with self.assertRaises(ValueError):
            shift_month(2026, 13, -1)


class ToUtcTests(SimpleTestCase):
    def test_aware_value_is_converted(self):
        berlin = datetime(2026, 10, 12, 21, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        result = to_utc(berlin)
        self.assertEqual(result, SAMPLE)
        self.assertIs(result.tzinfo, UTC)
        self.assertEqual(result.hour, 19)

    def test_naive_value_is_read_as_eve_time(self):
        for label, zone in local_zones():
            with self.subTest(label), zone:
                self.assertEqual(to_utc(datetime(2026, 10, 12, 19, 0)), SAMPLE)

    def test_none(self):
        self.assertIsNone(to_utc(None))

    def test_eve_now_is_utc(self):
        local = datetime(2026, 10, 13, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with frozen_now(local):
            result = eve_now()
        self.assertEqual(result, SAMPLE)
        self.assertIs(result.tzinfo, UTC)


class CurrentYearMonthTests(SimpleTestCase):
    def test_last_second_of_the_month_with_a_site_zone_ahead_of_utc(self):
        last_second = utc(2026, 10, 31, 23, 59, 59)
        # Brisbane is UTC+10 without daylight saving: locally it is already November.
        with override_settings(TIME_ZONE="Australia/Brisbane"), timezone.override(ZoneInfo("Australia/Brisbane")):
            self.assertEqual(timezone.localtime(last_second).month, 11)
            self.assertEqual(current_year_month(last_second), (2026, 10))
            with frozen_now(last_second):
                self.assertEqual(current_year_month(), (2026, 10))

    def test_first_second_of_the_month_with_a_site_zone_behind_utc(self):
        first_second = utc(2026, 11, 1, 0, 0, 0)
        with override_settings(TIME_ZONE="America/Los_Angeles"):
            self.assertEqual(timezone.localtime(first_second).month, 10)
            with frozen_now(first_second):
                self.assertEqual(current_year_month(), (2026, 11))

    def test_new_year(self):
        for label, zone in local_zones():
            with self.subTest(label), zone:
                self.assertEqual(current_year_month(utc(2026, 12, 31, 23, 59, 59)), (2026, 12))
                self.assertEqual(current_year_month(utc(2027, 1, 1)), (2027, 1))

    def test_aware_local_value_counts_in_its_utc_month(self):
        # 00:30 on 1 November in Berlin is still 31 October in EVE time.
        berlin = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        self.assertEqual(current_year_month(berlin), (2026, 10))
        self.assertEqual(year_month_of(berlin), (2026, 10))

    def test_naive_value_is_read_as_eve_time(self):
        with override_settings(TIME_ZONE="Asia/Shanghai"):
            self.assertEqual(current_year_month(datetime(2026, 10, 31, 23, 0)), (2026, 10))

    def test_defaults_to_now(self):
        with frozen_now(SAMPLE):
            self.assertEqual(current_year_month(), (2026, 10))


class MonthRefTests(SimpleTestCase):
    NOW = utc(2026, 10, 12, 8, 0)

    def test_current_month(self):
        ref = month_ref(2026, 10, now=self.NOW)
        self.assertEqual((ref.year, ref.month), (2026, 10))
        self.assertEqual((ref.start, ref.end), month_bounds(2026, 10))
        self.assertTrue(ref.is_current)
        self.assertFalse(ref.is_finished)
        self.assertFalse(ref.is_future)
        self.assertEqual(ref.days_in_month, 31)
        self.assertEqual(ref.days_elapsed, 12)
        self.assertEqual(ref.label, "2026-10")

    def test_finished_month(self):
        ref = month_ref(2026, 9, now=self.NOW)
        self.assertFalse(ref.is_current)
        self.assertTrue(ref.is_finished)
        self.assertFalse(ref.is_future)
        self.assertEqual(ref.days_in_month, 30)
        self.assertEqual(ref.days_elapsed, 30)

    def test_future_month(self):
        ref = month_ref(2027, 1, now=self.NOW)
        self.assertFalse(ref.is_current)
        self.assertFalse(ref.is_finished)
        self.assertTrue(ref.is_future)
        self.assertEqual(ref.days_elapsed, 0)
        self.assertEqual(ref.label, "2027-01")

    def test_first_instant_counts_as_day_one(self):
        ref = month_ref(2026, 10, now=utc(2026, 10, 1))
        self.assertTrue(ref.is_current)
        self.assertEqual(ref.days_elapsed, 1)

    def test_last_instant_is_still_current(self):
        ref = month_ref(2026, 10, now=utc(2026, 10, 31, 23, 59, 59, 999999))
        self.assertTrue(ref.is_current)
        self.assertEqual(ref.days_elapsed, 31)

    def test_end_is_exclusive(self):
        ref = month_ref(2026, 10, now=utc(2026, 11, 1))
        self.assertTrue(ref.is_finished)
        self.assertFalse(ref.is_current)

    def test_leap_february(self):
        ref = month_ref(2028, 2, now=utc(2028, 2, 29, 12))
        self.assertEqual(ref.days_in_month, 29)
        self.assertEqual(ref.days_elapsed, 29)

    def test_month_edge_follows_utc_not_the_local_zone(self):
        # 23:30 UTC on 30 September is already 1 October in Brisbane.
        now = utc(2026, 9, 30, 23, 30)
        for label, zone in local_zones():
            with self.subTest(label), zone:
                self.assertTrue(month_ref(2026, 9, now=now).is_current)
                self.assertEqual(month_ref(2026, 9, now=now).days_elapsed, 30)
                self.assertTrue(month_ref(2026, 10, now=now).is_future)
        with override_settings(TIME_ZONE="Australia/Brisbane"), timezone.override(ZoneInfo("Australia/Brisbane")):
            self.assertTrue(month_ref(2026, 10, now=now).is_future)

    def test_defaults_to_now(self):
        with frozen_now(self.NOW):
            ref = month_ref(2026, 10)
        self.assertTrue(ref.is_current)
        self.assertEqual(ref.days_elapsed, 12)

    def test_is_immutable(self):
        ref = month_ref(2026, 10, now=self.NOW)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ref.month = 11

    def test_invalid_month_is_rejected(self):
        with self.assertRaises(ValueError):
            month_ref(2026, 13, now=self.NOW)


class FormatEveTests(SimpleTestCase):
    def test_default_format(self):
        self.assertEqual(format_eve(SAMPLE), "2026-10-12 19:00")

    def test_custom_format(self):
        self.assertEqual(format_eve(SAMPLE, "%H:%M"), "19:00")
        self.assertEqual(format_eve(SAMPLE, "%Y-%m-%d %H:%M EVE"), "2026-10-12 19:00 EVE")

    def test_aware_local_value_is_shown_in_utc(self):
        shanghai = datetime(2026, 10, 13, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(format_eve(shanghai), "2026-10-12 19:00")

    def test_naive_value_is_read_as_eve_time(self):
        self.assertEqual(format_eve(datetime(2026, 10, 12, 19, 0)), "2026-10-12 19:00")

    def test_empty_values(self):
        self.assertEqual(format_eve(None), "")
        self.assertEqual(format_eve(""), "")

    def test_rejects_other_types(self):
        for value in (date(2026, 10, 12), "2026-10-12 19:00", 1791831600):
            with self.subTest(value=value), self.assertRaises(TypeError):
                format_eve(value)

    def test_ignores_site_zone_active_zone_and_language(self):
        for label, zone in local_zones():
            with self.subTest(label), zone, translation.override("zh-hans"):
                self.assertEqual(format_eve(SAMPLE), "2026-10-12 19:00")
                self.assertEqual(format_eve(utc(2026, 12, 31, 23, 30)), "2026-12-31 23:30")


class UnixTimestampTests(SimpleTestCase):
    def test_utc_value(self):
        self.assertEqual(unix_timestamp(SAMPLE), SAMPLE_UNIX)

    def test_same_instant_in_another_zone(self):
        adelaide = SAMPLE.astimezone(ZoneInfo("Australia/Adelaide"))
        self.assertEqual(unix_timestamp(adelaide), SAMPLE_UNIX)

    def test_naive_value_is_read_as_eve_time(self):
        # Overriding TIME_ZONE also changes the process time zone, which a naive
        # datetime.timestamp() would silently use.
        for label, zone in local_zones():
            with self.subTest(label), zone:
                self.assertEqual(unix_timestamp(datetime(2026, 10, 12, 19, 0)), SAMPLE_UNIX)

    def test_fractions_are_rounded_down(self):
        self.assertEqual(unix_timestamp(SAMPLE + timedelta(microseconds=999999)), SAMPLE_UNIX)
        self.assertEqual(unix_timestamp(utc(1969, 12, 31, 23, 59, 59, 500000)), -1)
        self.assertEqual(unix_timestamp(utc(1970, 1, 1)), 0)

    def test_returns_int(self):
        self.assertIs(type(unix_timestamp(SAMPLE)), int)

    def test_none(self):
        self.assertIsNone(unix_timestamp(None))


class DiscordTimestampTests(SimpleTestCase):
    def test_relative_by_default(self):
        self.assertEqual(discord_timestamp(SAMPLE), "<t:1791831600:R>")

    def test_full_date(self):
        self.assertEqual(discord_timestamp(SAMPLE, "F"), "<t:1791831600:F>")

    def test_every_documented_style(self):
        self.assertEqual(DISCORD_TIMESTAMP_STYLES, frozenset("tTdDfFR"))
        for style in sorted(DISCORD_TIMESTAMP_STYLES):
            with self.subTest(style=style):
                self.assertEqual(discord_timestamp(SAMPLE, style), f"<t:{SAMPLE_UNIX}:{style}>")

    def test_unknown_style_is_rejected(self):
        for style in ("x", "", "RR", "r"):
            with self.subTest(style=style), self.assertRaises(ValueError):
                discord_timestamp(SAMPLE, style)

    def test_none(self):
        self.assertEqual(discord_timestamp(None), "")
        self.assertEqual(discord_timestamp(None, "F"), "")

    def test_ignores_site_zone_active_zone_and_language(self):
        local = SAMPLE.astimezone(ZoneInfo("America/Los_Angeles"))
        for label, zone in local_zones():
            with self.subTest(label), zone, translation.override("zh-hans"):
                self.assertEqual(discord_timestamp(SAMPLE), "<t:1791831600:R>")
                self.assertEqual(discord_timestamp(local, "F"), "<t:1791831600:F>")


class ParseEveTests(SimpleTestCase):
    def test_date_and_clock(self):
        result = parse_eve_datetime(date(2026, 10, 12), "19:00")
        self.assertEqual(result, SAMPLE)
        self.assertIs(result.tzinfo, UTC)

    def test_strings(self):
        self.assertEqual(parse_eve_datetime("2026-10-12", "19:00"), SAMPLE)
        self.assertEqual(parse_eve_datetime(" 2026-10-12 ", " 19:00 "), SAMPLE)

    def test_time_object(self):
        self.assertEqual(parse_eve_datetime(date(2026, 10, 12), time(19, 0)), SAMPLE)

    def test_clock_edges(self):
        self.assertEqual(parse_eve_clock("00:00"), time(0, 0))
        self.assertEqual(parse_eve_clock("23:59"), time(23, 59))
        self.assertEqual(parse_eve_clock("09:05"), time(9, 5))

    def test_invalid_clock(self):
        for value in ("24:00", "19:60", "9:30", "1900", "19:00:00", "19.00", "", None, "ab:cd", "١٩:٠٠"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_eve_clock(value)

    def test_invalid_date(self):
        for value in ("2026-02-30", "2026-13-01", "12.10.2026", "2026-1-5", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_eve_date(value)

    def test_date_from_datetime_uses_the_eve_date(self):
        shanghai = datetime(2026, 10, 13, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(parse_eve_date(shanghai), date(2026, 10, 12))

    def test_month_and_year_end(self):
        self.assertEqual(parse_eve_datetime("2026-12-31", "23:59"), utc(2026, 12, 31, 23, 59))
        self.assertEqual(current_year_month(parse_eve_datetime("2026-12-31", "23:59")), (2026, 12))
        self.assertEqual(parse_eve_datetime("2028-02-29", "00:00"), utc(2028, 2, 29))

    def test_ignores_site_and_active_time_zone(self):
        for label, zone in local_zones():
            with self.subTest(label), zone:
                result = parse_eve_datetime("2026-10-12", "19:00")
                self.assertEqual(result, SAMPLE)
                self.assertEqual(result.utcoffset(), timedelta(0))
                self.assertEqual(discord_timestamp(result), "<t:1791831600:R>")
                self.assertEqual(format_eve(result), "2026-10-12 19:00")

    def test_clock_pattern_matches_the_parser(self):
        pattern = re.compile(EVE_CLOCK_PATTERN)
        for value in ("00:00", "09:05", "19:00", "23:59"):
            with self.subTest(value=value):
                self.assertTrue(pattern.fullmatch(value))
        for value in ("24:00", "19:60", "9:30", "1900"):
            with self.subTest(value=value):
                self.assertIsNone(pattern.fullmatch(value))


class UtcDayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user()
        # Late in the EVE day: already the next day in Shanghai.
        cls.late = f.create_operation(cls.fc, status=FleetOperation.Status.CLOSED, started_at=utc(2026, 10, 12, 23, 30))
        # Early in the EVE day: still the previous day in Los Angeles.
        cls.early = f.create_operation(cls.fc, status=FleetOperation.Status.CLOSED, started_at=utc(2026, 10, 13, 0, 30))

    def days(self, expression):
        rows = FleetOperation.objects.annotate(day=expression).values_list("pk", "day")
        return dict(rows)

    def test_groups_on_the_utc_date_under_a_zone_ahead_of_utc(self):
        with override_settings(TIME_ZONE="Asia/Shanghai"):
            local = self.days(TruncDate("started_at"))
            eve = self.days(utc_day("started_at"))
        self.assertEqual(local[self.late.pk], date(2026, 10, 13))
        self.assertEqual(eve[self.late.pk], date(2026, 10, 12))
        self.assertEqual(eve[self.early.pk], date(2026, 10, 13))

    def test_groups_on_the_utc_date_under_a_zone_behind_utc(self):
        with override_settings(TIME_ZONE="America/Los_Angeles"), timezone.override(ZoneInfo("America/Los_Angeles")):
            local = self.days(TruncDate("started_at"))
            eve = self.days(utc_day("started_at"))
        self.assertEqual(local[self.early.pk], date(2026, 10, 12))
        self.assertEqual(eve[self.early.pk], date(2026, 10, 13))
        self.assertEqual(eve[self.late.pk], date(2026, 10, 12))

    def test_month_bounds_select_by_utc_start(self):
        start, end = month_bounds(2026, 10)
        with override_settings(TIME_ZONE="Pacific/Kiritimati"):
            selected = set(
                FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end).values_list("pk", flat=True)
            )
        self.assertEqual(selected, {self.late.pk, self.early.pk})
