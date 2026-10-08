"""Mounted at /incidents/ inside the `web` namespace (slice 28). Keep these names: the nav, the device and work order drawers' hold
banners, the change log, and the survey binder's gaps link to them. `web:incident` takes the incident's number (IN-26-0004) and opens
its drawer, or a full page when opened directly."""
from django.urls import path

from . import views_incidents as v

urlpatterns = [
    path("", v.incidents, name="incidents"),
    path("<str:number>/", v.incident, name="incident"),
]
