from django.urls import include, path

from allianceauth import urls

urlpatterns = [
    path("", include(urls)),
]
