# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Month aggregates of PAP and FC points for the statistics pages, exports and incentives.

A month is an EVE (UTC) month. PAP is credited to the main character of the owner of the character that
earned it. Corporation totals use the corporation the main was in when the PAP was earned; member counts and
member lists use the current main's corporation (see ``services.population``).
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Prefetch, Q, Sum
from django.utils.translation import gettext_lazy

from fleetops.models import (
    AttendanceRecord,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    FleetType,
    OperationRoleAssignment,
    ReviewStatus,
)
from fleetops.services import population
from fleetops.services.assessment import (
    BELOW,
    BELOW_SO_FAR,
    MET,
    NONE,
    corporation_status,
    member_status,
    month_target,
    rank_of,
)
from fleetops.services.counting import counted_attendance, counted_operation_q, counted_operations
from fleetops.services.history import (
    apply_current_membership_filter,
    configured_alliance_ids,
    current_member_profiles,
    retention_cutoff,
)
from fleetops.services.periods import month_bounds, month_ref, utc_day  # noqa: F401 - month_bounds is re-exported
from fleetops.services.stats_types import CorporationOverview, CorporationRow, FcRow

TWO_PLACES = Decimal("0.01")
OTHER_CORPORATIONS = gettext_lazy("Other corporations")
# The orders of the corporation overview; the first is the default.
OVERVIEW_SORTS = ("name", "-name", "average", "-average", "total", "-total", "mains", "-mains", "status", "-status")
# Worst first (``status``), so that the corporations to look at come on top; ``-status`` puts the best first.
_STATUS_ORDER = {"missed": 0, "in_progress": 1, "no_members": 2, "none": 3, "met": 4}


def attendance_queryset(year, month, *, settings_obj=None):
    """Counted PAP of the month; PAP is credited to the main of the character's owner."""
    start, end = month_bounds(year, month)
    qs = counted_attendance(
        AttendanceRecord.objects.filter(operation__started_at__gte=start, operation__started_at__lt=end)
    ).select_related("operation__fleet_type")
    return apply_current_membership_filter(qs, settings_obj)


@dataclass(slots=True)
class UserTotals:
    """A user's counted PAP, fleets and characters in the rows of a queryset."""

    pap: int = 0
    fleets: int = 0
    characters: int = 0


def user_totals(qs) -> dict[int, UserTotals]:
    """PAP, distinct fleets and distinct characters per user; one query. Rows without a user are left out."""
    rows = (
        qs.filter(auth_user__isnull=False)
        .values("auth_user_id")
        .annotate(
            pap=Sum("attendance_value"),
            fleets=Count("operation_id", distinct=True),
            characters=Count("character_id", distinct=True),
        )
        .order_by()
    )
    return {
        row["auth_user_id"]: UserTotals(pap=row["pap"] or 0, fleets=row["fleets"], characters=row["characters"])
        for row in rows
    }


def user_fleet_type_totals(qs) -> dict[int, dict[int, int]]:
    """``{user id: {fleet type id: PAP}}``; one query."""
    rows = (
        qs.filter(auth_user__isnull=False)
        .values("auth_user_id", "operation__fleet_type_id")
        .annotate(pap=Sum("attendance_value"))
        .order_by()
    )
    result = defaultdict(dict)
    for row in rows:
        result[row["auth_user_id"]][row["operation__fleet_type_id"]] = row["pap"] or 0
    return dict(result)


def fleet_types_in(type_ids) -> list[FleetType]:
    """The given fleet types in display order; one query, none for an empty selection."""
    type_ids = {type_id for type_id in type_ids if type_id is not None}
    if not type_ids:
        return []
    return list(FleetType.objects.filter(pk__in=type_ids).order_by("sort_order", "name"))


def not_counted_summary(year: int, month: int) -> dict[str, int]:
    """Fleets started in the month that are not in the statistics (one aggregate query).

    ``excluded`` are closed fleets left out by the minimum duration or by an FC lead, ``pending`` and
    ``rejected`` fleets wait for or failed the approval, ``running`` fleets count once they are closed.
    """
    start, end = month_bounds(year, month)
    Status = FleetOperation.Status
    approved = Q(review_status=ReviewStatus.APPROVED)
    return FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end).aggregate(
        excluded=Count("pk", filter=approved & Q(status=Status.CLOSED, counts_for_stats=False)),
        pending=Count("pk", filter=Q(review_status=ReviewStatus.PENDING)),
        rejected=Count("pk", filter=Q(review_status=ReviewStatus.REJECTED)),
        running=Count("pk", filter=approved & Q(status__in=(Status.ACTIVE, Status.ENDING))),
    )


