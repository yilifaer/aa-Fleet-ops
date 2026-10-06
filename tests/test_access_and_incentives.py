"""Regression tests for fleet record access, FC incentives, paging and manual fleet records."""

import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetMemberState,
    FleetOperation,
    IncentivePeriod,
    MonthlyFCStatistic,
    OperationRoleAssignment,
)
from fleetops.services.attendance import create_manual_attendance
from fleetops.services.audit import audit
from fleetops.services.incentives import (
    INCENTIVES_DISABLED_MESSAGE,
    WAIVER_LOCKED_MESSAGE,
    IncentiveError,
    finalize_period,
    rebuild_period,
    set_waiver,
    unlock_period,
)
from fleetops.services.roles import add_role_assignment
from fleetops.services.statistics import fc_statistics, member_statistics

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
PeriodStatus = IncentivePeriod.Status
Role = OperationRoleAssignment.Role
MANUAL = AttendanceRecord.Source.MANUAL

# An FC who may run and manage their own fleets but has no read access to every fleet record.
OWN_FLEET_FC_PERMS = ["fleetops.basic_access", "fleetops.start_fleet", "fleetops.manage_own_fleet"]
NOT_RECORDABLE = (Status.DRAFT, Status.STARTING, Status.ENDING, Status.CANCELLED, Status.ERROR)
WEBHOOK_SECRET = "hook-secret-token"


def utc(*args):
    return datetime(*args, tzinfo=UTC)


NOW = utc(2026, 5, 20, 12, 0)


class FrozenNowMixin:
    """Pin timezone.now so fleets created relative to it stay in one archive year and month."""

    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()


def closed_op(fc_user, started_at, **extra):
    return f.create_operation(fc_user, status=Status.CLOSED, started_at=started_at, **extra)


def credit(operation, user, *, role=Role.BACKSEAT_FC, grants_fc_credit=True):
    main = f.main_of(user)
    return OperationRoleAssignment.objects.create(
        operation=operation,
        role=role,
        character_id=main.character_id,
        character_name=main.character_name,
        main_character_id=main.character_id,
        main_character_name=main.character_name,
        auth_user=user,
        corporation_id=main.corporation_id,
        corporation_name=main.corporation_name,
        grants_fc_credit=grants_fc_credit,
    )


def track(operation, user):
    main = f.main_of(user)
    return FleetMemberState.objects.create(
        operation=operation,
        character_id=main.character_id,
        character_name=main.character_name,
        main_character_id=main.character_id,
        main_character_name=main.character_name,
        auth_user=user,
        corporation_id=main.corporation_id,
        corporation_name=main.corporation_name,
        first_seen=operation.started_at,
        last_seen=operation.started_at,
    )


def stat_for(period, user):
    return MonthlyFCStatistic.objects.get(period=period, fc_user=user)


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# FC incentives
# ---------------------------------------------------------------------------


class ClosedFleetCreditTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.co_fc = f.create_user("cofc", perms=f.FC_PERMS)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=2)

    def test_rebuild_ignores_fleets_that_are_not_closed(self):
        closed_op(self.fc, utc(2026, 3, 1), type_obj=self.stratop)
        for status in (Status.ACTIVE, Status.STARTING, Status.ENDING, Status.ERROR):
            op = f.create_operation(self.fc, status=status, started_at=utc(2026, 3, 2), type_obj=self.stratop)
            credit(op, self.co_fc)

        rebuild_period(self.period)

        row = stat_for(self.period, self.fc)
        self.assertEqual((row.fleet_count, row.total_points, row.eligible), (1, Decimal("2.00"), False))
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.co_fc).exists())

    def test_fc_statistics_follow_the_incentive_rule(self):
        closed_op(self.fc, utc(2026, 3, 1), type_obj=self.stratop)
        f.create_operation(self.fc, status=Status.ACTIVE, started_at=utc(2026, 3, 2), type_obj=self.stratop)
        f.create_operation(self.fc, status=Status.ERROR, started_at=utc(2026, 3, 3), type_obj=self.stratop)

        rebuild_period(self.period)

        stats = fc_statistics(self.fc, 2026, 3)
        self.assertEqual(stats["fleet_count"], stat_for(self.period, self.fc).fleet_count)
        self.assertEqual((stats["fleet_count"], stats["total_points"]), (1, 2.0))
        self.assertEqual(member_statistics(self.fc, 2026, 3)["fc_fleet_count"], 1)
        # The dashboard also shows fleets that are still running, but never errored ones.
        self.assertEqual(fc_statistics(self.fc, 2026, 3, include_active=True)["fleet_count"], 2)

    def test_fc_statistics_pages_only_list_closed_fleets(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        closed = closed_op(self.fc, utc(2026, 3, 1), type_obj=self.stratop)
        f.create_operation(self.fc, status=Status.ACTIVE, started_at=utc(2026, 3, 2), type_obj=self.stratop)
        running_only = f.create_user("running", perms=f.FC_PERMS)
        f.create_operation(running_only, status=Status.ACTIVE, started_at=utc(2026, 3, 2))
        self.client.force_login(lead)

        overview = self.client.get(reverse("fleetops:all_fc_statistics"), {"year": 2026, "month": 3})
        detail = self.client.get(reverse("fleetops:fc_statistics_detail", args=[self.fc.pk]), {"year": 2026, "month": 3})

        self.assertEqual([(r["user"], r["fleet_count"]) for r in overview.context["rows"]], [(self.fc, 1)])
        self.assertEqual(list(detail.context["operations"]), [closed])
        self.assertEqual(detail.context["stat"]["fleet_count"], 1)


class IncentivesDisabledTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=False)
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        closed_op(self.fc, utc(2026, 3, 1))
        self.period = IncentivePeriod.objects.create(
            year=2026, month=3, budget=300, minimum_fleets=1, status=PeriodStatus.REVIEW
        )
        self.row = MonthlyFCStatistic.objects.create(period=self.period, fc_user=self.fc, fleet_count=1)
        self.client.force_login(self.manager)

    def test_review_page_redirects_with_a_message(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 3})

        self.assertRedirects(response, reverse("fleetops:dashboard"), fetch_redirect_response=False)
        self.assertIn("FC incentives are disabled in the FleetOps settings.", message_texts(response))

    def test_period_cannot_be_created(self):
        self.client.post(
            reverse("fleetops:incentive_review"),
            {"create_period": "1", "year": 2026, "month": 4, "budget": 1, "minimum_fleets": 1},
        )
        self.assertFalse(IncentivePeriod.objects.filter(month=4).exists())

    def test_actions_change_nothing(self):
        for name, args in [
            ("incentive_recalculate", (self.period.pk,)),
            ("incentive_finalize", (self.period.pk,)),
            ("incentive_unlock", (self.period.pk,)),
            ("incentive_waiver", (self.period.pk, self.fc.pk)),
        ]:
            with self.subTest(view=name):
                response = self.client.post(reverse(f"fleetops:{name}", args=args), {"waived": "1"})
                self.assertRedirects(response, reverse("fleetops:dashboard"), fetch_redirect_response=False)

        self.period.refresh_from_db()
        self.row.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertEqual(self.period.policy_snapshot, {})
        self.assertFalse(self.row.waived)
        self.assertFalse(AuditLog.objects.exists())

    def test_unknown_period_is_still_404(self):
        response = self.client.post(reverse("fleetops:incentive_recalculate", args=[987_654]))
        self.assertEqual(response.status_code, 404)

    def test_services_refuse_to_calculate(self):
        open_period = IncentivePeriod.objects.create(year=2026, month=4, budget=300, minimum_fleets=1)
        closed_op(self.fc, utc(2026, 4, 2))

        finalized = IncentivePeriod.objects.create(year=2026, month=5, status=PeriodStatus.FINALIZED)
        for call in (
            lambda: rebuild_period(open_period),
            lambda: set_waiver(self.period, self.fc.pk, True),
            lambda: finalize_period(self.period, self.manager),
            lambda: unlock_period(finalized),
        ):
            with self.assertRaisesMessage(IncentiveError, INCENTIVES_DISABLED_MESSAGE):
                call()

        self.period.refresh_from_db()
        finalized.refresh_from_db()
        self.assertEqual((self.period.status, finalized.status), (PeriodStatus.REVIEW, PeriodStatus.FINALIZED))
        open_period.refresh_from_db()
        self.row.refresh_from_db()
        self.assertEqual(open_period.status, PeriodStatus.OPEN)
        self.assertFalse(open_period.fc_statistics.exists())
        self.assertFalse(self.row.waived)

    def test_navigation_link_follows_the_setting(self):
        url = reverse("fleetops:incentive_review")
        self.assertNotContains(self.client.get(reverse("fleetops:dashboard")), url)
        f.settings(incentive_enabled=True)
        self.assertContains(self.client.get(reverse("fleetops:dashboard")), url)


class IncentiveActionGuardTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.manager = f.create_user("manager", perms=f.MEMBER_PERMS + ["fleetops.manage_incentives"])
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        closed_op(self.fc1, utc(2026, 3, 1))
        closed_op(self.fc2, utc(2026, 3, 2))
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=300, minimum_fleets=1)
        self.review_url = reverse("fleetops:incentive_review") + "?year=2026&month=3"
        self.client.force_login(self.manager)

    def post(self, name, *args, data=None):
        return self.client.post(reverse(f"fleetops:{name}", args=args), data or {})

    def entries(self, action):
        return list(AuditLog.objects.filter(action=action).values_list("old_value", "new_value"))

    def test_recalculating_a_finalized_period_is_refused(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        response = self.post("incentive_recalculate", self.period.pk)

        self.assertRedirects(response, self.review_url, fetch_redirect_response=False)
        self.assertIn("Finalized periods must be unlocked before recalculation.", message_texts(response))
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)
        self.assertEqual(self.entries("incentive.recalculate"), [])

    def test_finalize_requires_review(self):
        response = self.post("incentive_finalize", self.period.pk)

        self.assertRedirects(response, self.review_url, fetch_redirect_response=False)
        self.assertIn("Period must be in review before finalizing.", message_texts(response))
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertEqual(self.entries("incentive.finalize"), [])

    def test_double_finalize_is_audited_once_with_real_statuses(self):
        rebuild_period(self.period)

        self.post("incentive_finalize", self.period.pk)
        second = self.post("incentive_finalize", self.period.pk)

        self.assertEqual(second.status_code, 302)
        self.assertEqual(self.entries("incentive.finalize"), [({"status": "review"}, {"status": "finalized"})])

    def test_unlock_requires_a_finalized_period(self):
        for status in (PeriodStatus.OPEN, PeriodStatus.REVIEW):
            with self.subTest(status=status):
                IncentivePeriod.objects.filter(pk=self.period.pk).update(status=status)

                response = self.post("incentive_unlock", self.period.pk)

                self.assertRedirects(response, self.review_url, fetch_redirect_response=False)
                self.period.refresh_from_db()
                self.assertEqual(self.period.status, status)
        self.assertEqual(self.entries("incentive.unlock"), [])
        with self.assertRaises(IncentiveError):
            unlock_period(self.period)

    def test_unlock_audits_the_real_previous_status(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        self.post("incentive_unlock", self.period.pk)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertEqual(self.entries("incentive.unlock"), [({"status": "finalized"}, {"status": "review"})])

    def test_waiver_for_fc_without_credited_fleets_is_handled(self):
        rebuild_period(self.period)
        FleetOperation.objects.filter(fc_user=self.fc1).update(status=Status.CANCELLED)

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertRedirects(response, self.review_url, fetch_redirect_response=False)
        self.assertIn(
            "This FC no longer has credited fleets in this month. Payouts were recalculated without them.",
            message_texts(response),
        )
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.fc1).exists())
        self.assertEqual(stat_for(self.period, self.fc2).final_payout, 300)
        self.assertEqual(self.entries("incentive.waiver"), [({"waived": False}, {"waived": True})])

    def test_waiver_on_a_finalized_period_gives_the_same_message_as_the_service(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertRedirects(response, self.review_url, fetch_redirect_response=False)
        self.assertEqual(message_texts(response), [WAIVER_LOCKED_MESSAGE])
        with self.assertRaisesMessage(IncentiveError, WAIVER_LOCKED_MESSAGE):
            set_waiver(self.period, self.fc1.pk, True)
        self.assertFalse(stat_for(self.period, self.fc1).waived)
        self.assertEqual(self.entries("incentive.waiver"), [])

    def test_set_waiver_returns_none_when_the_row_is_dropped(self):
        rebuild_period(self.period)
        FleetOperation.objects.filter(fc_user=self.fc1).update(status=Status.ERROR)

        self.assertIsNone(set_waiver(self.period, self.fc1.pk, True))
        self.assertTrue(set_waiver(self.period, self.fc2.pk, True).waived)


class DepartedMemberStatisticsTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True, history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        self.member_fc = f.create_user("memberfc", perms=f.FC_PERMS)
        self.departed_fc = f.create_user(
            "departedfc", perms=f.FC_PERMS, alliance=f.FOREIGN_ALLIANCE, corporation=f.OTHER_CORP
        )
        self.lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        shared = closed_op(self.member_fc, utc(2026, 3, 1))
        credit(shared, self.departed_fc)
        self.departed_op = closed_op(self.departed_fc, utc(2026, 3, 2))
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=500, minimum_fleets=1)

    def test_rebuild_skips_departed_fcs(self):
        rebuild_period(self.period)

        self.assertEqual(list(self.period.fc_statistics.values_list("fc_user_id", flat=True)), [self.member_fc.pk])
        self.assertEqual(stat_for(self.period, self.member_fc).final_payout, 500)

    def test_fc_statistics_are_empty_for_departed_fcs(self):
        self.assertEqual(fc_statistics(self.departed_fc, 2026, 3)["fleet_count"], 0)
        self.assertEqual(fc_statistics(self.member_fc, 2026, 3)["fleet_count"], 1)

    def test_statistics_pages_exclude_departed_fcs(self):
        self.client.force_login(self.lead)
        params = {"year": 2026, "month": 3}

        overview = self.client.get(reverse("fleetops:all_fc_statistics"), params)
        detail = self.client.get(reverse("fleetops:fc_statistics_detail", args=[self.departed_fc.pk]), params)

        self.assertEqual([r["user"] for r in overview.context["rows"]], [self.member_fc])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context["stat"]["fleet_count"], 0)
        self.assertEqual(list(detail.context["operations"]), [])

    def test_everyone_counts_without_configured_alliances(self):
        f.settings(history_alliance_ids="")
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.departed_fc).fleet_count, 2)


