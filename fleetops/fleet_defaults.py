"""Default ship functions and fleet structure templates, and the English names of ship groups.

``fleetops_seed`` creates them on installations whose tables are empty. Migration 0014 keeps its
own copy of these values; both must stay identical.
"""

# (sort order, name, color, fleet positions, default, group rules, type rules). Rules are
# (SDE id, English name) pairs; the name is stored as the rule label.
DEFAULT_SHIP_FUNCTIONS = (
    (10, "Fleet Command", "#dc3545", "fleet_commander", False, ((1972, "Flag Cruiser"),), ()),
    (20, "DPS", "#0d6efd", "", True, (), ()),
    (
        30,
        "Logistics",
        "#198754",
        "",
        False,
        ((832, "Logistics"), (1527, "Logistics Frigate"), (1538, "Force Auxiliary")),
        (),
    ),
    (
        40,
        "Tackle / EWAR",
        "#fd7e14",
        "",
        False,
        ((831, "Interceptor"), (893, "Electronic Attack Ship"), (833, "Force Recon Ship"), (906, "Combat Recon Ship")),
        (),
    ),
    (50, "Interdictors", "#6f42c1", "", False, ((541, "Interdictor"), (894, "Heavy Interdiction Cruiser")), ()),
    (
        60,
        "Boosts",
        "#20c997",
        "",
        False,
        ((540, "Command Ship"), (941, "Industrial Command Ship"), (4902, "Expedition Command Ship")),
        (),
    ),
    (70, "Command Destroyers", "#0dcaf0", "", False, ((1534, "Command Destroyer"),), ()),
    (
        80,
        "Support",
        "#adb5bd",
        "",
        False,
        (
            (830, "Covert Ops"),
            (28, "Hauler"),
            (380, "Deep Space Transport"),
            (1202, "Blockade Runner"),
            (31, "Shuttle"),
            (1283, "Expedition Frigate"),
            (1022, "Prototype Exploration Ship"),
            (463, "Mining Barge"),
            (543, "Exhumer"),
            (513, "Freighter"),
            (902, "Jump Freighter"),
            (883, "Capital Industrial Ship"),
        ),
        (),
    ),
    # Admins add the hulls their cyno alts fly.
    (90, "Cyno", "#ffc107", "", False, (), ()),
    (100, "Newbro", "#d63384", "", False, ((237, "Corvette"),), ()),
    (
        110,
        "Capsules",
        "#6c757d",
        "",
        False,
        ((29, "Capsule"),),
        ((670, "Capsule"), (33328, "Capsule - Genolution 'Auroral' 197-variant")),
    ),
)

# (name, layout, description, sort order)
DEFAULT_STRUCTURE_TEMPLATES = (
    (
        "Standard",
        "Main: Alpha, Bravo, Charlie\nLogi: Logi\nBoosts: Boosts\nTackle: Tackle",
        "Main fleet in three squads, logistics, boosts and tackle.",
        10,
    ),
)

# Ship groups (SDE category 6) by id, in English, for pages that name them without django-eveonline-sde.
# Ship and system names are always English.
KNOWN_SHIP_GROUPS = {
    25: "Frigate",
    26: "Cruiser",
    27: "Battleship",
    28: "Hauler",
    29: "Capsule",
    30: "Titan",
    31: "Shuttle",
    237: "Corvette",
    324: "Assault Frigate",
    358: "Heavy Assault Cruiser",
    380: "Deep Space Transport",
    419: "Combat Battlecruiser",
    420: "Destroyer",
    463: "Mining Barge",
    485: "Dreadnought",
    513: "Freighter",
    540: "Command Ship",
    541: "Interdictor",
    543: "Exhumer",
    547: "Carrier",
    659: "Supercarrier",
    830: "Covert Ops",
    831: "Interceptor",
    832: "Logistics",
    833: "Force Recon Ship",
    834: "Stealth Bomber",
    883: "Capital Industrial Ship",
    893: "Electronic Attack Ship",
    894: "Heavy Interdiction Cruiser",
    898: "Black Ops",
    900: "Marauder",
    902: "Jump Freighter",
    906: "Combat Recon Ship",
    941: "Industrial Command Ship",
    963: "Strategic Cruiser",
    1022: "Prototype Exploration Ship",
    1201: "Attack Battlecruiser",
    1202: "Blockade Runner",
    1283: "Expedition Frigate",
    1305: "Tactical Destroyer",
    1527: "Logistics Frigate",
    1534: "Command Destroyer",
    1538: "Force Auxiliary",
    1972: "Flag Cruiser",
    4594: "Lancer Dreadnought",
    4902: "Expedition Command Ship",
    5087: "Special Edition Yachts",
    5120: "Command Carrier",
}


def seed_ship_functions(ShipFunction, ShipFunctionRule) -> bool:
    """Create the default functions and their rules when no function exists; returns whether it did."""
    if ShipFunction.objects.exists():
        return False
    for sort_order, name, color, fleet_roles, is_default, groups, types in DEFAULT_SHIP_FUNCTIONS:
        function = ShipFunction.objects.create(
            name=name, color=color, fleet_roles=fleet_roles, is_default=is_default, sort_order=sort_order
        )
        ShipFunctionRule.objects.bulk_create(
            ShipFunctionRule(function=function, kind=kind, target_id=target_id, label=label)
            for kind, rules in (("group", groups), ("type", types))
            for target_id, label in rules
        )
    return True


def seed_structure_templates(FleetStructureTemplate) -> bool:
    """Create the default structure templates when no template exists; returns whether it did."""
    if FleetStructureTemplate.objects.exists():
        return False
    for name, layout, description, sort_order in DEFAULT_STRUCTURE_TEMPLATES:
        FleetStructureTemplate.objects.create(
            name=name, layout=layout, description=description, sort_order=sort_order
        )
    return True
