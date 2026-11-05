# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ESI reads used by SRP: killmails, market prices and the recent killmails of a character.

The calls go through the shared client in ``fleetops.providers.esi``, whose ``operations`` list contains
``KILLMAIL_OPERATIONS`` and ``LOSS_OPERATIONS`` (defined there because this module imports that one). Only the
list of recent killmails needs a token, the one of the character it belongs to.
"""

from decimal import Decimal
from importlib import import_module

import requests
from allianceauth.services.hooks import get_extension_logger
from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException

from fleetops.providers import esi as esi_provider
from fleetops import texts
from fleetops.providers.esi import FleetESIError, _read, _status_code

logger = get_extension_logger(__name__)

KILLMAIL_OPERATIONS = esi_provider.KILLMAIL_OPERATIONS
NOT_FOUND_STATUSES = (404, 422)
KILLMAIL_NOT_FOUND = texts.KILLMAIL_NOT_FOUND()


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
        return texts.ESI_ERROR_LIMIT_MINUTE()
    if any(isinstance(item, ESIBucketLimitException) for item in chain):
        return texts.ESI_RATE_LIMIT()
    if status is not None:
        return texts.ESI_HTTP_PLAIN(status=status)
    if any(isinstance(item, TIMEOUT_ERRORS) for item in chain):
        return texts.ESI_TIMEOUT()
    return texts.ESI_UNEXPECTED(error_type=type(exc).__name__)


def _failure(code: str, failure, exc: Exception) -> FleetESIError:
    """The short error for a failed call; ``failure`` is the stored message that takes the ``detail``."""
    status = _status_code(exc)
    logger.warning(
        "FleetOps ESI call failed: %s (%s, HTTP status %s).",
        failure(detail="")[: -len(": ")],
        type(exc).__name__,
        status if status is not None else "none",
    )
    return FleetESIError(code, failure(detail=_describe(exc, status)), status)


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
        raise _failure("ESI_ERROR", texts.KILLMAIL_READ_FAILED, exc) from exc
    payload = to_plain(result)
    if not isinstance(payload, dict) or not payload:
        raise FleetESIError("ESI_ERROR", texts.KILLMAIL_EMPTY())
    return payload


class RecentKillmails(list):
    """``[(killmail_id, killmail_hash), ...]`` of one page, newest first; ``pages`` is the number of pages ESI said
    the list has (None when the answer did not say)."""

    pages: int | None = None


def is_rate_limited(exc) -> bool:
    """Whether a failed call hit the ESI error limit (420) or a rate-limit bucket."""
    return any(
        isinstance(item, (ESIErrorLimitException, ESIBucketLimitException)) or _status_code(item) == 420
        for item in _chain(exc)
    )


def _pages(response) -> int | None:
    try:
        value = int(response.headers.get("X-Pages"))
    except Exception:  # a cached answer may carry no headers
        return None
    return value if value > 0 else None


def get_recent_killmails(token, character_id: int, page: int = 1) -> RecentKillmails:
    """One page of the recent killmails (losses and kills) of the token's character, newest first.

    Needs ``esi-killmails.read_killmails.v1``. ESI refusing the token raises ``FleetESIError("LOSS_DENIED")``; the
    error limit and the rate-limit buckets raise ``FleetESIError("ESI_LIMITED")``; anything else
    ``FleetESIError("ESI_ERROR")``. django-esi keeps each page in its response cache for as long as ESI allows.
    """
    operation = esi_provider.esi.client.Killmails.GetCharactersCharacterIdKillmailsRecent(
        character_id=int(character_id), token=token, page=int(page)
    )
    try:
        result, response = esi_provider._read_with_response(operation)
    except Exception as exc:  # django-esi raises its own HTTP exception classes
        status = _status_code(exc)
        if status in (401, 403):
            logger.warning("FleetOps ESI call failed: recent losses refused (%s, HTTP status %s).", type(exc).__name__, status)
            raise FleetESIError("LOSS_DENIED", texts.LOSS_READ_DENIED(), status) from exc
        error = _failure("ESI_ERROR", texts.LOSS_LIST_FAILED, exc)
        if is_rate_limited(exc):
            error.code = "ESI_LIMITED"
        raise error from exc
    rows = RecentKillmails()
    for row in to_plain(result) or ():
        if not isinstance(row, dict):
            continue
        killmail_id, killmail_hash = row.get("killmail_id"), row.get("killmail_hash")
        if isinstance(killmail_id, bool) or not isinstance(killmail_id, int) or not isinstance(killmail_hash, str):
            continue
        rows.append((killmail_id, killmail_hash.lower()))
    rows.sort(key=lambda row: row[0], reverse=True)
    rows.pages = _pages(response)
    return rows


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
        raise _failure("ESI_ERROR", texts.MARKET_READ_FAILED, exc) from exc
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


def get_insurance_prices() -> dict[int, dict[str, tuple[Decimal, Decimal]]]:
    """``{type_id: {level: (cost, payout)}}`` from ``GET /insurance/prices``; the level names are asked for in English
    (``Accept-Language: en``) and returned in lower case. Rows and levels that are not what ESI documents are left out."""
    try:
        rows = _read(esi_provider.esi.client.Insurance.GetInsurancePrices(Accept_Language="en"))
    except Exception as exc:
        raise _failure("ESI_ERROR", texts.INSURANCE_READ_FAILED, exc) from exc
    prices = {}
    for row in to_plain(rows) or ():
        if not isinstance(row, dict):
            continue
        type_id = row.get("type_id")
        if isinstance(type_id, bool) or not isinstance(type_id, int):
            continue
        levels = {}
        for level in row.get("levels") or ():
            if not isinstance(level, dict) or not isinstance(level.get("name"), str):
                continue
            cost, payout = _price(level.get("cost")), _price(level.get("payout"))
            if cost is not None and payout is not None:
                levels[level["name"].strip().lower()] = (cost, payout)
        if levels:
            prices[type_id] = levels
    return prices
