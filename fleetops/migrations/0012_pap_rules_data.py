# AA FleetOps 0.2.0b1: seed the multiplier tiers and the special roles, keep the PAP selection of finished
# fleets, approximate the time in fleet of tracked pilots and turn merged and replaced attendance into the
# new correction fields

from django.db import migrations

BATCH_SIZE = 500
# Largest value a PositiveIntegerField holds on every supported database.
MAX_SECONDS = 2**31 - 1
# (threshold in minutes, multiplier): the multipliers FCs could choose before the tiers were configurable.
DEFAULT_TIERS = ((90, 2), (180, 3))
# (key, name, gives FC credit, sort order): the fixed roles of earlier versions.
DEFAULT_ROLES = (
    ("backseat_fc", "Back Seat FC", True, 10),
    ("logi_anchor", "Logi Anchor", False, 20),
    ("snowflake", "Snowflake Member", False, 30),
)
UNKNOWN_ROLE_KEY = "unknown"
# Fleets finished before the upgrade keep the PAP selection they were closed with.
LEGACY_STATUSES = ("closed", "cancelled", "error")
LEGACY_PAP_RULES = 1
# Note earlier versions added to an automatic row that a manual "replace" entry took over.
REPLACED_NOTE = "Replaced by manual attendance."


def _bulk_update(model, rows, fields):
    if rows:
        model.objects.bulk_update(rows, fields, batch_size=BATCH_SIZE)
    return []


def seed_multiplier_tiers(apps, schema_editor):
    MultiplierTier = apps.get_model("fleetops", "MultiplierTier")
    if not MultiplierTier.objects.exists():
        MultiplierTier.objects.bulk_create(
            MultiplierTier(threshold_minutes=minutes, multiplier=multiplier) for minutes, multiplier in DEFAULT_TIERS
        )


def _role_name(key, taken):
    name = " ".join(key.replace("_", " ").replace("-", " ").split()).title() or "Unknown role"
    if name.lower() in taken:
        name = f"{name} ({key})"
    return name[:100]


def link_special_roles(apps, schema_editor):
    # Every assignment keeps its role key and is linked to the role row with that key. Assignments keep
    # their FC credit snapshot, so statistics of earlier fleets do not change.
    SpecialRole = apps.get_model("fleetops", "SpecialRole")
    OperationRoleAssignment = apps.get_model("fleetops", "OperationRoleAssignment")
    for key, name, grants_fc_credit, sort_order in DEFAULT_ROLES:
        SpecialRole.objects.get_or_create(
            key=key, defaults={"name": name, "grants_fc_credit": grants_fc_credit, "sort_order": sort_order}
        )
    # Keys are compared without case, as MariaDB's unique index does.
    roles = {role.key.lower(): role for role in SpecialRole.objects.all()}
    names = {role.name.lower() for role in roles.values()}
    unlinked = OperationRoleAssignment.objects.filter(special_role=None)
    for value in unlinked.order_by("role").values_list("role", flat=True).distinct():
        key = (value or UNKNOWN_ROLE_KEY)[:30]
        role = roles.get(key.lower())
        if role is None:
            # A key FleetOps never offered; the role is kept for the page but not offered to FCs.
            role = SpecialRole.objects.create(
                key=key, name=_role_name(key, names), grants_fc_credit=False, is_active=False, sort_order=100
            )
            roles[key.lower()] = role
            names.add(role.name.lower())
        unlinked.filter(role=value).update(special_role=role)


def snapshot_pap_rules(apps, schema_editor):
    # Earlier versions granted attendance on any presence and capped it with the current setting.
    FleetOpsSettings = apps.get_model("fleetops", "FleetOpsSettings")
    FleetOperation = apps.get_model("fleetops", "FleetOperation")
    limit = FleetOpsSettings.objects.filter(pk=1).values_list("attendance_limit", flat=True).first()
    FleetOperation.objects.filter(status__in=LEGACY_STATUSES).update(pap_rules=LEGACY_PAP_RULES)
    FleetOperation.objects.update(pap_character_limit=limit, pap_minimum_minutes=0)


def approximate_time_in_fleet(apps, schema_editor):
    # Earlier versions did not add up the time in fleet; from first to last seen is the closest estimate.
    FleetMemberState = apps.get_model("fleetops", "FleetMemberState")
    batch = []
    states = FleetMemberState.objects.order_by("pk").values_list("pk", "first_seen", "last_seen")
    for pk, first_seen, last_seen in states.iterator(chunk_size=BATCH_SIZE):
        if first_seen is None or last_seen is None:
            continue
        seconds = min(max(0, int((last_seen - first_seen).total_seconds())), MAX_SECONDS)
        if seconds:
            batch.append(FleetMemberState(pk=pk, accumulated_seconds=seconds))
        if len(batch) >= BATCH_SIZE:
            batch = _bulk_update(FleetMemberState, batch, ["accumulated_seconds"])
    _bulk_update(FleetMemberState, batch, ["accumulated_seconds"])


