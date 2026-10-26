"""
The Device security screen's Advisories tab (slice 40, beyond the mock; build40 sections 7.1-7.4): Needs attention for advisories and the
list (with its CSV), an advisory's page (drawer partial or full page, as a decision's: views_cyber_decisions), its devices' CSV, and the
modals: Record advisory (CVE overlaps offered before saving), Edit advisory, Add source / Edit source / Record revision / Remove source,
Set models (with "Add a version not on file", which calls services.add_release and re-renders the form from the POST), Remove model,
Confirm models, Not applicable here, Close, Reopen, Entered in error. Views parse input (forms_cyber_advisories.py), call
apps.cybersecurity.advisories, and answer with the page and a toast; every word about an advisory, a line, a response, a status, or a
Check line is apps.cybersecurity.rules' or advisories' (never composed here from the fields). HHS 405(d) HICP 2023 recommends taking in
security advisories (rules.HICP_ADVISORIES, said as a recommendation): no rule requires any of it.

- The tab (`/security/advisories/`): HICP's sentence, Needs attention (rules.advisory_attention: each heading with its count and rows,
  links reversed from its data; Record decision and Open software update for those who may), and the advisories (Open by default; Not
  applicable here, Closed, or all; models to confirm; a CVE ID, a source's reference, or a number), each row's number opening its page
  in the drawer, with its status words and counts for its reader. Its body re-fetches itself on `cyber-changed`. Export carries the
  filters.
- An advisory's page (`/security/advisories/<number>/`, any letter case): its name and status words, received and recorded by, its CVE
  IDs and the other advisories sharing them, its Check lines, its sources (with their links, days, revision, and what changed), the
  models it names (which releases, what it offers, the counts; for a reader of devices each device's line and responses, those that
  need a response first, the rest behind Show), "Decisions for this advisory", the actions by level, and History (its sources', model
  rows', and release rows' changes too: history_tabs.RELATED). Its devices' CSV for a reader of devices.
- Per line: Record decision (device scope, can_decide) where it needs a response and nothing covers it, Review and renew for its own
  device decision past review, Open software update (slice 38's door, can_open_updates) only where one can open (a release or patches
  remedy, no update open on the device, no patches recorded or version to confirm: practice N3). Per model row: Set models, Remove
  model, Open updates to the fix (a release remedy), Record decision for a release it lists or every device of ours of the model, and
  "Record their releases" (slice 37's Set devices to this release, opened `from=advisory`) beside a count of devices with none
  recorded.

Every refusal a GET shows is read with apps.cybersecurity.advisories' lock-free readers (never a writer's helper: FOR UPDATE outside a
transaction is an error on PostgreSQL); every save calls the writer, which reads the same refusal again under its locks. Saves answer with
the advisory's page into #drawer, CHANGED, a toast, and the modal closed (advisory_saved: also the answer of Record decision, slice 38's
update doors, and Set devices to this release opened `from=advisory`; opened_with reads the advisory such a door names).

Who: Device security View reads the tab, the page, the list, and the CSV (the line kinds and update counts, the stored statuses); every
device line, response, decision-derived count or word, "Decisions for this advisory", and the devices CSV also need Equipment View
(permissions.can_view_devices). The writers' levels are advisories.py's (Edit records, sets and confirms models, marks not applicable,
reopens from not applicable; Close needs can_decide; reopening a closed one and Entered in error need Approve). Scoped users never.
"""
from dataclasses import dataclass
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import F
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.cybersecurity import advisories, rules
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import (
    AdvisoryChange,
    AdvisoryModel,
    AdvisoryRemedy,
    AdvisorySource,
    AdvisoryStatus,
    AdvisoryVersions,
    DeviceSecurityDecision,
    NotApplicableReason,
    SecurityAdvisory,
)
from apps.equipment.models import DeviceModel, Ownership

from . import history_tabs, views_cybersecurity
from .decorators import web_view
from .exports import csv_response
from .forms import parse_uuid
from .forms_cyber_advisories import (
    ConfirmForm,
    EditAdvisoryForm,
    ModelForm,
    NotApplicableForm,
    RecordAdvisoryForm,
    RevisionForm,
    SourceForm,
    model_initial,
)
from .htmx import PAGE_SIZE, is_partial, toast

MODULE = cyber_perms.MODULE
VIEW, EDIT, APPROVE = Level.VIEW, Level.EDIT, Level.APPROVE
AREA = "security_advisories"  # apps.core.history's area (Device security View)
PAGE = "web/cyber_advisories.html"
BODY = "web/_cyber_advisories.html"
BODY_ID = "cy-advisories"
DRAWER = "web/_cyber_advisory.html"
NEW = "web/_cyber_advisory_new.html"
EDIT_FORM = "web/_cyber_advisory_edit.html"
SOURCE = "web/_cyber_advisory_source.html"
REVISION = "web/_cyber_advisory_revision.html"
MODELS = "web/_cyber_advisory_models.html"
NOT_APPLICABLE = "web/_cyber_advisory_not_applicable.html"
CONFIRM = "web/_cyber_advisory_confirm.html"
CHANGED = views_cybersecurity.CHANGED  # the screen's lists (and this tab's body) re-fetch themselves on it
DEVICES_WORDS = "Device lines need Device security View and Equipment View."
ORIGIN = "advisory"  # `from=advisory`: Record decision, the update doors, and Set devices answer with the advisory's page (advisory_saved)

# The toasts (numbers and models only)
RECORDED = "{number} recorded."
SAVED = "{number} saved."
SOURCE_ADDED = "Source added to {number}."
SOURCE_SAVED = "Source saved on {number}."
SOURCE_REMOVED = "Source removed from {number}."
REVISION_RECORDED = "Revision recorded on {number}."
REVISION_TO_CONFIRM = "Revision recorded on {number}: confirm the models it names."
REVISION_REOPEN_TO_CONFIRM = "Revision recorded on {number}: reopen it to confirm the models it names."  # closed stays closed (its Check's words)
REVISION_REOPENED = "Revision recorded on {number}: it is open again, with the models it names to confirm."
MODEL_NAMED = "{number} names the {model}."
MODEL_SAVED = "{number}: the {model} saved."
MODEL_REMOVED = "{number} no longer names the {model}."
CONFIRMED = "{number}: models confirmed."
NOT_APPLICABLE_TOAST = "{number} marked not applicable here."
CLOSED = "{number} closed."
REOPENED = "{number} reopened."
IN_ERROR = "{number} marked entered in error."
VERSION_ADDED = "Version {version} added to the {model}'s releases."
CLEAR_TO_SAVE = "Clear this box to save the models."  # after add_release's refusal of a version Save sent (skeptic S1): the way to save
# The confirmations' words (what each does, before it is done)
CONFIRM_WORDS = ("Confirm that {number} names no other model of this facility's catalog than these: {models}. A revision that changes "
                 "what it names asks for this again.")
ALREADY_CONFIRMED = "{number}'s models were confirmed on {day}."
CLOSE_WORDS = ("Close {number}: each device it names on a release it names, or with no release recorded, has a response recorded now. "
               "A device it names later is listed under Needs attention again.")
REOPEN_WORDS = "Reopen {number}: the devices it names are listed again, and the models it names can be changed."
# Reopening keeps the confirmation as it is: the last sentence follows it (a revision or a new day received may have cleared it)
REOPEN_CONFIRMED = " Its models stay confirmed."
REOPEN_TO_CONFIRM = " Its models are then to confirm."
IN_ERROR_WORDS = ("Mark {number} entered in error: it stays on record, marked, with its history; the devices it names are no longer "
                  "listed under it, and its sources' references are free for another advisory.")
