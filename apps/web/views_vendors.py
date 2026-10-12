"""
The Contracts screen's Vendor staff tab (slice 31, beyond the mock): the evidence the facility keeps that the vendors' staff who service
its equipment were trained for it. One section per vendor, merged by apps.vendors.rules.vendor_key (the vendors with engineers or
attestations on file, the contracts' vendors, and the vendors of vendor-service work completed in the last 12 months, so a vendor with
nothing on file says so): its attestations first (the low-effort, usual evidence: one letter a year), then its engineers with their
training on file. `?vendor=<name>` shows one vendor (by key: the survey binder's gaps and the contract drawer's line link here), and the
search box narrows by a vendor's name (any spelling) or an engineer's.

The engineer drawer (web:vendor_engineer: a partial into #drawer, the tab with the drawer open when opened directly): their details
(who added them, and when a vendor account added them at a completion, which work order), their training on file (Remove: entered in
error), the work orders they are named on (Work orders View, through scoping.work_orders; a device links only with Equipment View),
and the History section (the engineer's changes with their trainings'). The modals: Add engineer, Change engineer, Add training (one
coverage for one or several of a vendor's engineers), Add attestation.

Who: Contracts View sees, Contracts Edit adds, changes, and removes (apps.vendors.permissions); scoped users are refused every view
here (web_view's default), even with Contracts View on a custom role: other vendors' engineers and the facility's evidence are never
theirs. Every write is apps.vendors.services; the words are apps.vendors.rules' (Cadence keeps documents and never judges competence:
"training on file", "attestation on file", never "trained" or "qualified"). Each change fires `vendors-changed` (the tab's body
re-fetches itself on it) and toasts.

Privacy: an engineer is a name and a vendor. Nothing here is emailed.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import Count, F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts import services as account_services
from apps.accounts.models import Level, Module
from apps.contracts.models import Contract
from apps.core import history
from apps.core.days import local_day
from apps.equipment.models import DeviceModel
from apps.pm.dates import add_months
from apps.vendors import permissions as perms
from apps.vendors import rules
from apps.vendors import services as vs
from apps.vendors.models import EngineerTraining, VendorAttestation, VendorEngineer
from apps.workorders import scoping
from apps.workorders.models import WorkOrder, WoStatus

from . import history_tabs
from .decorators import web_view
from .forms import parse_uuid
from .forms_vendors import (
    ATTESTATION_STATES,
    CREDENTIALING_ELSEWHERE,
    VENDOR_LIST,
    AttestationForm,
    EngineerForm,
    TrainingForm,
    VendorFilters,
    engineer_initial,
    parse_vendor_filters,
)
from .htmx import is_partial, toast

PAGE = "web/vendor_staff.html"
BODY = "web/_vendor_body.html"
DRAWER = "web/_vendor_engineer_drawer.html"
ENGINEER_MODAL = "web/_vendor_engineer_form.html"
TRAINING_MODAL = "web/_vendor_training_new.html"
ATTESTATION_MODAL = "web/_vendor_attestation_new.html"
CHANGED = "vendors-changed"  # the tab's body re-fetches itself on it
SOON_DAYS = 90  # a training or an attestation ending within this many days is flagged
JOB_MONTHS = 12  # "N vendor jobs in the last 12 months"
WORK_SHOWN = 25  # the engineer drawer's work orders: the newest this many, then "and N more"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
ADDED_AT_COMPLETION = "Added at completion of "  # vendors.services.engineer_named's history reason


# --- reading ----------------------------------------------------------------------------------------------------------------------

def _models_by_id(records) -> dict:
    """The catalog's device models the records name, by str(id), for rules.coverage_words (one query)."""
    ids = {str(i) for r in records for i in (r.model_ids or ())}
    return {str(m.pk): m for m in DeviceModel.objects.filter(pk__in=ids)} if ids else {}


