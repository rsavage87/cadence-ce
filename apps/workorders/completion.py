"""
Completing a work order (slice 15): the resolution, and for a PM the checklist results step by step and the overall result
(PmResult), a failed PM opening a follow-up repair work order. Completes through apps.workorders.services.change_status, so the
device (last and next PM, a tagged-out repair's return to service) and the portal's done email behave as they always have.

The rules (complete_work_order checks them all and reports every problem at once, keyed by the modal's field names):
- Only this facility's work order, only from a status that may move to completed (in progress), only once it is assigned (a
  technician or a vendor did the work), and never on a date before it was opened. `blocker` says which of these stops it now.
- Slice 24: a PM still open is completed in one step (done in one visit): completing starts it first, through services.change_status
  (open to in progress, so the status history keeps both moves) in the completion's transaction, for someone who may make both
  moves (permissions.can_transition; no user, as for services and commands: allowed). A repair still needs Start: its start date
  matters for downtime and turnaround (starts_on_completion).
- hours (optional, slice 24): time logged with the completion, a labor line for costs.default_technician on the day it is completed,
  by costs.add_labor's rules (at most 24 h for one technician on one day) in the same transaction. A refusal is an error on hours,
  and nothing is saved.
- resolution: what was found and done. Trimmed, at most RESOLUTION_MAX characters. Required for repairs and every other type;
  for a PM required with Pass with minor repair (what was repaired) and with Fail when there is no checklist (what failed),
  otherwise optional: a passed PM gets "PM completed per <procedure code>, all checks passed", a failed one names its failed steps.
- A PM needs its overall result (PmResult). Only a PM records one.
- When the device's model has a procedure with a checklist, every step gets pass, fail, or N/A (results, in the checklist's
  order); a step that records a reading takes one (at most READING_MAX characters) when it is pass or fail, and N/A keeps none.
  Not every step may be N/A. `signature` (checklist_signature of the steps the form showed) refuses results entered against a
  checklist that has since been revised.
- The result agrees with the steps: Pass has no failed step; Pass with minor repair may have failed steps put right during the
  PM, and the resolution says what was repaired; Fail has at least one failed step (or, with no checklist, a reason).
- The checklist is stored as it was (text, measure, result, reading) in WorkOrder.checklist_results, so revising the procedure
  later never rewrites a PM on record. Completing a reopened PM again records its results anew (the history keeps the old ones).
- A Fail opens a follow-up repair work order (follow_up_of = the PM; the problem names the PM and its failed steps; priority
  high for a life-support or high-risk model, else normal; follow_up_assignee: a vendor's PM to the same vendor when they do
  repairs on the device, else the PM's technician when active and credentialed for it, else unassigned for a manager). Never a
  second one, whoever completes it: a reopened PM whose repair is still open records the
  failure on that repair. With open_repair=False the failure is recorded (as a note) on the device's open repair instead, which
  is allowed only when it has one: no duplicate repair inflates the failure counts that AEM and MTBF read.
- tag_out (default on for a Fail) takes a device that is in service out of service until its repair is done: through
  create_work_order(tag_out=True) for a new repair (the portal's path: completing that repair returns the device), or, for a
  repair already open, by marking that repair tagged out and moving the device through equipment.services.set_status. Other
  results ignore open_repair and tag_out.
- late_reason (optional, slice 25): why a PM completed after its due date was late (LateReason), recorded through
  services.set_late_reason in the same transaction, after the completion (the survey binder reads it). Blank keeps a reason already
  recorded. Only a PM completed after its on-time window (completes_late; slice 27: the facility's window by the model's class, the
  due date by default) takes one; anything else, or a value not listed, is refused under late_reason with the rest, and nothing is
  saved. Never required: an API client or a hurried technician still completes. A PM done after its due date but inside its window
  is on time: no reason, and its device's next PM is set from its due date (services.pm_schedule_anchor).
- One transaction: a refusal anywhere (the start, the hours, the repair, the tag-out, the completion, the late reason) leaves nothing
  behind.
- The status history says the result: "PM passed", "PM passed with minor repair", "PM failed; WO-26-0057 opened for the repair".

Slice 26, incoming inspections (apps.workorders.inspections; the device's side is equipment.services):
- An inspection is done to a checklist like a PM (procedure_for): its model's PM procedure when that has steps, else the incoming
  checklist (inspections.INCOMING: received complete, electrical safety with the leakage reading, functional check, recalls and
  software, tagged). The checklist is optional on an inspection: every step recorded (pass, fail, or N/A, readings as for a PM, the
  signature checked), or none. A still-open inspection is completed in one step, as a PM is (starts_on_completion).
- inspection_result (InspectionResult) is required when the device is waiting for its incoming inspection (Asset.awaiting_inspection),
  optional on any other inspection (where it moves nothing), and refused on any other type, keyed inspection_result. Only a PM
  records a PM result. Passed with a failed step is refused (as Pass is on a PM). A pass's resolution defaults to "Incoming inspection
  passed per <procedure code, or the incoming checklist>, all checks passed" when every step was recorded, and is required otherwise
  (what was checked); a fail needs the resolution (what failed); an inspection with no result needs it as any work order does.
- Passed on a waiting device: the device passes (equipment.services.pass_incoming_inspection, through services._on_completed): it
  stops waiting, its PM clock starts, it goes in service unless a tagged-out repair holds it, and its other open incoming inspections
  are cancelled.
- Failed on a waiting device: never a repair (a repair would count a never-used device against its model in the AEM evidence and
  MTBF). A re-inspection: the device's open incoming inspection when it has one (a reopened inspection failing again: never a second
  one open), else a new one (follow_up_of the failed inspection, due REINSPECTION_DUE_DAYS later, assigned through services.assign
  to the same vendor for vendor service, else the same technician while active: reinspection_assignee; else unassigned). tag_out
  (default on) takes a device in use before its inspection (use_before_inspection) out of service.
- Reopened: an inspection that passed, of a device no longer waiting (its pass cleared the flag), stays passed when completed again
  (blank keeps Passed; Failed is refused: tag the device out and open a repair). A failed one completed Failed again records on the
  re-inspection still open. The status history says "Incoming inspection passed", "Incoming inspection failed; WO-26-0431 opened to
  re-inspect" (result_note).
- Locks: the device's open inspections first, in number order (inspections.lock_open), then the work order, then the device's row
  in the pass: two completions (or use_before_inspection) on one device at once take turns.

Slice 28, a device held as evidence for an incident investigation (Asset.incident_hold): only the investigation of an open incident
holding it is completed (or started). blocker says so first for any other work order still open on it (services.hold_blocker), and
services.change_status refuses the start and the completion on the device's locked row. A failed PM's repair (or a failed
inspection's re-inspection) is opened before the start, so its number is taken before the device's row. Completing the investigation
leaves the device held and out of service: the incident's release returns it.

Slice 29, a temporary device (a rental, vendor loaner, or demo unit: equipment.services.owner_maintains): its incoming inspection is
done to inspections.TEMPORARY_INCOMING (the incoming checklist, the owner's PM label, the model's open recalls), never its model's PM
procedure (inspection_procedure), and it never has a PM (services.create_work_order). Passed is refused, keyed inspection_result, while
its owner's PM date is recorded and past (inspections.owner_pm_refusal: the owner does the PM, or its new date is recorded); Failed,
or no result where none is required, is not. A kept pass (a reopened inspection whose pass ended the wait) is never refused: it moves
nothing, and Failed is refused there.

Slice 30, checks after a repair (apps.workorders.repair_checks; CMS A-0724, NFPA 99, IEC 62353): every repair records how it ended,
repair_check (RepairCheck), keyed repair_check when missing or not one of the outcomes the user may choose (repair_outcomes):
- passed ("Checked: ready for use"): the standard post-repair checklist (procedure_for gives repair_checks.POST_REPAIR, never the
  model's PM procedure), every step recorded as for a PM (pass, fail, or N/A; a reading on a measured step that passed or failed; the
  signature checked; none recorded is one error, keyed checklist), never N/A on repair_checks.NEVER_NA (keyed step_<n>), and no step
  failed (keyed step_<n>: a failed check means it is not ready for use: keep_open, or not returned). The device goes back as a
  repaired one always has (services._on_completed: a tagged-out repair, or a device in repair, the last repair holding it).
- vendor_report ("Checked by the vendor"): only on vendor service, and only for a user who sees the whole facility (a scoped vendor
  account completing its own repair records the steps); checks_reference, the vendor's service report or field service order number
  (a token: apps.core.validators), is required, and no steps are taken (keyed checklist). The device goes back as for passed.
- not_returned ("Not returned to use": beyond repair, to be retired, back to its owner, kept by the manufacturer): no steps (keyed
  checklist). The repair is marked tagged out and a device in use or in repair moves out of service through equipment.services
  .set_status (a device held as evidence stays as its hold keeps it); services._on_completed never returns it, and reopening the
  repair and completing it as passed later does.
A repair's resolution stays required (what was found and done): no default. checks_reference with any other outcome, repair_check or
checks_reference on another type, and results on a type other than a PM, an inspection, or a repair are refused. The steps are stored
in checklist_results (passed only), the outcome in repair_check, the report number in checks_reference; the status history says it:
"Checked after repair; CE-10241 back in service", "Checked by the vendor, report FSO-123", "Not returned to use: CE-10241 stays out of
service". keep_open records failed checks on a repair that stays open (a note in the failed PM's format) and, with hold, holds the
device out (IEC 62353 6.2: equipment that fails is marked unsafe until put right).

Slice 31, vendor staff (apps.vendors): a vendor-service completion of any type may name the vendor's engineer responsible, as their
report names them (vendor_engineer: an engineer or its id, None for not named, KEEP to leave it as stored; or engineer_name, a typed
name: the engineer of the work order's vendor with that name, created when there is none), through vendors.services.engineer_choice
(a scoped vendor account only among its own company's engineers; anything else answers as an unknown id, keyed vendor_engineer), and
the vendor's report number (checks_reference) on any vendor job: optional (left out, the number on file stays: a re-completion after a
reopen), still required with "Checked by the vendor", refused on in-house work (REFERENCE_ONLY). Optional by design: the survey binder
judges each vendor job by the evidence on file, the engineer's training or the vendor's attestation (apps.vendors.rules), and calls out
the jobs no evidence covers.

Slice 33, what a repair found (repair_finding, RepairFinding; apps.workorders.findings, the one rule), keyed repair_finding with the
rest, never preselected:
- Required when the repair has none stored (findings.REQUIRED), whatever the outcome (not returned to use takes one too). KEEP (left
  out: the API), None, or blank keeps the finding stored: a reopened repair completed again keeps it unless another is chosen, and blank
  never clears one.
- A value is one of findings.choices_for(the repair as locked, the user): a scoped user (a vendor's or a requester's account) only the
  observations, not stated by the vendor only on vendor service by a user who sees the whole facility, never use error on an
  incident's investigation (findings.is_investigation, read after the repair's row is locked: an adoption waits for it), never "PM
  could have caught it" on a failed PM's repair (findings.found_on_pm: the PM caught it). Refused in words: findings.INVESTIGATION or
  findings.FOUND_ON_PM where that limit is why (the value is otherwise this user's to choose), else findings.NOT_OFFERED. A scoped user
  who cannot choose the finding stored (the facility's) leaves it as stored: sent nothing, or the stored value sent back.
- On any other type a finding is refused (findings.NOT_A_REPAIR). Stored with the completion (change_status saves the row, so the
  work order's history has it). keep_open records no finding: the repair is not over.

Slice 35, the test equipment used (apps.testequipment; the rule is its rules, the checks its services.check_uses), keyed test_equipment
with the rest, never preselected: KEEP (left out) keeps what the work order recorded (but a completion as checked by the vendor takes
off our uses the kept-use rule does not keep: the vendor's report names its own); a list of item ids (text or UUID) replaces the set as
a diff (each use added or taken off is a history row of its own); [] clears. A use added is recorded on today, the completion's day (its
used_on); a use that stays keeps its own day (a reopened work order's), however the work is completed again. Refused when the work is
not offered (imported; vendor service, except a repair completed with an outcome other than the vendor's report: the outcome chosen in
the same call), but for sending back the kept uses there (any other refused; the rest come off as KEEP takes them off), for a scoped
user (a non-empty list; an empty one changes nothing) or one without Equipment View, and for an unknown id or another facility's. A new
item must be usable today (in use at some moment of today: rules.in_use_today); one already on the work order passes whatever its
status; one is taken off only under the kept-use rule (rules.may_remove on the use's own day: it stays once a calibration dated on or
after that day is on file). Read on the locked rows: the items' rows are locked after lock_work and the work order's row, in pk order,
before anything else this takes (a failed PM's repair number, the device's row). keep_open records no test equipment (its day is not
the completion's).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.credentials.services import qualification
from apps.equipment.models import AssetStatus, RiskClass
from apps.pm.procedures import step_parts
from apps.pm.windows import window_of
from apps.tenants.context import get_current_tenant
from apps.testequipment import services as test_equipment_services
from apps.vendors import services as vendor_services

from . import costs, findings, inspections, repair_checks, services
from . import permissions as wo_perms
from .models import (
    ALLOWED_TRANSITIONS,
    OPEN_STATUSES,
    InspectionResult,
    LaborLine,
    LateReason,
    PmResult,
    Priority,
    RepairCheck,
    RepairFinding,
    WorkOrder,
    WorkOrderNote,
    WorkOrderStatusHistory,
    WoStatus,
    WoType,
)

RESOLUTION_MAX = 1000
READING_MAX = 60
PASS, FAIL, NA = "pass", "fail", "na"
STEP_RESULTS = {PASS: "Pass", FAIL: "Fail", NA: "N/A"}
DONE_STATUSES = (WoStatus.COMPLETED, WoStatus.CLOSED)
# The status history's note (and the toast's wording) for each result: a PM's (PmResult) and, slice 26, an incoming inspection's
# (InspectionResult; the two sets of values never overlap)
RESULT_NOTES = {PmResult.PASS: "PM passed", PmResult.PASS_MINOR_REPAIR: "PM passed with minor repair", PmResult.FAIL: "PM failed",
                InspectionResult.PASSED: "Incoming inspection passed", InspectionResult.FAILED: "Incoming inspection failed"}
# A failure recorded on a repair already open: the PM's completion note ends with the repair's number. The repair's follow_up_of names
# only the first PM it follows, so the survey binder (apps.reports.survey.maintenance) reads a later PM's repair from this note.
RECORDED_ON = "; recorded on open repair "
_CONTROL = "Remove the invisible control character from {what}."
# The hours box when there is nobody to credit them to (the technician assigned is no longer active, and the user has no profile)
NO_TECHNICIAN = "There is no active technician to log these hours for. Log them with the work order's Log time, choosing who did the work."


@dataclass
class Completion:
    work_order: WorkOrder
    follow_up: WorkOrder | None = None  # the repair this completion opened
    repair: WorkOrder | None = None  # the repair a failed PM was recorded on: the new follow-up, or one already open
    tagged_out: bool = False  # the device went out of service with this completion
    device_changed: bool = False  # its status or its last or next PM moved (the Equipment list shows them)
    started: bool = False  # an open PM, started as it was completed (slice 24)
    labor: LaborLine | None = None  # the hours logged with it (slice 24)
    reinspection: WorkOrder | None = None  # slice 26: the re-inspection a failed incoming inspection opened, or the open one it reused
    reinspection_opened: bool = False  # ...opened by this completion (else it was already open)
    passed: bool = False  # slice 26: the device stopped waiting for its incoming inspection with this completion (its pass)
    returned: bool = False  # slice 30: a repair's device went back in service with this completion (checked, or the vendor's report)


@dataclass
class KeptOpen:
    """What keep_open did (slice 30): the note it wrote on the repair, and whether the device went out of service with it."""
    work_order: WorkOrder
    note: WorkOrderNote
    held: bool = False  # the repair now holds the device out (marked tagged out)
    tagged_out: bool = False  # the device was in use and is now out of service


# --- reading -------------------------------------------------------------------------------------------------------------------

def procedure_for(wo: WorkOrder):
    """The procedure a PM work order is done to: its device's model's PM procedure, as it is now (or None). Slice 26: an incoming
    inspection's is inspection_procedure(its device), so checklist_of(procedure_for(wo)) is an inspection's checklist as it is a PM's.
    Slice 30: a repair's checks after it are the standard post-repair checklist (repair_checks.POST_REPAIR), never its model's PM
    procedure (a PM done on a repair never credits the PM). None for other types."""
    if wo.type == WoType.PM:
        return wo.asset.device_model.pm_procedure
    if wo.type == WoType.INSPECTION:
        return inspection_procedure(wo.asset)
    if wo.type == WoType.REPAIR:
        return repair_checks.POST_REPAIR
    return None


def inspection_procedure(asset):
    """The checklist an incoming inspection of `asset` is done to (slice 26): its model's PM procedure when that has steps, else the
    incoming checklist (inspections.INCOMING, a stand-in in the procedure's shape with a blank code). Slice 29: a temporary device's is
    inspections.TEMPORARY_INCOMING whatever its model's procedure (its owner maintains it: CE checks it before first use and never
    does the owner's PM). inspections.open_for estimates the inspection's hours from it."""
    from apps.equipment.services import owner_maintains  # equipment.services imports this app inside its functions; keep it one way

    if owner_maintains(asset):
        return inspections.TEMPORARY_INCOMING
    procedure = asset.device_model.pm_procedure
    return procedure if checklist_of(procedure) else inspections.INCOMING


def checklist_of(procedure) -> list[tuple[str, str | bool | None]]:
    """A procedure's steps as (text, measure): measure is None (nothing to record), True (a reading), or what to record. Nothing on
    file is dropped: a checklist saved as one string is a step per line, and an odd step shows what it has (pm.procedures.step_parts)."""
    steps = procedure.checklist if procedure is not None else None
    if steps in (None, "", [], {}):
        return []
    if isinstance(steps, str):
        steps = [line.strip() for line in steps.splitlines() if line.strip()]
    elif not isinstance(steps, (list, tuple)):
        steps = [steps]
    return [step_parts(s) for s in steps]


def checklist_signature(steps) -> str:
    """A short fingerprint of a checklist's steps, so results typed against one version are never saved against another."""
    raw = json.dumps([[text, measure] for text, measure in steps], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def starts_on_completion(wo: WorkOrder, by=None) -> bool:
    """Whether completing `wo` starts it first (slice 24): a PM still open, done in one visit, by someone who may both start and
    complete it (no user: services and commands); slice 26, an incoming inspection likewise. A repair is started on its own: its start
    date matters for downtime and turnaround."""
    if wo.type not in (WoType.PM, WoType.INSPECTION) or wo.status != WoStatus.OPEN:
        return False
    return by is None or (wo_perms.can_transition(by, WoStatus.OPEN, WoStatus.IN_PROGRESS)
                          and wo_perms.can_transition(by, WoStatus.IN_PROGRESS, WoStatus.COMPLETED))


def on_time_window(wo: WorkOrder, w=None):
    """The on-time window (apps.pm.windows.Window) of a PM work order: its group's, by its model's class today (slice 27). Reads the
    class only when the facility's two groups differ: the callers select the device and its model with the work order."""
    w = window_of(w)
    return w.high if w.uniform else w.for_class(wo.asset.device_model.risk_class)


def completes_late(wo: WorkOrder, today: date, w=None) -> bool:
    """Whether completing `wo` on `today` finishes a PM after its on-time window (slice 25; slice 27, the window: by the due date unless
    the facility chose otherwise, apps.pm.windows, by the model's class today): it is then one of the PMs not on time
    (apps.pm.services.missed_pms) and may record why (LateReason). The modal offers "Why was it late?" on these only. A PM done by its
    due date reads no window."""
    if wo.type != WoType.PM or wo.due_on is None or today <= wo.due_on:
        return False
    return today > on_time_window(wo, w).end(wo.due_on)


def inside_window(wo: WorkOrder, today: date, w=None):
    """The last day of `wo`'s on-time window when completing it on `today` finishes a PM after its due date but inside its window
    (slice 27: on time, so no reason for lateness, and its next PM is set from its due date: services.pm_schedule_anchor), else None.
    Always None under the default window."""
    if wo.type != WoType.PM or wo.due_on is None or today <= wo.due_on:
        return None
    end = on_time_window(wo, w).end(wo.due_on)
    return end if today <= end else None


def blocker(wo: WorkOrder, today: date | None = None, by=None) -> str:
    """Why `wo` cannot be completed now by `by` (None: anyone allowed), or "" when it can. Slice 28: a work order still open on a
    device held as evidence, other than the incident's investigation, says so first (services.hold_blocker): starting it is refused
    too, so "start it first" would send the user to another refusal."""
    today = today or timezone.localdate()
    if wo.status in OPEN_STATUSES:
        held = services.hold_blocker(wo)
        if held:
            return held
    if WoStatus.COMPLETED not in ALLOWED_TRANSITIONS[wo.status] and not starts_on_completion(wo, by):
        return {
            WoStatus.OPEN: f"{wo.number} has not been started. Start work on it first.",
            WoStatus.AWAITING_PARTS: f"{wo.number} is waiting on parts. Resume it when they arrive, then complete it.",
            WoStatus.COMPLETED: f"{wo.number} is already completed.",
            WoStatus.CLOSED: f"{wo.number} is closed.",
            WoStatus.CANCELLED: f"{wo.number} was cancelled.",
        }.get(wo.status, f"{wo.number} cannot be completed from {wo.get_status_display().lower()}.")
    if wo.assigned_to_id is None and not wo.vendor_service:
        return f"{wo.number} is not assigned. A CE manager assigns it to whoever does the work before it is completed."
    if today < wo.opened_on:
        return f"{wo.number} cannot be completed before it was opened on {wo.opened_on:%b} {wo.opened_on.day}, {wo.opened_on.year}."
    return ""


def _visible(qs, user):
    """`qs` narrowed to what `user` may see (apps.workorders.scoping, slice 16): a vendor completing their PM is never offered, told
    about, or given another company's or the facility's own repair. No user: everything (services and commands)."""
    from .scoping import work_orders  # scoping imports the models; keep this module importable on its own

    return qs if user is None else work_orders(user, qs)


def own_open_repair(wo: WorkOrder, user=None) -> WorkOrder | None:
    """The repair this PM opened at an earlier failure, while it is still open (a reopened PM failing again records on it)."""
    qs = WorkOrder.objects.filter(follow_up_of=wo, type=WoType.REPAIR, status__in=OPEN_STATUSES).order_by("opened_on", "number")
    return _visible(qs, user).first()


def other_open_repair(wo: WorkOrder, user=None) -> WorkOrder | None:
    """The device's oldest open repair work order that this PM did not open: a failure can be recorded on it instead."""
    qs = (WorkOrder.objects.filter(asset_id=wo.asset_id, type=WoType.REPAIR, status__in=OPEN_STATUSES)
          .exclude(follow_up_of=wo).exclude(pk=wo.pk).order_by("opened_on", "number"))
    return _visible(qs, user).first()


def recorded_steps(wo: WorkOrder) -> list[dict]:
    """The checklist as it was recorded (checklist_results), each step with n, text, measured, measure (what to record; "" for a
    plain reading), result, its label, and reading. Odd shapes show what they have rather than fail."""
    out = []
    for n, step in enumerate(wo.checklist_results or [], 1):
        if not isinstance(step, dict):
            step = {"text": str(step)}
        measure = step.get("measure")
        measured = measure not in (None, False, "")
        result = str(step.get("result") or "")
        out.append({"n": n, "text": str(step.get("text") or ""), "measured": measured, "measure": str(measure) if measured and measure is not True else "",
                    "result": result, "label": STEP_RESULTS.get(result, result or "—"), "reading": str(step.get("reading") or "")})
    return out


# --- checks --------------------------------------------------------------------------------------------------------------------

def _resolution(value, errors: dict) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\x00" in text:
        errors["resolution"] = _CONTROL.format(what="the resolution")
    elif len(text) > RESOLUTION_MAX:
        errors["resolution"] = f"Keep the resolution to {RESOLUTION_MAX} characters (this one has {len(text)})."
    return text


def _answered(raw) -> bool:
    """Whether a step's posted result says anything: a result chosen, or a reading typed (a reading of 0 is a reading)."""
    if isinstance(raw, dict):
        reading = raw.get("reading")
        return bool(str(raw.get("result") or "").strip()) or bool(("" if reading is None else str(reading)).strip())
    return isinstance(raw, str) and bool(raw.strip())


def _steps(steps, results, errors: dict, optional: bool = False) -> list[dict]:
    """The checklist as recorded: each step with its result and reading. Errors under step_<n> and reading_<n> (from 1). `optional`
    (an incoming inspection, slice 26): no step answered records no checklist ([]); one step answered asks for them all."""
    if results is not None and not isinstance(results, (list, tuple)):  # the API passes what it was sent (review fix: a 400, not a 500)
        errors["checklist"] = "Send the checklist as a list: one {result, reading} per step."
        return []
    results = list(results or [])
    if optional and not any(_answered(r) for r in results):
        return []
    if not steps:
        if any(results):
            errors["checklist"] = "This PM has no checklist on file. Say in the resolution what was checked."
        return []
    if results and len(results) != len(steps):
        errors["checklist"] = f"Record a result for each of the {len(steps)} steps."
        return []
    out = []
    for n, (text, measure) in enumerate(steps, 1):
        raw = results[n - 1] if results else {}
        if isinstance(raw, str):
            raw = {"result": raw}
        elif not isinstance(raw, dict):
            raw = {}
        result = str(raw.get("result") or "").strip().lower()
        reading = raw.get("reading")
        reading = " ".join(("" if reading is None else str(reading)).split())  # a reading of 0 is a reading
        if result not in STEP_RESULTS:
            errors[f"step_{n}"] = f"Step {n}: choose pass, fail, or N/A."
        if measure is None or result == NA:
            reading = ""  # no box for it, or the step was not done
        elif "\x00" in reading:
            errors[f"reading_{n}"] = _CONTROL.format(what="the reading")
        elif len(reading) > READING_MAX:
            errors[f"reading_{n}"] = f"Step {n}: keep the reading to {READING_MAX} characters."
        elif not reading and result in (PASS, FAIL):
            errors[f"reading_{n}"] = f"Step {n}: record the reading."
        out.append({"text": text, "measure": measure, "result": result, "reading": reading})
    if not any(k.startswith(("step_", "reading_")) for k in errors) and all(s["result"] == NA for s in out):
        errors["checklist"] = ALL_NA
    return out


ALL_NA = "Every step is marked N/A. Mark the steps that were done as pass or fail."
REVISED = "The procedure's checklist was revised while this was open. Check the steps again."


def _failed(snapshot: list[dict]) -> list[int]:
    return [n for n, s in enumerate(snapshot, 1) if s["result"] == FAIL]


def _numbers(ns: list[int]) -> str:
    """"step 2", "steps 2 and 4", "steps 1, 3 and 5"."""
    if len(ns) == 1:
        return f"step {ns[0]}"
    return "steps " + ", ".join(str(n) for n in ns[:-1]) + f" and {ns[-1]}"


def _failed_lines(snapshot: list[dict]) -> list[str]:
    lines = []
    for n in _failed(snapshot):
        s = snapshot[n - 1]
        lines.append(f"{n}. {s['text']}" + (f" (reading {s['reading']})" if s["reading"] else ""))
    return lines


def _check_inspection(result: str, snapshot: list[dict], resolution: str, errors: dict, *, required: bool) -> None:
    """An incoming inspection's result against its steps and resolution (slice 26). `required`: the device waits for it."""
    if "inspection_result" in errors:
        return  # already answered (a kept pass): one answer, never two that send the user to each other
    if result and result not in InspectionResult.values:
        errors["inspection_result"] = "Choose passed or failed."
        return
    if not result and required:
        errors["inspection_result"] = "Choose the inspection's result: passed or failed."
        return
    if any(k.startswith(("step_", "reading_")) or k == "checklist" for k in errors):
        return  # the steps come first; their errors say what to fix
    failed = _failed(snapshot)
    if result == InspectionResult.PASSED and failed:
        errors["inspection_result"] = (f"{_numbers(failed).capitalize()} failed. A device that fails a check fails its incoming inspection: "
                                       "choose Failed.")
    elif not result and failed:  # review fix: a failed step with no result would read as a passed inspection
        errors["inspection_result"] = f"{_numbers(failed).capitalize()} failed: record the result, Failed."
    elif "resolution" in errors or resolution:
        return
    elif result == InspectionResult.PASSED and not snapshot:
        errors["resolution"] = "Record the checklist, or say what was checked."
    elif result == InspectionResult.FAILED:
        errors["resolution"] = "Say what failed."
    elif not result:
        errors["resolution"] = "Say what was found and done."


def _check_result(wo, pm_result: str, snapshot: list[dict], has_checklist: bool, resolution: str, errors: dict, *,
                  inspection_result: str = "", result_required: bool = False) -> None:
    if inspection_result and wo.type != WoType.INSPECTION:
        errors["inspection_result"] = "Only an incoming inspection records an inspection result."
    if wo.type != WoType.PM:
        if pm_result:
            errors["pm_result"] = "Only a PM records a PM result."
        if wo.type == WoType.INSPECTION:
            _check_inspection(inspection_result, snapshot, resolution, errors, required=result_required)
        elif not resolution and "resolution" not in errors:
            errors["resolution"] = "Say what was found and done."
        return
    if not pm_result:
        errors["pm_result"] = "Choose the PM's result: pass, pass with minor repair, or fail."
        return
    if pm_result not in PmResult.values:
        errors["pm_result"] = "Choose pass, pass with minor repair, or fail."
        return
    if any(k.startswith(("step_", "reading_")) or k == "checklist" for k in errors):
        return  # the steps come first; their errors say what to fix
    failed = _failed(snapshot)
    if pm_result == PmResult.PASS and failed:
        errors["pm_result"] = (f"{_numbers(failed).capitalize()} failed. Choose Pass with minor repair if it was put right during the PM, "
                               "or Fail.")
    elif pm_result == PmResult.PASS_MINOR_REPAIR and not resolution and "resolution" not in errors:
        errors["resolution"] = "Say what was repaired during the PM."
    elif pm_result == PmResult.FAIL and has_checklist and not failed:
        errors["pm_result"] = "Mark the step that failed, or choose another result."
    elif pm_result == PmResult.FAIL and not has_checklist and not resolution and "resolution" not in errors:
        errors["resolution"] = "Say what failed."


# --- checks after a repair (slice 30) ------------------------------------------------------------------------------------------

# The outcomes in words, in the order the modal shows them (repair_outcomes)
REPAIR_CHECK_WORDS = {RepairCheck.PASSED: "checked and ready for use", RepairCheck.VENDOR_REPORT: "checked by the vendor",
                      RepairCheck.NOT_RETURNED: "not returned to use"}
REFERENCE_MAX = WorkOrder._meta.get_field("checks_reference").max_length
# A failed step on a repair completed as checked and ready for use (keyed step_<n>)
FAILED_CHECK = ("A failed check means it is not ready for use: keep the repair open and hold the device out, or complete it as not "
                "returned to use.")
NO_CHECKS = "Record each check after the repair: pass, fail, or N/A (steps 1 and 5 are never N/A)."
REPAIR_REVISED = "The checks after repair were revised while this was open. Check the steps again."
REPAIR_RESULTS_ONLY = "Only a PM, an incoming inspection, or a repair records checklist results."
REPAIR_CHECK_ONLY = "Only a repair records its checks after repair."
REFERENCE_ONLY = "Only vendor service records the vendor's report number."  # slice 31: any vendor job may (slice 30: repairs only)
KEEP_OPEN_PERMISSION = "Recording failed checks after a repair needs Work orders Edit."
# Where not returned to use takes the device from: in use, or in repair (the repair is over). keep_open's hold takes it only from in
# use (IN_USE): a device in repair stays in repair, its repair still open. Out of service already, missing, or retired: nothing to move
# (the repair, marked tagged out, holds it).
KEEP_OUT_FROM = (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN, AssetStatus.IN_REPAIR)


def vendor_report_allowed(wo: WorkOrder, user=None) -> bool:
    """Whether `user` may complete the repair `wo` as checked by the vendor (slice 30): on vendor service only, and only for a user who
    sees the whole facility (no user: services and commands). A scoped vendor account completing its own repair records the steps; the
    vendor's report number is what CE records on the vendor's behalf."""
    if wo.type != WoType.REPAIR or not wo.vendor_service:
        return False
    if user is None:
        return True
    from .scoping import is_scoped  # scoping imports the models; keep this module importable on its own

    return not is_scoped(user)


def repair_outcomes(wo: WorkOrder, user=None) -> list[str]:
    """The outcomes (RepairCheck values) `user` may complete the repair `wo` with, in the modal's order; [] for any other type."""
    if wo.type != WoType.REPAIR:
        return []
    return [RepairCheck.PASSED, *([RepairCheck.VENDOR_REPORT] if vendor_report_allowed(wo, user) else []), RepairCheck.NOT_RETURNED]


def _or(words: list[str]) -> str:
    """"a or b", "a, b, or c"."""
    if len(words) <= 2:
        return " or ".join(words)
    return ", ".join(words[:-1]) + f", or {words[-1]}"


def _check_repair(wo: WorkOrder, repair_check: str, steps, results, signature, by, errors: dict) -> list[dict]:
    """A repair's checks after it against the outcome chosen (slice 30, the module's rules): the steps as recorded for passed, else [].
    Errors keyed repair_check, checklist, step_<n>, and reading_<n>."""
    allowed = repair_outcomes(wo, by)
    if results is not None and not isinstance(results, (list, tuple)):  # the API passes what it was sent: a 400, never a 500
        errors["checklist"] = "Send the checklist as a list: one {result, reading} per step."
        results = None
    answered = any(_answered(r) for r in results or [])
    if repair_check not in RepairCheck.values:
        choices = _or([REPAIR_CHECK_WORDS[c] for c in allowed])
        errors["repair_check"] = f"Choose how the repair ended: {choices}." if not repair_check else f"Choose {choices}."
        return []
    if repair_check not in allowed:  # checked by the vendor, where it is not offered
        if not wo.vendor_service:
            errors["repair_check"] = (f"{wo.number} was not done by vendor service. Record the checks after the repair, or choose not "
                                      "returned to use.")
        else:
            errors["repair_check"] = ("Record the checks after the repair step by step. Checked by the vendor is for the facility's CE "
                                      "staff, recording the vendor's service report.")
        return []
    if repair_check != RepairCheck.PASSED:
        if answered and "checklist" not in errors:
            errors["checklist"] = ("The vendor's service report is the record of their checks: send no checklist with checked by the vendor."
                                   if repair_check == RepairCheck.VENDOR_REPORT else
                                   "A device not returned to use records no checks: send no checklist with not returned to use.")
        return []
    if "checklist" in errors:
        return []
    if signature is not None and signature != checklist_signature(steps):
        errors["checklist"] = REPAIR_REVISED
        return []
    if not answered:
        errors["checklist"] = NO_CHECKS
        return []
    snapshot = _steps(steps, results, errors)
    if len(snapshot) != len(steps):
        return snapshot  # the wrong number of results: _steps said so
    never = [i + 1 for i in repair_checks.NEVER_NA if snapshot[i]["result"] == NA]
    for n in never:
        errors.setdefault(f"step_{n}", f"Step {n} is never N/A after a repair: mark it pass or fail.")
    if never and errors.get("checklist") == ALL_NA:
        del errors["checklist"]  # the steps never N/A say it
    for n in _failed(snapshot):
        errors.setdefault(f"step_{n}", FAILED_CHECK)
    return snapshot


def _reference(value, errors: dict, *, vendor_service: bool, repair_check: str | None = None) -> str:
    """The vendor's service report or field service order number (slice 30; slice 31: on any vendor-service completion): required with
    checked by the vendor, optional on other vendor work, a token (apps.core.validators) of at most REFERENCE_MAX characters; refused,
    keyed checks_reference, on in-house work. `repair_check` is a repair's outcome (None: not a repair). "" when it is not kept."""
    text = str(value if value is not None else "").strip()
    required = repair_check == RepairCheck.VENDOR_REPORT
    if not text:
        if required:
            errors["checks_reference"] = "Enter the vendor's service report or field service order number."
        return ""
    if not (vendor_service or required):  # checked by the vendor on in-house work: repair_check's refusal says it
        errors["checks_reference"] = REFERENCE_ONLY
        return ""
    if repair_check is not None and repair_check not in RepairCheck.values:
        return ""  # no outcome yet: repair_check's error says what to fix
    if len(text) > REFERENCE_MAX:
        errors["checks_reference"] = f"Keep the report number to {REFERENCE_MAX} characters."
    else:
        try:
            for validator in WorkOrder._meta.get_field("checks_reference").validators:
                validator(text)
        except ValidationError as e:
            errors["checks_reference"] = " ".join(e.messages)
    return text


def finding_refusal(wo: WorkOrder, value: str, user=None, *, investigation: bool, from_pm: bool) -> str:
    """Why `user` may not record `value` (a RepairFinding value) on the repair `wo` (slice 33), in words: findings.INVESTIGATION or
    findings.FOUND_ON_PM where that limit is why (the value is otherwise this user's to choose), else findings.NOT_OFFERED. Shared by
    complete_work_order and services.set_repair_finding."""
    if value == RepairFinding.USE_ERROR and investigation and value in findings.choices_for(wo, user, investigation=False, from_pm=from_pm):
        return findings.INVESTIGATION
    if value == RepairFinding.PM_PREVENTABLE and from_pm and value in findings.choices_for(wo, user, investigation=investigation, from_pm=False):
        return findings.FOUND_ON_PM
    return findings.NOT_OFFERED


def _repair_finding(wo: WorkOrder, value, by, errors: dict):
    """What the repair found (slice 33, the module's rules), checked before anything is saved: the value to store, or KEEP to leave the
    finding stored as it is. Refusals keyed repair_finding in `errors`. `wo` is the row as locked: an incident adopting it as its
    investigation waits until this completion commits."""
    from .scoping import is_scoped  # scoping imports the models; keep this module importable on its own

    sent = "" if value is vendor_services.KEEP or value is None else str(value).strip()
    if wo.type != WoType.REPAIR:
        if sent:
            errors["repair_finding"] = findings.NOT_A_REPAIR
        return vendor_services.KEEP
    investigation, from_pm = findings.is_investigation(wo), findings.found_on_pm(wo)
    if findings.facility_holds(wo, by, investigation=investigation, from_pm=from_pm):
        # review fix: a scoped user gets one answer whatever they send (the stored value included), so no refusal tells what is stored
        if sent:
            errors["repair_finding"] = findings.FACILITY_RECORDED
        return vendor_services.KEEP
    if (not sent and wo.repair_finding and is_scoped(by)
            and wo.repair_finding not in findings.choices_for(wo, by, investigation=investigation, from_pm=from_pm)):
        # review fix: one the repair's limits now refuse is asked for as if none were recorded, so a blank never tells it was there
        errors["repair_finding"] = findings.REQUIRED
        return vendor_services.KEEP
    if sent and sent not in RepairFinding.values:
        errors["repair_finding"] = findings.NOT_OFFERED
        return vendor_services.KEEP
    stored = wo.repair_finding
    if not sent and not stored:
        errors["repair_finding"] = findings.REQUIRED
        return vendor_services.KEEP
    wanted = sent or stored
    # Kept as stored, a scoped user's too: the facility's finding stays whatever their own choices, while the repair's limits allow it
    # (choices_for with no user); a stored finding those limits now refuse is chosen again.
    keeping = wanted == stored and is_scoped(by)
    if wanted in findings.choices_for(wo, None if keeping else by, investigation=investigation, from_pm=from_pm):
        return vendor_services.KEEP if wanted == stored else wanted
    errors["repair_finding"] = finding_refusal(wo, wanted, by, investigation=investigation, from_pm=from_pm)
    return vendor_services.KEEP


def _hold_out(repair: WorkOrder, asset, by, why: str, *, moves=KEEP_OUT_FROM, save: bool = True) -> bool:
    """Hold the device out on `repair` (slice 30: not returned to use, or keep_open's hold): the repair marked tagged out (completing it
    as checked later returns the device, as for a repair opened with the tag-out), and a device whose status is in `moves` (in use or
    in repair; keep_open: in use) moved out of service through equipment's status rules, with `why` in its history. Never a device held
    as evidence: its hold keeps it out, and only its incident releases it (set_status refuses it). True when the device was in use and
    is now out of service. The device's open work orders are locked already (services.lock_work), so its row is read in order. `save`
    False: the caller saves the repair (a completion: change_status saves the whole row)."""
    from apps.equipment.services import set_status  # equipment.services imports this app inside its functions; keep it one way

    if not repair.tagged_out:
        repair.tagged_out = True
        if save:
            repair.save(update_fields=["tagged_out", "updated_at"])
    asset.refresh_from_db(fields=["status", "incident_hold"])
    if asset.status not in moves or asset.incident_hold:
        return False
    in_use = asset.status in IN_USE
    set_status(asset, AssetStatus.OUT_OF_SERVICE, by=by, note=why)
    asset.__dict__.pop("_change_reason", None)
    return in_use


def repair_note(wo: WorkOrder, done: Completion) -> str:
    """A repair's completion in the status history, and the toast's wording (slice 30): "Checked after repair; CE-10241 back in
    service", "Checked by the vendor, report FSO-123", "Not returned to use: CE-10241 stays out of service". "" for a repair completed
    with no outcome (before slice 30)."""
    tag = wo.asset.tag
    back = f"; {tag} back in service" if done.returned else ""
    if wo.repair_check == RepairCheck.PASSED:
        return f"Checked after repair{back}"
    if wo.repair_check == RepairCheck.VENDOR_REPORT:
        return f"Checked by the vendor, report {wo.checks_reference}{back}"
    if wo.repair_check == RepairCheck.NOT_RETURNED:
        status = wo.asset.status
        stays = "out of service" if status == AssetStatus.OUT_OF_SERVICE or wo.asset.incident_hold else wo.asset.get_status_display().lower()
        return f"Not returned to use: {tag} stays {stays}"
    return ""


def _kept_steps(steps, results, errors: dict) -> list[dict]:
    """keep_open's steps: those done as recorded (a step left blank was not done: N/A in the record), at least one answered."""
    if results is not None and not isinstance(results, (list, tuple)):
        errors["checklist"] = "Send the checklist as a list: one {result, reading} per step."
        return []
    results = list(results or [])
    if not any(_answered(r) for r in results):
        errors["checklist"] = "Record the check that failed, with its reading when it has one."
        return []
    return _steps(steps, [r if _answered(r) else {"result": NA} for r in results], errors)


@transaction.atomic
def keep_open(wo: WorkOrder, *, results, signature: str | None = None, hold: bool = True, by=None, today: date | None = None) -> KeptOpen:
    """A repair whose checks after it failed stays open (slice 30): the failed steps and their readings recorded as a note on it ("Checks
    after repair failed on Oct 9, 2026: 3. Equipment (touch) leakage ... (reading 640 µA)", the failed PM's format) and, with `hold`
    (default on), the device held out (_hold_out: the repair marked tagged out, a device in use moved out of service, one in repair left
    in repair; IEC 62353 6.2). `results` as complete_work_order takes them for a repair (a step left blank was not done); refused, keyed checklist,
    when none failed (complete it as checked instead), and as completing is refused when the repair cannot be completed now (blocker).
    Work orders Edit for a user given as `by` (PermissionDenied), as completing. Locks as complete_work_order: the device's open work
    and this one, then the device's row. `wo` is refreshed."""
    today = today or timezone.localdate()
    _check_tenant(wo)
    if by is not None and not wo_perms.can_keep_open(by):
        raise PermissionDenied(KEEP_OPEN_PERMISSION)
    services.lock_work(wo.asset_id, wo.pk)
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if locked.type != WoType.REPAIR:
        raise ValidationError(f"{locked.number} is not a repair: only a repair records checks after it.")
    reason = blocker(locked, today, by)
    if reason:
        raise ValidationError(reason)
    steps = checklist_of(repair_checks.POST_REPAIR)
    errors: dict[str, str] = {}
    snapshot: list[dict] = []
    if signature is not None and signature != checklist_signature(steps):
        errors["checklist"] = REPAIR_REVISED
    else:
        snapshot = _kept_steps(steps, results, errors)
    if not errors and not _failed(snapshot):
        errors["checklist"] = "No check failed. Complete the repair as checked and ready for use."
    if errors:
        raise ValidationError(errors)
    failed = "; ".join(_failed_lines(snapshot))
    note = services.add_note(locked, f"Checks after repair failed on {today:%b} {today.day}, {today.year}: {failed}"[:services.NOTE_MAX_LENGTH], by=by)
    kept = KeptOpen(work_order=locked, note=note)
    if hold:
        asset = locked.asset
        newly_held = not locked.tagged_out
        kept.held = True
        kept.tagged_out = _hold_out(locked, asset, by, f"Tagged out: checks after repair {locked.number} failed", moves=IN_USE)
        if newly_held or kept.tagged_out:
            held = "tagged out of service" if kept.tagged_out else "held out of service"
            WorkOrderStatusHistory.objects.create(tenant=locked.tenant, work_order=locked, from_status=locked.status, to_status=locked.status,
                                                  changed_by=by, note=f"Device {held}: checks after repair failed")
    wo.refresh_from_db()
    return kept


def _log_hours(wo: WorkOrder, hours, by, today: date, errors: dict) -> LaborLine | None:
    """The hours typed with the completion, as a labor line for costs.default_technician today, by add_labor's rules (in its own
    savepoint: a refusal writes nothing, and its message goes under "hours" in `errors`). Any refusal in the completion takes a line
    logged here back with the rest (complete_work_order's transaction). None when no hours were given or they were refused."""
    if hours is None or not str(hours).strip():
        return None
    try:
        return costs.add_labor(wo, hours=hours, worked_on=today, by=by, today=today)
    except ValidationError as e:
        found = e.message_dict if hasattr(e, "error_dict") else {"hours": e.messages}
        messages = [NO_TECHNICIAN] if "technician" in found else []
        errors["hours"] = " ".join([*messages, *(m for key, ms in found.items() if key != "technician" for m in ms)])
        return None


def _late_reason(wo: WorkOrder, value, by, today: date, errors: dict) -> str:
    """The reason typed with the completion (slice 25), checked before anything is saved so its refusal is reported with the rest
    ("late_reason" in `errors`). "" when none: blank keeps a reason already recorded. _record_late_reason saves it afterwards."""
    reason = str(value if value is not None else "").strip()
    if not reason:
        return ""
    if reason not in LateReason.values:
        errors["late_reason"] = "Choose one of the reasons listed."
    elif wo.type != WoType.PM:
        errors["late_reason"] = "Only a PM records why it was late."
    elif not completes_late(wo, today):
        errors["late_reason"] = done_on_time_message(wo, today)
    elif by is not None and not by.has_level(wo_perms.MODULE, wo_perms.late_reason_level(WoStatus.COMPLETED)):
        errors["late_reason"] = "Recording why a PM was late needs Work orders Edit."
    return reason


def done_on_time_message(wo: WorkOrder, today: date, w=None) -> str:
    """Why a PM completed on `today` takes no reason for lateness: done by its due date, or (slice 27) after it but inside its on-time
    window, whose last day it names."""
    end = inside_window(wo, today, w)
    if end is None:
        return f"{wo.number} is done by its due date, so it has no reason to record."
    return f"{wo.number} is done inside its on-time window (until {end:%b} {end.day}, {end.year}), so it has no reason to record."


def _record_late_reason(wo: WorkOrder, reason: str, by, today: date) -> None:
    """Record the reason on the PM just completed, through its one writer (services.set_late_reason). A refusal there (a rule
    _late_reason does not foresee) is keyed late_reason and takes the completion back with it (complete_work_order's transaction)."""
    try:
        services.set_late_reason(wo, reason, by=by, today=today)
    except ValidationError as e:
        found = e.message_dict if hasattr(e, "error_dict") else {"late_reason": e.messages}
        raise ValidationError({"late_reason": " ".join(m for ms in found.values() for m in ms)}) from e


def _check_tenant(wo) -> None:
    tenant = get_current_tenant()
    if tenant is None or wo is None or wo.tenant_id != tenant.id:
        raise ValidationError("Choose a work order from this facility.")


# --- a failed PM's repair ------------------------------------------------------------------------------------------------------

def follow_up_priority(device_model) -> str:
    return Priority.HIGH if device_model.risk_class in (RiskClass.LIFE_SUPPORT, RiskClass.HIGH) else Priority.NORMAL


def vendor_repairs(asset, today: date) -> bool:
    """Whether the vendor who did the PM also does the repair: unless the device's live contract leaves repair labor out (preventive
    maintenance only, or parts only). No contract: the manufacturer's field service on time and materials."""
    from apps.contracts.models import Coverage

    contract = asset.contract if asset.contract_id else None
    if contract is None or contract.end_on < today:
        return True
    return contract.coverage not in (Coverage.PM_ONLY, Coverage.PARTS)


def follow_up_assignee(pm: WorkOrder, asset, today: date | None = None) -> tuple[str, object]:
    """Who a failed PM's repair goes to, as (vendor name or "", technician or None): a vendor's PM to the same vendor when they do
    repairs on this device (vendor_repairs), so the repair is in their share of the work orders too (apps.workorders.scoping);
    otherwise the PM's technician while active and credentialed; otherwise nobody, for a CE manager to assign. The completion
    and the modal's hint both read this, so the modal never says one thing and the save do another."""
    today = today or timezone.localdate()
    if pm.vendor_service and pm.vendor_name and vendor_repairs(asset, today):
        return pm.vendor_name, None
    return "", follow_up_technician(pm, asset, today)


def follow_up_technician(pm: WorkOrder, asset, today: date | None = None):
    """The in-house technician a failed PM's repair goes to (follow_up_assignee): the PM's technician while active and credentialed
    for the device, else None."""
    tech = pm.assigned_to if pm.assigned_to_id and not pm.vendor_service else None
    if tech is None or not tech.is_active or not qualification(tech, asset, today or timezone.localdate()).ok:
        return None
    return tech


def _requester(by, pm: WorkOrder) -> str:
    name = str(by) if by is not None else ""  # the full name, else the email: never a second facility's username (slice 22)
    if not name and pm.assigned_to_id:
        name = pm.assigned_to.name
    return (name or "CE shop")[:120]


def _problem(pm: WorkOrder, snapshot: list[dict], resolution: str) -> str:
    lines = _failed_lines(snapshot)
    if not lines:
        return f"PM {pm.number} failed: {resolution}"
    text = f"PM {pm.number} failed. Failed {'step' if len(lines) == 1 else 'steps'}:\n" + "\n".join(lines)
    return text + (f"\n{resolution}" if resolution else "")


# A failed PM holds the device out of service until its repair is done: tagged out when it is in service, and kept out when it is
# already out (another tagged-out repair, or in repair), so finishing that other repair cannot put it back in use
# (services._on_completed returns a device only when no other open repair holds it). On loan, missing, or retired: nothing to hold.
HOLDABLE = (AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE, AssetStatus.IN_REPAIR)


def _tag_out_on(repair: WorkOrder, asset, pm: WorkOrder, by) -> None:
    """Hold the device out on a repair already open: that repair is now why it is out (completing it returns the device, as for a
    repair opened with the tag-out). A device in service moves through equipment's status rules; one already out stays out."""
    from apps.equipment.services import set_status  # equipment.services imports this app inside its functions; keep it one way

    repair.tagged_out = True
    repair.save(update_fields=["tagged_out", "updated_at"])
    held = "tagged out of service" if asset.status == AssetStatus.IN_SERVICE else "held out of service"
    WorkOrderStatusHistory.objects.create(tenant=repair.tenant, work_order=repair, from_status=repair.status, to_status=repair.status, changed_by=by,
                                          note=f"Device {held}: PM {pm.number} failed")
    if asset.status == AssetStatus.IN_SERVICE:
        set_status(asset, AssetStatus.OUT_OF_SERVICE, by=by, note=f"Tagged out: PM {pm.number} failed")
        asset.__dict__.pop("_change_reason", None)  # the PM's own save of the device's dates comes next; it is not the tag-out


def _record_failure(pm: WorkOrder, asset, snapshot, resolution: str, *, open_repair: bool, tag_out: bool, by, today: date) -> Completion:
    """Open the follow-up repair (or record the failure on the repair already open), and hold the device out when asked (HOLDABLE).
    Completion.tagged_out says whether the device was in service and is now out."""
    hold = tag_out and asset.status in HOLDABLE
    newly_out = hold and asset.status == AssetStatus.IN_SERVICE
    # The PM's own open repair whoever completes it (never a second one for one PM, even when that repair is outside the user's share:
    # only its number is kept from them); another repair to record on only from what the user may see.
    existing = own_open_repair(pm) or (None if open_repair else other_open_repair(pm, by))
    if existing is not None:
        failed = "; ".join(_failed_lines(snapshot)) or resolution
        services.add_note(existing, f"PM {pm.number} failed on {today:%b} {today.day}, {today.year}: {failed}"[:services.NOTE_MAX_LENGTH], by=by)
        if existing.follow_up_of_id is None:  # the PM and the repair its failure is on name each other (drawer, print, PM history)
            existing.follow_up_of = pm
            existing.save(update_fields=["follow_up_of", "updated_at"])
        if hold:
            _tag_out_on(existing, asset, pm, by)
        return Completion(work_order=pm, repair=existing, tagged_out=newly_out)
    repair = services.create_work_order(asset=asset, type=WoType.REPAIR, priority=follow_up_priority(asset.device_model),
                                        problem=_problem(pm, snapshot, resolution), requester=_requester(by, pm), opened_on=today, created_by=by,
                                        tag_out=hold, follow_up_of=pm)
    vendor, tech = follow_up_assignee(pm, asset, today)
    if vendor:
        services.assign(repair, vendor_name=vendor, by=by)
    elif tech is not None:
        services.assign(repair, technician=tech, by=by)
    return Completion(work_order=pm, follow_up=repair, repair=repair, tagged_out=newly_out)


# --- a failed incoming inspection's re-inspection (slice 26) ---------------------------------------------------------------------

def open_reinspection(wo: WorkOrder) -> WorkOrder | None:
    """The device's open incoming inspection other than `wo`, which a failure of `wo` is recorded on instead of opening another: the
    re-inspection `wo` opened at an earlier failure first, else the earliest open. Whoever completes it: a device waiting for its
    inspection never has two open (a scoped user is told about it without its number, by the screens)."""
    others = inspections.incoming(wo.asset).filter(status__in=OPEN_STATUSES).exclude(pk=wo.pk)
    return others.filter(follow_up_of=wo).order_by("opened_on", "number").first() or others.order_by("opened_on", "number").first()


def reinspection_assignee(failed: WorkOrder) -> tuple[str, object]:
    """Who a failed incoming inspection's re-inspection goes to, as (vendor name or "", technician or None): the same vendor for
    vendor service (the vendor's acceptance test is redone by them), else the same technician while active, else nobody (a CE
    manager assigns it). Assigned through services.assign, which notes a credential override as for any assignment. The completion
    and the modal's hint both read this."""
    if failed.vendor_service and failed.vendor_name:
        return failed.vendor_name, None
    tech = failed.assigned_to if failed.assigned_to_id else None
    return "", tech if tech is not None and tech.is_active else None


def _reinspection_problem(failed: WorkOrder, snapshot: list[dict], resolution: str) -> str:
    lines = _failed_lines(snapshot)
    text = f"Re-inspection: incoming inspection {failed.number} failed."
    if lines:
        text += f" Failed {'step' if len(lines) == 1 else 'steps'}:\n" + "\n".join(lines)
    return text + (f"\n{resolution}" if resolution else "")


# Where a device waiting for its incoming inspection may be in use (use_before_inspection puts it in service): a failed inspection
# takes it out (tag_out, default on).
IN_USE = (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN)


def _reinspect(failed: WorkOrder, asset, snapshot, resolution: str, *, tag_out: bool, by, today: date) -> Completion:
    """A failed incoming inspection of a device that waits for it: record the failure on the device's open incoming inspection
    (open_reinspection) or open the re-inspection (inspections.open_for: follow_up_of the failed one, due REINSPECTION_DUE_DAYS later,
    reinspection_assignee); then take the device out of use when it was put in use before its inspection (and `tag_out`). In that
    order: a new work order's number before the device's row, as a tagged-out request takes them, so the two never wait in a circle."""
    from apps.equipment.services import set_status  # equipment.services imports this app inside its functions; keep it one way

    existing = open_reinspection(failed)
    if existing is not None:
        what = "; ".join(_failed_lines(snapshot)) or resolution
        services.add_note(existing, f"Incoming inspection {failed.number} failed on {today:%b} {today.day}, {today.year}: {what}"[:services.NOTE_MAX_LENGTH],
                          by=by)
        if existing.follow_up_of_id is None:  # the failed inspection and the one re-inspecting name each other (drawer, print)
            existing.follow_up_of = failed
            existing.save(update_fields=["follow_up_of", "updated_at"])
        done = Completion(work_order=failed, reinspection=existing)
    else:
        reinspection = inspections.open_for(asset, by=by, today=today, due_on=today + timedelta(days=inspections.REINSPECTION_DUE_DAYS),
                                            problem=_reinspection_problem(failed, snapshot, resolution), follow_up_of=failed)
        vendor, tech = reinspection_assignee(failed)
        if vendor:
            services.assign(reinspection, vendor_name=vendor, by=by)
        elif tech is not None:
            services.assign(reinspection, technician=tech, by=by)
        done = Completion(work_order=failed, reinspection=reinspection, reinspection_opened=True)
    if tag_out and asset.status in IN_USE:
        set_status(asset, AssetStatus.OUT_OF_SERVICE, by=by, note=f"Tagged out: incoming inspection {failed.number} failed")
        asset.__dict__.pop("_change_reason", None)
        done.tagged_out = True
    return done


def _default_resolution(pm_result: str, procedure, snapshot: list[dict], done: Completion) -> str:
    if pm_result == PmResult.PASS:
        return f"PM completed per {procedure.code}, all checks passed" if procedure is not None else "PM completed, all checks passed"
    if pm_result == PmResult.FAIL and done.repair is not None:
        where = f"repair {done.repair.number} opened" if done.follow_up else f"recorded on repair {done.repair.number}"
        return f"PM failed on {_numbers(_failed(snapshot))}; {where}."
    return ""


def _default_inspection_resolution(result: str, procedure, snapshot: list[dict]) -> str:
    """A pass with every step recorded (slice 26); anything else says its own (_check_inspection asks for it)."""
    if result != InspectionResult.PASSED or not snapshot:
        return ""
    per = "the incoming checklist" if procedure is None or inspections.is_incoming_checklist(procedure) or not procedure.code else procedure.code
    return f"Incoming inspection passed per {per}, all checks passed"


def result_note(result: str, done: Completion) -> str:
    """The status history's note, and the toast's wording: "PM passed", "PM failed; WO-26-0057 opened for the repair". Slice 26, an
    incoming inspection's (`result` is its InspectionResult): "Incoming inspection passed", "Incoming inspection failed; WO-26-0431
    opened to re-inspect" (or "; re-inspection WO-26-0431 already open")."""
    note = RESULT_NOTES.get(result, "")
    if result == PmResult.FAIL and done.repair is not None:
        note += f"; {done.repair.number} opened for the repair" if done.follow_up else f"{RECORDED_ON}{done.repair.number}"
    elif result == InspectionResult.FAILED and done.reinspection is not None:
        number = done.reinspection.number
        note += f"; {number} opened to re-inspect" if done.reinspection_opened else f"; re-inspection {number} already open"
    return note


# --- completing ----------------------------------------------------------------------------------------------------------------

@transaction.atomic
def complete_work_order(wo: WorkOrder, *, resolution: str = "", pm_result: str = "", inspection_result: str = "", repair_check: str = "",
                        checks_reference=vendor_services.KEEP, results=None, open_repair: bool = True, tag_out: bool | None = None,
                        signature: str | None = None, hours=None, late_reason=None, vendor_engineer=vendor_services.KEEP,
                        engineer_name: str = "", repair_finding=vendor_services.KEEP, test_equipment=vendor_services.KEEP, by=None,
                        today: date | None = None) -> Completion:
    """Complete `wo` with its resolution and, for a PM, its result and checklist results (the module's rules); a PM still open is
    started first (starts_on_completion). `results` is one {"result": "pass" | "fail" | "na", "reading": "..."} per checklist step, in
    order. `hours` (None or blank: none) are logged with it, and `late_reason` (None or blank: none; slice 25) is recorded on a PM
    completed after its due date. Slice 26: an incoming inspection takes `inspection_result` (InspectionResult) and, optionally, its
    checklist's `results`; `tag_out` also applies to its fail. Slice 30: a repair takes `repair_check` (RepairCheck), the post-repair
    checklist's `results` for passed, and `checks_reference` for vendor_report. Slice 31: vendor service takes `vendor_engineer` or
    `engineer_name` and `checks_reference` (the module's Slice 31 paragraph; left out, the number on file stays). Slice 33: a repair
    takes `repair_finding` (RepairFinding; KEEP, None, or blank keeps the one stored, required when none is: the module's Slice 33
    paragraph). Slice 35: `test_equipment` (KEEP, or a list of item ids: the module's Slice 35 paragraph). Raises ValidationError: a
    plain message when the work order cannot be completed now, else a dict keyed by resolution, pm_result, inspection_result,
    repair_check, checks_reference, checklist, step_<n>, reading_<n>, open_repair, hours, late_reason, vendor_engineer, engineer_name,
    repair_finding, and test_equipment. `wo` is refreshed from the database afterwards (and its device with it)."""
    today = today or timezone.localdate()
    _check_tenant(wo)
    # The device's open work orders and this one first, in number order (its open inspections among them: two of its inspections
    # completed at once take turns, and the second finds itself cancelled by the first's pass), then the device's row (slice 28 review
    # fix: locking this one alone first waited on a lower number a start held while it waited for this one: services.lock_work).
    services.lock_work(wo.asset_id, wo.pk)
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)  # two clicks, or two people, complete it once
    reason = blocker(locked, today, by)
    if reason:
        raise ValidationError(reason)
    asset = locked.asset
    before = (asset.status, asset.last_pm_on, asset.next_pm_on, asset.awaiting_inspection)
    is_pm, is_inspection, is_repair = locked.type == WoType.PM, locked.type == WoType.INSPECTION, locked.type == WoType.REPAIR
    procedure = procedure_for(locked)
    steps = checklist_of(procedure)
    pm_result = str(pm_result or "").strip()
    inspection_result = str(inspection_result or "").strip()
    repair_check = str(repair_check or "").strip()

    if checks_reference is vendor_services.KEEP:  # slice 31 merge fix: a completion that sends none keeps the number on file
        checks_reference = locked.checks_reference if locked.vendor_service else ""
    errors: dict[str, str] = {}
    text = _resolution(resolution, errors)
    snapshot: list[dict] = []
    reference = ""
    if is_repair:  # slice 30: the checks after it, by the outcome chosen
        snapshot = _check_repair(locked, repair_check, steps, results, signature, by, errors)
        reference = _reference(checks_reference, errors, vendor_service=locked.vendor_service, repair_check=repair_check)
    else:
        if repair_check:
            errors["repair_check"] = REPAIR_CHECK_ONLY
        reference = _reference(checks_reference, errors, vendor_service=locked.vendor_service)  # slice 31: any vendor job's report
        if not (is_pm or is_inspection):
            if results:
                errors["pm_result"] = REPAIR_RESULTS_ONLY
        elif signature is not None and signature != checklist_signature(steps):
            errors["checklist"] = REVISED
        else:
            snapshot = _steps(steps, results, errors, optional=is_inspection)
    # Slice 26: the inspection whose pass ended the device's wait stays passed when it is completed again (reopened); only that one
    # (inspections.pass_cleared_flag; review fix: a pass that changed nothing may be corrected). A failed step or Failed then gets the
    # one answer: the device is in use, so a failure now is a repair's.
    kept_pass = is_inspection and inspections.pass_cleared_flag(locked)
    if kept_pass and (inspection_result == InspectionResult.FAILED or _failed(snapshot)):
        errors["inspection_result"] = (f"{locked.number} passed and {asset.tag} no longer waits for its incoming inspection, so it stays "
                                       "passed. If the device has failed since, tag the device out and open a repair.")
    elif kept_pass and not inspection_result:
        inspection_result = InspectionResult.PASSED
    _check_result(locked, pm_result, snapshot, bool(steps), text, errors, inspection_result=inspection_result,
                  result_required=asset.awaiting_inspection)
    if is_inspection and inspection_result == InspectionResult.PASSED and not kept_pass and "inspection_result" not in errors:
        owner_pm = inspections.owner_pm_refusal(asset, today)  # slice 29: a temporary device whose owner's PM date is past
        if owner_pm:
            errors["inspection_result"] = owner_pm
    fail = is_pm and pm_result == PmResult.FAIL
    if fail and not open_repair and own_open_repair(locked) is None and other_open_repair(locked, by) is None:
        errors["open_repair"] = f"{asset.tag} has no open repair work order to record the failure on, so a failed PM opens one."
    reinspect = is_inspection and inspection_result == InspectionResult.FAILED and asset.awaiting_inspection
    late = _late_reason(locked, late_reason, by, today, errors)
    engineer = vendor_services.engineer_choice(locked, vendor_engineer, engineer_name, by=by, errors=errors)  # slice 31
    finding = _repair_finding(locked, repair_finding, by, errors)  # slice 33
    # Slice 35: the items' rows locked here, after lock_work and this row, before the repair's number and the device's row
    uses = test_equipment_services.check_uses(locked, test_equipment, by=by, today=today, errors=errors, completing=True,
                                              repair_check=repair_check if is_repair else None)
    if errors:
        _log_hours(locked, hours, by, today, errors)  # its refusal too, so every problem is reported at once (a line goes back with the rest)
        raise ValidationError(errors)

    started = locked.status == WoStatus.OPEN  # blocker let it through: a PM done in one visit, started as it is completed
    done = Completion(work_order=locked)
    # Slice 28: a failed PM's repair, or a failed inspection's re-inspection, first: its number (the facility's numbering) before the
    # device's row, which the start and the completion lock to read the device's hold (services.change_status) and a tag-out takes,
    # the order a tagged-out request takes them in, so the two never wait in a circle. Only another work order is written here: the
    # PM's own timeline still reads start, hours, completion.
    if fail:
        done = _record_failure(locked, asset, snapshot, text, open_repair=open_repair, tag_out=tag_out is None or bool(tag_out), by=by, today=today)
    elif reinspect:
        done = _reinspect(locked, asset, snapshot, text, tag_out=tag_out is None or bool(tag_out), by=by, today=today)
    if started:
        services.change_status(locked, WoStatus.IN_PROGRESS, by=by, as_of=today)
    labor = _log_hours(locked, hours, by, today, errors)  # after the start: the timeline reads in the order the work was done
    if errors:
        raise ValidationError(errors)  # the start, and a repair or re-inspection opened, go back with it
    done.started, done.labor = started, labor
    locked.checks_reference = reference  # slice 31: any vendor job's report number ("" on in-house work)
    if engineer is not vendor_services.KEEP:
        locked.vendor_engineer = vendor_services.resolve_choice(engineer, locked, by=by)  # saved with the completion
    note = ""
    if is_pm:
        locked.pm_result = pm_result
        locked.checklist_results = snapshot
        text = text or _default_resolution(pm_result, procedure, snapshot, done)
        note = result_note(pm_result, done)
    elif is_inspection:
        locked.inspection_result = inspection_result
        locked.checklist_results = snapshot
        text = text or _default_inspection_resolution(inspection_result, procedure, snapshot)
        note = result_note(inspection_result, done)
    elif is_repair:  # slice 30: saved with the completion (change_status saves the whole row)
        locked.repair_check, locked.checklist_results = repair_check, snapshot
        if finding is not vendor_services.KEEP:  # slice 33: what it found, saved with the completion
            locked.repair_finding = finding
        if repair_check == RepairCheck.NOT_RETURNED:
            # The repair holds the device out (marked tagged out) and services._on_completed never returns it; in use or in repair, it
            # moves out of service now, through equipment's status rules (the device's open work is locked already: lock_work).
            done.tagged_out = _hold_out(locked, asset, by, f"Not returned to use after repair {locked.number}", save=False)
        else:
            done.returned = services.returns_device(locked)
        note = repair_note(locked, done)
    locked.resolution = text
    # A passed inspection of a waiting device passes it here (services._on_completed: equipment.services.pass_incoming_inspection).
    services.change_status(locked, WoStatus.COMPLETED, by=by, note=note, as_of=today)
    if uses is not vendor_services.KEEP:
        test_equipment_services.apply_uses(locked, uses, by=by, completing=True)  # slice 35: each use added or taken off
    if late:
        _record_late_reason(locked, late, by, today)  # once completed late, it is a PM that missed its due date (missed_pms)
    asset.refresh_from_db(fields=["status", "last_pm_on", "next_pm_on", "awaiting_inspection"])
    done.device_changed = (asset.status, asset.last_pm_on, asset.next_pm_on, asset.awaiting_inspection) != before
    done.passed = before[3] and not asset.awaiting_inspection
    wo.refresh_from_db()
    return done
