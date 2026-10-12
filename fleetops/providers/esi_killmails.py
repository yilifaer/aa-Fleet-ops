"""Public ESI reads used by SRP: killmails and market prices.

Both calls go through the shared client in ``fleetops.providers.esi``; its ``operations``
list must contain ``KILLMAIL_OPERATIONS``. Neither call needs a token.
"""

import logging
from decimal import Decimal
from importlib import import_module

import requests
from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException

from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError, _read, _status_code

logger = logging.getLogger(__name__)

KILLMAIL_OPERATIONS = ("GetKillmailsKillmailIdKillmailHash", "GetMarketsPrices")
NOT_FOUND_STATUSES = (404, 422)
KILLMAIL_NOT_FOUND = "Killmail not found. Check the link or the hash."


def _http_timeouts():
    errors = [TimeoutError, requests.Timeout]
    # django-esi 9 talks to ESI through httpx, django-esi 10 through httpx2.
    for name in ("httpx", "httpx2"):
        try:
            errors.append(import_module(name).TimeoutException)
        except (ImportError, AttributeError):
            continue
    return tuple(errors)


TIMEOUT_ERRORS = _http_timeouts()


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen and len(seen) < 5:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _describe(exc, status: int | None) -> str:
    # django-esi exception texts carry every response header, so they are never shown.
    chain = _chain(exc)
    if status == 420 or any(isinstance(item, ESIErrorLimitException) for item in chain):
        return "ESI error limit reached; try again in a minute."
    if any(isinstance(item, ESIBucketLimitException) for item in chain):
        return "ESI rate limit reached; try again later."
    if status is not None:
        return f"ESI returned HTTP {status}."
    if any(isinstance(item, TIMEOUT_ERRORS) for item in chain):
        return "ESI did not respond in time."
    return f"Unexpected ESI error ({type(exc).__name__})."


def _failure(code: str, context: str, exc: Exception) -> FleetESIError:
    status = _status_code(exc)
    logger.warning(
        "FleetOps ESI call failed: %s (%s, HTTP status %s).",
        context,
        type(exc).__name__,
        status if status is not None else "none",
    )
    return FleetESIError(code, f"{context}: {_describe(exc, status)}", status)


def to_plain(value):
    """Nested dicts and lists from django-esi response models (datetimes are kept)."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif hasattr(value, "dict"):
        value = value.dict()
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


def get_killmail(killmail_id: int, killmail_hash: str) -> dict:
    """The ESI killmail as a plain dict; killmails never change, so cached answers are fine."""
    try:
        result = _read(
            esi_provider.esi.client.Killmails.GetKillmailsKillmailIdKillmailHash(
                killmail_id=int(killmail_id),
                killmail_hash=killmail_hash,
            )
        )
    except Exception as exc:  # django-esi raises its own HTTP exception classes
        status = _status_code(exc)
        if status in NOT_FOUND_STATUSES:
            raise FleetESIError("KILLMAIL_NOT_FOUND", KILLMAIL_NOT_FOUND, status) from exc
        raise _failure("ESI_ERROR", "Could not read the killmail", exc) from exc
    payload = to_plain(result)
    if not isinstance(payload, dict) or not payload:
        raise FleetESIError("ESI_ERROR", "Could not read the killmail: ESI returned an empty answer.")
    return payload


def _price(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    price = Decimal(str(value))
    return price if price.is_finite() and price >= 0 else None


def get_market_prices() -> dict[int, Decimal]:
    """CCP's average price per type id, else the adjusted price."""
    try:
        rows = _read(esi_provider.esi.client.Market.GetMarketsPrices())
    except Exception as exc:
        raise _failure("ESI_ERROR", "Could not read market prices", exc) from exc
    prices = {}
    for row in rows or ():
        row = to_plain(row)
        if not isinstance(row, dict):
            continue
        type_id = row.get("type_id")
        if isinstance(type_id, bool) or not isinstance(type_id, int):
            continue
        price = _price(row.get("average_price"))
        if price is None:
            price = _price(row.get("adjusted_price"))
        if price is not None:
            prices[type_id] = price
    return prices
