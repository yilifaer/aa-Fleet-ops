"""aa-charlink integration: pilots add the FleetOps ESI scopes when they link characters with CharLink.

CharLink imports this module through the ``charlink`` hook in ``auth_hooks.py``, so it is only loaded
where CharLink is installed. CharLink checks that ``"fleetops"`` is listed literally in INSTALLED_APPS.
Nothing is stored when a character is added: FleetOps reads the tokens when an FC starts a fleet or a
pilot registers through a PAP link.
"""

from charlink.app_imports.utils import AppImport, LoginImport
from django.contrib.auth.models import Permission, User
from django.db.models import Exists, OuterRef, Q
from esi.models import Token

from fleetops.constants import CLICK_SCOPES, FLEET_SCOPES


def _add_character(request, token):
    """CharLink's SSO already saved the token; FleetOps needs nothing else."""


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


def _users_with_any_perm(perms):
    """Users holding any of ``perms`` (``app_label.codename``) directly, through a group or their
    state, plus superusers."""
    query = Q()
    for perm in perms:
        app_label, codename = perm.split(".", 1)
        query |= Q(content_type__app_label=app_label, codename=codename)
    permissions = Permission.objects.filter(query)
    return User.objects.filter(
        Q(user_permissions__in=permissions)
        | Q(groups__permissions__in=permissions)
        | Q(profile__state__permissions__in=permissions)
        | Q(is_superuser=True)
    ).distinct()


app_import = AppImport(
    "fleetops",
    [
        LoginImport(
            app_label="fleetops",
            unique_id="fcfleet",
            field_label="FleetOps FC fleet control",
            add_character=_add_character,
            scopes=list(FLEET_SCOPES),
            check_permissions=lambda user: user.has_perm("fleetops.start_fleet"),
            is_character_added=_is_character_added(FLEET_SCOPES),
            is_character_added_annotation=_added_annotation(FLEET_SCOPES),
            get_users_with_perms=lambda: _users_with_any_perm(["fleetops.start_fleet"]),
            default_initial_selection=False,
        ),
        LoginImport(
            app_label="fleetops",
            unique_id="clickpap",
            field_label="FleetOps PAP links",
            add_character=_add_character,
            scopes=list(CLICK_SCOPES),
            check_permissions=lambda user: user.has_perm("fleetops.basic_access"),
            is_character_added=_is_character_added(CLICK_SCOPES),
            is_character_added_annotation=_added_annotation(CLICK_SCOPES),
            get_users_with_perms=lambda: _users_with_any_perm(["fleetops.basic_access"]),
            default_initial_selection=True,
        ),
    ],
)
