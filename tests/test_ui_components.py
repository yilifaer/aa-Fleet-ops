# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The interface foundation: tone maps, pills, the time tag, notices, and the rules of the style sheets and scripts."""

import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from importlib import import_module
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import translation
from django.utils.safestring import mark_safe

from fleetops import ui, ui_notices
from fleetops.models import FleetOperation, FleetType, IncentivePeriod, OperationAction, ReviewStatus, SrpClaim
from fleetops.services import assessment
from fleetops.srp import display
from fleetops.templatetags import fleetops_ui

from . import factories as f
from . import i18n_extract as extract
from .static_files import static_source

UTC = dt_timezone.utc


def render(source, **context):
    return Template("{% load fleetops_ui fleetops_messages %}" + source).render(Context(context)).strip()


class ToneMapTests(SimpleTestCase):
    def test_every_stored_status_has_a_tone(self):
        for statuses, tones in (
            (FleetOperation.Status.values, ui.OPERATION_STATUS_TONES),
            (OperationAction.Status.values, ui.STEP_STATUS_TONES),
            (ReviewStatus.values, ui.REVIEW_STATUS_TONES),
            (SrpClaim.Status.values, ui.SRP_STATUS_TONES),
            (IncentivePeriod.Status.values, ui.PERIOD_STATUS_TONES),
            (display.WARNING_LEVELS + display.PRESENCE_LEVELS, ui.SRP_LEVEL_TONES),
        ):
            with self.subTest(tones=sorted(tones)):
                self.assertLessEqual(set(statuses), set(tones))
                self.assertLessEqual(set(tones.values()), set(ui.TONES))

    def test_the_fleet_status_tones(self):
        self.assertEqual(
            ui.OPERATION_STATUS_TONES,
            {
                "draft": "neutral",
                "scheduled": "info",
                "starting": "warn",
                "active": "ok",
                "ending": "warn",
                "closed": "neutral",
                "cancelled": "neutral",
                "error": "bad",
            },
        )

    def test_the_step_tones(self):
        self.assertEqual(ui.tone_for(ui.STEP_STATUS_TONES, "success"), "ok")
        self.assertEqual(ui.tone_for(ui.STEP_STATUS_TONES, "failed"), "bad")
        self.assertEqual(ui.tone_for(ui.STEP_STATUS_TONES, "skipped"), "neutral")
        self.assertEqual(ui.tone_for(ui.STEP_STATUS_TONES, "pending"), "warn")
        self.assertEqual(ui.tone_for(ui.STEP_STATUS_TONES, "running"), "warn")

    def test_an_approved_srp_claim_is_information_until_it_is_paid(self):
        self.assertEqual(ui.SRP_STATUS_TONES["approved"], "info")
        self.assertEqual(ui.SRP_STATUS_TONES["paid"], "ok")
        self.assertEqual(ui.SRP_STATUS_TONES["pending"], "warn")
        self.assertEqual(ui.SRP_STATUS_TONES["rejected"], "bad")
        self.assertEqual(ui.SRP_STATUS_TONES["withdrawn"], "neutral")

    def test_srp_levels(self):
        self.assertEqual(ui.SRP_LEVEL_TONES, {"danger": "bad", "warning": "warn", "info": "info", "ok": "ok"})

    def test_statistics_statuses_follow_the_running_month_rule(self):
        # Reached is green, not reached yet while the month runs is amber, red only once the month is over;
        # a month without a target (or nothing to assess) is not coloured at all.
        self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, assessment.MET), "ok")
        self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, assessment.OK), "ok")
        self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, assessment.IN_PROGRESS), "warn")
        self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, assessment.BELOW_SO_FAR), "warn")
        self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, assessment.MISSED), "bad")
        self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, assessment.BELOW), "bad")
        for status in (assessment.NONE, assessment.NO_MEMBERS, "unknown", None):
            self.assertEqual(ui.tone_for(ui.STATISTICS_TONES, status), "neutral")

    def test_an_unknown_or_unhashable_status_is_neutral(self):
        self.assertEqual(ui.tone_for(ui.OPERATION_STATUS_TONES, "nonsense"), "neutral")
        self.assertEqual(ui.tone_for(ui.OPERATION_STATUS_TONES, ["active"]), "neutral")
        self.assertEqual(ui.tone_for(ui.OPERATION_STATUS_TONES, None, default="warn"), "warn")

    def test_yes_and_no(self):
        self.assertEqual(ui.bool_tone(True), "ok")
        self.assertEqual(ui.bool_tone(False), "neutral")

    def test_open_and_reviewed_periods_can_still_change(self):
        self.assertEqual(ui.PERIOD_STATUS_TONES, {"open": "warn", "review": "warn", "finalized": "ok"})


class TypeToneTests(SimpleTestCase):
    def test_a_type_dot_is_its_primary_key_modulo_six_plus_one(self):
        self.assertEqual([ui.type_tone(pk) for pk in range(1, 14)], [2, 3, 4, 5, 6, 1, 2, 3, 4, 5, 6, 1, 2])

    def test_a_model_and_its_key_give_the_same_dot(self):
        self.assertEqual(ui.type_tone(FleetType(pk=9)), ui.type_tone(9))
        self.assertEqual(ui.type_tone(FleetType(pk=9)), 4)

    def test_no_type_has_no_dot_colour(self):
        for value in (None, "", "x", True, FleetType()):
            with self.subTest(value=value):
                self.assertEqual(ui.type_tone(value), 0)


class PillTests(SimpleTestCase):
    def test_a_pill_has_its_tone_and_its_words(self):
        self.assertEqual(ui.pill_html("ok", "Active"), '<span class="fo-pill fo-pill-ok">Active</span>')
        for tone in ui.TONES:
            self.assertIn(f'class="fo-pill fo-pill-{tone}"', ui.pill_html(tone, "x"))

    def test_an_unknown_tone_is_neutral(self):
        for tone in ("purple", "", None, "OK ", "ok warn"):
            with self.subTest(tone=tone):
                html = ui.pill_html(tone, "x")
                if tone == "OK ":
                    self.assertIn("fo-pill-ok", html)
                else:
                    self.assertIn("fo-pill-neutral", html)

    def test_a_type_pill_names_its_dot(self):
        self.assertEqual(ui.tone_class("type-3"), "fo-pill fo-pill-type fo-type-3")
        self.assertEqual(ui.tone_class(5), "fo-pill fo-pill-type fo-type-5")
        self.assertEqual(ui.tone_class("type"), "fo-pill fo-pill-type")
        self.assertEqual(ui.tone_class("type-7"), "fo-pill fo-pill-neutral")
        self.assertEqual(ui.tone_class(True), "fo-pill fo-pill-neutral")

    def test_the_words_are_escaped_and_safe_words_are_kept(self):
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", ui.pill_html("ok", "<b>x</b>"))
        self.assertIn("<b>x</b>", ui.pill_html("ok", mark_safe("<b>x</b>")))

    def test_attributes(self):
        html = ui.pill_html("warn", "Pending", id="badge-x", title='A "quoted" title', role="status", data_state="stale")
        self.assertEqual(
            html,
            '<span class="fo-pill fo-pill-warn" id="badge-x" title="A &quot;quoted&quot; title" role="status"'
            ' data-state="stale">Pending</span>',
        )

    def test_classes_are_added_to_the_pill_classes(self):
        self.assertIn('class="fo-pill fo-pill-neutral fo-pill-group ms-1"', ui.pill_html("neutral", "x", **{"class": "fo-pill-group ms-1"}))
        self.assertIn('class="fo-pill fo-pill-ok extra"', ui.pill_html("ok", "x", class_="extra"))

    def test_unknown_attributes_and_empty_values_are_left_out(self):
        html = ui.pill_html("ok", "x", onclick="alert(1)", style="color:red", id=None, title=False, data_Bad="x", data_a_b="1")
        self.assertEqual(html, '<span class="fo-pill fo-pill-ok" data-a-b="1">x</span>')

    def test_the_icon_replaces_the_dot(self):
        html = ui.pill_html("neutral", "Group", icon="users")
        self.assertEqual(
            html,
            '<span class="fo-pill fo-pill-neutral fo-pill-icon"><i class="fa-solid fa-users" aria-hidden="true"></i>Group</span>',
        )
        self.assertIn("fa-solid fa-users", ui.pill_html("neutral", "Group", icon="fa-users"))
        for odd in ('x" onclick="y', "a b", "<i>", ""):
            with self.subTest(icon=odd):
                self.assertNotIn("<i ", ui.pill_html("ok", "x", icon=odd))
                self.assertNotIn("fo-pill-icon", ui.pill_html("ok", "x", icon=odd))


