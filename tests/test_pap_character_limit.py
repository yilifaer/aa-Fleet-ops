# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The PAP characters per player of one fleet: the rule, the start pages, presets, the fleet page while the fleet runs
and after it closed, the audit entries and the server's refusals of posts that did not come from the pages."""

import uuid
from datetime import timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import StartFleetForm
from fleetops.models import AttendanceRecord, AuditLog, FleetOperation, FleetStartPreset
from fleetops.services import pap_limit
from fleetops.services.operations import end_fleet, start_fleet
from fleetops.services.pap import compute_pap, finalize_pap
from fleetops.services.presets import load_preset, save_last_used, save_preset
from fleetops.services.mentions import EVERYONE
from fleetops.services.scheduling import schedule_fleet, start_scheduled_fleet

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .test_group_fleet_start import GroupStartBase
from .test_group_manage import GroupFleetBase
from .test_operations_tracking import FLEET_ID
from .test_scheduling import SchedulingTestBase, in_two_hours
from .test_pap_updated_at import LONG_AGO, age_all

UTC = dt_timezone.utc
Status = FleetOperation.Status
LIMIT = "pap_character_limit"
JSON = {"HTTP_ACCEPT": "application/json"}
ABOVE = "PAP characters per player can be at most 2 here; only FC leads can allow more or remove the limit."


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def limit_url(operation):
    return reverse("fleetops:set_pap_limit", args=[operation.uuid])


def audit_entries(operation, action="pap.limit"):
    return list(AuditLog.objects.filter(action=action, object_id=str(operation.pk)).order_by("pk"))


