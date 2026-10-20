"""Mounted at /security/ inside the `web` namespace (slice 37): the Device security screen. Keep these names: the change log and the
device drawer's Security tab link to them. Device models are named by their UUID; a device by its tag (any letter case)."""
from django.urls import path

from . import views_cybersecurity as v

urlpatterns = [
    path("", v.fleet, name="cybersecurity"),  # Fleet: one row per model, its head figures
    path("export.csv", v.fleet_csv, name="cybersecurity_csv"),
    path("devices.csv", v.devices_csv, name="cybersecurity_devices_csv"),
    path("models/", v.models_tab, name="cybersecurity_models"),
    path("models/<uuid:pk>/", v.model_panel, name="cybersecurity_model"),  # a model's profile, releases, and devices
    path("models/<uuid:pk>/releases/new/", v.release_new, name="cybersecurity_release_new"),
    path("models/<uuid:pk>/set-release/", v.set_release, name="cybersecurity_set_release"),  # bulk: one model's devices
    path("releases/<uuid:pk>/edit/", v.release_edit, name="cybersecurity_release_edit"),
    path("releases/<uuid:pk>/remove/", v.release_remove, name="cybersecurity_release_remove"),
    path("changes/", v.changes, name="cybersecurity_changes"),  # Changes for IT
    path("changes.csv", v.changes_csv, name="cybersecurity_changes_csv"),
    path("devices/<str:tag>/facts/", __import__("apps.web.views_cyber_facts", fromlist=["record_facts"]).record_facts, name="cybersecurity_facts"),
]
