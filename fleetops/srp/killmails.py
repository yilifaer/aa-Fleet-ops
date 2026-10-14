# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Reading killmail references pasted by members and the killmails ESI returns for them."""

import re
from datetime import datetime, timedelta, timezone as dt_timezone

from allianceauth.services.hooks import get_extension_logger
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from fleetops import texts
from fleetops.models import MAX_DATABASE_ID, SrpKillmail, SrpSettings
from fleetops.providers import zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import KILLMAIL_NOT_FOUND, get_killmail
from fleetops.services import names, sde
from fleetops.services.errors import log_failure
from fleetops.srp import values
from fleetops.srp.types import KillmailRef, SrpError, ZkillResult

logger = get_extension_logger(__name__)

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

# Stored messages (English): the pages show them through ``translate_stored``.
UNRECOGNISED = texts.SRP_UNRECOGNISED()

MAX_ITEMS = 5000
MAX_ITEM_DEPTH = 3

ZkillStatus = SrpKillmail.ZkillStatus
HASH_RE = re.compile(r"[0-9a-f]{40}")
SHIP_NAME_LENGTH = 255
SYSTEM_NAME_LENGTH = 100

USE_INGAME_LINK = texts.SRP_USE_INGAME_LINK()
NO_HASH = f"{texts.ZKILL_MISSING()} {USE_INGAME_LINK}"
ZKILLBOARD_OFF = f"{texts.SRP_KILLBOARD_OFF()} {USE_INGAME_LINK}"


def _ref(killmail_id: str, killmail_hash: str | None, source: str) -> KillmailRef:
    number = int(killmail_id)
    if not 0 < number <= MAX_DATABASE_ID:
        raise SrpError("invalid_id", texts.SRP_BAD_KILLMAIL_ID())
    return KillmailRef(number, killmail_hash.lower() if killmail_hash else None, source)


def parse_killmail_reference(text: str) -> KillmailRef:
    """Find the killmail in a pasted in-game link, ESI or killboard URL, or "ID HASH" text.

    Raises ``SrpError`` with a text for the member when nothing usable is found.
    """
    text = text.strip() if isinstance(text, str) else ""
    if len(text) > MAX_INPUT_LENGTH:
        raise SrpError("too_long", texts.SRP_TEXT_TOO_LONG())
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
        raise SrpError("invalid_killmail", texts.SRP_INCOMPLETE_KILLMAIL()) from exc


def _hash_from_zkillboard(killmail_id: int) -> ZkillResult:
    """zKillboard's answer for a killmail pasted as a killboard link; raises ``SrpError`` without a hash."""
    result = zkillboard.fetch_kill(killmail_id)
    if result.ok and result.killmail_hash:
        return result
    if result.status == "missing":
        raise SrpError("no_hash", NO_HASH)
    if result.status == "throttled":
        raise SrpError("zkill_busy", result.message)
    raise SrpError("zkill_unavailable", f"{result.message} {USE_INGAME_LINK}".strip())


def _read_and_store(killmail_id: int, killmail_hash: str) -> SrpKillmail:
    try:
        payload = get_killmail(killmail_id, killmail_hash)
    except FleetESIError as exc:
        code = "not_found" if exc.code == "KILLMAIL_NOT_FOUND" else "esi_unavailable"
        raise SrpError(code, str(exc)) from exc
    if killmail_fields(payload)["killmail_id"] != killmail_id:
        raise SrpError("invalid_killmail", texts.SRP_INCOMPLETE_KILLMAIL())
    return store_killmail(payload, killmail_hash)


def _wants_estimate(km: SrpKillmail) -> bool:
    """Whether the ESI estimate is the value source the admin chose and the killmail has none yet."""
    return km.esi_estimate is None and SrpSettings.get_solo().value_source == SrpSettings.ValueSource.ESI_ESTIMATE


def _update_value(km: SrpKillmail, zkill_ok: bool, zkill_result: ZkillResult | None) -> None:
    """Bring the value of a killmail up to date for the claim check; never raises for a value problem."""
    now = timezone.now()
    if zkill_result is not None:
        values.apply_zkill_result(km, zkill_result, now=now)
    if not zkill_ok:
        if km.zkill_status in values.WAITING_STATUSES or (
            km.zkill_status == ZkillStatus.DISABLED and km.esi_estimate is None
        ):
            values.settle_without_zkill(km, ZkillStatus.DISABLED, now=now)
        return
    if km.zkill_status == ZkillStatus.DISABLED:
        # zKillboard was switched off when the killmail was read and is on again.
        schedule_zkill(km, now=now)
    due = km.zkill_next_attempt_at is not None and km.zkill_next_attempt_at <= now
    if km.zkill_status in values.WAITING_STATUSES and due:
        values.refresh_zkill(km, now=now)
    if _wants_estimate(km):
        values.esi_estimate(km)


