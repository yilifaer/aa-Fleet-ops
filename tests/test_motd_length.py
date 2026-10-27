# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The MOTD length warning: the threshold is a setting, tags are counted and both numbers are shown."""

import uuid

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import translation

from fleetops.constants import DEFAULT_MOTD_TEMPLATE
from fleetops.models import MessageTemplate

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .static_files import static_source
from .test_messages import MessageTestBase, make_template

WARNING = (
    "The MOTD is %(length)s characters long with its tags (%(visible)s visible); "
    "the game may cut a MOTD longer than %(limit)s."
)


class MotdWarningRuleTests(MessageTestBase):
    def render_motd(self, template, **extra):
        return self.render(self.operation(motd_template=template, **extra))

    def test_the_default_motd_has_no_warning_with_the_default_setting(self):
        rendered = self.render(self.full_operation())

        self.assertEqual(rendered.motd_limit, 2000)
        self.assertGreater(rendered.motd_length, 400)
        self.assertEqual(rendered.warnings, [])

    def test_a_motd_of_the_length_that_works_in_the_game_does_not_warn(self):
        # The owner measured MOTDs of 1,743 characters with their tags in the game.
        for total in (1188, 1743, 2000):
            with self.subTest(total=total):
                template = make_template("x" * total, MessageTemplate.TemplateType.MOTD)

                rendered = self.render_motd(template)

                self.assertEqual(rendered.motd_length, total)
                self.assertEqual(rendered.warnings, [])

    def test_a_longer_motd_warns_with_both_numbers(self):
        template = make_template("<b>" + "x" * 2090 + "</b>", MessageTemplate.TemplateType.MOTD)

        rendered = self.render_motd(template)

        self.assertEqual((rendered.motd_length, rendered.motd_visible_length), (2097, 2090))
        self.assertEqual(
            rendered.warnings,
            [WARNING % {"length": 2097, "visible": 2090, "limit": 2000}],
        )
        self.assertEqual(rendered.blocking, [])

    def test_a_setting_of_zero_never_warns(self):
        f.settings(motd_warning_length=0)
        template = make_template("x" * 5000, MessageTemplate.TemplateType.MOTD)

        rendered = self.render_motd(template)

        self.assertEqual((rendered.motd_limit, rendered.warnings), (0, []))

    def test_the_setting_decides_the_threshold(self):
        f.settings(motd_warning_length=500)
        template = make_template("x" * 501, MessageTemplate.TemplateType.MOTD)

        rendered = self.render_motd(template)

        self.assertEqual(rendered.motd_limit, 500)
        self.assertEqual(rendered.warnings, [WARNING % {"length": 501, "visible": 501, "limit": 500}])
        f.settings(motd_warning_length=501)
        self.assertEqual(self.render_motd(template).warnings, [])

    def test_tags_count_for_the_warning_but_not_for_the_visible_length(self):
        # 1,900 visible characters inside tags that bring the total past the limit.
        template = make_template(
            "<font color=\"#bfffffff\">" + "<b>y</b>" * 230 + "x" * 1670 + "</font>", MessageTemplate.TemplateType.MOTD
        )

        rendered = self.render_motd(template)

        self.assertEqual(rendered.motd_visible_length, 1900)
        self.assertGreater(rendered.motd_length, 2000)
        self.assertEqual(len(rendered.warnings), 1)
        self.assertIn("(1900 visible)", rendered.warnings[0])

    def test_the_builtin_default_has_the_links_and_stays_short(self):
        self.assertLess(len(DEFAULT_MOTD_TEMPLATE), 1000)


class MotdLengthPreviewTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Length FC")
        self.fc_char = f.main_of(self.fc)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.client.force_login(self.fc)

    def preview(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            **extra,
        }
        return self.client.post(reverse("fleetops:preview_fleet"), data).json()

    def test_the_preview_reports_both_counts_and_the_limit(self):
        template = make_template("<b>" + "x" * 2090 + "</b>", MessageTemplate.TemplateType.MOTD)

        payload = self.preview(motd_template=str(template.pk))

        self.assertEqual(
            (payload["motd_length"], payload["motd_visible_length"], payload["motd_limit"]), (2097, 2090, 2000)
        )
        self.assertEqual(payload["warnings"], [WARNING % {"length": 2097, "visible": 2090, "limit": 2000}])

    def test_a_short_motd_reports_no_warning(self):
        payload = self.preview()

        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["motd_limit"], 2000)
        self.assertGreater(payload["motd_length"], payload["motd_visible_length"])

    def test_the_setting_reaches_the_preview(self):
        f.settings(motd_warning_length=0)

        self.assertEqual(self.preview()["motd_limit"], 0)


class MotdCounterScriptTests(TestCase):
    """The counter in the action bar of the start page (the script has no browser test here)."""

    def test_the_script_shows_both_counts_and_marks_the_limit(self):
        script = static_source("start_fleet.js")

        self.assertIn("MOTD %(visible)s visible, %(length)s with tags", script)
        self.assertIn("d.motd_limit", script)
        self.assertIn("d.motd_visible_length", script)


@tag("catalog")
class MotdWarningChineseTests(MessageTestBase):
    def test_the_warning_is_shown_in_the_language_of_the_viewer(self):
        template = make_template("<b>" + "x" * 2090 + "</b>", MessageTemplate.TemplateType.MOTD)

        with translation.override("zh-hans"):
            rendered = self.render(self.operation(motd_template=template))

        self.assertEqual(
            rendered.warnings, ["MOTD 含标签共 2097 个字符（可见 2090 个），游戏可能会截断超过 2000 个字符的 MOTD。"]
        )
