# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fleet composition: members by ship function, ship type, ship group and system, and bridgeable mass."""

from collections import Counter
from dataclasses import dataclass, field
from django.utils.translation import gettext, gettext_lazy

from fleetops.fleet_defaults import KNOWN_SHIP_GROUPS
from fleetops.models import FleetOpsSettings, ShipFunction, ShipFunctionRule
from fleetops.services import sde
from fleetops.services.fleet_rules import ShipFunctionMap

# Which ship function a member counts under (see fleet_rules.ShipFunctionMap).
Classifier = ShipFunctionMap

UNCLASSIFIED = gettext_lazy("Unclassified")
TYPE_ICON_URL = "https://images.evetech.net/types/{}/icon?size=32"


@dataclass
class Row:
    key: object
    label: str
    count: int = 0
    percent: float = 0.0
    color: str = ""
    icon_url: str = ""
    # Row details such as the function badge, the docked count or the FC system flag.
    extra: dict | None = None


@dataclass
class BridgeSummary:
    mass_kg: float = 0.0
    ships: int = 0
    excluded: list[Row] = field(default_factory=list)
    unknown_mass: int = 0


@dataclass
class Composition:
    total: int = 0
    by_function: list[Row] = field(default_factory=list)
    by_type: list[Row] = field(default_factory=list)
    by_group: list[Row] = field(default_factory=list)
    by_system: list[Row] = field(default_factory=list)
    bridge: BridgeSummary = field(default_factory=BridgeSummary)
    sde_available: bool = False
    unclassified: list[Row] = field(default_factory=list)


def _value(item, *names):
    """A field of a member given as a model instance, a dataclass or a dict."""
    for name in names:
        if isinstance(item, dict):
            if item.get(name) not in (None, ""):
                return item[name]
        else:
            value = getattr(item, name, None)
            if value not in (None, ""):
                return value
    return None


