# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Messages that are stored in English and translated when they are shown.

Text that code writes for people to read later (step results, end reasons, ESI errors, job results) is
stored in English, because it is written outside the request of whoever reads it. Each message is declared
here with :class:`StoredText`. Calling a declared message builds the English text for the database; when a
page shows that text, :func:`translate_stored` recognises it again and returns it in the language of the
viewer. Text that matches no declared message is returned unchanged.

Declaring and using a message::

    READ_MEMBERS_FAILED = StoredText(gettext_noop("Could not read fleet members: %(detail)s"), nested=("detail",))

    text = READ_MEMBERS_FAILED(detail=ESI_HTTP(status=503, reason="Service Unavailable"))

Rules for a message:

* The msgid is wrapped in ``gettext_noop`` so that ``makemessages`` finds it. It uses named ``%(name)s``
  placeholders only, and a literal percent sign is written ``%%``.
* The fixed words around the placeholders identify the message. When several messages fit a text, the one
  with the most fixed characters wins, so a general message never hides a specific one.
* A parameter listed in ``nested`` holds another stored message (a step name, the description of an ESI
  failure) and is translated on its own. Names, ESI text and numbers are plain parameters.
* Parameters carry what varies, never a sentence assembled from translated fragments.
* A message whose fixed text has no letters at all (``"%(name)s: %(error)s"``) only counts as that message when
  one of its nested parameters is a declared message itself, so ordinary text with a colon is left alone.
* Several declared messages in a row, joined by a space after a full stop or by ``"; "``, are translated one by
  one (``" ".join(errors)``, a note appended to a result, a list of skipped names).
* A count in a message is written as a pair, ``StoredPlural(singular, plural)``, so both English wordings stay.
* A placeholder that only ever holds a whole number is listed in ``numbers``: it matches digits only and calling the
  message with anything else raises ``TypeError``. A message whose fixed words end like another message ("%(label)s:
  %(done)s of %(total)s done.") is declared with ``priority=True``, which needs at least one number and makes it the
  first message tried; the digits keep such a message from claiming text that only ends alike.

Pages also pass text that a person typed (a withdraw reason, a note) through :func:`translate_stored`, so the
search is bounded: a text longer than ``MAX_TEXT_LENGTH`` is not searched, messages inside messages are
followed ``MAX_NESTING`` levels deep, and a search that costs more than ``MAX_LOOKUPS`` gives up. Such a text
is shown as written.

``STEP_LABELS`` and ``FLEET_ROLE_LABELS`` are not stored text: they are lazy labels for the codes that are
stored (the step of a fleet, a position in a fleet). :func:`outgoing_language` and :func:`user_language`
choose the language of text that is sent out rather than shown: what goes to Discord or into the game is always
English, whatever the site or the viewer language is.
"""

import contextlib
import functools
import re
import threading

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils import translation
from django.utils.translation import gettext, gettext_lazy, gettext_noop, pgettext_lazy

_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_DIGITS = re.compile(r"[0-9]+")
# What a placeholder matches in a stored text: digits only for a number, else as little as fits.
_NUMBER = r"\d+"
_ANYTHING = r".*?"
# Where one message ends and the next begins in a text that joins several: a space after a full stop
# (or another sentence end), or the "; " of a list.
_SEPARATOR = re.compile(r"(?<=[.!?]) |; ")
# No stored message comes close to this length. A longer text (a pasted report, a long reason) is shown as it is
# and never searched for messages, so text that a person wrote cannot make a page slow.
MAX_TEXT_LENGTH = 4000
# How many levels of messages inside messages are followed. Stored messages nest two or three deep; a text
# that nests further only imitates them.
MAX_NESTING = 5
# How much searching one text may cost before the search gives up and the text is shown as it is. Trying a text
# against the declared messages costs one unit, and one more for every 512 characters of the text.
MAX_LOOKUPS = 1500
_ENTRIES = []
_CONSTANTS = {}
_PATTERNS = []


def _split(msgid):
    """The literal pieces of ``msgid`` (percent signs unescaped) and the names of its placeholders."""
    pieces, names, position = [], [], 0
    for found in _PLACEHOLDER.finditer(msgid):
        pieces.append(msgid[position : found.start()])
        names.append(found.group(1))
        position = found.end()
    pieces.append(msgid[position:])
    for piece in pieces:
        if "%" in piece.replace("%%", ""):
            raise ValueError(f"A stored text only uses %(name)s placeholders and %%: {msgid!r}")
    return [piece.replace("%%", "%") for piece in pieces], names


def _is_whole_number(value):
    """Whether ``value`` can fill a number placeholder: an integer or its digits (a flag is no number)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, str) and _DIGITS.fullmatch(value) is not None)


class StoredText:
    """One message: calling it returns the English text to store, and the registry finds it again."""

    def __init__(self, msgid, *, nested=(), numbers=(), priority=False):
        literals, names = _split(msgid)
        unknown = set(nested) - set(names)
        if unknown:
            raise ValueError(f"nested names {sorted(unknown)} are not placeholders of {msgid!r}")
        unknown = set(numbers) - set(names)
        if unknown:
            raise ValueError(f"number names {sorted(unknown)} are not placeholders of {msgid!r}")
        if set(numbers) & set(nested):
            raise ValueError(f"A placeholder cannot be a number and a nested message: {msgid!r}")
        if priority and not numbers:
            raise ValueError(f"A message with priority needs a number placeholder: {msgid!r}")
        weight = sum(len(piece) for piece in literals)
        if names and not weight:
            raise ValueError(f"A stored text needs words around its placeholders: {msgid!r}")
        if msgid in {entry.msgid for entry in _ENTRIES}:
            raise ValueError(f"Stored text declared twice: {msgid!r}")
        self.msgid = msgid
        self.literals = tuple(literals)
        self.params = tuple(dict.fromkeys(names))
        self.nested = tuple(nested)
        # Placeholders that hold whole numbers, and whether the message is tried before every other one.
        self.numbers = tuple(dict.fromkeys(numbers))
        self.priority = bool(priority)
        # How specific the message is: the number of characters that do not vary.
        self.weight = weight
        # Fixed text without a single letter or digit ("%(name)s: %(error)s") fits far too much text to be
        # recognised on its own; it needs a nested message to prove the text is this message.
        self.generic = bool(names) and not any(char.isalnum() for piece in literals for char in piece)
        if self.generic and not self.nested:
            raise ValueError(f"A stored text without words needs a nested parameter: {msgid!r}")
        self.pattern = None
        # The first and last words of a message with parameters; most other texts are ruled out by them alone.
        self.first, self.last = literals[0], literals[-1]
        if names:
            parts, seen = [re.escape(literals[0])], set()
            for name, literal in zip(names, literals[1:]):
                if name in seen:
                    parts.append(f"(?P={name})")
                else:
                    parts.append(f"(?P<{name}>{_NUMBER if name in self.numbers else _ANYTHING})")
                seen.add(name)
                parts.append(re.escape(literal))
            self.pattern = re.compile("".join(parts), re.DOTALL)
            _PATTERNS.append(self)
            # A message with priority comes first. Of the others the most specific message wins when several
            # fit; equal weights keep their declaration order.
            _PATTERNS.sort(key=lambda entry: (not entry.priority, -entry.weight))
        else:
            _CONSTANTS[msgid % {}] = self
        _ENTRIES.append(self)
        # A text looked up before this message existed may be this message now.
        clear_cache()

    def __call__(self, **params):
        """The English text with ``params`` filled in."""
        if set(params) != set(self.params):
            raise TypeError(f"{self.msgid!r} takes {sorted(self.params)}, got {sorted(params)}")
        for name in self.numbers:
            if not _is_whole_number(params[name]):
                raise TypeError(f"{name} of {self.msgid!r} is a whole number, got {params[name]!r}")
        return self.msgid % {name: str(value) for name, value in params.items()}

    def __str__(self):
        return self.msgid

    def __repr__(self):
        return f"StoredText({self.msgid!r})"

    def match(self, text):
        """The parameters if ``text`` is this message, else None (a constant message has no parameters)."""
        if self.pattern is None:
            return {} if text == self.msgid % {} else None
        # The expression is only tried on a text that has all fixed pieces in order. Then the first place each
        # piece appears is the one that matches, so the lazy groups never have to backtrack.
        if not self._has_pieces(text):
            return None
        found = self.pattern.fullmatch(text)
        return None if found is None else found.groupdict()

    def _has_pieces(self, text):
        """Whether the fixed pieces of the message follow each other in ``text``, the last one at the end."""
        if len(text) < self.weight or not text.startswith(self.first) or not text.endswith(self.last):
            return False
        position, end = len(self.first), len(text) - len(self.last)
        for piece in self.literals[1:-1]:
            position = text.find(piece, position, end)
            if position < 0:
                return False
            position += len(piece)
        return position <= end


