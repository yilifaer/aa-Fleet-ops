# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Permission matrix, object isolation and page rendering tests for FleetOps views."""

import uuid
from collections import namedtuple
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.test import Client, RequestFactory, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from fleetops import views_members, views_statistics
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    ChannelPreset,
    CommsPreset,
    DiscordWebhook,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    FleetStartPreset,
    FleetStructureTemplate,
    IncentivePeriod,
    MessageTemplate,
    MonthlyFCStatistic,
    OperationAction,
    OperationRoleAssignment,
    PingRole,
    PingTarget,
    ShipFunction,
    SrpClaim,
    SrpSettings,
)
from fleetops.providers.esi import FleetDetectionResult, FleetESIError
from fleetops.providers.srp import SRPLinkResult

from . import factories as f
from .fleet_sim import FleetSim

WEBHOOK_SECRET = "s3cr3t-webhook-token"
WEBHOOK_URL = f"https://discord.com/api/webhooks/1234567890/{WEBHOOK_SECRET}"
CAPSULE_TYPE_ID = 670

ACTIVE = FleetOperation.Status.ACTIVE
CLOSED = FleetOperation.Status.CLOSED
CANCELLED = FleetOperation.Status.CANCELLED
AUTOMATIC = AttendanceRecord.Source.AUTOMATIC
MANUAL = AttendanceRecord.Source.MANUAL

OK = 200
REDIRECT = 302
DENIED = 403
NOT_ALLOWED = 405
HIDDEN = "hidden"  # object-level isolation: 403 or 404 are both acceptable

ROLES = ("member", "corp", "fc", "lead")
# A member with view_all_stats and nothing else. Only the statistics rows name this role; the rows of the
# other routes do not, and the statistics manager's matrix skips them.
STATS = "stats"

Case = namedtuple("Case", "label method url data expected")

REQUEST_SRP = "fleetops.request_srp"
MANAGE_SRP = "fleetops.manage_srp"
# The SRP rows name their own roles, built from explicit permissions, so they do not depend on the bundles above.
SRP_ROLES = (
    "srp_none",  # FleetOps access without request_srp
    "srp_member",  # request_srp; the claimant of most claims
    "srp_other",  # request_srp; claimed nothing
    "srp_fc",  # request_srp and FC permissions; led the fleet of the fleet claim
    "srp_corp",  # request_srp and view_corp_stats, same corporation as the claimants
    "srp_corp_other",  # request_srp and view_corp_stats of another corporation
    "srp_stats",  # view_all_stats only
    "srp_manager",  # manage_srp; has a claim of their own
    "srp_manager2",  # manage_srp; no claim of their own
)
SRP_REQUESTERS = frozenset(SRP_ROLES) - {"srp_none", "srp_stats"}
SRP_MANAGERS = frozenset({"srp_manager", "srp_manager2"})


def by_srp_role(allowed, *, granted=OK, otherwise=DENIED, **overrides):
    """Expected status per SRP role: ``granted`` for the roles in ``allowed``, ``otherwise`` for the rest."""
    expected = {role: (granted if role in allowed else otherwise) for role in SRP_ROLES}
    expected.update(overrides)
    return expected

CONFIG_SECTIONS = (
    "fleet-types", "multiplier-tiers", "special-roles", "comms", "channels", "ping-channels", "ping-roles", "templates",
)


def add_member_state(
    operation,
    character,
    user=None,
    *,
    ship_type_id=11987,
    ship_type_name="Guardian",
    fleet_role="squad_member",
    active=True,
    system_id=30000142,
    system_name="Jita",
):
    main = f.main_of(user) if user else None
    now = timezone.now()
    return FleetMemberState.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        auth_user=user,
        corporation_id=character.corporation_id,
        corporation_name=character.corporation_name,
        alliance_id=character.alliance_id,
        alliance_name=character.alliance_name or "",
        ship_type_id=ship_type_id,
        ship_type_name=ship_type_name,
        solar_system_id=system_id,
        solar_system_name=system_name,
        fleet_role=fleet_role,
        wing_id=1,
        squad_id=1,
        first_seen=operation.started_at,
        last_seen=now,
        left_at=None if active else now,
        is_active=active,
    )


def assign_role(operation, character, user=None, *, role=OperationRoleAssignment.Role.LOGI_ANCHOR, credit=False):
    main = f.main_of(user) if user else None
    source = main or character
    return OperationRoleAssignment.objects.create(
        operation=operation,
        role=role,
        character_id=character.character_id,
        character_name=character.character_name,
        main_character_id=main.character_id if main else None,
        main_character_name=main.character_name if main else "",
        auth_user=user,
        corporation_id=source.corporation_id,
        corporation_name=source.corporation_name,
        grants_fc_credit=credit,
    )


def add_event(operation, character, event_type, old="", new=""):
    return FleetMemberEvent.objects.create(
        operation=operation,
        character_id=character.character_id,
        character_name=character.character_name,
        event_type=event_type,
        old_value=old,
        new_value=new,
    )


