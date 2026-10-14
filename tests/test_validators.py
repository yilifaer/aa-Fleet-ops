# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from fleetops.forms_config import ChannelPresetForm, CommsPresetForm, PingRoleForm
from fleetops.models import MAX_DATABASE_ID
from fleetops.validators import VOICE_URL_SCHEMES, validate_voice_url, voice_url_scheme

CHANNEL_ID = "player_6f0ee87a6ac311ee9fb6b8ca3a65d540"


class VoiceUrlValidatorTests(SimpleTestCase):
    def test_voice_client_links_are_accepted(self):
        for url in (
            "mumble://voice.example.com:64738/Ops?version=1.2.0",
            "ts3server://voice.example.com?port=9987",
            "teamspeak://voice.example.com",
            "discord://discord.com/channels/1/2",
            "https://discord.gg/abcdef",
            "http://voice.example.com/",
            "MUMBLE://voice.example.com/",
            "",
        ):
            with self.subTest(url=url):
                validate_voice_url(url)

    def test_script_and_file_links_are_rejected(self):
        for url in (
            "javascript:alert(1)",
            " javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
            "voice.example.com",
            "mumble://voice.example.com/Ops Room",
            "mumble://voice.example.com/\tOps",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_voice_url(url)

    def test_each_problem_has_its_own_message(self):
        cases = {
            "ts.example.com:9987": "Enter a voice link starting with mumble://, ts3server://, teamspeak://, discord://, "
            "https://, http://.",
            "mumble://voice.example.com/Fleet Ops": "A voice link cannot contain spaces; write a space as %20.",
            "mumble://voice.example.com/\tOps": "A voice link cannot contain spaces; write a space as %20.",
            "mumble://alice:s3cret@voice.example.com/Ops": "Remove the personal user name and password from the voice link.",
            "https://Pilot@discord.gg/abcdef": "Remove the personal user name and password from the voice link.",
        }
        for url, message in cases.items():
            with self.subTest(url=url), self.assertRaises(ValidationError) as caught:
                validate_voice_url(url)
            self.assertEqual(caught.exception.messages, [message])

    def test_at_sign_outside_the_host_is_allowed(self):
        validate_voice_url("https://voice.example.com/join?user=a@b")

    def test_scheme_is_read_case_insensitively(self):
        self.assertEqual(voice_url_scheme(" TS3Server://host"), "ts3server")
        self.assertEqual(voice_url_scheme("no scheme"), "")
        self.assertIn("mumble", VOICE_URL_SCHEMES)


class ConfigurationFieldValidationTests(TestCase):
    def test_comms_form_rejects_a_script_link(self):
        form = CommsPresetForm({"name": "Bad", "voice_url": "javascript:alert(1)", "is_active": "on"})

        self.assertFalse(form.is_valid())
        self.assertIn("voice_url", form.errors)

    def test_channel_form_checks_the_ingame_channel_id(self):
        data = {"name": "Logi", "channel_type": "logi", "channel_value": "Logi & Reps", "is_active": "on"}

        self.assertTrue(ChannelPresetForm({**data, "ingame_channel_id": CHANNEL_ID}).is_valid())
        self.assertTrue(ChannelPresetForm({**data, "ingame_channel_id": ""}).is_valid())
        for value in ("player_123", f"{CHANNEL_ID}x", f'<a href="joinChannel:{CHANNEL_ID}">Logi</a>'):
            with self.subTest(value=value):
                form = ChannelPresetForm({**data, "ingame_channel_id": value})
                self.assertFalse(form.is_valid())
                self.assertIn("ingame_channel_id", form.errors)

    def test_ping_role_ids_must_fit_the_database(self):
        data = {"name": "Sig A", "is_active": "on", "sort_order": "0"}

        self.assertTrue(PingRoleForm({**data, "role_id": "123456789012345678"}).is_valid())
        for value in ("0", "-1", str(MAX_DATABASE_ID + 1), "abc"):
            with self.subTest(value=value):
                self.assertIn("role_id", PingRoleForm({**data, "role_id": value}).errors)
