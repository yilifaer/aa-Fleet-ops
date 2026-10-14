# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the public comms join page and the comms preset configuration that feeds it."""

import json
import uuid

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from fleetops.forms_config import CommsPresetForm
from fleetops.models import AuditLog, CommsPreset
from fleetops.services import links
from fleetops.validators import VOICE_URL_SCHEMES

from . import factories as f

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]
VOICE_URL = "mumble://voice.example.com/Ops"


def join_path(preset):
    return reverse("fleetops:comms_join", args=[preset.join_token])


class CommsJoinPageTests(TestCase):
    """The page as logged-in members see it; anonymous access is covered in test_public_urls."""

    def setUp(self):
        self.client.force_login(f.create_user())
        self.preset = CommsPreset.objects.create(name="Main Comms", channel_name="Fleet 1", voice_url=VOICE_URL)

    def test_active_preset_offers_the_voice_link(self):
        response = self.client.get(join_path(self.preset))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/comms_join.html")
        self.assertTemplateUsed(response, "public/base.html")
        self.assertContains(response, "Joining Main Comms…")
        self.assertContains(response, "Fleet 1")
        self.assertContains(response, f'<a class="btn btn-primary w-100 mb-3" href="{VOICE_URL}" rel="noreferrer">Open in voice client</a>', html=True)
        self.assertContains(response, f'value="{VOICE_URL}" readonly')
        self.assertContains(response, ">Copy</button>")
        self.assertContains(
            response, f'<script id="fleetops-voice-url" type="application/json">"{VOICE_URL}"</script>'
        )
        self.assertContains(response, "window.location.href = voiceUrl;")

    def test_page_is_not_indexed_cached_or_referred(self):
        response = self.client.get(join_path(self.preset))

        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, '<meta name="robots" content="noindex">')

    def test_the_voice_link_is_never_written_into_the_script(self):
        response = self.client.get(join_path(self.preset))

        script = response.content.decode().rsplit("<script>", 1)[1]
        self.assertNotIn(VOICE_URL, script)

    def test_every_voice_client_scheme_is_served(self):
        for scheme in VOICE_URL_SCHEMES:
            with self.subTest(scheme=scheme):
                url = f"{scheme}://voice.example.com/Ops"
                CommsPreset.objects.filter(pk=self.preset.pk).update(voice_url=url)

                response = self.client.get(join_path(self.preset))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'href="{url}"')

    def test_log_in_details_stored_outside_the_forms_are_never_shown(self):
        preset = CommsPreset.objects.create(name="Personal", voice_url="mumble://alice:s3cret@voice.example.com/Ops")
        web = CommsPreset.objects.create(name="Web", voice_url="https://alice:s3cret@voice.example.com/")

        response = self.client.get(join_path(preset))

        self.assertContains(response, 'value="mumble://voice.example.com/Ops" readonly')
        self.assertNotContains(response, "s3cret")
        self.assertNotContains(response, "alice")
        self.assertEqual(links.comms_link(web), "https://voice.example.com/")
        self.assertEqual(links.public_voice_url(web), "https://voice.example.com/")

    def test_inactive_preset_is_not_found(self):
        self.preset.is_active = False
        self.preset.save()

        self.assertEqual(self.client.get(join_path(self.preset)).status_code, 404)

    def test_unknown_token_is_not_found(self):
        self.assertEqual(self.client.get(reverse("fleetops:comms_join", args=[uuid.uuid4()])).status_code, 404)

    def test_preset_without_a_voice_link_is_not_found(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                CommsPreset.objects.filter(pk=self.preset.pk).update(voice_url=value)

                self.assertEqual(self.client.get(join_path(self.preset)).status_code, 404)

    def test_stored_script_links_are_not_found(self):
        # Rows written before the voice link validator, or straight into the database.
        for value in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd", "//evil.example"):
            with self.subTest(value=value):
                CommsPreset.objects.filter(pk=self.preset.pk).update(voice_url=value)

                response = self.client.get(join_path(self.preset))

                self.assertEqual(response.status_code, 404)
                self.assertNotIn("alert(1)", response.content.decode())

    def test_markup_in_stored_values_is_escaped(self):
        CommsPreset.objects.filter(pk=self.preset.pk).update(
            name="<b>Ops</b>", channel_name="<i>One</i>", voice_url='mumble://voice.example.com/"><script>alert(1)</script>'
        )

        response = self.client.get(join_path(self.preset))

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert(1)", content)
        self.assertNotIn("<b>Ops</b>", content)
        self.assertIn("&lt;b&gt;Ops&lt;/b&gt;", content)
        self.assertIn("\\u003Cscript\\u003Ealert(1)\\u003C/script\\u003E", content)
        voice_url = json.loads(content.split('type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(voice_url, 'mumble://voice.example.com/"><script>alert(1)</script>')

    def test_only_get_is_allowed(self):
        response = self.client.post(join_path(self.preset))

        self.assertEqual(response.status_code, 405)

    def test_any_logged_in_user_may_open_it(self):
        self.client.force_login(f.create_user(perms=[]))

        self.assertEqual(self.client.get(join_path(self.preset)).status_code, 200)


class CommsPresetFormTests(TestCase):
    def form(self, voice_url, instance=None, **extra):
        data = {"name": "Main Comms", "channel_name": "", "voice_url": voice_url, "description": "", "is_active": "on"}
        data.update(extra)
        return CommsPresetForm(data, instance=instance)

    def test_script_links_are_rejected(self):
        for value in ("javascript:alert(1)", "data:text/html,x", "vbscript:x", "mumble://voice example.com"):
            with self.subTest(value=value):
                form = self.form(value)
                self.assertFalse(form.is_valid())
                self.assertIn("voice_url", form.errors)

    def test_user_names_are_removed_with_a_warning(self):
        form = self.form("mumble://Pilot.Name@voice.example.com:64738/Ops?version=1.2.0")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["voice_url"], "mumble://voice.example.com:64738/Ops?version=1.2.0")
        self.assertEqual(form.warnings, ["The personal user name 'Pilot.Name' was removed from the voice link."])

    def test_passwords_are_removed_and_never_repeated(self):
        form = self.form("mumble://Pilot:hunter2@voice.example.com/Ops")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["voice_url"], "mumble://voice.example.com/Ops")
        self.assertEqual(form.warnings, ["The personal user name 'Pilot' and the password were removed from the voice link."])
        self.assertNotIn("hunter2", " ".join(form.warnings))

    def test_password_without_a_user_name(self):
        form = self.form("mumble://:hunter2@voice.example.com/Ops")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["voice_url"], "mumble://voice.example.com/Ops")
        self.assertEqual(form.warnings, ["The personal log-in details were removed from the voice link."])

    def test_links_without_log_in_details_have_no_warning(self):
        for value in (VOICE_URL, "https://discord.gg/abcdef", ""):
            with self.subTest(value=value):
                form = self.form(value)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["voice_url"], value)
                self.assertEqual(form.warnings, [])

    def test_new_join_link_is_only_offered_for_saved_presets(self):
        preset = CommsPreset.objects.create(name="Main Comms", voice_url=VOICE_URL)

        self.assertNotIn("new_join_link", CommsPresetForm().fields)
        self.assertIn("new_join_link", CommsPresetForm(instance=preset).fields)
        self.assertEqual(CommsPresetForm(instance=preset).fields["new_join_link"].label, "Issue a new join link")


class CommsConfigurationTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=CONFIG_PERMS + ["fleetops.view_audit_log"])
        self.client.force_login(self.admin)
        self.preset = CommsPreset.objects.create(name="Main Comms", channel_name="Fleet 1", voice_url=VOICE_URL)

    def edit(self, **extra):
        data = {"name": "Main Comms", "channel_name": "Fleet 1", "voice_url": VOICE_URL, "description": "", "is_active": "on"}
        data.update(extra)
        return self.client.post(reverse("fleetops:configuration_edit", args=["comms", self.preset.pk]), data, follow=True)

    def test_saving_a_personal_link_stores_and_audits_it_without_the_user_name(self):
        response = self.edit(voice_url="mumble://Pilot.Name:hunter2@voice.example.com/Ops")

        self.preset.refresh_from_db()
        self.assertEqual(self.preset.voice_url, VOICE_URL)
        self.assertIn(
            "The personal user name 'Pilot.Name' and the password were removed from the voice link.",
            [str(m) for m in response.context["messages"]],
        )
        entry = AuditLog.objects.get(action="configuration.update", object_type="CommsPreset")
        self.assertEqual(entry.new_value["voice_url"], VOICE_URL)
        self.assertNotIn("hunter2", response.content.decode())
        self.assertNotIn("hunter2", json.dumps(entry.new_value))

    def test_new_join_link_replaces_the_old_one(self):
        old_token = self.preset.join_token
        old_path = join_path(self.preset)

        self.edit(new_join_link="on")

        self.preset.refresh_from_db()
        self.assertNotEqual(self.preset.join_token, old_token)
        self.assertEqual(self.client.get(old_path).status_code, 404)
        self.assertEqual(self.client.get(join_path(self.preset)).status_code, 200)
        entry = AuditLog.objects.get(action="configuration.update", object_type="CommsPreset")
        self.assertIs(entry.new_value["join_link_reissued"], True)
        self.assertNotIn(str(old_token), json.dumps([entry.old_value, entry.new_value]))

    def test_join_link_is_kept_when_not_asked_for(self):
        old_token = self.preset.join_token

        self.edit(channel_name="Fleet 2")

        self.preset.refresh_from_db()
        self.assertEqual(self.preset.join_token, old_token)
        self.assertNotIn("join_link_reissued", AuditLog.objects.get(action="configuration.update").new_value)

    def test_new_presets_get_a_working_join_link(self):
        self.client.post(
            reverse("fleetops:configuration_add", args=["comms"]),
            {"name": "Alt Comms", "voice_url": "ts3server://ts.example.com", "is_active": "on", "new_join_link": "on"},
        )

        preset = CommsPreset.objects.get(name="Alt Comms")
        self.assertIsNotNone(preset.join_token)
        self.assertNotIn("join_link_reissued", AuditLog.objects.get(action="configuration.create").new_value)
        self.assertEqual(self.client.get(join_path(preset)).status_code, 200)

    @override_settings(SITE_URL="https://auth.example.org/")
    def test_list_shows_the_join_link_with_a_copy_button(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["comms"]))

        url = "https://auth.example.org" + join_path(self.preset)
        self.assertContains(response, f'value="{url}" readonly')
        self.assertContains(response, "data-fleetops-copy")

    def test_list_has_no_join_link_for_presets_that_cannot_be_joined(self):
        disabled = CommsPreset.objects.create(name="Old Comms", voice_url=VOICE_URL, is_active=False)
        silent = CommsPreset.objects.create(name="No Voice", voice_url="")

        response = self.client.get(reverse("fleetops:configuration_list", args=["comms"]))

        content = response.content.decode()
        self.assertIn(str(self.preset.join_token), content)
        self.assertNotIn(str(disabled.join_token), content)
        self.assertNotIn(str(silent.join_token), content)
        self.assertNotIn("Not a supported voice link", content)

    def test_list_marks_stored_voice_links_without_a_supported_scheme(self):
        bare = CommsPreset.objects.create(name="Bare Address", voice_url="ts.example.com:9987")

        response = self.client.get(reverse("fleetops:configuration_list", args=["comms"]))

        self.assertContains(response, "Not a supported voice link", count=1)
        self.assertNotContains(response, str(bare.join_token))

    def test_edit_form_offers_a_new_join_link(self):
        response = self.client.get(reverse("fleetops:configuration_edit", args=["comms", self.preset.pk]))

        self.assertContains(response, "Issue a new join link")
        self.assertNotContains(
            self.client.get(reverse("fleetops:configuration_add", args=["comms"])), "Issue a new join link"
        )


