"""
Device incidents (slice 28): every write, and the read helpers every screen, the API, the binder, and the guards share.

The rules (21 CFR 803; spec in BUILD_PLAN's slice 28 note):
- A user facility reports information that reasonably suggests a device may have caused or contributed (use error included) to a
  death or serious injury of a patient of the facility (its staff on duty included): a death to the FDA and the manufacturer, a
  serious injury to the manufacturer (the FDA in its place when the manufacturer cannot be identified), no more than 10 work days
  (apps.core.workdays) after the day its clinical staff first knew (aware_on). The clock never waits for the investigation.
- report_due_on is stored and written only here (due_date): the 10th work day after aware_on while the outcome is death, serious, or
  not known yet, the person a patient of the facility, and no decision says not reportable.
- The decision records reportable (yes / no), its basis (Basis), the day and the body that decided, and who typed it; the
  deliberations are in the facility's event report (event_reference, required for a decision on a harm incident).

The hold: holding a device sets Asset.incident_hold through equipment.services.set_incident_hold (the flag's one writer pair, on the
locked row). While any open incident holds a device nobody uses, repairs, or tests it: equipment.services.set_status refuses every
move, and no work order but an investigation of an incident holding it starts or completes (workorders.services.change_status reads
investigation_of). Holding always comes with an investigation work order: the open repair the incident was reported as (adopted: it
keeps its assignee, time, and tag-out) or one the incident opens (a tagged-out REPAIR, unassigned).

Lock order, everywhere: the incident's and the work order's rows and their Sequence numbers first; then the device's open inspections
and its row (equipment.services._locked_row). Opening a work order (Sequence) before taking the device's row avoids a deadlock with a
tagged-out portal request (which takes the numbering, then the device).

Every write: transaction.atomic, refusals as ValidationError keyed by field in plain words, levels checked here for a user given as
`by` (PermissionDenied; by=None is the system: the seed, tests), history with a _change_reason. The device's own history never names
the incident (Equipment View reads it); the incident's history does.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.db.models import Count, Q
from django.urls import reverse

from apps.core.workdays import add_work_days, work_days_between

from . import permissions as perms
from .models import CLOCK_OUTCOMES, FACILITY_PATIENTS, HARM_OUTCOMES, Affected, Incident, IncidentHold, Outcome, Status

REPORT_WORK_DAYS = 10  # 803.30: no more than 10 work days after the day the facility became aware
SOON_WORK_DAYS = 3  # the nav badge turns hot, and the reminder goes out, with this many work days left
HELD_WORDS = "Held by Clinical Engineering: do not use, repair, or test it."  # for readers without Incidents View (no number)
INVESTIGATION_PROBLEM = "Investigation of a reported incident: device held as evidence"  # an opened work order's problem (no details)
NOTES_PLACEHOLDER = ("Technical findings only: what was tested and found. No patient details: the event is in the facility's event "
                     "report.")


# --- reading -------------------------------------------------------------------------------------------------------------------------

def due_date(*, outcome: str, affected: str, aware_on: date | None, reportable: bool | None) -> date | None:
    """The report's due date for these facts: the 10th work day after aware_on while the incident has a clock, else None."""
    if reportable is False or aware_on is None:
        return None
    if outcome in CLOCK_OUTCOMES and affected in FACILITY_PATIENTS:
        return add_work_days(aware_on, REPORT_WORK_DAYS)
    return None


def needs_decision(incident: Incident) -> bool:
    """Someone was harmed (or may have been) and nobody has decided yet whether it was reportable."""
    return incident.outcome in HARM_OUTCOMES and incident.affected != Affected.NONE and incident.reportable is None


def required_recipients(incident: Incident) -> tuple[str, ...]:
    """Who must receive the report once it is decided reportable: ("fda", "manufacturer") for a death, ("manufacturer",) for a
    serious injury (the FDA's copy is accepted in its place: 803.30(a)(2), a manufacturer that cannot be identified); () otherwise."""
    if incident.reportable is not True:
        return ()
    if incident.outcome == Outcome.DEATH:
        return ("fda", "manufacturer")
    return ("manufacturer",)


def reports_missing(incident: Incident) -> tuple[str, ...]:
    """The required recipients with no report recorded (a serious injury's report to the FDA counts for the manufacturer's)."""
    need = required_recipients(incident)
    if need == ("manufacturer",):
        return () if (incident.manufacturer_reported_on or incident.fda_reported_on) else need
    sent = {"fda": incident.fda_reported_on, "manufacturer": incident.manufacturer_reported_on}
    return tuple(r for r in need if sent[r] is None)


def reports_done_by(incident: Incident) -> date | None:
    """The day the last required report went out (None while one is missing, or none is required)."""
    need = required_recipients(incident)
    if not need or reports_missing(incident):
        return None
    if need == ("manufacturer",):
        return min(d for d in (incident.manufacturer_reported_on, incident.fda_reported_on) if d)
    return max(incident.fda_reported_on, incident.manufacturer_reported_on)


