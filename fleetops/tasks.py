# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from datetime import timedelta

from allianceauth.services.hooks import get_extension_logger
from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from fleetops import texts
from fleetops.models import FleetOperation, FleetOpsSettings
from fleetops.providers.esi import FleetESIError
from fleetops.services import fleet_jobs
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.fleet_access import FleetManageError
from fleetops.services.fleet_refresh import REFRESH_TASK_LOCK_SECONDS, refresh_now, refresh_task_lock_key
from fleetops.services.names import apply_names_to_operation, resolve_and_store
from fleetops.services.operations import cancel_abandoned_starts, end_fleet, finish_requested_end
from fleetops.services.tracking import observe_unreadable_fleet, sync_operation, unreadable_fleet_message
from fleetops.srp.killmails import prune_unclaimed
from fleetops.srp.tasks import queue_srp_sweep, refresh_srp_values as sweep_srp_values

logger = get_extension_logger(__name__)

# The beat remembers when it queued each poll and paces the next one from that time, so a worker
# that picks polls up late does not stretch the interval. Only a successful sync sets
# last_esi_update; it paces the polls only when the cache lost the queue time.
POLL_ATTEMPT_TIMEOUT = 24 * 60 * 60
# Beats do not run exactly on the second, so a poll queued a little less than one interval ago
# is due. Must stay below the 30 s minimum interval.
POLL_SLACK_SECONDS = 10
# Tracking audit entries keep the stored short error text, which never holds an exception message.
TRACKING_AUDIT_MESSAGE_LENGTH = 500
# A fleet still Ending after this long lost its end task (worker restart); the beat finishes it.
ENDING_RECOVERY_AFTER = timedelta(minutes=5)
# A worker killed during the end task leaves its lock behind, so the lock must expire before the
# recovery is due. A slow end that outlives its lock is harmless: end_fleet re-checks the locked
# row before each phase, so a second run never closes the fleet twice.
END_LOCK_TIMEOUT = 4 * 60
# Kick batches queued by earlier versions retry this long while the fleet runs another job.
KICK_LOCK_TIMEOUT = 10 * 60
KICK_RETRY_SECONDS = 30
# One SRP value sweep at a time; a worker killed during a sweep leaves the lock behind until it expires.
SRP_VALUES_LOCK_KEY = "fleetops:srp:values"
SRP_VALUES_LOCK_TIMEOUT = 10 * 60
# Killmails that were looked up for a claim check but never claimed are deleted after this many days.
SRP_UNCLAIMED_KILLMAIL_DAYS = 7


def _poll_attempt_key(operation_id):
    return f"fleetops:track:attempt:{operation_id}"


def _poll_queued_key(operation_id):
    # Set while a queued poll waits for a worker, so a backlog never gets a second poll queued.
    return f"fleetops:track:queued:{operation_id}"


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
    keys = [_poll_attempt_key(operation.pk) for operation in operations]
    keys += [_poll_queued_key(operation.pk) for operation in operations]
    cached = cache.get_many(keys)
    queued = 0
    for operation in operations:
        if _poll_queued_key(operation.pk) in cached:
            continue
        last_poll = cached.get(_poll_attempt_key(operation.pk), operation.last_esi_update)
        if last_poll is not None:
            age = (now - last_poll).total_seconds()
            if age < settings.tracking_interval - POLL_SLACK_SECONDS:
                continue
        cache.set(_poll_attempt_key(operation.pk), now, timeout=POLL_ATTEMPT_TIMEOUT)
        cache.set(_poll_queued_key(operation.pk), now, timeout=2 * settings.tracking_interval)
        track_operation.delay(operation.pk)
        queued += 1
    _recover_stuck_ends(now)
    cancel_abandoned_starts(now=now)
    _queue_srp_sweep(now)
    return queued


def _queue_srp_sweep(now):
    """Queue the SRP value sweep; a problem there must never stop the fleet tracking."""
    try:
        queue_srp_sweep(now)
    except Exception as exc:
        log_failure(logger, "SRP value sweep", exc)


