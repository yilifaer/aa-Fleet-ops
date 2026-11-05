# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP summaries by EVE month or period of the loss: personal, corporation, FC and alliance, and the reports.

A claim counts in the EVE (UTC) month of its killmail time, whenever it was submitted or decided. A period is a
range of whole EVE days (``start`` included, ``end`` not). Withdrawn claims are left out of every total. Each function uses a constant number of grouped queries, whatever the
number of claims, members or fleets.

``approved_count`` counts approved and paid claims, like ``approved`` adds up their amounts; ``paid_count``
counts the paid ones, so the claims still to pay are ``approved_count - paid_count``.

The limits shown here are the ones a claim is checked against: ``quota.limit_states`` for a player and
``policy.corporation_cap`` for the corporation rows.
"""

from dataclasses import replace
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db.models import BigIntegerField, Case, CharField, Count, F, Max, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncMonth
from django.utils.translation import gettext

from fleetops.models import SrpCategory, SrpClaim, SrpClaimEvent, SrpCorporationLimit, SrpSettings
from fleetops.services import universe
from fleetops.services.periods import month_bounds
from fleetops.services.periods import shift_month as periods_shift
from fleetops.srp import policy as policy_rules
from fleetops.srp import quota
from fleetops.srp.types import (
    PersonalSrp,
    SrpAllianceSummary,
    SrpCategoryRow,
    SrpCorporationRow,
    SrpFcRow,
    SrpFleetRow,
    SrpLimitRow,
    SrpMemberRow,
    SrpPayerRow,
    SrpRegionRow,
    SrpReviewerRow,
    SrpRowSet,
    SrpShipRow,
    SrpTotals,
    SrpTrendRow,
)

Status = SrpClaim.Status
Scope = SrpClaim.Scope

TOP_SHIPS = 10
# Rows of the ship report; more ships are cut off and the page says so.
SHIP_REPORT_LIMIT = 500
UTC = dt_timezone.utc
# Claims listed on a corporation or FC page.
LIST_LIMIT = 200


def _aggregates() -> dict:
    """The aggregates every total is built from (withdrawn claims are excluded before they run)."""
    return {
        "n_claims": Count("pk"),
        "n_pending": Count("pk", filter=Q(status=Status.PENDING)),
        "n_approved": Count("pk", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
        "n_paid": Count("pk", filter=Q(status=Status.PAID)),
        "n_rejected": Count("pk", filter=Q(status=Status.REJECTED)),
        "sum_requested": Sum("suggested_amount"),
        "sum_approved": Sum("approved_amount", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
        "sum_paid": Sum("approved_amount", filter=Q(status=Status.PAID)),
        "sum_loss": Sum("loss_value"),
    }


def _totals(row) -> SrpTotals:
    approved = row["sum_approved"] or 0
    paid = row["sum_paid"] or 0
    return SrpTotals(
        claims=row["n_claims"] or 0,
        pending=row["n_pending"] or 0,
        approved_count=row["n_approved"] or 0,
        paid_count=row["n_paid"] or 0,
        rejected=row["n_rejected"] or 0,
        requested=row["sum_requested"] or 0,
        approved=approved,
        paid=paid,
        outstanding=approved - paid,
        loss_value=int(row.get("sum_loss") or 0),
    )


def add_totals(*parts: SrpTotals) -> SrpTotals:
    total = SrpTotals()
    for part in parts:
        total.claims += part.claims
        total.pending += part.pending
        total.approved_count += part.approved_count
        total.paid_count += part.paid_count
        total.rejected += part.rejected
        total.requested += part.requested
        total.approved += part.approved
        total.paid += part.paid
        total.outstanding += part.outstanding
        total.loss_value += part.loss_value
    return total


def period_claims(start, end, base=None):
    """The claims of a period by loss time, ``start`` included and ``end`` not, without the withdrawn ones;
    ``base`` is the claims to start from."""
    claims = SrpClaim.objects.all() if base is None else base
    return claims.filter(killmail_time__gte=start, killmail_time__lt=end).exclude(status=Status.WITHDRAWN)


def month_claims(year: int, month: int, base=None):
    """The claims of an EVE month by loss time, without the withdrawn ones; ``base`` is the claims to start from."""
    start, end = month_bounds(year, month)
    return period_claims(start, end, base)


def earliest_claim_time():
    """The loss time of the oldest claim, or None: where the preset "all" and the year lists begin."""
    return SrpClaim.objects.aggregate(first=Min("killmail_time"))["first"]


def claims_totals(claims) -> SrpTotals:
    """Totals of any claims (withdrawn ones left out), such as the claims of one fleet."""
    return _totals(claims.exclude(status=Status.WITHDRAWN).aggregate(**_aggregates()))


def month_totals(year: int, month: int, *, scope=None, base=None) -> SrpTotals:
    """Totals of a month; ``scope`` limits them to in-fleet or out-of-fleet claims, ``base`` is a queryset
    of claims to start from (such as the claims a viewer may see)."""
    claims = month_claims(year, month, base)
    if scope:
        claims = claims.filter(scope=scope)
    return _totals(claims.aggregate(**_aggregates()))


def claims_in_month(year: int, month: int, *, corporation_id=None, fc_user_id=None, limit: int = LIST_LIMIT) -> list:
    """The claims of a month for a corporation page (the corporation of the claim) or an FC page (in-fleet
    claims of that FC's fleets), the latest losses first."""
    claims = month_claims(year, month)
    if corporation_id is not None:
        claims = claims.filter(corporation_id=corporation_id)
    if fc_user_id is not None:
        claims = claims.filter(scope=Scope.IN_FLEET, fc_user_id=fc_user_id)
    return list(claims.order_by("-killmail_time", "-pk")[:limit])


# --- Limits ----------------------------------------------------------------------------------------------


def corporation_cap(corporation_id, srp_settings=None, overrides=None):
    """The monthly cap of a corporation: its own limit when it has one (no cap there means no limit for it),
    else the default of the settings. None is no limit. ``overrides`` is ``{corporation_id: monthly_cap}``."""
    return policy_rules.corporation_cap(corporation_id, srp_settings or SrpSettings.get_solo(), overrides)[0]


def _main_of(user):
    return getattr(getattr(user, "profile", None), "main_character", None)


def personal(user, year: int, month: int) -> PersonalSrp:
    """The claims of one player in a month, with the totals (withdrawn ones left out) and the limits of the
    player and of the corporation of the player's current main. The list keeps withdrawn claims, so they can
    be submitted again. The limits are the ones a claim of the month is checked against."""
    start, end = month_bounds(year, month)
    claims = SrpClaim.objects.filter(user=user, killmail_time__gte=start, killmail_time__lt=end)
    totals = claims_totals(claims)
    listed = list(claims.order_by("-killmail_time", "-pk"))

    main = _main_of(user)
    corporation_id = main.corporation_id if main is not None else None
    policy = policy_rules.current_policy(corporation_id=corporation_id)
    if not corporation_id:
        # A player without a main has no corporation to limit.
        policy = replace(policy, corporation_monthly_cap=None)
    limits = quota.limit_states(
        user_id=user.pk,
        corporation_id=corporation_id,
        # Out of fleet, so that both limits apply: the page shows what each of them has used.
        scope=Scope.OUT_OF_FLEET,
        killmail_time=start,
        policy=policy,
    )
    category_limits = quota.category_personal_limits(user_id=user.pk, killmail_time=start)
    return PersonalSrp(
        year=year, month=month, totals=totals, claims=listed, limits=limits, category_limits=category_limits
    )


# --- Members, corporations, FCs ----------------------------------------------------------------------------


def _latest_claim_of_members(claims, keys) -> dict:
    """``{member key: main character and corporation of the newest claim}``: one streamed query, the newest claim
    comes last so that it wins; id and name always come from the same claim."""
    ordered = (
        claims.order_by()
        .annotate(
            member_key=Coalesce(F("user_id"), -F("main_character_id"), Value(0), output_field=BigIntegerField())
        )
        .order_by("killmail_time", "pk")
        .values("member_key", "main_character_id", "main_character_name", "corporation_id", "corporation_name")
    )
    wanted = set(keys)
    return {e["member_key"]: e for e in ordered.iterator(chunk_size=2000) if e["member_key"] in wanted}


def by_member(start, end, *, corporation_id=None) -> list[SrpMemberRow]:
    """One row per player in the period (all characters of the player are one row, under the main character as
    claimed), the most approved ISK first. Claims without a player are grouped by their main character.
    ``corporation_id`` limits the claims to one corporation; the alliance report passes none."""
    claims = period_claims(start, end)
    if corporation_id is not None:
        claims = claims.filter(corporation_id=corporation_id)
    rows = (
        claims.order_by()
        .annotate(
            member_key=Coalesce(F("user_id"), -F("main_character_id"), Value(0), output_field=BigIntegerField())
        )
        .values("member_key")
        .annotate(member_user=Max("user_id"), **_aggregates())
    )
    rows = list(rows)
    latest = _latest_claim_of_members(claims, [row["member_key"] for row in rows])
    members = []
    for row in rows:
        claim = latest.get(row["member_key"], {})
        members.append(
            SrpMemberRow(
                user_id=row["member_user"],
                main_character_id=claim.get("main_character_id"),
                main_character_name=claim.get("main_character_name") or "",
                corporation_id=claim.get("corporation_id"),
                corporation_name=claim.get("corporation_name") or "",
                totals=_totals(row),
            )
        )
    members.sort(key=lambda row: (-row.totals.approved, -row.totals.requested, row.main_character_name.casefold()))
    return members


def _corporation_rows(claims, srp_settings) -> list[SrpCorporationRow]:
    rows = (
        claims.order_by()
        .values("corporation_id")
        .annotate(
            corp_name=Max("corporation_name"),
            corp_ticker=Max("corporation_ticker"),
            people=Count("user_id", distinct=True),
            used=Sum(
                "approved_amount", filter=Q(status__in=SrpClaim.USAGE_STATUSES, counts_corporation=True)
            ),
            **_aggregates(),
        )
    )
    overrides = dict(SrpCorporationLimit.objects.values_list("corporation_id", "monthly_cap"))
    result = []
    for row in rows:
        corporation_id = row["corporation_id"]
        cap = corporation_cap(corporation_id, srp_settings, overrides)
        used = row["used"] or 0
        result.append(
            SrpCorporationRow(
                corporation_id=corporation_id,
                corporation_name=row["corp_name"] or "",
                corporation_ticker=row["corp_ticker"] or "",
                totals=_totals(row),
                members=row["people"] or 0,
                cap=cap,
                used=used,
                remaining=None if cap is None else max(cap - used, 0),
            )
        )
    return result


def by_corporation(start, end) -> list[SrpCorporationRow]:
    """One row per corporation (the main character's corporation when the claim was made) in the period, with the
    people who claimed, its monthly cap, what the corporation used of it in the period and what is left (that last
    figure is meant for one month); the most approved ISK first."""
    rows = _corporation_rows(period_claims(start, end), SrpSettings.get_solo())
    rows.sort(key=lambda row: (-row.totals.approved, -row.totals.requested, row.corporation_name.casefold()))
    return rows


def corporation_row(corporation_id: int, year: int, month: int, *, name: str = "") -> SrpCorporationRow:
    """The row of one corporation, also for a month without claims (then with empty totals and its cap)."""
    srp_settings = SrpSettings.get_solo()
    rows = _corporation_rows(month_claims(year, month).filter(corporation_id=corporation_id), srp_settings)
    if rows:
        return rows[0]
    cap = corporation_cap(corporation_id, srp_settings)
    return SrpCorporationRow(
        corporation_id=corporation_id,
        corporation_name=name,
        totals=SrpTotals(),
        cap=cap,
        used=0,
        remaining=cap,
    )


def _user_names(user_ids) -> dict:
    """The current main character names of users, else their user names."""
    ids = [user_id for user_id in user_ids if user_id]
    if not ids:
        return {}
    users = get_user_model().objects.filter(pk__in=ids).select_related("profile__main_character")
    names = {}
    for user in users:
        main = _main_of(user)
        names[user.pk] = (main.character_name if main is not None else "") or user.get_username()
    return names


def by_fc(start, end) -> list[SrpFcRow]:
    """In-fleet claims of the period grouped by the FC of the fleet's SRP link, the most approved ISK first, with
    the number of fleets of each fleet type."""
    claims = period_claims(start, end).filter(scope=Scope.IN_FLEET)
    rows = list(
        claims.order_by()
        .values("fc_user_id", "fc_main_character_name")
        .annotate(fleets=Count("link_id", distinct=True), **_aggregates())
    )
    names = _user_names(row["fc_user_id"] for row in rows if not row["fc_main_character_name"])
    # One more query for the fleet types: (FC, type) -> fleets.
    types = {}
    for row in (
        claims.order_by()
        .values("fc_user_id", "fc_main_character_name", "operation__fleet_type__name")
        .annotate(fleets=Count("link_id", distinct=True))
    ):
        key = (row["fc_user_id"], row["fc_main_character_name"])
        name = row["operation__fleet_type__name"] or ""
        types.setdefault(key, {})[name] = types.get(key, {}).get(name, 0) + row["fleets"]
    result = [
        SrpFcRow(
            fc_user_id=row["fc_user_id"],
            fc_name=row["fc_main_character_name"] or names.get(row["fc_user_id"], ""),
            fleets=row["fleets"] or 0,
            totals=_totals(row),
            fleet_types=dict(sorted(types.get((row["fc_user_id"], row["fc_main_character_name"]), {}).items())),
        )
        for row in rows
    ]
    result.sort(key=lambda row: (-row.totals.approved, -row.totals.requested, row.fc_name.casefold()))
    return result


def fc_fleets(fc_user, year: int, month: int) -> list[SrpFleetRow]:
    """The fleets one FC led with claims in the month, the latest first."""
    claims = month_claims(year, month).filter(scope=Scope.IN_FLEET, fc_user=fc_user)
    rows = (
        claims.order_by()
        .values("link_id", "link__code", "link__is_open", "operation__uuid", "fleet_label")
        .annotate(last_loss=Max("killmail_time"), **_aggregates())
    )
    fleets = sorted(rows, key=lambda row: (row["last_loss"], row["link_id"] or 0), reverse=True)
    return [
        SrpFleetRow(
            link_code=row["link__code"] or "",
            operation_uuid=row["operation__uuid"],
            fleet_label=row["fleet_label"] or "",
            is_open=bool(row["link__is_open"]),
            totals=_totals(row),
        )
        for row in fleets
    ]


def alliance(year: int, month: int) -> SrpAllianceSummary:
    """Totals of the month, split in fleet and out of fleet, by status and the ships lost most."""
    claims = month_claims(year, month)
    by_scope = {
        row["scope"]: _totals(row) for row in claims.order_by().values("scope").annotate(**_aggregates())
    }
    in_fleet = by_scope.get(Scope.IN_FLEET, SrpTotals())
    out_of_fleet = by_scope.get(Scope.OUT_OF_FLEET, SrpTotals())
    totals = add_totals(in_fleet, out_of_fleet)
    ships = (
        claims.order_by()
        .values("ship_type_id")
        .annotate(
            ship=Max("ship_type_name"),
            losses=Count("pk"),
            approved=Sum("approved_amount", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
            approved_count=Count("pk", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
            rejected=Count("pk", filter=Q(status=Status.REJECTED)),
        )
        .order_by("-losses", "-approved", "ship_type_id")[:TOP_SHIPS]
    )
    return SrpAllianceSummary(
        year=year,
        month=month,
        totals=totals,
        in_fleet=in_fleet,
        out_of_fleet=out_of_fleet,
        top_ships=[
            SrpShipRow(
                ship_type_id=row["ship_type_id"],
                ship_type_name=row["ship"] or "",
                losses=row["losses"],
                approved=row["approved"] or 0,
                approved_count=row["approved_count"],
                rejected=row["rejected"],
            )
            for row in ships
        ],
        by_status={
            Status.PENDING: totals.pending,
            Status.APPROVED: totals.approved_count - totals.paid_count,
            Status.PAID: totals.paid_count,
            Status.REJECTED: totals.rejected,
        },
    )


# --- Reports of a period -------------------------------------------------------------------------------------

# Names of the groups that have no person behind them.
AUTOMATIC = "automatic"
IMPORTED = "imported"
UNKNOWN = "unknown"
USER = "user"


def period_totals(start, end) -> SrpTotals:
    """The totals of all claims of the period (withdrawn ones left out)."""
    return _totals(period_claims(start, end).aggregate(**_aggregates()))


def trend(start, end, *, by_category: bool = False) -> list[SrpTrendRow]:
    """The claims of the period month by month (EVE months of the loss), the oldest first, in one query.

    A month without claims is a row of zeros. With ``by_category`` each month has one row per category that has
    claims (``category_id`` None are the claims without a category) and a month without claims has no row.
    The first and the last month of a period that does not begin or end on a month border count only the days
    inside the period.
    """
    group = ["month"] + (["category_id", "category__name"] if by_category else [])
    rows = (
        period_claims(start, end)
        .order_by()
        .annotate(month=TruncMonth("killmail_time", tzinfo=UTC))
        .values(*group)
        .annotate(**_aggregates())
    )
    found = {}
    for row in rows:
        month = row["month"]
        key = (month.year, month.month, row.get("category_id"))
        found[key] = SrpTrendRow(
            year=month.year,
            month=month.month,
            category_id=row.get("category_id"),
            category_name=row.get("category__name") or "",
            totals=_totals(row),
        )
    if by_category:
        return [found[key] for key in sorted(found, key=lambda k: (k[0], k[1], k[2] is None, k[2] or 0))]
    result = []
    year, month = start.year, start.month
    final = end - timedelta(microseconds=1)
    while (year, month) <= (final.year, final.month):
        result.append(found.get((year, month, None)) or SrpTrendRow(year=year, month=month))
        year, month = periods_shift(year, month, 1)
    return result


def by_category(start, end) -> list[SrpCategoryRow]:
    """One row per category with claims in the period (claims without a category are one row with no id), with the
    people who claimed, the claims cut below their base amount and the monthly caps of the category; the most
    approved ISK first. Two queries, a third when a category has a corporation cap (the cap applies per corporation:
    ``corporation_peak`` is what the corporation that used most of it used)."""
    rows = list(
        period_claims(start, end)
        .order_by()
        .values("category_id")
        .annotate(
            members=Count("user_id", distinct=True),
            capped=Count("pk", filter=Q(suggested_amount__lt=F("base_amount"))),
            **_aggregates(),
        )
    )
    categories = {
        category.pk: category
        for category in SrpCategory.objects.filter(pk__in=[row["category_id"] for row in rows if row["category_id"]])
    }
    capped_ids = [pk for pk, category in categories.items() if category.corporation_monthly_cap is not None]
    peaks = {}
    if capped_ids:
        per_corporation = (
            period_claims(start, end)
            .filter(category_id__in=capped_ids, status__in=SrpClaim.USAGE_STATUSES, counts_corporation=True)
            .order_by()
            .values("category_id", "corporation_id")
            .annotate(used=Sum("approved_amount"))
        )
        for entry in per_corporation:
            peaks[entry["category_id"]] = max(peaks.get(entry["category_id"], 0), int(entry["used"] or 0))
    result = []
    for row in rows:
        category = categories.get(row["category_id"])
        result.append(
            SrpCategoryRow(
                category_id=row["category_id"],
                category_key=category.key if category else "",
                category_name=category.name if category else "",
                totals=_totals(row),
                members=row["members"] or 0,
                capped=row["capped"] or 0,
                personal_cap=category.personal_monthly_cap if category else None,
                corporation_cap=category.corporation_monthly_cap if category else None,
                corporation_peak=(
                    peaks.get(category.pk, 0) if category and category.corporation_monthly_cap is not None else None
                ),
            )
        )
    result.sort(key=lambda r: (-r.totals.approved, -r.totals.requested, r.category_name.casefold()))
    return result


def by_ship(start, end, limit: int = SHIP_REPORT_LIMIT) -> SrpRowSet:
    """One row per ship type: losses, approved and rejected claims, approved ISK; the most lost first. At most
    ``limit`` rows; ``truncated`` tells that more ship types exist."""
    rows = list(
        period_claims(start, end)
        .order_by()
        .values("ship_type_id")
        .annotate(
            ship=Max("ship_type_name"),
            losses=Count("pk"),
            approved=Sum("approved_amount", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
            approved_count=Count("pk", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
            rejected=Count("pk", filter=Q(status=Status.REJECTED)),
        )
        .order_by("-losses", "-approved", "ship_type_id")[: limit + 1]
    )
    shown = [
        SrpShipRow(
            ship_type_id=row["ship_type_id"],
            ship_type_name=row["ship"] or "",
            losses=row["losses"],
            approved=row["approved"] or 0,
            approved_count=row["approved_count"],
            rejected=row["rejected"],
        )
        for row in rows[:limit]
    ]
    return SrpRowSet(rows=shown, truncated=len(rows) > limit)


def by_region(start, end) -> list[SrpRegionRow]:
    """One row per region of the loss (the claims of killmails whose region is not known yet are one row with no
    id), the most approved ISK first. The names come from the name cache and the SDE, never from ESI."""
    rows = list(
        period_claims(start, end).order_by().values(region=F("killmail__region_id")).annotate(**_aggregates())
    )
    names = universe.region_names([row["region"] for row in rows if row["region"]])
    result = [
        SrpRegionRow(
            region_id=row["region"],
            region_name=names.get(row["region"], "") if row["region"] else "",
            totals=_totals(row),
        )
        for row in rows
    ]
    result.sort(key=lambda r: (-r.totals.approved, -r.totals.requested, r.region_name.casefold(), r.region_id or 0))
    return result


def _kind_of(user_field: str):
    """Annotation telling who stands behind a decision or a payment."""
    return Case(
        When(**{f"{user_field}__isnull": False}, then=Value(USER)),
        When(source=SrpClaim.Source.AASRP, then=Value(IMPORTED)),
        default=Value(UNKNOWN),
        output_field=CharField(),
    )


def _group_name(kind: str) -> str:
    if kind == AUTOMATIC:
        return gettext("Automatic")
    if kind == IMPORTED:
        return gettext("aa-srp (unknown)")
    return gettext("Unknown")


def by_reviewer(start, end) -> list[SrpReviewerRow]:
    """Who decided on the claims of the period: approved claims and their ISK and rejected claims per reviewer.
    Claims the rules rejected are one "Automatic" row, imported claims without a reviewer one "aa-srp (unknown)"
    row. The most approved ISK first."""
    decided = period_claims(start, end).filter(status__in=(*SrpClaim.USAGE_STATUSES, Status.REJECTED))
    kind = Case(
        When(auto_rejected=True, then=Value(AUTOMATIC)),
        default=_kind_of("decided_by"),
        output_field=CharField(),
    )
    rows = (
        decided.order_by()
        .annotate(reviewer_kind=kind)
        .values("reviewer_kind", "decided_by_id")
        .annotate(
            approved_count=Count("pk", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
            approved=Sum("approved_amount", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
            rejected=Count("pk", filter=Q(status=Status.REJECTED)),
        )
    )
    merged = {}
    for row in rows:
        user_id = row["decided_by_id"] if row["reviewer_kind"] == USER else None
        entry = merged.setdefault(
            (row["reviewer_kind"], user_id), SrpReviewerRow(kind=row["reviewer_kind"], user_id=user_id, name="")
        )
        entry.approved_count += row["approved_count"]
        entry.approved += row["approved"] or 0
        entry.rejected += row["rejected"]
    names = _user_names(key[1] for key in merged)
    result = list(merged.values())
    for row in result:
        row.name = names.get(row.user_id, "") if row.kind == USER else _group_name(row.kind)
        if row.kind == USER and not row.name:
            row.name = _group_name(UNKNOWN)
    result.sort(key=lambda r: (-r.approved, -r.rejected, r.name.casefold()))
    return result


def by_payer(start, end) -> list[SrpPayerRow]:
    """Who paid the paid claims of the period: claims, ISK, payment batches and the average per payer; the most
    ISK first. Imported payments without a payer are one "aa-srp (unknown)" row."""
    rows = (
        period_claims(start, end)
        .filter(status=Status.PAID)
        .order_by()
        .annotate(payer_kind=_kind_of("paid_by"))
        .values("payer_kind", "paid_by_id")
        .annotate(claims=Count("pk"), batches=Count("payment_batch_id", distinct=True), amount=Sum("approved_amount"))
    )
    merged = {}
    for row in rows:
        user_id = row["paid_by_id"] if row["payer_kind"] == USER else None
        entry = merged.setdefault(
            (row["payer_kind"], user_id), SrpPayerRow(kind=row["payer_kind"], user_id=user_id, name="")
        )
        entry.claims += row["claims"]
        entry.batches += row["batches"]
        entry.amount += row["amount"] or 0
    names = _user_names(key[1] for key in merged)
    result = list(merged.values())
    for row in result:
        row.name = names.get(row.user_id, "") if row.kind == USER else _group_name(row.kind)
        if row.kind == USER and not row.name:
            row.name = _group_name(UNKNOWN)
    result.sort(key=lambda r: (-r.amount, -r.claims, r.name.casefold()))
    return result


def rejections(start, end):
    """The rejected claims of the period, the latest loss first (a queryset: pages cut it, files stream it)."""
    return (
        period_claims(start, end)
        .filter(status=Status.REJECTED)
        .select_related("decided_by__profile__main_character")
        .order_by("-killmail_time", "-pk")
    )


def payments(start, end):
    """The paid claims of the period, the latest payment first (a queryset)."""
    return (
        period_claims(start, end)
        .filter(status=Status.PAID)
        .select_related("paid_by__profile__main_character", "payment_batch")
        .order_by("-paid_at", "-pk")
    )


def _waived_claims(start, end) -> dict:
    """``{claim id: (user id, month, category id, counts_personal)}`` of the approved and paid claims whose last approval
    event (approve or amount change) says the reviewer went over a limit (``over_limit_by`` above 0). One query."""
    events = (
        SrpClaimEvent.objects.filter(
            action__in=(SrpClaimEvent.Action.APPROVE, SrpClaimEvent.Action.AMOUNT_CHANGE),
            claim__killmail_time__gte=start,
            claim__killmail_time__lt=end,
            claim__status__in=SrpClaim.USAGE_STATUSES,
        )
        .order_by("claim_id", "created_at", "pk")
        .values_list(
            "claim_id", "data", "claim__user_id", "claim__killmail_time", "claim__category_id", "claim__counts_personal"
        )
    )
    last = {}
    for claim_id, data, user_id, killmail_time, category_id, counts_personal in events:
        over = data.get("over_limit_by") if isinstance(data, dict) else 0
        waived = isinstance(over, (int, float)) and not isinstance(over, bool) and over > 0
        last[claim_id] = (waived, user_id, (killmail_time.year, killmail_time.month), category_id, counts_personal)
    return {claim_id: value[1:] for claim_id, value in last.items() if value[0]}


def limits(start, end) -> list[SrpLimitRow]:
    """The personal monthly limits month by month and member by member, the oldest month first.

    One row for the general personal monthly cap (when the settings have one) and one row for every category with a
    personal monthly cap in which the member has claims that month. ``used`` is what approved and paid claims
    used of the limit, ``over_limit`` the claims (not rejected) whose amount was cut by a limit when it was
    calculated, ``waived`` the approved or paid claims whose last approval went over a limit with the reviewer's
    confirmation. Three queries and the names.

    A limit is monthly, so the months the period touches are counted whole: a period that starts or ends inside a
    month still shows the full usage of that month (and so the real remaining amount).
    """
    last = end - timedelta(microseconds=1)
    start = month_bounds(start.year, start.month)[0]
    end = month_bounds(last.year, last.month)[1]
    srp = SrpSettings.get_solo()
    categories = {
        category.pk: category for category in SrpCategory.objects.filter(personal_monthly_cap__isnull=False)
    }
    base = period_claims(start, end).exclude(status=Status.REJECTED).filter(user_id__isnull=False)
    group = ("month", "user_id", "category_id")
    counted = {
        "used": Sum("approved_amount", filter=Q(status__in=SrpClaim.USAGE_STATUSES)),
        "n_claims": Count("pk"),
        "n_over": Count("pk", filter=Q(over_limit=True)),
        "member": Max("main_character_name"),
    }
    rows = {}

    def add(year, month, user_id, category_id, name, used, claims, over):
        category = categories.get(category_id)
        cap = category.personal_monthly_cap if category else srp.personal_monthly_cap
        key = (year, month, user_id, category_id)
        rows[key] = SrpLimitRow(
            year=year,
            month=month,
            user_id=user_id,
            member_name=name or "",
            category_id=category_id,
            limit_name=category.name if category else "",
            used=int(used or 0),
            cap=cap,
            remaining=None if cap is None else max(cap - int(used or 0), 0),
            claims=claims,
            over_limit=over,
        )

    if srp.personal_monthly_cap is not None:
        general = (
            base.filter(counts_personal=True)
            .order_by()
            .annotate(month=TruncMonth("killmail_time", tzinfo=UTC))
            .values("month", "user_id")
            .annotate(**counted)
        )
        for row in general:
            add(row["month"].year, row["month"].month, row["user_id"], None, row["member"], row["used"], row["n_claims"], row["n_over"])
    if categories:
        per_category = (
            base.filter(category_id__in=list(categories))
            .order_by()
            .annotate(month=TruncMonth("killmail_time", tzinfo=UTC))
            .values(*group)
            .annotate(**counted)
        )
        for row in per_category:
            add(
                row["month"].year,
                row["month"].month,
                row["user_id"],
                row["category_id"],
                row["member"],
                row["used"],
                row["n_claims"],
                row["n_over"],
            )
    for user_id, (year, month), category_id, counts_personal in _waived_claims(start, end).values():
        targets = []
        if srp.personal_monthly_cap is not None and counts_personal:
            targets.append(None)
        if category_id in categories:
            targets.append(category_id)
        for target in targets:
            row = rows.get((year, month, user_id, target))
            if row is not None:
                row.waived += 1
    names = _user_names(row.user_id for row in rows.values() if not row.member_name)
    result = list(rows.values())
    for row in result:
        if not row.member_name:
            row.member_name = names.get(row.user_id, "")
    result.sort(key=lambda r: (r.year, r.month, -r.used, r.member_name.casefold(), r.category_id is not None, r.limit_name))
    return result
