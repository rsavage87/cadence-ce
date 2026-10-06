"""Mounted at /settings/ inside the `web` namespace. `web:settings` is the page; the shell's nav links to it."""
from django.urls import path

from . import views_imports as imp
from . import views_settings as v

urlpatterns = [
    path("", v.settings_page, name="settings"),
    path("portal/", v.settings_portal, name="settings_portal"),
    path("portal/links/", v.settings_dept_links, name="settings_dept_links"),
    path("policy/", v.settings_policy, name="settings_policy"),
    path("policy/reset/", v.settings_policy_reset, name="settings_policy_reset"),
    path("targets/", v.settings_targets, name="settings_targets"),
    path("time-zone/", v.settings_time_zone, name="settings_time_zone"),  # slice 21: the facility's time zone
    # Slice 23: Import data (views_imports): the kinds and the runs, a kind's template, an upload, and one run's steps
    path("import/", imp.imports, name="imports"),
    path("import/template/<str:kind>.csv", imp.import_template, name="import_template"),
    path("import/<uuid:pk>/", imp.import_run, name="import_run"),
    path("import/<uuid:pk>/samples/", imp.import_samples, name="import_samples"),
    path("import/<uuid:pk>/columns/", imp.import_columns, name="import_columns"),
    path("import/<uuid:pk>/next/", imp.import_next, name="import_next"),
    path("import/<uuid:pk>/import/", imp.import_start, name="import_start"),
    path("import/<uuid:pk>/discard/", imp.import_discard, name="import_discard"),
    path("import/<uuid:pk>/notes.csv", imp.import_notes, name="import_notes"),
]
