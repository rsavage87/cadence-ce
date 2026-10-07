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
"""
from __future__ import annotations

from dataclasses import dataclass
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
    """One query: the device's inspections, read in order."""
    s = InspectionState(awaiting=bool(asset.awaiting_inspection))
    for wo in incoming(asset).order_by("opened_on", "number"):
        if wo.status in OPEN_STATUSES and s.open is None:
            s.open = wo
        elif wo.inspection_result == InspectionResult.PASSED and s.passed is None:
            s.passed = wo
        elif wo.inspection_result == InspectionResult.FAILED:
            s.failed = wo
    return s


def open_for(asset, *, by=None, today: date | None = None, opened_on: date | None = None, due_on: date | None = None,
             problem: str = INCOMING_PROBLEM, follow_up_of: WorkOrder | None = None) -> WorkOrder:
    """Open an incoming inspection work order on `asset`, unassigned (the caller assigns it through services.take or assign, which
    write the history and the email). Opened on `opened_on` (Add device's added day) or today, due INSPECTION_DUE_DAYS later unless
    `due_on` says. Estimated hours: the model's PM procedure's, else 1."""
    from . import services  # services imports this module's constants

    today = today or timezone.localdate()
    opened_on = opened_on or today
    procedure = asset.device_model.pm_procedure
    hours = procedure.estimated_hours if procedure is not None else 1
    return services.create_work_order(asset=asset, type=WoType.INSPECTION, priority=Priority.NORMAL, problem=problem, opened_on=opened_on,
                                      due_on=due_on or opened_on + timedelta(days=INSPECTION_DUE_DAYS), created_by=by,
                                      estimated_hours=hours, follow_up_of=follow_up_of)
