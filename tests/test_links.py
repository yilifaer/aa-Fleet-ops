# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the link helpers used in pings, MOTDs and the configuration pages."""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from fleetops import validators
from fleetops.forms_config import ChannelPresetForm
from fleetops.models import AuditLog, ChannelPreset, CommsPreset
from fleetops.services import links
from fleetops.services.links import (
    absolute_url,
    channel_link_html,
    comms_join_url,
    comms_link,
    extract_ingame_channel_id,
    extract_link_text,
    site_url_configured,
    strip_url_userinfo,
)

from . import factories as f

CHANNEL_ID = "player_6f0e0f3c3e9a11eaa7a19abe94f6cb4f"
OTHER_CHANNEL_ID = "player_0123456789ABCDEF0123456789abcdef"
TOKEN = uuid.UUID("2f1d7c64-8b5e-4a8f-9a0c-6d3e2b1a0f99")

# What EVE puts on the clipboard when a channel link is copied from chat.
INGAME_HTML = (
    '<font size="14" color="#bfffffff"></font><font size="14" color="#ffd98d00">'
    f'<a href="joinChannel:{CHANNEL_ID}">STR Logi</a></font>'
)
INGAME_URL_TAG = f"<url=joinChannel:{CHANNEL_ID}//None//None>STR Logi</url>"


def preset(**values):
    defaults = {"name": "Main Comms", "voice_url": "", "join_token": TOKEN}
    defaults.update(values)
    return SimpleNamespace(**defaults)


def channel(**values):
    defaults = {"name": "Logi", "channel_value": "", "ingame_channel_id": ""}
    defaults.update(values)
    return SimpleNamespace(**defaults)


class AbsoluteUrlTests(SimpleTestCase):
    @override_settings(SITE_URL="https://auth.example.org/")
    def test_site_url_is_prefixed_once(self):
        self.assertEqual(absolute_url("/fleetops/comms/x/join/"), "https://auth.example.org/fleetops/comms/x/join/")
        self.assertEqual(absolute_url("fleetops/"), "https://auth.example.org/fleetops/")

    @override_settings(SITE_URL="https://auth.example.org/auth")
    def test_site_url_with_a_path_keeps_it(self):
        self.assertEqual(absolute_url("/fleetops/"), "https://auth.example.org/auth/fleetops/")

    def test_without_site_url_the_path_is_returned(self):
        for value in ("", "   ", None):
            with self.subTest(site_url=value), override_settings(SITE_URL=value):
                self.assertEqual(absolute_url("/fleetops/"), "/fleetops/")
                self.assertFalse(site_url_configured())

    @override_settings(SITE_URL="https://auth.example.org")
    def test_absolute_and_empty_values_are_left_alone(self):
        self.assertTrue(site_url_configured())
        self.assertEqual(absolute_url("https://other.example.org/x"), "https://other.example.org/x")
        self.assertEqual(absolute_url(""), "")
        self.assertEqual(absolute_url(None), "")


