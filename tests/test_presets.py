# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet Start presets: saving, loading with warnings, the last used settings and the preset pages."""

import uuid
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from fleetops.forms import StartFleetForm
from fleetops.models import AuditLog, ChannelPreset, CommsPreset, FleetOperation, FleetStartPreset, MessageTemplate
from fleetops.providers.esi import FleetDetectionResult
from fleetops.providers.srp import SRPLinkResult
from fleetops.services import operations as operations_module
from fleetops.services.mentions import HERE, NONE, role_choice
from fleetops.services.presets import PresetConflict, load_preset, save_last_used, save_preset

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module

FLEET_ID = 1_047_100_000_001
JSON = {"HTTP_ACCEPT": "application/json"}
# What a browser sends when the form is posted without script.
PAGE = {"HTTP_ACCEPT": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class PresetTestBase(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Preset FC")
        self.fc_char = f.main_of(self.fc)
        self.alt = f.add_alt(self.fc, "Preset Alt")
        self.fleet_type = f.fleet_type("Strategic Op", "1.00")
        self.fleet_type.short_name = "StratOp"
        self.fleet_type.save()
        self.comms = CommsPreset.objects.create(name="Main Mumble", channel_name="Ops")
        self.logi = ChannelPreset.objects.create(
            name="Logi", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="Logi Chat"
        )
        self.boost = ChannelPreset.objects.create(
            name="Boosts", channel_type=ChannelPreset.ChannelType.BOOST, channel_value="Boost Chat"
        )
        self.sig_a = f.ping_role("Sig A")
        self.channel = f.ping_channel("STR", allow_here=True, roles=[self.sig_a])
        self.ping_template = MessageTemplate.objects.create(
            name="Short Ping", template_type=MessageTemplate.TemplateType.PING, content="Ping {{ formup }}"
        )
        self.motd_template = MessageTemplate.objects.create(
            name="Short MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="MOTD {{ formup }}"
        )

    def data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(self.alt.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "custom_doctrine": "Ferox Fleet",
            "formup": "Jita 4-4",
            "comms": str(self.comms.pk),
            "logi_channel": str(self.logi.pk),
            "boost_channel": str(self.boost.pk),
            "ping_channel": str(self.channel.pk),
            "ping_mentions": [role_choice(self.sig_a)],
            "ping_template": str(self.ping_template.pk),
            "motd_template": str(self.motd_template.pk),
            "additional_message": "Bring cap boosters.",
        }
        data.update(extra)
        return data

    def preset(self, name="Home Defense", owner=None, **extra):
        return save_preset(owner or self.fc, name, self.data(), **extra)

    def load(self, preset, user=None):
        return load_preset(preset, StartFleetForm, user or self.fc)


class SavePresetTests(PresetTestBase):
    def test_saves_the_start_fields_without_fleet_time_request_and_fc(self):
        preset = save_preset(
            self.fc, "Home Defense", self.data(fleet_timing="pre_ping", fleet_date="2026-11-01", fleet_clock="19:00")
        )

        fields = preset.values["fields"]
        self.assertEqual(
            fields,
            {
                "operation_mode": "full",
                "fleet_type": str(self.fleet_type.pk),
                "doctrine_choice": "",
                "custom_doctrine": "Ferox Fleet",
                "formup": "Jita 4-4",
                "comms": str(self.comms.pk),
                "logi_channel": str(self.logi.pk),
                "boost_channel": str(self.boost.pk),
                "ping_channel": str(self.channel.pk),
                "ping_mentions": [role_choice(self.sig_a)],
                "ping_template": str(self.ping_template.pk),
                "motd_template": str(self.motd_template.pk),
                "additional_message": "Bring cap boosters.",
            },
        )
        for excluded in ("request_id", "fleet_timing", "fleet_date", "fleet_clock", "fc_character_id"):
            self.assertNotIn(excluded, fields)
        self.assertIsNone(preset.fc_character_id)
        self.assertEqual(preset.values["labels"]["ping_channel"], "STR")
        self.assertEqual(preset.values["labels"]["ping_mentions"], ["@Sig A"])
        self.assertEqual(preset.values["labels"]["comms"], "Main Mumble")
        self.assertFalse(preset.is_last_used)
        self.assertFalse(preset.is_default)

    def test_partial_form_keeps_the_valid_values(self):
        data = {"fleet_type": "999999", "formup": "", "comms": str(self.comms.pk), "ping_mentions": [HERE, "bogus"]}

        preset = save_preset(self.fc, "Partial", data)

        fields = preset.values["fields"]
        self.assertEqual(fields["comms"], str(self.comms.pk))
        # An unknown fleet type, an empty required form-up and an invalid mention are left out.
        self.assertNotIn("fleet_type", fields)
        self.assertNotIn("formup", fields)
        self.assertNotIn("ping_mentions", fields)
        self.assertEqual(fields["ping_channel"], "")

    def test_name_conflict_needs_overwrite(self):
        first = self.preset("Home Defense")

        with self.assertRaises(PresetConflict):
            save_preset(self.fc, "home defense", self.data(formup="Amarr"))

        second = save_preset(self.fc, "home defense", self.data(formup="Amarr"), overwrite=True)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.name, "home defense")
        self.assertEqual(second.values["fields"]["formup"], "Amarr")
        self.assertEqual(FleetStartPreset.objects.filter(owner=self.fc).count(), 1)

    def test_same_name_for_another_fc_is_no_conflict(self):
        other = f.create_user(perms=f.FC_PERMS)
        self.preset("Home Defense")

        save_preset(other, "Home Defense", {"formup": "Dodixie"})

        self.assertEqual(FleetStartPreset.objects.filter(name="Home Defense").count(), 2)

    def test_remember_fc_keeps_only_an_owned_character(self):
        remembered = self.preset("With FC", remember_fc=True)
        self.assertEqual(remembered.fc_character_id, self.alt.character_id)
        self.assertEqual(remembered.values["labels"]["fc_character_id"], "Preset Alt")

        stranger = f.create_character("Somebody Else")
        foreign = save_preset(self.fc, "Foreign", self.data(fc_character_id=str(stranger.character_id)), remember_fc=True)
        self.assertIsNone(foreign.fc_character_id)

    def test_overwrite_without_remember_fc_forgets_the_character(self):
        self.preset("With FC", remember_fc=True)

        preset = self.preset("With FC", overwrite=True)

        self.assertIsNone(preset.fc_character_id)

    def test_one_default_per_owner(self):
        first = self.preset("First", make_default=True)
        second = self.preset("Second", make_default=True)
        other = save_preset(f.create_user(perms=f.FC_PERMS), "Theirs", {}, make_default=True)

        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        other.refresh_from_db()
        self.assertTrue(other.is_default)

    def test_overwrite_keeps_the_default_flag(self):
        self.preset("First", make_default=True)

        preset = self.preset("First", overwrite=True)

        self.assertTrue(preset.is_default)

    def test_blank_name_is_refused(self):
        with self.assertRaises(ValueError):
            save_preset(self.fc, "  ", self.data())

    def test_presets_are_not_audited(self):
        self.preset("Quiet", make_default=True)

        self.assertFalse(AuditLog.objects.exists())


class LoadPresetTests(PresetTestBase):
    def test_round_trip(self):
        preset = self.preset(remember_fc=True)

        initial, warnings = self.load(preset)

        self.assertEqual(warnings, [])
        self.assertEqual(initial["fleet_type"], str(self.fleet_type.pk))
        self.assertEqual(initial["ping_channel"], str(self.channel.pk))
        self.assertEqual(initial["ping_mentions"], [role_choice(self.sig_a)])
        self.assertEqual(initial["custom_doctrine"], "Ferox Fleet")
        self.assertEqual(initial["fc_character_id"], str(self.alt.character_id))
        self.assertNotIn("request_id", initial)
        self.assertNotIn("fleet_date", initial)

        form = StartFleetForm({**initial, "request_id": str(uuid.uuid4()), "fleet_timing": "now"}, user=self.fc)
        self.assertTrue(form.is_valid(), form.errors)

    def test_every_missing_item_is_blank_with_a_warning(self):
        preset = self.preset(remember_fc=True)
        self.fleet_type.is_active = False
        self.fleet_type.save()
        self.comms.is_active = False
        self.comms.save()
        self.logi.delete()
        self.boost.is_active = False
        self.boost.save()
        self.channel.is_active = False
        self.channel.save()
        self.sig_a.is_active = False
        self.sig_a.save()
        self.ping_template.is_active = False
        self.ping_template.save()
        self.motd_template.delete()
        self.alt.character_ownership.delete()

        initial, warnings = self.load(preset)

        for name in ("fleet_type", "comms", "logi_channel", "boost_channel", "ping_channel", "ping_template", "motd_template"):
            self.assertEqual(initial[name], "", name)
        self.assertEqual(initial["ping_mentions"], [NONE])
        self.assertNotIn("fc_character_id", initial)
        self.assertEqual(
            warnings,
            [
                "Fleet type 'StratOp' is no longer available.",
                "Comms preset 'Main Mumble' is no longer available.",
                "Logi channel 'Logi: Logi' is no longer available.",
                "Boost channel 'Boost: Boosts' is no longer available.",
                "Ping channel 'STR' is no longer available.",
                "Ping target '@Sig A' is no longer available.",
                "Ping template 'Ping: Short Ping' is no longer available.",
                "MOTD template 'MOTD: Short MOTD' is no longer available.",
                "FC character 'Preset Alt' is no longer one of your characters.",
            ],
        )

    def test_mentions_keep_the_roles_still_offered(self):
        sig_b = f.ping_role("Sig B")
        preset = save_preset(self.fc, "Two Roles", self.data(ping_channel="", ping_mentions=[role_choice(self.sig_a), role_choice(sig_b)]))
        sig_b.delete()

        initial, warnings = self.load(preset)

        self.assertEqual(initial["ping_mentions"], [role_choice(self.sig_a)])
        self.assertEqual(warnings, ["Ping target '@Sig B' is no longer available."])

    def test_missing_doctrine_becomes_a_custom_doctrine(self):
        preset = save_preset(self.fc, "Doctrine", self.data())
        preset.values["fields"]["doctrine_choice"] = "fittings:fittings.doctrine|12|Armor HACs"
        preset.values["labels"]["doctrine_choice"] = "Armor HACs"
        preset.save()

        initial, warnings = self.load(preset)

        self.assertEqual(initial["doctrine_choice"], "")
        self.assertEqual(initial["custom_doctrine"], "Armor HACs")
        self.assertEqual(warnings, ["Doctrine 'Armor HACs' is no longer available; it was entered as a custom doctrine."])

    def test_doctrine_still_offered_is_kept(self):
        choice = "fittings:fittings.doctrine|12|Armor HACs"
        with mock.patch(
            "fleetops.forms.get_doctrines",
            return_value=[mock.Mock(source="fittings:fittings.doctrine", external_id="12")],
        ) as doctrines:
            doctrines.return_value[0].name = "Armor HACs"
            preset = save_preset(self.fc, "Doctrine", self.data(doctrine_choice=choice, custom_doctrine=""))
            initial, warnings = self.load(preset)

        self.assertEqual(preset.values["labels"]["doctrine_choice"], "Armor HACs")
        self.assertEqual(initial["doctrine_choice"], choice)
        self.assertEqual(warnings, [])

    def test_unknown_stored_values_are_ignored(self):
        preset = FleetStartPreset.objects.create(
            owner=self.fc,
            name="Odd",
            values={"fields": {"operation_mode": "launch", "request_id": "x", "fleet_type": "abc", "nonsense": "1"}},
        )

        initial, warnings = self.load(preset)

        self.assertNotIn("operation_mode", initial)
        self.assertNotIn("request_id", initial)
        self.assertNotIn("nonsense", initial)
        self.assertEqual(initial["fleet_type"], "")
        self.assertEqual(warnings, ["Fleet type 'abc' is no longer available."])


class LastUsedTests(PresetTestBase):
    def setUp(self):
        super().setUp()
        f.add_token(self.fc, self.alt)
        patches = {
            "detect": mock.patch.object(
                operations_module,
                "detect_character_fleet",
                side_effect=lambda user, character_id, fresh=False: FleetDetectionResult(
                    fleet_id=FLEET_ID, role="fleet_commander", fleet_boss_id=character_id
                ),
            ),
            "motd": mock.patch.object(operations_module, "set_fleet_motd"),
            "sync": mock.patch.object(operations_module, "sync_operation", return_value=0),
            "srp": mock.patch.object(
                operations_module, "create_srp_link", return_value=SRPLinkResult("test", message="No SRP in tests.")
            ),
            "post": mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")),
        }
        for patcher in patches.values():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.fc)

    def test_successful_start_saves_the_last_used_settings(self):
        response = self.client.post(reverse("fleetops:start_fleet"), self.data())

        operation = FleetOperation.objects.get()
        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False)
        last = FleetStartPreset.objects.get(owner=self.fc, is_last_used=True)
        self.assertEqual(last.name, "")
        self.assertEqual(last.fc_character_id, self.alt.character_id)
        self.assertEqual(last.values["fields"]["formup"], "Jita 4-4")
        self.assertEqual(last.values["fields"]["ping_channel"], str(self.channel.pk))
        self.assertEqual(last.values["fields"]["ping_mentions"], [role_choice(self.sig_a)])
        self.assertNotIn("request_id", last.values["fields"])

        # The next fleet, after the first one was closed.
        FleetOperation.objects.update(status=FleetOperation.Status.CLOSED)
        self.client.post(reverse("fleetops:start_fleet"), self.data(formup="Amarr"))
        self.assertEqual(FleetOperation.objects.count(), 2)
        last = FleetStartPreset.objects.get(owner=self.fc, is_last_used=True)
        self.assertEqual(last.values["fields"]["formup"], "Amarr")

    def test_failed_start_saves_nothing(self):
        self.client.post(reverse("fleetops:start_fleet"), self.data(formup=""))

        self.assertFalse(FleetStartPreset.objects.exists())

    def test_a_failing_save_does_not_break_the_start(self):
        with mock.patch("fleetops.views.save_last_used", side_effect=RuntimeError("boom")), self.assertLogs(
            "extensions.fleetops", "ERROR"
        ):
            response = self.client.post(reverse("fleetops:start_fleet"), self.data())

        operation = FleetOperation.objects.get()
        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False)

    def test_last_used_row_is_not_a_named_preset(self):
        save_last_used(self.fc, {"formup": "Jita"})

        response = self.client.get(reverse("fleetops:preset_list"))
        self.assertEqual(list(response.context["rows"]), [])
        last = FleetStartPreset.objects.get(owner=self.fc)
        self.assertEqual(self.client.post(reverse("fleetops:preset_delete", args=[last.pk])).status_code, 404)


