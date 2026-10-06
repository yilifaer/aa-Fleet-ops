from decimal import Decimal

from django.core.management.base import BaseCommand

from fleetops.constants import DEFAULT_MOTD_TEMPLATE, DEFAULT_PING_TEMPLATE
from fleetops.models import FleetOpsSettings, FleetType, MessageTemplate, PingTarget


class Command(BaseCommand):
    help = "Create FleetOps defaults; --demo also creates example Fleet Types for test installations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--demo",
            action="store_true",
            help="Create PCT, StratOps and CTA example fleet types plus a manual-copy ping target.",
        )

    def handle(self, *args, **options):
        FleetOpsSettings.get_solo()
        MessageTemplate.objects.update_or_create(
            template_type=MessageTemplate.TemplateType.PING,
            name="Default Ping",
            defaults={"content": DEFAULT_PING_TEMPLATE, "is_default": True, "is_active": True},
        )
        MessageTemplate.objects.update_or_create(
            template_type=MessageTemplate.TemplateType.MOTD,
            name="Default MOTD",
            defaults={"content": DEFAULT_MOTD_TEMPLATE, "is_default": True, "is_active": True},
        )
        self.stdout.write(self.style.SUCCESS("FleetOps settings and default message templates are ready."))

        if options["demo"]:
            for order, name, short, weight in (
                (10, "Peacetime", "PCT", Decimal("0.50")),
                (20, "Strategic Operation", "StratOps", Decimal("1.00")),
                (30, "Call To Arms", "CTA", Decimal("1.50")),
            ):
                FleetType.objects.update_or_create(
                    name=name,
                    defaults={
                        "short_name": short,
                        "point_weight": weight,
                        "is_active": True,
                        "sort_order": order,
                    },
                )
            PingTarget.objects.get_or_create(
                name="Manual / Copy Only",
                defaults={"target_value": "", "is_active": True},
            )
            self.stdout.write(
                self.style.WARNING(
                    "Demo data created: PCT 0.5 / StratOps 1.0 / CTA 1.5 and a manual-copy ping target."
                )
            )
