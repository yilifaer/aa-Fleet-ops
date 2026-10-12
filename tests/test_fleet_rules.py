"""Tests for the pure fleet structure rules and the in-memory ESI fleet simulator."""

from contextlib import contextmanager
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from esi.exceptions import ESIErrorLimitException, HTTPClientError, HTTPNotModified, HTTPServerError
from pydantic import BaseModel

from fleetops.constants import FLEET_READ_SCOPE, FLEET_WRITE_SCOPE
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import FleetESIError, detect_character_fleet, get_fleet_members, kick_fleet_member
from fleetops.services import fleet_rules as rules
from fleetops.services.fleet_rules import FleetRuleError, ShipFunctionMap

from . import factories as f
from .fleet_sim import FLEET_NOT_FOUND, FleetsFleetIdWingsGetItem, FleetSim, SimError

BOSS, PILOT, OTHER = 90_100_001, 90_100_002, 90_100_003

WINGS = [
    {"id": 101, "name": "Main", "squads": [{"id": 201, "name": "Alpha"}, {"id": 202, "name": "Bravo"}]},
    {"id": 102, "name": "Logi", "squads": [{"id": 203, "name": "Logi"}]},
    {"id": 103, "name": "", "squads": []},
]


def member(character_id, role="squad_member", wing_id=101, squad_id=201, name=None):
    row = {"character_id": character_id, "role": role, "wing_id": wing_id, "squad_id": squad_id}
    if name:
        row["name"] = name
    return row


def fc(character_id=1, name="Boss"):
    return member(character_id, "fleet_commander", -1, -1, name)


def token(character_id, scopes=(FLEET_READ_SCOPE, FLEET_WRITE_SCOPE)):
    return SimpleNamespace(character_id=character_id, scopes=list(scopes))


class RuleErrorAssertions:
    @contextmanager
    def assertRuleError(self, code, message=None):
        with self.assertRaises(FleetRuleError) as caught:
            yield caught
        self.assertEqual(caught.exception.code, code)
        if message is not None:
            self.assertEqual(str(caught.exception), message)


# ---------------------------------------------------------------------------
# Names, ids and layouts
# ---------------------------------------------------------------------------


class UnitNameTests(RuleErrorAssertions, SimpleTestCase):
    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(rules.clean_unit_name("  Main \t\n Fleet "), "Main Fleet")
        self.assertEqual(rules.clean_unit_name("Logi　 1"), "Logi 1")

    def test_drops_control_and_format_characters(self):
        self.assertEqual(rules.clean_unit_name("Ma\x00in\x07"), "Main")
        self.assertEqual(rules.clean_unit_name("Lo​gi‮"), "Logi")

    def test_keeps_other_characters(self):
        self.assertEqual(rules.clean_unit_name("<b>DPS</b>"), "<b>DPS</b>")
        self.assertEqual(rules.clean_unit_name("主力舰队"), "主力舰队")
        self.assertEqual(rules.clean_unit_name(12), "12")

    def test_length_limits(self):
        self.assertEqual(rules.clean_unit_name("A"), "A")
        self.assertEqual(rules.clean_unit_name("1234567890"), "1234567890")
        for value in ("12345678901", "", "   ", "\x00\x01", None):
            with self.subTest(value=value), self.assertRuleError("invalid", "Names can be 1 to 10 characters long."):
                rules.clean_unit_name(value)

    def test_length_counts_after_cleaning(self):
        self.assertEqual(rules.clean_unit_name("  12345 \t  6789  "), "12345 6789")
        with self.assertRuleError("invalid"):
            rules.clean_unit_name("12345   67890")

    def test_rule_errors_are_value_errors(self):
        with self.assertRaises(ValueError):
            rules.clean_unit_name("")


class IdListTests(RuleErrorAssertions, SimpleTestCase):
    def test_separators(self):
        self.assertEqual(rules.parse_id_list("1, 2;3"), [1, 2, 3])
        self.assertEqual(rules.parse_id_list(" 30\n485\t513 ,, ;"), [30, 485, 513])

    def test_duplicates_dropped_in_order(self):
        self.assertEqual(rules.parse_id_list("5,3,5,1,3"), [5, 3, 1])

    def test_empty_values(self):
        for value in (None, "", "  ", ",;"):
            with self.subTest(value=value):
                self.assertEqual(rules.parse_id_list(value), [])

    def test_sequences_and_numbers(self):
        self.assertEqual(rules.parse_id_list([30, "485", " 513 "]), [30, 485, 513])
        self.assertEqual(rules.parse_id_list((7,)), [7])
        self.assertEqual(rules.parse_id_list(42), [42])

    def test_invalid_tokens(self):
        for value, token in (
            ("1, abc", "abc"),
            ("1, -5", "-5"),
            ("0", "0"),
            ("1.5", "1.5"),
            ("²", "²"),
            (str(2**63), str(2**63)),
        ):
            with self.subTest(value=value):
                with self.assertRuleError("invalid", f'"{token}" is not a valid ID. Separate IDs with commas.'):
                    rules.parse_id_list(value)

    def test_largest_database_id_allowed(self):
        self.assertEqual(rules.parse_id_list(str(2**63 - 1)), [2**63 - 1])

    def test_format_id_list(self):
        self.assertEqual(rules.format_id_list(" 30; 485 ,30"), "30,485")
        self.assertEqual(rules.format_id_list(""), "")

    def test_default_bridge_exclusions(self):
        ids = rules.parse_id_list(rules.DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)
        self.assertEqual(ids, [30, 485, 513, 547, 659, 883, 902, 941, 1538, 4594, 5120])
        self.assertEqual(rules.format_id_list(rules.DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS), rules.DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS)


class StructureLayoutTests(RuleErrorAssertions, SimpleTestCase):
    def test_standard_layout(self):
        layout = "Main: Alpha, Bravo, Charlie\nLogi: Logi\nBoosts: Boosts\nTackle: Tackle"
        self.assertEqual(
            rules.parse_structure_layout(layout),
            [
                {"name": "Main", "squads": ["Alpha", "Bravo", "Charlie"]},
                {"name": "Logi", "squads": ["Logi"]},
                {"name": "Boosts", "squads": ["Boosts"]},
                {"name": "Tackle", "squads": ["Tackle"]},
            ],
        )

    def test_comments_blank_lines_and_spacing(self):
        layout = "# Standard doctrine\n\n   Main  :  Alpha ,Bravo  \r\n  # logi below\n\tLogi:Logi 1\n\n"
        self.assertEqual(
            rules.parse_structure_layout(layout),
            [{"name": "Main", "squads": ["Alpha", "Bravo"]}, {"name": "Logi", "squads": ["Logi 1"]}],
        )

    def test_duplicate_names_allowed(self):
        self.assertEqual(
            rules.parse_structure_layout("Main: Squad, Squad\nMain: Squad"),
            [{"name": "Main", "squads": ["Squad", "Squad"]}, {"name": "Main", "squads": ["Squad"]}],
        )

    def test_limits(self):
        five = "\n".join(f"W{n}: S1, S2, S3, S4, S5" for n in range(1, 6))
        wings = rules.parse_structure_layout(five)
        self.assertEqual(len(wings), 5)
        self.assertTrue(all(len(wing["squads"]) == 5 for wing in wings))

        with self.assertRuleError("invalid", "Line 8: EVE allows at most 5 wings."):
            rules.parse_structure_layout(five.replace("W3:", "# third\nW3:").replace("W5:", "\nW5:") + "\nW6: S1")
        with self.assertRuleError("invalid", 'Line 2: wing "Main" has 6 squads; EVE allows at most 5 per wing.'):
            rules.parse_structure_layout("Logi: Logi\nMain: A, B, C, D, E, F")

    def test_name_lengths(self):
        self.assertEqual(rules.parse_structure_layout("1234567890: abcdefghij")[0]["name"], "1234567890")
        with self.assertRuleError("invalid", 'Line 3: squad name "Logistics 1" is longer than 10 characters.'):
            rules.parse_structure_layout("Main: Alpha\n# comment\nLogi: Logistics 1")
        with self.assertRuleError("invalid", 'Line 1: wing name "Interdictor" is longer than 10 characters.'):
            rules.parse_structure_layout("Interdictor: Dictors")

    def test_wing_needs_squads(self):
        for layout in ("Main: Alpha\nLogi:", "Main: Alpha\nLogi", "Main: Alpha\nLogi:   "):
            with self.subTest(layout=layout):
                with self.assertRuleError(
                    "invalid", 'Line 2: wing "Logi" needs at least one squad, for example "Logi: Squad 1".'
                ):
                    rules.parse_structure_layout(layout)

    def test_empty_names(self):
        with self.assertRuleError("invalid", "Line 1: a squad name is empty."):
            rules.parse_structure_layout("Main: Alpha,, Bravo")
        with self.assertRuleError("invalid", "Line 1: a squad name is empty."):
            rules.parse_structure_layout("Main: Alpha, Bravo,")
        with self.assertRuleError("invalid", "Line 2: a wing name is empty."):
            rules.parse_structure_layout("Main: Alpha\n : Logi")

    def test_one_colon_per_line(self):
        with self.assertRuleError(
            "invalid", 'Line 1: use one ":" per line, between the wing name and its squads.'
        ):
            rules.parse_structure_layout("Main: Alpha: Bravo")

    def test_needs_a_wing(self):
        for layout in ("", None, "\n# only a comment\n   \n"):
            with self.subTest(layout=layout):
                with self.assertRuleError("invalid", 'Add at least one wing, for example "Main: Alpha, Bravo".'):
                    rules.parse_structure_layout(layout)

    def test_control_characters_dropped(self):
        self.assertEqual(rules.parse_structure_layout("Ma\x00in: Al​pha"), [{"name": "Main", "squads": ["Alpha"]}])


