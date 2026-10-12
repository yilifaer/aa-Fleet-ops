import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from importlib import import_module
from typing import Any

import requests
from allianceauth.services.hooks import get_extension_logger
from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException, HTTPNotModified
from esi.models import Token
from esi.openapi_clients import ESIClientProvider

from fleetops import __title__, __url__, __version__
from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE

logger = get_extension_logger(__name__)

esi = ESIClientProvider(
    compatibility_date="2025-11-06",
    ua_appname=__title__,
    ua_version=__version__,
    **({"ua_url": __url__} if __url__ else {}),
    operations=[
        "GetCharactersCharacterIdFleet",
        "GetFleetsFleetId",
        "GetFleetsFleetIdMembers",
        "GetFleetsFleetIdWings",
        "PutFleetsFleetId",
        "DeleteFleetsFleetIdMembersMemberId",
    ],
)


def _installed(module_name):
    try:
        return import_module(module_name)
    except ImportError:
        return None


# django-esi 9 talks to ESI through httpx, django-esi 10 through httpx2.
HTTP_CLIENTS = [module for module in (_installed("httpx"), _installed("httpx2")) if module is not None]
TIMEOUT_ERRORS = (TimeoutError, requests.Timeout, *(client.TimeoutException for client in HTTP_CLIENTS))
CONNECTION_ERRORS = (ConnectionError, requests.ConnectionError, *(client.TransportError for client in HTTP_CLIENTS))
# Reason phrases for statuses that http.HTTPStatus does not know.
REASON_PHRASES = {420: "Error limited"}
MAX_ESI_ERROR_LENGTH = 120


class FleetESIError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


@dataclass(slots=True)
class FleetDetectionResult:
    fleet_id: int
    role: str
    squad_id: int | None = None
    wing_id: int | None = None
    fleet_boss_id: int | None = None

    def is_boss(self, character_id) -> bool:
        """Only the fleet boss can read and write the fleet, whatever position it sits in."""
        return self.fleet_boss_id is not None and int(self.fleet_boss_id) == int(character_id)


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "response_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _exception_chain(exc):
    seen = []
    while exc is not None and exc not in seen and len(seen) < 5:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _reason_phrase(status: int) -> str:
    phrase = REASON_PHRASES.get(status)
    if phrase is None:
        try:
            phrase = HTTPStatus(status).phrase
        except ValueError:
            return "unknown status"
    return phrase[:1].upper() + phrase[1:].lower()


def _esi_error_field(exc) -> str:
    """The ``error`` field of an ESI error body, shortened to one line."""
    data = getattr(exc, "data", None)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return ""
    value = data.get("error") if isinstance(data, dict) else getattr(data, "error", None)
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()).rstrip(".")
    if len(value) > MAX_ESI_ERROR_LENGTH:
        value = value[: MAX_ESI_ERROR_LENGTH - 3].rstrip() + "..."
    return value


def _esi_request_id(exc) -> str:
    # aiopenapi3 errors such as ContentTypeError only carry the response.
    headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
    try:
        items = list(headers.items()) if headers else []
    except (AttributeError, TypeError):
        return ""
    for key, value in items:
        if str(key).lower() == "x-esi-request-id":
            return str(value)[:100]
    return ""


def describe_esi_failure(exc, status: int | None = None) -> str:
    """A short, user-facing description of a failed ESI call.

    django-esi exception texts embed every response header, so they are never shown.
    """
    status = _status_code(exc) if status is None else status
    if status is not None:
        return f"ESI returned HTTP {status} ({_esi_error_field(exc) or _reason_phrase(status)})."
    if isinstance(exc, ESIErrorLimitException):
        reset = getattr(exc, "reset", None)
        return f"ESI error limit reached; try again in {int(reset)} seconds." if reset else "ESI error limit reached."
    if isinstance(exc, ESIBucketLimitException):
        return "ESI rate limit reached; try again later."
    chain = _exception_chain(exc)
    if any(isinstance(item, TIMEOUT_ERRORS) for item in chain):
        return "ESI did not respond in time."
    if any(isinstance(item, CONNECTION_ERRORS) for item in chain):
        return "Could not connect to ESI."
    return f"Unexpected ESI error ({type(exc).__name__})."


def _log_esi_failure(context: str, exc: Exception, level=logging.WARNING):
    # The status and ESI's request id are enough to follow a call up with CCP. Tokens are never logged.
    # django-esi raises ESIErrorLimitException while handling the 420 answer, which stays in the chain.
    chain = _exception_chain(exc)
    status = next((code for code in map(_status_code, chain) if code is not None), None)
    request_id = next((value for value in map(_esi_request_id, chain) if value), "")
    logger.log(
        level,
        "FleetOps ESI call failed: %s (%s, HTTP status %s, x-esi-request-id %s).",
        context,
        type(exc).__name__,
        status if status is not None else "none",
        request_id or "none",
    )


def _esi_failure(code: str, context: str, exc: Exception) -> FleetESIError:
    """Log a failed ESI call and return the short error shown to users."""
    _log_esi_failure(context, exc)
    status = _status_code(exc)
    return FleetESIError(code, f"{context}: {describe_esi_failure(exc, status)}", status)


def _read(operation, fresh: bool = False):
    """Return the body of a GET operation, also when it has not changed since the last read."""
    # With ETags enabled django-esi raises HTTPNotModified for an unchanged resource instead
    # of returning it. Fleet state is needed on every read, so only its response cache is used.
    if fresh:
        return operation.result(use_etag=False, force_refresh=True)
    try:
        return operation.result(use_etag=False)
    except HTTPNotModified:
        return operation.result(use_etag=False, force_refresh=True)


