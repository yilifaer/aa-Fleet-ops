"""Pages show EVE time (UTC), whatever TIME_ZONE the site uses, and file fleets by their EVE month."""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    OperationActionAttempt,
)
from fleetops.services.fleet_view import stat_text

from . import factories as f
from .test_views_permissions import FleetOpsViewTestCase

UTC = dt_timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")


def late_evening(days_ago=3):
    """23:30:12 EVE a few days ago: already the next morning (07:30) in Shanghai."""
    day = timezone.now().astimezone(UTC) - timedelta(days=days_ago)
    return day.replace(hour=23, minute=30, second=12, microsecond=0)


def minute(moment):
    return moment.strftime("%Y-%m-%d %H:%M")


def second(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def local_minute(moment):
    return minute(moment.astimezone(SHANGHAI))


@override_settings(TIME_ZONE="Asia/Shanghai")
class EveTimePagesTests(FleetOpsViewTestCase):
    """A site eight hours ahead of EVE time must still read 23:30 for a 23:30 EVE fleet."""

    def setUp(self):
        super().setUp()
        self.late = late_evening()
        FleetOperation.objects.filter(pk=self.own_closed.pk).update(
            started_at=self.late, ended_at=self.late + timedelta(hours=1)
        )
        self.client = self.client_for(self.lead)

    def get(self, name, *args, user=None, **params):
        client = self.client_for(user) if user is not None else self.client
        response = client.get(reverse(f"fleetops:{name}", args=args), params)
        self.assertEqual(response.status_code, 200)
        return response

    def assertEveTime(self, response, expected, *, local=None):
        self.assertContains(response, expected)
        if local:
            self.assertNotContains(response, local)

    def test_fleet_archive_files_fleets_by_eve_month_and_shows_eve_time(self):
        late = self.late

        in_month = self.get("fleet_operations", year=late.year, month=late.month)
        self.assertIn(self.own_closed.pk, {op.pk for op in in_month.context["page"].object_list})
        self.assertEveTime(in_month, minute(late), local=local_minute(late))

        next_month = late.replace(day=1) + timedelta(days=32)
        elsewhere = self.get("fleet_operations", year=next_month.year, month=next_month.month)
        self.assertNotIn(self.own_closed.pk, {op.pk for op in elsewhere.context["page"].object_list})

        whole_year = self.get("fleet_operations", year=late.year)
        self.assertIn(self.own_closed.pk, {op.pk for op in whole_year.context["page"].object_list})

    def test_fleet_archive_month_edges_are_utc(self):
        # Midnight EVE time is 08:00 in Shanghai; the fleets sit on both sides of it.
        first = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        before = f.closed_operation(self.fc, started_at=first - timedelta(minutes=1))
        after = f.closed_operation(self.fc, started_at=first)

        march = {op.pk for op in self.get("fleet_operations", year=2026, month=3).context["page"].object_list}
        february = {op.pk for op in self.get("fleet_operations", year=2026, month=2).context["page"].object_list}

        self.assertIn(after.pk, march)
        self.assertNotIn(before.pk, march)
        self.assertIn(before.pk, february)
        self.assertNotIn(after.pk, february)

    def test_fleet_archive_year_edges_are_utc(self):
        last = f.closed_operation(self.fc, started_at=datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
        first = f.closed_operation(self.fc, started_at=datetime(2027, 1, 1, 0, 0, tzinfo=UTC))

        year_2026 = {op.pk for op in self.get("fleet_operations", year=2026).context["page"].object_list}
        year_2027 = {op.pk for op in self.get("fleet_operations", year=2027).context["page"].object_list}

        self.assertIn(last.pk, year_2026)
        self.assertNotIn(first.pk, year_2026)
        self.assertIn(first.pk, year_2027)
        self.assertNotIn(last.pk, year_2027)

    def test_archive_opens_on_the_eve_year(self):
        # 07:30 on 1 January in Shanghai, still 31 December 2026 in EVE time.
        with mock.patch("django.utils.timezone.now", return_value=datetime(2026, 12, 31, 23, 30, tzinfo=UTC)):
            client = self.client_for(self.lead)
            response = client.get(reverse("fleetops:fleet_operations"))

        self.assertEqual(response.context["year"], 2026)

    def test_attendance_history(self):
        response = self.get("attendance_history_me", user=self.fc)

        self.assertEveTime(response, minute(self.late), local=local_minute(self.late))
        self.assertContains(response, "<th>EVE Time</th>")

    def test_alliance_history(self):
        response = self.get("attendance_history_alliance")

        self.assertEveTime(response, minute(self.late), local=local_minute(self.late))

    def test_dashboard(self):
        FleetOperation.objects.filter(pk=self.own_active.pk).update(last_esi_update=self.late)
        AttendanceRecord.objects.filter(operation=self.own_active).update(created_at=self.late)

        response = self.get("dashboard", user=self.fc)

        # The active fleet's last read and the recent fleets are in EVE time.
        self.assertEveTime(response, f"{minute(self.late)} EVE", local=local_minute(self.late))

    def test_audit_log(self):
        AuditLog.objects.all().update(created_at=self.late)

        response = self.get("audit_log")

        self.assertEveTime(response, second(self.late), local=second(self.late.astimezone(SHANGHAI)))
        self.assertContains(response, "When (EVE)")

    def test_manual_attendance_entries(self):
        AttendanceRecord.objects.filter(pk=self.own_manual.pk).update(created_at=self.late)

        response = self.get("manual_attendance")

        self.assertContains(response, f"{minute(self.late)} · ")
        self.assertContains(response, f"<td>{minute(self.late)}</td>", html=False)
        self.assertNotContains(response, local_minute(self.late))

    def test_fc_statistics_detail(self):
        month_start = self.late.replace(day=1, hour=0, minute=0, second=0)
        response = self.get("fc_statistics_detail", self.fc.pk, year=month_start.year, month=month_start.month)

        self.assertEveTime(response, minute(self.late), local=local_minute(self.late))


@override_settings(TIME_ZONE="Asia/Shanghai")
class FleetPageEveTimeTests(FleetOpsViewTestCase):
    def setUp(self):
        super().setUp()
        self.late = late_evening(days_ago=1)
        self.client = self.client_for(self.lead)

    def test_header_cards_and_stale_notice(self):
        FleetOperation.objects.filter(pk=self.own_active.pk).update(last_esi_update=self.late)

        response = self.client.get(self.detail_url(self.own_active))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"{second(self.late)} EVE")
        self.assertNotContains(response, second(self.late.astimezone(SHANGHAI)))

    def test_live_header_text_matches_the_page(self):
        self.assertEqual(stat_text(self.late), f"{second(self.late)} EVE")
        self.assertEqual(stat_text(None), "—")

    def assertDetailShows(self, operation, moment):
        response = self.client.get(self.detail_url(operation))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, minute(moment))
        self.assertNotContains(response, local_minute(moment))

    def test_members_table_shows_when_a_pilot_was_last_seen(self):
        seen = self.late + timedelta(minutes=7)
        FleetMemberState.objects.filter(operation=self.own_active).update(last_seen=seen)

        self.assertDetailShows(self.own_active, seen)

    def test_event_list(self):
        happened = self.late + timedelta(minutes=13)
        FleetMemberEvent.objects.filter(operation=self.own_active).update(created_at=happened)

        self.assertDetailShows(self.own_active, happened)

    def test_closed_fleet_pap_table(self):
        first, last = self.late + timedelta(minutes=21), self.late + timedelta(minutes=29)
        AttendanceRecord.objects.filter(operation=self.own_closed).update(first_seen=first, last_seen=last)

        self.assertDetailShows(self.own_closed, first)
        self.assertDetailShows(self.own_closed, last)

    def test_attempt_times(self):
        step = self.own_active.actions.get(action="discord_ping")
        # The newest attempt is the step's result; older ones are listed underneath it.
        attempts = [
            OperationActionAttempt.objects.create(action=step, status="failed", message="try")
            for _ in range(2)
        ]
        for number, attempt in enumerate(reversed(attempts)):
            OperationActionAttempt.objects.filter(pk=attempt.pk).update(
                created_at=self.late - timedelta(minutes=number)
            )

        response = self.client.get(self.detail_url(self.own_active))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, second(self.late - timedelta(minutes=1)))
        self.assertNotContains(response, second((self.late - timedelta(minutes=1)).astimezone(SHANGHAI)))
