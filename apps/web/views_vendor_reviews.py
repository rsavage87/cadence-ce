"""
The Contracts screen's Reviews tab (slice 34): the yearly review of each contracted service vendor (apps.evaluations.vendor_reviews and
vendor_figures): the vendors with their last review and when the next is due, a new review, the review's page (the figures behind their
gate, the ratings, the vendor's reported figures, the decision and its plan, the sign-off, the committees it was reported to), its
print, and the contracted services summary for the governing body. Evaluations View with Contracts View reads; Edit drafts and writes;
Approve signs off. Scoped users are refused every view here (web_view's default): a vendor's account never sees its review.

Who: every view is `reviews_view(level)`: web_view on Evaluations at the level (it takes one module) and Contracts View too, the
screen the reviews live on (apps.evaluations.permissions.can_view_reviews and its kin: View reads and prints; Edit drafts, refreshes the
figures, writes the reviewer's fields, discards a draft, and records a corrective plan's result; Approve signs off, withdraws, and
records, corrects, and removes the committees a review was reported to). Each service checks again and its refusals come back in words.

The tab (/contracts/reviews/): vendor_reviews.vendors_list, by the day the next review is due (a contract's end first), each vendor
with its names, its contracts in force, its last signed-off review (number, overall, decision, plan status), a draft, and when the next
is due, or "Not reviewed"; `?show=not_reviewed` keeps the vendors with no signed-off review (the survey binder's check links there) and
`?vendor=<name>` one vendor (the contract drawer's line links there). New review (Edit, and the figures' gate: drafting takes them) is a
modal over the tab, or the tab with the form on it when opened directly: the vendor's spellings ticked, more typed, and the 12 months
by their last day (last month's end, yesterday, the 12 months after the vendor's last review, each contract's last full contract year,
or a day typed). Record a report to committee (Approve) records one meeting for the signed-off reviews ticked (vendor_reviews.
record_report). The summary print (web:vendor_reviews_print) is the governing body's document.

The review's page (/contracts/reviews/<number>/) renders whole when opened directly; each part is its own HTMX partial (PARTS, by the
target's id). The figures are the review's current snapshot through vendor_figures.cut, all or nothing (Evaluations, Contracts,
Reports, and Work orders View), never the stored data as it is. The reviewer's fields are four blocks, each saved on its own through
vendor_reviews.update_review (only the fields a block posts, so two people writing different blocks never overwrite each other). Sign
off posts the snapshot id the page was drawn with, so the approver signs the figures they saw (vendor_reviews.FIGURES_CHANGED
otherwise). A modal's refusals render in the modal with what was typed kept; a button's at the top of the part it swaps, and in the
toast.

Privacy: the summary is staff-written and length-limited, about the vendor's service as a whole (REVIEW_PRIVACY_WARNING says so where it
is written); it is on the page and the review's own print only: never in a CSV, an email, the summary print, or All facilities. The
figures name no person.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import wraps
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_htmx.http import HttpResponseClientRedirect, reswap, retarget, trigger_client_event

from apps.accounts import people
from apps.accounts.models import Level, Module
from apps.core import history
from apps.evaluations import permissions as perms
from apps.evaluations import vendor_figures
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import (
    CRITERIA,
    REVIEW_PRIVACY_WARNING,
    REVIEW_SUMMARY_MAX,
    Decision,
    Overall,
    PlanResult,
    Rating,
    ReportBody,
    ReviewStatus,
    StatedInContract,
    VendorReview,
    VendorReviewReport,
)
from apps.pm.dates import add_months
from apps.vendors import rules

from . import history_tabs
from .decorators import web_view
from .forms import parse_uuid
from .forms_vendor_reviews import (
    BLOCK_FIELDS,
    BLOCK_KEYS,
    DECISION,
    NAMES_SHOWN,
    OTHER,
    RATINGS,
    REPORTED,
    SUMMARY,
    BlockForm,
    NewReviewForm,
    PlanForm,
    ReportForm,
    SignOffForm,
)
from .htmx import is_partial, toast

PAGE = "web/vendor_reviews.html"
LIST = "web/_vendor_review_list.html"
NEW = "web/_vendor_review_new.html"
BATCH = "web/_vendor_review_batch.html"
BODY = "web/_vendor_review_body.html"
HEAD = "web/_vendor_review_head.html"
FIGURES = "web/_vendor_review_figures.html"
FIELDS = "web/_vendor_review_fields.html"
FIELD = "web/_vendor_review_field.html"
REPORTS = "web/_vendor_review_reports.html"
REPORT = "web/_vendor_review_report.html"
SIGN = "web/_vendor_review_sign.html"
PLAN = "web/_vendor_review_plan.html"
CONFIRM = "web/_vendor_review_confirm.html"
PRINT = "web/print_vendor_review.html"
SUMMARY_PRINT = "web/print_vendor_reviews.html"
# The page's parts, by the id an HTMX request targets: each renders on its own.
PARTS = {"vr-body": BODY, "vr-head": HEAD, "vr-figures": FIGURES, "vr-fields": FIELDS, "vr-reports": REPORTS}

NOT_REVIEWED = "not_reviewed"  # the tab's ?show= for the vendors with no signed-off review
SHOWS = {"": "Every vendor", NOT_REVIEWED: "Not reviewed"}
BATCH_MONTHS = 24  # Record a report to committee lists each vendor's last review and the reviews signed off in these months
VENDOR_MAX = vr.NAME_MAX

STATUS_CSS = {ReviewStatus.DRAFT: "neutral", ReviewStatus.APPROVED: "ok", ReviewStatus.WITHDRAWN: "neutral", ReviewStatus.DISCARDED: "neutral"}
RATING_CSS = {Rating.MET: "ok", Rating.PARTLY: "warn", Rating.NOT_MET: "crit", Rating.NA: "neutral"}
OVERALL_CSS = {Overall.MEETS: "ok", Overall.PARTIALLY: "warn", Overall.DOES_NOT_MEET: "crit"}
OVERALL_SHORT = {Overall.MEETS: "meets", Overall.PARTIALLY: "partially meets", Overall.DOES_NOT_MEET: "does not meet"}
PLAN_CSS = {PlanResult.MET: "ok", PlanResult.NOT_MET: "crit", PlanResult.CONTRACT_ENDED: "neutral"}

SPELLING_NOTE = ("A review finds the vendor's contracts and work orders by the names it gives, in any letter case. A vendor spelled "
                 "another way (Philips Healthcare and Philips field service) is another vendor, with no history here, until a review "
                 "names that spelling too: give every spelling your contracts and work orders use.")
FIGURES_NOTE = "The figures come from Cadence's records of the vendor's work, dated when they were entered. Cadence rates nothing."


def _today() -> date:
    """The pages' clock, in one place so tests can pin it: the facility's today (its time zone is active in a request)."""
    return timezone.localdate()


def reviews_view(level):
    """web_view on Evaluations at `level` (it takes one module), and Contracts View too: the screen the reviews live on. Scoped users
    are refused by web_view's default."""

    def deco(view):
        @wraps(view)
        def checked(request, *args, **kwargs):
            if not request.user.has_level(Module.CONTRACTS, Level.VIEW):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return web_view(perms.MODULE, level)(checked)

    return deco


