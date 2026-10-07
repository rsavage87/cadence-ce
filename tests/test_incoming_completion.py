"""
Slice 26, completing an incoming inspection (apps/workorders/completion.py): its checklist (the model's procedure, else the incoming
checklist), recorded all or not at all; the result (required while the device waits, optional and moving nothing otherwise, never on
another type); Passed with a failed step refused; the default resolution; one visit; a pass that moves the device; a fail that opens
(or reuses) the re-inspection, never a repair, and takes a device in use out; reopening; a vendor's pass; two passes at once; one
transaction. The device's side is tests/test_incoming_services.py.
"""
import threading
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone
from incoming_fixtures import INCOMING_ALL_PASS, failed_device, in_use_device, incoming_results, passed_device, waiting_device
from pg_helpers import needs_postgres

from apps.equipment.models import Asset, AssetStatus, UseBeforeInspection
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.workorders import completion, inspections
from apps.workorders import services as wo_services
from apps.workorders.completion import checklist_of, checklist_signature, complete_work_order, procedure_for
from apps.workorders.models import InspectionResult, PmResult, WorkOrder, WorkOrderNote, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

S = AssetStatus
PASSED, FAILED = InspectionResult.PASSED, InspectionResult.FAILED
CHECKLIST = ["Inspect and clean", {"text": "Leakage current", "measure": "µA, limit 100"}, "Alarm check"]


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def errors_of(excinfo) -> dict:
    return {k: " ".join(v) for k, v in excinfo.value.message_dict.items()}


def with_procedure(model, checklist=CHECKLIST, code="HM-G5-PM6"):
    proc = PmProcedure.objects.create(code=code, name="Hamilton G5 6-month PM", checklist=checklist, revision="C")
    model.pm_procedure = proc
    model.save()
    return proc


def inspection_of(asset, technician=None, vendor=""):
    wo = inspections.open_inspection(asset)
    if technician is not None or vendor:
        assign(wo, technician=technician, vendor_name=vendor)
    return wo


# --- the checklist ------------------------------------------------------------------------------------------------------------------

def test_an_inspection_uses_the_models_procedure_else_the_incoming_checklist(ctx, today, vent_model):
    a = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(a)
    assert procedure_for(wo) is inspections.INCOMING and checklist_of(procedure_for(wo)) == inspections.INCOMING_CHECKLIST
    empty = with_procedure(vent_model, checklist=[], code="EMPTY")
    wo = WorkOrder.objects.get(pk=wo.pk)
    assert vent_model.pm_procedure == empty and procedure_for(wo) is inspections.INCOMING  # a procedure with no steps: the incoming checklist
    proc = with_procedure(vent_model)
    wo = WorkOrder.objects.get(pk=wo.pk)
    assert procedure_for(wo) == proc and checklist_of(procedure_for(wo)) == checklist_of(proc)
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority="normal", problem="Dented")
    assert procedure_for(repair) is None


def test_the_incoming_checklist_is_recorded_with_its_leakage_reading(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    signature = checklist_signature(checklist_of(procedure_for(wo)))
    done = complete_work_order(wo, inspection_result=PASSED, results=incoming_results(reading="38"), signature=signature)
    assert wo.status == WoStatus.COMPLETED and wo.inspection_result == PASSED and wo.pm_result == ""
    assert [(s["text"], s["result"], s["reading"]) for s in wo.checklist_results] == [
        (text, "pass", "38" if measure else "") for text, measure in inspections.INCOMING_CHECKLIST]
    assert wo.checklist_results[1]["measure"] == "Record leakage µA"
    assert wo.resolution == "Incoming inspection passed per the incoming checklist, all checks passed"
    assert wo.status_history.last().note == "Incoming inspection passed" and done.passed


def test_the_checklist_is_all_or_nothing_and_checked_as_a_pms(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=[{"result": "pass"}, {"result": ""}, {}, {}, {}])
    assert set(errors_of(e)) == {"step_2", "step_3", "step_4", "step_5"}
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=incoming_results(reading=""))
    assert errors_of(e) == {"reading_2": "Step 2: record the reading."}
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=[{"result": ""}, {"result": "", "reading": "40"}, {}, {}, {}])
    assert set(errors_of(e)) == {"step_1", "step_2", "step_3", "step_4", "step_5"}  # a reading typed is a step answered
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=incoming_results("na", "na", "na", "na", "na"))
    assert errors_of(e) == {"checklist": "Every step is marked N/A. Mark the steps that were done as pass or fail."}
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=INCOMING_ALL_PASS, signature="stale")
    assert errors_of(e) == {"checklist": "The procedure's checklist was revised while this was open. Check the steps again."}
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN and wo.inspection_result == ""


