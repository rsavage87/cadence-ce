"""Completing a work order (slice 15, apps.workorders.completion): when it can be completed, the resolution, a PM's result and its
checklist step by step (results, readings, agreement with the result), the snapshot surviving a procedure revision, a failed PM's
follow-up repair (its fields, the tag-out, an open repair recorded on instead, never a duplicate), one transaction, the device and
the portal's done email behaving as before, tenant isolation, and the seed."""
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility import services as fs
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.pm.procedures import update_procedure
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import completion
from apps.workorders import services as wo_services
from apps.workorders.completion import checklist_signature, complete_work_order
from apps.workorders.models import PmResult, Priority, Source, WorkOrder, WorkOrderNote, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_service_request, create_work_order

TODAY = date.today()
CHECKLIST = ["Inspect and clean", {"text": "Leakage current", "measure": "µA, limit 100"}, {"text": "Battery run time", "measure": True}, "Alarm check"]
ALL_PASS = [{"result": "pass"}, {"result": "pass", "reading": "42 µA"}, {"result": "pass", "reading": "95 min"}, {"result": "pass"}]


def with_procedure(model, checklist=CHECKLIST, code="HM-G5-PM6"):
    proc = PmProcedure.objects.create(code=code, name="Hamilton G5 6-month PM", checklist=checklist, revision="C")
    model.pm_procedure = proc
    model.save()
    return proc


def started(asset, *, type=WoType.PM, tech=None, vendor="", opened=None, problem="Scheduled preventive maintenance"):
    wo = create_work_order(asset=asset, type=type, priority="high", problem=problem, source=Source.PM_PLANNER if type == WoType.PM else Source.MANUAL,
                           opened_on=opened or TODAY)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    change_status(wo, WoStatus.IN_PROGRESS, as_of=opened or TODAY)
    return wo


def results(*values):
    """Results for CHECKLIST: "pass" / "fail" / "na" per step, with readings for the measured steps."""
    readings = ["", "42 µA", "95 min", ""]
    return [{"result": v, "reading": readings[i]} for i, v in enumerate(values)]


def errors_of(excinfo) -> dict:
    return {k: " ".join(v) for k, v in excinfo.value.message_dict.items()}


@pytest.fixture
def vent_pm(ctx, vent, vent_model, techs):
    with_procedure(vent_model)
    return started(vent, tech=techs["dana"])


# --- when it can be completed --------------------------------------------------------------------------------------------------


def test_only_a_work_order_in_progress_is_completed(ctx, vent, techs):
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm")
    assign(wo, technician=techs["dana"])
    with pytest.raises(ValidationError, match="has not been started"):
        complete_work_order(wo, resolution="Fixed")
    change_status(wo, WoStatus.AWAITING_PARTS)
    with pytest.raises(ValidationError, match="waiting on parts"):
        complete_work_order(wo, resolution="Fixed")
    change_status(wo, WoStatus.IN_PROGRESS)
    complete_work_order(wo, resolution="Fixed")
    with pytest.raises(ValidationError, match="already completed"):
        complete_work_order(wo, resolution="Fixed again")
    change_status(wo, WoStatus.CLOSED)
    with pytest.raises(ValidationError, match="is closed"):
        complete_work_order(wo, resolution="Fixed")
    cancelled = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm")
    change_status(cancelled, WoStatus.CANCELLED)
    assert completion.blocker(cancelled) == f"{cancelled.number} was cancelled."
    assert wo.status_history.filter(to_status=WoStatus.COMPLETED).count() == 1


def test_an_unassigned_work_order_is_not_completed(ctx, vent):
    wo = started(vent, type=WoType.REPAIR)
    with pytest.raises(ValidationError, match="is not assigned"):
        complete_work_order(wo, resolution="Fixed")
    assign(wo, vendor_name="Hamilton Service")  # a vendor counts
    assert complete_work_order(wo, resolution="Vendor replaced the blower").work_order.status == WoStatus.COMPLETED


