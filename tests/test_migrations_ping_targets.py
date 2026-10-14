# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Upgrade steps of migrations 0008-0010: ping targets, channel links, voice links and default templates."""

import importlib
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from fleetops.models import ChannelPreset, CommsPreset, FleetOperation, MessageTemplate, PingRole, PingTarget
from fleetops.services.messages import render_messages

from . import factories as f

upgrade = importlib.import_module("fleetops.migrations.0009_convert_ping_targets_and_presets")

CHANNEL_ID = "player_6f0ee87a6ac311ee9fb6b8ca3a65d540"
GAME_HTML = (
    '<font size="13" color="#bfffffff"></font><font size="13" color="#ffffe400"><loc>'
    f'<a href="joinChannel:{CHANNEL_ID}">Logi &amp; Reps</a></loc></font>'
)


class ConvertPingTargetsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fc = f.create_user(perms=f.FC_PERMS)

    def target(self, name, value, webhook=None, **extra):
        target = PingTarget.objects.create(name=name, target_value=value, webhook=webhook, **extra)
        operation = f.create_operation(self.fc, ping_target=target)
        return target, operation

    def convert(self):
        upgrade.convert_ping_targets(apps, None)

    def test_everyone_target_allows_everyone_in_its_channel(self):
        channel = f.ping_channel("Main Pings")
        _target, operation = self.target("Everyone", "@everyone", channel)

        self.convert()

        channel.refresh_from_db()
        operation.refresh_from_db()
        self.assertTrue(channel.allow_everyone)
        self.assertFalse(channel.allow_here)
        self.assertEqual(operation.ping_channel, channel)
        self.assertEqual(operation.ping_channel_name, "Main Pings")
        self.assertEqual(operation.ping_mention_mode, "everyone")
        self.assertEqual(operation.ping_roles, [])
        self.assertFalse(PingRole.objects.exists())

    def test_here_target_allows_here_in_its_channel(self):
        channel = f.ping_channel("Online Pings")
        _target, operation = self.target("Online", "@here", channel)

        self.convert()

        channel.refresh_from_db()
        operation.refresh_from_db()
        self.assertFalse(channel.allow_everyone)
        self.assertTrue(channel.allow_here)
        self.assertEqual(operation.ping_mention_mode, "here")

    def test_one_role_takes_the_target_name(self):
        channel = f.ping_channel("Sig Pings")
        _target, operation = self.target("Sig A", "<@&123456789012345678> form up", channel)

        self.convert()

        role = PingRole.objects.get()
        self.assertEqual((role.role_id, role.name, role.source, role.is_active), (123456789012345678, "Sig A", "manual", True))
        channel.refresh_from_db()
        self.assertFalse(channel.allow_everyone or channel.allow_here)
        self.assertFalse(channel.allowed_roles.exists())
        operation.refresh_from_db()
        self.assertEqual(operation.ping_mention_mode, "roles")
        self.assertEqual(operation.ping_roles, [{"id": "123456789012345678", "name": "Sig A"}])

    def test_two_roles_are_named_by_their_ids(self):
        channel = f.ping_channel("Sig Pings")
        _target, operation = self.target("Sigs", "<@&222> <@&111> <@&222>", channel)

        self.convert()

        self.assertEqual(
            sorted(PingRole.objects.values_list("role_id", "name")), [(111, "Role 111"), (222, "Role 222")]
        )
        operation.refresh_from_db()
        self.assertEqual(
            operation.ping_roles, [{"id": "222", "name": "Role 222"}, {"id": "111", "name": "Role 111"}]
        )

    def test_known_role_keeps_its_name(self):
        f.ping_role("Capitals", role_id=555)
        _target, operation = self.target("Caps", "<@&555>", f.ping_channel())

        self.convert()

        self.assertEqual(PingRole.objects.get().name, "Capitals")
        operation.refresh_from_db()
        self.assertEqual(operation.ping_roles, [{"id": "555", "name": "Capitals"}])

    def test_empty_and_plain_text_targets_ping_nobody(self):
        channel = f.ping_channel("Quiet")
        _empty, empty_operation = self.target("Channel only", "", channel)
        _text, text_operation = self.target("Text", "Form up in Jita", channel)

        self.convert()

        channel.refresh_from_db()
        self.assertFalse(channel.allow_everyone or channel.allow_here)
        for operation in (empty_operation, text_operation):
            operation.refresh_from_db()
            self.assertEqual(operation.ping_channel, channel)
            self.assertEqual(operation.ping_mention_mode, "none")
            self.assertEqual(operation.ping_roles, [])
        self.assertFalse(PingRole.objects.exists())

    def test_target_without_webhook_keeps_the_mention_only(self):
        other = f.ping_channel("Unrelated")
        _target, operation = self.target("Copy only", "@everyone")

        self.convert()

        operation.refresh_from_db()
        self.assertIsNone(operation.ping_channel)
        self.assertEqual(operation.ping_channel_name, "")
        self.assertEqual(operation.ping_mention_mode, "everyone")
        other.refresh_from_db()
        self.assertFalse(other.allow_everyone)

    def test_inactive_target_does_not_allow_mass_mentions(self):
        channel = f.ping_channel("Old Pings")
        _target, operation = self.target("Old everyone", "@everyone", channel, is_active=False)

        self.convert()

        channel.refresh_from_db()
        self.assertFalse(channel.allow_everyone)
        operation.refresh_from_db()
        self.assertEqual(operation.ping_mention_mode, "everyone")

    def test_out_of_range_role_ids_are_ignored(self):
        _target, operation = self.target("Broken", "<@&99999999999999999999>", f.ping_channel())

        self.convert()

        self.assertFalse(PingRole.objects.exists())
        operation.refresh_from_db()
        self.assertEqual(operation.ping_mention_mode, "roles")
        self.assertEqual(operation.ping_roles, [])

    def test_fleets_with_a_channel_are_left_alone_and_a_second_run_changes_nothing(self):
        channel = f.ping_channel("Main Pings")
        newer = f.ping_channel("Newer")
        target, operation = self.target("Everyone", "@everyone", channel)
        converted = f.create_operation(self.fc, ping_target=target, ping_channel=newer, ping_mention_mode="here")

        self.convert()
        self.convert()

        converted.refresh_from_db()
        self.assertEqual((converted.ping_channel, converted.ping_mention_mode), (newer, "here"))
        operation.refresh_from_db()
        self.assertEqual((operation.ping_channel, operation.ping_mention_mode), (channel, "everyone"))
        self.assertEqual(PingRole.objects.count(), 0)


