"""
The survey binder (slice 25, apps.reports.survey) on the screen: Reports' "Survey binder" page for a period, a CSV per section table
and one of every gap, and the printable binder.

Reports View opens it (web_view, which refuses scoped users: the binder covers the whole facility); each section also needs View on
the areas it lists (apps.reports.permissions.survey_refusal). The page, its CSVs, the print, and the API ask the same question, so
none is the weaker door: a section the reader may not see is left out of the page and the print with why, its CSV is a 403 in the
same words, and the summary never reads as ready while one is left out.

The period is `from` and `to` in the address (apps.reports.survey.parse_period: the default twelve months when missing); the form
and the presets swap #survey-body and push the address, so a reload, Back, and every link on the page keep it. A period the binder
refuses shows the form's words (never a 500); its CSVs and print answer 400 in the same words.

Sections build their tables lazily (Table.rows): the screen reads each table's first PREVIEW_ROWS rows, the print up to PRINT_ROWS,
and the CSVs stream every row (apps.web.exports.csv_response). A table's link columns (Table.links) open the work order, device, or
(slice 28) incident drawer on the screen, for a reader who can view them; the print's and the gaps CSV's links name the facility
(people.with_facility), since record numbers repeat across facilities.
"""
from datetime import date, datetime
from decimal import Decimal
from itertools import islice
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils import timezone

from apps.accounts import people
from apps.accounts.models import Level, Module
from apps.reports import permissions as rep_perms
from apps.reports import survey as sv
from apps.reports.survey import DEVICE, GAPS_SHOWN, INCIDENT, KIND_HELP, KIND_LABELS, KINDS, PREVIEW_ROWS, PRINT_ROWS, WORK_ORDER

from .decorators import web_view
from .exports import csv_response
from .htmx import is_partial
from .views_reports import refused

# The record pages a gap or a table cell opens in the drawer over the binder (by URL name); any other page opens on its own.
DRAWERS = frozenset({"wo", "asset", "pm_model", "contract", "incident"})
KIND_CSS = {sv.GAP: "crit", sv.FINDING: "warn", sv.CHECK: "neutral"}
# A table's link column: the page its key opens, and the area whose View the reader needs for the link (slice 28: an incident's
# number links only for Incidents View; the binder refuses scoped users, so View is the whole rule here).
LINK_VIEWS = {WORK_ORDER: ("web:wo", Module.WORKORDERS), DEVICE: ("web:asset", Module.EQUIPMENT), INCIDENT: ("web:incident", Module.INCIDENTS)}
GAPS_COLUMNS = ["Section", "Kind", "Record", "Description", "Link"]


def _today() -> date:
    """The binder's clock, in one place so tests can pin it: the facility's today (its time zone is active in a request)."""
    return timezone.localdate()


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


# --- values and links -------------------------------------------------------------------------------------------------------

def display(value) -> str:
    """One value of a figure or a table cell as the screen and the print show it: dates in words, times in the facility's zone,
    floats to two decimals (as the CSV), None as a dash."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        at = timezone.localtime(value) if timezone.is_aware(value) else value
        return f"{_day(at)}, {at.strftime('%I:%M %p').lstrip('0')}"
    if isinstance(value, date):
        return _day(value)
    return str(value)


def opens_in_drawer(url: str) -> bool:
    """Whether a record's path is one of the drawers (a work order, device, model, or contract), so the screen opens it over the binder."""
    try:
        match = resolve(urlsplit(url).path)
    except Resolver404:
        return False
    return match.namespace == "web" and match.url_name in DRAWERS


def _record_path(kind: str, key) -> str:
    """The work order's, device's, or incident's page for a table cell holding its number or tag; "" when it names none."""
    name, _module = LINK_VIEWS[kind]
    if key in (None, ""):
        return ""
    try:
        return reverse(name, args=[str(key)])
    except NoReverseMatch:
        return ""


def linkable(user) -> dict:
    """Which kinds of record the reader may open from a table (their drawers need that area's View)."""
    return {kind: user.has_level(module, Level.VIEW) for kind, (_name, module) in LINK_VIEWS.items()}


def cells(row, links: dict, can: dict, tenant=None) -> list[dict]:
    """A row as the screen (tenant None: in-app paths, the drawer) or the print (tenant: paths naming the facility) shows it."""
    out = []
    for i, value in enumerate(row):
        kind = links.get(i)
        url = _record_path(kind, value) if kind and can.get(kind) else ""
        if url and tenant is not None:
            url = people.with_facility(url, tenant)
        num = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
        # Numbers, dates, and record keys stay on one line; text wraps.
        out.append({"text": display(value), "url": url, "num": num, "nowrap": bool(kind) or isinstance(value, date)})
    return out


def ordered_gaps(section) -> list:
    """A section's gaps, its gaps first, then findings, then checks (each kind in the section's order): the page, the print, the
    gaps CSV, and the API list them the same way."""
    return sorted(section.gaps, key=lambda g: KINDS.index(g.kind) if g.kind in KINDS else len(KINDS))


