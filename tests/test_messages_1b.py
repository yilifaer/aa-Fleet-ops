"""Tests for the ping and MOTD texts: default templates, variables, exact mentions and length checks."""

import importlib
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone, translation

from fleetops.constants import (
    DEFAULT_MOTD_TEMPLATE,
    DEFAULT_PING_TEMPLATE,
    MOTD_SOFT_LIMIT,
    SCHEDULE_CANCELLED_TEMPLATE,
)
from fleetops.models import ChannelPreset, CommsPreset, FleetOperation, MessageTemplate
from fleetops.providers import doctrines
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE
from fleetops.providers.pings import send_discord_webhook
from fleetops.services.mentions import allowed_mentions, selection_from_operation
from fleetops.services.messages import (
    MISSING_MENTION_WARNING,
    build_context,
    ping_too_long_message,
    render_cancellation_notice,
    render_messages,
    render_operation_messages,
)

from . import factories as f

UTC = dt_timezone.utc
ZWSP = "​"
ROCKET = "\U0001F680"
SITE = "https://auth.example.org"
DOCTRINE_PATH = "/fittings/doctrine/12/"
CHANNEL_ID = "player_" + "0123456789abcdef" * 2
WEBHOOK_URL = "https://discord.com/api/webhooks/555000222/messages-webhook-token"
Mode = FleetOperation.MentionMode

# 2026-10-12 19:00 UTC; the ping is sent half an hour earlier.
FLEET_TIME = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)
SENT = datetime(2026, 10, 12, 18, 30, tzinfo=UTC)

