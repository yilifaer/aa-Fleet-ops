import calendar
import logging
import uuid
from datetime import timedelta

from allianceauth.authentication.decorators import permissions_required
from allianceauth.authentication.models import UserProfile
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.deletion import ProtectedError
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
    ChannelPresetForm,
    CommsPresetForm,
    DiscordWebhookForm,
    EndFleetForm,
    FleetAttendanceMultiplierForm,
    FleetOpsSettingsForm,
    FleetTypeForm,
    HistoricalManualAttendanceForm,
    IncentivePeriodForm,
    ManualAttendanceForm,
    ManualFleetForm,
    MessageTemplateForm,
    OperationRoleAssignmentForm,
    OperationEditForm,
    PingTargetForm,
    StartFleetForm,
)
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetOperation,
    FleetMemberState,
    FleetOpsSettings,
    FleetType,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    OperationRoleAssignment,
    PingTarget,
)
from fleetops.providers.esi import FleetESIError, detect_character_fleet
from fleetops.services.audit import audit
from fleetops.services.attendance import (
    RECORDABLE_STATUSES,
    create_manual_attendance,
    set_operation_attendance_multiplier,
)
from fleetops.services.dashboard import dashboard_metrics
from fleetops.services.fleet_controls import kick_all_pods, pod_members
from fleetops.services.identity import get_owned_character
from fleetops.services.history import current_member_user_ids
from fleetops.services.incentives import (
    IncentiveError,
    finalize_period,
    rebuild_period,
    set_waiver,
    unlock_period,
)
from fleetops.services.messages import render_operation_messages
from fleetops.services.operations import create_manual_fleet, end_fleet, retry_motd, retry_ping, retry_srp, start_fleet
from fleetops.services.roles import add_role_assignment, delete_role_assignment
from fleetops.services.routing import proximity_rows
from fleetops.services.statistics import (
    attendance_history_queryset,
    corporation_statistics,
    fc_operations_queryset,
    fc_statistics,
    member_statistics,
)

logger = logging.getLogger(__name__)

# Fleets running longer than this get the 1x/2x/3x attendance prompt when ended.
ATTENDANCE_PROMPT_AFTER = timedelta(minutes=90)
HISTORY_PAGE_SIZE = 200
AUDIT_PAGE_SIZE = 50
MAX_DATABASE_ID = 2**63 - 1


def _render(request, template_name, context=None):
    """Render a FleetOps page with the context shared by the navigation."""
    context = dict(context or {})
    context["show_incentives"] = (
        request.user.has_perm("fleetops.manage_incentives")
        and FleetOpsSettings.get_solo().incentive_enabled
    )
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


def _can_view_operation(user, operation):
    if _can_view_all_fleets(user):
        return True
    if not user.has_perm("fleetops.basic_access"):
        return False
    if _is_credited_fc(user, operation):
        return True
    if operation.role_assignments.filter(auth_user=user).exists():
        return True
    return operation.attendance_records.filter(auth_user=user, granted=True).exists()


def _can_manage_attendance_operation(user, operation):
    if not user.has_perm("fleetops.manage_attendance"):
        return False
    return user.has_perm("fleetops.manage_fleets") or _is_credited_fc(user, operation)


