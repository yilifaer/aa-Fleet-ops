# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The attackers of a killmail as the review page shows them (REV-9b).

The killmail stores IDs only (``killmails.clean_attackers``). The page never calls ESI: names come from the cache
(``names.cached_entity_names``) and an ID without a cached name is shown as the ID. A background task warms that cache
after the killmail was stored, so the names are there when a reviewer opens the claim.
"""

from collections import Counter

from allianceauth.services.hooks import get_extension_logger
from django.db import transaction

from fleetops.services import names
from fleetops.services.errors import log_failure

logger = get_extension_logger(__name__)

TOP_GROUPS_SHOWN = 3
_NAME_FIELDS = ("character_id", "corporation_id", "alliance_id", "ship_type_id", "weapon_type_id")


def entity_ids(attackers) -> list[int]:
    """Every ID the summary may name, once each, in the order they appear."""
    found = {}
    for attacker in attackers or ():
        if not isinstance(attacker, dict):
            continue
        for name in _NAME_FIELDS:
            value = attacker.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                found[value] = None
    return list(found)


def warm_names(killmail_id) -> int:
    """Look up the names of the attackers of a stored killmail (this may call ESI: run it in a worker). Returns the
    number of IDs asked for; a failure is logged and ignored, the page shows IDs then."""
    from fleetops.models import SrpKillmail

    attackers = SrpKillmail.objects.filter(pk=killmail_id).values_list("attackers", flat=True).first()
    ids = entity_ids(attackers)
    if not ids:
        return 0
    try:
        names.entity_names(ids)
    except Exception as exc:  # names are a convenience
        log_failure(logger, "SRP attacker name lookup", exc)
    return len(ids)


def queue_name_warming(killmail) -> None:
    """Queue ``warm_srp_killmail_names`` once the current transaction has committed. A killmail without stored attackers
    queues nothing; a broker problem is logged and ignored."""
    if not killmail.attackers:
        return
    killmail_id = killmail.pk

    def queue():
        from fleetops import tasks

        try:
            tasks.warm_srp_killmail_names.delay(killmail_id)
        except Exception as exc:
            log_failure(logger, "Queueing the SRP attacker name lookup", exc)

    transaction.on_commit(queue)


def _label(identifier, cached) -> str:
    if identifier is None:
        return ""
    return cached.get(identifier) or str(identifier)


def summary(killmail) -> dict:
    """What the review page shows about the attackers of ``killmail``.

    ``stored`` is False for a killmail stored before the attackers were kept (the page then shows the count only).
    The counts of players and NPCs are those of the stored attackers; when ESI listed more than were stored
    (``truncated``) they are a lower bound. Costs one query for the names.
    """
    attackers = killmail.attackers if isinstance(killmail.attackers, list) else []
    total = killmail.attacker_count or 0
    result = {
        "stored": bool(attackers),
        "total": total,
        "players": 0,
        "npcs": 0,
        "truncated": bool(attackers) and total > len(attackers),
        "solo": killmail.zkill_solo,
        "npc": killmail.zkill_npc,
        "awox": killmail.zkill_awox,
        "final_blow": None,
        "top_damage": None,
        "groups": [],
    }
    if not attackers:
        return result
    cached = names.cached_entity_names(entity_ids(attackers))
    players = [row for row in attackers if row.get("character_id")]
    result["players"] = len(players)
    result["npcs"] = len(attackers) - len(players)
    taken = killmail.damage_taken or 0

    def person(row):
        return {
            "character": _label(row.get("character_id"), cached) if row.get("character_id") else "",
            "corporation": _label(row.get("corporation_id"), cached),
            "alliance": _label(row.get("alliance_id"), cached),
            "ship": _label(row.get("ship_type_id"), cached),
            "weapon": _label(row.get("weapon_type_id"), cached),
            "damage": row.get("damage_done") or 0,
            "percent": round(100 * (row.get("damage_done") or 0) / taken) if taken else None,
        }

    final = next((row for row in attackers if row.get("final_blow")), None)
    if final is not None:
        result["final_blow"] = person(final)
    result["top_damage"] = person(attackers[0])
    # Players only, grouped by alliance when they have one, else by corporation.
    counts = Counter(
        ("alliance", row["alliance_id"]) if row.get("alliance_id") else ("corporation", row.get("corporation_id"))
        for row in players
        if row.get("alliance_id") or row.get("corporation_id")
    )
    result["groups"] = [
        {"kind": kind, "name": _label(identifier, cached), "count": count}
        for (kind, identifier), count in counts.most_common(TOP_GROUPS_SHOWN)
    ]
    return result
