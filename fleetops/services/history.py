from __future__ import annotations

from datetime import timedelta

from allianceauth.authentication.models import UserProfile
from django.db.models import Q
from django.utils import timezone

from fleetops.models import (
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    AttendanceRecord,
    FleetMemberEvent,
    FleetOpsSettings,
)


def retention_cutoff(days, *, now=None):
    """History older than the returned time has expired; None means nothing expires."""
    days = max(MIN_RETENTION_DAYS, int(days or MIN_RETENTION_DAYS))
    if days > MAX_RETENTION_DAYS:
        # Longer than any supported window (e.g. 999999 meant as "keep forever");
        # subtracting it from now would also overflow the calendar.
        return None
    return (now or timezone.now()) - timedelta(days=days)


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


def current_member_user_ids(settings_obj=None):
    alliance_ids = configured_alliance_ids(settings_obj)
    if not alliance_ids:
        return None
    return set(
        UserProfile.objects.filter(main_character__alliance_id__in=alliance_ids)
        .exclude(main_character=None)
        .values_list("user_id", flat=True)
    )


def apply_current_membership_filter(qs, settings_obj=None):
    user_ids = current_member_user_ids(settings_obj)
    if user_ids is None:
        return qs
    # Unmapped historical characters cannot be proven to have left the alliance,
    # so they remain until the retention window expires.
    return qs.filter(Q(auth_user_id__in=user_ids) | Q(auth_user__isnull=True))


def prune_history(*, dry_run: bool = False) -> dict:
    settings_obj = FleetOpsSettings.get_solo()
    cutoff = retention_cutoff(settings_obj.data_retention_days)
    result = {"old_attendance": 0, "left_alliance": 0, "old_events": 0}

    old_attendance = AttendanceRecord.objects.none()
    old_events = FleetMemberEvent.objects.none()
    if cutoff is not None:
        old_attendance = AttendanceRecord.objects.filter(operation__started_at__lt=cutoff)
        old_events = FleetMemberEvent.objects.filter(created_at__lt=cutoff)
    result["old_attendance"] = old_attendance.count()
    result["old_events"] = old_events.count()

    user_ids = current_member_user_ids(settings_obj)
    left_qs = AttendanceRecord.objects.none()
    if user_ids is not None:
        left_qs = AttendanceRecord.objects.exclude(auth_user=None).exclude(auth_user_id__in=user_ids)
        if cutoff is not None:
            # Expired rows are already counted above; keep the two totals disjoint.
            left_qs = left_qs.exclude(operation__started_at__lt=cutoff)
        result["left_alliance"] = left_qs.count()

    if not dry_run:
        old_attendance.delete()
        old_events.delete()
        if user_ids is not None:
            left_qs.delete()
    return result