REMOVE_MODEL_WORDS = "{number} stops naming the {model}: the releases it lists for it go with it, and its devices are no longer listed under it."
REMOVE_MODEL_CONFIRMED = " The models it names then need confirming again."
REMOVE_SOURCE_WORDS = "Remove {source} from {number}. It stays in the advisory's history."
OVERLAP_WORDS = "{items} {verb} on {number} ({source})."
OVERLAP_HINT = ("The same issue can have several publications: add this one as a source of that advisory, or record it as its own (a "
                "platform bulletin of another manufacturer, for one).")
EDIT_OVERLAP_HINT = "Save again to keep them on this advisory too."
NONE_OPEN = "No advisory is open here."

# The list's filters, in words (the values are the models' own slugs)
OPEN, ALL = "", "all"
STATUS_FILTERS = [(OPEN, "Open"), (AdvisoryStatus.NOT_APPLICABLE.value, "Not applicable here"), (AdvisoryStatus.CLOSED.value, "Closed"),
                  (ALL, "All, entered in error too")]
# A heading's count links to the list it counts, where the list can show it
HEADING_FILTERS = {"to_confirm": {"to_confirm": "1"}, "changed": {"status": AdvisoryStatus.CLOSED.value, "to_confirm": "1"}}
UPDATE_KINDS = {rules.UPDATE_OPEN: rules.ANSWER_KINDS[rules.UPDATE_OPEN],
                rules.UPDATE_NOT_CONFIRMED: rules.ANSWER_KINDS[rules.UPDATE_NOT_CONFIRMED]}
# A reader of devices' counts of what answers each line, the update kinds apart (counted above for every reader)
RESPONSE_KINDS = {k: rules.ANSWER_KINDS[k] for k in (rules.NO_RESPONSE, rules.ANSWER_REVIEW_DUE, rules.PATCHES_OTHER_LIST, rules.ANSWER_DECIDED,
                                                       rules.PATCHES_RECORDED)}
OPEN_SHOWN = (rules.UPDATE_OPEN, rules.UPDATE_ELSEWHERE)  # an update open on the device, answering the line or not
NO_OPENER = rules.NOTHING_TO_OPEN  # answered by patches, or an update done to confirm: nothing to open (rules.nothing_to_open)
CVES_SHOWN = 3
WHOSE_WORDS = {Ownership.OWNED: "Ours"}
_DECISION_RELATED = ("asset", "device_model", "release")


# --- small parts -------------------------------------------------------------------------------------------------------------------

def _need_devices(user) -> None:
    if not cyber_perms.can_view_devices(user):
        raise PermissionDenied(DEVICES_WORDS)


def _need_edit(user) -> None:
    if not cyber_perms.can_edit(user):
        raise PermissionDenied(advisories.RECORD_PERMISSION)


def _advisory(number) -> SecurityAdvisory:
    """An advisory of this facility by its number, in any letter case (another facility's, or none, is a 404). Entered in error ones
    too: their page says so."""
    text = str(number or "").strip()
    found = (SecurityAdvisory.objects.select_related("recorded_by", "status_by", "models_confirmed_by", "removed_by")
             .filter(number__iexact=text).first() if text and "\x00" not in text else None)
    if found is None:
        raise Http404
    return found


def advisory_url(number: str) -> str:
    return reverse("web:security_advisory", args=[number])


def opened_with(request) -> SecurityAdvisory | None:
    """The advisory another screen's door was opened with (slice 38's Open software update and Open updates to this release, slice 37's
    Set devices to this release): `?advisory=<number>`, in any letter case, read from the address only (never posted, never carried
    forward) and without a lock; None when the address names none. An unknown number (another facility's, or none), an advisory entered
    in error, and `from=advisory` without a number are 404s. Whether the save answers with its page is answers_here's."""
    number = str(request.GET.get("advisory", "")).strip()
    if not number:
        if answers_here(request):
            raise Http404
        return None
    found = (SecurityAdvisory.objects.filter(number__iexact=number, removed_on__isnull=True).first()
             if "\x00" not in number else None)
    if found is None:
        raise Http404
    return found


def answers_here(request) -> bool:
    """`from=advisory`: a door opened from the advisory's page answers with it (advisory_saved), and its address opened directly shows
    the page."""
    return request.GET.get("from") == ORIGIN


def link_url(link) -> str:
    """A rules.Link reversed (its query appended), or "" for none."""
    if link is None:
        return ""
    url = reverse(link.name, args=list(link.args))
    return f"{url}?{urlencode([(k, str(v)) for k, v in link.query])}" if link.query else url


def _record_url(**query) -> str:
    """Record decision's address for an advisory need (advisory=, and asset=, release=, model=, or supersedes=), answering here."""
    query = {k: str(v) for k, v in query.items() if v not in (None, "")}
    return f"{reverse('web:security_decision_new')}?{urlencode({**query, 'from': ORIGIN})}"


def _from_here(url: str) -> str:
    """A Record decision link from Needs attention, answering with the advisory's page."""
    if not url or "from=" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode({'from': ORIGIN})}"


def _door(name: str, args, advisory) -> str:
    """Slice 38's update doors and slice 37's Set devices to this release opened from the advisory (`?advisory=<number>&from=advisory`):
    they answer with its page."""
    return f"{reverse(name, args=args)}?{urlencode({'advisory': advisory.number, 'from': ORIGIN})}"


def _linked(words: str, number: str, url: str) -> dict:
    """A line split around the number it names, so the number can be a link: {"before", "number", "after", "url"}."""
    if number and url and number in words:
        before, after = words.split(number, 1)
        return {"before": before, "number": number, "after": after, "url": url}
    return {"before": words, "number": "", "after": "", "url": ""}


def _counted(counts, labels: dict) -> list:
    """[(label, n)] for the kinds counted, in the labels' order."""
    if not counts:
        return []
    return [(label, counts.get(key, 0)) for key, label in labels.items() if counts.get(key, 0)]


def _cves_short(cves) -> str:
    """The first three CVE IDs, then "and N more"; or rules' "No CVE IDs recorded"."""
    cves = list(cves or ())
    if not cves:
        return rules.NO_CVES_WORDS
    more = len(cves) - CVES_SHOWN
    return ", ".join(cves[:CVES_SHOWN]) + (f" and {more} more" if more > 0 else "")


def _tone(a) -> str:
    if a.removed_on is not None or a.status != AdvisoryStatus.OPEN:
        return "neutral"
    return "warn" if a.models_confirmed_on is None else "info"


def _shell(request) -> dict:
    user = request.user
    return {"nav_active": "cybersecurity", "cyber_tab": "advisories", "can_devices": cyber_perms.can_view_devices(user),
            "can_edit": cyber_perms.can_edit(user), "can_edit_devices": cyber_perms.can_edit_devices(user)}


def _whose(asset) -> str:
    return WHOSE_WORDS.get(asset.ownership, asset.get_ownership_display())


def _names(items) -> dict:
    """{advisory id: rules.advisory_name} for `items`: their model rows and sources read once (three queries)."""
    if not items:
        return {}
    rows_by: dict = {}
    for r in rules.rows_of(items):
        rows_by.setdefault(r.advisory_id, []).append(r)
    sources_by: dict = {}
    for s in AdvisorySource.objects.filter(advisory_id__in=[a.pk for a in items]).order_by("created_at", "pk"):
        sources_by.setdefault(s.advisory_id, []).append(s)
    return {a.pk: rules.advisory_name(a, rows=rows_by.get(a.pk, []), sources=sources_by.get(a.pk, [])) for a in items}


# --- the Advisories tab --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ListFilters:
    status: str = OPEN
    to_confirm: bool = False
    q: str = ""

    def params(self) -> dict:
        found = {k: v for k, v in (("status", self.status), ("q", self.q)) if v}
        return {**found, "to_confirm": "1"} if self.to_confirm else found

    @property
    def any(self) -> bool:
        return bool(self.params())


