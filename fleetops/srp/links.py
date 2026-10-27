# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The SRP link of a fleet: creation, opening and closing, the links a member can use and the fleet page card."""

import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from fleetops.models import (
    AttendanceRecord,
    FleetMemberState,
    FleetOperation,
    FleetOpsSettings,
    PapRegistration,
    SrpClaim,
    SrpFleetLink,
    SrpSettings,
)
from fleetops.services import group_access
from fleetops.services import links as site_links
from fleetops.services.audit import audit
from fleetops.srp import access, summaries

# Attempts to find a free random code; a collision of 96 random bits will not happen in practice.
CODE_ATTEMPTS = 5
CODE_BYTES = 12
# Claims listed on the fleet page card.
CARD_CLAIMS = 25
# A fleet page shows the card for fleets in these states: the link exists from the start of the fleet.
CARD_STATUSES = (
    FleetOperation.Status.STARTING,
    FleetOperation.Status.ACTIVE,
    FleetOperation.Status.ENDING,
    FleetOperation.Status.CLOSED,
)


def _fc_main(operation):
    """``(id, name)`` of the FC's main character when the link is created: the fleet's own snapshot, else
    the FC user's current main."""
    if operation.fc_main_character_id:
        return operation.fc_main_character_id, operation.fc_main_character_name or ""
    main = getattr(getattr(operation.fc_user, "profile", None), "main_character", None)
    if main is not None:
        return main.character_id, main.character_name or ""
    return None, ""


