"""
Work order lifecycle. Views and the API call these; they never change status fields directly.

Slice 20: assigning work to a technician (assign, or create_work_order with assigned_to) announces it through
apps.notifications.assignments, which emails the technician's account once the transaction commits (their choice, at most once per
work order, never the problem text). Callers that assign many at once wrap the loop in assignments.batch(), so each technician gets one
email listing them all.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, Q, Value, When
from django.utils import timezone

from apps.equipment.models import AssetStatus
from apps.notifications import assignments
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

STATUS_NOTE_MAX = WorkOrderStatusHistory._meta.get_field("note").max_length
VENDOR_NAME_MAX = WorkOrder._meta.get_field("vendor_name").max_length
NAME_MAX = WorkOrder._meta.get_field("requester").max_length  # the 120-character name columns (requester, author_name, Technician.name)
URGENCY_TO_PRIORITY = {Urgency.CRITICAL: Priority.CRITICAL, Urgency.HIGH: Priority.HIGH, Urgency.NORMAL: Priority.NORMAL}


@transaction.atomic
def create_work_order(*, asset, type, priority, problem, requester="", source=Source.MANUAL, assigned_to=None, vendor_service=False,
                      vendor_name="", opened_on=None, due_on=None, created_by=None, tag_out=False, **extra) -> WorkOrder:
    opened_on = opened_on or timezone.localdate()
    due_on = due_on or opened_on + timedelta(days=DUE_DAYS[priority])
    wo = WorkOrder(asset=asset, type=type, priority=priority, problem=problem, requester=requester, source=source, assigned_to=assigned_to,
                   vendor_service=vendor_service, vendor_name=vendor_name, opened_on=opened_on, due_on=due_on, created_by=created_by,
                   tagged_out=tag_out, tenant=asset.tenant, **extra)
    wo.save()
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status="", to_status=WoStatus.OPEN, changed_by=created_by, note="Opened")
    if tag_out and asset.status == AssetStatus.IN_SERVICE:
        asset.status = AssetStatus.OUT_OF_SERVICE
        asset.save(update_fields=["status", "updated_at"])
    if assigned_to is not None and not vendor_service:
        assignments.announce(wo, assigned_to, by=created_by)  # slice 20: the technician is emailed once this commits
    return wo


@transaction.atomic
def change_status(wo: WorkOrder, to_status: str, by=None, note: str = "", as_of=None) -> WorkOrder:
    as_of = as_of or timezone.localdate()
    if to_status not in ALLOWED_TRANSITIONS[wo.status]:
        raise ValidationError(f"Cannot move {wo.number} from {wo.get_status_display()} to {to_status}.")
    if to_status in (WoStatus.IN_PROGRESS, WoStatus.COMPLETED) and as_of < wo.opened_on:
        raise ValidationError(f"{wo.number} cannot be started or completed before it was opened on {wo.opened_on:%b %-d, %Y}.")
    note = (note or "").strip()
    if len(note) > STATUS_NOTE_MAX:  # the API passes its note straight in; PostgreSQL refuses longer text (a 500, not a 400)
        raise ValidationError({"note": f"Keep the note to {STATUS_NOTE_MAX} characters."})
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
    if to_status == WoStatus.COMPLETED and wo.source == Source.PORTAL:
        from apps.portal.notifications import request_done_after_commit  # the portal imports this module

        request_done_after_commit(wo)  # emails the requester once the completion is saved, at most once per request; never raises
    return wo


def _on_completed(wo: WorkOrder, as_of):
    asset = wo.asset
    if wo.type == WoType.PM:
        asset.last_pm_on = as_of
        asset.next_pm_on = add_months(as_of, asset.pm_interval_months)
        asset.save(update_fields=["last_pm_on", "next_pm_on", "updated_at"])
    elif wo.type == WoType.REPAIR and (asset.status == AssetStatus.IN_REPAIR or (asset.status == AssetStatus.OUT_OF_SERVICE and wo.tagged_out)):
        # Back in service only when this repair is why it was out: tagged out with the request (the portal's checkbox), or marked in
        # repair. A device out of service for another reason (awaiting incoming inspection, quarantined by hand) stays out until
        # someone returns it from its drawer. And only when no other open repair holds it out too (a second tagged-out request, or
        # the repair a failed PM opened: apps.workorders.completion): the last of them returns it.
        held = WorkOrder.objects.filter(asset=asset, type=WoType.REPAIR, status__in=OPEN_STATUSES, tagged_out=True).exclude(pk=wo.pk)
        if not held.exists():
            asset.status = AssetStatus.IN_SERVICE
            asset.save(update_fields=["status", "updated_at"])


@transaction.atomic
def assign(wo: WorkOrder, technician=None, vendor_name: str = "", by=None) -> WorkOrder:
    """Assign to a technician or to vendor service. Flags credential overrides in the status history."""
    from apps.credentials.services import qualification

    note = ""
    vendor_name = (vendor_name or "").strip()  # a stray space from the API would keep it from its company's share (apps.workorders.scoping)
    if len(vendor_name) > VENDOR_NAME_MAX:
        raise ValidationError({"vendor_name": f"Keep the vendor's name to {VENDOR_NAME_MAX} characters."})
    if vendor_name:
        wo.vendor_service, wo.vendor_name, wo.assigned_to = True, vendor_name, None
        note = f"Assigned to vendor: {vendor_name}"
    elif technician is not None:
        wo.vendor_service, wo.vendor_name, wo.assigned_to = False, "", technician
        q = qualification(technician, wo.asset)
        note = f"Assigned to {technician.name}" + ("" if q.ok else " (override: not credentialed for this device)")
    wo.save(update_fields=["vendor_service", "vendor_name", "assigned_to", "updated_at"])
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status=wo.status, to_status=wo.status, changed_by=by, note=note)
    if technician is not None and not vendor_name:
        assignments.announce(wo, technician, by=by)  # slice 20: emailed once this commits, at most once per work order and person
    return wo


@transaction.atomic
def create_service_request(*, asset, department, problem, urgency, requester_name="", callback="", room="", tagged_out=False, ip=None,
                           requester_email="") -> ServiceRequest:
    """`requester_email` (optional, lowercased) gets a confirmation once the request is saved, and a notice when its work order is
    completed, each only while the facility confirms by email and the address is at one of its domains (apps.portal.notifications)."""
    priority = URGENCY_TO_PRIORITY[urgency]
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=priority, problem=problem,
                           # Composed from fields each within its own limit, so cut to the columns' (PostgreSQL refuses longer text).
                           requester=f"{requester_name or 'Unit staff'}, {department.name}"[:120], source=Source.PORTAL, callback=callback,
                           reported_location=f"{department.name} {room}".strip()[:120], tag_out=tagged_out)
    sr = ServiceRequest.objects.create(tenant=asset.tenant, asset=asset, department=department, room=room, requester_name=requester_name, callback=callback,
                                       requester_email=(requester_email or "").strip().lower(), problem=problem, urgency=urgency, tagged_out=tagged_out,
                                       work_order=wo, submitted_ip=ip)
    if sr.requester_email:
        from apps.portal.notifications import request_received_after_commit  # the portal imports this module

        request_received_after_commit(sr)
    return sr


def open_work_orders():
    return WorkOrder.objects.filter(status__in=OPEN_STATUSES)


NOTE_MAX_LENGTH = 1000


def add_note(wo: WorkOrder, text: str, by=None) -> WorkOrderNote:
    text = (text or "").strip()
    if not text:
        raise ValidationError("A note needs some text.")
    if len(text) > NOTE_MAX_LENGTH:
        raise ValidationError(f"Notes are limited to {NOTE_MAX_LENGTH} characters.")
    name = (str(by) if by else "")[:NAME_MAX]  # a full name may be longer than the column
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


def filter_work_orders(f: WorkOrderFilters, ignore_status: bool = False, qs=None):
    """The Work orders list's filters, over `qs` when given (a scoped user's work orders, apps.workorders.scoping), else all."""
    qs = ((WorkOrder.objects.all() if qs is None else qs).select_related("asset", "asset__device_model", "asset__department", "assigned_to")
          .prefetch_related("labor_lines", "part_lines"))
    if f.q:
        q = f.q.strip()
        qs = qs.filter(Q(number__icontains=q) | Q(asset__tag__icontains=q) | Q(asset__device_model__description__icontains=q)
                       | Q(asset__device_model__model__icontains=q) | Q(problem__icontains=q) | Q(asset__department__name__icontains=q)
                       | Q(requester__icontains=q) | Q(legacy_number__icontains=q))  # slice 23: an imported one's number in the previous system
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


def board_columns(f: WorkOrderFilters, today: date | None = None, qs=None) -> list[dict]:
    """Kanban columns. Status filters don't apply (the columns are the statuses); done work shows for the last 7 days. Over `qs`
    when given, as filter_work_orders."""
    today = today or timezone.localdate()
    base = filter_work_orders(f, ignore_status=True, qs=qs).order_by(PRIORITY_RANK, "due_on", "number")
    recent = today - timedelta(days=BOARD_RECENT_DAYS)
    columns = [
        ("Open", base.filter(status=WoStatus.OPEN)),
        ("In progress", base.filter(status=WoStatus.IN_PROGRESS)),
        ("Awaiting parts", base.filter(status=WoStatus.AWAITING_PARTS)),
        (f"Completed, last {BOARD_RECENT_DAYS} days", base.filter(status=WoStatus.COMPLETED, completed_on__gte=recent)),
        (f"Closed, last {BOARD_RECENT_DAYS} days", base.filter(status=WoStatus.CLOSED, completed_on__gte=recent)),
    ]
    return [{"name": name, "count": qs.count(), "items": list(qs[:BOARD_LIMIT])} for name, qs in columns]


def _cost_events(wo: WorkOrder) -> list[dict]:
    """Labor and part lines as the timeline tells them (slice 15), from their audit history: each line when it was logged and by
    whom ("1.5 h logged by Dana Whitfield", "Part: Pump door latch × 2 ($84.00)"), and each removal. A line on file from before
    lines were audited (no "Added" record) is told from the line itself."""
    from apps.credentials.models import Technician

    from . import costs
    from .models import LaborLine, PartLine

    # Two queries whatever the number of lines (the work-order print counts them): the technician comes with each record.
    labor_history = list(LaborLine.history.filter(work_order_id=wo.pk, tenant_id=wo.tenant_id, history_type__in=("+", "-"))
                         .select_related("history_user", "technician").order_by("history_date", "history_id"))
    part_history = list(PartLine.history.filter(work_order_id=wo.pk, tenant_id=wo.tenant_id, history_type__in=("+", "-"))
                        .select_related("history_user").order_by("history_date", "history_id"))
    told = {h.id for h in [*labor_history, *part_history] if h.history_type == "+"}
    untold = [line for line in wo.labor_lines.all() if line.id not in told]
    tech_ids = {line.technician_id for line in untold if line.technician_id}
    names = {t.pk: t.name for t in Technician.objects.filter(pk__in=tech_ids)} if tech_ids else {}  # only for lines from before the audit

    entries = []
    for h in labor_history:
        name = h.technician.name if h.technician_id and h.technician else ""
        text = costs.labor_event(h.hours, name)
        if h.history_type == "-":
            text = f"Labor removed: {text[0].lower()}{text[1:]}, worked {h.worked_on:%b %-d, %Y}"
        entries.append({"at": h.history_date, "who": str(h.history_user) if h.history_user else (name or "System"), "text": text})
    for h in part_history:
        text = costs.part_event(h.description, h.quantity, h.unit_cost)
        entries.append({"at": h.history_date, "who": str(h.history_user) if h.history_user else "System",
                        "text": f"Part removed: {text}" if h.history_type == "-" else f"Part: {text}"})
    for line in untold:
        name = names.get(line.technician_id, "")
        entries.append({"at": line.created_at, "who": name or "System", "text": costs.labor_event(line.hours, name)})
    for line in wo.part_lines.all():
        if line.id not in told:
            entries.append({"at": line.created_at, "who": "System", "text": f"Part: {costs.part_event(line.description, line.quantity, line.unit_cost)}"})
    return entries


def timeline(wo: WorkOrder) -> list[dict]:
    """Status history, notes, and labor and parts (slice 15) for the work order drawer, oldest first."""
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
    entries += _cost_events(wo)  # after the rest: at the same instant, the status change that came with it reads first
    entries.sort(key=lambda e: e["at"])
    return entries
