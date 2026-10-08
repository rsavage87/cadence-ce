"""
Incidents (slice 28, beyond the mock): the list and its CSV, Record incident, and the incident drawer with its actions. Views parse
input, call apps.incidents.services, and render; who may do what is apps/incidents/permissions.py, checked here on every request
(web_view's level) and again by each service (an incident is never a side door around Equipment or Work orders: holding needs
Equipment Edit, opening a work order Work orders Edit, returning a device to use Equipment Edit, and the services say so in words).
Scoped users (a vendor's company, a requester's unit) are refused every view here (web_view's default): incidents are never theirs.

The list follows the Contracts screen: a page with filters (status: open and closed by default, open, closed, recorded in error, all;
a year) whose body re-fetches itself on `incidents-changed`, and a drawer at /incidents/<number>/ (a partial into #drawer, or the
page with the drawer open when opened directly). Open incidents come first, by their report's due date; the cards' rail is the
clock's colour (overdue red, due within SOON_WORK_DAYS work days amber, running blue, reported or closed green).

A year filter keeps the incidents that happened that year and those whose report went out that year (the report number carries
it: 803.3(x)), so the CSV for a year is CE's share of the facility's annual report (Form FDA 3419): its file is named so, and the list
says it.

Record incident (a modal): the device by tag (the device drawer passes it; the Incidents page searches for it), the facts, the hold,
and the investigation: the device's open repair taken as it (the request the event was reported as: the work order drawer passes it,
and the device drawer's offer is its one open repair) or a new one. The report's due date shows before saving (incident_due).

The drawer: the clock in words, the facts (Edit facts), the decision (Decide: the 803.3 question), the reports, the device
evaluation's finding, the holds (each device: sent to / back from the manufacturer, Release), the investigation work order (Open
investigation), Close / Reopen / Recorded in error, and the History section (the incident's changes with its holds'). Each change
is its own view; a modal's refusals render in the modal, a drawer button's in the drawer and its toast. Each fires
`incidents-changed` (the list), `devices-changed` when a device's hold changed, `wo-changed` when a work order was opened or
cancelled.

Privacy (non-negotiable 6): nothing on an incident is typed text; the outcome and who was affected show only here (Incidents View).
"""
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Q, Value, When
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment import scan
from apps.equipment.models import Asset
from apps.equipment.services import search_assets
from apps.incidents import permissions as perms
from apps.incidents import services as inc
from apps.incidents.models import (
    CLOCK_OUTCOMES,
    FACILITY_PATIENTS,
    HARM_OUTCOMES,
    SHORT_OUTCOMES,
    Affected,
    Incident,
    IncidentHold,
    Outcome,
    Release,
    Status,
)
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoStatus, WoType

from . import history_tabs
from .decorators import web_view
from .exports import csv_response
from .forms_incidents import (
    NEW,
    NONE,
    RELEASE_WORDS,
    STATUS_FILTERS,
    DayForm,
    DecideForm,
    FactsForm,
    FindingForm,
    RecordForm,
    ReleaseForm,
    ReportsForm,
    parse_incident_filters,
)
from .htmx import PAGE_SIZE, is_partial, toast

PAGE = "web/incidents.html"
BODY = "web/_incidents_body.html"
DRAWER = "web/_incident_drawer.html"
RECORD = "web/_incident_new.html"
DUE = "web/_incident_due.html"
PICKS = "web/_incident_picks.html"
FACTS = "web/_incident_facts.html"
DECIDE = "web/_incident_decide.html"
REPORTS = "web/_incident_reports.html"
HOLD = "web/_incident_hold.html"
CUSTODY = "web/_incident_custody.html"
RELEASE = "web/_incident_release.html"
CLOSE = "web/_incident_close.html"
IN_ERROR = "web/_incident_in_error.html"
CHANGED = "incidents-changed"  # the list (and its page head) re-fetch on it
FORM_3419 = "CE's reports for the facility's annual report (Form FDA 3419)"

RECIPIENTS = {("fda", "manufacturer"): "the FDA and the manufacturer",
              ("manufacturer",): "the manufacturer (the FDA when the manufacturer cannot be identified)"}


# --- words ---------------------------------------------------------------------------------------------------------------------

