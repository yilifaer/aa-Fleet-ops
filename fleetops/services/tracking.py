# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from datetime import datetime

from allianceauth.services.hooks import get_extension_logger
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext, ngettext

from fleetops import texts
from fleetops.models import FleetMemberEvent, FleetMemberState, FleetOperation
from fleetops.providers.esi import FleetESIError, detect_character_fleet, get_fleet_members, get_fleet_wings
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.identity import character_label, resolve_many
from fleetops.services.names import (
    is_placeholder,
    queue_name_resolution,
    stale_character_ids,
    unnamed_character_ids,
)
from fleetops.services.periods import to_utc
from fleetops.services.sde import item_type_names, solar_system_names

logger = get_extension_logger(__name__)

EndReason = FleetOperation.EndReason
# How JOIN and REJOIN events store the ESI join time.
JOIN_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_join_time(value):
    """The ESI ``join_time`` of a member as an aware UTC datetime, or None when missing or unreadable."""
    if isinstance(value, str):
        try:
            value = parse_datetime(value.strip())
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return to_utc(value)


def join_time_value(join_time):
    """``join_time`` as stored in the new value of JOIN and REJOIN events (ISO 8601, UTC)."""
    return join_time.strftime(JOIN_TIME_FORMAT) if join_time else ""


def stored_join_time(value):
    """The join time a JOIN or REJOIN event stored, or None."""
    if not value:
        return None
    try:
        return to_utc(datetime.strptime(value, JOIN_TIME_FORMAT))
    except (TypeError, ValueError):
        return None


def _segment_start(state, join_time, lower, now):
    """Since when an incoming member has been in fleet without being counted yet.

    A member who was in fleet at the previous poll is counted from that poll, unless ESI says it
    joined again since (left and came back between two polls). A new or returning member is
    counted from its join time, but never from before the fleet started or the previous poll.
    """
    if state is not None and state.is_active:
        if join_time and join_time > state.last_seen:
            return min(join_time, now)
        return state.last_seen
    start = join_time or now
    if lower is not None:
        start = max(start, lower)
    return min(start, now)


def _event(operation, state, event_type, old_value="", new_value=""):
    FleetMemberEvent.objects.create(
        operation=operation,
        character_id=state.character_id,
        character_name=state.character_name,
        event_type=event_type,
        old_value=str(old_value or ""),
        new_value=str(new_value or ""),
    )


def _keep_known_details(values, state):
    """Keep the name and corporation a member without Alliance Auth account already has.

    The ESI name cache may not know the member (yet), and a placeholder never replaces a real name.
    """
    if is_placeholder(values["character_name"], state.character_id) and not is_placeholder(
        state.character_name, state.character_id
    ):
        values["character_name"] = state.character_name
    if values["corporation_id"] is None and state.corporation_id:
        for key in ("corporation_id", "corporation_name", "alliance_id", "alliance_name"):
            values[key] = getattr(state, key)


def _read_wings(operation, fresh):
    """The fleet's wings and squads from ESI, or None when they could not be read.

    A failed wings read never fails the member sync: the last snapshot is kept.
    """
    try:
        return get_fleet_wings(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id, fresh=fresh)
    except FleetESIError as exc:
        logger.info("FleetOps: wings of operation %s not read (%s).", operation.pk, exc.code)
    except Exception as exc:
        report_failure(logger, "Reading the fleet wings", exc, operation)
    return None


