from django.db import transaction
from django.utils import timezone

from fleetops.models import FleetMemberEvent, FleetMemberState
from fleetops.providers.esi import FleetESIError, get_fleet_members
from fleetops.services.attendance import ensure_automatic_attendance
from fleetops.services.identity import resolve_many
from fleetops.services.sde import item_type_names, solar_system_names


def _event(operation, state, event_type, old_value="", new_value=""):
    FleetMemberEvent.objects.create(
        operation=operation,
        character_id=state.character_id,
        character_name=state.character_name,
        event_type=event_type,
        old_value=str(old_value or ""),
        new_value=str(new_value or ""),
    )


def sync_operation(operation):
    if not operation.esi_fleet_id:
        raise FleetESIError("NO_FLEET_ID", "Operation has no ESI fleet ID.")
    now = timezone.now()
    members = get_fleet_members(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id)
    ids = [int(m.get("character_id")) for m in members if m.get("character_id")]
    identities = resolve_many(ids)
    ship_names = item_type_names({m.get("ship_type_id") for m in members if m.get("ship_type_id")})
    system_names = solar_system_names({m.get("solar_system_id") for m in members if m.get("solar_system_id")})

    incoming = {int(m["character_id"]): m for m in members if m.get("character_id")}
    existing = {s.character_id: s for s in operation.member_states.all()}

    with transaction.atomic():
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
            state = existing.get(cid)
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

        operation.last_esi_update = now
        operation.fleet_missing_count = 0
        operation.last_error = ""
        operation.save(update_fields=["last_esi_update", "fleet_missing_count", "last_error", "updated_at"])
    return len(incoming)