class RuleTests(TestCase):
    """Everybody except FC leads may only tighten the limit."""

    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)

    def test_a_member_chooses_up_to_the_setting_and_no_more(self):
        f.settings(attendance_limit=2)

        self.assertEqual([pap_limit.refusal(self.fc, value) for value in (1, 2)], ["", ""])
        self.assertEqual(pap_limit.refusal(self.fc, 3), ABOVE)
        self.assertEqual(pap_limit.refusal(self.fc, None), ABOVE)
        self.assertEqual(pap_limit.ceiling(self.fc), 2)

    def test_an_fc_lead_chooses_anything_including_no_limit(self):
        f.settings(attendance_limit=2)

        self.assertEqual([pap_limit.refusal(self.lead, value) for value in (1, 2, 3, 50, None)], [""] * 5)
        self.assertIsNone(pap_limit.ceiling(self.lead))

    def test_without_a_setting_everybody_chooses_anything(self):
        f.settings(attendance_limit=None)

        self.assertEqual([pap_limit.refusal(self.fc, value) for value in (1, 7, 1000, None)], [""] * 4)
        self.assertIsNone(pap_limit.ceiling(self.fc))

    def test_a_value_outside_the_range_is_refused_for_leads_too(self):
        for user in (self.fc, self.lead):
            for value in (0, -1, 1001):
                with self.subTest(user=user.username, value=value):
                    problem = pap_limit.check(user, value)
                    self.assertEqual(problem.status, 400)
                    self.assertEqual(
                        str(problem),
                        "PAP characters per player must be a whole number from 1 to 1000, or empty for no limit.",
                    )

    def test_the_posted_text_is_empty_or_digits(self):
        self.assertEqual([pap_limit.parse(text) for text in ("", "  ", None, "3", " 12 ")], [None, None, None, 3, 12])
        for text in ("abc", "1.5", "-1", "1e3", "٣", "3 4"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                pap_limit.parse(text)

    def test_the_starter_keeps_the_scheduled_value_unless_it_is_not_theirs_to_choose(self):
        f.settings(attendance_limit=2)

        self.assertEqual(pap_limit.starting_value(self.fc, 1), 1)
        # The setting was lowered since, or the fleet was scheduled before the limit could be chosen.
        self.assertEqual(pap_limit.starting_value(self.fc, 3), 2)
        self.assertEqual(pap_limit.starting_value(self.fc, None), 2)
        self.assertIsNone(pap_limit.starting_value(self.lead, None))
        self.assertEqual(pap_limit.starting_value(self.lead, 5), 5)


class StartPageBase(GroupStartBase):
    def setUp(self):
        super().setUp()
        f.settings(attendance_limit=2)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.lead_char = f.main_of(self.lead)
        f.add_token(self.lead, self.lead_char)

    def fc_data(self, user, **extra):
        """The start form of an FC for an alliance fleet."""
        data = self.data(f.main_of(user), fleet_type=str(self.stratop.pk), ping_channel=str(self.alliance.pk))
        data.update(extra)
        return data

    def start(self, user, **extra):
        character = f.main_of(user)
        self.esi.put_in_fleet(character)
        self.esi.members[FLEET_ID] = [f.esi_member(character)]
        return self.login(user).post(reverse("fleetops:start_fleet"), self.fc_data(user, **extra))

    def field_errors(self, response):
        return [str(error) for error in response.context["form"].errors.get(LIMIT, [])]


class StartFormTests(StartPageBase):
    def test_the_page_offers_the_setting_as_the_default_and_an_fc_no_more(self):
        html = self.page(self.fc).content.decode()

        self.assertIn("PAP characters per player", html)
        field = html.split('id="pap-limit-field"')[1].split("</div>")[0]
        self.assertIn('value="2"', field)
        self.assertIn('max="2"', field)
        self.assertIn(" required", field)
        self.assertNotIn("placeholder", field)
        self.assertIn("up to 2 (the setting)", html)
        self.assertIn("Only FC leads can allow more.", html)

    def test_an_fc_lead_may_leave_it_empty(self):
        html = self.page(self.lead).content.decode()

        field = html.split('id="pap-limit-field"')[1].split("</div>")[0]
        self.assertIn('value="2"', field)
        self.assertIn('placeholder="No limit"', field)
        self.assertNotIn(" required", field)
        self.assertNotIn('max="2"', field)
        self.assertIn("Empty means no limit.", html)

    def test_without_a_setting_the_default_is_empty_and_an_fc_may_leave_it_so(self):
        f.settings(attendance_limit=None)

        field = self.page(self.fc).content.decode().split('id="pap-limit-field"')[1].split("</div>")[0]

        self.assertNotIn("value=", field)
        self.assertIn('placeholder="No limit"', field)
        self.assertNotIn(" required", field)

    def test_the_fleet_takes_the_setting_when_nothing_is_changed(self):
        response = self.start(self.fc, **{LIMIT: "2"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(FleetOperation.objects.get().pap_character_limit, 2)

    def test_an_fc_tightens_the_limit_for_this_fleet(self):
        self.start(self.fc, **{LIMIT: "1"})

        operation = FleetOperation.objects.get()
        self.assertEqual(operation.pap_character_limit, 1)
        # The setting itself is untouched.
        self.assertEqual(f.settings().attendance_limit, 2)
        self.assertEqual(AuditLog.objects.get(action="fleet.start").new_value[LIMIT], 1)

    def test_a_post_without_the_field_takes_the_setting(self):
        self.start(self.fc)

        self.assertEqual(FleetOperation.objects.get().pap_character_limit, 2)

    def test_an_fc_cannot_allow_more_or_remove_the_limit(self):
        for value in ("3", "", "1000"):
            with self.subTest(value=value):
                response = self.start(self.fc, **{LIMIT: value})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.field_errors(response), [ABOVE])
                self.assertFalse(FleetOperation.objects.exists())
                self.assertContains(response, ABOVE)
        self.assertEqual(self.discord_post.call_count, 0)

    def test_values_outside_the_range_or_no_numbers_are_refused_at_the_field(self):
        for user in (self.fc, self.lead):
            for value in ("0", "-1", "1001", "abc", "1.5", "99999999999999999999"):
                with self.subTest(user=user.username, value=value):
                    response = self.start(user, **{LIMIT: value})

                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(self.field_errors(response))
                    self.assertFalse(FleetOperation.objects.exists())

    def test_an_fc_lead_may_loosen_it_or_remove_it(self):
        for value, expected in (("5", 5), ("", None)):
            with self.subTest(value=value):
                FleetOperation.objects.all().delete()
                response = self.start(self.lead, **{LIMIT: value})

                self.assertEqual(response.status_code, 302)
                operation = FleetOperation.objects.get()
                self.assertEqual(operation.pap_character_limit, expected)
                self.assertEqual(AuditLog.objects.filter(action="fleet.start").latest("pk").new_value[LIMIT], expected)

    def test_without_a_setting_an_fc_may_choose_anything(self):
        f.settings(attendance_limit=None)
        for value, expected in (("7", 7), ("", None)):
            with self.subTest(value=value):
                FleetOperation.objects.all().delete()
                self.start(self.fc, **{LIMIT: value})

                self.assertEqual(FleetOperation.objects.get().pap_character_limit, expected)

    def test_a_group_leader_without_fc_rights_may_only_tighten_it_too(self):
        self.esi.put_in_fleet(self.leader_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.leader_char)]
        self.login(self.leader)

        page = self.client.get(reverse("fleetops:start_fleet")).content.decode()
        self.assertIn('max="2"', page.split('id="pap-limit-field"')[1].split("</div>")[0])
        refused = self.client.post(reverse("fleetops:start_fleet"), self.data(self.leader_char, **{LIMIT: "3"}))
        self.assertEqual(self.field_errors(refused), [ABOVE])
        self.assertFalse(FleetOperation.objects.exists())

        self.client.post(reverse("fleetops:start_fleet"), self.data(self.leader_char, **{LIMIT: "1"}))
        operation = FleetOperation.objects.get()
        self.assertEqual((operation.group, operation.pap_character_limit), (self.group, 1))

    def test_a_start_that_skips_the_form_is_refused_by_the_service_too(self):
        character = f.main_of(self.fc)
        self.esi.put_in_fleet(character)
        data = {
            "operation_mode": "full",
            "fc_character_id": str(character.character_id),
            "fleet_type": self.stratop,
            "formup": "Jita",
            "ping_channel": self.alliance,
            LIMIT: 9,
        }

        with self.assertRaisesMessage(PermissionError, ABOVE):
            start_fleet(user=fresh_user(self.fc), cleaned_data=data, request_id=uuid.uuid4())

        self.assertFalse(FleetOperation.objects.exists())
        self.assertEqual(self.esi.calls, [])

    def test_the_preview_reports_a_refused_value_at_its_field(self):
        self.login(self.fc)

        payload = self.client.post(reverse("fleetops:preview_fleet"), self.fc_data(self.fc, **{LIMIT: "9"})).json()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][LIMIT][0]["message"], ABOVE)

    def test_the_refusal_is_shown_in_the_language_of_the_page(self):
        f.settings(attendance_limit=2)
        form = StartFleetForm(self.fc_data(self.fc, **{LIMIT: "9"}), user=fresh_user(self.fc))
        self.assertFalse(form.is_valid())
        self.assertEqual([str(error) for error in form.errors[LIMIT]], [ABOVE])


