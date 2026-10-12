"""Tests for ping mentions: selections, channel rules and the exact Discord payload."""

from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from fleetops.models import FleetOperation, PingRole
from fleetops.services import mentions
from fleetops.services.mentions import (
    EVERYONE,
    EXCLUSIVE_CHOICE_ERROR,
    HERE,
    NONE,
    ROLES,
    MentionSelection,
    RoleRef,
    allowed_mentions,
    choice_values,
    discord_length,
    mention_text,
    neutralize_mass_mentions,
    role_choice,
    selection_from_choices,
    selection_from_operation,
    snapshot_roles,
    target_label,
    validate_for_channel,
)

from . import factories as f

ZWSP = "\u200b"
SIG_A = RoleRef("111", "Sig A")
SIG_B = RoleRef("222", "Sig B")
TWO_ROLES = MentionSelection(ROLES, (SIG_A, SIG_B))


def operation(**values):
    return SimpleNamespace(**values)


def channel(name="STR", *, allow_everyone=False, allow_here=False, allowed_roles=()):
    return SimpleNamespace(
        name=name, allow_everyone=allow_everyone, allow_here=allow_here, allowed_roles=list(allowed_roles)
    )


def messages(error: ValidationError) -> list[str]:
    return list(error.messages)


class SelectionFromOperationTests(SimpleTestCase):
    def test_modes(self):
        self.assertEqual(selection_from_operation(operation(ping_mention_mode=NONE)), MentionSelection())
        self.assertEqual(selection_from_operation(operation(ping_mention_mode=EVERYONE)), MentionSelection(EVERYONE))
        self.assertEqual(selection_from_operation(operation(ping_mention_mode=HERE)), MentionSelection(HERE))

    def test_roles_come_from_the_snapshot(self):
        op = operation(ping_mention_mode=ROLES, ping_roles=[{"id": "111", "name": "Sig A"}, {"id": 222, "name": "Sig B"}])

        self.assertEqual(selection_from_operation(op), TWO_ROLES)

    def test_broken_snapshot_entries_are_skipped(self):
        op = operation(
            ping_mention_mode=ROLES,
            ping_roles=[{"id": "111", "name": "Sig A"}, {"id": "abc", "name": "X"}, "222", {"name": "Y"}, {"id": "111", "name": "Again"}],
        )

        self.assertEqual(selection_from_operation(op), MentionSelection(ROLES, (SIG_A,)))

    def test_roles_are_ignored_for_other_modes(self):
        op = operation(ping_mention_mode=EVERYONE, ping_roles=[{"id": "111", "name": "Sig A"}])

        self.assertEqual(selection_from_operation(op), MentionSelection(EVERYONE))

    def test_unknown_or_missing_mode_is_no_ping(self):
        self.assertEqual(selection_from_operation(operation(ping_mention_mode="all")), MentionSelection())
        self.assertEqual(selection_from_operation(operation()), MentionSelection())
        self.assertEqual(selection_from_operation(operation(ping_mention_mode="")), MentionSelection())


class SelectionFromOperationDatabaseTests(TestCase):
    def test_model_choices_are_the_mention_modes(self):
        self.assertEqual(tuple(FleetOperation.MentionMode.values), mentions.MENTION_MODES)

    def test_stored_operation(self):
        fc = f.create_user(perms=f.FC_PERMS)
        op = f.create_operation(
            fc, ping_mention_mode=FleetOperation.MentionMode.ROLES, ping_roles=snapshot_roles(TWO_ROLES)
        )
        op.refresh_from_db()

        self.assertEqual(selection_from_operation(op), TWO_ROLES)


