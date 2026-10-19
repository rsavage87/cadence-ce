"""
The CE department's test equipment (slice 35): the Equipment screen's Test equipment tab, an item's drawer (a full page when opened
directly), Add test equipment, Change, the status moves, Record calibration, entered in error, the tolerance review, and the CSVs.
Views parse input and call apps.testequipment.services; the rule is apps.testequipment.rules (every standing's words are its words,
never composed here); the levels apps.testequipment.permissions. Scoped users are refused every view here (web_view's default).

Who:
- View (Equipment View) sees the list, its CSV, an item, its calibrations, the review boxes' windows and counts, and History. The work
  orders themselves (their numbers, days, and devices: "Work done with it", a review's work list, both CSVs, and the uses in History)
  need Work orders View too, as the change log's "Test equipment used" area does.
- Edit (Equipment Edit) adds an item (with its latest certificate, optional), changes its details (the tag only before its first
  calibration or use; the interval only at Decide once a certificate is on file), moves its everyday status, and records a
  calibration.
- Decide (Work orders Approve with Equipment View) marks a calibration entered in error and records the tolerance review; Equipment
  Approve retires and reinstates. The views offer each action at its level and the services check every one again.

The list (`/test-equipment/`, nav_active "equipment", the Equipment screen's tab strip): tag, kind, make and model, serial, status and
since, held by, and the standing judged today; filters (standing, status, kind, held by) and a search by tag or serial; the body
re-fetches itself on `te-changed` (every change here fires it). Each change in a modal answers with the item's drawer into #drawer, the
toast, `te-changed`, and the modal's close; a drawer button's refusal shows at the drawer's top and in the toast.

The review box (each live calibration found out of tolerance as found, or failed): its look-back window in words, the work in it now,
the last SHOWN with each one's day and device, a CSV of the whole list; Decide's modal posts the version of the list it was drawn with
(services.review_version), refused in services.REVIEW_CHANGED's words when the list moved since (the modal then shows the list as it is
now, with its version). Once decided: the basis, who, when, the count it was decided on, and for the two recheck decisions the recheck
progress ("2 of 3 rechecked"); "work added since the review" when the window now holds more.
"""
import re
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.core import history
from apps.testequipment import permissions as perms
from apps.testequipment import rules
from apps.testequipment import services as tes
from apps.testequipment.models import (
    TAG_MAX,
    Basis,
    Calibration,
    Kind,
    Reason,
    Result,
    Status,
    TestEquipment,
    TestEquipmentUse,
    ToleranceReview,
)
from apps.vendors.rules import fmt

from . import history_tabs
from .decorators import web_view
from .exports import csv_response
from .forms_testequipment import (
    MARK_WORDS,
    NOBODY,
    STANDING_FILTERS,
    STATUS_FILTERS,
    CalibrationForm,
    ItemForm,
    NewItemForm,
    ReviewForm,
    calibration_initial,
    due_hint,
    holder_filter_choices,
    new_item_initial,
    parse_day,
    parse_te_filters,
)
from .htmx import PAGE_SIZE, is_partial, toast

PAGE = "web/test_equipment.html"
TABLE = "web/_te_table.html"
DRAWER = "web/_te_drawer.html"
NEW = "web/_te_new.html"
EDIT = "web/_te_edit.html"
CALIBRATE = "web/_te_calibrate.html"
DUE = "web/_te_due.html"
IN_ERROR = "web/_te_in_error.html"
REVIEW = "web/_te_review.html"
CHANGED = "te-changed"  # the list re-fetches itself on it
SHOWN = 10  # the drawer's lists: the newest this many, then a CSV of every one
RECHECK_BASES = (Basis.RECHECKED, Basis.NEXT_WORK)  # the decisions whose progress is the recheck of each device
WORK_VIEW = "The work orders done with test equipment need Work orders View."


# --- words ---------------------------------------------------------------------------------------------------------------------

