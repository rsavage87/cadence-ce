"""
Slice 28, the hold's guards (wave 1b): while an open incident holds a device as evidence, no work order on it but the incident's
investigation starts or completes (workorders.services.change_status on the locked row, completion.blocker before anything starts),
whatever its type, through the services, the drawer's buttons, Mark completed, and the API's transition alike; the investigation's own
completion leaves the device held and out of service; cancelling, assigning, taking, notes, time, parts, waiting on parts, and opening
work (PM generation included) stay allowed; a held device waiting for its incoming inspection is never put in use before it, and its
pass keeps it out; the devices import keeps a held device's status with a note in its own words; Auto-assign week and the PM plan put
a held device's PM on nobody's plate; My work marks held work; history imported from another system still comes in; and the same
under PostgreSQL's row-level security.
"""
import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from incident_fixtures import held_incident, make_incident
from incoming_fixtures import INCOMING_ALL_PASS, waiting_device
from pg_helpers import as_app_role, needs_postgres

from apps.credentials.models import Technician
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, UseBeforeInspection
from apps.imports import services as imports
from apps.imports.kinds.devices import HELD_NOTE
from apps.imports.models import ImportRun
from apps.incidents.models import IncidentHold
from apps.pm import schedule as sch
from apps.pm.services import assign_week, create_pm_work_orders_for_day, generate_pm_work_orders, week_assignment_preview
from apps.workorders import costs, inspections, my_work
from apps.workorders.completion import blocker, complete_work_order
from apps.workorders.models import InspectionResult, PmResult, Priority, Source, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import (
    add_note,
    assign,
    change_status,
    create_work_order,
    held_work_message,
    hold_blocker,
    take,
    with_held,
)

HX = {"HTTP_HX_REQUEST": "true"}
API = "/api/v1/"
HELD = "is held as evidence for an incident investigation: only its investigation work order may be started or completed"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def work(asset, type=WoType.REPAIR, *, tech=None, **extra):
    """A work order on `asset`, opened a week ago, assigned to `tech` when given."""
    return create_work_order(asset=asset, type=type, priority=Priority.NORMAL, problem="Work", assigned_to=tech,
                             opened_on=timezone.localdate() - timedelta(days=7), **extra)


def moves(wo) -> list:
    return list(WorkOrderStatusHistory.objects.filter(work_order=wo).order_by("created_at", "id").values_list("from_status", "to_status"))


def post_json(client, url, body=None):
    return client.post(url, body or {}, content_type="application/json")


# --- the guard -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("type_", [WoType.PM, WoType.REPAIR, WoType.INSPECTION, WoType.RECALL])
def test_no_work_but_the_investigation_starts_or_completes_on_a_held_device(vent, techs, type_):
    waiting = work(vent, type_, tech=techs["dana"])
    started = work(vent, type_, tech=techs["dana"])
    change_status(started, WoStatus.IN_PROGRESS)  # started before the hold
    parts = work(vent, type_, tech=techs["dana"])
    change_status(parts, WoStatus.AWAITING_PARTS)
    held_incident(vent)
    vent.refresh_from_db()
    message = held_work_message(vent)
    assert message == f"CE-10001 {HELD}."
    for wo, to in ((waiting, WoStatus.IN_PROGRESS), (started, WoStatus.COMPLETED), (parts, WoStatus.IN_PROGRESS)):
        with pytest.raises(ValidationError) as e:
            change_status(wo, to)
        assert e.value.messages == [message]  # no incident number: whoever tries may not see incidents
    # Mark completed says so before anything starts: a PM or an inspection still open is not started first
    for wo in (waiting, started):
        assert blocker(wo) == hold_blocker(wo) == message
        with pytest.raises(ValidationError, match=HELD):
            complete_work_order(wo, resolution="Done", pm_result=PmResult.PASS if type_ == WoType.PM else "")
    for wo, status in ((waiting, WoStatus.OPEN), (started, WoStatus.IN_PROGRESS), (parts, WoStatus.AWAITING_PARTS)):
        wo.refresh_from_db()
        assert wo.status == status and wo.completed_on is None
    assert moves(waiting) == [("", WoStatus.OPEN)]
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE and vent.last_pm_on is None


