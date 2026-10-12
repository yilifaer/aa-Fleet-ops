from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from celery import shared_task
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings
from fleetops.providers.esi import FleetESIError
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.operations import cancel_abandoned_starts, end_fleet, finish_requested_end
from fleetops.services.tracking import observe_unreadable_fleet, sync_operation, unreadable_fleet_message

logger = get_extension_logger(__name__)

# Only a successful sync sets last_esi_update, so the scheduler also remembers each poll attempt.
POLL_ATTEMPT_TIMEOUT = 24 * 60 * 60
# Polls are recorded by the worker, a little after the beat that queued them. Without some
# slack the next beat finds the poll just short of the interval and skips it, so a 60 s
# interval would poll every 120 s. Must stay below the 30 s minimum interval.
POLL_SLACK_SECONDS = 10
# A fleet still Ending after this long lost its end task (worker restart); the beat finishes it.
ENDING_RECOVERY_AFTER = timedelta(minutes=5)
# A worker killed during the end task leaves its lock behind, so the lock must expire before the
# recovery is due. A slow end that outlives its lock is harmless: end_fleet re-checks the locked
# row before each phase, so a second run never closes the fleet twice.
END_LOCK_TIMEOUT = 4 * 60


def _poll_attempt_key(operation_id):
    return f"fleetops:track:attempt:{operation_id}"


@shared_task
def schedule_active_fleet_tracking():
    """Fan out one task per active fleet when its configured interval is due.

    Also finishes ends whose task was lost and cancels starts that were interrupted.
    """
    settings = FleetOpsSettings.get_solo()
    now = timezone.now()
    operations = list(
        FleetOperation.objects.filter(
            status=FleetOperation.Status.ACTIVE,
            tracking_enabled=True,
        ).only("pk", "last_esi_update")
    )
    attempts = cache.get_many([_poll_attempt_key(operation.pk) for operation in operations])
    queued = 0
    for operation in operations:
        polls = [operation.last_esi_update, attempts.get(_poll_attempt_key(operation.pk))]
        last_poll = max((poll for poll in polls if poll is not None), default=None)
        if last_poll is not None:
            age = (now - last_poll).total_seconds()
            if age < settings.tracking_interval - POLL_SLACK_SECONDS:
                continue
        track_operation.delay(operation.pk)
        queued += 1
    _recover_stuck_ends(now)
    cancel_abandoned_starts(now=now)
    return queued


def _recover_stuck_ends(now):
    cutoff = now - ENDING_RECOVERY_AFTER
    stuck = FleetOperation.objects.filter(status=FleetOperation.Status.ENDING).filter(
        Q(end_requested_at__lt=cutoff) | Q(end_requested_at=None, updated_at__lt=cutoff)
    )
    for operation_id in stuck.values_list("pk", flat=True):
        finish_fleet_end.delay(operation_id)


@shared_task(bind=True, autoretry_for=(), max_retries=0)
def track_operation(self, operation_id):
    settings = FleetOpsSettings.get_solo()
    lock_timeout = max(55, min(settings.tracking_interval, 300))
    lock_key = f"fleetops:track:{operation_id}"
    if not cache.add(lock_key, "1", timeout=lock_timeout):
        return "already-running"
    try:
        operation = FleetOperation.objects.select_related("fc_user", "fleet_type").filter(pk=operation_id).first()
        if operation is None:
            return "missing"
        if operation.status != FleetOperation.Status.ACTIVE or not operation.tracking_enabled:
            return "inactive"
        cache.set(_poll_attempt_key(operation.pk), timezone.now(), timeout=POLL_ATTEMPT_TIMEOUT)
        try:
            count = sync_operation(operation)
        except FleetESIError as exc:
            if exc.code == "FLEET_NOT_FOUND":
                return _unreadable_fleet(operation, settings)
            return exc.code if _save_if_active(operation, last_error=str(exc)[:4000]) else "inactive"
        except Exception as exc:
            error = report_failure(logger, "Fleet tracking", exc, operation)[:4000]
            return "error" if _save_if_active(operation, last_error=error) else "inactive"
        # None means the fleet was ended while its members were being read.
        return "inactive" if count is None else count
    finally:
        cache.delete(lock_key)


def _save_if_active(operation, **fields):
    """Store the result of a poll unless the fleet stopped being Active meanwhile.

    The fleet may have been ended from its page while ESI was read; a closed fleet keeps the
    state its end left. Returns False when nothing was saved.
    """
    for name, value in fields.items():
        setattr(operation, name, value)
    saved = FleetOperation.objects.filter(pk=operation.pk, status=FleetOperation.Status.ACTIVE).update(
        updated_at=timezone.now(), **fields
    )
    return saved > 0


def _unreadable_fleet(operation, settings):
    """Count a poll whose fleet read got a 404 and end the fleet once enough polls in a row did."""
    observation = observe_unreadable_fleet(operation)
    if observation is None:
        # Whether the fleet is gone could not be checked, so this poll proves nothing.
        message = "The fleet could not be read and the fleet boss check failed; this check was not counted."
        return "FLEET_CHECK_FAILED" if _save_if_active(operation, last_error=message) else "inactive"
    reason, detail = observation
    counted = _save_if_active(
        operation,
        fleet_missing_count=operation.fleet_missing_count + 1,
        missing_reason=reason,
        missing_detail=detail[:255],
        last_error=unreadable_fleet_message(reason, detail),
    )
    if not counted:
        return "inactive"
    code = "BOSS_HANDOVER" if reason == FleetOperation.EndReason.BOSS_HANDOVER else "FLEET_NOT_FOUND"
    if not settings.auto_end_enabled or operation.fleet_missing_count < settings.auto_end_missing_count:
        return code
    # One more read before ending: if the fleet can be read again the end is called off.
    try:
        count = sync_operation(operation)
    except FleetESIError:
        pass
    except Exception as exc:
        report_failure(logger, "Fleet tracking", exc, operation)
    else:
        return "inactive" if count is None else "auto-end-cancelled"
    end_fleet(operation, automatic=True, reason=reason, detail=detail)
    return "auto-ended"


@shared_task
def finish_fleet_end(operation_id):
    """Final sync, attendance multiplier and close for a fleet whose end was requested."""
    lock_key = f"fleetops:end:{operation_id}"
    if not cache.add(lock_key, "1", timeout=END_LOCK_TIMEOUT):
        return "already-running"
    try:
        operation = FleetOperation.objects.select_related("fc_user", "fleet_type").filter(pk=operation_id).first()
        if operation is None:
            return "missing"
        if operation.status != FleetOperation.Status.ENDING:
            return "not-ending"
        try:
            finish_requested_end(operation)
        except Exception as exc:
            # The fleet stays Ending and the beat tries again later.
            log_failure(logger, "Fleet end", exc, operation)
            return "error"
        return "closed"
    finally:
        cache.delete(lock_key)


@shared_task
def prune_attendance_history():
    """Apply configured retention and remove attendance for departed alliance members."""
    from fleetops.services.history import prune_history

    return prune_history(dry_run=False)
