import logging
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings, OperationAction
from fleetops.providers.esi import FleetESIError, detect_character_fleet, set_fleet_motd
from fleetops.providers.pings import send_discord_webhook
from fleetops.providers.srp import create_srp_link
from fleetops.services.audit import audit
from fleetops.services.attendance import set_operation_attendance_multiplier
from fleetops.services.errors import report_failure
from fleetops.services.identity import get_owned_character
from fleetops.services.messages import render_messages
from fleetops.services.tracking import sync_operation

logger = logging.getLogger(__name__)

NOT_RENDERED = "Not sent because the fleet messages could not be rendered."


def set_action(operation, action, success, error=""):
    obj, created = OperationAction.objects.get_or_create(operation=operation, action=action)
    if not created:
        obj.attempts += 1
    if success is None:
        obj.status = OperationAction.Status.SKIPPED
    else:
        obj.status = OperationAction.Status.SUCCESS if success else OperationAction.Status.FAILED
    obj.error_message = str(error or "")[:4000]
    obj.save()
    return obj


def _skipped_message(operation, step):
    mode = "Manual fleet" if operation.is_manual else "Attendance-only mode"
    return f"{mode}: {step} intentionally skipped."


def _keep_skipped(operation, action, message):
    """Return the record of a step this fleet never performs, without attempting it."""
    obj, _created = OperationAction.objects.get_or_create(
        operation=operation,
        action=action,
        defaults={"status": OperationAction.Status.SKIPPED, "error_message": message},
    )
    return obj


def _send_ping(operation):
    target = operation.ping_target
    webhook_url = target.webhook.webhook_url if target and target.webhook and target.webhook.is_active else ""
    if not webhook_url:
        return set_action(
            operation, "discord_ping", False, "No active webhook configured. Ping remains available for manual copy."
        )
    if not operation.ping_text:
        return set_action(operation, "discord_ping", False, "Not sent because the ping text is empty.")
    result = send_discord_webhook(webhook_url, operation.ping_text)
    return set_action(operation, "discord_ping", result.success, result.message)


def _update_motd(operation, user, character_id):
    # An empty MOTD would wipe the one the FC already set in game.
    if not operation.motd_text:
        return set_action(operation, "motd_update", False, "Not sent because the MOTD text is empty.")
    try:
        set_fleet_motd(user, character_id, operation.esi_fleet_id, operation.motd_text)
    except Exception as exc:
        return set_action(operation, "motd_update", False, report_failure(logger, "MOTD update", exc, operation))
    return set_action(operation, "motd_update", True)


def _render_missing_text(operation, field):
    """Render ``ping_text`` or ``motd_text`` again when it is empty, e.g. after a render failure at start.

    Returns False when the messages still cannot be rendered.
    """
    if getattr(operation, field):
        return True
    try:
        with transaction.atomic():
            rendered = render_messages(operation)
            setattr(operation, field, rendered.ping if field == "ping_text" else rendered.motd)
            operation.save(update_fields=[field, "updated_at"])
    except Exception as exc:
        set_action(operation, "message_render", False, report_failure(logger, "Message rendering", exc, operation))
        return False
    if rendered.errors:
        set_action(operation, "message_render", False, " ".join(rendered.errors))
    elif operation.actions.filter(action="message_render").exists():
        set_action(operation, "message_render", True)
    return True


def _apply_srp_result(operation, srp):
    # A declined or unavailable provider must never erase a link that already exists.
    if srp.created or not (operation.srp_reference or operation.srp_url):
        operation.srp_provider = srp.provider
        operation.srp_reference = srp.reference
        operation.srp_url = srp.url
    operation.srp_error = "" if srp.created else srp.message


