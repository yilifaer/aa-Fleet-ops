# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC incentive periods that are not finalised follow the fleets of their month by themselves: every change to a fleet
recalculates its month after the commit, a finalised period never changes, and the page says when it was calculated."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    AuditLog,
    FleetOperation,
    IncentivePeriod,
    MonthlyFCStatistic,
    OperationRoleAssignment,
    ReviewStatus,
)
from fleetops.services import approvals, incentives
from fleetops.services.attendance import set_operation_attendance_multiplier
from fleetops.services.counting import set_stats_mode
from fleetops.services.incentives import (
    IncentiveError,
    months_of,
    numbers_differ,
    rebuild_period,
    refresh_months_after_commit,
)
from fleetops.services.operations import create_manual_fleet, end_fleet
from fleetops.services.roles import add_role_assignment, delete_role_assignment

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
PeriodStatus = IncentivePeriod.Status


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class RefreshTestCase(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.other_fc = f.create_user("other_fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        # A period in review, calculated once with one counted fleet of the first FC.
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=1)
        self.first = f.closed_operation(self.fc, utc(2026, 3, 5, 18), type_obj=self.stratop)
        rebuild_period(self.period)
        self.period.refresh_from_db()
        self.calculated_at = self.period.calculated_at

    def stat(self, user, period=None):
        return MonthlyFCStatistic.objects.filter(period=period or self.period, fc_user=user).first()

    def audits(self):
        return list(AuditLog.objects.filter(action="incentive.auto_recalculate").order_by("pk"))

    def run_callbacks(self):
        """The context that runs ``transaction.on_commit`` callbacks, as a commit would."""
        return self.captureOnCommitCallbacks(execute=True)


class TriggerTests(RefreshTestCase):
    def running_fleet(self, **extra):
        started = timezone.now() - timedelta(minutes=30)
        return f.create_operation(self.other_fc, started_at=started, type_obj=self.stratop, **extra)

    def test_a_period_is_not_touched_without_the_commit(self):
        self.assertEqual(self.stat(self.fc).fleet_count, 1)
        f.closed_operation(self.fc, utc(2026, 3, 6, 18), type_obj=self.stratop)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            refresh_months_after_commit((2026, 3))

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.stat(self.fc).fleet_count, 1)
        callbacks[0]()
        self.assertEqual(self.stat(self.fc).fleet_count, 2)

    def test_ending_a_fleet_updates_the_rows_and_the_time_of_the_calculation(self):
        fleet = f.create_operation(
            self.other_fc, started_at=utc(2026, 3, 9, 18), type_obj=self.stratop, tracking_enabled=True
        )
        self.assertIsNone(self.stat(self.other_fc))

        with self.run_callbacks(), mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(fleet, actor=self.other_fc)

        self.assertEqual(self.stat(self.other_fc).fleet_count, 1)
        self.assertEqual(self.stat(self.other_fc).total_points, Decimal("2.00"))
        self.period.refresh_from_db()
        self.assertGreater(self.period.calculated_at, self.calculated_at)
        entry = self.audits()[0]
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (self.other_fc, "IncentivePeriod", str(self.period.pk)))
        self.assertEqual(entry.reason, "Recalculated automatically after a fleet of this month changed.")
        self.assertNotIn(str(self.fc.pk), entry.old_value.keys() - entry.new_value.keys())

    def test_the_choice_of_the_multiplier_doubles_the_points(self):
        self.assertEqual(self.stat(self.fc).total_points, Decimal("2.00"))

        with self.run_callbacks():
            set_operation_attendance_multiplier(self.first, 2, actor=self.fc)

        self.assertEqual(self.stat(self.fc).total_points, Decimal("4.00"))
        entry = self.audits()[0]
        self.assertEqual(entry.old_value[str(self.fc.pk)]["total_points"], "2.00")
        self.assertEqual(entry.new_value[str(self.fc.pk)]["total_points"], "4.00")

    def test_approving_and_rejecting_a_manual_fleet_counts_and_uncounts_it(self):
        manual = f.create_operation(
            self.other_fc,
            status=Status.CLOSED,
            is_manual=True,
            esi_fleet_id=None,
            review_status=ReviewStatus.PENDING,
            started_at=utc(2026, 3, 12, 18),
            type_obj=self.stratop,
        )
        with self.run_callbacks():
            approvals.approve_operation(manual, actor=self.lead)
        self.assertEqual(self.stat(self.other_fc).fleet_count, 1)

        manual.review_status = ReviewStatus.APPROVED
        with self.run_callbacks():
            # Rejected fleets are only rejected from pending; a fleet sent to pending again is a new decision.
            FleetOperation.objects.filter(pk=manual.pk).update(review_status=ReviewStatus.PENDING)
            manual.refresh_from_db()
            approvals.reject_operation(manual, actor=self.lead, note="Wrong date")
        self.assertIsNone(self.stat(self.other_fc))

    def test_a_manual_fleet_counts_at_once_when_an_fc_lead_creates_it(self):
        with self.run_callbacks():
            create_manual_fleet(
                user=self.lead,
                cleaned_data={
                    "fleet_type": self.stratop,
                    "formup": "Jita",
                    "started_at": utc(2026, 3, 20, 18),
                    "ended_at": utc(2026, 3, 20, 19),
                    "attendance_multiplier": 1,
                },
            )

        self.assertEqual(self.stat(self.lead).fleet_count, 1)

    def test_the_statistics_mode_of_a_fleet_decides(self):
        with self.run_callbacks():
            set_stats_mode(self.first, "exclude", actor=self.lead, note="Test fleet")
        self.assertIsNone(self.stat(self.fc))

        with self.run_callbacks():
            set_stats_mode(self.first, "include", actor=self.lead, note="")
        self.assertEqual(self.stat(self.fc).fleet_count, 1)

    def test_a_credit_role_gives_the_pilot_the_fleet_and_removing_it_takes_it_away(self):
        pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        f.member_state(self.first, f.main_of(pilot), active=False)

        with self.run_callbacks():
            assignment = add_role_assignment(self.first, actor=self.fc, role="backseat_fc", character_id=f.main_of(pilot).character_id)
        self.assertEqual(self.stat(pilot).fleet_count, 1)

        with self.run_callbacks():
            delete_role_assignment(assignment, actor=self.fc)
        self.assertIsNone(self.stat(pilot))

    def test_a_role_without_credit_changes_nothing(self):
        pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        f.member_state(self.first, f.main_of(pilot), active=False)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            assignment = add_role_assignment(self.first, actor=self.fc, role="logi_anchor", character_id=f.main_of(pilot).character_id)
            delete_role_assignment(assignment, actor=self.fc)

        self.assertEqual(callbacks, [])

    def test_a_credit_role_on_the_fleet_page_view_refreshes_too(self):
        pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        f.member_state(self.first, f.main_of(pilot), active=False)
        self.client.force_login(self.fc)

        with self.run_callbacks():
            self.client.post(
                reverse("fleetops:add_operation_role", args=[self.first.uuid]),
                {"role": "backseat_fc", "character_id": str(f.main_of(pilot).character_id), "notes": ""},
            )

        self.assertEqual(self.stat(pilot).fleet_count, 1)

    def test_editing_the_times_updates_the_old_and_the_new_month(self):
        april = IncentivePeriod.objects.create(year=2026, month=4, budget=500, minimum_fleets=1)
        rebuild_period(april)
        self.client.force_login(self.lead)
        moved = utc(2026, 4, 3, 18)

        with self.run_callbacks():
            response = self.client.post(
                reverse("fleetops:edit_operation", args=[self.first.uuid]),
                {
                    "fleet_type": self.stratop.pk,
                    "doctrine_name": "",
                    "formup": "Jita",
                    "additional_message": "",
                    "started_at": moved.strftime("%Y-%m-%dT%H:%M"),
                    "ended_at": (moved + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.stat(self.fc))
        self.assertEqual(self.stat(self.fc, april).fleet_count, 1)

    def test_changing_the_fleet_type_changes_the_points(self):
        roam = f.fleet_type("Roam", "0.50")
        self.client.force_login(self.lead)
        start = self.first.started_at

        with self.run_callbacks():
            self.client.post(
                reverse("fleetops:edit_operation", args=[self.first.uuid]),
                {
                    "fleet_type": roam.pk,
                    "doctrine_name": "",
                    "formup": "Jita",
                    "additional_message": "",
                    "started_at": start.strftime("%Y-%m-%dT%H:%M"),
                    "ended_at": self.first.ended_at.strftime("%Y-%m-%dT%H:%M"),
                },
            )

        self.assertEqual(self.stat(self.fc).total_points, Decimal("0.50"))


class SafetyTests(RefreshTestCase):
    def finalize(self):
        incentives.finalize_period(self.period, self.lead)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)

    def test_a_finalized_period_never_changes(self):
        self.finalize()
        before = list(MonthlyFCStatistic.objects.filter(period=self.period).values_list("fc_user_id", "total_points"))
        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)

        with self.run_callbacks():
            set_operation_attendance_multiplier(self.first, 3, actor=self.fc)
            refresh_months_after_commit((2026, 3))

        self.assertEqual(list(MonthlyFCStatistic.objects.filter(period=self.period).values_list("fc_user_id", "total_points")), before)
        self.period.refresh_from_db()
        self.assertEqual(self.period.calculated_at, self.calculated_at)
        self.assertEqual(self.audits(), [])

    def test_a_rebuild_that_lost_the_race_with_the_finalising_does_nothing(self):
        # This copy of the period still says "review"; the row says it is finalised.
        stale_copy = IncentivePeriod.objects.get(pk=self.period.pk)
        self.finalize()
        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)

        self.assertIsNone(rebuild_period(stale_copy, automatic=True))
        with self.assertRaisesMessage(IncentiveError, "Finalized periods must be unlocked before recalculation."):
            rebuild_period(stale_copy)
        self.assertEqual(self.stat(self.fc).fleet_count, 1)

    def test_nothing_happens_when_the_incentives_are_switched_off(self):
        f.settings(incentive_enabled=False)
        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)

        with self.run_callbacks():
            refresh_months_after_commit((2026, 3))

        self.assertEqual(self.stat(self.fc).fleet_count, 1)
        self.assertIsNone(rebuild_period(self.period, automatic=True))
        with self.assertRaises(IncentiveError):
            rebuild_period(self.period)

    def test_a_month_without_a_period_is_skipped(self):
        with self.run_callbacks():
            refresh_months_after_commit((2026, 6), (2025, 1))

        self.assertEqual(IncentivePeriod.objects.count(), 1)

    def test_a_failing_recalculation_is_logged_and_does_not_break_the_action(self):
        fleet = f.create_operation(self.other_fc, started_at=utc(2026, 3, 9, 18), type_obj=self.stratop)

        # The callbacks run when the commit context ends, so the patch must still be in place then.
        with self.assertLogs("extensions.fleetops.services.incentives", level="ERROR") as logged, \
                mock.patch("fleetops.services.operations.sync_operation"), \
                mock.patch.object(incentives, "rebuild_period", side_effect=RuntimeError("secret detail")), \
                self.run_callbacks():
            operation = end_fleet(fleet, actor=self.other_fc)

        self.assertEqual(operation.status, Status.CLOSED)
        output = "\n".join(logged.output)
        self.assertIn("Incentive recalculation failed (RuntimeError)", output)
        self.assertNotIn("secret detail", output)

    def test_an_audit_entry_is_written_only_when_a_figure_changed(self):
        with self.run_callbacks():
            refresh_months_after_commit((2026, 3))
        self.assertEqual(self.audits(), [])

        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)
        with self.run_callbacks():
            refresh_months_after_commit((2026, 3))
        self.assertEqual(len(self.audits()), 1)
        # The time of the calculation moves either way.
        self.period.refresh_from_db()
        self.assertGreater(self.period.calculated_at, self.calculated_at)

    def test_waivers_and_budget_survive_an_automatic_rebuild(self):
        incentives.set_waiver(self.period, self.fc.pk, True)
        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)

        with self.run_callbacks():
            refresh_months_after_commit((2026, 3))

        row = self.stat(self.fc)
        self.assertEqual((row.fleet_count, row.waived, row.final_payout), (2, True, 0))

    def test_months_of_names_the_old_and_the_new_month(self):
        self.assertEqual(months_of(self.first), {(2026, 3)})
        self.assertEqual(months_of(self.first, previous_started_at=utc(2026, 2, 28, 23)), {(2026, 3), (2026, 2)})


