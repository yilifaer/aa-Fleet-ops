# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps pages for "not allowed" (403) and "not found" (404), with the reason in the viewer's language.

Alliance Auth's own error pages ignore the text of an exception, so a reason can only reach the viewer through
a page FleetOps renders itself. JSON and fragment endpoints and POST-only views keep raising
``PermissionDenied``/``Http404`` (see ``fleetops.decorators``).
"""

from django.utils.translation import gettext_lazy as _

# One sentence per FleetOps permission: why a page that needs it is not open to the viewer.
PERMISSION_REASONS = {
    "fleetops.basic_access": _(
        "Your account has no access to FleetOps. Ask an administrator for the FleetOps member permission."
    ),
    "fleetops.start_fleet": _("Only FCs can start fleets."),
    "fleetops.manage_own_fleet": _("Only FCs can manage fleets."),
    "fleetops.view_all_fleets": _("Only fleet managers can see all fleets."),
    "fleetops.create_manual_fleet": _("Only FCs with the manual fleet permission can add manual fleets."),
    "fleetops.manage_fleets": _("Only FC leads can open this page."),
    "fleetops.view_corp_stats": _("Only corporation statistics managers can open this page."),
    "fleetops.view_all_stats": _("Only alliance statistics managers can open this page."),
    "fleetops.manage_attendance": _("Only PAP managers can open this page."),
    "fleetops.manage_incentives": _("Only FC incentive managers can open this page."),
    "fleetops.manage_configuration": _("Only FleetOps administrators can open the administration."),
    "fleetops.view_audit_log": _("Only auditors can open the audit log."),
    "fleetops.request_srp": _("Your account cannot request SRP."),
    "fleetops.manage_srp": _("Only SRP managers can open this page."),
}
# The group decorators: FCs or the leaders of configured Alliance Auth groups.
STARTER_REASON = _("Only FCs and leaders of groups that run fleets can start fleets.")
PING_SENDER_REASON = _("Only FCs and group leaders can send pings.")
FLEET_MANAGER_REASON = _("Only FCs who manage fleets can open this page.")
# Every 404 of a FleetOps page that has no reason of its own. It never names the object, its kind or whether it
# exists, so an unknown ID and an object the viewer may not see look the same.
NOT_FOUND_REASON = _("This page does not exist, or you cannot open it.")
# Object scopes (404 with a reason, the same for an ID that exists and one that does not).
OWN_CORPORATION_STATISTICS_REASON = _("You can only open the statistics of your own corporation.")
OWN_CORPORATION_MEMBERS_REASON = _("You can only open members of your own corporation.")
OWN_FLEETS_SUMMARY_REASON = _("You can only open the summary of your own fleets.")
LEADERBOARD_REASON = _("The leaderboard is only open to statistics managers.")
EXPORT_REASON = _("Your account cannot download this export.")


def reason_for(permission) -> str:
    """The sentence for ``permission``; a general one for a permission without its own."""
    return PERMISSION_REASONS.get(permission, PERMISSION_REASONS["fleetops.basic_access"])


def _page(request, *, status, title, reason, permission=""):
    # Imported here: fleetops.views imports the decorators, which import this module.
    from fleetops.views import _render

    response = _render(
        request,
        "fleetops/denied.html",
        {"error_title": title, "error_reason": reason, "error_permission": permission, "error_status": status},
    )
    response.status_code = status
    return response


def denied(request, reason, *, permission=""):
    """The FleetOps 403 page with ``reason`` and, when given, the permission code in small text."""
    return _page(request, status=403, title=_("Not allowed"), reason=reason, permission=permission)


def not_found(request, reason=NOT_FOUND_REASON):
    """The FleetOps 404 page with ``reason``."""
    return _page(request, status=404, title=_("Not found"), reason=reason)
