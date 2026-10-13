import uuid
from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.db import IntegrityError, transaction
from django.utils import timezone
from kombu.exceptions import OperationalError as QueueError

from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetOpsSettings,
    OperationAction,
    OperationActionAttempt,
    end_reason_text,
)
from fleetops.providers.doctrines import doctrine_fits_snapshot
from fleetops.providers.esi import FleetESIError, detect_character_fleet, set_fleet_motd
from fleetops.providers.pings import send_discord_webhook
from fleetops.providers.srp import create_srp_link
from fleetops.services.audit import audit
from fleetops.services.counting import evaluate_stats_inclusion
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.identity import character_label, get_owned_character
from fleetops.services.mentions import (
    MentionSelection,
    allowed_mentions,
    limit_mass_mentions,
    selection_from_operation,
    snapshot_roles,
    validate_for_channel,
)
from fleetops.services.messages import render_messages
from fleetops.services.multipliers import close_multiplier
from fleetops.services.names import queue_name_resolution, stale_character_ids, unnamed_character_ids
from fleetops.services.pap import finalize_pap
from fleetops.services.review import initial_review_fields
from fleetops.services.tracking import sync_operation

logger = get_extension_logger(__name__)

NOT_RENDERED = "Not sent because the fleet messages could not be rendered."
FINISHED_STATUSES = (FleetOperation.Status.CLOSED, FleetOperation.Status.CANCELLED)
EndReason = FleetOperation.EndReason
# A start moves its record forward within seconds. One that has not moved for this long was
# interrupted (for example its web worker was killed) and no longer holds its ESI fleet.
STARTING_ABANDONED_AFTER = timedelta(minutes=5)
START_ABANDONED_MESSAGE = "The fleet start did not finish, so this operation was cancelled. Start the fleet again to track it."
NOT_OWNED_MESSAGE = "Selected character is not owned by the current Alliance Auth user."
NO_PING_CHANNEL = "No Discord channel selected; the ping is available to copy."
PING_NOT_REQUESTED = "Form-up ping not requested."
EMPTY_PING = "Not sent because the ping text is empty."


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


def _ping_channel_problem(operation, action):
    """Record why nothing can be posted to the fleet's ping channel; None when something can."""
    channel = operation.ping_channel
    if channel is None:
        return set_action(operation, action, None, NO_PING_CHANNEL)
    if not channel.is_active:
        return set_action(operation, action, False, f"Ping channel {channel.name} is disabled.")
    return None


def _post_to_ping_channel(operation, action, text, mentions):
    if not text:
        return set_action(operation, action, False, EMPTY_PING)
    result = send_discord_webhook(operation.ping_channel.webhook_url, text, mentions)
    return set_action(operation, action, result.success, result.message)


def _send_ping(operation, *, action="discord_ping"):
    """Post the stored ping to the fleet's ping channel, notifying exactly the stored mention."""
    problem = _ping_channel_problem(operation, action)
    if problem is not None:
        return problem
    selection = selection_from_operation(operation)
    # The channel or the roles may have changed since the FC picked them.
    errors = validate_for_channel(selection, operation.ping_channel)
    if errors:
        return set_action(operation, action, False, " ".join(errors))
    text = limit_mass_mentions(operation.ping_text, selection)
    return _post_to_ping_channel(operation, action, text, allowed_mentions(selection))


def send_channel_notice(operation, action, text):
    """Post ``text`` to the fleet's ping channel without notifying anybody."""
    problem = _ping_channel_problem(operation, action)
    if problem is not None:
        return problem
    return _post_to_ping_channel(operation, action, text, {"parse": []})


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
            rendered = render_messages(operation, pre_ping=operation.status == FleetOperation.Status.SCHEDULED)
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


def owned_fleet_character(user, character_id):
    """The user's own character, or PermissionError."""
    ownership = get_owned_character(user, int(character_id))
    if ownership is None:
        raise PermissionError(NOT_OWNED_MESSAGE)
    return ownership.character


