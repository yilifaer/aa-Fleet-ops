# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The Start Fleet page: its elements and ids, the segmented choices, "More settings", the action bar with its
counters, the live Discord and in-game previews, the confirmation before a post and the pages around it
(scheduled fleet, presets)."""

import re
import uuid
from datetime import timedelta
from html.parser import HTMLParser

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import FleetOperation, FleetStartPreset, MessageTemplate

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .static_files import static_source

# Ids the scripts, the tests and the interface check rely on.
KEPT_IDS = (
    "preset-bar", "preset-load", "preset-select", "preset-save", "preset-save-button", "preset-save-result",
    "fleet-start-form", "pre-ping-fields", "eve-clock", "fleet-time-relative", "fleet-detection",
    "custom-doctrine-wrap", "ping-channel-help", "ping-mentions", "ping-preview", "motd-preview", "ping-length",
    "motd-length", "preview-missing", "preview-blocking", "preview-warnings", "preview-notes", "fleet-start-submit",
    "link-only-fleet-link", "ping-channel-rules", "id_fc_character_id", "id_fleet_type", "id_formup",
    "id_additional_message", "id_ping_channel",
)
NEW_IDS = ("ping-box", "ping-look", "motd-box", "motd-look", "start-more", "start-actionbar", "fo-group-pill")


class Tree(HTMLParser):
    """Which element ids sit inside which form or container (enough to check the structure of a page)."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.ids = {}
        self.parents = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"input", "br", "img", "link", "meta", "hr"}:
            if attributes.get("id"):
                self.ids[attributes["id"]] = list(self.stack)
            return
        if attributes.get("id"):
            self.ids[attributes["id"]] = list(self.stack)
        self.stack.append((tag, attributes.get("id", ""), attributes.get("class", "")))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def tree(content):
    parser = Tree()
    parser.feed(content)
    return parser


class StartPageTestBase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.channel = f.ping_channel("Alliance Pings", allow_here=True)
        self.client.force_login(self.fc)

    def page(self, **query):
        return self.client.get(reverse("fleetops:start_fleet"), query)


class PageStructureTests(StartPageTestBase):
    def test_the_ids_of_the_page_are_kept_and_the_new_parts_exist(self):
        content = self.page().content.decode()

        ids = set(re.findall(r'id="([^"]+)"', content))
        for element in (*KEPT_IDS, *NEW_IDS):
            with self.subTest(element=element):
                self.assertIn(element, ids)

    def test_the_cards_keep_their_order(self):
        content = self.page().content.decode()

        titles = re.findall(r'<div class="card-header[^"]*"><strong>([^<]+)</strong>', content)
        self.assertEqual(
            titles,
            ["Operation Mode", "Fleet Time", "Fleet", "Communication", "Additional Information", "Ping Preview", "MOTD Preview"],
        )

    def test_the_action_bar_is_inside_the_form_with_the_button_and_the_counters(self):
        parsed = tree(self.page().content.decode())

        for element in ("start-actionbar", "fleet-start-submit", "ping-length", "motd-length", "fo-group-pill"):
            with self.subTest(element=element):
                self.assertIn("fleet-start-form", [item[1] for item in parsed.ids[element]])
        self.assertIn("start-actionbar", [item[1] for item in parsed.ids["fleet-start-submit"]])
        # The counters moved out of the preview boxes: each id exists once.
        content = self.page().content.decode()
        for element in ("ping-length", "motd-length"):
            self.assertEqual(content.count(f'id="{element}"'), 1)

    def test_mode_and_timing_are_segmented_choices(self):
        content = self.page().content.decode()

        self.assertRegex(content, r'<div class="fo-seg fo-seg-block"[^>]*><input type="radio" name="operation_mode" value="full"')
        self.assertRegex(content, r'id="fleet-timing"><input type="radio" name="fleet_timing" value="now"')
        self.assertIn('<label for="id_operation_mode_1">PAP Tracking Only (no Discord ping / MOTD / SRP)</label>', content)
        self.assertNotIn("btn-lg", content)

    def test_the_previews_have_a_look_and_a_raw_view(self):
        content = self.page().content.decode()

        self.assertRegex(content, r'id="ping-view-look" value="look" checked>\s*<label for="ping-view-look">Look</label>')
        self.assertRegex(content, r'id="motd-view-look" value="look" checked>\s*<label for="motd-view-look">In game</label>')
        self.assertIn('<pre class="preview-box mb-0" id="ping-preview" hidden>', content)
        self.assertIn('id="motd-look" data-ui-check-skip="user colours"', content)
        self.assertIn('data-copy="#ping-preview"', content)

    def test_the_scripts_load_in_order(self):
        content = self.page().content.decode()

        self.assertLess(content.index("fleetops/preview.js"), content.index("fleetops/start_fleet.js"))

    def test_the_page_uses_pills_and_components_not_the_old_look(self):
        content = self.page().content.decode()
        body = content[content.index('<div class="fo-start">') :]

        for old in ("badge", "text-bg-", "btn-lg", "border-primary", "table-light"):
            with self.subTest(old=old):
                self.assertNotIn(old, body)


