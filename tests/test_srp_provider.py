"""The FleetOps SRP provider, the fleet SRP link and the fleet page card."""

from datetime import timedelta
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, FleetOperation, OperationAction, SrpFleetLink
from fleetops.providers import srp as srp_module
from fleetops.providers.srp import (
    AUTO_PROVIDER_ORDER,
    FleetOpsSRPProvider,
    create_srp_link,
    get_registered_srp_providers,
    get_srp_provider,
)
from fleetops.services.operations import retry_srp
from fleetops.srp import links as srp_links
from fleetops.srp.links import (
    ensure_fleet_link,
    link_path,
    link_url,
    operation_srp_context,
    recent_links_for_user,
    set_link_open,
)

from . import factories as f
from .test_integrations_roles import FakeSRPProvider, IsolatedSRPRegistryMixin

Status = FleetOperation.Status


class ProviderRegistrationTests(IsolatedSRPRegistryMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fc = f.create_user("link_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)

    def test_fleetops_is_first_in_the_automatic_order(self):
        self.assertEqual(AUTO_PROVIDER_ORDER, ("fleetops", "aasrp", "allianceauth_builtin"))

    def test_available_only_while_fleetops_srp_is_enabled(self):
        provider = FleetOpsSRPProvider()

        f.srp_settings(enabled=False)
        self.assertFalse(provider.available())
        f.srp_settings(enabled=True)
        self.assertTrue(provider.available())

    def test_automatic_mode_prefers_fleetops_when_it_is_enabled(self):
        fleetops = self.register(FleetOpsSRPProvider())
        aasrp = self.register(FakeSRPProvider("aasrp"))
        builtin = self.register(FakeSRPProvider("allianceauth_builtin"))

        f.srp_settings(enabled=True)
        self.assertIs(get_srp_provider("auto"), fleetops)
        f.srp_settings(enabled=False)
        self.assertIs(get_srp_provider("auto"), aasrp)
        aasrp.is_available = False
        self.assertIs(get_srp_provider("auto"), builtin)

    def test_automatic_mode_without_fleetops_srp_keeps_the_old_order(self):
        self.register(FleetOpsSRPProvider())
        f.srp_settings(enabled=False)
        other = self.register(FakeSRPProvider("zeta_srp"))

        self.assertIs(get_srp_provider("auto"), other)

    def test_explicit_key_selects_fleetops_only_while_enabled(self):
        fleetops = self.register(FleetOpsSRPProvider())
        self.register(FakeSRPProvider("aasrp"))

        f.srp_settings(enabled=True)
        self.assertIs(get_srp_provider("fleetops"), fleetops)
        f.srp_settings(enabled=False)
        self.assertIsNone(get_srp_provider("fleetops"))

    def test_explicit_key_of_a_disabled_fleetops_srp_says_so(self):
        self.register(FleetOpsSRPProvider())
        f.srp_settings(enabled=False)

        result = create_srp_link(self.operation, "fleetops")

        self.assertFalse(result.created)
        self.assertEqual(result.provider, "fleetops")
        self.assertEqual(result.message, "FleetOps SRP is not enabled.")
        self.assertFalse(SrpFleetLink.objects.exists())

    def test_automatic_mode_creates_the_fleetops_link(self):
        self.register(FleetOpsSRPProvider())
        aasrp = self.register(FakeSRPProvider("aasrp"))
        f.srp_settings(enabled=True)

        result = create_srp_link(self.operation, "auto")

        self.assertEqual(result.provider, "fleetops")
        self.assertEqual(aasrp.calls, [])


class FleetOpsProviderTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("provider_fc", perms=f.FC_PERMS, main_name="Link Main")
        self.operation = f.create_operation(self.fc)
        self.provider = FleetOpsSRPProvider()
        f.srp_settings(enabled=True)

    def test_provider_is_registered_by_default(self):
        self.assertIsInstance(get_registered_srp_providers().get("fleetops"), FleetOpsSRPProvider)

    def test_creates_the_link_of_the_fleet(self):
        result = self.provider.create_for_operation(self.operation)

        link = SrpFleetLink.objects.get(operation=self.operation)
        self.assertTrue(result.created)
        self.assertEqual(result.provider, "fleetops")
        self.assertEqual(result.reference, link.code)
        self.assertEqual(result.url, reverse("fleetops:srp_link", args=[link.code]))
        self.assertEqual(result.message, "FleetOps SRP link created.")

    def test_a_second_call_returns_the_same_link(self):
        first = self.provider.create_for_operation(self.operation)

        second = self.provider.create_for_operation(self.operation)

        self.assertEqual(SrpFleetLink.objects.count(), 1)
        self.assertEqual((second.reference, second.url), (first.reference, first.url))
        self.assertTrue(second.created)
        self.assertEqual(second.message, "FleetOps SRP link already exists.")
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)

    def test_nothing_is_created_while_fleetops_srp_is_disabled(self):
        f.srp_settings(enabled=False)

        result = self.provider.create_for_operation(self.operation)

        self.assertFalse(result.created)
        self.assertEqual(result.message, "FleetOps SRP is not enabled.")
        self.assertFalse(SrpFleetLink.objects.exists())

    def test_the_provider_settings_choice_shows_its_state(self):
        from fleetops.forms_config import srp_provider_choices

        f.srp_settings(enabled=False)
        self.assertIn(("fleetops", "FleetOps SRP (not enabled)"), srp_provider_choices())
        f.srp_settings(enabled=True)
        self.assertIn(("fleetops", "FleetOps SRP (enabled)"), srp_provider_choices())


class RetryAndStartTests(TestCase):
    """The retry route makes a second link impossible: one link per fleet, whatever is clicked."""

    def setUp(self):
        self.fc = f.create_user("retry_fc", perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc)
        f.srp_settings(enabled=True)
        f.settings(srp_provider="auto", srp_auto_create=True)

    def test_retry_twice_leaves_one_link(self):
        first = retry_srp(self.operation)
        second = retry_srp(self.operation)

        self.operation.refresh_from_db()
        link = SrpFleetLink.objects.get()
        self.assertEqual(first.status, OperationAction.Status.SUCCESS)
        self.assertEqual(first.error_message, "FleetOps SRP link created.")
        self.assertEqual(second.status, OperationAction.Status.SUCCESS)
        self.assertIn("already linked", second.error_message)
        self.assertEqual(
            (self.operation.srp_provider, self.operation.srp_reference, self.operation.srp_url),
            ("fleetops", link.code, link_path(link)),
        )

    def test_retry_after_an_interrupted_save_reuses_the_link(self):
        retry_srp(self.operation)
        link = SrpFleetLink.objects.get()
        FleetOperation.objects.filter(pk=self.operation.pk).update(srp_provider="", srp_reference="", srp_url="")
        self.operation.refresh_from_db()

        action = retry_srp(self.operation)

        self.operation.refresh_from_db()
        self.assertEqual(SrpFleetLink.objects.count(), 1)
        self.assertEqual(action.status, OperationAction.Status.SUCCESS)
        self.assertEqual(self.operation.srp_reference, link.code)

    def test_retry_with_fleetops_srp_disabled_creates_no_link(self):
        f.srp_settings(enabled=False)

        action = retry_srp(self.operation)

        self.assertFalse(SrpFleetLink.objects.exists())
        self.assertEqual(action.status, OperationAction.Status.SKIPPED)

    def test_link_of_a_pap_link_fleet_is_created_by_the_retry_route(self):
        operation = f.create_operation(
            self.fc, esi_fleet_id=None, is_manual=True, link_only=True, send_ping=False, tracking_enabled=False
        )
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:retry_srp", args=[operation.uuid]))

        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        self.assertTrue(SrpFleetLink.objects.filter(operation=operation).exists())
        entry = AuditLog.objects.get(action="fleet.retry_srp")
        self.assertEqual(entry.object_id, str(operation.pk))


class EnsureFleetLinkTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("ensure_fc", perms=f.FC_PERMS, main_name="Snapshot Main")
        self.operation = f.create_operation(self.fc)

    def test_snapshot_of_the_fc_and_the_main(self):
        manager = f.create_user("ensure_manager", perms=f.SRP_MANAGER_PERMS)

        link, created = ensure_fleet_link(self.operation, actor=manager)

        self.assertTrue(created)
        self.assertEqual(link.fc_user, self.fc)
        self.assertEqual(link.fc_main_character_id, f.main_of(self.fc).character_id)
        self.assertEqual(link.fc_main_character_name, "Snapshot Main")
        self.assertEqual(link.created_by, manager)
        self.assertTrue(link.is_open)
        self.assertIsNone(link.closed_at)

    def test_without_a_stored_main_the_current_main_of_the_fc_is_used(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(fc_main_character_id=None, fc_main_character_name="")
        self.operation.refresh_from_db()

        link, _created = ensure_fleet_link(self.operation)

        self.assertEqual(link.fc_main_character_name, "Snapshot Main")
        self.assertIsNone(link.created_by)

    def test_code_is_random_and_url_safe(self):
        other = f.create_operation(self.fc)

        first, _ = ensure_fleet_link(self.operation)
        second, _ = ensure_fleet_link(other)

        self.assertNotEqual(first.code, second.code)
        for link in (first, second):
            self.assertRegex(link.code, r"^[A-Za-z0-9_-]{16}$")

    def test_second_call_returns_the_existing_link_without_audit(self):
        first, created_first = ensure_fleet_link(self.operation)
        second, created_second = ensure_fleet_link(self.operation)

        self.assertEqual((created_first, created_second), (True, False))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)

    def test_create_is_audited_with_the_actor_and_without_the_code(self):
        manager = f.create_user("audit_manager", perms=f.SRP_MANAGER_PERMS)

        link, _ = ensure_fleet_link(self.operation, actor=manager)

        entry = AuditLog.objects.get(action="srp.link_create")
        self.assertEqual((entry.actor, entry.object_type, entry.object_id), (manager, "SrpFleetLink", str(link.pk)))
        self.assertEqual(entry.new_value["operation_id"], self.operation.pk)
        self.assertNotIn(link.code, str(entry.new_value))

    def test_a_taken_code_is_replaced_by_a_new_one(self):
        other = f.create_operation(self.fc)
        taken = f.fleet_link(other, code="takenCODE0123456")
        codes = iter([taken.code, taken.code, "freshCODE0123456"])

        with mock.patch.object(srp_links.secrets, "token_urlsafe", side_effect=lambda _n: next(codes)):
            link, created = ensure_fleet_link(self.operation)

        self.assertTrue(created)
        self.assertEqual(link.code, "freshCODE0123456")

    def test_gives_up_when_no_free_code_is_found(self):
        taken = f.fleet_link(f.create_operation(self.fc), code="takenCODE0123456")

        with mock.patch.object(srp_links.secrets, "token_urlsafe", return_value=taken.code):
            with self.assertRaises(RuntimeError):
                ensure_fleet_link(self.operation)

        self.assertFalse(SrpFleetLink.objects.filter(operation=self.operation).exists())

    def test_a_link_created_meanwhile_is_returned(self):
        # Another request created the fleet's link after our first look and before our insert.
        racer = f.fleet_link(self.operation, code="racerCODE0123456")
        real_filter = SrpFleetLink.objects.filter
        looks = iter([SrpFleetLink.objects.none()])

        def filter_(*args, **kwargs):
            return next(looks, None) or real_filter(*args, **kwargs)

        with mock.patch.object(SrpFleetLink.objects, "filter", side_effect=filter_), mock.patch.object(
            SrpFleetLink.objects, "create", side_effect=IntegrityError("duplicate")
        ):
            link, created = ensure_fleet_link(self.operation)

        self.assertFalse(created)
        self.assertEqual(link, racer)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 0)

    def test_link_path_and_url(self):
        link, _ = ensure_fleet_link(self.operation)

        self.assertEqual(link_path(link), f"/fleetops/srp/link/{link.code}/")
        with self.settings(SITE_URL="https://auth.example.com"):
            self.assertEqual(link_url(link), f"https://auth.example.com/fleetops/srp/link/{link.code}/")


