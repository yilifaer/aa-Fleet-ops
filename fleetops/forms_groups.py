# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Forms of the group pages: the group access rows of FleetOps Administration."""

from django import forms

from fleetops.models import GroupAccess


class GroupAccessForm(forms.ModelForm):
    """What the leaders of one Alliance Auth group may do in FleetOps."""

    class Meta:
        model = GroupAccess
        fields = ["group", "mode", "fleet_types", "srp_enabled", "is_active", "note"]
        widgets = {"fleet_types": forms.CheckboxSelectMultiple}


def group_list_prepare(objects) -> None:
    """Fill in what the list cells and the leaders card of ``objects`` need, with a fixed number of queries."""
    return None


def group_cell(obj, field_name):
    """The value of a list cell of the Groups section; ``NotImplemented`` leaves it to the generic rules."""
    return NotImplemented
