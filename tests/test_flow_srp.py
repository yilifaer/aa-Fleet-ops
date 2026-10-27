# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""FleetOps SRP end to end through the web pages: fleet start, claims, limits, decisions, summaries, files.

A fleet starts with FleetOps SRP enabled and its link is in the MOTD and the ping. Members submit losses by
in-game link and by zKillboard URL; the same killmail is refused the second time. The payout table, the
percentage, the capsule rule, the per-loss caps and the personal and corporation monthly limits decide the
amounts (a loss counts in the EVE month it happened in), in review mode and in reject mode. SRP managers
approve, change amounts with a reason, reject, revoke and mark paid; they do not decide on their own claims
unless the setting allows it. Every kind of viewer sees only what the SRP visibility rules give them.

ESI killmails, zKillboard and Discord are in-memory fakes; nothing leaves the test process.
"""

import itertools
import re
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from allianceauth.notifications.models import Notification
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fleetops.models import (
    AuditLog,
    FleetMemberEvent,
    FleetMemberState,
    FleetOperation,
    MessageTemplate,
    OperationAction,
    SrpClaim,
    SrpClaimEvent,
    SrpCorporationLimit,
    SrpFleetLink,
    SrpKillmail,
    SrpPayoutRule,
)
from fleetops.providers import zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.srp import quota, summaries
from fleetops.srp import policy as srp_policy
from fleetops.srp import tasks as srp_tasks

from . import factories as f
from .test_export_datasets import csv_rows, workbook_rows
from .test_operations_tracking import FLEET_ID, FakeESIMixin
from .test_srp_intake import FakeResponse

UTC = dt_timezone.utc
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SITE = "https://auth.example.com"
CONTACT = "ops@example.com"

RIFTER, GUARDIAN, CAPSULE, MUNINN = 587, 11987, 670, 12015
SHIP_NAMES = {RIFTER: "Rifter", GUARDIAN: "Guardian", CAPSULE: "Capsule", MUNINN: "Muninn"}
M = 1_000_000

Status = SrpClaim.Status
Scope = SrpClaim.Scope
Event = FleetMemberEvent.EventType
ActionStatus = OperationAction.Status


def flashed(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def trimmed(rows):
    """Rows without their trailing empty cells, so a file written by one writer compares with another's."""
    result = []
    for row in rows:
        row = list(row)
        while row and row[-1] == "":
            row.pop()
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# A fleet that starts with FleetOps SRP
# ---------------------------------------------------------------------------


@override_settings(SITE_URL=SITE)
class FleetStartFlowTests(FakeESIMixin, TestCase):
    def setUp(self):
        super().setUp()
        f.builtin_default_templates()
        # The default ping has no SRP line; this site added one to its ping template.
        ping = MessageTemplate.objects.get(template_type=MessageTemplate.TemplateType.PING, name="Default Ping")
        ping.content = ping.content + "\nSRP: {{ srp_url }}"
        ping.save()
        f.srp_settings()
        f.settings(srp_provider="auto", srp_auto_create=True)
        self.fc = f.create_user("start_fc", perms=f.FC_PERMS + ["fleetops.request_srp"], main_name="Start FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.pilot = f.create_user("start_pilot", perms=f.SRP_MEMBER_PERMS, main_name="Line Pilot")
        self.fleet_type = f.fleet_type("StratOp", "1.00")
        self.channel = f.ping_channel("Pings", allow_everyone=True)
        self.esi.put_in_fleet(self.fc_char)
        self.esi.members[FLEET_ID] = [f.esi_member(self.fc_char, role="fleet_commander", wing_id=-1, squad_id=-1)]

    def start_data(self, **extra):
        data = {
            "request_id": "6f0a3c1e-1d2b-4c5d-8e9f-0a1b2c3d4e5f",
            "operation_mode": "full",
            "fleet_timing": "now",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Jita",
            "ping_channel": str(self.channel.pk),
            "ping_mentions": ["everyone"],
        }
        data.update(extra)
        return data

    def test_the_link_exists_before_the_ping_and_the_motd(self):
        self.client.force_login(self.fc)

        response = self.client.post(reverse("fleetops:start_fleet"), self.start_data())

        operation = FleetOperation.objects.get()
        self.assertRedirects(
            response, reverse("fleetops:operation_detail", args=[operation.uuid]), fetch_redirect_response=False
        )
        link = SrpFleetLink.objects.get(operation=operation)
        url = f"{SITE}{reverse('fleetops:srp_link', args=[link.code])}"
        self.assertEqual((operation.srp_provider, operation.srp_reference), ("fleetops", link.code))
        self.assertEqual((link.fc_user, link.fc_main_character_name, link.is_open), (self.fc, "Start FC", True))

        # The SRP step ran first, so both messages carry the address.
        steps = dict(operation.actions.values_list("action", "status"))
        for name in ("srp_link", "discord_ping", "motd_update"):
            self.assertEqual(steps[name], ActionStatus.SUCCESS, name)
        order = list(operation.actions.order_by("pk").values_list("action", flat=True))
        self.assertLess(order.index("srp_link"), order.index("discord_ping"))
        self.assertLess(order.index("srp_link"), order.index("motd_update"))
        self.assertIn(f'<br>SRP: <a href="{url}">Request SRP</a>', operation.motd_text)
        self.assertEqual(self.esi.put_calls[0]["body"]["motd"], operation.motd_text)
        self.assertIn(f"SRP: {url}", operation.ping_text)
        [ping] = [call.kwargs["json"] for call in self.discord_post.call_args_list]
        self.assertIn(f"SRP: {url}", ping["content"])
        self.assertEqual(AuditLog.objects.filter(action="srp.link_create").count(), 1)
        self.assertEqual(AuditLog.objects.filter(action="fleet.start").count(), 1)

        # The fleet page offers the link to the FC, and the pilot reaches the claim form with it.
        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))
        self.assertContains(page, url)
        self.client.force_login(self.pilot)
        self.assertContains(self.client.get(reverse("fleetops:srp_link", args=[link.code])), "Killmail")

    def test_the_start_form_preview_says_the_link_comes_with_the_start(self):
        self.client.force_login(self.fc)
        data = self.start_data()

        preview = self.client.post(reverse("fleetops:preview_fleet"), data).json()

        self.assertTrue(preview["ok"])
        self.assertEqual(preview["notes"], ["The SRP link is added when the fleet starts."])
        self.assertNotIn("/srp/link/", preview["motd"])
        # Not announced when no link will be made.
        f.settings(srp_auto_create=False)
        self.assertEqual(self.client.post(reverse("fleetops:preview_fleet"), data).json()["notes"], [])

    def test_a_pap_link_only_fleet_gets_a_link_and_the_fleet_page_creates_a_missing_one(self):
        self.client.force_login(self.fc)
        data = {
            "request_id": "7a1b3c1e-1d2b-4c5d-8e9f-0a1b2c3d4e5f",
            "fc_character_id": str(self.fc_char.character_id),
            "fleet_type": str(self.fleet_type.pk),
            "formup": "Home citadel",
            "link_minutes": "60",
            "require_online": "on",
        }

        self.client.post(reverse("fleetops:start_link_only_fleet"), data)

        operation = FleetOperation.objects.get(link_only=True)
        link = SrpFleetLink.objects.get(operation=operation)
        self.assertEqual(operation.srp_url, reverse("fleetops:srp_link", args=[link.code]))
        self.assertEqual(operation.actions.get(action="srp_link").status, ActionStatus.SUCCESS)
        self.assertEqual(operation.actions.get(action="discord_ping").status, ActionStatus.SKIPPED)
        # A resubmitted form keeps the one link.
        self.client.post(reverse("fleetops:start_link_only_fleet"), data)
        self.assertEqual(SrpFleetLink.objects.count(), 1)

        # With automatic creation off the fleet starts without a link; "Create SRP link" makes it, once.
        f.settings(srp_auto_create=False)
        self.client.post(
            reverse("fleetops:start_link_only_fleet"),
            {**data, "request_id": "8b1b3c1e-1d2b-4c5d-8e9f-0a1b2c3d4e5f"},
        )
        later = FleetOperation.objects.get(start_request_id="8b1b3c1e-1d2b-4c5d-8e9f-0a1b2c3d4e5f")
        self.assertFalse(SrpFleetLink.objects.filter(operation=later).exists())
        page = self.client.get(reverse("fleetops:operation_detail", args=[later.uuid]))
        self.assertContains(page, "Create SRP link")
        self.assertContains(page, reverse("fleetops:retry_srp", args=[later.uuid]))
        for _ in range(2):
            self.client.post(reverse("fleetops:retry_srp", args=[later.uuid]))
        link = SrpFleetLink.objects.get(operation=later)
        later.refresh_from_db()
        self.assertEqual(later.srp_reference, link.code)
        self.assertEqual(SrpFleetLink.objects.count(), 2)
        self.assertTrue(link.is_open)