FITS = [
    {"fit_id": 10, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"},
    {"fit_id": 11, "fit": "DPS Moa 2", "ship_type_id": 623, "ship": "Moa"},
    {"fit_id": 20, "fit": "Logi Osprey", "ship_type_id": 620, "ship": "Osprey"},
]
TWO_ROLES = [{"id": "111", "name": "Sig A"}, {"id": "222", "name": "Sig B"}]


def fittings_reverse():
    return mock.patch.object(doctrines, "reverse", return_value=DOCTRINE_PATH)


def make_template(content, template_type=MessageTemplate.TemplateType.PING):
    return MessageTemplate.objects.create(
        name=f"Template {MessageTemplate.objects.count() + 1}", template_type=template_type, content=content
    )


@override_settings(SITE_URL=SITE)
class MessageTestBase(TestCase):
    def setUp(self):
        f.builtin_default_templates()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Boss Main")
        self.fc_alt = f.add_alt(self.fc, "Boss Alt")
        self.stratop = f.fleet_type("StratOp")
        self.comms = CommsPreset.objects.create(
            name="Main Comms", channel_name="Fleet 1", voice_url="mumble://voice.example/fleet1"
        )
        self.logi = ChannelPreset.objects.create(
            name="Logi", channel_type=ChannelPreset.ChannelType.LOGI, channel_value="Logi Chat",
            ingame_channel_id=CHANNEL_ID,
        )
        self.boost = ChannelPreset.objects.create(
            name="Boost", channel_type=ChannelPreset.ChannelType.BOOST, channel_value="Boosts"
        )

    def join_url(self):
        return f"{SITE}/fleetops/comms/{self.comms.join_token}/join/"

    def operation(self, **extra):
        values = dict(fc_character=self.fc_alt, type_obj=self.stratop, formup="Jita IV-4")
        values.update(extra)
        return f.create_operation(self.fc, **values)

    def full_operation(self, **extra):
        values = dict(
            comms=self.comms,
            logi_channel=self.logi,
            boost_channel=self.boost,
            doctrine_name="Moa Fleet",
            doctrine_source=FITTINGS_DOCTRINE_SOURCE,
            doctrine_external_id="12",
            doctrine_fits=FITS,
            additional_message="Bring drones\nUndock at 19:00",
            scheduled_at=FLEET_TIME,
        )
        values.update(extra)
        return self.operation(**values)

    def render(self, operation, **kwargs):
        kwargs.setdefault("now", SENT)
        with fittings_reverse():
            return render_messages(operation, **kwargs)


class DefaultTemplateTests(TestCase):
    def test_built_in_templates_are_the_copies_in_the_migrations(self):
        upgrade = importlib.import_module("fleetops.migrations.0009_convert_ping_targets_and_presets")

        srp = importlib.import_module("fleetops.migrations.0017_srp_data")

        self.assertEqual(DEFAULT_PING_TEMPLATE.encode(), upgrade.NEW_PING.encode())
        # Migration 0017 added the SRP line to the MOTD; its copy is the current one.
        self.assertEqual(upgrade.NEW_MOTD.encode(), srp.OLD_MOTD.encode())
        self.assertEqual(DEFAULT_MOTD_TEMPLATE.encode(), srp.NEW_MOTD.encode())

    def test_default_ping_has_no_logi_or_boost(self):
        self.assertNotIn("logi", DEFAULT_PING_TEMPLATE.lower())
        self.assertNotIn("boost", DEFAULT_PING_TEMPLATE.lower())

    def test_cancellation_notice_never_mentions_anybody(self):
        self.assertNotIn("ping_mention", SCHEDULE_CANCELLED_TEMPLATE)
        self.assertNotIn("ping_target ", SCHEDULE_CANCELLED_TEMPLATE)
        self.assertEqual(MOTD_SOFT_LIMIT, 1000)


class DefaultPingTests(MessageTestBase):
    def test_full_ping(self):
        operation = self.full_operation()

        rendered = self.render(operation)

        self.assertEqual(
            rendered.ping,
            "Bring drones\n"
            "Undock at 19:00\n"
            "\n"
            "**FC Name:** Boss Alt\n"
            "**Formup Location:** Jita IV-4\n"
            "**PAP Type:** StratOp\n"
            f"**Comms:** [Main Comms]({self.join_url()})\n"
            f"**Doctrine:** [Moa Fleet]({SITE}{DOCTRINE_PATH}) (Moa > Moa > Osprey)\n"
            "\n"
            "-# This was a FleetOps broadcast from Boss Main to this channel at 2026-10-12 18:30 EVE",
        )
        self.assertEqual(rendered.ping_display, rendered.ping)
        self.assertEqual(rendered.errors, [])
        self.assertEqual(rendered.blocking, [])
        self.assertEqual(rendered.warnings, [])

    def test_pre_ping_shows_the_fleet_time(self):
        operation = self.full_operation(additional_message="")

        rendered = self.render(operation, pre_ping=True)

        self.assertEqual(
            rendered.ping,
            "**FC Name:** Boss Alt\n"
            "**Formup Location:** Jita IV-4\n"
            "**PAP Type:** StratOp\n"
            f"**Comms:** [Main Comms]({self.join_url()})\n"
            f"**Doctrine:** [Moa Fleet]({SITE}{DOCTRINE_PATH}) (Moa > Moa > Osprey)\n"
            "**Fleet Time:** 2026-10-12 19:00 EVE (<t:1791831600:R>)\n"
            "\n"
            "-# This was a FleetOps broadcast from Boss Main to this channel at 2026-10-12 18:30 EVE",
        )

    def test_optional_lines_are_left_out(self):
        operation = self.operation(logi_channel=self.logi, boost_channel=self.boost, scheduled_at=FLEET_TIME)

        rendered = self.render(operation)

        # No comms, doctrine, pre ping or additional information: no empty lines or empty labels.
        self.assertEqual(
            rendered.ping,
            "**FC Name:** Boss Alt\n"
            "**Formup Location:** Jita IV-4\n"
            "**PAP Type:** StratOp\n"
            "\n"
            "-# This was a FleetOps broadcast from Boss Main to this channel at 2026-10-12 18:30 EVE",
        )

    def test_doctrine_without_fittings_link_or_fits(self):
        operation = self.operation(doctrine_name="Ferox Fleet", doctrine_source="custom", doctrine_external_id="12")

        ping = self.render(operation).ping

        self.assertIn("\n**Doctrine:** Ferox Fleet\n", ping)

    def test_web_voice_links_are_used_as_they_are(self):
        self.comms.voice_url = "https://discord.gg/fleet"
        self.comms.save()

        ping = self.render(self.operation(comms=self.comms)).ping

        self.assertIn("\n**Comms:** [Main Comms](https://discord.gg/fleet)\n", ping)

    def test_comms_without_voice_link_is_plain_text(self):
        self.comms.voice_url = ""
        self.comms.save()

        ping = self.render(self.operation(comms=self.comms)).ping

        self.assertIn("\n**Comms:** Main Comms\n", ping)

    def test_broken_custom_template_falls_back_without_logi_or_boost(self):
        broken = make_template("{% if %}broken")
        operation = self.full_operation(ping_template=broken)

        rendered = self.render(operation)

        self.assertTrue(rendered.ping.startswith("Bring drones\nUndock at 19:00\n\n**FC Name:** Boss Alt\n"))
        self.assertNotIn("Logi", rendered.ping)
        self.assertNotIn("Boost", rendered.ping)
        self.assertEqual(len(rendered.errors), 1)
        self.assertIn(broken.name, rendered.errors[0])


class FooterTests(MessageTestBase):
    def footer(self, operation, **kwargs):
        return self.render(operation, **kwargs).ping.rsplit("\n", 1)[1]

    def test_target_label_per_mention(self):
        cases = [
            (Mode.NONE, [], "to this channel"),
            (Mode.EVERYONE, [], "to all"),
            (Mode.HERE, [], "to online members"),
            (Mode.ROLES, TWO_ROLES, "to Sig A, Sig B"),
        ]
        for mode, roles, label in cases:
            with self.subTest(mode=mode):
                operation = self.operation(ping_mention_mode=mode, ping_roles=roles)

                self.assertEqual(
                    self.footer(operation),
                    f"-# This was a FleetOps broadcast from Boss Main {label} at 2026-10-12 18:30 EVE",
                )

    def test_sender_is_the_main_of_the_user_sending(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")

        footer = self.footer(self.operation(), actor=lead)

        self.assertIn("from Lead Main to", footer)

    def test_sender_falls_back_to_the_fc(self):
        no_main = f.create_user(with_main=False)

        self.assertIn("from Boss Main to", self.footer(self.operation(), actor=no_main))
        self.assertIn("from Boss Main to", self.footer(self.operation()))
        self.assertIn("from Boss Alt to", self.footer(self.operation(fc_main_character_name="")))

    def test_sent_at_is_the_send_time_in_eve_time(self):
        local = SENT.astimezone(ZoneInfo("Australia/Adelaide"))

        with override_settings(TIME_ZONE="Australia/Adelaide"), timezone.override(ZoneInfo("Australia/Adelaide")):
            footer = self.footer(self.operation(), now=local)

        self.assertTrue(footer.endswith(" at 2026-10-12 18:30 EVE"))

    def test_sent_at_defaults_to_now(self):
        with mock.patch("django.utils.timezone.now", return_value=SENT + timedelta(minutes=7)):
            with fittings_reverse():
                ping = render_messages(self.operation()).ping

        self.assertTrue(ping.endswith(" at 2026-10-12 18:37 EVE"))


class DefaultMotdTests(MessageTestBase):
    def test_full_motd(self):
        operation = self.full_operation()

        motd = self.render(operation).motd

        self.assertEqual(
            motd,
            '<font color="#bfffffff"><b>StratOp</b><br>FC: Boss Alt<br>Form Up: Jita IV-4'
            f'<br>Doctrine: <a href="{SITE}{DOCTRINE_PATH}">Moa Fleet</a> (Moa > Moa > Osprey)'
            f'<br>Comms: <a href="{self.join_url()}">Main Comms</a>'
            f'<br>Logi: <a href="joinChannel:{CHANNEL_ID}">Logi Chat</a>'
            "<br>Boost: Boosts"
            "<br><br>Bring drones<br>Undock at 19:00</font>",
        )

    def test_minimal_motd(self):
        motd = self.render(self.operation(), pre_ping=True).motd

        self.assertEqual(motd, '<font color="#bfffffff"><b>StratOp</b><br>FC: Boss Alt<br>Form Up: Jita IV-4</font>')

    def test_doctrine_link_only_for_fittings_doctrines(self):
        operation = self.full_operation(doctrine_source="fittings:fitting.doctrinegroup", doctrine_fits=[])

        motd = self.render(operation).motd

        self.assertIn("<br>Doctrine: Moa Fleet<br>", motd)
        self.assertNotIn("<a href=\"/fittings", motd)

    def test_line_breaks_keep_the_text_as_it_is(self):
        operation = self.operation(additional_message="Bring <b>drones</b>\r\nand cap\nboosters")

        motd = self.render(operation).motd

        self.assertTrue(motd.endswith("<br><br>Bring <b>drones</b><br>and cap<br>boosters</font>"))

    def test_scripts_stay_neutralised(self):
        operation = self.operation(additional_message="<script>alert(1)</script>")

        motd = self.render(operation).motd

        self.assertNotIn("<script", motd)
        self.assertIn("&lt;script>alert(1)&lt;/script>", motd)


class ContextTests(MessageTestBase):
    def context(self, operation, **kwargs):
        with fittings_reverse():
            return build_context(operation, now=SENT, **kwargs)

    def test_fleet_time_is_eve_time_whatever_the_site_and_language(self):
        operation = self.operation(scheduled_at=FLEET_TIME)
        adelaide = ZoneInfo("Australia/Adelaide")

        with override_settings(TIME_ZONE="Australia/Adelaide"), timezone.override(adelaide), translation.override(
            "zh-hans"
        ):
            operation.refresh_from_db()
            values = self.context(operation)
            rendered = self.render(operation, pre_ping=True, now=SENT.astimezone(adelaide))

        self.assertEqual(values["fleet_time_eve"], "2026-10-12 19:00 EVE")
        self.assertEqual(values["fleet_time_unix"], "1791831600")
        self.assertEqual(values["fleet_time_discord"], "<t:1791831600:R>")
        self.assertEqual(values["fleet_time"], "2026-10-12 19:00 EVE")
        self.assertEqual(values["sent_at"], "2026-10-12 18:30")
        self.assertIn("**Fleet Time:** 2026-10-12 19:00 EVE (<t:1791831600:R>)\n", rendered.ping)
        self.assertTrue(rendered.ping.endswith(" at 2026-10-12 18:30 EVE"))

    def test_no_fleet_time(self):
        values = self.context(self.operation())

        self.assertEqual(
            [values[key] for key in ("fleet_time_eve", "fleet_time_unix", "fleet_time_discord", "fleet_time")],
            ["", "", "", "Now"],
        )
        self.assertIs(values["is_pre_ping"], False)
        self.assertIs(self.context(self.operation(), pre_ping=True)["is_pre_ping"], True)

    def test_doctrine_values(self):
        operation = self.full_operation(
            doctrine_fits=FITS + [{"fit_id": 30, "fit": "Booster Drake", "ship_type_id": 24698, "ship": "Drake"}]
        )

        values = self.context(operation)

        self.assertEqual(values["doctrine"], "Moa Fleet")
        self.assertEqual(values["doctrine_name"], "Moa Fleet")
        self.assertEqual(values["doctrine_url"], f"{SITE}{DOCTRINE_PATH}")
        # Every fit's hull in doctrine order, duplicates kept.
        self.assertEqual(values["doctrine_ships"], "Moa > Moa > Osprey > Drake")
        self.assertEqual(values["doctrine_fits"], "DPS Moa > DPS Moa 2 > Logi Osprey > Booster Drake")

    def test_doctrine_values_without_a_doctrine(self):
        values = self.context(self.operation(doctrine_fits=[{"fit": "Lonely", "ship_type_id": 623}, "junk", {}]))

        self.assertEqual(values["doctrine"], "None / Custom")
        self.assertEqual(values["doctrine_name"], "")
        self.assertEqual(values["doctrine_url"], "")
        self.assertEqual(values["doctrine_ships"], "623")
        self.assertEqual(values["doctrine_fits"], "Lonely")

    def test_comms_and_channel_values(self):
        values = self.context(self.full_operation())

        self.assertEqual(values["comms"], "Main Comms")
        self.assertEqual(values["comms_channel"], "Fleet 1")
        self.assertEqual(values["comms_link"], self.join_url())
        self.assertEqual(values["comms_voice_url"], "mumble://voice.example/fleet1")
        self.assertEqual(values["logi_channel"], "Logi Chat")
        self.assertEqual(values["logi_channel_link"], f'<a href="joinChannel:{CHANNEL_ID}">Logi Chat</a>')
        self.assertEqual(values["boost_channel"], "Boosts")
        self.assertEqual(values["boost_channel_link"], "Boosts")

    def test_empty_comms_and_channel_values(self):
        values = self.context(self.operation())

        for key in ("comms", "comms_channel", "comms_link", "comms_voice_url", "logi_channel", "logi_channel_link",
                    "boost_channel", "boost_channel_link", "ping_channel"):
            with self.subTest(key=key):
                self.assertEqual(values[key], "")

    def test_ping_values(self):
        channel = f.ping_channel("Fleet Pings")
        operation = self.operation(ping_channel=channel, ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES)

        values = self.context(operation)
        display = self.context(operation, display=True)

        self.assertEqual(values["ping_mention"], "<@&111> <@&222>")
        self.assertEqual(values["ping_target"], "<@&111> <@&222>")
        self.assertEqual(display["ping_mention"], "@Sig A @Sig B")
        self.assertEqual(values["ping_target_label"], "Sig A, Sig B")
        self.assertEqual(values["ping_target_name"], "Sig A, Sig B")
        self.assertEqual(values["ping_channel"], "Fleet Pings")

    def test_ping_channel_of_an_unsaved_fleet(self):
        channel = f.ping_channel("Fleet Pings")
        operation = FleetOperation(
            fc_character_name="Boss Alt", fleet_type=self.stratop, formup="Jita", ping_channel=channel
        )

        self.assertEqual(self.context(operation)["ping_channel"], "Fleet Pings")

    def test_unchanged_values(self):
        operation = self.full_operation()

        values = self.context(operation)

        self.assertEqual(values["operation_id"], str(operation.uuid))
        self.assertEqual(values["fc"], "Boss Alt")
        self.assertEqual(values["fc_main"], "Boss Main")
        self.assertEqual(values["sender"], "Boss Main")
        self.assertEqual(values["fleet_type"], "StratOp")
        self.assertEqual(values["formup"], "Jita IV-4")
        self.assertEqual(values["additional_message"], "Bring drones\nUndock at 19:00")

    def test_values_are_plain_text(self):
        values = self.context(self.full_operation(ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES))

        for key, value in values.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, bool if key == "is_pre_ping" else str)

    def test_custom_templates_see_the_new_variables(self):
        ping_template = make_template(
            "{{ ping_target }}|{{ ping_target_label }}|{{ ping_channel }}|{{ doctrine_fits }}|{{ fleet_time_unix }}"
        )
        motd_template = make_template(
            "{{ logi_channel_link }} {{ boost_channel_link }} {{ comms_voice_url }}", MessageTemplate.TemplateType.MOTD
        )
        operation = self.full_operation(
            ping_template=ping_template,
            motd_template=motd_template,
            ping_channel=f.ping_channel("Fleet Pings"),
            ping_mention_mode=Mode.HERE,
        )

        rendered = self.render(operation)

        self.assertEqual(
            rendered.ping, "@here|online members|Fleet Pings|DPS Moa > DPS Moa 2 > Logi Osprey|1791831600"
        )
        self.assertEqual(
            rendered.motd, f'<a href="joinChannel:{CHANNEL_ID}">Logi Chat</a> Boosts mumble://voice.example/fleet1'
        )


class ExactMentionTests(MessageTestBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text=""))
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, operation, **kwargs):
        rendered = self.render(operation, **kwargs)
        send_discord_webhook(WEBHOOK_URL, rendered.ping, allowed_mentions(selection_from_operation(operation)))
        return rendered, self.post.call_args.kwargs["json"]

    def test_payload_per_mention(self):
        cases = [
            (Mode.NONE, [], "", {"parse": []}),
            (Mode.EVERYONE, [], "@everyone\n", {"parse": ["everyone"]}),
            (Mode.HERE, [], "@here\n", {"parse": ["everyone"]}),
            (Mode.ROLES, TWO_ROLES, "<@&111> <@&222>\n", {"parse": [], "roles": ["111", "222"]}),
        ]
        for mode, roles, start, allowed in cases:
            with self.subTest(mode=mode):
                rendered, payload = self.send(self.operation(ping_mention_mode=mode, ping_roles=roles))

                self.assertEqual(payload, {"content": rendered.ping, "allowed_mentions": allowed, "flags": 4})
                self.assertTrue(rendered.ping.startswith(f"{start}**FC Name:** Boss Alt\n"))

    def test_roles_are_shown_by_name(self):
        rendered = self.render(self.operation(ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES))

        self.assertTrue(rendered.ping_display.startswith("@Sig A @Sig B\n**FC Name:** Boss Alt\n"))
        self.assertEqual(rendered.ping_display.split("\n")[1:], rendered.ping.split("\n")[1:])

    def test_mass_mentions_in_the_additional_message_are_neutralised(self):
        operation = self.operation(ping_mention_mode=Mode.HERE, additional_message="@everyone undock, @here too")

        rendered, payload = self.send(operation)

        self.assertTrue(rendered.ping.startswith(f"@here\n@{ZWSP}everyone undock, @{ZWSP}here too\n\n"))
        self.assertEqual(rendered.ping.count("@here"), 1)
        self.assertNotIn("@everyone", rendered.ping)
        self.assertNotIn("@everyone", rendered.ping_display)
        self.assertEqual(payload["allowed_mentions"], {"parse": ["everyone"]})

    def test_mass_mentions_without_a_selected_mention_are_neutralised(self):
        operation = self.operation(additional_message="@everyone", formup="@here Jita")

        rendered, payload = self.send(operation)

        self.assertNotIn("@everyone", rendered.ping)
        self.assertNotIn("@here", rendered.ping)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_mass_mentions_in_a_custom_template_are_neutralised(self):
        template = make_template("{{ ping_mention }} @everyone {% if ping_mention %}@here{% endif %} {{ formup }}")
        operation = self.operation(ping_template=template, ping_mention_mode=Mode.HERE)

        rendered = self.render(operation)

        self.assertEqual(rendered.ping, f"@here @{ZWSP}everyone @{ZWSP}here Jita IV-4")
        self.assertEqual(rendered.warnings, [])

    def test_role_typed_in_the_text_is_not_allowed(self):
        operation = self.operation(
            ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES[:1], additional_message="and <@&999>"
        )

        rendered, payload = self.send(operation)

        self.assertIn("<@&999>", rendered.ping)
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "roles": ["111"]})

    def test_template_without_the_mention(self):
        template = make_template("{{ fc }} at {{ formup }}")
        for mode, roles, warned in (
            (Mode.EVERYONE, [], True),
            (Mode.ROLES, TWO_ROLES, True),
            (Mode.NONE, [], False),
            (Mode.ROLES, [], False),
        ):
            with self.subTest(mode=mode, roles=roles):
                operation = self.operation(ping_template=template, ping_mention_mode=mode, ping_roles=roles)

                rendered = self.render(operation)

                self.assertEqual(rendered.ping, "Boss Alt at Jita IV-4")
                self.assertEqual(rendered.warnings, [MISSING_MENTION_WARNING] if warned else [])
        self.assertEqual(
            MISSING_MENTION_WARNING, "The ping template does not contain {{ ping_mention }}; nobody will be notified."
        )

    def test_mention_survives_filters(self):
        template = make_template("{{ ping_mention|upper }} {{ ping_target|escape }}")
        operation = self.operation(ping_template=template, ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES[:1])

        rendered = self.render(operation)

        self.assertEqual(rendered.ping, "<@&111> <@&111>")
        self.assertEqual(rendered.ping_display, "@Sig A @Sig A")

    def test_free_text_cannot_fake_the_mention(self):
        # Template code typed by the FC is never evaluated, so it cannot reach the mention placeholder.
        operation = self.operation(
            ping_mention_mode=Mode.EVERYONE, additional_message="{{ ping_mention }} \x02000000000000\x03"
        )

        rendered = self.render(operation)

        self.assertEqual(rendered.ping.count("@everyone"), 1)
        self.assertIn("{{ ping_mention }} \x02000000000000\x03", rendered.ping)

    def test_motd_shows_the_mention_by_name(self):
        template = make_template("{{ ping_mention }} {{ formup }}", MessageTemplate.TemplateType.MOTD)
        operation = self.operation(motd_template=template, ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES)

        self.assertEqual(self.render(operation).motd, "@Sig A @Sig B Jita IV-4")


