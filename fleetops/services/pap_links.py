"""PAP links: pilots open a link and register their characters for PAP in a fleet.

Registering needs an ESI token with the PAP link scopes, the same ones as AFAT's click tracking, so
tokens AFAT or CharLink already hold are used. A registered character gets PAP when the fleet's PAP is
finalized, whatever its time in fleet; the per-player character limit still applies then.
"""

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger
from django.db import IntegrityError, transaction
from django.utils import timezone
from esi.models import Token

from fleetops.constants import CLICK_SCOPES
from fleetops.models import FleetOperation, FleetOpsSettings, PapLink, PapRegistration
from fleetops.providers.doctrines import doctrine_fits_snapshot
from fleetops.providers.esi import FleetESIError, character_location, character_online, character_ship
from fleetops.services.audit import audit
from fleetops.services.errors import log_failure
from fleetops.services.operations import (
    create_srp_at_start,
    fc_values,
    owned_fleet_character,
    pap_rule_values,
    set_action,
)
from fleetops.services.pap import refresh_after_change
from fleetops.services.review import initial_review_fields
from fleetops.services.sde import item_type_names, solar_system_names

logger = get_extension_logger(__name__)

Status = FleetOperation.Status

# A link accepts registrations while its fleet has these states.
LINK_STATUSES = (Status.ACTIVE, Status.ENDING, Status.CLOSED)
# New links: running fleets, and closed ones for pilots who could not register in time.
CREATE_STATUSES = (Status.ACTIVE, Status.CLOSED)
# Characters registered by one form submission at most.
MAX_CHARACTERS_PER_REGISTRATION = 20
MIN_LINK_MINUTES = 1
MAX_LINK_MINUTES = 1440

NOT_OWNED = "Not one of your characters."
NO_TOKEN = "No valid ESI token with the PAP link scopes; use Add a character to log in with it."
TOKEN_CHECK_FAILED = "The ESI token could not be checked; try again."
ALREADY_REGISTERED = "Already registered for this fleet."
LINK_CLOSED = "The PAP link is no longer open."
LEGACY_FLEET = (
    "PAP links are not available for fleets closed before PAP followed time in fleet; add manual PAP instead."
)
NOT_ONLINE = "Not logged in to the game; this PAP link needs the character online."
TOO_MANY = (
    f"At most {MAX_CHARACTERS_PER_REGISTRATION} characters are registered at once; register this one again."
)

# Steps of an ESI fleet start that a PAP-link fleet has no use for.
LINK_ONLY_SKIPPED = (
    ("discord_ping", "Discord ping"),
    ("motd_update", "MOTD update"),
    ("tracking_start", "ESI tracking"),
)


class PapLinkError(ValueError):
    """A PAP link cannot be created for this fleet."""


@dataclass
class RegistrationResult:
    # (character_id, name)
    registered: list = field(default_factory=list)
    # (character_id, name, reason)
    rejected: list = field(default_factory=list)


def takes_registrations(operation):
    """Whether registrations can still give PAP in the fleet.

    Fleets closed before the upgrade keep the PAP selection they were closed with, so a registration
    would never be granted there.
    """
    return operation.status in LINK_STATUSES and operation.pap_rules != FleetOperation.PapRules.LEGACY


def accepts_new_links(operation):
    """Whether the FC can create a PAP link for the fleet."""
    return operation.status in CREATE_STATUSES and operation.pap_rules != FleetOperation.PapRules.LEGACY


def link_is_valid(link, now=None):
    """Not revoked, not expired, and its fleet still takes registrations."""
    now = now or timezone.now()
    return link.revoked_at is None and now < link.expires_at and takes_registrations(link.operation)


def link_state(link, now=None):
    """``open``, ``revoked``, ``expired`` or ``closed`` (the fleet no longer takes registrations)."""
    now = now or timezone.now()
    if link.revoked_at is not None:
        return "revoked"
    if now >= link.expires_at:
        return "expired"
    if not takes_registrations(link.operation):
        return "closed"
    return "open"