# --- words ----------------------------------------------------------------------------------------------------------------------

def _day(d) -> str:
    return vr.day_words(d) if isinstance(d, date) else ""


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _words(e) -> str:
    if isinstance(e, PermissionDenied):
        return str(e) or "You don't have access to do that."
    return " ".join(e.messages)


def _number(value) -> str:
    """97.50 -> "97.5", 100.00 -> "100"."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pct(value) -> str:
    return f"{_number(value)}%" if value is not None else ""


def approver_words(r: VendorReview) -> str:
    """"Pat Lee, VP Support Services"."""
    return ", ".join(p for p in (r.approver_name, r.approver_title) if p)


def stamp(r: VendorReview) -> str:
    """The review's state in words: "Draft", "Signed off <day> by <name, title>", "Withdrawn; signed off ...", "Discarded"."""
    if r.status == ReviewStatus.DRAFT:
        return "Draft"
    if r.status == ReviewStatus.DISCARDED:
        return "Discarded"
    signed = f"Signed off {_day(r.signed_on)}" + (f" by {approver_words(r)}" if r.approver_name else "") if r.signed_on else ""
    if r.status == ReviewStatus.WITHDRAWN:
        return f"Withdrawn; {signed[0].lower()}{signed[1:]}" if signed else "Withdrawn"
    return signed or "Signed off"


def plan_status(r: VendorReview | None, today: date) -> dict | None:
    """A review's corrective action plan in words: its result and the day it closed, or the day it is due (overdue once that day has
    passed on a signed-off review). None without a plan."""
    if r is None or r.decision != Decision.CORRECTIVE_PLAN:
        return None
    if r.plan_result:
        words = PlanResult(r.plan_result).label + (f" {_day(r.plan_closed_on)}" if r.plan_closed_on else "")
        return {"words": words, "short": PlanResult(r.plan_result).label.lower(), "css": PLAN_CSS.get(r.plan_result, "neutral"), "open": False,
                "overdue": False}
    if r.plan_due_on is None:
        return {"words": "Plan due day not set", "short": "plan due day not set", "css": "neutral", "open": True, "overdue": False}
    overdue = r.status == ReviewStatus.APPROVED and r.plan_due_on < today
    words = f"Plan overdue: due {_day(r.plan_due_on)}" if overdue else f"Plan due {_day(r.plan_due_on)}"
    return {"words": words, "short": words[0].lower() + words[1:], "css": "crit" if overdue else "info", "open": True, "overdue": overdue}


def review_url(r: VendorReview, name: str = "vendor_review", *extra) -> str:
    """An address of this review: its page, or one of its actions (`name`: "vendor_review_refresh" ...)."""
    return reverse(f"web:{name}", args=[r.number, *extra])


def print_url(r: VendorReview, tenant) -> str:
    return people.with_facility(reverse("web:vendor_review_print", args=[r.number]), tenant)


def _get(number: str) -> VendorReview:
    """The review by its number (any letter case) in this facility: another facility's, or no review, is a 404."""
    found = vr.current(number)
    if found is None:
        raise Http404
    return found


def _report(r: VendorReview, pk) -> VendorReviewReport:
    found = VendorReviewReport.objects.filter(review=r, pk=pk).first()
    if found is None:
        raise Http404
    return found


# --- the tab ----------------------------------------------------------------------------------------------------------------------

def _last_words(r: VendorReview | None, today: date) -> dict | None:
    if r is None:
        return None
    return {"r": r, "url": review_url(r), "signed": _day(r.signed_on), "overall": r.get_overall_display(), "overall_css": OVERALL_CSS.get(r.overall, ""),
            "decision": r.get_decision_display(), "plan": plan_status(r, today), "period": vr.period_label(r.period_start, r.period_end)}


def _row(row: vr.VendorRow, today: date, can_wo: bool) -> dict:
    return {
        "row": row, "name": row.name, "also": row.names[1:], "key": row.key,
        "contracts": [{"c": c, "url": reverse("web:contract", args=[c.pk]), "ends": _day(c.end_on)} for c in row.contracts],
        "jobs": row.jobs if can_wo else None, "last": _last_words(row.last_review, today),
        "open_plan": plan_status(row.open_plan, today) if row.open_plan is not None and row.open_plan != row.last_review else None,
        "open_plan_r": row.open_plan if row.open_plan is not None and row.open_plan != row.last_review else None,
        "draft": {"r": row.draft, "url": review_url(row.draft)} if row.draft is not None else None,
        "next_due": _day(row.next_due), "overdue": row.overdue, "expected": row.contracted,
        "new_url": f"{reverse('web:vendor_review_new')}?{urlencode({'vendor': row.key})}",
    }


def _vendor_filter(value) -> str:
    return " ".join(str(value or "").split())[:VENDOR_MAX]


def list_context(request, today: date) -> dict:
    """The tab: every vendor of vendors_list (a fixed number of queries), filtered by ?show= and ?vendor=."""
    user = request.user
    show = request.GET.get("show", "")
    show = show if show in SHOWS else ""
    vendor = _vendor_filter(request.GET.get("vendor"))
    key = rules.vendor_key(vendor) if vendor else ""
    every = vr.vendors_list(today)
    rows = [r for r in every if not key or r.key == key]
    counts = {"": len(rows), NOT_REVIEWED: sum(1 for r in rows if not r.reviewed)}
    if show == NOT_REVIEWED:
        rows = [r for r in rows if not r.reviewed]
    can_wo = user.has_level(Module.WORKORDERS, Level.VIEW)
    base = reverse("web:vendor_reviews")

    def show_url(value):
        q = {k: v for k, v in (("show", value), ("vendor", vendor)) if v}
        return f"{base}?{urlencode(q)}" if q else base

    return {
        "nav_active": "contracts", "contracts_tab": "reviews", "today": today, "list_url": base, "show": show, "vendor": vendor,
        "vendor_shown": next((r.name for r in every if r.key == key), vendor) if vendor else "",
        "vendor_unknown": bool(vendor) and not rows and not any(r.key == key for r in every),
        "shows": [{"value": v, "label": label, "n": counts[v], "url": show_url(v), "active": v == show} for v, label in SHOWS.items()],
        "rows": [_row(r, today, can_wo) for r in rows], "can_wo": can_wo, "spelling_note": SPELLING_NOTE,
        "can_new": perms.can_edit_reviews(user), "can_report": perms.can_approve_reviews(user),
        "summary_url": people.with_facility(reverse("web:vendor_reviews_print"), request.tenant), "due_after": vr.DUE_AFTER_DAYS,
        "all_url": base,
    }


@reviews_view(perms.VIEW_LEVEL)
def vendor_reviews(request):
    """The Reviews tab, or its list alone (HTMX, the filters: #vr-list)."""
    ctx = list_context(request, _today())
    if is_partial(request, "vr-list"):
        return render(request, LIST, ctx)
    return render(request, PAGE, ctx)