def attendance_history_queryset(*, user=None, corporation_id=None, alliance=False, days=365):
    qs = counted_attendance().select_related("operation", "operation__fleet_type", "auth_user")
    since = retention_cutoff(days)
    if since is not None:
        qs = qs.filter(operation__started_at__gte=since)
    qs = apply_current_membership_filter(qs)
    if user is not None:
        qs = qs.filter(auth_user=user)
    if corporation_id is not None:
        qs = qs.filter(corporation_id=corporation_id)
    return qs.order_by("-operation__started_at", "character_name", "created_at")


def _fleet_type_breakdown(qs) -> dict[str, int]:
    """PAP of the rows by fleet type name, in fleet type order: one grouped query and one for the type names."""
    pap_by_type = {
        row["operation__fleet_type_id"]: row["pap"] or 0
        for row in qs.values("operation__fleet_type_id").annotate(pap=Sum("attendance_value")).order_by()
    }
    breakdown = {}
    for fleet_type in fleet_types_in(pap_by_type):
        name = str(fleet_type)
        breakdown[name] = breakdown.get(name, 0) + pap_by_type[fleet_type.pk]
    return breakdown


def member_statistics(user, year, month):
    qs = attendance_queryset(year, month).filter(auth_user=user)
    total = qs.aggregate(total=Sum("attendance_value"))["total"] or 0
    unique_fleets = qs.values("operation_id").distinct().count()
    breakdown = _fleet_type_breakdown(qs)

    start, end = month_bounds(year, month)
    top_ships = list(
        FleetMemberState.objects.filter(
            counted_operation_q("operation__"),
            auth_user=user,
            operation__started_at__gte=start,
            operation__started_at__lt=end,
        )
        .exclude(ship_type_id=None)
        .values("ship_type_id", "ship_type_name")
        .annotate(uses=Count("operation_id", distinct=True))
        .order_by("-uses", "ship_type_name")[:10]
    )
    for row in top_ships:
        row["image_url"] = f"https://images.evetech.net/types/{row['ship_type_id']}/icon?size=64"

    character_rows = list(
        qs.values("character_id", "character_name")
        .annotate(attendance=Sum("attendance_value"), fleets=Count("operation_id", distinct=True))
        .order_by("-attendance", "character_name")[:10]
    )
    # By EVE (UTC) date, whatever time zone the site uses.
    daily = [
        {"operation__started_at__date": row["day"], "attendance": row["attendance"]}
        for row in qs.annotate(day=utc_day("operation__started_at"))
        .values("day")
        .annotate(attendance=Sum("attendance_value"))
        .order_by("day")
    ]
    role_rows = (
        OperationRoleAssignment.objects.filter(
            counted_operation_q("operation__"),
            auth_user=user,
            operation__started_at__gte=start,
            operation__started_at__lt=end,
        )
        .values("role", "special_role__name")
        .annotate(count=Count("id"))
        .order_by("special_role__sort_order", "special_role__name", "role")
    )
    role_labels = {key: str(label) for key, label in OperationRoleAssignment.Role.choices}
    roles = [
        {
            "key": row["role"],
            "label": row["special_role__name"] or role_labels.get(row["role"], row["role"]),
            "count": row["count"],
        }
        for row in role_rows
    ]
    fc_stats = fc_statistics(user, year, month)
    return {
        "total": total,
        "unique_fleets": unique_fleets,
        "breakdown": breakdown,
        "top_ships": top_ships,
        "characters": character_rows,
        "daily": daily,
        "roles": roles,
        "fc_fleet_count": fc_stats["fleet_count"],
        "fc_points": fc_stats["total_points"],
    }


def member_rank(user, year, month, *, corporation_id=None, settings_obj=None) -> tuple[int | None, int]:
    """``(rank, members with PAP)`` of a user among the members of the alliance or of one corporation.

    Ranks share places on equal PAP (1, 2, 2, 4). A user who is not a member of the scope, or has no PAP,
    has no rank. Two queries.
    """
    member_ids = population.member_user_ids_subquery(
        corporation_id=corporation_id, registered_before=month_bounds(year, month)[1], settings_obj=settings_obj
    )
    base = attendance_queryset(year, month, settings_obj=settings_obj)
    totals = user_totals(base.filter(auth_user_id__in=member_ids))
    paps = [entry.pap for entry in totals.values()]
    own = totals.get(user.pk)
    rank = rank_of(own.pap, paps) if own is not None else None
    return rank, sum(1 for pap in paps if pap > 0)