class PillTagTests(SimpleTestCase):
    def setUp(self):
        # The labels are translated; another test may have left a different language active.
        english = translation.override("en")
        english.__enter__()
        self.addCleanup(english.__exit__, None, None, None)

    def test_the_pill_tag(self):
        self.assertEqual(render('{% pill "ok" "Linked" %}'), '<span class="fo-pill fo-pill-ok">Linked</span>')

    def test_the_pill_tag_takes_id_title_class_and_icon(self):
        html = render('{% pill "info" label id="badge-link-only" title=hint class="ms-2" icon="link" %}', label="PAP link", hint="x")
        self.assertEqual(
            html,
            '<span class="fo-pill fo-pill-info fo-pill-icon ms-2" id="badge-link-only" title="x">'
            '<i class="fa-solid fa-link" aria-hidden="true"></i>PAP link</span>',
        )

    def test_the_group_pill_markup_agreed_between_the_lanes(self):
        html = render(
            '{% load i18n %}{% translate "Group fleet" as label %}'
            '{% pill "neutral" name class="fo-pill-group" icon="users" title=label %}',
            name="Example Fleet Group",
        )
        self.assertEqual(
            html,
            '<span class="fo-pill fo-pill-neutral fo-pill-icon fo-pill-group" title="Group fleet">'
            '<i class="fa-solid fa-users" aria-hidden="true"></i>Example Fleet Group</span>',
        )

    def test_the_text_is_escaped(self):
        self.assertIn("&lt;script&gt;", render('{% pill "bad" text %}', text="<script>"))

    def test_the_status_filters(self):
        self.assertEqual(render("{{ status|op_status_pill }}", status="active"), '<span class="fo-pill fo-pill-ok">Active</span>')
        self.assertEqual(render("{{ status|op_status_pill }}", status="ending"), '<span class="fo-pill fo-pill-warn">Ending</span>')
        self.assertEqual(render("{{ status|op_status_pill }}", status="closed"), '<span class="fo-pill fo-pill-neutral">Closed</span>')
        self.assertEqual(render("{{ status|op_status_pill }}", status="scheduled"), '<span class="fo-pill fo-pill-info">Scheduled</span>')
        self.assertEqual(render("{{ status|op_status_pill }}", status="error"), '<span class="fo-pill fo-pill-bad">Error</span>')

    def test_the_status_filters_take_the_object_too(self):
        operation = FleetOperation(status=FleetOperation.Status.ACTIVE)
        self.assertEqual(render("{{ operation|op_status_pill }}", operation=operation), '<span class="fo-pill fo-pill-ok">Active</span>')

    def test_the_step_filter(self):
        self.assertEqual(render("{{ s|step_status_pill }}", s="success"), '<span class="fo-pill fo-pill-ok">Success</span>')
        self.assertEqual(render("{{ s|step_status_pill }}", s="failed"), '<span class="fo-pill fo-pill-bad">Failed</span>')
        self.assertEqual(render("{{ s|step_status_pill }}", s="skipped"), '<span class="fo-pill fo-pill-neutral">Skipped</span>')
        self.assertEqual(render("{{ s|step_status_pill }}", s="pending"), '<span class="fo-pill fo-pill-warn">Pending</span>')
        self.assertEqual(render("{{ s|step_status_pill }}", s="running"), '<span class="fo-pill fo-pill-warn">Running</span>')

    def test_an_unknown_status_is_still_shown_in_words(self):
        self.assertEqual(render("{{ s|op_status_pill }}", s="odd_status"), '<span class="fo-pill fo-pill-neutral">odd_status</span>')
        self.assertEqual(render("{{ s|op_status_pill }}", s=None), '<span class="fo-pill fo-pill-neutral"></span>')

    def test_the_type_filters(self):
        fleet_type = FleetType(pk=2, name="StratOp")
        self.assertEqual(render("{{ t|type_tone }}", t=fleet_type), "3")
        self.assertEqual(render("fo-type-{{ t|type_tone }}", t=fleet_type), "fo-type-3")
        self.assertEqual(render("{{ t|type_tone }}", t=None), "0")
        self.assertEqual(render("{{ t|type_pill }}", t=fleet_type), '<span class="fo-pill fo-pill-type fo-type-3">StratOp</span>')
        self.assertEqual(render("{{ t|type_pill }}", t=None), '<span class="fo-pill fo-pill-type"></span>')

    def test_the_tone_class_filter(self):
        self.assertEqual(render("{{ tone|tone_class }}", tone="warn"), "fo-pill fo-pill-warn")
        self.assertEqual(render("{{ tone|tone_class }}", tone="whatever"), "fo-pill fo-pill-neutral")

    def test_the_filters_are_registered_under_their_names(self):
        for name in ("op_status_pill", "step_status_pill", "type_tone", "tone_class", "type_pill"):
            self.assertIn(name, fleetops_ui.register.filters)
        for name in ("pill", "fleetops_notices"):
            self.assertIn(name, fleetops_ui.register.tags)


