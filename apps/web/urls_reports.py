"""Mounted at /reports/ inside the `web` namespace. `web:report` takes a report key: one of apps.reports.services.REPORTS, or a
custom report's "custom-<id>" (slice 18); `web:reports` (no key) shows the first one. `web:report_csv` downloads the report's table.
The custom report builder's and the survey binder's routes come before the key routes."""
from django.urls import path

from . import views_custom_reports as vc
from . import views_reports as v
from . import views_survey as vs

urlpatterns = [
    path("", v.reports, name="reports"),
    # slice 18: building custom reports
    path("custom/new/", vc.custom_new, name="custom_report_new"),
    path("custom/fields/", vc.custom_fields, name="custom_report_fields"),
    path("custom/preview/", vc.custom_preview, name="custom_report_preview"),
    path("custom-<uuid:pk>/edit/", vc.custom_edit, name="custom_report_edit"),
    path("custom-<uuid:pk>/delete/", vc.custom_delete, name="custom_report_delete"),
    # slice 25: the survey binder (never a report key: it is not in REPORTS, so it is never emailed)
    path("survey/", vs.survey, name="survey"),
    path("survey/gaps.csv", vs.survey_gaps_csv, name="survey_gaps_csv"),
    path("survey/<slug:section>/<slug:table>.csv", vs.survey_csv, name="survey_csv"),
    path("<slug:key>.csv", v.report_csv, name="report_csv"),
    path("<slug:key>/", v.reports, name="report"),
    path("<slug:key>/schedule/", v.report_schedule, name="report_schedule"),  # slice 13: email me this report
]
