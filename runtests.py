#!/usr/bin/env python
"""Run the FleetOps test suite against a minimal Alliance Auth project."""

import os
import sys

import django
from django.conf import settings
from django.test.utils import get_runner

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testauth.settings")
    django.setup()
    TestRunner = get_runner(settings)
    test_runner = TestRunner(verbosity=1)
    failures = test_runner.run_tests(sys.argv[1:] or ["tests"])
    sys.exit(bool(failures))
