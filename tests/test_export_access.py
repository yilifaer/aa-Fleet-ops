# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for who may download which statistics export and for the scope each check returns."""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import RequestFactory, TestCase

from fleetops.models import MAX_DATABASE_ID, AuditLog
from fleetops.services import export_datasets
from fleetops.services.export_datasets import DATASETS, ExportScope, export_for_request, get_dataset

from . import factories as f

MEMBER = f.MEMBER_PERMS
CORP = f.CORP_MANAGEMENT_PERMS
ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
INCENTIVES = f.MEMBER_PERMS + ["fleetops.manage_incentives"]
NO_ACCESS = []
# Ids the database cannot hold: one past the largest and a number of thirty digits.
OUT_OF_RANGE = (str(MAX_DATABASE_ID + 1), "9" * 30)


def request_for(viewer, **params):
    request = RequestFactory().get("/export/", params)
    request.user = viewer
    return request


def check(slug, viewer, **params):
    return get_dataset(slug).check(request_for(viewer, **params))


class DatasetRegistryTests(TestCase):
    def test_datasets(self):
        self.assertEqual(
            set(DATASETS),
            {
                "corporations", "members", "member-fleets", "fcs", "attendance", "incentives",
                # FleetOps SRP; tests/test_srp_exports.py covers their access rules.
                "srp-claims", "srp-members", "srp-corporations", "srp-fcs",
            },
        )
        for slug, dataset in DATASETS.items():
            self.assertEqual(dataset.slug, slug)

    def test_there_is_no_afat_dataset(self):
        self.assertIsNone(get_dataset("afat"))

    def test_unknown_dataset(self):
        self.assertIsNone(get_dataset("everything"))

    def test_builders_return_a_table_and_an_ascii_file_name(self):
        lead = f.member("lead", perms=ALL_STATS + INCENTIVES)
        f.settings(incentive_enabled=True)
        from fleetops.models import IncentivePeriod

        period = IncentivePeriod.objects.create(year=2026, month=9)
        calls = {
            "corporations": {"year": 2026, "month": 10},
            "members": {"year": 2026, "month": 10},
            "member-fleets": {"user": lead.pk, "year": 2026, "month": 10},
            "fcs": {"year": 2026, "month": 10},
            "attendance": {"scope": "alliance", "year": 2026, "month": 10},
            "incentives": {"period": period.pk},
        }
        for slug, params in calls.items():
            with self.subTest(slug=slug):
                dataset = get_dataset(slug)
                request = request_for(lead, **params)
                table, filename = dataset.build(request, dataset.check(request))
                self.assertTrue(table.columns)
                self.assertTrue(filename.isascii())
                self.assertTrue(filename.startswith(f"fleetops-{slug}-"), filename)


class CorporationsAccessTests(TestCase):
    def test_needs_view_all_stats(self):
        for perms in (NO_ACCESS, MEMBER, CORP, INCENTIVES):
            with self.subTest(perms=perms), self.assertRaises(PermissionDenied):
                check("corporations", f.member(perms=perms))

    def test_scope(self):
        scope = check("corporations", f.member("lead", perms=ALL_STATS), year="2026", month="3", sort="-total")
        self.assertEqual((scope.year, scope.month), (2026, 3))
        self.assertEqual(scope.label, "alliance")
        self.assertEqual(scope.params, {"year": 2026, "month": 3, "sort": "-total"})
        self.assertEqual(scope.object_id, "corporations:alliance:2026-03")

    def test_bad_month_means_the_current_month(self):
        lead = f.member("lead", perms=ALL_STATS)
        scope = check("corporations", lead, year="x", month="99")
        self.assertEqual(scope.period_label, f"{scope.year:04d}-{scope.month:02d}")


