from fleetops.models import OperationRoleAssignment
from fleetops.services.audit import audit
from fleetops.services.identity import identity_for_character_id


def add_role_assignment(operation, *, actor, role: str, character_id: int, notes: str = ""):
    identity = identity_for_character_id(character_id)
    grants_fc_credit = bool(identity.user and identity.user.has_perm("fleetops.start_fleet"))
    assignment, created = OperationRoleAssignment.objects.update_or_create(
        operation=operation,
        role=role,
        character_id=character_id,
        defaults={
            "character_name": identity.character_name,
            "main_character_id": identity.main_character_id,
            "main_character_name": identity.main_character_name,
            "auth_user": identity.user,
            "corporation_id": identity.corporation_id,
            "corporation_name": identity.corporation_name,
            "grants_fc_credit": grants_fc_credit,
            "notes": notes,
            "assigned_by": actor,
        },
    )
    audit(
        actor,
        "fleet.role_create" if created else "fleet.role_update",
        assignment,
        None,
        {
            "role": assignment.role,
            "character_id": assignment.character_id,
            "grants_fc_credit": assignment.grants_fc_credit,
        },
    )
    return assignment


def delete_role_assignment(assignment, *, actor):
    audit(
        actor,
        "fleet.role_delete",
        assignment,
        {
            "role": assignment.role,
            "character_id": assignment.character_id,
            "grants_fc_credit": assignment.grants_fc_credit,
        },
        None,
    )
    assignment.delete()