# ---------------------------------------------------------------------------
# Wings, squads and labels
# ---------------------------------------------------------------------------


class NormalizeWingsTests(SimpleTestCase):
    expected = [
        {"id": 7, "name": "Main", "squads": [{"id": 70, "name": "Alpha"}, {"id": 71, "name": "Bravo"}]},
        {"id": 8, "name": "Logi", "squads": []},
    ]

    def test_dicts(self):
        raw = [
            {"id": 7, "name": "Main", "squads": [{"id": 70, "name": "Alpha"}, {"id": 71, "name": "Bravo"}]},
            {"id": 8, "name": "Logi", "squads": []},
        ]
        self.assertEqual(rules.normalize_wings(raw), self.expected)

    def test_pydantic_models(self):
        raw = [
            FleetsFleetIdWingsGetItem(id=7, name="Main", squads=[{"id": 70, "name": "Alpha"}, {"id": 71, "name": "Bravo"}]),
            FleetsFleetIdWingsGetItem(id=8, name="Logi", squads=[]),
        ]
        self.assertEqual(rules.normalize_wings(raw), self.expected)

    def test_attribute_objects(self):
        raw = [
            SimpleNamespace(
                id=7,
                name="Main",
                squads=[SimpleNamespace(id=70, name="Alpha"), SimpleNamespace(id=71, name="Bravo")],
            ),
            SimpleNamespace(id=8, name="Logi", squads=None),
        ]
        self.assertEqual(rules.normalize_wings(raw), self.expected)

    def test_esi_order_kept(self):
        raw = [{"id": 9, "name": "B", "squads": []}, {"id": 3, "name": "A", "squads": []}]
        self.assertEqual([wing["id"] for wing in rules.normalize_wings(raw)], [9, 3])

    def test_bad_entries_skipped_and_values_coerced(self):
        raw = [
            {"name": "no id", "squads": []},
            {"id": "12", "name": None, "squads": [{"id": None, "name": "x"}, {"id": "13", "name": 5}]},
            {"id": -1, "name": "invalid", "squads": []},
            {"id": True, "name": "bool", "squads": []},
        ]
        self.assertEqual(rules.normalize_wings(raw), [{"id": 12, "name": "", "squads": [{"id": 13, "name": "5"}]}])

    def test_empty_and_idempotent(self):
        self.assertEqual(rules.normalize_wings(None), [])
        self.assertEqual(rules.normalize_wings([]), [])
        self.assertEqual(rules.normalize_wings(self.expected), self.expected)


class IndexAndLabelTests(SimpleTestCase):
    def test_indexes(self):
        self.assertEqual(sorted(rules.wing_index(WINGS)), [101, 102, 103])
        wing, squad = rules.squad_index(WINGS)[203]
        self.assertEqual((wing["name"], squad["name"]), ("Logi", "Logi"))
        self.assertEqual(rules.wing_index(None), {})
        self.assertEqual(rules.squad_index(None), {})

    def test_first_entry_wins(self):
        # Tracking lists the new read first and the old snapshot after it.
        newer = [{"id": 101, "name": "DPS", "squads": [{"id": 201, "name": "Hammer"}]}]
        combined = newer + WINGS
        self.assertEqual(rules.wing_index(combined)[101]["name"], "DPS")
        self.assertEqual(rules.squad_index(combined)[201][1]["name"], "Hammer")
        self.assertEqual(rules.squad_index(combined)[202][1]["name"], "Bravo")
        self.assertEqual(rules.position_labels("squad_member", 101, 201, combined), ("DPS", "Hammer"))
        self.assertEqual(rules.position_labels("squad_member", 102, 203, combined), ("Logi", "Logi"))

    def test_fleet_command(self):
        self.assertEqual(rules.position_labels("fleet_commander", -1, -1, WINGS), ("Fleet Command", ""))
        self.assertEqual(rules.position_labels("fleet_commander", None, None, []), ("Fleet Command", ""))
        self.assertEqual(rules.position_labels("", -1, -1, WINGS), ("Fleet Command", ""))
        self.assertEqual(rules.position_labels("fleet_commander", 101, 201, WINGS), ("Fleet Command", ""))

    def test_wing_command(self):
        self.assertEqual(rules.position_labels("wing_commander", 102, -1, WINGS), ("Logi", "Wing Command"))
        self.assertEqual(rules.position_labels("squad_member", 101, -1, WINGS), ("Main", "Wing Command"))

    def test_squads(self):
        self.assertEqual(rules.position_labels("squad_member", 101, 201, WINGS), ("Main", "Alpha"))
        self.assertEqual(rules.position_labels("squad_commander", "101", "202", WINGS), ("Main", "Bravo"))

    def test_unknown_ids(self):
        self.assertEqual(
            rules.position_labels("squad_member", 2054000000001, 3100000000001, WINGS),
            ("Wing 2054000000001", "Squad 3100000000001"),
        )
        self.assertEqual(rules.position_labels("squad_member", 101, 999, WINGS), ("Main", "Squad 999"))
        self.assertEqual(rules.position_labels("wing_commander", 104, -1, None), ("Wing 104", "Wing Command"))

    def test_empty_names_fall_back_to_ids(self):
        self.assertEqual(rules.position_labels("wing_commander", 103, -1, WINGS), ("Wing 103", "Wing Command"))
        wings = [{"id": 5, "name": "Main", "squads": [{"id": 6, "name": ""}]}]
        self.assertEqual(rules.position_labels("squad_member", 5, 6, wings), ("Main", "Squad 6"))

    def test_squad_without_wing(self):
        self.assertEqual(rules.position_labels("squad_member", -1, 203, WINGS), ("Logi", "Logi"))
        self.assertEqual(rules.position_labels("squad_member", None, 999, WINGS), ("Unknown wing", "Squad 999"))

    def test_single_labels(self):
        self.assertEqual(rules.wing_label(101, WINGS), "Main")
        self.assertEqual(rules.wing_label(55), "Wing 55")
        self.assertEqual(rules.squad_label(202, WINGS), "Bravo")
        self.assertEqual(rules.squad_label(55, WINGS), "Squad 55")


# ---------------------------------------------------------------------------
# Positions and members
# ---------------------------------------------------------------------------


class PositionBodyTests(RuleErrorAssertions, SimpleTestCase):
    def test_valid_moves(self):
        self.assertEqual(rules.position_body("fleet_commander"), {"role": "fleet_commander"})
        self.assertEqual(rules.position_body("fleet_commander", -1, -1), {"role": "fleet_commander"})
        self.assertEqual(rules.position_body("wing_commander", 101), {"role": "wing_commander", "wing_id": 101})
        self.assertEqual(rules.position_body("wing_commander", "101", -1), {"role": "wing_commander", "wing_id": 101})
        self.assertEqual(
            rules.position_body("squad_commander", 101, 201),
            {"role": "squad_commander", "wing_id": 101, "squad_id": 201},
        )
        self.assertEqual(
            rules.position_body("squad_member", "101", " 201 "),
            {"role": "squad_member", "wing_id": 101, "squad_id": 201},
        )

    def test_invalid_moves(self):
        cases = (
            (("fleet_commander", 101, None), "The fleet commander has no wing or squad."),
            (("fleet_commander", None, 201), "The fleet commander has no wing or squad."),
            (("wing_commander", None, None), "A wing commander needs a wing."),
            (("wing_commander", 101, 201), "A wing commander has no squad."),
            (("squad_commander", 101, None), "A squad commander needs a wing and a squad."),
            (("squad_commander", None, None), "A squad commander needs a wing and a squad."),
            (("squad_member", None, 201), "A squad member needs a wing and a squad."),
            (("squad_member", None, None), "A squad member needs a wing and a squad."),
        )
        for args, message in cases:
            with self.subTest(args=args), self.assertRuleError("invalid", message):
                rules.position_body(*args)

    def test_invites(self):
        self.assertEqual(rules.position_body("squad_member", invite=True), {"role": "squad_member"})
        self.assertEqual(
            rules.position_body("squad_member", 101, 201, invite=True),
            {"role": "squad_member", "wing_id": 101, "squad_id": 201},
        )
        self.assertEqual(
            rules.position_body("wing_commander", 102, invite=True), {"role": "wing_commander", "wing_id": 102}
        )
        message = "Choose both a wing and a squad, or neither to join any squad with free positions."
        with self.assertRuleError("invalid", message):
            rules.position_body("squad_member", 101, None, invite=True)
        with self.assertRuleError("invalid", "A squad commander needs a wing and a squad."):
            rules.position_body("squad_commander", invite=True)

    def test_unknown_role_and_bad_ids(self):
        with self.assertRuleError("invalid", '"admiral" is not a fleet position.'):
            rules.position_body("admiral")
        for value in ("abc", 0, -2, True, 2**63, 1.5):
            with self.subTest(value=value), self.assertRuleError("invalid", f'"{value}" is not a valid wing ID.'):
                rules.position_body("wing_commander", value)
        with self.assertRuleError("invalid", '"x" is not a valid squad ID.'):
            rules.position_body("squad_member", 101, "x")

    def test_blank_ids_mean_none(self):
        self.assertEqual(rules.position_body("fleet_commander", "", ""), {"role": "fleet_commander"})
        self.assertEqual(rules.position_body("squad_member", "", "", invite=True), {"role": "squad_member"})