def test_never_completed_before_it_was_opened(ctx, vent, techs):
    wo = started(vent, type=WoType.REPAIR, tech=techs["dana"], opened=TODAY - timedelta(days=3))
    with pytest.raises(ValidationError, match="before it was opened"):
        complete_work_order(wo, resolution="Fixed", today=TODAY - timedelta(days=4))
    done = complete_work_order(wo, resolution="Fixed", today=TODAY - timedelta(days=3))
    assert done.work_order.completed_on == TODAY - timedelta(days=3) and wo.completed_on == TODAY - timedelta(days=3)


def test_another_facilitys_work_order_is_refused(ctx, tenant, other_tenant, techs):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        wo = create_work_order(asset=theirs, type="repair", priority="normal", problem="Alarm", assigned_to=None, vendor_service=True, vendor_name="GE")
        change_status(wo, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError, match="from this facility"):
        complete_work_order(wo, resolution="Fixed")
    with tenant_context(other_tenant):
        wo.refresh_from_db()
        assert wo.status == WoStatus.IN_PROGRESS and wo.resolution == ""


# --- the resolution ------------------------------------------------------------------------------------------------------------


def test_a_repair_needs_its_resolution_trimmed_and_within_the_limit(ctx, vent, techs):
    wo = started(vent, type=WoType.REPAIR, tech=techs["dana"])
    for bad, message in (("", "Say what was found and done."), ("   \n ", "Say what was found and done."),
                         ("x" * (completion.RESOLUTION_MAX + 1), "Keep the resolution to 1000 characters"),
                         ("Replaced \x00 sensor", "invisible control character")):
        with pytest.raises(ValidationError) as e:
            complete_work_order(wo, resolution=bad)
        assert message in errors_of(e)["resolution"]
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS
    complete_work_order(wo, resolution="  Replaced the flow sensor.\r\nVerified per OEM procedure.  ")
    assert wo.resolution == "Replaced the flow sensor.\nVerified per OEM procedure." and wo.pm_result == "" and wo.checklist_results == []
    assert wo.status_history.last().note == ""  # the resolution is the record; the timeline says the status


def test_other_types_need_a_resolution_and_record_no_pm_result(ctx, vent, techs):
    for wtype in (WoType.INSPECTION, WoType.RECALL, WoType.SAFETY):
        wo = started(vent, type=wtype, tech=techs["dana"])
        with pytest.raises(ValidationError) as e:
            complete_work_order(wo, resolution="", pm_result=PmResult.PASS)
        assert errors_of(e) == {"resolution": "Say what was found and done.", "pm_result": "Only a PM records a PM result."}
        with pytest.raises(ValidationError, match="Only a PM records checklist results"):
            complete_work_order(wo, resolution="Done", results=[{"result": "pass"}])
        complete_work_order(wo, resolution="Inspected and entered into inventory")
        assert wo.status == WoStatus.COMPLETED and wo.pm_result == ""


def test_a_completed_repair_still_returns_a_tagged_out_device(ctx, vent, techs):
    wo = create_work_order(asset=vent, type="repair", priority="critical", problem="Dead", tag_out=True, assigned_to=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    done = complete_work_order(wo, resolution="Replaced the power supply")
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and done.device_changed and not done.tagged_out and done.follow_up is None


# --- a PM's result and checklist -----------------------------------------------------------------------------------------------


def test_a_passed_pm_records_its_checklist_and_moves_the_next_pm(ctx, vent, vent_pm):
    done = complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS)
    wo = vent_pm
    assert wo.status == WoStatus.COMPLETED and wo.pm_result == PmResult.PASS and done.work_order.pk == wo.pk
    assert wo.checklist_results == [
        {"text": "Inspect and clean", "measure": None, "result": "pass", "reading": ""},
        {"text": "Leakage current", "measure": "µA, limit 100", "result": "pass", "reading": "42 µA"},
        {"text": "Battery run time", "measure": True, "result": "pass", "reading": "95 min"},
        {"text": "Alarm check", "measure": None, "result": "pass", "reading": ""},
    ]
    assert wo.resolution == "PM completed per HM-G5-PM6, all checks passed"
    assert wo.status_history.last().note == "PM passed"
    assert "Status changed to completed: PM passed" in [e["text"] for e in wo_services.timeline(wo)]
    vent.refresh_from_db()
    assert vent.last_pm_on == TODAY and vent.next_pm_on == add_months(TODAY, vent.pm_interval_months) and done.device_changed
    assert done.follow_up is None and not done.tagged_out and not wo.follow_ups.exists()


