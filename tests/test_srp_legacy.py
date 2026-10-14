"""The transition guard: a loss that is also claimed in aa-srp or Alliance Auth SRP.

Neither app is installed in the test project. The fakes below carry the fields FleetOps reads:
``killboard_link`` and ``request_status`` of ``aasrp.SrpRequest`` (aa-srp 5.3.0), ``killboard_link`` and
``srp_status`` of ``srp.SrpUserRequest`` (Alliance Auth 5.2 to 5.5).
"""

from unittest import mock

from django.test import SimpleTestCase

from fleetops.srp import legacy

KILL = 131_234_567


class FakeRows(list):
    """What ``filter()`` returns: rows of ``(killboard_link, status)``."""

    def __init__(self, rows, fields):
        super().__init__(rows)
        self.fields = fields

    def values_list(self, *fields):
        self.fields.append(fields)
        return list(self)


class FakeRequestModel:
    """A request model with one text field for the link and one for the status."""

    def __init__(self, *rows, unfiltered=False):
        self.rows = list(rows)
        self.unfiltered = unfiltered
        self.lookups = []
        self.fields = []
        self.objects = self

    def filter(self, **lookups):
        self.lookups.append(lookups)
        needle = lookups["killboard_link__contains"]
        # ``unfiltered``: the database hands over rows the lookup would not (a link field without text).
        return FakeRows([row for row in self.rows if self.unfiltered or needle in row[0]], self.fields)


class FakeApps:
    def __init__(self, **models):
        self.models = models

    def is_installed(self, name):
        return name in self.models

    def get_model(self, app_label, model_name):
        return self.models_by_label[(app_label, model_name)]

    @property
    def models_by_label(self):
        return {
            ("aasrp", "SrpRequest"): self.models.get("aasrp"),
            ("srp", "SrpUserRequest"): self.models.get("allianceauth.srp"),
        }