def status_words(item) -> str:
    """"Out of service (damaged)"."""
    words = Status(item.status).label
    if item.status_reason:
        words += f" ({Reason(item.status_reason).label.lower()})"
    return words


def standing_tone(standing, today) -> str:
    """The standing chip's colour: past due or failed red, no calibration on file or due within the list's 60 days amber, else green."""
    if standing.state in (rules.PAST_DUE, rules.FAILED):
        return "crit"
    if standing.state == rules.NO_RECORD or rules.matches(standing, "due_soon", today):
        return "warn"
    return "ok"


def window_words(item, bounds) -> str:
    """A review's look-back window (rules.window) in words."""
    start, end = bounds
    what = rules.noun(item).lower()
    if start is None:
        return f"Work completed on or before {fmt(end)}: no passed {what} before it is on file."
    return f"Work completed after {fmt(start)} (the passed {what} before it) through {fmt(end)}."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _file_part(text: str) -> str:
    """A tag or certificate number as part of a download's name: letters, digits, dot, dash, underscore."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:60]


def _words(e) -> str:
    if isinstance(e, PermissionDenied):
        return str(e) or "You don't have access to do that."
    return " ".join(e.messages)


# --- lookups -------------------------------------------------------------------------------------------------------------------

def _item(tag) -> TestEquipment:
    """One item by its tag in any letter case, in this facility: another facility's, or none, is a 404."""
    tag = str(tag or "")
    if not tag or "\x00" in tag or len(tag) > TAG_MAX * 2:
        raise Http404
    found = TestEquipment.objects.select_related("held_by", "created_by").filter(tag_key=tag.casefold()).first()
    if found is None:
        raise Http404
    return found


def _calibration(item, pk) -> Calibration:
    found = Calibration.objects.select_related("test_equipment").filter(pk=pk, test_equipment=item).first()
    if found is None:
        raise Http404
    return found


def reads_work(user) -> bool:
    """The work orders done with test equipment (their numbers, days, devices): Work orders View too."""
    return user.has_level(Module.WORKORDERS, Level.VIEW)


# --- the list ------------------------------------------------------------------------------------------------------------------

def filter_items(f, today) -> list:
    """The list's items for filters `f`, by tag, each with `.standing` judged today (services.list_with_standing: two queries)."""
    qs = TestEquipment.objects.select_related("held_by")
    if f.status == "":
        qs = qs.filter(status__in=tes.LIVE_STATUSES)
    elif f.status != "all":
        qs = qs.filter(status=f.status)
    if f.kind:
        qs = qs.filter(kind=f.kind)
    if f.held_by == NOBODY:
        qs = qs.filter(held_by__isnull=True)
    elif f.held_by:
        qs = qs.filter(held_by_id=f.held_by)
    if f.q:
        qs = qs.filter(Q(tag_key__contains=f.q.casefold()) | Q(serial__icontains=f.q))
    items = tes.list_with_standing(qs.order_by("tag_key"), today)
    if f.standing:
        items = [i for i in items if rules.matches(i.standing, f.standing, today)]
    return items


def _summary(today) -> dict:
    """The page's line: the items not retired, and how many are in each standing filter (each a link to it)."""
    items = tes.list_with_standing(TestEquipment.objects.filter(status__in=tes.LIVE_STATUSES), today)
    counts = [(key, label, sum(1 for i in items if rules.matches(i.standing, key, today))) for key, label in rules.FILTERS.items()]
    return {"live": len(items), "counts": [(key, label, n) for key, label, n in counts if n]}


def _list_context(request) -> dict:
    today = timezone.localdate()
    f = parse_te_filters(request.GET)
    items = filter_items(f, today)
    page = Paginator(items, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"i": i, "tone": standing_tone(i.standing, today), "status": status_words(i)} for i in page.object_list]
    options = {"standings": STANDING_FILTERS, "statuses": STATUS_FILTERS, "kinds": Kind.choices, "holders": holder_filter_choices()}
    return {"nav_active": "equipment", "equipment_tab": "test_equipment", "list_url": reverse("web:test_equipment"), "f": f, "page": page,
            "rows": rows, "options": options, "summary": _summary(today), "can_add": perms.can_edit(request.user)}


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment(request):
    ctx = _list_context(request)
    if is_partial(request, "te-table"):  # the page head's line comes along, so a change in the drawer shows there too
        return render(request, TABLE, {**ctx, "oob_summary": True})
    return render(request, PAGE, ctx)