def test_a_passed_pm_without_a_procedure_needs_no_checklist(ctx, vent, techs):
    wo = started(vent, tech=techs["dana"])
    complete_work_order(wo, pm_result=PmResult.PASS)
    assert wo.resolution == "PM completed, all checks passed" and wo.checklist_results == [] and wo.pm_result == PmResult.PASS
    other = started(vent, tech=techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(other, pm_result=PmResult.PASS, results=[{"result": "pass"}])
    assert "no checklist on file" in errors_of(e)["checklist"]


def test_a_typed_resolution_is_kept_on_a_pass(ctx, vent_pm):
    complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS, resolution="All good; cleaned the fan filter")
    assert vent_pm.resolution == "All good; cleaned the fan filter"


def test_a_pm_needs_its_overall_result(ctx, vent_pm):
    for bad, message in (("", "Choose the PM's result"), ("great", "Choose pass, pass with minor repair, or fail.")):
        with pytest.raises(ValidationError) as e:
            complete_work_order(vent_pm, pm_result=bad, results=ALL_PASS)
        assert message in errors_of(e)["pm_result"]
    vent_pm.refresh_from_db()
    assert vent_pm.status == WoStatus.IN_PROGRESS and vent_pm.pm_result == ""


def test_every_step_needs_pass_fail_or_na(ctx, vent_pm):
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS)
    assert set(errors_of(e)) == {"step_1", "step_2", "step_3", "step_4"} and errors_of(e)["step_1"] == "Step 1: choose pass, fail, or N/A."
    bad = results("pass", "pass", "", "ok")
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS, results=bad)
    assert set(errors_of(e)) == {"step_3", "step_4"}  # the result agreement waits for the steps
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS[:3])
    assert errors_of(e) == {"checklist": "Record a result for each of the 4 steps."}


def test_a_measured_step_needs_its_reading_unless_not_applicable(ctx, vent_pm):
    steps = [{"result": "pass"}, {"result": "pass", "reading": "  "}, {"result": "fail"}, {"result": "pass", "reading": "ignored"}]
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=steps)
    assert errors_of(e) == {"reading_2": "Step 2: record the reading.", "reading_3": "Step 3: record the reading."}
    long = [{"result": "pass"}, {"result": "pass", "reading": "9" * (completion.READING_MAX + 1)}, {"result": "na", "reading": "skip"}, {"result": "pass"}]
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS, results=long)
    assert errors_of(e) == {"reading_2": "Step 2: keep the reading to 60 characters."}
    ok = [{"result": "pass", "reading": "not a measured step"}, {"result": "PASS", "reading": " 42   µA "}, {"result": "na", "reading": "skip"},
          {"result": "pass"}]
    complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ok)
    assert [(s["result"], s["reading"]) for s in vent_pm.checklist_results] == [("pass", ""), ("pass", "42 µA"), ("na", ""), ("pass", "")]


def test_a_reading_of_zero_is_a_reading(ctx, vent_pm):
    complete_work_order(vent_pm, pm_result=PmResult.PASS, results=[{"result": "pass"}, {"result": "pass", "reading": 0}, {"result": "na"}, {"result": "pass"}])
    assert vent_pm.checklist_results[1]["reading"] == "0"


def test_not_every_step_may_be_not_applicable(ctx, vent_pm):
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS, results=results("na", "na", "na", "na"))
    assert errors_of(e) == {"checklist": "Every step is marked N/A. Mark the steps that were done as pass or fail."}


def test_pass_means_no_step_failed(ctx, vent_pm):
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS, results=results("pass", "fail", "pass", "fail"))
    assert errors_of(e) == {"pm_result": "Steps 2 and 4 failed. Choose Pass with minor repair if it was put right during the PM, or Fail."}