def _operation_for_user(request, operation_uuid, manage=False):
    operation = get_object_or_404(
        FleetOperation.objects.select_related(
            "fleet_type", "comms", "logi_channel", "boost_channel",
            "ping_target__webhook", "fc_user",
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
    messages.info(request, "FC incentives are disabled in the FleetOps settings.")
    return False


@permission_required("fleetops.basic_access", raise_exception=True)
def dashboard(request):
    active_qs = FleetOperation.objects.filter(status=FleetOperation.Status.ACTIVE)
    if not _can_view_all_fleets(request.user):
        active_qs = active_qs.filter(
            Q(fc_user=request.user)
            | Q(role_assignments__auth_user=request.user)
            | Q(attendance_records__auth_user=request.user, attendance_records__granted=True)
        ).distinct()
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
        .exclude(status=FleetOperation.Status.DRAFT)
        .distinct()[:10]
    )
    now = timezone.now()
    fc = fc_statistics(request.user, now.year, now.month, include_active=True)
    metrics = dashboard_metrics(request.user)
    return _render(
        request,
        "fleetops/dashboard.html",
        {
            "active_operations": active,
            "own_operations": own,
            "fc_stats": fc,
            "dashboard_cards": metrics["cards"],
            "top_ships": metrics["top_ships"],
            "recent_participations": metrics["recent"],
        },
    )


@permission_required("fleetops.start_fleet", raise_exception=True)
def start_fleet_view(request):
    if request.method == "POST":
        form = StartFleetForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                operation = start_fleet(
                    user=request.user,
                    cleaned_data=form.cleaned_data,
                    request_id=form.cleaned_data["request_id"],
                )
            except (FleetESIError, PermissionError) as exc:
                form.add_error(None, str(exc))
            except Exception:
                logger.exception("Fleet start failed")
                form.add_error(None, "Fleet start failed because of an unexpected error. Please try again or contact an administrator.")
            else:
                messages.success(
                    request,
                    "Fleet operation started. Review individual step status below.",
                )
                return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    else:
        form = StartFleetForm(user=request.user, initial={"request_id": uuid.uuid4()})
    return _render(request, "fleetops/start_fleet.html", {"form": form})


@permission_required("fleetops.start_fleet", raise_exception=True)
@require_POST
def preview_fleet(request):
    form = StartFleetForm(request.POST, user=request.user)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)
    data = form.cleaned_data
    ownership = get_owned_character(request.user, int(data["fc_character_id"]))
    if ownership is None:
        return JsonResponse(
            {"ok": False, "error": "Character is not owned by this user."}, status=403
        )
    profile = getattr(request.user, "profile", None)
    main = getattr(profile, "main_character", None)
    op = FleetOperation(
        uuid=uuid.uuid4(),
        created_by=request.user,
        fc_user=request.user,
        fc_character_id=ownership.character.character_id,
        fc_character_name=ownership.character.character_name,
        fc_main_character_id=getattr(main, "character_id", None),
        fc_main_character_name=getattr(main, "character_name", "") or "",
        fleet_type=data["fleet_type"],
        doctrine_name=data.get("doctrine_name", ""),
        doctrine_external_id=data.get("doctrine_external_id", ""),
        doctrine_source=data.get("doctrine_source", ""),
        formup=data["formup"],
        comms=data.get("comms"),
        logi_channel=data.get("logi_channel"),
        boost_channel=data.get("boost_channel"),
        ping_target=data.get("ping_target"),
        ping_template=data.get("ping_template"),
        motd_template=data.get("motd_template"),
        scheduled_at=data.get("scheduled_at"),
        additional_message=data.get("additional_message", ""),
    )
    try:
        ping, motd = render_operation_messages(op)
    except Exception:
        logger.exception("FleetOps message preview failed")
        return JsonResponse(
            {"ok": False, "error": "The ping or MOTD template could not be rendered. Check the message templates."},
            status=400,
        )
    return JsonResponse({"ok": True, "ping": ping, "motd": motd})


