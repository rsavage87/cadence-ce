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

Slice 29, a temporary device (a rental, vendor loaner, or demo unit: equipment.services.owner_maintains): its owner maintains it, so
create_work_order refuses a PM on one, keyed "type" (no_pm_message), and completing a PM never sets a temporary device's next PM (no
door opens one; _on_completed keeps it so). Every other type is opened as for our devices: repairs, recalls (vendor service named for
its owner: apps.recalls.services), its incoming inspection (inspections.TEMPORARY_INCOMING), an incident's investigation.

Slice 30, checks after a repair (apps.workorders.repair_checks, completion.complete_work_order): a repair completed as not returned to
use (RepairCheck.NOT_RETURNED) never returns its device (returns_device, _on_completed's rule); one completed as checked, or with the
vendor's report, returns it as a repair always has. While a repair holds the device out (holding_repairs, or a device in repair with an
open repair) the device's status never goes in service or on loan by hand (equipment.services.repair_hold_message).

Slice 33, what a repair found (WorkOrder.repair_finding; apps.workorders.findings): completion.complete_work_order records it, and
set_repair_finding records or corrects it afterwards on a completed or closed repair (one completed before findings were recorded too).
Two writes keep a stored finding true to the repair: assign clears "not stated by the vendor" when the work leaves that vendor
(vendors.rules.vendor_key, as for the vendor's report number), and clear_use_error takes a use error off a repair an incident adopts as
its investigation (an investigation never records one: who erred stays in the facility's event report).

Slice 35, the test equipment a work order recorded (apps.testequipment: TestEquipmentUse): completion.complete_work_order records it, and
set_test_equipment records or corrects it afterwards on a completed or closed work order (the same rules: testequipment.services
.check_uses). assign clears it when an open work order moves to a vendor (testequipment.services.clear_for_vendor, audited): that work
is the vendor's, and its report names its own test equipment; change_status clears it on a cancel (clear_for_cancel, taking no item's
row: see lock_work); neither ever takes off a use the kept-use rule keeps (each use judged on its own day, used_on). A work order with
test equipment recorded is never deleted (PROTECT).
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import BooleanField, Case, Exists, OuterRef, Q, Value, When
from django.utils import timezone

from apps.equipment.models import Asset, AssetStatus
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
    RepairCheck,
    RepairFinding,
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


def no_pm_message(asset) -> str:
    """Why a temporary device gets no PM work order (slice 29): create_work_order's refusal, keyed "type"; an edit that would put a
    PM on one can say the same."""
    return f"{asset.tag} is a {asset.get_ownership_display().lower()}: its owner maintains it, so it gets no PM work orders here."


# Slice 38: a software update is opened only by the Device security screen's openers (apps.cybersecurity.updates.open_update and
# open_updates), which write its update record with its target and pass the private keyword: never New work order, the API's create, or
# any later door that passes a user's type (keyed "type", the field both doors name it in).
UPDATE_OPENED_ELSEWHERE = "Software updates are opened from the Device security screen or a device's Security tab."


def _check_update_type(type, register_opener: bool) -> None:
    if type == WoType.SOFTWARE_UPDATE and not register_opener:
        raise ValidationError({"type": UPDATE_OPENED_ELSEWHERE})


def _tag_out_row(asset, opened_on) -> Asset:
    """The device's row as it is now, for a work order that tags it out, locked in the facility's order (its work, the work order
    numbering, its row: as equipment.services._locked_row), so the tag-out moves the row as it is, never the caller's copy: moving
    a device in service out of service needs no Equipment level, but the same write on a device retired since the caller read it
    would reinstate it, which needs Equipment Approve. A retired device (or one returned to its owner) is refused, keyed "asset"."""
    from apps.equipment.services import owner_maintains  # equipment.services imports this app inside its functions; keep it one way

    lock_work(asset.pk)
    lock_numbering(opened_on.year)
    fresh = Asset.objects.select_for_update().get(pk=asset.pk)  # the device's row only (a join would lock its model's row too)
    if fresh.status == AssetStatus.RETIRED:
        state = "returned to its owner" if owner_maintains(fresh) else "retired"
        raise ValidationError({"asset": TAG_OUT_REFUSED.format(tag=fresh.tag, state=state)})
    return fresh


TAG_OUT_REFUSED = "{tag} is {state}, so no work order tags it out."


def _check_owner_maintained(asset, type) -> None:
    """Slice 29: no PM work order on a device its owner maintains (equipment.services.owner_maintains)."""
    from apps.equipment.services import owner_maintains  # equipment.services imports this app inside its functions; keep it one way

    if type == WoType.PM and owner_maintains(asset):
        raise ValidationError({"type": no_pm_message(asset)})


@transaction.atomic
def create_work_order(*, asset, type, priority, problem, requester="", source=Source.MANUAL, assigned_to=None, vendor_service=False,
                      vendor_name="", opened_on=None, due_on=None, created_by=None, tag_out=False, **extra) -> WorkOrder:
    """Open a work order. Slice 26: refused, keyed "type", for a PM on a device waiting for its incoming inspection, and for a second
    open incoming inspection on one (_check_waiting_device). Slice 29: refused, keyed "type", for a PM on a temporary device
    (_check_owner_maintained). Slice 38: refused, keyed "type", for a software update unless `_register_opener` (apps.cybersecurity.updates
    only: _check_update_type)."""
    _check_update_type(type, bool(extra.pop("_register_opener", False)))
    _check_owner_maintained(asset, type)
    _check_waiting_device(asset, type, extra.get("follow_up_of"))
    opened_on = opened_on or timezone.localdate()
    due_on = due_on or opened_on + timedelta(days=DUE_DAYS[priority])
    tagged = _tag_out_row(asset, opened_on) if tag_out else None
    wo = WorkOrder(asset=asset, type=type, priority=priority, problem=problem, requester=requester, source=source, assigned_to=assigned_to,
                   vendor_service=vendor_service, vendor_name=vendor_name, opened_on=opened_on, due_on=due_on, created_by=created_by,
                   tagged_out=tag_out, tenant=asset.tenant, **extra)
    wo.save()
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status="", to_status=WoStatus.OPEN, changed_by=created_by, note="Opened")
    if tagged is not None and tagged.status == AssetStatus.IN_SERVICE:
        tagged.status = AssetStatus.OUT_OF_SERVICE
        tagged.save(update_fields=["status", "updated_at"])
        asset.status = tagged.status  # the caller's copy follows
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
    if to_status == WoStatus.CANCELLED:  # slice 35 review fix: a cancelled work order records no test equipment (kept uses stay)
        from apps.testequipment.services import clear_for_cancel

        clear_for_cancel(wo, by=by)
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
    row (complete_work_order, the incident services).

    Slice 35, test equipment: a writer of a work order's test equipment (complete_work_order, set_test_equipment) takes this, then the
    work order's row, then the test equipment items' rows (those it names and those already on the work order, in pk order:
    testequipment.services.check_uses), and only then what else it takes (a failed PM's repair number, the device's row). assign takes
    the work order's row, then the items' rows of the uses it clears. A cancel (change_status: testequipment.services.clear_for_cancel)
    takes no item's row: equipment's retirement and a passed incoming inspection cancel several work orders after taking the device's
    row, and items locked there would wait in a circle with a writer above holding items before the device (review fix). Every test
    equipment writer of its own (apps.testequipment.services: details, status, calibrations, entered in error, the review) locks only its
    item's row (after the facility's tag lock when it takes one) and takes nothing after it, so no writer waits for a work order while
    holding an item."""
    lock_devices_work([asset_id], *also)


def lock_numbering(year: int) -> None:
    """Hold the facility's work order numbering for `year` (the Sequence row WorkOrder.save takes for a work order opened that year:
    `wo-<year>`, got or created, never counted here) until the transaction ends. The order every writer keeps: the facility's tag lock
    where a writer takes it (equipment.services.lock_tags: create_asset, then an incoming inspection's number), a device's work
    (lock_work; a writer of several devices' work, all of it first: lock_devices_work), then the numbering, then the devices' rows.
    hold_device opening an investigation and Auto-assign week take every device's work before they number one; a devices import chunk
    takes this after its devices' work (and after the tag lock, which it takes first then) when one of its rows may reinstate a device
    waiting for its incoming inspection. A writer that may open a work order after
    taking a device's row takes this before that row (equipment.services.set_status's reinstate of a device waiting for its incoming
    inspection, use_before_inspection):
    a batch holds the numbering for its whole transaction and takes each device after it (a recall batch through its work orders'
    inserts, at COMMIT: Django's PostgreSQL foreign keys are deferred; slice 38's update batch explicitly), so a writer holding a
    device's row while it waited for the numbering waited in a circle with it."""
    from apps.core.models import Sequence
    from apps.tenants.context import get_current_tenant

    tenant = get_current_tenant()
    if tenant is None:
        raise RuntimeError("lock_numbering() needs a tenant context")
    key = f"wo-{year}"
    try:
        with transaction.atomic():  # a savepoint: two first-ever work orders of the year may both try to create the row
            Sequence.unscoped.get_or_create(tenant=tenant, key=key)  # unscoped + explicit tenant, as Sequence.next
    except IntegrityError:
        pass
    Sequence.unscoped.select_for_update().get(tenant=tenant, key=key)


def lock_devices_work(asset_ids, *also) -> None:
    """lock_work for several devices at once: all their open work orders and the work orders `also` names, in ONE number order, one
    query, until the transaction ends. Every writer of several devices' work takes this before any device's row (then the devices in
    the database's tag order, or one after another), never one device's work and row after another's: a sweep holds a device's work and
    row while another writer holding the lower numbers waits for it. The writers: AEM's pull-in (apps.pm.aem._pull_in, the model's
    devices in use), an incident's release and Recorded in error (incidents.services._lock_device_work, with the held days' late PMs),
    hold_device opening an investigation (the suspect and the held device), Auto-assign week (pm.services.assign_week, the week's
    devices), and a devices import chunk (imports.kinds.devices, the chunk's devices already here). Each device's own lock_work after this
    takes only what it holds already, or work orders opened since (higher numbers)."""
    rows = (WorkOrder.objects.select_for_update().filter(Q(asset_id__in=list(asset_ids), status__in=OPEN_STATUSES) | Q(pk__in=also))
            .order_by("number"))
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
    from apps.equipment.services import owner_maintains  # equipment.services imports this module in its functions; keep it one way

    asset = wo.asset
    if wo.type == WoType.PM:
        asset.last_pm_on = as_of
        fields = ["last_pm_on", "updated_at"]
        # Slice 26: a waiting device's PM clock starts at its pass (and no door opens a PM on one). Slice 29: a temporary device has no
        # PM clock of ours (no door opens a PM on one either; its owner's PM date is what Cadence keeps).
        if not asset.awaiting_inspection and not owner_maintains(asset):
            # Slice 27: from the due date for a PM done late but inside its window (pm_schedule_anchor), else from the day it was done
            asset.next_pm_on = next_pm_after(wo, asset, as_of)
            fields.append("next_pm_on")
        asset.save(update_fields=fields)
    elif wo.type == WoType.INSPECTION and wo.inspection_result == InspectionResult.PASSED:
        # Slice 26: the pass clears the waiting flag, starts the PM clock, and puts the device in service unless a repair holds it out
        # (nothing moves when the device no longer waits). Imported here: equipment.services imports this module in its functions.
        from apps.equipment.services import pass_incoming_inspection

        pass_incoming_inspection(asset, wo, by=by, on=as_of)
    elif wo.type == WoType.REPAIR and returns_device(wo):
        asset.status = AssetStatus.IN_SERVICE
        asset.save(update_fields=["status", "updated_at"])


def returns_device(wo: WorkOrder) -> bool:
    """Whether completing the repair `wo` now puts its device back in service (_on_completed; completion's status note reads it first).
    Back in service only when this repair is why it was out: tagged out with the request (the portal's checkbox), or marked in repair.
    A device out of service for another reason (quarantined by hand) stays out until someone returns it from its drawer, and one waiting
    for its incoming inspection (slice 26) until the inspection passes. And only when no other open repair holds it out too (a second
    tagged-out request, or the repair a failed PM opened: apps.workorders.completion): the last of them returns it. Slice 28: never a
    device held as evidence (the investigation's own completion included): only the incident's release returns it. Slice 30: never
    after a repair completed as not returned to use (RepairCheck.NOT_RETURNED): reopening it and completing it as checked does."""
    if wo.type != WoType.REPAIR or wo.repair_check == RepairCheck.NOT_RETURNED:
        return False
    asset = wo.asset
    if not (asset.status == AssetStatus.IN_REPAIR or (asset.status == AssetStatus.OUT_OF_SERVICE and wo.tagged_out)):
        return False
    return not holding_repairs(asset, exclude=wo).exists() and not _still_waiting(asset) and not _still_held(asset)


@transaction.atomic
def assign(wo: WorkOrder, technician=None, vendor_name: str = "", by=None) -> WorkOrder:
    """Assign an open work order to a technician or to vendor service. Flags credential overrides in the status history. Slice 31: only
    an open one (a completed or closed work order keeps who did it; the drawer refused already, now the API does too), and the
    vendor's engineer it names, and the vendor's report number, are cleared when the work moves in-house or to another vendor
    (apps.vendors.rules.vendor_key): they were that vendor's visit (review fix: a re-completion keeps a number left out). Slice 33:
    so is a finding of "not stated by the vendor" (a reopened repair's): that vendor's report was silent, not the next one's. Slice 35:
    the test equipment it recorded (a reopened work order's) is cleared when it moves to a vendor, from in-house or another vendor
    (testequipment.services.clear_for_vendor: the items' rows locked after this row, each use's history "Test equipment cleared:
    assigned to <vendor>")."""
    from apps.credentials.services import qualification
    from apps.vendors.rules import vendor_key

    # Slice 31 review fix: the status and the engineer as stored, on the row locked (the UPDATE below locks it anyway, and nothing else
    # is locked in between): a caller's copy read before a completion or a Change committed would move completed work, or keep an
    # engineer on work that moved.
    stored = (WorkOrder.objects.select_for_update().filter(pk=wo.pk)
              .values("status", "vendor_service", "vendor_name", "vendor_engineer_id", "checks_reference", "repair_finding").first())
    if stored is None:
        raise ValidationError("Choose a work order from this facility.")
    if stored["status"] not in OPEN_STATUSES:
        raise ValidationError("Only open work orders can be reassigned.")
    wo.status, wo.vendor_engineer_id, wo.checks_reference = stored["status"], stored["vendor_engineer_id"], stored["checks_reference"]
    wo.repair_finding = stored["repair_finding"]
    note = ""
    vendor_name = (vendor_name or "").strip()  # a stray space from the API would keep it from its company's share (apps.workorders.scoping)
    if len(vendor_name) > VENDOR_NAME_MAX:
        raise ValidationError({"vendor_name": f"Keep the vendor's name to {VENDOR_NAME_MAX} characters."})
    before = vendor_key(stored["vendor_name"]) if stored["vendor_service"] else None
    if vendor_name:
        wo.vendor_service, wo.vendor_name, wo.assigned_to = True, vendor_name, None
        note = f"Assigned to vendor: {vendor_name}"
    elif technician is not None:
        wo.vendor_service, wo.vendor_name, wo.assigned_to = False, "", technician
        q = qualification(technician, wo.asset)
        note = f"Assigned to {technician.name}" + ("" if q.ok else " (override: not credentialed for this device)")
    fields = ["vendor_service", "vendor_name", "assigned_to", "updated_at"]
    if before is not None and (not wo.vendor_service or vendor_key(wo.vendor_name) != before):  # the work moved off that vendor
        if wo.vendor_engineer_id:
            wo.vendor_engineer = None  # slice 31: that engineer did not do this vendor's (or this technician's) work
            fields.append("vendor_engineer")
        if wo.checks_reference:
            wo.checks_reference = ""  # review fix: nor is the earlier vendor's report this one's (completion keeps a number left out)
            fields.append("checks_reference")
        if wo.repair_finding == RepairFinding.NOT_STATED:
            wo.repair_finding = ""  # slice 33: that vendor's report named no cause; the work's next completion records what was found
            fields.append("repair_finding")
    wo.save(update_fields=fields)
    if wo.vendor_service and (before is None or vendor_key(wo.vendor_name) != before):  # slice 35: the work moved to a vendor
        from apps.testequipment.services import clear_for_vendor  # apps.testequipment imports this app

        clear_for_vendor(wo, wo.vendor_name, by=by)
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


# --- the vendor's engineer (slice 31) ---------------------------------------------------------------

@transaction.atomic
def set_vendor_engineer(wo: WorkOrder, engineer=None, *, engineer_name: str = "", by=None) -> WorkOrder:
    """Name the vendor's engineer responsible for `wo` (vendor service, not cancelled), as their report names them, once it arrives: an
    engineer or its id (None clears it: not named), or a typed name (the engineer of the work order's vendor with that name, created
    when there is none). Through apps.vendors.services.engineer_choice, as completing does. Never a scoped user's (a vendor account
    names its engineer when it completes the work, and never rewrites it after). The caller checks the level
    (permissions.can_set_vendor_engineer: Edit; Approve once closed). Audited in the work order's history ("Field engineer")."""
    from apps.vendors import services as vendor_services

    from . import scoping

    if scoping.is_scoped(by):
        raise ValidationError({"vendor_engineer": "The facility's CE staff record who did the vendor's work after it is completed."})
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if locked.status == WoStatus.CANCELLED:
        raise ValidationError({"vendor_engineer": "A cancelled work order names no engineer."})
    if not locked.vendor_service:  # review fix: clearing in-house work was a silent no-op; it names no engineer either way
        raise ValidationError({"vendor_engineer": vendor_services.ENGINEER_ONLY})
    errors: dict = {}
    choice = vendor_services.engineer_choice(locked, None if engineer in (None, "") else engineer, engineer_name, by=by, errors=errors)
    if errors:
        raise ValidationError(errors)
    locked._engineer_added = f"Added as the field engineer of {locked.number}"  # a typed name's new engineer (not at completion)
    engineer = vendor_services.resolve_choice(choice, locked, by=by)
    if engineer is not vendor_services.KEEP and (engineer.pk if engineer else None) != locked.vendor_engineer_id:
        locked.vendor_engineer = engineer
        # review fix: the history's reason column holds 100 characters (a long name was a database error on PostgreSQL)
        reason_max = WorkOrder.history.model._meta.get_field("history_change_reason").max_length
        locked._change_reason = (f"Field engineer: {engineer.name}" if engineer else "Field engineer cleared")[:reason_max]
        try:
            locked.save(update_fields=["vendor_engineer", "updated_at"])
        finally:  # simple_history reads it on every save: never let it reach a later one of this copy
            locked.__dict__.pop("_change_reason", None)
    wo.vendor_engineer = locked.vendor_engineer
    return locked


# --- what a repair found (slice 33) -----------------------------------------------------------------

def _reason_max() -> int:
    return WorkOrder.history.model._meta.get_field("history_change_reason").max_length  # 100: longer is a database error on PostgreSQL


def _save_finding(wo: WorkOrder, value: str, reason: str, by) -> None:
    """Save `value` as what the repair `wo` (its row locked) found, with `reason` in its history (cut to the column), as `by`."""
    wo.repair_finding = value
    wo._change_reason = reason[:_reason_max()]
    if by is not None:
        wo._history_user = by
    try:
        wo.save(update_fields=["repair_finding", "updated_at"])
    finally:  # simple_history reads them on every save: never let them reach a later one of this copy
        wo.__dict__.pop("_change_reason", None)
        wo.__dict__.pop("_history_user", None)


@transaction.atomic
def set_repair_finding(wo: WorkOrder, finding: str, *, by=None) -> WorkOrder:
    """Record or correct what the repair `wo` found (RepairFinding; apps.workorders.findings) once it is completed or closed, one
    completed before findings were recorded (or imported) included. Never on an open, cancelled, or other work order, never blank (a
    finding is corrected, never taken away: completing requires one), never a scoped user's (the facility's record: a vendor's or a
    requester's account records one only when completing), and only one of findings.choices_for(the repair as locked, `by`): an
    incident's investigation never use error, a failed PM's repair never "PM could have caught it", not stated by the vendor only on
    vendor service. Refusals keyed repair_finding in findings' words. The caller checks the level (permissions.can_set_repair_finding:
    Edit; Approve once closed). Locks as completing does: the device's open work and this one in number order (lock_work), then reads
    the row. Audited in the work order's history: "What was found: <label>", or "Recorded later: <label>" when none was recorded,
    within the history's 100 characters. Returns the row; `wo.repair_finding` says the same."""
    from . import findings, scoping
    from .completion import finding_refusal  # completion imports this module

    if scoping.is_scoped(by):
        raise ValidationError({"repair_finding": "The facility's CE staff record what a repair found after it is completed."})
    value = str(finding if finding is not None else "").strip()
    if not value:
        raise ValidationError({"repair_finding": findings.REQUIRED})
    if value not in RepairFinding.values:
        raise ValidationError({"repair_finding": findings.NOT_OFFERED})
    lock_work(wo.asset_id, wo.pk)
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if locked.type != WoType.REPAIR:
        raise ValidationError({"repair_finding": findings.NOT_A_REPAIR})
    if locked.status == WoStatus.CANCELLED:
        raise ValidationError({"repair_finding": f"{locked.number} was cancelled: it records no finding."})
    if locked.status not in (WoStatus.COMPLETED, WoStatus.CLOSED):
        raise ValidationError({"repair_finding": f"{locked.number} is not completed yet: record what was found when completing it."})
    investigation, from_pm = findings.is_investigation(locked), findings.found_on_pm(locked)
    if value not in findings.choices_for(locked, by, investigation=investigation, from_pm=from_pm):
        raise ValidationError({"repair_finding": finding_refusal(locked, value, by, investigation=investigation, from_pm=from_pm)})
    if value != locked.repair_finding:
        label = RepairFinding(value).label
        _save_finding(locked, value, f"What was found: {label}" if locked.repair_finding else f"Recorded later: {label}", by)
    wo.repair_finding = locked.repair_finding
    return locked


# --- the test equipment used (slice 35) --------------------------------------------------------------

@transaction.atomic
def set_test_equipment(wo: WorkOrder, ids, *, by, today: date | None = None) -> WorkOrder:
    """Record or correct which test equipment `wo` recorded, once it is completed or closed: `ids` a list of item ids (text or UUID)
    that replaces the set as a diff, [] clears (KEEP changes nothing). The rules of completing (testequipment.services.check_uses), but a
    new item need only not have been retired before the work's day: it is judged on that day, and its standing flags it. Never a scoped
    user's; Work orders Edit, Approve once closed (permissions.can_set_test_equipment, checked here on the locked row for a user given
    as `by`). Locks as completing does: lock_work, this row, then the items' rows. Refusals keyed test_equipment (a cancelled one: "was
    cancelled: it records no test equipment"). A use added is recorded on the work order's completed_on (its used_on); those that stay
    keep their own day. Each use added or taken off is a history row of its own ("Recorded after completion", "Taken off after
    completion"). Returns the row."""
    from django.core.exceptions import PermissionDenied

    from apps.testequipment import services as te

    from . import permissions, scoping

    today = today or timezone.localdate()
    if scoping.is_scoped(by):
        raise ValidationError({"test_equipment": te.SCOPED})
    lock_work(wo.asset_id, wo.pk)
    locked = WorkOrder.objects.select_for_update().get(pk=wo.pk)
    if locked.status == WoStatus.CANCELLED:  # review fix: the web's words (Change's blocker)
        raise ValidationError({"test_equipment": te.CANCELLED.format(number=locked.number)})
    if locked.status not in (WoStatus.COMPLETED, WoStatus.CLOSED):
        raise ValidationError({"test_equipment": te.NOT_DONE.format(number=locked.number)})
    if by is not None and not permissions.can_set_test_equipment(by, locked):
        raise PermissionDenied(te.SET_PERMISSION)
    errors: dict = {}
    change = te.check_uses(locked, ids, by=by, today=today, errors=errors, completing=False)
    if errors:
        raise ValidationError(errors)
    if change is not te.KEEP:
        te.apply_uses(locked, change, by=by, completing=False)
    return locked


USE_ERROR_CLEARED = "What was found cleared: an incident's investigation records no use error"


def clear_use_error(wo: WorkOrder, *, by=None) -> bool:
    """An open repair an incident adopts as its investigation (apps.incidents.services.record_incident, which holds the row locked,
    after lock_work) never keeps a use error (a repair completed as one and reopened): cleared, audited (USE_ERROR_CLEARED), so its
    next completion records what was found on the device. True when one was cleared."""
    if wo.repair_finding != RepairFinding.USE_ERROR:
        return False
    _save_finding(wo, "", USE_ERROR_CLEARED, by)
    return True


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
