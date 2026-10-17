# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FC lead review of manual fleets, PAP-link fleets and manual PAP.

Approvers are the holders of ``fleetops.manage_fleets`` (the FC lead bundle). Their own entries are
approved when they are created; entries of everybody else wait for an approver.
"""

from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus


def is_approver(user) -> bool:
    return bool(user is not None and user.has_perm("fleetops.manage_fleets"))


def initial_review_status(user) -> str:
    return ReviewStatus.APPROVED if is_approver(user) else ReviewStatus.PENDING


def initial_review_fields(user, now=None) -> dict:
    """Review fields of a new fleet or PAP row created by ``user``."""
    if is_approver(user):
        return {
            "review_status": ReviewStatus.APPROVED,
            "reviewed_by": user,
            "reviewed_at": now or timezone.now(),
        }
    return {"review_status": ReviewStatus.PENDING, "reviewed_by": None, "reviewed_at": None}


def deletable_manual_rows(user, rows):
    """The manual and imported PAP rows of ``rows`` (a queryset) that ``user`` may delete.

    Approvers delete any row. Everybody else deletes only their own rows that no approver accepted
    (pending or rejected), so a deletion never undoes an approved correction without an FC lead.
    """
    if is_approver(user):
        return rows
    return rows.filter(
        created_by_id=getattr(user, "pk", None), review_status__in=(ReviewStatus.PENDING, ReviewStatus.REJECTED)
    )


def can_delete_manual_row(user, record) -> bool:
    """Whether ``user`` may delete the manual or imported PAP row ``record``; see ``deletable_manual_rows``."""
    if is_approver(user):
        return True
    return record.created_by_id == getattr(user, "pk", None) and record.review_status in (
        ReviewStatus.PENDING,
        ReviewStatus.REJECTED,
    )


def pending_operations():
    return FleetOperation.objects.filter(review_status=ReviewStatus.PENDING)


def pending_attendance():
    return AttendanceRecord.objects.filter(review_status=ReviewStatus.PENDING)


def pending_approval_counts() -> dict:
    """Fleets and PAP rows waiting for an approver, counted apart: ``{"fleets": n, "pap": m}``."""
    return {"fleets": pending_operations().count(), "pap": pending_attendance().count()}


def pending_approval_count() -> int:
    """Fleets and PAP rows waiting for an approver."""
    return sum(pending_approval_counts().values())
