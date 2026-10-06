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
            help="Create PCT, StratOps and CTA example fleet types plus a manual-copy ping target. Existing entries are kept.",
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
        self.stdout.write(self.style.SUCCESS("FleetOps settings and default message templates are ready."))

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
            PingTarget.objects.get_or_create(
                name="Manual / Copy Only",
                defaults={"target_value": "", "is_active": True},
            )
            self.stdout.write(
                self.style.WARNING(
                    "Demo data created: PCT 0.5 / StratOps 1.0 / CTA 1.5 and a manual-copy ping target "
                    "(existing entries are kept as configured)."
                )
            )
