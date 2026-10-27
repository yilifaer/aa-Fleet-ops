# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The shared statistics template parts: status badges, corporation tabs and export links."""

import html
import re
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.http import HttpResponse
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.timezone import now as timezone_now

from fleetops.services import assessment
from fleetops.templatetags.fleetops_stats import status_badge_class, status_row_class

from . import factories as f

STATUS_BADGES = {
    "met": ("fo-pill-ok", "Target met"),
    "missed": ("fo-pill-bad", "Below target"),
    "in_progress": ("fo-pill-warn", "Not reached yet"),
    "no_members": ("fo-pill-neutral", "No members"),
    "ok": ("fo-pill-ok", "OK"),
    "below": ("fo-pill-bad", "Below minimum"),
    "below_so_far": ("fo-pill-warn", "Below minimum so far"),
}


def badge(**context):
    return render_to_string("fleetops/statistics/_status_badge.html", context)


class StatusBadgeTests(SimpleTestCase):
    def test_every_status_has_a_colour_and_words(self):
        for status, (css_class, label) in STATUS_BADGES.items():
            with self.subTest(status=status):
                page = badge(status=status)
                self.assertIn(f'class="fo-pill {css_class}"', page)
                # Colour is never the only signal.
                self.assertIn(f">{label}</span>", page)

    def test_labels_come_from_the_assessment_service(self):
        for status in STATUS_BADGES:
            self.assertEqual(STATUS_BADGES[status][1], assessment.STATUS_LABELS[status])

    def test_status_without_words_shows_a_dash(self):
        for status in ("none", "", None):
            with self.subTest(status=status):
                page = badge(status=status)
                self.assertNotIn("fo-pill", page)
                self.assertIn("&mdash;", page)

    def test_label_can_be_given(self):
        page = badge(status="none", label="No target")

        self.assertIn('class="fo-pill fo-pill-neutral">No target</span>', page)
        self.assertIn(">Custom</span>", badge(status="met", label="Custom"))

    def test_label_is_escaped(self):
        self.assertNotIn("<b>", badge(status="met", label="<b>x</b>"))

    def test_row_classes_follow_the_colours(self):
        self.assertEqual(status_row_class("met"), "fleetops-status-row-met")
        self.assertEqual(status_row_class("ok"), "fleetops-status-row-met")
        self.assertEqual(status_row_class("missed"), "fleetops-status-row-missed")
        self.assertEqual(status_row_class("below"), "fleetops-status-row-missed")
        self.assertEqual(status_row_class("in_progress"), "fleetops-status-row-progress")
        self.assertEqual(status_row_class("below_so_far"), "fleetops-status-row-progress")
        for status in ("none", "no_members", "", None, "unknown"):
            with self.subTest(status=status):
                self.assertEqual(status_row_class(status), "")
        self.assertEqual(status_badge_class("unknown"), "fo-pill fo-pill-neutral")

    def test_filters_work_in_a_template(self):
        template = Template('{% load fleetops_stats %}{{ s|status_label }}|{{ s|status_badge_class }}|{{ s|status_row_class }}')

        self.assertEqual(
            template.render(Context({"s": "in_progress"})),
            "Not reached yet|fo-pill fo-pill-warn|fleetops-status-row-progress",
        )