def average_pap(total_pap, mains) -> Decimal:
    """PAP per main, rounded half up to two places; 0 without mains."""
    if not mains:
        return Decimal("0.00")
    return (Decimal(total_pap) / Decimal(mains)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _below_minimum(pap, target, ref) -> bool:
    return member_status(pap, target, ref) in (BELOW, BELOW_SO_FAR)


def corporation_statistics(corporation_id: int, year: int, month: int):
    """One corporation's month: PAP totals at PAP time, current mains and how many of them are active.

    ``total_attendance`` and the breakdown are PAP earned while the main was in the corporation;
    ``mains_with_pap`` and ``mains_below_minimum`` are the corporation's current mains with their personal
    totals, whichever corporation they earned the PAP in.
    """
    settings_obj = FleetOpsSettings.get_solo()
    ref = month_ref(year, month)
    target = month_target(year, month)
    base = attendance_queryset(year, month, settings_obj=settings_obj)

    type_rows = {
        row["operation__fleet_type_id"]: row
        for row in base.filter(corporation_id=corporation_id)
        .values("operation__fleet_type_id")
        .annotate(pap=Sum("attendance_value"), fleets=Count("operation_id", distinct=True))
        .order_by()
    }
    breakdown = {}
    for fleet_type in fleet_types_in(type_rows):
        name = str(fleet_type)
        breakdown[name] = breakdown.get(name, 0) + (type_rows[fleet_type.pk]["pap"] or 0)
    total = sum(row["pap"] or 0 for row in type_rows.values())
    # A fleet has one type, so the counts of the types add up to the distinct fleets.
    fleets = sum(row["fleets"] for row in type_rows.values())

    main_count = population.member_profiles(
        corporation_id=corporation_id, registered_before=ref.end, settings_obj=settings_obj
    ).count()
    member_ids = population.member_user_ids_subquery(
        corporation_id=corporation_id, registered_before=ref.end, settings_obj=settings_obj
    )
    personal = [entry.pap for entry in user_totals(base.filter(auth_user_id__in=member_ids)).values()]
    below = sum(1 for pap in personal if _below_minimum(pap, target, ref))
    if _below_minimum(0, target, ref):
        below += max(main_count - len(personal), 0)

    averages = {name: (value / main_count if main_count else 0) for name, value in breakdown.items()}
    target_pap = None
    missing_pap = 0
    if target.average is not None and main_count:
        target_pap = target.average * main_count
        missing_pap = max(int((target_pap - total).to_integral_value(rounding=ROUND_CEILING)), 0)
    return {
        "corporation_id": corporation_id,
        "main_character_count": main_count,
        "total_attendance": total,
        "average_attendance": (total / main_count if main_count else 0),
        "breakdown": breakdown,
        "breakdown_average": averages,
        "total_pap": total,
        "average": average_pap(total, main_count),
        "fleets": fleets,
        "mains_with_pap": sum(1 for pap in personal if pap > 0),
        "mains_below_minimum": below,
        # PAP of the current mains, whichever corporation they earned it in.
        "member_pap": sum(personal),
        "status": corporation_status(total, main_count, target, ref),
        "target": target,
        "target_pap": target_pap,
        "missing_pap": missing_pap,
    }


def _sort_corporation_rows(rows, sort):
    """Order the rows by the sort key; rows with equal keys stay in corporation name order."""
    rows.sort(key=lambda row: (row.corporation_name.lower(), row.corporation_id or 0))
    keys = {
        "average": lambda row: row.average,
        "total": lambda row: row.total_pap,
        "mains": lambda row: row.mains,
        "status": lambda row: _STATUS_ORDER.get(row.status, len(_STATUS_ORDER)),
    }
    descending = sort.startswith("-")
    key = keys.get(sort.lstrip("-"))
    if key is not None:
        rows.sort(key=key, reverse=descending)
    elif descending:
        rows.reverse()
    return rows


def corporation_overview(year: int, month: int, *, sort: str = "name") -> CorporationOverview:
    """Every corporation of the alliance for a month, with the "Other corporations" row and the alliance totals.

    The same number of queries whatever the number of corporations or members: the current members, their
    personal totals, the totals by corporation at PAP time, the totals by corporation and fleet type, the
    fleet types, the month's target and the fleets that do not count.
    """
    sort = sort if sort in OVERVIEW_SORTS else "name"
    settings_obj = FleetOpsSettings.get_solo()
    ref = month_ref(year, month)
    target = month_target(year, month)
    base = attendance_queryset(year, month, settings_obj=settings_obj)

    # Every current member, to know which corporations exist; only those who registered by the end of the
    # month are mains of it.
    everyone = population.members(settings_obj=settings_obj)
    people = [person for person in everyone if population.registered_by(person, ref.end)]
    personal = user_totals(base)
    corporations = {}
    for person in everyone:
        corporations.setdefault(
            person.corporation_id, {"person": person, "mains": 0, "with_pap": 0, "below": 0}
        )
    member_pap = 0
    mains_with_pap = 0
    for person in people:
        entry = personal.get(person.user_id)
        pap = entry.pap if entry is not None else 0
        member_pap += pap
        corporation = corporations[person.corporation_id]
        corporation["mains"] += 1
        if pap > 0:
            corporation["with_pap"] += 1
            mains_with_pap += 1
        if _below_minimum(pap, target, ref):
            corporation["below"] += 1

    totals = {
        row["corporation_id"]: row
        for row in base.values("corporation_id")
        .annotate(pap=Sum("attendance_value"), fleets=Count("operation_id", distinct=True))
        .order_by()
    }
    type_totals = defaultdict(dict)
    for row in (
        base.values("corporation_id", "operation__fleet_type_id").annotate(pap=Sum("attendance_value")).order_by()
    ):
        type_totals[row["corporation_id"]][row["operation__fleet_type_id"]] = row["pap"] or 0
    fleet_types = fleet_types_in({type_id for by_type in type_totals.values() for type_id in by_type})

    rows = []
    for corporation_id, corporation in corporations.items():
        if not corporation["mains"] and corporation_id not in totals:
            # Only recruits of a later month and no PAP in this one: nothing to list.
            continue
        person = corporation["person"]
        snapshot = totals.get(corporation_id) or {}
        total = snapshot.get("pap") or 0
        rows.append(
            CorporationRow(
                corporation_id=corporation_id,
                corporation_name=person.corporation_name or str(corporation_id),
                corporation_ticker=person.corporation_ticker,
                alliance_id=person.alliance_id,
                alliance_name=person.alliance_name,
                mains=corporation["mains"],
                mains_with_pap=corporation["with_pap"],
                mains_below_minimum=corporation["below"],
                total_pap=total,
                fleets=snapshot.get("fleets") or 0,
                by_fleet_type=dict(type_totals.get(corporation_id, {})),
                average=average_pap(total, corporation["mains"]),
                status=corporation_status(total, corporation["mains"], target, ref),
            )
        )
    _sort_corporation_rows(rows, sort)

    # PAP earned in corporations without members in scope: left the alliance, NPC corporation, unknown.
    outside_ids = [corporation_id for corporation_id in totals if corporation_id not in corporations]
    other = None
    if outside_ids:
        outside = base.filter(
            Q(corporation_id__in=[corporation_id for corporation_id in outside_ids if corporation_id is not None])
            | Q(corporation_id__isnull=True)
        )
        by_type = defaultdict(int)
        for corporation_id in outside_ids:
            for type_id, pap in type_totals.get(corporation_id, {}).items():
                by_type[type_id] += pap
        other = CorporationRow(
            corporation_id=None,
            corporation_name=str(OTHER_CORPORATIONS),
            corporation_ticker="",
            alliance_id=None,
            alliance_name="",
            mains=0,
            mains_with_pap=0,
            mains_below_minimum=0,
            total_pap=sum(totals[corporation_id]["pap"] or 0 for corporation_id in outside_ids),
            fleets=outside.aggregate(fleets=Count("operation_id", distinct=True))["fleets"],
            by_fleet_type=dict(by_type),
            average=Decimal("0.00"),
            status=NONE,
            is_other=True,
        )

    mains = len(people)
    return CorporationOverview(
        month=ref,
        target=target,
        rows=rows,
        other=other,
        fleet_types=fleet_types,
        sort=sort,
        alliance_filter_configured=bool(configured_alliance_ids(settings_obj)),
        alliance_pap=sum(row["pap"] or 0 for row in totals.values()),
        member_pap=member_pap,
        mains=mains,
        mains_with_pap=mains_with_pap,
        average=average_pap(member_pap, mains),
        corporations_met=sum(1 for row in rows if row.status == MET),
        corporations_with_target=sum(1 for row in rows if row.status != NONE),
        not_counted=not_counted_summary(year, month),
    )


def fc_operations_queryset(user, year: int, month: int, *, include_active=False):
    """Operations credited to an FC, including post-fleet special-role FC credits.

    Only counted fleets count (closed, approved, not excluded), matching the FC incentive rules.
    ``include_active`` also counts fleets that are still running (used by the dashboard).
    Users who left the configured alliances have no FC statistics.
    """
    profiles = current_member_profiles()
    if profiles is not None and not profiles.filter(user_id=user.pk).exists():
        return FleetOperation.objects.none()
    start, end = month_bounds(year, month)
    return (
        counted_operations(
            FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end), include_active=include_active
        )
        .filter(
            Q(fc_user=user)
            | Q(role_assignments__auth_user=user, role_assignments__grants_fc_credit=True)
        )
        .distinct()
    )


