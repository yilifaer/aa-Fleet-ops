# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pre pings: fleets announced for a later time and started by their FC when it is due."""

import uuid

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from fleetops import texts
from fleetops.models import AuditLog, FleetOperation, OperationAction
from fleetops.providers.doctrines import doctrine_fits_snapshot
from fleetops.services.audit import audit
from fleetops.services.errors import report_failure
from fleetops.services.messages import render_cancellation_notice, render_messages
from fleetops.services.operations import (
    NOT_RENDERED,
    _send_ping,
    cancel_abandoned_starts,
    claim_esi_fleet,
    detect_fleet_boss,
    fc_values,
    finish_handovers,
    fleet_values,
    lock_fleet_starts,
    owned_fleet_character,
    record_start,
    run_start_steps,
    send_channel_notice,
    set_action,
)
from fleetops.services.periods import format_eve

logger = get_extension_logger(__name__)

Status = FleetOperation.Status

# Stored messages (English); the pages show them through ``translate_stored``.
NOT_SCHEDULED_MESSAGE = texts.NOT_SCHEDULED()
CHANNEL_CHANGE_NEEDS_POST = texts.CHANNEL_CHANGE_NEEDS_POST()
# How long a posted edit is remembered against a repeated submit of the same page.
EDIT_POST_TTL = 600
ONLY_SCHEDULING_FC_MESSAGE = texts.ONLY_SCHEDULING_FC()
# Fields an edit of a scheduled fleet can change, as listed in its audit entry.
EDITABLE_FIELDS = (
    "fleet_type",
    "doctrine_name",
    "doctrine_source",
    "doctrine_external_id",
    "formup",
    "comms",
    "logi_channel",
    "boost_channel",
    "ping_channel",
    "ping_mention_mode",
    "ping_roles",
    "ping_template",
    "motd_template",
    "additional_message",
    "scheduled_at",
)
# Foreign keys among them; the audit lists their primary keys.
_RELATIONS = {"fleet_type", "comms", "logi_channel", "boost_channel", "ping_channel", "ping_template", "motd_template"}
# The steps a start runs, as listed in its audit entry.
START_STEPS = ("fleet_detection", "message_render", "discord_ping", "motd_update", "tracking_start", "srp_link")


def is_pre_ping_record(operation) -> bool:
    """A fleet that was announced with a pre ping and never started (scheduled or cancelled)."""
    return (
        operation.status in (Status.SCHEDULED, Status.CANCELLED)
        and operation.scheduled_at is not None
        and operation.started_at is None
        and not operation.is_manual
    )


def _render_pre_ping(operation, *, actor):
    """Render and store the pre ping and the MOTD; False when they could not be rendered."""
    try:
        with transaction.atomic():
            rendered = render_messages(operation, actor=actor, pre_ping=True)
            operation.ping_text = rendered.ping
            operation.motd_text = rendered.motd
            operation.save(update_fields=["ping_text", "motd_text", "updated_at"])
    except Exception as exc:
        set_action(operation, "message_render", False, report_failure(logger, "Message rendering", exc, operation))
        return False
    if rendered.errors:
        set_action(operation, "message_render", False, " ".join(rendered.errors))
    elif operation.actions.filter(action="message_render").exists():
        set_action(operation, "message_render", True)
    return True


def _send_pre_ping(operation):
    try:
        return _send_ping(operation, action="pre_ping")
    except Exception as exc:
        return set_action(operation, "pre_ping", False, report_failure(logger, "Pre ping", exc, operation))


def schedule_fleet(*, user, cleaned_data, request_id=None) -> FleetOperation:
    """Create a scheduled fleet and post its pre ping. No ESI call: the fleet does not exist yet."""
    request_id = request_id or uuid.uuid4()
    existing = FleetOperation.objects.filter(start_request_id=request_id).first()
    if existing:
        return existing
    character = owned_fleet_character(user, cleaned_data["fc_character_id"])
    values = {"created_by": user, **fc_values(user, character), **fleet_values(cleaned_data)}
    try:
        with transaction.atomic():
            operation = FleetOperation.objects.create(
                start_request_id=request_id,
                status=Status.SCHEDULED,
                started_at=None,
                esi_fleet_id=None,
                tracking_enabled=False,
                attendance_multiplier=1,
                is_manual=False,
                **values,
            )
            audit(
                user,
                "fleet.schedule",
                operation,
                None,
                {
                    "status": operation.status,
                    "scheduled_at": format_eve(operation.scheduled_at),
                    "fc_character_id": operation.fc_character_id,
                    "fleet_type": str(operation.fleet_type),
                    "ping_channel": operation.ping_channel_name,
                    "ping_mention_mode": operation.ping_mention_mode,
                },
            )
    except IntegrityError:
        # A resubmitted form scheduled the fleet first.
        existing = FleetOperation.objects.filter(start_request_id=request_id).first()
        if existing is None:
            raise
        return existing

    if _render_pre_ping(operation, actor=user):
        _send_pre_ping(operation)
    else:
        set_action(operation, "pre_ping", False, NOT_RENDERED)
    return operation


def _snapshot(operation):
    values = {}
    for name in EDITABLE_FIELDS:
        value = getattr(operation, f"{name}_id") if name in _RELATIONS else getattr(operation, name)
        values[name] = format_eve(value) if name == "scheduled_at" else value
    return values


def _audit_value(value):
    return value if value is None or isinstance(value, (bool, int, list, dict)) else str(value)


def _edit_post_key(operation, edit_token):
    return f"fleetops:schedule-edit:{operation.pk}:{edit_token}"


