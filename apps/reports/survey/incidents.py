"""
The survey binder's "Device incidents" section (slice 28): the incidents that occurred in the period and those open at its end, the
reportability decisions, the reports sent against the 10-work-day clock (21 CFR 803.30), and the devices held as evidence. Needs
Incidents View (permissions.SURVEY_NEEDS): its rows are facts about a health event (CLAUDE.md non-negotiable 6). Nothing typed: an
incident has no free text, and its investigation work order is read for its number, status, and completion day only.

Listed: every incident that occurred in the period, and every one open at its end (occurred before it and still open, or closed after
it). An incident recorded in error is never counted nor listed: each one that occurred in the period is a CHECK.

The rules are apps.incidents.services' (due_date, needs_decision, required_recipients, reports_missing, reports_done_by), judged as of
today. "With the report clock": the clock ran at some time (report_due_on set now or in any version of the incident, from its history:
a decision that it was not reportable clears the due date).

Gaps, at most one per incident, the first that applies:
- FINDING its outcome is still not known and its report due date has passed;
- FINDING not decided, no report recorded, and past due ("if the device could not be ruled out, it should have been reported");
- GAP decided reportable, a required report not recorded, and past due ("record it if it was sent");
- FINDING decided reportable and the last required report went out after the due date;
- GAP decided with someone harmed and no event report number (the deliberations are in that report: 803.18).
Checks, at most one per incident: open more than OPEN_DAYS days since it occurred with no device evaluation finding; the investigation
completed more than RELEASE_DAYS days ago and a device still held; recorded in error.

Queries: four whatever the number of incidents: the incidents, their holds, their history's due dates, and the devices held now.
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.urls import reverse

from apps.core.history import _rows
from apps.core.workdays import work_days_between
from apps.incidents import services as inc
from apps.incidents.models import (
    HARM_OUTCOMES,
    SHORT_OUTCOMES,
    Affected,
    Basis,
    DecidedBy,
    Finding,
    Incident,
    IncidentHold,
    Outcome,
    Status,
)
from apps.workorders.models import WoStatus

from . import CHECK, DEVICE, FINDING, GAP, INCIDENT, Figure, Gap, Period, Section, Table

KEY, TITLE = "incidents", "Device incidents"
OPEN_DAYS = 30  # open this many days since it occurred with no finding: a check (Cadence's measure, not a rule of 21 CFR 803)
RELEASE_DAYS = 14  # a device still held this many days after the investigation was completed: a check (Cadence's measure)
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
# The decision's basis in a table cell (the labels are the regulation's words, too long for a column).
BASIS_SHORT = {
    Basis.MAY_HAVE: "May have caused or contributed",
    Basis.NOT_SERIOUS: "Not a death or serious injury",
    Basis.NO_SUGGESTION: "Does not suggest the device",
    Basis.NOT_PATIENT: "Not a patient of the facility",
}
COLUMNS = ["Incident", "Device", "Model", "Occurred", "Clinical staff first knew", "Outcome", "Affected", "Decision", "Basis", "Decided on",
           "Decided by", "Report due", "Sent to the FDA", "Sent to the manufacturer", "Report number", "Finding", "Event report number",
           "Holds released", "Status"]
# The incident's columns read: never anything typed (it has none) and only the investigation work order's number, status, and day done.
_FIELDS = ("number", "occurred_on", "aware_on", "outcome", "affected", "reportable", "basis", "decided_on", "decided_by", "report_due_on",
           "fda_reported_on", "manufacturer_reported_on", "report_number", "finding", "event_reference", "status", "closed_on", "asset",
           "asset__tag", "asset__device_model", "asset__device_model__manufacturer", "asset__device_model__model", "work_order",
           "work_order__number", "work_order__status", "work_order__completed_on")


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _incidents(period: Period) -> list[Incident]:
    """The incidents that occurred in the period, those open at its end, and those recorded in error that occurred in it, newest
    first. One query."""
    in_period = Q(occurred_on__gte=period.start, occurred_on__lte=period.end)
    open_at_end = Q(occurred_on__lt=period.start) & (Q(status=Status.OPEN) | Q(status=Status.CLOSED, closed_on__gt=period.end))
    return list(Incident.objects.filter(in_period | open_at_end).select_related("asset__device_model", "work_order").only(*_FIELDS)
                .order_by("-occurred_on", "-number"))


def _holds(ids: list) -> dict:
    """{incident id: [(tag, released_on)]} by the day each device was held. One query."""
    out: dict = {}
    for incident_id, tag, released_on in (IncidentHold.objects.filter(incident_id__in=ids).order_by("held_on", "asset__tag")
                                          .values_list("incident_id", "asset__tag", "released_on")):
        out.setdefault(incident_id, []).append((tag, released_on))
    return out


def _clocked(ids: list) -> set:
    """The ids of the incidents whose clock ran at some time: a version with a report due date, from their history (this facility's
    rows only, core.history._rows). One query."""
    return set(_rows(Incident.history.model).filter(id__in=ids, report_due_on__isnull=False).order_by().values_list("id", flat=True).distinct())


def _held_now() -> int:
    """The devices held now by open incidents (each once, however many incidents hold it). One query."""
    return (IncidentHold.objects.filter(released_on__isnull=True, incident__status=Status.OPEN).order_by()
            .aggregate(n=Count("asset_id", distinct=True))["n"])


def _past_due(i: Incident, today) -> bool:
    return i.report_due_on is not None and today > i.report_due_on


def _timing(i: Incident, today) -> str:
    """How a reportable incident's reports stand against the clock: "on_time" (every required report sent by the due date), "late"
    (the last sent after it, or one still missing past due), or "" (not reportable, or still in time)."""
    due, done = i.report_due_on, inc.reports_done_by(i)
    if i.reportable is not True or due is None:
        return ""
    if done is not None:
        return "on_time" if done <= due else "late"
    return "late" if today > due else ""


def _recipients(i: Incident) -> str:
    return _and([inc.RECIPIENT_WORDS[r] for r in inc.reports_missing(i)])


def _gap(i: Incident, today) -> Gap | None:
    """The first of the section's gaps and findings that applies to `i`, or None."""
    url, tag, due = reverse("web:incident", args=[i.number]), i.asset.tag, i.report_due_on
    sent = i.fda_reported_on or i.manufacturer_reported_on
    if i.outcome == Outcome.UNKNOWN and _past_due(i, today):
        return Gap(FINDING, f"{i.number} on {tag}: the outcome is still not known, and its report was due {_day(due)} "
                            f"({_n(work_days_between(due, today), 'work day')} ago)", url, i.number)
    if i.reportable is None and not sent and _past_due(i, today):
        return Gap(FINDING, f"{i.number} on {tag} was not decided by its report due date ({_day(due)}) and no report is recorded: if the "
                            "device could not be ruled out, it should have been reported", url, i.number)
    if i.reportable and inc.reports_missing(i) and _past_due(i, today):
        return Gap(GAP, f"{i.number} on {tag} was decided reportable and its report to {_recipients(i)} is not recorded (due "
                        f"{_day(due)}): record it if it was sent", url, i.number)
    done = inc.reports_done_by(i)
    if i.reportable and done is not None and due is not None and done > due:
        return Gap(FINDING, f"{i.number} on {tag}: the report went out {_day(done)}, {_n(work_days_between(due, done), 'work day')} after "
                            f"its due date ({_day(due)})", url, i.number)
    if i.reportable is not None and i.outcome in HARM_OUTCOMES and i.affected != Affected.NONE and not i.event_reference:
        return Gap(GAP, f"{i.number} on {tag} was decided with no event report number: record the number of the facility's event report, "
                        "where the deliberations are", url, i.number)
    return None


