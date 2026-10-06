from fleetops.models import FleetMemberState
from fleetops.providers.esi import kick_fleet_member
from fleetops.services.audit import audit
from fleetops.services.operations import set_action

# Known Capsule group type IDs commonly seen in Fleet ESI. Name fallback keeps
# this safe if a future capsule type is added before local SDE data catches up.
KNOWN_CAPSULE_TYPE_IDS = {670, 33328}


def is_capsule_member(member: FleetMemberState) -> bool:
    name = (member.ship_type_name or "").strip().lower()
    return member.ship_type_id in KNOWN_CAPSULE_TYPE_IDS or name == "capsule" or name.endswith(" capsule")


def pod_members(operation):
    return [m for m in operation.member_states.filter(is_active=True) if is_capsule_member(m)]


def kick_all_pods(operation, *, actor):
    if not operation.esi_fleet_id:
        raise ValueError("Operation has no ESI fleet ID.")
    targets = [m for m in pod_members(operation) if m.character_id != operation.fc_character_id]
    successes = []
    failures = []
    for member in targets:
        try:
            kick_fleet_member(
                operation.fc_user,
                operation.fc_character_id,
                operation.esi_fleet_id,
                member.character_id,
            )
            successes.append(member.character_name)
        except Exception as exc:
            failures.append({"character": member.character_name, "error": str(exc)})
    message = f"Kicked {len(successes)} capsule member(s)."
    if failures:
        message += f" {len(failures)} failed."
    set_action(operation, "kick_capsules", not failures, message + (" " + str(failures[:5]) if failures else ""))
    audit(
        actor,
        "fleet.kick_capsules",
        operation,
        None,
        {"kicked": successes, "failures": failures},
    )
    return {"kicked": successes, "failures": failures, "target_count": len(targets)}
