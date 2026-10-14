# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the Discord payload, the doctrine links/snapshot, the message filters and the render result."""

from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.exceptions import ObjectDoesNotExist
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch
from django.utils import timezone, translation

from fleetops.providers import doctrines
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE, doctrine_fits_snapshot, doctrine_url
from fleetops.providers.pings import send_discord_webhook
from fleetops.services.mentions import discord_length
from fleetops.services.messages import RenderedMessages, render_messages, render_operation_messages
from fleetops.templatetags.fleetops_messages import eve_time, ping_display

from . import factories as f

UTC = dt_timezone.utc
WEBHOOK_TOKEN = "interface-webhook-token"
WEBHOOK_URL = f"https://discord.com/api/webhooks/555000111/{WEBHOOK_TOKEN}"
ROCKET = "\U0001F680"

# 2026-10-12 19:00 UTC
SAMPLE = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)


class DiscordPayloadTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("fleetops.providers.pings.requests.post", return_value=mock.Mock(status_code=204, text=""))
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_payload_notifies_nobody_and_suppresses_embeds(self):
        result = send_discord_webhook(WEBHOOK_URL, "Form up")

        self.assertTrue(result.success)
        self.post.assert_called_once_with(
            WEBHOOK_URL,
            json={"content": "Form up", "allowed_mentions": {"parse": []}, "flags": 4},
            timeout=15,
            allow_redirects=False,
        )

    def test_allowed_mentions_are_sent_as_given(self):
        for allowed in ({"parse": ["everyone"]}, {"parse": [], "roles": ["111", "222"]}):
            with self.subTest(allowed=allowed):
                self.post.reset_mock()

                send_discord_webhook(WEBHOOK_URL, "@here Form up", allowed)

                self.assertEqual(
                    self.post.call_args.kwargs["json"],
                    {"content": "@here Form up", "allowed_mentions": allowed, "flags": 4},
                )

    def test_keyword_argument(self):
        send_discord_webhook(WEBHOOK_URL, "ping", allowed_mentions={"parse": [], "roles": ["111"]})

        self.assertEqual(self.post.call_args.kwargs["json"]["allowed_mentions"], {"parse": [], "roles": ["111"]})

    def test_limit_is_inclusive(self):
        result = send_discord_webhook(WEBHOOK_URL, "x" * 2000)

        self.assertTrue(result.success)
        self.post.assert_called_once()

    def test_over_the_limit_is_not_sent(self):
        result = send_discord_webhook(WEBHOOK_URL, "x" * 2001)

        self.assertFalse(result.success)
        self.assertIsNone(result.status_code)
        self.assertEqual(
            result.message,
            "The ping is 2001 characters; Discord accepts at most 2000. Shorten the additional information.",
        )
        self.post.assert_not_called()

    def test_emoji_count_as_two(self):
        content = "x" * 1999 + ROCKET
        self.assertEqual(len(content), 2000)

        result = send_discord_webhook(WEBHOOK_URL, content)

        self.assertFalse(result.success)
        self.assertIn("The ping is 2001 characters", result.message)
        self.post.assert_not_called()

    def test_url_checks_still_come_first(self):
        result = send_discord_webhook("https://example.com/api/webhooks/1/token", "x" * 2001)

        self.assertEqual(
            result.message, "The configured webhook URL is not a Discord webhook URL, so nothing was sent."
        )
        self.post.assert_not_called()


class DoctrineUrlTests(SimpleTestCase):
    @override_settings(SITE_URL="https://auth.example.org/")
    def test_fittings_doctrine_link_is_absolute(self):
        with mock.patch.object(doctrines, "reverse", return_value="/fittings/doctrine/12/") as reverse_mock:
            url = doctrine_url(FITTINGS_DOCTRINE_SOURCE, "12")

        self.assertEqual(url, "https://auth.example.org/fittings/doctrine/12/")
        reverse_mock.assert_called_once_with("fittings:view_doctrine", args=[12])

    def test_other_sources_and_ids_have_no_link(self):
        cases = [
            ("custom", "12"),
            ("none", ""),
            ("", "12"),
            ("fittings:fitting.doctrinegroup", "12"),
            ("fittings:fittings.fitting", "12"),
            (FITTINGS_DOCTRINE_SOURCE, ""),
            (FITTINGS_DOCTRINE_SOURCE, "abc"),
            (FITTINGS_DOCTRINE_SOURCE, "1.5"),
            (FITTINGS_DOCTRINE_SOURCE, "-3"),
            (FITTINGS_DOCTRINE_SOURCE, "\u0661\u0662"),
            (FITTINGS_DOCTRINE_SOURCE, None),
        ]
        with mock.patch.object(doctrines, "reverse") as reverse_mock:
            for source, external_id in cases:
                with self.subTest(source=source, external_id=external_id):
                    self.assertEqual(doctrine_url(source, external_id), "")
        reverse_mock.assert_not_called()

    def test_no_link_when_fittings_is_not_mounted(self):
        with mock.patch.object(doctrines, "reverse", side_effect=NoReverseMatch("fittings")):
            self.assertEqual(doctrine_url(FITTINGS_DOCTRINE_SOURCE, "12"), "")
        # The test project has no fittings URLs at all.
        self.assertEqual(doctrine_url(FITTINGS_DOCTRINE_SOURCE, "12"), "")


