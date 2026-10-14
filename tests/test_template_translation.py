# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The templates take their text from the catalog of the active language.

A catalog with a few Simplified Chinese messages stands in for the shipped one, so these tests
cover the template side only: the tags, the placeholders and the pieces that scripts read.
"""

import struct
import tempfile
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

import fleetops
from fleetops.models import CommsPreset, FleetStartPreset

from . import factories as f

TEMPLATE_DIR = Path(fleetops.__file__).resolve().parent / "templates"

MESSAGES = {
    # Common words carry the context "FleetOps".
    "FleetOps\x04Dashboard": "总览",
    "FleetOps\x04Fleet Operations": "舰队行动",
    "Start Fleet": "开舰队",
    "SEND PING & START FLEET": "发送 Ping 并开舰队",
    "SEND PRE PING & SCHEDULE FLEET": "发送预 Ping 并预定舰队",
    "Join %(name)s": "加入 %(name)s",
    "Open in voice client": "在语音客户端中打开",
    "Duration: %(duration)s": "时长：%(duration)s",
    "Delete the preset %(name)s?": "删除预设 %(name)s？",
    "Select a character to detect its current ESI fleet.": "选择角色以检测其当前的 ESI 舰队。",
    "Collapse or expand": "折叠或展开",
}


def write_catalog(directory: Path, messages: dict[str, str]) -> None:
    """A compiled django.mo for Simplified Chinese holding ``messages``."""
    target = directory / "zh_Hans" / "LC_MESSAGES"
    target.mkdir(parents=True)
    header = "Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=1; plural=0;\n"
    entries = sorted({"": header, **messages}.items(), key=lambda item: item[0].encode())
    originals = [key.encode() for key, _value in entries]
    translations = [value.encode() for _key, value in entries]
    count = len(entries)
    keys_start = 7 * 4 + 16 * count
    offsets = []
    position = keys_start
    for blob in originals:
        offsets.append((len(blob), position))
        position += len(blob) + 1
    value_offsets = []
    for blob in translations:
        value_offsets.append((len(blob), position))
        position += len(blob) + 1
    data = struct.pack("Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + 8 * count, 0, 0)
    data += b"".join(struct.pack("ii", *pair) for pair in offsets)
    data += b"".join(struct.pack("ii", *pair) for pair in value_offsets)
    data += b"".join(blob + b"\0" for blob in originals + translations)
    (target / "django.mo").write_bytes(data)


class TemplateSyntaxTests(SimpleTestCase):
    def test_every_template_compiles(self):
        names = sorted(path.relative_to(TEMPLATE_DIR).as_posix() for path in TEMPLATE_DIR.rglob("*.html"))

        self.assertGreater(len(names), 50)
        for name in names:
            with self.subTest(template=name):
                get_template(name)


class ChineseTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Chinese Lead")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        write_catalog(Path(directory.name), MESSAGES)
        override = override_settings(LOCALE_PATHS=[directory.name, *settings.LOCALE_PATHS])
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(translation.deactivate)
        self.client.force_login(self.lead)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"

    def test_pages_and_navigation_follow_the_language(self):
        page = self.client.get(reverse("fleetops:dashboard"))

        self.assertContains(page, "舰队行动")
        self.assertContains(page, ">总览</a>")
        self.assertContains(page, ">开舰队</a>")
        self.assertNotContains(page, "<h2 class=\"mb-0\">Fleet Operations</h2>")

    def test_the_start_button_labels_come_from_the_page(self):
        page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertContains(page, 'data-label-now="发送 Ping 并开舰队"')
        self.assertContains(page, 'data-label-pre-ping="发送预 Ping 并预定舰队"')
        self.assertContains(page, "选择角色以检测其当前的 ESI 舰队。")

    def test_texts_for_scripts_keep_their_placeholders(self):
        page = self.client.get(reverse("fleetops:manual_fleet"))

        # The script fills in %(duration)s with interpolate(); the translation keeps the name.
        self.assertContains(page, "时长：%(duration)s")

    def test_a_confirmation_in_an_attribute_names_the_preset(self):
        FleetStartPreset.objects.create(owner=self.lead, name="Roam <b>")

        page = self.client.get(reverse("fleetops:preset_list"))

        self.assertContains(page, 'data-confirm="删除预设 Roam &lt;b&gt;？"')

    def test_the_comms_join_page_uses_the_language_of_the_visitor(self):
        preset = CommsPreset.objects.create(name="Alliance Mumble", channel_name="Ops", voice_url="mumble://voice.example.com/ops")

        page = self.client.get(reverse("fleetops:comms_join", args=[preset.join_token]))

        self.assertContains(page, "在语音客户端中打开")
        self.assertContains(page, "加入 Alliance Mumble")


class EnglishTemplateTests(TestCase):
    def test_the_same_pages_stay_in_english(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="English Lead")
        self.client.force_login(lead)

        page = self.client.get(reverse("fleetops:start_fleet"))

        self.assertContains(page, 'data-label-now="SEND PING & START FLEET"')
        self.assertContains(page, 'data-label-pre-ping="SEND PRE PING & SCHEDULE FLEET"')
