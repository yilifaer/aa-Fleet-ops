# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Fixes found by the test-site run of 0.3.0b1: the ping retry, fleet edits, the automatic SRP link, the shared ping
cooldown, who may give FC credit, the recalculation message, ended fleets, the long claim deadline and the
notification of an undone payment."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from fleetops import texts
from fleetops.forms import OperationRoleAssignmentForm
from fleetops.models import (
    AuditLog,
    FleetOperation,
    GroupAccess,
    OperationAction,
    SrpLoss,
    SrpLossScan,
    SrpSettings,
    StandalonePing,
)
from fleetops.providers.srp import FleetOpsSRPProvider
from fleetops.services import operations as operations_module
from fleetops.services import standalone_pings
from fleetops.services.operations import PingCooldown, ping_cooldown_problem, retry_ping, send_ping_after_cooldown
from fleetops.services.roles import add_role_assignment, may_give_fc_credit
from fleetops.srp import discovery, notify, review

from . import factories as f
from .test_operations_tracking import FakeESIMixin

Status = FleetOperation.Status
POST = "fleetops.providers.pings.requests.post"
OK = mock.Mock(status_code=204, text="")


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class RetryPingTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("retry_fc", perms=f.FC_PERMS)
        self.channel = f.ping_channel("Ops")
        self.stale = '<font size="14" color="#bfffffff"><a href="fitting:123:1;1::">Raven</a></font> old text'
        self.operation = f.create_operation(
            self.fc, ping_channel=self.channel, send_ping=True, ping_text=self.stale, doctrine_name="Raven"
        )

    def test_the_stored_markup_is_not_posted_again(self):
        with mock.patch(POST, return_value=OK) as post:
            retry_ping(self.operation, actor=self.fc)

        sent = post.call_args.kwargs["json"]["content"]
        self.assertNotIn("fitting:", sent)
        self.assertNotIn("<font", sent)
        self.operation.refresh_from_db()
        self.assertNotIn("fitting:", self.operation.ping_text)
        self.assertIn(self.operation.ping_text.split("\n")[0], sent)

    def test_the_ping_follows_the_current_fleet_data(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(formup="Amarr")
        self.operation.refresh_from_db()

        with mock.patch(POST, return_value=OK) as post:
            retry_ping(self.operation, actor=self.fc)

        self.assertIn("Amarr", post.call_args.kwargs["json"]["content"])


class EditFleetTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("edit_fc", perms=f.FC_PERMS)
        start = (timezone.now() - timedelta(days=3)).replace(second=37, microsecond=123456)
        self.operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=start, ended_at=start + timedelta(hours=1, seconds=12, microseconds=5)
        )
        self.client.force_login(self.fc)

    def post(self, **changes):
        started = self.operation.started_at
        ended = self.operation.ended_at
        data = {
            "fleet_type": self.operation.fleet_type_id,
            "doctrine_name": self.operation.doctrine_name,
            "formup": self.operation.formup,
            "additional_message": self.operation.additional_message,
            "started_at": started.strftime("%Y-%m-%dT%H:%M"),
            "ended_at": ended.strftime("%Y-%m-%dT%H:%M"),
            **changes,
        }
        return self.client.post(reverse("fleetops:edit_operation", args=[self.operation.uuid]), data)

    def test_saving_without_a_change_keeps_the_times_exactly_and_writes_no_entry(self):
        before = (self.operation.started_at, self.operation.ended_at)

        response = self.post()

        self.assertEqual(response.status_code, 302)
        self.operation.refresh_from_db()
        self.assertEqual((self.operation.started_at, self.operation.ended_at), before)
        self.assertFalse(AuditLog.objects.filter(action="operation.edit").exists())

    def test_a_change_of_another_field_keeps_the_seconds_and_is_audited(self):
        before = (self.operation.started_at, self.operation.ended_at)

        self.post(formup="Amarr")

        self.operation.refresh_from_db()
        self.assertEqual((self.operation.started_at, self.operation.ended_at), before)
        self.assertEqual(self.operation.formup, "Amarr")
        self.assertEqual(AuditLog.objects.filter(action="operation.edit").count(), 1)

    def test_a_new_time_is_stored_as_typed(self):
        later = self.operation.started_at - timedelta(minutes=10)

        self.post(started_at=later.strftime("%Y-%m-%dT%H:%M"))

        self.operation.refresh_from_db()
        self.assertEqual(self.operation.started_at, later.replace(second=0, microsecond=0))
        self.assertEqual(AuditLog.objects.filter(action="operation.edit").count(), 1)