class CorporationTabsTests(TestCase):
    def tabs(self, **context):
        context.setdefault("year", 2026)
        context.setdefault("month", 3)
        return html.unescape(render_to_string("fleetops/statistics/_tabs.html", context))

    def test_own_corporation_tabs(self):
        page = self.tabs(active_tab="members", tab_corporation_id=None, leaderboard_visible=True)

        for name, query in (
            ("corporation_statistics", "?year=2026&month=3"),
            ("corporation_members", "?year=2026&month=3"),
            ("corporation_members", "?year=2026&month=3&status=inactive"),
            ("leaderboard", "?year=2026&month=3"),
            ("attendance_history_corporation", ""),
        ):
            with self.subTest(name=name, query=query):
                self.assertIn(f'href="{reverse(f"fleetops:{name}")}{query}"', page)

    def test_tabs_of_another_corporation_use_its_id(self):
        page = self.tabs(active_tab="overview", tab_corporation_id=98000001, leaderboard_visible=True)

        for name, query in (
            ("corporation_statistics_detail", "?year=2026&month=3"),
            ("corporation_members_detail", "?year=2026&month=3"),
            ("corporation_members_detail", "?year=2026&month=3&status=inactive"),
        ):
            with self.subTest(name=name, query=query):
                self.assertIn(f'href="{reverse(f"fleetops:{name}", args=[98000001])}{query}"', page)
        self.assertIn(f'href="{reverse("fleetops:leaderboard")}?year=2026&month=3&corporation=98000001"', page)
        # Only the own corporation has a PAP history page.
        self.assertNotIn(reverse("fleetops:attendance_history_corporation"), page)

    def test_the_leaderboard_tab_follows_the_visibility(self):
        page = self.tabs(active_tab="overview", tab_corporation_id=None, leaderboard_visible=False)

        self.assertNotIn(reverse("fleetops:leaderboard"), page)

    def test_the_active_tab_is_marked(self):
        for tab, label in (
            ("overview", "Overview"),
            ("members", "Members"),
            ("inactive", "Inactive"),
            ("leaderboard", "Leaderboard"),
            ("history", "History"),
        ):
            with self.subTest(tab=tab):
                page = self.tabs(active_tab=tab, tab_corporation_id=None, leaderboard_visible=True)
                self.assertEqual(page.count("aria-current"), 1)
                link = re.search(rf"<a [^>]*>{label}</a>", page).group(0)
                self.assertIn('aria-current="page"', link)
                self.assertIn("nav-link active", link)


class ExportButtonTests(TestCase):
    def buttons(self, query="", **context):
        request = RequestFactory().get(f"/fleetops/statistics/members/{query}")
        context.setdefault("dataset", "members")
        return render_to_string("fleetops/statistics/_export_buttons.html", context, request=request)

    @staticmethod
    def links(page):
        return {
            label: urlsplit(html.unescape(href))
            for href, label in re.findall(r'<a class="btn btn-outline-secondary" href="([^"]*)">([^<]*)</a>', page)
        }

    def test_excel_and_csv_links_to_the_dataset(self):
        links = self.links(self.buttons())

        self.assertEqual(set(links), {"Excel", "CSV"})
        for url in links.values():
            self.assertEqual(url.path, reverse("fleetops:export", args=["members"]))
        self.assertEqual(parse_qs(links["Excel"].query), {"format": ["xlsx"]})
        self.assertEqual(parse_qs(links["CSV"].query), {"format": ["csv"]})

    def test_the_page_filters_are_passed_on_without_the_page_number(self):
        links = self.links(self.buttons("?corporation=5&status=inactive&q=a%26b&sort=pap&page=3&year=2025&month=7"))

        self.assertEqual(
            parse_qs(links["Excel"].query),
            {
                "corporation": ["5"],
                "status": ["inactive"],
                "q": ["a&b"],
                "sort": ["pap"],
                "year": ["2025"],
                "month": ["7"],
                "format": ["xlsx"],
            },
        )

    def test_the_month_of_the_page_is_added_when_the_url_has_none(self):
        links = self.links(self.buttons(year=2026, month=3))

        self.assertEqual(parse_qs(links["CSV"].query), {"year": ["2026"], "month": ["3"], "format": ["csv"]})

    def test_the_month_in_the_url_wins_over_the_context(self):
        links = self.links(self.buttons("?year=2025&month=7", year=2026, month=3))

        self.assertEqual(parse_qs(links["CSV"].query)["year"], ["2025"])
        self.assertEqual(parse_qs(links["CSV"].query)["month"], ["7"])

    def test_params_override_and_remove_query_values(self):
        links = self.links(
            self.buttons(
                "?status=inactive&format=pdf&user=1",
                dataset="member-fleets",
                export_params={"user": 42, "status": None, "q": "", "scope": "alliance"},
            )
        )

        self.assertEqual(links["Excel"].path, reverse("fleetops:export", args=["member-fleets"]))
        self.assertEqual(parse_qs(links["Excel"].query), {"user": ["42"], "scope": ["alliance"], "format": ["xlsx"]})

    def test_values_are_escaped_in_the_page(self):
        page = self.buttons('?q="><script>x</script>')

        self.assertNotIn("<script>x</script>", page)