class StartPagePresetTests(PresetTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.fc)

    def get(self, query=""):
        return self.client.get(reverse("fleetops:start_fleet") + query)

    def test_default_preset_is_applied_without_a_parameter(self):
        preset = self.preset("Home Defense", make_default=True, remember_fc=True)

        response = self.get()

        form = response.context["form"]
        self.assertEqual(form.initial["formup"], "Jita 4-4")
        self.assertEqual(form.initial["fc_character_id"], str(self.alt.character_id))
        self.assertIn("request_id", form.initial)
        self.assertContains(response, "Default preset Home Defense applied.")
        self.assertContains(response, 'href="?preset=none">Clear</a>', html=False)
        self.assertEqual(response.context["preset_selected"], str(preset.pk))

    def test_none_clears_the_default(self):
        self.preset("Home Defense", make_default=True)

        response = self.get("?preset=none")

        self.assertNotIn("formup", response.context["form"].initial)
        self.assertNotContains(response, "Default preset")

    def test_chosen_preset_and_last_used(self):
        chosen = self.preset("Roam")
        save_last_used(self.fc, {"formup": "Hek", "fleet_type": self.fleet_type})

        response = self.get(f"?preset={chosen.pk}")
        self.assertContains(response, "Preset Roam loaded.")
        self.assertEqual(response.context["form"].initial["formup"], "Jita 4-4")

        response = self.get("?preset=last")
        self.assertContains(response, "Last used settings loaded.")
        self.assertEqual(response.context["form"].initial["formup"], "Hek")
        self.assertContains(response, '<option value="last" selected>Last used settings</option>', html=True)

    def test_last_without_saved_settings(self):
        response = self.get("?preset=last")

        self.assertContains(response, "No last used settings are saved yet.")

    def test_load_warnings_are_shown(self):
        preset = self.preset("Roam")
        self.comms.delete()

        response = self.get(f"?preset={preset.pk}")

        self.assertContains(response, "Comms preset &#x27;Main Mumble&#x27; is no longer available.")

    def test_presets_of_other_users_are_not_found(self):
        theirs = save_preset(f.create_user(perms=f.FC_PERMS), "Theirs", self.data(fc_character_id=""))

        self.assertEqual(self.get(f"?preset={theirs.pk}").status_code, 404)
        self.assertEqual(self.get("?preset=abc").status_code, 404)

    def test_preset_bar_lists_own_presets_only(self):
        self.preset("Mine")
        save_preset(f.create_user(perms=f.FC_PERMS), "Theirs", {})

        response = self.get()

        self.assertEqual([preset.name for preset in response.context["presets"]], ["Mine"])
        self.assertContains(response, 'id="preset-bar"')
        self.assertContains(response, 'form="fleet-start-form"')
        self.assertNotContains(response, "Theirs")

    def test_preset_save_fields_never_block_a_start(self):
        response = self.get()

        # The preset name belongs to the start form, so the browser must not require it for a start.
        name_input = str(response.context["preset_save_form"]["name"])
        self.assertIn('form="fleet-start-form"', name_input)
        self.assertNotIn("required", name_input)