class StoredPlural:
    """A message in two wordings, chosen by a count: one for exactly 1, the other for every other number.

    Both wordings are declared messages of their own, so the English text stays as it reads and a catalog
    translates each. ``count`` names the placeholder that holds the number; the singular wording may leave it
    out ("Move its member first.").
    """

    def __init__(self, singular, plural, *, count="count", nested=(), numbers=(), priority=False):
        self.count = count

        def wording(msgid):
            # The singular may leave a number out, and a wording without a number cannot have priority.
            present = [name for name in numbers if name in _split(msgid)[1]]
            return StoredText(msgid, nested=nested, numbers=present, priority=priority and bool(present))

        self.singular = wording(singular)
        self.plural = wording(plural)
        if count not in self.plural.params:
            raise ValueError(f"The plural wording needs %({count})s: {plural!r}")

    def __call__(self, **params):
        """The wording for ``params[count]``, filled in."""
        unknown = set(params) - set(self.singular.params) - set(self.plural.params)
        if self.count not in params or unknown:
            raise TypeError(f"{self!r} takes {sorted(self.plural.params)}, got {sorted(params)}")
        if self.count in self.plural.numbers and not _is_whole_number(params[self.count]):
            raise TypeError(f"{self.count} of {self.plural.msgid!r} is a whole number, got {params[self.count]!r}")
        entry = self.singular if int(params[self.count]) == 1 else self.plural
        return entry(**{name: value for name, value in params.items() if name in entry.params})

    def __repr__(self):
        return f"StoredPlural({self.singular.msgid!r}, {self.plural.msgid!r})"


_UNNAMED_CHARACTER = re.compile(r"character (\d+)")


def shown_name(label):
    """A character label as it reads in the active language.

    A character that no name is known for is stored as ``character 123``; that label has its own wording in
    the catalog. A name, and any other text, is returned as it is.
    """
    found = _UNNAMED_CHARACTER.fullmatch(label) if isinstance(label, str) else None
    return gettext("character %(id)s") % {"id": found.group(1)} if found else label


def entries():
    """Every declared message, in declaration order."""
    return tuple(_ENTRIES)


class _OutOfLookups(Exception):
    """The search for one text used up its lookups (see ``MAX_LOOKUPS``)."""


_lookups = threading.local()


def _spend(text):
    """Count a search through the declared messages for ``text``; stop when one text has used too much."""
    left = getattr(_lookups, "left", None)
    if left is not None:
        if left <= 0:
            raise _OutOfLookups
        _lookups.left = left - 1 - len(text) // 512


@contextlib.contextmanager
def _limited():
    """Give the search for one text a limited number of lookups (a search inside another shares its limit)."""
    if getattr(_lookups, "left", None) is not None:
        yield
        return
    _lookups.left = MAX_LOOKUPS
    try:
        yield
    finally:
        _lookups.left = None


@functools.lru_cache(maxsize=1024)
def _find(text, depth):
    # The result is shared by every caller and must not be changed.
    if len(text) > MAX_TEXT_LENGTH:
        return None
    entry = _CONSTANTS.get(text)
    if entry is not None:
        return entry, {}
    _spend(text)
    for entry in _PATTERNS:
        if not text.startswith(entry.first) or not text.endswith(entry.last):
            continue
        params = entry.match(text)
        if params is None:
            continue
        if entry.generic and not any(_registered(params[name], True, depth - 1) for name in entry.nested):
            continue
        return entry, params
    return None


@functools.lru_cache(maxsize=1024)
def _single(text, depth):
    """Whether ``text`` is exactly one declared message whose nested messages are declared too."""
    found = _find(text, depth)
    if found is None:
        return False
    entry, params = found
    return all(_registered(params[name], True, depth - 1) for name in entry.nested)


@functools.lru_cache(maxsize=1024)
def _registered(text, deep, depth):
    if depth < 0:
        return False
    found = _find(text, depth)
    if found is not None:
        entry, params = found
        if not deep or all(_registered(params[name], True, depth - 1) for name in entry.nested):
            return True
    return _series(text, depth) is not None


def _series(text, depth):
    if not text or len(text) > MAX_TEXT_LENGTH or _SEPARATOR.search(text) is None:
        return None
    return _split_series(text, depth)


def _split_series(text, depth):
    """``(message, separator, message, ...)`` when ``text`` is declared messages in a row, else None."""
    cuts = list(_SEPARATOR.finditer(text))
    starts = [0] + [cut.end() for cut in cuts]
    # next_cut[k]: where the first message of text[starts[k]:] ends, None when that text is one message
    # (or nothing fits). It is worked out from the end, so a text is never split twice.
    next_cut = [None] * len(starts)
    fits = [False] * len(starts)
    for k in range(len(cuts), -1, -1):
        rest = text[starts[k] :]
        if k and _single(rest, depth):
            fits[k] = True
            continue
        for j in range(k, len(cuts)):
            if fits[j + 1] and _single(text[starts[k] : cuts[j].start()], depth):
                fits[k], next_cut[k] = True, j
                break
    if not fits[0]:
        return None
    parts, k = [], 0
    while next_cut[k] is not None:
        j = next_cut[k]
        parts += [text[starts[k] : cuts[j].start()], cuts[j].group()]
        k = j + 1
    parts.append(text[starts[k] :])
    return tuple(parts)


def find(text):
    """The declared message that ``text`` is, with its parameters, or None.

    A text made of several messages (see ``split_series``) is not one message; ``registered`` knows both.
    """
    try:
        with _limited():
            found = _find(text, MAX_NESTING)
    except _OutOfLookups:
        return None
    return None if found is None else (found[0], dict(found[1]))


def split_series(text):
    """The declared messages in ``text`` when it joins several, else None.

    Messages follow each other after a full stop and a space (``"One. Two."``) or are listed with ``"; "``.
    The result alternates message and separator: ``("One.", " ", "Two.")``.
    """
    try:
        with _limited():
            return _series(text, MAX_NESTING)
    except _OutOfLookups:
        return None


def registered(text, *, deep=True):
    """Whether ``text`` is a declared message; with ``deep`` its nested parameters have to be declared too.

    Declared messages in a row (``split_series``) count as declared.
    """
    text = "" if text is None else str(text)
    try:
        with _limited():
            return _registered(text, deep, MAX_NESTING)
    except _OutOfLookups:
        return False


