from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetMemberState, FleetType
from fleetops.services.counting import counted_attendance, counted_operation_q
from fleetops.services.history import apply_current_membership_filter
from fleetops.services.periods import current_year_month, month_bounds


def dashboard_metrics(user):
    """The cards, recent participations and ships of the dashboard; the month is the current EVE month."""
    now = timezone.now()
    month_start, month_end = month_bounds(*current_year_month(now))
    attendance = apply_current_membership_filter(
        counted_attendance(AttendanceRecord.objects.filter(auth_user=user)).select_related("operation__fleet_type")
    )
    types = list(FleetType.objects.filter(is_active=True).order_by("sort_order", "name")[:4])
    # One grouped query for the month, the last 30 and the last 90 days of every card.
    since = min(month_start, now - timedelta(days=90))
    in_month = Q(operation__started_at__gte=month_start, operation__started_at__lt=month_end)
    in_30 = Q(operation__started_at__gte=now - timedelta(days=30))
    in_90 = Q(operation__started_at__gte=now - timedelta(days=90))
    windows = {}
    if types:
        windows = {
            row["operation__fleet_type_id"]: row
            for row in attendance.filter(operation__fleet_type__in=types, operation__started_at__gte=since)
            .values("operation__fleet_type_id")
            .annotate(
                month=Sum("attendance_value", filter=in_month),
                last30=Sum("attendance_value", filter=in_30),
                last90=Sum("attendance_value", filter=in_90),
            )
            .order_by()
        }
    cards = []
    for fleet_type in types:
        row = windows.get(fleet_type.pk, {})
        cards.append(
            {
                "fleet_type": fleet_type,
                "month": row.get("month") or 0,
                "last30": row.get("last30") or 0,
                "last90": row.get("last90") or 0,
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
        FleetMemberState.objects.filter(counted_operation_q("operation__"), auth_user=user)
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
