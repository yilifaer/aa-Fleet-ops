# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Stand-ins for the models of aa-srp (app label ``aasrp``), which is not installed in the test project.

The field sets follow aa-srp 5.x (``SrpLink``, ``SrpRequest``, ``RequestComment``, ``Setting``) and two older shapes:
requests without a ``killmail_id`` (the killmail is in the killboard link) and requests with only a ship name. The
managers only offer what a read needs; every write method fails the test that reaches it, which is how the tests
show that the import never writes to aa-srp.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from allianceauth.eveonline.models import EveCharacter
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import models

from fleetops.srp import legacy_import

from .test_integrations_roles import model_field

FLEET_TIME = datetime(2026, 9, 12, 19, 0, tzinfo=dt_timezone.utc)


class WriteAttempted(AssertionError):
    """The code under test wrote to a model of aa-srp."""


class FakeQuerySet:
    def __init__(self, rows, counter=None):
        self.rows = list(rows)
        self.counter = counter if counter is not None else {"queries": 0, "iterators": 0}

    def _clone(self, rows):
        return FakeQuerySet(rows, self.counter)

    def select_related(self, *names):
        return self._clone(self.rows)

    def order_by(self, *names):
        return self._clone(sorted(self.rows, key=lambda row: row.pk))

    def filter(self, **lookups):
        rows = self.rows
        for lookup, value in lookups.items():
            name, _, operator = lookup.partition("__")
            if operator == "in":
                rows = [row for row in rows if getattr(row, name) in value]
            elif operator == "gte":
                rows = [row for row in rows if getattr(row, name) >= value]
            elif operator == "lt":
                rows = [row for row in rows if getattr(row, name) < value]
            else:
                rows = [row for row in rows if getattr(row, name) == value]
        return self._clone(rows)

    def iterator(self, chunk_size=None):
        self.counter["iterators"] += 1
        return iter(self.rows)

    def first(self):
        self.counter["queries"] += 1
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeManager:
    def __init__(self):
        self.rows = []
        self.counter = {"queries": 0, "iterators": 0}

    def _all(self):
        return FakeQuerySet(self.rows, self.counter)

    def select_related(self, *names):
        return self._all().select_related(*names)

    def order_by(self, *names):
        return self._all().order_by(*names)

    def filter(self, **lookups):
        return self._all().filter(**lookups)

    def first(self):
        return self._all().first()

    def all(self):
        return self._all()

    def _forbidden(self, *args, **kwargs):
        raise WriteAttempted("the import wrote to aa-srp")

    create = update = delete = bulk_create = update_or_create = get_or_create = _forbidden


def _model(fields, manager=None):
    return SimpleNamespace(
        _meta=SimpleNamespace(get_fields=lambda: list(fields), concrete_fields=list(fields)), objects=manager or FakeManager()
    )


def _fk(name, to=EveCharacter):
    return model_field(name, models.ForeignKey(to, null=True, blank=True, on_delete=models.SET_NULL))


def link_model():
    return _model(
        [
            model_field("id", models.AutoField(primary_key=True)),
            model_field("srp_name", models.CharField(max_length=254)),
            model_field("srp_status", models.CharField(max_length=9, default="Active")),
            model_field("srp_code", models.CharField(max_length=16)),
            _fk("fleet_commander"),
            model_field("fleet_time", models.DateTimeField()),
            _fk("creator", User),
        ]
    )


def request_model(shape="5.x"):
    """``5.x`` has ``killmail_id``, a ship relation and ``reject_info``; ``no_killmail_id`` has only the killboard link;
    ``ship_name`` has the ship as a name only."""
    fields = [
        model_field("id", models.AutoField(primary_key=True)),
        model_field("request_code", models.CharField(max_length=16)),
        _fk("character"),
        _fk("srp_link"),
        _fk("creator", User),
        model_field("killboard_link", models.CharField(max_length=254)),
        model_field("additional_info", models.TextField()),
        model_field("request_status", models.CharField(max_length=8)),
        model_field("post_time", models.DateTimeField()),
        model_field("loss_amount", models.BigIntegerField(default=0)),
        model_field("payout_amount", models.BigIntegerField(default=0)),
    ]
    if shape == "5.x":
        fields += [
            model_field("killmail_id", models.BigIntegerField(null=True)),
            _fk("ship", User),  # a relation to the ship type; only the attribute names matter here
            model_field("ship_name", models.CharField(max_length=254, blank=True)),
            model_field("reject_info", models.TextField(blank=True)),
        ]
    elif shape == "ship_name":
        fields += [model_field("ship_name", models.CharField(max_length=254))]
    return _model(fields)


