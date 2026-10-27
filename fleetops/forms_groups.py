# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the group pages: the group access rows of FleetOps Administration and the Send Ping page.

Who leads a group and which channels a user may post to is decided in ``services.group_access``. These forms
only offer what those rules allow and check the choice again when they are sent; the Send Ping service checks it
once more right before the post (a crafted form, or a change made in Alliance Auth in between).
"""

from django import forms
from django.contrib.auth.models import Group
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import formats, timezone
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext, pgettext_lazy

from fleetops import texts
from fleetops.forms_common import style_fields
from fleetops.models import CommsPreset, DiscordWebhook, FleetType, GroupAccess, MessageTemplate, PingRole
from fleetops.providers.doctrines import get_doctrines
from fleetops.services import group_access
from fleetops.services.mentions import (
    DISCORD_MESSAGE_LIMIT,
    EVERYONE,
    HERE,
    NONE,
    role_choice,
    selection_from_choices,
    validate_for_channel,
)
from fleetops.services.periods import EVE_CLOCK_PATTERN, parse_eve_datetime
from fleetops.ui import TONE_INFO, TONE_NEUTRAL, TONE_WARN, bool_tone, pill_html

FLEETS_NEED_TYPE = _("A group that runs fleets needs at least one fleet type.")
GROUP_FOR_PING_REQUIRED = _("Choose the group this ping is for.")
FLEET_TIME_INCOMPLETE = _("Enter the fleet date and time in EVE time (UTC).")
FLEET_TIME_PAST = _("The fleet time must be in the future (EVE time).")
# At most this many groups are shown as a segmented choice; more become a list.
SEGMENTED_GROUPS = 4


# --- FleetOps Administration: Groups ------------------------------------------------------------------------


def fleet_type_label(fleet_type) -> str:
    """"StratOp — 2.00 FC points": a fleet type with the FC points one fleet of the type gives."""
    label = _("%(name)s — %(points)s FC points") % {
        "name": fleet_type.name,
        "points": formats.number_format(fleet_type.point_weight, 2),
    }
    if not fleet_type.is_active:
        label = _("%(label)s (switched off)") % {"label": label}
    return label


class FleetTypeChoiceField(forms.ModelMultipleChoiceField):
    """Fleet types labelled with their FC points, so the administrators see what a group fleet is worth."""

    def label_from_instance(self, obj):
        return fleet_type_label(obj)


class GroupAccessForm(forms.ModelForm):
    """What the leaders of one Alliance Auth group may do in FleetOps.

    The leaders themselves are read from Alliance Auth and cannot be set here. ``warnings`` are shown after a
    save; ``audit_extra`` adds the group's name to the audit entry.
    """

    class Meta:
        model = GroupAccess
        fields = ["group", "mode", "fleet_types", "srp_enabled", "is_active", "note"]
        field_classes = {"fleet_types": FleetTypeChoiceField}
        widgets = {"fleet_types": forms.CheckboxSelectMultiple}
        labels = {"group": _("Alliance Auth group")}
        help_texts = {
            "group": _(
                "Its leaders are the group leaders and the members of the leader groups set in Alliance Auth."
            ),
            "mode": _("Fleets with PAP include pings. The fleet types are kept when you switch back to pings only."),
            "is_active": _("While it is off, the leaders of the group can do nothing in FleetOps."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warnings = []
        self.audit_extra = {}
        # One row per group: groups that have a row already are not offered again (the row's own group is).
        configured = GroupAccess.objects.exclude(pk=self.instance.pk).values("group_id")
        self.fields["group"].queryset = Group.objects.exclude(pk__in=configured).order_by("name")
        # Switched-off fleet types only while the row still has them, so they can be unticked.
        selected = self.instance.fleet_types.values("pk") if self.instance.pk else FleetType.objects.none()
        self.fields["fleet_types"].queryset = FleetType.objects.filter(Q(is_active=True) | Q(pk__in=selected))
        style_fields(self.fields)

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("group")
        if cleaned.get("mode") == GroupAccess.Mode.FLEETS:
            active = [fleet_type for fleet_type in cleaned.get("fleet_types") or () if fleet_type.is_active]
            if not active:
                self.add_error("fleet_types", FLEETS_NEED_TYPE)
            weighted = [fleet_type.name for fleet_type in active if fleet_type.point_weight > 0]
            if weighted:
                self.warnings.append(
                    _("Fleets of type %(types)s give FC points; group leaders who run them get FC credit.")
                    % {"types": ", ".join(weighted)}
                )
        if group is not None:
            self.audit_extra = {"group": group.name}
            if not DiscordWebhook.objects.filter(is_active=True, allowed_groups=group).exists():
                self.warnings.append(
                    _("No ping channel is assigned to this group yet. Assign one under Ping Channels.")
                )
        return cleaned


def _admin_group_url(group_id) -> str:
    """The Alliance Auth admin page of a group (where its leaders are set); "" when the admin has none."""
    for name in ("admin:groupmanagement_group_change", "admin:auth_group_change"):
        try:
            return reverse(name, args=[group_id])
        except NoReverseMatch:
            continue
    return ""


def _channels_by_group(objects, group_ids) -> dict:
    """``{group_id: [channel, ...]}`` from the list page's prefetch, else with one query."""
    prefetched = all(
        "fleetops_ping_channels" in getattr(obj.group, "_prefetched_objects_cache", {}) for obj in objects
    )
    if prefetched:
        return {obj.group_id: list(obj.group.fleetops_ping_channels.all()) for obj in objects}
    result = {group_id: [] for group_id in group_ids}
    through = DiscordWebhook.allowed_groups.through.objects.filter(group_id__in=group_ids).select_related(
        "discordwebhook"
    )
    for row in through.order_by("discordwebhook__sort_order", "discordwebhook__name"):
        result.setdefault(row.group_id, []).append(row.discordwebhook)
    return result


