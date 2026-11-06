# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Importing the SRP history of aa-srp (IMP-3, IMP-4, IMP-7, IMP-8).

The importer reads aa-srp through ``legacy_import`` and never writes to it. An approved request becomes a paid claim, a
rejected one a rejected claim, a pending one is not imported. Every imported claim carries ``source`` aasrp and the
``external_id`` ``aasrp:<request number>``, which makes a second run update instead of duplicate; claims that were not
imported are never touched. The stand-in killmail of an imported claim (``SrpKillmail.origin`` aasrp) keeps the loss from
being claimed a second time. Each batch runs under ``quota_lock`` and in one transaction. No network is used, except by
``enrich``.
"""

import json
import re
import time as time_module
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.hooks import get_extension_logger
from django.contrib.auth import get_user_model
from django.utils import timezone

from fleetops import texts
from fleetops.models import SrpCategory, SrpClaim, SrpClaimEvent, SrpFleetLink, SrpKillmail, SrpSettings
from fleetops.providers import zkillboard
from fleetops.providers.esi import FleetESIError
from fleetops.providers.esi_killmails import get_killmail
from fleetops.services import sde
from fleetops.services.audit import audit_event
from fleetops.services.periods import format_eve
from fleetops.srp import categories, claims, killmails, quota
from fleetops.srp import policy as rules
from fleetops.srp import legacy_import
from fleetops.srp.legacy_import import APPROVED, PENDING, REJECTED, LegacyLink, LegacyRequest
from fleetops.srp.types import SrpError

logger = get_extension_logger(__name__)

PREFIX = "aasrp:"
LINK_PREFIX = "aa"
MAX_COMMENT = 1000
MAX_LISTED = 50
RANK = {APPROVED: 2, REJECTED: 1}


def claim_rank(claim) -> int:
    """The rank of the request an imported claim stands for: a rejected claim is 1, any other (paid) one 2."""
    return RANK[REJECTED] if claim.status == SrpClaim.Status.REJECTED else RANK[APPROVED]

# Skip reasons, in the order the report lists them.
SKIP_NO_KILLMAIL = "no_killmail"
SKIP_DUPLICATE = "duplicate"
SKIP_NATIVE = "native_claim"
SKIP_PENDING = "pending"
SKIP_NO_SHIP = "no_ship"
SKIP_NO_CHARACTER = "no_character"
SKIP_KILLMAIL_CHANGED = "killmail_changed"
SKIP_STATUS = "unknown_status"
SKIP_LABELS = {
    SKIP_NO_KILLMAIL: "no killmail ID",
    SKIP_DUPLICATE: "same killmail in another aa-srp request",
    SKIP_NATIVE: "already a FleetOps claim",
    SKIP_PENDING: "pending in aa-srp",
    SKIP_NO_SHIP: "ship type not found",
    SKIP_NO_CHARACTER: "no character",
    SKIP_KILLMAIL_CHANGED: "killmail of the request changed in aa-srp",
    SKIP_STATUS: "unknown status",
}

# What a second run compares; the values of these fields are the ones aa-srp decides.
COMPARED = (
    "status",
    "scope",
    "approved_amount",
    "suggested_amount",
    "limited_amount",
    "base_amount",
    "loss_value",
    "value_source",
    "decision_reason",
    "decided_by_id",
    "decided_at",
    "paid_at",
    "submitted_at",
    "member_comment",
    "user_id",
    "character_id",
    "character_name",
    "main_character_id",
    "main_character_name",
    "corporation_id",
    "corporation_name",
    "corporation_ticker",
    "alliance_id",
    "alliance_name",
    "link_id",
    "category_id",
    "fleet_label",
    "fc_user_id",
    "fc_main_character_name",
    "ship_type_id",
    "ship_type_name",
    "killmail_time",
    "counts_personal",
    "counts_corporation",
)
# The old and new value are written to the event only for these; for the others only the name of the field.
SHOWN_IN_UPDATE = frozenset(
    {"status", "approved_amount", "suggested_amount", "limited_amount", "base_amount", "paid_at", "killmail_time", "scope"}
)


class ImportConfigError(Exception):
    """An option of the import is wrong (the message names the option)."""


@dataclass(slots=True)
class Options:
    dry_run: bool = False
    since: datetime | None = None
    until: datetime | None = None
    category_map: tuple = ()
    actor: object = None
    batch_size: int = 500


@dataclass(slots=True)
class Item:
    """One request that is going to be imported, with the link it belongs to."""

    request: LegacyRequest
    link: LegacyLink | None
    category: SrpCategory | None = None


@dataclass
class Report:
    """What the import did (or would do): counts, never names of members."""

    dry_run: bool = False
    links_seen: int = 0
    links_used: int = 0
    requests_seen: int = 0
    pending: int = 0
    pending_amount: int = 0
    # {"YYYY-MM": {status: [count, ISK]}} by the month of the link (the month of the loss, as the reports count it).
    months: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(lambda: [0, 0])))
    create: int = 0
    update: int = 0
    unchanged: int = 0
    skipped: Counter = field(default_factory=Counter)
    native_claims: list = field(default_factory=list)
    unmatched_links: Counter = field(default_factory=Counter)
    category_links: int = 0
    estimated_times: int = 0
    without_user: int = 0
    links_created: int = 0
    links_updated: int = 0
    seconds: float = 0.0
    value_source: str = ""

    def counts(self) -> dict:
        """The numbers written to the audit entry."""
        return {
            "dry_run": self.dry_run,
            "links_seen": self.links_seen,
            "links_used": self.links_used,
            "requests_seen": self.requests_seen,
            "created": self.create,
            "updated": self.update,
            "unchanged": self.unchanged,
            "skipped": dict(self.skipped),
            "pending": self.pending,
            "links_created": self.links_created,
            "links_updated": self.links_updated,
        }


def month_key(value: datetime | None) -> str:
    return format_eve(value, "%Y-%m") if value else "unknown"


# --- Options -------------------------------------------------------------------------------------------------


def load_category_map(path) -> tuple:
    """The rules of a ``--category-map`` file: ``[{"pattern": regex, "category": key}]`` -> ``((regex, category), ...)``.

    The first rule that matches the name of a link decides. Raises ``ImportConfigError`` for an unreadable file, a bad
    pattern or a category that does not exist.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ImportConfigError(f"--category-map: cannot read {path!s} ({type(exc).__name__}).") from exc
    if not isinstance(data, list):
        raise ImportConfigError("--category-map: the file must hold a JSON list of {\"pattern\", \"category\"}.")
    result = []
    for number, entry in enumerate(data, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str) or not isinstance(entry.get("category"), str):
            raise ImportConfigError(f"--category-map: entry {number} needs a text \"pattern\" and a text \"category\".")
        try:
            pattern = re.compile(entry["pattern"])
        except re.error as exc:
            raise ImportConfigError(f"--category-map: entry {number} has an invalid pattern ({exc}).") from exc
        category = SrpCategory.objects.filter(key=entry["category"]).first()
        if category is None:
            raise ImportConfigError(f"--category-map: entry {number} names the unknown category \"{entry['category']}\".")
        result.append((pattern, category))
    return tuple(result)


