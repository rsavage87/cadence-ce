"""
Security decisions on the Device security screen (slice 39, beyond the mock; build39 section 7): the Decisions tab with Needs attention
and the list (and its CSV), a decision's page (the drawer; the tab with it open when its address is opened directly), its print and its
devices' CSV, and the Record decision, End, and Entered in error modals. Views parse input (forms_cyber_decisions.py), call
apps.cybersecurity.decisions, and answer with the drawer and a toast; every word about a decision, a need, a response, or a Check line
is apps.cybersecurity.rules' (never composed here from the fields). HHS 405(d) HICP 2023 recommends documenting mitigating controls and a
timeline when a system past its support stays in use (rules.HICP_DECISIONS, said as a recommendation): no rule requires any of it.

- The Decisions tab (`/security/decisions/`): HICP's sentence, Needs attention (rules.needs_attention: each heading with its count and
  rows, links reversed from its data; Record decision and End for those who may decide; its empty words), and the decisions (Not ended
  by default; In force, Review within 30 days, Review due, Ended, or all; by scope kind, decision, and IT exception number), each row's
  number opening its page in the drawer. Its body re-fetches itself on `cyber-changed`. Export carries the filters.
- A decision's page (`/security/decisions/<number>/`, any letter case): its sentence (rules.decision_words), Check lines, the scope, the
  reasons recorded and what holds now, the controls, the plan, who accepted it, IT's exception number, who recorded it and the day,
  the review, the end, supersedes and superseded by, the devices on site in its scope (tag, serial, model, department, connection,
  release, response), and History (the device_security_decisions area). Review and renew, End, and Entered in error for those who may
  decide, on a live one: Review and renew only while the renewal would be taken, else decisions.refusal_for's words say why (a fact to
  record first; or nothing in its scope needing one, End then the main action). Print (a new tab, print_base) and the devices' CSV.
- Record decision (`/security/decisions/new/`, a modal): its scope from the address (asset=<tag>, release=<id>, model=<id>, or
  supersedes=<number> for Review and renew), and `from=device` (the device drawer's Security tab) or `from=panel` (a release row of the
  model panel, with the panel's filters) for where the save answers; otherwise the new decision's page. What cannot be recorded now (a
  device not on site, nothing past its support, a live decision for the scope, a decision already ended) is said before it is tried,
  in the service's words (Review and renew's address keeping the rest of this one: a release row's panel filters). Refusals come back
  under the fields they name, the scope's at the top, with what was chosen kept. A review day already passed is said once before it is
  saved, only for a decision the service would take (a dry run, rolled back: else its refusals come back first).

Slice 40, decisions for the devices a security advisory names (build40 sections 7.5, 7.7; every door keyed by the decision's need: an
advisory decision is read and recorded only beside its own advisory, never a support need):
- Record decision takes `advisory=<number>` with its scope (asset=, release=, or model=; the advisory alone is a 404), and Review and
  renew reads the advisory from the decision renewed (a different `advisory=` beside `supersedes=` is a 404). The modal is keyed: the
  four responses and every control (forms_cyber_decisions), the advisory's hint (rules.ADVISORY_DECISION_HINT) and HHS 405(d) HICP 2023's
  sentence for advisories (rules.HICP_ADVISORY_DECISIONS) in place of the support hint, the advisory's name and the lines of the devices
  it would govern that need a response now in place of the reasons, and network_removed's refusal (decisions.network_refusal) said beside
  it before it is tried. A scope whose devices are each recorded "Not connected here" starts at Keep in use with controls, taken off the
  network (critique40_practice S1). A new advisory decision is prefilled (forms_cyber_decisions.prefill_initial, its note) from the live
  decision of exactly this scope of another need key (support first, else the most recently decided advisory decision), past its review
  or not; a source that copies nothing (another advisory's "not in use here": prefill_copies) prefills nothing, with no note, and the
  "Not connected here" start holds as with none. `from=advisory` answers with the advisory's page (views_cyber_advisories.advisory_saved);
  opened directly, it goes there.
- The list's "Why" filter (`why=support|advisory`) and columns (the CSV's "Why" and "Advisory" at the end); a decision's page and print
  name the advisory ("For: AD-26-0003 · DEMO-ADV-0001 · Alaris 8015 PCU", a link to its page), what holds now from its devices' lines
  under the advisory, the verb from rules.who_verb, and HICP's sentence by key; the tab shows both sentences, the second without the
  last sentence they share (IT's exception stays in IT's system: HICP_TAIL, said once; hicp_second).

Who: reading needs permissions.can_view_devices (Device security View and Equipment View: a decision names devices); recording, renewing,
ending, and marking one entered in error need permissions.can_decide (Device security Approve and Equipment View). Anyone else gets a
403 from every door here, as the Fleet's device lists do. Never a scoped user (web_view's default). The services check every level again.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_htmx.http import retarget, trigger_client_event

from apps.accounts import people
from apps.accounts.models import Level, Module, User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity.models import (
    EXCEPTION_REFERENCE_MAX,
    AcceptedBy,
    AppliesTo,
    DecisionResponse,
    DeviceSecurityDecision,
    SecurityAdvisory,
    SoftwareRelease,
)
from apps.equipment.models import Asset, DeviceModel, Ownership
from apps.facility import fiscal

from . import history_tabs, views_cybersecurity
from .decorators import web_view
from .exports import csv_response
from .forms import parse_uuid
from .forms_cyber_decisions import EndForm, RecordDecisionForm, prefill_copies, prefill_initial, prefill_note, renew_initial
from .htmx import PAGE_SIZE, is_partial, toast
from .views import asset_drawer_context, get_asset

MODULE = cyber_perms.MODULE
VIEW, APPROVE = cyber_perms.VIEW_LEVEL, cyber_perms.APPROVE_LEVEL
AREA = "device_security_decisions"  # apps.core.history's area (Device security View and Equipment View)
PAGE = "web/cyber_decisions.html"
BODY = "web/_cyber_decisions.html"
BODY_ID = "cy-decisions"
DRAWER = "web/_cyber_decision.html"
RECORD = "web/_cyber_decision_new.html"
END = "web/_cyber_decision_end.html"
PRINT = "web/print_cyber_decision.html"
DEVICE_DRAWER = "web/_asset_drawer.html"
CHANGED = views_cybersecurity.CHANGED  # the screen's lists (and this tab's body) re-fetch themselves on it
DEVICES_WORDS = "Security decisions need Device security View and Equipment View."
ORIGINS = ("device", "panel", "advisory")  # where a saved Record decision answers (the default: the new decision's page)

# The toasts (numbers and the scope in rules' words only)
RECORDED = "{number} recorded for {scope}."
RENEWED = "{number} recorded for {scope}, superseding {old}."
ENDED = "{number} ended: no longer needed."
IN_ERROR = "{number} marked entered in error."
# The End and Entered in error modals' words
# Never why it is no longer needed (the person ending it knows: its devices updated, replaced, retired, or returned, or each under a
# decision of its own): only what ending it does
END_WORDS = "End {number} as no longer needed. It stays on record as it was, with its history, and from today it governs no device."
IN_ERROR_WORDS = ("Mark {number} entered in error: it stays on record, marked, with its history, and from today it governs no device. "
                  "Record the decision again if one is needed.")
# The print's closing lines
SYSTEM_OF_RECORD = "As recorded in Cadence; IT's exception record ({reference}) is the system of record."
NO_EXCEPTION = "As recorded in Cadence; no IT exception number is recorded for it."
# The last sentence HICP's two sentences share (rules.HICP_DECISIONS, rules.HICP_ADVISORY_DECISIONS): the Decisions tab, showing both,
# says it once, after the first (hicp_second; smoke S2). A page showing one sentence shows it whole.
HICP_TAIL = "IT's exception stays in IT's system; record its number here."


def hicp_second(first: str, second: str) -> str:
    """The second of two HICP sentences shown together: without HICP_TAIL when both end with it (said once, by the first), else whole
    (a later edit of either never drops words the first does not say)."""
    if first.endswith(HICP_TAIL) and second.endswith(HICP_TAIL):
        return second.removesuffix(HICP_TAIL).rstrip()
    return second


# The list's filters, in words (the values are rules' and the models' own slugs)
LIVE, ALL = "", "all"
STATE_FILTERS = [(LIVE, "Not ended"), *((s, rules.DECISION_STATE_WORDS[s]) for s in rules.DECISION_STATES), (ALL, "All, ended too")]
SCOPE_FILTERS = [("", "Any scope"), *((k, rules.SCOPE_LABELS[k]) for k in (rules.DEVICE_SCOPE, rules.RELEASE_SCOPE, rules.MODEL_SCOPE))]
RESPONSE_FILTERS = [("", "Any decision"), *DecisionResponse.choices]
# Slice 40: why a decision was recorded (the API's ?applies_to= mirrors it), and the list's and CSV's "Why" words
SUPPORT, ADVISORY = "support", "advisory"
WHY_FILTERS = [("", "Any reason"), (SUPPORT, AppliesTo.SUPPORT_ENDED.label), (ADVISORY, "A security advisory")]
WHY_APPLIES = {SUPPORT: AppliesTo.SUPPORT_ENDED, ADVISORY: AppliesTo.ADVISORY}
WHY_ADVISORY = "Security advisory {number}"
TONES = {rules.IN_FORCE: "ok", rules.REVIEW_SOON: "info", rules.REVIEW_DUE: "warn", rules.ENDED: "neutral"}
# A heading's count links to the list it counts, where the list can show it
HEADING_STATES = {"review_due": rules.REVIEW_DUE, "review_soon": rules.REVIEW_SOON}
# The review headings count only decisions whose scope still holds a device here (rules.holds_device_q): their link narrows the same way
HEADING_HERE = {"review_due": {"here": "1"}, "review_soon": {"here": "1"}}
_RELATED = ("asset", "asset__device_model", "asset__department", "device_model", "release", "release__device_model", "plan_owner",
            "recorded_by", "ended_by", "supersedes", "superseded_by", "advisory")


# --- small parts -------------------------------------------------------------------------------------------------------------------

def _need_devices(user) -> None:
    if not cyber_perms.can_view_devices(user):
        raise PermissionDenied(DEVICES_WORDS)


def _need_decide(user) -> None:
    if not cyber_perms.can_decide(user):
        raise PermissionDenied(decisions.DECIDE_PERMISSION)


def _decision(number) -> DeviceSecurityDecision:
    """A decision of this facility by its number, in any letter case (another facility's, or none, is a 404)."""
    text = str(number or "").strip()
    found = DeviceSecurityDecision.objects.select_related(*_RELATED).filter(number__iexact=text).first() if text else None
    if found is None:
        raise Http404
    return found


def decision_url(number: str) -> str:
    return reverse("web:security_decision", args=[number])


def advisory_url(number: str) -> str:
    return reverse("web:security_advisory", args=[number])


def _advisory(number) -> SecurityAdvisory:
    """An advisory of this facility by its number, in any letter case, read without a lock (another facility's, or none, is a 404).
    One entered in error, or marked not applicable, is found: decisions.refusal_for says why nothing can be recorded under it."""
    text = str(number or "").strip()
    found = SecurityAdvisory.objects.filter(number__iexact=text).first() if text and "\x00" not in text else None
    if found is None:
        raise Http404
    return found


def why_words(d) -> str:
    """The list's and CSV's "Why": "Support ended, or its end announced", or "Security advisory AD-26-0003" (reads d.advisory)."""
    return WHY_ADVISORY.format(number=d.advisory.number) if d.advisory_id else AppliesTo.SUPPORT_ENDED.label


def link_url(link) -> str:
    """A rules.Link reversed (its query appended), or "" for none."""
    if link is None:
        return ""
    url = reverse(link.name, args=list(link.args))
    return f"{url}?{urlencode([(k, str(v)) for k, v in link.query])}" if link.query else url


def record_url(**query) -> str:
    """Record decision's address for a scope (asset=, release=, model=, or supersedes=) and where it answers (from=)."""
    return f"{reverse('web:security_decision_new')}?{urlencode({k: str(v) for k, v in query.items() if v not in (None, '')})}"


def _linked(words: str, number: str, url: str) -> dict:
    """A line split around the number it names, so the number can be a link: {"before", "number", "after", "url"}."""
    if number and url and number in words:
        before, after = words.split(number, 1)
        return {"before": before, "number": number, "after": after, "url": url}
    return {"before": words, "number": "", "after": "", "url": ""}


def _unique(lines) -> list:
    out = []
    for line in lines:
        if line not in out:
            out.append(line)
    return out


def _state(d, today) -> dict:
    state = rules.decision_state(d, today)
    return {"key": state, "words": rules.DECISION_STATE_WORDS[state], "tone": TONES[state]}


def _shell(request) -> dict:
    user = request.user
    return {"nav_active": "cybersecurity", "cyber_tab": "decisions", "can_devices": cyber_perms.can_view_devices(user),
            "can_edit": cyber_perms.can_edit(user), "can_edit_devices": cyber_perms.can_edit_devices(user)}


# --- the Decisions tab ---------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ListFilters:
    state: str = LIVE
    scope: str = ""
    response: str = ""
    ref: str = ""
    here: bool = False  # only decisions whose scope holds a device on site now (rules.holds_device_q: Needs attention's review counts)
    why: str = ""  # slice 40: SUPPORT or ADVISORY (the decision's applies_to)

    def params(self) -> dict:
        found = {k: v for k, v in (("state", self.state), ("scope", self.scope), ("response", self.response), ("ref", self.ref),
                                   ("why", self.why)) if v}
        return {**found, "here": "1"} if self.here else found

    @property
    def any(self) -> bool:
        return bool(self.params())


def parse_filters(params) -> ListFilters:
    """The list's filters from the address: an unknown choice is dropped, never a 500; IT's exception number as typed (spaces collapsed,
    a NUL taken out), matched in any letter case."""
    def one_of(name, allowed):
        value = params.get(name, "")
        return value if value in allowed else ""

    ref = " ".join(str(params.get("ref", "")).replace("\x00", "").split())[:EXCEPTION_REFERENCE_MAX]
    return ListFilters(state=one_of("state", {v for v, _ in STATE_FILTERS}), scope=one_of("scope", {v for v, _ in SCOPE_FILTERS}),
                       response=one_of("response", set(DecisionResponse.values)), ref=ref, here=params.get("here") == "1",
                       why=one_of("why", set(WHY_APPLIES)))


def filtered(f: ListFilters, today: date):
    """The decisions the filters keep (positive conditions only), live first (by review day), then ended (newest first)."""
    qs = DeviceSecurityDecision.objects.select_related(*_RELATED)
    soon = today + timedelta(days=rules.REVIEW_SOON_DAYS)
    states = {LIVE: Q(ended_on__isnull=True), rules.IN_FORCE: Q(ended_on__isnull=True, review_by__gt=soon),
              rules.REVIEW_SOON: Q(ended_on__isnull=True, review_by__gte=today, review_by__lte=soon),
              rules.REVIEW_DUE: Q(ended_on__isnull=True, review_by__lt=today), rules.ENDED: Q(ended_on__isnull=False)}
    if f.state in states:
        qs = qs.filter(states[f.state])
    scopes = {rules.DEVICE_SCOPE: Q(asset__isnull=False), rules.RELEASE_SCOPE: Q(release__isnull=False),
              rules.MODEL_SCOPE: Q(asset__isnull=True, release__isnull=True)}
    if f.scope in scopes:
        qs = qs.filter(scopes[f.scope])
    if f.response:
        qs = qs.filter(response=f.response)
    if f.why:
        qs = qs.filter(applies_to=WHY_APPLIES[f.why])
    if f.ref:
        qs = qs.filter(exception_reference__icontains=f.ref)
    if f.here:
        qs = qs.filter(rules.holds_device_q())
    return qs.order_by(F("ended_on").desc(nulls_first=True), "review_by", "number")


def _row(d, today) -> dict:
    return {"d": d, "url": decision_url(d.number), "scope": rules.scope_words(d), "why": why_words(d), "response": d.get_response_display(),
            "review": rules.review_words(d, today), "state": _state(d, today), "accepted": AcceptedBy(d.accepted_by).label,
            "recorded_by": str(d.recorded_by) if d.recorded_by_id else ""}


def _attention(request, today) -> dict:
    """Needs attention (rules.needs_attention) with its links reversed: each heading's words, count (a link to the list it counts, where
    the list can show it), and rows (words, link, Record decision, End, the work order)."""
    data = rules.needs_attention(today, user=request.user)
    base = reverse("web:security_decisions")
    headings = []
    for h in data.headings:
        state = HEADING_STATES.get(h.key)
        rows = [{"words": r.words, "count": r.count, "url": link_url(r.link), "record_url": link_url(r.record), "end_url": link_url(r.end),
                 "wo_url": link_url(r.work_order), "wo_number": r.work_order.args[0] if r.work_order is not None else ""} for r in h.rows]
        headings.append({"key": h.key, "words": h.words, "count": h.count, "info": h.info, "rows": rows,
                         "url": f"{base}?{urlencode({'state': state, **HEADING_HERE.get(h.key, {})})}" if state else ""})
    return {"empty": data.empty, "empty_words": data.empty_words, "headings": headings}


def _list_context(request) -> dict:
    today = timezone.localdate()
    f = parse_filters(request.GET)
    page = Paginator(filtered(f, today), PAGE_SIZE).get_page(request.GET.get("page"))
    return {**_shell(request), "list_url": reverse("web:security_decisions"), "f": f,
            "options": {"states": STATE_FILTERS, "scopes": SCOPE_FILTERS, "responses": RESPONSE_FILTERS, "whys": WHY_FILTERS},
            "page": page, "rows": [_row(d, today) for d in page.object_list], "attention": _attention(request, today),
            "hicp": rules.HICP_DECISIONS, "hicp_advisories": hicp_second(rules.HICP_DECISIONS, rules.HICP_ADVISORY_DECISIONS),
            "can_decide": cyber_perms.can_decide(request.user)}


@web_view(MODULE, VIEW)
def decisions_tab(request):
    """The Decisions tab: Needs attention and the decisions (Device security View and Equipment View)."""
    _need_devices(request.user)
    ctx = _list_context(request)
    if is_partial(request, BODY_ID):
        return render(request, BODY, ctx)
    return render(request, PAGE, ctx)


LIST_COLUMNS = ["Number", "State", "Scope", "Reasons", "Decision", "Controls", "IT's reference for the controls", "Plan", "Accepted by",
                "IT exception number", "Recorded by", "Decided on", "Review by", "Ended", "Why", "Advisory"]


@web_view(MODULE, VIEW)
def decisions_csv(request):
    """The list (its filters, every row) as CSV: each decision's number, state, scope, reasons, decision, controls, plan, who accepted
    it, IT's numbers, who recorded it, its days, its end, in rules' words, and (slice 40) why it was recorded and its advisory's number."""
    _need_devices(request.user)
    today = timezone.localdate()
    fy = fiscal.start_month()
    rows = ((d.number, rules.DECISION_STATE_WORDS[rules.decision_state(d, today)], rules.scope_words(d), "; ".join(rules.reason_words(d)),
             d.get_response_display(), "; ".join(rules.control_words(d)), d.controls_reference, rules.plan_words(d, fy_start=fy),
             AcceptedBy(d.accepted_by).label, d.exception_reference, str(d.recorded_by) if d.recorded_by_id else "", d.decided_on,
             d.review_by, rules.end_words(d), why_words(d), d.advisory.number if d.advisory_id else "")
            for d in filtered(parse_filters(request.GET), today))
    return csv_response(f"cadence-security-decisions-{today:%Y-%m-%d}.csv", LIST_COLUMNS, rows)


# --- a decision's page -------------------------------------------------------------------------------------------------------------

def _device_row(dd, today, *, can_wo: bool) -> dict:
    """One device on site in the decision's scope: its tag, serial, model, department, connection, and release, and its response (its
    update lines, each work order's number a link with Work orders View, then this decision, its review due, or the decision that
    governs it instead, a link to it). Slice 40, an advisory decision's device: also its line under the advisory ("On 5.2 (demo), a
    release the advisory names"), and its updates and the manufacturer's patches recorded on that line (a hand row names no work order)."""
    a, s = dd.asset, dd.standing
    updates = [_linked(r.words, r.number, reverse("web:wo", args=[r.number]) if can_wo and r.number else "") for r in dd.updates]
    words = dd.words(today)
    head = "; ".join(r.words for r in dd.updates)
    tail = words[len(head):].lstrip("; ") if words.startswith(head) else ""
    other = dd.governing if dd.governing is not None and not dd.governed else None
    return {"a": a, "model": f"{a.device_model.manufacturer} {a.device_model.model}", "department": a.department.name,
            "connection": s.connection.words, "release": s.release_words, "updates": updates, "words": words, "governed": dd.governed,
            "line": dd.line.words if dd.line is not None else "",
            "tail": _linked(tail, other.number, decision_url(other.number)) if other is not None else _linked(tail, "", "")}


def _scope_kwargs(d) -> dict:
    """The decision's scope and need key as record_decision and refusal_for take them (slice 40: an advisory decision's advisory)."""
    keyed = {"applies_to": AppliesTo.ADVISORY, "advisory": d.advisory} if d.advisory_id else {}
    if d.asset_id is not None:
        return {"asset": d.asset, **keyed}
    if d.release_id is not None:
        return {"release": d.release, **keyed}
    return {"device_model": d.device_model, **keyed}


def _advisory_name(advisory) -> str:
    """The advisory's name (rules.advisory_name: two queries), "(entered in error since)" after it once it was (rules' words)."""
    name = rules.advisory_name(advisory)
    return f"{name}{rules.ENTERED_IN_ERROR_SINCE}" if advisory.removed_on is not None else name


def _page_context(request, d) -> dict:
    """The decision's page (and its print): everything in rules' words, its devices on site and Check lines read once (fixed queries
    however many devices), the actions for permissions.can_decide on a live one."""
    user = request.user
    today = timezone.localdate()
    fy = fiscal.start_month()
    devices = rules.decision_devices(d, today)
    checks = rules.decision_checks(d, today, devices=devices)
    kind = rules.scope_kind(d)
    live = d.ended_on is None
    may = cyber_perms.can_decide(user) and live
    # Review and renew only while its renewal would be taken (review 39 F15); else the refusal's words say why (skeptics S3: a fact to
    # record first, or nothing in its scope needing one, when End is what answers it)
    refusal = decisions.refusal_for(**_scope_kwargs(d), supersedes=d, today=today) if may else None
    renewable = may and refusal is None
    can_wo = user.has_level(Module.WORKORDERS, Level.VIEW)
    url = decision_url(d.number)
    can_history = history_tabs.allowed(user, AREA)
    hist = history_tabs.context(request, d, AREA, url, tab=False) if can_history and request.GET.get("history") else {}
    try:
        later = d.superseded_by  # read with the decision (select_related)
    except DeviceSecurityDecision.DoesNotExist:
        later = None
    rows = [_device_row(dd, today, can_wo=can_wo) for dd in devices]
    advisory = d.advisory if d.advisory_id else None
    return {
        "d": d, "sd_url": url, "sentence": rules.decision_words(d, today, fy_start=fy), "sd_state": _state(d, today),
        "scope_label": rules.SCOPE_LABELS[kind], "scope_words": rules.scope_words(d, len(devices) if kind != rules.DEVICE_SCOPE else None),
        "applies": _linked(why_words(d), advisory.number, advisory_url(advisory.number)) if advisory else _linked(why_words(d), "", ""),
        "reasons": rules.reason_words(d),
        # slice 40: an advisory decision names its advisory in place of reasons, and carries HICP's sentence for advisories
        "sd_advisory": advisory, "advisory_name": _advisory_name(advisory) if advisory else "",
        "advisory_url": advisory_url(advisory.number) if advisory else "", "sd_hicp": rules.HICP_ADVISORY_DECISIONS if advisory else "",
        "now_lines": rules.holding_lines(d, devices),  # the devices it governs, never one a more specific decision governs (skeptics T0)
        "controls": rules.control_words(d), "plan_words": rules.plan_words(d, fy_start=fy), "who": rules.who_words(d),
        "verb": rules.who_verb(d),
        "accepted": AcceptedBy(d.accepted_by).label, "review": rules.review_words(d, today), "end": rules.end_words(d),
        "earlier": d.supersedes, "later": later, "sd_devices": rows, "governed_count": sum(1 for r in rows if r["governed"]),
        "checks": [{"words": c.words, "info": c.info, "end": may and c.action == "end"} for c in checks],
        "can_decide": may, "renew_url": record_url(supersedes=d.number) if renewable else "",
        "renew_refusal": refusal.words if refusal is not None else "", "end_answers": refusal is not None and refusal.ends_it,
        "end_url": reverse("web:security_decision_end", args=[d.number]) if may else "",
        "in_error_url": reverse("web:security_decision_in_error", args=[d.number]) if may else "",
        "print_url": people.with_facility(reverse("web:security_decision_print", args=[d.number]), request.tenant),
        "csv_url": reverse("web:security_decision_devices_csv", args=[d.number]),
        "can_view_asset": True,  # can_view_devices: every reader here has Equipment View
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist,
    }


@web_view(MODULE, VIEW)
def decision(request, number):
    """A decision's page: into #drawer by HTMX, or the Decisions tab with it open when its address is opened directly. Its History
    section and Show older answer here (?history=1, ?older=)."""
    _need_devices(request.user)
    d = _decision(number)
    if history_tabs.asked(request, tab=False):
        history_tabs.require(request.user, AREA)
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, d, AREA, decision_url(d.number), tab=False)
    ctx = _page_context(request, d)
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, DRAWER, ctx)
    return render(request, PAGE, {**_list_context(request), **ctx, "drawer_template": DRAWER})