def fresh_user(user):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(pk=user.pk)


class PapLinkFleetStartTests(StartPageBase):
    def data_for(self, user, **extra):
        character = f.main_of(user)
        data = {
            "request_id": str(uuid.uuid4()),
            "fc_character_id": str(character.character_id),
            "fleet_type": str(self.stratop.pk),
            "formup": "Jita",
            "link_minutes": "30",
            "require_online": "on",
        }
        data.update(extra)
        return data

    def post(self, user, **extra):
        return self.login(user).post(reverse("fleetops:start_link_only_fleet"), self.data_for(user, **extra))

    def test_the_page_has_the_field_with_the_setting_as_default(self):
        html = self.login(self.fc).get(reverse("fleetops:start_link_only_fleet")).content.decode()

        field = html.split('id="pap-limit-field"')[1].split("</div>")[0]
        self.assertIn("PAP characters per player", html)
        self.assertIn('value="2"', field)
        self.assertIn('max="2"', field)

    def test_the_fleet_keeps_the_limit_the_fc_chose(self):
        self.post(self.fc, **{LIMIT: "1"})

        operation = FleetOperation.objects.get(link_only=True)
        self.assertEqual(operation.pap_character_limit, 1)
        self.assertEqual(AuditLog.objects.get(action="fleet.start").new_value[LIMIT], 1)

    def test_an_fc_cannot_allow_more_but_an_fc_lead_can(self):
        refused = self.post(self.fc, **{LIMIT: "3"})

        self.assertEqual(refused.status_code, 200)
        self.assertEqual([str(error) for error in refused.context["form"].errors[LIMIT]], [ABOVE])
        self.assertFalse(FleetOperation.objects.exists())
        self.post(self.lead, **{LIMIT: ""})
        self.assertIsNone(FleetOperation.objects.get(link_only=True).pap_character_limit)


class SchedulingTests(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        f.settings(attendance_limit=2)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        f.add_token(self.lead, f.main_of(self.lead))

    def test_a_pre_ping_keeps_the_limit_it_was_scheduled_with_until_the_start(self):
        self.post_schedule(**{LIMIT: "1"})
        operation = FleetOperation.objects.get()
        self.assertEqual((operation.status, operation.pap_character_limit), (Status.SCHEDULED, 1))
        self.assertEqual(AuditLog.objects.get(action="fleet.schedule").new_value[LIMIT], 1)

        start_scheduled_fleet(operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=False)

        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.pap_character_limit), (Status.ACTIVE, 1))
        # The PAP minimum still follows the setting at the start.
        self.assertEqual(AuditLog.objects.get(action="fleet.start").new_value[LIMIT], 1)

    def test_a_lower_setting_at_the_start_wins_over_a_higher_scheduled_value_of_an_fc(self):
        self.post_schedule(**{LIMIT: "2"})
        operation = FleetOperation.objects.get()
        f.settings(attendance_limit=1)

        start_scheduled_fleet(operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=False)

        operation.refresh_from_db()
        self.assertEqual(operation.pap_character_limit, 1)

    def test_a_fleet_scheduled_before_the_limit_could_be_chosen_takes_the_setting(self):
        operation = self.scheduled()
        self.assertIsNone(operation.pap_character_limit)

        start_scheduled_fleet(operation, user=self.fc, character_id=self.fc_char.character_id, send_ping=False)

        operation.refresh_from_db()
        self.assertEqual(operation.pap_character_limit, 2)

    def test_an_fc_lead_keeps_a_limit_above_the_setting_or_none(self):
        self.esi.put_in_fleet(f.main_of(self.lead))
        self.esi.members[FLEET_ID] = [f.esi_member(f.main_of(self.lead))]
        self.client.force_login(self.lead)
        data = self.form_data(**{LIMIT: "", "fc_character_id": str(f.main_of(self.lead).character_id)})
        self.client.post(reverse("fleetops:start_fleet"), data)
        operation = FleetOperation.objects.get()
        self.assertIsNone(operation.pap_character_limit)

        start_scheduled_fleet(operation, user=self.lead, character_id=f.main_of(self.lead).character_id, send_ping=False)

        operation.refresh_from_db()
        self.assertEqual((operation.status, operation.pap_character_limit), (Status.ACTIVE, None))

    def test_an_fc_cannot_schedule_more_than_the_setting(self):
        response = self.post_schedule(**{LIMIT: "3"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FleetOperation.objects.exists())
        with self.assertRaises(PermissionError):
            schedule_fleet(
                user=fresh_user(self.fc),
                cleaned_data={
                    "fc_character_id": str(self.fc_char.character_id),
                    "fleet_type": self.fleet_type,
                    "formup": "Jita",
                    "ping_channel": self.channel,
                    "scheduled_at": in_two_hours(),
                    LIMIT: 3,
                },
            )

    def test_the_edit_page_shows_the_scheduled_value_and_saves_a_tighter_one(self):
        self.post_schedule(**{LIMIT: "2"})
        operation = FleetOperation.objects.get()
        edit_url = reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid])
        page = self.client.get(edit_url).content.decode()
        self.assertIn('value="2"', page.split('id="pap-limit-field"')[1].split("</div>")[0])
        when = operation.scheduled_at.astimezone(UTC)
        data = {
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "fleet_date": when.strftime("%Y-%m-%d"),
            "fleet_clock": when.strftime("%H:%M"),
            "ping_channel": str(self.channel.pk),
            "ping_mentions": [EVERYONE],
        }

        refused = self.client.post(edit_url, {**data, LIMIT: "3"})
        self.assertEqual(refused.status_code, 200)
        saved = self.client.post(edit_url, {**data, LIMIT: "1"})

        self.assertEqual(saved.status_code, 302)
        operation.refresh_from_db()
        self.assertEqual(operation.pap_character_limit, 1)
        entry = AuditLog.objects.get(action="fleet.schedule_edit")
        self.assertEqual((entry.old_value[LIMIT], entry.new_value[LIMIT]), (2, 1))

    def test_an_edit_that_does_not_carry_the_field_keeps_the_scheduled_value(self):
        self.post_schedule(**{LIMIT: "1"})
        operation = FleetOperation.objects.get()
        when = operation.scheduled_at.astimezone(UTC)

        self.client.post(
            reverse("fleetops:edit_scheduled_fleet", args=[operation.uuid]),
            {
                "fleet_type": str(self.fleet_type.pk),
                "formup": "Perimeter",
                "fleet_date": when.strftime("%Y-%m-%d"),
                "fleet_clock": when.strftime("%H:%M"),
                "ping_channel": str(self.channel.pk),
                "ping_mentions": [EVERYONE],
            },
        )

        operation.refresh_from_db()
        self.assertEqual((operation.formup, operation.pap_character_limit), ("Perimeter", 1))

    def test_the_scheduled_fleet_page_lists_the_limit(self):
        self.post_schedule(**{LIMIT: "1"})
        operation = FleetOperation.objects.get()

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "<th>PAP characters per player</th><td>1</td>", html=False)


