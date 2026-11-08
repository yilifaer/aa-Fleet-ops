# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The system checks for a site setup that leaves FleetOps half working, and the extension logger."""

import copy
from unittest import mock

from django.conf import settings
from django.core import checks
from django.test import SimpleTestCase, override_settings

from fleetops import checks as fleetops_checks

GOOD_SCHEDULE = {
    "a": {"task": fleetops_checks.BEAT_TRACKING_TASK},
    "b": {"task": fleetops_checks.BEAT_PRUNE_TASK},
}


def ids(**overrides):
    """The ids reported for a site that has the public page listed, unless the test changes it."""
    overrides.setdefault("APPS_WITH_PUBLIC_VIEWS", ["fleetops"])
    with override_settings(**overrides):
        return sorted(issue.id for issue in fleetops_checks.check_site_setup())


class SiteSetupChecksTests(SimpleTestCase):
    def test_a_complete_setup_is_silent(self):
        self.assertEqual(
            ids(CELERYBEAT_SCHEDULE=GOOD_SCHEDULE, SITE_URL="https://auth.example.com", APPS_WITH_PUBLIC_VIEWS=["fleetops"]),
            [],
        )

    def test_the_test_project_is_set_up_like_the_readme(self):
        # Only the public page is left out on purpose (the permission tests need it behind the login).
        self.assertEqual(sorted(issue.id for issue in fleetops_checks.check_site_setup()), ["fleetops.W004"])
        self.assertIn("fleetops.W004", settings.SILENCED_SYSTEM_CHECKS)

    def test_missing_beat_entries_are_named_one_by_one(self):
        self.assertEqual(ids(CELERYBEAT_SCHEDULE={}), ["fleetops.W001", "fleetops.W002"])
        self.assertEqual(ids(CELERYBEAT_SCHEDULE={"b": GOOD_SCHEDULE["b"]}), ["fleetops.W001"])
        self.assertEqual(ids(CELERYBEAT_SCHEDULE={"a": GOOD_SCHEDULE["a"]}), ["fleetops.W002"])

    def test_the_entry_is_found_under_any_key_and_a_broken_schedule_does_not_crash(self):
        renamed = {"my-own-key": {"task": fleetops_checks.BEAT_TRACKING_TASK}, "x": "not a dict", "b": GOOD_SCHEDULE["b"]}
        self.assertEqual(ids(CELERYBEAT_SCHEDULE=renamed), [])
        self.assertEqual(ids(CELERYBEAT_SCHEDULE=None), ["fleetops.W001", "fleetops.W002"])

    def test_site_url_must_not_be_empty(self):
        self.assertEqual(ids(SITE_URL=""), ["fleetops.W003"])
        self.assertEqual(ids(SITE_URL="   "), ["fleetops.W003"])

    def test_the_public_page_needs_the_app_in_the_public_list(self):
        self.assertEqual(ids(APPS_WITH_PUBLIC_VIEWS=[]), ["fleetops.W004"])
        self.assertEqual(ids(APPS_WITH_PUBLIC_VIEWS=["other"]), ["fleetops.W004"])

    def test_atomic_requests_are_reported(self):
        databases = copy.deepcopy(settings.DATABASES)
        databases["default"]["ATOMIC_REQUESTS"] = True
        self.assertEqual(ids(DATABASES=databases), ["fleetops.W005"])

    def test_charlink_needs_the_literal_app_entry(self):
        listed = [app for app in settings.INSTALLED_APPS if app != "fleetops"] + ["fleetops.apps.FleetOpsConfig"]
        with mock.patch.object(fleetops_checks.apps, "is_installed", side_effect=lambda name: name == "charlink"):
            self.assertEqual(ids(INSTALLED_APPS=listed), ["fleetops.W006"])
            self.assertEqual(ids(), [])
        # Without CharLink the way the app is written does not matter.
        self.assertEqual(ids(INSTALLED_APPS=listed), [])

    def test_every_issue_is_a_warning_with_a_hint_and_never_blocks(self):
        with override_settings(CELERYBEAT_SCHEDULE={}, SITE_URL="", APPS_WITH_PUBLIC_VIEWS=[]):
            issues = fleetops_checks.check_site_setup()
        self.assertEqual(len(issues), 4)
        for issue in issues:
            self.assertEqual(issue.level, checks.WARNING)
            self.assertTrue(issue.hint)

    def test_the_checks_are_registered(self):
        self.assertIn(fleetops_checks.check_site_setup, checks.registry.registry.registered_checks)


class ExtensionLoggerTests(SimpleTestCase):
    def test_no_module_writes_to_a_plain_logger(self):
        # Alliance Auth sends the extension loggers to extensions.log; a plain logger would end up elsewhere.
        from fleetops import views_pings
        from fleetops.services import standalone_pings

        for module in (views_pings, standalone_pings):
            # django's logger of that name is a child of "extensions" (the name itself is reset to the module name); a plain logger has no such ancestor
            ancestors = []
            parent = module.logger.parent
            while parent is not None:
                ancestors.append(parent.name)
                parent = parent.parent
            self.assertIn("extensions", ancestors)