class EveWhenTests(SimpleTestCase):
    def test_a_time_with_its_relative_words(self):
        value = datetime(2026, 10, 16, 13, 5, 41, tzinfo=UTC)

        html = render("{% eve_when value %}", value=value)

        self.assertEqual(
            html,
            '<time class="fo-time" datetime="2026-10-16T13:05:41Z">2026-10-16 13:05 EVE</time>'
            '<span class="fo-time-rel" data-relative-utc="2026-10-16T13:05:41Z"></span>',
        )

    def test_the_time_is_in_eve_time_whatever_the_zone_of_the_value(self):
        value = datetime(2026, 10, 16, 21, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        html = render("{% eve_when value %}", value=value)

        self.assertIn("2026-10-16 13:05 EVE", html)
        self.assertIn('datetime="2026-10-16T13:05:00Z"', html)

    def test_a_naive_time_is_taken_as_eve_time(self):
        self.assertIn("2026-10-16 13:05 EVE", render("{% eve_when value %}", value=datetime(2026, 10, 16, 13, 5)))

    def test_the_relative_words_can_be_left_out(self):
        html = render("{% eve_when value relative=False %}", value=datetime(2026, 10, 16, 13, 5, tzinfo=UTC))

        self.assertEqual(html, '<time class="fo-time" datetime="2026-10-16T13:05:00Z">2026-10-16 13:05 EVE</time>')

    def test_nothing_is_written_without_a_time(self):
        for value in (None, "", "2026-10-16", 17):
            with self.subTest(value=value):
                self.assertEqual(render("{% eve_when value %}", value=value), "")
        self.assertEqual(render("{% eve_when missing %}"), "")


class EveClockTests(SimpleTestCase):
    def test_the_clock_shows_the_minute_in_eve_time(self):
        now = datetime(2026, 10, 16, 13, 5, 41, tzinfo=UTC)

        with mock.patch("fleetops.templatetags.fleetops_messages.timezone.now", return_value=now):
            self.assertEqual(render("{% eve_clock %}"), "13:05 EVE")

    def test_the_clock_ignores_the_zone_of_the_site(self):
        now = datetime(2026, 10, 16, 21, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        with mock.patch("fleetops.templatetags.fleetops_messages.timezone.now", return_value=now):
            self.assertEqual(render("{% eve_clock %}"), "13:05 EVE")


class NoticeTests(TestCase):
    def request(self, store=None, secure=False):
        request = RequestFactory().get("/", secure=secure)
        engine = import_module(settings.SESSION_ENGINE)
        request.session = store if store is not None else engine.SessionStore()
        return request

    def test_a_notice_is_kept_once(self):
        request = self.request()

        ui_notices.push_notice(request, "Marked paid.", "success", "Undo payment…", "/fleetops/srp/claims/5/#undo-payment")

        self.assertEqual(
            ui_notices.pop_notices(request),
            [
                {
                    "text": "Marked paid.",
                    "level": "success",
                    "action_label": "Undo payment…",
                    "action_url": "/fleetops/srp/claims/5/#undo-payment",
                }
            ],
        )
        self.assertEqual(ui_notices.pop_notices(request), [])

    def test_a_notice_survives_the_redirect(self):
        first = self.request()
        ui_notices.push_notice(first, "Saved.", "success")
        first.session.save()
        second = self.request(import_module(settings.SESSION_ENGINE).SessionStore(first.session.session_key))

        self.assertEqual([notice["text"] for notice in ui_notices.pop_notices(second)], ["Saved."])

    def test_at_most_three_notices_are_kept_and_the_newest_win(self):
        request = self.request()
        for number in range(5):
            ui_notices.push_notice(request, f"Notice {number}", "info")

        self.assertEqual([notice["text"] for notice in ui_notices.pop_notices(request)], ["Notice 2", "Notice 3", "Notice 4"])

    def test_levels(self):
        request = self.request()
        for level in ("success", "info", "warning", "danger", "error", "warn", "ok", "bad", "SUCCESS", "nonsense", None, ""):
            ui_notices.push_notice(request, "x", level)
            self.assertIn(ui_notices.pop_notices(request)[0]["level"], ui_notices.LEVELS)
        for level, expected in (("error", "danger"), ("warn", "warning"), ("ok", "success"), ("bad", "danger"), ("nonsense", "info"), ("SUCCESS", "success")):
            with self.subTest(level=level):
                ui_notices.push_notice(request, "x", level)
                self.assertEqual(ui_notices.pop_notices(request)[0]["level"], expected)

    def test_the_action_needs_a_label_and_an_address_of_this_site(self):
        request = self.request()
        cases = (
            ("/path/", "Open", True),
            ("/path/?a=1#b", "Open", True),
            ("http://testserver/path/", "Open", True),
            ("https://evil.example/", "Open", False),
            ("//evil.example/", "Open", False),
            ("javascript:alert(1)", "Open", False),
            ("/path/", "", False),
            ("", "Open", False),
            (None, None, False),
        )
        for url, label, kept in cases:
            with self.subTest(url=url, label=label):
                ui_notices.push_notice(request, "Text", "info", label, url)
                notice = ui_notices.pop_notices(request)[0]
                self.assertEqual((notice["action_label"], notice["action_url"]), (label, url) if kept else ("", ""))

    def test_a_secure_request_does_not_follow_a_plain_http_link(self):
        request = self.request(secure=True)

        ui_notices.push_notice(request, "Text", "info", "Open", "http://testserver/path/")

        self.assertEqual(ui_notices.pop_notices(request)[0]["action_url"], "")

    def test_nothing_is_kept_without_text_or_without_a_store(self):
        request = self.request()
        ui_notices.push_notice(request, "   ", "info")
        ui_notices.push_notice(request, None, "info")
        self.assertEqual(ui_notices.pop_notices(request), [])

        bare = RequestFactory().get("/")
        ui_notices.push_notice(bare, "Text", "info")
        self.assertEqual(ui_notices.pop_notices(bare), [])

    def test_a_long_text_is_cut(self):
        request = self.request()

        ui_notices.push_notice(request, "x" * 1000, "info")

        self.assertEqual(len(ui_notices.pop_notices(request)[0]["text"]), ui_notices.MAX_TEXT_LENGTH)

    def test_damaged_stored_data_is_ignored(self):
        request = self.request()
        for stored in ("text", 5, [1, "a", None, {"text": ""}, {"text": "ok", "level": "x"}], {"a": 1}):
            request.session[ui_notices.SESSION_KEY] = stored
            notices = ui_notices.pop_notices(request)
            self.assertTrue(all(set(notice) == {"text", "level", "action_label", "action_url"} for notice in notices))
        request.session[ui_notices.SESSION_KEY] = "text"
        ui_notices.push_notice(request, "New", "info")
        self.assertEqual([notice["text"] for notice in ui_notices.pop_notices(request)], ["New"])

    def test_the_tag_writes_the_notices_as_json_and_only_once(self):
        request = self.request()
        ui_notices.push_notice(request, "Marked paid.", "success", "Undo payment…", "/undo/")

        html = render("{% fleetops_notices %}", request=request)

        match = re.fullmatch(r'<script id="fo-notices" type="application/json">(.*)</script>', html)
        self.assertIsNotNone(match, html)
        self.assertEqual(
            json.loads(match.group(1)),
            [{"text": "Marked paid.", "level": "success", "action_label": "Undo payment…", "action_url": "/undo/"}],
        )
        self.assertEqual(render("{% fleetops_notices %}", request=request), "")

    def test_the_tag_cannot_be_broken_out_of(self):
        request = self.request()
        ui_notices.push_notice(request, "</script><script>alert(1)</script> & <b>", "danger")

        html = render("{% fleetops_notices %}", request=request)

        self.assertEqual(html.count("<script"), 1)
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn("<b>", html)

    def test_the_tag_writes_nothing_without_notices_or_a_request(self):
        self.assertEqual(render("{% fleetops_notices %}", request=self.request()), "")
        self.assertEqual(render("{% fleetops_notices %}"), "")


# --- Style sheets and scripts -----------------------------------------------------------------------------


def css_without_comments(source):
    return re.sub(r"/\*.*?\*/", "", source, flags=re.S)


class TokenTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = static_source("fleetops.css")

    def tokens(self):
        match = re.search(r":root, \.fleetops \{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(match, "the tokens are declared on :root and .fleetops")
        return dict(re.findall(r"(--fo-[\w-]+):\s*([^;]+);", match.group(1)))

    def test_every_token_is_declared_once_on_root_and_fleetops(self):
        tokens = self.tokens()

        for name in (
            "surface-1 surface-2 surface-3 hover surface-solid text-1 text-2 text-disabled border divider bar "
            "accent accent-bg link code highlight-bg focus ok ok-bg warn warn-bg bad bad-bg info info-bg "
            "type-1 type-2 type-3 type-4 type-5 type-6 ingame-bg ingame-fg shadow shadow-raised "
            "radius-sm radius radius-lg space-1 space-2 space-3 space-4 space-5 space-6 ease fast normal sticky-top"
        ).split():
            with self.subTest(token=name):
                self.assertIn(f"--fo-{name}", tokens)

    def test_the_values_that_were_checked_for_contrast(self):
        tokens = self.tokens()

        self.assertEqual(tokens["--fo-text-2"], "rgba(var(--bs-body-color-rgb), .85)")
        self.assertEqual(tokens["--fo-accent-bg"], "rgba(var(--bs-primary-rgb), .12)")
        self.assertEqual(tokens["--fo-highlight-bg"], "rgba(var(--bs-warning-rgb), .15)")
        self.assertEqual(tokens["--fo-warn"], "color-mix(in srgb, var(--bs-warning) 30%, var(--bs-body-color))")
        for name, theme in (("ok", "success"), ("bad", "danger"), ("info", "info")):
            self.assertEqual(tokens[f"--fo-{name}"], f"color-mix(in srgb, var(--bs-{theme}) 35%, var(--bs-body-color))")
            self.assertEqual(tokens[f"--fo-{name}-bg"], f"rgba(var(--bs-{theme}-rgb), .12)")
        self.assertEqual(tokens["--fo-link"], "color-mix(in srgb, var(--bs-link-color) 50%, var(--bs-body-color))")
        self.assertEqual(tokens["--fo-focus"], "color-mix(in srgb, var(--bs-primary) 50%, var(--bs-body-color))")
        self.assertEqual(tokens["--fo-accent"], "color-mix(in srgb, var(--bs-primary) 50%, var(--bs-body-color))")
        self.assertEqual(tokens["--fo-surface-1"], "rgba(var(--bs-body-color-rgb), .04)")
        self.assertEqual(tokens["--fo-surface-2"], "rgba(var(--bs-body-color-rgb), .07)")
        self.assertEqual(tokens["--fo-text-disabled"], "rgba(var(--bs-body-color-rgb), .45)")
        self.assertEqual(tokens["--fo-radius-sm"], "6px")
        self.assertEqual(tokens["--fo-radius"], "10px")
        self.assertEqual(tokens["--fo-radius-lg"], "14px")

    def test_dark_rules_follow_the_theme_and_never_the_browser(self):
        self.assertNotIn("prefers-color-scheme", self.css)
        self.assertNotIn("prefers-color-scheme", static_source("fleet.css"))
        self.assertNotIn("prefers-color-scheme", static_source("fleetops.js"))
        for selector in ("[data-bs-theme=dark]", "[data-theme=darkly]", "[data-fo-dark]"):
            self.assertIn(selector, self.css)
        # The dark values also reach the container, which re-declares the tokens itself.
        self.assertRegex(self.css, r":root:is\(\[data-bs-theme=dark\], \[data-theme=darkly\], \[data-fo-dark\]\) \.fleetops")

    def test_color_mix_has_a_fallback_for_old_browsers(self):
        self.assertIn("@supports not (color: color-mix(in srgb, red, blue))", self.css)
        block = self.css[self.css.index("@supports not") :]
        block = block[: block.index("\n}\n")]
        self.assertIn(".fo-actionbar", block)
        self.assertIn(".fo-commandbar", block)
        self.assertIn(".fleetops-srp-paybar", block)
        self.assertIn(".fo-toast", block)
        self.assertIn("background: var(--bs-body-bg)", block)

    def test_the_themes_variables_that_stay_light_in_darkly_are_not_used(self):
        forbidden = re.compile(
            r"var\(--bs-(?:tertiary-bg|secondary-bg|border-color(?!-translucent)|[a-z]+-bg-subtle|[a-z]+-border-subtle|"
            r"emphasis-color|light\b|dark\b|gray-\d+|white|black|highlight-bg|secondary-color|tertiary-color)"
        )
        for name in ("fleetops.css", "fleet.css", "fleetops.js", "preview.js"):
            with self.subTest(file=name):
                self.assertIsNone(forbidden.search(static_source(name)))

    def test_only_the_allowed_colour_literals_are_written(self):
        # Colours come from the theme; the exceptions are the dark backdrop of EVE images, the gold of the boss
        # crown and black in shadows.
        for name in ("fleetops.css", "fleet.css"):
            with self.subTest(file=name):
                css = css_without_comments(static_source(name))
                literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", css))
                self.assertLessEqual(literals, {"#111", "#d4a017"})
                for found in re.finditer(r"rgba?\(\s*\d[^)]*\)", css):
                    line = css[: found.start()].rsplit("\n", 1)[-1] + css[found.start() :].split("\n", 1)[0]
                    self.assertTrue(
                        found.group(0).startswith("rgba(0, 0, 0") and ("--fo-shadow" in line or "box-shadow" in line),
                        found.group(0),
                    )

    def test_decoration_stays_out(self):
        for name in ("fleetops.css", "fleet.css"):
            css = css_without_comments(static_source(name))
            with self.subTest(file=name):
                self.assertNotIn("text-shadow", css)
                self.assertNotIn("drop-shadow(", css)
                self.assertNotIn("blur(", css)
                self.assertNotRegex(css, r"radial-gradient|conic-gradient|repeating-\w+-gradient")
                # The only gradient is the flat row tint of fo-table-pin (a colour layer, not a gradient).
                self.assertEqual(len(re.findall(r"linear-gradient\(", css)), 1 if name == "fleetops.css" else 0)
                for value in re.findall(r"box-shadow:\s*([^;]+);", css):
                    self.assertRegex(value, r"^(none|var\(--fo-shadow[\w-]*\))$")

    def test_nothing_is_set_in_thin_or_tiny_type(self):
        for name in ("fleetops.css", "fleet.css"):
            css = css_without_comments(static_source(name))
            with self.subTest(file=name):
                self.assertNotRegex(css, r"font-weight:\s*(?:[123]00|lighter)\b")
                for size in re.findall(r"font-size:\s*([\d.]+)(px|rem|em)", css):
                    value = float(size[0])
                    self.assertGreaterEqual(value, 12 if size[1] == "px" else 0.75, size)


class StyleSheetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = static_source("fleetops.css")

    def test_the_sections_come_in_order_and_each_lane_has_its_block(self):
        sections = [self.css.index(f"/* ===== {number}. ") for number in range(1, 7)]
        self.assertEqual(sections, sorted(sections))
        for lane, title in zip(
            "ABCDEF",
            ("start page", "fleet page", "fleet records", "home and statistics", "SRP and administration", "pings and groups"),
        ):
            with self.subTest(lane=lane):
                start = self.css.index(f"/* 6.{lane} {title}")
                end = self.css.index(f"/* end 6.{lane} */")
                self.assertLess(start, end)
        self.assertEqual(len(re.findall(r"/\* end 6\.[A-F] \*/", self.css)), 6)

    def test_the_period_selector_rule_is_kept_byte_for_byte(self):
        self.assertIn(".fleetops-period-selector .input-group>.form-select{width:auto;flex:0 0 auto}", self.css)

    def test_every_component_has_rules(self):
        for selector in (
            ".fo-pill", ".fo-pill-ok", ".fo-pill-warn", ".fo-pill-bad", ".fo-pill-info", ".fo-pill-neutral", ".fo-pill-type",
            ".fo-pill-wrap", ".fo-pill-icon", ".fo-pill-type.fo-type-1", ".fo-pill-type.fo-type-6", ".fo-count", ".fo-count-nav",
            ".fo-metric", ".fo-metric-dot", ".fo-metric-value", ".fo-metric-title", ".fo-metric-sub", ".fo-seg",
            ".fo-live", ".fo-live-dot", ".fo-toasts", ".fo-toast", ".fo-toast-ok", ".fo-toast-warn", ".fo-toast-bad",
            ".fo-toast-info", ".fo-actionbar", ".fo-commandbar", ".fleetops-srp-paybar", ".fo-empty", ".fo-skeleton",
            ".fo-time", ".fo-time-rel", ".fo-bars", ".fo-bar-track", ".fo-bar-fill", ".fo-steps", ".fo-step", ".fo-timeline",
            ".fo-loss-card", ".fo-loss-grid", ".fo-render", ".fo-icon-btn", ".fo-row-actions", ".fo-sort", ".fo-ingame",
            ".fo-discord", ".fo-discord-mention", ".fo-card-warn", ".fo-card-info", ".fo-card-bad", ".fo-group-row",
            ".fo-list-row", ".fo-admin-card", ".fo-checklist", ".fo-divider-top", ".fo-divider-bottom", ".fo-frame",
            ".fo-table-pin", ".fo-eve-clock", ".fo-num", ".fo-sticky-top",
        ):
            with self.subTest(selector=selector):
                self.assertRegex(self.css, re.escape(selector) + r"[\s{,:.>\[)]")

    def test_the_status_pills_use_the_status_tokens(self):
        for tone in ("ok", "warn", "bad", "info"):
            self.assertIn(f".fo-pill-{tone} {{ color: var(--fo-{tone}); background: var(--fo-{tone}-bg); --fo-dot: var(--bs-", self.css)

    def test_the_type_scale(self):
        self.assertIn(".fleetops h2 { font-size: 1.75rem; font-weight: 600; }", self.css)
        self.assertRegex(self.css, r"@media \(max-width: 575\.98px\) \{ \.fleetops h2 \{ font-size: 1\.5rem; \} \}")
        self.assertIn(
            ".fleetops :is(.card-header, .card-title, .modal-title), #fo-confirm .modal-title { font-size: 1.0625rem; font-weight: 600; }",
            self.css,
        )
        self.assertIn(".fleetops .form-text { font-size: .875rem; }", self.css)
        self.assertIn(".fo-metric-value { font-size: clamp(2.25rem, 9vw, 2.75rem); font-weight: 500;", self.css)

    def test_numbers_are_tabular_everywhere_but_in_the_game_look(self):
        self.assertIn(".fleetops, #fo-toasts, .fo-toasts, #fo-confirm { font-variant-numeric: tabular-nums; }", self.css)
        # Buttons and fields reset the number style of their font; they take it from the page again.
        self.assertIn(".fleetops :is(button, input, select, textarea), #fo-toasts button, .fo-toasts button, #fo-confirm :is(button, input) { font-variant-numeric: inherit; }", self.css)
        self.assertIn(".fleetops .fo-ingame { font-variant-numeric: normal; }", self.css)

    def test_the_focus_ring_uses_its_token(self):
        self.assertIn("outline: 2px solid var(--fo-focus); outline-offset: 2px;", self.css)
        self.assertIn(".fleetops :focus-visible", self.css)
        self.assertIn("#fo-confirm :focus-visible", self.css)

    def test_reduced_motion_stops_transitions_and_animations_inside_and_outside_the_page(self):
        block = self.css[self.css.index("@media (prefers-reduced-motion: reduce)") :]
        block = block[: block.index("\n}\n")]
        for selector in (".fleetops *", ".fleetops *::before", ".fleetops *::after", ".fo-toasts *", "#fo-confirm *"):
            self.assertIn(selector, block)
        self.assertIn("transition: none !important; animation: none !important;", block)

    def test_every_animation_is_inside_the_reduced_motion_rule(self):
        # Pulses and the toast entry live in .fleetops or .fo-toasts, which the rule above covers.
        css = css_without_comments(self.css)
        for match in re.finditer(r"([^{}]+)\{[^{}]*\banimation:\s*fo-", css):
            self.assertRegex(match.group(1), r"\.fo-(?:live|skeleton|toast)")

    def test_alerts_become_callouts_with_page_coloured_text(self):
        self.assertIn(".fleetops .alert { --bs-alert-color: var(--fo-text-1);", self.css)
        for tone, token in (("danger", "bad"), ("warning", "warn"), ("info", "info"), ("success", "ok")):
            self.assertIn(f".fleetops .alert-{tone} {{ background: var(--fo-{token}-bg);", self.css)
        self.assertIn(".fleetops .alert a:not(.btn)", self.css)

    def test_field_errors_get_an_icon(self):
        self.assertIn('.fleetops .errorlist:not(.nonfield) li::before {', self.css)
        self.assertIn('content: "\\f06a"', self.css)
        self.assertIn(".fleetops .errorlist { list-style: none; padding: 0; margin: .25rem 0 0; color: var(--fo-bad); }", self.css)

    def test_buttons_are_readable_in_every_theme(self):
        for kind, colour in (("secondary", "--fo-text-1"), ("primary", "--fo-accent"), ("danger", "--fo-bad")):
            self.assertRegex(self.css, rf"\.fleetops \.btn-outline-{kind}, #fo-confirm \.btn-outline-{kind} \{{\s+--bs-btn-color: var\({colour}\)")
        # Materia writes colour and border of .btn itself, so both are set directly as well.
        self.assertIn("color: var(--bs-btn-color); border: var(--bs-border-width) solid var(--bs-btn-border-color);", self.css)
        self.assertRegex(self.css, r"\.fleetops \.btn-danger, #fo-confirm \.btn-danger \{[^}]*background-color: var\(--bs-btn-bg\); background-image: none;")

    def test_materia_primary_buttons_are_mixed_with_the_text_colour(self):
        self.assertRegex(
            self.css,
            r"\[data-theme=materia\] \.fleetops \.btn-primary, \[data-theme=materia\] #fo-confirm \.btn-primary \{[^}]*"
            r"--bs-btn-bg: color-mix\(in srgb, var\(--bs-primary\) 60%, var\(--bs-body-color\)\);[^}]*background-image: none;",
        )

    def test_bars_that_stay_on_screen_are_opaque(self):
        for selector in (".fo-actionbar", ".fo-commandbar", ".fleetops-srp-paybar", ".fo-toast"):
            self.assertRegex(self.css, re.escape(selector) + r" \{[^}]*background: var\(--fo-surface-solid\)")

    def test_toasts_sit_at_the_bottom(self):
        self.assertRegex(self.css, r"\.fo-toasts \{ position: fixed;[^}]*bottom: var\(--fo-space-3\);")
        self.assertRegex(self.css, r"@media \(min-width: 768px\) \{ \.fo-toasts \{ left: auto;")

    def test_touch_targets_are_44_pixels(self):
        block = self.css[self.css.index("@media (pointer: coarse)") :]
        self.assertIn("min-height: 44px", block)
        self.assertIn("min-width: 44px", block)

    def test_fields_check_rows_and_summaries_reach_44_pixels_on_touch_screens(self):
        block = self.css[self.css.index("@media (pointer: coarse)") :]
        self.assertRegex(block, r"\.fleetops :is\(\.form-select, \.form-control:not\([^)]*textarea[^)]*\)\) \{ min-height: 44px; \}")
        self.assertRegex(block, r"\.fleetops \.form-check-label \{[^}]*min-height: 44px; min-width: 44px;")
        self.assertRegex(block, r":where\(\.fleetops\) summary \{ min-height: 44px; \}")
        # A switch keeps its two-to-one shape next to the larger check boxes.
        self.assertRegex(block, r"\.fleetops \.form-switch \.form-check-input \{ width: 2\.8em;")
        self.assertRegex(block, r"\.fo-check-cell \{ min-width: 44px; min-height: 44px; \}")

    def test_placeholders_use_the_text_colour_of_the_field(self):
        self.assertRegex(self.css, r"\.fleetops :is\(\.form-control, \.form-select\)::placeholder \{ color: inherit; opacity: \.72; \}")

    def test_the_focus_ring_stays_outside_controls_that_have_a_fill(self):
        self.assertIn("outline: 2px solid var(--fo-focus); outline-offset: 2px;", self.css)
        self.assertIn(":focus-visible:not(.btn, .form-control, .form-select, .form-check-input) { outline-offset: -2px; }", self.css)

    def test_the_stylesheet_keeps_no_fixed_palette_colours(self):
        self.assertNotIn("#20c997", self.css)
        self.assertNotIn("#6f42c1", self.css)
        self.assertNotIn("#adb5bd", self.css)
        self.assertNotIn("#0d6efd", self.css)


class FleetStyleSheetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = static_source("fleet.css")

    def test_the_tree_uses_the_tokens(self):
        for token in ("--fo-border", "--fo-hover", "--fo-text-2", "--fo-accent", "--fo-accent-bg", "--fo-highlight-bg", "--fo-surface-2"):
            self.assertIn(f"var({token})", self.css)

    def test_empty_squads_keep_their_height_without_script(self):
        self.assertRegex(self.css, r"\.fleet-members-empty \{\s+min-height: 1\.75rem;")
        self.assertRegex(self.css, r"\.fleet-drop-empty:not\(\.fleet-drop-over\) \{\s+outline: 1px dashed var\(--fo-border\);")
        self.assertNotIn("#6c757d", self.css)

    def test_the_drag_label_waits_off_screen(self):
        block = self.css[self.css.index(".fleet-drag-label {") :]
        block = block[: block.index("}")]
        self.assertIn("position: fixed;", block)
        self.assertIn("top: -1000px;", block)
        self.assertIn("pointer-events: none;", block)
        self.assertIn("font-size: 0.8125rem;", block)

    def test_the_position_marker_is_not_smaller_than_twelve_pixels(self):
        block = self.css[self.css.index(".fleet-position {") :]
        block = block[: block.index("}")]
        self.assertIn("font-size: 0.75rem;", block)
        self.assertNotIn("0.7em", block)

    def test_the_boss_crown_keeps_its_gold(self):
        self.assertRegex(self.css, r"\.fleet-crown \{\s+color: #d4a017;")


class ScriptTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js = static_source("fleetops.js")

    def test_the_api_is_exported(self):
        block = self.js[self.js.index("window.FleetOps = {") :]
        for name in (
            "duration", "eveClock", "eveDate", "eveStamp", "roundedEveTime", "relativeText", "updateRelative",
            "updateCountdowns", "roleLabel", "detection", "toast", "confirm", "copyText",
        ):
            with self.subTest(name=name):
                self.assertRegex(block, rf"\b{name}\b")

    def test_the_members_of_the_earlier_api_keep_working(self):
        self.assertIn("updateCountdowns: updateRelative", self.js)
        self.assertIn("data.is_fleet_boss", self.js)
        self.assertIn("data.message", self.js)

    def test_the_script_can_be_loaded_twice(self):
        self.assertRegex(self.js, r"if \(window\.FleetOps && window\.FleetOps\.loaded\) \{\s+return;")
        self.assertIn("loaded: true", self.js)

    def test_a_confirmation_asks_with_the_text_of_the_button_that_was_pressed(self):
        self.assertIn("event.submitter", self.js)
        self.assertRegex(self.js, r"submitter && submitter\.dataset\.foConfirm \? submitter : form")
        self.assertIn("source.dataset.foConfirm", self.js)
        self.assertIn("form.requestSubmit(submitter || undefined)", self.js)
        # The first submit is stopped before the other scripts of the page see it.
        self.assertIn("event.stopImmediatePropagation()", self.js)
        self.assertRegex(self.js, r"\}, true\);\s+// --- Copy buttons")

    def test_a_dangerous_question_is_the_default_and_the_label_can_be_set(self):
        self.assertIn("source.dataset.foConfirmDanger !== 'false'", self.js)
        self.assertIn("source.dataset.foConfirmLabel", self.js)

    def test_the_confirmation_dialog_contract(self):
        for element in ("#fo-confirm-ok", "#fo-confirm-cancel", "#fo-confirm-text", "#fo-confirm-title"):
            self.assertIn(f"'{element}'", self.js)
        self.assertIn("document.getElementById('fo-confirm')", self.js)
        self.assertIn("window.confirm(String(text))", self.js, "without the dialog the browser asks")
        self.assertIn("shown.bs.modal", self.js)
        self.assertIn("hidden.bs.modal", self.js)

    def test_notifications(self):
        self.assertIn("const TOAST_SECONDS = 5;", self.js)
        self.assertIn("const MAX_TOASTS = 3;", self.js)
        self.assertIn("document.getElementById('fleet-flash') || document.getElementById('fo-toasts')", self.js)
        self.assertIn("tone === 'warn' || tone === 'bad' ? 'alert' : 'status'", self.js)
        self.assertIn("tone === 'ok' || tone === 'info'", self.js, "only success and information disappear by themselves")
        self.assertIn("fo-toast fo-toast-${tone}", self.js)

    def test_a_toast_link_stays_on_the_web(self):
        self.assertIn("url.protocol === 'http:' || url.protocol === 'https:'", self.js)

    def test_server_messages_are_adopted_and_notices_shown(self):
        self.assertIn("function adoptServerMessages()", self.js)
        self.assertIn("DOCUMENT_POSITION_FOLLOWING", self.js)
        self.assertIn("document.getElementById('fo-notices')", self.js)
        self.assertIn("JSON.parse(block.textContent)", self.js)

    def test_time_attributes(self):
        for attribute in ("data-countdown-utc", "data-relative-utc", "data-since-utc"):
            self.assertIn(attribute, self.js)
        self.assertIn("fo-eve-clock", self.js)

    def test_page_set_up(self):
        self.assertIn("--fo-sticky-top", self.js)
        self.assertIn("nav.navbar.fixed-top", self.js)
        self.assertIn("data-fo-dark", self.js)
        self.assertIn("addEventListener('DOMContentLoaded', start)", self.js)

    def test_the_list_filter_and_copy_buttons(self):
        self.assertIn("input[data-fo-filter", self.js)
        self.assertIn("[data-fo-filter-item]", self.js)
        self.assertIn("[data-fo-copy], .copy-btn[data-copy], .copy[data-id]", self.js)
        self.assertIn("gettext('Copied')", self.js)

    def test_scripts_never_put_text_into_the_page_as_markup(self):
        for name in ("fleetops.js", "preview.js"):
            source = static_source(name)
            with self.subTest(script=name):
                for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
                    self.assertNotIn(risky, source)

    def test_no_class_of_the_old_look_is_written(self):
        # The solid badges and buttons have been replaced by pills and outlines.
        forbidden = re.compile(r"\b(?:badge|text-bg-\w+|bg-(?:secondary|info|warning|success|danger|primary|light|dark)|text-dark|btn-close-white|btn-(?:success|warning|secondary))\b")
        self.assertIsNone(forbidden.search(self.js))

    def test_texts_come_from_the_catalog(self):
        # Every message the script asks for is one the catalog already knows (this foundation adds none).
        asked = set(extract.script_messages(extract.PACKAGE / "static" / "fleetops" / "fleetops.js"))
        self.assertLessEqual(asked, extract.catalog_keys("djangojs"))

    def test_the_preview_stub_offers_the_agreed_functions(self):
        source = static_source("preview.js")
        self.assertIn("window.FleetOpsPreview = { discord, motd, motdColour };", source)
        self.assertIn("textContent", source)


class RelativeWindowTests(SimpleTestCase):
    def test_a_week_old_fleet_is_not_shown_relative(self):
        # The script leaves relative words empty beyond two days; the tag always writes the span.
        old = datetime.now(UTC) - timedelta(days=7)

        self.assertIn('data-relative-utc="', render("{% eve_when value %}", value=old))
        self.assertIn("const RELATIVE_WINDOW_MINUTES = 48 * 60;", static_source("fleetops.js"))


TEMPLATES = Path(__file__).resolve().parents[1] / "fleetops" / "templates" / "fleetops"


class BasePageTests(TestCase):
    """What the base page carries for every FleetOps page, exactly once."""

    def setUp(self):
        self.user = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(self.user)

    def page(self, name="fleetops:dashboard"):
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_toast_stack_and_the_dialog_are_written_once(self):
        html = self.page()

        self.assertEqual(html.count('id="fo-toasts"'), 1)
        self.assertIn('<div id="fo-toasts" class="fo-toasts" aria-live="polite"></div>', html)
        for element in ("fo-confirm", "fo-confirm-title", "fo-confirm-text", "fo-confirm-cancel", "fo-confirm-ok"):
            with self.subTest(element=element):
                self.assertEqual(html.count(f'id="{element}"'), 1)

    def test_every_page_carries_the_scripts_and_the_style_sheet_once(self):
        for name in ("fleetops:dashboard", "fleetops:start_fleet", "fleetops:manual_fleet", "fleetops:my_statistics"):
            with self.subTest(page=name):
                html = self.page(name)
                self.assertEqual(html.count("fleetops/fleetops.js"), 1)
                self.assertEqual(html.count("fleetops/fleetops.css"), 1)
                self.assertEqual(html.count("fleetops/jsi18n"), 1)

    def test_the_style_sheet_is_in_the_head_and_the_scripts_come_before_the_page(self):
        html = self.page()

        self.assertLess(html.index("fleetops/fleetops.css"), html.index("</head>"))
        order = [
            html.index("fleetops/jsi18n"),
            html.index("window.gettext = s => s;"),
            html.index("fleetops/fleetops.js"),
            html.index('id="fo-toasts"'),
            html.index('id="fo-confirm"'),
            html.index('class="container-fluid py-3 fleetops"'),
        ]
        self.assertEqual(order, sorted(order))

    def test_only_the_base_page_loads_the_script_of_the_foundation(self):
        # The pages that used to load it themselves rely on the base page now; a second copy does no harm
        # (the script returns at once) but it would be a request for nothing.
        for path in TEMPLATES.rglob("*.html"):
            name = path.relative_to(TEMPLATES).as_posix()
            with self.subTest(template=name):
                source = path.read_text()
                if name == "base.html":
                    self.assertIn("fleetops/fleetops.js", source)
                    self.assertIn("fleetops/fleetops.css", source)
                else:
                    self.assertNotIn("fleetops/fleetops.js", source)
                    self.assertNotIn("fleetops/fleetops.css", source)

    def test_the_brand_shows_the_eve_clock(self):
        now = datetime(2026, 10, 16, 13, 5, 41, tzinfo=UTC)

        with mock.patch("fleetops.templatetags.fleetops_messages.timezone.now", return_value=now):
            html = self.page()

        self.assertIn('FleetOps <span class="fo-eve-clock" id="fo-eve-clock">13:05 EVE</span>', html)
        self.assertEqual(html.count('id="fo-eve-clock"'), 1)
        self.assertLess(html.index('class="navbar-brand"'), html.index('id="fo-eve-clock"'))

    def test_a_notice_of_the_session_is_written_once(self):
        session = self.client.session
        request = RequestFactory().get("/")
        request.session = session
        ui_notices.push_notice(request, "Marked paid.", "success", "Undo payment…", "/fleetops/srp/claims/5/#undo-payment")
        session.save()

        first = self.page()
        second = self.page()

        self.assertEqual(first.count('id="fo-notices"'), 1)
        block = re.search(r'<script id="fo-notices" type="application/json">(.*?)</script>', first, re.S)
        self.assertEqual(
            json.loads(block.group(1)),
            [
                {
                    "text": "Marked paid.",
                    "level": "success",
                    "action_label": "Undo payment…",
                    "action_url": "/fleetops/srp/claims/5/#undo-payment",
                }
            ],
        )
        self.assertNotIn('id="fo-notices"', second)

    def test_no_notice_block_without_notices(self):
        self.assertNotIn('id="fo-notices"', self.page())

    def test_the_srp_counts_use_the_count_and_pill_looks(self):
        nav = {"enabled": True, "request": True, "review": True, "pending_count": 3, "summary": False, "visible": True}

        with mock.patch("fleetops.views.srp_access.srp_nav", return_value=nav):
            html = self.page()

        self.assertIn('SRP <span class="fo-count fo-count-nav">3</span></a>', html)
        self.assertIn('Review <span class="fo-pill fo-pill-warn">3</span></a>', html)
        self.assertNotIn('class="badge', html.split('class="container-fluid py-3 fleetops"')[0].split("<body>")[1])

    def test_the_rarely_used_pages_sit_in_the_more_menu(self):
        admin = f.create_user(perms=f.FC_PERMS + ["fleetops.manage_configuration", "fleetops.view_audit_log"])
        self.client.force_login(admin)

        html = self.page()
        menu = html.split('id="fo-nav-more"')[1].split("</ul>")[0]

        for label in ("Add Manual Fleet", "Administration", "Audit"):
            self.assertIn(f">{label}</a>", menu)
            self.assertNotIn(f'class="nav-link" href="{reverse("fleetops:configuration_index")}"', html)

    def test_a_user_without_those_pages_has_no_more_menu(self):
        plain = f.create_user(perms=["fleetops.basic_access"])
        self.client.force_login(plain)

        self.assertNotIn('id="fo-nav-more"', self.page())

    def test_a_pill_with_a_sentence_is_not_a_capsule(self):
        css = (Path(__file__).resolve().parent.parent / "fleetops/static/fleetops/fleetops.css").read_text()
        rule = re.search(r"^\.fo-pill-wrap \{[^}]*\}", css, re.M).group(0)

        self.assertIn("border-radius: var(--fo-radius-sm)", rule)

    def test_the_navbar_has_no_count_without_pending_claims(self):
        nav = {"enabled": True, "request": True, "review": True, "pending_count": 0, "summary": False, "visible": True}

        with mock.patch("fleetops.views.srp_access.srp_nav", return_value=nav):
            html = self.page()

        self.assertNotIn("fo-count-nav", html)
        self.assertNotIn("fo-pill-warn", html)

    def test_the_approval_counts_use_the_count_look(self):
        lead = f.create_user(perms=f.FC_PERMS + ["fleetops.manage_fleets"])
        f.create_operation(
            self.user, status=FleetOperation.Status.CLOSED, is_manual=True, review_status=ReviewStatus.PENDING
        )
        self.client.force_login(lead)

        html = self.page()

        self.assertRegex(
            html,
            r'Approvals <span class="fo-count fo-count-nav" title="Fleets and PAP rows waiting for approval\.">'
            r'<span class="fo-count-long">1 fleet</span><span class="fo-count-short" aria-hidden="true">1</span></span></a>',
        )
        self.assertNotIn("fo-approval-count", html)


class ConfirmModalTests(SimpleTestCase):
    """The dialog that FleetOps.confirm fills: the ids are the contract."""

    def setUp(self):
        # The texts are translated; another test may have left a different language active.
        english = translation.override("en")
        english.__enter__()
        self.addCleanup(english.__exit__, None, None, None)
        self.html = render_to_string("fleetops/partials/confirm_modal.html")

    def test_the_ids_of_the_contract(self):
        for element in ("fo-confirm", "fo-confirm-title", "fo-confirm-text", "fo-confirm-cancel", "fo-confirm-ok"):
            with self.subTest(element=element):
                self.assertEqual(self.html.count(f'id="{element}"'), 1)

    def test_the_dialog_is_a_labelled_hidden_modal(self):
        self.assertIn('class="modal fade" id="fo-confirm" tabindex="-1" aria-labelledby="fo-confirm-title" aria-hidden="true"', self.html)
        self.assertIn('<h5 class="modal-title" id="fo-confirm-title">Please confirm</h5>', self.html)
        self.assertIn('<p class="mb-0" id="fo-confirm-text"></p>', self.html)

    def test_the_buttons(self):
        self.assertIn('class="btn btn-outline-secondary" id="fo-confirm-cancel" data-bs-dismiss="modal">Cancel</button>', self.html)
        self.assertIn('class="btn btn-primary" id="fo-confirm-ok">Confirm</button>', self.html)
        self.assertIn('class="btn-close" data-bs-dismiss="modal" aria-label="Close"', self.html)

    def test_the_texts_are_in_the_catalog(self):
        # Four words the catalog has had since the fleet page; the dialog adds no message of its own.
        keys = extract.catalog_keys("django")
        for text in ("Please confirm", "Close", "Cancel", "Confirm"):
            with self.subTest(text=text):
                self.assertIn(text, keys)


class DiscordPreviewPartialTests(SimpleTestCase):
    def setUp(self):
        english = translation.override("en")
        english.__enter__()
        self.addCleanup(english.__exit__, None, None, None)

    def render(self, prefix):
        return render_to_string("fleetops/partials/discord_preview.html", {"prefix": prefix})

    def test_the_elements_of_the_contract(self):
        html = self.render("ping")

        for element in ("ping-box", "ping-look", "ping-preview", "ping-length", "ping-view-look", "ping-view-raw"):
            with self.subTest(element=element):
                self.assertEqual(html.count(f'id="{element}"'), 1)
        self.assertIn('<pre class="preview-box mb-0" id="ping-preview" hidden>', html)
        self.assertIn('<div class="fo-discord" id="ping-look"></div>', html)

    def test_look_and_raw_text_are_a_segmented_choice_with_the_look_first(self):
        html = self.render("ping")

        self.assertIn('class="fo-seg" role="radiogroup"', html)
        self.assertRegex(html, r'id="ping-view-look" value="look" checked>\s*<label for="ping-view-look">Look</label>')
        self.assertRegex(html, r'id="ping-view-raw" value="raw">\s*<label for="ping-view-raw">Raw text</label>')

    def test_two_previews_on_one_page_do_not_share_ids(self):
        ping, motd = self.render("ping"), self.render("motd")

        self.assertTrue(set(re.findall(r'id="([^"]+)"', ping)).isdisjoint(re.findall(r'id="([^"]+)"', motd)))

    def test_the_script_switches_the_two_panes(self):
        source = static_source("preview.js")

        self.assertIn("addEventListener('change'", source)
        self.assertIn("closest('[data-fo-preview]')", source)
        self.assertIn("look.hidden = input.value === 'raw'", source)
        self.assertIn("raw.hidden = input.value !== 'raw'", source)