def ensure_fleet_link(operation: FleetOperation, *, actor=None) -> tuple[SrpFleetLink, bool]:
    """The fleet's link and whether it was created now; one link per fleet, so a second call returns the
    first. Retries the random code on a collision."""
    existing = SrpFleetLink.objects.filter(operation=operation).first()
    if existing is not None:
        return existing, False
    main_id, main_name = _fc_main(operation)
    for _attempt in range(CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                link = SrpFleetLink.objects.create(
                    operation=operation,
                    code=secrets.token_urlsafe(CODE_BYTES),
                    fc_user=operation.fc_user,
                    fc_main_character_id=main_id,
                    fc_main_character_name=main_name,
                    created_by=actor,
                )
        except IntegrityError:
            # Either another request created the fleet's link first, or the code is taken. A locking read sees
            # the rows other requests have committed, also inside a transaction that started before them.
            with transaction.atomic():
                existing = SrpFleetLink.objects.select_for_update().filter(operation=operation).first()
            if existing is not None:
                return existing, False
            continue
        audit(
            actor,
            "srp.link_create",
            link,
            None,
            {"operation_id": operation.pk, "fc_user_id": link.fc_user_id, "is_open": link.is_open},
        )
        return link, True
    raise RuntimeError("No free SRP link code was found.")


def link_path(link: SrpFleetLink) -> str:
    """The site-relative URL members open to claim a loss of the fleet."""
    return reverse("fleetops:srp_link", args=[link.code])


def link_url(link: SrpFleetLink) -> str:
    """The link with the site's address in front, to paste into a message; a path without ``SITE_URL``."""
    return site_links.absolute_url(link_path(link))


def set_link_open(link: SrpFleetLink, *, actor, is_open: bool) -> SrpFleetLink:
    """Open or close the link. Closing stops new claims through it; claims already made stay as they are.
    Setting the state the link already has changes and audits nothing."""
    with transaction.atomic():
        current = SrpFleetLink.objects.select_for_update().get(pk=link.pk)
        if current.is_open != bool(is_open):
            old = {"is_open": current.is_open, "closed_at": current.closed_at, "closed_by_id": current.closed_by_id}
            current.is_open = bool(is_open)
            current.closed_at = None if is_open else timezone.now()
            current.closed_by = None if is_open else actor
            current.save(update_fields=["is_open", "closed_at", "closed_by"])
            audit(
                actor,
                "srp.link_open" if is_open else "srp.link_close",
                current,
                {**old, "closed_at": old["closed_at"].isoformat() if old["closed_at"] else None},
                {"is_open": current.is_open, "closed_at": current.closed_at.isoformat() if current.closed_at else None},
            )
    link.is_open, link.closed_at, link.closed_by = current.is_open, current.closed_at, current.closed_by
    return link


def recent_links_for_user(user, *, days: int = 30, limit: int = 20) -> list[SrpFleetLink]:
    """Open links of fleets started in the last ``days`` days in which the user was a member, was granted
    PAP or registered for PAP, or that the user led; the newest fleets first."""
    since = timezone.now() - timedelta(days=days)
    mine = (
        Q(operation__fc_user=user)
        | Q(operation_id__in=FleetMemberState.objects.filter(auth_user=user).values("operation_id"))
        | Q(operation_id__in=AttendanceRecord.objects.filter(auth_user=user).values("operation_id"))
        | Q(operation_id__in=PapRegistration.objects.filter(auth_user=user).values("operation_id"))
    )
    return list(
        SrpFleetLink.objects.filter(mine, is_open=True, operation__started_at__gte=since)
        .select_related("operation", "operation__fleet_type")
        .order_by("-operation__started_at", "-pk")[:limit]
    )


def link_made_at_start() -> bool:
    """Whether a fleet started now gets a FleetOps SRP link: SRP is enabled, the link is made automatically
    and the provider setting picks FleetOps SRP."""
    if not SrpSettings.get_solo().enabled:
        return False
    fleetops_settings = FleetOpsSettings.get_solo()
    return bool(fleetops_settings.srp_auto_create) and fleetops_settings.srp_provider in ("auto", "fleetops")


def _uses_fleetops_srp(operation, srp_settings) -> bool:
    """Whether a link created now would be a FleetOps link: SRP is enabled and the provider setting picks it."""
    if not srp_settings.enabled:
        return False
    return FleetOpsSettings.get_solo().srp_provider in ("auto", "fleetops")


def operation_srp_context(operation: FleetOperation, user) -> dict:
    """Template context of the SRP card on the fleet page.

    ``srp_card`` is missing when the card has nothing for this user, which costs no query for a user without
    SRP permissions who does not lead the fleet. Otherwise it holds the link and its state, what the user
    may do with it (``can_request``, ``can_toggle``, ``can_create``) and, for SRP managers, ``view_all_stats``
    and the fleet's FC, the claims of the fleet with their totals.
    """
    if operation.status not in CARD_STATUSES or not getattr(user, "is_authenticated", False):
        return {}
    wants_request = access.can_request(user)
    sees_everything = access.sees_everything(user)
    leads = operation.fc_user_id == user.pk
    from fleetops.services.fleet_access import can_manage_fleet

    manages = can_manage_fleet(user, operation)
    if not (wants_request or sees_everything or leads or manages):
        return {}

    srp_settings = SrpSettings.get_solo()
    link = SrpFleetLink.objects.filter(operation=operation).first()
    manager = access.is_manager(user)
    # The fleet may carry a link of another SRP app; FleetOps then offers no second one.
    has_other_link = link is None and bool(operation.srp_reference or operation.srp_url)
    # PAP-only fleets never get an SRP link (the retry refuses them too).
    creatable = operation.send_ping or operation.is_manual
    can_create = bool(
        manages
        and creatable
        and link is None
        and not has_other_link
        and _uses_fleetops_srp(operation, srp_settings)
        # Group fleets get a link only when their group allows it (one query, group fleets only).
        and group_access.group_srp_allowed(operation)
    )
    if link is None and not can_create:
        return {}
    card = {
        "enabled": srp_settings.enabled,
        "link": link,
        "link_path": link_path(link) if link else "",
        "link_url": link_url(link) if link else "",
        "is_open": bool(link and link.is_open),
        "can_request": bool(link and link.is_open and wants_request and srp_settings.enabled),
        "can_toggle": bool(link and manager),
        "can_copy": bool(link and (manages or leads or manager)),
        "can_create": can_create,
        "show_claims": False,
        "claims": [],
        "totals": None,
        "claims_hidden": 0,
    }
    if link and (sees_everything or link.fc_user_id == user.pk):
        claims = SrpClaim.objects.filter(link=link)
        totals = summaries.claims_totals(claims)
        listed = list(claims.exclude(status=SrpClaim.Status.WITHDRAWN).order_by("-submitted_at")[:CARD_CLAIMS])
        card.update(
            show_claims=True, totals=totals, claims=listed, claims_hidden=max(totals.claims - len(listed), 0)
        )
    return {"srp_card": card}
