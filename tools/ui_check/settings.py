# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Django settings for the interface check.

The test project of the suite, with a file database, a Redis cache chosen by the caller and the Bootstrap themes
Alliance Auth ships but does not enable by default. The network guard of the tests stays on: a page that tries to
reach ESI or the SSO while it is only displayed fails loudly instead of being slow.
"""

import os
from pathlib import Path

from testauth import netguard
from testauth.settings import *  # noqa: F401,F403

netguard.install()

WORK_DIR = Path(os.environ.get("FLEETOPS_UI_WORK", Path(__file__).resolve().parent / ".work"))

# "Bootstrap" and "Bootstrap Dark" are theme hooks of an app that the Alliance Auth project template does not enable.
if "allianceauth.theme.bootstrap" not in INSTALLED_APPS:  # noqa: F405
    INSTALLED_APPS += ["allianceauth.theme.bootstrap"]  # noqa: F405

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("FLEETOPS_UI_DB", str(WORK_DIR / "ui.sqlite3")),
        "OPTIONS": {"timeout": 30},
    }
}

# Alliance Auth reads a Redis connection while it creates users, so the cache must be a real Redis.
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.environ.get("FLEETOPS_UI_REDIS", os.environ.get("FLEETOPS_TEST_REDIS", "redis://localhost:6379/14")),
    }
}

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
DEBUG = False
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(name)s: %(message)s"}},
    "handlers": {"err": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "django.request": {"handlers": ["err"], "level": "ERROR", "propagate": False},
        "fleetops": {"handlers": ["err"], "level": "ERROR", "propagate": False},
        # Alliance Auth logs and swallows failed saves (theme choice, profile); the run should see them.
        "allianceauth": {"handlers": ["err"], "level": "ERROR", "propagate": False},
    },
}


class DetectFleetAnswerMiddleware:
    """Answer the fleet detection of the start pages with "not in a fleet" instead of calling ESI.

    The start page asks for the fleet of the chosen character as soon as the page is open. The check must not
    reach ESI (the network guard stays on), so the lookup is replaced by the ordinary answer of a pilot who is not
    in a fleet. Nothing else is touched.
    """

    def __init__(self, get_response):
        from fleetops import views
        from fleetops.providers.esi import FleetESIError

        def not_in_fleet(user, character_id, fresh=False):
            raise FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet.", 404)

        views.detect_character_fleet = not_in_fleet
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


MIDDLEWARE = [*MIDDLEWARE, "tools.ui_check.settings.DetectFleetAnswerMiddleware"]  # noqa: F405
