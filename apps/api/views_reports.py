"""
Reports (slice 19, part C): the Overview, the eight standard reports, the facility's custom reports, and report emails, with the
Reports screen's doors (apps/reports/permissions.py). Every endpoint here refuses scoped users (apps.workorders.scoping): every
report covers the whole facility, as the screen says when it refuses them.

GET    /api/v1/overview/?y=&m=        The Overview's KPIs for a month (default: this month). Reports View.
GET    /api/v1/reports/               The eight standard reports: key, title, subtitle. Reports View. Only the eight: custom reports
                                      are /api/v1/custom-reports/.
GET    /api/v1/reports/<key>/         One standard report as of today: columns and rows, floats to two decimals as the CSV. Reports View.

Custom reports (apps/reports/custom.py; the Reports screen's "+ Custom report" builder, a custom report's panel, and its Delete):
GET    /api/v1/custom-reports/        The facility's custom reports, by name in any letter case: id, key ("custom-<id>": its key on the
                                      screen and in report emails), name, source, source_label, description (the source, filters,
                                      grouping, and sort in words). Reports View.
GET    /api/v1/custom-reports/<id>/   One, with its definition as it runs (what the builder's Edit starts from): columns, filters,
                                      group_by, sort. Reports View.
POST   /api/v1/custom-reports/        {"name", "source", "columns": [...], "filters": {...}, "group_by", "sort"}: adds one through
                                      create_custom_report (201, the report as GET shows it). Reports Edit, and View on what the source
                                      lists: Work orders View for work_orders, labor, and parts, Equipment View for devices (a 403 in
                                      apps/reports/permissions.py build_refusal's words otherwise). A definition the registry refuses is a
                                      400 keyed by part as clean_definition raises it ("source", "columns", "filters", "filter_<key>",
                                      "date", "group_by", "sort"), or "name". Filters are a JSON object, as GET shows them, e.g.
                                      {"type": ["repair"], "date": {"field": "completed", "period": "last_30"}}.
PATCH  /api/v1/custom-reports/<id>/   Any of those parts; the others keep what GET shows (update_custom_report). Reports Edit, and View
                                      on what the report lists now and on what it would list (a 403 in build_refusal's words).
DELETE /api/v1/custom-reports/<id>/   Deletes it and everyone's email schedules for it (delete_custom_report); 200 with the id, key,
                                      name, and how many schedules went with it ("email_schedules_removed"). Reports Edit.
GET or POST /api/v1/custom-reports/<id>/run/
                                      Runs it as of today, as the screen and its CSV do (run_any), no body: columns, kinds, rows (floats
                                      to two decimals), total (rows, or groups), records, truncated (more than custom.MAX_ROWS), grouped,
                                      totals, left_out (chosen columns a grouped table leaves out), description (with a relative period's
                                      dates), problem (why a report whose source is no longer offered lists nothing). Reports View, and
                                      View on what it lists (a 403 in apps/reports/permissions.py refusal's words otherwise).
A write body is a JSON object; an unknown field is refused (400). id, key, source_label, and description may be sent back as GET
showed them (a PATCH of what was read); any other value for them is refused. Another facility's report is a 404.

Report emails, self-service (apps/reports/subscriptions.py; the Reports screen's Schedule button). Reports View, as the button:
GET       /api/v1/report-emails/      The signed-in user's own schedules, by report key: report (a standard key or "custom-<id>"), title,
                                      frequency ("weekly", "monthly"), frequency_label, next_on (the next sending day, as the Schedule
                                      modal says it), not_sent (why the daily job would skip it, in its words, else null).
PUT or POST /api/v1/report-emails/    {"report": "<key>", "frequency": "weekly" | "monthly" | "" (off)}: sets the user's own schedule
                                      for one report through set_subscription, and returns it as GET shows it (frequency "" once off).
                                      Its refusals are 400s in its words: no such report, a frequency it does not offer, and, turning
                                      one on, no email address or a custom report listing what the user cannot see. Only those two
                                      fields: it always sets the requesting user's own, sent to their account's email.
"""

import uuid
from datetime import date

from django.db.models.functions import Lower
from django.http import QueryDict
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.reports import custom
from apps.reports import permissions as rep_perms
from apps.reports import subscriptions as subs
from apps.reports.models import CustomReport, ReportSubscription
from apps.reports.services import REPORTS, overview_kpis, report_meta, run_any, run_report
from apps.tenants.context import get_current_tenant

