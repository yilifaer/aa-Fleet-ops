from dataclasses import dataclass

from django.apps import apps


@dataclass(slots=True)
class DoctrineChoice:
    external_id: str
    name: str
    source: str = "fittings"


def _fittings_configs():
    """Find configs belonging to the community `fittings` package.

    Current Fittings exposes permissions under both `fitting` and `doctrine`
    labels, so FleetOps deliberately discovers both without importing private
    module paths. This keeps the integration soft across Fittings releases.
    """
    matches = []
    for config in apps.get_app_configs():
        if (
            config.name == "fittings"
            or config.name.startswith("fittings.")
            or config.label in {"fittings", "fitting", "doctrine"}
        ):
            matches.append(config)
    return matches


def get_doctrines(user=None) -> list[DoctrineChoice]:
    """Best-effort soft integration with the community `fittings` app.

    FleetOps never hard-depends on Fittings' private model layout. We discover
    a doctrine-like model and readable name field and fall back to None/Custom
    when an installed revision exposes a layout we do not recognize.
    """
    candidates = []
    seen = set()
    for config in _fittings_configs():
        for model in config.get_models():
            if model.__name__.lower() not in {
                "doctrine",
                "doctrinemodel",
                "doctrinegroup",
                "doctrinefit",
            }:
                continue
            field_names = {f.name for f in model._meta.get_fields()}
            name_field = next(
                (name for name in ("name", "title", "doctrine_name") if name in field_names),
                None,
            )
            if not name_field:
                continue
            try:
                queryset = model.objects.all()
                for obj in queryset[:500]:
                    key = (model._meta.label_lower, str(obj.pk))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        DoctrineChoice(
                            external_id=str(obj.pk),
                            name=str(getattr(obj, name_field)),
                            source=f"fittings:{model._meta.label_lower}",
                        )
                    )
            except Exception:
                continue
    return sorted(candidates, key=lambda row: row.name.casefold())
