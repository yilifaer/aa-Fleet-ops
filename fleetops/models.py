# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

import re
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.template import Template, TemplateSyntaxError
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from fleetops.constants import MAX_ISK
from fleetops.services.fleet_rules import (
    DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS,
    FleetRuleError,
    format_fleet_roles,
    parse_fleet_roles,
    parse_id_list,
    parse_structure_layout,
)
from fleetops.texts import END_BOSS_HANDOVER, END_FLEET_GONE, END_MANUAL, translate_stored
from fleetops.validators import (  # noqa: F401 - VOICE_URL_SCHEMES re-exported
    INGAME_CHANNEL_ID_RE,
    VOICE_URL_SCHEMES,
    validate_voice_url,
)

MIN_RETENTION_DAYS = 365
MAX_RETENTION_DAYS = 36500
# Largest value a BigIntegerField id can hold.
MAX_DATABASE_ID = 2**63 - 1
# Highest PAP multiplier a fleet or a multiplier tier can have.
MAX_MULTIPLIER = 10
# Live fleet refresh while a manager watches the Fleet tab: 0 (off) or this many seconds.
MIN_LIVE_REFRESH_SECONDS = 10
MAX_LIVE_REFRESH_SECONDS = 300

# https://discord.com/api[/v<n>]/webhooks/<id>/<token>, optionally with a trailing slash and a query
# such as ?wait=true&thread_id=1. Suffixed endpoints (/slack, /github) are not accepted.
DISCORD_WEBHOOK_URL_RE = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+/?(?:\?[\w=&-]*)?",
    re.ASCII,
)


def validate_alliance_ids(value):
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part and not (part.isascii() and part.isdigit() and 0 < int(part) <= 2147483647):
            raise ValidationError(
                _(
                    "“%(value)s” is not a valid alliance ID. Enter numeric alliance IDs separated by commas or "
                    "semicolons."
                ),
                code="invalid",
                params={"value": part},
            )


def validate_discord_webhook_url(value):
    # Never echo the URL back: it carries the webhook token.
    if not DISCORD_WEBHOOK_URL_RE.fullmatch(value or ""):
        raise ValidationError(
            _("Enter a Discord webhook URL in the form https://discord.com/api/webhooks/<id>/<token>."),
            code="invalid",
        )


def validate_template_syntax(value):
    try:
        Template(value)
    except TemplateSyntaxError as exc:
        raise ValidationError(_("Invalid template syntax: %(error)s"), code="invalid", params={"error": exc})


def validate_id_list(value):
    """EVE IDs separated by commas, semicolons or spaces, such as the ship groups excluded from bridges."""
    try:
        parse_id_list(value)
    except FleetRuleError as exc:
        raise ValidationError(str(exc), code="invalid")


def validate_fleet_roles(value):
    try:
        parse_fleet_roles(value)
    except FleetRuleError as exc:
        raise ValidationError(str(exc), code="invalid")


def validate_structure_layout(value):
    try:
        parse_structure_layout(value)
    except FleetRuleError as exc:
        raise ValidationError(str(exc), code="invalid")


class ReviewStatus(models.TextChoices):
    """FC lead review of manual fleets and manual PAP; shared by fleets and PAP rows."""

    APPROVED = "approved", pgettext_lazy("FleetOps", "Approved")
    PENDING = "pending", _("Pending approval")
    REJECTED = "rejected", pgettext_lazy("FleetOps", "Rejected")


class FleetOpsSettings(models.Model):
    class MultiplierMode(models.TextChoices):
        FC = "fc", _("FC chooses when ending")
        AUTO = "auto", _("Automatic by fleet duration")

    class LeaderboardVisibility(models.TextChoices):
        MEMBERS = "members", _("Everyone with FleetOps access")
        MANAGERS = "managers", _("Statistics managers only")

    attendance_limit = models.PositiveIntegerField(
        _("PAP characters per player"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text=_("Maximum characters of one Alliance Auth user that receive PAP in one fleet. Empty = unlimited."),
    )
    multiplier_mode = models.CharField(
        _("PAP multiplier mode"),
        max_length=10,
        choices=MultiplierMode.choices,
        default=MultiplierMode.FC,
    )
    pap_minimum_minutes = models.PositiveIntegerField(
        _("PAP minimum time"),
        default=0,
        validators=[MaxValueValidator(1440)],
        help_text=_("Minutes a character must accumulate in fleet to receive PAP. 0 = any presence."),
    )
    minimum_fleet_minutes = models.PositiveIntegerField(
        _("Minimum fleet duration"),
        default=0,
        validators=[MaxValueValidator(1440)],
        help_text=_(
            "Fleets shorter than this are not counted in statistics and incentives unless included "
            "manually. 0 = count all."
        ),
    )
    pap_link_minutes = models.PositiveIntegerField(
        _("PAP link validity"),
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(1440)],
        help_text=_("Minutes a new PAP link stays open unless the FC chooses another time."),
    )
    tracking_interval = models.PositiveIntegerField(
        verbose_name=_("Tracking interval"),
        default=60,
        validators=[MinValueValidator(30)],
    )
    stale_threshold = models.PositiveIntegerField(
        verbose_name=_("Stale threshold"),
        default=180,
        validators=[MinValueValidator(60)],
    )
    auto_end_enabled = models.BooleanField(verbose_name=_("Auto end enabled"), default=True)
    auto_end_missing_count = models.PositiveIntegerField(
        verbose_name=_("Auto end missing count"),
        default=3,
        validators=[MinValueValidator(2)],
    )
    incentive_enabled = models.BooleanField(verbose_name=_("Incentive enabled"), default=False)
    incentive_minimum_fleets = models.PositiveIntegerField(
        verbose_name=_("Incentive minimum fleets"),
        default=3,
        validators=[MinValueValidator(1)],
    )
    data_retention_days = models.PositiveIntegerField(
        verbose_name=_("Data retention days"),
        default=MIN_RETENTION_DAYS,
        validators=[MinValueValidator(MIN_RETENTION_DAYS), MaxValueValidator(MAX_RETENTION_DAYS)],
        help_text=_("PAP/event history retention in days. Minimum 365 days, maximum 36500 days (100 years)."),
    )
    history_alliance_ids = models.CharField(
        _("Member alliance IDs"),
        max_length=500,
        blank=True,
        validators=[validate_alliance_ids],
        help_text=_(
            "Comma-separated alliance IDs. When set, statistics, leaderboards and history only include users "
            "whose main character is currently in one of these alliances, and departed members' history can be pruned. "
            "Empty = every Alliance Auth user with a main character."
        ),
    )
    leaderboard_size = models.PositiveSmallIntegerField(
        _("Leaderboard places"),
        default=20,
        validators=[MinValueValidator(3), MaxValueValidator(100)],
        help_text=_("Places shown on PAP leaderboards. Members tied on the last place are all shown."),
    )
    leaderboard_visibility = models.CharField(
        _("Leaderboard visibility"),
        max_length=10,
        choices=LeaderboardVisibility.choices,
        default=LeaderboardVisibility.MEMBERS,
        help_text=_(
            "Who sees the PAP leaderboard. Everyone with FleetOps access sees the alliance board and their "
            "own corporation's board; statistics managers see the boards their statistics permission covers."
        ),
    )
    srp_auto_create = models.BooleanField(
        verbose_name=_("SRP auto create"),
        default=True,
        help_text=_("Automatically create/link an SRP fleet when a supported SRP provider is available."),
    )
    srp_provider = models.CharField(
        verbose_name=_("SRP provider"),
        max_length=50,
        default="auto",
        help_text=_("SRP provider. 'auto' uses FleetOps SRP when it is enabled, then aa-srp, then Alliance Auth SRP."),
    )
    bridge_excluded_group_ids = models.CharField(
        _("Ship groups without bridge"),
        max_length=500,
        blank=True,
        default=DEFAULT_BRIDGE_EXCLUDED_GROUP_IDS,
        validators=[validate_id_list],
        help_text=_(
            "SDE ship group IDs that cannot take a jump bridge. Their mass is not counted as bridgeable. "
            "Default: Titan, Dreadnought, Freighter, Carrier, Supercarrier, Rorqual, Jump Freighter, Industrial "
            "Command Ship, Force Auxiliary, Lancer Dreadnought, Command Carrier."
        ),
    )
    fleet_live_refresh_seconds = models.PositiveIntegerField(
        _("Live fleet refresh"),
        default=15,
        validators=[MaxValueValidator(MAX_LIVE_REFRESH_SECONDS)],
        help_text=_(
            "While someone who can manage a fleet has its Fleet tab open, read the fleet from ESI this often "
            "(seconds). 0 = only the tracking interval and the Refresh button."
        ),
    )
    motd_warning_length = models.PositiveIntegerField(
        _("MOTD warning length"),
        default=2000,
        validators=[MaxValueValidator(100000)],
        help_text=_(
            "Warn in the start preview when the MOTD, tags included, is longer than this many characters. "
            "The game's limit is not documented; MOTDs of about 1,700 characters are known to work. "
            "0 = no warning."
        ),
    )

    class Meta:
        verbose_name = _("FleetOps setting")
        verbose_name_plural = _("FleetOps settings")
        permissions = [
            ("basic_access", "Can access FleetOps"),
            ("start_fleet", "Can start fleets"),
            ("manage_own_fleet", "Can manage own fleets"),
            ("view_all_fleets", "Can view all fleet records"),
            ("create_manual_fleet", "Can create manual fleet records"),
            ("manage_fleets", "Can manage all fleets"),
            ("view_corp_stats", "Can view own corporation statistics"),
            ("view_all_stats", "Can view all FleetOps statistics"),
            ("manage_attendance", "Can manage attendance"),
            ("manage_incentives", "Can manage FC incentives"),
            ("manage_configuration", "Can manage FleetOps configuration"),
            ("view_audit_log", "Can view FleetOps audit log"),
            ("request_srp", "Can request SRP"),
            ("manage_srp", "Can review and pay SRP claims"),
        ]

    def __str__(self):
        return gettext("FleetOps Settings")

    def clean(self):
        super().clean()
        # Every live refresh is an ESI read with the FC's token; shorter than ESI's own cache is pointless.
        seconds = self.fleet_live_refresh_seconds
        if seconds and seconds < MIN_LIVE_REFRESH_SECONDS:
            raise ValidationError(
                {
                    "fleet_live_refresh_seconds": _(
                        "Use 0 to turn the live refresh off, or at least %(seconds)s seconds."
                    )
                    % {"seconds": MIN_LIVE_REFRESH_SECONDS}
                }
            )

    @property
    def bridge_excluded_groups(self):
        """The excluded ship group IDs as a set; an unreadable stored value counts as none."""
        try:
            return set(parse_id_list(self.bridge_excluded_group_ids))
        except FleetRuleError:
            return set()

    @classmethod
    def get_solo(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class MultiplierTier(models.Model):
    """A PAP multiplier for fleets that ran longer than a threshold; 1x needs no tier."""

    threshold_minutes = models.PositiveIntegerField(
        verbose_name=_("Threshold minutes"),
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(10080)],
        help_text=_("Fleets running strictly longer than this many minutes qualify."),
    )
    multiplier = models.PositiveSmallIntegerField(
        verbose_name=_("Multiplier"),
        validators=[MinValueValidator(2), MaxValueValidator(MAX_MULTIPLIER)],
    )

    class Meta:
        verbose_name = _("multiplier tier")
        verbose_name_plural = _("multiplier tiers")
        ordering = ["threshold_minutes"]

    def __str__(self):
        return gettext("%(multiplier)sx after %(minutes)s min") % {
            "multiplier": self.multiplier,
            "minutes": self.threshold_minutes,
        }


