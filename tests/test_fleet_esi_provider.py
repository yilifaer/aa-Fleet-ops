"""Fleet structure, move and invite calls of the ESI provider against the in-memory fleet."""

from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from esi.exceptions import ESIBucketLimitException, HTTPClientError

from fleetops.constants import FLEET_READ_SCOPE
from fleetops.providers import esi as esi_provider
from fleetops.providers.esi import (
    FleetESIError,
    as_dict,
    create_fleet_squad,
    create_fleet_wing,
    delete_fleet_squad,
    delete_fleet_wing,
    get_fleet_wings,
    invite_fleet_member,
    move_fleet_member,
    rename_fleet_squad,
    rename_fleet_wing,
)
from fleetops.services.fleet_rules import FleetRuleError

from . import factories as f
from .fleet_sim import FleetSim
from .test_esi_errors import SECRET_HEADER, esi_error, timeout_from_the_http_client

WRITE_OPERATIONS = (
    "PostFleetsFleetIdWings",
    "PutFleetsFleetIdWingsWingId",
    "DeleteFleetsFleetIdWingsWingId",
    "PostFleetsFleetIdWingsWingIdSquads",
    "PutFleetsFleetIdSquadsSquadId",
    "DeleteFleetsFleetIdSquadsSquadId",
    "PutFleetsFleetIdMembersMemberId",
    "PostFleetsFleetIdMembers",
)


class SimFleetTestCase(TestCase):
    response_style = "dict"

    def setUp(self):
        super().setUp()
        self.fc = f.create_user(perms=f.FC_PERMS, main_name="Provider FC")
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)
        self.sim = FleetSim(response_style=self.response_style)
        self.sim.install(self)
        self.fleet = self.sim.create_fleet(self.fc_char.character_id, wings=(("Main", ("Alpha", "Bravo")),))
        self.wing_id = self.fleet.wing_id("Main")
        self.alpha = self.fleet.squad_id("Alpha")
        self.bravo = self.fleet.squad_id("Bravo")
        self.pilot_id = 2_119_400_001
        self.fleet.add_member(self.pilot_id, squad_id=self.alpha, wing_id=self.wing_id)

    def args(self):
        return self.fc, self.fc_char.character_id, self.fleet.fleet_id

    def last_request(self):
        return self.sim.requests[-1]


