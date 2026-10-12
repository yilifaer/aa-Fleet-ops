import uuid
from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.db import IntegrityError, transaction
from django.utils import timezone
from kombu.exceptions import OperationalError as QueueError

from fleetops.models import FleetOperation, FleetOpsSettings, OperationAction, OperationActionAttempt, end_reason_text
from fleetops.providers.esi import FleetESIError, detect_character_fleet, set_fleet_motd
from fleetops.providers.pings import send_discord_webhook
from fleetops.providers.srp import create_srp_link
from fleetops.services.audit import audit
from fleetops.services.attendance import set_operation_attendance_multiplier
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.identity import character_label, get_owned_character
from fleetops.services.messages import render_messages
from fleetops.services.tracking import sync_operation

logger = get_extension_logger(__name__)

NOT_RENDERED = "Not sent because the fleet messages could not be rendered."
FINISHED_STATUSES = (FleetOperation.Status.CLOSED, FleetOperation.Status.CANCELLED)
EndReason = FleetOperation.EndReason
# A start moves its record forward within seconds. One that has not moved for this long was
# interrupted (for example its web worker was killed) and no longer holds its ESI fleet.
STARTING_ABANDONED_AFTER = timedelta(minutes=5)
START_ABANDONED_MESSAGE = "The fleet start did not finish, so this operation was cancelled. Start the fleet again to track it."


def _record_attempt(obj):
    OperationActionAttempt.objects.create(action=obj, status=obj.status, message=obj.error_message)


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
    _record_attempt(obj)
    return obj


def not_boss_message(character_name, boss_id):
    return f"{character_name} is not the fleet boss. The current boss is {character_label(boss_id)}."


def _skipped_message(operation, step):
    mode = "Manual fleet" if operation.is_manual else "Attendance-only mode"
    return f"{mode}: {step} intentionally skipped."


def _keep_skipped(operation, action, message):
    """Return the record of a step this fleet never performs, without attempting it."""
    obj, created = OperationAction.objects.get_or_create(
        operation=operation,
        action=action,
        defaults={"status": OperationAction.Status.SKIPPED, "error_message": message},
    )
    if created:
        _record_attempt(obj)
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


