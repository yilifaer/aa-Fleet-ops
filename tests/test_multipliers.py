"""PAP multipliers: the end-fleet choice, the checks on the server, the close rules and later changes."""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import EndFleetForm, ManualFleetForm
from fleetops.models import MAX_MULTIPLIER, AuditLog, FleetOperation, FleetOpsSettings, IncentivePeriod
from fleetops.services import multipliers
from fleetops.services.fleet_rules import DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS
from fleetops.services.incentives import pending_multiplier_operations
from fleetops.services.operations import end_fleet, finish_requested_end, request_fleet_end

from . import factories as f

Status = FleetOperation.Status
Source = FleetOperation.MultiplierSource


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def minutes(value, seconds=0):
    return timedelta(minutes=value, seconds=seconds)


class AllowedMultiplierTests(TestCase):
    def test_boundaries_are_strict(self):
        cases = [
            (minutes(89, 59), [1]),
            (minutes(90), [1]),
            (minutes(90, 1), [1, 2]),
            (minutes(180, 1), [1, 2, 3]),
        ]
        for duration, expected in cases:
            with self.subTest(duration=duration):
                self.assertEqual(multipliers.allowed_multipliers(duration), expected)

    def test_requirements_and_messages_follow_the_tiers(self):
        self.assertEqual(multipliers.requirements_text(), "2x > 90 min, 3x > 180 min")
        self.assertEqual(multipliers.not_allowed_message(3), "3x needs the fleet to run longer than 180 minutes.")
        self.assertEqual(multipliers.not_allowed_message(7), "7x is not a configured PAP multiplier.")

    def test_up_to_the_maximum_multiplier(self):
        f.multiplier_tiers((60, 2), (120, MAX_MULTIPLIER))

        self.assertEqual(multipliers.allowed_multipliers(minutes(121)), [1, 2, MAX_MULTIPLIER])
        form = EndFleetForm({"attendance_multiplier": str(MAX_MULTIPLIER)})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(EndFleetForm({"attendance_multiplier": str(MAX_MULTIPLIER + 1)}).is_valid())