class PresetSaveViewTests(PresetTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.fc)

    def save(self, name="Home Defense", accept_json=True, **extra):
        data = self.data(**{"preset-name": name, **extra})
        return self.client.post(reverse("fleetops:preset_save"), data, **(JSON if accept_json else PAGE))

    def test_save_returns_json(self):
        response = self.save(**{"preset-remember_fc": "on", "preset-make_default": "on"})

        preset = FleetStartPreset.objects.get(owner=self.fc)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "conflict": False,
                "preset_id": preset.pk,
                "message": "Preset Home Defense saved. It is your default preset.",
            },
        )
        self.assertTrue(preset.is_default)
        self.assertEqual(preset.fc_character_id, self.alt.character_id)

    def test_conflict_then_overwrite(self):
        self.save()

        conflict = self.save(formup="Amarr").json()
        self.assertEqual(
            conflict,
            {"ok": False, "conflict": True, "preset_id": None, "message": "A preset named Home Defense exists."},
        )
        self.assertEqual(FleetStartPreset.objects.get().values["fields"]["formup"], "Jita 4-4")

        overwritten = self.save(formup="Amarr", **{"preset-overwrite": "1"}).json()
        self.assertTrue(overwritten["ok"])
        self.assertEqual(FleetStartPreset.objects.get().values["fields"]["formup"], "Amarr")

    def test_missing_name(self):
        response = self.save(name="").json()

        self.assertFalse(response["ok"])
        self.assertEqual(response["message"], "Preset not saved: This field is required.")
        self.assertFalse(FleetStartPreset.objects.exists())

    def test_remember_fc_without_a_character(self):
        response = self.save(fc_character_id="", **{"preset-remember_fc": "on"}).json()

        self.assertIn("No FC character of yours was selected", response["message"])

    def test_without_script_the_save_returns_to_the_start_page(self):
        response = self.save(accept_json=False)

        preset = FleetStartPreset.objects.get()
        self.assertRedirects(response, reverse("fleetops:start_fleet") + f"?preset={preset.pk}", fetch_redirect_response=False)
        self.assertEqual(flashed(response), ["Preset Home Defense saved."])

        conflict = self.save(accept_json=False)
        self.assertRedirects(conflict, reverse("fleetops:start_fleet") + "?preset=none", fetch_redirect_response=False)
        self.assertIn("Choose another name", flashed(conflict)[-1])

    def test_needs_start_fleet_and_post(self):
        member = f.create_user(perms=f.MEMBER_PERMS)
        self.client.force_login(member)
        self.assertEqual(self.save().status_code, 403)
        self.assertEqual(self.client.get(reverse("fleetops:preset_list")).status_code, 403)

        self.client.force_login(self.fc)
        self.assertEqual(self.client.get(reverse("fleetops:preset_save")).status_code, 405)