def group_list_prepare(objects) -> None:
    """Fill in what the list cells and the leaders card of ``objects`` need, with a fixed number of queries.

    Each row gets ``fleetops_leaders`` (all leaders, ``LeaderRow``), ``fleetops_direct_leaders``,
    ``fleetops_leader_groups`` (``[(leader group name, [LeaderRow, ...])]``), ``fleetops_channels`` and
    ``fleetops_admin_url``.
    """
    objects = list(objects)
    if not objects:
        return
    group_ids = [obj.group_id for obj in objects]
    leaders = group_access.leaders_of(group_ids)
    channels = _channels_by_group(objects, group_ids)
    for obj in objects:
        rows = leaders.get(obj.group_id, [])
        via = {}
        for row in rows:
            if row.via_group:
                via.setdefault(row.via_group, []).append(row)
        obj.fleetops_leaders = rows
        obj.fleetops_direct_leaders = [row for row in rows if not row.via_group]
        obj.fleetops_leader_groups = sorted(via.items(), key=lambda item: item[0].casefold())
        obj.fleetops_channels = channels.get(obj.group_id, [])
        obj.fleetops_admin_url = _admin_group_url(obj.group_id)


def _yes_no(value):
    return pill_html(bool_tone(value), pgettext("FleetOps", "Yes") if value else pgettext("FleetOps", "No"))


# A cell lists several names: each stays on one line, the list wraps between them.
def _channel_links(channels):
    links = []
    for channel in channels:
        url = reverse("fleetops:configuration_edit", args=["ping-channels", channel.pk])
        if channel.is_active:
            links.append(format_html('<span class="text-nowrap"><a href="{}">{}</a></span>', url, channel.name))
        else:
            links.append(
                format_html(
                    '<span class="text-nowrap"><a href="{}">{}</a> <span class="text-muted">({})</span></span>',
                    url,
                    channel.name,
                    pgettext("FleetOps", "Disabled"),
                )
            )
    return format_html_join(", ", "{}", ((link,) for link in links))


def _leader_names(rows):
    names = []
    for row in rows:
        if row.has_access:
            names.append(format_html('<span class="text-nowrap">{}</span>', row.name))
        else:
            names.append(
                format_html(
                    '<span class="text-nowrap">{} <span class="text-muted">({})</span></span>',
                    row.name,
                    _("no FleetOps access"),
                )
            )
    return format_html_join(", ", "{}", ((name,) for name in names))


