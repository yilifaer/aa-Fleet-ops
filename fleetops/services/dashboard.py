from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetMemberState, FleetType
from fleetops.services.history import apply_current_membership_filter
from fleetops.services.statistics import month_bounds


def dashboard_metrics(user):
    now = timezone.now()
    month_start, _ = month_bounds(now.year, now.month)
    attendance = apply_current_membership_filter(
        AttendanceRecord.objects.filter(auth_user=user, granted=True).select_related("operation__fleet_type")
    )
    types = list(FleetType.objects.filter(is_active=True).order_by("sort_order", "name")[:4])
    cards = []
    for fleet_type in types:
        base = attendance.filter(operation__fleet_type=fleet_type)
        cards.append(
            {
                "fleet_type": fleet_type,
                "month": base.filter(operation__started_at__gte=month_start).aggregate(v=Sum("attendance_value"))["v"] or 0,
                "last30": base.filter(operation__started_at__gte=now - timedelta(days=30)).aggregate(v=Sum("attendance_value"))["v"] or 0,
                "last90": base.filter(operation__started_at__gte=now - timedelta(days=90)).aggregate(v=Sum("attendance_value"))["v"] or 0,
            }
        )

    # Aggregate last 10 operation participations rather than individual alt rows.
    rows = attendance.order_by("-operation__started_at", "operation_id", "created_at")
    grouped = {}
    for row in rows[:500]:
        if row.operation_id not in grouped and len(grouped) >= 10:
            break
        item = grouped.setdefault(
            row.operation_id,
            {
                "operation": row.operation,
                "attendance": 0,
                "character_names": [],
            },
        )
        item["attendance"] += row.attendance_value
        item["character_names"].append(row.character_name)
    recent = list(grouped.values())

    top_ship_rows = (
        FleetMemberState.objects.filter(auth_user=user)
        .exclude(ship_type_id=None)
        .values("ship_type_id", "ship_type_name")
        .annotate(uses=Count("operation_id", distinct=True))
        .order_by("-uses", "ship_type_name")[:8]
    )
    top_ships = [
        {
            **row,
            "image_url": f"https://images.evetech.net/types/{row['ship_type_id']}/render?size=128",
        }
        for row in top_ship_rows
    ]
    return {"cards": cards, "recent": recent, "top_ships": top_ships}
