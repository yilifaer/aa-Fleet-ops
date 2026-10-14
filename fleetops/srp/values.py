"""Loss values: which value a claim uses, when zKillboard is asked again, and the ESI estimate."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from fleetops.models import SrpKillmail
from fleetops.providers import zkillboard
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

# A lookup that has started keeps the killmail away from a second one (web request and sweep) this long.
ZKILL_LEASE = timedelta(minutes=2)
# An estimate that could not be made because ESI had no market prices is tried again for a while.
ESTIMATE_RETRY_DELAY = timedelta(hours=1)
ESTIMATE_RETRY_WINDOW = timedelta(days=2)

ZkillStatus = SrpKillmail.ZkillStatus
# Statuses that still wait for a zKillboard answer.
WAITING_STATUSES = (ZkillStatus.PENDING, ZkillStatus.MISSING)
# Statuses after which the value is settled: found, or never coming from zKillboard.
SETTLED_STATUSES = (ZkillStatus.OK, ZkillStatus.UNAVAILABLE, ZkillStatus.DISABLED)


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


def _signature(km) -> tuple:
    """What a claim's value depends on: the values and whether they are settled."""
    settled = km.zkill_status if km.zkill_status in SETTLED_STATUSES else None
    return km.zkill_fitted_value, km.zkill_total_value, km.esi_estimate, settled


def _compute_estimate(km) -> tuple[Decimal | None, bool]:
    """The ESI market estimate of the killmail and whether a failed estimate is worth trying again.

    A ship ESI has no price for is not retried; missing market prices (an ESI error) are.
    """
    try:
        prices = get_market_prices()
    except FleetESIError:
        return None, True
    return estimate_value(km.ship_type_id, km.items, prices), False


def _settle(km, now: datetime) -> set[str]:
    """Make the estimate for a killmail that gets no zKillboard value; returns the fields changed.

    ``zkill_next_attempt_at`` is the retry time for an estimate that failed because of ESI, else empty.
    """
    fields = {"zkill_next_attempt_at"}
    km.zkill_next_attempt_at = None
    if km.esi_estimate is not None:
        return fields
    estimate, retry = _compute_estimate(km)
    if estimate is not None:
        km.esi_estimate, km.esi_estimate_at = estimate, now
        fields |= {"esi_estimate", "esi_estimate_at"}
    elif retry and now - km.fetched_at < ESTIMATE_RETRY_WINDOW:
        km.zkill_next_attempt_at = now + ESTIMATE_RETRY_DELAY
    return fields


def apply_zkill_result(km, result: ZkillResult, *, now: datetime | None = None) -> bool:
    """Store a zKillboard answer on the killmail and schedule the next lookup.

    After the last attempt without a value the status becomes ``unavailable`` and the ESI estimate is made.
    Returns True when the value claims depend on changed.
    """
    now = now or timezone.now()
    before = _signature(km)
    schedule = after_zkill_lookup(
        result,
        attempts=km.zkill_attempts,
        status=km.zkill_status,
        killmail_time=km.killmail_time,
        now=now,
    )
    fields = {"zkill_status", "zkill_attempts", "zkill_next_attempt_at"}
    if result.requested:
        km.zkill_checked_at = now
        fields.add("zkill_checked_at")
    if result.ok:
        # A value zKillboard once gave is never replaced by a later answer without it.
        for field, value in (
            ("zkill_fitted_value", result.fitted_value),
            ("zkill_total_value", result.total_value),
            ("zkill_destroyed_value", result.destroyed_value),
            ("zkill_dropped_value", result.dropped_value),
            ("zkill_npc", result.npc),
            ("zkill_solo", result.solo),
            ("zkill_awox", result.awox),
        ):
            if value is not None:
                setattr(km, field, value)
                fields.add(field)
        km.zkill_labels = result.labels
        fields.add("zkill_labels")
    km.zkill_status = schedule.status
    km.zkill_attempts = schedule.attempts
    km.zkill_next_attempt_at = schedule.next_attempt_at
    if schedule.status == ZkillStatus.UNAVAILABLE:
        fields |= _settle(km, now)
    km.save(update_fields=sorted(fields))
    return _signature(km) != before


def refresh_zkill(km, *, now: datetime | None = None) -> bool:
    """Ask zKillboard once for the killmail and apply the result and the retry schedule; True when the
    stored values changed.

    Does nothing for a killmail that is not waiting for zKillboard or that another lookup has just taken.
    """
    if km.zkill_status not in WAITING_STATUSES:
        return False
    now = now or timezone.now()
    # Take the killmail with one conditional update, so the web request and the sweep never both ask.
    taken = SrpKillmail.objects.filter(
        pk=km.pk, zkill_status=km.zkill_status, zkill_next_attempt_at=km.zkill_next_attempt_at
    ).update(zkill_next_attempt_at=now + ZKILL_LEASE)
    if not taken:
        return False
    km.zkill_next_attempt_at = now + ZKILL_LEASE
    result = zkillboard.fetch_kill(km.killmail_id, killmail_time=km.killmail_time, now=now)
    return apply_zkill_result(km, result, now=now)


def settle_without_zkill(km, status: str, *, now: datetime | None = None) -> bool:
    """Give up on zKillboard for the killmail (``disabled`` or ``unavailable``) and make the ESI estimate.

    Also the retry of an estimate that failed before. Returns True when the value claims depend on changed.
    """
    now = now or timezone.now()
    before = _signature(km)
    km.zkill_status = status
    fields = {"zkill_status"} | _settle(km, now)
    km.save(update_fields=sorted(fields))
    return _signature(km) != before


def esi_estimate(km) -> Decimal | None:
    """Estimate the loss at ESI market prices, store it on the killmail and return it.

    Returns None, and keeps what is stored, when ESI has no prices or no price for the ship.
    """
    estimate, _retry = _compute_estimate(km)
    if estimate is None:
        return None
    km.esi_estimate = estimate
    km.esi_estimate_at = timezone.now()
    km.save(update_fields=["esi_estimate", "esi_estimate_at"])
    return estimate