from . import serializers_reports as sr
from .base import ApiViewSet, _via_service

NO_TENANT = "Pick a tenant first (Admin, Tenants)."


def _today() -> date:
    """The reports' clock, in one place so tests can pin it (the screen's is apps.web.views_reports._today): the facility's today,
    since the API works in its time zone (apps.api.tenancy)."""
    return timezone.localdate()


def _object(request) -> dict:
    """A write body as a dict: JSON as sent, a form's fields with "columns" as a list. Anything else (a JSON list) is refused."""
    data = request.data
    if isinstance(data, QueryDict):
        return {k: data.getlist(k) if k == "columns" else data.get(k) for k in data}
    if isinstance(data, dict):
        return dict(data)
    raise DRFValidationError({"detail": "Send a JSON object."})


class InFacility:
    """A superuser who has not picked a tenant would otherwise read or change nobody's reports."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if get_current_tenant() is None:
            raise PermissionDenied(NO_TENANT)


class OverviewViewSet(ApiViewSet):
    module = rep_perms.MODULE
    MIN_YEAR, MAX_YEAR = 1900, 2200

    def list(self, request):
        today = _today()
        try:
            year, month = int(request.query_params.get("y", today.year)), int(request.query_params.get("m", today.month))
        except (TypeError, ValueError):
            raise DRFValidationError({"detail": "y and m must be whole numbers."}) from None
        if not (1 <= month <= 12 and self.MIN_YEAR <= year <= self.MAX_YEAR):
            raise DRFValidationError({"detail": f"m must be 1 to 12 and y {self.MIN_YEAR} to {self.MAX_YEAR}."})
        return Response(overview_kpis(year, month))


class ReportViewSet(ApiViewSet):
    """The Reports screen's eight standard tables as JSON: the list names them, `/<key>/` returns one (columns and rows, as the CSV
    download). Custom reports are CustomReportViewSet's."""

    module = rep_perms.MODULE

    def list(self, request):
        return Response([{"key": r["key"], "title": r["title"], "subtitle": r["subtitle"]} for r in REPORTS])

    def retrieve(self, request, pk=None):
        meta = report_meta(pk)
        if meta is None:
            raise NotFound("No such report")
        today = _today()
        data = run_report(pk, today)
        rows = [[sr.two_decimals(v) for v in row] for row in data["rows"]]  # as the CSV: two decimals
        return Response({"key": pk, "title": meta["title"], "as_of": today, "columns": data["columns"], "rows": rows})


