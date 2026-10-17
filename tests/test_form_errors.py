# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The page-level errors of a form: one callout that keeps the text readable in every theme."""

import re
import uuid
from pathlib import Path
from unittest import mock

from django import forms
from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import translation

from fleetops.models import DiscordWebhook, FleetOperation, StatisticsTarget
from fleetops.providers.esi import FleetDetectionResult, FleetESIError

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .static_files import static_source
from .test_configuration_audit import CONFIG_PERMS
from .test_scheduling import SchedulingTestBase

TEMPLATES = Path(__file__).resolve().parents[1] / "fleetops" / "templates" / "fleetops"
PARTIAL = "fleetops/partials/form_errors.html"
# The templates that show the page-level errors of their form, and how many places each has.
PLACES = {
    "start_fleet.html": 1,
    "scheduled_fleet.html": 1,
    "operation_edit.html": 1,
    "manual_fleet.html": 1,
    "manual_attendance.html": 1,
    "roster_import.html": 1,
    "pap_fleet_start.html": 1,
    "incentive_review.html": 2,
    "configuration/form.html": 1,
    "configuration/settings_form.html": 1,
    "configuration/srp_settings.html": 1,
    "send_ping.html": 1,
}
NOT_IN_FLEET = "Character is currently not in a fleet."
FLEET_ID = 1_046_000_000_001
WEBHOOK_URL = "https://discord.com/api/webhooks/555000111/Zq7-form-errors-webhook-token"


def callouts(html):
    """The lines of every page-level error callout of a page, one string per callout."""
    return re.findall(r'<div class="alert alert-danger" role="alert">((?:<div>.*?</div>)+)</div>', html, re.S)


def checked_form():
    """A bound form that has been validated, so errors can be added to it."""
    form = forms.Form({})
    form.is_valid()
    return form


class PartialTests(SimpleTestCase):
    def render(self, errors):
        return render_to_string(PARTIAL, {"errors": errors}).strip()

    def test_nothing_is_written_without_errors(self):
        for errors in (None, [], forms.Form().non_field_errors(), ""):
            with self.subTest(errors=errors):
                self.assertEqual(self.render(errors), "")

    def test_each_error_is_a_line_of_one_callout(self):
        html = self.render(["First problem.", "Second problem."])

        self.assertEqual(
            html,
            '<div class="alert alert-danger" role="alert"><div>First problem.</div><div>Second problem.</div></div>',
        )

    def test_the_errors_of_a_form_are_plain_lines_without_the_error_list(self):
        form = checked_form()
        form.add_error(None, "The ping is too long.")

        html = self.render(form.non_field_errors())

        self.assertIn("<div>The ping is too long.</div>", html)
        # The red list of field errors would put red text on the callout (the empty red bar of the owner's report).
        self.assertNotIn("errorlist", html)
        self.assertNotIn("<ul", html)
        self.assertNotIn("<li", html)

    def test_the_text_is_escaped(self):
        html = self.render(['<script>alert("x")</script> & more'])

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp; more", html)

    def test_an_error_with_markup_characters_in_a_message_stays_one_line(self):
        form = checked_form()
        form.add_error(None, forms.ValidationError("Use %(value)s <b>here</b>.", params={"value": "1 < 2"}))

        html = self.render(form.non_field_errors())

        self.assertIn("<div>Use 1 &lt; 2 &lt;b&gt;here&lt;/b&gt;.</div>", html)