def test_pass_with_minor_repair_says_what_was_repaired(ctx, vent_pm):
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS_MINOR_REPAIR, results=results("fail", "pass", "pass", "pass"))
    assert errors_of(e) == {"resolution": "Say what was repaired during the PM."}
    done = complete_work_order(vent_pm, pm_result=PmResult.PASS_MINOR_REPAIR, results=results("fail", "pass", "pass", "pass"),
                               resolution="Replaced a cracked hose clamp found on inspection")
    assert vent_pm.pm_result == PmResult.PASS_MINOR_REPAIR and vent_pm.checklist_results[0]["result"] == "fail" and done.follow_up is None
    assert vent_pm.status_history.last().note == "PM passed with minor repair"


def test_fail_needs_a_failed_step_or_with_no_checklist_a_reason(ctx, vent, vent_model, techs):
    with_procedure(vent_model)
    wo = started(vent, tech=techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, pm_result=PmResult.FAIL, results=ALL_PASS)
    assert errors_of(e) == {"pm_result": "Mark the step that failed, or choose another result."}
    vent_model.pm_procedure = None
    vent_model.save()
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, pm_result=PmResult.FAIL)
    assert errors_of(e) == {"resolution": "Say what failed."}
    done = complete_work_order(wo, pm_result=PmResult.FAIL, resolution="Blower noisy and over temperature")
    assert wo.resolution == "Blower noisy and over temperature" and done.follow_up.problem == f"PM {wo.number} failed: Blower noisy and over temperature"


def test_results_typed_against_a_revised_checklist_are_refused(ctx, vent_pm, vent_model):
    shown = checklist_signature(completion.checklist_of(vent_model.pm_procedure))
    update_procedure(vent_model.pm_procedure, checklist=["Inspect and clean", "Alarm check", "New step", "Another"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS, signature=shown)
    assert errors_of(e) == {"checklist": "The procedure's checklist was revised while this was open. Check the steps again."}
    vent_model.refresh_from_db()
    current = checklist_signature(completion.checklist_of(vent_model.pm_procedure))
    complete_work_order(vent_pm, pm_result=PmResult.PASS, results=results("pass", "pass", "pass", "na"), signature=current)
    assert [s["text"] for s in vent_pm.checklist_results] == ["Inspect and clean", "Alarm check", "New step", "Another"]


def test_the_recorded_checklist_survives_a_procedure_revision(ctx, vent_pm, vent_model):
    complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS)
    before = list(vent_pm.checklist_results)
    update_procedure(vent_model.pm_procedure, checklist="Only one step now | V", code="HM-G5-PM6B")
    vent_pm.refresh_from_db()
    assert vent_pm.checklist_results == before
    assert [s["text"] for s in completion.recorded_steps(vent_pm)] == ["Inspect and clean", "Leakage current", "Battery run time", "Alarm check"]
    assert completion.recorded_steps(vent_pm)[1] == {"n": 2, "text": "Leakage current", "measured": True, "measure": "µA, limit 100", "result": "pass",
                                                     "label": "Pass", "reading": "42 µA"}


def test_a_checklist_saved_as_text_is_recorded_step_by_step(ctx, vent, vent_model, techs):
    with_procedure(vent_model, checklist="Inspect\nTest alarms\n")
    wo = started(vent, tech=techs["dana"])
    complete_work_order(wo, pm_result=PmResult.PASS, results=["pass", "na"])
    assert [(s["text"], s["result"]) for s in wo.checklist_results] == [("Inspect", "pass"), ("Test alarms", "na")]


# --- a failed PM's follow-up repair --------------------------------------------------------------------------------------------


