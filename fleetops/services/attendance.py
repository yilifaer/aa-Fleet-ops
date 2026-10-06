from django.db.models import Sum

from fleetops.models import AttendanceRecord, FleetOpsSettings


def ensure_automatic_attendance(operation, identity, seen_at):
    if identity.user is None:
        return None
    existing = AttendanceRecord.objects.filter(
        operation=operation,
        character_id=identity.character_id,
        source=AttendanceRecord.Source.AUTOMATIC,
    ).first()
    if existing:
        if seen_at and (existing.last_seen is None or seen_at > existing.last_seen):
            existing.last_seen = seen_at
            existing.save(update_fields=["last_seen", "updated_at"])
        return existing

    settings = FleetOpsSettings.get_solo()
    granted_value = (
        AttendanceRecord.objects.filter(operation=operation, auth_user=identity.user, granted=True)
        .aggregate(total=Sum("attendance_value"))["total"]
        or 0
    )
    capped = settings.attendance_limit is not None and granted_value >= settings.attendance_limit
    return AttendanceRecord.objects.create(
        operation=operation,
        character_id=identity.character_id,
        character_name=identity.character_name,
        main_character_id=identity.main_character_id,
        main_character_name=identity.main_character_name,
        auth_user=identity.user,
        corporation_id=identity.corporation_id,
        corporation_name=identity.corporation_name,
        source=AttendanceRecord.Source.AUTOMATIC,
        attendance_value=max(1, int(getattr(operation, "attendance_multiplier", 1) or 1)),
        granted=not capped,
        capped=capped,
        first_seen=seen_at,
        last_seen=seen_at,
        notes="Attendance capped by per-user fleet limit." if capped else "",
    )


def create_manual_attendance(operation, *, actor, character_id: int, character_name: str = "", attendance_value: int = 1, duplicate_action: str = "keep", notes: str = ""):
    """Create a manual attendance credit.

    ``keep`` always creates a new manual row, so historical fleets can receive
    multiple corrections/credits. ``merge`` and ``replace`` operate on the
    automatic row when one exists.
    """
    from fleetops.services.audit import audit
    from fleetops.services.identity import identity_for_character_id

    identity = identity_for_character_id(character_id)
    automatic = AttendanceRecord.objects.filter(
        operation=operation,
        character_id=character_id,
        source=AttendanceRecord.Source.AUTOMATIC,
    ).first()

    if automatic and duplicate_action == "merge":
        old = {"attendance_value": automatic.attendance_value, "granted": automatic.granted}
        automatic.attendance_value += attendance_value
        automatic.granted = True
        automatic.capped = False
        automatic.notes = (automatic.notes + "\n" + notes).strip()
        automatic.save()
        audit(
            actor,
            "attendance.merge",
            automatic,
            old,
            {"attendance_value": automatic.attendance_value, "granted": True},
        )
        return automatic

    if automatic and duplicate_action == "replace":
        old = {"granted": automatic.granted}
        automatic.granted = False
        automatic.notes = (automatic.notes + "\nReplaced by manual attendance.").strip()
        automatic.save()
        audit(actor, "attendance.replace_auto", automatic, old, {"granted": False})

    manual = AttendanceRecord.objects.create(
        operation=operation,
        character_id=character_id,
        character_name=character_name or identity.character_name,
        main_character_id=identity.main_character_id,
        main_character_name=identity.main_character_name,
        auth_user=identity.user,
        corporation_id=identity.corporation_id,
        corporation_name=identity.corporation_name,
        source=AttendanceRecord.Source.MANUAL,
        attendance_value=attendance_value,
        granted=True,
        capped=False,
        first_seen=operation.started_at,
        last_seen=operation.ended_at or operation.started_at,
        notes=notes,
        created_by=actor,
    )
    audit(
        actor,
        "attendance.manual_create",
        manual,
        None,
        {"attendance_value": manual.attendance_value, "operation": str(operation.uuid)},
    )
    return manual


def set_operation_attendance_multiplier(operation, multiplier: int, *, actor=None):
    """Set 1x/2x/3x fleet-wide attendance for automatic granted rows.

    Manual attendance rows remain explicit corrections and are not multiplied.
    If an automatic row previously received a manual ``merge`` correction, the
    extra value above the old fleet multiplier is preserved.
    """
    from fleetops.services.audit import audit

    multiplier = int(multiplier)
    if multiplier not in (1, 2, 3):
        raise ValueError("Attendance multiplier must be 1, 2 or 3.")
    old = int(getattr(operation, "attendance_multiplier", 1) or 1)
    rows = list(AttendanceRecord.objects.filter(
        operation=operation,
        source=AttendanceRecord.Source.AUTOMATIC,
        granted=True,
    ))
    for row in rows:
        manual_extra = max(0, int(row.attendance_value or 0) - old)
        row.attendance_value = multiplier + manual_extra
        row.save(update_fields=["attendance_value", "updated_at"])
    operation.attendance_multiplier = multiplier
    operation.save(update_fields=["attendance_multiplier", "updated_at"])
    if old != multiplier:
        audit(
            actor,
            "attendance.multiplier",
            operation,
            {"attendance_multiplier": old},
            {"attendance_multiplier": multiplier, "automatic_rows_updated": len(rows)},
        )
    return len(rows)

