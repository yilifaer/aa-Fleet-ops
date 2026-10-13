from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings, FleetType, IncentivePeriod, MonthlyFCStatistic
from fleetops.services.counting import counted_operations
from fleetops.services.history import current_member_user_ids
from fleetops.services.statistics import month_bounds
from fleetops.calculations import calculate_payouts


INCENTIVES_DISABLED_MESSAGE = "FC incentives are disabled in the FleetOps settings."
WAIVER_LOCKED_MESSAGE = "Finalized periods must be unlocked before waiver changes."


class IncentiveError(ValueError):
    """An incentive action that is not allowed in the current state."""


def _ensure_enabled():
    if not FleetOpsSettings.get_solo().incentive_enabled:
        raise IncentiveError(INCENTIVES_DISABLED_MESSAGE)


def pending_multiplier_operations(period: IncentivePeriod):
    """Fleets of the period whose PAP multiplier still waits for the FC's choice; they count at 1x until then."""
    start, end = month_bounds(period.year, period.month)
    return (
        FleetOperation.objects.filter(
            started_at__gte=start,
            started_at__lt=end,
            status=FleetOperation.Status.CLOSED,
            multiplier_pending=True,
        )
        .select_related("fleet_type", "fc_user")
        .order_by("started_at", "pk")
    )


def rebuild_period(period: IncentivePeriod):
    _ensure_enabled()
    if period.status == IncentivePeriod.Status.FINALIZED:
        raise IncentiveError("Finalized periods must be unlocked before recalculation.")
    start, end = month_bounds(period.year, period.month)
    # Closed, approved fleets that are not excluded from the statistics; manual fleets are created Closed.
    operations = list(
        counted_operations(FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end))
        .select_related("fc_user", "fleet_type")
        .prefetch_related("role_assignments")
    )
    member_ids = current_member_user_ids()
    grouped = defaultdict(list)
    for op in operations:
        credited_user_ids = {op.fc_user_id}
        credited_user_ids.update(
            assignment.auth_user_id
            for assignment in op.role_assignments.all()
            if assignment.grants_fc_credit and assignment.auth_user_id
        )
        if member_ids is not None:
            credited_user_ids &= member_ids
        for user_id in credited_user_ids:
            grouped[user_id].append(op)

    existing_waivers = {row.fc_user_id: row.waived for row in period.fc_statistics.all()}
    rows = []
    for user_id, ops in grouped.items():
        # FC points: the fleet type weight times the PAP multiplier (1x while the choice is pending).
        points = sum((op.fc_points for op in ops), Decimal("0"))
        breakdown = defaultdict(lambda: {"count": 0, "points": 0.0})
        for op in ops:
            key = str(op.fleet_type)
            breakdown[key]["count"] += 1
            breakdown[key]["points"] += float(op.fc_points)
        rows.append({
            "user_id": user_id,
            "fleet_count": len(ops),
            "points": points,
            "eligible": len(ops) >= period.minimum_fleets,
            "waived": existing_waivers.get(user_id, False),
            "breakdown": dict(breakdown),
        })
    payouts = calculate_payouts(period.budget, rows)
    with transaction.atomic():
        seen = set()
        for row in rows:
            seen.add(row["user_id"])
            MonthlyFCStatistic.objects.update_or_create(
                period=period,
                fc_user_id=row["user_id"],
                defaults={
                    "fleet_count": row["fleet_count"],
                    "total_points": row["points"],
                    "fleet_type_breakdown": row["breakdown"],
                    "eligible": row["eligible"],
                    "waived": row["waived"],
                    "calculated_payout": payouts[row["user_id"]],
                    "final_payout": payouts[row["user_id"]],
                },
            )
        period.fc_statistics.exclude(fc_user_id__in=seen).delete()
        period.policy_snapshot = {
            "minimum_fleets": period.minimum_fleets,
            "fleet_type_weights": {str(ft.pk): str(ft.point_weight) for ft in FleetType.objects.all()},
            "co_fc_credit": "OperationRoleAssignment.grants_fc_credit",
            "fc_points": "fleet type weight x PAP multiplier",
            "counted": "closed, approved, counts_for_stats",
        }
        period.status = IncentivePeriod.Status.REVIEW
        period.save(update_fields=["policy_snapshot", "status"])
    return period


def finalize_period(period: IncentivePeriod, user):
    _ensure_enabled()
    if period.status != IncentivePeriod.Status.REVIEW:
        raise IncentiveError("Period must be in review before finalizing.")
    period.status = IncentivePeriod.Status.FINALIZED
    period.finalized_at = timezone.now()
    period.finalized_by = user
    period.save(update_fields=["status", "finalized_at", "finalized_by"])
    return period


def unlock_period(period: IncentivePeriod):
    _ensure_enabled()
    if period.status != IncentivePeriod.Status.FINALIZED:
        raise IncentiveError("Only finalized periods can be unlocked.")
    period.status = IncentivePeriod.Status.REVIEW
    period.finalized_at = None
    period.finalized_by = None
    period.save(update_fields=["status", "finalized_at", "finalized_by"])
    return period


def set_waiver(period: IncentivePeriod, user_id: int, waived: bool):
    """Set one FC waiver and immediately rebuild payout shares.

    Returns the FC's recalculated statistic, or ``None`` when the FC no longer
    has credited fleets in the period and the row was dropped by the rebuild.
    """
    _ensure_enabled()
    if period.status == IncentivePeriod.Status.FINALIZED:
        raise IncentiveError(WAIVER_LOCKED_MESSAGE)
    row = MonthlyFCStatistic.objects.filter(period=period, fc_user_id=user_id).first()
    if row is None:
        raise IncentiveError("FC statistic does not exist. Recalculate the period first.")
    row.waived = bool(waived)
    row.save(update_fields=["waived"])
    rebuild_period(period)
    return period.fc_statistics.filter(fc_user_id=user_id).first()