def detect_fleet_boss(user, character):
    """Read the character's fleet now and make sure it is the fleet boss.

    A start trusts this read to take a fleet over from another boss, so it never comes from the
    response cache.
    """
    detection = detect_character_fleet(user, character.character_id, fresh=True)
    # Only the fleet boss can read and write the fleet. The boss may sit in any position, and
    # a fleet commander who is not the boss cannot run the fleet.
    if not detection.is_boss(character.character_id):
        raise FleetESIError("NOT_FLEET_BOSS", not_boss_message(character.character_name, detection.fleet_boss_id))
    return detection


def fc_values(user, character):
    main = getattr(getattr(user, "profile", None), "main_character", None)
    return {
        "fc_user": user,
        "fc_character_id": character.character_id,
        "fc_character_name": character.character_name,
        "fc_main_character_id": getattr(main, "character_id", None),
        "fc_main_character_name": getattr(main, "character_name", "") or "",
    }


def fleet_values(cleaned_data):
    """Fleet settings from the start or scheduled fleet form, as model field values."""
    fleet_type = cleaned_data["fleet_type"]
    full_mode = cleaned_data.get("operation_mode", "full") == "full"
    # Attendance-only fleets never post, so their ping is only ever copied.
    channel = cleaned_data.get("ping_channel") if full_mode else None
    selection = cleaned_data.get("mention_selection") or MentionSelection()
    source = cleaned_data.get("doctrine_source", "") or "custom"
    external_id = cleaned_data.get("doctrine_external_id", "") or ""
    return {
        "fleet_type": fleet_type,
        "fleet_point_weight_snapshot": fleet_type.point_weight,
        "doctrine_name": cleaned_data.get("doctrine_name", "") or "",
        "doctrine_external_id": external_id,
        "doctrine_source": source,
        "doctrine_fits": doctrine_fits_snapshot(source, external_id),
        "formup": cleaned_data["formup"],
        "comms": cleaned_data.get("comms"),
        "logi_channel": cleaned_data.get("logi_channel"),
        "boost_channel": cleaned_data.get("boost_channel"),
        "ping_channel": channel,
        "ping_channel_name": channel.name if channel else "",
        "ping_mention_mode": selection.mode,
        "ping_roles": snapshot_roles(selection),
        "ping_template": cleaned_data.get("ping_template"),
        "motd_template": cleaned_data.get("motd_template"),
        "additional_message": cleaned_data.get("additional_message", "") or "",
        "scheduled_at": cleaned_data.get("scheduled_at"),
        "send_ping": full_mode,
    }


def pap_rule_values(settings=None) -> dict:
    """The PAP minimum time and the per-player character limit a fleet keeps from its start on.

    Later changes of the settings never alter the PAP of a fleet that already started.
    """
    settings = settings or FleetOpsSettings.get_solo()
    return {
        "pap_minimum_minutes": settings.pap_minimum_minutes,
        "pap_character_limit": settings.attendance_limit,
    }


def operation_values(cleaned_data, *, user, character) -> dict:
    """Fields a start, a preview and a pre ping take from the start form."""
    return {"created_by": user, **fc_values(user, character), **fleet_values(cleaned_data)}


def build_unsaved_operation(user, cleaned_data) -> FleetOperation:
    """The fleet the start form describes, unsaved, for previews and the checks before a start."""
    character = owned_fleet_character(user, cleaned_data["fc_character_id"])
    return FleetOperation(**operation_values(cleaned_data, user=user, character=character))


def lock_fleet_starts():
    """Serialise fleet starts on the settings row. Call inside a transaction; returns the settings.

    Two requests for the same ESI fleet (two tabs, a resubmitted form, a new boss) then cannot
    both create a record.
    """
    settings, _created = FleetOpsSettings.objects.select_for_update().get_or_create(pk=1)
    return settings