def _md(d: date) -> str:
    return f"{d:%b} {d.day}"


def _full(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _due_day(d: date, today: date) -> str:
    """A due date as the clock says it: "Fri Oct 23" (with the year when it is not this year's)."""
    return f"{d:%a} {d:%b} {d.day}" + (f", {d.year}" if d.year != today.year else "")


def _and(words: list[str]) -> str:
    if len(words) < 2:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _left(n: int) -> str:
    if n <= 0:
        return "due today"
    return f"{n} work day{'' if n == 1 else 's'} left"


@dataclass(frozen=True)
class ClockWords:
    text: str
    tone: str = ""  # the note's colour: crit (overdue), warn (soon), info (running), ok (reported in time), "" otherwise
    chip: tuple | None = None  # (css, label) for the list's card and the drawer's head
    rail: str = ""  # the list card's rail


def clock_words(i: Incident, today: date) -> ClockWords:
    """Where the incident's 10-work-day clock stands, in words (apps.incidents.services.clock): the drawer's note, the list's chip
    and rail. An undecided clock counts as reportable (if the device cannot be ruled out, it is)."""
    if i.status == Status.IN_ERROR:
        return ClockWords("Recorded in error: kept, and counted nowhere.", chip=("neutral", "Recorded in error"))
    done_rail = "ok" if i.status == Status.CLOSED else ""
    due = i.report_due_on
    if due is None:
        if i.reportable is False:
            by = f" ({i.get_decided_by_display().lower()})" if i.decided_by else ""
            when = f" on {_full(i.decided_on)}" if i.decided_on else ""
            return ClockWords(f"Decided not reportable{when}{by}: {i.get_basis_display()}.", chip=("neutral", "Not reportable"), rail=done_rail)
        if i.affected == Affected.NONE:
            why = "no one was affected."
        elif i.affected not in FACILITY_PATIENTS:
            why = "the person affected was not a patient of the facility or a staff member on duty (21 CFR 803.3)."
        else:
            why = "not a death or a serious injury."
        if inc.needs_decision(i) and i.status == Status.OPEN:
            return ClockWords(f"No report clock: {why} Someone was harmed, so decide whether it was reportable.", chip=("neutral", "Decision pending"))
        return ClockWords(f"No report clock: {why}", rail=done_rail)
    c = inc.clock(i, today)
    after = f"10 work days after {_md(i.aware_on)}"
    if c.pending:
        tone = "crit" if c.overdue else "warn" if c.soon else "info"
        if i.reportable is None:
            to = RECIPIENTS.get(c.recipients, "the manufacturer")
            if c.overdue:
                text = (f"The report to {to} was due no later than {_due_day(due, today)} ({after}). Not decided yet: if the device cannot "
                        "be ruled out, report it.")
            else:
                text = f"Report to {to} as soon as practicable, no later than {_due_day(due, today)} ({after}): {_left(c.left)}."
                if i.outcome == Outcome.UNKNOWN:
                    text += " Until it is decided, an outcome not known yet counts as serious."
        else:
            to = _and([inc.RECIPIENT_WORDS[r] for r in inc.reports_missing(i)])
            if c.overdue:
                text = f"Decided reportable. The report to {to} was due {_due_day(due, today)} ({after}): record it if it was sent."
            else:
                text = f"Decided reportable: report to {to} no later than {_due_day(due, today)} ({after}): {_left(c.left)}."
        label = "Report overdue" if c.overdue else f"Report due {_md(due)}"
        return ClockWords(text, tone, (tone, label), tone)
    if i.reportable:
        sent = inc.reports_done_by(i)
        number = f" (report {i.report_number})" if i.report_number else ""
        if sent is not None and sent > due:
            return ClockWords(f"Reported on {_full(sent)}{number}, after the due date ({_due_day(due, today)}).", "warn", ("warn", "Reported late"),
                              done_rail)
        when = f" on {_full(sent)}" if sent else ""
        return ClockWords(f"Reported{when}{number}, within the 10 work days (due {_due_day(due, today)}).", "ok", ("ok", "Reported"), done_rail)
    return ClockWords(f"Report due {_due_day(due, today)} ({after}).", rail=done_rail)


def due_preview(*, occurred, aware, outcome: str, affected: str, today: date) -> dict:
    """Record incident's due date before saving, from what was typed so far: {"text", "tone"}."""
    if occurred is None:
        return {"text": "Enter the day it happened to see when a report would be due.", "tone": ""}
    aware = aware or occurred
    if aware < occurred:
        return {"text": "Clinical staff cannot have known before it happened.", "tone": "warn"}
    if not affected:
        return {"text": "Choose who was affected to see whether a report is due.", "tone": ""}
    if affected == Affected.NONE:
        if outcome in HARM_OUTCOMES:
            return {"text": "Someone was harmed, or may have been: choose who. If no one was, the outcome is No harm.", "tone": "warn"}
        return {"text": "No report is due: no one was affected.", "tone": ""}
    if affected not in FACILITY_PATIENTS:
        return {"text": "No report clock: a visitor or another person is not a patient of the facility (21 CFR 803.3). Whether it was "
                        "reportable is still decided.", "tone": ""}
    if outcome not in CLOCK_OUTCOMES:
        return {"text": "No report clock: not a death or a serious injury.", "tone": ""}
    due = inc.due_date(outcome=outcome, affected=affected, aware_on=aware, reportable=None)
    to = RECIPIENTS[("fda", "manufacturer")] if outcome == Outcome.DEATH else RECIPIENTS[("manufacturer",)]
    text = f"Report to {to} no later than {_due_day(due, today)} (10 work days after {_md(aware)})."
    if outcome == Outcome.UNKNOWN:
        text += " Not known yet counts as serious until it is decided."
    return {"text": text, "tone": "warn"}


def _parse_day(value):
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


# --- lookups -------------------------------------------------------------------------------------------------------------------

def _get(number) -> Incident:
    """One incident by number, in this facility: another's is a 404."""
    found = (Incident.objects.select_related("asset", "asset__device_model", "asset__department", "work_order", "recorded_by", "closed_by",
                                             "created_by").filter(number=number).first())
    if found is None:
        raise Http404
    return found


def _get_hold(incident, pk) -> IncidentHold:
    return get_object_or_404(IncidentHold.objects.select_related("asset", "asset__device_model"), pk=pk, incident=incident)


def _asset(tag):
    if not tag:
        return None
    return Asset.objects.select_related("device_model", "department").filter(tag=tag).first()


def adoptable(asset):
    """The device's open repairs that may become an incident's investigation: not already the investigation of an open incident
    (the service's rule, apps.incidents.services._adopt_refusal, says why for any other)."""
    if asset is None:
        return []
    return list(WorkOrder.objects.filter(asset=asset, type=WoType.REPAIR, status__in=OPEN_STATUSES).exclude(incidents__status=Status.OPEN)
                .select_related("assigned_to").order_by("opened_on", "number"))


# --- the list ------------------------------------------------------------------------------------------------------------------

def _years() -> list[int]:
    """The years with an incident (the year it happened) or a report sent (its number's year), newest first."""
    years = {d.year for d in Incident.objects.dates("occurred_on", "year")}
    years |= {int(n[11:15]) for n in Incident.objects.exclude(report_number="").values_list("report_number", flat=True) if n[11:15].isdigit()}
    return sorted(years, reverse=True)


def filter_incidents(f):
    """The list's incidents for filters `f`, open first by their report's due date (then the rest), newest first."""
    qs = Incident.objects.select_related("asset", "asset__device_model", "asset__department")
    if f.status == "":
        qs = qs.exclude(status=Status.IN_ERROR)
    elif f.status != "all":
        qs = qs.filter(status=f.status)
    if f.year:
        qs = qs.filter(Q(occurred_on__year=f.year) | Q(report_number__contains=f"-{f.year}-"))
    held = IncidentHold.objects.filter(incident=OuterRef("pk"), released_on__isnull=True)
    rank = Case(When(status=Status.OPEN, then=Value(0)), When(status=Status.CLOSED, then=Value(1)), default=Value(2), output_field=IntegerField())
    due = Case(When(status=Status.OPEN, then=F("report_due_on")), default=None)
    return (qs.annotate(held_now=Exists(held), rank=rank, due_sort=due)
            .order_by("rank", F("due_sort").asc(nulls_last=True), "-occurred_on", "-number"))


def _list_context(request) -> dict:
    today = timezone.localdate()
    years = _years()
    f = parse_incident_filters(request.GET, years)
    page = Paginator(filter_incidents(f), PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"i": i, "clock": clock_words(i, today), "outcome": SHORT_OUTCOMES.get(i.outcome, i.outcome)} for i in page.object_list]
    return {"nav_active": "incidents", "list_url": reverse("web:incidents"), "f": f, "page": page, "rows": rows, "years": years,
            "statuses": STATUS_FILTERS, "form_3419": FORM_3419, "summary": _summary(), "can_record": perms.can_record(request.user)}


