# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Whether the victim of a loss was in the fleet at the time: a warning for the reviewer, never a block.

Fleet tracking polls ESI, so the events it wrote are a few seconds to a minute late. A loss that falls just
outside the time in fleet by that margin (the polling interval plus a minute) is ``near`` and not an outright
miss. Fleets without tracking (manual records, PAP-link fleets) are checked against PAP registrations and
granted PAP instead.
"""

from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

from fleetops.models import (
    AttendanceRecord,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    PapRegistration,
    SrpFleetLink,
)
from fleetops.services.tracking import stored_join_time
from fleetops.srp import display
from fleetops.srp.types import PresenceResult

EventType = FleetMemberEvent.EventType

# The most fleets a loss is checked against when looking for the fleet it happened in.
MAX_CANDIDATE_FLEETS = 3
MIN_POLL_SECONDS = 60
EXTRA_GRACE_SECONDS = 60


def poll_interval() -> int:
    """Seconds between two tracking polls."""
    return FleetOpsSettings.get_solo().tracking_interval or MIN_POLL_SECONDS


def grace_seconds(interval: int | None = None) -> int:
    """How far from the tracked time in fleet a loss may be and still count as near."""
    interval = poll_interval() if interval is None else interval
    return max(interval, MIN_POLL_SECONDS) + EXTRA_GRACE_SECONDS


def _result(code, params=None, ship=(None, "")) -> PresenceResult:
    return PresenceResult(
        code=code,
        level=display.presence_level(code),
        params=params or {},
        tracked_ship_type_id=ship[0],
        tracked_ship_name=ship[1],
    )


def _fleet_end(operation, now):
    return operation.ended_at or now


def _intervals(events, state, operation, now):
    """The stretches ``[start, end]`` the character was in the fleet by its events.

    A join starts at the ESI join time the event stored (else at the event); a leave ends the stretch at the
    event. A stretch still open ends with the fleet, or now. Without a join event (pruned) the member state
    gives the stretch.
    """
    stretches = []
    start = None
    for event_type, created_at, new_value in events:
        if event_type in (EventType.JOIN, EventType.REJOIN):
            if start is None:
                joined = stored_join_time(new_value)
                start = min(joined, created_at) if joined else created_at
        elif event_type == EventType.LEAVE and start is not None:
            stretches.append((start, created_at))
            start = None
    if start is not None:
        stretches.append((start, _fleet_end(operation, now)))
    if not stretches and state is not None:
        stretches.append((state.first_seen, state.left_at or _fleet_end(operation, now)))
    return stretches


def _verdict(stretches, at, grace, interval):
    """``in_fleet``, ``near`` (with its parameters) or ``not_in_fleet`` for a loss at ``at``."""
    nearest = None
    for start, end in stretches:
        if start <= at <= end:
            return display.IN_FLEET, {}
        if at < start:
            candidate = ((start - at).total_seconds(), "join")
        else:
            candidate = ((at - end).total_seconds(), "leave")
        if nearest is None or candidate[0] < nearest[0]:
            nearest = candidate
    if nearest is not None and nearest[0] <= grace:
        return display.NEAR, {"seconds": int(nearest[0]), "direction": nearest[1], "interval": interval}
    return display.NOT_IN_FLEET, {}


def _ship_before(ship_events, state, at):
    """``(type id or None, name)`` of the ship the character flew at ``at`` by the ship changes tracked.

    Ship change events store names (or the type ID when the name was unknown): the new value of the last
    change before ``at``, else the old value of the first change after it, else the ship of the state.
    """
    before = [event for event in ship_events if event[0] <= at]
    after = [event for event in ship_events if event[0] > at]
    if before:
        value = before[-1][2]
    elif after:
        value = after[0][1]
    elif state is not None:
        return state.ship_type_id, state.ship_type_name or ""
    else:
        return None, ""
    value = (value or "").strip()
    if value.isdigit():
        return int(value), ""
    return None, value


def _with_ship(result, ship, ship_type_id, ship_name):
    """``result`` with the tracked ship when it differs from the victim's ship (always when the victim's
    ship is not given)."""
    tracked_id, tracked_name = ship
    if not tracked_id and not tracked_name:
        return result
    if ship_type_id:
        if tracked_id:
            differs = tracked_id != ship_type_id
        else:
            differs = bool(ship_name) and tracked_name.casefold() != ship_name.casefold()
        if not differs:
            return result
    return _result(result.code, result.params, (tracked_id, tracked_name))


def _registered(operation, character_id, at, grace, now):
    """``registered`` or ``outside_window`` when the character has PAP evidence in this fleet, else None."""
    evidence = (
        PapRegistration.objects.filter(operation=operation, character_id=character_id).exists()
        or AttendanceRecord.objects.filter(operation=operation, character_id=character_id, granted=True).exists()
    )
    if not evidence:
        return None
    margin = timedelta(seconds=grace)
    start = operation.started_at or operation.created_at
    end = _fleet_end(operation, now)
    if start - margin <= at <= end + margin:
        return _result(display.REGISTERED)
    return _result(display.OUTSIDE_WINDOW)


def check_presence(
    operation: FleetOperation, character_id: int, at: datetime, *, ship_type_id=None, ship_name: str = ""
) -> PresenceResult:
    """Look the character up in the tracking events and member states of the fleet, or in the PAP
    registrations of a fleet without tracking. Codes: in_fleet, near, not_in_fleet, never_seen, registered,
    outside_window (not_applicable without a fleet).

    ``ship_type_id`` (and ``ship_name``) is the ship that was lost: when the ship the character was tracked
    in before the loss differs, the result carries it as ``tracked_ship_type_id`` and ``tracked_ship_name``.
    """
    if operation is None or not character_id:
        return _result(display.NOT_APPLICABLE)
    now = timezone.now()
    interval = poll_interval()
    grace = grace_seconds(interval)
    if operation.is_manual or operation.link_only:
        found = _registered(operation, character_id, at, grace, now)
        return found or _result(display.NEVER_SEEN)

    rows = list(
        FleetMemberEvent.objects.filter(
            operation=operation,
            character_id=character_id,
            event_type__in=(EventType.JOIN, EventType.REJOIN, EventType.LEAVE, EventType.SHIP_CHANGE),
        )
        .order_by("created_at", "pk")
        .values_list("event_type", "created_at", "new_value", "old_value")
    )
    state = FleetMemberState.objects.filter(operation=operation, character_id=character_id).first()
    events = [(event_type, created_at, new_value) for event_type, created_at, new_value, _old in rows]
    stretches = _intervals(events, state, operation, now)
    if not stretches:
        found = _registered(operation, character_id, at, grace, now)
        return found or _result(display.NEVER_SEEN)

    code, params = _verdict(stretches, at, grace, interval)
    result = _result(code, params)
    if code in (display.IN_FLEET, display.NEAR):
        ship_events = [
            (created_at, old_value, new_value)
            for event_type, created_at, new_value, old_value in rows
            if event_type == EventType.SHIP_CHANGE
        ]
        result = _with_ship(result, _ship_before(ship_events, state, at), ship_type_id, ship_name)
    return result


def find_fleet_for_loss(character_id: int, at: datetime) -> SrpFleetLink | None:
    """An open fleet SRP link whose fleet contained the character at ``at`` (in_fleet or near).

    One query on the member states of fleets that ran around ``at`` and have an open link; the fleets found
    (at most three, the latest first) are then checked against the tracking events. A fleet where the
    character was in the fleet beats one where it was only near.
    """
    margin = timedelta(seconds=grace_seconds())
    running_then = Q(operation__ended_at__isnull=True) | Q(operation__ended_at__gte=at - margin)
    states = (
        FleetMemberState.objects.filter(
            running_then,
            character_id=character_id,
            operation__srp_link__is_open=True,
            operation__started_at__lte=at + margin,
        )
        .select_related("operation__srp_link", "operation__fleet_type")
        .order_by("-operation__started_at")[:MAX_CANDIDATE_FLEETS]
    )
    near = None
    for state in states:
        result = check_presence(state.operation, character_id, at)
        if result.code == display.IN_FLEET:
            return state.operation.srp_link
        if result.code == display.NEAR and near is None:
            near = state.operation.srp_link
    return near
