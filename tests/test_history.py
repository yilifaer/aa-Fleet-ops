# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The PAP history pages: month filter, paging that keeps the filter, EVE time and the export links."""

import csv
import html
import io
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from fleetops.services.attendance_history import SCOPES, history_month, history_queryset
from fleetops.services.history import month_retention_days
from fleetops.services.periods import current_year_month, month_ref, shift_month

from . import factories as f

UTC = dt_timezone.utc
PAGE_SIZE = "fleetops.views_history.HISTORY_PAGE_SIZE"
ALL_STATS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
HISTORY_PAGES = {
    "me": "attendance_history_me",
    "corporation": "attendance_history_corporation",
    "alliance": "attendance_history_alliance",
}


def months_ago(count):
    return shift_month(*current_year_month(), -count)


def at(year_month, day=10, hour=12, minute=0):
    return datetime(year_month[0], year_month[1], day, hour, minute, tzinfo=UTC)


def last_day(year_month):
    next_year, next_month = shift_month(*year_month, 1)
    return datetime(next_year, next_month, 1, tzinfo=UTC) - timedelta(days=1)


def hrefs(page):
    return [html.unescape(href) for href in re.findall(r'href="([^"]*)"', page)]


def query_of(href):
    return {key: values for key, values in parse_qs(urlsplit(href).query).items()}


