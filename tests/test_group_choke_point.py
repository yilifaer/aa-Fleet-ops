# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The one Discord choke point: every post checks again, right before it goes out, that the acting user may
use the fleet's ping channel for the fleet's group."""

import re
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

import fleetops
from fleetops import texts
from fleetops.models import FleetOperation, GroupAccess, OperationAction
from fleetops.services.operations import retry_ping
from fleetops.services.scheduling import (
    _send_pre_ping,
    cancel_scheduled_fleet,
    start_scheduled_fleet,
    update_scheduled_fleet,
)

from . import factories as f
from .test_operations_tracking import FLEET_ID, FakeESIMixin

Status = FleetOperation.Status
ActionStatus = OperationAction.Status
PACKAGE = Path(fleetops.__file__).resolve().parent
# The modules allowed to post to Discord: the fleet choke point and, later, the pings without a fleet.
POSTERS = {"services/operations.py", "services/standalone_pings.py"}


def fresh(user):
    return get_user_model().objects.get(pk=user.pk)


def step(operation, name):
    return OperationAction.objects.get(operation=operation, action=name)


class SourceGuardTests(SimpleTestCase):
    def test_only_the_choke_points_use_the_discord_webhook(self):
        users = set()
        for path in PACKAGE.rglob("*.py"):
            relative = path.relative_to(PACKAGE).as_posix()
            if relative == "providers/pings.py" or "migrations" in relative:
                continue
            if re.search(r"\bsend_discord_webhook\b", path.read_text(encoding="utf-8")):
                users.add(relative)
        self.assertTrue(users)
        self.assertLessEqual(users, POSTERS)
        self.assertIn("services/operations.py", users)

    def test_no_background_task_posts_to_discord(self):
        for path in (PACKAGE / "tasks.py", PACKAGE / "srp" / "tasks.py"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("providers.pings", text)
                self.assertNotIn("send_discord_webhook", text)


class ChokePointTestBase(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.settings(srp_auto_create=False)
        self.fleet_type = f.fleet_type("Group Activity", "0.00")
        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="Example FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Example Lead")
        self.leader = f.create_user("leader", perms=f.MEMBER_PERMS, main_name="Pilot A")
        self.group = f.aa_group("Example Fleet Group", leaders=[self.leader])
        self.access = f.group_access(self.group, GroupAccess.Mode.FLEETS, types=[self.fleet_type])
        self.alliance = f.ping_channel("Alliance Pings")
        self.reserved = f.group_channel("Group Pings", groups=[self.group])

    def reserve(self, channel):
        """Reserve an alliance channel for the group after a fleet was announced in it."""
        channel.allowed_groups.set([self.group])
        channel.fcs_allowed = False
        channel.save()

    def group_fleet(self, **extra):
        values = {
            "group": self.group,
            "group_name": self.group.name,
            "ping_channel": self.reserved,
            "ping_text": "Group ping",
            "send_ping": True,
            "type_obj": self.fleet_type,
        }
        values.update(extra)
        return f.create_operation(self.leader, **values)

    def assertRefused(self, action, message):
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertEqual(action.error_message, message)
        self.discord_post.assert_not_called()


class RetryPingTests(ChokePointTestBase):
    def test_the_leader_retries_the_ping_of_their_group_fleet(self):
        operation = self.group_fleet()

        action = retry_ping(operation, actor=fresh(self.leader))

        self.assertEqual(action.status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 1)

    def test_a_leader_removed_in_alliance_auth_is_refused(self):
        operation = self.group_fleet()
        self.group.authgroup.group_leaders.remove(self.leader.pk)

        self.assertRefused(
            retry_ping(operation, actor=fresh(self.leader)), texts.GROUP_NOT_LED(group="Example Fleet Group")
        )

    def test_a_deactivated_row_refuses_the_leader(self):
        operation = self.group_fleet()
        GroupAccess.objects.filter(pk=self.access.pk).update(is_active=False)

        self.assertRefused(
            retry_ping(operation, actor=fresh(self.leader)), texts.GROUP_NOT_LED(group="Example Fleet Group")
        )

    def test_a_channel_no_longer_assigned_to_the_group_is_refused(self):
        operation = self.group_fleet()
        self.reserved.allowed_groups.clear()

        self.assertRefused(
            retry_ping(operation, actor=fresh(self.leader)),
            texts.GROUP_CHANNEL_NOT_ASSIGNED(name="Group Pings", group="Example Fleet Group"),
        )

    def test_a_credited_co_fc_who_does_not_lead_the_group_is_refused(self):
        operation = self.group_fleet()

        self.assertRefused(retry_ping(operation, actor=fresh(self.fc)), texts.GROUP_NOT_LED(group="Example Fleet Group"))

    def test_an_fc_lead_posts_to_a_reserved_channel(self):
        operation = self.group_fleet()

        action = retry_ping(operation, actor=fresh(self.lead))

        self.assertEqual(action.status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 1)

    def test_an_alliance_fleet_whose_channel_was_reserved_later(self):
        operation = f.create_operation(self.fc, ping_channel=self.alliance, ping_text="Form up", send_ping=True)
        self.reserve(self.alliance)

        self.assertRefused(
            retry_ping(operation, actor=fresh(self.fc)), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings")
        )
        self.assertEqual(retry_ping(operation, actor=fresh(self.lead)).status, ActionStatus.SUCCESS)

    def test_a_retry_without_an_actor_posts_nothing(self):
        operation = f.create_operation(self.fc, ping_channel=self.alliance, ping_text="Form up", send_ping=True)

        self.assertRefused(retry_ping(operation, actor=None), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings"))


class ScheduledPostTests(ChokePointTestBase):
    def scheduled(self, channel=None, **extra):
        return f.create_scheduled_operation(
            self.fc,
            ping_channel=channel or self.alliance,
            ping_text="Pre ping",
            send_ping=True,
            type_obj=f.fleet_type("StratOp", "1.00"),
            **extra,
        )

    def test_a_pre_ping_to_a_channel_reserved_later_is_refused(self):
        operation = self.scheduled()
        self.reserve(self.alliance)

        self.assertRefused(
            _send_pre_ping(operation, actor=fresh(self.fc)), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings")
        )

    def test_the_updated_pre_ping_is_refused(self):
        operation = self.scheduled()
        self.reserve(self.alliance)
        when = timezone.now() + timedelta(hours=3)
        data = {
            "fleet_type": operation.fleet_type,
            "formup": "Jita",
            "ping_channel": self.alliance,
            "scheduled_at": when,
            "doctrine_source": "none",
        }

        update_scheduled_fleet(operation, actor=fresh(self.fc), cleaned_data=data, send_update=True)

        self.assertRefused(step(operation, "pre_ping"), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings"))

    def test_the_cancellation_notice_is_refused_for_the_fc_and_posted_for_an_fc_lead(self):
        refused = self.scheduled()
        allowed = self.scheduled()
        self.reserve(self.alliance)

        cancel_scheduled_fleet(refused, actor=fresh(self.fc), notify=True)
        self.assertRefused(step(refused, "cancel_notice"), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings"))

        cancel_scheduled_fleet(allowed, actor=fresh(self.lead), notify=True)
        self.assertEqual(step(allowed, "cancel_notice").status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 1)

    def test_the_form_up_ping_of_a_scheduled_fleet_whose_channel_was_reserved(self):
        operation = self.scheduled()
        self.reserve(self.alliance)
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char)]

        started = start_scheduled_fleet(
            operation, user=fresh(self.fc), character_id=self.fc_char.character_id, send_ping=True
        )

        self.assertEqual(started.status, Status.ACTIVE)
        self.assertRefused(step(started, "discord_ping"), texts.CHANNEL_NOT_ALLOWED(name="Alliance Pings"))
        # The fleet still starts; its FC lead can post the ping again.
        self.assertEqual(retry_ping(started, actor=fresh(self.lead)).status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 1)