def start_fleet(*, user, cleaned_data: dict, request_id: uuid.UUID | None = None):
    request_id = request_id or uuid.uuid4()
    existing = FleetOperation.objects.filter(start_request_id=request_id).first()
    if existing:
        return existing

    char_id = int(cleaned_data["fc_character_id"])
    ownership = get_owned_character(user, char_id)
    if ownership is None:
        raise PermissionError("Selected character is not owned by the current Alliance Auth user.")
    character = ownership.character
    profile = getattr(user, "profile", None)
    main = getattr(profile, "main_character", None)

    detection = detect_character_fleet(user, char_id)
    # ESI uses fleet_commander for the fleet boss / FC role.
    if detection.role != "fleet_commander":
        raise FleetESIError(
            "NOT_FLEET_BOSS",
            f"Selected character is in fleet with role '{detection.role}', not fleet_commander.",
        )

    fleet_type = cleaned_data["fleet_type"]
    full_mode = cleaned_data.get("operation_mode", "full") == "full"
    try:
        with transaction.atomic():
            operation = FleetOperation.objects.create(
                start_request_id=request_id,
                status=FleetOperation.Status.STARTING,
                created_by=user,
                fc_user=user,
                fc_character_id=character.character_id,
                fc_character_name=character.character_name,
                fc_main_character_id=getattr(main, "character_id", None),
                fc_main_character_name=getattr(main, "character_name", "") or "",
                fleet_boss_character_id=character.character_id,
                esi_fleet_id=detection.fleet_id,
                fleet_type=fleet_type,
                fleet_point_weight_snapshot=fleet_type.point_weight,
                doctrine_name=cleaned_data.get("doctrine_name", "") or "",
                doctrine_external_id=cleaned_data.get("doctrine_external_id", "") or "",
                doctrine_source=cleaned_data.get("doctrine_source", "") or "custom",
                formup=cleaned_data["formup"],
                comms=cleaned_data.get("comms"),
                logi_channel=cleaned_data.get("logi_channel"),
                boost_channel=cleaned_data.get("boost_channel"),
                ping_target=cleaned_data.get("ping_target"),
                ping_template=cleaned_data.get("ping_template"),
                motd_template=cleaned_data.get("motd_template"),
                additional_message=cleaned_data.get("additional_message", "") or "",
                scheduled_at=cleaned_data.get("scheduled_at"),
                started_at=timezone.now(),
                tracking_enabled=True,
                send_ping=full_mode,
                attendance_multiplier=1,
                is_manual=False,
            )
            set_action(operation, "fleet_detection", True)
    except IntegrityError:
        # A concurrent submit of the same form created the operation first.
        existing = FleetOperation.objects.filter(start_request_id=request_id).first()
        if existing is None:
            raise
        return existing

    # Every step below is best effort: failures are recorded and the fleet still becomes active.
    messages_ready = False
    try:
        with transaction.atomic():
            rendered = render_messages(operation)
            operation.ping_text = rendered.ping
            operation.motd_text = rendered.motd
            operation.save(update_fields=["ping_text", "motd_text", "updated_at"])
        messages_ready = True
        if rendered.errors:
            set_action(operation, "message_render", False, " ".join(rendered.errors))
    except Exception as exc:
        set_action(operation, "message_render", False, report_failure(logger, "Message rendering", exc, operation))

    if not full_mode:
        set_action(operation, "discord_ping", None, _skipped_message(operation, "Discord ping"))
        set_action(operation, "motd_update", None, _skipped_message(operation, "MOTD update"))
    elif not messages_ready:
        set_action(operation, "discord_ping", False, NOT_RENDERED)
        set_action(operation, "motd_update", False, NOT_RENDERED)
    else:
        try:
            _send_ping(operation)
        except Exception as exc:
            set_action(operation, "discord_ping", False, report_failure(logger, "Discord ping", exc, operation))
        _update_motd(operation, user, char_id)

    try:
        sync_operation(operation)
        set_action(operation, "tracking_start", True)
    except Exception as exc:
        operation.last_error = report_failure(logger, "Fleet tracking", exc, operation)[:4000]
        set_action(operation, "tracking_start", False, operation.last_error)

    if not full_mode:
        set_action(operation, "srp_link", None, _skipped_message(operation, "SRP creation"))
    else:
        try:
            settings_obj = FleetOpsSettings.get_solo()
            if settings_obj.srp_auto_create:
                with transaction.atomic():
                    srp = create_srp_link(operation, settings_obj.srp_provider)
                _apply_srp_result(operation, srp)
                set_action(operation, "srp_link", True if srp.created else None, srp.message)
            else:
                set_action(operation, "srp_link", None, "Automatic SRP link creation is disabled.")
        except Exception as exc:
            operation.srp_error = report_failure(logger, "SRP creation", exc, operation)[:4000]
            set_action(operation, "srp_link", False, operation.srp_error)

    operation.status = FleetOperation.Status.ACTIVE
    operation.save(update_fields=["status", "last_error", "srp_provider", "srp_reference", "srp_url", "srp_error", "updated_at"])
    return operation


def retry_ping(operation):
    if not operation.send_ping:
        return _keep_skipped(operation, "discord_ping", _skipped_message(operation, "Discord ping"))
    if not _render_missing_text(operation, "ping_text"):
        return set_action(operation, "discord_ping", False, NOT_RENDERED)
    try:
        return _send_ping(operation)
    except Exception as exc:
        return set_action(operation, "discord_ping", False, report_failure(logger, "Discord ping", exc, operation))


