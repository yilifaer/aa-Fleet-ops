# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The one Discord choke point: every post checks again, right before it goes out, that the acting user may
use the fleet's ping channel for the fleet's group."""

import re
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

import fleetops
from fleetops import texts
from fleetops.models import AuditLog, DiscordWebhook, FleetOperation, GroupAccess, OperationAction
from fleetops.services import operations
from fleetops.services.operations import PingCooldown, retry_ping, start_fleet
from fleetops.services.pap_links import start_link_only_fleet
from fleetops.services.scheduling import (
    _send_pre_ping,
    cancel_scheduled_fleet,
    schedule_fleet,
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
# The modules that may read a ping channel's webhook address or load the Discord provider: the provider, the two
# posting services, the audit (which hides the address), the Django admin, the channel form and the model.
WEBHOOK_READERS = POSTERS | {"providers/pings.py", "services/audit.py", "admin.py", "forms_config.py", "models.py"}
# The address by name (an attribute, a getattr string, a template variable) or the provider module by any import.
WEBHOOK_ACCESS = re.compile(
    r"\bwebhook_url\b"
    r"|\bproviders\.pings\b"
    r"|\bfrom\s+\S*providers\s+import\b[^\n]*\bpings\b"
    r"|\bfrom\s+\.+\s*import\b[^\n]*\bpings\b"
    r"|\bfrom\s+\.pings\b"
)


def fresh(user):
    return get_user_model().objects.get(pk=user.pk)


def step(operation, name):
    return OperationAction.objects.get(operation=operation, action=name)


def webhook_readers(root):
    """The modules and templates under ``root`` that name the webhook address or load the Discord provider."""
    found = set()
    for path in [*root.rglob("*.py"), *root.rglob("*.html")]:
        relative = path.relative_to(root).as_posix()
        if "migrations/" in relative:
            continue
        if WEBHOOK_ACCESS.search(path.read_text(encoding="utf-8")):
            found.add(relative)
    return found


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

    def test_only_the_posting_modules_reach_the_webhook_address(self):
        readers = webhook_readers(PACKAGE)

        self.assertIn("services/operations.py", readers)
        self.assertLessEqual(readers, WEBHOOK_READERS)

    def test_the_webhook_guard_sees_direct_posts_and_indirect_imports(self):
        planted = {
            "services/direct.py": "requests.post(channel.webhook_url, json={'content': text})\n",
            "services/by_name.py": "url = getattr(channel, 'webhook_url')\n",
            "services/by_module.py": "send = getattr(import_module('fleetops.providers.pings'), 'send')\n",
            "providers/relative.py": "from . import doctrines, pings\n",
            "providers/relative_module.py": "from .pings import _redact\n",
            "views_example.py": "from fleetops.providers import esi, pings\n",
            "templates/fleetops/example.html": "{{ channel.webhook_url }}\n",
            "services/clean.py": "name = channel.name\nfrom fleetops.providers import esi\n",
            "migrations/0001_initial.py": "webhook_url = models.TextField()\n",
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative, text in planted.items():
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text, encoding="utf-8")

            found = webhook_readers(root)

        self.assertEqual(
            found,
            {
                "services/direct.py",
                "services/by_name.py",
                "services/by_module.py",
                "providers/relative.py",
                "providers/relative_module.py",
                "views_example.py",
                "templates/fleetops/example.html",
            },
        )

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


class FreshChannelTests(ChokePointTestBase):
    """The post reads the channel again: a change an administrator saves while a start runs counts for its ping."""

    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        self.shared = f.group_channel("Shared Pings", groups=[self.group], fcs_allowed=True)
        self.alliance_type = f.fleet_type("StratOp", "1.00")
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char)]

    def start_while(self, change, channel=None, during="detect_fleet_boss"):
        """Start an alliance fleet in ``channel`` and apply ``change`` between the start check and the post.

        ``change`` runs right after the step ``during`` of the start: by default the ESI check of the fleet boss,
        which comes after the start check and before the record exists.
        """
        original = getattr(operations, during)

        def run_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            change()
            return result

        cleaned_data = {
            "operation_mode": "full",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": self.alliance_type,
            "formup": "Jita",
            "ping_channel": channel or self.shared,
        }
        with mock.patch.object(operations, during, side_effect=run_then_change):
            return start_fleet(user=fresh(self.fc), cleaned_data=cleaned_data, request_id=uuid.uuid4())

    def test_a_channel_reserved_for_its_groups_during_the_start_gets_no_ping(self):
        started = self.start_while(lambda: DiscordWebhook.objects.filter(pk=self.shared.pk).update(fcs_allowed=False))

        self.assertEqual(started.status, Status.ACTIVE)
        self.assertRefused(step(started, "discord_ping"), texts.CHANNEL_NOT_ALLOWED(name="Shared Pings"))

    def test_a_channel_switched_off_during_the_start_gets_no_ping(self):
        started = self.start_while(lambda: DiscordWebhook.objects.filter(pk=self.shared.pk).update(is_active=False))

        self.assertRefused(step(started, "discord_ping"), texts.PING_CHANNEL_DISABLED(name="Shared Pings"))

    def test_a_channel_deleted_during_the_start_gets_no_ping(self):
        # Deleted once the record exists (its channel is then set to none in the database).
        started = self.start_while(
            lambda: DiscordWebhook.objects.filter(pk=self.shared.pk).delete(), during="create_srp_at_start"
        )

        action = step(started, "discord_ping")
        self.assertEqual(action.status, ActionStatus.SKIPPED)
        self.assertEqual(action.error_message, texts.NO_PING_CHANNEL())
        self.discord_post.assert_not_called()

    def test_a_webhook_replaced_during_the_start_gets_the_ping_at_its_new_address(self):
        replaced = "https://discord.com/api/webhooks/90000002/example-new-token"

        started = self.start_while(
            lambda: DiscordWebhook.objects.filter(pk=self.alliance.pk).update(webhook_url=replaced), channel=self.alliance
        )

        self.assertEqual(step(started, "discord_ping").status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 1)
        self.assertEqual(self.discord_post.call_args.args[0], replaced)

    def test_a_retry_reads_the_channel_again(self):
        operation = f.create_operation(self.fc, ping_channel=self.shared, ping_text="Form up", send_ping=True)
        operation = FleetOperation.objects.select_related("ping_channel").get(pk=operation.pk)
        DiscordWebhook.objects.filter(pk=self.shared.pk).update(fcs_allowed=False)

        self.assertRefused(retry_ping(operation, actor=fresh(self.fc)), texts.CHANNEL_NOT_ALLOWED(name="Shared Pings"))


class PingCooldownTests(ChokePointTestBase):
    """A ping retry waits out the cooldown of its channel, like Send Ping: no loop of pings by retrying."""

    def age(self, operation, name, seconds):
        OperationAction.objects.filter(operation=operation, action=name).update(
            executed_at=timezone.now() - timedelta(seconds=seconds)
        )

    def test_a_second_retry_within_the_cooldown_posts_nothing(self):
        self.reserved.allow_everyone = True
        self.reserved.save()
        operation = self.group_fleet(ping_mention_mode=FleetOperation.MentionMode.EVERYONE)
        self.assertEqual(retry_ping(operation, actor=fresh(self.leader)).status, ActionStatus.SUCCESS)

        with self.assertRaises(PingCooldown) as refused:
            retry_ping(operation, actor=fresh(self.leader))

        self.assertEqual(self.discord_post.call_count, 1)
        self.assertIn("Group Pings received a ping", str(refused.exception))
        action = step(operation, "discord_ping")
        self.assertEqual((action.status, action.attempts), (ActionStatus.SUCCESS, 1))

    def test_a_retry_after_the_cooldown_posts_again(self):
        operation = self.group_fleet()
        retry_ping(operation, actor=fresh(self.leader))
        self.age(operation, "discord_ping", 61)

        self.assertEqual(retry_ping(operation, actor=fresh(self.leader)).status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 2)

    def test_the_cooldown_holds_for_an_fc_lead_and_for_another_fleet_in_the_channel(self):
        first = self.group_fleet()
        second = self.group_fleet()
        retry_ping(first, actor=fresh(self.leader))

        with self.assertRaises(PingCooldown):
            retry_ping(second, actor=fresh(self.lead))
        with self.assertRaises(PingCooldown):
            retry_ping(first, actor=fresh(self.lead))
        self.assertEqual(self.discord_post.call_count, 1)

    def test_a_ping_without_a_fleet_starts_the_cooldown(self):
        operation = self.group_fleet()
        f.standalone_ping(channel=self.reserved, sender=self.leader, group=self.group)

        with self.assertRaises(PingCooldown):
            retry_ping(operation, actor=fresh(self.leader))
        self.discord_post.assert_not_called()

    def test_a_ping_that_failed_or_went_to_another_channel_does_not(self):
        failed = self.group_fleet()
        self.discord_post.return_value = mock.Mock(status_code=500, text="Example failure")
        self.assertEqual(retry_ping(failed, actor=fresh(self.leader)).status, ActionStatus.FAILED)
        self.discord_post.return_value = mock.Mock(status_code=204, text="")
        elsewhere = f.create_operation(self.fc, ping_channel=self.alliance, ping_text="Form up", send_ping=True)
        retry_ping(elsewhere, actor=fresh(self.fc))

        self.assertEqual(retry_ping(failed, actor=fresh(self.leader)).status, ActionStatus.SUCCESS)
        self.assertEqual(self.discord_post.call_count, 3)

    def test_a_retry_being_posted_holds_the_channel(self):
        operation = self.group_fleet()
        OperationAction.objects.create(operation=operation, action="discord_ping", status=ActionStatus.PENDING)

        with self.assertRaises(PingCooldown):
            retry_ping(operation, actor=fresh(self.leader))
        self.discord_post.assert_not_called()

    def test_a_pre_ping_retry_waits_for_the_pre_ping(self):
        operation = f.create_scheduled_operation(
            self.fc, ping_channel=self.alliance, ping_text="Pre ping", send_ping=True
        )
        self.assertEqual(_send_pre_ping(operation, actor=fresh(self.fc)).status, ActionStatus.SUCCESS)

        with self.assertRaises(PingCooldown):
            retry_ping(operation, actor=fresh(self.fc))
        self.assertEqual(self.discord_post.call_count, 1)

    def test_five_retry_requests_of_a_leader_post_once(self):
        operation = self.group_fleet(ping_mention_mode=FleetOperation.MentionMode.EVERYONE)
        self.reserved.allow_everyone = True
        self.reserved.save()
        self.client.force_login(self.leader)
        url = reverse("fleetops:retry_ping", kwargs={"operation_uuid": operation.uuid})

        responses = [self.client.post(url, follow=True) for _ in range(5)]

        self.assertEqual(self.discord_post.call_count, 1)
        self.assertContains(responses[-1], "received a ping")
        self.assertEqual(AuditLog.objects.filter(action="fleet.retry_ping").count(), 1)


class StartRequestOwnerTests(ChokePointTestBase):
    """A start form id names the fleet of the user who sent it; another user who sends it gets nothing."""

    def setUp(self):
        super().setUp()
        self.request_id = uuid.uuid4()
        self.operation = self.group_fleet(start_request_id=self.request_id)

    def test_start_fleet_refuses_the_form_id_of_another_user(self):
        with self.assertRaisesMessage(PermissionError, texts.START_REQUEST_USED()):
            start_fleet(user=fresh(self.fc), cleaned_data={}, request_id=self.request_id)

    def test_schedule_fleet_refuses_the_form_id_of_another_user(self):
        with self.assertRaisesMessage(PermissionError, texts.START_REQUEST_USED()):
            schedule_fleet(user=fresh(self.fc), cleaned_data={}, request_id=self.request_id)

    def test_a_pap_link_fleet_refuses_the_form_id_of_another_user(self):
        with self.assertRaisesMessage(PermissionError, texts.START_REQUEST_USED()):
            start_link_only_fleet(fresh(self.fc), {"request_id": self.request_id})

    def test_the_sender_gets_their_fleet_back(self):
        self.assertEqual(start_fleet(user=fresh(self.leader), cleaned_data={}, request_id=self.request_id), self.operation)
        self.assertEqual(
            schedule_fleet(user=fresh(self.leader), cleaned_data={}, request_id=self.request_id), self.operation
        )

