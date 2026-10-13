"""Which fleets and PAP count in statistics and incentives.

Every statistics query asks these functions, so the rule lives in one place: a fleet counts when
it is closed, approved by an FC lead (manual and PAP-link fleets) and not excluded from the
statistics; PAP counts when it is granted, approved and its fleet counts.
"""

from django.db import transaction
from django.db.models import Q

from fleetops.models import AttendanceRecord, FleetOperation, FleetOpsSettings, ReviewStatus
from fleetops.services.multipliers import fleet_duration

Status = FleetOperation.Status
StatsMode = FleetOperation.StatsMode
STATS_NOTE_REQUIRED = "Give a reason for excluding this fleet from the statistics."


def counted_operation_q(prefix="", *, include_active=False):
    """The filter of counted fleets; ``prefix`` reaches them through a relation, such as ``operation__``."""
    statuses = [Status.CLOSED]
    if include_active:
        statuses.append(Status.ACTIVE)
    return Q(
        **{
            f"{prefix}status__in": statuses,
            f"{prefix}review_status": ReviewStatus.APPROVED,
            f"{prefix}counts_for_stats": True,
        }
    )


def counted_operations(qs=None, *, include_active=False):
    """Fleets that count: Closed ones, and Active ones too when ``include_active``."""
    qs = FleetOperation.objects.all() if qs is None else qs
    return qs.filter(counted_operation_q(include_active=include_active))


def counted_attendance(qs=None):
    """PAP rows that count. PAP of fleets that are still running never counts: it exists only after the close."""
    qs = AttendanceRecord.objects.all() if qs is None else qs
    return qs.filter(counted_operation_q("operation__"), granted=True, review_status=ReviewStatus.APPROVED)


def operation_count_reason(operation, settings=None):
    """Why a fleet does not count, or "" when it counts (or will count once it closes)."""
    if operation.review_status == ReviewStatus.PENDING:
        return "Pending approval"
    if operation.review_status == ReviewStatus.REJECTED:
        return "Rejected"
    if operation.counts_for_stats:
        return ""
    if operation.stats_mode == StatsMode.EXCLUDE:
        return f"Excluded: {operation.stats_note}" if operation.stats_note else "Excluded"
    settings = settings or FleetOpsSettings.get_solo()
    if settings.minimum_fleet_minutes:
        return f"Shorter than {settings.minimum_fleet_minutes} minutes"
    return "Shorter than the minimum fleet duration"


def evaluate_stats_inclusion(operation, settings=None):
    """Set ``operation.counts_for_stats`` from the fleet's rules and return it; the caller saves.

    A closed fleet is measured to its end time (edited times included), a running one to its end
    request or now. The minimum duration setting applies whenever this runs: at the close, when
    a manual fleet is created or a fleet's times are edited, and when the mode is changed.
    """
    if operation.stats_mode == StatsMode.INCLUDE:
        operation.counts_for_stats = True
    elif operation.stats_mode == StatsMode.EXCLUDE:
        operation.counts_for_stats = False
    else:
        settings = settings or FleetOpsSettings.get_solo()
        minimum = int(settings.minimum_fleet_minutes or 0)
        duration = fleet_duration(operation, operation.ended_at)
        operation.counts_for_stats = not minimum or duration is None or duration.total_seconds() >= minimum * 60
    return operation.counts_for_stats


def set_stats_mode(operation, mode, *, actor, note):
    """Include a fleet in the statistics, exclude it, or go back to the automatic rule.

    Excluding needs a reason. Who may choose which mode is checked by the caller.
    """
    from fleetops.services.audit import audit

    if mode not in StatsMode.values:
        raise ValueError("Choose whether this fleet counts automatically, always or never.")
    note = " ".join((note or "").split())[:255]
    if mode == StatsMode.EXCLUDE and not note:
        raise ValueError(STATS_NOTE_REQUIRED)
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        old = {
            "stats_mode": current.stats_mode,
            "counts_for_stats": current.counts_for_stats,
            "stats_note": current.stats_note,
        }
        operation.stats_mode = mode
        operation.stats_note = note
        operation.ended_at = current.ended_at
        evaluate_stats_inclusion(operation)
        operation.save(update_fields=["stats_mode", "stats_note", "counts_for_stats", "updated_at"])
        audit(
            actor,
            "fleet.stats_mode",
            operation,
            old,
            {"stats_mode": mode, "counts_for_stats": operation.counts_for_stats, "stats_note": note},
            reason=note,
        )
    return operation