def _recover_stuck_ends(now):
    cutoff = now - ENDING_RECOVERY_AFTER
    stuck = FleetOperation.objects.filter(status=FleetOperation.Status.ENDING).filter(
        Q(end_requested_at__lt=cutoff) | Q(end_requested_at=None, updated_at__lt=cutoff)
    )
    for operation_id in stuck.values_list("pk", flat=True):
        finish_fleet_end.delay(operation_id)


@shared_task(bind=True, autoretry_for=(), max_retries=0)
def track_operation(self, operation_id):
    cache.delete(_poll_queued_key(operation_id))
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
        # A poll the beat did not queue paces the next one too, but never moves the beat's queue time.
        cache.add(_poll_attempt_key(operation.pk), timezone.now(), timeout=POLL_ATTEMPT_TIMEOUT)
        previous_error = operation.last_error
        try:
            count = sync_operation(operation)
        except FleetESIError as exc:
            if exc.code == "FLEET_NOT_FOUND":
                return _unreadable_fleet(operation, settings, had_error=bool(previous_error))
            error = str(exc)[:4000]
            if not _save_if_active(operation, last_error=error):
                return "inactive"
            _audit_tracking_error(operation, bool(previous_error), exc.code, error)
            return exc.code
        except Exception as exc:
            error = report_failure(logger, "Fleet tracking", exc, operation)[:4000]
            if not _save_if_active(operation, last_error=error):
                return "inactive"
            _audit_tracking_error(operation, bool(previous_error), "ERROR", error)
            return "error"
        # None means the fleet was ended while its members were being read.
        if count is None:
            return "inactive"
        if previous_error:
            _audit_tracking_recovered(operation, previous_error, count)
        return count
    finally:
        cache.delete(lock_key)


def _audit_tracking_error(operation, had_error, code, message):
    """Audit the first failed poll after a good one; repeated failures add no entries."""
    if had_error:
        return
    audit(
        None,
        "fleet.tracking_error",
        operation,
        None,
        {"code": code, "message": message[:TRACKING_AUDIT_MESSAGE_LENGTH]},
    )


def _audit_tracking_recovered(operation, previous_error, count):
    """Audit the first good poll after failures."""
    audit(
        None,
        "fleet.tracking_recovered",
        operation,
        {"last_error": previous_error[:TRACKING_AUDIT_MESSAGE_LENGTH]},
        {"members": count},
    )


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


def _unreadable_fleet(operation, settings, *, had_error=False):
    """Count a poll whose fleet read got a 404 and end the fleet once enough polls in a row did."""
    observation = observe_unreadable_fleet(operation)
    if observation is None:
        # Whether the fleet is gone could not be checked, so this poll proves nothing.
        message = texts.CHECK_NOT_COUNTED()
        if not _save_if_active(operation, last_error=message):
            return "inactive"
        _audit_tracking_error(operation, had_error, "FLEET_CHECK_FAILED", message)
        return "FLEET_CHECK_FAILED"
    reason, detail = observation
    fields = {}
    if observation.boss_id:
        # The label is shown; the id keeps who holds the boss role now.
        fields["fleet_boss_character_id"] = observation.boss_id
    counted = _save_if_active(
        operation,
        fleet_missing_count=operation.fleet_missing_count + 1,
        missing_reason=reason,
        missing_detail=detail[:255],
        last_error=unreadable_fleet_message(reason, detail),
        **fields,
    )
    if not counted:
        return "inactive"
    code = "BOSS_HANDOVER" if reason == FleetOperation.EndReason.BOSS_HANDOVER else "FLEET_NOT_FOUND"
    _audit_tracking_error(operation, had_error, code, operation.last_error)
    if not settings.auto_end_enabled or operation.fleet_missing_count < settings.auto_end_missing_count:
        return code
    # One more read before ending: if the fleet can be read again the end is called off.
    error = operation.last_error
    try:
        count = sync_operation(operation)
    except FleetESIError:
        pass
    except Exception as exc:
        report_failure(logger, "Fleet tracking", exc, operation)
    else:
        if count is None:
            return "inactive"
        _audit_tracking_recovered(operation, error, count)
        return "auto-end-cancelled"
    end_fleet(operation, automatic=True, reason=reason, detail=detail)
    return "auto-ended"