class FleetType(models.Model):
    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, unique=True)
    short_name = models.CharField(verbose_name=_("Short name"), max_length=30, blank=True)
    point_weight = models.DecimalField(
        verbose_name=_("Point weight"),
        max_digits=8,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)
    sort_order = models.PositiveIntegerField(verbose_name=_("Sort order"), default=0)

    class Meta:
        verbose_name = _("fleet type")
        verbose_name_plural = _("fleet types")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.short_name or self.name


class CommsPreset(models.Model):
    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, unique=True)
    channel_name = models.CharField(verbose_name=_("Channel name"), max_length=255, blank=True)
    voice_url = models.CharField(
        verbose_name=_("Voice URL"),
        max_length=500,
        blank=True,
        validators=[validate_voice_url],
        help_text=_(
            "Supports mumble://, ts3server://, discord:// and https:// voice links. "
            "Do not include a personal user name or password."
        ),
    )
    description = models.TextField(verbose_name=pgettext_lazy("FleetOps", "Description"), blank=True)
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)
    # Public join links use this instead of the primary key, so they cannot be enumerated.
    join_token = models.UUIDField(verbose_name=_("Join token"), default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        verbose_name = _("comms preset")
        verbose_name_plural = _("comms presets")
        ordering = ["name"]

    def __str__(self):
        return self.name


class ChannelPreset(models.Model):
    class ChannelType(models.TextChoices):
        LOGI = "logi", _("Logi")
        BOOST = "boost", _("Boost")

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100)
    channel_type = models.CharField(verbose_name=_("Channel type"), max_length=20, choices=ChannelType.choices)
    channel_value = models.CharField(_("Channel name"), max_length=255, help_text=_("Channel name shown in the MOTD"))
    ingame_channel_id = models.CharField(
        verbose_name=_("Ingame channel ID"),
        max_length=64,
        blank=True,
        validators=[RegexValidator(INGAME_CHANNEL_ID_RE, _(
            "Enter an in-game channel id such as player_<32 hex digits>."
        ))],
    )
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)

    class Meta:
        verbose_name = _("channel preset")
        verbose_name_plural = _("channel presets")
        ordering = ["channel_type", "name"]
        constraints = [
            models.UniqueConstraint(fields=["channel_type", "name"], name="fleetops_unique_channel_preset")
        ]

    def __str__(self):
        return f"{self.get_channel_type_display()}: {self.name}"


class PingRole(models.Model):
    """A Discord role that FCs may mention in a ping."""

    class Source(models.TextChoices):
        MANUAL = "manual", _("Manual")
        DISCORD = "discord", _("Synced from Discord")

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, help_text=_("Role name without the leading @."))
    role_id = models.BigIntegerField(
        verbose_name=_("Role ID"),
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_DATABASE_ID)],
        help_text=_("Discord role ID."),
    )
    is_active = models.BooleanField(
        verbose_name=_("Is active"),
        default=True,
        help_text=_("FCs can pick this role when they send a ping."),
    )
    source = models.CharField(verbose_name=_("Source"), max_length=10, choices=Source.choices, default=Source.MANUAL)
    missing_from_discord = models.BooleanField(verbose_name=_("Missing from Discord"), default=False)
    last_synced_at = models.DateTimeField(verbose_name=_("Last synced at"), null=True, blank=True)
    sort_order = models.PositiveIntegerField(verbose_name=_("Sort order"), default=0)

    class Meta:
        verbose_name = _("ping role")
        verbose_name_plural = _("ping roles")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return f"@{self.name}"


class DiscordWebhook(models.Model):
    """A Discord channel that receives pings through its webhook."""

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, unique=True)
    webhook_url = models.TextField(verbose_name=_("Webhook URL"), validators=[validate_discord_webhook_url])
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)
    allow_everyone = models.BooleanField(
        verbose_name=_("Allow everyone"),
        default=False,
        help_text=_("FCs may ping @everyone in this channel."),
    )
    allow_here = models.BooleanField(
        verbose_name=_("Allow here"),
        default=False,
        help_text=_("FCs may ping @here in this channel."),
    )
    allowed_roles = models.ManyToManyField(
        PingRole,
        verbose_name=_("Allowed roles"),
        blank=True,
        related_name="channels",
        help_text=_("Leave empty to allow every active role."),
    )
    fcs_allowed = models.BooleanField(
        verbose_name=_("Alliance FCs can use this channel"),
        default=True,
        help_text=_(
            "Untick to reserve the channel for the leaders of the groups below. FC leads (manage_fleets) can "
            "always use it. Only applies while the channel is assigned to at least one group."
        ),
    )
    allowed_groups = models.ManyToManyField(
        "auth.Group",
        verbose_name=pgettext_lazy("FleetOps", "Groups"),
        blank=True,
        related_name="fleetops_ping_channels",
        help_text=_("Leaders of these groups may send pings here."),
    )
    sort_order = models.PositiveIntegerField(verbose_name=_("Sort order"), default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("Ping channel")
        verbose_name_plural = _("Ping channels")

    def __str__(self):
        return self.name


class PingTarget(models.Model):
    """Legacy ping destination, replaced by ping channels and mentions. Kept for fleet history."""

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, unique=True)
    target_value = models.CharField(verbose_name=_("Target value"), max_length=255, blank=True)
    webhook = models.ForeignKey(
        DiscordWebhook,
        verbose_name=_("Webhook"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)

    class Meta:
        verbose_name = _("ping target")
        verbose_name_plural = _("ping targets")
        ordering = ["name"]

    def __str__(self):
        return self.name


class MessageTemplate(models.Model):
    class TemplateType(models.TextChoices):
        PING = "ping", _("Ping")
        MOTD = "motd", _("MOTD")
        STANDALONE = "standalone", _("Ping without fleet")

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100)
    template_type = models.CharField(verbose_name=_("Template type"), max_length=20, choices=TemplateType.choices)
    content = models.TextField(verbose_name=_("Content"), validators=[validate_template_syntax])
    is_default = models.BooleanField(verbose_name=_("Is default"), default=False)
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)

    class Meta:
        verbose_name = _("message template")
        verbose_name_plural = _("message templates")
        ordering = ["template_type", "name"]
        constraints = [
            models.UniqueConstraint(fields=["template_type", "name"], name="fleetops_unique_message_template")
        ]

    def __str__(self):
        return f"{self.get_template_type_display()}: {self.name}"

    def clean(self):
        super().clean()
        # Fleet start ignores inactive templates, so an inactive default would silently drop the live one.
        if self.is_default and not self.is_active:
            raise ValidationError({"is_default": _("A default template must be active.")})

    def save(self, *args, **kwargs):
        # One default per template type, otherwise the fallback template is ambiguous. Only an active
        # template takes over the default. Templates that lost the flag are kept in demoted_defaults
        # so callers can audit them.
        self.demoted_defaults = []
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_default and self.is_active:
                others = MessageTemplate.objects.select_for_update().filter(
                    template_type=self.template_type, is_default=True
                )
                self.demoted_defaults = list(others.exclude(pk=self.pk))
                demoted_pks = [other.pk for other in self.demoted_defaults]
                MessageTemplate.objects.filter(pk__in=demoted_pks).update(is_default=False)
                for other in self.demoted_defaults:
                    other.is_default = False