def create_link(operation, *, actor, minutes=None, require_online=True):
    """A new PAP link for the fleet, open for ``minutes`` (the configured default when None)."""
    if operation.status not in CREATE_STATUSES:
        status = operation.get_status_display().lower()
        raise PapLinkError(f"PAP links are created for active or closed fleets; this fleet is {status}.")
    if operation.pap_rules == FleetOperation.PapRules.LEGACY:
        raise PapLinkError(LEGACY_FLEET)
    if minutes is None:
        minutes = FleetOpsSettings.get_solo().pap_link_minutes
    minutes = int(minutes)
    if not MIN_LINK_MINUTES <= minutes <= MAX_LINK_MINUTES:
        raise PapLinkError(f"A PAP link stays open for {MIN_LINK_MINUTES} to {MAX_LINK_MINUTES} minutes.")
    now = timezone.now()
    link = PapLink.objects.create(
        operation=operation,
        code=secrets.token_urlsafe(24),
        created_by=actor,
        created_at=now,
        expires_at=now + timedelta(minutes=minutes),
        require_online=bool(require_online),
    )
    audit(
        actor,
        "pap_link.create",
        operation,
        None,
        {
            "link_id": link.pk,
            "minutes": minutes,
            "require_online": link.require_online,
            "expires_at": link.expires_at.isoformat(),
        },
    )
    return link


def revoke_link(link, *, actor):
    """Close a PAP link before it expires; revoking it again changes nothing."""
    with transaction.atomic():
        link = PapLink.objects.select_for_update().select_related("operation").get(pk=link.pk)
        if link.revoked_at is not None:
            return link
        link.revoked_at = timezone.now()
        link.save(update_fields=["revoked_at"])
        audit(
            actor,
            "pap_link.revoke",
            link.operation,
            {"link_id": link.pk, "expires_at": link.expires_at.isoformat()},
            {"link_id": link.pk, "revoked_at": link.revoked_at.isoformat()},
        )
    return link


def _click_token(user, character_id):
    """The user's own token of the character with the PAP link scopes, refreshed when it expired."""
    return (
        Token.objects.filter(user=user, character_id=character_id)
        .require_scopes(CLICK_SCOPES)
        .require_valid()
        .first()
    )


def _best_effort(read, token):
    # Where the pilot is and what they fly are shown on the fleet page only; a failed read leaves them blank.
    try:
        return read(token)
    except FleetESIError:
        return {}


def register_characters(link, user, character_ids):
    """Register the user's characters through a link; returns the registered and rejected ones.

    Each character must belong to the user and have the user's own token with the PAP link scopes
    (django-esi also hands out tokens without a user, so ownership is always checked here). A link that
    requires it also needs the character online. Its location and ship are recorded when ESI tells them.
    """
    link = PapLink.objects.select_related("operation").get(pk=link.pk)
    operation = link.operation
    result = RegistrationResult()
    ids = list(dict.fromkeys(int(character_id) for character_id in character_ids))
    if not ids:
        return result
    owned = dict(
        CharacterOwnership.objects.filter(user=user, character__character_id__in=ids).values_list(
            "character__character_id", "character__character_name"
        )
    )
    for character_id in ids[MAX_CHARACTERS_PER_REGISTRATION:]:
        result.rejected.append((character_id, owned.get(character_id) or f"Character {character_id}", TOO_MANY))

    pending = []
    for character_id in ids[:MAX_CHARACTERS_PER_REGISTRATION]:
        name = owned.get(character_id)
        if name is None:
            result.rejected.append((character_id, f"Character {character_id}", NOT_OWNED))
            continue
        if not link_is_valid(link):
            result.rejected.append((character_id, name, LINK_CLOSED))
            continue
        if PapRegistration.objects.filter(operation=operation, character_id=character_id).exists():
            result.rejected.append((character_id, name, ALREADY_REGISTERED))
            continue
        try:
            token = _click_token(user, character_id)
        except Exception as exc:
            # A token refresh talks to EVE SSO; its errors may quote the token, so only the class is logged.
            log_failure(logger, "PAP link token check", exc, operation)
            result.rejected.append((character_id, name, TOKEN_CHECK_FAILED))
            continue
        if token is None:
            result.rejected.append((character_id, name, NO_TOKEN))
            continue
        online = None
        if link.require_online:
            try:
                online = character_online(token)
            except FleetESIError as exc:
                result.rejected.append((character_id, name, str(exc)))
                continue
            if not online:
                result.rejected.append((character_id, name, NOT_ONLINE))
                continue
        location = _best_effort(character_location, token)
        ship = _best_effort(character_ship, token)
        # (character_id, name, online, solar_system_id, ship_type_id)
        pending.append((character_id, name, online, location.get("solar_system_id"), ship.get("ship_type_id")))

    ship_names = item_type_names({row[4] for row in pending if row[4]})
    system_names = solar_system_names({row[3] for row in pending if row[3]})
    for character_id, name, online, system_id, ship_id in pending:
        if not link_is_valid(link):
            result.rejected.append((character_id, name, LINK_CLOSED))
            continue
        try:
            with transaction.atomic():
                PapRegistration.objects.create(
                    operation=operation,
                    link=link,
                    character_id=character_id,
                    character_name=name,
                    auth_user=user,
                    ship_type_id=ship_id,
                    ship_type_name=ship_names.get(ship_id, "") if ship_id else "",
                    solar_system_id=system_id,
                    solar_system_name=system_names.get(system_id, "") if system_id else "",
                    online=online,
                    registered_at=timezone.now(),
                )
        except IntegrityError:
            # Registered meanwhile, e.g. by a second click.
            result.rejected.append((character_id, name, ALREADY_REGISTERED))
            continue
        result.registered.append((character_id, name))

    if result.registered:
        operation.refresh_from_db()
        # A closed fleet already has its PAP; it is recalculated so the new registrations count.
        if operation.status == Status.CLOSED:
            refresh_after_change(operation, actor=user, reason="PAP link registration")
    audit(
        user,
        "pap.register",
        operation,
        None,
        {
            "link_id": link.pk,
            "registered": [character_id for character_id, _name in result.registered],
            "rejected": [
                {"character_id": character_id, "reason": reason} for character_id, _name, reason in result.rejected
            ],
        },
    )
    return result


