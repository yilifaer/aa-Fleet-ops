# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

import fleetops.urls


class FleetOpsMenu(MenuItemHook):
    def __init__(self):
        super().__init__(
            "FleetOps",
            "fas fa-users-cog fa-fw",
            "fleetops:dashboard",
            1100
        )

    def render(self, request):
        if not request.user.is_authenticated or not request.user.has_perm("fleetops.basic_access"):
            return ""
        return super().render(request)


@hooks.register("menu_item_hook")
def register_menu():
    return FleetOpsMenu()


@hooks.register("charlink")
def register_charlink_hook():
    # aa-charlink imports this module only when it is installed; FleetOps works without it.
    return "fleetops.charlink_hook"


@hooks.register("url_hook")
def register_urls():
    # The comms join page is public when the site lists "fleetops" in APPS_WITH_PUBLIC_VIEWS.
    return UrlHook(
        fleetops.urls,
        "fleetops",
        r"^fleetops/",
        excluded_views=["fleetops.public_views.comms_join"],
    )
