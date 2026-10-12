"""Loss values: which value a claim uses, when zKillboard is asked again, and the ESI estimate."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import get_market_prices
from fleetops.providers.zkillboard import MIN_KILL_AGE
from fleetops.srp.types import ZkillResult

VALUE_ZKILL_FITTED = "zkill_fitted"
VALUE_ZKILL_TOTAL = "zkill_total"
VALUE_ESI_ESTIMATE = "esi_estimate"
VALUE_SOURCES = (VALUE_ZKILL_FITTED, VALUE_ZKILL_TOTAL, VALUE_ESI_ESTIMATE)

_VALUE_FIELDS = {
    VALUE_ZKILL_FITTED: "zkill_fitted_value",
    VALUE_ZKILL_TOTAL: "zkill_total_value",
    VALUE_ESI_ESTIMATE: "esi_estimate",
}
_VALUE_ORDER = {
    VALUE_ZKILL_FITTED: (VALUE_ZKILL_FITTED, VALUE_ZKILL_TOTAL, VALUE_ESI_ESTIMATE),
    VALUE_ZKILL_TOTAL: (VALUE_ZKILL_TOTAL, VALUE_ZKILL_FITTED, VALUE_ESI_ESTIMATE),
    VALUE_ESI_ESTIMATE: (VALUE_ESI_ESTIMATE, VALUE_ZKILL_FITTED, VALUE_ZKILL_TOTAL),
}

# Low, mid and high slots, drone bay, implants, rigs, subsystems, fighter bay and fighter tubes.
FITTED_FLAGS = frozenset([*range(11, 35), 87, 89, *range(92, 100), *range(125, 133), *range(158, 164)])
BLUEPRINT_COPY = 2
MAX_ESTIMATE_ITEMS = 10_000
CENT = Decimal("0.01")

# Delays after the 1st to 4th lookup without a value; the 5th is the last one.
ZKILL_RETRY_DELAYS = (timedelta(minutes=65), timedelta(hours=3), timedelta(hours=12), timedelta(hours=24))
ZKILL_MAX_ATTEMPTS = len(ZKILL_RETRY_DELAYS) + 1


def loss_value(km, source: str) -> tuple[Decimal | None, str]:
    """The value for ``source`` and the source it came from.

    Falls back to the other zKillboard value, then to the ESI estimate. ``km`` is any object
    with ``zkill_fitted_value``, ``zkill_total_value`` and ``esi_estimate`` attributes.
    """
    try:
        order = _VALUE_ORDER[source]
    except KeyError:
        raise ValueError(f"Unknown value source: {source!r}") from None
    for candidate in order:
        value = getattr(km, _VALUE_FIELDS[candidate], None)
        if value is not None:
            return Decimal(value), candidate
    return None, ""


@dataclass(frozen=True, slots=True)
class ZkillSchedule:
    """zKillboard state of a killmail: ``status`` uses the stored values (pending, ok, missing, unavailable)."""

    status: str
    attempts: int
    next_attempt_at: datetime | None


def first_zkill_check(killmail_time: datetime, *, now: datetime | None = None) -> datetime:
    """The earliest time zKillboard may be asked about a kill."""
    now = now or timezone.now()
    return max(now, killmail_time + MIN_KILL_AGE)


def after_zkill_lookup(
    result: ZkillResult,
    *,
    attempts: int,
    status: str,
    killmail_time: datetime,
    now: datetime | None = None,
) -> ZkillSchedule:
    """The state after ``result``, given the previous ``attempts`` count and ``status``.

    Answers that did not send a request (busy, paused, too recent) are not counted. After
    the last attempt without a value the status becomes ``unavailable`` and the ESI
    estimate takes over.
    """
    now = now or timezone.now()
    earliest = first_zkill_check(killmail_time, now=now)
    if not result.requested:
        wait = timedelta(seconds=result.retry_after or 0)
        return ZkillSchedule(status, attempts, max(earliest, now + wait))

    attempts += 1
    if result.has_value:
        return ZkillSchedule("ok", attempts, None)
    if attempts >= ZKILL_MAX_ATTEMPTS:
        return ZkillSchedule("unavailable", attempts, None)
    if result.status in ("ok", "missing"):
        status = "missing"
    delay = ZKILL_RETRY_DELAYS[attempts - 1]
    if result.retry_after:
        delay = max(delay, timedelta(seconds=result.retry_after))
    return ZkillSchedule(status, attempts, max(earliest, now + delay))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _quantity(item: dict) -> int:
    destroyed = item.get("quantity_destroyed")
    dropped = item.get("quantity_dropped")
    if destroyed is None and dropped is None:
        return 1
    return sum(value for value in (destroyed, dropped) if _is_int(value) and value > 0)


def _price(prices, type_id) -> Decimal | None:
    return prices.get(type_id) if _is_int(type_id) else None


def estimate_value(ship_type_id: int, items, prices) -> Decimal | None:
    """Ship plus fitted modules, drones, fighters and implants at ``prices``; cargo is left out.

    Contents of fitted items count too, blueprint copies count as 0. Returns None when the
    ship itself has no price.
    """
    ship_price = _price(prices, ship_type_id)
    if ship_price is None:
        return None
    total = Decimal(ship_price)
    stack = [
        item
        for item in items or ()
        if isinstance(item, dict) and _is_int(item.get("flag")) and item["flag"] in FITTED_FLAGS
    ]
    counted = 0
    while stack and counted < MAX_ESTIMATE_ITEMS:
        item = stack.pop()
        counted += 1
        if item.get("singleton") != BLUEPRINT_COPY:
            price = _price(prices, item.get("item_type_id"))
            if price is not None:
                total += price * _quantity(item)
        nested = item.get("items")
        if isinstance(nested, list):
            stack.extend(child for child in nested if isinstance(child, dict))
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def market_estimate(ship_type_id: int, items, *, prices=None) -> Decimal | None:
    """``estimate_value`` at ESI market prices; None when ESI cannot provide them."""
    if prices is None:
        try:
            prices = get_market_prices()
        except FleetESIError:
            return None
    return estimate_value(ship_type_id, items, prices)