def group_cell(obj, field_name):
    """The value of a list cell of the Groups section; ``NotImplemented`` leaves it to the generic rules.

    The group name, the mode and the fleet types break only between words, whatever the table around them allows.
    Mode, SRP and active are pills; fleet types are pills with their FC points; channels link to their edit
    page; leaders are the main names from Alliance Auth. A row without channels or leaders says so with a
    warning pill.
    """
    if field_name == "group":
        return format_html('<span class="fo-group-cell fo-group-name">{}</span>', obj.group.name)
    if field_name == "mode":
        tone = TONE_INFO if obj.mode == GroupAccess.Mode.FLEETS else TONE_NEUTRAL
        return pill_html(tone, obj.get_mode_display(), class_="fo-pill-wrap fo-group-mode")
    if field_name == "fleet_types":
        if obj.mode != GroupAccess.Mode.FLEETS:
            return ""
        types = list(obj.fleet_types.all())
        if not types:
            return pill_html(TONE_WARN, _("No fleet type"))
        pills = (pill_html(TONE_NEUTRAL, fleet_type_label(item), class_="fo-pill-wrap") for item in types)
        return format_html(
            '<span class="fo-group-cell fo-group-types">{}</span>',
            format_html_join(" ", "{}", ((pill,) for pill in pills)),
        )
    if field_name == "channels":
        channels = getattr(obj, "fleetops_channels", None)
        if channels is None:
            channels = list(DiscordWebhook.objects.filter(allowed_groups=obj.group_id))
        if not any(channel.is_active for channel in channels):
            warning = pill_html(TONE_WARN, _("No channel"))
            return format_html("{} {}", _channel_links(channels), warning) if channels else warning
        return _channel_links(channels)
    if field_name == "leaders":
        rows = getattr(obj, "fleetops_leaders", None)
        if rows is None:
            rows = group_access.leaders_of([obj.group_id]).get(obj.group_id, [])
        if not rows:
            return pill_html(TONE_WARN, _("No leaders in Alliance Auth"))
        return _leader_names(rows)
    if field_name in ("srp_enabled", "is_active"):
        return _yes_no(getattr(obj, field_name))
    return NotImplemented


# --- Send Ping ----------------------------------------------------------------------------------------------


