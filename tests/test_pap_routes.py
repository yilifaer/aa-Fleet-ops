# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pages and helpers the PAP rules add: configuration sections, approvals, links, kicks and presets."""

from unittest import mock

from celery.exceptions import Retry
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from fleetops import tasks
from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetStartPreset,
    MultiplierTier,
    OperationRoleAssignment,
    ReviewStatus,
    SpecialRole,
)
from fleetops.services import fleet_jobs
from fleetops.templatetags.fleetops_pap import minutes

from . import factories as f


class ConfigurationSectionTests(TestCase):
    def setUp(self):
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.client.force_login(self.lead)
        self.tiers = f.multiplier_tiers((90, 2), (180, 3))

    def test_index_lists_the_new_sections(self):
        response = self.client.get(reverse("fleetops:configuration_index"))

        self.assertContains(response, "PAP Multipliers")
        self.assertContains(response, "Special Roles")
        self.assertContains(response, reverse("fleetops:configuration_list", args=["multiplier-tiers"]))
        self.assertContains(response, reverse("fleetops:configuration_list", args=["special-roles"]))

    def test_tier_list_shows_thresholds_and_multipliers(self):
        response = self.client.get(reverse("fleetops:configuration_list", args=["multiplier-tiers"]))

        self.assertContains(response, "Longer than (minutes)")
        self.assertContains(response, "<td>180</td>", html=False)

    def test_adding_a_tier_is_audited(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["multiplier-tiers"]),
            {"threshold_minutes": "240", "multiplier": "4"},
        )

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["multiplier-tiers"]))
        tier = MultiplierTier.objects.get(threshold_minutes=240)
        self.assertEqual(tier.multiplier, 4)
        entry = AuditLog.objects.get(action="configuration.create", object_type="MultiplierTier")
        self.assertEqual(entry.new_value["multiplier"], 4)

    def test_a_tier_that_does_not_rise_is_refused(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["multiplier-tiers"]),
            {"threshold_minutes": "240", "multiplier": "3"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fleets longer than 180 minutes already get 3x.")
        self.assertFalse(MultiplierTier.objects.filter(threshold_minutes=240).exists())

    def test_adding_a_special_role_gives_it_a_key(self):
        response = self.client.post(
            reverse("fleetops:configuration_add", args=["special-roles"]),
            {"name": " Fleet Scout ", "grants_fc_credit": "on", "is_active": "on", "sort_order": "5"},
        )

        self.assertRedirects(response, reverse("fleetops:configuration_list", args=["special-roles"]))
        role = SpecialRole.objects.get(name="Fleet Scout")
        self.assertEqual(role.key, "fleet_scout")
        self.assertTrue(role.grants_fc_credit)
        self.assertTrue(AuditLog.objects.filter(action="configuration.create", object_type="SpecialRole").exists())

    def test_a_role_with_assignments_is_disabled_instead_of_deleted(self):
        fc = f.create_user("fc", perms=f.FC_PERMS)
        operation = f.create_operation(fc, status=FleetOperation.Status.CLOSED)
        role = f.special_role("Fleet Scout")
        character = f.main_of(fc)
        OperationRoleAssignment.objects.create(
            operation=operation,
            role=role.key,
            special_role=role,
            character_id=character.character_id,
            character_name=character.character_name,
            auth_user=fc,
        )

        response = self.client.post(
            reverse("fleetops:configuration_delete", args=["special-roles", role.pk]), follow=True
        )

        self.assertContains(response, "cannot be deleted. Disable it instead.")
        self.assertTrue(SpecialRole.objects.filter(pk=role.pk).exists())
        self.assertFalse(AuditLog.objects.filter(action="configuration.delete").exists())

    def test_an_unused_role_can_be_deleted(self):
        role = f.special_role("Fleet Scout")

        self.client.post(reverse("fleetops:configuration_delete", args=["special-roles", role.pk]))

        self.assertFalse(SpecialRole.objects.filter(pk=role.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="configuration.delete", object_type="SpecialRole").exists())


class NavigationAndPageTests(TestCase):
    def setUp(self):
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)

    def get(self, user, url):
        self.client.force_login(user)
        return self.client.get(url)

    def test_approvers_see_the_approvals_item_with_the_waiting_count(self):
        approvals = reverse("fleetops:approval_queue")
        self.assertContains(self.get(self.lead, reverse("fleetops:dashboard")), f'href="{approvals}">Approvals</a>')

        f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, is_manual=True, review_status=ReviewStatus.PENDING
        )

        response = self.get(self.lead, reverse("fleetops:dashboard"))
        self.assertContains(response, f'href="{approvals}">Approvals (1)</a>')
        self.assertEqual(response.context["pending_approval_count"], 1)

    def test_fcs_do_not_see_the_approvals_item(self):
        response = self.get(self.fc, reverse("fleetops:dashboard"))

        self.assertNotContains(response, reverse("fleetops:approval_queue"))
        self.assertNotIn("pending_approval_count", response.context)

    def test_member_pages_include_the_live_pap_status(self):
        for name in ("dashboard", "my_statistics"):
            with self.subTest(page=name):
                response = self.get(self.member, reverse(f"fleetops:{name}"))

                self.assertTemplateUsed(response, "fleetops/partials/live_pap_status.html")

    def test_live_pap_status_fragment(self):
        response = self.get(self.member, reverse("fleetops:live_pap"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "fleetops/partials/live_pap_status.html")
        self.assertEqual(response.context["live_pap_rows"], [])

    def test_start_fleet_links_to_the_pap_link_fleet_page(self):
        response = self.get(self.fc, reverse("fleetops:start_fleet"))

        self.assertContains(response, "No ESI fleet? Start a PAP-link fleet")
        self.assertContains(response, f'href="{reverse("fleetops:start_link_only_fleet")}"')

    def test_editing_a_scheduled_fleet_has_no_pap_link_fleet_link(self):
        scheduled = f.create_scheduled_operation(self.fc, ping_channel=f.ping_channel())

        response = self.get(self.fc, reverse("fleetops:edit_scheduled_fleet", args=[scheduled.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "No ESI fleet?")

    def test_fleet_page_merges_the_context_of_its_parts(self):
        operation = f.create_operation(self.fc)
        providers = {
            "fleetops.views_pap.pap_context": "pap_part",
            "fleetops.views_records.records_context": "records_part",
            "fleetops.views_links.links_context": "links_part",
            "fleetops.views_controls.controls_context": "controls_part",
        }
        patchers = [mock.patch(target, return_value={key: True}) for target, key in providers.items()]
        mocks = [patcher.start() for patcher in patchers]
        for patcher in patchers:
            self.addCleanup(patcher.stop)

        response = self.get(self.fc, reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertEqual(response.status_code, 200)
        for provider, key in zip(mocks, providers.values()):
            provider.assert_called_once()
            self.assertEqual(provider.call_args.args[1], operation)
            self.assertIs(response.context[key], True)


class RouteObjectCheckTests(TestCase):
    def setUp(self):
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.operation = f.create_operation(self.fc)

    def as_user(self, user):
        self.client.force_login(user)
        return self.client

    def test_unknown_pap_link_codes_are_not_found(self):
        client = self.as_user(self.member)

        for name in ("pap_link_register", "pap_link_authorize"):
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(f"fleetops:{name}", args=["no-such-code"])).status_code, 404)

    def test_pap_link_page_names_the_fleet(self):
        link = f.pap_link(self.operation)

        response = self.as_user(self.member).get(reverse("fleetops:pap_link_register", args=[link.code]))

        self.assertContains(response, f"FC {self.operation.fc_character_name}")

    def test_unknown_objects_are_not_found(self):
        client = self.as_user(self.lead)
        missing = "00000000-0000-0000-0000-000000000001"
        posts = [
            reverse("fleetops:revoke_pap_link", args=[999_999]),
            reverse("fleetops:review_operation", args=[missing]),
            reverse("fleetops:recalculate_pap", args=[missing]),
            reverse("fleetops:kick_member", args=[missing, 1]),
            reverse("fleetops:preset_delete", args=[999_999]),
        ]
        for url in posts:
            with self.subTest(url=url):
                self.assertEqual(client.post(url).status_code, 404)
        self.assertEqual(client.get(reverse("fleetops:roster_import", args=[missing])).status_code, 404)

    def test_the_last_used_settings_are_not_a_preset(self):
        last_used = FleetStartPreset.objects.create(owner=self.fc, name="", is_last_used=True)

        response = self.as_user(self.fc).post(reverse("fleetops:preset_delete", args=[last_used.pk]))

        self.assertEqual(response.status_code, 404)

    def test_preset_list_shows_only_the_users_presets(self):
        FleetStartPreset.objects.create(owner=self.fc, name="Muninn")
        FleetStartPreset.objects.create(owner=self.lead, name="Eagles")

        response = self.as_user(self.fc).get(reverse("fleetops:preset_list"))

        self.assertContains(response, "Muninn")
        self.assertNotContains(response, "Eagles")

    def test_only_fleet_managers_count_an_excluded_fleet_again(self):
        url = reverse("fleetops:set_stats_mode", args=[self.operation.uuid])
        detail = reverse("fleetops:operation_detail", args=[self.operation.uuid])

        fc, lead = Client(), Client()
        fc.force_login(self.fc)
        lead.force_login(self.lead)
        self.assertRedirects(fc.post(url, {"mode": "exclude", "note": "Test fleet"}), detail)
        for mode in ("include", "auto"):
            with self.subTest(mode=mode):
                self.assertEqual(fc.post(url, {"mode": mode, "note": "Counts"}).status_code, 403)
                self.assertRedirects(lead.post(url, {"mode": mode, "note": "Counts"}), detail)

    def test_kick_preview_answers_json(self):
        response = self.as_user(self.fc).get(reverse("fleetops:kick_preview", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"systems", "selected_system_id", "fc_system_id", "members"})

    def test_kick_preview_is_get_only(self):
        response = self.as_user(self.fc).post(reverse("fleetops:kick_preview", args=[self.operation.uuid]))

        self.assertEqual(response.status_code, 405)


class MinutesFilterTests(TestCase):
    def test_time_in_fleet(self):
        cases = [(0, "0 min"), (59, "0 min"), (2220, "37 min"), (3600, "1 h 00 min"), (3900, "1 h 05 min"),
                 (6720, "1 h 52 min"), (-5, "0 min"), ("120", "2 min")]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(minutes(seconds), expected)

    def test_unknown_time_is_empty(self):
        for value in (None, "", "soon"):
            with self.subTest(value=value):
                self.assertEqual(minutes(value), "")


class KickTaskTests(TestCase):
    """Kick batches queued by an earlier version run as fleet jobs."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        self.key = f"fleetops:fleet:{self.operation.pk}:job_lock"

    def test_runs_the_batch_as_a_job_and_releases_the_lock(self):
        with mock.patch("fleetops.services.fleet_controls.run_kick_job") as batch:
            result = tasks.kick_members(self.operation.pk, [1, 2], self.fc.pk, "capsules", {"system_id": None})

        self.assertEqual(result, "done")
        job, operation, payload, actor = batch.call_args.args
        self.assertEqual((job["kind"], job["total"], operation, actor), ("kick", 2, self.operation, self.fc))
        self.assertEqual(payload, {"character_ids": [1, 2], "purpose": "capsules", "context": {"system_id": None}})
        self.assertIsNone(cache.get(self.key))

    def test_a_second_batch_waits_for_the_running_job(self):
        fleet_jobs.claim_job(self.operation, kind="move", label="Move 3 members", actor=self.fc, total=3)

        with mock.patch("fleetops.services.fleet_controls.run_kick_job") as batch:
            with self.assertRaises(Retry):
                tasks.kick_members(self.operation.pk, [1], self.fc.pk, "outside_system")

        batch.assert_not_called()
        self.assertEqual(fleet_jobs.running_job(self.operation.pk)["label"], "Move 3 members")

    def test_the_lock_is_released_when_the_batch_fails(self):
        with mock.patch("fleetops.services.fleet_controls.run_kick_job", side_effect=RuntimeError("ESI down")):
            result = tasks.kick_members(self.operation.pk, [1], self.fc.pk, "capsules")

        self.assertEqual(result, "failed")
        self.assertIsNone(cache.get(self.key))
        job = fleet_jobs.current_job(self.operation.pk)
        self.assertEqual(job["messages"][-1]["text"], "Kick 1 capsule pilot failed unexpectedly (RuntimeError).")

    def test_a_batch_of_a_deleted_fleet_is_dropped(self):
        self.assertEqual(tasks.kick_members(999_999, [1], self.fc.pk, "capsules"), "missing")