def test_a_failed_pm_opens_its_follow_up_repair_and_tags_the_device_out(ctx, vent, vent_pm, techs, make_user):
    tech_user = make_user("technician")
    done = complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("pass", "fail", "pass", "fail"), by=tech_user)
    fu = done.follow_up
    assert fu is not None and done.repair == fu and done.tagged_out and done.device_changed
    assert fu.follow_up_of == vent_pm and fu.type == WoType.REPAIR and fu.status == WoStatus.OPEN
    assert fu.priority == Priority.HIGH and fu.due_on == TODAY + timedelta(days=2)  # life support
    assert fu.assigned_to == techs["dana"]  # credentialed for the Hamilton-G5
    assert fu.problem == f"PM {vent_pm.number} failed. Failed steps:\n2. Leakage current (reading 42 µA)\n4. Alarm check"
    assert fu.opened_on == TODAY and fu.created_by == tech_user and fu.requester == "Technician User" and fu.source == Source.MANUAL
    assert fu.tagged_out
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    assert vent_pm.pm_result == PmResult.FAIL and vent_pm.resolution == f"PM failed on steps 2 and 4; repair {fu.number} opened."
    assert vent_pm.status_history.last().note == f"PM failed; {fu.number} opened for the repair"
    assert list(vent_pm.follow_ups.all()) == [fu]
    # completing the repair returns the device, as a repair opened with the tag-out always has
    change_status(fu, WoStatus.IN_PROGRESS)
    complete_work_order(fu, resolution="Replaced the line cord; leakage 30 µA", by=tech_user)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_the_follow_up_is_unassigned_unless_the_pms_technician_is_active_and_credentialed(ctx, vent, vent_model, techs):
    with_procedure(vent_model)
    fail = results("fail", "pass", "pass", "pass")
    # Tom has no ventilator credential: a manager assigns it.
    wo = started(vent, tech=techs["tom"])
    fu = complete_work_order(wo, pm_result=PmResult.FAIL, results=fail, tag_out=False).follow_up
    assert fu is not None and fu.assigned_to is None and not fu.vendor_service and fu.status_history.count() == 1
    # A vendor's PM: the same vendor does the repair (no contract here: field service, time and materials; slice 16).
    wo = started(vent, vendor="Hamilton Service")
    fu = complete_work_order(wo, pm_result=PmResult.FAIL, results=fail, tag_out=False).follow_up
    assert fu.vendor_service and fu.vendor_name == "Hamilton Service" and fu.assigned_to is None
    techs["dana"].is_active = False
    techs["dana"].save()
    wo = started(vent, tech=techs["dana"])
    assert complete_work_order(wo, pm_result=PmResult.FAIL, results=fail, tag_out=False).follow_up.assigned_to is None
    assert completion.follow_up_technician(wo, vent) is None


def test_the_follow_ups_priority_follows_the_models_risk(ctx, dept, techs):
    for risk, priority in ((RiskClass.LIFE_SUPPORT, Priority.HIGH), (RiskClass.HIGH, Priority.HIGH), (RiskClass.MEDIUM, Priority.NORMAL),
                           (RiskClass.LOW, Priority.NORMAL)):
        model = DeviceModel.objects.create(manufacturer="Acme", model=f"M-{risk}", description="Device", category="Infusion pumps", risk_class=risk)
        asset = Asset.objects.create(tag=f"CE-{risk}", device_model=model, department=dept)
        wo = started(asset, tech=techs["dana"])
        fu = complete_work_order(wo, pm_result=PmResult.FAIL, resolution="Fails its self-test").follow_up
        assert fu.priority == priority and fu.assigned_to == techs["dana"], risk  # Dana holds Infusion pumps


def test_tag_out_can_be_declined_and_never_touches_a_device_not_in_service(ctx, vent, vent_model, techs):
    with_procedure(vent_model)
    wo = started(vent, tech=techs["dana"])
    done = complete_work_order(wo, pm_result=PmResult.FAIL, results=results("fail", "pass", "pass", "pass"), tag_out=False)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and not done.tagged_out and not done.follow_up.tagged_out
    change_status(done.follow_up, WoStatus.CANCELLED)
    vent.status = AssetStatus.ON_LOAN
    vent.save()
    again = started(vent, tech=techs["dana"])
    done = complete_work_order(again, pm_result=PmResult.FAIL, results=results("fail", "pass", "pass", "pass"), tag_out=True)
    vent.refresh_from_db()
    # not tagged out, and the repair is not marked as the reason it is out, so completing it never moves the device
    assert vent.status == AssetStatus.ON_LOAN and not done.tagged_out and not done.follow_up.tagged_out


