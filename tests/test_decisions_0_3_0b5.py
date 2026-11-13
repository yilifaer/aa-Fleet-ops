# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Three owner decisions on the re-test of 0.3.0b4: members may resubmit rejected claims (a setting, off by default),
the fleet type colour on the statistics page of the FCs, and the SRP link in the default ping."""

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fleetops.constants import DEFAULT_PING_TEMPLATE
from fleetops.forms_config import SrpSettingsForm
from fleetops.models import AuditLog, FleetOperation, MessageTemplate, SrpClaim, SrpClaimEvent, SrpSettings
from fleetops.services import mentions
from fleetops.services.messages import render_messages
from fleetops.srp import bans, claims, display, review, review_info
from fleetops.srp.types import SrpError, SrpStateError

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module (no ESI lookups)
from .test_fc_statistics import closed_op
from .test_messages import MessageTestBase
from .test_srp_claims import ClaimBase
from .test_srp_config import settings_post

Status = SrpClaim.Status
Action = SrpClaimEvent.Action
CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


class ResubmitSettingTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("b5_resubmit_admin", perms=CONFIG_PERMS)
        self.client.force_login(self.admin)
        self.url = reverse("fleetops:configuration_list", args=["srp"])

    def test_it_is_off_by_default(self):
        self.assertFalse(SrpSettings.get_solo().member_resubmit_rejected)

    def test_the_page_shows_the_setting_and_saves_it_with_an_audit_entry(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'name="member_resubmit_rejected"')
        self.assertContains(page, "Members may resubmit rejected claims")

        response = self.client.post(self.url, settings_post(member_resubmit_rejected="on"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(SrpSettings.get_solo().member_resubmit_rejected)
        entry = AuditLog.objects.filter(object_type="SrpSettings").latest("pk")
        self.assertIs(entry.new_value["member_resubmit_rejected"], True)
        self.assertIs(entry.old_value["member_resubmit_rejected"], False)

    def test_turning_it_off_again_is_audited(self):
        SrpSettings.objects.filter(pk=1).update(member_resubmit_rejected=True)

        self.client.post(self.url, settings_post())

        self.assertFalse(SrpSettings.get_solo().member_resubmit_rejected)
        entry = AuditLog.objects.filter(object_type="SrpSettings").latest("pk")
        self.assertIs(entry.old_value["member_resubmit_rejected"], True)
        self.assertIs(entry.new_value["member_resubmit_rejected"], False)

    def test_saving_it_unchanged_writes_no_audit_entry(self):
        SrpSettings.objects.filter(pk=1).update(member_resubmit_rejected=True)
        data = settings_post(member_resubmit_rejected="on")
        first = self.client.post(self.url, data)
        self.client.get(first["Location"])
        before = AuditLog.objects.count()

        second = self.client.post(self.url, data, follow=True)

        self.assertEqual(AuditLog.objects.count(), before)
        self.assertContains(second, "Nothing was changed.")

    def test_the_form_has_the_field_in_the_members_group(self):
        form = SrpSettingsForm(instance=SrpSettings.get_solo())
        self.assertIn("member_resubmit_rejected", form.fields)
        members = next(names for _title, _text, names in SrpSettingsForm.GROUPS if "member_edit_enabled" in names)
        self.assertIn("member_resubmit_rejected", members)


class ResubmitRejectedTests(ClaimBase):
    def rejected(self, *, link=None, **kwargs):
        claim = self.submit(self.killmail(**kwargs), link=link)
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.REJECTED)
        return claim

    def test_refused_while_the_setting_is_off_with_the_old_message(self):
        claim = self.rejected()
        with self.assertRaises(SrpStateError) as caught:
            claims.resubmit_claim(claim, actor=self.user)
        self.assertEqual(caught.exception.message, "Only withdrawn claims can be submitted again.")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.REJECTED)

    def test_a_rejected_claim_goes_back_to_pending_when_the_setting_is_on(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True)

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        # The decision of the rejection is gone from the claim...
        self.assertEqual((claim.decided_by, claim.decided_at, claim.auto_rejected), (None, None, False))
        # ...and stays in its history: the rejection with its reason and the new submission.
        reject = claim.events.get(action=Action.REJECT)
        self.assertEqual((reject.actor, reject.reason), (self.manager, "Wrong ship"))
        event = claim.events.get(action=Action.RESUBMIT)
        self.assertEqual((event.actor, event.from_status, event.to_status), (self.user, "rejected", "pending"))
        entry = AuditLog.objects.filter(action="srp.claim_resubmit").latest("pk")
        self.assertEqual(entry.old_value["status"], "rejected")
        self.assertTrue(AuditLog.objects.filter(action="srp.claim_reject").exists())

    def test_it_is_calculated_again_with_the_current_rules_and_waits_in_the_queue(self):
        claim = self.rejected(fitted="100000000")
        self.settings(member_resubmit_rejected=True, percentage=Decimal("50"))

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual(claim.suggested_amount, 50_000_000)
        self.assertIn(claim, SrpClaim.objects.filter(status=Status.PENDING))
        self.assertEqual(review.pending_count(), 1)

    def test_withdrawn_claims_work_as_before_with_the_setting_on(self):
        self.settings(member_resubmit_rejected=True)
        claim = self.submit(self.killmail())
        claims.withdraw_claim(claim, actor=self.user)

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)

    def test_approved_and_paid_claims_are_still_refused_with_the_new_wording(self):
        self.settings(member_resubmit_rejected=True)
        for status in (Status.APPROVED, Status.PAID):
            claim = f.srp_claim(self.user, status=status)
            with self.subTest(status=status):
                with self.assertRaises(SrpStateError) as caught:
                    claims.resubmit_claim(claim, actor=self.user)
                self.assertEqual(caught.exception.message, "Only withdrawn or rejected claims can be submitted again.")

    def test_only_the_claimant(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True)
        for actor in (self.other, self.manager, None):
            with self.subTest(actor=actor):
                with self.assertRaises(SrpError) as caught:
                    claims.resubmit_claim(claim, actor=actor)
                self.assertEqual(caught.exception.code, "forbidden")

    def test_a_banned_member_cannot(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True)
        bans.ban_user(self.user, actor=self.manager, days=5, reason="Abuse")

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(caught.exception.code, "not_eligible")
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.REJECTED)

    def test_not_after_the_deadline_nor_with_srp_switched_off(self):
        claim = self.rejected(time=timezone.now() - timedelta(days=10))
        self.settings(member_resubmit_rejected=True, claim_deadline_days=5)
        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)
        self.assertIn("older than 5 days", caught.exception.message)

        self.settings(claim_deadline_days=0, enabled=False)
        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(claim, actor=self.user)
        self.assertEqual(caught.exception.code, "not_eligible")

    def test_a_closed_link_does_not_stop_the_claim_as_for_a_withdrawn_one(self):
        _operation, link = self.fleet()
        claim = self.rejected(link=link)
        self.settings(member_resubmit_rejected=True)
        link.is_open = False
        link.save()

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.link), (Status.PENDING, link))

    def test_over_the_monthly_limit_the_usual_rules_apply(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True, personal_monthly_cap=70_000_000, over_limit_action="reject")
        f.srp_claim(self.user, amount=70_000_000, status=Status.APPROVED)

        with self.captureOnCommitCallbacks(execute=True):
            claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.auto_rejected), (Status.REJECTED, True))
        self.assertEqual(claim.events.filter(action=Action.REJECT).count(), 1)
        self.assertEqual(claim.events.filter(action=Action.AUTO_REJECT).count(), 1)

    def test_an_imported_claim_is_never_resubmitted(self):
        claim = f.srp_claim(self.user, status=Status.REJECTED, source=SrpClaim.Source.AASRP, external_id="aasrp:9")
        self.settings(member_resubmit_rejected=True)

        with self.assertRaises(SrpStateError) as caught:
            claims.resubmit_claim(claim, actor=self.user)

        self.assertEqual(caught.exception.code, "imported")


class ResubmitRejectedReviewTrailTests(ClaimBase):
    """A rejection the claimant undoes by submitting again must stay visible to reviewers (batch approval, the queue,
    the rejection history) and a rejection made by a ban is never undone by the claimant."""

    def rejected(self, **kwargs):
        claim = self.submit(self.killmail(**kwargs))
        review.reject_claim(claim, actor=self.manager, reason="Wrong ship")
        claim.refresh_from_db()
        return claim

    def test_a_resubmitted_manual_rejection_carries_a_warning_and_batch_approval_skips_it(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True)

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        warning = next(item for item in claim.warnings if item["code"] == "resubmitted_after_rejection")
        self.assertEqual((warning["level"], warning["params"]["reason"]), ("warning", "Wrong ship"))
        result = review.approve_clean_claims([claim], actor=self.manager)
        self.assertEqual(result.done, [])
        self.assertEqual(len(result.skipped), 1)
        claim.refresh_from_db()
        self.assertEqual(claim.status, Status.PENDING)
        # A reviewer who looks at it can still approve it by hand.
        review.approve_claim(claim, actor=self.manager, amount=claim.suggested_amount)

    def test_the_warning_is_shown_on_the_claim_page_and_dropped_when_rejected_again(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True)
        claims.resubmit_claim(claim, actor=self.user)
        claim.refresh_from_db()
        self.assertIn("Submitted again after a rejection", display.warning_text("resubmitted_after_rejection", claim.warnings[0]["params"]))

    def test_an_automatic_rejection_does_not_get_the_warning(self):
        claim = self.submit(self.killmail())
        SrpClaim.objects.filter(pk=claim.pk).update(status=Status.REJECTED, auto_rejected=True)
        SrpClaimEvent.objects.create(claim=claim, action=Action.AUTO_REJECT, to_status="rejected", reason="Over the limit")
        self.settings(member_resubmit_rejected=True)

        claims.resubmit_claim(claim, actor=self.user)

        claim.refresh_from_db()
        self.assertNotIn("resubmitted_after_rejection", [item["code"] for item in claim.warnings])

    def test_a_claim_rejected_by_a_ban_cannot_be_submitted_again_even_after_the_ban(self):
        pending = self.submit(self.killmail())
        self.settings(member_resubmit_rejected=True)
        ban, result = bans.ban_user(
            self.user, actor=self.manager, days=7, reason="Abuse", reject_pending=True, reject_reason="Banned"
        )
        self.assertEqual(len(result.done), 1)
        pending.refresh_from_db()
        self.assertEqual(pending.status, Status.REJECTED)
        self.assertTrue(pending.events.get(action=Action.REJECT).data["ban"])
        bans.lift_ban(ban, actor=self.manager, reason="Over")

        with self.assertRaises(SrpError) as caught:
            claims.resubmit_claim(pending, actor=self.user)

        self.assertEqual(caught.exception.code, "rejected_by_ban")
        self.assertFalse(claims.can_resubmit(pending, SrpSettings.get_solo()))
        pending.refresh_from_db()
        self.assertEqual(pending.status, Status.REJECTED)
        # A reviewer can still reopen it.
        review.reopen_claim(pending, actor=self.manager, reason="Mistake")
        pending.refresh_from_db()
        self.assertEqual(pending.status, Status.PENDING)

    def test_a_manual_rejection_outside_a_ban_can_still_be_submitted_again(self):
        claim = self.rejected()
        self.settings(member_resubmit_rejected=True)
        self.assertTrue(claims.can_resubmit(claim, SrpSettings.get_solo()))

    def test_resubmitting_keeps_the_rejection_in_the_history_of_the_reviewers(self):
        claims_ = [self.rejected(), self.rejected(), self.rejected()]
        self.settings(member_resubmit_rejected=True)
        for claim in claims_:
            claims.resubmit_claim(claim, actor=self.user)
        current = f.srp_claim(self.user)

        info = review_info.rejection_history(current, SrpSettings.get_solo())

        self.assertEqual((info["manual"], info["auto"], info["recent"]), (3, 0, 3))
        self.assertEqual(len(info["latest"]), 3)
        self.assertEqual({item.decision_reason for item in info["latest"]}, {"Wrong ship"})
        self.assertFalse(any(item.auto_rejected for item in info["latest"]))


class ResubmitRejectedPageTests(ClaimBase):
    def setUp(self):
        super().setUp()
        self.claim = self.submit(self.killmail())
        review.reject_claim(self.claim, actor=self.manager, reason="Wrong ship")

    def open(self, user=None):
        self.client.force_login(user or self.user)
        return self.client.get(reverse("fleetops:srp_claim", args=[self.claim.uuid]))

    def test_no_button_while_the_setting_is_off(self):
        page = self.open()
        self.assertFalse(page.context["can_resubmit"])
        self.assertNotContains(page, "Submit again")

    def test_the_claimant_sees_the_button_when_it_is_on_and_nobody_else_does(self):
        self.settings(member_resubmit_rejected=True)

        page = self.open()
        self.assertTrue(page.context["can_resubmit"])
        self.assertContains(page, reverse("fleetops:srp_claim_resubmit", args=[self.claim.uuid]))
        self.assertContains(page, "Submit again")
        # The rejection reason stays on the page until it is resubmitted.
        self.assertContains(page, "Wrong ship")

        for user in (self.manager,):
            with self.subTest(user=user.username):
                self.assertFalse(self.open(user).context["can_resubmit"])

    def test_posting_it_makes_the_claim_pending_and_the_history_keeps_the_rejection(self):
        self.settings(member_resubmit_rejected=True)
        self.client.force_login(self.user)

        response = self.client.post(reverse("fleetops:srp_claim_resubmit", args=[self.claim.uuid]))

        self.assertRedirects(response, reverse("fleetops:srp_claim", args=[self.claim.uuid]), fetch_redirect_response=False)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Status.PENDING)
        page = self.client.get(reverse("fleetops:srp_claim", args=[self.claim.uuid]))
        self.assertContains(page, "Wrong ship")
        self.assertNotContains(page, "Submit again")

    def test_posting_it_while_off_changes_nothing_and_says_why(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("fleetops:srp_claim_resubmit", args=[self.claim.uuid]), follow=True)

        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Status.REJECTED)
        self.assertContains(response, "Only withdrawn claims can be submitted again.")

    def test_the_new_message_has_a_chinese_translation(self):
        from django.utils import translation

        from fleetops import texts
        from fleetops.texts import translate_stored

        stored = texts.SRP_ONLY_WITHDRAWN_OR_REJECTED_RESUBMITTED()
        with translation.override("zh-hans"):
            self.assertEqual(translate_stored(stored), "只能重新提交已撤回或被拒绝的申请。")


STATS_PERMS = f.MEMBER_PERMS + ["fleetops.view_all_stats"]


class FcStatisticsTypeColourTests(TestCase):
    def setUp(self):
        self.viewer = f.create_user("b5_fcstats_viewer", perms=STATS_PERMS)
        self.fc = f.member("b5_fc", perms=f.FC_PERMS, main_name="Colour FC")
        self.client.force_login(self.viewer)

    def chips(self):
        page = self.client.get(reverse("fleetops:all_fc_statistics"), {"year": 2026, "month": 3})
        self.assertEqual(page.status_code, 200)
        return page.content.decode()

    def test_the_chips_carry_the_tone_of_the_type_like_the_other_statistics_pages(self):
        plain = f.fleet_type("PlainType", "1.00")
        closed_op(self.fc, datetime(2026, 3, 2, 18, tzinfo=dt_timezone.utc), type_obj=plain)

        html = self.chips()

        chip = re.search(r'<span class="([^"]*)"[^>]*>PlainType 1 / 1\.00 pts</span>', html)
        self.assertIsNotNone(chip, html[-3000:])
        self.assertRegex(chip.group(1), r"fo-pill fo-pill-type fo-type-[1-6]\b")
        self.assertNotIn("fo-pill-neutral", chip.group(1))

    def test_a_custom_colour_is_the_dot_colour_without_a_tone_class(self):
        custom = f.fleet_type("CustomType", "1.00")
        custom.color = "#e67e22"
        custom.save()
        closed_op(self.fc, datetime(2026, 3, 2, 18, tzinfo=dt_timezone.utc), type_obj=custom)

        html = self.chips()

        chip = re.search(r'<span class="([^"]*)"([^>]*)>CustomType 1 / 1\.00 pts</span>', html)
        self.assertIsNotNone(chip, html[-3000:])
        self.assertIn("--fo-dot: #e67e22", chip.group(2))
        self.assertNotRegex(chip.group(1), r"fo-type-\d")

    def test_the_chip_is_the_pill_of_the_type_helper(self):
        from fleetops.templatetags.fleetops_ui import type_chip, type_pill

        for color in ("", "theme-3", "#e67e22"):
            with self.subTest(color=color):
                kind = f.fleet_type(f"Kind {color or 'auto'}", "1.00")
                kind.color = color
                kind.save()
                self.assertEqual(type_chip(kind, str(kind)), type_pill(kind))

    def test_the_text_of_a_chip_is_escaped(self):
        from fleetops.templatetags.fleetops_ui import type_chip

        kind = f.fleet_type("Roam", "1.00")
        self.assertIn("&lt;b&gt;", type_chip(kind, "<b>x</b>"))

    def test_fc_rows_list_each_type_once_with_its_type_object(self):
        first = f.fleet_type("TypeOne", "1.00")
        second = f.fleet_type("TypeTwo", "2.00")
        for day, kind in ((2, first), (3, second), (4, first)):
            closed_op(self.fc, datetime(2026, 3, day, 18, tzinfo=dt_timezone.utc), type_obj=kind)

        from fleetops.services.statistics import fc_rows

        [row] = fc_rows(2026, 3)

        self.assertEqual([(kind, entry["count"]) for kind, entry in row.types], [(first, 2), (second, 1)])
        self.assertEqual([name for name in row.breakdown], ["TypeOne", "TypeTwo"])


SITE = "https://auth.example.test"


@override_settings(SITE_URL=SITE)
class DefaultPingSrpLineTests(MessageTestBase):
    SRP = "https://auth.example.test/fleetops/srp/links/AbC123/"

    def ping(self, **extra):
        return self.render(self.operation(**extra)).ping

    def test_the_default_has_the_srp_line_with_its_own_line(self):
        self.assertIn("{% if srp_url %}**SRP:** [Request SRP]({{ srp_url }})\n{% endif %}", DEFAULT_PING_TEMPLATE)

    def test_a_fleet_with_a_link_gets_the_line(self):
        ping = self.ping(srp_url=self.SRP)

        self.assertIn(f"\n**SRP:** [Request SRP]({self.SRP})\n", ping)

    def test_a_fleet_without_a_link_gets_no_line_and_no_empty_label(self):
        ping = self.ping()

        self.assertNotIn("SRP", ping)
        self.assertNotIn("\n\n\n", ping)

    def test_a_site_path_gets_the_site_address(self):
        ping = self.ping(srp_url="/fleetops/srp/links/AbC123/")

        self.assertIn(f"**SRP:** [Request SRP]({self.SRP})", ping)

    def test_the_line_comes_before_the_fleet_time_of_a_scheduled_fleet(self):
        operation = self.operation(srp_url=self.SRP, scheduled_at=timezone.now() + timedelta(hours=3))

        ping = self.render(operation, pre_ping=True).ping

        self.assertLess(ping.index("**SRP:**"), ping.index("**Fleet Time:**"))

    def test_an_address_cannot_end_the_masked_link(self):
        ping = self.ping(srp_url="https://other.example.test/a)b(c d>e")

        self.assertIn("[Request SRP](https://other.example.test/a%29b%28c%20d%3Ee)", ping)

    def test_an_address_that_is_not_a_web_link_is_left_out(self):
        for url in ("javascript:alert(1)", "//evil.example.test/x", "ftp://files.example.test/x"):
            with self.subTest(url=url):
                self.assertNotIn("SRP", self.ping(srp_url=url))

    def test_the_link_counts_toward_the_discord_limit(self):
        channel = f.ping_channel()
        base = self.render(self.operation(srp_url=self.SRP, ping_channel=channel))
        without = self.render(self.operation(ping_channel=channel))
        self.assertEqual(base.ping_length - without.ping_length, len(f"**SRP:** [Request SRP]({self.SRP})\n"))
        # A message that fits without the line but not with it is refused like any other ping that is too long.
        room = 2000 - without.ping_length
        text = "x" * (room - 5)
        refused = self.render(self.operation(srp_url=self.SRP, ping_channel=channel, additional_message=text))
        self.assertTrue(refused.blocking)
        fits = self.render(self.operation(ping_channel=channel, additional_message=text))
        self.assertFalse(fits.blocking)

    def test_a_stored_template_is_not_changed_and_has_no_line(self):
        stored = MessageTemplate.objects.get(template_type="ping", name="Default Ping")
        stored.content = DEFAULT_PING_TEMPLATE.replace("{% if srp_url %}**SRP:** [Request SRP]({{ srp_url }})\n{% endif %}", "")
        stored.save()

        ping = self.ping(srp_url=self.SRP)

        self.assertNotIn("**SRP:**", ping)

    def test_a_site_adds_the_variable_itself_and_gets_the_safe_address(self):
        template = MessageTemplate.objects.create(
            template_type="ping", name="Own", content="Fleet {{ fleet_type }}: <{{ srp_url }}>"
        )

        ping = self.ping(srp_url="https://other.example.test/a)b", ping_template=template)

        self.assertEqual(ping, "Fleet StratOp: <https://other.example.test/a%29b>")

    def test_the_fallback_without_a_stored_template_has_the_line(self):
        MessageTemplate.objects.filter(template_type="ping").delete()

        ping = self.ping(srp_url=self.SRP)

        self.assertIn(f"**SRP:** [Request SRP]({self.SRP})", ping)

    def test_the_seed_command_creates_the_new_default_but_never_overwrites(self):
        from django.core.management import call_command

        MessageTemplate.objects.filter(template_type="ping").delete()
        call_command("fleetops_seed", verbosity=0)
        self.assertIn("{{ srp_url }}", MessageTemplate.objects.get(name="Default Ping").content)

        MessageTemplate.objects.filter(name="Default Ping").update(content="mine")
        call_command("fleetops_seed", verbosity=0)
        self.assertEqual(MessageTemplate.objects.get(name="Default Ping").content, "mine")

    def test_the_migrations_do_not_write_the_new_default_into_stored_templates(self):
        import importlib

        data = importlib.import_module("fleetops.migrations.0020_b2_data")
        self.assertNotIn("srp_url", data.NEW_PING)
        self.assertNotEqual(data.NEW_PING, DEFAULT_PING_TEMPLATE)


class DiscordAddressTests(SimpleTestCase):
    def test_only_the_characters_that_break_a_link_are_encoded(self):
        self.assertEqual(
            mentions.discord_link_address("https://a.example.test/p?q=1&r=%20x#f"), "https://a.example.test/p?q=1&r=%20x#f"
        )
        self.assertEqual(mentions.discord_link_address("a b(c)<d>\\e\"f\ng"), "a%20b%28c%29%3Cd%3E%5Ce%22f%0Ag")
        self.assertEqual(mentions.discord_link_address(None), "")
