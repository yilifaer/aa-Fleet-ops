# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Group fleets in the statistics (G15) and their pill on the history rows (G14).

Group fleets are fleets: their PAP counts like the PAP of any fleet, by the fleet type's own rules, in the
member, corporation and leaderboard figures, in FC statistics and in the exports. A ping without a fleet
creates nothing that counts.
"""

import csv
import io
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import AttendanceRecord, FleetOperation, GroupAccess
from fleetops.services import members as member_service
from fleetops.services import statistics
from fleetops.services.export_datasets import export_for_request

from . import factories as f
from .test_export_datasets import request_for

UTC = dt_timezone.utc
MONTH = (2026, 3)
FLEETS = GroupAccess.Mode.FLEETS


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def clock(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


class GroupStatisticsBase(TestCase):
    """An alliance fleet and a group fleet (a type of weight 0) in March 2026, flown by the same pilots."""

    def setUp(self):
        patcher = clock(utc(2026, 4, 15, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.strat = f.fleet_type("StratOp", "2.00")
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.fc = f.member("fc", main_name="Example FC", perms=f.FC_PERMS, active=False)
        self.leader = f.member("leader", main_name="Pilot L", perms=f.MEMBER_PERMS)
        self.alice = f.member("alice", main_name="Pilot A", perms=f.MEMBER_PERMS)
        self.bob = f.member("bob", main_name="Pilot B", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        self.viewer = f.member(
            "viewer", main_name="Stats Lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats", "fleetops.view_all_fleets"]
        )
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader])
        f.group_access(self.group, FLEETS, types=[self.activity])
        self.alliance_op = f.closed_operation(self.fc, utc(2026, 3, 5, 18), type_obj=self.strat)
        f.add_attendance(self.alliance_op, self.alice, value=2)
        f.add_attendance(self.alliance_op, self.bob, value=1)
        self.group_op = f.closed_operation(
            self.leader, utc(2026, 3, 9, 19), type_obj=self.activity, group=self.group, group_name=self.group.name
        )
        f.add_attendance(self.group_op, self.alice, value=3)
        f.add_attendance(self.group_op, self.bob, value=2)

    def get(self, user, name, *args, **params):
        with clock(utc(2026, 4, 15, 12)):
            self.client.force_login(user)
            return self.client.get(reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params})


class GroupFleetsCountTests(GroupStatisticsBase):
    def test_the_pap_of_a_group_fleet_counts_for_the_member(self):
        stats = statistics.member_statistics(self.alice, *MONTH)

        self.assertEqual((stats["total"], stats["unique_fleets"]), (5, 2))
        self.assertEqual(stats["breakdown"], {"StratOp": 2, "Group Activity": 3})

    def test_it_counts_in_the_corporation_figures(self):
        own = statistics.corporation_statistics(f.DEFAULT_CORP[0], *MONTH)
        other = statistics.corporation_statistics(f.OTHER_CORP[0], *MONTH)

        self.assertEqual((own["total_pap"], own["fleets"]), (5, 2))
        self.assertEqual((other["total_pap"], other["fleets"]), (3, 2))
        self.assertEqual(own["breakdown"], {"StratOp": 2, "Group Activity": 3})

    def test_it_counts_in_the_overview_and_the_member_lists(self):
        overview = statistics.corporation_overview(*MONTH)
        table = member_service.member_rows(*MONTH)
        rows = {row.main_character_name: row for row in table.rows}

        self.assertEqual(overview.alliance_pap, 8)
        self.assertEqual((rows["Pilot A"].pap, rows["Pilot A"].fleets), (5, 2))
        self.assertEqual((rows["Pilot B"].pap, rows["Pilot B"].fleets), (3, 2))
        self.assertIn(self.activity.pk, rows["Pilot A"].by_fleet_type)

    def test_it_counts_in_the_leaderboard(self):
        board = member_service.leaderboard(*MONTH)

        self.assertEqual([(row.main_character_name, row.pap) for row in board.rows], [("Pilot A", 5), ("Pilot B", 3)])

    def test_the_target_assessment_uses_the_same_pap(self):
        f.statistics_target(2026, 1, "1.00", minimum=4)

        rows = {row.main_character_name: row.status for row in member_service.member_rows(*MONTH).rows}

        # Pilot A reaches the minimum only with the group fleet's PAP.
        self.assertEqual((rows["Pilot A"], rows["Pilot B"]), ("ok", "below"))

    def test_the_leader_is_an_fc_with_zero_points_on_a_weight_zero_type(self):
        rows = {row.main_name: row for row in statistics.fc_rows(*MONTH)}

        self.assertEqual((rows["Pilot L"].fleets, rows["Pilot L"].points), (1, 0))
        self.assertEqual(rows["Pilot L"].breakdown["Group Activity"]["count"], 1)
        self.assertEqual(statistics.fc_statistics(self.leader, *MONTH)["fleet_count"], 1)

    def test_a_weighted_group_type_gives_points_like_any_fleet(self):
        weighted = f.fleet_type("Weighted Group", "1.50")
        operation = f.closed_operation(
            self.leader, utc(2026, 3, 12, 19), type_obj=weighted, group=self.group, group_name=self.group.name
        )
        f.add_attendance(operation, self.alice)

        self.assertEqual(float(statistics.fc_statistics(self.leader, *MONTH)["total_points"]), 1.5)

    def test_the_fc_page_shows_the_leader(self):
        response = self.get(self.viewer, "all_fc_statistics")

        self.assertContains(response, "Pilot L")
        self.assertContains(response, "Group Activity 1 / 0.00 pts")


class PingOnlyTests(GroupStatisticsBase):
    """A ping without a fleet creates no fleet and no PAP: nothing changes in any figure or file."""

    def snapshot(self):
        member = statistics.member_statistics(self.alice, *MONTH)
        overview = statistics.corporation_overview(*MONTH)
        return (
            member["total"],
            overview.alliance_pap,
            [(row.main_character_name, row.pap) for row in member_service.member_rows(*MONTH).rows],
            [(row.main_name, row.fleets, row.points) for row in statistics.fc_rows(*MONTH)],
            FleetOperation.objects.count(),
            AttendanceRecord.objects.count(),
        )

    def test_a_ping_only_leaves_every_statistic_unchanged(self):
        before = self.snapshot()
        channel = f.group_channel("Group Pings", groups=[self.group])
        f.standalone_ping(channel=channel, sender=self.leader, group=self.group)
        f.standalone_ping(channel=channel, sender=self.leader, group=self.group, status="failed")

        self.assertEqual(self.snapshot(), before)

    def test_the_exports_do_not_change_either(self):
        def files():
            return [
                self.export(slug, **params)
                for slug, params in (
                    ("members", {}),
                    ("corporations", {}),
                    ("attendance", {"scope": "alliance"}),
                    ("fcs", {}),
                )
            ]

        before = files()
        f.standalone_ping(channel=f.group_channel("Group Pings", groups=[self.group]), sender=self.leader, group=self.group)

        self.assertEqual(files(), before)

    def export(self, slug, **params):
        request = request_for(self.viewer, year=2026, month=3, **params)
        with clock(utc(2026, 4, 15, 12)):
            response = export_for_request(request, slug, "csv")
            text = b"".join(response.streaming_content).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text, newline="")))
        # The export time is not part of the rows.
        return rows


class GroupColumnTests(GroupStatisticsBase):
    def rows(self, slug, viewer=None, **params):
        request = request_for(viewer or self.viewer, year=2026, month=3, **params)
        with clock(utc(2026, 4, 15, 12)):
            response = export_for_request(request, slug, "csv")
            text = b"".join(response.streaming_content).decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text, newline="")))

    def test_the_member_fleets_file_has_a_group_column(self):
        rows = self.rows("member-fleets", user=self.alice.pk)
        header = rows[0]
        group = header.index("Group")

        self.assertEqual(header[group - 1], "Fleet type")
        by_type = {row[header.index("Fleet type")]: row[group] for row in rows[1:]}
        self.assertEqual(by_type, {"StratOp": "", "Group Activity": "Example Fleet Group"})

    def test_the_attendance_file_has_a_group_column(self):
        rows = self.rows("attendance", scope="alliance")
        header = rows[0]
        group = header.index("Group")

        self.assertEqual(header[group - 1], "Fleet type")
        by_type = {row[header.index("Fleet type")] for row in rows[1:]}
        self.assertEqual(by_type, {"StratOp", "Group Activity"})
        for row in rows[1:]:
            self.assertEqual(row[group], "Example Fleet Group" if row[header.index("Fleet type")] == "Group Activity" else "")

    def test_the_column_keeps_the_group_name_after_the_group_is_deleted(self):
        self.group.delete()

        rows = self.rows("attendance", scope="alliance")
        header = rows[0]

        self.assertIn("Example Fleet Group", {row[header.index("Group")] for row in rows[1:]})
        self.assertTrue(FleetOperation.objects.get(pk=self.group_op.pk).group_id is None)


class GroupPillTests(GroupStatisticsBase):
    PILL = 'class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet"'

    def test_the_history_rows_carry_the_group_pill(self):
        for name in ("attendance_history_me", "attendance_history_alliance"):
            with self.subTest(page=name):
                page = self.get(self.alice if name.endswith("me") else self.viewer, name).content.decode()
                self.assertIn(self.PILL, page)
                self.assertIn("fa-users", page)
                self.assertEqual(page.count(self.PILL), 2 if name.endswith("alliance") else 1)

    def test_an_alliance_fleet_row_has_no_pill(self):
        AttendanceRecord.objects.filter(operation=self.group_op).delete()

        self.assertNotIn(self.PILL, self.get(self.alice, "attendance_history_me").content.decode())

    def test_the_member_page_fleet_list_carries_it(self):
        page = self.get(self.viewer, "member_detail", self.alice.pk).content.decode()

        self.assertIn(self.PILL, page)

    def test_the_fc_page_lists_the_group_fleet_with_the_pill(self):
        page = self.get(self.viewer, "fc_statistics_detail", self.leader.pk).content.decode()

        self.assertIn(self.PILL, page)

    def test_the_pill_costs_no_query(self):
        def count(name, user):
            self.get(user, name)
            with CaptureQueriesContext(connection) as captured:
                self.get(user, name)
            return len(captured)

        for name, user in (("attendance_history_alliance", self.viewer), ("attendance_history_me", self.alice)):
            with self.subTest(page=name):
                with_group = count(name, user)
                FleetOperation.objects.filter(pk=self.group_op.pk).update(group=None, group_name="")
                without_group = count(name, user)
                FleetOperation.objects.filter(pk=self.group_op.pk).update(group=self.group, group_name=self.group.name)
                self.assertEqual(with_group, without_group)
