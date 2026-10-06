from django.apps import apps


def _label(obj):
    for attr in ("name", "name_en", "type_name", "solar_system_name"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return ""


def item_type_names(type_ids):
    if not apps.is_installed("eve_sde"):
        return {}
    try:
        model = apps.get_model("eve_sde", "ItemType")
        return {obj.pk: _label(obj) for obj in model.objects.filter(pk__in=list(type_ids))}
    except Exception:
        return {}


def solar_system_names(system_ids):
    if not apps.is_installed("eve_sde"):
        return {}
    try:
        model = apps.get_model("eve_sde", "SolarSystem")
        return {obj.pk: _label(obj) for obj in model.objects.filter(pk__in=list(system_ids))}
    except Exception:
        return {}


def stargate_edges():
    """Return undirected solar-system adjacency from django-eveonline-sde.

    Current django-eveonline-sde stores Stargate.solar_system and
    Stargate.destination as SolarSystem foreign keys.  A small fallback is
    retained so FleetOps degrades cleanly with older/community SDE schemas.
    """
    if not apps.is_installed("eve_sde"):
        return {}
    try:
        model = apps.get_model("eve_sde", "Stargate")
    except Exception:
        return {}

    field_names = {f.name for f in model._meta.get_fields()}

    # Current django-eveonline-sde schema.
    if {"solar_system", "destination"}.issubset(field_names):
        graph = {}
        try:
            rows = model.objects.values_list("solar_system_id", "destination_id")
            for source_id, destination_id in rows.iterator():
                if not source_id or not destination_id:
                    continue
                a, b = int(source_id), int(destination_id)
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set()).add(a)
            return graph
        except Exception:
            return {}

    source_candidates = ["solar_system", "source_solar_system", "from_solar_system", "system"]
    destination_candidates = ["destination_solar_system", "to_solar_system", "destination_system"]
    source = next((f for f in source_candidates if f in field_names), None)
    destination = next((f for f in destination_candidates if f in field_names), None)
    if not source or not destination:
        destination_gate = next((f for f in ("destination", "destination_stargate") if f in field_names), None)
        if not source or not destination_gate:
            return {}
        graph = {}
        try:
            for gate in model.objects.select_related(source, destination_gate, f"{destination_gate}__{source}").all():
                a = getattr(getattr(gate, source), "pk", getattr(gate, f"{source}_id", None))
                dst_gate = getattr(gate, destination_gate, None)
                b = getattr(getattr(dst_gate, source, None), "pk", None) if dst_gate else None
                if a and b:
                    graph.setdefault(int(a), set()).add(int(b))
                    graph.setdefault(int(b), set()).add(int(a))
            return graph
        except Exception:
            return {}

    graph = {}
    try:
        for gate in model.objects.all():
            a = getattr(gate, f"{source}_id", None) or getattr(getattr(gate, source, None), "pk", None)
            b = getattr(gate, f"{destination}_id", None) or getattr(getattr(gate, destination, None), "pk", None)
            if a and b:
                graph.setdefault(int(a), set()).add(int(b))
                graph.setdefault(int(b), set()).add(int(a))
        return graph
    except Exception:
        return {}
