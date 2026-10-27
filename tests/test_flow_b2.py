# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""End-to-end paths of the 0.2.0b2 release, through the web pages.

A manual fleet waits for approval, is rejected, resubmitted and approved while the incentive period of its month
follows; a group leader starts a group fleet and pings the group; the access rules answer in Chinese; the site
language does not touch a ping or a MOTD; a month without a target is not assessed; and a fleet of the first
test build keeps its step texts in Chinese after the upgrade. Nothing leaves the test process.
"""

import csv
import importlib
import io
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.apps import apps
from django.conf import settings as django_settings
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    FleetOperation,
    GroupAccess,
    IncentivePeriod,
    MonthlyFCStatistic,
    OperationAction,
    OperationRoleAssignment,
    ReviewStatus,
    StandalonePing,
)
from fleetops.services import incentives
from fleetops.services.incentives import rebuild_period, toward_minimum_count
from fleetops.services.operations import end_fleet

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .test_denied_pages import CARD, UNKNOWN_CORPORATION
from .test_operations_tracking import FLEET_ID, FakeESIMixin
from .test_start_form import StartFormTestBase

UTC = dt_timezone.utc
Status = FleetOperation.Status
PENDING, APPROVED, REJECTED = ReviewStatus.PENDING, ReviewStatus.APPROVED, ReviewStatus.REJECTED
PeriodStatus = IncentivePeriod.Status
FLEETS, PING = GroupAccess.Mode.FLEETS, GroupAccess.Mode.PING
ZH = "zh-hans"
CJK = "[\u4e00-\u9fff]"
CREDIT_REFUSAL = "Special roles with FC credit can be assigned once an FC lead has approved the fleet."


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def last_month(now=None):
    """The first day of the previous month (UTC), so a manual fleet there is neither old nor in the future."""
    now = (now or timezone.now()).astimezone(UTC)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first - timedelta(days=1)).replace(day=1)


def form_time(moment):
    return moment.strftime("%Y-%m-%dT%H:%M")


class ReviewFlowTests(TestCase):
    """Flows 1 to 3: a manual fleet from creation to approval, a rejected one, and what a close changes."""

    def setUp(self):
        super().setUp()
        cache.clear()
        f.settings(incentive_enabled=True)
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.fc = f.create_user("flow_fc", perms=f.FC_PERMS, main_name="Flow FC")
        self.lead = f.create_user("flow_lead", perms=f.FC_LEAD_PERMS, main_name="Flow Lead")
        self.pilot = f.create_user("flow_pilot", perms=f.MEMBER_PERMS, main_name="Flow Pilot")
        self.late = f.create_user("flow_late", perms=f.MEMBER_PERMS, main_name="Late Pilot")
        self.month = last_month()
        self.period = IncentivePeriod.objects.create(year=self.month.year, month=self.month.month, budget=1000, minimum_fleets=1)
        # A first counted fleet of the lead calculated the period once (it is in review).
        self.first = f.closed_operation(self.lead, self.month + timedelta(days=2, hours=18), type_obj=self.stratop)
        rebuild_period(self.period)
        self.period.refresh_from_db()

    def stat(self, user):
        return MonthlyFCStatistic.objects.filter(period=self.period, fc_user=user).first()

    def callbacks(self):
        """The context that runs ``transaction.on_commit`` callbacks as a commit would."""
        return self.captureOnCommitCallbacks(execute=True)

    def create_manual(self, user, *, started=None):
        started = started or self.month + timedelta(days=9, hours=18)
        self.client.force_login(user)
        with self.callbacks():
            response = self.client.post(
                reverse("fleetops:manual_fleet"),
                {
                    "fleet_type": self.stratop.pk,
                    "formup": "Jita",
                    "started_at": form_time(started),
                    "ended_at": form_time(started + timedelta(hours=1)),
                    "attendance_multiplier": "1",
                },
            )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors)
        return FleetOperation.objects.get(uuid=response.url.split("/")[-2])

    def review(self, user, operation, action, *, rows=True, **extra):
        self.client.force_login(user)
        data = {"action": action, **extra}
        if rows:
            data["include_attendance"] = "on"
        with self.callbacks():
            return self.client.post(reverse("fleetops:review_operation", args=[operation.uuid]), data)

    def assign(self, user, operation, role="backseat_fc"):
        self.client.force_login(user)
        with self.callbacks():
            return self.client.post(
                reverse("fleetops:add_operation_role", args=[operation.uuid]),
                {"role": role, "character_id": str(f.main_of(self.pilot).character_id), "notes": ""},
            )

    def test_a_manual_fleet_is_approved_gets_its_credit_role_and_the_period_follows(self):
        stored = self.period.calculated_at
        manual = self.create_manual(self.fc)
        self.assertEqual((manual.review_status, manual.is_manual), (PENDING, True))
        self.client.force_login(self.fc)
        self.client.post(
            reverse("fleetops:add_manual_attendance", args=[manual.uuid]),
            {
                "character_id": str(f.main_of(self.pilot).character_id),
                "attendance_value": "1",
                "duplicate_action": AttendanceRecord.Correction.KEEP,
            },
        )
        # Not approved yet: the fleet is not in the period and a credit role is refused.
        self.assertIsNone(self.stat(self.fc))
        response = self.assign(self.fc, manual)
        self.assertEqual(flashed(response)[-1], CREDIT_REFUSAL)
        self.assertFalse(OperationRoleAssignment.objects.exists())
        # A role without credit is no problem.
        self.assign(self.fc, manual, role="logi_anchor")
        self.assertEqual(OperationRoleAssignment.objects.get().grants_fc_credit, False)

        self.review(self.lead, manual, "approve")

        manual.refresh_from_db()
        self.assertEqual(manual.review_status, APPROVED)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, PeriodStatus.REVIEW)
        self.assertGreater(self.period.calculated_at, stored)
        self.assertEqual((self.stat(self.fc).fleet_count, self.stat(self.fc).total_points), (1, Decimal("2.00")))
        self.assertEqual(AuditLog.objects.filter(action="incentive.auto_recalculate").count(), 1)

        # Now the credit role is accepted and the pilot gets the fleet in the period.
        response = self.assign(self.fc, manual)
        self.assertEqual(OperationRoleAssignment.objects.filter(grants_fc_credit=True).count(), 1, flashed(response))
        self.assertEqual(self.stat(self.pilot).fleet_count, 1)

        # A row added after the approval waits for its own decision; rejecting it grants nothing.
        self.client.force_login(self.fc)
        self.client.post(
            reverse("fleetops:add_manual_attendance", args=[manual.uuid]),
            {
                "character_id": str(f.main_of(self.late).character_id),
                "attendance_value": "1",
                "duplicate_action": AttendanceRecord.Correction.KEEP,
            },
        )
        row = AttendanceRecord.objects.get(operation=manual, auth_user=self.late)
        self.assertEqual(row.review_status, PENDING)
        self.client.force_login(self.lead)
        self.client.post(reverse("fleetops:review_attendance"), {"action": "reject", "ids": [row.pk], "note": "Not there"})
        row.refresh_from_db()
        self.assertEqual((row.review_status, row.granted), (REJECTED, False))
        self.assertTrue(AuditLog.objects.filter(action="approval.attendance_reject").exists())

    def test_a_rejected_fleet_stays_rejected_until_it_is_resubmitted(self):
        manual = self.create_manual(self.fc)
        self.review(self.lead, manual, "reject", note="Wrong date")
        manual.refresh_from_db()
        self.assertEqual(manual.review_status, REJECTED)

        # An edit alone does not send the fleet back.
        self.client.force_login(self.fc)
        edited = self.client.post(
            reverse("fleetops:edit_operation", args=[manual.uuid]),
            {
                "fleet_type": self.stratop.pk,
                "doctrine_name": "",
                "formup": "Amarr",
                "additional_message": "",
                "started_at": form_time(manual.started_at),
                "ended_at": form_time(manual.ended_at),
            },
        )
        self.assertEqual(edited.status_code, 302)
        manual.refresh_from_db()
        self.assertEqual((manual.formup, manual.review_status), ("Amarr", REJECTED))
        page = self.client.get(reverse("fleetops:operation_detail", args=[manual.uuid]))
        self.assertContains(page, "Resubmit for approval")

        # The resubmission carries a note and the queue shows it next to the fleet.
        with self.callbacks():
            response = self.client.post(reverse("fleetops:resubmit_operation", args=[manual.uuid]), {"note": "Date corrected"})
        self.assertEqual(response.status_code, 302)
        manual.refresh_from_db()
        self.assertEqual((manual.review_status, manual.review_request_note), (PENDING, "Date corrected"))
        self.client.force_login(self.lead)
        queue = self.client.get(reverse("fleetops:approval_queue"))
        self.assertContains(queue, "Date corrected")

        self.review(self.lead, manual, "approve")
        manual.refresh_from_db()
        self.assertEqual((manual.review_status, manual.review_request_note), (APPROVED, ""))
        self.assertEqual(self.stat(self.fc).fleet_count, 1)
        self.assertNotContains(self.client.get(reverse("fleetops:approval_queue")), "Date corrected")

    def test_a_close_changes_only_the_period_that_is_not_finalised(self):
        april = IncentivePeriod.objects.create(
            year=self.month.year if self.month.month > 1 else self.month.year - 1,
            month=self.month.month - 1 if self.month.month > 1 else 12,
            budget=500,
            minimum_fleets=1,
        )
        earlier = last_month(self.month)
        self.assertEqual((april.year, april.month), (earlier.year, earlier.month))
        counted = f.closed_operation(self.lead, earlier + timedelta(days=3, hours=18), type_obj=self.stratop)
        rebuild_period(april)
        incentives.finalize_period(april, self.lead)
        april.refresh_from_db()
        self.assertEqual(april.status, PeriodStatus.FINALIZED)
        before_final = MonthlyFCStatistic.objects.filter(period=april, fc_user=self.lead).values_list("fleet_count", "total_points").get()
        review_calculated = self.period.calculated_at

        running_this = f.create_operation(self.fc, started_at=self.month + timedelta(days=12, hours=18), type_obj=self.stratop, tracking_enabled=True)
        running_before = f.create_operation(self.fc, started_at=earlier + timedelta(days=12, hours=18), type_obj=self.stratop, tracking_enabled=True)
        with self.callbacks(), mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(running_this, actor=self.fc)
        with self.callbacks(), mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(running_before, actor=self.fc)

        self.period.refresh_from_db()
        self.assertGreater(self.period.calculated_at, review_calculated)
        self.assertEqual(self.stat(self.fc).fleet_count, 1)
        # The finalised month keeps its rows: the fleet that closed in it did not change them.
        after_final = MonthlyFCStatistic.objects.filter(period=april, fc_user=self.lead).values_list("fleet_count", "total_points").get()
        self.assertEqual(after_final, before_final)
        self.assertFalse(MonthlyFCStatistic.objects.filter(period=april, fc_user=self.fc).exists())
        self.assertEqual(counted.fc_user, self.lead)


class GroupFlowTests(FakeESIMixin, TestCase):
    """Flow 4: a group of 0-weight fleets, its leader's fleet, the ping to the group and the refused alliance ping."""

    def setUp(self):
        super().setUp()
        f.settings(srp_auto_create=False, incentive_enabled=True)
        f.builtin_default_templates()
        self.activity = f.fleet_type("Group Activity", "0.00")
        self.stratop = f.fleet_type("StratOp", "2.00")
        self.leader = f.create_user("group_leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.leader_char = f.main_of(self.leader)
        f.add_token(self.leader, self.leader_char)
        self.pilot = f.create_user("group_pilot", perms=f.MEMBER_PERMS, main_name="Pilot B")
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader])
        f.group_access(self.group, FLEETS, types=[self.activity])
        self.alliance = f.ping_channel("Alliance Pings")
        self.channel = f.group_channel("Group Pings", groups=[self.group])
        self.esi.put_in_fleet(self.leader_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.leader_char)]

    def start_data(self, **extra):
        data = {
            "request_id": str(uuid.uuid4()),
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(self.leader_char.character_id),
            "fleet_type": str(self.activity.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "fleet_group": str(self.group.pk),
        }
        data.update(extra)
        return data

    def test_the_leader_runs_a_group_fleet_and_pings_the_group(self):
        self.client.force_login(self.leader)
        response = self.client.post(reverse("fleetops:start_fleet"), self.start_data())
        self.assertEqual(response.status_code, 302)
        operation = FleetOperation.objects.get()
        self.assertEqual((operation.group, operation.fleet_type), (self.group, self.activity))
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertEqual(self.discord_post.call_args.args[0], self.channel.webhook_url)

        # The fleet ends: PAP is granted, the points of the fleet are 0 and it is no fleet of the incentive minimum.
        f.member_state(operation, f.main_of(self.pilot), accumulated_seconds=3600, active=False, user=self.pilot)
        with mock.patch("fleetops.services.operations.sync_operation"):
            end_fleet(operation, actor=self.leader)
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CLOSED)
        granted = AttendanceRecord.objects.filter(operation=operation, granted=True)
        self.assertIn(self.pilot.pk, {row.auth_user_id for row in granted})
        period = IncentivePeriod.objects.create(
            year=operation.started_at.year, month=operation.started_at.month, budget=1000, minimum_fleets=1
        )
        rebuild_period(period)
        row = MonthlyFCStatistic.objects.filter(period=period, fc_user=self.leader).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.total_points, Decimal("0.00"))
        self.assertEqual(toward_minimum_count(row), 0)
        self.assertFalse(row.eligible)

        # The same leader sends a ping to the group channel: a record without a fleet.
        self.discord_post.reset_mock()
        group_ping = self.client.post(
            reverse("fleetops:send_ping"),
            {
                "request_id": str(uuid.uuid4()),
                "ping_channel": str(self.channel.pk),
                "group": str(self.group.pk),
                "ping_mentions": ["none"],
                "additional_message": "Mining op in Jita",
            },
        )
        self.assertEqual(group_ping.status_code, 302, getattr(group_ping, "content", b"")[:400])
        ping = StandalonePing.objects.get()
        self.assertEqual((ping.sender, ping.group), (self.leader, self.group))
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertEqual(self.discord_post.call_args.args[0], self.channel.webhook_url)
        self.assertEqual(FleetOperation.objects.count(), 1)

        # A crafted ping to the alliance channel is refused and nothing is posted.
        self.discord_post.reset_mock()
        crafted = self.client.post(
            reverse("fleetops:send_ping"),
            {
                "request_id": str(uuid.uuid4()),
                "ping_channel": str(self.alliance.pk),
                "group": str(self.group.pk),
                "ping_mentions": ["none"],
                "additional_message": "Everybody out",
            },
        )
        self.assertNotEqual(crafted.status_code, 302)
        self.discord_post.assert_not_called()
        self.assertEqual(StandalonePing.objects.count(), 1)
        # A crafted alliance fleet is refused as well.
        crafted_fleet = self.client.post(
            reverse("fleetops:start_fleet"),
            self.start_data(ping_channel=str(self.alliance.pk), fleet_type=str(self.stratop.pk)),
        )
        self.assertEqual(crafted_fleet.status_code, 200)
        self.discord_post.assert_not_called()
        self.assertEqual(FleetOperation.objects.count(), 1)