def fc_statistics(user, year: int, month: int, *, include_active=False):
    """FC fleets and points of a month; a fleet gives its type's weight times its PAP multiplier.

    A fleet whose multiplier choice is still pending counts at 1x until the FC chooses.
    """
    qs = fc_operations_queryset(user, year, month, include_active=include_active)
    breakdown = defaultdict(lambda: {"count": 0, "points": 0.0})
    points = 0.0
    operations = list(qs.select_related("fleet_type"))
    for op in operations:
        key = str(op.fleet_type)
        p = float(op.fc_points)
        breakdown[key]["count"] += 1
        breakdown[key]["points"] += p
        points += p
    return {"fleet_count": len(operations), "total_points": points, "breakdown": dict(breakdown)}


def credited_operations(year: int, month: int):
    """The counted fleets of a month with what tells who is credited: the fleet type and the credit roles.

    Two queries (fleets, role assignments). The FC pages and the incentive periods use this one list, so
    they always agree.
    """
    start, end = month_bounds(year, month)
    credits = OperationRoleAssignment.objects.filter(grants_fc_credit=True).exclude(auth_user=None).order_by()
    return list(
        counted_operations(FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end))
        .select_related("fleet_type")
        .prefetch_related(Prefetch("role_assignments", queryset=credits))
        .order_by("started_at", "pk")
    )


