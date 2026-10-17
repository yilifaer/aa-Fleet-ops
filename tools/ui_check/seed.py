#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Build the database of the interface check: example data, one login cookie per role and the list of pages.

Usage (from the root of the checkout, with the Python of the Alliance Auth environment):

    python tools/ui_check/seed.py --out tools/ui_check/.work/seed.json [--db PATH] [--logins N]
    python tools/ui_check/seed.py --list [--lane C] [--pages 'fleet-*']

The database file is deleted and rebuilt on every run. The cache is the Redis of ``tools.ui_check.settings``
(``FLEETOPS_UI_REDIS``); nothing in it is cleared. Every name and ID in the data is an example value.
A page can name what the tree must have for it to exist (``requires``); such a page is marked ``skip`` in the
output instead of failing. Every page of the current list is expected to answer.
"""

import argparse
import fnmatch
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tools.ui_check.settings")

# --------------------------------------------------------------------------------------------------------
# Page list
# --------------------------------------------------------------------------------------------------------

# Lane of each page id (design 4.1 rule 6). The first matching pattern wins; "S0" pages belong to no lane and
# run with ``--lane S0`` or ``--lane all``.
LANE_PATTERNS = (
    ("A", ("start", "start-*", "presets", "scheduled", "edit-scheduled")),
    ("F", ("send-ping-*", "config-groups", "config-group-edit")),
    ("B", ("fleet-*", "pap-link-register", "pap-fleet-start")),
    ("C", ("operations*", "manual-fleet", "roster-import", "edit-operation", "approvals", "incentives")),
    ("D", ("dashboard*", "my-*", "leaderboard", "corp-*", "all-corps", "alliance-members", "history-*",
           "fc-stats*", "member-detail")),
    ("E", ("srp-*", "config*", "audit")),
)
# Pages that match B's ``fleet-*`` but belong to lane C (the approval states of a fleet record).
LANE_OVERRIDES = {"fleet-pending": "C", "fleet-rejected": "C", "fleet-resubmitted": "C"}
LANES = ("A", "B", "C", "D", "E", "F", "S0")

THEME_KEYS = ("bootstrap", "bootstrap-dark", "flatly", "darkly", "materia")


def lane_of(page_id: str) -> str:
    if page_id in LANE_OVERRIDES:
        return LANE_OVERRIDES[page_id]
    for lane, patterns in LANE_PATTERNS:
        if any(fnmatch.fnmatchcase(page_id, pattern) for pattern in patterns):
            return lane
    return "S0"


@dataclass
class Page:
    """One page of the list: an id, the role that opens it, how to build its URL and what it must answer."""

    id: str
    role: str
    url: object  # callable(ctx) -> str
    status: int = 200
    # What the tree must have for the page to exist: "url:<name>", "model:<Name>", "section:<key>".
    requires: tuple = ()
    # Scripted states of the page (run.js, states.js); the page itself is always loaded first.
    states: tuple = ()
    note: str = ""
    lane: str = field(init=False)

    def __post_init__(self):
        self.lane = lane_of(self.id)


def build_pages():
    """The pages in the order they are run. Each URL is built from the seeded objects in ``ctx``."""
    from django.urls import reverse

    def u(name, *args, query=""):
        return lambda ctx: reverse(f"fleetops:{name}", args=[a(ctx) if callable(a) else a for a in args]) + query

    def op(attr):
        return lambda ctx: getattr(ctx, attr).uuid

    pages = [
        # member
        Page("dashboard", "member", u("dashboard")),
        Page("my-stats", "member", u("my_statistics")),
        Page("my-history", "member", u("attendance_history_me")),
        Page("fleet-active-member", "member", u("operation_detail", op("active"))),
        Page("fleet-closed", "member", u("operation_detail", op("closed"))),
        Page("srp-home", "member", u("srp_home")),
        Page("srp-link", "member", lambda ctx: reverse("fleetops:srp_link", args=[ctx.srp_link.code])),
        Page("srp-claim-paid", "member", lambda ctx: reverse("fleetops:srp_claim", args=[ctx.claim_paid.uuid])),
        Page("pap-link-register", "member", lambda ctx: reverse("fleetops:pap_link_register", args=[ctx.pap_link.code])),
        Page("leaderboard", "member", u("leaderboard")),
        # fc
        Page("dashboard-fc", "fc", u("dashboard")),
        Page("start", "fc", u("start_fleet")),
        Page("presets", "fc", u("preset_list")),
        Page("fleet-active-manager", "fc", u("operation_detail", op("active"))),
        Page("fleet-nowrite", "fc_nowrite", u("operation_detail", op("active_nowrite"))),
        Page("scheduled", "fc", u("operation_detail", op("scheduled"))),
        Page("edit-scheduled", "fc", u("edit_scheduled_fleet", op("scheduled"))),
        Page("operations", "fc", u("fleet_operations")),
        Page("manual-fleet", "fc", u("manual_fleet")),
        Page("pap-fleet-start", "fc", u("start_link_only_fleet")),
        Page("roster-import", "fc", u("roster_import", op("closed"))),
        Page("edit-operation", "fc", u("edit_operation", op("closed"))),
        Page("fleet-link-only", "fc", u("operation_detail", op("link_only"))),
        Page("operations-manual-attendance", "lead", u("manual_attendance")),
        # lead
        Page("approvals", "lead", u("approval_queue")),
        Page("fc-stats", "lead", u("all_fc_statistics")),
        Page("fc-stats-detail", "lead", lambda ctx: reverse("fleetops:fc_statistics_detail", args=[ctx.users["fc"].pk])),
        Page("incentives", "lead", u("incentive_review")),
        Page("fleet-pending", "lead", u("operation_detail", op("manual_pending"))),
        Page("fleet-rejected", "lead", u("operation_detail", op("manual_rejected"))),
        Page("fleet-resubmitted", "lead", u("operation_detail", op("manual_resubmitted"))),
        Page("fleet-active-lead", "lead", u("operation_detail", op("active"))),
        Page("operations-lead", "lead", u("fleet_operations")),
        # corp
        Page("corp-stats", "corp", u("corporation_statistics")),
        Page("corp-members", "corp", u("corporation_members")),
        Page("corp-inactive", "corp", u("corporation_members", query="?status=inactive")),
        Page("member-detail", "corp", lambda ctx: reverse("fleetops:member_detail", args=[ctx.users["pilot03"].pk])),
        Page("history-corp", "corp", u("attendance_history_corporation")),
        # stats
        Page("all-corps", "stats", u("all_corporation_statistics")),
        Page("alliance-members", "stats", u("alliance_members")),
        Page("history-alliance", "stats", u("attendance_history_alliance")),
        # srp
        Page("srp-review", "srp", u("srp_review")),
        Page("srp-payments", "srp", u("srp_payments")),
        Page("srp-summary", "srp", u("srp_summary")),
        Page("srp-claim-pending", "srp", lambda ctx: reverse("fleetops:srp_claim", args=[ctx.claim_pending.uuid])),
        Page("srp-summary-fc", "fc", u("srp_summary_own_fc")),
        # admin
        Page("config", "admin", u("configuration_index")),
        Page("config-fleet-types", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["fleet-types"])),
        Page("config-ship-functions", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["ship-functions"])),
        Page("config-srp-rules", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["srp-rules"])),
        Page("config-ping-channels", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["ping-channels"])),
        Page("config-targets", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["statistics-targets"])),
        Page("config-templates", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["templates"])),
        Page("config-settings", "admin", u("configuration_settings")),
        Page("config-srp-settings", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["srp"])),
        Page("audit", "admin", u("audit_log")),
        # Error states of the forms (FB02): the page is loaded, filled in and sent by script (states.js).
        Page("start-error", "fc", u("start_fleet"), states=("start-error",)),
        Page("config-error", "admin", lambda ctx: reverse("fleetops:configuration_add", args=["srp-rules"]), states=("config-error",)),
        # Group roles and pages (design 3.8, G21). Send Ping is a placeholder page for now.
        Page("config-groups", "admin", lambda ctx: reverse("fleetops:configuration_list", args=["groups"])),
        Page("config-group-edit", "admin",
             lambda ctx: reverse("fleetops:configuration_edit", args=["groups", ctx.group_access_fleets.pk])),
        Page("config-channel-edit", "admin",
             lambda ctx: reverse("fleetops:configuration_edit", args=["ping-channels", ctx.channel_groups.pk])),
        Page("start-fleetleader", "fleetleader", u("start_fleet")),
        Page("send-ping-fleetleader", "fleetleader", u("send_ping")),
        Page("fleet-group-manager", "fleetleader", u("operation_detail", op("group_fleet"))),
        Page("operations-fleetleader", "fleetleader", u("fleet_operations")),
        Page("dashboard-fleetleader", "fleetleader2", u("dashboard")),
        Page("fleet-group-readonly", "fleetleader2", u("operation_detail", op("group_fleet"))),
        Page("start-fcleader", "fcleader", u("start_fleet")),
        Page("send-ping-fcleader", "fcleader", u("send_ping")),
        Page("send-ping-pingleader", "pingleader", u("send_ping")),
        Page("send-ping-fc", "fc", u("send_ping")),
        # Refused and missing pages: the expected status is part of the list, a different answer is a failure.
        Page("denied-start", "pingleader", u("start_fleet"), status=403),
        Page("denied-send-ping", "member", u("send_ping"), status=403),
        Page("notfound-fleet", "member",
             lambda ctx: reverse("fleetops:operation_detail", args=["6f0c2c4e-3f6a-4d0a-9d52-0a1f2d3c4b5a"]), status=404),
        Page("notfound-preping", "member", u("operation_detail", op("scheduled")), status=404),
        Page("notfound-corp", "corp",
             lambda ctx: reverse("fleetops:corporation_statistics_detail", args=[ctx.other_corp_id]), status=404),
    ]
    return pages


def missing_requirement(page, ctx):
    """Why the tree cannot show ``page`` (an empty string when it can)."""
    from django.urls import NoReverseMatch, reverse

    from fleetops import models, views_config

    for requirement in page.requires:
        kind, _, name = requirement.partition(":")
        if kind == "url":
            try:
                reverse(f"fleetops:{name}")
            except NoReverseMatch:
                return f"the URL {name} does not exist in this tree"
        elif kind == "model":
            if not hasattr(models, name):
                return f"the model {name} does not exist in this tree"
        elif kind == "section":
            if name not in views_config.CONFIG_SECTIONS:
                return f"the configuration section {name} does not exist in this tree"
        elif kind == "field":
            model_name, _, field_name = name.partition(".")
            model = getattr(models, model_name, None)
            if model is None or field_name not in {f.name for f in model._meta.get_fields()}:
                return f"the field {name} does not exist in this tree"
    return ""


# --------------------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------------------

SHIPS = (
    (623, "Moa"), (620, "Osprey"), (11985, "Basilisk"), (11987, "Guardian"), (11978, "Scimitar"),
    (24698, "Drake"), (11993, "Cerberus"), (24702, "Hurricane"), (22456, "Sabre"), (11999, "Huginn"),
    (11963, "Rapier"), (587, "Rifter"),
)
CAPSULE = (670, "Capsule")
SYSTEMS = ((30000142, "Jita"), (30000144, "Perimeter"), (30002187, "Amarr"), (30002659, "Dodixie"))
JITA_STATION = 60003760
CORPORATIONS = ("DEFAULT_CORP", "OTHER_CORP", "THIRD_CORP")


def month_start(year, month):
    return datetime(year, month, 1, tzinfo=dt_timezone.utc)


def previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def build_data(login_count):
    """Create everything and return the context object the page URLs are built from."""
    import django
    from django.utils import timezone

    django.setup()

    from allianceauth.groupmanagement.models import AuthGroup
    from django.contrib.auth.models import Group

    from fleetops import models as m
    from fleetops.constants import FLEET_READ_SCOPE
    from fleetops.services import incentives
    from fleetops.services.operations import set_action
    from fleetops.srp.types import ClaimWarning
    from tests import factories as f

    ctx = SimpleNamespace()
    now = timezone.now()
    cur_year, cur_month = now.year, now.month
    prev_year, prev_month = previous_month(cur_year, cur_month)
    two_year, two_month = previous_month(prev_year, prev_month)
    cur_start = month_start(cur_year, cur_month)
    prev_start = month_start(prev_year, prev_month)

    def inside_current(fraction):
        return cur_start + (now - cur_start) * fraction

    def inside_previous(fraction):
        return prev_start + (cur_start - prev_start) * fraction

    # ---- settings -----------------------------------------------------------------------------------
    solo = m.FleetOpsSettings.get_solo()
    solo.fleet_live_refresh_seconds = 0
    solo.incentive_enabled = True
    solo.incentive_minimum_fleets = 2
    solo.attendance_limit = None
    solo.save()

    # ---- users ----------------------------------------------------------------------------------------
    stats_perms = f.MEMBER_PERMS + ["fleetops.view_all_stats"]
    admin_perms = f.MEMBER_PERMS + [
        "fleetops.start_fleet", "fleetops.manage_own_fleet", "fleetops.view_all_fleets", "fleetops.create_manual_fleet",
        "fleetops.manage_fleets", "fleetops.view_corp_stats", "fleetops.view_all_stats", "fleetops.manage_attendance",
        "fleetops.manage_incentives", "fleetops.manage_configuration", "fleetops.view_audit_log",
        "fleetops.request_srp", "fleetops.manage_srp",
    ]
    users = {}

    def role_user(username, perms, main_name, **kwargs):
        users[username] = f.create_user(username, perms=perms, main_name=main_name, **kwargs)
        return users[username]

    role_user("member", f.SRP_MEMBER_PERMS, "Example Pilot")
    role_user("fc", f.FC_PERMS + ["fleetops.request_srp"], "Example FC")
    role_user("fc_nowrite", f.FC_PERMS, "Example FC Two")
    role_user("lead", f.FC_LEAD_PERMS, "Example Lead")
    role_user("corp", f.CORP_MANAGEMENT_PERMS, "Example Corp Manager")
    role_user("stats", stats_perms, "Example Analyst")
    role_user("srp", f.SRP_MANAGER_PERMS, "Example Payer")
    role_user("admin", admin_perms, "Example Admin")
    fc_alt = f.add_alt(users["fc"], "Example FC Alt")
    f.add_token(users["fc"], f.main_of(users["fc"]))
    f.add_token(users["fc"], fc_alt)
    # Only the read scope: the controls of its fleet are disabled with a reason.
    f.add_token(users["fc_nowrite"], f.main_of(users["fc_nowrite"]), scopes=[FLEET_READ_SCOPE])
    f.add_token(users["lead"], f.main_of(users["lead"]))

    corp_cycle = (f.DEFAULT_CORP, f.OTHER_CORP, f.THIRD_CORP)
    for index in range(1, 31):
        # Ten pilots registered before the previous month, ten during it, ten this month.
        if index <= 10:
            joined = prev_start - timedelta(days=45 + index)
        elif index <= 20:
            joined = prev_start + timedelta(days=index - 10)
        else:
            joined = cur_start + timedelta(minutes=index)
        users[f"pilot{index:02d}"] = f.member(
            f"pilot{index:02d}",
            corporation=corp_cycle[index % 3],
            perms=f.MEMBER_PERMS,
            main_name=f"Pilot {index:02d}",
            joined=min(joined, now - timedelta(minutes=1)),
        )
    ctx.users = users
    ctx.other_corp_id = f.OTHER_CORP[0]
    # The corporation manager belongs to the first corporation; its statistics are not the second one's.
    pilots = [users[f"pilot{i:02d}"] for i in range(1, 31)]

    # ---- configuration -------------------------------------------------------------------------------
    types = {name: f.fleet_type(name, weight) for name, weight in
             (("StratOp", "2.00"), ("CTA", "3.00"), ("Roam", "1.00"), ("Home Defense", "1.50"))}
    f.multiplier_tiers((90, 2), (180, 3))
    # A target from the previous month on: the month before it has none (FB11.2).
    m.StatisticsTarget.objects.all().delete()
    f.statistics_target(prev_year, prev_month, "3.00", minimum=2, note="Example target")
    f.special_role("Back Seat FC", key="backseat_fc", grants_fc_credit=True)
    if not m.ShipFunction.objects.exists():
        f.ship_function("DPS", rules=(("group", 25),), color="#c0392b", is_default=True)
        f.ship_function("Logistics", rules=(("group", 832),), color="#16a085")
    f.structure_template("Standard", "Main: Alpha, Bravo\nLogi: Logi")
    m.CommsPreset.objects.get_or_create(name="Example Voice", defaults={"channel_name": "Fleet 1", "is_active": True})
    role_a = f.ping_role("Sig A")
    f.ping_role("Sig B")
    alliance_channel = f.ping_channel("Alliance Pings", allow_here=True)
    alliance_channel.allowed_roles.set([role_a])
    f.builtin_default_templates()
    f.settings(srp_provider="auto")

    # ---- closed fleets: the statistics ------------------------------------------------------------------
    fc, lead = users["fc"], users["lead"]
    others = {"fc2": users["fc_nowrite"], "lead": lead}

    def closed(fc_user, started, ftype, attendees, *, hours=2, multiplier=1, **extra):
        operation = f.closed_operation(
            fc_user,
            started_at=started,
            type_obj=types[ftype],
            attendance_multiplier=multiplier,
            multiplier_source=m.FleetOperation.MultiplierSource.FC,
            doctrine_name=extra.pop("doctrine_name", "Example Doctrine"),
            ended_at=started + timedelta(hours=hours),
            end_reason=m.FleetOperation.EndReason.MANUAL,
            **extra,
        )
        for user, kwargs in attendees:
            f.add_attendance(operation, user, value=multiplier, **{"tracked_seconds": int(hours * 3600), **kwargs})
        return operation

    closed_ops = []
    for step, fraction in enumerate((0.15, 0.35, 0.55, 0.8)):
        group = pilots[step * 3: step * 3 + 14] + [users["member"]]
        attendees = [(user, {}) for user in group[:-3]]
        # One capped, one below the minimum time, one not granted.
        attendees.append((group[-3], {"capped": True, "granted": False}))
        attendees.append((group[-2], {"below_minimum": True, "granted": False, "tracked_seconds": 240}))
        attendees.append((group[-1], {}))
        owner = (fc, others["fc2"], fc, lead)[step]
        closed_ops.append(closed(owner, inside_current(fraction), ("StratOp", "CTA", "Roam", "Home Defense")[step],
                                 attendees, multiplier=(1, 2, 1, 3)[step]))
    for step, fraction in enumerate((0.1, 0.3, 0.5, 0.7, 0.9)):
        group = pilots[step * 2: step * 2 + 12] + [users["member"]]
        closed_ops.append(
            closed((fc, others["fc2"], fc, lead, fc)[step], inside_previous(fraction),
                   ("StratOp", "Roam", "CTA", "StratOp", "Home Defense")[step], [(user, {}) for user in group],
                   multiplier=(1, 1, 2, 1, 1)[step])
        )
    ctx.closed = closed_ops[0]
    # The page of the closed fleet: member took part and the record has messages and a role.
    f.special_role("Back Seat FC", key="backseat_fc", grants_fc_credit=True)
    m.OperationRoleAssignment.objects.create(
        operation=ctx.closed, role="backseat_fc", character_id=f.main_of(users["pilot01"]).character_id,
        character_name=f.main_of(users["pilot01"]).character_name, auth_user=users["pilot01"],
        grants_fc_credit=True, assigned_by=fc,
    )
    set_action(ctx.closed, "fleet_detection", True)
    set_action(ctx.closed, "discord_ping", True)
    set_action(ctx.closed, "motd_update", True)
    set_action(ctx.closed, "srp_link", None, "SRP creation intentionally skipped.")
    set_action(ctx.closed, "fleet_end", True)

    # ---- manual and reviewed fleets ----------------------------------------------------------------------
    def manual_fleet(review_status, **extra):
        operation = f.create_operation(
            fc, status=m.FleetOperation.Status.CLOSED, is_manual=True, esi_fleet_id=None,
            review_status=review_status, started_at=inside_current(0.45), multiplier_source="manual",
            doctrine_name="Manual record", **extra,
        )
        for user in pilots[:6]:
            f.add_attendance(operation, user, source=m.AttendanceRecord.Source.MANUAL, review_status=review_status,
                             created_by=fc)
        return operation

    ctx.manual_pending = manual_fleet(m.ReviewStatus.PENDING)
    ctx.manual_rejected = manual_fleet(
        m.ReviewStatus.REJECTED, reviewed_by=lead, reviewed_at=now, review_note="The roster does not match the screenshot",
    )
    ctx.manual_resubmitted = manual_fleet(m.ReviewStatus.PENDING, review_request_note="Roster corrected, please check again")
    f.add_attendance(ctx.manual_pending, users["member"], source=m.AttendanceRecord.Source.MANUAL,
                     review_status=m.ReviewStatus.PENDING, created_by=fc, notes="Joined late")
    # A pending manual row on a fleet that is already approved.
    f.add_attendance(closed_ops[1], users["pilot30"], source=m.AttendanceRecord.Source.MANUAL,
                     review_status=m.ReviewStatus.PENDING, created_by=fc, notes="Forgot to join the channel")
    # A rejected manual row on another approved fleet.
    f.add_attendance(closed_ops[2], users["pilot29"], source=m.AttendanceRecord.Source.MANUAL, granted=False,
                     review_status=m.ReviewStatus.REJECTED, created_by=fc, reviewed_by=lead, reviewed_at=now,
                     review_note="Not in the fleet", notes="Was on another character")

    # ---- the fleets that run now -------------------------------------------------------------------------
    started = now - timedelta(minutes=47)
    wings = f.wings(("Main", ("Alpha", "Bravo")), ("Logi", ("Logi",)))
    ctx.active = f.create_operation(
        fc, status=m.FleetOperation.Status.ACTIVE, type_obj=types["StratOp"], started_at=started,
        last_esi_update=now, fleet_wings=wings, fleet_wings_read_at=now, ping_channel=alliance_channel,
        ping_mention_mode=m.FleetOperation.MentionMode.NONE, doctrine_name="Example Doctrine",
        doctrine_fits=[{"fit_id": 1, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"},
                       {"fit_id": 2, "fit": "Logi Guardian", "ship_type_id": 11987, "ship": "Guardian"}],
        comms=m.CommsPreset.objects.get(name="Example Voice"), tracking_enabled=True,
        additional_message="Form up on the Jita 4-4 undock.",
    )
    set_action(ctx.active, "fleet_detection", True)
    set_action(ctx.active, "message_render", True)
    set_action(ctx.active, "discord_ping", False, "The ping channel did not answer; the message was not delivered.")
    set_action(ctx.active, "discord_ping", False, "The ping channel did not answer; the message was not delivered.")
    set_action(ctx.active, "motd_update", True)
    set_action(ctx.active, "srp_link", True)
    set_action(ctx.active, "tracking_start", True)

    squads = [(w["id"], s["id"]) for w in wings for s in w["squads"]]
    roster = [users["fc"], users["member"], *pilots[:20]]
    index = 0
    for slot in range(25):
        wing_id, squad_id = squads[slot % len(squads)]
        ship = CAPSULE if slot == 7 else SHIPS[slot % len(SHIPS)]
        system = SYSTEMS[slot % 3]
        extra = dict(ship_type_id=ship[0], ship_type_name=ship[1], solar_system_id=system[0],
                     solar_system_name=system[1], wing_id=wing_id, squad_id=squad_id, fleet_role="squad_member",
                     accumulated_seconds=60 * (47 - slot), takes_fleet_warp=slot % 5 != 0)
        if slot == 3:
            extra.update(station_id=JITA_STATION, solar_system_id=30000142, solar_system_name="Jita")
        if slot == 0:
            extra.update(fleet_role="fleet_commander", wing_id=-1, squad_id=-1)
        if slot < 22:
            user = roster[index]
            character = f.main_of(user)
            index += 1
            f.member_state(ctx.active, character, user=user, **extra)
        else:
            # Pilots Alliance Auth does not know.
            f.member_state(ctx.active, 95_100_000 + slot, name=f"Visitor {slot}", **extra)
    for name, kind, old, new in (
        ("Pilot 01", m.FleetMemberEvent.EventType.JOIN, "", "Joined"),
        ("Pilot 02", m.FleetMemberEvent.EventType.SHIP_CHANGE, "Moa", "Osprey"),
        ("Pilot 03", m.FleetMemberEvent.EventType.SYSTEM_CHANGE, "Perimeter", "Jita"),
        ("Pilot 04", m.FleetMemberEvent.EventType.LEAVE, "", "Left"),
    ):
        m.FleetMemberEvent.objects.create(operation=ctx.active, character_id=95_000_000 + len(name), character_name=name,
                                          event_type=kind, old_value=old, new_value=new)
    ctx.srp_link = f.fleet_link(ctx.active, code="example-fleet-link")
    ctx.pap_link = f.pap_link(ctx.active, minutes=25)

    ctx.active_nowrite = f.create_operation(
        users["fc_nowrite"], status=m.FleetOperation.Status.ACTIVE, type_obj=types["Roam"],
        started_at=now - timedelta(minutes=20), last_esi_update=now, fleet_wings=f.wings(("Main", ("Alpha",))),
        ping_channel=alliance_channel, tracking_enabled=True,
    )
    for slot, user in enumerate(pilots[20:26]):
        f.member_state(ctx.active_nowrite, f.main_of(user), user=user, ship_type_id=SHIPS[slot][0],
                       ship_type_name=SHIPS[slot][1], solar_system_id=30000142, solar_system_name="Jita")
    set_action(ctx.active_nowrite, "fleet_detection", True)
    set_action(ctx.active_nowrite, "tracking_start", True)

    ctx.scheduled = f.create_scheduled_operation(fc, scheduled_at=now + timedelta(hours=3, minutes=5),
                                                 type_obj=types["CTA"], ping_channel=alliance_channel,
                                                 doctrine_name="Example Doctrine", formup="Jita")
    set_action(ctx.scheduled, "discord_ping", True)

    ctx.link_only = f.create_operation(
        fc, status=m.FleetOperation.Status.ACTIVE, type_obj=types["Home Defense"], started_at=now - timedelta(minutes=12),
        link_only=True, esi_fleet_id=None, tracking_enabled=False, doctrine_name="",
    )
    link_only_link = f.pap_link(ctx.link_only, minutes=40)
    f.registration(ctx.link_only, f.main_of(pilots[3]), link=link_only_link)
    f.registration(ctx.link_only, f.main_of(pilots[4]), link=link_only_link)

    # ---- incentives -------------------------------------------------------------------------------------
    period, _created = m.IncentivePeriod.objects.get_or_create(year=cur_year, month=cur_month)
    period.budget = 500_000_000
    period.minimum_fleets = 2
    period.save()
    incentives.rebuild_period(period)
    period.status = m.IncentivePeriod.Status.REVIEW
    period.calculated_at = now
    period.save()

    # ---- SRP ---------------------------------------------------------------------------------------------
    f.srp_settings(enabled=True, percentage=Decimal("60"))
    f.payout_rule(f.RIFTER, 20_000_000, ship_name="Rifter")
    f.payout_rule(623, 45_000_000, ship_name="Moa", doctrine="Example Doctrine")
    f.payout_rule(f.CAPSULE, 1_000_000, ship_name="Capsule")
    member = users["member"]
    victims = [f.main_of(member), f.add_alt(member, "Example Pilot Alt")]
    over_limit = ClaimWarning("over_limit", "danger", {"kind": "personal", "requested": 45_000_000, "remaining": 30_000_000})
    ctx.claim_pending = f.srp_claim(
        member, character=victims[0], amount=45_000_000, link=ctx.srp_link,
        warnings=[ClaimWarning("value_estimate").as_json(), over_limit.as_json()],
        over_limit=True, member_comment="Lost to a bubble camp.",
    )
    f.claim_event(ctx.claim_pending, m.SrpClaimEvent.Action.SUBMIT, actor=member)
    pending_two = f.srp_claim(users["pilot02"], amount=20_000_000,
                              warnings=[ClaimWarning("external_claim", params={"claims": ["Other SRP: Pending"]}).as_json()])
    f.claim_event(pending_two, m.SrpClaimEvent.Action.SUBMIT, actor=users["pilot02"])
    approved = f.srp_claim(users["pilot03"], status=m.SrpClaim.Status.APPROVED, amount=20_000_000, decided_by=users["srp"])
    f.claim_event(approved, m.SrpClaimEvent.Action.APPROVE, actor=users["srp"], amount=20_000_000)
    ctx.claim_paid = f.srp_claim(member, character=victims[1], status=m.SrpClaim.Status.PAID, amount=20_000_000,
                                 decided_by=users["srp"])
    f.claim_event(ctx.claim_paid, m.SrpClaimEvent.Action.SUBMIT, actor=member)
    f.claim_event(ctx.claim_paid, m.SrpClaimEvent.Action.APPROVE, actor=users["srp"], amount=20_000_000)
    f.claim_event(ctx.claim_paid, m.SrpClaimEvent.Action.PAID, actor=users["srp"], amount=20_000_000)
    f.srp_claim(users["pilot04"], status=m.SrpClaim.Status.REJECTED, amount=20_000_000, decided_by=users["srp"])
    f.srp_claim(users["pilot05"], status=m.SrpClaim.Status.WITHDRAWN, amount=20_000_000)

    # ---- groups (only in trees that have the models) ----------------------------------------------------
    build_group_data(ctx, users, types, alliance_channel, now, f, m, Group, AuthGroup)

    prime_eve_links(users, f)

    # ---- logins ---------------------------------------------------------------------------
    from django.test import Client

    logins = {}
    for role, user in users.items():
        if role.startswith("pilot") and role[5:].isdigit():
            continue
        values = []
        for _copy in range(login_count):
            client = Client()
            client.force_login(user)
            values.append(client.cookies["sessionid"].value)
        logins[role] = values
    ctx.logins = logins
    return ctx


def prime_eve_links(users, f):
    """Teach the form-up and FC lookups of the start page the seeded values, so no page reaches ESI (R27).

    ``eve_links`` arrives with lane A; until then there is nothing to prime.
    """
    try:
        from fleetops.services import eve_links
    except ImportError:
        return

    remember_type = getattr(eve_links, "remember_character_type", None)
    remember_system = getattr(eve_links, "remember_solar_system", None)
    if remember_type is not None:
        for role, user in users.items():
            if role.startswith("pilot") and role[5:].isdigit():
                continue
            for ownership in user.character_ownerships.select_related("character"):
                remember_type(ownership.character.character_id, 1378)
    if remember_system is not None:
        remember_system("Jita", 30000142, "Jita")


def build_group_data(ctx, users, types, alliance_channel, now, f, m, Group, AuthGroup):
    """The group leaders, the groups with their access rows, the group fleet and the two pings without a fleet."""
    ctx.group_access_fleets = ctx.group_access_ping = ctx.channel_groups = ctx.group_fleet = None
    pingleader = f.create_user("pingleader", perms=f.MEMBER_PERMS, main_name="Example Ping Leader")
    fleetleader = f.create_user("fleetleader", perms=f.MEMBER_PERMS, main_name="Example Fleet Leader")
    fleetleader2 = f.create_user("fleetleader2", perms=f.MEMBER_PERMS, main_name="Example Fleet Leader Two")
    fcleader = f.create_user("fcleader", perms=f.FC_PERMS, main_name="Example FC Leader")
    users.update(pingleader=pingleader, fleetleader=fleetleader, fleetleader2=fleetleader2, fcleader=fcleader)
    fleet_group, _created = Group.objects.get_or_create(name="Example Fleet Group")
    ping_group, _created = Group.objects.get_or_create(name="Example Ping Group")
    leader_group, _created = Group.objects.get_or_create(name="Example Fleet Leaders")
    group_type = f.fleet_type("Group Activity", "0.00")
    fleetleader.groups.add(leader_group)
    # Leaders are added by primary key: Alliance Auth 5.4 and later point the relation at proxy models.
    AuthGroup.objects.get_or_create(group=ping_group)[0].group_leaders.add(pingleader.pk)
    fleet_auth_group = AuthGroup.objects.get_or_create(group=fleet_group)[0]
    fleet_auth_group.group_leaders.add(fleetleader2.pk, fcleader.pk)
    fleet_auth_group.group_leader_groups.add(leader_group.pk)

    ctx.group_access_fleets = m.GroupAccess.objects.create(group=fleet_group, mode="fleets", srp_enabled=False,
                                                           note="Example group with fleets")
    ctx.group_access_fleets.fleet_types.add(group_type)
    ctx.group_access_ping = m.GroupAccess.objects.create(group=ping_group, mode="ping", note="Example ping-only group")

    ctx.channel_groups = f.ping_channel("Group Pings")
    ctx.channel_groups.allowed_groups.set([fleet_group, ping_group])
    ctx.channel_groups.fcs_allowed = False
    ctx.channel_groups.save()

    group_fleet = f.create_operation(
        fleetleader, status=m.FleetOperation.Status.ACTIVE, type_obj=group_type, started_at=now - timedelta(minutes=25),
        last_esi_update=now, fleet_wings=f.wings(("Main", ("Alpha",))), ping_channel=ctx.channel_groups,
        tracking_enabled=True, group=fleet_group, group_name=fleet_group.name,
    )
    pilots = [users[f"pilot{i:02d}"] for i in range(1, 7)]
    for slot, user in enumerate(pilots):
        f.member_state(group_fleet, f.main_of(user), user=user, ship_type_id=SHIPS[slot][0], ship_type_name=SHIPS[slot][1],
                       solar_system_id=30000142, solar_system_name="Jita")
    ctx.group_fleet = group_fleet

    for status, result in (("sent", "Delivered."), ("failed", "The ping channel did not answer.")):
        m.StandalonePing.objects.create(
            request_id=uuid.uuid4(), sender=pingleader, sender_name="Example Ping Leader",
            group=ping_group, group_name=ping_group.name, channel=ctx.channel_groups,
            channel_name=ctx.channel_groups.name, additional_message="Example ping text.",
            text="Example ping text.", display_text="Example ping text.", status=status, result_message=result,
            http_status=204 if status == "sent" else 502,
        )


def point_database(path):
    """Use another SQLite file for the rest of this process."""
    from django.db import connections

    connection = connections["default"]
    connection.close()
    connection.settings_dict["NAME"] = str(path)


def migrations_key():
    """A name for the state of all migrations on disk, so a migrated empty database can be reused."""
    import hashlib

    import allianceauth
    import django
    from django.db.migrations.loader import MigrationLoader

    import fleetops

    digest = hashlib.sha256()
    digest.update(f"{django.__version__}|{allianceauth.__version__}|{sys.version_info[:2]}".encode())
    for node in sorted(MigrationLoader(None, load=True).graph.nodes):
        digest.update(repr(node).encode())
    # A migration edited in place keeps its name; its size and time tell.
    for path in sorted((Path(fleetops.__file__).parent / "migrations").glob("*.py")):
        stat = path.stat()
        digest.update(f"{path.name}{stat.st_size}{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


def prepare_database(db_path, work_dir):
    """Copy a migrated empty database to ``db_path`` (migrating once per state of the migrations)."""
    import shutil

    import django
    from django.core.management import call_command

    django.setup()
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    base = Path(work_dir) / f"base-{migrations_key()}.sqlite3"
    if not base.exists():
        for old in Path(work_dir).glob("base-*.sqlite3"):
            old.unlink(missing_ok=True)
        building = base.with_suffix(f".building-{os.getpid()}")
        building.unlink(missing_ok=True)
        point_database(building)
        call_command("migrate", verbosity=0, interactive=False)
        point_database(":memory:")
        building.replace(base)
    shutil.copyfile(base, db_path)
    # Several browser contexts write sessions and profiles at the same time: let readers and the writer work together.
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    finally:
        connection.close()
    point_database(db_path)


def check_pages(pages, ctx):
    """Request every available page once without a browser; returns the number of unexpected answers."""
    from django.test import Client

    failed = 0
    for page in pages:
        if page["skip"]:
            print(f"SKIP {page['id']:32} {page['skip']}")
            continue
        client = Client(HTTP_HOST="127.0.0.1")
        client.cookies["sessionid"] = ctx.logins[page["role"]][0]
        response = client.get(page["url"])
        ok = response.status_code == page["status"]
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {page['id']:32} {response.status_code} (expected {page['status']})")
    return failed


def theme_hooks():
    """``{"flatly": "allianceauth.theme.flatly.auth_hooks.FlatlyThemeHook", ...}`` from Alliance Auth's hooks."""
    from allianceauth.hooks import get_hooks

    found = {}
    for hook in get_hooks("theme_hook"):
        theme = hook()
        tags = theme.html_tags or ""
        key = ""
        for part in str(tags).split():
            if part.startswith("data-theme="):
                key = part.split("=", 1)[1].strip("\"'")
        found[key or theme.name.lower()] = f"{theme.__class__.__module__}.{theme.__class__.__name__}"
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", help="write the JSON description of the run here")
    parser.add_argument("--db", help="database file (default: FLEETOPS_UI_DB or tools/ui_check/.work/ui.sqlite3)")
    parser.add_argument("--logins", type=int, default=4, help="login cookies per role (one per parallel browser context)")
    parser.add_argument("--check", action="store_true", help="after seeding, request every page once and print its status")
    parser.add_argument("--list", action="store_true", help="print the page list and exit without a database")
    parser.add_argument("--lane", help="with --list: only the pages of this lane (A-F or S0)")
    parser.add_argument("--pages", help="with --list: comma separated ids or patterns")
    args = parser.parse_args(argv)

    if args.list:
        os.environ.setdefault("FLEETOPS_UI_DB", ":memory:")
        import django

        django.setup()
        wanted = [p for p in (args.pages or "").split(",") if p]
        for page in build_pages():
            if args.lane and page.lane != args.lane.upper():
                continue
            if wanted and not any(fnmatch.fnmatchcase(page.id, pattern) for pattern in wanted):
                continue
            print(f"{page.id:32} {page.lane:3} {page.role:12} {page.status} {','.join(page.states)}")
        return 0

    if args.db:
        os.environ["FLEETOPS_UI_DB"] = str(Path(args.db).resolve())
    from tools.ui_check import settings as ui_settings  # noqa: F401  (reads the environment)

    db_path = Path(os.environ.get("FLEETOPS_UI_DB") or ui_settings.DATABASES["default"]["NAME"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)

    prepare_database(db_path, ui_settings.WORK_DIR)
    ctx = build_data(max(1, args.logins))
    from django.conf import settings

    pages = []
    for page in build_pages():
        reason = missing_requirement(page, ctx)
        entry = {
            "id": page.id, "role": page.role, "lane": page.lane, "status": page.status, "states": list(page.states),
            "requires": list(page.requires), "skip": reason,
        }
        if not reason:
            entry["url"] = page.url(ctx)
        else:
            entry["url"] = ""
        if page.role not in ctx.logins:
            entry["skip"] = entry["skip"] or f"the role {page.role} does not exist in this tree"
        pages.append(entry)
    result = {
        "database": str(db_path),
        "logins": ctx.logins,
        "themes": theme_hooks(),
        "pages": pages,
        "users": {role: user.username for role, user in ctx.users.items() if not role.startswith("pilot") or not role[5:].isdigit()},
        "language_cookie": settings.LANGUAGE_COOKIE_NAME,
    }
    text = json.dumps(result, indent=1)
    if args.check:
        failed = check_pages(pages, ctx)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(f"seeded {len([p for p in pages if not p['skip']])} pages, {len(ctx.logins)} roles")
    return 1 if args.check and failed else 0


if __name__ == "__main__":
    sys.exit(main())
