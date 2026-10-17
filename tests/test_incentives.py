# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC incentive periods: rebuilding, the review and finalize states, the incentive page and the switch."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.db.models import QuerySet
from django.test import TestCase
from django.urls import reverse

from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetType,
    IncentivePeriod,
    MonthlyFCStatistic,
    OperationRoleAssignment,
    ReviewStatus,
)
from fleetops.services.incentives import finalize_period, rebuild_period, set_waiver, unlock_period
from fleetops.services.operations import create_manual_fleet

from . import factories as f

UTC = dt_timezone.utc
Status = FleetOperation.Status
Role = OperationRoleAssignment.Role
PeriodStatus = IncentivePeriod.Status


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def closed_op(fc_user, started_at, *, type_obj=None, **extra):
    return f.create_operation(fc_user, status=Status.CLOSED, started_at=started_at, type_obj=type_obj, **extra)


def add_role(operation, user, *, character=None, role=Role.BACKSEAT_FC, grants_fc_credit=True):
    character = character or f.main_of(user)
    main = f.main_of(user)
    return OperationRoleAssignment.objects.create(
        operation=operation,
        role=role,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id,
        main_character_name=main.character_name,
        auth_user=user,
        corporation_id=main.corporation_id,
        corporation_name=main.corporation_name,
        grants_fc_credit=grants_fc_credit,
    )


def stat_for(period, user):
    return MonthlyFCStatistic.objects.get(period=period, fc_user=user)


