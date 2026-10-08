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
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Sequence
from apps.core.workdays import add_work_days, work_days_between
from apps.equipment.models import Asset, AssetStatus
from apps.workorders.models import OPEN_STATUSES, LateReason, Priority, WorkOrder, WoStatus, WoType

from . import notify
from . import permissions as perms
from .models import (
    CLOCK_OUTCOMES,
    EVENT_REFERENCE_VALIDATOR,
    FACILITY_PATIENTS,
    FAILURE_FINDINGS,
    HARM_OUTCOMES,
    OUTCOME_RANK,
    RELEASE_CHOICES,
    REPORT_NUMBER_VALIDATOR,
    Accessories,
    Affected,
    AwareChange,
    Basis,
    DecidedBy,
    EventLog,
    Finding,
    Incident,
    IncidentHold,
    Outcome,
    Release,
    Status,
)

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


def release_note(hold) -> str:
    """What keeps a released hold's device out of service now, in words for the screens and the API after release(): another open
    incident still holding it (its number: the reader has Incidents View), or, for a return to use, its incoming inspection or the
    repair holding it out (workorders.services.holding_repairs). "" when nothing does, or the hold is still active."""
    from apps.workorders.services import holding_repairs

    if hold.released_on is None:
        return ""
    device = Asset.objects.get(pk=hold.asset_id)
    other = holding_incidents(device).exclude(pk=hold.incident_id).first()
    if other is not None:
        return f"{device.tag} stays held: incident {other.number} still holds it."
    if hold.release != Release.RETURN_TO_USE or device.status == AssetStatus.IN_SERVICE:
        return ""
    if device.awaiting_inspection:
        return f"{device.tag} stays out of service until it passes its incoming inspection."
    repair = holding_repairs(device).order_by("opened_on", "number").first()
    if repair is not None:
        return f"{device.tag} stays out of service until repair {repair.number} is completed."
    return ""


# --- writing ---------------------------------------------------------------------------------------------------------------------

CHANGE_REASON_MAX = 100  # simple_history's history_change_reason column
EARLIEST = date(2000, 1, 1)  # review fix: a year typed with a slip (1926, 0026) is refused, never numbered
FACT_FIELDS = ("occurred_on", "aware_on", "outcome", "affected", "event_reference", "accessories", "event_log")
EVENT_REFERENCE_MAX = Incident._meta.get_field("event_reference").max_length
# An opened work order's problem when the device is not held (an incident recorded without a hold, with an investigation): the
# technician may test it. INVESTIGATION_PROBLEM says the device is evidence.
NOT_HELD_PROBLEM = "Investigation of a reported incident"
DECIDE_AGAIN = "New information: decide again"  # the history's reason when a failure finding clears a no_suggestion decision
DECISION_CLEARED = ("The device evaluation found a failure, so the decision that the information does not suggest the device caused or "
                    "contributed was cleared: decide again.")  # what the screens and the API say when record_finding cleared it
IN_ERROR_NOTE = "Cancelled: the incident was recorded in error"  # the opened work order's status history (no number)
RECIPIENT_WORDS = {"fda": "the FDA", "manufacturer": "the manufacturer"}

RECORD_PERMISSION = "Recording an incident needs Incidents Edit."
HOLD_PERMISSION = "Holding a device as evidence needs Incidents Edit and Equipment Edit."
OPEN_WORK_ORDER_PERMISSION = "Opening an investigation work order needs Incidents Edit and Work orders Edit."
FACTS_PERMISSION = "Changing an incident's facts needs Incidents Edit."
LOWER_PERMISSION = "Lowering the outcome needs Incidents Approve."
LATER_PERMISSION = "Moving the day clinical staff first knew later needs Incidents Approve."
CLOCK_PERMISSION = "A change that stops the report clock needs Incidents Approve."
FINDING_PERMISSION = "Recording the device evaluation's finding needs Incidents Edit."
DECIDE_PERMISSION = "Deciding whether an incident was reportable needs Incidents Approve."
REPORTS_PERMISSION = "Recording the reports sent needs Incidents Approve."
CUSTODY_PERMISSION = "Sending a held device to the manufacturer, or taking it back, needs Incidents Approve."
RELEASE_PERMISSION = "Releasing a held device needs Incidents Approve."
RETURN_PERMISSION = "Returning a held device to use needs Incidents Approve and Equipment Edit."
CLOSE_PERMISSION = "Closing or reopening an incident needs Incidents Approve."
IN_ERROR_PERMISSION = "Marking an incident recorded in error needs Incidents Approve."


def _check(by, allowed, message: str) -> None:
    """The level check every write starts with, for a user given as `by` (None is the system: the seed, tests)."""
    if by is not None and not allowed(by):
        raise PermissionDenied(message)


def _save(obj, by, reason: str, **kwargs) -> None:
    obj._change_reason = reason[:CHANGE_REASON_MAX]
    if by is not None:
        obj._history_user = by
    obj.save(**kwargs)


