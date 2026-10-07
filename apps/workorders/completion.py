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
  recorded. Only a PM completed after its due date (completes_late) takes one; anything else, or a value not listed, is refused under
  late_reason with the rest, and nothing is saved. Never required: an API client or a hurried technician still completes.
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
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.credentials.services import qualification
from apps.equipment.models import AssetStatus, RiskClass
from apps.pm.procedures import step_parts
from apps.tenants.context import get_current_tenant

from . import costs, inspections, services
from . import permissions as wo_perms
from .models import (
    ALLOWED_TRANSITIONS,
    OPEN_STATUSES,
    InspectionResult,
    LaborLine,
    LateReason,
    PmResult,
    Priority,
    WorkOrder,
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


# --- reading -------------------------------------------------------------------------------------------------------------------

def procedure_for(wo: WorkOrder):
    """The procedure a PM work order is done to: its device's model's PM procedure, as it is now (or None). Slice 26: an incoming
    inspection is done to the same procedure when it has steps, else to the incoming checklist (inspections.INCOMING, a stand-in in
    the procedure's shape with a blank code), so checklist_of(procedure_for(wo)) is an inspection's checklist as it is a PM's. None
    for other types."""
    if wo.type == WoType.PM:
        return wo.asset.device_model.pm_procedure
    if wo.type == WoType.INSPECTION:
        procedure = wo.asset.device_model.pm_procedure
        return procedure if checklist_of(procedure) else inspections.INCOMING
    return None


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


def completes_late(wo: WorkOrder, today: date) -> bool:
    """Whether completing `wo` on `today` finishes a PM after its due date (slice 25): it is then one of the PMs that missed their due
    date (apps.pm.services.missed_pms) and may record why (LateReason). The modal offers "Why was it late?" on these only."""
    return wo.type == WoType.PM and wo.due_on is not None and today > wo.due_on


def blocker(wo: WorkOrder, today: date | None = None, by=None) -> str:
    """Why `wo` cannot be completed now by `by` (None: anyone allowed), or "" when it can."""
    today = today or timezone.localdate()
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
        errors["checklist"] = "Every step is marked N/A. Mark the steps that were done as pass or fail."
    return out


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
        errors["late_reason"] = f"{wo.number} is done by its due date, so it has no reason to record."
    elif by is not None and not by.has_level(wo_perms.MODULE, wo_perms.late_reason_level(WoStatus.COMPLETED)):
        errors["late_reason"] = "Recording why a PM was late needs Work orders Edit."
    return reason


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
def complete_work_order(wo: WorkOrder, *, resolution: str = "", pm_result: str = "", inspection_result: str = "", results=None,
                        open_repair: bool = True, tag_out: bool | None = None, signature: str | None = None, hours=None, late_reason=None,
                        by=None, today: date | None = None) -> Completion:
    """Complete `wo` with its resolution and, for a PM, its result and checklist results (the module's rules); a PM still open is
    started first (starts_on_completion). `results` is one {"result": "pass" | "fail" | "na", "reading": "..."} per checklist step, in
    order. `hours` (None or blank: none) are logged with it, and `late_reason` (None or blank: none; slice 25) is recorded on a PM
    completed after its due date. Slice 26: an incoming inspection takes `inspection_result` (InspectionResult) and, optionally, its
    checklist's `results`; `tag_out` also applies to its fail. Raises ValidationError: a plain message when the work order cannot be
    completed now, else a dict keyed by resolution, pm_result, inspection_result, checklist, step_<n>, reading_<n>, open_repair, hours,
    and late_reason. `wo` is refreshed from the database afterwards (and its device with it)."""
    today = today or timezone.localdate()
    _check_tenant(wo)
    if wo.type == WoType.INSPECTION:
        # Slice 26: the device's open inspections first, in number order, then this one, then the device's row in the pass: two of its
        # inspections completed at once take turns; the second finds itself cancelled by the first's pass (or the device passed).
        inspections.lock_open(wo.asset_id)
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)  # two clicks, or two people, complete it once
    reason = blocker(locked, today, by)
    if reason:
        raise ValidationError(reason)
    asset = locked.asset
    before = (asset.status, asset.last_pm_on, asset.next_pm_on, asset.awaiting_inspection)
    is_pm, is_inspection = locked.type == WoType.PM, locked.type == WoType.INSPECTION
    procedure = procedure_for(locked)
    steps = checklist_of(procedure)
    pm_result = str(pm_result or "").strip()
    inspection_result = str(inspection_result or "").strip()

    errors: dict[str, str] = {}
    text = _resolution(resolution, errors)
    snapshot: list[dict] = []
    if not (is_pm or is_inspection):
        if results:
            errors["pm_result"] = "Only a PM or an incoming inspection records checklist results."
    elif signature is not None and signature != checklist_signature(steps):
        errors["checklist"] = "The procedure's checklist was revised while this was open. Check the steps again."
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
    fail = is_pm and pm_result == PmResult.FAIL
    if fail and not open_repair and own_open_repair(locked) is None and other_open_repair(locked, by) is None:
        errors["open_repair"] = f"{asset.tag} has no open repair work order to record the failure on, so a failed PM opens one."
    reinspect = is_inspection and inspection_result == InspectionResult.FAILED and asset.awaiting_inspection
    late = _late_reason(locked, late_reason, by, today, errors)
    if errors:
        _log_hours(locked, hours, by, today, errors)  # its refusal too, so every problem is reported at once (a line goes back with the rest)
        raise ValidationError(errors)

    started = locked.status == WoStatus.OPEN  # blocker let it through: a PM done in one visit, started as it is completed
    if started:
        services.change_status(locked, WoStatus.IN_PROGRESS, by=by, as_of=today)
    labor = _log_hours(locked, hours, by, today, errors)  # after the start: the timeline reads in the order the work was done
    if errors:
        raise ValidationError(errors)  # the start goes back with it
    done = Completion(work_order=locked)
    if fail:
        done = _record_failure(locked, asset, snapshot, text, open_repair=open_repair, tag_out=tag_out is None or bool(tag_out), by=by, today=today)
    elif reinspect:
        done = _reinspect(locked, asset, snapshot, text, tag_out=tag_out is None or bool(tag_out), by=by, today=today)
    done.started, done.labor = started, labor
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
    locked.resolution = text
    # A passed inspection of a waiting device passes it here (services._on_completed: equipment.services.pass_incoming_inspection).
    services.change_status(locked, WoStatus.COMPLETED, by=by, note=note, as_of=today)
    if late:
        _record_late_reason(locked, late, by, today)  # once completed late, it is a PM that missed its due date (missed_pms)
    asset.refresh_from_db(fields=["status", "last_pm_on", "next_pm_on", "awaiting_inspection"])
    done.device_changed = (asset.status, asset.last_pm_on, asset.next_pm_on, asset.awaiting_inspection) != before
    done.passed = before[3] and not asset.awaiting_inspection
    wo.refresh_from_db()
    return done
