"""The SRP link at the start of a fleet and the {{ srp_url }} message variable."""

from unittest import mock

from django.test import TestCase, override_settings

from fleetops.models import AuditLog, FleetOperation, MessageTemplate, OperationAction, SrpFleetLink
from fleetops.providers.srp import AllianceAuthBuiltinSRPProvider
from fleetops.services import operations as operations_module
from fleetops.services.messages import build_context, render_messages
from fleetops.services.operations import create_srp_at_start, retry_motd, retry_srp
from fleetops.srp.links import link_path

from . import factories as f
from .test_aasrp_provider import AaSrpInstalledMixin
from .test_integrations_roles import PatchedStartFleetMixin, action_of, fake_aa_srp_model
from .test_scheduling import SchedulingTestBase, steps

Status = FleetOperation.Status
ActionStatus = OperationAction.Status

SITE = "https://auth.example.com"


def sent_motd(stubs):
    """The MOTD the start pushed to the game."""
    return stubs["set_fleet_motd"].call_args.args[3]


def sent_ping(stubs):
    return stubs["send_discord_webhook"].call_args.args[1]


@override_settings(SITE_URL=SITE)
class StartWithFleetOpsSrpTests(PatchedStartFleetMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        f.srp_settings(enabled=True)
        f.settings(srp_provider="auto", srp_auto_create=True)

    def test_start_creates_the_link_and_stores_it_on_the_fleet(self):
        operation = self.start()

        operation.refresh_from_db()
        link = SrpFleetLink.objects.get(operation=operation)
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(
            (operation.srp_provider, operation.srp_reference, operation.srp_url, operation.srp_error),
            ("fleetops", link.code, link_path(link), ""),
        )
        step = action_of(operation, "srp_link")
        self.assertEqual((step.status, step.error_message), (ActionStatus.SUCCESS, "FleetOps SRP link created."))
        self.assertEqual((link.fc_user, link.created_by), (self.fc, None))
        self.assertTrue(link.is_open)

    def test_the_link_exists_when_the_messages_are_rendered(self):
        seen = {}
        real_render = operations_module.render_messages

        def render(operation, **kwargs):
            seen["url"] = operation.srp_url
            seen["link"] = SrpFleetLink.objects.filter(operation=operation).exists()
            seen["srp_step"] = OperationAction.objects.filter(operation=operation, action="srp_link").exists()
            return real_render(operation, **kwargs)

        with mock.patch.object(operations_module, "render_messages", side_effect=render):
            operation = self.start()

        link = SrpFleetLink.objects.get(operation=operation)
        self.assertEqual(seen, {"url": link_path(link), "link": True, "srp_step": True})

    def test_default_motd_has_the_srp_link(self):
        operation = self.start()

        link = SrpFleetLink.objects.get(operation=operation)
        url = f"{SITE}/fleetops/srp/link/{link.code}/"
        self.assertIn(f'<br>SRP: <a href="{url}">Request SRP</a>', operation.motd_text)
        self.assertEqual(sent_motd(self.stubs), operation.motd_text)

    def test_a_ping_template_can_use_srp_url(self):
        MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.PING).update(is_default=False)
        MessageTemplate.objects.create(
            name="SRP ping",
            template_type=MessageTemplate.TemplateType.PING,
            content="Form up {{ formup }}. SRP: {{ srp_url }}",
            is_default=True,
        )

        operation = self.start(ping_channel=f.ping_channel("STR"))

        link = SrpFleetLink.objects.get(operation=operation)
        self.assertEqual(sent_ping(self.stubs), f"Form up Jita. SRP: {SITE}/fleetops/srp/link/{link.code}/")

    def test_attendance_only_fleets_get_no_link(self):
        operation = self.start(mode="attendance_only")

        operation.refresh_from_db()
        self.assertFalse(SrpFleetLink.objects.exists())
        self.assertEqual(operation.srp_url, "")
        step = action_of(operation, "srp_link")
        self.assertEqual(
            (step.status, step.error_message),
            (ActionStatus.SKIPPED, "Attendance-only mode: SRP creation intentionally skipped."),
        )
        self.assertNotIn("SRP:", operation.motd_text)

    def test_automatic_creation_can_be_switched_off(self):
        f.settings(srp_auto_create=False)

        operation = self.start()

        operation.refresh_from_db()
        self.assertFalse(SrpFleetLink.objects.exists())
        step = action_of(operation, "srp_link")
        self.assertEqual((step.status, step.error_message), (ActionStatus.SKIPPED, "Automatic SRP link creation is disabled."))
        self.assertNotIn("SRP:", operation.motd_text)
        self.assertNotIn("Request SRP", sent_motd(self.stubs))

    def test_a_failing_provider_does_not_block_the_start_or_the_messages(self):
        with mock.patch.object(operations_module, "create_srp_link", side_effect=RuntimeError("database is locked")):
            with self.assertLogs(operations_module.logger, "ERROR"):
                operation = self.start()

        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.ACTIVE)
        step = action_of(operation, "srp_link")
        self.assertEqual(step.status, ActionStatus.FAILED)
        self.assertEqual(operation.srp_error, "SRP creation failed unexpectedly (RuntimeError).")
        self.assertNotIn("database is locked", operation.srp_error)
        self.assertEqual(operation.srp_url, "")
        self.assertEqual(action_of(operation, "motd_update").status, ActionStatus.SUCCESS)
        self.assertNotIn("Request SRP", operation.motd_text)
        self.assertEqual(action_of(operation, "tracking_start").status, ActionStatus.SUCCESS)

    def test_with_fleetops_srp_disabled_no_provider_means_a_skipped_step(self):
        f.srp_settings(enabled=False)

        operation = self.start()

        operation.refresh_from_db()
        step = action_of(operation, "srp_link")
        self.assertEqual(step.status, ActionStatus.SKIPPED)
        self.assertEqual(step.error_message, "No supported SRP provider is available.")
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertFalse(SrpFleetLink.objects.exists())

    def test_an_explicit_fleetops_provider_that_is_disabled_says_so(self):
        f.srp_settings(enabled=False)
        f.settings(srp_provider="fleetops")

        operation = self.start()

        step = action_of(operation, "srp_link")
        self.assertEqual((step.status, step.error_message), (ActionStatus.SKIPPED, "FleetOps SRP is not enabled."))

    def test_the_start_audit_entry_lists_the_srp_step(self):
        from fleetops.services.scheduling import record_start_steps

        operation = self.start()
        record_start_steps(operation)

        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual(entry.new_value["steps"]["srp_link"], ActionStatus.SUCCESS)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)

    def test_a_later_motd_retry_keeps_the_link(self):
        operation = self.start()
        link = SrpFleetLink.objects.get(operation=operation)
        FleetOperation.objects.filter(pk=operation.pk).update(motd_text="")
        operation.refresh_from_db()

        retry_motd(operation)

        operation.refresh_from_db()
        self.assertIn(f"{SITE}/fleetops/srp/link/{link.code}/", operation.motd_text)

    def test_the_retry_after_a_start_keeps_the_one_link(self):
        operation = self.start()

        action = retry_srp(operation)

        self.assertEqual(SrpFleetLink.objects.count(), 1)
        self.assertIn("already linked", action.error_message)


