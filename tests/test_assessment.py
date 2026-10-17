# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the monthly targets and the statuses derived from them."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from fleetops.services.assessment import (
    STATUS_LABELS,
    MonthTarget,
    competition_ranks,
    corporation_status,
    member_status,
    month_target,
    rank_of,
    status_label,
)
from fleetops.services.periods import month_ref

from . import factories as f

UTC = dt_timezone.utc

# 2026-10-12 19:00 UTC: October 2026 is running, September is finished, November has not begun.
NOW = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)
RUNNING = month_ref(2026, 10, NOW)
FINISHED = month_ref(2026, 9, NOW)
FUTURE = month_ref(2026, 11, NOW)


def target(average=None, minimum=1):
    return MonthTarget(average=None if average is None else Decimal(str(average)), minimum=minimum)


class MonthTargetTests(TestCase):
    def test_no_rows_means_no_target_and_nothing_to_assess(self):
        result = month_target(2026, 10)
        self.assertIsNone(result.average)
        self.assertEqual(result.minimum, 0)
        self.assertIsNone(result.rule)

    def test_an_earlier_row_applies(self):
        rule = f.statistics_target(2026, 3, "2.50", minimum=4)
        result = month_target(2026, 10)
        self.assertEqual(result.average, Decimal("2.50"))
        self.assertEqual(result.minimum, 4)
        self.assertEqual(result.rule, rule)

    def test_the_exact_month_applies(self):
        f.statistics_target(2026, 10, "3.00", minimum=2)
        self.assertEqual(month_target(2026, 10).average, Decimal("3.00"))

    def test_a_later_row_does_not_apply_backwards(self):
        f.statistics_target(2026, 11, "3.00", minimum=2)
        result = month_target(2026, 10)
        self.assertIsNone(result.average)
        self.assertEqual(result.minimum, 0)
        self.assertIsNone(result.rule)

    def test_the_latest_row_up_to_the_month_wins(self):
        f.statistics_target(2026, 1, "1.00", minimum=1)
        f.statistics_target(2026, 6, "2.00", minimum=3)
        f.statistics_target(2026, 12, "9.00", minimum=9)
        self.assertEqual(month_target(2026, 5).average, Decimal("1.00"))
        self.assertEqual(month_target(2026, 6).average, Decimal("2.00"))
        self.assertEqual(month_target(2026, 11).average, Decimal("2.00"))
        self.assertEqual(month_target(2026, 12).average, Decimal("9.00"))

    def test_december_carries_over_into_january(self):
        f.statistics_target(2026, 12, "2.00", minimum=2)
        result = month_target(2027, 1)
        self.assertEqual(result.average, Decimal("2.00"))
        self.assertEqual(result.minimum, 2)

    def test_an_earlier_year_applies_in_any_month_of_the_later_year(self):
        f.statistics_target(2025, 11, "2.00")
        self.assertEqual(month_target(2026, 1).average, Decimal("2.00"))
        self.assertEqual(month_target(2026, 12).average, Decimal("2.00"))
        self.assertIsNone(month_target(2025, 10).average)

    def test_a_month_before_the_first_row_is_not_assessed(self):
        f.statistics_target(2026, 6, "2.00", minimum=3)
        before = month_target(2026, 5)

        self.assertIsNone(before.rule)
        self.assertEqual((before.average, before.minimum), (None, 0))
        for ref in (RUNNING, FINISHED):
            with self.subTest(ref=ref.label):
                self.assertEqual(member_status(0, before, ref), "none")
                self.assertEqual(corporation_status(0, 5, before, ref), "none")

    def test_a_row_without_target_still_sets_the_minimum(self):
        f.statistics_target(2026, 1, None, minimum=0)
        result = month_target(2026, 10)
        self.assertIsNone(result.average)
        self.assertEqual(result.minimum, 0)

    def test_a_target_of_zero_is_a_target(self):
        f.statistics_target(2026, 1, "0")
        self.assertEqual(month_target(2026, 10).average, Decimal("0.00"))


class CorporationStatusTests(SimpleTestCase):
    def test_no_target_has_no_status(self):
        for ref in (RUNNING, FINISHED):
            with self.subTest(ref=ref.label):
                self.assertEqual(corporation_status(50, 10, target(), ref), "none")

    def test_no_target_wins_over_no_members(self):
        self.assertEqual(corporation_status(0, 0, target(), FINISHED), "none")

    def test_no_members(self):
        self.assertEqual(corporation_status(0, 0, target("2.50"), FINISHED), "no_members")
        self.assertEqual(corporation_status(7, 0, target("2.50"), RUNNING), "no_members")

    def test_exact_arithmetic_without_rounding(self):
        # 2.50 x 2 mains = 5.00
        self.assertEqual(corporation_status(5, 2, target("2.50"), FINISHED), "met")
        self.assertEqual(corporation_status(4, 2, target("2.50"), FINISHED), "missed")
        # 0.33 x 3 mains = 0.99: one PAP reaches it, no rounding of the average is involved.
        self.assertEqual(corporation_status(1, 3, target("0.33"), FINISHED), "met")
        self.assertEqual(corporation_status(10, 3, target("3.34"), FINISHED), "missed")
        self.assertEqual(corporation_status(11, 3, target("3.34"), FINISHED), "met")

    def test_reaching_the_target_exactly_is_met(self):
        self.assertEqual(corporation_status(20, 10, target("2.00"), FINISHED), "met")
        self.assertEqual(corporation_status(19, 10, target("2.00"), FINISHED), "missed")

    def test_a_target_of_zero_is_always_met(self):
        self.assertEqual(corporation_status(0, 4, target("0.00"), FINISHED), "met")
        self.assertEqual(corporation_status(0, 4, target("0.00"), RUNNING), "met")

    def test_met_is_green_in_the_running_month_too(self):
        self.assertEqual(corporation_status(30, 10, target("2.00"), RUNNING), "met")

    def test_running_month_below_target_is_in_progress(self):
        self.assertEqual(corporation_status(5, 10, target("2.00"), RUNNING), "in_progress")

    def test_finished_month_below_target_is_missed(self):
        self.assertEqual(corporation_status(5, 10, target("2.00"), FINISHED), "missed")

    def test_future_month_has_no_status(self):
        self.assertEqual(corporation_status(0, 10, target("2.00"), FUTURE), "none")
        self.assertEqual(corporation_status(0, 10, target("0.00"), FUTURE), "none")

    def test_month_edges_follow_eve_time(self):
        last_second = month_ref(2026, 9, datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC))
        first_second = month_ref(2026, 9, datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))
        self.assertEqual(corporation_status(0, 5, target("1.00"), last_second), "in_progress")
        self.assertEqual(corporation_status(0, 5, target("1.00"), first_second), "missed")

    def test_accepts_a_float_target_without_binary_noise(self):
        self.assertEqual(corporation_status(3, 10, MonthTarget(average=0.3, minimum=1), FINISHED), "met")


