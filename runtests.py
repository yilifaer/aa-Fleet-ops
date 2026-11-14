#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Run the FleetOps test suite against a minimal Alliance Auth project."""

import argparse
import os
import sys

import django
from django.conf import settings
from django.test.runner import get_max_test_processes, parallel_type
from django.test.utils import get_runner

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testauth.settings")
    from testauth import netguard

    netguard.install()
    django.setup()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", nargs="*", help="test modules, classes or methods (default: tests)")
    # Serial unless asked. Every worker gets its own SQL database and Redis database (see testauth/runner.py).
    parser.add_argument("--parallel", nargs="?", const="auto", default=None,
                        metavar="N", help="run in N processes (default: serial; plain --parallel uses all cores)")
    args = parser.parse_args()
    if args.parallel is None:
        args.parallel = os.environ.get("FLEETOPS_TEST_PARALLEL", "1")
        if not (args.parallel.isdigit() or args.parallel == "auto"):
            parser.error(f"FLEETOPS_TEST_PARALLEL must be a number or 'auto', not {args.parallel!r}")
    elif not (args.parallel.isdigit() or args.parallel == "auto"):  # "--parallel tests.x" was meant as a label
        args.labels.insert(0, args.parallel)
        args.parallel = "auto"
    TestRunner = get_runner(settings)
    # FLEETOPS_TEST_EXCLUDE_TAGS="catalog" leaves out the tests that need the compiled translation catalog.
    exclude_tags = [tag.strip() for tag in os.environ.get("FLEETOPS_TEST_EXCLUDE_TAGS", "").split(",") if tag.strip()]
    # parallel_type() leaves "auto" as a string, which the runner cannot compare with a number.
    parallel = get_max_test_processes() if args.parallel == "auto" else parallel_type(args.parallel)
    test_runner = TestRunner(verbosity=1, exclude_tags=exclude_tags, parallel=parallel)
    failures = test_runner.run_tests(args.labels or ["tests"])
    sys.exit(bool(failures))
