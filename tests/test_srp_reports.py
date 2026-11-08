# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""STAT-1 to STAT-7 and CAT-10: periods, the report page, the dimensions and the files.

The tests name their dates; none of them reads the clock (the presets are given a fixed ``now``).
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.exceptions import BadRequest, PermissionDenied
from django.db import connection
from django.http import Http404
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from fleetops.models import AuditLog, EveEntityName, SrpClaim, SrpClaimEvent, SrpPaymentBatch
from fleetops.services import export_datasets as datasets
from fleetops.services import periods
from fleetops.services.periods import MAX_REPORT_DAYS, PeriodError, parse_period
from fleetops.srp import display, reports, summaries

from . import factories as f
from . import test_srp_summaries as data
from .test_export_datasets import build, csv_rows, request_for

UTC = dt_timezone.utc
M = data.M
Status = SrpClaim.Status
at = data.at
MARCH = {"from": "2026-03-01", "to": "2026-03-31"}
NOW = datetime(2026, 11, 15, 10, 30, tzinfo=UTC)


def period(**params):
    return parse_period(params, now=NOW)


class PeriodParsingTests(SimpleTestCase):
    def test_a_september_range_is_the_old_month(self):
        # STAT-1 acceptance: the date range of September equals ?year=2026&month=9.
        by_dates = period(**{"from": "2026-09-01", "to": "2026-09-30"})
        by_month = period(year="2026", month="9")

        self.assertEqual((by_dates.start, by_dates.end), (by_month.start, by_month.end))
        self.assertEqual(by_dates.start, datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(by_dates.end, datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual((by_dates.kind, by_month.kind), (periods.KIND_RANGE, periods.KIND_MONTH))

    def test_both_days_are_included(self):
        one_day = period(**{"from": "2026-02-28", "to": "2026-02-28"})

        self.assertEqual(one_day.end - one_day.start, timedelta(days=1))
        self.assertEqual((one_day.first_day.isoformat(), one_day.last_day.isoformat()), ("2026-02-28", "2026-02-28"))

    def test_the_longest_period(self):
        first = datetime(2023, 1, 1, tzinfo=UTC)
        last = (first + timedelta(days=MAX_REPORT_DAYS - 1)).date().isoformat()
        self.assertEqual(period(**{"from": "2023-01-01", "to": last}).days, MAX_REPORT_DAYS)

        too_long = (first + timedelta(days=MAX_REPORT_DAYS)).date().isoformat()
        with self.assertRaises(PeriodError):
            period(**{"from": "2023-01-01", "to": too_long})

    def test_unusable_ranges_are_errors(self):
        for params in (
            {"from": "2026-03-10", "to": "2026-03-09"},
            {"from": "yesterday", "to": "2026-03-09"},
            {"from": "2026-02-30", "to": "2026-03-09"},
            {"from": "2026-03-01"},
            {"to": "2026-03-01"},
            {"preset": "forever"},
        ):
            with self.subTest(params=params), self.assertRaises(PeriodError):
                period(**params)

    def test_the_default_is_the_last_six_months(self):
        default = period()

        self.assertEqual(default.kind, periods.KIND_DEFAULT)
        self.assertEqual(default.start, datetime(2026, 6, 1, tzinfo=UTC))
        self.assertEqual(default.end, datetime(2026, 12, 1, tzinfo=UTC))
        self.assertEqual(len(default.months), 6)

    def test_presets(self):
        expected = {
            "this_month": ((2026, 11, 1), (2026, 12, 1)),
            "last_month": ((2026, 10, 1), (2026, 11, 1)),
            "last_3": ((2026, 9, 1), (2026, 12, 1)),
            "last_6": ((2026, 6, 1), (2026, 12, 1)),
            "last_12": ((2025, 12, 1), (2026, 12, 1)),
            "this_year": ((2026, 1, 1), (2026, 12, 1)),
        }
        for name, (start, end) in expected.items():
            with self.subTest(preset=name):
                result = period(preset=name)
                self.assertEqual((result.start, result.end), (datetime(*start, tzinfo=UTC), datetime(*end, tzinfo=UTC)))
                self.assertEqual((result.kind, result.preset), (periods.KIND_PRESET, name))

    def test_last_month_across_the_new_year(self):
        january = parse_period({"preset": "last_month"}, now=datetime(2027, 1, 3, tzinfo=UTC))

        self.assertEqual(january.start, datetime(2026, 12, 1, tzinfo=UTC))

    def test_all_begins_at_the_oldest_claim(self):
        result = parse_period({"preset": "all"}, earliest=datetime(2026, 2, 14, 9, 0, tzinfo=UTC), now=NOW)

        self.assertEqual(result.start, datetime(2026, 2, 14, tzinfo=UTC))
        self.assertFalse(result.cut)

    def test_all_is_cut_to_the_longest_period(self):
        # "all" older than 1100 days is cut, and the period says so.
        result = parse_period({"preset": "all"}, earliest=datetime(2020, 1, 1, tzinfo=UTC), now=NOW)

        self.assertTrue(result.cut)
        self.assertEqual(result.days, MAX_REPORT_DAYS)
        self.assertEqual(result.end, datetime(2026, 12, 1, tzinfo=UTC))

    def test_all_without_claims_is_the_running_month(self):
        result = parse_period({"preset": "all"}, now=NOW)

        self.assertEqual(result.start, datetime(2026, 11, 1, tzinfo=UTC))

    def test_the_old_month_parameters(self):
        self.assertEqual(period(year="2026", month="3").file_label, "2026-03-01_2026-03-31")
        # An unreadable pair is ignored like on the month pages.
        self.assertEqual(period(year="abc", month="3").kind, periods.KIND_DEFAULT)
        self.assertEqual(period(year="2026", month="13").kind, periods.KIND_DEFAULT)

    def test_typed_dates_win_over_a_preset(self):
        # The more specific input wins, whatever order the parameters came in.
        result = period(preset="last_month", **{"from": "2026-01-01", "to": "2026-01-02"})

        self.assertEqual(result.start, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(result.kind, periods.KIND_RANGE)
        # Empty dates leave the preset; one lone date is no range, so the preset still counts.
        self.assertEqual(period(preset="last_month", **{"from": "", "to": ""}).start, datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(period(preset="last_month", **{"from": "2026-01-01"}).start, datetime(2026, 10, 1, tzinfo=UTC))
        # Bad dates are an error even next to a preset.
        with self.assertRaises(PeriodError):
            period(preset="last_month", **{"from": "2026-01-05", "to": "2026-01-02"})
        with self.assertRaises(PeriodError):
            period(preset="forever", **{"from": "2026-01-01", "to": "2026-01-02"})

    def test_file_labels(self):
        # Every period names its first and last day, a whole calendar month included.
        self.assertEqual(period(**{"from": "2026-03-01", "to": "2026-03-31"}).file_label, "2026-03-01_2026-03-31")
        self.assertEqual(period(**{"from": "2026-02-01", "to": "2026-02-28"}).file_label, "2026-02-01_2026-02-28")
        self.assertEqual(period(**{"from": "2026-03-02", "to": "2026-03-31"}).file_label, "2026-03-02_2026-03-31")
        self.assertEqual(period(preset="last_3").file_label, "2026-09-01_2026-11-30")

    def test_the_months_of_a_period(self):
        result = period(**{"from": "2026-01-31", "to": "2026-03-01"})

        self.assertEqual(result.months, [(2026, 1), (2026, 2), (2026, 3)])

    def test_the_period_query_gives_the_period_again(self):
        result = period(**{"from": "2026-03-02", "to": "2026-05-04"})

        self.assertEqual(periods.period_query(result), {"from": "2026-03-02", "to": "2026-05-04"})
        self.assertEqual(periods.period_query(period(preset="all")), {"preset": "all"})


class BoundaryTests(TestCase):
    def test_months_are_cut_at_utc_midnight(self):
        user = f.create_user("stat_bound", perms=f.SRP_MEMBER_PERMS)
        inside = f.srp_claim(user, time=datetime(2026, 9, 30, 23, 59, tzinfo=UTC))
        outside = f.srp_claim(user, time=datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
        first = f.srp_claim(user, time=datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
        before = f.srp_claim(user, time=datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
        september = period(year="2026", month="9")

        found = set(summaries.period_claims(september.start, september.end).values_list("pk", flat=True))

        self.assertEqual(found, {inside.pk, first.pk})
        self.assertNotIn(outside.pk, found)
        self.assertNotIn(before.pk, found)
        self.assertEqual(
            set(summaries.month_claims(2026, 9).values_list("pk", flat=True)), found
        )

    def test_withdrawn_claims_are_left_out_and_a_base_is_respected(self):
        user = f.create_user("stat_bound2", perms=f.SRP_MEMBER_PERMS)
        other = f.create_user("stat_bound3", perms=f.SRP_MEMBER_PERMS)
        kept = f.srp_claim(user, time=at(5))
        f.srp_claim(user, time=at(6), status=Status.WITHDRAWN)
        f.srp_claim(other, time=at(7))
        march = period(**MARCH)

        claims = summaries.period_claims(march.start, march.end, SrpClaim.objects.filter(user=user))

        self.assertEqual(list(claims), [kept])


class ReportData(data.SummaryData):
    """March 2026 plus categories, regions, batches and decision events."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.march = period(**MARCH)
        cls.viewer = f.create_user("rep_viewer", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])
        cls.manager = f.create_user("rep_manager", perms=f.SRP_MANAGER_PERMS)
        cls.reviewer = f.create_user("rep_reviewer", perms=f.SRP_REVIEWER_PERMS)
        cls.payer = f.create_user("rep_payer", perms=f.SRP_PAYER_PERMS)
        cls.member = f.create_user("rep_member", perms=f.SRP_MEMBER_PERMS)
        cls.corp_manager = f.create_user("rep_corp", perms=[*f.MEMBER_PERMS, "fleetops.view_corp_stats"])


class TrendTests(ReportData):
    def test_every_month_is_a_row_and_the_rows_add_up(self):
        window = period(**{"from": "2026-01-01", "to": "2026-06-30"})

        rows = summaries.trend(window.start, window.end)

        self.assertEqual([(row.year, row.month) for row in rows], [(2026, m) for m in range(1, 7)])
        self.assertEqual([row.totals.claims for row in rows], [0, 0, 8, 0, 0, 0])
        whole = summaries.period_totals(window.start, window.end)
        self.assertEqual(sum(row.totals.claims for row in rows), whole.claims)
        self.assertEqual(sum(row.totals.requested for row in rows), whole.requested)
        self.assertEqual(sum(row.totals.approved for row in rows), whole.approved)

    def test_a_month_has_the_figures_of_the_summary(self):
        march = summaries.trend(self.march.start, self.march.end)[0]
        totals = summaries.month_totals(2026, 3)

        self.assertEqual(
            (march.totals.claims, march.totals.requested, march.totals.approved, march.totals.paid, march.totals.rejected),
            (totals.claims, totals.requested, totals.approved, totals.paid, totals.rejected),
        )
        self.assertEqual(march.totals.average_payout, round(totals.approved / totals.approved_count))
        self.assertEqual(march.totals.loss_value, int(sum(c.loss_value or 0 for c in SrpClaim.objects.exclude(status=Status.WITHDRAWN))))

    def test_a_partial_month_counts_only_its_days(self):
        window = period(**{"from": "2026-03-12", "to": "2026-03-31"})

        rows = summaries.trend(window.start, window.end)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].totals.claims, 4)

    def test_split_by_category_has_an_uncategorised_row(self):
        rows = summaries.trend(self.march.start, self.march.end, by_category=True)

        categories = {row.category_id for row in rows}
        self.assertIn(None, categories)
        self.assertEqual(sum(row.totals.claims for row in rows), 8)
        self.assertEqual(sum(row.totals.approved for row in rows), summaries.month_totals(2026, 3).approved)

    def test_the_number_of_queries_is_one(self):
        with CaptureQueriesContext(connection) as captured:
            summaries.trend(self.march.start, self.march.end)
        self.assertEqual(len(captured), 1)


class DimensionTests(ReportData):
    def test_by_category_adds_up_to_the_total(self):
        rows = summaries.by_category(self.march.start, self.march.end)

        self.assertEqual(sum(row.totals.claims for row in rows), 8)
        self.assertEqual(sum(row.totals.requested for row in rows), summaries.month_totals(2026, 3).requested)
        fleet = next(row for row in rows if row.category_key == "fleet")
        self.assertEqual(fleet.totals.claims, 4)
        self.assertEqual(fleet.members, 3)
        self.assertIn(None, [row.category_id for row in rows])

    def test_capped_claims_and_the_caps_of_a_category(self):
        category = f.srp_category("Cut", personal_monthly_cap=30 * M, corporation_monthly_cap=90 * M)
        user = f.create_user("rep_cap", perms=f.SRP_MEMBER_PERMS)
        f.srp_claim(user, time=at(20), category=category, amount=10 * M, suggested_amount=7 * M, limited_amount=7 * M)
        f.srp_claim(user, time=at(21), category=category, amount=10 * M)

        row = next(r for r in summaries.by_category(self.march.start, self.march.end) if r.category_id == category.pk)

        self.assertEqual((row.totals.claims, row.capped, row.members), (2, 1, 1))
        self.assertEqual((row.personal_cap, row.corporation_cap), (30 * M, 90 * M))

    def test_by_ship_and_its_cut(self):
        result = summaries.by_ship(self.march.start, self.march.end)

        self.assertFalse(result.truncated)
        rifter = next(row for row in result.rows if row.ship_type_id == data.RIFTER)
        self.assertEqual((rifter.losses, rifter.approved_count, rifter.rejected), (4, 2, 0))
        self.assertEqual(rifter.approved, 75 * M)
        self.assertEqual(rifter.average_payout, round(75 * M / 2))
        self.assertEqual(sum(row.losses for row in result.rows), 8)

        cut = summaries.by_ship(self.march.start, self.march.end, limit=2)
        self.assertTrue(cut.truncated)
        self.assertEqual(len(cut.rows), 2)

    def test_by_member_adds_the_alts_up_under_the_main(self):
        alt = f.create_character_for(self.alice) if hasattr(f, "create_character_for") else None
        if alt is None:
            # A second loss of Alice under another character is the same player.
            f.srp_claim(self.alice, time=at(22), character=f.main_of(self.alice), amount=5 * M, status=Status.APPROVED)
        rows = summaries.by_member(self.march.start, self.march.end)

        alice = [row for row in rows if row.user_id == self.alice.pk]
        self.assertEqual(len(alice), 1)
        self.assertEqual(alice[0].totals.claims, 5)
        self.assertEqual(len(rows), 3)

    def test_by_member_keeps_the_claims_of_a_deleted_account_together(self):
        SrpClaim.objects.filter(pk__in=[self.b1.pk, self.b2.pk]).update(user=None)

        rows = summaries.by_member(self.march.start, self.march.end)

        bob = [row for row in rows if row.main_character_name == "Bob Main"]
        self.assertEqual(len(bob), 1)
        self.assertIsNone(bob[0].user_id)
        self.assertEqual(bob[0].totals.claims, 2)

    def test_by_corporation_counts_the_people(self):
        rows = {row.corporation_id: row for row in summaries.by_corporation(self.march.start, self.march.end)}

        self.assertEqual(rows[f.DEFAULT_CORP[0]].members, 2)
        self.assertEqual(rows[f.OTHER_CORP[0]].members, 1)
        self.assertEqual(rows[f.OTHER_CORP[0]].cap, 100 * M)
        self.assertEqual(rows[f.DEFAULT_CORP[0]].totals.average_payout, round(rows[f.DEFAULT_CORP[0]].totals.approved / rows[f.DEFAULT_CORP[0]].totals.approved_count))

    def test_by_region_names_the_unknown_ones(self):
        SrpClaim.objects.filter(pk=self.a1.pk).select_related("killmail")
        killmail = self.a1.killmail
        killmail.region_id = 10_000_002
        killmail.save(update_fields=["region_id"])
        EveEntityName.objects.create(id=10_000_002, category="region", name="The Forge", updated_at=at(1))

        rows = {row.region_id: row for row in summaries.by_region(self.march.start, self.march.end)}

        self.assertEqual(rows[10_000_002].region_name, "The Forge")
        self.assertEqual(rows[10_000_002].totals.claims, 1)
        self.assertEqual(rows[None].totals.claims, 7)
        self.assertEqual(rows[None].region_name, "")

    def test_by_fc_has_the_fleet_types(self):
        rows = {row.fc_name: row for row in summaries.by_fc(self.march.start, self.march.end)}

        first = rows["First FC"]
        self.assertEqual(sum(first.fleet_types.values()), first.fleets)
        self.assertEqual(first.fleets, 1)
        self.assertEqual(reports.fleet_type_text({"Home defence": 2, "": 1}), "Home defence: 2; Other: 1")

    def test_reviewers_automatic_and_imported(self):
        auto = f.srp_claim(self.alice, time=at(23), status=Status.REJECTED, auto_rejected=True, decided_by=None)
        imported = f.srp_claim(self.bob, time=at(24), status=Status.APPROVED, source=SrpClaim.Source.AASRP, decided_by=None)
        imported.decided_by = None
        imported.save(update_fields=["decided_by"])

        rows = {row.name: row for row in summaries.by_reviewer(self.march.start, self.march.end)}

        self.assertEqual(rows["First FC"].approved_count, 5)
        self.assertEqual(rows["First FC"].approved, 265 * M)
        self.assertEqual(rows["First FC"].rejected, 1)
        self.assertEqual(rows["Automatic"].rejected, 1)
        self.assertEqual((rows["aa-srp (unknown)"].approved_count, rows["aa-srp (unknown)"].approved), (1, 10 * M))
        del auto

    def test_payers_and_their_batches(self):
        batch = SrpPaymentBatch.objects.create(created_by=self.fc1, claim_count=2, payee_count=2, total=115 * M)
        SrpClaim.objects.filter(pk__in=[self.a1.pk, self.b2.pk]).update(payment_batch=batch)

        rows = summaries.by_payer(self.march.start, self.march.end)

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].claims, rows[0].amount, rows[0].batches), (3, 175 * M, 1))
        self.assertEqual(rows[0].name, "First FC")

    def test_rejections_and_payments_are_lists(self):
        rejected = list(summaries.rejections(self.march.start, self.march.end))
        paid = list(summaries.payments(self.march.start, self.march.end))

        self.assertEqual([claim.pk for claim in rejected], [self.a4.pk])
        self.assertEqual({claim.pk for claim in paid}, {self.a1.pk, self.b2.pk, self.c1.pk})


class LimitsTests(ReportData):
    def approve_event(self, claim, over, action=SrpClaimEvent.Action.APPROVE):
        return f.claim_event(claim, action, actor=self.fc1, data={"over_limit_by": over})

    def test_used_cap_remaining_and_counts(self):
        SrpClaim.objects.filter(pk=self.a3.pk).update(over_limit=True)

        rows = {row.member_name: row for row in summaries.limits(self.march.start, self.march.end) if row.category_id is None}

        alice = rows["Alice Main"]
        self.assertEqual((alice.used, alice.cap, alice.remaining), (140 * M, 150 * M, 10 * M))
        self.assertEqual((alice.claims, alice.over_limit), (3, 1))
        # Rejected claims are no part of a limit; the withdrawn one neither.
        self.assertEqual(rows["Bob Main"].used, 65 * M)

    def test_waivers_come_from_the_last_approval_event(self):
        self.approve_event(self.a1, 0)
        self.approve_event(self.a1, 15 * M, SrpClaimEvent.Action.AMOUNT_CHANGE)
        self.approve_event(self.a2, 5 * M)
        self.approve_event(self.a2, 0, SrpClaimEvent.Action.AMOUNT_CHANGE)
        self.approve_event(self.b1, 0)

        rows = {row.member_name: row for row in summaries.limits(self.march.start, self.march.end)}

        self.assertEqual(rows["Alice Main"].waived, 1)
        self.assertEqual(rows["Bob Main"].waived, 0)

    def test_category_caps_are_extra_rows_and_waivers_follow_the_category(self):
        category = f.srp_category("Capped", personal_monthly_cap=40 * M)
        claim = f.srp_claim(self.bob, time=at(25), category=category, status=Status.APPROVED, amount=30 * M)
        self.approve_event(claim, 3 * M)

        rows = [row for row in summaries.limits(self.march.start, self.march.end) if row.member_name == "Bob Main"]

        self.assertEqual(len(rows), 2)
        special = next(row for row in rows if row.category_id == category.pk)
        general = next(row for row in rows if row.category_id is None)
        self.assertEqual((special.used, special.cap, special.remaining, special.waived), (30 * M, 40 * M, 10 * M, 1))
        self.assertEqual(general.waived, 1)

    def test_without_a_cap_there_is_no_general_row(self):
        self.settings_obj.personal_monthly_cap = None
        self.settings_obj.save()

        self.assertEqual(summaries.limits(self.march.start, self.march.end), [])

    def test_months_are_separate_rows(self):
        f.srp_claim(self.alice, time=at(5, month=4), status=Status.APPROVED, amount=10 * M)
        window = period(**{"from": "2026-03-01", "to": "2026-04-30"})

        rows = [row for row in summaries.limits(window.start, window.end) if row.member_name == "Alice Main"]

        self.assertEqual([(row.year, row.month) for row in rows], [(2026, 3), (2026, 4)])

    def test_constant_queries(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                summaries.limits(self.march.start, self.march.end)
            return len(captured)

        small = count()
        self.grow(4)
        self.assertEqual(count(), small)


class ReportPageTests(ReportData):
    url = reverse("fleetops:srp_reports")

    def get(self, user, **params):
        self.client.force_login(user)
        # A preset replaces the default dates (typed dates would win over it).
        dates = {"from": "", "to": ""} if "preset" in params else MARCH
        return self.client.get(self.url, {**dates, **params})

    def test_the_period_form_keeps_dates_only_for_a_custom_range(self):
        by_preset = self.get(self.manager, report="categories", preset="last_3")
        by_dates = self.get(self.manager, report="categories")

        self.assertContains(by_preset, 'name="from" value=""')
        self.assertContains(by_dates, 'name="from" value="2026-03-01"')
        self.assertContains(by_dates, 'name="to" value="2026-03-31"')
        self.assertEqual(by_dates.context["period"].kind, periods.KIND_RANGE)

    def test_who_may_open_the_page(self):
        for user in (self.manager, self.reviewer, self.payer, self.viewer):
            with self.subTest(user=user.username):
                self.assertEqual(self.get(user).status_code, 200)
        for user in (self.member, self.corp_manager):
            with self.subTest(user=user.username):
                self.assertEqual(self.get(user).status_code, 403)

    def test_the_cards_and_the_trend(self):
        response = self.get(self.manager)

        self.assertContains(response, 'id="srp-report-cards"')
        self.assertContains(response, "2026-03")
        totals = response.context["totals"]
        self.assertEqual((totals.claims, totals.approved), (8, 265 * M))
        self.assertEqual(response.context["report"].slug, "trend")
        self.assertContains(response, "fo-bar-fill")

    def test_every_report_renders(self):
        for slug in reports.REPORTS:
            with self.subTest(report=slug):
                response = self.get(self.manager, report=slug)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "srp-report-table")

    def test_reviewer_and_payer_reports_are_staff_only(self):
        for slug in ("reviewers", "payers"):
            with self.subTest(report=slug):
                self.assertEqual(self.get(self.viewer, report=slug).status_code, 403)
                for user in (self.manager, self.reviewer, self.payer):
                    self.assertEqual(self.get(user, report=slug).status_code, 200)

    def test_view_all_stats_does_not_see_them_in_the_list(self):
        viewer = [choice.slug for choice in self.get(self.viewer).context["report_choices"]]
        staff = [choice.slug for choice in self.get(self.manager).context["report_choices"]]

        self.assertNotIn("reviewers", viewer)
        self.assertNotIn("payers", viewer)
        self.assertIn("reviewers", staff)
        self.assertIn("payers", staff)
        self.assertNotContains(self.get(self.viewer), 'value="reviewers"')

    def test_names_of_deciders_are_for_staff(self):
        self.assertContains(self.get(self.manager, report="rejections"), "Not covered")
        staff = self.get(self.manager, report="rejections")
        viewer = self.get(self.viewer, report="rejections")

        self.assertContains(staff, "First FC")
        self.assertNotContains(viewer, "First FC")
        self.assertNotContains(self.get(self.viewer, report="payments"), "First FC")

    def test_a_period_too_long_is_named_and_the_default_is_shown(self):
        response = self.get(self.manager, **{"from": "2020-01-01", "to": "2026-03-31"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="srp-report-error"')
        self.assertEqual(response.context["period"].kind, periods.KIND_DEFAULT)

    def test_a_range_over_two_months(self):
        response = self.get(self.manager, **{"from": "2026-02-01", "to": "2026-03-31"})

        rows = response.context["rows"]
        self.assertEqual([row["cells"][0]["text"] for row in rows], ["2026-02", "2026-03"])

    def test_the_search_narrows_the_rows_but_not_the_total(self):
        response = self.get(self.manager, report="members", q="alice")

        self.assertEqual(response.context["row_count"], 1)
        self.assertEqual(response.context["footer"][0]["text"], "Total")
        self.assertEqual(response.context["footer"][2]["text"], "8")

    def test_the_rows_are_paged_by_fifty(self):
        self.grow(24)
        response = self.get(self.manager, report="members")

        self.assertEqual(len(response.context["rows"]), 27)
        self.grow(25)
        response = self.get(self.manager, report="members")
        self.assertEqual(len(response.context["rows"]), 50)
        self.assertEqual(self.get(self.manager, report="members", page=2).context["page"].number, 2)

    def test_the_export_links_name_the_period(self):
        response = self.get(self.manager, report="categories", preset="last_3", q="x")

        content = response.content.decode()
        self.assertIn("/export/srp-categories/", content)
        self.assertIn("from=", content)
        self.assertNotIn("preset=last_3&amp;format", content)

    def test_year_list_reaches_back_to_an_imported_year(self):
        # STAT-1: after an import the year lists have 2025.
        f.srp_claim(self.alice, time=at(4, month=7, year=2025))

        for name in ("srp_summary", "srp_review", "srp_payments"):
            with self.subTest(page=name):
                self.client.force_login(self.manager)
                response = self.client.get(reverse(f"fleetops:{name}"), {"year": 2026, "month": 3})
                self.assertIn(2025, response.context["year_options"])

    def test_the_summary_links_to_the_reports_and_has_the_category_card(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("fleetops:srp_summary"), {"year": 2026, "month": 3})

        self.assertContains(response, 'id="srp-reports-link"')
        self.assertContains(response, "This month by category")
        self.assertTrue(response.context["category_rows"])

    def test_chinese_page(self):
        with translation.override("zh-hans"):
            response = self.get(self.manager, report="categories")
        self.assertEqual(response.status_code, 200)

    def test_menu_entry(self):
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse("fleetops:srp_summary")), reverse("fleetops:srp_reports"))


class ReportFileTests(ReportData):
    def table(self, slug, user=None, **params):
        return build(slug, user or self.manager, **{**MARCH, **params})

    def test_trend_file_has_the_total_in_the_footer(self):
        table, filename, scope = self.table("srp-trend", **{"from": "2026-01-01", "to": "2026-03-31"})

        self.assertEqual(len(table.rows), 3)
        self.assertEqual(table.footer[0][0], "Total")
        self.assertEqual(table.footer[0][1], 8)
        self.assertNotIn("Total", [row[0] for row in table.rows])
        self.assertEqual(filename, "fleetops-srp-trend-alliance-2026-01-01_2026-03-31")
        self.assertEqual(dict(table.about)["Period (EVE time)"], "2026-01-01 - 2026-03-31")

    def test_a_calendar_month_is_named_by_its_days(self):
        _table, filename, scope = self.table("srp-categories")

        self.assertEqual(filename, "fleetops-srp-categories-alliance-2026-03-01_2026-03-31")
        self.assertEqual((scope.year, scope.month), (2026, 3))
        self.assertEqual(scope.params, {"year": 2026, "month": 3})

    def test_a_range_writes_its_days_into_the_audit(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fleetops:export", args=["srp-ships"]), {"format": "csv", "from": "2026-03-02", "to": "2026-03-20"}
        )

        self.assertEqual(response.status_code, 200)
        entry = AuditLog.objects.filter(action="srp.export").latest("pk")
        self.assertEqual(entry.new_value["params"], {"from": "2026-03-02", "to": "2026-03-20"})
        self.assertIn("2026-03-02_2026-03-20", response["Content-Disposition"])

    def test_a_period_over_the_limit_is_a_bad_request(self):
        for slug in ("srp-claims", "srp-trend", "srp-members", "srp-payable"):
            with self.subTest(slug=slug), self.assertRaises(BadRequest):
                build(slug, self.manager, **{"from": "2020-01-01", "to": "2026-03-31"})

    def test_all_notes_its_cut(self):
        # The oldest claim is older than the 1100 days before the (fixed) now, so "All time" is cut.
        f.srp_claim(self.alice, time=datetime(2022, 6, 1, 12, tzinfo=UTC), status=Status.APPROVED, amount=10 * M)
        with mock.patch.object(periods.timezone, "now", return_value=NOW):
            table, filename, scope = self.table("srp-ships", preset="all", **{"from": "", "to": ""})
            self.client.force_login(self.manager)
            page = self.client.get(reverse("fleetops:srp_reports"), {"report": "ships", "preset": "all"})
        details = [value for label, value in table.about if label == "Note"]

        self.assertEqual(scope.params, {"preset": "all"})
        self.assertEqual(scope.window.kind, periods.KIND_PRESET)
        self.assertTrue(scope.window.cut)
        self.assertIn("All time is cut to the last 1100 days.", details)
        self.assertContains(page, 'id="srp-report-cut"')

    def test_a_period_that_is_not_cut_has_no_cut_note(self):
        table, _filename, _scope = self.table("srp-ships")
        self.client.force_login(self.manager)
        page = self.client.get(self.reverse_reports, {"report": "ships", **MARCH})

        self.assertNotIn("All time is cut to the last 1100 days.", [v for label, v in table.about if label == "Note"])
        self.assertNotContains(page, "srp-report-cut")

    reverse_reports = reverse("fleetops:srp_reports")

    def test_every_dimension_has_a_footer_and_no_total_row(self):
        for slug in ("srp-categories", "srp-ships", "srp-regions", "srp-reviewers", "srp-payers", "srp-rejections", "srp-payments"):
            with self.subTest(slug=slug):
                table, _filename, _scope = self.table(slug)
                self.assertEqual(len(table.footer), 1)
                self.assertEqual(table.footer[0][0], "Total")
                self.assertNotIn("Total", [row[0] for row in table.rows])
                self.assertEqual(len(table.footer[0]), len(table.columns))

    def test_the_categories_file_adds_up(self):
        table, _filename, _scope = self.table("srp-categories")
        titles = [column.title for column in table.columns]
        claims = titles.index("Claims")

        self.assertEqual(sum(row[claims] for row in table.rows), table.footer[0][claims])
        self.assertEqual(table.footer[0][claims], 8)

    def test_categories_used_and_remaining_for_one_calendar_month(self):
        # Used is always there; Remaining of a category-wide cap only for exactly one calendar month.
        category = f.srp_category("Roams", corporation_monthly_cap=50 * M)
        user = f.create_user("rep_used", perms=f.SRP_MEMBER_PERMS)
        f.srp_claim(user, time=at(20), category=category, status=Status.APPROVED, amount=12 * M)
        f.srp_claim(user, time=at(21), category=category, status=Status.PAID, amount=8 * M)
        f.srp_claim(user, time=at(22), category=category, status=Status.REJECTED, amount=30 * M)
        f.srp_claim(user, time=at(23), category=category, status=Status.PENDING, amount=30 * M)

        month = reports.REPORTS["categories"].build(period(**MARCH))
        keys = [column.key for column in month.columns]
        row = next(r for r in month.rows if r[0] == "Roams")

        self.assertEqual(row[keys.index("used")], 20 * M)
        self.assertEqual(row[keys.index("remaining")], 30 * M)
        self.assertEqual(month.footer[keys.index("used")], sum(r[keys.index("used")] for r in month.rows))
        self.assertEqual(len(month.footer), len(month.columns))
        self.assertEqual(len(month.notes), 1)
        self.assertIn("applies to each corporation", month.notes[0])

        wider = reports.REPORTS["categories"].build(period(**{"from": "2026-03-01", "to": "2026-04-30"}))
        row = next(r for r in wider.rows if r[0] == "Roams")
        self.assertEqual(row[keys.index("used")], 20 * M)
        self.assertIsNone(row[keys.index("remaining")])
        self.assertEqual(len(wider.notes), 1)
        self.assertIn("one calendar month", wider.notes[0])

        part = reports.REPORTS["categories"].build(period(**{"from": "2026-03-02", "to": "2026-03-31"}))
        self.assertIsNone(next(r for r in part.rows if r[0] == "Roams")[keys.index("remaining")])

    def test_remaining_does_not_go_below_zero(self):
        category = f.srp_category("Small", corporation_monthly_cap=5 * M)
        user = f.create_user("rep_over", perms=f.SRP_MEMBER_PERMS)
        f.srp_claim(user, time=at(20), category=category, status=Status.APPROVED, amount=9 * M)

        table = reports.REPORTS["categories"].build(period(**MARCH))
        keys = [column.key for column in table.columns]

        self.assertEqual(next(r for r in table.rows if r[0] == "Small")[keys.index("remaining")], 0)

    def test_reviewers_and_payers_are_for_staff(self):
        for slug in ("srp-reviewers", "srp-payers"):
            with self.subTest(slug=slug):
                self.assertEqual(datasets.get_dataset(slug).check(request_for(self.reviewer, **MARCH)).dataset, slug)
                with self.assertRaises(PermissionDenied):
                    datasets.get_dataset(slug).check(request_for(self.viewer, **MARCH))
                with self.assertRaises(PermissionDenied):
                    datasets.get_dataset(slug).check(request_for(self.corp_manager, **MARCH))

    def test_the_report_files_are_for_everyone_who_sees_all_summaries(self):
        for slug in ("srp-trend", "srp-categories", "srp-ships", "srp-regions", "srp-rejections", "srp-payments", "srp-limits"):
            with self.subTest(slug=slug):
                self.assertEqual(datasets.get_dataset(slug).check(request_for(self.viewer, **MARCH)).dataset, slug)
                for user in (self.member, self.corp_manager):
                    with self.assertRaises(PermissionDenied):
                        datasets.get_dataset(slug).check(request_for(user, **MARCH))

    def test_the_decider_columns_are_blank_for_view_all_stats(self):
        staff, _f, _s = self.table("srp-rejections")
        viewer, _f, _s = self.table("srp-rejections", self.viewer)
        titles = [column.title for column in staff.columns]
        decided = titles.index("Rejected by")

        self.assertEqual(staff.rows[0][decided], "First FC")
        self.assertEqual(viewer.rows[0][decided], "")

    def test_limits_file(self):
        table, filename, _scope = self.table("srp-limits")
        titles = [column.title for column in table.columns]

        self.assertEqual(titles[:4], ["Month", "Main character", "Limit", "Used (ISK)"])
        self.assertEqual(table.footer, [])
        self.assertEqual(filename, "fleetops-srp-limits-alliance-2026-03-01_2026-03-31")

    def test_split_trend_file(self):
        table, _filename, scope = self.table("srp-trend", split="category")

        self.assertEqual([column.title for column in table.columns][1], "Category")
        self.assertEqual(scope.params["split"], "category")
        self.assertIn("Uncategorised", [row[1] for row in table.rows])

    def test_responses_download_as_csv_and_xlsx(self):
        self.client.force_login(self.manager)
        for slug in ("srp-trend", "srp-limits", "srp-regions"):
            for fmt in ("csv", "xlsx"):
                with self.subTest(slug=slug, fmt=fmt):
                    response = self.client.get(reverse("fleetops:export", args=[slug]), {"format": fmt, **MARCH})
                    self.assertEqual(response.status_code, 200)
                    if fmt == "csv":
                        rows = csv_rows(response)
                        self.assertTrue(rows)


class ClaimsFileColumnTests(ReportData):
    def setUp(self):
        self.titles_before = [
            "Claim", "Loss time (EVE)", "Pilot (main)", "Character", "Corporation", "Ship", "System", "Scope", "Fleet",
            "FC", "Value (ISK)", "Value source", "Payout basis", "Suggested (ISK)", "Within limit (ISK)",
            "Approved (ISK)", "Status", "Decided by", "Decided at (EVE)", "Reason", "Paid at (EVE)", "Paid by",
            "Fleet check", "Warnings", "Payment batch",
        ]

    def test_four_columns_are_appended_at_the_end(self):
        table, _filename, _scope = build("srp-claims", self.manager, **MARCH)
        titles = [column.title for column in table.columns]

        self.assertEqual(titles[:25], self.titles_before)
        self.assertEqual(titles[25:], ["Category", "Link", "Source", "Security status"])

    def test_category_link_source_security(self):
        standing = f.standing_link(name="Weekend roam", category=f.srp_category("Roams"))
        claim = f.srp_claim(self.alice, time=at(26), link=standing)
        killmail = claim.killmail
        killmail.solar_system_security = "0.4500"
        killmail.save(update_fields=["solar_system_security"])

        table, _filename, _scope = build("srp-claims", self.manager, **MARCH)
        titles = [column.title for column in table.columns]
        rows = {row[0]: dict(zip(titles, row)) for row in table.rows}
        row = rows[claim.reference]

        self.assertEqual((row["Category"], row["Link"], row["Source"]), ("Roams", "Weekend roam", "Pasted link"))
        self.assertEqual(float(row["Security status"]), 0.45)
        fleet_row = rows[self.a1.reference]
        self.assertEqual(fleet_row["Category"], display.category_label(self.a1.category))
        self.assertEqual(fleet_row["Link"], self.a1.fleet_label)
        self.assertEqual(rows[self.a2.reference]["Link"], "")

    def test_imported_links_use_the_aa_srp_name(self):
        link = f.standing_link(name="Fleet SRP from aa-srp", category=f.srp_category("Old"))
        link.origin = link.Origin.AASRP
        link.kind = link.Kind.FLEET
        link.save()
        claim = f.srp_claim(self.alice, time=at(27), link=link, source=SrpClaim.Source.AASRP)

        table, _filename, _scope = build("srp-claims", self.manager, **MARCH)
        titles = [column.title for column in table.columns]
        row = next(dict(zip(titles, r)) for r in table.rows if r[0] == claim.reference)

        self.assertEqual((row["Link"], row["Source"]), ("Fleet SRP from aa-srp", "aa-srp import"))

    def test_security_is_for_staff_only(self):
        for user, shown in ((self.manager, True), (self.viewer, False)):
            with self.subTest(user=user.username):
                killmail = self.a1.killmail
                killmail.solar_system_security = "0.9000"
                killmail.save(update_fields=["solar_system_security"])
                table, _filename, _scope = build("srp-claims", user, **MARCH)
                titles = [column.title for column in table.columns]
                row = next(dict(zip(titles, r)) for r in table.rows if r[0] == self.a1.reference)
                self.assertEqual(bool(row["Security status"]), shown)

    def test_filter_by_category(self):
        fleet = self.a1.category
        table, _filename, scope = build("srp-claims", self.manager, category=fleet.key, **MARCH)

        self.assertEqual(len(list(table.rows)), 4)
        self.assertEqual(scope.params["category"], fleet.key)
        self.assertEqual(table.footer[0][2], "4 claims")
        self.assertIn("Category: fleet", dict(table.about)["Filters"])

    def test_an_unknown_category_is_not_found(self):
        with self.assertRaises(Http404):
            build("srp-claims", self.manager, category="nothing", **MARCH)

    def test_a_range_filters_the_claims(self):
        table, filename, _scope = build("srp-claims", self.manager, **{"from": "2026-03-12", "to": "2026-03-14"})

        self.assertEqual(len(list(table.rows)), 3)
        self.assertEqual(filename, "fleetops-srp-claims-alliance-2026-03-12_2026-03-14")

    def test_older_files_got_appended_columns(self):
        members, _f, _s = build("srp-members", self.manager, **MARCH)
        corps, _f, _s = build("srp-corporations", self.manager, **MARCH)
        fcs, _f, _s = build("srp-fcs", self.manager, **MARCH)

        self.assertEqual(members.columns[-1].title, "Average payout (ISK)")
        self.assertEqual([c.title for c in corps.columns][-2:], ["People", "Average payout (ISK)"])
        self.assertEqual([c.title for c in fcs.columns][-2:], ["Average payout (ISK)", "Fleet types (fleets)"])
        # The older files keep the corporation and FC totals as the last data row.
        self.assertEqual(corps.rows[-1][0], "Total")
        self.assertEqual(fcs.rows[-1][0], "Total")

    def test_payable_accepts_a_range(self):
        payer = self.manager
        table, filename, scope = build("srp-payable", payer, **{"from": "2026-03-10", "to": "2026-03-10"})

        self.assertEqual(scope.params["from"], "2026-03-10")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(filename, "fleetops-srp-payable-alliance-2026-03-10_2026-03-10")


class ReportBudgetTests(ReportData):
    def test_the_report_queries_do_not_grow(self):
        self.client.force_login(self.manager)

        def count(slug):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(reverse("fleetops:srp_reports"), {**MARCH, "report": slug})
            self.assertEqual(response.status_code, 200)
            return len(captured)

        for slug in reports.REPORTS:
            count(slug)  # the first request fills the permission cache
        small = {slug: count(slug) for slug in reports.REPORTS}
        self.grow(6)
        for slug in reports.REPORTS:
            with self.subTest(report=slug):
                self.assertEqual(count(slug), small[slug])


class ReviewFixTests(ReportData):
    """Findings of the phase 5 review: per-corporation caps, the SQL limit of the lists, whole-month limits."""

    def keys(self, table):
        return [column.key for column in table.columns]

    def test_categories_remaining_is_for_the_corporation_that_used_most(self):
        category = f.srp_category("Two corps", corporation_monthly_cap=100 * M)
        first = f.create_user("rep_cat_a", perms=f.SRP_MEMBER_PERMS)
        second = f.create_user("rep_cat_b", perms=f.SRP_MEMBER_PERMS, corporation=f.OTHER_CORP)
        f.srp_claim(first, time=at(20), category=category, status=Status.APPROVED, amount=60 * M)
        f.srp_claim(second, time=at(21), category=category, status=Status.PAID, amount=70 * M)
        # Not counted for the corporation: the corporation's own figure leaves it out.
        f.srp_claim(first, time=at(22), category=category, status=Status.APPROVED, amount=50 * M, counts_corporation=False)

        table = reports.REPORTS["categories"].build(period(**MARCH))
        keys = self.keys(table)
        row = next(r for r in table.rows if r[0] == "Two corps")

        self.assertEqual(row[keys.index("used")], 180 * M)
        self.assertEqual(row[keys.index("remaining")], 30 * M)
        self.assertTrue(any("applies to each corporation" in note for note in table.notes))

    def test_categories_remaining_with_three_corporations_below_the_cap(self):
        category = f.srp_category("Three corps", corporation_monthly_cap=1000 * M)
        for index, corp in enumerate((None, f.OTHER_CORP, f.THIRD_CORP)):
            kwargs = {"corporation": corp} if corp else {}
            user = f.create_user(f"rep_three_{index}", perms=f.SRP_MEMBER_PERMS, **kwargs)
            f.srp_claim(user, time=at(20 + index), category=category, status=Status.APPROVED, amount=400 * M)

        table = reports.REPORTS["categories"].build(period(**MARCH))
        keys = self.keys(table)
        row = next(r for r in table.rows if r[0] == "Three corps")

        self.assertEqual(row[keys.index("used")], 1200 * M)
        self.assertEqual(row[keys.index("remaining")], 600 * M)

    def test_the_lists_are_cut_in_sql(self):
        with CaptureQueriesContext(connection) as captured:
            table = reports.REPORTS["payments"].build(self.march, staff=True, limit=2)
        self.assertEqual(len(table.rows), 2)
        self.assertTrue(any("LIMIT 3" in query["sql"] for query in captured))

        with CaptureQueriesContext(connection) as captured:
            reports.REPORTS["rejections"].build(self.march, staff=True, limit=1)
        self.assertTrue(any("LIMIT 2" in query["sql"] for query in captured))

    def test_the_payments_footer_covers_the_whole_period_when_cut(self):
        table = reports.REPORTS["payments"].build(self.march, staff=True, limit=2)

        self.assertTrue(table.truncated)
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(table.footer[2], 3)
        self.assertEqual(table.footer[-1], 175 * M)
        whole = summaries.period_totals(self.march.start, self.march.end)
        self.assertEqual(table.footer[-1], whole.paid)

    def test_the_rejections_footer_covers_the_whole_period_when_cut(self):
        f.srp_claim(self.alice, time=at(26), status=Status.REJECTED, amount=1 * M)
        f.srp_claim(self.bob, time=at(27), status=Status.REJECTED, amount=1 * M)

        cut = reports.REPORTS["rejections"].build(self.march, staff=True, limit=1)
        whole = reports.REPORTS["rejections"].build(self.march, staff=True)

        self.assertTrue(cut.truncated)
        self.assertEqual(len(cut.rows), 1)
        self.assertEqual(cut.footer[1], "3 claims")
        self.assertEqual(whole.footer[1], "3 claims")

    def test_a_limit_counts_whole_months(self):
        user = f.create_user("rep_partial", perms=f.SRP_MEMBER_PERMS, main_name="Partial Main")
        f.srp_claim(user, time=at(5), status=Status.APPROVED, amount=90 * M)
        f.srp_claim(user, time=at(20), status=Status.APPROVED, amount=5 * M)
        window = period(**{"from": "2026-03-10", "to": "2026-03-31"})

        rows = [row for row in summaries.limits(window.start, window.end) if row.user_id == user.pk]

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].used, rows[0].remaining, rows[0].claims), (95 * M, 55 * M, 2))
        table = reports.REPORTS["limits"].build(window)
        self.assertTrue(any("counted whole" in note for note in table.notes))

    def test_a_player_who_changed_corporation_shows_the_newest(self):
        user = f.create_user("rep_mover", perms=f.SRP_MEMBER_PERMS, main_name="Mover Main")
        f.srp_claim(
            user, time=at(3), status=Status.APPROVED, amount=5 * M,
            corporation_id=9_999_999, corporation_name="Zeta Old Corp", main_character_name="Zeta Old Main",
        )
        f.srp_claim(user, time=at(25), status=Status.APPROVED, amount=5 * M)

        rows = [row for row in summaries.by_member(self.march.start, self.march.end) if row.user_id == user.pk]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].main_character_name, "Mover Main")
        self.assertEqual(rows[0].corporation_name, f.main_of(user).corporation_name)
        self.assertEqual(rows[0].corporation_id, f.main_of(user).corporation_id)
        self.assertEqual(rows[0].totals.approved, 10 * M)

    def test_an_automatic_rejection_is_labelled_for_everyone(self):
        f.srp_claim(self.bob, time=at(28), status=Status.REJECTED, amount=1 * M, auto_rejected=True, decided_by=None)

        for user in (self.manager, self.viewer):
            table = reports.REPORTS["rejections"].build(self.march, staff=user is self.manager)
            decided = self.keys(table).index("decided_by")
            self.assertIn("Automatic", [row[decided] for row in table.rows])

    def test_corporations_file_fills_used_only_for_one_month(self):
        month, _f1, _s1 = self.table_for("srp-corporations", **MARCH)
        wide, _f2, _s2 = self.table_for("srp-corporations", **{"from": "2026-03-01", "to": "2026-04-30"})
        titles = [column.title for column in month.columns]
        used, remaining = titles.index("Used of the cap (ISK)"), titles.index("Remaining (ISK)")

        self.assertTrue(any(row[used] is not None for row in month.rows[:-1]))
        self.assertNotIn("Note", [label for label, _v in month.about])
        for row in wide.rows:
            self.assertIsNone(row[used])
            self.assertIsNone(row[remaining])
        self.assertTrue(any("exactly one calendar month" in value for label, value in wide.about if label == "Note"))

    def table_for(self, slug, **params):
        return build(slug, self.manager, **params)
