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
day, core.days.local_day). Dates as apps.vendors.rules.fmt.

Found out of tolerance later: a calibration that needs a review (Calibration.needs_review: out of tolerance as found, or failed) puts in
question the work done with the item in its look-back window (window): after the latest live passed calibration before it (exclusive:
work on that day stands on that certificate) through its own day (inclusive: work that day before it shipped); with no earlier passed
calibration, every use on or before its day. A use in a live window reads "Found out of tolerance on <day>", then its review's state:
not reviewed; reviewed (the basis's words), when the use is in the review's frozen list; or added since the review (a binder GAP).

A use counts (counted_uses_q, a positive condition: never negate it) when its work order is completed or closed, not imported, and either
a repair whose outcome is not the vendor's report, or not vendor service; its day is the work order's completed_on (a reopened work order
has none, and its uses count again when it is completed again, on the new day). offered(wo, user) says whether a work order may record
test equipment at all; usable_today(item, today) whether an item may be newly recorded today; may_remove whether a recorded use may be
taken off (the kept-use rule: never once a live calibration dated on or after the work's day is on file: the use has been judged since).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.db.models import Q

NO_RECORD = "no_record"
IN_DATE = "in_date"
PAST_DUE = "past_due"
FAILED = "failed"


@dataclass(frozen=True)
class Standing:
    """An item's standing on a day: `state` (NO_RECORD, IN_DATE, PAST_DUE, FAILED), the governing calibration (None for NO_RECORD),
    its due day (IN_DATE, PAST_DUE), `since` (PAST_DUE: due_on + 1; FAILED: its day), `entered_after` (the facility day the governing
    record, or for NO_RECORD the first record, was entered, when after the day judged; else None), and `words`."""
    state: str
    calibration: object | None
    due_on: date | None
    since: date | None
    entered_after: date | None
    words: str

    @property
    def flagged(self) -> bool:
        """Anything but IN_DATE: the binder's FINDING and the modal's warning."""
        return self.state != IN_DATE


def standing_on(item, day: date, calibrations, *, entered: dict | None = None) -> Standing:
    """`item`'s standing on `day` from `calibrations` (its records, any order; those entered in error are left out here). `entered`:
    {calibration id: the facility day it was entered} (entered_days) for the "on file since" disclosure; None leaves it out."""
    raise NotImplementedError


def entered_days(calibrations) -> dict:
    """{calibration id: the facility day its history's first row was written} for `calibrations`, in one query (core.history._rows)."""
    raise NotImplementedError


def window(calibration, calibrations) -> tuple[date | None, date]:
    """(start, end) of the look-back window of `calibration` (one that needs a review) among the item's `calibrations`: start the
    latest live passed calibration's day before it (exclusive), or None; end its own day (inclusive)."""
    raise NotImplementedError


def counted_uses_q(prefix: str = "") -> Q:
    """The uses that count (module docstring), as a Q on TestEquipmentUse (prefix "") or on a model reaching it through `prefix`."""
    raise NotImplementedError


def offered(wo, user=None) -> bool:
    """Whether `wo` may record test equipment: not imported, a repair (any outcome but the vendor's report) or not vendor service, and
    never for a scoped `user`."""
    raise NotImplementedError


def usable_today(item, today: date) -> bool:
    """Whether `item` may be newly recorded on work completed today: in use, or its status changed today (work done this morning with
    an analyzer that failed its self-test at noon is recorded truthfully)."""
    raise NotImplementedError


def may_remove(item, completed_on: date | None, calibrations) -> bool:
    """The kept-use rule: a recorded use may be taken off unless a live calibration of `item` dated on or after `completed_on` is on
    file."""
    raise NotImplementedError