class PresetTests(StartPageBase):
    def saved(self, user, **extra):
        data = self.fc_data(user, **extra)
        return save_preset(fresh_user(user), "Evening", data, overwrite=True)

    def test_a_preset_keeps_the_limit_and_loads_it_again(self):
        preset = self.saved(self.fc, **{LIMIT: "1"})

        self.assertEqual(preset.values["fields"][LIMIT], "1")
        initial, warnings = load_preset(preset, StartFleetForm, fresh_user(self.fc))
        self.assertEqual(initial[LIMIT], "1")
        self.assertEqual(warnings, [])
        page = self.login(self.fc).get(reverse("fleetops:start_fleet") + f"?preset={preset.pk}")
        self.assertEqual(page.context["form"][LIMIT].value(), "1")

    def test_an_fc_lead_keeps_no_limit_in_a_preset(self):
        preset = self.saved(self.lead, **{LIMIT: ""})

        self.assertEqual(preset.values["fields"][LIMIT], "")
        initial, warnings = load_preset(preset, StartFleetForm, fresh_user(self.lead))
        self.assertEqual((initial[LIMIT], warnings), ("", []))

    def test_a_preset_does_not_keep_a_value_the_fc_may_not_choose(self):
        for value in ("9", "", "0"):
            with self.subTest(value=value):
                preset = self.saved(self.fc, **{LIMIT: value})

                self.assertNotIn(LIMIT, preset.values["fields"])
                self.assertFalse(preset.values["labels"].get(LIMIT))

    def test_a_preset_made_without_the_field_leaves_the_limit_to_the_setting(self):
        data = self.fc_data(self.fc)
        data.pop(LIMIT, None)

        preset = save_preset(fresh_user(self.fc), "Plain", data)

        self.assertNotIn(LIMIT, preset.values["fields"])
        initial, warnings = load_preset(preset, StartFleetForm, fresh_user(self.fc))
        self.assertNotIn(LIMIT, initial)
        self.assertEqual(warnings, [])
        self.assertEqual(StartFleetForm(user=fresh_user(self.fc))[LIMIT].value(), 2)

    def test_a_saved_value_that_is_too_high_after_the_setting_was_lowered_falls_back_with_a_warning(self):
        f.settings(attendance_limit=3)
        preset = self.saved(self.fc, **{LIMIT: "3"})
        f.settings(attendance_limit=1)

        initial, warnings = load_preset(preset, StartFleetForm, fresh_user(self.fc))

        self.assertNotIn(LIMIT, initial)
        self.assertEqual(
            warnings,
            ["PAP characters per player (3) is more than you may choose now; the default 1 is used."],
        )
        page = self.login(self.fc).get(reverse("fleetops:start_fleet") + f"?preset={preset.pk}")
        self.assertEqual(page.context["form"][LIMIT].value(), 1)
        self.assertContains(page, "is more than you may choose now")

    def test_a_preset_with_no_limit_is_replaced_for_an_fc_who_may_not_remove_it(self):
        f.settings(attendance_limit=None)
        preset = self.saved(self.fc, **{LIMIT: ""})
        f.settings(attendance_limit=2)

        initial, warnings = load_preset(preset, StartFleetForm, fresh_user(self.fc))

        self.assertNotIn(LIMIT, initial)
        self.assertEqual(
            warnings,
            ["PAP characters per player (no limit) is more than you may choose now; the default 2 is used."],
        )

    def test_the_last_used_settings_keep_the_limit(self):
        self.start(self.fc, **{LIMIT: "1"})

        last = FleetStartPreset.objects.get(owner=self.fc, is_last_used=True)

        self.assertEqual(last.values["fields"][LIMIT], "1")
        initial, _warnings = load_preset(last, StartFleetForm, fresh_user(self.fc))
        self.assertEqual(initial[LIMIT], "1")

    def test_the_preset_pages_save_what_the_form_posts(self):
        self.login(self.fc)

        response = self.client.post(
            reverse("fleetops:preset_save"),
            {**self.fc_data(self.fc, **{LIMIT: "1"}), "preset-name": "Posted"},
            **JSON,
        )

        self.assertTrue(response.json()["ok"])
        self.assertEqual(FleetStartPreset.objects.get(name="Posted").values["fields"][LIMIT], "1")

    def test_the_last_used_values_of_a_form_are_the_cleaned_ones(self):
        form = StartFleetForm(self.fc_data(self.fc, **{LIMIT: "2"}), user=fresh_user(self.fc))
        self.assertTrue(form.is_valid(), form.errors)

        preset = save_last_used(fresh_user(self.fc), form.cleaned_data)

        self.assertEqual(preset.values["fields"][LIMIT], "2")