@override_settings(SITE_URL=SITE, ROOT_URLCONF="tests.test_aasrp_provider")
class StartWithOtherProvidersTests(AaSrpInstalledMixin, PatchedStartFleetMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        f.settings(srp_provider="auto", srp_auto_create=True)

    def test_aasrp_link_goes_into_the_motd(self):
        link_model = self.install_aasrp()
        f.srp_settings(enabled=False)

        operation = self.start()

        code = link_model.objects.rows[0].srp_code
        self.assertEqual(operation.srp_provider, "aasrp")
        self.assertIn(
            f'SRP: <a href="{SITE}/ship-replacement/srp-link/{code}/request-srp/">Request SRP</a>',
            operation.motd_text,
        )
        self.assertEqual(sent_motd(self.stubs), operation.motd_text)

    def test_fleetops_srp_wins_over_aasrp_when_enabled(self):
        link_model = self.install_aasrp()
        f.srp_settings(enabled=True)

        operation = self.start()

        self.assertEqual(operation.srp_provider, "fleetops")
        self.assertEqual(link_model.objects.rows, [])
        self.assertTrue(SrpFleetLink.objects.filter(operation=operation).exists())

    def test_aasrp_is_used_when_fleetops_srp_is_not_enabled(self):
        link_model = self.install_aasrp()
        f.srp_settings(enabled=False)

        operation = self.start()

        self.assertEqual(operation.srp_provider, "aasrp")
        self.assertEqual(len(link_model.objects.rows), 1)
        self.assertFalse(SrpFleetLink.objects.exists())

    def test_the_builtin_link_goes_into_the_motd(self):
        f.srp_settings(enabled=False)
        model = fake_aa_srp_model()
        with mock.patch.object(AllianceAuthBuiltinSRPProvider, "available", return_value=True), mock.patch.object(
            AllianceAuthBuiltinSRPProvider, "_model", return_value=model
        ):
            operation = self.start()

        self.assertEqual(operation.srp_provider, "allianceauth_builtin")
        self.assertIn('SRP: <a href="https://auth.example.com/srp/">Request SRP</a>', operation.motd_text)


@override_settings(SITE_URL=SITE)
class ScheduledStartTests(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        f.srp_settings(enabled=True)
        f.settings(srp_provider="auto", srp_auto_create=True)

    def start(self, operation):
        self.client.force_login(self.fc)
        return self.client.post(
            f"/fleetops/operations/{operation.uuid}/start-scheduled/",
            {"fc_character_id": str(self.fc_char.character_id), "send_ping": "on"},
        )

    def test_the_link_is_made_when_the_fleet_starts_and_goes_into_the_messages(self):
        operation = self.scheduled()
        self.assertFalse(SrpFleetLink.objects.exists())

        self.start(operation)

        operation.refresh_from_db()
        link = SrpFleetLink.objects.get(operation=operation)
        url = f"{SITE}/fleetops/srp/link/{link.code}/"
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertEqual(steps(operation)["srp_link"], ActionStatus.SUCCESS)
        self.assertIn(url, operation.motd_text)
        [put] = self.esi.put_calls
        self.assertIn(url, put["body"]["motd"])

    def test_scheduling_a_fleet_makes_no_link(self):
        response = self.post_schedule()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SrpFleetLink.objects.exists())

    def test_the_start_entry_lists_the_srp_step(self):
        operation = self.scheduled()

        self.start(operation)

        entry = AuditLog.objects.get(action="fleet.start")
        self.assertEqual(entry.new_value["steps"]["srp_link"], ActionStatus.SUCCESS)


class PapLinkFleetTests(TestCase):
    """PAP-link-only fleets get their link from the same step when the PAP-link start calls it."""

    def setUp(self):
        self.fc = f.create_user("paplink_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(
            self.fc, esi_fleet_id=None, is_manual=True, link_only=True, send_ping=False, tracking_enabled=False
        )
        f.srp_settings(enabled=True)
        f.settings(srp_provider="auto", srp_auto_create=True)

    def test_the_step_creates_the_link(self):
        create_srp_at_start(self.operation)

        self.operation.refresh_from_db()
        link = SrpFleetLink.objects.get(operation=self.operation)
        self.assertEqual((self.operation.srp_provider, self.operation.srp_reference), ("fleetops", link.code))
        self.assertEqual(action_of(self.operation, "srp_link").status, ActionStatus.SUCCESS)

    def test_the_step_respects_the_automatic_creation_switch(self):
        f.settings(srp_auto_create=False)

        create_srp_at_start(self.operation)

        self.assertFalse(SrpFleetLink.objects.exists())
        self.assertEqual(action_of(self.operation, "srp_link").status, ActionStatus.SKIPPED)

    def test_running_the_step_again_keeps_the_link(self):
        create_srp_at_start(self.operation)
        create_srp_at_start(self.operation)

        self.assertEqual(SrpFleetLink.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)

    def test_a_provider_error_is_recorded_not_raised(self):
        with mock.patch.object(operations_module, "create_srp_link", side_effect=RuntimeError("boom")):
            with self.assertLogs(operations_module.logger, "ERROR"):
                create_srp_at_start(self.operation)

        self.operation.refresh_from_db()
        self.assertEqual(action_of(self.operation, "srp_link").status, ActionStatus.FAILED)
        self.assertEqual(self.operation.srp_error, "SRP creation failed unexpectedly (RuntimeError).")


class SrpUrlVariableTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("variable_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)

    def url(self, srp_url, **settings):
        self.operation.srp_url = srp_url
        with override_settings(**settings):
            return build_context(self.operation)["srp_url"]

    def test_a_site_path_gets_the_site_address(self):
        self.assertEqual(self.url("/fleetops/srp/link/abc/", SITE_URL=SITE), f"{SITE}/fleetops/srp/link/abc/")
        self.assertEqual(self.url("/fleetops/srp/link/abc/", SITE_URL=f"{SITE}/"), f"{SITE}/fleetops/srp/link/abc/")

    def test_without_a_site_address_the_path_is_all_there_is(self):
        self.assertEqual(self.url("/fleetops/srp/link/abc/", SITE_URL=""), "/fleetops/srp/link/abc/")

    def test_a_stored_address_is_used_as_it_is(self):
        self.assertEqual(self.url("https://srp.example.org/r/1/", SITE_URL=SITE), "https://srp.example.org/r/1/")
        self.assertEqual(self.url("http://srp.example.org/r/1/", SITE_URL=SITE), "http://srp.example.org/r/1/")

    def test_nothing_else_is_a_link(self):
        for value in ("", "   ", "javascript:alert(1)", "//evil.example.com/x", "ftp://files.example.com/x", "srp/1/"):
            with self.subTest(value=value):
                self.assertEqual(self.url(value, SITE_URL=SITE), "")

    def test_a_fleet_that_is_not_saved_yet_has_no_link(self):
        unsaved = FleetOperation(fleet_type=f.fleet_type(), fc_character_name="FC", formup="Jita")

        self.assertEqual(build_context(unsaved)["srp_url"], "")

    def test_every_provider_gets_the_variable(self):
        for provider, url in (("fleetops", "/fleetops/srp/link/x/"), ("aasrp", "/ship-replacement/srp-link/Y/request-srp/")):
            with self.subTest(provider=provider):
                self.operation.srp_provider = provider
                self.assertEqual(self.url(url, SITE_URL=SITE), f"{SITE}{url}")

    def test_a_template_without_the_variable_is_unchanged(self):
        self.operation.srp_url = "/fleetops/srp/link/x/"
        MessageTemplate.objects.filter(template_type=MessageTemplate.TemplateType.MOTD).update(
            content="{{ fleet_type }} {{ fc }}"
        )

        rendered = render_messages(self.operation)

        self.assertNotIn("srp", rendered.motd.lower())

    def test_relative_link_without_site_url_is_warned_about(self):
        self.operation.srp_url = "/fleetops/srp/link/x/"
        with override_settings(SITE_URL=""):
            rendered = render_messages(self.operation)

        self.assertIn("SITE_URL is not set, so the SRP link in the messages is incomplete.", rendered.warnings)

    def test_no_warning_with_a_site_address_or_without_a_link(self):
        self.operation.srp_url = "/fleetops/srp/link/x/"
        with override_settings(SITE_URL=SITE):
            self.assertNotIn(
                "SITE_URL is not set, so the SRP link in the messages is incomplete.",
                render_messages(self.operation).warnings,
            )
        self.operation.srp_url = ""
        with override_settings(SITE_URL=""):
            self.assertNotIn(
                "SITE_URL is not set, so the SRP link in the messages is incomplete.",
                render_messages(self.operation).warnings,
            )

    def test_the_default_motd_has_the_srp_line_only_with_a_link(self):
        f.builtin_default_templates()
        with override_settings(SITE_URL=SITE):
            without = render_messages(self.operation).motd
            self.operation.srp_url = "/fleetops/srp/link/x/"
            with_link = render_messages(self.operation).motd

        self.assertNotIn("SRP:", without)
        self.assertIn(f'SRP: <a href="{SITE}/fleetops/srp/link/x/">Request SRP</a>', with_link)
