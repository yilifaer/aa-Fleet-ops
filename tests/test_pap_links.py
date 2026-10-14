# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""PAP links: creating and revoking them, registering characters and PAP-link fleets."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from esi import app_settings as esi_settings
from esi.exceptions import HTTPClientError, HTTPServerError
from esi.models import CallbackRedirect

from fleetops.constants import CLICK_SCOPES, FLEET_SCOPES
from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetOpsSettings,
    OperationAction,
    PapLink,
    PapRegistration,
    ReviewStatus,
    SrpFleetLink,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers.doctrines import DoctrineChoice
from fleetops.services import pap_links
from fleetops.services.operations import request_fleet_end
from fleetops.services.pap_links import (
    ALREADY_REGISTERED,
    LEGACY_FLEET,
    LINK_CLOSED,
    NO_TOKEN,
    NOT_ONLINE,
    NOT_OWNED,
    PapLinkError,
    create_link,
    link_is_valid,
    link_state,
    register_characters,
    revoke_link,
    start_link_only_fleet,
)

from . import factories as f

Status = FleetOperation.Status
JITA = 30000142
GUARDIAN = 11987


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def http_error(status):
    if status >= 500:
        return HTTPServerError(status_code=status, headers={}, data={"error": "Internal error occurred"})
    return HTTPClientError(status_code=status, headers={}, data={"error": "Forbidden"})


