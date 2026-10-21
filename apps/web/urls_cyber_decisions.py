"""Mounted at /security/decisions/ inside the `web` namespace (slice 39): the Device security screen's Decisions tab, a decision's page,
its print and its devices' CSV, and the Record decision, End, and Entered in error modals. A decision is named by its number (SD-26-0004,
any letter case). Keep these names: apps.cybersecurity.rules.URLS and apps.core.history's area reverse them."""
from django.urls import path

from . import views_cyber_decisions as v

urlpatterns = [
    path("", v.decisions_tab, name="security_decisions"),  # Needs attention and the decisions
    path("export.csv", v.decisions_csv, name="security_decisions_csv"),
    path("new/", v.decision_new, name="security_decision_new"),  # ?asset=<tag> | release=<id> | model=<id> | supersedes=<number>
    path("<str:number>/", v.decision, name="security_decision"),
    path("<str:number>/end/", v.decision_end, name="security_decision_end"),
    path("<str:number>/in-error/", v.decision_in_error, name="security_decision_in_error"),
    path("<str:number>/print/", v.decision_print, name="security_decision_print"),
    path("<str:number>/devices.csv", v.decision_devices_csv, name="security_decision_devices_csv"),
]