class PresetListTests(PresetTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.fc)
        self.home = self.preset("Home Defense", remember_fc=True)
        self.roam = self.preset("Roam")

    def post(self, name, preset, data=None):
        return self.client.post(reverse(f"fleetops:{name}", args=[preset.pk]), data or {})

    def test_list_shows_own_presets_with_a_summary(self):
        save_preset(f.create_user(perms=f.FC_PERMS), "Theirs", {})

        response = self.client.get(reverse("fleetops:preset_list"))

        rows = response.context["rows"]
        self.assertEqual([row["preset"].name for row in rows], ["Home Defense", "Roam"])
        self.assertEqual(rows[0]["summary"], "StratOp · Ferox Fleet · Main Mumble · STR · FC Preset Alt")
        self.assertNotContains(response, "Theirs")

    def test_rename(self):
        response = self.post("preset_rename", self.roam, {"name": "Weekend Roam"})

        self.assertRedirects(response, reverse("fleetops:preset_list"), fetch_redirect_response=False)
        self.roam.refresh_from_db()
        self.assertEqual(self.roam.name, "Weekend Roam")

    def test_rename_to_a_taken_name_is_refused(self):
        response = self.post("preset_rename", self.roam, {"name": "home defense"})

        self.assertEqual(flashed(response), ["You already have a preset named home defense."])
        self.roam.refresh_from_db()
        self.assertEqual(self.roam.name, "Roam")

    def test_rename_changes_the_letter_case(self):
        self.post("preset_rename", self.roam, {"name": "ROAM"})

        self.roam.refresh_from_db()
        self.assertEqual(self.roam.name, "ROAM")

    def test_delete(self):
        self.post("preset_delete", self.roam)

        self.assertFalse(FleetStartPreset.objects.filter(pk=self.roam.pk).exists())

    def test_set_and_unset_default(self):
        self.post("preset_default", self.home, {"is_default": "1"})
        self.post("preset_default", self.roam, {"is_default": "1"})
        self.home.refresh_from_db()
        self.roam.refresh_from_db()
        self.assertFalse(self.home.is_default)
        self.assertTrue(self.roam.is_default)

        self.post("preset_default", self.roam, {"is_default": "0"})
        self.roam.refresh_from_db()
        self.assertFalse(self.roam.is_default)

    def test_presets_of_other_users_are_not_found(self):
        other = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(other)

        for name, data in (("preset_rename", {"name": "Mine"}), ("preset_delete", {}), ("preset_default", {"is_default": "1"})):
            with self.subTest(view=name):
                self.assertEqual(self.post(name, self.home, data).status_code, 404)
        self.home.refresh_from_db()
        self.assertEqual(self.home.name, "Home Defense")
        self.assertFalse(self.home.is_default)
