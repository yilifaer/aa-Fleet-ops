# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from dataclasses import astuple, dataclass

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps
from django.core.cache import cache
from django.utils import translation

logger = get_extension_logger(__name__)

SHIP_CATEGORY_ID = 6
# Ship types change with game patches only; a type the SDE does not know yet is looked up again sooner.
SDE_CACHE_SECONDS = 24 * 3600
SDE_MISS_SECONDS = 3600
_MISSING = "missing"
_reported = False


@dataclass(frozen=True)
class ShipTypeInfo:
    """What the fleet composition needs to know about a ship type; names are English."""

    type_id: int
    name: str
    group_id: int | None
    group_name: str
    category_id: int | None
    mass: float | None


def sde_installed() -> bool:
    return apps.is_installed("eve_sde")


def _label(obj):
    # django-eveonline-sde translates "name" into the active language; name_en is the English column.
    for attr in ("name_en", "name", "type_name", "solar_system_name"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return ""


def _names(model_name, ids):
    """English names by id; {} when the SDE app is missing or cannot be read."""
    if not sde_installed():
        return {}
    try:
        model = apps.get_model("eve_sde", model_name)
        # Stored names must not depend on the language of whoever triggered the lookup (a Chinese web
        # request, or a Celery worker), so the lookup always runs in English.
        with translation.override("en"):
            return {obj.pk: _label(obj) for obj in model.objects.filter(pk__in=list(ids))}
    except Exception:
        return {}


def item_type_names(type_ids):
    return _names("ItemType", type_ids)


def solar_system_names(system_ids):
    return _names("SolarSystem", system_ids)


def _report(step, exc):
    """Note an unreadable SDE once per process; FleetOps then works without its data."""
    global _reported
    if not _reported:
        _reported = True
        logger.debug("FleetOps: %s from django-eveonline-sde failed (%s).", step, type(exc).__name__)


def _ids(values):
    ids = set()
    for value in values or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            ids.add(number)
    return ids


def _columns(model):
    """Field names of an SDE model, such as {"id", "name", "name_en", "group", "mass"}."""
    return {field.name for field in model._meta.get_fields()}


def _name_column(columns):
    # django-modeltranslation adds name_en next to the translated name; older schemas have name only.
    return "name_en" if "name_en" in columns else "name"


def _group_rows(group_ids):
    """``{group_id: (English name, category_id)}`` read with one query."""
    if not group_ids:
        return {}
    model = apps.get_model("eve_sde", "ItemGroup")
    columns = _columns(model)
    fields = ["pk", _name_column(columns)]
    if "category" in columns:
        fields.append("category_id")
    rows = model.objects.filter(pk__in=list(group_ids)).values_list(*fields)
    return {int(row[0]): (str(row[1] or ""), row[2] if len(row) > 2 else None) for row in rows}


def _read_types(type_ids):
    """ShipTypeInfo of the types the SDE knows, by id; None when the SDE cannot be read."""
    try:
        with translation.override("en"):
            model = apps.get_model("eve_sde", "ItemType")
            columns = _columns(model)
            # Only the columns needed: item types also carry long translated descriptions.
            fields = ["pk", _name_column(columns)]
            has_group, has_mass = "group" in columns, "mass" in columns
            fields += ["group_id"] if has_group else []
            fields += ["mass"] if has_mass else []
            rows = list(model.objects.filter(pk__in=list(type_ids)).values_list(*fields))
            group_ids = {row[2] for row in rows if has_group and row[2]}
            groups = _group_rows(group_ids)
    except Exception as exc:
        _report("Reading ship types", exc)
        return None
    found = {}
    for row in rows:
        type_id, name = int(row[0]), str(row[1] or "")
        group_id = row[2] if has_group else None
        mass = row[-1] if has_mass else None
        group_name, category_id = groups.get(group_id, ("", None))
        found[type_id] = ShipTypeInfo(
            type_id=type_id,
            name=name,
            group_id=int(group_id) if group_id else None,
            group_name=group_name,
            category_id=int(category_id) if category_id is not None else None,
            mass=float(mass) if mass is not None else None,
        )
    return found


def _type_key(type_id):
    return f"fleetops:sde:type:{type_id}"


def ship_type_details(type_ids) -> dict:
    """``{type_id: ShipTypeInfo}`` for the types the SDE knows; {} without django-eveonline-sde.

    Results are cached for a day, so a fleet page refresh usually reads nothing from the database.
    """
    ids = _ids(type_ids)
    if not ids or not sde_installed():
        return {}
    keys = {_type_key(type_id): type_id for type_id in ids}
    try:
        cached = cache.get_many(list(keys))
    except Exception:
        cached = {}
    result = {}
    for key, value in cached.items():
        if value != _MISSING:
            result[keys[key]] = ShipTypeInfo(*value)
    missing = ids - {keys[key] for key in cached}
    if not missing:
        return result
    found = _read_types(missing)
    if found is None:
        return result
    result.update(found)
    try:
        cache.set_many({_type_key(type_id): astuple(info) for type_id, info in found.items()}, SDE_CACHE_SECONDS)
        unknown = missing - set(found)
        if unknown:
            cache.set_many({_type_key(type_id): _MISSING for type_id in unknown}, SDE_MISS_SECONDS)
    except Exception as exc:
        _report("Caching ship types", exc)
    return result


def item_group_names(group_ids) -> dict:
    """English names of SDE item groups by id; {} without django-eveonline-sde."""
    ids = _ids(group_ids)
    if not ids or not sde_installed():
        return {}
    try:
        with translation.override("en"):
            return {group_id: row[0] for group_id, row in _group_rows(ids).items()}
    except Exception as exc:
        _report("Reading item groups", exc)
        return {}


def _find(model_name, name, filters, order, strict=False):
    text = " ".join(str(name or "").split())
    if not text or not sde_installed():
        return None
    try:
        with translation.override("en"):
            model = apps.get_model("eve_sde", model_name)
            columns = _columns(model)
            column = _name_column(columns)
            rows = model.objects.filter(**{f"{column}__iexact": text})
            for field, lookup, value in filters:
                if field in columns and value is not None:
                    rows = rows.filter(**{lookup: value})
            ordering = [f"-{field}" if field != "pk" else field for field in order if field == "pk" or field in columns]
            row = rows.order_by(*ordering).values_list("pk", column).first()
    except Exception as exc:
        if strict:
            raise
        _report(f"Looking up {model_name}", exc)
        return None
    return (int(row[0]), str(row[1])) if row else None


def find_item_type(name, category_id=SHIP_CATEGORY_ID, *, strict=False):
    """``(type_id, English name)`` of the type called ``name`` (exact, any case), or None.

    Only ships (category 6) by default; ``category_id=None`` searches every type. Published
    types win over unpublished ones of the same name. With ``strict`` an unreadable SDE raises
    its error instead of finding nothing.
    """
    return _find("ItemType", name, [("group", "group__category_id", category_id)], ["published", "pk"], strict)


def find_item_group(name, category_id=SHIP_CATEGORY_ID, *, strict=False):
    """``(group_id, English name)`` of the item group called ``name`` (exact, any case), or None."""
    return _find("ItemGroup", name, [("category", "category_id", category_id)], ["published", "pk"], strict)


def stargate_edges():
    """Return undirected solar-system adjacency from django-eveonline-sde.

    Current django-eveonline-sde stores Stargate.solar_system and
    Stargate.destination as SolarSystem foreign keys.  A small fallback is
    retained so FleetOps degrades cleanly with older/community SDE schemas.
    """
    if not sde_installed():
        return {}
    try:
        model = apps.get_model("eve_sde", "Stargate")
    except Exception:
        return {}

    field_names = {f.name for f in model._meta.get_fields()}

    # Current django-eveonline-sde schema.
    if {"solar_system", "destination"}.issubset(field_names):
        graph = {}
        try:
            rows = model.objects.values_list("solar_system_id", "destination_id")
            for source_id, destination_id in rows.iterator():
                if not source_id or not destination_id:
                    continue
                a, b = int(source_id), int(destination_id)
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set()).add(a)
            return graph
        except Exception:
            return {}

    source_candidates = ["solar_system", "source_solar_system", "from_solar_system", "system"]
    destination_candidates = ["destination_solar_system", "to_solar_system", "destination_system"]
    source = next((f for f in source_candidates if f in field_names), None)
    destination = next((f for f in destination_candidates if f in field_names), None)
    if not source or not destination:
        destination_gate = next((f for f in ("destination", "destination_stargate") if f in field_names), None)
        if not source or not destination_gate:
            return {}
        graph = {}
        try:
            for gate in model.objects.select_related(source, destination_gate, f"{destination_gate}__{source}").all():
                a = getattr(getattr(gate, source), "pk", getattr(gate, f"{source}_id", None))
                dst_gate = getattr(gate, destination_gate, None)
                b = getattr(getattr(dst_gate, source, None), "pk", None) if dst_gate else None
                if a and b:
                    graph.setdefault(int(a), set()).add(int(b))
                    graph.setdefault(int(b), set()).add(int(a))
            return graph
        except Exception:
            return {}

    graph = {}
    try:
        for gate in model.objects.all():
            a = getattr(gate, f"{source}_id", None) or getattr(getattr(gate, source, None), "pk", None)
            b = getattr(gate, f"{destination}_id", None) or getattr(getattr(gate, destination, None), "pk", None)
            if a and b:
                graph.setdefault(int(a), set()).add(int(b))
                graph.setdefault(int(b), set()).add(int(a))
        return graph
    except Exception:
        return {}