class FakeQuerySet:
    def __init__(self, rows, calls):
        self.rows = list(rows)
        self.calls = calls

    def filter(self, **kwargs):
        self.calls.append(("filter", kwargs))
        return self

    def select_related(self, *names):
        self.calls.append(("select_related", names))
        return self

    def prefetch_related(self, *names):
        self.calls.append(("prefetch_related", names))
        return self

    def order_by(self, *names):
        self.calls.append(("order_by", names))
        return FakeQuerySet(sorted(self.rows, key=lambda row: row.pk), self.calls)

    def __iter__(self):
        return iter(self.rows)


class MissingShip:
    def __get__(self, instance, owner):
        raise ObjectDoesNotExist("ItemType matching query does not exist.")


class FitWithoutShip(SimpleNamespace):
    ship_type = MissingShip()


def fake_doctrine_model(rows, calls):
    through = SimpleNamespace(objects=FakeQuerySet(rows, calls))
    return SimpleNamespace(fittings=SimpleNamespace(through=through))


def through_row(pk, fit_id, name, type_id, ship=None):
    fitting = SimpleNamespace(pk=fit_id, name=name, ship_type_type_id=type_id, ship_type=ship)
    return SimpleNamespace(pk=pk, fitting=fitting)


class DoctrineFitsSnapshotTests(SimpleTestCase):
    def patch_model(self, model=None, error=None):
        fake_apps = mock.Mock()
        if error is not None:
            fake_apps.get_model.side_effect = error
        else:
            fake_apps.get_model.return_value = model
        return mock.patch.object(doctrines, "apps", fake_apps)

    def test_fits_in_doctrine_order_with_english_ship_names(self):
        calls = []
        rows = [
            through_row(7, 30, "Booster Drake", 24698, SimpleNamespace(name_en="Drake", name="幼龙级")),
            through_row(2, 10, "DPS Moa", 623, SimpleNamespace(name_en="Moa", name="摩艾级")),
            through_row(5, 20, "Logi Osprey", 620, SimpleNamespace(name_en="", name="Osprey")),
            through_row(3, 11, "DPS Moa 2", 623, SimpleNamespace(name_en="Moa", name="摩艾级")),
        ]

        with self.patch_model(fake_doctrine_model(rows, calls)) as fake_apps:
            fits = doctrine_fits_snapshot(FITTINGS_DOCTRINE_SOURCE, "12")

        self.assertEqual(
            fits,
            [
                {"fit_id": 10, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"},
                {"fit_id": 11, "fit": "DPS Moa 2", "ship_type_id": 623, "ship": "Moa"},
                {"fit_id": 20, "fit": "Logi Osprey", "ship_type_id": 620, "ship": "Osprey"},
                {"fit_id": 30, "fit": "Booster Drake", "ship_type_id": 24698, "ship": "Drake"},
            ],
        )
        fake_apps.get_model.assert_called_once_with("fittings", "Doctrine")
        self.assertIn(("filter", {"doctrine_id": 12}), calls)
        self.assertIn(("order_by", ("pk",)), calls)

    def test_ship_type_id_when_the_ship_has_no_name(self):
        rows = [
            through_row(1, 10, "Nameless", 623, SimpleNamespace(name_en=None, name="")),
            SimpleNamespace(pk=2, fitting=FitWithoutShip(pk=11, name="Missing", ship_type_type_id=620)),
        ]

        with self.patch_model(fake_doctrine_model(rows, [])):
            fits = doctrine_fits_snapshot(FITTINGS_DOCTRINE_SOURCE, "12")

        self.assertEqual([fit["ship"] for fit in fits], ["623", "620"])

    def test_other_sources_are_not_read(self):
        with self.patch_model(error=AssertionError("must not be called")) as fake_apps:
            for source, external_id in (("custom", "12"), (FITTINGS_DOCTRINE_SOURCE, "x"), ("", "")):
                with self.subTest(source=source):
                    self.assertEqual(doctrine_fits_snapshot(source, external_id), [])
        fake_apps.get_model.assert_not_called()

    def test_fittings_not_installed(self):
        self.assertEqual(doctrine_fits_snapshot(FITTINGS_DOCTRINE_SOURCE, "12"), [])

    def test_broken_model_gives_no_fits(self):
        calls = []
        model = fake_doctrine_model([], calls)
        model.fittings.through.objects.filter = mock.Mock(side_effect=RuntimeError("no such table: fittings_doctrine"))

        with self.patch_model(model), self.assertLogs(doctrines.logger, "DEBUG") as logs:
            fits = doctrine_fits_snapshot(FITTINGS_DOCTRINE_SOURCE, "12")

        self.assertEqual(fits, [])
        self.assertIn("doctrine 12", logs.output[0])

    def test_unexpected_layout_gives_no_fits(self):
        with self.patch_model(SimpleNamespace()), self.assertLogs(doctrines.logger, "DEBUG"):
            self.assertEqual(doctrine_fits_snapshot(FITTINGS_DOCTRINE_SOURCE, "12"), [])


class EveTimeFilterTests(SimpleTestCase):
    def test_utc_whatever_the_site_or_active_time_zone(self):
        for zone in ("Australia/Adelaide", "America/Los_Angeles", "Asia/Shanghai"):
            with self.subTest(zone=zone), override_settings(TIME_ZONE=zone), timezone.override(ZoneInfo(zone)):
                self.assertEqual(eve_time(SAMPLE), "2026-10-12 19:00")
                self.assertEqual(eve_time(SAMPLE.astimezone(ZoneInfo(zone))), "2026-10-12 19:00")

    def test_naive_values_are_eve_time(self):
        self.assertEqual(eve_time(datetime(2026, 10, 12, 19, 0)), "2026-10-12 19:00")

    def test_format_argument(self):
        self.assertEqual(eve_time(SAMPLE, "%H:%M"), "19:00")

    def test_anything_else_is_empty(self):
        for value in (None, "", "2026-10-12", date(2026, 10, 12), 5):
            with self.subTest(value=value):
                self.assertEqual(eve_time(value), "")

    @override_settings(TIME_ZONE="Australia/Adelaide")
    def test_in_a_template(self):
        template = Template('{% load fleetops_messages %}{{ value|eve_time }} / {{ value|eve_time:"%H:%M" }}')

        with translation.override("zh-hans"), timezone.override(ZoneInfo("Australia/Adelaide")):
            text = template.render(Context({"value": SAMPLE}))

        self.assertEqual(text, "2026-10-12 19:00 / 19:00")


class PingDisplayFilterTests(SimpleTestCase):
    def setUp(self):
        self.operation = SimpleNamespace(
            ping_roles=[{"id": "111", "name": "Sig A"}, {"id": "222", "name": "Sig <B> & Co"}]
        )

    def test_role_tokens_become_names(self):
        self.assertEqual(
            ping_display("<@&111> <@&222> form up", self.operation), "@Sig A @Sig <B> & Co form up"
        )

    def test_unknown_roles_show_their_id(self):
        self.assertEqual(ping_display("<@&999> and <@&111>", self.operation), "@role 999 and @Sig A")
        self.assertEqual(ping_display("<@&111>", None), "@role 111")
        self.assertEqual(ping_display("<@&111>", SimpleNamespace(ping_roles=[{"id": "111", "name": ""}])), "@role 111")

    def test_other_text_is_unchanged(self):
        self.assertEqual(ping_display("@everyone <@111> <#222>", self.operation), "@everyone <@111> <#222>")
        self.assertEqual(ping_display("", self.operation), "")
        self.assertEqual(ping_display(None, self.operation), "")

    def test_template_escapes_the_result(self):
        template = Template("{% load fleetops_messages %}{{ text|ping_display:operation }}")

        html = template.render(Context({"text": "<@&222> <b>go</b>", "operation": self.operation}))

        self.assertEqual(html, "@Sig &lt;B&gt; &amp; Co &lt;b&gt;go&lt;/b&gt;")


class RenderedMessagesTests(SimpleTestCase):
    def test_new_fields_have_defaults(self):
        rendered = RenderedMessages("ping", "motd")

        self.assertEqual(rendered.errors, [])
        self.assertEqual(rendered.ping_display, "")
        self.assertEqual(rendered.blocking, [])
        self.assertEqual(rendered.warnings, [])
        self.assertEqual(rendered.ping_length, 0)
        self.assertEqual(rendered.motd_length, 0)

    def test_lists_are_not_shared(self):
        first, second = RenderedMessages("a", "b"), RenderedMessages("c", "d")
        first.blocking.append("x")
        first.warnings.append("y")

        self.assertEqual((second.blocking, second.warnings), ([], []))


class RenderMessagesInterfaceTests(TestCase):
    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.operation = f.create_operation(self.fc, additional_message=f"Bring drones {ROCKET}")

    def test_keyword_arguments_are_accepted(self):
        rendered = render_messages(self.operation, actor=self.fc, pre_ping=True, now=SAMPLE)

        self.assertIsInstance(rendered, RenderedMessages)
        self.assertTrue(rendered.ping)
        self.assertTrue(rendered.motd)

    def test_lengths_and_display_text(self):
        rendered = render_messages(self.operation)

        self.assertEqual(rendered.ping_display, rendered.ping)
        self.assertEqual(rendered.ping_length, discord_length(rendered.ping))
        self.assertEqual(rendered.ping_length, len(rendered.ping) + 1)
        self.assertEqual(rendered.motd_length, len(rendered.motd))
        self.assertEqual(rendered.blocking, [])
        self.assertEqual(rendered.errors, [])

    def test_compatibility_wrapper(self):
        rendered = render_messages(self.operation)

        self.assertEqual(render_operation_messages(self.operation), (rendered.ping, rendered.motd))
