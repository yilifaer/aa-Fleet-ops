# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Losses SRP does not cover: ship groups, ships and kinds of space the alliance excluded in the SRP settings.

``submission_blocks`` is a pure function of a loss and the settings (no database, no network), so the claim check,
the list of discovered losses and the pages can all use it. A value that is not known (the loss has no security
status or ship group because the lookup failed) never blocks and never adds a warning.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from django.utils.translation import gettext, gettext_lazy, pgettext_lazy

from fleetops.models import SECURITY_BANDS  # noqa: F401 - the band names are part of this module's interface
from fleetops.services.fleet_rules import FleetRuleError, parse_id_list

HIGHSEC, LOWSEC, NULLSEC, WORMHOLE, ABYSSAL, POCHVEN = SECURITY_BANDS

# Security status from which a system is high security: EVE shows 0.5 for 0.45 and above.
HIGHSEC_FROM = Decimal("0.45")
WORMHOLE_SYSTEMS = range(31_000_000, 32_000_000)
ABYSSAL_SYSTEMS = range(32_000_000, 33_000_000)
POCHVEN_REGION_ID = 10_000_070

# The recommended lists of the "Fill in the recommended list" button: mining, industrial and exploration ship groups
# (Mining Barge, Exhumer, Hauler, Deep Space Transport, Blockade Runner, Freighter, Jump Freighter, Industrial Command
# Ship, Capital Industrial Ship, Expedition Frigate, Expedition Command Ship, Prototype Exploration Ship), the
# ships of the general frigate group that are built for the same work (Venture, Heron, Imicus, Magnate, Probe) and
# high security. Nothing is blocked until an administrator saves them.
RECOMMENDED_SHIP_GROUP_IDS = (463, 543, 28, 380, 1202, 513, 902, 941, 883, 1283, 4902, 1022)
RECOMMENDED_SHIP_TYPE_IDS = (32880, 605, 607, 29248, 586)
RECOMMENDED_SECURITY_BANDS = (HIGHSEC,)

BAND_LABELS = {
    HIGHSEC: gettext_lazy("high security"),
    LOWSEC: gettext_lazy("low security"),
    NULLSEC: gettext_lazy("null security"),
    WORMHOLE: gettext_lazy("wormhole"),
    ABYSSAL: gettext_lazy("abyssal"),
    POCHVEN: gettext_lazy("Pochven"),
}
# The names of the bands as the settings page lists them.
BAND_CHOICES = (
    (HIGHSEC, pgettext_lazy("security band", "High security (0.5 and above)")),
    (LOWSEC, pgettext_lazy("security band", "Low security (0.1 to 0.4)")),
    (NULLSEC, pgettext_lazy("security band", "Null security (0.0 and below)")),
    (WORMHOLE, pgettext_lazy("security band", "Wormhole space")),
    (ABYSSAL, pgettext_lazy("security band", "Abyssal space")),
    (POCHVEN, pgettext_lazy("security band", "Pochven")),
)


class Loss(Protocol):
    """What the checks read from a loss: a stored killmail or a discovered loss."""

    ship_type_id: int
    ship_group_id: int | None
    solar_system_id: int
    solar_system_name: str
    solar_system_security: Decimal | None
    region_id: int | None
    war_id: int | None


def security_band(loss) -> str | None:
    """The kind of space the loss happened in, or None when it is not known.

    Wormhole and abyssal systems and Pochven are told apart by their IDs; the rest follow the security status:
    0.45 and above is high security, above 0.0 low security, the rest null security.
    """
    system_id = getattr(loss, "solar_system_id", None)
    if isinstance(system_id, int):
        if system_id in WORMHOLE_SYSTEMS:
            return WORMHOLE
        if system_id in ABYSSAL_SYSTEMS:
            return ABYSSAL
    if getattr(loss, "region_id", None) == POCHVEN_REGION_ID:
        return POCHVEN
    security = getattr(loss, "solar_system_security", None)
    if security is None:
        return None
    security = Decimal(security)
    if security >= HIGHSEC_FROM:
        return HIGHSEC
    return LOWSEC if security > 0 else NULLSEC


