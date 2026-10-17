# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps Administration, Groups: the access rows, their list cells, the leaders card and what happens when
Alliance Auth deletes a group."""

import json

from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops import texts
from fleetops.forms_groups import GroupAccessForm, group_cell, group_list_prepare
from fleetops.models import AuditLog, DiscordWebhook, GroupAccess, StandalonePing

from . import factories as f

PING = GroupAccess.Mode.PING
FLEETS = GroupAccess.Mode.FLEETS
CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


class GroupConfigFixture(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = f.create_user("admin", perms=CONFIG_PERMS)
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.outsider = f.create_user("outsider", main_name="Pilot B")
        self.via = f.create_user("via", perms=f.MEMBER_PERMS, main_name="Pilot C")
        self.leader_group = f.aa_group("Example Fleet Leaders")
        self.via.groups.add(self.leader_group)
        self.group = f.aa_group(
            "Example Fleet Group", leaders=[self.leader, self.outsider], leader_groups=[self.leader_group]
        )
        self.access = f.group_access(self.group, FLEETS, types=[self.activity])
        self.channel = f.group_channel("Group Pings", groups=[self.group])

    def url(self, name, *args):
        return reverse(f"fleetops:configuration_{name}", args=["groups", *args])


class AccessTests(GroupConfigFixture):
    def test_only_administrators_change_group_access(self):
        others = (self.leader, f.create_user("fc", perms=f.FC_PERMS), f.create_user("lead", perms=[
            perm for perm in f.FC_LEAD_PERMS if perm != "fleetops.manage_configuration"
        ]))
        for user in others:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url("list")).status_code, 403)
                self.assertEqual(self.client.get(self.url("edit", self.access.pk)).status_code, 403)
                response = self.client.post(self.url("edit", self.access.pk), {"group": self.group.pk, "mode": PING})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.client.post(self.url("delete", self.access.pk)).status_code, 403)
        self.access.refresh_from_db()
        self.assertEqual(self.access.mode, FLEETS)
        self.client.force_login(self.admin)
        for name, args in (("list", ()), ("add", ()), ("edit", (self.access.pk,))):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(self.url(name, *args)).status_code, 200)


class FormTests(GroupConfigFixture):
    def form(self, data, instance=None):
        return GroupAccessForm(data, instance=instance)

    def test_groups_with_a_row_are_not_offered_again(self):
        free = f.aa_group("Example Free Group")
        offered = set(GroupAccessForm().fields["group"].queryset)
        self.assertIn(free, offered)
        self.assertNotIn(self.group, offered)
        self.assertIn(self.group, set(GroupAccessForm(instance=self.access).fields["group"].queryset))

    def test_fleet_types_show_their_fc_points(self):
        labels = [label for _value, label in GroupAccessForm().fields["fleet_types"].choices]
        self.assertIn("Group Activity — 0.00 FC points", labels)
        self.assertIn("StratOp — 2.00 FC points", labels)
        self.stratop.is_active = False
        self.stratop.save()
        self.access.fleet_types.add(self.stratop)
        labels = [label for _value, label in GroupAccessForm(instance=self.access).fields["fleet_types"].choices]
        self.assertIn("StratOp — 2.00 FC points (switched off)", labels)
        self.assertNotIn("StratOp — 2.00 FC points (switched off)", [
            label for _value, label in GroupAccessForm().fields["fleet_types"].choices
        ])

    def test_a_group_that_runs_fleets_needs_a_fleet_type(self):
        group = f.aa_group("Example New Group")
        form = self.form({"group": group.pk, "mode": FLEETS, "is_active": "on"})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["fleet_types"], ["A group that runs fleets needs at least one fleet type."])
        self.assertTrue(self.form({"group": group.pk, "mode": PING, "is_active": "on"}).is_valid())

    def test_warnings_for_fc_points_and_a_missing_channel(self):
        group = f.aa_group("Example New Group")
        form = self.form({"group": group.pk, "mode": FLEETS, "fleet_types": [self.stratop.pk, self.activity.pk]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            [str(warning) for warning in form.warnings],
            [
                "Fleets of type StratOp give FC points; group leaders who run them get FC credit.",
                "No ping channel is assigned to this group yet. Assign one under Ping Channels.",
            ],
        )
        self.assertEqual(form.audit_extra, {"group": "Example New Group"})
        form = self.form({"group": self.group.pk, "mode": FLEETS, "fleet_types": [self.activity.pk]}, self.access)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.warnings, [])

    def test_the_selects_are_styled(self):
        form = GroupAccessForm()
        self.assertEqual(form.fields["group"].widget.attrs["class"], "form-select")
        self.assertEqual(form.fields["mode"].widget.attrs["class"], "form-select")
        self.assertEqual(form.fields["note"].widget.attrs["class"], "form-control")


class SaveAndAuditTests(GroupConfigFixture):
    def test_a_saved_row_is_audited_with_its_fleet_types_and_group_name(self):
        group = f.aa_group("Example New Group")
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url("add"),
            {"group": group.pk, "mode": FLEETS, "fleet_types": [self.activity.pk, self.stratop.pk], "is_active": "on"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        row = GroupAccess.objects.get(group=group)
        entry = AuditLog.objects.get(action="configuration.create", object_type="GroupAccess")
        self.assertEqual(entry.new_value["fleet_types"], sorted([self.activity.pk, self.stratop.pk]))
        self.assertEqual(entry.new_value["group"], "Example New Group")
        self.assertEqual(entry.object_id, str(row.pk))
        shown = [str(message) for message in response.context["messages"]]
        self.assertIn("Fleets of type StratOp give FC points; group leaders who run them get FC credit.", shown)

    def test_switching_to_pings_only_keeps_the_ticked_fleet_types(self):
        self.client.force_login(self.admin)
        self.client.post(
            self.url("edit", self.access.pk),
            {"group": self.group.pk, "mode": PING, "fleet_types": [self.activity.pk], "is_active": "on"},
        )
        self.access.refresh_from_db()
        self.assertEqual(self.access.mode, PING)
        self.assertEqual(list(self.access.fleet_types.all()), [self.activity])
        entry = AuditLog.objects.get(action="configuration.update", object_type="GroupAccess")
        self.assertEqual((entry.old_value["mode"], entry.new_value["mode"]), (FLEETS, PING))
        self.assertEqual(entry.new_value["group"], "Example Fleet Group")


class ListTests(GroupConfigFixture):
    def rows(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url("list"))
        self.assertEqual(response.status_code, 200)
        return response, {row["object"].pk: dict(row["values"]) for row in response.context["rows"]}

    def test_the_cells(self):
        ping_only = f.group_access(f.aa_group("Example Ping Group"), PING)
        response, rows = self.rows()
        cells = {str(label): str(value) for label, value in rows[self.access.pk].items()}
        self.assertIn("fo-pill-info", cells["Leaders may"])
        self.assertIn("Pings and fleets with PAP", cells["Leaders may"])
        self.assertIn("Group Activity — 0.00 FC points", cells["Fleet types"])
        edit = reverse("fleetops:configuration_edit", args=["ping-channels", self.channel.pk])
        self.assertIn(f'<a href="{edit}">Group Pings</a>', cells["Ping channels"])
        self.assertIn('<span class="text-nowrap">Pilot A</span>', cells["Leaders (from Alliance Auth)"])
        self.assertIn("Pilot A", cells["Leaders (from Alliance Auth)"])
        self.assertIn("Pilot C", cells["Leaders (from Alliance Auth)"])
        self.assertIn('Pilot B <span class="text-muted">(no FleetOps access)</span>', cells["Leaders (from Alliance Auth)"])
        self.assertIn("fo-pill-neutral", cells["SRP"])
        self.assertIn("fo-pill-ok", cells["Active"])
        other = {str(label): str(value) for label, value in rows[ping_only.pk].items()}
        self.assertIn("Ping only", other["Leaders may"])
        self.assertEqual(other["Fleet types"], "")
        self.assertIn("No channel", other["Ping channels"])
        self.assertIn("No leaders in Alliance Auth", other["Leaders (from Alliance Auth)"])
        self.assertNotContains(response, 'class="badge')

    def test_a_switched_off_channel_is_named_but_does_not_count(self):
        DiscordWebhook.objects.filter(pk=self.channel.pk).update(is_active=False)
        _response, rows = self.rows()
        cell = str({str(label): value for label, value in rows[self.access.pk].items()}["Ping channels"])
        self.assertIn("Group Pings", cell)
        self.assertIn("Disabled", cell)
        self.assertIn("No channel", cell)

    def test_the_cells_work_without_the_prepare_hook(self):
        self.assertIn("Pilot A", str(group_cell(self.access, "leaders")))
        self.assertIn("Group Pings", str(group_cell(self.access, "channels")))
        self.assertIs(group_cell(self.access, "group"), NotImplemented)

    def test_the_list_does_not_cost_more_with_more_groups_or_leaders(self):
        def count():
            self.client.force_login(self.admin)
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(self.url("list")).status_code, 200)
            return len(captured)

        count()  # The first page of a test also sets up Alliance Auth's menu.
        small = count()
        for number in range(4):
            leaders = [f.create_user(f"leader{number}{index}", perms=f.MEMBER_PERMS) for index in range(3)]
            leader_group = f.aa_group(f"Example Leaders {number}")
            leader_group.user_set.add(f.create_user(f"via{number}", perms=f.MEMBER_PERMS))
            group = f.aa_group(f"Example Group {number}", leaders=leaders, leader_groups=[leader_group])
            f.group_access(group, FLEETS, types=[self.activity, self.stratop])
            f.group_channel(f"Channel {number}", groups=[group, self.group])
        self.assertEqual(count(), small)

    def test_prepare_fills_the_rows_with_a_fixed_number_of_queries(self):
        one = GroupAccess.objects.select_related("group").get(pk=self.access.pk)
        with CaptureQueriesContext(connection) as captured:
            group_list_prepare([one])
        small = len(captured)
        rows = []
        for number in range(3):
            group = f.aa_group(f"Example Group {number}", leaders=[self.leader])
            rows.append(f.group_access(group, PING))
        objects = list(GroupAccess.objects.select_related("group"))
        with CaptureQueriesContext(connection) as captured:
            group_list_prepare(objects)
        self.assertEqual(len(captured), small)
        prepared = {obj.pk: obj for obj in objects}[self.access.pk]
        self.assertEqual([row.name for row in prepared.fleetops_direct_leaders], ["Pilot A", "Pilot B"])
        self.assertEqual(
            [(name, [row.name for row in rows]) for name, rows in prepared.fleetops_leader_groups],
            [("Example Fleet Leaders", ["Pilot C"])],
        )


class LeadersCardTests(GroupConfigFixture):
    def test_the_edit_page_shows_the_leaders_from_alliance_auth(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url("edit", self.access.pk))
        self.assertContains(response, 'id="group-leaders"')
        html = response.content.decode()
        card = html.split('id="group-leaders"', 1)[1]
        self.assertIn("Pilot A", card)
        self.assertIn("Members of the leader group Example Fleet Leaders", card)
        self.assertIn("Pilot C", card)
        self.assertIn("no FleetOps access", card)
        # The link to Alliance Auth's admin is for staff only.
        self.assertNotIn("Edit the leaders in Alliance Auth", card)
        self.admin.is_staff = True
        self.admin.save()
        response = self.client.get(self.url("edit", self.access.pk))
        self.assertContains(response, "Edit the leaders in Alliance Auth")
        self.assertContains(response, f"/{self.group.pk}/change/")

    def test_a_group_without_leaders_and_a_new_row(self):
        empty = f.group_access(f.aa_group("Example Empty Group"), PING)
        self.client.force_login(self.admin)
        response = self.client.get(self.url("edit", empty.pk))
        self.assertContains(response, 'id="group-leaders-empty"')
        response = self.client.get(self.url("add"))
        self.assertContains(response, 'id="group-leaders-new"')


class GroupDeletionTests(GroupConfigFixture):
    """Deleting a group in Alliance Auth is audited; fleets and pings keep its name; channels follow 5-7."""

    def test_the_deletion_is_audited_and_names_are_kept(self):
        other = f.aa_group("Example Other Group")
        f.group_access(other, PING)
        shared = f.group_channel("Shared Pings", groups=[self.group, other])
        open_channel = f.group_channel("Open Pings", groups=[self.group], fcs_allowed=True)
        fleet = f.create_operation(self.leader, group=self.group, group_name=self.group.name)
        ping = f.standalone_ping(sender=self.leader, channel=self.channel, group=self.group)
        access_pk = self.access.pk

        Group.objects.get(pk=self.group.pk).delete()

        entry = AuditLog.objects.get(action="group.deleted")
        self.assertIsNone(entry.actor)
        self.assertEqual((entry.object_type, entry.object_id), ("Group", str(self.group.pk)))
        self.assertEqual(entry.old_value["name"], "Example Fleet Group")
        self.assertEqual(entry.old_value["access"]["id"], access_pk)
        self.assertEqual(entry.old_value["access"]["fleet_types"], [self.activity.pk])
        self.assertEqual(entry.old_value["channels"], sorted([self.channel.pk, shared.pk, open_channel.pk]))
        self.assertEqual(entry.new_value, {"channels_reopened": [self.channel.pk]})
        self.assertEqual(entry.reason, texts.AUDIT_GROUP_DELETED())
        self.assertNotIn("webhooks", json.dumps([entry.old_value, entry.new_value]))
        self.assertFalse(GroupAccess.objects.filter(pk=access_pk).exists())
        fleet.refresh_from_db()
        ping.refresh_from_db()
        self.assertEqual((fleet.group, fleet.group_name), (None, "Example Fleet Group"))
        self.assertEqual((ping.group, ping.group_name), (None, "Example Fleet Group"))
        # The channel left without a group is an alliance channel again; the shared one stays reserved.
        self.assertTrue(DiscordWebhook.objects.get(pk=self.channel.pk).fcs_allowed)
        self.assertFalse(DiscordWebhook.objects.get(pk=shared.pk).fcs_allowed)
        self.assertEqual(list(DiscordWebhook.objects.get(pk=shared.pk).allowed_groups.all()), [other])
        self.assertTrue(DiscordWebhook.objects.get(pk=open_channel.pk).fcs_allowed)

    def test_the_proxy_of_alliance_auth_is_seen_too(self):
        from allianceauth.groupmanagement.models import Group as AuthProxyGroup

        AuthProxyGroup.objects.get(pk=self.group.pk).delete()
        self.assertTrue(AuditLog.objects.filter(action="group.deleted", object_id=str(self.group.pk)).exists())
        self.assertTrue(DiscordWebhook.objects.get(pk=self.channel.pk).fcs_allowed)

    def test_a_group_fleetops_does_not_use_leaves_no_entry(self):
        unused = f.aa_group("Example Unused Group")
        unused.delete()
        self.leader_group.delete()
        self.assertFalse(AuditLog.objects.filter(action="group.deleted").exists())

    def test_a_group_with_only_a_channel_is_audited(self):
        group = f.aa_group("Example Channel Group")
        channel = f.group_channel("Only Channel", groups=[group])
        group.delete()
        entry = AuditLog.objects.get(action="group.deleted")
        self.assertIsNone(entry.old_value["access"])
        self.assertEqual(entry.new_value, {"channels_reopened": [channel.pk]})
        self.assertFalse(StandalonePing.objects.exists())
