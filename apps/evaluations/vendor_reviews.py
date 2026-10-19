"""
The review of each contracted service vendor (slice 34): every write, and the read helpers the Reviews tab, the review's page, the
prints, the binder, the seed, and the API share. Standalone: never slice 32's ProgramEvaluation models or services (their rules are
copied here in this review's own words).

The rules:
- A review names the vendor as work orders and contracts spell it: one or more names (each at most NAME_MAX characters, one per
  apps.vendors.rules.vendor_key, at most NAMES_MAX), the first shown; the keys are written only here and never change. Each key must
  name a contract in force on any day of the period, or a vendor job completed in it (any type, imported ones too), else the name is
  refused ("Phillips", or the blank key of "Vendor not named"). A contract is read from its history (contract_vendors), deleted ones
  included: each day by the version Cadence held at the end of that day, its vendor and term; a day before the contract was entered by
  the version it stood as at the end of the day it was entered (a contract entered at onboarding covers its past term; a typo fixed
  the same day never counts).
- Its period is the 12 months ending on period_end, any day that has ended (before the facility's today) but Feb 28 of a leap year
  (leap_feb_28: the 12 months after could not start on Feb 29): period_start_of(end) is the day after the end, 12 months back, so
  each period's next_period_end starts the day after it (Mar 1, 2024 to Feb 28, 2025, then Mar 1, 2025 to Feb 28, 2026). Its number
  is VR-<yy>-<nnnn> (Sequence "vendor-review-<yy>", yy from the period's end).
- No vendor key is in two live reviews (LIVE_REVIEW_STATUSES: draft, signed off) whose periods overlap: checked before the count and
  again under the facility's vendor review lock (lock_reviews: a Sequence row "vendor-reviews" that never counts), the partial unique
  constraint on a signed-off review's first key and start behind it. Withdrawn and discarded reviews free their period.
- A new draft is prefilled from the previous signed-off review naming any of its keys (the latest whose period ended before this one
  starts): the six ratings, whether the contract states the expectations, and the uptime and response targets (CARRIED).
- The figures (vendor_figures.snapshot, read-only) are taken when the draft is made and on Refresh figures (a draft only): counted
  outside any lock, then saved only if the review is still a draft with the snapshot the count started from (else FIGURES_CHANGED).
  Taking the figures, and signing them off, also needs their gate (permissions.can_see_vendor_figures: Reports View and Work orders
  View), so nobody signs or takes figures they may not read.
- draft -> approved ("Signed off", Approve): the approver posts the snapshot id they saw (else FIGURES_CHANGED) and the version of the
  reviewer's fields they saw (fields_version, else REVIEW_CHANGED); every criterion rated,
  overall and decision set; "does not meet" needs a corrective action plan, replacing or ending the contract, or bringing the service
  in-house; a corrective plan needs its due day after the sign-off, and only a plan has a due day; the approver's name; signed on a day
  not after today and not before the period's end. approved -> withdrawn (Approve: it stays readable, frees its period, its reports
  kept). A draft is discarded (Edit). Nothing signed off changes but its plan's result (Edit: met, not met, contract ended, closed on a
  day not after today nor before the sign-off; cleared as entered in error) and the committees it was reported to.
- A committee report (Approve): one meeting of one body for one or several signed-off reviews, in one transaction; the meeting on or
  after each one's sign-off and never after today; one report per review, body, and meeting day (each review's row locked, in id
  order, so two batches never write the same report twice). Corrected with the same checks; removed only as entered in error.

Every write: transaction.atomic, refusals as ValidationError in plain words (keyed by the model's field where a form has the field),
levels checked here for a user given as `by` (PermissionDenied; by=None is the system: the seed, tests), history with a _change_reason
(the snapshot has none: it is immutable). Today is the facility's, read once (timezone.localdate()) and passed down.

Privacy (CLAUDE.md non-negotiable 6): nothing here reads a requester's text; the summary is staff-written and length-limited
(REVIEW_SUMMARY_MAX, the form warns: REVIEW_PRIVACY_WARNING); nothing is emailed.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from apps.contracts.models import Contract
from apps.core.days import local_day
from apps.core.history import LABELS, _rows
from apps.core.models import Sequence
from apps.pm.dates import add_months
from apps.tenants.context import get_current_tenant
from apps.vendors import rules
from apps.workorders.models import WorkOrder, WoStatus

from . import permissions as perms
from .models import (
    CRITERIA,
    CRITERIA_FIELDS,
    LIVE_REVIEW_STATUSES,
    MINUTES_VALIDATOR,
    REPORT_REFERENCE_VALIDATOR,
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
    VendorReviewSnapshot,
)

LOCK = "vendor-reviews"  # the facility's vendor review lock (a Sequence row that never counts)
EARLIEST = date(2000, 1, 1)  # a year typed with a slip (1926, 0026) is refused
CHANGE_REASON_MAX = 100  # simple_history's history_change_reason column
DUE_AFTER_DAYS = 90  # next due: the next 12 months' review is signed off within 90 days after they end
NAME_MAX = Contract._meta.get_field("vendor").max_length  # 120, as contracts and work orders keep a vendor's name
NAMES_MAX = 10  # spellings of one vendor on one review
APPROVER_MAX = VendorReview._meta.get_field("approver_name").max_length
REFERENCE_MAX = VendorReview._meta.get_field("vendor_report_reference").max_length
MINUTES_MAX = VendorReviewReport._meta.get_field("minutes_reference").max_length
COUNT_MAX = 32767  # PositiveSmallIntegerField on every database
HUNDREDTH = Decimal("0.01")
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)  # a vendor job counts once completed (closed ones too), as the binder's vendors section

PCT_FIELDS = ("uptime_target_pct", "uptime_reported_pct", "uptime_lowest_pct", "response_target_pct", "response_reported_pct")
COUNT_FIELDS = ("systems_below", "systems_total")
CHOICE_FIELDS = {**{f: Rating for f in CRITERIA_FIELDS}, "stated_in_contract": StatedInContract, "overall": Overall,
                 "decision": Decision}
TEXT_LIMITS = {"vendor_report_reference": REFERENCE_MAX, "summary": REVIEW_SUMMARY_MAX, "approver_name": APPROVER_MAX,
               "approver_title": APPROVER_MAX}
# update_review's fields: everything the reviewer records on a draft (never the names, the period, the status, the sign-off's day, the
# plan's result: those have their own services)
UPDATE_FIELDS = (*CRITERIA_FIELDS, "stated_in_contract", *PCT_FIELDS, *COUNT_FIELDS, "vendor_report_reference", "overall", "decision",
                 "plan_due_on", "summary", "approver_name", "approver_title")
FIXED_FIELDS = ("vendor_names", "vendor_keys", "vendor_key", "period_start", "period_end", "number")
# What a new draft takes from the previous signed-off review of the same vendor
CARRIED = (*CRITERIA_FIELDS, "stated_in_contract", "uptime_target_pct", "response_target_pct")
REPORT_FIELDS = ("body", "meeting_on", "minutes_reference")
FIELD_LABELS = LABELS["evaluations.vendorreview"]  # the change log's words for each field (apps.core.history)
# Overall "does not meet" needs one of these decisions
ACTS_ON_FAILURE = (Decision.CORRECTIVE_PLAN, Decision.REPLACE, Decision.IN_HOUSE)

EDIT_PERMISSION = "Drafting and writing a vendor review, or recording its plan's result, needs Evaluations Edit and Contracts View."
APPROVE_PERMISSION = ("Signing off a vendor review, withdrawing one, or recording the committees it was reported to needs Evaluations "
                      "Approve and Contracts View.")
FIGURES_PERMISSION = "{doing} needs Reports View and Work orders View too: the figures are shown only with both. Ask someone who has them."
FIGURES_CHANGED = "The figures were taken again meanwhile. Read the new figures, then try again."
REVIEW_CHANGED = "The review was changed meanwhile. Read it again, then sign off."
SIGNED_FIELDS = (*CRITERIA_FIELDS, "stated_in_contract", *PCT_FIELDS, *COUNT_FIELDS, "vendor_report_reference", "overall", "decision",
                 "plan_due_on", "summary")  # what the approver signs beside the figures (fields_version); their name and title come with it
FIGURES_RULE = "only a draft's figures are taken again."
PCT_RULE = "Enter a percentage from 0 to 100, with at most two decimals."
CHOICE_WORDS = {
    **{f: "Choose met, partly met, not met, or not applicable." for f in CRITERIA_FIELDS},
    "stated_in_contract": "Choose yes, partly, or no.",
    "overall": "Choose meets, partially meets, or does not meet expectations.",
    "decision": "Choose renew, renew with changes, a corrective action plan, replace or end the contract, or bring the service in-house.",
}


# --- small helpers --------------------------------------------------------------------------------------------------------------


def _check(by, allowed, message: str) -> None:
    """The level check every write starts with, for a user given as `by` (None is the system: the seed, tests)."""
    if by is not None and not allowed(by):
        raise PermissionDenied(message)


def _check_figures(by, doing: str) -> None:
    """Taking or signing off the figures needs their gate (Reports View and Work orders View, all or nothing)."""
    if by is not None and not perms.can_see_vendor_figures(by):
        raise PermissionDenied(FIGURES_PERMISSION.format(doing=doing))


def _save(obj, by, reason: str, **kwargs) -> None:
    obj._change_reason = reason[:CHANGE_REASON_MAX]
    if by is not None:
        obj._history_user = by
    obj.save(**kwargs)


def _is_day(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _day_value(value):
    """A date from a date or its ISO text; None for nothing; the value itself when it is neither (the caller refuses it)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return value
    return value


