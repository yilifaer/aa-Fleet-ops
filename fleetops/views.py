import calendar
import uuid
from datetime import timedelta

from allianceauth.authentication.decorators import permissions_required
from allianceauth.authentication.models import UserProfile
from allianceauth.services.hooks import get_extension_logger
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.forms.models import model_to_dict
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from esi.decorators import token_required

from fleetops.constants import FLEET_SCOPES
from fleetops.forms import (
    CancelScheduledFleetForm,
    EndFleetForm,
    FleetAttendanceMultiplierForm,
    HistoricalManualAttendanceForm,
    IncentivePeriodForm,
    ManualAttendanceForm,
    ManualFleetForm,
    OperationRoleAssignmentForm,
    OperationEditForm,
    ScheduledFleetForm,
    StartFleetForm,
    StartScheduledFleetForm,
    manageable_attendance_operations,
)
from fleetops.forms_presets import PRESET_FORM_PREFIX, PresetSaveForm
from fleetops.models import (
    MAX_DATABASE_ID,
    MIN_RETENTION_DAYS,
    AttendanceRecord,
    AuditLog,
    FleetOperation,
    FleetMemberState,
    FleetOpsSettings,
    FleetStartPreset,
    FleetType,
    IncentivePeriod,
    MonthlyFCStatistic,
    OperationActionAttempt,
    OperationRoleAssignment,
    ReviewStatus,
)
from fleetops.providers.esi import FleetESIError, detect_character_fleet
from fleetops.services import pap
from fleetops.services.audit import audit
from fleetops.services.attendance import (
    RECORDABLE_STATUSES,
    create_manual_attendance,
    set_operation_attendance_multiplier,
)
from fleetops.services.counting import counted_operations, evaluate_stats_inclusion, operation_count_reason
from fleetops.services.dashboard import dashboard_metrics
from fleetops.services.errors import log_failure
from fleetops.services.fleet_controls import CAPSULES, KickRefused, kick_members
from fleetops.services.identity import get_owned_character
from fleetops.services.history import current_member_user_ids
from fleetops.services.incentives import (
    INCENTIVES_DISABLED_MESSAGE,
    WAIVER_LOCKED_MESSAGE,
    IncentiveError,
    finalize_period,
    pending_multiplier_operations,
    rebuild_period,
    set_waiver,
    unlock_period,
)
from fleetops.services.mentions import DISCORD_MESSAGE_LIMIT, mention_text, selection_from_operation
from fleetops.services.messages import render_messages
from fleetops.services.multipliers import (
    allowed_multipliers,
    fleet_duration,
    needs_choice,
    not_allowed_message,
    tiers,
)
from fleetops.services.operations import (
    build_unsaved_operation,
    create_manual_fleet,
    fleet_values,
    not_boss_message,
    request_fleet_end,
    retry_motd,
    retry_ping,
    retry_srp,
    start_fleet,
)
from fleetops.services.periods import month_bounds
from fleetops.services.presets import load_preset, save_last_used
from fleetops.services.review import can_delete_manual_row, is_approver, pending_approval_count
from fleetops.services.roles import add_role_assignment, delete_role_assignment
from fleetops.services.routing import proximity_rows
from fleetops.services.scheduling import (
    CHANNEL_CHANGE_NEEDS_POST,
    NOT_SCHEDULED_MESSAGE,
    cancel_scheduled_fleet as cancel_scheduled_fleet_service,
    is_pre_ping_record,
    record_start_steps,
    schedule_fleet,
    start_scheduled_fleet as start_scheduled_fleet_service,
    update_scheduled_fleet,
)
from fleetops.services.statistics import (
    attendance_history_queryset,
    corporation_statistics,
    fc_operations_queryset,
    fc_statistics,
    member_statistics,
)
from fleetops.services.tracking import unreadable_fleet_warning

logger = get_extension_logger(__name__)

# Earlier attempts listed under each operation step.
PREVIOUS_ATTEMPTS_SHOWN = 5
# A scheduled fleet still not started this long after its time is shown as overdue.
SCHEDULED_OVERDUE_AFTER = timedelta(hours=2)
SCHEDULED_FLEETS_SHOWN = 20
NOT_STARTED_MESSAGE = "This fleet has not started; the MOTD and the SRP link are set when it starts."
HISTORY_PAGE_SIZE = 200
AUDIT_PAGE_SIZE = 50
# Step messages are written for the fleet page (short ESI texts, error classes), so audit entries may keep them.
RETRY_AUDIT_MESSAGE_LENGTH = 500
PENDING_MULTIPLIERS_SHOWN = 20
# The id of the start form in start_fleet.html; the preset fields above it post with it.
START_FORM_ID = "fleet-start-form"


def _render(request, template_name, context=None):
    """Render a FleetOps page with the context shared by the navigation."""
    context = dict(context or {})
    context["show_incentives"] = (
        request.user.has_perm("fleetops.manage_incentives")
        and FleetOpsSettings.get_solo().incentive_enabled
    )
    if is_approver(request.user):
        context["pending_approval_count"] = pending_approval_count()
    return render(request, template_name, context)