def training_chip(t, today: date) -> tuple[str, str]:
    """A training's state by its own dates: current, expiring within SOON_DAYS, or expired."""
    if t.expires_on is not None and t.expires_on < today:
        return "crit", f"Expired {rules.fmt(t.expires_on)}"
    if t.expires_on is not None and t.expires_on <= today + timedelta(days=SOON_DAYS):
        return "warn", f"Expires {rules.fmt(t.expires_on)}"
    return "ok", "Current"


def attestation_chip(a, today: date) -> tuple[str, str]:
    """An attestation's state by the period it states: on file, ending within SOON_DAYS, ended, or not started yet."""
    if a.valid_through < today:
        return "neutral", f"Ended {rules.fmt(a.valid_through)}"
    if a.valid_from > today:
        return "info", f"Starts {rules.fmt(a.valid_from)}"
    if a.valid_through <= today + timedelta(days=SOON_DAYS):
        return "warn", f"Ends {rules.fmt(a.valid_through)}"
    return "ok", "On file"


def _training_rows(trainings, models_by_id, today) -> list[dict]:
    return [{"t": t, "words": rules.training_words(t, models_by_id), "chip": training_chip(t, today)} for t in trainings]


@dataclass
class VendorSection:
    """One vendor on the tab: every spelling of it the facility uses (the one shown first), its contracts, attestations, engineers,
    and (Work orders View) its vendor jobs in the last JOB_MONTHS months."""
    key: str
    names: list = field(default_factory=list)
    contracts: list = field(default_factory=list)  # (id, reference)
    attestations: list = field(default_factory=list)  # {"a", "words", "chip"}
    engineers: list = field(default_factory=list)  # {"e", "trainings": [{"t", "words", "chip"}]}
    jobs: int | None = None
    active_engineers: int = 0  # Add training is offered when the vendor has one
    narrowed: bool = False  # the search matched some of its engineers only

    @property
    def name(self) -> str:
        return self.names[0]

    @property
    def also(self) -> list:
        return self.names[1:]

    @property
    def empty(self) -> bool:
        return not self.attestations and not self.engineers

    @property
    def vendor_query(self) -> str:
        """The modals' query for this vendor (Add engineer, Add training, Add attestation)."""
        return "?" + urlencode({"vendor": self.name})


def _section(sections: dict, name: str) -> VendorSection | None:
    name = " ".join(str(name or "").split())
    key = rules.vendor_key(name)
    if not key:
        return None
    s = sections.setdefault(key, VendorSection(key))
    if name.casefold() not in {n.casefold() for n in s.names}:
        s.names.append(name)
    return s


def vendor_sections(user, today: date) -> list[VendorSection]:
    """Every vendor the facility has on file or works with, merged by vendor_key, by name. A fixed number of queries (engineers,
    trainings, attestations, contracts, the catalog's named models, and, with Work orders View, one count of the last JOB_MONTHS
    months' vendor jobs by vendor name). Names are spelled as the contracts write them first, then the engineers' vendors, the
    attestations', and the work orders'."""
    sections: dict[str, VendorSection] = {}
    for pk, reference, vendor in Contract.objects.order_by("vendor", "reference").values_list("pk", "reference", "vendor"):
        s = _section(sections, vendor)
        if s is not None:
            s.contracts.append((pk, reference))
    engineers = list(VendorEngineer.objects.order_by("name"))
    trainings = list(EngineerTraining.objects.order_by("-issued_on", "created_at"))
    attestations = list(VendorAttestation.objects.order_by("-valid_through", "reference"))
    models_by_id = _models_by_id([*trainings, *attestations])
    by_engineer: dict = {}
    for t in trainings:
        by_engineer.setdefault(t.engineer_id, []).append(t)
    for e in engineers:
        s = _section(sections, e.company)
        if s is not None:
            s.engineers.append({"e": e, "trainings": _training_rows(by_engineer.get(e.pk, []), models_by_id, today)})
    for a in attestations:
        row = {"a": a, "words": rules.coverage_words(a, models_by_id), "chip": attestation_chip(a, today)}
        for name in a.vendor_names or ():
            s = _section(sections, name)
            if s is not None and row not in s.attestations:
                s.attestations.append(row)
    if user.has_level(Module.WORKORDERS, Level.VIEW):
        jobs = scoping.work_orders(user, WorkOrder.objects.filter(vendor_service=True, status__in=DONE, completed_on__lte=today,
                                                                  completed_on__gt=add_months(today, -JOB_MONTHS)))
        for s in sections.values():
            s.jobs = 0
        for row in jobs.values("vendor_name").annotate(n=Count("pk")).order_by():
            s = _section(sections, row["vendor_name"])
            if s is not None:
                s.jobs = (s.jobs or 0) + row["n"]
    for s in sections.values():  # the engineers in name order, active first
        s.engineers.sort(key=lambda r: (not r["e"].is_active, r["e"].name.casefold()))
        s.active_engineers = sum(r["e"].is_active for r in s.engineers)
    return sorted(sections.values(), key=lambda s: s.name.casefold())