def parse_filters(params) -> ListFilters:
    """The list's filters from the address: an unknown status is dropped (Open), never a 500; the search as typed (spaces collapsed, a
    NUL taken out)."""
    status = params.get("status", "")
    q = " ".join(str(params.get("q", "")).replace("\x00", "").split())[:80]
    return ListFilters(status=status if status in {v for v, _ in STATUS_FILTERS} else OPEN, to_confirm=params.get("to_confirm") == "1",
                       q=q)


def _matching(q: str) -> list:
    """The advisories a search finds, matched in Python over one query (never a JSON lookup: SQLite has none): a CVE ID (exact, in any
    letter case), a live source's reference (in any letter case), or the advisory's number."""
    cve, key = q.upper(), q.casefold()
    found = set()
    rows = AdvisorySource.objects.values_list("advisory_id", "advisory__number", "advisory__cves", "reference_key", "removed_on")
    for advisory_id, number, cves, ref_key, removed_on in rows:
        if cve in (cves or ()) or number.upper() == cve or (removed_on is None and ref_key and ref_key == key):
            found.add(advisory_id)
    return list(found)


def filtered(f: ListFilters):
    """The advisories the filters keep (positive conditions only), newest received first."""
    qs = SecurityAdvisory.objects.select_related("status_by")
    if f.status in (AdvisoryStatus.NOT_APPLICABLE, AdvisoryStatus.CLOSED):
        qs = qs.filter(status=f.status, removed_on__isnull=True)
    elif f.status != ALL:
        qs = qs.filter(status=AdvisoryStatus.OPEN, removed_on__isnull=True)
    if f.to_confirm:
        qs = qs.filter(models_confirmed_on__isnull=True, removed_on__isnull=True)
    if f.q:
        qs = qs.filter(pk__in=_matching(f.q))
    return qs.order_by("-received_on", "-number")


def _row(a, counts, name: str) -> dict:
    """A row of the list. An advisory entered in error reads no count (rules.advisory_counts): `counted` False, and its count cells say
    nothing about the devices on site (never "None on site")."""
    return {"a": a, "url": advisory_url(a.number), "name": name, "words": counts.words, "tone": _tone(a), "cves": _cves_short(a.cves),
            "counted": a.removed_on is None,
            "lines": _counted(counts.lines, rules.LINE_KINDS), "updates": _counted(counts.updates, UPDATE_KINDS),
            "answers": _counted(counts.answers, RESPONSE_KINDS) if counts.answers is not None else None}


def _attention(request, today) -> dict:
    """Needs attention (rules.advisory_attention) with its links reversed: each heading's words, count (a link to the list it counts,
    where the list can show it), and rows (words, link: the advisory's page or a device's Security tab into the drawer, the Decisions
    tab's list as a page; Record decision answering here, Open software update, the work order)."""
    data = rules.advisory_attention(today, user=request.user)
    base = reverse("web:security_advisories")
    headings = []
    for h in data.headings:
        rows = [{"words": r.words, "count": r.count, "url": link_url(r.link),
                 "page": r.link is not None and r.link.name == rules.URLS["decisions"],
                 "record_url": _from_here(link_url(r.record)), "open_url": link_url(r.open_update), "wo_url": link_url(r.work_order),
                 "wo_number": r.work_order.args[0] if r.work_order is not None else ""} for r in h.rows]
        query = HEADING_FILTERS.get(h.key)
        headings.append({"key": h.key, "words": h.words, "count": h.count, "info": h.info, "rows": rows,
                         "url": f"{base}?{urlencode(query)}" if query else ""})
    return {"empty": data.empty, "empty_words": data.empty_words, "headings": headings}


def _list_context(request) -> dict:
    today = timezone.localdate()
    user = request.user
    f = parse_filters(request.GET)
    page = Paginator(filtered(f), PAGE_SIZE).get_page(request.GET.get("page"))
    items = list(page.object_list)
    devices_visible = cyber_perms.can_view_devices(user)
    counts = rules.advisory_counts(items, today=today, devices_visible=devices_visible) if items else {}
    names = _names(items)
    recorded = bool(items) or SecurityAdvisory.objects.exists()  # an empty page with advisories recorded: none open, never "none yet"
    return {**_shell(request), "list_url": reverse("web:security_advisories"), "f": f, "options": {"statuses": STATUS_FILTERS},
            "recorded": recorded, "none_open": NONE_OPEN,
            "page": page, "rows": [_row(a, counts[a.pk], names[a.pk]) for a in items], "attention": _attention(request, today),
            "hicp": rules.HICP_ADVISORIES, "empty_words": rules.ADVISORIES_EMPTY, "may_record": cyber_perms.can_edit(user)}


@web_view(MODULE, VIEW)
def advisories_tab(request):
    """The Advisories tab: Needs attention and the advisories (Device security View; device-derived parts for Equipment View too)."""
    ctx = _list_context(request)
    if is_partial(request, BODY_ID):
        return render(request, BODY, ctx)
    return render(request, PAGE, ctx)


LIST_COLUMNS = ["Number", "CVE IDs", "Sources", "Models", "Received", "Status", *rules.LINE_KINDS.values(), *UPDATE_KINDS.values()]
DEVICE_COLUMNS_EXTRA = list(RESPONSE_KINDS.values())  # for a reader of devices only


def _source_words(s) -> str:
    return f"{s.get_source_display()} {s.reference}".strip()


@web_view(MODULE, VIEW)
def advisories_csv(request):
    """The list (its filters, every row) as CSV: each advisory's number, CVE IDs, sources, models, day received, status words, and its
    counts by line kind and update; for a reader of devices also by what answers each line (tokens and choices only: no link). An
    advisory entered in error reads no count: its count cells blank, never 0."""
    today = timezone.localdate()
    devices_visible = cyber_perms.can_view_devices(request.user)
    items = list(filtered(parse_filters(request.GET)))
    counts = rules.advisory_counts(items, today=today, devices_visible=devices_visible) if items else {}
    models: dict = {}
    for r in rules.rows_of(items) if items else ():
        models.setdefault(r.advisory_id, []).append(f"{r.device_model.manufacturer} {r.device_model.model}")
    sources: dict = {}
    for s in AdvisorySource.objects.filter(advisory_id__in=[a.pk for a in items], removed_on__isnull=True).order_by("created_at", "pk"):
        sources.setdefault(s.advisory_id, []).append(_source_words(s))
    columns = LIST_COLUMNS + (DEVICE_COLUMNS_EXTRA if devices_visible else [])

    def rows():
        for a in items:
            c = counts[a.pk]
            figures = [*(c.lines.get(k, 0) for k in rules.LINE_KINDS), *(c.updates.get(k, 0) for k in UPDATE_KINDS)]
            if devices_visible:
                figures += [c.answers.get(k, 0) for k in RESPONSE_KINDS]
            if a.removed_on is not None:
                figures = [""] * len(figures)
            yield [a.number, ", ".join(a.cves or ()), "; ".join(sources.get(a.pk, [])), "; ".join(models.get(a.pk, [])), a.received_on,
                   c.words, *figures]

    return csv_response(f"cadence-security-advisories-{today:%Y-%m-%d}.csv", columns, rows())


# --- an advisory's page ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Reader:
    """Who reads the page, and what they may do on it."""

    devices: bool
    edit: bool
    decide: bool
    open_updates: bool
    edit_devices: bool
    work_orders: bool


def _reader(user, a) -> _Reader:
    live = a.removed_on is None
    needs = live and a.status in rules.HAS_NEEDS
    return _Reader(devices=cyber_perms.can_view_devices(user), edit=cyber_perms.can_edit(user) and live,
                   decide=cyber_perms.can_decide(user) and needs, open_updates=cyber_perms.can_open_updates(user) and needs,
                   edit_devices=cyber_perms.can_edit_devices(user) and needs, work_orders=user.has_level(Module.WORKORDERS, Level.VIEW))


