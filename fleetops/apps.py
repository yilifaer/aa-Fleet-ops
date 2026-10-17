# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.apps import AppConfig
from django.utils.translation import pgettext_lazy


class FleetOpsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fleetops"
    verbose_name = pgettext_lazy("FleetOps", "Fleet Operations")

    def ready(self):
        from fleetops import signals  # noqa: F401 - connects the receivers