@web_view(MODULE, VIEW)
def decision_print(request, number):
    """The decision for paper or a PDF (print_base, a new tab): the facility, the day printed, its number and sentence, scope, reasons
    with their days, controls, plan, who accepted it, IT's exception number, who recorded it, the days, Check lines, the devices on site in
    its scope, the system-of-record line, and HICP's recommendation."""
    _need_devices(request.user)
    d = _decision(number)
    ctx = _page_context(request, d)
    reference = d.exception_reference
    closing = SYSTEM_OF_RECORD.format(reference=reference) if reference else NO_EXCEPTION
    hicp = rules.HICP_ADVISORY_DECISIONS if d.advisory_id else rules.HICP_DECISIONS  # by key (slice 40)
    return render(request, PRINT, {**ctx, "closing": closing, "hicp": hicp, "printed_on": timezone.localdate()})


DEVICE_COLUMNS = ["Tag", "Serial", "Manufacturer", "Model", "Department", "Connection", "Release", "Response"]


@web_view(MODULE, VIEW)
def decision_devices_csv(request, number):
    """The devices on site in the decision's scope as CSV: the page's columns (the response in rules' words), nothing more."""
    _need_devices(request.user)
    d = _decision(number)
    today = timezone.localdate()
    rows = ((dd.asset.tag, dd.asset.serial, dd.asset.device_model.manufacturer, dd.asset.device_model.model, dd.asset.department.name,
             dd.standing.connection.words, dd.standing.release_words, dd.words(today)) for dd in rules.decision_devices(d, today))
    return csv_response(f"cadence-security-decision-{d.number}-devices-{today:%Y-%m-%d}.csv", DEVICE_COLUMNS, rows)


