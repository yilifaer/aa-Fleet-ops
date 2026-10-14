# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.db.models import Q

from fleetops import texts
from fleetops.models import AttendanceRecord, OperationRoleAssignment, ReviewStatus, SpecialRole
from fleetops.services.attendance import RECORDABLE_STATUSES
from fleetops.services.audit import audit
from fleetops.services.identity import identity_for_character_id

# Stored message (English); the page shows it through ``translate_stored``.
NOT_IN_FLEET = texts.SPECIAL_ROLE_NOT_IN_FLEET()


def _special_role(role) -> SpecialRole:
    """The role itself, or the role with the given key."""
    if isinstance(role, SpecialRole):
        return role
    found = SpecialRole.objects.filter(key=str(role or "")).first()
    if found is None:
        raise ValueError(texts.SPECIAL_ROLE_UNKNOWN())
    return found


def participant_rows(operation):
    """PAP rows that show a character took part: tracked and PAP link rows, and accepted corrections.

    Manual and imported rows an FC lead rejected or has not approved do not, except while the fleet
    itself waits for approval: its roles count only once an FC lead approves the fleet with its rows.
    """
    accepted = [ReviewStatus.APPROVED]
    if operation.review_status == ReviewStatus.PENDING:
        accepted.append(ReviewStatus.PENDING)
    return operation.attendance_records.filter(
        Q(source__in=AttendanceRecord.MANAGED_SOURCES)
        | Q(source__in=AttendanceRecord.USER_SOURCES, review_status__in=accepted)
    )


def is_fleet_member(operation, character_id) -> bool:
    """Whether the character is recorded in the fleet: tracked, registered by PAP link or with PAP."""
    return any(
        source.filter(character_id=character_id).exists()
        for source in (operation.member_states, operation.pap_registrations, participant_rows(operation))
    )


def add_role_assignment(operation, *, actor, role, character_id: int, notes: str = ""):
    """Assign a special role (a ``SpecialRole`` or its key) to a character in the fleet.

    The role's "Gives FC credit" switch alone decides whether the fleet counts as an FC fleet for
    the pilot's user; managing the fleet still needs the FC permissions. The credit is stored on the
    assignment, so later changes of the role only apply to new assignments.
    """
    if operation.status not in RECORDABLE_STATUSES:
        raise ValueError(texts.SPECIAL_ROLE_ONLY_ACTIVE_OR_CLOSED())
    role = _special_role(role)
    if not role.is_active:
        raise ValueError(texts.SPECIAL_ROLE_DISABLED(name=role.name))
    if not is_fleet_member(operation, character_id):
        # Pending or rejected manual PAP is no proof of taking part, so it gives no FC credit.
        raise ValueError(NOT_IN_FLEET)
    identity = identity_for_character_id(character_id)
    grants_fc_credit = bool(role.grants_fc_credit and identity.user is not None)
    assignment, created = OperationRoleAssignment.objects.update_or_create(
        operation=operation,
        role=role.key,
        character_id=character_id,
        defaults={
            "special_role": role,
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
        _audit_values(assignment),
    )
    return assignment


def _audit_values(assignment):
    return {
        "role": assignment.role,
        "role_name": assignment.role_label,
        "character_id": assignment.character_id,
        "grants_fc_credit": assignment.grants_fc_credit,
    }


def delete_role_assignment(assignment, *, actor):
    audit(actor, "fleet.role_delete", assignment, _audit_values(assignment), None)
    assignment.delete()
