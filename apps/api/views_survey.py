"""
The survey binder (slice 25, apps.reports.survey) over the API, with the Reports screen's doors (apps.reports.permissions: Reports
View, and each section's own View through survey_refusal, in the same words as the screen, its CSVs, and the print). Scoped users are
refused, as on the screen (no scoped_actions: the binder covers the whole facility).

GET /api/v1/survey/?from=&to=            The binder for the period (the default twelve months when missing): period {from, to, today,
                                         label}, complete (no section left out), counts {gap, finding, check}, sections [{key, title,
                                         topic, covers, figures [{label, value, hint}], gaps [{kind, text, record, url}], tables [{key,
                                         title, columns, count}], notes}], and left_out [{key, title, reason}]. Gaps are listed gaps
                                         first, then findings, then checks, as the screen lists them; a gap's url is the record's path
                                         naming the facility (?facility=), since record numbers repeat across facilities.
GET /api/v1/survey/<section>/?from=&to=  One section, its tables with their rows: up to MAX_ROWS each, with "truncated": true beyond
                                         (the screen's CSV has every row). A section the user may not see is a 403 in survey_refusal's
                                         words; an unknown one a 404.
A period the binder refuses is a 400 keyed by the parameter ("from" or "to"), in its words. Values: dates as ISO, times as ISO in the
facility's zone, floats to two decimals and Decimals as text, as the CSV writes them.
"""
from datetime import date, datetime
from decimal import Decimal
from itertools import islice

from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from apps.accounts import people
from apps.reports import permissions as rep_perms
from apps.reports import survey as sv
from apps.tenants.context import get_current_tenant

from .base import ApiViewSet, _via_service

MAX_ROWS = 10_000  # a table's rows in one response; the screen's CSV streams every row


def json_value(value):
    """A figure's or a cell's value as JSON: what the CSV writes, kept a number where it is one."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return (timezone.localtime(value) if timezone.is_aware(value) else value).isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def ordered_gaps(section) -> list:
    """Gaps, then findings, then checks, each in the section's order (as the screen and its gaps CSV list them)."""
    return sorted(section.gaps, key=lambda g: sv.KINDS.index(g.kind) if g.kind in sv.KINDS else len(sv.KINDS))


def _gap(gap, link, tenant) -> dict:
    url = link(gap)  # no link the reader cannot open (sv.gap_links; review fix)
    return {"kind": gap.kind, "text": gap.text, "record": gap.record, "url": people.with_facility(url, tenant) if url else ""}


def _table(table, with_rows: bool) -> dict:
    out = {"key": table.key, "title": table.title, "columns": list(table.columns), "count": table.count}
    if with_rows:
        it = iter(table.rows())
        rows = [[json_value(v) for v in row] for row in islice(it, MAX_ROWS)]
        truncated = next(it, None) is not None
        if out["count"] is None and not truncated:
            out["count"] = len(rows)
        out.update(rows=rows, truncated=truncated)
    return out


def section_data(section, link, tenant, with_rows: bool = False) -> dict:
    return {"key": section.key, "title": section.title, "topic": section.topic, "covers": section.covers,
            "figures": [{"label": f.label, "value": json_value(f.value), "hint": f.hint} for f in section.figures],
            "gaps": [_gap(g, link, tenant) for g in ordered_gaps(section)], "tables": [_table(t, with_rows) for t in section.tables],
            "notes": list(section.notes)}


def period_data(period) -> dict:
    return {"from": period.start.isoformat(), "to": period.end.isoformat(), "today": period.today.isoformat(), "label": period.label}


class SurveyViewSet(ApiViewSet):
    module = rep_perms.MODULE
    lookup_value_regex = "[a-z0-9_-]+"

    def _period(self, request):
        """The period from ?from=&to=, the facility's today read once; a refusal is a 400 keyed by the parameter."""
        return _via_service(sv.parse_period, request.query_params, timezone.localdate())

    def list(self, request):
        period = self._period(request)
        binder = sv.binder(request.user, period)
        tenant, link = get_current_tenant(), sv.gap_links(request.user)
        return Response({"period": period_data(period), "complete": binder.complete, "counts": binder.counts(),
                         "sections": [section_data(s, link, tenant) for s in binder.sections],
                         "left_out": [{"key": lo.key, "title": lo.title, "reason": lo.reason} for lo in binder.left_out]})

    def retrieve(self, request, pk=None):
        if pk not in sv.section_keys():
            raise NotFound("No such section.")
        refusal = rep_perms.survey_refusal(request.user, pk)
        if refusal:
            raise PermissionDenied(refusal)
        period = self._period(request)
        section = sv.build_section(pk, period, request.user)
        return Response({"period": period_data(period), **section_data(section, sv.gap_links(request.user), get_current_tenant(), with_rows=True)})


def register(router):
    router.register("survey", SurveyViewSet, basename="survey")