# --- answers -------------------------------------------------------------------------------------------------------------------------

def _answered(response, message: str):
    trigger_client_event(response, CHANGED, {})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _decision_saved(request, number: str, message: str):
    """Saved: the decision's page into #drawer, freshly read, the lists refreshed, the toast, then the modal closes after settle (closing
    it first would detach the form that sent this request and cancel the swap)."""
    return _answered(retarget(render(request, DRAWER, _page_context(request, _decision(number))), "#drawer"), message)


def _device_saved(request, asset, message: str):
    """Saved from the device drawer's Security tab: the device's drawer on that tab, freshly read."""
    request.GET = request.GET.copy()
    request.GET["tab"] = "security"
    return _answered(retarget(render(request, DEVICE_DRAWER, asset_drawer_context(request, get_asset(request, asset.tag))), "#drawer"),
                     message)


def _panel_saved(request, dm, message: str):
    """Saved from a release row: the model's panel into #drawer with the filters it was opened with (they are in this address)."""
    return _answered(retarget(render(request, views_cybersecurity.PANEL, views_cybersecurity._panel_context(request, dm)), "#drawer"),
                     message)


# --- Record decision ---------------------------------------------------------------------------------------------------------------

@dataclass
class Scope:
    """What Record decision is for, from its address: one device, a model's devices of ours on one release, or every device of ours of a
    model; `old` the decision superseded (Review and renew); `origin` where the save answers; `advisory` (slice 40) the advisory whose
    devices it is for (None: devices past support), its need key."""

    kind: str
    model: object
    asset: object = None
    release: object = None
    old: object = None
    origin: str = ""
    advisory: object = None

    def kwargs(self) -> dict:
        """The scope and its need key as record_decision, refusal_for, and would_govern take them (support's: the scope alone)."""
        keyed = {"applies_to": AppliesTo.ADVISORY, "advisory": self.advisory} if self.advisory is not None else {}
        if self.kind == rules.DEVICE_SCOPE:
            return {"asset": self.asset, **keyed}
        if self.kind == rules.RELEASE_SCOPE:
            return {"release": self.release, **keyed}
        return {"device_model": self.model, **keyed}

    @property
    def key(self) -> tuple:
        """The need key (rules.key_of's shape)."""
        return rules.advisory_key(self.advisory) if self.advisory is not None else rules.SUPPORT_KEY

    def words(self, count: int | None = None) -> str:
        """The scope in rules' words (rules.scope_words of the decision it would be; an advisory's after its number: "AD-26-0003 ·
        Alaris 8015 PCU on 5.2 (demo)")."""
        shape = DeviceSecurityDecision(asset=self.asset, device_model=None if self.asset is not None else self.model, release=self.release,
                                       advisory=self.advisory)
        return rules.scope_words(shape, count, advisory=True)

    @property
    def temporary(self) -> bool:
        return self.kind == rules.DEVICE_SCOPE and self.asset.ownership != Ownership.OWNED

    def devices(self):
        """The devices on site the scope holds (rules.scope_devices)."""
        if self.kind == rules.DEVICE_SCOPE:
            return rules.scope_devices(asset=self.asset)
        return rules.scope_devices(device_model=self.model, release=self.release)

    def home(self, request) -> str:
        """Where the modal's address opened directly goes: the advisory's page (from=advisory), the device's Security tab, the model's
        panel, or the decision's page."""
        if self.origin == "advisory" and self.advisory is not None:
            return advisory_url(self.advisory.number)
        if self.old is not None and self.origin != "device":
            return decision_url(self.old.number)
        if self.kind == rules.DEVICE_SCOPE:
            return reverse("web:asset", args=[self.asset.tag]) + "?tab=security"
        query = _carried(request)
        return reverse("web:cybersecurity_model", args=[self.model.pk]) + (f"?{urlencode(query)}" if query else "")


