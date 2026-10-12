"""Reading killmail references pasted by members and the killmails ESI returns for them."""

import re
from datetime import datetime, timezone as dt_timezone

from django.utils.dateparse import parse_datetime

from fleetops.models import MAX_DATABASE_ID
from fleetops.srp.types import KillmailRef, SrpError

MAX_INPUT_LENGTH = 2000

SOURCE_INGAME = "ingame"
SOURCE_ESI_URL = "esi_url"
SOURCE_ID_HASH = "id_hash"
SOURCE_ZKILLBOARD = "zkillboard"
SOURCE_KILLBOARD = "killboard"

_HASH = r"([0-9a-fA-F]{40})(?![0-9a-fA-F])"
# In-game links arrive as <url=killReport:ID:HASH>…</url> or <a href="killReport:ID:HASH">…</a>.
INGAME_RE = re.compile(r"killReport:(\d{1,19}):" + _HASH, re.IGNORECASE | re.ASCII)
ESI_URL_RE = re.compile(
    r"esi\.evetech\.net/(?:latest/|v\d+/|dev/)?killmails/(\d+)/" + _HASH,
    re.IGNORECASE | re.ASCII,
)
ID_HASH_RE = re.compile(r"(\d{1,19})(?:\s*[:/]\s*|\s+)([0-9a-fA-F]{40})", re.ASCII)


def _kill_url_re(host: str) -> re.Pattern:
    return re.compile(
        r"(?<![\w.-])(?:https?://)?" + host + r"/kill/(\d+)(?=[/?#\s\"'<>]|$)",
        re.IGNORECASE | re.ASCII,
    )


ZKILLBOARD_RE = _kill_url_re(r"(?:www\.)?zkillboard\.com")
KILLBOARD_RES = (_kill_url_re(r"(?:www\.)?eve-kill\.com"), _kill_url_re(r"kb\.evetools\.org"))

UNRECOGNISED = "Paste an in-game killmail link, a zKillboard link, or the killmail ID and hash."

MAX_ITEMS = 5000
MAX_ITEM_DEPTH = 3


def _ref(killmail_id: str, killmail_hash: str | None, source: str) -> KillmailRef:
    number = int(killmail_id)
    if not 0 < number <= MAX_DATABASE_ID:
        raise SrpError("invalid_id", "This killmail ID is not valid.")
    return KillmailRef(number, killmail_hash.lower() if killmail_hash else None, source)


def parse_killmail_reference(text: str) -> KillmailRef:
    """Find the killmail in a pasted in-game link, ESI or killboard URL, or "ID HASH" text.

    Raises ``SrpError`` with a text for the member when nothing usable is found.
    """
    text = text.strip() if isinstance(text, str) else ""
    if len(text) > MAX_INPUT_LENGTH:
        raise SrpError("too_long", "That text is too long. Paste only the killmail link.")
    if not text:
        raise SrpError("unrecognised", UNRECOGNISED)

    match = INGAME_RE.search(text)
    if match:
        return _ref(match.group(1), match.group(2), SOURCE_INGAME)
    match = ESI_URL_RE.search(text)
    if match:
        return _ref(match.group(1), match.group(2), SOURCE_ESI_URL)
    match = ID_HASH_RE.fullmatch(text)
    if match:
        return _ref(match.group(1), match.group(2), SOURCE_ID_HASH)
    match = ZKILLBOARD_RE.search(text)
    if match:
        return _ref(match.group(1), None, SOURCE_ZKILLBOARD)
    for pattern in KILLBOARD_RES:
        match = pattern.search(text)
        if match:
            return _ref(match.group(1), None, SOURCE_KILLBOARD)
    raise SrpError("unrecognised", UNRECOGNISED)


def _id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_DATABASE_ID:
        raise ValueError(f"not an EVE id: {value!r}")
    return value


def _optional_id(value) -> int | None:
    return None if value is None else _id(value)


def _count(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"not a count: {value!r}")
    return value


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = parse_datetime(value)
    if not isinstance(value, datetime):
        raise ValueError("killmail_time is missing")
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def _clean_items(items, depth: int, budget: list[int]) -> list[dict]:
    cleaned = []
    for item in items or ():
        if budget[0] <= 0:
            break
        if not isinstance(item, dict):
            continue
        try:
            entry = {
                "flag": _count(item.get("flag")),
                "item_type_id": _id(item.get("item_type_id")),
                "quantity_destroyed": _count(item.get("quantity_destroyed")),
                "quantity_dropped": _count(item.get("quantity_dropped")),
                "singleton": _count(item.get("singleton")) or 0,
            }
        except ValueError:
            continue
        if entry["flag"] is None:
            continue
        budget[0] -= 1
        nested = item.get("items")
        if nested and depth < MAX_ITEM_DEPTH:
            entry["items"] = _clean_items(nested, depth + 1, budget)
        cleaned.append(entry)
    return cleaned


def killmail_fields(payload: dict) -> dict:
    """The stored fields of an ESI killmail, with types checked and items reduced to what SRP uses."""
    try:
        victim = payload["victim"]
        if not isinstance(victim, dict):
            raise TypeError("victim is not an object")
        attackers = payload.get("attackers") or []
        return {
            "killmail_id": _id(payload["killmail_id"]),
            "killmail_time": _utc(payload["killmail_time"]),
            "solar_system_id": _id(payload["solar_system_id"]),
            "victim_character_id": _optional_id(victim.get("character_id")),
            "victim_corporation_id": _optional_id(victim.get("corporation_id")),
            "victim_alliance_id": _optional_id(victim.get("alliance_id")),
            "ship_type_id": _id(victim["ship_type_id"]),
            "attacker_count": len(attackers) if isinstance(attackers, list) else 0,
            "war_id": _optional_id(payload.get("war_id")),
            "items": _clean_items(victim.get("items"), 1, [MAX_ITEMS]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SrpError("invalid_killmail", "ESI returned an incomplete killmail. Try again later.") from exc
