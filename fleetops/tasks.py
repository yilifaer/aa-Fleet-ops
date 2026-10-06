import logging

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from fleetops.models import FleetOperation, FleetOpsSettings
from fleetops.providers.esi import FleetESIError
from fleetops.services.errors import report_failure
from fleetops.services.operations import end_fleet
from fleetops.services.tracking import sync_operation

logger = logging.getLogger(__name__)

# Only a successful sync sets last_esi_update, so the scheduler also remembers each poll attempt.
POLL_ATTEMPT_TIMEOUT = 24 * 60 * 60
# Polls are recorded by the worker, a little after the beat that queued them. Without some
# slack the next beat finds the poll just short of the interval and skips it, so a 60 s
# interval would poll every 120 s. Must stay below the 30 s minimum interval.
POLL_SLACK_SECONDS = 10


def _poll_attempt_key(operation_id):
    return f"fleetops:track:attempt:{operation_id}"


@shared_task
def schedule_active_fleet_tracking():
    """Fan out one task per active fleet when its configured interval is due."""
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
    return queued


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
            return count
        except FleetESIError as exc:
            operation.last_error = str(exc)[:4000]
            if exc.code == "FLEET_NOT_FOUND":
                operation.fleet_missing_count += 1
                if settings.auto_end_enabled and operation.fleet_missing_count >= settings.auto_end_missing_count:
                    end_fleet(operation, automatic=True)
                    return "auto-ended"
            operation.save(update_fields=["last_error", "fleet_missing_count", "updated_at"])
            return exc.code
        except Exception as exc:
            operation.last_error = report_failure(logger, "Fleet tracking", exc, operation)[:4000]
            operation.save(update_fields=["last_error", "updated_at"])
            return "error"
    finally:
        cache.delete(lock_key)


@shared_task
def prune_attendance_history():
    """Apply configured retention and remove attendance for departed alliance members."""
    from fleetops.services.history import prune_history

    return prune_history(dry_run=False)