def test_a_completed_work_order_is_not_reopened_while_the_device_is_held(vent, techs):
    wo = work(vent, tech=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    complete_work_order(wo, resolution="Replaced the flow sensor")
    held_incident(vent)
    with pytest.raises(ValidationError, match=HELD):
        change_status(wo, WoStatus.IN_PROGRESS)  # reopening is work on the device too
    assert blocker(wo) == f"{wo.number} is already completed."  # a finished one says what it is, not the hold
    change_status(wo, WoStatus.CLOSED)  # closing records the work done before the hold


def test_the_investigation_starts_and_completes_and_leaves_the_device_held_and_out(vent, techs):
    incident = held_incident(vent)
    investigation = incident.work_order
    assert hold_blocker(investigation) == ""
    assign(investigation, technician=techs["dana"])
    change_status(investigation, WoStatus.IN_PROGRESS)
    done = complete_work_order(investigation, resolution="Tested to the manufacturer's specification; no fault found")
    vent.refresh_from_db()
    assert investigation.status == WoStatus.COMPLETED and not done.device_changed
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE  # only the incident's release returns it
    change_status(investigation, WoStatus.IN_PROGRESS)  # and it may be reopened to look again
    eq.clear_incident_hold(vent)  # the release (apps.incidents.services.release decides the status)
    other = work(vent, tech=techs["dana"])
    change_status(other, WoStatus.IN_PROGRESS)  # released: work goes on as before


def test_an_adopted_repair_completes_without_returning_the_device_even_from_an_older_copy(vent, techs):
    """The nurse's tagged-out request, adopted as the investigation: completing it never puts the device back in service, even when
    the caller's work order (and its device) were read before the hold."""
    request = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarmed and stopped", source=Source.PORTAL,
                                tag_out=True, assigned_to=techs["dana"])
    change_status(request, WoStatus.IN_PROGRESS)
    stale = WorkOrder.objects.select_related("asset").get(pk=request.pk)
    assert not stale.asset.incident_hold
    held_incident(vent, work_order=request, open_work_order=False)
    change_status(stale, WoStatus.COMPLETED)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE


def test_two_incidents_holding_one_device_each_keep_their_investigation(vent, techs):
    first, second = held_incident(vent), held_incident(vent)
    for incident in (first, second):
        assign(incident.work_order, technician=techs["dana"])
        change_status(incident.work_order, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError, match=HELD):
        change_status(work(vent, tech=techs["dana"]), WoStatus.IN_PROGRESS)


def test_another_part_of_the_system_held_by_the_incident_has_no_investigation_of_its_own(vent, pump, techs):
    """A pump held with the ventilator on one incident: the investigation is on the ventilator, so nothing on the pump starts (by
    identity: the incident's investigation is not the pump's work order)."""
    incident = held_incident(vent)
    pump.refresh_from_db()
    IncidentHold.objects.create(incident=incident, asset=pump, held_on=incident.occurred_on, status_before=pump.status)
    eq.set_incident_hold(pump)
    repair = work(pump, tech=techs["dana"])
    with pytest.raises(ValidationError, match=f"CE-10002 {HELD}"):
        change_status(repair, WoStatus.IN_PROGRESS)
    assert with_held(WorkOrder.objects.filter(pk__in=[repair.pk, incident.work_order_id])).filter(held=True).get() == repair


def test_cancelling_assigning_taking_notes_time_parts_and_opening_work_stay_allowed(vent, techs, make_user, today):
    held_incident(vent)
    user = make_user("technician")
    Technician.objects.filter(pk=techs["dana"].pk).update(user=user)
    repair = work(vent)
    take(repair, by=user)  # unassigned in-house work on a device Dana is credentialed for
    assert repair.assigned_to_id == techs["dana"].pk
    assign(repair, technician=techs["tom"])
    add_note(repair, "Waiting for the incident's release")
    change_status(repair, WoStatus.AWAITING_PARTS)
    costs.add_labor(repair, hours="0.5", worked_on=today, by=user, today=today)
    costs.add_part(repair, description="Flow sensor", quantity=1, unit_cost="120.00", by=user)
    change_status(repair, WoStatus.CANCELLED)
    pm = work(vent, WoType.PM)
    change_status(pm, WoStatus.CANCELLED)
    inspection = inspections.open_for(vent, today=today)
    assert inspection.status == WoStatus.OPEN
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE


# --- the screens and the API -----------------------------------------------------------------------------------------------------