def fetch_killmail(ref: KillmailRef, *, zkill_ok: bool) -> SrpKillmail:
    """The stored killmail for a pasted reference.

    Uses the stored row when there is one (a given hash must match it), else takes the hash from the
    reference or from zKillboard (``zkill_ok``), reads the killmail from ESI and stores it. The value is
    looked up as far as it can be now: from zKillboard once the killmail is old enough, or as an ESI
    estimate. Raises ``SrpError``.
    """
    km = SrpKillmail.objects.filter(pk=ref.killmail_id).first()
    zkill_result = None
    if km is None:
        killmail_hash = ref.killmail_hash
        if killmail_hash is None:
            if not zkill_ok:
                raise SrpError("zkill_off", ZKILLBOARD_OFF)
            zkill_result = _hash_from_zkillboard(ref.killmail_id)
            killmail_hash = zkill_result.killmail_hash
        km = _read_and_store(ref.killmail_id, killmail_hash.lower())
    elif ref.killmail_hash and ref.killmail_hash.lower() != km.killmail_hash:
        # The same answer as for a hash ESI does not know, so the stored hash is not given away.
        raise SrpError("not_found", KILLMAIL_NOT_FOUND)
    try:
        _update_value(km, zkill_ok, zkill_result)
    except Exception as exc:  # the value is a convenience here; the claim check goes on without it
        log_failure(logger, "SRP loss value lookup", exc)
        km.refresh_from_db()
    return km


def _names(ship_type_id: int, solar_system_id: int) -> tuple[str, str]:
    """English ship and system names: from the SDE when installed, else from ESI (cached); '' when unknown."""
    ship = sde.item_type_names([ship_type_id]).get(ship_type_id, "")
    system = sde.solar_system_names([solar_system_id]).get(solar_system_id, "")
    unknown = [pk for pk, name in ((ship_type_id, ship), (solar_system_id, system)) if not name]
    if unknown:
        try:
            found = names.entity_names(unknown)
        except Exception as exc:  # names are a convenience; the claim does not depend on them
            log_failure(logger, "SRP killmail name lookup", exc)
            found = {}
        ship = ship or found.get(ship_type_id, "")
        system = system or found.get(solar_system_id, "")
    return ship[:SHIP_NAME_LENGTH], system[:SYSTEM_NAME_LENGTH]


def store_killmail(payload: dict, killmail_hash: str) -> SrpKillmail:
    """Create the ``SrpKillmail`` row for an ESI killmail (a concurrent create returns the existing row),
    with ship and system names, and schedule the zKillboard lookup."""
    fields = killmail_fields(payload)
    killmail_hash = killmail_hash.lower() if isinstance(killmail_hash, str) else ""
    if not HASH_RE.fullmatch(killmail_hash):
        raise SrpError("invalid_hash", KILLMAIL_NOT_FOUND)
    killmail_id = fields["killmail_id"]
    existing = SrpKillmail.objects.filter(pk=killmail_id).first()
    if existing is not None:
        return existing
    ship_name, system_name = _names(fields["ship_type_id"], fields["solar_system_id"])
    try:
        with transaction.atomic():
            km = SrpKillmail.objects.create(
                killmail_hash=killmail_hash,
                ship_type_name=ship_name,
                solar_system_name=system_name,
                **fields,
            )
    except IntegrityError:
        # Another request stored the same killmail first.
        km = SrpKillmail.objects.filter(pk=killmail_id).first()
        if km is None:
            raise
        return km
    schedule_zkill(km)
    return km


def schedule_zkill(km: SrpKillmail, *, now: datetime | None = None) -> None:
    """Set when zKillboard is asked about the killmail first (never before it is six minutes old).

    A killmail that has its value, or that zKillboard gave up on, is left alone.
    """
    if km.zkill_status in (ZkillStatus.OK, ZkillStatus.UNAVAILABLE):
        return
    km.zkill_next_attempt_at = values.first_zkill_check(km.killmail_time, now=now)
    fields = ["zkill_next_attempt_at"]
    if km.zkill_status == ZkillStatus.DISABLED:
        km.zkill_status = ZkillStatus.PENDING
        fields.append("zkill_status")
    km.save(update_fields=fields)


def prune_unclaimed(days: int = 7) -> int:
    """Delete killmails without a claim that were fetched more than ``days`` days ago; returns how many."""
    cutoff = timezone.now() - timedelta(days=days)
    for _attempt in range(2):
        try:
            deleted, _by_model = SrpKillmail.objects.filter(claim__isnull=True, fetched_at__lt=cutoff).delete()
        except ProtectedError:
            # A claim was created for one of them while it was being deleted; the next try leaves it out.
            continue
        return deleted
    return 0
