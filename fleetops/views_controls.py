"""Fleet control from the fleet page: kicking single members, everyone outside a system, capsules."""

from allianceauth.authentication.decorators import permissions_required
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST

from fleetops.models import FleetOperation
from fleetops.services.fleet_controls import (
    CAPSULES,
    NO_WRITE_TOKEN,
    OUTSIDE_SYSTEM,
    KickRefused,
    capsule_preview,
    fc_can_kick,
    fresh_sync,
    kick_member,
    kick_members,
    kickable_pod_members,
    outside_system_preview,
    protected_character_ids,
)
from fleetops.services.sde import solar_system_names
from fleetops.views import _can_manage_operation, _database_id, _operation_for_user

# The kick preview reads the fleet from ESI at most once in this many seconds per fleet.
PREVIEW_SYNC_SECONDS = 10
EMPTY_PREVIEW = {"systems": [], "selected_system_id": None, "fc_system_id": None, "members": []}


def _kicks_possible(operation):
    return operation.status == FleetOperation.Status.ACTIVE and bool(operation.esi_fleet_id)


def controls_context(request, operation):
    """What the fleet page's member list and Fleet Control card need for kicks."""
    if not (_kicks_possible(operation) and _can_manage_operation(request.user, operation)):
        return {"kick_enabled": False}
    return {
        "kick_enabled": True,
        "kick_token_ok": fc_can_kick(operation),
        "kick_disabled_reason": NO_WRITE_TOKEN,
        "kick_protected_ids": protected_character_ids(operation),
        "kick_pods": sorted(kickable_pod_members(operation), key=lambda member: member.character_name),
    }


def _back(operation):
    return redirect("fleetops:operation_detail", operation_uuid=operation.uuid)


def posted_character_ids(request):
    """The valid ``character_ids`` of a POST; anything else is dropped."""
    ids = (_database_id(value) for value in request.POST.getlist("character_ids"))
    return [value for value in ids if value is not None]


def _plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def report_kicks(request, result, noun, where=""):
    """Messages for a batch of kicks, e.g. ``noun`` "capsule member" or "member" with ``where`` " outside Jita"."""
    if result["queued"]:
        messages.info(
            request,
            f"Kicking {_plural(result['target_count'], noun)}{where} in the background; "
            "results appear under Operation Actions.",
        )
    elif not result["target_count"]:
        messages.warning(request, f"Nobody was kicked: none of the selected {noun}s{where} can be kicked now.")
        return
    elif result["failures"]:
        messages.warning(
            request, f"Kicked {_plural(len(result['kicked']), noun)}{where}; {len(result['failures'])} failed."
        )
    else:
        messages.success(request, f"Kicked {_plural(len(result['kicked']), noun)}{where}.")
    if result["skipped"]:
        skipped = _plural(len(result["skipped"]), "selected member")
        messages.info(request, f"{skipped} skipped: no longer in the fleet or no longer a target.")


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def kick_member_view(request, operation_uuid, character_id):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    try:
        result = kick_member(operation, character_id, actor=request.user)
    except KickRefused as exc:
        messages.error(request, str(exc))
    else:
        if result.ok:
            messages.success(request, result.message)
        else:
            messages.error(request, result.message)
    return _back(operation)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_GET
def kick_preview_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    if not _kicks_possible(operation):
        return JsonResponse(EMPTY_PREVIEW)
    # Kick lists come from a fresh read of the fleet; opening the list again within seconds reuses it.
    if cache.add(f"fleetops:kickpreview:{operation.pk}", "1", timeout=PREVIEW_SYNC_SECONDS):
        fresh_sync(operation)
    if request.GET.get("mode") == CAPSULES:
        return JsonResponse(capsule_preview(operation))
    return JsonResponse(outside_system_preview(operation, _database_id(request.GET.get("system_id", ""))))


def _system_name(operation, system_id):
    name = (
        operation.member_states.filter(solar_system_id=system_id)
        .exclude(solar_system_name="")
        .values_list("solar_system_name", flat=True)
        .first()
    )
    return name or solar_system_names({system_id}).get(system_id) or str(system_id)


@permissions_required(("fleetops.manage_own_fleet", "fleetops.manage_fleets"), raise_exception=True)
@require_POST
def kick_outside_system_view(request, operation_uuid):
    operation = _operation_for_user(request, operation_uuid, manage=True)
    system_id = _database_id(request.POST.get("system_id", ""))
    if system_id is None:
        messages.error(request, "Choose the system whose members stay in the fleet.")
        return _back(operation)
    ids = posted_character_ids(request)
    if not ids:
        messages.warning(request, "Nobody was kicked: no members were selected.")
        return _back(operation)
    system_name = _system_name(operation, system_id)
    try:
        result = kick_members(
            operation,
            ids,
            actor=request.user,
            purpose=OUTSIDE_SYSTEM,
            context={"system_id": system_id, "system_name": system_name},
        )
    except KickRefused as exc:
        messages.error(request, str(exc))
        return _back(operation)
    report_kicks(request, result, "member", f" outside {system_name}")
    return _back(operation)
