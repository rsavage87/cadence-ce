"""Mounted at /security/ inside the `web` namespace (slice 37): the Device security screen. Keep these names: the change log and the
device drawer's Security tab link to them. Device models are named by their UUID; a device by its tag (any letter case)."""
from django.urls import path

from . import views_cyber_facts, views_cyber_updates
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
    path("devices/<str:tag>/facts/", views_cyber_facts.record_facts, name="cybersecurity_facts"),  # the device drawer's Record facts
    # Slice 38: software updates (the Security tab's doors; a release row's batch)
    path("devices/<str:tag>/updates/open/", views_cyber_updates.update_open, name="cybersecurity_update_open"),
    path("devices/<str:tag>/updates/record/", views_cyber_updates.update_record, name="cybersecurity_update_record"),
    path("updates/<uuid:pk>/confirm/", views_cyber_updates.update_confirm, name="cybersecurity_update_confirm"),
    path("updates/<uuid:pk>/remove/", views_cyber_updates.update_remove, name="cybersecurity_update_remove"),
    path("releases/<uuid:pk>/open-updates/", views_cyber_updates.release_open_updates, name="cybersecurity_release_open_updates"),
]
