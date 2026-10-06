"""
Work order history from the previous system (slice 23): the service the work order import (apps.imports.kinds.work_orders) calls
for each row of the file. A work order arrives in its final state with what it cost, and is recorded as history: it never emails
anyone, never changes its device, and never moves a PM date (completing a PM in Cadence moves the device's next one; an old PM is
already behind the dates the device carries).

What import_work_order records:
- A number of Cadence's own from the facility's sequence for the year it was opened (WO-24-0187, as WorkOrder.save numbers live
  work), or, for the check (`placeholder`), a stand-in that takes no lock on the numbering live work needs (the check is rolled
  back). Its number in the previous system (legacy_number) is kept beside it: shown in the drawer and on its print, searchable,
  and the key a re-run finds it by (find, find_many), so a file imported twice never doubles a work order.
- Source IMPORTED, the status the file gives (open, in progress, awaiting parts, completed, or closed), started on the day it was
  opened once work had started, and the problem in Cadence's own words: the old system's free text (problem, notes, requester)
  never comes across, since it may name a patient (CLAUDE.md, non-negotiable 6). No resolution.
- The technician (active or not: former staff did the work) or the vendor, set directly: no qualification check, no assignment
  row in the status history, no email (services.assign does all three for live work).
- An open repair on a device that is out of service now (or in repair) is tagged out: it is what holds the device out, so
  completing it returns the device (services._on_completed), as a tagged-out portal request's would. The device is not changed.
- Two status history rows, Opened and the final status (one for a work order still open), noted "Imported" and dated on the
  business days: opened, then completed (or, for work under way, the day it was opened), so the timeline reads in the order the
  work happened.
- Its costs (Costs), through apps.workorders.costs' import writer, which stays the only writer of lines: hours with a labor cost
  give the rate (cost over hours to the cent, half up; `drift` is what that rounding moves); hours alone are priced at today's
  Settings rate (a note says so); a labor cost without hours is outside service (a note); parts and outside cost are a part line
  each; a total alone (or beside a breakdown of zeros: exports write 0.00 in the columns they do not use) is one "Imported cost"
  line; a total that is not what the lines add up to gets a note. Hours are the technician's labor line, or the vendor's on vendor
  service; in-house hours that name no technician here are an in-house cost line of hours × rate (a note), since a labor line
  without a technician is vendor time wherever it is read. The lines are worked on the day it was completed, else opened, and
  dated between its two status rows.

The rows it refuses (ValidationError, in words the import screen shows beside the row): a number already here, a cancelled work
order (it was never work), an open PM (the planner makes each device's next PM from its next PM date: importing one would make a
second), an open work order on a retired device, a PM without its due date (PM on-time is counted by it), opened before 2000 or
after today, done without a completed date or completed before it was opened or after today, and a cost no line can hold.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from apps.credentials.models import Technician
from apps.equipment.models import AssetStatus
from apps.tenants.context import get_current_tenant

from . import costs as lines
from .models import DUE_DAYS, OPEN_STATUSES, Priority, Source, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType

EARLIEST = date(2000, 1, 1)  # older history is not imported: no report reads that far back, and dates before it are usually typos
NUMBER_MAX = WorkOrder._meta.get_field("legacy_number").max_length
VENDOR_NAME_MAX = WorkOrder._meta.get_field("vendor_name").max_length
IMPORTED_NOTE = "Imported"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
STARTED = (WoStatus.IN_PROGRESS, WoStatus.COMPLETED, WoStatus.CLOSED)
HELD_OUT = (AssetStatus.OUT_OF_SERVICE, AssetStatus.IN_REPAIR)
# The time of day each record is dated at on its business day: opened first, then the work's lines, then the final status.
OPENED_AT, STARTED_AT, LINES_AT, DONE_AT = time(8), time(8, 1), time(12), time(17)
PARTS, OUTSIDE, TOTAL_ONLY = "Parts (imported)", "Outside service (imported)", "Imported cost"
_AMOUNT_WORDS = {PARTS: "Parts cost", OUTSIDE: "Outside cost", TOTAL_ONLY: "Total cost"}
CENT = Decimal("0.01")

# Why a row is not imported, and notes on one that is: generic words, so the import screen groups them.
CANCELLED = "Cancelled in the previous system: not imported"
OPEN_PM = "Open PMs come from each device's next PM date: not imported"
RETIRED = "An open work order on a retired device: not imported"
ALREADY = "This number is already on a work order here"
ESTIMATED = "Labor cost estimated at today's rate"
NO_HOURS = "Labor cost without hours: imported as outside service"
TOTAL_DIFFERS = "Total cost is not what the labor, parts, and outside cost add up to: the lines were imported"
UNNAMED = "In-house hours without a technician here: recorded as an in-house labor cost"


@dataclass
class Costs:
    """A work order's costs as the previous system kept them; None where the file has none."""

    hours: Decimal | None = None
    labor: Decimal | None = None
    parts: Decimal | None = None
    outside: Decimal | None = None
    total: Decimal | None = None