def comment_model():
    return _model(
        [
            model_field("id", models.AutoField(primary_key=True)),
            _fk("srp_request", User),
            model_field("comment", models.TextField()),
            model_field("comment_time", models.DateTimeField()),
            _fk("creator", User),
            model_field("comment_type", models.CharField(max_length=32, blank=True)),
            model_field("new_status", models.CharField(max_length=8, blank=True)),
        ]
    )


def setting_model():
    return _model([model_field("id", models.AutoField(primary_key=True)), model_field("loss_value_source", models.CharField(max_length=20))])


class FakeAaSrp:
    """aa-srp, installed: build the rows with ``add_link`` and ``add_request`` and call ``install``."""

    def __init__(self, shape="5.x", value_source="totalValue"):
        self.shape = shape
        self.links = link_model()
        self.requests = request_model(shape)
        self.comments = comment_model()
        self.settings = setting_model()
        if value_source:
            self.settings.objects.rows.append(SimpleNamespace(pk=1, loss_value_source=value_source))
        self.next_link = 1
        self.next_request = 1
        self.next_comment = 1

    def add_link(self, name="Fleet", *, fleet_time=FLEET_TIME, status="Completed", commander=None, creator=None, code=None):
        pk = self.next_link
        self.next_link += 1
        row = SimpleNamespace(
            pk=pk,
            srp_name=name,
            srp_status=status,
            srp_code=code or f"CODE{pk:04d}",
            fleet_commander=commander,
            fleet_time=fleet_time,
            creator_id=getattr(creator, "pk", None),
        )
        self.links.objects.rows.append(row)
        return row

    def add_request(
        self,
        link,
        character,
        *,
        status="Approved",
        loss=100_000_000,
        payout=None,
        killmail_id=None,
        creator=None,
        post_time=None,
        ship_id=587,
        ship_name="Rifter",
        info="",
        reject_info="",
        decided_by=None,
        decided_at=None,
        decision_comment="",
        comment_status=None,
        reject_reason="",
    ):
        pk = self.next_request
        self.next_request += 1
        killmail_id = killmail_id or 90_000_000 + pk
        row = SimpleNamespace(
            pk=pk,
            request_code=f"REQ{pk:05d}",
            character=character,
            srp_link_id=getattr(link, "pk", None),
            creator_id=getattr(creator, "pk", None),
            killboard_link=f"https://zkillboard.com/kill/{killmail_id}/",
            additional_info=info,
            request_status=status,
            post_time=post_time or (getattr(link, "fleet_time", FLEET_TIME) + timedelta(hours=14)),
            loss_amount=loss,
            payout_amount=loss if payout is None else payout,
        )
        if self.shape == "5.x":
            row.killmail_id = killmail_id
            row.ship_id = ship_id
            row.ship = SimpleNamespace(name=ship_name)
            row.ship_name = ship_name
            row.reject_info = reject_info
        elif self.shape == "ship_name":
            row.ship_name = ship_name
        self.requests.objects.rows.append(row)
        if decided_by is not None or decided_at is not None or decision_comment:
            self.add_comment(row, decided_by, decision_comment, decided_at or row.post_time + timedelta(hours=3), comment_status or status)
        if reject_reason:
            # aa-srp 5.x writes the reason of a rejection as a second comment, without a status.
            self.add_comment(row, decided_by, reject_reason, (decided_at or row.post_time + timedelta(hours=3)), "", "Reject Reason")
        return row

    def add_comment(self, request, commenter, text="", when=None, new_status="", comment_type=None):
        pk = self.next_comment
        self.next_comment += 1
        row = SimpleNamespace(
            pk=pk,
            srp_request_id=request.pk,
            comment=text,
            comment_time=when or FLEET_TIME,
            creator_id=getattr(commenter, "pk", None),
            comment_type=comment_type or ("Status Change" if new_status else "Comment"),
            new_status=new_status,
        )
        self.comments.objects.rows.append(row)
        return row

    def install(self, testcase):
        """Make ``apps`` find the fake models; undone when the test ends."""
        real_is_installed = django_apps.is_installed
        real_get_model = django_apps.get_model
        fakes = {
            ("aasrp", "SrpLink"): self.links,
            ("aasrp", "SrpRequest"): self.requests,
            ("aasrp", "RequestComment"): self.comments,
            ("aasrp", "Setting"): self.settings,
        }

        def is_installed(name):
            return name == "aasrp" or real_is_installed(name)

        def get_model(app_label, model_name=None, **kwargs):
            if (app_label, model_name) in fakes:
                return fakes[(app_label, model_name)]
            return real_get_model(app_label, model_name, **kwargs)

        for name, replacement in (("is_installed", is_installed), ("get_model", get_model)):
            patcher = mock.patch.object(django_apps, name, side_effect=replacement)
            patcher.start()
            testcase.addCleanup(patcher.stop)
        return self


def uninstalled(testcase):
    """aa-srp is not installed (the default in the test project); kept for the tests that say so."""
    assert not legacy_import.available()