class DictResponseTests(SimFleetTestCase):
    def test_create_wing_sends_no_body_and_returns_the_id(self):
        wing_id = create_fleet_wing(*self.args())

        request = self.last_request()
        self.assertEqual(
            (request.name, request.params, request.body, request.character_id),
            ("PostFleetsFleetIdWings", {"fleet_id": self.fleet.fleet_id}, None, self.fc_char.character_id),
        )
        self.assertEqual(wing_id, self.fleet.wings[-1]["id"])

    def test_rename_and_delete_a_wing(self):
        wing_id = self.fleet.add_wing("Spare", squads=0)

        rename_fleet_wing(*self.args(), wing_id, "Logi")
        self.assertEqual(self.last_request().body, {"name": "Logi"})
        self.assertEqual(self.last_request().params, {"fleet_id": self.fleet.fleet_id, "wing_id": wing_id})
        self.assertEqual(self.fleet.wing(wing_id)["name"], "Logi")

        delete_fleet_wing(*self.args(), wing_id)
        self.assertEqual(self.last_request().name, "DeleteFleetsFleetIdWingsWingId")
        self.assertIsNone(self.last_request().body)
        self.assertNotIn(wing_id, [wing["id"] for wing in self.fleet.wings])

    def test_create_rename_and_delete_a_squad(self):
        squad_id = create_fleet_squad(*self.args(), self.wing_id)
        request = self.last_request()
        self.assertEqual(
            (request.name, request.params, request.body),
            ("PostFleetsFleetIdWingsWingIdSquads", {"fleet_id": self.fleet.fleet_id, "wing_id": self.wing_id}, None),
        )
        self.assertEqual(self.fleet.squad(squad_id)[0]["id"], self.wing_id)

        rename_fleet_squad(*self.args(), squad_id, "Charlie")
        self.assertEqual(self.last_request().params, {"fleet_id": self.fleet.fleet_id, "squad_id": squad_id})
        self.assertEqual(self.last_request().body, {"name": "Charlie"})

        delete_fleet_squad(*self.args(), squad_id)
        self.assertEqual(self.last_request().name, "DeleteFleetsFleetIdSquadsSquadId")
        self.assertNotIn(squad_id, [squad["id"] for squad in self.fleet.wings[0]["squads"]])

    def test_moves_send_the_body_of_each_position(self):
        pilot = self.pilot_id
        in_bravo = {"wing_id": self.wing_id, "squad_id": self.bravo}
        cases = [
            (("squad_member", self.wing_id, self.bravo), {"role": "squad_member", **in_bravo}),
            (("squad_commander", self.wing_id, self.bravo), {"role": "squad_commander", **in_bravo}),
            (("wing_commander", self.wing_id, None), {"role": "wing_commander", "wing_id": self.wing_id}),
        ]
        for position, body in cases:
            with self.subTest(position=position[0]):
                move_fleet_member(*self.args(), pilot, *position)
                request = self.last_request()
                self.assertEqual(request.name, "PutFleetsFleetIdMembersMemberId")
                self.assertEqual(request.params, {"fleet_id": self.fleet.fleet_id, "member_id": pilot})
                self.assertEqual(request.body, body)
                self.assertEqual(self.fleet.position(pilot)[0], position[0])

        # The boss leaves the fleet commander position, then the pilot takes it: no wing or squad.
        move_fleet_member(*self.args(), self.fc_char.character_id, "squad_member", self.wing_id, self.alpha)
        move_fleet_member(*self.args(), pilot, "fleet_commander", -1, -1)
        self.assertEqual(self.last_request().body, {"role": "fleet_commander"})
        self.assertEqual(self.fleet.position(pilot), ("fleet_commander", -1, -1))

    def test_invalid_positions_are_refused_before_anything_is_sent(self):
        sent = len(self.sim.requests)
        for args in (("squad_member", self.wing_id, None), ("wing_commander", None, None), ("captain", None, None)):
            with self.subTest(args=args), self.assertRaises(FleetRuleError):
                move_fleet_member(*self.args(), self.pilot_id, *args)
        self.assertEqual(len(self.sim.requests), sent)

    def test_invites(self):
        invite_fleet_member(*self.args(), 2_119_400_002)
        self.assertEqual(self.last_request().name, "PostFleetsFleetIdMembers")
        self.assertEqual(self.last_request().params, {"fleet_id": self.fleet.fleet_id})
        self.assertEqual(self.last_request().body, {"character_id": 2_119_400_002, "role": "squad_member"})

        invite_fleet_member(*self.args(), 2_119_400_003, "squad_member", self.wing_id, self.bravo)
        self.assertEqual(
            self.last_request().body,
            {"character_id": 2_119_400_003, "role": "squad_member", "wing_id": self.wing_id, "squad_id": self.bravo},
        )
        self.assertEqual(set(self.fleet.invitations), {2_119_400_002, 2_119_400_003})
        self.sim.accept_invite(2_119_400_003)
        self.assertEqual(self.fleet.position(2_119_400_003), ("squad_member", self.wing_id, self.bravo))

    def test_wings_are_read_and_normalised(self):
        wings = get_fleet_wings(*self.args())

        self.assertEqual(
            wings,
            [
                {
                    "id": self.wing_id,
                    "name": "Main",
                    "squads": [{"id": self.alpha, "name": "Alpha"}, {"id": self.bravo, "name": "Bravo"}],
                }
            ],
        )

    def test_fresh_wing_reads_skip_the_response_cache(self):
        get_fleet_wings(*self.args())
        self.fleet.rename_wing(self.wing_id, "Renamed")

        self.assertEqual(get_fleet_wings(*self.args())[0]["name"], "Main")
        self.assertEqual(get_fleet_wings(*self.args(), fresh=True)[0]["name"], "Renamed")
        self.assertEqual(len(self.sim.requests_of("GetFleetsFleetIdWings")), 2)

    def test_reading_wings_of_a_fleet_the_fc_does_not_boss(self):
        self.fleet.set_boss(self.pilot_id)

        with self.assertRaises(FleetESIError) as ctx:
            get_fleet_wings(*self.args())

        self.assertEqual(ctx.exception.code, "FLEET_NOT_FOUND")
        self.assertTrue(str(ctx.exception).startswith("Could not read fleet wings: ESI returned HTTP 404"))


