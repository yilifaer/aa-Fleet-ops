# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Failure wording of the fleet page: what went wrong and what to do next (D23).

The answers of the Fleet tab's routes carry ``hint`` for a failure, a UI text in the viewer's language that is
never stored; the script shows ``message`` and ``hint`` in one notification that stays until it is closed.
"""

from pathlib import Path

from django.contrib.messages import get_messages
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, override_settings, tag
from django.utils import translation

from fleetops.models import AuditLog
from fleetops.services import fleet_view
from fleetops.views_fleet_common import HINTS, failure_hint

from .test_fleet_structure_actions import FleetWriteTestCase

STATIC = Path(__file__).resolve().parent.parent / "fleetops" / "static" / "fleetops"
TRY_AGAIN = "Nothing was changed. Try again in a moment."
RELOAD = "Reload the page to see the fleet as it is now."
READ_AGAIN = "The fleet is being read again; check the tree and try again."


class HintTests(FleetWriteTestCase):
    def move(self, **changes):
        data = {
            "character_ids": [str(self.fc_char.character_id)],
            "role": "squad_member",
            "wing_id": str(self.fleet.wing_id("Main")),
            "squad_id": str(self.fleet.squad_id("Alpha")),
            **changes,
        }
        return self.post("fleet_members_move", data)

    def test_an_esi_error_says_that_nothing_changed(self):
        self.sim.fail("PostFleetsFleetIdWings", status=502)

        response = self.post("fleet_wing_add", {"name": "Logi"})

        body = response.json()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body["ok"], False)
        self.assertIn("ESI returned HTTP 502", body["message"])
        self.assertEqual(body["hint"], TRY_AGAIN)

    def test_a_move_that_esi_refuses_says_that_nothing_changed(self):
        self.sim.fail("PutFleetsFleetIdMembersMemberId", status=500, error="Boom")

        response = self.move()

        self.assertEqual(response.status_code, 502)
        self.assertIn("ESI returned HTTP 500", response.json()["message"])
        self.assertEqual(response.json()["hint"], TRY_AGAIN)

    def test_a_fleet_that_changed_under_the_page_asks_for_a_reload(self):
        for name in ("Three", "Four", "Five"):
            self.fleet.add_wing(name)
        self.read_fleet()

        response = self.post("fleet_wing_add")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "EVE allows at most 5 wings.")
        self.assertEqual(response.json()["hint"], RELOAD)

    def test_a_gone_unit_says_the_fleet_is_read_again(self):
        response = self.move(squad_id="4000000009")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["hint"], READ_AGAIN)

    def test_a_refused_rule_and_the_error_limit_need_no_hint(self):
        refused = self.move(character_ids=[])
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("hint", refused.json())

        # The message of the limit already says how long to wait.
        self.sim.fail("PostFleetsFleetIdWings", status=420)
        limited = self.post("fleet_wing_add", {"name": "Logi"})
        self.assertIn("ESI error limit reached", limited.json()["message"])
        self.assertNotIn("hint", limited.json())

    def test_a_success_carries_no_hint(self):
        response = self.post("fleet_wing_add")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertNotIn("hint", response.json())

    def test_the_hint_is_never_stored(self):
        self.sim.fail("PostFleetsFleetIdWings", status=502)

        self.post("fleet_wing_add", {"name": "Logi"})

        [entry] = self.entries("fleet.wing_create")
        for text in (TRY_AGAIN, RELOAD, READ_AGAIN):
            self.assertNotIn(text, str(entry.new_value))
        self.assertNotIn("hint", entry.new_value)
        self.assertEqual(AuditLog.objects.filter(action="fleet.wing_create").count(), 1)

    def test_a_form_post_keeps_its_message(self):
        # A plain form post (no script) is redirected with the message; the hint is for the script's notification.
        self.sim.fail("PostFleetsFleetIdWings", status=502)
        client = Client()
        client.force_login(self.fc)

        response = client.post(
            self.url("fleet_wing_add"), {"name": "Logi"}, HTTP_ACCEPT="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [str(message) for message in get_messages(response.wsgi_request)],
            ["Could not create a wing: ESI returned HTTP 502 (Bad Gateway)."],
        )

    def test_the_hint_table_is_the_one_of_the_design(self):
        self.assertEqual(sorted(HINTS), [404, 409, 502])
        for status, text in ((502, TRY_AGAIN), (409, RELOAD), (404, READ_AGAIN)):
            self.assertEqual(failure_hint(status), text)
        for status in (200, 400, 403, 429):
            self.assertEqual(failure_hint(status), "")
        self.assertEqual(failure_hint(502, ok=True), "")
        self.assertEqual(failure_hint(502, message="ESI rate limit reached; try again later."), "")


class JobHintTests(FleetWriteTestCase):
    def status_html(self, job):
        panel = fleet_view.fleet_panel(self.operation, self.fc)
        return render_to_string("fleetops/operation/_fleet_status.html", {**panel, "fleet_job": job})

    def job(self, failed, state="done"):
        return {
            "id": "job",
            "kind": "move",
            "label": "Move 3 members",
            "state": state,
            "total": 3,
            "done": 3,
            "failed": failed,
            "messages": [{"ok": False, "text": "Could not move Pilot A."}] if failed else [],
        }

    def test_a_job_with_failures_points_to_the_operation_actions(self):
        html = self.status_html(self.job(failed=1))

        self.assertIn('id="fleet-job-hint"', html)
        self.assertIn("See Operation Actions for each failure.", html)
        self.assertIn("progress-bar is-failed", html)
        self.assertNotIn("bg-warning", html)

    def test_a_job_without_failures_has_no_hint(self):
        html = self.status_html(self.job(failed=0))

        self.assertNotIn('id="fleet-job-hint"', html)
        self.assertNotIn("is-failed", html)

    def test_a_stopped_job_has_the_hint_too(self):
        self.assertIn('id="fleet-job-hint"', self.status_html(self.job(failed=0, state="failed")))


class ScriptTests(SimpleTestCase):
    """The page script shows the message and the hint in one notification of the shared stack."""

    def source(self, name):
        return (STATIC / name).read_text(encoding="utf-8")

    def test_fleet_live_uses_the_shared_toast_with_five_seconds(self):
        script = self.source("fleet_live.js")

        self.assertIn("window.FleetOps.toast(", script)
        self.assertIn("const FLASH_SECONDS = 5;", script)
        self.assertNotIn("alert alert-", script)
        self.assertNotIn("FLASHES_SHOWN", script)

    def test_the_toast_text_is_the_message_and_the_hint(self):
        script = self.source("fleet_live.js")

        self.assertIn("answer.hint ? `${answer.message} ${answer.hint}` : answer.message", script)
        self.assertIn("flash(answerText(answer)", script)

    def test_failures_stay_until_closed_and_successes_go_away(self):
        # The shared stack keeps a warning or an error until it is closed and removes the others after the seconds.
        toast = self.source("fleetops.js")

        self.assertIn("!options.sticky && (tone === 'ok' || tone === 'info')", toast)

    def test_the_fallback_of_the_move_script_uses_the_toast_too(self):
        script = self.source("fleet_actions.js")

        self.assertIn("window.FleetOps.toast(message, level)", script)
        self.assertNotIn("alert alert-", script)

    def test_the_fleet_page_stack_is_the_toast_stack(self):
        path = STATIC.parent.parent / "templates" / "fleetops" / "operation" / "_fleet.html"
        template = path.read_text(encoding="utf-8")

        self.assertEqual(template.count('<div id="fleet-flash" class="fo-toasts" aria-live="polite"></div>'), 2)

    def test_an_expired_login_offers_a_reload_in_the_toast(self):
        script = self.source("fleet_live.js")

        self.assertIn("action: { label: gettext('Reload'), onClick: () => window.location.reload() }", script)


@tag("catalog")
@override_settings(LANGUAGE_CODE="zh-hans")
class ChineseHintTests(FleetWriteTestCase):
    """The hints follow the viewer's language (needs the compiled catalog)."""

    def post_in_chinese(self, name, data=None, **kwargs):
        client = Client(HTTP_ACCEPT_LANGUAGE="zh-hans")
        client.force_login(self.fc)
        with translation.override("zh-hans"):
            return client.post(self.url(name, **kwargs), data or {}, HTTP_ACCEPT="application/json")

    def test_the_hint_is_chinese(self):
        self.sim.fail("PostFleetsFleetIdWings", status=502)

        response = self.post_in_chinese("fleet_wing_add", {"name": "Logi"})

        self.assertEqual(response.status_code, 502)
        self.assertNotEqual(response.json()["hint"], TRY_AGAIN)
        self.assertRegex(response.json()["hint"], r"[\u4e00-\u9fff]")

    def test_every_hint_has_a_translation(self):
        with translation.override("zh-hans"):
            for status, english in ((502, TRY_AGAIN), (409, RELOAD), (404, READ_AGAIN)):
                with self.subTest(status=status):
                    self.assertNotEqual(str(HINTS[status]), english)
