# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The SRP exports: who may download which file, what the files hold and the audit entry of a download."""

from datetime import date
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from fleetops.models import AuditLog, SrpClaim
from fleetops.services import export_datasets as datasets
from fleetops.services import exports
from fleetops import texts
from fleetops.srp import summaries
from fleetops.views_srp_review import day_bounds

from . import factories as f
from . import test_srp_summaries as data
from .test_export_datasets import build, csv_rows, request_for, workbook_rows

M = data.M
Status = SrpClaim.Status
SRP_SLUGS = ("srp-claims", "srp-members", "srp-corporations", "srp-fcs")
PARAMS = {"year": 2026, "month": 3}


def translation_of(text):
    with translation.override("zh-hans"):
        return texts.translate_stored(text)


def rows_by_title(table):
    """The rows of a table as dicts keyed by column title."""
    titles = [column.title for column in table.columns]
    return [dict(zip(titles, row)) for row in table.rows]


class ExportTestCase(data.SummaryData):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.manager = f.create_user("exp_manager", perms=f.SRP_MANAGER_PERMS)
        cls.viewer = f.create_user("exp_viewer", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])
        cls.corp_manager = f.create_user("exp_corp", perms=[*f.MEMBER_PERMS, "fleetops.view_corp_stats"])
        cls.member = f.create_user("exp_member", perms=f.SRP_MEMBER_PERMS)

    def download(self, user, slug, fmt="csv", **params):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:export", args=[slug]), {"format": fmt, **PARAMS, **params})


class RegistryTests(ExportTestCase):
    def test_the_srp_datasets_are_registered(self):
        for slug in SRP_SLUGS:
            with self.subTest(slug=slug):
                dataset = datasets.get_dataset(slug)
                self.assertEqual((dataset.slug, dataset.audit_action, dataset.audit_object), (slug, "srp.export", "SrpSummary"))

    def test_the_statistics_datasets_keep_their_audit_entry(self):
        for slug in ("corporations", "members", "member-fleets", "fcs", "attendance", "incentives"):
            with self.subTest(slug=slug):
                dataset = datasets.get_dataset(slug)
                self.assertEqual((dataset.audit_action, dataset.audit_object), ("statistics.export", "Statistics"))