class PresetUpgradeTests(TestCase):
    def test_channel_html_becomes_link_and_name(self):
        anchor = ChannelPreset.objects.create(name="Logi", channel_type="logi", channel_value=GAME_HTML)
        url_tag = ChannelPreset.objects.create(
            name="Boost",
            channel_type="boost",
            channel_value=f"<url=joinChannel:{CHANNEL_ID.upper().replace('PLAYER', 'player')}//None//None>Boosts</url>",
        )
        bare = ChannelPreset.objects.create(name="Bare", channel_type="logi", channel_value=f"joinChannel:{CHANNEL_ID}")
        plain = ChannelPreset.objects.create(name="Plain", channel_type="boost", channel_value="boost-channel")
        lower = ChannelPreset.objects.create(
            name="Lower", channel_type="boost", channel_value=f'<a href="joinchannel:{CHANNEL_ID}">Links</a>'
        )

        upgrade.extract_channel_links(apps, None)

        for preset in (anchor, url_tag, bare, plain, lower):
            preset.refresh_from_db()
        self.assertEqual((anchor.ingame_channel_id, anchor.channel_value), (CHANNEL_ID, "Logi & Reps"))
        self.assertEqual(url_tag.ingame_channel_id.lower(), CHANNEL_ID)
        self.assertEqual(url_tag.channel_value, "Boosts")
        self.assertEqual((bare.ingame_channel_id, bare.channel_value), (CHANNEL_ID, "Bare"))
        self.assertEqual((plain.ingame_channel_id, plain.channel_value), ("", "boost-channel"))
        self.assertEqual((lower.ingame_channel_id, lower.channel_value), (CHANNEL_ID, "Links"))

    def test_user_names_and_passwords_are_removed_from_voice_links(self):
        cases = {
            "mumble://fc.alpha@voice.example.com:64738/Ops?version=1.2.0": "mumble://voice.example.com:64738/Ops?version=1.2.0",
            "mumble://fc:hunter2@voice.example.com/": "mumble://voice.example.com/",
            # The password ends at the last "@" of the host part.
            "mumble://fc:p@ss@voice.example.com/Ops": "mumble://voice.example.com/Ops",
            "mumble://:secret@voice.example.com/": "mumble://voice.example.com/",
            "mumble://voice.example.com/Ops": "mumble://voice.example.com/Ops",
            "https://voice.example.com/join?user=a@b": "https://voice.example.com/join?user=a@b",
            "": "",
        }
        presets = {
            url: CommsPreset.objects.create(name=f"Comms {index}", voice_url=url) for index, url in enumerate(cases)
        }

        upgrade.strip_voice_userinfo(apps, None)

        for url, expected in cases.items():
            with self.subTest(url=url):
                presets[url].refresh_from_db()
                self.assertEqual(presets[url].voice_url, expected)

    def test_spaces_in_voice_links_are_percent_encoded(self):
        cases = {
            "mumble://voice.example.com/Fleet Ops": "mumble://voice.example.com/Fleet%20Ops",
            " ts3server://ts.example.com?channel=Fleet One ": "ts3server://ts.example.com?channel=Fleet%20One",
            "mumble://voice.example.com/Fleet\tOps": "mumble://voice.example.com/Fleet%09Ops",
            "mumble://voice.example.com/Ops": "mumble://voice.example.com/Ops",
            # Not a voice link FleetOps can offer; reported in the upgrade notes instead.
            "ts.example.com:9987": "ts.example.com:9987",
            "voice server one": "voice server one",
            "": "",
        }
        presets = {
            url: CommsPreset.objects.create(name=f"Comms {index}", voice_url=url) for index, url in enumerate(cases)
        }

        upgrade.encode_voice_url_spaces(apps, None)

        for url, expected in cases.items():
            with self.subTest(url=url):
                presets[url].refresh_from_db()
                self.assertEqual(presets[url].voice_url, expected)
        presets["mumble://voice.example.com/Fleet Ops"].full_clean()

    def test_join_tokens_are_kept(self):
        first = CommsPreset.objects.create(name="One")
        second = CommsPreset.objects.create(name="Two")
        tokens = (first.join_token, second.join_token)

        upgrade.fill_join_tokens(apps, None)

        self.assertNotEqual(*tokens)
        self.assertEqual(tuple(CommsPreset.objects.order_by("name").values_list("join_token", flat=True)), tokens)


