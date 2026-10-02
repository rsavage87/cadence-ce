"""
Building custom reports on the Reports screen (slice 18): the mock's "+ Custom report" button. Views parse the builder's form, call
apps.reports.custom, and render; a saved report is then shown, downloaded, printed, and scheduled by views_reports like the eight.

The builder takes the Reports panel's place inside #rep-body (the list stays on the left), at /reports/custom/new/ and
/reports/custom-<id>/edit/; both render as a full page when opened directly. Its parts:
- the source select re-renders the source's columns and filters (#cr-fields, custom_fields) and clears the preview;
- Preview (custom_preview) runs the form as it stands, the first PREVIEW_ROWS rows, and saves nothing;
- Save creates or changes the report: #rep-body comes back on the saved report, its address pushed, with a toast; a refused save
  comes back as the builder with each problem next to its part and what was typed kept;
- Delete (on a custom report's panel, after the browser's confirm) removes it and its email schedules, and shows the first report.

Everything here needs Reports Edit (apps/reports/permissions.py BUILD_LEVEL, in web_view), checked on every request, GET and POST;
Reports View runs custom reports but never builds them. A builder only builds, previews, and changes reports on what they can see
(Work orders View for work orders, labor, and parts; Equipment View for devices); deleting one shows nothing, so it needs only Reports
Edit. Another facility's report is a 404. Scoped users are refused (web_view).
"""
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import push_url

from apps.reports import custom
from apps.reports import permissions as rep_perms
from apps.reports.models import CustomReport

from . import views_reports
from .decorators import web_view
from .htmx import is_partial, toast
from .reports_custom import blank, builder_fields, present_custom

FIELDS = "web/_custom_report_fields.html"
PREVIEW = "web/_custom_report_preview.html"


def _get(pk) -> CustomReport:
    return get_object_or_404(CustomReport.objects, pk=pk)  # tenant-scoped: another facility's report is a 404


def _sources(user) -> list[dict]:
    """The sources this user may build on, in the registry's order."""
    return [{"value": s.key, "label": s.label} for s in custom.SOURCES.values() if not rep_perms.build_refusal(user, s.key)]


def _typed(post) -> dict:
    """The builder's form as a definition plus its name, exactly as sent: the service checks it."""
    source = post.get("source", "")
    spec = custom.spec_of(source)
    filters = {}
    for f in spec.filters if spec else ():
        values = [v for v in post.getlist(f"f_{f.key}") if v != ""]
        if values:
            filters[f.key] = values
    if post.get("date_period"):
        filters["date"] = {"field": post.get("date_field", ""), "period": post.get("date_period", ""),
                           "from": post.get("date_from", ""), "to": post.get("date_to", "")}
    sort = post.get("sort", "")
    if sort and post.get("sort_dir") == "desc":
        sort = f"-{sort}"
    return {"name": post.get("name", ""), "source": source, "columns": post.getlist("col"), "filters": filters,
            "group_by": post.get("group_by", ""), "sort": sort}


def _definition(typed: dict) -> dict:
    return {k: typed[k] for k in ("source", "columns", "filters", "group_by", "sort")}


def _errors(e: ValidationError) -> dict:
    return {k: v[0] for k, v in e.message_dict.items()} if hasattr(e, "error_dict") else {"form": e.messages[0]}


def _builder(request, typed: dict, errors: dict | None = None, report: CustomReport | None = None, refusal: str = ""):
    """The builder in #rep-body, or the whole page when opened directly. `refusal` shows instead of the form."""
    errors = errors or {}
    partial = is_partial(request, "rep-body")
    title = f"Edit {report.name}" if report else "New custom report"
    action = reverse("web:custom_report_edit", args=[report.pk]) if report else reverse("web:custom_report_new")
    cancel = reverse("web:report", args=[report.key]) if report else reverse("web:reports")
    sources = _sources(request.user)
    offered = typed["source"] in {s["value"] for s in sources}  # a source they may not build on shows none of its parts
    builder = {"typed": typed, "errors": errors, "sources": sources, "fields": builder_fields(typed["source"], typed, errors) if offered else None,
               "report": report, "action": action, "cancel": cancel, "title": title, "refusal": refusal}
    ctx = {**views_reports.body_context(request, partial), "report": {"key": report.key if report else "", "title": title}, "builder": builder}
    return render(request, "web/_reports_body.html" if partial else "web/reports.html", ctx)


