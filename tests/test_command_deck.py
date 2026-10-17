# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The fleet page's command bar and tabs: what each kind of viewer sees (UD5.3), the default tab and the group pill."""

import re
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetOperation, OperationAction

from . import factories as f
from .test_fleet_page import FleetTabTestCase
from .test_group_manage import GroupFleetBase
from .test_views_permissions import add_member_state

Status = FleetOperation.Status


def active_tab(html):
    """The ``data-bs-target`` of the tab button that is marked active on a fleet page."""
    found = re.search(r'class="nav-link active" data-bs-toggle="tab" data-bs-target="(#[a-z]+)"', html)
    return found.group(1) if found else None


def bar_classes(html):
    """The classes of the command bar element."""
    return re.search(r'<div class="([^"]*)" id="fleet-command-bar"', html).group(1)


class CommandBarTests(FleetTabTestCase):
    def html(self, user, operation):
        return self.page(user, operation).content.decode()

    def test_the_bar_replaces_the_badges_and_keeps_the_ids(self):
        operation = f.create_operation(
            self.fc,
            link_only=True,
            multiplier_pending=True,
            review_status="pending",
        )

        html = self.html(self.fc, operation)

        self.assertIn('id="fleet-command-bar"', html)
        self.assertIn('class="fo-commandbar fleet-command-bar mb-3', html)
        for element_id in ("badge-link-only", "badge-review-pending", "badge-multiplier-pending"):
            with self.subTest(element_id=element_id):
                self.assertRegex(html, rf'<span class="fo-pill fo-pill-(info|warn)" id="{element_id}">')
        # The status is a pill, never a Bootstrap badge, anywhere in the header part of the page.
        header = html.split('id="fleet-command-bar"', 1)[1].split('id="operation-tabs"', 1)[0]
        self.assertNotIn('class="badge', header)
        self.assertIn('<span class="fo-pill fo-pill-ok">Active</span>', header)

    def test_the_figures_keep_their_ids_and_sit_in_tiles(self):
        html = self.html(self.fc, self.own_active)

        self.assertIn('<strong class="fo-metric-figure" id="stat-characters">', html)
        self.assertIn('id="stat-last-esi"', html)
        self.assertIn('class="fo-metric fo-metric-sm"', html)
        self.assertNotIn('class="stat-card"', html)

    def test_started_time_is_shown_as_eve_time(self):
        html = self.html(self.fc, self.own_active)

        self.assertIn('id="fleet-started"', html)
        self.assertIn('<time class="fo-time"', html)
        self.assertIn(" EVE</time>", html)

    def test_the_bar_stays_under_the_navbar_only_for_managers_of_an_open_fleet(self):
        for user, operation, sticky in (
            (self.fc, self.own_active, True),
            (self.lead, self.own_active, True),
            (self.member, self.own_active, False),
            (self.other_fc, self.own_active, False),
            (self.fc, self.own_closed, False),
        ):
            with self.subTest(user=user.username, status=operation.status):
                classes = bar_classes(self.html(user, operation))
                self.assertEqual("is-sticky" in classes.split(), sticky, classes)

    def test_end_fleet_asks_before_a_fleet_without_an_end_dialog_is_ended(self):
        html = self.html(self.fc, self.own_active)

        self.assertIn('class="end-fleet-form" data-fo-confirm="End this fleet now and finalize its PAP?"', html)
        self.assertIn('data-fo-confirm-label="End Fleet"', html)
        self.assertIn('<button class="btn btn-outline-danger">End Fleet</button>', html)
        self.assertNotIn('class="btn btn-danger">End Fleet', html)

    def test_a_long_fleet_keeps_its_end_dialog(self):
        long = f.create_operation(self.fc, started_at=timezone.now() - timedelta(minutes=120))
        add_member_state(long, self.fc_main, self.fc)

        html = self.html(self.fc, long)

        self.assertIn('class="btn btn-outline-danger" data-bs-toggle="modal" data-bs-target="#endFleetModal"', html)

    def test_the_buttons_follow_the_viewer(self):
        manager = self.html(self.fc, self.own_active)
        member = self.html(self.member, self.own_active)

        for text in ("Edit Operation", "End Fleet", "Open SRP"):
            self.assertIn(text, manager)
        for text in ("Edit Operation", "End Fleet"):
            self.assertNotIn(text, member)
        self.assertIn(">Open SRP</a>", member)
        # No solid red or green button is left on the bar: outlines only.
        self.assertNotIn("btn-outline-success", manager)

    def test_failed_start_steps_are_named_for_managers_while_the_fleet_is_open(self):
        manager = self.html(self.fc, self.own_active)
        member = self.html(self.member, self.own_active)

        self.assertIn('id="fleet-failed-steps"', manager)
        self.assertIn("1 start step failed", manager)
        self.assertIn("Discord ping", manager.split('id="fleet-failed-steps"', 1)[1].split("</div>", 1)[0])
        self.assertIn('href="#operation-steps"', manager)
        self.assertIn('id="operation-steps"', manager)
        self.assertNotIn('id="fleet-failed-steps"', member)

    def test_no_callout_for_a_closed_fleet_or_for_other_steps(self):
        closed = f.create_operation(self.fc, status=Status.CLOSED)
        OperationAction.objects.create(operation=closed, action="discord_ping", status="failed", error_message="x")
        job = f.create_operation(self.fc)
        OperationAction.objects.create(operation=job, action="kick_member", status="failed", error_message="x")

        self.assertNotIn('id="fleet-failed-steps"', self.html(self.fc, closed))
        self.assertNotIn('id="fleet-failed-steps"', self.html(self.fc, job))

    def test_two_failed_steps_are_counted(self):
        operation = f.create_operation(self.fc)
        for action in ("discord_ping", "motd_update"):
            OperationAction.objects.create(operation=operation, action=action, status="failed", error_message="x")

        html = self.html(self.fc, operation)

        self.assertIn("2 start steps failed", html)
        self.assertIn("Discord ping, MOTD update", html)

    def test_steps_use_pills_and_frames(self):
        html = self.html(self.fc, self.own_active)

        steps = html.split('id="operation-steps"', 1)[1].split("</div>\n  </div>\n  <div class=\"col-lg-4\">", 1)[0]
        self.assertIn('<span class="fo-pill fo-pill-bad">Failed</span>', steps)
        self.assertIn('<span class="fo-pill fo-pill-ok">Success</span>', steps)
        self.assertNotIn("badge", steps)
        self.assertNotIn("border-top", steps)
        self.assertIn("fo-frame", steps)