class DefaultTemplateUpgradeTests(TestCase):
    def set_content(self, template_type, name, content):
        MessageTemplate.objects.update_or_create(
            template_type=template_type, name=name, defaults={"content": content}
        )

    def content(self, template_type, name):
        return MessageTemplate.objects.get(template_type=template_type, name=name).content

    def test_untouched_defaults_are_replaced(self):
        self.set_content("ping", "Default Ping", upgrade.OLD_PING)
        self.set_content("motd", "Default MOTD", upgrade.OLD_MOTD.replace("\n", "\r\n"))

        upgrade.update_untouched_default_templates(apps, None)

        self.assertEqual(self.content("ping", "Default Ping"), upgrade.NEW_PING)
        self.assertEqual(self.content("motd", "Default MOTD"), upgrade.NEW_MOTD)

    def test_defaults_edited_with_crlf_line_ends_are_replaced(self):
        self.set_content("ping", "Default Ping", upgrade.OLD_PING.replace("\n", "\r\n"))

        upgrade.update_untouched_default_templates(apps, None)

        self.assertEqual(self.content("ping", "Default Ping"), upgrade.NEW_PING)

    def test_customised_and_other_templates_are_left_alone(self):
        custom = upgrade.OLD_PING.replace("FC: {{ fc }}", "Fleet commander: {{ fc }}")
        self.set_content("ping", "Default Ping", custom)
        self.set_content("motd", "Default MOTD", "<b>{{ fleet_type }}</b>")
        self.set_content("ping", "Copy of Default Ping", upgrade.OLD_PING)

        upgrade.update_untouched_default_templates(apps, None)

        self.assertEqual(self.content("ping", "Default Ping"), custom)
        self.assertEqual(self.content("motd", "Default MOTD"), "<b>{{ fleet_type }}</b>")
        self.assertEqual(self.content("ping", "Copy of Default Ping"), upgrade.OLD_PING)

    def test_channel_names_in_motd_templates_become_channel_links(self):
        edited = upgrade.OLD_MOTD.replace("Form Up:", "Staging:")
        self.set_content("motd", "Default MOTD", edited)
        self.set_content("motd", "Custom MOTD", "Logi: {{logi_channel}}<br>Boost: {{  boost_channel }}{% if logi_channel %}!{% endif %}")
        self.set_content("ping", "Default Ping", upgrade.OLD_PING)

        upgrade.update_untouched_default_templates(apps, None)
        upgrade.link_channels_in_motd_templates(apps, None)

        self.assertEqual(
            self.content("motd", "Default MOTD"),
            edited.replace("{{ logi_channel }}", "{{ logi_channel_link }}").replace(
                "{{ boost_channel }}", "{{ boost_channel_link }}"
            ),
        )
        self.assertEqual(
            self.content("motd", "Custom MOTD"),
            "Logi: {{ logi_channel_link }}<br>Boost: {{ boost_channel_link }}{% if logi_channel %}!{% endif %}",
        )
        # Discord gets the plain name; the new defaults already use the link variables.
        self.assertEqual(self.content("ping", "Default Ping"), upgrade.NEW_PING)

    def test_edited_motd_keeps_the_in_game_channel_link(self):
        logi = ChannelPreset.objects.create(name="Logi", channel_type="logi", channel_value=GAME_HTML)
        self.set_content("motd", "Default MOTD", upgrade.OLD_MOTD.replace("Form Up:", "Staging:"))
        operation = f.create_operation(
            f.create_user(perms=f.FC_PERMS),
            logi_channel=logi,
            motd_template=MessageTemplate.objects.get(template_type="motd", name="Default MOTD"),
        )
        link = f'<a href="joinChannel:{CHANNEL_ID}">Logi &amp; Reps</a>'
        self.assertIn(link, render_messages(operation).motd)

        upgrade.extract_channel_links(apps, None)
        upgrade.update_untouched_default_templates(apps, None)
        upgrade.link_channels_in_motd_templates(apps, None)

        operation = FleetOperation.objects.get(pk=operation.pk)
        self.assertIn(f"Logi: {link}", render_messages(operation).motd)

    def test_new_defaults_are_valid_templates(self):
        for content in (upgrade.NEW_PING, upgrade.NEW_MOTD):
            MessageTemplate(name="Check", template_type="ping", content=content).full_clean()
        self.assertNotIn("logi", upgrade.NEW_PING.lower())
        self.assertNotIn("boost", upgrade.NEW_PING.lower())
        self.assertTrue(upgrade.NEW_MOTD.startswith('<font color="#bfffffff">'))