def test_open_repair_and_tag_out_are_ignored_unless_the_pm_failed(ctx, vent, vent_pm):
    done = complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS, open_repair=False, tag_out=True)
    vent.refresh_from_db()
    assert done.follow_up is None and vent.status == AssetStatus.IN_SERVICE and not WorkOrder.objects.filter(type=WoType.REPAIR).exists()


def test_a_failure_can_be_recorded_on_the_repair_already_open(ctx, vent, vent_pm, techs):
    with pytest.raises(ValidationError) as e:
        complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("fail", "pass", "pass", "pass"), open_repair=False)
    assert errors_of(e) == {"open_repair": "CE-10001 has no open repair work order to record the failure on, so a failed PM opens one."}
    repair = create_work_order(asset=vent, type="repair", priority="normal", problem="Intermittent leak alarm", assigned_to=techs["dana"])
    done = complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("pass", "fail", "pass", "pass"), open_repair=False)
    assert done.follow_up is None and done.repair == repair and done.tagged_out
    assert WorkOrder.objects.filter(type=WoType.REPAIR).count() == 1  # no duplicate repair for the failure counts
    note = WorkOrderNote.objects.get(work_order=repair)
    assert note.text == f"PM {vent_pm.number} failed on {TODAY:%b} {TODAY.day}, {TODAY.year}: 2. Leakage current (reading 42 µA)"
    repair.refresh_from_db()
    vent.refresh_from_db()
    # The repair the failure is on names the PM (the drawer, the print, and the PM history link them both ways).
    assert repair.tagged_out and repair.follow_up_of == vent_pm and vent.status == AssetStatus.OUT_OF_SERVICE
    assert repair.status_history.last().note == f"Device tagged out of service: PM {vent_pm.number} failed"
    assert vent_pm.status_history.last().note == f"PM failed; recorded on open repair {repair.number}"
    assert vent_pm.resolution == f"PM failed on step 2; recorded on repair {repair.number}."
    assert vent.history.filter(status=AssetStatus.OUT_OF_SERVICE, history_change_reason=f"Tagged out: PM {vent_pm.number} failed").exists()
    change_status(repair, WoStatus.IN_PROGRESS)
    complete_work_order(repair, resolution="Replaced the leaking seal")
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_a_reopened_pm_failing_again_records_on_its_open_repair(ctx, vent, vent_pm):
    first = complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("fail", "pass", "pass", "pass"), tag_out=False).follow_up
    change_status(vent_pm, WoStatus.IN_PROGRESS)  # reopened
    vent_pm.refresh_from_db()
    assert vent_pm.pm_result == PmResult.FAIL  # kept until it is completed again
    done = complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("pass", "fail", "pass", "pass"), tag_out=False)
    assert done.follow_up is None and done.repair == first and list(vent_pm.follow_ups.all()) == [first]
    assert WorkOrderNote.objects.filter(work_order=first).count() == 1


def test_a_reopened_pm_records_its_results_anew(ctx, vent_pm):
    complete_work_order(vent_pm, pm_result=PmResult.PASS, results=ALL_PASS)
    change_status(vent_pm, WoStatus.IN_PROGRESS)
    vent_pm.refresh_from_db()
    complete_work_order(vent_pm, pm_result=PmResult.PASS_MINOR_REPAIR, results=results("fail", "pass", "pass", "pass"), resolution="Re-seated the cover")
    assert vent_pm.pm_result == PmResult.PASS_MINOR_REPAIR and vent_pm.checklist_results[0]["result"] == "fail"
    assert [h.pm_result for h in vent_pm.history.order_by("history_id") if h.status == WoStatus.COMPLETED][:2] == [PmResult.PASS, PmResult.PASS_MINOR_REPAIR]