def _open_records(esi_fleet_id):
    # A record whose boss handed the fleet over is closing and no longer owns it.
    return (
        FleetOperation.objects.filter(esi_fleet_id=esi_fleet_id, status__in=FleetOperation.OPEN_STATUSES)
        .exclude(status=FleetOperation.Status.ENDING, end_reason=EndReason.BOSS_HANDOVER)
        .order_by("pk")
    )


def claim_esi_fleet(detection, character, *, now):
    """Find out who else tracks the ESI fleet before a start. Call under ``lock_fleet_starts``.

    Returns ``(running, handed_over)``. ``running`` is the open record this FC character already
    runs for the fleet; the start returns it instead of creating a second one. Otherwise ESI has
    just confirmed ``character`` as the boss, so open records of other FC characters lost the
    fleet: they end at ``now`` with a boss handover and stop counting as open. The rest of their
    end runs in the background (``finish_handovers``). ``handed_over`` lists them with the status
    each had.
    """
    records = list(_open_records(detection.fleet_id).select_for_update())
    for record in records:
        if record.fc_character_id == character.character_id:
            return record, []
    handed_over = []
    for record in records:
        old_status = record.status
        record.status = FleetOperation.Status.ENDING
        record.end_reason = EndReason.BOSS_HANDOVER
        record.end_detail = character.character_name[:255]
        # The record keeps the boss it lost the fleet to, as tracking does for a handover it observes.
        record.fleet_boss_character_id = character.character_id
        update_fields = ["status", "end_reason", "end_detail", "fleet_boss_character_id", "updated_at"]
        # An end its FC requested earlier keeps that time.
        if record.end_requested_at is None:
            record.end_requested_at = now
            update_fields.append("end_requested_at")
        record.save(update_fields=update_fields)
        handed_over.append((record, old_status))
    return None, handed_over


def finish_handovers(handed_over):
    """Close the records a start took the fleet from; call after the start's transaction."""
    for record, _old_status in handed_over:
        _queue_end(record)


def record_start(operation, actor, *, character, from_schedule, handed_over):
    """The one ``fleet.start`` audit entry of a start, plus one entry per handover it caused.

    A fleet that started from a schedule takes its PAP rules here, when it starts.
    """
    if from_schedule or operation.pap_minimum_minutes is None:
        for name, value in pap_rule_values().items():
            setattr(operation, name, value)
        operation.save(update_fields=["pap_minimum_minutes", "pap_character_limit", "updated_at"])
    set_action(operation, "fleet_detection", True)
    audit(
        actor,
        "fleet.start",
        operation,
        {"status": FleetOperation.Status.SCHEDULED} if from_schedule else None,
        {
            "status": operation.status,
            "esi_fleet_id": operation.esi_fleet_id,
            "fc_character_id": operation.fc_character_id,
            "fleet_type": str(operation.fleet_type),
            "operation_mode": "full" if operation.send_ping else "attendance_only",
            "ping_channel": operation.ping_channel_name,
            "ping_mention_mode": operation.ping_mention_mode,
            "from_schedule": from_schedule,
            "handed_over_from": [record.pk for record, _old_status in handed_over],
        },
    )
    for record, old_status in handed_over:
        audit(
            actor,
            "fleet.boss_handover",
            record,
            {"status": old_status},
            {
                "status": record.status,
                "end_reason": record.end_reason,
                "end_detail": record.end_detail,
                "new_boss_character_id": character.character_id,
                "new_operation_id": operation.pk,
            },
            reason=end_reason_text(record.end_reason, record.end_detail),
        )