class AutomaticSrpLinkTests(TestCase):
    def test_the_link_created_at_the_start_names_the_fc_as_actor(self):
        f.srp_settings()
        fc = f.create_user("link_fc", perms=f.FC_PERMS)
        operation = f.create_operation(fc)

        FleetOpsSRPProvider().create_for_operation(operation)

        entry = AuditLog.objects.get(action="srp.link_create")
        self.assertEqual(entry.actor, fc)


class LinkActorTests(TestCase):
    """srp.link_create names the person who acts: the one who starts the fleet or presses Retry SRP."""

    def setUp(self):
        f.srp_settings()
        f.settings(srp_provider="fleetops", srp_auto_create=True)
        self.fc = f.create_user("actor_fc", perms=f.FC_PERMS)
        self.lead = f.create_user("actor_lead", perms=f.FC_PERMS)

    def test_retry_srp_names_the_person_who_retries(self):
        from fleetops.services.operations import retry_srp

        operation = f.create_operation(self.fc)

        retry_srp(operation, actor=self.lead)

        self.assertEqual(AuditLog.objects.get(action="srp.link_create").actor, self.lead)

    def test_the_start_steps_name_the_user_who_starts(self):
        from fleetops.services.operations import create_srp_at_start

        operation = f.create_operation(self.fc)

        create_srp_at_start(operation, actor=self.lead)

        self.assertEqual(AuditLog.objects.get(action="srp.link_create").actor, self.lead)

    def test_the_automatic_start_without_a_person_names_the_fc(self):
        from fleetops.services.operations import create_srp_at_start

        operation = f.create_operation(self.fc)

        create_srp_at_start(operation)

        self.assertEqual(AuditLog.objects.get(action="srp.link_create").actor, self.fc)

    def test_another_provider_keeps_its_own_signature(self):
        from fleetops.providers.srp import _PROVIDERS, SRPLinkResult, create_srp_link

        calls = []

        class Other:
            key = "other_srp"

            def available(self):
                return True

            def create_for_operation(self, operation):
                calls.append(operation)
                return SRPLinkResult(self.key, reference="x", created=True)

        operation = f.create_operation(self.fc)

        with mock.patch.dict(_PROVIDERS, {"other_srp": Other()}):
            create_srp_link(operation, "other_srp", actor=self.lead)

        self.assertEqual(calls, [operation])


class LayoutTests(TestCase):
    def test_the_ship_render_of_the_srp_pages_keeps_64_pixels(self):
        from pathlib import Path

        import fleetops

        root = Path(fleetops.__file__).parent
        css = (root / "static/fleetops/fleetops.css").read_text()
        rule = next(line for line in css.splitlines() if line.startswith(".fo-render-64"))
        for part in ("flex: 0 0 64px", "width: 64px", "height: 64px", "align-self: flex-start"):
            self.assertIn(part, rule)
        for name in ("preview", "claim", "_loss_card"):
            source = (root / "templates/fleetops/srp" / f"{name}.html").read_text()
            self.assertIn('class="fo-render fo-render-64"', source, name)


class LongerDeadlineTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("deadline_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.srp = f.srp_settings(claim_deadline_days=7)
        self.now = timezone.now()

    def old_row(self, killmail_id, days):
        return SrpLoss.objects.create(
            character_id=1, killmail_id=killmail_id, kind=SrpLoss.Kind.OLD, killmail_time=self.now - timedelta(days=days)
        )

    def test_old_rows_that_fit_the_longer_deadline_are_dropped_and_the_scan_is_due(self):
        fits = self.old_row(1, 20)
        unknown = SrpLoss.objects.create(character_id=1, killmail_id=2, kind=SrpLoss.Kind.OLD)
        too_old = self.old_row(3, 60)
        kept = SrpLoss.objects.create(
            character_id=1, killmail_id=4, kind=SrpLoss.Kind.LOSS, killmail_time=self.now - timedelta(days=2)
        )
        scan = SrpLossScan.objects.create(character_id=1, next_check_at=self.now + timedelta(hours=1))

        removed = discovery.reopen_old_losses(7, 30, now=self.now)

        self.assertEqual(removed, 2)
        self.assertFalse(SrpLoss.objects.filter(pk__in=[fits.pk, unknown.pk]).exists())
        self.assertTrue(SrpLoss.objects.filter(pk__in=[too_old.pk, kept.pk]).count() == 2)
        scan.refresh_from_db()
        self.assertIsNone(scan.next_check_at)

    def test_a_shorter_or_equal_deadline_changes_nothing(self):
        self.old_row(1, 20)

        self.assertEqual(discovery.reopen_old_losses(30, 7, now=self.now), 0)
        self.assertEqual(discovery.reopen_old_losses(7, 7, now=self.now), 0)
        self.assertEqual(SrpLoss.objects.count(), 1)

    def test_no_deadline_means_the_default_window(self):
        self.old_row(1, 80)

        self.assertEqual(discovery.reopen_old_losses(30, 0, now=self.now), 1)

    def test_saving_the_setting_brings_the_losses_back(self):
        from tests.test_srp_config import settings_post

        self.old_row(1, timedelta(days=20).days)
        self.client.force_login(self.admin)

        response = self.client.post(reverse("fleetops:configuration_list", args=["srp"]), settings_post(claim_deadline_days="30"))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SrpLoss.objects.filter(kind=SrpLoss.Kind.OLD).exists())


class SharedPingCooldownTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("cool_fc", perms=f.FC_PERMS)
        self.channel = f.ping_channel("Shared")

    def test_a_fleet_ping_holds_up_a_ping_without_a_fleet(self):
        operation = f.create_operation(self.fc, ping_channel=self.channel, send_ping=True, ping_text="Form up")
        operations_module.set_action(operation, "discord_ping", True)

        self.assertIn("received a ping", standalone_pings.cooldown_problem(self.channel))

    def test_a_ping_without_a_fleet_holds_up_a_fleet_ping(self):
        f.standalone_ping(sender=self.fc, channel=self.channel, status=StandalonePing.Status.SENT)
        operation = f.create_operation(self.fc, ping_channel=self.channel, send_ping=True, ping_text="Form up")

        with mock.patch(POST, return_value=OK) as post:
            action = send_ping_after_cooldown(operation, actor=self.fc, action="discord_ping")

        post.assert_not_called()
        self.assertEqual(action.status, OperationAction.Status.FAILED)
        self.assertIn("received a ping", action.error_message)

    def test_a_fleet_ping_goes_out_when_the_channel_is_quiet_and_then_holds_the_next_one(self):
        first = f.create_operation(self.fc, ping_channel=self.channel, send_ping=True, ping_text="One")
        second = f.create_operation(self.fc, ping_channel=self.channel, send_ping=True, ping_text="Two")

        with mock.patch(POST, return_value=OK) as post:
            one = send_ping_after_cooldown(first, actor=self.fc, action="discord_ping")
            two = send_ping_after_cooldown(second, actor=self.fc, action="discord_ping")

        self.assertEqual(one.status, OperationAction.Status.SUCCESS)
        self.assertEqual(two.status, OperationAction.Status.FAILED)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(ping_cooldown_problem(self.channel))
        with self.assertRaises(PingCooldown):
            retry_ping(second, actor=self.fc)


class FcCreditRoleTests(TestCase):
    def setUp(self):
        self.leader = f.create_user("credit_leader", perms=f.MEMBER_PERMS)
        self.fc = f.create_user("credit_fc", perms=f.FC_PERMS)
        self.lead = f.create_user("credit_lead", perms=f.FC_LEAD_PERMS)
        self.pilot = f.create_user("credit_pilot", perms=f.MEMBER_PERMS)
        self.group = f.aa_group("Example Credit Group", leaders=[self.leader])
        self.activity = f.fleet_type("Group Activity", "0.00")
        f.group_access(self.group, GroupAccess.Mode.FLEETS, types=[self.activity])
        self.operation = f.create_operation(
            self.leader, type_obj=self.activity, group=self.group, group_name=self.group.name
        )
        f.member_state(self.operation, f.main_of(self.pilot))
        self.credit = f.special_role("Backseat", key="backseat_fc", grants_fc_credit=True)
        self.plain = f.special_role("Scout", key="scout", grants_fc_credit=False)

    def test_a_group_leader_cannot_give_a_role_with_fc_credit(self):
        self.assertFalse(may_give_fc_credit(self.leader, self.operation))

        with self.assertRaisesMessage(ValueError, texts.SPECIAL_ROLE_FC_CREDIT_NOT_ALLOWED()):
            add_role_assignment(self.operation, actor=self.leader, role=self.credit, character_id=f.main_of(self.pilot).character_id)

    def test_a_group_leader_can_still_give_a_role_without_credit(self):
        assignment = add_role_assignment(
            self.operation, actor=self.leader, role=self.plain, character_id=f.main_of(self.pilot).character_id
        )

        self.assertFalse(assignment.grants_fc_credit)

    def test_the_form_does_not_offer_such_roles_to_a_group_leader(self):
        form = OperationRoleAssignmentForm(operation=self.operation, user=self.leader)
        offered = set(form.fields["role"].queryset.values_list("key", flat=True))
        self.assertIn("scout", offered)
        self.assertNotIn("backseat_fc", offered)

        lead_form = OperationRoleAssignmentForm(operation=self.operation, user=self.lead)
        self.assertIn("backseat_fc", set(lead_form.fields["role"].queryset.values_list("key", flat=True)))

    def test_the_fc_lead_and_the_fc_of_the_fleet_may(self):
        own = f.create_operation(self.fc)
        self.assertTrue(may_give_fc_credit(self.lead, self.operation))
        self.assertTrue(may_give_fc_credit(self.fc, own))
        self.assertFalse(may_give_fc_credit(self.fc, self.operation))

    def test_an_existing_assignment_stays(self):
        from fleetops.models import OperationRoleAssignment

        add_role_assignment(self.operation, actor=self.lead, role=self.credit, character_id=f.main_of(self.pilot).character_id)

        self.client.force_login(self.leader)
        self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))

        self.assertEqual(OperationRoleAssignment.objects.count(), 1)

    def test_the_view_refuses_the_role_for_a_group_leader(self):
        from fleetops.models import OperationRoleAssignment

        self.client.force_login(self.leader)
        response = self.client.post(
            reverse("fleetops:add_operation_role", args=[self.operation.uuid]),
            {"role": "backseat_fc", "character_id": str(f.main_of(self.pilot).character_id), "notes": ""},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OperationRoleAssignment.objects.exists())