def matched_category(name: str, category_map) -> SrpCategory | None:
    for pattern, category in category_map:
        if pattern.search(name or ""):
            return category
    return None


# --- Collecting ----------------------------------------------------------------------------------------------


def _link_time(link: LegacyLink | None, request: LegacyRequest) -> datetime | None:
    return (link.fleet_time if link else None) or request.post_time


def collect(source, options: Options, report: Report) -> tuple[list[Item], dict[int, LegacyLink]]:
    """Read the requests, count them and choose what to import: one request per killmail, pending ones left out.

    Returns the items (oldest request first) and the links of aa-srp by number. Reads only.
    """
    links = {link.pk: link for link in source.links()}
    report.links_seen = len(links)
    candidates: dict[int, list[Item]] = defaultdict(list)
    order = []
    for request in source.requests(options.since, options.until):
        report.requests_seen += 1
        link = links.get(request.link_pk)
        when = _link_time(link, request)
        amount = request.payout_amount if request.status == APPROVED else request.loss_amount
        report.months[month_key(when)][request.status][0] += 1
        report.months[month_key(when)][request.status][1] += amount
        if request.status == PENDING:
            report.pending += 1
            report.pending_amount += request.loss_amount
            report.skipped[SKIP_PENDING] += 1
            continue
        if request.status not in (APPROVED, REJECTED):
            report.skipped[SKIP_STATUS] += 1
            continue
        if request.killmail_id is None:
            report.skipped[SKIP_NO_KILLMAIL] += 1
            continue
        if request.character_id is None:
            report.skipped[SKIP_NO_CHARACTER] += 1
            continue
        if request.ship_type_id is None:
            report.skipped[SKIP_NO_SHIP] += 1
            continue
        item = Item(request, link, matched_category(link.name, options.category_map) if link else None)
        if request.killmail_id not in candidates:
            order.append(request.killmail_id)
        candidates[request.killmail_id].append(item)

    items = []
    for killmail_id in order:
        group = candidates[killmail_id]
        # Approved before rejected, then the newest, then the highest number.
        group.sort(
            key=lambda item: (
                RANK[item.request.status],
                item.request.post_time or datetime.min.replace(tzinfo=dt_timezone.utc),
                item.request.pk,
            ),
            reverse=True,
        )
        items.append(group[0])
        report.skipped[SKIP_DUPLICATE] += len(group) - 1
    items.sort(key=lambda item: item.request.pk)
    return items, links