def _shown(request, report: CustomReport, message: str):
    """A saved report: its panel in #rep-body, its address pushed, a toast. A plain form post goes to its page instead."""
    if not request.htmx:
        return redirect("web:report", key=report.key)
    meta = custom.meta_of(report)
    ctx = {**views_reports.body_context(request, True), **views_reports.report_context(request, meta, views_reports._today())}
    response = render(request, "web/_reports_body.html", ctx)
    push_url(response, reverse("web:report", args=[report.key]))
    return toast(response, message)


@web_view(rep_perms.MODULE, rep_perms.BUILD_LEVEL)
def custom_new(request):
    if request.method != "POST":
        sources = _sources(request.user)
        source = request.GET.get("source", "")
        if source not in {s["value"] for s in sources}:
            source = sources[0]["value"] if sources else ""
        return _builder(request, blank(source))
    typed = _typed(request.POST)
    try:
        report = custom.create_custom_report(name=typed["name"], **_definition(typed), by=request.user)
    except ValidationError as e:
        return _builder(request, typed, _errors(e))
    return _shown(request, report, f"Saved {report.name}")


@web_view(rep_perms.MODULE, rep_perms.BUILD_LEVEL)
def custom_edit(request, pk):
    report = _get(pk)
    refusal = rep_perms.build_refusal(request.user, report.source)
    if refusal:  # its definition would preview what they cannot see; Delete stays on the panel
        return _builder(request, {**blank(""), "name": report.name}, report=report, refusal=refusal)
    if request.method != "POST":
        d = custom.runnable(report) or blank(report.source)
        return _builder(request, {**d, "name": report.name}, report=report)
    typed = _typed(request.POST)
    try:
        custom.update_custom_report(report, name=typed["name"], **_definition(typed), by=request.user)
    except ValidationError as e:
        return _builder(request, typed, _errors(e), report)
    return _shown(request, report, f"Saved {report.name}")


@require_POST
@web_view(rep_perms.MODULE, rep_perms.BUILD_LEVEL)
def custom_delete(request, pk):
    report = _get(pk)
    name = report.name
    removed = custom.delete_custom_report(report, by=request.user)
    message = f"Deleted {name}" + (f" and {removed} email schedule{'' if removed == 1 else 's'}" if removed else "")
    if not request.htmx:
        return redirect("web:reports")
    meta = views_reports._meta(None)
    ctx = {**views_reports.body_context(request, True), **views_reports.report_context(request, meta, views_reports._today())}
    response = render(request, "web/_reports_body.html", ctx)
    push_url(response, reverse("web:reports"))
    return toast(response, message)


@web_view(rep_perms.MODULE, rep_perms.BUILD_LEVEL)
def custom_fields(request):
    """The source's columns and filters (#cr-fields), when the builder's source changes. Opened directly: the builder on it."""
    source = request.GET.get("source", "")
    if not request.htmx:
        url = reverse("web:custom_report_new")
        return redirect(f"{url}?source={source}" if custom.spec_of(source) else url)
    allowed = {s["value"] for s in _sources(request.user)}
    if source not in allowed:
        return render(request, FIELDS, {"f": None, "errors": {}, "refusal": rep_perms.build_refusal(request.user, source)})
    typed = blank(source)
    return render(request, FIELDS, {"f": builder_fields(source, typed, {}), "errors": {}, "clear_preview": True})


@web_view(rep_perms.MODULE, rep_perms.BUILD_LEVEL)
def custom_preview(request):
    """Run the form as it stands, the first rows only; nothing is saved. Opened directly: the builder."""
    if request.method != "POST":
        return redirect("web:custom_report_new")
    typed = _typed(request.POST)
    refusal = rep_perms.build_refusal(request.user, typed["source"])
    if refusal:
        return render(request, PREVIEW, {"refusal": refusal})
    try:
        definition = custom.clean_definition(**_definition(typed))
    except ValidationError as e:
        return render(request, PREVIEW, {"problems": list(_errors(e).values())})
    data = custom.run(definition, views_reports._today(), limit=custom.PREVIEW_ROWS)
    return render(request, PREVIEW, {"r": data, "p": present_custom(data, rows=custom.PREVIEW_ROWS, preview=True)})
