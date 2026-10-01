"""Mounted at /print/ inside the `web` namespace (slice 11): printable pages, opened in a new tab (web/print_base.html)."""
from django.urls import path

from . import views_print, views_print_sheets

urlpatterns = [
    path("labels/", views_print.labels, name="labels"),
    path("work-orders/<str:number>/", views_print.wo_print, name="wo_print"),
    path("route-sheets/", views_print_sheets.route_sheets, name="route_sheets"),
    path("reports/<str:key>/", views_print_sheets.report_print, name="report_print"),
]
