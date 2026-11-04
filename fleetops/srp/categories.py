# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Which SRP category a claim belongs to. ``resolve_category`` is the only place that decides it."""

from fleetops import texts
from fleetops.models import SrpCategory, SrpSettings
from fleetops.srp.types import SrpError

IN_FLEET = "in_fleet"
KIND_STANDING = "standing"


def system_category() -> SrpCategory | None:
    """The built-in fleet category; None only when it was removed from the database by hand."""
    return SrpCategory.objects.filter(key=SrpCategory.SYSTEM_KEY).first()


def selectable_categories():
    """The categories a member can choose for a loss outside a fleet."""
    return SrpCategory.objects.filter(is_active=True, member_selectable=True)


def category_for_fleet_type(fleet_type_id) -> SrpCategory | None:
    """The active category that the fleet type is mapped to, if any."""
    if not fleet_type_id:
        return None
    return SrpCategory.objects.filter(is_active=True, fleet_types=fleet_type_id).order_by("sort_order", "pk").first()


def default_category(settings_obj=None) -> SrpCategory | None:
    """The category of a loss outside a fleet when nobody chose one: the default category of the settings if it
    can still be used."""
    srp = settings_obj or SrpSettings.get_solo()
    if srp.default_category_id is None:
        return None
    default = srp.default_category
    return default if default.is_active and default.member_selectable else None


def resolve_category(*, scope, link, operation, chosen, settings_obj=None) -> SrpCategory | None:
    """The category of a claim; every way of making or changing a claim goes through here.

    1. A standing link gives its own category.
    2. A loss in a fleet gets the category its fleet type is mapped to, else the built-in ``fleet`` category.
    3. A loss outside a fleet gets the ``chosen`` category (it has to be active and one that members can choose),
       else the default category of the settings, else none; none is refused while the settings require a
       category.

    ``link`` is the fleet link the claim is made through (None outside a fleet), ``operation`` the fleet of an
    in-fleet claim that has no link at hand. Raises ``SrpError`` with the codes ``category_required`` and
    ``category_unavailable``.
    """
    if link is not None and getattr(link, "kind", "") == KIND_STANDING:
        return link.category
    if scope == IN_FLEET:
        if operation is None and link is not None:
            operation = link.operation
        mapped = category_for_fleet_type(getattr(operation, "fleet_type_id", None))
        return mapped or system_category()

    srp = settings_obj or SrpSettings.get_solo()
    if chosen is not None:
        if not (chosen.is_active and chosen.member_selectable):
            raise SrpError("category_unavailable", texts.SRP_CATEGORY_UNAVAILABLE())
        return chosen
    default = default_category(srp)
    if default is not None:
        return default
    if srp.out_of_fleet_category_required:
        raise SrpError("category_required", texts.SRP_CATEGORY_REQUIRED())
    return None