def test_the_drawers_start_and_mark_completed_refuse_held_work(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    repair, pm = work(vent, tech=techs["dana"]), work(vent, WoType.PM, tech=techs["dana"])
    incident = held_incident(vent)
    r = client.post(f"/work-orders/{repair.number}/status/", {"to": "in_progress"}, **HX)
    assert r.status_code == 200 and HELD in r["HX-Trigger"]
    r = client.get(f"/work-orders/{pm.number}/complete/", **HX)
    assert HELD in r.content.decode()
    r = client.post(f"/work-orders/{pm.number}/complete/", {"resolution": "Done", "pm_result": PmResult.PASS}, **HX)
    assert HELD in r.content.decode()
    repair.refresh_from_db()
    pm.refresh_from_db()
    assert repair.status == pm.status == WoStatus.OPEN
    assign(incident.work_order, technician=techs["dana"])
    client.post(f"/work-orders/{incident.work_order.number}/status/", {"to": "in_progress"}, **HX)
    client.post(f"/work-orders/{incident.work_order.number}/complete/", {"resolution": "No fault found"}, **HX)
    incident.work_order.refresh_from_db()
    assert incident.work_order.status == WoStatus.COMPLETED


def test_the_apis_transition_refuses_held_work_and_moves_the_investigation(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    repair = work(vent, tech=techs["dana"])
    recall = work(vent, WoType.RECALL, tech=techs["dana"])
    change_status(recall, WoStatus.IN_PROGRESS)
    incident = held_incident(vent)
    r = post_json(client, f"{API}work-orders/{repair.pk}/transition/", {"status": "in_progress"})
    assert r.status_code == 400 and HELD in r.json()["detail"]
    r = post_json(client, f"{API}work-orders/{recall.pk}/transition/", {"status": "completed", "resolution": "Software updated"})
    assert r.status_code == 400 and HELD in r.json()["detail"]
    r = post_json(client, f"{API}work-orders/{repair.pk}/transition/", {"status": "cancelled"})
    assert r.status_code == 200
    investigation = incident.work_order
    assign(investigation, technician=techs["dana"])
    r = post_json(client, f"{API}work-orders/{investigation.pk}/transition/", {"status": "in_progress"})
    assert r.status_code == 200
    r = post_json(client, f"{API}work-orders/{investigation.pk}/transition/", {"status": "completed", "resolution": "No fault found"})
    assert r.status_code == 200 and r.json()["status"] == WoStatus.COMPLETED
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE


# --- incoming inspections ---------------------------------------------------------------------------------------------------------

def test_a_held_device_waiting_for_its_inspection_is_never_put_in_use_before_it(client, vent_model, make_user, today):
    device = waiting_device(vent_model, today=today)
    make_incident(device, open_work_order=False)  # held, with no repair of its own holding it out
    with pytest.raises(ValidationError) as e:
        eq.use_before_inspection(device, UseBeforeInspection.EMERGENCY)
    assert e.value.messages == [eq.held_message(device)]
    client.force_login(make_user("director"))
    r = post_json(client, f"{API}assets/{device.pk}/use-before-inspection/", {"reason": UseBeforeInspection.EMERGENCY})
    assert r.status_code == 400 and "held as evidence" in str(r.json())
    device.refresh_from_db()
    assert device.status == AssetStatus.OUT_OF_SERVICE and device.awaiting_inspection
    inspection = inspections.open_inspection(device)
    with pytest.raises(ValidationError, match=HELD):
        complete_work_order(inspection, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS)


def test_a_pass_recorded_on_a_held_device_keeps_it_out_of_service(vent_model, today):
    """pass_incoming_inspection, the flag's one writer: the pass is recorded, the PM clock starts, and the device stays out until the
    incident releases it."""
    device = waiting_device(vent_model, today=today)
    make_incident(device, open_work_order=False)
    eq.pass_incoming_inspection(device, inspections.open_inspection(device), on=today)
    device.refresh_from_db()
    assert not device.awaiting_inspection and device.next_pm_on is not None
    assert device.incident_hold and device.status == AssetStatus.OUT_OF_SERVICE


# --- the devices import -------------------------------------------------------------------------------------------------------------

def _run(user, kind, data: bytes, name: str):
    run = imports.upload(user, kind, name, data)
    run = imports.confirm_columns(run, user, imports.mapping_of(run))
    for _ in range(2):  # the check, then the import it promised
        run = imports.process(run, user)
        while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
            run = imports.process(run, user)
        if run.status == ImportRun.Status.CHECKED:
            run = imports.start_import(run, user)
    assert run.status == ImportRun.Status.IMPORTED
    return run


def test_the_devices_import_keeps_a_held_devices_status_with_its_own_note(vent, pump, make_user):
    held_incident(vent)
    data = "\r\n".join(["Tag,Manufacturer,Model,Room,Status", "CE-10001,,,7,In service", "CE-10002,,,8,Out of service", ""]).encode()
    run = _run(make_user("director"), "devices", data, "inventory.csv")
    notes = {g["note"]: g["keys"] for g in imports.problem_groups(run)}
    assert notes == {HELD_NOTE: ["CE-10001"]}
    assert "open work orders" not in HELD_NOTE and "IN-" not in HELD_NOTE
    vent.refresh_from_db()
    pump.refresh_from_db()
    assert vent.room == "7" and vent.status == AssetStatus.OUT_OF_SERVICE and vent.incident_hold  # its other changes still go in
    assert pump.room == "8" and pump.status == AssetStatus.OUT_OF_SERVICE


# --- history imported from another system --------------------------------------------------------------------------------------

def test_work_order_history_still_imports_onto_a_held_device(vent, make_user, today):
    held_incident(vent)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["WO #", "Control No.", "Work Type", "Status", "Date Opened", "Date Completed"])
    writer.writerow(["10423", "CE-10001", "Repair", "Completed", "2024-03-04", "2024-03-06"])
    writer.writerow(["10424", "CE-10001", "Repair", "In progress", str(today - timedelta(days=3)), ""])
    run = _run(make_user("director"), "work_orders", buf.getvalue().encode(), "work-orders.csv")
    assert run.counts == {"create": 2}
    done, under_way = WorkOrder.objects.get(legacy_number="10423"), WorkOrder.objects.get(legacy_number="10424")
    assert (done.source, done.status, under_way.status) == (Source.IMPORTED, WoStatus.COMPLETED, WoStatus.IN_PROGRESS)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    with pytest.raises(ValidationError, match=HELD):  # live work on it waits for the release like any other
        change_status(under_way, WoStatus.COMPLETED)


# --- the PM plan ----------------------------------------------------------------------------------------------------------------

def test_auto_assign_week_and_the_plan_leave_a_held_devices_pm_on_nobodys_plate(vent, pump, techs, today):
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today + timedelta(days=1))
    Asset.objects.filter(pk=pump.pk).update(next_pm_on=today + timedelta(days=2))
    held_incident(vent)
    w = week_assignment_preview(today)
    assert [(h["asset"].tag, h["has_open_pm"]) for h in w.on_hold] == [("CE-10001", False)]
    assert not w.uncovered and [(s.technician.name, s.new) for s in w.shares] == [("Dana Whitfield", 1)]
    assert sch.week_plan(today)["suggested"][vent.pk] is None
    assert [r["technician"] for r in sch.day_plan(today + timedelta(days=1), today)["rows"]] == [None]
    assign_week(today=today)
    assert not WorkOrder.objects.filter(asset=vent, type=WoType.PM).exists()  # neither created nor assigned here
    pump_pm = WorkOrder.objects.get(asset=pump, type=WoType.PM)
    assert pump_pm.assigned_to == techs["dana"]
    # PM generation is unchanged: the held device's PM is opened, and simply cannot start
    batch = create_pm_work_orders_for_day(today + timedelta(days=1), today=today)
    assert (batch.created, batch.assigned) == (1, 0)
    vent_pm = WorkOrder.objects.get(asset=vent, type=WoType.PM)
    assert vent_pm.assigned_to is None and vent_pm.status == WoStatus.OPEN
    assign_week(today=today)
    vent_pm.refresh_from_db()
    assert vent_pm.assigned_to is None
    assert [(h["asset"].tag, h["has_open_pm"]) for h in week_assignment_preview(today).on_hold] == [("CE-10001", True)]
    plates = {load.technician.name: (load.pm_count, load.pm_hours) for load in sch.workload_next_7_days(today)}
    assert plates["Dana Whitfield"] == (1, Decimal("1"))  # the pump's PM only
    # Released: the next Auto-assign week puts it on a plate
    eq.clear_incident_hold(vent)
    assert week_assignment_preview(today).on_hold == []
    assign_week(today=today)
    vent_pm.refresh_from_db()
    assert vent_pm.assigned_to == techs["dana"]