def test_passed_with_a_failed_step_is_refused(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=incoming_results("pass", "fail", "pass", "fail", "pass"))
    assert errors_of(e) == {"inspection_result": "Steps 2 and 4 failed. A device that fails a check fails its incoming inspection: choose Failed."}


def test_a_pass_without_the_checklist_says_what_was_checked(ctx, today, vent_model):
    """A vendor's or physicist's acceptance test: no checklist steps, the resolution names the acceptance report."""
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, vendor="Hamilton Service")
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED)
    assert errors_of(e) == {"resolution": "Record the checklist, or say what was checked."}
    complete_work_order(wo, inspection_result=PASSED, resolution="Vendor acceptance test per report AT-5521; leakage 31 µA")
    assert wo.checklist_results == [] and wo.resolution == "Vendor acceptance test per report AT-5521; leakage 31 µA"
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)


def test_a_procedure_codes_the_default_resolution(ctx, today, vent_model, techs):
    with_procedure(vent_model)
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    complete_work_order(wo, inspection_result=PASSED, results=[{"result": "pass"}, {"result": "pass", "reading": "30"}, {"result": "na"}])
    assert wo.resolution == "Incoming inspection passed per HM-G5-PM6, all checks passed"
    assert [s["text"] for s in wo.checklist_results] == ["Inspect and clean", "Leakage current", "Alarm check"]


# --- the result ---------------------------------------------------------------------------------------------------------------------

def test_the_result_is_required_while_the_device_waits(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, results=INCOMING_ALL_PASS, resolution="Looked fine")
    assert errors_of(e) == {"inspection_result": "Choose the inspection's result: passed or failed."}
    for bad in ("pass", "ok", "PASSED!"):
        with pytest.raises(ValidationError) as e:
            complete_work_order(wo, inspection_result=bad, results=INCOMING_ALL_PASS)
        assert errors_of(e) == {"inspection_result": "Choose passed or failed."}
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, pm_result=PmResult.PASS, results=INCOMING_ALL_PASS)
    assert errors_of(e) == {"pm_result": "Only a PM records a PM result."}


def test_a_fail_needs_the_resolution(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=FAILED, results=incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"))
    assert errors_of(e) == {"resolution": "Say what failed."}
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=FAILED)
    assert errors_of(e) == {"resolution": "Say what failed."}
    assert not WorkOrder.objects.filter(follow_up_of=wo).exists()


def test_an_inspection_of_a_device_that_does_not_wait_moves_nothing(ctx, today, vent, techs):
    before = (vent.status, vent.next_pm_on, vent.last_pm_on)
    for result in (PASSED, FAILED, ""):
        wo = create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Post-repair inspection", assigned_to=techs["dana"])
        done = complete_work_order(wo, inspection_result=result, resolution="Checked after the repair")
        assert wo.inspection_result == result and done.reinspection is None and not done.device_changed and not done.passed
        assert wo.status_history.last().note == completion.RESULT_NOTES.get(result, "")
    vent.refresh_from_db()
    assert (vent.status, vent.next_pm_on, vent.last_pm_on) == before
    assert WorkOrder.objects.filter(asset=vent, type=WoType.INSPECTION, status=WoStatus.OPEN).count() == 0


# --- one visit ----------------------------------------------------------------------------------------------------------------------