def translate_stored(text):
    """``text`` in the active language when it is a declared message, else unchanged."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text or len(text) > MAX_TEXT_LENGTH:
        return text
    return _translate(translation.get_language() or "", text)


def clear_cache():
    """Forget translated texts (after the catalogs or the active language rules change)."""
    for cached in (_translate, _find, _single, _registered):
        cached.cache_clear()


@receiver(setting_changed)
def _settings_changed(sender, setting, **kwargs):
    if setting in {"LANGUAGE_CODE", "LANGUAGES", "LOCALE_PATHS", "INSTALLED_APPS", "USE_I18N"}:
        clear_cache()


def _render(entry, params, fallback):
    translated = gettext(entry.msgid)
    if not entry.params:
        # A message without parameters is not a format string; only an escaped percent sign needs undoing.
        return translated.replace("%%", "%") if "%%" in entry.msgid else translated
    try:
        return translated % params
    except (KeyError, ValueError, TypeError):
        # A catalog entry with wrong placeholders must not break the page.
        return fallback


@functools.lru_cache(maxsize=4096)
def _translate(language, text):
    # ``language`` only keys the cache; the translation itself follows the active language.
    try:
        with _limited():
            return _translated(language, text, MAX_NESTING)
    except (_OutOfLookups, RecursionError):
        # Text that only imitates a message is shown as written, and stays so in the cache.
        return text


def _translated(language, text, depth):
    entry = _CONSTANTS.get(text)
    if entry is not None:
        return _render(entry, {}, text)
    if depth < 0:
        return text
    found = _find(text, depth)
    # A message with priority that fits the whole text with single messages inside is that message, even when the
    # text could also be cut into messages after a full stop ("...handed over to Pilot A. PAP: 2 granted at 1x.").
    if found is None or not (
        found[0].priority and all(_single(found[1][name], depth - 1) for name in found[0].nested)
    ):
        series = _series(text, depth)
        if series is not None:
            return "".join(
                _translated(language, part, depth) if number % 2 == 0 else part for number, part in enumerate(series)
            )
    if found is None:
        return text
    entry, stored = found
    params = {name: shown_name(value) for name, value in stored.items()}
    for name in entry.nested:
        params[name] = _translated(language, stored[name], depth - 1)
    return _render(entry, params, text)


def outgoing_language():
    """Context manager for text that goes to Discord or the game: always English.

    Whatever the site or the viewer language is: pings and MOTDs are read by many people, so they follow neither
    the language of whoever clicked nor the site language. FleetOps's own words in them are plain English; text
    that admins write in a template is sent as written.
    """
    return translation.override("en")


def stored_language():
    """Context manager for text that is written to the database: always English.

    Helpers that return labels in the language of the viewer are called inside it when their result goes
    into a stored message.
    """
    return translation.override("en")


def user_language(user):
    """The Alliance Auth language of ``user``, else the site language."""
    return getattr(getattr(user, "profile", None), "language", "") or settings.LANGUAGE_CODE


# Step names inside messages ("<step> failed unexpectedly (...)"). They are nested parameters.
STEP_NAMES = {
    "Cancellation notice": StoredText(gettext_noop("Cancellation notice")),
    "Capsule kick": StoredText(gettext_noop("Capsule kick")),
    "Creating a squad": StoredText(gettext_noop("Creating a squad")),
    "Creating a wing": StoredText(gettext_noop("Creating a wing")),
    "Deleting a squad": StoredText(gettext_noop("Deleting a squad")),
    "Deleting a wing": StoredText(gettext_noop("Deleting a wing")),
    "Inviting a character": StoredText(gettext_noop("Inviting a character")),
    "Kick outside system": StoredText(gettext_noop("Kick outside system")),
    "Moving a fleet member": StoredText(gettext_noop("Moving a fleet member")),
    "Renaming a squad": StoredText(gettext_noop("Renaming a squad")),
    "Renaming a wing": StoredText(gettext_noop("Renaming a wing")),
    "Renaming the squad": StoredText(gettext_noop("Renaming the squad")),
    "Renaming the wing": StoredText(gettext_noop("Renaming the wing")),
    "Discord ping": StoredText(gettext_noop("Discord ping")),
    "ESI tracking": StoredText(gettext_noop("ESI tracking")),
    "Final fleet sync": StoredText(gettext_noop("Final fleet sync")),
    "Fleet boss check": StoredText(gettext_noop("Fleet boss check")),
    "Fleet end": StoredText(gettext_noop("Fleet end")),
    "Fleet kick": StoredText(gettext_noop("Fleet kick")),
    "Fleet refresh": StoredText(gettext_noop("Fleet refresh")),
    "Fleet start": StoredText(gettext_noop("Fleet start")),
    "Fleet sync after kicks": StoredText(gettext_noop("Fleet sync after kicks")),
    "Fleet tracking": StoredText(gettext_noop("Fleet tracking")),
    "Manual fleet creation": StoredText(gettext_noop("Manual fleet creation")),
    "Message check": StoredText(gettext_noop("Message check")),
    "Message preview": StoredText(gettext_noop("Message preview")),
    "Message rendering": StoredText(gettext_noop("Message rendering")),
    "MOTD update": StoredText(gettext_noop("MOTD update")),
    "Name lookup": StoredText(gettext_noop("Name lookup")),
    "Name update": StoredText(gettext_noop("Name update")),
    "PAP link token check": StoredText(gettext_noop("PAP link token check")),
    "Pre ping": StoredText(gettext_noop("Pre ping")),
    "Queueing a fleet job": StoredText(gettext_noop("Queueing a fleet job")),
    "Queueing a fleet refresh": StoredText(gettext_noop("Queueing a fleet refresh")),
    "Queueing the fleet end": StoredText(gettext_noop("Queueing the fleet end")),
    "Queueing the name lookup": StoredText(gettext_noop("Queueing the name lookup")),
    "Queueing the SRP value sweep": StoredText(gettext_noop("Queueing the SRP value sweep")),
    "Reading the fleet wings": StoredText(gettext_noop("Reading the fleet wings")),
    "Saving the last used start settings": StoredText(gettext_noop("Saving the last used start settings")),
    "Scheduled fleet start": StoredText(gettext_noop("Scheduled fleet start")),
    "SRP claim update after a loss value lookup": StoredText(
        gettext_noop("SRP claim update after a loss value lookup")
    ),
    "SRP creation": StoredText(gettext_noop("SRP creation")),
    "SRP killmail cleanup": StoredText(gettext_noop("SRP killmail cleanup")),
    "SRP killmail name lookup": StoredText(gettext_noop("SRP killmail name lookup")),
    "SRP loss value lookup": StoredText(gettext_noop("SRP loss value lookup")),
    "SRP value sweep": StoredText(gettext_noop("SRP value sweep")),
}

# How a fleet is treated when a step is skipped on purpose ("<mode>: <step> intentionally skipped.").
MODE_MANUAL_FLEET = StoredText(gettext_noop("Manual fleet"))
MODE_PAP_ONLY = StoredText(gettext_noop("PAP-only mode"))
STEP_SKIPPED = StoredText(gettext_noop("%(mode)s: %(step)s intentionally skipped."), nested=("mode", "step"))
STEP_FAILED = StoredText(gettext_noop("%(step)s failed unexpectedly (%(error_type)s)."), nested=("step",))

# Messages of the start, ping, MOTD and SRP steps.
NOT_RENDERED = StoredText(gettext_noop("Not sent because the fleet messages could not be rendered."))
EMPTY_PING = StoredText(gettext_noop("Not sent because the ping text is empty."))
EMPTY_MOTD = StoredText(gettext_noop("Not sent because the MOTD text is empty."))
NO_PING_CHANNEL = StoredText(gettext_noop("No Discord channel selected; the ping is available to copy."))
PING_NOT_REQUESTED = StoredText(gettext_noop("Form-up ping not requested."))
PING_CHANNEL_DISABLED = StoredText(gettext_noop("Ping channel %(name)s is disabled."))
SRP_AUTO_DISABLED = StoredText(gettext_noop("Automatic SRP link creation is disabled."))
START_ABANDONED = StoredText(
    gettext_noop("The fleet start did not finish, so this operation was cancelled. Start the fleet again to track it.")
)
NOT_OWNED = StoredText(gettext_noop("Selected character is not owned by the current Alliance Auth user."))
NOT_FLEET_BOSS = StoredText(gettext_noop("%(character)s is not the fleet boss. The current boss is %(boss)s."))
NO_ESI_FLEET = StoredText(gettext_noop("Operation has no ESI fleet ID."))
FLEET_CANCELLED = StoredText(gettext_noop("Scheduled fleet cancelled."))

# Manual fleets and PAP-link fleets.
MANUAL_FLEET_CREATED = StoredText(gettext_noop("Manual fleet record created without ESI tracking."))
MANUAL_NO_PING = StoredText(gettext_noop("Manual fleet: no Discord ping sent."))
MANUAL_NO_MOTD = StoredText(gettext_noop("Manual fleet: no MOTD update attempted."))
MANUAL_NO_TRACKING = StoredText(gettext_noop("Manual fleet: no ESI tracking started."))
MANUAL_SRP = StoredText(gettext_noop("Manual fleet: SRP can be linked/retried manually if needed."))
LINK_FLEET_STARTED = StoredText(gettext_noop("PAP-link fleet started without an ESI fleet."))
LINK_STEP_UNUSED = StoredText(gettext_noop("PAP-link fleet: %(step)s not used."), nested=("step",))

# How the end of a fleet is shown.
END_MANUAL = StoredText(gettext_noop("Ended manually"))
END_FLEET_GONE = StoredText(gettext_noop("Ended automatically: the fleet no longer exists or the FC left it"))
END_BOSS_HANDOVER = StoredText(gettext_noop("Ended automatically: fleet boss handed over to %(name)s"))

# ESI failures: the description of the answer, then the call that failed.
ESI_HTTP = StoredText(gettext_noop("ESI returned HTTP %(status)s (%(reason)s)."))
ESI_ERROR_LIMIT = StoredText(gettext_noop("ESI error limit reached."))
ESI_ERROR_LIMIT_RESET = StoredText(gettext_noop("ESI error limit reached; try again in %(seconds)s seconds."))
ESI_RATE_LIMIT = StoredText(gettext_noop("ESI rate limit reached; try again later."))
ESI_TIMEOUT = StoredText(gettext_noop("ESI did not respond in time."))
ESI_CONNECTION = StoredText(gettext_noop("Could not connect to ESI."))
ESI_UNEXPECTED = StoredText(gettext_noop("Unexpected ESI error (%(error_type)s)."))
ESI_NO_TOKEN = StoredText(
    gettext_noop("No valid ESI token with the required fleet scopes was found for this character.")
)
ESI_TOKEN_REFRESH_FAILED = StoredText(
    gettext_noop("The ESI token of the character could not be refreshed (%(error_type)s). Try again in a moment.")
)
NOT_IN_FLEET = StoredText(gettext_noop("Character is currently not in a fleet."))
DETECT_FAILED = StoredText(gettext_noop("Could not detect fleet: %(detail)s"), nested=("detail",))
DETECT_DENIED = StoredText(gettext_noop("ESI denied access to this character's fleet: %(detail)s"), nested=("detail",))
READ_FLEET_FAILED = StoredText(gettext_noop("Could not read fleet information: %(detail)s"), nested=("detail",))
READ_MEMBERS_FAILED = StoredText(gettext_noop("Could not read fleet members: %(detail)s"), nested=("detail",))
MOTD_DENIED = StoredText(gettext_noop("ESI denied permission to update the fleet MOTD: %(detail)s"), nested=("detail",))
MOTD_FAILED = StoredText(gettext_noop("Could not update fleet MOTD: %(detail)s"), nested=("detail",))

# More ESI failures of the fleet routes: what failed, then the description of the answer.
READ_WINGS_FAILED = StoredText(gettext_noop("Could not read fleet wings: %(detail)s"), nested=("detail",))
KICK_NOT_FOUND = StoredText(gettext_noop("Fleet member %(member)s was not found: %(detail)s"), nested=("detail",))
KICK_DENIED = StoredText(
    gettext_noop("ESI denied permission to kick this fleet member: %(detail)s"), nested=("detail",)
)
KICK_ESI_FAILED = StoredText(gettext_noop("Could not kick fleet member %(member)s: %(detail)s"), nested=("detail",))
WING_CREATE_FAILED = StoredText(gettext_noop("Could not create a wing: %(detail)s"), nested=("detail",))
WING_NO_ID = StoredText(gettext_noop("Could not create a wing: ESI did not return the new wing id."))
WING_RENAME_FAILED = StoredText(gettext_noop("Could not rename the wing: %(detail)s"), nested=("detail",))
WING_DELETE_FAILED = StoredText(gettext_noop("Could not delete the wing: %(detail)s"), nested=("detail",))
SQUAD_CREATE_FAILED = StoredText(gettext_noop("Could not create a squad: %(detail)s"), nested=("detail",))
SQUAD_NO_ID = StoredText(gettext_noop("Could not create a squad: ESI did not return the new squad id."))
SQUAD_RENAME_FAILED = StoredText(gettext_noop("Could not rename the squad: %(detail)s"), nested=("detail",))
SQUAD_DELETE_FAILED = StoredText(gettext_noop("Could not delete the squad: %(detail)s"), nested=("detail",))
MOVE_FAILED = StoredText(gettext_noop("Could not move %(name)s: %(detail)s"), nested=("detail",))
MOVE_CHARACTER_FAILED = StoredText(
    gettext_noop("Could not move character %(member)s: %(detail)s"), nested=("detail",)
)
INVITE_FAILED = StoredText(gettext_noop("Could not invite %(name)s: %(detail)s"), nested=("detail",))
INVITE_CHARACTER_FAILED = StoredText(
    gettext_noop("Could not invite character %(member)s: %(detail)s"), nested=("detail",)
)
ONLINE_DENIED = StoredText(gettext_noop("ESI denied access to the online status: %(detail)s"), nested=("detail",))
ONLINE_FAILED = StoredText(gettext_noop("Could not read the online status: %(detail)s"), nested=("detail",))
LOCATION_DENIED = StoredText(gettext_noop("ESI denied access to the location: %(detail)s"), nested=("detail",))
LOCATION_FAILED = StoredText(gettext_noop("Could not read the location: %(detail)s"), nested=("detail",))
SHIP_DENIED = StoredText(gettext_noop("ESI denied access to the current ship: %(detail)s"), nested=("detail",))
SHIP_FAILED = StoredText(gettext_noop("Could not read the current ship: %(detail)s"), nested=("detail",))
NAMES_LOOKUP_FAILED = StoredText(gettext_noop("Could not look up names: %(detail)s"), nested=("detail",))
AFFILIATIONS_LOOKUP_FAILED = StoredText(
    gettext_noop("Could not look up character affiliations: %(detail)s"), nested=("detail",)
)
CHARACTER_NAMES_LOOKUP_FAILED = StoredText(
    gettext_noop("Could not look up character names: %(detail)s"), nested=("detail",)
)
ITEM_NAMES_LOOKUP_FAILED = StoredText(gettext_noop("Could not look up item names: %(detail)s"), nested=("detail",))

# Killmails and market prices (public ESI reads for SRP).
KILLMAIL_NOT_FOUND = StoredText(gettext_noop("Killmail not found. Check the link or the hash."))
KILLMAIL_READ_FAILED = StoredText(gettext_noop("Could not read the killmail: %(detail)s"), nested=("detail",))
KILLMAIL_EMPTY = StoredText(gettext_noop("Could not read the killmail: ESI returned an empty answer."))
MARKET_READ_FAILED = StoredText(gettext_noop("Could not read market prices: %(detail)s"), nested=("detail",))
ESI_ERROR_LIMIT_MINUTE = StoredText(gettext_noop("ESI error limit reached; try again in a minute."))
ESI_HTTP_PLAIN = StoredText(gettext_noop("ESI returned HTTP %(status)s."))

# zKillboard answers.
ZKILL_MISSING = StoredText(
    gettext_noop("zKillboard does not have this killmail yet (new killmails appear after about 5 minutes).")
)
ZKILL_TOO_RECENT = StoredText(
    gettext_noop("This killmail is too recent for zKillboard; its value is looked up a few minutes later.")
)
ZKILL_UNAVAILABLE = StoredText(gettext_noop("zKillboard is not available at the moment."))
ZKILL_BUSY = StoredText(gettext_noop("zKillboard is busy, try again in a few seconds."))
ZKILL_BLOCKED = StoredText(gettext_noop("zKillboard is refusing requests at the moment."))

# Posting to a Discord webhook.
PING_NO_WEBHOOK = StoredText(gettext_noop("No Discord webhook is configured for this ping channel."))
PING_NOT_A_WEBHOOK = StoredText(
    gettext_noop("The configured webhook URL is not a Discord webhook URL, so nothing was sent.")
)
PING_TOO_LONG = StoredText(
    gettext_noop(
        "The ping is %(length)s characters; Discord accepts at most %(limit)s. Shorten the additional information."
    )
)
PING_BAD_WEBHOOK = StoredText(gettext_noop("The configured Discord webhook URL is invalid."))
PING_REQUEST_FAILED = StoredText(gettext_noop("Discord webhook request failed (%(error_type)s)."))
PING_HTTP = StoredText(gettext_noop("Discord returned HTTP %(status)s."))

# SRP links created for a fleet by the SRP providers.
SRP_BUILTIN_MISSING = StoredText(gettext_noop("Alliance Auth built-in SRP is not installed."))
SRP_BUILTIN_NO_FC = StoredText(gettext_noop("FC character is not present in Alliance Auth EveCharacter data."))
SRP_BUILTIN_SCHEMA = StoredText(gettext_noop("Unsupported built-in SRP schema; required fields: %(fields)s"))
SRP_BUILTIN_CREATED = StoredText(gettext_noop("Built-in Alliance Auth SRP fleet created."))
AASRP_MISSING = StoredText(gettext_noop("aa-srp is not installed."))
AASRP_REUSED = StoredText(gettext_noop("Existing aa-srp link of this fleet reused."))
AASRP_SCHEMA = StoredText(gettext_noop("Unsupported aasrp schema; required fields: %(fields)s"))
AASRP_CREATED = StoredText(gettext_noop("aa-srp link created."))
FLEETOPS_SRP_DISABLED = StoredText(gettext_noop("FleetOps SRP is not enabled."))
FLEETOPS_SRP_CREATED = StoredText(gettext_noop("FleetOps SRP link created."))
FLEETOPS_SRP_EXISTS = StoredText(gettext_noop("FleetOps SRP link already exists."))
SRP_NO_PROVIDER = StoredText(gettext_noop("No supported SRP provider is available."))
SRP_PROVIDER_MISSING = StoredText(gettext_noop("SRP provider '%(key)s' is not installed."))
SRP_ALREADY_LINKED = StoredText(gettext_noop("SRP fleet is already linked; no new SRP fleet was created."))

# Step names that only appear inside failure texts and job names.
# (More of them are declared with STEP_NAMES above.)

# How a fleet end is written down, and the PAP result that follows it.
END_BOSS_HANDOVER_UNKNOWN = StoredText(
    gettext_noop("Ended automatically: fleet boss handed over to another character")
)
PAP_GRANTED = StoredText(gettext_noop("PAP: %(granted)s granted at %(multiplier)sx."))
PAP_MULTIPLIER_PENDING = StoredText(gettext_noop("The multiplier choice is pending."))
FLEET_END_PAP = StoredText(
    gettext_noop("%(reason)s. PAP: %(granted)s granted at %(multiplier)sx."),
    nested=("reason",),
    numbers=("granted", "multiplier"),
    priority=True,
)

# Tracking: why a fleet could not be read.
UNREADABLE_GONE = StoredText(gettext_noop("The fleet could not be read: it no longer exists or the FC left it."))
UNREADABLE_HANDOVER = StoredText(gettext_noop("Fleet boss handed over to %(name)s."))
UNREADABLE_HANDOVER_UNKNOWN = StoredText(gettext_noop("Fleet boss handed over to another character."))
CHECK_NOT_COUNTED = StoredText(
    gettext_noop("The fleet could not be read and the fleet boss check failed; this check was not counted.")
)

# Fleet starts and scheduled fleets.
MAIN_REQUIRED = StoredText(gettext_noop("A main character is required to create a manual fleet record."))
NOT_SCHEDULED = StoredText(gettext_noop("This fleet is no longer scheduled."))
CHANNEL_CHANGE_NEEDS_POST = StoredText(
    gettext_noop(
        "Post the updated pre ping when you change the ping channel: "
        "a cancellation notice is posted in the channel of the pre ping."
    )
)
ONLY_SCHEDULING_FC = StoredText(gettext_noop("Only the FC who scheduled this fleet can start it."))
MOTD_NOT_ACTIVE = StoredText(gettext_noop("The MOTD can only be regenerated while the fleet is active."))

# Template and ping checks (the problems with a ping that cannot be sent).
TEMPLATE_FALLBACK = StoredText(
    gettext_noop('%(label)s template "%(name)s" could not be rendered (%(error_type)s); the built-in default was used.')
)
CHANNEL_NO_EVERYONE = StoredText(gettext_noop("@everyone is not allowed in %(name)s."))
CHANNEL_NO_HERE = StoredText(gettext_noop("@here is not allowed in %(name)s."))
TOO_MANY_ROLES = StoredText(
    gettext_noop("Select at most %(limit)s roles; Discord cannot mention more in one message.")
)
ROLE_UNAVAILABLE = StoredText(gettext_noop("Role %(name)s is no longer available."))
ROLE_NOT_ALLOWED = StoredText(gettext_noop("Role %(name)s is not allowed in %(channel)s."))

# Who may change a fleet, and why not now.
ACCESS_NOT_ACTIVE = StoredText(gettext_noop("Fleet management is only available while the fleet is active."))
ACCESS_FLEET_GONE = StoredText(
    gettext_noop("The fleet could not be read. It may have been disbanded, or the FC left it.")
)
ACCESS_NO_WRITE_TOKEN = StoredText(
    gettext_noop("The FC character has no ESI token with the fleet write scope (%(scope)s).")
)
ACCESS_WRITE_BUSY = StoredText(gettext_noop("Another change to this fleet is in progress. Try again in a moment."))
ACCESS_NOT_BOSS = StoredText(
    gettext_noop("%(name)s is not the fleet boss any more (current boss: %(boss)s). Fleet changes are disabled.")
)
ACCESS_NOT_BOSS_UNKNOWN = StoredText(
    gettext_noop(
        "%(name)s is not the fleet boss any more (current boss: another character). Fleet changes are disabled."
    )
)
ACCESS_CANNOT_CHANGE = StoredText(gettext_noop("You cannot change this fleet."))

# Background jobs of a fleet.
THE_RUNNING_JOB = StoredText(gettext_noop("the running job"))
JOB_WAIT = StoredText(gettext_noop("Wait until “%(label)s” has finished."), nested=("label",))
JOB_STOPPED_UNEXPECTEDLY = StoredText(gettext_noop("The job stopped unexpectedly."))
JOB_NOT_STARTED = StoredText(gettext_noop("The background worker did not start the job."))
JOB_WORKER_DOWN = StoredText(gettext_noop("The background worker is not available. Try again later."))
JOB_FLEET_MISSING = StoredText(gettext_noop("The fleet no longer exists."))
JOB_NOT_ALLOWED = StoredText(gettext_noop("You can no longer change this fleet."))
JOB_SUMMARY = StoredText(
    gettext_noop("%(label)s: %(done)s of %(total)s done."), nested=("label",), numbers=("done", "total"), priority=True
)
JOB_SUMMARY_FAILED = StoredText(
    gettext_noop("%(label)s: %(done)s of %(total)s done, %(failed)s failed."),
    nested=("label",),
    numbers=("done", "total", "failed"),
    priority=True,
)
STOPPED = StoredText(gettext_noop("Stopped: %(error)s"), nested=("error",))

# "<name>: <what went wrong>" lines of a batch; the name is a character, a wing or a squad.
NAMED_ERROR = StoredText(gettext_noop("%(name)s: %(error)s"), nested=("error",))
SQUAD_UNIT_FAILED = StoredText(gettext_noop("%(wing)s / %(squad)s: %(error)s"), nested=("error",))
SKIPPED_LIST = StoredText(gettext_noop("Skipped: %(items)s."), nested=("items",))

# Reading the fleet again.
REFRESHED_RECENTLY = StoredText(gettext_noop("Refreshed a moment ago."))
REFRESHED = StoredText(gettext_noop("Fleet refreshed from ESI."))
NOT_OPEN = StoredText(gettext_noop("The fleet is no longer open."))

# Wings and squads: the answers of the fleet page.
VIEW_REFRESHED = StoredText(gettext_noop("The fleet view has been refreshed."))
WING_CREATED = StoredText(gettext_noop("Wing created."))
WING_CREATED_NAMED = StoredText(gettext_noop("Wing “%(name)s” created."))
WING_CREATED_RENAME_FAILED = StoredText(
    gettext_noop("Wing created, but renaming failed: %(error)s"), nested=("error",)
)
WING_ALREADY_NAMED = StoredText(gettext_noop("The wing is already called “%(name)s”."))
WING_RENAMED = StoredText(gettext_noop("Wing “%(old)s” renamed to “%(name)s”."))
WING_DELETED = StoredText(gettext_noop("Wing “%(name)s” deleted."))
SQUAD_CREATED = StoredText(gettext_noop("Squad created in “%(wing)s”."))
SQUAD_CREATED_NAMED = StoredText(gettext_noop("Squad “%(name)s” created in “%(wing)s”."))
SQUAD_CREATED_RENAME_FAILED = StoredText(
    gettext_noop("Squad created, but renaming failed: %(error)s"), nested=("error",)
)
SQUAD_ALREADY_NAMED = StoredText(gettext_noop("The squad is already called “%(name)s”."))
SQUAD_RENAMED = StoredText(gettext_noop("Squad “%(old)s” renamed to “%(name)s”."))
SQUAD_DELETED = StoredText(gettext_noop("Squad “%(name)s” deleted."))

# Moving members.
MOVE_NO_MEMBERS = StoredText(gettext_noop("Choose the members to move."))
MOVE_TOO_MANY = StoredText(gettext_noop("Move at most %(limit)s members at a time."))
MOVE_ONE_COMMANDER = StoredText(
    gettext_noop("Only one member can take a commander position. Choose a single member.")
)
MOVED_FLEET_COMMANDER = StoredText(gettext_noop("%(name)s is now fleet commander."))
MOVED_WING_COMMANDER = StoredText(gettext_noop("%(name)s is now wing commander of %(wing)s."))
MOVED_SQUAD_COMMANDER = StoredText(gettext_noop("%(name)s is now squad commander of %(wing)s / %(squad)s."))
MOVED_TO_SQUAD = StoredText(gettext_noop("Moved %(name)s to %(wing)s / %(squad)s."))
ALREADY_THERE = StoredText(gettext_noop("%(name)s is already there."))
ALREADY_THERE_ITEM = StoredText(gettext_noop("%(name)s is already there"))
LEFT_FLEET = StoredText(gettext_noop("%(name)s is no longer in the fleet."))
LEFT_FLEET_ITEM = StoredText(gettext_noop("%(name)s is no longer in the fleet"))
NOBODY_MOVED = StoredText(gettext_noop("Nobody was moved."))
MOVING_IN_BACKGROUND = StoredPlural(
    gettext_noop("Moving %(count)s member in the background."),
    gettext_noop("Moving %(count)s members in the background."),
)
MOVE_JOB = StoredPlural(gettext_noop("Move %(count)s member"), gettext_noop("Move %(count)s members"))
MOVE_JOB_TO = StoredPlural(
    gettext_noop("Move %(count)s member to %(wing)s / %(squad)s"),
    gettext_noop("Move %(count)s members to %(wing)s / %(squad)s"),
)

# Inviting characters.
INVITE_NO_CHARACTERS = StoredText(gettext_noop("Choose the characters to invite."))
INVITE_TOO_MANY = StoredText(gettext_noop("Invite at most %(limit)s characters at a time."))
INVITE_ONE_COMMANDER = StoredText(gettext_noop("Only one character can be invited into a commander position."))
INVITE_NOBODY = StoredText(gettext_noop("Nobody was invited."))
INVITE_NOT_REGISTERED_ITEM = StoredText(
    gettext_noop("%(name)s is not registered in Alliance Auth by an active user")
)
INVITE_IN_FLEET_ITEM = StoredText(gettext_noop("%(name)s is already in the fleet"))
INVITE_IN_FLEET = StoredText(gettext_noop("%(name)s is already in the fleet."))
INVITE_SENT = StoredText(gettext_noop("Invitation sent to %(name)s."))
INVITE_SENT_ANY_SQUAD = StoredText(gettext_noop("Invitation sent to %(name)s (any squad with free positions)."))
INVITE_SENT_FLEET_COMMANDER = StoredText(gettext_noop("Invitation sent to %(name)s (fleet commander)."))
INVITE_SENT_WING_COMMANDER = StoredText(
    gettext_noop("Invitation sent to %(name)s (wing commander of %(wing)s).")
)
INVITE_SENT_SQUAD_COMMANDER = StoredText(
    gettext_noop("Invitation sent to %(name)s (squad commander of %(wing)s / %(squad)s).")
)
INVITE_SENT_TO_SQUAD = StoredText(gettext_noop("Invitation sent to %(name)s (%(wing)s / %(squad)s)."))
INVITING_IN_BACKGROUND = StoredPlural(
    gettext_noop("Inviting %(count)s character in the background."),
    gettext_noop("Inviting %(count)s characters in the background."),
)
INVITE_JOB = StoredPlural(gettext_noop("Invite %(count)s character"), gettext_noop("Invite %(count)s characters"))
INVITE_JOB_INTO = StoredPlural(
    gettext_noop("Invite %(count)s character into %(wing)s / %(squad)s"),
    gettext_noop("Invite %(count)s characters into %(wing)s / %(squad)s"),
)

# Kicking members.
KICK_NOT_ACTIVE = StoredText(gettext_noop("Members can only be kicked from an active fleet."))
KICK_NO_ESI_FLEET = StoredText(gettext_noop("This fleet has no ESI fleet to kick members from."))
KICK_PROTECTED = StoredText(gettext_noop("The FC character and the fleet boss are never kicked."))
KICK_NOT_IN_FLEET = StoredText(gettext_noop("This character is not in the fleet."))
KICK_NO_WRITE_TOKEN = StoredText(gettext_noop("The FC character has no ESI token with esi-fleets.write_fleet.v1"))
KICKED_MEMBER = StoredText(gettext_noop("Kicked %(member)s."))
KICK_FAILED = StoredText(gettext_noop("Could not kick %(member)s: %(error)s"), nested=("error",))
KICK_ALREADY_LEFT = StoredText(gettext_noop("Could not kick %(member)s: already left the fleet."))
KICKED_CAPSULES = StoredText(gettext_noop("Kicked %(count)s capsule member(s)."))
KICKED_OUTSIDE = StoredText(gettext_noop("Kicked %(count)s member(s) outside %(system)s."))
KICK_FAILURES = StoredText(gettext_noop("%(count)s failed. %(details)s"), nested=("details",))
KICK_SKIPPED = StoredText(gettext_noop("%(count)s skipped: no longer in the fleet or no longer a target."))
KICK_NOBODY = StoredText(gettext_noop("Nobody was kicked. %(reason)s"), nested=("reason",))
KICK_JOB_CAPSULES = StoredPlural(
    gettext_noop("Kick %(count)s capsule pilot"), gettext_noop("Kick %(count)s capsule pilots")
)
KICK_JOB_OUTSIDE = StoredPlural(
    gettext_noop("Kick %(count)s member outside %(system)s"),
    gettext_noop("Kick %(count)s members outside %(system)s"),
)

# Structure templates.
TEMPLATE_GONE = StoredText(gettext_noop("That structure template no longer exists or is disabled."))
TEMPLATE_INVALID = StoredText(
    gettext_noop("The template “%(name)s” cannot be applied. %(reason)s"), nested=("reason",)
)
TEMPLATE_JOB = StoredText(gettext_noop("Apply template %(name)s"))
TEMPLATE_APPLYING = StoredText(
    gettext_noop("Applying template “%(name)s” in the background. Members are not moved.")
)
TEMPLATE_NOTHING = StoredText(gettext_noop("Nothing to change: the fleet already matches “%(name)s”."))
TEMPLATE_WING_RENAMED = StoredText(gettext_noop("Renamed wing “%(old)s” to “%(name)s”."))
TEMPLATE_WING_CREATED = StoredText(gettext_noop("Created wing %(number)s “%(name)s”."))
TEMPLATE_SQUAD_RENAMED = StoredText(gettext_noop("Renamed squad “%(old)s” of %(wing)s to “%(name)s”."))
TEMPLATE_SQUAD_CREATED = StoredText(gettext_noop("Created squad “%(name)s” in %(wing)s."))
TEMPLATE_KEPT_WING = StoredText(gettext_noop("Kept the wing “%(name)s”: it has members."))
TEMPLATE_KEPT_SQUAD = StoredText(gettext_noop("Kept the squad “%(name)s” of %(wing)s: it has members."))
TEMPLATE_DELETED_WING = StoredText(gettext_noop("Deleted the empty wing “%(name)s”."))
TEMPLATE_DELETED_SQUAD = StoredText(gettext_noop("Deleted the empty squad “%(name)s” of %(wing)s."))
TEMPLATE_WING_GONE = StoredText(gettext_noop("The wing is no longer in the fleet."))
WING_FAILED = StoredText(gettext_noop("Wing “%(name)s”: %(error)s"), nested=("error",))
WING_NUMBER_FAILED = StoredText(gettext_noop("Wing %(number)s “%(name)s”: %(error)s"), nested=("error",))
WING_UNNAMED_FAILED = StoredText(
    gettext_noop("Wing %(number)s was created, but not named “%(name)s”: %(error)s"), nested=("error",)
)
SQUAD_FAILED = StoredText(gettext_noop("Squad “%(name)s” of %(wing)s: %(error)s"), nested=("error",))
SQUAD_UNNAMED_FAILED = StoredText(
    gettext_noop("%(wing)s: a squad was created, but not named “%(name)s”: %(error)s"), nested=("error",)
)

# Rules of the fleet structure.
RULE_NAME_LENGTH = StoredText(gettext_noop("Names can be 1 to %(max)s characters long."))
RULE_INVALID_WING_ID = StoredText(gettext_noop('"%(value)s" is not a valid wing ID.'))
RULE_INVALID_SQUAD_ID = StoredText(gettext_noop('"%(value)s" is not a valid squad ID.'))
RULE_INVALID_ID = StoredText(gettext_noop('"%(token)s" is not a valid ID. Separate IDs with commas.'))
RULE_WING_NAME_EMPTY = StoredText(gettext_noop("Line %(line)s: a wing name is empty."))
RULE_SQUAD_NAME_EMPTY = StoredText(gettext_noop("Line %(line)s: a squad name is empty."))
RULE_WING_NAME_LONG = StoredText(
    gettext_noop('Line %(line)s: wing name "%(name)s" is longer than %(max)s characters.')
)
RULE_SQUAD_NAME_LONG = StoredText(
    gettext_noop('Line %(line)s: squad name "%(name)s" is longer than %(max)s characters.')
)
RULE_LINE_TOO_MANY_WINGS = StoredText(gettext_noop("Line %(line)s: EVE allows at most %(max)s wings."))
RULE_LINE_NEEDS_SQUAD = StoredText(
    gettext_noop('Line %(line)s: wing "%(name)s" needs at least one squad, for example "%(name)s: Squad 1".')
)
RULE_LINE_COLON = StoredText(
    gettext_noop('Line %(line)s: use one ":" per line, between the wing name and its squads.')
)
RULE_LINE_TOO_MANY_SQUADS = StoredText(
    gettext_noop('Line %(line)s: wing "%(name)s" has %(count)s squads; EVE allows at most %(max)s per wing.')
)
RULE_NO_WINGS = StoredText(gettext_noop('Add at least one wing, for example "Main: Alpha, Bravo".'))
RULE_WING_GONE = StoredText(gettext_noop("That wing no longer exists."))
RULE_SQUAD_GONE = StoredText(gettext_noop("That squad no longer exists."))
RULE_NOT_A_POSITION = StoredText(gettext_noop('"%(role)s" is not a fleet position.'))
RULE_FLEET_COMMANDER_PLACE = StoredText(gettext_noop("The fleet commander has no wing or squad."))
RULE_WING_COMMANDER_NEEDS_WING = StoredText(gettext_noop("A wing commander needs a wing."))
RULE_WING_COMMANDER_NO_SQUAD = StoredText(gettext_noop("A wing commander has no squad."))
RULE_ANY_SQUAD_HINT = StoredText(
    gettext_noop("Choose both a wing and a squad, or neither to join any squad with free positions.")
)
RULE_SQUAD_COMMANDER_NEEDS_PLACE = StoredText(gettext_noop("A squad commander needs a wing and a squad."))
RULE_SQUAD_MEMBER_NEEDS_PLACE = StoredText(gettext_noop("A squad member needs a wing and a squad."))
RULE_SQUAD_NOT_IN_WING = StoredText(gettext_noop("That squad is not part of the chosen wing."))
RULE_FLEET_COMMANDER_TAKEN = StoredText(
    gettext_noop("The fleet already has a fleet commander (%(holder)s). Move them first.")
)
RULE_WING_COMMANDER_TAKEN = StoredText(
    gettext_noop("%(unit)s already has a wing commander (%(holder)s). Move them first.")
)
RULE_SQUAD_COMMANDER_TAKEN = StoredText(
    gettext_noop("%(unit)s already has a squad commander (%(holder)s). Move them first.")
)
RULE_FLEET_FULL = StoredText(gettext_noop("The fleet is full: EVE allows %(max)s members."))
RULE_FLEET_ROOM = StoredPlural(
    gettext_noop("Only %(count)s more character fits into the fleet (EVE allows %(max)s members)."),
    gettext_noop("Only %(count)s more characters fit into the fleet (EVE allows %(max)s members)."),
)
RULE_TOO_MANY_WINGS = StoredText(gettext_noop("EVE allows at most %(max)s wings."))
RULE_TOO_MANY_SQUADS = StoredText(gettext_noop("EVE allows at most %(max)s squads per wing."))
RULE_WING_NOT_EMPTY = StoredText(
    gettext_noop("Only empty wings can be deleted: move the wing commander and all squad members first.")
)
RULE_SQUAD_NOT_EMPTY = StoredPlural(
    gettext_noop("Only empty squads can be deleted. Move its member first."),
    gettext_noop("Only empty squads can be deleted. Move its %(count)s members first."),
)
RULE_NOT_A_COMMANDER_POSITION = StoredText(
    gettext_noop('"%(role)s" is not a commander position. Use fleet_commander, wing_commander or squad_commander.')
)

# PAP links: why a character was not registered, and why a link cannot be created.
PAPLINK_NOT_OWNED = StoredText(gettext_noop("Not one of your characters."))
PAPLINK_NO_TOKEN = StoredText(
    gettext_noop("No valid ESI token with the PAP link scopes; use Add a character to log in with it.")
)
PAPLINK_TOKEN_CHECK_FAILED = StoredText(gettext_noop("The ESI token could not be checked; try again."))
PAPLINK_ALREADY_REGISTERED = StoredText(gettext_noop("Already registered for this fleet."))
PAPLINK_CLOSED = StoredText(gettext_noop("The PAP link is no longer open."))
PAPLINK_LEGACY_FLEET = StoredText(
    gettext_noop(
        "PAP links are not available for fleets closed before PAP followed time in fleet; add manual PAP instead."
    )
)
PAPLINK_NOT_ONLINE = StoredText(gettext_noop("Not logged in to the game; this PAP link needs the character online."))
PAPLINK_TOO_MANY = StoredText(
    gettext_noop("At most %(limit)s characters are registered at once; register this one again.")
)
PAPLINK_WRONG_STATUS = StoredText(
    gettext_noop("PAP links are created for active or closed fleets; this fleet is %(status)s."), nested=("status",)
)
PAPLINK_MINUTES = StoredText(gettext_noop("A PAP link stays open for %(min)s to %(max)s minutes."))

# Manual PAP, approvals and the statistics rules.
PAP_MANUAL_ENTRY = StoredText(gettext_noop("Manual PAP is entered by hand or imported from a roster."))
PAP_CHOOSE_DUPLICATE = StoredText(gettext_noop("Choose whether to keep, replace or merge the tracked PAP."))
PAP_MULTIPLIER_RANGE = StoredText(gettext_noop("The PAP multiplier must be a whole number from 1 to %(max)s."))
PAP_ONLY_ACTIVE_OR_CLOSED = StoredText(gettext_noop("Manual PAP can only be added to Active or Closed fleets."))
APPROVAL_REASON_REQUIRED = StoredText(gettext_noop("Give a reason for the rejection."))
APPROVAL_ALREADY_APPROVED = StoredText(gettext_noop("This fleet is already approved."))
APPROVAL_NOT_PENDING = StoredText(gettext_noop("Only fleets waiting for approval can be rejected."))
COUNTING_REASON_REQUIRED = StoredText(gettext_noop("Give a reason for excluding this fleet from the statistics."))
COUNTING_MODE_CHOICE = StoredText(gettext_noop("Choose whether this fleet counts automatically, always or never."))
INCENTIVES_DISABLED = StoredText(gettext_noop("FC incentives are disabled in the FleetOps settings."))
INCENTIVES_WAIVER_LOCKED = StoredText(gettext_noop("Finalized periods must be unlocked before waiver changes."))
INCENTIVES_REBUILD_LOCKED = StoredText(gettext_noop("Finalized periods must be unlocked before recalculation."))
INCENTIVES_NOT_IN_REVIEW = StoredText(gettext_noop("Period must be in review before finalizing."))
INCENTIVES_NOT_FINALIZED = StoredText(gettext_noop("Only finalized periods can be unlocked."))
INCENTIVES_NO_STATISTIC = StoredText(gettext_noop("FC statistic does not exist. Recalculate the period first."))
SPECIAL_ROLE_UNKNOWN = StoredText(gettext_noop("Unknown special role."))
SPECIAL_ROLE_ONLY_ACTIVE_OR_CLOSED = StoredText(
    gettext_noop("Special roles can only be assigned on Active or Closed fleets.")
)
SPECIAL_ROLE_DISABLED = StoredText(gettext_noop("The special role %(name)s is disabled."))
SPECIAL_ROLE_NOT_IN_FLEET = StoredText(gettext_noop("Special roles are given to characters recorded in the fleet."))
PRESET_NAME_REQUIRED = StoredText(gettext_noop("A preset needs a name."))

# Pasted rosters: why a name is not imported.
ROSTER_EXPIRED = StoredText(
    gettext_noop("The preview is older than 30 minutes or was changed. Paste the roster again.")
)
ROSTER_NOT_IN_AUTH = StoredText(gettext_noop("Not in Alliance Auth"))
ROSTER_NOT_FOUND = StoredText(gettext_noop("No EVE character with this name"))
ROSTER_ESI_ERROR = StoredText(gettext_noop("Could not be checked (ESI error)"))
ROSTER_HAS_PAP = StoredText(gettext_noop("Already has PAP in this fleet"))
ROSTER_LISTED_TWICE = StoredText(gettext_noop("Listed twice"))

# The status of a fleet or a claim as a word inside a stored message ("this fleet is scheduled"). Only the
# fleet statuses that message can name are listed: PAP links are refused for fleets that are not active or closed.
FLEET_STATUS_WORDS = {
    "draft": StoredText(gettext_noop("draft")),
    "scheduled": StoredText(gettext_noop("scheduled")),
    "starting": StoredText(gettext_noop("starting")),
    "ending": StoredText(gettext_noop("ending")),
    "cancelled": StoredText(gettext_noop("cancelled")),
    "error": StoredText(gettext_noop("error")),
}
CLAIM_STATUS_WORDS = {
    "pending": StoredText(gettext_noop("pending")),
    "approved": StoredText(gettext_noop("approved")),
    "paid": StoredText(gettext_noop("paid")),
    "rejected": StoredText(gettext_noop("rejected")),
    "withdrawn": StoredText(gettext_noop("withdrawn")),
}

# Reasons written to the audit log by code.
CLASSIFY_REASON = StoredText(gettext_noop("Classified from a fleet's composition"))
DISCORD_ROLE_SYNC_REASON = StoredText(
    gettext_noop("Roles synced from Discord in FleetOps front-end administration")
)
AUDIT_ARCHIVE_EDIT = StoredText(gettext_noop("Edited from FleetOps fleet archive"))
AUDIT_FLEET_TIMES_EDITED = StoredText(gettext_noop("Fleet times edited. %(detail)s"), nested=("detail",))
AUDIT_PAP_DELETED = StoredText(gettext_noop("manual PAP deleted"))
AUDIT_RECALCULATED = StoredText(gettext_noop("manual recalculation"))
AUDIT_CONFIG_UPDATED = StoredText(gettext_noop("Updated from FleetOps front-end administration"))
AUDIT_CONFIG_CHANGED = StoredText(gettext_noop("Changed from FleetOps front-end administration"))
AUDIT_CONFIG_DELETED = StoredText(gettext_noop("Deleted from FleetOps front-end administration"))
AUDIT_DEFAULT_TEMPLATE_REPLACED = StoredText(
    gettext_noop("Default template replaced from FleetOps front-end administration")
)
AUDIT_DEFAULT_FUNCTION_REPLACED = StoredText(
    gettext_noop("Default ship function replaced from FleetOps front-end administration")
)
AUDIT_DEFAULT_PAYOUT_RULE_REPLACED = StoredText(
    gettext_noop("Default payout rule replaced from FleetOps front-end administration")
)

# Ship functions (the answers of the fleet composition page).
FUNCTION_GONE = StoredText(gettext_noop("That ship function no longer exists or is disabled."))
SHIP_TYPE_GONE = StoredText(gettext_noop("That ship type does not exist."))
SHIP_ALREADY_COUNTS = StoredText(gettext_noop("%(ship)s already counts as %(function)s."))
SHIP_NOW_COUNTS = StoredText(gettext_noop("%(ship)s now counts as %(function)s."))

# SRP: automatic decisions and the reasons written to claims.
SRP_LIMIT_PERSONAL = StoredText(gettext_noop("monthly personal limit"))
SRP_LIMIT_CORPORATION = StoredText(gettext_noop("monthly corporation limit"))
SRP_LIMIT_MONTHLY = StoredText(gettext_noop("monthly limit"))
SRP_REJECTED_LIMIT_USED_UP = StoredText(
    gettext_noop("Rejected automatically: the %(limit)s is used up (cap %(cap)s)."), nested=("limit",)
)
# The note a roster import offers; left as it is, the records carry it as a message.
ROSTER_IMPORT_NOTE = StoredText(gettext_noop("Imported from fleet roster paste"))
SRP_REASON_ZKILL_VALUE = StoredText(gettext_noop("Loss value from zKillboard"))
SRP_REASON_RECALCULATED = StoredText(gettext_noop("Recalculated with the current policy"))
# The reason that goes with "Approve within limit" when the reviewer wrote none.
SRP_REDUCED_PERSONAL = StoredText(gettext_noop("Reduced to the remaining monthly personal limit."))
SRP_REDUCED_CORPORATION = StoredText(gettext_noop("Reduced to the remaining monthly corporation limit."))
SRP_REDUCED_MONTHLY = StoredText(gettext_noop("Reduced to the remaining monthly limit."))

# SRP: why a request cannot go ahead (the member or the reviewer is told; the pages show the English text
# through translate_stored).
SRP_CHECK_EXPIRED = StoredText(gettext_noop("This check has expired. Check the loss again."))
SRP_THROTTLED = StoredText(gettext_noop("You checked many losses in a short time. Wait a few minutes and try again."))
SRP_OWN_DUPLICATE = StoredText(gettext_noop("You already submitted this loss."))
SRP_OTHER_DUPLICATE = StoredText(gettext_noop("This loss was already claimed by another pilot."))
SRP_CLAIMED_BY = StoredText(gettext_noop("This loss was already claimed by %(name)s."))
SRP_ALREADY_STATUS = StoredText(gettext_noop("This claim is already %(status)s."), nested=("status",))
SRP_CLAIM_GONE = StoredText(gettext_noop("This claim no longer exists."))
SRP_NEED_REASON = StoredText(gettext_noop("Give a reason."))
SRP_NEED_REASON_REVOKE = StoredText(gettext_noop("Give a reason for revoking the approval."))
SRP_NEED_REASON_UNPAY = StoredText(gettext_noop("Give a reason for undoing the payment."))
SRP_NEED_REASON_REOPEN = StoredText(gettext_noop("Give a reason for reopening the claim."))
SRP_REASON_TOO_LONG = StoredText(gettext_noop("The reason is too long (at most %(max)s characters)."))
SRP_REJECTION_REASON_TOO_LONG = StoredText(
    gettext_noop("The reason for the rejection is too long (at most %(max)s characters).")
)
SRP_REVOKE_REASON_TOO_LONG = StoredText(
    gettext_noop("The reason for revoking the approval is too long (at most %(max)s characters).")
)
SRP_UNPAY_REASON_TOO_LONG = StoredText(
    gettext_noop("The reason for undoing the payment is too long (at most %(max)s characters).")
)
SRP_REOPEN_REASON_TOO_LONG = StoredText(
    gettext_noop("The reason for reopening the claim is too long (at most %(max)s characters).")
)
SRP_COMMENT_TOO_LONG = StoredText(gettext_noop("The comment is too long (at most %(max)s characters)."))
SRP_NOTE_TOO_LONG = StoredText(gettext_noop("The note is too long (at most %(max)s characters)."))
SRP_NOTE_REQUIRED = StoredText(gettext_noop("Write the note."))
SRP_ONLY_CLAIMANT_WITHDRAWS = StoredText(gettext_noop("Only the claimant can withdraw this claim."))
SRP_ONLY_CLAIMANT_RESUBMITS = StoredText(gettext_noop("Only the claimant can submit this claim again."))
SRP_ONLY_PENDING_WITHDRAWN = StoredText(gettext_noop("Only pending claims can be withdrawn."))
SRP_ONLY_WITHDRAWN_RESUBMITTED = StoredText(gettext_noop("Only withdrawn claims can be submitted again."))
SRP_ONLY_PENDING_RECALCULATED = StoredText(gettext_noop("Only pending claims can be recalculated."))
SRP_ONLY_PENDING_APPROVED = StoredText(gettext_noop("Only pending claims can be approved."))
SRP_ONLY_PENDING_REJECTED = StoredText(gettext_noop("Only pending claims can be rejected."))
SRP_ONLY_APPROVED_REVOKED = StoredText(gettext_noop("Only approved claims can be revoked."))
SRP_ONLY_APPROVED_PAID = StoredText(gettext_noop("Only approved claims can be marked paid."))
SRP_ONLY_PAID_UNPAID = StoredText(gettext_noop("Only paid claims can be marked unpaid."))
SRP_ONLY_REJECTED_REOPENED = StoredText(gettext_noop("Only rejected claims can be reopened."))
SRP_ONLY_PENDING_MOVED = StoredText(
    gettext_noop("Only pending claims can be moved between in fleet and out of fleet.")
)
SRP_NOT_A_MANAGER = StoredText(gettext_noop("You are not allowed to decide on SRP claims."))
SRP_OWN_CLAIM = StoredText(gettext_noop("Another SRP manager has to decide on your own claim."))
SRP_OWN_CLAIM_SKIPPED = StoredText(gettext_noop("Your own claims are decided by another SRP manager."))
SRP_AMOUNT_RANGE = StoredText(gettext_noop("Enter an amount between 1 and %(max)s ISK."))
SRP_STATUS_CHANGED = StoredText(
    gettext_noop("This claim is %(now)s now; it was %(was)s on your page. Check it and decide again."),
    nested=("now", "was"),
)
SRP_SUGGESTED_CHANGED = StoredText(
    gettext_noop(
        "The suggested amount changed from %(shown)s to %(current)s while you were looking at the claim. "
        "Check it and approve again."
    )
)
SRP_LIMITED_CHANGED = StoredText(
    gettext_noop(
        "The amount within the limits changed from %(shown)s to %(current)s while you were looking at the claim. "
        "Check it and approve again."
    )
)
SRP_APPROVED_CHANGED = StoredText(
    gettext_noop(
        "The approved amount changed from %(expected)s to %(current)s after your page was loaded. "
        "Check it before paying, so the books show what was sent."
    )
)
SRP_PAID_CHANGE = StoredText(gettext_noop("This claim is paid. Undo the payment before changing it."))
SRP_PAID_REVOKE = StoredText(gettext_noop("This claim is paid. Undo the payment first, then revoke the approval."))
SRP_ALREADY_APPROVED = StoredText(gettext_noop("This claim is already approved for %(amount)s."))
SRP_ABOVE_POLICY = StoredText(
    gettext_noop(
        "%(amount)s is more than the policy amount of %(policy)s. Confirm to pay more than the policy amount."
    )
)
SRP_LIMIT_LEFT = StoredText(gettext_noop("Only %(remaining)s left in the %(limit)s."), nested=("limit",))
SRP_OVER_LIMIT = StoredText(
    gettext_noop("This is %(over)s over the %(limit)s (%(remaining)s left). Confirm to approve it anyway."),
    nested=("limit",),
)
SRP_AMOUNT_REASON = StoredText(gettext_noop("Give a reason for this amount."))
SRP_HAS_WARNINGS = StoredText(gettext_noop("This claim has warnings. Review it on its own."))
SRP_NO_SUGGESTION = StoredText(gettext_noop("This claim has no suggested amount."))
SRP_CHOOSE_SCOPE = StoredText(gettext_noop("Choose in fleet or out of fleet."))
SRP_CHOOSE_FLEET = StoredText(gettext_noop("Choose the fleet of the loss."))
SRP_LINK_CLOSED = StoredText(gettext_noop("This SRP link is closed."))
SRP_SAME_FLEET = StoredText(gettext_noop("The claim is already a loss of this fleet."))
SRP_SAME_OUT_OF_FLEET = StoredText(gettext_noop("The claim is already an out-of-fleet loss."))
SRP_FLEET_GONE = StoredText(gettext_noop("The fleet of this claim no longer exists."))

# SRP: reading a killmail the member pasted.
SRP_UNRECOGNISED = StoredText(
    gettext_noop("Paste an in-game killmail link, a zKillboard link, or the killmail ID and hash.")
)
SRP_USE_INGAME_LINK = StoredText(gettext_noop("Paste the in-game killmail link instead."))
SRP_KILLBOARD_OFF = StoredText(gettext_noop("Killboard links are not accepted at the moment."))
SRP_BAD_KILLMAIL_ID = StoredText(gettext_noop("This killmail ID is not valid."))
SRP_TEXT_TOO_LONG = StoredText(gettext_noop("That text is too long. Paste only the killmail link."))
SRP_INCOMPLETE_KILLMAIL = StoredText(gettext_noop("ESI returned an incomplete killmail. Try again later."))

# Group pings and group fleets: why a ping channel or a fleet is refused.
CHANNEL_NOT_ALLOWED = StoredText(gettext_noop("You may not post to the ping channel %(name)s."))
GROUP_NOT_LED = StoredText(gettext_noop("You no longer lead the group %(group)s in FleetOps."))
GROUP_CHANNEL_NOT_ASSIGNED = StoredText(gettext_noop("%(name)s is not a ping channel of the group %(group)s."))
GROUP_NO_FLEETS = StoredText(gettext_noop("The leaders of %(group)s may only send pings."))
GROUP_FLEET_TYPE_NOT_ALLOWED = StoredText(gettext_noop("Fleet type %(type)s is not allowed for the group %(group)s."))
GROUP_REQUIRED = StoredText(gettext_noop("Choose the group this fleet belongs to."))
GROUP_NO_PRE_PING = StoredText(
    gettext_noop(
        "Group fleets cannot be pre pinged. Send the fleet time with Send Ping and start the fleet when it is due."
    )
)
GROUP_NO_SRP = StoredText(gettext_noop("Group fleets of %(group)s get no SRP link."))

# Display labels of the codes stored with a fleet (not stored text: they are translated lazily).
# One entry for every code that is written to OperationAction.action.
STEP_LABELS = {
    "fleet_detection": gettext_lazy("Fleet detection"),
    "message_render": gettext_lazy("Message rendering"),
    "discord_ping": gettext_lazy("Discord ping"),
    "pre_ping": gettext_lazy("Pre ping"),
    "motd_update": gettext_lazy("MOTD update"),
    "tracking_start": gettext_lazy("Fleet tracking"),
    "srp_link": gettext_lazy("SRP link"),
    "fleet_end": gettext_lazy("Fleet end"),
    "fleet_cancel": gettext_lazy("Fleet cancellation"),
    "cancel_notice": gettext_lazy("Cancellation notice"),
    "manual_fleet": gettext_lazy("Manual fleet"),
    "pap_link_fleet": gettext_lazy("PAP link fleet"),
    "kick_member": gettext_lazy("Kick member"),
    "kick_outside_system": gettext_lazy("Kick outside system"),
    "kick_capsules": gettext_lazy("Capsule kick"),
    "fleet_kick": gettext_lazy("Fleet kick"),
    "fleet_template": gettext_lazy("Fleet template"),
    "fleet_move": gettext_lazy("Fleet move"),
    "fleet_invite": gettext_lazy("Fleet invite"),
}

# Positions in a fleet as ESI names them.
FLEET_ROLE_LABELS = {
    "fleet_commander": pgettext_lazy("fleet position", "Fleet Commander"),
    "wing_commander": pgettext_lazy("fleet position", "Wing Commander"),
    "squad_commander": pgettext_lazy("fleet position", "Squad Commander"),
    "squad_member": pgettext_lazy("fleet position", "Squad Member"),
}

# Each lane of the 0.2.0b2 work declares its new messages between its own markers, so the lanes merge without
# conflicts.

# --- 0.2.0b2 lane A: start and messages ---
# --- end lane A ---

# --- 0.2.0b2 lane B: fleet page ---
# --- end lane B ---

# --- 0.2.0b2 lane C: records and incentives ---
# --- end lane C ---

# --- 0.2.0b2 lane D: statistics ---
# --- end lane D ---

# --- 0.2.0b2 lane E: SRP and administration ---
# --- end lane E ---

# --- 0.2.0b2 lane F: group pings ---
# --- end lane F ---
