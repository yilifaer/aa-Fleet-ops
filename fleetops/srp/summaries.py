# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""SRP summaries by EVE month of the loss: personal, corporation, FC and alliance.

A claim counts in the EVE (UTC) month of its killmail time, whenever it was submitted or decided. Withdrawn
claims are left out of every total. Each function uses a constant number of grouped queries, whatever the
number of claims, members or fleets.

``approved_count`` counts approved and paid claims, like ``approved`` adds up their amounts; ``paid_count``
counts the paid ones, so the claims still to pay are ``approved_count - paid_count``.

The limits shown here are the ones a claim is checked against: ``quota.limit_states`` for a player and
``policy.corporation_cap`` for the corporation rows.
"""

from dataclasses import replace

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q, Sum

from fleetops.models import SrpClaim, SrpCorporationLimit, SrpSettings
from fleetops.services.periods import month_bounds
from fleetops.srp import policy as policy_rules
from fleetops.srp import quota
from fleetops.srp.types import (
    PersonalSrp,
    SrpAllianceSummary,
    SrpCorporationRow,
    SrpFcRow,
    SrpFleetRow,
    SrpMemberRow,
    SrpShipRow,
    SrpTotals,
)

Status = SrpClaim.Status
Scope = SrpClaim.Scope

TOP_SHIPS = 10
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
    return total


def month_claims(year: int, month: int, base=None):
    """The claims of an EVE month by loss time, without the withdrawn ones; ``base`` is the claims to start from."""
    start, end = month_bounds(year, month)
    claims = SrpClaim.objects.all() if base is None else base
    return claims.filter(killmail_time__gte=start, killmail_time__lt=end).exclude(status=Status.WITHDRAWN)


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
    return PersonalSrp(year=year, month=month, totals=totals, claims=listed, limits=limits)


# --- Members, corporations, FCs ----------------------------------------------------------------------------


def by_member(year: int, month: int, *, corporation_id=None) -> list[SrpMemberRow]:
    """One row per player, main character and corporation (as claimed), the most approved ISK first."""
    claims = month_claims(year, month)
    if corporation_id is not None:
        claims = claims.filter(corporation_id=corporation_id)
    rows = (
        claims.order_by()
        .values("user_id", "main_character_id", "main_character_name", "corporation_id", "corporation_name")
        .annotate(**_aggregates())
    )
    members = [
        SrpMemberRow(
            user_id=row["user_id"],
            main_character_id=row["main_character_id"],
            main_character_name=row["main_character_name"] or "",
            corporation_id=row["corporation_id"],
            corporation_name=row["corporation_name"] or "",
            totals=_totals(row),
        )
        for row in rows
    ]
    members.sort(key=lambda row: (-row.totals.approved, -row.totals.requested, row.main_character_name.casefold()))
    return members


def _corporation_rows(claims, srp_settings) -> list[SrpCorporationRow]:
    rows = (
        claims.order_by()
        .values("corporation_id")
        .annotate(
            corp_name=Max("corporation_name"),
            corp_ticker=Max("corporation_ticker"),
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
                cap=cap,
                used=used,
                remaining=None if cap is None else max(cap - used, 0),
            )
        )
    return result


def by_corporation(year: int, month: int) -> list[SrpCorporationRow]:
    """One row per corporation (the main character's corporation when the claim was made) with its monthly
    cap, what the corporation used of it and what is left; the most approved ISK first."""
    rows = _corporation_rows(month_claims(year, month), SrpSettings.get_solo())
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


def by_fc(year: int, month: int) -> list[SrpFcRow]:
    """In-fleet claims grouped by the FC of the fleet's SRP link, the most approved ISK first."""
    claims = month_claims(year, month).filter(scope=Scope.IN_FLEET)
    rows = list(
        claims.order_by()
        .values("fc_user_id", "fc_main_character_name")
        .annotate(fleets=Count("link_id", distinct=True), **_aggregates())
    )
    names = _user_names(row["fc_user_id"] for row in rows if not row["fc_main_character_name"])
    result = [
        SrpFcRow(
            fc_user_id=row["fc_user_id"],
            fc_name=row["fc_main_character_name"] or names.get(row["fc_user_id"], ""),
            fleets=row["fleets"] or 0,
            totals=_totals(row),
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