def _fmt(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _is_day(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _has_clock(incident) -> bool:
    """The facts start the 10-work-day clock (whatever the decision says): death, serious, or not known yet, of a patient of the
    facility."""
    return incident.outcome in CLOCK_OUTCOMES and incident.affected in FACILITY_PATIENTS


def _locked(incident) -> Incident:
    """The incident's row as it is now, locked until the transaction ends. Its own row only: a join would lock the device's row
    before the device's inspections, out of the module's lock order."""
    return Incident.objects.select_for_update().get(pk=incident.pk)


def _check_open(incident) -> None:
    if incident.status == Status.CLOSED:
        raise ValidationError(f"{incident.number} is closed: reopen it first.")
    if incident.status == Status.IN_ERROR:
        raise ValidationError(f"{incident.number} was recorded in error.")


def _returned(caller, current):
    """The current row, with the caller's copy (a page's, a serializer's) read again so it says the same."""
    if caller is not current:
        caller.refresh_from_db()
    return current


def _notify_after_commit(incident) -> None:
    """The managers hear once the transaction commits (never for a write rolled back); a failed send never fails the write."""
    transaction.on_commit(lambda: notify.recorded(incident), robust=True)


def _clean_facts(values: dict) -> dict:
    out = dict(values)
    for field in ("outcome", "affected", "event_reference", "accessories", "event_log"):
        out[field] = str(out[field] or "").strip()
    return out


def _facts_errors(v: dict, today: date) -> dict:
    """The field refusals for an incident's facts as they would stand (record_incident, update_facts), keyed by field."""
    errors = {}
    if v["outcome"] not in Outcome.values:
        errors["outcome"] = "Choose the outcome."
    if v["affected"] not in Affected.values:
        errors["affected"] = "Choose who was affected."
    elif v["outcome"] in HARM_OUTCOMES and v["affected"] == Affected.NONE:
        errors["affected"] = "Someone was harmed, or may have been: choose who (a patient, a staff member on duty, or another person)."
    occurred, aware = v["occurred_on"], v["aware_on"]
    if not _is_day(occurred):
        errors["occurred_on"] = "Enter the day it happened."
    elif occurred > today:
        errors["occurred_on"] = "Enter the day it happened, not a day to come."
    elif occurred < EARLIEST:
        errors["occurred_on"] = f"Enter the day it happened (from {EARLIEST.year} on)."
    if not _is_day(aware):
        errors["aware_on"] = "Enter the day the facility's clinical staff first knew."
    elif aware > today:
        errors["aware_on"] = "Enter the day clinical staff first knew, not a day to come."
    elif _is_day(occurred) and aware < occurred:
        errors["aware_on"] = f"Clinical staff cannot have known before it happened ({_fmt(occurred)})."
    reference = v["event_reference"]
    if len(reference) > EVENT_REFERENCE_MAX:
        errors["event_reference"] = f"Keep the event report number to {EVENT_REFERENCE_MAX} characters."
    elif reference:
        try:
            EVENT_REFERENCE_VALIDATOR(reference)
        except ValidationError as e:
            errors["event_reference"] = e.messages[0]
    if v["accessories"] not in ("", *Accessories.values):
        errors["accessories"] = "Choose what became of the accessories and disposables in use."
    if v["event_log"] not in ("", *EventLog.values):
        errors["event_log"] = "Choose what became of the device's event log."
    return errors


def _adopt_refusal(wo: WorkOrder, asset) -> str:
    """Why `wo` (read locked) cannot be the incident's investigation: an open repair on this device that is not already the
    investigation of another open incident."""
    if wo.asset_id != asset.pk:
        return f"{wo.number} is on {wo.asset.tag}, not {asset.tag}."
    if wo.type != WoType.REPAIR:
        return f"{wo.number} is not a repair: only an open repair request is taken as the investigation."
    if wo.status not in OPEN_STATUSES:
        return f"{wo.number} is {wo.get_status_display().lower()}: only an open repair request is taken as the investigation."
    other = Incident.objects.filter(work_order=wo, status=Status.OPEN).first()
    if other is not None:
        return f"{wo.number} is already the investigation of incident {other.number}."
    return ""


def _live_investigation(incident) -> WorkOrder | None:
    """The incident's investigation work order unless there is none or it was cancelled (then another may be opened)."""
    if incident.work_order_id is None:
        return None
    return WorkOrder.objects.filter(pk=incident.work_order_id).exclude(status=WoStatus.CANCELLED).first()


def _open_work_order(asset, *, by, today: date, held: bool) -> WorkOrder:
    """Open the investigation: a tagged-out REPAIR, priority high, unassigned, opened today, through create_work_order. For a device
    the incident holds (or is about to), create_work_order's own tag-out is left off and the work order is marked tagged out after:
    the hold takes the device out of service on its row, locked in order (set_incident_hold), once the hold has read the status it
    had (IncidentHold.status_before); the tag-out would write that row before the device's inspections were locked."""
    from apps.workorders.services import create_work_order

    fresh = Asset.objects.get(pk=asset.pk)  # create_work_order's tag-out reads the status on the copy it is given
    wo = create_work_order(asset=fresh, type=WoType.REPAIR, priority=Priority.HIGH, problem=INVESTIGATION_PROBLEM if held else NOT_HELD_PROBLEM,
                           created_by=by, opened_on=today, tag_out=not held)
    if held:
        wo.tagged_out = True
        wo.save(update_fields=["tagged_out", "updated_at"])
    return wo


def _hold(incident, asset, *, by, today: date) -> IncidentHold:
    """Hold `asset` for `incident` (its row locked, or just added): the IncidentHold row (held today, the device's status before),
    then the flag (equipment.services.set_incident_hold). A hold this incident had on the device and released is taken up again (one
    row per incident and device; its history keeps the first)."""
    from apps.equipment.services import _locked_row, set_incident_hold

    existing = IncidentHold.objects.select_for_update().filter(incident=incident, asset_id=asset.pk).first()
    if existing is not None and existing.active:
        raise ValidationError({"asset": f"{incident.number} already holds {asset.tag}."})
    device = _locked_row(asset)  # its open inspections, then its row: the status it has now
    if device.status in (AssetStatus.MISSING, AssetStatus.RETIRED):
        raise ValidationError({"asset": f"{device.tag} is {device.get_status_display().lower()}: it cannot be held."})
    hold = existing or IncidentHold(tenant=incident.tenant, incident=incident, asset=device)
    hold.held_on, hold.status_before = today, device.status
    hold.sent_on = hold.back_on = hold.released_on = hold.released_by = None
    hold.release = ""
    _save(hold, by, "Held again" if existing is not None else "Held")
    set_incident_hold(device, by=by, today=today)
    if asset is not device:
        asset.refresh_from_db()
    return hold


def _set_investigation(incident, wo: WorkOrder, by) -> None:
    incident.work_order, incident.opened_work_order = wo, True
    _save(incident, by, "Investigation opened")


def _late_reasons(hold, *, by, today: date) -> None:
    """The device's PMs that missed their due date while it was held (due from the day it was held to today, missed as of today:
    apps.pm.services.missed_pms) and have no reason get LateReason.INCIDENT_HOLD, through workorders.services.set_late_reason (which
    refuses the others: skipped)."""
    from apps.pm.services import missed_pms
    from apps.pm.windows import windows
    from apps.workorders.services import set_late_reason

    pms = (missed_pms(today, w=windows()).filter(asset_id=hold.asset_id, due_on__gte=hold.held_on, due_on__lte=today, late_reason="")
           .order_by("due_on", "number"))
    for wo in pms:
        try:
            set_late_reason(wo, LateReason.INCIDENT_HOLD, by=by, today=today)
        except ValidationError:
            continue


def _missed_while_held(hold, today: date) -> list:
    """The pks of the device's PMs that missed their due date while it was held, with no late reason yet (_late_reasons' list),
    read without a lock so the release can lock them in number order with the device's other work first."""
    from apps.pm.services import missed_pms
    from apps.pm.windows import windows

    return list(missed_pms(today, w=windows()).filter(asset_id=hold.asset_id, due_on__gte=hold.held_on, due_on__lte=today, late_reason="")
                .values_list("pk", flat=True))


def _lock_device_work(holds, today: date, *also) -> None:
    """Every work order the release of `holds` may write, before any of them is written (review fix: lock order): the held devices'
    open work orders, the PMs they missed while held, and `also`, all in one number order (workorders.services.lock_work's order, which
    every writer of one device's work keeps: a sweep per device would lock `also` ahead of another device's lower numbers)."""
    missed = [pk for hold in holds for pk in _missed_while_held(hold, today)]
    rows = WorkOrder.objects.select_for_update().filter(Q(asset_id__in=[h.asset_id for h in holds], status__in=OPEN_STATUSES)
                                                        | Q(pk__in=[*missed, *also])).order_by("number")
    list(rows.values_list("pk", flat=True))


def _end_hold(hold, kind: str, *, by, today: date) -> None:
    """The hold row's end (its incident locked): released today, how, by whom; then the late PMs' reason. Work orders first: the
    device's row comes after (_release_device)."""
    hold.released_on, hold.release, hold.released_by = today, kind, by
    _save(hold, by, Release(kind).label)
    _late_reasons(hold, by=by, today=today)


def _release_device(hold, incident, *, kind: str, by) -> None:
    """The device after its hold ended, on its row locked (its inspections, then its row: a release by another incident at the same
    moment waits here, then sees this one). While another open incident holds it, nothing changes. Otherwise the flag clears and:
    a return to use puts it in service unless it waits for its incoming inspection or a repair holds it (release_note says which);
    "recorded in error" restores its status before the hold, but never in service or on loan while it waits or a repair holds it
    (out of service then); keep_out and kept_by_manufacturer leave the status as it is."""
    from apps.equipment.services import _locked_row, clear_incident_hold
    from apps.workorders.services import holding_repairs

    device = _locked_row(hold.asset)
    if holding_incidents(device).exclude(pk=incident.pk).exists():
        return
    to_status = None
    if kind == Release.RETURN_TO_USE:
        if not device.awaiting_inspection and not holding_repairs(device).exists():
            to_status = AssetStatus.IN_SERVICE
    elif kind == Release.IN_ERROR:
        to_status = hold.status_before
        if to_status in (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN) and (device.awaiting_inspection or holding_repairs(device).exists()):
            to_status = AssetStatus.OUT_OF_SERVICE
    clear_incident_hold(device, to_status=to_status, by=by)


def _locked_hold(hold) -> tuple[Incident, IncidentHold]:
    """The hold's incident, then the hold, locked and read as they are now; refused unless the incident is open and the hold active."""
    incident = Incident.objects.select_for_update().get(pk=hold.incident_id)
    current = IncidentHold.objects.select_for_update().get(pk=hold.pk)
    _check_open(incident)
    if not current.active:
        raise ValidationError(f"{incident.number} released {current.asset.tag} on {_fmt(current.released_on)}.")
    return incident, current


@transaction.atomic
def record_incident(*, asset, occurred_on=None, aware_on=None, outcome, affected, event_reference="", hold=True, work_order=None,
                    open_work_order=None, accessories="", event_log="", by=None, today=None) -> Incident:
    """Record an incident on `asset` (Incidents Edit; holding also Equipment Edit; opening a work order also Work orders Edit).
    occurred_on defaults to today (or the adopted work order's opened_on); aware_on to occurred_on; occurred_on <= aware_on <= today.
    A harm outcome (HARM_OUTCOMES) needs affected other than "no one". `work_order`: an open REPAIR work order on this device to adopt
    as the investigation (the request it was reported as). Holding always means an investigation: hold=True with no work_order opens
    one (REPAIR, priority high, INVESTIGATION_PROBLEM, tag_out=True, unassigned; opened_work_order=True); hold=False opens one only when
    open_work_order is True. Holding refuses a missing or retired device (record it without a hold). Numbered IN-<yy>-<nnnn>
    (Sequence "incident-<yy>"; a day before EARLIEST is refused). Sets report_due_on (due_date). After commit, notify.recorded(incident) when it has a clock.

    An adopted work order keeps everything (assignee, time, tag-out, priority) and the incident never opens a second. The number is
    taken first, then the work order is opened (or the adopted one locked), then the device's inspections and row (the hold): every
    record_incident takes the numbering first, so two never wait on each other's device. Refusals of the facts come together, keyed by
    field; holding a missing or retired device is keyed "hold", a work order that cannot be adopted "work_order"."""
    from apps.workorders.services import lock_work

    today = today or timezone.localdate()
    adopting = work_order is not None
    opening = not adopting and (bool(hold) or bool(open_work_order))
    _check(by, perms.can_record, RECORD_PERMISSION)
    if hold:
        _check(by, perms.can_hold, HOLD_PERMISSION)
    if opening:
        _check(by, perms.can_open_work_order, OPEN_WORK_ORDER_PERMISSION)
    if occurred_on is None:
        occurred_on = work_order.opened_on if adopting else today
    facts = _clean_facts({"occurred_on": occurred_on, "aware_on": occurred_on if aware_on is None else aware_on, "outcome": outcome,
                          "affected": affected, "event_reference": event_reference, "accessories": accessories, "event_log": event_log})
    errors = _facts_errors(facts, today)
    if hold:
        status = Asset.objects.filter(pk=asset.pk).values_list("status", flat=True).first()
        if status in (AssetStatus.MISSING, AssetStatus.RETIRED):
            errors["hold"] = f"{asset.tag} is {AssetStatus(status).label.lower()}: record the incident without holding it."
    if errors:
        raise ValidationError(errors)
    yy = facts["occurred_on"].year % 100
    number = f"IN-{yy:02d}-{Sequence.next(f'incident-{yy:02d}', asset.tenant):04d}"  # one counter per printed year (review fix)
    wo = None
    if adopting:
        lock_work(asset.pk, work_order.pk)  # the device's work in number order before any one of it (review fix: lock order)
        wo = WorkOrder.objects.select_for_update().get(pk=work_order.pk)
        refusal = _adopt_refusal(wo, asset)
        if refusal:
            raise ValidationError({"work_order": refusal})
    elif opening:
        lock_work(asset.pk)  # the device's work before the work order numbering, as a failed PM's completion takes them (review fix)
        wo = _open_work_order(asset, by=by, today=today, held=bool(hold))
    incident = Incident(tenant=asset.tenant, number=number, asset=asset, work_order=wo, opened_work_order=opening, created_by=by, **facts)
    incident.report_due_on = due_date(outcome=incident.outcome, affected=incident.affected, aware_on=incident.aware_on, reportable=None)
    _save(incident, by, "Recorded")
    if hold:
        _hold(incident, asset, by=by, today=today)
    if incident.report_due_on is not None:
        _notify_after_commit(incident)
    return incident


@transaction.atomic
def hold_device(incident, asset, *, by=None, today=None) -> IncidentHold:
    """Hold another device (or the suspect device) for an open incident (Incidents Edit and Equipment Edit): an IncidentHold row with
    the device's status before, then equipment.services.set_incident_hold. A device this incident already holds is refused; a
    missing or retired one too (keyed "asset"). Holding always comes with an investigation: an incident with none (or only a
    cancelled one) opens it first, as open_investigation does (also Work orders Edit). The device may already be held by another
    incident (both hold it; its status before is then what the first hold made it)."""
    today = today or timezone.localdate()
    _check(by, perms.can_hold, HOLD_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    if _live_investigation(current) is None:
        from apps.workorders.services import lock_work

        _check(by, perms.can_open_work_order, OPEN_WORK_ORDER_PERMISSION)
        lock_work(current.asset_id)  # the suspect device's work before the numbering (review fix: lock order)
        suspect_held = asset.pk == current.asset_id or current.holds.filter(asset_id=current.asset_id, released_on__isnull=True).exists()
        _set_investigation(current, _open_work_order(current.asset, by=by, today=today, held=suspect_held), by)
    hold = _hold(current, asset, by=by, today=today)
    _returned(incident, current)
    return hold


@transaction.atomic
def update_facts(incident, *, by=None, today=None, aware_reason="", **fields) -> Incident:
    """Change occurred_on, aware_on, outcome, affected, event_reference, accessories, event_log on an open incident. Edit: raise the
    outcome (OUTCOME_RANK), move aware_on earlier, set the others. Approve: lower the outcome, move aware_on later (aware_reason from
    AwareChange, required: the history's reason). Once decided, outcome and affected change only through decide. Recomputes
    report_due_on.

    Also Approve: a change of who was affected that stops the clock (a patient, or staff on duty, to another person), as lowering the
    outcome does. Once decided, the event report number may be corrected but never cleared (the decision points to it), and the
    incident cannot have happened after it was decided or reported. A change that starts the clock (the outcome raised to serious or
    not known yet) tells the managers after commit, as recording one does. Nothing changed, nothing saved."""
    today = today or timezone.localdate()
    unknown = set(fields) - set(FACT_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    _check(by, perms.can_record, FACTS_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    before = {f: getattr(current, f) for f in FACT_FIELDS}
    after = _clean_facts({**before, **fields})
    changed = {f for f in FACT_FIELDS if after[f] != before[f]}
    if not changed:
        return _returned(incident, current)
    errors = _facts_errors(after, today)
    if current.reportable is not None:
        decided = f"{current.number} was decided on {_fmt(current.decided_on)}" if current.decided_on else f"{current.number} was decided"
        if "outcome" in changed:
            errors["outcome"] = f"{decided}: change the outcome by deciding again."
        if "affected" in changed:
            errors["affected"] = f"{decided}: change who was affected by deciding again (Decide again takes both)."
        if "event_reference" in changed and not after["event_reference"]:
            errors["event_reference"] = "The decision points to this event report: correct its number, never clear it."
    if "occurred_on" in changed and "occurred_on" not in errors:
        for day, what in ((current.decided_on, "decided"), (current.fda_reported_on, "reported to the FDA"),
                          (current.manufacturer_reported_on, "reported to the manufacturer")):
            if day is not None and after["occurred_on"] > day:
                errors["occurred_on"] = f"It was {what} on {_fmt(day)}: it cannot have happened after that."
                break
    later = "aware_on" in changed and _is_day(after["aware_on"]) and after["aware_on"] > before["aware_on"]
    aware_reason = str(aware_reason or "").strip()
    if later and aware_reason not in AwareChange.values:
        errors["aware_reason"] = "Choose why the day clinical staff first knew moves later."
    if errors:
        raise ValidationError(errors)
    due = due_date(outcome=after["outcome"], affected=after["affected"], aware_on=after["aware_on"], reportable=current.reportable)
    if by is not None and not perms.can_decide(by):
        if "outcome" in changed and OUTCOME_RANK[after["outcome"]] < OUTCOME_RANK[before["outcome"]]:
            raise PermissionDenied(LOWER_PERMISSION)
        if later:
            raise PermissionDenied(LATER_PERMISSION)
        if current.report_due_on is not None and due is None:
            raise PermissionDenied(CLOCK_PERMISSION)
    started = current.report_due_on is None and due is not None
    for field in changed:
        setattr(current, field, after[field])
    current.report_due_on = due
    _save(current, by, AwareChange(aware_reason).label if later else "Facts changed")
    if started:
        _notify_after_commit(current)
    return _returned(incident, current)


@transaction.atomic
def decide(incident, *, outcome, basis, decided_on, decided_by, affected=None, by=None, today=None) -> Incident:
    """Record whether it was reportable (Incidents Approve). `outcome` is the final one (never unknown), and `affected` who was
    affected as it is now known (None: as recorded). may_have needs death or serious injury of a patient of the facility; not_serious
    needs injury or no harm; no_suggestion is refused while the finding is a failure (FAILURE_FINDINGS); not_patient needs affected
    "visitor or another person"; a harm outcome needs someone affected. event_reference is required on a harm incident.
    occurred_on <= decided_on <= today. Sets reportable (basis == may_have), recorded_by, report_due_on. An open incident only.

    A harm incident here is one whose recorded or final outcome harmed someone (HARM_OUTCOMES) with someone affected: the
    deliberations are in that event report. Deciding again replaces the decision (the history keeps the one before). Review fix:
    once decided, the outcome and who was affected change only here (update_facts says so), so new information about either (a
    visitor who was staff on duty, a near miss that turned out to be a death) is decided again with both; a decision that starts
    the clock tells the managers, as recording one does."""
    today = today or timezone.localdate()
    _check(by, perms.can_decide, DECIDE_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    outcome, basis, decided_by = (str(v or "").strip() for v in (outcome, basis, decided_by))
    affected = current.affected if affected is None else str(affected or "").strip()
    errors = {}
    if outcome not in Outcome.values:
        errors["outcome"] = "Choose the outcome as it is now known."
    elif outcome == Outcome.UNKNOWN:
        errors["outcome"] = "Choose the outcome as it is now known: a decision never leaves it not known."
    if affected not in Affected.values:
        errors["affected"] = "Choose who was affected."
    elif outcome in HARM_OUTCOMES and affected == Affected.NONE:
        errors["affected"] = "Someone was harmed, or may have been: choose who (a patient, a staff member on duty, or another person)."
    if basis not in Basis.values:
        errors["basis"] = "Choose the basis for the decision."
    if decided_by not in DecidedBy.values:
        errors["decided_by"] = "Choose who decided."
    if not _is_day(decided_on):
        errors["decided_on"] = "Enter the day it was decided."
    elif decided_on > today:
        errors["decided_on"] = "Enter the day it was decided, not a day to come."
    elif decided_on < current.occurred_on:
        errors["decided_on"] = f"It cannot have been decided before it happened ({_fmt(current.occurred_on)})."
    if "outcome" not in errors and "basis" not in errors and "affected" not in errors:
        if basis == Basis.MAY_HAVE and outcome not in (Outcome.DEATH, Outcome.SERIOUS_INJURY):
            errors["basis"] = "A report is required only for a death or a serious injury: choose that outcome, or another basis."
        elif basis == Basis.MAY_HAVE and affected not in FACILITY_PATIENTS:
            errors["basis"] = ("A report is required only for a patient of the facility or a staff member on duty: the person affected "
                               "was not one.")
        elif basis == Basis.NOT_SERIOUS and outcome not in (Outcome.INJURY, Outcome.NO_HARM):
            errors["basis"] = "Not serious means the final outcome was an injury that was not serious, or no harm."
        elif basis == Basis.NO_SUGGESTION and current.finding in FAILURE_FINDINGS:
            errors["basis"] = (f"The device evaluation found a failure ({Finding(current.finding).label.lower()}): that suggests the "
                               "device may have caused or contributed. Choose another basis.")
        elif basis == Basis.NOT_PATIENT and affected != Affected.OTHER:
            errors["basis"] = f"The person affected is {Affected(affected).label.lower()}, not a visitor or another person."
    # The recorded facts or the final ones harmed someone: either way the deliberations are in the event report (a decision that stops
    # a running clock points to it too).
    harm = (outcome in HARM_OUTCOMES and affected != Affected.NONE) or (current.outcome in HARM_OUTCOMES and current.affected != Affected.NONE)
    if harm and not current.event_reference:
        errors["event_reference"] = "Enter the facility's event report number first: the deliberations on this decision are there."
    if errors:
        raise ValidationError(errors)
    had_clock = current.report_due_on is not None
    current.outcome, current.affected, current.basis, current.decided_on, current.decided_by = outcome, affected, basis, decided_on, decided_by
    current.reportable = basis == Basis.MAY_HAVE
    current.recorded_by = by
    current.report_due_on = due_date(outcome=outcome, affected=affected, aware_on=current.aware_on, reportable=current.reportable)
    _save(current, by, "Decided: reportable" if current.reportable else "Decided: not reportable")
    if not had_clock and current.report_due_on is not None:
        _notify_after_commit(current)
    return _returned(incident, current)


@transaction.atomic
def record_reports(incident, *, fda_reported_on=None, manufacturer_reported_on=None, report_number, by=None, today=None) -> Incident:
    """Record the reports sent (Incidents Approve): dates between occurred_on and today, the report number in 803.3(x)'s form, its
    year the earliest report date's year, unique in the facility. A date already recorded may be corrected (history keeps it).

    Both dates are set as given (None: not sent; so a date recorded against the wrong recipient can be moved); at least one is
    needed. An open incident only, at any decision (the clock never waits for one)."""
    today = today or timezone.localdate()
    _check(by, perms.can_decide, REPORTS_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    errors, dates = {}, {}
    for field, value in (("fda_reported_on", fda_reported_on), ("manufacturer_reported_on", manufacturer_reported_on)):
        if value in (None, ""):
            dates[field] = None
        elif not _is_day(value):
            errors[field] = "Enter a date."
        elif value > today:
            errors[field] = "Enter the day the report went out, not a day to come."
        elif value < current.occurred_on:
            errors[field] = f"A report cannot have gone out before the incident ({_fmt(current.occurred_on)})."
        else:
            dates[field] = value
    sent = [d for d in dates.values() if d is not None]
    if not errors and not sent:
        errors["fda_reported_on"] = "Enter the day the report went to the FDA, to the manufacturer, or both."
    number = str(report_number or "").strip()
    if not number:
        errors["report_number"] = "Enter the report number (0123456789-2026-0001)."
    else:
        try:
            REPORT_NUMBER_VALIDATOR(number)
        except ValidationError as e:
            errors["report_number"] = e.messages[0]
        else:
            other = Incident.objects.filter(report_number=number).exclude(pk=current.pk).first()
            if sent and int(number[11:15]) != min(sent).year:
                errors["report_number"] = (f"The report number's year ({number[11:15]}) is the year the report went out "
                                           f"({min(sent).year}).")
            elif other is not None:
                errors["report_number"] = f"Incident {other.number} has report number {number}: each report has its own."
    if errors:
        raise ValidationError(errors)
    corrected = bool(current.fda_reported_on or current.manufacturer_reported_on or current.report_number)
    current.fda_reported_on, current.manufacturer_reported_on = dates["fda_reported_on"], dates["manufacturer_reported_on"]
    current.report_number = number
    _save(current, by, "Reports corrected" if corrected else "Reports recorded")
    return _returned(incident, current)


@transaction.atomic
def record_finding(incident, finding, *, by=None) -> Incident:
    """Record the device evaluation's result (Incidents Edit). A failure finding after a no_suggestion decision clears the decision
    (history reason "New information: decide again") and the caller says so (the returned incident's reportable is None again).

    The returned incident carries `decision_cleared` (True when this cleared it: DECISION_CLEARED is the words). Clearing the
    decision brings back the clock when the facts have one (report_due_on from aware_on, as before the decision), and the managers
    hear after commit."""
    _check(by, perms.can_record, FINDING_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    finding = str(finding or "").strip()
    if finding not in Finding.values:
        raise ValidationError({"finding": "Choose what the device evaluation found."})
    current.decision_cleared = False
    if finding == current.finding:
        _returned(incident, current)
        return current
    current.finding = finding
    reason = "Finding recorded"
    started = False
    if finding in FAILURE_FINDINGS and current.basis == Basis.NO_SUGGESTION:
        current.reportable, current.basis, current.decided_on, current.decided_by, current.recorded_by = None, "", None, "", None
        due = due_date(outcome=current.outcome, affected=current.affected, aware_on=current.aware_on, reportable=None)
        started = current.report_due_on is None and due is not None
        current.report_due_on = due
        current.decision_cleared = True
        reason = DECIDE_AGAIN
    _save(current, by, reason)
    if started:
        _notify_after_commit(current)
    _returned(incident, current)
    return current


@transaction.atomic
def open_investigation(incident, *, by=None, today=None):
    """Open an investigation work order for an open incident that has none, or whose work order was cancelled (Incidents Edit and
    Work orders Edit): a tagged-out REPAIR as record_incident opens it. Refused, keyed "work_order", while it has one that was not
    cancelled. Returns the work order."""
    today = today or timezone.localdate()
    _check(by, perms.can_open_work_order, OPEN_WORK_ORDER_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    live = _live_investigation(current)
    if live is not None:
        raise ValidationError({"work_order": f"{current.number} has its investigation: {live.number} ({live.get_status_display().lower()})."})
    held = current.holds.filter(asset_id=current.asset_id, released_on__isnull=True).exists()
    wo = _open_work_order(current.asset, by=by, today=today, held=held)
    _set_investigation(current, wo, by)
    _returned(incident, current)
    return wo


@transaction.atomic
def sent_to_manufacturer(hold, *, on, by=None) -> IncidentHold:
    """The held device went to the manufacturer for evaluation (Incidents Approve): the hold stays (custody inside it). `on` from the
    day it was held (or came back from an earlier trip) to today; refused while it is already there. A second trip replaces the
    first's days (the history keeps them)."""
    today = timezone.localdate()
    _check(by, perms.can_decide, CUSTODY_PERMISSION)
    _incident, current = _locked_hold(hold)
    tag = current.asset.tag
    if current.with_manufacturer:
        raise ValidationError({"sent_on": f"{tag} is with the manufacturer since {_fmt(current.sent_on)}: record it back first."})
    earliest, since = (current.back_on, "came back") if current.back_on else (current.held_on, "was held")
    if not _is_day(on):
        raise ValidationError({"sent_on": "Enter the day it went to the manufacturer."})
    if on > today:
        raise ValidationError({"sent_on": "Enter the day it went to the manufacturer, not a day to come."})
    if on < earliest:
        raise ValidationError({"sent_on": f"{tag} {since} on {_fmt(earliest)}: it went to the manufacturer on or after that day."})
    current.sent_on, current.back_on = on, None
    _save(current, by, "Sent to the manufacturer")
    return _returned(hold, current)


@transaction.atomic
def back_from_manufacturer(hold, *, on, by=None) -> IncidentHold:
    """The held device came back from the manufacturer (Incidents Approve), on or after it was sent, by today."""
    today = timezone.localdate()
    _check(by, perms.can_decide, CUSTODY_PERMISSION)
    _incident, current = _locked_hold(hold)
    tag = current.asset.tag
    if not current.with_manufacturer:
        raise ValidationError({"back_on": f"{tag} is not with the manufacturer: record that it went there first."})
    if not _is_day(on):
        raise ValidationError({"back_on": "Enter the day it came back."})
    if on > today:
        raise ValidationError({"back_on": "Enter the day it came back, not a day to come."})
    if on < current.sent_on:
        raise ValidationError({"back_on": f"{tag} went to the manufacturer on {_fmt(current.sent_on)}: it came back on or after that day."})
    current.back_on = on
    _save(current, by, "Back from the manufacturer")
    return _returned(hold, current)


def _release_refusal(incident, hold, kind: str, today: date) -> str:
    """Why `hold` cannot end as `kind` today ("" when it can)."""
    tag = hold.asset.tag
    if kind == Release.KEPT_BY_MANUFACTURER:
        return "" if hold.with_manufacturer else f"Record that {tag} went to the manufacturer first."
    if hold.with_manufacturer:
        return (f"{tag} is with the manufacturer (sent {_fmt(hold.sent_on)}): record it back first, or release it as kept by the "
                "manufacturer.")
    if kind != Release.RETURN_TO_USE:
        return ""
    if hold.asset_id == incident.asset_id:
        wo = WorkOrder.objects.filter(pk=incident.work_order_id).first() if incident.work_order_id else None
        if wo is None:
            return f"{tag} returns to use once its investigation is done: open the investigation work order and complete it."
        if wo.status == WoStatus.CANCELLED:
            return (f"Investigation {wo.number} was cancelled: open another and complete it before {tag} returns to use, or release it "
                    "kept out of service.")
        if wo.status not in (WoStatus.COMPLETED, WoStatus.CLOSED):
            return f"Complete investigation {wo.number} before {tag} returns to use."
    if _has_clock(incident) and incident.reportable is None:
        return f"Decide whether {incident.number} was reportable before {tag} returns to use."
    missing = reports_missing(incident)
    if missing:
        return f"Record the report to {_and([RECIPIENT_WORDS[r] for r in missing])} before {tag} returns to use."
    next_pm_on = Asset.objects.filter(pk=hold.asset_id).values_list("next_pm_on", flat=True).first()
    if next_pm_on is not None and next_pm_on < today:
        return (f"{tag}'s PM was due {_fmt(next_pm_on)}: release it kept out of service, do the PM, then return it to service from its "
                "drawer.")
    return ""


@transaction.atomic
def release(hold, release, *, by=None, today=None) -> IncidentHold:
    """End a hold (Incidents Approve): Release.return_to_use (also Equipment Edit), keep_out, or kept_by_manufacturer. Return to use
    is refused while the device is with the manufacturer; for the suspect device until the investigation work order is completed or
    closed; on an incident with a clock until it is decided and, when reportable, the required reports are recorded; and when the
    device's next PM date has passed (release it kept out, do the PM, then return it from its drawer). The device goes in service only
    when no other open incident holds it, it is not awaiting its incoming inspection, and no repair holds it (the message names what
    does); while another incident holds it the flag stays and the status does not change. The device's PMs that missed their due date
    while it was held, with no late reason, get LateReason.INCIDENT_HOLD.

    The release succeeds when the device stays out: release_note(hold) has the words. keep_out is refused while the device is with
    the manufacturer, and kept_by_manufacturer unless it is there (803.32 asks for the day it was sent). Refusals keyed "release".
    Locks: the incident, the hold, the device's open work orders and late PMs in number order, then its row."""
    today = today or timezone.localdate()
    kind = str(release or "").strip()
    _check(by, perms.can_decide, RELEASE_PERMISSION)
    if kind not in RELEASE_CHOICES:
        raise ValidationError({"release": "Choose how the hold ends: returned to use, kept out of service, or kept by the manufacturer."})
    if kind == Release.RETURN_TO_USE:
        _check(by, perms.can_return_to_use, RETURN_PERMISSION)
    incident, current = _locked_hold(hold)
    refusal = _release_refusal(incident, current, kind, today)
    if refusal:
        raise ValidationError({"release": refusal})
    _lock_device_work([current], today)
    _end_hold(current, kind, by=by, today=today)
    _release_device(current, incident, kind=kind, by=by)
    return _returned(hold, current)


@transaction.atomic
def close(incident, *, by=None, today=None) -> Incident:
    """Close (Incidents Approve): every hold released; the investigation work order completed, closed, or cancelled; a finding; a
    decision when someone was harmed; the required reports when reportable.

    Everything missing is refused at once, keyed by field: "holds", "work_order", "finding", "reportable", and the missing report's
    date ("fda_reported_on", "manufacturer_reported_on")."""
    today = today or timezone.localdate()
    _check(by, perms.can_decide, CLOSE_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    errors = {}
    held = sorted(IncidentHold.objects.filter(incident=current, released_on__isnull=True).values_list("asset__tag", flat=True))
    if held:
        errors["holds"] = f"Release {_and(held)} first."
    wo = WorkOrder.objects.filter(pk=current.work_order_id).first() if current.work_order_id else None
    if wo is not None and wo.status in OPEN_STATUSES:
        errors["work_order"] = f"Complete or cancel investigation {wo.number} first."
    if not current.finding:
        errors["finding"] = "Record what the device evaluation found."
    if needs_decision(current):
        errors["reportable"] = "Decide whether it was reportable."
    for recipient in reports_missing(current):
        errors[f"{recipient}_reported_on"] = f"Record the report to {RECIPIENT_WORDS[recipient]}."
    if errors:
        raise ValidationError(errors)
    current.status, current.closed_on, current.closed_by = Status.CLOSED, today, by
    _save(current, by, "Closed")
    return _returned(incident, current)


@transaction.atomic
def reopen(incident, *, by=None) -> Incident:
    """Reopen a closed incident (Incidents Approve). Its holds stay released (hold a device again from the incident); one recorded in
    error stays so."""
    _check(by, perms.can_decide, CLOSE_PERMISSION)
    current = _locked(incident)
    if current.status == Status.IN_ERROR:
        raise ValidationError(f"{current.number} was recorded in error: it stays so.")
    if current.status != Status.CLOSED:
        raise ValidationError(f"{current.number} is open.")
    current.status, current.closed_on, current.closed_by = Status.OPEN, None, None
    _save(current, by, "Reopened")
    return _returned(incident, current)


@transaction.atomic
def recorded_in_error(incident, *, by=None, today=None) -> Incident:
    """An open incident recorded on the wrong device, or by mistake (Incidents Approve): every hold released with Release.in_error,
    the device's status before the hold restored (when no other incident holds it); the work order cancelled only when the incident
    opened it and it is open; status in_error. Kept, never counted (a binder CHECK).

    A device waiting for its incoming inspection, or held out by a repair, never goes back in service or on loan this way (out of
    service then). An adopted work order is left as it is (the request it was goes on). The held days' late PMs get their reason, as
    any release gives it (the device was held all the same). Locks: the incident, the holds and the late PMs, the work order it
    opened, then each device's inspections and row, in the devices' order (every work order before any device).

    Review fixes: never a side door around Equipment or Work orders. A hold whose device would go back in service or on loan needs
    Equipment Edit too (as a return to use does: perms.can_return_to_use), and cancelling the work order it opened needs Work orders
    Edit (wo_perms.can_transition); refused in words before anything changes. The report number and the report days are cleared
    (the history keeps them): a report sent about the wrong device belongs to the incident recorded on the right one, which records
    it under the same number."""
    from apps.workorders import permissions as wo_perms
    from apps.workorders.services import change_status

    today = today or timezone.localdate()
    _check(by, perms.can_decide, IN_ERROR_PERMISSION)
    current = _locked(incident)
    _check_open(current)
    holds = list(IncidentHold.objects.select_for_update().filter(incident=current, released_on__isnull=True).order_by("asset_id"))
    opened = (WorkOrder.objects.filter(pk=current.work_order_id, status__in=OPEN_STATUSES).first()
              if current.opened_work_order and current.work_order_id is not None else None)
    if by is not None:
        back_in_use = [h.asset.tag for h in holds if h.status_before in (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN)]
        if back_in_use and not perms.can_return_to_use(by):
            raise PermissionDenied(f"Marking {current.number} recorded in error puts {_and(back_in_use)} back in use: that needs "
                                   "Incidents Approve and Equipment Edit.")
        if opened is not None and not wo_perms.can_transition(by, opened.status, WoStatus.CANCELLED):
            raise PermissionDenied(f"Marking {current.number} recorded in error cancels {opened.number}: that needs Work orders Edit.")
    _lock_device_work(holds, today, *([opened.pk] if opened is not None else []))
    for hold in holds:
        _end_hold(hold, Release.IN_ERROR, by=by, today=today)
    if opened is not None:
        wo = WorkOrder.objects.select_for_update().get(pk=opened.pk)
        if wo.status in OPEN_STATUSES:
            if wo.status == WoStatus.IN_PROGRESS:  # in progress cannot be cancelled: back to open first
                change_status(wo, WoStatus.OPEN, by=by, note=IN_ERROR_NOTE, as_of=today)
            change_status(wo, WoStatus.CANCELLED, by=by, note=IN_ERROR_NOTE, as_of=today)
    for hold in holds:
        _release_device(hold, current, kind=Release.IN_ERROR, by=by)
    current.status = Status.IN_ERROR
    current.report_number, current.fda_reported_on, current.manufacturer_reported_on = "", None, None
    _save(current, by, "Recorded in error")
    return _returned(incident, current)