def test_an_open_inspection_is_completed_in_one_visit(ctx, today, vent_model, techs, make_user):
    tech_user = make_user("technician")
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    assert wo.status == WoStatus.OPEN and completion.starts_on_completion(wo, tech_user) and completion.blocker(wo, today, tech_user) == ""
    assert not completion.starts_on_completion(wo, make_user("analyst"))
    done = complete_work_order(wo, inspection_result=PASSED, results=INCOMING_ALL_PASS, by=tech_user)
    assert done.started and wo.started_on == today and wo.completed_on == today
    moves = [(h.from_status, h.to_status) for h in wo.status_history.order_by("created_at", "id")]
    assert moves[-2:] == [(WoStatus.OPEN, WoStatus.IN_PROGRESS), (WoStatus.IN_PROGRESS, WoStatus.COMPLETED)]
    unassigned = waiting_device(vent_model, "NEW-2", today=today)
    assert "is not assigned" in completion.blocker(inspections.open_inspection(unassigned), today, tech_user)


# --- a pass ---------------------------------------------------------------------------------------------------------------------------

def test_a_pass_puts_the_device_in_service_and_starts_its_pm_clock(ctx, today, vent_model, techs):
    a, wo = passed_device(vent_model, technician=techs["dana"], today=today)
    assert (a.status, a.awaiting_inspection, a.next_pm_on, a.last_pm_on) == (S.IN_SERVICE, False, add_months(today, vent_model.pm_interval_months),
                                                                            None)
    assert wo.inspection_result == PASSED and inspections.state(a).passed == wo
    assert not WorkOrder.objects.filter(type=WoType.REPAIR).exists()


# --- a fail ---------------------------------------------------------------------------------------------------------------------------

def test_a_fail_opens_a_reinspection_never_a_repair(ctx, today, vent_model, techs, make_user):
    tech_user = make_user("technician")
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    done = complete_work_order(wo, inspection_result=FAILED, results=incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"),
                               resolution="Leakage over the limit; vendor to swap the unit", by=tech_user)
    again = done.reinspection
    assert again is not None and done.reinspection_opened and done.follow_up is None and done.repair is None and not done.tagged_out
    assert (again.type, again.status, again.follow_up_of, again.opened_on, again.due_on) == (
        WoType.INSPECTION, WoStatus.OPEN, wo, today, today + timedelta(days=inspections.REINSPECTION_DUE_DAYS))
    assert again.assigned_to == techs["dana"] and again.created_by == tech_user and not again.vendor_service
    assert again.problem == (f"Re-inspection: incoming inspection {wo.number} failed. Failed step:\n2. Electrical safety test per IEC 62353 "
                             "(N/A for battery-only or non-electrical devices) (reading 640)\nLeakage over the limit; vendor to swap the unit")
    assert wo.status_history.last().note == f"Incoming inspection failed; {again.number} opened to re-inspect"
    assert completion.result_note(FAILED, done) == f"Incoming inspection failed; {again.number} opened to re-inspect"
    assert wo.resolution == "Leakage over the limit; vendor to swap the unit" and wo.inspection_result == FAILED
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.OUT_OF_SERVICE, True, None)
    assert not WorkOrder.objects.filter(type=WoType.REPAIR).exists()  # nothing counts against the model in AEM or MTBF
    assert inspections.open_inspection(a) == again


def test_the_reinspection_goes_to_the_same_vendor_or_an_active_technician_else_nobody(ctx, today, vent_model, techs):
    _a, _wo, again = failed_device(vent_model, "NEW-1", vendor="Hamilton Service", today=today)
    assert again.vendor_service and again.vendor_name == "Hamilton Service" and again.assigned_to is None
    tom = techs["tom"]  # not credentialed for the ventilator: assigned all the same (the history notes the override), as the first was
    _a, _wo, again = failed_device(vent_model, "NEW-2", technician=tom, today=today)
    assert again.assigned_to == tom and "override" in again.status_history.last().note
    techs["dana"].is_active = False
    techs["dana"].save()
    a = waiting_device(vent_model, "NEW-3", today=today)
    wo = inspection_of(a, techs["dana"])
    assert completion.reinspection_assignee(wo) == ("", None)
    again = complete_work_order(wo, inspection_result=FAILED, resolution="Damaged in shipping").reinspection
    assert again.assigned_to is None and not again.vendor_service and again.status_history.count() == 1


