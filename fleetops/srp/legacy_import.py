# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Reading the SRP history of aa-srp (app label ``aasrp``), read only, for the import (IMP-2).

The models are found with ``apps.get_model`` and their fields are looked at with ``_meta.get_fields()``, so a version of
aa-srp whose requests have no ``killmail_id``, or only a ship name, still works. Nothing here writes to aa-srp: the
only calls on its models are ``filter``, ``order_by``, ``select_related``, ``iterator`` and ``first``.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps

from fleetops.models import MAX_DATABASE_ID
from fleetops.services import sde

logger = get_extension_logger(__name__)

APP_LABEL = "aasrp"
BATCH_SIZE = 500
KILL_LINK = re.compile(r"/kill/(\d+)")
SENTINEL_USERNAME = "deleted"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STATUSES = (PENDING, APPROVED, REJECTED)

# Which value the Setting of aa-srp used for "ISK lost" -> the value source of FleetOps.
VALUE_SOURCES = {"totalvalue": "zkill_total", "fittedvalue": "zkill_fitted"}


class AaSrpMissing(Exception):
    """aa-srp is not installed (or not usable)."""


@dataclass(slots=True)
class LegacyLink:
    """An SRP link of aa-srp."""

    pk: int
    code: str
    name: str
    status: str
    fleet_time: datetime | None
    fc_character_id: int | None
    creator_id: int | None


@dataclass(slots=True)
class LegacyRequest:
    """An SRP request of aa-srp with the decision read from its comments."""

    pk: int
    code: str
    link_pk: int | None
    character_id: int | None
    character_name: str
    creator_id: int | None
    killmail_id: int | None
    ship_type_id: int | None
    ship_name: str
    status: str
    post_time: datetime | None
    loss_amount: int
    payout_amount: int
    comment: str
    reject_info: str
    decided_by_id: int | None
    decided_at: datetime | None
    decision_text: str


def available() -> bool:
    return apps.is_installed(APP_LABEL)


def _field_names(model) -> set[str]:
    return {field.name for field in model._meta.get_fields()}


def _is_relation(model, name: str) -> bool:
    return any(field.name == name and getattr(field, "is_relation", False) for field in model._meta.get_fields())


def _amount(value) -> int:
    """Whole ISK, never negative; unreadable values are 0."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0
    if not number.is_finite() or number < 0:
        return 0
    return int(number)


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def normalise_status(value) -> str:
    """``pending``, ``approved`` or ``rejected`` for aa-srp's status text; anything else is returned in lower case."""
    return _text(value).casefold()


def killmail_id_of(row, field_names) -> int | None:
    """The killmail of a request: its ``killmail_id`` when aa-srp stores one, else the number in the killboard link."""
    value = getattr(row, "killmail_id", None) if "killmail_id" in field_names else None
    if value in (None, ""):
        match = KILL_LINK.search(_text(getattr(row, "killboard_link", "")))
        value = match.group(1) if match else None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= MAX_DATABASE_ID else None