class SendPingForm(forms.Form):
    """A Discord ping without a fleet: for FCs (alliance channels) and the leaders of configured groups.

    ``group`` is absent for an FC who leads no group, a fixed hidden value for a leader of one group who is no
    FC, and a choice otherwise ("Alliance ping" only for FCs). The channels are the ones the user may post to;
    ``clean()`` checks the pair of group and channel and the channel's mention rules.
    """

    request_id = forms.UUIDField(widget=forms.HiddenInput())
    group = forms.ModelChoiceField(
        label=pgettext_lazy("FleetOps", "Group"), queryset=Group.objects.none(), required=False
    )
    ping_channel = forms.ModelChoiceField(
        label=_("Ping channel"),
        queryset=DiscordWebhook.objects.none(),
        empty_label=_("Choose a channel"),
    )
    ping_mentions = forms.MultipleChoiceField(
        label=_("Ping target"), required=False, widget=forms.CheckboxSelectMultiple, initial=[NONE]
    )
    ping_template = forms.ModelChoiceField(
        label=_("Ping template"),
        queryset=MessageTemplate.objects.none(),
        required=False,
        empty_label=_("Default template"),
    )
    additional_message = forms.CharField(
        label=_("Message"),
        max_length=DISCORD_MESSAGE_LIMIT,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    formup = forms.CharField(label=_("Form Up / Staging"), max_length=255, required=False)
    doctrine_choice = forms.ChoiceField(label=pgettext_lazy("FleetOps", "Doctrine"), required=False)
    custom_doctrine = forms.CharField(label=_("Custom Doctrine"), required=False, max_length=255)
    comms = forms.ModelChoiceField(label=_("Comms"), queryset=CommsPreset.objects.none(), required=False)
    fleet_date = forms.DateField(
        label=_("Date"),
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    fleet_clock = forms.CharField(
        label=_("Time (HH:MM)"),
        required=False,
        max_length=5,
        widget=forms.TextInput(
            attrs={"pattern": EVE_CLOCK_PATTERN, "placeholder": "HH:MM", "inputmode": "numeric", "autocomplete": "off"}
        ),
    )

    # The fields under "More settings": the box opens when one of them is filled in or has an error.
    MORE_SETTINGS = ("formup", "doctrine_choice", "custom_doctrine", "comms", "fleet_date", "fleet_clock")

    def __init__(self, *args, user, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        self.user = user
        self.is_fc = user.has_perm("fleetops.start_fleet")
        self.grants = group_access.leader_access(user).grants
        self.fixed_group = None
        self._set_up_group()
        alliance = group_access.alliance_channels(user) if self.is_fc else DiscordWebhook.objects.none()
        self.alliance_channel_ids = set(alliance.values_list("pk", flat=True))
        self.group_channel_ids = {}
        if self.grants:
            through = DiscordWebhook.allowed_groups.through.objects.filter(
                group_id__in=[grant.group_id for grant in self.grants], discordwebhook__is_active=True
            )
            for group_id, channel_id in through.values_list("group_id", "discordwebhook_id"):
                self.group_channel_ids.setdefault(group_id, set()).add(channel_id)
        channel_ids = self.alliance_channel_ids | set().union(*self.group_channel_ids.values())
        self.has_channels = bool(channel_ids)
        self.fields["ping_channel"].queryset = DiscordWebhook.objects.filter(pk__in=channel_ids, is_active=True)
        if not self.is_bound and not self.initial.get("ping_channel") and len(channel_ids) == 1:
            # One channel only (a leader of one group, usually): it is chosen already.
            self.initial["ping_channel"] = next(iter(channel_ids))
        self.fields["ping_template"].queryset = MessageTemplate.objects.filter(
            is_active=True, template_type=MessageTemplate.TemplateType.STANDALONE
        )
        self.fields["comms"].queryset = CommsPreset.objects.filter(is_active=True)
        self.fields["ping_mentions"].choices = [(NONE, _("No ping")), (EVERYONE, "@everyone"), (HERE, "@here")] + [
            (role_choice(role), f"@{role.name}") for role in PingRole.objects.filter(is_active=True)
        ]
        doctrines = [("", _("None / Custom"))]
        for doctrine in get_doctrines(user):
            doctrines.append((f"{doctrine.source}|{doctrine.external_id}|{doctrine.name}", doctrine.name))
        self.fields["doctrine_choice"].choices = doctrines
        choice = self.initial.get("doctrine_choice")
        if choice and choice not in dict(doctrines):
            # A doctrine that is no longer offered keeps its name as a custom doctrine ("Use again").
            self.initial["doctrine_choice"] = ""
            self.initial["custom_doctrine"] = choice.split("|", 2)[-1]
        if not self.is_bound and "ping_template" not in self.initial:
            default = next((t for t in self.fields["ping_template"].queryset if t.is_default), None)
            if default is not None:
                self.initial["ping_template"] = default.pk
        style_fields(self.fields)

    def _set_up_group(self):
        """The group choice: none for a pure FC, fixed for a leader of one group, a choice otherwise."""
        field = self.fields["group"]
        if not self.grants:
            del self.fields["group"]
            return
        group_ids = [grant.group_id for grant in self.grants]
        field.queryset = Group.objects.filter(pk__in=group_ids).order_by("name")
        # A prefilled group the user no longer leads is replaced by the first choice.
        if self.initial.get("group") not in group_ids:
            self.initial["group"] = "" if self.is_fc else group_ids[0]
        if not self.is_fc and len(self.grants) == 1:
            self.fixed_group = self.grants[0]
            field.widget = forms.HiddenInput()
            field.required = True
            return
        options = len(self.grants) + (1 if self.is_fc else 0)
        if self.is_fc:
            field.empty_label = _("Alliance ping")
        else:
            field.empty_label = None
            field.required = True
        if options <= SEGMENTED_GROUPS:
            field.widget = forms.RadioSelect()
            field.widget.choices = field.choices

    @property
    def more_settings_open(self) -> bool:
        """Open "More settings" when one of its fields has an error or a value other than the default."""
        if any(name in self.errors for name in self.MORE_SETTINGS):
            return True
        for name in self.MORE_SETTINGS:
            value = self[name].value()
            if value not in (None, "", []):
                return True
        return False

    def group_rules(self) -> dict:
        """The channels each choice of group offers, for the page's script: ``""`` is the alliance ping."""
        rules = {}
        if self.is_fc:
            rules[""] = sorted(self.alliance_channel_ids)
        for grant in self.grants:
            rules[str(grant.group_id)] = sorted(self.group_channel_ids.get(grant.group_id, ()))
        return rules

    def channel_rules(self) -> dict:
        """Mentions each ping channel allows, keyed by channel pk; ``roles`` None allows every active role."""
        rules = {}
        for channel in self.fields["ping_channel"].queryset.prefetch_related("allowed_roles"):
            roles = [role.pk for role in channel.allowed_roles.all()]
            rules[str(channel.pk)] = {
                "everyone": channel.allow_everyone,
                "here": channel.allow_here,
                "roles": roles or None,
            }
        return rules

    def clean_fleet_clock(self):
        return (self.cleaned_data.get("fleet_clock") or "").strip()

    def clean_additional_message(self):
        return (self.cleaned_data.get("additional_message") or "").strip()

    def clean(self):
        data = super().clean()
        group = data.get("group") if "group" in self.fields else None
        data["group"] = group
        if group is None and not self.is_fc and "group" not in self.errors:
            self.add_error("group" if "group" in self.fields else None, GROUP_FOR_PING_REQUIRED)
        channel = data.get("ping_channel")
        if channel is not None and (group is not None or self.is_fc):
            refusal = group_access.channel_refusal(self.user, channel, group_id=group.pk if group else None)
            if refusal:
                self.add_error("ping_channel", texts.translate_stored(refusal))
        self._clean_mentions(data, channel)
        self._clean_doctrine(data)
        self._clean_fleet_time(data)
        return data

    def _clean_mentions(self, data, channel):
        if "ping_mentions" in self.errors:
            return
        try:
            selection = selection_from_choices(data.get("ping_mentions") or [])
        except forms.ValidationError as exc:
            self.add_error("ping_mentions", exc)
            return
        data["mention_selection"] = selection
        for message in validate_for_channel(selection, channel):
            self.add_error("ping_mentions", texts.translate_stored(message))

    def _clean_doctrine(self, data):
        choice = data.get("doctrine_choice") or ""
        custom = (data.get("custom_doctrine") or "").strip()
        data["doctrine_source"], data["doctrine_external_id"], data["doctrine_name"] = "", "", custom
        if choice:
            try:
                source, external_id, name = choice.split("|", 2)
            except ValueError:
                self.add_error("doctrine_choice", _("Invalid doctrine selection."))
                return
            data["doctrine_source"], data["doctrine_external_id"], data["doctrine_name"] = source, external_id, name
        elif custom:
            data["doctrine_source"] = "custom"

    def _clean_fleet_time(self, data):
        """``fleet_time`` from the optional EVE date and time; it has to lie in the future."""
        data["fleet_time"] = None
        if "fleet_date" in self.errors or "fleet_clock" in self.errors:
            return
        day, clock = data.get("fleet_date"), data.get("fleet_clock")
        if not day and not clock:
            return
        if not day or not clock:
            self.add_error("fleet_date" if not day else "fleet_clock", FLEET_TIME_INCOMPLETE)
            return
        try:
            fleet_time = parse_eve_datetime(day, clock)
        except ValueError as exc:
            self.add_error("fleet_clock", forms.ValidationError(str(exc), code="invalid"))
            return
        if fleet_time <= timezone.now():
            self.add_error("fleet_clock", FLEET_TIME_PAST)
            return
        data["fleet_time"] = fleet_time
