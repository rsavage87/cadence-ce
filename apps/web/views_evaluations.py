"""
Reports' Program evaluation (slice 32): the facility's annual evaluations of its medical equipment program, a new one for a period, the
evaluation's page (its figures cut to the reader, the text, the objectives, the approvals, its history), and the printable copy for the
committee. Views parse input, call apps.evaluations.services, and render; who may do what is apps/evaluations/permissions.py, checked
here on every request (web_view's level: View reads and prints; Edit drafts, refreshes, writes, sends to committee, takes back to draft,
and discards; Approve records, corrects, and removes approvals, and amends) and again by each service, whose refusals come back in
words. Scoped users are refused every view here (web_view's default).

The figures are the evaluation's current snapshot cut to the reader (apps.evaluations.indicators.cut), never the stored data as it is:
an indicator, the AEM table, or a binder section the reader's role cannot see is left out, the previous column and the "n of m met"
tally count only what is shown, and the page and the print name what was left out (the print is then stamped "Partial copy: not the
approved document"). A reader without Reports View sees the text, the objectives, and the approvals only.

The list (/reports/evaluations/): every evaluation, newest period first, with when the next is due (services.next_due) and New
evaluation. The page (/reports/evaluations/<number>/ opens the version services.current names; .../v<n>/ a given one) renders whole
when opened directly; each part is its own HTMX partial (PARTS, by the target's id), and every action names the version it acts on in
its address (.../v<n>/<action>/), so a page left open on one version never changes another. A modal's refusals render in the modal
with what was typed kept; a button's at the top of the part it swaps, and in the toast. New evaluation and Amend open the new
version's page.

The text (apps.evaluations.models.NARRATIVE, the inventory review day, the written plan reviewed) is saved one block at a time
(services.update_text, field by field), so two people writing different fields never overwrite each other. It is staff-written, about
the program as a whole: the form says so (PRIVACY_WARNING). It is never in a CSV or an email from here.

The print (print_base): the committee's copy, stamped Draft, "Sent to committee <day>", or "Approved <day> by <committee>", with the
figures, the text, the objectives, the approvals, and on a copy sent to committee the committee's signature lines.
"""
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_htmx.http import HttpResponseClientRedirect, reswap, retarget, trigger_client_event

from apps.accounts import people
from apps.evaluations import indicators, services
from apps.evaluations import permissions as perms
from apps.evaluations.models import (
    COMMENT_MAX,
    LIVE_STATUSES,
    NARRATIVE,
    NARRATIVE_FIELDS,
    OBJECTIVE_MAX,
    PRIVACY_WARNING,
    TEXT_MAX,
    Body,
    EvaluationApproval,
    EvaluationObjective,
    ObjectiveKind,
    ProgramEvaluation,
    Status,
)
from apps.pm.dates import add_months
from apps.reports import survey as sv

from . import history_tabs
from .decorators import web_view
from .forms_evaluations import ApprovalForm, NewEvaluationForm, ObjectiveForm, month_value, parse_month
from .htmx import toast

PAGE = "web/evaluations.html"
NEW = "web/_evaluation_new.html"
BODY = "web/_evaluation_body.html"
HEAD = "web/_evaluation_head.html"
FIGURES = "web/_evaluation_figures.html"
TEXT = "web/_evaluation_text.html"
FIELD = "web/_evaluation_field.html"
OBJECTIVES = "web/_evaluation_objectives.html"
OBJECTIVE = "web/_evaluation_objective.html"
APPROVALS = "web/_evaluation_approvals.html"
APPROVAL = "web/_evaluation_approval.html"
CONFIRM = "web/_evaluation_confirm.html"
PRINT = "web/print_evaluation.html"
# The page's parts, by the id an HTMX request targets: each renders on its own.
PARTS = {"ev-body": BODY, "ev-head": HEAD, "ev-figures": FIGURES, "ev-text": TEXT, "ev-objectives": OBJECTIVES, "ev-approvals": APPROVALS}
VERSION_MAX = 32767  # a PositiveSmallIntegerField: a larger version in an address is a 404, never a database error
STATUS_CSS = {Status.DRAFT: "neutral", Status.READY: "info", Status.APPROVED: "ok", Status.SUPERSEDED: "neutral", Status.DISCARDED: "neutral"}
MET_CSS = {True: "ok", False: "crit"}
PARTIAL_COPY = "Partial copy: not the approved document"


def _today() -> date:
    """The pages' clock, in one place so tests can pin it: the facility's today (its time zone is active in a request)."""
    return timezone.localdate()


# --- words ----------------------------------------------------------------------------------------------------------------------

def _day(d) -> str:
    return indicators.day_words(d) if d else ""


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def body_words(approval) -> str:
    """The committee an approval names: its name for another committee, else the choice's words."""
    if approval.body == Body.OTHER and approval.body_name:
        return approval.body_name
    return approval.get_body_display()