SCOPE_PARAMS = ("asset", "release", "model", "supersedes", "advisory", "from")  # the modal's own: its scope and where its save answers


def _carried(request) -> dict:
    """The rest of the modal's address (from a release row: the panel's filters), kept by every address it leads to (the panel opened
    directly, Review and renew), so a save from=panel answers with the panel as it was opened."""
    return {k: v for k, v in request.GET.items() if k not in SCOPE_PARAMS}


def _scope(request) -> Scope:
    """The scope from the address (another facility's device, release, model, decision, or advisory, or none, is a 404). Slice 40, in
    this order: Review and renew (`supersedes`) reads the advisory from the decision renewed (a different `advisory=` beside it is a
    404); else `advisory=<number>` with the scope (asset, release, or model) records a decision for that advisory's devices; the advisory
    alone is a 404."""
    params = request.GET
    origin = params.get("from", "") if params.get("from", "") in ORIGINS else ""
    named = str(params.get("advisory", "")).strip()
    number = str(params.get("supersedes", "")).strip()
    if number:
        old = _decision(number)
        given = old.advisory if old.advisory_id else None
        if named and (given is None or given.number.casefold() != named.casefold()):
            raise Http404
        model = old.asset.device_model if old.asset_id else old.device_model
        return Scope(rules.scope_kind(old), model=model, asset=old.asset, release=old.release, old=old, origin=origin, advisory=given)
    advisory = _advisory(named) if named else None
    tag = str(params.get("asset", "")).strip()
    if tag:
        asset = Asset.objects.select_related("device_model", "department").filter(tag__iexact=tag).first() if "\x00" not in tag else None
        if asset is None:
            raise Http404
        return Scope(rules.DEVICE_SCOPE, model=asset.device_model, asset=asset, origin=origin, advisory=advisory)
    release_id = parse_uuid(params.get("release", ""))
    if release_id is not None:
        release = (SoftwareRelease.objects.select_related("device_model").filter(pk=release_id, removed_on__isnull=True)
                   .exclude(version_key="").first())
        if release is None:
            raise Http404
        return Scope(rules.RELEASE_SCOPE, model=release.device_model, release=release, origin=origin, advisory=advisory)
    model_id = parse_uuid(params.get("model", ""))
    model = DeviceModel.objects.filter(pk=model_id).first() if model_id is not None else None
    if model is None:
        raise Http404
    return Scope(rules.MODEL_SCOPE, model=model, origin=origin, advisory=advisory)


