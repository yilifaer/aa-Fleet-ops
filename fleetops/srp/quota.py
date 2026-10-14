# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Monthly limit accounting. Usage is never stored: it is the sum of the approved and paid claims of the month.

Every change that can alter usage runs inside ``quota_lock()``, which must be the outermost transaction:
the first statement of the transaction locks the ``SrpSettings`` row, so the decisions of all managers and the
submissions of all members queue up one after the other, and the usage rows are read with ``select_for_update``
afterwards. A locking read returns the latest committed rows even under MariaDB's REPEATABLE READ snapshot, so a
second decision sees the amount the first one just approved. Lock order everywhere: settings row, then claim
rows. SRP is a few hundred claims a month, so one global lock is cheap and cannot deadlock.

Do not open ``quota_lock()`` inside a transaction that already read claims; the web request is not atomic.
"""

from contextlib import contextmanager

from django.db import transaction
from django.db.models import Case, Sum, When

from fleetops.models import SrpClaim, SrpSettings
from fleetops.services import periods
from fleetops.srp.types import LIMIT_CORPORATION, LIMIT_PERSONAL, LimitState

USAGE_STATUSES = tuple(SrpClaim.USAGE_STATUSES)


@contextmanager
def quota_lock():
    """Outermost ``transaction.atomic()`` whose first statement locks the ``SrpSettings`` row
    (``select_for_update``); lock order everywhere is settings row, then claim row.

    Yields the locked settings, so the policy read under the lock is the one the decision is made with.
    """
    with transaction.atomic():
        try:
            locked = SrpSettings.objects.select_for_update().get(pk=1)
        except SrpSettings.DoesNotExist:
            SrpSettings.get_solo()
            locked = SrpSettings.objects.select_for_update().get(pk=1)
        yield locked


def claim_month(value) -> tuple[int, int]:
    """``(year, month)`` in EVE time (UTC) a claim counts in: the month of the loss.

    ``value`` is a killmail time or an object with ``killmail_time``. Changing which date decides the month
    means changing this function and the month filters of the summaries.
    """
    return periods.year_month_of(getattr(value, "killmail_time", value))


def month_usage(
    *, year, month, user_id=None, corporation_id=None, exclude_claim_id=None, locking: bool = False
) -> tuple[int, int]:
    """``(used, pending)`` of the month: the sum of ``approved_amount`` of approved and paid claims with the
    matching ``counts_*`` flag, and the sum of ``limited_amount`` of pending claims.

    Give ``user_id`` for the personal limit and ``corporation_id`` for the corporation's. ``locking`` reads
    the rows with ``select_for_update`` (inside ``quota_lock``); ``exclude_claim_id`` leaves a claim out, such
    as the one being recalculated.
    """
    if user_id is None and corporation_id is None:
        raise ValueError("Give a user or a corporation.")
    start, end = periods.month_bounds(year, month)
    claims = SrpClaim.objects.filter(killmail_time__gte=start, killmail_time__lt=end)
    if user_id is not None:
        claims = claims.filter(user_id=user_id, counts_personal=True)
    if corporation_id is not None:
        claims = claims.filter(corporation_id=corporation_id, counts_corporation=True)
    if exclude_claim_id is not None:
        claims = claims.exclude(pk=exclude_claim_id)
    pending_status = SrpClaim.Status.PENDING

    if locking:
        rows = (
            claims.filter(status__in=(*USAGE_STATUSES, pending_status))
            .select_for_update()
            .order_by("pk")
            .values_list("status", "approved_amount", "limited_amount")
        )
        used = pending = 0
        for status, approved_amount, limited_amount in rows:
            if status == pending_status:
                pending += limited_amount or 0
            else:
                used += approved_amount or 0
        return used, pending

    totals = claims.aggregate(
        used=Sum(Case(When(status__in=USAGE_STATUSES, then="approved_amount"))),
        pending=Sum(Case(When(status=pending_status, then="limited_amount"))),
    )
    return int(totals["used"] or 0), int(totals["pending"] or 0)


def _state(kind, cap, applies, *, year, month, usage_id, exclude_claim_id, locking, id_name) -> LimitState:
    used = pending = 0
    if usage_id is not None:
        used, pending = month_usage(
            year=year, month=month, exclude_claim_id=exclude_claim_id, locking=locking, **{id_name: usage_id}
        )
    remaining = None if cap is None else max(cap - used, 0)
    return LimitState(kind=kind, cap=cap, used=used, pending=pending, remaining=remaining, applies=applies)


def limit_states(
    *,
    user_id,
    corporation_id,
    scope: str,
    killmail_time,
    policy,
    exclude_claim_id=None,
    locking: bool = False,
) -> tuple[LimitState, ...]:
    """The personal and the corporation limit for a claim of that month; ``locking`` reads the usage rows
    with ``select_for_update``.

    The caps come from ``policy`` (the corporation's already resolved, override or default). A limit does not
    ``apply`` to an in-fleet claim when its switch says in-fleet losses do not count toward it. The
    remaining amount is the cap less what approved and paid claims of the month used, never below 0.
    """
    year, month = claim_month(killmail_time)
    out_of_fleet = scope == SrpClaim.Scope.OUT_OF_FLEET
    common = {
        "year": year,
        "month": month,
        "exclude_claim_id": exclude_claim_id,
        "locking": locking,
    }
    return (
        _state(
            LIMIT_PERSONAL,
            policy.personal_monthly_cap,
            out_of_fleet or policy.personal_cap_includes_in_fleet,
            usage_id=user_id,
            id_name="user_id",
            **common,
        ),
        _state(
            LIMIT_CORPORATION,
            policy.corporation_monthly_cap,
            out_of_fleet or policy.corporation_cap_includes_in_fleet,
            usage_id=corporation_id,
            id_name="corporation_id",
            **common,
        ),
    )
