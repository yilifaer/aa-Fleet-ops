# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Regenerate the MOTD of a running fleet from the current templates and push it to EVE."""

from allianceauth.services.hooks import get_extension_logger
from django.db import transaction

from fleetops import texts
from fleetops.models import FleetOperation
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE, doctrine_fits_snapshot
from fleetops.services.audit import audit
from fleetops.services.errors import report_failure
from fleetops.services.operations import (
    NOT_RENDERED,
    _keep_skipped,
    _skipped_message,
    _update_motd,
    set_action,
)
from fleetops.services.messages import render_messages

logger = get_extension_logger(__name__)

# Stored message (English); the page shows it through ``translate_stored``.
NOT_ACTIVE = texts.MOTD_NOT_ACTIVE()


def _refresh_doctrine_fits(operation) -> bool:
    """Take a new snapshot of the doctrine's fits; returns True when it changed.

    A doctrine that cannot be read (Fittings removed, doctrine deleted) keeps its last snapshot.
    """
    if operation.doctrine_source != FITTINGS_DOCTRINE_SOURCE:
        return False
    fits = doctrine_fits_snapshot(operation.doctrine_source, operation.doctrine_external_id)
    if not fits or fits == operation.doctrine_fits:
        return False
    operation.doctrine_fits = fits
    return True


def regenerate_and_push_motd(operation, *, actor):
    """Render the MOTD again (current templates, comms, channels and doctrine) and push it to the fleet.

    PAP-only and manual fleets never send messages; their step stays skipped. Raises
    ValueError when the fleet is not active. Returns the ``motd_update`` step.
    """
    if operation.is_manual or not operation.send_ping:
        return _keep_skipped(operation, "motd_update", _skipped_message(operation, "MOTD update"))
    operation.refresh_from_db()
    if operation.status != FleetOperation.Status.ACTIVE:
        raise ValueError(NOT_ACTIVE)

    old_motd = operation.motd_text
    try:
        with transaction.atomic():
            fields = ["motd_text", "updated_at"]
            if _refresh_doctrine_fits(operation):
                fields.append("doctrine_fits")
            rendered = render_messages(operation, actor=actor)
            operation.motd_text = rendered.motd
            operation.save(update_fields=fields)
    except Exception as exc:
        set_action(operation, "message_render", False, report_failure(logger, "Message rendering", exc, operation))
        return set_action(operation, "motd_update", False, NOT_RENDERED)
    if rendered.errors:
        set_action(operation, "message_render", False, " ".join(rendered.errors))
    elif operation.actions.filter(action="message_render").exists():
        set_action(operation, "message_render", True)

    action = _update_motd(operation, operation.fc_user, operation.fc_character_id)
    audit(
        actor,
        "fleet.motd_regenerate",
        operation,
        None,
        {"old_length": len(old_motd), "new_length": len(operation.motd_text), "status": action.status},
        reason=action.error_message,
    )
    return action