class SelectionFromChoicesTests(TestCase):
    def setUp(self):
        self.sig_b = f.ping_role("Sig B", role_id=222, sort_order=2)
        self.sig_a = f.ping_role("Sig A", role_id=111, sort_order=1)

    def test_single_choices(self):
        self.assertEqual(selection_from_choices([]), MentionSelection())
        self.assertEqual(selection_from_choices(None), MentionSelection())
        self.assertEqual(selection_from_choices(["none"]), MentionSelection())
        self.assertEqual(selection_from_choices(["everyone"]), MentionSelection(EVERYONE))
        self.assertEqual(selection_from_choices(["here"]), MentionSelection(HERE))
        self.assertEqual(selection_from_choices(["here", "here"]), MentionSelection(HERE))

    def test_roles_are_loaded_in_configured_order(self):
        selection = selection_from_choices([role_choice(self.sig_b), role_choice(self.sig_a), role_choice(self.sig_b)])

        self.assertEqual(selection, TWO_ROLES)

    def test_exclusive_choices_cannot_be_combined(self):
        for values in (
            ["none", "everyone"],
            ["everyone", "here"],
            ["here", role_choice(self.sig_a)],
            ["none", role_choice(self.sig_a)],
            [role_choice(self.sig_a), "everyone"],
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError) as caught:
                selection_from_choices(values)
            self.assertEqual(messages(caught.exception), [EXCLUSIVE_CHOICE_ERROR])
        self.assertEqual(EXCLUSIVE_CHOICE_ERROR, "@everyone, @here and No ping cannot be combined with other targets.")

    def test_inactive_and_deleted_roles_are_rejected(self):
        self.sig_b.is_active = False
        self.sig_b.save()
        deleted = f.ping_role("Gone", role_id=333)
        deleted_value = role_choice(deleted)
        deleted.delete()

        with self.assertRaises(ValidationError) as caught:
            selection_from_choices([role_choice(self.sig_a), role_choice(self.sig_b), deleted_value])

        self.assertEqual(
            messages(caught.exception),
            ["Role Sig B is no longer available.", "A selected role is no longer available."],
        )

    def test_unknown_values_are_rejected(self):
        for value in ("all", "role:", "role:abc", "role:-1", "role:99999999999999999999999", "<@&111>"):
            with self.subTest(value=value), self.assertRaises(ValidationError) as caught:
                selection_from_choices([value])
            self.assertEqual(messages(caught.exception), [f"Unknown ping target {value}."])

    def test_choice_values_round_trip(self):
        selection = selection_from_choices([role_choice(self.sig_a), role_choice(self.sig_b)])

        self.assertEqual(choice_values(selection), [role_choice(self.sig_a), role_choice(self.sig_b)])
        self.assertEqual(choice_values(MentionSelection()), ["none"])
        self.assertEqual(choice_values(MentionSelection(HERE)), ["here"])

    def test_choice_values_leave_out_roles_that_no_longer_exist(self):
        selection = MentionSelection(ROLES, (SIG_A, RoleRef("444", "Gone")))

        self.assertEqual(choice_values(selection), [role_choice(self.sig_a)])


class SnapshotTests(SimpleTestCase):
    def test_snapshot_of_roles(self):
        self.assertEqual(snapshot_roles(TWO_ROLES), [{"id": "111", "name": "Sig A"}, {"id": "222", "name": "Sig B"}])

    def test_no_snapshot_for_other_modes(self):
        for selection in (MentionSelection(), MentionSelection(EVERYONE), MentionSelection(HERE)):
            with self.subTest(mode=selection.mode):
                self.assertEqual(snapshot_roles(selection), [])

    def test_notifies(self):
        self.assertFalse(MentionSelection().notifies)
        self.assertFalse(MentionSelection(ROLES).notifies)
        self.assertTrue(MentionSelection(EVERYONE).notifies)
        self.assertTrue(MentionSelection(HERE).notifies)
        self.assertTrue(TWO_ROLES.notifies)


