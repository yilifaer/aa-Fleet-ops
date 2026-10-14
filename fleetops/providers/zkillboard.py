# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""zKillboard killmail lookups: the hash for killboard links and the ISK value of a loss.

zKillboard asks API users for a descriptive User-Agent, no query strings, local caching
and spaced-out requests; more than 40 error answers in five minutes block the IP. Requests
are therefore limited site-wide to one every two seconds and paused after a block, a
"slow down" answer or repeated failures. Response bodies are never logged.
"""

import logging
import math
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from email.utils import parsedate_to_datetime

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from fleetops import __title__, __url__, __version__, texts
from fleetops.srp.types import ZkillResult

logger = logging.getLogger(__name__)

API_BASE = "https://zkillboard.com/api/"
KILL_URL = "https://zkillboard.com/kill/{id}/"
TIMEOUT = (5, 10)

# zKillboard withholds killmails younger than five minutes, and its edge cache can keep the
# empty answer for an hour, so a kill is never looked up before it is six minutes old.
MIN_KILL_AGE = timedelta(minutes=6)

SLOT_KEY = "fleetops:zkill:slot"
SLOT_SECONDS = 2
PAUSE_KEY = "fleetops:zkill:pause"
FAILURES_KEY = "fleetops:zkill:failures"
BLOCK_WARNED_KEY = "fleetops:zkill:block-warned"
FAILURE_WINDOW = 5 * 60
FAILURE_LIMIT = 3
BLOCK_PAUSE = 60 * 60
SLOW_DOWN_PAUSE = 15 * 60
FAILURE_PAUSE = 15 * 60
MIN_PAUSE = 60
MAX_PAUSE = 24 * 60 * 60

MAX_ISK = Decimal("1e15")
CENT = Decimal("0.01")
MAX_LABELS = 50
MAX_LABEL_LENGTH = 50
HASH_RE = re.compile(r"[0-9a-f]{40}")
PRODUCT = re.sub(r"\s+", "-", __title__.strip())

MESSAGES = {
    "missing": texts.ZKILL_MISSING(),
    "too_recent": texts.ZKILL_TOO_RECENT(),
    "unavailable": texts.ZKILL_UNAVAILABLE(),
    "throttled": texts.ZKILL_BUSY(),
    "blocked": texts.ZKILL_BLOCKED(),
}


def kill_url(killmail_id: int) -> str:
    return KILL_URL.format(id=int(killmail_id))


def _header_safe(value) -> str:
    # Keep the User-Agent a single, well-formed header value whatever the settings contain.
    return "".join(ch for ch in str(value or "") if 32 < ord(ch) < 127 and ch not in "();")[:200]


def user_agent() -> str:
    """``AA-FleetOps/<version> (+<project url>; <contact>) requests/<version>``.

    The contact is django-esi's ``ESI_USER_CONTACT_EMAIL``, else the site URL.
    """
    details = []
    if __url__:
        details.append(f"+{_header_safe(__url__)}")
    contact = _header_safe(getattr(settings, "ESI_USER_CONTACT_EMAIL", None) or getattr(settings, "SITE_URL", None))
    if contact:
        details.append(contact)
    comment = f" ({'; '.join(details)})" if details else ""
    return f"{PRODUCT}/{__version__}{comment} requests/{requests.__version__}"


def paused_seconds(now: datetime | None = None) -> int:
    """Seconds left before zKillboard may be asked again, 0 when requests are allowed."""
    until = cache.get(PAUSE_KEY)
    if not isinstance(until, (int, float)):
        return 0
    now = now or timezone.now()
    return max(math.ceil(until - now.timestamp()), 0)


def _pause(seconds: int, now: datetime) -> None:
    until = now.timestamp() + seconds
    current = cache.get(PAUSE_KEY)
    if isinstance(current, (int, float)) and current >= until:
        return
    cache.set(PAUSE_KEY, until, timeout=seconds)


def _record_failure(now: datetime) -> None:
    cache.add(FAILURES_KEY, 0, timeout=FAILURE_WINDOW)
    try:
        count = cache.incr(FAILURES_KEY)
    except ValueError:
        # The counter expired between add() and incr().
        cache.set(FAILURES_KEY, 1, timeout=FAILURE_WINDOW)
        count = 1
    if count >= FAILURE_LIMIT:
        cache.delete(FAILURES_KEY)
        _pause(FAILURE_PAUSE, now)
        logger.warning(
            "zKillboard failed %s times in a row; FleetOps pauses its requests for %s minutes.",
            count,
            FAILURE_PAUSE // 60,
        )


def _record_success() -> None:
    cache.delete(FAILURES_KEY)


def _retry_after(value, now: datetime) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        seconds = int(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt_timezone.utc)
        seconds = math.ceil((when - now).total_seconds())
    return min(max(seconds, MIN_PAUSE), MAX_PAUSE)


def _isk(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = Decimal(str(value))
    if not amount.is_finite() or not 0 <= amount < MAX_ISK:
        return None
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _flag(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _labels(value) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = [label for label in value if isinstance(label, str) and 0 < len(label) <= MAX_LABEL_LENGTH]
    return labels[:MAX_LABELS]


def _result(status: str, *, retry_after: int | None = None, requested: bool = True, message: str = "") -> ZkillResult:
    return ZkillResult(
        status=status,
        retry_after=retry_after,
        requested=requested,
        message=message or MESSAGES[status],
    )


def _block(response_status: int, killmail_id: int, now: datetime) -> ZkillResult:
    _pause(BLOCK_PAUSE, now)
    if cache.add(BLOCK_WARNED_KEY, 1, timeout=BLOCK_PAUSE):
        logger.warning(
            "zKillboard refused the request for kill %s (HTTP %s); FleetOps pauses its requests for %s minutes.",
            killmail_id,
            response_status,
            BLOCK_PAUSE // 60,
        )
    return _result("blocked", retry_after=BLOCK_PAUSE)


def _failure(killmail_id: int, now: datetime, detail: str) -> ZkillResult:
    logger.warning("zKillboard lookup of kill %s failed: %s.", killmail_id, detail)
    _record_failure(now)
    return _result("unavailable")


def _parse(data, killmail_id: int, response_status: int, now: datetime) -> ZkillResult:
    if isinstance(data, dict) and "error" in data:
        return _failure(killmail_id, now, f"zKillboard returned an error (HTTP {response_status})")
    if not isinstance(data, list):
        return _failure(killmail_id, now, f"unexpected response (HTTP {response_status})")
    item = next(
        (
            entry
            for entry in data
            if isinstance(entry, dict)
            and not isinstance(entry.get("killmail_id"), bool)
            and entry.get("killmail_id") == killmail_id
        ),
        None,
    )
    if item is None:
        _record_success()
        return _result("missing")
    zkb = item.get("zkb")
    killmail_hash = zkb.get("hash") if isinstance(zkb, dict) else None
    killmail_hash = killmail_hash.lower() if isinstance(killmail_hash, str) else ""
    if not HASH_RE.fullmatch(killmail_hash):
        return _failure(killmail_id, now, "the answer has no valid killmail hash")
    _record_success()
    return ZkillResult(
        status="ok",
        killmail_hash=killmail_hash,
        fitted_value=_isk(zkb.get("fittedValue")),
        total_value=_isk(zkb.get("totalValue")),
        destroyed_value=_isk(zkb.get("destroyedValue")),
        dropped_value=_isk(zkb.get("droppedValue")),
        npc=_flag(zkb.get("npc")),
        solo=_flag(zkb.get("solo")),
        awox=_flag(zkb.get("awox")),
        labels=_labels(zkb.get("labels")),
    )


def fetch_kill(killmail_id: int, *, killmail_time: datetime | None = None, now: datetime | None = None) -> ZkillResult:
    """Look one killmail up on zKillboard.

    With ``killmail_time`` a kill younger than ``MIN_KILL_AGE`` is not requested at all.
    """
    killmail_id = int(killmail_id)
    if killmail_id <= 0:
        raise ValueError("killmail_id must be positive")
    now = now or timezone.now()

    if killmail_time is not None and killmail_time > now - MIN_KILL_AGE:
        wait = math.ceil((killmail_time + MIN_KILL_AGE - now).total_seconds())
        return _result("missing", retry_after=wait, requested=False, message=MESSAGES["too_recent"])
    paused = paused_seconds(now)
    if paused:
        return _result("unavailable", retry_after=paused, requested=False)
    if not cache.add(SLOT_KEY, 1, timeout=SLOT_SECONDS):
        return _result("throttled", retry_after=SLOT_SECONDS, requested=False)

    try:
        response = requests.get(
            f"{API_BASE}killID/{killmail_id}/",
            headers={
                "User-Agent": user_agent(),
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return _failure(killmail_id, now, type(exc).__name__)

    status = response.status_code
    if status in (301, 302, 303, 307, 308):
        if "/api/blocked.json" in (response.headers.get("Location") or ""):
            return _block(status, killmail_id, now)
        return _failure(killmail_id, now, f"unexpected redirect (HTTP {status})")
    if status == 403:
        return _block(status, killmail_id, now)
    if status in (429, 503):
        seconds = _retry_after(response.headers.get("Retry-After"), now) or SLOW_DOWN_PAUSE
        _pause(seconds, now)
        logger.warning(
            "zKillboard asked FleetOps to slow down (HTTP %s for kill %s); pausing requests for %s seconds.",
            status,
            killmail_id,
            seconds,
        )
        return _result("unavailable", retry_after=seconds)
    if status != 200:
        return _failure(killmail_id, now, f"HTTP {status}")
    try:
        data = response.json()
    except ValueError:
        return _failure(killmail_id, now, f"the answer is not JSON (HTTP {status})")
    return _parse(data, killmail_id, status, now)