def day_words(d: date) -> str:
    """"Oct 1, 2025"."""
    return f"{d:%b} {d.day}, {d.year}"


def period_label(start: date, end: date) -> str:
    """"Oct 1, 2025 to Sep 30, 2026"."""
    return f"{day_words(start)} to {day_words(end)}"


def period_start_of(end: date) -> date:
    """The first day of the 12 months ending on `end`: the day after it, 12 months back (month ends tile: Feb 28, 2025 -> Mar 1, 2024)."""
    return add_months(end + timedelta(days=1), -12)


def next_period_end(end: date) -> date:
    """The last day of the 12 months after a period ending on `end` (Feb 29, 2024 -> Feb 28, 2025; Feb 28, 2025 -> Feb 28, 2026)."""
    return add_months(end + timedelta(days=1), 12) - timedelta(days=1)


def _status_words(review: VendorReview) -> str:
    return ReviewStatus(review.status).label.lower()


def _locked(review) -> VendorReview:
    """The review's row as it is now, locked until the transaction ends."""
    return VendorReview.objects.select_for_update().get(pk=review.pk)


def _require_draft(review: VendorReview, rule: str) -> None:
    """Refuse unless the review is a draft: "VR-26-0001 is signed off: <rule>"."""
    if review.status != ReviewStatus.DRAFT:
        raise ValidationError(f"{review.number} is {_status_words(review)}: {rule}")


def lock_reviews() -> None:
    """Hold the facility's vendor review lock until the transaction ends: drafting (the overlap check and the number) takes it first,
    so two drafts at once wait here and the second reads what the first committed. A row of its own that nothing else locks (a
    Sequence row that never counts, as apps.pm.services.lock_planner), never slice 32's evaluation lock: the two never wait on each
    other. SQLite runs one writer at a time."""
    tenant = get_current_tenant()
    if tenant is None:
        raise RuntimeError("lock_reviews() needs a tenant context")
    try:
        with transaction.atomic():  # a savepoint: two first-ever requests may both try to create the row
            Sequence.unscoped.get_or_create(tenant=tenant, key=LOCK)  # unscoped + explicit tenant, as Sequence.next
    except IntegrityError:
        pass
    Sequence.unscoped.select_for_update().get(tenant=tenant, key=LOCK)  # unscoped + explicit tenant, as Sequence.next