def start_link_only_fleet(user, cleaned_data):
    """Start a fleet without an ESI fleet, where pilots get PAP through PAP links only.

    The fleet is a manual record: it waits for FC lead approval unless an FC lead starts it. Its first
    PAP link is created with it. A resubmitted form returns the fleet its first submit started.
    """
    request_id = cleaned_data.get("request_id") or uuid.uuid4()
    existing = FleetOperation.objects.filter(start_request_id=request_id).first()
    if existing is not None:
        return existing
    character = owned_fleet_character(user, cleaned_data["fc_character_id"])
    settings = FleetOpsSettings.get_solo()
    fleet_type = cleaned_data["fleet_type"]
    source = cleaned_data.get("doctrine_source") or "none"
    external_id = cleaned_data.get("doctrine_external_id") or ""
    now = timezone.now()
    try:
        with transaction.atomic():
            operation = FleetOperation.objects.create(
                start_request_id=request_id,
                status=Status.ACTIVE,
                created_by=user,
                **fc_values(user, character),
                fleet_type=fleet_type,
                fleet_point_weight_snapshot=fleet_type.point_weight,
                doctrine_name=cleaned_data.get("doctrine_name", "") or "",
                doctrine_external_id=external_id,
                doctrine_source=source,
                doctrine_fits=doctrine_fits_snapshot(source, external_id),
                formup=cleaned_data["formup"],
                additional_message=cleaned_data.get("notes", "") or "",
                started_at=now,
                tracking_enabled=False,
                send_ping=False,
                is_manual=True,
                link_only=True,
                attendance_multiplier=1,
                # The PAP rules of the fleet are the ones in force when it starts.
                **pap_rule_values(settings),
                **initial_review_fields(user, now),
            )
            link = create_link(
                operation,
                actor=user,
                minutes=cleaned_data.get("link_minutes"),
                require_online=cleaned_data.get("require_online", True),
            )
            set_action(operation, "pap_link_fleet", True, "PAP-link fleet started without an ESI fleet.")
            for action, step in LINK_ONLY_SKIPPED:
                set_action(operation, action, None, f"PAP-link fleet: {step} not used.")
            # The one fleet.start entry of this start, like an ESI fleet's.
            audit(
                user,
                "fleet.start",
                operation,
                None,
                {
                    "status": operation.status,
                    "esi_fleet_id": None,
                    "fc_character_id": operation.fc_character_id,
                    "fleet_type": str(fleet_type),
                    "operation_mode": "pap_link",
                    "link_only": True,
                    "from_schedule": False,
                    "review_status": operation.review_status,
                    "link_id": link.pk,
                    "link_minutes": round((link.expires_at - link.created_at).total_seconds() / 60),
                    "require_online": link.require_online,
                },
            )
    except IntegrityError:
        # A second submit of the same form started the fleet first.
        existing = FleetOperation.objects.filter(start_request_id=request_id).first()
        if existing is None:
            raise
        return existing
    # The SRP link of the fleet, like an ESI fleet's. It is best effort and records its own step.
    create_srp_at_start(operation)
    return operation