def format_security(value) -> str:
    """A security status as EVE shows it: one decimal, rounded half up, and never 0.0 for a low security system
    (0.01 to 0.04 show as 0.1). '' when the value is not known."""
    if value is None:
        return ""
    number = Decimal(value)
    shown = number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if number > 0 and shown == 0:
        shown = Decimal("0.1")
    return f"{shown:.1f}"


def band_label(band) -> str:
    label = BAND_LABELS.get(band)
    return str(label) if label is not None else str(band or "")


def parse_ids(value) -> set[int]:
    """The IDs of a stored id list; an unreadable list gives none (nothing is blocked by it)."""
    try:
        return set(parse_id_list(value))
    except FleetRuleError:
        return set()


def parse_id_list_ordered(value) -> list[int]:
    """The IDs of a stored id list in their order; an unreadable list gives []."""
    try:
        return parse_id_list(value)
    except FleetRuleError:
        return []


def blocked_bands(value) -> set[str]:
    """The security bands of the stored comma list, without names this version does not know."""
    return {band for band in str(value or "").replace(" ", "").split(",") if band in SECURITY_BANDS}


def format_bands(bands) -> str:
    """The stored form of a set of bands: comma separated, in the order of ``SECURITY_BANDS``."""
    chosen = set(bands)
    return ",".join(band for band in SECURITY_BANDS if band in chosen)


def has_blocks(srp) -> bool:
    """Whether the settings refuse anything at all."""
    return bool(
        parse_ids(srp.blocked_ship_group_ids) or parse_ids(srp.blocked_ship_type_ids) or blocked_bands(srp.blocked_security_bands)
    )


def ship_block_ids(srp) -> tuple[set[int], set[int]]:
    """``(ship group IDs, ship type IDs)`` the settings refuse."""
    return parse_ids(srp.blocked_ship_group_ids), parse_ids(srp.blocked_ship_type_ids)


def ship_is_blocked(loss, srp) -> bool:
    """Whether the ship (its type or its group) is on the block lists, before a payout table entry lifts it."""
    groups, types = ship_block_ids(srp)
    if loss.ship_type_id in types:
        return True
    group_id = getattr(loss, "ship_group_id", None)
    return group_id is not None and group_id in groups


def submission_blocks(
    loss, srp, *, scope=None, operation=None, category=None, rule=None, group_names=None
) -> list[str]:
    """The reasons (texts for the member) why a loss is not covered, in the order ship type, ship group, space.

    ``rule`` is the payout table entry that applies to the loss (the caller matched it for its scope, fleet and
    category): an entry for this very ship lifts the ship blocks while ``payout_rule_lifts_ship_block`` is on.
    Security bands are never lifted. A loss in a war in high security is let through when
    ``highsec_war_losses_covered`` is on. ``scope``, ``operation`` and ``category`` name what the rule was matched
    for; they do not change the answer here. ``group_names`` maps ship group IDs to names (see ``blocked_group_names``);
    a group missing from it is shown as 'Group <id>'. Reads no database and calls no network.
    """
    found = []
    groups, types = ship_block_ids(srp)
    lifted = bool(
        srp.payout_rule_lifts_ship_block
        and rule is not None
        and getattr(rule, "ship_type_id", None) == loss.ship_type_id
    )
    if not lifted:
        if loss.ship_type_id in types:
            ship = getattr(loss, "ship_type_name", "") or gettext("Type %(id)s") % {"id": loss.ship_type_id}
            found.append(gettext("%(ship)s losses are not covered.") % {"ship": ship})
        group_id = getattr(loss, "ship_group_id", None)
        if group_id is not None and group_id in groups:
            found.append(gettext("%(group)s ships are not covered.") % {"group": group_label(group_id, group_names)})
    band = security_band(loss)
    if band is not None and band in blocked_bands(srp.blocked_security_bands):
        war = getattr(loss, "war_id", None) is not None
        if not (band == HIGHSEC and war and srp.highsec_war_losses_covered):
            system = getattr(loss, "solar_system_name", "") or ""
            security = format_security(getattr(loss, "solar_system_security", None))
            if system or security:
                found.append(
                    gettext("Losses in %(band)s space are not covered (%(system)s %(security)s).")
                    % {"band": band_label(band), "system": system, "security": security}
                )
            else:
                found.append(gettext("Losses in %(band)s space are not covered.") % {"band": band_label(band)})
    return found