def test_the_nightly_generation_opens_a_held_devices_pm(vent, today):
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today + timedelta(days=3))
    held_incident(vent)
    assert generate_pm_work_orders(as_of=today, lead_days=14) == 1
    pm = WorkOrder.objects.get(asset=vent, type=WoType.PM)
    assert pm.status == WoStatus.OPEN and pm.assigned_to is None
    with pytest.raises(ValidationError, match=HELD):
        complete_work_order(pm, pm_result=PmResult.PASS)


# --- My work ------------------------------------------------------------------------------------------------------------------------

def test_my_work_marks_held_work_and_keeps_the_investigations_moves(vent, pump, techs, make_user, today):
    user = make_user("technician")
    Technician.objects.filter(pk=techs["dana"].pk).update(user=user)
    dana = techs["dana"]
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.HIGH, problem="PM", assigned_to=dana, opened_on=today, due_on=today)
    pump_repair = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Occlusion alarm", assigned_to=dana)
    unassigned = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    incident = held_incident(vent)
    assign(incident.work_order, technician=dana)
    groups = my_work.groups(user, today)
    held = {wo.number: wo.held for wo in [*groups.repairs, *groups.pms_today]}
    assert held == {pm.number: True, incident.work_order.number: False, pump_repair.number: False}
    assert [(wo.number, wo.held) for wo in groups.takeable if wo.asset_id == vent.pk] == [(unassigned.number, True)]
    assert {wo.number for wo in my_work.due(user, today)} >= {pm.number}  # still theirs, waiting for the release