class ExternalClaimsTests(SimpleTestCase):
    def claims(self, aasrp=None, builtin=None, killmail_id=KILL):
        models = {}
        if aasrp is not None:
            models["aasrp"] = aasrp
        if builtin is not None:
            models["allianceauth.srp"] = builtin
        with mock.patch.object(legacy, "apps", FakeApps(**models)):
            return legacy.external_claims(killmail_id)

    def test_neither_app_installed(self):
        self.assertEqual(self.claims(), [])

    def test_aa_srp_request_with_the_trailing_slash(self):
        model = FakeRequestModel((f"https://zkillboard.com/kill/{KILL}/", "Pending"))

        self.assertEqual(self.claims(aasrp=model), ["aa-srp: Pending"])

    def test_alliance_auth_srp_request(self):
        model = FakeRequestModel((f"https://zkillboard.com/kill/{KILL}/", "Approved"))

        self.assertEqual(self.claims(builtin=model), ["Alliance Auth SRP: Approved"])

    def test_both_systems_are_reported_aa_srp_first(self):
        aasrp = FakeRequestModel((f"https://zkillboard.com/kill/{KILL}/", "Rejected"))
        builtin = FakeRequestModel((f"https://zkillboard.com/kill/{KILL}/", "Pending"))

        self.assertEqual(self.claims(aasrp, builtin), ["aa-srp: Rejected", "Alliance Auth SRP: Pending"])

    def test_every_link_form_is_found(self):
        for link in (
            f"https://zkillboard.com/kill/{KILL}/",
            f"https://zkillboard.com/kill/{KILL}",
            f"http://zkillboard.com/kill/{KILL}/?foo=1",
            f"https://zkillboard.com/kill/{KILL}#top",
            f"https://eve-kill.com/kill/{KILL}",
            f"https://kb.evetools.org/kill/{KILL}",
            f"zkillboard.com/kill/{KILL}/",
        ):
            with self.subTest(link=link):
                self.assertEqual(self.claims(aasrp=FakeRequestModel((link, "Pending"))), ["aa-srp: Pending"])

    def test_other_killmails_do_not_match(self):
        for link in (
            f"https://zkillboard.com/kill/{KILL}9/",
            f"https://zkillboard.com/kill/{KILL}9",
            f"https://zkillboard.com/kill/9{KILL}/",
            f"https://zkillboard.com/kill/{KILL - 1}/",
            f"https://zkillboard.com/character/{KILL}/",
            "",
        ):
            with self.subTest(link=link):
                self.assertEqual(self.claims(aasrp=FakeRequestModel((link, "Pending"))), [])

    def test_a_short_id_does_not_match_a_longer_one(self):
        model = FakeRequestModel(("https://zkillboard.com/kill/1234/", "Pending"), ("https://zkillboard.com/kill/123/", "Approved"))

        self.assertEqual(self.claims(aasrp=model, killmail_id=123), ["aa-srp: Approved"])
        self.assertEqual(self.claims(aasrp=model, killmail_id=12), [])

    def test_the_database_is_asked_for_the_killmail_path_only(self):
        model = FakeRequestModel()
        builtin = FakeRequestModel()

        self.claims(model, builtin)

        self.assertEqual(model.lookups, [{"killboard_link__contains": f"/kill/{KILL}"}])
        self.assertEqual(model.fields, [("killboard_link", "request_status")])
        self.assertEqual(builtin.lookups, [{"killboard_link__contains": f"/kill/{KILL}"}])
        self.assertEqual(builtin.fields, [("killboard_link", "srp_status")])

    def test_two_requests_for_the_same_killmail_are_both_listed(self):
        link = f"https://zkillboard.com/kill/{KILL}/"
        model = FakeRequestModel((link, "Rejected"), (link, "Pending"), (link, "Pending"))

        self.assertEqual(self.claims(aasrp=model), ["aa-srp: Rejected", "aa-srp: Pending"])

    def test_the_status_is_cut_to_a_sensible_length(self):
        model = FakeRequestModel((f"https://zkillboard.com/kill/{KILL}/", "x" * 200), (f"/kill/{KILL}", None))

        self.assertEqual(self.claims(aasrp=model), ["aa-srp: " + "x" * 30, "aa-srp"])

    def test_a_request_without_a_link_text_is_ignored(self):
        model = FakeRequestModel((None, "Pending"), ("", "Approved"), unfiltered=True)

        self.assertEqual(self.claims(aasrp=model), [])

    def test_one_broken_app_leaves_the_other_working(self):
        broken = FakeRequestModel()
        broken.filter = mock.Mock(side_effect=RuntimeError("no such table: aasrp_srprequest"))
        builtin = FakeRequestModel((f"https://zkillboard.com/kill/{KILL}/", "Approved"))

        with self.assertLogs("extensions", "DEBUG") as logs:
            result = self.claims(broken, builtin)

        self.assertEqual(result, ["Alliance Auth SRP: Approved"])
        self.assertIn("aa-srp", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("no such table", logs.output[0])

    def test_an_unreadable_model_gives_an_empty_list(self):
        apps = mock.Mock()
        apps.is_installed.return_value = True
        apps.get_model.side_effect = LookupError("srp.SrpUserRequest")

        with mock.patch.object(legacy, "apps", apps), self.assertLogs("extensions", "DEBUG"):
            self.assertEqual(legacy.external_claims(KILL), [])

    def test_a_schema_without_the_expected_fields_gives_an_empty_list(self):
        model = FakeRequestModel()
        model.filter = mock.Mock(side_effect=Exception("Cannot resolve keyword 'killboard_link'"))

        with self.assertLogs("extensions", "DEBUG"):
            self.assertEqual(self.claims(aasrp=model), [])

    def test_an_id_that_is_not_a_number_gives_an_empty_list(self):
        with self.assertLogs("extensions", "DEBUG"):
            self.assertEqual(self.claims(aasrp=FakeRequestModel(), killmail_id="abc"), [])

    def test_the_installed_apps_registry_is_used(self):
        # In the test project neither app is installed, so the real registry finds nothing.
        self.assertEqual(legacy.external_claims(KILL), [])
