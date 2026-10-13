"""Background jobs that change a fleet step by step, and the lock that keeps fleet changes apart.

Structure templates, bulk moves, bulk invites and big kick batches run as jobs in Celery, so web
requests stay short. One fleet runs at most one job at a time, and while it runs no single change
(rename, move, kick …) is sent: single changes hold ``fleet_write_lock``, jobs hold the job lock,
and each refuses while the other is held. Job progress lives in the Django cache, so the fleet page
can show it; a job whose worker died is reported as stopped after two minutes without a heartbeat.

A runner is ``runner(job, operation, payload, actor)``. It calls ``ensure_writable`` before every ESI
call, reports each call with ``step`` and raises ``JobStopped`` to end the job early, for example when
``stop_reason`` says a failed call ends it.
"""

import time
import uuid
from contextlib import contextmanager

from allianceauth.services.hooks import get_extension_logger
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils.module_loading import import_string
from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException
from kombu.exceptions import OperationalError as QueueError

from fleetops.models import FleetOperation
from fleetops.providers.esi import FleetESIError, describe_esi_failure
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure, report_failure
from fleetops.services.fleet_access import WRITE_BUSY, FleetManageError, job_wait_message, write_state
from fleetops.services.operations import set_action

logger = get_extension_logger(__name__)

JOB_LOCK_TIMEOUT = 15 * 60
JOB_TTL = 3600
# A running job reports at least this often; a queued one is picked up within QUEUE_STALE.
HEARTBEAT_STALE = 120
QUEUE_STALE = 5 * 60
# Finished jobs stay on the fleet page this long.
FINISHED_SHOWN = 5 * 60
MESSAGES_KEPT = 20
# Single changes: one ESI call, which takes about 35 seconds at worst while ESI fails.
WRITE_LOCK_TIMEOUT = 60

RUNNERS = {
    "template": "fleetops.services.fleet_templates.run_template_job",
    "move": "fleetops.services.fleet_manage.run_bulk_move",
    "invite": "fleetops.services.fleet_invites.run_bulk_invite",
    "kick": "fleetops.services.fleet_controls.run_kick_job",
}
# Kicks keep the operation steps and audit entries they had before they ran as jobs, and read the
# fleet again themselves.
SELF_RECORDING_KINDS = frozenset({"kick"})

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
ACTIVE_STATES = (QUEUED, RUNNING)

STOPPED_UNEXPECTEDLY = "The job stopped unexpectedly."
NOT_STARTED = "The background worker did not start the job."
WORKER_DOWN = "The background worker is not available. Try again later."
FLEET_MISSING = "The fleet no longer exists."
NOT_ALLOWED = "You can no longer change this fleet."
# Failed ESI calls after which a job stops instead of going on with its next step.
STOP_CODES = frozenset(
    {"FLEET_WRITE_FORBIDDEN", "FLEET_WRITE_LIMITED", "MISSING_WRITE_SCOPE", "MISSING_READ_SCOPE"}
)


class JobStopped(Exception):
    """Raised by a runner to end its job early; the message says why."""


def _key(operation_id, name):
    return f"fleetops:fleet:{operation_id}:{name}"


def _job_key(operation_id):
    return _key(operation_id, "job")


def _lock_key(operation_id):
    return _key(operation_id, "job_lock")


def _write_key(operation_id):
    return _key(operation_id, "write")


def _save(job) -> None:
    job["updated_at"] = time.time()
    cache.set(_job_key(job["operation_id"]), job, JOB_TTL)


def _release(operation_id, job_id) -> None:
    if cache.get(_lock_key(operation_id)) == job_id:
        cache.delete(_lock_key(operation_id))


def _add_message(job, ok, text) -> None:
    job["messages"] = [*job.get("messages", []), {"ok": bool(ok), "text": str(text)}][-MESSAGES_KEPT:]


def _finish(job, state, message="") -> None:
    if message:
        _add_message(job, False, message)
    job["state"] = state
    job["finished_at"] = time.time()
    _save(job)
    _release(job["operation_id"], job["id"])


