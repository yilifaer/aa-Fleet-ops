# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Manual PAP from a fleet roster pasted out of the game client.

The text is read by ``roster_paste``; this module looks the names up, previews what an
import would do and creates the PAP rows.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext, ngettext

from fleetops import texts
from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError
from fleetops.services import attendance, pap
from fleetops.services.audit import audit
from fleetops.services.roster_paste import RosterLine, clean_name, name_error, parse_roster
from fleetops.texts import translate_stored

__all__ = [
    "ImportResult",
    "PreviewRow",
    "Resolved",
    "RosterLine",
    "build_import_preview",
    "import_rows",
    "load_preview",
    "parse_roster",
    "resolve_character_name",
    "resolve_names",
    "sign_preview",
]

# Resolved.status
AUTH = "auth"
NOT_IN_AUTH = "not_in_auth"
NOT_FOUND = "not_found"
ERROR = "error"

# PreviewRow.status
ADD = "add"
CAPPED = "capped"
HAS_PAP = "has_pap"
INVALID = "invalid"
# Rows that can be ticked for the import; the others are listed only. A row that has PAP already is importable
# only as a correction (see ``PreviewRow.importable``).
IMPORTABLE = (ADD, CAPPED, HAS_PAP)
CORRECTIONS = (AttendanceRecord.Correction.REPLACE, AttendanceRecord.Correction.MERGE)

PREVIEW_SALT = "fleetops.roster"
PREVIEW_MAX_AGE = 30 * 60
# Stored messages (English). The reasons of an import go into its audit entry; the preview shows them in the
# language of the viewer.
PREVIEW_EXPIRED = texts.ROSTER_EXPIRED()

NOT_IN_AUTH_REASON = texts.ROSTER_NOT_IN_AUTH()
NOT_FOUND_REASON = texts.ROSTER_NOT_FOUND()
ESI_ERROR_REASON = texts.ROSTER_ESI_ERROR()
HAS_PAP_REASON = texts.ROSTER_HAS_PAP()
HAS_PAP_KEEP_REASON = texts.ROSTER_HAS_PAP_KEEP()
HAS_MANUAL_PAP_REASON = texts.ROSTER_HAS_MANUAL_PAP()


@dataclass
class Resolved:
    """What a pasted name turned out to be."""

    name: str
    character_id: int | None = None
    # "auth" (owned in Alliance Auth), "not_in_auth", "not_found" or "error"
    status: str = NOT_FOUND
    user_id: int | None = None
    error: str = ""
    main_character_id: int | None = None
    main_character_name: str = ""


@dataclass
class PreviewRow:
    """One pasted line in the import preview."""

    line: RosterLine
    # "add", "capped", "has_pap", "not_in_auth", "not_found", "error" or "invalid"
    status: str
    character_id: int | None = None
    character_name: str = ""
    user_id: int | None = None
    main_character_name: str = ""
    checked: bool = False
    reason: str = ""
    main_character_id: int | None = None
    # For a row that has PAP already: the PAP can be corrected (replaced or merged into) by this import.
    can_correct: bool = False

    @property
    def importable(self) -> bool:
        if self.status == HAS_PAP:
            return self.can_correct
        return self.status in IMPORTABLE


@dataclass
class ImportResult:
    created: list = field(default_factory=list)
    # {"character_id", "character_name", "reason"} of the selected rows that were not imported
    skipped: list = field(default_factory=list)


def _esi_detail(exc) -> str:
    # "Could not look up character names: ESI returned HTTP 503 (...)." -> the part after the colon
    _context, separator, detail = str(exc).partition(": ")
    return detail if separator else str(exc)


def _resolved(character, ownership, status=None) -> Resolved:
    user = ownership.user if ownership else None
    main = getattr(getattr(user, "profile", None), "main_character", None) if user else None
    return Resolved(
        name=character.character_name,
        character_id=character.character_id,
        status=status or (AUTH if user else NOT_IN_AUTH),
        user_id=user.pk if user else None,
        main_character_id=getattr(main, "character_id", None),
        main_character_name=getattr(main, "character_name", "") or "",
    )