def _check(i: Incident, holds: list, today) -> Gap | None:
    """The first of the section's checks that applies to an open incident, or None."""
    if i.status != Status.OPEN:
        return None
    url, open_days = reverse("web:incident", args=[i.number]), (today - i.occurred_on).days
    if open_days > OPEN_DAYS and not i.finding:
        return Gap(CHECK, f"{i.number} on {i.asset.tag} has been open {open_days} days since it occurred ({_day(i.occurred_on)}) with no "
                          "device evaluation finding recorded", url, i.number)
    wo = i.work_order
    held = [tag for tag, released_on in holds if released_on is None]
    if wo is not None and wo.status in DONE and wo.completed_on is not None and held and (today - wo.completed_on).days > RELEASE_DAYS:
        verb = "is" if len(held) == 1 else "are"
        return Gap(CHECK, f"{i.number}: its investigation {wo.number} was completed {_day(wo.completed_on)}, and {_and(held)} {verb} still "
                          f"held {(today - wo.completed_on).days} days later", url, i.number)
    return None


def _decision(i: Incident) -> str:
    if i.reportable is True:
        return "Reportable"
    if i.reportable is False:
        return "Not reportable"
    return "Pending" if inc.needs_decision(i) and i.status == Status.OPEN else "None needed"


def _released(holds: list) -> str:
    if not holds:
        return "No hold"
    return f"{sum(1 for _tag, released_on in holds if released_on is not None)} of {len(holds)}"