class MemberHelperTests(SimpleTestCase):
    def setUp(self):
        self.members = [
            fc(1, "Boss"),
            member(2, "wing_commander", 101, -1, "Wing Lead"),
            member(3, "squad_commander", 101, 201, "Alpha Lead"),
            member(4, "squad_member", 101, 201),
            member(5, "squad_member", 102, 203),
            # A stale row: the squad belongs to wing 101, the wing id is missing.
            member(6, "squad_member", -1, 202),
        ]

    def test_member_position(self):
        self.assertEqual(rules.member_position(fc()), {"role": "fleet_commander"})
        self.assertEqual(rules.member_position(self.members[1]), {"role": "wing_commander", "wing_id": 101})
        self.assertEqual(
            rules.member_position(self.members[3]), {"role": "squad_member", "wing_id": 101, "squad_id": 201}
        )
        self.assertEqual(rules.member_position(self.members[5]), {"role": "squad_member", "squad_id": 202})

    def test_member_state_field_names(self):
        state = SimpleNamespace(character_id=9, character_name="Pilot Nine", fleet_role="squad_commander", wing_id=102, squad_id=203)
        self.assertEqual(rules.member_role(state), "squad_commander")
        self.assertEqual(rules.member_name(state), "Pilot Nine")
        self.assertEqual(rules.member_name({"character_id": 9}), "Character 9")
        self.assertEqual(rules.commander([state], "squad_commander", squad_id=203), state)

    def test_wing_and_squad_members(self):
        self.assertEqual([m["character_id"] for m in rules.wing_members(self.members, 101, WINGS)], [2, 3, 4, 6])
        self.assertEqual([m["character_id"] for m in rules.wing_members(self.members, 102, WINGS)], [5])
        self.assertEqual(rules.wing_members(self.members, 103, WINGS), [])
        self.assertEqual([m["character_id"] for m in rules.squad_members(self.members, 201)], [3, 4])
        self.assertEqual(rules.squad_members(self.members, 999), [])

    def test_commanders(self):
        self.assertEqual(rules.commander(self.members, "fleet_commander")["character_id"], 1)
        self.assertEqual(rules.commander(self.members, "wing_commander", 101)["character_id"], 2)
        self.assertIsNone(rules.commander(self.members, "wing_commander", 102))
        self.assertEqual(rules.commander(self.members, "squad_commander", 101, 201)["character_id"], 3)
        self.assertIsNone(rules.commander(self.members, "squad_commander", 101, 202))


class OperationCheckTests(RuleErrorAssertions, SimpleTestCase):
    def setUp(self):
        self.members = [
            fc(1, "Boss"),
            member(2, "wing_commander", 101, -1, "Wing Lead"),
            member(3, "squad_commander", 101, 201, "Alpha Lead"),
            member(4, "squad_member", 101, 201, "Pilot Four"),
            member(5, "squad_member", 101, 201, "Pilot Five"),
            member(6, "squad_member", 101, 201, "Pilot Six"),
            member(7, "squad_member", 102, 203, "Logi Seven"),
        ]

    def test_add_wing(self):
        rules.check_add_wing(WINGS)
        rules.check_add_wing([])
        five = [{"id": n, "name": f"W{n}", "squads": []} for n in range(1, 6)]
        with self.assertRuleError("limit", "EVE allows at most 5 wings."):
            rules.check_add_wing(five)

    def test_add_squad(self):
        self.assertEqual(rules.check_add_squad(WINGS, 101)["name"], "Main")
        full = [{"id": 1, "name": "Main", "squads": [{"id": n, "name": f"S{n}"} for n in range(10, 15)]}]
        with self.assertRuleError("limit", "EVE allows at most 5 squads per wing."):
            rules.check_add_squad(full, 1)
        with self.assertRuleError("not_found", "That wing no longer exists."):
            rules.check_add_squad(WINGS, 999)

    def test_renames(self):
        wing, name = rules.check_rename_wing(WINGS, "102", "  Logi  2 ")
        self.assertEqual((wing["id"], name), (102, "Logi 2"))
        wing, squad, name = rules.check_rename_squad(WINGS, 202, "Hammer")
        self.assertEqual((wing["id"], squad["id"], name), (101, 202, "Hammer"))
        with self.assertRuleError("not_found", "That squad no longer exists."):
            rules.check_rename_squad(WINGS, 999, "X")
        with self.assertRuleError("invalid", "Names can be 1 to 10 characters long."):
            rules.check_rename_wing(WINGS, 101, "Main Fleet 1")

    def test_delete_wing(self):
        self.assertEqual(rules.check_delete_wing(WINGS, 103, self.members)["id"], 103)
        message = "Only empty wings can be deleted: move the wing commander and all squad members first."
        with self.assertRuleError("not_empty", message):
            rules.check_delete_wing(WINGS, 102, self.members)
        only_commander = [fc(), member(2, "wing_commander", 101, -1)]
        with self.assertRuleError("not_empty", message):
            rules.check_delete_wing(WINGS, 101, only_commander)
        # Empty squads do not block deleting their wing.
        self.assertEqual(rules.check_delete_wing(WINGS, 101, [fc()])["id"], 101)
        with self.assertRuleError("not_found"):
            rules.check_delete_wing(WINGS, 999, self.members)

    def test_delete_squad(self):
        self.assertEqual(rules.check_delete_squad(WINGS, 202, self.members)[1]["id"], 202)
        with self.assertRuleError("not_empty", "Only empty squads can be deleted. Move its 4 members first."):
            rules.check_delete_squad(WINGS, 201, self.members)
        with self.assertRuleError("not_empty", "Only empty squads can be deleted. Move its member first."):
            rules.check_delete_squad(WINGS, 203, self.members)
        with self.assertRuleError("not_found", "That squad no longer exists."):
            rules.check_delete_squad(WINGS, 999, self.members)

    def test_move_returns_body(self):
        pilot = self.members[3]
        self.assertEqual(
            rules.check_move(pilot, "squad_member", 101, 202, wings=WINGS, members=self.members),
            {"role": "squad_member", "wing_id": 101, "squad_id": 202},
        )
        self.assertEqual(
            rules.check_move(pilot, "squad_commander", 102, 203, wings=WINGS, members=self.members),
            {"role": "squad_commander", "wing_id": 102, "squad_id": 203},
        )
        self.assertEqual(
            rules.check_move(pilot, "wing_commander", 102, wings=WINGS, members=self.members),
            {"role": "wing_commander", "wing_id": 102},
        )

    def test_unchanged_position(self):
        self.assertIsNone(rules.check_move(self.members[3], "squad_member", 101, 201, wings=WINGS, members=self.members))
        self.assertIsNone(rules.check_move(self.members[0], "fleet_commander", -1, -1, wings=WINGS, members=self.members))
        self.assertIsNone(rules.check_move(self.members[1], "wing_commander", 101, wings=WINGS, members=self.members))
        self.assertIsNone(
            rules.check_move(self.members[2], "squad_commander", 101, 201, wings=WINGS, members=self.members)
        )

    def test_occupied_commander_positions(self):
        pilot = self.members[3]
        cases = (
            (("squad_commander", 101, 201), "Alpha already has a squad commander (Alpha Lead). Move them first."),
            (("wing_commander", 101, None), "Main already has a wing commander (Wing Lead). Move them first."),
            (("fleet_commander", None, None), "The fleet already has a fleet commander (Boss). Move them first."),
        )
        for args, message in cases:
            with self.subTest(args=args), self.assertRuleError("occupied", message):
                rules.check_move(pilot, *args, wings=WINGS, members=self.members)

    def test_occupied_without_known_structure(self):
        with self.assertRuleError("occupied", "Squad 201 already has a squad commander (Alpha Lead). Move them first."):
            rules.check_move(self.members[3], "squad_commander", 101, 201, wings=None, members=self.members)

    def test_commander_can_move_within_own_position_kind(self):
        # The squad commander of Alpha takes the free commander position of Bravo.
        self.assertEqual(
            rules.check_move(self.members[2], "squad_commander", 101, 202, wings=WINGS, members=self.members),
            {"role": "squad_commander", "wing_id": 101, "squad_id": 202},
        )

    def test_move_to_missing_units(self):
        pilot = self.members[3]
        with self.assertRuleError("not_found", "That squad no longer exists."):
            rules.check_move(pilot, "squad_member", 101, 999, wings=WINGS, members=self.members)
        with self.assertRuleError("not_found", "That wing no longer exists."):
            rules.check_move(pilot, "wing_commander", 999, wings=WINGS, members=self.members)
        with self.assertRuleError("not_found", "That squad is not part of the chosen wing."):
            rules.check_move(pilot, "squad_member", 102, 201, wings=WINGS, members=self.members)
        # Without a structure read only the position rules are checked.
        self.assertEqual(
            rules.check_move(pilot, "squad_member", 999, 998, wings=None, members=self.members),
            {"role": "squad_member", "wing_id": 999, "squad_id": 998},
        )

    def test_move_invalid_combination(self):
        with self.assertRuleError("invalid", "A wing commander has no squad."):
            rules.check_move(self.members[3], "wing_commander", 101, 201, wings=WINGS, members=self.members)

    def test_invite(self):
        self.assertEqual(rules.check_invite(wings=WINGS, members=self.members), {"role": "squad_member"})
        self.assertEqual(
            rules.check_invite("squad_member", 102, 203, wings=WINGS, members=self.members),
            {"role": "squad_member", "wing_id": 102, "squad_id": 203},
        )
        self.assertEqual(
            rules.check_invite("squad_commander", 102, 203, wings=WINGS, members=self.members),
            {"role": "squad_commander", "wing_id": 102, "squad_id": 203},
        )
        with self.assertRuleError("occupied", "Main already has a wing commander (Wing Lead). Move them first."):
            rules.check_invite("wing_commander", 101, wings=WINGS, members=self.members)
        with self.assertRuleError("not_found"):
            rules.check_invite("squad_member", 101, 999, wings=WINGS, members=self.members)
        self.assertEqual(
            rules.check_invite("squad_member", 104, 299, members=self.members),
            {"role": "squad_member", "wing_id": 104, "squad_id": 299},
        )
        with self.assertRuleError("occupied", "Wing 101 already has a wing commander (Wing Lead). Move them first."):
            rules.check_invite("wing_commander", 101, members=self.members)

    def test_fleet_room(self):
        rules.check_fleet_room(0)
        rules.check_fleet_room(255)
        rules.check_fleet_room(206, 50)
        with self.assertRuleError("limit", "The fleet is full: EVE allows 256 members."):
            rules.check_fleet_room(256)
        with self.assertRuleError("limit", "Only 1 more character fits into the fleet (EVE allows 256 members)."):
            rules.check_fleet_room(255, 2)
        with self.assertRuleError("limit", "Only 6 more characters fit into the fleet (EVE allows 256 members)."):
            rules.check_fleet_room(250, 10)