class FleetOperation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SCHEDULED = "scheduled", _("Scheduled")
        STARTING = "starting", _("Starting")
        ACTIVE = "active", pgettext_lazy("fleet status", "Active")
        ENDING = "ending", _("Ending")
        CLOSED = "closed", pgettext_lazy("fleet status", "Closed")
        CANCELLED = "cancelled", _("Cancelled")
        ERROR = "error", _("Error")

    class EndReason(models.TextChoices):
        MANUAL = "manual", _("Ended manually")
        FLEET_GONE = "fleet_gone", _("Fleet no longer exists or the FC left it")
        BOSS_HANDOVER = "boss_handover", _("Fleet boss handed over")

    class MentionMode(models.TextChoices):
        NONE = "none", _("No ping")
        EVERYONE = "everyone", _("@everyone")
        HERE = "here", _("@here")
        ROLES = "roles", _("Roles")

    class MultiplierSource(models.TextChoices):
        DEFAULT = "default", pgettext_lazy("multiplier source", "Default")
        FC = "fc", _("Chosen by the FC")
        AUTO = "auto", _("By fleet duration")
        OVERRIDE = "override", _("Override by an FC lead")
        MANUAL = "manual", _("Manual fleet")

    class PapRules(models.IntegerChoices):
        # Fleets closed before PAP was granted by time in fleet keep the selection they were closed with.
        LEGACY = 1, _("Legacy")
        TIME_IN_FLEET = 2, _("Time in fleet")

    class StatsMode(models.TextChoices):
        AUTO = "auto", _("Automatic")
        INCLUDE = "include", _("Always count")
        EXCLUDE = "exclude", _("Do not count")

    # Fleets in these states own their ESI fleet; a second start for it returns the same record.
    # Scheduled fleets have no ESI fleet yet.
    OPEN_STATUSES = (Status.STARTING, Status.ACTIVE, Status.ENDING)

    uuid = models.UUIDField(verbose_name=_("UUID"), default=uuid.uuid4, unique=True, editable=False)
    start_request_id = models.UUIDField(
        verbose_name=_("Start request ID"),
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    status = models.CharField(
        verbose_name=_("Status"),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Created by"),
        on_delete=models.PROTECT,
        related_name="fleetops_created_operations",
    )
    fc_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("FC user"),
        on_delete=models.PROTECT,
        related_name="fleetops_fc_operations",
    )
    fc_character_id = models.BigIntegerField(verbose_name=_("FC character ID"), db_index=True)
    fc_character_name = models.CharField(verbose_name=_("FC character name"), max_length=255)
    fc_main_character_id = models.BigIntegerField(
        verbose_name=_("FC main character ID"),
        null=True,
        blank=True,
        db_index=True,
    )
    fc_main_character_name = models.CharField(verbose_name=_("FC main character name"), max_length=255, blank=True)
    fleet_boss_character_id = models.BigIntegerField(verbose_name=_("Fleet boss character ID"), null=True, blank=True)
    esi_fleet_id = models.BigIntegerField(verbose_name=_("ESI fleet ID"), null=True, blank=True, db_index=True)

    fleet_type = models.ForeignKey(FleetType, verbose_name=_("Fleet type"), on_delete=models.PROTECT)
    fleet_point_weight_snapshot = models.DecimalField(
        verbose_name=_("Fleet point weight snapshot"),
        max_digits=8,
        decimal_places=2,
        default=Decimal("0"),
    )
    doctrine_name = models.CharField(verbose_name=_("Doctrine name"), max_length=255, blank=True)
    doctrine_external_id = models.CharField(verbose_name=_("Doctrine external ID"), max_length=255, blank=True)
    doctrine_source = models.CharField(verbose_name=_("Doctrine source"), max_length=50, blank=True)
    formup = models.CharField(verbose_name=_("Formup"), max_length=255)

    comms = models.ForeignKey(CommsPreset, verbose_name=_("Comms"), null=True, blank=True, on_delete=models.SET_NULL)
    logi_channel = models.ForeignKey(
        ChannelPreset,
        verbose_name=_("Logi channel"),
        null=True,
        blank=True,
        related_name="fleetops_logi_operations",
        on_delete=models.SET_NULL,
    )
    boost_channel = models.ForeignKey(
        ChannelPreset,
        verbose_name=_("Boost channel"),
        null=True,
        blank=True,
        related_name="fleetops_boost_operations",
        on_delete=models.SET_NULL,
    )
    # Legacy; new fleets store the ping channel and mention snapshot below.
    ping_target = models.ForeignKey(
        PingTarget,
        verbose_name=_("Ping target"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    ping_channel = models.ForeignKey(
        DiscordWebhook,
        verbose_name=_("Ping channel"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_operations",
    )
    ping_channel_name = models.CharField(verbose_name=_("Ping channel name"), max_length=100, blank=True)
    # The group a group fleet belongs to; empty for alliance fleets. The name is a snapshot that stays when the
    # Alliance Auth group is deleted.
    group = models.ForeignKey(
        "auth.Group",
        verbose_name=pgettext_lazy("FleetOps", "Group"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_operations",
    )
    group_name = models.CharField(verbose_name=_("Group name"), max_length=150, blank=True)
    ping_mention_mode = models.CharField(
        verbose_name=_("Ping mention mode"),
        max_length=10,
        choices=MentionMode.choices,
        default=MentionMode.NONE,
    )
    # Snapshots: [{"id": "<role id>", "name": "Sig A"}] and
    # [{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}].
    ping_roles = models.JSONField(verbose_name=_("Ping roles"), default=list, blank=True)
    doctrine_fits = models.JSONField(verbose_name=_("Doctrine fits"), default=list, blank=True)
    ping_template = models.ForeignKey(
        MessageTemplate,
        verbose_name=_("Ping template"),
        null=True,
        blank=True,
        related_name="fleetops_ping_operations",
        on_delete=models.SET_NULL,
    )
    motd_template = models.ForeignKey(
        MessageTemplate,
        verbose_name=_("MOTD template"),
        null=True,
        blank=True,
        related_name="fleetops_motd_operations",
        on_delete=models.SET_NULL,
    )

    additional_message = models.TextField(verbose_name=_("Additional message"), blank=True)
    ping_text = models.TextField(verbose_name=_("Ping text"), blank=True)
    motd_text = models.TextField(verbose_name=_("MOTD text"), blank=True)
    send_ping = models.BooleanField(
        verbose_name=_("Send ping"),
        default=True,
        help_text=_("If false, the operation tracks PAP without sending a Discord ping."),
    )
    attendance_multiplier = models.PositiveIntegerField(
        verbose_name=_("PAP multiplier"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_MULTIPLIER)],
        help_text=_("Fleet-wide PAP multiplier applied to automatically granted PAP."),
    )
    multiplier_pending = models.BooleanField(
        verbose_name=_("Multiplier pending"),
        default=False,
        db_index=True,
        help_text=_("The fleet ended without a multiplier choice although a higher multiplier was reached."),
    )
    multiplier_source = models.CharField(
        verbose_name=_("Multiplier source"),
        max_length=10,
        choices=MultiplierSource.choices,
        blank=True,
    )
    is_manual = models.BooleanField(
        verbose_name=_("Is manual"),
        default=False,
        help_text=_("Manual fleet record created without ESI fleet detection/tracking."),
    )
    link_only = models.BooleanField(
        verbose_name=_("Link only"),
        default=False,
        help_text=_("Fleet started without ESI; members register through PAP links."),
    )

    # PAP rules this fleet is finalized with; the minimum and the limit are copied from the settings
    # when the fleet starts, so later setting changes never alter it.
    pap_rules = models.PositiveSmallIntegerField(
        verbose_name=_("PAP rules"),
        choices=PapRules.choices,
        default=PapRules.TIME_IN_FLEET,
    )
    pap_minimum_minutes = models.PositiveIntegerField(verbose_name=_("PAP minimum minutes"), null=True, blank=True)
    pap_character_limit = models.PositiveIntegerField(verbose_name=_("PAP character limit"), null=True, blank=True)

    review_status = models.CharField(
        verbose_name=_("Review status"),
        max_length=10,
        choices=ReviewStatus.choices,
        default=ReviewStatus.APPROVED,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Reviewed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_reviewed_operations",
    )
    reviewed_at = models.DateTimeField(verbose_name=_("Reviewed at"), null=True, blank=True)
    review_note = models.CharField(verbose_name=_("Review note"), max_length=255, blank=True)
    review_request_note = models.CharField(verbose_name=_("Resubmission note"), max_length=255, blank=True)

    stats_mode = models.CharField(
        verbose_name=_("Stats mode"),
        max_length=10,
        choices=StatsMode.choices,
        default=StatsMode.AUTO,
    )
    counts_for_stats = models.BooleanField(
        verbose_name=_("Counts for stats"),
        default=True,
        db_index=True,
        help_text=_("Counted in statistics and incentives; follows the minimum fleet duration unless overridden."),
    )
    stats_note = models.CharField(verbose_name=_("Stats note"), max_length=255, blank=True)

    srp_provider = models.CharField(verbose_name=_("SRP provider"), max_length=50, blank=True)
    srp_reference = models.CharField(verbose_name=_("SRP reference"), max_length=255, blank=True)
    srp_url = models.CharField(verbose_name=_("SRP URL"), max_length=1000, blank=True)
    srp_error = models.TextField(verbose_name=_("SRP error"), blank=True)

    scheduled_at = models.DateTimeField(verbose_name=_("Scheduled at"), null=True, blank=True)
    started_at = models.DateTimeField(verbose_name=_("Started at"), null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(verbose_name=_("Ended at"), null=True, blank=True)
    last_esi_update = models.DateTimeField(verbose_name=_("Last ESI update"), null=True, blank=True)
    tracking_enabled = models.BooleanField(verbose_name=_("Tracking enabled"), default=False)
    fleet_missing_count = models.PositiveIntegerField(verbose_name=_("Fleet missing count"), default=0)
    missing_reason = models.CharField(
        verbose_name=_("Missing reason"),
        max_length=20,
        choices=EndReason.choices,
        blank=True,
        help_text=_("Why the latest tracking poll could not read the fleet. Cleared by a successful read."),
    )
    missing_detail = models.CharField(verbose_name=_("Missing detail"), max_length=255, blank=True)
    last_error = models.TextField(verbose_name=_("Last error"), blank=True)
    # Wings and squads from the latest successful read: [{"id": 1, "name": "Main", "squads": [{"id": 2,
    # "name": "Alpha"}]}], in ESI order. Kept after the fleet closed, for the names in its history.
    fleet_wings = models.JSONField(verbose_name=_("Fleet wings"), default=list, blank=True)
    fleet_wings_read_at = models.DateTimeField(verbose_name=_("Fleet wings read at"), null=True, blank=True)

    end_requested_at = models.DateTimeField(verbose_name=_("End requested at"), null=True, blank=True)
    end_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("End requested by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_end_requests",
    )
    end_requested_multiplier = models.PositiveIntegerField(
        verbose_name=_("End requested multiplier"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_MULTIPLIER)],
    )
    end_reason = models.CharField(verbose_name=_("End reason"), max_length=20, choices=EndReason.choices, blank=True)
    end_detail = models.CharField(verbose_name=_("End detail"), max_length=255, blank=True)

    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("fleet operation")
        verbose_name_plural = _("fleet operations")
        ordering = ["-started_at", "-created_at"]
        indexes = [models.Index(fields=["status", "tracking_enabled"], name="fleetops_fl_status_8c6d24_idx")]

    def __str__(self):
        return f"{self.fleet_type} — {self.fc_character_name} ({self.uuid})"

    @property
    def end_reason_text(self):
        return end_reason_text(self.end_reason, self.end_detail)

    @property
    def fc_points(self):
        """FC points of this fleet: fleet type weight times the PAP multiplier."""
        return Decimal(self.fleet_point_weight_snapshot or 0) * int(self.attendance_multiplier or 1)


def end_reason_text(reason, detail=""):
    """How the end of a fleet is shown on its page, in the archive and in the audit log.

    The text follows the active language. What is stored with a fleet is the English message declared in
    ``fleetops.texts``.
    """
    if reason == FleetOperation.EndReason.MANUAL:
        return translate_stored(END_MANUAL())
    if reason == FleetOperation.EndReason.FLEET_GONE:
        return translate_stored(END_FLEET_GONE())
    if reason == FleetOperation.EndReason.BOSS_HANDOVER:
        # The registry recognises the sentence by its fixed words; the name is shown as it is.
        name = detail or gettext("another character")
        return translate_stored(END_BOSS_HANDOVER(name=name))
    return ""


class OperationAction(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", pgettext_lazy("FleetOps", "Pending")
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="actions",
    )
    action = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Action"), max_length=50)
    status = models.CharField(verbose_name=_("Status"), max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(verbose_name=_("Error message"), blank=True)
    attempts = models.PositiveIntegerField(verbose_name=_("Attempts"), default=1)
    executed_at = models.DateTimeField(verbose_name=_("Executed at"), auto_now=True)

    class Meta:
        verbose_name = _("operation action")
        verbose_name_plural = _("operation actions")
        ordering = ["executed_at"]
        constraints = [models.UniqueConstraint(fields=["operation", "action"], name="fleetops_unique_operation_action")]

    def __str__(self):
        return f"{self.operation_id}: {self.action} {self.status}"


class OperationActionAttempt(models.Model):
    """One run of an operation step; the step itself only keeps the latest result."""

    action = models.ForeignKey(
        OperationAction,
        verbose_name=pgettext_lazy("FleetOps", "Action"),
        on_delete=models.CASCADE,
        related_name="history",
    )
    status = models.CharField(verbose_name=_("Status"), max_length=20, choices=OperationAction.Status.choices)
    message = models.TextField(verbose_name=_("Message"), blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = _("operation action attempt")
        verbose_name_plural = _("operation action attempts")
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return gettext("%(action)s: %(status)s at %(time)s") % {
            "action": self.action_id,
            "status": self.status,
            "time": self.created_at,
        }


class FleetMemberState(models.Model):
    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="member_states",
    )
    character_id = models.BigIntegerField(verbose_name=_("Character ID"))
    character_name = models.CharField(verbose_name=_("Character name"), max_length=255)
    main_character_id = models.BigIntegerField(
        verbose_name=_("Main character ID"),
        null=True,
        blank=True,
        db_index=True,
    )
    main_character_name = models.CharField(verbose_name=_("Main character name"), max_length=255, blank=True)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Auth user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_member_states",
    )
    corporation_id = models.BigIntegerField(verbose_name=_("Corporation ID"), null=True, blank=True, db_index=True)
    corporation_name = models.CharField(verbose_name=_("Corporation name"), max_length=255, blank=True)
    alliance_id = models.BigIntegerField(verbose_name=_("Alliance ID"), null=True, blank=True)
    alliance_name = models.CharField(verbose_name=_("Alliance name"), max_length=255, blank=True)
    ship_type_id = models.BigIntegerField(verbose_name=_("Ship type ID"), null=True, blank=True)
    ship_type_name = models.CharField(verbose_name=_("Ship type name"), max_length=255, blank=True)
    solar_system_id = models.BigIntegerField(verbose_name=_("Solar system ID"), null=True, blank=True, db_index=True)
    solar_system_name = models.CharField(verbose_name=_("Solar system name"), max_length=255, blank=True)
    fleet_role = models.CharField(verbose_name=_("Fleet role"), max_length=50, blank=True)
    wing_id = models.BigIntegerField(verbose_name=_("Wing ID"), null=True, blank=True)
    squad_id = models.BigIntegerField(verbose_name=_("Squad ID"), null=True, blank=True)
    station_id = models.BigIntegerField(
        verbose_name=_("Station ID"),
        null=True,
        blank=True,
        help_text=_("Set while the member is docked."),
    )
    takes_fleet_warp = models.BooleanField(verbose_name=_("Takes fleet warp"), null=True, blank=True)
    first_seen = models.DateTimeField(verbose_name=_("First seen"))
    last_seen = models.DateTimeField(verbose_name=_("Last seen"))
    left_at = models.DateTimeField(verbose_name=_("Left at"), null=True, blank=True)
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True, db_index=True)
    accumulated_seconds = models.PositiveIntegerField(
        verbose_name=_("Accumulated seconds"),
        default=0,
        help_text=_("Time in fleet so far."),
    )

    class Meta:
        verbose_name = _("fleet member state")
        verbose_name_plural = _("fleet member states")
        ordering = ["character_name"]
        constraints = [models.UniqueConstraint(fields=["operation", "character_id"], name="fleetops_unique_operation_character_state")]

    def __str__(self):
        return f"{self.character_name} @ {self.operation_id}"


class FleetMemberEvent(models.Model):
    class EventType(models.TextChoices):
        JOIN = "join", _("Join")
        LEAVE = "leave", _("Leave")
        REJOIN = "rejoin", _("Rejoin")
        SHIP_CHANGE = "ship_change", _("Ship change")
        SYSTEM_CHANGE = "system_change", _("System change")
        ROLE_CHANGE = "role_change", _("Role change")
        WING_CHANGE = "wing_change", _("Wing change")
        SQUAD_CHANGE = "squad_change", _("Squad change")

    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="member_events",
    )
    character_id = models.BigIntegerField(verbose_name=_("Character ID"), db_index=True)
    character_name = models.CharField(verbose_name=_("Character name"), max_length=255)
    event_type = models.CharField(
        verbose_name=_("Event type"),
        max_length=30,
        choices=EventType.choices,
        db_index=True,
    )
    old_value = models.CharField(verbose_name=_("Old value"), max_length=255, blank=True)
    new_value = models.CharField(verbose_name=_("New value"), max_length=255, blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("fleet member event")
        verbose_name_plural = _("fleet member events")
        ordering = ["-created_at"]


class AttendanceRecord(models.Model):
    class Source(models.TextChoices):
        AUTOMATIC = "automatic", _("Automatic")
        MANUAL = "manual", _("Manual")
        IMPORTED = "imported", _("Imported")
        CLICK = "click", _("PAP link")

    class Correction(models.TextChoices):
        KEEP = "keep", _("Keep separate")
        REPLACE = "replace", _("Replace tracked PAP")
        MERGE = "merge", _("Merge into tracked PAP")

    # Rows the PAP engine writes from tracking and PAP links; manual and imported rows are written by users.
    MANAGED_SOURCES = (Source.AUTOMATIC, Source.CLICK)
    USER_SOURCES = (Source.MANUAL, Source.IMPORTED)

    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="attendance_records",
    )
    character_id = models.BigIntegerField(verbose_name=_("Character ID"))
    character_name = models.CharField(verbose_name=_("Character name"), max_length=255)
    main_character_id = models.BigIntegerField(
        verbose_name=_("Main character ID"),
        null=True,
        blank=True,
        db_index=True,
    )
    main_character_name = models.CharField(verbose_name=_("Main character name"), max_length=255, blank=True)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Auth user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_attendance",
    )
    corporation_id = models.BigIntegerField(
        verbose_name=_("Corporation ID"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Main character corporation at PAP time."),
    )
    corporation_name = models.CharField(verbose_name=_("Corporation name"), max_length=255, blank=True)
    source = models.CharField(verbose_name=_("Source"), max_length=20, choices=Source.choices)
    attendance_value = models.PositiveIntegerField(verbose_name=_("PAP value"), default=1)
    granted = models.BooleanField(verbose_name=_("Granted"), default=True, db_index=True)
    capped = models.BooleanField(verbose_name=_("Capped"), default=False)
    below_minimum = models.BooleanField(
        verbose_name=_("Below minimum"),
        default=False,
        help_text=_("The character did not reach the PAP minimum time."),
    )
    replaced = models.BooleanField(
        verbose_name=_("Replaced"),
        default=False,
        help_text=_("Replaced by an approved manual PAP."),
    )
    # Merged manual PAP on top of the multiplier; legacy_extra_value holds merges made before the upgrade.
    extra_value = models.PositiveIntegerField(verbose_name=_("Extra value"), default=0)
    legacy_extra_value = models.PositiveIntegerField(verbose_name=_("Legacy extra value"), default=0)
    tracked_seconds = models.PositiveIntegerField(verbose_name=_("Tracked seconds"), null=True, blank=True)
    correction = models.CharField(
        verbose_name=_("Correction"),
        max_length=10,
        choices=Correction.choices,
        default=Correction.KEEP,
        help_text=_("What a manual or imported row does with the tracked PAP of the same character."),
    )
    first_seen = models.DateTimeField(verbose_name=_("First seen"), null=True, blank=True)
    last_seen = models.DateTimeField(verbose_name=_("Last seen"), null=True, blank=True)
    notes = models.TextField(verbose_name=_("Notes"), blank=True)
    review_status = models.CharField(
        verbose_name=_("Review status"),
        max_length=10,
        choices=ReviewStatus.choices,
        default=ReviewStatus.APPROVED,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Reviewed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_reviewed_attendance",
    )
    reviewed_at = models.DateTimeField(verbose_name=_("Reviewed at"), null=True, blank=True)
    review_note = models.CharField(verbose_name=_("Review note"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_attendance_created",
    )
    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("PAP record")
        verbose_name_plural = _("PAP records")
        ordering = ["character_name", "created_at"]
        indexes = [
            models.Index(fields=["operation", "character_id", "source"], name="fleetops_att_op_char_src_idx"),
            models.Index(fields=["auth_user", "created_at"], name="fleetops_att_user_date_idx"),
        ]


class SpecialRole(models.Model):
    """A role FCs can assign to a pilot in a fleet, such as Back Seat FC."""

    key = models.SlugField(verbose_name=_("Key"), max_length=30, unique=True, editable=False)
    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, unique=True)
    grants_fc_credit = models.BooleanField(
        _("Gives FC credit"),
        default=False,
        help_text=_(
            "Pilots with this role get the fleet as an FC fleet for FC points and incentives. "
            "A change applies to new assignments; remove and add an assignment again to apply it there."
        ),
    )
    is_active = models.BooleanField(
        verbose_name=_("Is active"),
        default=True,
        help_text=_("FCs can assign this role."),
    )
    sort_order = models.PositiveIntegerField(verbose_name=_("Sort order"), default=0)

    class Meta:
        verbose_name = _("special role")
        verbose_name_plural = _("special roles")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # The key is stored on every assignment, so it is set once and never follows a rename.
        if not self.key:
            self.key = self._free_key(slugify(self.name).replace("-", "_")[:30].rstrip("_") or "role")
        super().save(*args, **kwargs)

    @classmethod
    def _free_key(cls, base):
        key, number = base, 1
        while cls.objects.filter(key=key).exists():
            number += 1
            suffix = f"_{number}"
            key = base[: 30 - len(suffix)].rstrip("_") + suffix
        return key


class PapLink(models.Model):
    """A link pilots open to register their characters for PAP in a fleet."""

    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="pap_links",
    )
    code = models.CharField(verbose_name=_("Code"), max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_pap_links",
    )
    created_at = models.DateTimeField(verbose_name=_("Created at"), default=timezone.now)
    expires_at = models.DateTimeField(verbose_name=_("Expires at"), db_index=True)
    require_online = models.BooleanField(
        verbose_name=_("Require online"),
        default=True,
        help_text=_("Characters must be logged in to the game when they register."),
    )
    revoked_at = models.DateTimeField(verbose_name=_("Revoked at"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("PAP link")
        verbose_name_plural = _("PAP links")

    def __str__(self):
        return gettext("PAP link %(pk)s @ %(operation)s") % {"pk": self.pk, "operation": self.operation_id}


class PapRegistration(models.Model):
    """A character registered for PAP through a PAP link."""

    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="pap_registrations",
    )
    link = models.ForeignKey(
        PapLink, verbose_name=_("Link"), null=True, blank=True, on_delete=models.SET_NULL, related_name="registrations"
    )
    character_id = models.BigIntegerField(verbose_name=_("Character ID"), db_index=True)
    character_name = models.CharField(verbose_name=_("Character name"), max_length=255)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Auth user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_pap_registrations",
    )
    ship_type_id = models.BigIntegerField(verbose_name=_("Ship type ID"), null=True, blank=True)
    ship_type_name = models.CharField(verbose_name=_("Ship type name"), max_length=255, blank=True)
    solar_system_id = models.BigIntegerField(verbose_name=_("Solar system ID"), null=True, blank=True)
    solar_system_name = models.CharField(verbose_name=_("Solar system name"), max_length=255, blank=True)
    online = models.BooleanField(verbose_name=_("Online"), null=True, blank=True)
    registered_at = models.DateTimeField(verbose_name=_("Registered at"), default=timezone.now)

    class Meta:
        ordering = ["registered_at"]
        verbose_name = _("PAP registration")
        verbose_name_plural = _("PAP registrations")
        constraints = [
            models.UniqueConstraint(fields=["operation", "character_id"], name="fleetops_unique_pap_registration")
        ]

    def __str__(self):
        return f"{self.character_name} @ {self.operation_id}"