def gap_view(gap, link, tenant=None) -> dict:
    url = link(gap)  # no link the reader cannot open (sv.gap_links; review fix)
    return {"kind": gap.kind, "label": KIND_LABELS.get(gap.kind, gap.kind), "css": KIND_CSS.get(gap.kind, "neutral"), "help": KIND_HELP.get(gap.kind, ""),
            "text": gap.text, "record": gap.record, "url": people.with_facility(url, tenant) if url and tenant is not None else url,
            "drawer": bool(url) and tenant is None and opens_in_drawer(url)}


def _n_kind(n: int, kind: str) -> str:
    return f"{n} {KIND_LABELS[kind].lower()}{'' if n == 1 else 's'}"


def count_chips(counts: dict) -> list[tuple[str, str]]:
    """(chip class, "2 gaps") for each kind a section has, in kind order; none when it has nothing."""
    return [(KIND_CSS[kind], _n_kind(counts[kind], kind)) for kind in KINDS if counts.get(kind)]


def counts_words(counts: dict) -> str:
    """"2 gaps, 1 finding, no checks"; "No gaps" when there is nothing at all."""
    if not any(counts.values()):
        return "No gaps"
    return ", ".join(_n_kind(counts.get(kind, 0), kind) if counts.get(kind) else f"no {KIND_LABELS[kind].lower()}s" for kind in KINDS)


# --- the period ---------------------------------------------------------------------------------------------------------------

def _period(request, today: date):
    """(period, refusal messages): the period from the address, or None and the binder's words for why not."""
    try:
        return sv.parse_period(request.GET, today), []
    except ValidationError as e:
        return None, e.messages


def presets(today: date, period) -> list[dict]:
    """The period form's shortcuts: the default twelve months, this calendar year to date, and last calendar year."""
    default = sv.default_period(today)
    options = [("Last 12 months", default.start, default.end), ("This year to date", date(today.year, 1, 1), today),
               ("Last calendar year", date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))]
    return [{"label": label, "query": f"from={start.isoformat()}&to={end.isoformat()}",
             "active": period is not None and (period.start, period.end) == (start, end)} for label, start, end in options]


def _refused_period(messages) -> HttpResponseBadRequest:
    return HttpResponseBadRequest(" ".join(messages), content_type="text/plain; charset=utf-8")


# --- the page -----------------------------------------------------------------------------------------------------------------

def _csv_url(section_key: str, table_key: str, period, tenant) -> str:
    return people.with_facility(reverse("web:survey_csv", args=[section_key, table_key]) + "?" + period.query(), tenant)


def _preview(section, table, period, can, tenant) -> dict:
    rows = list(islice(table.rows(), PREVIEW_ROWS + 1))
    return {"key": table.key, "title": table.title, "columns": table.columns, "count": table.count, "empty": table.empty,
            "rows": [cells(r, table.links, can) for r in rows[:PREVIEW_ROWS]], "more": len(rows) > PREVIEW_ROWS,
            "csv": _csv_url(section.key, table.key, period, tenant)}


def cards(binder) -> list[dict]:
    """One summary card per section in the binder's order: its counts, or why it was left out."""
    shown = {s.key: s for s in binder.sections}
    left_out = {lo.key: lo for lo in binder.left_out}
    out = []
    for key in sv.section_keys():
        if key in shown:
            s = shown[key]
            counts = s.counts()
            out.append({"key": key, "title": s.title, "counts": counts, "chips": count_chips(counts), "left_out": ""})
        elif key in left_out:
            out.append({"key": key, "title": left_out[key].title, "counts": {}, "chips": [], "left_out": left_out[key].reason})
    return out


def incomplete_words(binder) -> str:
    """The line that keeps a binder with sections left out from reading as ready."""
    if binder.complete:
        return ""
    titles = ", ".join(lo.title for lo in binder.left_out)
    return (f"This binder is incomplete for your role: {titles} {'is' if len(binder.left_out) == 1 else 'are'} left out. "
            "Someone whose role can see every section should print the binder a surveyor gets.")


def _figures(section) -> list[dict]:
    return [{"label": f.label, "value": display(f.value), "hint": f.hint} for f in section.figures]


def _kind_help() -> list[tuple[str, str, str]]:
    return [(KIND_LABELS[k], KIND_CSS[k], KIND_HELP[k]) for k in KINDS]