def _ownerships(**filters):
    return {
        ownership.character.character_id: ownership
        for ownership in CharacterOwnership.objects.filter(**filters).select_related(
            "character", "user__profile__main_character"
        )
    }


def resolve_names(names: list[str]) -> dict[str, Resolved]:
    """Look names up in Alliance Auth, then in ESI; keyed by the casefolded name.

    Names that cannot be character names are left out. When ESI cannot be reached, the names
    Alliance Auth does not know get the status "error".
    """
    wanted = {}
    for name in names:
        name = clean_name(name)
        if name and not name_error(name):
            wanted.setdefault(name.casefold(), name)
    result = {}
    if not wanted:
        return result

    known = defaultdict(list)
    lowered = {name.lower() for name in wanted.values()}
    for character in EveCharacter.objects.annotate(lname=Lower("character_name")).filter(lname__in=lowered):
        known[character.character_name.casefold()].append(character)
    owned = _ownerships(character__in=[character for group in known.values() for character in group])
    for key, characters in known.items():
        if key not in wanted:
            continue
        # A stale copy of a name can exist; the owned character wins.
        character = next((c for c in characters if c.character_id in owned), characters[0])
        result[key] = _resolved(character, owned.get(character.character_id))

    remaining = [name for key, name in wanted.items() if key not in result]
    if not remaining:
        return result
    try:
        found = esi_provider.universe_ids(remaining)
    except FleetESIError as exc:
        detail = _esi_detail(exc)
        for name in remaining:
            result[name.casefold()] = Resolved(name=name, status=ERROR, error=detail)
        return result
    owned = _ownerships(character__character_id__in=[character_id for character_id, _name in found.values()])
    for name in remaining:
        key = name.casefold()
        hit = found.get(key)
        if hit is None:
            result[key] = Resolved(name=name, status=NOT_FOUND)
            continue
        character_id, esi_name = hit
        ownership = owned.get(character_id)
        if ownership is not None:
            result[key] = _resolved(ownership.character, ownership)
        else:
            result[key] = Resolved(name=esi_name, character_id=character_id, status=NOT_IN_AUTH)
    return result


def resolve_character_name(name: str) -> Resolved:
    """What one typed character name is, looked up like the names of a pasted roster."""
    name = clean_name(name)
    return resolve_names([name]).get(name.casefold()) or Resolved(name=name)


def _pap_holders(operation) -> dict[int, tuple[int | None, str, bool]]:
    """Characters that already have, or will get, PAP in the fleet: ``{character_id: (user_id, name, tracked)}``.

    Granted rows and manual rows waiting for approval count; on a fleet that is still open, tracked members and
    PAP link registrations count too. ``tracked`` says whether the PAP comes from tracking or a PAP link: only
    that PAP can be replaced or merged into. A character that has both a tracked and a manual row is tracked.
    """
    holders = {}
    rows = (
        operation.attendance_records.exclude(review_status=ReviewStatus.REJECTED)
        .filter(
            Q(granted=True)
            | Q(source__in=AttendanceRecord.USER_SOURCES, review_status=ReviewStatus.PENDING)
        )
        .values_list("character_id", "auth_user_id", "character_name", "source")
    )
    for character_id, user_id, name, source in rows:
        tracked = source in AttendanceRecord.MANAGED_SOURCES
        if character_id in holders:
            known_user, known_name, known_tracked = holders[character_id]
            holders[character_id] = (known_user, known_name, known_tracked or tracked)
        else:
            holders[character_id] = (user_id, name, tracked)
    if operation.status != FleetOperation.Status.CLOSED:
        for source in (operation.member_states, operation.pap_registrations):
            for character_id, user_id, name in source.values_list("character_id", "auth_user_id", "character_name"):
                known_user, known_name, _known_tracked = holders.get(character_id, (user_id, name, True))
                holders[character_id] = (known_user, known_name, True)
    return holders