def _database_id(value):
    """Return ``value`` as a positive database id, or None if it is not a valid one."""
    if not (value.isascii() and value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= MAX_DATABASE_ID else None


def _can_view_all_fleets(user):
    return user.has_perm("fleetops.manage_fleets") or user.has_perm("fleetops.view_all_fleets")


def _is_credited_fc(user, operation):
    if operation.fc_user_id == user.pk:
        return True
    return operation.role_assignments.filter(auth_user=user, grants_fc_credit=True).exists()


def _can_manage_operation(user, operation):
    return user.has_perm("fleetops.manage_fleets") or (
        user.has_perm("fleetops.manage_own_fleet") and _is_credited_fc(user, operation)
    )


def _can_see_scheduled_fleets(user):
    """FCs coordinate fleet times with each other; members do not see pre pings in FleetOps."""
    return _can_view_all_fleets(user) or user.has_perm("fleetops.start_fleet")


def _can_view_operation(user, operation):
    if _can_view_all_fleets(user):
        return True
    if not user.has_perm("fleetops.basic_access"):
        return False
    if is_pre_ping_record(operation):
        # A pre ping may have gone to a restricted channel.
        return operation.fc_user_id == user.pk or user.has_perm("fleetops.start_fleet")
    if _is_credited_fc(user, operation):
        return True
    if operation.role_assignments.filter(auth_user=user).exists():
        return True
    # Members follow the fleets they fly in; PAP rows only exist once a fleet has closed.
    if operation.member_states.filter(auth_user=user).exists():
        return True
    if operation.pap_registrations.filter(auth_user=user).exists():
        return True
    return operation.attendance_records.filter(auth_user=user, granted=True).exists()


def _member_of(user):
    """Fleets ``user`` takes part in: tracked, registered by PAP link, special role or granted PAP."""
    return (
        Q(member_states__auth_user=user)
        | Q(pap_registrations__auth_user=user)
        | Q(role_assignments__auth_user=user)
        | Q(attendance_records__auth_user=user, attendance_records__granted=True)
    )


def _can_manage_attendance_operation(user, operation):
    if not user.has_perm("fleetops.manage_attendance"):
        return False
    return user.has_perm("fleetops.manage_fleets") or _is_credited_fc(user, operation)


def _operation_for_user(request, operation_uuid, manage=False):
    operation = get_object_or_404(
        FleetOperation.objects.select_related(
            "fleet_type", "comms", "logi_channel", "boost_channel",
            "ping_target__webhook", "ping_channel", "fc_user",
        ),
        uuid=operation_uuid,
    )
    if manage:
        if _can_manage_operation(request.user, operation):
            return operation
        raise Http404
    if _can_view_operation(request.user, operation):
        return operation
    raise Http404


def _year_month(request):
    now = timezone.now()
    try:
        year = int(request.GET.get("year", now.year))
        month = int(request.GET.get("month", now.month))
    except (TypeError, ValueError):
        return now.year, now.month
    if month < 1 or month > 12 or year < 2003 or year > 2200:
        return now.year, now.month
    return year, month


def _period_choices(year, month):
    now = timezone.now()
    earliest = (
        FleetOperation.objects.exclude(started_at=None)
        .order_by("started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    first_year = earliest.year if earliest else now.year
    first_year = max(2003, min(first_year, now.year))
    years = list(range(now.year, first_year - 1, -1))
    if year not in years:
        years.append(year)
        years.sort(reverse=True)
    months = [(i, calendar.month_name[i]) for i in range(1, 13)]
    return {"year_options": years, "month_options": months, "selected_year": year, "selected_month": month}


def _period_render_context(year, month, **kwargs):
    context = _period_choices(year, month)
    context.update({"year": year, "month": month})
    context.update(kwargs)
    return context


def _incentive_redirect(period):
    return redirect(
        f"{reverse('fleetops:incentive_review')}?year={period.year}&month={period.month}"
    )


def _incentives_enabled(request):
    if FleetOpsSettings.get_solo().incentive_enabled:
        return True
    messages.info(request, INCENTIVES_DISABLED_MESSAGE)
    return False


@permission_required("fleetops.basic_access", raise_exception=True)
def dashboard(request):
    from fleetops.views_pap import live_pap_rows

    active_qs = FleetOperation.objects.filter(status=FleetOperation.Status.ACTIVE)
    if not _can_view_all_fleets(request.user):
        active_qs = active_qs.filter(Q(fc_user=request.user) | _member_of(request.user)).distinct()
    active = (
        active_qs.select_related("fleet_type", "fc_user")
        .annotate(active_member_count=Count("member_states", filter=Q(member_states__is_active=True), distinct=True))[:20]
    )
    own = (
        FleetOperation.objects.filter(
            Q(fc_user=request.user)
            | Q(role_assignments__auth_user=request.user, role_assignments__grants_fc_credit=True)
            | Q(attendance_records__auth_user=request.user, attendance_records__granted=True)
        )
        .exclude(status__in=[FleetOperation.Status.DRAFT, FleetOperation.Status.SCHEDULED])
        # A cancelled pre ping never ran.
        .exclude(status=FleetOperation.Status.CANCELLED, started_at=None)
        .distinct()[:10]
    )
    scheduled = FleetOperation.objects.filter(status=FleetOperation.Status.SCHEDULED)
    if not _can_see_scheduled_fleets(request.user):
        scheduled = scheduled.filter(fc_user=request.user)
    scheduled = scheduled.select_related("fleet_type", "ping_channel").order_by("scheduled_at", "pk")
    now = timezone.now()
    fc = fc_statistics(request.user, now.year, now.month, include_active=True)
    metrics = dashboard_metrics(request.user)
    # Fleets that ended without the FC's multiplier choice, such as after an automatic end.
    pending_multipliers = (
        FleetOperation.objects.filter(
            fc_user=request.user, multiplier_pending=True, status=FleetOperation.Status.CLOSED
        )
        .select_related("fleet_type")
        .order_by("-ended_at", "-pk")
    )
    return _render(
        request,
        "fleetops/dashboard.html",
        {
            "active_operations": active,
            "scheduled_operations": scheduled[:SCHEDULED_FLEETS_SHOWN],
            "own_operations": own,
            "fc_stats": fc,
            "dashboard_cards": metrics["cards"],
            "top_ships": metrics["top_ships"],
            "recent_participations": metrics["recent"],
            "pending_multiplier_operations": pending_multipliers[:20],
            "live_pap_rows": live_pap_rows(request.user),
        },
    )


def _already_tracked_text(operation, can_view):
    """Why a start returned the operation that its FC character already runs for the same ESI fleet.

    Records of other FC characters never get here: the new boss takes the fleet over.
    """
    text = f"ESI fleet {operation.esi_fleet_id} is already tracked by FleetOps (FC {operation.fc_character_name})."
    text += " This is the running operation; no new fleet was started." if can_view else " No new fleet was started."
    if operation.status == FleetOperation.Status.STARTING:
        text += " Its start has not finished yet; if it was interrupted, start the fleet again in about five minutes."
    elif not can_view:
        text += " Ask its FC or a fleet manager to end it, then start the fleet again."
    return text


def _handover_text(operation):
    """The records a start ended because their boss handed the fleet over, for the start message."""
    names = ", ".join(f"FC {record.fc_character_name}" for record in getattr(operation, "handed_over", []))
    if not names:
        return ""
    return f" The fleet boss was handed over to you, so the previous operation ({names}) was ended."


def _send_blockers(operation, *, actor, pre_ping):
    """Problems that stop the ping of an unsaved fleet from being posted, e.g. a text Discord rejects.

    Checked before a start, a pre ping or an edit, so nothing is changed. A ping that is only
    copied is never blocked.
    """
    if operation.ping_channel is None or not operation.send_ping:
        return []
    try:
        rendered = render_messages(operation, actor=actor, pre_ping=pre_ping)
    except Exception as exc:
        # The start records the rendering failure as one of its steps.
        log_failure(logger, "Message check", exc)
        return []
    return list(rendered.blocking)


def _form_errors_payload(form):
    # The preview runs while the FC fills the form in, so an incomplete form is not an error.
    missing = [
        str(form[name].label)
        for name, errors in form.errors.as_data().items()
        if name in form.fields and any(error.code == "required" for error in errors)
    ]
    return JsonResponse({"ok": False, "missing": missing, "errors": form.errors.get_json_data()})


def _preview_payload(operation, *, actor, pre_ping):
    try:
        rendered = render_messages(operation, actor=actor, pre_ping=pre_ping)
    except Exception as exc:
        log_failure(logger, "Message preview", exc)
        return JsonResponse(
            {"ok": False, "error": "The ping or MOTD template could not be rendered. Check the message templates."},
            status=400,
        )
    sends = operation.ping_channel is not None and operation.send_ping
    # A ping that is only copied is never blocked; render_messages lists its length as a warning.
    warnings = [*rendered.errors, *rendered.warnings]
    # Template problems only name the template and the error class, so they are safe to show.
    return JsonResponse(
        {
            "ok": True,
            "ping": rendered.ping,
            "ping_display": rendered.ping_display or rendered.ping,
            "motd": rendered.motd,
            "warnings": warnings,
            "blocking": list(rendered.blocking) if sends else [],
            "ping_length": rendered.ping_length,
            "motd_length": rendered.motd_length,
        }
    )


def _start_page(request, form, operation=None, **extra):
    return _render(
        request,
        "fleetops/start_fleet.html",
        {
            "form": form,
            "operation": operation,
            "channel_rules": form.channel_rules(),
            "ping_limit": DISCORD_MESSAGE_LIMIT,
            "eve_now": timezone.now(),
            **extra,
        },
    )


def _chosen_preset(request):
    """The preset the start page opens with and how it was chosen.

    ``?preset=<id>``, ``?preset=last`` (the last used settings) or ``?preset=none``; without the
    parameter the FC's default preset. Presets of other users are not found.
    """
    choice = request.GET.get("preset", "").strip()
    own = FleetStartPreset.objects.filter(owner=request.user)
    if choice == "none":
        return None, "none"
    if choice == "last":
        return own.filter(is_last_used=True).first(), "last"
    if choice:
        pk = _database_id(choice)
        if pk is None:
            raise Http404
        return get_object_or_404(own, pk=pk), "chosen"
    return own.filter(is_default=True, is_last_used=False).first(), "default"


def _preset_bar(request, *, preset=None, how="", warnings=()):
    """Context of the preset bar above the start form."""
    own = FleetStartPreset.objects.filter(owner=request.user)
    if preset is None:
        notice = "No last used settings are saved yet." if how == "last" else ""
    elif preset.is_last_used:
        notice = "Last used settings loaded."
    elif how == "default":
        notice = f"Default preset {preset.name} applied."
    else:
        notice = f"Preset {preset.name} loaded."
    named = preset is not None and not preset.is_last_used
    save_form = PresetSaveForm(
        prefix=PRESET_FORM_PREFIX,
        form_id=START_FORM_ID,
        initial={"name": preset.name if named else "", "remember_fc": bool(preset and preset.fc_character_id)},
    )
    return {
        "presets": own.filter(is_last_used=False),
        "has_last_used": own.filter(is_last_used=True).exists(),
        "preset_selected": "last" if preset is not None and preset.is_last_used else str(preset.pk) if named else "",
        "preset_notice": notice,
        "preset_default_applied": how == "default" and preset is not None,
        "preset_warnings": list(warnings),
        "preset_save_form": save_form,
    }


def _remember_last_used(user, cleaned_data):
    # The start itself succeeded; a failure here only loses the "last used" shortcut.
    try:
        save_last_used(user, cleaned_data)
    except Exception as exc:
        log_failure(logger, "Saving the last used start settings", exc)


@permission_required("fleetops.start_fleet", raise_exception=True)
def start_fleet_view(request):
    if request.method != "POST":
        preset, how = _chosen_preset(request)
        initial, warnings = load_preset(preset, StartFleetForm, request.user) if preset else ({}, [])
        form = StartFleetForm(user=request.user, initial={**initial, "request_id": uuid.uuid4()})
        return _start_page(request, form, **_preset_bar(request, preset=preset, how=how, warnings=warnings))
    form = StartFleetForm(request.POST, user=request.user)
    if not form.is_valid():
        return _start_page(request, form, **_preset_bar(request))
    data = form.cleaned_data
    pre_ping = data["is_pre_ping"]
    try:
        blockers = _send_blockers(
            build_unsaved_operation(request.user, data), actor=request.user, pre_ping=pre_ping
        )
        for problem in blockers:
            form.add_error(None, problem)
        if blockers:
            return _start_page(request, form, **_preset_bar(request))
        if pre_ping:
            operation = schedule_fleet(user=request.user, cleaned_data=data, request_id=data["request_id"])
        else:
            operation = start_fleet(user=request.user, cleaned_data=data, request_id=data["request_id"])
    except (FleetESIError, PermissionError) as exc:
        form.add_error(None, str(exc))
        return _start_page(request, form, **_preset_bar(request))
    except Exception as exc:
        log_failure(logger, "Fleet start", exc)
        form.add_error(None, "Fleet start failed because of an unexpected error. Please try again or contact an administrator.")
        return _start_page(request, form, **_preset_bar(request))

    if not getattr(operation, "already_tracked", False):
        _remember_last_used(request.user, data)
    if operation.status == FleetOperation.Status.SCHEDULED:
        messages.success(request, "Fleet scheduled. Start it from this page when it is time.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    if not getattr(operation, "already_tracked", False):
        record_start_steps(operation)
        # A resubmitted form can return a start that was interrupted and cancelled; its page says so.
        if operation.status != FleetOperation.Status.CANCELLED:
            messages.success(
                request, "Fleet operation started. Review individual step status below." + _handover_text(operation)
            )
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    # After a boss handover the new boss may not be allowed to open the running record.
    can_view = _can_view_operation(request.user, operation)
    text = _already_tracked_text(operation, can_view)
    if can_view:
        messages.info(request, text)
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    form.add_error(None, text)
    return _start_page(request, form, **_preset_bar(request))


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def preview_fleet(request):
    form = StartFleetForm(request.POST, user=request.user)
    if not form.is_valid():
        return _form_errors_payload(form)
    try:
        operation = build_unsaved_operation(request.user, form.cleaned_data)
    except PermissionError:
        return JsonResponse(
            {"ok": False, "error": "Character is not owned by this user."}, status=403
        )
    return _preview_payload(operation, actor=request.user, pre_ping=form.cleaned_data["is_pre_ping"])


@permission_required("fleetops.start_fleet", raise_exception=True)
def detect_fleet(request, character_id):
    ownership = get_owned_character(request.user, character_id)
    if ownership is None:
        return JsonResponse(
            {"ok": False, "code": "NOT_OWNED", "message": "Character is not owned by this user."},
            status=403,
        )
    try:
        result = detect_character_fleet(request.user, character_id)
    except FleetESIError as exc:
        return JsonResponse({"ok": False, "code": exc.code, "message": str(exc)})
    is_boss = result.is_boss(character_id)
    return JsonResponse(
        {
            "ok": True,
            "fleet_id": result.fleet_id,
            "role": result.role,
            "fleet_boss_id": result.fleet_boss_id,
            "is_fleet_boss": is_boss,
            "message": "" if is_boss else not_boss_message(ownership.character.character_name, result.fleet_boss_id),
        }
    )


@permission_required("fleetops.start_fleet", raise_exception=True)
@token_required(scopes=FLEET_SCOPES)
def authorize_esi(request, token):
    messages.success(request, f"Fleet ESI token saved for {token.character_name}.")
    return redirect("fleetops:start_fleet")


@permission_required("fleetops.basic_access", raise_exception=True)
def fleet_operations_view(request):
    now = timezone.now()
    try:
        year = int(request.GET.get("year", now.year))
    except (TypeError, ValueError):
        year = now.year
    if year < 2003 or year > 2200:
        year = now.year
    month_raw = request.GET.get("month", "")
    try:
        month = int(month_raw) if month_raw else None
    except (TypeError, ValueError):
        month = None
    if month is not None and not (1 <= month <= 12):
        month = None

    # Scheduled and cancelled pre pings have no start time; they are filed under their fleet time.
    qs = (
        FleetOperation.objects.exclude(status=FleetOperation.Status.DRAFT)
        .select_related("fleet_type", "fc_user", "comms")
        .annotate(fleet_time=Coalesce("started_at", "scheduled_at"))
    )
    if not _can_view_all_fleets(request.user):
        visible = Q(fc_user=request.user) | _member_of(request.user)
        if _can_see_scheduled_fleets(request.user):
            visible |= Q(
                status__in=[FleetOperation.Status.SCHEDULED, FleetOperation.Status.CANCELLED],
                started_at=None,
                is_manual=False,
            ) & ~Q(scheduled_at=None)
        qs = qs.filter(visible).distinct()

    qs = qs.filter(fleet_time__year=year)
    if month:
        qs = qs.filter(fleet_time__month=month)

    fleet_type = request.GET.get("fleet_type", "").strip()
    status = request.GET.get("status", "").strip()
    fc = request.GET.get("fc", "").strip()
    doctrine = request.GET.get("doctrine", "").strip()
    query = request.GET.get("q", "").strip()
    fleet_type_id = _database_id(fleet_type)
    if fleet_type_id is not None:
        qs = qs.filter(fleet_type_id=fleet_type_id)
    if status:
        qs = qs.filter(status=status)
    if fc:
        qs = qs.filter(Q(fc_character_name__icontains=fc) | Q(fc_main_character_name__icontains=fc))
    if doctrine:
        qs = qs.filter(doctrine_name__icontains=doctrine)
    if query:
        qs = qs.filter(
            Q(formup__icontains=query)
            | Q(additional_message__icontains=query)
            | Q(fc_character_name__icontains=query)
            | Q(fc_main_character_name__icontains=query)
            | Q(doctrine_name__icontains=query)
        )

    attendance_total_subquery = (
        AttendanceRecord.objects.filter(operation_id=OuterRef("pk"), granted=True, review_status=ReviewStatus.APPROVED)
        .values("operation_id")
        .annotate(total=Sum("attendance_value"))
        .values("total")[:1]
    )
    tracked_members_subquery = (
        FleetMemberState.objects.filter(operation_id=OuterRef("pk"))
        .values("operation_id")
        .annotate(total=Count("id"))
        .values("total")[:1]
    )
    qs = qs.annotate(
        attendance_total=Coalesce(Subquery(attendance_total_subquery, output_field=IntegerField()), 0),
        tracked_members=Coalesce(Subquery(tracked_members_subquery, output_field=IntegerField()), 0),
    ).order_by("-fleet_time", "-created_at")

    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    settings = FleetOpsSettings.get_solo()
    for operation in page.object_list:
        operation.can_edit = _can_manage_operation(request.user, operation)
        operation.is_pre_ping = is_pre_ping_record(operation)
        operation.count_reason = "" if operation.is_pre_ping else operation_count_reason(operation, settings)
    period = _period_choices(year, month or now.month)
    period.update(
        {
            "year": year,
            "selected_archive_month": month,
            "page": page,
            "fleet_types": FleetType.objects.all(),
            "status_choices": FleetOperation.Status.choices,
            "filters": {
                "fleet_type": fleet_type,
                "status": status,
                "fc": fc,
                "doctrine": doctrine,
                "q": query,
            },
        }
    )
    return _render(request, "fleetops/fleet_operations.html", period)


EDITED_FIELDS = [
    "fleet_type", "doctrine_name", "doctrine_source", "doctrine_external_id", "formup", "comms",
    "logi_channel", "boost_channel", "ping_template", "motd_template", "additional_message",
    "started_at", "ended_at",
]
# Recorded with each edit; a change by someone who is not an FC lead can send a fleet back to review.
REVIEW_FIELDS = ["review_status", "counts_for_stats"]


def _minute(value):
    # Fleet times are edited to the minute.
    return value.replace(second=0, microsecond=0) if value else None


def _keep_multiplier_within_duration(user, operation):
    """Lower the multiplier of a closed fleet to the highest one its edited times allow.

    Returns the new multiplier, or None when the times still allow it. An FC lead's override is
    kept; a fleet that is still open is checked when it ends.
    """
    if operation.status != FleetOperation.Status.CLOSED:
        return None
    if operation.multiplier_source == FleetOperation.MultiplierSource.OVERRIDE:
        return None
    current = int(operation.attendance_multiplier or 1)
    allowed = allowed_multipliers(fleet_duration(operation, operation.ended_at))
    if current in allowed:
        return None
    lowered = max(value for value in allowed if value <= current)
    source = operation.multiplier_source
    if source not in FleetOperation.MultiplierSource.values:
        source = FleetOperation.MultiplierSource.FC
    set_operation_attendance_multiplier(
        operation, lowered, actor=user, source=source, reason=f"Fleet times edited. {not_allowed_message(current)}"
    )
    return lowered


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
def edit_operation_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    if operation.status == FleetOperation.Status.SCHEDULED:
        return redirect("fleetops:edit_scheduled_fleet", operation_uuid=operation.uuid)
    if is_pre_ping_record(operation):
        messages.info(request, "This scheduled fleet was cancelled before it started, so there is nothing to edit.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    old = model_to_dict(operation, fields=EDITED_FIELDS + REVIEW_FIELDS)
    if request.method == "POST":
        form = OperationEditForm(request.POST, instance=operation)
        if form.is_valid():
            fleet_type_changed = form.cleaned_data["fleet_type"].pk != old.get("fleet_type")
            updated = form.save(commit=False)
            if fleet_type_changed:
                updated.fleet_point_weight_snapshot = updated.fleet_type.point_weight
            if updated.doctrine_name != old.get("doctrine_name"):
                # A doctrine typed in by hand is no longer the Fittings doctrine and its fits.
                updated.doctrine_source = "custom"
                updated.doctrine_external_id = ""
                updated.doctrine_fits = []
            times_changed = _minute(updated.started_at) != _minute(old["started_at"]) or _minute(
                updated.ended_at
            ) != _minute(old["ended_at"])
            approver = is_approver(request.user)
            back_to_review = (
                (times_changed or (fleet_type_changed and (updated.is_manual or updated.link_only)))
                and updated.review_status != ReviewStatus.PENDING
                and not approver
            )
            if back_to_review:
                # A fleet entered by hand counts only as an FC lead approved it. So do new times of any
                # fleet: the multiplier tiers and the minimum fleet duration are measured from them.
                updated.review_status = ReviewStatus.PENDING
                updated.reviewed_by = None
                updated.reviewed_at = None
            if times_changed:
                evaluate_stats_inclusion(updated)
            updated.save()
            new = model_to_dict(updated, fields=EDITED_FIELDS + REVIEW_FIELDS)
            audit(
                request.user,
                "operation.edit",
                updated,
                {k: str(v) for k, v in old.items()},
                {k: str(v) for k, v in new.items()},
                reason="Edited from FleetOps fleet archive",
            )
            # The multiplier chosen for the old times stays within the duration rule.
            lowered = None
            if times_changed and not approver:
                lowered = _keep_multiplier_within_duration(request.user, updated)
            if lowered is not None:
                messages.warning(request, f"PAP multiplier lowered to {lowered}x: the new times do not allow more.")
            if back_to_review:
                messages.warning(
                    request, "Fleet operation updated. Changes need FC lead approval; the fleet does not count until then."
                )
            else:
                messages.success(request, "Fleet operation updated.")
            return redirect("fleetops:operation_detail", operation_uuid=updated.uuid)
    else:
        form = OperationEditForm(instance=operation)
    return _render(
        request,
        "fleetops/operation_edit.html",
        {
            "operation": operation,
            "form": form,
            "eve_time": timezone.now(),
            # Only changed times need the long fleet confirmation.
            "stored_started_at": old["started_at"],
            "stored_ended_at": old["ended_at"],
        },
    )


def _operation_steps(operation):
    actions = list(
        operation.actions.prefetch_related(
            Prefetch("history", queryset=OperationActionAttempt.objects.order_by("-created_at", "-pk"))
        )
    )
    for step in actions:
        # The latest attempt is the step's current result; the rest is shown underneath it.
        previous = list(step.history.all())[1:]
        step.previous_attempts = previous[:PREVIOUS_ATTEMPTS_SHOWN]
        step.older_attempt_count = max(0, len(previous) - PREVIOUS_ATTEMPTS_SHOWN)
    return actions


def _can_start_scheduled(user, operation):
    # Only the FC who announced the fleet starts it.
    return (
        operation.status == FleetOperation.Status.SCHEDULED
        and operation.fc_user_id == user.pk
        and user.has_perm("fleetops.start_fleet")
    )


def _scheduled_fleet_page(request, operation, *, start_form=None):
    scheduled = operation.status == FleetOperation.Status.SCHEDULED
    can_manage = scheduled and _can_manage_operation(request.user, operation)
    can_start = _can_start_scheduled(request.user, operation)
    if can_start and start_form is None:
        start_form = StartScheduledFleetForm(user=request.user, operation=operation)
    return _render(
        request,
        "fleetops/scheduled_fleet.html",
        {
            "operation": operation,
            "actions": _operation_steps(operation),
            "is_scheduled": scheduled,
            "overdue": scheduled and timezone.now() > operation.scheduled_at + SCHEDULED_OVERDUE_AFTER,
            "can_start": can_start,
            "can_manage": can_manage,
            "start_form": start_form if can_start else None,
            "cancel_form": CancelScheduledFleetForm() if can_manage else None,
            "mention_label": mention_text(selection_from_operation(operation), display=True) or "No ping",
        },
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def operation_detail(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid)
    if is_pre_ping_record(operation):
        return _scheduled_fleet_page(request, operation)
    actions = _operation_steps(operation)
    is_closed = operation.status == FleetOperation.Status.CLOSED
    # A closed fleet lists everyone who was tracked in it; an open one only the current members.
    members = operation.member_states.all() if is_closed else operation.member_states.filter(is_active=True)
    members = members.order_by("character_name")
    assignable_members = operation.member_states.all().order_by("character_name")
    events = operation.member_events.all()[:100]
    attendance = operation.attendance_records.all()
    attendance_total = sum(r.attendance_value for r in attendance if r.granted)
    unique_players = members.exclude(auth_user=None).values("auth_user_id").distinct().count()
    map_rows = proximity_rows(operation, 3)
    role_assignments = operation.role_assignments.select_related("auth_user", "assigned_by").all()
    is_active = operation.status == FleetOperation.Status.ACTIVE
    settings = FleetOpsSettings.get_solo()
    stale = bool(
        is_active
        and operation.last_esi_update
        and (timezone.now() - operation.last_esi_update).total_seconds()
        > settings.stale_threshold
    )
    context = {
        "operation": operation,
        "actions": actions,
        "members": members,
        "assignable_members": assignable_members,
        "events": events,
        "attendance": attendance,
        "attendance_total": attendance_total,
        "unique_players": unique_players,
        "map_rows": map_rows,
        "role_assignments": role_assignments,
        "role_assignment_form": OperationRoleAssignmentForm(operation=operation),
        "stale": stale,
        "unreadable_warning": unreadable_fleet_warning(operation, settings) if is_active else "",
        "can_manage": _can_manage_operation(request.user, operation),
        "can_manage_attendance": _can_manage_attendance_operation(request.user, operation),
        "accepts_records": operation.status in RECORDABLE_STATUSES,
        "manual_attendance_form": ManualAttendanceForm(),
    }
    # The page's PAP (end prompt, multiplier form), records, link, control and Fleet tab parts add
    # their own context. Imported here because those modules import their helpers from this one.
    from fleetops import views_controls, views_fleet_state, views_links, views_pap, views_records

    for provider in (
        views_pap.pap_context,
        views_records.records_context,
        views_links.links_context,
        views_controls.controls_context,
        views_fleet_state.fleet_context,
    ):
        context.update(provider(request, operation))
    return _render(request, "fleetops/operation_detail.html", context)


def _end_multiplier_problem(operation, posted):
    """Why an end request must not end the fleet, or "" when it may.

    In the FC mode a fleet that ran longer than a multiplier tier needs the FC's choice (the page
    may have been loaded before the fleet got there), and the choice must be one the duration
    allows. The automatic mode ignores a posted value.
    """
    if operation.status != FleetOperation.Status.ACTIVE:
        return ""
    settings = FleetOpsSettings.get_solo()
    if settings.multiplier_mode != FleetOpsSettings.MultiplierMode.FC:
        return ""
    duration = fleet_duration(operation)
    if posted is not None:
        return "" if posted in allowed_multipliers(duration) else not_allowed_message(posted)
    if not needs_choice(duration, settings):
        return ""
    longest = max(tier.threshold_minutes for tier in tiers() if duration > timedelta(minutes=tier.threshold_minutes))
    return f"This fleet now runs longer than {longest} minutes; choose the PAP multiplier."


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def end_fleet_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    multiplier = None
    if request.POST.get("attendance_multiplier"):
        form = EndFleetForm(request.POST)
        if not form.is_valid():
            choices = ", ".join(label for _value, label in form.fields["attendance_multiplier"].choices)
            messages.error(request, f"Choose one of the PAP multipliers ({choices}). The fleet was not ended.")
            return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
        multiplier = form.cleaned_data["attendance_multiplier"]
    problem = _end_multiplier_problem(operation, multiplier)
    if problem:
        messages.error(request, f"{problem} The fleet was not ended.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    # The final sync and the PAP run in a background task, so the browser does not wait for ESI.
    # The task checks the multiplier against the duration again.
    operation, requested = request_fleet_end(operation, actor=request.user, attendance_multiplier=multiplier)
    if requested and operation.status == FleetOperation.Status.CLOSED:
        messages.success(request, f"Fleet ended. Attendance finalized at {operation.attendance_multiplier}x.")
    elif requested:
        messages.info(request, "Ending the fleet. The final sync and attendance are being finished; this page updates when the fleet is closed.")
    else:
        messages.info(request, f"This fleet is already {operation.get_status_display().lower()}.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def start_scheduled_fleet(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid)
    if operation.fc_user_id != request.user.pk:
        raise Http404
    if operation.status != FleetOperation.Status.SCHEDULED:
        # A second click, or the fleet was cancelled meanwhile.
        messages.info(request, f"This fleet is already {operation.get_status_display().lower()}.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    form = StartScheduledFleetForm(request.POST, user=request.user, operation=operation)
    if not form.is_valid():
        return _scheduled_fleet_page(request, operation, start_form=form)
    try:
        started = start_scheduled_fleet_service(
            operation,
            user=request.user,
            character_id=int(form.cleaned_data["fc_character_id"]),
            send_ping=form.cleaned_data["send_ping"],
        )
    except (FleetESIError, PermissionError) as exc:
        form.add_error(None, str(exc))
        return _scheduled_fleet_page(request, operation, start_form=form)
    except Exception as exc:
        log_failure(logger, "Scheduled fleet start", exc, operation)
        form.add_error(None, "Fleet start failed because of an unexpected error. Please try again or contact an administrator.")
        return _scheduled_fleet_page(request, operation, start_form=form)
    if getattr(started, "already_tracked", False):
        can_view = _can_view_operation(request.user, started)
        text = _already_tracked_text(started, can_view) + " The scheduled fleet was not started."
        if can_view:
            messages.info(request, text)
            return redirect("fleetops:operation_detail", operation_uuid=started.uuid)
        form.add_error(None, text)
        return _scheduled_fleet_page(request, operation, start_form=form)
    if started.status in (FleetOperation.Status.ACTIVE, FleetOperation.Status.STARTING):
        messages.success(
            request, "Scheduled fleet started. Review individual step status below." + _handover_text(started)
        )
    else:
        messages.info(request, f"This fleet is already {started.get_status_display().lower()}.")
    return redirect("fleetops:operation_detail", operation_uuid=started.uuid)


def _scheduled_for_manager(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not is_pre_ping_record(operation):
        raise Http404
    return operation


def _edited_copy(operation, cleaned_data):
    """The scheduled fleet with the edit form's values, unsaved."""
    copy = FleetOperation.objects.select_related("ping_channel", "fleet_type").get(pk=operation.pk)
    for name, value in fleet_values({**cleaned_data, "operation_mode": "full"}).items():
        setattr(copy, name, value)
    return copy


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
def edit_scheduled_fleet(request, operation_uuid):
    operation = _scheduled_for_manager(request, operation_uuid)
    if operation.status != FleetOperation.Status.SCHEDULED:
        messages.info(request, NOT_SCHEDULED_MESSAGE)
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    if request.method != "POST":
        return _start_page(request, ScheduledFleetForm(operation=operation, user=request.user), operation)
    form = ScheduledFleetForm(request.POST, operation=operation, user=request.user)
    # The edit page previews its messages through its own URL.
    if request.POST.get("preview"):
        if not form.is_valid():
            return _form_errors_payload(form)
        return _preview_payload(_edited_copy(operation, form.cleaned_data), actor=request.user, pre_ping=True)
    if not form.is_valid():
        return _start_page(request, form, operation)
    data = form.cleaned_data
    channel = data.get("ping_channel")
    if channel is not None and channel.pk != operation.ping_channel_id and not data["send_update"]:
        # A cancellation notice goes to the fleet's ping channel, which must have seen the pre ping.
        form.add_error("send_update", CHANNEL_CHANGE_NEEDS_POST)
        return _start_page(request, form, operation)
    blockers = _send_blockers(_edited_copy(operation, data), actor=request.user, pre_ping=True)
    for problem in blockers:
        form.add_error(None, problem)
    if blockers:
        return _start_page(request, form, operation)
    try:
        updated = update_scheduled_fleet(
            operation,
            actor=request.user,
            cleaned_data=data,
            send_update=data["send_update"],
            edit_token=data.get("edit_token"),
        )
    except ValueError as exc:
        messages.info(request, str(exc))
    else:
        if updated.already_posted:
            messages.info(request, "Scheduled fleet updated. The updated pre ping was already posted.")
        elif data["send_update"]:
            messages.success(request, "Scheduled fleet updated and the updated pre ping was posted.")
        else:
            messages.success(request, "Scheduled fleet updated. No new pre ping was posted.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def cancel_scheduled_fleet(request, operation_uuid):
    operation = _scheduled_for_manager(request, operation_uuid)
    form = CancelScheduledFleetForm(request.POST)
    notify = form.is_valid() and form.cleaned_data["notify"]
    operation = cancel_scheduled_fleet_service(operation, actor=request.user, notify=notify)
    if not operation.cancelled:
        messages.info(request, f"This fleet is already {operation.get_status_display().lower()}.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    notice = operation.actions.filter(action="cancel_notice").first() if notify else None
    text = "Scheduled fleet cancelled."
    if notice:
        text += f" Cancellation notice: {notice.get_status_display()}."
        if notice.error_message:
            text += f" {notice.error_message}"
    messages.success(request, text)
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


def _step_statuses(operation):
    return dict(operation.actions.values_list("action", "status"))


def _audit_retry(actor, name, operation, action, before):
    """One audit entry per retry with the step, its status before and after, and the step's message."""
    audit(
        actor,
        name,
        operation,
        {"step": action.action, "status": before.get(action.action)},
        {
            "step": action.action,
            "status": action.status,
            "message": (action.error_message or "")[:RETRY_AUDIT_MESSAGE_LENGTH],
        },
    )


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def retry_ping_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    if operation.status == FleetOperation.Status.CANCELLED and is_pre_ping_record(operation):
        messages.info(request, "This scheduled fleet was cancelled, so its pre ping is not posted again.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    before = _step_statuses(operation)
    action = retry_ping(operation)
    _audit_retry(request.user, "fleet.retry_ping", operation, action, before)
    detail = f" {action.error_message}" if action.error_message else ""
    messages.info(request, f"Ping retry: {action.get_status_display()}.{detail}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def retry_motd_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    if is_pre_ping_record(operation):
        messages.info(request, NOT_STARTED_MESSAGE)
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    before = _step_statuses(operation)
    action = retry_motd(operation)
    _audit_retry(request.user, "fleet.retry_motd", operation, action, before)
    detail = f" {action.error_message}" if action.error_message else ""
    messages.info(request, f"MOTD retry: {action.get_status_display()}.{detail}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def retry_srp_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    if is_pre_ping_record(operation):
        messages.info(request, NOT_STARTED_MESSAGE)
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    before = _step_statuses(operation)
    action = retry_srp(operation)
    _audit_retry(request.user, "fleet.retry_srp", operation, action, before)
    detail = f" {action.error_message}" if action.error_message else ""
    messages.info(request, f"SRP retry: {action.get_status_display()}.{detail}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


def _can_set_multiplier(user, operation):
    """Attendance managers of the fleet change its multiplier; its FC can always settle a pending choice."""
    if _can_manage_attendance_operation(user, operation):
        return True
    return operation.multiplier_pending and _can_manage_operation(user, operation)


@permissions_required(
    ("fleetops.manage_attendance", "fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True
)
@require_POST
def set_attendance_multiplier_view(request, operation_uuid):
    operation = get_object_or_404(FleetOperation, uuid=operation_uuid)
    if not _can_set_multiplier(request.user, operation):
        raise Http404
    detail = redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    if operation.status != FleetOperation.Status.CLOSED:
        messages.error(request, "The PAP multiplier is chosen when the fleet ends.")
        return detail
    form = FleetAttendanceMultiplierForm(
        request.POST, operation=operation, can_override=request.user.has_perm("fleetops.manage_fleets")
    )
    if not form.is_valid():
        errors = " ".join(str(error) for errors in form.errors.values() for error in errors)
        messages.error(request, f"The PAP multiplier was not changed. {errors}".strip())
        return detail
    multiplier = form.cleaned_data["attendance_multiplier"]
    reason = (form.cleaned_data.get("reason") or "").strip()
    # Beyond the duration rule only with an FC lead's written reason; the audit entry keeps it.
    source = FleetOperation.MultiplierSource.OVERRIDE if form.is_override else FleetOperation.MultiplierSource.FC
    summary = set_operation_attendance_multiplier(operation, multiplier, actor=request.user, source=source, reason=reason)
    messages.success(request, f"PAP multiplier set to {multiplier}x; PAP recalculated ({summary['granted']} granted).")
    return detail


@permission_required("fleetops.create_manual_fleet", raise_exception=True)
def manual_fleet_view(request):
    if request.method == "POST":
        form = ManualFleetForm(request.POST)
        if form.is_valid():
            try:
                operation = create_manual_fleet(user=request.user, cleaned_data=form.cleaned_data)
            except ValueError as exc:
                form.add_error(None, str(exc))
            except Exception as exc:
                log_failure(logger, "Manual fleet creation", exc)
                form.add_error(None, "The manual fleet could not be created because of an unexpected error.")
            else:
                if operation.review_status == ReviewStatus.PENDING:
                    messages.success(
                        request,
                        "Manual fleet record created. It counts once an FC lead approves it. "
                        "Add attendance from the Attendance tab or Manual Attendance page.",
                    )
                else:
                    messages.success(request, "Manual fleet record created. Add attendance from the Attendance tab or Manual Attendance page.")
                return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    else:
        form = ManualFleetForm()
    return _render(
        request,
        "fleetops/manual_fleet.html",
        {"form": form, "eve_time": timezone.now(), "needs_approval": not is_approver(request.user)},
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def my_statistics_view(request):
    from fleetops.views_pap import live_pap_rows

    year, month = _year_month(request)
    stats = member_statistics(request.user, year, month)
    return _render(
        request,
        "fleetops/my_statistics.html",
        _period_render_context(year, month, stats=stats, live_pap_rows=live_pap_rows(request.user)),
    )


@permission_required("fleetops.view_corp_stats", raise_exception=True)
def corporation_statistics_view(request):
    profile = getattr(request.user, "profile", None)
    main = getattr(profile, "main_character", None)
    if not main or not main.corporation_id:
        raise Http404
    year, month = _year_month(request)
    stats = corporation_statistics(main.corporation_id, year, month)
    stats["corporation_name"] = main.corporation_name
    return _render(
        request,
        "fleetops/corporation_statistics.html",
        _period_render_context(year, month, stats=stats, own_corporation=True),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def corporation_statistics_detail_view(request, corporation_id):
    year, month = _year_month(request)
    corp_name = (
        UserProfile.objects.filter(main_character__corporation_id=corporation_id)
        .exclude(main_character=None)
        .values_list("main_character__corporation_name", flat=True)
        .first()
    )
    if not corp_name:
        raise Http404
    stats = corporation_statistics(corporation_id, year, month)
    stats["corporation_name"] = corp_name
    return _render(
        request,
        "fleetops/corporation_statistics.html",
        _period_render_context(year, month, stats=stats, own_corporation=False),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def all_corporation_statistics_view(request):
    year, month = _year_month(request)
    corps = (
        UserProfile.objects.exclude(main_character=None)
        .exclude(main_character__corporation_id=None)
        .values("main_character__corporation_id", "main_character__corporation_name")
        .distinct()
        .order_by("main_character__corporation_name")
    )
    rows = []
    for corp in corps:
        stat = corporation_statistics(corp["main_character__corporation_id"], year, month)
        stat["corporation_name"] = corp["main_character__corporation_name"] or str(stat["corporation_id"])
        rows.append(stat)
    return _render(
        request,
        "fleetops/all_corporation_statistics.html",
        _period_render_context(year, month, rows=rows),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def all_fc_statistics_view(request):
    year, month = _year_month(request)
    operation_qs = counted_operations(FleetOperation.objects.filter(started_at__year=year, started_at__month=month))
    user_ids = set(operation_qs.values_list("fc_user_id", flat=True))
    user_ids.update(
        OperationRoleAssignment.objects.filter(
            operation__in=operation_qs, grants_fc_credit=True
        ).exclude(auth_user=None).values_list("auth_user_id", flat=True)
    )
    member_ids = current_member_user_ids()
    if member_ids is not None:
        user_ids &= member_ids
    # Reuse the same definition shown to individual FCs.
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(pk__in=user_ids).select_related("profile__main_character")
    rows = []
    for user in users:
        stat = fc_statistics(user, year, month)
        stat["user"] = user
        rows.append(stat)
    rows.sort(key=lambda r: (-r["total_points"], -r["fleet_count"], r["user"].username))
    return _render(
        request,
        "fleetops/all_fc_statistics.html",
        _period_render_context(year, month, rows=rows),
    )


@permission_required("fleetops.basic_access", raise_exception=True)
def fc_statistics_detail_view(request, user_id):
    from django.contrib.auth import get_user_model

    if request.user.pk != user_id and not request.user.has_perm("fleetops.view_all_stats"):
        raise Http404
    user = get_object_or_404(
        get_user_model().objects.select_related("profile__main_character"), pk=user_id
    )
    year, month = _year_month(request)
    stat = fc_statistics(user, year, month)
    operations = fc_operations_queryset(user, year, month).select_related("fleet_type").order_by("-started_at")
    return _render(
        request,
        "fleetops/fc_statistics_detail.html",
        _period_render_context(
            year,
            month,
            stat=stat,
            fc_user=user,
            operations=operations,
            can_open_fleets=user.pk == request.user.pk or _can_view_all_fleets(request.user),
        ),
    )


def _incentive_page(request, year, month, *, period=None, form=None, edit_form=None):
    """The incentive page of a month: the create form, or the period with its edit form."""
    if period is None and form is None:
        form = IncentivePeriodForm(initial={"minimum_fleets": FleetOpsSettings.get_solo().incentive_minimum_fleets})
    if period is not None and period.status != IncentivePeriod.Status.FINALIZED and edit_form is None:
        edit_form = IncentivePeriodForm(instance=period)
    start, end = month_bounds(year, month)
    closed = FleetOperation.objects.filter(
        status=FleetOperation.Status.CLOSED, started_at__gte=start, started_at__lt=end
    )
    pending = pending_multiplier_operations(period or IncentivePeriod(year=year, month=month))
    return _render(
        request,
        "fleetops/incentive_review.html",
        _period_render_context(
            year,
            month,
            period=period,
            form=form,
            edit_form=edit_form,
            pending_multiplier_operations=list(pending.select_related("fleet_type")[:PENDING_MULTIPLIERS_SHOWN]),
            pending_multiplier_count=pending.count(),
            not_counted_count=closed.count() - counted_operations(closed).count(),
        ),
    )


@permission_required("fleetops.manage_incentives", raise_exception=True)
def incentive_review(request):
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    year, month = _year_month(request)
    period = IncentivePeriod.objects.filter(year=year, month=month).first()
    if request.method != "POST":
        return _incentive_page(request, year, month, period=period)
    # The create form posts to the page of its month; never fall back to another month.
    if (request.GET.get("year"), request.GET.get("month")) != (str(year), str(month)):
        messages.error(request, "Choose the month of the new incentive period first.")
        return redirect("fleetops:incentive_review")
    if period is not None:
        # A page opened before the month was created; show the month as it is.
        messages.info(request, f"The incentive period {period} already exists; nothing was changed.")
        return _incentive_page(request, year, month, period=period)
    # The month comes from the page (?year=&month=); the form only holds the budget and the minimum.
    form = IncentivePeriodForm(request.POST, instance=IncentivePeriod(year=year, month=month))
    if not form.is_valid():
        return _incentive_page(request, year, month, form=form)
    try:
        with transaction.atomic():
            period = form.save()
    except IntegrityError:
        # Another manager created the month at the same moment.
        period = IncentivePeriod.objects.filter(year=year, month=month).first()
        if period is None:
            raise
        messages.info(request, f"The incentive period {period} was created meanwhile; nothing was changed.")
        return _incentive_redirect(period)
    audit(
        request.user,
        "incentive.period_create",
        period,
        None,
        {"budget": period.budget, "minimum_fleets": period.minimum_fleets},
    )
    messages.success(request, f"Incentive period {period} created. Recalculate to build the FC statistics.")
    return _incentive_redirect(period)


@permission_required("fleetops.manage_incentives", raise_exception=True)
@require_POST
def incentive_recalculate(request, pk):
    period = get_object_or_404(IncentivePeriod, pk=pk)
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    old_status = period.status
    try:
        rebuild_period(period)
    except IncentiveError as exc:
        messages.error(request, str(exc))
        return _incentive_redirect(period)
    audit(request.user, "incentive.recalculate", period, {"status": old_status}, {"status": period.status})
    messages.success(request, "Incentive period recalculated and moved to Review.")
    return _incentive_redirect(period)


@permission_required("fleetops.manage_incentives", raise_exception=True)
@require_POST
def incentive_update(request, pk):
    period = get_object_or_404(IncentivePeriod, pk=pk)
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    with transaction.atomic():
        # Locked so a finalize cannot slip in between the check and the save.
        period = IncentivePeriod.objects.select_for_update().get(pk=period.pk)
        if period.status == IncentivePeriod.Status.FINALIZED:
            messages.error(
                request, "This period is finalized. Unlock it before changing the budget or the minimum fleets."
            )
            return _incentive_redirect(period)
        old = {"budget": period.budget, "minimum_fleets": period.minimum_fleets}
        # A copy, so a refused value never shows up as the period's own value on the page.
        form = IncentivePeriodForm(request.POST, instance=IncentivePeriod.objects.get(pk=period.pk))
        if form.is_valid():
            period = form.save()
    if form.errors:
        return _incentive_page(request, period.year, period.month, period=period, edit_form=form)
    new = {"budget": period.budget, "minimum_fleets": period.minimum_fleets}
    if new == old:
        messages.info(request, "Budget and minimum fleets are unchanged.")
        return _incentive_redirect(period)
    audit(request.user, "incentive.period_update", period, old, new)
    messages.success(request, "Budget and minimum fleets saved. Recalculate to apply them.")
    return _incentive_redirect(period)


@permission_required("fleetops.manage_incentives", raise_exception=True)
@require_POST
def incentive_finalize(request, pk):
    period = get_object_or_404(IncentivePeriod, pk=pk)
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    old_status = period.status
    try:
        finalize_period(period, request.user)
    except IncentiveError as exc:
        messages.error(request, str(exc))
        return _incentive_redirect(period)
    audit(request.user, "incentive.finalize", period, {"status": old_status}, {"status": period.status})
    messages.success(request, "Incentive period finalized.")
    return _incentive_redirect(period)


@permission_required("fleetops.manage_incentives", raise_exception=True)
@require_POST
def incentive_unlock(request, pk):
    period = get_object_or_404(IncentivePeriod, pk=pk)
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    old_status = period.status
    try:
        unlock_period(period)
    except IncentiveError as exc:
        messages.error(request, str(exc))
        return _incentive_redirect(period)
    audit(request.user, "incentive.unlock", period, {"status": old_status}, {"status": period.status})
    messages.warning(request, "Incentive period unlocked.")
    return _incentive_redirect(period)


@permission_required("fleetops.manage_incentives", raise_exception=True)
@require_POST
def incentive_waiver(request, pk, user_id):
    period = get_object_or_404(IncentivePeriod, pk=pk)
    row = get_object_or_404(MonthlyFCStatistic, period=period, fc_user_id=user_id)
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    if period.status == IncentivePeriod.Status.FINALIZED:
        messages.error(request, WAIVER_LOCKED_MESSAGE)
        return _incentive_redirect(period)
    old = row.waived
    new = request.POST.get("waived") == "1"
    try:
        updated = set_waiver(period, user_id, new)
    except IncentiveError as exc:
        messages.error(request, str(exc))
        return _incentive_redirect(period)
    audit(request.user, "incentive.waiver", row, {"waived": old}, {"waived": new})
    if updated is None:
        messages.warning(
            request, "This FC no longer has credited fleets in this month. Payouts were recalculated without them."
        )
    else:
        messages.success(request, "FC waiver updated and payouts recalculated.")
    return _incentive_redirect(period)


@permission_required("fleetops.manage_attendance", raise_exception=True)
@require_POST
def add_manual_attendance(request, operation_uuid):
    operation = get_object_or_404(FleetOperation, uuid=operation_uuid)
    if not _can_manage_attendance_operation(request.user, operation):
        raise Http404
    if operation.status not in RECORDABLE_STATUSES:
        messages.error(request, "Manual attendance can only be added to Active or Closed fleets.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    form = ManualAttendanceForm(request.POST)
    if not form.is_valid():
        details = " ".join(str(error) for errors in form.errors.values() for error in errors)
        messages.error(request, f"Invalid manual attendance entry. {details}".strip())
        _keep_manual_attendance_entry(request, operation, [str(e) for errors in form.errors.values() for e in errors])
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    _save_manual_attendance(request, operation, form.cleaned_data)
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


# A manual attendance entry the fleet page could not save is shown there again, with its errors.
MANUAL_ATTENDANCE_ENTRY_KEY = "fleetops_manual_attendance_entry"


def _keep_manual_attendance_entry(request, operation, errors):
    request.session[MANUAL_ATTENDANCE_ENTRY_KEY] = {
        "operation": str(operation.uuid),
        "values": {name: request.POST.get(name, "") for name in ManualAttendanceForm.base_fields},
        "errors": errors,
    }


def _save_manual_attendance(request, operation, data, *, form=None):
    """Create a manual PAP row and tell the user; the row, or None when it was refused.

    Refusals go to ``form`` when there is one, else to the fleet page.
    """
    try:
        record = create_manual_attendance(
            operation,
            actor=request.user,
            character_id=data["character_id"],
            character_name=data["character_name"],
            attendance_value=data["attendance_value"],
            duplicate_action=data["duplicate_action"],
            notes=data["notes"],
        )
    except ValueError as exc:
        if form is not None:
            form.add_error(None, str(exc))
        else:
            messages.error(request, str(exc))
            _keep_manual_attendance_entry(request, operation, [str(exc)])
        return None
    text = "Historical manual attendance saved." if form is not None else "Manual attendance saved."
    if record.source in AttendanceRecord.USER_SOURCES and record.review_status == ReviewStatus.PENDING:
        text += " It counts once an FC lead approves it."
    messages.success(request, text)
    return record


@permission_required("fleetops.manage_attendance", raise_exception=True)
@require_POST
def delete_manual_attendance(request, pk):
    record = get_object_or_404(AttendanceRecord, pk=pk, source__in=AttendanceRecord.USER_SOURCES)
    operation = record.operation
    if not _can_manage_attendance_operation(request.user, operation):
        raise Http404
    if not can_delete_manual_row(request.user, record):
        # Deleting an accepted correction would change PAP without an FC lead.
        raise Http404
    audit(
        request.user,
        "attendance.manual_delete",
        record,
        {"attendance_value": record.attendance_value},
        None,
    )
    record.delete()
    # A deleted correction gives the tracked PAP back.
    pap.refresh_after_change(operation, actor=request.user, reason="manual attendance deleted")
    messages.warning(request, "Manual attendance deleted.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.manage_attendance", raise_exception=True)
def manual_attendance_view(request):
    roster_fleet = request.GET.get("roster_fleet")
    if roster_fleet:
        # "Paste roster" picks the fleet first.
        operation = manageable_attendance_operations(request.user).filter(pk=_database_id(roster_fleet) or 0).first()
        if operation is not None and _can_manage_attendance_operation(request.user, operation):
            return redirect("fleetops:roster_import", operation_uuid=operation.uuid)
        messages.error(request, "Choose an active or closed fleet you manage.")
        return redirect("fleetops:manual_attendance")
    if request.method == "POST":
        form = HistoricalManualAttendanceForm(request.POST, user=request.user)
        if form.is_valid():
            operation = form.cleaned_data["operation"]
            if not _can_manage_attendance_operation(request.user, operation):
                raise Http404
            if _save_manual_attendance(request, operation, form.cleaned_data, form=form) is not None:
                return redirect("fleetops:manual_attendance")
    else:
        form = HistoricalManualAttendanceForm(user=request.user)
    recent = AttendanceRecord.objects.filter(source__in=AttendanceRecord.USER_SOURCES)
    if not request.user.has_perm("fleetops.manage_fleets"):
        recent = recent.filter(
            Q(operation__fc_user=request.user)
            | Q(operation__role_assignments__auth_user=request.user, operation__role_assignments__grants_fc_credit=True)
        ).distinct()
    recent_rows = list(recent.select_related("operation__fleet_type", "created_by").order_by("-created_at")[:100])
    return _render(
        request,
        "fleetops/manual_attendance.html",
        {
            "form": form,
            "recent_manual": recent_rows,
            "deletable_row_ids": {row.pk for row in recent_rows if can_delete_manual_row(request.user, row)},
            "roster_fleets": manageable_attendance_operations(request.user)[:200],
            "needs_approval": not is_approver(request.user),
        },
    )


def _history_context(request, queryset, title, scope_label, retention_days):
    page = Paginator(queryset, HISTORY_PAGE_SIZE).get_page(request.GET.get("page"))
    return {
        "rows": page.object_list,
        "page": page,
        "title": title,
        "scope_label": scope_label,
        "total": queryset.aggregate(total=Sum("attendance_value"))["total"] or 0,
        "retention_days": retention_days,
        "can_open_fleets": _can_view_all_fleets(request.user),
    }


@permission_required("fleetops.basic_access", raise_exception=True)
def attendance_history_me(request):
    retention_days = max(MIN_RETENTION_DAYS, FleetOpsSettings.get_solo().data_retention_days)
    rows = attendance_history_queryset(user=request.user, days=retention_days)
    return _render(
        request,
        "fleetops/attendance_history.html",
        _history_context(request, rows, "My Attendance History", "Personal", retention_days),
    )


@permission_required("fleetops.view_corp_stats", raise_exception=True)
def attendance_history_corporation(request):
    main = getattr(getattr(request.user, "profile", None), "main_character", None)
    if not main or not main.corporation_id:
        raise Http404
    retention_days = max(MIN_RETENTION_DAYS, FleetOpsSettings.get_solo().data_retention_days)
    rows = attendance_history_queryset(corporation_id=main.corporation_id, days=retention_days)
    return _render(
        request,
        "fleetops/attendance_history.html",
        _history_context(request, rows, f"{main.corporation_name} Attendance History", "Corporation", retention_days),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def attendance_history_alliance(request):
    retention_days = max(MIN_RETENTION_DAYS, FleetOpsSettings.get_solo().data_retention_days)
    rows = attendance_history_queryset(alliance=True, days=retention_days)
    return _render(
        request,
        "fleetops/attendance_history.html",
        _history_context(request, rows, "Alliance Attendance History", "Alliance", retention_days),
    )


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def add_operation_role(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    form = OperationRoleAssignmentForm(request.POST, operation=operation)
    if not form.is_valid():
        details = " ".join(str(error) for errors in form.errors.values() for error in errors)
        messages.error(request, f"Invalid special-role assignment. {details}".strip())
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    try:
        assignment = add_role_assignment(
            operation,
            actor=request.user,
            role=form.cleaned_data["role"],
            character_id=form.cleaned_data["character_id"],
            notes=form.cleaned_data["notes"],
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        text = f"{assignment.character_name} assigned as {assignment.role_label}"
        if assignment.grants_fc_credit:
            messages.success(request, f"{text}; FC credit granted.")
        elif form.cleaned_data["role"].grants_fc_credit:
            messages.success(request, f"{text}. No FC credit: the character is not registered in Alliance Auth.")
        else:
            messages.success(request, f"{text}.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def delete_operation_role(request, pk):
    assignment = get_object_or_404(OperationRoleAssignment.objects.select_related("operation"), pk=pk)
    operation = assignment.operation
    if not _can_manage_operation(request.user, operation):
        raise Http404
    delete_role_assignment(assignment, actor=request.user)
    messages.warning(request, "Special-role assignment removed.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def kick_capsules_view(request, operation_uuid):
    # Imported here because the fleet control views import their helpers from this module.
    from fleetops.views_controls import posted_character_ids, report_kicks

    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation) or operation.status != FleetOperation.Status.ACTIVE:
        raise Http404
    # The capsule list lets the FC spare pilots; without a selection every capsule pilot is kicked.
    selected = posted_character_ids(request) if "character_ids" in request.POST else None
    try:
        result = kick_members(operation, selected, actor=request.user, purpose=CAPSULES)
    except KickRefused as exc:
        messages.error(request, str(exc))
    else:
        report_kicks(request, result, "capsule member")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.view_audit_log", raise_exception=True)
def audit_log_view(request):
    entries = AuditLog.objects.select_related("actor").order_by("-created_at", "-pk")
    page = Paginator(entries, AUDIT_PAGE_SIZE).get_page(request.GET.get("page"))
    return _render(request, "fleetops/audit_log.html", {"rows": page.object_list, "page": page})
