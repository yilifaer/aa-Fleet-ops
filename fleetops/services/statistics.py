from collections import defaultdict
from datetime import datetime, timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetMemberState, FleetOperation, OperationRoleAssignment
from fleetops.services.history import apply_current_membership_filter, current_member_user_ids
from fleetops.services.identity import corporation_main_count


def month_bounds(year: int, month: int):
    start = timezone.make_aware(datetime(year, month, 1))
    if month == 12:
        end = timezone.make_aware(datetime(year + 1, 1, 1))
    else:
        end = timezone.make_aware(datetime(year, month + 1, 1))
    return start, end


def attendance_queryset(year, month):
    start, end = month_bounds(year, month)
    qs = AttendanceRecord.objects.filter(
        granted=True,
        operation__started_at__gte=start,
        operation__started_at__lt=end,
    ).select_related("operation__fleet_type")
    return apply_current_membership_filter(qs)


def attendance_history_queryset(*, user=None, corporation_id=None, alliance=False, days=365):
    since = timezone.now() - timedelta(days=max(365, int(days or 365)))
    qs = AttendanceRecord.objects.filter(
        granted=True,
        operation__started_at__gte=since,
    ).select_related("operation", "operation__fleet_type", "auth_user")
    qs = apply_current_membership_filter(qs)
    if user is not None:
        qs = qs.filter(auth_user=user)
    if corporation_id is not None:
        qs = qs.filter(corporation_id=corporation_id)
    return qs.order_by("-operation__started_at", "character_name", "created_at")


def member_statistics(user, year, month):
    qs = attendance_queryset(year, month).filter(auth_user=user)
    total = qs.aggregate(total=Sum("attendance_value"))["total"] or 0
    unique_fleets = qs.values("operation_id").distinct().count()
    breakdown = defaultdict(int)
    for row in qs:
        breakdown[str(row.operation.fleet_type)] += row.attendance_value

    start, end = month_bounds(year, month)
    top_ships = list(
        FleetMemberState.objects.filter(
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
    daily = list(
        qs.values("operation__started_at__date")
        .annotate(attendance=Sum("attendance_value"))
        .order_by("operation__started_at__date")
    )
    role_counts = {
        row["role"]: row["count"]
        for row in OperationRoleAssignment.objects.filter(
            auth_user=user,
            operation__started_at__gte=start,
            operation__started_at__lt=end,
        )
        .values("role")
        .annotate(count=Count("id"))
    }
    role_labels = dict(OperationRoleAssignment.Role.choices)
    roles = [
        {"key": key, "label": role_labels.get(key, key), "count": count}
        for key, count in role_counts.items()
    ]
    fc_stats = fc_statistics(user, year, month)
    return {
        "total": total,
        "unique_fleets": unique_fleets,
        "breakdown": dict(breakdown),
        "top_ships": top_ships,
        "characters": character_rows,
        "daily": daily,
        "roles": roles,
        "fc_fleet_count": fc_stats["fleet_count"],
        "fc_points": fc_stats["total_points"],
    }


def corporation_statistics(corporation_id: int, year: int, month: int):
    qs = attendance_queryset(year, month).filter(corporation_id=corporation_id)
    total = qs.aggregate(total=Sum("attendance_value"))["total"] or 0
    main_count = corporation_main_count(corporation_id)
    breakdown = defaultdict(int)
    for row in qs:
        breakdown[str(row.operation.fleet_type)] += row.attendance_value
    averages = {name: (value / main_count if main_count else 0) for name, value in breakdown.items()}
    return {
        "corporation_id": corporation_id,
        "main_character_count": main_count,
        "total_attendance": total,
        "average_attendance": (total / main_count if main_count else 0),
        "breakdown": dict(breakdown),
        "breakdown_average": averages,
    }


def fc_operations_queryset(user, year: int, month: int, *, include_active=False):
    """Operations credited to an FC, including post-fleet special-role FC credits.

    Only Closed fleets count, matching the FC incentive rules. ``include_active``
    also counts fleets that are still running (used by the dashboard).
    Users who left the configured alliances have no FC statistics.
    """
    member_ids = current_member_user_ids()
    if member_ids is not None and user.pk not in member_ids:
        return FleetOperation.objects.none()
    statuses = [FleetOperation.Status.CLOSED]
    if include_active:
        statuses.append(FleetOperation.Status.ACTIVE)
    start, end = month_bounds(year, month)
    return (
        FleetOperation.objects.filter(started_at__gte=start, started_at__lt=end, status__in=statuses)
        .filter(
            Q(fc_user=user)
            | Q(role_assignments__auth_user=user, role_assignments__grants_fc_credit=True)
        )
        .distinct()
    )


def fc_statistics(user, year: int, month: int, *, include_active=False):
    qs = fc_operations_queryset(user, year, month, include_active=include_active)
    breakdown = defaultdict(lambda: {"count": 0, "points": 0.0})
    points = 0.0
    operations = list(qs.select_related("fleet_type"))
    for op in operations:
        key = str(op.fleet_type)
        p = float(op.fleet_point_weight_snapshot)
        breakdown[key]["count"] += 1
        breakdown[key]["points"] += p
        points += p
    return {"fleet_count": len(operations), "total_points": points, "breakdown": dict(breakdown)}