class SetLinkOpenTests(TestCase):
    def setUp(self):
        self.fc = f.create_user("toggle_fc", perms=f.FC_PERMS)
        self.manager = f.create_user("toggle_manager", perms=f.SRP_MANAGER_PERMS)
        self.link = f.fleet_link(f.create_operation(self.fc))

    def test_closing_records_who_and_when(self):
        before = timezone.now()

        link = set_link_open(self.link, actor=self.manager, is_open=False)

        link.refresh_from_db()
        self.assertFalse(link.is_open)
        self.assertEqual(link.closed_by, self.manager)
        self.assertGreaterEqual(link.closed_at, before)
        entry = AuditLog.objects.get(action="srp.link_close")
        self.assertEqual((entry.actor, entry.object_id), (self.manager, str(link.pk)))
        self.assertEqual(entry.old_value["is_open"], True)
        self.assertEqual(entry.new_value["is_open"], False)

    def test_opening_again_clears_the_close(self):
        set_link_open(self.link, actor=self.manager, is_open=False)

        link = set_link_open(self.link, actor=self.fc, is_open=True)

        link.refresh_from_db()
        self.assertTrue(link.is_open)
        self.assertIsNone(link.closed_at)
        self.assertIsNone(link.closed_by)
        entry = AuditLog.objects.get(action="srp.link_open")
        self.assertEqual(entry.actor, self.fc)
        self.assertEqual(entry.old_value["is_open"], False)
        self.assertEqual(entry.new_value, {"is_open": True, "closed_at": None})

    def test_setting_the_state_it_has_changes_and_audits_nothing(self):
        set_link_open(self.link, actor=self.manager, is_open=True)
        set_link_open(self.link, actor=self.manager, is_open=False)
        set_link_open(self.link, actor=self.manager, is_open=False)

        self.assertEqual(AuditLog.objects.filter(action="srp.link_open").count(), 0)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_close").count(), 1)

    def test_the_given_link_object_follows_the_stored_state(self):
        stale = SrpFleetLink.objects.get(pk=self.link.pk)
        set_link_open(self.link, actor=self.manager, is_open=False)

        set_link_open(stale, actor=self.manager, is_open=False)

        self.assertFalse(stale.is_open)
        self.assertEqual(AuditLog.objects.filter(action="srp.link_close").count(), 1)

    def test_view_closes_and_opens_for_managers(self):
        self.client.force_login(self.manager)

        response = self.client.post(reverse("fleetops:srp_link_close", args=[self.link.code]))

        self.assertRedirects(
            response,
            reverse("fleetops:operation_detail", args=[self.link.operation.uuid]),
            fetch_redirect_response=False,
        )
        self.link.refresh_from_db()
        self.assertFalse(self.link.is_open)
        self.client.post(reverse("fleetops:srp_link_open", args=[self.link.code]))
        self.link.refresh_from_db()
        self.assertTrue(self.link.is_open)

    def test_view_tells_when_nothing_changed(self):
        from django.contrib.messages import get_messages

        self.client.force_login(self.manager)

        response = self.client.post(reverse("fleetops:srp_link_open", args=[self.link.code]))

        self.assertIn("The SRP link was already open.", [str(m) for m in get_messages(response.wsgi_request)])
        self.assertFalse(AuditLog.objects.filter(action="srp.link_open").exists())

    def test_view_refuses_users_without_manage_srp(self):
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:srp_link_close", args=[self.link.code]))

        self.assertEqual(response.status_code, 403)
        self.link.refresh_from_db()
        self.assertTrue(self.link.is_open)

    def test_view_of_an_unknown_link_is_a_404(self):
        self.client.force_login(self.manager)

        response = self.client.post(reverse("fleetops:srp_link_close", args=["nosuchcode"]))

        self.assertEqual(response.status_code, 404)


class RecentLinksTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.fc = f.create_user("recent_fc", perms=f.FC_PERMS)
        self.pilot = f.create_user("recent_pilot", perms=f.SRP_MEMBER_PERMS)
        self.pilot_char = f.main_of(self.pilot)

    def fleet(self, days_ago=1, **extra):
        operation = f.create_operation(
            self.fc, status=Status.CLOSED, started_at=self.now - timedelta(days=days_ago), **extra
        )
        return f.fleet_link(operation)

    def test_fleets_the_pilot_flew_in_are_listed_newest_first(self):
        older = self.fleet(days_ago=5)
        newer = self.fleet(days_ago=1)
        f.member_state(older.operation, self.pilot_char, user=self.pilot)
        f.member_state(newer.operation, self.pilot_char, user=self.pilot)

        self.assertEqual(recent_links_for_user(self.pilot), [newer, older])

    def test_granted_attendance_and_pap_registration_count(self):
        attended = self.fleet(days_ago=3)
        registered = self.fleet(days_ago=2)
        f.add_attendance(attended.operation, self.pilot, self.pilot_char)
        f.registration(registered.operation, self.pilot_char, user=self.pilot)

        self.assertEqual(recent_links_for_user(self.pilot), [registered, attended])

    def test_the_fc_sees_the_fleets_they_led(self):
        led = self.fleet()

        self.assertEqual(recent_links_for_user(self.fc), [led])

    def test_fleets_of_other_pilots_are_not_listed(self):
        self.fleet()
        stranger = f.create_user("recent_stranger", perms=f.SRP_MEMBER_PERMS)

        self.assertEqual(recent_links_for_user(stranger), [])

    def test_closed_links_are_not_listed(self):
        closed = self.fleet()
        f.member_state(closed.operation, self.pilot_char, user=self.pilot)
        SrpFleetLink.objects.filter(pk=closed.pk).update(is_open=False)

        self.assertEqual(recent_links_for_user(self.pilot), [])

    def test_fleets_older_than_the_window_are_not_listed(self):
        old = self.fleet(days_ago=40)
        recent = self.fleet(days_ago=29)
        for link in (old, recent):
            f.member_state(link.operation, self.pilot_char, user=self.pilot)

        self.assertEqual(recent_links_for_user(self.pilot), [recent])
        self.assertEqual(recent_links_for_user(self.pilot, days=60), [recent, old])

    def test_limit(self):
        links = [self.fleet(days_ago=day) for day in range(1, 6)]
        for link in links:
            f.member_state(link.operation, self.pilot_char, user=self.pilot)

        self.assertEqual(recent_links_for_user(self.pilot, limit=2), links[:2])

    def test_a_fleet_with_several_reasons_is_listed_once(self):
        link = self.fleet()
        f.member_state(link.operation, self.pilot_char, user=self.pilot)
        f.add_attendance(link.operation, self.pilot, self.pilot_char)
        f.registration(link.operation, self.pilot_char, user=self.pilot)

        self.assertEqual(recent_links_for_user(self.pilot), [link])

    def test_a_constant_number_of_queries(self):
        for day in range(1, 6):
            link = self.fleet(days_ago=day)
            f.member_state(link.operation, self.pilot_char, user=self.pilot)

        with self.assertNumQueries(1):
            rows = recent_links_for_user(self.pilot)
            [row.operation.fleet_type for row in rows]