class ModelResponseTests(DictResponseTests):
    response_style = "model"


class ObjectResponseTests(DictResponseTests):
    response_style = "object"


class WriteFailureTests(SimFleetTestCase):
    def assert_failure(self, call, code, message):
        with self.assertRaises(FleetESIError) as ctx:
            call()
        self.assertEqual((ctx.exception.code, str(ctx.exception)), (code, message))
        return ctx.exception

    def test_status_codes_map_to_short_errors(self):
        def rename():
            rename_fleet_wing(*self.args(), self.wing_id, "Logi")

        cases = [
            (404, "FLEET_WRITE_NOT_FOUND", "Could not rename the wing: ESI returned HTTP 404 (Not Found)."),
            (403, "FLEET_WRITE_FORBIDDEN", "Could not rename the wing: ESI returned HTTP 403 (Forbidden)."),
            (400, "FLEET_WRITE_REJECTED", "Could not rename the wing: ESI returned HTTP 400 (Bad Request)."),
            (502, "FLEET_WRITE_ERROR", "Could not rename the wing: ESI returned HTTP 502 (Bad Gateway)."),
            (420, "FLEET_WRITE_LIMITED", "Could not rename the wing: ESI error limit reached; try again in 60 seconds."),
        ]
        for status, code, message in cases:
            with self.subTest(status=status):
                self.sim.fail("PutFleetsFleetIdWingsWingId", status=status, times=1)
                error = self.assert_failure(rename, code, message)
                self.assertEqual(error.status_code, None if status == 420 else status)
        self.assertEqual(self.fleet.wing(self.wing_id)["name"], "Main")

    def test_esi_rule_errors_are_shown(self):
        self.assert_failure(
            lambda: delete_fleet_squad(*self.args(), self.alpha),
            "FLEET_WRITE_REJECTED",
            "Could not delete the squad: ESI returned HTTP 400 (Only empty squads can be deleted).",
        )

    def test_writes_after_a_boss_handover_are_not_found(self):
        self.fleet.set_boss(self.pilot_id)

        self.assert_failure(
            lambda: move_fleet_member(*self.args(), self.pilot_id, "squad_member", self.wing_id, self.bravo, name="Pilot X"),
            "FLEET_WRITE_NOT_FOUND",
            "Could not move Pilot X: ESI returned HTTP 404 (The fleet does not exist or you don't have access to it!).",
        )

    def test_invite_errors_name_the_character(self):
        self.sim.create_fleet(2_119_400_009)

        self.assert_failure(
            lambda: invite_fleet_member(*self.args(), 2_119_400_009),
            "FLEET_WRITE_REJECTED",
            "Could not invite character 2119400009: ESI returned HTTP 400 (Character is already in a fleet).",
        )

    def test_timeouts_and_rate_limits(self):
        self.sim.fail("PostFleetsFleetIdWings", exception=timeout_from_the_http_client(), times=1)
        self.assert_failure(
            lambda: create_fleet_wing(*self.args()), "FLEET_WRITE_ERROR", "Could not create a wing: ESI did not respond in time."
        )
        self.sim.fail("PostFleetsFleetIdWings", exception=ESIBucketLimitException("fleet", 30), times=1)
        self.assert_failure(
            lambda: create_fleet_wing(*self.args()),
            "FLEET_WRITE_LIMITED",
            "Could not create a wing: ESI rate limit reached; try again later.",
        )
        self.assertEqual(len(self.fleet.wings), 1)

    def test_a_token_without_the_write_scope_sends_nothing(self):
        self.fc.token_set.all().delete()
        f.add_token(self.fc, self.fc_char, scopes=[FLEET_READ_SCOPE])

        error = self.assert_failure(
            lambda: create_fleet_wing(*self.args()),
            "MISSING_WRITE_SCOPE",
            "No valid ESI token with the required fleet scopes was found for this character.",
        )
        self.assertIsNone(error.status_code)
        self.assertEqual(self.sim.writes(), [])

    def test_a_create_without_the_new_id(self):
        self.sim._handle_PostFleetsFleetIdWingsWingIdSquads = lambda params, body, caller: (201, {})
        self.sim.response_style = "dict"

        self.assert_failure(
            lambda: create_fleet_squad(*self.args(), self.wing_id),
            "FLEET_WRITE_ERROR",
            "Could not create a squad: ESI did not return the new squad id.",
        )


