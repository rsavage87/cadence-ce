"""
Reports screen (slice 7): the mock's eight reports on one page, the list on the left and the selected report on the
right. Every report is as of today; its numbers come from apps.reports.services.run_report and its chart geometry from
apps.web.reports.present. Views parse the key, call the report, and render.

The list links swap #rep-body (list and panel together, so the active item follows the selection) and push the URL,
so /reports/<key>/ renders as a full page too. Each report also downloads as CSV: the same table the API serves.
"""
import csv
from datetime import date

from django.http import Http404, HttpResponse
from django.shortcuts import render

from apps.accounts.models import Level, Module
from apps.reports import services as rs

from .decorators import web_view
from .htmx import is_partial
from .reports import present


def _meta(key: str | None) -> dict:
    meta = rs.report_meta(key or rs.REPORT_KEYS[0])
    if meta is None:
        raise Http404("No such report")
    return meta


@web_view(Module.REPORTS, Level.VIEW)
def reports(request, key=None):
    meta = _meta(key)
    today = date.today()
    data = rs.run_report(meta["key"], today)
    ctx = {"nav_active": "reports", "today": today, "reports": rs.REPORTS, "report": meta, "r": data, "p": present(meta["key"], data),
           "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW),
           "can_view_recalls": request.user.has_level(Module.RECALLS, Level.VIEW)}
    return render(request, "web/_reports_body.html" if is_partial(request, "rep-body") else "web/reports.html", ctx)


def _cell(v):
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, date):
        return v.isoformat()
    return "" if v is None else v


@web_view(Module.REPORTS, Level.VIEW)
def report_csv(request, key):
    meta = _meta(key)
    today = date.today()
    data = rs.run_report(meta["key"], today)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="cadence-{meta["key"]}-{today:%Y-%m-%d}.csv"'
    writer = csv.writer(response)
    writer.writerow(data["columns"])
    for row in data["rows"]:
        writer.writerow([_cell(v) for v in row])
    return response