class RunningFleetBase(GroupFleetBase):
    """A running fleet with a pilot who flies a main and an alt in it."""

    def setUp(self):
        super().setUp()
        f.settings(attendance_limit=2)
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS, main_name="Pilot Main")
        self.main = f.main_of(self.pilot)
        self.alt = f.add_alt(self.pilot, "Pilot Alt")
        self.operation = f.create_operation(
            self.fc, started_at=timezone.now() - timedelta(minutes=90), pap_minimum_minutes=0, pap_character_limit=2
        )
        f.member_state(self.operation, self.fc_char, role="fleet_commander", accumulated_seconds=5000)
        f.member_state(self.operation, self.main, accumulated_seconds=4000)
        f.member_state(self.operation, self.alt, accumulated_seconds=3000)

    def change(self, user, operation, value=None, *, reason=None, **headers):
        data = {} if value is None else {LIMIT: str(value)}
        if reason is not None:
            data["reason"] = reason
        return self.client_for(user).post(limit_url(operation), data, **headers)

    def stored(self, operation=None):
        return FleetOperation.objects.get(pk=(operation or self.operation).pk).pap_character_limit


class RunningFleetTests(RunningFleetBase):
    def test_an_fc_tightens_the_limit_and_comes_back_to_the_pap_tab(self):
        response = self.change(self.fc, self.operation, 1)

        self.assertRedirects(
            response,
            reverse("fleetops:operation_detail", args=[self.operation.uuid]) + "#attendance",
            fetch_redirect_response=False,
        )
        self.assertEqual(self.stored(), 1)
        self.assertEqual(
            flashed(response), ["PAP characters per player set to 1. PAP is granted with it when the fleet closes."]
        )

    def test_the_change_is_audited_with_the_old_and_the_new_value(self):
        self.change(self.fc, self.operation, 1)

        (entry,) = audit_entries(self.operation)
        self.assertEqual((entry.actor, entry.old_value, entry.new_value), (self.fc, {LIMIT: 2}, {LIMIT: 1}))
        self.assertEqual(entry.reason, "")

    def test_a_reason_posted_with_a_running_change_is_kept_in_the_audit_entry(self):
        self.change(self.fc, self.operation, 1, reason="Small fleet tonight")

        self.assertEqual(audit_entries(self.operation)[0].reason, "Small fleet tonight")

    def test_an_fc_cannot_loosen_it_or_remove_it(self):
        for value in (3, "", 50):
            with self.subTest(value=value):
                response = self.change(self.fc, self.operation, value if value != "" else None)

                self.assertEqual(self.stored(), 2)
                # The messages of the earlier posts have not been shown yet.
                self.assertEqual(flashed(response)[-1], ABOVE)
        self.assertEqual(audit_entries(self.operation), [])

    def test_the_json_answer_carries_the_status_and_the_value(self):
        refused = self.change(self.fc, self.operation, 3, **JSON)
        accepted = self.change(self.fc, self.operation, 1, **JSON)

        self.assertEqual((refused.status_code, refused.json()["ok"], refused.json()["message"]), (403, False, ABOVE))
        self.assertEqual((accepted.status_code, accepted.json()["ok"]), (200, True))
        self.assertEqual(accepted.json()["data"], {LIMIT: 1})
        self.assertEqual(accepted.json()["level"], "success")

    def test_an_fc_lead_may_loosen_it_and_remove_it(self):
        lead_fleet = f.create_operation(self.lead, pap_minimum_minutes=0, pap_character_limit=2)

        self.change(self.lead, self.operation, 5)
        self.assertEqual(self.stored(), 5)
        self.change(self.lead, self.operation, None)
        self.assertIsNone(self.stored())
        self.change(self.lead, lead_fleet, 1)
        self.assertEqual(self.stored(lead_fleet), 1)

        entries = audit_entries(self.operation)
        self.assertEqual([(e.old_value[LIMIT], e.new_value[LIMIT]) for e in entries], [(2, 5), (5, None)])

    def test_without_a_setting_an_fc_may_choose_anything(self):
        f.settings(attendance_limit=None)

        self.change(self.fc, self.operation, 7)
        self.assertEqual(self.stored(), 7)
        self.change(self.fc, self.operation, None)
        self.assertIsNone(self.stored())

    def test_a_group_leader_tightens_the_limit_of_their_group_fleet_and_cannot_go_above_the_setting(self):
        fleet = self.group_fleet(pap_minimum_minutes=0, pap_character_limit=2)

        tightened = self.change(self.leader, fleet, 1)
        self.assertEqual((tightened.status_code, self.stored(fleet)), (302, 1))
        # Back up to the setting is still within what they may choose.
        self.assertEqual(self.change(self.leader, fleet, 2, **JSON).status_code, 200)
        self.assertEqual(self.stored(fleet), 2)
        for value in (3, None):
            with self.subTest(value=value):
                self.assertEqual(self.change(self.leader, fleet, value, **JSON).status_code, 403)
                self.assertEqual(self.stored(fleet), 2)

    def test_other_leaders_and_non_managers_cannot_change_it(self):
        fleet = self.group_fleet(pap_minimum_minutes=0, pap_character_limit=2)

        # Another leader of the same group did not start the fleet; a leader of another group; an FC of another fleet.
        for user in (self.second, self.other_leader, self.fc):
            with self.subTest(user=user.username):
                self.assertEqual(self.change(user, fleet, 1, **JSON).status_code, 404)
        self.assertEqual(self.change(self.member, self.operation, 1, **JSON).status_code, 403)
        self.assertEqual(self.change(self.lead, fleet, 1, **JSON).status_code, 200)
        self.assertEqual(self.stored(fleet), 1)
        self.assertEqual(self.stored(), 2)

    def test_the_post_must_be_a_post_and_a_login(self):
        self.client_for(self.fc)
        self.assertEqual(self.client.get(limit_url(self.operation)).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(limit_url(self.operation), {LIMIT: "1"}).status_code, 302)
        self.assertEqual(self.stored(), 2)

    def test_crafted_values_are_refused_and_change_nothing(self):
        for value in ("abc", "0", "-1", "1.5", "1001", "99999999999999999999", "1 2"):
            for user in (self.fc, self.lead):
                with self.subTest(user=user.username, value=value):
                    response = self.client_for(user).post(limit_url(self.operation), {LIMIT: value}, **JSON)

                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json()["ok"])
                    self.assertEqual(self.stored(), 2)
        self.assertEqual(audit_entries(self.operation), [])

    def test_the_same_value_changes_nothing_and_writes_no_audit_entry(self):
        before = FleetOperation.objects.get(pk=self.operation.pk).updated_at

        response = self.change(self.fc, self.operation, 2)

        self.assertEqual(flashed(response), ["The PAP characters per player is already 2."])
        self.assertEqual(audit_entries(self.operation), [])
        self.assertEqual(FleetOperation.objects.get(pk=self.operation.pk).updated_at, before)

    def test_a_fleet_that_is_ending_or_not_started_cannot_be_changed(self):
        for status in (Status.ENDING, Status.STARTING, Status.CANCELLED):
            with self.subTest(status=status):
                FleetOperation.objects.filter(pk=self.operation.pk).update(status=status)

                response = self.change(self.lead, self.operation, 1, **JSON)

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["message"],
                    "The PAP characters per player can be changed while the fleet runs, "
                    "and by an FC lead after it has closed.",
                )
                self.assertEqual(self.stored(), 2)

    def test_the_live_pap_preview_follows_the_new_limit(self):
        self.client_for(self.lead)
        first = self.client.get(reverse("fleetops:fleet_state", args=[self.operation.uuid])).json()
        self.assertEqual(
            {c.character_id: c.outcome for c in compute_pap(self.operation).candidates if c.identity.user == self.pilot},
            {self.main.character_id: "granted", self.alt.character_id: "granted"},
        )
        self.assertIn("2 characters per player", first["pap_html"])

        self.change(self.fc, self.operation, 1)

        second = self.client.get(
            reverse("fleetops:fleet_state", args=[self.operation.uuid]), {"v": first["version"]}
        ).json()
        self.assertFalse(second["unchanged"])
        self.assertNotEqual(second["version"], first["version"])
        self.assertIn("1 character per player", second["pap_html"])
        self.assertIn("Over the character limit", second["pap_html"])
        outcomes = {
            c.character_id: c.outcome
            for c in compute_pap(FleetOperation.objects.get(pk=self.operation.pk)).candidates
            if c.identity.user == self.pilot
        }
        self.assertEqual(outcomes, {self.main.character_id: "granted", self.alt.character_id: "capped"})

    def test_the_member_sees_the_new_limit_in_their_live_status(self):
        self.change(self.fc, self.operation, 1)

        rows = self.client_for(self.pilot).get(reverse("fleetops:live_pap")).content.decode()

        self.assertIn("Over the limit of 1 character per player", rows)

    def test_pap_at_the_close_uses_the_latest_value(self):
        self.change(self.fc, self.operation, 1)
        self.change(self.fc, self.operation, 2)
        self.change(self.fc, self.operation, 1)

        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(FleetOperation.objects.get(pk=self.operation.pk), actor=self.fc)

        rows = {row.character_id: row for row in AttendanceRecord.objects.filter(operation=self.operation)}
        self.assertEqual((rows[self.main.character_id].granted, rows[self.main.character_id].capped), (True, False))
        self.assertEqual((rows[self.alt.character_id].granted, rows[self.alt.character_id].capped), (False, True))
        self.assertEqual(len(audit_entries(self.operation)), 3)

    def test_a_loosened_limit_grants_both_characters_at_the_close(self):
        self.change(self.lead, self.operation, None)

        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(FleetOperation.objects.get(pk=self.operation.pk), actor=self.fc)

        self.assertTrue(AttendanceRecord.objects.get(operation=self.operation, character_id=self.alt.character_id).granted)