def current_job(operation_id) -> dict | None:
    """The fleet's running job, or the last one for five minutes after it finished.

    ``{"id", "kind", "label", "state" (queued, running, done, failed), "total", "done", "failed",
    "messages": [{"ok", "text"}] (the last 20), "actor_id", "operation_id", "started_at",
    "updated_at", "finished_at"}``; times are Unix timestamps. A job without a heartbeat for two
    minutes (five while queued) is marked failed and its lock released.
    """
    job = cache.get(_job_key(operation_id))
    if not job:
        return None
    now = time.time()
    if job["state"] in ACTIVE_STATES:
        queued = job["state"] == QUEUED
        if now - job["updated_at"] > (QUEUE_STALE if queued else HEARTBEAT_STALE):
            _finish(job, FAILED, NOT_STARTED if queued else STOPPED_UNEXPECTEDLY)
        return job
    if now - (job.get("finished_at") or job["updated_at"]) > FINISHED_SHOWN:
        return None
    return job


def running_job(operation_id) -> dict | None:
    """The job that holds the fleet now, or None."""
    job = current_job(operation_id)
    if job is not None and job["state"] in ACTIVE_STATES:
        return job
    holder = cache.get(_lock_key(operation_id))
    if holder and (job is None or job["id"] != holder):
        # The cache lost the job's progress; its lock still holds the fleet until it expires.
        return {"id": holder, "label": "the running job", "state": RUNNING}
    return None


def claim_job(operation, *, kind, label, actor, total) -> dict:
    """Take the fleet's job lock and record a queued job; raises FleetManageError (409) when busy."""
    if kind not in RUNNERS:
        raise ValueError(f"Unknown fleet job kind {kind!r}")
    running = running_job(operation.pk)
    if running is not None:
        raise FleetManageError(job_wait_message(running), 409)
    job_id = uuid.uuid4().hex
    if not cache.add(_lock_key(operation.pk), job_id, JOB_LOCK_TIMEOUT):
        raise FleetManageError(job_wait_message(running_job(operation.pk) or {}), 409)
    if cache.get(_write_key(operation.pk)):
        _release(operation.pk, job_id)
        raise FleetManageError(WRITE_BUSY, 409)
    now = time.time()
    job = {
        "id": job_id,
        "kind": kind,
        "label": label,
        "state": QUEUED,
        "total": int(total),
        "done": 0,
        "failed": 0,
        "messages": [],
        "actor_id": getattr(actor, "pk", None),
        "operation_id": operation.pk,
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
    }
    _save(job)
    return job


def start_job(operation, *, kind, label, payload, actor, total) -> dict:
    """Queue a job; returns its state. Raises FleetManageError: 409 while busy, 503 without a worker queue.

    ``payload`` must be JSON-serialisable; it is handed to the runner as it is.
    """
    job = claim_job(operation, kind=kind, label=label, actor=actor, total=total)
    # Imported here: the tasks import this module.
    from fleetops.tasks import run_fleet_job

    try:
        run_fleet_job.delay(operation.pk, job["id"], kind, payload, getattr(actor, "pk", None))
    except QueueError as exc:
        log_failure(logger, "Queueing a fleet job", exc, operation)
        if (cache.get(_job_key(operation.pk)) or {}).get("id") == job["id"]:
            cache.delete(_job_key(operation.pk))
        _release(operation.pk, job["id"])
        raise FleetManageError(WORKER_DOWN, 503) from exc
    # A worker (or an eager test run) may have moved the job on already.
    stored = cache.get(_job_key(operation.pk))
    return stored if stored and stored.get("id") == job["id"] else job


def step(job, *, ok, message) -> None:
    """Record one step of a job (one ESI call): counters, message and heartbeat."""
    job["done" if ok else "failed"] += 1
    _add_message(job, ok, message)
    _save(job)


def touch(job) -> None:
    """Heartbeat for a runner that waits, so the job does not count as stopped."""
    _save(job)


def set_remaining(job, steps, *, message="") -> None:
    """Make the job's total the steps done plus ``steps`` still to come; ``message`` says why it changed."""
    job["total"] = job["done"] + job["failed"] + int(steps)
    if message:
        _add_message(job, True, message)
    _save(job)


