# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Where a loss happened and what kind of ship it was: security status, constellation and region of a solar system
and the group of a ship type.

The data come from django-eveonline-sde when it is installed and from public ESI reads otherwise. Both are static game
data, so answers are kept for seven days; a failed lookup is remembered for an hour so a broken source is not asked
again for every killmail. Every error is swallowed and the missing values are left out of the result: SRP never
refuses a claim for a value it could not look up.
"""

from dataclasses import astuple, dataclass
from decimal import ROUND_HALF_UP, Decimal

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps
from django.core.cache import cache
from django.utils import translation

from fleetops.providers import esi_universe
from fleetops.services import sde

logger = get_extension_logger(__name__)

CACHE_SECONDS = 7 * 24 * 3600
FAILURE_SECONDS = 3600
SECURITY_PLACES = Decimal("0.0001")
# Column names of the security status in the SDE app, in the order they are tried.
SECURITY_COLUMNS = ("security_status", "security")
_FAILED = "failed"
_reported = set()


@dataclass(frozen=True)
class SystemInfo:
    """What SRP needs to know about a solar system; ``None`` for a value that could not be found."""

    system_id: int
    security: Decimal | None
    constellation_id: int | None
    region_id: int | None

    @property
    def complete(self) -> bool:
        return self.security is not None and self.constellation_id is not None and self.region_id is not None


def _report(step: str, exc: Exception) -> None:
    """Note an unreadable source once per kind of failure and process; SRP then goes on without the data."""
    key = (step, type(exc).__name__)
    if key not in _reported:
        _reported.add(key)
        logger.debug("FleetOps: %s failed (%s).", step, type(exc).__name__)


def security_value(value) -> Decimal | None:
    """A security status as stored: four decimal places; None for anything that is not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or abs(number) > 1:
        return None
    return number.quantize(SECURITY_PLACES, rounding=ROUND_HALF_UP)


def _positive_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _ids(values) -> set[int]:
    return {number for number in map(_positive_int, values or ()) if number}


def _system_key(system_id) -> str:
    return f"fleetops:universe:system:{system_id}"


def _constellation_key(constellation_id) -> str:
    return f"fleetops:universe:constellation:{constellation_id}"


def _type_key(type_id) -> str:
    return f"fleetops:universe:type-group:{type_id}"


def _security_column(columns) -> str | None:
    return next((name for name in SECURITY_COLUMNS if name in columns), None)


def _sde_systems(ids) -> dict[int, SystemInfo]:
    """Solar systems from django-eveonline-sde; {} without it or when it cannot be read."""
    if not sde.sde_installed():
        return {}
    try:
        model = apps.get_model("eve_sde", "SolarSystem")
        columns = sde._columns(model)
        security = _security_column(columns)
        if security is None:
            return {}
        fields = ["pk", security]
        has_constellation = "constellation" in columns
        if has_constellation:
            fields.append("constellation_id")
            fields.append("constellation__region_id")
        with translation.override("en"):
            rows = list(model.objects.filter(pk__in=list(ids)).values_list(*fields))
    except Exception as exc:
        _report("Reading solar systems from django-eveonline-sde", exc)
        return {}
    found = {}
    for row in rows:
        system_id = int(row[0])
        found[system_id] = SystemInfo(
            system_id=system_id,
            security=security_value(row[1]),
            constellation_id=_positive_int(row[2]) if has_constellation else None,
            region_id=_positive_int(row[3]) if has_constellation else None,
        )
    return found


def _esi_region(constellation_id: int) -> int | None:
    """The region of a constellation, from the cache or ESI; None when it cannot be found."""
    key = _constellation_key(constellation_id)
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached == _FAILED:
        return None
    if isinstance(cached, int):
        return cached
    try:
        region_id = _positive_int(esi_universe.get_constellation(constellation_id).get("region_id"))
    except Exception as exc:
        _report("Reading a constellation from ESI", exc)
        region_id = None
    try:
        cache.set(key, region_id if region_id else _FAILED, CACHE_SECONDS if region_id else FAILURE_SECONDS)
    except Exception as exc:
        _report("Caching a constellation", exc)
    return region_id


def _esi_system(system_id: int) -> SystemInfo | None:
    try:
        payload = esi_universe.get_system(system_id)
    except Exception as exc:
        _report("Reading a solar system from ESI", exc)
        return None
    security = security_value(payload.get("security_status"))
    constellation_id = _positive_int(payload.get("constellation_id"))
    if security is None and constellation_id is None:
        return None
    return SystemInfo(system_id, security, constellation_id, None)


def _with_region(info: SystemInfo) -> SystemInfo:
    """The system with its region looked up through the constellation when it is missing."""
    if info.region_id is not None or info.constellation_id is None:
        return info
    region_id = _esi_region(info.constellation_id)
    return SystemInfo(info.system_id, info.security, info.constellation_id, region_id)


def system_infos(ids) -> dict[int, SystemInfo]:
    """``{system_id: SystemInfo}`` for the systems that could be found; the others are left out.

    Order of sources: the cache, django-eveonline-sde, ESI (the system, then its constellation for the region).
    """
    wanted = _ids(ids)
    if not wanted:
        return {}
    keys = {_system_key(system_id): system_id for system_id in wanted}
    try:
        cached = cache.get_many(list(keys))
    except Exception:
        cached = {}
    result = {}
    for key, value in cached.items():
        if value != _FAILED:
            result[keys[key]] = SystemInfo(*value)
    missing = wanted - {keys[key] for key in cached}
    if not missing:
        return result
    found = _sde_systems(missing)
    for system_id in sorted(missing):
        info = found.get(system_id) or _esi_system(system_id)
        if info is None:
            _remember(system_id, None)
            continue
        info = _with_region(info)
        result[system_id] = info
        _remember(system_id, info)
    return result


def _remember(system_id: int, info: SystemInfo | None) -> None:
    try:
        if info is None:
            cache.set(_system_key(system_id), _FAILED, FAILURE_SECONDS)
        else:
            # A system whose constellation or region is unknown is looked up again after an hour.
            cache.set(_system_key(system_id), astuple(info), CACHE_SECONDS if info.complete else FAILURE_SECONDS)
    except Exception as exc:
        _report("Caching a solar system", exc)


def type_groups(ids) -> dict[int, int]:
    """``{type_id: group_id}`` for the item types that could be found; the others are left out.

    Order of sources: the cache, django-eveonline-sde, ESI.
    """
    wanted = _ids(ids)
    if not wanted:
        return {}
    keys = {_type_key(type_id): type_id for type_id in wanted}
    try:
        cached = cache.get_many(list(keys))
    except Exception:
        cached = {}
    result = {keys[key]: value for key, value in cached.items() if isinstance(value, int)}
    missing = wanted - {keys[key] for key in cached}
    if not missing:
        return result
    try:
        details = sde.ship_type_details(missing)
    except Exception as exc:
        _report("Reading item types from django-eveonline-sde", exc)
        details = {}
    for type_id in sorted(missing):
        info = details.get(type_id)
        group_id = _positive_int(info.group_id) if info is not None else None
        if group_id is None:
            try:
                group_id = _positive_int(esi_universe.get_type(type_id).get("group_id"))
            except Exception as exc:
                _report("Reading an item type from ESI", exc)
                group_id = None
        try:
            cache.set(_type_key(type_id), group_id if group_id else _FAILED, CACHE_SECONDS if group_id else FAILURE_SECONDS)
        except Exception as exc:
            _report("Caching an item type", exc)
        if group_id:
            result[type_id] = group_id
    return result