def retry_motd(operation):
    if not operation.send_ping:
        return _keep_skipped(operation, "motd_update", _skipped_message(operation, "MOTD update"))
    if not _render_missing_text(operation, "motd_text"):
        return set_action(operation, "motd_update", False, NOT_RENDERED)
    return _update_motd(operation, operation.fc_user, operation.fc_character_id)


def retry_srp(operation):
    # Manual records may link SRP later; attendance-only fleets never create one.
    if not operation.send_ping and not operation.is_manual:
        return _keep_skipped(operation, "srp_link", _skipped_message(operation, "SRP creation"))
    try:
        with transaction.atomic():
            # Lock the row so concurrent retries cannot both create an SRP fleet.
            current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
            if current.srp_reference or current.srp_url:
                return set_action(
                    operation, "srp_link", True, "SRP fleet is already linked; no new SRP fleet was created."
                )
            srp = create_srp_link(operation, FleetOpsSettings.get_solo().srp_provider)
            _apply_srp_result(operation, srp)
            operation.save(update_fields=["srp_provider", "srp_reference", "srp_url", "srp_error", "updated_at"])
            return set_action(operation, "srp_link", True if srp.created else None, srp.message)
    except Exception as exc:
        operation.srp_error = report_failure(logger, "SRP creation", exc, operation)[:4000]
        operation.save(update_fields=["srp_error", "updated_at"])
        return set_action(operation, "srp_link", False, operation.srp_error)


def end_fleet(operation, actor=None, automatic=False, attendance_multiplier=None):
    if operation.status in (FleetOperation.Status.CLOSED, FleetOperation.Status.CANCELLED):
        return operation
    old_state = {"status": operation.status, "tracking_enabled": operation.tracking_enabled}
    operation.status = FleetOperation.Status.ENDING
    operation.save(update_fields=["status", "updated_at"])
    try:
        sync_operation(operation)
    except Exception as exc:
        # Best effort: a fleet that is already gone must still close.
        report_failure(logger, "Final fleet sync", exc, operation)
    if attendance_multiplier is None:
        attendance_multiplier = operation.attendance_multiplier or 1
    set_operation_attendance_multiplier(operation, attendance_multiplier, actor=actor)
    operation.tracking_enabled = False
    operation.ended_at = timezone.now()
    operation.status = FleetOperation.Status.CLOSED
    operation.save(update_fields=["tracking_enabled", "ended_at", "status", "updated_at"])
    set_action(operation, "fleet_end", True, "Automatic" if automatic else "Manual")
    audit(
        actor,
        "fleet.auto_end" if automatic else "fleet.end",
        operation,
        old_state,
        {"status": operation.status, "tracking_enabled": operation.tracking_enabled},
    )
    return operation


def create_manual_fleet(*, user, cleaned_data: dict):
    """Create a closed fleet record without ESI, Discord or automatic tracking."""
    profile = getattr(user, "profile", None)
    main = getattr(profile, "main_character", None)
    if main is None:
        raise ValueError("A main character is required to create a manual fleet record.")
    started_at = cleaned_data["started_at"]
    ended_at = cleaned_data.get("ended_at") or max(timezone.now(), started_at)
    fleet_type = cleaned_data["fleet_type"]
    operation = FleetOperation.objects.create(
        status=FleetOperation.Status.CLOSED,
        created_by=user,
        fc_user=user,
        fc_character_id=main.character_id,
        fc_character_name=main.character_name,
        fc_main_character_id=main.character_id,
        fc_main_character_name=main.character_name,
        fleet_type=fleet_type,
        fleet_point_weight_snapshot=fleet_type.point_weight,
        doctrine_name=cleaned_data.get("doctrine_name", "") or "",
        doctrine_source="manual",
        formup=cleaned_data["formup"],
        additional_message=cleaned_data.get("notes", "") or "",
        started_at=started_at,
        ended_at=ended_at,
        tracking_enabled=False,
        send_ping=False,
        attendance_multiplier=int(cleaned_data.get("attendance_multiplier", 1) or 1),
        is_manual=True,
    )
    set_action(operation, "manual_fleet", True, "Manual fleet record created without ESI tracking.")
    set_action(operation, "discord_ping", None, "Manual fleet: no Discord ping sent.")
    set_action(operation, "motd_update", None, "Manual fleet: no MOTD update attempted.")
    set_action(operation, "tracking_start", None, "Manual fleet: no ESI tracking started.")
    set_action(operation, "srp_link", None, "Manual fleet: SRP can be linked/retried manually if needed.")
    audit(
        user,
        "fleet.manual_create",
        operation,
        None,
        {
            "fleet_type": str(fleet_type),
            "started_at": str(started_at),
            "ended_at": str(ended_at),
            "attendance_multiplier": operation.attendance_multiplier,
        },
    )
    return operation
