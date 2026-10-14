# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet records: the FC lead approval queue, roster imports and the fleet page parts for them."""

from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from fleetops.forms import ManualAttendanceForm, format_duration
from fleetops.forms_records import ReviewForm, RosterPasteForm
from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus, SpecialRole
from fleetops.services import approvals, roster_import
from fleetops.services.attendance import RECORDABLE_STATUSES
from fleetops.services.review import deletable_manual_rows, is_approver, pending_attendance, pending_operations
from fleetops.texts import translate_stored
from fleetops.views import MANUAL_ATTENDANCE_ENTRY_KEY, _can_manage_attendance_operation, _render

# Pending PAP rows listed on the approvals page at once.
QUEUE_ROWS_SHOWN = 500


def records_context(request, operation):
    """What the fleet page's review, manual PAP and special role parts need."""
    user = request.user
    approver = is_approver(user)
    manual_rows = operation.attendance_records.filter(source__in=AttendanceRecord.USER_SOURCES)
    context = {
        "can_review": approver,
        "needs_approval": not approver,
        "pending_pap_count": manual_rows.filter(review_status=ReviewStatus.PENDING).count(),
        "rejected_pap_count": manual_rows.filter(review_status=ReviewStatus.REJECTED).count(),
        # Approvers decide on the fleet's manual PAP rows right on the fleet page.
        "review_rows": (
            list(manual_rows.exclude(review_status=ReviewStatus.APPROVED).select_related("created_by"))
            if approver
            else []
        ),
        # The manual PAP rows the user may delete.
        "deletable_row_ids": (
            set(deletable_manual_rows(user, manual_rows).values_list("pk", flat=True))
            if _can_manage_attendance_operation(user, operation)
            else set()
        ),
        "role_assignments": operation.role_assignments.select_related("auth_user", "assigned_by", "special_role"),
        "has_active_roles": SpecialRole.objects.filter(is_active=True).exists(),
    }
    entry = request.session.get(MANUAL_ATTENDANCE_ENTRY_KEY)
    if entry and entry.get("operation") == str(operation.uuid):
        del request.session[MANUAL_ATTENDANCE_ENTRY_KEY]
        context["manual_attendance_form"] = ManualAttendanceForm(initial=entry.get("values") or {})
        context["manual_attendance_errors"] = entry.get("errors") or []
    return context