def audit_actions(obj):
    return list(
        AuditLog.objects.filter(object_type=obj.__class__.__name__, object_id=str(obj.pk))
        .order_by("pk")
        .values_list("action", flat=True)
    )


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class IncentiveRebuildTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        self.fc3 = f.create_user("fc3", perms=f.FC_PERMS)
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1000, minimum_fleets=2)

    def test_rebuild_moves_period_to_review_with_policy_snapshot(self):
        closed_op(self.fc1, utc(2026, 3, 2))
        rebuild_period(self.period)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertEqual(self.period.policy_snapshot["minimum_fleets"], 2)
        self.assertEqual(self.period.policy_snapshot["fleet_type_weights"][str(self.stratop.pk)], "2.00")

    def test_eligibility_threshold_and_payout_split(self):
        closed_op(self.fc1, utc(2026, 3, 2), type_obj=self.roam)
        closed_op(self.fc1, utc(2026, 3, 3), type_obj=self.roam)
        closed_op(self.fc1, utc(2026, 3, 4), type_obj=self.stratop)
        closed_op(self.fc2, utc(2026, 3, 5), type_obj=self.roam)
        closed_op(self.fc2, utc(2026, 3, 6), type_obj=self.roam)
        closed_op(self.fc3, utc(2026, 3, 7), type_obj=FleetType.objects.create(name="Heavy", point_weight=Decimal("3.00")))

        rebuild_period(self.period)

        fc1, fc2, fc3 = (stat_for(self.period, u) for u in (self.fc1, self.fc2, self.fc3))
        self.assertEqual((fc1.fleet_count, fc1.total_points, fc1.eligible), (3, Decimal("4.00"), True))
        self.assertEqual((fc2.fleet_count, fc2.total_points, fc2.eligible), (2, Decimal("2.00"), True))
        self.assertEqual((fc3.fleet_count, fc3.total_points, fc3.eligible), (1, Decimal("3.00"), False))
        self.assertEqual((fc1.calculated_payout, fc2.calculated_payout, fc3.calculated_payout), (667, 333, 0))
        self.assertEqual(fc1.final_payout, fc1.calculated_payout)

    def test_once_eligible_all_fleets_of_the_month_count(self):
        for day in (1, 9, 17, 25):
            closed_op(self.fc1, utc(2026, 3, day), type_obj=self.stratop)
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual(row.fleet_count, 4)
        self.assertEqual(row.total_points, Decimal("8.00"))
        self.assertEqual(row.fleet_type_breakdown, {"StratOp": {"count": 4, "points": 8.0, "toward_minimum": 4}})

    def test_exactly_minimum_fleets_is_eligible(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        closed_op(self.fc1, utc(2026, 3, 2))
        closed_op(self.fc2, utc(2026, 3, 3))
        rebuild_period(self.period)
        self.assertTrue(stat_for(self.period, self.fc1).eligible)
        self.assertFalse(stat_for(self.period, self.fc2).eligible)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 1000)

    def test_point_weight_snapshot_is_used(self):
        closed_op(self.fc1, utc(2026, 3, 1), type_obj=self.stratop)
        closed_op(self.fc1, utc(2026, 3, 2), type_obj=self.stratop)
        self.stratop.point_weight = Decimal("10.00")
        self.stratop.save()

        rebuild_period(self.period)

        self.assertEqual(stat_for(self.period, self.fc1).total_points, Decimal("4.00"))

    def test_draft_and_cancelled_fleets_are_excluded(self):
        f.create_operation(self.fc1, status=Status.DRAFT, started_at=utc(2026, 3, 1))
        f.create_operation(self.fc1, status=Status.CANCELLED, started_at=utc(2026, 3, 2))
        closed_op(self.fc1, utc(2026, 3, 3))
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual(row.fleet_count, 1)
        self.assertFalse(row.eligible)

    def test_fc_with_only_draft_or_cancelled_fleets_gets_no_row(self):
        f.create_operation(self.fc2, status=Status.CANCELLED, started_at=utc(2026, 3, 2))
        rebuild_period(self.period)
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.fc2).exists())

    def test_only_closed_fleets_count(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        for status in (Status.ACTIVE, Status.STARTING, Status.ENDING, Status.ERROR):
            f.create_operation(self.fc1, status=status, started_at=utc(2026, 3, 2))
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual(row.fleet_count, 1)
        self.assertFalse(row.eligible)

    def test_fleets_outside_the_month_are_excluded(self):
        closed_op(self.fc1, utc(2026, 2, 28, 23, 59, 59))
        closed_op(self.fc1, utc(2026, 3, 1, 0, 0, 0))
        closed_op(self.fc1, utc(2026, 3, 31, 23, 59, 59))
        closed_op(self.fc1, utc(2026, 4, 1, 0, 0, 0))
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc1).fleet_count, 2)

    def test_manual_fleets_count_once_approved(self):
        for day in (5, 6):
            create_manual_fleet(
                user=self.fc1,
                cleaned_data={
                    "started_at": utc(2026, 3, day, 19),
                    "ended_at": utc(2026, 3, day, 21),
                    "fleet_type": self.stratop,
                    "formup": "Amarr",
                },
            )
        rebuild_period(self.period)
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.fc1).exists())

        FleetOperation.objects.filter(is_manual=True).update(review_status=ReviewStatus.APPROVED)
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertEqual((row.fleet_count, row.total_points, row.eligible), (2, Decimal("4.00"), True))
        self.assertEqual(row.calculated_payout, 1000)

    def test_special_role_credit_counts_once_per_fleet(self):
        op1 = closed_op(self.fc1, utc(2026, 3, 1), type_obj=self.stratop)
        op2 = closed_op(self.fc1, utc(2026, 3, 2), type_obj=self.roam)
        add_role(op1, self.fc1, role=Role.BACKSEAT_FC)
        add_role(op1, self.fc2, role=Role.BACKSEAT_FC)
        add_role(op1, self.fc2, role=Role.LOGI_ANCHOR)
        add_role(op2, self.fc2, role=Role.SNOWFLAKE)
        add_role(op2, self.fc3, role=Role.LOGI_ANCHOR, grants_fc_credit=False)

        rebuild_period(self.period)

        fc1, fc2 = stat_for(self.period, self.fc1), stat_for(self.period, self.fc2)
        self.assertEqual((fc1.fleet_count, fc1.total_points), (2, Decimal("3.00")))
        self.assertEqual((fc2.fleet_count, fc2.total_points), (2, Decimal("3.00")))
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=self.period, fc_user=self.fc3).exists())
        self.assertEqual(fc1.calculated_payout + fc2.calculated_payout, 1000)

    def test_remainder_tie_break_goes_to_lowest_user_id(self):
        self.period.budget = 1001
        self.period.save()
        for user in (self.fc3, self.fc2, self.fc1):
            closed_op(user, utc(2026, 3, 1))
            closed_op(user, utc(2026, 3, 2))
        rebuild_period(self.period)
        payouts = {row.fc_user_id: row.calculated_payout for row in self.period.fc_statistics.all()}
        lowest = min(payouts)
        self.assertEqual(payouts[lowest], 335)
        self.assertEqual(sorted(v for k, v in payouts.items() if k != lowest), [333, 333])

    def test_ineligible_points_do_not_dilute_eligible_payouts(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        closed_op(self.fc1, utc(2026, 3, 2))
        heavy = FleetType.objects.create(name="Heavy", point_weight=Decimal("50.00"))
        closed_op(self.fc2, utc(2026, 3, 3), type_obj=heavy)
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 1000)
        self.assertEqual(stat_for(self.period, self.fc2).calculated_payout, 0)

    def test_rebuild_is_idempotent_and_drops_stale_rows(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        op = closed_op(self.fc2, utc(2026, 3, 2))
        rebuild_period(self.period)
        rebuild_period(self.period)
        self.assertEqual(self.period.fc_statistics.count(), 2)

        op.status = Status.CANCELLED
        op.save()
        rebuild_period(self.period)

        self.assertEqual(list(self.period.fc_statistics.values_list("fc_user_id", flat=True)), [self.fc1.pk])

    def test_rebuild_refuses_finalized_period(self):
        closed_op(self.fc1, utc(2026, 3, 1))
        rebuild_period(self.period)
        finalize_period(self.period, self.fc1)
        closed_op(self.fc1, utc(2026, 3, 2))

        with self.assertRaises(ValueError):
            rebuild_period(self.period)

        self.assertEqual(stat_for(self.period, self.fc1).fleet_count, 1)


class IncentiveStateMachineTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True)
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=900, minimum_fleets=1)
        weighted = f.fleet_type("StratOp", "2.00")
        closed_op(self.fc1, utc(2026, 3, 1), type_obj=weighted)
        closed_op(self.fc2, utc(2026, 3, 2), type_obj=f.fleet_type("Roam", "1.00"))

    def test_open_period_cannot_be_finalized(self):
        with self.assertRaises(ValueError):
            finalize_period(self.period, self.manager)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)

    def test_review_period_can_be_finalized(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)
        self.assertEqual(self.period.finalized_by, self.manager)
        self.assertIsNotNone(self.period.finalized_at)

    def test_finalized_period_cannot_be_finalized_again(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        with self.assertRaises(ValueError):
            finalize_period(self.period, self.manager)

    def test_unlock_returns_to_review_and_allows_recalculation(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        unlock_period(self.period)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertIsNone(self.period.finalized_at)
        self.assertIsNone(self.period.finalized_by)
        closed_op(self.fc2, utc(2026, 3, 9))
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc2).fleet_count, 2)

    def test_unlock_only_applies_to_finalized_periods(self):
        try:
            unlock_period(self.period)
        except ValueError:
            pass
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)

    def test_waiver_recalculates_immediately(self):
        rebuild_period(self.period)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 600)
        self.assertEqual(stat_for(self.period, self.fc2).calculated_payout, 300)

        row = set_waiver(self.period, self.fc1.pk, True)

        self.assertTrue(row.waived)
        self.assertEqual(row.calculated_payout, 0)
        self.assertEqual(stat_for(self.period, self.fc2).calculated_payout, 900)

        set_waiver(self.period, self.fc1.pk, False)
        self.assertEqual(stat_for(self.period, self.fc1).calculated_payout, 600)

    def test_waiver_survives_recalculation(self):
        rebuild_period(self.period)
        set_waiver(self.period, self.fc1.pk, True)
        rebuild_period(self.period)
        row = stat_for(self.period, self.fc1)
        self.assertTrue(row.waived)
        self.assertEqual(row.final_payout, 0)

    def test_waiver_blocked_when_finalized(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        with self.assertRaises(ValueError):
            set_waiver(self.period, self.fc1.pk, True)
        self.assertFalse(stat_for(self.period, self.fc1).waived)

    def test_waiver_requires_existing_statistic(self):
        with self.assertRaises(ValueError):
            set_waiver(self.period, self.fc1.pk, True)
        rebuild_period(self.period)
        outsider = f.create_user("outsider")
        with self.assertRaises(ValueError):
            set_waiver(self.period, outsider.pk, True)


class IncentiveViewTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True, incentive_minimum_fleets=4)
        self.manager = f.create_user("manager", perms=f.MEMBER_PERMS + ["fleetops.manage_incentives"])
        self.fc1 = f.create_user("fc1", perms=f.FC_PERMS)
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS)
        closed_op(self.fc1, utc(2026, 3, 1), type_obj=f.fleet_type("StratOp", "2.00"))
        closed_op(self.fc2, utc(2026, 3, 2), type_obj=f.fleet_type("Roam", "1.00"))
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=300, minimum_fleets=1)
        self.client.force_login(self.manager)

    def post(self, name, *args, data=None):
        return self.client.post(reverse(f"fleetops:{name}", args=args), data or {})

    def test_review_page_for_explicit_month(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period"], self.period)

    def test_review_page_offers_period_creation_with_default_minimum(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 4})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["period"])
        self.assertEqual(response.context["form"].initial["minimum_fleets"], 4)

    def test_create_period_is_audited(self):
        response = self.client.post(
            reverse("fleetops:incentive_review") + "?year=2026&month=4",
            {"create_period": "1", "year": 2026, "month": 4, "budget": 5_000_000_000, "minimum_fleets": 3},
        )
        period = IncentivePeriod.objects.get(year=2026, month=4)
        self.assertRedirects(response, reverse("fleetops:incentive_review") + "?year=2026&month=4", fetch_redirect_response=False)
        # A new period is calculated at once.
        self.assertEqual(period.status, PeriodStatus.REVIEW)
        self.assertIsNotNone(period.calculated_at)
        entry = AuditLog.objects.get(action="incentive.period_create")
        self.assertEqual(entry.actor, self.manager)
        self.assertEqual(entry.new_value, {"budget": 5_000_000_000, "minimum_fleets": 3})

    def test_duplicate_period_is_rejected(self):
        response = self.client.post(
            reverse("fleetops:incentive_review") + "?year=2026&month=3",
            {"create_period": "1", "year": 2026, "month": 3, "budget": 1, "minimum_fleets": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IncentivePeriod.objects.filter(year=2026, month=3).count(), 1)

    def test_recalculate_finalize_unlock_are_audited(self):
        self.post("incentive_recalculate", self.period.pk)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertEqual(self.period.fc_statistics.count(), 2)

        self.post("incentive_finalize", self.period.pk)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)
        self.assertEqual(self.period.finalized_by, self.manager)

        self.post("incentive_unlock", self.period.pk)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)

        self.assertEqual(
            audit_actions(self.period),
            ["incentive.recalculate", "incentive.finalize", "incentive.unlock"],
        )
        recalc = AuditLog.objects.get(action="incentive.recalculate")
        self.assertEqual((recalc.old_value, recalc.new_value), ({"status": "open"}, {"status": "review"}))
        self.assertTrue(all(e.actor == self.manager for e in AuditLog.objects.filter(action__startswith="incentive.")))

    def test_waiver_view_recalculates_and_audits(self):
        rebuild_period(self.period)

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertRedirects(response, reverse("fleetops:incentive_review") + "?year=2026&month=3", fetch_redirect_response=False)
        self.assertTrue(stat_for(self.period, self.fc1).waived)
        self.assertEqual(stat_for(self.period, self.fc2).final_payout, 300)
        entry = AuditLog.objects.get(action="incentive.waiver")
        self.assertEqual((entry.old_value, entry.new_value), ({"waived": False}, {"waived": True}))
        self.assertEqual(entry.object_type, "MonthlyFCStatistic")

        self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "0"})
        self.assertFalse(stat_for(self.period, self.fc1).waived)
        self.assertEqual(stat_for(self.period, self.fc1).final_payout, 200)

    def test_waiver_view_blocked_when_finalized(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(stat_for(self.period, self.fc1).waived)
        self.assertFalse(AuditLog.objects.filter(action="incentive.waiver").exists())

    def test_waiver_for_unknown_fc_is_404(self):
        rebuild_period(self.period)
        response = self.post("incentive_waiver", self.period.pk, self.manager.pk, data={"waived": "1"})
        self.assertEqual(response.status_code, 404)

    def test_incentive_pages_require_manage_incentives(self):
        self.client.force_login(f.create_user("lead", perms=f.FC_PERMS + ["fleetops.view_all_stats"]))
        self.assertEqual(self.client.get(reverse("fleetops:incentive_review")).status_code, 403)
        for name, args in [
            ("incentive_recalculate", (self.period.pk,)),
            ("incentive_finalize", (self.period.pk,)),
            ("incentive_unlock", (self.period.pk,)),
            ("incentive_waiver", (self.period.pk, self.fc1.pk)),
        ]:
            with self.subTest(view=name):
                self.assertEqual(self.post(name, *args).status_code, 403)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(MonthlyFCStatistic.objects.exists())

    def test_incentive_actions_require_post(self):
        for name in ("incentive_recalculate", "incentive_finalize", "incentive_unlock"):
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(f"fleetops:{name}", args=[self.period.pk])).status_code, 405)

    def test_recalculate_finalized_period_is_rejected_gracefully(self):
        rebuild_period(self.period)
        finalize_period(self.period, self.manager)
        self.client.raise_request_exception = False

        response = self.post("incentive_recalculate", self.period.pk)

        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.FINALIZED)

    def test_finalize_open_period_is_rejected_gracefully(self):
        self.client.raise_request_exception = False

        response = self.post("incentive_finalize", self.period.pk)

        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(AuditLog.objects.filter(action="incentive.finalize").exists())

    def test_unlock_open_period_does_nothing(self):
        self.client.raise_request_exception = False

        self.post("incentive_unlock", self.period.pk)

        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(AuditLog.objects.filter(action="incentive.unlock").exists())

    def test_waiver_after_fc_lost_all_fleets_does_not_crash(self):
        rebuild_period(self.period)
        FleetOperation.objects.filter(fc_user=self.fc1).update(status=Status.CANCELLED)
        self.client.raise_request_exception = False

        response = self.post("incentive_waiver", self.period.pk, self.fc1.pk, data={"waived": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(stat_for(self.period, self.fc2).final_payout, 300)


class IncentiveDisabledTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=False)
        self.manager = f.create_user("manager", perms=f.FC_LEAD_PERMS)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        closed_op(self.fc, utc(2026, 3, 1))
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=300, minimum_fleets=1)
        self.client.force_login(self.manager)

    def test_enabled_flag_is_persisted(self):
        self.assertFalse(f.settings().incentive_enabled)
        self.assertTrue(f.settings(incentive_enabled=True).incentive_enabled)

    def test_review_page_unavailable_when_disabled(self):
        response = self.client.get(reverse("fleetops:incentive_review"), {"year": 2026, "month": 3})
        self.assertIn(response.status_code, (302, 403, 404))

    def test_nothing_is_calculated_when_disabled(self):
        self.client.raise_request_exception = False
        self.client.post(reverse("fleetops:incentive_recalculate", args=[self.period.pk]))
        try:
            rebuild_period(self.period)
        except (ValueError, PermissionError):
            pass
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.OPEN)
        self.assertFalse(MonthlyFCStatistic.objects.exists())

    def test_navigation_hides_incentive_link_when_disabled(self):
        response = self.client.get(reverse("fleetops:my_statistics"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("fleetops:incentive_review"))

    def test_navigation_shows_incentive_link_when_enabled(self):
        f.settings(incentive_enabled=True)
        response = self.client.get(reverse("fleetops:my_statistics"))
        self.assertContains(response, reverse("fleetops:incentive_review"))


class IncentivePeriodPageTests(TestCase):
    def setUp(self):
        f.settings(incentive_enabled=True, incentive_minimum_fleets=4)
        self.manager = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.manager)

    def page_url(self, year=2026, month=3):
        return reverse("fleetops:incentive_review") + f"?year={year}&month={month}"

    def create(self, **data):
        return self.client.post(self.page_url(), {"create_period": "1", **data})

    def test_create_form_has_only_budget_and_minimum(self):
        response = self.client.get(self.page_url())

        form = response.context["form"]
        self.assertEqual(list(form.fields), ["budget", "minimum_fleets"])
        self.assertEqual(form.initial["minimum_fleets"], 4)
        self.assertContains(response, "Create 2026-03")
        self.assertContains(response, 'action="/fleetops/incentives/?year=2026&amp;month=3"')

    def test_create_takes_the_month_from_the_page(self):
        response = self.create(year=2025, month=7, budget=5_000_000, minimum_fleets=2)

        period = IncentivePeriod.objects.get()
        self.assertEqual((period.year, period.month, period.budget, period.minimum_fleets), (2026, 3, 5_000_000, 2))
        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        entry = AuditLog.objects.get(action="incentive.period_create")
        self.assertEqual(entry.new_value, {"budget": 5_000_000, "minimum_fleets": 2})

    def test_create_needs_the_month_of_the_page(self):
        for query in ("", "?year=2026", "?year=99999&month=1", "?year=2026&month=13"):
            with self.subTest(query=query):
                response = self.client.post(
                    reverse("fleetops:incentive_review") + query,
                    {"create_period": "1", "year": 2026, "month": 3, "budget": 1, "minimum_fleets": 1},
                )

                self.assertRedirects(response, reverse("fleetops:incentive_review"), fetch_redirect_response=False)
                self.assertIn("Choose the month of the new incentive period first.", flashed(response))
        self.assertFalse(IncentivePeriod.objects.exists())

    def test_create_errors_are_shown(self):
        response = self.create(budget=-1, minimum_fleets=0)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 0.")
        self.assertContains(response, "Ensure this value is greater than or equal to 1.")
        self.assertFalse(IncentivePeriod.objects.exists())

    def test_create_race_returns_the_existing_period(self):
        IncentivePeriod.objects.create(year=2026, month=3, budget=7)
        real_first = QuerySet.first
        looked_up = []

        def not_there_yet(queryset):
            # Another manager creates the month right after this request looked for it.
            if queryset.model is IncentivePeriod and not looked_up:
                looked_up.append(True)
                return None
            return real_first(queryset)

        with mock.patch.object(QuerySet, "first", not_there_yet):
            response = self.create(budget=1, minimum_fleets=1)

        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        self.assertEqual(IncentivePeriod.objects.get().budget, 7)
        self.assertIn("was created meanwhile", flashed(response)[0])
        self.assertFalse(AuditLog.objects.filter(action="incentive.period_create").exists())

    def test_create_for_an_existing_month_changes_nothing(self):
        IncentivePeriod.objects.create(year=2026, month=3, budget=7)

        response = self.create(budget=1, minimum_fleets=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(IncentivePeriod.objects.get().budget, 7)
        self.assertIn("already exists", flashed(response)[0])

    def test_edit_budget_and_minimum(self):
        period = IncentivePeriod.objects.create(year=2026, month=3, budget=100, minimum_fleets=3)
        page = self.client.get(self.page_url())
        self.assertContains(page, "Edit budget / minimum fleets")

        response = self.client.post(
            reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 250, "minimum_fleets": 5}
        )

        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        period.refresh_from_db()
        self.assertEqual((period.budget, period.minimum_fleets), (250, 5))
        entry = AuditLog.objects.get(action="incentive.period_update")
        self.assertEqual(entry.old_value, {"budget": 100, "minimum_fleets": 3})
        self.assertEqual(entry.new_value, {"budget": 250, "minimum_fleets": 5})
        self.assertEqual(flashed(response), ["Budget and minimum fleets saved; the month was recalculated."])
        self.assertEqual(period.status, PeriodStatus.REVIEW)
        self.assertIsNotNone(period.calculated_at)

    def test_edit_errors_are_shown_and_nothing_changes(self):
        period = IncentivePeriod.objects.create(year=2026, month=3, budget=100, minimum_fleets=3)

        response = self.client.post(
            reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 999, "minimum_fleets": 0}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ensure this value is greater than or equal to 1.")
        self.assertContains(response, 'id="period-budget">100 ISK</strong>')
        period.refresh_from_db()
        self.assertEqual((period.budget, period.minimum_fleets), (100, 3))
        self.assertFalse(AuditLog.objects.filter(action="incentive.period_update").exists())

    def test_finalized_periods_are_not_editable(self):
        period = IncentivePeriod.objects.create(
            year=2026, month=3, budget=100, minimum_fleets=3, status=IncentivePeriod.Status.FINALIZED
        )
        page = self.client.get(self.page_url())
        self.assertNotContains(page, "Edit budget / minimum fleets")

        response = self.client.post(
            reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 250, "minimum_fleets": 5}
        )

        self.assertIn("Unlock it before changing", flashed(response)[0])
        period.refresh_from_db()
        self.assertEqual(period.budget, 100)

    def test_update_needs_manage_incentives(self):
        period = IncentivePeriod.objects.create(year=2026, month=3)
        self.client.force_login(f.create_user(perms=f.FC_PERMS))

        response = self.client.post(reverse("fleetops:incentive_update", args=[period.pk]), {"budget": 1})

        self.assertEqual(response.status_code, 403)

    def test_pending_multipliers_and_not_counted_fleets_are_shown(self):
        start = datetime(2026, 3, 10, 18, 0, tzinfo=dt_timezone.utc)
        fc = f.create_user(perms=f.FC_PERMS)
        waiting = f.create_operation(fc, status=Status.CLOSED, started_at=start)
        excluded = f.create_operation(fc, status=Status.CLOSED, started_at=start)
        f.create_operation(fc, status=Status.CLOSED, started_at=start)

        with mock.patch(
            "fleetops.views.pending_multiplier_operations",
            return_value=FleetOperation.objects.filter(pk=waiting.pk),
        ), mock.patch(
            "fleetops.views.counted_operations", side_effect=lambda qs, **kwargs: qs.exclude(pk=excluded.pk)
        ):
            response = self.client.get(self.page_url())

        self.assertContains(response, "1 fleet in this month still waits for the FC")
        self.assertContains(response, reverse("fleetops:operation_detail", args=[waiting.uuid]))
        self.assertContains(response, "1 closed fleet in this month is not counted")