class HistoryTestCase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("pilot", perms=f.CORP_MANAGEMENT_PERMS)
        self.neighbour = f.create_user("neighbour", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        self.lead = f.create_user("lead", perms=ALL_STATS)
        self.last_month = months_ago(1)
        self.earlier_month = months_ago(2)

    def fleet(self, started_at, *pilots, value=1, **extra):
        operation = f.closed_operation(self.fc, started_at, **extra)
        for pilot in pilots:
            f.add_attendance(operation, pilot, value=value)
        return operation

    def get(self, user, scope="me", **params):
        self.client.force_login(user)
        response = self.client.get(reverse(f"fleetops:{HISTORY_PAGES[scope]}"), params)
        self.assertEqual(response.status_code, 200)
        return response

    @staticmethod
    def row_operations(response):
        return [row.operation_id for row in response.context["rows"]]


class MonthParameterTests(TestCase):
    def test_a_month_needs_a_valid_year_and_month(self):
        self.assertEqual(history_month({"year": "2026", "month": "3"}), (2026, 3))
        self.assertEqual(history_month({"year": " 2026 ", "month": "03"}), (2026, 3))
        for params in (
            {},
            {"year": "2026"},
            {"month": "3"},
            {"year": "2026", "month": ""},
            {"year": "", "month": ""},
            {"year": "abc", "month": "3"},
            {"year": "2026", "month": "13"},
            {"year": "2026", "month": "0"},
            {"year": "1999", "month": "3"},
            {"year": "2201", "month": "3"},
            {"year": "20.5", "month": "3"},
        ):
            with self.subTest(params=params):
                self.assertIsNone(history_month(params))


class HistoryQuerysetTests(HistoryTestCase):
    def test_scopes(self):
        self.assertEqual(SCOPES, ("me", "corporation", "alliance"))

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            history_queryset("world")

    def test_personal_and_corporation_history_need_their_subject(self):
        # Without the user or corporation the rows of everybody would be returned.
        with self.assertRaises(ValueError):
            history_queryset("me")
        with self.assertRaises(ValueError):
            history_queryset("corporation")

    def test_scope_and_month(self):
        in_month = self.fleet(at(self.last_month), self.pilot, self.neighbour)
        elsewhere = self.fleet(at(self.earlier_month), self.pilot)

        def operations(scope, **kwargs):
            return sorted(row.operation_id for row in history_queryset(scope, **kwargs))

        self.assertEqual(operations("me", user=self.pilot), sorted([in_month.pk, elsewhere.pk]))
        self.assertEqual(operations("me", user=self.pilot, month=self.last_month), [in_month.pk])
        self.assertEqual(operations("corporation", corporation_id=f.DEFAULT_CORP[0]), sorted([in_month.pk, elsewhere.pk]))
        self.assertEqual(operations("corporation", corporation_id=f.OTHER_CORP[0], month=self.last_month), [in_month.pk])
        self.assertEqual(operations("alliance"), sorted([in_month.pk, in_month.pk, elsewhere.pk]))
        self.assertEqual(operations("alliance", month=self.earlier_month), [elsewhere.pk])

    def test_newest_fleet_first(self):
        old = self.fleet(at(self.earlier_month), self.pilot)
        new = self.fleet(at(self.last_month), self.pilot)

        rows = list(history_queryset("me", user=self.pilot))

        self.assertEqual([row.operation_id for row in rows], [new.pk, old.pk])


class MonthFilterTests(HistoryTestCase):
    def setUp(self):
        super().setUp()
        self.recent = self.fleet(at(self.last_month), self.pilot, self.neighbour, value=2)
        self.old = self.fleet(at(self.earlier_month), self.pilot, value=3)

    def test_the_whole_retention_window_by_default(self):
        response = self.get(self.pilot)

        self.assertCountEqual(self.row_operations(response), [self.recent.pk, self.old.pk])
        self.assertEqual(response.context["total"], 5)
        self.assertIsNone(response.context["selected_month"])
        self.assertNotIn("year", response.context)
        self.assertNotIn("month", response.context)
        self.assertContains(response, "Showing everything within the retention period")

    def test_a_month_narrows_the_list_and_the_total(self):
        year, month = self.last_month
        response = self.get(self.pilot, year=year, month=month)

        self.assertEqual(self.row_operations(response), [self.recent.pk])
        self.assertEqual(response.context["total"], 2)
        self.assertEqual((response.context["year"], response.context["month"]), self.last_month)
        self.assertEqual(response.context["period_label"], f"{year:04d}-{month:02d}")
        self.assertContains(response, f"Showing the fleets that started in {year:04d}-{month:02d}.")

    def test_the_month_applies_to_every_history_page(self):
        year, month = self.last_month
        for scope, user, expected in (
            ("me", self.pilot, [self.recent.pk]),
            ("corporation", self.pilot, [self.recent.pk]),
            ("alliance", self.lead, [self.recent.pk, self.recent.pk]),
        ):
            with self.subTest(scope=scope):
                self.assertEqual(self.row_operations(self.get(user, scope, year=year, month=month)), expected)

    def test_a_month_without_fleets_is_empty(self):
        year, month = months_ago(5)
        response = self.get(self.pilot, year=year, month=month)

        self.assertEqual(self.row_operations(response), [])
        self.assertEqual(response.context["total"], 0)
        self.assertContains(response, f"No PAP records for {year:04d}-{month:02d}.")

    def test_unusable_values_show_everything(self):
        for params in ({"year": "2026"}, {"month": "3"}, {"year": "x", "month": "y"}, {"year": "2026", "month": "13"}, {"year": "2026", "month": ""}):
            with self.subTest(params=params):
                response = self.get(self.pilot, **params)
                self.assertEqual(len(self.row_operations(response)), 2)
                self.assertIsNone(response.context["selected_month"])

    def test_months_are_eve_months(self):
        # 23:30 EVE on the last day is already the next morning in Shanghai, 00:00 EVE is 08:00 there.
        before = self.fleet(last_day(self.last_month).replace(hour=23, minute=30), self.pilot, value=1)
        after_year, after_month = shift_month(*self.last_month, 1)
        after = self.fleet(datetime(after_year, after_month, 1, 0, 0, tzinfo=UTC), self.pilot, value=1)
        with override_settings(TIME_ZONE="Asia/Shanghai"):
            in_last = self.row_operations(self.get(self.pilot, year=self.last_month[0], month=self.last_month[1]))
            in_next = self.row_operations(self.get(self.pilot, year=after_year, month=after_month))

        self.assertIn(before.pk, in_last)
        self.assertNotIn(after.pk, in_last)
        self.assertIn(after.pk, in_next)
        self.assertNotIn(before.pk, in_next)

    def test_times_are_shown_in_eve_time(self):
        late = self.fleet(last_day(self.last_month).replace(hour=23, minute=30), self.pilot)
        local = late.started_at.astimezone(dt_timezone(timedelta(hours=8)))

        with override_settings(TIME_ZONE="Asia/Shanghai"):
            response = self.get(self.pilot)

        self.assertContains(response, "<th>EVE Time</th>")
        self.assertContains(response, late.started_at.strftime("%Y-%m-%d %H:%M"))
        self.assertNotContains(response, local.strftime("%Y-%m-%d %H:%M"))

    def test_the_selector_lists_years_months_and_all_retained(self):
        year, month = self.last_month
        page = self.get(self.pilot, year=year, month=month).content.decode()

        self.assertIn(f'<option value="{year}" selected>{year}</option>', page)
        self.assertIn(f'<option value="{month}" selected>', page)
        self.assertIn('<option value="" >All retained</option>', page)
        self.assertIn("Month (EVE time)", page)

    def test_all_retained_is_selected_without_a_month(self):
        page = self.get(self.pilot).content.decode()

        self.assertIn('<option value="" selected>All retained</option>', page)

    def test_previous_and_next_month_links_keep_the_year_and_month_only(self):
        year, month = self.earlier_month
        page = self.get(self.pilot, year=year, month=month, page=1).content.decode()
        previous = shift_month(year, month, -1)
        following = shift_month(year, month, 1)

        links = {query_of(link).get("month", [None])[0]: query_of(link) for link in hrefs(page) if "month=" in link}
        self.assertEqual(links[str(previous[1])], {"year": [str(previous[0])], "month": [str(previous[1])]})
        self.assertEqual(links[str(following[1])], {"year": [str(following[0])], "month": [str(following[1])]})
        self.assertIn("Previous month", page)
        self.assertIn("Next month", page)

    def test_there_is_no_next_link_for_the_running_month(self):
        year, month = current_year_month()
        page = self.get(self.pilot, year=year, month=month).content.decode()

        self.assertIn("Previous month", page)
        self.assertNotIn("Next month", page)

    def test_there_are_no_neighbour_links_without_a_month(self):
        page = self.get(self.pilot).content.decode()

        self.assertNotIn("Previous month", page)
        self.assertNotIn("Next month", page)

    def test_months_older_than_the_retention_window_may_be_incomplete(self):
        f.settings(data_retention_days=365)
        old_year, old_month = months_ago(13)
        recent_year, recent_month = self.last_month

        old_page = self.get(self.pilot, year=old_year, month=old_month)
        recent_page = self.get(self.pilot, year=recent_year, month=recent_month)

        self.assertContains(old_page, "so this month may be incomplete")
        self.assertNotContains(recent_page, "may be incomplete")


@mock.patch(PAGE_SIZE, 2)
class PagingTests(HistoryTestCase):
    def setUp(self):
        super().setUp()
        for day in range(1, 6):
            self.fleet(at(self.last_month, day=day), self.pilot)
        self.fleet(at(self.earlier_month), self.pilot)

    def test_paging_links_keep_the_month(self):
        year, month = self.last_month
        page = self.get(self.pilot, year=year, month=month).content.decode()

        self.assertIn("Page 1 of 3", page)
        links = [query_of(link) for link in hrefs(page) if "page=" in link]
        self.assertTrue(links)
        for query in links:
            self.assertEqual((query["year"], query["month"]), ([str(year)], [str(month)]))
        self.assertIn({"year": [str(year)], "month": [str(month)], "page": ["2"]}, links)
        self.assertIn({"year": [str(year)], "month": [str(month)], "page": ["3"]}, links)

    def test_later_pages_keep_the_month_and_every_row_appears_once(self):
        year, month = self.last_month
        seen = []
        for number in (1, 2, 3):
            response = self.get(self.pilot, year=year, month=month, page=number)
            self.assertEqual(response.context["page"].number, number)
            seen.extend(row.pk for row in response.context["rows"])

        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_the_last_page_links_back_with_the_month(self):
        year, month = self.last_month
        page = self.get(self.pilot, year=year, month=month, page=3).content.decode()

        links = [query_of(link) for link in hrefs(page) if "page=" in link]
        self.assertIn({"year": [str(year)], "month": [str(month)], "page": ["2"]}, links)
        self.assertIn({"year": [str(year)], "month": [str(month)], "page": ["1"]}, links)

    def test_paging_without_a_month_has_only_the_page(self):
        page = self.get(self.pilot).content.decode()

        links = [query_of(link) for link in hrefs(page) if "page=" in link]
        self.assertIn({"page": ["2"]}, links)

    def test_the_month_links_start_on_the_first_page(self):
        year, month = self.last_month
        page = self.get(self.pilot, year=year, month=month, page=2).content.decode()

        neighbours = [query_of(link) for link in hrefs(page) if "month=" in link and query_of(link)["month"] != [str(month)]]
        self.assertEqual(len(neighbours), 2)
        for query in neighbours:
            self.assertNotIn("page", query)

    def test_page_values_that_make_no_sense_fall_back(self):
        year, month = self.last_month
        for number in ("abc", "0", "-1", "999", ""):
            with self.subTest(page=number):
                response = self.get(self.pilot, year=year, month=month, page=number)
                self.assertIn(response.context["page"].number, (1, 3))


class ScopeTests(HistoryTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.fleet(at(self.last_month), self.pilot)
        self.shared = self.fleet(at(self.last_month, day=11), self.pilot, self.neighbour)
        self.theirs = self.fleet(at(self.last_month, day=12), self.neighbour)

    def test_personal_history_is_the_users_own(self):
        response = self.get(self.pilot)

        self.assertCountEqual(self.row_operations(response), [self.mine.pk, self.shared.pk])
        self.assertEqual(response.context["title"], "My PAP History")
        self.assertEqual(response.context["history_scope"], "me")

    def test_corporation_history_is_the_snapshot_corporation(self):
        response = self.get(self.pilot, "corporation")

        self.assertCountEqual(self.row_operations(response), [self.mine.pk, self.shared.pk])
        self.assertEqual(response.context["title"], f"{f.DEFAULT_CORP[1]} PAP History")
        self.assertEqual(response.context["history_scope"], "corporation")

    def test_alliance_history_is_everybody(self):
        response = self.get(self.lead, "alliance")

        self.assertEqual(len(self.row_operations(response)), 4)
        self.assertEqual(response.context["title"], "Alliance PAP History")
        self.assertEqual(response.context["history_scope"], "alliance")

    def test_the_corporation_history_has_the_corporation_tabs(self):
        year, month = current_year_month()
        page = html.unescape(self.get(self.pilot, "corporation").content.decode())

        self.assertIn(f'href="{reverse("fleetops:corporation_statistics")}?year={year}&month={month}"', page)
        self.assertIn(f'href="{reverse("fleetops:corporation_members")}?year={year}&month={month}&status=inactive"', page)
        link = re.search(r'<a class="nav-link[^>]*>History</a>', page).group(0)
        self.assertIn('aria-current="page"', link)
        self.assertIn("nav-link active", link)

    def test_the_tabs_follow_the_chosen_month(self):
        year, month = self.last_month
        page = html.unescape(self.get(self.pilot, "corporation", year=year, month=month).content.decode())

        self.assertIn(f'href="{reverse("fleetops:corporation_statistics")}?year={year}&month={month}"', page)

    def test_the_other_history_pages_have_no_tabs(self):
        for user, scope in ((self.pilot, "me"), (self.lead, "alliance")):
            with self.subTest(scope=scope):
                self.assertNotContains(self.get(user, scope), "fleetops-corporation-tabs")

    def test_a_manager_without_a_main_has_no_corporation_history(self):
        nomad = f.create_user("nomad", perms=f.CORP_MANAGEMENT_PERMS, with_main=False)
        self.client.force_login(nomad)

        # Alliance Auth sends users without a main to the dashboard before the page is reached.
        response = self.client.get(reverse("fleetops:attendance_history_corporation"))
        self.assertIn(response.status_code, (302, 404))

    def test_fleets_that_do_not_count_are_not_listed(self):
        pending = self.fleet(at(self.last_month, day=13), self.pilot, review_status="pending")
        excluded = self.fleet(at(self.last_month, day=14), self.pilot, counts_for_stats=False)

        operations = self.row_operations(self.get(self.pilot))

        self.assertNotIn(pending.pk, operations)
        self.assertNotIn(excluded.pk, operations)

    def test_users_who_left_the_alliance_are_hidden(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        leaver = f.create_user("leaver", perms=f.MEMBER_PERMS, alliance=f.FOREIGN_ALLIANCE)
        self.fleet(at(self.last_month, day=15), leaver)

        response = self.get(self.lead, "alliance")

        names = {row.character_name for row in response.context["rows"]}
        self.assertNotIn(f.main_of(leaver).character_name, names)

    def test_fleet_links_need_access_to_the_fleet(self):
        link = reverse("fleetops:operation_detail", args=[self.mine.uuid])
        viewer = f.create_user("viewer", perms=ALL_STATS + ["fleetops.view_all_fleets"])

        self.assertContains(self.get(self.pilot), link)
        self.assertNotContains(self.get(self.lead, "alliance"), link)
        self.assertContains(self.get(viewer, "alliance"), link)


class ExportLinkTests(HistoryTestCase):
    def setUp(self):
        super().setUp()
        self.recent = self.fleet(at(self.last_month), self.pilot, self.neighbour, value=2)
        self.old = self.fleet(at(self.earlier_month), self.pilot, value=3)

    def export_links(self, response):
        page = response.content.decode()
        found = re.findall(r'<a class="btn btn-outline-secondary" href="([^"]*)">(Excel|CSV)</a>', page)
        return {label: html.unescape(href) for href, label in found}

    def test_every_history_page_offers_excel_and_csv(self):
        for scope, user in (("me", self.pilot), ("corporation", self.pilot), ("alliance", self.lead)):
            with self.subTest(scope=scope):
                links = self.export_links(self.get(user, scope))
                self.assertEqual(set(links), {"Excel", "CSV"})
                for fmt, label in (("xlsx", "Excel"), ("csv", "CSV")):
                    url = urlsplit(links[label])
                    self.assertEqual(url.path, reverse("fleetops:export", args=["attendance"]))
                    self.assertEqual(query_of(links[label]), {"scope": [scope], "format": [fmt]})

    def test_the_links_carry_the_chosen_month_and_no_page(self):
        year, month = self.last_month
        with mock.patch(PAGE_SIZE, 1):
            links = self.export_links(self.get(self.pilot, year=year, month=month, page=1))

        self.assertEqual(
            query_of(links["CSV"]),
            {"scope": ["me"], "year": [str(year)], "month": [str(month)], "format": ["csv"]},
        )

    def test_the_links_carry_no_month_while_everything_is_shown(self):
        links = self.export_links(self.get(self.pilot))

        self.assertNotIn("year", query_of(links["CSV"]))
        self.assertNotIn("month", query_of(links["CSV"]))

    def download(self, user, link):
        self.client.force_login(user)
        response = self.client.get(link)
        self.assertEqual(response.status_code, 200)
        text = b"".join(response.streaming_content).decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text, newline="")))

    def test_the_download_holds_the_rows_of_the_page(self):
        year, month = self.last_month
        cases = (
            ("me", self.pilot, {}),
            ("me", self.pilot, {"year": year, "month": month}),
            ("corporation", self.pilot, {"year": year, "month": month}),
            ("alliance", self.lead, {}),
            ("alliance", self.lead, {"year": year, "month": month}),
        )
        for scope, user, params in cases:
            with self.subTest(scope=scope, params=params):
                response = self.get(user, scope, **params)
                page_rows = sorted((str(r.operation.uuid), r.character_name, str(r.attendance_value)) for r in response.context["rows"])
                rows = self.download(user, self.export_links(response)["CSV"])

                self.assertEqual(rows[0][0], "Fleet start (EVE)")
                exported = sorted((row[11], row[6], row[10]) for row in rows[1:])
                self.assertEqual(exported, page_rows)

    def test_the_download_is_audited_with_the_month(self):
        year, month = self.last_month
        links = self.export_links(self.get(self.pilot, year=year, month=month))
        self.download(self.pilot, links["CSV"])

        from fleetops.models import AuditLog

        entry = AuditLog.objects.get(action="statistics.export")
        self.assertEqual(entry.object_id, f"attendance:me:{year:04d}-{month:02d}")
        self.assertEqual(entry.new_value, {"format": "csv", "params": {"scope": "me", "year": year, "month": month}})