class OperationRoleAssignment(models.Model):
    # The special roles before they became configurable; migration 0012 turned them into SpecialRole rows.
    class Role(models.TextChoices):
        BACKSEAT_FC = "backseat_fc", _("Back Seat FC")
        LOGI_ANCHOR = "logi_anchor", _("Logi Anchor")
        SNOWFLAKE = "snowflake", _("Snowflake Member")

    operation = models.ForeignKey(
        FleetOperation, verbose_name=_("Operation"), on_delete=models.CASCADE, related_name="role_assignments"
    )
    role = models.CharField(
        verbose_name=_("Role"),
        max_length=30,
        db_index=True,
        help_text=_("Key of the special role."),
    )
    special_role = models.ForeignKey(
        SpecialRole,
        verbose_name=_("Special role"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    character_id = models.BigIntegerField(verbose_name=_("Character ID"), db_index=True)
    character_name = models.CharField(verbose_name=_("Character name"), max_length=255)
    main_character_id = models.BigIntegerField(
        verbose_name=_("Main character ID"),
        null=True,
        blank=True,
        db_index=True,
    )
    main_character_name = models.CharField(verbose_name=_("Main character name"), max_length=255, blank=True)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Auth user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_role_assignments",
    )
    corporation_id = models.BigIntegerField(verbose_name=_("Corporation ID"), null=True, blank=True)
    corporation_name = models.CharField(verbose_name=_("Corporation name"), max_length=255, blank=True)
    grants_fc_credit = models.BooleanField(
        verbose_name=_("Grants FC credit"),
        default=False,
        help_text=_("Snapshot: this assignment counts as an FC fleet/points credit for the assigned user."),
    )
    notes = models.TextField(verbose_name=_("Notes"), blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Assigned by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_roles_assigned",
    )
    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("operation role assignment")
        verbose_name_plural = _("operation role assignments")
        ordering = ["role", "character_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "role", "character_id"],
                name="fleetops_unique_operation_role_character",
            )
        ]

    def __str__(self):
        return f"{self.role_label}: {self.character_name}"

    def save(self, *args, **kwargs):
        # An assignment made by role key alone is linked to the role with that key.
        if self.special_role_id is None and self.role:
            self.special_role = SpecialRole.objects.filter(key=self.role).first()
        super().save(*args, **kwargs)

    @property
    def role_label(self):
        if self.special_role_id is not None:
            return self.special_role.name
        return dict(self.Role.choices).get(self.role, self.role)

    def get_role_display(self):
        # The role field lost its fixed choices; pages written for them still call this.
        return self.role_label