class MemberStatusTests(SimpleTestCase):
    def test_reaching_the_minimum_exactly_is_ok(self):
        self.assertEqual(member_status(3, target(minimum=3), FINISHED), "ok")
        self.assertEqual(member_status(3, target(minimum=3), RUNNING), "ok")

    def test_one_below_the_minimum(self):
        self.assertEqual(member_status(2, target(minimum=3), FINISHED), "below")
        self.assertEqual(member_status(2, target(minimum=3), RUNNING), "below_so_far")

    def test_zero_pap_is_below_a_minimum_of_one(self):
        self.assertEqual(member_status(0, target(), FINISHED), "below")
        self.assertEqual(member_status(0, target(), RUNNING), "below_so_far")

    def test_a_minimum_of_zero_lists_nobody(self):
        for ref in (RUNNING, FINISHED):
            with self.subTest(ref=ref.label):
                self.assertEqual(member_status(0, target(minimum=0), ref), "none")
                self.assertEqual(member_status(5, target(minimum=0), ref), "none")

    def test_future_month_has_no_status(self):
        self.assertEqual(member_status(0, target(minimum=3), FUTURE), "none")


class CompetitionRanksTests(SimpleTestCase):
    def test_ties_share_a_rank_and_skip_the_next(self):
        ranks = competition_ranks([(1, 30), (2, 20), (3, 20), (4, 10)])
        self.assertEqual(ranks, {1: 1, 2: 2, 3: 2, 4: 4})

    def test_three_way_tie_at_the_top(self):
        self.assertEqual(competition_ranks([(1, 9), (2, 9), (3, 9), (4, 1)]), {1: 1, 2: 1, 3: 1, 4: 4})

    def test_members_without_pap_have_no_rank(self):
        self.assertEqual(competition_ranks([(1, 5), (2, 0), (3, 0)]), {1: 1})

    def test_input_order_does_not_matter(self):
        self.assertEqual(competition_ranks([(4, 10), (2, 20), (1, 30), (3, 20)]), {1: 1, 2: 2, 3: 2, 4: 4})

    def test_empty(self):
        self.assertEqual(competition_ranks([]), {})

    def test_accepts_a_generator(self):
        self.assertEqual(competition_ranks((user, pap) for user, pap in [(1, 2), (2, 1)]), {1: 1, 2: 2})


class RankOfTests(SimpleTestCase):
    def test_one_plus_the_number_of_members_with_more_pap(self):
        paps = [30, 20, 20, 10, 0]
        self.assertEqual(rank_of(30, paps), 1)
        self.assertEqual(rank_of(20, paps), 2)
        self.assertEqual(rank_of(10, paps), 4)

    def test_no_pap_has_no_rank(self):
        self.assertIsNone(rank_of(0, [30, 20, 0]))

    def test_agrees_with_competition_ranks(self):
        totals = [(1, 12), (2, 7), (3, 7), (4, 7), (5, 3), (6, 0)]
        ranks = competition_ranks(totals)
        paps = [pap for _user, pap in totals]
        for user_id, pap in totals:
            with self.subTest(user=user_id):
                self.assertEqual(rank_of(pap, paps), ranks.get(user_id))

    def test_accepts_a_generator(self):
        self.assertEqual(rank_of(5, (pap for pap in [9, 5, 1])), 2)


class StatusLabelTests(SimpleTestCase):
    def test_labels(self):
        self.assertEqual(status_label("met"), "Target met")
        self.assertEqual(status_label("missed"), "Below target")
        self.assertEqual(status_label("in_progress"), "Not reached yet")
        self.assertEqual(status_label("no_members"), "No members")
        self.assertEqual(status_label("ok"), "OK")
        self.assertEqual(status_label("below"), "Below minimum")
        self.assertEqual(status_label("below_so_far"), "Below minimum so far")

    def test_no_status_has_no_label(self):
        self.assertEqual(status_label("none"), "")
        self.assertEqual(status_label("unknown"), "")

    def test_every_status_the_assessment_returns_is_labelled(self):
        returned = {
            corporation_status(total, mains, target("2.00"), ref)
            for total in (0, 100)
            for mains in (0, 5)
            for ref in (RUNNING, FINISHED, FUTURE)
        } | {member_status(pap, target(minimum=minimum), ref) for pap in (0, 9) for minimum in (0, 3) for ref in (RUNNING, FINISHED, FUTURE)}
        self.assertEqual(returned - {"none"} - set(STATUS_LABELS), set())