@dataclass
class Imported:
    """What import_work_order recorded: the work order, notes for the person importing, and what its lines hold (the import's
    summary adds these up for the person to reconcile with the old system)."""

    work_order: WorkOrder
    notes: list[str] = field(default_factory=list)
    hours: Decimal = Decimal(0)
    labor: Decimal = Decimal(0)  # the hours × rate, to the cent: on the labor line, or the in-house cost line (UNNAMED)
    parts: Decimal = Decimal(0)
    outside: Decimal = Decimal(0)
    total_only: Decimal = Decimal(0)
    drift: Decimal = Decimal(0)  # `labor` minus the labor cost in the file: what rounding the rate moved


def problem_text(legacy_number: str) -> str:
    """An imported work order's problem: Cadence's words, never the old system's (which may name a patient)."""
    return f"Imported from the previous system (work order {legacy_number})"


def find(legacy_number: str) -> WorkOrder | None:
    """The facility's work order imported as `legacy_number` (any letter case), or None."""
    number = (legacy_number or "").strip()
    return WorkOrder.objects.filter(legacy_number__iexact=number).select_related("asset").first() if number else None


def find_many(numbers) -> dict[str, WorkOrder]:
    """{lowercased previous number: work order} for `numbers`, in one query (a chunk of the import)."""
    wanted = {n.strip().lower() for n in numbers if n and n.strip()}
    if not wanted:
        return {}
    qs = WorkOrder.objects.annotate(legacy_lower=Lower("legacy_number")).filter(legacy_lower__in=wanted).select_related("asset")
    return {wo.legacy_number.lower(): wo for wo in qs}


def _moment(day: date, at: time) -> datetime:
    """`day` at `at` on the facility's clock (its time zone is active inside it)."""
    return timezone.make_aware(datetime.combine(day, at))


def _refuse(message: str):
    raise ValidationError(message)