def _write(operation):
    """Send a PUT/DELETE operation to ESI; a write is never answered from the cache."""
    return operation.result(use_etag=False, use_cache=False, store_cache=False)


def as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    result = {}
    for key in (
        "fleet_id", "fleet_boss_id", "role", "squad_id", "wing_id", "character_id", "ship_type_id",
        "solar_system_id", "role_name", "takes_fleet_warp", "is_free_move",
        "is_registered", "is_voice_enabled", "motd",
    ):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def get_token(user, character_id: int, write: bool = False):
    scopes = [FLEET_READ_SCOPE]
    if write:
        scopes.append(FLEET_WRITE_SCOPE)
    token = (
        Token.objects.filter(user=user, character_id=character_id)
        .require_scopes(scopes)
        .require_valid()
        .first()
    )
    if token is None:
        raise FleetESIError(
            "MISSING_WRITE_SCOPE" if write else "MISSING_READ_SCOPE",
            "No valid ESI token with the required fleet scopes was found for this character.",
        )
    return token


def has_scope_token(user, character_id: int, write: bool = False) -> bool:
    try:
        get_token(user, character_id, write=write)
        return True
    except FleetESIError:
        return False


def detect_character_fleet(user, character_id: int, fresh: bool = False) -> FleetDetectionResult:
    """Read the fleet a character is in; ``fresh`` skips django-esi's response cache."""
    token = get_token(user, character_id, write=False)
    try:
        payload = as_dict(
            _read(
                esi.client.Fleets.GetCharactersCharacterIdFleet(
                    character_id=character_id,
                    token=token,
                ),
                fresh=fresh,
            )
        )
    except Exception as exc:  # django-esi raises its own HTTP exception classes
        status = _status_code(exc)
        if status == 404:
            # Not being in a fleet is an ordinary answer, not an ESI problem.
            _log_esi_failure("Could not detect fleet", exc, level=logging.INFO)
            raise FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet.", status) from exc
        if status in (401, 403):
            raise _esi_failure("ESI_FORBIDDEN", "ESI denied access to this character's fleet", exc) from exc
        raise _esi_failure("ESI_ERROR", "Could not detect fleet", exc) from exc

    fleet_id = payload.get("fleet_id")
    if not fleet_id:
        raise FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet.")
    boss_id = payload.get("fleet_boss_id")
    return FleetDetectionResult(
        fleet_id=int(fleet_id),
        role=str(payload.get("role") or ""),
        squad_id=payload.get("squad_id"),
        wing_id=payload.get("wing_id"),
        fleet_boss_id=int(boss_id) if boss_id else None,
    )


def get_fleet_info(user, character_id: int, fleet_id: int) -> dict:
    token = get_token(user, character_id, write=False)
    try:
        return as_dict(_read(esi.client.Fleets.GetFleetsFleetId(fleet_id=fleet_id, token=token)))
    except Exception as exc:
        code = "FLEET_NOT_FOUND" if _status_code(exc) == 404 else "ESI_ERROR"
        raise _esi_failure(code, "Could not read fleet information", exc) from exc


def get_fleet_members(user, character_id: int, fleet_id: int) -> list[dict]:
    token = get_token(user, character_id, write=False)
    try:
        result = _read(esi.client.Fleets.GetFleetsFleetIdMembers(fleet_id=fleet_id, token=token))
        return [as_dict(item) for item in (result or [])]
    except Exception as exc:
        # Only the fleet boss can read a fleet: a 404 means the fleet is gone or the boss changed.
        code = "FLEET_NOT_FOUND" if _status_code(exc) == 404 else "ESI_ERROR"
        raise _esi_failure(code, "Could not read fleet members", exc) from exc


def set_fleet_motd(user, character_id: int, fleet_id: int, motd: str) -> None:
    token = get_token(user, character_id, write=True)
    info = get_fleet_info(user, character_id, fleet_id)
    body = {
        "motd": motd,
        "is_free_move": bool(info.get("is_free_move", False)),
    }
    try:
        _write(esi.client.Fleets.PutFleetsFleetId(fleet_id=fleet_id, token=token, body=body))
    except Exception as exc:
        if _status_code(exc) in (401, 403):
            raise _esi_failure("MOTD_FORBIDDEN", "ESI denied permission to update the fleet MOTD", exc) from exc
        raise _esi_failure("MOTD_ERROR", "Could not update fleet MOTD", exc) from exc


def kick_fleet_member(user, character_id: int, fleet_id: int, member_id: int) -> None:
    """Kick one member from an EVE fleet via the fleet boss write token."""
    token = get_token(user, character_id, write=True)
    try:
        _write(
            esi.client.Fleets.DeleteFleetsFleetIdMembersMemberId(
                fleet_id=fleet_id,
                member_id=member_id,
                token=token,
            )
        )
    except Exception as exc:
        status = _status_code(exc)
        if status == 404:
            raise _esi_failure("MEMBER_NOT_FOUND", f"Fleet member {member_id} was not found", exc) from exc
        if status in (401, 403):
            raise _esi_failure("KICK_FORBIDDEN", "ESI denied permission to kick this fleet member", exc) from exc
        raise _esi_failure("KICK_ERROR", f"Could not kick fleet member {member_id}", exc) from exc
