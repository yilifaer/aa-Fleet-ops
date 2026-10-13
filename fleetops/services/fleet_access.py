"""Who may see and change a running EVE fleet from the fleet page, and whether a change is possible now.

Every change goes to ESI with the FC character's token, never with the acting user's: only the fleet
boss can change a fleet, and the FC character is the boss while FleetOps tracks it. After a boss
handover, without the write scope or while a background job runs, ``write_state`` says why the
controls are disabled.
"""

from dataclasses import dataclass

from esi.models import Token

from fleetops.constants import FLEET_SCOPES, FLEET_WRITE_SCOPE
from fleetops.models import FleetOperation

Status = FleetOperation.Status
EndReason = FleetOperation.EndReason

# Statuses whose fleet page has a Fleet tab: the live tree while open, the composition after the close.
FLEET_TAB_STATUSES = (Status.ACTIVE, Status.ENDING, Status.CLOSED)
LIVE_STATUSES = (Status.ACTIVE, Status.ENDING)

NOT_ACTIVE = "Fleet management is only available while the fleet is active."
FLEET_GONE = "The fleet could not be read. It may have been disbanded, or the FC left it."
NO_WRITE_TOKEN = f"The FC character has no ESI token with the fleet write scope ({FLEET_WRITE_SCOPE})."
WRITE_BUSY = "Another change to this fleet is in progress. Try again in a moment."


class FleetManageError(Exception):
    """A fleet change that cannot be made; ``status`` is the HTTP status of the answer (409, 404 …)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class WriteState:
    allowed: bool
    reason: str = ""
    # The FC user may add the missing write scope through FleetOps' ESI authorization.
    show_authorize: bool = False


def not_boss_message(operation, boss_label="") -> str:
    boss = boss_label or "another character"
    return (
        f"{operation.fc_character_name} is not the fleet boss any more (current boss: {boss}). "
        "Fleet changes are disabled."
    )


def job_wait_message(job) -> str:
    return f"Wait until “{job.get('label') or 'the running job'}” has finished."


def has_esi_fleet(operation) -> bool:
    """Whether the fleet is an EVE fleet FleetOps reads: not a manual or PAP-link-only record."""
    return bool(operation.esi_fleet_id) and not operation.is_manual and not operation.link_only


def can_manage_fleet(user, operation) -> bool:
    """Managers of the fleet: its credited FCs with manage_own_fleet, and manage_fleets holders."""
    # Imported here: the views import the fleet services.
    from fleetops.views import _can_manage_operation

    return bool(user is not None and user.is_authenticated and _can_manage_operation(user, operation))


def can_view_live_fleet(user, operation) -> bool:
    """Who sees the tree and composition of an open fleet: its managers, view_all_fleets and manage_fleets."""
    if user is None or not user.is_authenticated:
        return False
    return (
        user.has_perm("fleetops.manage_fleets")
        or user.has_perm("fleetops.view_all_fleets")
        or can_manage_fleet(user, operation)
    )


def can_view_fleet_tab(user, operation) -> bool:
    """Whether the fleet page shows the Fleet tab to ``user``.

    Open fleets (Active, Ending): only people who see the live fleet. Closed fleets: everyone who can
    open the fleet page sees the composition. Manual, PAP-link-only and scheduled fleets have no tab.
    """
    if not has_esi_fleet(operation) or operation.status not in FLEET_TAB_STATUSES:
        return False
    if operation.status in LIVE_STATUSES:
        return can_view_live_fleet(user, operation)
    from fleetops.views import _can_view_operation

    return bool(user is not None and user.is_authenticated and _can_view_operation(user, operation))


def fc_has_write_token(operation) -> bool:
    """Whether the FC character has a token with the fleet scopes; not refreshed, so no SSO call on page load."""
    return (
        Token.objects.filter(user_id=operation.fc_user_id, character_id=operation.fc_character_id)
        .require_scopes(FLEET_SCOPES)
        .exists()
    )


def boss_problem(operation):
    """``(reason, detail)`` when the FC character cannot change the fleet any more, else None.

    Tracking stores what its latest poll found; a boss check after a failed write or refresh is
    cached for a minute and counts too.
    """
    from fleetops.services.fleet_refresh import cached_boss_problem

    stored = None
    if operation.missing_reason in (EndReason.BOSS_HANDOVER, EndReason.FLEET_GONE):
        stored = (operation.missing_reason, operation.missing_detail)
    cached = cached_boss_problem(operation.pk)
    found = [problem for problem in (stored, cached) if problem is not None]
    # A handover names the new boss, so it wins over "gone".
    for problem in found:
        if problem[0] == EndReason.BOSS_HANDOVER:
            return problem[0], problem[1]
    return (found[0][0], found[0][1]) if found else None


def fleet_problem(operation, *, job_id=None) -> str:
    """Why the fleet cannot be changed now, whoever asks; an empty string when it can.

    ``job_id`` is the background job asking, which does not block itself.
    """
    if operation.status != Status.ACTIVE or not has_esi_fleet(operation):
        return NOT_ACTIVE
    problem = boss_problem(operation)
    if problem is not None:
        reason, detail = problem[0], problem[1]
        if reason == EndReason.BOSS_HANDOVER:
            return not_boss_message(operation, detail)
        return FLEET_GONE
    if not fc_has_write_token(operation):
        return NO_WRITE_TOKEN
    from fleetops.services.fleet_jobs import running_job

    job = running_job(operation.pk)
    if job is not None and job.get("id") != job_id:
        return job_wait_message(job)
    return ""


def write_state(operation, user, *, job_id=None) -> WriteState:
    """Whether ``user`` may change the fleet now; the first failing check gives the reason.

    Users who cannot manage the fleet get no reason: the page shows them no controls.
    """
    if not can_manage_fleet(user, operation):
        return WriteState(False, "")
    reason = fleet_problem(operation, job_id=job_id)
    if not reason:
        return WriteState(True)
    show_authorize = reason == NO_WRITE_TOKEN and operation.fc_user_id == user.pk
    return WriteState(False, reason, show_authorize)


def require_writable(operation, user, *, job_id=None) -> None:
    """Raise FleetManageError (409) with the reason when ``user`` cannot change the fleet now."""
    state = write_state(operation, user, job_id=job_id)
    if not state.allowed:
        raise FleetManageError(state.reason or "You cannot change this fleet.", 409 if state.reason else 404)
