# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Test runner that gives every ``--parallel`` worker its own Redis database.

Django clones the SQL test database per worker. The cache lives in Redis, and several tests call ``cache.clear()``,
which empties the whole Redis database, so the workers must not share one. Worker ``n`` uses the database number of
``FLEETOPS_TEST_REDIS`` plus ``n``; with the default of 13 that needs a Redis with more than 16 databases or a lower
base, for example ``FLEETOPS_TEST_REDIS=redis://localhost:6379/8`` for four workers. The runner checks this before the
workers start and stops with a message if the numbers do not fit.
"""

import re
from urllib.parse import parse_qsl, urlencode

from django.conf import settings
from django.core.cache import caches
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker

from testauth import netguard


def _split_redis_url(url):
    """``(rewrite, database)`` for a Redis URL; ``rewrite(n)`` is the URL with database ``n``. Raises without one."""
    if not isinstance(url, str):
        raise ValueError(f"Cannot give every worker its own Redis database: LOCATION must be one URL, got {url!r}")
    base, _, query = url.partition("?")
    params = parse_qsl(query, keep_blank_values=True)
    for index, (key, value) in enumerate(params):
        if key == "db" and value.isdigit():  # unix:///path/redis.sock?db=3
            def rewrite(database, index=index):
                changed = list(params)
                changed[index] = ("db", str(database))
                return f"{base}?{urlencode(changed)}"

            return rewrite, int(value)
    match = re.fullmatch(r"(.*?://[^/]*)(?:/(\d+))?", base)
    if match is None or match.group(1).startswith("unix://"):
        raise ValueError(f"Cannot give every worker its own Redis database: no database number in {url!r}")
    return (lambda database: f"{match.group(1)}/{database}" + (f"?{query}" if query else "")), int(match.group(2) or 0)


def redis_url_for_worker(url, worker_id):
    """``url`` with its database number raised by ``worker_id`` (a URL without a database counts as 0)."""
    rewrite, database = _split_redis_url(url)
    return rewrite(database + worker_id)


def check_redis_databases(url, workers):
    """Fail early if the databases ``base + 1 .. base + workers`` do not exist on the Redis server."""
    from redis import Redis

    _, base = _split_redis_url(url)
    try:
        available = int(Redis.from_url(url).config_get("databases")["databases"])
    except Exception:  # CONFIG may be disabled; the first cache call then reports the problem
        return
    if base + workers >= available:
        raise RuntimeError(
            f"{workers} parallel workers need Redis databases {base + 1} to {base + workers}, but the server has "
            f"{available} (0 to {available - 1}). Lower the base, for example FLEETOPS_TEST_REDIS="
            f"redis://localhost:6379/{max(available - workers - 1, 0)}, or use fewer workers."
        )


def _init_fleetops_worker(counter, *args):
    _init_worker(counter, *args)
    netguard.install()  # spawned workers do not inherit the guard installed by runtests.py
    from django.test import runner

    options = settings.CACHES["default"]
    if options.get("BACKEND", "").endswith("RedisCache"):
        options["LOCATION"] = redis_url_for_worker(options["LOCATION"], runner._worker_id)
    # The connection made before the fork points at the database of the parent.
    for alias in list(settings.CACHES):
        del caches[alias]


class FleetOpsParallelTestSuite(ParallelTestSuite):
    init_worker = _init_fleetops_worker


class FleetOpsRunner(DiscoverRunner):
    parallel_test_suite = FleetOpsParallelTestSuite

    def run_tests(self, test_labels, **kwargs):
        options = settings.CACHES["default"]
        if self.parallel > 1 and options.get("BACKEND", "").endswith("RedisCache"):
            check_redis_databases(options["LOCATION"], self.parallel)
        return super().run_tests(test_labels, **kwargs)