class CloseRuleTests(TestCase):
    """How ``end_fleet`` sets the multiplier when a fleet closes."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        patcher = mock.patch("fleetops.services.operations.sync_operation")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fleet(self, ran, **extra):
        return f.create_operation(self.fc, started_at=timezone.now() - ran, **extra)

    def closed(self, operation, **kwargs):
        end_fleet(operation, actor=self.fc, **kwargs)
        operation.refresh_from_db()
        return operation

    def state(self, operation):
        return operation.attendance_multiplier, operation.multiplier_source, operation.multiplier_pending

    def test_the_fcs_allowed_choice_is_kept(self):
        operation = self.closed(self.fleet(minutes(100)), attendance_multiplier=2)

        self.assertEqual(self.state(operation), (2, Source.FC, False))

    def test_a_choice_the_duration_does_not_allow_is_lowered(self):
        operation = self.closed(self.fleet(minutes(100)), attendance_multiplier=3)

        self.assertEqual(self.state(operation), (2, Source.FC, False))

    def test_without_a_choice_a_long_fleet_closes_at_1x_with_the_choice_pending(self):
        for reason in (FleetOperation.EndReason.FLEET_GONE, FleetOperation.EndReason.BOSS_HANDOVER):
            with self.subTest(reason=reason):
                operation = self.fleet(minutes(100))

                end_fleet(operation, automatic=True, reason=reason, detail="New Boss")

                operation.refresh_from_db()
                self.assertEqual(self.state(operation), (1, Source.DEFAULT, True))

    def test_a_short_fleet_has_nothing_pending(self):
        operation = self.fleet(minutes(30))

        end_fleet(operation, automatic=True)

        operation.refresh_from_db()
        self.assertEqual(self.state(operation), (1, Source.DEFAULT, False))

    def test_the_automatic_mode_takes_the_duration_tier(self):
        f.settings(multiplier_mode="auto")
        for ran, expected in ((minutes(30), 1), (minutes(100), 2), (minutes(200), 3)):
            with self.subTest(ran=ran):
                # A posted choice plays no part.
                operation = self.closed(self.fleet(ran), attendance_multiplier=1)

                self.assertEqual(self.state(operation), (expected, Source.AUTO, False))

    def test_the_duration_ends_at_the_end_request(self):
        operation = self.fleet(minutes(89, 30))
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(operation, actor=self.fc, attendance_multiplier=2)
        # The background close runs ten minutes later, after the 90-minute mark.
        later = timezone.now() + minutes(10)

        with mock.patch("django.utils.timezone.now", return_value=later):
            finish_requested_end(FleetOperation.objects.get(pk=operation.pk))

        operation.refresh_from_db()
        self.assertEqual(self.state(operation), (1, Source.FC, False))

    def test_a_recovered_end_checks_the_requested_multiplier_again(self):
        operation = self.fleet(minutes(100))
        with mock.patch("fleetops.tasks.finish_fleet_end.delay"):
            request_fleet_end(operation, actor=self.fc, attendance_multiplier=3)

        finish_requested_end(FleetOperation.objects.get(pk=operation.pk))

        operation.refresh_from_db()
        self.assertEqual(self.state(operation), (2, Source.FC, False))

    def test_a_fleet_without_esi_fleet_closes_without_a_final_sync(self):
        operation = self.fleet(minutes(30), link_only=True)
        FleetOperation.objects.filter(pk=operation.pk).update(esi_fleet_id=None)
        operation.refresh_from_db()

        with mock.patch("fleetops.services.operations.sync_operation") as sync:
            self.closed(operation)

        sync.assert_not_called()
        self.assertEqual(operation.status, Status.CLOSED)

    def test_the_close_is_audited_with_the_multiplier_and_the_pap(self):
        operation = self.fleet(minutes(100))
        f.member_state(operation, f.main_of(self.fc), accumulated_seconds=6000)

        self.closed(operation, attendance_multiplier=2)

        entry = AuditLog.objects.get(action="fleet.end")
        self.assertEqual(entry.new_value["attendance_multiplier"], 2)
        self.assertEqual(entry.new_value["multiplier_source"], "fc")
        self.assertEqual(entry.new_value["pap"]["granted"], 1)


class EndFleetViewTests(TestCase):
    """#24: the server checks the multiplier against the fleet's duration."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(self.fc)
        patcher = mock.patch("fleetops.services.operations.sync_operation")
        patcher.start()
        self.addCleanup(patcher.stop)

    def end(self, ran, data):
        operation = f.create_operation(self.fc, started_at=timezone.now() - ran)
        response = self.client.post(reverse("fleetops:end_fleet", args=[operation.uuid]), data)
        operation.refresh_from_db()
        return operation, messages_of(response)

    def test_tampered_multiplier_beyond_the_duration_is_rejected(self):
        operation, texts = self.end(minutes(100), {"attendance_multiplier": "3"})

        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertIn("3x needs the fleet to run longer than 180 minutes. The fleet was not ended.", texts)

    def test_an_allowed_choice_ends_the_fleet(self):
        operation, texts = self.end(minutes(100), {"attendance_multiplier": "2"})

        self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 2))
        self.assertIn("Fleet ended. Attendance finalized at 2x.", texts)

    def test_a_fleet_past_a_tier_needs_a_choice(self):
        operation, texts = self.end(minutes(100), {})

        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertIn(
            "This fleet now runs longer than 90 minutes; choose the PAP multiplier. The fleet was not ended.", texts
        )

    def test_a_short_fleet_ends_at_1x_without_a_choice(self):
        operation, _texts = self.end(minutes(30), {})

        self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 1))

    def test_the_documented_rule_decides_whether_a_choice_is_needed(self):
        # End Fleet and its prompt use multipliers.needs_choice, the rule its tests describe.
        with mock.patch("fleetops.views.needs_choice", return_value=False) as rule:
            operation, _texts = self.end(minutes(100), {})

        rule.assert_called_once()
        self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 1))
        running = f.create_operation(self.fc, started_at=timezone.now() - minutes(30))
        with mock.patch("fleetops.views_pap.needs_choice", return_value=True):
            page = self.client.get(reverse("fleetops:operation_detail", args=[running.uuid]))
        self.assertEqual(page.context["end_prompt"]["mode"], "choice")

    def test_the_automatic_mode_ignores_the_posted_multiplier(self):
        f.settings(multiplier_mode="auto")

        operation, _texts = self.end(minutes(100), {"attendance_multiplier": "1"})

        self.assertEqual((operation.status, operation.attendance_multiplier), (Status.CLOSED, 2))
        self.assertEqual(operation.multiplier_source, Source.AUTO)


class EndPromptTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(self.fc)

    def page(self, ran):
        operation = f.create_operation(self.fc, started_at=timezone.now() - ran)
        return self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

    def test_past_a_tier_the_fc_picks_from_the_allowed_multipliers(self):
        page = self.page(minutes(112, 10))

        self.assertEqual(page.context["end_prompt"]["mode"], "choice")
        self.assertContains(page, "This fleet has run <strong>1 h 52 min</strong>. Multipliers above 1x need: 2x &gt; 90 min, 3x &gt; 180 min.")
        self.assertContains(page, 'type="radio" name="attendance_multiplier"', count=2)
        self.assertContains(page, 'value="2"')
        self.assertNotContains(page, 'value="3" ')
        html = page.content.decode()
        modal = html[html.index('id="endFleetModal"'):]
        modal = modal[: modal.index("</form>")]
        self.assertNotIn("checked", modal)

    def test_a_short_fleet_ends_with_a_plain_button(self):
        page = self.page(minutes(30))

        self.assertEqual(page.context["end_prompt"]["mode"], "")
        self.assertNotContains(page, 'id="endFleetModal"')
        self.assertContains(page, 'class="end-fleet-form"')

    def test_the_automatic_mode_confirms_the_multiplier(self):
        f.settings(multiplier_mode="auto")

        page = self.page(minutes(112, 10))

        self.assertContains(page, 'id="endFleetModal"')
        self.assertContains(
            page, "PAP will be finalized at <strong>2x</strong> (fleet ran 1 h 52 min, more than 90 minutes)."
        )


