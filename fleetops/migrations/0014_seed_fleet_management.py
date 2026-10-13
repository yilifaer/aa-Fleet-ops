# AA FleetOps 0.1.0a6: seed the default ship functions of the fleet composition and the "Standard" fleet
# structure template. Tables that already hold rows are left alone, so functions or templates an admin
# deleted are not created again; "manage.py fleetops_seed" adds them to empty tables.

from django.db import migrations

# (sort order, name, color, fleet positions, default, group rules, type rules); rules are (SDE id, English
# name) pairs, the name is stored as the rule label. fleetops/fleet_defaults.py holds the same values.
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


def seed_ship_functions(apps, schema_editor):
    ShipFunction = apps.get_model("fleetops", "ShipFunction")
    ShipFunctionRule = apps.get_model("fleetops", "ShipFunctionRule")
    if ShipFunction.objects.exists():
        return
    for sort_order, name, color, fleet_roles, is_default, groups, types in DEFAULT_SHIP_FUNCTIONS:
        function = ShipFunction.objects.create(
            name=name, color=color, fleet_roles=fleet_roles, is_default=is_default, sort_order=sort_order
        )
        ShipFunctionRule.objects.bulk_create(
            ShipFunctionRule(function=function, kind=kind, target_id=target_id, label=label)
            for kind, rules in (("group", groups), ("type", types))
            for target_id, label in rules
        )


def seed_structure_templates(apps, schema_editor):
    FleetStructureTemplate = apps.get_model("fleetops", "FleetStructureTemplate")
    if FleetStructureTemplate.objects.exists():
        return
    for name, layout, description, sort_order in DEFAULT_STRUCTURE_TEMPLATES:
        FleetStructureTemplate.objects.create(name=name, layout=layout, description=description, sort_order=sort_order)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0013_fleet_management"),
    ]

    # Fleets, members and settings keep the defaults of 0013: no wings read yet, docking and fleet warp
    # unknown until the next poll, the titan bridge exclusions and a 15 second live refresh.
    operations = [
        migrations.RunPython(seed_ship_functions, migrations.RunPython.noop),
        migrations.RunPython(seed_structure_templates, migrations.RunPython.noop),
    ]