class CustomReportViewSet(InFacility, ApiViewSet):
    """The facility's custom reports, through apps.reports.custom's services with by=the user, as the builder saves them."""

    module = rep_perms.MODULE
    delete_level = rep_perms.BUILD_LEVEL  # deleting is building's, as on the screen (it shows nothing, so no source check)
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]  # PATCH changes any part; there is no PUT
    PARTS = custom.EDITABLE  # name, source, columns, filters, group_by, sort
    SHOWN = ("id", "key", "source_label", "description")  # what GET shows and no write sets

    @property
    def write_level(self):
        # Running is reading (a POST only so a client can ask for it as an action); everything else builds.
        return rep_perms.VIEW_LEVEL if self.action == "run" else rep_perms.BUILD_LEVEL

    def _get(self, pk) -> CustomReport:
        try:
            pk = uuid.UUID(str(pk))
        except ValueError:
            raise NotFound("No such custom report.") from None
        report = CustomReport.objects.filter(pk=pk).first()  # tenant-scoped: another facility's report is not found
        if report is None:
            raise NotFound("No such custom report.")
        return report

    def _parts(self, request, report: CustomReport | None) -> dict:
        """The parts a write sets, from its body. Unknown fields are refused; so are the shown-only ones, unless sent back as shown."""
        body = _object(request)
        unknown = set(body) - set(self.PARTS) - set(self.SHOWN)
        if unknown:
            raise DRFValidationError({"detail": f"Unknown fields: {', '.join(sorted(unknown))}."})
        shown = sr.CustomReportDetailSerializer(report).data if report is not None else {}
        errors = {f: [f"{f} is not set by a write; send it as GET shows it, or leave it out."] for f in self.SHOWN
                  if f in body and (report is None or body[f] != shown[f])}
        if body.get("name") is not None and not isinstance(body["name"], str):
            errors["name"] = ["Enter the name as text."]
        if errors:
            raise DRFValidationError(errors)
        return {k: body[k] for k in self.PARTS if k in body}

    def _may_build_on(self, user, source) -> None:
        """A builder builds only on what they can see (build_refusal): a 403 in its words. A source that is not one is the
        service's 400."""
        refusal = rep_perms.build_refusal(user, source) if isinstance(source, str) else ""
        if refusal:
            raise PermissionDenied(refusal)

    def list(self, request):
        reports = CustomReport.objects.order_by(Lower("name"), "pk")  # as the screen's list (custom.menu)
        return Response(sr.CustomReportSerializer(reports, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(sr.CustomReportDetailSerializer(self._get(pk)).data)

    def create(self, request):
        parts = self._parts(request, None)
        self._may_build_on(request.user, parts.get("source"))
        report = _via_service(custom.create_custom_report, name=parts.get("name"), source=parts.get("source"), columns=parts.get("columns"),
                              filters=parts.get("filters"), group_by=parts.get("group_by", ""), sort=parts.get("sort", ""), by=request.user)
        return Response(sr.CustomReportDetailSerializer(report).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        report = self._get(pk)
        # As the builder's Edit: someone who cannot see what the report lists does not edit it (Delete stays theirs).
        self._may_build_on(request.user, report.source)
        parts = self._parts(request, report)
        self._may_build_on(request.user, parts.get("source", report.source))
        # The parts not sent keep what GET shows, the definition as it runs, as the builder's Edit form sends them back.
        current = custom.runnable(report) or {}
        _via_service(custom.update_custom_report, report, by=request.user, **{**current, **parts})
        return Response(sr.CustomReportDetailSerializer(report).data)

    def destroy(self, request, pk=None):
        report = self._get(pk)
        gone = {"id": str(report.pk), "key": report.key, "name": report.name}
        removed = _via_service(custom.delete_custom_report, report, by=request.user)
        return Response({**gone, "deleted": True, "email_schedules_removed": removed})

    @action(detail=True, methods=["get", "post"])
    def run(self, request, pk=None):
        report = self._get(pk)
        refusal = rep_perms.refusal(request.user, report.source)  # the panel's and the CSV's: View on what it lists
        if refusal:
            raise PermissionDenied(refusal)
        today = _today()
        data = run_any(report.key, today)  # what the screen, the CSV, and the email run
        return Response({"id": str(report.pk), "key": report.key, "name": report.name, "as_of": today, **sr.table(data)})


class ReportEmailViewSet(InFacility, ApiViewSet):
    """The signed-in user's own report emails, as the Schedule button sets them. PUT and POST both set one, on the list's URL."""

    module = rep_perms.MODULE
    write_level = rep_perms.VIEW_LEVEL  # the Schedule button's level: anyone who views Reports schedules their own
    FIELDS = ("report", "frequency")

    @classmethod
    def as_view(cls, actions=None, **initkwargs):
        if actions and actions.get("post") == "create":
            actions = {**actions, "put": "create"}  # setting a schedule is idempotent: PUT it, or POST it
        return super().as_view(actions, **initkwargs)

    def list(self, request):
        tenant = get_current_tenant()
        mine = ReportSubscription.objects.filter(user=request.user).select_related("user").order_by("report")
        return Response([sr.report_email(sub.report, sub, tenant) for sub in mine])

    def create(self, request):
        body = _object(request)
        unknown = set(body) - set(self.FIELDS)
        if unknown:
            raise DRFValidationError({"detail": f"Unknown fields: {', '.join(sorted(unknown))}. Only report and frequency are set here: "
                                                "a report is emailed to its own user."})
        report, frequency = body.get("report"), body.get("frequency")
        errors = {}
        if not isinstance(report, str) or not report:
            errors["report"] = ["Required: a report's key, from /api/v1/reports/ or /api/v1/custom-reports/."]
        if "frequency" not in body:
            errors["frequency"] = ['Required: "weekly", "monthly", or "" to turn it off.']
        elif frequency is not None and not isinstance(frequency, str):
            errors["frequency"] = ['Choose "weekly", "monthly", or "" to turn it off.']
        if errors:
            raise DRFValidationError(errors)
        sub = _via_service(subs.set_subscription, request.user, report, frequency)
        return Response(sr.report_email(report, sub, get_current_tenant()))


def register(router):
    router.register("overview", OverviewViewSet, basename="overview")
    router.register("reports", ReportViewSet, basename="report")
    router.register("custom-reports", CustomReportViewSet, basename="customreport")
    router.register("report-emails", ReportEmailViewSet, basename="reportemail")