class TemplateUseTests(SimpleTestCase):
    def sources(self):
        return {path.relative_to(TEMPLATES).as_posix(): path.read_text() for path in TEMPLATES.rglob("*.html")}

    def test_no_template_wraps_the_form_errors_in_an_alert_itself(self):
        wrapped = re.compile(r'alert-danger[^>]*>\s*\{\{[^}]*non_field_errors', re.S)
        for name, source in self.sources().items():
            with self.subTest(template=name):
                self.assertIsNone(wrapped.search(source))

    def test_the_twelve_places_use_the_partial(self):
        sources = self.sources()
        include = re.compile(r'\{% include "fleetops/partials/form_errors\.html" with errors=(\w+)\.non_field_errors %\}')
        for name, count in PLACES.items():
            with self.subTest(template=name):
                self.assertEqual(len(include.findall(sources[name])), count)
        self.assertEqual(sum(PLACES.values()), 13)
        used = {name for name, source in sources.items() if PARTIAL in source and name != "partials/form_errors.html"}
        self.assertEqual(used, set(PLACES))

    def test_the_errors_of_a_form_are_never_printed_as_a_list_inside_an_alert(self):
        # The only other place that prints non_field_errors writes the list on the page itself, outside any alert.
        for name, source in self.sources().items():
            for match in re.finditer(r"\{\{\s*\w+\.non_field_errors\s*\}\}", source):
                with self.subTest(template=name):
                    self.assertEqual(name, "srp/_decision_forms.html")
                    self.assertNotIn("alert", source[max(0, match.start() - 80) : match.start()])

    def test_an_error_list_inside_an_alert_takes_the_colour_of_the_alert(self):
        css = static_source("fleetops.css")

        self.assertRegex(css, r"\.fleetops \.alert \.errorlist\s*\{[^}]*color:\s*inherit")
        self.assertRegex(css, r"\.fleetops \.alert \.errorlist\s*\{[^}]*margin:\s*0")


class StartPageTests(TestCase):
    def setUp(self):
        f.builtin_default_templates()
        self.addCleanup(translation.deactivate)
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Start FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.channel = DiscordWebhook.objects.create(name="Pings", webhook_url=WEBHOOK_URL)
        detect = mock.patch(
            "fleetops.services.operations.detect_character_fleet",
            return_value=FleetDetectionResult(
                fleet_id=FLEET_ID, role="fleet_commander", fleet_boss_id=self.fc_char.character_id
            ),
        )
        detect.start()
        self.addCleanup(detect.stop)

    def payload(self):
        return {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
        }

    def post_with_error(self, language=None):
        self.client.force_login(self.fc)
        if language:
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        error = FleetESIError("NOT_IN_FLEET", NOT_IN_FLEET)
        with mock.patch("fleetops.views.start_fleet", side_effect=error):
            return self.client.post(reverse("fleetops:start_fleet"), self.payload())

    def test_the_error_is_a_line_in_the_callout_and_not_a_red_list(self):
        response = self.post_with_error()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(callouts(response.content.decode()), [f"<div>{NOT_IN_FLEET}</div>"])
        self.assertNotContains(response, 'class="errorlist')

    def test_the_error_reads_in_chinese(self):
        response = self.post_with_error(language="zh-hans")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(callouts(html), ["<div>该角色当前不在舰队中。</div>"])
        self.assertNotIn(NOT_IN_FLEET, html)
        self.assertNotContains(response, 'class="errorlist')

    def test_a_field_error_keeps_its_list_under_the_field(self):
        self.client.force_login(self.fc)
        data = self.payload()
        data["formup"] = ""

        response = self.client.post(reverse("fleetops:start_fleet"), data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(callouts(response.content.decode()), [])
        self.assertContains(response, 'class="errorlist')


class ConfigurationPageTests(TestCase):
    def test_the_error_of_a_configuration_form_is_in_the_callout(self):
        f.statistics_target(2026, 10, "1.00")
        admin = f.create_user(perms=CONFIG_PERMS)
        self.client.force_login(admin)

        response = self.client.post(
            reverse("fleetops:configuration_add", args=["statistics-targets"]),
            {"year": "2026", "month": "10", "corporation_average_target": "2.50", "member_minimum_pap": "1", "note": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(StatisticsTarget.objects.count(), 1)
        self.assertEqual(callouts(response.content.decode()), ["<div>A target for this month already exists.</div>"])
        self.assertNotContains(response, 'class="errorlist nonfield')


class ScheduledPageTests(SchedulingTestBase):
    def test_the_error_of_the_scheduled_fleet_page_is_in_the_callout(self):
        operation = self.scheduled()
        self.esi.put_in_fleet(self.fc_char, role="squad_member")
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:start_scheduled_fleet", args=[operation.uuid]),
            {"fc_character_id": str(self.fc_char.character_id), "send_ping": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/scheduled_fleet.html")
        lines = callouts(response.content.decode())
        self.assertEqual(len(lines), 1)
        self.assertIn("Plan FC is not the fleet boss.", lines[0])
        self.assertNotContains(response, 'class="errorlist nonfield')
        operation.refresh_from_db()
        self.assertEqual(operation.status, FleetOperation.Status.SCHEDULED)