def start_fleet(*, user, cleaned_data: dict, request_id: uuid.UUID | None = None):
    request_id = request_id or uuid.uuid4()
    existing = FleetOperation.objects.filter(start_request_id=request_id).first()
    if existing:
        return existing

    character = owned_fleet_character(user, cleaned_data["fc_character_id"])
    detection = detect_fleet_boss(user, character)
    full_mode = cleaned_data.get("operation_mode", "full") == "full"
    values = operation_values(cleaned_data, user=user, character=character)
    try:
        with transaction.atomic():
            settings = lock_fleet_starts()
            existing = FleetOperation.objects.filter(start_request_id=request_id).first()
            if existing:
                return existing
            # An interrupted start would otherwise hold this ESI fleet for good.
            cancel_abandoned_starts(actor=user, esi_fleet_id=detection.fleet_id)
            now = timezone.now()
            running, handed_over = claim_esi_fleet(detection, character, now=now)
            if running:
                running.already_tracked = True
                return running
            operation = FleetOperation.objects.create(
                start_request_id=request_id,
                status=FleetOperation.Status.STARTING,
                fleet_boss_character_id=detection.fleet_boss_id,
                esi_fleet_id=detection.fleet_id,
                started_at=now,
                tracking_enabled=True,
                attendance_multiplier=1,
                is_manual=False,
                **pap_rule_values(settings),
                **values,
            )
            record_start(operation, user, character=character, from_schedule=False, handed_over=handed_over)
    except IntegrityError:
        # A concurrent submit of the same form created the operation first.
        existing = FleetOperation.objects.filter(start_request_id=request_id).first()
        if existing is None:
            raise
        return existing

    finish_handovers(handed_over)
    run_start_steps(operation, user=user, character_id=character.character_id, full_mode=full_mode)
    operation.handed_over = [record for record, _old_status in handed_over]
    return operation


def run_start_steps(operation, *, user, character_id, full_mode, send_ping=True) -> None:
    """Messages, ping, MOTD, tracking and SRP of a fleet whose record is Starting; then Active.

    Every step is best effort: failures are recorded and the fleet still becomes active.
    """
    messages_ready = False
    try:
        with transaction.atomic():
            rendered = render_messages(operation, actor=user)
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
        if send_ping:
            try:
                _send_ping(operation)
            except Exception as exc:
                set_action(operation, "discord_ping", False, report_failure(logger, "Discord ping", exc, operation))
        else:
            set_action(operation, "discord_ping", None, PING_NOT_REQUESTED)
        _update_motd(operation, user, character_id)

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
        # A start that stalled for STARTING_ABANDONED_AFTER may have been cancelled meanwhile, or a
        # new boss took the fleet over; it must not come back as a second active record.
        current = FleetOperation.objects.select_for_update().only("status").get(pk=operation.pk)
        if current.status == FleetOperation.Status.STARTING:
            operation.status = FleetOperation.Status.ACTIVE
            operation.save(update_fields=["status", "last_error", *srp_fields])
        else:
            operation.save(update_fields=srp_fields)
            operation.refresh_from_db()


def retry_ping(operation):
    # A fleet that has not started yet only has its pre ping.
    action = "pre_ping" if operation.status == FleetOperation.Status.SCHEDULED else "discord_ping"
    if not operation.send_ping:
        return _keep_skipped(operation, action, _skipped_message(operation, "Discord ping"))
    if not _render_missing_text(operation, "ping_text"):
        return set_action(operation, action, False, NOT_RENDERED)
    try:
        return _send_ping(operation, action=action)
    except Exception as exc:
        return set_action(operation, action, False, report_failure(logger, "Discord ping", exc, operation))


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


def _status_before_end(operation):
    """The status a fleet had before its end was requested; Ending only marks the request.

    The end request and a handover at a new start record it in their audit entries.
    """
    entries = AuditLog.objects.filter(
        object_type=FleetOperation.__name__,
        object_id=str(operation.pk),
        action__in=("fleet.end_request", "fleet.boss_handover"),
    ).order_by("-pk")
    for old_value in entries.values_list("old_value", flat=True):
        status = old_value.get("status") if isinstance(old_value, dict) else None
        if status and status != FleetOperation.Status.ENDING:
            return status
    return FleetOperation.Status.ACTIVE