def keep_merged_attendance(apps, schema_editor):
    # A manual "merge" entry used to add its value to the automatic row; the part above the fleet
    # multiplier is that merged extra.
    AttendanceRecord = apps.get_model("fleetops", "AttendanceRecord")
    batch = []
    rows = (
        AttendanceRecord.objects.filter(source="automatic", granted=True)
        .order_by("pk")
        .values_list("pk", "attendance_value", "operation__attendance_multiplier")
    )
    for pk, value, multiplier in rows.iterator(chunk_size=BATCH_SIZE):
        extra = max(0, int(value or 0) - int(multiplier or 1))
        if extra:
            batch.append(AttendanceRecord(pk=pk, extra_value=extra, legacy_extra_value=extra))
        if len(batch) >= BATCH_SIZE:
            batch = _bulk_update(AttendanceRecord, batch, ["extra_value", "legacy_extra_value"])
    _bulk_update(AttendanceRecord, batch, ["extra_value", "legacy_extra_value"])


def _replacing_rows(AuditLog, automatic_pk, manual_pks):
    # Each "replace" was audited on the automatic row and created its manual row right after it, so the
    # next manual row the same user created is the replacement. Without those audit entries every manual
    # row is marked: the automatic row then stays replaced while any of them is left, as it did before.
    replaces = AuditLog.objects.filter(
        action="attendance.replace_auto", object_type="AttendanceRecord", object_id=str(automatic_pk)
    ).order_by("pk")
    found = []
    for replace_pk, actor_id in replaces.values_list("pk", "actor_id"):
        created = (
            AuditLog.objects.filter(
                action="attendance.manual_create", object_type="AttendanceRecord", pk__gt=replace_pk, actor_id=actor_id
            )
            .order_by("pk")
            .values_list("object_id", flat=True)
            .first()
        )
        if created is not None and created.isdigit() and int(created) in manual_pks:
            found.append(int(created))
    return found or manual_pks


def keep_replaced_attendance(apps, schema_editor):
    # A manual "replace" entry used to switch the automatic row off, also when the row was capped, and
    # add a manual row. The automatic row is marked replaced and the manual row the replace added becomes
    # the replacing correction; earlier and later manual rows of the character stay separate credits.
    AttendanceRecord = apps.get_model("fleetops", "AttendanceRecord")
    AuditLog = apps.get_model("fleetops", "AuditLog")
    replaced, corrections = [], []
    rows = (
        AttendanceRecord.objects.filter(source="automatic", granted=False, notes__contains=REPLACED_NOTE)
        .order_by("pk")
        .values_list("pk", "operation_id", "character_id")
    )
    for pk, operation_id, character_id in rows.iterator(chunk_size=BATCH_SIZE):
        replaced.append(AttendanceRecord(pk=pk, replaced=True))
        manual_pks = list(
            AttendanceRecord.objects.filter(operation_id=operation_id, character_id=character_id, source="manual")
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if manual_pks:
            corrections.extend(
                AttendanceRecord(pk=manual_pk, correction="replace")
                for manual_pk in _replacing_rows(AuditLog, pk, manual_pks)
            )
        if len(replaced) >= BATCH_SIZE:
            replaced = _bulk_update(AttendanceRecord, replaced, ["replaced"])
            corrections = _bulk_update(AttendanceRecord, corrections, ["correction"])
    _bulk_update(AttendanceRecord, replaced, ["replaced"])
    _bulk_update(AttendanceRecord, corrections, ["correction"])


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0011_pap_rules_schema"),
    ]

    # Review status, statistics inclusion and the other new fields keep their defaults: existing fleets
    # and attendance stay approved and counted.
    operations = [
        migrations.RunPython(seed_multiplier_tiers, migrations.RunPython.noop),
        migrations.RunPython(link_special_roles, migrations.RunPython.noop),
        migrations.RunPython(snapshot_pap_rules, migrations.RunPython.noop),
        migrations.RunPython(approximate_time_in_fleet, migrations.RunPython.noop),
        migrations.RunPython(keep_merged_attendance, migrations.RunPython.noop),
        migrations.RunPython(keep_replaced_attendance, migrations.RunPython.noop),
    ]
