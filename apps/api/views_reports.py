"""
Reports (slice 19, part C): the Overview, the eight standard reports, custom reports, and report emails.
"""

from datetime import date

from rest_framework import viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.reports.services import REPORTS, overview_kpis, report_meta, run_report

from .permissions import ModulePermission
from .tenancy import TenantAPIMixin


class OverviewViewSet(TenantAPIMixin, viewsets.ViewSet):
    permission_classes = [IsAuthenticated, ModulePermission]
    module = "reports"

    def list(self, request):

        today = date.today()
        year, month = int(request.query_params.get("y", today.year)), int(request.query_params.get("m", today.month))
        return Response(overview_kpis(year, month))


class ReportViewSet(TenantAPIMixin, viewsets.ViewSet):
    """The Reports screen's tables as JSON: the list names them, `/<key>/` returns one (columns and rows, as the CSV download)."""

    permission_classes = [IsAuthenticated, ModulePermission]
    module = "reports"

    def list(self, request):
        return Response([{"key": r["key"], "title": r["title"], "subtitle": r["subtitle"]} for r in REPORTS])

    def retrieve(self, request, pk=None):

        meta = report_meta(pk)
        if meta is None:
            raise NotFound("No such report")
        today = date.today()
        data = run_report(pk, today)
        rows = [[round(v, 2) if isinstance(v, float) else v for v in row] for row in data["rows"]]  # as the CSV: two decimals
        return Response({"key": pk, "title": meta["title"], "as_of": today, "columns": data["columns"], "rows": rows})


def register(router):
    router.register("overview", OverviewViewSet, basename="overview")
    router.register("reports", ReportViewSet, basename="report")