def _validate(*, tenant, asset, number, type, priority, status, opened_on, due_on, completed_on, technician, vendor, today) -> None:
    """The first reason the row is refused, as a ValidationError; nothing when it may be imported."""
    if not number:
        _refuse("No work order number")
    if len(number) > NUMBER_MAX:
        _refuse(f"The work order number is longer than {NUMBER_MAX} characters")
    if tenant is None or asset.tenant_id != tenant.id:
        _refuse("That device is not in this facility")
    if status == WoStatus.CANCELLED:
        _refuse(CANCELLED)
    if status not in WoStatus.values:
        _refuse("Status not read")
    if type not in WoType.values:
        _refuse("Type not read")
    if priority not in Priority.values:
        _refuse("Priority not read")
    is_open = status in OPEN_STATUSES
    if is_open and type == WoType.PM:
        _refuse(OPEN_PM)
    if is_open and asset.status == AssetStatus.RETIRED:
        _refuse(RETIRED)
    if opened_on is None:
        _refuse("No opened date: a work order needs the day it was opened")
    if opened_on < EARLIEST:
        _refuse(f"Opened before {EARLIEST.year}: not imported")
    if opened_on > today:
        _refuse("The opened date is in the future")
    if type == WoType.PM and due_on is None:
        _refuse("A PM needs its due date (PM on-time is counted by it)")
    if due_on is not None and due_on < EARLIEST:
        _refuse(f"The due date is before {EARLIEST.year}")
    if status in DONE:
        if completed_on is None:
            _refuse("Completed or closed without a completed date")
        if completed_on < opened_on:
            _refuse("Completed before it was opened")
        if completed_on > today:
            _refuse("The completed date is in the future")
    elif completed_on is not None:
        _refuse("A work order still open has no completed date")
    if technician is not None and vendor:
        _refuse("A work order is assigned to a technician or to a vendor, not both")
    if technician is not None and not Technician.objects.filter(pk=technician.pk).exists():  # the scoped manager: never another facility's
        _refuse("That technician is not in this facility")
    if len(vendor) > VENDOR_NAME_MAX:
        _refuse(f"The vendor's name is longer than {VENDOR_NAME_MAX} characters")
    if WorkOrder.objects.filter(legacy_number__iexact=number).exists():
        _refuse(ALREADY)


@dataclass
class _Plan:
    labor: tuple | None = None  # (hours, rate)
    parts: list = field(default_factory=list)  # [(description, amount)]
    notes: list = field(default_factory=list)
    drift: Decimal = Decimal(0)


def _plan(c: Costs, rate_today: Decimal) -> _Plan:
    """The lines `c` becomes, checked against what the line columns hold before anything is written."""
    plan = _Plan()
    if any(v is not None and v < 0 for v in (c.hours, c.labor, c.parts, c.outside, c.total)):
        _refuse("A cost or a number of hours is below 0")
    outside = c.outside or Decimal(0)
    if c.hours:  # 0 hours is no hours
        if c.hours > lines.IMPORT_HOURS_MAX:
            _refuse(f"Labor hours are more than one line holds ({lines.IMPORT_HOURS_MAX:,})")
        if c.labor is not None:
            rate = (c.labor / c.hours).quantize(CENT, rounding=ROUND_HALF_UP)
            if rate > lines.IMPORT_RATE_MAX:
                _refuse(f"Labor cost over hours is more than a rate holds (${lines.IMPORT_RATE_MAX:,} an hour)")
            plan.drift = lines.cents(c.hours * rate) - c.labor
        else:
            rate = rate_today
            plan.notes.append(ESTIMATED)
        plan.labor = (c.hours, rate)
    elif c.labor:
        outside += c.labor
        plan.notes.append(NO_HOURS)
    if c.parts:
        plan.parts.append((PARTS, c.parts))
    if outside:
        plan.parts.append((OUTSIDE, outside))
    itemized = any(v for v in (c.hours, c.labor, c.parts, c.outside))  # zeros are no breakdown: the total is then the cost
    if c.total and not itemized:
        plan.parts.append((TOTAL_ONLY, c.total))
    for description, amount in plan.parts:
        if amount > lines.IMPORT_AMOUNT_MAX:
            _refuse(f"{_AMOUNT_WORDS[description]} is more than one line holds (${lines.IMPORT_AMOUNT_MAX:,})")
    # The file's own sum (its labor cost where it has one, else the estimate), so the cent a rate's rounding moves is never a note
    labor = c.labor if c.labor is not None else (lines.cents(plan.labor[0] * plan.labor[1]) if plan.labor else Decimal(0))
    if c.total is not None and itemized and labor + (c.parts or 0) + (c.outside or 0) != c.total:
        plan.notes.append(TOTAL_DIFFERS)
    return plan


