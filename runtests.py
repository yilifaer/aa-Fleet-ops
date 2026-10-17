#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Run the FleetOps test suite against a minimal Alliance Auth project."""

import os
import sys

import django
from django.conf import settings
from django.test.utils import get_runner

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testauth.settings")
    from testauth import netguard

    netguard.install()
    django.setup()
    TestRunner = get_runner(settings)
    # FLEETOPS_TEST_EXCLUDE_TAGS="catalog" leaves out the tests that need the compiled translation catalog.
    exclude_tags = [tag.strip() for tag in os.environ.get("FLEETOPS_TEST_EXCLUDE_TAGS", "").split(",") if tag.strip()]
    test_runner = TestRunner(verbosity=1, exclude_tags=exclude_tags)
    failures = test_runner.run_tests(sys.argv[1:] or ["tests"])
    sys.exit(bool(failures))
