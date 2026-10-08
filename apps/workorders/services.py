"""
Work order lifecycle. Views and the API call these; they never change status fields directly.

Slice 20: assigning work to a technician (assign, or create_work_order with assigned_to) announces it through
apps.notifications.assignments, which emails the technician's account once the transaction commits (their choice, at most once per
work order, never the problem text). Callers that assign many at once wrap the loop in assignments.batch(), so each technician gets one
email listing them all.

Slice 24: a technician may take open, unassigned, in-house work on a device they are credentialed for (take), while the facility's
setting lets them; it is an assignment like any other (assign), made by the technician for themselves.

Slice 26, incoming inspections (apps.workorders.inspections): a device waiting for its incoming inspection (Asset.awaiting_inspection)
has one inspection open at a time and no PM work order (its PMs start when it passes): create_work_order refuses either, keyed "type".
Completing its passed inspection clears the flag, starts its PM clock, and puts it in service (equipment.services
.pass_incoming_inspection, from _on_completed); completing a tagged-out repair never puts it in service while it still waits.

Slice 28, a device held as evidence for an incident investigation (Asset.incident_hold, apps.incidents): nobody uses, repairs, or tests
it. change_status refuses to start or complete any of its work orders but the investigation of an open incident holding it
(incidents.services.investigation_of, by identity), reading the hold on the device's row locked after the work order's
(_lock_for_hold); completion.blocker says the same before anything starts (hold_blocker). Completing a repair, the investigation
included, never returns a held device to service: only the incident's release does. Everything else stays allowed: opening work
orders (PM generation included), assigning and taking them, notes, labor and parts, waiting on parts, and cancelling. History imported
from another system (apps.workorders.legacy) is written in its final state without change_status, so a hold never refuses it.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import BooleanField, Case, Exists, OuterRef, Q, Value, When
from django.utils import timezone

from apps.equipment.models import AssetStatus
from apps.notifications import assignments
from apps.pm.dates import add_months
from apps.pm.windows import window_of, windows

from .models import (
    ALLOWED_TRANSITIONS,
    DUE_DAYS,
    OPEN_STATUSES,
    InspectionResult,
    LateReason,
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


def _check_waiting_device(asset, type, follow_up_of=None) -> None:
    """Slice 26: a device waiting for its incoming inspection gets no PM work order (its PMs start when it passes) and never a second
    open incoming inspection; a re-inspection opened while the failed inspection it follows is being completed (`follow_up_of`) is
    the one exception. Keyed "type", the field New work order and the API name it in."""
    if not asset.awaiting_inspection:
        return
    if type == WoType.PM:
        raise ValidationError({"type": f"{asset.tag} is waiting for its incoming inspection: its PMs start when it passes its incoming inspection."})
    if type == WoType.INSPECTION:
        from . import inspections

        others = inspections.incoming(asset).filter(status__in=OPEN_STATUSES)
        if follow_up_of is not None:
            others = others.exclude(pk=follow_up_of.pk)
        first = others.order_by("opened_on", "number").first()
        if first is not None:
            raise ValidationError({"type": f"{asset.tag} already has its incoming inspection open: {first.number}."})


@transaction.atomic
def create_work_order(*, asset, type, priority, problem, requester="", source=Source.MANUAL, assigned_to=None, vendor_service=False,
                      vendor_name="", opened_on=None, due_on=None, created_by=None, tag_out=False, **extra) -> WorkOrder:
    """Open a work order. Slice 26: refused, keyed "type", for a PM on a device waiting for its incoming inspection, and for a second
    open incoming inspection on one (_check_waiting_device)."""
    _check_waiting_device(asset, type, extra.get("follow_up_of"))
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
    """Move `wo` along ALLOWED_TRANSITIONS, with a status history row. Slice 28: a start or a completion (HOLD_MOVES) of a work order
    whose device is held as evidence is refused in words (held_work_message) unless it is the investigation of an open incident holding
    the device, read on the device's row locked after the work order's (_lock_for_hold)."""
    as_of = as_of or timezone.localdate()
    if to_status not in ALLOWED_TRANSITIONS[wo.status]:
        raise ValidationError(f"Cannot move {wo.number} from {wo.get_status_display()} to {to_status}.")
    if to_status in HOLD_MOVES and as_of < wo.opened_on:
        raise ValidationError(f"{wo.number} cannot be started or completed before it was opened on {wo.opened_on:%b %-d, %Y}.")
    note = (note or "").strip()
    if len(note) > STATUS_NOTE_MAX:  # the API passes its note straight in; PostgreSQL refuses longer text (a 500, not a 400)
        raise ValidationError({"note": f"Keep the note to {STATUS_NOTE_MAX} characters."})
    if to_status in HOLD_MOVES:
        device = _lock_for_hold(wo)
        if _held_from(wo, device):
            raise ValidationError(held_work_message(device))
    from_status = wo.status
    wo.status = to_status
    if to_status == WoStatus.IN_PROGRESS:
        wo.started_on = wo.started_on or as_of
        if from_status in (WoStatus.COMPLETED, WoStatus.CLOSED):
            wo.completed_on = None
    if to_status == WoStatus.COMPLETED:
        wo.completed_on = as_of
        wo.started_on = wo.started_on or as_of
        _on_completed(wo, as_of, by=by)
    wo.save()
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status=from_status, to_status=to_status, changed_by=by, note=note)
    if to_status == WoStatus.COMPLETED and wo.source == Source.PORTAL:
        from apps.portal.notifications import request_done_after_commit  # the portal imports this module

        request_done_after_commit(wo)  # emails the requester once the completion is saved, at most once per request; never raises
    return wo


def holding_repairs(asset, exclude=None):
    """The device's open repairs that hold it out of service (tagged out with the request, or by a failed PM): while one is open,
    neither another repair's completion nor a passed incoming inspection puts it back in service; the last of them does."""
    held = WorkOrder.objects.filter(asset=asset, type=WoType.REPAIR, status__in=OPEN_STATUSES, tagged_out=True)
    return held.exclude(pk=exclude.pk) if exclude is not None else held


