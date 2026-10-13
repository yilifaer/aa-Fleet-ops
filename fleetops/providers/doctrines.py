from dataclasses import dataclass

from allianceauth.services.hooks import get_extension_logger
from django.apps import apps
from django.contrib.auth.models import Group
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import translation

from fleetops.services import links

logger = get_extension_logger(__name__)

# doctrine_source of doctrines picked from the community fittings app.
FITTINGS_DOCTRINE_MODEL = "fittings.doctrine"
FITTINGS_DOCTRINE_SOURCE = f"fittings:{FITTINGS_DOCTRINE_MODEL}"


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


def _doctrine_visibility(model, user):
    """Filter for the Fittings doctrines ``user`` may see; None when every doctrine is shown.

    Fittings' own rule: ``fittings.manage`` sees everything; any other user sees doctrines without
    a category, in a category without groups, or in a category of one of the user's groups. Only
    the Fittings ``Doctrine`` model is filtered, and only when its layout is the one this rule
    was written for; anything else keeps the full list.
    """
    if user is None or model._meta.label_lower != FITTINGS_DOCTRINE_MODEL:
        return None
    if user.has_perm("fittings.manage"):
        return None
    try:
        relation = next(field for field in model._meta.get_fields() if field.name == "category")
        groups = relation.related_model._meta.get_field("groups")
    except Exception:
        return None
    if not (getattr(relation, "many_to_many", False) and groups.many_to_many and groups.related_model is Group):
        return None
    return (
        Q(category__isnull=True)
        | Q(category__groups__isnull=True)
        | Q(category__groups__in=user.groups.all())
    )


def _doctrine_rows(model, user):
    visible = _doctrine_visibility(model, user)
    if visible is not None:
        try:
            # A savepoint, so a failed query leaves an outer transaction usable.
            with transaction.atomic():
                return list(model.objects.filter(visible).distinct()[:500])
        except Exception:
            logger.debug("Could not filter the Fittings doctrines for %s; listing all.", user, exc_info=True)
    return list(model.objects.all()[:500])


def get_doctrines(user=None) -> list[DoctrineChoice]:
    """Best-effort soft integration with the community `fittings` app.

    FleetOps never hard-depends on Fittings' private model layout. We discover
    a doctrine-like model and readable name field and fall back to None/Custom
    when an installed revision exposes a layout we do not recognize.

    ``user`` None lists every doctrine (configuration forms); with a user, Fittings' doctrines
    are limited to the ones Fittings shows that user (category groups).
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
                for obj in _doctrine_rows(model, user):
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


def _fittings_doctrine_id(source, external_id) -> int | None:
    if source != FITTINGS_DOCTRINE_SOURCE:
        return None
    text = str(external_id or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def doctrine_url(source: str, external_id: str) -> str:
    """Absolute link to a Fittings doctrine page; "" for other sources or when Fittings has no route."""
    doctrine_id = _fittings_doctrine_id(source, external_id)
    if doctrine_id is None:
        return ""
    try:
        return links.absolute_url(reverse("fittings:view_doctrine", args=[doctrine_id]))
    except NoReverseMatch:
        return ""


def _ship_name(fitting) -> str:
    try:
        ship_type = fitting.ship_type
    except ObjectDoesNotExist:
        ship_type = None
    for attribute in ("name_en", "name"):
        name = getattr(ship_type, attribute, None) if ship_type is not None else None
        if name:
            return str(name)
    return str(fitting.ship_type_type_id)


def doctrine_fits_snapshot(source: str, external_id: str) -> list[dict]:
    """The fits of a Fittings doctrine in doctrine order, for the fleet record.

    ``[{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}]``; [] for other sources
    and whenever Fittings cannot be read.
    """
    doctrine_id = _fittings_doctrine_id(source, external_id)
    if doctrine_id is None:
        return []
    try:
        doctrine_model = apps.get_model("fittings", "Doctrine")
    except LookupError:
        return []
    try:
        # Through rows keep the order the fits were added to the doctrine. The ship type is prefetched
        # rather than joined so a fit whose SDE row is missing still shows up (with its type id).
        rows = (
            doctrine_model.fittings.through.objects.filter(doctrine_id=doctrine_id)
            .select_related("fitting")
            .prefetch_related("fitting__ship_type")
            .order_by("pk")
        )
        # Ship names are stored in English, whatever language the request uses.
        with translation.override("en"):
            return [
                {
                    "fit_id": row.fitting.pk,
                    "fit": str(row.fitting.name),
                    "ship_type_id": row.fitting.ship_type_type_id,
                    "ship": _ship_name(row.fitting),
                }
                for row in rows
            ]
    except Exception:
        logger.debug("Could not read the fits of doctrine %s from Fittings.", doctrine_id, exc_info=True)
        return []
