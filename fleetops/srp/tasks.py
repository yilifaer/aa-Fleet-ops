"""The SRP value sweep: zKillboard values for killmails that had none when they were claimed.

The Celery task is ``fleetops.tasks.refresh_srp_values``; this module holds what it runs and the beat's queueing.
"""

import time
from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from fleetops.models import SrpClaim, SrpKillmail, SrpSettings
from fleetops.providers import zkillboard
from fleetops.services.errors import log_failure
from fleetops.srp import values

logger = get_extension_logger(__name__)

SWEEP_KEY = "fleetops:srp:sweep"
SWEEP_SECONDS = 5 * 60
BATCH_SIZE = 20
# A killmail whose lookup failed unexpectedly is left alone this long, so it cannot block the others.
ERROR_DELAY = timedelta(minutes=15)


def due_killmails(now):
    """Killmails the sweep has work for: waiting for zKillboard, or without an estimate that is still
    wanted, whose time has come; oldest first."""
    status = SrpKillmail.ZkillStatus
    return SrpKillmail.objects.filter(zkill_next_attempt_at__lte=now).filter(
        Q(zkill_status__in=values.WAITING_STATUSES)
        | Q(zkill_status__in=(status.UNAVAILABLE, status.DISABLED), esi_estimate__isnull=True)
    ).order_by("zkill_next_attempt_at", "pk")


def _refresh_one(km, zkill_enabled: bool, when):
    """Handle one due killmail; returns whether the value changed and whether zKillboard was asked."""
    status = values.ZkillStatus
    if km.zkill_status not in values.WAITING_STATUSES:
        # zKillboard gave up on it or is off, and the estimate failed before: try the estimate again.
        return values.settle_without_zkill(km, km.zkill_status, now=when), False
    if not zkill_enabled:
        return values.settle_without_zkill(km, status.DISABLED, now=when), False
    checked = km.zkill_checked_at
    changed = values.refresh_zkill(km, now=when)
    return changed, km.zkill_checked_at != checked


def refresh_srp_values(now=None) -> int:
    """Look up up to 20 killmails whose zKillboard value is due (status pending or missing and
    ``zkill_next_attempt_at`` reached, oldest first), apply the result and update the claims waiting for it.

    Without zKillboard (switched off in the SRP settings) the ESI estimate is made instead. An estimate
    that failed before is tried again, too. Returns the number of killmails handled.
    """
    from fleetops.srp import claims

    rows = list(due_killmails(now or timezone.now())[:BATCH_SIZE])
    if not rows:
        return 0
    zkill_enabled = SrpSettings.get_solo().zkillboard_enabled
    handled = 0
    asked = False
    for km in rows:
        if asked:
            # zKillboard allows one request every two seconds; wait out the slot the last one took.
            time.sleep(zkillboard.SLOT_SECONDS)
        when = now or timezone.now()
        try:
            changed, asked = _refresh_one(km, zkill_enabled, when)
        except Exception as exc:
            log_failure(logger, "SRP loss value lookup", exc)
            SrpKillmail.objects.filter(pk=km.pk).update(zkill_next_attempt_at=when + ERROR_DELAY)
            asked = False
            continue
        handled += 1
        if changed and SrpClaim.objects.filter(killmail_id=km.pk).exists():
            try:
                claims.on_value_updated(km)
            except Exception as exc:  # one claim must not stop the others from getting their value
                log_failure(logger, "SRP claim update after a loss value lookup", exc)
    return handled


def queue_srp_sweep(now=None) -> bool:
    """Queue ``fleetops.tasks.refresh_srp_values`` unless a sweep was queued in the last 5 minutes.

    Called by the minute beat (which passes its clock; the interval is kept in the cache). A broker error
    is logged and ignored. Returns whether a sweep was queued.
    """
    # fleetops.tasks imports this module, so it can only be imported once the call is made.
    from fleetops import tasks

    try:
        if not cache.add(SWEEP_KEY, "1", timeout=SWEEP_SECONDS):
            return False
        try:
            tasks.refresh_srp_values.delay()
        except Exception:
            # Without a queued sweep the next beat may try again.
            cache.delete(SWEEP_KEY)
            raise
    except Exception as exc:
        log_failure(logger, "Queueing the SRP value sweep", exc)
        return False
    return True