def cancel_abandoned_starts(*, actor=None, esi_fleet_id=None, now=None):
    """Cancel operations whose start was interrupted and never finished; returns how many."""
    cutoff = (now or timezone.now()) - STARTING_ABANDONED_AFTER
    stale = FleetOperation.objects.filter(status=FleetOperation.Status.STARTING, updated_at__lt=cutoff)
    if esi_fleet_id is not None:
        stale = stale.filter(esi_fleet_id=esi_fleet_id)
    cancelled = 0
    for pk in stale.values_list("pk", flat=True):
        with transaction.atomic():
            operation = FleetOperation.objects.select_for_update().get(pk=pk)
            # The start may have finished since the query above.
            if operation.status != FleetOperation.Status.STARTING or operation.updated_at >= cutoff:
                continue
            old_state = {"status": operation.status, "tracking_enabled": operation.tracking_enabled}
            ended_at = timezone.now()
            operation.status = FleetOperation.Status.CANCELLED
            operation.tracking_enabled = False
            operation.ended_at = ended_at
            operation.last_error = START_ABANDONED_MESSAGE
            operation.save(update_fields=["status", "tracking_enabled", "ended_at", "last_error", "updated_at"])
            operation.member_states.filter(is_active=True).update(is_active=False, left_at=ended_at)
            audit(
                actor,
                "fleet.start_abandoned",
                operation,
                old_state,
                {"status": operation.status, "tracking_enabled": False},
                reason=START_ABANDONED_MESSAGE,
            )
        cancelled += 1
    return cancelled


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
    # Only the fleet boss can read and write the fleet. The boss may sit in any position, and
    # a fleet commander who is not the boss cannot run the fleet.
    if not detection.is_boss(char_id):
        raise FleetESIError("NOT_FLEET_BOSS", not_boss_message(character.character_name, detection.fleet_boss_id))

    fleet_type = cleaned_data["fleet_type"]
    full_mode = cleaned_data.get("operation_mode", "full") == "full"
    try:
        with transaction.atomic():
            # The settings row serialises fleet starts, so two requests for the same ESI fleet
            # (two tabs, a resubmitted form) cannot both create a record.
            FleetOpsSettings.objects.select_for_update().get_or_create(pk=1)
            existing = FleetOperation.objects.filter(start_request_id=request_id).first()
            if existing:
                return existing
            # An interrupted start would otherwise hold this ESI fleet for good.
            cancel_abandoned_starts(actor=user, esi_fleet_id=detection.fleet_id)
            running = (
                FleetOperation.objects.filter(esi_fleet_id=detection.fleet_id, status__in=FleetOperation.OPEN_STATUSES)
                .order_by("pk")
                .first()
            )
            if running:
                running.already_tracked = True
                return running
            operation = FleetOperation.objects.create(
                start_request_id=request_id,
                status=FleetOperation.Status.STARTING,
                created_by=user,
                fc_user=user,
                fc_character_id=character.character_id,
                fc_character_name=character.character_name,
                fc_main_character_id=getattr(main, "character_id", None),
                fc_main_character_name=getattr(main, "character_name", "") or "",
                fleet_boss_character_id=detection.fleet_boss_id,
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

    srp_fields = ["srp_provider", "srp_reference", "srp_url", "srp_error", "updated_at"]
    with transaction.atomic():
        # A start that stalled for STARTING_ABANDONED_AFTER may have been cancelled meanwhile;
        # it must not come back as a second active record for its ESI fleet.
        current = FleetOperation.objects.select_for_update().only("status").get(pk=operation.pk)
        if current.status == FleetOperation.Status.STARTING:
            operation.status = FleetOperation.Status.ACTIVE
            operation.save(update_fields=["status", "last_error", *srp_fields])
        else:
            operation.save(update_fields=srp_fields)
            operation.refresh_from_db()
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


def end_fleet(operation, actor=None, automatic=False, attendance_multiplier=None, reason="", detail=""):
    """Close a fleet: final sync (manual ends only), attendance multiplier, member cleanup.

    An automatic end comes right after a confirming read, so it does not sync again.
    Ending a closed or cancelled fleet does nothing.
    """
    if operation.status in FINISHED_STATUSES:
        return operation
    reason = reason or (EndReason.FLEET_GONE if automatic else EndReason.MANUAL)
    detail = (detail or "")[:255]
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        # Closed meanwhile, or an end requested from the fleet page is already finishing it
        # (for example while a tracking poll that still saw the fleet Active was running).
        ending_elsewhere = current.status == FleetOperation.Status.ENDING and operation.status != current.status
        if current.status in FINISHED_STATUSES or ending_elsewhere:
            operation.refresh_from_db()
            return operation
        old_state = {"status": current.status, "tracking_enabled": current.tracking_enabled}
        operation.refresh_from_db()
        operation.status = FleetOperation.Status.ENDING
        operation.end_reason = reason
        operation.end_detail = detail
        update_fields = ["status", "end_reason", "end_detail", "updated_at"]
        if operation.end_requested_at is None:
            operation.end_requested_at = timezone.now()
            operation.end_requested_by = actor
            update_fields += ["end_requested_at", "end_requested_by"]
        operation.save(update_fields=update_fields)
    if not automatic:
        try:
            sync_operation(operation)
        except Exception as exc:
            # Best effort: a fleet that is already gone must still close.
            report_failure(logger, "Final fleet sync", exc, operation)

    with transaction.atomic():
        # Another worker may have closed the fleet meanwhile (recovery of a slow end).
        if FleetOperation.objects.select_for_update().get(pk=operation.pk).status in FINISHED_STATUSES:
            operation.refresh_from_db()
            return operation
        operation.refresh_from_db()
        if attendance_multiplier is None:
            attendance_multiplier = operation.attendance_multiplier or 1
        set_operation_attendance_multiplier(operation, attendance_multiplier, actor=actor)
        label = end_reason_text(reason, detail)
        ended_at = timezone.now()
        operation.tracking_enabled = False
        operation.ended_at = ended_at
        operation.status = FleetOperation.Status.CLOSED
        operation.last_error = label if automatic else ""
        operation.save(
            update_fields=[
                "tracking_enabled", "ended_at", "status", "last_error", "fleet_missing_count", "updated_at",
            ]
        )
        # Whoever was still in fleet left when it ended.
        operation.member_states.filter(is_active=True).update(is_active=False, left_at=ended_at)
        set_action(operation, "fleet_end", True, label)
        audit(
            actor,
            "fleet.auto_end" if automatic else "fleet.end",
            operation,
            old_state,
            {
                "status": operation.status,
                "tracking_enabled": operation.tracking_enabled,
                "end_reason": reason,
                "end_detail": detail,
            },
            reason=label,
        )
    return operation


def request_fleet_end(operation, *, actor, attendance_multiplier=None):
    """Record a manual end request and finish it in the background.

    Returns ``(operation, requested)``; ``requested`` is False when the fleet was already
    ending or closed, so a double click never ends a fleet twice.
    """
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if current.status in FINISHED_STATUSES or current.status == FleetOperation.Status.ENDING:
            return current, False
        old_status = current.status
        current.status = FleetOperation.Status.ENDING
        current.end_requested_at = timezone.now()
        current.end_requested_by = actor
        current.end_requested_multiplier = attendance_multiplier
        current.end_reason = EndReason.MANUAL
        current.end_detail = ""
        current.save(
            update_fields=[
                "status", "end_requested_at", "end_requested_by", "end_requested_multiplier",
                "end_reason", "end_detail", "updated_at",
            ]
        )
        audit(
            actor,
            "fleet.end_request",
            current,
            {"status": old_status},
            {
                "status": current.status,
                "attendance_multiplier": attendance_multiplier or current.attendance_multiplier,
            },
        )
    _queue_end(current)
    current.refresh_from_db()
    return current, True


def _queue_end(operation):
    from fleetops.tasks import finish_fleet_end

    try:
        finish_fleet_end.delay(operation.pk)
    except QueueError as exc:
        # Without a reachable queue the end is finished in this request instead.
        log_failure(logger, "Queueing the fleet end", exc, operation)
        finish_requested_end(operation)


def finish_requested_end(operation):
    """Finish a fleet whose end was requested or interrupted; does nothing unless it is Ending."""
    if operation.status != FleetOperation.Status.ENDING:
        return operation
    automatic = operation.end_reason in (EndReason.FLEET_GONE, EndReason.BOSS_HANDOVER)
    return end_fleet(
        operation,
        actor=operation.end_requested_by,
        automatic=automatic,
        attendance_multiplier=operation.end_requested_multiplier,
        reason=operation.end_reason or EndReason.MANUAL,
        detail=operation.end_detail,
    )


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