def has_pap_reason(duplicate_action, tracked) -> str:
    """Why a character that has PAP already is not imported (or what to know when it is): a stored message."""
    if not tracked:
        return HAS_MANUAL_PAP_REASON
    if duplicate_action not in CORRECTIONS:
        return HAS_PAP_KEEP_REASON
    return HAS_PAP_REASON


def _limit_reason(limit, keeping):
    """Why a character is over the PAP limit of its player; only the preview shows this."""
    names = ", ".join(keeping)
    if keeping:
        return ngettext(
            "Over the limit of %(limit)s PAP character per player (%(names)s first)",
            "Over the limit of %(limit)s PAP characters per player (%(names)s first)",
            limit,
        ) % {"limit": limit, "names": names}
    return ngettext(
        "Over the limit of %(limit)s PAP character per player",
        "Over the limit of %(limit)s PAP characters per player",
        limit,
    ) % {"limit": limit}


def build_import_preview(operation, lines, *, actor, duplicate_action=AttendanceRecord.Correction.KEEP) -> list[PreviewRow]:
    """What importing the pasted lines into the fleet would do, line by line.

    Characters over the fleet's PAP character limit are marked "capped" (not ticked). The
    characters a player already has PAP with keep their places; among the pasted ones the FC
    character comes first, then the player's main, then the paste order. A character that has PAP
    already can only be imported to correct it: with ``duplicate_action`` replace or merge, and only
    when the PAP comes from tracking or a PAP link. Such a row is listed but not ticked.
    """
    resolved = resolve_names([line.name for line in lines if line.is_valid])
    holders = _pap_holders(operation)
    rows = []
    candidates = defaultdict(list)
    listed = set()
    for line in lines:
        if not line.is_valid:
            rows.append(PreviewRow(line, INVALID, reason=line.error))
            continue
        found = resolved.get(line.key) or Resolved(name=line.name)
        row = PreviewRow(
            line,
            found.status,
            character_id=found.character_id,
            character_name=found.name,
            user_id=found.user_id,
            main_character_id=found.main_character_id,
            main_character_name=found.main_character_name,
        )
        rows.append(row)
        if found.status == ERROR:
            row.reason = translate_stored(ESI_ERROR_REASON)
        elif found.status == NOT_FOUND:
            row.reason = translate_stored(NOT_FOUND_REASON)
        elif found.status == NOT_IN_AUTH:
            row.reason = translate_stored(NOT_IN_AUTH_REASON)
        elif found.character_id in listed:
            row.status, row.reason = INVALID, gettext("The same character is listed twice")
        elif found.character_id in holders:
            tracked = holders[found.character_id][2]
            row.status = HAS_PAP
            row.can_correct = tracked and duplicate_action in CORRECTIONS
            row.reason = translate_stored(has_pap_reason(duplicate_action, tracked))
        else:
            row.status, row.checked = ADD, True
            candidates[found.user_id].append((len(rows), row))
        if found.character_id:
            listed.add(found.character_id)

    limit = operation.pap_character_limit
    if limit is not None:
        holding = defaultdict(list)
        for user_id, name, _tracked in holders.values():
            if user_id is not None:
                holding[user_id].append(name)
        for user_id, user_rows in candidates.items():
            user_rows.sort(
                key=lambda item: (
                    item[1].character_id != operation.fc_character_id,
                    item[1].character_id != item[1].main_character_id,
                    item[0],
                )
            )
            free = max(0, limit - len(holding[user_id]))
            keeping = holding[user_id] + [row.character_name for _index, row in user_rows[:free]]
            for _index, row in user_rows[free:]:
                row.status, row.checked, row.reason = CAPPED, False, _limit_reason(limit, keeping)
    return rows