# ---------------------------------------------------------------------------
# Ship functions
# ---------------------------------------------------------------------------


class FleetRolesTests(RuleErrorAssertions, SimpleTestCase):
    def test_parse(self):
        self.assertEqual(rules.parse_fleet_roles("wing_commander, fleet_commander"), ("fleet_commander", "wing_commander"))
        self.assertEqual(rules.parse_fleet_roles(" Squad_Commander ;squad_commander"), ("squad_commander",))
        self.assertEqual(rules.parse_fleet_roles(["squad_commander", "fleet_commander"]), ("fleet_commander", "squad_commander"))
        for value in (None, "", " , "):
            with self.subTest(value=value):
                self.assertEqual(rules.parse_fleet_roles(value), ())

    def test_only_commander_positions(self):
        for value in ("squad_member", "fleet_commander, boss"):
            with self.subTest(value=value):
                with self.assertRuleError("invalid"):
                    rules.parse_fleet_roles(value)
        with self.assertRuleError(
            "invalid", '"boss" is not a commander position. Use fleet_commander, wing_commander or squad_commander.'
        ):
            rules.parse_fleet_roles("boss")

    def test_format(self):
        self.assertEqual(rules.format_fleet_roles("squad_commander,fleet_commander"), "fleet_commander,squad_commander")
        self.assertEqual(rules.format_fleet_roles([]), "")


class ShipFunctionMapTests(SimpleTestCase):
    def setUp(self):
        self.command = {"id": 1, "name": "Fleet Command", "sort_order": 10, "fleet_roles": "fleet_commander"}
        self.dps = {"id": 2, "name": "DPS", "sort_order": 20, "is_default": True}
        self.logi = {"id": 3, "name": "Logistics", "sort_order": 30}
        self.capsules = {"id": 4, "name": "Capsules", "sort_order": 110}
        self.functions = [self.capsules, self.logi, self.dps, self.command]
        self.rules = [
            {"function_id": 1, "kind": "group", "target_id": 1972},
            {"function_id": 3, "kind": "group", "target_id": 832},
            {"function_id": 3, "kind": "type", "target_id": 11987},
            {"function_id": 4, "kind": "group", "target_id": 29},
            {"function_id": 4, "kind": "type", "target_id": 670},
        ]
        self.map = ShipFunctionMap.build(self.functions, self.rules)

    def test_precedence(self):
        resolve = self.map.resolve
        # Position first, whatever the ship.
        self.assertIs(resolve("fleet_commander", 11987, 832, 6), self.command)
        # Then the type rule, then the group rule.
        self.assertIs(resolve("squad_member", 11987, 999, 6), self.logi)
        self.assertIs(resolve("squad_member", 11978, 832, 6), self.logi)
        self.assertIs(resolve("wing_commander", 670, None, None), self.capsules)
        self.assertIs(resolve("squad_member", 33328, 29, 6), self.capsules)
        # The default only takes known ships.
        self.assertIs(resolve("squad_member", 24690, 27, 6), self.dps)
        self.assertIsNone(resolve("squad_member", 24690, None, None))
        self.assertIsNone(resolve("squad_member", 24690, 1, 2))
        self.assertIsNone(resolve())

    def test_inactive_functions_and_their_rules_are_ignored(self):
        self.logi["is_active"] = False
        self.command["is_active"] = False
        mapping = ShipFunctionMap.build(self.functions, self.rules)
        self.assertIs(mapping.resolve("fleet_commander", 11987, 832, 6), self.dps)
        self.assertNotIn(832, mapping.by_group)
        self.assertNotIn("fleet_commander", mapping.by_role)

    def test_lowest_sort_order_wins(self):
        boosts = {"id": 5, "name": "Boosts", "sort_order": 5, "fleet_roles": "fleet_commander,wing_commander"}
        backup = {"id": 6, "name": "Backup", "sort_order": 50, "is_default": True}
        mapping = ShipFunctionMap.build(
            self.functions + [boosts, backup],
            self.rules + [{"function_id": 6, "kind": "group", "target_id": 832}, {"function_id": 5, "kind": "type", "target_id": 670}],
        )
        self.assertIs(mapping.resolve("fleet_commander"), boosts)
        self.assertIs(mapping.resolve("wing_commander"), boosts)
        self.assertIs(mapping.by_group[832], self.logi)
        self.assertIs(mapping.by_type[670], boosts)
        self.assertIs(mapping.default, self.dps)

    def test_same_sort_order_sorted_by_name(self):
        first = {"id": 7, "name": "Alpha", "sort_order": 0, "fleet_roles": "squad_commander"}
        second = {"id": 8, "name": "Beta", "sort_order": 0, "fleet_roles": "squad_commander"}
        self.assertIs(ShipFunctionMap.build([second, first]).resolve("squad_commander"), first)

    def test_model_like_objects(self):
        logi = SimpleNamespace(pk=3, name="Logistics", sort_order=30, fleet_roles="", is_default=False, is_active=True)
        default = SimpleNamespace(pk=2, name="DPS", sort_order=20, fleet_roles="", is_default=True, is_active=True)
        rules_ = [
            SimpleNamespace(function_id=None, function=logi, kind="group", target_id=832),
            {"function": 3, "kind": "type", "target_id": "11987"},
            {"function_id": 99, "kind": "group", "target_id": 1},
            {"function_id": 3, "kind": "skill", "target_id": 2},
            {"function_id": 3, "kind": "group", "target_id": None},
        ]
        mapping = ShipFunctionMap.build([logi, default], rules_)
        self.assertEqual(mapping.by_group, {832: logi})
        self.assertEqual(mapping.by_type, {11987: logi})
        self.assertIs(mapping.resolve("squad_member", "11987"), logi)
        self.assertIs(mapping.resolve("squad_member", 1, 1, "6"), default)

    def test_stored_roles_are_read_leniently(self):
        odd = {"id": 9, "name": "Odd", "sort_order": 1, "fleet_roles": "squad_member, squad_commander, junk"}
        mapping = ShipFunctionMap.build([odd])
        self.assertEqual(list(mapping.by_role), ["squad_commander"])

    def test_empty(self):
        mapping = ShipFunctionMap.build([])
        self.assertIsNone(mapping.resolve("fleet_commander", 670, 29, 6))


# ---------------------------------------------------------------------------
# Fleet simulator
# ---------------------------------------------------------------------------


class SimTestMixin:
    def setUp(self):
        super().setUp()
        self.sim = FleetSim()
        self.fleet = self.sim.create_fleet(BOSS)
        self.fleet_id = self.fleet.fleet_id
        self.wing = self.fleet.wings[0]["id"]
        self.squad = self.fleet.wings[0]["squads"][0]["id"]
        self.fleet.add_member(PILOT)
        self.boss_token = token(BOSS)

    def call(self, name, caller=BOSS, scopes=(FLEET_READ_SCOPE, FLEET_WRITE_SCOPE), **params):
        """One call the way FleetOps' provider makes it, with ``caller``'s token."""
        operation = getattr(self.sim, name)(token=token(caller, scopes), **params)
        if name.startswith("Get"):
            return operation.result(use_etag=False, force_refresh=True)
        return operation.result(use_etag=False, use_cache=False, store_cache=False)

    def move(self, character_id, body, caller=BOSS):
        return self.call(
            "PutFleetsFleetIdMembersMemberId", caller, fleet_id=self.fleet_id, member_id=character_id, body=body
        )

    def invite(self, body, caller=BOSS):
        return self.call("PostFleetsFleetIdMembers", caller, fleet_id=self.fleet_id, body=body)

    @contextmanager
    def assertESIError(self, status, error=None):
        with self.assertRaises(HTTPClientError if status < 500 else HTTPServerError) as caught:
            yield caught
        self.assertEqual(caught.exception.status_code, status)
        if error is not None:
            self.assertEqual(caught.exception.data, {"error": error})