# --- New review -------------------------------------------------------------------------------------------------------------------

def _contract_year_end(start_on: date, today: date) -> date | None:
    """The last day of the contract's latest full year before today (its anniversary's eve), counted from its term's start."""
    try:
        k = today.year - start_on.year + 1
        for _ in range(4):
            end = add_months(start_on, 12 * k) - timedelta(days=1)
            if end < today:
                return end
            k -= 1
    except (ValueError, OverflowError):
        return None
    return None


def presets(today: date, row: vr.VendorRow | None) -> list[dict]:
    """New review's choices for the last day of its 12 months: the 12 months after the vendor's last review (when they have ended),
    last month's end, yesterday, and the 12 months up to the latest anniversary (or the start) of each of its contracts in force that
    has passed; each day once."""
    options: list[tuple[date, str]] = []
    if row is not None and row.last_review is not None:
        after = vr.next_period_end(row.last_review.period_end)
        if after < today:
            options.append((after, f"The 12 months after {row.last_review.number}"))
    options.append((today.replace(day=1) - timedelta(days=1), "Last month's end"))
    options.append((today - timedelta(days=1), "Yesterday"))
    for c in (row.contracts if row is not None else ()):
        end = _contract_year_end(c.start_on, today)
        if end is not None:
            options.append((end, f"Up to {c.reference}'s {'start' if end + timedelta(days=1) == c.start_on else 'anniversary'}"))
    seen, out = set(), []
    for end, label in options:
        if end in seen or end < vr.EARLIEST:
            continue
        seen.add(end)
        out.append({"value": end.isoformat(), "label": label, "period": vr.period_label(vr.period_start_of(end), end)})
    return out


def _figures_refusal(user, doing: str) -> str:
    """Why `user` may not take or sign the figures (Reports View and Work orders View, all or nothing); "" when they may."""
    return "" if vendor_figures.can_see(user) else vr.FIGURES_PERMISSION.format(doing=doing)


def _new(request, form: NewReviewForm, today: date, *, posted: bool):
    """The New review form: a modal over the tab (HTMX), or the tab with the form on it (opened directly)."""
    every = vr.vendors_list(today)
    data = form.data if form.is_bound else request.GET  # a GET re-draws the form for the vendor chosen, keeping what was typed
    key = rules.vendor_key(_vendor_filter(data.get("vendor")))
    row = next((r for r in every if r.key == key), None) if key else None
    choices = presets(today, row)
    values = {p["value"] for p in choices}
    typed = NewReviewForm.typed_in(data)
    period, period_end = typed["period"], typed["period_end"]
    if period and period != OTHER and period not in values:  # another vendor's choice: the day kept, as typed
        period, period_end = OTHER, period
    if not period:
        period = choices[0]["value"] if choices else OTHER
    names = list(row.names) if row is not None else []
    ticked = set(typed["names"]) if posted else set(names)
    others = [n for n in typed["other_names"] if isinstance(n, str)][:NAMES_SHOWN]
    others += [""] * (NAMES_SHOWN - len(others))
    ctx = {"form": form, "vendors": [{"key": r.key, "name": r.name, "selected": r.key == key} for r in every], "row": row,
           "names": [{"name": n, "ticked": n in ticked} for n in names], "others": others,
           "known": sorted({n for r in every for n in r.names}, key=str.casefold),
           "presets": [{**p, "active": p["value"] == period} for p in choices], "other_active": period not in values,
           "period_end": period_end, "draft_refusal": _figures_refusal(request.user, "Drafting a vendor review (it takes the figures)"),
           "spelling_note": SPELLING_NOTE, "modal": bool(request.htmx), "today": today}
    if request.htmx:
        return render(request, NEW, ctx)
    return render(request, PAGE, {**list_context(request, today), "new": ctx}, status=400 if posted and form.errors else 200)


