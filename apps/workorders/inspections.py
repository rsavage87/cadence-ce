"""
Incoming inspections (slice 26): a new device is inspected before its first clinical use. Add device (equipment.services.create_asset
with incoming_inspection="waiting") adds it out of service with Asset.awaiting_inspection set, no next PM, and an Incoming inspection
work order open; completing that work order records InspectionResult. A pass (equipment.services.pass_incoming_inspection) clears the
flag, starts the device's PM clock on the day it passed, and puts it in service unless an open tagged-out repair still holds it; a fail
keeps it out with a re-inspection open. Nothing else puts a device awaiting inspection in service except use_before_inspection
(Equipment Approve, a reason from UseBeforeInspection), which leaves the flag set until the inspection passes.

This module holds the read side every door shares (the drawer's banner, set_status's refusals, the API, the survey binder) and the
one opener of an incoming inspection work order. The rules that change a device or a work order live in the services they belong to
(equipment.services, workorders.services, workorders.completion).

The read helpers:
- state(asset): the device's open, first passed, and last failed inspection (one query).
- open_inspection(asset): its open incoming inspection, the earliest opened.
- banner(asset, user): what the device drawer says about it, for one user (numbers outside a scoped user's share masked).
- uses_before(asset_ids): when each device was put in use before its inspection, and why (read from the device's history, where
  use_before_inspection records it), for the banner and the survey binder.
- waiting_for_inspector(): open incoming inspections on waiting devices that nobody has (the Work orders page's note for managers).
- INCOMING: the incoming checklist as a procedure, what completion.procedure_for gives an inspection whose model's PM procedure has
  no steps (or that has no procedure).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.utils import timezone

from .models import OPEN_STATUSES, InspectionResult, Priority, WorkOrder, WoStatus, WoType

INSPECTION_DUE_DAYS = 5  # an incoming inspection is due this many days after it is opened, unless Add device says otherwise
REINSPECTION_DUE_DAYS = 14  # after a fail: the vendor's swap or fix usually takes longer than the first inspection's window
INCOMING_PROBLEM = "Incoming inspection before first clinical use"
# The checklist of an incoming inspection when the device's model has no PM procedure (completion.checklist_of's shape: text and what
# to record, or None). With a procedure, the inspection uses the procedure's checklist, as a PM does: the full PM is the core of it.
INCOMING_CHECKLIST = [
    ("Received complete and undamaged, with accessories and manuals", None),
    ("Electrical safety test per IEC 62353 (N/A for battery-only or non-electrical devices)", "Record leakage µA"),
    ("Functional check per the manufacturer's instructions", None),
    ("Checked for open recalls and software updates", None),
    ("Tagged and labeled", None),
]
DONE_STATUSES = (WoStatus.COMPLETED, WoStatus.CLOSED)
# The device's history reason use_before_inspection writes (equipment.services); uses_before reads it back.
USE_BEFORE_PREFIX = "In use before its incoming inspection: "


@dataclass(frozen=True, eq=False)
class IncomingChecklist:
    """The incoming checklist in a PM procedure's shape (code, name, checklist), so completion.checklist_of, the signature, and the
    modal read it as they read a procedure. `code` is blank: it is no procedure on file, and the default resolution says "the incoming
    checklist" instead (completion._default_resolution). Never saved anywhere: a completed inspection keeps its steps as recorded
    (WorkOrder.checklist_results), as a PM does."""
    code: str = ""
    name: str = "Incoming inspection checklist"
    revision: str = ""
    checklist: tuple = tuple({"text": text, "measure": measure} for text, measure in INCOMING_CHECKLIST)
    is_incoming: bool = True


INCOMING = IncomingChecklist()


def is_incoming_checklist(procedure) -> bool:
    return isinstance(procedure, IncomingChecklist)


@dataclass
class InspectionState:
    """Where a device stands with its incoming inspection."""
    awaiting: bool  # Asset.awaiting_inspection
    open: WorkOrder | None = None  # its open incoming inspection (the earliest opened)
    passed: WorkOrder | None = None  # the first completed inspection with result passed
    failed: WorkOrder | None = None  # the last completed inspection with result failed


def incoming(asset):
    """The device's incoming inspection work orders (type inspection), not cancelled."""
    return WorkOrder.objects.filter(asset=asset, type=WoType.INSPECTION).exclude(status=WoStatus.CANCELLED)


def open_inspection(asset) -> WorkOrder | None:
    return incoming(asset).filter(status__in=OPEN_STATUSES).order_by("opened_on", "number").first()