class FleetCardTests(TestCase):
    """What the SRP card on the fleet page offers each user."""

    def setUp(self):
        f.srp_settings(enabled=True)
        f.settings(srp_provider="auto", srp_auto_create=True)
        self.fc = f.create_user("card_fc", perms=[*f.FC_PERMS, "fleetops.request_srp"], main_name="Card FC")
        self.other_fc = f.create_user("card_other_fc", perms=[*f.FC_PERMS, "fleetops.request_srp"])
        self.pilot = f.create_user("card_pilot", perms=f.SRP_MEMBER_PERMS)
        self.plain = f.create_user("card_plain", perms=f.MEMBER_PERMS)
        # SRP managers open the fleets of the claims they review with view_all_fleets.
        self.manager = f.create_user("card_manager", perms=[*f.SRP_MANAGER_PERMS, "fleetops.view_all_fleets"])
        self.stats = f.create_user("card_stats", perms=[*f.MEMBER_PERMS, "fleetops.view_all_stats"])
        self.operation = f.create_operation(self.fc, status=Status.ACTIVE)
        # Members open the pages of the fleets they fly in.
        f.member_state(self.operation, f.main_of(self.pilot), user=self.pilot)

    def card(self, user, operation=None):
        return operation_srp_context(operation or self.operation, user).get("srp_card")

    def page(self, user, operation=None):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[(operation or self.operation).uuid]))

    def test_no_card_for_a_user_without_any_srp_role(self):
        f.fleet_link(self.operation)
        # The permissions of the user are read once per request, whatever the page asks.
        self.plain.has_perm("fleetops.basic_access")

        with self.assertNumQueries(0):
            self.assertEqual(operation_srp_context(self.operation, self.plain), {})

    def test_no_card_for_a_fleet_that_has_not_started(self):
        scheduled = f.create_scheduled_operation(self.fc)

        self.assertIsNone(self.card(self.fc, scheduled))

    def test_the_fc_can_create_a_missing_link(self):
        card = self.card(self.fc)

        self.assertTrue(card["can_create"])
        self.assertIsNone(card["link"])
        self.assertFalse(card["can_request"])
        page = self.page(self.fc)
        self.assertContains(page, "Create SRP link")
        self.assertContains(page, reverse("fleetops:retry_srp", args=[self.operation.uuid]))

    def test_other_users_cannot_create_a_link(self):
        for user in (self.pilot, self.other_fc, self.stats):
            with self.subTest(user=user.username):
                self.assertIsNone(self.card(user))
        self.assertNotContains(self.page(self.pilot), "Create SRP link")

    def test_no_create_button_when_srp_is_off_or_another_app_made_the_link(self):
        f.srp_settings(enabled=False)
        self.assertIsNone(self.card(self.fc))
        f.srp_settings(enabled=True)
        f.settings(srp_provider="aasrp")
        self.assertIsNone(self.card(self.fc))
        f.settings(srp_provider="auto")
        FleetOperation.objects.filter(pk=self.operation.pk).update(srp_provider="aasrp", srp_url="/ship-replacement/")
        self.operation.refresh_from_db()
        self.assertIsNone(self.card(self.fc))

    def test_no_create_button_for_an_attendance_only_fleet(self):
        operation = f.create_operation(self.fc, send_ping=False)

        self.assertIsNone(self.card(self.fc, operation))

    def test_pap_link_fleets_offer_the_button(self):
        operation = f.create_operation(self.fc, esi_fleet_id=None, is_manual=True, link_only=True, send_ping=False)

        self.assertTrue(self.card(self.fc, operation)["can_create"])

    def test_pilots_with_request_srp_get_the_request_button_of_an_open_link(self):
        link = f.fleet_link(self.operation)

        card = self.card(self.pilot)
        page = self.page(self.pilot)

        self.assertTrue(card["can_request"])
        self.assertFalse(card["can_copy"])
        self.assertFalse(card["can_toggle"])
        self.assertFalse(card["show_claims"])
        self.assertContains(page, "Request SRP for this fleet")
        self.assertContains(page, reverse("fleetops:srp_link", args=[link.code]))
        self.assertNotContains(page, "Close SRP link")

    def test_closed_link_offers_no_request_button(self):
        f.fleet_link(self.operation, is_open=False)

        card = self.card(self.pilot)

        self.assertFalse(card["can_request"])
        self.assertFalse(card["is_open"])
        self.assertContains(self.page(self.pilot), "Link closed")

    def test_the_card_says_so_when_fleetops_srp_is_not_enabled(self):
        f.fleet_link(self.operation)
        f.srp_settings(enabled=False)

        card = self.card(self.pilot)

        self.assertFalse(card["can_request"])
        self.assertContains(self.page(self.pilot), "FleetOps SRP is not enabled")

    def test_fc_and_managers_get_the_link_to_copy(self):
        link = f.fleet_link(self.operation)

        for user in (self.fc, self.manager):
            with self.subTest(user=user.username):
                card = self.card(user)
                self.assertTrue(card["can_copy"])
                self.assertEqual(card["link_path"], link_path(link))
        with self.settings(SITE_URL="https://auth.example.com"):
            page = self.page(self.fc)
        self.assertContains(page, f"https://auth.example.com/fleetops/srp/link/{link.code}/")

    def test_only_managers_get_the_toggle(self):
        link = f.fleet_link(self.operation)

        self.assertTrue(self.card(self.manager)["can_toggle"])
        self.assertFalse(self.card(self.fc)["can_toggle"])
        page = self.page(self.manager)
        self.assertContains(page, reverse("fleetops:srp_link_close", args=[link.code]))
        SrpFleetLink.objects.filter(pk=link.pk).update(is_open=False)
        self.assertContains(self.page(self.manager), reverse("fleetops:srp_link_open", args=[link.code]))

    def test_claims_are_listed_for_managers_view_all_stats_and_the_fc_of_the_fleet(self):
        link = f.fleet_link(self.operation)
        claim = f.srp_claim(self.pilot, link=link, amount=40_000_000)
        f.srp_claim(self.pilot, link=link, status="approved", amount=10_000_000)
        f.srp_claim(self.pilot, link=link, status="withdrawn")

        for user in (self.manager, self.stats, self.fc):
            with self.subTest(user=user.username):
                card = self.card(user)
                self.assertTrue(card["show_claims"])
                self.assertEqual(len(card["claims"]), 2)
                self.assertEqual(card["totals"].claims, 2)
                self.assertEqual(card["totals"].requested, 50_000_000)
        page = self.page(self.fc)
        self.assertContains(page, claim.reference)
        self.assertContains(page, reverse("fleetops:srp_claim", args=[claim.uuid]))

    def test_claims_are_hidden_from_pilots_and_other_fcs(self):
        link = f.fleet_link(self.operation)
        claim = f.srp_claim(self.pilot, link=link)

        for user in (self.pilot, self.other_fc):
            with self.subTest(user=user.username):
                card = self.card(user)
                self.assertTrue(card is None or not card["show_claims"])
        self.assertNotContains(self.page(self.pilot), claim.reference)

    def test_claims_list_is_cut_and_counts_the_rest(self):
        link = f.fleet_link(self.operation)
        for _ in range(srp_links.CARD_CLAIMS + 3):
            f.srp_claim(self.pilot, link=link)

        card = self.card(self.manager)

        self.assertEqual(len(card["claims"]), srp_links.CARD_CLAIMS)
        self.assertEqual(card["claims_hidden"], 3)

    def test_claims_of_another_fleet_do_not_show(self):
        link = f.fleet_link(self.operation)
        other = f.fleet_link(f.create_operation(self.other_fc))
        f.srp_claim(self.pilot, link=other)

        self.assertEqual(self.card(self.manager)["claims"], [])
        self.assertEqual(link.claims.count(), 0)