# --- Mapping -------------------------------------------------------------------------------------------------


@dataclass
class Context:
    """What the claims of one batch need from Alliance Auth, read with a few queries."""

    users: dict
    owners: dict
    characters: dict
    sentinel_ids: set
    value_source: str
    fleet_category: SrpCategory | None
    policy: object
    ship_names: dict


def _main(user):
    return rules.main_character(user) if user is not None else None


def build_context(items: list[Item], value_source: str, fleet_category) -> Context:
    User = get_user_model()
    user_ids = set()
    character_ids = set()
    ship_ids = set()
    for item in items:
        request = item.request
        user_ids.update(pk for pk in (request.creator_id, request.decided_by_id) if pk is not None)
        character_ids.add(request.character_id)
        if item.link is not None and item.link.fc_character_id is not None:
            character_ids.add(item.link.fc_character_id)
        if item.link is not None and item.link.creator_id is not None:
            user_ids.add(item.link.creator_id)
        if not request.ship_name:
            ship_ids.add(request.ship_type_id)
    users = User.objects.filter(pk__in=user_ids).select_related("profile__main_character").in_bulk() if user_ids else {}
    owners = {}
    for ownership in CharacterOwnership.objects.filter(character__character_id__in=character_ids).select_related(
        "user__profile__main_character", "character"
    ):
        owners[ownership.character.character_id] = ownership.user
    characters = {
        row.character_id: row for row in EveCharacter.objects.filter(character_id__in=character_ids)
    }
    sentinels = {pk for pk, user in users.items() if user.username == legacy_import.SENTINEL_USERNAME}
    names = sde.item_type_names(list(ship_ids)) if ship_ids else {}
    settings_obj = SrpSettings.get_solo()
    return Context(
        users=users,
        owners=owners,
        characters=characters,
        sentinel_ids=sentinels,
        value_source=value_source,
        fleet_category=fleet_category,
        policy=rules.current_policy(corporation_id=None, settings_obj=settings_obj),
        ship_names=names,
    )


def claimant_of(request: LegacyRequest, ctx: Context):
    """The Auth user a request belongs to: its creator, else the owner of the character; None when neither is known."""
    if request.creator_id is not None and request.creator_id not in ctx.sentinel_ids:
        user = ctx.users.get(request.creator_id)
        if user is not None:
            return user
    return ctx.owners.get(request.character_id)


def decider_of(request: LegacyRequest, ctx: Context):
    if request.decided_by_id is None or request.decided_by_id in ctx.sentinel_ids:
        return None
    return ctx.users.get(request.decided_by_id)