class RecordingOperation:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def result(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class WriteCallTests(TestCase):
    """What reaches django-esi: no response cache, no ETag, and no secrets in texts or logs."""

    def setUp(self):
        self.fc = f.create_user(perms=f.FC_PERMS)
        self.fc_char = f.main_of(self.fc)
        f.add_token(self.fc, self.fc_char)

    def fleets(self, operation):
        fleets = mock.Mock()
        for name in WRITE_OPERATIONS:
            getattr(fleets, name).return_value = operation
        patcher = mock.patch.object(esi_provider, "esi", SimpleNamespace(client=SimpleNamespace(Fleets=fleets)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return fleets

    def test_writes_never_use_the_response_cache(self):
        operation = RecordingOperation({"wing_id": 7})
        fleets = self.fleets(operation)

        self.assertEqual(create_fleet_wing(self.fc, self.fc_char.character_id, 5), 7)
        rename_fleet_wing(self.fc, self.fc_char.character_id, 5, 7, "Main")

        self.assertEqual(operation.calls, [{"use_etag": False, "use_cache": False, "store_cache": False}] * 2)
        kwargs = fleets.PostFleetsFleetIdWings.call_args.kwargs
        self.assertEqual(set(kwargs), {"fleet_id", "token"})
        self.assertEqual(kwargs["token"].character_id, self.fc_char.character_id)

    def test_created_ids_from_bare_numbers(self):
        self.fleets(RecordingOperation(9))

        self.assertEqual(create_fleet_squad(self.fc, self.fc_char.character_id, 5, 7), 9)

    def test_failures_never_show_or_log_response_headers(self):
        self.fleets(RecordingOperation(esi_error(400, "Name too long")))

        with self.assertLogs(esi_provider.logger, "WARNING") as logs, self.assertRaises(FleetESIError) as ctx:
            rename_fleet_squad(self.fc, self.fc_char.character_id, 5, 9, "Alpha")

        self.assertEqual(str(ctx.exception), "Could not rename the squad: ESI returned HTTP 400 (Name too long).")
        output = "\n".join(logs.output)
        self.assertNotIn(SECRET_HEADER, output + str(ctx.exception))
        self.assertIn("x-esi-request-id req-abc-123", output)
        self.assertNotIn("access-", output)

    def test_unexpected_errors_keep_only_the_class_name(self):
        self.fleets(RecordingOperation(RuntimeError("token=access-secret")))

        with self.assertLogs(esi_provider.logger, "WARNING"), self.assertRaises(FleetESIError) as ctx:
            delete_fleet_wing(self.fc, self.fc_char.character_id, 5, 9)

        self.assertEqual(ctx.exception.code, "FLEET_WRITE_ERROR")
        self.assertEqual(str(ctx.exception), "Could not delete the wing: Unexpected ESI error (RuntimeError).")


class AsDictTests(TestCase):
    def test_every_write_operation_is_declared(self):
        # django-esi only builds the operations named when the client is created.
        for name in (*WRITE_OPERATIONS, "GetFleetsFleetIdWings"):
            with self.subTest(name=name):
                self.assertIn(name, esi_provider.esi._operations)

    def test_attribute_objects_keep_wing_fields(self):
        squad = SimpleNamespace(id=2, name="Alpha")
        wing = SimpleNamespace(id=1, name="Main", squads=[squad])

        self.assertEqual(as_dict(wing), {"id": 1, "name": "Main", "squads": [squad]})
        self.assertEqual(as_dict(SimpleNamespace(wing_id=4)), {"wing_id": 4})

    def test_http_errors_are_client_errors(self):
        # The status mapping relies on django-esi's error classes carrying the status code.
        self.assertEqual(esi_provider._status_code(HTTPClientError(status_code=404, headers={}, data=None)), 404)
