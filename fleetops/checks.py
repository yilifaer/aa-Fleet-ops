# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""System checks for the settings FleetOps cannot work without or that make a feature silently incomplete.

Every check is a warning and none reads the database, so a missing line in ``local.py`` is named by
``python manage.py check`` (and when the server starts) but never stops the site. Like the output of the management
commands, the messages are English only: they are read by the administrator who edits ``local.py``. A site that has a reason for the
setting (for example a schedule kept only in the admin) can silence one check with
``SILENCED_SYSTEM_CHECKS = ["fleetops.W001"]``.
"""

from django.apps import apps
from django.conf import settings
from django.core.checks import Warning, register

BEAT_TRACKING_TASK = "fleetops.tasks.schedule_active_fleet_tracking"
BEAT_PRUNE_TASK = "fleetops.tasks.prune_attendance_history"


def beat_tasks():
    """The task names in ``CELERYBEAT_SCHEDULE``. Alliance Auth's scheduler copies the entries of this setting into
    its database table at start, so the setting is where the entries are written."""
    schedule = getattr(settings, "CELERYBEAT_SCHEDULE", None)
    if not isinstance(schedule, dict):
        return set()
    return {entry.get("task") for entry in schedule.values() if isinstance(entry, dict)}


def default_database_is_atomic():
    return bool(getattr(settings, "DATABASES", {}).get("default", {}).get("ATOMIC_REQUESTS", False))


@register()
def check_site_setup(app_configs=None, **kwargs):
    problems = []
    tasks = beat_tasks()
    if BEAT_TRACKING_TASK not in tasks:
        problems.append(
            Warning(
                ("The Celery beat entry of FleetOps fleet tracking is missing."),
                hint=(
                    'Add CELERYBEAT_SCHEDULE["fleetops_track_active_fleets"] with the task "%(task)s" to local.py '
                    "(see the README). Without it running fleets are not read and nothing is cleaned up."
                )
                % {"task": BEAT_TRACKING_TASK},
                id="fleetops.W001",
            )
        )
    if BEAT_PRUNE_TASK not in tasks:
        problems.append(
            Warning(
                ("The daily Celery beat entry of FleetOps is missing."),
                hint=(
                    'Add CELERYBEAT_SCHEDULE["fleetops_prune_history"] with the task "%(task)s" to local.py '
                    "(see the README). Without it old history is never removed."
                )
                % {"task": BEAT_PRUNE_TASK},
                id="fleetops.W002",
            )
        )
    if not str(getattr(settings, "SITE_URL", "") or "").strip():
        problems.append(
            Warning(
                ("SITE_URL is not set."),
                hint=(
                    "Links in pings, MOTDs and SRP messages are built from SITE_URL. "
                    "Set it to the https address of the site, for example https://auth.example.com."
                ),
                id="fleetops.W003",
            )
        )
    if "fleetops" not in (getattr(settings, "APPS_WITH_PUBLIC_VIEWS", None) or ()):
        problems.append(
            Warning(
                ('"fleetops" is not in APPS_WITH_PUBLIC_VIEWS.'),
                hint=(
                    "The comms join page then asks for a login. Add it with "
                    'APPS_WITH_PUBLIC_VIEWS += ["fleetops"] if pilots who are not logged in should open voice links, '
                    "or silence this check if you do not use the page."
                ),
                id="fleetops.W004",
            )
        )
    if default_database_is_atomic():
        problems.append(
            Warning(
                ("ATOMIC_REQUESTS is on for the default database."),
                hint=(
                    "FleetOps locks rows for the SRP limits and for the end of a fleet and needs each request to "
                    'run outside one transaction. Remove "ATOMIC_REQUESTS": True from DATABASES["default"].'
                ),
                id="fleetops.W005",
            )
        )
    if apps.is_installed("charlink") and "fleetops" not in settings.INSTALLED_APPS:
        problems.append(
            Warning(
                'aa-charlink is installed but INSTALLED_APPS does not list "fleetops" literally.',
                hint=(
                    'CharLink looks for the exact entry "fleetops"; written as "fleetops.apps.FleetOpsConfig" the '
                    "FleetOps scopes do not appear on its page."
                ),
                id="fleetops.W006",
            )
        )
    return problems
