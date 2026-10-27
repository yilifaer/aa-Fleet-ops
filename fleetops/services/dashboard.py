# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from datetime import timedelta

from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetMemberState, FleetType, OperationRoleAssignment
from fleetops.services import group_access
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
    _add_member_counts(recent)

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


def _add_member_counts(recent) -> None:
    """Set ``member_count`` on the recent rows: the characters that were granted PAP in the fleet (one query)."""
    if not recent:
        return
    operation_ids = [row["operation"].pk for row in recent]
    counts = {
        row["operation_id"]: row["members"]
        for row in counted_attendance(AttendanceRecord.objects.filter(operation_id__in=operation_ids))
        .values("operation_id")
        .annotate(members=Count("pk"))
        .order_by()
    }
    for row in recent:
        row["member_count"] = counts.get(row["operation"].pk, 0)


def running_fleet_rows(user, operations) -> list[dict]:
    """The running fleets of the home page, one row per fleet of ``operations`` (already limited to what
    ``user`` may see).

    Each row has ``operation``, ``can_manage``, ``show_system``, ``fc_system_name`` and ``member_count``.
    ``can_manage`` is the rule of the fleet page (``views._can_manage_operation``) and decides who gets the
    command deck link; ``show_system`` is the rule of the live tree (managers, ``view_all_fleets`` and
    ``manage_fleets``), so a member never sees where the FC is (K3, S14), and a leader who did not start a
    group fleet sees it read-only. The FC's system is one query for all rows that may show it, and the roles
    that give FC credit are one query for all rows, whatever the number of fleets.
    """
    rows = list(operations)
    if not rows:
        return []
    manage_all = user.has_perm("fleetops.manage_fleets")
    view_all = manage_all or user.has_perm("fleetops.view_all_fleets")
    credited = set()
    if not manage_all and user.has_perm("fleetops.manage_own_fleet"):
        others = [operation.pk for operation in rows if operation.fc_user_id != user.pk]
        if others:
            credited = set(
                OperationRoleAssignment.objects.filter(
                    operation_id__in=others, auth_user=user, grants_fc_credit=True
                ).values_list("operation_id", flat=True)
            )
    manage_own = user.has_perm("fleetops.manage_own_fleet")
    result = []
    for operation in rows:
        can_manage = (
            manage_all
            or (manage_own and (operation.fc_user_id == user.pk or operation.pk in credited))
            or group_access.manages_group_fleet(user, operation)
        )
        result.append(
            {
                "operation": operation,
                "can_manage": bool(can_manage),
                "show_system": bool(view_all or can_manage),
                "fc_system_name": "",
                "member_count": getattr(operation, "active_member_count", 0),
            }
        )
    visible = [row["operation"].pk for row in result if row["show_system"]]
    if visible:
        systems = dict(
            FleetMemberState.objects.filter(
                operation_id__in=visible, character_id=F("operation__fc_character_id"), is_active=True
            ).values_list("operation_id", "solar_system_name")
        )
        for row in result:
            row["fc_system_name"] = systems.get(row["operation"].pk, "") or ""
    return result
