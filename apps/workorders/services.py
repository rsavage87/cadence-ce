"""
Work order lifecycle. Views and the API call these; they never change status fields directly.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, Q, Value, When

from apps.equipment.models import AssetStatus
from apps.pm.dates import add_months

from .models import (
    ALLOWED_TRANSITIONS,
    DUE_DAYS,
    OPEN_STATUSES,
    Priority,
    ServiceRequest,
    Source,
    Urgency,
    WorkOrder,
    WorkOrderNote,
    WorkOrderStatusHistory,
    WoStatus,
    WoType,
)

URGENCY_TO_PRIORITY = {Urgency.CRITICAL: Priority.CRITICAL, Urgency.HIGH: Priority.HIGH, Urgency.NORMAL: Priority.NORMAL}
IN_HOUSE_RATE = 82.0
VENDOR_RATE = 215.0


@transaction.atomic
def create_work_order(*, asset, type, priority, problem, requester="", source=Source.MANUAL, assigned_to=None, vendor_service=False,
                      vendor_name="", opened_on=None, due_on=None, created_by=None, tag_out=False, **extra) -> WorkOrder:
    opened_on = opened_on or date.today()
    due_on = due_on or opened_on + timedelta(days=DUE_DAYS[priority])
    wo = WorkOrder(asset=asset, type=type, priority=priority, problem=problem, requester=requester, source=source, assigned_to=assigned_to,
                   vendor_service=vendor_service, vendor_name=vendor_name, opened_on=opened_on, due_on=due_on, created_by=created_by,
                   tagged_out=tag_out, tenant=asset.tenant, **extra)
    wo.save()
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status="", to_status=WoStatus.OPEN, changed_by=created_by, note="Opened")
    if tag_out and asset.status == AssetStatus.IN_SERVICE:
        asset.status = AssetStatus.OUT_OF_SERVICE
        asset.save(update_fields=["status", "updated_at"])
    return wo


@transaction.atomic
def change_status(wo: WorkOrder, to_status: str, by=None, note: str = "", as_of=None) -> WorkOrder:
    as_of = as_of or date.today()
    if to_status not in ALLOWED_TRANSITIONS[wo.status]:
        raise ValidationError(f"Cannot move {wo.number} from {wo.get_status_display()} to {to_status}.")
    if to_status in (WoStatus.IN_PROGRESS, WoStatus.COMPLETED) and as_of < wo.opened_on:
        raise ValidationError(f"{wo.number} cannot be started or completed before it was opened on {wo.opened_on:%b %-d, %Y}.")
    from_status = wo.status
    wo.status = to_status
    if to_status == WoStatus.IN_PROGRESS:
        wo.started_on = wo.started_on or as_of
        if from_status in (WoStatus.COMPLETED, WoStatus.CLOSED):
            wo.completed_on = None
    if to_status == WoStatus.COMPLETED:
        wo.completed_on = as_of
        wo.started_on = wo.started_on or as_of
        _on_completed(wo, as_of)
    wo.save()
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status=from_status, to_status=to_status, changed_by=by, note=note)
    return wo


def _on_completed(wo: WorkOrder, as_of):
    asset = wo.asset
    if wo.type == WoType.PM:
        asset.last_pm_on = as_of
        asset.next_pm_on = add_months(as_of, asset.pm_interval_months)
        asset.save(update_fields=["last_pm_on", "next_pm_on", "updated_at"])
    elif wo.type == WoType.REPAIR and asset.status in (AssetStatus.IN_REPAIR, AssetStatus.OUT_OF_SERVICE):
        asset.status = AssetStatus.IN_SERVICE
        asset.save(update_fields=["status", "updated_at"])


@transaction.atomic
def assign(wo: WorkOrder, technician=None, vendor_name: str = "", by=None) -> WorkOrder:
    """Assign to a technician or to vendor service. Flags credential overrides in the status history."""
    from apps.credentials.services import qualification

    note = ""
    if vendor_name:
        wo.vendor_service, wo.vendor_name, wo.assigned_to = True, vendor_name, None
        note = f"Assigned to vendor: {vendor_name}"
    elif technician is not None:
        wo.vendor_service, wo.vendor_name, wo.assigned_to = False, "", technician
        q = qualification(technician, wo.asset)
        note = f"Assigned to {technician.name}" + ("" if q.ok else " (override: not credentialed for this device)")
    wo.save(update_fields=["vendor_service", "vendor_name", "assigned_to", "updated_at"])
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status=wo.status, to_status=wo.status, changed_by=by, note=note)
    return wo


@transaction.atomic
def create_service_request(*, asset, department, problem, urgency, requester_name="", callback="", room="", tagged_out=False, ip=None) -> ServiceRequest:
    priority = URGENCY_TO_PRIORITY[urgency]
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=priority, problem=problem,
                           requester=f"{requester_name or 'Unit staff'}, {department.name}", source=Source.PORTAL, callback=callback,
                           reported_location=f"{department.name} {room}".strip(), tag_out=tagged_out)
    return ServiceRequest.objects.create(tenant=asset.tenant, asset=asset, department=department, room=room, requester_name=requester_name, callback=callback,
                                         problem=problem, urgency=urgency, tagged_out=tagged_out, work_order=wo, submitted_ip=ip)


def open_work_orders():
    return WorkOrder.objects.filter(status__in=OPEN_STATUSES)


NOTE_MAX_LENGTH = 1000


def add_note(wo: WorkOrder, text: str, by=None) -> WorkOrderNote:
    text = (text or "").strip()
    if not text:
        raise ValidationError("A note needs some text.")
    if len(text) > NOTE_MAX_LENGTH:
        raise ValidationError(f"Notes are limited to {NOTE_MAX_LENGTH} characters.")
    name = (by.get_full_name() or by.username) if by else ""
    return WorkOrderNote.objects.create(tenant=wo.tenant, work_order=wo, author=by, author_name=name, text=text)


# --- lists for the Work orders screen ---------------------------------------------------------

UNASSIGNED = "unassigned"
BOARD_LIMIT = 25
BOARD_RECENT_DAYS = 7
PRIORITY_RANK = Case(*[When(priority=p, then=Value(i)) for i, p in enumerate(Priority.values)], default=Value(9))


@dataclass
class WorkOrderFilters:
    q: str = ""
    type: str = ""
    status: str = ""
    assigned: str = ""  # technician id, or "unassigned"
    open_only: bool = True


def filter_work_orders(f: WorkOrderFilters, ignore_status: bool = False):
    qs = (WorkOrder.objects.select_related("asset", "asset__device_model", "asset__department", "assigned_to")
          .prefetch_related("labor_lines", "part_lines"))
    if f.q:
        q = f.q.strip()
        qs = qs.filter(Q(number__icontains=q) | Q(asset__tag__icontains=q) | Q(asset__device_model__description__icontains=q)
                       | Q(asset__device_model__model__icontains=q) | Q(problem__icontains=q) | Q(asset__department__name__icontains=q)
                       | Q(requester__icontains=q))
    if f.type:
        qs = qs.filter(type=f.type)
    if f.assigned == UNASSIGNED:
        qs = qs.filter(assigned_to__isnull=True, vendor_service=False)
    elif f.assigned:
        qs = qs.filter(assigned_to_id=f.assigned)
    if not ignore_status:
        if f.status:
            qs = qs.filter(status=f.status)
        if f.open_only:
            qs = qs.filter(status__in=OPEN_STATUSES)
    return qs.order_by("-opened_on", "-created_at")


def unassigned_portal_requests():
    return WorkOrder.objects.filter(source=Source.PORTAL, status__in=OPEN_STATUSES, assigned_to__isnull=True, vendor_service=False)


def board_columns(f: WorkOrderFilters, today: date | None = None) -> list[dict]:
    """Kanban columns. Status filters don't apply (the columns are the statuses); done work shows for the last 7 days."""
    today = today or date.today()
    base = filter_work_orders(f, ignore_status=True).order_by(PRIORITY_RANK, "due_on", "number")
    recent = today - timedelta(days=BOARD_RECENT_DAYS)
    columns = [
        ("Open", base.filter(status=WoStatus.OPEN)),
        ("In progress", base.filter(status=WoStatus.IN_PROGRESS)),
        ("Awaiting parts", base.filter(status=WoStatus.AWAITING_PARTS)),
        (f"Completed, last {BOARD_RECENT_DAYS} days", base.filter(status=WoStatus.COMPLETED, completed_on__gte=recent)),
        (f"Closed, last {BOARD_RECENT_DAYS} days", base.filter(status=WoStatus.CLOSED, completed_on__gte=recent)),
    ]
    return [{"name": name, "count": qs.count(), "items": list(qs[:BOARD_LIMIT])} for name, qs in columns]


def timeline(wo: WorkOrder) -> list[dict]:
    """Status history and notes for the work order drawer, oldest first."""
    entries = []
    for h in wo.status_history.select_related("changed_by"):
        who = str(h.changed_by) if h.changed_by else ""
        if not h.from_status:
            text = f"Opened{' from the service request portal' if wo.source == Source.PORTAL else ''}: {wo.problem}"
            who = who or wo.requester or "System"
        elif h.from_status == h.to_status:
            text = h.note
        else:
            text = f"Status changed to {h.get_to_status_display().lower()}" + (f": {h.note}" if h.note else "")
        entries.append({"at": h.created_at, "who": who or "System", "text": text})
    for n in wo.notes.all():
        entries.append({"at": n.created_at, "who": n.author_name or str(n.author or "") or "Staff", "text": n.text})
    entries.sort(key=lambda e: e["at"])
    return entries