class StripUrlUserinfoTests(SimpleTestCase):
    def test_user_names_and_passwords_are_removed(self):
        cases = [
            (
                "mumble://bob@voice.example.org:64738/Fleet/Alpha?version=1.2.0",
                "mumble://voice.example.org:64738/Fleet/Alpha?version=1.2.0",
                "bob",
            ),
            ("mumble://bob:hunter2@voice.example.org/", "mumble://voice.example.org/", "bob"),
            ("mumble://bob:p@ss@voice.example.org", "mumble://voice.example.org", "bob"),
            ("ts3server://Bob%20Smith@ts.example.org?port=9987", "ts3server://ts.example.org?port=9987", "Bob Smith"),
            ("https://user@voice.example.org/join", "https://voice.example.org/join", "user"),
            ("  mumble://bob@voice.example.org  ", "mumble://voice.example.org", "bob"),
        ]
        for url, clean, user in cases:
            with self.subTest(url=url):
                self.assertEqual(strip_url_userinfo(url), (clean, user))

    def test_a_password_alone_is_removed_without_a_user_name(self):
        self.assertEqual(strip_url_userinfo("mumble://:secret@voice.example.org"), ("mumble://voice.example.org", ""))

    def test_the_password_is_never_returned(self):
        _clean, user = strip_url_userinfo("mumble://bob:hunter2@voice.example.org")

        self.assertNotIn("hunter2", user)

    def test_links_without_user_info_are_unchanged(self):
        for url in (
            "mumble://voice.example.org:64738/Fleet",
            "https://voice.example.org/@here/room",
            "discord://discord.com/channels/1/2",
            "http://[::1",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(strip_url_userinfo(url), (url, ""))
        self.assertEqual(strip_url_userinfo(None), ("", ""))


class CommsLinkTests(SimpleTestCase):
    def test_voice_url_schemes_come_from_the_validators(self):
        self.assertIs(links.VOICE_URL_SCHEMES, validators.VOICE_URL_SCHEMES)

    @override_settings(SITE_URL="https://auth.example.org")
    def test_join_url_is_the_absolute_public_join_page(self):
        with mock.patch.object(links, "reverse", return_value=f"/fleetops/comms/{TOKEN}/join/") as reverse_mock:
            url = comms_join_url(preset(voice_url="mumble://voice.example.org/Fleet"))

        self.assertEqual(url, f"https://auth.example.org/fleetops/comms/{TOKEN}/join/")
        reverse_mock.assert_called_once_with("fleetops:comms_join", args=[TOKEN])

    def test_join_url_is_empty_without_something_to_join(self):
        with mock.patch.object(links, "reverse", return_value="/join/") as reverse_mock:
            for value in (
                None,
                preset(voice_url=""),
                preset(voice_url="   "),
                preset(voice_url="mumble://voice.example.org", join_token=None),
                preset(voice_url="javascript:alert(1)"),
            ):
                with self.subTest(preset=value):
                    self.assertEqual(comms_join_url(value), "")
        reverse_mock.assert_not_called()

    def test_join_url_is_empty_when_the_route_does_not_reverse(self):
        with mock.patch.object(links, "reverse", side_effect=NoReverseMatch("missing")):
            self.assertEqual(comms_join_url(preset(voice_url="mumble://voice.example.org")), "")

    def test_web_links_are_used_as_they_are(self):
        with mock.patch.object(links, "reverse") as reverse_mock:
            self.assertEqual(
                comms_link(preset(voice_url=" https://discord.gg/abcdef ")), "https://discord.gg/abcdef"
            )
            self.assertEqual(comms_link(preset(voice_url="HTTP://voice.example.org")), "HTTP://voice.example.org")
        reverse_mock.assert_not_called()

    @override_settings(SITE_URL="https://auth.example.org/")
    def test_voice_client_links_go_through_the_join_page(self):
        with mock.patch.object(links, "reverse", return_value=f"/fleetops/comms/{TOKEN}/join/"):
            for voice_url in ("mumble://voice.example.org/Fleet", "ts3server://ts.example.org", "discord://x/y"):
                with self.subTest(voice_url=voice_url):
                    self.assertEqual(
                        comms_link(preset(voice_url=voice_url)),
                        f"https://auth.example.org/fleetops/comms/{TOKEN}/join/",
                    )

    def test_no_link_without_a_voice_url(self):
        self.assertEqual(comms_link(None), "")
        self.assertEqual(comms_link(preset(voice_url="")), "")


class CommsLinkDatabaseTests(TestCase):
    @override_settings(SITE_URL="https://auth.example.org")
    def test_join_url_of_a_stored_preset_uses_its_token(self):
        comms = CommsPreset.objects.create(name="Main", voice_url="mumble://voice.example.org/Fleet")

        url = comms_join_url(comms)

        self.assertEqual(url, "https://auth.example.org" + reverse("fleetops:comms_join", args=[comms.join_token]))
        self.assertIn(f"/{comms.join_token}/", url)
        self.assertEqual(comms_link(comms), url)


class IngameChannelIdTests(SimpleTestCase):
    def test_copied_ingame_links_are_recognised(self):
        for text in (
            INGAME_HTML,
            INGAME_URL_TAG,
            f'<a href="joinChannel:{CHANNEL_ID}">STR Logi</a>',
            f"joinChannel:{CHANNEL_ID}",
            f"  {CHANNEL_ID}  ",
            f"Logi channel: {CHANNEL_ID}.",
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_ingame_channel_id(text), CHANNEL_ID)

    def test_upper_case_hex_digits_are_kept(self):
        self.assertEqual(extract_ingame_channel_id(f"<url=joinChannel:{OTHER_CHANNEL_ID}>X</url>"), OTHER_CHANNEL_ID)

    def test_first_link_wins(self):
        text = f'<a href="joinChannel:{OTHER_CHANNEL_ID}">Boost</a> <a href="joinChannel:{CHANNEL_ID}">Logi</a>'

        self.assertEqual(extract_ingame_channel_id(text), OTHER_CHANNEL_ID)

    def test_a_join_link_wins_over_a_bare_id_before_it(self):
        text = f'{OTHER_CHANNEL_ID} <a href="joinChannel:{CHANNEL_ID}">Logi</a>'

        self.assertEqual(extract_ingame_channel_id(text), CHANNEL_ID)

    def test_garbage_gives_nothing(self):
        for text in (
            "",
            None,
            "STR Logi",
            "joinChannel:player_123",
            f"joinChannel:{CHANNEL_ID}0",
            f"x{CHANNEL_ID}",
            "player_" + "g" * 32,
            '<a href="showinfo:1377//93475128">Some Pilot</a>',
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_ingame_channel_id(text), "")


class LinkTextTests(SimpleTestCase):
    def test_text_of_the_first_link_without_markup(self):
        cases = [
            (INGAME_HTML, "STR Logi"),
            (INGAME_URL_TAG, "STR Logi"),
            ('<A HREF="joinChannel:x"><b>Logi</b>  &amp;\n Boost</A>', "Logi & Boost"),
            ('<a href="joinChannel:x">First</a><a href="joinChannel:y">Second</a>', "First"),
            ('<url=joinChannel:x>First</url> <a href="joinChannel:y">Second</a>', "First"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_link_text(text), expected)

    def test_no_link_gives_nothing(self):
        for text in ("", None, CHANNEL_ID, "<b>Logi</b>", '<a href="joinChannel:x">'):
            with self.subTest(text=text):
                self.assertEqual(extract_link_text(text), "")


class ChannelLinkHtmlTests(SimpleTestCase):
    def test_channel_with_an_ingame_id_becomes_a_join_link(self):
        html = channel_link_html(channel(channel_value="STR Logi", ingame_channel_id=CHANNEL_ID))

        self.assertEqual(html, f'<a href="joinChannel:{CHANNEL_ID}">STR Logi</a>')

    def test_the_name_is_escaped(self):
        html = channel_link_html(channel(channel_value='Logi & <b>"Boost"</b>', ingame_channel_id=CHANNEL_ID))

        self.assertEqual(html, f'<a href="joinChannel:{CHANNEL_ID}">Logi &amp; &lt;b&gt;&quot;Boost&quot;&lt;/b&gt;</a>')

    def test_preset_name_is_used_without_a_channel_name(self):
        html = channel_link_html(channel(name="Logi", channel_value="  ", ingame_channel_id=CHANNEL_ID))

        self.assertEqual(html, f'<a href="joinChannel:{CHANNEL_ID}">Logi</a>')

    def test_plain_name_without_a_valid_channel_id(self):
        for channel_id in ("", "player_123", f'{CHANNEL_ID}" onclick="alert(1)'):
            with self.subTest(channel_id=channel_id):
                self.assertEqual(
                    channel_link_html(channel(channel_value="Logi & Boost", ingame_channel_id=channel_id)),
                    "Logi &amp; Boost",
                )

    def test_no_preset_gives_nothing(self):
        self.assertEqual(channel_link_html(None), "")


class ChannelLinkDatabaseTests(TestCase):
    def test_stored_preset(self):
        preset_row = ChannelPreset.objects.create(
            name="Logi", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="STR Logi", ingame_channel_id=CHANNEL_ID
        )

        self.assertEqual(channel_link_html(preset_row), f'<a href="joinChannel:{CHANNEL_ID}">STR Logi</a>')


class ChannelPresetPasteTests(TestCase):
    """Pasting an in-game channel link into the channel preset form."""

    def form(self, instance=None, **values):
        data = {"name": "Logi", "channel_type": "logi", "channel_value": "", "ingame_channel_id": "", "is_active": "on"}
        data.update(values)
        return ChannelPresetForm(data, instance=instance)

    def test_copied_links_fill_the_id_and_the_channel_name(self):
        for text in (INGAME_HTML, INGAME_URL_TAG, f"  {INGAME_HTML}\n"):
            with self.subTest(text=text):
                form = self.form(ingame_link=text)

                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["ingame_channel_id"], CHANNEL_ID)
                self.assertEqual(form.cleaned_data["channel_value"], "STR Logi")

    def test_a_typed_channel_name_is_kept(self):
        form = self.form(ingame_link=INGAME_HTML, channel_value="Logi & Reps")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["channel_value"], "Logi & Reps")
        self.assertEqual(form.cleaned_data["ingame_channel_id"], CHANNEL_ID)

    def test_bare_ids_and_join_links_need_a_channel_name(self):
        for text in (CHANNEL_ID, f"joinChannel:{CHANNEL_ID}"):
            with self.subTest(text=text):
                named = self.form(ingame_link=text, channel_value="STR Logi")
                unnamed = self.form(ingame_link=text)

                self.assertTrue(named.is_valid(), named.errors)
                self.assertEqual(named.cleaned_data["ingame_channel_id"], CHANNEL_ID)
                self.assertFalse(unnamed.is_valid())
                self.assertEqual(list(unnamed.errors), ["channel_value"])

    def test_text_without_a_channel_link_is_rejected(self):
        for text in ("STR Logi", "joinChannel:player_123", '<a href="showinfo:1377//93475128">Some Pilot</a>'):
            with self.subTest(text=text):
                form = self.form(ingame_link=text, channel_value="STR Logi")

                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["ingame_link"], ["No joinChannel:player_… link found."])

    def test_a_pasted_link_replaces_the_typed_id(self):
        form = self.form(ingame_link=INGAME_HTML, ingame_channel_id=OTHER_CHANNEL_ID)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["ingame_channel_id"], CHANNEL_ID)

    def test_channel_name_is_still_required_without_a_link(self):
        form = self.form()

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["channel_value"], ["This field is required."])

    def test_the_link_field_is_not_stored_or_prefilled(self):
        preset_row = ChannelPreset.objects.create(
            name="Logi", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="STR Logi", ingame_channel_id=CHANNEL_ID
        )

        form = ChannelPresetForm(instance=preset_row)

        self.assertEqual(form["ingame_channel_id"].value(), CHANNEL_ID)
        self.assertIsNone(form["ingame_link"].value())
        self.assertEqual(form.fields["ingame_link"].label, "In-game link")