@shared_task
def finish_fleet_end(operation_id):
    """Final sync, PAP multiplier and close for a fleet whose end was requested."""
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


@shared_task(bind=True, max_retries=KICK_LOCK_TIMEOUT // KICK_RETRY_SECONDS)
def kick_members(self, operation_id, character_ids, actor_id, purpose, context=None):
    """Kick a batch of fleet members queued by an earlier version.

    New batches are fleet jobs (``run_fleet_job``); this one runs as a job too, so it never overlaps
    another job or a single change of the fleet. While the fleet is busy it retries.
    """
    # Imported here: the kick service imports this module.
    from fleetops.services.fleet_controls import kick_job_label, kick_job_payload

    operation = FleetOperation.objects.filter(pk=operation_id).first()
    if operation is None:
        return "missing"
    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    try:
        job = fleet_jobs.claim_job(
            operation,
            kind="kick",
            label=kick_job_label(purpose, context or {}, len(character_ids)),
            actor=actor,
            total=len(character_ids),
        )
    except FleetManageError:
        raise self.retry(countdown=KICK_RETRY_SECONDS)
    payload = kick_job_payload(character_ids, purpose, context or {})
    return fleet_jobs.run_job(operation_id, job["id"], "kick", payload, actor_id)


@shared_task
def refresh_fleet_structure(operation_id, not_before=None):
    """Read an active fleet from ESI past the response cache, after a change or for a watched Fleet tab.

    ``not_before`` (a Unix timestamp) is when the read was due: a fleet read since then, for example
    by an earlier refresh that waited for the same busy worker, is not read again.
    """
    lock_key = refresh_task_lock_key(operation_id)
    if not cache.add(lock_key, "1", timeout=REFRESH_TASK_LOCK_SECONDS):
        return "already-running"
    try:
        operation = FleetOperation.objects.select_related("fc_user").filter(pk=operation_id).first()
        if operation is None:
            return "missing"
        if operation.status != FleetOperation.Status.ACTIVE:
            return "inactive"
        last_read = operation.last_esi_update
        if not_before is not None and last_read is not None and last_read.timestamp() >= float(not_before):
            return "fresh"
        ok, _message = refresh_now(operation, min_interval=0)
        return "refreshed" if ok else "failed"
    finally:
        cache.delete(lock_key)


@shared_task(autoretry_for=(), max_retries=0)
def run_fleet_job(operation_id, job_id, kind, payload, actor_id):
    """Run a fleet job queued by ``fleet_jobs.start_job`` (structure template, bulk move, invite, kicks)."""
    return fleet_jobs.run_job(operation_id, job_id, kind, payload, actor_id)


@shared_task(autoretry_for=(), max_retries=0)
def resolve_character_names(operation_id, character_ids):
    """Look up members without an Alliance Auth account and put their names on the fleet.

    Never raises: a failed lookup is logged and a later poll queues the next one.
    """
    try:
        resolve_and_store(character_ids)
    except FleetESIError:
        # The provider has logged the failure; whatever was stored before it is still applied.
        pass
    except Exception as exc:
        log_failure(logger, "Name lookup", exc)
    try:
        return apply_names_to_operation(operation_id, character_ids)
    except Exception as exc:
        log_failure(logger, "Name update", exc)
        return "error"


@shared_task
def refresh_srp_values():
    """Look up the zKillboard values that SRP claims are waiting for (queued by the minute beat)."""
    if not cache.add(SRP_VALUES_LOCK_KEY, "1", timeout=SRP_VALUES_LOCK_TIMEOUT):
        return "already-running"
    try:
        return sweep_srp_values()
    except Exception as exc:
        log_failure(logger, "SRP value sweep", exc)
        return "error"
    finally:
        cache.delete(SRP_VALUES_LOCK_KEY)


@shared_task
def prune_attendance_history():
    """Apply configured retention and remove PAP for departed alliance members.

    Also deletes the killmails that SRP read but nobody claimed.
    """
    from fleetops.services.history import prune_history

    result = prune_history(dry_run=False)
    try:
        prune_unclaimed(SRP_UNCLAIMED_KILLMAIL_DAYS)
    except Exception as exc:
        log_failure(logger, "SRP killmail cleanup", exc)
    return result
