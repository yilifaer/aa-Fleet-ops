# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from collections import Counter
from decimal import Decimal

from allianceauth.services.hooks import get_extension_logger
from django.db import transaction
from django.utils import timezone

from fleetops import texts
from fleetops.calculations import calculate_payouts
from fleetops.models import FleetOperation, FleetOpsSettings, FleetType, IncentivePeriod, MonthlyFCStatistic
from fleetops.services import group_access
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure
from fleetops.services.history import current_member_user_ids
from fleetops.services.periods import year_month_of
from fleetops.services.statistics import (
    credited_operations,
    fc_breakdown,
    fc_points,
    group_operations_by_fc,
    month_bounds,
)

logger = get_extension_logger(__name__)

# Stored messages (English); the pages show them through ``translate_stored``.
INCENTIVES_DISABLED_MESSAGE = texts.INCENTIVES_DISABLED()
WAIVER_LOCKED_MESSAGE = texts.INCENTIVES_WAIVER_LOCKED()
# The two decimals of the points a statistic stores.
POINTS_STEP = Decimal("0.01")


class IncentiveError(ValueError):
    """An incentive action that is not allowed in the current state."""


def _ensure_enabled():
    if not FleetOpsSettings.get_solo().incentive_enabled:
        raise IncentiveError(INCENTIVES_DISABLED_MESSAGE)


def _lock(period):
    """The period's row, locked until the end of the transaction: rebuilds, finalising, waivers and edits queue up."""
    return IncentivePeriod.objects.select_for_update().get(pk=period.pk)


def _copy_back(period, locked, *fields):
    """Show ``period`` (the caller's copy) what was written to the locked row."""
    for name in fields:
        setattr(period, name, getattr(locked, name))


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


def counts_toward_minimum(operation) -> bool:
    """Whether a fleet counts toward the minimum fleets of an FC.

    Every fleet does, except a group fleet that gives no FC points (a fleet type with weight 0). Those fleets
    still appear in the FC's fleet count and in the statistics; they only do not make an FC eligible.
    """
    return not (group_access.is_group_fleet(operation) and operation.fc_points == 0)


def fleets_toward_minimum(operations) -> int:
    """How many of an FC's fleets count toward the minimum fleets."""
    return sum(1 for operation in operations if counts_toward_minimum(operation))


def period_rows(period, grouped=None) -> list[dict]:
    """The FC rows of a period as the current fleets of its month give them.

    ``fleet_count`` is the number of credited fleets (the figure of the FC statistics), ``points`` their FC points,
    ``eligible`` whether enough of them count toward the minimum, and ``breakdown`` has an entry per fleet type with
    ``count``, ``points`` and ``toward_minimum``. The rebuild and the check that tells whether the stored rows are
    out of date both use this one function, so they cannot disagree. ``grouped`` is the result of
    ``group_operations_by_fc`` when the caller has it already. ``waived`` is False here: a waiver belongs to the
    stored row.
    """
    if grouped is None:
        grouped = group_operations_by_fc(credited_operations(period.year, period.month), current_member_user_ids())
    rows = []
    for user_id, ops in grouped.items():
        # FC points: the fleet type weight times the PAP multiplier (1x while the choice is pending).
        toward = Counter(str(operation.fleet_type) for operation in ops if counts_toward_minimum(operation))
        breakdown = {
            name: {"count": entry["count"], "points": float(entry["points"]), "toward_minimum": toward.get(name, 0)}
            for name, entry in fc_breakdown(ops).items()
        }
        rows.append({
            "user_id": user_id,
            "fleet_count": len(ops),
            "points": fc_points(ops),
            "eligible": fleets_toward_minimum(ops) >= period.minimum_fleets,
            "waived": False,
            "breakdown": breakdown,
        })
    return rows


def toward_minimum_count(statistic) -> int:
    """The fleets of a stored FC row that count toward the minimum; rows of older periods count all of them."""
    return sum(
        int(entry.get("toward_minimum", entry.get("count", 0))) for entry in (statistic.fleet_type_breakdown or {}).values()
    )


