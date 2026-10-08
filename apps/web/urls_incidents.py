"""Mounted at /incidents/ inside the `web` namespace (slice 28). Keep these names: the nav, the device and work order drawers' hold
banners, the change log, and the survey binder's gaps link to them. `web:incident` takes the incident's number (IN-26-0004) and opens
its drawer, or a full page when opened directly. The fixed paths come before `<str:number>/`."""
from django.urls import path

from . import views_incidents as v

urlpatterns = [
    path("", v.incidents, name="incidents"),
    path("incidents.csv", v.incidents_csv, name="incidents_csv"),
    path("new/", v.incident_new, name="incident_new"),
    path("new/due/", v.incident_due, name="incident_due"),
    path("devices/", v.incident_devices, name="incident_devices"),
    path("<str:number>/", v.incident, name="incident"),
    path("<str:number>/facts/", v.incident_facts, name="incident_facts"),
    path("<str:number>/decide/", v.incident_decide, name="incident_decide"),
    path("<str:number>/reports/", v.incident_reports, name="incident_reports"),
    path("<str:number>/finding/", v.incident_finding, name="incident_finding"),
    path("<str:number>/hold/", v.incident_hold, name="incident_hold"),
    path("<str:number>/investigation/", v.incident_investigation, name="incident_investigation"),
    path("<str:number>/holds/<uuid:pk>/sent/", v.incident_hold_sent, name="incident_hold_sent"),
    path("<str:number>/holds/<uuid:pk>/back/", v.incident_hold_back, name="incident_hold_back"),
    path("<str:number>/holds/<uuid:pk>/release/", v.incident_hold_release, name="incident_hold_release"),
    path("<str:number>/close/", v.incident_close, name="incident_close"),
    path("<str:number>/reopen/", v.incident_reopen, name="incident_reopen"),
    path("<str:number>/in-error/", v.incident_in_error, name="incident_in_error"),
]
