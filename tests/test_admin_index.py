# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The administration index: every section on a card, grouped by area, in the order of the sections."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from fleetops import views_config
from fleetops.views_config import CONFIG_SECTIONS, INDEX_GROUPS, INDEX_ICONS, OTHER_GROUP, index_group

from . import factories as f

CONFIG_PERMS = f.MEMBER_PERMS + ["fleetops.manage_configuration"]


class IndexGroupTests(SimpleTestCase):
    def test_every_section_is_in_exactly_one_group(self):
        keys = [key for _title, _icon, section_keys in INDEX_GROUPS for key in section_keys]

        for section in CONFIG_SECTIONS:
            self.assertEqual(keys.count(section), 1, section)
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_group_names_only_sections_or_the_two_special_cards(self):
        known = set(CONFIG_SECTIONS) | {"settings", "srp"}

        for _title, _icon, section_keys in INDEX_GROUPS:
            self.assertLessEqual(set(section_keys), known)

    def test_groups_sit_next_to_the_ping_channels(self):
        messages = next(keys for title, _icon, keys in INDEX_GROUPS if str(title) == "Messages and Discord")

        self.assertIn("groups", messages)
        self.assertIn("ping-channels", messages)
        self.assertEqual(abs(messages.index("groups") - messages.index("ping-channels")), 1)

    def test_a_section_nobody_listed_falls_into_other(self):
        self.assertEqual(index_group("a-section-of-the-future"), OTHER_GROUP)
        self.assertEqual(str(OTHER_GROUP), "Other")
        self.assertEqual(str(index_group("ping-channels")), "Messages and Discord")
        self.assertEqual(str(index_group("fleet-types")), "Fleets")

    def test_every_card_has_an_icon(self):
        for section in (*CONFIG_SECTIONS, "settings", "srp"):
            self.assertTrue(INDEX_ICONS[section].startswith("fa-"), section)

    def test_the_numeric_columns_name_columns_that_exist(self):
        fields = {field for config in CONFIG_SECTIONS.values() for _label, field in config["columns"]}

        self.assertLessEqual(views_config.NUMERIC_COLUMNS, fields)


class IndexPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.srp_settings(enabled=True)
        cls.admin = f.create_user("index-admin", perms=CONFIG_PERMS + ["fleetops.view_audit_log"])

    def setUp(self):
        self.client.force_login(self.admin)
        self.response = self.client.get(reverse("fleetops:configuration_index"))
        self.html = self.response.content.decode()

    def test_the_cards_keep_the_order_of_the_sections(self):
        keys = [card["key"] for card in self.response.context["cards"]]

        self.assertEqual(keys, list(CONFIG_SECTIONS))
        positions = [self.html.index(f'id="admin-card-{key}"') for key in keys]
        self.assertEqual(positions, sorted(positions))

    def test_the_general_settings_and_srp_cards_come_first(self):
        first = self.html.index('id="admin-card-settings"')
        second = self.html.index('id="admin-card-srp"')
        third = self.html.index(f'id="admin-card-{next(iter(CONFIG_SECTIONS))}"')

        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_a_card_names_its_group_its_icon_and_its_count(self):
        card = self.html.split('id="admin-card-ping-channels"', 1)[1].split("</div>\n  </div>", 1)[0]

        self.assertIn('<span class="fo-admin-kicker">Messages and Discord</span>', card)
        self.assertIn("fa-hashtag", card)
        self.assertIn('<span class="fo-count">0</span>', card)
        self.assertIn(reverse("fleetops:configuration_list", args=["ping-channels"]), card)

    def test_the_groups_card_is_there_with_its_count(self):
        cards = {card["key"]: card for card in self.response.context["cards"]}

        self.assertEqual(cards["groups"]["count"], 0)
        self.assertEqual(str(cards["groups"]["group"]), "Messages and Discord")
        self.assertIn('id="admin-card-groups"', self.html)

    def test_the_srp_card_keeps_its_three_links_and_shows_the_state_as_a_pill(self):
        card = self.html.split('id="admin-card-srp"', 1)[1].split("</div>\n  </div>", 1)[0]

        self.assertIn('<span class="fo-pill fo-pill-ok">Enabled</span>', card)
        for name in ("srp", "srp-rules", "srp-corporation-limits"):
            self.assertIn(reverse("fleetops:configuration_list", args=[name]), card)
        self.assertEqual(card.count("btn btn-primary"), 1)

    def test_a_disabled_srp_is_a_neutral_pill(self):
        f.srp_settings(enabled=False)

        html = self.client.get(reverse("fleetops:configuration_index")).content.decode()

        self.assertIn('<span class="fo-pill fo-pill-neutral">Disabled</span>', html)

    def test_no_card_uses_a_bootstrap_badge_or_a_bright_border(self):
        content = self.html.split('<div class="fo-admin-grid', 1)[1]

        for forbidden in ("badge", "text-bg-", "border-primary", "border-warning", " border "):
            self.assertNotIn(forbidden, content)

    def test_the_operational_links_are_still_there_with_their_permissions(self):
        self.assertContains(self.response, reverse("fleetops:audit_log"))
        self.assertNotContains(self.response, reverse("fleetops:all_fc_statistics"))
        self.assertContains(self.response, "Operational management")

    def test_the_permission_note_is_a_callout_and_its_code_is_readable(self):
        self.assertIn('<div class="alert alert-info">', self.html)
        self.assertIn("<code>fleetops.manage_configuration</code>", self.html)