class FleetOpsViewTestCase(TestCase):
    """Builds a small but realistic alliance with fleets owned by two FCs."""

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.stratop = f.fleet_type("StratOp", "2.00")
        cls.home_defense = f.fleet_type("Home Defense", "1.00")
        cls.comms = CommsPreset.objects.create(
            name="Alliance Mumble", channel_name="Ops 1", voice_url="mumble://voice.example.com/ops1"
        )
        cls.logi = ChannelPreset.objects.create(
            name="Logi", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="TEST Logi"
        )
        cls.boost = ChannelPreset.objects.create(
            name="Boost", channel_type=ChannelPreset.ChannelType.BOOST, channel_value="TEST Boost"
        )
        cls.webhook = DiscordWebhook.objects.create(name="Ops Pings", webhook_url=WEBHOOK_URL)
        cls.ping_target = PingTarget.objects.create(name="Everyone", target_value="@everyone", webhook=cls.webhook)
        cls.ping_role = PingRole.objects.create(name="Sig A", role_id=123456789012345678)
        cls.ping_template = MessageTemplate.objects.create(
            name="Standard ping",
            template_type=MessageTemplate.TemplateType.PING,
            content="{{ ping_target }} {{ fleet_type }} FC {{ fc }} form up {{ formup }}",
            is_default=True,
        )
        cls.motd_template = MessageTemplate.objects.create(
            name="Standard MOTD",
            template_type=MessageTemplate.TemplateType.MOTD,
            content="<b>{{ fleet_type }}</b><br>FC: {{ fc }}",
            is_default=True,
        )
        f.settings(incentive_enabled=True)

        cls.member = f.create_user("member", perms=f.MEMBER_PERMS)
        cls.corp_manager = f.create_user("corpmanager", perms=f.CORP_MANAGEMENT_PERMS)
        cls.fc = f.create_user("fc", perms=f.FC_PERMS)
        cls.other_fc = f.create_user("otherfc", perms=f.FC_PERMS)
        cls.lead = f.create_user("fclead", perms=f.FC_LEAD_PERMS)
        cls.no_perms = f.create_user("noperms")
        cls.stats_manager = f.create_user("statsmanager", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        cls.no_main = f.create_user("nomain", perms=f.MEMBER_PERMS, with_main=False)
        cls.outsider = f.create_user("outsider", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)
        cls.users = {
            "member": cls.member,
            "corp": cls.corp_manager,
            "fc": cls.fc,
            "lead": cls.lead,
            STATS: cls.stats_manager,
        }

        cls.fc_main = f.main_of(cls.fc)
        cls.fc_alt = f.add_alt(cls.fc, "FC Alt")
        cls.member_main = f.main_of(cls.member)
        cls.other_fc_main = f.main_of(cls.other_fc)
        cls.outsider_main = f.main_of(cls.outsider)
        cls.pod_pilot = f.create_character("Neutral Pod", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_token(cls.fc, cls.fc_main)

        # FC's active fleet with tracking data, attendance, roles and actions.
        cls.own_active = f.create_operation(
            cls.fc,
            type_obj=cls.stratop,
            doctrine_name="Muninn Fleet",
            comms=cls.comms,
            logi_channel=cls.logi,
            boost_channel=cls.boost,
            ping_target=cls.ping_target,
            ping_template=cls.ping_template,
            motd_template=cls.motd_template,
            ping_text="@everyone StratOp FC fc Main form up Jita",
            motd_text="<b>StratOp</b><br>FC: fc Main",
            additional_message="Bring links",
            srp_provider="allianceauth_builtin",
            srp_reference="42",
            srp_url="/srp/42/",
            last_esi_update=now - timedelta(minutes=10),
        )
        add_member_state(cls.own_active, cls.fc_main, cls.fc, fleet_role="fleet_commander")
        add_member_state(
            cls.own_active, cls.member_main, cls.member, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule"
        )
        add_member_state(cls.own_active, cls.fc_alt, cls.fc, active=False)
        add_member_state(cls.own_active, cls.pod_pilot, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule")
        add_event(cls.own_active, cls.member_main, FleetMemberEvent.EventType.JOIN)
        add_event(cls.own_active, cls.member_main, FleetMemberEvent.EventType.SHIP_CHANGE, "Guardian", "Capsule")
        add_event(cls.own_active, cls.fc_alt, FleetMemberEvent.EventType.LEAVE)
        f.add_attendance(cls.own_active, cls.fc)
        f.add_attendance(cls.own_active, cls.member)
        f.add_attendance(cls.own_active, cls.fc, cls.fc_alt, granted=False, capped=True)
        cls.own_role = assign_role(cls.own_active, cls.member_main, cls.member)
        for action, status, error in (
            ("fleet_detection", OperationAction.Status.SUCCESS, ""),
            ("discord_ping", OperationAction.Status.FAILED, "Discord returned HTTP 500"),
            ("motd_update", OperationAction.Status.SUCCESS, ""),
            ("tracking_start", OperationAction.Status.SUCCESS, ""),
            ("srp_link", OperationAction.Status.SUCCESS, "Built-in Alliance Auth SRP fleet created."),
        ):
            OperationAction.objects.create(operation=cls.own_active, action=action, status=status, error_message=error)

        # FC's closed fleet that the member did not attend.
        cls.own_closed = f.create_operation(
            cls.fc, status=CLOSED, type_obj=cls.home_defense, started_at=now - timedelta(days=2)
        )
        add_member_state(cls.own_closed, cls.fc_main, cls.fc, fleet_role="fleet_commander", active=False)
        add_member_state(cls.own_closed, cls.outsider_main, cls.outsider, active=False)
        f.add_attendance(cls.own_closed, cls.fc)
        cls.own_manual = f.add_attendance(cls.own_closed, cls.outsider, source=MANUAL)

        # Another FC's fleets.
        cls.other_active = f.create_operation(cls.other_fc, type_obj=cls.stratop, ping_target=cls.ping_target)
        add_member_state(cls.other_active, cls.other_fc_main, cls.other_fc, fleet_role="fleet_commander")
        add_member_state(
            cls.other_active, cls.outsider_main, cls.outsider, ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule"
        )
        f.add_attendance(cls.other_active, cls.other_fc)
        cls.other_role = assign_role(cls.other_active, cls.outsider_main, cls.outsider)

        cls.other_closed = f.create_operation(
            cls.other_fc, status=CLOSED, type_obj=cls.home_defense, started_at=now - timedelta(days=3)
        )
        add_member_state(cls.other_closed, cls.other_fc_main, cls.other_fc, fleet_role="fleet_commander", active=False)
        add_member_state(cls.other_closed, cls.outsider_main, cls.outsider, active=False)
        f.add_attendance(cls.other_closed, cls.other_fc)
        cls.other_manual = f.add_attendance(cls.other_closed, cls.outsider, source=MANUAL)

        # Another FC's fleet where our FC was assigned Back Seat FC with FC credit.
        cls.credited_closed = f.create_operation(
            cls.other_fc, status=CLOSED, type_obj=cls.stratop, started_at=now - timedelta(days=4)
        )
        add_member_state(
            cls.credited_closed, cls.other_fc_main, cls.other_fc, fleet_role="fleet_commander", active=False
        )
        add_member_state(cls.credited_closed, cls.fc_main, cls.fc, active=False)
        assign_role(
            cls.credited_closed, cls.fc_main, cls.fc, role=OperationRoleAssignment.Role.BACKSEAT_FC, credit=True
        )

        # Pre-pinged fleets waiting for their FC.
        cls.own_scheduled = f.create_scheduled_operation(cls.fc, ping_channel=cls.webhook, ping_text="Pre ping")
        cls.other_scheduled = f.create_scheduled_operation(cls.other_fc, ping_channel=cls.webhook)

        cls.period = IncentivePeriod.objects.create(
            year=now.year,
            month=now.month,
            status=IncentivePeriod.Status.REVIEW,
            budget=1_000_000_000,
            minimum_fleets=1,
        )
        MonthlyFCStatistic.objects.create(
            period=cls.period,
            fc_user=cls.fc,
            fleet_count=2,
            total_points=Decimal("3.00"),
            fleet_type_breakdown={"StratOp": {"count": 1, "points": 2.0}},
            eligible=True,
            calculated_payout=1_000_000_000,
            final_payout=1_000_000_000,
        )
        AuditLog.objects.create(
            actor=cls.lead,
            action="configuration.update",
            object_type="FleetType",
            object_id=str(cls.stratop.pk),
            old_value={"point_weight": "1.00"},
            new_value={"point_weight": "2.00"},
            reason="Weight review",
        )

        # PAP links of both FCs' active fleets and a Fleet Start preset of the FC.
        cls.own_link = f.pap_link(cls.own_active)
        cls.other_link = f.pap_link(cls.other_active)
        cls.fc_preset = FleetStartPreset.objects.create(owner=cls.fc, name="Muninn")

        cls.config_objects = {
            "fleet-types": cls.stratop,
            "multiplier-tiers": f.multiplier_tiers()[0],
            "special-roles": f.special_role("Scout"),
            "comms": cls.comms,
            "channels": cls.logi,
            "ping-channels": cls.webhook,
            "ping-roles": cls.ping_role,
            "templates": cls.ping_template,
        }

    def setUp(self):
        super().setUp()
        self.discord_post = self.patch("fleetops.providers.pings.requests.post")
        self.discord_post.return_value = mock.Mock(status_code=204, text="")
        self.sync = self.patch("fleetops.services.operations.sync_operation", return_value=0)
        self.set_motd = self.patch("fleetops.services.operations.set_fleet_motd")
        self.create_srp = self.patch(
            "fleetops.services.operations.create_srp_link",
            return_value=SRPLinkResult(
                "allianceauth_builtin", reference="77", url="/srp/77/", created=True, message="created"
            ),
        )
        self.kick = self.patch("fleetops.services.fleet_controls.kick_fleet_member")
        self.detect = self.patch(
            "fleetops.views.detect_character_fleet",
            side_effect=lambda user, character_id: FleetDetectionResult(
                fleet_id=1_000_000_001, role="fleet_commander", fleet_boss_id=character_id
            ),
        )
        self.start_detect = self.patch(
            "fleetops.services.operations.detect_character_fleet",
            side_effect=lambda user, character_id, fresh=False: FleetDetectionResult(
                fleet_id=1_000_000_002, role="fleet_commander", fleet_boss_id=character_id
            ),
        )

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def client_for(self, user):
        client = Client(raise_request_exception=False)
        if user is not None:
            client.force_login(user)
        return client

    @staticmethod
    def login_url():
        return reverse("auth_login_user")

    def detail_url(self, operation):
        return reverse("fleetops:operation_detail", args=[operation.uuid])


class SrpViewTestCase(FleetOpsViewTestCase):
    """The alliance above plus the people, the fleet SRP links and the claims the SRP routes are tested with."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        f.srp_settings(enabled=True)
        access = ["fleetops.basic_access"]
        cls.srp_users = {
            "srp_none": f.create_user("srpnone", perms=access),
            "srp_member": f.create_user("srpmember", perms=[*access, REQUEST_SRP]),
            "srp_other": f.create_user("srpother", perms=[*access, REQUEST_SRP]),
            "srp_fc": f.create_user("srpfc", perms=[*f.FC_PERMS, REQUEST_SRP]),
            "srp_corp": f.create_user("srpcorp", perms=[*access, REQUEST_SRP, "fleetops.view_corp_stats"]),
            "srp_corp_other": f.create_user(
                "srpcorpother", perms=[*access, REQUEST_SRP, "fleetops.view_corp_stats"], corporation=f.OTHER_CORP
            ),
            "srp_stats": f.create_user("srpstats", perms=[*access, "fleetops.view_all_stats"]),
            "srp_manager": f.create_user("srpmanager", perms=[*access, REQUEST_SRP, MANAGE_SRP]),
            "srp_manager2": f.create_user("srpmanagertwo", perms=[*access, REQUEST_SRP, MANAGE_SRP]),
        }
        cls.users = {**cls.users, **cls.srp_users}
        fc = cls.srp_users["srp_fc"]
        member = cls.srp_users["srp_member"]

        # The fleet the member lost a ship in, and a second fleet whose SRP link was closed.
        cls.srp_fleet = f.create_operation(
            fc, status=CLOSED, type_obj=cls.stratop, started_at=now - timedelta(days=1), doctrine_name="Muninn Fleet"
        )
        cls.srp_link = f.fleet_link(cls.srp_fleet)
        cls.srp_closed_fleet = f.create_operation(
            fc, status=CLOSED, type_obj=cls.home_defense, started_at=now - timedelta(days=2)
        )
        cls.srp_closed_link = f.fleet_link(cls.srp_closed_fleet, is_open=False)

        loss_time = now - timedelta(hours=20)
        cls.claim_fleet = f.srp_claim(member, link=cls.srp_link, time=loss_time)
        cls.claim_out = f.srp_claim(member, time=loss_time)
        cls.claim_other_corp = f.srp_claim(cls.outsider, time=loss_time)
        cls.claim_manager = f.srp_claim(cls.srp_users["srp_manager"], time=loss_time)
        # An internal note of a reviewer and a line every viewer sees.
        f.claim_event(
            cls.claim_fleet, "note", actor=cls.srp_users["srp_manager2"], internal=True,
            reason="Looks like an alt of a spy",
        )
        f.claim_event(cls.claim_fleet, "submit", actor=member, to_status="pending", reason="Submitted by the pilot")

    def srp_url(self, name, *args):
        return reverse(f"fleetops:{name}", args=args)

    def claim_url(self, claim, name="srp_claim", *args):
        return reverse(f"fleetops:{name}", args=[claim.uuid, *args])


class PermissionMatrixTests(SrpViewTestCase):
    """Every FleetOps URL against every permission bundle."""

    def setUp(self):
        super().setUp()
        # The Fleet tab routes read and change the fleet: they reach an in-memory EVE, never ESI.
        FleetSim().install(self)

    def cases(self):
        r = reverse
        member_only = {"member": DENIED, "corp": DENIED, "fc": DENIED, "lead": OK}
        lead_post = {"member": DENIED, "corp": DENIED, "fc": DENIED, "lead": REDIRECT}

        def own_and_other(label, name, own_op, other_op, data=None):
            return [
                Case(f"{label} own", "post", r(f"fleetops:{name}", args=[own_op.uuid]), data,
                     {"member": DENIED, "corp": DENIED, "fc": REDIRECT, "lead": REDIRECT}),
                Case(f"{label} other", "post", r(f"fleetops:{name}", args=[other_op.uuid]), data,
                     {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": REDIRECT}),
            ]

        def start_scheduled_data(user):
            return {"fc_character_id": str(f.main_of(user).character_id), "send_ping": "on"}

        def preview_data(user):
            return {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(f.main_of(user).character_id),
                "fleet_type": str(self.stratop.pk),
                "formup": "Jita 4-4",
                "additional_message": "Bring links",
            }

        cases = [
            Case("dashboard", "get", r("fleetops:dashboard"), None, dict.fromkeys(ROLES, OK)),
            Case("start_fleet", "get", r("fleetops:start_fleet"), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("manual_fleet", "get", r("fleetops:manual_fleet"), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            # The FC has a fleet token and gets the token picker; the lead has none and goes to SSO.
            Case("authorize_esi", "get", r("fleetops:authorize_esi"), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": REDIRECT}),
            Case("detect_fleet", "get",
                 lambda user: r("fleetops:detect_fleet", args=[f.main_of(user).character_id]), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("fleet_operations", "get", r("fleetops:fleet_operations"), None, dict.fromkeys(ROLES, OK)),
            Case("operation_detail own_active", "get", self.detail_url(self.own_active), None,
                 {"member": OK, "corp": HIDDEN, "fc": OK, "lead": OK}),
            Case("operation_detail other_closed", "get", self.detail_url(self.other_closed), None,
                 {"member": HIDDEN, "corp": HIDDEN, "fc": OK, "lead": OK}),
            # Pre pings may go to restricted channels: FCs and managers only.
            Case("operation_detail own_scheduled", "get", self.detail_url(self.own_scheduled), None,
                 {"member": HIDDEN, "corp": HIDDEN, "fc": OK, "lead": OK}),
            Case("operation_detail other_scheduled", "get", self.detail_url(self.other_scheduled), None,
                 {"member": HIDDEN, "corp": HIDDEN, "fc": OK, "lead": OK}),
            Case("edit_operation own", "get", r("fleetops:edit_operation", args=[self.own_closed.uuid]), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("edit_operation other", "get", r("fleetops:edit_operation", args=[self.other_closed.uuid]), None,
                 {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": OK}),
            Case("edit_operation credited", "get",
                 r("fleetops:edit_operation", args=[self.credited_closed.uuid]), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("my_statistics", "get", r("fleetops:my_statistics"), None, {**dict.fromkeys(ROLES, OK), STATS: OK}),
            Case("corporation_statistics", "get", r("fleetops:corporation_statistics"), None,
                 {"member": DENIED, "corp": OK, "fc": DENIED, "lead": OK, STATS: DENIED}),
            Case("corporation_statistics_detail", "get",
                 r("fleetops:corporation_statistics_detail", args=[f.DEFAULT_CORP[0]]), None,
                 {**member_only, STATS: OK}),
            Case("all_corporation_statistics", "get", r("fleetops:all_corporation_statistics"), None,
                 {**member_only, STATS: OK}),
            Case("all_fc_statistics", "get", r("fleetops:all_fc_statistics"), None, {**member_only, STATS: OK}),
            Case("fc_statistics_detail", "get", r("fleetops:fc_statistics_detail", args=[self.fc.pk]), None,
                 {"member": HIDDEN, "corp": HIDDEN, "fc": OK, "lead": OK, STATS: OK}),
            Case("manual_attendance", "get", r("fleetops:manual_attendance"), None,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("attendance_history_me", "get", r("fleetops:attendance_history_me"), None,
                 {**dict.fromkeys(ROLES, OK), STATS: OK}),
            Case("attendance_history_corporation", "get", r("fleetops:attendance_history_corporation"), None,
                 {"member": DENIED, "corp": OK, "fc": DENIED, "lead": OK, STATS: DENIED}),
            Case("attendance_history_alliance", "get", r("fleetops:attendance_history_alliance"), None,
                 {**member_only, STATS: OK}),
            Case("audit_log", "get", r("fleetops:audit_log"), None, member_only),
            Case("incentive_review", "get", r("fleetops:incentive_review"), None, member_only),
            Case("configuration_index", "get", r("fleetops:configuration_index"), None, member_only),
            Case("configuration_settings", "get", r("fleetops:configuration_settings"), None, member_only),
            Case("sync_discord_roles", "post", r("fleetops:sync_discord_roles"), None, lead_post),
            # The translations of the page scripts need a login only (see test_js_catalog).
            Case("jsi18n", "get", r("fleetops:jsi18n"), None, dict.fromkeys(ROLES, OK)),
            # Public page; anonymous access depends on APPS_WITH_PUBLIC_VIEWS (see test_public_urls).
            Case("comms_join", "get", r("fleetops:comms_join", args=[self.comms.join_token]), None,
                 dict.fromkeys(ROLES, OK)),
        ]
        for section in CONFIG_SECTIONS:
            obj = self.config_objects[section]
            cases += [
                Case(f"configuration_list {section}", "get",
                     r("fleetops:configuration_list", args=[section]), None, member_only),
                Case(f"configuration_add {section}", "get",
                     r("fleetops:configuration_add", args=[section]), None, member_only),
                Case(f"configuration_edit {section}", "get",
                     r("fleetops:configuration_edit", args=[section, obj.pk]), None, member_only),
                Case(f"configuration_delete {section}", "post",
                     r("fleetops:configuration_delete", args=[section, obj.pk]), None, lead_post),
            ]

        cases += [
            Case("preview_fleet", "post", r("fleetops:preview_fleet"), preview_data,
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            *own_and_other("end_fleet", "end_fleet", self.own_active, self.other_active,
                           {"attendance_multiplier": "2"}),
            *own_and_other("set_attendance_multiplier", "set_attendance_multiplier", self.own_closed,
                           self.other_closed, {"attendance_multiplier": "2"}),
            *own_and_other("retry_ping", "retry_ping", self.own_active, self.other_active),
            *own_and_other("retry_motd", "retry_motd", self.own_active, self.other_active),
            *own_and_other("retry_srp", "retry_srp", self.own_active, self.other_active),
            *own_and_other("regenerate_motd", "regenerate_motd", self.own_active, self.other_active),
            # Only the FC who scheduled a fleet starts it; managers edit and cancel it.
            Case("start_scheduled_fleet own", "post",
                 r("fleetops:start_scheduled_fleet", args=[self.own_scheduled.uuid]), start_scheduled_data,
                 {"member": DENIED, "corp": DENIED, "fc": REDIRECT, "lead": HIDDEN}),
            Case("start_scheduled_fleet other", "post",
                 r("fleetops:start_scheduled_fleet", args=[self.other_scheduled.uuid]), start_scheduled_data,
                 {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": HIDDEN}),
            Case("edit_scheduled_fleet own", "get", r("fleetops:edit_scheduled_fleet", args=[self.own_scheduled.uuid]),
                 None, {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("edit_scheduled_fleet other", "get",
                 r("fleetops:edit_scheduled_fleet", args=[self.other_scheduled.uuid]),
                 None, {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": OK}),
            *own_and_other("cancel_scheduled_fleet", "cancel_scheduled_fleet", self.own_scheduled,
                           self.other_scheduled, {"notify": "on"}),
            *own_and_other("add_operation_role", "add_operation_role", self.own_closed, self.other_closed,
                           {"role": "backseat_fc", "character_id": str(self.outsider_main.character_id),
                            "notes": "Ran logi"}),
            *own_and_other("kick_capsules", "kick_capsules", self.own_active, self.other_active),
            *own_and_other("add_manual_attendance", "add_manual_attendance", self.own_closed,
                           self.other_closed,
                           {"character_id": str(self.member_main.character_id), "character_name": "",
                            "attendance_value": "1", "duplicate_action": "keep", "notes": "Late join"}),
            Case("delete_operation_role own", "post",
                 r("fleetops:delete_operation_role", args=[self.own_role.pk]), None,
                 {"member": DENIED, "corp": DENIED, "fc": REDIRECT, "lead": REDIRECT}),
            Case("delete_operation_role other", "post",
                 r("fleetops:delete_operation_role", args=[self.other_role.pk]), None,
                 {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": REDIRECT}),
            # An approved correction is deleted by FC leads only.
            Case("delete_manual_attendance own", "post",
                 r("fleetops:delete_manual_attendance", args=[self.own_manual.pk]), None,
                 {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": REDIRECT}),
            Case("delete_manual_attendance other", "post",
                 r("fleetops:delete_manual_attendance", args=[self.other_manual.pk]), None,
                 {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": REDIRECT}),
            Case("incentive_recalculate", "post", r("fleetops:incentive_recalculate", args=[self.period.pk]),
                 None, lead_post),
            Case("incentive_finalize", "post", r("fleetops:incentive_finalize", args=[self.period.pk]),
                 None, lead_post),
            Case("incentive_unlock", "post", r("fleetops:incentive_unlock", args=[self.period.pk]),
                 None, lead_post),
            Case("incentive_waiver", "post",
                 r("fleetops:incentive_waiver", args=[self.period.pk, self.fc.pk]), {"waived": "1"}, lead_post),
            Case("incentive_update", "post", r("fleetops:incentive_update", args=[self.period.pk]),
                 {"budget": "1000000000", "minimum_fleets": "1"}, lead_post),
        ]
        cases += self.pap_rule_cases(own_and_other, member_only, lead_post)
        cases += self.fleet_tab_cases(lead_post)
        cases += self.statistics_cases()
        cases += self.srp_cases()
        return cases

    def srp_cases(self):
        """FleetOps SRP: claims, the fleet SRP link, review, payments and the summaries.

        A claim of the matrix is one of the member's (in a fleet the SRP FC led, and outside a fleet), the outsider's
        in another corporation, or the SRP manager's own. Routes that accept GET and POST have their POST rows in
        SrpRouteTests, because the matrix also checks that every POST row refuses GET.
        """
        r = reverse
        managers = by_srp_role(SRP_MANAGERS)
        manager_redirects = by_srp_role(SRP_MANAGERS, granted=REDIRECT)
        requesters = by_srp_role(SRP_REQUESTERS)
        # SRP managers and view_all_stats open every claim. Every role has FleetOps access, so a claim a role may
        # not open is hidden, not denied.
        everybody_reading = SRP_MANAGERS | {"srp_stats"}

        def claim_page(label, claim, *also):
            return Case(label, "get", self.claim_url(claim), None, by_srp_role(everybody_reading | set(also), otherwise=HIDDEN))

        def claimant_only(label, name, claim, claimant):
            expected = {
                role: (REDIRECT if role == claimant else HIDDEN if role in SRP_REQUESTERS else DENIED)
                for role in SRP_ROLES
            }
            return Case(label, "post", self.claim_url(claim, name), None, expected)

        def decision(action, data=None):
            return Case(
                f"srp_claim_action {action}", "post", self.claim_url(self.claim_fleet, "srp_claim_action", action),
                data, manager_redirects,
            )

        # Deciding on one's own claim needs allow_self_review; a note or a re-check does not.
        own_claim = {**manager_redirects, "srp_manager": DENIED}
        no_claim = self.claim_url(self.claim_fleet, "srp_claim_action", "approve").replace(
            str(self.claim_fleet.uuid), str(uuid.uuid4())
        )
        everyone_with_access = dict.fromkeys(SRP_ROLES, HIDDEN)
        return [
            Case("srp_home", "get", r("fleetops:srp_home"), None, requesters),
            Case("srp_home month", "get", r("fleetops:srp_home"), {"year": "2026", "month": "3"}, requesters),
            Case("srp_home invalid month", "get", r("fleetops:srp_home"), {"year": "99999", "month": "13"}, requesters),
            Case("srp_check", "post", r("fleetops:srp_check"), {"killmail": "not a killmail"}, requesters),
            Case("srp_check empty", "post", r("fleetops:srp_check"), {"killmail": ""}, requesters),
            Case("srp_link", "get", r("fleetops:srp_link", args=[self.srp_link.code]), None, requesters),
            Case("srp_link closed", "get", r("fleetops:srp_link", args=[self.srp_closed_link.code]), None, requesters),
            Case("srp_link unknown", "get", r("fleetops:srp_link", args=["nosuchcode"]), None,
                 by_srp_role(SRP_REQUESTERS, granted=HIDDEN)),
            Case("srp_submit", "post", r("fleetops:srp_submit"), {"token": "invalid", "comment": ""},
                 by_srp_role(SRP_REQUESTERS, granted=REDIRECT)),
            # VIS(3): the claimant, SRP managers, view_all_stats, the FC of the fleet and the corporation's managers.
            claim_page("srp_claim fleet loss", self.claim_fleet, "srp_member", "srp_fc", "srp_corp"),
            claim_page("srp_claim loss outside a fleet", self.claim_out, "srp_member", "srp_corp"),
            claim_page("srp_claim other corporation", self.claim_other_corp, "srp_corp_other"),
            claim_page("srp_claim manager's own", self.claim_manager, "srp_corp"),
            Case("srp_claim unknown", "get", r("fleetops:srp_claim", args=[uuid.uuid4()]), None, everyone_with_access),
            claimant_only("srp_claim_withdraw", "srp_claim_withdraw", self.claim_fleet, "srp_member"),
            claimant_only("srp_claim_withdraw outside a fleet", "srp_claim_withdraw", self.claim_out, "srp_member"),
            claimant_only("srp_claim_resubmit", "srp_claim_resubmit", self.claim_fleet, "srp_member"),
            claimant_only("srp_claim_resubmit manager", "srp_claim_resubmit", self.claim_manager, "srp_manager"),
            Case("srp_review", "get", r("fleetops:srp_review"), None, managers),
            Case("srp_review filters", "get", r("fleetops:srp_review"),
                 {"status": "paid", "scope": "zzz", "flag": "over_limit", "corporation": "x", "fc": "-1", "q": "pilot",
                  "year": "99999", "month": "13", "page": "abc"}, managers),
            Case("srp_review_bulk_approve", "post", r("fleetops:srp_review_bulk_approve"),
                 {"claim": [str(self.claim_fleet.uuid)]}, manager_redirects),
            Case("srp_review_recalculate", "post", r("fleetops:srp_review_recalculate"), None, manager_redirects),
            decision("approve", {"mode": "suggested"}),
            decision("reject", {"reason": "Not in the fleet"}),
            decision("revoke", {"reason": "Wrong amount"}),
            decision("paid", {"note": "Sent in game"}),
            decision("unpaid", {"reason": "Not received"}),
            decision("reopen", {"reason": "Appeal"}),
            decision("reclassify", {"scope": "out_of_fleet", "reason": "Lost on the way home"}),
            decision("recalculate"),
            decision("recheck"),
            decision("note", {"text": "Asked the pilot"}),
            Case("srp_claim_action approve own claim", "post",
                 self.claim_url(self.claim_manager, "srp_claim_action", "approve"), {"mode": "suggested"}, own_claim),
            Case("srp_claim_action reject own claim", "post",
                 self.claim_url(self.claim_manager, "srp_claim_action", "reject"), {"reason": "Mistake"}, own_claim),
            Case("srp_claim_action paid own claim", "post",
                 self.claim_url(self.claim_manager, "srp_claim_action", "paid"), None, own_claim),
            Case("srp_claim_action note on own claim", "post",
                 self.claim_url(self.claim_manager, "srp_claim_action", "note"), {"text": "Mine"}, manager_redirects),
            Case("srp_claim_action recheck own claim", "post",
                 self.claim_url(self.claim_manager, "srp_claim_action", "recheck"), None, manager_redirects),
            Case("srp_claim_action unknown action", "post", self.claim_url(self.claim_fleet, "srp_claim_action", "explode"),
                 None, by_srp_role(SRP_MANAGERS, granted=HIDDEN)),
            Case("srp_claim_action unknown claim", "post", no_claim, {"mode": "suggested"},
                 by_srp_role(SRP_MANAGERS, granted=HIDDEN)),
            Case("srp_payments", "get", r("fleetops:srp_payments"), None, managers),
            Case("srp_payments dates", "get", r("fleetops:srp_payments"),
                 {"paid_from": "2026-01-01", "paid_to": "nonsense"}, managers),
            Case("srp_link_close", "post", r("fleetops:srp_link_close", args=[self.srp_link.code]), None,
                 manager_redirects),
            Case("srp_link_open", "post", r("fleetops:srp_link_open", args=[self.srp_closed_link.code]), None,
                 manager_redirects),
            Case("srp_link_open unknown", "post", r("fleetops:srp_link_open", args=["nosuchcode"]), None,
                 by_srp_role(SRP_MANAGERS, granted=HIDDEN)),
            # Summaries: SRP managers and view_all_stats read all of them, corporation managers their own
            # corporation, FCs the fleets they led.
            Case("srp_summary", "get", r("fleetops:srp_summary"), None, by_srp_role(SRP_MANAGERS | {"srp_stats"})),
            Case("srp_summary month", "get", r("fleetops:srp_summary"), {"year": "2026", "month": "3"},
                 by_srp_role(SRP_MANAGERS | {"srp_stats"})),
            Case("srp_summary invalid month", "get", r("fleetops:srp_summary"), {"year": "x", "month": "13"},
                 by_srp_role(SRP_MANAGERS | {"srp_stats"})),
            Case("srp_summary_own_corporation", "get", r("fleetops:srp_summary_own_corporation"), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats", "srp_corp", "srp_corp_other"})),
            Case("srp_summary_corporation", "get", r("fleetops:srp_summary_corporation", args=[f.DEFAULT_CORP[0]]), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats"})),
            Case("srp_summary_corporation other", "get",
                 r("fleetops:srp_summary_corporation", args=[f.OTHER_CORP[0]]), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats"})),
            Case("srp_summary_corporation unknown", "get", r("fleetops:srp_summary_corporation", args=[2_999_999]), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats"}, granted=HIDDEN)),
            Case("srp_summary_own_fc", "get", r("fleetops:srp_summary_own_fc"), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats", "srp_fc"})),
            Case("srp_summary_fc", "get", r("fleetops:srp_summary_fc", args=[self.srp_users["srp_fc"].pk]), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats"})),
            Case("srp_summary_fc unknown", "get", r("fleetops:srp_summary_fc", args=[99_999_999]), None,
                 by_srp_role(SRP_MANAGERS | {"srp_stats"}, granted=HIDDEN)),
        ]

    def statistics_cases(self):
        """Member lists, leaderboard, member details and exports of the statistics."""
        r = reverse
        unknown_corporation = 2_999_999
        stats_pages = {"member": DENIED, "corp": DENIED, "fc": DENIED, "lead": OK, STATS: OK}
        corporation_pages = {"member": DENIED, "corp": OK, "fc": DENIED, "lead": OK, STATS: DENIED}
        everyone = {**dict.fromkeys(ROLES, OK), STATS: OK}
        nobody = dict.fromkeys((*ROLES, STATS), HIDDEN)
        csv = {"format": "csv"}

        def member_detail(label, target, expected):
            return Case(f"member_detail {label}", "get", r("fleetops:member_detail", args=[target.pk]), None, expected)

        def export(label, dataset, expected, **params):
            return Case(f"export {label}", "get", r("fleetops:export", args=[dataset]), {**csv, **params}, expected)

        return [
            Case("alliance_members", "get", r("fleetops:alliance_members"), None, stats_pages),
            Case("alliance_members inactive", "get", r("fleetops:alliance_members"),
                 {"status": "inactive", "sort": "pap", "q": "main", "corporation": str(f.OTHER_CORP[0])}, stats_pages),
            Case("alliance_members invalid filters", "get", r("fleetops:alliance_members"),
                 {"status": "zzz", "sort": "-;--", "page": "abc", "corporation": "x", "year": "99999", "month": "13"},
                 stats_pages),
            Case("corporation_members", "get", r("fleetops:corporation_members"), None, corporation_pages),
            Case("corporation_members inactive", "get", r("fleetops:corporation_members"),
                 {"status": "inactive", "q": "main"}, corporation_pages),
            Case("corporation_members_detail own", "get",
                 r("fleetops:corporation_members_detail", args=[f.DEFAULT_CORP[0]]), None, stats_pages),
            Case("corporation_members_detail other", "get",
                 r("fleetops:corporation_members_detail", args=[f.OTHER_CORP[0]]), None, stats_pages),
            Case("corporation_members_detail unknown", "get",
                 r("fleetops:corporation_members_detail", args=[unknown_corporation]), None,
                 {"member": DENIED, "corp": DENIED, "fc": DENIED, "lead": HIDDEN, STATS: HIDDEN}),
            # Members see themselves; a corporation manager sees the members of the own corporation.
            Case("member_detail self", "get", lambda user: r("fleetops:member_detail", args=[user.pk]), None,
                 dict.fromkeys((*ROLES, STATS), REDIRECT)),
            member_detail("same corporation", self.member,
                          {"member": REDIRECT, "corp": OK, "fc": HIDDEN, "lead": OK, STATS: OK}),
            member_detail("other corporation", self.outsider,
                          {"member": HIDDEN, "corp": HIDDEN, "fc": HIDDEN, "lead": OK, STATS: OK}),
            member_detail("without a main", self.no_main, nobody),
            Case("member_detail unknown", "get", r("fleetops:member_detail", args=[99_999_999]), None, nobody),
            # Default setting: everybody with FleetOps access sees the leaderboard.
            Case("leaderboard", "get", r("fleetops:leaderboard"), None, everyone),
            Case("leaderboard corporation", "get", r("fleetops:leaderboard"),
                 {"corporation": str(f.OTHER_CORP[0])}, everyone),
            export("corporations", "corporations", stats_pages),
            export("corporations xlsx", "corporations", stats_pages, **{"format": "xlsx"}),
            export("corporations sorted", "corporations", stats_pages, sort="-average", year="2026", month="3"),
            export("members alliance", "members", stats_pages),
            export("members filtered", "members", stats_pages, status="inactive", q="main", sort="pap"),
            export("members own corporation", "members",
                   {"member": DENIED, "corp": OK, "fc": DENIED, "lead": OK, STATS: OK},
                   corporation=str(f.DEFAULT_CORP[0])),
            export("members other corporation", "members",
                   {"member": DENIED, "corp": HIDDEN, "fc": DENIED, "lead": OK, STATS: OK},
                   corporation=str(f.OTHER_CORP[0])),
            export("member-fleets", "member-fleets",
                   {"member": OK, "corp": OK, "fc": HIDDEN, "lead": OK, STATS: OK}, user=str(self.member.pk)),
            export("member-fleets other corporation", "member-fleets",
                   {"member": HIDDEN, "corp": HIDDEN, "fc": HIDDEN, "lead": OK, STATS: OK},
                   user=str(self.outsider.pk)),
            export("member-fleets without a user", "member-fleets", nobody),
            export("fcs", "fcs", stats_pages),
            export("attendance me", "attendance", everyone, scope="me"),
            export("attendance corporation", "attendance", corporation_pages, scope="corporation"),
            export("attendance alliance", "attendance", stats_pages, scope="alliance"),
            export("attendance month", "attendance", stats_pages, scope="alliance", year="2026", month="3"),
            export("incentives", "incentives",
                   {"member": DENIED, "corp": DENIED, "fc": DENIED, "lead": OK, STATS: DENIED},
                   period=str(self.period.pk)),
            export("unknown dataset", "no-such-dataset", nobody),
            export("unsupported format", "corporations", nobody, **{"format": "pdf"}),
            Case("export without format", "get", r("fleetops:export", args=["corporations"]), None, nobody),
        ]

    def fleet_tab_cases(self, lead_post):
        r = reverse
        own_post = {"member": DENIED, "corp": DENIED, "fc": REDIRECT, "lead": REDIRECT}
        other_post = {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": REDIRECT}
        wing_id, squad_id = 2_054_000_000_001, 3_108_000_000_001
        template, _ = FleetStructureTemplate.objects.get_or_create(name="Matrix", defaults={"layout": "Main: Alpha"})
        function, _ = ShipFunction.objects.get_or_create(name="Matrix")
        posts = (
            ("fleet_refresh", {}, None),
            ("fleet_wing_add", {}, {"name": "Logi"}),
            ("fleet_wing_rename", {"wing_id": wing_id}, {"name": "Main"}),
            ("fleet_wing_delete", {"wing_id": wing_id}, None),
            ("fleet_squad_add", {"wing_id": wing_id}, None),
            ("fleet_squad_rename", {"squad_id": squad_id}, {"name": "Alpha"}),
            ("fleet_squad_delete", {"squad_id": squad_id}, None),
            ("fleet_members_move", {}, {"character_ids": [str(self.member_main.character_id)], "role": "squad_member",
                                        "wing_id": str(wing_id), "squad_id": str(squad_id)}),
            ("fleet_invite", {}, {"character_ids": [str(self.outsider_main.character_id)]}),
            ("fleet_apply_template", {}, {"template_id": str(template.pk)}),
        )
        cases = []
        fleets = (("own", self.own_active, own_post), ("other", self.other_active, other_post))
        for name, kwargs, data in posts:
            for label, operation, expected in fleets:
                url = r(f"fleetops:{name}", kwargs={"operation_uuid": operation.uuid, **kwargs})
                cases.append(Case(f"{name} {label}", "post", url, data, expected))
        # VIS(2): the live fleet is for its managers and viewers of all fleets, not for its members.
        return cases + [
            Case("fleet_state own", "get", r("fleetops:fleet_state", args=[self.own_active.uuid]), None,
                 {"member": HIDDEN, "corp": HIDDEN, "fc": OK, "lead": OK}),
            Case("fleet_state other", "get", r("fleetops:fleet_state", args=[self.other_active.uuid]), None,
                 {"member": HIDDEN, "corp": HIDDEN, "fc": OK, "lead": OK}),
            Case("fleet_invite_candidates own", "get",
                 r("fleetops:fleet_invite_candidates", args=[self.own_active.uuid]), {"q": "member"},
                 {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}),
            Case("fleet_invite_candidates other", "get",
                 r("fleetops:fleet_invite_candidates", args=[self.other_active.uuid]), {"q": "member"},
                 {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": OK}),
            Case("fleet_classify", "post", r("fleetops:fleet_classify", args=[self.own_active.uuid]),
                 {"type_id": "11987", "function_id": str(function.pk)}, lead_post),
        ]

    def pap_rule_cases(self, own_and_other, member_only, lead_post):
        r = reverse
        fc_pages = {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}
        own_page = {"member": DENIED, "corp": DENIED, "fc": OK, "lead": OK}
        other_page = {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": OK}
        own_post = {"member": DENIED, "corp": DENIED, "fc": REDIRECT, "lead": REDIRECT}
        other_post = {"member": DENIED, "corp": DENIED, "fc": HIDDEN, "lead": REDIRECT}
        # Presets belong to their owner; the FC lead does not see the FC's presets either.
        owner_post = {"member": DENIED, "corp": DENIED, "fc": REDIRECT, "lead": HIDDEN}

        def kick_member_url(operation):
            return r("fleetops:kick_member", args=[operation.uuid, self.member_main.character_id])

        return [
            *own_and_other("recalculate_pap", "recalculate_pap", self.own_closed, self.other_closed),
            *own_and_other("set_stats_mode", "set_stats_mode", self.own_closed, self.other_closed,
                           {"mode": "exclude", "note": "Test fleet"}),
            Case("live_pap", "get", r("fleetops:live_pap"), None, dict.fromkeys(ROLES, OK)),
            Case("approval_queue", "get", r("fleetops:approval_queue"), None, member_only),
            Case("review_operation", "post", r("fleetops:review_operation", args=[self.own_closed.uuid]),
                 {"action": "approve", "note": ""}, lead_post),
            Case("review_attendance", "post", r("fleetops:review_attendance"),
                 {"ids": [str(self.own_manual.pk)], "action": "approve", "note": ""}, lead_post),
            Case("roster_import own", "get", r("fleetops:roster_import", args=[self.own_closed.uuid]), None,
                 own_page),
            Case("roster_import other", "get", r("fleetops:roster_import", args=[self.other_closed.uuid]), None,
                 other_page),
            # Any pilot with FleetOps access registers through a link of a fleet they cannot open.
            Case("pap_link_register", "get", r("fleetops:pap_link_register", args=[self.other_link.code]), None,
                 dict.fromkeys(ROLES, OK)),
            Case("pap_link_authorize", "get", r("fleetops:pap_link_authorize", args=[self.other_link.code]), None,
                 dict.fromkeys(ROLES, REDIRECT)),
            *own_and_other("create_pap_link", "create_pap_link", self.own_active, self.other_active,
                           {"minutes": "30", "require_online": "on"}),
            Case("revoke_pap_link own", "post", r("fleetops:revoke_pap_link", args=[self.own_link.pk]), None,
                 own_post),
            Case("revoke_pap_link other", "post", r("fleetops:revoke_pap_link", args=[self.other_link.pk]), None,
                 other_post),
            Case("start_link_only_fleet", "get", r("fleetops:start_link_only_fleet"), None, fc_pages),
            Case("kick_member own", "post", kick_member_url(self.own_active), None, own_post),
            Case("kick_member other", "post", kick_member_url(self.other_active), None, other_post),
            Case("kick_preview own", "get", r("fleetops:kick_preview", args=[self.own_active.uuid]), None,
                 own_page),
            Case("kick_preview other", "get", r("fleetops:kick_preview", args=[self.other_active.uuid]), None,
                 other_page),
            *own_and_other("kick_outside_system", "kick_outside_system", self.own_active, self.other_active,
                           {"system_id": "30000142"}),
            Case("preset_list", "get", r("fleetops:preset_list"), None, fc_pages),
            Case("preset_save", "post", r("fleetops:preset_save"), {"name": "Muninn"}, fc_pages),
            Case("preset_rename", "post", r("fleetops:preset_rename", args=[self.fc_preset.pk]),
                 {"name": "Eagles"}, owner_post),
            Case("preset_delete", "post", r("fleetops:preset_delete", args=[self.fc_preset.pk]), None, owner_post),
            Case("preset_default", "post", r("fleetops:preset_default", args=[self.fc_preset.pk]), None, owner_post),
        ]

    def resolve(self, value, user):
        return value(user) if callable(value) else value

    def perform(self, client, case, user):
        """Run one request inside a savepoint so state-changing cases do not leak into the next one."""
        url = self.resolve(case.url, user)
        data = self.resolve(case.data, user) or {}
        savepoint = transaction.savepoint()
        try:
            if case.method == "post":
                return client.post(url, data)
            return client.get(url, data)
        finally:
            transaction.savepoint_rollback(savepoint)

    def assert_expected(self, response, expected, label):
        if expected == HIDDEN:
            self.assertIn(response.status_code, (403, 404), label)
            return
        self.assertEqual(response.status_code, expected, label)
        if expected == REDIRECT:
            self.assertFalse(response["Location"].startswith(self.login_url()), label)

    def run_matrix(self, role):
        user = self.users[role]
        client = self.client_for(user)
        for case in self.cases():
            if role not in case.expected:
                continue
            with self.subTest(role=role, case=case.label):
                response = self.perform(client, case, user)
                self.assert_expected(response, case.expected[role], f"{role}: {case.label}")

    def test_every_url_is_covered_by_the_matrix(self):
        from fleetops import urls

        names = {pattern.name for pattern in urls.urlpatterns}
        covered = {resolve(self.resolve(case.url, self.fc)).url_name for case in self.cases()}
        self.assertEqual(names - covered, set())

    def test_anonymous_users_are_redirected_to_login(self):
        client = self.client_for(None)
        for case in self.cases():
            with self.subTest(case=case.label):
                response = self.perform(client, case, self.fc)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(self.login_url() + "?next="))

    def test_authenticated_user_without_basic_access_gets_403_everywhere(self):
        client = self.client_for(self.no_perms)
        for case in self.cases():
            with self.subTest(case=case.label):
                response = self.perform(client, case, self.no_perms)
                # The comms join page is the one public FleetOps page; the script catalog only needs a login.
                self.assertEqual(response.status_code, OK if case.label in {"comms_join", "jsi18n"} else DENIED)

    def test_member_matrix(self):
        self.run_matrix("member")

    def test_corp_management_matrix(self):
        self.run_matrix("corp")

    def test_fc_matrix(self):
        self.run_matrix("fc")

    def test_fc_lead_matrix(self):
        self.run_matrix("lead")

    def test_statistics_manager_matrix(self):
        self.run_matrix(STATS)

    def test_srp_matrices(self):
        for role in SRP_ROLES:
            self.run_matrix(role)

    def test_post_only_endpoints_reject_get(self):
        client = self.client_for(self.lead)
        for case in self.cases():
            if case.method != "post":
                continue
            with self.subTest(case=case.label):
                response = client.get(self.resolve(case.url, self.lead))
                self.assertEqual(response.status_code, NOT_ALLOWED)

    def test_rejected_get_on_post_only_endpoint_changes_nothing(self):
        client = self.client_for(self.lead)
        client.get(reverse("fleetops:end_fleet", args=[self.other_active.uuid]))
        client.get(reverse("fleetops:kick_capsules", args=[self.other_active.uuid]))
        self.other_active.refresh_from_db()
        self.assertEqual(self.other_active.status, ACTIVE)
        self.kick.assert_not_called()


class SrpRouteTests(SrpViewTestCase):
    """SRP routes beyond the matrix: the GET-and-POST routes, the self-review setting, notes and the menu."""

    def as_role(self, role, method, url, data=None):
        client = self.client_for(self.srp_users[role])
        return client.post(url, data or {}) if method == "post" else client.get(url, data or {})

    def test_member_pages_say_when_srp_is_switched_off(self):
        SrpSettings.objects.filter(pk=1).update(enabled=False)

        for url in (self.srp_url("srp_home"), self.srp_url("srp_link", self.srp_link.code)):
            with self.subTest(url=url):
                response = self.as_role("srp_member", "get", url)
                self.assertEqual(response.status_code, OK)
                self.assertContains(response, "FleetOps SRP is not enabled.")
        # Managers still review and pay what was claimed before.
        for name in ("srp_review", "srp_payments"):
            self.assertEqual(self.as_role("srp_manager", "get", self.srp_url(name)).status_code, OK)
        self.assertEqual(self.as_role("srp_member", "get", self.claim_url(self.claim_fleet)).status_code, OK)

    def test_open_link_page_has_the_claim_form(self):
        response = self.as_role("srp_member", "get", self.srp_url("srp_link", self.srp_link.code))

        self.assertEqual(response.status_code, OK)
        self.assertContains(response, 'name="killmail"')
        self.assertContains(response, f'action="{self.srp_url("srp_link", self.srp_link.code)}"')

    def test_closed_link_page_has_no_claim_form(self):
        url = self.srp_url("srp_link", self.srp_closed_link.code)

        response = self.as_role("srp_member", "get", url)

        self.assertEqual(response.status_code, OK)
        self.assertContains(response, "closed")
        self.assertNotContains(response, 'name="killmail"')
        self.assertEqual(self.as_role("srp_member", "post", url, {"killmail": "not a killmail"}).status_code, OK)

    def test_fleet_link_page_accepts_post(self):
        url = self.srp_url("srp_link", self.srp_link.code)

        self.assertEqual(self.as_role("srp_member", "post", url, {"killmail": "not a killmail"}).status_code, OK)
        self.assertEqual(self.as_role("srp_none", "post", url, {"killmail": "not a killmail"}).status_code, DENIED)

    def test_payments_accept_post_from_managers_only(self):
        url = self.srp_url("srp_payments")

        self.assertEqual(self.as_role("srp_manager", "post", url).status_code, REDIRECT)
        self.assertEqual(self.as_role("srp_member", "post", url).status_code, DENIED)
        self.assertEqual(self.as_role("srp_stats", "post", url).status_code, DENIED)

    def test_srp_routes_refuse_unsupported_methods(self):
        self.assertEqual(self.as_role("srp_manager", "post", self.srp_url("srp_review")).status_code, NOT_ALLOWED)
        self.assertEqual(self.as_role("srp_member", "post", self.srp_url("srp_home")).status_code, NOT_ALLOWED)
        self.assertEqual(self.as_role("srp_manager", "get", self.srp_url("srp_check")).status_code, NOT_ALLOWED)

    def test_internal_notes_are_for_managers_only(self):
        url = self.claim_url(self.claim_fleet)

        for role, sees_note in (
            ("srp_member", False),
            ("srp_fc", False),
            ("srp_corp", False),
            ("srp_stats", False),
            ("srp_manager", True),
            ("srp_manager2", True),
        ):
            with self.subTest(role=role):
                response = self.as_role(role, "get", url)
                self.assertContains(response, "Submitted by the pilot")
                if sees_note:
                    self.assertContains(response, "Looks like an alt of a spy")
                else:
                    self.assertNotContains(response, "Looks like an alt of a spy")

    def test_claim_page_offers_decisions_by_the_self_review_rule(self):
        expected = {
            ("srp_manager2", "claim_fleet"): True,
            ("srp_manager", "claim_fleet"): True,
            ("srp_manager", "claim_manager"): False,
            ("srp_manager2", "claim_manager"): True,
            ("srp_stats", "claim_fleet"): False,
            ("srp_member", "claim_fleet"): False,
            ("srp_fc", "claim_fleet"): False,
        }
        for (role, name), can_decide in expected.items():
            with self.subTest(role=role, claim=name):
                response = self.as_role(role, "get", self.claim_url(getattr(self, name)))
                self.assertEqual(response.context["can_decide"], can_decide)

    def test_self_review_setting_lets_managers_decide_on_their_own_claims(self):
        approve = self.claim_url(self.claim_manager, "srp_claim_action", "approve")
        self.assertEqual(self.as_role("srp_manager", "post", approve, {"mode": "suggested"}).status_code, DENIED)

        SrpSettings.objects.filter(pk=1).update(allow_self_review=True)

        self.assertEqual(self.as_role("srp_manager", "post", approve, {"mode": "suggested"}).status_code, REDIRECT)
        response = self.as_role("srp_manager", "get", self.claim_url(self.claim_manager))
        self.assertTrue(response.context["can_decide"])

    def test_only_the_claimant_sees_withdraw_and_resubmit(self):
        claim = SrpClaim.objects.get(pk=self.claim_fleet.pk)
        for role, can_withdraw in (("srp_member", True), ("srp_manager", False), ("srp_stats", False)):
            with self.subTest(role=role):
                response = self.as_role(role, "get", self.claim_url(claim))
                self.assertEqual(response.context["can_withdraw"], can_withdraw)
                self.assertFalse(response.context["can_resubmit"])
        SrpClaim.objects.filter(pk=claim.pk).update(status="withdrawn")
        response = self.as_role("srp_member", "get", self.claim_url(claim))
        self.assertTrue(response.context["can_resubmit"])
        self.assertFalse(response.context["can_withdraw"])

    def test_menu_follows_the_permissions(self):
        links = {name: self.srp_url(name) for name in ("srp_home", "srp_review", "srp_payments", "srp_summary")}
        links["corporation"] = self.srp_url("srp_summary_own_corporation")
        links["fleets"] = self.srp_url("srp_summary_own_fc")
        expected = {
            "srp_none": set(),
            "srp_member": {"srp_home"},
            "srp_corp": {"srp_home", "corporation"},
            "srp_fc": {"srp_home", "fleets"},
            "srp_stats": {"srp_summary"},
            "srp_manager": {"srp_home", "srp_review", "srp_payments", "srp_summary"},
        }
        for role, shown in expected.items():
            with self.subTest(role=role):
                html = self.as_role(role, "get", reverse("fleetops:dashboard")).content.decode()
                for name, url in links.items():
                    self.assertEqual(f'href="{url}"' in html, name in shown, name)

    def test_menu_offers_my_fleets_to_every_fc_permission_holder_who_led_a_fleet(self):
        for name, perm in (("fleetsmanage", "fleetops.manage_fleets"), ("fleetsmanual", "fleetops.create_manual_fleet")):
            with self.subTest(permission=perm):
                lead = f.create_user(name, perms=["fleetops.basic_access", perm])
                f.fleet_link(f.create_operation(lead, status=CLOSED, type_obj=self.stratop))

                html = self.client_for(lead).get(reverse("fleetops:dashboard")).content.decode()

                self.assertIn(f'href="{self.srp_url("srp_summary_own_fc")}"', html)
                self.assertNotIn(f'href="{self.srp_url("srp_home")}"', html)

    def test_menu_counts_the_claims_a_manager_can_decide(self):
        for role, count in (("srp_manager", 3), ("srp_manager2", 4)):
            with self.subTest(role=role):
                html = self.as_role(role, "get", reverse("fleetops:dashboard")).content.decode()
                self.assertIn(f'<span class="badge bg-danger">{count}</span>', html)
        html = self.as_role("srp_member", "get", reverse("fleetops:dashboard")).content.decode()
        self.assertNotIn("badge bg-danger", html)

    def test_menu_hides_my_srp_while_srp_is_off(self):
        SrpSettings.objects.filter(pk=1).update(enabled=False)

        member = self.as_role("srp_member", "get", reverse("fleetops:dashboard")).content.decode()
        manager = self.as_role("srp_manager", "get", reverse("fleetops:dashboard")).content.decode()

        self.assertNotIn(f'href="{self.srp_url("srp_home")}"', member)
        self.assertIn(f'href="{self.srp_url("srp_review")}"', manager)
        self.assertNotIn(f'href="{self.srp_url("srp_home")}"', manager)


class StatisticsRouteTests(FleetOpsViewTestCase):
    """Access rules of the member, leaderboard and export routes beyond the matrix."""

    def get(self, user, name, *args, **params):
        return self.client_for(user).get(reverse(f"fleetops:{name}", args=args), params)

    def call(self, view, user, *args):
        """Run a view for ``user`` without the Alliance Auth URL checks (users without a main never reach it)."""
        request = RequestFactory().get("/fleetops/statistics/")
        request.user = user
        return view(request, *args)

    def test_corporation_manager_without_a_main_has_no_own_corporation(self):
        nomad = f.create_user("nomad", perms=f.CORP_MANAGEMENT_PERMS, with_main=False)

        with self.assertRaises(Http404):
            self.call(views_members.corporation_members_view, nomad)
        with self.assertRaises(Http404):
            self.call(views_statistics.corporation_statistics_view, nomad)

    def test_users_without_a_main_are_sent_to_the_dashboard(self):
        nomad = f.create_user("nomad", perms=f.CORP_MANAGEMENT_PERMS, with_main=False)

        response = self.get(nomad, "alliance_members")

        self.assertEqual((response.status_code, response["Location"]), (302, "/dashboard/"))

    def test_member_detail_of_oneself_goes_to_my_statistics_with_the_month(self):
        response = self.get(self.member, "member_detail", self.member.pk, year="2025", month="7")

        self.assertRedirects(
            response, reverse("fleetops:my_statistics") + "?year=2025&month=7", fetch_redirect_response=False
        )

    def test_member_detail_of_oneself_uses_the_current_month_by_default(self):
        response = self.get(self.member, "member_detail", self.member.pk, year="oops")

        year, month = timezone.now().year, timezone.now().month
        self.assertRedirects(
            response, reverse("fleetops:my_statistics") + f"?year={year}&month={month}", fetch_redirect_response=False
        )

    def test_member_detail_is_for_members_of_the_population_only(self):
        disabled = f.member("disabled", perms=f.MEMBER_PERMS, active=False)
        biomassed = f.member("biomassed", perms=f.MEMBER_PERMS, corporation=f.DOOMHEIM_CORP)

        for user in (disabled, biomassed, self.no_main):
            with self.subTest(user=user.username):
                self.assertEqual(self.get(self.lead, "member_detail", user.pk).status_code, 404)
                self.assertEqual(self.get(self.stats_manager, "member_detail", user.pk).status_code, 404)

    def test_departed_members_have_no_page(self):
        f.settings(history_alliance_ids=str(f.DEFAULT_ALLIANCE[0]))
        departed = f.member("departed", perms=f.MEMBER_PERMS, alliance=f.FOREIGN_ALLIANCE)

        self.assertEqual(self.get(self.lead, "member_detail", departed.pk).status_code, 404)
        self.assertEqual(self.get(self.lead, "member_detail", self.member.pk).status_code, 200)

    def test_corporation_manager_does_not_open_another_corporations_members(self):
        self.assertEqual(self.get(self.corp_manager, "member_detail", self.outsider.pk).status_code, 404)
        self.assertEqual(self.get(self.corp_manager, "member_detail", self.member.pk).status_code, 200)
        self.assertEqual(
            self.get(self.corp_manager, "corporation_members_detail", f.OTHER_CORP[0]).status_code, 403
        )

    def test_leaderboard_visibility_setting(self):
        expected = {
            "members": {self.member: 200, self.fc: 200, self.corp_manager: 200, self.stats_manager: 200, self.lead: 200},
            "managers": {self.member: 403, self.fc: 403, self.corp_manager: 200, self.stats_manager: 200, self.lead: 200},
        }
        for visibility, statuses in expected.items():
            f.settings(leaderboard_visibility=visibility)
            for user, status in statuses.items():
                with self.subTest(visibility=visibility, user=user.username):
                    self.assertEqual(self.get(user, "leaderboard").status_code, status)

    def test_corporation_manager_without_a_corporation_has_no_leaderboard_when_it_is_for_managers_only(self):
        f.settings(leaderboard_visibility="managers")
        nomad = f.create_user("nomad", perms=f.CORP_MANAGEMENT_PERMS, with_main=False)

        with self.assertRaises(PermissionDenied):
            self.call(views_members.leaderboard_view, nomad)

    def test_exports_are_get_only(self):
        client = self.client_for(self.lead)
        url = reverse("fleetops:export", args=["corporations"])

        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(client, method)(url, {"format": "csv"}).status_code, 405)

    def test_navigation_flag_matches_the_leaderboard_access(self):
        for visibility, member_sees in (("members", True), ("managers", False)):
            f.settings(leaderboard_visibility=visibility)
            with self.subTest(visibility=visibility):
                response = self.get(self.member, "fleet_operations")
                self.assertEqual(response.context["leaderboard_visible"], member_sees)
                self.assertEqual(self.get(self.corp_manager, "fleet_operations").context["leaderboard_visible"], True)


class ObjectIsolationTests(FleetOpsViewTestCase):
    def test_member_cannot_open_unrelated_fleet(self):
        client = self.client_for(self.member)
        self.assertEqual(client.get(self.detail_url(self.own_closed)).status_code, 404)
        self.assertEqual(client.get(self.detail_url(self.other_active)).status_code, 404)

    def test_member_can_open_fleet_with_granted_attendance(self):
        operation = f.create_operation(self.other_fc, status=CLOSED)
        f.add_attendance(operation, self.member)
        response = self.client_for(self.member).get(self.detail_url(operation))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])
        self.assertFalse(response.context["can_manage_attendance"])

    def test_member_with_only_capped_attendance_cannot_open_fleet(self):
        operation = f.create_operation(self.other_fc, status=CLOSED)
        f.add_attendance(operation, self.member, granted=False, capped=True)
        self.assertEqual(self.client_for(self.member).get(self.detail_url(operation)).status_code, 404)

    def test_member_with_special_role_can_open_fleet(self):
        operation = f.create_operation(self.other_fc, status=CLOSED)
        assign_role(operation, self.member_main, self.member, role=OperationRoleAssignment.Role.SNOWFLAKE)
        self.assertEqual(self.client_for(self.member).get(self.detail_url(operation)).status_code, 200)

    def test_member_archive_lists_only_involved_fleets(self):
        attended = f.create_operation(self.other_fc, status=CLOSED)
        f.add_attendance(attended, self.member)
        capped = f.create_operation(self.other_fc, status=CLOSED)
        f.add_attendance(capped, self.member, granted=False, capped=True)
        client = self.client_for(self.member)
        for operation, visible in (
            (self.own_active, True),
            (attended, True),
            (capped, False),
            (self.own_closed, False),
            (self.other_closed, False),
        ):
            with self.subTest(operation=str(operation)):
                response = client.get(
                    reverse("fleetops:fleet_operations"), {"year": operation.started_at.year}
                )
                listed = {op.uuid for op in response.context["page"].object_list}
                self.assertEqual(operation.uuid in listed, visible)

    def test_fc_reads_other_fleet_without_management_controls(self):
        response = self.client_for(self.fc).get(self.detail_url(self.other_active))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])
        self.assertFalse(response.context["can_manage_attendance"])
        content = response.content.decode()
        for name in ("end_fleet", "edit_operation", "kick_capsules", "add_operation_role", "add_manual_attendance"):
            self.assertNotIn(reverse(f"fleetops:{name}", args=[self.other_active.uuid]), content, name)

    def test_fc_cannot_change_another_fcs_fleet(self):
        before = {
            "audit": AuditLog.objects.count(),
            "attendance": AttendanceRecord.objects.count(),
            "roles": OperationRoleAssignment.objects.count(),
            "actions": OperationAction.objects.count(),
        }
        client = self.client_for(self.fc)
        requests_to_make = [
            ("end_fleet", self.other_active, {"attendance_multiplier": "3"}),
            ("retry_ping", self.other_active, {}),
            ("retry_motd", self.other_active, {}),
            ("retry_srp", self.other_active, {}),
            ("kick_capsules", self.other_active, {}),
            ("add_operation_role", self.other_active,
             {"role": "backseat_fc", "character_id": str(self.outsider_main.character_id)}),
            ("add_manual_attendance", self.other_active,
             {"character_id": str(self.fc_main.character_id), "attendance_value": "3", "duplicate_action": "keep"}),
            ("set_attendance_multiplier", self.other_active, {"attendance_multiplier": "3"}),
            ("edit_operation", self.other_active, {"fleet_type": str(self.home_defense.pk), "formup": "Amarr"}),
        ]
        for name, operation, data in requests_to_make:
            with self.subTest(name=name):
                response = client.post(reverse(f"fleetops:{name}", args=[operation.uuid]), data)
                self.assertIn(response.status_code, (403, 404))
        for name, pk in (
            ("delete_operation_role", self.other_role.pk),
            ("delete_manual_attendance", self.other_manual.pk),
        ):
            with self.subTest(name=name):
                self.assertIn(client.post(reverse(f"fleetops:{name}", args=[pk])).status_code, (403, 404))

        self.other_active.refresh_from_db()
        self.assertEqual(self.other_active.status, ACTIVE)
        self.assertTrue(self.other_active.tracking_enabled)
        self.assertEqual(self.other_active.attendance_multiplier, 1)
        self.assertEqual(self.other_active.fleet_type, self.stratop)
        self.assertEqual(self.other_active.formup, "Jita")
        after = {
            "audit": AuditLog.objects.count(),
            "attendance": AttendanceRecord.objects.count(),
            "roles": OperationRoleAssignment.objects.count(),
            "actions": OperationAction.objects.count(),
        }
        self.assertEqual(after, before)
        self.discord_post.assert_not_called()
        self.set_motd.assert_not_called()
        self.create_srp.assert_not_called()
        self.kick.assert_not_called()
        self.sync.assert_not_called()

    def test_fc_manages_own_fleet(self):
        # Long enough for 2x.
        FleetOperation.objects.filter(pk=self.own_active.pk).update(started_at=timezone.now() - timedelta(minutes=100))
        client = self.client_for(self.fc)
        response = client.post(
            reverse("fleetops:end_fleet", args=[self.own_active.uuid]), {"attendance_multiplier": "2"}
        )
        self.assertRedirects(response, self.detail_url(self.own_active), fetch_redirect_response=False)
        self.own_active.refresh_from_db()
        self.assertEqual(self.own_active.status, CLOSED)
        self.assertFalse(self.own_active.tracking_enabled)
        self.assertIsNotNone(self.own_active.ended_at)
        self.assertEqual(self.own_active.attendance_multiplier, 2)
        self.assertTrue(AuditLog.objects.filter(action="fleet.end", actor=self.fc).exists())

    def test_fc_credit_role_allows_managing_another_fcs_fleet(self):
        client = self.client_for(self.fc)
        self.assertEqual(
            client.get(reverse("fleetops:edit_operation", args=[self.credited_closed.uuid])).status_code, 200
        )
        response = client.post(
            reverse("fleetops:add_manual_attendance", args=[self.credited_closed.uuid]),
            {"character_id": str(self.member_main.character_id), "attendance_value": "1",
             "duplicate_action": "keep", "notes": "Scout"},
        )
        self.assertEqual(response.status_code, 302)
        record = AttendanceRecord.objects.get(operation=self.credited_closed, source=MANUAL)
        self.assertEqual(record.auth_user, self.member)
        self.assertEqual(record.created_by, self.fc)
        detail = client.get(self.detail_url(self.credited_closed))
        self.assertTrue(detail.context["can_manage"])
        self.assertTrue(detail.context["can_manage_attendance"])

    def test_special_role_without_fc_credit_does_not_allow_management(self):
        assign_role(self.other_closed, self.fc_main, self.fc, role=OperationRoleAssignment.Role.LOGI_ANCHOR)
        client = self.client_for(self.fc)
        self.assertEqual(
            client.get(reverse("fleetops:edit_operation", args=[self.other_closed.uuid])).status_code, 404
        )
        response = client.post(
            reverse("fleetops:add_manual_attendance", args=[self.other_closed.uuid]),
            {"character_id": str(self.fc_main.character_id), "attendance_value": "1", "duplicate_action": "keep"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AttendanceRecord.objects.filter(operation=self.other_closed, auth_user=self.fc).exists())

    def test_manage_attendance_alone_does_not_bypass_object_rules(self):
        clerk = f.create_user("clerk", perms=f.MEMBER_PERMS + ["fleetops.manage_attendance"])
        client = self.client_for(clerk)
        payload = {
            "character_id": str(f.main_of(clerk).character_id),
            "attendance_value": "3",
            "duplicate_action": "keep",
            "notes": "",
        }
        response = client.post(reverse("fleetops:add_manual_attendance", args=[self.other_closed.uuid]), payload)
        self.assertIn(response.status_code, (403, 404))
        response = client.post(
            reverse("fleetops:set_attendance_multiplier", args=[self.other_closed.uuid]),
            {"attendance_multiplier": "3"},
        )
        self.assertIn(response.status_code, (403, 404))
        response = client.post(reverse("fleetops:delete_manual_attendance", args=[self.other_manual.pk]))
        self.assertIn(response.status_code, (403, 404))

        page = client.get(reverse("fleetops:manual_attendance"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(list(page.context["form"].fields["operation"].queryset), [])
        response = client.post(
            reverse("fleetops:manual_attendance"), {**payload, "operation": str(self.other_closed.pk)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("operation", response.context["form"].errors)

        self.assertFalse(AttendanceRecord.objects.filter(auth_user=clerk).exists())
        self.assertTrue(AttendanceRecord.objects.filter(pk=self.other_manual.pk).exists())
        self.other_closed.refresh_from_db()
        self.assertEqual(self.other_closed.attendance_multiplier, 1)

    def test_fc_lead_manages_any_fleet(self):
        client = self.client_for(self.lead)
        response = client.post(
            reverse("fleetops:add_operation_role", args=[self.other_closed.uuid]),
            {"role": "backseat_fc", "character_id": str(self.outsider_main.character_id), "notes": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            OperationRoleAssignment.objects.filter(
                operation=self.other_closed, character_id=self.outsider_main.character_id, role="backseat_fc"
            ).exists()
        )
        response = client.post(reverse("fleetops:end_fleet", args=[self.other_active.uuid]))
        self.assertEqual(response.status_code, 302)
        self.other_active.refresh_from_db()
        self.assertEqual(self.other_active.status, CLOSED)
        self.assertTrue(AuditLog.objects.filter(action="fleet.end", actor=self.lead).exists())

    def test_kick_capsules_requires_active_fleet(self):
        response = self.client_for(self.lead).post(reverse("fleetops:kick_capsules", args=[self.own_closed.uuid]))
        self.assertIn(response.status_code, (403, 404))
        self.kick.assert_not_called()

    def test_kick_capsules_skips_fleet_boss_and_reports_failures(self):
        FleetMemberState.objects.filter(operation=self.own_active, character_id=self.fc_main.character_id).update(
            ship_type_id=CAPSULE_TYPE_ID, ship_type_name="Capsule"
        )
        self.kick.side_effect = [
            None,
            FleetESIError("FLEET_WRITE_FORBIDDEN", "ESI denied permission to kick this fleet member."),
        ]
        # The fleet is read again after the kicks, at once and after ESI's cache; those reads are not part of this test.
        with mock.patch("fleetops.services.fleet_controls.sync_operation") as sync, mock.patch(
            "fleetops.services.fleet_refresh.schedule_refresh"
        ) as schedule:
            response = self.client_for(self.fc).post(reverse("fleetops:kick_capsules", args=[self.own_active.uuid]))
        sync.assert_called_once()
        schedule.assert_called_once()
        self.assertEqual(response.status_code, 302)
        kicked_ids = sorted(call.args[3] for call in self.kick.call_args_list)
        self.assertEqual(kicked_ids, sorted([self.member_main.character_id, self.pod_pilot.character_id]))
        entry = AuditLog.objects.get(action="fleet.kick_capsules")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(len(entry.new_value["kicked"]), 1)
        self.assertEqual(len(entry.new_value["failures"]), 1)

    def test_detect_fleet_rejects_character_owned_by_someone_else(self):
        response = self.client_for(self.fc).get(
            reverse("fleetops:detect_fleet", args=[self.other_fc_main.character_id])
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "NOT_OWNED")
        self.detect.assert_not_called()

    def test_detect_fleet_reports_boss_role_and_esi_errors(self):
        client = self.client_for(self.fc)
        url = reverse("fleetops:detect_fleet", args=[self.fc_main.character_id])
        payload = client.get(url).json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["is_fleet_boss"])

        self.detect.side_effect = FleetESIError("NOT_IN_FLEET", "Character is currently not in a fleet.", 404)
        payload = client.get(url).json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "NOT_IN_FLEET")

    def test_start_fleet_only_offers_owned_characters(self):
        response = self.client_for(self.fc).get(reverse("fleetops:start_fleet"))
        choices = {value for value, _label in response.context["form"].fields["fc_character_id"].choices}
        self.assertEqual(choices, {str(self.fc_main.character_id), str(self.fc_alt.character_id)})

    def test_start_fleet_rejects_foreign_character(self):
        response = self.client_for(self.fc).post(
            reverse("fleetops:start_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "full",
                "fc_character_id": str(self.other_fc_main.character_id),
                "fleet_type": str(self.stratop.pk),
                "formup": "Jita",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("fc_character_id", response.context["form"].errors)
        self.assertEqual(FleetOperation.objects.filter(fc_user=self.fc).count(), 3)

    def test_fc_statistics_detail_hidden_from_other_members(self):
        client = self.client_for(self.member)
        self.assertEqual(client.get(reverse("fleetops:fc_statistics_detail", args=[self.member.pk])).status_code, 200)
        self.assertEqual(client.get(reverse("fleetops:fc_statistics_detail", args=[self.fc.pk])).status_code, 404)

    def test_corp_management_history_only_shows_own_corporation(self):
        client = self.client_for(self.corp_manager)
        f.add_attendance(self.own_closed, self.corp_manager)
        response = client.get(reverse("fleetops:attendance_history_corporation"))
        self.assertEqual(response.status_code, 200)
        corporations = {row.corporation_id for row in response.context["rows"]}
        self.assertEqual(corporations, {f.DEFAULT_CORP[0]})
        self.assertNotIn(self.outsider_main.character_name, response.content.decode())

    def test_corp_management_cannot_open_other_corporation_statistics(self):
        client = self.client_for(self.corp_manager)
        response = client.get(reverse("fleetops:corporation_statistics_detail", args=[f.OTHER_CORP[0]]))
        self.assertEqual(response.status_code, 403)
        response = client.get(reverse("fleetops:corporation_statistics"))
        self.assertEqual(response.context["stats"]["corporation_id"], f.DEFAULT_CORP[0])

    def test_manual_attendance_page_lists_only_fcs_own_entries(self):
        response = self.client_for(self.fc).get(reverse("fleetops:manual_attendance"))
        listed = {record.pk for record in response.context["recent_manual"]}
        self.assertIn(self.own_manual.pk, listed)
        self.assertNotIn(self.other_manual.pk, listed)
        operations = set(response.context["form"].fields["operation"].queryset)
        self.assertEqual(operations, {self.own_active, self.own_closed, self.credited_closed})


class PageRenderingTests(FleetOpsViewTestCase):
    def test_operation_detail_renders_tracking_data_for_owner(self):
        response = self.client_for(self.fc).get(self.detail_url(self.own_active))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/operation_detail.html")
        context = response.context
        self.assertTrue(context["can_manage"])
        self.assertTrue(context["can_manage_attendance"])
        self.assertTrue(context["stale"])
        self.assertEqual(context["pod_count"], 2)
        self.assertEqual(context["attendance_total"], 2)
        self.assertEqual(context["unique_players"], 2)
        content = response.content.decode()
        for text in (
            self.member_main.character_name,
            self.pod_pilot.character_name,
            "Capsule",
            "Ship change",
            "Logi Anchor",
            "Discord returned HTTP 500",
            "Kick 2 Capsules",
            "/srp/42/",
            "@everyone StratOp",
            reverse("fleetops:end_fleet", args=[self.own_active.uuid]),
        ):
            self.assertIn(text, content)

    def test_attendance_only_fleet_detail_keeps_previews_and_hides_retry_buttons(self):
        operation = f.create_operation(
            self.fc, send_ping=False, ping_text="Ping preview text", motd_text="MOTD preview text"
        )
        response = self.client_for(self.fc).get(self.detail_url(operation))
        content = response.content.decode()
        self.assertIn("PAP Only", content)
        self.assertIn("Ping preview text", content)
        self.assertIn("MOTD preview text", content)
        self.assertNotIn(reverse("fleetops:retry_ping", args=[operation.uuid]), content)
        self.assertNotIn(reverse("fleetops:retry_srp", args=[operation.uuid]), content)

    def test_retry_srp_is_offered_only_until_an_srp_fleet_is_linked(self):
        linked = f.create_operation(self.fc, srp_reference="9", srp_url="/srp/9/")
        unlinked = f.create_operation(self.fc)
        client = self.client_for(self.fc)

        self.assertNotContains(client.get(self.detail_url(linked)), reverse("fleetops:retry_srp", args=[linked.uuid]))
        self.assertContains(client.get(self.detail_url(unlinked)), reverse("fleetops:retry_srp", args=[unlinked.uuid]))

    def test_manual_fleet_detail_renders(self):
        operation = f.create_operation(
            self.fc, status=CLOSED, is_manual=True, send_ping=False, esi_fleet_id=0, tracking_enabled=False
        )
        f.add_attendance(operation, self.member, source=MANUAL, value=2)
        response = self.client_for(self.fc).get(self.detail_url(operation))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual Record")
        self.assertEqual(response.context["attendance_total"], 2)

    def test_dashboard_shows_member_participation(self):
        f.add_attendance(self.own_closed, self.member)
        response = self.client_for(self.member).get(reverse("fleetops:dashboard"))
        self.assertEqual(response.status_code, 200)
        active = list(response.context["active_operations"])
        self.assertEqual([op.pk for op in active], [self.own_active.pk])
        recent = [row["operation"].pk for row in response.context["recent_participations"]]
        # PAP of the running fleet is granted when it closes.
        self.assertEqual(recent, [self.own_closed.pk])

    def test_statistics_pages_render_with_data(self):
        client = self.client_for(self.lead)
        f.add_attendance(self.other_closed, self.member)
        now = timezone.now()
        for url in (
            reverse("fleetops:my_statistics"),
            reverse("fleetops:corporation_statistics"),
            reverse("fleetops:corporation_statistics_detail", args=[f.DEFAULT_CORP[0]]),
            reverse("fleetops:all_corporation_statistics"),
            reverse("fleetops:all_fc_statistics"),
            reverse("fleetops:fc_statistics_detail", args=[self.fc.pk]),
            reverse("fleetops:incentive_review"),
        ):
            with self.subTest(url=url):
                response = client.get(url, {"year": now.year, "month": now.month})
                self.assertEqual(response.status_code, 200)
                self.assertEqual((response.context["year"], response.context["month"]), (now.year, now.month))

        response = client.get(reverse("fleetops:attendance_history_alliance"))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.context["rows"]), 0)
        self.assertEqual(response.context["retention_days"], 365)

    def test_incentive_review_lists_fc_rows(self):
        response = self.client_for(self.lead).get(reverse("fleetops:incentive_review"))
        self.assertContains(response, self.fc_main.character_name)
        self.assertContains(response, "1000000000 ISK")

    def test_audit_log_page_lists_entries(self):
        response = self.client_for(self.lead).get(reverse("fleetops:audit_log"))
        self.assertContains(response, "configuration.update")
        self.assertContains(response, "Weight review")

    def test_preview_returns_rendered_ping_and_motd(self):
        response = self.client_for(self.fc).post(
            reverse("fleetops:preview_fleet"),
            {
                "request_id": str(uuid.uuid4()),
                "operation_mode": "attendance_only",
                "fc_character_id": str(self.fc_alt.character_id),
                "fleet_type": str(self.stratop.pk),
                "formup": "Amarr",
                "ping_channel": str(self.webhook.pk),
                "ping_mentions": ["everyone"],
            },
        )
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("FC Alt", payload["ping"])
        self.assertIn("FC Alt", payload["ping_display"])
        self.assertEqual(payload["blocking"], [])
        self.assertIn("Amarr", payload["ping"])
        self.assertIn("<b>StratOp</b>", payload["motd"])
        self.assertFalse(FleetOperation.objects.filter(formup="Amarr").exists())

    def test_authorize_esi_with_existing_token_hands_selected_token_to_start_page(self):
        token = self.fc.token_set.get()
        response = self.client_for(self.fc).post(reverse("fleetops:authorize_esi"), {"_token": str(token.pk)})
        self.assertRedirects(response, reverse("fleetops:start_fleet"), fetch_redirect_response=False)

    def test_rendered_pages_never_contain_webhook_url(self):
        client = self.client_for(self.lead)
        urls = [
            reverse("fleetops:dashboard"),
            reverse("fleetops:start_fleet"),
            reverse("fleetops:fleet_operations"),
            self.detail_url(self.own_active),
            self.detail_url(self.other_active),
            reverse("fleetops:edit_operation", args=[self.own_active.uuid]),
            reverse("fleetops:audit_log"),
            reverse("fleetops:configuration_index"),
            reverse("fleetops:configuration_settings"),
        ]
        for section in CONFIG_SECTIONS:
            urls.append(reverse("fleetops:configuration_list", args=[section]))
            if section != "ping-channels":
                urls.append(
                    reverse("fleetops:configuration_edit", args=[section, self.config_objects[section].pk])
                )
        # The ping channel's own edit page does not show its URL either.
        urls.append(reverse("fleetops:configuration_edit", args=["ping-channels", self.webhook.pk]))
        for url in urls:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(WEBHOOK_SECRET, response.content.decode())

    def test_edit_operation_updates_metadata_without_side_effects(self):
        client = self.client_for(self.fc)
        events_before = FleetMemberEvent.objects.count()
        response = client.post(
            reverse("fleetops:edit_operation", args=[self.own_active.uuid]),
            {
                "fleet_type": str(self.home_defense.pk),
                "doctrine_name": "Ferox Fleet",
                "formup": "Amarr",
                "comms": str(self.comms.pk),
                "logi_channel": str(self.logi.pk),
                "boost_channel": str(self.boost.pk),
                "additional_message": "Changed",
                "started_at": self.own_active.started_at.strftime("%Y-%m-%dT%H:%M"),
                "ended_at": "",
            },
        )
        self.assertRedirects(response, self.detail_url(self.own_active), fetch_redirect_response=False)
        self.own_active.refresh_from_db()
        self.assertEqual(self.own_active.fleet_type, self.home_defense)
        self.assertEqual(self.own_active.fleet_point_weight_snapshot, Decimal("1.00"))
        self.assertEqual(self.own_active.status, ACTIVE)
        self.assertEqual(AuditLog.objects.filter(action="operation.edit", actor=self.fc).count(), 1)
        self.assertEqual(FleetMemberEvent.objects.count(), events_before)
        self.discord_post.assert_not_called()
        self.set_motd.assert_not_called()
        self.sync.assert_not_called()


class QueryParameterTests(FleetOpsViewTestCase):
    PERIOD_PARAMS = [
        {"year": "-1"},
        {"year": "0"},
        {"year": "99999"},
        {"year": "abc"},
        {"month": "13"},
        {"month": "0"},
        {"month": "abc"},
        {"year": "abc", "month": "abc"},
        {"year": "2025", "month": "-3"},
        {"fleet_type": "abc"},
        {"page": "abc"},
        {"page": "999"},
        {"page": "-1"},
    ]

    def period_urls(self):
        return [
            reverse("fleetops:fleet_operations"),
            reverse("fleetops:my_statistics"),
            reverse("fleetops:corporation_statistics"),
            reverse("fleetops:corporation_statistics_detail", args=[f.DEFAULT_CORP[0]]),
            reverse("fleetops:all_corporation_statistics"),
            reverse("fleetops:all_fc_statistics"),
            reverse("fleetops:fc_statistics_detail", args=[self.fc.pk]),
            reverse("fleetops:incentive_review"),
            reverse("fleetops:dashboard"),
            reverse("fleetops:attendance_history_me"),
        ]

    def test_invalid_period_and_paging_parameters_never_fail(self):
        client = self.client_for(self.lead)
        for url in self.period_urls():
            for params in self.PERIOD_PARAMS:
                with self.subTest(url=url, params=params):
                    self.assertEqual(client.get(url, params).status_code, 200)

    def test_invalid_period_falls_back_to_current_month(self):
        now = timezone.now()
        client = self.client_for(self.lead)
        for params in ({"year": "99999", "month": "13"}, {"year": "abc"}, {"month": "0"}):
            with self.subTest(params=params):
                response = client.get(reverse("fleetops:my_statistics"), params)
                self.assertEqual((response.context["year"], response.context["month"]), (now.year, now.month))

    def test_explicit_period_is_respected(self):
        client = self.client_for(self.lead)
        for url in self.period_urls():
            if url in (
                reverse("fleetops:fleet_operations"),
                reverse("fleetops:dashboard"),
                reverse("fleetops:attendance_history_me"),
            ):
                continue
            with self.subTest(url=url):
                response = client.get(url, {"year": "2024", "month": "2"})
                self.assertEqual((response.context["year"], response.context["month"]), (2024, 2))
                self.assertIn(2024, response.context["year_options"])

    def test_archive_page_out_of_range_returns_last_page(self):
        for index in range(55):
            f.create_operation(
                self.fc, status=CLOSED, started_at=self.own_active.started_at - timedelta(minutes=index + 1)
            )
        client = self.client_for(self.lead)
        year = self.own_active.started_at.year
        response = client.get(reverse("fleetops:fleet_operations"), {"year": year, "page": "999"})
        self.assertEqual(response.status_code, 200)
        page = response.context["page"]
        self.assertEqual(page.number, page.paginator.num_pages)
        self.assertEqual(page.paginator.per_page, 50)

    def test_archive_filters_combine(self):
        client = self.client_for(self.lead)
        started = self.own_active.started_at
        response = client.get(
            reverse("fleetops:fleet_operations"),
            {
                "year": started.year,
                "month": started.month,
                "fleet_type": str(self.stratop.pk),
                "status": ACTIVE,
                "fc": "fc Main",
                "doctrine": "muninn",
                "q": "links",
            },
        )
        self.assertEqual([op.pk for op in response.context["page"].object_list], [self.own_active.pk])

    def test_unknown_corporation_returns_404(self):
        client = self.client_for(self.lead)
        self.assertEqual(
            client.get(reverse("fleetops:corporation_statistics_detail", args=[98_765_432])).status_code, 404
        )

    def test_unknown_fc_user_returns_404(self):
        client = self.client_for(self.lead)
        self.assertEqual(client.get(reverse("fleetops:fc_statistics_detail", args=[987_654])).status_code, 404)

    def test_unknown_objects_return_404(self):
        client = self.client_for(self.lead)
        self.assertEqual(client.get(reverse("fleetops:operation_detail", args=[uuid.uuid4()])).status_code, 404)
        self.assertEqual(client.get(reverse("fleetops:configuration_list", args=["nope"])).status_code, 404)
        self.assertEqual(
            client.get(reverse("fleetops:configuration_edit", args=["comms", 987_654])).status_code, 404
        )
        self.assertEqual(client.post(reverse("fleetops:incentive_recalculate", args=[987_654])).status_code, 404)
        self.assertEqual(client.post(reverse("fleetops:delete_operation_role", args=[987_654])).status_code, 404)

    def test_archive_unicode_digit_fleet_type_does_not_fail(self):
        response = self.client_for(self.lead).get(reverse("fleetops:fleet_operations"), {"fleet_type": "²"})
        self.assertEqual(response.status_code, 200)

    def test_archive_oversized_fleet_type_does_not_fail(self):
        response = self.client_for(self.lead).get(
            reverse("fleetops:fleet_operations"), {"fleet_type": "9" * 25}
        )
        self.assertEqual(response.status_code, 200)


class ViewAllStatsScopeTests(FleetOpsViewTestCase):
    """view_all_stats is a statistics permission, not a fleet records permission."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.stats_viewer = f.create_user(
            "statsviewer", perms=f.CORP_MANAGEMENT_PERMS + ["fleetops.view_all_stats"]
        )

    def test_view_all_stats_opens_all_statistics_pages(self):
        client = self.client_for(self.stats_viewer)
        for url in (
            reverse("fleetops:all_corporation_statistics"),
            reverse("fleetops:all_fc_statistics"),
            reverse("fleetops:fc_statistics_detail", args=[self.other_fc.pk]),
            reverse("fleetops:corporation_statistics_detail", args=[f.OTHER_CORP[0]]),
            reverse("fleetops:attendance_history_alliance"),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_view_all_stats_does_not_open_fleet_detail(self):
        response = self.client_for(self.stats_viewer).get(self.detail_url(self.other_closed))
        self.assertIn(response.status_code, (403, 404))

    def test_view_all_stats_does_not_list_other_active_fleets_on_dashboard(self):
        response = self.client_for(self.stats_viewer).get(reverse("fleetops:dashboard"))
        active = {op.pk for op in response.context["active_operations"]}
        self.assertNotIn(self.other_active.pk, active)

    def test_view_all_stats_does_not_list_other_fleets_in_archive(self):
        response = self.client_for(self.stats_viewer).get(
            reverse("fleetops:fleet_operations"), {"year": self.other_closed.started_at.year}
        )
        listed = {op.uuid for op in response.context["page"].object_list}
        self.assertNotIn(self.other_closed.uuid, listed)


class EndFleetPromptTests(FleetOpsViewTestCase):
    def detail_at(self, elapsed, status=ACTIVE):
        frozen = timezone.now().replace(microsecond=0)
        operation = f.create_operation(self.fc, status=status, started_at=frozen - elapsed)
        if status == CLOSED:
            operation.ended_at = frozen
            operation.save(update_fields=["ended_at"])
        client = self.client_for(self.fc)
        with mock.patch("django.utils.timezone.now", return_value=frozen):
            response = client.get(self.detail_url(operation))
        self.assertEqual(response.status_code, 200)
        return response

    def test_no_prompt_at_exactly_90_minutes(self):
        response = self.detail_at(timedelta(minutes=90))
        self.assertFalse(response.context["attendance_prompt_eligible"])
        self.assertNotContains(response, "endFleetModal")

    def test_prompt_after_90_minutes_30_seconds(self):
        response = self.detail_at(timedelta(minutes=90, seconds=30))
        self.assertTrue(response.context["attendance_prompt_eligible"])

    def test_prompt_after_91_minutes(self):
        response = self.detail_at(timedelta(minutes=91))
        self.assertTrue(response.context["attendance_prompt_eligible"])
        self.assertContains(response, "endFleetModal")

    def test_no_prompt_for_closed_fleet(self):
        response = self.detail_at(timedelta(hours=3), status=CLOSED)
        self.assertFalse(response.context["attendance_prompt_eligible"])


class AttendanceHistoryLimitTests(FleetOpsViewTestCase):
    def test_personal_history_does_not_truncate_large_result_sets(self):
        pilot = f.create_user("veteran", perms=f.MEMBER_PERMS)
        main = f.main_of(pilot)
        now = timezone.now()
        total = 2001
        operations = FleetOperation.objects.bulk_create(
            FleetOperation(
                status=CLOSED,
                created_by=self.fc,
                fc_user=self.fc,
                fc_character_id=self.fc_main.character_id,
                fc_character_name=self.fc_main.character_name,
                fleet_type=self.stratop,
                fleet_point_weight_snapshot=self.stratop.point_weight,
                formup="Jita",
                started_at=now - timedelta(hours=3 * index + 1),
                ended_at=now - timedelta(hours=3 * index),
            )
            for index in range(total)
        )
        AttendanceRecord.objects.bulk_create(
            AttendanceRecord(
                operation=operation,
                character_id=main.character_id,
                character_name=main.character_name,
                main_character_id=main.character_id,
                main_character_name=main.character_name,
                auth_user=pilot,
                corporation_id=main.corporation_id,
                corporation_name=main.corporation_name,
                source=AUTOMATIC,
                attendance_value=1,
                granted=True,
            )
            for operation in operations
        )
        url = reverse("fleetops:attendance_history_me")
        response = self.client_for(pilot).get(url)
        self.assertEqual(response.status_code, 200)
        paginator = response.context["page"].paginator
        self.assertEqual(paginator.count, total)
        self.assertEqual(response.context["total"], total)

        last = self.client_for(pilot).get(url, {"page": paginator.num_pages})
        self.assertEqual(last.status_code, 200)
        # The oldest fleet is the last one created, so it closes the last page.
        self.assertEqual(list(last.context["rows"])[-1].operation_id, operations[-1].pk)


class IncentiveActionTests(FleetOpsViewTestCase):
    def set_status(self, status):
        IncentivePeriod.objects.filter(pk=self.period.pk).update(status=status)

    def test_review_period_can_be_finalized_and_unlocked(self):
        client = self.client_for(self.lead)
        response = client.post(reverse("fleetops:incentive_finalize", args=[self.period.pk]))
        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, IncentivePeriod.Status.FINALIZED)
        response = client.post(reverse("fleetops:incentive_waiver", args=[self.period.pk, self.fc.pk]), {"waived": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MonthlyFCStatistic.objects.get(period=self.period, fc_user=self.fc).waived)
        client.post(reverse("fleetops:incentive_unlock", args=[self.period.pk]))
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, IncentivePeriod.Status.REVIEW)

    def test_finalize_open_period_is_rejected_gracefully(self):
        self.set_status(IncentivePeriod.Status.OPEN)
        response = self.client_for(self.lead).post(reverse("fleetops:incentive_finalize", args=[self.period.pk]))
        self.assertNotEqual(response.status_code, 500)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, IncentivePeriod.Status.OPEN)

    def test_recalculate_finalized_period_is_rejected_gracefully(self):
        self.set_status(IncentivePeriod.Status.FINALIZED)
        response = self.client_for(self.lead).post(
            reverse("fleetops:incentive_recalculate", args=[self.period.pk])
        )
        self.assertNotEqual(response.status_code, 500)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, IncentivePeriod.Status.FINALIZED)

    def test_unlock_only_applies_to_finalized_periods(self):
        self.set_status(IncentivePeriod.Status.OPEN)
        self.client_for(self.lead).post(reverse("fleetops:incentive_unlock", args=[self.period.pk]))
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, IncentivePeriod.Status.OPEN)
        self.assertFalse(AuditLog.objects.filter(action="incentive.unlock").exists())

    def test_out_of_range_period_year_cannot_break_recalculation(self):
        client = self.client_for(self.lead)
        client.post(
            reverse("fleetops:incentive_review"),
            {"create_period": "1", "year": "99999", "month": "1", "budget": "0", "minimum_fleets": "1"},
        )
        period = IncentivePeriod.objects.filter(year=99999).first()
        if period is not None:
            response = client.post(reverse("fleetops:incentive_recalculate", args=[period.pk]))
            self.assertNotEqual(response.status_code, 500)