def fc_of(link: LegacyLink | None, ctx: Context):
    """``(user, main id, main name)`` of the FC of a link: the owner of the commander's character, empty if unknown."""
    if link is None or link.fc_character_id is None:
        return None, None, ""
    user = ctx.owners.get(link.fc_character_id)
    if user is None:
        return None, None, ""
    main = _main(user)
    return user, getattr(main, "character_id", None), getattr(main, "character_name", "") or ""


def link_values(link: LegacyLink, category, ctx: Context, state: dict) -> dict:
    """The fields of the FleetOps link for a link of aa-srp. ``state`` is the settled state of its imported claims."""
    fleet_time = link.fleet_time or state.get("first_time") or timezone.now()
    fc_user, fc_main_id, fc_main_name = (None, None, "") if category is not None else fc_of(link, ctx)
    completed_at = state.get("completed_at")
    return {
        "kind": SrpFleetLink.Kind.STANDING if category is not None else SrpFleetLink.Kind.FLEET,
        "operation": None,
        "origin": SrpFleetLink.Origin.AASRP,
        "code": (LINK_PREFIX + link.code)[:32] if link.code else f"{LINK_PREFIX}{link.pk}",
        "name": link.name[:255],
        "category": category,
        "link_date": fleet_time.astimezone(dt_timezone.utc).date(),
        "is_open": False,
        "closed_at": completed_at or fleet_time,
        "completed_at": completed_at,
        "fc_user": fc_user,
        "fc_main_character_id": fc_main_id,
        "fc_main_character_name": fc_main_name[:255],
    }


def claim_values(item: Item, ctx: Context, km_time: datetime) -> dict:
    """The fields of the claim for a request of aa-srp (IMP-3), without the killmail and the link."""
    request = item.request
    link = item.link
    standing = item.category is not None
    scope = SrpClaim.Scope.OUT_OF_FLEET if standing else SrpClaim.Scope.IN_FLEET
    user = claimant_of(request, ctx)
    decider = decider_of(request, ctx)
    main = _main(user)
    character = ctx.characters.get(request.character_id)
    if main is not None:
        identity_source = main
        main_id, main_name = main.character_id, main.character_name
    else:
        identity_source = character
        main_id, main_name = None, ""
    fc_user, _fc_main_id, fc_main_name = (None, None, "") if standing else fc_of(link, ctx)
    link_name = link.name if link else ""
    if standing:
        fleet_label = link_name
    else:
        fleet_label = f"{link_name} — {format_eve(_link_time(link, request))} EVE" if link else ""
    ship_name = request.ship_name or ctx.ship_names.get(request.ship_type_id, "")
    values = {
        "scope": scope,
        "category": item.category if standing else ctx.fleet_category,
        "fleet_label": fleet_label[:255],
        "fc_user": fc_user,
        "fc_main_character_name": fc_main_name[:255],
        "user": user,
        "character_id": request.character_id,
        "character_name": (request.character_name or getattr(character, "character_name", "") or "")[:255],
        "main_character_id": main_id,
        "main_character_name": (main_name or "")[:255],
        "corporation_id": getattr(identity_source, "corporation_id", None),
        "corporation_name": (getattr(identity_source, "corporation_name", "") or "")[:255],
        "corporation_ticker": (getattr(identity_source, "corporation_ticker", "") or "")[:20],
        "alliance_id": getattr(identity_source, "alliance_id", None),
        "alliance_name": (getattr(identity_source, "alliance_name", "") or "")[:255],
        "killmail_time": km_time,
        "ship_type_id": request.ship_type_id,
        "ship_type_name": ship_name[:255],
        "solar_system_name": "",
        "loss_value": Decimal(request.loss_amount),
        "value_source": ctx.value_source,
        "payout_basis": SrpClaim.PayoutBasis.IMPORTED,
        "policy": {},
        "calculation": {},
        "base_amount": request.loss_amount,
        "over_limit": False,
        "counts_personal": rules.counts_toward_personal(ctx.policy, scope),
        "counts_corporation": rules.counts_toward_corporation(ctx.policy, scope),
        "fleet_check": "",
        "fleet_check_params": {},
        "warnings": [],
        "member_comment": request.comment[:MAX_COMMENT],
        "auto_rejected": False,
        "decided_by": decider,
        "decided_at": request.decided_at,
        "submitted_at": request.post_time or km_time,
        "source": SrpClaim.Source.AASRP,
    }
    if request.status == APPROVED:
        amount = request.payout_amount or request.loss_amount
        values.update(
            status=SrpClaim.Status.PAID,
            approved_amount=amount,
            suggested_amount=amount,
            limited_amount=amount,
            decision_reason="",
            paid_by=None,
            paid_at=request.decided_at or request.post_time or km_time,
            payment_note=texts.SRP_IMPORTED_PAYMENT_NOTE(),
            payment_batch=None,
        )
    else:
        values.update(
            status=SrpClaim.Status.REJECTED,
            approved_amount=None,
            # What aa-srp recorded as the payout (usually 0), not the loss: the requested ISK is not made larger.
            suggested_amount=request.payout_amount,
            limited_amount=request.payout_amount,
            decision_reason=request.reject_info or request.decision_text,
            paid_by=None,
            paid_at=None,
            payment_note="",
            payment_batch=None,
        )
    return values