# ---------------------------------------------------------------------------
# Claims, limits and decisions
# ---------------------------------------------------------------------------


@override_settings(SITE_URL=SITE, ESI_USER_CONTACT_EMAIL=CONTACT)
class SrpFlowBase(TestCase):
    """A site on 14 October 2026 12:00 EVE with one running fleet and the people around it."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        for target, kwargs in (
            ("django.utils.timezone.now", {"return_value": NOW}),
            ("fleetops.srp.killmails.get_killmail", {"side_effect": self.esi_killmail}),
            ("fleetops.providers.zkillboard.requests.get", {"side_effect": self.zkill_answer}),
            ("fleetops.srp.killmails.names.entity_names", {"side_effect": self.entity_names}),
            ("fleetops.srp.tasks.time.sleep", {}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.kills = {}
        self.zkill_requests = []
        self.ids = itertools.count(140_000_001)

        f.srp_settings(percentage=Decimal("80"))
        f.settings(srp_auto_create=True)

        # People: two corporations, an FC with a fleet, SRP managers, viewers.
        self.fc = f.create_user("fc", perms=f.FC_PERMS + ["fleetops.request_srp"], main_name="Flow FC")
        self.fc2 = f.create_user("fc2", perms=f.FC_PERMS + ["fleetops.request_srp"], main_name="Other FC")
        self.p1 = f.create_user("p1", perms=f.SRP_MEMBER_PERMS, main_name="Alpha One")
        self.p2 = f.create_user("p2", perms=f.SRP_MEMBER_PERMS, main_name="Alpha Two")
        self.q1 = f.create_user("q1", perms=f.SRP_MEMBER_PERMS, main_name="Beta One", corporation=f.OTHER_CORP)
        self.mgr = f.create_user("mgr", perms=f.SRP_MANAGER_PERMS, main_name="SRP Manager")
        self.mgr2 = f.create_user("mgr2", perms=f.SRP_MANAGER_PERMS, main_name="SRP Manager Two")
        self.corpmgr = f.create_user(
            "corpmgr", perms=f.CORP_MANAGEMENT_PERMS + ["fleetops.request_srp"], main_name="Alpha Manager"
        )
        self.statsall = f.create_user("statsall", perms=f.MEMBER_PERMS + ["fleetops.view_all_stats"], main_name="Stats Viewer")
        self.nosrp = f.create_user("nosrp", perms=f.MEMBER_PERMS, main_name="No SRP Pilot")

        self.fleet = f.create_operation(
            self.fc, started_at=NOW - timedelta(hours=3), doctrine_name="Muninns", status=FleetOperation.Status.ACTIVE
        )
        self.link = f.fleet_link(self.fleet)
        self.other_fleet = f.create_operation(self.fc2, started_at=NOW - timedelta(hours=3))
        self.other_link = f.fleet_link(self.other_fleet)
        # p1 was in the fleet from the start; p2 never was; q1 joined 5 minutes before the loss time used below.
        self.join(self.fleet, self.p1, NOW - timedelta(hours=2, minutes=50))
        self.join(self.fleet, self.q1, NOW - timedelta(hours=2, minutes=50))

    # Fakes of the outside world

    def entity_names(self, ids):
        return {pk: SHIP_NAMES.get(pk, "Amamake" if pk == f.SRP_SYSTEM[0] else f"Type {pk}") for pk in ids}

    def esi_killmail(self, killmail_id, killmail_hash):
        kill = self.kills.get(killmail_id)
        if kill is None or kill.hash != killmail_hash:
            raise FleetESIError("KILLMAIL_NOT_FOUND", "Killmail not found. Check the link or the hash.", 404)
        return kill.payload

    def zkill_answer(self, url, **kwargs):
        self.zkill_requests.append((url, kwargs))
        killmail_id = int(re.search(r"killID/(\d+)/", url).group(1))
        kill = self.kills.get(killmail_id)
        if kill is None or kill.hidden:
            return FakeResponse(200, [])
        return FakeResponse(
            200, f.zkill_payload(killmail_id, killmail_hash=kill.hash, fitted=kill.fitted, total=kill.total)
        )

    # Building the scene

    def join(self, operation, user, when):
        character = f.main_of(user)
        if not FleetMemberState.objects.filter(operation=operation, character_id=character.character_id).exists():
            f.member_state(operation, character, user=user, first_seen=when)
        event = FleetMemberEvent.objects.create(
            operation=operation,
            character_id=f.main_of(user).character_id,
            character_name=f.main_of(user).character_name,
            event_type=Event.JOIN,
        )
        FleetMemberEvent.objects.filter(pk=event.pk).update(created_at=when)

    def loss(self, who, *, ship=RIFTER, ago=timedelta(hours=1), at=None, fitted=10_000_000, total=None, hidden=False):
        """A killmail ESI and zKillboard know, as the pasted texts a member would have."""
        character = who if isinstance(who, EveCharacter) else f.main_of(who)
        killmail_id, killmail_hash = next(self.ids), secrets.token_hex(20)
        when = at or NOW - ago
        payload = f.esi_killmail_payload(killmail_id, victim=character, ship_type_id=ship, time=when)
        self.kills[killmail_id] = SimpleNamespace(
            hash=killmail_hash, payload=payload, fitted=fitted, total=total, hidden=hidden
        )
        return SimpleNamespace(
            id=killmail_id,
            hash=killmail_hash,
            ingame=f"<url=killReport:{killmail_id}:{killmail_hash}>Kill: {character.character_name}</url>",
            zkill=f"https://zkillboard.com/kill/{killmail_id}/",
            time=when,
        )

    # Member steps

    def post(self, *args, **kwargs):
        """A POST that also runs what the views queue for after the commit (the notifications)."""
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(*args, **kwargs)

    def check(self, user, text, link=None):
        self.client.force_login(user)
        cache.delete(zkillboard.SLOT_KEY)
        url = reverse("fleetops:srp_link", args=[link.code]) if link else reverse("fleetops:srp_check")
        response = self.post(url, {"killmail": text})
        self.assertEqual(response.status_code, 200)
        return response

    def preview(self, user, text, link=None):
        response = self.check(user, text, link)
        self.assertIn("preview", response.context, response.context["claim_form"].errors if "claim_form" in response.context else "")
        return response.context["preview"]

    def submit(self, user, text, link=None, comment=""):
        preview = self.preview(user, text, link)
        self.assertTrue(preview.eligible, preview.blockers)
        response = self.post(reverse("fleetops:srp_submit"), {"token": preview.token, "comment": comment})
        claim = SrpClaim.objects.get(killmail_id=preview.killmail.pk)
        self.assertRedirects(
            response, reverse("fleetops:srp_claim", args=[claim.uuid]), fetch_redirect_response=False
        )
        return claim

    # Reviewer steps

    def act(self, user, claim, action, data=None, expect=302):
        self.client.force_login(user)
        response = self.post(reverse("fleetops:srp_claim_action", args=[claim.uuid, action]), data or {})
        self.assertEqual(response.status_code, expect, (action, response.content[:300]))
        claim.refresh_from_db()
        return response

    def approve(self, user, claim, **data):
        data.setdefault("mode", "suggested")
        return self.act(user, claim, "approve", data, expect=data.pop("expect", 302))

    def page(self, user, route, *args, expect=200, **params):
        self.client.force_login(user)
        response = self.client.get(reverse(f"fleetops:{route}", args=args), params)
        self.assertEqual(response.status_code, expect, f"{route} {args} {params}")
        return response

    def claim_page(self, user, claim, expect=200):
        return self.page(user, "srp_claim", claim.uuid, expect=expect)

    def rule(self, ship, amount, **extra):
        return f.payout_rule(ship, amount, ship_name=SHIP_NAMES.get(ship), **extra)


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


class SubmitFlowTests(SrpFlowBase):
    def test_an_in_fleet_loss_by_in_game_link_and_another_by_zkillboard_url(self):
        first = self.loss(self.p1, fitted=10234567.89)
        second = self.loss(self.p1, ago=timedelta(minutes=90))

        # The member checks the loss on the fleet's page; the preview shows what will be paid.
        preview = self.preview(self.p1, first.ingame, self.link)
        self.assertEqual((preview.scope, preview.blockers, preview.eligible), (Scope.IN_FLEET, [], True))
        self.assertEqual(preview.presence.code, "in_fleet")
        self.assertEqual(preview.calculation.basis, "percentage")
        self.assertEqual(preview.calculation.loss_value, Decimal("10234567.89"))
        # 80 % of the fitted value, rounded down to whole ISK.
        self.assertEqual((preview.calculation.base, preview.calculation.suggested), (8_187_654, 8_187_654))
        self.assertEqual(SrpClaim.objects.count(), 0)

        claim = self.submit(self.p1, first.ingame, self.link, comment="Lost to a gate camp")
        self.assertEqual((claim.status, claim.scope, claim.link), (Status.PENDING, Scope.IN_FLEET, self.link))
        self.assertEqual(
            (claim.user, claim.character_id, claim.main_character_name, claim.corporation_id),
            (self.p1, f.main_of(self.p1).character_id, "Alpha One", f.DEFAULT_CORP[0]),
        )
        self.assertEqual((claim.fc_user, claim.fc_main_character_name, claim.operation), (self.fc, "Flow FC", self.fleet))
        self.assertEqual(claim.fleet_label.split(" — ")[1], "Flow FC")
        self.assertEqual(
            (claim.payout_basis, claim.value_source, claim.loss_value, claim.suggested_amount, claim.limited_amount),
            ("percentage", "zkill_fitted", Decimal("10234567.89"), 8_187_654, 8_187_654),
        )
        self.assertEqual((claim.fleet_check, claim.member_comment), ("in_fleet", "Lost to a gate camp"))
        self.assertEqual(claim.killmail.ship_type_name, "Rifter")
        self.assertEqual(claim.killmail.zkill_status, SrpKillmail.ZkillStatus.OK)
        self.assertEqual(claim.killmail.killmail_hash, first.hash)
        self.assertEqual(claim.events.get().action, "submit")
        self.assertEqual(AuditLog.objects.filter(action="srp.claim_submit", object_id=str(claim.pk)).count(), 1)

        # A killboard link finds the hash on zKillboard and the value in the same answer.
        self.zkill_requests.clear()
        by_url = self.submit(self.p1, second.zkill, self.link)
        self.assertEqual(by_url.killmail.killmail_hash, second.hash)
        self.assertEqual((by_url.suggested_amount, by_url.loss_value), (8 * M, Decimal("10000000.00")))
        [(url, kwargs)] = self.zkill_requests
        self.assertEqual(url, f"https://zkillboard.com/api/killID/{second.id}/")
        self.assertNotIn("?", url)
        self.assertTrue(kwargs["headers"]["User-Agent"].startswith("AA-FleetOps/"))
        self.assertIn(CONTACT, kwargs["headers"]["User-Agent"])

        # The member's page lists both claims.
        home = self.page(self.p1, "srp_home")
        self.assertEqual(list(home.context["claims"]), [claim, by_url] if claim.killmail_time > by_url.killmail_time else [by_url, claim])
        self.assertEqual(home.context["totals"].claims, 2)

    def test_the_same_killmail_cannot_be_claimed_twice(self):
        kill = self.loss(self.p1)
        claim = self.submit(self.p1, kill.ingame, self.link)

        # The killboard link of the claimed loss is the same killmail.
        again = self.preview(self.p1, kill.zkill, self.link)
        self.assertFalse(again.eligible)
        self.assertEqual(again.token, "")
        self.assertEqual(again.existing_claim, claim)
        self.assertEqual(again.blockers, ["You already submitted this loss."])

        # So is the in-game link on the general page, and a second submit of a preview.
        general = self.preview(self.p1, kill.ingame)
        self.assertEqual((general.existing_claim, general.token), (claim, ""))
        token = self.preview(self.p1, self.loss(self.p1).ingame, self.link).token
        self.post(reverse("fleetops:srp_submit"), {"token": token})
        response = self.post(reverse("fleetops:srp_submit"), {"token": token})
        self.assertEqual(response.status_code, 302)
        self.assertIn("You already submitted this loss.", flashed(response))
        self.assertEqual(SrpClaim.objects.count(), 2)

        # Another pilot gets no hint of who has it.
        self.client.force_login(self.p2)
        other = self.post(reverse("fleetops:srp_check"), {"killmail": kill.zkill}).context["preview"]
        self.assertEqual(other.blockers, ["This loss was already claimed by another pilot."])
        self.claim_page(self.p2, claim, expect=404)

    def test_a_loss_of_another_pilot_a_wrong_hash_and_a_loss_without_pilot_are_refused(self):
        other = self.loss(self.p2)
        preview = self.preview(self.p1, other.ingame, self.link)
        self.assertEqual((preview.eligible, preview.token), (False, ""))
        self.assertEqual(preview.blockers, ["This character is not on your Alliance Auth account."])

        wrong = f"killReport:{other.id}:{'a' * 40}"
        response = self.check(self.p1, wrong)
        self.assertIn("Killmail not found. Check the link or the hash.", response.context["claim_form"].non_field_errors())

        structure = self.loss(self.p1)
        self.kills[structure.id].payload["victim"].pop("character_id")
        preview = self.preview(self.p1, structure.ingame)
        self.assertEqual(preview.blockers, ["This loss has no pilot."])

        # zKillboard has not got a fresh kill: the member is told to paste the in-game link, which works.
        fresh = self.loss(self.p1, hidden=True, ago=timedelta(minutes=2))
        response = self.check(self.p1, fresh.zkill)
        self.assertTrue(response.context["claim_form"].non_field_errors())
        self.assertIn("in-game", " ".join(response.context["claim_form"].non_field_errors()))
        self.assertTrue(self.preview(self.p1, fresh.ingame).eligible)

    def test_members_without_the_right_have_no_srp_pages(self):
        self.client.force_login(self.nosrp)
        self.assertEqual(self.client.get(reverse("fleetops:srp_home")).status_code, 403)
        self.assertEqual(self.client.get(reverse("fleetops:srp_link", args=[self.link.code])).status_code, 403)
        self.assertEqual(self.post(reverse("fleetops:srp_check"), {"killmail": "x"}).status_code, 403)

    def test_a_pilot_who_was_not_in_the_fleet_gets_a_warning_and_a_reviewer_can_check_again(self):
        loss = self.loss(self.p2, ago=timedelta(hours=1))

        preview = self.preview(self.p2, loss.ingame, self.link)
        self.assertTrue(preview.eligible)
        self.assertEqual((preview.presence.code, preview.presence.level), ("never_seen", "danger"))
        self.assertIn("never_seen", [warning.code for warning in preview.warnings])
        claim = self.submit(self.p2, loss.ingame, self.link)
        self.assertEqual(claim.status, Status.PENDING)
        self.assertEqual(claim.fleet_check, "never_seen")
        self.assertIn("never_seen", [warning["code"] for warning in claim.warnings])
        page = self.claim_page(self.p2, claim)
        self.assertContains(page, "This character was never seen in the fleet.")

        # The pilot's join arrives in the tracking data later; the reviewer checks again.
        self.join(self.fleet, self.p2, NOW - timedelta(hours=2))
        self.act(self.mgr, claim, "recheck")
        self.assertEqual(claim.fleet_check, "in_fleet")
        self.assertNotIn("never_seen", [warning["code"] for warning in claim.warnings])
        self.assertEqual(claim.events.filter(action="recheck").count(), 1)

        # Joining a minute after the loss is within the polling grace.
        late = self.loss(self.q1, at=NOW - timedelta(hours=2, minutes=51))
        self.assertEqual(self.preview(self.q1, late.ingame, self.link).presence.code, "near")

    def test_a_pap_link_fleet_checks_the_presence_by_registration(self):
        fleet = f.create_operation(
            self.fc, link_only=True, is_manual=True, tracking_enabled=False, started_at=NOW - timedelta(hours=3)
        )
        link = f.fleet_link(fleet)
        f.registration(fleet, f.main_of(self.p1))

        registered = self.submit(self.p1, self.loss(self.p1).ingame, link)
        stranger = self.submit(self.p2, self.loss(self.p2).ingame, link)

        self.assertEqual((registered.fleet_check, stranger.fleet_check), ("registered", "never_seen"))
        self.assertEqual(registered.scope, Scope.IN_FLEET)
        self.assertEqual(
            [warning["level"] for warning in registered.warnings if warning["code"] == "registered"], ["warning"]
        )
        self.assertContains(
            self.claim_page(self.mgr, registered), "No tracking data; registered for PAP in this fleet."
        )

    def test_a_loss_in_the_fleet_is_offered_as_a_fleet_loss_on_the_general_page(self):
        loss = self.loss(self.p1)

        preview = self.preview(self.p1, loss.ingame)

        self.assertEqual((preview.scope, preview.suggested_link), (Scope.OUT_OF_FLEET, self.link))
        self.assertContains(self.check(self.p1, loss.ingame), "Submit it as a fleet loss?")
        # A closed link is not offered.
        self.client.force_login(self.mgr)
        self.post(reverse("fleetops:srp_link_close", args=[self.link.code]))
        self.assertIsNone(self.preview(self.p1, loss.ingame).suggested_link)
        closed = self.page(self.p1, "srp_link", self.link.code)
        self.assertNotContains(closed, 'name="killmail"')

    def test_a_young_loss_waits_for_its_value_and_the_sweep_brings_it(self):
        kill = self.loss(self.p1, ago=timedelta(minutes=2), fitted=20 * M)

        claim = self.submit(self.p1, kill.ingame, self.link)

        self.assertEqual(self.zkill_requests, [])
        self.assertEqual((claim.suggested_amount, claim.payout_basis, claim.loss_value), (0, "none", None))
        self.assertIn("value_pending", [warning["code"] for warning in claim.warnings])
        with mock.patch("django.utils.timezone.now", return_value=NOW + timedelta(minutes=10)):
            cache.delete(zkillboard.SLOT_KEY)
            self.assertEqual(srp_tasks.refresh_srp_values(), 1)
        claim.refresh_from_db()
        self.assertEqual((claim.suggested_amount, claim.payout_basis), (16 * M, "percentage"))
        self.assertEqual(claim.loss_value, Decimal("20000000.00"))
        self.assertNotIn("value_pending", [warning["code"] for warning in claim.warnings])
        event = claim.events.get(action="value_update")
        self.assertIsNone(event.actor)
        self.assertEqual(len(self.zkill_requests), 1)


# ---------------------------------------------------------------------------
# What a loss is worth
# ---------------------------------------------------------------------------


class AmountFlowTests(SrpFlowBase):
    def amounts(self, preview):
        return preview.calculation.basis, preview.calculation.suggested

    def test_the_payout_table_beats_the_percentage_and_the_most_specific_rule_wins(self):
        self.rule(GUARDIAN, 100 * M)
        self.rule(0, 40 * M, doctrine="Muninns")
        self.rule(GUARDIAN, 120 * M, doctrine="Muninns")

        in_fleet_guardian = self.preview(self.p1, self.loss(self.p1, ship=GUARDIAN).ingame, self.link)
        in_fleet_rifter = self.preview(self.p1, self.loss(self.p1).ingame, self.link)
        out_guardian = self.preview(self.p1, self.loss(self.p1, ship=GUARDIAN).ingame)
        out_rifter = self.preview(self.p1, self.loss(self.p1, fitted=10234567.89).ingame)

        # Doctrine and ship, then doctrine alone; out of fleet only the ship rule can match.
        self.assertEqual(self.amounts(in_fleet_guardian), ("rule", 120 * M))
        self.assertEqual(in_fleet_guardian.policy.rule.match, "doctrine_ship")
        self.assertEqual(self.amounts(in_fleet_rifter), ("rule", 40 * M))
        self.assertEqual(in_fleet_rifter.policy.rule.match, "doctrine")
        self.assertEqual(self.amounts(out_guardian), ("rule", 100 * M))
        self.assertEqual(out_guardian.policy.rule.match, "ship")
        # No rule: 80 % of the fitted value, rounded down.
        self.assertEqual(self.amounts(out_rifter), ("percentage", 8_187_654))
        self.assertIsNone(out_rifter.policy.rule)

        claim = self.submit(self.p1, self.loss(self.p1, ship=GUARDIAN).ingame, self.link)
        self.assertEqual((claim.payout_basis, claim.suggested_amount, claim.payout_rule.amount), ("rule", 120 * M, 120 * M))
        # The claim keeps the rule as it was when it was made.
        SrpClaim.objects.get(pk=claim.pk).payout_rule.delete()
        claim.refresh_from_db()
        self.assertEqual((claim.policy["rule"]["amount"], claim.policy["rule"]["match"]), (120 * M, "doctrine_ship"))

    def test_a_percentage_of_zero_pays_nothing_without_a_rule(self):
        f.srp_settings(percentage=Decimal("0"))

        preview = self.preview(self.p1, self.loss(self.p1).ingame, self.link)

        self.assertEqual(self.amounts(preview), ("none", 0))

    def test_capsule_losses_are_paid_from_a_capsule_entry_only(self):
        self.rule(0, 40 * M, doctrine="Muninns")
        pod = self.loss(self.p1, ship=CAPSULE, fitted=900_000_000)

        # 80 % of an implant-laden value, or a doctrine rule for any ship, never covers a pod.
        for link in (None, self.link):
            blocked = self.preview(self.p1, pod.ingame, link)
            self.assertEqual((blocked.eligible, blocked.token), (False, ""))
            self.assertEqual(blocked.blockers, ["Capsule losses are not covered."])

        self.rule(CAPSULE, 5 * M)
        covered = self.preview(self.p1, pod.ingame, self.link)
        self.assertTrue(covered.eligible, covered.blockers)
        self.assertEqual(self.amounts(covered), ("rule", 5 * M))

        # The switch is the owner's: with it off a pod is paid like any ship.
        SrpPayoutRule.objects.filter(ship_type_id=CAPSULE).delete()
        f.srp_settings(capsules_need_rule=False)
        self.assertEqual(self.amounts(self.preview(self.p1, pod.ingame)), ("percentage", 720 * M))

    def test_per_loss_caps_apply_by_scope(self):
        self.rule(GUARDIAN, 100 * M)
        f.srp_settings(in_fleet_loss_cap=5 * M, out_of_fleet_loss_cap=3 * M)

        in_fleet = self.preview(self.p1, self.loss(self.p1, ship=GUARDIAN).ingame, self.link)
        out_of_fleet = self.preview(self.p1, self.loss(self.p1, ship=GUARDIAN).ingame)
        under_cap = self.preview(self.p1, self.loss(self.p1, fitted=2 * M).ingame, self.link)

        self.assertEqual((in_fleet.calculation.base, in_fleet.calculation.suggested), (100 * M, 5 * M))
        self.assertEqual(in_fleet.calculation.binding_limit, "loss_cap")
        self.assertEqual((out_of_fleet.calculation.base, out_of_fleet.calculation.suggested), (100 * M, 3 * M))
        self.assertEqual(out_of_fleet.calculation.cap, 3 * M)
        self.assertEqual(under_cap.calculation.suggested, int(2 * M * Decimal("0.8")))
        self.assertEqual(under_cap.calculation.binding_limit, "")


# ---------------------------------------------------------------------------
# Monthly limits
# ---------------------------------------------------------------------------


class LimitFlowTests(SrpFlowBase):
    def setUp(self):
        super().setUp()
        self.rule(RIFTER, 30 * M)

    def usage(self, user, claim_scope=Scope.OUT_OF_FLEET):
        main = f.main_of(user)
        policy = srp_policy.current_policy(corporation_id=main.corporation_id)
        states = quota.limit_states(
            user_id=user.pk,
            corporation_id=main.corporation_id,
            scope=claim_scope,
            killmail_time=NOW - timedelta(hours=1),
            policy=policy,
        )
        return {state.kind: (state.used, state.remaining) for state in states}

    def test_personal_and_corporation_limits_with_the_in_fleet_switch(self):
        # 50M a player (in-fleet losses do not count), 70M a corporation (they do); Beta has 20M of its own.
        f.srp_settings(
            personal_monthly_cap=50 * M,
            personal_cap_includes_in_fleet=False,
            corporation_monthly_cap=70 * M,
            corporation_cap_includes_in_fleet=True,
        )
        SrpCorporationLimit.objects.create(corporation_id=f.OTHER_CORP[0], corporation_name="Test Corp Two", monthly_cap=20 * M)

        out_one = self.submit(self.p1, self.loss(self.p1).ingame)
        self.assertEqual((out_one.limited_amount, out_one.over_limit), (30 * M, False))
        self.approve(self.mgr, out_one)
        self.assertEqual((out_one.status, out_one.approved_amount), (Status.APPROVED, 30 * M))

        # The in-fleet loss does not count toward the personal limit but toward the corporation's.
        in_fleet = self.submit(self.p1, self.loss(self.p1).ingame, self.link)
        self.assertEqual((in_fleet.counts_personal, in_fleet.counts_corporation), (False, True))
        self.assertEqual((in_fleet.limited_amount, in_fleet.over_limit), (30 * M, False))
        self.approve(self.mgr, in_fleet)
        self.assertEqual(self.usage(self.p1), {"personal": (30 * M, 20 * M), "corporation": (60 * M, 10 * M)})

        # Alpha has 10M left; the player has 20M: the most restrictive limit wins.
        out_two = self.submit(self.p1, self.loss(self.p1).ingame)
        self.assertEqual((out_two.suggested_amount, out_two.limited_amount, out_two.over_limit), (30 * M, 10 * M, True))
        self.assertEqual(out_two.calculation["binding_limit"], "corporation")
        warning = next(item for item in out_two.warnings if item["code"] == "over_limit")
        self.assertEqual((warning["level"], warning["params"]["kind"], warning["params"]["remaining"]), ("danger", "corporation", 10 * M))
        # Review mode keeps it for a person to decide, with the red warning.
        self.assertEqual(out_two.status, Status.PENDING)

        # More than what is left needs the box and a reason; the button for the limit needs neither.
        refused = self.approve(self.mgr, out_two, mode="amount", amount="30m", confirm_above_policy="", expect=200)
        self.assertEqual(out_two.status, Status.PENDING)
        self.assertIn("over the monthly corporation limit", " ".join(flashed(refused)))
        self.assertEqual(self.act(self.mgr, out_two, "approve", {"mode": "amount", "amount": "30m", "confirm_over_limit": "on"}, expect=200).status_code, 200)
        self.assertEqual(out_two.status, Status.PENDING)
        self.approve(self.mgr, out_two, mode="within_limit")
        self.assertEqual((out_two.status, out_two.approved_amount), (Status.APPROVED, 10 * M))
        self.assertEqual(out_two.decision_reason, "Reduced to the remaining monthly corporation limit.")
        self.assertEqual(self.usage(self.p1), {"personal": (40 * M, 10 * M), "corporation": (70 * M, 0)})

        # A colleague in the same corporation finds it used up; in review mode the claim waits with the warning.
        p2_claim = self.submit(self.p2, self.loss(self.p2).ingame)
        self.assertEqual((p2_claim.status, p2_claim.limited_amount, p2_claim.over_limit), (Status.PENDING, 0, True))
        # Another corporation has its own cap, so the pilot of Beta is limited by 20M, not 70M.
        q_claim = self.submit(self.q1, self.loss(self.q1).ingame)
        self.assertEqual((q_claim.suggested_amount, q_claim.limited_amount), (30 * M, 20 * M))
        self.assertEqual(q_claim.policy["corporation_cap_source"], "override")

        # Revoking an approval releases its amount for the others.
        self.act(self.mgr, out_two, "revoke", {"reason": "Wrong fit"})
        self.assertEqual(out_two.status, Status.PENDING)
        self.assertEqual(self.usage(self.p1), {"personal": (30 * M, 20 * M), "corporation": (60 * M, 10 * M)})
        self.assertEqual(out_two.events.filter(action="revoke").count(), 1)
        # ...and a paid claim keeps counting.
        self.client.force_login(self.mgr)
        self.post(reverse("fleetops:srp_payments"), {"claim": [str(out_one.uuid)], "note": "In game"})
        out_one.refresh_from_db()
        self.assertEqual(out_one.status, Status.PAID)
        self.assertEqual(self.usage(self.p1)["personal"][0], 30 * M)

    def test_reject_mode_cuts_a_partial_claim_and_rejects_when_nothing_is_left(self):
        f.srp_settings(corporation_monthly_cap=50 * M, over_limit_action="reject")
        used = self.submit(self.p1, self.loss(self.p1).ingame)
        self.approve(self.mgr, used)

        # 20M of the 50M are left: the claim is cut to what is left and goes to review (3-2).
        partial = self.submit(self.p2, self.loss(self.p2).ingame)
        self.assertEqual((partial.status, partial.suggested_amount, partial.limited_amount), (Status.PENDING, 30 * M, 20 * M))
        self.assertFalse(partial.auto_rejected)
        warning = next(item for item in partial.warnings if item["code"] == "reduced_by_limit")
        self.assertEqual(warning["level"], "warning")
        # More than what is left is refused outright in reject mode, whatever the manager ticks.
        refused = self.act(
            self.mgr, partial, "approve", {"mode": "amount", "amount": "30m", "confirm_over_limit": "on", "reason": "x"}, expect=200
        )
        self.assertIn("Only 20,000,000 ISK left in the monthly corporation limit.", " ".join(flashed(refused)))
        self.approve(self.mgr, partial, mode="within_limit")
        self.assertEqual((partial.status, partial.approved_amount), (Status.APPROVED, 20 * M))

        # Nothing is left: rejected on the spot, with the reason, the numbers and a notice in Alliance Auth.
        none_left = self.submit(self.p1, self.loss(self.p1).ingame)
        self.assertEqual((none_left.status, none_left.auto_rejected), (Status.REJECTED, True))
        self.assertEqual(
            none_left.decision_reason,
            "Rejected automatically: the monthly corporation limit is used up (cap 50,000,000 ISK).",
        )
        event = none_left.events.get()
        self.assertEqual(event.action, "auto_reject")
        self.assertEqual(event.data["auto_reject"], {"code": "limit_used_up", "kind": "corporation", "cap": 50 * M})
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_auto_reject").exists())
        self.assertEqual(Notification.objects.filter(user=self.p1, title__startswith="SRP rejected").count(), 1)

    def test_a_loss_counts_in_the_month_it_happened_in(self):
        f.srp_settings(personal_monthly_cap=40 * M)
        september = self.submit(self.p1, self.loss(self.p1, at=utc(2026, 9, 30, 23, 50)).ingame)
        self.approve(self.mgr, september)

        # Approved in October, but September's loss: October's allowance is untouched.
        october = self.submit(self.p1, self.loss(self.p1, at=utc(2026, 10, 1, 0, 10)).ingame)
        self.assertEqual((october.limited_amount, october.over_limit), (30 * M, False))
        self.assertEqual(september.killmail_time, utc(2026, 9, 30, 23, 50))
        self.assertEqual(quota.claim_month(september), (2026, 9))
        self.assertEqual(quota.claim_month(october), (2026, 10))
        self.approve(self.mgr, october)
        sep = summaries.personal(self.p1, 2026, 9)
        octo = summaries.personal(self.p1, 2026, 10)
        self.assertEqual((sep.totals.approved, octo.totals.approved), (30 * M, 30 * M))
        self.assertEqual((sep.limits[0].used, octo.limits[0].used), (30 * M, 30 * M))
        # The page shows each month's own claims.
        self.assertEqual([c.pk for c in self.page(self.p1, "srp_home", year=2026, month=9).context["claims"]], [september.pk])
        self.assertEqual([c.pk for c in self.page(self.p1, "srp_home", year=2026, month=10).context["claims"]], [october.pk])

        # The payments list can also be filtered by the day of payment.
        self.client.force_login(self.mgr)
        self.post(reverse("fleetops:srp_payments"), {"claim": [str(september.uuid)], "note": ""})
        september.refresh_from_db()
        self.assertEqual(september.paid_at, NOW)
        paid_today = self.page(self.mgr, "srp_payments", paid_from="2026-10-14", paid_to="2026-10-14")
        self.assertEqual([c.pk for c in paid_today.context["paid_claims"]], [september.pk])
        paid_later = self.page(self.mgr, "srp_payments", paid_from="2026-10-15")
        self.assertEqual(list(paid_later.context["paid_claims"]), [])


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


    def test_the_reason_for_a_reduction_is_stored_in_English_and_read_in_the_language_of_each_reader(self):
        f.srp_settings(personal_monthly_cap=20 * M, corporation_monthly_cap=None)
        claim = self.submit(self.p1, self.loss(self.p1).ingame)
        self.assertEqual((claim.limited_amount, claim.calculation["binding_limit"]), (20 * M, "personal"))

        # A reviewer who reads Chinese presses the button and writes nothing.
        self.client.cookies["django_language"] = "zh-hans"
        self.approve(self.mgr, claim, mode="within_limit")

        english = "Reduced to the remaining monthly personal limit."
        chinese = "已削减到个人月度限额的剩余额度。"
        self.assertEqual(claim.decision_reason, english)
        self.assertEqual(list(claim.events.filter(action="approve").values_list("reason", flat=True)), [english])
        self.assertEqual(list(AuditLog.objects.filter(action="srp.claim_approve").values_list("reason", flat=True)), [english])
        self.assertContains(self.claim_page(self.p1, claim), chinese)
        self.client.cookies["django_language"] = "en"
        page = self.claim_page(self.p1, claim)
        self.assertContains(page, english)
        self.assertNotContains(page, chinese)


class DecisionFlowTests(SrpFlowBase):
    def setUp(self):
        super().setUp()
        self.rule(RIFTER, 30 * M)

    def test_a_manager_changes_the_amount_with_a_reason_rejects_revokes_and_pays(self):
        first = self.submit(self.p1, self.loss(self.p1).ingame, self.link)
        second = self.submit(self.p1, self.loss(self.p1).ingame)
        third = self.submit(self.p2, self.loss(self.p2).ingame)

        # A different amount needs a reason, more than the policy amount also the box.
        refused = self.approve(self.mgr, first, mode="amount", amount="25m", expect=200)
        self.assertIn("Give a reason for this amount.", " ".join(flashed(refused)))
        self.assertEqual(first.status, Status.PENDING)
        refused = self.approve(self.mgr, first, mode="amount", amount="35m", reason="Fit was expensive", expect=200)
        self.assertEqual(first.status, Status.PENDING)
        self.assertContains(refused, "Pay more than the policy amount")
        self.approve(self.mgr, first, mode="amount", amount="25m", reason="Old fit")
        self.assertEqual((first.status, first.approved_amount, first.decided_by, first.decision_reason), (Status.APPROVED, 25 * M, self.mgr, "Old fit"))
        # The amount of an approved claim can still be changed, with a reason; the change is audited.
        self.approve(self.mgr, first, mode="amount", amount="28m", reason="Typo")
        self.assertEqual(first.approved_amount, 28 * M)
        self.assertEqual(list(first.events.order_by("pk").values_list("action", flat=True)), ["submit", "approve", "amount_change"])
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_amount_change", object_id=str(first.pk)).exists())

        # Reject needs a reason; the pilot sees it and gets a notice.
        self.act(self.mgr, second, "reject", {"reason": ""}, expect=200)
        self.assertEqual(second.status, Status.PENDING)
        self.act(self.mgr, second, "reject", {"reason": "Not a fleet doctrine ship"})
        self.assertEqual((second.status, second.decision_reason), (Status.REJECTED, "Not a fleet doctrine ship"))
        self.assertContains(self.claim_page(self.p1, second), "Not a fleet doctrine ship")
        self.assertEqual(Notification.objects.filter(user=self.p1, title__startswith="SRP rejected").count(), 1)
        # A decision twice does not do anything twice.
        again = self.act(self.mgr, second, "reject", {"reason": "Again"})
        self.assertEqual(second.decision_reason, "Not a fleet doctrine ship")
        self.assertIn("This claim is already rejected.", flashed(again))
        # A rejected claim can be reopened with a reason.
        self.act(self.mgr, second, "reopen", {"reason": "Appeal accepted"})
        self.assertEqual(second.status, Status.PENDING)

        # Revoking sends it back for a decision.
        self.act(self.mgr, first, "revoke", {"reason": "Counted twice"})
        self.assertEqual((first.status, first.approved_amount), (Status.PENDING, None))
        self.approve(self.mgr, first)
        self.approve(self.mgr, third)
        # Approved, approved again after the revoke; the changed amount has its own notice.
        self.assertEqual(Notification.objects.filter(user=self.p1, title__startswith="SRP approved").count(), 2)
        self.assertEqual(Notification.objects.filter(user=self.p1, title__startswith="SRP amount changed").count(), 1)

        # Payments: grouped by main, with the copy buttons; the amount is digits only for the in-game field.
        page = self.page(self.mgr, "srp_payments")
        rows = {row.main_character_name: row for row in page.context["pay_rows"]}
        self.assertEqual(set(rows), {"Alpha One", "Alpha Two"})
        self.assertEqual(rows["Alpha One"].total, 30 * M)
        self.assertContains(page, 'data-srp-copy="30000000"')
        self.assertContains(page, 'data-srp-copy="Alpha One"')
        response = self.post(
            reverse("fleetops:srp_payments"),
            {"claim": [str(first.uuid), str(third.uuid)], "note": "Paid in game"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("2 claims marked paid (60,000,000 ISK)." in text for text in flashed(response)))
        for claim in (first, third):
            claim.refresh_from_db()
            self.assertEqual((claim.status, claim.paid_by, claim.paid_at, claim.payment_note), (Status.PAID, self.mgr, NOW, "Paid in game"))
        self.assertEqual(Notification.objects.filter(user=self.p2, title__startswith="SRP paid").count(), 1)

        # A paid claim is final until the payment is undone.
        stuck = self.act(self.mgr, first, "revoke", {"reason": "Too late"})
        self.assertEqual(first.status, Status.PAID)
        self.assertIn("This claim is paid. Undo the payment first, then revoke the approval.", flashed(stuck))
        self.act(self.mgr, first, "unpaid", {"reason": "ISK was not sent"})
        self.assertEqual((first.status, first.paid_at), (Status.APPROVED, first.paid_at))
        trail = [action for action in SrpClaimEvent.objects.filter(claim=first).order_by("pk").values_list("action", flat=True)]
        self.assertEqual(trail[-2:], ["paid", "unpaid"])
        for action in ("claim_approve", "claim_reject", "claim_revoke", "claim_paid", "claim_unpaid", "claim_reopen"):
            self.assertTrue(AuditLog.objects.filter(action=f"srp.{action}").exists(), action)

    def test_managers_cannot_decide_on_their_own_claims_unless_the_setting_allows_it(self):
        own = self.submit(self.mgr, self.loss(self.mgr).ingame)
        own_two = self.submit(self.mgr, self.loss(self.mgr).ingame)
        clean = self.submit(self.p1, self.loss(self.p1).ingame)

        # No decision form on the page, and the routes refuse.
        page = self.claim_page(self.mgr, own)
        self.assertNotContains(page, reverse("fleetops:srp_claim_action", args=[own.uuid, "approve"]))
        self.assertContains(page, "Another SRP manager has to decide on it.")
        for action, data in (("approve", {"mode": "suggested"}), ("reject", {"reason": "no"}), ("recalculate", {})):
            self.act(self.mgr, own, action, data, expect=403)
        self.assertEqual(own.status, Status.PENDING)
        # A re-check and a note only need the right to manage.
        self.act(self.mgr, own, "recheck")
        self.act(self.mgr, own, "note", {"text": "Checked the killmail"})

        # Bulk approval skips the manager's own claims and says so.
        self.client.force_login(self.mgr)
        response = self.post(
            reverse("fleetops:srp_review_bulk_approve"),
            {"claim": [str(own.uuid), str(own_two.uuid), str(clean.uuid)]},
        )
        texts = " ".join(flashed(response))
        self.assertIn("1 claim approved at the suggested amount.", texts)
        self.assertIn("your own claim; another SRP manager has to decide.", texts)
        clean.refresh_from_db(), own.refresh_from_db()
        self.assertEqual((clean.status, own.status), (Status.APPROVED, Status.PENDING))

        # Another manager decides; the payment of the manager's own claim is for another manager as well.
        self.approve(self.mgr2, own)
        self.assertEqual(own.status, Status.APPROVED)
        self.client.force_login(self.mgr)
        paid = self.post(reverse("fleetops:srp_payments"), {"claim": [str(own.uuid)], "note": ""})
        own.refresh_from_db()
        self.assertEqual(own.status, Status.APPROVED)
        self.assertIn("your own claim; another SRP manager has to decide.", " ".join(flashed(paid)))
        self.act(self.mgr2, own, "paid")
        self.assertEqual(own.status, Status.PAID)

        # Small alliances can allow it.
        f.srp_settings(allow_self_review=True)
        self.approve(self.mgr, own_two)
        self.assertEqual((own_two.status, own_two.decided_by), (Status.APPROVED, self.mgr))

    def test_internal_notes_stay_with_the_managers(self):
        claim = self.submit(self.p1, self.loss(self.p1).ingame, self.link)
        self.act(self.mgr, claim, "note", {"text": "Pilot has a history of false claims"})

        self.assertContains(self.claim_page(self.mgr2, claim), "Pilot has a history of false claims")
        for viewer in (self.p1, self.fc, self.statsall):
            with self.subTest(viewer=viewer.username):
                self.assertNotContains(self.claim_page(viewer, claim), "Pilot has a history of false claims")
        self.assertTrue(claim.events.get(action="note").internal)

    def test_withdraw_and_resubmit_by_the_pilot(self):
        claim = self.submit(self.p1, self.loss(self.p1).ingame)
        self.client.force_login(self.p1)
        self.post(reverse("fleetops:srp_claim_withdraw", args=[claim.uuid]), {"reason": "Wrong ship"})
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.WITHDRAWN)
        self.post(reverse("fleetops:srp_claim_resubmit", args=[claim.uuid]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        # Nobody else can.
        self.client.force_login(self.p2)
        self.assertEqual(self.post(reverse("fleetops:srp_claim_withdraw", args=[claim.uuid])).status_code, 404)


# ---------------------------------------------------------------------------
# Who sees which summary
# ---------------------------------------------------------------------------


class VisibilityFlowTests(SrpFlowBase):
    def setUp(self):
        super().setUp()
        self.rule(RIFTER, 10 * M)
        # Claims: p1 and p2 (Alpha) in the fleet of fc and out of fleet; q1 (Beta) in the fleet of fc2.
        self.join(self.other_fleet, self.q1, NOW - timedelta(hours=2, minutes=50))
        self.a1 = self.submit(self.p1, self.loss(self.p1).ingame, self.link)
        self.a2 = self.submit(self.p2, self.loss(self.p2).ingame)
        self.b1 = self.submit(self.q1, self.loss(self.q1).ingame, self.other_link)
        self.approve(self.mgr, self.a1)
        self.approve(self.mgr, self.b1)
        self.client.force_login(self.mgr)
        self.post(reverse("fleetops:srp_payments"), {"claim": [str(self.b1.uuid)], "note": ""})
        for claim in (self.a1, self.a2, self.b1):
            claim.refresh_from_db()
        self.month = {"year": 2026, "month": 10}

    def claim_ids(self, response):
        return {claim.pk for claim in response.context["claims"]}

    @staticmethod
    def claim_lines(rows):
        """The claim rows of an SRP claims file by pilot, each as a dict by column title."""
        start = next(index for index, row in enumerate(rows) if row and row[0] == "Claim")
        header = rows[start]
        lines = [dict(zip(header, row)) for row in rows[start + 1 :] if row and row[0].startswith("SRP-")]
        return {line["Pilot (main)"]: line for line in lines}

    def test_a_member_sees_their_own_claims_only(self):
        for route, args in (("srp_summary", ()), ("srp_summary_own_corporation", ()), ("srp_summary_own_fc", ())):
            self.page(self.p1, route, *args, expect=403)
        self.page(self.p1, "srp_summary_fc", self.fc.pk, expect=404)
        self.page(self.p1, "srp_review", expect=403)
        self.page(self.p1, "srp_payments", expect=403)
        self.claim_page(self.p1, self.a1)
        self.claim_page(self.p1, self.a2, expect=404)
        self.claim_page(self.p1, self.b1, expect=404)
        home = self.page(self.p1, "srp_home", **self.month)
        self.assertEqual({claim.pk for claim in home.context["claims"]}, {self.a1.pk})
        # A pilot's own claim shows no manager forms.
        self.assertNotContains(self.claim_page(self.p1, self.a1), reverse("fleetops:srp_claim_action", args=[self.a1.uuid, "reject"]))

    def test_an_fc_sees_the_claims_and_totals_of_the_fleets_they_led(self):
        page = self.page(self.fc, "srp_summary_own_fc", **self.month)
        self.assertEqual(self.claim_ids(page), {self.a1.pk})
        self.assertEqual((page.context["totals"].claims, page.context["totals"].approved), (1, 10 * M))
        self.assertEqual([row.totals.claims for row in page.context["fleet_rows"]], [1])
        other = self.page(self.fc2, "srp_summary_own_fc", **self.month)
        self.assertEqual(self.claim_ids(other), {self.b1.pk})
        self.claim_page(self.fc, self.a1)
        self.claim_page(self.fc, self.b1, expect=404)
        self.claim_page(self.fc, self.a2, expect=404)
        self.page(self.fc, "srp_summary", expect=403)
        self.page(self.fc, "srp_summary_own_corporation", expect=403)
        self.page(self.fc, "srp_summary_fc", self.fc2.pk, expect=404)
        self.page(self.fc, "srp_review", expect=403)
        # The fleet page lists the claims of the fleet for its FC.
        fleet_page = self.page(self.fc, "operation_detail", self.fleet.uuid)
        self.assertContains(fleet_page, self.a1.reference)
        self.assertNotContains(fleet_page, self.b1.reference)

    def test_a_corporation_manager_sees_the_own_corporation_and_its_member_claims(self):
        page = self.page(self.corpmgr, "srp_summary_own_corporation", **self.month)
        self.assertEqual(self.claim_ids(page), {self.a1.pk, self.a2.pk})
        self.assertEqual(page.context["corporation_id"], f.DEFAULT_CORP[0])
        self.assertEqual({row.main_character_name for row in page.context["member_rows"]}, {"Alpha One", "Alpha Two"})
        self.assertEqual((page.context["corporation"].totals.claims, page.context["corporation"].totals.approved), (2, 10 * M))
        self.claim_page(self.corpmgr, self.a2)
        self.claim_page(self.corpmgr, self.b1, expect=404)
        self.page(self.corpmgr, "srp_summary_corporation", f.OTHER_CORP[0], expect=404)
        self.page(self.corpmgr, "srp_summary", expect=403)
        self.page(self.corpmgr, "srp_summary_fc", self.fc.pk, expect=404)
        self.page(self.corpmgr, "srp_review", expect=403)

    def test_view_all_stats_sees_every_summary_read_only(self):
        alliance = self.page(self.statsall, "srp_summary", **self.month)
        summary = alliance.context["summary"]
        self.assertEqual(summary.totals.claims, 3)
        self.assertEqual((summary.totals.approved, summary.totals.paid), (20 * M, 10 * M))
        self.assertEqual(summary.in_fleet.claims, 2)
        self.assertEqual(summary.out_of_fleet.claims, 1)
        corporations = {row.corporation_name: row for row in alliance.context["corporation_rows"]}
        self.assertEqual(set(corporations), {"Test Corp One", "Test Corp Two"})
        self.assertEqual(corporations["Test Corp One"].totals.approved, 10 * M)
        for corporation_id in (f.DEFAULT_CORP[0], f.OTHER_CORP[0]):
            self.page(self.statsall, "srp_summary_corporation", corporation_id, **self.month)
        fcs = {row.fc_name: row for row in alliance.context["fc_rows"]}
        self.assertEqual(set(fcs), {"Flow FC", "Other FC"})
        self.assertEqual((fcs["Flow FC"].fleets, fcs["Flow FC"].totals.approved), (1, 10 * M))
        self.page(self.statsall, "srp_summary_fc", self.fc2.pk, **self.month)
        # Claims are readable, but there is nothing to decide with.
        detail = self.claim_page(self.statsall, self.a2)
        self.assertNotContains(detail, reverse("fleetops:srp_claim_action", args=[self.a2.uuid, "approve"]))
        self.act(self.statsall, self.a2, "approve", {"mode": "suggested"}, expect=403)
        self.page(self.statsall, "srp_review", expect=403)
        self.page(self.statsall, "srp_payments", expect=403)
        self.assertEqual(self.a2.status, Status.PENDING)

    def test_srp_managers_see_everything_and_can_decide(self):
        alliance = self.page(self.mgr2, "srp_summary", **self.month)
        self.assertEqual(alliance.context["summary"].totals.claims, 3)
        queue = self.page(self.mgr2, "srp_review", status="all")
        self.assertEqual({claim.pk for claim in queue.context["page"].object_list}, {self.a1.pk, self.a2.pk, self.b1.pk})
        pending = self.page(self.mgr2, "srp_review")
        self.assertEqual({claim.pk for claim in pending.context["page"].object_list}, {self.a2.pk})
        for corporation_id in (f.DEFAULT_CORP[0], f.OTHER_CORP[0]):
            self.page(self.mgr2, "srp_summary_corporation", corporation_id, **self.month)
        self.page(self.mgr2, "srp_summary_own_fc", **self.month)
        self.assertContains(self.claim_page(self.mgr2, self.b1), reverse("fleetops:srp_claim_action", args=[self.b1.uuid, "unpaid"]))

    def test_the_files_hold_what_the_pages_show_and_are_audited(self):
        def download(user, slug, fmt="csv", expect=200, **params):
            self.client.force_login(user)
            response = self.client.get(reverse("fleetops:export", args=[slug]), {"format": fmt, **self.month, **params})
            self.assertEqual(response.status_code, expect, (slug, params))
            return response

        rows = csv_rows(download(self.mgr, "srp-claims"))
        book = workbook_rows(download(self.mgr, "srp-claims", "xlsx"))
        self.assertEqual(trimmed(book), trimmed(rows))
        by_pilot = self.claim_lines(rows)
        self.assertEqual(
            {name: line["Status"] for name, line in by_pilot.items()},
            {"Alpha One": "Approved", "Alpha Two": "Pending", "Beta One": "Paid"},
        )
        self.assertEqual(by_pilot["Alpha One"]["Approved (ISK)"], "10000000")
        self.assertEqual(by_pilot["Beta One"]["Approved (ISK)"], "10000000")
        self.assertEqual(by_pilot["Alpha One"]["Fleet check"], "In the fleet at the time of the loss.")

        # The same numbers as the pages.
        alliance = self.page(self.mgr, "srp_summary", **self.month).context["summary"]
        self.assertEqual(
            alliance.totals.approved, sum(int(line["Approved (ISK)"] or 0) for line in by_pilot.values())
        )

        download(self.statsall, "srp-corporations")
        download(self.statsall, "srp-members")
        download(self.statsall, "srp-fcs")
        # A corporation manager gets the own corporation's claims and members, nothing else.
        own = self.claim_lines(csv_rows(download(self.corpmgr, "srp-claims", corporation=f.DEFAULT_CORP[0])))
        self.assertEqual(set(own), {"Alpha One", "Alpha Two"})
        download(self.corpmgr, "srp-members", corporation=f.DEFAULT_CORP[0])
        download(self.corpmgr, "srp-claims", corporation=f.OTHER_CORP[0], expect=404)
        download(self.corpmgr, "srp-claims", expect=403)
        download(self.corpmgr, "srp-fcs", expect=403)
        for user in (self.fc, self.p1):
            download(user, "srp-claims", expect=403)
        exports = AuditLog.objects.filter(action="srp.export")
        self.assertEqual(exports.count(), 7)
        self.assertEqual({entry.object_type for entry in exports}, {"SrpSummary"})
