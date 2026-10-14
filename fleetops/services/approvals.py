# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC lead approval of manual fleets and manual PAP.

A fleet and each manual or imported PAP row carry their own review status. A PAP row only counts
when both are approved, so rejecting a fleet is enough to keep its PAP out of the statistics.
"""

from django.db import transaction
from django.utils import timezone

from fleetops import texts
from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus
from fleetops.services import pap
from fleetops.services.audit import audit

# Stored messages (English); the pages show them through ``translate_stored``.
REJECTION_NOTE_REQUIRED = texts.APPROVAL_REASON_REQUIRED()
ALREADY_APPROVED = texts.APPROVAL_ALREADY_APPROVED()
NOT_PENDING = texts.APPROVAL_NOT_PENDING()


def _note(note):
    return (note or "").strip()[:255]


def _review_rows(rows, status, *, actor, note, now):
    """Set the review fields of ``rows`` (a list of records) and save them."""
    for row in rows:
        row.review_status = status
        row.reviewed_by = actor
        row.reviewed_at = now
        row.review_note = note
    AttendanceRecord.objects.bulk_update(rows, ["review_status", "reviewed_by", "reviewed_at", "review_note"])


def _set_operation_review(operation, status, *, actor, note, include_attendance, from_statuses, refusal):
    """Decide on a fleet in ``from_statuses`` (else ValueError ``refusal``), with its pending PAP rows."""
    now = timezone.now()
    with transaction.atomic():
        locked = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if locked.review_status not in from_statuses:
            raise ValueError(refusal)
        old_status = locked.review_status
        locked.review_status = status
        locked.reviewed_by = actor
        locked.reviewed_at = now
        locked.review_note = note
        locked.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
        rows = []
        if include_attendance:
            rows = list(
                AttendanceRecord.objects.select_for_update()
                .filter(
                    operation=locked,
                    source__in=AttendanceRecord.USER_SOURCES,
                    review_status=ReviewStatus.PENDING,
                )
                .order_by("pk")
            )
            _review_rows(rows, status, actor=actor, note=note, now=now)
        action = "approval.operation_approve" if status == ReviewStatus.APPROVED else "approval.operation_reject"
        audit(
            actor,
            action,
            locked,
            {"review_status": old_status},
            {"review_status": status, "note": note, "attendance_ids": [row.pk for row in rows]},
        )
    for field in ("review_status", "reviewed_by", "reviewed_at", "review_note"):
        setattr(operation, field, getattr(locked, field))
    pap.refresh_after_change(locked, actor=actor, reason=action)
    return len(rows)


def approve_operation(operation, *, actor, note="", include_attendance=True):
    """Approve a pending or rejected fleet, and its pending PAP rows when ``include_attendance``.

    Returns how many PAP rows were approved with it.
    """
    return _set_operation_review(
        operation,
        ReviewStatus.APPROVED,
        actor=actor,
        note=_note(note),
        include_attendance=include_attendance,
        from_statuses=(ReviewStatus.PENDING, ReviewStatus.REJECTED),
        refusal=ALREADY_APPROVED,
    )


def reject_operation(operation, *, actor, note, include_attendance=True):
    """Reject a pending fleet; it stays visible but does not count.

    Its pending PAP rows are rejected with it when ``include_attendance``. Returns how many.
    """
    note = _note(note)
    if not note:
        raise ValueError(REJECTION_NOTE_REQUIRED)
    return _set_operation_review(
        operation,
        ReviewStatus.REJECTED,
        actor=actor,
        note=note,
        include_attendance=include_attendance,
        from_statuses=(ReviewStatus.PENDING,),
        refusal=NOT_PENDING,
    )


def _set_attendance_review(records, status, *, actor, note, from_statuses):
    ids = {getattr(record, "pk", record) for record in records}
    now = timezone.now()
    action = "approval.attendance_approve" if status == ReviewStatus.APPROVED else "approval.attendance_reject"
    by_operation = {}
    with transaction.atomic():
        # The fleets first, then their rows: the order in which closing a fleet and recalculating its
        # PAP lock them, so an approval during a close waits for it instead of deadlocking.
        operation_ids = set(AttendanceRecord.objects.filter(pk__in=ids).values_list("operation_id", flat=True))
        operations = {
            operation.pk: operation
            for operation in FleetOperation.objects.select_for_update().filter(pk__in=operation_ids).order_by("pk")
        }
        rows = list(
            AttendanceRecord.objects.select_for_update()
            .filter(pk__in=ids, source__in=AttendanceRecord.USER_SOURCES, review_status__in=from_statuses)
            .order_by("pk")
        )
        old_statuses = {str(row.pk): row.review_status for row in rows}
        _review_rows(rows, status, actor=actor, note=note, now=now)
        for row in rows:
            row.operation = operations[row.operation_id]
            by_operation.setdefault(row.operation_id, (row.operation, []))[1].append(row)
        for operation, operation_rows in by_operation.values():
            audit(
                actor,
                action,
                operation,
                {"review_status": {str(row.pk): old_statuses[str(row.pk)] for row in operation_rows}},
                {"review_status": status, "note": note, "attendance_ids": [row.pk for row in operation_rows]},
            )
    for operation, _rows in by_operation.values():
        pap.refresh_after_change(operation, actor=actor, reason=action)
    return len(rows)


def approve_attendance(records, *, actor, note=""):
    """Approve pending or rejected manual PAP rows, of one or more fleets; returns how many changed."""
    return _set_attendance_review(
        records,
        ReviewStatus.APPROVED,
        actor=actor,
        note=_note(note),
        from_statuses=(ReviewStatus.PENDING, ReviewStatus.REJECTED),
    )


def reject_attendance(records, *, actor, note):
    """Reject pending manual PAP rows, of one or more fleets; returns how many changed."""
    note = _note(note)
    if not note:
        raise ValueError(REJECTION_NOTE_REQUIRED)
    return _set_attendance_review(
        records, ReviewStatus.REJECTED, actor=actor, note=note, from_statuses=(ReviewStatus.PENDING,)
    )