class ValidateForChannelTests(TestCase):
    def setUp(self):
        self.sig_a = f.ping_role("Sig A", role_id=111)
        self.sig_b = f.ping_role("Sig B", role_id=222)

    def test_copy_only_accepts_anything(self):
        many = MentionSelection(ROLES, tuple(RoleRef(str(1000 + n), f"R{n}") for n in range(150)))
        for selection in (MentionSelection(), MentionSelection(EVERYONE), MentionSelection(HERE), TWO_ROLES, many):
            with self.subTest(mode=selection.mode):
                self.assertEqual(validate_for_channel(selection, None), [])

    def test_no_ping_is_always_allowed(self):
        self.assertEqual(validate_for_channel(MentionSelection(), channel()), [])

    def test_everyone_and_here_follow_the_channel_switches(self):
        self.assertEqual(
            validate_for_channel(MentionSelection(EVERYONE), channel(allow_here=True)),
            ["@everyone is not allowed in STR."],
        )
        self.assertEqual(
            validate_for_channel(MentionSelection(HERE), channel(allow_everyone=True)),
            ["@here is not allowed in STR."],
        )
        self.assertEqual(validate_for_channel(MentionSelection(EVERYONE), channel(allow_everyone=True)), [])
        self.assertEqual(validate_for_channel(MentionSelection(HERE), channel(allow_here=True)), [])

    def test_every_active_role_is_allowed_without_a_role_list(self):
        self.assertEqual(validate_for_channel(TWO_ROLES, channel()), [])

    def test_role_list_restricts_the_roles(self):
        self.assertEqual(
            validate_for_channel(TWO_ROLES, channel(allowed_roles=[self.sig_a])),
            ["Role Sig B is not allowed in STR."],
        )

    def test_inactive_and_deleted_roles_are_no_longer_available(self):
        self.sig_b.is_active = False
        self.sig_b.save()
        selection = MentionSelection(ROLES, (SIG_A, SIG_B, RoleRef("333", "Sig C")))

        self.assertEqual(
            validate_for_channel(selection, channel(allowed_roles=[self.sig_a, self.sig_b])),
            ["Role Sig B is no longer available.", "Role Sig C is no longer available."],
        )

    def test_more_than_one_hundred_roles(self):
        roles = [PingRole(name=f"R{n}", role_id=1000 + n) for n in range(101)]
        PingRole.objects.bulk_create(roles)
        selection = MentionSelection(ROLES, tuple(RoleRef(str(role.role_id), role.name) for role in roles))

        self.assertEqual(
            validate_for_channel(selection, channel()),
            ["Select at most 100 roles; Discord cannot mention more in one message."],
        )
        self.assertEqual(validate_for_channel(MentionSelection(ROLES, selection.roles[:100]), channel()), [])

    def test_stored_ping_channel(self):
        restricted = f.ping_channel("Fleet Pings", allow_here=True, roles=[self.sig_a])
        open_channel = f.ping_channel("Open")

        self.assertEqual(validate_for_channel(MentionSelection(HERE), restricted), [])
        self.assertEqual(
            validate_for_channel(MentionSelection(EVERYONE), restricted), ["@everyone is not allowed in Fleet Pings."]
        )
        self.assertEqual(validate_for_channel(TWO_ROLES, restricted), ["Role Sig B is not allowed in Fleet Pings."])
        self.assertEqual(validate_for_channel(TWO_ROLES, open_channel), [])
        self.assertEqual(validate_for_channel(MentionSelection(HERE), open_channel), ["@here is not allowed in Open."])


