from decimal import Decimal

from django.core.management.base import BaseCommand

from fleetops.constants import DEFAULT_MOTD_TEMPLATE, DEFAULT_PING_TEMPLATE
from fleetops.fleet_defaults import seed_ship_functions, seed_structure_templates
from fleetops.models import (
    FleetOpsSettings,
    FleetStructureTemplate,
    FleetType,
    MessageTemplate,
    MultiplierTier,
    ShipFunction,
    ShipFunctionRule,
    SpecialRole,
)

# (threshold in minutes, multiplier); only created when no tier exists.
DEFAULT_MULTIPLIER_TIERS = ((90, 2), (180, 3))
# (key, name, gives FC credit, sort order); existing roles are kept as configured.
DEFAULT_SPECIAL_ROLES = (
    ("backseat_fc", "Back Seat FC", True, 10),
    ("logi_anchor", "Logi Anchor", False, 20),
    ("snowflake", "Snowflake Member", False, 30),
)


class Command(BaseCommand):
    help = "Create FleetOps defaults; --demo also creates example Fleet Types for test installations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--demo",
            action="store_true",
            help="Create PCT, StratOps and CTA example fleet types. Existing entries are kept.",
        )

    def handle(self, *args, **options):
        FleetOpsSettings.get_solo()
        # Never overwrite configuration that already exists; only fill in what is missing.
        for template_type, name, content in (
            (MessageTemplate.TemplateType.PING, "Default Ping", DEFAULT_PING_TEMPLATE),
            (MessageTemplate.TemplateType.MOTD, "Default MOTD", DEFAULT_MOTD_TEMPLATE),
        ):
            has_default = MessageTemplate.objects.filter(template_type=template_type, is_default=True).exists()
            MessageTemplate.objects.get_or_create(
                template_type=template_type,
                name=name,
                defaults={"content": content, "is_default": not has_default, "is_active": True},
            )
        # The tiers are one set: a partly configured set is never topped up.
        if not MultiplierTier.objects.exists():
            for minutes, multiplier in DEFAULT_MULTIPLIER_TIERS:
                MultiplierTier.objects.create(threshold_minutes=minutes, multiplier=multiplier)
        for key, name, grants_fc_credit, sort_order in DEFAULT_SPECIAL_ROLES:
            if not SpecialRole.objects.filter(key=key).exists() and not SpecialRole.objects.filter(name=name).exists():
                SpecialRole.objects.create(
                    key=key, name=name, grants_fc_credit=grants_fc_credit, sort_order=sort_order
                )
        # Ship functions and structure templates are added only to empty tables.
        seed_ship_functions(ShipFunction, ShipFunctionRule)
        seed_structure_templates(FleetStructureTemplate)
        self.stdout.write(
            self.style.SUCCESS(
                "FleetOps settings, default message templates, multiplier tiers, special roles, ship functions "
                "and fleet structure templates are ready."
            )
        )

        if options["demo"]:
            for order, name, short, weight in (
                (10, "Peacetime", "PCT", Decimal("0.50")),
                (20, "Strategic Operation", "StratOps", Decimal("1.00")),
                (30, "Call To Arms", "CTA", Decimal("1.50")),
            ):
                FleetType.objects.get_or_create(
                    name=name,
                    defaults={
                        "short_name": short,
                        "point_weight": weight,
                        "is_active": True,
                        "sort_order": order,
                    },
                )
            # No demo ping channel: a channel needs a real Discord webhook, and without one the ping is
            # copied by hand anyway.
            self.stdout.write(
                self.style.WARNING(
                    "Demo data created: PCT 0.5 / StratOps 1.0 / CTA 1.5 "
                    "(existing entries are kept as configured)."
                )
            )
