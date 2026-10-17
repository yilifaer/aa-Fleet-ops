# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The PAP engine: who gets PAP in a fleet, from time in fleet and PAP link registrations.

``compute_pap`` only reads and works on fleets in any state; ``finalize_pap`` writes the
PAP rows of a closed fleet. ``live_pap_status`` tells a pilot how their running fleets look.

Each tracked or registered character of a fleet has one managed row (source automatic or PAP
link); characters below the minimum time or over the per-player limit keep theirs ungranted, so
the fleet page can say why. Manual and imported rows are corrections made by people: their
value, and whether they replace or add to a managed row, is applied every time the PAP is
calculated, so deleting a correction and recalculating restores the tracked PAP.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime

from allianceauth.authentication.models import CharacterOwnership
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from fleetops.models import (
    AttendanceRecord,
    FleetMemberState,
    FleetOperation,
    PapRegistration,
    ReviewStatus,
)
from fleetops.services.attendance import manual_row_granted
from fleetops.services.identity import CharacterIdentity, resolve_many

Source = AttendanceRecord.Source
Correction = AttendanceRecord.Correction
# Fleets whose members can follow their PAP live.
LIVE_STATUSES = (FleetOperation.Status.ACTIVE, FleetOperation.Status.ENDING)
OUTCOME_GRANTED = "granted"
OUTCOME_CAPPED = "capped"
OUTCOME_BELOW_MINIMUM = "below_minimum"
# Row fields the engine writes.
_MANAGED_FIELDS = [
    "character_name", "main_character_id", "main_character_name", "auth_user", "corporation_id",
    "corporation_name", "source", "attendance_value", "granted", "capped", "below_minimum", "replaced",
    "extra_value", "tracked_seconds", "first_seen", "last_seen",
]
# The fields as the rows hold them (the foreign key as its id); a row whose values did not change keeps its
# ``updated_at``.
_MANAGED_VALUES = [("auth_user_id" if name == "auth_user" else name) for name in _MANAGED_FIELDS]


@dataclass
class PapCandidate:
    character_id: int
    character_name: str
    identity: CharacterIdentity
    state: FleetMemberState | None
    registration: PapRegistration | None
    tracked_seconds: int
    # state.first_seen or registration.registered_at
    first_time: datetime | None
    # Reached the minimum time, or registered through a PAP link.
    qualifies: bool
    # Qualifies and is within the per-player character limit.
    selected: bool = False
    capped: bool = False

    @property
    def outcome(self):
        if self.selected:
            return OUTCOME_GRANTED
        return OUTCOME_CAPPED if self.capped else OUTCOME_BELOW_MINIMUM


@dataclass
class PapPlan:
    operation: FleetOperation
    minimum_seconds: int
    limit: int | None
    # Characters with an Alliance Auth user.
    candidates: list[PapCandidate]
    # (character_id, name) of characters Alliance Auth does not know.
    unregistered: list[tuple[int, str]]


@dataclass
class LivePapRow:
    """One of a pilot's characters in a running fleet, as the member pages show it."""

    operation: FleetOperation
    character_id: int
    character_name: str
    fc_name: str
    fleet_type: str
    tracked_seconds: int
    minimum_seconds: int
    reached: bool
    in_fleet: bool
    via_link: bool
    # "granted", "capped" or "below_minimum"
    outcome: str
    # The character that gets the PAP instead, when capped.
    capped_by: str = ""
    seconds_to_go: int = 0
    as_of: datetime | None = None
    can_open: bool = False

    @property
    def ending(self):
        return self.operation.status == FleetOperation.Status.ENDING


def minimum_seconds(operation):
    """The PAP minimum time of the fleet in seconds; 0 means any presence counts."""
    return int(operation.pap_minimum_minutes or 0) * 60


def _priority(operation, candidate):
    # Over the limit, the FC character keeps its PAP first, then the main, then the character with
    # the longest time in fleet; ties go to the one seen first.
    first = candidate.first_time
    return (
        candidate.character_id != operation.fc_character_id,
        candidate.character_id != candidate.identity.main_character_id,
        -candidate.tracked_seconds,
        first is None,
        first.timestamp() if first else 0,
        candidate.character_id,
    )