class PublicLinkHintTests(TestCase):
    def setUp(self):
        self.client.force_login(f.create_user(perms=CONFIG_PERMS))

    def pages(self):
        return [
            self.client.get(reverse("fleetops:configuration_index")),
            self.client.get(reverse("fleetops:configuration_list", args=["comms"])),
        ]

    @override_settings(APPS_WITH_PUBLIC_VIEWS=[], SITE_URL="https://auth.example.org")
    def test_hint_when_join_links_need_a_login(self):
        for response in self.pages():
            self.assertContains(response, "Comms join links need an Alliance Auth login.")
            self.assertContains(response, "<code>APPS_WITH_PUBLIC_VIEWS</code>")
            self.assertNotContains(response, "is empty.")

    @override_settings(APPS_WITH_PUBLIC_VIEWS=["fleetops"], SITE_URL="")
    def test_hint_when_site_url_is_empty(self):
        for response in self.pages():
            self.assertNotContains(response, "need an Alliance Auth login")
            self.assertContains(response, "<strong><code>SITE_URL</code> is empty.</strong>", html=True)

    @override_settings(APPS_WITH_PUBLIC_VIEWS=("some_app", "fleetops"), SITE_URL="https://auth.example.org")
    def test_no_hints_when_both_are_set(self):
        for response in self.pages():
            self.assertNotContains(response, "need an Alliance Auth login")
            self.assertNotContains(response, "is empty.")

    @override_settings(SITE_URL="https://auth.example.org")
    def test_missing_setting_counts_as_not_public(self):
        with self.settings():
            del settings.APPS_WITH_PUBLIC_VIEWS
            response = self.client.get(reverse("fleetops:configuration_index"))

        self.assertContains(response, "Comms join links need an Alliance Auth login.")

    @override_settings(APPS_WITH_PUBLIC_VIEWS=[], SITE_URL="")
    def test_other_sections_show_no_hints(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["fleet-types"]))

        self.assertNotContains(response, "need an Alliance Auth login")
        self.assertNotContains(response, "is empty.")