def _summary() -> dict:
    return {"open": Incident.objects.filter(status=Status.OPEN).count(), "action": inc.needing_action().count(),
            "held": Asset.objects.filter(incident_hold=True).count()}


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def incidents(request):
    ctx = _list_context(request)
    if is_partial(request, "inc-body"):  # the page head's line comes along, so a change in the drawer shows there too
        return render(request, BODY, {**ctx, "oob_summary": True})
    return render(request, PAGE, ctx)


CSV_COLUMNS = ["Incident", "Device", "Model", "Occurred", "Clinical staff first knew", "Outcome", "Affected", "Decision", "Basis", "Decided on",
               "Report due", "Sent to the FDA", "Sent to the manufacturer", "Report number", "Finding", "Status"]


def _decision(i) -> str:
    return "" if i.reportable is None else "Reportable" if i.reportable else "Not reportable"


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def incidents_csv(request):
    """The list's incidents (its filters, every page) as CSV. Filtered to a year it is CE's reports for the facility's annual report
    (Form FDA 3419), and named so."""
    today = timezone.localdate()
    f = parse_incident_filters(request.GET, _years())
    rows = ((i.number, i.asset.tag, f"{i.asset.device_model.manufacturer} {i.asset.device_model.model}", i.occurred_on, i.aware_on,
             SHORT_OUTCOMES.get(i.outcome, i.outcome), i.get_affected_display(), _decision(i), i.get_basis_display(), i.decided_on,
             i.report_due_on, i.fda_reported_on, i.manufacturer_reported_on, i.report_number, i.get_finding_display(), i.get_status_display())
            for i in filter_incidents(f).iterator(chunk_size=500))
    name = f"cadence-incidents-{f.year}-form-3419-{today:%Y-%m-%d}.csv" if f.year else f"cadence-incidents-{today:%Y-%m-%d}.csv"
    return csv_response(name, CSV_COLUMNS, rows)