def _number(period_end: date) -> str:
    yy = period_end.year % 100
    return f"VR-{yy:02d}-{Sequence.next(f'vendor-review-{yy:02d}'):04d}"


# --- the vendor's names and the period ------------------------------------------------------------------------------------------


def clean_names(names) -> tuple[list[str], list[str]]:
    """The vendor's names as typed (ends trimmed), one per vendor_key, the first shown, and their keys; refused keyed vendor_names: none,
    one longer than NAME_MAX (or whose key would be), more than NAMES_MAX."""
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)):
        raise ValidationError({"vendor_names": "Enter the vendor as your work orders and contracts name it."})
    kept, keys = [], []
    for name in names:
        text = str(name or "").strip()
        key = rules.vendor_key(text)
        if not key:
            continue
        if len(text) > NAME_MAX:
            raise ValidationError({"vendor_names": f"Keep each vendor name to {NAME_MAX} characters."})
        if len(key) > rules.KEY_MAX:  # letters that casefold longer ("ß") would overflow the stored key
            raise ValidationError({"vendor_names": "Shorten the vendor's name: it is too long to store."})
        if key not in keys:
            kept.append(text)
            keys.append(key)
    if not kept:
        raise ValidationError({"vendor_names": "Enter the vendor as your work orders and contracts name it."})
    if len(kept) > NAMES_MAX:
        raise ValidationError({"vendor_names": f"A review names at most {NAMES_MAX} spellings of the vendor."})
    return kept, keys


def leap_feb_28(d: date) -> bool:
    """Feb 28 of a leap year: no 12 months end there, since the 12 months after them would have to start on Feb 29 (period_start_of
    never does), so Feb 29 would fall in no review."""
    return d.month == 2 and d.day == 28 and calendar.isleap(d.year)


def _clean_period_end(period_end, today: date) -> tuple[date, date]:
    """(period_start, period_end), or a refusal keyed by period_end."""
    period_end = _day_value(period_end)
    if not _is_day(period_end):
        raise ValidationError({"period_end": "Enter the last day of the 12 months the review covers."})
    if period_end >= today:
        latest = "Feb 27" if leap_feb_28(today - timedelta(days=1)) else "yesterday"  # on Feb 29, yesterday is never an end
        raise ValidationError({"period_end": f"A review covers 12 months that have ended: choose {latest} or an earlier day."})
    if leap_feb_28(period_end):
        instead = (f"Feb 29, {period_end.year} or Feb 27" if period_end + timedelta(days=1) < today
                   else f"Feb 27, {period_end.year}, or wait until Feb 29 has ended")
        raise ValidationError({"period_end": f"End the 12 months on {instead}: the next 12 months could not start on Feb 29, which no "
                                             "review would then cover."})
    start = period_start_of(period_end) if period_end >= EARLIEST else None  # never 12 months before year 1
    if start is None or start < EARLIEST:
        raise ValidationError({"period_end": f"Choose 12 months from {EARLIEST.year} on."})
    return start, period_end


def _contract_windows(versions: list) -> list:
    """(first day, last day, version) for each version of one contract as Cadence held it: from the day it was saved to the day before
    the next save's day, the last version saved on a day standing for that day, and the first day's standing back to the beginning (it
    describes the contract as entered). A deletion ends the last version: from its day nothing stands."""
    standing: list = []  # (the facility's day, the last version saved that day), in order
    for v in versions:
        day = local_day(v["history_date"])
        if standing and standing[-1][0] == day:
            standing[-1] = (day, v)
        else:
            standing.append((day, v))
    windows = []
    for i, (day, v) in enumerate(standing):
        if v["history_type"] == "-":
            continue
        first = date.min if i == 0 else day
        last = standing[i + 1][0] - timedelta(days=1) if i + 1 < len(standing) else date.max
        windows.append((first, last, v))
    return windows


def contract_vendors(start: date, end: date) -> dict[str, list[str]]:
    """{vendor key: the names it was written} of the contracts in force on any day from `start` to `end` as Cadence held them each day
    (Contract history, deleted contracts included; _contract_windows): a day counts for a version's vendor when the version stood that
    day and its term covers it. A contract renamed mid-period counts for both names, each for its own days. One query."""
    history = _rows(Contract.history.model)
    touching = history.filter(start_on__lte=end, end_on__gte=start).values("id")  # a version whose term never touches the days can't count
    versions = (history.filter(id__in=touching).order_by("id", "history_date", "history_id")
                .values("id", "vendor", "start_on", "end_on", "history_date", "history_type"))
    by_contract = defaultdict(list)
    for v in versions:
        by_contract[v["id"]].append(v)
    found: dict[str, list[str]] = defaultdict(list)
    for rows in by_contract.values():
        for first, last, v in _contract_windows(rows):
            if max(first, v["start_on"], start) <= min(last, v["end_on"], end):
                key = rules.vendor_key(v["vendor"])
                if key and v["vendor"] not in found[key]:
                    found[key].append(v["vendor"])
    return {key: sorted(names, key=lambda n: (n.casefold(), n)) for key, names in found.items()}


def job_vendors(start: date, end: date) -> dict[str, Counter]:
    """{vendor key: Counter(name as written: jobs)} of the vendor jobs completed from `start` to `end` (work orders on vendor service,
    any type, completed or closed, imported ones too). One query."""
    rows = (WorkOrder.objects.filter(vendor_service=True, status__in=DONE, completed_on__gte=start, completed_on__lte=end)
            .values("vendor_name").annotate(n=Count("id")).order_by())
    found: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        key = rules.vendor_key(r["vendor_name"])
        if key:
            found[key][r["vendor_name"]] += r["n"]
    return dict(found)


