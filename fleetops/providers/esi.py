from dataclasses import dataclass
from typing import Any

from esi.exceptions import HTTPNotModified
from esi.models import Token
from esi.openapi_clients import ESIClientProvider

from fleetops import __title__, __url__, __version__
from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE


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


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "response_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _read(operation):
    """Return the body of a GET operation, also when it has not changed since the last read."""
    # With ETags enabled django-esi raises HTTPNotModified for an unchanged resource instead
    # of returning it. Fleet state is needed on every read, so only its response cache is used.
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
        "fleet_id", "role", "squad_id", "wing_id", "character_id", "ship_type_id",
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


def detect_character_fleet(user, character_id: int) -> FleetDetectionResult:
    token = get_token(user, character_id, write=False)
    try:
        payload = as_dict(
            _read(
                esi.client.Fleets.GetCharactersCharacterIdFleet(
                    character_id=character_id,
                    token=token,
                )
            )
        )
    except Exception as exc:  # django-esi raises its own HTTP exception classes
        status = _status_code(exc)
        if status == 404:
            raise FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet.", status) from exc
        if status in (401, 403):
            raise FleetESIError("ESI_FORBIDDEN", "ESI denied access to this character fleet.", status) from exc
        raise FleetESIError("ESI_ERROR", f"Could not detect fleet: {exc}", status) from exc

    fleet_id = payload.get("fleet_id")
    if not fleet_id:
        raise FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet.")
    return FleetDetectionResult(
        fleet_id=int(fleet_id),
        role=str(payload.get("role") or ""),
        squad_id=payload.get("squad_id"),
        wing_id=payload.get("wing_id"),
    )


def get_fleet_info(user, character_id: int, fleet_id: int) -> dict:
    token = get_token(user, character_id, write=False)
    try:
        return as_dict(_read(esi.client.Fleets.GetFleetsFleetId(fleet_id=fleet_id, token=token)))
    except Exception as exc:
        status = _status_code(exc)
        if status == 404:
            raise FleetESIError("FLEET_NOT_FOUND", "Fleet no longer exists.", status) from exc
        raise FleetESIError("ESI_ERROR", f"Could not read fleet information: {exc}", status) from exc


def get_fleet_members(user, character_id: int, fleet_id: int) -> list[dict]:
    token = get_token(user, character_id, write=False)
    try:
        result = _read(esi.client.Fleets.GetFleetsFleetIdMembers(fleet_id=fleet_id, token=token))
        return [as_dict(item) for item in (result or [])]
    except Exception as exc:
        status = _status_code(exc)
        if status == 404:
            raise FleetESIError("FLEET_NOT_FOUND", "Fleet no longer exists.", status) from exc
        raise FleetESIError("ESI_ERROR", f"Could not read fleet members: {exc}", status) from exc


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
        status = _status_code(exc)
        if status in (401, 403):
            raise FleetESIError("MOTD_FORBIDDEN", "ESI denied permission to update the fleet MOTD.", status) from exc
        raise FleetESIError("MOTD_ERROR", f"Could not update fleet MOTD: {exc}", status) from exc


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
            raise FleetESIError("MEMBER_NOT_FOUND", f"Fleet member {member_id} was not found.", status) from exc
        if status in (401, 403):
            raise FleetESIError("KICK_FORBIDDEN", "ESI denied permission to kick this fleet member.", status) from exc
        raise FleetESIError("KICK_ERROR", f"Could not kick fleet member {member_id}: {exc}", status) from exc