def filter_sections(sections: list[VendorSection], f: VendorFilters) -> list[VendorSection]:
    """`?vendor=` keeps that vendor (by key; one with nothing on file and no work here still shows, under the name given, so its
    evidence can be added); `?q=` keeps the vendors with a name containing it, and the vendors with an engineer whose name does (those
    engineers only)."""
    if f.vendor:
        key = rules.vendor_key(f.vendor)
        sections = [s for s in sections if s.key == key] or ([VendorSection(key, [f.vendor])] if key else [])
    if f.q:
        q = f.q.casefold()
        kept = []
        for s in sections:
            if any(q in n.casefold() for n in s.names):
                kept.append(s)
                continue
            matched = [r for r in s.engineers if q in r["e"].name.casefold()]
            if matched:
                s.engineers, s.narrowed = matched, True
                kept.append(s)
        sections = kept
    return sections


def _summary(sections: list[VendorSection], today: date) -> dict:
    engineers = [r["e"] for s in sections for r in s.engineers]
    attestations = {r["a"].pk for s in sections for r in s.attestations if rules.attestation_in_force(r["a"], today)}
    return {"vendors": len(sections), "engineers": sum(e.is_active for e in engineers), "attestations": len(attestations)}


def vendor_line(vendor_name: str, today: date) -> dict:
    """The contract drawer's line for its vendor: "Philips: 2 engineers with training on file; attestation LTR-1 on file through Dec
    31, 2026", "Philips: nothing on file in force today" (records that have lapsed), or "Nothing on file for Philips", with the link to
    the Vendor staff tab filtered to the vendor. Active engineers count; trainings and attestations by their own dates, today."""
    url = f"{reverse('web:vendor_staff')}?{urlencode({'vendor': vendor_name})}"
    key = rules.vendor_key(vendor_name)
    trainings = EngineerTraining.objects.filter(engineer__company_key=key, engineer__is_active=True) if key else EngineerTraining.objects.none()
    current = trainings.filter(issued_on__lte=today).filter(Q(expires_on__isnull=True) | Q(expires_on__gte=today))
    n = current.values("engineer_id").distinct().count()
    attestations = vs.attestations_for(vendor_name) if key else []
    in_force = [a for a in attestations if rules.attestation_in_force(a, today)]
    parts = []
    if n:
        parts.append(f"{n} engineer{'' if n == 1 else 's'} with training on file")
    if in_force:
        parts.append(f"attestation {in_force[0].reference} on file through {rules.fmt(in_force[0].valid_through)}")
    if parts:
        text = f"{vendor_name}: {'; '.join(parts)}"
    elif attestations or trainings.exists():
        text = f"{vendor_name}: nothing on file in force today"
    else:
        text = f"Nothing on file for {vendor_name}"
    return {"text": text, "url": url}


# --- the tab -----------------------------------------------------------------------------------------------------------------------

