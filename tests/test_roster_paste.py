# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for parsing rosters pasted from the game client."""

from dataclasses import asdict

from django.test import SimpleTestCase

from fleetops.services.roster_paste import (
    MAX_NAME_LENGTH,
    SOURCE_CHAT_LOG,
    SOURCE_FLEET,
    SOURCE_NAME_LIST,
    RosterLine,
    clean_name,
    name_error,
    name_key,
    parse_roster,
)

# Copied from the Chinese client's fleet composition window on 2026-10-12. The fleet
# commander's position column is empty but its tab is still there.
CHINESE_FLEET = (
    "Pilot A\tJita (已停靠)\tRagnarok\tTitan\t中队队员\t5 - 5 - 5\t联队1 / 中队1\n"
    "Example FC\tAmarr (已停靠)\tArazu\tForce Recon Ship\t舰队指挥官（统帅）\t5 - 5 - 5\t\n"
    "Pilot B\tDodixie (已停靠)\tMoa\tCruiser\t中队队员\t5 - 5 - 5\t联队1 / 中队1\n"
)

ENGLISH_FLEET = (
    "Name\tLocation\tShip Type\tShip Group\tFleet Role\tFleet Skills\tPosition\r\n"
    "Alpha Pilot\tJita IV - Moon 4 - Caldari Navy Assembly Plant (Docked)\tRaven\tBattleship\t"
    "Fleet Commander (Boss)\t5 - 5 - 5\t\r\n"
    "Bravo Pilot\tPerimeter\tScimitar\tLogistics\tSquad Member\t0 - 0 - 0\tWing 1 / Squad 1\r\n"
    "Charlie Pilot\tAmarr VIII (Oris) - Emperor Family Academy (Docked)\tGuardian\tLogistics\t"
    "Squad Commander\t5 - 5 - 4\tWing 1 / Squad 2\r\n"
)


def names(lines):
    return [line.name for line in lines]


class ChineseFleetCompositionTests(SimpleTestCase):
    def test_real_sample_is_split_into_columns(self):
        lines, warnings = parse_roster(CHINESE_FLEET)

        self.assertEqual(warnings, [])
        self.assertEqual(names(lines), ["Pilot A", "Example FC", "Pilot B"])
        self.assertEqual(
            asdict(lines[0]),
            {
                "name": "Pilot A",
                "location": "Jita",
                "ship_type": "Ragnarok",
                "ship_group": "Titan",
                "role": "中队队员",
                "position": "联队1 / 中队1",
                "line_no": 1,
                "skills": "5 - 5 - 5",
                "docked": True,
                "source": SOURCE_FLEET,
                "error": "",
            },
        )

    def test_boss_row_keeps_role_and_has_empty_position(self):
        lines, _ = parse_roster(CHINESE_FLEET)
        boss = lines[1]

        self.assertEqual(boss.location, "Amarr")
        self.assertEqual(boss.ship_type, "Arazu")
        self.assertEqual(boss.ship_group, "Force Recon Ship")
        self.assertEqual(boss.role, "舰队指挥官（统帅）")
        self.assertEqual(boss.skills, "5 - 5 - 5")
        self.assertEqual(boss.position, "")
        self.assertEqual(boss.line_no, 2)
        self.assertTrue(boss.is_valid)

    def test_docked_marker_is_removed_from_every_location(self):
        lines, _ = parse_roster(CHINESE_FLEET)

        self.assertEqual([line.location for line in lines], ["Jita", "Amarr", "Dodixie"])
        self.assertTrue(all(line.docked for line in lines))
        self.assertTrue(all(line.source == SOURCE_FLEET for line in lines))

    def test_chinese_header_row_is_skipped(self):
        header = "名字\t地点\t舰船类型\t舰船组\t舰队职位\t舰队技能\t舰队位置\n"
        lines, warnings = parse_roster(header + CHINESE_FLEET)

        self.assertEqual(warnings, [])
        self.assertEqual(names(lines), ["Pilot A", "Example FC", "Pilot B"])
        self.assertEqual(lines[0].line_no, 2)

    def test_header_detected_by_ship_type_column(self):
        for header in ("角色\t地点\t舰船类型\n", "名称\tx\ty\n", "Pilot\tWhere\t舰船类型\n", "Who\tWhere\tShip Type\n"):
            with self.subTest(header=header):
                lines, _ = parse_roster(header + "Real Pilot\tJita\tRaven\n")
                self.assertEqual(names(lines), ["Real Pilot"])

    def test_full_width_parentheses_and_location_in_space(self):
        text = "Pilot One\tJita（已停靠）\tRagnarok\nPilot Two\tJita\tRagnarok\nPilot Three\tJita ( 已停靠 ) \tMoa\n"
        lines, _ = parse_roster(text)

        self.assertEqual([(line.location, line.docked) for line in lines], [("Jita", True), ("Jita", False), ("Jita", True)])

    def test_rows_from_an_indented_paste_are_still_read(self):
        indented = "".join(f"  {line}\n" for line in CHINESE_FLEET.splitlines())
        lines, warnings = parse_roster(indented)

        self.assertEqual(warnings, [])
        self.assertEqual(names(lines), ["Pilot A", "Example FC", "Pilot B"])
        self.assertEqual(lines[2].position, "联队1 / 中队1")


