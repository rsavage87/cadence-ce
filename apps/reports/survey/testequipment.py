"""
The survey binder's test equipment section (slice 35): the CE department's own test equipment and its calibration, as DNV surveyors ask
for it under ISO 9001's control of measuring equipment (calibrated at intervals, its status identified, the test equipment used on the
records, the work done with one later found out of tolerance reviewed). Cadence never says which accreditor asks, and never that a
standard requires it.

Every rule is apps.testequipment.rules (standing on a day, the look-back window, which work counts, the words); nothing is re-derived
here. The standing's words are the rules' (never "calibrated", "in date", or "uncalibrated" as a status).

Tables (rows built in the one pass, read lazily):
- items: every item not retired before the period began, with its status and its standing today and at the period's end (the latter
  with "on file since" when its certificate was entered after that day; "Not in Cadence yet" for an item added after it).
- calibrations: the live certificates dated in the period (provider, number, as found, adjusted, result, the ISO/IEC 17025 mark, the
  day it was entered), each with its tolerance review when it needs one (not reviewed, or the decision, and work added since it).
- work: the counted work completed in the period with test equipment past due, with no calibration on file, or failed on the work's
  day, or found out of tolerance later, or judged on a certificate entered after the work (its "on file since").
- unnamed: the counted work completed in the period, since test equipment was first entered in Cadence, that recorded a reading (a
  measured checklist step with a reading) or was a safety check, and names no test equipment.

Gaps (GAP: one record fixes it, linked to the item's page; FINDING: be ready to explain; CHECK: housekeeping):
- GAP per item in use today that is past due, has no calibration on file, or failed (beyond build35.md: a failed item can be in use
  again only when reinstated or found while its failed certificate governs).
- GAP per live calibration that needs a review (out of tolerance as found, or failed) and has none, when its look-back window touches
  the period (it may be dated after the period's end: the period's work is in question).
- GAP per decided review whose window (touching the period) now holds counted work outside its frozen list (added since the review).
- FINDING per item and stretch: the period's work done with it on days it was past due, with no calibration on file, or failed, one per
  governing certificate (the stretch), with the first and last day of that work; a certificate in force on those days entered late
  clears it, disclosed "on file since".
- CHECK: the unnamed work above, one with the count (earlier work, before test equipment was first entered, is a figure apart).
- CHECK per calibration dated in the period that was adjusted with its as found not reported: ask the provider for the as-found data.

Built with user=None (the program evaluation's binder_gaps) in a fixed number of queries whatever the facility's size: the items, their
history's first days (core.history._rows), their calibrations, the calibrations' entered days, their reviews, the period's counted uses,
the uses in the touching windows (only when some calibration there needs a review), and the period's counted work naming none (eight at
most). Never requester text: work orders are read for their number, type, day, device tag, and checklist steps (staff-recorded; only
whether a measured step has a reading is used).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from django.db.models import Exists, Min, OuterRef, Q
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows
from apps.testequipment import rules
from apps.testequipment.models import AsFound, Kind, Mark, Program, Reason, Result, Status, TestEquipment, TestEquipmentUse
from apps.vendors.rules import fmt
from apps.workorders.models import WorkOrder, WoType

from . import CHECK, DEVICE, FINDING, GAP, TEST_EQUIPMENT, WORK_ORDER, Figure, Gap, Period, Section, Table

KEY, TITLE = "testequipment", "Test equipment calibration"
ITEM_COLUMNS = ["Tag", "Kind", "Make and model", "Serial number", "Certificates", "Interval (months)", "Status", "Since", "Held by",
                "Standing today", "Standing at the period's end"]
CALIBRATION_COLUMNS = ["Tag", "Calibrated", "Due", "Provider", "Certificate", "As found", "Adjusted", "Result",
                       "Certificate carries an ISO/IEC 17025 accreditation mark", "Entered in Cadence", "Tolerance review"]
WORK_COLUMNS = ["Work order", "Type", "Completed on", "Device", "Test equipment", "Standing on the day", "On file since",
                "Found out of tolerance later"]
UNNAMED_COLUMNS = ["Work order", "Type", "Completed on", "Device"]
NOT_YET = "Not in Cadence yet"  # an item added after the period's end has no standing there
NOT_NEEDED = "Not needed"  # in tolerance as found and passed: nothing to review
NOT_REVIEWED = "Not reviewed"
_TYPES = dict(WoType.choices)
# The columns a use is read with (never the work order's problem, notes, resolution, or requester)
_USE_ONLY = ("id", "work_order", "test_equipment", "work_order__number", "work_order__type", "work_order__completed_on", "work_order__asset",
             "work_order__asset__tag", "test_equipment__tag", "test_equipment__manufacturer", "test_equipment__model", "test_equipment__program")


def _n(n: int, what: str) -> str:
    return f"{n:,} {what}{'' if n == 1 else 's'}"


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


def _span(first: date, last: date) -> str:
    return fmt(first) if first == last else f"{fmt(first)} to {fmt(last)}"


def _url(tag: str) -> str:
    return reverse("web:test_equipment_item", args=[tag])


def _status_words(item) -> str:
    words = Status(item.status).label
    return f"{words} ({_lower(Reason(item.status_reason).label)})" if item.status_reason else words


def _touches(bounds: tuple, period: Period) -> bool:
    """Whether a look-back window (rules.window: after its start, through its end) holds any day of the period."""
    start, end = bounds
    return end >= period.start and (start is None or start < period.end)


def _window_words(bounds: tuple) -> str:
    start, end = bounds
    if start is None:
        return f"on or before {fmt(end)}"
    return f"after its previous passed certificate ({fmt(start)}) through {fmt(end)}"


def _has_reading(steps) -> bool:
    """A measured checklist step with a reading recorded (completion's checklist_results: measure is None or False when nothing is
    recorded on the step)."""
    return any(isinstance(s, dict) and s.get("measure") not in (None, False, "") and str(s.get("reading") or "").strip()
               for s in steps or ())


def _review_words(cal, review, added: int) -> str:
    if not cal.needs_review:
        return NOT_NEEDED
    if review is None:
        return NOT_REVIEWED
    words = rules.basis_words(review)
    words = words[:1].upper() + words[1:]
    return words + (f"; {_n(added, 'work order')} added since the review" if added else "")


def _item_gap(item, standing) -> Gap:
    """An item in use today that is past due, has no calibration on file, or failed."""
    name, words = rules.item_name(item), _lower(standing.words)
    if standing.state == rules.FAILED:
        text = f"{name} is in use: {words}. Take it out of use, or record the certificate that passed it since."
    elif standing.state == rules.PAST_DUE:
        text = f"{name} is in use: {words}. Record its new certificate, or move it out for calibration."
    else:
        text = f"{name} is in use with {words}. Record its latest certificate, or move it out for calibration."
    return Gap(GAP, text, _url(item.tag), item.tag)


def _not_reviewed_gap(item, cal, bounds, n: int) -> Gap:
    verb = "failed it" if cal.result == Result.FAILED else "found it out of tolerance"
    if n:
        work = f"the {_n(n, 'work order')} done with it {_window_words(bounds)} {'is' if n == 1 else 'are'} not reviewed"
    else:
        work = f"it is not reviewed (no work is recorded with it {_window_words(bounds)})"
    return Gap(GAP, f"{rules.item_name(item)}: certificate {cal.certificate} ({fmt(cal.calibrated_on)}) {verb}, and {work}. Record the "
                    "tolerance review on its page.", _url(item.tag), item.tag)


def _added_gap(item, cal, review, added: int) -> Gap:
    were, them = ("was", "it") if added == 1 else ("were", "them")
    return Gap(GAP, f"{rules.item_name(item)}: the tolerance review of certificate {cal.certificate} ({fmt(review.decided_on)}) covered "
                    f"{_n(review.work_count, 'work order')}; {_n(added, 'more work order')} done with it in the same window {were} recorded "
                    f"since, and no decision covers {them}. Decide about {them} in the facility's quality record (its page lists {them}).",
               _url(item.tag), item.tag)


def _findings(judged: list) -> list[Gap]:
    """One FINDING per item and stretch: the period's work done with it on days its standing was flagged (past due, no calibration on
    file, failed), grouped by the certificate that governed (none for no calibration on file)."""
    groups: dict = defaultdict(list)
    for j in judged:
        if j.standing is not None and j.standing.flagged:
            cal = j.standing.calibration
            groups[(j.item.pk, j.standing.state, cal.pk if cal is not None else None)].append(j)
    out = []
    for found in groups.values():
        first = found[0]
        item, standing = first.item, first.standing
        days = sorted(j.day for j in found)
        n = _n(len({j.use.work_order_id for j in found}), "work order")
        plain, span = _lower(standing.plain), _span(days[0], days[-1])
        if standing.state == rules.PAST_DUE:
            head = f"{rules.item_name(item)} was used on {n} while past due, {span} ({plain})."
        elif standing.state == rules.FAILED:
            head = f"{rules.item_name(item)} was used on {n} after it failed, {span} ({plain})."
        else:
            head = f"{rules.item_name(item)} was used on {n} with {plain}, {span}."
        text = (f"{head} If a certificate in force on those days was not recorded, record it: the work is then judged on it, showing the "
                "day it was entered.")
        out.append(((item.tag, days[0]), Gap(FINDING, text, _url(item.tag), item.tag)))
    return [gap for _key, gap in sorted(out, key=lambda pair: pair[0])]


def _window_work(scoped: list) -> dict:
    """{calibration id: {work order id (text)}}: the counted uses of each calibration's item in its window now, for `scoped` [(calibration,
    bounds)]. One query (none for an empty list)."""
    if not scoped:
        return {}
    q = Q()
    for cal, bounds in scoped:
        q |= Q(test_equipment_id=cal.test_equipment_id) & rules.window_q(bounds, "work_order__")
    rows = list(TestEquipmentUse.objects.filter(rules.counted_uses_q()).filter(q).order_by()
                .values_list("test_equipment_id", "work_order_id", "work_order__completed_on"))
    return {cal.pk: {str(wo) for item, wo, day in rows if item == cal.test_equipment_id and rules.in_window(day, bounds)}
            for cal, bounds in scoped}


def build(period: Period, user) -> Section:
    today = period.today
    items = list(TestEquipment.objects.select_related("held_by").order_by("tag"))
    added = {pk: local_day(first) for pk, first in _rows(TestEquipment.history.model).order_by().values("id")
             .annotate(first=Min("history_date")).values_list("id", "first")}
    first_added = min(added.values(), default=None)  # the day the facility's first test equipment was entered in Cadence
    cals = rules.calibrations_by_item(i.pk for i in items)
    every = [c for records in cals.values() for c in records]
    entered = rules.entered_days(every)
    live = rules.live(every)
    reviews = rules.reviews_by_calibration(live)
    by_id = {i.pk: i for i in items}

    # The items: listed unless retired before the period began; standing today and at the period's end
    listed = [i for i in items if i.status != Status.RETIRED or i.status_since >= period.start]
    now = rules.standings(listed, today, calibrations=cals)
    at_end = rules.standings(listed, period.end, calibrations=cals, entered=entered)

    def end_words(item) -> str:
        day = added.get(item.pk)
        return NOT_YET if day is not None and day > period.end else at_end[item.pk].words

    item_rows = [[i.tag, Kind(i.kind).label, f"{i.manufacturer} {i.model}", i.serial, Program(i.program).label, i.interval_months,
                  _status_words(i), i.status_since, i.held_by.name if i.held_by else "", now[i.pk].words, end_words(i)] for i in listed]
    in_use = [i for i in listed if i.status == Status.IN_USE]
    gaps = [_item_gap(i, now[i.pk]) for i in in_use if now[i.pk].flagged]

    # The tolerance reviews of the windows that touch the period
    scoped = []
    for c in sorted(live, key=lambda c: (by_id[c.test_equipment_id].tag, c.calibrated_on)):
        if c.needs_review:
            bounds = rules.window(c, cals[c.test_equipment_id])
            if _touches(bounds, period):
                scoped.append((c, bounds))
    in_window = _window_work(scoped)
    added_since = {}
    for c, bounds in scoped:
        item, review, work = by_id[c.test_equipment_id], reviews.get(c.pk), in_window[c.pk]
        if review is None:
            gaps.append(_not_reviewed_gap(item, c, bounds, len(work)))
            continue
        added_since[c.pk] = len(work - rules.reviewed_ids(review))
        if added_since[c.pk]:
            gaps.append(_added_gap(item, c, review, added_since[c.pk]))

    # The period's calibrations
    in_period = sorted((c for c in live if period.start <= c.calibrated_on <= period.end),
                       key=lambda c: (c.calibrated_on, by_id[c.test_equipment_id].tag))
    in_error = sum(1 for c in every if c.entered_in_error and period.start <= c.calibrated_on <= period.end)
    calibration_rows = [[by_id[c.test_equipment_id].tag, c.calibrated_on, c.due_on, c.provider, c.certificate, AsFound(c.as_found).label,
                         "Yes" if c.adjusted else "No", Result(c.result).label, "Yes" if c.accreditation == Mark.SHOWN else "No",
                         entered.get(c.pk), _review_words(c, reviews.get(c.pk), added_since.get(c.pk, 0))] for c in in_period]
    needing = [c for c in in_period if c.needs_review]

    # The period's work done with test equipment, each use judged on its day
    uses = (TestEquipmentUse.objects.filter(rules.counted_uses_q(), work_order__completed_on__gte=period.start,
                                            work_order__completed_on__lte=period.end)
            .select_related("work_order__asset", "test_equipment").only(*_USE_ONLY)
            .order_by("work_order__completed_on", "work_order__number", "test_equipment__tag"))
    judged = rules.judge_uses(uses, calibrations=cals, entered=entered, reviews=reviews)
    shown = [j for j in judged if j.flagged or j.standing.entered_after]
    work_rows = [[j.use.work_order.number, _TYPES.get(j.use.work_order.type, j.use.work_order.type), j.day, j.use.work_order.asset.tag,
                  j.item.tag, j.standing.plain, j.standing.entered_after, "; ".join(f.words for f in j.later)] for j in shown]
    gaps += _findings(judged)

    # Work with readings or a safety check, naming no test equipment
    candidates = (WorkOrder.objects.filter(rules.counted_work_q(), completed_on__gte=period.start, completed_on__lte=period.end)
                  .filter(~Exists(TestEquipmentUse.objects.filter(work_order=OuterRef("pk"))))
                  .order_by("completed_on", "number").values("number", "type", "completed_on", "asset__tag", "checklist_results"))
    unnamed, before = [], 0
    for w in candidates:
        if w["type"] != WoType.SAFETY and not _has_reading(w["checklist_results"]):
            continue
        if first_added is None or w["completed_on"] < first_added:
            before += 1
            continue
        unnamed.append([w["number"], _TYPES.get(w["type"], w["type"]), w["completed_on"], w["asset__tag"]])
    if unnamed:
        n, span = len(unnamed), _span(unnamed[0][2], unnamed[-1][2])
        gaps.append(Gap(CHECK, f"{_n(n, 'work order')} completed {span}, since test equipment was first entered in Cadence ({fmt(first_added)}), "
                               f"recorded readings or {'was a safety check' if n == 1 else 'were safety checks'} but "
                               f"{'names' if n == 1 else 'name'} no test equipment: record the test equipment used on each (the table lists "
                               f"{'it' if n == 1 else 'them'})."))
    for c in in_period:
        if c.adjusted and c.as_found == AsFound.NOT_REPORTED:
            item = by_id[c.test_equipment_id]
            gaps.append(Gap(CHECK, f"{rules.item_name(item)}: certificate {c.certificate} ({fmt(c.calibrated_on)}) says it was adjusted but "
                                   "does not report what it found before: ask the provider for the as-found data (it shows whether the work "
                                   "done with it before needs a review).", _url(item.tag), item.tag))

    work_orders = {j.use.work_order_id for j in judged}
    flagged_on_day = {j.use.work_order_id for j in judged if j.standing.flagged}
    found_later = {j.use.work_order_id for j in judged if j.later}
    figures = [
        Figure("Test equipment", len(listed), "not retired before the period began"),
        Figure("In use today", len(in_use)),
        Figure("In use and past due today", sum(1 for i in in_use if now[i.pk].state == rules.PAST_DUE)),
        Figure("In use with no calibration on file today", sum(1 for i in in_use if now[i.pk].state == rules.NO_RECORD)),
        Figure("Calibrations in the period", len(in_period), f"{in_error:,} more entered in error, left out" if in_error else ""),
        Figure("Found out of tolerance or failed", len(needing), "calibrations in the period"),
        Figure("Of those, reviewed", sum(1 for c in needing if c.pk in reviews)),
        Figure("Work done with test equipment", len(work_orders), "work orders completed in the period"),
        Figure("Done with test equipment past due, with no calibration on file, or failed", len(flagged_on_day), "on the day of the work"),
        Figure("Done with test equipment found out of tolerance later", len(found_later)),
        Figure("Work with readings naming no test equipment", len(unnamed),
               f"since test equipment was first entered in Cadence ({fmt(first_added)})" if first_added else "no test equipment entered yet"),
        Figure("Work with readings before test equipment was entered", before,
               "completed in the period before then: counted apart, never a check" if first_added else "counted, never a check"),
    ]
    tables = [
        Table("items", "Test equipment", ITEM_COLUMNS, lambda: iter(item_rows), count=len(item_rows), links={0: TEST_EQUIPMENT},
              empty="No test equipment is entered in Cadence."),
        Table("calibrations", "Calibrations in the period", CALIBRATION_COLUMNS, lambda: iter(calibration_rows), count=len(calibration_rows),
              links={0: TEST_EQUIPMENT}, empty="No certificate dated in this period is recorded."),
        Table("work", "Work done with test equipment past due, with no calibration on file, failed, found out of tolerance later, or with "
                      "a certificate entered after the work", WORK_COLUMNS, lambda: iter(work_rows), count=len(work_rows),
              links={0: WORK_ORDER, 3: DEVICE, 4: TEST_EQUIPMENT},
              empty="No work in this period was done with test equipment past due, with no calibration on file, or failed, and none "
                    "was found out of tolerance later."),
        Table("unnamed", "Work with readings naming no test equipment", UNNAMED_COLUMNS, lambda: iter(unnamed), count=len(unnamed),
              links={0: WORK_ORDER, 3: DEVICE}, empty="Every work order with readings in this period names its test equipment."),
    ]
    notes = [
        "An item's standing on a day is read from its certificates by their own dates, whenever they were entered: the latest dated on or "
        "before the day governs (passed, with its due day; or failed), or none is on file. One entered in Cadence after the day says so: "
        "on file since the day it was entered.",
        "Found out of tolerance later: the work done with an item after its previous passed certificate, through the day a certificate "
        "found it out of tolerance or failed it, needs a decision, the tolerance review (the error was within the margin to the limits "
        "used, the function was not used, the devices were rechecked, or they are rechecked at their next scheduled work).",
        "Work counts when it is completed in the period, not imported from the previous system, and the facility's own: vendor service "
        "names its test equipment on the vendor's report, as does a repair the vendor checked.",
        "Cadence records each certificate's number, provider, as found, result, and whether it carries an ISO/IEC 17025 accreditation "
        "mark; it keeps no copy of the certificate and cannot check a laboratory's scope.",
        "Work with readings (a measured checklist step with a reading) or a safety check that names no test equipment is checked from "
        "the day test equipment was first entered in Cadence; earlier work is counted apart.",
        f"Items are listed unless retired before the period began; standing today is as of {fmt(today)}, and at the period's end as of "
        f"{fmt(period.end)}.",
    ]
    if in_error:
        notes.append(f"Certificates entered in error are left out ({in_error} dated in the period).")
    return Section(KEY, TITLE, topic="The CE department's own test equipment: its calibration, and the work done with it.",
                   covers=f"{period.label} for the calibrations and the work done; test equipment {period.as_of_today}",
                   figures=figures, gaps=gaps, tables=tables, notes=notes)
