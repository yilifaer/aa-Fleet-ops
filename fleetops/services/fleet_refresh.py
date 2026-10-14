# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Reading a fleet again right after a change, and what went wrong when it could not be read.

ESI answers the fleet routes from its own 5 second cache, so a change is read twice: at once and
``ESI_READ_LAG`` seconds later (see ``fleet_overlay``). A read that gets a 404 checks whether the FC
character is still the fleet boss; that result and other read errors are kept for a short time so
the fleet page can say why its controls are disabled. Only tracking counts unreadable polls towards
the automatic end; these reads never do.
"""

import time

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from kombu.exceptions import OperationalError as QueueError

from fleetops import texts
from fleetops.models import FleetOperation
from fleetops.providers.esi import FleetESIError, detect_character_fleet
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.fleet_overlay import ESI_READ_LAG
from fleetops.services.identity import character_label
from fleetops.services.tracking import UnreadableFleet, sync_operation

logger = get_extension_logger(__name__)

EndReason = FleetOperation.EndReason

# A boss check after a failed write or refresh is trusted this long.
BOSS_CHECK_SECONDS = 60
READ_ERROR_SECONDS = 120
REFRESH_TASK_LOCK_SECONDS = 30

# Stored messages (English); the pages show them through ``translate_stored``.
REFRESHED_RECENTLY = texts.REFRESHED_RECENTLY()
REFRESHED = texts.REFRESHED()
NOT_OPEN = texts.NOT_OPEN()


def _key(operation_id, name):
    return f"fleetops:fleet:{operation_id}:{name}"


def refresh_task_lock_key(operation_id):
    """Held while ``tasks.refresh_fleet_structure`` reads the fleet."""
    return _key(operation_id, "refresh_task")


def problem_message(operation, problem) -> str:
    """The text for a ``(reason, detail)`` boss check result."""
    from fleetops.services.fleet_access import FLEET_GONE, not_boss_message

    if problem[0] == EndReason.BOSS_HANDOVER:
        return not_boss_message(operation, problem[1])
    return FLEET_GONE


def cached_boss_problem(operation_id):
    """``(reason, detail)`` of a boss check made in the last minute that found a problem, or None."""
    value = cache.get(_key(operation_id, "boss"))
    if not value:
        return None
    return value[0], value[1]


def read_problem(operation_id) -> dict:
    """``{"boss": (reason, detail) | None, "read_error": str}`` for the fleet page."""
    values = cache.get_many([_key(operation_id, "boss"), _key(operation_id, "read_error")])
    boss = values.get(_key(operation_id, "boss"))
    return {
        "boss": (boss[0], boss[1]) if boss else None,
        "read_error": values.get(_key(operation_id, "read_error")) or "",
    }


def clear_read_problem(operation_id) -> None:
    cache.delete_many([_key(operation_id, "boss"), _key(operation_id, "read_error")])


def check_boss(operation):
    """Find out with one fresh ESI read whether the FC character is still the fleet boss.

    Returns an ``UnreadableFleet`` (``reason, detail``; ``boss_id`` after a handover) when the fleet
    is gone or the boss changed, and keeps it for a minute. Returns None when the FC character is
    still the boss (a cached problem is cleared) or the check itself failed.
    """
    try:
        detection = detect_character_fleet(operation.fc_user, operation.fc_character_id, fresh=True)
    except FleetESIError as exc:
        if exc.code != "NOT_IN_FLEET":
            return None
        problem = UnreadableFleet(EndReason.FLEET_GONE)
    except Exception as exc:
        report_failure(logger, "Fleet boss check", exc, operation)
        return None
    else:
        if detection.fleet_id != operation.esi_fleet_id:
            # The FC character is in another fleet now.
            problem = UnreadableFleet(EndReason.FLEET_GONE)
        elif detection.fleet_boss_id and not detection.is_boss(operation.fc_character_id):
            boss_id = int(detection.fleet_boss_id)
            problem = UnreadableFleet(EndReason.BOSS_HANDOVER, character_label(boss_id), boss_id)
        else:
            cache.delete(_key(operation.pk, "boss"))
            return None
    cache.set(_key(operation.pk, "boss"), [problem.reason, problem.detail, problem.boss_id], BOSS_CHECK_SECONDS)
    return problem


def refresh_now(operation, *, min_interval=5) -> tuple[bool, str]:
    """Read members (and, through tracking, wings) from ESI past django-esi's cache; returns ``(ok, message)``.

    With ``min_interval`` a second refresh within that many seconds is skipped. A 404 runs the boss
    check; other failures are kept for two minutes as the fleet's read error.
    """
    if min_interval and not cache.add(_key(operation.pk, "refresh"), 1, min_interval):
        return False, REFRESHED_RECENTLY
    try:
        count = sync_operation(operation, fresh=True)
    except FleetESIError as exc:
        if exc.code == "FLEET_NOT_FOUND":
            problem = check_boss(operation)
            if problem is not None:
                return False, problem_message(operation, problem)
        message = str(exc)
    except Exception as exc:
        message = report_failure(logger, "Fleet refresh", exc, operation)
    else:
        if count is None:
            return False, NOT_OPEN
        clear_read_problem(operation.pk)
        return True, REFRESHED
    cache.set(_key(operation.pk, "read_error"), message, READ_ERROR_SECONDS)
    return False, message


def schedule_refresh(operation, *, delays=(0, ESI_READ_LAG), dedupe_seconds=None) -> bool:
    """Queue fresh reads of the fleet, one per delay in seconds; returns whether anything was queued.

    With ``dedupe_seconds`` only the first call in that window queues reads, however many pages ask.
    Each read is skipped when the fleet was read after it was due (``not_before``), so reads that
    waited for a busy worker do not run back to back. Without a reachable queue the fleet is read
    once in this request instead.
    """
    if dedupe_seconds and not cache.add(_key(operation.pk, "refresh_queued"), 1, dedupe_seconds):
        return False
    # Imported here: the tasks import the fleet services.
    from fleetops.tasks import refresh_fleet_structure

    now = time.time()
    try:
        for delay in delays:
            refresh_fleet_structure.apply_async((operation.pk,), {"not_before": now + delay}, countdown=delay)
    except QueueError as exc:
        log_failure(logger, "Queueing a fleet refresh", exc, operation)
        refresh_now(operation)
    return True