class ChannelPresetPastePageTests(TestCase):
    def setUp(self):
        self.client.force_login(f.create_user(perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"]))

    def test_pasted_link_is_stored_and_audited(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["channels"]),
            {"name": "Logi A", "channel_type": "logi", "ingame_link": INGAME_HTML, "is_active": "on"},
        )

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["channels"]))
        preset_row = ChannelPreset.objects.get(name="Logi A")
        self.assertEqual((preset_row.channel_value, preset_row.ingame_channel_id), ("STR Logi", CHANNEL_ID))
        self.assertEqual(channel_link_html(preset_row), f'<a href="joinChannel:{CHANNEL_ID}">STR Logi</a>')
        entry = AuditLog.objects.get(action="configuration.create")
        self.assertEqual(entry.new_value["ingame_channel_id"], CHANNEL_ID)
        self.assertNotIn("ingame_link", entry.new_value)

    def test_clearing_the_id_removes_the_link(self):
        preset_row = ChannelPreset.objects.create(
            name="Logi A", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="STR Logi", ingame_channel_id=CHANNEL_ID
        )

        self.client.post(
            reverse("fleetops:configuration_edit", args=["channels", preset_row.pk]),
            {"name": "Logi A", "channel_type": "logi", "channel_value": "STR Logi", "ingame_channel_id": "", "is_active": "on"},
        )

        preset_row.refresh_from_db()
        self.assertEqual(preset_row.ingame_channel_id, "")
        self.assertEqual(channel_link_html(preset_row), "STR Logi")

    def test_bad_paste_is_shown_on_the_form(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["channels"]),
            {"name": "Logi A", "channel_type": "logi", "channel_value": "STR Logi", "ingame_link": "STR Logi", "is_active": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No joinChannel:player_… link found.")
        self.assertFalse(ChannelPreset.objects.exists())
        self.assertFalse(AuditLog.objects.exists())
