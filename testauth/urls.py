# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.urls import include, path

from allianceauth import urls

urlpatterns = [
    path("", include(urls)),
]