class ControlTests(RunningFleetBase):
    def card(self, user, operation=None):
        html = self.client_for(user).get(
            reverse("fleetops:operation_detail", args=[(operation or self.operation).uuid])
        ).content.decode()
        return html.split('id="pap-limit-card"')[1].split("</form>")[0] if 'id="pap-limit-card"' in html else None

    def test_the_fc_of_a_running_fleet_sees_the_control_with_the_most_they_may_choose(self):
        card = self.card(self.fc)

        self.assertIn(f'action="{limit_url(self.operation)}"', card)
        self.assertIn('name="pap_character_limit"', card)
        self.assertIn('value="2"', card)
        self.assertIn('max="2"', card)
        self.assertIn(" required", card)
        self.assertIn("Apply", card)
        self.assertNotIn('name="reason"', card)
        self.assertNotIn("recalculate", card.lower())

    def test_an_fc_lead_may_leave_it_empty(self):
        card = self.card(self.lead)

        self.assertIn('placeholder="No limit"', card)
        self.assertNotIn('max="2"', card)
        self.assertNotIn('name="reason"', card)

    def test_the_card_sits_above_the_pap_preview(self):
        html = self.client_for(self.fc).get(reverse("fleetops:operation_detail", args=[self.operation.uuid])).content.decode()

        self.assertLess(html.index('id="pap-limit-card"'), html.index('id="pap-preview-fragment"'))
        # The poller replaces the preview but not the control.
        self.assertNotIn("pap-limit", html.split('id="pap-preview-fragment"')[1].split('id="roles"')[0])

    def test_a_group_leader_sees_it_on_their_group_fleet(self):
        fleet = self.group_fleet(pap_minimum_minutes=0, pap_character_limit=2)

        self.assertIn('max="2"', self.card(self.leader, fleet))
        self.assertIsNone(self.card(self.second, fleet))

    def test_nobody_else_sees_it(self):
        view_all = f.create_user("viewer", perms=f.MEMBER_PERMS + ["fleetops.view_all_fleets"])
        for user in (self.pilot, view_all):
            with self.subTest(user=user.username):
                self.assertIsNone(self.card(user))

    def test_a_closed_fleet_shows_it_to_fc_leads_only_and_asks_for_a_reason(self):
        closed = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=3),
            pap_minimum_minutes=0,
            pap_character_limit=2,
        )

        self.assertIsNone(self.card(self.fc, closed))
        card = self.card(self.lead, closed)
        self.assertIn('name="reason"', card)
        self.assertIn("Apply and recalculate PAP", card)
        self.assertIn('minlength="5"', card)

    def test_a_fleet_that_closed_before_pap_followed_time_in_fleet_has_no_control(self):
        closed = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=timezone.now() - timedelta(hours=3),
            pap_rules=FleetOperation.PapRules.LEGACY,
            pap_minimum_minutes=0,
            pap_character_limit=2,
        )

        self.assertIsNone(self.card(self.lead, closed))

    def test_the_control_does_not_build_its_form_for_the_pap_fragment(self):
        from fleetops.forms import PapLimitChangeForm

        with mock.patch.object(PapLimitChangeForm, "__init__", side_effect=AssertionError("built")) as built:
            self.client_for(self.lead).get(reverse("fleetops:fleet_state", args=[self.operation.uuid]))

        built.assert_not_called()


