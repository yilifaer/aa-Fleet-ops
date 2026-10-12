"""SRP provider abstraction.

FleetOps deliberately keeps SRP integration behind a tiny provider interface so any
community SRP app can register without changing FleetOperation core logic.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from django.apps import apps
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from allianceauth.eveonline.models import EveCharacter

from fleetops.services import links
from fleetops.services.periods import format_eve

# "auto" tries these first, then any other registered provider that is available.
AUTO_PROVIDER_ORDER = ("aasrp", "allianceauth_builtin")
# Keys stored by earlier versions.
PROVIDER_ALIASES = {"aa_srp": "aasrp"}


@dataclass(slots=True)
class SRPLinkResult:
    provider: str
    reference: str = ""
    url: str = ""
    created: bool = False
    message: str = ""


class SRPProvider(Protocol):
    key: str

    def available(self) -> bool: ...
    def create_for_operation(self, operation) -> SRPLinkResult: ...


_PROVIDERS: dict[str, SRPProvider] = {}


def register_srp_provider(provider: SRPProvider) -> None:
    """Register/replace a provider by key.

    Third-party apps may call this from AppConfig.ready() or an auth hook.
    """
    _PROVIDERS[provider.key] = provider


def get_registered_srp_providers() -> dict[str, SRPProvider]:
    return dict(_PROVIDERS)


def _required_unknown_fields(model, supplied: dict) -> list[str]:
    """Required fields of an SRP model that FleetOps does not know how to fill."""
    unknown = []
    for field in model._meta.concrete_fields:
        if field.primary_key or field.auto_created or field.name in supplied:
            continue
        if field.null or field.blank or field.has_default():
            continue
        # Auto timestamp fields do not need input.
        if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False):
            continue
        unknown.append(field.name)
    return unknown


def _reverse_first(candidates, args=None):
    for name in candidates:
        try:
            return reverse(name, args=args or [])
        except NoReverseMatch:
            continue
    return ""


class AllianceAuthBuiltinSRPProvider:
    """Best-effort adapter for Alliance Auth's built-in ``allianceauth.srp``.

    The core SRP app has kept the SrpFleetMain concept across many AA releases.
    We intentionally introspect fields and URL names instead of importing forms
    or views, reducing coupling to one exact AA patch release.
    """

    key = "allianceauth_builtin"

    def available(self) -> bool:
        return apps.is_installed("allianceauth.srp")

    def _model(self):
        try:
            return apps.get_model("srp", "SrpFleetMain")
        except LookupError:
            # Defensive fallback if the app label differs from module name.
            from allianceauth.srp.models import SrpFleetMain

            return SrpFleetMain

    def create_for_operation(self, operation) -> SRPLinkResult:
        if not self.available():
            return SRPLinkResult(self.key, message="Alliance Auth built-in SRP is not installed.")

        model = self._model()
        fields = {f.name for f in model._meta.concrete_fields}
        values = {}
        if "fleet_name" in fields:
            values["fleet_name"] = f"{operation.fleet_type} — {operation.fc_character_name}"
        if "fleet_time" in fields:
            values["fleet_time"] = operation.started_at or operation.scheduled_at
        if "fleet_doctrine" in fields:
            values["fleet_doctrine"] = operation.doctrine_name or str(operation.fleet_type)
        if "fleet_commander" in fields:
            commander = EveCharacter.objects.filter(character_id=operation.fc_character_id).first()
            if commander is None:
                return SRPLinkResult(self.key, message="FC character is not present in Alliance Auth EveCharacter data.")
            values["fleet_commander"] = commander
        if "fleet_srp_code" in fields:
            # Alliance Auth treats a fleet with an empty code as disabled; it uses 8 uppercase characters.
            values["fleet_srp_code"] = uuid.uuid4().hex.upper()[:8]

        unknown = _required_unknown_fields(model, values)
        if unknown:
            return SRPLinkResult(
                self.key,
                message="Unsupported built-in SRP schema; required fields: " + ", ".join(unknown),
            )

        obj = model.objects.create(**values)
        reference = str(getattr(obj, "pk", "") or "")

        # Prefer model-provided URL when the SRP app exposes one.
        url = ""
        get_absolute_url = getattr(obj, "get_absolute_url", None)
        if callable(get_absolute_url):
            try:
                url = str(get_absolute_url() or "")
            except Exception:
                url = ""

        # Alliance Auth names the fleet page "srp:fleet" and the pilot request page
        # "srp:request". Older/community versions used other names, tried afterwards.
        if not url and reference:
            url = _reverse_first(
                [
                    "srp:fleet",
                    "srp:request_srp",
                    "srp:srp_request",
                    "srp:srp_fleet_view",
                    "srp:srp_fleet_detail",
                    "srp_fleet_view",
                    "srp_request",
                ],
                [obj.pk],
            )
        if not url and values.get("fleet_srp_code"):
            url = _reverse_first(["srp:request"], [values["fleet_srp_code"]])
        if not url:
            url = _reverse_first(["srp:management", "srp:index", "srp:srp", "srp:srp_management", "srp_management"])
        if not url:
            # Built-in AA app normally lives here. Keeping this as a relative
            # fallback makes the operation useful even if URL names changed.
            url = "/srp/"

        return SRPLinkResult(
            provider=self.key,
            reference=reference,
            url=url,
            created=True,
            message="Built-in Alliance Auth SRP fleet created.",
        )


class AaSrpProvider:
    """SRP links in aa-srp (app label ``aasrp``).

    The link is created at fleet start for the FC (its creator is the FC's Alliance Auth user)
    without checking aa-srp's own permissions, like the built-in provider; admins turn it off with
    "SRP auto create". Its AAR link is the FleetOps fleet page, and an aa-srp link that already
    points there is reused, so a start interrupted between creating the link and saving the fleet
    never creates a second one. Retries are also serialised by the row lock in ``retry_srp``.
    """

    key = "aasrp"

    def available(self) -> bool:
        return apps.is_installed("aasrp")

    def _model(self):
        return apps.get_model("aasrp", "SrpLink")

    def _fleet_type(self, operation):
        """The enabled aa-srp fleet type named like the FleetOps fleet type (name or short name)."""
        try:
            model = apps.get_model("aasrp", "FleetType")
        except LookupError:
            return None
        names = {
            name.casefold()
            for name in (operation.fleet_type.name, operation.fleet_type.short_name)
            if name
        }
        for fleet_type in model.objects.filter(is_enabled=True).order_by("pk"):
            if str(fleet_type.name).casefold() in names:
                return fleet_type
        return None

    @staticmethod
    def _commander(operation):
        for character_id in (operation.fc_main_character_id, operation.fc_character_id):
            if character_id:
                commander = EveCharacter.objects.filter(character_id=character_id).first()
                if commander is not None:
                    return commander
        return None

    def _result(self, link, message) -> SRPLinkResult:
        code = str(getattr(link, "srp_code", "") or "")
        try:
            url = reverse("aasrp:request_srp", args=[code]) if code else ""
        except NoReverseMatch:
            url = ""
        return SRPLinkResult(
            provider=self.key,
            reference=code or str(getattr(link, "pk", "") or ""),
            url=url or "/ship-replacement/",
            created=True,
            message=message,
        )

    def create_for_operation(self, operation) -> SRPLinkResult:
        if not self.available():
            return SRPLinkResult(self.key, message="aa-srp is not installed.")

        model = self._model()
        fields = {f.name for f in model._meta.concrete_fields}
        aar_link = links.absolute_url(reverse("fleetops:operation_detail", args=[operation.uuid]))[:254]
        if "aar_link" in fields:
            existing = model.objects.filter(aar_link=aar_link).order_by("pk").first()
            if existing is not None:
                return self._result(existing, "Existing aa-srp link of this fleet reused.")

        fleet_time = operation.started_at or operation.scheduled_at or timezone.now()
        name = f"{operation.fleet_type} — {operation.fc_character_name} — {format_eve(fleet_time)} EVE"
        values = {
            "srp_name": name[:254],
            "fleet_time": fleet_time,
            "fleet_doctrine": (operation.doctrine_name or str(operation.fleet_type))[:254],
            "fleet_commander": self._commander(operation),
            "fleet_type": self._fleet_type(operation),
            "aar_link": aar_link,
            "creator": operation.fc_user,
        }
        values = {name: value for name, value in values.items() if name in fields}
        unknown = _required_unknown_fields(model, values)
        if unknown:
            return SRPLinkResult(self.key, message="Unsupported aasrp schema; required fields: " + ", ".join(unknown))

        # aa-srp fills srp_code with a random code when the link is saved.
        link = model.objects.create(**values)
        return self._result(link, "aa-srp link created.")


register_srp_provider(AllianceAuthBuiltinSRPProvider())
register_srp_provider(AaSrpProvider())


def _provider_key(key) -> str:
    key = (key or "auto").strip() or "auto"
    return PROVIDER_ALIASES.get(key, key)


def get_srp_provider(key: str = "auto") -> SRPProvider | None:
    key = _provider_key(key)
    if key != "auto":
        provider = _PROVIDERS.get(key)
        return provider if provider and provider.available() else None
    for provider_key in AUTO_PROVIDER_ORDER:
        provider = _PROVIDERS.get(provider_key)
        if provider and provider.available():
            return provider
    for provider_key, provider in _PROVIDERS.items():
        if provider_key not in AUTO_PROVIDER_ORDER and provider.available():
            return provider
    return None


def create_srp_link(operation, provider_key: str = "auto") -> SRPLinkResult:
    provider = get_srp_provider(provider_key)
    if provider is None:
        key = (provider_key or "auto").strip() or "auto"
        if key == "auto":
            return SRPLinkResult(key, message="No supported SRP provider is available.")
        return SRPLinkResult(key, message=f"SRP provider '{key}' is not installed.")
    return provider.create_for_operation(operation)