@permission_required("fleetops.start_fleet", raise_exception=True)
def detect_fleet(request, character_id):
    if get_owned_character(request.user, character_id) is None:
        return JsonResponse(
            {"ok": False, "code": "NOT_OWNED", "message": "Character is not owned by this user."},
            status=403,
        )
    try:
        result = detect_character_fleet(request.user, character_id)
    except FleetESIError as exc:
        return JsonResponse({"ok": False, "code": exc.code, "message": str(exc)})
    return JsonResponse(
        {
            "ok": True,
            "fleet_id": result.fleet_id,
            "role": result.role,
            "is_fleet_boss": result.role == "fleet_commander",
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

    qs = FleetOperation.objects.exclude(status=FleetOperation.Status.DRAFT).select_related(
        "fleet_type", "fc_user", "comms"
    )
    if not _can_view_all_fleets(request.user):
        qs = qs.filter(
            Q(fc_user=request.user)
            | Q(role_assignments__auth_user=request.user)
            | Q(attendance_records__auth_user=request.user, attendance_records__granted=True)
        ).distinct()

    qs = qs.filter(started_at__year=year)
    if month:
        qs = qs.filter(started_at__month=month)

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
        AttendanceRecord.objects.filter(operation_id=OuterRef("pk"), granted=True)
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
    ).order_by("-started_at", "-created_at")

    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    for operation in page.object_list:
        operation.can_edit = _can_manage_operation(request.user, operation)
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


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
def edit_operation_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    old = model_to_dict(
        operation,
        fields=[
            "fleet_type", "doctrine_name", "formup", "comms", "logi_channel",
            "boost_channel", "additional_message", "started_at", "ended_at",
        ],
    )
    if request.method == "POST":
        form = OperationEditForm(request.POST, instance=operation)
        if form.is_valid():
            fleet_type_changed = form.cleaned_data["fleet_type"].pk != old.get("fleet_type")
            updated = form.save(commit=False)
            if fleet_type_changed:
                updated.fleet_point_weight_snapshot = updated.fleet_type.point_weight
            updated.save()
            new = model_to_dict(
                updated,
                fields=[
                    "fleet_type", "doctrine_name", "formup", "comms", "logi_channel",
                    "boost_channel", "additional_message", "started_at", "ended_at",
                ],
            )
            audit(
                request.user,
                "operation.edit",
                updated,
                {k: str(v) for k, v in old.items()},
                {k: str(v) for k, v in new.items()},
                reason="Edited from FleetOps fleet archive",
            )
            messages.success(request, "Fleet operation updated.")
            return redirect("fleetops:operation_detail", operation_uuid=updated.uuid)
    else:
        form = OperationEditForm(instance=operation)
    return _render(request, "fleetops/operation_edit.html", {"operation": operation, "form": form})


@permission_required("fleetops.basic_access", raise_exception=True)
def operation_detail(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid)
    actions = operation.actions.all()
    members = operation.member_states.filter(is_active=True).order_by("character_name")
    assignable_members = operation.member_states.all().order_by("character_name")
    events = operation.member_events.all()[:100]
    attendance = operation.attendance_records.all()
    attendance_total = sum(r.attendance_value for r in attendance if r.granted)
    unique_players = members.exclude(auth_user=None).values("auth_user_id").distinct().count()
    map_rows = proximity_rows(operation, 3)
    role_assignments = operation.role_assignments.select_related("auth_user", "assigned_by").all()
    pods = pod_members(operation) if operation.status == FleetOperation.Status.ACTIVE else []
    settings = FleetOpsSettings.get_solo()
    end_reference = operation.ended_at or timezone.now()
    elapsed = end_reference - operation.started_at if operation.started_at else timedelta(0)
    duration_minutes = max(0, int(elapsed.total_seconds() // 60))
    stale = bool(
        operation.last_esi_update
        and (timezone.now() - operation.last_esi_update).total_seconds()
        > settings.stale_threshold
    )
    return _render(
        request,
        "fleetops/operation_detail.html",
        {
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
            "pod_count": len(pods),
            "stale": stale,
            "can_manage": _can_manage_operation(request.user, operation),
            "can_manage_attendance": _can_manage_attendance_operation(request.user, operation),
            "accepts_records": operation.status in RECORDABLE_STATUSES,
            "manual_attendance_form": ManualAttendanceForm(),
            "multiplier_form": FleetAttendanceMultiplierForm(initial={"attendance_multiplier": operation.attendance_multiplier}),
            "end_form": EndFleetForm(initial={"attendance_multiplier": operation.attendance_multiplier}),
            "duration_minutes": duration_minutes,
            "attendance_prompt_eligible": (
                operation.status == FleetOperation.Status.ACTIVE and elapsed > ATTENDANCE_PROMPT_AFTER
            ),
        },
    )


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
            messages.error(request, "Attendance multiplier must be 1x, 2x or 3x. The fleet was not ended.")
            return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
        multiplier = form.cleaned_data["attendance_multiplier"]
    # Without an explicit choice the fleet keeps its current multiplier.
    operation = end_fleet(operation, actor=request.user, attendance_multiplier=multiplier)
    messages.success(request, f"Fleet ended. Attendance finalized at {operation.attendance_multiplier}x.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def retry_ping_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    action = retry_ping(operation)
    detail = f" {action.error_message}" if action.error_message else ""
    messages.info(request, f"Ping retry: {action.get_status_display()}.{detail}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def retry_motd_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    action = retry_motd(operation)
    detail = f" {action.error_message}" if action.error_message else ""
    messages.info(request, f"MOTD retry: {action.get_status_display()}.{detail}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def retry_srp_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation):
        raise Http404
    action = retry_srp(operation)
    detail = f" {action.error_message}" if action.error_message else ""
    messages.info(request, f"SRP retry: {action.get_status_display()}.{detail}")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.manage_attendance", raise_exception=True)
@require_POST
def set_attendance_multiplier_view(request, operation_uuid):
    operation = get_object_or_404(FleetOperation, uuid=operation_uuid)
    if not _can_manage_attendance_operation(request.user, operation):
        raise Http404
    form = FleetAttendanceMultiplierForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Attendance multiplier must be 1x, 2x or 3x.")
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    multiplier = form.cleaned_data["attendance_multiplier"]
    updated = set_operation_attendance_multiplier(operation, multiplier, actor=request.user)
    messages.success(request, f"Fleet attendance set to {multiplier}x ({updated} automatic records updated).")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.create_manual_fleet", raise_exception=True)
def manual_fleet_view(request):
    if request.method == "POST":
        form = ManualFleetForm(request.POST)
        if form.is_valid():
            try:
                operation = create_manual_fleet(user=request.user, cleaned_data=form.cleaned_data)
            except Exception as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Manual fleet record created. Add attendance from the Attendance tab or Manual Attendance page.")
                return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    else:
        form = ManualFleetForm(initial={"started_at": timezone.now(), "ended_at": timezone.now()})
    return _render(request, "fleetops/manual_fleet.html", {"form": form})


@permission_required("fleetops.basic_access", raise_exception=True)
def my_statistics_view(request):
    year, month = _year_month(request)
    stats = member_statistics(request.user, year, month)
    return _render(
        request,
        "fleetops/my_statistics.html",
        _period_render_context(year, month, stats=stats),
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
    operation_qs = FleetOperation.objects.filter(
        status=FleetOperation.Status.CLOSED, started_at__year=year, started_at__month=month
    )
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


@permission_required("fleetops.manage_incentives", raise_exception=True)
def incentive_review(request):
    if not _incentives_enabled(request):
        return redirect("fleetops:dashboard")
    year, month = _year_month(request)
    period = IncentivePeriod.objects.filter(year=year, month=month).first()
    if request.method == "POST" and request.POST.get("create_period"):
        form = IncentivePeriodForm(request.POST)
        if form.is_valid():
            period = form.save()
            audit(
                request.user,
                "incentive.period_create",
                period,
                None,
                {"budget": period.budget, "minimum_fleets": period.minimum_fleets},
            )
            return _incentive_redirect(period)
    else:
        form = IncentivePeriodForm(
            initial={
                "year": year,
                "month": month,
                "minimum_fleets": FleetOpsSettings.get_solo().incentive_minimum_fleets,
            }
        )
    return _render(
        request,
        "fleetops/incentive_review.html",
        _period_render_context(year, month, period=period, form=form),
    )


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
        messages.error(request, "Unlock the finalized period before changing waivers.")
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
    form = ManualAttendanceForm(request.POST)
    if not form.is_valid():
        details = " ".join(str(error) for errors in form.errors.values() for error in errors)
        messages.error(request, f"Invalid manual attendance entry. {details}".strip())
        return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)
    data = form.cleaned_data
    try:
        create_manual_attendance(
            operation,
            actor=request.user,
            character_id=data["character_id"],
            character_name=data["character_name"],
            attendance_value=data["attendance_value"],
            duplicate_action=data["duplicate_action"],
            notes=data["notes"],
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Manual attendance saved.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.manage_attendance", raise_exception=True)
@require_POST
def delete_manual_attendance(request, pk):
    record = get_object_or_404(
        AttendanceRecord, pk=pk, source=AttendanceRecord.Source.MANUAL
    )
    operation = record.operation
    if not _can_manage_attendance_operation(request.user, operation):
        raise Http404
    audit(
        request.user,
        "attendance.manual_delete",
        record,
        {"attendance_value": record.attendance_value},
        None,
    )
    record.delete()
    messages.warning(request, "Manual attendance deleted.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.manage_attendance", raise_exception=True)
def manual_attendance_view(request):
    if request.method == "POST":
        form = HistoricalManualAttendanceForm(request.POST, user=request.user)
        if form.is_valid():
            operation = form.cleaned_data["operation"]
            if not _can_manage_attendance_operation(request.user, operation):
                raise Http404
            data = form.cleaned_data
            try:
                create_manual_attendance(
                    operation,
                    actor=request.user,
                    character_id=data["character_id"],
                    character_name=data["character_name"],
                    attendance_value=data["attendance_value"],
                    duplicate_action=data["duplicate_action"],
                    notes=data["notes"],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Historical manual attendance saved.")
                return redirect("fleetops:manual_attendance")
    else:
        form = HistoricalManualAttendanceForm(user=request.user)
    recent = AttendanceRecord.objects.filter(source=AttendanceRecord.Source.MANUAL)
    if not request.user.has_perm("fleetops.manage_fleets"):
        recent = recent.filter(
            Q(operation__fc_user=request.user)
            | Q(operation__role_assignments__auth_user=request.user, operation__role_assignments__grants_fc_credit=True)
        ).distinct()
    return _render(
        request,
        "fleetops/manual_attendance.html",
        {"form": form, "recent_manual": recent.select_related("operation__fleet_type", "created_by")[:100]},
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
    retention_days = max(365, FleetOpsSettings.get_solo().data_retention_days)
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
    retention_days = max(365, FleetOpsSettings.get_solo().data_retention_days)
    rows = attendance_history_queryset(corporation_id=main.corporation_id, days=retention_days)
    return _render(
        request,
        "fleetops/attendance_history.html",
        _history_context(request, rows, f"{main.corporation_name} Attendance History", "Corporation", retention_days),
    )


@permission_required("fleetops.view_all_stats", raise_exception=True)
def attendance_history_alliance(request):
    retention_days = max(365, FleetOpsSettings.get_solo().data_retention_days)
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
        messages.error(request, "Invalid special-role assignment.")
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
        if assignment.grants_fc_credit:
            messages.success(request, f"{assignment.character_name} assigned; FC credit granted.")
        else:
            messages.success(request, f"{assignment.character_name} assigned.")
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
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _can_manage_operation(request.user, operation) or operation.status != FleetOperation.Status.ACTIVE:
        raise Http404
    result = kick_all_pods(operation, actor=request.user)
    if result["failures"]:
        messages.warning(request, f"Kicked {len(result['kicked'])} capsule members; {len(result['failures'])} failed.")
    else:
        messages.success(request, f"Kicked {len(result['kicked'])} capsule members.")
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


@permission_required("fleetops.view_audit_log", raise_exception=True)
def audit_log_view(request):
    entries = AuditLog.objects.select_related("actor").order_by("-created_at", "-pk")
    page = Paginator(entries, AUDIT_PAGE_SIZE).get_page(request.GET.get("page"))
    return _render(request, "fleetops/audit_log.html", {"rows": page.object_list, "page": page})


# ---------------------------------------------------------------------------
# Front-end configuration center
# ---------------------------------------------------------------------------

CONFIG_SECTIONS = {
    "fleet-types": {
        "title": "Fleet Types",
        "description": "Fleet categories and the FC point weight attached to each type.",
        "model": FleetType,
        "form": FleetTypeForm,
        "columns": [
            ("Name", "name"),
            ("Short name", "short_name"),
            ("Point weight", "point_weight"),
            ("Active", "is_active"),
            ("Sort", "sort_order"),
        ],
    },
    "comms": {
        "title": "Comms Presets",
        "description": "Reusable voice/comms choices shown to FCs when starting a fleet.",
        "model": CommsPreset,
        "form": CommsPresetForm,
        "columns": [
            ("Name", "name"),
            ("Channel", "channel_name"),
            ("Voice link", "voice_url"),
            ("Active", "is_active"),
        ],
    },
    "channels": {
        "title": "Logi / Boost Channels",
        "description": "Reusable Logi and Boost channel choices shown on Fleet Start.",
        "model": ChannelPreset,
        "form": ChannelPresetForm,
        "columns": [
            ("Name", "name"),
            ("Type", "channel_type"),
            ("Value", "channel_value"),
            ("Active", "is_active"),
        ],
    },
    "webhooks": {
        "title": "Discord Webhooks",
        "description": "Discord destinations used by Ping Targets. Webhook secrets are only available to configuration managers.",
        "model": DiscordWebhook,
        "form": DiscordWebhookForm,
        "columns": [
            ("Name", "name"),
            ("Active", "is_active"),
        ],
    },
    "ping-targets": {
        "title": "Ping Targets",
        "description": "Reusable ping mentions/targets and their Discord destination.",
        "model": PingTarget,
        "form": PingTargetForm,
        "columns": [
            ("Name", "name"),
            ("Target", "target_value"),
            ("Webhook", "webhook"),
            ("Active", "is_active"),
        ],
    },
    "templates": {
        "title": "Message Templates",
        "description": "Ping and Fleet MOTD templates. Template variables are rendered when the fleet is prepared.",
        "model": MessageTemplate,
        "form": MessageTemplateForm,
        "columns": [
            ("Name", "name"),
            ("Type", "template_type"),
            ("Default", "is_default"),
            ("Active", "is_active"),
        ],
    },
}


def _configuration_section(section):
    config = CONFIG_SECTIONS.get(section)
    if config is None:
        raise Http404
    return config


def _configuration_snapshot(obj):
    data = model_to_dict(obj)
    # Never write Discord webhook secrets into FleetOps audit JSON.
    if isinstance(obj, DiscordWebhook) and "webhook_url" in data:
        data["webhook_url"] = "***redacted***" if data["webhook_url"] else ""
    for key, value in list(data.items()):
        if hasattr(value, "pk"):
            data[key] = value.pk
        elif not isinstance(value, (str, int, float, bool, type(None), list, dict)):
            data[key] = str(value)
    return data


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_index(request):
    settings_obj = FleetOpsSettings.get_solo()
    cards = [
        {
            "key": key,
            "title": config["title"],
            "description": config["description"],
            "count": config["model"].objects.count(),
        }
        for key, config in CONFIG_SECTIONS.items()
    ]
    return _render(
        request,
        "fleetops/configuration/index.html",
        {"settings_obj": settings_obj, "cards": cards},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_settings(request):
    obj = FleetOpsSettings.get_solo()
    old = _configuration_snapshot(obj)
    if request.method == "POST":
        form = FleetOpsSettingsForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            audit(
                request.user,
                "configuration.update",
                obj,
                old,
                _configuration_snapshot(obj),
                reason="Updated from FleetOps front-end administration",
            )
            messages.success(request, "FleetOps settings updated.")
            return redirect("fleetops:configuration_index")
    else:
        form = FleetOpsSettingsForm(instance=obj)
    return _render(
        request,
        "fleetops/configuration/settings_form.html",
        {"form": form, "settings_obj": obj},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_list(request, section):
    config = _configuration_section(section)
    objects = config["model"].objects.all()
    rows = []
    for obj in objects:
        values = []
        for label, field_name in config["columns"]:
            value = getattr(obj, field_name)
            if field_name == "channel_type" and hasattr(obj, "get_channel_type_display"):
                value = obj.get_channel_type_display()
            elif field_name == "template_type" and hasattr(obj, "get_template_type_display"):
                value = obj.get_template_type_display()
            values.append((label, value))
        rows.append({"object": obj, "values": values})
    return _render(
        request,
        "fleetops/configuration/list.html",
        {"section": section, "config": config, "rows": rows},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
def configuration_edit(request, section, pk=None):
    config = _configuration_section(section)
    model = config["model"]
    obj = get_object_or_404(model, pk=pk) if pk is not None else None
    old = _configuration_snapshot(obj) if obj is not None else None
    if request.method == "POST":
        form = config["form"](request.POST, instance=obj)
        if form.is_valid():
            saved = form.save()
            audit(
                request.user,
                "configuration.update" if obj is not None else "configuration.create",
                saved,
                old,
                _configuration_snapshot(saved),
                reason="Changed from FleetOps front-end administration",
            )
            messages.success(request, f"{config['title']} saved.")
            return redirect("fleetops:configuration_list", section=section)
    else:
        form = config["form"](instance=obj)
    return _render(
        request,
        "fleetops/configuration/form.html",
        {"section": section, "config": config, "form": form, "object": obj},
    )


@permission_required("fleetops.manage_configuration", raise_exception=True)
@require_POST
def configuration_delete(request, section, pk):
    config = _configuration_section(section)
    obj = get_object_or_404(config["model"], pk=pk)
    old = _configuration_snapshot(obj)
    object_type = obj.__class__.__name__
    object_id = str(obj.pk)
    try:
        obj.delete()
    except ProtectedError:
        messages.error(
            request,
            "This item is referenced by existing fleet data and cannot be deleted. Disable it instead.",
        )
    else:
        AuditLog.objects.create(
            actor=request.user,
            action="configuration.delete",
            object_type=object_type,
            object_id=object_id,
            old_value=old,
            new_value=None,
            reason="Deleted from FleetOps front-end administration",
        )
        messages.warning(request, f"{config['title']} item deleted.")
    return redirect("fleetops:configuration_list", section=section)