class AccessInChineseTests(TestCase):
    """Flow 5: the rules of FB12 across statistics, members, member detail and SRP summaries, in Chinese."""

    def setUp(self):
        super().setUp()
        f.srp_settings(enabled=True)
        self.corp = f.create_user("corp", perms=f.CORP_MANAGEMENT_PERMS, main_name="Pilot B")
        self.colleague = f.create_user("colleague", perms=f.MEMBER_PERMS, main_name="Pilot D")
        self.other = f.create_user("other", perms=f.MEMBER_PERMS, main_name="Pilot C", corporation=f.OTHER_CORP)
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.member = f.create_user("member", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.own, self.foreign = f.DEFAULT_CORP[0], f.OTHER_CORP[0]
        self.addCleanup(translation.deactivate)

    def get(self, user, name, *args):
        client = Client()
        client.force_login(user)
        client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = ZH
        return client.get(reverse(f"fleetops:{name}", args=args))

    def card(self, response):
        found = CARD.search(response.content.decode())
        self.assertTrue(found, "no FleetOps denied card")
        return found.group(0)

    def assertOneNotFound(self, user, name, known, unknown):
        first, second = self.get(user, name, known), self.get(user, name, unknown)
        self.assertEqual((first.status_code, second.status_code), (404, 404))
        self.assertEqual(self.card(first), self.card(second))
        self.assertRegex(self.card(first), CJK)
        return first

    def test_other_corporations_and_unknown_ids_answer_the_same_404_with_a_reason(self):
        for name, known, unknown in (
            ("corporation_statistics_detail", self.foreign, UNKNOWN_CORPORATION),
            ("corporation_members_detail", self.foreign, UNKNOWN_CORPORATION),
            ("srp_summary_corporation", self.foreign, UNKNOWN_CORPORATION),
            ("member_detail", self.other.pk, 9_999_999),
        ):
            with self.subTest(page=name):
                self.assertOneNotFound(self.corp, name, known, unknown)

    def test_the_own_corporation_and_own_data_open(self):
        for name, argument in (
            ("corporation_statistics_detail", self.own),
            ("corporation_members_detail", self.own),
            ("srp_summary_corporation", self.own),
            ("member_detail", self.colleague.pk),
        ):
            with self.subTest(page=name):
                self.assertEqual(self.get(self.corp, name, argument).status_code, 200)
        self.assertEqual(self.get(self.fc, "srp_summary_fc", self.fc.pk).status_code, 200)
        self.assertOneNotFound(self.fc, "srp_summary_fc", self.lead.pk, 9_999_999)

    def test_a_member_without_the_permission_gets_the_reason_in_chinese(self):
        response = self.get(self.member, "corporation_statistics_detail", self.own)

        self.assertEqual(response.status_code, 403)
        self.assertRegex(self.card(response), CJK)
        self.assertIn("fleetops.view_corp_stats", self.card(response))

    def test_an_unknown_page_of_fleetops_is_one_neutral_404(self):
        client = Client()
        client.force_login(self.member)
        client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = ZH

        response = client.get(reverse("fleetops:export", args=["no-such-export"]), {"format": "csv"})

        self.assertEqual(response.status_code, 404)
        self.assertRegex(self.card(response), CJK)


class LanguageOfMessagesTests(StartFormTestBase):
    """Flow 6: on a Chinese site a Chinese FC still sends English pings and MOTDs (5-10, 5-11)."""

    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        f.settings(srp_auto_create=False)
        self.addCleanup(translation.deactivate)
        self.client.force_login(self.fc)
        self.client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = ZH
        self.channel = f.ping_channel("Zh Pings", allow_everyone=True, allow_here=True)
        patches = {
            "detect": mock.patch(
                "fleetops.services.operations.detect_character_fleet",
                side_effect=lambda user, character_id, fresh=False: __import__(
                    "fleetops.providers.esi", fromlist=["FleetDetectionResult"]
                ).FleetDetectionResult(fleet_id=FLEET_ID, role="fleet_commander", fleet_boss_id=character_id),
            ),
            "motd": mock.patch("fleetops.services.operations.set_fleet_motd"),
            "sync": mock.patch("fleetops.services.operations.sync_operation", return_value=0),
            "post": mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text="")),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def preview(self, **extra):
        return self.client.post(reverse("fleetops:preview_fleet"), self.data(ping_channel=str(self.channel.pk), **extra))

    @override_settings(LANGUAGE_CODE=ZH)
    def test_preview_and_start_agree_and_are_english(self):
        preview = self.preview(additional_message="x" * 2100)
        payload = preview.json()
        self.assertTrue(payload["ok"], payload)
        # The warning is for the person at the screen and follows their language.
        self.assertRegex(" ".join(payload["warnings"]), CJK)
        self.assertNotRegex(payload["ping"].replace("x" * 2100, ""), CJK)
        self.assertIn("FC: ", payload["motd"])
        self.assertIn("showinfo:1378//", payload["motd"])
        self.assertIn('Form Up: <loc><a href="showinfo:5//30000142">Jita</a></loc>', payload["motd"])
        self.assertNotRegex(payload["motd"], CJK)

        short = self.preview(additional_message="Bring boosters").json()
        started = self.client.post(
            reverse("fleetops:start_fleet"),
            self.data(ping_channel=str(self.channel.pk), additional_message="Bring boosters"),
        )
        self.assertEqual(started.status_code, 302)
        operation = FleetOperation.objects.get()
        self.assertEqual(operation.ping_text, short["ping"])
        self.assertEqual(operation.motd_text, short["motd"])
        self.assertEqual(self.mocks["post"].call_args.kwargs["json"]["content"], short["ping"])
        self.assertEqual(self.mocks["motd"].call_args.args[3], short["motd"])
        for text in (operation.ping_text, operation.motd_text):
            self.assertNotRegex(text, CJK)

    @override_settings(LANGUAGE_CODE=ZH)
    def test_a_pre_ping_its_cancellation_notice_and_a_send_ping_stay_english(self):
        schedule = SchedulingFlow(self)
        operation = schedule.pre_ping()
        self.assertRegex(operation.ping_text, r"<t:\d+:t>")
        self.assertNotRegex(operation.ping_text, CJK)
        posted = [call.kwargs["json"]["content"] for call in self.mocks["post"].call_args_list]
        self.assertEqual(posted, [operation.ping_text])

        self.mocks["post"].reset_mock()
        cancelled = self.client.post(reverse("fleetops:cancel_scheduled_fleet", args=[operation.uuid]), {"notify": "on"})
        self.assertEqual(cancelled.status_code, 302)
        notice = self.mocks["post"].call_args.kwargs["json"]["content"]
        self.assertIn("**Cancelled:**", notice)
        self.assertNotRegex(notice, CJK)

        self.mocks["post"].reset_mock()
        sender = f.create_user("zh_sender", perms=f.FC_PERMS, main_name="Zh Sender")
        self.client.force_login(sender)
        self.client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = ZH
        sent = self.client.post(
            reverse("fleetops:send_ping"),
            {
                "request_id": str(uuid.uuid4()),
                "ping_channel": str(self.channel.pk),
                "ping_mentions": ["here"],
                "additional_message": "Form up now",
            },
        )
        self.assertEqual(sent.status_code, 302)
        content = self.mocks["post"].call_args.kwargs["json"]["content"]
        self.assertIn("online members", content)
        self.assertNotRegex(content, CJK)