def _list_context(request, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    f = parse_vendor_filters(request.GET)
    every = vendor_sections(request.user, today)
    summary = _summary(every, today)  # the facility's, before the filters narrow a vendor's engineers
    return {"nav_active": "contracts", "contracts_tab": "vendors", "list_url": reverse("web:vendor_staff"), "f": f, "today": today,
            "sections": filter_sections(every, f), "summary": summary, "can_edit": perms.can_edit(request.user),
            "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW), "soon_days": SOON_DAYS, "job_months": JOB_MONTHS}


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_staff(request):
    ctx = _list_context(request)
    if is_partial(request, "vs-body"):
        return render(request, BODY, ctx)
    return render(request, PAGE, ctx)


def _body(request, message: str):
    """The tab's body after a change made from it (Remove an attestation), with its filters (the post's query) and the toast. No
    `vendors-changed`: the body swapped in is already the new one."""
    return toast(render(request, BODY, _list_context(request)), message)


# --- the engineer drawer --------------------------------------------------------------------------------------------------------

def _get_engineer(pk) -> VendorEngineer:
    return get_object_or_404(VendorEngineer.objects, pk=pk)


def _added(engineer) -> dict | None:
    """Who added the engineer and when (their first history row), and the work order a completion added them at (a vendor account's
    "someone else": CE reviews it here)."""
    rec = history._rows(VendorEngineer.history.model).select_related("history_user").filter(id=engineer.pk).order_by(
        "history_date", "history_id").first()
    if rec is None:
        return None
    reason = (rec.history_change_reason or "").strip()
    number = reason[len(ADDED_AT_COMPLETION):] if reason.startswith(ADDED_AT_COMPLETION) else ""
    return {"on": local_day(rec.history_date), "who": history.who(rec), "work_order": number}


def _engineer_history(request, engineer, url: str) -> dict:
    """The History section's page: the engineer's changes with their trainings' (removed ones too), newest first; as
    history_tabs.context reads a record's (its RELATED has no vendor areas, so the trainings are named here)."""
    after = request.GET.get("older") or None
    if after is not None and history.parse_cursor(after) is None:
        after = None
    related = {"engineer_training": {"engineer_id": engineer.pk}} if history.can_read(request.user, "engineer_training") else {}
    entries, following = history.record_history(engineer, related, limit=history_tabs.PAGE, after=after)
    rows = [{"e": e, "title": e.action_label if e.area == "vendor_engineers" else f"Training {history_tabs.VERBS.get(e.action, e.action)}"}
            for e in entries]
    more_url = f"{url}?{urlencode({'history': 1, 'older': following})}" if following else None
    return {"hist": {"rows": rows, "more_url": more_url, "first": after is None}}


def _work(user, engineer) -> tuple[list, int]:
    """The work orders that name the engineer, as the user may see them (scoping.work_orders), newest first: open ones, then by the
    day completed."""
    qs = scoping.work_orders(user, WorkOrder.objects.filter(vendor_engineer=engineer))
    rows = list(qs.select_related("asset", "asset__device_model").order_by(F("completed_on").desc(nulls_first=True), "-opened_on",
                                                                           "-number")[:WORK_SHOWN])
    total = len(rows) if len(rows) < WORK_SHOWN else qs.count()
    return rows, total


def _drawer_context(request, engineer) -> dict:
    user = request.user
    today = timezone.localdate()
    trainings = list(engineer.trainings.order_by("-issued_on", "created_at"))
    can_wo = user.has_level(Module.WORKORDERS, Level.VIEW)
    work, work_total = _work(user, engineer) if can_wo else ([], 0)
    url = reverse("web:vendor_engineer", args=[engineer.pk])
    can_history = history.can_read(user, "vendor_engineers")
    hist = _engineer_history(request, engineer, url) if can_history and request.GET.get("history") else {}
    return {
        "eng": engineer, "today": today, "added": _added(engineer),
        "trainings": _training_rows(trainings, _models_by_id(trainings), today),
        "vendor_url": f"{reverse('web:vendor_staff')}?{urlencode({'vendor': engineer.company})}",
        "can_edit": perms.can_edit(user), "can_view_wo": can_wo, "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW),
        "work": work, "work_more": work_total - len(work), "work_total": work_total,
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist,
    }


def _drawer(request, engineer):
    return render(request, DRAWER, _drawer_context(request, _get_engineer(engineer.pk)))


@web_view(perms.MODULE, perms.VIEW_LEVEL)
def vendor_engineer(request, pk):
    engineer = _get_engineer(pk)
    if history_tabs.asked(request, tab=False):  # the History section or its Show older (Contracts View; scoped users never get here)
        history_tabs.require(request.user, "vendor_engineers")
        if history_tabs.wants_entries(request):
            return render(request, history_tabs.ENTRIES, _engineer_history(request, engineer, reverse("web:vendor_engineer", args=[pk])))
    ctx = _drawer_context(request, engineer)
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, DRAWER, ctx)
    return render(request, PAGE, {**_list_context(request), **ctx, "drawer_template": DRAWER})


def _saved_to_drawer(request, engineer, message: str):
    """A modal saved for an engineer: their drawer into #drawer, the tab's body refreshed, the toast, then the modal closed after
    settle (closing it first would detach the form that sent this request, which cancels the swap and the events)."""
    response = retarget(_drawer(request, engineer), "#drawer")
    toast(trigger_client_event(response, CHANGED, {}), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _saved_to_tab(message: str):
    """A modal saved from the tab: nothing to swap; the body re-fetches itself (keeping its filters), the toast, the modal closed."""
    response = toast(trigger_client_event(HttpResponse(""), CHANGED, {}), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


# --- engineers ----------------------------------------------------------------------------------------------------------------------

def _suggestions(request) -> list[str]:
    return account_services.company_suggestions(request.tenant)


def _engineer_modal(request, form, engineer=None):
    return render(request, ENGINEER_MODAL, {"form": form, "eng": engineer, "suggestions": _suggestions(request), "vendor_list": VENDOR_LIST})


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def engineer_new(request):
    """Add engineer: GET the modal (?vendor=<name> from a vendor's section), POST vendors.services.add_engineer. Saved, the new
    engineer's drawer opens (to add their training next)."""
    if request.method != "POST":
        vendor = " ".join(request.GET.get("vendor", "").split())
        return _engineer_modal(request, EngineerForm(initial={"company": vendor} if vendor else None))
    form = EngineerForm(request.POST)
    engineer = None
    if form.is_valid():
        try:
            engineer = vs.add_engineer(**form.service_kwargs(), by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if engineer is None:
        return _engineer_modal(request, form)
    return _saved_to_drawer(request, engineer, f"{engineer.name} added to {engineer.company}")


def _named_on_work(engineer) -> bool:
    return WorkOrder.objects.filter(vendor_engineer=engineer).exists()


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def engineer_change(request, pk):
    """Change engineer: their name, their vendor (until a work order names them: then the box is read-only and the service refuses a
    change), and whether they are active (an inactive engineer is never offered for new work; their records stay)."""
    engineer = _get_engineer(pk)
    fixed = _named_on_work(engineer)
    if request.method != "POST":
        return _engineer_modal(request, EngineerForm(initial=engineer_initial(engineer), change=True, fixed=fixed), engineer)
    form = EngineerForm(request.POST, change=True, fixed=fixed)
    saved = False
    if form.is_valid():
        try:
            vs.update_engineer(engineer, **form.service_kwargs(), by=request.user)
            saved = True
        except ValidationError as e:
            form.add_service_errors(e)
    if not saved:
        return _engineer_modal(request, form, engineer)
    return _saved_to_drawer(request, engineer, f"{engineer.name} ({engineer.company}) saved")


# --- trainings ----------------------------------------------------------------------------------------------------------------------

def _training_offer(params) -> tuple[str, list, VendorEngineer | None]:
    """Whose training the modal adds: (the vendor's name, the engineers offered, the engineer it was opened from). From an engineer
    (?engineer=<id>): their vendor's active engineers and them (inactive too: a certificate from before they stopped coming). From a
    vendor's section (?vendor=<name>): that vendor's active engineers."""
    from_engineer = None
    pk = parse_uuid(params.get("engineer", ""))
    if pk is not None:
        from_engineer = _get_engineer(pk)
    vendor = from_engineer.company if from_engineer is not None else " ".join(params.get("vendor", "").split())
    offered = list(vs.engineers_for(vendor))
    if from_engineer is not None and from_engineer not in offered:
        offered.insert(0, from_engineer)
    return vendor, offered, from_engineer


def _training_modal(request, form, vendor, offered, from_engineer):
    return render(request, TRAINING_MODAL, {"form": form, "vendor": vendor, "offered": offered, "from_engineer": from_engineer})


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def training_new(request):
    """Add training: GET the modal (?vendor=<name> or ?engineer=<id>), POST vendors.services.add_training for the engineers ticked.
    From an engineer's drawer the drawer comes back; from the tab the body re-fetches."""
    today = timezone.localdate()
    params = request.POST if request.method == "POST" else request.GET
    vendor, offered, from_engineer = _training_offer(params)
    if request.method != "POST":
        initial = {"engineers": [str(from_engineer.pk)]} if from_engineer is not None else None
        return _training_modal(request, TrainingForm(initial=initial, engineers=offered, today=today, vendor=vendor), vendor, offered, from_engineer)
    form = TrainingForm(request.POST, engineers=offered, today=today, vendor=vendor)
    made = None
    if form.is_valid():
        try:
            made = vs.add_training(form.chosen(), **form.service_kwargs(), by=request.user, today=today)
        except ValidationError as e:
            form.add_service_errors(e)
    if made is None:
        return _training_modal(request, form, vendor, offered, from_engineer)
    who = made[0].engineer.name if len(made) == 1 else f"{len(made)} engineers of {vendor}"
    message = f"Training on file added for {who}"
    if from_engineer is not None:
        return _saved_to_drawer(request, from_engineer, message)
    return _saved_to_tab(message)


@require_POST
@web_view(perms.MODULE, perms.EDIT_LEVEL)
def training_remove(request, pk):
    """Remove a training entered in error (from the engineer drawer): it covers no day from now on; its history keeps it. A training
    that lapsed is not removed: it covered its own days."""
    training = get_object_or_404(EngineerTraining.objects.select_related("engineer"), pk=pk)
    engineer = training.engineer
    vs.remove_training(training, by=request.user)
    return toast(trigger_client_event(_drawer(request, engineer), CHANGED, {}), f"Training removed from {engineer.name}'s record (entered in error)")


# --- attestations ----------------------------------------------------------------------------------------------------------------

def _attestation_modal(request, form):
    return render(request, ATTESTATION_MODAL, {"form": form, "suggestions": _suggestions(request), "vendor_list": VENDOR_LIST,
                                               "states": ATTESTATION_STATES, "elsewhere": CREDENTIALING_ELSEWHERE})


@web_view(perms.MODULE, perms.EDIT_LEVEL)
def attestation_new(request):
    """Add attestation: GET the modal (?vendor=<name> from a vendor's section fills the first name), POST
    vendors.services.add_attestation."""
    today = timezone.localdate()
    if request.method != "POST":
        vendor = " ".join(request.GET.get("vendor", "").split())
        return _attestation_modal(request, AttestationForm(today=today, vendor=vendor))
    form = AttestationForm(request.POST, today=today)
    made = None
    if form.is_valid():
        try:
            made = vs.add_attestation(**form.service_kwargs(), by=request.user, today=today)
        except ValidationError as e:
            form.add_service_errors(e)
    if made is None:
        return _attestation_modal(request, form)
    return _saved_to_tab(f"Attestation {made.reference} on file for {', '.join(made.vendor_names)}, valid through {rules.fmt(made.valid_through)}")


@require_POST
@web_view(perms.MODULE, perms.EDIT_LEVEL)
def attestation_remove(request, pk):
    """Remove an attestation entered in error (from the tab): it covers no day from now on; its history keeps it."""
    attestation = get_object_or_404(VendorAttestation.objects, pk=pk)
    vs.remove_attestation(attestation, by=request.user)
    return _body(request, f"Attestation {attestation.reference} removed (entered in error)")