class QueryBudgetTests(HistoryTestCase):
    def build(self, fleets, scope_user):
        for index in range(fleets):
            self.fleet(at(self.last_month, day=1 + index % 20, hour=index % 24), scope_user, self.neighbour)

    def queries(self, user, scope):
        self.client.force_login(user)
        url = reverse(f"fleetops:{HISTORY_PAGES[scope]}")
        year, month = self.last_month
        # The first request fills the permission and content type caches.
        self.client.get(url, {"year": year, "month": month})
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, {"year": year, "month": month})
        self.assertEqual(response.status_code, 200)
        return len(queries), len(response.context["rows"])

    def test_the_query_count_does_not_grow_with_the_rows(self):
        self.build(3, self.pilot)
        small = {scope: self.queries(user, scope) for scope, user in (("me", self.pilot), ("corporation", self.pilot), ("alliance", self.lead))}
        self.build(9, self.pilot)
        large = {scope: self.queries(user, scope) for scope, user in (("me", self.pilot), ("corporation", self.pilot), ("alliance", self.lead))}

        for scope in small:
            with self.subTest(scope=scope):
                self.assertGreater(large[scope][1], small[scope][1])
                self.assertEqual(large[scope][0], small[scope][0])
                self.assertLessEqual(large[scope][0], 30)


class MonthRetentionDaysTests(TestCase):
    """The window a month older than the retention is measured against; None while the month is still complete."""

    def days(self, year, month, now, **settings):
        if settings:
            f.settings(**settings)
        with mock.patch("django.utils.timezone.now", return_value=datetime(*now, tzinfo=UTC)):
            return month_retention_days(month_ref(year, month))

    def test_a_month_that_starts_before_the_cutoff_gives_the_window(self):
        self.assertEqual(self.days(2025, 8, (2026, 10, 13, 12)), 365)

    def test_a_month_that_starts_after_the_cutoff_is_complete(self):
        self.assertIsNone(self.days(2025, 11, (2026, 10, 13, 12)))
        self.assertIsNone(self.days(2026, 10, (2026, 10, 13, 12)))

    def test_the_configured_window_is_named(self):
        self.assertEqual(self.days(2025, 1, (2026, 10, 13, 12), data_retention_days=400), 400)

    def test_a_window_longer_than_any_supported_one_never_expires(self):
        self.assertIsNone(self.days(2015, 1, (2026, 10, 13, 12), data_retention_days=999_999))