class EnglishFleetCompositionTests(SimpleTestCase):
    def test_english_client_sample(self):
        lines, warnings = parse_roster(ENGLISH_FLEET)

        self.assertEqual(warnings, [])
        self.assertEqual(names(lines), ["Alpha Pilot", "Bravo Pilot", "Charlie Pilot"])
        boss, member, commander = lines
        self.assertEqual(boss.location, "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
        self.assertTrue(boss.docked)
        self.assertEqual(boss.role, "Fleet Commander (Boss)")
        self.assertEqual(boss.position, "")
        self.assertEqual(member.location, "Perimeter")
        self.assertFalse(member.docked)
        self.assertEqual(member.role, "Squad Member")
        self.assertEqual(member.position, "Wing 1 / Squad 1")
        self.assertEqual(member.ship_group, "Logistics")
        self.assertEqual(commander.location, "Amarr VIII (Oris) - Emperor Family Academy")
        self.assertEqual(commander.skills, "5 - 5 - 4")
        self.assertEqual([line.line_no for line in lines], [2, 3, 4])

    def test_docked_marker_is_case_insensitive_and_only_at_the_end(self):
        text = "Pilot One\tJita (docked)\nPilot Two\tStation (Docked) Annex\nPilot Three\tDodixie (DOCKED)  \n"
        lines, _ = parse_roster(text)

        self.assertEqual(
            [(line.location, line.docked) for line in lines],
            [("Jita", True), ("Station (Docked) Annex", False), ("Dodixie", True)],
        )

    def test_missing_trailing_columns_are_blank(self):
        lines, _ = parse_roster("Short Row\tJita\n")

        self.assertEqual(lines[0].location, "Jita")
        self.assertEqual((lines[0].ship_type, lines[0].ship_group, lines[0].role, lines[0].skills, lines[0].position), ("", "", "", "", ""))
        self.assertEqual(lines[0].source, SOURCE_FLEET)

    def test_extra_columns_are_ignored(self):
        lines, _ = parse_roster("Wide Row\tJita\tRaven\tBattleship\tSquad Member\t5 - 5 - 5\tWing 1 / Squad 1\textra\tmore\n")

        self.assertEqual(lines[0].position, "Wing 1 / Squad 1")

    def test_cells_are_stripped(self):
        lines, _ = parse_roster("  Spaced Pilot \t Jita (Docked) \t Raven \t Battleship \t Squad Member \t 5 - 5 - 5 \t Wing 1 / Squad 1 \n")
        line = lines[0]

        self.assertEqual(
            (line.name, line.location, line.ship_type, line.ship_group, line.role, line.skills, line.position),
            ("Spaced Pilot", "Jita", "Raven", "Battleship", "Squad Member", "5 - 5 - 5", "Wing 1 / Squad 1"),
        )

    def test_row_without_name_is_reported(self):
        lines, warnings = parse_roster("Good Pilot\tJita\n\tJita\tRaven\n")

        self.assertEqual(names(lines), ["Good Pilot"])
        self.assertEqual(warnings, ["Line 2: no character name before the first tab, the line was skipped."])

    def test_lines_of_only_tabs_are_blank(self):
        lines, warnings = parse_roster("\t\t\t\nGood Pilot\tJita\n \t \n")

        self.assertEqual(names(lines), ["Good Pilot"])
        self.assertEqual(lines[0].line_no, 2)
        self.assertEqual(warnings, [])


class NameListTests(SimpleTestCase):
    def test_one_name_per_line(self):
        text = "Pilot A\nPilot C\nExample FC\nPilot Cccccc\nPilot Ccccc\nPilot Ccc\nPilot Cc\nPilot B\n"
        lines, warnings = parse_roster(text)

        self.assertEqual(warnings, [])
        self.assertEqual(
            names(lines),
            [
                "Pilot A",
                "Pilot C",
                "Example FC",
                "Pilot Cccccc",
                "Pilot Ccccc",
                "Pilot Ccc",
                "Pilot Cc",
                "Pilot B",
            ],
        )
        self.assertTrue(all(line.source == SOURCE_NAME_LIST for line in lines))
        self.assertTrue(all(line.is_valid for line in lines))
        self.assertEqual(lines[0].location, "")

    def test_blank_lines_and_line_endings(self):
        lines, warnings = parse_roster("\r\n  First Pilot  \r\n\r\n\nSecond Pilot\rThird Pilot\n\n")

        self.assertEqual(warnings, [])
        self.assertEqual(names(lines), ["First Pilot", "Second Pilot", "Third Pilot"])
        self.assertEqual([line.line_no for line in lines], [2, 5, 6])

    def test_whitespace_and_invisible_characters_are_cleaned(self):
        lines, _ = parse_roster("﻿First Pilot\nSecond   Pilot​\n　第三 飞行员　\n")

        self.assertEqual(names(lines), ["First Pilot", "Second Pilot", "第三 飞行员"])
        self.assertTrue(all(line.is_valid for line in lines))

    def test_names_with_apostrophe_hyphen_and_digits(self):
        lines, _ = parse_roster("O'Neil Pilot\nX-Ray 7\nJ.R. Pilot\n舰长\n")

        self.assertEqual([line.error for line in lines], ["", "", "", ""])

    def test_separator_lines_are_skipped(self):
        lines, warnings = parse_roster("-----\nReal Pilot\n***\n")

        self.assertEqual(names(lines), ["Real Pilot"])
        self.assertEqual(warnings, [])

    def test_empty_input(self):
        for text in ("", "   \n\n\t\n", None):
            with self.subTest(text=text):
                self.assertEqual(parse_roster(text), ([], []))


class ChatLogTests(SimpleTestCase):
    def test_speakers_are_taken_from_chat_lines(self):
        text = (
            "[12:18:36] Pilot A > o7\n"
            "[12:18:40] Pilot B > x up\n"
            "[ 2026.10.12 12:19:02 ] Pilot Cc > 舰队集合\n"
        )
        lines, warnings = parse_roster(text)

        self.assertEqual(warnings, [])
        self.assertEqual(names(lines), ["Pilot A", "Pilot B", "Pilot Cc"])
        self.assertTrue(all(line.source == SOURCE_CHAT_LOG for line in lines))
        self.assertEqual([line.line_no for line in lines], [1, 2, 3])
        self.assertEqual(lines[0].location, "")

    def test_repeated_speakers_are_kept_once_without_warning(self):
        text = "[12:18:36] Pilot A > o7\n[12:18:37] pilot a > x\n[12:18:38] Pilot A > again\n"
        lines, warnings = parse_roster(text)

        self.assertEqual(names(lines), ["Pilot A"])
        self.assertEqual(warnings, [])

    def test_message_text_with_arrows_and_tabs(self):
        text = "[12:18:36] Pilot B > align > gate\tnow\n[12:18:37] Empty Message >\n"
        lines, _ = parse_roster(text)

        self.assertEqual(names(lines), ["Pilot B", "Empty Message"])
        self.assertEqual(lines[0].source, SOURCE_CHAT_LOG)

    def test_system_messages_are_skipped(self):
        text = "[12:00:00] EVE System > Channel changed to Local\n[12:00:01] EVE系统 > 频道已更改\n[12:00:02] Real Pilot > hi\n"
        lines, _ = parse_roster(text)

        self.assertEqual(names(lines), ["Real Pilot"])

    def test_log_file_header_lines_are_ignored(self):
        text = (
            "---------------------------------------------------------------\n"
            "  Channel ID:      -12345\n"
            "  Channel Name:    Fleet\n"
            "  Listener:        Real Pilot\n"
            "  Session started: 2026.10.12 12:00:00\n"
            "---------------------------------------------------------------\n"
            "[ 2026.10.12 12:00:05 ] Real Pilot > o7\n"
            "[ 2026.10.12 12:00:09 ] Other Pilot > o/\n"
        )
        lines, warnings = parse_roster(text)

        self.assertEqual(names(lines), ["Real Pilot", "Other Pilot"])
        self.assertEqual(warnings, ["Ignored 4 lines that are not chat messages."])

    def test_single_ignored_line_wording(self):
        lines, warnings = parse_roster("stray text\n[12:00:05] Real Pilot > o7\n")

        self.assertEqual(names(lines), ["Real Pilot"])
        self.assertEqual(warnings, ["Ignored 1 line that is not a chat message."])

    def test_brackets_without_a_time_are_not_chat_lines(self):
        lines, _ = parse_roster("[CORP] Pilot > text\n")

        self.assertEqual(lines[0].source, SOURCE_NAME_LIST)
        self.assertFalse(lines[0].is_valid)

    def test_fleet_rows_are_kept_next_to_chat_lines(self):
        lines, warnings = parse_roster("[12:00:05] Real Pilot > o7\nFleet Pilot\tJita\tRaven\n")

        self.assertEqual(names(lines), ["Real Pilot", "Fleet Pilot"])
        self.assertEqual(lines[1].source, SOURCE_FLEET)
        self.assertEqual(warnings, [])


class DedupeAndLimitTests(SimpleTestCase):
    def test_duplicates_are_case_insensitive_and_first_wins(self):
        text = CHINESE_FLEET + "PILOT A\npilot  b\nNew Pilot\n"
        lines, warnings = parse_roster(text)

        self.assertEqual(names(lines), ["Pilot A", "Example FC", "Pilot B", "New Pilot"])
        self.assertEqual(lines[0].source, SOURCE_FLEET)
        self.assertEqual(lines[0].ship_type, "Ragnarok")
        self.assertEqual(warnings, ["Skipped 2 duplicate names."])

    def test_single_duplicate_wording(self):
        _, warnings = parse_roster("Pilot\npilot\n")

        self.assertEqual(warnings, ["Skipped 1 duplicate name."])

    def test_similar_names_are_not_merged(self):
        lines, _ = parse_roster("Pilot C\nPilot Cc\nPilot Ccc\n")

        self.assertEqual(len(lines), 3)

    def test_max_lines_keeps_the_first_names(self):
        text = "\n".join(f"Pilot {number}" for number in range(1, 6))
        lines, warnings = parse_roster(text, max_lines=3)

        self.assertEqual(names(lines), ["Pilot 1", "Pilot 2", "Pilot 3"])
        self.assertEqual(warnings, ["Only the first 3 names are used, 2 more were ignored."])

    def test_max_lines_counts_names_not_repeated_lines(self):
        text = "Pilot 1\nPilot 1\nPilot 2\nPilot 3\npilot 3\nPilot 4\n"
        lines, warnings = parse_roster(text, max_lines=3)

        self.assertEqual(names(lines), ["Pilot 1", "Pilot 2", "Pilot 3"])
        self.assertEqual(
            warnings,
            ["Skipped 2 duplicate names.", "Only the first 3 names are used, 1 more was ignored."],
        )

    def test_repeated_names_beyond_the_limit_are_counted_once(self):
        _, warnings = parse_roster("Pilot 1\nPilot 2\nPilot 2b\npilot 2b\n", max_lines=2)

        self.assertEqual(warnings, ["Skipped 1 duplicate name.", "Only the first 2 names are used, 1 more was ignored."])

    def test_default_limit(self):
        text = "\n".join(f"Pilot {number}" for number in range(1005))
        lines, warnings = parse_roster(text)

        self.assertEqual(len(lines), 1000)
        self.assertEqual(warnings, ["Only the first 1000 names are used, 5 more were ignored."])


class InvalidNameTests(SimpleTestCase):
    def test_long_names_are_kept_as_invalid(self):
        longest = "A" * MAX_NAME_LENGTH
        too_long = "B" * (MAX_NAME_LENGTH + 1)
        lines, warnings = parse_roster(f"{longest}\n{too_long}\n")

        self.assertEqual(warnings, [])
        self.assertTrue(lines[0].is_valid)
        self.assertFalse(lines[1].is_valid)
        self.assertEqual(lines[1].name, too_long)
        self.assertEqual(lines[1].error, "Character names are at most 37 characters long.")

    def test_fleet_row_with_spaces_instead_of_tabs_is_invalid(self):
        lines, _ = parse_roster("Pilot A Jita (已停靠) Ragnarok Titan 中队队员 5 - 5 - 5 联队1 / 中队1\n")

        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0].is_valid)

    def test_disallowed_characters(self):
        for name in ("Pilot: One", "<b>Pilot</b>", "Pilot/Two", "Pilot (alt)", "Pilot@corp"):
            with self.subTest(name=name):
                lines, _ = parse_roster(name)
                self.assertEqual(lines[0].error, "Contains characters that cannot appear in a character name.")

    def test_invalid_names_take_part_in_dedupe(self):
        too_long = "C" * 40
        lines, warnings = parse_roster(f"{too_long}\n{too_long.lower()}\n")

        self.assertEqual(len(lines), 1)
        self.assertEqual(warnings, ["Skipped 1 duplicate name."])


