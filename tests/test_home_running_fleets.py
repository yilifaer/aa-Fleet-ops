# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The running fleets on the home page: who sees which fleet, the command deck link, the FC's system (K3, S14),
pre pings (H3), group fleets for leaders who did not start them (G23) and the recent fleets."""

import re
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetOperation, GroupAccess
from fleetops.services import dashboard as dashboard_service
from fleetops.services import group_access
from fleetops.views import _can_manage_operation

from . import factories as f

FLEETS = GroupAccess.Mode.FLEETS
Status = FleetOperation.Status
MANAGE_OWN_PERMS = f.MEMBER_PERMS + ["fleetops.start_fleet", "fleetops.manage_own_fleet"]
VIEW_ALL_PERMS = f.MEMBER_PERMS + ["fleetops.view_all_fleets"]
STRIP = 'id="fo-running-fleets"'


def strip_of(page):
    """The HTML of the running fleets block; empty when the page has none."""
    found = re.search(r'<section id="fo-running-fleets".*?</section>', page, re.S)
    return found.group(0) if found else ""


class HomeFixture(TestCase):
    def setUp(self):
        self.strat = f.fleet_type("StratOp", "1.00")
        self.roam = f.fleet_type("Roam", "1.00")
        self.fc = f.create_user("fc", perms=MANAGE_OWN_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.viewer = f.create_user("viewer", perms=VIEW_ALL_PERMS, main_name="Example Viewer")
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.outsider = f.create_user("outsider", perms=f.MEMBER_PERMS, main_name="Pilot B")
        self.operation = f.create_operation(self.fc, type_obj=self.strat, started_at=timezone.now() - timedelta(minutes=72))
        self.fc_state = f.member_state(
            self.operation, f.main_of(self.fc), user=self.fc, role="fleet_commander", solar_system_name="Jita"
        )
        f.member_state(self.operation, f.main_of(self.pilot), user=self.pilot, solar_system_name="Amarr")
        self.url = reverse("fleetops:dashboard")

    def page(self, user):
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response


class VisibilityTests(HomeFixture):
    def test_the_strip_is_not_there_without_a_running_fleet(self):
        FleetOperation.objects.update(status=Status.CLOSED)

        page = self.page(self.fc).content.decode()

        self.assertNotIn(STRIP, page)
        self.assertEqual(self.page(self.fc).context["running_fleets"], [])

    def test_a_member_sees_the_fleet_but_no_system_and_no_command_deck(self):
        response = self.page(self.pilot)
        strip = strip_of(response.content.decode())

        self.assertIn("Example FC", strip)
        self.assertIn("StratOp", strip)
        self.assertIn(">Open</a>", strip)
        self.assertNotIn("Open command deck", strip)
        self.assertNotIn("Jita", strip)
        self.assertNotIn("#fleet", strip)
        row = response.context["running_fleets"][0]
        self.assertEqual((row["can_manage"], row["show_system"], row["fc_system_name"]), (False, False, ""))

    def test_a_member_outside_the_fleet_sees_nothing(self):
        self.assertNotIn(STRIP, self.page(self.outsider).content.decode())

    def test_the_fc_gets_the_command_deck_and_the_system(self):
        response = self.page(self.fc)
        strip = strip_of(response.content.decode())

        self.assertIn("Open command deck", strip)
        self.assertIn(f"{reverse('fleetops:operation_detail', args=[self.operation.uuid])}#fleet", strip)
        self.assertIn("Jita", strip)
        row = response.context["running_fleets"][0]
        self.assertEqual((row["can_manage"], row["show_system"], row["fc_system_name"]), (True, True, "Jita"))

    def test_a_manager_of_all_fleets_gets_the_command_deck(self):
        strip = strip_of(self.page(self.lead).content.decode())

        self.assertIn("Open command deck", strip)
        self.assertIn("Jita", strip)

    def test_a_viewer_of_all_fleets_sees_the_system_but_not_the_command_deck(self):
        response = self.page(self.viewer)
        strip = strip_of(response.content.decode())

        self.assertIn("Jita", strip)
        self.assertNotIn("Open command deck", strip)
        self.assertIn(">Open</a>", strip)
        row = response.context["running_fleets"][0]
        self.assertEqual((row["can_manage"], row["show_system"]), (False, True))

    def test_a_credited_co_fc_manages_the_fleet(self):
        co_fc = f.create_user("cofc", perms=MANAGE_OWN_PERMS, main_name="Example Co-FC")
        main = f.main_of(co_fc)
        self.operation.role_assignments.create(
            role="backseat_fc", character_id=main.character_id, character_name=main.character_name,
            auth_user=co_fc, grants_fc_credit=True,
        )

        response = self.page(co_fc)

        row = response.context["running_fleets"][0]
        self.assertEqual((row["can_manage"], row["show_system"], row["fc_system_name"]), (True, True, "Jita"))
        self.assertIn("Open command deck", strip_of(response.content.decode()))

    def test_a_pre_ping_is_never_in_the_strip_and_members_do_not_see_it_at_all(self):
        f.create_scheduled_operation(self.fc, type_obj=self.roam, formup="Jita")

        for user in (self.pilot, self.outsider):
            page = self.page(user).content.decode()
            self.assertNotIn('id="scheduled-fleets"', page)
            self.assertNotIn("Roam", strip_of(page))
        fc_page = self.page(self.fc)
        self.assertEqual(len(fc_page.context["running_fleets"]), 1)
        self.assertNotIn("Roam", strip_of(fc_page.content.decode()))
        self.assertContains(fc_page, 'id="scheduled-fleets"')

    def test_the_fc_without_a_tracked_state_has_no_system_line(self):
        self.fc_state.delete()

        row = self.page(self.fc).context["running_fleets"][0]

        self.assertEqual((row["show_system"], row["fc_system_name"]), (True, ""))

    def test_the_system_is_the_one_of_the_fc_character_not_of_another_member(self):
        row = self.page(self.fc).context["running_fleets"][0]

        self.assertEqual(row["fc_system_name"], "Jita")
        self.assertNotEqual(row["fc_system_name"], "Amarr")


class RowTests(HomeFixture):
    def test_each_row_shows_the_member_count_and_the_elapsed_time(self):
        strip = strip_of(self.page(self.fc).content.decode())

        self.assertIn("2 members", strip)
        self.assertIn("data-since-utc=", strip)
        self.assertIn('class="fo-live" data-state="live"', strip)

    def test_the_member_count_is_plural_or_singular(self):
        solo = f.create_operation(self.lead, type_obj=self.roam, started_at=timezone.now() - timedelta(minutes=5))
        f.member_state(solo, f.main_of(self.lead), user=self.lead)
        empty = f.create_operation(self.lead, type_obj=self.roam, started_at=timezone.now() - timedelta(minutes=6))

        strip = strip_of(self.page(self.lead).content.decode())

        self.assertIn("2 members", strip)
        self.assertIn("1 member<", strip)
        self.assertIn("0 members", strip)
        self.assertTrue(empty.pk)

    def test_the_fleet_type_is_a_pill_with_its_dot_and_no_badge(self):
        strip = strip_of(self.page(self.fc).content.decode())

        self.assertRegex(strip, r'class="fo-pill fo-pill-type fo-type-\d">StratOp</span>')
        self.assertNotIn("badge", strip)

    def test_only_three_fleets_are_shown_and_the_rest_is_a_link(self):
        for index in range(3):
            f.create_operation(self.fc, type_obj=self.roam, started_at=timezone.now() - timedelta(minutes=index + 1))

        page = self.page(self.fc)
        strip = strip_of(page.content.decode())

        self.assertEqual(len(page.context["running_fleets"]), 4)
        self.assertEqual(strip.count('<article class="fo-running-card"'), 3)
        self.assertIn("and 1 more running fleet", strip)
        self.assertIn(f"{reverse('fleetops:fleet_operations')}?status=active", strip)

    def test_the_helper_matches_the_rule_of_the_fleet_page(self):
        outsider_group_leader = f.create_user("gl", perms=MANAGE_OWN_PERMS, main_name="Pilot G")
        co_fc = f.create_user("cofc2", perms=MANAGE_OWN_PERMS, main_name="Example Co-FC Two")
        main = f.main_of(co_fc)
        self.operation.role_assignments.create(
            role="backseat_fc", character_id=main.character_id, character_name=main.character_name,
            auth_user=co_fc, grants_fc_credit=True,
        )
        other = f.create_operation(self.lead, type_obj=self.roam)
        for user in (self.fc, self.lead, self.viewer, self.pilot, co_fc, outsider_group_leader):
            with self.subTest(user=user.username):
                group_access.forget(user)
                rows = dashboard_service.running_fleet_rows(user, [self.operation, other])
                for row in rows:
                    self.assertEqual(row["can_manage"], bool(_can_manage_operation(user, row["operation"])))

    def test_queries_do_not_grow_with_the_number_of_fleets(self):
        def queries(user):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            return len(captured)

        self.client.force_login(self.fc)
        queries(self.fc)
        small = queries(self.fc)
        for index in range(4):
            other = f.create_user(f"fc{index}", perms=MANAGE_OWN_PERMS, main_name=f"Example FC {index}")
            op = f.create_operation(other, type_obj=self.roam)
            f.member_state(op, f.main_of(other), user=other, solar_system_name="Jita")
            self.operation.role_assignments.create(
                role="backseat_fc", character_id=f.main_of(other).character_id, character_name=f"Example FC {index}",
                auth_user=self.fc, grants_fc_credit=True,
            )
        large = queries(self.fc)

        self.assertEqual(large, small)


class GroupFleetTests(HomeFixture):
    def setUp(self):
        super().setUp()
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot L")
        self.second = f.create_user("second", perms=f.MEMBER_PERMS, main_name="Pilot M")
        self.other_leader = f.create_user("otherleader", perms=f.MEMBER_PERMS, main_name="Pilot N")
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader, self.second])
        f.group_access(self.group, FLEETS, types=[self.activity])
        self.other_group = f.aa_group("Example Other Group", leaders=[self.other_leader])
        f.group_access(self.other_group, FLEETS, types=[self.activity])
        self.group_op = f.create_operation(
            self.leader, type_obj=self.activity, group=self.group, group_name=self.group.name,
            started_at=timezone.now() - timedelta(minutes=30),
        )
        f.member_state(self.group_op, f.main_of(self.leader), user=self.leader, role="fleet_commander", solar_system_name="Jita")

    def test_the_leader_who_started_it_gets_the_command_deck(self):
        response = self.page(self.leader)
        strip = strip_of(response.content.decode())

        self.assertIn("Open command deck", strip)
        self.assertIn("Example Fleet Group", strip)
        row = response.context["running_fleets"][0]
        self.assertEqual((row["can_manage"], row["show_system"], row["fc_system_name"]), (True, True, "Jita"))

    def test_a_second_leader_sees_it_read_only_without_the_fc_system(self):
        response = self.page(self.second)
        strip = strip_of(response.content.decode())

        self.assertIn("Example Fleet Group", strip)
        self.assertIn(">Open</a>", strip)
        self.assertNotIn("Open command deck", strip)
        self.assertNotIn("Jita", strip)
        row = response.context["running_fleets"][0]
        self.assertEqual((row["can_manage"], row["show_system"], row["fc_system_name"]), (False, False, ""))

    def test_the_leader_of_another_group_and_a_member_see_nothing_of_it(self):
        for user in (self.other_leader, self.outsider):
            self.assertNotIn(STRIP, self.page(user).content.decode())

    def test_the_group_pill_is_built_from_the_stored_name(self):
        strip = strip_of(self.page(self.leader).content.decode())

        self.assertRegex(
            strip,
            r'<span class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet">'
            r'<i class="fa-solid fa-users" aria-hidden="true"></i>Example Fleet Group</span>',
        )

    def test_a_leader_who_lost_leadership_loses_the_row(self):
        self.group.authgroup.group_leaders.remove(self.second.pk)

        self.assertNotIn(STRIP, self.page(self.second).content.decode())

    def test_an_alliance_fleet_has_no_group_pill(self):
        strip = strip_of(self.page(self.fc).content.decode())

        self.assertNotIn("fo-pill-group", strip)
        self.assertNotIn("fa-users", strip)

    def test_the_group_pill_costs_no_query(self):
        def count(operation_user):
            self.client.force_login(operation_user)
            self.client.get(self.url)
            with CaptureQueriesContext(connection) as captured:
                self.client.get(self.url)
            return len(captured)

        with_group = count(self.lead)
        FleetOperation.objects.filter(pk=self.group_op.pk).update(group=None, group_name="")
        without_group = count(self.lead)

        self.assertEqual(with_group, without_group)


class OrderTests(HomeFixture):
    """5-12: the page keeps its order; the strip is added under the title row, the PAP status stays above it."""

    def test_the_strip_comes_after_the_page_title_and_before_the_metrics(self):
        page = self.page(self.pilot).content.decode()

        title = page.index("<h2")
        strip = page.index(STRIP)
        self.assertLess(title, strip)
        self.assertLess(strip, page.index("fleetops-metric-row"))

    def test_the_live_pap_status_keeps_its_place_above_the_title(self):
        page = self.page(self.pilot).content.decode()

        self.assertIn('id="live-pap-status"', page)
        self.assertLess(page.index('id="live-pap-status"'), page.index("<h2"))

    def test_the_active_fleets_card_is_still_there_in_its_place(self):
        page = self.page(self.fc).content.decode()

        self.assertIn("Active Fleets", page)
        self.assertLess(page.index("Scheduled Fleets"), page.index("Active Fleets"))
        self.assertLess(page.index("Active Fleets"), page.index("My FC Month"))

    def test_the_start_button_stays_in_the_title_row(self):
        page = self.page(self.fc).content.decode()

        title = page.index("<h2")
        button = page.index(f'href="{reverse("fleetops:start_fleet")}">Start Fleet</a>', title)
        self.assertLess(button, page.index(STRIP))


class RecentFleetsTests(HomeFixture):
    def test_a_recent_row_shows_the_member_count_the_type_and_the_group(self):
        group = f.aa_group("Example Fleet Group")
        closed = f.closed_operation(
            self.fc, timezone.now() - timedelta(days=2), type_obj=self.roam, group=group, group_name=group.name
        )
        for user in (self.pilot, self.outsider, self.viewer):
            f.add_attendance(closed, user, value=2)

        page = self.page(self.pilot)
        rows = page.context["recent_participations"]
        html = page.content.decode()

        self.assertEqual(rows[0]["member_count"], 3)
        self.assertIn("3 members", html)
        self.assertRegex(html, r'class="fo-pill fo-pill-type fo-type-\d">Roam</span>')
        self.assertIn('class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet"', html)
        self.assertIn("Example Fleet Group", html)

    def test_the_member_count_counts_granted_rows_only(self):
        closed = f.closed_operation(self.fc, timezone.now() - timedelta(days=2), type_obj=self.roam)
        f.add_attendance(closed, self.pilot, value=2)
        f.add_attendance(closed, self.outsider, value=2, granted=False)

        self.assertEqual(self.page(self.pilot).context["recent_participations"][0]["member_count"], 1)

    def test_an_empty_home_offers_the_fleets_list_or_the_start_page(self):
        member_page = self.page(self.outsider).content.decode()
        fc_page = self.page(self.fc).content.decode()

        self.assertIn("No PAP yet.", member_page)
        self.assertIn(reverse("fleetops:fleet_operations"), member_page)
        self.assertIn("No PAP yet.", fc_page)
        self.assertIn(reverse("fleetops:start_fleet"), fc_page)

    def test_no_display_class_and_the_ship_renders_have_the_frame(self):
        closed = f.closed_operation(self.fc, timezone.now() - timedelta(days=2), type_obj=self.roam)
        f.member_state(closed, f.main_of(self.pilot), user=self.pilot, ship_type_id=587, ship_type_name="Rifter")

        page = self.page(self.pilot).content.decode()

        self.assertNotIn("display-6", page)
        self.assertIn('<img class="fo-render" src="https://images.evetech.net/types/587/render?size=128"', page)
        self.assertNotIn("badge", strip_of(page))
