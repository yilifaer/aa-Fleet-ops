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


@hooks.register("url_hook")
def register_urls():
    return UrlHook(fleetops.urls, "fleetops", r"^fleetops/")