FOREIGN_KEYS = ("user", "decided_by", "link", "category", "fc_user")


def _shown(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def changes_of(claim: SrpClaim, values: dict) -> dict:
    """``{field: [old, new]}`` for the compared fields where the stored claim differs from ``values``."""
    desired = dict(values)
    for name in FOREIGN_KEYS:
        desired[f"{name}_id"] = getattr(desired.pop(name, None), "pk", None)
    changed = {}
    for name in COMPARED:
        if name not in desired:
            continue
        old = getattr(claim, name)
        if old != desired[name]:
            changed[name] = [old, desired[name]]
    return changed


# --- Running -------------------------------------------------------------------------------------------------


class Importer:
    """One run of the import."""

    def __init__(self, options: Options, source=None, on_collected=None):
        self.options = options
        # Called with the report once the requests are read and before anything is written.
        self.on_collected = on_collected
        self.source = source or legacy_import.AaSrpSource()
        self.report = Report(dry_run=options.dry_run)
        self.link_rows: dict[int, SrpFleetLink] = {}
        self.link_state: dict[int, dict] = {}

    # Links ---------------------------------------------------------------------------------------------------

    def _prepare_link_states(self, items: list[Item]) -> None:
        """How each link ends: completed when every claim imported through it is paid or rejected and one is paid."""
        grouped = defaultdict(list)
        for item in items:
            if item.link is not None:
                grouped[item.link.pk].append(item.request)
        for pk, requests in grouped.items():
            paid_times = [
                request.decided_at or request.post_time for request in requests if request.status == APPROVED
            ]
            paid_times = [value for value in paid_times if value is not None]
            state = {"first_time": min((r.post_time for r in requests if r.post_time), default=None)}
            if any(request.status == APPROVED for request in requests):
                state["completed_at"] = max(paid_times) if paid_times else None
            self.link_state[pk] = state

    def _ensure_link(self, item: Item, ctx: Context, write: bool) -> SrpFleetLink | None:
        link = item.link
        if link is None:
            return None
        if link.pk in self.link_rows:
            return self.link_rows[link.pk]
        values = link_values(link, item.category, ctx, self.link_state.get(link.pk, {}))
        external_id = f"{PREFIX}{link.pk}"
        row = SrpFleetLink.objects.filter(external_id=external_id).first()
        if row is None:
            self.report.links_used += 1
            if item.category is not None:
                self.report.category_links += 1
            self.report.links_created += 1
            if write:
                code = values["code"]
                # A code that is taken by a link of FleetOps gets the link number as well.
                if SrpFleetLink.objects.filter(code=code).exists():
                    code = f"{LINK_PREFIX}{link.pk}-{code}"[:32]
                values["code"] = code
                row = SrpFleetLink.objects.create(external_id=external_id, created_by=self.options.actor, **values)
        else:
            self.report.links_used += 1
            if item.category is not None:
                self.report.category_links += 1
            values.pop("code")
            changed = {name: value for name, value in values.items() if getattr(row, name) != value}
            if changed:
                self.report.links_updated += 1
                if write:
                    for name, value in changed.items():
                        setattr(row, name, value)
                    row.save()
        self.link_rows[link.pk] = row
        return row

    # Claims --------------------------------------------------------------------------------------------------

    def _killmail(self, item: Item, existing: dict, write: bool):
        """``(killmail row or None, time, estimated)``: the stored killmail, or the stand-in for the loss."""
        request = item.request
        km = existing.get(request.killmail_id)
        if km is not None and not killmails.is_placeholder(km):
            return km, km.killmail_time, False
        # The stand-in has no real time: the time of the link stands for it (and follows the link when that changes).
        return km, _link_time(item.link, request) or timezone.now(), True

    def _create_placeholder(self, item: Item, ctx: Context, km_time: datetime) -> SrpKillmail:
        request = item.request
        return SrpKillmail.objects.create(
            killmail_id=request.killmail_id,
            killmail_hash="",
            killmail_time=km_time,
            solar_system_id=0,
            victim_character_id=request.character_id,
            ship_type_id=request.ship_type_id,
            ship_type_name=(request.ship_name or ctx.ship_names.get(request.ship_type_id, ""))[:255],
            origin=SrpKillmail.Origin.AASRP,
            zkill_status=SrpKillmail.ZkillStatus.DISABLED,
            zkill_next_attempt_at=None,
            # Nothing to look up: the system is unknown and the lookup is not made for a stand-in.
            location_checked_at=timezone.now(),
        )

    def _process(self, batch: list[Item], value_source: str, fleet_category, write: bool) -> None:
        report = self.report
        ctx = build_context(batch, value_source, fleet_category)
        killmail_ids = [item.request.killmail_id for item in batch]
        external_ids = [f"{PREFIX}{item.request.pk}" for item in batch]
        kms = SrpKillmail.objects.in_bulk(killmail_ids)
        by_killmail = {claim.killmail_id: claim for claim in SrpClaim.objects.filter(killmail_id__in=killmail_ids)}
        by_external = {claim.external_id: claim for claim in SrpClaim.objects.filter(external_id__in=external_ids)}
        for item in batch:
            request = item.request
            external_id = f"{PREFIX}{request.pk}"
            claim = by_external.get(external_id)
            other = by_killmail.get(request.killmail_id)
            switched_from = ""
            if claim is None and other is not None:
                if other.source == SrpClaim.Source.AASRP and other.external_id.startswith(PREFIX):
                    if claim_rank(other) >= RANK[request.status]:
                        report.skipped[SKIP_DUPLICATE] += 1
                        continue
                    # An earlier run (a window, or the request was still pending) kept the worse request of this
                    # killmail: the better one takes its place (IMP-5: approved before rejected).
                    claim, switched_from = other, other.external_id
                    by_killmail[request.killmail_id] = claim
                elif other.source == SrpClaim.Source.AASRP:
                    report.skipped[SKIP_DUPLICATE] += 1
                    continue
                else:
                    report.skipped[SKIP_NATIVE] += 1
                    if len(report.native_claims) < MAX_LISTED:
                        report.native_claims.append(other.reference)
                    continue
            if claim is not None and claim.killmail_id != request.killmail_id:
                report.skipped[SKIP_KILLMAIL_CHANGED] += 1
                continue
            km, km_time, estimated = self._killmail(item, kms, write)
            link_row = self._ensure_link(item, ctx, write)
            values = claim_values(item, ctx, km_time)
            if km is not None and not estimated:
                # A killmail FleetOps stored already has its real system.
                values["solar_system_name"] = km.solar_system_name
            if values["user"] is None:
                report.without_user += 1
            if link_row is not None:
                values["link"] = link_row
            else:
                values["link"] = None
            if claim is None:
                report.create += 1
                if estimated:
                    report.estimated_times += 1
                if write:
                    self._create(item, ctx, km, km_time, estimated, values, external_id)
                continue
            changed = changes_of(claim, values)
            if switched_from:
                changed["external_id"] = [switched_from, external_id]
                values["external_id"] = external_id
            if not changed:
                report.unchanged += 1
                continue
            report.update += 1
            if write:
                self._update(claim, item, km, km_time, estimated, values, changed)

    def _event_data(self, item: Item, estimated: bool) -> dict:
        request = item.request
        return {
            "aasrp_request": request.pk,
            "request_code": request.code,
            "aasrp_status": request.status,
            "killmail_time_estimated": estimated,
        }

    def _create(self, item, ctx, km, km_time, estimated, values, external_id) -> None:
        if km is None:
            km = self._create_placeholder(item, ctx, km_time)
        claim = SrpClaim.objects.create(killmail=km, external_id=external_id, **values)
        claims.record_event(
            claim,
            SrpClaimEvent.Action.IMPORT,
            actor=self.options.actor,
            amount=claim.approved_amount if claim.approved_amount is not None else claim.suggested_amount,
            data=self._event_data(item, estimated),
            new={"status": claim.status, "external_id": external_id, "approved_amount": claim.approved_amount},
        )

    def _update(self, claim, item, km, km_time, estimated, values, changed) -> None:
        previous_status = claim.status
        if km is not None and km.origin == SrpKillmail.Origin.AASRP and not km.killmail_hash and km.killmail_time != km_time:
            SrpKillmail.objects.filter(pk=km.pk).update(killmail_time=km_time)
        for name, value in values.items():
            setattr(claim, name, value)
        claim.save()
        data = self._event_data(item, estimated)
        data["update"] = {
            name: ([_shown(old), _shown(new)] if name in SHOWN_IN_UPDATE else True) for name, (old, new) in changed.items()
        }
        claims.record_event(
            claim,
            SrpClaimEvent.Action.IMPORT,
            actor=self.options.actor,
            from_status=previous_status,
            amount=claim.approved_amount if claim.approved_amount is not None else claim.suggested_amount,
            data=data,
            new={"status": claim.status, "external_id": claim.external_id, "updated": sorted(changed)},
        )

    # The run -------------------------------------------------------------------------------------------------

    def run(self) -> Report:
        options = self.options
        started = time_module.monotonic()
        report = self.report
        items, _links = collect(self.source, options, report)
        if self.on_collected is not None:
            self.on_collected(report)
        self._prepare_link_states(items)
        value_source = self.source.value_source()
        report.value_source = value_source
        fleet_category = categories.system_category()
        size = max(1, options.batch_size)
        for start in range(0, len(items), size):
            batch = items[start : start + size]
            if options.dry_run:
                self._process(batch, value_source, fleet_category, write=False)
            else:
                with quota.quota_lock():
                    self._process(batch, value_source, fleet_category, write=True)
        report.unmatched_links = self._unmatched_links(items, options)
        report.seconds = time_module.monotonic() - started
        if not options.dry_run:
            audit_event(
                options.actor,
                "srp.import_aasrp",
                "SrpImport",
                timezone.now().strftime("%Y%m%d%H%M%S"),
                new_value={
                    "options": {
                        "since": options.since.isoformat() if options.since else None,
                        "until": options.until.isoformat() if options.until else None,
                        "category_map": len(options.category_map),
                        "batch_size": options.batch_size,
                    },
                    "counts": report.counts(),
                },
            )
        return report

    @staticmethod
    def _unmatched_links(items, options) -> Counter:
        """How many requests (about to be imported) each link name that no rule of ``--category-map`` matched has (only with a map)."""
        if not options.category_map:
            return Counter()
        return Counter(item.link.name for item in items if item.link is not None and item.category is None)


def run_import(options: Options, source=None, on_collected=None) -> Report:
    return Importer(options, source, on_collected).run()


# --- Undo (IMP-7) --------------------------------------------------------------------------------------------


def undo(actor, dry_run: bool = False) -> dict:
    """Delete everything the import made: the imported claims (their events go with them), the killmails that no claim
    uses any more, and the imported links. Claims of FleetOps are never touched. Returns the counts."""
    counts = {
        "claims": SrpClaim.objects.filter(source=SrpClaim.Source.AASRP).count(),
        "links": SrpFleetLink.objects.filter(origin=SrpFleetLink.Origin.AASRP).count(),
    }
    imported_killmails = SrpKillmail.objects.filter(origin=SrpKillmail.Origin.AASRP)
    counts["killmails"] = imported_killmails.filter(claim__isnull=True).count() + imported_killmails.filter(
        claim__source=SrpClaim.Source.AASRP
    ).count()
    counts["dry_run"] = dry_run
    if dry_run:
        return counts
    with quota.quota_lock():
        SrpClaim.objects.filter(source=SrpClaim.Source.AASRP).delete()
        SrpKillmail.objects.filter(origin=SrpKillmail.Origin.AASRP, claim__isnull=True).delete()
        SrpFleetLink.objects.filter(origin=SrpFleetLink.Origin.AASRP).delete()
    audit_event(
        actor, "srp.import_aasrp_undo", "SrpImport", timezone.now().strftime("%Y%m%d%H%M%S"), new_value=counts
    )
    return counts


# --- Enrich (IMP-8) ------------------------------------------------------------------------------------------


# One lookup per slot of the zKillboard provider; a lookup that finds the slot taken waits this many times.
DEFAULT_ENRICH_RATE = 1.0 / zkillboard.SLOT_SECONDS
ENRICH_SLOT_WAITS = 5


@dataclass
class EnrichResult:
    done: int = 0
    failed: int = 0
    stopped: str = ""
    remaining: int = 0


def placeholders():
    """The stand-in killmails of imported claims that ESI has not completed yet, lowest ID first."""
    return SrpKillmail.objects.filter(origin=SrpKillmail.Origin.AASRP, killmail_hash="").order_by("pk")


def _paused_message(found) -> str:
    minutes = max(1, -(-int(found.retry_after or 0) // 60))
    return f"zKillboard lookups are paused for about {minutes} minutes; run the command again later."


def enrich(
    *, limit: int = 0, rate: float = DEFAULT_ENRICH_RATE, actor=None, sleep=time_module.sleep
) -> EnrichResult:
    """Give the stand-in killmails their real killmail: the hash from zKillboard, the killmail from ESI, then
    ``complete_placeholder``. One lookup at a time, ``rate`` per second at most (the default is what FleetOps' own
    zKillboard slot allows: one lookup per ``SLOT_SECONDS``). A lookup that finds the slot taken waits for it and
    is tried again; when zKillboard is paused (too many failures, a request to slow down, a block) the run stops
    at once. A killmail that cannot be read now is left as it is and tried again by the next run, which continues
    where this one stopped. Raises ``SrpError`` when zKillboard is switched off in the SRP settings."""
    if not SrpSettings.get_solo().zkillboard_enabled:
        raise SrpError("zkill_off", texts.SRP_ENRICH_ZKILL_OFF())
    delay = 1.0 / rate if rate > 0 else 0.0
    result = EnrichResult()
    todo = list(placeholders().values_list("pk", flat=True)[: limit or None])
    first = True
    for pk in todo:
        km = SrpKillmail.objects.filter(pk=pk).first()
        if km is None or not killmails.is_placeholder(km):
            continue
        if not first and delay:
            sleep(delay)
        first = False
        for attempt in range(ENRICH_SLOT_WAITS + 1):
            found = zkillboard.fetch_kill(pk)
            if found.status != "throttled":
                break
            if attempt < ENRICH_SLOT_WAITS:
                # The slot is taken (by this run's previous lookup or by another process); wait until it is free.
                sleep(max(1, int(found.retry_after or zkillboard.SLOT_SECONDS)))
        if found.status == "throttled":
            result.stopped = "zKillboard lookups are busy with other work of FleetOps; run the command again later."
            break
        if found.status in ("unavailable", "blocked") and (not found.requested or found.retry_after):
            # Paused: no request was sent, or the answer just paused the requests. Nothing is tried until it ends.
            if found.requested:
                result.failed += 1
            result.stopped = _paused_message(found)
            break
        if not (found.ok and found.killmail_hash):
            result.failed += 1
            continue
        try:
            payload = get_killmail(pk, found.killmail_hash)
            killmails.complete_placeholder(km, payload, found.killmail_hash, actor=actor)
        except FleetESIError as exc:
            result.failed += 1
            if exc.code == "ESI_LIMITED":
                result.stopped = "ESI asked to slow down; run the command again later."
                break
            continue
        except SrpError:
            result.failed += 1
            continue
        result.done += 1
    result.remaining = placeholders().count()
    return result