def _points(value) -> Decimal:
    return Decimal(str(value)).quantize(POINTS_STEP)


def _figures(row) -> dict:
    """What the audit entry and the comparison of a stored or a new row keep: the numbers an FC cares about."""
    return {
        "fleet_count": row["fleet_count"],
        "total_points": str(_points(row["total_points"])),
        "eligible": bool(row["eligible"]),
        "final_payout": int(row["final_payout"]),
    }


def numbers_differ(period, stored) -> bool:
    """Whether the stored FC rows of ``period`` (a list of ``MonthlyFCStatistic``) differ from the current FC statistics.

    Compares the fleet count, the points and the eligibility of every FC. This catches changes no trigger sees,
    such as an FC who left the member alliances or a period calculated by an earlier version.
    """
    current = {row["user_id"]: row for row in period_rows(period)}
    kept = {row.fc_user_id: row for row in stored}
    if set(current) != set(kept):
        return True
    for user_id, row in current.items():
        statistic = kept[user_id]
        if (statistic.fleet_count, _points(statistic.total_points), bool(statistic.eligible)) != (
            row["fleet_count"],
            _points(row["points"]),
            row["eligible"],
        ):
            return True
    return False


def rebuild_period(period: IncentivePeriod, *, automatic=False, actor=None):
    """Calculate the FC rows of a period from the fleets of its month and move it to Review.

    The period's row is locked while it runs and its status is read again under the lock, so a rebuild and a
    finalising never overlap. A finalised period is refused with an ``IncentiveError``; an ``automatic`` rebuild
    (after a fleet of the month changed) quietly does nothing for a finalised period and when incentives are
    switched off. An automatic rebuild writes an audit entry only when a figure changed. Returns the period, or
    None when an automatic rebuild did nothing.
    """
    try:
        _ensure_enabled()
    except IncentiveError:
        if automatic:
            return None
        raise
    now = timezone.now()
    with transaction.atomic():
        locked = _lock(period)
        if locked.status == IncentivePeriod.Status.FINALIZED:
            if automatic:
                return None
            raise IncentiveError(texts.INCENTIVES_REBUILD_LOCKED())
        # Closed, approved fleets that are not excluded from the statistics; manual fleets are created Closed.
        stored = {row.fc_user_id: row for row in locked.fc_statistics.all()}
        rows = period_rows(locked)
        for row in rows:
            row["waived"] = stored[row["user_id"]].waived if row["user_id"] in stored else False
        payouts = calculate_payouts(locked.budget, rows)
        before = {
            user_id: _figures(
                {
                    "fleet_count": row.fleet_count,
                    "total_points": row.total_points,
                    "eligible": row.eligible,
                    "final_payout": row.final_payout,
                }
            )
            for user_id, row in stored.items()
        }
        after = {}
        seen = set()
        for row in rows:
            user_id = row["user_id"]
            seen.add(user_id)
            payout = payouts[user_id]
            MonthlyFCStatistic.objects.update_or_create(
                period=locked,
                fc_user_id=user_id,
                defaults={
                    "fleet_count": row["fleet_count"],
                    "total_points": row["points"],
                    "fleet_type_breakdown": row["breakdown"],
                    "eligible": row["eligible"],
                    "waived": row["waived"],
                    "calculated_payout": payout,
                    "final_payout": payout,
                },
            )
            after[user_id] = _figures({**row, "total_points": row["points"], "final_payout": payout})
        locked.fc_statistics.exclude(fc_user_id__in=seen).delete()
        locked.policy_snapshot = {
            "minimum_fleets": locked.minimum_fleets,
            "fleet_type_weights": {str(ft.pk): str(ft.point_weight) for ft in FleetType.objects.all()},
            "co_fc_credit": "OperationRoleAssignment.grants_fc_credit",
            "fc_points": "fleet type weight x PAP multiplier",
            "counted": "closed, approved, counts_for_stats",
            "group_fleets": "group fleets without FC points do not count toward the minimum fleets",
        }
        locked.status = IncentivePeriod.Status.REVIEW
        locked.calculated_at = now
        locked.save(update_fields=["policy_snapshot", "status", "calculated_at"])
        if automatic and before != after:
            audit(
                actor,
                "incentive.auto_recalculate",
                locked,
                {str(user_id): figures for user_id, figures in sorted(before.items())},
                {str(user_id): figures for user_id, figures in sorted(after.items())},
                reason=texts.INCENTIVES_AUTO_RECALCULATED(),
            )
    _copy_back(period, locked, "policy_snapshot", "status", "calculated_at")
    return period