def _row(i: Incident, holds: list) -> list:
    dm = i.asset.device_model
    return [i.number, i.asset.tag, f"{dm.manufacturer} {dm.model}", i.occurred_on, i.aware_on, SHORT_OUTCOMES.get(i.outcome, i.outcome),
            Affected(i.affected).label, _decision(i), BASIS_SHORT.get(i.basis, ""), i.decided_on,
            DecidedBy(i.decided_by).label if i.decided_by else "", i.report_due_on, i.fda_reported_on, i.manufacturer_reported_on,
            i.report_number, Finding(i.finding).label if i.finding else "", i.event_reference, _released(holds), Status(i.status).label]


def build(period: Period, user) -> Section:
    today = period.today
    found = _incidents(period)
    ids = [i.pk for i in found]
    holds = _holds(ids) if ids else {}
    clocked = _clocked(ids) if ids else set()
    held_now = _held_now()

    listed = [i for i in found if i.status != Status.IN_ERROR]
    in_error = [i for i in found if i.status == Status.IN_ERROR]
    occurred = [i for i in listed if period.start <= i.occurred_on <= period.end]
    timing = [_timing(i, today) for i in listed]
    pending = [i for i in listed if i.status == Status.OPEN and inc.needs_decision(i)]
    before = len(listed) - len(occurred)

    figures = [
        Figure("Incidents recorded", len(occurred), "occurred in the period" + (f"; {before} more from before it were open at its end" if before else "")),
        Figure("With the report clock", sum(1 for i in listed if i.pk in clocked or i.report_due_on is not None),
               "a death, a serious injury, or an outcome not known yet, of a patient or a staff member on duty"),
        Figure("Decided reportable", sum(1 for i in listed if i.reportable is True),
               f"{sum(1 for i in listed if i.reportable is False)} decided not reportable"),
        Figure("Reports on time", timing.count("on_time"), "every required report sent by its due date"),
        Figure("Reports late", timing.count("late"), "sent after the due date, or not recorded and past due"),
        Figure("Decisions pending", len(pending), f"{sum(1 for i in pending if _past_due(i, today)) or 'none'} past their report due date"),
        Figure("Devices held now", held_now, f"as evidence by an open incident ({period.as_of_today})"),
    ]

    gaps = []
    for i in listed:
        gap = _gap(i, today)
        if gap is not None:
            gaps.append(gap)
    for i in listed:
        check = _check(i, holds.get(i.pk, []), today)
        if check is not None:
            gaps.append(check)
    gaps += [Gap(CHECK, f"{i.number} on {i.asset.tag} (occurred {_day(i.occurred_on)}) was marked recorded in error: it is left out of this "
                        "section's figures and table", reverse("web:incident", args=[i.number]), i.number) for i in in_error]

    rows = [_row(i, holds.get(i.pk, [])) for i in listed]
    table = Table(key="incidents", title="Device incidents", columns=list(COLUMNS), rows=lambda: iter(rows), count=len(rows),
                  links={0: INCIDENT, 1: DEVICE}, empty="No device incident occurred in this period, and none was open at its end.")
    return Section(
        key=KEY, title=TITLE,
        topic="Devices suspected in a death, serious injury, or serious illness: held, investigated, decided, and reported as the Safe "
              "Medical Devices Act requires.",
        covers=f"Incidents that occurred {period.label}, and those open at its end; devices held {period.as_of_today}",
        figures=figures, gaps=gaps, tables=[table],
        notes=[
            "Listed: every incident that occurred in the period, and every one open at its end. An incident marked recorded in error is "
            "left out of the figures and the table and listed as a check.",
            "The report clock: a death, a serious injury, or an outcome not known yet, of a patient of the facility (its staff on duty "
            "included), is reported no more than 10 work days after the day the facility's clinical staff first knew (21 CFR 803.30): a "
            "death to the FDA and the manufacturer, a serious injury to the manufacturer (or the FDA when the manufacturer is not known). "
            "Work days are Monday to Friday but Federal holidays. A decision that it was not reportable stops the clock; the incidents "
            "counted with the clock are those it ran for at any time.",
            "Reports on time: every required report sent by its due date. Late: sent after it, or not recorded and past due today.",
            "The decision's deliberations, the narrative, and the report copies are in the facility's own event report, whose number "
            "each incident records; Cadence keeps no free text about an incident.",
            f"Checks are Cadence's measures, not rules of 21 CFR 803: an incident open more than {OPEN_DAYS} days since it occurred with "
            f"no device evaluation finding, and a device still held more than {RELEASE_DAYS} days after its investigation was completed.",
            "Only devices in Cadence are covered: incidents with disposables, implants, or patients' own devices are in the facility's "
            "event system.",
        ],
    )