class SchedulingFlow:
    """A pre ping through the start page of the test that owns this object."""

    def __init__(self, case):
        self.case = case

    def pre_ping(self):
        when = (timezone.now() + timedelta(hours=2)).astimezone(UTC).replace(second=0, microsecond=0)
        response = self.case.client.post(
            reverse("fleetops:start_fleet"),
            self.case.data(
                fleet_timing="pre_ping",
                fleet_date=when.strftime("%Y-%m-%d"),
                fleet_clock=when.strftime("%H:%M"),
                ping_channel=str(self.case.channel.pk),
                ping_mentions=["everyone"],
            ),
        )
        self.case.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return FleetOperation.objects.get(status=Status.SCHEDULED)


class NoTargetFlowTests(TestCase):
    """Flow 7: a month without an applicable monthly target is not assessed anywhere (5-5)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("django.utils.timezone.now", return_value=utc(2026, 4, 15, 12))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fc = f.member("fc", main_name="Example FC", perms=f.FC_PERMS, active=False)
        self.alice = f.member("alice", main_name="Pilot A", perms=f.MEMBER_PERMS)
        self.bob = f.member("bob", main_name="Pilot B", perms=f.MEMBER_PERMS)
        operation = f.closed_operation(self.fc, utc(2026, 3, 5, 18))
        f.add_attendance(operation, self.alice, value=3)
        self.stats = f.member("stats", main_name="Stats Lead", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"])
        self.addCleanup(translation.deactivate)

    def get(self, user, name, *args, **params):
        self.client.force_login(user)
        return self.client.get(reverse(f"fleetops:{name}", args=args), {"year": 2026, "month": 3, **params})

    def test_every_page_says_nobody_is_assessed(self):
        members = self.get(self.stats, "alliance_members")
        self.assertContains(members, "No target for this month: nobody is assessed or listed as inactive.")
        self.assertEqual(members.context["counts"]["inactive"], 0)
        overview = self.get(self.stats, "all_corporation_statistics")
        self.assertContains(overview, "No monthly target applies to this month, so corporations and members are not assessed.")
        corporation = self.get(self.stats, "corporation_statistics_detail", f.DEFAULT_CORP[0])
        self.assertContains(corporation, "No monthly target applies to this month")
        mine = self.get(self.alice, "my_statistics")
        self.assertEqual(mine.status_code, 200)
        self.assertNotContains(mine, "fo-pill-bad")
        for dataset in ("members", "corporations"):
            with self.subTest(dataset=dataset):
                export = self.get(self.stats, "export", dataset, format="csv")
                self.assertEqual(export.status_code, 200)
                rows = list(csv.DictReader(io.StringIO(b"".join(export.streaming_content).decode("utf-8-sig"))))
                self.assertTrue(rows)
                # The status and target columns stay empty: nobody is assessed.
                for row in rows:
                    row = {key.lower(): value for key, value in row.items()}
                    self.assertEqual(row["status"], "", row)
                    self.assertEqual(row.get("target", ""), "", row)


class LegacyFleetTests(TestCase):
    """Flow 8: a fleet of the first test build after `migrate` shows its step texts in Chinese."""

    def setUp(self):
        super().setUp()
        self.addCleanup(translation.deactivate)
        self.fc = f.create_user("legacy_fc", perms=f.FC_PERMS, main_name="Legacy FC")
        self.operation = f.create_operation(
            self.fc, status=Status.CLOSED,
            started_at=timezone.now() - timedelta(days=40),
            last_error="Fleet no longer exists.",
            pap_rules=FleetOperation.PapRules.LEGACY,
        )
        self.texts = (
            ("fleet_end", "Automatic"),
            ("discord_ping", "Attendance-only mode: Discord ping intentionally skipped."),
            ("fleet_detection", "Fleet no longer exists."),
        )
        for action, text in self.texts:
            OperationAction.objects.create(
                operation=self.operation, action=action, status=OperationAction.Status.SUCCESS, error_message=text
            )

    def test_the_steps_and_the_pap_link_card_are_chinese_after_the_upgrade(self):
        data = importlib.import_module("fleetops.migrations.0020_b2_data")
        data.rewrite_legacy_step_texts(apps, None)
        stored = list(OperationAction.objects.filter(operation=self.operation).values_list("error_message", flat=True))
        self.assertNotIn("Attendance-only mode: Discord ping intentionally skipped.", stored)

        self.client.force_login(self.fc)
        self.client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = ZH
        page = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        for english in ("Ended automatically", "The fleet no longer exists.", "Discord ping intentionally skipped"):
            self.assertNotIn(english, html)
        self.assertRegex(html, CJK)
        # The PAP link card of a fleet closed before the upgrade says so in Chinese.
        self.assertIn('id="pap-links"', html)
        self.assertIn("在 PAP 按在队时长发放之前就已结束的舰队不能使用 PAP 登记链接；请改为补录 PAP。", html)
        self.assertNotIn("PAP links are not available", html)