def update_scheduled_fleet(operation, *, actor, cleaned_data, send_update: bool, edit_token=None) -> FleetOperation:
    """Change a scheduled fleet; posts a new pre ping only when ``send_update`` is set.

    The pre ping of one edit page (``edit_token``) is posted once: a repeated submit saves the
    changes again without posting, and the result's ``already_posted`` is True.
    """
    already_posted = False
    if send_update and edit_token:
        already_posted = not cache.add(_edit_post_key(operation, edit_token), True, EDIT_POST_TTL)
        send_update = not already_posted
    values = fleet_values({**cleaned_data, "operation_mode": "full"})
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if current.status != Status.SCHEDULED:
            raise ValueError(NOT_SCHEDULED_MESSAGE)
        old = _snapshot(current)
        if values["doctrine_source"] == current.doctrine_source and (
            values["doctrine_external_id"] == current.doctrine_external_id
        ):
            # The same doctrine keeps the fits it was announced with until the fleet starts.
            values["doctrine_fits"] = current.doctrine_fits
        for name, value in values.items():
            setattr(current, name, value)
        current.save()
        new = _snapshot(current)
        changed = [name for name in EDITABLE_FIELDS if old[name] != new[name]]
        audit(
            actor,
            "fleet.schedule_edit",
            current,
            {name: _audit_value(old[name]) for name in changed},
            {**{name: _audit_value(new[name]) for name in changed}, "send_update": send_update},
        )
    result = None
    if _render_pre_ping(current, actor=actor):
        if send_update:
            result = _send_pre_ping(current)
    elif send_update:
        result = set_action(current, "pre_ping", False, NOT_RENDERED)
    if edit_token and result is not None and result.status != OperationAction.Status.SUCCESS:
        # Nothing was posted, so the same page may try again.
        cache.delete(_edit_post_key(operation, edit_token))
    current.already_posted = already_posted
    return current


def start_scheduled_fleet(operation, *, user, character_id, send_ping: bool) -> FleetOperation:
    """Start a scheduled fleet with the ESI fleet ``character_id`` is boss of.

    Only the FC who scheduled the fleet may start it. A second click returns the record as it is
    now; an ESI fleet that the same FC character already tracks is returned with
    ``already_tracked`` and the scheduled record stays as it is.
    """
    if operation.status != Status.SCHEDULED:
        return operation
    if operation.fc_user_id != user.pk:
        raise PermissionError(ONLY_SCHEDULING_FC_MESSAGE)
    character = owned_fleet_character(user, character_id)
    detection = detect_fleet_boss(user, character)
    with transaction.atomic():
        # Same lock order as start_fleet: the settings row, then the fleet records.
        lock_fleet_starts()
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if current.status != Status.SCHEDULED:
            return current
        cancel_abandoned_starts(actor=user, esi_fleet_id=detection.fleet_id)
        now = timezone.now()
        running, handed_over = claim_esi_fleet(detection, character, now=now)
        if running:
            running.already_tracked = True
            return running
        for name, value in fc_values(user, character).items():
            setattr(current, name, value)
        current.status = Status.STARTING
        current.fleet_boss_character_id = detection.fleet_boss_id
        current.esi_fleet_id = detection.fleet_id
        current.started_at = now
        current.tracking_enabled = True
        current.doctrine_fits = doctrine_fits_snapshot(current.doctrine_source, current.doctrine_external_id)
        current.save()
        record_start(current, user, character=character, from_schedule=True, handed_over=handed_over)

    finish_handovers(handed_over)
    run_start_steps(
        current, user=user, character_id=character.character_id, full_mode=current.send_ping, send_ping=send_ping
    )
    record_start_steps(current)
    current.handed_over = [record for record, _old_status in handed_over]
    return current


def record_start_steps(operation):
    """Add the result of each start step to the start's one ``fleet.start`` audit entry.

    The entry is written when the start claims the ESI fleet, before the steps run. A start that
    is still running (a resubmitted form returns it) and an entry that already lists its steps
    are left as they are, so later retries never rewrite what the start did.
    """
    if operation.status == Status.STARTING:
        return
    entry = (
        AuditLog.objects.filter(action="fleet.start", object_type="FleetOperation", object_id=str(operation.pk))
        .order_by("-pk")
        .first()
    )
    if entry is None or not isinstance(entry.new_value, dict) or "steps" in entry.new_value:
        return
    results = dict(operation.actions.filter(action__in=START_STEPS).values_list("action", "status"))
    entry.new_value = {
        **entry.new_value,
        "steps": {step: results[step] for step in START_STEPS if step in results},
    }
    entry.save(update_fields=["new_value"])


def cancel_scheduled_fleet(operation, *, actor, notify: bool) -> FleetOperation:
    """Cancel a scheduled fleet; ``notify`` posts a notice without mentions to its ping channel.

    A second click finds the fleet cancelled and does nothing; the result's ``cancelled`` says
    whether this call cancelled it.
    """
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if current.status != Status.SCHEDULED:
            current.cancelled = False
            return current
        current.status = Status.CANCELLED
        current.tracking_enabled = False
        current.save(update_fields=["status", "tracking_enabled", "updated_at"])
        set_action(current, "fleet_cancel", True, texts.FLEET_CANCELLED())
        audit(
            actor,
            "fleet.schedule_cancel",
            current,
            {"status": Status.SCHEDULED},
            {"status": current.status, "notify": notify},
        )
    if notify:
        try:
            send_channel_notice(current, "cancel_notice", render_cancellation_notice(current, actor=actor))
        except Exception as exc:
            set_action(current, "cancel_notice", False, report_failure(logger, "Cancellation notice", exc, current))
    current.cancelled = True
    return current