class FleetSimClientContractTests(SimTestMixin, SimpleTestCase):
    """The django-esi ``result()`` contract."""

    def test_reads_are_cached_with_etags(self):
        members = self.sim.GetFleetsFleetIdMembers(fleet_id=self.fleet_id, token=self.boss_token)
        self.assertEqual(len(members.result()), 2)
        with self.assertRaises(HTTPNotModified):
            self.sim.GetFleetsFleetIdMembers(fleet_id=self.fleet_id, token=self.boss_token).result()
        self.sim.expire_cache()
        with self.assertRaises(HTTPNotModified):
            self.sim.GetFleetsFleetIdMembers(fleet_id=self.fleet_id, token=self.boss_token).result()
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdMembers")), 2)

    def test_cache_without_etags_and_force_refresh(self):
        def read(**flags):
            operation = self.sim.GetFleetsFleetIdMembers(fleet_id=self.fleet_id, token=self.boss_token)
            return operation.result(use_etag=False, **flags)

        self.assertEqual(len(read()), 2)
        self.fleet.add_member(OTHER)
        self.assertEqual(len(read()), 2, "answered from django-esi's cache")
        self.assertEqual(len(read(force_refresh=True)), 3)
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdMembers")), 2)
        self.sim.cache_responses = False
        self.fleet.remove_member(OTHER)
        self.sim.expire_cache()
        self.assertEqual(len(read()), 2)
        self.assertEqual(len(read(use_cache=False)), 2)
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdMembers")), 4)

    def test_cache_key_ignores_the_token(self):
        # As in django-esi: a cached response is served whoever asks.
        self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id, token=self.boss_token).result(use_etag=False)
        wings = self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id, token=token(PILOT)).result(use_etag=False)
        self.assertEqual(len(wings), 1)
        with self.assertESIError(404, FLEET_NOT_FOUND):
            self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id, token=token(PILOT)).result(
                use_etag=False, force_refresh=True
            )

    def test_same_operation_read_twice_like_the_provider(self):
        operation = self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id, token=self.boss_token)
        operation.result()
        with self.assertRaises(HTTPNotModified):
            operation.result()
        # The token stays with the operation, as in django-esi.
        self.assertEqual(len(operation.result(use_etag=False, force_refresh=True)), 1)

    def test_always_not_modified(self):
        self.sim.always_not_modified = True
        with self.assertRaises(HTTPNotModified):
            self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id, token=self.boss_token).result(use_etag=False)
        self.assertEqual(len(self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)), 1)

    def test_writes_are_not_cached(self):
        first = self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)
        second = self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)
        self.assertNotEqual(first["wing_id"], second["wing_id"])
        self.assertEqual(self.sim.response_cache, {})

    def test_body_rules(self):
        with self.assertRaisesMessage(ValueError, "Request Body provided on endpoint with no request body parameter."):
            self.sim.PostFleetsFleetIdWings(fleet_id=self.fleet_id, token=self.boss_token, body={"name": "x"}).result()
        with self.assertRaisesMessage(ValueError, "Request Body provided on endpoint with no request body parameter."):
            self.sim.PostFleetsFleetIdWingsWingIdSquads(
                fleet_id=self.fleet_id, wing_id=self.wing, token=self.boss_token, body={"name": "x"}
            ).result()
        with self.assertRaisesMessage(ValueError, "Request Body is required but none was provided."):
            self.sim.PutFleetsFleetIdWingsWingId(fleet_id=self.fleet_id, wing_id=self.wing, token=self.boss_token).result()
        with self.assertRaises(TypeError):
            self.sim.PutFleetsFleetIdWingsWingId(
                fleet_id=self.fleet_id, wing_id=self.wing, token=self.boss_token, body="Main"
            ).result()
        self.assertEqual(self.sim.requests, [], "nothing reached ESI")

    def test_parameters(self):
        with self.assertRaisesMessage(ValueError, "Parameter ['wing_id'] unknown"):
            self.sim.PostFleetsFleetIdWings(fleet_id=self.fleet_id, wing_id=self.wing, token=self.boss_token).result()
        with self.assertRaisesMessage(ValueError, "Required Parameter ['squad_id'] missing"):
            self.sim.DeleteFleetsFleetIdSquadsSquadId(fleet_id=self.fleet_id, token=self.boss_token).result()
        with self.assertRaisesMessage(ValueError, "Parameter fleet_id must be an integer"):
            self.sim.GetFleetsFleetIdWings(fleet_id="abc", token=self.boss_token).result()
        # Header parameters are accepted under their Python names.
        wings = self.sim.GetFleetsFleetIdWings(
            fleet_id=str(self.fleet_id), token=self.boss_token, Accept_Language="en"
        ).result(use_etag=False)
        self.assertEqual(len(wings), 1)
        self.assertEqual(self.sim.requests[-1].params, {"fleet_id": self.fleet_id, "Accept-Language": "en"})
        with self.assertRaises(AttributeError):
            self.sim.GetFleetsFleetIdFittings  # noqa: B018

    def test_token_scopes(self):
        with self.assertRaisesMessage(ValueError, f"Token Missing Scopes - ['{FLEET_WRITE_SCOPE}']"):
            self.call("PostFleetsFleetIdWings", scopes=(FLEET_READ_SCOPE,), fleet_id=self.fleet_id)
        with self.assertRaisesMessage(ValueError, f"Token Missing Scopes - ['{FLEET_READ_SCOPE}']"):
            self.call("GetFleetsFleetIdWings", scopes=(FLEET_WRITE_SCOPE,), fleet_id=self.fleet_id)
        with self.assertRaisesMessage(ValueError, "No security requirement satisfied"):
            self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id).result()
        # A token object without scope information is not checked.
        wings = self.sim.GetFleetsFleetIdWings(
            fleet_id=self.fleet_id, token=SimpleNamespace(character_id=BOSS)
        ).result(use_etag=False)
        self.assertEqual(len(wings), 1)

    def test_return_response(self):
        data, response = self.sim.GetFleetsFleetIdWings(fleet_id=self.fleet_id, token=self.boss_token).result(
            use_etag=False, return_response=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("ETag", response.headers)
        created, response = self.sim.PostFleetsFleetIdWings(fleet_id=self.fleet_id, token=self.boss_token).result(
            use_cache=False, store_cache=False, return_response=True
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("wing_id", created)
        result, response = self.sim.PutFleetsFleetIdWingsWingId(
            fleet_id=self.fleet_id, wing_id=self.wing, token=self.boss_token, body={"name": "Main"}
        ).result(use_cache=False, store_cache=False, return_response=True)
        self.assertEqual((result, response.status_code), (None, 204))

    def test_requests_are_logged_with_token_owner_and_body(self):
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)
        (write,) = self.sim.writes()
        self.assertEqual(write.name, "PutFleetsFleetIdWingsWingId")
        self.assertEqual(write.method, "PUT")
        self.assertEqual(write.params, {"fleet_id": self.fleet_id, "wing_id": self.wing})
        self.assertEqual(write.body, {"name": "Main"})
        self.assertEqual(write.character_id, BOSS)
        self.assertEqual([request.name for request in self.sim.requests], ["PutFleetsFleetIdWingsWingId", "GetFleetsFleetIdWings"])

    def test_error_answers(self):
        with self.assertESIError(404, FLEET_NOT_FOUND) as caught:
            self.call("GetFleetsFleetIdWings", fleet_id=1)
        self.assertIn("x-esi-request-id", caught.exception.headers)


class FleetSimFleetTests(SimTestMixin, SimpleTestCase):
    """EVE's fleet rules as the simulator applies them."""

    def test_new_fleet(self):
        self.assertEqual(self.fleet.boss_id, BOSS)
        self.assertEqual(self.fleet.position(BOSS), ("fleet_commander", -1, -1))
        self.assertEqual(self.fleet.position(PILOT), ("squad_member", self.wing, self.squad))
        self.assertEqual(self.fleet.structure(), [{"id": self.wing, "name": "Wing 1", "squads": [{"id": self.squad, "name": "Squad 1"}]}])
        self.assertIs(self.sim.fleet_of(PILOT), self.fleet)
        self.assertIsNone(self.sim.fleet_of(OTHER))
        with self.assertRaisesMessage(SimError, "Character is already in a fleet"):
            self.sim.create_fleet(PILOT)
        with self.assertRaisesMessage(ValueError, "exists already"):
            self.sim.create_fleet(OTHER, fleet_id=self.fleet_id)

    def test_character_fleet(self):
        self.fleet.add_member(OTHER, "wing_commander")
        self.assertEqual(
            self.call("GetCharactersCharacterIdFleet", character_id=BOSS),
            {"fleet_boss_id": BOSS, "fleet_id": self.fleet_id, "role": "fleet_commander", "squad_id": -1, "wing_id": -1},
        )
        self.assertEqual(
            self.call("GetCharactersCharacterIdFleet", OTHER, character_id=OTHER),
            {"fleet_boss_id": BOSS, "fleet_id": self.fleet_id, "role": "wing_commander", "squad_id": -1, "wing_id": self.wing},
        )
        with self.assertESIError(403):
            self.call("GetCharactersCharacterIdFleet", OTHER, character_id=BOSS)
        with self.assertESIError(404, "Character is not in a fleet"):
            self.call("GetCharactersCharacterIdFleet", 4, character_id=4)

    def test_only_the_boss_can_read_or_write(self):
        self.assertEqual(len(self.call("GetFleetsFleetIdMembers", fleet_id=self.fleet_id)), 2)
        for name, params in (
            ("GetFleetsFleetIdMembers", {}),
            ("GetFleetsFleetIdWings", {}),
            ("GetFleetsFleetId", {}),
            ("PostFleetsFleetIdWings", {}),
            ("DeleteFleetsFleetIdMembersMemberId", {"member_id": BOSS}),
        ):
            with self.subTest(name=name), self.assertESIError(404, FLEET_NOT_FOUND):
                self.call(name, PILOT, fleet_id=self.fleet_id, **params)

    def test_boss_handover_and_disband(self):
        self.fleet.set_boss(PILOT)
        with self.assertESIError(404, FLEET_NOT_FOUND):
            self.call("GetFleetsFleetIdMembers", fleet_id=self.fleet_id)
        rows = self.call("GetFleetsFleetIdMembers", PILOT, fleet_id=self.fleet_id)
        self.assertEqual({row["character_id"]: row["role_name"] for row in rows}, {BOSS: "Fleet Commander", PILOT: "Squad Member (Boss)"})
        self.assertEqual(self.call("GetCharactersCharacterIdFleet", character_id=BOSS)["fleet_boss_id"], PILOT)
        with self.assertRaisesMessage(SimError, "Fleet member not found"):
            self.fleet.set_boss(OTHER)

        self.sim.disband(self.fleet_id)
        with self.assertESIError(404, FLEET_NOT_FOUND):
            self.call("GetFleetsFleetIdMembers", PILOT, fleet_id=self.fleet_id)
        self.assertIsNone(self.sim.fleet_of(PILOT))

    def test_leaving_boss_hands_over(self):
        self.fleet.add_member(OTHER)
        self.fleet.remove_member(BOSS)
        self.assertEqual(self.fleet.boss_id, PILOT, "longest member")
        self.fleet.move_member(OTHER, "fleet_commander")
        self.fleet.remove_member(PILOT)
        self.assertEqual(self.fleet.boss_id, OTHER)
        self.fleet.remove_member(OTHER)
        self.assertNotIn(self.fleet_id, self.sim.fleets)

    def test_member_rows(self):
        joined = self.fleet.member(PILOT).join_time
        self.fleet.update_member(PILOT, ship_type_id=670, solar_system_id=30002187, station_id=60008494, takes_fleet_warp=False)
        rows = {row["character_id"]: row for row in self.call("GetFleetsFleetIdMembers", fleet_id=self.fleet_id)}
        self.assertEqual(
            rows[PILOT],
            {
                "character_id": PILOT,
                "join_time": joined,
                "role": "squad_member",
                "role_name": "Squad Member",
                "ship_type_id": 670,
                "solar_system_id": 30002187,
                "squad_id": self.squad,
                "station_id": 60008494,
                "takes_fleet_warp": False,
                "wing_id": self.wing,
            },
        )
        self.assertNotIn("station_id", rows[BOSS], "ESI leaves optional fields out")
        self.assertEqual(rows[BOSS]["role_name"], "Fleet Commander (Boss)")
        with self.assertRaises(TypeError):
            self.fleet.update_member(PILOT, role="fleet_commander")

    def test_fleet_settings(self):
        self.call("PutFleetsFleetId", fleet_id=self.fleet_id, body={"motd": "<b>Hi</b>", "is_free_move": True})
        self.assertEqual(
            self.call("GetFleetsFleetId", fleet_id=self.fleet_id),
            {"is_free_move": True, "is_registered": False, "is_voice_enabled": False, "motd": "<b>Hi</b>"},
        )
        with self.assertESIError(400, "motd must be a string"):
            self.call("PutFleetsFleetId", fleet_id=self.fleet_id, body={"motd": 5})
        with self.assertESIError(400, "is_free_move must be a boolean"):
            self.call("PutFleetsFleetId", fleet_id=self.fleet_id, body={"is_free_move": "yes"})
        with self.assertESIError(400, "Unknown field(s): name"):
            self.call("PutFleetsFleetId", fleet_id=self.fleet_id, body={"name": "x"})

    def test_create_wings(self):
        created = self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)
        wing = self.fleet.wing(created["wing_id"])
        self.assertGreater(wing["id"], self.wing)
        self.assertEqual(wing["name"], "Wing 2")
        self.assertEqual([squad["name"] for squad in wing["squads"]], ["Squad 1"])

        self.sim.new_wing_squads = 0
        bare = self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)["wing_id"]
        self.assertEqual(self.fleet.wing(bare)["squads"], [])
        self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)
        self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)
        self.assertEqual(len(self.fleet.wings), 5)
        with self.assertESIError(400, "The fleet already has the maximum number of wings"):
            self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)

    def test_create_squads(self):
        ids = [self.call("PostFleetsFleetIdWingsWingIdSquads", fleet_id=self.fleet_id, wing_id=self.wing)["squad_id"] for _ in range(4)]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual([squad["name"] for squad in self.fleet.wing(self.wing)["squads"]], [f"Squad {n}" for n in range(1, 6)])
        with self.assertESIError(400, "The wing already has the maximum number of squads"):
            self.call("PostFleetsFleetIdWingsWingIdSquads", fleet_id=self.fleet_id, wing_id=self.wing)
        with self.assertESIError(404, "Wing not found"):
            self.call("PostFleetsFleetIdWingsWingIdSquads", fleet_id=self.fleet_id, wing_id=self.squad)

    def test_renames(self):
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.call("PutFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=self.squad, body={"name": "1234567890"})
        self.assertEqual(
            self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id),
            [{"id": self.wing, "name": "Main", "squads": [{"id": self.squad, "name": "1234567890"}]}],
        )
        with self.assertESIError(400, "name must not be longer than 10 characters"):
            self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "12345678901"})
        with self.assertESIError(400, "Missing field(s): name"):
            self.call("PutFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=self.squad, body={})
        with self.assertESIError(400, "name must be a string"):
            self.call("PutFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=self.squad, body={"name": 5})
        with self.assertESIError(404, "Squad not found"):
            self.call("PutFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=self.wing, body={"name": "X"})
        with self.assertESIError(404, "Wing not found"):
            self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=1, body={"name": "X"})

    def test_only_empty_units_can_be_deleted(self):
        second = self.fleet.add_wing("Logi", ["Logi", "Spare"])
        logi, spare = (squad["id"] for squad in self.fleet.wing(second)["squads"])
        self.fleet.add_member(OTHER, "squad_commander", second, logi)

        with self.assertESIError(400, "Only empty squads can be deleted"):
            self.call("DeleteFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=logi)
        with self.assertESIError(400, "Only empty wings can be deleted"):
            self.call("DeleteFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=second)
        self.call("DeleteFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=spare)

        self.fleet.move_member(OTHER, "wing_commander", second)
        with self.assertESIError(400, "Only empty wings can be deleted"):
            self.call("DeleteFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=second)
        self.fleet.move_member(OTHER, "squad_member", self.wing, self.squad)
        # The wing still has its (empty) Logi squad.
        self.call("DeleteFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=second)
        self.assertEqual([wing["id"] for wing in self.fleet.wings], [self.wing])
        with self.assertESIError(404, "Wing not found"):
            self.call("DeleteFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=second)
        with self.assertESIError(404, "Squad not found"):
            self.call("DeleteFleetsFleetIdSquadsSquadId", fleet_id=self.fleet_id, squad_id=logi)

    def test_moves(self):
        second = self.fleet.add_wing("Logi", ["Logi"])
        logi = self.fleet.squad_id("Logi", wing="Logi")
        moves = (
            ({"role": "squad_commander", "wing_id": second, "squad_id": logi}, ("squad_commander", second, logi)),
            ({"role": "wing_commander", "wing_id": self.wing}, ("wing_commander", self.wing, -1)),
            ({"role": "squad_member", "wing_id": second, "squad_id": logi}, ("squad_member", second, logi)),
        )
        for body, position in moves:
            with self.subTest(body=body):
                self.move(PILOT, body)
                self.assertEqual(self.fleet.position(PILOT), position)
        self.move(BOSS, {"role": "squad_member", "wing_id": self.wing, "squad_id": self.squad})
        self.move(PILOT, {"role": "fleet_commander"})
        self.assertEqual(self.fleet.position(PILOT), ("fleet_commander", -1, -1))
        self.assertEqual(self.fleet.boss_id, BOSS, "moving does not hand over the boss role")

    def test_move_position_rules(self):
        cases = (
            ({"role": "fleet_commander", "wing_id": self.wing}, 400, "Neither wing_id nor squad_id may be set for the fleet_commander role"),
            ({"role": "wing_commander", "wing_id": self.wing, "squad_id": self.squad}, 400, "Only wing_id must be set for the wing_commander role"),
            ({"role": "wing_commander"}, 400, "Only wing_id must be set for the wing_commander role"),
            ({"role": "squad_commander", "wing_id": self.wing}, 400, "Both wing_id and squad_id must be set for the squad_commander role"),
            ({"role": "squad_member"}, 400, "Both wing_id and squad_id must be set for the squad_member role"),
            ({"role": "boss"}, 400, None),
            ({"role": "squad_member", "wing_id": str(self.wing), "squad_id": self.squad}, 400, "wing_id must be an integer"),
            ({"wing_id": self.wing}, 400, "Missing field(s): role"),
            ({"role": "squad_member", "wing": 1}, 400, "Unknown field(s): wing"),
            ({"role": "squad_member", "wing_id": self.wing, "squad_id": 1}, 404, "Squad not found"),
            ({"role": "wing_commander", "wing_id": 1}, 404, "Wing not found"),
        )
        for body, status, error in cases:
            with self.subTest(body=body), self.assertESIError(status, error):
                self.move(PILOT, body)
        second = self.fleet.add_wing("Logi", ["Logi"])
        with self.assertESIError(404, "Squad not found in this wing"):
            self.move(PILOT, {"role": "squad_member", "wing_id": second, "squad_id": self.squad})
        with self.assertESIError(404, "Fleet member not found"):
            self.move(OTHER, {"role": "fleet_commander"})
        self.assertEqual(self.fleet.position(PILOT), ("squad_member", self.wing, self.squad))

    def test_commander_positions_hold_one_character(self):
        self.fleet.add_member(OTHER, "squad_commander")
        cases = (
            ({"role": "fleet_commander"}, "The fleet already has a fleet commander"),
            ({"role": "squad_commander", "wing_id": self.wing, "squad_id": self.squad}, "The squad already has a squad commander"),
        )
        for body, error in cases:
            with self.subTest(body=body), self.assertESIError(400, error):
                self.move(PILOT, body)
        self.move(PILOT, {"role": "wing_commander", "wing_id": self.wing})
        with self.assertESIError(400, "The wing already has a wing commander"):
            self.move(OTHER, {"role": "wing_commander", "wing_id": self.wing})
        # Moving onto one's own position is accepted.
        self.move(PILOT, {"role": "wing_commander", "wing_id": self.wing})
        # Every squad has its own commander position.
        bravo = self.fleet.add_squad(self.wing, "Bravo")
        self.move(PILOT, {"role": "squad_commander", "wing_id": self.wing, "squad_id": bravo})
        self.assertEqual(self.fleet.position(PILOT), ("squad_commander", self.wing, bravo))

    def test_kick(self):
        self.call("DeleteFleetsFleetIdMembersMemberId", fleet_id=self.fleet_id, member_id=PILOT)
        self.assertNotIn(PILOT, self.fleet.members)
        with self.assertESIError(404, "Fleet member not found"):
            self.call("DeleteFleetsFleetIdMembersMemberId", fleet_id=self.fleet_id, member_id=PILOT)
        with self.assertESIError(400, "The fleet boss cannot be kicked"):
            self.call("DeleteFleetsFleetIdMembersMemberId", fleet_id=self.fleet_id, member_id=BOSS)

    def test_invites(self):
        self.invite({"character_id": OTHER, "role": "squad_member"})
        self.assertEqual(self.fleet.invitations, {OTHER: {"role": "squad_member", "wing_id": None, "squad_id": None}})
        self.assertNotIn(OTHER, self.fleet.members)
        joined = self.sim.accept_invite(OTHER, ship_type_id=670)
        self.assertEqual(self.fleet.position(OTHER), ("squad_member", self.wing, self.squad), "any squad with free positions")
        self.assertEqual(joined.ship_type_id, 670)
        self.assertEqual(self.fleet.invitations, {})
        with self.assertRaises(KeyError):
            self.sim.accept_invite(OTHER)

    def test_invite_into_a_position(self):
        second = self.fleet.add_wing("Logi", ["Logi"])
        logi = self.fleet.squad_id("Logi")
        body = {"character_id": OTHER, "role": "squad_commander", "wing_id": second, "squad_id": logi}
        self.invite(body)
        self.fleet.accept_invite(OTHER)
        self.assertEqual(self.fleet.position(OTHER), ("squad_commander", second, logi))

    def test_invite_rules(self):
        cases = (
            ({"character_id": PILOT, "role": "squad_member"}, "Character is already in a fleet"),
            ({"character_id": OTHER, "role": "fleet_commander"}, "The fleet already has a fleet commander"),
            ({"character_id": OTHER, "role": "squad_member", "wing_id": self.wing}, "Both wing_id and squad_id must be set for the squad_member role"),
            ({"character_id": OTHER}, "Missing field(s): role"),
            ({"character_id": str(OTHER), "role": "squad_member"}, "character_id must be an integer"),
        )
        for body, error in cases:
            with self.subTest(body=body), self.assertESIError(400, error):
                self.invite(body)
        with self.assertESIError(404, "Squad not found"):
            self.invite({"character_id": OTHER, "role": "squad_member", "wing_id": self.wing, "squad_id": 1})
        self.assertEqual(self.fleet.invitations, {})

    def test_declined_invite_and_slot_taken_before_accepting(self):
        self.invite({"character_id": OTHER, "role": "wing_commander", "wing_id": self.wing})
        self.fleet.move_member(PILOT, "wing_commander", self.wing)
        with self.assertRaisesMessage(SimError, "The wing already has a wing commander"):
            self.sim.accept_invite(OTHER)
        self.fleet.decline_invite(OTHER)
        self.assertEqual(self.fleet.invitations, {})

    def test_any_squad_skips_wings_without_squads(self):
        fleet = self.sim.create_fleet(OTHER, wings=(("Empty", 0), ("Main", ("Alpha",))))
        member = fleet.add_member(4)
        self.assertEqual((member.wing_id, member.squad_id), (fleet.wing_id("Main"), fleet.squad_id("Alpha")))
        bare = self.sim.create_fleet(5, wings=())
        with self.assertRaisesMessage(SimError, "No squad has a free position"):
            bare.add_member(6)
        with self.assertRaisesMessage(SimError, "Character is already in a fleet"):
            bare.add_member(PILOT)

    def test_member_limit(self):
        for number in range(254):
            self.fleet.add_member(1_000 + number)
        with self.assertESIError(400, "The fleet is full"):
            self.invite({"character_id": OTHER, "role": "squad_member"})
        with self.assertRaisesMessage(SimError, "The fleet is full"):
            self.fleet.add_member(OTHER)

    def test_in_game_helpers(self):
        second = self.fleet.add_wing(squads=0)
        self.assertEqual(self.fleet.wing(second)["name"], "Wing 2")
        squad = self.fleet.add_squad(second, "Bravo")
        self.fleet.rename_wing(second, "Logi")
        self.fleet.rename_squad(squad, "Logi 1")
        self.assertEqual(self.fleet.wing_id("Logi"), second)
        self.assertEqual(self.fleet.squad_id("Logi 1", wing=second), squad)
        with self.assertRaises(KeyError):
            self.fleet.squad_id("Logi 1", wing=self.wing)
        with self.assertRaises(KeyError):
            self.fleet.wing_id("Nope")
        self.fleet.delete_squad(squad)
        self.fleet.delete_wing(second)
        self.assertEqual(self.fleet.positions(), {BOSS: ("fleet_commander", -1, -1), PILOT: ("squad_member", self.wing, self.squad)})
        fleet = self.sim.create_fleet(OTHER, wings=(("Main", ("Alpha", "Bravo")), ("Logi", 2)), motd="Hello")
        self.assertEqual([[squad["name"] for squad in wing["squads"]] for wing in fleet.wings], [["Alpha", "Bravo"], ["Squad 1", "Squad 2"]])
        self.assertEqual(fleet.motd, "Hello")

    def test_states_are_copies(self):
        wings = self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)
        wings[0]["name"] = "Changed"
        self.fleet.structure()[0]["squads"].clear()
        self.assertEqual(self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)[0]["name"], "Wing 1")
        self.assertEqual(len(self.fleet.wings[0]["squads"]), 1)


class FleetSimFailureTests(SimTestMixin, SimpleTestCase):
    def test_configured_status(self):
        self.sim.fail("PutFleetsFleetIdWingsWingId", status=502)
        with self.assertESIError(502, "Bad Gateway"):
            self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        with self.assertESIError(502):
            self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.assertEqual(self.fleet.wings[0]["name"], "Wing 1")
        self.assertEqual(len(self.sim.requests_of("PutFleetsFleetIdWingsWingId")), 2, "failed calls reached ESI")
        self.sim.clear_failures()
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.assertEqual(self.fleet.wings[0]["name"], "Main")

    def test_times_and_match(self):
        self.fleet.add_member(OTHER)
        self.sim.fail("PostFleetsFleetIdMembers", status=400, error="CSPA charge set", character_id=4)
        self.sim.fail("DeleteFleetsFleetIdMembersMemberId", status=503, times=1, member_id=OTHER)
        with self.assertESIError(400, "CSPA charge set"):
            self.invite({"character_id": 4, "role": "squad_member"})
        self.invite({"character_id": 5, "role": "squad_member"})
        self.call("DeleteFleetsFleetIdMembersMemberId", fleet_id=self.fleet_id, member_id=PILOT)
        with self.assertESIError(503):
            self.call("DeleteFleetsFleetIdMembersMemberId", fleet_id=self.fleet_id, member_id=OTHER)
        self.call("DeleteFleetsFleetIdMembersMemberId", fleet_id=self.fleet_id, member_id=OTHER)
        self.assertEqual(list(self.fleet.members), [BOSS])

    def test_limited_number_of_failures(self):
        self.sim.fail("GetFleetsFleetIdWings", status=500, times=2)
        for _ in range(2):
            with self.assertESIError(500, "Internal Server Error"):
                self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)
        self.assertEqual(len(self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)), 1)

    def test_error_limit(self):
        self.sim.fail("GetFleetsFleetIdWings", status=420)
        with self.assertRaises(ESIErrorLimitException) as caught:
            self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)
        self.assertEqual(caught.exception.reset, 60)
        context = caught.exception.__context__
        self.assertIsInstance(context, HTTPClientError)
        self.assertEqual(context.status_code, 420)

    def test_exception(self):
        self.sim.fail("GetFleetsFleetIdMembers", exception=TimeoutError("read timeout"), times=1)
        with self.assertRaises(TimeoutError):
            self.call("GetFleetsFleetIdMembers", fleet_id=self.fleet_id)
        self.assertEqual(len(self.call("GetFleetsFleetIdMembers", fleet_id=self.fleet_id)), 2)

    def test_unknown_operation(self):
        with self.assertRaisesMessage(ValueError, "Unknown operation PutFleetsFleetIdWing"):
            self.sim.fail("PutFleetsFleetIdWing")


class FleetSimStaleReadTests(SimTestMixin, SimpleTestCase):
    def wing_names(self):
        return [wing["name"] for wing in self.call("GetFleetsFleetIdWings", fleet_id=self.fleet_id)]

    def test_reads_after_a_write_show_the_old_state(self):
        self.sim.stale_reads = 2
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.assertEqual(self.wing_names(), ["Wing 1"])
        self.assertEqual(self.wing_names(), ["Wing 1"])
        self.assertEqual(self.wing_names(), ["Main"])

    def test_each_fleet_route_counts_separately(self):
        self.sim.stale_reads = 1
        self.call(
            "PutFleetsFleetIdMembersMemberId",
            fleet_id=self.fleet_id,
            member_id=PILOT,
            body={"role": "squad_commander", "wing_id": self.wing, "squad_id": self.squad},
        )
        self.assertEqual(self.wing_names(), ["Wing 1"])

        def pilot_role():
            rows = self.call("GetFleetsFleetIdMembers", fleet_id=self.fleet_id)
            return next(row["role"] for row in rows if row["character_id"] == PILOT)

        self.assertEqual(pilot_role(), "squad_member")
        self.assertEqual(pilot_role(), "squad_commander")
        self.assertEqual(pilot_role(), "squad_commander")
        # The character route is not part of it.
        self.call("PutFleetsFleetId", fleet_id=self.fleet_id, body={"motd": "new"})
        self.assertEqual(self.call("GetCharactersCharacterIdFleet", PILOT, character_id=PILOT)["role"], "squad_commander")
        self.assertEqual(self.call("GetFleetsFleetId", fleet_id=self.fleet_id)["motd"], "")
        self.assertEqual(self.call("GetFleetsFleetId", fleet_id=self.fleet_id)["motd"], "new")

    def test_consecutive_writes_keep_the_first_old_state(self):
        self.sim.stale_reads = 1
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "DPS"})
        self.assertEqual(self.wing_names(), ["Wing 1"])
        self.assertEqual(self.wing_names(), ["DPS"])
        self.call("PostFleetsFleetIdWings", fleet_id=self.fleet_id)
        self.assertEqual(self.wing_names(), ["DPS"])
        self.assertEqual(self.wing_names(), ["DPS", "Wing 2"])

    def test_failed_writes_and_in_game_changes_do_not_count(self):
        self.sim.stale_reads = 1
        with self.assertESIError(404):
            self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=1, body={"name": "Main"})
        self.fleet.rename_wing(self.wing, "Game")
        self.assertEqual(self.wing_names(), ["Game"])

    def test_disband_drops_old_states(self):
        self.sim.stale_reads = 1
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.sim.disband(self.fleet_id)
        self.assertEqual(self.sim._stale, {})

    def test_off_by_default(self):
        self.call("PutFleetsFleetIdWingsWingId", fleet_id=self.fleet_id, wing_id=self.wing, body={"name": "Main"})
        self.assertEqual(self.wing_names(), ["Main"])