class MembersAccessTests(TestCase):
    def setUp(self):
        self.lead = f.member("lead", perms=ALL_STATS)
        self.manager = f.member("manager", perms=CORP)
        self.plain = f.member("plain", perms=MEMBER)

    def test_alliance_needs_view_all_stats(self):
        self.assertEqual(check("members", self.lead, year="2026", month="3").label, "alliance")
        for user in (self.manager, self.plain):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                check("members", user)

    def test_corporation_manager_may_export_their_own_corporation(self):
        scope = check("members", self.manager, corporation=str(f.DEFAULT_CORP[0]), status="inactive", q="ali")
        self.assertEqual(scope.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(scope.label, f"corporation-{f.DEFAULT_CORP[0]}")
        self.assertEqual(scope.params["status"], "inactive")
        self.assertEqual(scope.params["q"], "ali")
        self.assertEqual(scope.params["corporation"], f.DEFAULT_CORP[0])

    def test_corporation_manager_cannot_export_another_corporation(self):
        with self.assertRaises(Http404):
            check("members", self.manager, corporation=str(f.OTHER_CORP[0]))

    def test_view_all_stats_may_export_any_corporation(self):
        scope = check("members", self.lead, corporation=str(f.OTHER_CORP[0]))
        self.assertEqual(scope.corporation_id, f.OTHER_CORP[0])

    def test_member_cannot_export_a_corporation(self):
        with self.assertRaises(PermissionDenied):
            check("members", self.plain, corporation=str(f.DEFAULT_CORP[0]))

    def test_a_manager_without_a_main_gets_nothing(self):
        manager = f.member("nomain", perms=CORP, with_main=False)
        with self.assertRaises(Http404):
            check("members", manager, corporation=str(f.DEFAULT_CORP[0]))

    def test_an_unreadable_corporation_means_the_alliance(self):
        with self.assertRaises(PermissionDenied):
            check("members", self.manager, corporation="abc")
        self.assertEqual(check("members", self.lead, corporation="abc").label, "alliance")


    def test_a_corporation_id_beyond_the_database_range_is_unreadable(self):
        for value in OUT_OF_RANGE:
            with self.subTest(value=value):
                with self.assertRaises(PermissionDenied):
                    check("members", self.manager, corporation=value)
                scope = check("members", self.lead, corporation=value)
                self.assertEqual((scope.label, scope.corporation_id), ("alliance", None))
                self.assertNotIn("corporation", scope.params)


class MemberFleetsAccessTests(TestCase):
    def setUp(self):
        self.target = f.member("target", perms=MEMBER)
        self.colleague = f.member("colleague", perms=CORP)
        self.elsewhere = f.member("elsewhere", perms=CORP, corporation=f.OTHER_CORP)
        self.lead = f.member("lead", perms=ALL_STATS)

    def test_own_fleets(self):
        scope = check("member-fleets", self.target, user=str(self.target.pk), year="2026", month="3")
        self.assertEqual(scope.user, self.target)
        self.assertEqual(scope.label, f"user-{self.target.pk}")
        self.assertEqual(scope.params, {"user": self.target.pk, "year": 2026, "month": 3})

    def test_corporation_manager_and_view_all_stats(self):
        self.assertEqual(check("member-fleets", self.colleague, user=str(self.target.pk)).user, self.target)
        self.assertEqual(check("member-fleets", self.lead, user=str(self.target.pk)).user, self.target)

    def test_everybody_else_gets_a_404(self):
        with self.assertRaises(Http404):
            check("member-fleets", self.elsewhere, user=str(self.target.pk))
        with self.assertRaises(Http404):
            check("member-fleets", f.member("plain", perms=MEMBER), user=str(self.target.pk))

    def test_missing_unreadable_or_unknown_users(self):
        for params in ({}, {"user": "abc"}, {"user": "999999"}):
            with self.subTest(params=params), self.assertRaises(Http404):
                check("member-fleets", self.lead, **params)

    def test_a_user_id_beyond_the_database_range_has_no_export(self):
        for value in (*OUT_OF_RANGE, str(MAX_DATABASE_ID)):
            with self.subTest(value=value), self.assertRaises(Http404):
                check("member-fleets", self.lead, user=value)

    def test_a_departed_member_has_no_export(self):
        leaver = f.member("leaver", alliance=f.FOREIGN_ALLIANCE)
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        with self.assertRaises(Http404):
            check("member-fleets", self.lead, user=str(leaver.pk))


class FcsAccessTests(TestCase):
    def test_needs_view_all_stats(self):
        for perms in (NO_ACCESS, MEMBER, CORP):
            with self.subTest(perms=perms), self.assertRaises(PermissionDenied):
                check("fcs", f.member(perms=perms))
        self.assertEqual(check("fcs", f.member("lead", perms=ALL_STATS), year="2026", month="3").label, "alliance")


class AttendanceAccessTests(TestCase):
    def setUp(self):
        self.plain = f.member("plain", perms=MEMBER)
        self.manager = f.member("manager", perms=CORP)
        self.lead = f.member("lead", perms=ALL_STATS)

    def test_me_needs_basic_access(self):
        self.assertEqual(check("attendance", self.plain).label, "me")
        self.assertEqual(check("attendance", self.plain, scope="me").label, "me")
        with self.assertRaises(PermissionDenied):
            check("attendance", f.member("outsider", perms=NO_ACCESS))

    def test_corporation_is_the_users_own(self):
        scope = check("attendance", self.manager, scope="corporation", year="2026", month="3")
        self.assertEqual(scope.corporation_id, f.DEFAULT_CORP[0])
        self.assertEqual(scope.label, "corporation")
        with self.assertRaises(PermissionDenied):
            check("attendance", self.plain, scope="corporation")

    def test_corporation_without_a_main(self):
        manager = f.member("nomain", perms=CORP, with_main=False)
        with self.assertRaises(Http404):
            check("attendance", manager, scope="corporation")

    def test_alliance_needs_view_all_stats(self):
        self.assertEqual(check("attendance", self.lead, scope="alliance").label, "alliance")
        for user in (self.plain, self.manager):
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                check("attendance", user, scope="alliance")

    def test_unknown_scope(self):
        with self.assertRaises(Http404):
            check("attendance", self.lead, scope="world")

    def test_without_a_month_the_whole_retention_window_is_exported(self):
        scope = check("attendance", self.plain)
        self.assertIsNone(scope.year)
        self.assertIsNone(scope.month)
        self.assertEqual(scope.period_label, "all")
        self.assertEqual(scope.object_id, "attendance:me:all")
        self.assertEqual(scope.params, {"scope": "me"})

    def test_with_a_month(self):
        scope = check("attendance", self.plain, scope="me", year="2026", month="3")
        self.assertEqual((scope.year, scope.month), (2026, 3))
        self.assertEqual(scope.params, {"scope": "me", "year": 2026, "month": 3})


class IncentivesAccessTests(TestCase):
    def setUp(self):
        from fleetops.models import IncentivePeriod

        self.period = IncentivePeriod.objects.create(year=2026, month=9)
        self.manager = f.member("manager", perms=INCENTIVES)

    def test_needs_the_permission(self):
        f.settings(incentive_enabled=True)
        for perms in (NO_ACCESS, MEMBER, ALL_STATS):
            with self.subTest(perms=perms), self.assertRaises(PermissionDenied):
                check("incentives", f.member(perms=perms), period=str(self.period.pk))

    def test_incentives_must_be_enabled(self):
        f.settings(incentive_enabled=False)
        with self.assertRaises(Http404):
            check("incentives", self.manager, period=str(self.period.pk))

    def test_scope(self):
        f.settings(incentive_enabled=True)
        scope = check("incentives", self.manager, period=str(self.period.pk))
        self.assertEqual(scope.period, self.period)
        self.assertEqual((scope.year, scope.month), (2026, 9))
        self.assertEqual(scope.object_id, "incentives:alliance:2026-09")
        self.assertEqual(scope.params, {"period": self.period.pk})

    def test_unknown_period(self):
        f.settings(incentive_enabled=True)
        for params in ({}, {"period": "abc"}, {"period": "999999"}):
            with self.subTest(params=params), self.assertRaises(Http404):
                check("incentives", self.manager, **params)

    def test_a_period_id_beyond_the_database_range_is_unknown(self):
        f.settings(incentive_enabled=True)
        for value in (*OUT_OF_RANGE, str(MAX_DATABASE_ID)):
            with self.subTest(value=value), self.assertRaises(Http404):
                check("incentives", self.manager, period=value)


class ExportForRequestTests(TestCase):
    def setUp(self):
        self.lead = f.member("lead", perms=ALL_STATS)

    def test_csv_download_with_an_audit_entry(self):
        response = export_for_request(request_for(self.lead, year="2026", month="3"), "corporations", "csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("fleetops-corporations-alliance-2026-03.csv", response["Content-Disposition"])
        entry = AuditLog.objects.get(action="statistics.export")
        self.assertEqual(entry.actor, self.lead)
        self.assertEqual(entry.object_type, "Statistics")
        self.assertEqual(entry.object_id, "corporations:alliance:2026-03")
        self.assertEqual(entry.new_value, {"format": "csv", "params": {"year": 2026, "month": 3}})

    def test_free_text_in_the_audit_entry_is_cut(self):
        export_for_request(request_for(self.lead, year="2026", month="3", q="x" * 3000), "members", "csv")

        entry = AuditLog.objects.get(action="statistics.export")

        self.assertEqual(entry.new_value["params"]["q"], "x" * 100)

    def test_every_text_parameter_is_cut_to_the_limit(self):
        params = export_datasets._params(q="y" * 3000, scope="me", year=2026, empty="", missing=None)

        self.assertEqual(params, {"q": "y" * export_datasets.PARAM_TEXT_LIMIT, "scope": "me", "year": 2026})
        self.assertEqual(export_datasets.PARAM_TEXT_LIMIT, 100)

    def test_xlsx_download(self):
        response = export_for_request(request_for(self.lead, year="2026", month="3"), "fcs", "xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].endswith("spreadsheetml.sheet"))
        self.assertIn(".xlsx", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"PK"))

    def test_unknown_dataset_and_format(self):
        with self.assertRaises(Http404):
            export_for_request(request_for(self.lead), "afat", "csv")
        with self.assertRaises(Http404):
            export_for_request(request_for(self.lead), "fcs", "pdf")
        self.assertFalse(AuditLog.objects.filter(action="statistics.export").exists())

    def test_a_refused_download_leaves_no_audit_entry(self):
        with self.assertRaises(PermissionDenied):
            export_for_request(request_for(f.member("plain", perms=MEMBER)), "corporations", "csv")
        self.assertFalse(AuditLog.objects.filter(action="statistics.export").exists())


class ExportScopeTests(TestCase):
    def test_period_label(self):
        self.assertEqual(ExportScope("members", "alliance", 2026, 3).period_label, "2026-03")
        self.assertEqual(ExportScope("members", "alliance").period_label, "all")
        self.assertEqual(ExportScope("members", "alliance", 2026, 12).object_id, "members:alliance:2026-12")