class ExportViewTests(TestCase):
    """The export route hands the dataset and format to the export service after the access check."""

    def setUp(self):
        self.url = reverse("fleetops:export", args=["corporations"])

    def test_the_request_goes_to_the_export_service(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        with mock.patch("fleetops.views_exports.export_for_request", return_value=HttpResponse("file")) as export:
            response = self.client.get(self.url, {"format": "xlsx"})

        self.assertEqual(response.content, b"file")
        request, dataset, fmt = export.call_args.args
        self.assertEqual((request.user, dataset, fmt), (lead, "corporations", "xlsx"))

    def test_a_missing_format_is_passed_on_empty(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        with mock.patch("fleetops.views_exports.export_for_request", return_value=HttpResponse("file")) as export:
            self.client.get(self.url)

        self.assertEqual(export.call_args.args[2], "")

    def test_only_get_is_allowed(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        with mock.patch("fleetops.views_exports.export_for_request") as export:
            for method in ("post", "put", "delete", "patch"):
                with self.subTest(method=method):
                    self.assertEqual(getattr(self.client, method)(self.url, {"format": "csv"}).status_code, 405)
        export.assert_not_called()

    def test_fleetops_access_is_needed_for_every_dataset(self):
        stranger = f.create_user("stranger")
        self.client.force_login(stranger)

        with mock.patch("fleetops.views_exports.export_for_request") as export:
            response = self.client.get(
                reverse("fleetops:export", args=["member-fleets"]), {"format": "csv", "user": stranger.pk}
            )

        self.assertEqual(response.status_code, 403)
        export.assert_not_called()

    def test_csv_and_excel_downloads(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        csv = self.client.get(self.url, {"format": "csv"})
        xlsx = self.client.get(self.url, {"format": "xlsx"})

        self.assertEqual((csv.status_code, xlsx.status_code), (200, 200))
        self.assertEqual(csv["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", csv["Content-Disposition"])
        self.assertTrue(xlsx.content.startswith(b"PK"))

    def test_formats_are_csv_and_xlsx_only(self):
        lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(lead)

        for params in ({}, {"format": ""}, {"format": "pdf"}, {"format": "CSV"}, {"format": "csv,xlsx"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params).status_code, 404)


class EmptyStateAndMonthNavigationTests(TestCase):
    """Pages without rows say so with an icon (UD4.7); the month neighbours are a segmented control."""

    def setUp(self):
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.client.force_login(self.lead)

    def get(self, name, *args, **params):
        response = self.client.get(reverse(f"fleetops:{name}", args=args), params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_empty_pages_use_the_empty_state(self):
        for name, sentence in (
            ("attendance_history_me", "No PAP records for 2026-03."),
            ("all_fc_statistics", "No FC fleets in this month."),
            ("my_statistics", "No PAP this month."),
            ("leaderboard", "Nobody has PAP in this month yet."),
        ):
            with self.subTest(page=name):
                page = self.get(name, year=2026, month=3)
                self.assertRegex(page, r'<div class="fo-empty">(?:(?!</div>).)*' + re.escape(sentence), re.S)
                self.assertNotIn("p-4", page.split('id="fo-toasts"')[-1])

    def test_the_month_neighbours_are_a_segmented_control_with_the_month_between(self):
        page = self.get("my_statistics", year=2026, month=3)

        nav = re.search(r'<nav class="fo-seg fo-period-nav".*?</nav>', page, re.S).group(0)
        self.assertRegex(nav, r'aria-label="Previous month: 2026-02"')
        self.assertRegex(nav, r'aria-label="Next month: 2026-04"')
        self.assertIn('<span class="fo-period-current" aria-current="true">2026-03</span>', nav)
        self.assertNotIn("btn", nav)

    def test_a_month_without_a_next_month_has_a_disabled_end_and_no_next_link(self):
        current = f"{timezone_now().year:04d}"
        page = self.get("my_statistics")

        nav = re.search(r'<nav class="fo-seg fo-period-nav".*?</nav>', page, re.S).group(0)
        self.assertNotIn("Next month", nav)
        self.assertIn('<span aria-disabled="true" aria-hidden="true">&rsaquo;</span>', nav)
        self.assertIn(current, nav)

    def test_the_selector_keeps_its_form_and_ids(self):
        page = self.get("my_statistics", year=2026, month=3)

        for marker in (
            'class="row g-2 align-items-end fleetops-period-selector mb-2"',
            'id="fleetops-period-year"',
            'id="fleetops-period-month"',
            '<div class="input-group">',
            "View Period",
        ):
            self.assertIn(marker, page)

    def test_the_history_page_shows_the_neighbours_only_for_a_chosen_month(self):
        everything = self.get("attendance_history_me")
        chosen = self.get("attendance_history_me", year=2026, month=3)

        self.assertNotIn("fo-period-nav", everything)
        self.assertIn("fo-period-nav", chosen)
        self.assertIn("2026-03</span>", chosen)
