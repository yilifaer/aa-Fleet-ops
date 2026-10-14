# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The JavaScript catalog route and the way the base template loads it."""

import json
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.shortcuts import resolve_url
from django.test import TestCase
from django.urls import reverse

import fleetops
import fleetops.urls

from . import factories as f
from . import i18n_extract as extract

CATALOG_URL = "/fleetops/jsi18n/"


class JavaScriptCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = f.create_user("member", perms=f.MEMBER_PERMS)

    def test_the_route_sits_above_the_public_page(self):
        names = [pattern.name for pattern in fleetops.urls.urlpatterns]

        self.assertIn("jsi18n", names)
        self.assertLess(names.index("jsi18n"), names.index("comms_join"))
        self.assertEqual(names[-1], "comms_join")
        self.assertEqual(reverse("fleetops:jsi18n"), CATALOG_URL)

    def test_a_member_gets_the_catalog(self):
        self.client.force_login(self.member)

        response = self.client.get(CATALOG_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        body = response.content.decode()
        self.assertIn("django.gettext = function", body)
        self.assertIn("globals.gettext = django.gettext", body)

    def test_browsers_keep_the_catalog_for_a_day_but_proxies_do_not(self):
        self.client.force_login(self.member)

        control = {part.strip() for part in self.client.get(CATALOG_URL)["Cache-Control"].split(",")}

        self.assertIn("private", control)
        self.assertIn("max-age=86400", control)
        self.assertNotIn("public", control)

    def test_anonymous_visitors_are_sent_to_the_login_page(self):
        response = self.client.get(CATALOG_URL)

        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, resolve_url(settings.LOGIN_URL))
        self.assertIn("next=", location.query)

    def test_the_catalog_only_offers_get(self):
        self.client.force_login(self.member)

        self.assertEqual(self.client.post(CATALOG_URL).status_code, 405)


class BaseTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = f.create_user("member", perms=f.MEMBER_PERMS)

    def page(self, **cookies):
        self.client.force_login(self.member)
        for name, value in cookies.items():
            self.client.cookies[name] = value
        response = self.client.get(reverse("fleetops:dashboard"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_page_loads_the_catalog_with_language_and_version(self):
        html = self.page()

        self.assertRegex(html, rf'<script src="{CATALOG_URL}\?l=[\w-]+&amp;v={re.escape(fleetops.__version__)}"></script>')

    def test_the_catalog_url_changes_with_the_language(self):
        html = self.page(**{settings.LANGUAGE_COOKIE_NAME: "zh-hans"})

        self.assertIn(f'<script src="{CATALOG_URL}?l=zh-hans&amp;v={fleetops.__version__}"></script>', html)

    def test_the_shim_follows_the_catalog_and_keeps_pages_working_without_it(self):
        html = self.page()

        catalog = html.index(CATALOG_URL)
        shim = html.index("window.gettext = s => s;")
        self.assertLess(catalog, shim)
        for name in ("gettext", "ngettext", "pgettext", "npgettext", "interpolate"):
            self.assertIn(f"window.{name} = ", html[shim:])
        # The shim is defined only when the catalog did not load, and both run before the page's own scripts.
        self.assertRegex(html[catalog:shim], r"typeof window\.gettext !== 'function'")
        self.assertLess(shim, html.index('<div class="container-fluid py-3 fleetops">'))

    def test_the_shim_interpolates_like_django(self):
        html = self.page()

        self.assertTrue(re.search(r"window\.interpolate = \(f, o, named\) => named \?", html))


class ScriptCatalogTests(TestCase):
    """The JavaScript catalog follows the scripts and the language of the page."""

    @classmethod
    def setUpTestData(cls):
        cls.member = f.create_user("member", perms=f.MEMBER_PERMS)

    def test_the_catalog_holds_the_messages_of_the_scripts_and_nothing_else(self):
        found = {}
        for path in extract.script_files():
            found.update(extract.script_messages(path))

        self.assertGreater(len(found), 100)
        self.assertEqual(sorted(set(found) - extract.catalog_keys("djangojs")), [], "run makemessages -d djangojs")
        self.assertEqual(sorted(extract.catalog_keys("djangojs") - set(found)), [], "run makemessages -d djangojs")

    def catalog(self, language):
        self.client.force_login(self.member)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        response = self.client.get(CATALOG_URL)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_chinese_page_gets_the_chinese_messages(self):
        body = self.catalog("zh-hans")

        for text in ("复制失败", "请求没有到达服务器。请检查网络连接后重试。"):
            self.assertIn(json.dumps(text)[1:-1], body)

    def test_an_english_page_gets_no_translation(self):
        body = self.catalog("en")

        self.assertNotIn(json.dumps("复制失败")[1:-1], body)

    def test_the_plural_rule_of_chinese_has_one_form(self):
        self.assertIn("const v = 0;", self.catalog("zh-hans"))