# --- the drawer ----------------------------------------------------------------------------------------------------------------

def _hold_rows(holds, *, may_act: bool) -> list[dict]:
    out = []
    for h in holds:
        acts = h.active and may_act
        out.append({"h": h, "custody": ("back" if h.with_manufacturer else "sent") if acts else "", "release": acts,
                    "how": Release(h.release).label if h.release else ""})
    return out


def _drawer_context(request, i: Incident, refusal: str = "") -> dict:
    user = request.user
    today = timezone.localdate()
    is_open = i.status == Status.OPEN
    can_record, can_decide = perms.can_record(user) and is_open, perms.can_decide(user) and is_open
    holds = list(i.holds.select_related("asset", "asset__device_model", "asset__department", "released_by").order_by("held_on", "created_at"))
    wo = i.work_order
    can_history = history_tabs.allowed(user, "incidents")
    url = reverse("web:incident", args=[i.number])
    hist = history_tabs.context(request, i, "incidents", url, tab=False) if can_history and request.GET.get("history") else {}
    required = inc.required_recipients(i)
    return {
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist,
        "i": i, "dm": i.asset.device_model, "today": today, "clock": clock_words(i, today), "refusal": refusal, "is_open": is_open,
        "holds": _hold_rows(holds, may_act=can_decide), "held_now": any(h.active for h in holds),
        "wo": wo, "investigation_live": wo is not None and wo.status != WoStatus.CANCELLED,
        "required": _and([inc.RECIPIENT_WORDS[r] for r in required]) if required else "",
        "missing": _and([inc.RECIPIENT_WORDS[r] for r in inc.reports_missing(i)]) if required else "",
        "needs_decision": inc.needs_decision(i), "finding_form": FindingForm(initial={"finding": i.finding}, auto_id="incg-%s") if can_record else None,
        "can_record": can_record, "can_decide": can_decide, "can_hold": perms.can_hold(user) and is_open,
        "can_open_wo": perms.can_open_work_order(user) and is_open, "can_reopen": perms.can_decide(user) and i.status == Status.CLOSED,
        "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": user.has_level(Module.WORKORDERS, Level.VIEW),
    }