def test_fail_then_pass_reuses_nothing_and_the_evidence_is_the_pass(ctx, today, vent_model, techs):
    a, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)
    done = complete_work_order(again, inspection_result=PASSED, results=INCOMING_ALL_PASS, today=today + timedelta(days=9))
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.IN_SERVICE, False, add_months(today + timedelta(days=9), vent_model.pm_interval_months))
    assert done.passed and done.reinspection is None
    s = inspections.state(a)
    assert (s.passed, s.failed, s.open) == (again, failed, None)
    assert WorkOrder.objects.filter(asset=a, type=WoType.INSPECTION).count() == 2  # never doubled


def test_a_fail_takes_a_device_in_use_before_its_inspection_out(ctx, today, vent_model, techs, make_user):
    a = in_use_device(vent_model, today=today, by=make_user("director"))
    wo = inspection_of(a, techs["dana"])
    done = complete_work_order(wo, inspection_result=FAILED, resolution="Alarm speaker dead")
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection) == (S.OUT_OF_SERVICE, True) and done.tagged_out and done.device_changed
    assert a.history.first().history_change_reason == f"Tagged out: incoming inspection {wo.number} failed"
    # Declined, it stays in use (the user's call, as for a failed PM)
    b = in_use_device(vent_model, "NEW-5", today=today)
    other = inspection_of(b, techs["dana"])
    done = complete_work_order(other, inspection_result=FAILED, resolution="Label missing", tag_out=False)
    b.refresh_from_db()
    assert b.status == S.IN_SERVICE and not done.tagged_out and done.reinspection is not None


# --- reopening ------------------------------------------------------------------------------------------------------------------------

def test_a_reopened_fail_failing_again_reuses_its_reinspection(ctx, today, vent_model, techs):
    a, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)
    change_status(failed, WoStatus.IN_PROGRESS)  # reopened
    failed.refresh_from_db()
    assert failed.inspection_result == FAILED and inspections.state(a).failed is None  # open again: no longer read as failed
    done = complete_work_order(failed, inspection_result=FAILED, resolution="Still over the limit after re-seating the cord")
    assert done.reinspection == again and not done.reinspection_opened
    assert WorkOrder.objects.filter(asset=a, type=WoType.INSPECTION, status=WoStatus.OPEN).count() == 1
    assert failed.status_history.last().note == f"Incoming inspection failed; re-inspection {again.number} already open"
    note = WorkOrderNote.objects.get(work_order=again)
    assert note.text == f"Incoming inspection {failed.number} failed on {today:%b} {today.day}, {today.year}: Still over the limit after re-seating the cord"
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.OUT_OF_SERVICE, True, None)  # the invariant, after a re-completion


def test_a_reopened_fail_completed_passed_passes_the_device_and_cancels_the_reinspection(ctx, today, vent_model, techs):
    a, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)
    change_status(again, WoStatus.IN_PROGRESS)  # someone had started the re-inspection
    change_status(failed, WoStatus.IN_PROGRESS)
    done = complete_work_order(failed, inspection_result=PASSED, results=INCOMING_ALL_PASS)
    a.refresh_from_db()
    again.refresh_from_db()
    assert done.passed and (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)
    assert again.status == WoStatus.CANCELLED
    notes = [(h.from_status, h.to_status, h.note) for h in again.status_history.order_by("created_at", "id")][-2:]
    assert notes == [(WoStatus.IN_PROGRESS, WoStatus.OPEN, f"Cancelled: incoming inspection {failed.number} passed"),
                     (WoStatus.OPEN, WoStatus.CANCELLED, f"Cancelled: incoming inspection {failed.number} passed")]


def test_a_reopened_pass_stays_passed(ctx, today, vent_model, techs):
    a, wo = passed_device(vent_model, technician=techs["dana"], today=today)
    rows = a.history.count()
    change_status(wo, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=FAILED, resolution="Actually the alarm is dead")
    assert errors_of(e) == {"inspection_result": f"{wo.number} passed and {a.tag} no longer waits for its incoming inspection, so it stays "
                                                 "passed. If the device has failed since, tag the device out and open a repair."}
    done = complete_work_order(wo, results=INCOMING_ALL_PASS)  # blank keeps Passed
    a.refresh_from_db()
    assert wo.inspection_result == PASSED and not done.device_changed and a.history.count() == rows
    assert a.status == S.IN_SERVICE and not WorkOrder.objects.filter(asset=a, type=WoType.INSPECTION, status=WoStatus.OPEN).exists()