CSV_COLUMNS = ["Tag", "Kind", "Manufacturer", "Model", "Serial", "Certificates", "Interval (months)", "Status", "Status since", "Held by",
               "Standing today", "Due"]


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_csv(request):
    """The list's items (its filters, every page) as CSV, each with its standing judged today in the rules' words."""
    today = timezone.localdate()
    items = filter_items(parse_te_filters(request.GET), today)
    rows = ((i.tag, i.get_kind_display(), i.manufacturer, i.model, i.serial, i.get_program_display(), i.interval_months, status_words(i),
             i.status_since, i.held_by.name if i.held_by else "", i.standing.words, i.standing.due_on) for i in items)
    return csv_response(f"cadence-test-equipment-{today:%Y-%m-%d}.csv", CSV_COLUMNS, rows)


# --- the drawer ----------------------------------------------------------------------------------------------------------------

def _review_box(item, cal, *, work_rows: bool) -> dict:
    """One calibration's review box: the window, the work in it now, the last SHOWN (Work orders View), the review and what changed
    since, and the recheck progress for a recheck decision."""
    s = tes.review_summary(cal)
    review = s.review
    progress = tes.recheck_progress(cal) if review is not None and review.basis in RECHECK_BASES else None
    reviewed = rules.reviewed_ids(review) if review is not None else set()
    recent = [{"w": w, "added": review is not None and str(w.pk) not in reviewed} for w in reversed(s.work[-SHOWN:])] if work_rows else []
    return {"s": s, "window": window_words(item, s.window), "count": len(s.work), "recent": recent, "progress": progress,
            "devices": progress.devices[:SHOWN * 2] if progress is not None and work_rows else [], "found": _found(cal)}


def _found(cal) -> str:
    """Why the work done with it needs a review: the certificate failed it, or found it out of tolerance as found."""
    return "Failed" if cal.result == Result.FAILED else "Found out of tolerance"


def _work_words(item, cals, count: int) -> str:
    last = max((c.calibrated_on for c in rules.live(cals)), default=None)
    what = rules.noun(item).lower()
    if last is None:
        return f"{_plural(count, 'work order')} recorded it (no {what} on file)."
    return f"{_plural(count, 'work order')} since its last {what} ({fmt(last)})."


def _drawer_context(request, item, refusal: str = "") -> dict:
    user = request.user
    today = timezone.localdate()
    cals = list(Calibration.objects.filter(test_equipment=item).select_related("recorded_by", "entered_in_error_by")
                .order_by("-calibrated_on", "-created_at"))
    standing = rules.standing_on(item, today, cals)
    entered = rules.entered_days(cals)
    can_edit, can_decide, work_rows = perms.can_edit(user), perms.can_decide(user), reads_work(user)
    cal_rows = [{"c": c, "entered": entered.get(c.pk), "mark": MARK_WORDS.get(c.accreditation, ""),
                 "box": _review_box(item, c, work_rows=work_rows) if c.needs_review and not c.entered_in_error else None,
                 "can_error": can_decide and not c.entered_in_error} for c in cals]
    count = tes.work_since_last_calibration(item)
    work = tes.work_done(item, limit=SHOWN) if work_rows else []
    moves = [{"m": m, "confirm": f"Retire {item.tag} ({Reason(m.reason).label.lower()})?" if m.to == Status.RETIRED else ""}
             for m in tes.moves_for(item, user)]
    can_history = history.can_read(user, "test_equipment")
    url = reverse("web:test_equipment_item", args=[item.tag])
    hist = _history_context(request, item, url) if can_history and request.GET.get("history") else {}
    return {
        "item": item, "today": today, "refusal": refusal, "standing": standing, "tone": standing_tone(standing, today),
        "status": status_words(item), "noun": rules.noun(item), "back_words": tes.back_in_use_words(item) if item.status == Status.OUT_OF_SERVICE else "",
        "cal_rows": cal_rows, "work": work, "work_count": count, "work_words": _work_words(item, cals, count), "work_rows": work_rows,
        "moves": moves, "can_edit": can_edit, "can_decide": can_decide, "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW),
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist,
    }


