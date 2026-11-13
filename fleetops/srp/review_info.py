# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""What the claim page tells SRP staff beyond the claim itself (REV-9): the limits as they are now and the member's
history of rejections. Members do not see these."""

from datetime import timedelta

from django.db.models import Case, Count, Exists, OuterRef, Q, Subquery, When, Window
from django.db.models.functions import Coalesce
from django.utils import timezone

from fleetops.models import SrpClaim, SrpClaimEvent
from fleetops.srp import quota, review
from fleetops.srp.types import PolicySnapshot

Status = SrpClaim.Status
RECENT_REJECTIONS_SHOWN = 3
REJECTION_ACTIONS = (SrpClaimEvent.Action.REJECT, SrpClaimEvent.Action.AUTO_REJECT)


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

    A claim counts as rejected when it is rejected now or its history holds a rejection event (``reject`` or
    ``auto_reject``): a claim the claimant submitted again stays counted, so a resubmission cannot reset the history.
    ``manual`` and ``auto`` are the rejections by a reviewer and by the rules, ``withdrawn`` the withdrawn claims and
    ``recent`` the rejections of the last ``review_history_days`` days (all when that is 0). None for a claim
    without an account.
    """
    if claim.user_id is None:
        return None
    now = now or timezone.now()
    events = SrpClaimEvent.objects.filter(claim=OuterRef("pk"))
    rejection_events = events.filter(action__in=REJECTION_ACTIONS)
    latest_event = rejection_events.order_by("-created_at", "-pk")
    mine = SrpClaim.objects.filter(user_id=claim.user_id).annotate(
        had_reject=Exists(events.filter(action=SrpClaimEvent.Action.REJECT)),
        had_auto=Exists(events.filter(action=SrpClaimEvent.Action.AUTO_REJECT)),
        event_at=Subquery(latest_event.values("created_at")[:1]),
        event_reason=Subquery(latest_event.values("reason")[:1]),
        event_action=Subquery(latest_event.values("action")[:1]),
    )
    now_rejected = Q(status=Status.REJECTED)
    manual = Q(had_reject=True) | (now_rejected & Q(auto_rejected=False))
    rejected = manual | Q(had_auto=True) | now_rejected
    auto = rejected & ~manual
    rejected_at = Coalesce("event_at", "decided_at")
    recent = rejected
    if srp.review_history_days:
        recent = rejected & Q(rejected_at__gte=now - timedelta(days=srp.review_history_days))
    # One query: the counts are window totals over all the claims of the member, carried by the first rows, which are
    # the latest rejections (rejected claims sort first, newest rejection first).
    window = {
        "total": Window(Count("pk")),
        "manual": Window(Count("pk", filter=manual)),
        "auto": Window(Count("pk", filter=auto)),
        "withdrawn": Window(Count("pk", filter=Q(status=Status.WITHDRAWN))),
        "recent": Window(Count("pk", filter=recent)),
    }
    rows = list(
        mine.annotate(rejected_at=rejected_at)
        .annotate(is_rejected=Case(When(rejected, then=1), default=0), **window)
        .order_by("-is_rejected", "-rejected_at", "-pk")[:RECENT_REJECTIONS_SHOWN]
    )
    totals = {name: getattr(rows[0], name) if rows else 0 for name in window}
    latest = []
    for row in rows:
        if not row.is_rejected:
            continue
        if row.status != Status.REJECTED and row.event_at is not None:
            # Submitted again since: what the rejection said comes from its event, the claim's own fields were reset.
            row.decision_reason = row.event_reason
            row.auto_rejected = row.event_action == SrpClaimEvent.Action.AUTO_REJECT
        latest.append(row)
    return {**totals, "latest": latest, "days": srp.review_history_days}
