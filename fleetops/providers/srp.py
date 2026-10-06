"""SRP provider abstraction.

FleetOps deliberately keeps SRP integration behind a tiny provider interface so a
future Better SRP app (or any community SRP app) can register without changing
FleetOperation core logic.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from django.apps import apps
from django.urls import NoReverseMatch, reverse

from allianceauth.eveonline.models import EveCharacter


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

    @staticmethod
    def _required_unknown_fields(model, supplied: dict) -> list[str]:
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

        unknown = self._required_unknown_fields(model, values)
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


register_srp_provider(AllianceAuthBuiltinSRPProvider())


def get_srp_provider(key: str = "auto") -> SRPProvider | None:
    key = (key or "auto").strip()
    if key != "auto":
        provider = _PROVIDERS.get(key)
        return provider if provider and provider.available() else None
    # Deterministic priority; future Better SRP provider can be configured by key.
    preferred = ["better_srp", "aa_srp", "allianceauth_builtin"]
    for provider_key in preferred:
        provider = _PROVIDERS.get(provider_key)
        if provider and provider.available():
            return provider
    for provider in _PROVIDERS.values():
        if provider.available():
            return provider
    return None


def create_srp_link(operation, provider_key: str = "auto") -> SRPLinkResult:
    provider = get_srp_provider(provider_key)
    if provider is None:
        return SRPLinkResult(provider_key or "auto", message="No supported SRP provider is available.")
    return provider.create_for_operation(operation)