def sync_operation(operation, fresh=False, until=None):
    """Read the fleet members from ESI and record joins, leaves, changes and time in fleet.

    The wings and squads are read too and kept as ``operation.fleet_wings``; position events keep
    the raw wing and squad ids (names are shown from that snapshot).
    ``fresh`` skips django-esi's response cache, so a sync right after a kick sees the change.
    Returns the number of members, or None when the fleet was closed or cancelled while
    ESI was being read; nothing is written then. A read older than the one already recorded
    (another sync read ESI later and finished first) writes nothing either and returns the
    recorded member count. PAP is granted when the fleet closes.

    Time in fleet is approximated from the polls: a member present at two successive polls is
    counted for the whole time between them, also when an ESI outage made that gap long.
    ``until`` (the end request of a fleet being closed) ends the time in fleet: nothing after it
    is counted, and members who joined after it are not added. A fleet that is Ending always
    counts up to its end request, whoever reads it (a refresh after a fleet job, a kick batch).
    """
    if not operation.esi_fleet_id:
        raise FleetESIError("NO_FLEET_ID", texts.NO_ESI_FLEET())
    now = timezone.now()
    members = get_fleet_members(operation.fc_user, operation.fc_character_id, operation.esi_fleet_id, fresh=fresh)
    wings = _read_wings(operation, fresh)
    ids = [int(m.get("character_id")) for m in members if m.get("character_id")]
    identities = resolve_many(ids)
    ship_names = item_type_names({m.get("ship_type_id") for m in members if m.get("ship_type_id")})
    system_names = solar_system_names({m.get("solar_system_id") for m in members if m.get("solar_system_id")})

    incoming = {int(m["character_id"]): m for m in members if m.get("character_id")}

    with transaction.atomic():
        # The close of a fleet locks the same row, so a poll whose ESI read was still running
        # when the fleet ended cannot put its pilots back in fleet afterwards. The lock also
        # serialises the tracking poll, the final sync and the syncs after kicks, so no stretch
        # of time in fleet is counted twice.
        current = (
            FleetOperation.objects.select_for_update()
            .only(
                "status", "attendance_multiplier", "missing_reason", "last_esi_update", "started_at", "end_requested_at"
            )
            .get(pk=operation.pk)
        )
        if current.status not in FleetOperation.OPEN_STATUSES:
            return None
        if until is None and current.status == FleetOperation.Status.ENDING:
            # The FC ended the fleet while this read ran: pilots who joined after the end request
            # must not be added, nor anybody counted past it.
            until = current.end_requested_at
        operation.attendance_multiplier = current.attendance_multiplier
        previous_poll = current.last_esi_update
        if previous_poll and previous_poll > now:
            # A sync that read ESI later finished first: its member list is newer than this one,
            # which would bring back pilots that left or were kicked meanwhile.
            return operation.member_states.filter(is_active=True).count()
        late = until is not None and now > until
        if late:
            # The fleet ended at ``until``; never count back before the previous poll.
            now = max(until, previous_poll) if previous_poll else until
        bounds = [value for value in (previous_poll, current.started_at) if value]
        lower = max(bounds) if bounds else None
        existing = {s.character_id: s for s in operation.member_states.all()}

        # Missing active pilots leave. They were counted up to the previous poll.
        for cid, state in existing.items():
            if state.is_active and cid not in incoming:
                state.is_active = False
                state.left_at = now
                state.last_seen = now
                state.save(update_fields=["is_active", "left_at", "last_seen"])
                _event(operation, state, FleetMemberEvent.EventType.LEAVE)

        for cid, member in incoming.items():
            identity = identities[cid]
            state = existing.get(cid)
            join_time = parse_join_time(member.get("join_time"))
            if late and (state is None or not state.is_active) and (join_time is None or join_time > now):
                # Joined (or came back) after the fleet ended.
                continue
            seconds = max(0, int((now - _segment_start(state, join_time, lower, now)).total_seconds()))
            # The member list shows each character's own corporation; PAP and statistics use the main's.
            defaults = {
                "character_name": identity.character_name,
                "main_character_id": identity.main_character_id,
                "main_character_name": identity.main_character_name,
                "auth_user": identity.user,
                "corporation_id": identity.character_corporation_id,
                "corporation_name": identity.character_corporation_name,
                "alliance_id": identity.character_alliance_id,
                "alliance_name": identity.character_alliance_name,
                "ship_type_id": member.get("ship_type_id"),
                "ship_type_name": ship_names.get(member.get("ship_type_id"), ""),
                "solar_system_id": member.get("solar_system_id"),
                "solar_system_name": system_names.get(member.get("solar_system_id"), ""),
                "fleet_role": member.get("role") or member.get("role_name") or "",
                "wing_id": member.get("wing_id"),
                "squad_id": member.get("squad_id"),
                # Docked while set; neither is a source of member events.
                "station_id": member.get("station_id"),
                "takes_fleet_warp": member.get("takes_fleet_warp"),
                "first_seen": now,
                "last_seen": now,
                "left_at": None,
                "is_active": True,
            }
            if identity.external and state is not None:
                _keep_known_details(defaults, state)
            if state is None:
                state = FleetMemberState.objects.create(
                    operation=operation, character_id=cid, accumulated_seconds=seconds, **defaults
                )
                _event(operation, state, FleetMemberEvent.EventType.JOIN, new_value=join_time_value(join_time))
            else:
                state.accumulated_seconds += seconds
                if not state.is_active:
                    state.is_active = True
                    state.left_at = None
                    _event(
                        operation, state, FleetMemberEvent.EventType.REJOIN, new_value=join_time_value(join_time)
                    )
                comparisons = [
                    ("ship_type_id", "ship_type_name", FleetMemberEvent.EventType.SHIP_CHANGE),
                    ("solar_system_id", "solar_system_name", FleetMemberEvent.EventType.SYSTEM_CHANGE),
                    ("fleet_role", "fleet_role", FleetMemberEvent.EventType.ROLE_CHANGE),
                    ("wing_id", "wing_id", FleetMemberEvent.EventType.WING_CHANGE),
                    ("squad_id", "squad_id", FleetMemberEvent.EventType.SQUAD_CHANGE),
                ]
                for field, label_field, event_type in comparisons:
                    old = getattr(state, field)
                    new = defaults[field]
                    if old != new:
                        old_label = getattr(state, label_field) if label_field != field else old
                        new_label = defaults[label_field] if label_field != field else new
                        _event(operation, state, event_type, old_label or old, new_label or new)
                for key, value in defaults.items():
                    if key != "first_seen":
                        setattr(state, key, value)
                state.save()

        update_fields = [
            "last_esi_update", "fleet_missing_count", "missing_reason", "missing_detail", "last_error", "updated_at",
        ]
        if current.missing_reason == EndReason.BOSS_HANDOVER:
            # Only the boss can read the fleet, so the FC character holds the boss role again.
            operation.fleet_boss_character_id = operation.fc_character_id
            update_fields.append("fleet_boss_character_id")
        if wings is not None:
            # Read before the wings were: an expected change newer than this read still shows (fleet_overlay).
            operation.fleet_wings = wings
            operation.fleet_wings_read_at = now
            update_fields += ["fleet_wings", "fleet_wings_read_at"]
        operation.last_esi_update = now
        operation.fleet_missing_count = 0
        operation.missing_reason = ""
        operation.missing_detail = ""
        operation.last_error = ""
        operation.save(update_fields=update_fields)

    # The fleet was read with the FC's token, so the FC character is its boss: earlier read
    # problems and boss checks no longer apply. Imported here: fleet_refresh imports this module.
    from fleetops.services.fleet_refresh import clear_read_problem

    clear_read_problem(operation.pk)
    # Members without an Alliance Auth account are named in the background, after this commit; so are
    # members who left before a lookup could name them.
    external = [cid for cid in incoming if identities[cid].external]
    try:
        queue_name_resolution(operation.pk, stale_character_ids([*external, *unnamed_character_ids(operation.pk)]))
    except Exception as exc:
        # The poll itself succeeded; names are looked up again on a later one.
        log_failure(logger, "Queueing the name lookup", exc, operation)
    return len(incoming)