class DefaultTabTests(FleetTabTestCase):
    def html(self, user, operation):
        return self.page(user, operation).content.decode()

    def test_the_fleet_tab_opens_first_while_a_manager_watches_a_running_fleet(self):
        response = self.page(self.fc, self.own_active)

        self.assertEqual(response.context["default_tab"], "fleet")
        self.assertEqual(active_tab(response.content.decode()), "#fleet")
        self.assertContains(response, 'class="tab-pane fade show active" id="fleet"')
        self.assertContains(response, 'class="tab-pane fade" id="attendance"')

    def test_viewers_of_all_fleets_open_the_running_fleet_too(self):
        response = self.page(self.other_fc, self.own_active)

        self.assertEqual(response.context["default_tab"], "fleet")
        self.assertEqual(active_tab(response.content.decode()), "#fleet")

    def test_members_of_a_running_fleet_open_on_pap(self):
        response = self.page(self.member, self.own_active)

        self.assertEqual(response.context["default_tab"], "attendance")
        html = response.content.decode()
        self.assertEqual(active_tab(html), "#attendance")
        self.assertContains(response, 'class="tab-pane fade show active" id="attendance"')
        self.assertNotContains(response, 'class="tab-pane fade show active" id="members"')

    def test_a_closed_fleet_opens_on_pap_for_everybody(self):
        for user in (self.fc, self.outsider, self.lead):
            with self.subTest(user=user.username):
                response = self.page(user, self.own_closed)
                self.assertEqual(response.context["default_tab"], "attendance")
                self.assertEqual(active_tab(response.content.decode()), "#attendance")

    def test_fleets_without_a_fleet_tab_open_on_pap(self):
        manual = f.create_operation(self.fc, status=Status.CLOSED, is_manual=True, esi_fleet_id=None)
        link_only = f.create_operation(self.fc, link_only=True)
        for operation in (manual, link_only):
            with self.subTest(operation=operation.pk):
                response = self.page(self.fc, operation)
                self.assertEqual(response.context["default_tab"], "attendance")
                self.assertEqual(active_tab(response.content.decode()), "#attendance")

    def test_an_ending_fleet_still_opens_on_the_fleet_tab(self):
        ending = f.create_operation(self.fc, status=Status.ENDING)
        add_member_state(ending, self.fc_main, self.fc, fleet_role="fleet_commander")

        self.assertEqual(active_tab(self.html(self.fc, ending)), "#fleet")

    def test_a_tab_named_in_the_address_still_wins(self):
        html = self.html(self.member, self.own_active)

        # The page script opens the tab of the address on load and keeps the open tab in the address.
        self.assertIn("button(location.hash)", html)
        self.assertIn("bootstrap.Tab.getOrCreateInstance(target).show()", html)
        self.assertIn("history.replaceState(null,'',event.target.dataset.bsTarget)", html)

    def test_every_tab_keeps_its_target_and_the_tabs_are_marked_for_screen_readers(self):
        html = self.html(self.fc, self.own_active)

        for target in ("fleet", "members", "map", "events", "attendance", "roles", "messages"):
            with self.subTest(tab=target):
                self.assertIn(f'role="tab" aria-controls="{target}"', html)
                self.assertIn(f'data-bs-target="#{target}"', html)
                self.assertIn('role="tabpanel" class="tab-pane fade', html)
                self.assertRegex(html, rf'id="{target}"')
        # The order of the tabs is the one the page always had.
        tabs = html.split('id="operation-tabs"', 1)[1].split("</ul>", 1)[0]
        order = re.findall(r'data-bs-target="#([a-z]+)"', tabs)
        self.assertEqual(order, ["fleet", "members", "map", "events", "attendance", "roles", "messages"])