@dataclass(frozen=True)
class Now:
    """What holds now for the scope: its devices on site (`read`: their standings; the scope's words count them), their needs, and, of
    the devices the decision would govern (decisions.would_govern: never one a more specific live decision governs), the decidable
    needs' reasons (kinds, in REASONS' order) and their standing lines. Slice 40, an advisory's: no needs or reasons; `governed` the
    devices the decision would govern, `needing` their lines under the advisory that need a response now, and `lines` those lines'
    words."""

    read: dict
    needs: dict
    reasons: list
    lines: list
    today: date
    governed: tuple = ()
    needing: tuple = ()

    @property
    def decidable(self) -> bool:
        return bool(self.reasons) or bool(self.needing)


def _now(scope: Scope, today: date) -> Now:
    """rules.standings and rules.support_need over the scope's devices on site; the reasons and lines from the devices the decision would
    govern recorded now (decisions.would_govern, in place of the one renewed), never a device a more specific live decision governs, as
    record_decision reads them (skeptics T0). Fixed queries. An advisory's scope: _advisory_now."""
    if scope.advisory is not None:
        return _advisory_now(scope, today)
    read = rules.standings(scope.devices(), today)
    needs = {pk: rules.support_need(s, today) for pk, s in read.items()}
    governed = decisions.would_govern([s.asset for s in read.values()], **scope.kwargs(), old=scope.old)
    decidable = [needs[a.pk] for a in governed if needs[a.pk] is not None and needs[a.pk].decidable]
    reasons = [kind for kind in rules.REASONS if any(kind in n.reasons for n in decidable)]
    return Now(read=read, needs=needs, reasons=reasons, lines=_unique(line for n in decidable for line in n.lines), today=today)


