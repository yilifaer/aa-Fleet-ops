"""Who a Discord ping notifies, and the Discord payload rules that keep it exact.

A ping only notifies the targets the FC selected: ``allowed_mentions`` lists exactly those, and
``@everyone``/``@here`` typed anywhere else in the text are neutralised before sending.
"""

import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from fleetops.models import MAX_DATABASE_ID, PingRole

NONE, EVERYONE, HERE, ROLES = "none", "everyone", "here", "roles"
MENTION_MODES = (NONE, EVERYONE, HERE, ROLES)

DISCORD_MESSAGE_LIMIT = 2000
# Discord rejects allowed_mentions.roles with more entries.
MAX_ROLE_MENTIONS = 100
# Message flag: no link previews, so join and doctrine links do not unfurl into cards.
SUPPRESS_EMBEDS = 4

# Form values: "none", "everyone", "here" and "role:<PingRole pk>".
ROLE_CHOICE_PREFIX = "role:"
EXCLUSIVE_CHOICE_ERROR = "@everyone, @here and No ping cannot be combined with other targets."

ROLE_MENTION_RE = re.compile(r"<@&(\d{1,20})>")
_MASS_MENTION_RE = re.compile(r"@(everyone|here)")
_ZERO_WIDTH_SPACE = "\u200b"


@dataclass(frozen=True)
class RoleRef:
    id: str
    name: str


@dataclass(frozen=True)
class MentionSelection:
    mode: str = NONE
    roles: tuple[RoleRef, ...] = ()

    @property
    def notifies(self) -> bool:
        """True when the ping is meant to notify somebody."""
        return self.mode in (EVERYONE, HERE) or (self.mode == ROLES and bool(self.roles))


def _role_ref(role_id, name) -> RoleRef | None:
    role_id = str(role_id or "").strip()
    if not (role_id.isascii() and role_id.isdigit()):
        return None
    return RoleRef(id=role_id, name=str(name or "").strip())


def _unique_roles(roles) -> tuple[RoleRef, ...]:
    seen = set()
    unique = []
    for role in roles:
        if role is not None and role.id not in seen:
            seen.add(role.id)
            unique.append(role)
    return tuple(unique)


def selection_from_operation(op) -> MentionSelection:
    """The mention stored on a fleet record (mode plus role snapshot)."""
    mode = getattr(op, "ping_mention_mode", "") or NONE
    if mode not in MENTION_MODES:
        return MentionSelection()
    if mode != ROLES:
        return MentionSelection(mode)
    roles = []
    for entry in getattr(op, "ping_roles", None) or []:
        if isinstance(entry, dict):
            roles.append(_role_ref(entry.get("id"), entry.get("name")))
    return MentionSelection(ROLES, _unique_roles(roles))


def role_choice(role) -> str:
    """Form value of a PingRole."""
    return f"{ROLE_CHOICE_PREFIX}{role.pk}"


def selection_from_choices(values) -> MentionSelection:
    """Build a selection from the start form's checkbox values.

    Raises ``ValidationError`` when "No ping", "@everyone" or "@here" is combined with anything else,
    for unknown values, and for roles that were deleted or deactivated after the form was shown.
    """
    choices = list(dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip()))
    if not choices:
        return MentionSelection()
    singles = [value for value in choices if value in (NONE, EVERYONE, HERE)]
    if singles:
        if len(choices) > 1:
            raise ValidationError(EXCLUSIVE_CHOICE_ERROR, code="exclusive")
        return MentionSelection(singles[0])

    pks = []
    for value in choices:
        pk = value[len(ROLE_CHOICE_PREFIX) :] if value.startswith(ROLE_CHOICE_PREFIX) else ""
        if not (pk.isascii() and pk.isdigit() and int(pk) <= MAX_DATABASE_ID):
            raise ValidationError("Unknown ping target %(value)s.", code="invalid", params={"value": value})
        pks.append(int(pk))
    # Configured order (sort order, then name), not the order the boxes were ticked in.
    roles = list(PingRole.objects.filter(pk__in=pks))
    by_pk = {role.pk: role for role in roles}
    errors = []
    for pk in pks:
        role = by_pk.get(pk)
        if role is None:
            errors.append(ValidationError("A selected role is no longer available.", code="unavailable"))
        elif not role.is_active:
            errors.append(
                ValidationError("Role %(name)s is no longer available.", code="unavailable", params={"name": role.name})
            )
    if errors:
        raise ValidationError(errors)
    return MentionSelection(ROLES, _unique_roles(_role_ref(role.role_id, role.name) for role in roles))


def choice_values(selection) -> list[str]:
    """Form values for a stored selection; roles that no longer exist are left out."""
    if selection.mode != ROLES:
        return [selection.mode] if selection.mode in MENTION_MODES else [NONE]
    return [role_choice(role) for role in PingRole.objects.filter(role_id__in=_database_ids(selection.roles))]


