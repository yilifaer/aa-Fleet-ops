# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Finding the losses of the members: the scan of the recent killmails of their characters, and what the pages show.

A character is scanned when its owner may request SRP and has authorized loss reading for it
(``esi-killmails.read_killmails.v1``, a token of the owner's own). The scan reads the list of recent killmails of the
character, looks at the ones it has not seen and stores one ``SrpLoss`` row for each: the character's own losses, kills
(the character was an attacker) and rows older than the claim deadline. It never writes ``SrpKillmail`` rows; a
killmail is stored there only when a member checks it for a claim.

The Celery tasks live in ``fleetops.tasks``; they are queued by the minute beat (``queue_loss_scan``) and need no
beat entry of their own. The limits below are module constants, not settings: a run stops when its time or one of
its ESI budgets is used up and the next run goes on with the characters that are still due.
"""

import random
import time
from dataclasses import dataclass, field
from datetime import timedelta

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger
from django.core.cache import cache
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from esi.models import Token

from fleetops.constants import LOSS_SCOPES
from fleetops.models import SrpClaim, SrpKillmail, SrpLoss, SrpLossScan, SrpPayoutRule, SrpSettings
from fleetops.providers import esi as esi_provider
from fleetops.providers import esi_universe
from fleetops.providers.esi import FleetESIError, LookupBudget
from fleetops.providers.esi_killmails import get_killmail, get_recent_killmails, is_rate_limited
from fleetops.services import universe
from fleetops.services.errors import log_failure
from fleetops.services.permission_users import users_with_any_perm
from fleetops.srp import access, blocks, killmails, presence
from fleetops.srp.types import SrpError

logger = get_extension_logger(__name__)

Kind = SrpLoss.Kind
Status = SrpLossScan.Status

# A run ends after this many seconds, after this many characters, or when one of its ESI budgets is used up: at most
# 400 reads of a list of recent killmails and 600 reads of a killmail (a lookup of a solar system, constellation or
# ship type counts as one read each). With the default of one read per hour and character this carries about 3600
# characters per hour (12 runs of 300).
LOSS_SCAN_TIME_BUDGET = 240
LOSS_SCAN_MAX_CHARACTERS = 300
LOSS_LIST_BUDGET = 400
LOSS_DETAIL_BUDGET = 600
# The pages of the list that are read for one character, and the killmails read in full for one character per run.
MAX_RECENT_PAGES = 5
MAX_FETCH_PER_CHARACTER = 30
# The days losses stay claimable for the list when the settings have no deadline.
DEFAULT_WINDOW_DAYS = 90
# A character without a token or refused by ESI is looked at again after this long.
RETRY_AFTER_NO_ACCESS = timedelta(hours=24)
MAX_RETRY_DELAY = timedelta(hours=24)
RETRY_BASE_MINUTES = 15
MAX_BACKOFF_EXPONENT = 7
# After the error limit or a rate limit of ESI nothing is queued for this long.
BACKOFF_SECONDS = 15 * 60
SCAN_LOCK_KEY = "fleetops:srp:loss-scan"
SCAN_LOCK_SECONDS = 10 * 60
QUEUED_KEY = "fleetops:srp:loss-scan-queued"
QUEUED_SECONDS = 5 * 60
BACKOFF_KEY = "fleetops:srp:loss-scan-backoff"
REFRESH_COOLDOWN_SECONDS = 5 * 60
# The rows of the list of recent losses on My SRP.
RECENT_LOSSES_SHOWN = 50
# How long a kill or a too-old row is kept after it left the window, and a loss after the window.
KEEP_KILLS_DAYS = 1
KEEP_LOSSES_DAYS = 30


class ScanLimited(Exception):
    """ESI's error limit or a rate-limit bucket: the run ends and nothing is queued for a while."""


class OutOfBudget(Exception):
    """The ESI budget of the run is used up: the character stays due for the next run."""


def charlink_url() -> str:
    """The CharLink page, when CharLink is installed, for authorizing all characters at once."""
    try:
        return reverse("charlink:index")
    except NoReverseMatch:
        return ""


def discovery_active(srp) -> bool:
    """Whether losses are found automatically (SRP on and the discovery switch on)."""
    return bool(srp.enabled and srp.loss_discovery_enabled)


def window_floor(srp, now=None):
    """The oldest killmail time that can still be claimed: the claim deadline, else 90 days back."""
    now = now or timezone.now()
    return now - timedelta(days=srp.claim_deadline_days or DEFAULT_WINDOW_DAYS)


def _loss_tokens(user, character_ref):
    """The tokens of ``user`` for a character that carry every loss scope (a subquery on ``character_ref``)."""
    tokens = Token.objects.filter(user=user, character_id=character_ref)
    for scope in LOSS_SCOPES:
        tokens = tokens.filter(scopes__name=scope)
    return tokens


def unauthorized_characters(user, srp=None) -> list[tuple[int, str]]:
    """``[(character_id, name)]`` of the characters of ``user`` that have no token of the user's own for loss reading.

    Empty unless losses are found automatically and the switch for all characters is on. One query, no refresh of
    a token and no ESI call: a character counts as authorized when the user has a token with the scope for it.
    """
    srp = srp or SrpSettings.get_solo()
    if not (discovery_active(srp) and srp.require_all_alts_authorized) or user is None or user.pk is None:
        return []
    rows = (
        CharacterOwnership.objects.filter(user=user)
        .annotate(authorized=Exists(_loss_tokens(user, OuterRef("character__character_id"))))
        .filter(authorized=False)
        .order_by("character__character_name")
        .values_list("character__character_id", "character__character_name")
    )
    return [(character_id, name) for character_id, name in rows]


def character_cards(user) -> list[dict]:
    """The characters of ``user`` for the card on My SRP: whether loss reading is authorized and how the last read
    went. Two queries and no ESI call, no token is refreshed."""
    rows = list(
        CharacterOwnership.objects.filter(user=user)
        .select_related("character")
        .annotate(authorized=Exists(_loss_tokens(user, OuterRef("character__character_id"))))
        .order_by("character__character_name")
    )
    scans = {
        scan.character_id: scan
        for scan in SrpLossScan.objects.filter(character_id__in=[row.character.character_id for row in rows])
    }
    main_pk = getattr(getattr(user, "profile", None), "main_character_id", None)
    cards = []
    for row in rows:
        scan = scans.get(row.character.character_id)
        # A refusal or a missing token noted earlier is out of date once the character has a token again.
        shown = scan is not None and row.authorized and scan.status not in (Status.NO_TOKEN, Status.DENIED)
        cards.append(
            {
                "id": row.character.character_id,
                "name": row.character.character_name,
                "is_main": row.character_id == main_pk,
                "authorized": row.authorized,
                "status": scan.status if shown else "",
                "status_label": scan.get_status_display() if shown and scan.status else "",
                "last_checked_at": scan.last_checked_at if scan is not None else None,
            }
        )
    return cards


def mark_due(character_ids, now=None) -> int:
    """Read these characters in the next run (rows that do not exist yet are due anyway); returns the rows changed."""
    ids = [int(value) for value in character_ids]
    if not ids:
        return 0
    return SrpLossScan.objects.filter(character_id__in=ids).update(next_check_at=now or timezone.now())


# --- The scan ------------------------------------------------------------------------------------------------------


@dataclass
class ScanBudget:
    """What one run may still spend: time, list reads and killmail reads."""

    started: float = field(default_factory=lambda: time.monotonic())
    seconds: float = LOSS_SCAN_TIME_BUDGET
    lists: LookupBudget = field(default_factory=lambda: LookupBudget(LOSS_LIST_BUDGET))
    details: LookupBudget = field(default_factory=lambda: LookupBudget(LOSS_DETAIL_BUDGET))

    def spent(self) -> bool:
        return time.monotonic() - self.started >= self.seconds or self.lists.remaining <= 0 or self.details.remaining <= 0


@dataclass
class ScanResult:
    """What a run did: the characters scanned and why it ended (``done``, ``limited`` or ``budget``)."""

    scanned: int = 0
    ended: str = "done"

    def __str__(self):
        return f"{self.ended}: {self.scanned} characters"


def candidate_characters(user_id=None) -> dict[int, int]:
    """``{character_id: user_id}`` of the characters to scan: all characters of the users who may request SRP and
    that have a loss token of their owner. One query."""
    users = users_with_any_perm([access.REQUEST_SRP]).filter(is_active=True)
    if user_id is not None:
        users = users.filter(pk=user_id)
    rows = (
        CharacterOwnership.objects.filter(user__in=users.values("pk"))
        .annotate(authorized=Exists(_loss_tokens(OuterRef("user_id"), OuterRef("character__character_id"))))
        .filter(authorized=True)
        .values_list("character__character_id", "user_id")
    )
    return dict(rows)


def _ensure_scans(candidates: dict[int, int]) -> None:
    """A scan row for each candidate (new rows are due at once) with the current owner."""
    if not candidates:
        return
    existing = dict(SrpLossScan.objects.filter(character_id__in=list(candidates)).values_list("character_id", "user_id"))
    missing = [SrpLossScan(character_id=cid, user_id=uid) for cid, uid in candidates.items() if cid not in existing]
    if missing:
        SrpLossScan.objects.bulk_create(missing, ignore_conflicts=True)
    for character_id, user_id in existing.items():
        if candidates[character_id] != user_id:
            # The character changed hands: the new owner's token is used from now on.
            SrpLossScan.objects.filter(character_id=character_id).update(user_id=candidates[character_id])


def _delay(failures: int) -> timedelta:
    return min(timedelta(minutes=RETRY_BASE_MINUTES * 2 ** min(failures, MAX_BACKOFF_EXPONENT)), MAX_RETRY_DELAY)


def _finish(scan, status, now, *, next_check_at, failures=0) -> None:
    scan.status = status
    scan.status_at = now
    scan.failures = failures
    scan.next_check_at = next_check_at
    scan.last_checked_at = now
    scan.save(update_fields=["status", "status_at", "failures", "next_check_at", "last_checked_at"])


def _fail(scan, now) -> None:
    failures = min(scan.failures + 1, 60000)
    _finish(scan, Status.ERROR, now, next_check_at=now + _delay(failures), failures=failures)


def _kind_for(victim_character_id, character_id) -> str:
    return Kind.LOSS if victim_character_id == character_id else Kind.KILL


def _row_from_killmail(character_id, km, floor) -> SrpLoss:
    """A row from a stored killmail of the ESI (location included) or from the placeholder of an imported claim."""
    if km.origin == SrpKillmail.Origin.AASRP:
        # A placeholder has no usable ship, place or time; the claim exists, so the page only shows "claimed".
        kind = Kind.OLD if km.killmail_time < floor else _kind_for(km.victim_character_id, character_id)
        return SrpLoss(character_id=character_id, killmail_id=km.pk, kind=kind, killmail_time=km.killmail_time)
    kind = Kind.OLD if km.killmail_time < floor else _kind_for(km.victim_character_id, character_id)
    row = SrpLoss(character_id=character_id, killmail_id=km.pk, killmail_hash=km.killmail_hash, kind=kind, killmail_time=km.killmail_time)
    if kind == Kind.LOSS:
        row.ship_type_id, row.ship_type_name = km.ship_type_id, km.ship_type_name
        row.solar_system_id, row.solar_system_name = km.solar_system_id, km.solar_system_name
        row.ship_group_id, row.solar_system_security = km.ship_group_id, km.solar_system_security
        row.region_id, row.war_id = km.region_id, km.war_id
    return row


def _row_from_detail(character_id, killmail_id, killmail_hash, fields, floor) -> SrpLoss:
    kind = Kind.OLD if fields["killmail_time"] < floor else _kind_for(fields["victim_character_id"], character_id)
    row = SrpLoss(
        character_id=character_id,
        killmail_id=killmail_id,
        killmail_hash=killmail_hash,
        kind=kind,
        killmail_time=fields["killmail_time"],
    )
    if kind == Kind.LOSS:
        ship_name, system_name = killmails._names(fields["ship_type_id"], fields["solar_system_id"])
        row.ship_type_id, row.ship_type_name = fields["ship_type_id"], ship_name
        row.solar_system_id, row.solar_system_name = fields["solar_system_id"], system_name
        row.war_id = fields["war_id"]
    return row


def _add_locations(rows, budget) -> None:
    """The ship group, security status and region of the loss rows that have none: one lookup of the systems and one
    of the ship types for all of them. A lookup that fails leaves the values empty (nothing is refused for it)."""
    pending = [row for row in rows if row.kind == Kind.LOSS and row.ship_group_id is None and row.solar_system_security is None]
    if not pending:
        return
    before = esi_universe.calls_made()
    try:
        systems = universe.system_infos({row.solar_system_id for row in pending})
        groups = universe.type_groups({row.ship_type_id for row in pending})
    except Exception as exc:
        log_failure(logger, "SRP loss location lookup", exc)
        return
    finally:
        budget.details.remaining -= esi_universe.calls_made() - before
    for row in pending:
        info = systems.get(row.solar_system_id)
        if info is not None:
            row.solar_system_security, row.region_id = info.security, info.region_id
        row.ship_group_id = groups.get(row.ship_type_id)


def _add_matches(rows, floor) -> None:
    """The fleet each loss row belongs to by the fleet tracking and the PAP registrations (INT-5)."""
    for row in rows:
        if row.kind != Kind.LOSS or row.killmail_time is None or row.killmail_time < floor:
            continue
        link, code = presence.find_loss_match(row.character_id, row.killmail_time)
        if link is not None:
            row.matched_link, row.match_code = link, code


def _read_character(scan, token, floor, budget) -> bool:
    """Read the recent list of the character and store the rows for the killmails not seen before. Returns whether the
    list was handled to its end (False: a limit of the run stopped it and the character stays due)."""
    character_id = scan.character_id
    rows: list[SrpLoss] = []
    fetched = 0
    complete = True
    try:
        for page in range(1, MAX_RECENT_PAGES + 1):
            if not budget.lists.take():
                raise OutOfBudget
            try:
                listed = get_recent_killmails(token, character_id, page)
            except FleetESIError as exc:
                if page > 1 and exc.status_code in (404, 422):
                    break  # a page the list does not have
                raise
            if not listed:
                break
            ids = [killmail_id for killmail_id, _hash in listed]
            known = set(SrpLoss.objects.filter(character_id=character_id, killmail_id__in=ids).values_list("killmail_id", flat=True))
            stored = {km.pk: km for km in SrpKillmail.objects.filter(pk__in=[value for value in ids if value not in known])}
            # Everything older than the oldest ID of the page was stored by an earlier run only if that ID is known.
            oldest_known = min(ids) in known
            stop = False
            for index, (killmail_id, killmail_hash) in enumerate(listed):
                if killmail_id in known:
                    continue
                km = stored.get(killmail_id)
                if km is not None and (km.origin == SrpKillmail.Origin.AASRP or km.location_checked_at is not None):
                    row = _row_from_killmail(character_id, km, floor)
                else:
                    if fetched >= MAX_FETCH_PER_CHARACTER:
                        complete = False
                        stop = True
                        break
                    if not budget.details.take():
                        raise OutOfBudget
                    fetched += 1
                    try:
                        fields = killmails.killmail_fields(get_killmail(killmail_id, killmail_hash))
                    except SrpError:
                        rows.append(SrpLoss(character_id=character_id, killmail_id=killmail_id, killmail_hash=killmail_hash, kind=Kind.OLD))
                        continue
                    except FleetESIError as exc:
                        if exc.code != "KILLMAIL_NOT_FOUND":
                            raise
                        rows.append(SrpLoss(character_id=character_id, killmail_id=killmail_id, killmail_hash=killmail_hash, kind=Kind.OLD))
                        continue
                    row = _row_from_detail(character_id, killmail_id, killmail_hash, fields, floor)
                rows.append(row)
                if row.kind == Kind.OLD:
                    # Killmail IDs grow with time: every smaller ID is older still and is not read.
                    rows.extend(
                        SrpLoss(character_id=character_id, killmail_id=older_id, killmail_hash=older_hash, kind=Kind.OLD)
                        for older_id, older_hash in listed[index + 1 :]
                        if older_id not in known
                    )
                    stop = True
                    break
            if stop or oldest_known:
                break
            if listed.pages is not None and page >= listed.pages:
                break
    finally:
        _store(rows, floor, budget)
    return complete


def _store(rows, floor, budget) -> None:
    if not rows:
        return
    _add_locations(rows, budget)
    _add_matches(rows, floor)
    SrpLoss.objects.bulk_create(rows, ignore_conflicts=True)


def scan_character(scan, srp, now, budget) -> None:
    """Scan one character and record how it went on its scan row. Raises ``ScanLimited`` and ``OutOfBudget``."""
    if scan.user is None:
        return _finish(scan, Status.NO_TOKEN, now, next_check_at=now + RETRY_AFTER_NO_ACCESS)
    try:
        token = esi_provider.get_scoped_token(scan.user, scan.character_id, LOSS_SCOPES)
    except FleetESIError as exc:
        if exc.code == "MISSING_SCOPE":
            return _finish(scan, Status.NO_TOKEN, now, next_check_at=now + RETRY_AFTER_NO_ACCESS)
        return _fail(scan, now)
    try:
        complete = _read_character(scan, token, window_floor(srp, now), budget)
    except FleetESIError as exc:
        if exc.code == "LOSS_DENIED":
            return _finish(scan, Status.DENIED, now, next_check_at=now + RETRY_AFTER_NO_ACCESS)
        if exc.code == "ESI_LIMITED" or is_rate_limited(exc):
            scan.status, scan.status_at = Status.THROTTLED, now
            scan.save(update_fields=["status", "status_at"])
            raise ScanLimited from exc
        return _fail(scan, now)
    if not complete:
        return  # more killmails than one run reads for a character: it stays due
    interval = timedelta(minutes=srp.loss_discovery_interval_minutes * (1 + random.uniform(0, 0.1)))
    _finish(scan, Status.OK, now, next_check_at=now + interval)


def run_scan(now=None, *, user_id=None) -> ScanResult:
    """Scan the characters that are due, oldest first, within the limits of one run. With ``user_id`` only the
    characters of that user. The caller holds the lock and has checked that the feature is on."""
    now = now or timezone.now()
    srp = SrpSettings.get_solo()
    candidates = candidate_characters(user_id)
    _ensure_scans(candidates)
    due = list(
        SrpLossScan.objects.filter(character_id__in=list(candidates))
        .filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
        .select_related("user")
        .order_by(F("next_check_at").asc(nulls_first=True), "pk")[:LOSS_SCAN_MAX_CHARACTERS]
    )
    budget = ScanBudget()
    result = ScanResult()
    for scan in due:
        if budget.spent():
            result.ended = "budget"
            break
        try:
            scan_character(scan, srp, now, budget)
        except ScanLimited:
            cache.set(BACKOFF_KEY, 1, BACKOFF_SECONDS)
            result.ended = "limited"
            break
        except OutOfBudget:
            result.ended = "budget"
            break
        except Exception as exc:  # one character must not stop the others
            log_failure(logger, "SRP loss scan", exc)
            _fail(scan, now)
        result.scanned += 1
    return result


def queue_loss_scan(now=None) -> bool:
    """Queue ``fleetops.tasks.scan_srp_losses`` from the minute beat, at most every five minutes, when SRP and the
    discovery are on and ESI did not just ask us to slow down. A broker error is logged and ignored."""
    from fleetops import tasks

    try:
        if not discovery_active(SrpSettings.get_solo()) or cache.get(BACKOFF_KEY):
            return False
        if not cache.add(QUEUED_KEY, 1, QUEUED_SECONDS):
            return False
        try:
            tasks.scan_srp_losses.delay()
        except Exception:
            # Without a queued scan the next beat may try again.
            cache.delete(QUEUED_KEY)
            raise
    except Exception as exc:
        log_failure(logger, "Queueing the SRP loss scan", exc)
        return False
    return True


def prune_losses(now=None) -> int:
    """Delete the rows that are no use any more; returns how many. Kills and too-old rows go a day after they left
    the window, losses 30 days after, and scan rows of characters that are no longer on any account."""
    now = now or timezone.now()
    floor = window_floor(SrpSettings.get_solo(), now)
    gone = floor - timedelta(days=KEEP_KILLS_DAYS)
    deleted = SrpLoss.objects.filter(kind__in=(Kind.KILL, Kind.OLD)).filter(
        Q(killmail_time__lt=gone) | Q(killmail_time__isnull=True, discovered_at__lt=gone)
    ).delete()[0]
    deleted += SrpLoss.objects.filter(kind=Kind.LOSS, killmail_time__lt=floor - timedelta(days=KEEP_LOSSES_DAYS)).delete()[0]
    owned = CharacterOwnership.objects.filter(character__character_id=OuterRef("character_id"))
    deleted += SrpLossScan.objects.annotate(owned=Exists(owned)).filter(owned=False).delete()[0]
    return deleted


# --- What the pages show -------------------------------------------------------------------------------------------


def matched_open_link(loss):
    """The open fleet link of the match of a loss; a link that closed since counts as no match."""
    link = loss.matched_link
    return link if link is not None and link.is_open and link.operation_id is not None else None


def recent_losses(user, srp, now=None, *, show_claimed=False, show_ignored=False):
    """The rows of "My recent losses": the losses of the characters the user owns now, inside the claim window, newest
    first (50 at most). By default only losses nobody claimed and the user did not ignore.

    Each row carries ``claimed`` (anyone has a claim for the killmail), ``own_claim_uuid`` and ``own_claim_id`` (the
    user's own claim), ``ignored`` (by this user) and ``block_reasons`` (why the loss is not covered, from the stored
    ship group and place; no lookup and no network call). A fixed number of queries whatever the number of rows.
    """
    now = now or timezone.now()
    owned = {
        ownership.character.character_id: ownership.character.character_name
        for ownership in CharacterOwnership.objects.filter(user=user).select_related("character")
    }
    main_id = getattr(getattr(getattr(user, "profile", None), "main_character", None), "character_id", None)
    claims = SrpClaim.objects.filter(killmail_id=OuterRef("killmail_id"))
    own = claims.filter(user=user)
    queryset = (
        SrpLoss.objects.filter(kind=Kind.LOSS, character_id__in=list(owned), killmail_time__gte=window_floor(srp, now))
        .select_related("matched_link__operation__fleet_type")
        .annotate(
            claimed=Exists(claims),
            own_claim_uuid=Subquery(own.values("uuid")[:1]),
            own_claim_id=Subquery(own.values("pk")[:1]),
        )
        .order_by("-killmail_time", "-killmail_id")
    )
    if not show_claimed:
        queryset = queryset.filter(claimed=False)
    if not show_ignored:
        queryset = queryset.exclude(ignored_by=user)
    rows = list(queryset[:RECENT_LOSSES_SHOWN])
    exempt = {}
    if rows and blocks.has_blocks(srp):
        exempt = {
            rule.ship_type_id: rule
            for rule in SrpPayoutRule.objects.filter(is_active=True, ship_type_id__in={row.ship_type_id for row in rows})
        }
    group_names = blocks.blocked_group_names(rows, srp) if rows else {}
    for row in rows:
        row.character_name = owned.get(row.character_id, "")
        row.is_main = row.character_id == main_id
        row.ignored = row.ignored_by_id == user.pk
        row.open_link = matched_open_link(row)
        row.block_reasons = blocks.submission_blocks(row, srp, rule=exempt.get(row.ship_type_id), group_names=group_names)
        row.days_left = None
        if srp.claim_deadline_days and row.killmail_time is not None:
            left = row.killmail_time + timedelta(days=srp.claim_deadline_days) - now
            row.days_left = max(0, left.days)
    return rows


def owned_loss(user, killmail_id):
    """The found loss with this killmail ID that belongs to a character the user owns now, else None."""
    owned = CharacterOwnership.objects.filter(user=user, character__character_id=OuterRef("character_id"))
    return (
        SrpLoss.objects.filter(killmail_id=killmail_id, kind=Kind.LOSS)
        .annotate(mine=Exists(owned))
        .filter(mine=True)
        .select_related("matched_link__operation__fleet_type")
        .first()
    )
