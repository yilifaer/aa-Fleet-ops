# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Query budgets of the statistics pages and exports.

A page or file costs the same number of queries whatever the number of corporations, members, fleets and
FCs: the dataset below grows to three times its size and every page is measured again. The ceilings count
every query of the request, the framework's own included (login state, user and permissions, the menu, the
settings and saving the login: about 24), and sit two queries above what the page needs today, so a query added
to a page shows up here. The data queries of a page are the members, the user totals, the totals by corporation
and fleet type, the fleet types, the target and the not-counted summary.
"""

from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import IncentivePeriod
from fleetops.services.incentives import rebuild_period

from . import factories as f

UTC = dt_timezone.utc
NOW = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
MONTH = {"year": 2026, "month": 3}
APRIL_FIRST = datetime(2026, 4, 1, tzinfo=UTC)

# None of the page viewers below but the dashboard's FC holds start_fleet, so the menu looks up whether they lead
# a group that may send pings or run fleets (one query, services.group_access.leader_access).
GROUP_NAV = 1
# The dashboard's running fleets: for a viewer without view_all_fleets the query asks for the groups they lead
# (one query).
RUNNING_GROUP_FLEETS = 1
# The incentive page compares its stored rows with the current FC statistics (four queries).
INCENTIVE_STALENESS = 4

ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
FLEET_LEAD = ALL_STATS + ["fleetops.view_all_fleets", "fleetops.manage_incentives"]


def utc(day, hour=19):
    return datetime(2026, 3, day, hour, 0, tzinfo=UTC)


def route(name, *args):
    return reverse(f"fleetops:{name}", args=args)


def export(dataset):
    return route("export", dataset)


class Dataset:
    """Corporations with members and fleets that grow in steps; every step adds corporations, members, FCs and fleets."""

    def __init__(self):
        self.serial = 0
        self.corporations = []
        self.members = []
        self.fcs = []
        # Users who registered after the measured month: not in its figures, but their pages open.
        self.late = []
        self.types = [f.fleet_type("StratOp", "1.00"), f.fleet_type("Roam", "0.50")]

    def next(self):
        self.serial += 1
        return self.serial

    def grow(self, corporations, members_each, fcs, fleets):
        for _ in range(corporations):
            number = self.next()
            corporation = (2_100_000 + number, f"Budget Corp {number}")
            self.corporations.append(corporation)
            for _ in range(members_each):
                member = f.member(f"member{self.next()}", corporation=corporation, perms=f.MEMBER_PERMS)
                self.members.append(member)
            late = f.member(f"late{self.next()}", corporation=corporation, perms=f.MEMBER_PERMS, joined=APRIL_FIRST)
            self.late.append(late)
        for _ in range(fcs):
            number = self.next()
            self.fcs.append(f.member(f"fc{number}", perms=f.FC_PERMS, main_name=f"Budget FC {number}"))
        if len(self.types) < 4:
            self.types.extend(f.fleet_type(name, "0.25") for name in ("Mining", "Hauling")[len(self.types) - 2 :])
        for index in range(fleets):
            fc = self.fcs[index % len(self.fcs)]
            operation = f.closed_operation(
                fc, utc(1 + index % 27, 10 + index % 12), type_obj=self.types[index % len(self.types)]
            )
            for pilot in self.members[index % 2 :: 2] + self.late[index % 2 :: 2]:
                f.add_attendance(operation, pilot, value=1 + index % 3)
        return self


class StatisticsQueryBudgetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        clock = mock.patch("django.utils.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]), incentive_enabled=True)
        f.statistics_target(2026, 1, "2.00", minimum=2)
        self.statsmgr = f.member("statsmgr", perms=FLEET_LEAD, main_name="Stats Manager", corporation=f.THIRD_CORP)
        self.corpmgr = f.member("corpmgr", perms=f.CORP_MANAGEMENT_PERMS, main_name="Corp Manager")
        self.pilot = f.member("pilot", perms=f.MEMBER_PERMS, main_name="Budget Pilot")
        self.data = Dataset()
        # The users of the pages are members with PAP, so their own pages are not empty.
        self.data.members.extend([self.corpmgr, self.pilot, self.statsmgr])

    def request(self, viewer, url, params=None):
        self.client.force_login(viewer)
        response = self.client.get(url, params or {})
        self.assertEqual(response.status_code, 200, url)
        # A file is written while it is read.
        if response.streaming:
            b"".join(response.streaming_content)
        return response

    def queries(self, viewer, url, params=None):
        """The number of queries of a request after one warm-up request (caches of the process)."""
        self.request(viewer, url, params)
        with CaptureQueriesContext(connection) as captured:
            self.request(viewer, url, params)
        return len(captured)

    def cases(self):
        """``(name, viewer, url, params, ceiling)`` for every page and export."""
        data = self.data
        own_corporation = f.DEFAULT_CORP[0]
        inactive = {**MONTH, "status": "inactive"}
        fc = data.fcs[0]
        return [
            ("dashboard", fc, route("dashboard"), {}, 40 + RUNNING_GROUP_FLEETS),
            ("my statistics", self.pilot, route("my_statistics"), MONTH, 47 + GROUP_NAV),
            ("all corporations", self.statsmgr, route("all_corporation_statistics"), MONTH, 36 + GROUP_NAV),
            ("all corporations sorted", self.statsmgr, route("all_corporation_statistics"), {**MONTH, "sort": "-total"}, 36 + GROUP_NAV),
            ("corporation", self.corpmgr, route("corporation_statistics"), MONTH, 35 + GROUP_NAV),
            ("corporation by id", self.statsmgr, route("corporation_statistics_detail", own_corporation), MONTH, 37 + GROUP_NAV),
            ("fc statistics", self.statsmgr, route("all_fc_statistics"), MONTH, 32 + GROUP_NAV),
            ("fc detail", self.statsmgr, route("fc_statistics_detail", fc.pk), MONTH, 35 + GROUP_NAV),
            ("alliance members", self.statsmgr, route("alliance_members"), MONTH, 37 + GROUP_NAV),
            ("alliance inactive", self.statsmgr, route("alliance_members"), inactive, 37 + GROUP_NAV),
            ("alliance members of a corporation", self.statsmgr, route("alliance_members"), {**MONTH, "corporation": own_corporation}, 37 + GROUP_NAV),
            ("corporation members", self.corpmgr, route("corporation_members"), MONTH, 36 + GROUP_NAV),
            ("corporation inactive", self.corpmgr, route("corporation_members"), inactive, 36 + GROUP_NAV),
            ("corporation members by id", self.statsmgr, route("corporation_members_detail", own_corporation), MONTH, 38 + GROUP_NAV),
            ("leaderboard", self.pilot, route("leaderboard"), MONTH, 37 + GROUP_NAV),
            ("leaderboard of the manager", self.statsmgr, route("leaderboard"), MONTH, 37 + GROUP_NAV),
            ("member detail", self.statsmgr, route("member_detail", self.pilot.pk), MONTH, 42 + GROUP_NAV),
            ("member detail of the corporation", self.corpmgr, route("member_detail", self.pilot.pk), MONTH, 42 + GROUP_NAV),
            ("member detail of a later registration", self.statsmgr, route("member_detail", data.late[0].pk), MONTH, 42 + GROUP_NAV),
            ("history", self.pilot, route("attendance_history_me"), MONTH, 34 + GROUP_NAV),
            ("corporation history", self.corpmgr, route("attendance_history_corporation"), MONTH, 34 + GROUP_NAV),
            ("alliance history", self.statsmgr, route("attendance_history_alliance"), MONTH, 34 + GROUP_NAV),
            ("alliance history of all months", self.statsmgr, route("attendance_history_alliance"), {}, 34 + GROUP_NAV),
            ("incentive review", self.statsmgr, route("incentive_review"), MONTH, 35 + GROUP_NAV + INCENTIVE_STALENESS),
            ("corporations file", self.statsmgr, export("corporations"), {**MONTH, "format": "csv"}, 29),
            ("corporations workbook", self.statsmgr, export("corporations"), {**MONTH, "format": "xlsx"}, 29),
            ("members file", self.statsmgr, export("members"), {**MONTH, "format": "csv"}, 29),
            ("members workbook", self.statsmgr, export("members"), {**MONTH, "format": "xlsx"}, 29),
            ("corporation members file", self.corpmgr, export("members"), {**MONTH, "format": "csv", "corporation": own_corporation}, 29),
            ("member fleets file", self.statsmgr, export("member-fleets"), {**MONTH, "format": "csv", "user": self.pilot.pk}, 28),
            ("fc file", self.statsmgr, export("fcs"), {**MONTH, "format": "csv"}, 26),
            ("history file", self.pilot, export("attendance"), {**MONTH, "format": "csv", "scope": "me"}, 25),
            ("corporation history file", self.corpmgr, export("attendance"), {**MONTH, "format": "csv", "scope": "corporation"}, 25),
            ("alliance history file", self.statsmgr, export("attendance"), {**MONTH, "format": "xlsx", "scope": "alliance"}, 25),
            ("incentives file", self.statsmgr, export("incentives"), {"format": "csv", "period": self.period.pk}, 24),
        ]

    def test_a_larger_dataset_costs_the_same_queries(self):
        data = self.data
        data.grow(corporations=2, members_each=3, fcs=2, fleets=4)
        self.period = IncentivePeriod.objects.create(year=2026, month=3, budget=1_000_000, minimum_fleets=1)
        for fc in data.fcs[:1]:
            f.add_attendance(f.closed_operation(fc, utc(5), type_obj=data.types[0]), self.pilot)
        rebuild_period(self.period)
        small = {name: self.queries(viewer, url, params) for name, viewer, url, params, _ in self.cases()}

        # Three times the corporations, members, FCs and fleets.
        data.grow(corporations=4, members_each=3, fcs=4, fleets=8)
        f.add_attendance(f.closed_operation(data.fcs[3], utc(6), type_obj=data.types[1]), self.pilot)
        rebuild_period(self.period)
        for name, viewer, url, params, ceiling in self.cases():
            with self.subTest(name):
                large = self.queries(viewer, url, params)
                self.assertEqual(large, small[name], f"{name}: {small[name]} queries, then {large} for the larger dataset")
                self.assertLessEqual(large, ceiling, f"{name} needs {large} queries")