def end_fleet(operation, actor=None, automatic=False, attendance_multiplier=None, reason="", detail=""):
    """Close a fleet: final sync (manual ends only), PAP multiplier, member cleanup, PAP.

    An automatic end comes right after a confirming read, so it does not sync again; neither
    does a fleet without ESI fleet. ``attendance_multiplier`` is the FC's choice, checked against
    the fleet's duration again (``close_multiplier``). Ending a closed or cancelled fleet does
    nothing.
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
        old_status = current.status
        if old_status == FleetOperation.Status.ENDING:
            old_status = _status_before_end(current)
        old_state = {"status": old_status, "tracking_enabled": current.tracking_enabled}
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
    if not automatic and operation.esi_fleet_id:
        try:
            # Time in fleet ends with the end request, also when the close runs later.
            sync_operation(operation, until=operation.end_requested_at)
        except Exception as exc:
            # Best effort: a fleet that is already gone must still close.
            report_failure(logger, "Final fleet sync", exc, operation)

    with transaction.atomic():
        # Another worker may have closed the fleet meanwhile (recovery of a slow end).
        if FleetOperation.objects.select_for_update().get(pk=operation.pk).status in FINISHED_STATUSES:
            operation.refresh_from_db()
            return operation
        operation.refresh_from_db()
        settings = FleetOpsSettings.get_solo()
        label = end_reason_text(reason, detail)
        ended_at = timezone.now()
        if operation.end_requested_at:
            # The fleet ended when its end was requested (or the boss handed it over), even if this
            # close runs later; every duration rule measures to that time, so a slow or recovered
            # close changes nothing.
            ended_at = min(ended_at, operation.end_requested_at)
        operation.ended_at = ended_at
        multiplier, source, pending = close_multiplier(operation, attendance_multiplier, settings)
        operation.attendance_multiplier = multiplier
        operation.multiplier_source = source
        operation.multiplier_pending = pending
        evaluate_stats_inclusion(operation, settings)
        operation.tracking_enabled = False
        operation.status = FleetOperation.Status.CLOSED
        operation.last_error = label if automatic else ""
        operation.save(
            update_fields=[
                "tracking_enabled", "ended_at", "status", "last_error", "fleet_missing_count",
                "attendance_multiplier", "multiplier_source", "multiplier_pending", "counts_for_stats",
                "updated_at",
            ]
        )
        # Whoever was still in fleet left when it ended.
        operation.member_states.filter(is_active=True).update(is_active=False, left_at=ended_at)
        summary = finalize_pap(operation, actor=actor)
        pap_text = f"PAP: {summary['granted']} granted at {multiplier}x."
        if pending:
            pap_text += " The multiplier choice is pending."
        set_action(operation, "fleet_end", True, f"{label}. {pap_text}" if label else pap_text)
        new_state = {
            "status": operation.status,
            "tracking_enabled": operation.tracking_enabled,
            "end_reason": reason,
            "end_detail": detail,
            "attendance_multiplier": multiplier,
            "multiplier_source": source,
            "multiplier_pending": pending,
            "counts_for_stats": operation.counts_for_stats,
            "pap": summary,
        }
        if reason == EndReason.BOSS_HANDOVER:
            # The detail is the new boss's name; keep the id too.
            new_state["fleet_boss_character_id"] = operation.fleet_boss_character_id
        audit(actor, "fleet.auto_end" if automatic else "fleet.end", operation, old_state, new_state, reason=label)
    try:
        # Tracking stops here, so members seen shortly before the end get one last lookup.
        queue_name_resolution(operation.pk, stale_character_ids(unnamed_character_ids(operation.pk)), force=True)
    except Exception as exc:
        log_failure(logger, "Queueing the name lookup", exc, operation)
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
    """Create a closed fleet record without ESI, Discord or automatic tracking.

    Manual fleets of users who are not FC leads wait for approval before they count.
    """
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
        multiplier_source=FleetOperation.MultiplierSource.MANUAL,
        is_manual=True,
        **pap_rule_values(),
        **initial_review_fields(user),
    )
    if not evaluate_stats_inclusion(operation):
        operation.save(update_fields=["counts_for_stats", "updated_at"])
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
            "review_status": operation.review_status,
            "counts_for_stats": operation.counts_for_stats,
        },
    )
    return operation
