"""
Reports screen (slice 7): the mock's eight reports on one page, the list on the left and the selected report on the
right. Every report is as of today; its numbers come from apps.reports.services.run_report and its chart geometry from
apps.web.reports.present. Views parse the key, call the report, and render.

The list links swap #rep-body (list and panel together, so the active item follows the selection) and push the URL,
so /reports/<key>/ renders as a full page too. Each report also downloads as CSV: the same table the API serves.
"""
from datetime import date

from django.http import Http404
from django.shortcuts import render

from apps.accounts.models import Level, Module
from apps.reports import services as rs

from .decorators import web_view
from .exports import csv_response
from .htmx import is_partial
from .reports import present


def _today() -> date:
    """The reports' clock, in one place so tests can pin it (fixture `freeze_today`)."""
    return date.today()


def _meta(key: str | None) -> dict:
    meta = rs.report_meta(key or rs.REPORT_KEYS[0])
    if meta is None:
        raise Http404("No such report")
    return meta


@web_view(Module.REPORTS, Level.VIEW)
def reports(request, key=None):
    meta = _meta(key)
    today = _today()
    data = rs.run_report(meta["key"], today)
    partial = is_partial(request, "rep-body")
    ctx = {"nav_active": "reports", "today": today, "reports": rs.REPORTS, "report": meta, "r": data, "p": present(meta["key"], data), "partial": partial,
           "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW),
           "can_view_recalls": request.user.has_level(Module.RECALLS, Level.VIEW)}
    return render(request, "web/_reports_body.html" if partial else "web/reports.html", ctx)


@web_view(Module.REPORTS, Level.VIEW)
def report_csv(request, key):
    meta = _meta(key)
    today = _today()
    data = rs.run_report(meta["key"], today)
    return csv_response(f"cadence-{meta['key']}-{today:%Y-%m-%d}.csv", data["columns"], data["rows"])
