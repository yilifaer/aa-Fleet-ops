# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Groups whose access row counts all members as leaders (a members group and a leaders group per small group,
no group leaders recorded in Alliance Auth): the rights, the refusals, the next-request effect, the
administration pages and migration 0022."""

import uuid
from importlib import import_module
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from allianceauth.groupmanagement.models import AuthGroup

from fleetops import texts
from fleetops.forms import StartFleetForm
from fleetops.forms_groups import GroupAccessForm, group_cell, group_list_prepare
from fleetops.models import AuditLog, GroupAccess, StandalonePing
from fleetops.services import group_access
from fleetops.srp import access as srp_access

from . import factories as f

PING = GroupAccess.Mode.PING
FLEETS = GroupAccess.Mode.FLEETS
POST = "fleetops.providers.pings.requests.post"
CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


def fresh(user):
    """The user as the next request loads it: no cached permissions, no cached leadership."""
    return get_user_model().objects.get(pk=user.pk)


def discord():
    return mock.Mock(status_code=204, text="")


class MembersFixture(TestCase):
    """A leaders group that counts all members as leaders, a second group that does not, and their channels."""

    def setUp(self):
        super().setUp()
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.member = f.create_user("member", perms=f.MEMBER_PERMS, main_name="Pilot M")
        self.other = f.create_user("other", perms=f.MEMBER_PERMS, main_name="Pilot O")
        self.recorded = f.create_user("recorded", perms=f.MEMBER_PERMS, main_name="Pilot R")
        self.admin = f.create_user("admin", perms=CONFIG_PERMS)
        self.leaders = f.aa_group("Example Squad leaders")
        self.rank = f.aa_group("Example Wing leaders", leaders=[self.recorded])
        self.leaders.user_set.add(self.member)
        self.access = f.group_access(self.leaders, FLEETS, types=[self.activity], members_are_leaders=True, srp_enabled=True)
        self.rank_access = f.group_access(self.rank, FLEETS, types=[self.activity])
        self.alliance = f.ping_channel("Alliance Pings")
        self.channel = f.group_channel("Squad Pings", groups=[self.leaders])
        self.rank_channel = f.group_channel("Wing Pings", groups=[self.rank])

    def data(self, channel, group=None, **extra):
        values = {
            "request_id": str(uuid.uuid4()),
            "ping_channel": str(channel.pk),
            "ping_mentions": ["none"],
            "additional_message": "Form up",
        }
        if group is not None:
            values["group"] = str(group.pk)
        values.update(extra)
        return values

    def send(self, user, data):
        self.client.force_login(user)
        return self.client.post(reverse("fleetops:send_ping"), data)

    def start_form(self, user, channel, group=None):
        character = f.main_of(user)
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(character.character_id),
            "fleet_type": str(self.activity.pk),
            "formup": "Jita",
            "ping_channel": str(channel.pk),
            "fleet_group": str((group or self.leaders).pk),
        }
        return StartFleetForm(data, user=fresh(user))


class LeaderAccessTests(MembersFixture):
    def test_a_member_leads_the_group_like_a_recorded_leader(self):
        access = group_access.leader_access(fresh(self.member))

        self.assertEqual(access.group_ids, {self.leaders.pk})
        grant = access.grant(self.leaders.pk)
        self.assertTrue(grant.runs_fleets)
        self.assertTrue(grant.srp_enabled)
        self.assertEqual(grant.fleet_type_ids, frozenset({self.activity.pk}))
        self.assertEqual(group_access.visible_group_ids(fresh(self.member)), {self.leaders.pk})
        self.assertTrue(group_access.can_start_fleets(fresh(self.member)))
        self.assertTrue(group_access.can_send_pings(fresh(self.member)))

    def test_with_the_option_off_members_lead_nothing(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(members_are_leaders=False)

        user = fresh(self.member)
        self.assertEqual(group_access.leader_access(user).grants, ())
        self.assertFalse(group_access.can_send_pings(user))
        self.assertFalse(group_access.can_start_fleets(user))

    def test_someone_outside_the_group_leads_nothing(self):
        self.assertEqual(group_access.leader_access(fresh(self.other)).grants, ())

    def test_an_inactive_row_gives_nothing(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(is_active=False)

        self.assertEqual(group_access.leader_access(fresh(self.member)).grants, ())

    def test_an_inactive_user_and_a_user_without_basic_access_are_refused_without_a_query(self):
        no_access = f.create_user("noaccess")
        inactive = f.member("inactive", perms=f.MEMBER_PERMS, active=False)
        for user in (no_access, inactive):
            self.leaders.user_set.add(user)
            user = fresh(user)
            user.has_perm("fleetops.basic_access")
            with self.subTest(user=user.username):
                with self.assertNumQueries(0):
                    self.assertEqual(group_access.leader_access(user).grants, ())

    def test_removal_from_the_group_counts_from_the_next_request(self):
        user = fresh(self.member)
        self.assertTrue(group_access.leader_access(user).grants)

        self.leaders.user_set.remove(self.member)

        self.assertTrue(group_access.leader_access(user).grants)
        self.assertEqual(group_access.leader_access(fresh(self.member)).grants, ())

    def test_recorded_leaders_and_leader_groups_still_work(self):
        via_group = f.aa_group("Example Squad boss")
        via = f.create_user("via", perms=f.MEMBER_PERMS)
        via_group.user_set.add(via)
        self.rank.authgroup.group_leader_groups.add(via_group.pk)

        self.assertEqual(group_access.leader_access(fresh(self.recorded)).group_ids, {self.rank.pk})
        self.assertEqual(group_access.leader_access(fresh(via)).group_ids, {self.rank.pk})

    def test_a_member_who_is_also_a_recorded_leader_has_one_grant(self):
        self.leaders.authgroup.group_leaders.add(self.member.pk)

        self.assertEqual(len(group_access.leader_access(fresh(self.member)).grants), 1)

    def test_the_query_count_is_the_same_as_for_recorded_leaders(self):
        for user, queries in ((self.member, 2), (self.other, 1), (self.recorded, 2)):
            user = fresh(user)
            user.has_perm("fleetops.basic_access")
            with self.subTest(user=user.username):
                with self.assertNumQueries(queries):
                    group_access.leader_access(user)
                with self.assertNumQueries(0):
                    group_access.leader_access(user)

    def test_the_lookup_does_not_grow_with_more_groups(self):
        for number in range(4):
            group = f.aa_group(f"Example Extra {number}")
            group.user_set.add(self.member)
            f.group_access(group, FLEETS, types=[self.activity], members_are_leaders=True)
        user = fresh(self.member)
        user.has_perm("fleetops.basic_access")
        with self.assertNumQueries(2):
            self.assertEqual(len(group_access.leader_access(user).grants), 5)

    def test_srp_follows_the_leaders_group(self):
        self.assertTrue(srp_access.leads_srp_group(fresh(self.member)))
        self.leaders.user_set.remove(self.member)
        self.assertFalse(srp_access.leads_srp_group(fresh(self.member)))


class ChannelRuleTests(MembersFixture):
    def test_a_member_may_use_the_assigned_channel_and_no_other(self):
        user = fresh(self.member)

        self.assertEqual(group_access.channel_refusal(user, self.channel, group_id=self.leaders.pk), "")
        for channel, group in ((self.rank_channel, self.rank), (self.rank_channel, self.leaders), (self.alliance, None)):
            with self.subTest(channel=channel.name, group=group):
                self.assertNotEqual(
                    group_access.channel_refusal(fresh(self.member), channel, group_id=group.pk if group else None), ""
                )

    def test_with_the_option_off_the_channel_is_refused(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(members_are_leaders=False)

        refusal = group_access.channel_refusal(fresh(self.member), self.channel, group_id=self.leaders.pk)

        self.assertEqual(refusal, texts.GROUP_NOT_LED(group="Example Squad leaders"))

    def test_the_send_time_check_follows_a_removal(self):
        self.leaders.user_set.remove(self.member)

        refusal = group_access.channel_refusal(fresh(self.member), self.channel, group_id=self.leaders.pk)

        self.assertEqual(refusal, texts.GROUP_NOT_LED(group="Example Squad leaders"))

    def test_start_rules(self):
        user = fresh(self.member)
        ok = group_access.fleet_start_refusal(
            user, group_id=self.leaders.pk, fleet_type=self.activity, channel=self.channel, sends=True, pre_ping=False
        )
        other = group_access.fleet_start_refusal(
            user, group_id=self.rank.pk, fleet_type=self.activity, channel=self.rank_channel, sends=True, pre_ping=False
        )
        self.assertEqual(ok, ("", ""))
        self.assertEqual(other[0], "fleet_group")


class PingPageTests(MembersFixture):
    def test_a_member_sends_to_the_assigned_channel(self):
        with mock.patch(POST, return_value=discord()) as post:
            response = self.send(self.member, self.data(self.channel, self.leaders))

        self.assertEqual(response.status_code, 302)
        post.assert_called_once()
        self.assertEqual(StandalonePing.objects.get().group, self.leaders)

    def test_crafted_forms_for_other_channels_and_groups_are_refused(self):
        for channel, group in ((self.rank_channel, self.leaders), (self.rank_channel, self.rank), (self.alliance, self.leaders), (self.channel, None)):
            with self.subTest(channel=channel.name, group=group):
                with mock.patch(POST) as post:
                    response = self.send(self.member, self.data(channel, group))
                self.assertEqual(response.status_code, 200)
                post.assert_not_called()
        self.assertFalse(StandalonePing.objects.exists())

    def test_with_the_option_off_the_member_cannot_open_or_send(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(members_are_leaders=False)

        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("fleetops:send_ping")).status_code, 403)
        with mock.patch(POST) as post:
            self.assertEqual(self.send(self.member, self.data(self.channel, self.leaders)).status_code, 403)
        post.assert_not_called()

    def test_removal_from_the_group_refuses_the_next_request(self):
        self.leaders.user_set.remove(self.member)

        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("fleetops:send_ping")).status_code, 403)
        with mock.patch(POST) as post:
            self.assertEqual(self.send(self.member, self.data(self.channel, self.leaders)).status_code, 403)
        post.assert_not_called()

    def test_an_inactive_user_and_a_user_without_basic_access_cannot_send(self):
        no_access = f.create_user("noaccess")
        self.leaders.user_set.add(no_access)
        with mock.patch(POST) as post:
            self.assertEqual(self.send(no_access, self.data(self.channel, self.leaders)).status_code, 403)
        post.assert_not_called()

    def test_recorded_leaders_still_send(self):
        with mock.patch(POST, return_value=discord()):
            response = self.send(self.recorded, self.data(self.rank_channel, self.rank))
        self.assertEqual(response.status_code, 302)

    def test_the_menu_shows_start_and_send_ping_to_members(self):
        for user, shown in ((self.member, True), (self.other, False)):
            self.client.force_login(user)
            html = self.client.get(reverse("fleetops:my_statistics")).content.decode()
            with self.subTest(user=user.username):
                self.assertEqual(f'href="{reverse("fleetops:send_ping")}"' in html, shown)
                self.assertEqual(f'href="{reverse("fleetops:start_fleet")}"' in html, shown)


class StartFormTests(MembersFixture):
    def test_a_member_may_start_a_fleet_for_the_group_with_its_channel(self):
        form = self.start_form(self.member, self.channel)
        form.is_valid()
        self.assertNotIn("fleet_group", form.errors)
        self.assertNotIn("ping_channel", form.errors)
        self.assertNotIn("fleet_type", form.errors)

    def test_other_channels_and_groups_are_refused(self):
        form = self.start_form(self.member, self.rank_channel)
        form.is_valid()
        self.assertIn("ping_channel", form.errors)
        form = self.start_form(self.member, self.rank_channel, self.rank)
        form.is_valid()
        self.assertIn("fleet_group", form.errors)

    def test_with_the_option_off_it_is_refused(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(members_are_leaders=False)
        form = self.start_form(self.member, self.channel)
        self.assertFalse(form.is_valid())
        self.assertIn("ping_channel", form.errors)
        self.assertIn("fleet_type", form.errors)


class AdministrationTests(MembersFixture):
    def url(self, name, *args):
        return reverse(f"fleetops:configuration_{name}", args=["groups", *args])

    def test_the_option_is_off_by_default_and_has_a_label_and_help_text(self):
        form = GroupAccessForm()
        field = form.fields["members_are_leaders"]

        self.assertFalse(field.initial)
        self.assertEqual(str(field.label), "All members of this group count as its leaders")
        self.assertIn("two Alliance Auth groups per small group", str(field.help_text))
        self.assertFalse(GroupAccess.objects.create(group=f.aa_group("Example New")).members_are_leaders)

    def test_saving_the_option_is_audited_and_warns(self):
        group = f.aa_group("Example New leaders")
        group.user_set.add(self.other)
        self.client.force_login(self.admin)

        response = self.client.post(
            self.url("add"), {"group": group.pk, "members_are_leaders": "on", "mode": PING, "is_active": "on"}, follow=True
        )

        row = GroupAccess.objects.get(group=group)
        self.assertTrue(row.members_are_leaders)
        entry = AuditLog.objects.get(action="configuration.create", object_type="GroupAccess", object_id=str(row.pk))
        self.assertIs(entry.new_value["members_are_leaders"], True)
        shown = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("Every active member of Example New leaders", shown)
        self.assertIn("Members counted: 1", shown)

    def test_the_notice_counts_only_active_members_with_access(self):
        group = f.aa_group("Example Count leaders")
        group.user_set.add(self.other, f.member("inactive", perms=f.MEMBER_PERMS, active=False), f.create_user("noaccess"))
        self.client.force_login(self.admin)

        response = self.client.post(
            self.url("add"), {"group": group.pk, "members_are_leaders": "on", "mode": PING, "is_active": "on"}, follow=True
        )

        shown = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("Members counted: 1.", shown)

    def test_a_group_people_can_join_is_refused(self):
        for field in ("open", "public"):
            group = f.aa_group(f"Example {field} leaders")
            AuthGroup.objects.filter(group=group).update(internal=False, **{field: True})
            self.client.force_login(self.admin)

            response = self.client.post(
                self.url("add"), {"group": group.pk, "members_are_leaders": "on", "mode": PING, "is_active": "on"}
            )

            self.assertEqual(response.status_code, 200)
            self.assertFalse(GroupAccess.objects.filter(group=group).exists())
            self.assertIn("members_are_leaders", response.context["form"].errors)

    def test_a_row_saved_earlier_for_a_group_that_is_no_longer_internal_grants_nothing(self):
        AuthGroup.objects.filter(group=self.leaders).update(internal=False)

        self.assertEqual(group_access.leader_access(fresh(self.member)).grants, ())
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(self.url("edit", self.access.pk)), 'id="group-leaders-members-blocked"')
        objects = list(GroupAccess.objects.select_related("group"))
        group_list_prepare(objects)
        cell = str(group_cell([o for o in objects if o.pk == self.access.pk][0], "leaders"))
        self.assertIn("not internal", cell)

    def test_the_list_cell_counts_members_with_access(self):
        self.leaders.user_set.add(f.create_user("noaccess2"))
        objects = list(GroupAccess.objects.select_related("group"))
        group_list_prepare(objects)
        cell = str(group_cell([o for o in objects if o.pk == self.access.pk][0], "leaders"))

        self.assertIn("All members (1 of 2 have access)", cell)

    def test_switching_the_option_off_is_audited(self):
        self.client.force_login(self.admin)

        self.client.post(self.url("edit", self.access.pk), {"group": self.leaders.pk, "mode": PING, "is_active": "on"})

        self.access.refresh_from_db()
        self.assertFalse(self.access.members_are_leaders)
        entry = AuditLog.objects.filter(action="configuration.update", object_type="GroupAccess").latest("pk")
        self.assertIs(entry.old_value["members_are_leaders"], True)
        self.assertIs(entry.new_value["members_are_leaders"], False)

    def test_the_leaders_card_lists_the_members_read_only(self):
        self.client.force_login(self.admin)
        html = self.client.get(self.url("edit", self.access.pk)).content.decode()
        card = html.split('id="group-leaders"', 1)[1]

        self.assertIn("All members of Example Squad leaders", card)
        self.assertIn("Pilot M", card)
        self.assertNotIn('id="group-leaders-empty"', card)
        self.assertNotIn("<input", card)
        other = self.client.get(self.url("edit", self.rank_access.pk)).content.decode()
        self.assertNotIn("All members of", other.split('id="group-leaders"', 1)[1])
        self.assertIn("Pilot R", other)

    def test_the_card_for_a_group_without_active_members(self):
        self.leaders.user_set.remove(self.member)
        self.client.force_login(self.admin)

        self.assertContains(self.client.get(self.url("edit", self.access.pk)), 'id="group-leaders-members-empty"')

    def test_the_list_cell(self):
        objects = list(GroupAccess.objects.select_related("group"))
        group_list_prepare(objects)
        cells = {obj.group.name: str(group_cell(obj, "leaders")) for obj in objects}

        self.assertIn("All members (1)", cells["Example Squad leaders"])
        self.assertIn("Pilot R", cells["Example Wing leaders"])
        self.assertNotIn("All members", cells["Example Wing leaders"])

    def test_the_list_costs_no_more_with_more_members(self):
        self.client.force_login(self.admin)
        self.client.get(self.url("list"))
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get(self.url("list")).status_code, 200)
        for number in range(5):
            self.leaders.user_set.add(f.create_user(f"extra{number}", perms=f.MEMBER_PERMS))
        group = f.aa_group("Example Another leaders")
        f.group_access(group, PING, members_are_leaders=True)
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(self.client.get(self.url("list")).status_code, 200)
        self.assertEqual(len(large), len(small))

    def test_prepare_costs_a_fixed_number_of_queries(self):
        with CaptureQueriesContext(connection) as small:
            group_list_prepare(list(GroupAccess.objects.select_related("group")))
        for number in range(3):
            group = f.aa_group(f"Example Group {number}")
            group.user_set.add(self.other)
            f.group_access(group, PING, members_are_leaders=True)
        with CaptureQueriesContext(connection) as large:
            group_list_prepare(list(GroupAccess.objects.select_related("group")))
        self.assertEqual(len(large), len(small))

    def test_members_of_ignores_inactive_users(self):
        inactive = f.member("inactive", perms=f.MEMBER_PERMS, active=False)
        self.leaders.user_set.add(inactive)

        rows = group_access.members_of([self.leaders.pk])[self.leaders.pk]

        self.assertEqual([row.name for row in rows], ["Pilot M"])


class MigrationTests(TransactionTestCase):
    """0022 adds the switch, off for every existing row, and can be reversed."""

    serialized_rollback = True
    before = [("fleetops", "0021_group_access")]
    after = [("fleetops", "0022_group_members_are_leaders")]

    def migrate(self, nodes):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(nodes)
        return executor.loader.project_state(nodes).apps

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("fleetops"))

    def test_forward_and_back(self):
        self.addCleanup(self.migrate_to_latest)
        old_apps = self.migrate(self.before)
        Group = old_apps.get_model("auth", "Group")
        Access = old_apps.get_model("fleetops", "GroupAccess")
        group = Group.objects.create(name="Example Migration Group")
        Access.objects.create(group=group, mode="ping", srp_enabled=True, note="kept")
        self.assertNotIn("members_are_leaders", {field.name for field in Access._meta.get_fields()})

        new_apps = self.migrate(self.after)
        row = new_apps.get_model("fleetops", "GroupAccess").objects.get(group_id=group.pk)
        self.assertIs(row.members_are_leaders, False)
        self.assertEqual((row.mode, row.srp_enabled, row.note), ("ping", True, "kept"))
        new_apps.get_model("fleetops", "GroupAccess").objects.filter(pk=row.pk).update(members_are_leaders=True)

        back = self.migrate(self.before)
        kept = back.get_model("fleetops", "GroupAccess").objects.get(group_id=group.pk)
        self.assertEqual((kept.mode, kept.srp_enabled, kept.note), ("ping", True, "kept"))
        self.assertNotIn("members_are_leaders", {field.name for field in kept._meta.get_fields()})

        again = self.migrate(self.after)
        self.assertIs(again.get_model("fleetops", "GroupAccess").objects.get(group_id=group.pk).members_are_leaders, False)

    def test_it_follows_0021_and_needs_no_import_of_the_app(self):
        module = import_module("fleetops.migrations.0022_group_members_are_leaders")
        self.assertEqual(module.Migration.dependencies, [("fleetops", "0021_group_access")])
