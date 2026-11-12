# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""The Doctrine settings page of the administration: who may ping which doctrine, who may type a doctrine name, and the
link to the ship order of each doctrine. One form, one Save; every change is audited as ``configuration.*``.
"""

from django.contrib import messages
from django.contrib.auth.models import Group
from django.db import transaction
from django.shortcuts import redirect
from django.utils.translation import gettext as _

from fleetops import texts
from fleetops.decorators import fleetops_permission_required
from fleetops.models import DoctrineAccess, DoctrineSettings, DoctrineShipOrder
from fleetops.providers.doctrines import FITTINGS_DOCTRINE_SOURCE, get_doctrines
from fleetops.services.audit import audit
from fleetops.views import _render

# Posted: "everybody" (the default) or "groups".
CUSTOM_EVERYBODY = "everybody"
CUSTOM_GROUPS = "groups"


def _key(source, external_id) -> str:
    return f"{source}|{external_id}"


def _group_values(group_ids, names) -> dict:
    ids = sorted(group_ids)
    return {"groups": ids, "group_names": [names[pk] for pk in ids if pk in names]}


def _posted_ids(post, name, valid):
    """The group IDs posted under ``name``; None when one of them is not an Alliance Auth group."""
    ids = set()
    for value in post.getlist(name):
        if not (value.isascii() and value.isdigit() and int(value) in valid):
            return None
        ids.add(int(value))
    return ids


def _save_access(request, doctrines, rows, names):
    """Store the group choices of the doctrine rows; returns the number of doctrines changed, or None on a bad form.

    The form posts ``key_<n>`` (the doctrine) and ``groups_<n>`` (its groups; nothing when none is chosen) for each row.
    """
    known = {_key(c.source, c.external_id): c.name for c in doctrines}
    for row in rows.values():
        known.setdefault(_key(row.doctrine_source, row.doctrine_external_id), row.doctrine_external_id)
    changes = []
    seen = set()
    for field, key in request.POST.items():
        index = field[len("key_"):]
        if not (field.startswith("key_") and index.isascii() and index.isdigit()):
            continue
        # A key that is neither a doctrine nor a stored record (a forged form) is ignored.
        if key not in known or key in seen:
            continue
        seen.add(key)
        new_ids = _posted_ids(request.POST, f"groups_{index}", set(names))
        if new_ids is None:
            return None
        source, _sep, external_id = key.partition("|")
        row = rows.get((source, external_id))
        old_ids = {group.pk for group in row.groups.all()} if row is not None else set()
        if new_ids != old_ids:
            changes.append((source, external_id, known[key], row, old_ids, new_ids))
    for source, external_id, name, row, old_ids, new_ids in changes:
        created = row is None
        if created:
            row = DoctrineAccess.objects.create(doctrine_source=source, doctrine_external_id=external_id)
        row.groups.set(new_ids)
        row.save(update_fields=["updated_at"])
        audit(
            request.user,
            "configuration.create" if created else "configuration.update",
            row,
            None if created else {"doctrine_name": name, **_group_values(old_ids, names)},
            {"doctrine_name": name, **_group_values(new_ids, names)},
            reason=texts.AUDIT_CONFIG_CHANGED(),
        )
    return len(changes)


def _save_custom(request, names):
    """Store the custom doctrine name setting; returns whether it changed, or None on a bad form."""
    mode = request.POST.get("custom_mode", CUSTOM_EVERYBODY)
    if mode not in (CUSTOM_EVERYBODY, CUSTOM_GROUPS):
        return None
    new_ids = _posted_ids(request.POST, "custom_groups", set(names))
    if new_ids is None:
        return None
    restricted = mode == CUSTOM_GROUPS
    settings_row = DoctrineSettings.get_solo()
    old_ids = {group.pk for group in settings_row.custom_name_groups.all()}
    # The groups stay stored while the setting is off, so switching back and forth loses nothing.
    if not restricted and "custom_groups" not in request.POST:
        new_ids = old_ids
    if restricted == settings_row.custom_name_restricted and new_ids == old_ids:
        return False
    old = {"custom_name_restricted": settings_row.custom_name_restricted, **_group_values(old_ids, names)}
    settings_row.custom_name_restricted = restricted
    settings_row.save()
    settings_row.custom_name_groups.set(new_ids)
    audit(
        request.user,
        "configuration.update",
        settings_row,
        old,
        {"custom_name_restricted": restricted, **_group_values(new_ids, names)},
        reason=texts.AUDIT_CONFIG_UPDATED(),
    )
    if restricted and not new_ids:
        messages.warning(request, _("No group is chosen: only administrators can use a custom doctrine name."))
    return True


@fleetops_permission_required("fleetops.manage_configuration")
def doctrine_settings(request):
    """Every doctrine of the doctrine app with its groups and ship order, and the custom doctrine name setting."""
    doctrines = get_doctrines()
    groups = list(Group.objects.order_by("name"))
    names = {group.pk: group.name for group in groups}
    rows = {
        (row.doctrine_source, row.doctrine_external_id): row
        for row in DoctrineAccess.objects.prefetch_related("groups")
    }
    if request.method == "POST":
        with transaction.atomic():
            changed = _save_access(request, doctrines, rows, names)
            custom = _save_custom(request, names) if changed is not None else None
            if changed is None or custom is None:
                transaction.set_rollback(True)
        if changed is None or custom is None:
            messages.error(request, _("The settings could not be saved: choose only groups from the list."))
        else:
            messages.success(request, _("Doctrine settings saved.") if changed or custom else _("Nothing was changed."))
            return redirect("fleetops:doctrine_orders")
    orders = set(
        DoctrineShipOrder.objects.filter(doctrine_source=FITTINGS_DOCTRINE_SOURCE).values_list(
            "doctrine_external_id", flat=True
        )
    )
    present = set()
    items = []
    for choice in doctrines:
        row = rows.get((choice.source, choice.external_id))
        present.add((choice.source, choice.external_id))
        items.append(
            {
                "key": _key(choice.source, choice.external_id),
                "name": choice.name,
                "external_id": choice.external_id,
                "selected": {group.pk for group in row.groups.all()} if row is not None else set(),
                "has_order": choice.source == FITTINGS_DOCTRINE_SOURCE and choice.external_id in orders,
                "can_order": choice.source == FITTINGS_DOCTRINE_SOURCE,
            }
        )
    # Records whose doctrine left the doctrine app are kept and shown; one without groups restricts nothing.
    gone = [
        {
            "key": _key(row.doctrine_source, row.doctrine_external_id),
            "name": row.doctrine_external_id,
            "selected": {group.pk for group in row.groups.all()},
        }
        for key, row in rows.items()
        if key not in present and row.groups.all()
    ]
    for index, item in enumerate(items + gone):
        item["index"] = index
    settings_row = DoctrineSettings.objects.filter(pk=1).first()
    custom_groups = {group.pk for group in settings_row.custom_name_groups.all()} if settings_row else set()
    return _render(
        request,
        "fleetops/configuration/doctrine_orders.html",
        {
            "items": items,
            "gone": gone,
            "groups": groups,
            "custom_restricted": bool(settings_row and settings_row.custom_name_restricted),
            "custom_groups": custom_groups,
        },
    )
