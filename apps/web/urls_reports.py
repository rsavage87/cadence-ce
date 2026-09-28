"""Mounted at /reports/ inside the `web` namespace. `web:report` takes a report key from apps.reports.services.REPORTS;
`web:reports` (no key) shows the first one. `web:report_csv` downloads the report's table."""
from django.urls import path

from . import views_reports as v

urlpatterns = [
    path("", v.reports, name="reports"),
    path("<slug:key>.csv", v.report_csv, name="report_csv"),
    path("<slug:key>/", v.reports, name="report"),
]