class MoreSettingsTests(StartPageTestBase):
    def opened(self, response):
        return bool(re.search(r'<details id="start-more" open>', response.content.decode()))

    def test_it_is_closed_by_default(self):
        response = self.page()

        self.assertContains(response, '<details id="start-more">')
        self.assertFalse(self.opened(response))

    def test_a_template_other_than_the_default_opens_it(self):
        template = MessageTemplate.objects.create(
            name="Short", template_type=MessageTemplate.TemplateType.PING, content="{{ fc }}"
        )

        self.assertFalse(self.opened(self.page()))
        preset = FleetStartPreset.objects.create(
            owner=self.fc, name="Short pings", values={"fields": {"ping_template": str(template.pk)}, "labels": {}}
        )

        self.assertTrue(self.opened(self.page(preset=preset.pk)))

    def test_an_error_in_a_field_inside_it_opens_it(self):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_template": "99999",
        }

        response = self.client.post(reverse("fleetops:start_fleet"), data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.opened(response))
        self.assertFalse(FleetOperation.objects.exists())

    def test_the_script_opens_it_for_an_error_found_by_the_preview(self):
        script = static_source("start_fleet.js")

        self.assertIn("moreSettings.contains(field)", script)
        self.assertIn("moreSettings.open = true", script)


class ScriptTests(TestCase):
    def test_no_script_puts_text_into_the_page_as_markup(self):
        for name in ("start_fleet.js", "preview.js"):
            source = static_source(name)
            with self.subTest(script=name):
                for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
                    self.assertNotIn(risky, source)

    def test_the_renderers_make_nodes_only(self):
        source = static_source("preview.js")

        for needed in ("createTextNode", "document.createElement", "DOMParser", "textContent"):
            self.assertIn(needed, source)
        # A link is made for web addresses only; the MOTD's links open nothing.
        self.assertIn("LINK = /^https?:\\/\\//i", source)
        self.assertIn("fo-ingame-link", source)

    def test_the_renderers_cover_what_a_ping_and_a_motd_use(self):
        source = static_source("preview.js")

        for marker in ("<t:", "@everyone|@here", "fo-discord-mention", "fo-discord-sub", "fo-discord-quote", "motdColour"):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        self.assertIn("window.FleetOpsPreview = { discord, motd, motdColour };", source)

    def test_the_start_script_shows_the_live_previews_and_asks_before_posting(self):
        source = static_source("start_fleet.js")

        self.assertIn("FleetOpsPreview.discord(pingLook, d.ping_display", source)
        self.assertIn("FleetOpsPreview.motd(motdLook, d.motd)", source)
        self.assertIn("FleetOpsPreview.skeleton(", source)
        # The question is kept on the form; fleetops.js asks it before the form is sent.
        self.assertIn("form.dataset.foConfirm =", source)
        self.assertIn("delete form.dataset.foConfirm", source)
        self.assertIn("Post this ping to %(channel)s? It notifies nobody.", source)
        self.assertNotIn("window.confirm", source)
        self.assertNotIn(" confirm(", source.replace("FleetOps.confirm(", ""))

    def test_the_confirmation_is_only_asked_when_something_is_posted(self):
        source = static_source("start_fleet.js")

        self.assertIn("attendanceOnly() || channel.disabled || !channel.value", source)
        self.assertIn("!editing || (!!sendUpdate && sendUpdate.checked)", source)


class EditScheduledPageTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.channel = f.ping_channel("Alliance Pings")
        self.operation = f.create_scheduled_operation(
            self.fc, scheduled_at=timezone.now() + timedelta(hours=3), type_obj=f.fleet_type("StratOp", "1.00"), ping_channel=self.channel
        )
        self.client.force_login(self.fc)

    def test_the_edit_page_keeps_its_form_and_gets_the_action_bar(self):
        response = self.client.get(reverse("fleetops:edit_scheduled_fleet", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 200)
        parsed = tree(response.content.decode())
        self.assertIn("start-actionbar", [item[1] for item in parsed.ids["id_send_update"]])
        self.assertContains(response, 'data-mode="edit"')
        self.assertContains(response, "SAVE CHANGES")
        self.assertNotContains(response, 'id="preset-bar"')
        self.assertNotContains(response, 'name="operation_mode"')
        self.assertNotContains(response, 'name="fleet_group"')

    def test_the_scheduled_page_asks_before_it_posts_and_uses_pills(self):
        response = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-fo-confirm="Cancel this scheduled fleet?"', content)
        self.assertIn("Post the pre ping to Alliance Pings again?", content)
        self.assertIn('data-ask-ping="Post the form-up ping to Alliance Pings?', content)
        for old in ("badge", "border-primary", "border-danger", "data-confirm="):
            with self.subTest(old=old):
                self.assertNotIn(old, content)
        self.assertRegex(content, r'<span class="fo-pill fo-pill-info">Scheduled</span>')


class PresetListTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.client.force_login(self.fc)

    def test_a_default_preset_is_a_pill_and_delete_asks_through_the_shared_dialog(self):
        preset = FleetStartPreset.objects.create(
            owner=self.fc, name="Moa fleet", is_default=True, values={"fields": {}, "labels": {}}
        )

        content = self.client.get(reverse("fleetops:preset_list")).content.decode()

        self.assertIn('<span class="fo-pill fo-pill-info">Default</span>', content)
        self.assertIn(f'data-fo-confirm="Delete the preset {preset.name}?"', content)
        self.assertNotIn("badge", content)
        self.assertNotIn("form[data-confirm]", content)

    def test_no_presets_is_an_empty_state_with_the_way_forward(self):
        content = self.client.get(reverse("fleetops:preset_list")).content.decode()

        self.assertIn('class="fo-empty"', content)
        self.assertIn(reverse("fleetops:start_fleet"), content)