class LaterChangeTests(TestCase):
    """After the close the duration limits the multiplier; FC leads may override it with a reason."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.lead = f.create_user(perms=f.FC_LEAD_PERMS)
        started = timezone.now() - timedelta(hours=4)
        self.operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=started, ended_at=started + minutes(100)
        )
        f.member_state(self.operation, f.main_of(self.fc), accumulated_seconds=6000, active=False)

    def post(self, user, **data):
        self.client.force_login(user)
        response = self.client.post(reverse("fleetops:set_attendance_multiplier", args=[self.operation.uuid]), data)
        self.operation.refresh_from_db()
        return response

    def test_within_the_duration_rule(self):
        self.post(self.fc, attendance_multiplier="2")

        self.assertEqual((self.operation.attendance_multiplier, self.operation.multiplier_source), (2, Source.FC))

    def test_an_fc_cannot_go_beyond_the_duration_rule(self):
        response = self.post(self.fc, attendance_multiplier="3", reason="Structure timer")

        self.assertEqual(self.operation.attendance_multiplier, 1)
        self.assertIn(
            "The PAP multiplier was not changed. 3x needs the fleet to run longer than 180 minutes.",
            messages_of(response),
        )

    def test_an_fc_lead_overrides_with_a_reason(self):
        self.post(self.lead, attendance_multiplier="3", reason="Structure timer, fleet held the grid")

        self.assertEqual((self.operation.attendance_multiplier, self.operation.multiplier_source), (3, Source.OVERRIDE))
        entry = AuditLog.objects.get(action="attendance.multiplier")
        self.assertEqual(entry.actor, self.lead)
        self.assertEqual(entry.reason, "Structure timer, fleet held the grid")
        self.assertEqual(entry.new_value["multiplier_source"], "override")

    def test_an_override_needs_a_written_reason(self):
        for reason in ("", "ok", "    yes   "):
            with self.subTest(reason=reason):
                response = self.post(self.lead, attendance_multiplier="3", reason=reason)

                self.assertEqual(self.operation.attendance_multiplier, 1)
                self.assertTrue(any("Give a reason of at least 5 characters" in text for text in messages_of(response)))
        self.assertFalse(AuditLog.objects.filter(action="attendance.multiplier").exists())

    def test_the_fc_settles_a_pending_choice_without_attendance_rights(self):
        fc = f.create_user(perms=["fleetops.basic_access", "fleetops.start_fleet", "fleetops.manage_own_fleet"])
        FleetOperation.objects.filter(pk=self.operation.pk).update(fc_user=fc, multiplier_pending=True)

        self.post(fc, attendance_multiplier="2")

        self.assertEqual((self.operation.attendance_multiplier, self.operation.multiplier_pending), (2, False))

    def test_without_a_pending_choice_the_fc_needs_attendance_rights(self):
        fc = f.create_user(perms=["fleetops.basic_access", "fleetops.start_fleet", "fleetops.manage_own_fleet"])
        FleetOperation.objects.filter(pk=self.operation.pk).update(fc_user=fc)

        response = self.post(fc, attendance_multiplier="2")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.operation.attendance_multiplier, 1)

    def test_keep_1x_clears_the_pending_choice(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(multiplier_pending=True)

        self.post(self.fc, attendance_multiplier="1")

        self.assertEqual((self.operation.attendance_multiplier, self.operation.multiplier_pending), (1, False))

    def test_a_running_fleet_gets_its_multiplier_when_it_ends(self):
        running = f.create_operation(self.fc, started_at=timezone.now() - minutes(100))
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:set_attendance_multiplier", args=[running.uuid]), {"attendance_multiplier": "2"}
        )

        running.refresh_from_db()
        self.assertEqual(running.attendance_multiplier, 1)
        self.assertIn("The PAP multiplier is chosen when the fleet ends.", messages_of(response))

    def test_page_shows_the_pending_banner_and_the_override_field_for_leads(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(multiplier_pending=True)

        self.client.force_login(self.fc)
        page = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertContains(page, "This fleet ended automatically. Choose the PAP multiplier (currently 1x).")
        self.assertContains(page, "Keep 1x")
        self.assertContains(page, 'id="badge-multiplier-pending"')
        self.assertNotContains(page, 'name="reason"')

        self.client.force_login(self.lead)
        page = self.client.get(reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        self.assertContains(page, 'name="reason"')
        self.assertContains(page, "3x (override)")


class PendingVisibilityTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        now = timezone.now()
        self.pending = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=now - timedelta(hours=3), multiplier_pending=True
        )
        f.create_operation(self.fc, status=Status.CLOSED, started_at=now - timedelta(hours=5))

    def test_dashboard_lists_the_fcs_fleets_waiting_for_a_choice(self):
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:dashboard"))

        self.assertEqual(list(page.context["pending_multiplier_operations"]), [self.pending])
        self.assertContains(page, "Fleets waiting for your multiplier choice")
        other = f.create_user(perms=f.FC_PERMS)
        self.client.force_login(other)
        self.assertNotContains(self.client.get(reverse("fleetops:dashboard")), 'id="pending-multipliers"')

    def test_incentive_period_lists_pending_fleets(self):
        started = self.pending.started_at
        period = IncentivePeriod.objects.create(year=started.year, month=started.month)

        self.assertEqual(list(pending_multiplier_operations(period)), [self.pending])


class ManualFleetMultiplierTests(TestCase):
    def form(self, multiplier, hours):
        return ManualFleetForm(
            data={
                "fleet_type": f.fleet_type().pk,
                "formup": "Jita",
                "started_at": "2026-10-01 19:00",
                "ended_at": f"2026-10-01 {19 + hours}:00",
                "attendance_multiplier": str(multiplier),
            }
        )

    def test_the_duration_rule_applies(self):
        form = self.form(3, 2)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["attendance_multiplier"], ["3x needs the fleet to run longer than 180 minutes."])
        self.assertTrue(self.form(2, 2).is_valid())
        self.assertTrue(self.form(3, 4).is_valid())


class SettingsPageTests(TestCase):
    def setUp(self):
        self.admin = f.create_user(perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_settings")

    def test_pap_rules_are_grouped_and_saved(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'id="settings-pap-rules"')
        self.assertContains(page, 'id="settings-statistics"')
        self.assertContains(page, 'id="settings-pap-links"')
        self.assertContains(page, "PAP characters per player")

        response = self.client.post(
            self.url,
            {
                "multiplier_mode": "auto",
                "pap_minimum_minutes": "20",
                "attendance_limit": "2",
                "minimum_fleet_minutes": "15",
                "pap_link_minutes": "45",
                "tracking_interval": "60",
                "stale_threshold": "180",
                "auto_end_missing_count": "3",
                "incentive_minimum_fleets": "3",
                "data_retention_days": "365",
                "history_alliance_ids": "",
                "srp_provider": "auto",
                "fleet_live_refresh_seconds": "15",
                "bridge_excluded_group_ids": DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS,
            },
        )

        self.assertRedirects(response, reverse("fleetops:configuration_index"))
        saved = FleetOpsSettings.get_solo()
        self.assertEqual(
            (saved.multiplier_mode, saved.pap_minimum_minutes, saved.attendance_limit),
            ("auto", 20, 2),
        )
        self.assertEqual((saved.minimum_fleet_minutes, saved.pap_link_minutes), (15, 45))
        entry = AuditLog.objects.get(action="configuration.update")
        self.assertEqual(entry.new_value["multiplier_mode"], "auto")