class PermissionTests(ExportTestCase):
    def check(self, slug, user, **params):
        return datasets.get_dataset(slug).check(request_for(user, **params))

    def test_srp_managers_and_view_all_stats_download_everything(self):
        for user in (self.manager, self.viewer):
            for slug in SRP_SLUGS:
                with self.subTest(user=user.username, slug=slug):
                    self.assertEqual(self.check(slug, user, **PARAMS).dataset, slug)
                    self.assertEqual(self.download(user, slug).status_code, 200)

    def test_a_corporation_can_be_named(self):
        for slug in ("srp-claims", "srp-members"):
            with self.subTest(slug=slug):
                scope = self.check(slug, self.manager, corporation=str(f.OTHER_CORP[0]), **PARAMS)
                self.assertEqual((scope.label, scope.corporation_id), (f"corporation-{f.OTHER_CORP[0]}", f.OTHER_CORP[0]))

    def test_a_corporation_manager_downloads_the_members_and_claims_of_the_own_corporation(self):
        for slug in ("srp-claims", "srp-members"):
            with self.subTest(slug=slug):
                response = self.download(self.corp_manager, slug, corporation=f.DEFAULT_CORP[0])
                self.assertEqual(response.status_code, 200)

    def test_not_of_another_corporation_nor_of_the_alliance(self):
        for slug in ("srp-claims", "srp-members"):
            with self.subTest(slug=slug):
                self.assertEqual(self.download(self.corp_manager, slug, corporation=f.OTHER_CORP[0]).status_code, 404)
                self.assertEqual(self.download(self.corp_manager, slug).status_code, 403)

    def test_corporation_and_fc_files_are_for_the_people_who_see_everything(self):
        for slug in ("srp-corporations", "srp-fcs"):
            with self.subTest(slug=slug):
                self.assertEqual(self.download(self.corp_manager, slug).status_code, 403)
                self.assertEqual(self.download(self.corp_manager, slug, corporation=f.DEFAULT_CORP[0]).status_code, 403)

    def test_members_and_fcs_have_no_srp_file(self):
        for user in (self.member, self.fc1):
            for slug in SRP_SLUGS:
                with self.subTest(user=user.username, slug=slug):
                    self.assertEqual(self.download(user, slug).status_code, 403)
                    self.assertEqual(self.download(user, slug, corporation=f.DEFAULT_CORP[0]).status_code, 403)

    def test_nothing_is_written_to_the_audit_log_when_it_is_refused(self):
        self.download(self.member, "srp-claims")
        self.download(self.corp_manager, "srp-claims", corporation=f.OTHER_CORP[0])

        self.assertFalse(AuditLog.objects.filter(action="srp.export").exists())

    def test_anonymous_users_are_sent_to_the_login(self):
        response = self.client.get(reverse("fleetops:export", args=["srp-claims"]), {"format": "csv"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_the_checks_raise_the_usual_errors(self):
        with self.assertRaises(PermissionDenied):
            self.check("srp-corporations", self.corp_manager, **PARAMS)
        with self.assertRaises(Http404):
            self.check("srp-claims", self.corp_manager, corporation=str(f.OTHER_CORP[0]), **PARAMS)

    def test_a_corporation_that_is_not_a_number_means_the_alliance(self):
        scope = self.check("srp-claims", self.manager, corporation="abc", **PARAMS)

        self.assertEqual((scope.label, scope.corporation_id), ("alliance", None))
        with self.assertRaises(PermissionDenied):
            self.check("srp-claims", self.corp_manager, corporation="abc", **PARAMS)


class ClaimsFileTests(ExportTestCase):
    def table(self, user=None, **params):
        return build("srp-claims", user or self.manager, **{**PARAMS, **params})

    def test_one_row_per_claim_of_the_month_without_the_withdrawn_ones(self):
        table, filename, scope = self.table()
        rows = rows_by_title(table)

        self.assertEqual(len(rows), 8)
        self.assertEqual({row["Claim"] for row in rows}, {c.reference for c in (self.a1, self.a2, self.a3, self.a4, self.b1, self.b2, self.c1, self.c2)})
        self.assertNotIn(self.a5.reference, {row["Claim"] for row in rows})
        self.assertEqual(filename, "fleetops-srp-claims-alliance-2026-03")
        self.assertEqual(scope.params, {"year": 2026, "month": 3})

    def test_the_numbers_are_the_ones_of_the_summaries(self):
        rows = rows_by_title(self.table()[0])
        totals = summaries.month_totals(2026, 3)

        self.assertEqual(sum(row["Suggested (ISK)"] for row in rows), totals.requested)
        self.assertEqual(sum(row["Approved (ISK)"] or 0 for row in rows if row["Status"] in ("Approved", "Paid")), totals.approved)
        self.assertEqual(sum(row["Approved (ISK)"] or 0 for row in rows if row["Status"] == "Paid"), totals.paid)
        self.assertEqual(len(rows), totals.claims)

    def test_a_paid_claim_row(self):
        row = next(row for row in rows_by_title(self.table()[0]) if row["Claim"] == self.a1.reference)

        self.assertEqual(row["Pilot (main)"], "Alice Main")
        self.assertEqual(row["Character"], f.main_of(self.alice).character_name)
        self.assertEqual(row["Corporation"], f.DEFAULT_CORP[1])
        self.assertEqual(row["Ship"], f"Ship {data.MUNINN}")
        self.assertEqual((row["Scope"], row["Fleet"], row["FC"]), ("In fleet", self.a1.fleet_label, "First FC"))
        self.assertEqual(row["Value (ISK)"], self.a1.loss_value)
        self.assertEqual((row["Suggested (ISK)"], row["Within limit (ISK)"], row["Approved (ISK)"]), (100 * M, 100 * M, 90 * M))
        self.assertEqual(row["Status"], "Paid")
        self.assertEqual(row["Decided by"], "First FC")
        self.assertEqual(row["Decided at (EVE)"], self.a1.decided_at)
        self.assertEqual(row["Paid at (EVE)"], self.a1.paid_at)

    def test_the_names_of_the_reviewers_are_for_srp_managers_only(self):
        paid = next(row for row in rows_by_title(self.table(self.manager)[0]) if row["Claim"] == self.a1.reference)
        self.assertEqual((paid["Decided by"], paid["Paid by"]), ("First FC", "First FC"))

        for user in (self.viewer, self.corp_manager):
            with self.subTest(user=user.username):
                params = {"corporation": f.DEFAULT_CORP[0]} if user == self.corp_manager else {}
                rows = rows_by_title(self.table(user, **params)[0])
                row = next(row for row in rows if row["Claim"] == self.a1.reference)
                # The decision itself stays; who took it does not.
                self.assertEqual(row["Status"], "Paid")
                self.assertEqual(row["Decided at (EVE)"], self.a1.decided_at)
                self.assertEqual(row["Paid at (EVE)"], self.a1.paid_at)
                self.assertEqual((row["Decided by"], row["Paid by"]), ("", ""))
                self.assertTrue(all(row["Decided by"] == row["Paid by"] == "" for row in rows))

    def test_the_downloaded_file_of_a_stats_role_has_no_reviewer_name(self):
        for user, params in ((self.viewer, {}), (self.corp_manager, {"corporation": f.DEFAULT_CORP[0]})):
            with self.subTest(user=user.username):
                response = self.download(user, "srp-claims", **params)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("First FC", "".join(row[17] + row[21] for row in csv_rows(response)[1:]))

    def test_a_pending_claim_has_no_decision(self):
        row = next(row for row in rows_by_title(self.table()[0]) if row["Claim"] == self.a3.reference)

        self.assertEqual((row["Suggested (ISK)"], row["Within limit (ISK)"], row["Approved (ISK)"]), (30 * M, 20 * M, None))
        self.assertEqual((row["Status"], row["Decided by"], row["Paid by"]), ("Pending", "", ""))
        self.assertEqual((row["Scope"], row["Fleet"]), ("Out of fleet", ""))

    def test_warnings_and_the_fleet_check_are_written_out(self):
        SrpClaim.objects.filter(pk=self.a3.pk).update(
            warnings=[
                {"code": "over_limit", "level": "danger", "params": {"kind": "personal", "requested": 30 * M, "remaining": 10 * M}},
                {"code": "value_pending", "level": "warning", "params": {}},
            ],
            fleet_check="never_seen",
            fleet_check_params={},
        )

        row = next(row for row in rows_by_title(self.table()[0]) if row["Claim"] == self.a3.reference)

        self.assertIn("; ", row["Warnings"])
        self.assertIn("Over the monthly personal limit: 30,000,000 ISK requested, 10,000,000 ISK left.", row["Warnings"])
        self.assertNotIn("over_limit", row["Warnings"])
        self.assertTrue(row["Fleet check"])
        self.assertNotIn("never_seen", row["Fleet check"])

    def test_an_unknown_warning_code_does_not_break_the_file(self):
        SrpClaim.objects.filter(pk=self.a3.pk).update(warnings=[{"code": "from_the_future", "params": {}}, "garbage"])

        row = next(row for row in rows_by_title(self.table()[0]) if row["Claim"] == self.a3.reference)

        self.assertIn("from the future", row["Warnings"].lower())

    def test_a_reason_the_system_wrote_is_in_the_language_of_the_viewer_and_a_typed_one_is_not_changed(self):
        system = texts.SRP_REJECTED_LIMIT_USED_UP(limit=texts.SRP_LIMIT_PERSONAL(), cap="100m ISK")
        SrpClaim.objects.filter(pk=self.a2.pk).update(decision_reason=system)
        SrpClaim.objects.filter(pk=self.a4.pk).update(decision_reason="Wrong ship: see the killmail")

        def reasons(language):
            with translation.override(language):
                table, *_ = self.table()
                position = [column.key for column in table.columns].index("reason")
                names = [column.key for column in table.columns].index("claim")
                return {row[names]: row[position] for row in table.rows}

        english, chinese = reasons("en"), reasons("zh-hans")
        self.assertEqual(english[self.a2.reference], system)
        self.assertEqual(chinese[self.a2.reference], translation_of(system))
        self.assertNotEqual(chinese[self.a2.reference], system)
        self.assertEqual(english[self.a4.reference], "Wrong ship: see the killmail")
        self.assertEqual(chinese[self.a4.reference], "Wrong ship: see the killmail")

    def test_filters(self):
        paid = rows_by_title(self.table(status="paid")[0])
        in_fleet = rows_by_title(self.table(scope="in_fleet")[0])
        both = rows_by_title(self.table(status="approved", scope="in_fleet")[0])

        self.assertEqual({row["Claim"] for row in paid}, {self.a1.reference, self.b2.reference, self.c1.reference})
        self.assertEqual(len(in_fleet), 4)
        self.assertEqual({row["Claim"] for row in both}, {self.b1.reference})

    def test_unknown_filter_values_are_ignored(self):
        table, _filename, scope = self.table(status="withdrawn", scope="everywhere")

        self.assertEqual(len(rows_by_title(table)), 8)
        self.assertEqual(scope.params, {"year": 2026, "month": 3})

    def test_one_corporation(self):
        table, filename, scope = self.table(corporation=str(f.OTHER_CORP[0]))
        rows = rows_by_title(table)

        self.assertEqual({row["Claim"] for row in rows}, {self.c1.reference, self.c2.reference})
        self.assertEqual(filename, f"fleetops-srp-claims-corporation-{f.OTHER_CORP[0]}-2026-03")
        self.assertEqual(scope.params["corporation"], f.OTHER_CORP[0])

    def test_the_month_comes_from_the_request(self):
        self.assertEqual(rows_by_title(self.table(month=4)[0]), [])
        self.assertEqual(rows_by_title(self.table(month=2)[0]), [])

    def test_the_file_says_what_it_counts(self):
        table, *_ = self.table(status="paid")
        about = dict(table.about)

        self.assertEqual(about["Dataset"], "SRP claims")
        self.assertEqual(about["Scope"], "Alliance")
        self.assertEqual(about["Month (EVE time)"], "2026-03")
        self.assertEqual(about["Filters"], "Status: Paid")
        self.assertIn("EVE month of the loss", about["Counting rule"])

    def test_a_name_that_looks_like_a_formula_is_defused(self):
        formula = '=HYPERLINK("http://evil.example")'
        SrpClaim.objects.filter(pk=self.a2.pk).update(main_character_name=formula)

        response = self.download(self.manager, "srp-claims")
        rows = csv_rows(response)

        cells = [cell for row in rows for cell in row]
        self.assertIn("'" + formula, cells)
        self.assertNotIn(formula, cells)

    def test_the_csv_is_a_download_with_the_header_row(self):
        response = self.download(self.manager, "srp-claims")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("fleetops-srp-claims-alliance-2026-03.csv", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        rows = csv_rows(response)
        self.assertEqual(rows[0][:3], ["Claim", "Loss time (EVE)", "Pilot (main)"])
        self.assertEqual(len(rows), 9)

    def test_the_workbook_has_the_rows_and_the_about_sheet(self):
        response = self.download(self.manager, "srp-claims", fmt="xlsx")

        self.assertEqual(response.status_code, 200)
        sheet = workbook_rows(response)
        self.assertEqual(len(sheet), 9)
        about = {row[0]: row[1] for row in workbook_rows(response, "xl/worksheets/sheet2.xml") if len(row) > 1}
        self.assertEqual(about["Dataset"], "SRP claims")

    def test_the_export_limit_cuts_the_file_and_says_so(self):
        with mock.patch.object(exports, "MAX_EXPORT_ROWS", 3):
            rows = csv_rows(self.download(self.manager, "srp-claims"))
            note = exports.truncation_note()

        self.assertEqual(len(rows), 1 + 3 + 1)
        self.assertEqual(rows[-1], [note])

    def test_the_number_of_queries_does_not_grow_with_the_claims(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                response = self.download(self.manager, "srp-claims")
                csv_rows(response)
            return len(captured)

        queries()
        small = queries()
        self.grow(5)
        large = queries()

        self.assertEqual(small, large)


class OtherFilesTests(ExportTestCase):
    def test_members(self):
        table, filename, _scope = build("srp-members", self.manager, **PARAMS)
        rows = rows_by_title(table)
        expected = summaries.by_member(2026, 3)

        self.assertEqual(filename, "fleetops-srp-members-alliance-2026-03")
        self.assertEqual([row["Main character"] for row in rows], [row.main_character_name for row in expected])
        for row, source in zip(rows, expected):
            self.assertEqual(row["Corporation"], source.corporation_name)
            self.assertEqual(
                [row[title] for title in ("Claims", "Pending", "Approved claims", "Paid claims", "Rejected")],
                [source.totals.claims, source.totals.pending, source.totals.approved_count, source.totals.paid_count, source.totals.rejected],
            )
            self.assertEqual(
                [row[title] for title in ("Requested (ISK)", "Approved (ISK)", "Paid (ISK)", "Outstanding (ISK)")],
                [source.totals.requested, source.totals.approved, source.totals.paid, source.totals.outstanding],
            )

    def test_members_of_one_corporation(self):
        table, filename, _scope = build("srp-members", self.corp_manager, corporation=str(f.DEFAULT_CORP[0]), **PARAMS)

        self.assertEqual([row["Main character"] for row in rows_by_title(table)], ["Alice Main", "Bob Main"])
        self.assertEqual(filename, f"fleetops-srp-members-corporation-{f.DEFAULT_CORP[0]}-2026-03")
        self.assertEqual(dict(table.about)["Scope"], f"Corporation {f.DEFAULT_CORP[1]}")

    def test_corporations_with_a_total_row_that_matches_the_page(self):
        table, filename, _scope = build("srp-corporations", self.viewer, **PARAMS)
        rows = rows_by_title(table)
        summary = summaries.alliance(2026, 3)

        self.assertEqual(filename, "fleetops-srp-corporations-alliance-2026-03")
        self.assertEqual([row["Corporation"] for row in rows], [f.DEFAULT_CORP[1], f.OTHER_CORP[1], "Total"])
        corp1, corp2, total = rows
        self.assertEqual((corp1["Claims"], corp1["Requested (ISK)"], corp1["Approved (ISK)"], corp1["Paid (ISK)"]), (6, 255 * M, 205 * M, 115 * M))
        self.assertEqual((corp1["Monthly cap (ISK)"], corp1["Used of the cap (ISK)"], corp1["Remaining (ISK)"]), (500 * M, 205 * M, 295 * M))
        self.assertEqual((corp2["Monthly cap (ISK)"], corp2["Remaining (ISK)"]), (100 * M, 40 * M))
        self.assertEqual(
            (total["Claims"], total["Requested (ISK)"], total["Approved (ISK)"], total["Paid (ISK)"], total["Outstanding (ISK)"]),
            (summary.totals.claims, summary.totals.requested, summary.totals.approved, summary.totals.paid, summary.totals.outstanding),
        )
        self.assertIsNone(total["Monthly cap (ISK)"])

    def test_fcs_with_a_total_row_that_matches_the_page(self):
        table, filename, _scope = build("srp-fcs", self.manager, **PARAMS)
        rows = rows_by_title(table)

        self.assertEqual(filename, "fleetops-srp-fcs-alliance-2026-03")
        self.assertEqual([row["FC (main)"] for row in rows], ["First FC", "Second FC", "Total"])
        first, second, total = rows
        self.assertEqual((first["Fleets"], first["Claims"], first["Approved (ISK)"]), (1, 2, 130 * M))
        self.assertEqual((second["Fleets"], second["Claims"], second["Paid (ISK)"]), (1, 2, 25 * M))
        in_fleet = summaries.month_totals(2026, 3, scope="in_fleet")
        self.assertEqual((total["Claims"], total["Approved (ISK)"], total["Outstanding (ISK)"]), (in_fleet.claims, in_fleet.approved, in_fleet.outstanding))
        self.assertEqual(total["Fleets"], 2)

    def test_an_empty_month_has_the_header_and_the_total(self):
        table, *_ = build("srp-corporations", self.manager, year=2025, month=1)

        self.assertEqual([row["Corporation"] for row in rows_by_title(table)], ["Total"])
        table, *_ = build("srp-members", self.manager, year=2025, month=1)
        self.assertEqual(list(table.rows), [])


class AuditTests(ExportTestCase):
    def test_a_download_is_audited_with_its_parameters(self):
        self.download(self.manager, "srp-claims", status="paid", corporation=f.DEFAULT_CORP[0])

        entry = AuditLog.objects.get(action="srp.export")
        self.assertEqual(entry.actor, self.manager)
        self.assertEqual(entry.object_type, "SrpSummary")
        self.assertEqual(entry.object_id, f"srp-claims:corporation-{f.DEFAULT_CORP[0]}:2026-03")
        self.assertEqual(
            entry.new_value,
            {"format": "csv", "params": {"year": 2026, "month": 3, "corporation": f.DEFAULT_CORP[0], "status": "paid"}},
        )
        self.assertFalse(AuditLog.objects.filter(action="statistics.export").exists())

    def test_every_dataset_and_format_is_audited(self):
        for slug in SRP_SLUGS:
            for fmt in ("csv", "xlsx"):
                self.download(self.viewer, slug, fmt=fmt)

        entries = AuditLog.objects.filter(action="srp.export")
        self.assertEqual(entries.count(), 8)
        self.assertEqual({entry.new_value["format"] for entry in entries}, {"csv", "xlsx"})

    def test_a_statistics_download_still_has_its_own_entry(self):
        viewer = f.create_user("exp_stats", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])
        self.client.force_login(viewer)

        self.client.get(reverse("fleetops:export", args=["fcs"]), {"format": "csv", **PARAMS})

        self.assertTrue(AuditLog.objects.filter(action="statistics.export").exists())
        self.assertFalse(AuditLog.objects.filter(action="srp.export").exists())

    def test_the_entry_holds_no_names_or_links(self):
        self.download(self.manager, "srp-claims")

        entry = AuditLog.objects.get(action="srp.export")
        self.assertNotIn("Alice", str(entry.new_value))
        self.assertNotIn("http", str(entry.new_value))

    def test_an_unknown_format_or_dataset_is_a_404(self):
        self.assertEqual(self.download(self.manager, "srp-claims", fmt="pdf").status_code, 404)
        self.assertEqual(self.download(self.manager, "srp-nothing").status_code, 404)
        self.assertFalse(AuditLog.objects.filter(action="srp.export").exists())


class PaymentDateTests(ExportTestCase):
    """The paid time of a claim is a column of the claims file, so a payment can be found by its date too."""

    def test_paid_claims_carry_their_payment_time(self):
        table, *_ = build("srp-claims", self.manager, status="paid", **PARAMS)
        rows = rows_by_title(table)

        self.assertEqual({row["Paid at (EVE)"] for row in rows}, {data.at(20)})
        lower, upper = day_bounds(date(2026, 3, 20), date(2026, 3, 20))
        paid = SrpClaim.objects.filter(status=Status.PAID, paid_at__gte=lower, paid_at__lt=upper)
        self.assertEqual({claim.reference for claim in paid}, {row["Claim"] for row in rows})
