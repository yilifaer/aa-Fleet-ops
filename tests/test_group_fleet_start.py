# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Starting fleets for a group: the "Fleet for" choice of the Start Fleet page, what it offers, how each refusal
of the server shows at its field, presets with a group, the live preview and a start through the page."""

import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import translation

from fleetops import texts
from fleetops.forms import StartFleetForm
from fleetops.models import AuditLog, FleetOperation, FleetStartPreset, GroupAccess
from fleetops.services import group_access
from fleetops.services.presets import load_preset, save_preset

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .static_files import static_source
from .test_operations_tracking import FLEET_ID, FakeESIMixin

FLEETS = GroupAccess.Mode.FLEETS
PING = GroupAccess.Mode.PING


def fresh(user):
    return get_user_model().objects.get(pk=user.pk)


class GroupStartBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.settings(srp_auto_create=False)
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.mining = f.fleet_type("Group Mining", "0.50")
        self.stratop = f.fleet_type("StratOp", "1.00")
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.leader_char = f.main_of(self.leader)
        f.add_token(self.leader, self.leader_char)
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader])
        f.group_access(self.group, FLEETS, types=[self.activity])
        self.other_group = f.aa_group("Example Mining Group")
        f.group_access(self.other_group, FLEETS, types=[self.mining])
        self.ping_group = f.aa_group("Example Ping Group", leaders=[self.leader])
        f.group_access(self.ping_group, PING)
        self.alliance = f.ping_channel("Alliance Pings")
        self.channel = f.group_channel("Group Pings", groups=[self.group])
        self.other_channel = f.group_channel("Mining Pings", groups=[self.other_group])
        self.ping_channel = f.group_channel("Ping Group Pings", groups=[self.ping_group])

    def login(self, user):
        self.client.force_login(user)
        return self.client

    def page(self, user):
        return self.login(user).get(reverse("fleetops:start_fleet"))

    def data(self, character, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(character.character_id),
            "fleet_type": str(self.activity.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "fleet_group": str(self.group.pk),
        }
        data.update(extra)
        return data

    def form(self, user, **extra):
        character = f.main_of(user)
        return StartFleetForm(self.data(character, **extra), user=fresh(user))

    def rules(self, response):
        found = re.search(r'<script id="fleet-group-rules" type="application/json">(.*?)</script>', response.content.decode())
        return json.loads(found.group(1)) if found else None


class FleetForSelectorTests(GroupStartBase):
    def test_a_leader_without_start_fleet_sees_the_fixed_group_and_only_its_types_and_channels(self):
        response = self.page(self.leader)

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<input type="hidden" name="fleet_group" id="id_fleet_group" value="{self.group.pk}">', content)
        self.assertRegex(
            content,
            r'<span class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet">'
            r'<i class="fa-solid fa-users" aria-hidden="true"></i>Example Fleet Group</span>',
        )
        self.assertNotIn('type="radio" name="fleet_group"', content)
        types = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', content.split('name="fleet_type"')[1].split("</select>")[0])
        self.assertEqual(types, [(str(self.activity.pk), "Group Activity")])
        channels = content.split('name="ping_channel"')[1].split("</select>")[0]
        self.assertIn("Group Pings", channels)
        for other in ("Alliance Pings", "Mining Pings", "Ping Group Pings"):
            self.assertNotIn(other, channels)
        self.assertEqual(set(self.rules(response)), {str(self.group.pk)})

    def test_the_page_of_a_leader_has_no_pap_link_fleet_link_and_no_pre_ping(self):
        content = self.page(self.leader).content.decode()

        self.assertNotIn("link-only-fleet-link", content)
        self.assertIn("One form, one start action.", content)
        self.assertRegex(content, r'<input type="radio" name="fleet_timing" value="pre_ping" class="fo-seg-input" id="id_fleet_timing_1" disabled>')
        self.assertIn('id="fleet-group-hint"', content)
        self.assertIn(reverse("fleetops:send_ping"), content)

    def test_an_fc_keeps_the_link_and_has_no_group_field(self):
        response = self.page(self.fc)

        content = response.content.decode()
        self.assertIn("link-only-fleet-link", content)
        self.assertNotIn("fleet_group", content)
        self.assertNotIn("fleet-group-rules", content)
        self.assertNotIn(" disabled>", content.split('name="fleet_timing"')[1].split("</div>")[0])
        self.assertEqual(response.context["group_rules"], {})

    def test_an_fc_who_leads_a_group_chooses_between_alliance_and_group_fleets(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)

        response = self.page(self.fc)

        content = response.content.decode()
        self.assertRegex(content, r'name="fleet_group" value="" class="fo-seg-input" id="id_fleet_group_0" checked><label for="id_fleet_group_0">Alliance fleet</label>')
        self.assertRegex(content, rf'name="fleet_group" value="{self.group.pk}" class="fo-seg-input" id="id_fleet_group_1"><label for="id_fleet_group_1">Example Fleet Group</label>')
        rules = self.rules(response)
        self.assertEqual(set(rules), {"", str(self.group.pk)})
        self.assertEqual(rules[""]["channels"], [self.alliance.pk])
        self.assertEqual(rules[str(self.group.pk)], {"name": "Example Fleet Group", "fleet_types": [self.activity.pk], "channels": [self.channel.pk]})
        self.assertIn(self.stratop.pk, rules[""]["fleet_types"])
        channel_rules = json.loads(re.search(r'<script id="ping-channel-rules" type="application/json">(.*?)</script>', content).group(1))
        self.assertEqual(channel_rules[str(self.channel.pk)]["groups"], [self.group.pk])
        self.assertFalse(channel_rules[str(self.channel.pk)]["alliance"])
        self.assertTrue(channel_rules[str(self.alliance.pk)]["alliance"])
        self.assertEqual(channel_rules[str(self.alliance.pk)]["groups"], [])
        # Pre ping stays possible for the alliance fleet.
        self.assertNotRegex(content, r'name="fleet_timing" value="pre_ping" class="fo-seg-input" id="id_fleet_timing_1" disabled')

    def test_a_leader_of_two_groups_picks_one(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        response = self.page(self.leader)

        content = response.content.decode()
        self.assertEqual(content.count('type="radio" name="fleet_group"'), 2)
        self.assertNotIn(">Alliance fleet<", content)
        self.assertNotRegex(content, r'name="fleet_group"[^>]* checked')
        self.assertNotIn('type="hidden" name="fleet_group"', content)
        types = {key for key in self.rules(response)}
        self.assertEqual(types, {str(self.group.pk), str(self.other_group.pk)})

    def test_many_groups_are_a_list(self):
        for number in range(5):
            group = f.aa_group(f"Example Extra Group {number}", leaders=[self.leader])
            f.group_access(group, FLEETS, types=[self.activity])

        content = self.page(self.leader).content.decode()

        self.assertIn('<select name="fleet_group" class="form-select"', content)
        self.assertNotIn('type="radio" name="fleet_group"', content)

    def test_the_group_lookup_costs_one_query_for_an_fc_and_two_for_an_fc_who_leads(self):
        def lookup(user):
            user = fresh(user)
            user.has_perm("fleetops.basic_access")  # the permission cache is not part of the group lookup
            with CaptureQueriesContext(connection) as captured:
                group_access.leader_access(user)
            return len(captured)

        self.assertEqual(lookup(self.fc), 1)
        self.group.authgroup.group_leaders.add(self.fc.pk)
        self.assertEqual(lookup(self.fc), 2)
        # More groups, types and channels cost nothing more.
        for number in range(4):
            group = f.aa_group(f"Example More Group {number}", leaders=[self.fc])
            f.group_access(group, FLEETS, types=[self.activity, self.mining])
            f.group_channel(f"More Pings {number}", groups=[group])
        self.assertEqual(lookup(self.fc), 2)

    def test_the_start_page_does_not_query_more_with_more_groups(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)
        self.login(self.fc)
        self.client.get(reverse("fleetops:start_fleet"))  # warm up (sessions, caches)

        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get(reverse("fleetops:start_fleet"))
            return len(captured)

        one = queries()
        for number in range(3):
            group = f.aa_group(f"Example More Group {number}", leaders=[self.fc])
            f.group_access(group, FLEETS, types=[self.activity])
            f.group_channel(f"More Pings {number}", groups=[group])

        self.assertLessEqual(queries(), one + 1)

    def test_the_script_filters_by_group_and_does_not_pre_ping_a_group_fleet(self):
        script = static_source("start_fleet.js")

        for marker in (
            "fleet-group-rules",
            "limitOptions(fleetType, rule && rule.fleet_types)",
            "limitOptions(channel, rule && rule.channels)",
            "radio.disabled = prePingLocked || attendance || group",
            "const prePingLocked =",
            "groupHint.classList.toggle('d-none', !group)",
            "fo-pill-group",
            "groupPill.dataset.title",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)
        self.assertNotIn("innerHTML", script)


class RefusalAtItsFieldTests(GroupStartBase):
    def errors(self, user, **extra):
        form = self.form(user, **extra)
        self.assertFalse(form.is_valid())
        return form.errors

    def test_a_leader_without_a_group_is_asked_for_one(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        errors = self.errors(self.leader, fleet_group="")

        self.assertEqual(errors["fleet_group"], [texts.GROUP_REQUIRED()])
        self.assertEqual(set(errors), {"fleet_group"})

    def test_a_fleet_type_of_another_group_belongs_to_the_fleet_type_field(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        errors = self.errors(self.leader, fleet_type=str(self.mining.pk))

        self.assertEqual(
            errors["fleet_type"], [texts.GROUP_FLEET_TYPE_NOT_ALLOWED(type="Group Mining", group="Example Fleet Group")]
        )
        self.assertEqual(set(errors), {"fleet_type"})

    def test_a_channel_of_another_group_belongs_to_the_channel_field(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        errors = self.errors(self.leader, ping_channel=str(self.other_channel.pk))

        self.assertEqual(
            errors["ping_channel"], [texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Mining Pings", group="Example Fleet Group")]
        )
        self.assertEqual(set(errors), {"ping_channel"})

    def test_a_pre_ping_of_a_group_fleet_belongs_to_the_timing_field(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)

        errors = self.errors(
            self.fc, fleet_timing="pre_ping", fleet_date="2099-01-01", fleet_clock="19:00", fleet_type=str(self.activity.pk)
        )

        self.assertEqual(errors["fleet_timing"], [texts.GROUP_NO_PRE_PING()])

    def test_a_group_the_user_does_not_lead_or_that_only_pings_is_not_a_choice(self):
        for label, group in (("another group", self.other_group), ("ping-only group", self.ping_group)):
            with self.subTest(case=label):
                errors = self.errors(self.leader, fleet_group=str(group.pk))

                self.assertIn("fleet_group", errors)
                self.assertIn("Select a valid choice", errors["fleet_group"][0])

    def test_an_alliance_fleet_on_a_group_channel_is_refused_for_an_fc_who_leads(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)

        errors = self.errors(self.fc, fleet_group="", fleet_type=str(self.stratop.pk))

        self.assertEqual(errors["ping_channel"], [texts.CHANNEL_NOT_ALLOWED(name="Group Pings")])

    def test_a_missing_field_does_not_pile_up_a_second_error(self):
        errors = self.errors(self.leader, fleet_type="")

        self.assertEqual(set(errors), {"fleet_type"})
        self.assertEqual(errors["fleet_type"][0], "This field is required.")

    def test_the_page_shows_a_refusal_at_the_field_and_starts_nothing(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        response = self.login(self.leader).post(
            reverse("fleetops:start_fleet"), self.data(self.leader_char, ping_channel=str(self.other_channel.pk))
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Mining Pings", group="Example Fleet Group"))
        self.assertTrue(response.context["form"].errors["ping_channel"])
        self.assertFalse(FleetOperation.objects.exists())
        self.assertEqual(self.esi.calls, [])
        self.discord_post.assert_not_called()

    def test_the_preview_reports_a_refusal_as_a_field_error(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        response = self.login(self.leader).post(
            reverse("fleetops:preview_fleet"), self.data(self.leader_char, fleet_type=str(self.mining.pk))
        )

        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("fleet_type", payload["errors"])


@tag("catalog")
class RefusalInChineseTests(GroupStartBase):
    def test_a_refusal_is_shown_in_the_language_of_the_page(self):
        self.other_group.authgroup.group_leaders.add(self.leader.pk)
        form = self.form(self.leader, fleet_group="")

        with translation.override("zh-hans"):
            self.assertFalse(form.is_valid())
            message = form.errors["fleet_group"][0]

        self.assertNotEqual(message, texts.GROUP_REQUIRED())
        self.assertRegex(message, r"[一-鿿]")


class PresetWithGroupTests(GroupStartBase):
    def test_a_preset_keeps_the_group_and_its_channel(self):
        self.login(self.leader)
        data = self.data(self.leader_char)
        preset = save_preset(fresh(self.leader), "Group night", data)

        self.assertEqual(preset.values["fields"]["fleet_group"], str(self.group.pk))
        self.assertEqual(preset.values["labels"]["fleet_group"], "Example Fleet Group")
        initial, warnings = load_preset(preset, StartFleetForm, fresh(self.leader))
        self.assertEqual((initial["fleet_group"], initial["ping_channel"], warnings), (str(self.group.pk), str(self.channel.pk), []))

    def test_a_preset_after_leadership_ended_says_what_is_no_longer_available(self):
        preset = save_preset(fresh(self.leader), "Group night", self.data(self.leader_char))
        self.group.authgroup.group_leaders.remove(self.leader.pk)
        # The user still leads another fleet group, so the form (and its preset loading) still works.
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        initial, warnings = load_preset(preset, StartFleetForm, fresh(self.leader))

        self.assertIn("Fleet for 'Example Fleet Group' is no longer available.", warnings)
        self.assertIn("Ping channel 'Group Pings' is no longer available.", warnings)
        self.assertEqual(initial["fleet_group"], "")

    def test_the_page_opens_with_the_preset_warnings(self):
        preset = save_preset(fresh(self.leader), "Group night", self.data(self.leader_char))
        self.group.authgroup.group_leaders.remove(self.leader.pk)
        self.other_group.authgroup.group_leaders.add(self.leader.pk)

        response = self.login(self.leader).get(reverse("fleetops:start_fleet"), {"preset": preset.pk})

        self.assertContains(response, "Fleet for &#x27;Example Fleet Group&#x27; is no longer available.")

    def test_the_summary_of_a_preset_names_the_group(self):
        save_preset(fresh(self.leader), "Group night", self.data(self.leader_char))
        self.assertTrue(FleetStartPreset.objects.filter(owner=self.leader, name="Group night").exists())

        response = self.login(self.leader).get(reverse("fleetops:preset_list"))

        self.assertContains(response, "Example Fleet Group · Group Activity")

    def test_a_pure_fc_preset_has_no_group(self):
        preset = save_preset(fresh(self.fc), "Alliance night", self.data(self.fc_char, fleet_type=str(self.stratop.pk), ping_channel=str(self.alliance.pk)))

        self.assertNotIn("fleet_group", preset.values["fields"])


class LivePreviewTests(GroupStartBase):
    def test_the_group_variable_is_rendered_in_the_preview(self):
        response = self.login(self.leader).post(reverse("fleetops:preview_fleet"), self.data(self.leader_char))

        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertIn("**Group:** Example Fleet Group", payload["ping"])
        self.assertIn("**PAP Type:** Group Activity", payload["ping"])

    def test_an_alliance_fleet_has_no_group_line(self):
        self.group.authgroup.group_leaders.add(self.fc.pk)

        response = self.login(self.fc).post(
            reverse("fleetops:preview_fleet"),
            self.data(self.fc_char, fleet_group="", fleet_type=str(self.stratop.pk), ping_channel=str(self.alliance.pk)),
        )

        self.assertNotIn("**Group:**", response.json()["ping"])


class StartThroughThePageTests(GroupStartBase):
    def test_a_leader_starts_a_group_fleet_from_the_page(self):
        self.esi.put_in_fleet(self.leader_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.leader_char)]

        response = self.login(self.leader).post(reverse("fleetops:start_fleet"), self.data(self.leader_char))

        self.assertEqual(response.status_code, 302)
        operation = FleetOperation.objects.get()
        self.assertEqual((operation.group, operation.group_name), (self.group, "Example Fleet Group"))
        self.assertEqual(operation.ping_channel, self.channel)
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertIn("**Group:** Example Fleet Group", self.discord_post.call_args.kwargs["json"]["content"])
        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual((entry.new_value["group_id"], entry.new_value["group"]), (self.group.pk, "Example Fleet Group"))

    def test_the_stored_preset_of_the_last_start_keeps_the_group(self):
        self.esi.put_in_fleet(self.leader_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.leader_char)]
        self.login(self.leader).post(reverse("fleetops:start_fleet"), self.data(self.leader_char))

        last = FleetStartPreset.objects.get(owner=self.leader, is_last_used=True)

        self.assertEqual(last.values["fields"]["fleet_group"], str(self.group.pk))

    def test_a_ping_only_leader_cannot_open_the_page(self):
        ping_only = f.create_user("pingonly", perms=f.MEMBER_PERMS, main_name="Pilot P")
        self.ping_group.authgroup.group_leaders.add(ping_only.pk)

        self.assertEqual(self.login(ping_only).get(reverse("fleetops:start_fleet")).status_code, 403)