# ---------------------------------------------------------------------------
# Fleet record access
# ---------------------------------------------------------------------------


class FleetRecordAccessTests(FrozenNowMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("ownfc", perms=OWN_FLEET_FC_PERMS)
        self.other_fc = f.create_user("otherfc", perms=f.FC_PERMS)
        now = timezone.now()
        self.own_active = f.create_operation(self.fc)
        self.own_closed = closed_op(self.fc, now - timedelta(hours=5))
        self.credited = closed_op(self.other_fc, now - timedelta(hours=4))
        credit(self.credited, self.fc)
        self.unrelated_closed = closed_op(self.other_fc, now - timedelta(hours=3))
        self.unrelated_active = f.create_operation(self.other_fc)
        self.year = {"year": now.year}

    def archive_for(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:fleet_operations"), self.year)
        self.assertEqual(response.status_code, 200)
        return {op.pk for op in response.context["page"].object_list}

    def active_for(self, user):
        self.client.force_login(user)
        return {op.pk for op in self.client.get(reverse("fleetops:dashboard")).context["active_operations"]}

    def detail_status(self, user, operation):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid])).status_code

    def test_fc_without_view_all_fleets_sees_own_and_credited_fleets_only(self):
        self.assertEqual(self.archive_for(self.fc), {self.own_active.pk, self.own_closed.pk, self.credited.pk})
        for operation in (self.own_active, self.own_closed, self.credited):
            self.assertEqual(self.detail_status(self.fc, operation), 200)
        for operation in (self.unrelated_closed, self.unrelated_active):
            self.assertEqual(self.detail_status(self.fc, operation), 404)
        self.assertEqual(self.active_for(self.fc), {self.own_active.pk})

    def test_view_all_stats_is_a_statistics_permission_only(self):
        analyst = f.create_user("analyst", perms=f.CORP_MANAGEMENT_PERMS + ["fleetops.view_all_stats"])

        self.assertEqual(self.archive_for(analyst), set())
        self.assertEqual(self.active_for(analyst), set())
        self.assertEqual(self.detail_status(analyst, self.unrelated_closed), 404)
        self.assertEqual(self.client.get(reverse("fleetops:all_fc_statistics")).status_code, 200)

    def test_view_all_fleets_reads_every_fleet(self):
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])

        self.assertEqual(len(self.archive_for(viewer)), 5)
        self.assertEqual(self.active_for(viewer), {self.own_active.pk, self.unrelated_active.pk})
        self.assertEqual(self.detail_status(viewer, self.unrelated_closed), 200)

    def test_fc_detail_links_fleets_only_for_viewers_who_can_open_them(self):
        params = {"year": NOW.year, "month": NOW.month}
        url = reverse("fleetops:fc_statistics_detail", args=[self.fc.pk])
        link = reverse("fleetops:operation_detail", args=[self.own_closed.uuid])
        analyst = f.create_user("analyst", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.view_all_fleets"])

        self.client.force_login(analyst)
        self.assertNotContains(self.client.get(url, params), link)
        self.client.force_login(viewer)
        self.assertContains(self.client.get(url, params), link)
        self.client.force_login(self.fc)
        self.assertContains(self.client.get(url, params), link)


# ---------------------------------------------------------------------------
# Ending fleets
# ---------------------------------------------------------------------------


class EndFleetTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.client.force_login(self.fc)
        patcher = mock.patch("fleetops.services.operations.sync_operation", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def prompt_after(self, elapsed):
        frozen = timezone.now().replace(microsecond=0)
        operation = f.create_operation(self.fc, started_at=frozen - elapsed)
        with mock.patch("django.utils.timezone.now", return_value=frozen):
            response = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        return response.context["attendance_prompt_eligible"]

    def test_prompt_only_after_more_than_90_minutes(self):
        self.assertFalse(self.prompt_after(timedelta(minutes=90)))
        self.assertTrue(self.prompt_after(timedelta(minutes=90, seconds=1)))
        self.assertTrue(self.prompt_after(timedelta(minutes=90, seconds=59)))

    def end(self, operation, data):
        return self.client.post(reverse("fleetops:end_fleet", args=[operation.uuid]), data)

    def test_missing_multiplier_keeps_the_current_one(self):
        for data in ({}, {"attendance_multiplier": ""}):
            with self.subTest(data=data):
                operation = f.create_operation(self.fc, attendance_multiplier=2)

                response = self.end(operation, data)

                operation.refresh_from_db()
                self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 2))
                self.assertIn("Fleet ended. Attendance finalized at 2x.", message_texts(response))

    def test_invalid_multiplier_does_not_end_the_fleet(self):
        for value in ("0", "4", "abc", "2.5"):
            with self.subTest(value=value):
                operation = f.create_operation(self.fc, attendance_multiplier=2)

                response = self.end(operation, {"attendance_multiplier": value})

                self.assertRedirects(
                    response,
                    reverse("fleetops:operation_detail", args=[operation.uuid]),
                    fetch_redirect_response=False,
                )
                operation.refresh_from_db()
                self.assertEqual((operation.status, operation.attendance_multiplier), (Status.ACTIVE, 2))
                self.assertIn(
                    "Attendance multiplier must be 1x, 2x or 3x. The fleet was not ended.", message_texts(response)
                )

    def test_chosen_multiplier_is_applied(self):
        operation = f.create_operation(self.fc)
        self.end(operation, {"attendance_multiplier": "3"})
        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 3))


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