def _still_waiting(asset) -> bool:
    """Whether the device still waits for its incoming inspection (slice 26), read from the database: the copy a work order holds may
    predate the pass (equipment.services.pass_incoming_inspection, the flag's one writer, works on the row as it is)."""
    from apps.equipment.models import Asset

    return Asset.objects.filter(pk=asset.pk, awaiting_inspection=True).exists()


# --- a device held as evidence (slice 28) -----------------------------------------------------------

HOLD_MOVES = (WoStatus.IN_PROGRESS, WoStatus.COMPLETED)  # the moves a device's hold refuses to all but its investigation


def held_work_message(asset) -> str:
    """Why a work order on a device held as evidence does not start or complete: change_status's refusal and completion.blocker's
    words. No incident number: whoever reads it may not see incidents (Work orders Edit is enough to try)."""
    return f"{asset.tag} is held as evidence for an incident investigation: only its investigation work order may be started or completed."


def _held_from(wo: WorkOrder, asset) -> bool:
    """Whether `asset` (the device of `wo`, as read by the caller) is held as evidence and `wo` is not the investigation of an open
    incident holding it (incidents.services.investigation_of, by identity, never by type). The incidents are read only for a held
    device."""
    if not asset.incident_hold:
        return False
    from apps.incidents.services import investigation_of  # apps.incidents imports this app

    return wo.pk not in investigation_of(asset)


def _lock_for_hold(wo: WorkOrder):
    """The device of `wo` as it is now, locked until the transaction ends, for change_status's hold check. In the established order
    (equipment.services._locked_row's): the device's open work orders and `wo` itself, in number order (its open incoming
    inspections among them, inspections.lock_open's order), then the device's row. Slice 28 merge fix: all of them, not only `wo`, so
    a later step of the same transaction that takes the device's rows again (a failed PM's tag-out through set_status) never waits on
    a work order after holding the device's row (completion.complete_work_order opens that repair, its number first, before the
    start)."""
    from apps.equipment.models import Asset

    lock_work(wo.asset_id, wo.pk)
    return Asset.objects.select_for_update().only("pk", "tag", "incident_hold").get(pk=wo.asset_id)


def lock_work(asset_id, *also) -> None:
    """The first locks every writer of a device's work takes (slice 28): the device's open work orders and the work orders `also`
    names (one being completed or reopened, PMs a release gives a late reason), all in number order, until the transaction ends. Its
    row comes after (equipment.services._locked_row, _lock_for_hold). A writer that locked one work order before this would wait here
    for a lower number another writer holds while that writer waits for its row: so this comes first, before any other work order's
    row (complete_work_order, the incident services)."""
    rows = WorkOrder.objects.select_for_update().filter(Q(asset_id=asset_id, status__in=OPEN_STATUSES) | Q(pk__in=also)).order_by("number")
    list(rows.values_list("pk", flat=True))


def hold_blocker(wo: WorkOrder) -> str:
    """Why `wo` may not be started or completed because its device is held as evidence (held_work_message), or "": read now, without a
    lock, for what a screen offers and for completion.blocker (change_status checks again on the locked row)."""
    from apps.equipment.models import Asset

    asset = Asset.objects.only("pk", "tag", "incident_hold").get(pk=wo.asset_id)
    return held_work_message(asset) if _held_from(wo, asset) else ""


