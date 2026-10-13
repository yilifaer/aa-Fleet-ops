"""What the Fleet tab shows: the fleet tree, the composition, and the state the page's poller applies."""

import hashlib
import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import timezone as dt_timezone

from django.db.models import Count, Max, Q
from django.template.loader import render_to_string
from django.utils import timezone

from fleetops import __version__
from fleetops.models import AuditLog, FleetOperation, FleetOpsSettings, FleetStructureTemplate, ShipFunction
from fleetops.services import sde
from fleetops.services.composition import build_composition, classify, function_key, load_classifier
from fleetops.services.fleet_access import LIVE_STATUSES, can_manage_fleet, can_view_live_fleet, write_state
from fleetops.services.fleet_jobs import current_job
from fleetops.services.fleet_overlay import current_structure, pending_invitations
from fleetops.services.fleet_refresh import read_problem
from fleetops.services.fleet_rules import (
    FLEET_MAX_MEMBERS,
    MAX_SQUADS_PER_WING,
    MAX_WINGS,
    NO_UNIT,
    squad_index,
)
from fleetops.services.periods import format_eve

# How often the Fleet tab asks for a new state while it is shown.
POLL_SECONDS = 10

# Parts of the Fleet tab the poller replaces, by state payload key (see operation/_fleet.html).
FRAGMENTS = {
    "status_html": "fleetops/operation/_fleet_status.html",
    "tree_html": "fleetops/operation/_fleet_tree.html",
    "composition_html": "fleetops/operation/_fleet_composition.html",
    "changes_html": "fleetops/operation/_fleet_changes.html",
}
# A closed fleet's tab shows only its composition.
CLOSED_FRAGMENTS = ("composition_html",)
# Parts of the page outside the Fleet tab the poller replaces when the payload carries them (None
# keeps the page's copy): "members_html" fills #members-fragment, "pap_html" #pap-preview-fragment
# and "controls_html" the Fleet Control card (#fleet-control-fragment).
PAGE_FRAGMENTS = ("members_html", "pap_html", "controls_html")

# Audit entries of the changes made to the EVE fleet from the fleet page (structure, moves, invites,
# templates, kicks); starting and ending the fleet are not listed.
CHANGE_ACTION_PREFIXES = (
    "fleet.wing_",
    "fleet.squad_",
    "fleet.member_",
    "fleet.move_",
    "fleet.invite",
    "fleet.template_",
    "fleet.kick_",
)
CHANGES_SHOWN = 10

SQUAD_ROLES = ("squad_commander", "squad_member")


@dataclass
class MemberView:
    character_id: int
    name: str
    in_auth: bool = False
    main_name: str = ""
    corporation: str = ""
    alliance: str = ""
    ship_type_id: int | None = None
    ship_name: str = ""
    system_id: int | None = None
    system_name: str = ""
    docked: bool = False
    takes_fleet_warp: bool | None = None
    role: str = "squad_member"
    wing_id: int | None = None
    squad_id: int | None = None
    is_boss: bool = False
    # The FC character and the fleet boss are never kicked.
    protected: bool = False
    function_id: int | None = None
    function_name: str = ""
    function_color: str = ""
    seconds_in_fleet: int = 0
    pending: bool = False


@dataclass
class SquadView:
    id: int
    name: str
    pending: bool = False
    commander: MemberView | None = None
    members: list[MemberView] = field(default_factory=list)
    count: int = 0
    is_empty: bool = True


@dataclass
class WingView:
    id: int
    name: str
    pending: bool = False
    commander: MemberView | None = None
    squads: list[SquadView] = field(default_factory=list)
    count: int = 0
    can_add_squad: bool = True
    is_empty: bool = True


@dataclass
class FleetView:
    commander: MemberView | None = None
    wings: list[WingView] = field(default_factory=list)
    # Members whose wing or squad is not in the last wings read yet.
    unplaced: list[MemberView] = field(default_factory=list)
    member_count: int = 0
    max_members: int = FLEET_MAX_MEMBERS
    boss_id: int | None = None
    boss_name: str = ""
    can_add_wing: bool = True
    structure_known: bool = False
    invitations: list[dict] = field(default_factory=list)

    def members(self) -> list[MemberView]:
        """Everyone in the fleet, in tree order."""
        found = [self.commander] if self.commander else []
        for wing in self.wings:
            found += [wing.commander] if wing.commander else []
            for squad in wing.squads:
                found += ([squad.commander] if squad.commander else []) + squad.members
        return found + self.unplaced