def _refuse_unknown(names: list[str], keys: list[str], start: date, end: date) -> None:
    """Refuse a name whose key names no contract in force in the period and no vendor job completed in it (a typo, "Phillips")."""
    known = set(contract_vendors(start, end)) | set(job_vendors(start, end))
    unknown = [name for name, key in zip(names, keys) if key not in known]
    if unknown:
        raise ValidationError({"vendor_names": f"No contract in force and no vendor job completed from {period_label(start, end)} names "
                                               f"{' or '.join(unknown)}: write the vendor as your contracts and work orders do."})


def _overlapping(keys, start: date, end: date, exclude=None) -> list[VendorReview]:
    """The live reviews naming any of `keys` whose periods overlap start..end."""
    wanted = set(keys)
    qs = VendorReview.objects.filter(status__in=LIVE_REVIEW_STATUSES, period_start__lte=end, period_end__gte=start)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return [r for r in qs.order_by("period_start", "number") if wanted & set(r.vendor_keys or ())]


def _refuse_overlap(keys, start: date, end: date) -> None:
    found = _overlapping(keys, start, end)
    if not found:
        return
    r = next((r for r in found if r.status == ReviewStatus.APPROVED), found[0])
    shared = [n for n, k in zip(r.vendor_names, r.vendor_keys) if k in set(keys)] or [r.vendor]
    what = f"{r.number} ({_status_words(r)}) reviews {', '.join(shared)} for {period_label(r.period_start, r.period_end)}"
    if r.status == ReviewStatus.APPROVED:
        tail = "a vendor is in one review for any day. Choose other months, or withdraw it first."
    else:
        tail = "a vendor is in one review for any day. Finish or discard it, or choose other months."
    raise ValidationError({"period_end": f"{what}, which overlaps these 12 months: {tail}"})


# --- the figures ------------------------------------------------------------------------------------------------------------------


def _snapshot(keys, start: date, end: date, today: date) -> dict:
    """The figures for the vendor's keys and the period (slice 34's vendor_figures, read-only). Imported here, through the module, so
    the binder's vendors section may import this module's read helpers without a cycle (vendor_figures reads survey.vendors), and so a
    test may stand in for it."""
    from . import vendor_figures

    return vendor_figures.snapshot(list(keys), start, end, today=today)


def _take(review: VendorReview, data: dict, by, today: date, reason: str) -> VendorReviewSnapshot:
    """A new snapshot row for `review` (locked or just made by the caller) and the review pointing at it."""
    snap = VendorReviewSnapshot.objects.create(review=review, taken_at=timezone.now(), taken_on=today, taken_by=by, data=data)
    review.snapshot = snap
    _save(review, by, reason, update_fields=["snapshot", "updated_at"])
    return snap


def refresh(review, *, by, today: date | None = None) -> VendorReview:
    """Refresh figures (Edit, and the figures' gate): a draft's figures counted again. Counted outside any lock, then the review is
    locked and the new snapshot saved only if it is still a draft with the snapshot the count started from; else refused."""
    _check(by, perms.can_edit_reviews, EDIT_PERMISSION)
    _check_figures(by, "Taking a vendor review's figures")
    today = today or timezone.localdate()
    r = VendorReview.objects.get(pk=review.pk)
    _require_draft(r, FIGURES_RULE)
    started = r.snapshot_id
    data = _snapshot(r.vendor_keys, r.period_start, r.period_end, today)
    with transaction.atomic():
        current = _locked(r)
        _require_draft(current, FIGURES_RULE)
        if current.snapshot_id != started:
            raise ValidationError(FIGURES_CHANGED)
        _take(current, data, by, today, "Figures taken again")
    return current


# --- drafting ---------------------------------------------------------------------------------------------------------------------


def _previous(keys, before: date, exclude=None) -> VendorReview | None:
    """The latest signed-off review naming any of `keys` whose period ended before `before`."""
    wanted = set(keys)
    qs = VendorReview.objects.filter(status=ReviewStatus.APPROVED, period_end__lt=before)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return next((r for r in qs.order_by("-period_end", "-number") if wanted & set(r.vendor_keys or ())), None)


def create_review(*, vendor_names, period_end, by, today: date | None = None) -> VendorReview:
    """New review (Edit, and the figures' gate): a draft of the vendor's 12 months ending on `period_end`, numbered, prefilled from the
    previous signed-off review of the same vendor (CARRIED), its first figures taken."""
    _check(by, perms.can_edit_reviews, EDIT_PERMISSION)
    _check_figures(by, "Taking a vendor review's figures")
    today = today or timezone.localdate()
    errors: dict = {}
    names = keys = start = end = None
    try:
        names, keys = clean_names(vendor_names)
    except ValidationError as e:
        errors.update(e.message_dict)
    try:
        start, end = _clean_period_end(period_end, today)
    except ValidationError as e:
        errors.update(e.message_dict)
    if errors:
        raise ValidationError(errors)
    _refuse_unknown(names, keys, start, end)
    _refuse_overlap(keys, start, end)  # before the slow count; again under the lock
    data = _snapshot(keys, start, end, today)
    with transaction.atomic():
        lock_reviews()
        _refuse_overlap(keys, start, end)
        review = VendorReview(number=_number(end), vendor_names=names, vendor_keys=keys, vendor_key=keys[0], period_start=start,
                              period_end=end, created_by=by)
        source = _previous(keys, start)
        reason = "Drafted"
        if source is not None:
            for f in CARRIED:
                setattr(review, f, getattr(source, f))
            reason = f"Drafted, carried from {source.number}"
        _save(review, by, reason)
        _take(review, data, by, today, "Figures taken")
    return review


# --- the reviewer's fields --------------------------------------------------------------------------------------------------------


