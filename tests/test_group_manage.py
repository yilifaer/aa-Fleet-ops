# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Group fleets: who manages and sees them, the SRP gate, the server-side start rules and the start form
choices."""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fleetops import texts
from fleetops.forms import ScheduledFleetForm, StartFleetForm
from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetStartPreset,
    GroupAccess,
    OperationAction,
    OperationRoleAssignment,
    PapLink,
    SrpFleetLink,
)
from fleetops.services import fleet_access
from fleetops.services.operations import create_srp_at_start, retry_srp, start_fleet
from fleetops.services.presets import save_preset
from fleetops.services.scheduling import schedule_fleet
from fleetops.srp.links import operation_srp_context

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
FLEETS = GroupAccess.Mode.FLEETS
PING = GroupAccess.Mode.PING
JSON = {"HTTP_ACCEPT": "application/json"}


def fresh(user):
    return get_user_model().objects.get(pk=user.pk)


class GroupFleetBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.settings(srp_auto_create=False)
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.stratop = f.fleet_type("StratOp", "1.00")
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.leader_char = f.main_of(self.leader)
        f.add_token(self.leader, self.leader_char)
        self.second = f.create_user("second", perms=f.MEMBER_PERMS, main_name="Pilot B")
        self.other_leader = f.create_user("otherleader", perms=f.MEMBER_PERMS, main_name="Pilot C")
        self.member = f.create_user("member", perms=f.MEMBER_PERMS, main_name="Pilot D")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader, self.second])
        self.access = f.group_access(self.group, FLEETS, types=[self.activity])
        self.other_group = f.aa_group("Example Other Group", leaders=[self.other_leader])
        self.other_access = f.group_access(self.other_group, FLEETS, types=[self.activity])
        self.ping_group = f.aa_group("Example Ping Group", leaders=[self.leader])
        f.group_access(self.ping_group, PING)
        self.alliance = f.ping_channel("Alliance Pings")
        self.channel = f.group_channel("Group Pings", groups=[self.group])
        self.other_channel = f.group_channel("Other Group Pings", groups=[self.other_group])
        self.ping_channel = f.group_channel("Ping Group Pings", groups=[self.ping_group])

    def group_fleet(self, user=None, group=None, **extra):
        group = group or self.group
        values = {"group": group, "group_name": group.name, "type_obj": self.activity, "ping_channel": self.channel}
        values.update(extra)
        return f.create_operation(user or self.leader, **values)

    def client_for(self, user):
        self.client.force_login(user)
        return self.client


class ManageGroupFleetTests(GroupFleetBase):
    def setUp(self):
        super().setUp()
        self.operation = self.group_fleet()
        f.member_state(self.operation, self.leader_char, user=self.leader, role="fleet_commander")
        self.pilot_char = f.main_of(self.member)
        f.member_state(self.operation, self.pilot_char, user=self.member)

    def post(self, user, name, *args, data=None, **headers):
        return self.client_for(user).post(reverse(f"fleetops:{name}", args=args), data or {}, **headers)

    def test_the_leader_ends_their_group_fleet_with_a_multiplier(self):
        response = self.post(self.leader, "end_fleet", self.operation.uuid, data={"attendance_multiplier": "1"})

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.status, Status.CLOSED)
        self.assertEqual(self.operation.attendance_multiplier, 1)

    def test_the_leader_kicks_a_member(self):
        with mock.patch(
            "fleetops.views_controls.kick_member", return_value=SimpleNamespace(ok=True, message="Kicked.")
        ) as kick:
            response = self.post(self.leader, "kick_member", self.operation.uuid, self.pilot_char.character_id)

        self.assertEqual(response.status_code, 302)
        kick.assert_called_once()
        self.assertEqual(kick.call_args.kwargs["actor"], self.leader)

    def test_the_leader_reads_the_fleet_tab(self):
        response = self.client_for(self.leader).get(
            reverse("fleetops:fleet_state", args=[self.operation.uuid]), **JSON
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("tree_html", response.json())
        self.assertTrue(fleet_access.can_manage_fleet(fresh(self.leader), self.operation))

    def test_the_leader_creates_a_pap_link(self):
        response = self.post(self.leader, "create_pap_link", self.operation.uuid, data={"minutes": "30"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PapLink.objects.filter(operation=self.operation).count(), 1)

    def test_the_leader_assigns_a_special_role(self):
        role = f.special_role("Scout")

        response = self.post(
            self.leader,
            "add_operation_role",
            self.operation.uuid,
            data={"role": role.key, "character_id": str(self.pilot_char.character_id)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(OperationRoleAssignment.objects.filter(operation=self.operation, special_role=role).exists())

    def test_the_leader_excludes_the_fleet_from_statistics(self):
        response = self.post(
            self.leader,
            "set_stats_mode",
            self.operation.uuid,
            data={"mode": FleetOperation.StatsMode.EXCLUDE, "note": "Training run"},
        )

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual(self.operation.stats_mode, FleetOperation.StatsMode.EXCLUDE)

    def test_the_leader_opens_the_edit_page(self):
        response = self.client_for(self.leader).get(reverse("fleetops:edit_operation", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 200)

    def test_other_fleets_answer_not_found(self):
        others = {
            "another leader's group fleet": self.group_fleet(self.other_leader, self.other_group),
            "a fleet of the same group started by another leader": self.group_fleet(self.second),
            "an alliance fleet": f.create_operation(self.fc),
        }
        for label, operation in others.items():
            with self.subTest(fleet=label):
                response = self.post(self.leader, "end_fleet", operation.uuid, data={"attendance_multiplier": "1"})
                self.assertEqual(response.status_code, 404)
                response = self.post(self.leader, "create_pap_link", operation.uuid, data={"minutes": "30"})
                self.assertEqual(response.status_code, 404)
                operation.refresh_from_db()
                self.assertEqual(operation.status, Status.ACTIVE)

    def test_a_leader_who_loses_the_group_mid_fleet_manages_it_no_longer(self):
        changes = {
            "row deactivated": lambda: GroupAccess.objects.filter(pk=self.access.pk).update(is_active=False),
            "leadership removed": lambda: self.group.authgroup.group_leaders.remove(self.leader.pk),
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                change()
                response = self.post(
                    self.leader, "end_fleet", self.operation.uuid, data={"attendance_multiplier": "1"}
                )
                self.assertEqual(response.status_code, 404)
                self.operation.refresh_from_db()
                self.assertEqual(self.operation.status, Status.ACTIVE)
                GroupAccess.objects.filter(pk=self.access.pk).update(is_active=True)
                self.group.authgroup.group_leaders.add(self.leader.pk)

    def test_a_deleted_group_leaves_the_fleet_to_the_fc_leads(self):
        self.group.delete()
        self.operation.refresh_from_db()

        self.assertIsNone(self.operation.group_id)
        self.assertEqual(self.operation.group_name, "Example Fleet Group")
        # The leader still leads another group, so the decorator lets them in and the fleet check says 404.
        response = self.post(self.leader, "end_fleet", self.operation.uuid, data={"attendance_multiplier": "1"})
        self.assertEqual(response.status_code, 404)
        response = self.post(self.lead, "end_fleet", self.operation.uuid, data={"attendance_multiplier": "1"})
        self.assertEqual(response.status_code, 302)

    def test_other_leaders_of_the_group_open_it_read_only(self):
        response = self.client_for(self.second).get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])
        second = fresh(self.second)
        self.assertFalse(fleet_access.can_view_fleet_tab(second, self.operation))
        self.assertFalse(fleet_access.can_view_live_fleet(second, self.operation))
        self.assertNotContains(response, reverse("fleetops:end_fleet", args=[self.operation.uuid]))
        self.assertEqual(
            self.client_for(self.second)
            .get(reverse("fleetops:fleet_state", args=[self.operation.uuid]), **JSON)
            .status_code,
            404,
        )

    def test_members_without_a_grant_are_refused_by_the_decorator(self):
        response = self.post(self.member, "end_fleet", self.operation.uuid, data={"attendance_multiplier": "1"})

        self.assertEqual(response.status_code, 403)

    def test_leaders_of_other_groups_and_members_do_not_see_the_fleet(self):
        for user in (self.other_leader, self.fc):
            with self.subTest(user=user.username):
                response = self.client_for(user).get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
                expected = 200 if user.has_perm("fleetops.view_all_fleets") else 404
                self.assertEqual(response.status_code, expected)


class DashboardTests(GroupFleetBase):
    def active(self, user):
        response = self.client_for(user).get(reverse("fleetops:dashboard"))
        self.assertEqual(response.status_code, 200)
        return list(response.context["active_operations"]), response.context["running_fleets"]

    def test_leaders_see_the_running_fleets_of_their_groups(self):
        operation = self.group_fleet()
        closed = self.group_fleet(status=Status.CLOSED)

        active, rows = self.active(self.second)

        self.assertEqual(active, [operation])
        self.assertNotIn(closed, active)
        self.assertEqual(
            rows,
            [
                {
                    "operation": operation,
                    "can_manage": False,
                    "show_system": False,
                    "fc_system_name": "",
                    "member_count": 0,
                }
            ],
        )
        self.assertEqual(self.active(self.other_leader)[0], [])
        self.assertEqual(self.active(self.member)[0], [])

    def test_viewers_of_all_fleets_see_every_running_fleet(self):
        operation = self.group_fleet()
        alliance = f.create_operation(self.lead)

        active, _rows = self.active(self.fc)

        self.assertEqual(set(active), {operation, alliance})


class SrpGateTests(GroupFleetBase):
    def setUp(self):
        super().setUp()
        f.srp_settings(enabled=True)
        f.settings(srp_auto_create=True, srp_provider="fleetops")

    def test_group_fleets_get_no_srp_link_while_the_group_switch_is_off(self):
        operation = self.group_fleet()

        create_srp_at_start(operation)

        action = OperationAction.objects.get(operation=operation, action="srp_link")
        self.assertEqual(action.status, ActionStatus.SKIPPED)
        self.assertEqual(action.error_message, texts.GROUP_NO_SRP(group="Example Fleet Group"))
        self.assertFalse(SrpFleetLink.objects.exists())
        retried = retry_srp(operation)
        self.assertEqual(retried.status, ActionStatus.SKIPPED)
        self.assertEqual(retried.error_message, texts.GROUP_NO_SRP(group="Example Fleet Group"))
        self.assertFalse(SrpFleetLink.objects.exists())
        self.assertFalse(operation_srp_context(operation, fresh(self.leader)).get("srp_card", {}).get("can_create"))
        self.assertFalse(operation_srp_context(operation, fresh(self.lead)).get("srp_card", {}).get("can_create"))

    def test_the_provider_refuses_group_fleets_too(self):
        from fleetops.providers.srp import create_srp_link

        result = create_srp_link(self.group_fleet(), "fleetops")

        self.assertFalse(result.created)
        self.assertEqual(result.message, texts.GROUP_NO_SRP(group="Example Fleet Group"))

    def test_with_the_switch_on_the_link_is_created_at_start(self):
        GroupAccess.objects.filter(pk=self.access.pk).update(srp_enabled=True)
        operation = self.group_fleet()

        create_srp_at_start(operation)

        self.assertTrue(SrpFleetLink.objects.filter(operation=operation).exists())

    def test_alliance_fleets_are_unchanged(self):
        operation = f.create_operation(self.fc)

        create_srp_at_start(operation)

        self.assertTrue(SrpFleetLink.objects.filter(operation=operation).exists())
        card = operation_srp_context(f.create_operation(self.fc), fresh(self.fc))["srp_card"]
        self.assertTrue(card["can_create"])

    def test_the_retry_view_refuses_for_the_leader(self):
        operation = self.group_fleet()

        response = self.client_for(self.leader).post(reverse("fleetops:retry_srp", args=[operation.uuid]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SrpFleetLink.objects.exists())


class ServerStartRuleTests(GroupFleetBase):
    """``start_fleet`` and ``schedule_fleet`` check who may start which fleet before ESI or the database."""

    def cleaned(self, character, **extra):
        data = {
            "operation_mode": "full",
            "fc_character_id": str(character.character_id),
            "fleet_type": self.activity,
            "formup": "Jita",
            "ping_channel": self.channel,
        }
        data.update(extra)
        return data

    def assertRefusedBeforeEsi(self, user, data, message):
        with self.assertRaises(PermissionError) as raised:
            start_fleet(user=fresh(user), cleaned_data=data, request_id=uuid.uuid4())
        self.assertEqual(str(raised.exception), message)
        self.assertEqual(self.esi.calls, [])
        self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()

    def test_forged_starts_are_refused_before_any_esi_call(self):
        char = self.leader_char
        cases = {
            "alliance fleet by a leader": (self.cleaned(char), texts.GROUP_REQUIRED()),
            "alliance channel for an alliance fleet": (
                self.cleaned(char, ping_channel=self.alliance, fleet_type=self.stratop),
                texts.GROUP_REQUIRED(),
            ),
            "type not allowed": (
                self.cleaned(char, group=self.group, fleet_type=self.stratop),
                texts.GROUP_FLEET_TYPE_NOT_ALLOWED(type="StratOp", group="Example Fleet Group"),
            ),
            "alliance channel for the group": (
                self.cleaned(char, group=self.group, ping_channel=self.alliance),
                texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Alliance Pings", group="Example Fleet Group"),
            ),
            "another group's channel": (
                self.cleaned(char, group=self.group, ping_channel=self.other_channel),
                texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Other Group Pings", group="Example Fleet Group"),
            ),
            "another group": (
                self.cleaned(char, group=self.other_group, ping_channel=self.other_channel),
                texts.GROUP_NOT_LED(group="Example Other Group"),
            ),
            "ping-only group": (
                self.cleaned(char, group=self.ping_group, ping_channel=self.ping_channel),
                texts.GROUP_NO_FLEETS(group="Example Ping Group"),
            ),
        }
        for label, (data, message) in cases.items():
            with self.subTest(case=label):
                self.assertRefusedBeforeEsi(self.leader, data, message)

    def test_an_fc_cannot_post_to_a_reserved_channel(self):
        self.assertRefusedBeforeEsi(
            self.fc,
            self.cleaned(self.fc_char, fleet_type=self.stratop),
            texts.CHANNEL_NOT_ALLOWED(name="Group Pings"),
        )

    def test_a_group_without_an_access_row_or_a_deleted_group(self):
        GroupAccess.objects.filter(pk=self.access.pk).delete()
        self.assertRefusedBeforeEsi(
            self.leader, self.cleaned(self.leader_char, group=self.group), texts.GROUP_NOT_LED(group="Example Fleet Group")
        )

    def test_pre_pings_of_groups_and_of_users_without_start_fleet_are_refused(self):
        when = {"scheduled_at": None, "is_pre_ping": True}
        with self.assertRaises(PermissionError) as raised:
            schedule_fleet(user=fresh(self.leader), cleaned_data=self.cleaned(self.leader_char, group=self.group, **when))
        self.assertEqual(str(raised.exception), texts.GROUP_NO_PRE_PING())
        with self.assertRaises(PermissionError) as raised:
            schedule_fleet(user=fresh(self.leader), cleaned_data=self.cleaned(self.leader_char, **when))
        self.assertEqual(str(raised.exception), texts.GROUP_REQUIRED())
        with self.assertRaises(PermissionError):
            schedule_fleet(user=fresh(self.fc), cleaned_data=self.cleaned(self.fc_char, fleet_type=self.stratop, **when))
        self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()

    def start_group_fleet(self, user, character, **extra):
        self.esi.put_in_fleet(character)
        self.esi.members[FLEET_ID] = [f.esi_member(character)]
        return start_fleet(
            user=fresh(user), cleaned_data=self.cleaned(character, group=self.group, **extra), request_id=uuid.uuid4()
        )

    def test_a_leader_starts_a_group_fleet_that_keeps_its_group(self):
        operation = self.start_group_fleet(self.leader, self.leader_char)

        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual((operation.group, operation.group_name), (self.group, "Example Fleet Group"))
        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual(entry.new_value["group_id"], self.group.pk)
        self.assertEqual(entry.new_value["group"], "Example Fleet Group")
        self.assertEqual(OperationAction.objects.get(operation=operation, action="discord_ping").status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 1)

    def test_a_leader_through_a_leader_group_starts_a_group_fleet(self):
        leaders = f.aa_group("Example Leaders")
        via = f.create_user("via", perms=f.MEMBER_PERMS, main_name="Pilot E")
        via.groups.add(leaders)
        self.group.authgroup.group_leader_groups.add(leaders.pk)
        via_char = f.main_of(via)
        f.add_token(via, via_char)

        operation = self.start_group_fleet(via, via_char)

        self.assertEqual(operation.group, self.group)

    def test_a_leader_who_is_also_an_fc(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)

        operation = self.start_group_fleet(self.fc, self.fc_char)
        self.assertEqual(operation.group, self.group)
        FleetOperation.objects.all().delete()
        # As an alliance fleet the reserved channel stays closed to them; the alliance channel is open.
        with self.assertRaises(PermissionError):
            start_fleet(user=fresh(self.fc), cleaned_data=self.cleaned(self.fc_char, fleet_type=self.stratop), request_id=uuid.uuid4())
        alliance = start_fleet(
            user=fresh(self.fc),
            cleaned_data=self.cleaned(self.fc_char, fleet_type=self.stratop, ping_channel=self.alliance),
            request_id=uuid.uuid4(),
        )
        self.assertIsNone(alliance.group)
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").order_by("pk").last().new_value["group_id"], None)

    def test_a_leader_of_two_groups_uses_each_group_with_its_own_channel(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        with self.assertRaises(PermissionError) as raised:
            start_fleet(
                user=fresh(self.leader),
                cleaned_data=self.cleaned(self.leader_char, group=self.group, ping_channel=self.other_channel),
                request_id=uuid.uuid4(),
            )
        self.assertEqual(
            str(raised.exception),
            texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Other Group Pings", group="Example Fleet Group"),
        )
        self.esi.put_in_fleet(self.leader_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.leader_char)]
        operation = start_fleet(
            user=fresh(self.leader),
            cleaned_data=self.cleaned(self.leader_char, group=self.other_group, ping_channel=self.other_channel),
            request_id=uuid.uuid4(),
        )
        self.assertEqual(operation.group, self.other_group)

    def test_a_pap_only_group_fleet_needs_no_channel(self):
        operation = self.start_group_fleet(self.leader, self.leader_char, operation_mode="attendance_only", ping_channel=self.alliance)

        self.assertEqual(operation.group, self.group)
        self.assertIsNone(operation.ping_channel)
        self.discord_post.assert_not_called()


class StartFormChoiceTests(GroupFleetBase):
    """The start, scheduled and preset forms offer only what the user may use; crafted posts are refused."""

    def start_data(self, character, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(character.character_id),
            "fleet_type": str(self.activity.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
        }
        data.update(extra)
        return data

    def test_the_leader_is_offered_only_the_groups_types_and_channels(self):
        form = StartFleetForm(user=fresh(self.leader))

        self.assertEqual(list(form.fields["fleet_type"].queryset), [self.activity])
        self.assertEqual(set(form.fields["ping_channel"].queryset), {self.channel})
        self.assertEqual(form.group_rules(), {})

    def test_an_fc_is_never_offered_a_reserved_channel(self):
        form = StartFleetForm(user=fresh(self.fc))

        self.assertEqual(set(form.fields["fleet_type"].queryset), {self.activity, self.stratop})
        self.assertEqual(set(form.fields["ping_channel"].queryset), {self.alliance})
        lead_form = StartFleetForm(user=fresh(self.lead))
        self.assertEqual(
            set(lead_form.fields["ping_channel"].queryset),
            {self.alliance, self.channel, self.other_channel, self.ping_channel},
        )

    def test_an_fc_who_leads_a_group_gets_both(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)

        form = StartFleetForm(user=fresh(self.fc))

        self.assertEqual(set(form.fields["ping_channel"].queryset), {self.alliance, self.channel})

    def test_crafted_posts_by_a_leader_to_alliance_channels_and_fleet_types_are_refused(self):
        cases = {
            "alliance channel": self.start_data(self.leader_char, ping_channel=str(self.alliance.pk)),
            "another group's channel": self.start_data(self.leader_char, ping_channel=str(self.other_channel.pk)),
            "alliance fleet type": self.start_data(self.leader_char, fleet_type=str(self.stratop.pk)),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                form = StartFleetForm(data, user=fresh(self.leader))
                self.assertFalse(form.is_valid())
                response = self.client_for(self.leader).post(reverse("fleetops:start_fleet"), data)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(FleetOperation.objects.exists())
        self.discord_post.assert_not_called()
        self.assertEqual(self.esi.calls, [])

    def test_a_leader_posting_the_start_form_without_a_group_is_refused_before_esi(self):
        response = self.client_for(self.leader).post(reverse("fleetops:start_fleet"), self.start_data(self.leader_char))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, texts.GROUP_REQUIRED())
        self.assertFalse(FleetOperation.objects.exists())
        self.assertEqual(self.esi.calls, [])
        self.discord_post.assert_not_called()

    def test_a_crafted_post_by_an_fc_to_a_reserved_channel_is_refused(self):
        data = self.start_data(self.fc_char, fleet_type=str(self.stratop.pk))

        self.assertFalse(StartFleetForm(data, user=fresh(self.fc)).is_valid())
        response = self.client_for(self.fc).post(reverse("fleetops:start_fleet"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FleetOperation.objects.exists())

    def test_the_scheduled_fleet_form_offers_alliance_channels_only(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)
        operation = f.create_scheduled_operation(self.fc, ping_channel=self.alliance, type_obj=self.stratop)

        form = ScheduledFleetForm(operation=operation, user=fresh(self.fc))

        self.assertEqual(set(form.fields["ping_channel"].queryset), {self.alliance})
        self.assertFalse(form.fields["ping_channel"].required)

    def test_a_scheduled_fleet_whose_channel_was_reserved_asks_for_another(self):
        operation = f.create_scheduled_operation(self.fc, ping_channel=self.alliance, type_obj=self.stratop)
        self.alliance.allowed_groups.set([self.group])
        self.alliance.fcs_allowed = False
        self.alliance.save()

        form = ScheduledFleetForm(operation=operation, user=fresh(self.fc))

        self.assertNotIn(self.alliance, form.fields["ping_channel"].queryset)
        self.assertTrue(form.fields["ping_channel"].required)
        self.assertIsNone(form.initial["ping_channel"])
        posted = ScheduledFleetForm(
            {"fleet_type": str(self.stratop.pk), "formup": "Jita", "ping_channel": str(self.alliance.pk)},
            operation=operation,
            user=fresh(self.fc),
        )
        self.assertFalse(posted.is_valid())
        self.assertIn("ping_channel", posted.errors)

    def test_presets_drop_channels_and_types_the_user_may_not_use(self):
        preset = save_preset(
            fresh(self.fc),
            "Example Preset",
            {"ping_channel": str(self.channel.pk), "fleet_type": str(self.stratop.pk), "formup": "Jita"},
        )
        fields = preset.values["fields"]
        self.assertNotIn("ping_channel", fields)
        self.assertIn("fleet_type", fields)

        preset = save_preset(fresh(self.leader), "Leader Preset", {"ping_channel": str(self.alliance.pk), "fleet_type": str(self.stratop.pk)})
        self.assertNotIn("ping_channel", preset.values["fields"])
        self.assertNotIn("fleet_type", preset.values["fields"])
        self.assertEqual(FleetStartPreset.objects.count(), 2)

    def test_a_deleted_channel_leaves_group_fleets_without_a_post(self):
        operation = self.group_fleet(ping_text="Group ping", send_ping=True)
        self.channel.delete()
        operation.refresh_from_db()

        from fleetops.services.operations import retry_ping

        action = retry_ping(operation, actor=fresh(self.leader))

        self.assertEqual(action.status, ActionStatus.SKIPPED)
        self.discord_post.assert_not_called()