def snapshot_roles(selection) -> list[dict]:
    """JSON snapshot stored in ``FleetOperation.ping_roles``."""
    if selection.mode != ROLES:
        return []
    return [{"id": role.id, "name": role.name} for role in selection.roles]


def _database_ids(roles) -> list[int]:
    # Snapshot ids are digit strings; ids a BigIntegerField cannot hold cannot match any row.
    return [int(role.id) for role in roles if int(role.id) <= MAX_DATABASE_ID]


def _channel_name(channel) -> str:
    return getattr(channel, "name", "") or str(channel)


def _allowed_role_ids(channel) -> set[str]:
    roles = getattr(channel, "allowed_roles", None)
    if roles is None:
        return set()
    try:
        roles = roles.all() if hasattr(roles, "all") else roles
        return {str(role.role_id) for role in roles}
    except ValueError:
        # Unsaved channel: no M2M rows yet.
        return set()


def validate_for_channel(selection, channel) -> list[str]:
    """Problems that stop this selection from being sent to ``channel``; [] when it may be sent.

    Without a channel the ping is only copied, so any selection is fine.
    """
    if channel is None:
        return []
    name = _channel_name(channel)
    if selection.mode == EVERYONE:
        return [] if getattr(channel, "allow_everyone", False) else [f"@everyone is not allowed in {name}."]
    if selection.mode == HERE:
        return [] if getattr(channel, "allow_here", False) else [f"@here is not allowed in {name}."]
    if selection.mode != ROLES or not selection.roles:
        return []

    errors = []
    if len(selection.roles) > MAX_ROLE_MENTIONS:
        errors.append(f"Select at most {MAX_ROLE_MENTIONS} roles; Discord cannot mention more in one message.")
    active = PingRole.objects.filter(role_id__in=_database_ids(selection.roles), is_active=True)
    active_ids = {str(role_id) for role_id in active.values_list("role_id", flat=True)}
    # No allowed roles configured means every active role may be mentioned.
    allowed_ids = _allowed_role_ids(channel)
    for role in selection.roles:
        label = role.name or role.id
        if role.id not in active_ids:
            errors.append(f"Role {label} is no longer available.")
        elif allowed_ids and role.id not in allowed_ids:
            errors.append(f"Role {label} is not allowed in {name}.")
    return errors


def role_delete_refusal(role) -> str:
    """Why ``role`` must not be deleted, or ""."""
    channels = list(role.channels.values_list("name", flat=True)) if role.pk else []
    if not channels:
        return ""
    # Removing the last allowed role would let the channel ping every active role.
    return (
        f"This role is allowed in the ping channels {', '.join(channels)}. Remove it from those channels "
        "first, or disable it instead."
    )


def mention_text(selection, *, display=False) -> str:
    """The mention as sent (``<@&id>``) or, with ``display``, as people read it (``@Name``)."""
    if selection.mode == EVERYONE:
        return "@everyone"
    if selection.mode == HERE:
        return "@here"
    if selection.mode == ROLES:
        if display:
            return " ".join(f"@{role.name or 'role ' + role.id}" for role in selection.roles)
        return " ".join(f"<@&{role.id}>" for role in selection.roles)
    return ""


def target_label(selection) -> str:
    """Who the ping went to, in words, for the message footer."""
    if selection.mode == EVERYONE:
        return "all"
    if selection.mode == HERE:
        return "online members"
    if selection.mode == ROLES and selection.roles:
        return ", ".join(role.name or f"role {role.id}" for role in selection.roles)
    return "this channel"


def allowed_mentions(selection) -> dict:
    """Discord ``allowed_mentions`` that lets exactly the selected targets be notified."""
    if selection.mode in (EVERYONE, HERE):
        # Discord has no separate switch for @here; "everyone" covers both.
        return {"parse": ["everyone"]}
    if selection.mode == ROLES:
        return {"parse": [], "roles": [role.id for role in selection.roles]}
    return {"parse": []}


def neutralize_mass_mentions(text: str) -> str:
    """Break ``@everyone``/``@here`` with a zero-width space so Discord does not treat them as mentions."""
    return _MASS_MENTION_RE.sub(rf"@{_ZERO_WIDTH_SPACE}\1", text or "")


def limit_mass_mentions(text: str, selection) -> str:
    """``text`` as it may be sent with ``allowed_mentions(selection)``.

    Discord's "everyone" switch also lets ``@everyone`` notify in an ``@here`` ping, so a stray
    ``@everyone`` (e.g. in a ping stored before mentions were neutralised) is broken.
    """
    text = text or ""
    if selection.mode == HERE:
        return text.replace("@everyone", f"@{_ZERO_WIDTH_SPACE}everyone")
    return text


def discord_length(text: str) -> int:
    """Length as Discord counts it (UTF-16 code units), so emoji never push a ping over the limit unseen."""
    return len((text or "").encode("utf-16-le")) // 2