def stop_reason(exc) -> str:
    """Why a failed ESI call ends the whole job; empty when the job may go on with its next step.

    Calls stop on a forbidden write, a missing token scope and ESI's error or rate limit.
    """
    if isinstance(exc, FleetESIError):
        return str(exc) if exc.code in STOP_CODES else ""
    if isinstance(exc, (ESIErrorLimitException, ESIBucketLimitException)):
        return describe_esi_failure(exc)
    return ""


def ensure_writable(job, operation, actor) -> None:
    """Check before an ESI call that ``actor`` may still change the fleet; raises JobStopped if not.

    Reloads the fleet's state first: it may have ended or changed boss while the job ran.
    """
    operation.refresh_from_db(
        fields=[
            "status",
            "esi_fleet_id",
            "missing_reason",
            "missing_detail",
            "fleet_boss_character_id",
            "last_esi_update",
            "fleet_wings",
            "fleet_wings_read_at",
        ]
    )
    state = write_state(operation, actor, job_id=job["id"])
    if not state.allowed:
        raise JobStopped(state.reason or NOT_ALLOWED)
    touch(job)


def job_summary(job, error="") -> str:
    summary = f"{job['label']}: {job['done']} of {job['total']} done"
    if job["failed"]:
        summary += f", {job['failed']} failed"
    summary += "."
    if error:
        summary += f" Stopped: {error}"
    return summary


def run_job(operation_id, job_id, kind, payload, actor_id) -> str:
    """Run a queued job (the body of ``tasks.run_fleet_job``); returns its final state.

    A job that is no longer the fleet's current queued job (given up as stopped, or replaced) is
    skipped. Afterwards the lock is released and, except for kicks, the fleet is read again and the
    job is recorded as the operation step ``fleet_<kind>`` and the audit entry ``fleet.<kind>_job``
    (with the runner's ``job["audit"]`` details).
    """
    job = cache.get(_job_key(operation_id))
    if not job or job.get("id") != job_id or job.get("state") != QUEUED:
        if not job:
            # The cache lost the job's progress: nobody else runs it, so its lock must not hold the
            # fleet until it expires.
            _release(operation_id, job_id)
        return "stale"
    error = ""
    try:
        operation = FleetOperation.objects.select_related("fc_user").filter(pk=operation_id).first()
        if operation is None:
            error = FLEET_MISSING
            return "missing"
        actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
        job["state"] = RUNNING
        _save(job)
        try:
            runner = import_string(RUNNERS[kind])
            runner(job, operation, payload, actor)
        except JobStopped as exc:
            error = str(exc) or NOT_ALLOWED
        except Exception as exc:
            error = report_failure(logger, job["label"], exc, operation)
    finally:
        _finish(job, FAILED if error else DONE, error)
    if kind not in SELF_RECORDING_KINDS:
        _record(job, operation, actor, error)
    return job["state"]


def _record(job, operation, actor, error) -> None:
    ok = not error and not job["failed"]
    summary = job_summary(job, error)
    set_action(operation, f"fleet_{job['kind']}", ok, summary)
    details = {
        "job_id": job["id"],
        "label": job["label"],
        "total": job["total"],
        "done": job["done"],
        "failed": job["failed"],
        "result": "ok" if ok else "failed",
        "summary": summary,
        **({"error": error} if error else {}),
        **job.get("audit", {}),
    }
    audit(actor, f"fleet.{job['kind']}_job", operation, None, details)
    from fleetops.services.fleet_refresh import schedule_refresh

    schedule_refresh(operation)


@contextmanager
def fleet_write_lock(operation, *, timeout=WRITE_LOCK_TIMEOUT):
    """Hold the fleet for one change; raises FleetManageError (409) while another change or a job runs."""
    key = _write_key(operation.pk)
    if not cache.add(key, 1, timeout):
        raise FleetManageError(WRITE_BUSY, 409)
    try:
        job = running_job(operation.pk)
        if job is not None:
            raise FleetManageError(job_wait_message(job), 409)
        yield
    finally:
        cache.delete(key)
