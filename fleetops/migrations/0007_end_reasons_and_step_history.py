# AA FleetOps 0.1.0a6: fleet end reasons, end requests and per-step attempt history

import re
from http import HTTPStatus

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q

END_REASON_CHOICES = [
    ("manual", "Ended manually"),
    ("fleet_gone", "Fleet no longer exists or the FC left it"),
    ("boss_handover", "Fleet boss handed over"),
]
BATCH_SIZE = 500

# 0.1.0a5 and older stored django-esi's exception text, e.g.
# "Could not update fleet MOTD: <HTTPClientError 403 None {'date': ..., 'set-cookie': ...}>"
# or "... <HTTPServerError 503 details=None error='Service unavailable' status=None Headers({...})>".
# The text runs to the end of the header dict, or to the end of a message cut at 4000 characters.
LEGACY_ESI_ERROR = re.compile(
    r"<HTTP(?:ClientError|ServerError|NotModified) (?P<status>\d{3})\b(?P<body>.*?)(?:\}\)?>|\Z)", re.DOTALL
)
ERROR_FIELD = re.compile(r"""\berror['"]?\s*[:=]\s*(['"])(?P<error>.*?)\1""", re.DOTALL)
LEGACY_MARKERS = ("<HTTPClientError ", "<HTTPServerError ", "<HTTPNotModified ", "Headers({")
NO_DETAILS = "ESI returned an error (details not kept)."


def _reason_phrase(status):
    phrase = {420: "Error limited"}.get(status)
    if phrase is None:
        try:
            phrase = HTTPStatus(status).phrase
        except ValueError:
            return "unknown status"
    return phrase[:1].upper() + phrase[1:].lower()


def _short_esi_error(match):
    status = int(match.group("status"))
    found = ERROR_FIELD.search(match.group("body"))
    error = " ".join(found.group("error").split()).rstrip(".") if found else ""
    if len(error) > 120:
        error = error[:117].rstrip() + "..."
    return f"ESI returned HTTP {status} ({error or _reason_phrase(status)})."


def clean_esi_error(text):
    """Rewrite django-esi exception text into the short form FleetOps stores now."""
    if not text or not any(marker in text for marker in LEGACY_MARKERS):
        return text
    cleaned = LEGACY_ESI_ERROR.sub(_short_esi_error, text)
    if any(marker in cleaned for marker in LEGACY_MARKERS):
        prefix, separator, _rest = cleaned.partition(": ")
        cleaned = f"{prefix}: {NO_DETAILS}" if separator else NO_DETAILS
    return cleaned


def _legacy_text(field):
    query = Q()
    for marker in LEGACY_MARKERS:
        query |= Q(**{f"{field}__contains": marker})
    return query


def keep_latest_step_results(apps, schema_editor):
    # Earlier versions only kept the latest result of each step; it becomes the first attempt.
    # Raw django-esi error text is shortened first, so it does not stay listed under the step's
    # previous attempts after a successful retry.
    OperationAction = apps.get_model("fleetops", "OperationAction")
    OperationActionAttempt = apps.get_model("fleetops", "OperationActionAttempt")
    cleaned = {
        pk: clean_esi_error(text)
        for pk, text in OperationAction.objects.filter(_legacy_text("error_message")).values_list("pk", "error_message")
    }
    batch = []
    for action in OperationAction.objects.order_by("pk").iterator():
        batch.append(
            OperationActionAttempt(
                action_id=action.pk,
                status=action.status,
                message=cleaned.get(action.pk, action.error_message),
                created_at=action.executed_at,
            )
        )
        if len(batch) >= BATCH_SIZE:
            OperationActionAttempt.objects.bulk_create(batch)
            batch = []
    if batch:
        OperationActionAttempt.objects.bulk_create(batch)
    # update() keeps executed_at as it was.
    for pk, message in cleaned.items():
        OperationAction.objects.filter(pk=pk).update(error_message=message)


def clean_fleet_errors(apps, schema_editor):
    FleetOperation = apps.get_model("fleetops", "FleetOperation")
    for pk, text in FleetOperation.objects.filter(_legacy_text("last_error")).values_list("pk", "last_error"):
        message = clean_esi_error(text)
        if message != text:
            FleetOperation.objects.filter(pk=pk).update(last_error=message)


def label_ended_fleets(apps, schema_editor):
    # Before this release a fleet only ended automatically when ESI no longer returned it.
    FleetOperation = apps.get_model("fleetops", "FleetOperation")
    FleetMemberState = apps.get_model("fleetops", "FleetMemberState")
    closed = FleetOperation.objects.filter(status="closed", is_manual=False, end_reason="")
    for message, reason in (("Manual", "manual"), ("Automatic", "fleet_gone")):
        closed.filter(actions__action="fleet_end", actions__error_message=message).update(end_reason=reason)

    # Closed fleets could keep pilots marked as still in fleet; they left when the fleet ended.
    stale = (
        FleetOperation.objects.filter(status__in=["closed", "cancelled"], member_states__is_active=True)
        .exclude(ended_at=None)
        .distinct()
    )
    for pk, ended_at in stale.values_list("pk", "ended_at"):
        FleetMemberState.objects.filter(operation_id=pk, is_active=True).update(is_active=False, left_at=ended_at)


class Migration(migrations.Migration):

    dependencies = [
        ("fleetops", "0006_redact_webhook_tokens_keep_incentives"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="fleetoperation",
            name="end_detail",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="end_reason",
            field=models.CharField(blank=True, choices=END_REASON_CHOICES, max_length=20),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="end_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="end_requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fleetops_end_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="end_requested_multiplier",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3),
                ],
            ),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="missing_detail",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="fleetoperation",
            name="missing_reason",
            field=models.CharField(
                blank=True,
                choices=END_REASON_CHOICES,
                help_text="Why the latest tracking poll could not read the fleet. Cleared by a successful read.",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="OperationActionAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("success", "Success"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        max_length=20,
                    ),
                ),
                ("message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                (
                    "action",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="history",
                        to="fleetops.operationaction",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.RunPython(keep_latest_step_results, migrations.RunPython.noop),
        migrations.RunPython(clean_fleet_errors, migrations.RunPython.noop),
        migrations.RunPython(label_ended_fleets, migrations.RunPython.noop),
    ]