class LengthTests(MessageTestBase):
    def setUp(self):
        super().setUp()
        self.raw = make_template("{{ additional_message }}")
        self.short_motd = make_template("{{ fc }}", MessageTemplate.TemplateType.MOTD)

    def operation(self, **extra):
        extra.setdefault("motd_template", self.short_motd)
        return super().operation(**extra)

    def test_limit_is_inclusive(self):
        operation = self.operation(
            ping_template=self.raw, additional_message="x" * 1998 + ROCKET, ping_channel=f.ping_channel()
        )

        rendered = self.render(operation)

        self.assertEqual(rendered.ping_length, 2000)
        self.assertEqual(rendered.blocking, [])
        self.assertEqual(rendered.warnings, [])

    def test_over_the_limit_blocks_a_ping_to_a_channel(self):
        operation = self.operation(
            ping_template=self.raw, additional_message="x" * 1999 + ROCKET, ping_channel=f.ping_channel()
        )

        rendered = self.render(operation)

        self.assertEqual(len(rendered.ping), 2000)
        self.assertEqual(rendered.ping_length, 2001)
        self.assertEqual(
            rendered.blocking,
            ["The ping is 2001 characters; Discord accepts at most 2000. Shorten the additional information."],
        )
        self.assertEqual(rendered.warnings, [])

    def test_over_the_limit_is_a_warning_for_a_copy_only_ping(self):
        operation = self.operation(ping_template=self.raw, additional_message="x" * 2001)

        rendered = self.render(operation)

        self.assertEqual(rendered.blocking, [])
        self.assertEqual(rendered.warnings, [ping_too_long_message(2001)])

    def test_send_time_guard_matches_the_render_check(self):
        operation = self.operation(ping_template=self.raw, additional_message="x" * 1999 + ROCKET)
        rendered = self.render(operation)

        with mock.patch("fleetops.providers.pings.requests.post") as post:
            result = send_discord_webhook(WEBHOOK_URL, rendered.ping)

        post.assert_not_called()
        self.assertFalse(result.success)
        self.assertEqual(result.message, ping_too_long_message(2001))

    def test_neutralised_mentions_count(self):
        operation = self.operation(ping_template=self.raw, additional_message="@here" + "x" * 1995)

        rendered = self.render(operation)

        self.assertEqual(rendered.ping_length, 2001)
        self.assertEqual(rendered.warnings, [ping_too_long_message(2001)])

    def test_role_mentions_count_as_sent(self):
        operation = self.operation(
            ping_template=make_template("{{ ping_mention }}"), ping_mention_mode=Mode.ROLES, ping_roles=TWO_ROLES
        )

        rendered = self.render(operation)

        self.assertEqual(rendered.ping_length, len("<@&111> <@&222>"))

    def test_long_motd_is_a_warning(self):
        motd = make_template("{{ additional_message }}", MessageTemplate.TemplateType.MOTD)
        for length, warned in ((1000, False), (1001, True)):
            with self.subTest(length=length):
                rendered = self.render(self.operation(motd_template=motd, additional_message="m" * length))

                self.assertEqual(rendered.motd_length, length)
                self.assertEqual(rendered.blocking, [])
                self.assertEqual(
                    rendered.warnings,
                    ["The MOTD is 1001 characters; the game may cut a MOTD longer than 1000."] if warned else [],
                )


