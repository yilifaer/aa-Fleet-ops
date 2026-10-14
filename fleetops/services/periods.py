# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""EVE time (UTC) helpers for months, display strings and Discord timestamps.

Everything here works in UTC, whatever ``TIME_ZONE`` the site uses and whatever
time zone is active for the request. Naive datetimes are read as EVE time.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_timezone

from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dates import MONTHS
from django.utils.translation import gettext

UTC = dt_timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

EVE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# The years a month selector and a monthly target accept.
MIN_YEAR = 2003
MAX_YEAR = 2200

# Month names for month selectors, in the language of the viewer (Django's own names, so the C library's
# locale does not decide what the page says).
MONTH_NAMES = tuple(MONTHS[number] for number in range(1, 13))
# The same months in English, for file names and logs.
ENGLISH_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Shared by the form field and the HTML ``pattern`` attribute of the time input.
EVE_CLOCK_PATTERN = r"([01]\d|2[0-3]):[0-5]\d"
_CLOCK_RE = re.compile(EVE_CLOCK_PATTERN, re.ASCII)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)

# https://discord.com/developers/docs/reference#message-formatting-timestamp-styles
DISCORD_TIMESTAMP_STYLES = frozenset("tTdDfFR")


def to_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` as an aware UTC datetime; naive values are taken as UTC."""
    if value is None:
        return None
    if timezone.is_naive(value):
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def eve_now() -> datetime:
    return timezone.now().astimezone(UTC)


def month_name(month: int) -> str:
    """The English name of a month, whatever language is active."""
    return ENGLISH_MONTH_NAMES[month - 1]


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    if not 1 <= month <= 12:
        raise ValueError(f"Month must be between 1 and 12, not {month!r}.")
    new_year, index = divmod(year * 12 + month - 1 + delta, 12)
    return new_year, index + 1


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """``[first day 00:00 UTC, first day of the next month 00:00 UTC)``."""
    next_year, next_month = shift_month(year, month, 1)
    return datetime(year, month, 1, tzinfo=UTC), datetime(next_year, next_month, 1, tzinfo=UTC)


def year_bounds(year: int) -> tuple[datetime, datetime]:
    return datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)


def year_month_of(value: datetime) -> tuple[int, int]:
    """The EVE month a point in time belongs to."""
    value = to_utc(value)
    return value.year, value.month


def current_year_month(now: datetime | None = None) -> tuple[int, int]:
    return year_month_of(timezone.now() if now is None else now)


def parse_year_month(params, default: tuple[int, int] | None = None) -> tuple[int, int]:
    """The EVE month in ``year`` and ``month`` query parameters.

    A missing, unreadable or out-of-range value gives ``default`` (the current month) for both numbers.
    """
    default = current_year_month() if default is None else default
    try:
        year = int(params.get("year", default[0]))
        month = int(params.get("month", default[1]))
    except (TypeError, ValueError):
        return default
    if not 1 <= month <= 12 or not MIN_YEAR <= year <= MAX_YEAR:
        return default
    return year, month


@dataclass(frozen=True, slots=True)
class MonthRef:
    year: int
    month: int
    start: datetime
    end: datetime
    is_current: bool
    is_finished: bool
    is_future: bool
    days_in_month: int
    # Days of the month that have begun in EVE time, today included ("day 12 of 31").
    days_elapsed: int
    label: str


def month_ref(year: int, month: int, now: datetime | None = None) -> MonthRef:
    start, end = month_bounds(year, month)
    now = to_utc(timezone.now() if now is None else now)
    days_in_month = calendar.monthrange(year, month)[1]
    if now < start:
        days_elapsed = 0
    elif now >= end:
        days_elapsed = days_in_month
    else:
        days_elapsed = now.day
    return MonthRef(
        year=year,
        month=month,
        start=start,
        end=end,
        is_current=start <= now < end,
        is_finished=now >= end,
        is_future=now < start,
        days_in_month=days_in_month,
        days_elapsed=days_elapsed,
        label=f"{year:04d}-{month:02d}",
    )


def utc_day(expression) -> TruncDate:
    """Group a datetime column by its EVE (UTC) date instead of the local date."""
    return TruncDate(expression, tzinfo=UTC)


def format_eve(value, fmt: str = EVE_DATETIME_FORMAT) -> str:
    """Format a datetime in EVE time; empty string for no value."""
    if value is None or value == "":
        return ""
    if not isinstance(value, datetime):
        raise TypeError(f"Expected a datetime, got {type(value).__name__}.")
    return to_utc(value).strftime(fmt)


def parse_eve_date(value) -> date:
    if isinstance(value, datetime):
        return to_utc(value).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not _DATE_RE.fullmatch(text):
        raise ValueError(gettext("Enter the date as YYYY-MM-DD."))
    try:
        return date(int(text[:4]), int(text[5:7]), int(text[8:]))
    except ValueError:
        raise ValueError(gettext("%(date)s is not a valid date.") % {"date": text}) from None


def parse_eve_clock(value) -> time:
    """Read a 24-hour ``HH:MM`` EVE clock time."""
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not _CLOCK_RE.fullmatch(text):
        raise ValueError(gettext("Enter the time as HH:MM (24-hour EVE time)."))
    return time(int(text[:2]), int(text[3:]))


def parse_eve_datetime(day, clock) -> datetime:
    """Combine an EVE date and ``HH:MM`` clock time into an aware UTC datetime."""
    return datetime.combine(parse_eve_date(day), parse_eve_clock(clock), tzinfo=UTC)


def unix_timestamp(value: datetime | None) -> int | None:
    """Whole seconds since the Unix epoch, rounded down."""
    if value is None:
        return None
    return (to_utc(value) - EPOCH) // timedelta(seconds=1)


def discord_timestamp(value: datetime | None, style: str = "R") -> str:
    """Discord ``<t:unix:style>`` markup, shown in each reader's own time zone."""
    if style not in DISCORD_TIMESTAMP_STYLES:
        raise ValueError(f"Unknown Discord timestamp style {style!r}.")
    if value is None:
        return ""
    return f"<t:{unix_timestamp(value)}:{style}>"