class VisibilityMatrixTests(FleetTabTestCase):
    """UD5.3: the new parts of the fleet page stay inside the rules for who sees a running fleet."""

    def setUp(self):
        super().setUp()
        # A step that names fleet members: members of the fleet must not read its details (VIS(2)).
        OperationAction.objects.create(
            operation=self.own_active,
            action="kick_member",
            status="failed",
            error_message="Could not kick Neutral Pod.",
        )

    def html(self, user, operation=None):
        return self.page(user, operation or self.own_active).content.decode()

    def assert_not_managing(self, html):
        for text in (
            "fo-row-actions",
            "fleet-quick-move",
            "fleet-quick-kick",
            "data-fleet-move-open",
            "data-fleet-rename-open",
            'id="fleet-invite-modal"',
            'id="fleet-refresh-form"',
            "Edit Operation",
            "End Fleet",
        ):
            with self.subTest(hidden=text):
                self.assertNotIn(text, html)
        self.assertNotIn("is-sticky", bar_classes(html).split())

    def test_a_tracked_member_sees_the_bar_and_their_own_rows_only(self):
        html = self.html(self.member)

        for text in (
            'data-bs-target="#fleet"',
            'id="fleet-root"',
            'id="fleet-live-state"',
            'id="fleet-status"',
            'id="fleet-tree"',
            'id="fleet-composition"',
            'class="fleet-system',
            "fleet-fc-system",
            "Neutral Pod",
            "Kick Neutral Pod",
        ):
            with self.subTest(hidden=text):
                self.assertNotIn(text, html)
        self.assert_not_managing(html)
        self.assertEqual(active_tab(html), "#attendance")
        # K3: the PAP tab lists the member's own rows, not the FC's.
        pap = html.split('id="pap-preview-fragment"', 1)[1].split('id="roles"', 1)[0]
        self.assertIn("member Main", pap)
        self.assertNotIn("fc Main", pap)
        # The steps keep their rule: no details of steps that name fleet members.
        self.assertIn("member-step-hidden", html)
        self.assertNotIn("Could not kick Neutral Pod.", html)
        self.assertIn('id="composition-hidden"', html)

    def test_a_viewer_of_all_fleets_sees_the_fleet_but_cannot_change_it(self):
        html = self.html(self.other_fc)

        for text in (
            'data-bs-target="#fleet"',
            'id="fleet-root"',
            'id="fleet-live-state"',
            'id="fleet-tree"',
            'id="fleet-composition"',
            "Neutral Pod",
            "fleet-fc-system",
        ):
            with self.subTest(shown=text):
                self.assertIn(text, html)
        self.assert_not_managing(html)
        self.assertIn('data-can-manage="0" data-can-write="0"', html)
        # The "…" menus that change the fleet are not rendered either.
        self.assertNotIn("fleet-member-menu", html)
        self.assertNotIn("Make squad commander", html)
        self.assertEqual(active_tab(html), "#fleet")

    def test_a_manager_sees_everything(self):
        html = self.html(self.fc)

        for text in (
            "fo-row-actions",
            "fleet-quick-move",
            "fleet-quick-kick",
            "fleet-member-menu",
            'id="fleet-live-state"',
            "Edit Operation",
            "End Fleet",
            'id="fleet-invite-modal"',
            'draggable="true"',
        ):
            with self.subTest(shown=text):
                self.assertIn(text, html)
        self.assertIn("is-sticky", bar_classes(html).split())

    def test_the_live_indicator_carries_the_words_of_every_state(self):
        html = self.html(self.fc)

        indicator = html.split('id="fleet-live-state"', 1)[1].split("</button>", 1)[0]
        for state in ("live", "paused", "stale", "error"):
            with self.subTest(state=state):
                self.assertIn(f'data-live-text="{state}"', indicator)
        self.assertIn("Paused — click to resume", indicator)
        self.assertIn("No connection — click to retry", indicator)
        # The test fleet was last read ten minutes ago, which is stale; a fleet read just now is live.
        self.assertIn('data-state="stale"', html.split('id="fleet-live-state"', 1)[1][:80])
        FleetOperation.objects.filter(pk=self.own_active.pk).update(last_esi_update=timezone.now())
        fresh = self.html(self.fc)
        self.assertIn('data-state="live"', fresh.split('id="fleet-live-state"', 1)[1][:80])

    def test_the_closed_fleet_shows_its_composition_to_everybody_and_opens_on_pap(self):
        for user in (self.fc, self.outsider, self.lead):
            with self.subTest(user=user.username):
                html = self.html(user, self.own_closed)
                self.assertIn('id="fleet-composition"', html)
                self.assertIn("Composition</button>", html)
                self.assertEqual(active_tab(html), "#attendance")
                self.assertNotIn('id="fleet-live-state"', html)
                self.assertNotIn("is-sticky", bar_classes(html).split())
                self.assertNotIn("fo-row-actions", html)