def _words(e) -> str:
    if isinstance(e, PermissionDenied):
        return str(e) or "You don't have access to do that."
    return " ".join(e.messages)


def stamp(ev: ProgramEvaluation, first=None, superseded_by=None) -> str:
    """The copy's stamp: "Draft", "Sent to committee <day>", "Approved <day> by <committee>", "... superseded by <label>",
    "Discarded"."""
    if ev.status == Status.DRAFT:
        return "Draft"
    if ev.status == Status.READY:
        return f"Sent to committee {_day(ev.sent_on)}"
    if ev.status == Status.DISCARDED:
        return "Discarded"
    approved = f"Approved {_day(ev.approved_on)}" + (f" by {body_words(first)}" if first is not None else "")
    if ev.status == Status.SUPERSEDED:
        return f"{approved}; superseded by {superseded_by.label}" if superseded_by is not None else f"{approved}; superseded by an amendment"
    return approved


def last_twelve_start(today: date) -> date:
    """The first month of the 12 months ending at the end of last month."""
    return add_months(today.replace(day=1), -12)


def default_start(today: date) -> date:
    """New evaluation's month: the day after the latest live evaluation (an approved one, or one drafted after it) ends, else the 12
    months ending at the end of last month."""
    if ProgramEvaluation.objects.filter(status__in=LIVE_STATUSES).exists():
        return services.suggested_start(today)
    return last_twelve_start(today)


def presets(today: date, chosen: date | None, default: date) -> list[dict]:
    """New evaluation's shortcuts: the default, the 12 months ending last month, and last calendar year (each once)."""
    last, calendar = last_twelve_start(today), date(today.year - 1, 1, 1)
    options = [(default, "After the latest evaluation")] if default not in (last, calendar) else []
    options.append((last, f"12 months ending {services.period_end_of(last):%b %Y}"))
    if calendar != last:  # in January the last 12 months are last calendar year: offered once
        options.append((calendar, f"Calendar year {today.year - 1}"))
    return [{"label": label, "start": month_value(start), "period": services.period_label(start, services.period_end_of(start)),
             "active": start == chosen} for start, label in options]


def _covers(start: date | None, today: date) -> dict:
    """The period a month starts, and whether it has ended yet."""
    if start is None or start.year >= 9999:
        return {}
    end = services.period_end_of(start)
    return {"words": services.period_label(start, end), "open": end >= today, "end": _day(end), "from": _day(end + timedelta(days=1))}


def area_refusal(user) -> str:
    """Why `user` may not take the figures (draft or refresh): the areas they cover that their role cannot see; "" when they may."""
    missing = indicators.missing_for(user)
    if not missing:
        return ""
    return (f"Taking the figures needs every area they cover: {indicators.needs_words(missing)}, which your role does not have. Ask "
            "someone who has them to take the figures.")


# --- lookups --------------------------------------------------------------------------------------------------------------------

def _get(number: str, version: int | None = None) -> ProgramEvaluation:
    """One evaluation by number (the version services.current names) or by number and version, in this facility: another's is a 404."""
    if version is not None and not 1 <= version <= VERSION_MAX:
        raise Http404
    if version is None:
        found = services.current(number)
        if found is None:
            raise Http404
        version = found.version
    ev = (ProgramEvaluation.objects.select_related("snapshot", "snapshot__taken_by", "created_by", "amends")
          .filter(number=number, version=version).first())
    if ev is None:
        raise Http404
    return ev


def _objective(ev, pk) -> EvaluationObjective:
    found = EvaluationObjective.objects.filter(evaluation=ev, pk=pk).first()
    if found is None:
        raise Http404
    return found


def _approval(ev, pk) -> EvaluationApproval:
    found = EvaluationApproval.objects.filter(evaluation=ev, pk=pk).first()
    if found is None:
        raise Http404
    return found


def url_of(ev, name: str = "evaluation_version", *extra) -> str:
    """An address for this version: its page, or one of its actions (`name`: "evaluation_send" ...)."""
    return reverse(f"web:{name}", args=[ev.number, ev.version, *extra])


def print_url(ev, tenant) -> str:
    return people.with_facility(reverse("web:evaluation_version_print", args=[ev.number, ev.version]), tenant)


# --- the list -------------------------------------------------------------------------------------------------------------------

def _next_due(today: date) -> dict | None:
    nd = services.next_due(today)
    if nd is None:
        return None
    start = nd.after.period_end + timedelta(days=1)
    return {"due_on": nd.due_on, "overdue": nd.overdue, "after": nd.after, "period": services.period_label(start, services.period_end_of(start)),
            "url": url_of(nd.after)}