@reviews_view(perms.EDIT_LEVEL)
def vendor_review_new(request):
    """New review (Edit, and the figures' gate): GET the form (?vendor=<key or name> shows that vendor's spellings), POST
    vendor_reviews.create_review. Saved, the new draft's page opens."""
    today = _today()
    if request.method != "POST":
        return _new(request, NewReviewForm(), today, posted=False)
    form = NewReviewForm(request.POST)
    review = None
    if form.is_valid():
        try:
            review = vr.create_review(vendor_names=form.cleaned_data["vendor_names"], period_end=form.cleaned_data["period_end"],
                                      by=request.user, today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if review is None:
        return _new(request, form, today, posted=True)
    url = review_url(review)
    return HttpResponseClientRedirect(url) if request.htmx else redirect(url)


# --- Record a report to committee (the tab) ---------------------------------------------------------------------------------------

def _body_value(value) -> str:
    return value if value in ReportBody.values else ReportBody.GOVERNING


def _reported_words(reports) -> str:
    return "; ".join(f"{rep.get_body_display()} {_day(rep.meeting_on)}" for rep in reports)


def batch_rows(today: date, body: str, ticked: set | None = None) -> list[dict]:
    """The signed-off reviews Record a report to committee offers: each vendor's last, and those signed off in the last BATCH_MONTHS
    months, newest first, with the committees each was reported to. Ticked: `ticked` (the ids posted), else each vendor's last review
    not yet reported to `body`."""
    lasts = {row.last_review.pk for row in vr.vendors_list(today) if row.last_review is not None}
    since = add_months(today, -BATCH_MONTHS)
    reviews = [r for r in VendorReview.objects.filter(status=ReviewStatus.APPROVED).order_by("-signed_on", "-number")
               if r.pk in lasts or (r.signed_on and r.signed_on >= since)]
    by_review = defaultdict(list)
    for rep in VendorReviewReport.objects.filter(review__in=reviews).order_by("meeting_on", "created_at"):
        by_review[rep.review_id].append(rep)
    out = []
    for r in reviews:
        reps = by_review[r.pk]
        default = r.pk in lasts and not any(rep.body == body for rep in reps)
        out.append({"r": r, "id": str(r.pk), "period": vr.period_label(r.period_start, r.period_end), "signed": _day(r.signed_on),
                    "overall": r.get_overall_display(), "decision": r.get_decision_display(), "reported": _reported_words(reps),
                    "ticked": (str(r.pk) in ticked) if ticked is not None else default, "last": r.pk in lasts})
    return out


def _batch(request, form: ReportForm, today: date, *, ticked: set | None = None):
    body = _body_value(form.data.get("body") if form.is_bound else request.GET.get("body"))
    ctx = {"form": form, "rows": batch_rows(today, body, ticked), "body": body, "bodies": ReportBody.choices, "today": today,
           "meeting_on": form.data.get("meeting_on", "") if form.is_bound else request.GET.get("meeting_on", "") or today.isoformat(),
           "minutes": form.data.get("minutes_reference", "") if form.is_bound else request.GET.get("minutes_reference", ""),
           "months": BATCH_MONTHS}
    return render(request, BATCH, ctx)


@reviews_view(perms.APPROVE_LEVEL)
def vendor_reviews_report(request):
    """Record a report to committee (Approve): GET the modal (?body= ticks each vendor's last review not yet reported to it), POST
    vendor_reviews.record_report for the reviews ticked: one meeting, all or nothing."""
    today = _today()
    if request.method != "POST":
        if not request.htmx:
            return redirect(reverse("web:vendor_reviews"))
        return _batch(request, ReportForm(), today)
    form = ReportForm(request.POST)
    posted = [v for v in request.POST.getlist("reviews") if v]
    ids = [parse_uuid(v) for v in posted]
    reports = None
    if form.is_valid():
        found = list(VendorReview.objects.filter(pk__in=[i for i in ids if i is not None]))
        if any(i is None for i in ids) or len(found) != len(set(ids)):
            form.add_error("reviews", "Choose reviews from the list.")
        else:
            try:
                reports = vr.record_report(found, by=request.user, today=today, **form.values())
            except (ValidationError, PermissionDenied) as e:
                form.add_service_errors(e)
    if reports is None:
        if not request.htmx:
            ctx = list_context(request, today)
            return render(request, PAGE, {**ctx, "refusal": " ".join(m for ms in form.errors.values() for m in ms)}, status=400)
        return _batch(request, form, today, ticked=set(posted))
    n = len(reports)
    message = f"Reported to the {vr.BODY_WORDS.get(form.values()['body'], 'committee')}: {n} review{'' if n == 1 else 's'}"
    if not request.htmx:
        return redirect(reverse("web:vendor_reviews"))
    return trigger_client_event(toast(reswap(HttpResponse(""), "none"), message), "modal-close", {})


# --- the review's page ------------------------------------------------------------------------------------------------------------

def _split_rows(f: dict) -> list[dict]:
    """A figure's splits that hold something: a value (a count of none is left out; a share of none, "0 of 4", stays)."""
    out = []
    for s in f.get("splits") or []:
        if not isinstance(s, dict) or s.get("value") is None:
            continue
        if f.get("unit") == vendor_figures.COUNT and not s.get("value"):
            continue
        out.append({"label": s.get("label", ""), "display": s.get("display", ""), "detail": s.get("detail", "")})
    return out


def _figure_row(f: dict) -> dict:
    return {"key": f.get("key", ""), "label": f.get("label") or f.get("key", ""), "display": f.get("display", ""), "detail": f.get("detail", ""),
            "rule": f.get("rule", ""), "taken": f.get("basis") == vendor_figures.TAKEN, "splits": _split_rows(f)}


def _groups(data: dict) -> list[dict]:
    """The figures by group, in the snapshot's group order (a figure naming a group it does not list, last)."""
    figures = [f for f in data.get("figures") or [] if isinstance(f, dict)]
    labels = {g.get("key"): g.get("label") for g in data.get("groups") or [] if isinstance(g, dict)}
    order = list(labels) + sorted({f.get("group", "") for f in figures} - set(labels))
    out = []
    for key in order:
        rows = [_figure_row(f) for f in figures if f.get("group", "") == key]
        if rows:
            out.append({"key": key, "label": labels.get(key) or "Other figures", "rows": rows})
    return out


def _contracts(data: dict) -> list[dict]:
    out = []
    for c in data.get("contracts") or []:
        if not isinstance(c, dict):
            continue
        versions = [{**v, "term": f"{_day(_iso(v.get('start_on')))} to {_day(_iso(v.get('end_on')))}",
                     "counted": f"{_day(_iso(v.get('from')))} to {_day(_iso(v.get('through')))}"} for v in c.get("versions") or [] if isinstance(v, dict)]
        out.append({**c, "versions": versions})
    return out


def _repeats(data: dict, can_wo: bool) -> list[dict]:
    return [{**p, "completed": _day(_iso(p.get("completed_on"))), "opened": _day(_iso(p.get("opened_on"))),
             "repair_url": reverse("web:wo", args=[p["repair"]]) if can_wo and p.get("repair") else "",
             "later_url": reverse("web:wo", args=[p["later"]]) if can_wo and p.get("later") else ""}
            for p in data.get("repeats") or [] if isinstance(p, dict)]


def figures_context(r: VendorReview, user) -> dict:
    """The figures as `user` may read them: always through vendor_figures.cut (all or nothing)."""
    snap = r.snapshot
    data = vendor_figures.cut(snap.data if snap is not None else {}, user)
    shown = vendor_figures.can_see(user)
    settings = data.get("settings") or {}
    windows, targets = settings.get("pm_windows") or {}, settings.get("pm_targets") or {}
    return {
        "shown": shown, "taken": snap is not None, "taken_on": _day(snap.taken_on) if snap is not None else "",
        "taken_by": str(snap.taken_by) if snap is not None and snap.taken_by_id else "", "snapshot_id": str(snap.pk) if snap is not None else "",
        "needs": vendor_figures.needs_words(), "groups": _groups(data), "contracts": _contracts(data),
        "repeats": _repeats(data, user.has_level(Module.WORKORDERS, Level.VIEW)), "names": list(data.get("names") or []),
        "not_recorded": list(data.get("not_recorded") or []), "notes": list(data.get("notes") or []),
        "provisional": _day(_iso(data.get("provisional_until"))), "as_of": _day(_iso(settings.get("as_of"))),
        "window_high": windows.get("high", "") if isinstance(windows.get("high"), str) else "",
        "window_other": windows.get("other", "") if isinstance(windows.get("other"), str) else "",
        "target_other": _pct(targets.get("other")) if isinstance(targets.get("other"), (int, float)) else "",
    }


# The reviewer's fields: each one's label, input, choices, and help (the service decides and words every refusal).
FIELD_LABELS = {
    **{f: label for f, label, _help in CRITERIA}, "stated_in_contract": "Expectations stated in the contract",
    "uptime_target_pct": "Uptime target (%)", "uptime_reported_pct": "Uptime the vendor reports (%)",
    "uptime_lowest_pct": "Lowest system's uptime (%)", "systems_below": "Systems below the guarantee", "systems_total": "Systems",
    "response_target_pct": "Response target (%)", "response_reported_pct": "Response the vendor reports (%)",
    "vendor_report_reference": "Vendor's report reference", "overall": "Overall", "decision": "Decision",
    "plan_due_on": "Corrective action plan due", "summary": "Summary",
}
FIELD_HELP = {
    **{f: help_text for f, _label, help_text in CRITERIA}, "stated_in_contract": "Whether the contract states these expectations.",
    "vendor_report_reference": "As the vendor's uptime or response report is numbered, without spaces (UPT-2026-Q4).",
    "plan_due_on": "Only for a corrective action plan: the day the vendor's plan is due, after the sign-off.",
}
CHOICES = {
    **{f: Rating.choices for f, _label, _help in CRITERIA}, "stated_in_contract": StatedInContract.choices, "overall": Overall.choices,
    "decision": Decision.choices,
}
BLANK = {**{f: "Not rated" for f, _label, _help in CRITERIA}, "stated_in_contract": "Not recorded", "overall": "Not chosen", "decision": "Not chosen"}
PCT_INPUTS = ("uptime_target_pct", "uptime_reported_pct", "uptime_lowest_pct", "response_target_pct", "response_reported_pct")
COUNT_INPUTS = ("systems_below", "systems_total")
BLOCK_LABELS = {RATINGS: "Ratings", REPORTED: "The vendor's own figures", DECISION: "Overall and decision", SUMMARY: "Summary"}
BLOCK_HELP = {
    RATINGS: "Your rating of each, with the figures above and what the units and staff report behind it. Cadence rates nothing.",
    REPORTED: "As the vendor's own uptime and response reports give them: Cadence never computes uptime or response from its dates.",
    DECISION: "Does not meet expectations needs a corrective action plan, replacing or ending the contract, or bringing the service "
              "in-house.",
    SUMMARY: "",
}


def _kind(f: str) -> str:
    if f in CHOICES:
        return "select"
    if f in PCT_INPUTS or f in COUNT_INPUTS:
        return "number"
    if f == "plan_due_on":
        return "date"
    if f == "summary":
        return "textarea"
    return "text"


def _input_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _number(value)
    return str(value)


def _read(r: VendorReview, key: str, today: date) -> dict:
    """How a block reads when it is not being written."""
    if key == RATINGS:
        rows = [{"label": label, "help": help_text, "words": getattr(r, f"get_{f}_display")() if getattr(r, f) else "",
                 "css": RATING_CSS.get(getattr(r, f), "")} for f, label, help_text in CRITERIA]
        return {"rows": rows, "stated": r.get_stated_in_contract_display() if r.stated_in_contract else ""}
    if key == REPORTED:
        uptime = [w for w in (f"target {_pct(r.uptime_target_pct)}" if r.uptime_target_pct is not None else "",
                              f"reported {_pct(r.uptime_reported_pct)}" if r.uptime_reported_pct is not None else "",
                              f"lowest system {_pct(r.uptime_lowest_pct)}" if r.uptime_lowest_pct is not None else "") if w]
        systems = ""
        if r.systems_below is not None and r.systems_total is not None:
            systems = f"{r.systems_below:,} of {r.systems_total:,} systems below the guarantee"
        elif r.systems_below is not None:
            systems = f"{r.systems_below:,} system{'' if r.systems_below == 1 else 's'} below the guarantee"
        elif r.systems_total is not None:
            systems = f"{r.systems_total:,} system{'' if r.systems_total == 1 else 's'}"
        response = [w for w in (f"target {_pct(r.response_target_pct)}" if r.response_target_pct is not None else "",
                                f"reported {_pct(r.response_reported_pct)}" if r.response_reported_pct is not None else "") if w]
        lines = [line for line in (("Uptime: " + ", ".join(uptime)) if uptime else "", systems,
                                   ("Response: " + ", ".join(response)) if response else "",
                                   f"Vendor's report: {r.vendor_report_reference}" if r.vendor_report_reference else "") if line]
        return {"lines": lines}
    if key == DECISION:
        return {"overall": r.get_overall_display() if r.overall else "", "overall_css": OVERALL_CSS.get(r.overall, ""),
                "decision": r.get_decision_display() if r.decision else "", "plan": plan_status(r, today),
                "stray_due": _day(r.plan_due_on) if r.plan_due_on and r.decision != Decision.CORRECTIVE_PLAN else ""}
    return {"text": r.summary}


def block(r: VendorReview, key: str, today: date, *, typed: dict | None = None, errors: dict | None = None) -> dict:
    """One block of the reviewer's fields: its inputs (what was typed when a save was refused, else what is stored) and how it reads."""
    errors = errors or {}

    def value(f):
        return typed[f] if typed is not None and f in typed else _input_value(getattr(r, f))

    inputs = [{"name": f, "label": FIELD_LABELS[f], "help": FIELD_HELP.get(f, ""), "kind": _kind(f), "value": value(f),
               "choices": CHOICES.get(f, ()), "blank": BLANK.get(f, ""), "error": " ".join(errors.get(f, [])),
               "step": "0.01" if f in PCT_INPUTS else "1", "max": "100" if f in PCT_INPUTS else ""} for f in BLOCK_FIELDS[key]]
    top = [m for k, ms in errors.items() if k not in BLOCK_FIELDS[key] for m in ms]
    return {"key": key, "label": BLOCK_LABELS[key], "help": BLOCK_HELP[key], "url": review_url(r, "vendor_review_field", key), "inputs": inputs,
            "error": " ".join(top), "read": _read(r, key, today), "summary_max": REVIEW_SUMMARY_MAX}


def actions_for(user, r: VendorReview) -> dict:
    """What `user` may do to `r` as it stands (the services check again)."""
    edit, approve = perms.can_edit_reviews(user), perms.can_approve_reviews(user)
    s = r.status
    draft, signed = s == ReviewStatus.DRAFT, s == ReviewStatus.APPROVED
    return {"write": edit and draft, "refresh": edit and draft, "discard": edit and draft, "sign": approve and draft,
            "withdraw": approve and signed, "plan": edit and signed and r.decision == Decision.CORRECTIVE_PLAN,
            "report": approve and signed, "correct": approve and s in (ReviewStatus.APPROVED, ReviewStatus.WITHDRAWN)}


def _history(request, r: VendorReview, url: str) -> dict:
    """The History section's page: the review's changes with its committee reports' (removed ones too), newest first."""
    after = request.GET.get("older") or None
    if after is not None and history.parse_cursor(after) is None:
        after = None
    related = {"vendor_review_reports": {"review_id": r.pk}} if history.can_read(request.user, "vendor_review_reports") else {}
    entries, following = history.record_history(r, related, limit=history_tabs.PAGE, after=after)
    rows = [{"e": e, "title": e.action_label if e.area == "vendor_reviews" else f"Committee report {history_tabs.VERBS.get(e.action, e.action)}"}
            for e in entries]
    more_url = f"{url}?{urlencode({'history': 1, 'older': following})}" if following else None
    return {"hist": {"rows": rows, "more_url": more_url, "first": after is None}}


def _previous(r: VendorReview, today: date) -> dict | None:
    p = vr.previous_approved(r)
    if p is None:
        return None
    return {"r": p, "url": review_url(p), "period": vr.period_label(p.period_start, p.period_end), "overall": p.get_overall_display(),
            "decision": p.get_decision_display(), "plan": plan_status(p, today)}


def reports_rows(r: VendorReview) -> list[dict]:
    return [{"rep": rep, "body": rep.get_body_display(), "meeting": _day(rep.meeting_on)}
            for rep in VendorReviewReport.objects.filter(review=r).select_related("recorded_by").order_by("meeting_on", "created_at")]


def page_context(request, r: VendorReview, *, refusal: str = "", part_refusal: dict | None = None, today: date | None = None) -> dict:
    """Everything the page, its parts, and the print read."""
    user = request.user
    today = today or _today()
    acts = actions_for(user, r)
    url = review_url(r)
    can_history = history.can_read(user, "vendor_reviews")
    hist = _history(request, r, url) if can_history and request.GET.get("history") else {}
    part_refusal = part_refusal or {}
    fig = figures_context(r, user)
    return {
        "nav_active": "contracts", "r": r, "url": url, "today": today, "period": vr.period_label(r.period_start, r.period_end),
        "status_css": STATUS_CSS.get(r.status, "neutral"), "stamp": stamp(r), "refusal": refusal, "acts": acts, "fig": fig,
        "refresh_refusal": _figures_refusal(user, "Taking a vendor review's figures") if acts["refresh"] else "",
        "sign_refusal": _figures_refusal(user, "Signing off a vendor review") if acts["sign"] else "",
        "blocks": [block(r, key, today) for key in BLOCK_KEYS], "privacy": REVIEW_PRIVACY_WARNING, "previous": _previous(r, today),
        "plan": plan_status(r, today), "overall_css": OVERALL_CSS.get(r.overall, ""), "approver": approver_words(r),
        "recorded_by": str(r.signed_by) if r.signed_by_id else "", "reports": reports_rows(r),
        "reports_refusal": part_refusal.get("reports", ""), "print_url": print_url(r, request.tenant),
        "can_history": can_history, "hist_url": f"{url}?history=1", **hist, "figures_note": FIGURES_NOTE,
        "list_url": reverse("web:vendor_reviews"), "spelling_note": SPELLING_NOTE,
    }


@reviews_view(perms.VIEW_LEVEL)
def vendor_review(request, number):
    """The review's page, one of its parts (HTMX: PARTS), or its History (?history=1)."""
    r = _get(number)
    if history_tabs.asked(request, tab=False):
        history_tabs.require(request.user, "vendor_reviews")
        if history_tabs.wants_entries(request):
            return render(request, history_tabs.ENTRIES, _history(request, r, review_url(r)))
    ctx = page_context(request, r)
    if request.htmx and not request.htmx.history_restore_request and request.htmx.target in PARTS:
        return render(request, PARTS[request.htmx.target], ctx)
    return render(request, PAGE, ctx)


def _part(request, r: VendorReview, template: str, target: str, message: str = "", *, refusal: str = "", part: str = ""):
    """A part of the page after an action: the part (its refusal at its top) swapped in place, the toast; posted without HTMX (scripts
    off), the page itself (the refusal at its top), or the page again after a change."""
    r = _get(r.number)
    if not request.htmx:
        if refusal:
            return render(request, PAGE, page_context(request, r, refusal=refusal), status=400)
        return redirect(review_url(r))
    ctx = page_context(request, r, refusal=refusal if template in (BODY, HEAD) else "", part_refusal={part: refusal} if part and refusal else None)
    response = reswap(retarget(render(request, template, ctx), target), "outerHTML")
    return toast(response, refusal or message) if (refusal or message) else response


def _saved(request, r: VendorReview, message: str, template: str = BODY, target: str = "#vr-body"):
    """A modal saved: the part it changed in place, the toast, then the modal closed after settle (closing it first would detach the
    form that sent this request, which cancels the swap)."""
    response = _part(request, r, template, target, message)
    return trigger_client_event(response, "modal-close", {}, after="settle") if request.htmx else response


def _modal_get(request, r: VendorReview, anchor: str = ""):
    """A modal's address opened without HTMX (a link followed, scripts off): the review's page, which has the buttons."""
    return redirect(review_url(r) + (f"#{anchor}" if anchor else ""))


def _modal_refused(request, r: VendorReview, words: str):
    """A modal's form posted with scripts off and refused: the page with the refusal at its top."""
    return render(request, PAGE, page_context(request, _get(r.number), refusal=words), status=400)


def _form_words(form) -> str:
    return " ".join(m for ms in form.errors.values() for m in ms)


# --- the figures ------------------------------------------------------------------------------------------------------------------

@require_POST
@reviews_view(perms.EDIT_LEVEL)
def vendor_review_refresh(request, number):
    """Refresh figures (Edit, a draft, and the figures' gate): vendor_reviews.refresh."""
    r = _get(number)
    try:
        vr.refresh(r, by=request.user, today=_today())
    except (ValidationError, PermissionDenied) as e:
        return _part(request, r, BODY, "#vr-body", refusal=_words(e))
    return _part(request, r, BODY, "#vr-body", f"{r.number}: figures taken again")


# --- the reviewer's fields --------------------------------------------------------------------------------------------------------

@reviews_view(perms.EDIT_LEVEL)
def vendor_review_field(request, number, key):
    """One block of the reviewer's fields (Edit): GET it to read (?edit=1 to write), POST it saved (vendor_reviews.update_review: the
    fields the block posted, all or none). Refused, the block stays open with what was typed and the words on the field they name."""
    r = _get(number)
    if key not in BLOCK_KEYS:
        raise Http404
    today = _today()
    writable = actions_for(request.user, r)["write"]
    if request.method != "POST":
        if not request.htmx:
            return redirect(f"{review_url(r)}#vr-f-{key}")
        editing = bool(request.GET.get("edit")) and writable
        return render(request, FIELD, {"r": r, "b": block(r, key, today), "editing": editing, "write": writable, "privacy": REVIEW_PRIVACY_WARNING})
    parsed = BlockForm(key, request.POST)
    values = parsed.values()
    errors: dict = {} if values is not None else parsed.errors
    if values is not None:
        if not values:
            errors = {"": ["Nothing to save: the form sent none of this block's fields."]}
        else:
            try:
                vr.update_review(r, by=request.user, **values)
            except ValidationError as e:
                errors = e.message_dict if hasattr(e, "error_dict") else {"": e.messages}
            except PermissionDenied as e:
                errors = {"": [_words(e)]}
    r = _get(r.number)
    typed = {f: request.POST.get(f, "") for f in BLOCK_FIELDS[key] if f in request.POST} if errors else None
    b = block(r, key, today, typed=typed, errors=errors)
    words = " ".join(m for ms in errors.values() for m in ms)
    if not request.htmx:  # a form posted with scripts off: the page again, or the page with the refusal at its top
        return render(request, PAGE, page_context(request, r, refusal=words, today=today), status=400) if errors else redirect(f"{review_url(r)}#vr-f-{key}")
    writable = actions_for(request.user, r)["write"]
    ctx = {"r": r, "b": b, "editing": bool(errors) and writable, "write": writable, "privacy": REVIEW_PRIVACY_WARNING}
    return toast(render(request, FIELD, ctx), words or f"{b['label']} saved")


# --- the sign-off -----------------------------------------------------------------------------------------------------------------

def _sign_blockers(r: VendorReview) -> list[str]:
    """What keeps a draft from being signed off, said before the button is offered (the service refuses the same, and the rest)."""
    out = []
    unrated = [label.lower() for f, label, _help in CRITERIA if not getattr(r, f)]
    if unrated:
        out.append(f"Rate {', '.join(unrated)} (not applicable is a rating).")
    if not r.overall:
        out.append("Choose the overall rating.")
    if not r.decision:
        out.append("Choose the decision.")
    elif r.decision == Decision.CORRECTIVE_PLAN and r.plan_due_on is None:
        out.append("Enter the day the corrective action plan is due.")
    if r.snapshot_id is None:
        out.append("Take the figures before signing off.")
    return out


def _sign_modal(request, r: VendorReview, form: SignOffForm, *, snapshot: str, today: date, blockers=()):
    figures_moved = bool(snapshot) and r.snapshot_id is not None and parse_uuid(snapshot) != r.snapshot_id
    ctx = {"r": r, "form": form, "snapshot": snapshot, "today": today, "action": review_url(r, "vendor_review_sign_off"),
           "taken_on": _day(r.snapshot.taken_on) if r.snapshot_id else "", "blockers": list(blockers),
           "moved": vr.FIGURES_CHANGED if figures_moved else "", "refusal": _figures_refusal(request.user, "Signing off a vendor review"),
           "period": vr.period_label(r.period_start, r.period_end), "plan": plan_status(r, today), "overall": r.get_overall_display(),
           "decision": r.get_decision_display()}
    return render(request, SIGN, ctx)


@reviews_view(perms.APPROVE_LEVEL)
def vendor_review_sign_off(request, number):
    """Sign off (Approve, a draft, and the figures' gate): GET the modal (?snapshot= the figures the page was drawn with; what keeps it
    from being signed, with no button), POST the approver's name and title (vendor_reviews.update_review) and vendor_reviews.sign_off
    with the snapshot id posted, both or neither."""
    r = _get(number)
    today = _today()
    if request.method != "POST":
        if not request.htmx:
            return _modal_get(request, r)
        snapshot = request.GET.get("snapshot", "") or (str(r.snapshot_id) if r.snapshot_id else "")
        initial = {"snapshot_id": snapshot, "signed_on": today.isoformat(), "approver_name": r.approver_name, "approver_title": r.approver_title}
        blockers = _sign_blockers(r) if r.status == ReviewStatus.DRAFT else []
        return _sign_modal(request, r, SignOffForm(initial=initial), snapshot=snapshot, today=today, blockers=blockers)
    form = SignOffForm(request.POST)
    done = False
    if form.is_valid():
        d = form.cleaned_data
        try:
            with transaction.atomic():
                names = {f: d[f] for f in ("approver_name", "approver_title") if f in request.POST}
                if names and r.status == ReviewStatus.DRAFT:
                    vr.update_review(r, by=request.user, **names)
                vr.sign_off(r, snapshot_id=d["snapshot_id"], signed_on=d["signed_on"], by=request.user, today=today)
            done = True
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not done:
        r = _get(r.number)
        if not request.htmx:
            return _modal_refused(request, r, _form_words(form))
        return _sign_modal(request, r, form, snapshot=request.POST.get("snapshot_id", ""), today=today)
    return _saved(request, r, f"{r.number} signed off")


# --- statuses ---------------------------------------------------------------------------------------------------------------------

def _confirm(request, r: VendorReview, *, action: str, title: str, words: list[str], button: str, error: str = "", danger: bool = False):
    return render(request, CONFIRM, {"r": r, "action": review_url(r, action), "title": title, "words": words, "button": button, "error": error,
                                     "danger": danger})


@reviews_view(perms.APPROVE_LEVEL)
def vendor_review_withdraw(request, number):
    """Withdraw (Approve, signed off): GET the modal, POST vendor_reviews.withdraw. It stays readable with its reports."""
    r = _get(number)
    args = {"action": "vendor_review_withdraw", "title": f"Withdraw {r.number}", "button": "Withdraw", "danger": True,
            "words": [f"{r.number} stays readable, with the committees it was reported to, marked withdrawn; it is no longer {r.vendor}'s "
                      "last review, and its 12 months are free for another review.",
                      "A withdrawn review is not signed off again: draft a new one instead."]}
    if request.method != "POST":
        return _confirm(request, r, **args) if request.htmx else _modal_get(request, r)
    try:
        vr.withdraw(r, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _confirm(request, r, **args, error=_words(e)) if request.htmx else _part(request, r, BODY, "#vr-body", refusal=_words(e))
    return _saved(request, r, f"{r.number} withdrawn")


@reviews_view(perms.EDIT_LEVEL)
def vendor_review_discard(request, number):
    """Discard (Edit, a draft): GET the modal, POST vendor_reviews.discard. Kept, never deleted."""
    r = _get(number)
    args = {"action": "vendor_review_discard", "title": f"Discard {r.number}", "button": "Discard", "danger": True,
            "words": [f"{r.number} is kept, marked discarded, and listed nowhere; its 12 months are free for another review.",
                      "A discarded review is not brought back: draft a new one instead."]}
    if request.method != "POST":
        return _confirm(request, r, **args) if request.htmx else _modal_get(request, r)
    try:
        vr.discard(r, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _confirm(request, r, **args, error=_words(e)) if request.htmx else _part(request, r, BODY, "#vr-body", refusal=_words(e))
    return _saved(request, r, f"{r.number} discarded")


# --- the corrective action plan's result ------------------------------------------------------------------------------------------

def _plan_modal(request, r: VendorReview, form: PlanForm, today: date):
    return render(request, PLAN, {"r": r, "form": form, "today": today, "results": PlanResult.choices, "plan": plan_status(r, today),
                                  "action": review_url(r, "vendor_review_plan"), "signed": _day(r.signed_on)})


@reviews_view(perms.EDIT_LEVEL)
def vendor_review_plan(request, number):
    """The corrective action plan's result (Edit, signed off with a plan): GET the modal, POST vendor_reviews.record_plan_result (a
    blank result clears one entered in error)."""
    r = _get(number)
    today = _today()
    if request.method != "POST":
        if not request.htmx:
            return _modal_get(request, r)
        initial = {"plan_result": r.plan_result, "plan_closed_on": (r.plan_closed_on or today).isoformat()}
        return _plan_modal(request, r, PlanForm(initial=initial), today)
    form = PlanForm(request.POST)
    if form.is_valid() and not form.cleaned_data["plan_result"] and not r.plan_result:  # nothing to clear: a result was not chosen
        form.add_error("plan_result", "Choose plan met, plan not met, or contract ended.")
    if form.is_valid():
        try:
            vr.record_plan_result(r, result=form.cleaned_data["plan_result"], closed_on=form.cleaned_data["plan_closed_on"], by=request.user,
                                  today=today)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _plan_modal(request, r, form, today) if request.htmx else _modal_refused(request, r, _form_words(form))
    cleared = not form.cleaned_data["plan_result"]
    return _saved(request, r, f"{r.number}: plan result cleared" if cleared else f"{r.number}: plan result recorded")


# --- the committees it was reported to --------------------------------------------------------------------------------------------

def _report_modal(request, r: VendorReview, form: ReportForm, today: date, *, report=None):
    action = review_url(r, "vendor_review_report", report.pk) if report is not None else review_url(r, "vendor_review_report_new")
    data = form.data if form.is_bound else form.initial
    return render(request, REPORT, {"r": r, "form": form, "report": report, "action": action, "today": today, "bodies": ReportBody.choices,
                                    "body": data.get("body", ""), "meeting_on": data.get("meeting_on", ""),
                                    "minutes": data.get("minutes_reference", ""), "signed": _day(r.signed_on)})


@reviews_view(perms.APPROVE_LEVEL)
def vendor_review_report_new(request, number):
    """Record a committee report (Approve, signed off): GET the modal, POST vendor_reviews.record_report for this review."""
    r = _get(number)
    today = _today()
    if request.method != "POST":
        if not request.htmx:
            return _modal_get(request, r, "vr-reports")
        return _report_modal(request, r, ReportForm(initial={"body": ReportBody.GOVERNING, "meeting_on": today.isoformat()}), today)
    form = ReportForm(request.POST)
    if form.is_valid():
        try:
            vr.record_report([r], by=request.user, today=today, **form.values())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _report_modal(request, r, form, today) if request.htmx else _modal_refused(request, r, _form_words(form))
    return _saved(request, r, "Committee report recorded", REPORTS, "#vr-reports")


@reviews_view(perms.APPROVE_LEVEL)
def vendor_review_report(request, number, pk):
    """Correct a committee report (Approve): GET the modal, POST vendor_reviews.correct_report."""
    r = _get(number)
    rep = _report(r, pk)
    today = _today()
    if request.method != "POST":
        if not request.htmx:
            return _modal_get(request, r, "vr-reports")
        initial = {"body": rep.body, "meeting_on": rep.meeting_on.isoformat(), "minutes_reference": rep.minutes_reference}
        return _report_modal(request, r, ReportForm(initial=initial), today, report=rep)
    form = ReportForm(request.POST)
    if form.is_valid():
        try:
            vr.correct_report(rep, by=request.user, today=today, **form.values())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _report_modal(request, r, form, today, report=rep) if request.htmx else _modal_refused(request, r, _form_words(form))
    return _saved(request, r, "Committee report corrected", REPORTS, "#vr-reports")


@require_POST
@reviews_view(perms.APPROVE_LEVEL)
def vendor_review_report_remove(request, number, pk):
    """Remove a committee report entered in error (Approve): vendor_reviews.remove_report. Its history keeps it."""
    r = _get(number)
    rep = _report(r, pk)
    try:
        vr.remove_report(rep, by=request.user)
    except (ValidationError, PermissionDenied) as e:
        return _part(request, r, REPORTS, "#vr-reports", refusal=_words(e), part="reports")
    return _part(request, r, REPORTS, "#vr-reports", "Committee report removed")


# --- the prints -------------------------------------------------------------------------------------------------------------------

@reviews_view(perms.VIEW_LEVEL)
def vendor_review_print(request, number):
    """The review's print, the contract owner's file (print_base): its stamp ("Draft", or "Signed off <day> by <name, title>") and who
    recorded it in Cadence, the figures through their gate (a copy without them says so), the reviewer's fields, the sign-off, and the
    committees it was reported to."""
    r = _get(number)
    today = _today()
    return render(request, PRINT, page_context(request, r, today=today))


def summary_rows(today: date, can_wo: bool) -> dict:
    """The contracted services summary: every vendor with a contract in force today, its contracts, its last signed-off review and that
    review's committee reports; the vendors with a contract and no signed-off review; the plans still open; the vendors without a
    contract in force with work in the last 12 months (a review of them is optional)."""
    rows = vr.vendors_list(today)
    lasts = [row.last_review for row in rows if row.last_review is not None]
    plans = {row.open_plan.pk: row.open_plan for row in rows if row.open_plan is not None}
    by_review = defaultdict(list)
    for rep in VendorReviewReport.objects.filter(review__in=lasts).order_by("meeting_on", "created_at"):
        by_review[rep.review_id].append(rep)
    in_force = []
    for row in rows:
        if not row.contracts:
            continue
        last = row.last_review
        in_force.append({"row": row, "name": row.name, "also": row.names[1:],
                         "contracts": [{"c": c, "coverage": c.get_coverage_display(), "term": f"{_day(c.start_on)} to {_day(c.end_on)}"}
                                       for c in row.contracts],
                         "last": _last_words(last, today), "approver": approver_words(last) if last is not None else "",
                         "reports": [{"body": rep.get_body_display(), "meeting": _day(rep.meeting_on), "minutes": rep.minutes_reference}
                                     for rep in by_review.get(last.pk, [])] if last is not None else [],
                         "next_due": _day(row.next_due), "overdue": row.overdue})
    open_plans = sorted(plans.values(), key=lambda p: (p.plan_due_on or date.max, p.number))
    others = [{"name": row.name, "jobs": row.jobs if can_wo else None, "last": _last_words(row.last_review, today)}
              for row in rows if not row.contracts and (row.contracted or row.jobs)]
    return {"in_force": in_force, "not_reviewed": [x for x in in_force if x["last"] is None],
            "open_plans": [{"r": p, "vendor": p.vendor, "plan": plan_status(p, today)} for p in open_plans], "others": others}


@reviews_view(perms.VIEW_LEVEL)
def vendor_reviews_print(request):
    """The contracted services summary (print_base), the governing body's document, as of today: every vendor with a contract in force
    (its contracts' reference, coverage, and term; its last review, overall, decision, plan status, and the committees it went to), the
    vendors not reviewed, the plans open, and the other vendors with work. No summary text and no figures."""
    today = _today()
    ctx = summary_rows(today, request.user.has_level(Module.WORKORDERS, Level.VIEW))
    return render(request, SUMMARY_PRINT, {**ctx, "today": today, "as_of": _day(today), "due_after": vr.DUE_AFTER_DAYS})


# --- the contract drawer's line ---------------------------------------------------------------------------------------------------

def review_line(user, vendor_name: str, today: date) -> dict | None:
    """The contract drawer's line for its vendor (Evaluations View with Contracts View; None for anyone else): "Last review VR-26-0003,
    partially meets, plan due Mar 31, 2027; next due Dec 29, 2027", or "Not reviewed; next due ...", linking to the last review or the
    Reviews tab filtered to the vendor. The same row as the tab's (vendor_reviews.vendors_list)."""
    if user is None or not perms.can_view_reviews(user):
        return None
    key = rules.vendor_key(vendor_name)
    tab = f"{reverse('web:vendor_reviews')}?{urlencode({'vendor': vendor_name})}"
    row = next((r for r in vr.vendors_list(today) if r.key == key), None) if key else None
    last = row.last_review if row is not None else None
    if last is not None:
        plan = plan_status(last, today)
        text = f"Last review {last.number}, {OVERALL_SHORT.get(last.overall, last.get_overall_display().lower())}, " + \
               (plan["short"] if plan else last.get_decision_display().lower())
        url = review_url(last)
    else:
        plan = None
        text, url = vr.NOT_REVIEWED, tab
    if row is not None and row.draft is not None:
        text += f"; draft {row.draft.number} open"
    if row is not None and row.next_due is not None:
        text += f"; next due {_day(row.next_due)}" + (" (overdue)" if row.overdue else "")
    warn = bool(row is not None and (row.overdue or row.plan_overdue)) or bool(plan and plan["overdue"])
    return {"text": text, "url": url, "warn": warn}
