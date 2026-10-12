"""Names, corporations and alliances of characters Alliance Auth does not know.

ESI answers are kept in two small caches, ``EveEntityName`` and ``CharacterAffiliation``. Fleet
tracking queues a background lookup for members it cannot name; pages and identities only read
the caches and never wait for ESI.
"""

from dataclasses import dataclass
from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from django.db import connections, router, transaction
from django.utils import timezone

from fleetops.models import (
    CharacterAffiliation,
    EveEntityName,
    FleetMemberEvent,
    FleetMemberState,
    OperationRoleAssignment,
)
from fleetops.providers import esi
from fleetops.services.errors import log_failure

logger = get_extension_logger(__name__)

NAME_TTL = timedelta(days=7)
AFFILIATION_TTL = timedelta(hours=1)
# IDs ESI did not know are asked for again after this long.
UNRESOLVED_RETRY = timedelta(days=1)
# A fleet queues at most one lookup in this many seconds, however often it is polled.
RESOLVE_DEBOUNCE_SECONDS = 120

UNRESOLVED = EveEntityName.UNRESOLVED


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    character_id: int
    character_name: str
    corporation_id: int | None
    corporation_name: str
    alliance_id: int | None
    alliance_name: str


def placeholder_name(character_id) -> str:
    """Name shown for a character until its real name is known."""
    return f"Character {character_id}"


def is_placeholder(name, character_id) -> bool:
    return not name or name == placeholder_name(character_id)


def _ids(values) -> list[int]:
    return list(dict.fromkeys(int(value) for value in values or () if value))


def _name_rows(ids) -> dict[int, EveEntityName]:
    return {row.id: row for row in EveEntityName.objects.filter(id__in=list(ids))}


def _fresh(row, now) -> bool:
    if row is None:
        return False
    ttl = UNRESOLVED_RETRY if row.category == UNRESOLVED else NAME_TTL
    return row.updated_at >= now - ttl


def _upsert(model, rows, update_fields):
    if not rows:
        return
    # SQLite and PostgreSQL need the conflicting key named; MySQL and MariaDB refuse it.
    features = connections[router.db_for_write(model)].features
    unique_fields = [model._meta.pk.name] if features.supports_update_conflicts_with_target else None
    model.objects.bulk_create(rows, update_conflicts=True, unique_fields=unique_fields, update_fields=update_fields)


def _store_names(results, now):
    """Store ``{id: (category, name)}`` lookups.

    An ID ESI no longer knows keeps the name it had, so a resolved name is never lost.
    """
    resolved = [
        EveEntityName(id=entity_id, category=category[:20], name=name[:255], updated_at=now)
        for entity_id, (category, name) in results.items()
        if category != UNRESOLVED and name
    ]
    unresolved = [
        EveEntityName(id=entity_id, category=UNRESOLVED, name="", updated_at=now)
        for entity_id, (category, name) in results.items()
        if category == UNRESOLVED or not name
    ]
    _upsert(EveEntityName, resolved, ["category", "name", "updated_at"])
    _upsert(EveEntityName, unresolved, ["category", "updated_at"])


def entity_names(ids) -> dict[int, str]:
    """Names of EVE IDs of any category: cached ones first, the others from ESI (and then cached).

    IDs nobody could name are left out. When ESI fails the cached names are returned, also old ones.
    """
    ids = _ids(ids)
    if not ids:
        return {}
    now = timezone.now()
    rows = _name_rows(ids)
    missing = [entity_id for entity_id in ids if not _fresh(rows.get(entity_id), now)]
    if missing:
        try:
            _store_names(esi.resolve_names(missing), now)
        except esi.FleetESIError:
            # The provider has logged the failure; old names are better than none.
            pass
        else:
            rows = _name_rows(ids)
    return {entity_id: row.name for entity_id, row in rows.items() if row.name}


def cached_identities(character_ids) -> dict[int, ExternalIdentity]:
    """Name, corporation and alliance of characters from the caches; ESI is not called.

    Characters with neither a cached name nor an affiliation are left out. A character with only
    an affiliation gets the placeholder name.
    """
    ids = _ids(character_ids)
    if not ids:
        return {}
    affiliations = {row.character_id: row for row in CharacterAffiliation.objects.filter(character_id__in=ids)}
    related = {
        entity_id
        for row in affiliations.values()
        for entity_id in (row.corporation_id, row.alliance_id)
        if entity_id
    }
    names = {
        row.id: row.name for row in EveEntityName.objects.filter(id__in=[*ids, *related]).exclude(name="")
    }
    result = {}
    for character_id in ids:
        affiliation = affiliations.get(character_id)
        if character_id not in names and affiliation is None:
            continue
        corporation_id = affiliation.corporation_id if affiliation else None
        alliance_id = affiliation.alliance_id if affiliation else None
        result[character_id] = ExternalIdentity(
            character_id=character_id,
            character_name=names.get(character_id) or placeholder_name(character_id),
            corporation_id=corporation_id,
            corporation_name=names.get(corporation_id, "") if corporation_id else "",
            alliance_id=alliance_id,
            alliance_name=names.get(alliance_id, "") if alliance_id else "",
        )
    return result


