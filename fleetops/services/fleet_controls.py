from allianceauth.services.hooks import get_extension_logger

from fleetops.models import FleetMemberState
from fleetops.providers.esi import kick_fleet_member
from fleetops.services.audit import audit
from fleetops.services.errors import report_failure
from fleetops.services.operations import set_action

logger = get_extension_logger(__name__)

# Known Capsule group type IDs commonly seen in Fleet ESI. Name fallback keeps
# this safe if a future capsule type is added before local SDE data catches up.
KNOWN_CAPSULE_TYPE_IDS = {670, 33328}


def is_capsule_member(member: FleetMemberState) -> bool:
    name = (member.ship_type_name or "").strip().lower()
    return member.ship_type_id in KNOWN_CAPSULE_TYPE_IDS or name == "capsule" or name.endswith(" capsule")


def pod_members(operation):
    return [m for m in operation.member_states.filter(is_active=True) if is_capsule_member(m)]


def kickable_pod_members(operation):
    """Capsule pilots that Kick Capsules removes: never the FC or the fleet boss.

    The button count and the kick both use this list, so the count is what actually happens.
    """
    protected = {operation.fc_character_id, operation.fleet_boss_character_id} - {None}
    return [m for m in pod_members(operation) if m.character_id not in protected]


def kick_all_pods(operation, *, actor):
    if not operation.esi_fleet_id:
        raise ValueError("Operation has no ESI fleet ID.")
    targets = kickable_pod_members(operation)
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
            failures.append({"character": member.character_name, "error": report_failure(logger, "Capsule kick", exc, operation)})
    message = f"Kicked {len(successes)} capsule member(s)."
    if failures:
        details = "; ".join(f"{failure['character']}: {failure['error']}" for failure in failures[:5])
        message += f" {len(failures)} failed. {details}"
    set_action(operation, "kick_capsules", not failures, message)
    audit(
        actor,
        "fleet.kick_capsules",
        operation,
        None,
        {"kicked": successes, "failures": failures},
    )
    return {"kicked": successes, "failures": failures, "target_count": len(targets)}
