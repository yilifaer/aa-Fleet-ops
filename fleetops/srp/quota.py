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
from django.db.models import Case, Q, Sum, When

from fleetops.models import SrpCategory, SrpClaim, SrpSettings
from fleetops.services import periods
from fleetops.srp.types import (
    CategoryLimit,
    LIMIT_CATEGORY_CORPORATION,
    LIMIT_CATEGORY_PERSONAL,
    LIMIT_CORPORATION,
    LIMIT_PERSONAL,
    LimitState,
)

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
    *, year, month, user_id=None, corporation_id=None, category_id=None, exclude_claim_id=None, locking: bool = False
) -> tuple[int, int]:
    """``(used, pending)`` of the month: the sum of ``approved_amount`` of approved and paid claims with the
    matching ``counts_*`` flag, and the sum of ``limited_amount`` of pending claims.

    Give ``user_id`` for the personal limit and ``corporation_id`` for the corporation's. With ``category_id`` only
    the claims of that category count, and the ``counts_*`` flags are not looked at: they belong to the general
    limits, not to the limits of a category. ``locking`` reads
    the rows with ``select_for_update`` (inside ``quota_lock``); ``exclude_claim_id`` leaves a claim out, such
    as the one being recalculated.
    """
    if user_id is None and corporation_id is None:
        raise ValueError("Give a user or a corporation.")
    start, end = periods.month_bounds(year, month)
    claims = SrpClaim.objects.filter(killmail_time__gte=start, killmail_time__lt=end)
    if category_id is not None:
        claims = claims.filter(category_id=category_id)
    if user_id is not None:
        claims = claims.filter(user_id=user_id)
        if category_id is None:
            claims = claims.filter(counts_personal=True)
    if corporation_id is not None:
        claims = claims.filter(corporation_id=corporation_id)
        if category_id is None:
            claims = claims.filter(counts_corporation=True)
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


def _usage_filter(*, user_id=None, corporation_id=None, category_id=None) -> Q:
    """The claims one limit counts (the same rules as ``month_usage``)."""
    condition = Q()
    if category_id is not None:
        condition &= Q(category_id=category_id)
    if user_id is not None:
        condition &= Q(user_id=user_id)
        if category_id is None:
            condition &= Q(counts_personal=True)
    if corporation_id is not None:
        condition &= Q(corporation_id=corporation_id)
        if category_id is None:
            condition &= Q(counts_corporation=True)
    return condition


def month_usages(specs, *, year, month, exclude_claim_id=None) -> list[tuple[int, int]]:
    """``month_usage`` for several limits in one query (no locking): ``specs`` is a list of dicts with ``user_id``,
    ``corporation_id`` and ``category_id`` as that function takes them; the result has one ``(used, pending)`` each."""
    start, end = periods.month_bounds(year, month)
    claims = SrpClaim.objects.filter(killmail_time__gte=start, killmail_time__lt=end)
    if exclude_claim_id is not None:
        claims = claims.exclude(pk=exclude_claim_id)
    sums = {}
    for index, spec in enumerate(specs):
        condition = _usage_filter(**spec)
        sums[f"used_{index}"] = Sum(Case(When(condition & Q(status__in=USAGE_STATUSES), then="approved_amount")))
        sums[f"pending_{index}"] = Sum(Case(When(condition & Q(status=SrpClaim.Status.PENDING), then="limited_amount")))
    totals = claims.aggregate(**sums)
    return [(int(totals[f"used_{i}"] or 0), int(totals[f"pending_{i}"] or 0)) for i in range(len(specs))]


def _state(
    kind, cap, applies, *, year, month, usage_id, exclude_claim_id, locking, id_name, category_id=None, usage=None
) -> LimitState:
    used = pending = 0
    if usage is not None:
        used, pending = usage
    elif usage_id is not None:
        used, pending = month_usage(
            year=year,
            month=month,
            category_id=category_id,
            exclude_claim_id=exclude_claim_id,
            locking=locking,
            **{id_name: usage_id},
        )
    remaining = None if cap is None else max(cap - used, 0)
    return LimitState(kind=kind, cap=cap, used=used, pending=pending, remaining=remaining, applies=applies)