def _select(operation, candidates, limit):
    """Mark the qualifying characters of each player as selected, up to ``limit`` per player."""
    by_user = defaultdict(list)
    for candidate in candidates:
        if candidate.qualifies:
            by_user[candidate.identity.user.pk].append(candidate)
    for group in by_user.values():
        group.sort(key=lambda candidate: _priority(operation, candidate))
        for index, candidate in enumerate(group):
            if limit is None or index < limit:
                candidate.selected = True
            else:
                candidate.capped = True


def _stored_owner(identity, row):
    """``identity`` with the player its PAP row was credited to, whoever owns the character now."""
    return replace(
        identity,
        user=row.auth_user,
        main_character_id=row.main_character_id,
        main_character_name=row.main_character_name,
        corporation_id=row.corporation_id,
        corporation_name=row.corporation_name,
    )


def compute_pap(operation, *, only_user=None, owned_rows=None) -> PapPlan:
    """Who would get PAP if the fleet closed now; ``only_user`` limits the plan to one player.

    Reads the database only (identities come from Alliance Auth and FleetOps' caches), so it can
    run inside the transaction that closes a fleet. ``owned_rows`` maps characters to the PAP rows
    a closed fleet already credited to a player: those characters stay with that player, also when
    they changed or lost their owner since.
    """
    states = {state.character_id: state for state in operation.member_states.all()}
    registrations = {item.character_id: item for item in operation.pap_registrations.all()}
    identities = resolve_many([*states, *registrations])
    for character_id, row in (owned_rows or {}).items():
        if character_id in identities:
            identities[character_id] = _stored_owner(identities[character_id], row)
    minimum = minimum_seconds(operation)
    limit = operation.pap_character_limit
    candidates, unregistered = [], []
    for character_id in sorted(identities):
        identity = identities[character_id]
        state = states.get(character_id)
        registration = registrations.get(character_id)
        stored_name = (state.character_name if state else "") or (registration.character_name if registration else "")
        name = (stored_name or identity.character_name) if identity.external else identity.character_name
        if identity.user is None:
            unregistered.append((character_id, name))
            continue
        if only_user is not None and identity.user.pk != only_user.pk:
            continue
        tracked = int(state.accumulated_seconds or 0) if state else 0
        candidates.append(
            PapCandidate(
                character_id=character_id,
                character_name=name,
                identity=identity,
                state=state,
                registration=registration,
                tracked_seconds=tracked,
                first_time=state.first_seen if state else registration.registered_at,
                # The time threshold is reached at exactly the minimum; a PAP link needs no time.
                qualifies=registration is not None or (state is not None and (minimum == 0 or tracked >= minimum)),
            )
        )
    _select(operation, candidates, limit)
    return PapPlan(
        operation=operation,
        minimum_seconds=minimum,
        limit=limit,
        candidates=candidates,
        unregistered=sorted(unregistered, key=lambda item: (item[1].casefold(), item[0])),
    )


def _managed_state(row):
    """What the engine writes to a managed row, to tell whether a recalculation changed it."""
    return tuple(getattr(row, name) for name in _MANAGED_VALUES)


def _seen_state(row):
    """The state of a managed row as far as anyone is credited by it.

    The value of a row that is not granted only follows the fleet's multiplier and counts for nothing, so a
    change of the multiplier does not make such a row "changed": it is written, but keeps its ``updated_at``.
    """
    state = list(_managed_state(row))
    if not row.granted:
        state[_MANAGED_VALUES.index("attendance_value")] = None
    return tuple(state)


def _write_identity(row, candidate):
    identity = candidate.identity
    row.main_character_id = identity.main_character_id
    row.main_character_name = identity.main_character_name
    row.auth_user = identity.user
    # Statistics are credited to the main's corporation, whatever corporation the character is in.
    row.corporation_id = identity.corporation_id
    row.corporation_name = identity.corporation_name


def _owned_rows(managed):
    """The managed rows that were credited to a player, by character."""
    owned = {}
    for row in sorted(managed, key=lambda item: (item.source != Source.AUTOMATIC, item.pk)):
        if row.auth_user_id is not None:
            owned.setdefault(row.character_id, row)
    return owned