def _drawer(request, item, refusal: str = ""):
    return render(request, DRAWER, _drawer_context(request, _item(item.tag), refusal))


def _changed(response):
    trigger_client_event(response, CHANGED, {})
    return response


def _saved(request, item, message: str):
    """A modal saved: the item's drawer into #drawer, the list refreshed, the toast, then the modal closed after settle (closing it
    first would detach the form that sent this request, which cancels the swap and the events)."""
    response = retarget(_drawer(request, item), "#drawer")
    toast(_changed(response), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _refused(request, item, e):
    message = _words(e)
    return toast(_drawer(request, item, message), message)


# --- History -------------------------------------------------------------------------------------------------------------------

USE_TITLES = {"added": "Recorded on {number}", "deleted": "Taken off {number}"}


def _history_title(entry) -> str:
    """The item's own changes by their action; a calibration's or a review's by the record ("Calibration C-1 added"); a use by its work
    order ("Recorded on WO-26-0042")."""
    if entry.area == "test_equipment":
        return entry.action_label
    verb = history_tabs.VERBS.get(entry.action, entry.action)
    if entry.area == "test_equipment_uses":
        number = entry.record.split(" · ")[0]
        return USE_TITLES.get(entry.action, "Use on {number} " + verb).format(number=number)
    what = entry.record.split(" · ", 1)[-1]  # "calibration C-1", "review of calibration C-1"
    return f"{what[:1].upper()}{what[1:]} {verb}"


def _history_context(request, item, url: str) -> dict:
    """{"hist": ...} (history_tabs' shape) for the item's History section: its own changes with its calibrations', their reviews', and
    (Work orders View too: the change log's "Test equipment used") the work orders that recorded it."""
    user = request.user
    after = request.GET.get("older") or None
    if after is not None and history.parse_cursor(after) is None:
        after = None
    related = {"calibrations": {"test_equipment_id": item.pk}}
    cal_ids = list(Calibration.objects.filter(test_equipment=item).values_list("pk", flat=True))
    if cal_ids:
        related["tolerance_reviews"] = {"calibration_id__in": cal_ids}
    if history.can_read(user, "test_equipment_uses"):
        related["test_equipment_uses"] = {"test_equipment_id": item.pk}
    related = {k: v for k, v in related.items() if history.can_read(user, k)}
    entries, following = history.record_history(item, related, limit=history_tabs.PAGE, after=after)
    rows = [{"e": e, "title": _history_title(e)} for e in entries]
    more_url = f"{url}?" + urlencode({"history": 1, "older": following}) if following is not None else None
    return {"hist": {"rows": rows, "more_url": more_url, "first": after is None}}


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_item(request, tag):
    """The item's drawer (HTMX), or the list with the drawer open when its address is opened directly. ?history=1 the History
    section (its entries only for the HTMX partials #hist and #hist-more)."""
    item = _item(tag)
    if history_tabs.asked(request, tab=False):
        history_tabs.require(request.user, "test_equipment")
        if history_tabs.wants_entries(request):
            url = reverse("web:test_equipment_item", args=[item.tag])
            return render(request, history_tabs.ENTRIES, _history_context(request, item, url))
    ctx = _drawer_context(request, item)
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, DRAWER, ctx)
    return render(request, PAGE, {**_list_context(request), **ctx, "drawer_template": DRAWER})


# --- Add test equipment and Change ---------------------------------------------------------------------------------------------

