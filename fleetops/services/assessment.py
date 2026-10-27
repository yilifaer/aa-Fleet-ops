# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Monthly targets and the statuses derived from them.

A target row applies from its month (EVE time) until the next row. Statuses follow one colour rule for
corporations and members: green when reached, amber while the month still runs, red only for a finished
month that missed the target. A month without an applicable row (before the first one, or none at all) is
not assessed: no status, no inactive members, a member minimum of 0.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q
from django.utils.translation import gettext_lazy, pgettext_lazy

from fleetops.models import StatisticsTarget
from fleetops.services.periods import MonthRef

# Corporation statuses.
MET = "met"
MISSED = "missed"
IN_PROGRESS = "in_progress"
NO_MEMBERS = "no_members"
# Member statuses.
OK = "ok"
BELOW = "below"
BELOW_SO_FAR = "below_so_far"
# Both: nothing to assess.
NONE = "none"

STATUS_LABELS = {
    MET: gettext_lazy("Target met"),
    MISSED: gettext_lazy("Below target"),
    IN_PROGRESS: gettext_lazy("Not reached yet"),
    NO_MEMBERS: gettext_lazy("No members"),
    OK: pgettext_lazy("member status", "OK"),
    BELOW: gettext_lazy("Below minimum"),
    BELOW_SO_FAR: gettext_lazy("Below minimum so far"),
}


def status_label(status: str) -> str:
    """Words for a status key in the active language; empty for ``none`` and unknown keys."""
    label = STATUS_LABELS.get(status)
    return "" if label is None else str(label)


@dataclass(frozen=True, slots=True)
class MonthTarget:
    # Average PAP per main a corporation needs; None = no target.
    average: Decimal | None
    # PAP a member needs; 0 = nobody is listed as inactive (also without a rule).
    minimum: int
    rule: StatisticsTarget | None = None


def month_target(year: int, month: int) -> MonthTarget:
    """The target row with the latest month up to the given one.

    Without a row (a month before the first target, or no target ever set) nothing is assessed: no corporation
    average, a member minimum of 0 and ``rule`` None.
    """
    rule = (
        StatisticsTarget.objects.filter(Q(year__lt=year) | Q(year=year, month__lte=month))
        .order_by("-year", "-month")
        .first()
    )
    if rule is None:
        return MonthTarget(average=None, minimum=0, rule=None)
    return MonthTarget(average=rule.corporation_average_target, minimum=rule.member_minimum_pap, rule=rule)


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def corporation_status(total_pap: int, mains: int, target: MonthTarget, ref: MonthRef) -> str:
    """``met`` when the total reaches ``target x mains`` (exact arithmetic, no rounding).

    Otherwise ``missed`` for a finished month and ``in_progress`` while the month runs. Without a target, and
    for a month that has not begun, there is no status; a corporation without mains has ``no_members``.
    """
    if target.average is None or ref.is_future:
        return NONE
    if mains <= 0:
        return NO_MEMBERS
    if _decimal(total_pap) >= _decimal(target.average) * mains:
        return MET
    return MISSED if ref.is_finished else IN_PROGRESS


def member_status(pap: int, target: MonthTarget, ref: MonthRef) -> str:
    """``ok`` from the minimum upwards; below it ``below_so_far`` while the month runs, ``below`` once it is over."""
    if target.minimum <= 0 or ref.is_future:
        return NONE
    if pap >= target.minimum:
        return OK
    return BELOW_SO_FAR if ref.is_current else BELOW


def competition_ranks(totals: Iterable[tuple[int, int]]) -> dict[int, int]:
    """``{user id: rank}`` for ``(user id, PAP)`` pairs: ties share a rank and the next rank is skipped (1, 2, 2, 4).

    Members without PAP have no rank.
    """
    ordered = sorted(((user_id, pap) for user_id, pap in totals if pap > 0), key=lambda item: -item[1])
    ranks = {}
    previous_pap = None
    rank = 0
    for position, (user_id, pap) in enumerate(ordered, start=1):
        if pap != previous_pap:
            rank = position
            previous_pap = pap
        ranks[user_id] = rank
    return ranks


def rank_of(pap: int, all_paps: Iterable[int]) -> int | None:
    """1 plus the number of members with more PAP; None without PAP."""
    if pap <= 0:
        return None
    return 1 + sum(1 for other in all_paps if other > pap)
