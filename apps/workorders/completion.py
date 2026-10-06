"""
Completing a work order (slice 15): the resolution, and for a PM the checklist results step by step and the overall result
(PmResult), a failed PM opening a follow-up repair work order. Completes through apps.workorders.services.change_status, so the
device (last and next PM, a tagged-out repair's return to service) and the portal's done email behave as they always have.

The rules (complete_work_order checks them all and reports every problem at once, keyed by the modal's field names):
- Only this facility's work order, only from a status that may move to completed (in progress), only once it is assigned (a
  technician or a vendor did the work), and never on a date before it was opened. `blocker` says which of these stops it now.
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
- One transaction: a refusal anywhere (the repair, the tag-out, the completion) leaves nothing behind.
- The status history says the result: "PM passed", "PM passed with minor repair", "PM failed; WO-26-0057 opened for the repair".
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.credentials.services import qualification
from apps.equipment.models import AssetStatus, RiskClass
from apps.pm.procedures import step_parts
from apps.tenants.context import get_current_tenant

from . import services
from .models import ALLOWED_TRANSITIONS, OPEN_STATUSES, PmResult, Priority, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType

RESOLUTION_MAX = 1000
READING_MAX = 60
PASS, FAIL, NA = "pass", "fail", "na"
STEP_RESULTS = {PASS: "Pass", FAIL: "Fail", NA: "N/A"}
DONE_STATUSES = (WoStatus.COMPLETED, WoStatus.CLOSED)
# The status history's note (and the toast's wording) for each result
RESULT_NOTES = {PmResult.PASS: "PM passed", PmResult.PASS_MINOR_REPAIR: "PM passed with minor repair", PmResult.FAIL: "PM failed"}
_CONTROL = "Remove the invisible control character from {what}."


@dataclass
class Completion:
    work_order: WorkOrder
    follow_up: WorkOrder | None = None  # the repair this completion opened
    repair: WorkOrder | None = None  # the repair a failed PM was recorded on: the new follow-up, or one already open
    tagged_out: bool = False  # the device went out of service with this completion
    device_changed: bool = False  # its status or its last or next PM moved (the Equipment list shows them)


# --- reading -------------------------------------------------------------------------------------------------------------------

def procedure_for(wo: WorkOrder):
    """The PM procedure a PM work order is done to: its device's model's, as it is now. None for other types."""
    return wo.asset.device_model.pm_procedure if wo.type == WoType.PM else None


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


def blocker(wo: WorkOrder, today: date | None = None) -> str:
    """Why `wo` cannot be completed now, or "" when it can."""
    today = today or timezone.localdate()
    if WoStatus.COMPLETED not in ALLOWED_TRANSITIONS[wo.status]:
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


def _steps(steps, results, errors: dict) -> list[dict]:
    """The checklist as recorded: each step with its result and reading. Errors under step_<n> and reading_<n> (from 1)."""
    results = list(results or [])
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


def _check_result(wo, pm_result: str, snapshot: list[dict], has_checklist: bool, resolution: str, errors: dict) -> None:
    if wo.type != WoType.PM:
        if pm_result:
            errors["pm_result"] = "Only a PM records a PM result."
        if not resolution and "resolution" not in errors:
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


def _default_resolution(pm_result: str, procedure, snapshot: list[dict], done: Completion) -> str:
    if pm_result == PmResult.PASS:
        return f"PM completed per {procedure.code}, all checks passed" if procedure is not None else "PM completed, all checks passed"
    if pm_result == PmResult.FAIL and done.repair is not None:
        where = f"repair {done.repair.number} opened" if done.follow_up else f"recorded on repair {done.repair.number}"
        return f"PM failed on {_numbers(_failed(snapshot))}; {where}."
    return ""


def result_note(pm_result: str, done: Completion) -> str:
    """The status history's note, and the toast's wording: "PM passed", "PM failed; WO-26-0057 opened for the repair"."""
    note = RESULT_NOTES.get(pm_result, "")
    if pm_result == PmResult.FAIL and done.repair is not None:
        note += f"; {done.repair.number} opened for the repair" if done.follow_up else f"; recorded on open repair {done.repair.number}"
    return note


# --- completing ----------------------------------------------------------------------------------------------------------------

@transaction.atomic
def complete_work_order(wo: WorkOrder, *, resolution: str = "", pm_result: str = "", results=None, open_repair: bool = True,
                        tag_out: bool | None = None, signature: str | None = None, by=None, today: date | None = None) -> Completion:
    """Complete `wo` with its resolution and, for a PM, its result and checklist results (the module's rules). `results` is one
    {"result": "pass" | "fail" | "na", "reading": "..."} per checklist step, in order. Raises ValidationError: a plain message
    when the work order cannot be completed now, else a dict keyed by resolution, pm_result, checklist, step_<n>, reading_<n>,
    and open_repair. `wo` is refreshed from the database afterwards."""
    today = today or timezone.localdate()
    _check_tenant(wo)
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)  # two clicks, or two people, complete it once
    reason = blocker(locked, today)
    if reason:
        raise ValidationError(reason)
    asset = locked.asset
    before = (asset.status, asset.last_pm_on, asset.next_pm_on)
    is_pm = locked.type == WoType.PM
    procedure = procedure_for(locked)
    steps = checklist_of(procedure)
    pm_result = str(pm_result or "").strip()

    errors: dict[str, str] = {}
    text = _resolution(resolution, errors)
    snapshot: list[dict] = []
    if not is_pm:
        if results:
            errors["pm_result"] = "Only a PM records checklist results."
    elif signature is not None and signature != checklist_signature(steps):
        errors["checklist"] = "The procedure's checklist was revised while this was open. Check the steps again."
    else:
        snapshot = _steps(steps, results, errors)
    _check_result(locked, pm_result, snapshot, bool(steps), text, errors)
    fail = is_pm and pm_result == PmResult.FAIL
    if fail and not open_repair and own_open_repair(locked) is None and other_open_repair(locked, by) is None:
        errors["open_repair"] = f"{asset.tag} has no open repair work order to record the failure on, so a failed PM opens one."
    if errors:
        raise ValidationError(errors)

    done = Completion(work_order=locked)
    if fail:
        done = _record_failure(locked, asset, snapshot, text, open_repair=open_repair, tag_out=tag_out is None or bool(tag_out), by=by, today=today)
    if is_pm:
        locked.pm_result = pm_result
        locked.checklist_results = snapshot
        text = text or _default_resolution(pm_result, procedure, snapshot, done)
    locked.resolution = text
    services.change_status(locked, WoStatus.COMPLETED, by=by, note=result_note(pm_result, done) if is_pm else "", as_of=today)
    asset.refresh_from_db(fields=["status", "last_pm_on", "next_pm_on"])
    done.device_changed = (asset.status, asset.last_pm_on, asset.next_pm_on) != before
    wo.refresh_from_db()
    return done