@transaction.atomic
def import_work_order(*, asset, legacy_number: str, type: str, status: str, opened_on: date | None, due_on: date | None = None,
                      completed_on: date | None = None, priority: str = Priority.NORMAL, technician=None, vendor_name: str = "",
                      costs: Costs | None = None, by=None, placeholder: bool = False, today: date | None = None, settings=None) -> Imported:
    """Record one work order from the previous system (see the module's docstring). `due_on` None: opened plus the priority's
    days, as a new work order's (never for a PM, which needs its own). `settings`: the facility's (apps.facility.services
    .get_settings), for the rate of hours without a cost; read when not given. Raises ValidationError in words."""
    today = today or timezone.localdate()
    number = (legacy_number or "").strip()
    vendor = " ".join((vendor_name or "").split())
    tenant = get_current_tenant()
    _validate(tenant=tenant, asset=asset, number=number, type=type, priority=priority, status=status, opened_on=opened_on, due_on=due_on,
              completed_on=completed_on, technician=technician, vendor=vendor, today=today)
    done = status in DONE
    wo = WorkOrder(tenant=tenant, asset=asset, type=type, priority=priority, status=status, source=Source.IMPORTED, legacy_number=number,
                   opened_on=opened_on, due_on=due_on or opened_on + timedelta(days=DUE_DAYS[priority]),
                   started_on=opened_on if status in STARTED else None, completed_on=completed_on if done else None,
                   assigned_to=technician, vendor_service=bool(vendor), vendor_name=vendor, problem=problem_text(number), created_by=by,
                   tagged_out=type == WoType.REPAIR and not done and asset.status in HELD_OUT)
    plan = _plan(costs or Costs(), lines.default_rate(wo, settings))  # checked before anything is written
    if placeholder:  # the check: rolled back, so it never takes the facility's numbering lock (Sequence) that live work waits on
        wo.number = f"CHECK-{uuid4().hex[:12].upper()}"
    wo._change_reason = IMPORTED_NOTE
    if by is not None:
        wo._history_user = by
    try:
        wo.save()
    finally:  # simple_history reads these on every save; never let them reach a later one
        wo.__dict__.pop("_change_reason", None)
        wo.__dict__.pop("_history_user", None)

    opened = WorkOrderStatusHistory.objects.create(tenant=tenant, work_order=wo, from_status="", to_status=WoStatus.OPEN, changed_by=by,
                                                   note=IMPORTED_NOTE)
    WorkOrderStatusHistory.objects.filter(pk=opened.pk).update(created_at=_moment(opened_on, OPENED_AT))
    if status != WoStatus.OPEN:
        final = WorkOrderStatusHistory.objects.create(tenant=tenant, work_order=wo, from_status=WoStatus.OPEN, to_status=status, changed_by=by,
                                                      note=IMPORTED_NOTE)
        at = _moment(completed_on, DONE_AT) if done else _moment(opened_on, STARTED_AT)
        WorkOrderStatusHistory.objects.filter(pk=final.pk).update(created_at=at)

    result = Imported(work_order=wo, notes=plan.notes, drift=plan.drift)
    at = _moment(wo.completed_on or wo.opened_on, LINES_AT)
    if plan.labor and technician is None and not wo.vendor_service:  # in-house time naming nobody here: never vendor time
        hours, rate = plan.labor
        line = lines.add_imported_unnamed_labor(wo, hours=hours, rate=rate, at=at, by=by)
        result.hours, result.labor = line.quantity, lines.part_amount(line)
        result.notes.append(UNNAMED)
    elif plan.labor:
        hours, rate = plan.labor
        line = lines.add_imported_labor(wo, hours=hours, rate=rate, technician=technician, at=at, by=by)
        result.hours, result.labor = line.hours, lines.labor_amount(line)
    for description, amount in plan.parts:
        lines.add_imported_part(wo, description=description, amount=amount, at=at, by=by)
        if description == PARTS:
            result.parts = amount
        elif description == OUTSIDE:
            result.outside = amount
        else:
            result.total_only = amount
    return result
