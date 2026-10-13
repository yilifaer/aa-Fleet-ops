"""Which fleets and PAP count in statistics and incentives (#37), and FC points."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, FleetOperation, IncentivePeriod, MonthlyFCStatistic, ReviewStatus
from fleetops.services.counting import (
    counted_attendance,
    counted_operations,
    evaluate_stats_inclusion,
    operation_count_reason,
    set_stats_mode,
)
from fleetops.services.dashboard import dashboard_metrics
from fleetops.services.incentives import rebuild_period
from fleetops.services.operations import create_manual_fleet, end_fleet
from fleetops.services.statistics import (
    attendance_history_queryset,
    corporation_statistics,
    fc_statistics,
    member_statistics,
)

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
StatsMode = FleetOperation.StatsMode


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class CountedFleetsTests(TestCase):
    """Pending, rejected, excluded and short fleets are left out of every statistic."""

    def setUp(self):
        f.settings(incentive_enabled=True)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        start = utc(2026, 3, 10, 19)
        self.counted = self.fleet(start)
        self.uncounted = [
            self.fleet(start + timedelta(days=1), review_status=ReviewStatus.PENDING),
            self.fleet(start + timedelta(days=2), review_status=ReviewStatus.REJECTED),
            self.fleet(start + timedelta(days=3), stats_mode=StatsMode.EXCLUDE, counts_for_stats=False),
            self.fleet(start + timedelta(days=4), counts_for_stats=False),
        ]
        self.running = f.create_operation(self.fc, started_at=start + timedelta(days=5), type_obj=self.stratop)
        for operation in [self.counted, *self.uncounted, self.running]:
            f.add_attendance(operation, self.pilot)

    def fleet(self, started_at, **extra):
        return f.create_operation(self.fc, status=Status.CLOSED, started_at=started_at, type_obj=self.stratop, **extra)

    def test_only_closed_approved_included_fleets_count(self):
        self.assertEqual(list(counted_operations(FleetOperation.objects.all())), [self.counted])
        self.assertEqual(
            set(counted_operations(include_active=True)), {self.counted, self.running}
        )
        self.assertEqual([row.operation for row in counted_attendance()], [self.counted])

    def test_member_corporation_and_history_statistics(self):
        self.assertEqual(member_statistics(self.pilot, 2026, 3)["total"], 1)
        self.assertEqual(member_statistics(self.pilot, 2026, 3)["unique_fleets"], 1)
        self.assertEqual(corporation_statistics(f.DEFAULT_CORP[0], 2026, 3)["total_attendance"], 1)
        self.assertEqual(
            [row.operation for row in attendance_history_queryset(user=self.pilot, days=36500)], [self.counted]
        )

    def test_fc_statistics_and_incentives(self):
        stats = fc_statistics(self.fc, 2026, 3)
        self.assertEqual((stats["fleet_count"], stats["total_points"]), (1, 2.0))
        self.assertEqual(fc_statistics(self.fc, 2026, 3, include_active=True)["fleet_count"], 2)

        period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=2)
        rebuild_period(period)

        row = MonthlyFCStatistic.objects.get(period=period, fc_user=self.fc)
        # Excluded fleets do not help reach the minimum fleets either.
        self.assertEqual((row.fleet_count, row.eligible), (1, False))
        self.assertEqual(period.policy_snapshot["fc_points"], "fleet type weight x PAP multiplier")
        self.assertEqual(period.policy_snapshot["counted"], "closed, approved, counts_for_stats")

    def test_dashboard_counts_only_counted_pap(self):
        with mock.patch("django.utils.timezone.now", return_value=utc(2026, 3, 20, 12)):
            metrics = dashboard_metrics(self.pilot)

        self.assertEqual(metrics["cards"][0]["month"], 1)
        self.assertEqual([item["operation"] for item in metrics["recent"]], [self.counted])

    def test_all_fc_statistics_page(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        only_excluded = f.create_user("excludedfc", perms=f.FC_PERMS)
        f.create_operation(only_excluded, status=Status.CLOSED, started_at=utc(2026, 3, 3, 19), counts_for_stats=False)
        self.client.force_login(lead)

        page = self.client.get(reverse("fleetops:all_fc_statistics"), {"year": 2026, "month": 3})

        users = [row.user for row in page.context["rows"]]
        self.assertEqual(users, [self.fc])

    def test_reasons(self):
        pending, rejected, excluded, short = self.uncounted
        f.settings(minimum_fleet_minutes=30)
        excluded.stats_note = "Test fleet"

        self.assertEqual(operation_count_reason(self.counted), "")
        self.assertEqual(operation_count_reason(pending), "Pending approval")
        self.assertEqual(operation_count_reason(rejected), "Rejected")
        self.assertEqual(operation_count_reason(excluded), "Excluded: Test fleet")
        self.assertEqual(operation_count_reason(short), "Shorter than 30 minutes")

    def test_archive_marks_fleets_that_do_not_count(self):
        self.client.force_login(f.create_user("lead", perms=f.FC_LEAD_PERMS))

        page = self.client.get(reverse("fleetops:fleet_operations"), {"year": 2026})

        self.assertContains(page, ">Not counted</span>", count=len(self.uncounted))
        self.assertContains(page, "Pending approval")


class FCPointsTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.stratop = f.fleet_type("StratOp", "2.50")
        self.fc = f.create_user("fc", perms=f.FC_PERMS)

    def test_points_are_the_weight_times_the_multiplier(self):
        f.create_operation(self.fc, status=Status.CLOSED, started_at=utc(2026, 3, 1, 19), type_obj=self.stratop, attendance_multiplier=3)
        f.create_operation(self.fc, status=Status.CLOSED, started_at=utc(2026, 3, 2, 19), type_obj=self.stratop)
        # Waiting for the FC's choice: counts at 1x until then.
        f.create_operation(
            self.fc, status=Status.CLOSED, started_at=utc(2026, 3, 3, 19), type_obj=self.stratop, multiplier_pending=True
        )

        stats = fc_statistics(self.fc, 2026, 3)
        self.assertEqual(stats["fleet_count"], 3)
        self.assertAlmostEqual(stats["total_points"], 12.5)
        self.assertEqual(stats["breakdown"]["StratOp"], {"count": 3, "points": 12.5})

        period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=1)
        rebuild_period(period)
        row = MonthlyFCStatistic.objects.get(period=period, fc_user=self.fc)
        self.assertEqual(row.total_points, Decimal("12.50"))


class MinimumDurationTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        f.settings(minimum_fleet_minutes=30)

    def ended(self, ran):
        # The fleet closes exactly ``ran`` after its start.
        closed_at = timezone.now()
        operation = f.create_operation(self.fc, started_at=closed_at - ran)
        with mock.patch("fleetops.services.operations.sync_operation"), mock.patch(
            "django.utils.timezone.now", return_value=closed_at
        ):
            end_fleet(operation, actor=self.fc)
        operation.refresh_from_db()
        return operation

    def test_short_fleets_do_not_count_from_their_close(self):
        self.assertFalse(self.ended(timedelta(minutes=29, seconds=59)).counts_for_stats)
        self.assertTrue(self.ended(timedelta(minutes=30)).counts_for_stats)

    def test_a_fleet_of_exactly_the_minimum_counts(self):
        # Manual fleets and edited times are entered to the minute.
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=utc(2026, 3, 12, 19), ended_at=utc(2026, 3, 12, 19, 30)
        )
        self.assertTrue(evaluate_stats_inclusion(operation))

        operation.ended_at = utc(2026, 3, 12, 19, 29, 59)
        self.assertFalse(evaluate_stats_inclusion(operation))

    def test_zero_counts_every_fleet(self):
        f.settings(minimum_fleet_minutes=0)

        self.assertTrue(self.ended(timedelta(minutes=1)).counts_for_stats)

    def test_a_changed_setting_does_not_touch_closed_fleets(self):
        short = self.ended(timedelta(minutes=10))
        f.settings(minimum_fleet_minutes=0)

        short.refresh_from_db()
        self.assertFalse(short.counts_for_stats)

    def test_manual_fleets_are_measured_when_created(self):
        operation = create_manual_fleet(
            user=self.lead,
            cleaned_data={
                "started_at": utc(2026, 3, 12, 19),
                "ended_at": utc(2026, 3, 12, 19, 20),
                "fleet_type": f.fleet_type(),
                "formup": "Jita",
            },
        )

        self.assertFalse(operation.counts_for_stats)
        self.assertEqual(operation.review_status, ReviewStatus.APPROVED)
        self.assertEqual(AuditLog.objects.get(action="fleet.manual_create").new_value["counts_for_stats"], False)

    def test_edited_times_are_measured_again(self):
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=utc(2026, 3, 12, 19), ended_at=utc(2026, 3, 12, 19, 20)
        )
        self.assertFalse(evaluate_stats_inclusion(operation))

        operation.ended_at = utc(2026, 3, 12, 20)
        self.assertTrue(evaluate_stats_inclusion(operation))

    def test_modes_win_over_the_duration(self):
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=utc(2026, 3, 12, 19), ended_at=utc(2026, 3, 12, 22)
        )
        operation.stats_mode = StatsMode.EXCLUDE
        self.assertFalse(evaluate_stats_inclusion(operation))
        operation.stats_mode = StatsMode.INCLUDE
        operation.ended_at = utc(2026, 3, 12, 19, 5)
        self.assertTrue(evaluate_stats_inclusion(operation))


class StatsModeTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        f.settings(minimum_fleet_minutes=30)
        started = timezone.now() - timedelta(hours=3)
        self.short = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=started, ended_at=started + timedelta(minutes=10),
            counts_for_stats=False,
        )
        self.url = reverse("fleetops:set_stats_mode", args=[self.short.uuid])

    def post(self, user, **data):
        self.client.force_login(user)
        response = self.client.post(self.url, data)
        self.short.refresh_from_db()
        return response

    def test_the_fc_may_exclude_with_a_reason(self):
        self.post(self.fc, mode="exclude", note="Test fleet")

        self.assertEqual((self.short.stats_mode, self.short.counts_for_stats), (StatsMode.EXCLUDE, False))
        entry = AuditLog.objects.get(action="fleet.stats_mode")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.old_value["stats_mode"], "auto")
        self.assertEqual(entry.new_value, {"stats_mode": "exclude", "counts_for_stats": False, "stats_note": "Test fleet"})

    def test_excluding_needs_a_reason(self):
        self.post(self.fc, mode="exclude", note="   ")

        self.assertEqual(self.short.stats_mode, StatsMode.AUTO)
        self.assertFalse(AuditLog.objects.filter(action="fleet.stats_mode").exists())

    def test_only_fleet_managers_count_a_fleet(self):
        for mode in ("include", "auto"):
            with self.subTest(mode=mode):
                self.assertEqual(self.post(self.fc, mode=mode).status_code, 403)

        self.post(self.lead, mode="include", note="Real fight, short but counted")
        self.assertEqual((self.short.stats_mode, self.short.counts_for_stats), (StatsMode.INCLUDE, True))

        self.post(self.lead, mode="auto")
        self.assertEqual((self.short.stats_mode, self.short.counts_for_stats), (StatsMode.AUTO, False))

    def test_other_fcs_cannot_touch_the_fleet(self):
        other = f.create_user("otherfc", perms=f.FC_PERMS)

        self.assertEqual(self.post(other, mode="exclude", note="Not mine").status_code, 404)
        self.assertEqual(self.short.stats_mode, StatsMode.AUTO)

    def test_pending_fleets_never_count_even_when_included(self):
        FleetOperation.objects.filter(pk=self.short.pk).update(review_status=ReviewStatus.PENDING)
        self.short.refresh_from_db()

        set_stats_mode(self.short, StatsMode.INCLUDE, actor=self.lead, note="")

        self.assertTrue(self.short.counts_for_stats)
        self.assertNotIn(self.short, counted_operations())

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            set_stats_mode(self.short, "sometimes", actor=self.lead, note="")

    def test_fleet_page_card(self):
        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[self.short.uuid]))
        self.assertContains(page, "No — Shorter than 30 minutes")
        self.assertContains(page, 'id="stats-exclude-form"')
        self.assertNotContains(page, 'id="stats-include-form"')
        self.assertContains(page, 'id="badge-not-counted"')

        self.client.force_login(self.lead)
        page = self.client.get(reverse("fleetops:operation_detail", args=[self.short.uuid]))
        self.assertContains(page, 'id="stats-include-form"')