# --- who completes it ---------------------------------------------------------------------------------------------------------------

def test_a_vendors_pass_moves_the_device(ctx, today, vent_model, make_user):
    vendor = make_user("vendor")
    vendor.company = "Hamilton Service"
    vendor.save()
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, vendor="Hamilton Service")
    from apps.workorders import scoping

    assert scoping.can_see_work_order(vendor, wo) and completion.starts_on_completion(wo, vendor)
    done = complete_work_order(wo, inspection_result=PASSED, resolution="Acceptance test per report AT-77; leakage 22 µA", by=vendor)
    a.refresh_from_db()
    assert done.passed and (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)
    last = a.history.first()
    assert last.history_change_reason == f"Passed incoming inspection {wo.number}" and last.history_user == vendor


def test_a_ce_user_records_a_vendor_service_inspection(ctx, today, vent_model, make_user):
    manager = make_user("manager")
    a = waiting_device(vent_model, today=today)
    wo = inspection_of(a, vendor="Hamilton Service")
    complete_work_order(wo, inspection_result=PASSED, results=INCOMING_ALL_PASS, by=manager)
    a.refresh_from_db()
    assert a.status == S.IN_SERVICE and wo.status_history.last().changed_by == manager


# --- two passes at once ---------------------------------------------------------------------------------------------------------

def test_two_inspections_passing_one_after_the_other_move_the_device_once(ctx, today, vent_model, techs):
    """The first's pass cancels the other open inspection; the second, completed from a copy read before, is refused as cancelled.
    (At the very same moment, on PostgreSQL: the next test.)"""
    a, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)
    change_status(failed, WoStatus.IN_PROGRESS)  # both open now: the reopened inspection and its re-inspection
    stale_again = WorkOrder.objects.get(pk=again.pk)
    complete_work_order(failed, inspection_result=PASSED, results=INCOMING_ALL_PASS)
    rows = a.history.count()
    with pytest.raises(ValidationError, match=f"{again.number} was cancelled."):
        complete_work_order(stale_again, inspection_result=PASSED, results=INCOMING_ALL_PASS)
    a.refresh_from_db()
    assert a.history.count() == rows and (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_inspections_completed_at_the_same_moment_move_the_device_once(tenant, monkeypatch):
    """Two connections complete the device's two open inspections at the same moment (a barrier before, and each holding its locks a
    while in services._on_completed): both lock the device's open inspections first, in number order (inspections.lock_open), so they
    take turns. The first passes the device and cancels the other inspection; the second is refused as cancelled. No deadlock (each
    holding its own inspection while waiting for the device's row held by the other did deadlock, through the re-inspection's
    follow_up_of checked at commit)."""
    import time

    from apps.credentials.models import Credential, Scope, Technician
    from apps.equipment.models import DeviceModel, RiskClass

    today = timezone.localdate()
    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                           risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        dana = Technician.objects.create(name="Dana Whitfield", title="Lead BMET")
        Credential.objects.create(technician=dana, scope=Scope.MODEL, value="Hamilton-G5", expires_on=today + timedelta(days=400))
        a, failed, again = failed_device(model, technician=dana, today=today)
        change_status(failed, WoStatus.IN_PROGRESS)  # reopened: two inspections of one waiting device open
    barrier = threading.Barrier(2, timeout=20)
    real = wo_services._on_completed

    def slowly(wo, as_of, by=None):
        if wo.type == WoType.INSPECTION:
            time.sleep(1)  # holding its locks: the other is trying to take them meanwhile
        return real(wo, as_of, by=by)

    monkeypatch.setattr(wo_services, "_on_completed", slowly)
    errors = []

    def complete(pk):
        try:
            with tenant_context(tenant):
                wo = WorkOrder.objects.get(pk=pk)
                barrier.wait()
                complete_work_order(wo, inspection_result=PASSED, results=INCOMING_ALL_PASS)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=complete, args=(pk,)) for pk in (failed.pk, again.pk)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(errors) == 1 and isinstance(errors[0], ValidationError) and errors[0].messages[0].endswith("was cancelled."), errors
    with tenant_context(tenant):
        a.refresh_from_db()
        assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)
        assert a.history.filter(history_change_reason__startswith="Passed incoming inspection").count() == 1
        outcomes = sorted(WorkOrder.objects.filter(pk__in=[failed.pk, again.pk]).values_list("status", "inspection_result"))
        assert outcomes in ([(WoStatus.CANCELLED, ""), (WoStatus.COMPLETED, PASSED)], [(WoStatus.CANCELLED, FAILED), (WoStatus.COMPLETED, PASSED)])


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_use_before_inspection_while_the_inspection_passes_takes_its_turn(tenant, monkeypatch):
    """use_before_inspection at the moment the inspection is being completed: it locks the open inspection before the device's row,
    as the completion does, so it waits its turn and then finds the device passed (taking the device's row first, it would wait for
    the inspection while the pass waits for the device: a deadlock)."""
    import time

    from apps.credentials.models import Technician
    from apps.equipment.models import DeviceModel, RiskClass
    from apps.equipment.services import use_before_inspection

    today = timezone.localdate()
    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        a = waiting_device(model, today=today)
        wo = inspection_of(a, Technician.objects.create(name="Dana Whitfield"))
    barrier = threading.Barrier(2, timeout=20)
    real = wo_services._on_completed

    def slowly(w, as_of, by=None):
        time.sleep(1)  # the inspection's lock held: use_before_inspection is trying meanwhile
        return real(w, as_of, by=by)

    monkeypatch.setattr(wo_services, "_on_completed", slowly)
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

    def complete():
        complete_work_order(WorkOrder.objects.get(pk=wo.pk), inspection_result=PASSED, results=INCOMING_ALL_PASS)

    def use():
        time.sleep(0.3)  # the completion takes the inspection's lock first
        use_before_inspection(Asset.objects.get(pk=a.pk), UseBeforeInspection.EMERGENCY)

    threads = [threading.Thread(target=run, args=(call,)) for call in (complete, use)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(errors) == 1 and isinstance(errors[0], ValidationError), errors
    assert errors[0].messages == [f"{a.tag} is not waiting for an incoming inspection."]
    with tenant_context(tenant):
        a.refresh_from_db()
        assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False) and inspections.uses_before([a.pk]) == {}


