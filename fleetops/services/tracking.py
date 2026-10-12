from allianceauth.services.hooks import get_extension_logger
from django.db import transaction
from django.utils import timezone

from fleetops.models import FleetMemberEvent, FleetMemberState, FleetOperation
from fleetops.providers.esi import FleetESIError, detect_character_fleet, get_fleet_members
from fleetops.services.attendance import ensure_automatic_attendance
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.identity import character_label, resolve_many
from fleetops.services.names import (
    is_placeholder,
    queue_name_resolution,
    stale_character_ids,
    unnamed_character_ids,
)
from fleetops.services.sde import item_type_names, solar_system_names

logger = get_extension_logger(__name__)

EndReason = FleetOperation.EndReason


def _event(operation, state, event_type, old_value="", new_value=""):
    FleetMemberEvent.objects.create(
        operation=operation,
        character_id=state.character_id,
        character_name=state.character_name,
        event_type=event_type,
        old_value=str(old_value or ""),
        new_value=str(new_value or ""),
    )


def _keep_known_details(values, state):
    """Keep the name and corporation a member without Alliance Auth account already has.

    The ESI name cache may not know the member (yet), and a placeholder never replaces a real name.
    """
    if is_placeholder(values["character_name"], state.character_id) and not is_placeholder(
        state.character_name, state.character_id
    ):
        values["character_name"] = state.character_name
    if values["corporation_id"] is None and state.corporation_id:
        for key in ("corporation_id", "corporation_name", "alliance_id", "alliance_name"):
            values[key] = getattr(state, key)


def sync_operation(operation):
    """Read the fleet members from ESI and record joins, leaves, changes and attendance.

    Returns the number of members, or None when the fleet was closed or cancelled while
    ESI was being read; nothing is written then.
    """
    if not operation.esi_fleet_id:
        raise FleetESIError("NO_FLEET_ID", "Operation has no ESI fleet ID.")
    now = timezone.now()
    members = get_fleet_members(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id)
    ids = [int(m.get("character_id")) for m in members if m.get("character_id")]
    identities = resolve_many(ids)
    ship_names = item_type_names({m.get("ship_type_id") for m in members if m.get("ship_type_id")})
    system_names = solar_system_names({m.get("solar_system_id") for m in members if m.get("solar_system_id")})

    incoming = {int(m["character_id"]): m for m in members if m.get("character_id")}

    with transaction.atomic():
        # The close of a fleet locks the same row, so a poll whose ESI read was still running
        # when the fleet ended cannot put its pilots back in fleet afterwards.
        current = (
            FleetOperation.objects.select_for_update()
            .only("status", "attendance_multiplier", "missing_reason")
            .get(pk=operation.pk)
        )
        if current.status not in FleetOperation.OPEN_STATUSES:
            return None
        operation.attendance_multiplier = current.attendance_multiplier
        existing = {s.character_id: s for s in operation.member_states.all()}

        # Missing active pilots leave.
        for cid, state in existing.items():
            if state.is_active and cid not in incoming:
                state.is_active = False
                state.left_at = now
                state.last_seen = now
                state.save(update_fields=["is_active", "left_at", "last_seen"])
                _event(operation, state, FleetMemberEvent.EventType.LEAVE)

        for cid, member in incoming.items():
            identity = identities[cid]
            state = existing.get(cid)
            defaults = {
                "character_name": identity.character_name,
                "main_character_id": identity.main_character_id,
                "main_character_name": identity.main_character_name,
                "auth_user": identity.user,
                "corporation_id": identity.corporation_id,
                "corporation_name": identity.corporation_name,
                "alliance_id": identity.alliance_id,
                "alliance_name": identity.alliance_name,
                "ship_type_id": member.get("ship_type_id"),
                "ship_type_name": ship_names.get(member.get("ship_type_id"), ""),
                "solar_system_id": member.get("solar_system_id"),
                "solar_system_name": system_names.get(member.get("solar_system_id"), ""),
                "fleet_role": member.get("role") or member.get("role_name") or "",
                "wing_id": member.get("wing_id"),
                "squad_id": member.get("squad_id"),
                "first_seen": now,
                "last_seen": now,
                "left_at": None,
                "is_active": True,
            }
            if identity.external and state is not None:
                _keep_known_details(defaults, state)
            if state is None:
                state = FleetMemberState.objects.create(operation=operation, character_id=cid, **defaults)
                _event(operation, state, FleetMemberEvent.EventType.JOIN)
            else:
                if not state.is_active:
                    state.is_active = True
                    state.left_at = None
                    _event(operation, state, FleetMemberEvent.EventType.REJOIN)
                comparisons = [
                    ("ship_type_id", "ship_type_name", FleetMemberEvent.EventType.SHIP_CHANGE),
                    ("solar_system_id", "solar_system_name", FleetMemberEvent.EventType.SYSTEM_CHANGE),
                    ("fleet_role", "fleet_role", FleetMemberEvent.EventType.ROLE_CHANGE),
                    ("wing_id", "wing_id", FleetMemberEvent.EventType.WING_CHANGE),
                    ("squad_id", "squad_id", FleetMemberEvent.EventType.SQUAD_CHANGE),
                ]
                for field, label_field, event_type in comparisons:
                    old = getattr(state, field)
                    new = defaults[field]
                    if old != new:
                        old_label = getattr(state, label_field) if label_field != field else old
                        new_label = defaults[label_field] if label_field != field else new
                        _event(operation, state, event_type, old_label or old, new_label or new)
                for key, value in defaults.items():
                    if key != "first_seen":
                        setattr(state, key, value)
                state.save()

            ensure_automatic_attendance(operation, identity, now)

        update_fields = [
            "last_esi_update", "fleet_missing_count", "missing_reason", "missing_detail", "last_error", "updated_at",
        ]
        if current.missing_reason == EndReason.BOSS_HANDOVER:
            # Only the boss can read the fleet, so the FC character holds the boss role again.
            operation.fleet_boss_character_id = operation.fc_character_id
            update_fields.append("fleet_boss_character_id")
        operation.last_esi_update = now
        operation.fleet_missing_count = 0
        operation.missing_reason = ""
        operation.missing_detail = ""
        operation.last_error = ""
        operation.save(update_fields=update_fields)

    # Members without an Alliance Auth account are named in the background, after this commit; so are
    # members who left before a lookup could name them.
    external = [cid for cid in incoming if identities[cid].external]
    try:
        queue_name_resolution(operation.pk, stale_character_ids([*external, *unnamed_character_ids(operation.pk)]))
    except Exception as exc:
        # The poll itself succeeded; names are looked up again on a later one.
        log_failure(logger, "Queueing the name lookup", exc, operation)
    return len(incoming)