@mock.patch("fleetops.views.HISTORY_PAGE_SIZE", 2)
class AttendanceHistoryPagingTests(TestCase):
    BAD_PAGES = ("abc", "0", "-1", "1.5", "999", "")

    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot", perms=f.CORP_MANAGEMENT_PERMS)
        self.outsider = f.create_user("outsider", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        now = timezone.now()
        self.operations = [closed_op(self.fc, now - timedelta(days=day + 1)) for day in range(5)]
        for value, operation in enumerate(self.operations, start=1):
            f.add_attendance(operation, self.pilot, value=value)
        f.add_attendance(self.operations[0], self.outsider, value=10)

    def get(self, user, name, page=None):
        self.client.force_login(user)
        response = self.client.get(reverse(f"fleetops:{name}"), {} if page is None else {"page": page})
        self.assertEqual(response.status_code, 200)
        return response

    def assert_paged(self, user, name, *, count, total):
        first = self.get(user, name)
        self.assertEqual(len(first.context["rows"]), 2)
        self.assertEqual(first.context["page"].paginator.count, count)
        self.assertEqual(first.context["total"], total)
        self.assertContains(first, "Page 1 of 3")
        self.assertContains(first, "?page=2")

        last = self.get(user, name, page=3)
        self.assertEqual(len(last.context["rows"]), count - 4)
        self.assertEqual(last.context["total"], total)
        self.assertContains(last, "?page=2")

        seen = {r.pk for page in (1, 2, 3) for r in self.get(user, name, page=page).context["rows"]}
        self.assertEqual(len(seen), count)

        for page in self.BAD_PAGES:
            with self.subTest(page=page):
                self.assertIn(self.get(user, name, page=page).context["page"].number, (1, 3))

    def test_personal_history(self):
        self.assert_paged(self.pilot, "attendance_history_me", count=5, total=15)

    def test_corporation_history(self):
        self.assert_paged(self.pilot, "attendance_history_corporation", count=5, total=15)

    def test_alliance_history(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.assert_paged(lead, "attendance_history_alliance", count=6, total=25)

    def test_fleet_links_require_access_to_the_fleet(self):
        lead = f.create_user("lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        link = reverse("fleetops:operation_detail", args=[self.operations[0].uuid])

        self.assertNotContains(self.get(lead, "attendance_history_alliance"), link)
        self.assertContains(self.get(self.pilot, "attendance_history_me"), link)
        viewer = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.view_all_fleets"])
        self.assertContains(self.get(viewer, "attendance_history_alliance"), link)


@mock.patch("fleetops.views.AUDIT_PAGE_SIZE", 2)
class AuditLogPagingTests(TestCase):
    def setUp(self):
        self.auditor = f.create_user("auditor", perms=f.MEMBER_PERMS + ["fleetops.view_audit_log"])
        fleet_type = f.fleet_type()
        self.oldest = audit(self.auditor, "oldest.entry", fleet_type)
        AuditLog.objects.filter(pk=self.oldest.pk).update(created_at=timezone.now() - timedelta(days=30))
        for index in range(4):
            audit(self.auditor, f"entry.{index}", fleet_type)
        self.client.force_login(self.auditor)

    def get(self, **params):
        response = self.client.get(reverse("fleetops:audit_log"), params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_pages_cover_every_entry_newest_first(self):
        first = self.get()
        self.assertEqual([r.action for r in first.context["rows"]], ["entry.3", "entry.2"])
        self.assertContains(first, "Page 1 of 3")
        self.assertContains(first, "?page=2")

        last = self.get(page=3)
        self.assertEqual([r.action for r in last.context["rows"]], ["oldest.entry"])

    def test_bad_page_values_fall_back(self):
        for page in ("abc", "0", "-1", "1.5", "999"):
            with self.subTest(page=page):
                self.assertIn(self.get(page=page).context["page"].number, (1, 3))


# ---------------------------------------------------------------------------
# Archive filters
# ---------------------------------------------------------------------------


class ArchiveFleetTypeFilterTests(FrozenNowMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.stratop = f.fleet_type("StratOp")
        self.roam = f.fleet_type("Roam")
        self.strat_op = closed_op(self.lead, timezone.now() - timedelta(hours=2), type_obj=self.stratop)
        self.roam_op = closed_op(self.lead, timezone.now() - timedelta(hours=1), type_obj=self.roam)
        self.client.force_login(self.lead)
        self.client.raise_request_exception = False

    def listed(self, fleet_type):
        response = self.client.get(
            reverse("fleetops:fleet_operations"), {"year": timezone.now().year, "fleet_type": fleet_type}
        )
        self.assertEqual(response.status_code, 200)
        return {op.pk for op in response.context["page"].object_list}

    def test_valid_ids_filter_the_archive(self):
        self.assertEqual(self.listed(str(self.roam.pk)), {self.roam_op.pk})
        self.assertEqual(self.listed(f" {self.stratop.pk} "), {self.strat_op.pk})

    def test_invalid_ids_are_ignored(self):
        everything = {self.strat_op.pk, self.roam_op.pk}
        roam_in_arabic_indic_digits = str(self.roam.pk).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        for value in ("²", roam_in_arabic_indic_digits, "9" * 30, str(2**63), "0", "-1", "1e5", "abc", ""):
            with self.subTest(value=value):
                self.assertEqual(self.listed(value), everything)


# ---------------------------------------------------------------------------
# Manual attendance and special roles
# ---------------------------------------------------------------------------


class ManualRecordStatusTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot")
        self.client.force_login(self.fc)

    def attendance_payload(self, **extra):
        payload = {
            "character_id": f.main_of(self.pilot).character_id,
            "character_name": "",
            "attendance_value": 1,
            "duplicate_action": "keep",
            "notes": "",
        }
        payload.update(extra)
        return payload

    def test_per_fleet_attendance_only_on_active_or_closed_fleets(self):
        for status in NOT_RECORDABLE:
            with self.subTest(status=status):
                operation = f.create_operation(self.fc, status=status)

                response = self.client.post(
                    reverse("fleetops:add_manual_attendance", args=[operation.uuid]), self.attendance_payload()
                )

                self.assertRedirects(
                    response,
                    reverse("fleetops:operation_detail", args=[operation.uuid]),
                    fetch_redirect_response=False,
                )
                self.assertIn("Manual attendance can only be added to Active or Closed fleets.", message_texts(response))
                self.assertFalse(AttendanceRecord.objects.filter(operation=operation).exists())
        self.assertFalse(AuditLog.objects.exists())

        for status in (Status.ACTIVE, Status.CLOSED):
            with self.subTest(status=status):
                operation = f.create_operation(self.fc, status=status)
                self.client.post(
                    reverse("fleetops:add_manual_attendance", args=[operation.uuid]), self.attendance_payload()
                )
                self.assertTrue(AttendanceRecord.objects.filter(operation=operation, source=MANUAL).exists())

    def test_service_rejects_other_statuses(self):
        operation = f.create_operation(self.fc, status=Status.CANCELLED)
        with self.assertRaises(ValueError):
            create_manual_attendance(operation, actor=self.fc, character_id=f.main_of(self.pilot).character_id)
        self.assertFalse(AttendanceRecord.objects.exists())

    def test_historical_page_rejects_fleets_that_are_not_active_or_closed(self):
        errored = f.create_operation(self.fc, status=Status.ERROR)

        response = self.client.post(reverse("fleetops:manual_attendance"), self.attendance_payload(operation=errored.pk))

        self.assertEqual(response.status_code, 200)
        self.assertIn("operation", response.context["form"].errors)
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_historical_page_adds_attendance_to_a_closed_credited_fleet(self):
        other_fc = f.create_user("otherfc", perms=f.FC_PERMS)
        operation = closed_op(other_fc, timezone.now() - timedelta(days=3))
        credit(operation, self.fc)

        response = self.client.post(
            reverse("fleetops:manual_attendance"),
            self.attendance_payload(operation=operation.pk, attendance_value=2, notes="Late login"),
        )

        self.assertRedirects(response, reverse("fleetops:manual_attendance"), fetch_redirect_response=False)
        record = AttendanceRecord.objects.get(operation=operation)
        self.assertEqual((record.source, record.attendance_value, record.granted), (MANUAL, 2, True))
        self.assertEqual((record.auth_user, record.created_by, record.notes), (self.pilot, self.fc, "Late login"))
        entry = AuditLog.objects.get(action="attendance.manual_create")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.fc, "AttendanceRecord", str(record.pk)))
        self.assertEqual(entry.new_value, {"attendance_value": 2, "operation": str(operation.uuid)})

    def test_special_roles_only_on_active_or_closed_fleets(self):
        for status in (Status.DRAFT, Status.CANCELLED, Status.ERROR):
            with self.subTest(status=status):
                operation = f.create_operation(self.fc, status=status)
                track(operation, self.pilot)

                response = self.client.post(
                    reverse("fleetops:add_operation_role", args=[operation.uuid]),
                    {"role": Role.LOGI_ANCHOR, "character_id": str(f.main_of(self.pilot).character_id)},
                )

                self.assertEqual(response.status_code, 302)
                self.assertIn("Special roles can only be assigned on Active or Closed fleets.", message_texts(response))
                self.assertFalse(OperationRoleAssignment.objects.filter(operation=operation).exists())
                with self.assertRaises(ValueError):
                    add_role_assignment(
                        operation, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=f.main_of(self.pilot).character_id
                    )
        closed = closed_op(self.fc, timezone.now() - timedelta(hours=3))
        add_role_assignment(closed, actor=self.fc, role=Role.LOGI_ANCHOR, character_id=f.main_of(self.pilot).character_id)
        self.assertTrue(OperationRoleAssignment.objects.filter(operation=closed).exists())

    def test_detail_page_hides_record_forms_for_other_statuses(self):
        cancelled = f.create_operation(self.fc, status=Status.CANCELLED)
        track(cancelled, self.pilot)
        closed = closed_op(self.fc, timezone.now() - timedelta(hours=3))
        track(closed, self.pilot)

        hidden = self.client.get(reverse("fleetops:operation_detail", args=[cancelled.uuid]))
        shown = self.client.get(reverse("fleetops:operation_detail", args=[closed.uuid]))

        self.assertNotContains(hidden, reverse("fleetops:add_manual_attendance", args=[cancelled.uuid]))
        self.assertNotContains(hidden, reverse("fleetops:add_operation_role", args=[cancelled.uuid]))
        self.assertContains(hidden, "Special roles can only be assigned on Active or Closed fleets.")
        self.assertContains(shown, reverse("fleetops:add_manual_attendance", args=[closed.uuid]))
        self.assertContains(shown, reverse("fleetops:add_operation_role", args=[closed.uuid]))


# ---------------------------------------------------------------------------
# Start fleet preview
# ---------------------------------------------------------------------------


class PreviewErrorTests(TestCase):
    def test_render_failure_returns_a_json_error_without_details(self):
        fc = f.create_user("fc", perms=f.FC_PERMS)
        fleet_type = f.fleet_type()
        self.client.force_login(fc)
        error = RuntimeError(f"Failed posting to https://discord.com/api/webhooks/1/{WEBHOOK_SECRET}")

        with mock.patch("fleetops.views.render_messages", side_effect=error), self.assertLogs(
            "fleetops.views", "ERROR"
        ) as logs:
            response = self.client.post(
                reverse("fleetops:preview_fleet"),
                {
                    "request_id": str(uuid.uuid4()),
                    "operation_mode": "full",
                    "fc_character_id": str(f.main_of(fc).character_id),
                    "fleet_type": str(fleet_type.pk),
                    "formup": "Jita",
                },
            )

        self.assertEqual(response.status_code, 400)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertIn("could not be rendered", body["error"])
        self.assertNotIn(WEBHOOK_SECRET, response.content.decode())
        log = "\n".join(logs.output)
        self.assertIn("Message preview failed (RuntimeError)", log)
        self.assertIn("Traceback", log)
        self.assertNotIn(WEBHOOK_SECRET, log)
        self.assertTrue(all(record.exc_info is None for record in logs.records))