def _advisory_now(scope: Scope, today: date) -> Now:
    """Slice 40, a decision for the advisory's devices: rules.standings over the scope's devices on site, the devices the decision would
    govern recorded now (decisions.would_govern over the advisory's need key, in place of the one renewed), and their lines under the
    advisory's row for the scope's model that need a response now (rules.lines_for, line_updates, needs_now: the rule decisions' scope
    read takes), as record_decision reads them. Fixed queries."""
    read = rules.standings(scope.devices(), today)
    members = [s.asset for s in read.values()]
    governed = decisions.would_govern(members, **scope.kwargs(), old=scope.old)
    rows = [r for r in rules.rows_of([scope.advisory]) if r.device_model_id == scope.model.pk]
    lines = {pk: line for (pk, _advisory), line in rules.lines_for(members, read, rows).items()}
    updates = rules.line_updates(lines.values())
    needing = tuple(lines[a.pk] for a in governed if rules.needs_now(lines.get(a.pk), updates))
    return Now(read=read, needs={}, reasons=[], lines=_unique(line.words for line in needing), today=today, governed=tuple(governed),
               needing=needing)


def _prefill_source(scope: Scope):
    """The live decision of exactly this scope of another need key (build40 section 7.5): a support decision first, else the most
    recently decided advisory decision of another advisory; past its review or not; or None. Read without a lock (a GET door). One
    query."""
    rows = DeviceSecurityDecision.objects.filter(ended_on__isnull=True)
    if scope.kind == rules.DEVICE_SCOPE:
        rows = rows.filter(asset_id=scope.asset.pk)
    elif scope.kind == rules.RELEASE_SCOPE:
        rows = rows.filter(release_id=scope.release.pk)
    else:
        rows = rows.filter(asset__isnull=True, release__isnull=True, device_model_id=scope.model.pk)
    found = [d for d in rows if rules.key_of(d) != scope.key]
    support = [d for d in found if rules.key_of(d) == rules.SUPPORT_KEY]
    if support:
        return support[0]
    return max(found, key=lambda d: (d.decided_on, d.number)) if found else None