def credited_user_ids(operation) -> set[int]:
    """Who gets FC credit for a fleet: its FC and every special role that gives FC credit, once each."""
    user_ids = {operation.fc_user_id}
    user_ids.update(
        assignment.auth_user_id
        for assignment in operation.role_assignments.all()
        if assignment.grants_fc_credit and assignment.auth_user_id
    )
    return user_ids


def group_operations_by_fc(operations, member_ids=None) -> dict[int, list]:
    """``{user id: [fleets]}`` for the credited users; ``member_ids`` limits them to the current alliance members."""
    grouped = defaultdict(list)
    for operation in operations:
        user_ids = credited_user_ids(operation)
        if member_ids is not None:
            user_ids &= member_ids
        for user_id in user_ids:
            grouped[user_id].append(operation)
    return dict(grouped)


def fc_breakdown(operations) -> dict[str, dict]:
    """``{fleet type name: {"count", "points"}}`` for an FC's fleets, in fleet type order; points are Decimals."""
    ordered = sorted(operations, key=lambda op: (op.fleet_type.sort_order, str(op.fleet_type)))
    breakdown = {}
    for operation in ordered:
        entry = breakdown.setdefault(str(operation.fleet_type), {"count": 0, "points": Decimal("0")})
        entry["count"] += 1
        entry["points"] += operation.fc_points
    return breakdown


def fc_points(operations) -> Decimal:
    """FC points of fleets: each gives its type's weight at the time times its PAP multiplier."""
    return sum((operation.fc_points for operation in operations), Decimal("0"))


def fc_rows(year: int, month: int) -> list[FcRow]:
    """Every FC with credited fleets in the month, most points first; four queries however many FCs there are."""
    grouped = group_operations_by_fc(credited_operations(year, month))
    if not grouped:
        return []
    users = get_user_model().objects.filter(pk__in=list(grouped)).select_related("profile__main_character")
    profiles = current_member_profiles()
    if profiles is not None:
        # Users who left the member alliances have no FC statistics.
        users = users.filter(pk__in=profiles.values("user_id"))
    rows = []
    for user in users:
        operations = grouped[user.pk]
        main = getattr(getattr(user, "profile", None), "main_character", None)
        rows.append(
            FcRow(
                user=user,
                main_name=main.character_name if main is not None else user.username,
                fleets=len(operations),
                points=fc_points(operations),
                breakdown=fc_breakdown(operations),
                operation_ids=frozenset(operation.pk for operation in operations),
            )
        )
    rows.sort(key=lambda row: (-row.points, -row.fleets, row.main_name.lower(), row.user.pk))
    return rows
