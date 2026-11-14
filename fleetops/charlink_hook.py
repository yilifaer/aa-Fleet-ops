# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""aa-charlink integration: pilots add all FleetOps ESI scopes with one CharLink item.

CharLink imports this module through the ``charlink`` hook in ``auth_hooks.py``, so it is only loaded
where CharLink is installed. CharLink checks that ``"fleetops"`` is listed literally in INSTALLED_APPS.
Nothing is stored when a character is added: FleetOps reads the tokens when an FC starts a fleet, a
pilot registers through a PAP link or the loss scan reads the recent losses of a character.
"""

from charlink.app_imports.utils import AppImport, LoginImport
from django.db.models import Exists, OuterRef
from django.utils.translation import pgettext_lazy
from esi.models import Token

from fleetops.constants import ALL_SCOPES
from fleetops.services.permission_users import users_with_any_perm


def _loss_discovery_on() -> bool:
    """Whether SRP finds losses automatically."""
    from fleetops.models import SrpSettings

    srp = SrpSettings.get_solo()
    return bool(srp.enabled and srp.loss_discovery_enabled)


def _add_character(request, token):
    """CharLink's SSO already saved the token. Only when SRP and loss reading are on is the character made due, so
    the next scan run reads its losses; otherwise nothing is done."""
    if _loss_discovery_on():
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


app_import = AppImport(
    "fleetops",
    [
        # One item only. The unique_id stays "clickpap" so the CharLink setting a site already stores for it
        # (fleetops_clickpap) keeps working; the rows of the former items "fcfleet" and "srplosses" stay unused.
        LoginImport(
            app_label="fleetops",
            unique_id="clickpap",
            # The FleetOps context keeps another plugin's translation of the same words from replacing this one.
            field_label=pgettext_lazy("FleetOps", "FleetOps (PAP links, SRP, fleet control)"),
            add_character=_add_character,
            scopes=list(ALL_SCOPES),
            check_permissions=lambda user: user.has_perm("fleetops.basic_access"),
            is_character_added=_is_character_added(ALL_SCOPES),
            is_character_added_annotation=_added_annotation(ALL_SCOPES),
            get_users_with_perms=lambda: users_with_any_perm(["fleetops.basic_access"]),
            default_initial_selection=True,
        ),
    ],
)