def _response(r, who: _Reader) -> dict:
    """One response in rules' words: a work order's number a link with Work orders View; a decision's number (its page) first."""
    if r.decision is not None:
        url = reverse("web:security_decision", args=[r.decision.number])
        return {"before": "", "number": r.decision.number, "after": f": {r.words}", "url": url, "kind": r.kind}
    url = reverse("web:wo", args=[r.number]) if r.number and who.work_orders else ""
    return {**_linked(r.words, r.number, url), "kind": r.kind}


def _line_row(a, ln, answer, who: _Reader) -> dict:
    """A device under a model row: its tag, whose, serial, department and connection, the line's words, its responses (when it needs
    one), and what can be done from here: Record decision (device scope) where nothing covers it, Review and renew for its own device
    decision past review, Open software update where one can open (practice N3)."""
    asset, row = ln.asset, ln.row
    needs = ln.needs_response and answer is not None and bool(answer.kind)
    record = renew = opener = ""
    if needs and who.decide and answer.kind not in rules.ANSWERED:
        own = answer.decision is not None and answer.decision.asset_id == asset.pk
        if own:
            renew = _record_url(supersedes=answer.decision.number)
        else:
            record = _record_url(advisory=a.number, asset=asset.tag)
    if (needs and who.open_updates and row.remedy in (AdvisoryRemedy.RELEASE, AdvisoryRemedy.PATCHES) and not rules.nothing_to_open(answer)
            and not any(r.kind in OPEN_SHOWN for r in answer.responses) and not asset.incident_hold):
        opener = _door("web:cybersecurity_update_open", [asset.tag], a)
    return {"asset": asset, "words": ln.words, "place": ln.place, "kind": ln.kind, "needs": ln.needs_response,
            "responses": [_response(r, who) for r in answer.responses] if needs else [], "record_url": record, "renew_url": renew,
            "open_url": opener}


def _more_specific(decision, scope: str) -> bool:
    """Whether the governing decision is more specific than a release's (`scope` rules.RELEASE_SCOPE: its own device's) or the model's
    (rules.MODEL_SCOPE: its own or its release's): read from the decision's scope, never from the answer's kind."""
    if decision is None:
        return False
    return decision.asset_id is not None or (scope == rules.MODEL_SCOPE and decision.release_id is not None)


def _ours_waiting(entries, scope: str) -> bool:
    """A device of ours among `entries` (line, answer) that needs a response, has none that answers it (no decision covering it, no
    patches recorded: rules.ANSWERED), and that a decision for `scope` (rules.RELEASE_SCOPE or MODEL_SCOPE) would govern: never one a
    more specific decision governs, past its review or not (decisions._would_govern gives a release's decision none under its own device
    decision, the model's none under its own or its release's, and record_decision refuses a scope left with none). A decision of the
    scope itself past review leaves the button: its Record decision says to review and renew it."""
    return any(ln.asset.ownership == Ownership.OWNED and ln.needs_response and ans is not None and ans.kind
               and ans.kind not in rules.ANSWERED and not _more_specific(ans.decision, scope) for ln, ans in entries)


def _model_row(a, row, rc, lines, answers, who: _Reader) -> dict:
    """A model the advisory names: what it states (versions, the releases it lists, its remedy, the fix or patch list), the counts for
    every reader, and for a reader of devices its devices (those that need a response first) and the actions where they can act. `rc`
    its RowCounts (None for an advisory entered in error: it reads no count)."""
    dm = row.device_model
    entries = [(ln, answers.get((ln.asset.pk, row.pk))) for ln in lines]
    needing = [(ln, ans) for ln, ans in entries if ln.needs_response]
    rest = [(ln, ans) for ln, ans in entries if not ln.needs_response]
    is_open = a.status == AdvisoryStatus.OPEN and a.removed_on is None
    listed = getattr(row, "listed", [])
    releases = []
    for rel in listed:
        on_it = [(ln, ans) for ln, ans in needing if ln.release is not None and ln.release.pk == rel.pk]
        releases.append({"version": rel.version, "record_url": (_record_url(advisory=a.number, release=rel.pk)
                                                                if who.decide and _ours_waiting(on_it, rules.RELEASE_SCOPE) else "")})
    unopened = any(ans is not None and ans.kind and not rules.nothing_to_open(ans) and not any(r.kind in OPEN_SHOWN for r in ans.responses)
                   for _ln, ans in needing)
    counts_lines = rc.lines if rc is not None else {}
    found = {
        "row": row, "model": f"{dm.manufacturer} {dm.model}", "versions": row.get_versions_display(), "counted": rc is not None,
        "listed": releases, "remedy": row.get_remedy_display(),
        "fix": rules.release_name(row.fix_release) if row.fix_release_id else "", "patches": row.patches_reference,
        "lines": _counted(counts_lines, rules.LINE_KINDS), "updates": _counted(rc.updates if rc is not None else {}, UPDATE_KINDS),
        "answers": _counted(rc.answers, RESPONSE_KINDS) if rc is not None and rc.answers is not None else None,
        "needing": [_line_row(a, ln, ans, who) for ln, ans in needing] if who.devices else [],
        "rest": [_line_row(a, ln, ans, who) for ln, ans in rest] if who.devices else [],
        "set_url": (f"{reverse('web:security_advisory_models', args=[a.number])}?{urlencode({'model': dm.pk})}"
                    if who.edit and is_open else ""),
        "remove_url": reverse("web:security_advisory_model_remove", args=[a.number, row.pk]) if who.edit and is_open else "",
        "open_url": (_door("web:cybersecurity_release_open_updates", [row.fix_release_id], a)
                     if who.open_updates and row.remedy == AdvisoryRemedy.RELEASE and row.fix_release_id and unopened else ""),
        "fix_version": row.fix_release.version if row.fix_release_id else "",
        "model_record_url": (_record_url(advisory=a.number, model=dm.pk)
                             if who.decide and _ours_waiting(needing, rules.MODEL_SCOPE) else ""),
        "releases_url": (_door("web:cybersecurity_set_release", [dm.pk], a)
                         if who.edit_devices and counts_lines.get(rules.LINE_NOT_RECORDED, 0) else ""),
    }
    found["acts"] = bool(found["set_url"] or found["remove_url"] or found["open_url"] or found["model_record_url"]
                         or any(r["record_url"] for r in releases))
    return found


def _source_row(a, s, *, may_edit: bool, removable: bool) -> dict:
    """A live source: who published it, its reference and link, its days, its revision and what it changed; Edit, Record revision, and
    Remove (never the last live one) for Edit."""
    changed = [AdvisoryChange(v).label for v in (s.last_change or ()) if v in AdvisoryChange.values]
    return {"s": s, "label": s.get_source_display(), "changed": changed,
            "edit_url": reverse("web:security_advisory_source_edit", args=[a.number, s.pk]) if may_edit else "",
            "revision_url": reverse("web:security_advisory_revision", args=[a.number, s.pk]) if may_edit else "",
            "remove_url": reverse("web:security_advisory_source_remove", args=[a.number, s.pk]) if may_edit and removable else ""}