class UpgradeFrom0007Tests(TransactionTestCase):
    """Migrates a database holding pre-release development data from 0007 to 0010."""

    serialized_rollback = True
    start = [("fleetops", "0007_end_reasons_and_step_history")]
    target = [("fleetops", "0010_comms_join_token_unique")]

    def migrate(self, nodes):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(nodes)
        return executor.loader.project_state(nodes).apps

    def setUp(self):
        super().setUp()
        self.addCleanup(self.migrate_to_latest)
        old_apps = self.migrate(self.start)
        self.insert_0007_rows(old_apps)
        self.new_apps = self.migrate(self.target)

    def migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("fleetops"))

    def insert_0007_rows(self, old_apps):
        User = old_apps.get_model("auth", "User")
        FleetType = old_apps.get_model("fleetops", "FleetType")
        Webhook = old_apps.get_model("fleetops", "DiscordWebhook")
        Target = old_apps.get_model("fleetops", "PingTarget")
        Operation = old_apps.get_model("fleetops", "FleetOperation")
        Comms = old_apps.get_model("fleetops", "CommsPreset")
        Channel = old_apps.get_model("fleetops", "ChannelPreset")
        Template = old_apps.get_model("fleetops", "MessageTemplate")

        user = User.objects.create(username="legacy-fc")
        fleet_type = FleetType.objects.create(name="Legacy Type", point_weight=Decimal("1.00"))
        webhook = Webhook.objects.create(name="Legacy Pings", webhook_url="https://discord.com/api/webhooks/1/legacy-token")
        target = Target.objects.create(name="Sig Legacy", target_value="<@&4242>", webhook=webhook)
        Target.objects.create(name="Legacy Here", target_value="@here", webhook=webhook)
        started = timezone.now() - timedelta(days=3)
        Operation.objects.create(
            created_by=user,
            fc_user=user,
            fc_character_id=90_123_456,
            fc_character_name="Legacy FC",
            fleet_type=fleet_type,
            formup="Jita",
            status="closed",
            ping_target=target,
            started_at=started,
            ended_at=started + timedelta(hours=1),
        )
        Comms.objects.create(name="Legacy Mumble", voice_url="mumble://fc.alpha:pw@voice.example.com/Ops")
        Comms.objects.create(name="Legacy Web", voice_url="https://voice.example.com/")
        Comms.objects.create(name="Legacy Room", voice_url="mumble://voice.example.com/Fleet Ops")
        Channel.objects.create(name="Legacy Logi", channel_type="logi", channel_value=GAME_HTML)
        Template.objects.update_or_create(
            template_type="ping",
            name="Default Ping",
            defaults={"content": upgrade.OLD_PING.replace("\n", "\r\n"), "is_default": True, "is_active": True},
        )
        Template.objects.update_or_create(
            template_type="motd",
            name="Default MOTD",
            defaults={"content": "<b>Custom MOTD</b>", "is_default": True, "is_active": True},
        )
        Template.objects.create(template_type="motd", name="Custom MOTD", content="Logi: {{ logi_channel }}")

    def test_rows_from_0007_are_converted(self):
        Comms = self.new_apps.get_model("fleetops", "CommsPreset")
        tokens = list(Comms.objects.values_list("join_token", flat=True))
        self.assertEqual(len(tokens), 3)
        self.assertNotIn(None, tokens)
        self.assertEqual(len(set(tokens)), 3)
        self.assertEqual(
            Comms.objects.get(name="Legacy Mumble").voice_url, "mumble://voice.example.com/Ops"
        )
        self.assertEqual(Comms.objects.get(name="Legacy Room").voice_url, "mumble://voice.example.com/Fleet%20Ops")

        Channel = self.new_apps.get_model("fleetops", "ChannelPreset")
        logi = Channel.objects.get(name="Legacy Logi")
        self.assertEqual((logi.ingame_channel_id, logi.channel_value), (CHANNEL_ID, "Logi & Reps"))

        Webhook = self.new_apps.get_model("fleetops", "DiscordWebhook")
        webhook = Webhook.objects.get(name="Legacy Pings")
        self.assertEqual((webhook.allow_everyone, webhook.allow_here, webhook.sort_order), (False, True, 0))

        Operation = self.new_apps.get_model("fleetops", "FleetOperation")
        operation = Operation.objects.get(fc_character_name="Legacy FC")
        self.assertEqual(operation.ping_channel_id, webhook.pk)
        self.assertEqual(operation.ping_channel_name, "Legacy Pings")
        self.assertEqual(operation.ping_mention_mode, "roles")
        self.assertEqual(operation.ping_roles, [{"id": "4242", "name": "Sig Legacy"}])
        self.assertEqual(operation.doctrine_fits, [])

        PingRoleModel = self.new_apps.get_model("fleetops", "PingRole")
        self.assertEqual(list(PingRoleModel.objects.values_list("role_id", "name")), [(4242, "Sig Legacy")])

        Template = self.new_apps.get_model("fleetops", "MessageTemplate")
        self.assertEqual(Template.objects.get(template_type="ping", name="Default Ping").content, upgrade.NEW_PING)
        self.assertEqual(Template.objects.get(template_type="motd", name="Default MOTD").content, "<b>Custom MOTD</b>")
        self.assertEqual(Template.objects.get(name="Custom MOTD").content, "Logi: {{ logi_channel_link }}")

    def test_new_presets_get_their_own_token(self):
        Comms = self.new_apps.get_model("fleetops", "CommsPreset")
        created = Comms.objects.create(name="After upgrade")

        self.assertIsNotNone(created.join_token)
        self.assertEqual(Comms.objects.filter(join_token=created.join_token).count(), 1)