def category_personal_limits(*, user_id, killmail_time) -> tuple[CategoryLimit, ...]:
    """The personal monthly limit of every active category that has one, with what the user used of it in the
    month: the rows of "My SRP" (the limits of the category of a claim come from ``limit_states``)."""
    year, month = claim_month(killmail_time)
    categories = list(
        SrpCategory.objects.filter(is_active=True, personal_monthly_cap__isnull=False).order_by("sort_order", "pk")
    )
    if not categories:
        return ()
    start, end = periods.month_bounds(year, month)
    # One query for the usage of all capped categories, grouped by category.
    totals = {
        row["category_id"]: row
        for row in SrpClaim.objects.filter(
            killmail_time__gte=start,
            killmail_time__lt=end,
            user_id=user_id,
            category_id__in=[category.pk for category in categories],
        )
        .values("category_id")
        .annotate(
            used=Sum(Case(When(status__in=USAGE_STATUSES, then="approved_amount"))),
            pending=Sum(Case(When(status=SrpClaim.Status.PENDING, then="limited_amount"))),
        )
    }
    rows = []
    for category in categories:
        row = totals.get(category.pk, {})
        used = int(row.get("used") or 0)
        pending = int(row.get("pending") or 0)
        cap = category.personal_monthly_cap
        state = LimitState(
            kind=LIMIT_CATEGORY_PERSONAL,
            cap=cap,
            used=used,
            pending=pending,
            remaining=max(cap - used, 0),
            applies=True,
        )
        rows.append(CategoryLimit(category_name=category.name, state=state))
    return tuple(rows)


def limit_states(
    *,
    user_id,
    corporation_id,
    scope: str,
    killmail_time,
    policy,
    exclude_claim_id=None,
    locking: bool = False,
    one_query: bool = False,
) -> tuple[LimitState, ...]:
    """The personal and the corporation limit for a claim of that month, and those of the claim's category when
    the policy has a monthly cap for it; ``locking`` reads the usage rows with ``select_for_update``.

    The caps come from ``policy`` (the corporation's already resolved, override or default). A limit does not
    ``apply`` to an in-fleet claim when its switch says in-fleet losses do not count toward it. The
    remaining amount is the cap less what approved and paid claims of the month used, never below 0. ``one_query``
    reads all the usage in a single query (not with ``locking``): for pages that only show the states.
    """
    year, month = claim_month(killmail_time)
    out_of_fleet = scope == SrpClaim.Scope.OUT_OF_FLEET
    category = policy.category
    # (kind, cap, applies, id name, id, category id) of every limit that is checked
    limits = [
        (LIMIT_PERSONAL, policy.personal_monthly_cap, out_of_fleet or policy.personal_cap_includes_in_fleet, "user_id", user_id, None),
        (
            LIMIT_CORPORATION,
            policy.corporation_monthly_cap,
            out_of_fleet or policy.corporation_cap_includes_in_fleet,
            "corporation_id",
            corporation_id,
            None,
        ),
    ]
    if category is not None and category.personal_monthly_cap is not None:
        limits.append((LIMIT_CATEGORY_PERSONAL, category.personal_monthly_cap, True, "user_id", user_id, category.id))
    if category is not None and category.corporation_monthly_cap is not None:
        limits.append(
            (LIMIT_CATEGORY_CORPORATION, category.corporation_monthly_cap, True, "corporation_id", corporation_id, category.id)
        )
    usages = [None] * len(limits)
    if one_query and not locking:
        wanted = [index for index, limit in enumerate(limits) if limit[4] is not None]
        if wanted:
            found = month_usages(
                [{limits[i][3]: limits[i][4], "category_id": limits[i][5]} for i in wanted],
                year=year,
                month=month,
                exclude_claim_id=exclude_claim_id,
            )
            for index, usage in zip(wanted, found):
                usages[index] = usage
    return tuple(
        _state(
            kind,
            cap,
            applies,
            year=year,
            month=month,
            usage_id=usage_id,
            id_name=id_name,
            category_id=category_id,
            exclude_claim_id=exclude_claim_id,
            locking=locking,
            usage=usage,
        )
        for (kind, cap, applies, id_name, usage_id, category_id), usage in zip(limits, usages)
    )