class MixedPasteTests(SimpleTestCase):
    def test_fleet_composition_with_names_typed_below(self):
        lines, warnings = parse_roster(CHINESE_FLEET + "\nPilot C\nPilot Cc\n")

        self.assertEqual(warnings, [])
        self.assertEqual(
            [(line.name, line.source) for line in lines],
            [
                ("Pilot A", SOURCE_FLEET),
                ("Example FC", SOURCE_FLEET),
                ("Pilot B", SOURCE_FLEET),
                ("Pilot C", SOURCE_NAME_LIST),
                ("Pilot Cc", SOURCE_NAME_LIST),
            ],
        )
        self.assertEqual([line.line_no for line in lines], [1, 2, 3, 5, 6])

    def test_english_and_chinese_rows_in_one_paste(self):
        lines, _ = parse_roster(ENGLISH_FLEET + CHINESE_FLEET)

        self.assertEqual(len(lines), 6)
        self.assertEqual([line.docked for line in lines], [True, False, True, True, True, True])


class HelperTests(SimpleTestCase):
    def test_clean_name(self):
        self.assertEqual(clean_name("  Pilot\u00a0 A\tOne\u200b "), "Pilot A One")
        self.assertEqual(clean_name(""), "")
        self.assertEqual(clean_name(None), "")

    def test_name_key(self):
        self.assertEqual(name_key(" PILOT  b "), "pilot b")
        self.assertEqual(name_key("Pilot B"), name_key("pilot b"))

    def test_name_error(self):
        self.assertEqual(name_error("Pilot B"), "")
        self.assertEqual(name_error("中队队员"), "")
        self.assertNotEqual(name_error("x" * 38), "")
        self.assertNotEqual(name_error("a > b"), "")

    def test_roster_line_key_and_defaults(self):
        line = RosterLine(name="Pilot B")

        self.assertEqual(line.key, "pilot b")
        self.assertTrue(line.is_valid)
        self.assertEqual(line.source, SOURCE_NAME_LIST)
        self.assertFalse(line.docked)

    def test_rows_are_serialisable(self):
        lines, _ = parse_roster(CHINESE_FLEET)

        self.assertEqual(RosterLine(**asdict(lines[1])), lines[1])