class ViewRuleViolationTests(FleetOpsViewTestCase):
    def test_failed_ping_does_not_expose_webhook_url_on_detail_page(self):
        self.discord_post.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            f"/api/webhooks/1234567890/{WEBHOOK_SECRET} (Caused by NameResolutionError("
            "\"<urllib3.connection.HTTPSConnection object>: Failed to resolve 'discord.com'\"))"
        )
        response = self.client_for(self.fc).post(reverse("fleetops:retry_ping", args=[self.own_active.uuid]))
        self.assertEqual(response.status_code, 302)
        detail = self.client_for(self.member).get(self.detail_url(self.own_active))
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn(WEBHOOK_SECRET, detail.content.decode())

    def test_manual_attendance_not_added_to_cancelled_fleet(self):
        cancelled = f.create_operation(self.fc, status=CANCELLED)
        self.client_for(self.fc).post(
            reverse("fleetops:add_manual_attendance", args=[cancelled.uuid]),
            {"character_id": str(self.member_main.character_id), "attendance_value": "1", "duplicate_action": "keep"},
        )
        self.assertFalse(AttendanceRecord.objects.filter(operation=cancelled).exists())

    def test_edit_rejects_end_before_start(self):
        start = self.own_closed.started_at
        response = self.client_for(self.fc).post(
            reverse("fleetops:edit_operation", args=[self.own_closed.uuid]),
            {
                "fleet_type": str(self.home_defense.pk),
                "formup": "Jita",
                "started_at": start.strftime("%Y-%m-%dT%H:%M"),
                "ended_at": (start - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.own_closed.refresh_from_db()
        self.assertGreaterEqual(self.own_closed.ended_at, self.own_closed.started_at)

    def test_retry_ping_does_not_ping_for_attendance_only_fleet(self):
        operation = f.create_operation(self.fc, send_ping=False, ping_target=self.ping_target, ping_text="ping")
        OperationAction.objects.create(
            operation=operation,
            action="discord_ping",
            status=OperationAction.Status.SKIPPED,
            error_message="PAP-only mode: Discord ping intentionally skipped.",
        )
        self.client_for(self.fc).post(reverse("fleetops:retry_ping", args=[operation.uuid]))
        self.discord_post.assert_not_called()
        self.assertEqual(
            OperationAction.objects.get(operation=operation, action="discord_ping").status,
            OperationAction.Status.SKIPPED,
        )
