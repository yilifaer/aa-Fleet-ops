# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The stored order of the ships of a doctrine (marks between the ships), where it is shown, its editor and its audit
entries, and doctrine names with square brackets in the masked links of a ping."""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.models import AuditLog, DoctrineShipOrder
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE, DoctrineChoice
from fleetops.services import doctrine_order as order
from fleetops.services.mentions import discord_link_text
from fleetops.services.messages import render_messages

from . import factories as f
from .link_fakes import setUpModule, tearDownModule  # noqa: F401 - unittest runs them around the module
from .test_messages import DOCTRINE_PATH, SITE, MessageTestBase, fittings_reverse

SOURCE = FITTINGS_DOCTRINE_SOURCE
FIT_ROWS = [
    {"fit": "DPS Minokawa", "ship_type_id": 1, "ship": "Minokawa"},
    {"fit": "Logi Lif", "ship_type_id": 2, "ship": "Lif"},
    {"fit": "DPS Minokawa 2", "ship_type_id": 1, "ship": "Minokawa"},
    {"fit": "DPS Raven", "ship_type_id": 3, "ship": "Raven"},
    {"fit": "Tackle Basilisk", "ship_type_id": 4, "ship": "Basilisk"},
]
HULLS = ["Minokawa", "Lif", "Raven", "Basilisk"]


def store(ships, marks, external_id="12"):
    return DoctrineShipOrder.objects.create(
        doctrine_source=SOURCE, doctrine_external_id=external_id, ships=ships, marks=marks
    )


class ArrangeTests(TestCase):
    def test_hulls_are_listed_once_in_the_order_of_the_fits(self):
        self.assertEqual(order.hulls(FIT_ROWS), HULLS)

    def test_a_fit_without_a_hull_name_uses_its_type_id_and_junk_is_skipped(self):
        self.assertEqual(order.hulls([{"ship_type_id": 623}, "junk", {}, {"ship": "<b>Moa</b>"}]), ["623", "Moa"])

    def test_the_automatic_order_joins_with_greater_than(self):
        self.assertEqual(order.ship_line(order.arrange(HULLS)), "Minokawa > Lif > Raven > Basilisk")

    def test_the_stored_order_and_its_marks(self):
        items = order.arrange(HULLS, ["Minokawa", "Lif", "Raven", "Basilisk"], ["=", ">", ">>>"])
        self.assertEqual(order.ship_line(items), "Minokawa = Lif > Raven >>> Basilisk")

    def test_a_ship_that_left_the_doctrine_is_dropped_and_the_next_mark_stays(self):
        items = order.arrange(["A", "C"], ["A", "B", "C"], ["=", ">>>"])
        self.assertEqual(order.ship_line(items), "A >>> C")

    def test_a_dropped_first_ship_makes_the_next_one_first(self):
        items = order.arrange(["B", "C"], ["A", "B", "C"], [">>>", "="])
        self.assertEqual(order.ship_line(items), "B = C")

    def test_ships_added_later_are_appended_with_greater_than(self):
        items = order.arrange(["A", "B", "New"], ["B", "A"], ["="])
        self.assertEqual(order.ship_line(items), "B = A > New")

    def test_an_order_that_lost_every_ship_is_the_automatic_order(self):
        self.assertEqual(order.ship_line(order.arrange(["A", "B"], ["X", "Y"], ["="])), "A > B")

    def test_a_broken_stored_mark_becomes_greater_than(self):
        self.assertEqual(order.ship_line(order.arrange(["A", "B", "C"], ["A", "B", "C"], ["<script>"])), "A > B > C")

    def test_a_ship_stored_twice_is_listed_once(self):
        self.assertEqual(order.ship_line(order.arrange(["A", "B"], ["A", "A", "B"], ["=", "="])), "A = B")

    def test_no_ships_give_an_empty_line(self):
        self.assertEqual(order.ship_line(order.arrange([])), "")

    def test_the_submission_check(self):
        self.assertEqual(order.clean_submission(["A", "B"], ["="], ["A", "B", "C"]), (["A", "B"], ["="]))
        for ships, marks in (
            ([], []),
            (["A", "X"], [">"]),
            (["A", "A"], [">"]),
            (["A", "B"], []),
            (["A", "B"], [">", ">"]),
            (["A", "B"], ["<"]),
            (["A", "B"], [">>"]),
        ):
            with self.subTest(ships=ships, marks=marks), self.assertRaises(order.OrderError):
                order.clean_submission(ships, marks, ["A", "B"])


class RenderTests(MessageTestBase):
    def operation_with_fits(self, **extra):
        return self.full_operation(doctrine_fits=FIT_ROWS, **extra)

    def test_without_a_stored_order_each_hull_appears_once(self):
        rendered = self.render(self.operation_with_fits())

        self.assertIn("(Minokawa > Lif > Raven > Basilisk)", rendered.ping)
        # The MOTD is EVE markup: the marks are written as entities, or the game would drop them.
        self.assertIn("(Minokawa &gt; Lif &gt; Raven &gt; Basilisk)", rendered.motd)

    def test_the_fit_names_keep_every_fit(self):
        from fleetops.services.messages import build_context

        with fittings_reverse():
            values = build_context(self.operation_with_fits(), now=None)

        self.assertEqual(
            values["doctrine_fits"], "DPS Minokawa > Logi Lif > DPS Minokawa 2 > DPS Raven > Tackle Basilisk"
        )

    def test_the_stored_order_is_used_in_the_ping_and_the_motd(self):
        store(["Minokawa", "Lif", "Raven", "Basilisk"], ["=", ">", ">>>"])

        rendered = self.render(self.operation_with_fits())

        self.assertIn("(Minokawa = Lif > Raven >>> Basilisk)", rendered.ping)
        # The MOTD is EVE markup: a plain ">" would be eaten by the game, so the marks are written as entities.
        self.assertIn("Doctrine: ", rendered.motd)
        self.assertIn("(Minokawa = Lif &gt; Raven &gt;&gt;&gt; Basilisk)", rendered.motd)

    def test_an_order_with_wrong_shapes_from_the_admin_falls_back_to_the_automatic_order(self):
        operation = self.operation_with_fits()
        automatic = "(Minokawa > Lif > Raven > Basilisk)"
        for ships, marks in ((5, []), ([["Moa"]], 3), ("Moa", {"a": 1}), ({"Lif": 1}, "=")):
            with self.subTest(ships=ships, marks=marks):
                row = store(ships, marks)
                try:
                    rendered = self.render(operation)
                finally:
                    row.delete()
                self.assertIn(automatic, rendered.ping)
                # The MOTD escapes the marks (see above).
                self.assertIn(automatic.replace(">", "&gt;"), rendered.motd)

    def test_a_changed_order_shows_at_the_next_render_of_an_open_fleet(self):
        operation = self.operation_with_fits()
        before = self.render(operation).motd

        row = store(["Basilisk", "Raven", "Lif", "Minokawa"], [">>>", ">", "="])
        after = self.render(operation).motd
        row.delete()

        # The MOTD writes the marks as entities (see above).
        self.assertIn("(Minokawa &gt; Lif &gt; Raven &gt; Basilisk)", before)
        self.assertIn("(Basilisk &gt;&gt;&gt; Raven &gt; Lif = Minokawa)", after)
        # The stored fits of the fleet are not touched.
        operation.refresh_from_db()
        self.assertEqual(operation.doctrine_fits, FIT_ROWS)

    def test_a_hull_added_to_the_doctrine_later_is_appended(self):
        store(["Raven", "Minokawa"], ["="])

        rendered = self.render(self.operation_with_fits())

        self.assertIn("(Raven = Minokawa > Lif > Basilisk)", rendered.ping)

    def test_the_order_of_another_doctrine_or_source_is_not_used(self):
        store(["Basilisk", "Raven", "Lif", "Minokawa"], [">", ">", ">"], external_id="99")
        DoctrineShipOrder.objects.create(
            doctrine_source="other:doctrine", doctrine_external_id="12", ships=["Raven", "Lif"], marks=["="]
        )

        rendered = self.render(self.operation_with_fits())

        self.assertIn("(Minokawa > Lif > Raven > Basilisk)", rendered.ping)

    def test_a_typed_doctrine_name_is_not_affected(self):
        store(["Raven", "Lif"], ["="])

        rendered = self.render(self.operation_with_fits(doctrine_source="", doctrine_external_id=""))

        self.assertIn("(Minokawa > Lif > Raven > Basilisk)", rendered.ping)

    def test_the_marks_in_a_ping_cannot_start_a_quote(self):
        store(["Minokawa", "Lif", "Raven", "Basilisk"], [">>>", ">>>", ">>>"])

        ping = self.render(self.operation_with_fits()).ping

        # Mid-line ">>>" is text in Discord; only a line that starts with it is a quote.
        for line in ping.splitlines():
            self.assertFalse(line.startswith(">"), line)

    def test_the_scheduled_fleet_page_shows_the_same_list(self):
        from fleetops.models import FleetOperation

        store(["Raven", "Lif", "Minokawa", "Basilisk"], ["=", ">", ">>>"])
        fc = f.create_user("sched_fc", perms=f.FC_PERMS)
        operation = f.create_operation(
            fc,
            status=FleetOperation.Status.SCHEDULED,
            scheduled_at=timezone.now() + timedelta(hours=2),
            doctrine_name="Moa Fleet",
            doctrine_source=SOURCE,
            doctrine_external_id="12",
            doctrine_fits=FIT_ROWS,
        )
        self.client.force_login(fc)

        page = self.client.get(reverse("fleetops:operation_detail", args=[operation.uuid]))

        self.assertContains(page, "Raven = Lif &gt; Minokawa &gt;&gt;&gt; Basilisk")


class EditorTests(TestCase):
    def setUp(self):
        self.admin = f.create_user("order_admin", perms=f.MEMBER_PERMS + ["fleetops.manage_configuration"])
        self.client.force_login(self.admin)
        self.choice = DoctrineChoice(external_id="12", name="[ACTIVE] Example Doctrine", source=SOURCE)
        self.fits = mock.patch("fleetops.views_config.doctrine_fits_snapshot", side_effect=lambda *a: list(FIT_ROWS))
        self.doctrines = mock.patch("fleetops.views_config.get_doctrines", side_effect=lambda user=None: [self.choice])
        self.fits.start()
        self.doctrines.start()
        self.addCleanup(self.fits.stop)
        self.addCleanup(self.doctrines.stop)
        self.url = reverse("fleetops:doctrine_order_edit", args=[12])

    def post(self, ships, marks, **extra):
        return self.client.post(self.url, {"ship": ships, "mark": marks, **extra})

    def texts(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_the_pages_need_the_configuration_permission(self):
        member = f.create_user("order_member", perms=f.MEMBER_PERMS)
        self.client.force_login(member)
        for url in (reverse("fleetops:doctrine_orders"), self.url):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"ship": HULLS, "mark": [">"] * 3}).status_code, 403)
        self.assertEqual(self.client.post(reverse("fleetops:doctrine_order_reset", args=[12])).status_code, 403)
        self.assertFalse(DoctrineShipOrder.objects.exists())
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_list_shows_the_doctrines_and_their_state(self):
        page = self.client.get(reverse("fleetops:doctrine_orders"))
        self.assertContains(page, "[ACTIVE] Example Doctrine")
        self.assertContains(page, "Automatic order")

        store(HULLS, [">"] * 3)
        self.assertContains(self.client.get(reverse("fleetops:doctrine_orders")), "Stored order")

    def test_the_administration_index_links_the_page(self):
        page = self.client.get(reverse("fleetops:configuration_index"))
        self.assertContains(page, reverse("fleetops:doctrine_orders"))

    def test_the_editor_lists_each_hull_once_with_three_marks_between_them(self):
        page = self.client.get(self.url).content.decode()

        self.assertEqual(page.count('name="ship"'), 4)
        self.assertEqual(page.count('name="mark"'), 3)
        self.assertEqual(page.count('value="&gt;&gt;&gt;"'), 3)
        self.assertEqual(page.count('value="="'), 3)
        self.assertIn("Minokawa &gt; Lif &gt; Raven &gt; Basilisk", page)
        self.assertEqual(page.count('value="move"') + page.count('name="move"'), 8)

    def test_unknown_doctrines_are_not_found(self):
        self.assertEqual(self.client.get(reverse("fleetops:doctrine_order_edit", args=[13])).status_code, 404)
        self.assertEqual(self.client.post(reverse("fleetops:doctrine_order_reset", args=[13])).status_code, 404)

    def test_saving_stores_the_order_and_writes_a_create_entry(self):
        response = self.post(["Lif", "Minokawa", "Raven", "Basilisk"], ["=", ">", ">>>"])

        self.assertRedirects(response, self.url)
        row = DoctrineShipOrder.objects.get()
        self.assertEqual((row.doctrine_source, row.doctrine_external_id), (SOURCE, "12"))
        self.assertEqual((row.ships, row.marks), (["Lif", "Minokawa", "Raven", "Basilisk"], ["=", ">", ">>>"]))
        entry = AuditLog.objects.get(action="configuration.create")
        self.assertEqual(entry.object_type, "DoctrineShipOrder")
        self.assertEqual(entry.actor, self.admin)
        self.assertIsNone(entry.old_value)
        self.assertEqual(entry.new_value["ships"], row.ships)
        self.assertEqual(entry.new_value["doctrine_name"], "[ACTIVE] Example Doctrine")
        page = self.client.get(self.url).content.decode()
        self.assertIn("Lif = Minokawa &gt; Raven &gt;&gt;&gt; Basilisk", page)

    def test_saving_again_updates_the_row_and_writes_an_update_entry(self):
        store(HULLS, [">", ">", ">"])

        self.post(["Basilisk", "Raven", "Lif", "Minokawa"], ["=", "=", "="])

        self.assertEqual(DoctrineShipOrder.objects.count(), 1)
        entry = AuditLog.objects.get(action="configuration.update")
        self.assertEqual(entry.old_value["ships"], HULLS)
        self.assertEqual(entry.new_value["marks"], ["=", "=", "="])

    def test_invalid_submissions_store_nothing(self):
        for ships, marks in (
            (["Lif", "Titan"], [">"]),
            (["Lif", "Lif"], [">"]),
            (["Lif", "Raven"], ["<"]),
            (["Lif", "Raven"], [">>"]),
            (["Lif", "Raven"], []),
            ([], []),
        ):
            with self.subTest(ships=ships, marks=marks):
                response = self.post(ships, marks)
                self.assertEqual(response.status_code, 200)
                self.assertIn("could not be saved", " ".join(self.texts(response)))
        self.assertFalse(DoctrineShipOrder.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action__startswith="configuration.").exists())

    def test_a_stale_ship_is_dropped_when_shown_and_on_the_next_save(self):
        store(["Gone", "Raven", "Lif", "Minokawa", "Basilisk"], [">>>", "=", ">", ">"])

        page = self.client.get(self.url).content.decode()
        self.assertEqual(page.count('name="ship"'), 4)
        self.assertNotIn("Gone", page)
        self.assertIn("Raven = Lif &gt; Minokawa &gt; Basilisk", page)

        self.post(["Raven", "Lif", "Minokawa", "Basilisk"], ["=", ">", ">"])
        self.assertNotIn("Gone", DoctrineShipOrder.objects.get().ships)

    def test_a_move_button_works_without_javascript_and_saves_nothing(self):
        response = self.post(HULLS, ["=", ">", ">>>"], move="down:0")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DoctrineShipOrder.objects.exists())
        page = response.content.decode()
        # The ship moved; the marks stay between the same positions.
        self.assertIn("Lif = Minokawa &gt; Raven &gt;&gt;&gt; Basilisk", page)
        self.assertIn("not saved yet", " ".join(self.texts(response)))

    def test_moves_at_the_ends_and_nonsense_change_nothing(self):
        for command in ("up:0", "down:3", "up:x", "sideways:1", "down:-1", "down:99"):
            with self.subTest(command=command):
                page = self.post(HULLS, [">", ">", ">"], move=command).content.decode()
                self.assertIn("Minokawa &gt; Lif &gt; Raven &gt; Basilisk", page)

    def test_a_form_with_a_forged_ship_shows_only_the_doctrine_ships(self):
        page = self.post(["Lif", "<script>", "Raven"], [">", ">"], move="up:1").content.decode()

        self.assertNotIn("&lt;script&gt;", page)
        self.assertEqual(page.count('name="ship"'), 4)

    def test_back_to_the_automatic_order_deletes_the_row_and_audits_it(self):
        row = store(HULLS, [">", "=", ">"])
        reset = reverse("fleetops:doctrine_order_reset", args=[12])

        self.assertEqual(self.client.get(reset).status_code, 405)
        response = self.client.post(reset)

        self.assertRedirects(response, self.url)
        self.assertFalse(DoctrineShipOrder.objects.exists())
        entry = AuditLog.objects.get(action="configuration.delete")
        self.assertEqual((entry.object_type, entry.object_id), ("DoctrineShipOrder", str(row.pk)))
        self.assertEqual(entry.old_value["ships"], HULLS)
        self.assertIsNone(entry.new_value)

    def test_resetting_without_a_stored_order_writes_no_entry(self):
        self.client.post(reverse("fleetops:doctrine_order_reset", args=[12]))
        self.assertFalse(AuditLog.objects.exists())

    def test_the_editor_needs_a_post_with_a_csrf_token_to_change_data(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.admin)

        response = client.post(self.url, {"ship": HULLS, "mark": [">"] * 3})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(DoctrineShipOrder.objects.exists())

    def test_a_doctrine_without_ships_says_so(self):
        with mock.patch("fleetops.views_config.doctrine_fits_snapshot", return_value=[]):
            page = self.client.get(self.url)
        self.assertContains(page, "no ships yet")

    def test_the_model_has_no_default_permissions(self):
        self.assertEqual(DoctrineShipOrder._meta.default_permissions, ())


class BracketNameTests(MessageTestBase):
    NAME = "[ACTIVE] Example Doctrine"

    def doctrine_ping(self, name, **extra):
        return self.render(self.full_operation(doctrine_name=name, **extra)).ping

    def test_the_link_text_escapes_brackets_and_backslashes(self):
        self.assertEqual(discord_link_text("[ACTIVE] A\\B"), "\\[ACTIVE\\] A\\\\B")
        self.assertEqual(discord_link_text(None), "")

    def test_the_ping_has_the_name_inside_the_masked_link(self):
        ping = self.doctrine_ping(self.NAME)

        self.assertIn(f"**Doctrine:** [\\[ACTIVE\\] Example Doctrine]({SITE}{DOCTRINE_PATH}) (", ping)

    def test_a_name_cannot_close_the_link_text_or_open_an_address(self):
        ping = self.doctrine_ping("x](https://evil.example/) [y")

        link = f"[x\\](https://evil.example/) \\[y]({SITE}{DOCTRINE_PATH})"
        self.assertIn(link, ping)
        # The only unescaped "](" is the one FleetOps writes.
        self.assertEqual(ping.replace("\\]", "").count("]("), 2)

    def test_a_parenthesis_in_the_name_is_harmless(self):
        ping = self.doctrine_ping("Example (old) Doctrine")

        self.assertIn(f"[Example (old) Doctrine]({SITE}{DOCTRINE_PATH})", ping)

    def test_the_comms_link_text_is_escaped_too(self):
        self.comms.name = "[Main] Comms"
        self.comms.save()

        ping = self.render(self.full_operation()).ping

        self.assertIn(f"[\\[Main\\] Comms]({self.join_url()})", ping)

    def test_a_name_without_an_address_is_escaped_as_text(self):
        ping = self.doctrine_ping(self.NAME, doctrine_source="", doctrine_external_id="")

        self.assertIn("**Doctrine:** \\[ACTIVE\\] Example Doctrine", ping)

    def test_the_motd_keeps_the_name_as_typed(self):
        motd = self.render(self.full_operation(doctrine_name=self.NAME)).motd

        self.assertIn(f'<a href="{SITE}{DOCTRINE_PATH}">{self.NAME}</a>', motd)

    def test_a_standalone_ping_escapes_the_name(self):
        from fleetops.models import StandalonePing
        from fleetops.services.messages import render_standalone_ping

        ping = StandalonePing(
            doctrine_name=self.NAME,
            doctrine_source=SOURCE,
            doctrine_external_id="12",
            formup="Jita",
            additional_message="",
            sender_name="Boss",
        )
        with fittings_reverse():
            text = render_standalone_ping(ping, actor=self.fc).ping

        self.assertIn(f"[\\[ACTIVE\\] Example Doctrine]({SITE}{DOCTRINE_PATH})", text)

    def test_the_template_variables_are_unchanged_for_other_uses(self):
        from fleetops.services.messages import build_context

        with fittings_reverse():
            values = build_context(self.full_operation(doctrine_name=self.NAME))

        self.assertEqual(values["doctrine_name"], self.NAME)