class FleetSimResponseStyleTests(SimpleTestCase):
    def setUp(self):
        self.models = FleetSim(response_style="model")
        self.objects = FleetSim(response_style="object")
        for sim in (self.models, self.objects):
            fleet = sim.create_fleet(BOSS, fleet_id=1_000)
            fleet.add_member(PILOT, station_id=60003760)

    def test_models(self):
        boss = token(BOSS)
        detection = self.models.GetCharactersCharacterIdFleet(character_id=BOSS, token=boss).result(use_etag=False)
        self.assertEqual(
            detection.model_dump(),
            {"fleet_boss_id": BOSS, "fleet_id": 1_000, "role": "fleet_commander", "squad_id": -1, "wing_id": -1},
        )
        members = self.models.GetFleetsFleetIdMembers(fleet_id=1_000, token=boss).result(use_etag=False)
        self.assertEqual([member.station_id for member in members], [None, 60003760])
        wings = self.models.GetFleetsFleetIdWings(fleet_id=1_000, token=boss).result(use_etag=False)
        self.assertEqual(
            rules.normalize_wings(wings),
            [{"id": wings[0].id, "name": "Wing 1", "squads": [{"id": wings[0].squads[0].id, "name": "Squad 1"}]}],
        )
        created = self.models.PostFleetsFleetIdWings(fleet_id=1_000, token=boss).result(use_cache=False, store_cache=False)
        self.assertIsInstance(created.wing_id, int)
        with self.assertRaises(HTTPClientError) as caught:
            self.models.GetFleetsFleetIdWings(fleet_id=1_000, token=token(PILOT)).result(use_etag=False, use_cache=False)
        self.assertEqual(caught.exception.data.error, FLEET_NOT_FOUND)

    def test_attribute_objects(self):
        boss = token(BOSS)
        members = self.objects.GetFleetsFleetIdMembers(fleet_id=1_000, token=boss).result(use_etag=False)
        self.assertFalse(hasattr(members[0], "model_dump"))
        self.assertEqual(members[1].station_id, 60003760)
        wings = self.objects.GetFleetsFleetIdWings(fleet_id=1_000, token=boss).result(use_etag=False)
        self.assertEqual(rules.normalize_wings(wings)[0]["squads"][0]["name"], "Squad 1")
        squad = self.objects.PostFleetsFleetIdWingsWingIdSquads(fleet_id=1_000, wing_id=wings[0].id, token=boss).result(
            use_cache=False, store_cache=False
        )
        self.assertIsInstance(squad.squad_id, int)
        with self.assertRaises(HTTPClientError) as caught:
            self.objects.GetFleetsFleetIdWings(fleet_id=1_000, token=token(PILOT)).result(use_etag=False, use_cache=False)
        self.assertEqual(caught.exception.data.error, FLEET_NOT_FOUND)

    def test_pydantic_bodies(self):
        class Rename(BaseModel):
            name: str

        boss = token(BOSS)
        wing_id = self.models.fleets[1_000].wings[0]["id"]
        self.models.PutFleetsFleetIdWingsWingId(fleet_id=1_000, wing_id=wing_id, token=boss, body=Rename(name="Main")).result(
            use_cache=False, store_cache=False
        )
        self.assertEqual(self.models.fleets[1_000].wings[0]["name"], "Main")
        self.assertEqual(self.models.requests[-1].body, {"name": "Main"})

    def test_unknown_style(self):
        with self.assertRaises(ValueError):
            FleetSim(response_style="json")

    def test_install_without_a_test_case(self):
        patcher = self.models.install()
        try:
            self.assertIs(esi_provider.esi.client.Fleets, self.models)
        finally:
            patcher.stop()
        self.assertNotIsInstance(esi_provider.esi, SimpleNamespace)


