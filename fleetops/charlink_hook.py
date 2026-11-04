# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""aa-charlink integration: pilots add the FleetOps ESI scopes when they link characters with CharLink.

CharLink imports this module through the ``charlink`` hook in ``auth_hooks.py``, so it is only loaded
where CharLink is installed. CharLink checks that ``"fleetops"`` is listed literally in INSTALLED_APPS.
Nothing is stored when a character is added: FleetOps reads the tokens when an FC starts a fleet, a
pilot registers through a PAP link or the loss scan reads the recent losses of a character.
"""

from charlink.app_imports.utils import AppImport, LoginImport
from django.db.models import Exists, OuterRef
from django.utils.translation import gettext_lazy as _
from esi.models import Token

from fleetops.constants import CLICK_SCOPES, FLEET_SCOPES, LOSS_SCOPES
from fleetops.services.permission_users import users_with_any_perm


def _add_character(request, token):
    """CharLink's SSO already saved the token; FleetOps needs nothing else."""


def _add_loss_character(request, token):
    """CharLink's SSO already saved the token; the character is read in the next scan run."""
    from fleetops.srp import discovery

    discovery.mark_due([token.character_id])


def _tokens_with(scopes):
    # One join per scope, so a token needs all of them. Built lazily: Token.require_scopes() reads the
    # scope table at once, and this module is imported when CharLink loads its hooks.
    tokens = Token.objects.all()
    for scope in scopes:
        tokens = tokens.filter(scopes__name=scope)
    return tokens


def _is_character_added(scopes):
    def is_character_added(character):
        return _tokens_with(scopes).filter(character_id=character.character_id).exists()

    return is_character_added


def _added_annotation(scopes):
    return Exists(_tokens_with(scopes).filter(character_id=OuterRef("character_id")))


def _loss_discovery_on() -> bool:
    """Whether SRP finds losses automatically; the import item is hidden while it does not (the check runs each
    time CharLink asks, so switching the feature on or off shows or hides the item without a restart)."""
    from fleetops.models import SrpSettings

    srp = SrpSettings.get_solo()
    return bool(srp.enabled and srp.loss_discovery_enabled)


app_import = AppImport(
    "fleetops",
    [
        LoginImport(
            app_label="fleetops",
            unique_id="fcfleet",
            field_label=_("FleetOps FC fleet control"),
            add_character=_add_character,
            scopes=list(FLEET_SCOPES),
            check_permissions=lambda user: user.has_perm("fleetops.start_fleet"),
            is_character_added=_is_character_added(FLEET_SCOPES),
            is_character_added_annotation=_added_annotation(FLEET_SCOPES),
            get_users_with_perms=lambda: users_with_any_perm(["fleetops.start_fleet"]),
            default_initial_selection=False,
        ),
        LoginImport(
            app_label="fleetops",
            unique_id="clickpap",
            field_label=_("FleetOps PAP links"),
            add_character=_add_character,
            scopes=list(CLICK_SCOPES),
            check_permissions=lambda user: user.has_perm("fleetops.basic_access"),
            is_character_added=_is_character_added(CLICK_SCOPES),
            is_character_added_annotation=_added_annotation(CLICK_SCOPES),
            get_users_with_perms=lambda: users_with_any_perm(["fleetops.basic_access"]),
            default_initial_selection=True,
        ),
        LoginImport(
            app_label="fleetops",
            unique_id="srplosses",
            field_label=_("FleetOps SRP loss reading"),
            add_character=_add_loss_character,
            scopes=list(LOSS_SCOPES),
            check_permissions=lambda user: user.has_perm("fleetops.request_srp") and _loss_discovery_on(),
            is_character_added=_is_character_added(LOSS_SCOPES),
            is_character_added_annotation=_added_annotation(LOSS_SCOPES),
            get_users_with_perms=lambda: users_with_any_perm(["fleetops.request_srp"]),
            default_initial_selection=True,
        ),
    ],
)