def sign_preview(operation, rows, *, actor, attendance_value, duplicate_action, notes) -> str:
    """The importable preview rows and the import settings, signed for the import step."""
    return signing.dumps(
        {
            "operation": str(operation.uuid),
            "actor": actor.pk,
            "attendance_value": attendance_value,
            "duplicate_action": duplicate_action,
            "notes": notes,
            "rows": [
                [row.character_id, row.character_name, row.status] for row in rows if row.importable
            ],
        },
        salt=PREVIEW_SALT,
        compress=True,
    )


def load_preview(operation, token, *, actor) -> dict:
    """The signed preview of ``sign_preview``; ValueError when it expired, was changed or is not this one."""
    try:
        data = signing.loads(token or "", salt=PREVIEW_SALT, max_age=PREVIEW_MAX_AGE)
    except signing.BadSignature:
        raise ValueError(PREVIEW_EXPIRED) from None
    if not isinstance(data, dict) or data.get("operation") != str(operation.uuid) or data.get("actor") != actor.pk:
        raise ValueError(PREVIEW_EXPIRED)
    data["rows"] = [
        {"character_id": int(character_id), "character_name": name, "status": status}
        for character_id, name, status in data.get("rows") or []
    ]
    return data


def _import_row(row) -> dict:
    if isinstance(row, PreviewRow):
        return {"character_id": row.character_id, "character_name": row.character_name, "status": row.status}
    return row


def import_rows(operation, rows, *, actor, attendance_value, duplicate_action, notes) -> ImportResult:
    """Create the manual PAP rows of the selected preview rows.

    Ownership and existing PAP are checked again, so a preview imported twice does not credit a
    character twice, also when both imports run at the same time. Rows of non-approvers wait for FC
    lead approval. One audit entry lists what was created and what was skipped.
    """
    rows = [_import_row(row) for row in rows]
    owned = set(
        CharacterOwnership.objects.filter(
            character__character_id__in=[row["character_id"] for row in rows]
        ).values_list("character__character_id", flat=True)
    )
    result = ImportResult()
    with transaction.atomic():
        # A second import of the fleet waits here and then sees the rows of the first one; a close
        # running now finishes first, so the recalculation below sees the closed fleet.
        current = FleetOperation.objects.select_for_update().get(pk=operation.pk)
        operation.status, operation.ended_at = current.status, current.ended_at
        if operation.status not in attendance.RECORDABLE_STATUSES:
            raise ValueError(texts.PAP_ONLY_ACTIVE_OR_CLOSED())
        holders = _pap_holders(operation)
        done = set()
        for row in rows:
            character_id = row["character_id"]
            reason = ""
            if character_id in done:
                reason = texts.ROSTER_LISTED_TWICE()
            elif character_id not in owned:
                reason = NOT_IN_AUTH_REASON
            elif character_id in holders:
                # A character that has PAP in the fleet is only imported as a correction of tracked PAP, whatever
                # the preview said (a signed preview can be sent again with other settings, or the PAP changed).
                tracked = holders[character_id][2]
                if row.get("status") != HAS_PAP or not tracked or duplicate_action not in CORRECTIONS:
                    reason = has_pap_reason(duplicate_action, tracked)
            if reason:
                result.skipped.append(
                    {"character_id": character_id, "character_name": row["character_name"], "reason": reason}
                )
                continue
            done.add(character_id)
            result.created.append(
                attendance.create_manual_attendance(
                    operation,
                    actor=actor,
                    character_id=character_id,
                    character_name=row["character_name"],
                    attendance_value=attendance_value,
                    duplicate_action=duplicate_action,
                    notes=notes,
                    source=AttendanceRecord.Source.IMPORTED,
                    refresh=False,
                )
            )
        if result.created:
            # One recalculation for the whole paste instead of one per row.
            pap.refresh_after_change(operation, actor=actor, reason="roster import")
        audit(
            actor,
            "attendance.roster_import",
            operation,
            None,
            {
                "count": len(result.created),
                "created_ids": [record.pk for record in result.created],
                "skipped": result.skipped,
                "attendance_value": attendance_value,
                "duplicate_action": duplicate_action,
            },
        )
    return result