def state(asset) -> InspectionState:
    """One query: the device's inspections, read in order. Only a completed (or closed) inspection counts as passed or failed: one
    reopened keeps the result it had until it is completed again, and is open meanwhile."""
    s = InspectionState(awaiting=bool(asset.awaiting_inspection))
    for wo in incoming(asset).order_by("opened_on", "number"):
        if wo.status in OPEN_STATUSES:
            s.open = s.open or wo
        elif wo.status in DONE_STATUSES and wo.inspection_result == InspectionResult.PASSED and s.passed is None:
            s.passed = wo
        elif wo.status in DONE_STATUSES and wo.inspection_result == InspectionResult.FAILED:
            s.failed = wo
    return s


def passed_reason(wo) -> str:
    """The device history's reason when `wo`'s pass ends its wait (equipment.services.pass_incoming_inspection writes it)."""
    return f"Passed incoming inspection {wo.number}"


def pass_cleared_flag(wo) -> bool:
    """Whether `wo` is the incoming inspection whose pass ended its device's wait (review fix): only that one stays passed when it is
    reopened and completed again (completion's kept pass). A passed inspection of a device that never waited, or one that passed
    after another had already ended the wait, records a result that changed nothing, and may be corrected. One query on the
    device's history (matched by its id: this facility's row)."""
    from apps.equipment.models import Asset

    if wo.type != WoType.INSPECTION or wo.inspection_result != InspectionResult.PASSED or wo.asset.awaiting_inspection:
        return False
    return Asset.history.filter(id=wo.asset_id, history_change_reason=passed_reason(wo)).exists()


def lock_open(asset_id) -> list:
    """Lock the device's open incoming inspections, in number order, until the transaction ends; returns their ids. The first lock
    the writers of a waiting device's inspections take (completion.complete_work_order on an inspection, equipment.services
    .use_before_inspection), before the device's row (pass_incoming_inspection, use_before_inspection): two at once then take turns on
    the first inspection, never each holding one inspection while waiting for the device's row held by the other (a re-inspection
    completed in one visit checks its follow_up_of, the failed inspection's row, when it commits). SQLite runs one writer at a time."""
    qs = WorkOrder.objects.select_for_update().filter(asset_id=asset_id, type=WoType.INSPECTION, status__in=OPEN_STATUSES).order_by("number")
    return [wo.pk for wo in qs.only("id")]


def waiting_for_inspector():
    """Open incoming inspections of devices still waiting for them that nobody has (no technician, not vendor service): what the Work
    orders page tells a manager (Work orders Approve) is waiting for a technician."""
    return WorkOrder.objects.filter(type=WoType.INSPECTION, status__in=OPEN_STATUSES, assigned_to__isnull=True, vendor_service=False,
                                    asset__awaiting_inspection=True)


def open_for(asset, *, by=None, today: date | None = None, opened_on: date | None = None, due_on: date | None = None,
             problem: str = INCOMING_PROBLEM, follow_up_of: WorkOrder | None = None, priority: str = Priority.NORMAL) -> WorkOrder:
    """Open an incoming inspection work order on `asset`, unassigned (the caller assigns it through services.take or assign, which
    write the history and the email). Opened on `opened_on` (Add device's added day) or today, due INSPECTION_DUE_DAYS later unless
    `due_on` says. Estimated hours: the model's PM procedure's, else 1. Goes through services.create_work_order, so a device waiting
    for its inspection never gets a second one open (other than the failed inspection a re-inspection follows: `follow_up_of`)."""
    from . import services  # services imports this module

    today = today or timezone.localdate()
    opened_on = opened_on or today
    procedure = asset.device_model.pm_procedure
    hours = procedure.estimated_hours if procedure is not None else 1
    return services.create_work_order(asset=asset, type=WoType.INSPECTION, priority=priority, problem=problem, opened_on=opened_on,
                                      due_on=due_on or opened_on + timedelta(days=INSPECTION_DUE_DAYS), created_by=by,
                                      estimated_hours=hours, follow_up_of=follow_up_of)


# --- in use before its inspection -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class UseBefore:
    """A device put in use before its incoming inspection (equipment.services.use_before_inspection), as its history records it."""
    on: date  # the day it went into use (the facility's)
    reason: str  # the UseBeforeInspection slug ("" for a label no longer listed)
    label: str  # the reason as it was recorded
    by: object = None  # the user who approved it, when known


def use_before_reason(label: str) -> str:
    """The device history's reason for it (at most 100 characters, as every change reason)."""
    return f"{USE_BEFORE_PREFIX}{label}"[:100]