def stale_character_ids(character_ids, *, now=None) -> list[int]:
    """Characters whose cached name or affiliation is missing or old enough to be looked up again."""
    ids = _ids(character_ids)
    if not ids:
        return []
    now = now or timezone.now()
    names = _name_rows(ids)
    affiliations = dict(
        CharacterAffiliation.objects.filter(character_id__in=ids).values_list("character_id", "updated_at")
    )
    stale = []
    for character_id in ids:
        row = names.get(character_id)
        if row is not None and row.category == UNRESOLVED:
            # ESI did not know this ID; it is only asked for again after UNRESOLVED_RETRY.
            if not _fresh(row, now):
                stale.append(character_id)
            continue
        affiliation_at = affiliations.get(character_id)
        if not _fresh(row, now) or affiliation_at is None or affiliation_at < now - AFFILIATION_TTL:
            stale.append(character_id)
    return stale


def unnamed_character_ids(operation_id) -> list[int]:
    """Members of a fleet without an Alliance Auth account whose name or corporation is still missing.

    Includes members who have left, so a lookup that was skipped or failed while they were in
    fleet is made up for later.
    """
    rows = FleetMemberState.objects.filter(operation_id=operation_id, auth_user=None).values_list(
        "character_id", "character_name", "corporation_id"
    )
    return [
        character_id
        for character_id, name, corporation_id in rows
        if is_placeholder(name, character_id) or not corporation_id
    ]


def _debounce_key(operation_id) -> str:
    return f"fleetops:names:{operation_id}"


def queue_name_resolution(operation_id, character_ids, *, force=False) -> None:
    """Look the characters up in the background once the current transaction has committed.

    A fleet queues at most one lookup every RESOLVE_DEBOUNCE_SECONDS, unless ``force`` is set (the
    last lookup when the fleet ends). Characters that are still unknown, also those who have left,
    are passed again by a later poll (see ``unnamed_character_ids``).
    """
    ids = sorted(_ids(character_ids))
    if not ids:
        return

    def queue():
        from fleetops.tasks import resolve_character_names

        try:
            key = _debounce_key(operation_id)
            if force:
                cache.set(key, "1", timeout=RESOLVE_DEBOUNCE_SECONDS)
            if force or cache.add(key, "1", timeout=RESOLVE_DEBOUNCE_SECONDS):
                resolve_character_names.delay(operation_id, ids)
        except Exception as exc:
            # Names are a convenience and must never break tracking; a later poll queues them again.
            log_failure(logger, "Queueing the name lookup", exc)

    transaction.on_commit(queue)


def resolve_and_store(character_ids) -> int:
    """Look up the characters whose cached name or affiliation is missing or old.

    One affiliation lookup, then one name lookup for the characters and their corporations and
    alliances, within one call budget. Returns how many names and affiliations were stored. ESI
    failures raise FleetESIError; affiliations stored before a failed name lookup are kept.
    """
    now = timezone.now()
    ids = stale_character_ids(character_ids, now=now)
    if not ids:
        return 0
    budget = esi.LookupBudget()
    affiliations = esi.character_affiliations(ids, budget=budget)
    known = {character_id: values for character_id, values in affiliations.items() if values is not None}
    unknown = [character_id for character_id, values in affiliations.items() if values is None]
    _upsert(
        CharacterAffiliation,
        [CharacterAffiliation(character_id=character_id, updated_at=now, **values) for character_id, values in known.items()],
        ["corporation_id", "alliance_id", "faction_id", "updated_at"],
    )
    related = sorted(
        {entity_id for values in known.values() for entity_id in (values["corporation_id"], values["alliance_id"]) if entity_id}
    )
    rows = _name_rows([*ids, *related])
    wanted = [
        entity_id
        for entity_id in dict.fromkeys([*ids, *related])
        if entity_id not in unknown and not _fresh(rows.get(entity_id), now)
    ]
    # A character ESI does not know cannot be named either; it is not asked for twice.
    names = dict.fromkeys(unknown, (UNRESOLVED, ""))
    try:
        names.update(esi.resolve_names(wanted, budget=budget))
    finally:
        _store_names(names, now)
    return len(known) + len(names)


def apply_names_to_operation(operation_id, character_ids) -> int:
    """Put cached names, corporations and alliances on a fleet's rows for characters Alliance Auth does not know.

    Changes member states without an Alliance Auth user, and events and special roles still showing
    the placeholder name. A real name is never replaced by a placeholder. Returns the number of
    rows changed.
    """
    changed = 0
    for character_id, identity in cached_identities(character_ids).items():
        named = not is_placeholder(identity.character_name, character_id)
        placeholder = placeholder_name(character_id)
        states = FleetMemberState.objects.filter(operation_id=operation_id, character_id=character_id, auth_user=None)
        roles = OperationRoleAssignment.objects.filter(
            operation_id=operation_id, character_id=character_id, auth_user=None
        )
        if named:
            changed += states.filter(character_name=placeholder).update(character_name=identity.character_name)
            changed += roles.filter(character_name=placeholder).update(character_name=identity.character_name)
            changed += FleetMemberEvent.objects.filter(
                operation_id=operation_id, character_id=character_id, character_name=placeholder
            ).update(character_name=identity.character_name)
        if identity.corporation_id and identity.corporation_name:
            changed += states.update(
                corporation_id=identity.corporation_id,
                corporation_name=identity.corporation_name,
                alliance_id=identity.alliance_id,
                alliance_name=identity.alliance_name,
            )
            changed += roles.update(corporation_id=identity.corporation_id, corporation_name=identity.corporation_name)
    return changed