def _decision_rows(a, today) -> list:
    """Decisions for this advisory (practice S7): each one's number (its page), scope, decision, IT exception number, review, and state;
    live ones first (by review day), then ended (newest first). One query."""
    tones = {rules.IN_FORCE: "ok", rules.REVIEW_SOON: "info", rules.REVIEW_DUE: "warn", rules.ENDED: "neutral"}
    out = []
    for d in (DeviceSecurityDecision.objects.filter(advisory_id=a.pk).select_related(*_DECISION_RELATED)
              .order_by(F("ended_on").desc(nulls_first=True), "review_by", "number")):
        state = rules.decision_state(d, today)
        out.append({"d": d, "url": reverse("web:security_decision", args=[d.number]), "scope": rules.scope_words(d),
                    "response": d.get_response_display(), "review": rules.review_words(d, today),
                    "state": {"words": rules.DECISION_STATE_WORDS[state], "tone": tones[state]}})
    return out


def _page_context(request, a) -> dict:
    """The advisory's page: everything in rules' and advisories' words, its reading read once per reader (fixed queries however many
    sources, models, devices, and decisions), the actions by level."""
    user = request.user
    today = timezone.localdate()
    who = _reader(user, a)
    live = a.removed_on is None
    is_open = live and a.status == AdvisoryStatus.OPEN
    counts = rules.advisory_counts([a], today=today, devices_visible=who.devices)[a.pk]
    rows = [rc.row for rc in counts.rows] if live else rules.rows_of([a])
    by_row = {rc.row.pk: rc for rc in counts.rows}
    reading = rules.advisory_reading([a], today) if live and who.devices else rules.AdvisoryReading(rows, [], {})
    lines_by: dict = {}
    for ln in reading.lines:
        lines_by.setdefault(ln.row.pk, []).append(ln)
    sources = list(AdvisorySource.objects.filter(advisory_id=a.pk).order_by("created_at", "pk"))
    live_sources = [s for s in sources if s.removed_on is None]
    url = advisory_url(a.number)
    can_history = history_tabs.allowed(user, AREA)
    hist = history_tabs.context(request, a, AREA, url, tab=False) if can_history and request.GET.get("history") else {}
    also = advisories.overlaps(a.cves, exclude=a) if a.cves and live else {}
    may_correct = cyber_perms.can_correct_advisories(user)
    reopenable = live and ((a.status == AdvisoryStatus.NOT_APPLICABLE and cyber_perms.can_edit(user))
                           or (a.status == AdvisoryStatus.CLOSED and may_correct))
    return {
        "a": a, "ad_url": url, "ad_name": rules.advisory_name(a, rows=rows, sources=sources), "ad_words": counts.words, "ad_tone": _tone(a),
        "ad_cves": list(a.cves or ()), "no_cves": rules.NO_CVES_WORDS,
        "also": [{"number": n, "url": advisory_url(n), "cves": ", ".join(items)} for n, items in also.items()],
        "checks": rules.advisory_checks(a, today=today, devices_visible=who.devices),
        "sources": [_source_row(a, s, may_edit=who.edit, removable=len(live_sources) > 1) for s in live_sources],
        "source_new_url": reverse("web:security_advisory_source_new", args=[a.number]) if who.edit else "",
        "ad_rows": [_model_row(a, r, by_row.get(r.pk), lines_by.get(r.pk, []), reading.answers, who) for r in rows],
        "ad_decisions": _decision_rows(a, today) if who.devices else None,
        "who": who, "today": today,
        "edit_url": reverse("web:security_advisory_edit", args=[a.number]) if who.edit else "",
        "models_url": reverse("web:security_advisory_models", args=[a.number]) if who.edit and is_open else "",
        "confirm_url": (reverse("web:security_advisory_confirm", args=[a.number])
                        if who.edit and is_open and a.models_confirmed_on is None and rows else ""),
        "na_url": reverse("web:security_advisory_not_applicable", args=[a.number]) if who.edit and is_open else "",
        "close_url": reverse("web:security_advisory_close", args=[a.number]) if is_open and cyber_perms.can_decide(user) else "",
        "reopen_url": reverse("web:security_advisory_reopen", args=[a.number]) if reopenable else "",
        "in_error_url": reverse("web:security_advisory_in_error", args=[a.number]) if live and may_correct else "",
        "csv_url": reverse("web:security_advisory_devices_csv", args=[a.number]) if who.devices else "",
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist,
    }


@web_view(MODULE, VIEW)
def advisory(request, number):
    """An advisory's page: into #drawer by HTMX, or the Advisories tab with it open when its address is opened directly. Its History
    section and Show older answer here (?history=1, ?older=)."""
    a = _advisory(number)
    if history_tabs.asked(request, tab=False):
        history_tabs.require(request.user, AREA)
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, a, AREA, advisory_url(a.number), tab=False)
    ctx = _page_context(request, a)
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, DRAWER, ctx)
    return render(request, PAGE, {**_list_context(request), **ctx, "drawer_template": DRAWER})


DEVICE_COLUMNS = ["Advisory", "CVE IDs", "Tag", "Serial", "Manufacturer", "Model", "Department", "Whose", "Connection", "Release", "Line",
                  "Response", "Decision", "IT exception number", "Review by"]


@web_view(MODULE, VIEW)
def advisory_devices_csv(request, number):
    """Every device on site of a model the advisory names, one row each, every line kind (practice B5: IT's handoff, matched by serial):
    its line and, where it needs a response, the responses in rules' words (a work order's number only inside them) and the governing
    decision's number, IT exception number, and review day."""
    _need_devices(request.user)
    a = _advisory(number)
    today = timezone.localdate()
    reading = rules.advisory_reading([a], today) if a.removed_on is None else rules.AdvisoryReading([], [], {})
    cves = ", ".join(a.cves or ())

    def rows():
        for ln in reading.lines:
            asset, answer = ln.asset, reading.answers.get((ln.asset.pk, ln.row.pk))
            needs = answer is not None and bool(answer.kind)
            d = answer.decision if needs else None
            yield (a.number, cves, asset.tag, asset.serial, asset.device_model.manufacturer, asset.device_model.model,
                   asset.department.name if asset.department_id else "", _whose(asset), ln.standing.connection.words,
                   ln.standing.release_words, rules.LINE_KINDS[ln.kind], "; ".join(r.words for r in answer.responses) if needs else "",
                   d.number if d is not None else "", d.exception_reference if d is not None else "",
                   d.review_by if d is not None else "")

    return csv_response(f"cadence-security-advisory-{a.number}-devices-{today:%Y-%m-%d}.csv", DEVICE_COLUMNS, rows())


# --- answers -------------------------------------------------------------------------------------------------------------------------

def advisory_saved(request, advisory, message: str):
    """The answer of a save made from the advisory's page (this module's modals; Record decision, slice 38's update doors, and Set
    devices to this release, each `from=advisory`): the advisory's page into #drawer, freshly read, CHANGED (the lists refresh), a
    toast with `message`, then the modal closes after settle (closing it first would detach the form that sent this request and cancel
    the swap). `advisory` an instance or its number."""
    a = _advisory(getattr(advisory, "number", advisory))
    response = retarget(render(request, DRAWER, _page_context(request, a)), "#drawer")
    trigger_client_event(response, CHANGED, {})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _modal(request, template: str, ctx: dict):
    form = ctx.get("form")
    if form is not None and form.is_bound:
        form.focus_first_error()
    return render(request, template, {"action": request.get_full_path(), **ctx})


def _opened_directly(request, a=None):
    """A modal's address opened directly (no HTMX): the advisory's page, or the tab."""
    return redirect(advisory_url(a.number) if a is not None else reverse("web:security_advisories"))


def _without(error, *keys) -> ValidationError | None:
    """The refusal without the keys the modal says otherwise (the CVE overlap's note), or None when nothing else is left."""
    if not hasattr(error, "error_dict"):
        return error
    left = {k: v for k, v in error.message_dict.items() if k not in keys}
    return ValidationError(left) if left else None


def _parsed_cves(text) -> list | None:
    """The CVE IDs as typed (advisories._cves), or None when the text is not a list of them: the writer's refusal of it under `cves`
    is then the format's (said as it is), never an overlap to note."""
    problems: dict = {}
    found = advisories._cves(text, problems)
    return None if problems else found


