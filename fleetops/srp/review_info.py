# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""What the claim page tells SRP staff beyond the claim itself (REV-9): the limits as they are now and the member's
history of rejections. Members do not see these."""

from datetime import timedelta

from django.db.models import Case, Count, Q, When, Window
from django.utils import timezone

from fleetops.models import SrpClaim
from fleetops.srp import quota, review
from fleetops.srp.types import PolicySnapshot

Status = SrpClaim.Status
RECENT_REJECTIONS_SHOWN = 3


def limits_now(claim) -> dict:
    """The monthly limits for the claim as they stand now, with the claim's own amount left out: the same states the
    approval checks (``quota.limit_states`` on the claim's policy snapshot, without locks).

    ``over_by`` is how far the suggested amount of a pending claim goes beyond the tightest remaining limit (0 when it
    fits or no limit applies); approving that amount needs the "over the monthly limit" confirmation.
    """
    states = quota.limit_states(
        user_id=claim.user_id,
        corporation_id=claim.corporation_id,
        scope=claim.scope,
        killmail_time=claim.killmail_time,
        policy=PolicySnapshot.from_json(claim.policy),
        exclude_claim_id=claim.pk,
        locking=False,
        one_query=True,
    )
    remaining, kind = review._tightest(states)
    over_by = 0
    if claim.status == Status.PENDING and remaining is not None and claim.suggested_amount > remaining:
        over_by = claim.suggested_amount - remaining
    return {"states": list(states), "remaining": remaining, "kind": kind, "over_by": over_by}


def rejection_history(claim, srp, now=None) -> dict | None:
    """How the claimant's claims ended: the numbers and the latest rejections (one query).

    ``manual`` and ``auto`` are the rejections by a reviewer and by the monthly limit, ``withdrawn`` the withdrawn
    claims and ``recent`` the rejections of the last ``review_history_days`` days (all when that is 0). None for a
    claim without an account.
    """
    if claim.user_id is None:
        return None
    now = now or timezone.now()
    mine = SrpClaim.objects.filter(user_id=claim.user_id)
    rejected = Q(status=Status.REJECTED)
    recent = rejected
    if srp.review_history_days:
        recent = rejected & Q(decided_at__gte=now - timedelta(days=srp.review_history_days))
    # One query: the counts are window totals over all the claims of the member, carried by the first rows, which are
    # the latest rejections (rejected claims sort first, newest decision first).
    window = {
        "total": Window(Count("pk")),
        "manual": Window(Count("pk", filter=rejected & Q(auto_rejected=False))),
        "auto": Window(Count("pk", filter=rejected & Q(auto_rejected=True))),
        "withdrawn": Window(Count("pk", filter=Q(status=Status.WITHDRAWN))),
        "recent": Window(Count("pk", filter=recent)),
    }
    rows = list(
        mine.annotate(is_rejected=Case(When(rejected, then=1), default=0), **window).order_by(
            "-is_rejected", "-decided_at", "-pk"
        )[:RECENT_REJECTIONS_SHOWN]
    )
    totals = {name: getattr(rows[0], name) if rows else 0 for name in window}
    latest = [row for row in rows if row.status == Status.REJECTED]
    return {**totals, "latest": latest, "days": srp.review_history_days}
