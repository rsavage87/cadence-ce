"""Mounted at /security/advisories/ inside the `web` namespace (slice 40): the Device security screen's Advisories tab, an advisory's
page and its devices' CSV, and its modals. An advisory is named by its number (AD-26-0003, any letter case). Keep these names:
apps.cybersecurity.rules.URLS, apps.core.history's areas, and the Record decision and update doors' `from=advisory` answers reverse
them. The fixed paths come before `<str:number>/`."""
from django.urls import path

from . import views_cyber_advisories as v

urlpatterns = [
    path("", v.advisories_tab, name="security_advisories"),  # Needs attention and the list
    path("export.csv", v.advisories_csv, name="security_advisories_csv"),
    path("new/", v.advisory_new, name="security_advisory_new"),
    path("<str:number>/", v.advisory, name="security_advisory"),
    path("<str:number>/edit/", v.advisory_edit, name="security_advisory_edit"),  # received, CVE IDs
    path("<str:number>/sources/new/", v.source_new, name="security_advisory_source_new"),
    path("<str:number>/sources/<uuid:source_id>/edit/", v.source_edit, name="security_advisory_source_edit"),
    path("<str:number>/sources/<uuid:source_id>/revision/", v.source_revision, name="security_advisory_revision"),
    path("<str:number>/sources/<uuid:source_id>/remove/", v.source_remove, name="security_advisory_source_remove"),
    path("<str:number>/models/", v.advisory_models, name="security_advisory_models"),  # Set models; ?model=<id> edits that row
    path("<str:number>/models/<uuid:row_id>/remove/", v.model_remove, name="security_advisory_model_remove"),
    path("<str:number>/confirm/", v.advisory_confirm, name="security_advisory_confirm"),
    path("<str:number>/not-applicable/", v.advisory_not_applicable, name="security_advisory_not_applicable"),
    path("<str:number>/close/", v.advisory_close, name="security_advisory_close"),
    path("<str:number>/reopen/", v.advisory_reopen, name="security_advisory_reopen"),
    path("<str:number>/in-error/", v.advisory_in_error, name="security_advisory_in_error"),
    path("<str:number>/devices.csv", v.advisory_devices_csv, name="security_advisory_devices_csv"),
]
