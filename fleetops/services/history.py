# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import math
from datetime import timedelta

from allianceauth.authentication.models import UserProfile
from allianceauth.services.hooks import get_extension_logger
from django.db.models import Q
from django.utils import timezone

from fleetops.models import (
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    AttendanceRecord,
    FleetMemberEvent,
    FleetOpsSettings,
    StandalonePing,
)


logger = get_extension_logger(__name__)

# The most of the users with PAP history that the pruning of departed members may remove in one run, in percent. A
# larger share points to a wrong "Member alliance IDs" (or outdated alliances in Alliance Auth), not to people leaving.
DEFAULT_MAX_PRUNE_SHARE = 50
REFUSED_NO_MEMBERS = "no_members"
REFUSED_TOO_MANY = "too_many_users"


def max_prune_share(settings_obj=None):
    """The share in percent from the settings page (1 to 99, default 50)."""
    settings_obj = settings_obj or FleetOpsSettings.get_solo()
    value = settings_obj.history_prune_max_share
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99:
        return DEFAULT_MAX_PRUNE_SHARE
    return value


def refusal_message(result) -> str:
    """The text that tells why the departed members were not pruned, for the log and the command."""
    if result.get("refused") == REFUSED_NO_MEMBERS:
        return (
            "Departed members were not pruned: none of the Member alliance IDs matches the main character of any "
            f"user, so all {result['history_users']} users with PAP history would be removed. Check the setting, "
            "or run the command with --force if this is intended."
        )
    return (
        f"Departed members were not pruned: {result['refused_users']} of {result['history_users']} users with PAP "
        f"history ({result['refused_share']}%) are outside the Member alliance IDs, more than the "
        f"{result['max_share']}% that the FleetOps setting \"Largest share of departed members to prune\" allows. "
        "Check the Member alliance IDs, or run the command with "
        "--force if this is intended."
    )


def _prune_refusal(profiles, settings_obj):
    """``(code, users to prune, users with history)`` when pruning the departed members must not run, else None."""
    with_history = AttendanceRecord.objects.exclude(auth_user=None)
    history_users = with_history.values("auth_user_id").distinct().count()
    departed_users = (
        with_history.exclude(auth_user_id__in=profiles.values("user_id")).values("auth_user_id").distinct().count()
    )
    if not departed_users:
        return None
    if not profiles.exists():
        return REFUSED_NO_MEMBERS, departed_users, history_users
    if departed_users * 100 > history_users * max_prune_share(settings_obj):
        return REFUSED_TOO_MANY, departed_users, history_users
    return None


def retention_cutoff(days, *, now=None):
    """History older than the returned time has expired; None means nothing expires."""
    days = max(MIN_RETENTION_DAYS, int(days or MIN_RETENTION_DAYS))
    if days > MAX_RETENTION_DAYS:
        # Longer than any supported window (e.g. 999999 meant as "keep forever");
        # subtracting it from now would also overflow the calendar.
        return None
    return (now or timezone.now()) - timedelta(days=days)


def month_retention_days(ref, settings_obj=None):
    """The retention window in days when PAP of the month may already be removed, else None.

    PAP older than the window is pruned, so a month that starts before the cutoff can be incomplete.
    """
    settings_obj = settings_obj or FleetOpsSettings.get_solo()
    cutoff = retention_cutoff(settings_obj.data_retention_days)
    if cutoff is None or ref.start >= cutoff:
        return None
    return max(MIN_RETENTION_DAYS, settings_obj.data_retention_days)


def configured_alliance_ids(settings_obj=None) -> set[int]:
    settings_obj = settings_obj or FleetOpsSettings.get_solo()
    result = set()
    for part in (settings_obj.history_alliance_ids or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def current_member_profiles(settings_obj=None):
    """Profiles whose main character is in a configured member alliance; None when no alliance is configured.

    A queryset, so callers can use it as a subquery (``.values("user_id")``) instead of a Python id list.
    """
    alliance_ids = configured_alliance_ids(settings_obj)
    if not alliance_ids:
        return None
    return UserProfile.objects.filter(main_character__alliance_id__in=alliance_ids)


def current_member_user_ids(settings_obj=None):
    """The user IDs behind :func:`current_member_profiles` as a set, for checks of single users."""
    profiles = current_member_profiles(settings_obj)
    if profiles is None:
        return None
    return set(profiles.values_list("user_id", flat=True))


def apply_current_membership_filter(qs, settings_obj=None):
    profiles = current_member_profiles(settings_obj)
    if profiles is None:
        return qs
    # Unmapped historical characters cannot be proven to have left the alliance,
    # so they remain until the retention window expires.
    return qs.filter(Q(auth_user_id__in=profiles.values("user_id")) | Q(auth_user__isnull=True))


def prune_history(*, dry_run: bool = False, force: bool = False) -> dict:
    settings_obj = FleetOpsSettings.get_solo()
    cutoff = retention_cutoff(settings_obj.data_retention_days)
    result = {"old_attendance": 0, "left_alliance": 0, "old_events": 0, "old_pings": 0}

    old_attendance = AttendanceRecord.objects.none()
    old_events = FleetMemberEvent.objects.none()
    old_pings = StandalonePing.objects.none()
    if cutoff is not None:
        old_attendance = AttendanceRecord.objects.filter(operation__started_at__lt=cutoff)
        old_events = FleetMemberEvent.objects.filter(created_at__lt=cutoff)
        # The text of a ping sent without a fleet; the audit entry (who, where, when, status) stays.
        old_pings = StandalonePing.objects.filter(created_at__lt=cutoff)
    result["old_attendance"] = old_attendance.count()
    result["old_events"] = old_events.count()
    result["old_pings"] = old_pings.count()

    profiles = current_member_profiles(settings_obj)
    left_qs = AttendanceRecord.objects.none()
    refusal = _prune_refusal(profiles, settings_obj) if profiles is not None else None
    if refusal is not None:
        code, departed_users, history_users = refusal
        if force:
            result["forced"] = True
            logger.warning(
                "Pruning of departed members forced although %s of %s users with PAP history are outside the "
                "Member alliance IDs.",
                departed_users,
                history_users,
            )
        else:
            result.update(
                refused=code,
                refused_users=departed_users,
                history_users=history_users,
                refused_share=math.ceil(departed_users * 100 / history_users),
                max_share=max_prune_share(settings_obj),
            )
            profiles = None
            logger.warning(refusal_message(result))
    if profiles is not None:
        left_qs = AttendanceRecord.objects.exclude(auth_user=None).exclude(auth_user_id__in=profiles.values("user_id"))
        if cutoff is not None:
            # Expired rows are already counted above; keep the two totals disjoint.
            left_qs = left_qs.exclude(operation__started_at__lt=cutoff)
        result["left_alliance"] = left_qs.count()

    if not dry_run:
        old_attendance.delete()
        old_events.delete()
        old_pings.delete()
        if profiles is not None:
            left_qs.delete()
    return result