def _drawer(request, i, refusal: str = ""):
    return render(request, DRAWER, _drawer_context(request, _get(i.number), refusal))


def _changed(response, *also):
    for event in (CHANGED, *also):
        trigger_client_event(response, event, {})
    return response


def _saved(request, i, message: str, *also):
    """A modal saved: the incident's drawer into #drawer, the list refreshed (and whatever `also` names), the toast, then the modal
    closed after settle (closing it first would detach the form that sent this request, which cancels the swap and the events)."""
    response = retarget(_drawer(request, i), "#drawer")
    toast(_changed(response, *also), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _refused(request, i, e) -> HttpResponse:
    """A drawer button's refusal: the drawer with the words at its top, and the toast."""
    message = _words(e)
    return toast(_drawer(request, i, message), message)


def _words(e) -> str:
    if isinstance(e, PermissionDenied):
        return str(e) or "You don't have access to do that."
    return " ".join(e.messages)


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def incident(request, number):
    i = _get(number)
    if history_tabs.asked(request, tab=False):  # the History section or its Show older (Incidents View; scoped users never get here)
        history_tabs.require(request.user, "incidents")
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, i, "incidents", reverse("web:incident", args=[i.number]), tab=False)
    ctx = _drawer_context(request, i)
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, DRAWER, ctx)
    return render(request, PAGE, {**_list_context(request), **ctx, "drawer_template": DRAWER})


# --- Record incident -----------------------------------------------------------------------------------------------------------

def _record_initial(params, asset, fixed, choices, *, can_hold: bool, can_open: bool, today: date) -> dict:
    """The modal's values: what was typed so far (a device picked re-renders it), else the defaults: today (or the adopted request's
    day), the outcome not known yet, the hold on (for a user who may hold, with an investigation to go with it: the request taken
    over, or one they may open), and the investigation the request it was reported as (the work order drawer's, or the device's one
    open repair), else a new one."""
    typed = {k: v for k, v in params.items() if k in RecordForm.base_fields and k != "hold"}
    if params.get("shown"):  # the form was on screen: an unticked hold stays unticked
        typed["hold"] = bool(params.get("hold")) and can_hold
    adopt = fixed or (choices[0] if len(choices) == 1 else None)
    default = adopt.number if adopt is not None else NEW if can_open else NONE
    initial = {"outcome": Outcome.UNKNOWN, "hold": can_hold and (can_open or adopt is not None), "investigation": default,
               "occurred_on": adopt.opened_on if adopt is not None else today, **typed}
    if asset is not None:
        initial["asset"] = asset.tag
    return initial


def _record_modal(request, form, *, asset, fixed, choices, today):
    data = form.data if form.is_bound else form.initial
    preview = due_preview(occurred=_parse_day(data.get("occurred_on")) if form.is_bound else form.initial.get("occurred_on"),
                          aware=_parse_day(data.get("aware_on")), outcome=data.get("outcome", ""), affected=data.get("affected", ""), today=today)
    chosen = str(data.get("investigation") or "")
    return render(request, RECORD, {"form": form, "asset": asset, "fixed": fixed, "choices": choices, "chosen": chosen, "due": preview,
                                    "can_hold": perms.can_hold(request.user), "can_open": perms.can_open_work_order(request.user),
                                    "new": NEW, "none": NONE})