def _sync_managed_rows(operation, plan, managed):
    """Give every candidate one managed row and drop rows of characters that are no longer candidates.

    Returns ``(rows, created)``. Identity fields are written when a row is created (or never had a
    player), so a recalculation never moves history to the character's new owner or corporation.
    A character leaves the candidates only when it is no longer tracked or registered in the fleet:
    ``finalize_pap`` keeps characters with a credited row as candidates of that player.
    """
    by_character = {}
    stale = []
    for row in sorted(managed, key=lambda item: (item.source != Source.AUTOMATIC, item.pk)):
        if row.character_id in by_character:
            stale.append(row.pk)
        else:
            by_character[row.character_id] = row
    rows, created = [], []
    for candidate in plan.candidates:
        row = by_character.pop(candidate.character_id, None)
        if row is None:
            row = AttendanceRecord(operation=operation, character_id=candidate.character_id, attendance_value=1)
            _write_identity(row, candidate)
            created.append(row)
        elif row.auth_user_id is None:
            _write_identity(row, candidate)
        state, registration = candidate.state, candidate.registration
        row.character_name = candidate.character_name
        row.source = Source.AUTOMATIC if state is not None else Source.CLICK
        row.capped = candidate.capped
        row.below_minimum = not candidate.qualifies
        row.tracked_seconds = candidate.tracked_seconds
        row.first_seen = state.first_seen if state else registration.registered_at
        row.last_seen = state.last_seen if state else registration.registered_at
        rows.append(row)
    stale.extend(row.pk for row in by_character.values())
    if stale:
        AttendanceRecord.objects.filter(pk__in=stale).delete()
    return rows, created


def _apply_corrections(operation, managed, user_rows):
    """Derive grants and values of all rows from the selection and the approved corrections."""
    merged = defaultdict(int)
    replacing = set()
    for row in user_rows:
        if row.review_status != ReviewStatus.APPROVED:
            continue
        if row.correction == Correction.MERGE:
            merged[row.character_id] += int(row.attendance_value or 0)
        elif row.correction == Correction.REPLACE:
            replacing.add(row.character_id)
    multiplier = int(operation.attendance_multiplier or 1)
    for row in managed:
        selected = not row.capped and not row.below_minimum
        # A merge forces the grant and adds its value, also over the limit and over a replacement,
        # as merges always did; a replacement takes the place of the tracked PAP. A merge made
        # before the upgrade (the legacy extra) still forces the grant, but a later replacement wins.
        forced = row.character_id in merged
        legacy_forced = int(row.legacy_extra_value or 0) > 0
        row.extra_value = int(row.legacy_extra_value or 0) + merged.get(row.character_id, 0)
        row.replaced = row.character_id in replacing and not forced
        row.granted = forced or ((selected or legacy_forced) and not row.replaced)
        row.attendance_value = multiplier + (row.extra_value if row.granted else 0)
    managed_characters = {row.character_id for row in managed}
    for row in user_rows:
        row.granted = manual_row_granted(row.review_status, row.correction, row.character_id in managed_characters)


def _summary(managed, user_rows, unregistered):
    return {
        "granted": sum(1 for row in managed if row.granted),
        "capped": sum(1 for row in managed if row.capped and not row.granted),
        "below_minimum": sum(1 for row in managed if row.below_minimum and not row.granted),
        "replaced": sum(1 for row in managed if row.replaced),
        "unregistered": unregistered,
        "manual": sum(1 for row in user_rows if row.granted),
    }