class AaSrpSource:
    """The SRP links and requests of aa-srp. Raises ``AaSrpMissing`` when aa-srp is not installed."""

    def __init__(self):
        if not available():
            raise AaSrpMissing("aa-srp (aasrp) is not installed.")
        try:
            self.link_model = apps.get_model(APP_LABEL, "SrpLink")
            self.request_model = apps.get_model(APP_LABEL, "SrpRequest")
        except LookupError as exc:
            raise AaSrpMissing("aa-srp (aasrp) is installed but has no SRP links or requests.") from exc
        try:
            self.comment_model = apps.get_model(APP_LABEL, "RequestComment")
        except LookupError:
            self.comment_model = None
        try:
            self.setting_model = apps.get_model(APP_LABEL, "Setting")
        except LookupError:
            self.setting_model = None
        self.request_fields = _field_names(self.request_model)
        self.link_fields = _field_names(self.link_model)
        self.comment_fields = _field_names(self.comment_model) if self.comment_model is not None else set()
        self._ship_names: dict[str, int | None] = {}

    # --- links ---------------------------------------------------------------------------------------------

    def links(self) -> Iterator[LegacyLink]:
        relations = [name for name in ("fleet_commander",) if _is_relation(self.link_model, name)]
        rows = self.link_model.objects.select_related(*relations).order_by("pk")
        for row in rows.iterator(chunk_size=BATCH_SIZE):
            commander = getattr(row, "fleet_commander", None) if relations else None
            yield LegacyLink(
                pk=row.pk,
                code=_text(getattr(row, "srp_code", "")),
                name=_text(getattr(row, "srp_name", "")),
                status=_text(getattr(row, "srp_status", "")),
                fleet_time=getattr(row, "fleet_time", None),
                fc_character_id=getattr(commander, "character_id", None),
                creator_id=getattr(row, "creator_id", None),
            )

    # --- requests ------------------------------------------------------------------------------------------

    def requests(self, since: datetime | None = None, until: datetime | None = None) -> Iterator[LegacyRequest]:
        """The requests posted from ``since`` (included) until ``until`` (excluded), oldest first, 500 at a time."""
        names = self.request_fields
        relations = [name for name in ("character", "srp_link", "ship") if name in names and _is_relation(self.request_model, name)]
        rows = self.request_model.objects.select_related(*relations).order_by("pk")
        if since is not None:
            rows = rows.filter(post_time__gte=since)
        if until is not None:
            rows = rows.filter(post_time__lt=until)
        batch = []
        for row in rows.iterator(chunk_size=BATCH_SIZE):
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                yield from self._convert(batch)
                batch = []
        if batch:
            yield from self._convert(batch)

    def _commenter_attribute(self) -> str:
        """The attribute that holds the id of the user who wrote a comment: aa-srp 5.x names the relation ``creator``;
        ``commenter`` is the fallback for another shape."""
        for name in ("creator", "commenter"):
            if name in self.comment_fields:
                return f"{name}_id" if _is_relation(self.comment_model, name) else name
        return ""

    def _decisions(self, pks) -> dict[int, list[tuple[str, object, datetime | None, str, bool]]]:
        """``{request pk: [(status, commenter id, time, text, is a reject reason), ...]}`` in comment order; one query
        for the whole batch. The status changes carry a status; aa-srp 5.x writes the reason of a rejection as a
        separate comment of type "Reject Reason" without a status."""
        if self.comment_model is None or "new_status" not in self.comment_fields:
            return {}
        by_attribute = self._commenter_attribute()
        has_type = "comment_type" in self.comment_fields
        comments = self.comment_model.objects.filter(srp_request_id__in=list(pks)).order_by("pk")
        found = {}
        for comment in comments.iterator(chunk_size=BATCH_SIZE):
            status = normalise_status(getattr(comment, "new_status", ""))
            kind = _text(getattr(comment, "comment_type", "")).replace("_", " ").casefold() if has_type else ""
            is_reason = kind == "reject reason"
            if status or is_reason:
                found.setdefault(comment.srp_request_id, []).append(
                    (
                        "" if is_reason else status,
                        getattr(comment, by_attribute, None) if by_attribute else None,
                        getattr(comment, "comment_time", None),
                        _text(getattr(comment, "comment", "")),
                        is_reason,
                    )
                )
        return found

    def _ship(self, row) -> tuple[int | None, str]:
        names = self.request_fields
        ship_id = None
        name = ""
        if "ship" in names:
            if _is_relation(self.request_model, "ship"):
                ship_id = getattr(row, "ship_id", None)
                name = _text(getattr(getattr(row, "ship", None), "name", ""))
            else:
                name = _text(getattr(row, "ship", ""))
        elif "ship_id" in names:
            ship_id = getattr(row, "ship_id", None)
        if "ship_name" in names:
            name = _text(getattr(row, "ship_name", "")) or name
        try:
            ship_id = int(ship_id) if ship_id is not None else None
        except (TypeError, ValueError):
            ship_id = None
        if ship_id is not None and not 0 < ship_id <= MAX_DATABASE_ID:
            ship_id = None
        if ship_id is None and name:
            if name not in self._ship_names:
                found = sde.find_item_type(name)
                self._ship_names[name] = found[0] if found else None
            ship_id = self._ship_names[name]
        return ship_id, name

    def _convert(self, rows) -> Iterator[LegacyRequest]:
        decisions = self._decisions([row.pk for row in rows])
        names = self.request_fields
        for row in rows:
            status = normalise_status(getattr(row, "request_status", ""))
            decided_by = decided_at = None
            decision_text = ""
            # The last comment that moved the request to its present status is the decision.
            entries = decisions.get(row.pk, [])
            for comment_status, commenter_id, comment_time, text, is_reason in reversed(entries):
                if not is_reason and comment_status == status:
                    decided_by, decided_at, decision_text = commenter_id, comment_time, text
                    break
            if status == REJECTED:
                # aa-srp 5.x keeps the reason in its own comment; it wins over the (empty) text of the status change.
                for _status, _by, _time, text, is_reason in reversed(entries):
                    if is_reason and text:
                        decision_text = text
                        break
            character = getattr(row, "character", None) if "character" in names else None
            ship_id, ship_name = self._ship(row)
            yield LegacyRequest(
                pk=row.pk,
                code=_text(getattr(row, "request_code", "")),
                link_pk=getattr(row, "srp_link_id", None),
                character_id=getattr(character, "character_id", None),
                character_name=_text(getattr(character, "character_name", "")),
                creator_id=getattr(row, "creator_id", None) if "creator" in names else None,
                killmail_id=killmail_id_of(row, names),
                ship_type_id=ship_id,
                ship_name=ship_name,
                status=status,
                post_time=getattr(row, "post_time", None),
                loss_amount=_amount(getattr(row, "loss_amount", 0)),
                payout_amount=_amount(getattr(row, "payout_amount", 0)),
                comment=_text(getattr(row, "additional_info", "")),
                reject_info=_text(getattr(row, "reject_info", "")) if "reject_info" in names else "",
                decided_by_id=decided_by,
                decided_at=decided_at,
                decision_text=decision_text,
            )

    # --- settings ------------------------------------------------------------------------------------------

    def value_source(self) -> str:
        """The value source of FleetOps that matches ``Setting.loss_value_source`` of aa-srp; empty when unknown."""
        if self.setting_model is None or "loss_value_source" not in _field_names(self.setting_model):
            return ""
        try:
            row = self.setting_model.objects.first()
        except Exception as exc:  # another app's schema or database problem must not stop the import
            logger.debug("FleetOps: reading the aa-srp settings failed (%s).", type(exc).__name__)
            return ""
        raw = _text(getattr(row, "loss_value_source", "")).replace("_", "").replace(" ", "").casefold()
        return VALUE_SOURCES.get(raw, "")