@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_new(request):
    """Record incident. GET the modal (?asset=<tag> from the device drawer, ?work_order=<number> from a repair's drawer: that work
    order is the investigation), POST record it (apps.incidents.services.record_incident). Saved, the incident's drawer replaces
    whatever drawer was open."""
    user = request.user
    today = timezone.localdate()
    params = request.POST if request.method == "POST" else request.GET
    fixed = WorkOrder.objects.select_related("asset", "asset__device_model", "asset__department").filter(number=params.get("work_order", "")).first()
    asset = _asset(fixed.asset.tag) if fixed is not None else _asset(params.get("asset", ""))
    choices = [fixed] if fixed is not None else adoptable(asset)
    can_hold, can_open = perms.can_hold(user), perms.can_open_work_order(user)
    if request.method != "POST":
        form = RecordForm(initial=_record_initial(request.GET, asset, fixed, choices, can_hold=can_hold, can_open=can_open, today=today),
                          asset=asset, adoptable=choices, can_open=can_open)
        return _record_modal(request, form, asset=asset, fixed=fixed, choices=choices, today=today)
    form = RecordForm(request.POST, asset=asset, adoptable=choices, can_open=can_open)
    incident = None
    if form.is_valid():
        try:
            incident = inc.record_incident(**form.service_kwargs(), by=user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if incident is None:
        return _record_modal(request, form, asset=asset, fixed=fixed, choices=choices, today=today)
    incident = _get(incident.number)
    parts = [f"{incident.number} recorded"]
    if incident.holds.exists():
        parts.append(f"{incident.asset.tag} held as evidence")
    if incident.work_order_id:
        parts.append(f"{incident.work_order.number} is its investigation")
    if incident.report_due_on:
        parts.append(f"report due {_due_day(incident.report_due_on, today)}")
    return _saved(request, incident, "; ".join(parts), "devices-changed", "wo-changed")


@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_due(request):
    """Record incident's due date, from the modal's fields as they stand (it re-fetches on each change)."""
    today = timezone.localdate()
    p = request.GET
    return render(request, DUE, {"due": due_preview(occurred=_parse_day(p.get("occurred_on")), aware=_parse_day(p.get("aware_on")),
                                                    outcome=p.get("outcome", ""), affected=p.get("affected", ""), today=today)})


@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_devices(request):
    """The device search of Record incident, or (?incident=<number>) of Hold another device: equipment.services.search_assets, with
    the device a scanned label or a typed tag names first (apps.equipment.scan: a handheld scanner types the label's link or the tag)."""
    q = request.GET.get("asset_q", "").strip()
    if len(q) < 2:
        return HttpResponse("")
    assets = list(search_assets(q))
    try:
        scanned = scan.find(q, slug=request.tenant.slug, qs=Asset.objects.select_related("device_model", "department"))
    except ValidationError:
        scanned = None
    if scanned is not None:
        assets = [scanned] + [a for a in assets if a.pk != scanned.pk]
    number = request.GET.get("incident", "")
    if number:
        i = _get(number)
        pick, form = reverse("web:incident_hold", args=[i.number]), "#inch-form"
    else:
        pick, form = reverse("web:incident_new"), "#inc-form"
    return render(request, PICKS, {"assets": assets, "pick_url": pick, "form_id": form})


# --- the drawer's actions ------------------------------------------------------------------------------------------------------

def _blocker(i) -> str:
    if i.status == Status.CLOSED:
        return f"{i.number} is closed: reopen it first."
    if i.status == Status.IN_ERROR:
        return f"{i.number} was recorded in error."
    return ""


@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_facts(request, number):
    """Edit facts: GET the modal, POST update_facts. A technician raises the outcome or moves the first-knew day earlier; lowering it,
    moving that day later (with its reason), or stopping the clock is a decider's (the service says so)."""
    i = _get(number)
    user = request.user
    can_decide = perms.can_decide(user)
    if request.method != "POST":
        return render(request, FACTS, {"i": i, "form": FactsForm(incident=i, can_decide=can_decide), "blocker": _blocker(i)})
    form = FactsForm(request.POST, incident=i, can_decide=can_decide)
    if form.is_valid():
        try:
            inc.update_facts(i, by=user, today=timezone.localdate(), **form.service_kwargs())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return render(request, FACTS, {"i": i, "form": form, "blocker": ""})
    return _saved(request, i, f"{i.number}: facts saved")


def _decide_initial(i, today) -> dict:
    return {"outcome": "" if i.outcome == Outcome.UNKNOWN else i.outcome, "basis": i.basis, "decided_on": i.decided_on or today,
            "decided_by": i.decided_by}


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_decide(request, number):
    """Decide: GET the modal (the 803.3 question), POST decide. With no event report number on file, the modal asks for it and saves
    it first (update_facts), both or neither."""
    i = _get(number)
    today = timezone.localdate()
    if request.method != "POST":
        return render(request, DECIDE, {"i": i, "form": DecideForm(incident=i, initial=_decide_initial(i, today)), "blocker": _blocker(i)})
    form = DecideForm(request.POST, incident=i)
    if form.is_valid():
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if d.get("event_reference"):
                    inc.update_facts(i, by=request.user, today=today, event_reference=d["event_reference"])
                inc.decide(i, outcome=d["outcome"], basis=d["basis"], decided_on=d["decided_on"], decided_by=d["decided_by"], by=request.user,
                           today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        i.refresh_from_db()
        return render(request, DECIDE, {"i": i, "form": form, "blocker": ""})
    i.refresh_from_db()
    return _saved(request, i, f"{i.number}: decided {'reportable' if i.reportable else 'not reportable'}")


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_reports(request, number):
    """Record reports: GET the modal, POST record_reports (one report number; the day each copy went out)."""
    i = _get(number)
    required = inc.required_recipients(i)
    ctx = {"i": i, "required": _and([inc.RECIPIENT_WORDS[r] for r in required]) if required else "", "blocker": _blocker(i)}
    if request.method != "POST":
        initial = {"fda_reported_on": i.fda_reported_on, "manufacturer_reported_on": i.manufacturer_reported_on, "report_number": i.report_number}
        return render(request, REPORTS, {**ctx, "form": ReportsForm(initial=initial)})
    form = ReportsForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            inc.record_reports(i, fda_reported_on=d["fda_reported_on"], manufacturer_reported_on=d["manufacturer_reported_on"],
                               report_number=d["report_number"], by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return render(request, REPORTS, {**ctx, "form": form, "blocker": ""})
    return _saved(request, i, f"{i.number}: reports recorded")


@require_POST
@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_finding(request, number):
    """The device evaluation's finding (record_finding), from the drawer's select. A failure found after a decision that the
    information does not suggest the device contributed clears that decision: the toast says so (services.DECISION_CLEARED)."""
    i = _get(number)
    form = FindingForm(request.POST)
    if not form.is_valid():
        return _refused(request, i, ValidationError(form.errors["finding"][0]))
    try:
        result = inc.record_finding(i, form.cleaned_data["finding"], by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _refused(request, i, e)
    message = inc.DECISION_CLEARED if getattr(result, "decision_cleared", False) else f"{i.number}: finding recorded"
    return toast(_changed(_drawer(request, i)), message)


@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_hold(request, number):
    """Hold another device (another part of the system): GET the modal with the device search (?asset=<tag>: the one picked), POST
    hold_device (Incidents Edit and Equipment Edit; with no investigation yet, also Work orders Edit to open one)."""
    i = _get(number)
    params = request.POST if request.method == "POST" else request.GET
    asset = _asset(params.get("asset", ""))
    ctx = {"i": i, "asset": asset, "blocker": _blocker(i), "error": ""}
    if request.method != "POST":
        return render(request, HOLD, ctx)
    if asset is None:
        return render(request, HOLD, {**ctx, "blocker": "", "error": "Choose the device from the list first."})
    try:
        inc.hold_device(i, asset, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        return render(request, HOLD, {**ctx, "blocker": "", "error": _words(e)})
    return _saved(request, i, f"{asset.tag} held as evidence for {i.number}", "devices-changed", "wo-changed")


@require_POST
@web_view(perms.MODULE, perms.RECORD_LEVEL)
def incident_investigation(request, number):
    """Open investigation (open_investigation): when the incident has no work order, or its work order was cancelled."""
    i = _get(number)
    try:
        wo = inc.open_investigation(i, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        return _refused(request, i, e)
    return toast(_changed(_drawer(request, i), "wo-changed"), f"{wo.number} opened as the investigation of {i.number}")


CUSTODY_WORDS = {"sent": ("sent_on", "Sent to the manufacturer", "went to the manufacturer for evaluation"),
                 "back": ("back_on", "Back from the manufacturer", "came back from the manufacturer")}


def _custody(request, number, pk, which: str):
    i = _get(number)
    hold = _get_hold(i, pk)
    field, title, verb = CUSTODY_WORDS[which]
    url = reverse(f"web:incident_hold_{which}", args=[i.number, hold.pk])
    blocker = _blocker(i) or (f"{i.number} released {hold.asset.tag} on {_full(hold.released_on)}." if not hold.active else "")
    ctx = {"i": i, "hold": hold, "title": title, "verb": verb, "url": url, "blocker": blocker}
    if request.method != "POST":
        return render(request, CUSTODY, {**ctx, "form": DayForm(field=field, initial={"on": timezone.localdate()})})
    form = DayForm(request.POST, field=field)
    if form.is_valid():
        write = inc.sent_to_manufacturer if which == "sent" else inc.back_from_manufacturer
        try:
            write(hold, on=form.cleaned_data["on"], by=request.user)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return render(request, CUSTODY, {**ctx, "blocker": "", "form": form})
    return _saved(request, i, f"{hold.asset.tag} {verb} on {_full(form.cleaned_data['on'])}")


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_hold_sent(request, number, pk):
    """A held device went to the manufacturer for evaluation (sent_to_manufacturer): the day."""
    return _custody(request, number, pk, "sent")


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_hold_back(request, number, pk):
    """A held device came back from the manufacturer (back_from_manufacturer): the day."""
    return _custody(request, number, pk, "back")


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_hold_release(request, number, pk):
    """Release a hold (release): returned to use, kept out of service, or kept by the manufacturer. Saved, the toast says what
    still keeps the device out (services.release_note)."""
    i = _get(number)
    hold = _get_hold(i, pk)
    blocker = _blocker(i) or (f"{i.number} released {hold.asset.tag} on {_full(hold.released_on)}." if not hold.active else "")
    ctx = {"i": i, "hold": hold, "words": RELEASE_WORDS, "blocker": blocker,
           "can_return": perms.can_return_to_use(request.user)}
    if request.method != "POST":
        initial = {"release": Release.KEPT_BY_MANUFACTURER if hold.with_manufacturer else ""}
        return render(request, RELEASE, {**ctx, "form": ReleaseForm(initial=initial)})
    form = ReleaseForm(request.POST)
    if form.is_valid():
        try:
            inc.release(hold, form.cleaned_data["release"], by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return render(request, RELEASE, {**ctx, "blocker": "", "form": form})
    hold.refresh_from_db()
    message = f"{hold.asset.tag} released: {Release(hold.release).label.lower()}"
    note = inc.release_note(hold)
    return _saved(request, i, f"{message}. {note}" if note else message, "devices-changed")


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_close(request, number):
    """Close: GET the modal saying what closing needs, POST close. Everything still missing is listed at once."""
    i = _get(number)
    if request.method != "POST":
        return render(request, CLOSE, {"i": i, "blocker": _blocker(i), "errors": []})
    try:
        inc.close(i, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        errors = [_words(e)] if isinstance(e, PermissionDenied) else e.messages
        return render(request, CLOSE, {"i": i, "blocker": "", "errors": errors})
    return _saved(request, i, f"{i.number} closed")


@require_POST
@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_reopen(request, number):
    i = _get(number)
    try:
        inc.reopen(i, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _refused(request, i, e)
    return toast(_changed(_drawer(request, i)), f"{i.number} reopened")


@web_view(perms.MODULE, perms.DECIDE_LEVEL)
def incident_in_error(request, number):
    """Recorded in error: GET the modal saying what it does, POST recorded_in_error (every hold released, each device back to its
    status before; the work order the incident opened cancelled). Kept, counted nowhere."""
    i = _get(number)
    if request.method != "POST":
        return render(request, IN_ERROR, {"i": i, "blocker": _blocker(i), "error": ""})
    try:
        inc.recorded_in_error(i, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        return render(request, IN_ERROR, {"i": i, "blocker": "", "error": _words(e)})
    return _saved(request, i, f"{i.number} marked recorded in error; its holds are released", "devices-changed", "wo-changed")
