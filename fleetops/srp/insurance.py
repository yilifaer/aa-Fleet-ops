# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The insurance estimate shown next to SRP amounts (REV-10).

ESI publishes the insurance prices of every ship type. FleetOps keeps them in ``SrpInsurancePrice`` (replaced as a
whole, at most once a day, only while the estimate is shown) and shows "compensation + net insurance payout = total"
for the level the administrator chose. The estimate is a hint: it is not part of any amount, limit, export or payment.
"""

from decimal import ROUND_HALF_UP, Decimal

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from fleetops.models import INSURANCE_LEVELS, SrpInsurancePrice, SrpSettings
from fleetops.services.errors import log_failure

logger = get_extension_logger(__name__)

REFRESH_KEY = "fleetops:srp:insurance"
REFRESH_SECONDS = 24 * 60 * 60
# After a failed read the next try is made this long after.
RETRY_SECONDS = 60 * 60
BATCH = 1000


def refresh_active(srp=None) -> bool:
    """Whether the prices are kept up to date: SRP is enabled and the estimate is shown."""
    srp = srp or SrpSettings.get_solo()
    return bool(srp.enabled and srp.show_insurance)


def refresh_prices(now=None) -> int:
    """Read the prices from ESI and replace the stored ones in one transaction. Returns the number of ship types stored.

    The old rows stay when the read fails (``FleetESIError`` is raised before anything is deleted) or returns nothing.
    """
    from fleetops.providers import esi_killmails

    prices = esi_killmails.get_insurance_prices()
    if not prices:
        logger.warning("FleetOps: ESI returned no insurance prices; the stored ones are kept.")
        return 0
    when = now or timezone.now()
    rows = [
        SrpInsurancePrice(
            ship_type_id=type_id,
            levels={name: {"cost": str(cost), "payout": str(payout)} for name, (cost, payout) in levels.items()},
            fetched_at=when,
        )
        for type_id, levels in prices.items()
    ]
    with transaction.atomic():
        SrpInsurancePrice.objects.all().delete()
        SrpInsurancePrice.objects.bulk_create(rows, batch_size=BATCH)
    return len(rows)


def retry_soon() -> None:
    """After a failed read: try again in an hour instead of waiting for the next day."""
    try:
        cache.set(REFRESH_KEY, "1", RETRY_SECONDS)
    except Exception as exc:
        log_failure(logger, "SRP insurance retry", exc)


def queue_refresh(now=None) -> bool:
    """Queue ``fleetops.tasks.refresh_srp_insurance`` unless one was queued in the last 24 hours (or the last try
    failed less than an hour ago). Called by the minute beat; does nothing while SRP or the estimate is off. A broker
    problem is logged and ignored. Returns whether a task was queued."""
    from fleetops import tasks

    if not refresh_active():
        return False
    try:
        if not cache.add(REFRESH_KEY, "1", timeout=REFRESH_SECONDS):
            return False
        try:
            tasks.refresh_srp_insurance.delay()
        except Exception:
            cache.delete(REFRESH_KEY)
            raise
    except Exception as exc:
        log_failure(logger, "Queueing the SRP insurance read", exc)
        return False
    return True


def net_payout(ship_type_id, level):
    """Payout minus cost of the level for the ship type as whole ISK, or None without data for it."""
    row = SrpInsurancePrice.objects.filter(pk=ship_type_id).first()
    return _net(row, level)


def _net(row, level):
    if row is None or level not in INSURANCE_LEVELS:
        return None
    levels = row.levels if isinstance(row.levels, dict) else {}
    values = levels.get(level)
    if not isinstance(values, dict):
        return None
    try:
        net = Decimal(str(values["payout"])) - Decimal(str(values["cost"]))
    except (KeyError, ArithmeticError, ValueError):
        return None
    if not net.is_finite():
        return None
    return int(net.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def estimate(ship_type_id, compensation, srp=None):
    """The line shown next to an amount: ``{"compensation", "insurance", "total", "level"}`` or None while the estimate
    is switched off. ``insurance`` is None (shown as a dash) without data for the ship type; the total is then the
    compensation. ``compensation`` is the approved amount of an approved or paid claim, else the suggested amount. One
    query. Never used in an amount."""
    srp = srp or SrpSettings.get_solo()
    if not srp.show_insurance:
        return None
    compensation = int(compensation or 0)
    net = net_payout(ship_type_id, srp.insurance_level)
    return {
        "compensation": compensation,
        "insurance": net,
        "total": compensation + (net or 0),
        "level": srp.insurance_level,
    }
