# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Messages the code stores are declared in ``fleetops.texts`` and do not depend on the language.

The first tests read the source: no message that is stored or raised for a person is a bare string. The
others run the main flows with ESI and Discord replaced by fakes, and check that every text they leave in
the database is a declared message, whatever language the request uses.
"""

import ast
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests
from django.test import SimpleTestCase
from django.utils import timezone, translation

import fleetops
from fleetops import texts
from fleetops.models import (
    AuditLog,
    FleetOperation,
    FleetStructureTemplate,
    MessageTemplate,
    OperationAction,
    OperationActionAttempt,
    PingRole,
    SrpClaim,
    SrpClaimEvent,
)
from fleetops.providers import esi as esi_provider
from fleetops.providers.srp import SRPLinkResult
from fleetops.services import fleet_access, fleet_controls, fleet_jobs, fleet_rules, mentions
from fleetops.services.fleet_invites import invite_characters
from fleetops.services.fleet_manage import MoveRequest, add_squad, add_wing, delete_squad, move_members, rename_wing
from fleetops.services.motd import regenerate_and_push_motd
from fleetops.services.operations import (
    create_manual_fleet,
    end_fleet,
    request_fleet_end,
    retry_motd,
    retry_ping,
    retry_srp,
)
from fleetops.services.scheduling import cancel_scheduled_fleet, schedule_fleet
from fleetops.services.tracking import UnreadableFleet
from fleetops.srp import claims, killmails, review
from fleetops.srp.types import SrpError

from . import factories as f
from .test_fleet_invites import SimFleetTestCase
from .test_fleet_templates import TemplateJobTestCase
from .test_operations_tracking import FLEET_ID, StartFleetTestBase, http_error
from .test_srp_claims import ClaimBase
from .test_texts import CATALOG, FakeCatalogMixin

EndReason = FleetOperation.EndReason
Status = FleetOperation.Status
PACKAGE_DIR = Path(fleetops.__file__).resolve().parent
# The files that store text or raise it for a person.
SERVICE_FILES = sorted(
    path
    for folder in ("services", "providers", "srp")
    for path in (PACKAGE_DIR / folder).glob("*.py")
    if path.name != "__init__.py"
) + [PACKAGE_DIR / "tasks.py"]


def calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node, getattr(node.func, "id", getattr(node.func, "attr", ""))


def is_text(node):
    """A string literal or f-string that holds words."""
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(part, ast.Constant) and any(char.isalpha() for char in part.value) for part in node.values
        )
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and any(c.isalpha() for c in node.value)


def argument(node, position, name):
    if len(node.args) > position:
        return node.args[position]
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


class SourceGuardTests(SimpleTestCase):
    """Texts reach the database and the pages through declared messages, never as loose strings."""

    # (call name, position of the message, keyword of the message)
    MESSAGE_SINKS = (
        ("set_action", 3, "error"),
        ("FleetESIError", 1, "message"),
        ("FleetRuleError", 0, "message"),
        ("FleetManageError", 0, "message"),
        ("FleetActionResult", 1, "message"),
        ("KickRefused", 0, None),
        ("JobStopped", 0, None),
        ("PapLinkError", 0, None),
        ("IncentiveError", 0, None),
        ("SrpError", 1, "message"),
        ("SrpStateError", 1, "message"),
        ("SrpLimitError", 1, "message"),
        ("PingResult", 99, "message"),
        ("SRPLinkResult", 99, "message"),
        ("PermissionError", 0, None),
    )
    # Where the step of a failure is given: the position of the argument.
    STEP_ARGUMENTS = {"report_failure": 1, "failure_message": 0, "failure_answer": 2, "job_failure": 3, "_failed": 4}

    def test_messages_are_not_loose_strings(self):
        loose = []
        for path in SERVICE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node, name in calls(tree):
                for sink, position, keyword in self.MESSAGE_SINKS:
                    if name == sink:
                        message = argument(node, position, keyword)
                        if message is not None and is_text(message):
                            loose.append(f"{path.name}:{node.lineno} {sink}")
        self.assertEqual(loose, [])

    def test_the_steps_of_failures_are_declared(self):
        """The name of a failed step is a nested message of ``STEP_FAILED``, so it has to be declared."""
        undeclared = []
        for path in SERVICE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node, name in calls(tree):
                position = self.STEP_ARGUMENTS.get(name)
                if position is None and name == "failed" and len(node.args) >= 4:
                    position = 2
                step = argument(node, position, "step") if position is not None else None
                is_name = isinstance(step, ast.Constant) and isinstance(step.value, str)
                if is_name and step.value not in texts.STEP_NAMES:
                    undeclared.append(f"{path.name}:{node.lineno} {step.value}")
        self.assertEqual(undeclared, [])

    def test_the_names_of_skipped_steps_are_declared(self):
        for step in ("Discord ping", "MOTD update", "SRP creation", "ESI tracking"):
            self.assertIn(step, texts.STEP_NAMES)
            self.assertTrue(texts.registered(texts.STEP_SKIPPED(mode=texts.MODE_PAP_ONLY(), step=step)))

    def test_constants_of_the_modules_are_declared_messages(self):
        from fleetops.services import (
            approvals,
            counting,
            fleet_invites,
            fleet_manage,
            fleet_refresh,
            fleet_templates,
            incentives,
            motd,
            pap_links,
            roles,
            roster_import,
            scheduling,
        )
        from fleetops.srp import claims, killmails, review

        constants = {
            fleet_access: ("NOT_ACTIVE", "FLEET_GONE", "NO_WRITE_TOKEN", "WRITE_BUSY"),
            fleet_controls: ("NOT_ACTIVE", "NO_ESI_FLEET", "PROTECTED", "NOT_IN_FLEET", "NO_WRITE_TOKEN"),
            fleet_invites: ("NO_CHARACTERS", "TOO_MANY", "ONE_COMMANDER", "NOBODY"),
            fleet_jobs: ("STOPPED_UNEXPECTEDLY", "NOT_STARTED", "WORKER_DOWN", "FLEET_MISSING", "NOT_ALLOWED"),
            fleet_manage: ("NO_MEMBERS", "TOO_MANY_MOVES", "ONE_COMMANDER", "VIEW_REFRESHED"),
            fleet_refresh: ("REFRESHED_RECENTLY", "REFRESHED", "NOT_OPEN"),
            fleet_rules: ("NAME_LENGTH_MESSAGE",),
            fleet_templates: ("TEMPLATE_GONE",),
            incentives: ("INCENTIVES_DISABLED_MESSAGE", "WAIVER_LOCKED_MESSAGE"),
            motd: ("NOT_ACTIVE",),
            pap_links: ("NOT_OWNED", "NO_TOKEN", "TOKEN_CHECK_FAILED", "ALREADY_REGISTERED", "LINK_CLOSED",
                        "LEGACY_FLEET", "NOT_ONLINE", "TOO_MANY"),
            roles: ("NOT_IN_FLEET",),
            roster_import: ("PREVIEW_EXPIRED", "NOT_IN_AUTH_REASON", "NOT_FOUND_REASON", "ESI_ERROR_REASON",
                            "HAS_PAP_REASON"),
            scheduling: ("NOT_SCHEDULED_MESSAGE", "CHANNEL_CHANGE_NEEDS_POST", "ONLY_SCHEDULING_FC_MESSAGE"),
            approvals: ("REJECTION_NOTE_REQUIRED", "ALREADY_APPROVED", "NOT_PENDING"),
            counting: ("STATS_NOTE_REQUIRED",),
            claims: ("EXPIRED_TEXT", "THROTTLED_TEXT", "OWN_DUPLICATE_TEXT", "OTHER_DUPLICATE_TEXT"),
            review: ("NOT_A_MANAGER", "OWN_CLAIM", "OWN_CLAIM_SKIPPED"),
            killmails: ("UNRECOGNISED", "NO_HASH", "USE_INGAME_LINK", "ZKILLBOARD_OFF"),
        }
        for module, names in constants.items():
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(texts.registered(getattr(module, name)), getattr(module, name))

    def test_messages_with_a_count_keep_both_wordings(self):
        self.assertEqual(texts.KICK_JOB_CAPSULES(count=1), "Kick 1 capsule pilot")
        self.assertEqual(texts.KICK_JOB_CAPSULES(count=12), "Kick 12 capsule pilots")
        self.assertEqual(
            texts.RULE_SQUAD_NOT_EMPTY(count=1), "Only empty squads can be deleted. Move its member first."
        )
        self.assertEqual(
            texts.RULE_SQUAD_NOT_EMPTY(count=3), "Only empty squads can be deleted. Move its 3 members first."
        )
        self.assertEqual(
            texts.RULE_FLEET_ROOM(count=1, max=256),
            "Only 1 more character fits into the fleet (EVE allows 256 members).",
        )
        for plural in (texts.INVITE_JOB, texts.MOVE_JOB, texts.MOVING_IN_BACKGROUND, texts.INVITING_IN_BACKGROUND):
            for count in (1, 2):
                self.assertTrue(texts.registered(plural(count=count)), plural)


def audit_texts(operation):
    """The texts the audit entries of a fleet keep in their values."""
    found = []
    for entry in AuditLog.objects.filter(object_type="FleetOperation", object_id=str(operation.pk)):
        found.append((f"audit {entry.action} reason", entry.reason))
        values = entry.new_value if isinstance(entry.new_value, dict) else {}
        for key in ("summary", "error", "stopped", "message"):
            if isinstance(values.get(key), str):
                found.append((f"audit {entry.action} {key}", values[key]))
        for key in ("failed", "failures"):
            failures = values.get(key)
            for failure in failures if isinstance(failures, list) else ():
                if isinstance(failure, dict) and failure.get("error"):
                    found.append((f"audit {entry.action} {key}", failure["error"]))
    return found


def stored_texts(operation):
    """Every text the code stored for ``operation``, with where it was stored."""
    found = []
    for row in OperationAction.objects.filter(operation=operation):
        found.append((f"step {row.action}", row.error_message))
    for row in OperationActionAttempt.objects.filter(action__operation=operation):
        found.append((f"attempt {row.action.action}", row.message))
    operation.refresh_from_db()
    for name in ("last_error", "srp_error"):
        found.append((name, getattr(operation, name)))
    job = fleet_jobs.current_job(operation.pk)
    if job:
        found.append(("job label", job["label"]))
        found.extend(("job message", message["text"]) for message in job["messages"])
    found.extend(audit_texts(operation))
    return [(where, text) for where, text in found if text]


class StoredTextAssertions:
    def assertStored(self, operation, ignore=()):
        """Every stored text is a declared message (or free text of Discord listed in ``ignore``)."""
        seen = stored_texts(operation)
        self.assertTrue(seen, "the flow stored no text at all")
        for where, text in seen:
            if text in ignore:
                continue
            with self.subTest(where=where, text=text[:80]):
                self.assertTrue(texts.registered(text), f"{where}: {text!r} is not a declared message")
        return seen

    def assertDeclared(self, *messages):
        for message in messages:
            with self.subTest(message=str(message)[:80]):
                self.assertTrue(texts.registered(str(message)), f"{message!r} is not a declared message")


class StartFlowTests(StoredTextAssertions, StartFleetTestBase):
    def setUp(self):
        super().setUp()
        self.create_srp_link.return_value = SRPLinkResult(
            "test_srp", reference="77", url="/srp/77/", created=True, message=texts.AASRP_CREATED()
        )

    def test_a_start_without_problems(self):
        operation = self.start()

        self.assertStored(operation)

    def test_message_rendering_fails(self):
        with mock.patch("fleetops.services.operations.render_messages", side_effect=RuntimeError("secret-token")):
            operation = self.start()

        stored = dict(stored_texts(operation))
        self.assertEqual(
            stored["step message_render"], texts.STEP_FAILED(step="Message rendering", error_type="RuntimeError")
        )
        self.assertEqual(stored["step discord_ping"], texts.NOT_RENDERED())
        self.assertNotIn("secret-token", " ".join(stored.values()))
        self.assertStored(operation)

    def test_a_template_that_cannot_be_rendered_falls_back(self):
        broken = MessageTemplate.objects.create(
            name="Broken", template_type=MessageTemplate.TemplateType.PING, content="{% nope %}"
        )
        broken_motd = MessageTemplate.objects.create(
            name="Broken MOTD", template_type=MessageTemplate.TemplateType.MOTD, content="{% nope %}"
        )

        operation = self.start(ping_template=broken, motd_template=broken_motd)

        message = OperationAction.objects.get(operation=operation, action="message_render").error_message
        self.assertIn('Ping template "Broken" could not be rendered (TemplateSyntaxError)', message)
        # Two messages in a row are two declared messages.
        self.assertIsNotNone(texts.split_series(message))
        self.assertStored(operation)

    def test_discord_failures(self):
        operation = self.start()
        cases = [
            lambda: setattr(self.discord_post, "return_value", mock.Mock(status_code=500, text="")),
            lambda: setattr(self.discord_post, "side_effect", requests.ConnectionError("x")),
            lambda: setattr(self.discord_post, "side_effect", requests.exceptions.MissingSchema("x")),
        ]
        for prepare in cases:
            self.discord_post.side_effect = None
            prepare()
            retry_ping(operation, actor=operation.fc_user)
            self.assertStored(operation)
        self.discord_post.side_effect = None
        # A ping over Discord's limit is not sent.
        FleetOperation.objects.filter(pk=operation.pk).update(ping_text="x" * 2100)
        operation.refresh_from_db()
        action = retry_ping(operation, actor=operation.fc_user)
        self.assertEqual(action.error_message, texts.PING_TOO_LONG(length=2100, limit=2000))
        # The webhook is switched off, then gone.
        self.webhook.is_active = False
        self.webhook.save()
        operation.refresh_from_db()
        self.assertEqual(retry_ping(operation, actor=operation.fc_user).error_message, texts.PING_CHANNEL_DISABLED(name="Pings"))
        FleetOperation.objects.filter(pk=operation.pk).update(ping_channel=None)
        operation.refresh_from_db()
        self.assertEqual(retry_ping(operation, actor=operation.fc_user).error_message, texts.NO_PING_CHANNEL())
        self.assertStored(operation)

    def test_a_ping_that_may_not_notify_the_selection(self):
        role = PingRole.objects.create(name="Gone", role_id=1234567890, is_active=False)
        other = PingRole.objects.create(name="Also gone", role_id=1234567891, is_active=False)
        selection = mentions.selection_from_operation(
            SimpleNamespace(
                ping_mention_mode="roles",
                ping_roles=[
                    {"id": str(role.role_id), "name": role.name},
                    {"id": str(other.role_id), "name": "Also gone"},
                ],
            )
        )

        operation = self.start(mention_selection=selection)

        message = OperationAction.objects.get(operation=operation, action="discord_ping").error_message
        self.assertEqual(
            message, "Role Gone is no longer available. Role Also gone is no longer available."
        )
        self.assertIsNotNone(texts.split_series(message))
        self.assertStored(operation)

    def test_motd_failures(self):
        operation = self.start()
        for outcome in (
            http_error(403),
            http_error(503),
            TimeoutError("slow"),
            ConnectionError("down"),
            ValueError("secret-token"),
        ):
            self.esi.put_outcome = outcome
            retry_motd(operation)
            self.assertStored(operation)
        self.assertNotIn("secret-token", " ".join(text for _where, text in stored_texts(operation)))
        self.assertEqual(
            OperationAction.objects.get(operation=operation, action="motd_update").error_message,
            texts.MOTD_FAILED(detail=texts.ESI_UNEXPECTED(error_type="ValueError")),
        )

    def test_srp_failures(self):
        operation = self.start()
        self.create_srp_link.side_effect = RuntimeError("secret-token")
        retry_srp(operation)
        self.assertStored(operation)
        self.create_srp_link.side_effect = None
        FleetOperation.objects.filter(pk=operation.pk).update(srp_reference="", srp_url="")
        operation.refresh_from_db()
        for message in (texts.SRP_NO_PROVIDER, texts.FLEETOPS_SRP_DISABLED, texts.AASRP_MISSING):
            self.create_srp_link.return_value = SRPLinkResult("none", message=message())
            retry_srp(operation)
            self.assertStored(operation)
        FleetOperation.objects.filter(pk=operation.pk).update(srp_reference="77")
        operation.refresh_from_db()
        self.assertEqual(retry_srp(operation).error_message, texts.SRP_ALREADY_LINKED())

    def test_tracking_cannot_read_the_fleet_at_the_start(self):
        self.esi.members[FLEET_ID] = http_error(503)

        operation = self.start()

        seen = dict(stored_texts(operation))
        self.assertEqual(
            seen["step tracking_start"],
            texts.READ_MEMBERS_FAILED(detail=texts.ESI_HTTP(status=503, reason="Service unavailable")),
        )
        self.assertStored(operation)

    def test_a_fleet_that_only_collects_pap(self):
        operation = self.start(operation_mode="attendance_only", ping_channel=None)

        seen = dict(stored_texts(operation))
        self.assertEqual(
            seen["step discord_ping"], texts.STEP_SKIPPED(mode=texts.MODE_PAP_ONLY(), step="Discord ping")
        )
        self.assertStored(operation)

    def test_a_manual_fleet(self):
        operation = create_manual_fleet(
            user=self.fc,
            cleaned_data={
                "started_at": timezone.now() - timedelta(hours=1),
                "ended_at": timezone.now(),
                "fleet_type": self.fleet_type,
                "formup": "Jita",
            },
        )

        self.assertStored(operation)

    def test_why_a_fleet_cannot_be_started(self):
        character = self.fc_char
        cases = {
            "not in a fleet": lambda: self.esi.character_fleets.pop(character.character_id),
            "denied": lambda: self.esi.character_fleets.update({character.character_id: http_error(403)}),
            "down": lambda: self.esi.character_fleets.update({character.character_id: http_error(502)}),
            "slow": lambda: self.esi.character_fleets.update({character.character_id: TimeoutError()}),
            "offline": lambda: self.esi.character_fleets.update({character.character_id: ConnectionError()}),
            "odd": lambda: self.esi.character_fleets.update({character.character_id: KeyError("x")}),
        }
        for name, prepare in cases.items():
            self.esi.put_in_fleet(character)
            prepare()
            with self.subTest(case=name), self.assertRaises(esi_provider.FleetESIError) as caught:
                esi_provider.detect_character_fleet(self.fc, character.character_id)
            self.assertDeclared(caught.exception)
        # Someone else is the boss.
        self.esi.put_in_fleet(character, role="squad_member")
        with self.assertRaises(esi_provider.FleetESIError) as caught:
            self.start()
        self.assertDeclared(caught.exception)
        self.assertEqual(caught.exception.code, "NOT_FLEET_BOSS")
        # A character that is not the user's.
        with self.assertRaises(PermissionError) as caught:
            self.start(fc_character_id="1")
        self.assertDeclared(caught.exception)

    def test_a_missing_token(self):
        self.token.delete()

        with self.assertRaises(esi_provider.FleetESIError) as caught:
            esi_provider.get_token(self.fc, self.fc_char.character_id, write=True)

        self.assertEqual(str(caught.exception), texts.ESI_NO_TOKEN())

    def test_how_a_fleet_ends(self):
        operation = self.start()

        end_fleet(operation, actor=self.fc)

        seen = dict(stored_texts(operation))
        self.assertEqual(
            seen["step fleet_end"], texts.FLEET_END_PAP(reason=texts.END_MANUAL(), granted=2, multiplier=1)
        )
        self.assertStored(operation)

    def test_how_a_fleet_ends_by_itself(self):
        for reason, detail, label in (
            (EndReason.FLEET_GONE, "", texts.END_FLEET_GONE()),
            (EndReason.BOSS_HANDOVER, "New Boss", texts.END_BOSS_HANDOVER(name="New Boss")),
            (EndReason.BOSS_HANDOVER, "", texts.END_BOSS_HANDOVER_UNKNOWN()),
        ):
            with self.subTest(reason=reason, detail=detail):
                self.esi.put_in_fleet(self.fc_char, fleet_id=FLEET_ID + len(detail))
                operation = self.start()
                end_fleet(operation, automatic=True, reason=reason, detail=detail)
                operation.refresh_from_db()
                self.assertEqual(operation.last_error, label)
                self.assertStored(operation)
                operation.status = Status.CLOSED
                FleetOperation.objects.filter(pk=operation.pk).update(esi_fleet_id=None)

    def test_a_requested_end(self):
        operation = self.start()

        request_fleet_end(operation, actor=self.fc)

        self.assertStored(operation)

    def test_the_motd_is_regenerated(self):
        operation = self.start()
        self.esi.put_outcome = http_error(503)

        action = regenerate_and_push_motd(operation, actor=self.fc)

        self.assertDeclared(action.error_message)
        self.assertStored(operation)

    def test_a_scheduled_fleet_is_cancelled(self):
        operation = schedule_fleet(
            user=self.fc, cleaned_data=self.cleaned_data(scheduled_at=timezone.now() + timedelta(hours=3))
        )
        self.discord_post.side_effect = requests.ConnectionError("x")

        cancel_scheduled_fleet(operation, actor=self.fc, notify=True)

        seen = dict(stored_texts(operation))
        self.assertEqual(seen["step fleet_cancel"], texts.FLEET_CANCELLED())
        self.assertEqual(seen["step cancel_notice"], texts.PING_REQUEST_FAILED(error_type="ConnectionError"))
        self.assertStored(operation)

    def test_why_tracking_cannot_read_the_fleet(self):
        for reason, detail in (
            (EndReason.FLEET_GONE, ""),
            (EndReason.BOSS_HANDOVER, "Bob"),
            (EndReason.BOSS_HANDOVER, ""),
        ):
            from fleetops.services.tracking import unreadable_fleet_message

            self.assertDeclared(unreadable_fleet_message(reason, detail))
        self.assertEqual(UnreadableFleet(EndReason.FLEET_GONE).reason, EndReason.FLEET_GONE)


class TextsDoNotDependOnTheLanguage(FakeCatalogMixin, SimFleetTestCase):
    """What is stored is English, also when the request is in another language."""

    def setUp(self):
        super().setUp()
        CATALOG["zh-hans"].update(
            {
                "Wing %(id)s": "联队 %(id)s",
                "Squad %(id)s": "中队 %(id)s",
                "Character %(id)s": "角色 %(id)s",
                "Fleet Command": "舰队指挥",
                "Wing Command": "联队指挥",
            }
        )
        self.addCleanup(
            lambda: [
                CATALOG["zh-hans"].pop(key, None)
                for key in ("Wing %(id)s", "Squad %(id)s", "Character %(id)s", "Fleet Command", "Wing Command")
            ]
        )
        for target in ("fleetops.services.fleet_rules.gettext",):
            patcher = mock.patch(
                target, lambda message: CATALOG.get(translation.get_language(), {}).get(message, message)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_in(self, language, make):
        with translation.override(language):
            return make()

    def test_labels_shown_to_a_viewer_follow_the_language(self):
        self.assertEqual(self.run_in("en", lambda: fleet_rules.wing_label(999)), "Wing 999")
        self.assertEqual(self.run_in("zh-hans", lambda: fleet_rules.wing_label(999)), "联队 999")
        self.assertEqual(self.run_in("en", lambda: str(fleet_rules.FLEET_COMMAND)), "Fleet Command")
        self.assertEqual(self.run_in("zh-hans", lambda: str(fleet_rules.FLEET_COMMAND)), "舰队指挥")

    def test_labels_inside_stored_messages_are_English(self):
        self.assertEqual(self.run_in("zh-hans", lambda: fleet_rules.stored_wing_label(999)), "Wing 999")
        self.assertEqual(self.run_in("zh-hans", lambda: fleet_rules.stored_squad_label(5)), "Squad 5")
        self.assertEqual(
            self.run_in("zh-hans", lambda: fleet_rules.stored_position_labels("fleet_commander", None, None)),
            ("Fleet Command", ""),
        )

    def test_a_refused_change_is_stored_in_English(self):
        def refused():
            with self.assertRaises(fleet_access.FleetManageError) as caught:
                rename_wing(self.operation, 4242, "Other", actor=self.fc)
            return caught.exception.message

        self.assertEqual(self.run_in("zh-hans", refused), self.run_in("en", refused))
        self.assertDeclared(self.run_in("zh-hans", refused))

    def assertDeclared(self, message):
        self.assertTrue(texts.registered(message), message)

    def test_a_commander_position_that_is_taken(self):
        taker = self.pilot("Taker")[1]
        self.join(taker, role="wing_commander", wing_id=self.fleet.wing_id("Main"), squad_id=None)
        other = self.pilot("Other")[1]
        self.join(other)

        with translation.override("zh-hans"), self.assertRaises(fleet_access.FleetManageError) as caught:
            move_members(
                self.operation,
                [MoveRequest(other.character_id, "wing_commander", self.fleet.wing_id("Main"))],
                actor=self.fc,
            )

        self.assertEqual(caught.exception.message, "Main already has a wing commander (Taker). Move them first.")
        self.assertDeclared(caught.exception.message)


class JobFlowTests(StoredTextAssertions, SimFleetTestCase):
    """Texts of the fleet changes, jobs and kicks."""

    def setUp(self):
        super().setUp()
        self.pilots = [self.pilot(f"Job Pilot {number}")[1] for number in range(1, 4)]
        self.alpha = self.fleet.squad_id("Alpha")
        self.main = self.fleet.wing_id("Main")

    def ids(self):
        return [pilot.character_id for pilot in self.pilots]

    def test_single_changes_and_their_failures(self):
        results = [
            add_wing(self.operation, actor=self.fc, name="Logi"),
            add_wing(self.operation, actor=self.fc),
            add_squad(self.operation, self.main, actor=self.fc, name="Echo"),
            rename_wing(self.operation, self.main, "Primary", actor=self.fc),
        ]
        self.sim.fail("PutFleetsFleetIdWingsWingId", 500)
        results.append(rename_wing(self.operation, self.main, "Again", actor=self.fc))
        self.sim.clear_failures()
        self.sim.fail("PutFleetsFleetIdWingsWingId", 500)
        results.append(add_wing(self.operation, actor=self.fc, name="Named"))
        self.sim.clear_failures()
        results.append(delete_squad(self.operation, self.alpha, actor=self.fc))
        for result in results:
            self.assertDeclared(result.message)
        self.assertStored(self.operation)

    def test_invites(self):
        self.sim.fail(
            "PostFleetsFleetIdMembers", status=400, error="Character has a CSPA charge", character_id=self.ids()[1]
        )
        stranger = f.create_character("Stranger")
        results = [
            invite_characters(self.operation, [self.ids()[0]], actor=self.fc, squad_id=self.alpha),
            invite_characters(self.operation, self.ids(), actor=self.fc, squad_id=self.alpha),
            invite_characters(self.operation, [stranger.character_id], actor=self.fc),
            invite_characters(self.operation, [stranger.character_id, self.ids()[2]], actor=self.fc),
        ]
        for result in results:
            self.assertDeclared(result.message)
        self.assertStored(self.operation)

    def test_moves(self):
        for pilot in self.pilots:
            self.join(pilot)
        bravo = self.fleet.squad_id("Bravo")
        moves = [MoveRequest(pilot.character_id, "squad_member", self.main, bravo) for pilot in self.pilots]
        self.sim.fail("PutFleetsFleetIdMembersMemberId", status=409, character_id=self.ids()[1])
        results = [
            move_members(self.operation, moves[:1], actor=self.fc),
            move_members(self.operation, moves, actor=self.fc),
            move_members(self.operation, moves, actor=self.fc),
        ]
        for result in results:
            self.assertDeclared(result.message)
        self.assertStored(self.operation)

    def test_capsule_kicks(self):
        for pilot in self.pilots:
            self.join(pilot, ship_type_id=670)
        self.sim.fail("DeleteFleetsFleetIdMembersMemberId", status=500, character_id=self.ids()[0])

        single = fleet_controls.kick_member(self.operation, self.ids()[2], actor=self.fc)
        result = fleet_controls.kick_members(self.operation, None, actor=self.fc, purpose=fleet_controls.CAPSULES)

        self.assertDeclared(single.message)
        for failure in result["failures"]:
            self.assertDeclared(failure["error"])
        self.assertStored(self.operation)

    def test_kicks_that_run_as_a_job(self):
        pilots = [self.pilot(f"Pod {number}")[1] for number in range(12)]
        for pilot in pilots:
            self.join(pilot, ship_type_id=670)
        self.sim.fail("DeleteFleetsFleetIdMembersMemberId", status=500, character_id=pilots[3].character_id)

        fleet_controls.kick_members(self.operation, None, actor=self.fc, purpose=fleet_controls.CAPSULES)

        self.assertStored(self.operation)
        self.assertEqual(fleet_jobs.current_job(self.operation.pk)["label"], texts.KICK_JOB_CAPSULES(count=12))

    def test_refused_kicks(self):
        for call in (
            lambda: fleet_controls.kick_member(self.operation, self.fc_char.character_id, actor=self.fc),
            lambda: fleet_controls.kick_member(self.operation, 1, actor=self.fc),
        ):
            with self.assertRaises(fleet_controls.KickRefused) as caught:
                call()
            self.assertDeclared(str(caught.exception))

    def test_a_handover_stops_a_job(self):
        self.join(self.pilots[0])
        self.fleet.set_boss(self.pilots[0].character_id)

        invite_characters(self.operation, self.ids()[1:], actor=self.fc)

        self.assertStored(self.operation)


class TemplateFlowTests(StoredTextAssertions, TemplateJobTestCase):
    def test_a_template_with_failures(self):
        self.sim.fail("PutFleetsFleetIdWingsWingId", 500)
        self.sim.fail("PostFleetsFleetIdWingsWingIdSquads", 404)

        self.apply(delete_extra=True)

        self.assertStored(self.operation)

    def test_a_template_that_changes_nothing(self):
        self.apply()
        self.apply()

        self.assertStored(self.operation)
        self.assertIn(texts.TEMPLATE_NOTHING(name="Standard"), [text for _where, text in stored_texts(self.operation)])

    def test_extra_units(self):
        self.fleet.add_wing("Extra", squads=2)
        self.sync()

        self.apply(delete_extra=True)

        self.assertStored(self.operation)

    def test_a_template_that_cannot_be_applied(self):
        template = FleetStructureTemplate.objects.create(name="Broken", layout="Only one line")

        with self.assertRaises(fleet_access.FleetManageError) as caught:
            self.apply(template)

        self.assertDeclared(caught.exception.message)

    def assertDeclared(self, message):
        self.assertTrue(texts.registered(message), message)


class AutomaticDecisionTests(ClaimBase):
    """The reasons of automatic SRP decisions and the texts of refused requests."""

    def used(self, amount, user=None):
        return f.srp_claim(user or self.user, amount=amount, status=SrpClaim.Status.APPROVED)

    def test_a_claim_rejected_for_a_used_up_limit(self):
        for kind, cap, extra in (
            ("personal", {"personal_monthly_cap": 100_000_000}, {}),
            ("corporation", {"corporation_monthly_cap": 100_000_000}, {"user": self.other}),
        ):
            with self.subTest(kind=kind):
                self.settings(over_limit_action="reject", **cap)
                self.used(100_000_000, **extra)
                with self.captureOnCommitCallbacks(execute=True), translation.override("zh-hans"):
                    claim = self.submit(self.killmail(fitted="125000000"))
                self.assertTrue(claim.auto_rejected)
                self.assertTrue(texts.registered(claim.decision_reason), claim.decision_reason)
                self.assertTrue(claim.decision_reason.startswith("Rejected automatically: the monthly"))
                event = claim.events.get(action="auto_reject")
                self.assertEqual(event.reason, claim.decision_reason)
                SrpClaim.objects.all().delete()

    def test_a_value_that_arrives_later_is_a_declared_reason(self):
        claim = self.submit(self.killmail())

        claims.recalculate_under_lock(
            claim, actor=None, reason=texts.SRP_REASON_ZKILL_VALUE(), action=SrpClaimEvent.Action.VALUE_UPDATE
        )

        event = claim.events.get(action=SrpClaimEvent.Action.VALUE_UPDATE)
        self.assertTrue(texts.registered(event.reason), event.reason)

    def test_refused_requests_say_why_in_declared_messages(self):
        claim = self.submit(self.killmail())
        refusals = []
        for call in (
            lambda: claims.withdraw_claim(claim, actor=self.other),
            lambda: claims.resubmit_claim(claim, actor=self.user),
            lambda: claims.withdraw_claim(claim, actor=self.user, reason="x" * 2000),
            lambda: review.approve_claim(claim, actor=self.user, amount=1),
            lambda: review.reject_claim(claim, actor=self.manager, reason=""),
            lambda: review.reject_claim(claim, actor=self.manager, reason="x" * 2000),
            lambda: review.approve_claim(claim, actor=self.manager, amount=0),
            lambda: review.add_note(claim, actor=self.manager, text=""),
            lambda: review.approve_clean_claims([claim], actor=self.user),
            lambda: killmails.parse_killmail_reference("nothing useful"),
            lambda: killmails.parse_killmail_reference("x" * 3000),
        ):
            with self.assertRaises(SrpError) as caught:
                call()
            refusals.append(caught.exception.message)
        # Several claims at once: the ones skipped come with the reason.
        refusals += [reason for _claim, reason in review.mark_paid([claim], actor=self.manager).skipped]
        twice = review.approve_clean_claims([claim, claim], actor=self.manager)
        refusals += [reason for _claim, reason in twice.skipped]
        for message in refusals:
            self.assertTrue(texts.registered(message), message)

    def test_the_reason_of_a_rejection_for_a_used_up_limit(self):
        for kind in ("personal", "corporation", ""):
            text = texts.SRP_REJECTED_LIMIT_USED_UP(limit=claims.limit_name(kind), cap="1,000 ISK")
            with self.subTest(kind=kind):
                self.assertTrue(texts.registered(text), text)

    def test_a_status_in_words_is_English(self):
        with translation.override("zh-hans"):
            self.assertEqual(claims.status_label("approved"), "approved")
            self.assertEqual(claims.limit_name("personal"), "monthly personal limit")


class FlowHelpersTests(SimpleTestCase):
    def test_the_boss_messages(self):
        fc = SimpleNamespace(fc_character_name="Boss Main")
        self.assertEqual(
            fleet_access.not_boss_message(fc, "Bob"),
            "Boss Main is not the fleet boss any more (current boss: Bob). Fleet changes are disabled.",
        )
        self.assertEqual(
            fleet_access.not_boss_message(fc),
            "Boss Main is not the fleet boss any more (current boss: another character). Fleet changes are disabled.",
        )
        self.assertTrue(texts.registered(fleet_access.not_boss_message(fc, "Bob")))
        self.assertTrue(texts.registered(fleet_access.not_boss_message(fc)))


class GroupMessageTests(SimpleTestCase):
    """Why a ping channel or a group fleet is refused: declared, English as stored, recognised again."""

    CASES = (
        (texts.CHANNEL_NOT_ALLOWED, {"name": "STR"}, "You may not post to the ping channel STR."),
        (texts.GROUP_NOT_LED, {"group": "Sig A"}, "You no longer lead the group Sig A in FleetOps."),
        (
            texts.GROUP_CHANNEL_NOT_ASSIGNED,
            {"name": "STR", "group": "Sig A"},
            "STR is not a ping channel of the group Sig A.",
        ),
        (texts.GROUP_NO_FLEETS, {"group": "Sig A"}, "The leaders of Sig A may only send pings."),
        (
            texts.GROUP_FLEET_TYPE_NOT_ALLOWED,
            {"type": "StratOp", "group": "Sig A"},
            "Fleet type StratOp is not allowed for the group Sig A.",
        ),
        (texts.GROUP_REQUIRED, {}, "Choose the group this fleet belongs to."),
        (
            texts.GROUP_NO_PRE_PING,
            {},
            "Group fleets cannot be pre pinged. Send the fleet time with Send Ping and start the fleet when it is due.",
        ),
        (texts.GROUP_NO_SRP, {"group": "Sig A"}, "Group fleets of Sig A get no SRP link."),
    )

    def test_each_message_reads_as_declared_and_is_found_again(self):
        for message, params, expected in self.CASES:
            text = message(**params)
            with self.subTest(message=expected):
                self.assertEqual(text, expected)
                self.assertTrue(texts.registered(text))
                self.assertIs(texts.find(text)[0], message)
                self.assertEqual(texts.find(text)[1], params)

    def test_they_stay_English_for_an_English_viewer(self):
        for message, params, expected in self.CASES:
            with self.subTest(message=expected), translation.override("en"):
                self.assertEqual(texts.translate_stored(message(**params)), expected)

    def test_a_name_with_stop_and_colon_keeps_the_message_whole(self):
        text = texts.GROUP_NOT_LED(group="Sig A. B: C")

        self.assertEqual(texts.find(text)[1], {"group": "Sig A. B: C"})