def _unit(value):
    """A wing or squad id of a member; None for ESI's -1 and unknown values."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return None if number in (NO_UNIT, 0) else number


def _attr(item, name):
    """A field of a ship function given as a model instance or a dict."""
    value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
    return "" if value is None else value


def _name_key(member):
    return (member.name.casefold(), member.character_id)


def _member_view(data, *, boss_id, protected_ids, infos, classifier):
    state = data.get("state")
    character_id = int(data["character_id"])
    type_id = data.get("ship_type_id")
    info = infos.get(type_id)
    member = MemberView(
        character_id=character_id,
        name=data.get("name") or f"Character {character_id}",
        in_auth=bool(getattr(state, "auth_user_id", None)),
        main_name=getattr(state, "main_character_name", "") or "",
        corporation=getattr(state, "corporation_name", "") or "",
        alliance=getattr(state, "alliance_name", "") or "",
        ship_type_id=type_id,
        ship_name=data.get("ship_type_name") or (info.name if info else ""),
        system_id=data.get("solar_system_id"),
        system_name=data.get("solar_system_name") or "",
        docked=bool(data.get("station_id")),
        takes_fleet_warp=data.get("takes_fleet_warp"),
        role=data.get("role") or "",
        wing_id=_unit(data.get("wing_id")),
        squad_id=_unit(data.get("squad_id")),
        is_boss=character_id == boss_id,
        protected=character_id in protected_ids,
        seconds_in_fleet=int(getattr(state, "accumulated_seconds", 0) or 0),
        pending=bool(data.get("pending")),
    )
    function = classify(member, info, classifier)
    if function is not None:
        member.function_id = function_key(function)
        member.function_name = str(_attr(function, "name"))
        member.function_color = str(_attr(function, "color"))
    return member


def _invitation(invitation, wings):
    """A pending invitation with the squad it goes to, for the tree's header."""
    found = squad_index(wings).get(_unit(invitation.get("squad_id")))
    where = f"{found[0]['name']} / {found[1]['name']}" if found else ""
    return {**invitation, "where": where}


def build_fleet_view(operation, *, classifier=None) -> FleetView:
    """The fleet tree as FleetOps expects it now: the stored snapshot plus changes ESI does not show yet.

    Wings and squads keep ESI's order; in a squad the commander comes first, then the members by
    name. Members whose wing or squad is not in the snapshot (the wings read is older than the
    members read) are listed as not yet placed.
    """
    classifier = classifier if classifier is not None else load_classifier()
    wings, members = current_structure(operation)
    boss_id = operation.fleet_boss_character_id or operation.fc_character_id
    protected_ids = {operation.fc_character_id, operation.fleet_boss_character_id} - {None}
    infos = sde.ship_type_details({data.get("ship_type_id") for data in members.values()} - {None})

    wing_views, squads_by_id = [], {}
    for wing in wings:
        wing_view = WingView(
            id=wing["id"], name=wing["name"] or f"Wing {wing['id']}", pending=bool(wing.get("pending"))
        )
        for squad in wing["squads"]:
            squad_view = SquadView(
                id=squad["id"], name=squad["name"] or f"Squad {squad['id']}", pending=bool(squad.get("pending"))
            )
            wing_view.squads.append(squad_view)
            squads_by_id.setdefault(squad_view.id, (wing_view, squad_view))
        wing_views.append(wing_view)
    wings_by_id = {wing.id: wing for wing in wing_views}

    views = sorted(
        (
            _member_view(data, boss_id=boss_id, protected_ids=protected_ids, infos=infos, classifier=classifier)
            for data in members.values()
        ),
        key=_name_key,
    )
    commander, unplaced = None, []
    for member in views:
        if member.role == "fleet_commander" and commander is None:
            commander = member
            continue
        if member.role == "wing_commander":
            wing = wings_by_id.get(member.wing_id)
            if wing is not None and wing.commander is None:
                wing.commander = member
                continue
        elif member.role in SQUAD_ROLES:
            found = squads_by_id.get(member.squad_id)
            if found is not None and member.wing_id in (None, found[0].id):
                squad = found[1]
                if member.role == "squad_member":
                    squad.members.append(member)
                    continue
                if squad.commander is None:
                    squad.commander = member
                    continue
        unplaced.append(member)

    for wing in wing_views:
        for squad in wing.squads:
            squad.count = len(squad.members) + (1 if squad.commander else 0)
            squad.is_empty = squad.count == 0
        wing.count = (1 if wing.commander else 0) + sum(squad.count for squad in wing.squads)
        wing.is_empty = wing.count == 0
        wing.can_add_squad = len(wing.squads) < MAX_SQUADS_PER_WING

    boss = next((member for member in views if member.character_id == boss_id), None)
    if boss is not None:
        boss_name = boss.name
    elif boss_id == operation.fc_character_id:
        boss_name = operation.fc_character_name
    else:
        boss_name = f"Character {boss_id}"
    return FleetView(
        commander=commander,
        wings=wing_views,
        unplaced=unplaced,
        member_count=len(views),
        boss_id=boss_id,
        boss_name=boss_name,
        can_add_wing=len(wing_views) < MAX_WINGS,
        structure_known=bool(operation.fleet_wings or wing_views),
        invitations=[_invitation(invitation, wings) for invitation in pending_invitations(operation.pk, members)],
    )


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return value


