# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The order of the ships of a doctrine in pings and MOTDs, and the marks between them.

Without a stored order the list is the hulls of the doctrine, each once, in doctrine order, joined with " > ".
An admin can store one order per doctrine: the ships in the order they want and a mark between every two,
``>`` (higher priority), ``=`` (same priority) or ``>>>`` (much higher priority). The order is applied when a
message is rendered, so an open fleet shows a changed order at its next MOTD or ping; the fleet record is not
touched. Ships that left the doctrine are dropped and ships added to it are appended with ``>``.
"""

from fleetops.models import DoctrineShipOrder
from fleetops.services.ingame_text import plain_text

HIGHER = ">"
SAME = "="
MUCH_HIGHER = ">>>"
MARKS = (HIGHER, SAME, MUCH_HIGHER)
# The mark of a ship that has none stored: the plain "A > B" of the automatic order.
DEFAULT_MARK = HIGHER


def hulls(fits) -> list[str]:
    """The hull names of a doctrine's fits, each once, in the order of the fits.

    A fit without a hull name falls back to its type ID, like the fit snapshot always did.
    """
    names = []
    for fit in fits or []:
        if not isinstance(fit, dict):
            continue
        name = plain_text(fit.get("ship") or fit.get("ship_type_id") or "")
        if name and name not in names:
            names.append(name)
    return names


def stored_order(source, external_id):
    """The stored order of a doctrine, or None."""
    if not source or not external_id:
        return None
    return DoctrineShipOrder.objects.filter(doctrine_source=source, doctrine_external_id=str(external_id)).first()


def arrange(current, ships=None, marks=None) -> list[tuple[str, str]]:
    """``[(hull, mark before it)]``: the stored order applied to the hulls ``current`` of the doctrine.

    The mark of the first hull is "". Stored ships that are no longer in ``current`` (or listed twice) are dropped;
    the mark stored in front of a dropped ship goes with it, so the mark in front of the next ship stays. Hulls that
    the order does not know are appended with ``>``. Without ``ships`` this is the automatic order.
    """
    current = list(current)
    known = set(current)
    items: list[list[str]] = []
    placed = set()
    # The admin accepts any JSON; keep only what the editor could have stored (a list of strings).
    ships = [ship for ship in ships if isinstance(ship, str)] if isinstance(ships, list) else []
    marks = [mark for mark in marks if isinstance(mark, str)] if isinstance(marks, list) else []
    for index, ship in enumerate(ships):
        if ship not in known or ship in placed:
            continue
        mark = marks[index - 1] if 0 < index <= len(marks) else DEFAULT_MARK
        items.append([ship, mark if mark in MARKS else DEFAULT_MARK])
        placed.add(ship)
    items.extend([ship, DEFAULT_MARK] for ship in current if ship not in placed)
    if items:
        items[0][1] = ""
    return [(ship, mark) for ship, mark in items]


def ship_line(items) -> str:
    """The text of ``arrange`` items: ``Minokawa = Lif > Raven >>> Basilisk``."""
    parts = []
    for ship, mark in items:
        if parts:
            parts.append(mark or DEFAULT_MARK)
        parts.append(ship)
    return " ".join(parts)


def doctrine_ships(operation) -> str:
    """The ship list of a fleet's doctrine for its messages: every hull once, in the stored order if there is one."""
    current = hulls(getattr(operation, "doctrine_fits", None))
    if not current:
        return ""
    order = stored_order(getattr(operation, "doctrine_source", ""), getattr(operation, "doctrine_external_id", ""))
    if order is None:
        return ship_line(arrange(current))
    return ship_line(arrange(current, order.ships, order.marks))


class OrderError(ValueError):
    """The submitted order is not valid; the text is shown on the editor."""


def clean_submission(ships, marks, current):
    """Check an order submitted by the editor against the hulls ``current``; returns ``(ships, marks)``.

    Raises :class:`OrderError`: a ship that is not in the doctrine, a ship twice, a mark other than the three, or
    not exactly one mark between every two ships.
    """
    ships = [str(ship) for ship in ships]
    marks = [str(mark) for mark in marks]
    if not ships:
        raise OrderError("no ships")
    known = set(current)
    if any(ship not in known for ship in ships):
        raise OrderError("unknown ship")
    if len(set(ships)) != len(ships):
        raise OrderError("duplicate ship")
    if len(marks) != len(ships) - 1:
        raise OrderError("wrong number of marks")
    if any(mark not in MARKS for mark in marks):
        raise OrderError("unknown mark")
    return ships, marks