def page_context(request, today: date) -> dict:
    period, errors = _period(request, today)
    tenant = request.tenant
    shown = period or sv.default_period(today)  # a refused period keeps what was typed in the form, the default for what was not

    def typed(key, fallback: date) -> str:
        raw = (request.GET.get(key) or "").strip() if errors else ""
        return raw or fallback.isoformat()

    ctx = {"nav_active": "reports", "today": today, "period": period, "errors": errors, "presets": presets(today, period),
           "from_value": typed("from", shown.start), "to_value": typed("to", shown.end), "kind_help": _kind_help()}
    if period is None:
        return ctx
    binder = sv.binder(request.user, period)
    can, link = linkable(request.user), sv.gap_links(request.user)
    sections = []
    for s in binder.sections:
        gaps = ordered_gaps(s)
        sections.append({"key": s.key, "title": s.title, "topic": s.topic, "covers": s.covers, "notes": s.notes, "figures": _figures(s),
                         "tables": [_preview(s, t, period, can, tenant) for t in s.tables],
                         "gaps": [gap_view(g, link) for g in gaps[:GAPS_SHOWN]], "more_gaps": max(0, len(gaps) - GAPS_SHOWN)})
    query = period.query()
    return {**ctx, "binder": binder, "counts": binder.counts(), "counts_words": counts_words(binder.counts()), "cards": cards(binder),
            "incomplete": incomplete_words(binder), "sections": sections, "any_gaps": any(s["gaps"] for s in sections),
            "print_url": people.with_facility(reverse("web:survey_print") + "?" + query, tenant),
            "gaps_csv_url": people.with_facility(reverse("web:survey_gaps_csv") + "?" + query, tenant), "preview_rows": PREVIEW_ROWS,
            "gaps_shown": GAPS_SHOWN}


@web_view(Module.REPORTS, Level.VIEW)
def survey(request):
    """The page, or #survey-body alone for the period form and its presets (HTMX)."""
    partial = is_partial(request, "survey-body")
    ctx = {**page_context(request, _today()), "partial": partial}
    return render(request, "web/_survey_body.html" if partial else "web/survey.html", ctx)


# --- CSVs ---------------------------------------------------------------------------------------------------------------------

def _filename(stem: str, period, today: date) -> str:
    return f"cadence-survey-{stem}-{period.start:%Y-%m-%d}-to-{period.end:%Y-%m-%d}-as-of-{today:%Y-%m-%d}.csv"


@web_view(Module.REPORTS, Level.VIEW)
def survey_csv(request, section, table):
    """One section table, every row, streamed. An unknown section or table is a 404; a section the reader may not see a 403 in
    survey_refusal's words."""
    if section not in sv.section_keys():
        raise Http404("No such section")
    refusal = rep_perms.survey_refusal(request.user, section)
    if refusal:
        return refused(refusal)
    today = _today()
    period, errors = _period(request, today)
    if period is None:
        return _refused_period(errors)
    built = sv.build_section(section, period, request.user)
    t = built.table(table)
    if t is None:
        raise Http404("No such table")
    return csv_response(_filename(f"{section}-{table}", period, today), t.columns, t.rows())


def gap_rows(binder, user, tenant):
    """Every gap of every section in the binder, with a link from APP_BASE_URL that names the facility (the file leaves the app), when
    the reader may open it."""
    link = sv.gap_links(user)
    for s in binder.sections:
        for g in ordered_gaps(s):
            url = link(g)
            yield [s.title, KIND_LABELS.get(g.kind, g.kind), g.record, g.text,
                   people.with_facility(settings.APP_BASE_URL + url, tenant) if url else ""]


@web_view(Module.REPORTS, Level.VIEW)
def survey_gaps_csv(request):
    today = _today()
    period, errors = _period(request, today)
    if period is None:
        return _refused_period(errors)
    binder = sv.binder(request.user, period)
    return csv_response(_filename("gaps", period, today), GAPS_COLUMNS, gap_rows(binder, request.user, request.tenant))


# --- the print ----------------------------------------------------------------------------------------------------------------

def _printed_table(table, section_key, period, can, tenant) -> dict:
    out = {"key": table.key, "title": table.title, "columns": table.columns, "empty": table.empty, "printed": table.printed,
           "csv": _csv_url(section_key, table.key, period, tenant), "rows": [], "more": 0, "count": table.count}
    if not table.printed:
        if out["count"] is None:
            out["count"] = sum(1 for _ in table.rows())
        return out
    it = iter(table.rows())
    rows = list(islice(it, PRINT_ROWS))
    more = sum(1 for _ in it)
    out.update(rows=[cells(r, table.links, can, tenant) for r in rows], more=more, count=len(rows) + more)
    return out


@web_view(Module.REPORTS, Level.VIEW)
def survey_print(request):
    today = _today()
    period, errors = _period(request, today)
    if period is None:
        return _refused_period(errors)
    tenant = request.tenant
    binder = sv.binder(request.user, period)
    can, link = linkable(request.user), sv.gap_links(request.user)
    sections = [{"key": s.key, "title": s.title, "topic": s.topic, "covers": s.covers, "notes": s.notes, "counts": counts_words(s.counts()),
                 "figures": _figures(s),
                 "gaps": [gap_view(g, link, tenant) for g in ordered_gaps(s)],
                 "tables": [_printed_table(t, s.key, period, can, tenant) for t in s.tables]} for s in binder.sections]
    ctx = {"today": today, "period": period, "binder": binder, "sections": sections, "counts": binder.counts(),
           "counts_words": counts_words(binder.counts()), "incomplete": incomplete_words(binder), "not_covered": sv.NOT_COVERED,
           "print_rows": PRINT_ROWS, "kind_help": _kind_help()}
    return render(request, "web/print_survey.html", ctx)