class SiteUrlWarningTests(MessageTestBase):
    warning = "SITE_URL is not set, so the comms and doctrine links in the messages are incomplete."

    @override_settings(SITE_URL="")
    def test_relative_links_are_reported(self):
        operation = self.full_operation()

        rendered = self.render(operation)

        self.assertIn(f"[Main Comms](/fleetops/comms/{self.comms.join_token}/join/)", rendered.ping)
        self.assertEqual(rendered.warnings, [self.warning])

    @override_settings(SITE_URL="")
    def test_only_links_in_the_messages_count(self):
        self.comms.voice_url = "https://discord.gg/fleet"
        self.comms.save()
        unused = make_template("{{ fc }}")
        operation = self.full_operation(
            ping_template=unused, motd_template=make_template("{{ fc }}", MessageTemplate.TemplateType.MOTD)
        )

        self.assertEqual(self.render(operation).warnings, [])

    def test_absolute_links_are_fine(self):
        self.assertEqual(self.render(self.full_operation()).warnings, [])


class CancellationNoticeTests(MessageTestBase):
    def test_notice(self):
        lead = f.create_user(perms=f.FC_LEAD_PERMS, main_name="Lead Main")
        operation = f.create_scheduled_operation(
            self.fc, scheduled_at=FLEET_TIME, type_obj=self.stratop, formup="Jita IV-4", ping_mention_mode=Mode.EVERYONE
        )

        text = render_cancellation_notice(operation, actor=lead, now=SENT)

        self.assertEqual(
            text,
            "**Cancelled:** StratOp at 2026-10-12 19:00 EVE (Jita IV-4)\n"
            "-# FleetOps notice from Lead Main at 2026-10-12 18:30 EVE",
        )

    def test_notice_never_mentions_anybody(self):
        operation = f.create_scheduled_operation(
            self.fc, scheduled_at=FLEET_TIME, type_obj=self.stratop, formup="@everyone Jita"
        )

        text = render_cancellation_notice(operation, now=SENT)

        self.assertIn(f"(@{ZWSP}everyone Jita)", text)
        self.assertIn("notice from Boss Main at", text)


class CompatibilityTests(MessageTestBase):
    def test_wrapper_renders_like_render_messages(self):
        operation = self.full_operation(ping_mention_mode=Mode.EVERYONE)

        with fittings_reverse():
            ping, motd = render_operation_messages(operation)

        self.assertTrue(ping.startswith("@everyone\nBring drones\n"))
        self.assertTrue(motd.startswith('<font color="#bfffffff">'))

    def test_stored_default_templates_are_used(self):
        MessageTemplate.objects.filter(name="Default Ping").update(content="STORED {{ sender }}")

        self.assertEqual(self.render(self.operation()).ping, "STORED Boss Main")