# --- locks, on PostgreSQL ---------------------------------------------------------------------------------------------------------

@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_repair_completed_while_its_device_is_being_held_waits_for_the_hold_and_is_refused(tenant):
    """The hold takes the device's row; a tagged-out repair completed at that moment reads the hold on the same row, locked, so it
    waits for the hold to commit and is refused. (Read without the lock, it would see no hold, and its return to service would land
    on the device after the hold: held and in service.)"""
    import threading
    import time

    from django.db import connection, transaction

    from apps.equipment.models import Department, DeviceModel, RiskClass
    from apps.tenants.context import tenant_context

    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                           risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        device = Asset.objects.create(tag="CE-20001", device_model=model, department=Department.objects.create(name="ICU"))
        dana = Technician.objects.create(name="Dana Whitfield")
        repair = create_work_order(asset=device, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarm", tag_out=True, assigned_to=dana)
        change_status(repair, WoStatus.IN_PROGRESS)
    barrier = threading.Barrier(2, timeout=20)
    errors = []

    def run(call):
        try:
            with tenant_context(tenant):
                barrier.wait()
                call()
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            connection.close()

    def hold():
        with transaction.atomic():
            make_incident(Asset.objects.get(pk=device.pk))
            time.sleep(1.5)  # the device's row held: the completion is trying meanwhile

    def complete():
        time.sleep(0.5)  # the hold takes the device's row first
        complete_work_order(WorkOrder.objects.get(pk=repair.pk), resolution="Replaced the flow sensor")

    threads = [threading.Thread(target=run, args=(call,)) for call in (hold, complete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(errors) == 1 and isinstance(errors[0], ValidationError) and errors[0].messages == [f"CE-20001 {HELD}."], errors
    with tenant_context(tenant):
        device.refresh_from_db()
        assert device.incident_hold and device.status == AssetStatus.OUT_OF_SERVICE
        assert WorkOrder.objects.get(pk=repair.pk).status == WoStatus.IN_PROGRESS


@needs_postgres
def test_a_failed_pm_takes_its_repairs_number_before_the_devices_row(vent, techs):
    """Completing a failed PM opens its repair before the PM's start and completion lock the device's row to read its hold: the
    facility's numbering first, then the device, as a tagged-out request takes them, so the two never wait in a circle."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.core.models import Sequence

    pm = work(vent, WoType.PM, tech=techs["dana"])
    with CaptureQueriesContext(connection) as q:
        done = complete_work_order(pm, pm_result=PmResult.FAIL, resolution="Flow sensor failed calibration", tag_out=True)
    sqls = [x["sql"] for x in q.captured_queries]
    numbering = next(i for i, s in enumerate(sqls) if f'"{Sequence._meta.db_table}"' in s and "FOR UPDATE" in s)
    device = next(i for i, s in enumerate(sqls) if f'"{Asset._meta.db_table}"' in s and ("FOR UPDATE" in s or s.startswith("UPDATE")))
    assert numbering < device
    assert done.started and done.follow_up is not None and done.tagged_out
    assert moves(pm) == [("", WoStatus.OPEN), (WoStatus.OPEN, WoStatus.IN_PROGRESS), (WoStatus.IN_PROGRESS, WoStatus.COMPLETED)]


# --- under the policies ---------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_guard_reads_the_hold_under_the_policies(vent, techs):
    repair = work(vent, tech=techs["dana"])
    incident = held_incident(vent)
    assign(incident.work_order, technician=techs["dana"])
    as_app_role()
    with pytest.raises(ValidationError, match=HELD):
        change_status(repair, WoStatus.IN_PROGRESS)
    assert with_held(WorkOrder.objects.filter(asset=vent)).filter(held=True).get() == repair
    change_status(incident.work_order, WoStatus.IN_PROGRESS)
    complete_work_order(incident.work_order, resolution="No fault found")
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