class FakeLocationOperation:
    """A prepared location read, like django-esi's ``EsiOperation``."""

    def __init__(self, api, route, character_id, token):
        self.api = api
        self.route = route
        self.character_id = character_id
        self.token = token

    def result(self, use_etag=True, return_response=False, force_refresh=False, use_cache=True, store_cache=True, **extra):
        self.api.calls.append((self.route, self.character_id, use_etag))
        outcome = self.api.answers.get((self.route, self.character_id), http_error(404))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeLocationAPI:
    """``esi.client.Location`` with answers per character (ESI compatibility date 2025-11-06)."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def GetCharactersCharacterIdOnline(self, *, character_id, token):
        return FakeLocationOperation(self, "online", character_id, token)

    def GetCharactersCharacterIdLocation(self, *, character_id, token):
        return FakeLocationOperation(self, "location", character_id, token)

    def GetCharactersCharacterIdShip(self, *, character_id, token):
        return FakeLocationOperation(self, "ship", character_id, token)

    def pilot(self, character, *, online=True, system_id=JITA, ship_type_id=GUARDIAN):
        cid = character.character_id
        self.answers[("online", cid)] = {"online": online, "last_login": None, "last_logout": None, "logins": 3}
        self.answers[("location", cid)] = {"solar_system_id": system_id, "station_id": None, "structure_id": None}
        self.answers[("ship", cid)] = {"ship_item_id": 1_000_000_000_001, "ship_name": "Mine", "ship_type_id": ship_type_id}

    def routes_called(self, character=None):
        return [route for route, cid, _etag in self.calls if character is None or cid == character.character_id]


class PapLinkTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.esi = FakeLocationAPI()
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Location=self.esi)))
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, names in (("item_type_names", {GUARDIAN: "Guardian"}), ("solar_system_names", {JITA: "Jita"})):
            names_patcher = mock.patch(f"fleetops.services.pap_links.{target}", return_value=names)
            names_patcher.start()
            self.addCleanup(names_patcher.stop)

        self.fc = f.create_user("fc", perms=f.FC_PERMS, main_name="FC Main")
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        self.pilot = f.create_user("pilot", perms=f.MEMBER_PERMS, main_name="Pilot Main")
        self.main = f.main_of(self.pilot)
        # An alt in another corporation and alliance: its PAP still belongs to the pilot (and their main).
        self.alt = f.add_alt(self.pilot, "Pilot Alt", corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        for character in (self.main, self.alt):
            f.add_token(self.pilot, character, scopes=CLICK_SCOPES)
            self.esi.pilot(character)
        self.operation = f.create_operation(self.fc)
        self.link = f.pap_link(self.operation)

    def register_url(self, link=None):
        return reverse("fleetops:pap_link_register", args=[(link or self.link).code])

    def post_register(self, *characters, user=None, link=None):
        self.client.force_login(user or self.pilot)
        ids = [str(getattr(character, "character_id", character)) for character in characters]
        return self.client.post(self.register_url(link), {"character_ids": ids})


class CreateAndRevokeTests(PapLinkTestCase):
    def detail(self, user, operation=None):
        self.client.force_login(user)
        return self.client.get(reverse("fleetops:operation_detail", args=[(operation or self.operation).uuid]))

    def test_new_links_stay_open_for_the_configured_time(self):
        f.settings(pap_link_minutes=45)
        now = timezone.now()

        with mock.patch("django.utils.timezone.now", return_value=now):
            link = create_link(self.operation, actor=self.fc)

        self.assertEqual(link.expires_at, now + timedelta(minutes=45))
        self.assertTrue(link.require_online)
        self.assertEqual(link.created_by, self.fc)
        self.assertGreaterEqual(len(link.code), 32)
        entry = AuditLog.objects.get(action="pap_link.create")
        self.assertEqual((entry.actor, entry.object_id), (self.fc, str(self.operation.pk)))
        self.assertEqual(entry.new_value["minutes"], 45)
        self.assertNotIn(link.code, str(entry.new_value))

    def test_fc_creates_a_link_from_the_fleet_page(self):
        self.client.force_login(self.fc)

        response = self.client.post(
            reverse("fleetops:create_pap_link", args=[self.operation.uuid]), {"minutes": "20", "require_online": "on"}
        )

        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[self.operation.uuid]))
        link = self.operation.pap_links.exclude(pk=self.link.pk).get()
        self.assertTrue(link.require_online)
        self.assertAlmostEqual((link.expires_at - link.created_at).total_seconds(), 20 * 60, delta=1)
        self.assertTrue(any(text.startswith("PAP link created; open until") for text in message_texts(response)))

    def test_require_online_is_ticked_by_default_and_can_be_unticked_per_link(self):
        page = self.detail(self.fc)
        self.assertContains(page, 'name="require_online"')
        self.assertTrue(page.context["pap_link_form"]["require_online"].value())

        self.client.post(reverse("fleetops:create_pap_link", args=[self.operation.uuid]), {"minutes": "60"})

        link = self.operation.pap_links.exclude(pk=self.link.pk).get()
        self.assertFalse(link.require_online)
        self.assertIs(AuditLog.objects.get(action="pap_link.create").new_value["require_online"], False)

    def test_links_are_created_for_active_and_closed_fleets_only(self):
        for status, allowed in (
            (Status.ACTIVE, True),
            (Status.CLOSED, True),
            (Status.ENDING, False),
            (Status.CANCELLED, False),
            (Status.ERROR, False),
        ):
            with self.subTest(status=status):
                FleetOperation.objects.filter(pk=self.operation.pk).update(status=status)
                self.operation.refresh_from_db()
                if allowed:
                    self.assertTrue(create_link(self.operation, actor=self.fc, minutes=5).pk)
                else:
                    with self.assertRaises(PapLinkError):
                        create_link(self.operation, actor=self.fc, minutes=5)

    def test_fleets_closed_before_the_upgrade_get_no_links(self):
        # They keep the PAP selection they were closed with, so a registration could never give PAP.
        legacy = f.create_operation(self.fc, status=Status.CLOSED, pap_rules=FleetOperation.PapRules.LEGACY)

        with self.assertRaisesMessage(PapLinkError, LEGACY_FLEET):
            create_link(legacy, actor=self.fc, minutes=5)
        page = self.detail(self.fc, legacy)
        self.assertIsNone(page.context["pap_link_form"])
        self.assertContains(page, 'id="pap-links-unavailable"')
        response = self.client.post(reverse("fleetops:create_pap_link", args=[legacy.uuid]), {"minutes": "20"})

        self.assertIn(LEGACY_FLEET, message_texts(response))
        self.assertFalse(legacy.pap_links.exists())

    def test_validity_outside_the_limits_is_refused(self):
        self.client.force_login(self.fc)
        url = reverse("fleetops:create_pap_link", args=[self.operation.uuid])

        for minutes in ("0", "1441", "soon"):
            with self.subTest(minutes=minutes):
                response = self.client.post(url, {"minutes": minutes, "require_online": "on"})
                self.assertTrue(any(text.startswith("PAP link not created.") for text in message_texts(response)))
        with self.assertRaises(PapLinkError):
            create_link(self.operation, actor=self.fc, minutes=1441)
        self.assertEqual(self.operation.pap_links.count(), 1)

    def test_only_the_fleets_managers_create_and_revoke_links(self):
        other_fc = f.create_user("other_fc", perms=f.FC_PERMS)
        create = reverse("fleetops:create_pap_link", args=[self.operation.uuid])
        revoke = reverse("fleetops:revoke_pap_link", args=[self.link.pk])
        for user, status in ((other_fc, 404), (self.pilot, 403)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.post(create, {"minutes": "30"}).status_code, status)
                self.assertEqual(self.client.post(revoke).status_code, status)
        self.client.force_login(self.lead)
        self.assertEqual(self.client.post(revoke).status_code, 302)

        self.assertEqual(self.operation.pap_links.count(), 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.revoked_at)

    def test_revoking_closes_the_link_once(self):
        self.client.force_login(self.fc)
        url = reverse("fleetops:revoke_pap_link", args=[self.link.pk])

        first = self.client.post(url)
        second = self.client.post(url)

        self.assertIn("PAP link revoked; it no longer accepts registrations.", message_texts(first))
        self.assertIn("This PAP link was already revoked.", message_texts(second))
        self.link.refresh_from_db()
        self.assertFalse(link_is_valid(self.link))
        self.assertEqual(link_state(self.link), "revoked")
        entry = AuditLog.objects.get(action="pap_link.revoke")
        self.assertEqual(entry.new_value["link_id"], self.link.pk)
        self.assertEqual(revoke_link(self.link, actor=self.fc).revoked_at, self.link.revoked_at)
        self.assertEqual(AuditLog.objects.filter(action="pap_link.revoke").count(), 1)

    def test_link_states(self):
        now = timezone.now()
        self.assertEqual(link_state(self.link, now), "open")
        self.assertEqual(link_state(self.link, self.link.expires_at), "expired")
        self.assertFalse(link_is_valid(self.link, self.link.expires_at))
        for status, state in ((Status.ENDING, "open"), (Status.CLOSED, "open"), (Status.CANCELLED, "closed")):
            with self.subTest(status=status):
                self.operation.status = status
                self.assertEqual(link_state(self.link, now), state)
                self.assertEqual(link_is_valid(self.link, now), state == "open")

    def test_fleet_page_card_for_managers(self):
        f.registration(self.operation, self.main, link=self.link)
        revoked = f.pap_link(self.operation, revoked_at=timezone.now())

        page = self.detail(self.fc)

        self.assertContains(page, 'id="pap-links"')
        self.assertContains(page, f"http://localhost:8000{self.register_url()}")
        self.assertContains(page, reverse("fleetops:revoke_pap_link", args=[self.link.pk]))
        self.assertNotContains(page, reverse("fleetops:revoke_pap_link", args=[revoked.pk]))
        rows = {link.pk: link for link in page.context["pap_links"]}
        self.assertEqual((rows[self.link.pk].state, rows[self.link.pk].registration_count), ("open", 1))
        self.assertEqual(rows[revoked.pk].state, "revoked")
        self.assertContains(page, reverse("fleetops:create_pap_link", args=[self.operation.uuid]))

    @override_settings(SITE_URL="https://auth.example.com")
    def test_links_are_built_from_site_url(self):
        # Behind a TLS proxy the request itself may look like plain http.
        page = self.detail(self.fc)

        self.assertContains(page, f"https://auth.example.com{self.register_url()}")
        self.assertNotContains(page, f"http://testserver{self.register_url()}")

    @override_settings(SITE_URL="")
    def test_without_site_url_links_follow_the_request(self):
        page = self.detail(self.fc)

        self.assertContains(page, f"http://testserver{self.register_url()}")

    def test_pilots_who_can_open_the_fleet_do_not_see_the_links(self):
        f.add_attendance(self.operation, self.pilot)

        page = self.detail(self.pilot)

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="pap-links"')
        self.assertNotIn(self.link.code, page.content.decode())

    def test_ending_fleets_list_their_links_without_the_create_form(self):
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.ENDING)

        page = self.detail(self.fc)

        self.assertContains(page, 'id="pap-links"')
        self.assertIsNone(page.context["pap_link_form"])
        self.assertNotContains(page, reverse("fleetops:create_pap_link", args=[self.operation.uuid]))


class RegistrationTests(PapLinkTestCase):
    def test_registering_records_ship_system_and_online_state(self):
        response = self.post_register(self.main)

        self.assertRedirects(response, self.register_url())
        registration = PapRegistration.objects.get(operation=self.operation, character_id=self.main.character_id)
        self.assertEqual(registration.link, self.link)
        self.assertEqual(registration.auth_user, self.pilot)
        self.assertEqual(registration.character_name, "Pilot Main")
        self.assertEqual((registration.ship_type_id, registration.ship_type_name), (GUARDIAN, "Guardian"))
        self.assertEqual((registration.solar_system_id, registration.solar_system_name), (JITA, "Jita"))
        self.assertIs(registration.online, True)
        self.assertIn("Pilot Main is registered for PAP in this fleet.", message_texts(response))
        self.assertEqual(self.esi.routes_called(), ["online", "location", "ship"])
        # Location reads never send an ETag, so ESI always answers with data.
        self.assertEqual({etag for _route, _cid, etag in self.esi.calls}, {False})

    def test_alts_of_other_corporations_register_for_their_owner(self):
        result = register_characters(self.link, self.pilot, [self.main.character_id, self.alt.character_id])

        self.assertEqual([name for _cid, name in result.registered], ["Pilot Main", "Pilot Alt"])
        self.assertEqual(result.rejected, [])
        # PAP and statistics follow the owner, whatever corporation the alt is in.
        self.assertEqual(
            set(PapRegistration.objects.filter(operation=self.operation).values_list("auth_user", flat=True)),
            {self.pilot.pk},
        )

    def test_links_of_fleets_closed_before_the_upgrade_register_nobody(self):
        legacy = f.create_operation(self.fc, status=Status.CLOSED, pap_rules=FleetOperation.PapRules.LEGACY)
        link = f.pap_link(legacy, require_online=False)

        response = self.post_register(self.main, link=link)

        self.assertIn(f"Pilot Main was not registered: {LINK_CLOSED}", message_texts(response))
        self.assertFalse(PapRegistration.objects.filter(operation=legacy).exists())
        self.assertEqual(link_state(link), "closed")

    def test_offline_characters_are_refused_when_the_link_requires_online(self):
        self.esi.pilot(self.alt, online=False)

        result = register_characters(self.link, self.pilot, [self.alt.character_id])

        self.assertEqual(result.rejected, [(self.alt.character_id, "Pilot Alt", NOT_ONLINE)])
        self.assertFalse(PapRegistration.objects.exists())
        self.assertEqual(self.esi.routes_called(), ["online"])

    def test_online_check_failures_refuse_with_a_short_text(self):
        self.esi.answers[("online", self.main.character_id)] = http_error(503)

        with self.assertLogs(esi_provider.logger, "WARNING"):
            response = self.post_register(self.main)

        self.assertFalse(PapRegistration.objects.exists())
        self.assertIn(
            "Pilot Main was not registered: Could not read the online status: "
            "ESI returned HTTP 503 (Internal error occurred).",
            message_texts(response),
        )

    def test_without_require_online_esi_failures_leave_the_details_blank(self):
        link = f.pap_link(self.operation, require_online=False)
        for route in ("online", "location", "ship"):
            self.esi.answers[(route, self.main.character_id)] = http_error(500)

        with self.assertLogs(esi_provider.logger, "WARNING"):
            result = register_characters(link, self.pilot, [self.main.character_id])

        self.assertEqual(result.rejected, [])
        registration = PapRegistration.objects.get(character_id=self.main.character_id)
        self.assertEqual((registration.online, registration.ship_type_id, registration.solar_system_id), (None, None, None))
        self.assertEqual((registration.ship_type_name, registration.solar_system_name), ("", ""))
        self.assertNotIn("online", self.esi.routes_called())

    def test_characters_of_other_users_are_refused(self):
        other = f.create_user("other", perms=f.MEMBER_PERMS)
        f.add_token(other, f.main_of(other), scopes=CLICK_SCOPES)

        result = register_characters(self.link, self.pilot, [f.main_of(other).character_id])

        self.assertEqual(result.rejected, [(f.main_of(other).character_id, f"Character {f.main_of(other).character_id}", NOT_OWNED)])
        self.assertFalse(PapRegistration.objects.exists())
        self.assertEqual(self.esi.calls, [])

    def test_tokens_without_a_user_or_a_scope_are_not_used(self):
        stray = f.add_alt(self.pilot, "Stray Alt")
        f.add_token(None, stray, scopes=CLICK_SCOPES)
        partial = f.add_alt(self.pilot, "Partial Alt")
        f.add_token(self.pilot, partial, scopes=CLICK_SCOPES[:2] + FLEET_SCOPES)

        result = register_characters(self.link, self.pilot, [stray.character_id, partial.character_id])

        self.assertEqual(
            result.rejected, [(stray.character_id, "Stray Alt", NO_TOKEN), (partial.character_id, "Partial Alt", NO_TOKEN)]
        )
        self.assertEqual(self.esi.calls, [])

    def test_each_character_registers_once(self):
        register_characters(self.link, self.pilot, [self.main.character_id])
        second = register_characters(f.pap_link(self.operation), self.pilot, [self.main.character_id])

        self.assertEqual(second.rejected, [(self.main.character_id, "Pilot Main", ALREADY_REGISTERED)])
        self.assertEqual(PapRegistration.objects.filter(character_id=self.main.character_id).count(), 1)

    def test_a_registration_racing_another_is_refused_by_the_database(self):
        def other_request_registers_first(token):
            f.registration(self.operation, self.main, link=self.link)
            return True

        with mock.patch("fleetops.services.pap_links.character_online", side_effect=other_request_registers_first):
            result = register_characters(self.link, self.pilot, [self.main.character_id])

        self.assertEqual(result.rejected, [(self.main.character_id, "Pilot Main", ALREADY_REGISTERED)])
        self.assertEqual(PapRegistration.objects.count(), 1)

    def test_expired_and_revoked_links_take_no_registrations(self):
        expired = f.pap_link(self.operation, expires_at=timezone.now() - timedelta(seconds=1))
        revoked = f.pap_link(self.operation, revoked_at=timezone.now())
        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CANCELLED)
        cancelled = self.link

        for link in (expired, revoked, cancelled):
            with self.subTest(link=link.pk):
                result = register_characters(link, self.pilot, [self.main.character_id])
                self.assertEqual(result.rejected, [(self.main.character_id, "Pilot Main", LINK_CLOSED)])
        self.assertFalse(PapRegistration.objects.exists())
        self.assertEqual(self.esi.calls, [])

    def test_a_link_that_expired_after_the_page_was_loaded_refuses_the_post(self):
        self.client.force_login(self.pilot)
        page = self.client.get(self.register_url())
        self.assertContains(page, "Register selected characters")

        with mock.patch("django.utils.timezone.now", return_value=self.link.expires_at + timedelta(seconds=1)):
            response = self.post_register(self.main)

        self.assertIn(f"Pilot Main was not registered: {LINK_CLOSED}", message_texts(response))
        self.assertFalse(PapRegistration.objects.exists())

    def test_closed_fleets_recalculate_their_pap_after_a_registration(self):
        with mock.patch("fleetops.services.pap_links.refresh_after_change") as refresh:
            register_characters(self.link, self.pilot, [self.main.character_id])
        refresh.assert_not_called()

        FleetOperation.objects.filter(pk=self.operation.pk).update(status=Status.CLOSED, ended_at=timezone.now())
        with mock.patch("fleetops.services.pap_links.refresh_after_change") as refresh:
            register_characters(self.link, self.pilot, [self.alt.character_id])
            register_characters(self.link, self.pilot, [self.alt.character_id])

        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args[0].pk, self.operation.pk)
        self.assertEqual(refresh.call_args.kwargs["actor"], self.pilot)

    def test_at_most_twenty_characters_per_submission(self):
        extra = f.add_alt(self.pilot, "Third Alt")
        f.add_token(self.pilot, extra, scopes=CLICK_SCOPES)
        self.esi.pilot(extra)

        with mock.patch.object(pap_links, "MAX_CHARACTERS_PER_REGISTRATION", 2):
            result = register_characters(
                self.link, self.pilot, [self.main.character_id, self.alt.character_id, extra.character_id]
            )

        self.assertEqual(len(result.registered), 2)
        self.assertEqual([(cid, name) for cid, name, _reason in result.rejected], [(extra.character_id, "Third Alt")])
        self.assertEqual(pap_links.MAX_CHARACTERS_PER_REGISTRATION, 20)

    def test_one_audit_entry_per_submission(self):
        self.esi.pilot(self.alt, online=False)

        self.post_register(self.main, self.alt, 123)

        entry = AuditLog.objects.get(action="pap.register")
        self.assertEqual((entry.actor, entry.object_id), (self.pilot, str(self.operation.pk)))
        self.assertEqual(entry.new_value["link_id"], self.link.pk)
        self.assertEqual(entry.new_value["registered"], [self.main.character_id])
        self.assertEqual(
            entry.new_value["rejected"],
            [{"character_id": self.alt.character_id, "reason": NOT_ONLINE}, {"character_id": 123, "reason": NOT_OWNED}],
        )

    def test_a_submission_without_characters_registers_nobody(self):
        response = self.post_register()

        self.assertIn("Select the characters to register.", message_texts(response))
        self.assertFalse(AuditLog.objects.filter(action="pap.register").exists())


class RegistrationPageTests(PapLinkTestCase):
    def get(self, link=None, user=None):
        self.client.force_login(user or self.pilot)
        return self.client.get(self.register_url(link))

    def test_page_lists_the_users_characters_and_their_state(self):
        no_token = f.add_alt(self.pilot, "Fresh Alt")
        f.registration(self.operation, self.alt, link=self.link)
        f.settings(attendance_limit=2)
        FleetOperation.objects.filter(pk=self.operation.pk).update(pap_character_limit=2)

        page = self.get()

        rows = {row["name"]: row for row in page.context["characters"]}
        self.assertEqual(set(rows), {"Pilot Main", "Pilot Alt", "Fresh Alt"})
        self.assertTrue(rows["Pilot Main"]["eligible"])
        self.assertTrue(rows["Pilot Main"]["is_main"])
        self.assertFalse(rows["Pilot Alt"]["eligible"])
        self.assertTrue(rows["Pilot Alt"]["registered"])
        self.assertFalse(rows["Fresh Alt"]["has_token"])
        self.assertContains(page, f'value="{self.main.character_id}"')
        self.assertNotContains(page, f'value="{self.alt.character_id}"')
        self.assertNotContains(page, f'value="{no_token.character_id}"')
        self.assertContains(page, "Login needed")
        self.assertContains(page, "The limit of 2 characters per player applies when PAP is granted.")
        self.assertContains(page, "Characters must be logged in to the game when they register.")
        self.assertContains(page, reverse("fleetops:pap_link_authorize", args=[self.link.code]))
        # The page only looks the tokens up; nothing is asked from ESI.
        self.assertEqual(self.esi.calls, [])

    def test_link_without_online_check_says_nothing_about_it(self):
        page = self.get(f.pap_link(self.operation, require_online=False))

        self.assertNotContains(page, "Characters must be logged in to the game when they register.")

    def test_closed_links_show_why_and_no_form(self):
        cases = (
            (f.pap_link(self.operation, expires_at=timezone.now() - timedelta(minutes=1)), "This PAP link expired at"),
            (f.pap_link(self.operation, revoked_at=timezone.now()), "The FC closed this PAP link"),
        )
        for link, text in cases:
            with self.subTest(text=text):
                page = self.get(link)
                self.assertContains(page, text)
                self.assertNotContains(page, "Register selected characters")
                self.assertNotContains(page, 'name="character_ids"')

    def test_pending_link_only_fleets_say_so(self):
        operation = f.create_operation(
            self.fc, esi_fleet_id=None, link_only=True, is_manual=True, review_status=ReviewStatus.PENDING
        )

        page = self.get(f.pap_link(operation))

        self.assertContains(page, "This fleet is waiting for FC lead approval")

    def test_any_pilot_with_access_can_open_a_link(self):
        outsider = f.create_user("outsider", perms=f.MEMBER_PERMS, corporation=f.OTHER_CORP)

        page = self.get(user=outsider)

        self.assertEqual(page.status_code, 200)
        self.assertEqual([row["name"] for row in page.context["characters"]], ["outsider Main"])


class AuthorizeTests(PapLinkTestCase):
    def url(self, link=None):
        return reverse("fleetops:pap_link_authorize", args=[(link or self.link).code])

    def test_unknown_codes_are_not_found(self):
        self.client.force_login(self.pilot)

        self.assertEqual(self.client.get(reverse("fleetops:pap_link_authorize", args=["nope"])).status_code, 404)

    def test_pilots_are_sent_to_eve_login_with_the_pap_link_scopes(self):
        self.client.force_login(self.pilot)

        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(esi_settings.ESI_OAUTH_LOGIN_URL))
        for scope in CLICK_SCOPES:
            self.assertIn(scope, response["Location"])
        self.assertEqual(CallbackRedirect.objects.get().url, self.url())

    def test_the_character_from_eve_login_is_registered(self):
        new_alt = f.add_alt(self.pilot, "New Alt")
        token = f.add_token(self.pilot, new_alt, scopes=CLICK_SCOPES)
        self.esi.pilot(new_alt)
        self.client.force_login(self.pilot)

        with mock.patch("esi.decorators._check_callback", return_value=token):
            response = self.client.get(self.url())

        self.assertRedirects(response, self.register_url())
        self.assertTrue(PapRegistration.objects.filter(character_id=new_alt.character_id, auth_user=self.pilot).exists())
        self.assertIn("New Alt is registered for PAP in this fleet.", message_texts(response))

    def test_a_login_with_somebody_elses_character_registers_nothing(self):
        other = f.create_user("other", perms=f.MEMBER_PERMS)
        token = f.add_token(other, f.main_of(other), scopes=CLICK_SCOPES)
        self.client.force_login(self.pilot)

        with mock.patch("esi.decorators._check_callback", return_value=token):
            response = self.client.get(self.url())

        self.assertFalse(PapRegistration.objects.exists())
        self.assertIn(
            f"Character {f.main_of(other).character_id} was not registered: {NOT_OWNED}", message_texts(response)
        )

    def login_that_returns_after(self, user, close):
        """Start the EVE login for a new alt of ``user``, run ``close``, and come back with its token."""
        new_alt = f.add_alt(user, "New Alt")
        self.esi.pilot(new_alt)
        self.client.force_login(user)
        self.client.get(self.url())
        CallbackRedirect.objects.update(token=f.add_token(user, new_alt, scopes=CLICK_SCOPES))
        close()
        return self.client.get(self.url())

    def test_a_link_that_closed_during_eve_login_takes_the_new_token_and_refuses_it(self):
        response = self.login_that_returns_after(
            self.fc, lambda: PapLink.objects.filter(pk=self.link.pk).update(revoked_at=timezone.now())
        )

        self.assertRedirects(response, self.register_url())
        self.assertIn(f"New Alt was not registered: {LINK_CLOSED}", message_texts(response))
        self.assertFalse(PapRegistration.objects.exists())
        self.assertFalse(CallbackRedirect.objects.exists())
        # The user's next ESI login goes to EVE instead of taking the PAP link token.
        response = self.client.get(reverse("fleetops:authorize_esi"))
        self.assertTrue(response["Location"].startswith(esi_settings.ESI_OAUTH_LOGIN_URL))

    def test_a_link_that_expired_during_eve_login_leaves_no_token_behind(self):
        response = self.login_that_returns_after(
            self.pilot, lambda: PapLink.objects.filter(pk=self.link.pk).update(expires_at=timezone.now())
        )

        self.assertIn(f"New Alt was not registered: {LINK_CLOSED}", message_texts(response))
        self.assertFalse(CallbackRedirect.objects.exists())

    def test_a_closed_link_leaves_another_login_alone(self):
        revoked = f.pap_link(self.operation, revoked_at=timezone.now())
        self.client.force_login(self.pilot)
        self.client.get(self.url())
        CallbackRedirect.objects.update(url="/elsewhere/", token=f.add_token(self.pilot, self.alt, scopes=CLICK_SCOPES))

        response = self.client.get(self.url(revoked))

        self.assertIn(LINK_CLOSED, message_texts(response))
        self.assertTrue(CallbackRedirect.objects.filter(url="/elsewhere/").exists())

    def test_closed_links_do_not_send_pilots_to_eve_login(self):
        revoked = f.pap_link(self.operation, revoked_at=timezone.now())
        self.client.force_login(self.pilot)

        response = self.client.get(self.url(revoked))

        self.assertRedirects(response, self.register_url(revoked))
        self.assertIn(LINK_CLOSED, message_texts(response))
        self.assertFalse(CallbackRedirect.objects.exists())


class LinkOnlyFleetTests(PapLinkTestCase):
    url = reverse("fleetops:start_link_only_fleet")

    def setUp(self):
        super().setUp()
        self.stratop = f.fleet_type("StratOp", "2.00")
        f.settings(pap_link_minutes=40, pap_minimum_minutes=15, attendance_limit=2)
        doctrines = mock.patch(
            "fleetops.forms_links.get_doctrines",
            return_value=[DoctrineChoice(source="fittings:fittings.doctrine", external_id="7", name="Muninns")],
        )
        self.get_doctrines = doctrines.start()
        self.addCleanup(doctrines.stop)
        detect = mock.patch("fleetops.services.operations.detect_character_fleet")
        self.detect = detect.start()
        self.addCleanup(detect.stop)

    def data(self, user, **extra):
        values = {
            "request_id": "6f0a3c1e-1d2b-4c5d-8e9f-0a1b2c3d4e5f",
            "fc_character_id": str(f.main_of(user).character_id),
            "fleet_type": str(self.stratop.pk),
            "doctrine_choice": "fittings:fittings.doctrine|7|Muninns",
            "formup": "Home citadel",
            "notes": "Structure bash, no fleet in game",
            "link_minutes": "90",
            "require_online": "on",
        }
        values.update(extra)
        return values

    def start(self, user, **extra):
        self.client.force_login(user)
        return self.client.post(self.url, self.data(user, **extra))

    def test_page_offers_the_users_characters_types_and_doctrines(self):
        alt = f.add_alt(self.fc, "FC Alt")
        self.client.force_login(self.fc)

        page = self.client.get(self.url)

        form = page.context["form"]
        self.assertEqual(
            [label for _value, label in form.fields["fc_character_id"].choices], ["FC Alt — ALT", "FC Main — MAIN"]
        )
        self.assertIn(alt.character_id, dict(form.fields["fc_character_id"].choices))
        self.assertIn(("fittings:fittings.doctrine|7|Muninns", "Muninns"), form.fields["doctrine_choice"].choices)
        self.assertEqual(form["link_minutes"].value(), 40)
        self.assertTrue(form["require_online"].value())
        self.get_doctrines.assert_called_with(self.fc)
        self.assertContains(page, "The fleet and its PAP count once an FC lead has approved it.")

    def test_fc_start_waits_for_approval(self):
        response = self.start(self.fc)

        operation = FleetOperation.objects.get(link_only=True)
        self.assertRedirects(response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False)
        self.assertEqual(operation.status, Status.ACTIVE)
        self.assertTrue(operation.is_manual)
        self.assertFalse(operation.tracking_enabled)
        self.assertFalse(operation.send_ping)
        self.assertIsNone(operation.esi_fleet_id)
        self.assertEqual(operation.review_status, ReviewStatus.PENDING)
        self.assertIsNone(operation.reviewed_by)
        self.assertEqual((operation.fc_user, operation.fc_character_name), (self.fc, "FC Main"))
        self.assertEqual((operation.doctrine_name, operation.doctrine_external_id), ("Muninns", "7"))
        self.assertEqual(operation.additional_message, "Structure bash, no fleet in game")
        self.assertEqual(operation.fleet_point_weight_snapshot, self.stratop.point_weight)
        # The PAP rules in force at the start stay with the fleet.
        self.assertEqual((operation.pap_minimum_minutes, operation.pap_character_limit), (15, 2))
        link = operation.pap_links.get()
        self.assertTrue(link.require_online)
        self.assertAlmostEqual((link.expires_at - link.created_at).total_seconds(), 90 * 60, delta=1)
        self.detect.assert_not_called()
        self.assertEqual(self.esi.calls, [])

    def test_steps_and_audit_of_a_link_only_start(self):
        self.start(self.fc)

        operation = FleetOperation.objects.get(link_only=True)
        steps = dict(operation.actions.values_list("action", "status"))
        self.assertEqual(steps.pop("pap_link_fleet"), OperationAction.Status.SUCCESS)
        self.assertEqual(
            steps,
            dict.fromkeys(["discord_ping", "motd_update", "tracking_start", "srp_link"], OperationAction.Status.SKIPPED),
        )
        self.assertEqual(
            operation.actions.get(action="tracking_start").error_message, "PAP-link fleet: ESI tracking not used."
        )
        # One fleet.start entry, as for every start.
        entry = AuditLog.objects.get(action="fleet.start")
        self.assertTrue(entry.new_value["link_only"])
        self.assertEqual(entry.new_value["operation_mode"], "pap_link")
        self.assertEqual(entry.new_value["review_status"], ReviewStatus.PENDING)
        self.assertEqual(entry.new_value["link_minutes"], 90)
        self.assertTrue(AuditLog.objects.filter(action="pap_link.create", object_id=str(operation.pk)).exists())

    def test_srp_step_is_skipped_without_a_provider(self):
        self.start(self.fc)

        operation = FleetOperation.objects.get(link_only=True)
        step = operation.actions.get(action="srp_link")
        self.assertEqual(step.status, OperationAction.Status.SKIPPED)
        self.assertEqual(step.error_message, "No supported SRP provider is available.")
        self.assertEqual(operation.srp_url, "")

    def test_link_only_fleet_gets_a_fleetops_srp_link(self):
        f.srp_settings(enabled=True)

        self.start(self.fc)

        operation = FleetOperation.objects.get(link_only=True)
        link = operation.srp_link
        self.assertEqual((operation.srp_provider, operation.srp_reference), ("fleetops", link.code))
        self.assertEqual(operation.srp_url, reverse("fleetops:srp_link", args=[link.code]))
        self.assertEqual(operation.srp_error, "")
        self.assertEqual(operation.actions.get(action="srp_link").status, OperationAction.Status.SUCCESS)
        self.assertEqual((link.fc_user, link.fc_main_character_name), (self.fc, "FC Main"))
        # The other steps stay skipped.
        for action in ("discord_ping", "motd_update", "tracking_start"):
            self.assertEqual(operation.actions.get(action=action).status, OperationAction.Status.SKIPPED)
        self.assertTrue(AuditLog.objects.filter(action="srp.link_create").exists())

    def test_link_only_fleet_has_no_srp_link_when_auto_create_is_off(self):
        f.srp_settings(enabled=True)
        FleetOpsSettings.objects.update(srp_auto_create=False)

        self.start(self.fc)

        operation = FleetOperation.objects.get(link_only=True)
        self.assertFalse(SrpFleetLink.objects.exists())
        step = operation.actions.get(action="srp_link")
        self.assertEqual(step.status, OperationAction.Status.SKIPPED)
        self.assertEqual(step.error_message, "Automatic SRP link creation is disabled.")

    def test_resubmitted_start_keeps_the_one_srp_link(self):
        f.srp_settings(enabled=True)

        self.start(self.fc)
        self.start(self.fc)

        self.assertEqual(FleetOperation.objects.filter(link_only=True).count(), 1)
        self.assertEqual(SrpFleetLink.objects.count(), 1)

    def test_fc_lead_start_is_approved(self):
        self.start(self.lead)

        operation = FleetOperation.objects.get(link_only=True)
        self.assertEqual(operation.review_status, ReviewStatus.APPROVED)
        self.assertEqual(operation.reviewed_by, self.lead)
        self.assertIsNotNone(operation.reviewed_at)

    def test_require_online_can_be_unticked(self):
        data = self.data(self.fc)
        del data["require_online"]
        self.client.force_login(self.fc)

        self.client.post(self.url, data)

        self.assertFalse(PapLink.objects.get(operation__link_only=True).require_online)

    def test_a_resubmitted_form_starts_one_fleet(self):
        first = self.start(self.fc)
        second = self.start(self.fc)

        self.assertEqual(FleetOperation.objects.filter(link_only=True).count(), 1)
        self.assertEqual(first["Location"], second["Location"])
        operation = FleetOperation.objects.get(link_only=True)
        self.assertEqual(start_link_only_fleet(self.fc, {"request_id": operation.start_request_id}), operation)

    def test_characters_of_other_users_cannot_lead(self):
        response = self.start(self.fc, fc_character_id=str(self.main.character_id))

        self.assertEqual(response.status_code, 200)
        self.assertIn("fc_character_id", response.context["form"].errors)
        self.assertFalse(FleetOperation.objects.filter(link_only=True).exists())
        with self.assertRaises(PermissionError):
            start_link_only_fleet(
                self.fc,
                {"fc_character_id": self.main.character_id, "fleet_type": self.stratop, "formup": "Jita"},
            )

    def test_members_cannot_start_one(self):
        self.client.force_login(self.pilot)

        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.data(self.pilot)).status_code, 403)

    def test_fleet_page_shows_the_link_and_no_fleet_control(self):
        self.start(self.fc)
        operation = FleetOperation.objects.get(link_only=True)
        self.client.force_login(self.fc)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "PAP-link fleet")
        self.assertContains(page, operation.pap_links.get().code)
        self.assertNotContains(page, 'id="fleet-control"')
        self.assertFalse(page.context["kick_enabled"])

    def test_pilots_register_and_the_fc_ends_the_fleet_without_esi(self):
        f.multiplier_tiers((90, 2), (180, 3))
        started = timezone.now() - timedelta(minutes=100)
        with mock.patch("django.utils.timezone.now", return_value=started):
            self.start(self.fc)
        operation = FleetOperation.objects.get(link_only=True)
        # The first link closed after its 90 minutes; this pilot registers on a reopened one.
        PapLink.objects.filter(operation=operation).update(expires_at=timezone.now() + timedelta(minutes=10))
        register_characters(operation.pap_links.get(), self.pilot, [self.main.character_id])

        with mock.patch("fleetops.services.tracking.get_fleet_members") as members:
            operation, requested = request_fleet_end(operation, actor=self.fc, attendance_multiplier=2)

        self.assertTrue(requested)
        members.assert_not_called()
        operation.refresh_from_db()
        self.assertEqual(operation.status, Status.CLOSED)
        self.assertEqual(operation.attendance_multiplier, 2)
        self.assertTrue(PapRegistration.objects.filter(operation=operation, auth_user=self.pilot).exists())