@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_new(request):
    """Add test equipment: GET the modal, POST services.add_test_equipment with its latest certificate when any of the certificate's
    fields is filled. Refusals come back on their fields, with what was typed."""
    today = timezone.localdate()
    if request.method != "POST":
        return render(request, NEW, {"form": NewItemForm(initial=new_item_initial())})
    form = NewItemForm(request.POST)
    item = None
    if form.is_valid():
        try:
            item = tes.add_test_equipment(**form.item_kwargs(), calibration=form.calibration(), by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if item is None:
        form.focus_first_error()
        return render(request, NEW, {"form": form})
    item = _item(item.tag)
    standing = rules.standing_on(item, today, Calibration.objects.filter(test_equipment=item))
    return _saved(request, item, f"{item.tag} added: {standing.words}")


def _edit_locks(item, user) -> dict:
    """The fields Change does not offer, with why: the tag once a calibration or work is on record, the interval below Decide once a
    live certificate is on file (the services refuse both too)."""
    locks = {}
    if Calibration.objects.filter(test_equipment=item).exists() or TestEquipmentUse.objects.filter(test_equipment=item).exists():
        locks["tag"] = f"{item.tag} has calibrations or work on record, so its tag stays."
    if Calibration.objects.filter(test_equipment=item, entered_in_error=False).exists() and not perms.can_decide(user):
        locks["interval_months"] = tes.INTERVAL_PERMISSION
    return locks


def _item_initial(item) -> dict:
    return {"tag": item.tag, "kind": item.kind, "manufacturer": item.manufacturer, "model": item.model, "serial": item.serial,
            "program": item.program, "interval_months": item.interval_months, "held_by": str(item.held_by_id) if item.held_by_id else ""}


def _edit_form(item, user, data=None) -> ItemForm:
    form = ItemForm(data, initial=_item_initial(item), holder=item.held_by)
    form.locks = _edit_locks(item, user)
    for name in form.locks:
        del form.fields[name]
    return form


@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_edit(request, tag):
    """Change: GET the modal, POST services.update_test_equipment with the fields offered."""
    item = _item(tag)
    if request.method != "POST":
        return render(request, EDIT, {"item": item, "form": _edit_form(item, request.user)})
    form = _edit_form(item, request.user, request.POST)
    saved = None
    # Review fix: a field offered when the modal opened and locked since (a certificate recorded meanwhile) is refused, never dropped
    stored = _item_initial(item)
    changed_locked = [why for name, why in form.locks.items()
                      if name in request.POST and str(request.POST.get(name, "")).strip() != str(stored[name])]
    if changed_locked:
        form.is_valid()
        form.add_error(None, " ".join(changed_locked))
    elif form.is_valid():
        try:
            # Review fix: only the fields the modal posted (a field locked when it opened and unlocked since is not sent as blank)
            fields = {name: value for name, value in form.item_kwargs().items() if name in request.POST}
            saved = tes.update_test_equipment(item, by=request.user, today=timezone.localdate(), **fields)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if saved is None:
        form.focus_first_error()
        return render(request, EDIT, {"item": item, "form": form})
    return _saved(request, saved, f"{saved.tag}: details saved")


# --- status --------------------------------------------------------------------------------------------------------------------

@require_POST
@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_status(request, tag):
    """A status move by hand (services.set_status: `to` and `reason`, the drawer's buttons from services.moves_for). Retiring and
    reinstating need Equipment Approve (the service says so); out of service goes back in use only by a passed calibration."""
    item = _item(tag)
    try:
        item = tes.set_status(item, to=request.POST.get("to", ""), reason=request.POST.get("reason", ""), by=request.user,
                              today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        return _refused(request, item, e)
    return toast(_changed(_drawer(request, item)), f"{item.tag}: {status_words(item).lower()}")


# --- calibrations --------------------------------------------------------------------------------------------------------------

def _due(item, *, calibrated_on, typed: str, prefilled: str) -> dict:
    """The due day's field re-fetched (#tec-due): the day prefilled for the calibration day typed (unless a day of the user's own is
    typed), the hidden prefill it was drawn with, and the hint with the cap."""
    hint = due_hint(item, parse_day(calibrated_on))
    default = hint.default.isoformat() if hint.default else ""
    value = typed if typed and typed != prefilled else default
    return {"value": value, "default": default, "hint": hint, "error": ""}


def _calibrate_modal(request, item, form):
    """The modal: the due day as typed (or prefilled) with the prefill it was drawn with, and the hint for the calibration day shown."""
    data = form.data if form.is_bound else form.initial
    first = form.focus_first_error() if form.is_bound else ""
    due = {"value": str(data.get("due_on") or "").strip(), "default": str(data.get("due_default") or "").strip(),
           "hint": due_hint(item, parse_day(data.get("calibrated_on"))), "error": " ".join(form.errors.get("due_on", [])),
           "autofocus": first == "due_on"}
    return render(request, CALIBRATE, {"item": item, "form": form, "due": due, "noun": rules.noun(item)})


@web_view(Module.EQUIPMENT, Level.EDIT)
def test_equipment_calibrate(request, tag):
    """Record calibration: GET the modal (the due day prefilled), POST services.record_calibration. The modal's due day re-fetches
    itself (#tec-due) when the calibration day changes: the new day's prefill, unless a day was typed."""
    item = _item(tag)
    today = timezone.localdate()
    if is_partial(request, "tec-due"):
        p = request.GET
        return render(request, DUE, {"due": _due(item, calibrated_on=p.get("calibrated_on", ""), typed=p.get("due_on", "").strip(),
                                                 prefilled=p.get("due_default", "").strip())})
    if request.method != "POST":
        last = Calibration.objects.filter(test_equipment=item, entered_in_error=False).order_by("-calibrated_on").first()
        return _calibrate_modal(request, item, CalibrationForm(initial=calibration_initial(item, today, last), item=item))
    form = CalibrationForm(request.POST, item=item)
    before = item.status
    cal = None
    if form.is_valid():
        try:
            cal = tes.record_calibration(item, **form.service_kwargs(), by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if cal is None:
        return _calibrate_modal(request, item, form)
    item = _item(item.tag)
    parts = [f"{item.tag}: {rules.noun(item).lower()} of {fmt(cal.calibrated_on)} recorded"]
    if item.status != before:
        parts.append(f"now {status_words(item).lower()}")
    if cal.needs_review:
        parts.append("review the work done with it")
    return _saved(request, item, "; ".join(parts))


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_calibration_error(request, tag, pk):
    """Entered in error (Decide): GET the modal saying what it does, POST services.mark_entered_in_error. The record stays, marked;
    the standing follows the other calibrations; the status stays (the toast says so: services.entered_in_error_note)."""
    item = _item(tag)
    if not perms.can_decide(request.user):
        raise PermissionDenied(tes.DECIDE_PERMISSION)
    cal = _calibration(item, pk)
    has_review = ToleranceReview.objects.filter(calibration=cal).exists()
    blocker = f"Certificate {cal.certificate} is already marked entered in error." if cal.entered_in_error else ""
    ctx = {"item": item, "c": cal, "blocker": blocker, "error": "", "has_review": has_review, "status": status_words(item).lower(),
           "noun": rules.noun(item).lower()}
    if request.method != "POST":
        return render(request, IN_ERROR, ctx)
    try:
        cal = tes.mark_entered_in_error(cal, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        return render(request, IN_ERROR, {**ctx, "blocker": "", "error": _words(e)})
    return _saved(request, item, tes.entered_in_error_note(_calibration(item, cal.pk)))


# --- the tolerance review ------------------------------------------------------------------------------------------------------

def _review_blocker(cal) -> str:
    if cal.entered_in_error:
        return f"Certificate {cal.certificate} was marked entered in error: there is nothing to review."
    if not cal.needs_review:
        return f"Certificate {cal.certificate} found it in tolerance and it passed: there is nothing to review."
    if ToleranceReview.objects.filter(calibration=cal).exists():
        return f"The review of certificate {cal.certificate} is already recorded."
    return ""


def _review_modal(request, item, cal, form, s=None):
    """Decide's modal, drawn with the work in the window now and its version (a refusal shows the list as it is now)."""
    s = s or tes.review_summary(cal)
    return render(request, REVIEW, {"item": item, "c": cal, "form": form, "s": s, "window": window_words(item, s.window), "count": len(s.work),
                                    "recent": [{"w": w, "added": False} for w in reversed(s.work[-SHOWN:])], "blocker": _review_blocker(cal),
                                    "found": _found(cal)})


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_review(request, tag, pk):
    """The tolerance review (Decide): GET the modal (the window, the work in it, the decision's form carrying the list's version),
    POST services.decide_review. A list that moved since is refused (REVIEW_CHANGED) and the modal shows it as it is now."""
    item = _item(tag)
    if not perms.can_decide(request.user):
        raise PermissionDenied(tes.DECIDE_PERMISSION)
    cal = _calibration(item, pk)
    today = timezone.localdate()
    if request.method != "POST":
        return _review_modal(request, item, cal, ReviewForm(initial={"decided_on": today.isoformat()}))
    form = ReviewForm(request.POST)
    review = None
    if form.is_valid():
        try:
            review = tes.decide_review(cal, **form.service_kwargs(), by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if review is None:
        cal = _calibration(item, cal.pk)
        s = tes.review_summary(cal)
        if str(request.POST.get("version", "")) != s.version and tes.REVIEW_CHANGED not in form.non_field_errors():
            form.add_error(None, tes.REVIEW_CHANGED)  # refused for another reason, but the list moved too: say so before it is decided
        form.focus_first_error()
        return _review_modal(request, item, cal, form, s)
    return _saved(request, item, f"Review of certificate {cal.certificate} recorded: {_plural(review.work_count, 'work order')}")


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_review_csv(request, tag, pk):
    """A review box's whole work list (Work orders View too): each work order in the window now, the day of the work (the use's own
    day, the one the window reads) and its device, and whether the review's decision covers it."""
    item = _item(tag)
    if not reads_work(request.user):
        raise PermissionDenied(WORK_VIEW)
    cal = _calibration(item, pk)
    if not cal.needs_review:
        raise Http404
    s = tes.review_summary(cal)
    reviewed = rules.reviewed_ids(s.review) if s.review is not None else set()

    def covered(wo) -> str:
        if s.review is None:
            return "Not reviewed"
        return "Yes" if str(wo.pk) in reviewed else "Added since the review"

    rows = [(w.number, w.get_type_display(), w.asset.tag, w.used_on, covered(w)) for w in s.work]  # the use's day, the window's
    today = timezone.localdate()
    name = f"cadence-{_file_part(item.tag)}-review-{_file_part(cal.certificate)}-{today:%Y-%m-%d}.csv"
    return csv_response(name, ["Work order", "Type", "Device", "Day of the work", "In the review"], rows)


@web_view(Module.EQUIPMENT, Level.VIEW)
def test_equipment_uses_csv(request, tag):
    """Every use of the item (Work orders View too): each work order, the day of the work (the use's own day) and its device, the
    item's standing on that day, and whether a later certificate found it out of tolerance, in the rules' words."""
    item = _item(tag)
    if not reads_work(request.user):
        raise PermissionDenied(WORK_VIEW)
    judged = tes.work_done(item)
    rows = [(j.use.work_order.number, j.use.work_order.get_type_display(), j.use.work_order.asset.tag, j.day,
             j.standing.words if j.standing else "", "; ".join(f.words for f in j.later)) for j in judged]
    today = timezone.localdate()
    return csv_response(f"cadence-{_file_part(item.tag)}-work-{today:%Y-%m-%d}.csv",
                        ["Work order", "Type", "Device", "Day of the work", "Standing that day", "Found out of tolerance later"], rows)