def _id(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def member_role(member):
    return _value(member, "role", "fleet_role") or ""


def member_type_id(member):
    return _id(_value(member, "ship_type_id"))


def load_classifier() -> Classifier:
    """The active ship functions and their rules."""
    functions = list(ShipFunction.objects.filter(is_active=True))
    rules = list(ShipFunctionRule.objects.filter(function__is_active=True))
    return Classifier.build(functions, rules)


def classify(member, info, classifier):
    """The ship function ``member`` counts under (``info``: its ShipTypeInfo or None), or None."""
    return classifier.resolve(
        role=member_role(member) or None,
        ship_type_id=member_type_id(member),
        group_id=info.group_id if info else None,
        category_id=info.category_id if info else None,
    )


def function_key(function):
    """The id of a ship function (model instance or dict), or None for "Unclassified"."""
    if function is None:
        return None
    return _value(function, "pk", "id")


def _function_sort(function):
    return (_value(function, "sort_order") or 0, str(_value(function, "name") or "").casefold())


def _percent(count, total):
    return round(count * 100 / total, 1) if total else 0.0


def _by_count(rows):
    # Most common first; ties in name order, so the order does not jump between refreshes.
    return sorted(rows, key=lambda row: (-row.count, row.label.casefold(), str(row.key)))


def _type_label(type_id, names, info):
    if type_id is None:
        return gettext("Unknown ship")
    return names.get(type_id) or (info.name if info else "") or gettext("Type %(id)s") % {"id": type_id}


def _system_label(system_id, names):
    if system_id is None:
        return gettext("Unknown system")
    return names.get(system_id) or gettext("System %(id)s") % {"id": system_id}


def _docked(member):
    docked = _value(member, "docked")
    if docked is not None:
        return bool(docked)
    return bool(_value(member, "station_id"))


def build_composition(members, *, fc_system_id=None, settings=None, classifier=None) -> Composition:
    """Count ``members`` (tracked member states, fleet view members or dicts) for the composition panel."""
    members = list(members)
    settings = settings or FleetOpsSettings.get_solo()
    classifier = classifier if classifier is not None else load_classifier()
    sde_available = sde.sde_installed()
    infos = sde.ship_type_details({member_type_id(member) for member in members} - {None})
    total = len(members)

    functions, function_counts = {}, Counter()
    type_counts, type_names, type_unclassified = Counter(), {}, Counter()
    group_counts, group_names = Counter(), {}
    system_counts, system_names, docked = Counter(), {}, Counter()
    bridge = BridgeSummary()
    excluded_groups = settings.bridge_excluded_groups
    excluded_counts = Counter()

    for member in members:
        type_id = member_type_id(member)
        info = infos.get(type_id)
        function = classify(member, info, classifier)
        key = function_key(function)
        functions.setdefault(key, function)
        function_counts[key] += 1

        type_counts[type_id] += 1
        name = _value(member, "ship_type_name", "ship_name")
        if name and type_id is not None:
            type_names.setdefault(type_id, str(name))
        if function is None:
            type_unclassified[type_id] += 1

        system_id = _id(_value(member, "solar_system_id", "system_id"))
        system_counts[system_id] += 1
        system_name = _value(member, "solar_system_name", "system_name")
        if system_name and system_id is not None:
            system_names.setdefault(system_id, str(system_name))
        if _docked(member):
            docked[system_id] += 1

        if info is None:
            group_counts[None] += 1
            bridge.unknown_mass += 1
            continue
        group_counts[info.group_id] += 1
        if info.group_id is not None:
            group_names.setdefault(
                info.group_id,
                info.group_name
                or KNOWN_SHIP_GROUPS.get(info.group_id)
                or gettext("Group %(id)s") % {"id": info.group_id},
            )
        if info.group_id in excluded_groups:
            excluded_counts[info.group_id] += 1
        elif info.mass is None:
            bridge.unknown_mass += 1
        else:
            bridge.mass_kg += info.mass
            bridge.ships += 1

    by_function = [
        Row(
            key=key,
            label=str(_value(function, "name") or ""),
            count=function_counts[key],
            percent=_percent(function_counts[key], total),
            color=str(_value(function, "color") or ""),
        )
        for key, function in sorted(
            ((key, function) for key, function in functions.items() if key is not None),
            key=lambda item: _function_sort(item[1]),
        )
    ]
    if function_counts[None]:
        count = function_counts[None]
        by_function.append(Row(key=None, label=str(UNCLASSIFIED), count=count, percent=_percent(count, total)))

    def type_row(type_id, count):
        info = infos.get(type_id)
        own = classifier.resolve(
            ship_type_id=type_id,
            group_id=info.group_id if info else None,
            category_id=info.category_id if info else None,
        )
        extra = {"function_id": function_key(own), "function_name": "", "function_color": ""}
        if own is not None:
            extra.update(function_name=str(_value(own, "name") or ""), function_color=str(_value(own, "color") or ""))
        return Row(
            key=type_id,
            label=_type_label(type_id, type_names, info),
            count=count,
            percent=_percent(count, total),
            icon_url=TYPE_ICON_URL.format(type_id) if type_id else "",
            extra=extra,
        )

    by_type = _by_count(type_row(type_id, count) for type_id, count in type_counts.items())
    unclassified = _by_count(type_row(type_id, count) for type_id, count in type_unclassified.items())

    by_group = []
    if sde_available:
        by_group = _by_count(
            Row(
                key=group_id,
                label=group_names.get(group_id) or gettext("Not in the SDE"),
                count=count,
                percent=_percent(count, total),
            )
            for group_id, count in group_counts.items()
        )

    by_system = _by_count(
        Row(
            key=system_id,
            label=_system_label(system_id, system_names),
            count=count,
            percent=_percent(count, total),
            extra={"docked": docked[system_id], "fc_system": bool(fc_system_id) and system_id == fc_system_id},
        )
        for system_id, count in system_counts.items()
    )

    bridge.excluded = _by_count(
        Row(key=group_id, label=group_names.get(group_id) or gettext("Group %(id)s") % {"id": group_id}, count=count)
        for group_id, count in excluded_counts.items()
    )
    return Composition(
        total=total,
        by_function=by_function,
        by_type=by_type,
        by_group=by_group,
        by_system=by_system,
        bridge=bridge,
        sde_available=sde_available,
        unclassified=unclassified,
    )