def _back(request, default):
    """The page the form came from, or ``default``."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect(default)


def _form_errors(form):
    return " ".join(str(error) for errors in form.errors.values() for error in errors)


@permission_required("fleetops.manage_fleets", raise_exception=True)
def approval_queue(request):
    operations = list(
        pending_operations()
        .select_related("fleet_type", "created_by", "fc_user")
        .annotate(
            pending_rows=Count(
                "attendance_records",
                filter=Q(
                    attendance_records__review_status=ReviewStatus.PENDING,
                    attendance_records__source__in=AttendanceRecord.USER_SOURCES,
                ),
            )
        )
        .order_by("started_at", "pk")
    )
    for operation in operations:
        operation.duration_text = (
            format_duration(operation.ended_at - operation.started_at)
            if operation.started_at and operation.ended_at
            else ""
        )
    rows = list(
        pending_attendance()
        .filter(source__in=AttendanceRecord.USER_SOURCES)
        .select_related("operation__fleet_type", "created_by")
        .order_by("-operation__started_at", "operation_id", "character_name", "pk")[: QUEUE_ROWS_SHOWN + 1]
    )
    groups = {}
    for row in rows[:QUEUE_ROWS_SHOWN]:
        groups.setdefault(row.operation_id, {"operation": row.operation, "rows": []})["rows"].append(row)
    return _render(
        request,
        "fleetops/approvals.html",
        {
            "pending_fleets": operations,
            "row_groups": list(groups.values()),
            "rows_truncated": len(rows) > QUEUE_ROWS_SHOWN,
            "rows_shown": QUEUE_ROWS_SHOWN,
        },
    )


@permission_required("fleetops.manage_fleets", raise_exception=True)
@require_POST
def review_operation_view(request, operation_uuid):
    operation = get_object_or_404(FleetOperation, uuid=operation_uuid)
    form = ReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("The fleet was not reviewed. %(errors)s") % {"errors": _form_errors(form)})
        return _back(request, "fleetops:approval_queue")
    data = form.cleaned_data
    try:
        if data["action"] == ReviewForm.APPROVE:
            count = approvals.approve_operation(
                operation, actor=request.user, note=data["note"], include_attendance=data["include_attendance"]
            )
            approved = True
        else:
            count = approvals.reject_operation(
                operation, actor=request.user, note=data["note"], include_attendance=data["include_attendance"]
            )
            approved = False
    except ValueError as exc:
        messages.error(request, translate_stored(str(exc)))
    else:
        if approved and count:
            text = ngettext("Fleet approved with %(count)s PAP row.", "Fleet approved with %(count)s PAP rows.", count)
        elif approved:
            text = _("Fleet approved.")
        elif count:
            text = ngettext("Fleet rejected with %(count)s PAP row.", "Fleet rejected with %(count)s PAP rows.", count)
        else:
            text = _("Fleet rejected.")
        messages.success(request, text % {"count": count})
    return _back(request, "fleetops:approval_queue")


@permission_required("fleetops.manage_fleets", raise_exception=True)
@require_POST
def review_attendance_view(request):
    form = ReviewForm(request.POST)
    ids = {int(value) for value in request.POST.getlist("ids") if value.isascii() and value.isdigit()}
    if not ids:
        messages.error(request, _("Select at least one PAP row."))
        return _back(request, "fleetops:approval_queue")
    if not form.is_valid():
        messages.error(request, _("The PAP rows were not reviewed. %(errors)s") % {"errors": _form_errors(form)})
        return _back(request, "fleetops:approval_queue")
    data = form.cleaned_data
    records = AttendanceRecord.objects.filter(pk__in=ids)
    try:
        if data["action"] == ReviewForm.APPROVE:
            count = approvals.approve_attendance(records, actor=request.user, note=data["note"])
            approved = True
        else:
            count = approvals.reject_attendance(records, actor=request.user, note=data["note"])
            approved = False
    except ValueError as exc:
        messages.error(request, translate_stored(str(exc)))
    else:
        if count and approved:
            text = ngettext("%(count)s PAP row approved.", "%(count)s PAP rows approved.", count)
            messages.success(request, text % {"count": count})
        elif count:
            text = ngettext("%(count)s PAP row rejected.", "%(count)s PAP rows rejected.", count)
            messages.success(request, text % {"count": count})
        else:
            messages.warning(request, _("No PAP rows were changed; they were already reviewed."))
    return _back(request, "fleetops:approval_queue")


def _preview_page(request, operation, form, lines, warnings):
    preview = roster_import.build_import_preview(operation, lines, actor=request.user)
    data = form.cleaned_data
    payload = roster_import.sign_preview(
        operation,
        preview,
        actor=request.user,
        attendance_value=data["attendance_value"],
        duplicate_action=data["duplicate_action"],
        notes=data["notes"],
    )
    rows = []
    index = 0
    for row in preview:
        rows.append({"row": row, "index": index if row.importable else None})
        if row.importable:
            index += 1
    counts = {}
    for row in preview:
        counts[row.status] = counts.get(row.status, 0) + 1
    return _render(
        request,
        "fleetops/roster_import.html",
        {
            "operation": operation,
            "form": form,
            "preview_rows": rows,
            "warnings": warnings,
            "payload": payload,
            "counts": counts,
            "duplicate_action_label": dict(form.fields["duplicate_action"].choices).get(data["duplicate_action"], ""),
            "needs_approval": not is_approver(request.user),
        },
    )


def _import(request, operation):
    try:
        preview = roster_import.load_preview(operation, request.POST.get("payload"), actor=request.user)
    except ValueError as exc:
        messages.error(request, translate_stored(str(exc)))
        return redirect("fleetops:roster_import", operation_uuid=operation.uuid)
    selected = {int(value) for value in request.POST.getlist("rows") if value.isascii() and value.isdigit()}
    rows = [row for index, row in enumerate(preview["rows"]) if index in selected]
    if not rows:
        messages.error(request, _("Nothing was imported: no character was selected."))
        return redirect("fleetops:roster_import", operation_uuid=operation.uuid)
    try:
        result = roster_import.import_rows(
            operation,
            rows,
            actor=request.user,
            attendance_value=preview["attendance_value"],
            duplicate_action=preview["duplicate_action"],
            notes=preview["notes"],
        )
    except ValueError as exc:
        messages.error(request, translate_stored(str(exc)))
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    created = len(result.created)
    sentences = [
        ngettext("Imported PAP for %(count)s character.", "Imported PAP for %(count)s characters.", created)
        % {"count": created}
    ]
    if created and not is_approver(request.user):
        sentences.append(_("It counts once an FC lead approves it."))
    messages.success(request, " ".join(sentences))
    if result.skipped:
        skipped = "; ".join(f"{row['character_name']}: {translate_stored(row['reason'])}" for row in result.skipped)
        messages.warning(
            request, _("Skipped %(count)s: %(details)s.") % {"count": len(result.skipped), "details": skipped}
        )
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.manage_attendance", raise_exception=True)
def roster_import_view(request, operation_uuid):
    operation = get_object_or_404(FleetOperation.objects.select_related("fleet_type"), uuid=operation_uuid)
    if not _can_manage_attendance_operation(request.user, operation):
        raise Http404
    if operation.status not in RECORDABLE_STATUSES:
        messages.error(request, _("Manual PAP can only be added to Active or Closed fleets."))
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    if request.method == "POST" and request.POST.get("step") == "import":
        return _import(request, operation)
    if request.method == "POST":
        form = RosterPasteForm(request.POST)
        if form.is_valid():
            lines, warnings = roster_import.parse_roster(form.cleaned_data["text"])
            if lines:
                return _preview_page(request, operation, form, lines, warnings)
            form.add_error("text", _("No character names were found in the pasted text."))
    else:
        form = RosterPasteForm()
    return _render(
        request,
        "fleetops/roster_import.html",
        {"operation": operation, "form": form, "needs_approval": not is_approver(request.user)},
    )
