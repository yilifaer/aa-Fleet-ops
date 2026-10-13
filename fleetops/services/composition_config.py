"""The ship function mapping: names of ship types and groups, and changes made from the fleet composition.

Rules point at SDE ids (``ShipFunctionRule.target_id``) and keep the English name they had when they
were saved. Names come from django-eveonline-sde when it is installed; ship groups also have a
built-in English list, so the configuration pages and the bridge setting can name them without it.
"""

from django.db import IntegrityError, transaction

from fleetops.fleet_defaults import KNOWN_SHIP_GROUPS
from fleetops.models import MAX_DATABASE_ID, FleetMemberState, ShipFunction, ShipFunctionRule
from fleetops.services import sde
from fleetops.services.audit import audit, config_snapshot
from fleetops.services.fleet_manage import FleetActionResult
from fleetops.services.fleet_rules import SHIP_CATEGORY_ID, FleetRuleError, parse_id_list

TYPE, GROUP = ShipFunctionRule.Kind.TYPE, ShipFunctionRule.Kind.GROUP
KIND_LABELS = {TYPE: "Ship type", GROUP: "Ship group"}

FUNCTION_GONE = "That ship function no longer exists or is disabled."
CLASSIFY_REASON = "Classified from a fleet's composition"


class SDEUnavailable(Exception):
    """django-eveonline-sde is not installed or could not be read."""


def default_label(kind, target_id) -> str:
    return f"{'Type' if kind == TYPE else 'Group'} {target_id}"


def group_names(group_ids) -> dict:
    """English names of ship groups by id: the SDE first, then the built-in list."""
    ids = {int(group_id) for group_id in group_ids}
    names = {group_id: KNOWN_SHIP_GROUPS[group_id] for group_id in ids if group_id in KNOWN_SHIP_GROUPS}
    names.update({group_id: name for group_id, name in sde.item_group_names(ids).items() if name})
    return names


def type_name(type_id) -> str:
    """The English name of a ship type: the SDE first, then the ship name a tracked member had; "" if unknown."""
    name = sde.item_type_names([type_id]).get(type_id, "")
    if name:
        return name
    return (
        FleetMemberState.objects.filter(ship_type_id=type_id)
        .exclude(ship_type_name="")
        .order_by("-last_seen")
        .values_list("ship_type_name", flat=True)
        .first()
        or ""
    )


def target_label(kind, target_id) -> str:
    """The English name of a rule's ship type or group, or "" when nothing knows it."""
    if kind == GROUP:
        return group_names([target_id]).get(int(target_id), "")
    return type_name(int(target_id))


def find_target(kind, name):
    """``(id, English name)`` of the ship type or group called ``name``, or None.

    Ship groups are also found in the built-in list. Raises SDEUnavailable when a ship type is
    looked up without django-eveonline-sde, or when the SDE cannot be read.
    """
    text = " ".join(str(name or "").split())
    known = None
    if kind == GROUP:
        known = next((item for item in KNOWN_SHIP_GROUPS.items() if item[1].lower() == text.lower()), None)
    if not sde.sde_installed():
        if kind == GROUP:
            return known
        raise SDEUnavailable
    lookup = sde.find_item_group if kind == GROUP else sde.find_item_type
    try:
        found = lookup(text, SHIP_CATEGORY_ID, strict=True)
    except Exception as exc:
        if known:
            return known
        raise SDEUnavailable from exc
    return found or known


def describe_group_ids(value) -> str:
    """Ship group ids as people read them: "Titan (30), Dreadnought (485), Group 99999 (99999)".

    ``value`` is a stored id list such as ``FleetOpsSettings.bridge_excluded_group_ids``; an
    unreadable one gives "".
    """
    try:
        ids = parse_id_list(value)
    except FleetRuleError:
        return ""
    names = group_names(ids)
    return ", ".join(f"{names.get(group_id) or default_label(GROUP, group_id)} ({group_id})" for group_id in ids)


def rule_text(rule) -> str:
    """"Ship group 832 (Logistics)" for messages."""
    text = f"{KIND_LABELS.get(rule.kind, 'Ship')} {rule.target_id}"
    return f"{text} ({rule.label})" if rule.label else text


def _save_type_rule(type_id, function, label) -> tuple:
    """``(audit action, rule, snapshot before the change)``; the action is "" when the rule was there already."""
    with transaction.atomic():
        rule = ShipFunctionRule.objects.select_for_update().filter(kind=TYPE, target_id=type_id).first()
        if rule is None:
            rule = ShipFunctionRule.objects.create(function=function, kind=TYPE, target_id=type_id, label=label)
            return "configuration.create", rule, None
        if rule.function_id == function.pk:
            return "", rule, None
        old = config_snapshot(rule)
        rule.function = function
        rule.label = label or rule.label
        rule.save(update_fields=["function", "label"])
        return "configuration.update", rule, old


def classify_type(type_id, function_id, *, actor) -> FleetActionResult:
    """Count a ship type under a ship function (creates or updates its "type" rule; audited)."""
    type_id, function_id = int(type_id), int(function_id)
    if not 0 < type_id <= MAX_DATABASE_ID:
        return FleetActionResult(False, "That ship type does not exist.", level="error", status=400)
    function = ShipFunction.objects.filter(pk=function_id, is_active=True).first()
    if function is None:
        return FleetActionResult(False, FUNCTION_GONE, level="error", status=404)
    label = type_name(type_id)
    ship = label or default_label(TYPE, type_id)
    data = {"type_id": type_id, "function_id": function.pk}
    try:
        action, rule, old = _save_type_rule(type_id, function, label)
    except IntegrityError:
        # Someone else mapped the type at the same moment: change their rule instead.
        action, rule, old = _save_type_rule(type_id, function, label)
    if not action:
        return FleetActionResult(True, f"{ship} already counts as {function.name}.", level="info", data=data)
    audit(actor, action, rule, old, config_snapshot(rule), reason=CLASSIFY_REASON)
    return FleetActionResult(True, f"{ship} now counts as {function.name}.", data=data)