class RecalculateMessageTests(TestCase):
    def setUp(self):
        f.srp_settings(allow_self_review=False)
        self.manager = f.create_user("recalc_manager", perms=f.SRP_MANAGER_PERMS)
        self.client.force_login(self.manager)

    def pending(self, user):
        return f.srp_claim(user)

    def test_only_own_claims_pending_is_not_a_green_zero(self):
        self.pending(self.manager)

        response = self.client.post(reverse("fleetops:srp_review_recalculate"))

        text = " ".join(messages_of(response))
        self.assertIn("No claim was recalculated", text)
        self.assertNotIn("0 pending claims recalculated", text)
        levels = [message.level_tag for message in get_messages(response.wsgi_request)]
        self.assertEqual(levels, ["warning"])

    def test_the_message_says_how_many_of_the_own_claims_were_skipped(self):
        other = f.create_user("recalc_member", perms=f.SRP_MEMBER_PERMS)
        self.pending(other)
        self.pending(self.manager)

        response = self.client.post(reverse("fleetops:srp_review_recalculate"))

        text = " ".join(messages_of(response))
        self.assertIn("1 pending claim recalculated with the current policy.", text)
        self.assertIn("1 of your own claims was skipped", text)

    def test_nothing_is_added_when_nothing_was_skipped(self):
        other = f.create_user("recalc_member2", perms=f.SRP_MEMBER_PERMS)
        self.pending(other)

        response = self.client.post(reverse("fleetops:srp_review_recalculate"))

        self.assertEqual(messages_of(response), ["1 pending claim recalculated with the current policy."])

    def test_with_self_review_allowed_the_own_claims_are_done(self):
        f.srp_settings(allow_self_review=True)
        self.pending(self.manager)

        response = self.client.post(reverse("fleetops:srp_review_recalculate"))

        self.assertEqual(messages_of(response), ["1 pending claim recalculated with the current policy."])


@tag("catalog")
class ChineseTotalRowTests(TestCase):
    def test_the_total_row_says_heji(self):
        from fleetops.services.export_datasets import SRP_TOTAL_ROW
        from fleetops.srp.reports import TOTAL

        with translation.override("zh-hans"):
            self.assertEqual(str(SRP_TOTAL_ROW), "合计")
            self.assertEqual(str(TOTAL), "合计")


class EndedFleetTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("ended_fc", perms=f.FC_PERMS)
        self.channel = f.ping_channel("Ended")
        self.client.force_login(self.fc)

    def operation(self, status):
        return f.create_operation(
            self.fc, status=status, ping_channel=self.channel, send_ping=True, ping_text="x", motd_text="y",
            ended_at=timezone.now() - timedelta(minutes=5),
        )

    def test_an_ended_fleet_shows_no_retry_buttons_and_the_views_refuse(self):
        for status in (Status.CLOSED, Status.CANCELLED):
            operation = self.operation(status)
            with self.subTest(status=status):
                page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
                self.assertNotContains(page, reverse("fleetops:retry_ping", args=[operation.uuid]))
                self.assertNotContains(page, reverse("fleetops:retry_motd", args=[operation.uuid]))
                with mock.patch(POST, return_value=OK) as post, mock.patch.object(operations_module, "set_fleet_motd") as motd:
                    ping = self.client.post(reverse("fleetops:retry_ping", args=[operation.uuid]))
                    motd_response = self.client.post(reverse("fleetops:retry_motd", args=[operation.uuid]))
                post.assert_not_called()
                motd.assert_not_called()
                self.assertEqual(ping.status_code, 302)
                self.assertEqual(motd_response.status_code, 302)
                self.assertIn("already", " ".join(messages_of(ping)))
                self.assertFalse(AuditLog.objects.filter(action__startswith="fleet.retry").exists())

    def test_a_running_fleet_keeps_the_buttons(self):
        operation = f.create_operation(self.fc, ping_channel=self.channel, send_ping=True, ping_text="x", motd_text="y")

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, reverse("fleetops:retry_ping", args=[operation.uuid]))
        self.assertContains(page, reverse("fleetops:retry_motd", args=[operation.uuid]))


class UndonePaymentNotificationTests(TestCase):
    def setUp(self):
        self.member = f.create_user("unpaid_member", perms=f.SRP_MEMBER_PERMS)
        self.payer = f.create_user("unpaid_payer", perms=f.SRP_MANAGER_PERMS)
        self.srp = f.srp_settings(notify_members=True, require_different_payer=False)

    def paid_claim(self):
        return f.srp_claim(self.member, status="paid")

    def undo(self, claim, reason="Paid to the wrong pilot"):
        with mock.patch("fleetops.srp.notify.notify") as notifier, self.captureOnCommitCallbacks(execute=True):
            review.undo_payment(claim, actor=self.payer, reason=reason)
        return notifier

    def test_the_switch_is_off_by_default(self):
        self.assertFalse(SrpSettings._meta.get_field("notify_on_unpaid").default)
        self.assertFalse(SrpSettings.get_solo().notify_on_unpaid)

    def test_off_means_no_notification(self):
        notifier = self.undo(self.paid_claim())

        notifier.warning.assert_not_called()

    def test_on_but_the_general_switch_off_means_no_notification(self):
        f.srp_settings(notify_on_unpaid=True, notify_members=False)

        notifier = self.undo(self.paid_claim())

        notifier.warning.assert_not_called()

    def test_both_on_tells_the_member_with_the_reason(self):
        f.srp_settings(notify_on_unpaid=True, notify_members=True)
        claim = self.paid_claim()

        notifier = self.undo(claim, "Paid to the wrong pilot")

        notifier.warning.assert_called_once()
        user, title, message = notifier.warning.call_args.args
        self.assertEqual(user, self.member)
        self.assertIn("SRP payment undone", title)
        self.assertIn("Reason: Paid to the wrong pilot", message)
        self.assertIn(claim.reference, message)

    def test_the_text_follows_the_language_of_the_member(self):
        f.srp_settings(notify_on_unpaid=True, notify_members=True)
        profile = self.member.profile
        claim = self.paid_claim()

        with mock.patch("fleetops.srp.notify.user_language", return_value="zh-hans"):
            notifier = self.undo(claim)

        _user, title, message = notifier.warning.call_args.args
        self.assertNotIn("SRP payment undone", title)
        self.assertNotIn("Reason:", message)
        self.assertIsNotNone(profile)

    def test_notify_claimant_ignores_unpaid_without_the_switch(self):
        claim = self.paid_claim()
        with mock.patch("fleetops.srp.notify.notify") as notifier:
            notify.notify_claimant(claim, "unpaid", "x")
        notifier.warning.assert_not_called()


class UnpaidSwitchSettingsTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("switch_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_list", args=["srp"])

    def test_the_page_offers_the_switch_off_and_says_how_it_depends_on_the_general_one(self):
        page = self.client.get(self.url)

        form = page.context["form"]
        self.assertIn("notify_on_unpaid", form.fields)
        self.assertFalse(form["notify_on_unpaid"].value())
        self.assertContains(page, "Notify members when a payment is undone")
        self.assertContains(page, "Only works while")

    def test_changing_it_is_audited(self):
        from tests.test_srp_config import settings_post

        response = self.client.post(self.url, settings_post(notify_on_unpaid="on"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SrpSettings.get_solo().notify_on_unpaid)
        entry = AuditLog.objects.filter(action="configuration.update", object_type="SrpSettings").latest("pk")
        self.assertFalse(entry.old_value["notify_on_unpaid"])
        self.assertTrue(entry.new_value["notify_on_unpaid"])