def list_context(request, today: date) -> dict:
    """Every evaluation, newest period first (their approvals' committees with them: three queries whatever the count)."""
    approvals = Prefetch("approvals", queryset=EvaluationApproval.objects.order_by("meeting_on", "created_at"))
    rows = []
    for ev in ProgramEvaluation.objects.prefetch_related(approvals).order_by("-period_start", "-version"):
        rows.append({"ev": ev, "url": url_of(ev), "period": services.period_label(ev.period_start, ev.period_end), "status": ev.get_status_display(),
                     "css": STATUS_CSS.get(ev.status, "neutral"), "bodies": "; ".join(body_words(a) for a in ev.approvals.all()),
                     "quiet": ev.status in (Status.DISCARDED, Status.SUPERSEDED)})
    return {"nav_active": "reports", "today": today, "rows": rows, "next_due": _next_due(today), "can_new": perms.can_edit(request.user)}


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def evaluations(request):
    return render(request, PAGE, list_context(request, _today()))


# --- New evaluation -------------------------------------------------------------------------------------------------------------

def _new(request, form, today: date):
    """The New evaluation form: a modal over the list (HTMX), or the list page with the form on it (opened directly)."""
    default = default_start(today)
    typed = parse_month(form.data.get("start") if form.is_bound else form.initial.get("start"))
    ctx = {"form": form, "covers": _covers(typed, today), "presets": presets(today, typed, default), "refusal": area_refusal(request.user),
           "modal": bool(request.htmx)}
    if request.htmx:
        return render(request, NEW, ctx)
    return render(request, PAGE, {**list_context(request, today), "new": ctx})


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_new(request):
    """New evaluation (Edit, and View on every area the figures cover): GET the form (?start=YYYY-MM chooses the month), POST
    services.create_draft. Saved, the new draft's page opens."""
    today = _today()
    if request.method != "POST":
        start = parse_month(request.GET.get("start")) or default_start(today)
        return _new(request, NewEvaluationForm(initial={"start": month_value(start)}, today=today), today)
    form = NewEvaluationForm(request.POST, today=today)
    ev = None
    if form.is_valid():
        try:
            ev = services.create_draft(period_start=form.cleaned_data["start"], by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if ev is None:
        return _new(request, form, today)
    url = url_of(ev)
    return HttpResponseClientRedirect(url) if request.htmx else redirect(url)


# --- the page -------------------------------------------------------------------------------------------------------------------

def _detail(unit: str, value: dict | None) -> str:
    """"39 of 40" for a stored share or count out of something, as the snapshot words its own."""
    if not value or unit not in (indicators.PCT, indicators.COUNT):
        return ""
    num, den = value.get("num"), value.get("den")
    try:
        return f"{int(num):,} of {int(den):,}" if num is not None and den else ""
    except (TypeError, ValueError):
        return ""


def _figure_row(i: dict, previous: dict | None) -> dict:
    met = i.get("met")
    return {"key": i.get("key", ""), "label": i.get("label") or i.get("key", ""), "rule": i.get("rule", ""), "full_rule": i.get("rule", ""),
            "value": i.get("display", ""),
            "detail": i.get("detail", ""), "target": i.get("target_display", ""), "met": indicators.met_words(met), "met_css": MET_CSS.get(met, ""),
            "taken": i.get("basis") == indicators.TAKEN, "previous": (previous or {}).get("display", ""),
            "previous_detail": _detail(i.get("unit", ""), previous)}


def _groups(data: dict) -> list[dict]:
    """The reader's indicators by group, in the registry's group order (a group an old snapshot stored that no longer exists last)."""
    values = (data.get("previous") or {}).get("values") or {}
    shown = data.get("indicators") or []
    known = [key for key, _label in indicators.GROUPS]
    order = known + sorted({i.get("group", "") for i in shown} - set(known))
    out = []
    for key in order:
        rows = [_figure_row(i, values.get(i.get("key"))) for i in shown if i.get("group", "") == key]
        for before, row in zip(rows, rows[1:]):  # a counting rule said once for a run of indicators that share it
            if row["rule"] == before["full_rule"]:
                row["rule"] = ""
        if rows:
            out.append({"key": key, "label": indicators.GROUP_LABELS.get(key, key.title() or "Other figures"), "rows": rows})
    return out


def _quarters(data: dict) -> dict | None:
    """The PM figures by quarter, as a table: the quarters' labels, and a row per PM indicator the reader sees."""
    rows = [i for i in data.get("indicators") or [] if i.get("quarters")]
    if not rows:
        return None
    labels = [q.get("label", "") for q in rows[0]["quarters"]]
    return {"labels": labels, "rows": [{"label": i.get("label", ""), "cells": [{"value": q.get("display", ""), "detail": _detail(indicators.PCT, q)}
                                                                              for q in i["quarters"]]} for i in rows]}


def _previous(data: dict) -> dict | None:
    """The previous column's heading: its dates, and whether it is an approved evaluation's or counted from Cadence's records."""
    prev = data.get("previous") or {}
    if not prev:
        return None
    period = prev.get("period") or {}
    start, end = _iso(period.get("start")), _iso(period.get("end"))
    words = services.period_label(start, end) if start and end else ""
    if prev.get("source") == "approved":
        approved = _iso(prev.get("approved_on"))
        return {"title": prev.get("label") or prev.get("number", ""), "period": words, "approved": True,
                "how": f"approved {_day(approved)}" if approved else "approved"}
    shown_period = [i for i in data.get("indicators") or [] if i.get("basis") == indicators.PERIOD]
    how = f"counted {_day(_iso(prev.get('counted_on')))}, not approved"
    begin = _iso(prev.get("records_begin"))
    if begin and prev.get("values"):  # review fix: a column counted from part of those months says so
        how += f"; only from {_day(begin)}, when Cadence's records begin"
    return {"title": "The 12 months before", "period": words, "approved": False, "how": how,
            "empty": bool(shown_period) and not prev.get("values")}


def _aem(data: dict) -> list[dict] | None:
    """The AEM table's rows (None when the reader's cut left it out)."""
    if "aem" not in (data.get("tables") or {}):
        return None
    out = []
    for r in data["tables"]["aem"] or []:
        out.append({**r, "approved_on": _day(_iso(r.get("approved_on")))})
    return out


def _binder(data: dict) -> list[dict]:
    return [{"key": key, "title": sv.section_title(key) or key, "gap": c.get("gap", 0), "finding": c.get("finding", 0), "check": c.get("check", 0)}
            for key, c in (data.get("binder_gaps") or {}).items() if isinstance(c, dict)]


def _changed(data: dict) -> list[dict]:
    return [{"on": _day(_iso(c.get("on"))), "what": c.get("what", ""), "before": c.get("before", ""), "after": c.get("after", "")}
            for c in ((data.get("settings") or {}).get("changed_since") or [])]


def figures_context(ev: ProgramEvaluation, user) -> dict:
    """The figures as `user` may read them: always through indicators.cut."""
    snap = ev.snapshot
    data = indicators.cut(snap.data if snap is not None else {}, user)
    shown = perms.can_see_figures(user)
    begin = _iso(data.get("records_begin"))
    windows = (data.get("settings") or {}).get("pm_windows") or {}
    met, judged = indicators.tally(data.get("indicators") or [])
    return {
        "shown": shown, "taken": snap is not None, "taken_on": _day(snap.taken_on) if snap else "",
        "taken_by": str(snap.taken_by) if snap is not None and snap.taken_by_id else "",
        "groups": _groups(data), "quarters": _quarters(data), "aem": _aem(data), "binder": _binder(data),
        "not_recorded": list(data.get("not_recorded") or []), "left_out": list(data.get("left_out") or []),
        "previous": _previous(data) if shown else None, "met": met, "judged": judged,
        "provisional": _day(_iso(data.get("provisional_until"))), "records_begin": _day(begin) if begin and begin > ev.period_start else "",
        "changed": _changed(data), "as_of": _day(_iso((data.get("settings") or {}).get("as_of"))),
        "window_high": (windows.get("high") or {}).get("words", ""), "window_other": (windows.get("other") or {}).get("words", ""),
        "binder_url": f"{reverse('web:survey')}?from={ev.period_start.isoformat()}&to={ev.period_end.isoformat()}" if shown else "",
    }


# The text, block by block: a narrative field, the inventory review day, the written plan reviewed. Each block is saved on its own.
INVENTORY, PLAN = "inventory", "plan"
BLOCK_KEYS = (*NARRATIVE_FIELDS, INVENTORY, PLAN)
PLAN_INPUTS = (("plan_title", "Title", "text", services.PLAN_TITLE_MAX), ("plan_version", "Version", "text", services.PLAN_VERSION_MAX),
               ("plan_effective_on", "Effective", "date", None))


def _input_value(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return value or ""


def block(ev: ProgramEvaluation, key: str, *, typed: dict | None = None, errors: dict | None = None) -> dict:
    """One block of the text: its inputs (what was typed when a save was refused, else what is stored) and how it reads."""
    errors = errors or {}
    url = url_of(ev, "evaluation_text", key)

    def value(name):
        return typed[name] if typed is not None and name in typed else _input_value(getattr(ev, name))

    def inp(name, label, kind, limit):
        return {"name": name, "label": label, "type": kind, "max": limit, "value": value(name), "error": " ".join(errors.get(name, []))}

    if key in NARRATIVE_FIELDS:
        label, help_text = next((lab, h) for f, lab, h in NARRATIVE if f == key)
        return {"key": key, "url": url, "label": label, "help": help_text, "kind": "text", "text": getattr(ev, key),
                "inputs": [inp(key, label, "textarea", TEXT_MAX)], "error": " ".join(errors.get("", []))}
    if key == INVENTORY:
        return {"key": key, "url": url, "label": "Inventory reviewed", "help": "When the inventory was last reviewed for completeness.", "kind": "date",
                "text": _day(ev.inventory_reviewed_on), "inputs": [inp("inventory_reviewed_on", "Reviewed on", "date", None)],
                "error": " ".join(errors.get("", []))}
    plan = []
    if ev.plan_title:
        plan.append(ev.plan_title)
    if ev.plan_version:
        plan.append(f"version {ev.plan_version}")
    if ev.plan_effective_on:
        plan.append(f"effective {_day(ev.plan_effective_on)}")
    return {"key": key, "url": url, "label": "Written plan reviewed",
            "help": "The medical equipment management plan reviewed with this evaluation, as your policy system names it: the plan itself stays there.",
            "kind": "plan", "text": ", ".join(plan), "inputs": [inp(*spec) for spec in PLAN_INPUTS], "error": " ".join(errors.get("", []))}


def _input_names(key: str) -> list[str]:
    if key in NARRATIVE_FIELDS:
        return [key]
    if key == INVENTORY:
        return ["inventory_reviewed_on"]
    return [name for name, *_rest in PLAN_INPUTS]


def actions_for(user, ev: ProgramEvaluation, live_amendment) -> dict:
    """What `user` may do to `ev` as it stands (the services check again)."""
    edit, approve = perms.can_edit(user), perms.can_approve(user)
    s = ev.status
    return {"write": edit and s == Status.DRAFT, "refresh": edit and s == Status.DRAFT, "send": edit and s == Status.DRAFT,
            "back": edit and s == Status.READY, "discard": edit and s in (Status.DRAFT, Status.READY),
            "approve": approve and s in (Status.READY, Status.APPROVED), "amend": approve and s == Status.APPROVED and live_amendment is None,
            "correct": approve and s == Status.APPROVED}  # review fix: a superseded version's approvals stay as recorded


def page_context(request, ev: ProgramEvaluation, *, refusal: str = "", part_refusal: dict | None = None) -> dict:
    """Everything the page, its parts, and the print read."""
    user = request.user
    versions = list(ProgramEvaluation.objects.filter(number=ev.number).order_by("version"))
    live_amendment = next((v for v in versions if v.amends_id == ev.pk and v.status in (Status.DRAFT, Status.READY)), None)
    superseded_by = next((v for v in versions if v.amends_id == ev.pk and v.status == Status.APPROVED), None) if ev.status == Status.SUPERSEDED else None
    objectives = list(EvaluationObjective.objects.filter(evaluation=ev).select_related("carried_from__evaluation").order_by("position", "created_at"))
    approvals = list(EvaluationApproval.objects.filter(evaluation=ev).select_related("recorded_by").order_by("created_at", "id"))
    first = approvals[0] if approvals else None
    acts = actions_for(user, ev, live_amendment)
    url = url_of(ev)
    can_history = history_tabs.allowed(user, "evaluations")
    hist = history_tabs.context(request, ev, "evaluations", url, tab=False) if can_history and request.GET.get("history") else {}
    part_refusal = part_refusal or {}
    return {
        "nav_active": "reports", "ev": ev, "url": url, "today": _today(), "period": services.period_label(ev.period_start, ev.period_end),
        "status_css": STATUS_CSS.get(ev.status, "neutral"), "stamp": stamp(ev, first, superseded_by), "refusal": refusal,
        "versions": [{"ev": v, "url": url_of(v), "current": v.pk == ev.pk} for v in versions] if len(versions) > 1 else [],
        "live_amendment": live_amendment, "superseded_by": superseded_by,
        "fig": figures_context(ev, user), "refresh_refusal": area_refusal(user) if acts["refresh"] else "",
        "blocks": [block(ev, key) for key in BLOCK_KEYS], "privacy": PRIVACY_WARNING,
        "review": [o for o in objectives if o.kind == ObjectiveKind.REVIEW], "next": [o for o in objectives if o.kind == ObjectiveKind.NEXT],
        "approvals": [{"a": a, "body": body_words(a), "first": a.pk == first.pk} for a in sorted(approvals, key=lambda a: (a.meeting_on, a.created_at))],
        "first_approval": first, "acts": acts, "print_url": print_url(ev, request.tenant),
        "objectives_refusal": part_refusal.get("objectives", ""), "approvals_refusal": part_refusal.get("approvals", ""),
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist,
        "partial_copy": PARTIAL_COPY,
    }


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def evaluation(request, number, version=None):
    """The evaluation's page (the version services.current names, or the one in the address), or one of its parts (HTMX: PARTS), or
    its History (?history=1)."""
    ev = _get(number, version)
    if history_tabs.asked(request, tab=False):
        history_tabs.require(request.user, "evaluations")
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, ev, "evaluations", url_of(ev), tab=False)
    ctx = page_context(request, ev)
    if request.htmx and not request.htmx.history_restore_request and request.htmx.target in PARTS:
        return render(request, PARTS[request.htmx.target], ctx)
    return render(request, PAGE, ctx)


def _part(request, ev, template: str, target: str, message: str = "", *, refusal: str = "", part: str = ""):
    """A part of the page after an action: the part (its refusal at its top) swapped in place, the toast; opened without HTMX (a form
    posted with scripts off), the page itself (the refusal at its top), or the page again after a change."""
    ev = _get(ev.number, ev.version)
    if not request.htmx:
        if refusal:
            return render(request, PAGE, page_context(request, ev, refusal=refusal), status=400)
        return redirect(url_of(ev))
    ctx = page_context(request, ev, refusal=refusal if template in (BODY, HEAD) else "",
                       part_refusal={part: refusal} if part and refusal else None)
    response = reswap(retarget(render(request, template, ctx), target), "outerHTML")
    return toast(response, refusal or message) if (refusal or message) else response


def _saved(request, ev, message: str, template: str = BODY, target: str = "#ev-body"):
    """A modal saved: the part it changed in place, the toast, then the modal closed after settle (closing it first would detach the
    form that sent this request, which cancels the swap)."""
    response = _part(request, ev, template, target, message)
    return trigger_client_event(response, "modal-close", {}, after="settle") if request.htmx else response


def _confirm(request, ev, *, action: str, title: str, words: list[str], button: str, error: str = "", danger: bool = False,
             blocked: bool = False):
    """A confirming modal: what the action does in words, its refusal (or what keeps it from being done: `blocked`, no button)."""
    return render(request, CONFIRM, {"ev": ev, "action": url_of(ev, action), "title": title, "words": words, "button": button,
                                     "error": error, "danger": danger, "blocked": blocked})


# --- the figures ----------------------------------------------------------------------------------------------------------------

@require_POST
@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_refresh(request, number, version):
    """Refresh figures (Edit, a draft, View on every area the figures cover): services.refresh."""
    ev = _get(number, version)
    try:
        services.refresh(ev, by=request.user, today=_today())
    except (ValidationError, PermissionDenied) as e:
        return _part(request, ev, BODY, "#ev-body", refusal=_words(e))
    return _part(request, ev, BODY, "#ev-body", f"{ev.label}: figures taken again")


# --- the text -------------------------------------------------------------------------------------------------------------------

@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_text(request, number, version, key):
    """One block of the text (Edit): GET it to read (?edit=1 to write), POST it saved (services.update_text, field by field, all or
    none). Refused, the block stays open with what was typed and the words on the field they name."""
    ev = _get(number, version)
    if key not in BLOCK_KEYS:
        raise Http404
    writable = actions_for(request.user, ev, None)["write"]
    if request.method != "POST":
        if not request.htmx:
            return redirect(f"{url_of(ev)}#ev-f-{key}")
        editing = bool(request.GET.get("edit")) and writable
        return render(request, FIELD, {"ev": ev, "b": block(ev, key), "editing": editing, "url": url_of(ev, "evaluation_text", key),
                                       "privacy": PRIVACY_WARNING, "write": writable})
    typed = {name: request.POST.get(name, "") for name in _input_names(key)}
    errors: dict = {}
    today = _today()
    try:
        with transaction.atomic():
            for name, value in typed.items():
                services.update_text(ev, name, value, by=request.user, today=today)
    except ValidationError as e:
        errors = e.message_dict if hasattr(e, "error_dict") else {"": e.messages}
    except PermissionDenied as e:
        errors = {"": [_words(e)]}
    ev = _get(ev.number, ev.version)
    b = block(ev, key, typed=typed if errors else None, errors=errors)
    words = " ".join(m for ms in errors.values() for m in ms)
    if not request.htmx:  # a form posted with scripts off: the page again, or the page with the refusal at its top
        return render(request, PAGE, page_context(request, ev, refusal=words), status=400) if errors else redirect(f"{url_of(ev)}#ev-f-{key}")
    ctx = {"ev": ev, "b": b, "editing": bool(errors), "url": url_of(ev, "evaluation_text", key), "privacy": PRIVACY_WARNING,
           "write": actions_for(request.user, ev, None)["write"]}
    return toast(render(request, FIELD, ctx), words or f"{b['label']} saved")


# --- objectives -----------------------------------------------------------------------------------------------------------------

def _objective_modal(request, ev, form, *, objective=None):
    kind = form.kind
    action = url_of(ev, "evaluation_objective", objective.pk) if objective else f"{url_of(ev, 'evaluation_objective_new')}?kind={kind}"
    return render(request, OBJECTIVE, {"ev": ev, "form": form, "objective": objective, "action": action, "review": kind == ObjectiveKind.REVIEW,
                                       "title": ObjectiveKind(kind).label, "privacy": PRIVACY_WARNING, "text_max": OBJECTIVE_MAX,
                                       "comment_max": COMMENT_MAX})


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_objective_new(request, number, version):
    """Add an objective (Edit, a draft): ?kind=review (this period's: its result and comment too) or ?kind=next. GET the modal, POST
    services.add_objective (and update_objective for a result or comment, both or neither)."""
    ev = _get(number, version)
    kind = request.GET.get("kind", "")
    if kind not in ObjectiveKind.values:
        raise Http404
    if request.method != "POST":
        return _objective_modal(request, ev, ObjectiveForm(kind=kind))
    form = ObjectiveForm(request.POST, kind=kind)
    if form.is_valid():
        v = form.values()
        try:
            with transaction.atomic():
                obj = services.add_objective(ev, kind=kind, text=v["text"], target=v["target"], by=request.user)
                if v.get("result") or (v.get("comment") or "").strip():
                    services.update_objective(obj, result=v.get("result", ""), comment=v.get("comment", ""), by=request.user)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _objective_modal(request, ev, form)
    return _saved(request, ev, "Objective added", OBJECTIVES, "#ev-objectives")


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_objective(request, number, version, pk):
    """Change an objective (Edit, a draft): GET the modal, POST services.update_objective."""
    ev = _get(number, version)
    obj = _objective(ev, pk)
    if request.method != "POST":
        initial = {"text": obj.text, "target": obj.target, "result": obj.result, "comment": obj.comment}
        return _objective_modal(request, ev, ObjectiveForm(initial=initial, kind=obj.kind), objective=obj)
    form = ObjectiveForm(request.POST, kind=obj.kind)
    if form.is_valid():
        v = form.values()
        try:
            services.update_objective(obj, by=request.user, **v)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _objective_modal(request, ev, form, objective=obj)
    return _saved(request, ev, "Objective saved", OBJECTIVES, "#ev-objectives")


@require_POST
@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_objective_remove(request, number, version, pk):
    ev = _get(number, version)
    obj = _objective(ev, pk)
    try:
        services.remove_objective(obj, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _part(request, ev, OBJECTIVES, "#ev-objectives", refusal=_words(e), part="objectives")
    return _part(request, ev, OBJECTIVES, "#ev-objectives", "Objective removed")


@require_POST
@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_objective_move(request, number, version, pk):
    """Move an objective one place up (up=1) or down among its kind."""
    ev = _get(number, version)
    obj = _objective(ev, pk)
    try:
        services.move_objective(obj, up=request.POST.get("up") == "1", by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _part(request, ev, OBJECTIVES, "#ev-objectives", refusal=_words(e), part="objectives")
    return _part(request, ev, OBJECTIVES, "#ev-objectives")


# --- statuses -------------------------------------------------------------------------------------------------------------------

def _send_blocker(ev) -> str:
    """What keeps a draft from going to committee, said before the button is offered (the service refuses the same)."""
    if not ev.snapshot_id:
        return "Take the figures before sending it to committee."
    missing = EvaluationObjective.objects.filter(evaluation=ev, kind=ObjectiveKind.REVIEW, result="").count()
    if missing:
        return (f"Record a result for every objective of this period first: {missing} of them {'has' if missing == 1 else 'have'} "
                "none yet.")
    return ""


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_send(request, number, version):
    """Send to committee (Edit, a draft): GET the modal saying what it freezes (and what keeps it from going, with no button), POST
    services.send_to_committee."""
    ev = _get(number, version)
    words = ["The figures" + (f" (taken {_day(ev.snapshot.taken_on)})" if ev.snapshot_id else "") + ", the text, and the objectives are "
             "frozen while it is with the committee: the copy you print is the one it approves.",
             "Take it back to draft if the committee asks for changes, then send it again."]
    args = {"action": "evaluation_send", "title": f"Send {ev.label} to committee", "words": words, "button": "Send to committee"}
    if request.method != "POST":
        blocker = _send_blocker(ev) if ev.status == Status.DRAFT else ""
        return _confirm(request, ev, **args, error=blocker, blocked=bool(blocker))
    try:
        services.send_to_committee(ev, by=request.user, today=_today())
    except (ValidationError, PermissionDenied) as e:
        return _confirm(request, ev, **args, error=_words(e))
    return _saved(request, ev, f"{ev.label} sent to committee: print the committee's copy")


@require_POST
@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_back(request, number, version):
    """Back to draft (Edit, sent to committee): services.back_to_draft."""
    ev = _get(number, version)
    try:
        services.back_to_draft(ev, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _part(request, ev, BODY, "#ev-body", refusal=_words(e))
    return _part(request, ev, BODY, "#ev-body", f"{ev.label} is a draft again")


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def evaluation_discard(request, number, version):
    """Discard (Edit, a draft or one sent to committee): GET the modal, POST services.discard. Kept, never deleted."""
    ev = _get(number, version)
    args = {"action": "evaluation_discard", "title": f"Discard {ev.label}", "button": "Discard", "danger": True,
            "words": [f"{ev.label} is kept, marked discarded, and counted nowhere; its 12 months are free for another evaluation.",
                      "A discarded evaluation is not brought back: draft a new one instead."]}
    if request.method != "POST":
        return _confirm(request, ev, **args)
    try:
        services.discard(ev, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _confirm(request, ev, **args, error=_words(e))
    return _saved(request, ev, f"{ev.label} discarded")


@web_view(perms.MODULE, perms.APPROVE_LEVEL)
def evaluation_amend(request, number, version):
    """Amend (Approve, an approved evaluation): GET the modal, POST services.amend. Saved, the amendment's page opens."""
    ev = _get(number, version)
    nxt = (ProgramEvaluation.objects.filter(number=ev.number).order_by("-version").values_list("version", flat=True).first() or ev.version) + 1
    args = {"action": "evaluation_amend", "title": f"Amend {ev.label}", "button": "Start the amendment",
            "words": [f"A new draft, {ev.number} v{nxt}, starts from {ev.label}'s figures, text, and objectives; take the figures again there "
                      "if they should change.",
                      f"{ev.label} stays the approved version until the amendment is approved, then it is marked superseded."]}
    if request.method != "POST":
        return _confirm(request, ev, **args)
    try:
        new = services.amend(ev, by=request.user, today=_today())
    except (ValidationError, PermissionDenied) as e:
        return _confirm(request, ev, **args, error=_words(e))
    url = url_of(new)
    return HttpResponseClientRedirect(url) if request.htmx else redirect(url)


# --- approvals ------------------------------------------------------------------------------------------------------------------

def _approval_modal(request, ev, form, today: date, *, approval=None):
    first = approval is None and ev.status == Status.READY
    action = url_of(ev, "evaluation_approval", approval.pk) if approval else url_of(ev, "evaluation_approval_new")
    return render(request, APPROVAL, {"ev": ev, "form": form, "approval": approval, "action": action, "first": first,
                                      "sent_on": _day(ev.sent_on), "approved_on": _day(ev.approved_on), "today": today})


@web_view(perms.MODULE, perms.APPROVE_LEVEL)
def evaluation_approval_new(request, number, version):
    """Record approval (Approve): GET the modal, POST services.record_approval. The first, on one sent to committee, approves it."""
    ev = _get(number, version)
    today = _today()
    if request.method != "POST":
        return _approval_modal(request, ev, ApprovalForm(initial={"meeting_on": today.isoformat()}), today)
    form = ApprovalForm(request.POST)
    if form.is_valid():
        try:
            services.record_approval(ev, by=request.user, today=today, **form.values())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _approval_modal(request, ev, form, today)
    was_ready = ev.status == Status.READY
    return _saved(request, ev, f"{ev.label} approved" if was_ready else "Approval recorded")


@web_view(perms.MODULE, perms.APPROVE_LEVEL)
def evaluation_approval(request, number, version, pk):
    """Correct an approval's details (Approve): GET the modal, POST services.correct_approval."""
    ev = _get(number, version)
    approval = _approval(ev, pk)
    today = _today()
    if request.method != "POST":
        if ev.status == Status.SUPERSEDED:  # review fix: its approvals stay as recorded, so no form to correct them
            return _part(request, ev, APPROVALS, "#ev-approvals", part="approvals",
                         refusal=f"{ev.label} was superseded by its amendment: its approvals stay as recorded. Correct the amendment's instead.")
        initial = {f: getattr(approval, f) for f in services.APPROVAL_FIELDS}
        initial["meeting_on"] = approval.meeting_on.isoformat()
        return _approval_modal(request, ev, ApprovalForm(initial=initial), today, approval=approval)
    form = ApprovalForm(request.POST)
    if form.is_valid():
        try:
            services.correct_approval(approval, by=request.user, today=today, **form.values())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _approval_modal(request, ev, form, today, approval=approval)
    return _saved(request, ev, "Approval corrected")


@require_POST
@web_view(perms.MODULE, perms.APPROVE_LEVEL)
def evaluation_approval_remove(request, number, version, pk):
    """Remove a later approval entered in error (Approve): services.remove_approval (never the one that approved it)."""
    ev = _get(number, version)
    approval = _approval(ev, pk)
    try:
        services.remove_approval(approval, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _part(request, ev, APPROVALS, "#ev-approvals", refusal=_words(e), part="approvals")
    return _part(request, ev, BODY, "#ev-body", "Approval removed")


# --- the print ------------------------------------------------------------------------------------------------------------------

@web_view(perms.MODULE, perms.VIEW_LEVEL)
def evaluation_print(request, number, version=None):
    """The committee's copy (print_base): the figures cut to the reader (stamped a partial copy when anything was left out), the text,
    the objectives, the approvals, and on a copy sent to committee the committee's signature lines."""
    ev = _get(number, version)
    return render(request, PRINT, {**page_context(request, ev), "narrative": [block(ev, key) for key in NARRATIVE_FIELDS],
                                   "inventory": block(ev, INVENTORY), "plan": block(ev, PLAN)})