def uses_before(asset_ids) -> dict:
    """{device id: [UseBefore, oldest first]} for the devices among `asset_ids` ever put in use before their incoming inspection. One
    query on the devices' history, in the current facility (the historical tables' managers are not tenant-scoped)."""
    from apps.equipment.models import Asset, AssetStatus, UseBeforeInspection
    from apps.tenants.context import get_current_tenant

    tenant = get_current_tenant()
    ids = list(asset_ids)
    if tenant is None or not ids:
        return {}
    slugs = {label: slug for slug, label in UseBeforeInspection.choices}
    out: dict = {}
    # Only use_before_inspection's own rows: the device in service while still awaiting its inspection, with one of the listed reasons
    # word for word (review fix: a note typed with a status change could start with the same words).
    rows = (Asset.history.filter(tenant_id=tenant.id, id__in=ids, status=AssetStatus.IN_SERVICE, awaiting_inspection=True,
                                 history_change_reason__in=[use_before_reason(label) for label in slugs])
            .select_related("history_user").order_by("history_date", "history_id"))
    for h in rows:
        label = h.history_change_reason[len(USE_BEFORE_PREFIX):]
        out.setdefault(h.id, []).append(UseBefore(on=timezone.localtime(h.history_date).date(), reason=slugs.get(label, ""), label=label,
                                                  by=h.history_user))
    return out


# --- the device drawer's banner ---------------------------------------------------------------------------------------------------

def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


@dataclass
class Banner:
    """What the device drawer says about a device's incoming inspection, for one user (banner()). The work orders are given only when
    the user may see them (apps.workorders.scoping.can_see_work_order); a number outside a scoped user's share is "" and the lines
    leave it out. `lines` are the sentences to show, in order (none for a device not waiting for its inspection)."""
    awaiting: bool
    lines: list = field(default_factory=list)
    open: WorkOrder | None = None  # the open incoming inspection (a re-inspection after a fail), when the user may see it
    open_number: str = ""
    open_due_on: date | None = None  # its due date, whether or not the user may see it
    failed: WorkOrder | None = None  # the last failed inspection, while the device still waits (when the user may see it)
    failed_number: str = ""
    failed_on: date | None = None
    passed: WorkOrder | None = None  # the first passed inspection (the evidence), when the user may see it
    passed_number: str = ""
    passed_on: date | None = None
    in_use: UseBefore | None = None  # in service before its inspection: the latest use_before_inspection, while it still waits
    offer_open: bool = False  # waiting with no inspection open: the drawer offers "Open incoming inspection"


def banner(asset, user=None) -> Banner:
    """The drawer's incoming-inspection banner for `asset` as `user` sees it (None: everything, as for services):
    - waiting: "Waiting for its incoming inspection: WO-26-0412, due Oct 12, 2026."
    - after a fail: "Failed its incoming inspection on Oct 9, 2026 (WO-26-0412); re-inspection WO-26-0431 open, due Oct 23, 2026."
      (or "; no re-inspection is open.")
    - none open: "Waiting for its incoming inspection; none is open." (offer_open)
    - put in use before it, while in service: "In use before its incoming inspection: Emergency clinical need (since Oct 7, 2026)."
    A device not waiting has no lines; its passed inspection is still given (passed, passed_on) for the PM tab. Queries: state()'s one,
    the history's one only for a device in service while it waits, and a visibility check per number for a scoped user only."""
    from apps.equipment.models import AssetStatus

    from .scoping import can_see_work_order

    def visible(wo):
        return wo if wo is not None and (user is None or can_see_work_order(user, wo)) else None

    s = state(asset)
    b = Banner(awaiting=s.awaiting)
    if s.passed is not None:
        b.passed, b.passed_on = visible(s.passed), s.passed.completed_on
        b.passed_number = b.passed.number if b.passed else ""
    if not s.awaiting:
        return b
    if s.open is not None:
        b.open, b.open_due_on = visible(s.open), s.open.due_on
        b.open_number = b.open.number if b.open else ""
    if s.failed is not None:
        b.failed, b.failed_on = visible(s.failed), s.failed.completed_on
        b.failed_number = b.failed.number if b.failed else ""
    b.offer_open = s.open is None
    if asset.status == AssetStatus.IN_SERVICE:
        uses = uses_before([asset.pk]).get(asset.pk)
        b.in_use = uses[-1] if uses else None
    if b.in_use is not None:
        b.lines.append(f"In use before its incoming inspection: {b.in_use.label} (since {_day(b.in_use.on)}).")
    if s.failed is not None:
        failed = f"Failed its incoming inspection on {_day(b.failed_on)}" + (f" ({b.failed_number})" if b.failed_number else "")
        if s.open is not None:
            b.lines.append(f"{failed}; re-inspection{' ' + b.open_number if b.open_number else ''} open, due {_day(b.open_due_on)}.")
        else:
            b.lines.append(f"{failed}; no re-inspection is open.")
    elif s.open is not None:
        b.lines.append(f"Waiting for its incoming inspection{': ' + b.open_number if b.open_number else ''}, due {_day(b.open_due_on)}.")
    else:
        b.lines.append("Waiting for its incoming inspection; none is open.")
    return b