def _network(scope: Scope, now: Now) -> str:
    """Why an advisory decision for the scope could not name network_removed (decisions.network_refusal over the devices it would govern:
    each needs its own fact "Not connected here"), said beside it before it is tried; "" for a support scope, or when it could."""
    if scope.advisory is None or not now.governed:
        return ""
    return decisions.network_refusal(now.governed, now.today)


def _blocker(request, scope: Scope, now: Now) -> tuple[str, str, str]:
    """(why a decision cannot be recorded for the scope now, or ""; Review and renew's address for the live decision in force for it,
    or ""; End's address for the live decision being renewed when nothing in its scope needs one now (review 39 F15: renewing it is
    refused, ending it is what is left; never for a fact not recorded, which the words say to record first: Refusal.ends_it, skeptics
    S3), or ""), said before it is tried: decisions.refusal_for, the service's own checks in its order and words. Review and renew's
    address keeps where the save answers and the rest of this address (_carried: a release row's panel filters, so the renewal saved
    answers with the panel as it was opened)."""
    refusal = decisions.refusal_for(**scope.kwargs(), supersedes=scope.old, today=now.today)
    if refusal is None:
        return "", "", ""
    if refusal.live is not None:
        return refusal.words, record_url(supersedes=refusal.live.number, **{"from": scope.origin}, **_carried(request)), ""
    old = scope.old
    ends = old is not None and old.ended_on is None and refusal.ends_it
    return refusal.words, "", reverse("web:security_decision_end", args=[old.number]) if ends else ""


def owners(request) -> list:
    """The people who may own a plan: active accounts of this facility, never scoped, with Device security View (decisions' rule for
    plan_owner), by name. One query with their roles' levels."""
    users = (User.objects.filter(tenant=request.tenant, is_active=True).select_related("role").prefetch_related("role__permissions")
             .order_by("first_name", "last_name", "email", "pk"))
    return [u for u in users if cyber_perms.can_view(u)]


def _day(text: str):
    try:
        return date.fromisoformat(text.strip()) if text and text.strip() else None
    except ValueError:
        return None


def _passed(kwargs: dict, today: date, scope: Scope):
    """The review day the decision would read, when it has already passed (after the day decided: an exception that lapsed): the day
    typed, or the default (rules.review_default) for a blank one; else None. Whether the service would take the decision is _dry_run's."""
    decided = _day(kwargs["decided_on"]) or today
    review = _day(kwargs["review_by"])
    if review is None and not kwargs["review_by"] and kwargs["response"] in DecisionResponse.values and decided <= today:
        review = rules.review_default(kwargs["response"], decided, asset=scope.asset)
    return review if review is not None and decided < review < today else None


class _DryRun(Exception):
    """Raised at the end of _dry_run's transaction to roll back the decision it recorded."""


def _dry_run(record: dict) -> None:
    """decisions.record_decision with `record`, rolled back (apps.imports' check precedent): its refusal (a ValidationError, every field
    problem at once, or the scope's under its locks) raised as the save would raise it, nothing kept when it would be recorded (the
    decision, its number, its history). Record decision says a review day already passed only for a decision the service would take, so
    the second save the note asks for records it."""
    try:
        with transaction.atomic():
            decisions.record_decision(**record)
            raise _DryRun
    except _DryRun:
        pass


def _record_modal(request, scope: Scope, now: Now, form, *, blocker: str = "", renew: str = "", end: str = "", note: str = "",
                  prefill: str = "", network: str = ""):
    """The modal, keyed (slice 40): a support scope's hint and reasons; an advisory scope's hint, HICP's sentence for advisories, the
    advisory's name and the lines that need a response now, the prefill's note, and network_removed's refusal beside it."""
    if form is not None and form.is_bound:
        form.focus_first_error()
    advisory = scope.advisory
    return render(request, RECORD, {"scope": scope, "scope_words": scope.words(len(now.read) if scope.kind != rules.DEVICE_SCOPE else None),
                                    "now": now, "reason_words": [rules.REASONS[k] for k in now.reasons], "form": form,
                                    "blocker": blocker, "renew_url": renew, "end_url": end, "note": note, "action": request.get_full_path(),
                                    "hint": rules.NETWORK_REMOVED_HINT if advisory is None else "",
                                    "advisory_hint": rules.ADVISORY_DECISION_HINT.format(number=advisory.number) if advisory else "",
                                    "hicp": rules.HICP_ADVISORY_DECISIONS if advisory else "",
                                    "advisory_name": _advisory_name(advisory) if advisory is not None and form is not None else "",
                                    "advisory_url": advisory_url(advisory.number) if advisory else "",
                                    "prefill_note": prefill, "network_note": network, "network_slug": rules.NETWORK_REMOVED})


