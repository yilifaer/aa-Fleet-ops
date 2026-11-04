# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Query budgets of the statistics pages and exports.

A page or file costs the same number of queries whatever the number of corporations, members, fleets and
FCs: the dataset below grows to three times its size and every page is measured again. The ceilings count
every query of the request, the framework's own included (login state, user and permissions, the menu, the
settings and saving the login: about 24, and on Alliance Auth 5.6 and later two more for the notification badge
of the page menu, see ``factories.notification_badge_queries``), and sit two queries above what the page needs
today, so a query added to a page shows up here. The data queries of a page are the members, the user totals,
the totals by corporation and fleet type, the fleet types, the target and the not-counted summary.
"""

from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.models import IncentivePeriod, SrpSettings
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
        # None of the viewers has unread notifications, so the badge costs every one of them the same.
        self.badge = f.notification_badge_queries(self.statsmgr)

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
        # Pages carry Alliance Auth's menu and its notification badge; the files do not.
        badge = self.badge
        return [
            ("dashboard", fc, route("dashboard"), {}, 40 + RUNNING_GROUP_FLEETS + badge),
            ("my statistics", self.pilot, route("my_statistics"), MONTH, 47 + GROUP_NAV + badge),
            ("all corporations", self.statsmgr, route("all_corporation_statistics"), MONTH, 36 + GROUP_NAV + badge),
            ("all corporations sorted", self.statsmgr, route("all_corporation_statistics"), {**MONTH, "sort": "-total"}, 36 + GROUP_NAV + badge),
            ("corporation", self.corpmgr, route("corporation_statistics"), MONTH, 35 + GROUP_NAV + badge),
            ("corporation by id", self.statsmgr, route("corporation_statistics_detail", own_corporation), MONTH, 37 + GROUP_NAV + badge),
            ("fc statistics", self.statsmgr, route("all_fc_statistics"), MONTH, 32 + GROUP_NAV + badge),
            ("fc detail", self.statsmgr, route("fc_statistics_detail", fc.pk), MONTH, 35 + GROUP_NAV + badge),
            ("alliance members", self.statsmgr, route("alliance_members"), MONTH, 37 + GROUP_NAV + badge),
            ("alliance inactive", self.statsmgr, route("alliance_members"), inactive, 37 + GROUP_NAV + badge),
            ("alliance members of a corporation", self.statsmgr, route("alliance_members"), {**MONTH, "corporation": own_corporation}, 37 + GROUP_NAV + badge),
            ("corporation members", self.corpmgr, route("corporation_members"), MONTH, 36 + GROUP_NAV + badge),
            ("corporation inactive", self.corpmgr, route("corporation_members"), inactive, 36 + GROUP_NAV + badge),
            ("corporation members by id", self.statsmgr, route("corporation_members_detail", own_corporation), MONTH, 38 + GROUP_NAV + badge),
            ("leaderboard", self.pilot, route("leaderboard"), MONTH, 37 + GROUP_NAV + badge),
            ("leaderboard of the manager", self.statsmgr, route("leaderboard"), MONTH, 37 + GROUP_NAV + badge),
            ("member detail", self.statsmgr, route("member_detail", self.pilot.pk), MONTH, 42 + GROUP_NAV + badge),
            ("member detail of the corporation", self.corpmgr, route("member_detail", self.pilot.pk), MONTH, 42 + GROUP_NAV + badge),
            ("member detail of a later registration", self.statsmgr, route("member_detail", data.late[0].pk), MONTH, 42 + GROUP_NAV + badge),
            ("history", self.pilot, route("attendance_history_me"), MONTH, 34 + GROUP_NAV + badge),
            ("corporation history", self.corpmgr, route("attendance_history_corporation"), MONTH, 34 + GROUP_NAV + badge),
            ("alliance history", self.statsmgr, route("attendance_history_alliance"), MONTH, 34 + GROUP_NAV + badge),
            ("alliance history of all months", self.statsmgr, route("attendance_history_alliance"), {}, 34 + GROUP_NAV + badge),
            ("incentive review", self.statsmgr, route("incentive_review"), MONTH, 35 + GROUP_NAV + INCENTIVE_STALENESS + badge),
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


class SrpCategoryPageBudgetTests(TestCase):
    """The pages of the SRP categories (CAT-8) cost the same number of queries whatever the number of rows."""

    def count(self, url, user):
        self.client.force_login(user)
        # The first request of a login also loads the permissions and the menu; measure the second.
        self.client.get(url)
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def grow(self, step):
        for index in range(step):
            category = f.srp_category(percentage=None)
            f.payout_rule(587 + index + step * 100, category=category)

    def test_the_category_list_and_the_payout_table_do_not_grow_with_the_rows(self):
        admin = f.create_user("budget_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        pages = [
            route("configuration_list", "srp-categories"),
            route("configuration_list", "srp-rules"),
            route("configuration_add", "srp-rules"),
        ]
        self.grow(2)
        small = [self.count(url, admin) for url in pages]
        self.grow(6)

        large = [self.count(url, admin) for url in pages]

        self.assertEqual(small, large)

    def test_the_claim_page_does_not_query_the_category_again(self):
        manager = f.create_user("budget_manager", perms=f.SRP_MANAGER_PERMS)
        solo = f.srp_category("Solo PVP")
        pilot = f.create_user("budget_pilot", perms=f.SRP_MEMBER_PERMS)
        with_category = f.srp_claim(pilot, category=solo)
        plain = f.srp_claim(pilot, category=None)

        self.assertEqual(
            self.count(route("srp_claim", with_category.uuid), manager),
            self.count(route("srp_claim", plain.uuid), manager),
        )


class MySrpBudgetTests(TestCase):
    """My SRP: the limits of the capped categories come from one query, whatever their number."""

    def count(self, user):
        self.client.force_login(user)
        url = route("srp_home")
        self.client.get(url)
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_capped_categories_do_not_add_queries(self):
        pilot = f.create_user("budget_my_srp", perms=f.SRP_MEMBER_PERMS)
        f.srp_category("Capped 0", personal_monthly_cap=1_000_000)
        one = self.count(pilot)
        for index in range(1, 6):
            category = f.srp_category(f"Capped {index}", personal_monthly_cap=1_000_000)
            f.srp_claim(pilot, category=category)

        self.assertEqual(self.count(pilot), one)


class IntakeBudgetTests(TestCase):
    """INT-10, RUL-4, REV-8: the standing-link card, the "Not covered" card and the ban list do not grow with
    their rows, and the preview costs at most one query for the ban."""

    def count(self, user, url):
        self.client.force_login(user)
        self.client.get(url)
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_my_srp_does_not_grow_with_standing_links_or_blocked_ships(self):
        pilot = f.create_user("budget_intake_pilot", perms=f.SRP_MEMBER_PERMS)
        f.standing_link(category=f.srp_category("Intake 0"))
        SrpSettings.objects.filter(pk=1).update(blocked_ship_group_ids="463", blocked_ship_type_ids="32880")
        one = self.count(pilot, route("srp_home"))
        for index in range(1, 8):
            f.standing_link(category=f.srp_category(f"Intake {index}"))
        SrpSettings.objects.filter(pk=1).update(
            blocked_ship_group_ids="463,543,28,380", blocked_ship_type_ids="32880,605,607,29248,586"
        )

        self.assertEqual(self.count(pilot, route("srp_home")), one)

    def test_the_ban_list_does_not_grow_with_the_bans(self):
        from fleetops.models import SrpBan

        reviewer = f.create_user("budget_intake_reviewer", perms=f.SRP_REVIEWER_PERMS)

        def add(number):
            for index in range(number):
                member = f.create_user(f"budget_banned_{number}_{index}", perms=f.SRP_MEMBER_PERMS)
                SrpBan.objects.create(user=member, reason="Test", created_by=reviewer)

        add(2)
        small = self.count(reviewer, route("srp_bans"))
        add(8)

        self.assertEqual(self.count(reviewer, route("srp_bans")), small)

    def test_the_claim_page_costs_one_query_more_for_the_ban_of_a_reviewer(self):
        reviewer = f.create_user("budget_claim_reviewer", perms=f.SRP_REVIEWER_PERMS)
        payer = f.create_user("budget_claim_payer", perms=f.SRP_PAYER_PERMS)
        claim = f.srp_claim(f.create_user("budget_claim_member", perms=f.SRP_MEMBER_PERMS))
        url = route("srp_claim", claim.uuid)

        # The reviewer's page also reads the ban of the member: one query for the ban, the form needs none.
        self.assertLessEqual(self.count(reviewer, url) - self.count(payer, url), 6)

    def test_the_preview_costs_at_most_one_query_more_for_the_ban(self):
        """REV-8: the whole preview (``prepare_claim``) on one fixture, with the ban lookup patched away and for real."""
        from fleetops.srp import bans, claims

        pilot = f.create_user("budget_elig_pilot", perms=f.SRP_MEMBER_PERMS)
        km = f.srp_killmail(f.main_of(pilot), fitted="1000000")
        f.srp_settings()
        text = f"<url=killReport:{km.killmail_id}:{km.killmail_hash}>Loss</url>"

        def preview_queries(ban_lookup=None):
            cache.delete(f"fleetops:srp:check:{pilot.pk}")
            patches = [mock.patch.object(claims.killmails, "fetch_killmail", return_value=km)]
            if ban_lookup is not None:
                patches.append(mock.patch.object(bans, "active_ban", ban_lookup))
            with CaptureQueriesContext(connection) as queries:
                for patch in patches:
                    patch.start()
                try:
                    preview = claims.prepare_claim(pilot, text)
                finally:
                    for patch in patches:
                        patch.stop()
            self.assertEqual(preview.blockers, [])
            return len(queries)

        preview_queries()  # warm the caches both runs share
        without = preview_queries(lambda user, now=None: None)
        with_ban = preview_queries()

        self.assertLessEqual(with_ban - without, 1)
        self.assertGreaterEqual(with_ban, without)


class SrpRoleBudgetTests(TestCase):
    """REV-1: the menu of a payer costs one count, the menu of a reviewer one count, a manager both; the payments
    page does not grow with the claims."""

    def count(self, user, url):
        self.client.force_login(user)
        self.client.get(url)
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_the_menu_costs_one_count_per_role(self):
        reviewer = f.create_user("budget_reviewer", perms=f.SRP_REVIEWER_PERMS)
        payer = f.create_user("budget_payer", perms=f.SRP_PAYER_PERMS)
        manager = f.create_user("budget_both", perms=f.SRP_MANAGER_PERMS)
        url = route("dashboard")

        counts = {name: self.count(user, url) for name, user in (("reviewer", reviewer), ("payer", payer), ("manager", manager))}

        self.assertEqual(counts["payer"], counts["reviewer"])
        self.assertEqual(counts["manager"], counts["payer"] + 1)

    def test_the_payments_page_does_not_grow_with_the_claims(self):
        payer = f.create_user("budget_payer_page", perms=f.SRP_PAYER_PERMS)
        pilots = [f.create_user(f"budget_pilot_{index}", perms=f.SRP_MEMBER_PERMS) for index in range(6)]
        approver = f.create_user("budget_approver", perms=f.SRP_MANAGER_PERMS)
        url = route("srp_payments")
        for pilot in pilots[:2]:
            f.srp_claim(pilot, status="approved", decided_by=approver)
        small = self.count(payer, url)
        for pilot in pilots[2:]:
            f.srp_claim(pilot, status="approved", decided_by=approver)
            f.srp_claim(pilot, status="approved", decided_by=approver)

        self.assertEqual(self.count(payer, url), small)


class SrpLinkBudgetTests(TestCase):
    """LINK (INT-9): the link list counts the claims of each link in the query that lists them, so it does not
    grow with the links, their claims or their categories."""

    def count(self, user, url):
        self.client.force_login(user)
        self.client.get(url)
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def add_links(self, number, pilot):
        for index in range(number):
            link = f.standing_link(category=f.srp_category(f"Budget category {index}-{number}"))
            f.srp_claim(pilot, link=link)
            f.srp_claim(pilot, link=link, status="approved", decided_by=pilot)

    def test_the_list_does_not_grow_with_the_links(self):
        payer = f.create_user("budget_links_payer", perms=f.SRP_PAYER_PERMS)
        pilot = f.create_user("budget_links_pilot", perms=f.SRP_MEMBER_PERMS)
        url = route("srp_links")
        self.add_links(2, pilot)
        small = self.count(payer, url)
        self.add_links(8, pilot)

        self.assertEqual(self.count(payer, url), small)
        # The page itself: the framework's queries, the menu, the settings, the count and the page of links.
        self.assertLessEqual(small, 32)

    def test_the_forms_cost_the_same_with_many_categories(self):
        reviewer = f.create_user("budget_links_reviewer", perms=f.SRP_REVIEWER_PERMS)
        url = route("srp_link_new")
        f.srp_category("Budget form 0")
        small = self.count(reviewer, url)
        for index in range(1, 7):
            f.srp_category(f"Budget form {index}")

        self.assertEqual(self.count(reviewer, url), small)
