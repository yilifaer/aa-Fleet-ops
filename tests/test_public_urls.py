# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps URLs behind Alliance Auth's login check, and the one public page.

The URL conf is built the way Alliance Auth builds it (``urls_from_apps`` with the FleetOps url hook),
from a fresh copy of ``fleetops.urls``: Alliance Auth decorates the views of the shared URL patterns
in place, so the patterns of the test project are already decorated.
"""

import importlib.util
import types
import uuid
from unittest import mock
from urllib.parse import urlsplit

import allianceauth.urls
from allianceauth.urls import urls_from_apps
from django.conf import settings
from django.shortcuts import resolve_url
from django.test import TestCase, override_settings
from django.urls import clear_url_caches, converters, reverse

import fleetops
import fleetops.urls
from fleetops.auth_hooks import register_urls
from fleetops.models import CommsPreset

DUMMY_VALUES = {
    converters.IntConverter: 1,
    converters.UUIDConverter: uuid.UUID(int=1),
    converters.SlugConverter: "fleet-types",
    converters.StringConverter: "value",
    converters.PathConverter: "value",
}


def fresh_fleetops_urls():
    spec = importlib.util.find_spec("fleetops.urls")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_urlconf():
    """The project URL conf with FleetOps mounted as Alliance Auth mounts plugin URLs."""
    with mock.patch.object(fleetops, "urls", fresh_fleetops_urls()):
        fleetops_patterns = urls_from_apps([register_urls], getattr(settings, "APPS_WITH_PUBLIC_VIEWS", []))
    urlconf = types.ModuleType("fleetops_public_urls_test")
    # The fresh FleetOps patterns come first, so they answer before the project's own copy.
    urlconf.urlpatterns = fleetops_patterns + list(allianceauth.urls.urlpatterns)
    return urlconf


def dummy_kwargs(pattern):
    return {name: DUMMY_VALUES[type(converter)] for name, converter in pattern.pattern.converters.items()}


class PublicUrlTests(TestCase):
    def setUp(self):
        self.addCleanup(clear_url_caches)
        self.preset = CommsPreset.objects.create(name="Main Comms", voice_url="mumble://voice.example.com/Ops")

    def fleetops_urls(self):
        for pattern in fleetops.urls.urlpatterns:
            yield pattern.name, reverse(f"fleetops:{pattern.name}", kwargs=dummy_kwargs(pattern))

    def get_anonymous(self, path, public_views):
        with override_settings(APPS_WITH_PUBLIC_VIEWS=public_views):
            urlconf = build_urlconf()
            with override_settings(ROOT_URLCONF=urlconf):
                return self.client.get(path)

    def assertLoginRedirect(self, response, path):
        self.assertEqual(response.status_code, 302, path)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, resolve_url(settings.LOGIN_URL), path)
        self.assertIn("next=", location.query, path)

    def test_comms_join_is_the_last_pattern(self):
        last = fleetops.urls.urlpatterns[-1]

        self.assertEqual(last.name, "comms_join")
        self.assertEqual(last.lookup_str, "fleetops.public_views.comms_join")

    def test_every_pattern_has_a_name(self):
        self.assertTrue(all(pattern.name for pattern in fleetops.urls.urlpatterns))

    def test_auth_hook_excludes_only_the_comms_join_page(self):
        hook = register_urls()

        self.assertEqual(hook.excluded_views, {"fleetops.public_views.comms_join"})

    def test_anonymous_users_are_sent_to_the_login_page(self):
        for public_views in ([], ["fleetops"]):
            for name, path in self.fleetops_urls():
                if name == "comms_join":
                    continue
                with self.subTest(public_views=public_views, name=name):
                    self.assertLoginRedirect(self.get_anonymous(path, public_views), path)

    def test_comms_join_is_public_only_when_the_site_allows_public_views(self):
        path = reverse("fleetops:comms_join", args=[self.preset.join_token])

        public = self.get_anonymous(path, ["fleetops"])
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, 'href="mumble://voice.example.com/Ops"')

        self.assertLoginRedirect(self.get_anonymous(path, []), path)

    def test_other_apps_in_public_views_do_not_open_fleetops(self):
        path = reverse("fleetops:comms_join", args=[self.preset.join_token])

        self.assertLoginRedirect(self.get_anonymous(path, ["some_other_app"]), path)

    def test_unknown_token_is_not_found_for_anonymous_users(self):
        path = reverse("fleetops:comms_join", args=[uuid.uuid4()])

        self.assertEqual(self.get_anonymous(path, ["fleetops"]).status_code, 404)

    def test_inactive_preset_is_not_found_for_anonymous_users(self):
        self.preset.is_active = False
        self.preset.save()
        path = reverse("fleetops:comms_join", args=[self.preset.join_token])

        self.assertEqual(self.get_anonymous(path, ["fleetops"]).status_code, 404)

    def test_public_page_for_anonymous_users(self):
        path = reverse("fleetops:comms_join", args=[self.preset.join_token])

        response = self.get_anonymous(path, ["fleetops"])

        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, "Joining Main Comms…")
        self.assertContains(
            response,
            '<script id="fleetops-voice-url" type="application/json">"mumble://voice.example.com/Ops"</script>',
        )

    def test_url_caches_are_restored(self):
        self.get_anonymous(reverse("fleetops:dashboard"), ["fleetops"])

        self.assertEqual(settings.ROOT_URLCONF, "testauth.urls")
        self.assertEqual(reverse("fleetops:dashboard"), "/fleetops/")