class FleetSimProviderTests(TestCase):
    """The simulator behind FleetOps' ESI provider, with real ESI tokens."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Sim Boss")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.pilot = f.create_user(main_name="Sim Pilot")
        self.pilot_char = f.main_of(self.pilot)
        f.add_token(self.pilot, self.pilot_char)
        self.sim = FleetSim()
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id)
        self.fleet.add_member(self.pilot_char.character_id)

    def test_provider_reads(self):
        detection = detect_character_fleet(self.fc, self.fc_char.character_id)
        self.assertEqual((detection.fleet_id, detection.role), (self.fleet.fleet_id, "fleet_commander"))
        rows = get_fleet_members(self.fc, self.fc_char.character_id, self.fleet.fleet_id)
        self.assertEqual({row["character_id"] for row in rows}, {self.fc_char.character_id, self.pilot_char.character_id})
        self.assertEqual({request.character_id for request in self.sim.requests}, {self.fc_char.character_id})

    def test_provider_read_by_someone_else_than_the_boss(self):
        with self.assertRaises(FleetESIError) as caught:
            get_fleet_members(self.pilot, self.pilot_char.character_id, self.fleet.fleet_id)
        self.assertEqual(caught.exception.code, "FLEET_NOT_FOUND")
        self.assertEqual(caught.exception.status_code, 404)

    def test_provider_kick(self):
        kick_fleet_member(self.fc, self.fc_char.character_id, self.fleet.fleet_id, self.pilot_char.character_id)
        self.assertNotIn(self.pilot_char.character_id, self.fleet.members)
        with self.assertRaises(FleetESIError) as caught:
            kick_fleet_member(self.fc, self.fc_char.character_id, self.fleet.fleet_id, self.pilot_char.character_id)
        self.assertEqual(caught.exception.code, "MEMBER_NOT_FOUND")

    def test_rules_with_provider_helpers(self):
        """Read the structure, check a move with the rules and send it the way the provider writes."""
        fleets = esi_provider.esi.client.Fleets
        boss_token = esi_provider.get_token(self.fc, self.fc_char.character_id, write=True)
        wings = rules.normalize_wings(
            esi_provider._read(fleets.GetFleetsFleetIdWings(fleet_id=self.fleet.fleet_id, token=boss_token))
        )
        wing_id, squad_id = wings[0]["id"], wings[0]["squads"][0]["id"]
        pilot = {"character_id": self.pilot_char.character_id, "role": "squad_member", "wing_id": wing_id, "squad_id": squad_id}
        boss = {"character_id": self.fc_char.character_id, "role": "fleet_commander", "wing_id": -1, "squad_id": -1}
        body = rules.check_move(pilot, "squad_commander", wing_id, squad_id, wings=wings, members=[boss, pilot])
        esi_provider._write(
            fleets.PutFleetsFleetIdMembersMemberId(
                fleet_id=self.fleet.fleet_id, member_id=self.pilot_char.character_id, token=boss_token, body=body
            )
        )
        self.assertEqual(self.fleet.position(self.pilot_char.character_id), ("squad_commander", wing_id, squad_id))

    def test_scopes_of_real_tokens_are_checked(self):
        reader = f.create_user(main_name="Read Only")
        reader_char = f.main_of(reader)
        read_token = f.add_token(reader, reader_char, scopes=[FLEET_READ_SCOPE])
        self.sim.create_fleet(reader_char.character_id)
        fleets = esi_provider.esi.client.Fleets
        with self.assertRaisesMessage(ValueError, "Token Missing Scopes"):
            esi_provider._write(fleets.PostFleetsFleetIdWings(fleet_id=self.sim.fleet_of(reader_char.character_id).fleet_id, token=read_token))
        self.assertEqual(self.sim.writes(), [])