def with_held(qs):
    """Work orders `qs` annotated `held`: True for one its device's hold keeps from starting or completing (the device is held as
    evidence and the work order is not the investigation of an open incident holding it), in the same query. For lists that show
    the moves (My work's cards); change_status decides on the locked row."""
    from apps.incidents.models import IncidentHold
    from apps.incidents.models import Status as IncidentStatus

    investigation = IncidentHold.objects.filter(asset_id=OuterRef("asset_id"), released_on__isnull=True, incident__status=IncidentStatus.OPEN,
                                                incident__work_order=OuterRef("pk"))
    return qs.annotate(held=Case(When(asset__incident_hold=False, then=Value(False)), When(Exists(investigation), then=Value(False)),
                                 default=Value(True), output_field=BooleanField()))


def _still_held(asset) -> bool:
    """Whether the device is held as evidence (slice 28), read from the database: the copy a work order holds may predate the hold."""
    from apps.equipment.models import Asset

    return Asset.objects.filter(pk=asset.pk, incident_hold=True).exists()


def pm_schedule_anchor(wo: WorkOrder, done_on: date, w=None) -> date:
    """The day a PM completed on `done_on` sets its device's next PM from (slice 27): its due date when it was done after the due date
    but inside the facility's on-time window (apps.pm.windows, the model's class today), so a window that allows late completion never
    stretches the interval cycle after cycle; otherwise the day it was done, as always (by its due date, after its window, or under the
    default window, where nothing changes). The window is read only for a PM done after its due date."""
    if wo.due_on is None or done_on <= wo.due_on:
        return done_on
    w = window_of(w)
    if w.is_default:
        return done_on
    window = w.high if w.uniform else w.for_class(wo.asset.device_model.risk_class)
    return wo.due_on if done_on <= window.end(wo.due_on) else done_on


def next_pm_after(wo: WorkOrder, asset, done_on: date, w=None) -> date:
    """The device's next PM when its PM `wo` is completed on `done_on` (slice 27): one interval after pm_schedule_anchor, except that
    an anchored date never lands on or before the day the PM was done (a monthly PM under a window longer than a month) and never pulls
    the device's next PM back from a later date already set (an older PM reopened and completed again after a newer one moved it):
    then from the day it was done, as before the window (review fix)."""
    interval = asset.pm_interval_months
    plain = add_months(done_on, interval)
    anchor = pm_schedule_anchor(wo, done_on, w)
    if anchor == done_on:
        return plain
    anchored = add_months(anchor, interval)
    if anchored <= done_on or (asset.next_pm_on is not None and anchored < asset.next_pm_on):
        return plain
    return anchored


def _on_completed(wo: WorkOrder, as_of, by=None):
    asset = wo.asset
    if wo.type == WoType.PM:
        asset.last_pm_on = as_of
        fields = ["last_pm_on", "updated_at"]
        if not asset.awaiting_inspection:  # slice 26: a waiting device's PM clock starts at its pass (and no door opens a PM on one)
            # Slice 27: from the due date for a PM done late but inside its window (pm_schedule_anchor), else from the day it was done
            asset.next_pm_on = next_pm_after(wo, asset, as_of)
            fields.append("next_pm_on")
        asset.save(update_fields=fields)
    elif wo.type == WoType.INSPECTION and wo.inspection_result == InspectionResult.PASSED:
        # Slice 26: the pass clears the waiting flag, starts the PM clock, and puts the device in service unless a repair holds it out
        # (nothing moves when the device no longer waits). Imported here: equipment.services imports this module in its functions.
        from apps.equipment.services import pass_incoming_inspection

        pass_incoming_inspection(asset, wo, by=by, on=as_of)
    elif wo.type == WoType.REPAIR and (asset.status == AssetStatus.IN_REPAIR or (asset.status == AssetStatus.OUT_OF_SERVICE and wo.tagged_out)):
        # Back in service only when this repair is why it was out: tagged out with the request (the portal's checkbox), or marked in
        # repair. A device out of service for another reason (quarantined by hand) stays out until someone returns it from its
        # drawer, and one waiting for its incoming inspection (slice 26) until the inspection passes. And only when no other open
        # repair holds it out too (a second tagged-out request, or the repair a failed PM opened: apps.workorders.completion): the
        # last of them returns it. Slice 28: never a device held as evidence (the investigation's own completion included): only the
        # incident's release returns it.
        if not holding_repairs(asset, exclude=wo).exists() and not _still_waiting(asset) and not _still_held(asset):
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


# --- why a PM was late (slice 25) -----------------------------------------------------------------

def not_missed_message(wo: WorkOrder, w=None) -> str:
    """Why `wo` takes no reason for lateness: it is not a PM that missed its on-time window (slice 27). Under the default window the
    window is the due date, and the words stay slice 25's."""
    w = window_of(w)
    if wo.type != WoType.PM or w.is_default:
        return f"{wo.number} is not a PM that missed its due date, so it has no reason to record."
    window = w.high if w.uniform else w.for_class(wo.asset.device_model.risk_class)
    text = window.describe()
    return f"{wo.number} is not a PM that missed its on-time window ({text[0].lower()}{text[1:]}), so it has no reason to record."