class UnreadableFleet(tuple):
    """Why a fleet could not be read; unpacks to ``(reason, detail)``.

    ``boss_id`` is the character that holds the boss role after a handover, else None.
    """

    def __new__(cls, reason, detail="", boss_id=None):
        observation = super().__new__(cls, (reason, detail))
        observation.boss_id = boss_id
        return observation

    @property
    def reason(self):
        return self[0]

    @property
    def detail(self):
        return self[1]


def observe_unreadable_fleet(operation):
    """Find out why the FC token got a 404 for the fleet.

    Only the fleet boss can read a fleet, so a 404 means either the fleet is gone (disbanded,
    or the FC left it) or the FC handed the boss role to someone else. Returns an
    ``UnreadableFleet`` (``reason, detail`` plus the new boss id of a handover), or None when the
    check itself failed and the poll proves nothing.
    """
    try:
        detection = detect_character_fleet(operation.fc_user, operation.fc_character_id, fresh=True)
    except FleetESIError as exc:
        if exc.code == "NOT_IN_FLEET":
            return UnreadableFleet(EndReason.FLEET_GONE)
        return None
    except Exception as exc:
        report_failure(logger, "Fleet boss check", exc, operation)
        return None
    if detection.fleet_id == operation.esi_fleet_id and detection.fleet_boss_id and not detection.is_boss(
        operation.fc_character_id
    ):
        boss_id = int(detection.fleet_boss_id)
        return UnreadableFleet(EndReason.BOSS_HANDOVER, character_label(boss_id), boss_id)
    return UnreadableFleet(EndReason.FLEET_GONE)


def unreadable_fleet_message(reason, detail=""):
    if reason == EndReason.BOSS_HANDOVER:
        return f"Fleet boss handed over to {detail or 'another character'}."
    return "The fleet could not be read: it no longer exists or the FC left it."


def unreadable_fleet_warning(operation, settings):
    """Warning for an active fleet whose latest polls could not read it, or an empty string."""
    if not (operation.fleet_missing_count and operation.missing_reason):
        return ""
    count = operation.fleet_missing_count
    checks = f"Unreadable checks in a row: {count}."
    if operation.missing_reason == EndReason.BOSS_HANDOVER:
        what = f"Fleet boss handed over to {operation.missing_detail or 'another character'}"
        unless = "unless the boss is returned"
    else:
        what = "The fleet could not be read: it no longer exists or the FC left it"
        unless = "unless it can be read again"
    if not settings.auto_end_enabled:
        return f"{what} — automatic end is disabled, so this fleet stays open until it is ended manually. {checks}"
    remaining = max(settings.auto_end_missing_count - count, 1)
    plural = "" if remaining == 1 else "s"
    return f"{what} — this fleet will end automatically after {remaining} more check{plural} {unless}. {checks}"