class PageTests(RefreshTestCase):
    def setUp(self):
        super().setUp()
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.manager)
        self.url = reverse("fleetops:incentive_review") + "?year=2026&month=3"

    def test_the_page_shows_when_the_numbers_were_calculated(self):
        page = self.client.get(self.url)

        self.assertContains(page, 'id="period-calculated"')
        self.assertContains(page, "Last calculated")
        self.assertContains(page, "datetime=" + '"%s"' % self.calculated_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertNotContains(page, "Not calculated yet")

    def test_a_period_from_before_the_upgrade_says_it_was_not_calculated(self):
        IncentivePeriod.objects.filter(pk=self.period.pk).update(calculated_at=None)

        self.assertContains(self.client.get(self.url), "Not calculated yet")

    def test_no_warning_right_after_the_rebuild(self):
        page = self.client.get(self.url)

        self.assertFalse(page.context["numbers_stale"])
        self.assertNotContains(page, 'id="incentive-stale"')

    def test_a_change_no_trigger_sees_shows_the_warning_with_the_button(self):
        # Written with the ORM: no trigger runs.
        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)

        page = self.client.get(self.url)

        self.assertTrue(page.context["numbers_stale"])
        self.assertContains(page, "The numbers differ from the current FC statistics.")
        self.assertContains(page, reverse("fleetops:incentive_recalculate", args=[self.period.pk]))

        self.client.post(reverse("fleetops:incentive_recalculate", args=[self.period.pk]))
        self.assertNotContains(self.client.get(self.url), 'id="incentive-stale"')

    def test_a_finalized_period_says_its_numbers_are_kept(self):
        incentives.finalize_period(self.period, self.manager)
        f.closed_operation(self.fc, utc(2026, 3, 8, 18), type_obj=self.stratop)

        page = self.client.get(self.url)

        self.assertContains(page, "Finalized numbers are kept; the current FC statistics of this month differ.")
        self.assertNotContains(page, "The numbers differ from the current FC statistics.")

    def test_an_fc_who_left_the_member_alliances_is_noticed(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        self.assertFalse(numbers_differ(self.period, list(self.period.fc_statistics.all())))

        profile = self.fc.profile
        profile.main_character.alliance_id = f.FOREIGN_ALLIANCE[0]
        profile.main_character.save()

        self.assertTrue(numbers_differ(self.period, list(self.period.fc_statistics.all())))

    def test_a_new_period_is_calculated_at_once(self):
        f.closed_operation(self.fc, utc(2026, 4, 8, 18), type_obj=self.stratop)

        response = self.client.post(
            reverse("fleetops:incentive_review") + "?year=2026&month=4",
            {"create_period": "1", "budget": 5_000_000_000, "minimum_fleets": 1},
        )

        period = IncentivePeriod.objects.get(year=2026, month=4)
        self.assertEqual((period.status, period.fc_statistics.count()), (PeriodStatus.REVIEW, 1))
        self.assertEqual(period.fc_statistics.get().final_payout, 5_000_000_000)
        self.assertIsNotNone(period.calculated_at)
        page = self.client.get(response["Location"])
        self.assertContains(page, "5,000,000,000 ISK")

    def test_saving_the_budget_recalculates_at_once(self):
        response = self.client.post(
            reverse("fleetops:incentive_update", args=[self.period.pk]), {"budget": 4000, "minimum_fleets": 2}
        )

        self.assertEqual(response.status_code, 302)
        row = self.stat(self.fc)
        # One fleet, minimum two: not eligible any more, and no payout.
        self.assertEqual((row.eligible, row.final_payout), (False, 0))
        self.period.refresh_from_db()
        self.assertGreater(self.period.calculated_at, self.calculated_at)

    def test_isk_amounts_have_thousands_separators(self):
        IncentivePeriod.objects.filter(pk=self.period.pk).update(budget=5_000_000_000)
        rebuild_period(self.period)

        page = self.client.get(self.url)

        self.assertContains(page, "5,000,000,000 ISK")
        self.assertNotContains(page, "5000000000 ISK")

    def test_the_pending_choice_hint_no_longer_asks_for_a_recalculation(self):
        FleetOperation.objects.filter(pk=self.first.pk).update(multiplier_pending=True)

        page = self.client.get(self.url)

        self.assertContains(page, "They count with 1x until the multiplier is chosen.")
        self.assertNotContains(page, "recalculate afterwards")


class NoWarningAfterATriggerTests(RefreshTestCase):
    """Whatever recalculated the month, the page then agrees with the FC statistics (no warning)."""

    def setUp(self):
        super().setUp()
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.manager)
        self.url = reverse("fleetops:incentive_review") + "?year=2026&month=3"

    def assertAgrees(self):
        page = self.client.get(self.url)
        self.assertFalse(page.context["numbers_stale"])
        self.assertNotContains(page, 'id="incentive-stale"')

    def test_after_every_trigger(self):
        pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        f.member_state(self.first, f.main_of(pilot), active=False)
        manual = f.create_operation(
            self.other_fc,
            status=Status.CLOSED,
            is_manual=True,
            esi_fleet_id=None,
            review_status=ReviewStatus.PENDING,
            started_at=utc(2026, 3, 12, 18),
            type_obj=self.stratop,
        )
        actions = [
            lambda: set_operation_attendance_multiplier(self.first, 3, actor=self.fc),
            lambda: approvals.approve_operation(manual, actor=self.lead),
            lambda: set_stats_mode(self.first, "exclude", actor=self.lead, note="Test"),
            lambda: set_stats_mode(self.first, "auto", actor=self.lead, note=""),
            lambda: add_role_assignment(self.first, actor=self.fc, role="backseat_fc", character_id=f.main_of(pilot).character_id),
        ]
        for number, action in enumerate(actions):
            with self.subTest(action=number):
                with self.run_callbacks():
                    action()
                self.assertAgrees()
        assignment = OperationRoleAssignment.objects.get()
        with self.run_callbacks():
            delete_role_assignment(assignment, actor=self.fc)
        self.assertAgrees()