def group_label(group_id, names=None) -> str:
    """The name of a ship group for a message from the mapping ``names``; 'Group <id>' when it has none. Pure."""
    return (names or {}).get(int(group_id)) or gettext("Group %(id)s") % {"id": group_id}


def blocked_group_names(losses, srp) -> dict[int, str]:
    """English names of the blocked ship groups the ``losses`` are in: one lookup for any number of losses, none
    when no loss is in a blocked group. Reads the SDE when it is installed; pass the result to ``submission_blocks``."""
    from fleetops.services.composition_config import group_names

    blocked = parse_ids(srp.blocked_ship_group_ids)
    wanted = {
        group_id
        for group_id in (getattr(loss, "ship_group_id", None) for loss in losses)
        if group_id is not None and group_id in blocked
    }
    return group_names(sorted(wanted)) if wanted else {}


# English names of the ships the recommended list names, for pages that cannot look them up.
KNOWN_SHIP_NAMES = {32880: "Venture", 605: "Heron", 607: "Imicus", 29248: "Magnate", 586: "Probe"}


def ship_names(type_ids) -> dict[int, str]:
    """English names of ship types for a page: the SDE, then the names SRP and the name cache know, then the five
    recommended ships. Never calls ESI."""
    from fleetops.models import EveEntityName
    from fleetops.services import sde

    ids = list(dict.fromkeys(type_ids))
    names = {type_id: KNOWN_SHIP_NAMES[type_id] for type_id in ids if type_id in KNOWN_SHIP_NAMES}
    names.update({type_id: name for type_id, name in sde.item_type_names(ids).items() if name})
    missing = [type_id for type_id in ids if type_id not in names]
    if missing:
        names.update(
            {row.id: row.name for row in EveEntityName.objects.filter(id__in=missing).exclude(name="")}
        )
    return names


def member_notice(srp) -> dict | None:
    """What members are told is not covered, for the "Not covered" card: ship groups, ships, kinds of space and the
    capsule rule. None when nothing is refused and the capsule switch is off. Reads the SDE and the name cache,
    never ESI."""
    from fleetops.services.composition_config import group_names

    group_ids = parse_id_list_ordered(srp.blocked_ship_group_ids)
    type_ids = parse_id_list_ordered(srp.blocked_ship_type_ids)
    bands = blocked_bands(srp.blocked_security_bands)
    if not (group_ids or type_ids or bands or srp.capsules_need_rule):
        return None
    groups = group_names(group_ids) if group_ids else {}
    ships = ship_names(type_ids) if type_ids else {}
    return {
        "groups": [groups.get(group_id) or gettext("Group %(id)s") % {"id": group_id} for group_id in group_ids],
        "ships": [ships.get(type_id) or gettext("Type %(id)s") % {"id": type_id} for type_id in type_ids],
        "bands": [band_label(band) for band in SECURITY_BANDS if band in bands],
        "capsules": bool(srp.capsules_need_rule),
        "lifted_by_rule": bool(srp.payout_rule_lifts_ship_block and (group_ids or type_ids)),
        "war_covered": bool(srp.highsec_war_losses_covered and HIGHSEC in bands),
    }