def _clean_pct(f: str, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValidationError({f: PCT_RULE})
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValidationError({f: PCT_RULE}) from None
    if not d.is_finite() or d < 0 or d > 100 or d != d.quantize(HUNDREDTH):
        raise ValidationError({f: PCT_RULE})
    return d.quantize(HUNDREDTH)


def _clean_count(f: str, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= COUNT_MAX:
        raise ValidationError({f: f"Enter a whole number of systems, from 0 to {COUNT_MAX:,}."})
    return value


def _clean_text(f: str, value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError({f: "Enter text."})
    text = value.strip()
    limit = TEXT_LIMITS[f]
    if len(text) > limit:
        raise ValidationError({f: f"At most {limit:,} characters ({len(text):,} entered)."})
    if f == "vendor_report_reference" and text:
        try:
            REPORT_REFERENCE_VALIDATOR(text)
        except ValidationError as e:
            raise ValidationError({f: e.messages[0]}) from None
    return text


def _clean_field(f: str, value):
    """One of update_review's fields checked on its own, keyed by the field."""
    if f in CHOICE_FIELDS:
        value = "" if value is None else value
        if value != "" and value not in CHOICE_FIELDS[f].values:
            raise ValidationError({f: CHOICE_WORDS[f]})
        return value
    if f in PCT_FIELDS:
        return _clean_pct(f, value)
    if f in COUNT_FIELDS:
        return _clean_count(f, value)
    if f == "plan_due_on":
        value = _day_value(value)
        if value is None:
            return None
        if not _is_day(value):
            raise ValidationError({f: "Enter the day the plan is due."})
        if value < EARLIEST:
            raise ValidationError({f: f"Enter a day from {EARLIEST.year} on."})
        return value
    return _clean_text(f, value)


def _check_together(r: VendorReview, given) -> dict:
    """The figures that are checked together, on the row with the change applied: the lowest system's uptime is not above the uptime
    reported, the systems below the guarantee are not more than the systems. Keyed by the field the change gave."""
    errors = {}
    if r.uptime_lowest_pct is not None and r.uptime_reported_pct is not None and r.uptime_lowest_pct > r.uptime_reported_pct:
        key = "uptime_reported_pct" if "uptime_reported_pct" in given and "uptime_lowest_pct" not in given else "uptime_lowest_pct"
        errors[key] = "The lowest system's uptime cannot be above the uptime the vendor reports for all of them."
    if r.systems_below is not None and r.systems_total is not None and r.systems_below > r.systems_total:
        key = "systems_total" if "systems_total" in given and "systems_below" not in given else "systems_below"
        errors[key] = "The systems below the guarantee cannot be more than the systems."
    return errors


def _changed_reason(words: list[str]) -> str:
    """"Overall changed", "Changed: Overall, Decision", or, past the history's reason column, "Changed: <the first that fit>, and 4
    more" (never cut mid-word; the entry lists every change)."""
    if len(words) == 1:
        return f"{words[0]} changed"
    whole = f"Changed: {', '.join(words)}"
    if len(whole) <= CHANGE_REASON_MAX:
        return whole
    for n in range(len(words) - 1, 0, -1):
        text = f"Changed: {', '.join(words[:n])}, and {len(words) - n} more"
        if len(text) <= CHANGE_REASON_MAX:
            return text
    return f"Changed {len(words)} fields"


def update_review(review, *, by, **fields) -> VendorReview:
    """Save the fields given on a draft (Edit): the ratings, whether the contract states the expectations, the vendor's reported figures
    (percentages from 0 to 100 with two decimals, systems counts; the lowest not above the reported, the systems below not more than the
    systems), the vendor's report reference (a token), overall, decision, the plan's due day, the summary (REVIEW_SUMMARY_MAX), the
    approver's name and title (APPROVER_MAX each). A value of None or "" clears a field. Only the fields given are written, so two people
    writing different fields never overwrite each other; nothing changed writes nothing."""
    _check(by, perms.can_edit_reviews, EDIT_PERMISSION)
    fixed = sorted(set(fields) & set(FIXED_FIELDS))
    if fixed:
        raise ValidationError("A review's vendor names and period are fixed when it is drafted: discard it and draft another.")
    unknown = sorted(set(fields) - set(UPDATE_FIELDS))
    if unknown:
        raise ValidationError(f"A vendor review has no {', '.join(unknown)} to change here.")
    errors: dict = {}
    cleaned = {}
    for f, value in fields.items():
        try:
            cleaned[f] = _clean_field(f, value)
        except ValidationError as e:
            errors.update(e.message_dict)
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        r = _locked(review)
        _require_draft(r, "only a draft is changed." + (" Withdraw it and draft another to change it." if r.status == ReviewStatus.APPROVED
                                                         else ""))
        changed = [f for f, value in cleaned.items() if getattr(r, f) != value]
        for f in changed:
            setattr(r, f, cleaned[f])
        together = _check_together(r, cleaned)
        if together:
            raise ValidationError(together)
        if changed:
            _save(r, by, _changed_reason([FIELD_LABELS.get(f, f) for f in changed]), update_fields=[*changed, "updated_at"])
    return r


# --- the sign-off -----------------------------------------------------------------------------------------------------------------


def fields_version(review: VendorReview) -> str:
    """The reviewer's fields the approver signs (SIGNED_FIELDS) as a short token: the sign-off modal posts the one it was drawn with,
    and sign_off refuses when they changed since (REVIEW_CHANGED), as the snapshot id guards the figures."""
    values = {f: getattr(review, f) for f in SIGNED_FIELDS}
    text = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _same_id(posted, stored) -> bool:
    if posted is None or stored is None:
        return False
    try:
        return uuid.UUID(str(posted)) == uuid.UUID(str(stored))
    except (TypeError, ValueError, AttributeError):
        return False


def _signed_on_errors(signed_on, r: VendorReview, today: date) -> str:
    if not _is_day(signed_on):
        return "Enter the day the review was signed off."
    if signed_on > today:
        return "The sign-off cannot be dated after today."
    if signed_on < r.period_end:
        return f"The period ends on {day_words(r.period_end)}: the sign-off is on or after that day."
    return ""


def sign_off(review, *, snapshot_id, signed_on, by, today: date | None = None, version: str | None = None) -> VendorReview:
    """Sign off a draft (Approve, and the figures' gate): `snapshot_id` is the figures the approver saw (refused if they were taken
    again since: FIGURES_CHANGED), and `version` the reviewer's fields they saw (fields_version; refused if any changed since:
    REVIEW_CHANGED; None only where nobody was shown a page: the seed, a test); every criterion rated, overall and decision set;
    overall "does not meet" needs a corrective action plan, replacing or ending the contract, or bringing the service in-house; a
    corrective action plan needs its due day after the sign-off, and only a plan has a due day; the approver's name; `signed_on` not
    after today and not before the period's end. The review is then signed off (status approved) by whoever recorded it (signed_by).
    Problems with the fields come keyed by field, all at once."""
    _check(by, perms.can_approve_reviews, APPROVE_PERMISSION)
    _check_figures(by, "Signing off a vendor review")
    today = today or timezone.localdate()
    signed_on = _day_value(signed_on)
    with transaction.atomic():
        r = _locked(review)
        _require_draft(r, "only a draft is signed off.")
        if r.snapshot_id is None:
            raise ValidationError("Take the figures before signing off.")
        if not _same_id(snapshot_id, r.snapshot_id):
            raise ValidationError(FIGURES_CHANGED)
        if version is not None and str(version) != fields_version(r):
            raise ValidationError(REVIEW_CHANGED)
        errors = {}
        for f, label, _help in CRITERIA:
            if not getattr(r, f):
                errors[f] = f"Rate {label.lower()} (not applicable is a rating)."
        if not r.overall:
            errors["overall"] = "Choose the overall rating."
        if not r.decision:
            errors["decision"] = "Choose the decision."
        elif r.overall == Overall.DOES_NOT_MEET and r.decision not in ACTS_ON_FAILURE:
            errors["decision"] = ("A vendor that does not meet expectations needs a corrective action plan, replacing or ending the "
                                  "contract, or bringing the service in-house.")
        when = _signed_on_errors(signed_on, r, today)
        if when:
            errors["signed_on"] = when
        if r.decision == Decision.CORRECTIVE_PLAN:
            if r.plan_due_on is None:
                errors["plan_due_on"] = "Enter the day the corrective action plan is due."
            elif not when and r.plan_due_on <= signed_on:
                errors["plan_due_on"] = f"The plan is due after the day it is signed off ({day_words(signed_on)})."
        elif r.plan_due_on is not None:
            errors["plan_due_on"] = "Only a corrective action plan has a due day: clear it, or choose a corrective action plan."
        if not r.approver_name:
            errors["approver_name"] = "Enter the approver's name as they sign."
        if errors:
            raise ValidationError(errors)
        r.status, r.signed_on, r.signed_by = ReviewStatus.APPROVED, signed_on, by
        try:
            with transaction.atomic():
                _save(r, by, "Signed off")
        except IntegrityError:
            raise ValidationError(f"Another review of {r.vendor} for the 12 months from {day_words(r.period_start)} is signed off "
                                  "already.") from None
    return r


def record_plan_result(review, *, result, closed_on, by, today: date | None = None) -> VendorReview:
    """The corrective action plan's result (Edit) on a signed-off review with a plan: met, not met, or contract ended (PlanResult),
    closed on a day not after today and not before the sign-off. Recording again replaces it (its history keeps the earlier); a result
    of "" clears it (entered in error), the plan open again. Errors keyed plan_result and plan_closed_on."""
    _check(by, perms.can_edit_reviews, EDIT_PERMISSION)
    today = today or timezone.localdate()
    result = result or ""
    closed_on = _day_value(closed_on)
    errors = {}
    if result and result not in PlanResult.values:
        errors["plan_result"] = "Choose plan met, plan not met, or contract ended."
    if result:
        if not _is_day(closed_on):
            errors["plan_closed_on"] = "Enter the day the plan was closed."
        elif closed_on > today:
            errors["plan_closed_on"] = "The plan cannot be closed after today."
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        r = _locked(review)
        if r.status != ReviewStatus.APPROVED or r.decision != Decision.CORRECTIVE_PLAN:
            what = "is signed off without a corrective action plan" if r.status == ReviewStatus.APPROVED else f"is {_status_words(r)}"
            raise ValidationError(f"{r.number} {what}: only a signed-off review with a corrective action plan has a plan result.")
        if not result:
            if not r.plan_result:
                return r
            r.plan_result, r.plan_closed_on = "", None
            _save(r, by, "Plan result cleared: entered in error")
            return r
        if closed_on < r.signed_on:
            raise ValidationError({"plan_closed_on": f"{r.number} was signed off on {day_words(r.signed_on)}: its plan closes on or after "
                                                     "that day."})
        if (r.plan_result, r.plan_closed_on) != (result, closed_on):
            r.plan_result, r.plan_closed_on = result, closed_on
            _save(r, by, f"Plan result: {PlanResult(result).label}")
    return r


# --- statuses ---------------------------------------------------------------------------------------------------------------------


def withdraw(review, *, by) -> VendorReview:
    """Withdraw (Approve) a signed-off review: it stays readable with its reports, and its period is free for another review."""
    _check(by, perms.can_approve_reviews, APPROVE_PERMISSION)
    with transaction.atomic():
        r = _locked(review)
        if r.status == ReviewStatus.DRAFT:
            raise ValidationError(f"{r.number} is a draft: discard it instead.")
        if r.status != ReviewStatus.APPROVED:
            raise ValidationError(f"{r.number} is {_status_words(r)} already.")
        r.status = ReviewStatus.WITHDRAWN
        _save(r, by, "Withdrawn")
    return r


def discard(review, *, by) -> VendorReview:
    """Discard (Edit) a draft: kept, never deleted; its period is free again. A signed-off review is withdrawn instead."""
    _check(by, perms.can_edit_reviews, EDIT_PERMISSION)
    with transaction.atomic():
        r = _locked(review)
        if r.status == ReviewStatus.APPROVED:
            raise ValidationError(f"{r.number} is signed off: withdraw it instead (Evaluations Approve).")
        if r.status != ReviewStatus.DRAFT:
            raise ValidationError(f"{r.number} is {_status_words(r)} already." if r.status == ReviewStatus.DISCARDED
                                  else f"{r.number} is {_status_words(r)}: only a draft is discarded.")
        r.status = ReviewStatus.DISCARDED
        _save(r, by, "Discarded")
    return r


# --- the committees it was reported to --------------------------------------------------------------------------------------------


def _clean_report(values: dict, today: date) -> dict:
    """A report's fields checked, keyed by field: the body, the meeting day (not after today), the minutes' reference (a token)."""
    errors = {}
    body = values.get("body")
    if body not in ReportBody.values:
        errors["body"] = "Choose the committee: quality oversight (QAPI) or the governing body."
    meeting_on = _day_value(values.get("meeting_on"))
    if not _is_day(meeting_on):
        errors["meeting_on"] = "Enter the day the committee met."
    elif meeting_on > today:
        errors["meeting_on"] = "The meeting cannot be after today: record the report once the committee has met."
    elif meeting_on < EARLIEST:
        errors["meeting_on"] = f"Enter a day from {EARLIEST.year} on."
    minutes = values.get("minutes_reference")
    minutes = "" if minutes is None else minutes
    if not isinstance(minutes, str):
        errors["minutes_reference"] = "Enter the minutes' reference."
    else:
        minutes = minutes.strip()
        if len(minutes) > MINUTES_MAX:
            errors["minutes_reference"] = f"At most {MINUTES_MAX} characters ({len(minutes)} entered)."
        elif minutes:
            try:
                MINUTES_VALIDATOR(minutes)
            except ValidationError as e:
                errors["minutes_reference"] = e.messages[0]
    if errors:
        raise ValidationError(errors)
    return {"body": body, "meeting_on": meeting_on, "minutes_reference": minutes}


BODY_WORDS = {ReportBody.QUALITY: "quality oversight committee (QAPI)", ReportBody.GOVERNING: "governing body"}  # "reported to the ..."


def _body_words(body: str) -> str:
    return BODY_WORDS.get(body, body)


def record_report(reviews, *, body, meeting_on, minutes_reference: str = "", by, today: date | None = None) -> list[VendorReviewReport]:
    """Record one committee meeting (Approve) that received one or several signed-off reviews, in one transaction: each review signed
    off on or before the meeting, the meeting not after today, and no review already recorded as reported to that body that day. All or
    nothing. Each review's row is locked (in id order) while its report is written, so two batches at once never write one twice."""
    _check(by, perms.can_approve_reviews, APPROVE_PERMISSION)
    today = today or timezone.localdate()
    values = _clean_report({"body": body, "meeting_on": meeting_on, "minutes_reference": minutes_reference}, today)
    if isinstance(reviews, VendorReview):
        reviews = [reviews]
    ids = list(dict.fromkeys(r.pk for r in reviews or ()))
    if not ids:
        raise ValidationError({"reviews": "Choose the reviews the committee received."})
    with transaction.atomic():
        locked = list(VendorReview.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        if len(locked) != len(ids):
            raise ValidationError({"reviews": "Choose reviews of this facility."})
        locked.sort(key=lambda r: r.number)
        not_signed = [f"{r.number} is {_status_words(r)}: only a signed-off review is reported to a committee." for r in locked
                      if r.status != ReviewStatus.APPROVED]
        if not_signed:
            raise ValidationError({"reviews": not_signed})
        early = [f"{r.number} was signed off on {day_words(r.signed_on)}: the meeting that received it is on or after that day."
                 for r in locked if values["meeting_on"] < r.signed_on]
        if early:
            raise ValidationError({"meeting_on": early})
        twice = (VendorReviewReport.objects.filter(review__in=locked, body=values["body"], meeting_on=values["meeting_on"])
                 .select_related("review").order_by("review__number"))
        already = [f"{rep.review.number} is recorded as reported to the {_body_words(rep.body)} on {day_words(rep.meeting_on)} "
                   "already." for rep in twice]
        if already:
            raise ValidationError({"reviews": already})
        reports = []
        for r in locked:
            rep = VendorReviewReport(review=r, recorded_by=by, **values)
            _save(rep, by, f"Reported to the {_body_words(values['body'])}")
            reports.append(rep)
    return reports


def _locked_report(report) -> tuple[VendorReview, VendorReviewReport]:
    """The report's review and the report, locked in that order (record_report's order: the review first)."""
    gone = ValidationError("That report is not on file: it was removed meanwhile.")
    review_id = VendorReviewReport.objects.filter(pk=report.pk).values_list("review_id", flat=True).first()
    if review_id is None:
        raise gone
    r = VendorReview.objects.select_for_update().get(pk=review_id)
    rep = VendorReviewReport.objects.select_for_update().filter(pk=report.pk).first()
    if rep is None:  # removed while this waited for the review's lock
        raise gone
    return r, rep


def correct_report(report, *, by, today: date | None = None, **fields) -> VendorReviewReport:
    """Correct a report's body, meeting day, or minutes' reference (Approve), with the checks of recording it: the meeting on or after
    the review's sign-off and not after today, and no other report of that review to that body that day. Its history keeps what it
    said before."""
    _check(by, perms.can_approve_reviews, APPROVE_PERMISSION)
    unknown = sorted(set(fields) - set(REPORT_FIELDS))
    if unknown:
        raise ValidationError(f"A committee report has no {', '.join(unknown)}.")
    today = today or timezone.localdate()
    with transaction.atomic():
        r, rep = _locked_report(report)
        values = _clean_report({**{f: getattr(rep, f) for f in REPORT_FIELDS}, **fields}, today)
        if r.signed_on and values["meeting_on"] < r.signed_on:
            raise ValidationError({"meeting_on": f"{r.number} was signed off on {day_words(r.signed_on)}: the meeting that received it is "
                                                 "on or after that day."})
        if (VendorReviewReport.objects.filter(review=r, body=values["body"], meeting_on=values["meeting_on"]).exclude(pk=rep.pk)
                .exists()):
            raise ValidationError({"meeting_on": f"{r.number} is recorded as reported to the {_body_words(values['body'])} on "
                                                 f"{day_words(values['meeting_on'])} already."})
        changed = [f for f in REPORT_FIELDS if getattr(rep, f) != values[f]]
        if changed:
            for f in changed:
                setattr(rep, f, values[f])
            _save(rep, by, "Report corrected")
    return rep


def remove_report(report, *, by) -> None:
    """Remove a report entered in error (Approve). Its history keeps it."""
    _check(by, perms.can_approve_reviews, APPROVE_PERMISSION)
    with transaction.atomic():
        _r, rep = _locked_report(report)
        rep._change_reason = "Removed: entered in error"
        if by is not None:
            rep._history_user = by
        rep.delete()


# --- reading ----------------------------------------------------------------------------------------------------------------------


def previous_approved(review) -> VendorReview | None:
    """The latest signed-off review naming any of this review's vendor keys whose period ended before this one starts (the one a new
    draft was prefilled from)."""
    return _previous(review.vendor_keys or (), review.period_start, exclude=review.pk)


def current(number: str) -> VendorReview | None:
    """The review numbered `number` (VR-26-0001, any letter case) in this facility, or None."""
    number = str(number or "").strip().upper()
    if not number:
        return None
    return VendorReview.objects.filter(number=number).select_related("snapshot").first()


NOT_REVIEWED = "Not reviewed"  # a vendor row with no signed-off review (VendorRow.reviewed false)


@dataclass
class VendorRow:
    """One vendor key on the Reviews tab."""
    key: str
    name: str  # shown: a contract in force today's spelling, else the latest review's, else the most jobs', else a contract's
    names: list = field(default_factory=list)  # every spelling seen, the shown first
    contracts: list = field(default_factory=list)  # its contracts in force today (Contract rows), ending first
    contracted: bool = False  # a contract in force today or on any day of the last 12 months: its review is expected
    jobs: int = 0  # vendor jobs completed in the last 12 months
    last_review: VendorReview | None = None  # the latest signed off, by its period's end
    draft: VendorReview | None = None  # a draft naming it
    open_plan: VendorReview | None = None  # the latest signed-off review with a corrective action plan and no result yet
    next_due: date | None = None
    overdue: bool = False  # next_due is before today
    plan_overdue: bool = False  # the open plan's due day is before today

    @property
    def reviewed(self) -> bool:
        return self.last_review is not None


def _next_due(last: VendorReview | None, contracts: list) -> date | None:
    """Whichever comes first: the next 12 months' review, due DUE_AFTER_DAYS after they end, and the end of a contract in force (a
    review before each renewal) that no signed-off review's period ended in the 12 months before (the review done for that renewal
    covers it). None for a vendor never reviewed with no contract in force."""
    candidates = []
    if last is not None:
        candidates.append(next_period_end(last.period_end) + timedelta(days=DUE_AFTER_DAYS))
    for c in contracts:
        if last is None or last.period_end < add_months(c.end_on, -12):
            candidates.append(c.end_on)
    return min(candidates) if candidates else None


def vendors_list(today: date | None = None) -> list[VendorRow]:
    """Every vendor (by vendor_key) with a contract in force today or on any day of the 12 months through today (contract_vendors),
    vendor jobs completed in those 12 months, or a review that is not discarded: its names, its contracts in force today, its last
    signed-off review, a draft, its open plan, and when the next review is due (_next_due; None and "not reviewed" for a vendor with
    neither a signed-off review nor a contract in force). The blank key ("Vendor not named") is never listed. Ordered by the day next due,
    then the vendors with none, by name. Four queries."""
    today = today or timezone.localdate()
    since = add_months(today + timedelta(days=1), -12)
    contracted = contract_vendors(since, today)
    jobs = job_vendors(since, today)
    in_force = list(Contract.objects.filter(start_on__lte=today, end_on__gte=today).order_by("end_on", "reference"))
    reviews = list(VendorReview.objects.exclude(status=ReviewStatus.DISCARDED).order_by("-period_end", "-number"))
    contracts_of = defaultdict(list)
    for c in in_force:
        contracts_of[rules.vendor_key(c.vendor)].append(c)
    keys = set(contracted) | set(jobs) | set(contracts_of) | {k for r in reviews for k in (r.vendor_keys or ())}
    keys.discard("")
    rows = []
    for key in keys:
        mine = [r for r in reviews if key in (r.vendor_keys or ())]
        last = next((r for r in mine if r.status == ReviewStatus.APPROVED), None)
        open_plan = next((r for r in mine if r.status == ReviewStatus.APPROVED and r.decision == Decision.CORRECTIVE_PLAN
                          and not r.plan_result), None)
        draft = next((r for r in mine if r.status == ReviewStatus.DRAFT), None)
        review_names = [n for r in mine for n, k in zip(r.vendor_names or (), r.vendor_keys or ()) if k == key]
        job_names = [n for n, _count in sorted(jobs.get(key, Counter()).items(), key=lambda kv: (-kv[1], kv[0].casefold(), kv[0]))]
        current_names = [c.vendor for c in contracts_of.get(key, ())]
        names = list(dict.fromkeys([*current_names, *review_names, *job_names, *contracted.get(key, ())]))
        row = VendorRow(key=key, name=names[0] if names else key, names=names, contracts=contracts_of.get(key, []),
                        contracted=key in contracted or key in contracts_of, jobs=sum(jobs.get(key, Counter()).values()), last_review=last,
                        draft=draft, open_plan=open_plan)
        row.next_due = _next_due(last, row.contracts)
        row.overdue = row.next_due is not None and row.next_due < today
        row.plan_overdue = bool(open_plan is not None and open_plan.plan_due_on and open_plan.plan_due_on < today)
        rows.append(row)
    rows.sort(key=lambda row: (row.next_due is None, row.next_due or date.max, row.name.casefold(), row.name))
    return rows
