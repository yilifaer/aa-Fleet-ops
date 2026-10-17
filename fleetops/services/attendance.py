# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.db import transaction

from fleetops import texts
from fleetops.models import MAX_MULTIPLIER, AttendanceRecord, FleetOperation, ReviewStatus

# Fleets that accept manual PAP and special-role assignments.
RECORDABLE_STATUSES = (FleetOperation.Status.ACTIVE, FleetOperation.Status.CLOSED)


def manual_row_granted(review_status, correction, has_managed_row):
    """Whether a manual or imported row counts by itself.

    Rows waiting for review or rejected never count. A merge adds its value to the tracked PAP of
    the character; with no tracked PAP to merge into it counts as a separate credit.
    """
    if review_status != ReviewStatus.APPROVED:
        return False
    if correction == AttendanceRecord.Correction.MERGE:
        return not has_managed_row
    return True


def create_manual_attendance(
    operation,
    *,
    actor,
    character_id: int,
    character_name: str = "",
    attendance_value: int = 1,
    duplicate_action: str = "keep",
    notes: str = "",
    source: str = AttendanceRecord.Source.MANUAL,
    refresh: bool = True,
):
    """Create one manual (or, with ``source`` IMPORTED, pasted roster) PAP row.

    ``duplicate_action`` is the correction the row makes to the tracked PAP of the same character:
    ``keep`` is a separate credit, ``replace`` takes the place of the tracked PAP and ``merge`` adds
    its value to it. The tracked rows themselves are only written by the PAP engine, so deleting
    the row and recalculating undoes the correction. Rows of users who are not FC leads wait for
    approval. A caller adding many rows passes ``refresh=False`` and recalculates the fleet once.
    """
    from fleetops.services.identity import identity_for_character_id
    from fleetops.services.pap import refresh_after_change
    from fleetops.services.review import initial_review_fields

    if source not in AttendanceRecord.USER_SOURCES:
        raise ValueError(texts.PAP_MANUAL_ENTRY())
    if duplicate_action not in AttendanceRecord.Correction.values:
        raise ValueError(texts.PAP_CHOOSE_DUPLICATE())
    identity = identity_for_character_id(character_id)
    review = initial_review_fields(actor)
    with transaction.atomic():
        # The caller's copy may be older than a close that is running now. Waiting for it, the row
        # is applied by the recalculation below instead of being missed by the close.
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        operation.status, operation.ended_at = current.status, current.ended_at
        if operation.status not in RECORDABLE_STATUSES:
            raise ValueError(texts.PAP_ONLY_ACTIVE_OR_CLOSED())
        manual = _create_manual_row(
            operation,
            actor=actor,
            identity=identity,
            character_id=character_id,
            character_name=character_name,
            attendance_value=attendance_value,
            duplicate_action=duplicate_action,
            notes=notes,
            source=source,
            review=review,
        )
        if refresh:
            refresh_after_change(operation, actor=actor, reason="manual PAP")
            manual.refresh_from_db()
    return manual


def _create_manual_row(
    operation,
    *,
    actor,
    identity,
    character_id,
    character_name,
    attendance_value,
    duplicate_action,
    notes,
    source,
    review,
):
    from fleetops.services.audit import audit

    has_managed_row = AttendanceRecord.objects.filter(
        operation=operation, character_id=character_id, source__in=AttendanceRecord.MANAGED_SOURCES
    ).exists()
    manual = AttendanceRecord.objects.create(
        operation=operation,
        character_id=character_id,
        character_name=character_name or identity.character_name,
        main_character_id=identity.main_character_id,
        main_character_name=identity.main_character_name,
        auth_user=identity.user,
        corporation_id=identity.corporation_id,
        corporation_name=identity.corporation_name,
        source=source,
        attendance_value=attendance_value,
        granted=manual_row_granted(review["review_status"], duplicate_action, has_managed_row),
        capped=False,
        correction=duplicate_action,
        first_seen=operation.started_at,
        last_seen=operation.ended_at or operation.started_at,
        notes=notes,
        created_by=actor,
        **review,
    )
    audit(
        actor,
        "attendance.manual_create",
        manual,
        None,
        {
            "attendance_value": manual.attendance_value,
            "operation": str(operation.uuid),
            "correction": manual.correction,
            "review_status": manual.review_status,
        },
    )
    return manual


def set_operation_attendance_multiplier(operation, multiplier: int, *, actor=None, source="fc", reason=""):
    """Set the PAP multiplier of a fleet and recalculate its PAP when it is closed.

    Manual rows keep their own values; tracked PAP is the multiplier plus any merged extra.
    Choosing a multiplier, even the current one, clears a pending choice. ``source`` says how the
    multiplier was set (a ``FleetOperation.MultiplierSource``); ``reason`` is required by the
    callers for overrides beyond the duration rule. Returns the PAP summary of a closed fleet,
    otherwise None.
    """
    from fleetops.services import incentives
    from fleetops.services.audit import audit
    from fleetops.services.pap import refresh_after_change

    try:
        multiplier = int(multiplier)
    except (TypeError, ValueError):
        raise ValueError(texts.PAP_MULTIPLIER_RANGE(max=MAX_MULTIPLIER)) from None
    if not 1 <= multiplier <= MAX_MULTIPLIER:
        raise ValueError(texts.PAP_MULTIPLIER_RANGE(max=MAX_MULTIPLIER))
    if source not in FleetOperation.MultiplierSource.values:
        raise ValueError("Unknown multiplier source.")
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        old = {
            "attendance_multiplier": current.attendance_multiplier,
            "multiplier_source": current.multiplier_source,
            "multiplier_pending": current.multiplier_pending,
        }
        operation.attendance_multiplier = multiplier
        operation.multiplier_source = source
        operation.multiplier_pending = False
        operation.save(update_fields=["attendance_multiplier", "multiplier_source", "multiplier_pending", "updated_at"])
        summary = refresh_after_change(operation, actor=actor, reason="multiplier change")
        if old["attendance_multiplier"] != multiplier or old["multiplier_pending"]:
            new = {
                "attendance_multiplier": multiplier,
                "multiplier_source": source,
                "multiplier_pending": False,
                "pending_cleared": old["multiplier_pending"],
            }
            if summary is not None:
                new["pap"] = summary
            audit(actor, "attendance.multiplier", operation, old, new, reason=reason)
    # The FC points of the month follow the multiplier, also when the choice only cleared the pending state.
    incentives.refresh_months_after_commit(*incentives.months_of(operation), actor=actor)
    return summary
