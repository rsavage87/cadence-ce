"""Mounted at /reports/ inside the `web` namespace. `web:report` takes a report key from apps.reports.services.REPORTS;
`web:reports` (no key) shows the first one. `web:report_csv` downloads the report's table."""
from django.urls import path

from . import views_reports as v

urlpatterns = [
    path("", v.reports, name="reports"),
    path("<slug:key>.csv", v.report_csv, name="report_csv"),
    path("<slug:key>/", v.reports, name="report"),
    path("<slug:key>/schedule/", v.report_schedule, name="report_schedule"),  # slice 13: email me this report
]