@transaction.atomic
def set_late_reason(wo: WorkOrder, reason: str, by=None, today: date | None = None) -> WorkOrder:
    """Record why a PM missed its on-time window (LateReason; blank clears it), for the survey binder. A reason is set only on a PM that
    missed its window (apps.pm.services.missed_pms: past its window and not completed within it, whether open, completed late, or
    cancelled; the window is the due date unless the facility chose otherwise, apps.pm.windows), at any status: it documents the
    work, never changes it. Slice 27: blank clears a reason on any PM, so one recorded before the facility widened its window (the PM
    now on time) can be taken off. The caller checks the level (permissions.can_set_late_reason: Approve once the work order is
    closed). Audited in the work order's history, which is where the binder reads when a reason was recorded."""
    from apps.pm.services import missed_due_date

    reason = str(reason or "").strip()
    if reason and reason not in LateReason.values:
        raise ValidationError({"late_reason": "Choose one of the reasons listed."})
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    w = windows()
    if locked.type != WoType.PM or (reason and not missed_due_date(locked, today, w=w)):
        raise ValidationError({"late_reason": not_missed_message(locked, w)})
    if reason != locked.late_reason:
        locked.late_reason = reason
        locked._change_reason = f"Why late: {LateReason(reason).label}" if reason else "Why late cleared"
        locked.save(update_fields=["late_reason", "updated_at"])
    wo.late_reason = locked.late_reason
    return locked


# --- taking work (slice 24) -------------------------------------------------------------------------

def taking_technician(by):
    """The technician profile `by` takes work as, or a ValidationError saying why they take none: the facility lets technicians take
    work (Settings), `by` has Work orders Edit and sees the whole facility (a vendor's work is the facility's to assign), and has an
    active technician profile here (credentials.services.technician_of)."""
    from apps.credentials.services import technician_of
    from apps.facility.services import technicians_may_take

    from . import permissions, scoping

    if not technicians_may_take():
        raise ValidationError("Technicians do not take unassigned work in this facility; a CE manager assigns it.")
    if by is None or not permissions.can_take(by):
        raise ValidationError("Taking work needs Work orders Edit.")
    if scoping.is_scoped(by):
        raise ValidationError("Taking work is for the facility's own technicians; the facility assigns work to vendor service.")
    technician = technician_of(by)
    if technician is None:
        raise ValidationError("Your account has no technician profile here, so there is nobody to assign the work to.")
    return technician


def may_take_as(by):
    """The technician `by` may take work as, or None: what My work's "You could take" and the new work order form ask before they
    offer it (taking_technician without the reason)."""
    try:
        return taking_technician(by)
    except ValidationError:
        return None


@transaction.atomic
def take(wo: WorkOrder, by) -> WorkOrder:
    """`by` takes `wo` for themselves (slice 24: My work's Take, the new work order form's "Assign it to me", the API's take): an open
    work order nobody has started, unassigned and in-house (not vendor service), on a device `by`'s own technician profile is
    credentialed for today (credentials.services.qualification), while the facility lets technicians take work and `by` may
    (taking_technician). It is assigned through assign(), so the status history records it and nobody is emailed about their own
    choice. Anything else is a ValidationError in words. The row is locked first, so two technicians taking it at once cannot both
    have it: the second is told who did. `wo` is read again and updated in place."""
    from apps.credentials.services import qualification

    technician = taking_technician(by)
    if not WorkOrder.objects.select_for_update().filter(pk=wo.pk).exists():
        raise ValidationError(f"{wo.number} is no longer on file.")
    wo.refresh_from_db()
    if wo.vendor_service:
        raise ValidationError(f"{wo.number} is assigned to vendor service ({wo.vendor_name}).")
    if wo.assigned_to_id == technician.pk:
        raise ValidationError(f"{wo.number} is already yours.")
    if wo.assigned_to_id is not None:
        raise ValidationError(f"{wo.number} is already assigned to {wo.assigned_to.name}.")
    if wo.status != WoStatus.OPEN:
        raise ValidationError(f"{wo.number} is {wo.get_status_display().lower()}; only open work nobody has started can be taken.")
    q = qualification(technician, wo.asset)
    if not q.ok:
        what = f"the {wo.asset.device_model} ({wo.asset.tag})"
        if q.expired_only:
            raise ValidationError(f"Your credentials for {what} have expired, so a CE manager assigns {wo.number}.")
        raise ValidationError(f"You are not credentialed for {what}, so a CE manager assigns {wo.number}.")
    return assign(wo, technician=technician, by=by)


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