def test_a_refusal_after_the_repair_is_opened_rolls_everything_back(ctx, vent, vent_pm, monkeypatch):
    def boom(*args, **kwargs):
        raise ValidationError("Database said no.")

    monkeypatch.setattr(wo_services, "change_status", boom)  # fails after the follow-up and the tag-out are written
    with pytest.raises(ValidationError, match="Database said no"):
        complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("fail", "pass", "pass", "pass"))
    vent_pm.refresh_from_db()
    vent.refresh_from_db()
    assert vent_pm.status == WoStatus.IN_PROGRESS and vent_pm.pm_result == "" and vent_pm.checklist_results == [] and vent_pm.resolution == ""
    assert not WorkOrder.objects.filter(type=WoType.REPAIR).exists() and vent.status == AssetStatus.IN_SERVICE


def test_a_refusal_while_opening_the_repair_rolls_back_too(ctx, vent, vent_pm, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValidationError("Assignment refused.")

    monkeypatch.setattr(wo_services, "assign", refuse)
    with pytest.raises(ValidationError, match="Assignment refused"):
        complete_work_order(vent_pm, pm_result=PmResult.FAIL, results=results("fail", "pass", "pass", "pass"))
    vent.refresh_from_db()
    assert not WorkOrder.objects.filter(type=WoType.REPAIR).exists() and vent.status == AssetStatus.IN_SERVICE
    assert WorkOrder.objects.get(pk=vent_pm.pk).status == WoStatus.IN_PROGRESS


# --- the portal's done email ---------------------------------------------------------------------------------------------------


def test_the_portal_done_email_still_goes_out(ctx, vent, techs, mailoutbox, django_capture_on_commit_callbacks):
    fs.update_settings(portal_confirmation="email", portal_email_domains="rrmc.org")
    sr = create_service_request(asset=vent, department=vent.department, problem="Alarm", urgency="high", requester_email="kim.lee@rrmc.org")
    mailoutbox.clear()
    wo = sr.work_order
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    with django_capture_on_commit_callbacks(execute=True):
        complete_work_order(wo, resolution="Replaced the alarm speaker")
    assert len(mailoutbox) == 1 and "is done" in mailoutbox[0].body and "speaker" not in mailoutbox[0].body


# --- the demo --------------------------------------------------------------------------------------------------------------------


def test_the_demo_records_its_pm_results(db):
    call_command("seed_demo", stdout=StringIO())
    tenant = Tenant.objects.get(slug="riverside")
    with tenant_context(tenant):
        done = WorkOrder.objects.filter(type=WoType.PM, status__in=completion.DONE_STATUSES)
        counts = {r: done.filter(pm_result=r).count() for r in PmResult.values}
        assert done.filter(pm_result="").count() == 0 and counts[PmResult.FAIL] == 1
        assert counts[PmResult.PASS] > 5 * counts[PmResult.PASS_MINOR_REPAIR] and counts[PmResult.PASS_MINOR_REPAIR] >= 3  # mostly Pass
        for wo in done.select_related("asset__device_model__pm_procedure"):
            steps = completion.checklist_of(wo.asset.device_model.pm_procedure)
            assert [(s["text"], s["measure"]) for s in wo.checklist_results] == [(t, m) for t, m in steps], wo.number
            assert all(s["reading"] for s in wo.checklist_results if s["measure"]), wo.number
        minor = done.filter(pm_result=PmResult.PASS_MINOR_REPAIR).first()
        assert minor.resolution.startswith("Visual inspection:") and minor.checklist_results[0]["result"] == "fail"
        failed = done.get(pm_result=PmResult.FAIL)
        repair = failed.follow_ups.get()
        assert failed.asset.device_model.risk_class == RiskClass.LIFE_SUPPORT and failed.checklist_results[1]["reading"] == "184 µA"
        assert repair.status == WoStatus.CLOSED and repair.tagged_out and repair.assigned_to_id and repair.priority == Priority.HIGH
        assert repair.completed_on == failed.completed_on + timedelta(days=2) and repair.resolution.startswith("Replaced the line cord")
        assert failed.asset.status == AssetStatus.IN_SERVICE  # back from its repair
        assert not WorkOrder.objects.filter(type=WoType.REPAIR, status__in=completion.DONE_STATUSES, resolution="").exists()
    call_command("seed_demo", stdout=StringIO())  # still a no-op the second time
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(pm_result=PmResult.FAIL).count() == 1