class GroupFleetPageTests(GroupFleetBase):
    def setUp(self):
        super().setUp()
        self.operation = self.group_fleet()
        f.member_state(self.operation, self.leader_char, user=self.leader, role="fleet_commander")
        self.pilot_char = f.main_of(self.member)
        f.member_state(self.operation, self.pilot_char, user=self.member)

    def page(self, user, operation=None):
        operation = operation or self.operation
        response = self.client_for(user).get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_group_pill_names_the_group_with_its_icon_and_title(self):
        html = self.page(self.leader)

        self.assertIn(
            '<span class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet">'
            '<i class="fa-solid fa-users" aria-hidden="true"></i>Example Fleet Group</span>',
            html,
        )

    def test_an_alliance_fleet_has_no_group_pill(self):
        alliance = f.create_operation(self.fc, type_obj=self.stratop)

        self.assertNotIn("fo-pill-group", self.page(self.fc, alliance))

    def test_the_pill_costs_no_query(self):
        # The pill comes from the stored name: the page makes the same queries with and without it.
        client = self.client_for(self.leader)
        url = reverse("fleetops:operation_detail", args=[self.operation.uuid])
        client.get(url)
        counts = []
        for name in (self.group.name, ""):
            FleetOperation.objects.filter(pk=self.operation.pk).update(group_name=name)
            with CaptureQueriesContext(connection) as queries:
                html = client.get(url).content.decode()
            counts.append(len(queries))
            self.assertEqual("fo-pill-group" in html, bool(name))
        self.assertEqual(counts[0], counts[1], counts)

    def test_the_starting_leader_manages_the_fleet(self):
        html = self.page(self.leader)

        self.assertIn('id="fleet-root"', html)
        self.assertIn("is-sticky", bar_classes(html).split())
        self.assertEqual(active_tab(html), "#fleet")

    def test_another_leader_of_the_group_gets_the_read_only_view_of_a_member(self):
        html = self.page(self.second)

        for text in ('id="fleet-root"', 'id="fleet-live-state"', "fo-row-actions", "Edit Operation", "End Fleet",
                     'data-bs-target="#fleet"'):
            with self.subTest(hidden=text):
                self.assertNotIn(text, html)
        self.assertNotIn("is-sticky", bar_classes(html).split())
        self.assertEqual(active_tab(html), "#attendance")
        self.assertIn("fo-pill-group", html)


class SourceTests(TestCase):
    """The page script and the template keep the contracts the harness and the other lanes rely on."""

    def test_the_command_bar_and_metric_ids_exist_once(self):
        from django.template.loader import render_to_string

        operation = f.create_operation(f.create_user("bar-fc", perms=f.FC_PERMS))
        html = render_to_string(
            "fleetops/operation/_header.html",
            {"operation": operation, "actions": [], "members": [], "unique_players": 0, "attendance_total": 0},
        )
        for element_id in ("stat-characters", "stat-last-esi", "fleet-command-bar"):
            self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)