# --- one transaction ----------------------------------------------------------------------------------------------------------------

def test_a_refusal_after_the_reinspection_opened_rolls_everything_back(ctx, today, vent_model, techs, monkeypatch):
    a = in_use_device(vent_model, today=today)
    wo = inspection_of(a, techs["dana"])
    real = wo_services.change_status

    def boom(w, to_status, *args, **kwargs):
        if to_status == WoStatus.COMPLETED:
            raise ValidationError("Database said no.")
        return real(w, to_status, *args, **kwargs)

    monkeypatch.setattr(wo_services, "change_status", boom)
    with pytest.raises(ValidationError, match="Database said no"):
        complete_work_order(wo, inspection_result=FAILED, resolution="Alarm dead")
    wo.refresh_from_db()
    a.refresh_from_db()
    assert (wo.status, wo.inspection_result, wo.resolution) == (WoStatus.OPEN, "", "")
    assert WorkOrder.objects.filter(asset=a, type=WoType.INSPECTION).count() == 1 and a.status == S.IN_SERVICE


def test_the_fixtures_build_what_they_say(ctx, today, vent_model, techs):
    a, wo = passed_device(vent_model, technician=techs["dana"], today=today)
    assert (a.status, a.awaiting_inspection, wo.inspection_result) == (S.IN_SERVICE, False, PASSED)
    b, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)
    assert (b.status, b.awaiting_inspection, failed.inspection_result, again.follow_up_of) == (S.OUT_OF_SERVICE, True, FAILED, failed)
    c = in_use_device(vent_model, today=today, reason=UseBeforeInspection.ARRIVED_IN_USE)
    assert (c.status, c.awaiting_inspection, c.next_pm_on) == (S.IN_SERVICE, True, None)
    assert Asset.objects.filter(awaiting_inspection=True).count() == 2