class FleetStartPreset(models.Model):
    """Fleet Start values an FC saved; the row without a name holds the FC's last used values."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Owner"),
        on_delete=models.CASCADE,
        related_name="fleetops_start_presets",
    )
    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, blank=True)
    is_last_used = models.BooleanField(verbose_name=_("Is last used"), default=False)
    is_default = models.BooleanField(verbose_name=_("Is default"), default=False)
    # {"fields": {field name: value or list}, "labels": {field name: text shown when the value is gone}}
    values = models.JSONField(verbose_name=_("Values"), default=dict, blank=True)
    fc_character_id = models.BigIntegerField(verbose_name=_("FC character ID"), null=True, blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("fleet start preset")
        verbose_name_plural = _("fleet start presets")
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="fleetops_unique_start_preset")]

    def __str__(self):
        return self.name or gettext("Last used settings")

    def save(self, *args, **kwargs):
        # One default preset per FC. MariaDB has no conditional unique constraint, so the other
        # defaults are cleared here, under a row lock.
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_default:
                others = FleetStartPreset.objects.select_for_update().filter(owner_id=self.owner_id, is_default=True)
                demoted = list(others.exclude(pk=self.pk).values_list("pk", flat=True))
                if demoted:
                    FleetStartPreset.objects.filter(pk__in=demoted).update(is_default=False)


class ShipFunction(models.Model):
    """What a fleet member does, such as DPS or Logistics; the fleet composition counts members by it."""

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=60, unique=True)
    color = models.CharField(
        verbose_name=_("Color"),
        max_length=7,
        default="#6c757d",
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", _("Enter a color as # and six hex digits, such as #0d6efd."))],
    )
    fleet_roles = models.CharField(
        _("Fleet positions"),
        max_length=100,
        blank=True,
        validators=[validate_fleet_roles],
        help_text=_(
            "Comma-separated fleet positions (fleet_commander, wing_commander, squad_commander). "
            "Members in these positions count here whatever they fly."
        ),
    )
    is_default = models.BooleanField(
        _("Default"),
        default=False,
        help_text=_("Ships that no rule matches count here. Needs django-eveonline-sde to recognise ships."),
    )
    sort_order = models.PositiveIntegerField(verbose_name=_("Sort order"), default=0)
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)

    class Meta:
        verbose_name = _("ship function")
        verbose_name_plural = _("ship functions")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def roles(self):
        """The fleet positions as a tuple, in fleet order."""
        try:
            return parse_fleet_roles(self.fleet_roles)
        except FleetRuleError:
            return ()

    def clean(self):
        super().clean()
        # Inactive functions are ignored, so an inactive default would leave every unmatched ship unclassified.
        if self.is_default and not self.is_active:
            raise ValidationError({"is_default": _("The default function must be active.")})

    def save(self, *args, **kwargs):
        # The positions are stored as the classifier reads them (lower case, fleet order), also when
        # typed otherwise in Django admin; text that does not parse is left to the validator.
        try:
            self.fleet_roles = format_fleet_roles(self.fleet_roles)
        except FleetRuleError:
            pass
        # At most one active default. Functions that lost the flag are kept in demoted_defaults so
        # callers can audit them.
        self.demoted_defaults = []
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_default and self.is_active:
                others = ShipFunction.objects.select_for_update().filter(is_default=True)
                self.demoted_defaults = list(others.exclude(pk=self.pk))
                demoted_pks = [other.pk for other in self.demoted_defaults]
                ShipFunction.objects.filter(pk__in=demoted_pks).update(is_default=False)
                for other in self.demoted_defaults:
                    other.is_default = False


class ShipFunctionRule(models.Model):
    """Puts a ship type or a whole ship group (from the SDE) under a ship function."""

    class Kind(models.TextChoices):
        TYPE = "type", _("Ship type")
        GROUP = "group", _("Ship group")

    function = models.ForeignKey(
        ShipFunction,
        verbose_name=_("Function"),
        on_delete=models.CASCADE,
        related_name="rules",
    )
    kind = models.CharField(verbose_name=_("Kind"), max_length=10, choices=Kind.choices)
    target_id = models.BigIntegerField(
        _("Type or group ID"),
        validators=[MinValueValidator(1), MaxValueValidator(MAX_DATABASE_ID)],
        help_text=_("ID of the ship type or ship group in the SDE."),
    )
    label = models.CharField(
        verbose_name=_("Label"),
        max_length=255,
        blank=True,
        help_text=_("Type or group name when the rule was saved, shown without the SDE."),
    )

    class Meta:
        verbose_name = _("ship function rule")
        verbose_name_plural = _("ship function rules")
        ordering = ["function__sort_order", "kind", "label", "target_id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "target_id"], name="fleetops_unique_ship_function_rule")
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.label or self.target_id} → {self.function}"


class FleetStructureTemplate(models.Model):
    """Wing and squad names an FC can apply to a running fleet."""

    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=100, unique=True)
    layout = models.TextField(
        verbose_name=_("Layout"),
        validators=[validate_structure_layout],
        help_text=_(
            "One wing per line: 'Wing name: Squad 1, Squad 2'. Names up to 10 characters; "
            "up to 5 wings with up to 5 squads each. Lines starting with # are ignored."
        ),
    )
    description = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Description"), max_length=255, blank=True)
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)
    sort_order = models.PositiveIntegerField(verbose_name=_("Sort order"), default=0)

    class Meta:
        verbose_name = _("fleet structure template")
        verbose_name_plural = _("fleet structure templates")
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def wings(self):
        """``[{"name": "Main", "squads": ["Alpha", ...]}, ...]``; raises FleetRuleError for a broken layout."""
        return parse_structure_layout(self.layout)


class IncentivePeriod(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", pgettext_lazy("period status", "Open")
        REVIEW = "review", pgettext_lazy("period status", "Review")
        FINALIZED = "finalized", _("Finalized")

    year = models.PositiveIntegerField(
        verbose_name=_("Year"),
        validators=[MinValueValidator(2003), MaxValueValidator(2200)],
    )
    month = models.PositiveIntegerField(
        verbose_name=_("Month"),
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    status = models.CharField(verbose_name=_("Status"), max_length=20, choices=Status.choices, default=Status.OPEN)
    budget = models.BigIntegerField(verbose_name=_("Budget"), default=0, validators=[MinValueValidator(0)])
    minimum_fleets = models.PositiveIntegerField(
        verbose_name=_("Minimum fleets"),
        default=3,
        validators=[MinValueValidator(1)],
    )
    policy_snapshot = models.JSONField(verbose_name=_("Policy snapshot"), default=dict, blank=True)
    calculated_at = models.DateTimeField(verbose_name=_("Calculated at"), null=True, blank=True)
    finalized_at = models.DateTimeField(verbose_name=_("Finalized at"), null=True, blank=True)
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Finalized by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_finalized_periods",
    )

    class Meta:
        verbose_name = _("incentive period")
        verbose_name_plural = _("incentive periods")
        ordering = ["-year", "-month"]
        constraints = [models.UniqueConstraint(fields=["year", "month"], name="fleetops_unique_incentive_period")]

    def __str__(self):
        return f"{self.year}-{self.month:02d}"


class StatisticsTarget(models.Model):
    """The corporation PAP target and member minimum that apply from a month (EVE time) until the next entry."""

    year = models.PositiveIntegerField(
        verbose_name=_("Year"),
        validators=[MinValueValidator(2003), MaxValueValidator(2200)],
    )
    month = models.PositiveSmallIntegerField(
        verbose_name=_("Month"),
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    corporation_average_target = models.DecimalField(
        _("Corporation average target"),
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Average PAP per main character a corporation needs in a month. Empty = no target."),
    )
    member_minimum_pap = models.PositiveIntegerField(
        _("Member minimum PAP"),
        default=1,
        validators=[MaxValueValidator(10000)],
        help_text=_("Members with less PAP than this in a month are listed as inactive. 0 = nobody is listed."),
    )
    note = models.CharField(verbose_name=_("Note"), max_length=255, blank=True)

    class Meta:
        ordering = ["-year", "-month"]
        verbose_name = _("monthly target")
        verbose_name_plural = _("monthly targets")
        constraints = [models.UniqueConstraint(fields=["year", "month"], name="fleetops_unique_statistics_target")]

    @property
    def effective_from(self):
        return f"{self.year}-{self.month:02d}"

    def __str__(self):
        return gettext("From %(month)s") % {"month": self.effective_from}


class MonthlyFCStatistic(models.Model):
    period = models.ForeignKey(
        IncentivePeriod,
        verbose_name=_("Period"),
        on_delete=models.CASCADE,
        related_name="fc_statistics",
    )
    fc_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("FC user"),
        on_delete=models.PROTECT,
        related_name="fleetops_monthly_fc_stats",
    )
    fleet_count = models.PositiveIntegerField(verbose_name=_("Fleet count"), default=0)
    total_points = models.DecimalField(
        verbose_name=_("Total points"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
    )
    fleet_type_breakdown = models.JSONField(verbose_name=_("Fleet type breakdown"), default=dict, blank=True)
    eligible = models.BooleanField(verbose_name=_("Eligible"), default=False)
    waived = models.BooleanField(verbose_name=_("Waived"), default=False)
    calculated_payout = models.BigIntegerField(verbose_name=_("Calculated payout"), default=0)
    final_payout = models.BigIntegerField(verbose_name=_("Final payout"), default=0)

    class Meta:
        verbose_name = _("monthly FC statistic")
        verbose_name_plural = _("monthly FC statistics")
        ordering = ["-total_points", "fc_user__username"]
        constraints = [models.UniqueConstraint(fields=["period", "fc_user"], name="fleetops_unique_monthly_fc_stat")]


class EveEntityName(models.Model):
    """ESI names for IDs Alliance Auth does not know (characters, corporations, alliances)."""

    UNRESOLVED = "unresolved"

    id = models.BigIntegerField(verbose_name=_("ID"), primary_key=True)
    category = models.CharField(
        verbose_name=_("Category"),
        max_length=20,
        blank=True,
        help_text=_("ESI category, or \"unresolved\"."),
    )
    name = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Name"), max_length=255, blank=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), db_index=True)

    class Meta:
        verbose_name = _("EVE entity name")
        verbose_name_plural = _("EVE entity names")

    def __str__(self):
        return self.name or str(self.id)


class CharacterAffiliation(models.Model):
    """Corporation and alliance of characters Alliance Auth does not know, from ESI."""

    character_id = models.BigIntegerField(verbose_name=_("Character ID"), primary_key=True)
    corporation_id = models.BigIntegerField(verbose_name=_("Corporation ID"), null=True, blank=True)
    alliance_id = models.BigIntegerField(verbose_name=_("Alliance ID"), null=True, blank=True)
    faction_id = models.BigIntegerField(verbose_name=_("Faction ID"), null=True, blank=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), db_index=True)

    class Meta:
        verbose_name = _("character affiliation")
        verbose_name_plural = _("character affiliations")

    def __str__(self):
        return str(self.character_id)


class AuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Actor"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_audit_entries",
    )
    action = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Action"), max_length=100, db_index=True)
    object_type = models.CharField(verbose_name=_("Object type"), max_length=100)
    object_id = models.CharField(verbose_name=_("Object ID"), max_length=100)
    old_value = models.JSONField(verbose_name=_("Old value"), null=True, blank=True)
    new_value = models.JSONField(verbose_name=_("New value"), null=True, blank=True)
    reason = models.TextField(verbose_name=_("Reason"), blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("audit log")
        verbose_name_plural = _("audit logs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at}: {self.action} {self.object_type}:{self.object_id}"


# --- FleetOps SRP ---------------------------------------------------------------------------------
# ISK amounts are whole ISK; values read from killboards keep their cents. Codes (warnings, fleet checks,
# calculation notes) are stored with their parameters and turned into text when they are shown.


def isk_validators():
    """Validators of a configurable ISK amount (payout, cap)."""
    return [MinValueValidator(1), MaxValueValidator(MAX_ISK)]


def srp_doctrine_key(source, external_id, name):
    """The key that lets a payout rule and a fleet name the same doctrine.

    ``source:external_id`` for a doctrine of a doctrine app, else ``name:`` and the casefolded name with its
    white space (tabs and no-break spaces included) collapsed to single spaces, so a rule and a fleet agree
    however each was typed; empty when no doctrine is given.
    """
    source = (source or "").strip()
    external_id = (external_id or "").strip()
    if source and external_id:
        return f"{source}:{external_id}"
    name = " ".join(str(name or "").split()).casefold()
    return f"name:{name}" if name else ""


class SrpSettings(models.Model):
    """FleetOps SRP configuration; one row (pk=1)."""

    class ValueSource(models.TextChoices):
        ZKILL_FITTED = "zkill_fitted", _("zKillboard fitted value (ship, modules, drones, implants)")
        ZKILL_TOTAL = "zkill_total", _("zKillboard total value (includes cargo)")
        ESI_ESTIMATE = "esi_estimate", _("ESI market estimate")

    class OverLimitAction(models.TextChoices):
        REVIEW = "review", _("Send to review with a warning")
        REJECT = "reject", _("Reject automatically")

    enabled = models.BooleanField(
        verbose_name=pgettext_lazy("FleetOps", "Enabled"),
        default=False,
        help_text=_("Members can submit FleetOps SRP claims and fleets get FleetOps SRP links."),
    )
    out_of_fleet_enabled = models.BooleanField(
        _("Out-of-fleet losses"),
        default=True,
        help_text=_("Allow claims for losses outside FleetOps fleets."),
    )
    zkillboard_enabled = models.BooleanField(
        _("Use zKillboard"),
        default=True,
        help_text=_("Use zKillboard for killmail hashes and values. Without it only in-game killmail links work."),
    )
    value_source = models.CharField(
        verbose_name=_("Value source"),
        max_length=20,
        choices=ValueSource.choices,
        default=ValueSource.ZKILL_FITTED,
        help_text=_(
            "Where the loss value comes from. The ESI market estimate is also the fallback when zKillboard "
            "has no value."
        ),
    )
    percentage = models.DecimalField(
        _("Payout percentage"),
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Share of the loss value paid for ships not in the payout table. 0 = no automatic amount."),
    )
    in_fleet_loss_cap = models.PositiveBigIntegerField(
        _("In-fleet loss cap"),
        null=True,
        blank=True,
        validators=isk_validators(),
        help_text=_("Maximum per in-fleet loss. Empty = no cap."),
    )
    out_of_fleet_loss_cap = models.PositiveBigIntegerField(
        _("Out-of-fleet loss cap"),
        null=True,
        blank=True,
        validators=isk_validators(),
        help_text=_("Maximum per out-of-fleet loss. Empty = no cap."),
    )
    personal_monthly_cap = models.PositiveBigIntegerField(
        _("Personal monthly cap"),
        null=True,
        blank=True,
        validators=isk_validators(),
        help_text=_("Maximum approved SRP per player (all characters) per EVE month. Empty = no cap."),
    )
    personal_cap_includes_in_fleet = models.BooleanField(
        _("In-fleet losses count toward the personal cap"),
        default=True,
    )
    corporation_monthly_cap = models.PositiveBigIntegerField(
        _("Corporation monthly cap"),
        null=True,
        blank=True,
        validators=isk_validators(),
        help_text=_(
            "Maximum approved SRP per corporation and EVE month; the default for every corporation. "
            "Corporation limits override it. Empty = no cap."
        ),
    )
    corporation_cap_includes_in_fleet = models.BooleanField(
        _("In-fleet losses count toward the corporation cap"),
        default=True,
    )
    over_limit_action = models.CharField(
        verbose_name=_("Over limit action"),
        max_length=10,
        choices=OverLimitAction.choices,
        default=OverLimitAction.REVIEW,
        help_text=_(
            "What happens to a claim that does not fit into the remaining monthly limit. When it only partly "
            "fits it is cut to the remaining amount and sent to review; it is rejected automatically only when "
            "nothing is left."
        ),
    )
    capsules_need_rule = models.BooleanField(
        _("Capsules need a payout rule"),
        default=True,
        help_text=_("Capsule losses are only covered when the payout table lists the capsule (fixed amount)."),
    )
    claim_deadline_days = models.PositiveIntegerField(
        _("Claim deadline (days)"),
        default=0,
        validators=[MaxValueValidator(3650)],
        help_text=_("Losses older than this cannot be submitted. 0 = no deadline."),
    )
    allow_self_review = models.BooleanField(
        _("SRP managers may decide on their own claims"),
        default=False,
        help_text=_(
            "By default another SRP manager decides on a manager's own claims. Allow this for small alliances."
        ),
    )
    notify_members = models.BooleanField(
        _("Notify members"),
        default=True,
        help_text=_("Send an Alliance Auth notification when a claim is decided or paid."),
    )
    member_instructions = models.TextField(
        verbose_name=_("Member instructions"),
        blank=True,
        validators=[MaxLengthValidator(2000)],
        help_text=_("Shown to members on the SRP pages."),
    )
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("SRP setting")
        verbose_name_plural = _("SRP settings")

    def __str__(self):
        return gettext("FleetOps SRP Settings")

    def clean(self):
        super().clean()
        if not self.zkillboard_enabled and self.value_source != self.ValueSource.ESI_ESTIMATE:
            raise ValidationError(
                {"value_source": _("Choose the ESI market estimate when zKillboard is turned off.")}
            )

    @classmethod
    def get_solo(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class SrpPayoutRule(models.Model):
    """A fixed SRP amount for a ship type and/or a fleet doctrine; the most specific active rule wins."""

    class Scope(models.TextChoices):
        ANY = "any", _("In fleet and out of fleet")
        IN_FLEET = "in_fleet", _("In-fleet losses only")
        OUT_OF_FLEET = "out_of_fleet", _("Out-of-fleet losses only")

    # Fields of the unique constraint; validated even though doctrine_key is not a form field.
    CONSTRAINT_FIELDS = frozenset({"ship_type_id", "doctrine_key", "scope"})

    label = models.CharField(verbose_name=_("Label"), max_length=100, blank=True)
    ship_type_id = models.PositiveBigIntegerField(
        _("Ship type ID"), default=0, help_text=_("0 = any ship (doctrine rules only).")
    )
    ship_type_name = models.CharField(verbose_name=_("Ship type name"), max_length=255, blank=True)
    doctrine_source = models.CharField(verbose_name=_("Doctrine source"), max_length=100, blank=True)
    doctrine_external_id = models.CharField(verbose_name=_("Doctrine external ID"), max_length=255, blank=True)
    doctrine_name = models.CharField(verbose_name=_("Doctrine name"), max_length=255, blank=True)
    doctrine_key = models.CharField(
        verbose_name=_("Doctrine key"),
        max_length=400,
        blank=True,
        db_index=True,
        editable=False,
    )
    scope = models.CharField(verbose_name=_("Scope"), max_length=12, choices=Scope.choices, default=Scope.ANY)
    amount = models.PositiveBigIntegerField(
        verbose_name=_("Amount"),
        validators=isk_validators(),
        help_text=_("Whole ISK."),
    )
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)
    notes = models.CharField(verbose_name=_("Notes"), max_length=255, blank=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("SRP payout rule")
        verbose_name_plural = _("SRP payout rules")
        ordering = ["ship_type_name", "doctrine_name", "scope"]
        constraints = [
            models.UniqueConstraint(
                fields=["ship_type_id", "doctrine_key", "scope"],
                name="fleetops_unique_srp_rule",
                violation_error_message=_("A rule for this ship, doctrine and scope already exists."),
            )
        ]

    def __str__(self):
        ship = gettext("Ship %(id)s") % {"id": self.ship_type_id} if self.ship_type_id else ""
        parts = [self.ship_type_name or ship]
        if self.doctrine_key:
            parts.append(self.doctrine_name or self.doctrine_key)
        return " / ".join(part for part in parts if part) or gettext("SRP rule")

    def compute_doctrine_key(self):
        return srp_doctrine_key(self.doctrine_source, self.doctrine_external_id, self.doctrine_name)

    def clean(self):
        super().clean()
        self.doctrine_key = self.compute_doctrine_key()
        if not self.ship_type_id and not self.doctrine_key:
            raise ValidationError(_("Choose a ship type, a doctrine, or both."), code="no_target")

    def validate_constraints(self, exclude=None):
        # The key is derived, never a form field, but a duplicate rule must still be refused.
        self.doctrine_key = self.compute_doctrine_key()
        super().validate_constraints(set(exclude or ()) - self.CONSTRAINT_FIELDS)

    def save(self, *args, **kwargs):
        self.doctrine_key = self.compute_doctrine_key()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "doctrine_key" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "doctrine_key"]
        super().save(*args, **kwargs)


class SrpCorporationLimit(models.Model):
    """Monthly cap of one corporation; replaces the default corporation cap of the SRP settings."""

    corporation_id = models.BigIntegerField(verbose_name=_("Corporation ID"), unique=True)
    corporation_name = models.CharField(verbose_name=_("Corporation name"), max_length=255, blank=True)
    monthly_cap = models.PositiveBigIntegerField(
        verbose_name=_("Monthly cap"),
        null=True,
        blank=True,
        validators=isk_validators(),
        help_text=_("Maximum approved SRP of this corporation per EVE month. Empty = no limit for this corporation."),
    )
    note = models.CharField(verbose_name=_("Note"), max_length=255, blank=True)

    class Meta:
        verbose_name = _("SRP corporation limit")
        verbose_name_plural = _("SRP corporation limits")
        ordering = ["corporation_name", "corporation_id"]

    def __str__(self):
        return self.corporation_name or str(self.corporation_id)


class SrpFleetLink(models.Model):
    """The SRP link of a fleet; members of that fleet submit their losses through its code."""

    operation = models.OneToOneField(
        FleetOperation,
        verbose_name=_("Operation"),
        on_delete=models.CASCADE,
        related_name="srp_link",
    )
    code = models.CharField(verbose_name=_("Code"), max_length=32, unique=True)
    # The fleet's FC and the FC's main character when the link was created.
    fc_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("FC user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_srp_links",
    )
    fc_main_character_id = models.BigIntegerField(verbose_name=_("FC main character ID"), null=True, blank=True)
    fc_main_character_name = models.CharField(verbose_name=_("FC main character name"), max_length=255, blank=True)
    is_open = models.BooleanField(verbose_name=_("Is open"), default=True)
    closed_at = models.DateTimeField(verbose_name=_("Closed at"), null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Closed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(verbose_name=_("Created at"), default=timezone.now)

    class Meta:
        verbose_name = _("SRP fleet link")
        verbose_name_plural = _("SRP fleet links")
        ordering = ["-created_at"]

    def __str__(self):
        return gettext("SRP link of fleet %(operation)s") % {"operation": self.operation_id}


class SrpKillmail(models.Model):
    """A killmail read from ESI for a claim, with the value zKillboard (or an ESI estimate) gave it.

    The killmail ID is the primary key and a claim points to it one to one, so a killmail can only be
    claimed once, whatever link was used.
    """

    class ZkillStatus(models.TextChoices):
        PENDING = "pending", _("Waiting")
        OK = "ok", _("Value available")
        MISSING = "missing", _("Not on zKillboard yet")
        UNAVAILABLE = "unavailable", _("zKillboard value unavailable")
        DISABLED = "disabled", _("zKillboard disabled")

    killmail_id = models.BigIntegerField(verbose_name=_("Killmail ID"), primary_key=True)
    killmail_hash = models.CharField(verbose_name=_("Killmail hash"), max_length=40)
    killmail_time = models.DateTimeField(verbose_name=_("Killmail time"), db_index=True)
    solar_system_id = models.BigIntegerField(verbose_name=_("Solar system ID"))
    solar_system_name = models.CharField(verbose_name=_("Solar system name"), max_length=100, blank=True)
    victim_character_id = models.BigIntegerField(
        verbose_name=_("Victim character ID"),
        null=True,
        blank=True,
        db_index=True,
    )
    victim_corporation_id = models.BigIntegerField(verbose_name=_("Victim corporation ID"), null=True, blank=True)
    victim_alliance_id = models.BigIntegerField(verbose_name=_("Victim alliance ID"), null=True, blank=True)
    ship_type_id = models.BigIntegerField(verbose_name=_("Ship type ID"))
    ship_type_name = models.CharField(verbose_name=_("Ship type name"), max_length=255, blank=True)
    attacker_count = models.PositiveIntegerField(verbose_name=_("Attacker count"), default=0)
    war_id = models.BigIntegerField(verbose_name=_("War ID"), null=True, blank=True)
    # The victim's items as ESI returns them: flag, type, quantities, singleton and nested items.
    items = models.JSONField(verbose_name=_("Items"), default=list, blank=True)
    fetched_at = models.DateTimeField(verbose_name=_("Fetched at"), default=timezone.now)

    zkill_status = models.CharField(
        verbose_name=_("zKillboard status"),
        max_length=12,
        choices=ZkillStatus.choices,
        default=ZkillStatus.PENDING,
        db_index=True,
    )
    zkill_attempts = models.PositiveSmallIntegerField(verbose_name=_("zKillboard attempts"), default=0)
    zkill_next_attempt_at = models.DateTimeField(
        verbose_name=_("zKillboard next attempt at"),
        null=True,
        blank=True,
        db_index=True,
    )
    zkill_checked_at = models.DateTimeField(verbose_name=_("zKillboard checked at"), null=True, blank=True)
    zkill_fitted_value = models.DecimalField(
        verbose_name=_("zKillboard fitted value"),
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
    )
    zkill_total_value = models.DecimalField(
        verbose_name=_("zKillboard total value"),
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
    )
    zkill_destroyed_value = models.DecimalField(
        verbose_name=_("zKillboard destroyed value"),
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
    )
    zkill_dropped_value = models.DecimalField(
        verbose_name=_("zKillboard dropped value"),
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
    )
    zkill_npc = models.BooleanField(verbose_name=_("zKillboard NPC"), null=True, blank=True)
    zkill_solo = models.BooleanField(verbose_name=_("zKillboard solo"), null=True, blank=True)
    zkill_awox = models.BooleanField(verbose_name=_("zKillboard awox"), null=True, blank=True)
    zkill_labels = models.JSONField(verbose_name=_("zKillboard labels"), default=list, blank=True)
    esi_estimate = models.DecimalField(
        verbose_name=_("ESI estimate"),
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
    )
    esi_estimate_at = models.DateTimeField(verbose_name=_("ESI estimate at"), null=True, blank=True)

    class Meta:
        verbose_name = _("SRP killmail")
        verbose_name_plural = _("SRP killmails")
        ordering = ["-killmail_time"]

    def __str__(self):
        return gettext("Killmail %(id)s") % {"id": self.killmail_id}


class SrpClaim(models.Model):
    """A member's request to replace a lost ship; one per killmail."""

    class Scope(models.TextChoices):
        IN_FLEET = "in_fleet", _("In fleet")
        OUT_OF_FLEET = "out_of_fleet", _("Out of fleet")

    class Status(models.TextChoices):
        PENDING = "pending", pgettext_lazy("FleetOps", "Pending")
        APPROVED = "approved", pgettext_lazy("FleetOps", "Approved")
        PAID = "paid", _("Paid")
        REJECTED = "rejected", pgettext_lazy("FleetOps", "Rejected")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    class PayoutBasis(models.TextChoices):
        RULE = "rule", _("Payout table")
        PERCENTAGE = "percentage", _("Percentage of the value")
        NONE = "none", _("No automatic amount")

    # Approved and paid claims use up the monthly limits; withdrawn claims count in no summary.
    USAGE_STATUSES = (Status.APPROVED, Status.PAID)

    uuid = models.UUIDField(verbose_name=_("UUID"), default=uuid.uuid4, unique=True, editable=False)
    killmail = models.OneToOneField(
        SrpKillmail,
        verbose_name=_("Killmail"),
        on_delete=models.PROTECT,
        related_name="claim",
    )
    scope = models.CharField(verbose_name=_("Scope"), max_length=12, choices=Scope.choices, db_index=True)
    link = models.ForeignKey(
        SrpFleetLink,
        verbose_name=_("Link"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claims",
    )
    operation = models.ForeignKey(
        FleetOperation,
        verbose_name=_("Operation"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="srp_claims",
    )
    # "<type> — <FC> — YYYY-MM-DD HH:MM EVE", kept when the fleet is deleted.
    fleet_label = models.CharField(verbose_name=_("Fleet label"), max_length=255, blank=True)
    fc_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("FC user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_srp_fc_claims",
    )
    fc_main_character_name = models.CharField(verbose_name=_("FC main character name"), max_length=255, blank=True)

    # The claimant owns the victim character. Snapshot of the identity at submission; the corporation is
    # the main character's, not the victim's.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("User"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_srp_claims",
    )
    character_id = models.BigIntegerField(verbose_name=_("Character ID"), db_index=True)
    character_name = models.CharField(verbose_name=_("Character name"), max_length=255)
    main_character_id = models.BigIntegerField(verbose_name=_("Main character ID"), null=True, blank=True)
    main_character_name = models.CharField(verbose_name=_("Main character name"), max_length=255, blank=True)
    corporation_id = models.BigIntegerField(verbose_name=_("Corporation ID"), null=True, blank=True, db_index=True)
    corporation_name = models.CharField(verbose_name=_("Corporation name"), max_length=255, blank=True)
    corporation_ticker = models.CharField(verbose_name=_("Corporation ticker"), max_length=20, blank=True)
    alliance_id = models.BigIntegerField(verbose_name=_("Alliance ID"), null=True, blank=True)
    alliance_name = models.CharField(verbose_name=_("Alliance name"), max_length=255, blank=True)

    killmail_time = models.DateTimeField(verbose_name=_("Killmail time"), db_index=True)
    ship_type_id = models.BigIntegerField(verbose_name=_("Ship type ID"), db_index=True)
    ship_type_name = models.CharField(verbose_name=_("Ship type name"), max_length=255, blank=True)
    solar_system_name = models.CharField(verbose_name=_("Solar system name"), max_length=100, blank=True)

    loss_value = models.DecimalField(
        verbose_name=_("Loss value"),
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
    )
    value_source = models.CharField(verbose_name=_("Value source"), max_length=20, blank=True)
    payout_basis = models.CharField(
        verbose_name=_("Payout basis"),
        max_length=12,
        choices=PayoutBasis.choices,
        default=PayoutBasis.NONE,
    )
    payout_rule = models.ForeignKey(
        SrpPayoutRule,
        verbose_name=_("Payout rule"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claims",
    )
    # PolicySnapshot and Calculation as JSON (fleetops.srp.types).
    policy = models.JSONField(verbose_name=_("Policy"), default=dict, blank=True)
    calculation = models.JSONField(verbose_name=_("Calculation"), default=dict, blank=True)
    base_amount = models.PositiveBigIntegerField(verbose_name=_("Base amount"), default=0)
    suggested_amount = models.PositiveBigIntegerField(verbose_name=_("Suggested amount"), default=0)
    limited_amount = models.PositiveBigIntegerField(verbose_name=_("Limited amount"), default=0)
    over_limit = models.BooleanField(verbose_name=_("Over limit"), default=False, db_index=True)
    counts_personal = models.BooleanField(verbose_name=_("Counts personal"), default=True)
    counts_corporation = models.BooleanField(verbose_name=_("Counts corporation"), default=True)

    # PresenceResult code and its parameters.
    fleet_check = models.CharField(verbose_name=_("Fleet check"), max_length=20, blank=True, db_index=True)
    fleet_check_params = models.JSONField(verbose_name=_("Fleet check params"), default=dict, blank=True)
    fleet_checked_at = models.DateTimeField(verbose_name=_("Fleet checked at"), null=True, blank=True)
    # [{"code": "over_limit", "level": "danger", "params": {...}}]
    warnings = models.JSONField(verbose_name=_("Warnings"), default=list, blank=True)
    member_comment = models.TextField(verbose_name=_("Member comment"), blank=True)

    status = models.CharField(
        verbose_name=_("Status"),
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    auto_rejected = models.BooleanField(verbose_name=_("Auto rejected"), default=False)
    approved_amount = models.PositiveBigIntegerField(verbose_name=_("Approved amount"), null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Decided by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    decided_at = models.DateTimeField(verbose_name=_("Decided at"), null=True, blank=True)
    decision_reason = models.TextField(verbose_name=_("Decision reason"), blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Paid by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    paid_at = models.DateTimeField(verbose_name=_("Paid at"), null=True, blank=True, db_index=True)
    payment_note = models.CharField(verbose_name=_("Payment note"), max_length=255, blank=True)

    submitted_at = models.DateTimeField(verbose_name=_("Submitted at"), default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("SRP claim")
        verbose_name_plural = _("SRP claims")
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["status", "killmail_time"], name="fleetops_srp_status_time_idx"),
            models.Index(fields=["user", "killmail_time"], name="fleetops_srp_user_time_idx"),
            models.Index(fields=["corporation_id", "killmail_time"], name="fleetops_srp_corp_time_idx"),
            models.Index(fields=["fc_user", "killmail_time"], name="fleetops_srp_fc_time_idx"),
        ]

    def __str__(self):
        return self.reference

    @property
    def reference(self):
        return f"SRP-{self.pk}" if self.pk else "SRP"


class SrpClaimEvent(models.Model):
    """One step in the history of a claim; the claimant sees all but the internal notes."""

    class Action(models.TextChoices):
        SUBMIT = "submit", _("Submitted")
        AUTO_REJECT = "auto_reject", _("Rejected automatically")
        WITHDRAW = "withdraw", _("Withdrawn")
        RESUBMIT = "resubmit", _("Submitted again")
        APPROVE = "approve", pgettext_lazy("FleetOps", "Approved")
        AMOUNT_CHANGE = "amount_change", _("Amount changed")
        REJECT = "reject", pgettext_lazy("FleetOps", "Rejected")
        REVOKE = "revoke", _("Approval revoked")
        PAID = "paid", _("Marked paid")
        UNPAID = "unpaid", _("Payment undone")
        REOPEN = "reopen", _("Reopened")
        RECLASSIFY = "reclassify", _("Moved between in fleet and out of fleet")
        RECALCULATE = "recalculate", _("Recalculated")
        RECHECK = "recheck", _("Fleet presence checked again")
        VALUE_UPDATE = "value_update", _("Loss value updated")
        NOTE = "note", _("Internal note")

    claim = models.ForeignKey(SrpClaim, verbose_name=_("Claim"), on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Actor"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    action = models.CharField(verbose_name=pgettext_lazy("FleetOps", "Action"), max_length=30, choices=Action.choices)
    from_status = models.CharField(verbose_name=_("From status"), max_length=12, blank=True)
    to_status = models.CharField(verbose_name=_("To status"), max_length=12, blank=True)
    amount = models.PositiveBigIntegerField(verbose_name=_("Amount"), null=True, blank=True)
    reason = models.TextField(verbose_name=_("Reason"), blank=True)
    # Manager-only note; hidden from the claimant.
    internal = models.BooleanField(verbose_name=_("Internal"), default=False)
    # Old and new amounts, the previous calculation and similar details.
    data = models.JSONField(verbose_name=_("Data"), default=dict, blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = _("SRP claim event")
        verbose_name_plural = _("SRP claim events")
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.claim_id}: {self.action}"


class GroupAccess(models.Model):
    """What the leaders of one Alliance Auth group may do in FleetOps. The leaders are read from Alliance Auth."""

    class Mode(models.TextChoices):
        PING = "ping", _("Ping only")
        FLEETS = "fleets", _("Pings and fleets with PAP")

    group = models.OneToOneField(
        "auth.Group",
        verbose_name=pgettext_lazy("FleetOps", "Group"),
        on_delete=models.CASCADE,
        related_name="fleetops_access",
    )
    mode = models.CharField(_("Leaders may"), max_length=10, choices=Mode.choices, default=Mode.PING)
    # Kept when the mode is switched back to "ping", so switching is reversible.
    fleet_types = models.ManyToManyField(
        FleetType,
        verbose_name=_("Fleet types"),
        blank=True,
        related_name="group_accesses",
        help_text=_("Fleet types the leaders may start. Only used for pings and fleets with PAP."),
    )
    srp_enabled = models.BooleanField(
        _("SRP link for group fleets"),
        default=False,
        help_text=_("Group fleets get an SRP link like alliance fleets."),
    )
    is_active = models.BooleanField(verbose_name=_("Is active"), default=True)
    note = models.CharField(verbose_name=_("Note"), max_length=255, blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(verbose_name=_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("group access")
        verbose_name_plural = _("group accesses")
        ordering = ["group__name"]

    def __str__(self):
        return self.group.name


class StandalonePing(models.Model):
    """A ping sent from the Send Ping page, without a fleet. The webhook URL is never stored here."""

    class Status(models.TextChoices):
        PENDING = "pending", pgettext_lazy("FleetOps", "Pending")
        SENT = "sent", pgettext_lazy("FleetOps", "Sent")
        FAILED = "failed", _("Failed")

    uuid = models.UUIDField(verbose_name=_("UUID"), default=uuid.uuid4, unique=True, editable=False)
    # Guards against a double submit of the same form.
    request_id = models.UUIDField(verbose_name=_("Request ID"), unique=True, editable=False)
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Sender"),
        null=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_pings",
    )
    # The main character of the sender at send time.
    sender_name = models.CharField(verbose_name=_("Sender name"), max_length=255, blank=True)
    group = models.ForeignKey(
        "auth.Group",
        verbose_name=pgettext_lazy("FleetOps", "Group"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fleetops_pings",
    )
    # Empty for an alliance ping sent by an FC.
    group_name = models.CharField(verbose_name=_("Group name"), max_length=150, blank=True)
    channel = models.ForeignKey(
        DiscordWebhook,
        verbose_name=_("Ping channel"),
        null=True,
        on_delete=models.SET_NULL,
        related_name="standalone_pings",
    )
    channel_name = models.CharField(verbose_name=_("Ping channel name"), max_length=100)
    template = models.ForeignKey(
        MessageTemplate,
        verbose_name=_("Template"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    mention_mode = models.CharField(
        verbose_name=_("Ping mention mode"),
        max_length=10,
        choices=FleetOperation.MentionMode.choices,
        default=FleetOperation.MentionMode.NONE,
    )
    # Snapshot like FleetOperation.ping_roles: [{"id": "<role id>", "name": "Sig A"}].
    mention_roles = models.JSONField(verbose_name=_("Ping roles"), default=list, blank=True)
    fleet_time = models.DateTimeField(verbose_name=_("Fleet time"), null=True, blank=True)
    formup = models.CharField(verbose_name=_("Formup"), max_length=255, blank=True)
    doctrine_name = models.CharField(verbose_name=_("Doctrine name"), max_length=255, blank=True)
    doctrine_source = models.CharField(verbose_name=_("Doctrine source"), max_length=50, blank=True)
    doctrine_external_id = models.CharField(verbose_name=_("Doctrine external ID"), max_length=255, blank=True)
    comms = models.ForeignKey(
        CommsPreset,
        verbose_name=_("Comms"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    additional_message = models.TextField(verbose_name=_("Additional message"))
    # The ping as posted (<@&id>) and as people read it (@Name).
    text = models.TextField(verbose_name=_("Ping text"), blank=True)
    display_text = models.TextField(verbose_name=_("Ping text as read"), blank=True)
    status = models.CharField(
        verbose_name=_("Status"),
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    # PingResult.message, which is already scrubbed of the webhook address.
    result_message = models.TextField(verbose_name=_("Result"), blank=True)
    http_status = models.PositiveSmallIntegerField(verbose_name=_("HTTP status"), null=True, blank=True)
    created_at = models.DateTimeField(verbose_name=_("Created at"), default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(verbose_name=_("Sent at"), null=True, blank=True)

    class Meta:
        verbose_name = _("ping without fleet")
        verbose_name_plural = _("pings without fleet")
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["channel", "created_at"], name="fleetops_ping_channel_time_idx")]

    def __str__(self):
        return f"{self.channel_name} — {self.sender_name} ({self.uuid})"
