# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Months without an applicable monthly target are not assessed (owner decision 5-5), and the notes of the
statistics pages: FB11.2 (no target), FB11.3 (registered after the month), FB11.4 (two decimals of FC points).
"""

import io
import zipfile
from datetime import datetime, timezone as dt_timezone
from unittest import mock
from xml.etree import ElementTree

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils import translation

from fleetops.services.assessment import corporation_status, member_status, month_target
from fleetops.services.periods import month_ref

from . import factories as f

UTC = dt_timezone.utc
MONTH = (2026, 3)
TARGETS = reverse("fleetops:configuration_list", args=["statistics-targets"])
NO_TARGET_MEMBERS = "No target for this month: nobody is assessed or listed as inactive."
NO_TARGET_CORPORATIONS = "No monthly target applies to this month, so corporations and members are not assessed."


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def clock(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


class UnassessedFixture(TestCase):
    """Three members of one corporation, one of them without PAP, and a fleet in March 2026."""

    def setUp(self):
        patcher = clock(utc(2026, 4, 15, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fc = f.member("fc", main_name="Example FC", perms=f.FC_PERMS, active=False)
        self.alice = f.member("alice", main_name="Pilot A", perms=f.MEMBER_PERMS)
        self.bob = f.member("bob", main_name="Pilot B", perms=f.MEMBER_PERMS)
        self.carol = f.member("carol", main_name="Pilot C", perms=f.MEMBER_PERMS)
        operation = f.closed_operation(self.fc, utc(2026, 3, 5, 18))
        f.add_attendance(operation, self.alice, value=3)
        f.add_attendance(operation, self.bob, value=1)
        self.stats = f.member("stats", main_name="Stats Lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.admin = f.member(
            "admin",
            main_name="Admin One",
            perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.manage_configuration"],
        )
        self.manager = f.member("manager", main_name="Corp Manager", perms=f.CORP_MANAGEMENT_PERMS)

    def get(self, user, name, *args, now=None, **params):
        with clock(now or utc(2026, 4, 15, 12)):
            self.client.force_login(user)
            return self.client.get(reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params})


class NoTargetPagesTests(UnassessedFixture):
    def test_the_members_page_has_no_colours_and_nobody_is_inactive(self):
        response = self.get(self.stats, "alliance_members")
        page = response.content.decode()

        self.assertEqual(response.context["counts"]["inactive"], 0)
        self.assertContains(response, NO_TARGET_MEMBERS)
        for marker in ("fo-pill-bad", "fo-pill-warn", "fo-pill-ok", "fleetops-status-row"):
            self.assertNotIn(marker, page)
        # The count line names no "below minimum" figure either (the filter option keeps its words).
        self.assertNotIn("</strong> below minimum", page)
        self.assertEqual({item["row"].status for item in response.context["items"]}, {"none"})

    def test_the_inactive_list_is_empty(self):
        response = self.get(self.stats, "alliance_members", status="inactive")

        self.assertEqual(response.context["items"], [])
        self.assertContains(response, "Nobody is below the member minimum.")

    def test_only_a_configuration_manager_is_offered_the_targets(self):
        self.assertContains(self.get(self.admin, "alliance_members"), TARGETS)
        self.assertNotContains(self.get(self.stats, "alliance_members"), TARGETS)

    def test_the_corporation_page_and_the_overview_say_nobody_is_assessed(self):
        corporation = self.get(self.manager, "corporation_statistics")
        overview = self.get(self.stats, "all_corporation_statistics")

        for response in (corporation, overview):
            self.assertContains(response, NO_TARGET_CORPORATIONS)
            self.assertNotContains(response, "fleetops-status-row")
            self.assertNotContains(response, "fo-pill-bad")
            self.assertNotContains(response, "fo-pill-warn")
        self.assertEqual(corporation.context["stats"]["mains_below_minimum"], 0)
        self.assertEqual(overview.context["below_total"], 0)
        self.assertNotContains(corporation, "listed below a minimum")

    def test_the_corporation_notice_links_the_targets_for_administrators_only(self):
        admin_page = self.get(self.admin, "corporation_statistics_detail", f.DEFAULT_CORP[0])
        manager_page = self.get(self.manager, "corporation_statistics")

        self.assertContains(admin_page, TARGETS)
        self.assertNotContains(manager_page, TARGETS)

    def test_the_member_page_says_there_is_no_target(self):
        response = self.get(self.stats, "member_detail", self.alice.pk)

        self.assertEqual(response.context["detail"].target.minimum, 0)
        self.assertContains(response, "No target for this month")
        self.assertNotContains(response, "Minimum 0 PAP")

    def test_my_statistics_says_there_is_no_target(self):
        response = self.get(self.carol, "my_statistics")

        self.assertContains(response, "No target for this month")
        self.assertEqual(response.context["minimum_status"], "none")

    def test_a_target_applies_from_its_month_only(self):
        f.statistics_target(2026, 3, "1.00", minimum=2)

        before = self.get(self.stats, "alliance_members", year=2026, month=2)
        within = self.get(self.stats, "alliance_members")

        self.assertContains(before, NO_TARGET_MEMBERS)
        self.assertNotContains(within, NO_TARGET_MEMBERS)
        # Only Pilot A reaches two PAP; the other five members (Pilot B, Pilot C and the three staff accounts)
        # are below it in the finished month.
        self.assertEqual(within.context["counts"]["inactive"], 5)
        self.assertContains(within, "fo-pill-bad")

    def test_a_running_month_without_a_target_is_not_amber(self):
        response = self.get(self.stats, "alliance_members", now=utc(2026, 3, 20, 12))

        self.assertNotContains(response, "fo-pill-warn")
        self.assertNotContains(response, "fleetops-status-row-progress")

    def test_the_services_agree(self):
        target = month_target(*MONTH)

        self.assertEqual((target.average, target.minimum, target.rule), (None, 0, None))
        self.assertEqual(member_status(0, target, month_ref(*MONTH, utc(2026, 4, 15))), "none")
        self.assertEqual(corporation_status(0, 3, target, month_ref(*MONTH, utc(2026, 4, 15))), "none")


class NoTargetExportsTests(UnassessedFixture):
    def download(self, user, dataset, fmt="xlsx", **params):
        with clock(utc(2026, 4, 15, 12)):
            self.client.force_login(user)
            return self.client.get(reverse("fleetops:export", args=[dataset]), {"year": 2026, "month": 3, "format": fmt, **params})

    def about(self, response):
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        rows = []
        for row in root.iter(f"{main}row"):
            cells = []
            for cell in row:
                number = cell.find(f"{main}v")
                cells.append("".join(t.text or "" for t in cell.iter(f"{main}t")) if number is None else number.text)
            rows.append(cells)
        return rows

    def test_the_members_file_prints_no_target(self):
        rows = self.about(self.download(self.stats, "members"))

        self.assertIn(["Member minimum PAP", "No target"], rows)
        self.assertIn(["Corporation average target (PAP per main)", "No target"], rows)

    def test_the_corporations_file_prints_no_target(self):
        rows = self.about(self.download(self.stats, "corporations"))

        self.assertIn(["Member minimum PAP", "No target"], rows)

    def test_a_month_with_a_target_prints_the_minimum(self):
        f.statistics_target(2026, 3, "1.00", minimum=2)

        rows = self.about(self.download(self.stats, "members"))

        self.assertIn(["Member minimum PAP", "2"], rows)
        self.assertNotIn(["Member minimum PAP", "No target"], rows)


class MyStatisticsNotesTests(UnassessedFixture):
    """FB11.3 and FB11.4."""

    def test_a_member_who_registered_after_the_month_sees_the_sentence_not_a_dash(self):
        f.statistics_target(2026, 1, minimum=2)
        get_user_model().objects.filter(pk=self.carol.pk).update(date_joined=utc(2026, 10, 14, 9))

        response = self.get(self.carol, "my_statistics", now=utc(2026, 10, 20, 12), year=2026, month=9)

        self.assertContains(response, "Not assessed: you registered after this month.")
        self.assertNotContains(response, "No member minimum applies to this month.")

    def test_the_registration_month_itself_is_assessed(self):
        f.statistics_target(2026, 1, minimum=2)
        get_user_model().objects.filter(pk=self.carol.pk).update(date_joined=utc(2026, 10, 14, 9))

        response = self.get(self.carol, "my_statistics", now=utc(2026, 10, 20, 12), year=2026, month=10)

        self.assertNotContains(response, "Not assessed: you registered after this month.")
        self.assertContains(response, "2 PAP")

    def test_fc_points_have_two_decimals(self):
        fleet_type = f.fleet_type("Fractions", "4.99")
        operation = f.closed_operation(self.alice, utc(2026, 3, 6, 18), type_obj=fleet_type)
        f.add_attendance(operation, self.bob)

        page = self.get(self.alice, "my_statistics").content.decode()

        self.assertIn('<div class="fo-metric-value">4.99</div>', page)

    def test_whole_fc_points_show_two_decimals_too(self):
        fleet_type = f.fleet_type("Whole", "5.00")
        operation = f.closed_operation(self.alice, utc(2026, 3, 6, 18), type_obj=fleet_type)
        f.add_attendance(operation, self.bob)

        page = self.get(self.alice, "my_statistics").content.decode()

        self.assertIn('<div class="fo-metric-value">5.00</div>', page)


@tag("catalog")
@override_settings(LANGUAGE_CODE="zh-hans")
class ChineseNotesTests(UnassessedFixture):
    """The notes in the viewer's language (needs the compiled catalog)."""

    def get_zh(self, user, name, *args, **params):
        with clock(utc(2026, 4, 15, 12)), translation.override("zh-hans"):
            self.client.force_login(user)
            return self.client.get(
                reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params}, HTTP_ACCEPT_LANGUAGE="zh-hans"
            )

    def test_the_no_target_notices_are_chinese(self):
        members = self.get_zh(self.stats, "alliance_members")
        overview = self.get_zh(self.stats, "all_corporation_statistics")

        self.assertNotContains(members, NO_TARGET_MEMBERS)
        self.assertContains(members, "本月没有达标线")
        self.assertNotContains(overview, NO_TARGET_CORPORATIONS)
        self.assertContains(overview, "本月没有适用的月度达标线")

    def test_the_footer_labels_and_the_two_notes_are_chinese(self):
        overview = self.get_zh(self.stats, "all_corporation_statistics")

        self.assertContains(overview, "全部计入的 PAP")
        self.assertContains(overview, "仅当前大号")
        self.assertContains(overview, "联盟行：")
        self.assertContains(overview, "军团行：PAP 是该军团在本月飞出的全部 PAP，包括之后才注册的飞行员")
        self.assertNotContains(overview, "Corporation rows: PAP is every PAP")

    def test_the_registered_later_sentence_is_chinese(self):
        f.statistics_target(2026, 1, minimum=2)
        get_user_model().objects.filter(pk=self.carol.pk).update(date_joined=utc(2026, 10, 14, 9))
        with clock(utc(2026, 10, 20, 12)), translation.override("zh-hans"):
            self.client.force_login(self.carol)
            response = self.client.get(
                reverse("fleetops:my_statistics"), {"year": 2026, "month": 9}, HTTP_ACCEPT_LANGUAGE="zh-hans"
            )

        self.assertNotContains(response, "Not assessed: you registered after this month.")
        self.assertContains(response, "不评估：你在本月之后才注册。")