@dataclass(frozen=True)
class Clock:
    due: date | None  # report_due_on
    left: int  # work days after today up to the due date (0 when due today or past)
    overdue: bool  # past due with the decision or a required report missing
    soon: bool  # due within SOON_WORK_DAYS work days with the decision or a required report missing
    pending: bool  # the decision or a required report is missing (the clock is running)
    recipients: tuple[str, ...]  # who must get the report (an undecided clock: who would, if it is reportable)


def clock(incident: Incident, today: date) -> Clock:
    """Where the incident's 10-work-day clock stands today. An undecided incident with a clock counts as reportable (if the device
    cannot be ruled out, it is): its recipients are the outcome's."""
    due = incident.report_due_on
    if incident.reportable is None and due is not None:
        recipients = ("fda", "manufacturer") if incident.outcome == Outcome.DEATH else ("manufacturer",)
        pending = True
    else:
        recipients = required_recipients(incident)
        pending = bool(reports_missing(incident))
    pending = pending and due is not None and incident.status == Status.OPEN
    left = work_days_between(today, due) if due else 0
    return Clock(due=due, left=left, overdue=pending and today > due, soon=pending and left <= SOON_WORK_DAYS, pending=pending,
                 recipients=recipients)


def needing_action():
    """Open incidents whose clock is running: undecided, or reportable with a required report missing (the nav badge, the list's
    first group, the reminders). report_due_on is None once decided not reportable."""
    missing = (Q(outcome=Outcome.DEATH) & (Q(fda_reported_on__isnull=True) | Q(manufacturer_reported_on__isnull=True))) | (
        ~Q(outcome=Outcome.DEATH) & Q(fda_reported_on__isnull=True, manufacturer_reported_on__isnull=True))
    return Incident.objects.filter(Q(status=Status.OPEN, report_due_on__isnull=False) & (Q(reportable__isnull=True) | (Q(reportable=True) & missing)))


def badge(today: date) -> tuple[int, bool]:
    """(how many need action, whether one is due within SOON_WORK_DAYS work days or overdue): one query."""
    soon = add_work_days(today, SOON_WORK_DAYS)
    row = needing_action().aggregate(n=Count("pk"), hot=Count("pk", filter=Q(report_due_on__lte=soon)))
    return row["n"], bool(row["hot"])


def holding_incidents(asset):
    """The open incidents holding `asset` (an active IncidentHold), oldest first."""
    return (Incident.objects.filter(status=Status.OPEN, holds__asset=asset, holds__released_on__isnull=True)
            .distinct().order_by("occurred_on", "number"))


def active_holds(asset):
    return IncidentHold.objects.filter(asset=asset, released_on__isnull=True, incident__status=Status.OPEN).select_related("incident")


def investigation_of(asset) -> set:
    """The pks of the work orders that may start and complete on `asset` while it is held: the investigations of the open incidents
    holding it (by identity, never by type)."""
    return set(holding_incidents(asset).exclude(work_order=None).values_list("work_order_id", flat=True))


def hold_words(asset, user) -> dict | None:
    """The hold as a banner says it to `user`: {"text", "url", "number"}; None when the device is not held. The incident's number and
    link only for Incidents View (never for a scoped user); everyone else reads HELD_WORDS."""
    if not getattr(asset, "incident_hold", False):
        return None
    if user is None or not perms.can_view(user):
        return {"text": HELD_WORDS, "url": "", "number": ""}
    hold = active_holds(asset).order_by("held_on", "created_at").first()
    if hold is None:  # the flag without an open hold (an incident closed by hand in the database): say it plainly
        return {"text": HELD_WORDS, "url": "", "number": ""}
    inc = hold.incident
    held_on = hold.held_on
    return {"text": f"Held as evidence for incident {inc.number} since {held_on:%b} {held_on.day}, {held_on.year}: do not use, repair, "
                    "or test it until the incident releases it.",
            "url": reverse("web:incident", args=[inc.number]), "number": inc.number}


# --- writing (wave 1a fills these in; the signatures and rules are fixed) ------------------------------------------------------------

def record_incident(*, asset, occurred_on=None, aware_on=None, outcome, affected, event_reference="", hold=True, work_order=None,
                    open_work_order=None, accessories="", event_log="", by=None, today=None) -> Incident:
    """Record an incident on `asset` (Incidents Edit; holding also Equipment Edit; opening a work order also Work orders Edit).
    occurred_on defaults to today (or the adopted work order's opened_on); aware_on to occurred_on; occurred_on <= aware_on <= today.
    A harm outcome (HARM_OUTCOMES) needs affected other than "no one". `work_order`: an open REPAIR work order on this device to adopt
    as the investigation (the request it was reported as). Holding always means an investigation: hold=True with no work_order opens
    one (REPAIR, priority high, INVESTIGATION_PROBLEM, tag_out=True, unassigned; opened_work_order=True); hold=False opens one only when
    open_work_order is True. Holding refuses a missing or retired device (record it without a hold). Numbered IN-<yy>-<nnnn>
    (Sequence "incident-<yyyy>"). Sets report_due_on (due_date). After commit, notify.recorded(incident) when it has a clock."""
    raise NotImplementedError