class UnreadableFleet(tuple):
    """Why a fleet could not be read; unpacks to ``(reason, detail)``.

    ``boss_id`` is the character that holds the boss role after a handover, else None.
    """

    def __new__(cls, reason, detail="", boss_id=None):
        observation = super().__new__(cls, (reason, detail))
        observation.boss_id = boss_id
        return observation

    @property
    def reason(self):
        return self[0]

    @property
    def detail(self):
        return self[1]


def observe_unreadable_fleet(operation):
    """Find out why the FC token got a 404 for the fleet.

    Only the fleet boss can read a fleet, so a 404 means either the fleet is gone (disbanded,
    or the FC left it) or the FC handed the boss role to someone else. Returns an
    ``UnreadableFleet`` (``reason, detail`` plus the new boss id of a handover), or None when the
    check itself failed and the poll proves nothing.
    """
    try:
        detection = detect_character_fleet(operation.fc_user, operation.fc_character_id, fresh=True)
    except FleetESIError as exc:
        if exc.code == "NOT_IN_FLEET":
            return UnreadableFleet(EndReason.FLEET_GONE)
        return None
    except Exception as exc:
        report_failure(logger, "Fleet boss check", exc, operation)
        return None
    if detection.fleet_id == operation.esi_fleet_id and detection.fleet_boss_id and not detection.is_boss(
        operation.fc_character_id
    ):
        boss_id = int(detection.fleet_boss_id)
        return UnreadableFleet(EndReason.BOSS_HANDOVER, character_label(boss_id), boss_id)
    return UnreadableFleet(EndReason.FLEET_GONE)


def unreadable_fleet_message(reason, detail=""):
    """Why the fleet could not be read, as the stored message kept with the fleet (English)."""
    if reason == EndReason.BOSS_HANDOVER:
        return texts.UNREADABLE_HANDOVER(name=detail) if detail else texts.UNREADABLE_HANDOVER_UNKNOWN()
    return texts.UNREADABLE_GONE()


def unreadable_fleet_warning(operation, settings):
    """Warning for an active fleet whose latest polls could not read it, or an empty string.

    The warning is worked out each time the page is shown, so it follows the language of the viewer.
    """
    if not (operation.fleet_missing_count and operation.missing_reason):
        return ""
    count = operation.fleet_missing_count
    checks = gettext("Unreadable checks in a row: %(count)s.") % {"count": count}
    handover = operation.missing_reason == EndReason.BOSS_HANDOVER
    boss = {"name": texts.shown_name(operation.missing_detail) or gettext("another character")}
    if not settings.auto_end_enabled:
        if handover:
            what = gettext(
                "Fleet boss handed over to %(name)s — automatic end is disabled, so this fleet stays open "
                "until it is ended manually."
            )
        else:
            what = gettext(
                "The fleet could not be read: it no longer exists or the FC left it — automatic end is "
                "disabled, so this fleet stays open until it is ended manually."
            )
        return f"{what % boss} {checks}"
    remaining = max(settings.auto_end_missing_count - count, 1)
    if handover:
        what = ngettext(
            "Fleet boss handed over to %(name)s — this fleet will end automatically after %(remaining)s more "
            "check unless the boss is returned.",
            "Fleet boss handed over to %(name)s — this fleet will end automatically after %(remaining)s more "
            "checks unless the boss is returned.",
            remaining,
        )
    else:
        what = ngettext(
            "The fleet could not be read: it no longer exists or the FC left it — this fleet will end "
            "automatically after %(remaining)s more check unless it can be read again.",
            "The fleet could not be read: it no longer exists or the FC left it — this fleet will end "
            "automatically after %(remaining)s more checks unless it can be read again.",
            remaining,
        )
    return f"{what % {**boss, 'remaining': remaining}} {checks}"