@require_http_methods(["GET", "POST"])
@web_view(MODULE, APPROVE)
def decision_new(request):
    """Record decision (GET the modal, POST it): permissions.can_decide; decisions.record_decision for the scope in the address, the
    reasons that hold now (sent with the form, so a change meanwhile is refused under them), and the decision superseded (Review and
    renew). Saved, it answers where it was opened from (`from`), else with the new decision's page. Slice 40, an advisory's scope
    (`advisory=`): its own need key throughout; a new decision prefilled from the scope's live decision of another need key (its note)
    when that copies anything (prefill_copies), else, when every device it would govern is recorded "Not connected here", Keep in use
    with controls, taken off the network."""
    _need_decide(request.user)
    scope = _scope(request)
    if request.method != "POST" and not request.htmx:
        return redirect(scope.home(request))
    today = timezone.localdate()
    now = _now(scope, today)
    people_ = owners(request)
    options = {"owners": people_, "today": today, "temporary": scope.temporary, "asset": scope.asset, "advisory": scope.advisory}
    if request.method != "POST":
        blocker, renew, end = _blocker(request, scope, now)
        if blocker:
            return _record_modal(request, scope, now, None, blocker=blocker, renew=renew, end=end)
        network = _network(scope, now)
        initial, prefill = {}, ""
        if scope.old is not None:
            initial = renew_initial(scope.old, today)
        elif scope.advisory is not None:
            source = _prefill_source(scope)
            copied = prefill_initial(source, today, scope.key) if source is not None else {}
            if prefill_copies(copied):
                initial, prefill = copied, prefill_note(source, today)
            # A source that copies nothing (another advisory's "not in use here": no controls, no plan) prefills nothing: no note, and
            # the scope's own starting values (review 40 #11)
            elif not network:  # every device it would govern recorded "Not connected here" (critique40_practice S1)
                initial = {"response": DecisionResponse.KEEP_WITH_CONTROLS, "controls": [rules.NETWORK_REMOVED]}
        return _record_modal(request, scope, now, RecordDecisionForm(initial=initial, **options), prefill=prefill, network=network)
    form = RecordDecisionForm(request.POST, **options)
    saved = None
    if form.is_valid():
        kwargs = form.service_kwargs()
        record = {**scope.kwargs(), "reasons": request.POST.getlist("reasons"), "by": request.user, "today": today, **kwargs}
        passed = _passed(kwargs, today, scope)
        try:
            if passed is not None and form.cleaned_data.get("review_passed") != passed.isoformat():
                # Said once before it is saved, and only when the service would take it: else its refusals now, never the note first.
                # The decision superseded by its id: the dry run never touches this request's copy (record_decision syncs an instance).
                _dry_run({**record, "supersedes": scope.old.pk if scope.old is not None else None})
                data = request.POST.copy()
                data["review_passed"] = passed.isoformat()
                form = RecordDecisionForm(data, **options)
                form.is_valid()
                note = decisions.REVIEW_PASSED.format(day=rules.day_words(passed))
                return _record_modal(request, scope, now, form, note=note, network=_network(scope, now))
            saved = decisions.record_decision(**record, supersedes=scope.old)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if saved is None:
        return _record_modal(request, scope, now, form, network=_network(scope, now))
    words = rules.scope_words(saved, advisory=True)  # an advisory decision's after its advisory's number
    message = (RENEWED.format(number=saved.number, scope=words, old=scope.old.number) if scope.old is not None
               else RECORDED.format(number=saved.number, scope=words))
    if scope.origin == "device" and scope.kind == rules.DEVICE_SCOPE:
        return _device_saved(request, scope.asset, message)
    if scope.origin == "panel":
        return _panel_saved(request, scope.model, message)
    if scope.origin == "advisory" and scope.advisory is not None:
        from . import views_cyber_advisories  # imported here: that module reads this one's helpers

        return views_cyber_advisories.advisory_saved(request, scope.advisory, message)
    return _decision_saved(request, saved.number, message)


# --- End, Entered in error ---------------------------------------------------------------------------------------------------------

def _ending(request, number, *, kind: str):
    """End (decisions.end_decision) or Entered in error (decisions.mark_entered_in_error): GET the modal saying what it does (or why it
    cannot: already ended), POST it. Saved: the decision's page, the toast."""
    _need_decide(request.user)
    d = _decision(number)
    if request.method != "POST" and not request.htmx:
        return redirect(decision_url(d.number))
    ended = (decisions.ALREADY_ENDED.format(number=d.number, day=rules.day_words(d.ended_on)) if d.ended_on is not None else "")
    words = (END_WORDS if kind == "end" else IN_ERROR_WORDS).format(number=d.number)
    action = reverse("web:security_decision_end" if kind == "end" else "web:security_decision_in_error", args=[d.number])
    ctx = {"d": d, "kind": kind, "words": words, "action": action, "scope_words": rules.scope_words(d)}
    if request.method != "POST":
        return render(request, END, {**ctx, "blocker": ended, "form": EndForm()})
    form = EndForm(request.POST)
    form.is_valid()
    today = timezone.localdate()
    try:
        if kind == "end":
            decisions.end_decision(d, by=request.user, today=today)
        else:
            decisions.mark_entered_in_error(d, by=request.user, today=today)
    except (ValidationError, PermissionDenied) as e:
        form.add_service_errors(e)
        return render(request, END, {**ctx, "blocker": "", "form": form})
    return _decision_saved(request, d.number, (ENDED if kind == "end" else IN_ERROR).format(number=d.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, APPROVE)
def decision_end(request, number):
    """End: no longer needed (its devices updated, replaced, retired, or returned, or each under a decision of its own).
    permissions.can_decide."""
    return _ending(request, number, kind="end")


@require_http_methods(["GET", "POST"])
@web_view(MODULE, APPROVE)
def decision_in_error(request, number):
    """Entered in error. permissions.can_decide."""
    return _ending(request, number, kind="in_error")
