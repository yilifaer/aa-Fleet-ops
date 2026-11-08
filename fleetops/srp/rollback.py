# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The checks before going back to an earlier FleetOps version (``migrate fleetops 0023``), read only.

Migrations 0036 and 0035 (0037 only adds an index and is undone first) run the same checks when going back and stop before anything is changed; their copies are
written into the migrations, because a migration may not import this code. ``RollbackGuardFunctionTests`` in
``tests/test_migration_srp.py`` keep the two equal. Only rule IDs, link codes and counts are reported: no names, no amounts.
"""

from dataclasses import dataclass, field

from django.db.models import Count, Q

from fleetops.models import SrpClaim, SrpFleetLink, SrpPayoutRule

MAX_LISTED = 50

RULES = "rules"
LINKS = "links"
CLAIMS = "claims"


@dataclass(slots=True)
class Problem:
    """One reason going back is not safe: ``code`` is ``rules``, ``links`` or ``claims``."""

    code: str
    count: int
    listed: list = field(default_factory=list)


def check_rules() -> Problem | None:
    """Payout rules that belong to a category (or keep a category key from a write that skipped ``save()``)."""
    rules = SrpPayoutRule.objects.filter(Q(category__isnull=False) | ~Q(category_key=0))
    count = rules.count()
    if not count:
        return None
    return Problem(RULES, count, list(rules.order_by("pk").values_list("pk", flat=True)[:MAX_LISTED]))


def check_links() -> Problem | None:
    """Links without a fleet that have claims (standing links), and every link imported from aa-srp."""
    links = SrpFleetLink.objects.annotate(claim_count=Count("claims")).filter(
        Q(operation__isnull=True, claim_count__gt=0) | Q(origin=SrpFleetLink.Origin.AASRP)
    )
    count = links.count()
    if not count:
        return None
    return Problem(LINKS, count, list(links.order_by("pk").values_list("code", flat=True)[:MAX_LISTED]))


def check_claims() -> Problem | None:
    """Claims imported from aa-srp."""
    count = SrpClaim.objects.filter(source=SrpClaim.Source.AASRP).count()
    return Problem(CLAIMS, count) if count else None


def check_all() -> list[Problem]:
    """Every reason going back is not safe; an empty list means it is. Writes nothing."""
    return [problem for problem in (check_rules(), check_links(), check_claims()) if problem is not None]


def _listed(problem: Problem) -> str:
    text = ", ".join(str(value) for value in problem.listed)
    if problem.count > len(problem.listed):
        text += f" and {problem.count - len(problem.listed)} more, {problem.count} in total"
    return text


def describe(problem: Problem) -> str:
    """What is in the way and what to do about it, in one line."""
    if problem.code == RULES:
        return (
            f"Payout rules that belong to a category (IDs: {_listed(problem)}): delete them or change them into "
            "general rules without a category."
        )
    if problem.code == LINKS:
        return (
            f"SRP links without a fleet that have claims, or that were imported from aa-srp (codes: {_listed(problem)}): "
            "for imported links run fleetops_import_aasrp --undo; delete the other links (their claims stay, without a "
            "link) or restore the database from before the upgrade."
        )
    return f"{problem.count} claims were imported from aa-srp. Run fleetops_import_aasrp --undo first."
