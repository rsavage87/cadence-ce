"""
The one rule for test equipment (slice 35): every door reads it (the completion modal's options, the work order's drawer row and print,
the item's page, its CSVs, the binder, the API). Read only; no writes here.

Standing on a day (standing_on): from the item's live calibrations (not entered in error), ordered by calibrated_on, never by entry,
the latest with calibrated_on on or before the day governs:
- none: NO_RECORD, "No calibration on file";
- failed: FAILED, "Failed calibration Oct 3, 2026" (since its day);
- passed, due_on on or after the day: IN_DATE, "Calibration due Mar 31, 2027" ("Verification due" for Program.VERIFICATION): usable
  through its due day;
- passed, due_on before the day: PAST_DUE, "Calibration was due Aug 31, 2026" (past due from due_on + 1).
Each record is judged by its own dates whenever it was entered (slice 31's rule); when the governing record (for NO_RECORD, the first
record on file) was entered after the day, the words disclose it: "(on file since Oct 5, 2026)" (its history's first row's facility
day, core.days.local_day); for NO_RECORD, "(first certificate on file since Oct 5, 2026)": nothing was on file for that day, and the
reader learns when the item's records begin. Dates as apps.vendors.rules.fmt.

Found out of tolerance later: a calibration that needs a review (Calibration.needs_review: out of tolerance as found, or failed) puts in
question the work done with the item in its look-back window (window): after the latest live passed calibration before it (exclusive:
work on that day stands on that certificate) through its own day (inclusive: work that day before it shipped); with no earlier passed
calibration, every use on or before its day. A use in a live window reads "found out of tolerance on <day>", then its review's state:
not reviewed; reviewed (the basis's words), when the use is in the review's frozen list; or added since the review (a binder GAP).

A use counts (counted_uses_q, a positive condition: never negate it) when its work order is completed or closed, not imported, and either
a repair whose outcome is not the vendor's report, or not vendor service; its day is the work order's completed_on (a reopened work order
has none, and its uses count again when it is completed again, on the new day). offered(wo, user) says whether a work order may record
test equipment at all; usable_today(item, today) whether an item may be newly recorded today; may_remove whether a recorded use may be
taken off (the kept-use rule: never once a live calibration dated on or after the work's day is on file: the use has been judged since;
for a reopened work order the day is the one it was last completed on, work_day).

Batches (a fixed number of queries, whatever the number of items or uses): calibrations_by_item (one query), entered_days (one),
reviews_by_calibration (one), then standings (items on one day) and judge_uses (uses on their work's days, with "found out of tolerance
later") in Python. The list, the CSV, the API, the binder, and the work order's row and list all read through them.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Min, Q

from apps.vendors.rules import fmt
from apps.workorders.models import RepairCheck, Source, WoStatus, WoType

from .models import Basis, Program, Result, Status

NO_RECORD = "no_record"
IN_DATE = "in_date"
PAST_DUE = "past_due"
FAILED = "failed"

# Found out of tolerance later: the review's state for one use (FoundLater.state)
NOT_REVIEWED, REVIEWED, ADDED = "not_reviewed", "reviewed", "added"

DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
COUNTED_SOURCES = tuple(s for s in Source.values if s != Source.IMPORTED)  # positive: every source but an import
COUNTED_REPAIR_CHECKS = ("", RepairCheck.PASSED, RepairCheck.NOT_RETURNED)  # every outcome but the vendor's report ("" before slice 30)
RECHECK_TYPES = (WoType.PM, WoType.INSPECTION, WoType.SAFETY)  # with a repair checked and ready for use: work that rechecks a device

# The list's standing filters (the list, its CSV, and the API share them: matches)
DUE_SOON_DAYS = 60
FILTERS = {"past_due": "Past due", "due_soon": f"Due within {DUE_SOON_DAYS} days", "no_record": "No calibration on file", "failed": "Failed"}


@dataclass(frozen=True)
class Standing:
    """An item's standing on a day: `state` (NO_RECORD, IN_DATE, PAST_DUE, FAILED), the governing calibration (None for NO_RECORD),
    its due day (IN_DATE, PAST_DUE), `since` (PAST_DUE: due_on + 1; FAILED: its day), `entered_after` (the facility day the governing
    record, or for NO_RECORD the first record, was entered, when after the day judged; else None), and `words`. `plain` is the words
    without the "on file since" disclosure (use_words puts the day of the work between them)."""
    state: str
    calibration: object | None
    due_on: date | None
    since: date | None
    entered_after: date | None
    words: str
    plain: str = ""

    @property
    def flagged(self) -> bool:
        """Anything but IN_DATE: the binder's FINDING and the modal's warning."""
        return self.state != IN_DATE


def live(calibrations) -> list:
    """The records not entered in error."""
    return [c for c in calibrations if not c.entered_in_error]


def _by_day(calibrations) -> list:
    """The live records by calibrated_on (one live record per item and day: the id only orders a list a caller mixed)."""
    return sorted(live(calibrations), key=lambda c: (c.calibrated_on, str(c.pk)))


def noun(item) -> str:
    """"Calibration", or "Verification" for an item whose certificates verify it (a SpO2 simulator)."""
    return "Verification" if item.program == Program.VERIFICATION else "Calibration"


def _entered(record, day: date, entered: dict | None) -> date | None:
    if record is None or entered is None:
        return None
    on = entered.get(record.pk)
    return on if on is not None and on > day else None


def standing_on(item, day: date, calibrations, *, entered: dict | None = None) -> Standing:
    """`item`'s standing on `day` from `calibrations` (its records, any order; those entered in error are left out here). `entered`:
    {calibration id: the facility day it was entered} (entered_days) for the "on file since" disclosure; None leaves it out."""
    records = [c for c in _by_day(calibrations) if c.test_equipment_id == item.pk]
    governing = None
    for c in records:
        if c.calibrated_on > day:
            break
        governing = c
    what = noun(item)
    if governing is None:
        after = _entered(records[0] if records else None, day, entered)
        plain = f"No {what.lower()} on file"
        words = plain + (f" (first certificate on file since {fmt(after)})" if after else "")
        return Standing(NO_RECORD, None, None, None, after, words, plain)
    after = _entered(governing, day, entered)
    since_words = f" (on file since {fmt(after)})" if after else ""
    if governing.result == Result.FAILED:
        plain = f"Failed {what.lower()} {fmt(governing.calibrated_on)}"
        return Standing(FAILED, governing, None, governing.calibrated_on, after, plain + since_words, plain)
    if governing.due_on >= day:
        plain = f"{what} due {fmt(governing.due_on)}"
        return Standing(IN_DATE, governing, governing.due_on, None, after, plain + since_words, plain)
    plain = f"{what} was due {fmt(governing.due_on)}"
    return Standing(PAST_DUE, governing, governing.due_on, governing.due_on + timedelta(days=1), after, plain + since_words, plain)


def entered_days(calibrations) -> dict:
    """{calibration id: the facility day its history's first row was written} for `calibrations`, in one query (core.history._rows).
    A record with no history (none is written without it) falls back to its own created_at."""
    from apps.core.days import local_day
    from apps.core.history import _rows

    from .models import Calibration

    records = list(calibrations)
    if not records:
        return {}
    firsts = dict(_rows(Calibration.history.model).filter(id__in=[c.pk for c in records]).order_by().values("id")
                  .annotate(first=Min("history_date")).values_list("id", "first"))
    out = {}
    for c in records:
        at = firsts.get(c.pk) or c.created_at
        out[c.pk] = local_day(at) if at else None
    return out


def window(calibration, calibrations) -> tuple[date | None, date]:
    """(start, end) of the look-back window of `calibration` (one that needs a review) among the item's `calibrations`: start the
    latest live passed calibration's day before it (exclusive), or None; end its own day (inclusive)."""
    before = [c.calibrated_on for c in live(calibrations)
              if c.pk != calibration.pk and c.test_equipment_id == calibration.test_equipment_id and c.result == Result.PASSED
              and c.calibrated_on < calibration.calibrated_on]
    return (max(before) if before else None, calibration.calibrated_on)


def in_window(day: date | None, bounds: tuple[date | None, date]) -> bool:
    """Whether work done on `day` falls in the window `bounds` (window's): after its start (exclusive), through its end (inclusive)."""
    start, end = bounds
    return day is not None and day <= end and (start is None or day > start)


def window_q(bounds: tuple[date | None, date], prefix: str = "") -> Q:
    """The window as a Q on the work order's completed_on (`prefix` reaches the work order: "work_order__" from a use)."""
    start, end = bounds
    q = Q(**{f"{prefix}completed_on__lte": end})
    return q & Q(**{f"{prefix}completed_on__gt": start}) if start is not None else q


def counted_work_q(prefix: str = "") -> Q:
    """The work orders whose test equipment counts (the module docstring), as a Q on WorkOrder (prefix "") or on a model reaching it
    through `prefix`. Positive conditions only: never negate it."""
    p = prefix
    in_house = Q(**{f"{p}vendor_service": False})
    checked_by_us = Q(**{f"{p}type": WoType.REPAIR, f"{p}repair_check__in": COUNTED_REPAIR_CHECKS})
    return Q(**{f"{p}status__in": DONE, f"{p}completed_on__isnull": False, f"{p}source__in": COUNTED_SOURCES}) & (in_house | checked_by_us)


def counted_uses_q(prefix: str = "") -> Q:
    """The uses that count (module docstring), as a Q on TestEquipmentUse (prefix "") or on a model reaching it through `prefix`."""
    return counted_work_q(f"{prefix}work_order__")


def counted(wo) -> bool:
    """counted_work_q for one work order in memory (the drawer row, a use already read)."""
    if wo.status not in DONE or wo.completed_on is None or wo.source not in COUNTED_SOURCES:
        return False
    return not wo.vendor_service or (wo.type == WoType.REPAIR and wo.repair_check in COUNTED_REPAIR_CHECKS)


def offered(wo, user=None, *, repair_check: str | None = None) -> bool:
    """Whether `wo` may record test equipment: not imported, a repair (any outcome but the vendor's report) or not vendor service, and
    never for a scoped `user`. `repair_check` is the outcome a completion chooses in the same call; left out, a repair not yet completed
    is offered (its outcome is chosen when it is completed: the modal drops the ticks for the vendor's report) and a completed one is
    judged by its stored outcome."""
    from apps.workorders.scoping import is_scoped  # scoping imports the work order models; kept one way

    if user is not None and is_scoped(user):
        return False
    if wo.source == Source.IMPORTED:
        return False
    if not wo.vendor_service:
        return True
    if wo.type != WoType.REPAIR:
        return False
    if repair_check is None:
        if wo.status not in DONE:
            return True
        repair_check = wo.repair_check
    return repair_check != RepairCheck.VENDOR_REPORT


def usable_today(item, today: date) -> bool:
    """Whether `item` may be newly recorded on work completed today: in use, or its status changed today (work done this morning with
    an analyzer that failed its self-test at noon is recorded truthfully)."""
    return item.status == Status.IN_USE or item.status_since == today


def may_remove(item, completed_on: date | None, calibrations) -> bool:
    """The kept-use rule: a recorded use may be taken off unless a live calibration of `item` dated on or after `completed_on` is on
    file. `completed_on` is the work's day (work_day: for a reopened work order, the day it was last completed); None (never completed)
    takes off freely."""
    if completed_on is None:
        return True
    return not any(c.test_equipment_id == item.pk and c.calibrated_on >= completed_on for c in live(calibrations))


def work_day(wo) -> date | None:
    """The day a work order's uses are judged by for the kept-use rule: its completed_on, or for a reopened one (completed_on cleared)
    the day it was last completed, from its history (one query); None when it was never completed."""
    if wo.completed_on is not None:
        return wo.completed_on
    from apps.core.history import _rows
    from apps.workorders.models import WorkOrder

    return (_rows(WorkOrder.history.model).filter(id=wo.pk, completed_on__isnull=False).order_by("-history_date", "-history_id")
            .values_list("completed_on", flat=True).first())


def retired_throughout(item, day: date) -> date | None:
    """When `item` was retired before `day` and still retired on it (its history: the status in force on `day` is retired, set before
    it), the day it was retired; else None. One query. An item added after `day` was not retired then (a facility adding its test
    equipment after the work records it truthfully)."""
    from apps.core.history import _rows

    from .models import TestEquipment

    row = (_rows(TestEquipment.history.model).filter(id=item.pk, status_since__lte=day).order_by("-history_date", "-history_id")
           .values("status", "status_since").first())
    if row is None or row["status"] != Status.RETIRED or row["status_since"] >= day:
        return None
    return row["status_since"]


# --- batches ---------------------------------------------------------------------------------------------------------------------

def calibrations_by_item(item_ids) -> dict:
    """{item id: [its calibrations, entered in error included]} for `item_ids`, in one query (standing_on and window leave those
    entered in error out)."""
    from .models import Calibration

    ids = list({i for i in item_ids})
    out = defaultdict(list)
    if ids:
        for c in Calibration.objects.filter(test_equipment_id__in=ids).order_by("calibrated_on", "id"):
            out[c.test_equipment_id].append(c)
    return out


def reviews_by_calibration(calibrations) -> dict:
    """{calibration id: its ToleranceReview} for those of `calibrations` that need a review, in one query (none when none needs one)."""
    from .models import ToleranceReview

    ids = [c.pk for c in calibrations if c.needs_review]
    if not ids:
        return {}
    return {r.calibration_id: r for r in ToleranceReview.objects.filter(calibration_id__in=ids).select_related("decided_by")}


def standings(items, day: date, *, calibrations: dict | None = None, entered: dict | None = None) -> dict:
    """{item id: its Standing on `day`} for `items`: one query for their calibrations (none when `calibrations`, calibrations_by_item's,
    is given). `entered` (entered_days) adds the disclosure: on today it never applies (nothing is entered after today)."""
    items = list(items)
    cals = calibrations if calibrations is not None else calibrations_by_item(i.pk for i in items)
    return {i.pk: standing_on(i, day, cals.get(i.pk, ()), entered=entered) for i in items}


def matches(standing: Standing, key: str, today: date) -> bool:
    """Whether a standing judged `today` is in the list's filter `key` (FILTERS): past due, due within DUE_SOON_DAYS days, no
    calibration on file, failed. An unknown key matches everything (the caller keeps only FILTERS' keys)."""
    if key == "past_due":
        return standing.state == PAST_DUE
    if key == "due_soon":
        return standing.state == IN_DATE and standing.due_on <= today + timedelta(days=DUE_SOON_DAYS)
    if key == "no_record":
        return standing.state == NO_RECORD
    if key == "failed":
        return standing.state == FAILED
    return True


# --- words ---------------------------------------------------------------------------------------------------------------------

def item_name(item) -> str:
    """"SA-03 (Fluke ESA612)"."""
    return f"{item.tag} ({item.manufacturer} {item.model})"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def basis_words(review) -> str:
    """A review's decision in words: "reviewed Oct 10, 2026: the error was smaller than the margin to the limits used"."""
    return f"reviewed {fmt(review.decided_on)}: {_lower_first(Basis(review.basis).label)}"


def reviewed_ids(review) -> set:
    """The work order ids (as text) in a review's frozen list."""
    return {str(row[0]) for row in review.work_orders or ()}


@dataclass(frozen=True)
class FoundLater:
    """A use's work fell in the look-back window of `calibration` (one found out of tolerance, or failed): its review's `state`
    (NOT_REVIEWED, REVIEWED: in the review's frozen list, ADDED: added since the review) and the words."""
    calibration: object
    review: object | None
    state: str
    words: str


def found_later(work_order_id, day: date | None, calibrations, reviews: dict) -> tuple:
    """The FoundLater of work done on `day` (work order `work_order_id`) with the item whose records are `calibrations`, by the
    calibrations' days. `reviews`: reviews_by_calibration's."""
    if day is None:
        return ()
    out = []
    for c in _by_day(calibrations):
        if not c.needs_review or not in_window(day, window(c, calibrations)):
            continue
        found = f"found out of tolerance on {fmt(c.calibrated_on)}"
        review = reviews.get(c.pk)
        if review is None:
            out.append(FoundLater(c, None, NOT_REVIEWED, f"{found}; not reviewed"))
        elif str(work_order_id) in reviewed_ids(review):
            out.append(FoundLater(c, review, REVIEWED, f"{found}; {basis_words(review)}"))
        else:
            out.append(FoundLater(c, review, ADDED, f"{found}; added since the review of {fmt(review.decided_on)}"))
    return tuple(out)


def use_words(item, standing: Standing | None, later=()) -> str:
    """One use in words on the work's day: "SA-03 (Fluke ESA612): calibration due Mar 31, 2027 on the day of this work", then "(on
    file since <day>)" when the record was entered later, then each "found out of tolerance on <day>; <its review>". A use whose work
    has no day yet (reopened, not completed again): "SA-03 (Fluke ESA612): judged on the day the work is completed"."""
    if standing is None:
        return f"{item_name(item)}: judged on the day the work is completed"
    text = f"{item_name(item)}: {_lower_first(standing.plain)} on the day of this work"
    if standing.entered_after:
        first = "first certificate " if standing.state == NO_RECORD else ""
        text += f" ({first}on file since {fmt(standing.entered_after)})"
    for f in later:
        text += f"; {f.words}"
    return text


@dataclass(frozen=True)
class JudgedUse:
    """A use judged on its work's day: `day` (the work order's completed_on, None while it is not completed), its item's `standing`
    then (None without a day), `later` (FoundLater, by calibration day), and `words` (use_words)."""
    use: object
    item: object
    day: date | None
    standing: Standing | None
    later: tuple
    words: str

    @property
    def flagged(self) -> bool:
        """Past due, no calibration on file, or failed on its day, or found out of tolerance later (whatever its review)."""
        return self.standing is not None and (self.standing.flagged or bool(self.later))


def judge_uses(uses, *, calibrations: dict | None = None, entered: dict | None = None, reviews: dict | None = None) -> list:
    """Each of `uses` (TestEquipmentUse rows with work_order and test_equipment selected) judged on its work's day, in the same order:
    at most three queries for any number of uses (calibrations_by_item, entered_days, reviews_by_calibration), none for what is given."""
    uses = list(uses)
    cals = calibrations if calibrations is not None else calibrations_by_item(u.test_equipment_id for u in uses)
    every = [c for records in cals.values() for c in records]
    entered = entered if entered is not None else entered_days(every)
    reviews = reviews if reviews is not None else reviews_by_calibration(live(every))
    out = []
    for u in uses:
        item, wo = u.test_equipment, u.work_order
        day = wo.completed_on
        records = cals.get(item.pk, ())
        standing = standing_on(item, day, records, entered=entered) if day is not None else None
        later = found_later(wo.pk, day, records, reviews)
        out.append(JudgedUse(u, item, day, standing, later, use_words(item, standing, later)))
    return out