class MentionTextTests(SimpleTestCase):
    def test_mention_text(self):
        self.assertEqual(mention_text(MentionSelection()), "")
        self.assertEqual(mention_text(MentionSelection(EVERYONE)), "@everyone")
        self.assertEqual(mention_text(MentionSelection(HERE)), "@here")
        self.assertEqual(mention_text(TWO_ROLES), "<@&111> <@&222>")
        self.assertEqual(mention_text(MentionSelection(ROLES)), "")

    def test_display_text(self):
        self.assertEqual(mention_text(MentionSelection(), display=True), "")
        self.assertEqual(mention_text(MentionSelection(EVERYONE), display=True), "@everyone")
        self.assertEqual(mention_text(MentionSelection(HERE), display=True), "@here")
        self.assertEqual(mention_text(TWO_ROLES, display=True), "@Sig A @Sig B")
        self.assertEqual(mention_text(MentionSelection(ROLES, (RoleRef("333", ""),)), display=True), "@role 333")

    def test_target_label(self):
        self.assertEqual(target_label(MentionSelection()), "this channel")
        self.assertEqual(target_label(MentionSelection(EVERYONE)), "all")
        self.assertEqual(target_label(MentionSelection(HERE)), "online members")
        self.assertEqual(target_label(TWO_ROLES), "Sig A, Sig B")
        self.assertEqual(target_label(MentionSelection(ROLES)), "this channel")


class AllowedMentionsTests(SimpleTestCase):
    def test_exact_payload_per_selection(self):
        self.assertEqual(allowed_mentions(MentionSelection()), {"parse": []})
        self.assertEqual(allowed_mentions(MentionSelection(EVERYONE)), {"parse": ["everyone"]})
        self.assertEqual(allowed_mentions(MentionSelection(HERE)), {"parse": ["everyone"]})
        self.assertEqual(allowed_mentions(TWO_ROLES), {"parse": [], "roles": ["111", "222"]})

    def test_role_mentions_never_use_parse(self):
        # Discord rejects "parse": ["roles"] together with a "roles" list, and parse would allow every role.
        payload = allowed_mentions(TWO_ROLES)

        self.assertNotIn("roles", payload["parse"])
        self.assertNotIn("users", payload["parse"])

    def test_role_typed_in_the_text_is_not_allowed(self):
        text = f"{mention_text(MentionSelection(ROLES, (SIG_A,)))} bring <@&999>"

        self.assertIn("<@&999>", text)
        self.assertEqual(allowed_mentions(MentionSelection(ROLES, (SIG_A,))), {"parse": [], "roles": ["111"]})

    def test_users_are_never_parsed(self):
        for selection in (MentionSelection(), MentionSelection(EVERYONE), MentionSelection(HERE), TWO_ROLES):
            with self.subTest(mode=selection.mode):
                self.assertNotIn("users", allowed_mentions(selection)["parse"])
                self.assertNotIn("users", allowed_mentions(selection))


class NeutralizeTests(SimpleTestCase):
    def test_mass_mentions_are_broken(self):
        self.assertEqual(
            neutralize_mass_mentions("@everyone form up, @here too"), f"@{ZWSP}everyone form up, @{ZWSP}here too"
        )

    def test_neutralised_text_shows_the_same_words(self):
        text = neutralize_mass_mentions("@here")

        self.assertNotIn("@here", text)
        self.assertEqual(text.replace(ZWSP, ""), "@here")

    def test_idempotent_and_other_text_unchanged(self):
        once = neutralize_mass_mentions("@everyone")

        self.assertEqual(neutralize_mass_mentions(once), once)
        self.assertEqual(neutralize_mass_mentions("<@&111> @Sig A @Everyone"), "<@&111> @Sig A @Everyone")
        self.assertEqual(neutralize_mass_mentions(""), "")
        self.assertEqual(neutralize_mass_mentions(None), "")


class DiscordLengthTests(SimpleTestCase):
    def test_utf16_code_units(self):
        self.assertEqual(discord_length(""), 0)
        self.assertEqual(discord_length(None), 0)
        self.assertEqual(discord_length("Form up"), 7)
        self.assertEqual(discord_length("救世级"), 3)
        self.assertEqual(discord_length("\U0001F680"), 2)
        self.assertEqual(discord_length("a\U0001F680b"), 4)

    def test_limits(self):
        self.assertEqual(mentions.DISCORD_MESSAGE_LIMIT, 2000)
        self.assertEqual(mentions.SUPPRESS_EMBEDS, 4)
        self.assertEqual(mentions.MAX_ROLE_MENTIONS, 100)