def _seen(request) -> list:
    return [n.strip().upper() for n in request.POST.getlist("seen") if n.strip()]


def _overlap_notes(found: dict, seen: list) -> list:
    """The overlaps the writer refused as not shown (`found`: advisories.overlaps; `seen`: the numbers shown before), each in words with
    its advisory's first live source: "CVE-YYYY-NNNN and 11 more are on AD-26-0002 (Manufacturer bulletin DEMO-ADV-0002)." One query."""
    unseen = {n: items for n, items in found.items() if n.upper() not in seen}
    if not unseen:
        return []
    first: dict = {}
    for s in (AdvisorySource.objects.filter(advisory__number__in=list(unseen), removed_on__isnull=True).select_related("advisory")
              .order_by("created_at", "pk")):
        first.setdefault(s.advisory.number, s)
    out = []
    for n, items in unseen.items():
        shown = items[0] if len(items) == 1 else f"{items[0]} and {len(items) - 1} more"
        source = _source_words(first[n]) if n in first else n
        out.append({"number": n, "words": OVERLAP_WORDS.format(items=shown, verb="is" if len(items) == 1 else "are", number=n, source=source),
                    "add_url": reverse("web:security_advisory_source_new", args=[n])})
    return out


# --- Record advisory, Edit advisory ------------------------------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def advisory_new(request):
    """Record advisory (GET the modal, POST advisories.record_advisory): the day received, the CVE IDs, and its first source. A CVE ID
    (or the reference under another source kind) already on a live advisory comes back as a note naming it, with "Add as a source of
    AD-…" (Add source there, posting these fields) and "Record as its own advisory" (posting again with the numbers shown: `seen`)."""
    _need_edit(request.user)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request)
    today = timezone.localdate()
    if request.method != "POST":
        return _modal(request, NEW, {"form": RecordAdvisoryForm(today=today), "overlaps": [], "seen": [], "isac_help": advisories.ISAC_HELP})
    form = RecordAdvisoryForm(request.POST, today=today)
    seen = _seen(request)
    saved, notes = None, []
    if form.is_valid():
        kwargs = form.service_kwargs()
        try:
            saved = advisories.record_advisory(**kwargs, seen=seen, by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            parsed = _parsed_cves(kwargs["cves"])
            if hasattr(e, "error_dict") and "cves" in e.error_dict and parsed is not None:
                found = advisories.overlaps(parsed, source=kwargs["source"], reference=kwargs["reference"])
                notes = _overlap_notes(found, seen)
                if notes:
                    seen = sorted({*seen, *found})
                    e = _without(e, "cves")
            if e is not None:
                form.add_service_errors(e)
    if saved is None:
        return _modal(request, NEW, {"form": form, "overlaps": notes, "seen": seen, "overlap_hint": OVERLAP_HINT,
                                     "isac_help": advisories.ISAC_HELP})
    return advisory_saved(request, saved, RECORDED.format(number=saved.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def advisory_edit(request, number):
    """Edit advisory: the day received and the CVE IDs (advisories.update_advisory). CVE IDs added that another live advisory holds come
    back as a note; saving again keeps them (`seen`)."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    blocker = advisories.ENTERED_IN_ERROR.format(number=a.number) if a.removed_on is not None else ""
    if request.method != "POST":
        initial = {"received_on": a.received_on.isoformat(), "cves": " ".join(a.cves or ())}
        return _modal(request, EDIT_FORM, {"a": a, "form": None if blocker else EditAdvisoryForm(initial=initial), "blocker": blocker,
                                           "overlaps": [], "seen": []})
    form = EditAdvisoryForm(request.POST)
    seen = _seen(request)
    saved, notes = None, []
    if form.is_valid():
        kwargs = form.service_kwargs()
        try:
            saved = advisories.update_advisory(a, **kwargs, seen=seen, by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            parsed = _parsed_cves(kwargs["cves"])
            if hasattr(e, "error_dict") and "cves" in e.error_dict and parsed is not None:
                added = [c for c in parsed if c not in (a.cves or [])]
                found = advisories.overlaps(added, exclude=a)
                notes = _overlap_notes(found, seen)
                if notes:
                    seen = sorted({*seen, *found})
                    e = _without(e, "cves")
            if e is not None:
                form.add_service_errors(e)
    if saved is None:
        return _modal(request, EDIT_FORM, {"a": a, "form": form, "blocker": "", "overlaps": notes, "seen": seen,
                                           "overlap_hint": EDIT_OVERLAP_HINT})
    return advisory_saved(request, a, SAVED.format(number=a.number))


# --- sources -------------------------------------------------------------------------------------------------------------------------

def _source(a, source_id) -> AdvisorySource:
    found = AdvisorySource.objects.filter(pk=source_id, advisory_id=a.pk).first()
    if found is None:
        raise Http404
    return found


def _source_blocker(a, source=None) -> str:
    if a.removed_on is not None:
        return advisories.ENTERED_IN_ERROR.format(number=a.number)
    if source is not None and source.removed_on is not None:
        return advisories.SOURCE_REMOVED_WORDS.format(number=a.number)
    return ""


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def source_new(request, number):
    """Add source (advisories.add_source): who published it, its reference, link, day published, and the CVE IDs it adds (merged into the
    advisory's). Also Record advisory's "Add as a source of AD-…", which posts its fields here."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    blocker = _source_blocker(a)
    if request.method != "POST":
        return _modal(request, SOURCE, {"a": a, "form": None if blocker else SourceForm(adding=True), "blocker": blocker, "adding": True,
                                        "isac_help": advisories.ISAC_HELP})
    form = SourceForm(request.POST, adding=True)
    saved = None
    if form.is_valid():
        try:
            saved = advisories.add_source(a, **form.service_kwargs(), by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if saved is None:
        return _modal(request, SOURCE, {"a": a, "form": form, "blocker": "", "adding": True, "isac_help": advisories.ISAC_HELP,
                                        "action": reverse("web:security_advisory_source_new", args=[a.number])})
    return advisory_saved(request, a, SOURCE_ADDED.format(number=a.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def source_edit(request, number, source_id):
    """Edit source (advisories.update_source): its kind, reference, link, and day published (its revision through Record revision)."""
    _need_edit(request.user)
    a = _advisory(number)
    source = _source(a, source_id)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    blocker = _source_blocker(a, source)
    ctx = {"a": a, "source": source, "blocker": blocker, "adding": False, "isac_help": advisories.ISAC_HELP}
    if request.method != "POST":
        initial = {"source": source.source, "reference": source.reference, "url": source.url,
                   "published_on": source.published_on.isoformat() if source.published_on else ""}
        return _modal(request, SOURCE, {**ctx, "form": None if blocker else SourceForm(initial=initial, adding=False)})
    form = SourceForm(request.POST, adding=False)
    saved = None
    if form.is_valid():
        try:
            saved = advisories.update_source(source, **form.service_kwargs(), by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if saved is None:
        return _modal(request, SOURCE, {**ctx, "blocker": "", "form": form})
    return advisory_saved(request, a, SOURCE_SAVED.format(number=a.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def source_revision(request, number, source_id):
    """Record revision (advisories.record_revision): the revision as printed, the day updated, and what changed. One that changes what it
    names (products or releases, vulnerabilities added, a fix named) asks to confirm the models again, and an advisory marked not
    applicable opens again: the toast says which."""
    _need_edit(request.user)
    a = _advisory(number)
    source = _source(a, source_id)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    blocker = _source_blocker(a, source)
    ctx = {"a": a, "source": source, "blocker": blocker}
    if request.method != "POST":
        initial = {"revision": source.revision, "updated_on": source.updated_on.isoformat() if source.updated_on else ""}
        return _modal(request, REVISION, {**ctx, "form": None if blocker else RevisionForm(initial=initial)})
    form = RevisionForm(request.POST)
    saved = None
    was = (a.status, a.models_confirmed_on)
    if form.is_valid():
        try:
            saved = advisories.record_revision(source, **form.service_kwargs(), by=request.user, today=timezone.localdate())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if saved is None:
        return _modal(request, REVISION, {**ctx, "blocker": "", "form": form})
    now = SecurityAdvisory.objects.filter(pk=a.pk).values_list("status", "models_confirmed_on").first()
    if was[0] == AdvisoryStatus.NOT_APPLICABLE and now[0] == AdvisoryStatus.OPEN:
        message = REVISION_REOPENED
    elif was[1] is not None and now[1] is None:
        # Confirm models is an open advisory's: a closed one stays closed, and reopening it comes first (its Check says so)
        message = REVISION_REOPEN_TO_CONFIRM if now[0] == AdvisoryStatus.CLOSED else REVISION_TO_CONFIRM
    else:
        message = REVISION_RECORDED
    return advisory_saved(request, a, message.format(number=a.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def source_remove(request, number, source_id):
    """Remove source (advisories.remove_source): never the last live one (advisories.source_removal_refusal, said before it is tried)."""
    _need_edit(request.user)
    a = _advisory(number)
    source = _source(a, source_id)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    words = REMOVE_SOURCE_WORDS.format(source=_source_words(source), number=a.number)
    return _confirming(request, a, title=f"{a.number}: remove a source", words=words, button="Remove the source", tone="danger",
                       blocker=advisories.source_removal_refusal(source) or "",
                       write=lambda: advisories.remove_source(source, by=request.user, today=timezone.localdate()),
                       message=SOURCE_REMOVED.format(number=a.number))


# --- Set models, Remove model, Confirm models -----------------------------------------------------------------------------------------

def _model_choices(os_only: bool) -> list:
    """The catalog's models for Set models' first step, by manufacturer and model: every model, or (`os_only`) those recorded with an
    operating system on their profile or a release, each with it ("Windows: Siemens Cios Spin (Windows 10 IoT ...)": practice S6, one
    query over SoftwareRelease)."""
    if not os_only:
        return [(str(pk), f"{maker} {model}") for pk, maker, model in
                DeviceModel.objects.order_by("manufacturer", "model", "pk").values_list("pk", "manufacturer", "model")]
    from apps.cybersecurity.models import OsFamily, SoftwareRelease

    found: dict = {}
    for pk, maker, model, family, os_name in (SoftwareRelease.objects.filter(removed_on__isnull=True)
                                              .exclude(os_family__in=["", OsFamily.NONE])
                                              .order_by("device_model__manufacturer", "device_model__model", "version_key")
                                              .values_list("device_model_id", "device_model__manufacturer", "device_model__model",
                                                           "os_family", "os_name")):
        entry = found.setdefault(pk, {"label": f"{maker} {model}", "os": []})
        words = f"{OsFamily(family).label}{f' ({os_name})' if os_name else ''}"
        if words not in entry["os"]:
            entry["os"].append(words)
    return [(str(pk), f"{entry['label']}: {', '.join(entry['os'])}") for pk, entry in found.items()]


def _model_releases(dm, today) -> list:
    """[(release, its devices on site, its OS name)] of the model's versioned releases (rules.releases), the counts from the devices'
    standings (fixed queries), the OS name the release's or its profile's (rules.facts)."""
    from collections import Counter

    read = rules.standings(rules.on_site().filter(device_model=dm), today)
    running = Counter(s.release.pk for s in read.values() if s.release is not None)
    profile = rules.profile(dm)
    return [(r, running.get(r.pk, 0), rules.facts(r, profile).get("os_name") or "") for r in rules.releases(dm)]


def _models_modal(request, a, dm, form, *, note: str = ""):
    row = AdvisoryModel.objects.filter(advisory_id=a.pk, device_model=dm).first()
    return _modal(request, MODELS, {"a": a, "dm": dm, "form": form, "editing": row is not None, "note": note, "blocker": "",
                                    "step": "model", "back_url": reverse("web:security_advisory_models", args=[a.number])})


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def advisory_models(request, number):
    """Set models (advisories.set_model): first the model (?model=<id>: one it names already edits its row; "Models recorded with an
    operating system" narrows the choice, ?os=1), then which releases it names (each with its devices on site and OS name), what it
    offers, the fix or the patch list, and "These are all the models it names that are here". "Add a version not on file" posts
    `add_version` here: services.add_release alone (Edit), then this form again from the POST with the new release ticked (releases
    listed) or chosen as the fix (a release remedy with none chosen); nothing else is saved. A save posted with a version in that box
    (Enter pressed in it, or Save clicked) is the same: the version is added and the form comes back to be saved, never saved without
    it; refused, the box says so and how to save without it (CLEAR_TO_SAVE). Refused on an advisory not open."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    today = timezone.localdate()
    blocker = advisories.set_model_refusal(a) or ""
    model_id = parse_uuid(request.GET.get("model", ""))
    if model_id is None and request.method == "POST":
        raise Http404
    if model_id is None or (blocker and request.method != "POST"):  # a POST meanwhile: the writer says why, under its locks
        os_only = request.GET.get("os") == "1"
        named = {str(pk) for pk in AdvisoryModel.objects.filter(advisory_id=a.pk).values_list("device_model_id", flat=True)}
        return _modal(request, MODELS, {"a": a, "blocker": blocker, "step": "choose", "os_only": os_only,
                                        "choices": [] if blocker else _model_choices(os_only), "named": named,
                                        "choose_url": reverse("web:security_advisory_models", args=[a.number])})
    dm = DeviceModel.objects.filter(pk=model_id).first()
    if dm is None:
        raise Http404
    if request.method != "POST":
        rows = [r for r in rules.rows_of([a]) if r.device_model_id == dm.pk]
        form = ModelForm(initial=model_initial(rows[0] if rows else None), releases=_model_releases(dm, today))
        return _models_modal(request, a, dm, form)
    saving = request.POST.get("action") != "add_version"
    if not saving or str(request.POST.get("add_version", "")).strip():
        # The button, or Enter in its box (the browser submits through Save, without `action`): a version typed is added, never dropped
        return _add_version(request, a, dm, today, saving=saving)
    form = ModelForm(request.POST, releases=_model_releases(dm, today))
    row = None
    named = AdvisoryModel.objects.filter(advisory_id=a.pk, device_model=dm).exists()
    if form.is_valid():
        try:
            row = advisories.set_model(a, device_model=dm, **form.service_kwargs(), by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if row is None:
        return _models_modal(request, a, dm, form)
    return advisory_saved(request, a, (MODEL_SAVED if named else MODEL_NAMED).format(number=a.number, model=dm.model))


def _add_version(request, a, dm, today, *, saving: bool):
    """"Add a version not on file": services.add_release (profile=False, its own transaction), then Set models again from the POST with
    the new release ticked (releases listed) or chosen as the fix (a release remedy with none chosen). Its refusal lands on the box, the
    text typed kept there: from the button in add_release's words alone; from Save (`saving`), which sends the box whenever it is not
    blank, followed by CLEAR_TO_SAVE, so a refused version never leaves every later Save refused with nothing saying why."""
    version = request.POST.get("add_version", "")
    added = None
    try:
        added = cyber.add_release(dm, version=version, profile=False, by=request.user, today=today)
    except (ValidationError, PermissionDenied) as e:
        form = ModelForm(request.POST, releases=_model_releases(dm, today))
        form.is_valid()
        form.add_service_errors(e)
        if saving and form.has_error("add_version"):
            form.errors["add_version"][:] = [" ".join([*form.errors["add_version"], CLEAR_TO_SAVE])]
        return _models_modal(request, a, dm, form)
    data = request.POST.copy()
    data.pop("action", None)
    data["add_version"] = ""
    if data.get("remedy") == AdvisoryRemedy.RELEASE and not data.get("fix_release"):
        data["fix_release"] = str(added.pk)
    elif data.get("versions") == AdvisoryVersions.LISTED and str(added.pk) not in data.getlist("release_ids"):
        data.appendlist("release_ids", str(added.pk))
    form = ModelForm(data, releases=_model_releases(dm, today))
    form.is_valid()
    return _models_modal(request, a, dm, form, note=VERSION_ADDED.format(version=added.version, model=dm.model))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def model_remove(request, number, row_id):
    """Remove model (advisories.remove_model): refused on an advisory not open, or while a live decision under it has its scope on the
    model (advisories.model_removal_refusal, said before it is tried in its reader's words)."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    row = AdvisoryModel.objects.filter(pk=row_id, advisory_id=a.pk).select_related("device_model").first()
    if row is None:
        return _confirming(request, a, title=f"{a.number}: remove a model", words="", button="", tone="",
                           blocker=advisories.ROW_GONE.format(number=a.number), write=None, message="")
    model = row.device_model.model
    words = REMOVE_MODEL_WORDS.format(number=a.number, model=model) + (REMOVE_MODEL_CONFIRMED if a.models_confirmed_on else "")
    return _confirming(request, a, title=f"{a.number}: stop naming the {model}", words=words, button="Remove the model", tone="danger",
                       blocker=advisories.model_removal_refusal(row, user=request.user) or "",
                       write=lambda: advisories.remove_model(row, by=request.user, today=timezone.localdate()),
                       message=MODEL_REMOVED.format(number=a.number, model=model))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def advisory_confirm(request, number):
    """Confirm models (advisories.confirm_models): the models it names are all the ones here."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    rows = rules.rows_of([a])
    blocker = advisories.set_model_refusal(a) or ("" if rows else advisories.NAME_A_MODEL)
    if not blocker and a.models_confirmed_on is not None:
        blocker = ALREADY_CONFIRMED.format(number=a.number, day=rules.day_words(a.models_confirmed_on))
    models = ", ".join(f"the {r.device_model.model}" for r in rows)
    return _confirming(request, a, title=f"{a.number}: confirm the models it names", words=CONFIRM_WORDS.format(number=a.number, models=models),
                       button="Confirm the models", tone="primary", blocker=blocker,
                       write=lambda: advisories.confirm_models(a, by=request.user, today=timezone.localdate()),
                       message=CONFIRMED.format(number=a.number))


# --- Not applicable, Close, Reopen, Entered in error ---------------------------------------------------------------------------------

def _confirming(request, a, *, title: str, words: str, button: str, tone: str, blocker: str, write, message: str):
    """A confirmation modal: GET says what it does (or why it cannot now: `blocker`, a lock-free reader's words); POST calls `write`
    (the writer, which reads the same refusal again under its locks) and answers with the advisory's page, or comes back with the
    writer's refusal at the top."""
    ctx = {"a": a, "title": title, "words": words, "button": button, "tone": tone}
    if request.method != "POST":
        return _modal(request, CONFIRM, {**ctx, "blocker": blocker, "form": ConfirmForm()})
    if write is None:
        raise Http404
    form = ConfirmForm(request.POST)
    form.is_valid()
    try:
        write()
    except (ValidationError, PermissionDenied) as e:
        form.add_service_errors(e)
        return _modal(request, CONFIRM, {**ctx, "blocker": "", "form": form})
    return advisory_saved(request, a, message)


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def advisory_not_applicable(request, number):
    """Not applicable here (advisories.mark_not_applicable): one of three reasons, each checked against the records and its refusal shown
    before it is tried (advisories.not_applicable_refusals); refused as a whole while a live decision is under it, or on an advisory not
    open (not_applicable_refusal)."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    today = timezone.localdate()
    blocker = advisories.not_applicable_refusal(a, user=request.user) or ""
    refusals = advisories.not_applicable_refusals(a, today) if not blocker else {}
    reasons = [{"value": value, "label": label, "refusal": refusals.get(value) or ""} for value, label in NotApplicableReason.choices]
    ctx = {"a": a, "reasons": reasons, "blocker": blocker, "note": advisories.NOT_APPLICABLE_NOTE if not rules.rows_of([a]) else "",
           "open_reasons": [r for r in reasons if not r["refusal"]]}
    if request.method != "POST":
        return _modal(request, NOT_APPLICABLE, {**ctx, "form": NotApplicableForm()})
    form = NotApplicableForm(request.POST)
    form.is_valid()
    try:
        advisories.mark_not_applicable(a, reason=form.reason(), by=request.user, today=today)
    except (ValidationError, PermissionDenied) as e:
        form.add_service_errors(e)
        return _modal(request, NOT_APPLICABLE, {**ctx, "blocker": "", "form": form})
    return advisory_saved(request, a, NOT_APPLICABLE_TOAST.format(number=a.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, APPROVE)
def advisory_close(request, number):
    """Close (advisories.close): permissions.can_decide (Approve and Equipment View: it rests on device lines and decisions); why it
    cannot be closed now is advisories.close_refusal's words, said before it is tried (the devices' tags, up to 10)."""
    if not cyber_perms.can_decide(request.user):
        raise PermissionDenied(advisories.CLOSE_PERMISSION)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    today = timezone.localdate()
    if a.status == AdvisoryStatus.CLOSED and a.removed_on is None:
        blocker = advisories.ALREADY_CLOSED.format(number=a.number)
    else:
        blocker = advisories.close_refusal(a, today, user=request.user) or ""
    return _confirming(request, a, title=f"{a.number}: close it", words=CLOSE_WORDS.format(number=a.number), button="Close it",
                       tone="primary", blocker=blocker, write=lambda: advisories.close(a, by=request.user, today=today),
                       message=CLOSED.format(number=a.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, EDIT)
def advisory_reopen(request, number):
    """Reopen (advisories.reopen): from not applicable at Edit, from closed at Device security Approve (advisories.reopen_refusal says
    so before it is tried)."""
    _need_edit(request.user)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    words = REOPEN_WORDS.format(number=a.number) + (REOPEN_CONFIRMED if a.models_confirmed_on is not None else REOPEN_TO_CONFIRM)
    return _confirming(request, a, title=f"{a.number}: reopen it", words=words, button="Reopen it",
                       tone="primary", blocker=advisories.reopen_refusal(a, user=request.user) or "",
                       write=lambda: advisories.reopen(a, by=request.user, today=timezone.localdate()),
                       message=REOPENED.format(number=a.number))


@require_http_methods(["GET", "POST"])
@web_view(MODULE, APPROVE)
def advisory_in_error(request, number):
    """Entered in error (advisories.mark_entered_in_error): permissions.can_correct_advisories (Approve); refused while a live decision
    is under it (advisories.entered_in_error_refusal, in its reader's words)."""
    if not cyber_perms.can_correct_advisories(request.user):
        raise PermissionDenied(advisories.CORRECT_PERMISSION)
    a = _advisory(number)
    if request.method != "POST" and not request.htmx:
        return _opened_directly(request, a)
    return _confirming(request, a, title=f"{a.number}: entered in error", words=IN_ERROR_WORDS.format(number=a.number),
                       button="Mark entered in error", tone="danger",
                       blocker=advisories.entered_in_error_refusal(a, user=request.user) or "",
                       write=lambda: advisories.mark_entered_in_error(a, by=request.user, today=timezone.localdate()),
                       message=IN_ERROR.format(number=a.number))