def hold_device(incident, asset, *, by=None, today=None) -> IncidentHold:
    """Hold another device (or the suspect device) for an open incident (Incidents Edit and Equipment Edit): an IncidentHold row with
    the device's status before, then equipment.services.set_incident_hold. A device this incident already holds is refused."""
    raise NotImplementedError


def update_facts(incident, *, by=None, today=None, aware_reason="", **fields) -> Incident:
    """Change occurred_on, aware_on, outcome, affected, event_reference, accessories, event_log on an open incident. Edit: raise the
    outcome (OUTCOME_RANK), move aware_on earlier, set the others. Approve: lower the outcome, move aware_on later (aware_reason from
    AwareChange, required: the history's reason). Once decided, outcome and affected change only through decide. Recomputes
    report_due_on."""
    raise NotImplementedError


def decide(incident, *, outcome, basis, decided_on, decided_by, by=None, today=None) -> Incident:
    """Record whether it was reportable (Incidents Approve). `outcome` is the final one (never unknown). may_have needs death or
    serious injury of a patient of the facility; not_serious needs injury or no harm; no_suggestion is refused while the finding is a
    failure (FAILURE_FINDINGS); not_patient needs affected "visitor or another person". event_reference is required on a harm
    incident. occurred_on <= decided_on <= today. Sets reportable (basis == may_have), recorded_by, report_due_on. An open incident
    only."""
    raise NotImplementedError


def record_reports(incident, *, fda_reported_on=None, manufacturer_reported_on=None, report_number, by=None, today=None) -> Incident:
    """Record the reports sent (Incidents Approve): dates between occurred_on and today, the report number in 803.3(x)'s form, its
    year the earliest report date's year, unique in the facility. A date already recorded may be corrected (history keeps it)."""
    raise NotImplementedError


def record_finding(incident, finding, *, by=None) -> Incident:
    """Record the device evaluation's result (Incidents Edit). A failure finding after a no_suggestion decision clears the decision
    (history reason "New information: decide again") and the caller says so (the returned incident's reportable is None again)."""
    raise NotImplementedError


def open_investigation(incident, *, by=None, today=None):
    """Open an investigation work order for an open incident that has none, or whose work order was cancelled (Incidents Edit and
    Work orders Edit): a tagged-out REPAIR as record_incident opens it."""
    raise NotImplementedError


def sent_to_manufacturer(hold, *, on, by=None) -> IncidentHold:
    """The held device went to the manufacturer for evaluation (Incidents Approve): the hold stays (custody inside it)."""
    raise NotImplementedError


def back_from_manufacturer(hold, *, on, by=None) -> IncidentHold:
    """The held device came back from the manufacturer (Incidents Approve), on or after it was sent."""
    raise NotImplementedError


def release(hold, release, *, by=None, today=None) -> IncidentHold:
    """End a hold (Incidents Approve): Release.return_to_use (also Equipment Edit), keep_out, or kept_by_manufacturer. Return to use
    is refused while the device is with the manufacturer; for the suspect device until the investigation work order is completed or
    closed; on an incident with a clock until it is decided and, when reportable, the required reports are recorded; and when the
    device's next PM date has passed (release it kept out, do the PM, then return it from its drawer). The device goes in service only
    when no other open incident holds it, it is not awaiting its incoming inspection, and no repair holds it (the message names what
    does); while another incident holds it the flag stays and the status does not change. The device's PMs that missed their due date
    while it was held, with no late reason, get LateReason.INCIDENT_HOLD."""
    raise NotImplementedError


def close(incident, *, by=None, today=None) -> Incident:
    """Close (Incidents Approve): every hold released; the investigation work order completed, closed, or cancelled; a finding; a
    decision when someone was harmed; the required reports when reportable."""
    raise NotImplementedError


def reopen(incident, *, by=None) -> Incident:
    """Reopen a closed incident (Incidents Approve)."""
    raise NotImplementedError


def recorded_in_error(incident, *, by=None, today=None) -> Incident:
    """An open incident recorded on the wrong device, or by mistake (Incidents Approve): every hold released with Release.in_error,
    the device's status before the hold restored (when no other incident holds it); the work order cancelled only when the incident
    opened it and it is open; status in_error. Kept, never counted (a binder CHECK)."""
    raise NotImplementedError