class ClosedFleetTests(RunningFleetBase):
    def setUp(self):
        super().setUp()
        started = timezone.now() - timedelta(hours=4)
        self.closed = f.create_operation(
            self.fc,
            status=Status.CLOSED,
            started_at=started,
            ended_at=started + timedelta(hours=1),
            pap_minimum_minutes=0,
            pap_character_limit=1,
        )
        self.other_pilot = f.create_user("other", perms=f.MEMBER_PERMS, main_name="Other Main")
        for character, seconds in (
            (self.fc_char, 3600),
            (self.main, 3000),
            (self.alt, 1800),
            (f.main_of(self.other_pilot), 2400),
        ):
            f.member_state(self.closed, character, accumulated_seconds=seconds, active=False)
        finalize_pap(self.closed)
        age_all(self.closed)

    def row(self, character):
        return AttendanceRecord.objects.get(operation=self.closed, character_id=character.character_id)

    def test_an_fc_lead_changes_the_limit_with_a_reason_and_the_pap_is_calculated_again(self):
        self.assertFalse(self.row(self.alt).granted)

        response = self.change(self.lead, self.closed, 2, reason="Roster was wrong")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(self.closed), 2)
        self.assertTrue(self.row(self.alt).granted)
        self.assertFalse(self.row(self.alt).capped)
        self.assertEqual(
            flashed(response), ["PAP characters per player set to 2; PAP recalculated (4 granted)."]
        )

    def test_only_the_rows_that_change_get_a_new_updated_at(self):
        self.change(self.lead, self.closed, 2, reason="Roster was wrong")

        self.assertGreater(self.row(self.alt).updated_at, LONG_AGO)
        for character in (self.fc_char, self.main, f.main_of(self.other_pilot)):
            self.assertEqual(self.row(character).updated_at, LONG_AGO)

    def test_the_audit_entry_has_both_values_the_reason_and_the_new_pap(self):
        self.change(self.lead, self.closed, None, reason="Open event for everybody")

        (entry,) = audit_entries(self.closed)
        self.assertEqual((entry.actor, entry.old_value, entry.reason), (self.lead, {LIMIT: 1}, "Open event for everybody"))
        self.assertEqual(entry.new_value[LIMIT], None)
        self.assertEqual(entry.new_value["pap"]["granted"], 4)

    def test_tightening_takes_pap_away_again(self):
        self.change(self.lead, self.closed, 2, reason="Roster was wrong")
        self.change(self.lead, self.closed, 1, reason="It was right after all")

        self.assertFalse(self.row(self.alt).granted)
        self.assertTrue(self.row(self.alt).capped)
        self.assertEqual(len(audit_entries(self.closed)), 2)

    def test_a_reason_is_required_and_a_short_one_does_not_count(self):
        for reason in (None, "", "   ", "abcd"):
            with self.subTest(reason=reason):
                response = self.change(self.lead, self.closed, 2, reason=reason, **JSON)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["message"],
                    "Give a reason of at least 5 characters to change the PAP characters per player of a closed fleet.",
                )
                self.assertEqual(self.stored(self.closed), 1)
        self.assertFalse(self.row(self.alt).granted)
        self.assertEqual(audit_entries(self.closed), [])

    def test_the_reason_is_as_long_as_the_one_of_a_multiplier_override(self):
        from fleetops.forms import FleetAttendanceMultiplierForm

        self.assertEqual(FleetAttendanceMultiplierForm.REASON_MIN_LENGTH, 5)
        self.assertEqual(self.change(self.lead, self.closed, 2, reason="12345", **JSON).status_code, 200)

    def test_an_fc_cannot_change_it_after_the_close_whatever_they_post(self):
        for value in (0, 1, 2, 5, None):
            with self.subTest(value=value):
                response = self.change(self.fc, self.closed, value, reason="Roster was wrong", **JSON)

                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json()["message"], "Only FC leads can change the PAP characters per player of a closed fleet."
                )
        self.assertEqual(self.stored(self.closed), 1)
        self.assertEqual(audit_entries(self.closed), [])

    def test_a_group_leader_cannot_change_a_closed_group_fleet(self):
        fleet = self.group_fleet(
            status=Status.CLOSED, started_at=timezone.now() - timedelta(hours=3), pap_minimum_minutes=0, pap_character_limit=2
        )

        response = self.change(self.leader, fleet, 1, reason="Wrong roster", **JSON)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stored(fleet), 2)

    def test_the_same_value_writes_nothing(self):
        response = self.change(self.lead, self.closed, 1, reason="No change at all", **JSON)

        self.assertEqual((response.status_code, response.json()["level"]), (200, "info"))
        self.assertEqual(audit_entries(self.closed), [])
        self.assertTrue(all(row.updated_at == LONG_AGO for row in AttendanceRecord.objects.filter(operation=self.closed)))

    def test_a_fleet_that_closed_before_pap_followed_time_in_fleet_keeps_its_selection(self):
        FleetOperation.objects.filter(pk=self.closed.pk).update(pap_rules=FleetOperation.PapRules.LEGACY)

        response = self.change(self.lead, self.closed, 2, reason="Roster was wrong", **JSON)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["message"],
            "This fleet closed before PAP followed time in fleet, so its PAP does not use this limit.",
        )
        self.assertEqual(self.stored(self.closed), 1)

    def test_the_fc_points_of_the_month_are_checked_again(self):
        with mock.patch("fleetops.services.incentives.refresh_months_after_commit") as refresh:
            self.change(self.lead, self.closed, 2, reason="Roster was wrong")

        refresh.assert_called_once()
        year, month = self.closed.started_at.year, self.closed.started_at.month
        self.assertEqual(refresh.call_args.args, ((year, month),))
        self.assertEqual(refresh.call_args.kwargs, {"actor": self.lead})

    def test_a_running_change_does_not_touch_the_incentives(self):
        with mock.patch("fleetops.services.incentives.refresh_months_after_commit") as refresh:
            self.change(self.fc, self.operation, 1)

        refresh.assert_not_called()

    def test_recalculating_by_hand_gives_the_same_result(self):
        self.change(self.lead, self.closed, 2, reason="Roster was wrong")
        before = {row.pk: row.updated_at for row in AttendanceRecord.objects.filter(operation=self.closed)}

        finalize_pap(self.closed)

        self.assertEqual({row.pk: row.updated_at for row in AttendanceRecord.objects.filter(operation=self.closed)}, before)
