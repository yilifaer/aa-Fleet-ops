import uuid

from django.db import transaction
from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings, OperationAction
from fleetops.providers.esi import FleetESIError, detect_character_fleet, set_fleet_motd
from fleetops.providers.pings import send_discord_webhook
from fleetops.providers.srp import create_srp_link
from fleetops.services.audit import audit
from fleetops.services.attendance import set_operation_attendance_multiplier
from fleetops.services.identity import get_owned_character
from fleetops.services.messages import render_operation_messages
from fleetops.services.tracking import sync_operation


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
        ping, motd = render_operation_messages(operation)
        operation.ping_text = ping
        operation.motd_text = motd
        operation.save(update_fields=["ping_text", "motd_text", "updated_at"])

    if full_mode:
        target = operation.ping_target
        webhook_url = target.webhook.webhook_url if target and target.webhook and target.webhook.is_active else ""
        if target and webhook_url:
            result = send_discord_webhook(webhook_url, operation.ping_text)
            set_action(operation, "discord_ping", result.success, result.message)
        else:
            set_action(operation, "discord_ping", False, "No active webhook configured. Ping remains available for manual copy.")

        try:
            set_fleet_motd(user, char_id, operation.esi_fleet_id, operation.motd_text)
            set_action(operation, "motd_update", True)
        except Exception as exc:
            set_action(operation, "motd_update", False, exc)
    else:
        set_action(operation, "discord_ping", None, "Attendance-only mode: Discord ping intentionally skipped.")
        set_action(operation, "motd_update", None, "Attendance-only mode: MOTD update intentionally skipped.")

    try:
        sync_operation(operation)
        set_action(operation, "tracking_start", True)
    except Exception as exc:
        set_action(operation, "tracking_start", False, exc)
        operation.last_error = str(exc)[:4000]

    settings_obj = FleetOpsSettings.get_solo()
    if not full_mode:
        set_action(operation, "srp_link", None, "Attendance-only mode: SRP creation intentionally skipped.")
    elif settings_obj.srp_auto_create:
        try:
            srp = create_srp_link(operation, settings_obj.srp_provider)
            operation.srp_provider = srp.provider
            operation.srp_reference = srp.reference
            operation.srp_url = srp.url
            operation.srp_error = "" if srp.created else srp.message
            if srp.created:
                set_action(operation, "srp_link", True, srp.message)
            else:
                set_action(operation, "srp_link", None, srp.message)
        except Exception as exc:
            operation.srp_error = str(exc)[:4000]
            set_action(operation, "srp_link", False, exc)
    else:
        set_action(operation, "srp_link", None, "Automatic SRP link creation is disabled.")

    operation.status = FleetOperation.Status.ACTIVE
    operation.save(update_fields=["status", "last_error", "srp_provider", "srp_reference", "srp_url", "srp_error", "updated_at"])
    return operation


def retry_ping(operation):
    target = operation.ping_target
    webhook_url = target.webhook.webhook_url if target and target.webhook and target.webhook.is_active else ""
    result = send_discord_webhook(webhook_url, operation.ping_text)
    return set_action(operation, "discord_ping", result.success, result.message)


def retry_motd(operation):
    try:
        set_fleet_motd(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id, operation.motd_text)
        return set_action(operation, "motd_update", True)
    except Exception as exc:
        return set_action(operation, "motd_update", False, exc)



def retry_srp(operation):
    settings_obj = FleetOpsSettings.get_solo()
    try:
        srp = create_srp_link(operation, settings_obj.srp_provider)
        operation.srp_provider = srp.provider
        operation.srp_reference = srp.reference
        operation.srp_url = srp.url
        operation.srp_error = "" if srp.created else srp.message
        operation.save(
            update_fields=["srp_provider", "srp_reference", "srp_url", "srp_error", "updated_at"]
        )
        if srp.created:
            return set_action(operation, "srp_link", True, srp.message)
        return set_action(operation, "srp_link", None, srp.message)
    except Exception as exc:
        operation.srp_error = str(exc)[:4000]
        operation.save(update_fields=["srp_error", "updated_at"])
        return set_action(operation, "srp_link", False, exc)

def end_fleet(operation, actor=None, automatic=False, attendance_multiplier=None):
    if operation.status in (FleetOperation.Status.CLOSED, FleetOperation.Status.CANCELLED):
        return operation
    operation.status = FleetOperation.Status.ENDING
    operation.save(update_fields=["status", "updated_at"])
    try:
        sync_operation(operation)
    except Exception:
        pass
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
        {"status": FleetOperation.Status.ACTIVE, "tracking_enabled": True},
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
