# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Shared parts of the Fleet tab's views: answers for scripts and forms, object checks, posted ids."""

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.middleware.gzip import GZipMiddleware
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _

from fleetops.models import MAX_DATABASE_ID
from fleetops.services.fleet_access import FleetManageError, can_view_fleet_tab
from fleetops.services.fleet_manage import FleetActionResult
from fleetops.services.fleet_rules import NO_UNIT
from fleetops.services.fleet_view import state_payload
from fleetops.texts import translate_stored
from fleetops.views import _database_id, _operation_for_user

MESSAGE_LEVELS = {
    "success": messages.success,
    "info": messages.info,
    "warning": messages.warning,
    "error": messages.error,
}


def wants_json(request) -> bool:
    """Whether the Fleet tab's scripts sent the request; plain form posts get a redirect.

    Browsers accept anything (``*/*``) for a form post, so only an explicit request for JSON counts.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accepted = [part.split(";", 1)[0].strip().lower() for part in request.headers.get("Accept", "").split(",")]
    if "application/json" not in accepted:
        return False
    return "text/html" not in accepted or accepted.index("application/json") < accepted.index("text/html")


def no_store(response):
    """Fleet answers show live state, so neither the browser nor a proxy keeps them."""
    add_never_cache_headers(response)
    return response


# Compresses an answer the way Django's GZipMiddleware does, for browsers that accept it.
_GZIP = GZipMiddleware(lambda request: None)


def json_response(payload, status=200, *, request=None):
    """A JSON answer that is never stored; with ``request`` it is gzipped when the browser accepts it.

    The Fleet tab's state carries the HTML of the whole fleet, about 1 MB for a full fleet, which
    compresses to a few percent. Django masks the CSRF tokens in that HTML for every response, so
    compressing it does not give them away.
    """
    response = no_store(JsonResponse(payload, status=status))
    return _GZIP.process_response(request, response) if request is not None else response


def manage_operation_or_404(request, operation_uuid):
    """The fleet when the user can manage it; 404 otherwise, as on the other fleet controls."""
    return _operation_for_user(request, operation_uuid, manage=True)


def fleet_tab_operation_or_404(request, operation_uuid):
    """The fleet when the user may see its Fleet tab (VIS(2)); 404 otherwise."""
    operation = _operation_for_user(request, operation_uuid)
    if not can_view_fleet_tab(request.user, operation):
        raise Http404
    return operation


def unit_or_404(unit_id):
    """A wing or squad id from the URL; ids outside ESI's int64 range cannot exist."""
    if not 0 < unit_id <= MAX_DATABASE_ID:
        raise Http404
    return unit_id


def posted_ids(request, name="character_ids"):
    """The valid ids posted under ``name`` (a list field), without duplicates, in posted order."""
    ids = (_database_id(value.strip()) for value in request.POST.getlist(name))
    return list(dict.fromkeys(value for value in ids if value is not None))


def posted_unit(request, name, kind):
    """An optional posted wing or squad id; blank and -1 mean none. ValueError for anything else."""
    value = request.POST.get(name, "").strip()
    if value in ("", str(NO_UNIT)):
        return None
    number = _database_id(value)
    if number is None:
        raise ValueError(_("That wing does not exist.") if kind == "wing" else _("That squad does not exist."))
    return number


def invalid(message) -> FleetActionResult:
    """The answer to a request with missing or malformed fields."""
    return FleetActionResult(False, message, level="error", status=400)


def _answer(result):
    """``(answer, HTTP status)`` of a FleetActionResult or a FleetManageError."""
    if isinstance(result, FleetManageError):
        ok, data, message, status, level = False, {}, result.message, result.status, "error"
    else:
        ok, data = bool(result.ok), dict(result.data or {})
        message, status, level = result.message, result.status or 200, result.level
    # Messages that services store or raise are English; the page shows them in the viewer's language.
    message = translate_stored(message)
    if level not in MESSAGE_LEVELS or (level == "success" and not ok):
        level = "success" if ok else "error"
    return {"ok": ok, "message": message, "level": level, "data": data}, status


def fleet_response(request, operation, result):
    """Answer a fleet action: JSON for the Fleet tab's scripts, a message and the fleet page for forms.

    ``result`` is the action's FleetActionResult or the FleetManageError it raised. The JSON
    answer carries the Fleet tab's new state for viewers who may see the tab.
    """
    answer, status = _answer(result)
    if wants_json(request):
        # The action may have changed the fleet's row (a refresh, the status), so the state is read again.
        operation.refresh_from_db()
        state = state_payload(request, operation) if can_view_fleet_tab(request.user, operation) else None
        return json_response({**answer, "state": state}, status=status, request=request)
    if answer["message"]:
        MESSAGE_LEVELS[answer["level"]](request, answer["message"])
    url = reverse("fleetops:operation_detail", kwargs={"operation_uuid": operation.uuid})
    return no_store(redirect(f"{url}#fleet"))


def run_fleet_action(request, operation, action, *args, **kwargs):
    """Run a fleet service call and answer with its result, or with the FleetManageError it raised."""
    try:
        result = action(*args, **kwargs)
    except FleetManageError as exc:
        result = exc
    return fleet_response(request, operation, result)