def _as_shown(value):
    """``value`` with each time in fleet in whole minutes, as the page shows it."""
    if isinstance(value, dict):
        return {
            key: item // 60 if key == "seconds_in_fleet" and isinstance(item, int) else _as_shown(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_as_shown(item) for item in value]
    return value


def view_version(operation, fleet_view, composition, write_state, job, problem, *, extra=None) -> str:
    """A short hash of what the Fleet tab shows; the poller gets new HTML only when it changes.

    ``extra`` covers everything else the state carries, such as the recent changes and the stamp
    of the page's member list and PAP preview. Times count as the page shows them, so a read that
    only adds seconds to the time in fleet keeps the version and the page keeps its HTML.
    """
    data = {
        "status": operation.status,
        "view": _as_shown(_plain(fleet_view)),
        "composition": _plain(composition),
        "write": _plain(write_state),
        "job": job,
        "problem": problem,
        "extra": extra,
    }
    text = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _closed_members(operation):
    # A closed fleet counts the last known ship of everyone tracked in it.
    return operation.member_states.all()


def _fc_system_id(operation, fleet_view):
    if fleet_view is None:
        return None
    return next(
        (member.system_id for member in fleet_view.members() if member.character_id == operation.fc_character_id),
        None,
    )


def _is_stale(operation, settings):
    if operation.status != FleetOperation.Status.ACTIVE or not operation.last_esi_update:
        return False
    return (timezone.now() - operation.last_esi_update).total_seconds() > settings.stale_threshold


def recent_changes(operation) -> list:
    """The latest changes made to the EVE fleet from the fleet page, newest first."""
    actions = Q()
    for prefix in CHANGE_ACTION_PREFIXES:
        actions |= Q(action__startswith=prefix)
    return list(
        AuditLog.objects.filter(actions, object_type=FleetOperation.__name__, object_id=str(operation.pk))
        .select_related("actor__profile__main_character")
        .order_by("-created_at", "-pk")[:CHANGES_SHOWN]
    )


def page_stamp(operation) -> list:
    """What, besides the fleet itself, the page's member list and PAP preview show; changes with them.

    Members' "Last seen" counts to the minute, as the member list shows it.
    """
    members = operation.member_states.aggregate(count=Count("pk"), seen=Max("last_seen", filter=Q(is_active=True)))
    attendance = operation.attendance_records.aggregate(count=Count("pk"), changed=Max("updated_at"))
    registrations = operation.pap_registrations.aggregate(count=Count("pk"), changed=Max("registered_at"))
    seen = _iso(members["seen"])
    return [
        operation.pap_minimum_minutes,
        members["count"],
        seen[:16] if seen else None,
        attendance["count"],
        _iso(attendance["changed"]),
        registrations["count"],
        _iso(registrations["changed"]),
    ]


def fleet_panel(operation, user) -> dict:
    """The Fleet tab's template context for ``user``, who may see the tab (fleet_access.can_view_fleet_tab)."""
    settings = FleetOpsSettings.get_solo()
    is_closed = operation.status == FleetOperation.Status.CLOSED
    can_manage = can_manage_fleet(user, operation)
    state = write_state(operation, user)
    classifier = load_classifier()
    fleet_view = None if is_closed else build_fleet_view(operation, classifier=classifier)
    composition = build_composition(
        _closed_members(operation) if is_closed else fleet_view.members(),
        fc_system_id=_fc_system_id(operation, fleet_view),
        settings=settings,
        classifier=classifier,
    )
    job = current_job(operation.pk) if can_manage else None
    problem = read_problem(operation.pk) if can_manage else {}
    changes = recent_changes(operation) if can_manage and not is_closed else []
    # The member list and the PAP preview of an open fleet follow the poller too (see state_payload).
    page_live = operation.status in LIVE_STATUSES and can_view_live_fleet(user, operation)
    stamp = page_stamp(operation) if page_live else None
    can_classify = user.has_perm("fleetops.manage_configuration")
    live_manager = can_manage and operation.status == FleetOperation.Status.ACTIVE
    extra = {"changes": [entry.pk for entry in changes], "page": stamp}
    return {
        "operation": operation,
        "fleet_view": fleet_view,
        "composition": composition,
        "write_state": state,
        "fleet_can_manage": can_manage,
        "fleet_can_write": state.allowed,
        # Only an active fleet is polled; a closed one shows its composition only.
        "fleet_live": operation.status == FleetOperation.Status.ACTIVE,
        "fleet_stale": _is_stale(operation, settings),
        "fleet_job": job,
        "fleet_problem": problem,
        "fleet_changes": changes,
        "fleet_version": view_version(operation, fleet_view, composition, state, job, problem, extra=extra),
        "last_esi_update_iso": _iso(operation.last_esi_update),
        # The toolbar and its dialogs are not redrawn by the poller, so they list the templates
        # even while a change is not possible; their buttons follow the write state.
        "structure_templates": list(FleetStructureTemplate.objects.filter(is_active=True)) if live_manager else [],
        "can_classify": can_classify,
        "ship_functions": list(ShipFunction.objects.filter(is_active=True)) if can_classify else [],
        "poll_seconds": POLL_SECONDS,
        "live_refresh_seconds": settings.fleet_live_refresh_seconds if can_manage else 0,
        "fleetops_version": __version__,
    }


def _iso(value):
    """A UTC time as the page's scripts read it, e.g. 2026-10-13T18:04:05Z."""
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stat_text(value) -> str:
    """A time as the fleet page's header cards show it: EVE time, as the page renders it."""
    if value is None:
        return "—"
    return f"{format_eve(value, '%Y-%m-%d %H:%M:%S')} EVE"


def state_payload(request, operation, *, known_version=None) -> dict:
    """The Fleet tab's state for the page's poller and for the JSON answer of every fleet action.

    When ``known_version`` is the current version only the times and the job are sent
    (``"unchanged": true``); otherwise the HTML fragments too. A fragment that is None leaves
    that part of the page as it is.
    """
    panel = fleet_panel(operation, request.user)
    head = {
        "version": panel["fleet_version"],
        "server_time": _iso(timezone.now()),
        "status": operation.status,
        "last_esi_update": _iso(operation.last_esi_update),
        "structure_read_at": _iso(operation.fleet_wings_read_at),
        "stale": panel["fleet_stale"],
        "job": panel["fleet_job"],
    }
    if known_version and known_version == panel["fleet_version"]:
        return {"unchanged": True, **head}
    fleet_view = panel["fleet_view"]
    state = panel["write_state"]
    # Imported here: the views module imports this one.
    from fleetops.views_fleet_state import page_fragments

    return {
        **head,
        "unchanged": False,
        "member_count": fleet_view.member_count if fleet_view else panel["composition"].total,
        "max_members": FLEET_MAX_MEMBERS,
        "stat_last_esi": stat_text(operation.last_esi_update),
        "write": {"allowed": state.allowed, "reason": state.reason, "show_authorize": state.show_authorize},
        **{
            key: render_to_string(template, panel, request=request)
            if fleet_view is not None or key in CLOSED_FRAGMENTS
            else None
            for key, template in FRAGMENTS.items()
        },
        **page_fragments(request, operation),
    }