def finalize_period(period: IncentivePeriod, user):
    _ensure_enabled()
    with transaction.atomic():
        locked = _lock(period)
        if locked.status != IncentivePeriod.Status.REVIEW:
            raise IncentiveError(texts.INCENTIVES_NOT_IN_REVIEW())
        locked.status = IncentivePeriod.Status.FINALIZED
        locked.finalized_at = timezone.now()
        locked.finalized_by = user
        locked.save(update_fields=["status", "finalized_at", "finalized_by"])
    _copy_back(period, locked, "status", "finalized_at", "finalized_by")
    return period


def unlock_period(period: IncentivePeriod):
    _ensure_enabled()
    with transaction.atomic():
        locked = _lock(period)
        if locked.status != IncentivePeriod.Status.FINALIZED:
            raise IncentiveError(texts.INCENTIVES_NOT_FINALIZED())
        locked.status = IncentivePeriod.Status.REVIEW
        locked.finalized_at = None
        locked.finalized_by = None
        locked.save(update_fields=["status", "finalized_at", "finalized_by"])
    _copy_back(period, locked, "status", "finalized_at", "finalized_by")
    return period


def set_waiver(period: IncentivePeriod, user_id: int, waived: bool):
    """Set one FC waiver and immediately rebuild payout shares.

    Returns the FC's recalculated statistic, or ``None`` when the FC no longer
    has credited fleets in the period and the row was dropped by the rebuild.
    """
    _ensure_enabled()
    with transaction.atomic():
        locked = _lock(period)
        if locked.status == IncentivePeriod.Status.FINALIZED:
            raise IncentiveError(WAIVER_LOCKED_MESSAGE)
        row = MonthlyFCStatistic.objects.filter(period=locked, fc_user_id=user_id).first()
        if row is None:
            raise IncentiveError(texts.INCENTIVES_NO_STATISTIC())
        row.waived = bool(waived)
        row.save(update_fields=["waived"])
        rebuild_period(period)
    return period.fc_statistics.filter(fc_user_id=user_id).first()


def months_of(operation, *, previous_started_at=None) -> set[tuple[int, int]]:
    """The EVE months whose incentive periods a change to ``operation`` can affect.

    The month of its start and, after its start time was edited, the month of the old start too.
    """
    months = set()
    for value in (operation.started_at, previous_started_at):
        if value is not None:
            months.add(year_month_of(value))
    return months


def _refresh_months(months, actor):
    for year, month in sorted(months):
        try:
            period = IncentivePeriod.objects.filter(year=year, month=month).exclude(
                status=IncentivePeriod.Status.FINALIZED
            ).first()
            if period is not None:
                rebuild_period(period, automatic=True, actor=actor)
        except Exception as exc:
            # The action that changed the fleet has succeeded; a failed recalculation is logged and can be
            # repeated with the button of the incentive page.
            log_failure(logger, "Incentive recalculation", exc)


def refresh_months_after_commit(*months: tuple[int, int], actor=None) -> None:
    """Recalculate the incentive periods of ``months`` that are not finalised, once the current transaction committed.

    Called wherever a fleet's figures change (end, manual fleet, multiplier, approval, edits, credit roles). It runs
    after the commit, so the recalculation reads what was saved; outside a transaction it runs at once. A finalised
    period never changes, a month without a period is skipped, and a failure is logged without breaking the action
    that called this.
    """
    wanted = {(int(year), int(month)) for year, month in months}
    if not wanted:
        return
    transaction.on_commit(lambda: _refresh_months(wanted, actor))
