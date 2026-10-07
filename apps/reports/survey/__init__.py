"""
The survey binder (slice 25, beyond the mock): the evidence a Joint Commission, CMS, or DNV surveyor asks a Clinical Engineering
department for, read from the records Cadence already keeps, for a period, with what is missing listed and linked.

One module per section (program, inventory, maintenance, aem, inspections, recalls, staff), each with `build(period, user) ->
Section`, registered in SECTIONS in the order the binder prints them. Each section needs Reports View plus its own areas' View
(apps.reports.permissions.SURVEY_NEEDS); `binder()` builds the sections a reader may see and names the ones left out.

Rules every section keeps:
- Read only. Tenant-scoped managers for live rows; historical tables (not tenant-scoped) through apps.core.history._rows, or filtered
  by the ids of this facility's rows. Business days of timestamps through apps.core.days.local_day, inside the facility.
- No text a requester typed (a work order's problem, notes, resolution, requester, callback, reported location; a device's notes),
  ever: the binder is printed, downloaded, and handed to people outside the department. Staff-written records are fine (a recall's
  disposition note, an AEM decision's minutes reference, the facility's policy texts).
- A fixed number of queries whatever the facility's size: figures and gaps by aggregates or one pass; a table's rows lazy
  (`Table.rows` is called by the CSV, which streams it, and sliced for the screen and print), never one query per row.
- Plain words. Topics, not standard element numbers (manuals get renumbered); never claim a rule the standards do not make.

Kinds of gap (Gap.kind): GAP, missing evidence one record fixes (its url opens that record); FINDING, something a surveyor will ask
about that no record can now fix (its url explains: the record it is about); CHECK, the facility's own housekeeping, not a survey
finding. The summary counts each kind and never says a binder is ready while a section was left out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

from django.core.exceptions import ValidationError

GAP, FINDING, CHECK = "gap", "finding", "check"
KINDS = (GAP, FINDING, CHECK)
KIND_LABELS = {GAP: "Gap", FINDING: "Finding", CHECK: "Check"}
KIND_HELP = {
    GAP: "Missing evidence; the linked record fixes it.",
    FINDING: "A surveyor will ask about it; no record can change it now, so be ready to explain.",
    CHECK: "The facility's own housekeeping, not a survey finding.",
}

MAX_DAYS = 3 * 366  # a binder covers at most three years (1,098 days)
PREVIEW_ROWS = 10  # a table's first rows on the screen
PRINT_ROWS = 500  # a table's rows in the printed binder; the rest are in its CSV
GAPS_SHOWN = 50  # a section's gaps listed on the screen; all of them in the gaps CSV and the print

# Table.links: the column holding a record's key, and what kind of record it is (the screen and print make the link).
WORK_ORDER, DEVICE = "work_order", "device"


@dataclass(frozen=True)
class Period:
    """The binder's period: start..end inclusive (end never after today), and today, the facility's (read once per binder)."""
    start: date
    end: date
    today: date

    @property
    def label(self) -> str:
        return f"{self.start:%b} {self.start.day}, {self.start.year} to {self.end:%b} {self.end.day}, {self.end.year}"

    @property
    def as_of_today(self) -> str:
        return f"as of today, {self.today:%b} {self.today.day}, {self.today.year}"

    def query(self) -> str:
        """The period as a query string, for links that must keep it."""
        return f"from={self.start.isoformat()}&to={self.end.isoformat()}"


def default_period(today: date) -> Period:
    """The twelve months ending today, from the first of the month eleven months back (Nov 1 to Oct 7, for Oct 7)."""
    month = today.month - 11
    year = today.year
    if month <= 0:
        month, year = month + 12, year - 1
    return Period(date(year, month, 1), today, today)


def parse_period(params, today: date) -> Period:
    """The period from `from` and `to` (ISO dates) in `params` (a QueryDict or dict); either missing is the default's. Refusals are a
    ValidationError keyed by the parameter, in words."""
    default = default_period(today)

    def day(key, fallback):
        raw = (params.get(key) or "").strip()
        if not raw:
            return fallback
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValidationError({key: "Enter a date as YYYY-MM-DD."}) from None

    start, end = day("from", default.start), day("to", default.end)
    if end > today:
        raise ValidationError({"to": "The binder cannot cover days after today."})
    if start > end:
        raise ValidationError({"from": "The period starts after it ends."})
    if (end - start).days >= MAX_DAYS:
        raise ValidationError({"from": "A binder covers at most three years."})
    return Period(start, end, today)


@dataclass
class Gap:
    kind: str  # GAP, FINDING, or CHECK
    text: str  # plain words; the record's own number or tag may appear in it, never requester text
    url: str = ""  # the record's path (reverse("web:...")), no ?facility=: the print and the API add it (people.with_facility)
    record: str = ""  # what the url opens, as shown: "WO-26-0042", "CE-10241", "Hamilton G5"


@dataclass
class Figure:
    label: str
    value: object  # a number, a date, or short text; the template formats it
    hint: str = ""


@dataclass
class Table:
    key: str  # unique in its section; the CSV is /reports/survey/<section>/<key>.csv
    title: str
    columns: list[str]
    rows: Callable[[], Iterable[list]]  # lazy: every row, plain values (str, int, Decimal, float, date, None)
    count: int | None = None  # how many rows, when the section knows it cheaply (else the page says "see the CSV")
    links: dict[int, str] = field(default_factory=dict)  # {column index: WORK_ORDER or DEVICE}
    printed: bool = True  # False: the print shows the count and points to the CSV (the inventory's every device)
    empty: str = "None in this period."


@dataclass
class Section:
    key: str
    title: str
    topic: str  # what it is evidence for, in plain words
    covers: str  # "Nov 1, 2025 to Oct 7, 2026" (Period.label) or "as of today, Oct 7, 2026" (Period.as_of_today), or both in words
    figures: list[Figure] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what was measured and how, what is left out and why

    def counts(self) -> dict:
        return {kind: sum(1 for g in self.gaps if g.kind == kind) for kind in KINDS}

    def table(self, key: str) -> Table | None:
        return next((t for t in self.tables if t.key == key), None)


@dataclass
class LeftOut:
    key: str
    title: str
    reason: str  # apps.reports.permissions.survey_refusal's words


@dataclass
class Binder:
    period: Period
    sections: list[Section]
    left_out: list[LeftOut]

    def counts(self) -> dict:
        totals = {kind: 0 for kind in KINDS}
        for s in self.sections:
            for kind, n in s.counts().items():
                totals[kind] += n
        return totals

    @property
    def complete(self) -> bool:
        """Every section is in it (none left out for access)."""
        return not self.left_out

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)


# Not covered: what a surveyor may ask for that Cadence keeps no record of, printed on the cover so the binder is never read as the
# whole program.
NOT_COVERED = [
    "The facility's written medical equipment management plan and its annual evaluation",
    "Checks done after a repair before the device went back into use (Cadence records the repair, not those checks)",
    "Device incident investigations and reports to the FDA or the manufacturer",
    "Training of the clinical staff who use the equipment",
    "Rental, loaner, and vendor-owned equipment not entered in Cadence",
    "The competence of contracted vendor staff",
]


def _sections():
    from . import aem, inspections, inventory, maintenance, program, recalls, staff

    return [
        ("program", "Program and policies", program.build),
        ("inventory", "Medical equipment inventory", inventory.build),
        ("maintenance", "Scheduled maintenance (PM) completion", maintenance.build),
        ("aem", "Alternate equipment maintenance (AEM)", aem.build),
        ("inspections", "Incoming inspection before first use", inspections.build),
        ("recalls", "Recalls and safety alerts", recalls.build),
        ("staff", "Technician qualifications", staff.build),
    ]


def section_keys() -> list[str]:
    return [key for key, _title, _build in _sections()]


def section_title(key: str) -> str:
    return next((title for k, title, _build in _sections() if k == key), "")


def build_section(key: str, period: Period, user) -> Section:
    """One section, for its CSV or the API (the caller checks the reader may see it: permissions.survey_refusal)."""
    for k, _title, build in _sections():
        if k == key:
            return build(period, user)
    raise KeyError(key)


def binder(user, period: Period) -> Binder:
    """Every section `user` may see, in order, and the ones left out with why."""
    from apps.reports.permissions import survey_refusal

    sections, left_out = [], []
    for key, title, build in _sections():
        reason = survey_refusal(user, key)
        if reason:
            left_out.append(LeftOut(key, title, reason))
        else:
            sections.append(build(period, user))
    return Binder(period, sections, left_out)