def finalize_pap(operation, *, actor=None, reason="") -> dict:
    """Write the PAP rows of a closed fleet; returns how many rows ended up in each state.

    Runs again after every change that affects PAP and gives the same result for the same data.
    Fleets closed before PAP followed time in fleet (``pap_rules`` legacy) keep the selection
    they were closed with; their values and corrections are still recalculated. ``actor`` and
    ``reason`` describe the change for the caller's audit entry.
    """
    with transaction.atomic():
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        if current.status != FleetOperation.Status.CLOSED:
            raise ValueError("PAP is finalized when the fleet closes.")
        operation.attendance_multiplier = current.attendance_multiplier
        rows = list(AttendanceRecord.objects.filter(operation=current).select_related("auth_user"))
        managed = [row for row in rows if row.source in AttendanceRecord.MANAGED_SOURCES]
        user_rows = [row for row in rows if row.source in AttendanceRecord.USER_SOURCES]
        # What the rows hold now: only a row whose values change is written, and only then does its
        # ``updated_at`` move, so it says when the row last really changed.
        managed_before = {row.pk: (_managed_state(row), _seen_state(row)) for row in managed}
        granted_before = {row.pk: row.granted for row in user_rows}
        created = []
        unregistered = 0
        if current.pap_rules == FleetOperation.PapRules.TIME_IN_FLEET:
            # Characters keep the player their PAP was credited to; only characters without a
            # credited row (such as an alt added to Alliance Auth since) follow today's owner.
            plan = compute_pap(current, owned_rows=_owned_rows(managed))
            managed, created = _sync_managed_rows(current, plan, managed)
            unregistered = len(plan.unregistered)
        _apply_corrections(current, managed, user_rows)
        new_rows = {id(row) for row in created}
        if created:
            AttendanceRecord.objects.bulk_create(created, batch_size=500)
        now = timezone.now()
        existing = [row for row in managed if id(row) not in new_rows]
        changed = [row for row in existing if _managed_state(row) != managed_before[row.pk][0]]
        # Rows whose credit changed get a new ``updated_at``; the others are only brought up to date.
        moved = [row for row in changed if _seen_state(row) != managed_before[row.pk][1]]
        changed_user_rows = [row for row in user_rows if row.granted != granted_before[row.pk]]
        for row in (*moved, *changed_user_rows):
            row.updated_at = now
        moved_ids = {id(row) for row in moved}
        if moved:
            AttendanceRecord.objects.bulk_update(moved, [*_MANAGED_FIELDS, "updated_at"], batch_size=500)
        quiet = [row for row in changed if id(row) not in moved_ids]
        if quiet:
            AttendanceRecord.objects.bulk_update(quiet, _MANAGED_FIELDS, batch_size=500)
        if changed_user_rows:
            AttendanceRecord.objects.bulk_update(changed_user_rows, ["granted", "updated_at"], batch_size=500)
    return _summary(managed, user_rows, unregistered)


def refresh_after_change(operation, *, actor=None, reason=""):
    """Recalculate the PAP of a closed fleet after a change; open fleets get theirs when they close."""
    if operation.status != FleetOperation.Status.CLOSED:
        return None
    return finalize_pap(operation, actor=actor, reason=reason)


def live_pap_status(user) -> list[LivePapRow]:
    """The user's characters in running fleets, with the PAP each would get."""
    owned = list(CharacterOwnership.objects.filter(user=user).values_list("character__character_id", flat=True))
    if not owned:
        return []
    operations = (
        FleetOperation.objects.filter(status__in=LIVE_STATUSES)
        .filter(Q(member_states__character_id__in=owned) | Q(pap_registrations__character_id__in=owned))
        .select_related("fleet_type")
        .distinct()
        .order_by("-started_at", "pk")
    )
    rows = []
    for operation in operations:
        plan = compute_pap(operation, only_user=user)
        winners = [candidate.character_name for candidate in plan.candidates if candidate.selected]
        for candidate in sorted(plan.candidates, key=lambda item: (item.character_name.casefold(), item.character_id)):
            outcome = candidate.outcome
            reached = plan.minimum_seconds == 0 or candidate.tracked_seconds >= plan.minimum_seconds
            # Whole minutes, rounded up: a pilot 30 s short still has 1 min to go.
            to_go = 0 if reached else -(-(plan.minimum_seconds - candidate.tracked_seconds) // 60) * 60
            rows.append(
                LivePapRow(
                    operation=operation,
                    character_id=candidate.character_id,
                    character_name=candidate.character_name,
                    fc_name=operation.fc_character_name,
                    fleet_type=str(operation.fleet_type),
                    tracked_seconds=candidate.tracked_seconds,
                    minimum_seconds=plan.minimum_seconds,
                    reached=reached,
                    in_fleet=bool(candidate.state and candidate.state.is_active),
                    via_link=candidate.registration is not None,
                    outcome=outcome,
                    capped_by=", ".join(winners) if outcome == OUTCOME_CAPPED else "",
                    seconds_to_go=to_go,
                    as_of=operation.last_esi_update,
                )
            )
    return rows
