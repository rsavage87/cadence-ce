"""
Slice 30, checks after a repair (apps.workorders.repair_checks, completion.complete_work_order's repair branch, completion.keep_open):
every repair records how it ended. Checked and ready for use takes the standard post-repair checklist, every step recorded, never N/A on
the visual check and the function check, none failed, a reading on each measured step, the signature checked, and returns the device
as a repaired one always came back; checked by the vendor only on vendor service, for a user who sees the whole facility, with the
vendor's report number (a token) and no steps; not returned to use takes no steps, keeps the device out (moved out of service through
equipment's rules, the repair marked tagged out, never returned on completion, but by its reopening and completion as checked), and
both keep the resolution required. keep_open records failed checks on the repair that stays open and holds the device out. The doors
around them close: equipment.services.set_status (and the drawer's buttons, and the devices import) never puts in service or lends out
a device a repair holds out; an incident's suspect device returns to use only after its investigation was checked; a vendor loaner's
device of ours is not back after its last repair ended not returned. The API and the completion modal pass the outcome through. The
demo seeds each outcome. All on both databases (the row-level security policies on PostgreSQL).
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.utils import timezone
from incident_fixtures import held_incident, make_incident
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, POST_REPAIR_ALL_PASS, checked_post, checked_repair, post_repair_results

from apps.demo.management.commands import seed_demo
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Ownership
from apps.imports import services as imports
from apps.imports.models import ImportRun
from apps.incidents import services as inc
from apps.incidents.models import Affected, Outcome, Release
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import completion, repair_checks
from apps.workorders.completion import checklist_of, checklist_signature, complete_work_order, keep_open
from apps.workorders.models import Priority, RepairCheck, WorkOrder, WorkOrderNote, WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
STEPS = checklist_of(repair_checks.POST_REPAIR)
SIGNATURE = checklist_signature(STEPS)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def repair(asset, *, tech=None, vendor="", tag_out=False, start=True, problem="Low tidal volume alarm"):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem=problem, tag_out=tag_out)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def errors_of(excinfo) -> dict:
    return {k: " ".join(v) for k, v in excinfo.value.message_dict.items()}


def refused(wo, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, **kwargs)
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.repair_check == ""  # nothing saved
    return errors_of(e)


def last_note(wo) -> str:
    return wo.status_history.filter(to_status=WoStatus.COMPLETED).latest("created_at").note


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Hamilton Medical"
    user.save()
    return user


# --- the checklist ---------------------------------------------------------------------------------------------------------------


def test_a_repair_is_done_to_the_standard_post_repair_checklist_never_its_models_pm(ctx, vent, vent_model, techs):
    from apps.pm.models import PmProcedure

    vent_model.pm_procedure = PmProcedure.objects.create(code="HM-G5-PM6", name="Ventilator PM", checklist=["Inspect"])
    vent_model.save()
    wo = repair(vent, tech=techs["dana"])
    assert completion.procedure_for(wo) is repair_checks.POST_REPAIR
    assert [m for _t, m in STEPS] == [None, "Record Ω", "Record leakage µA", "Record leakage µA", None, None, None]
    assert completion.procedure_for(create_work_order(asset=vent, type=WoType.RECALL, priority="normal", problem="Recall")) is None


# --- checked and ready for use ------------------------------------------------------------------------------------------------------


def test_checked_records_every_step_and_returns_a_tagged_out_device(vent, techs):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    done = complete_work_order(wo, resolution="Replaced the flow sensor", signature=SIGNATURE, **CHECKED)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and done.returned and done.device_changed and not done.tagged_out
    assert (wo.status, wo.repair_check, wo.checks_reference, wo.resolution) == (WoStatus.COMPLETED, RepairCheck.PASSED, "", "Replaced the flow sensor")
    assert wo.checklist_results == [{"text": t, "measure": m, "result": "pass", "reading": r["reading"]}
                                    for (t, m), r in zip(STEPS, POST_REPAIR_ALL_PASS, strict=True)]
    assert last_note(wo) == "Checked after repair; CE-10001 back in service"
    assert completion.repair_note(wo, done) == last_note(wo)


def test_checked_on_a_device_that_stayed_in_service_says_only_checked(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    done = complete_work_order(wo, resolution="Tightened the pole clamp", **CHECKED)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and not done.returned and last_note(wo) == "Checked after repair"


def test_checked_keeps_a_device_another_repair_holds_out(vent, techs):
    first = repair(vent, tech=techs["dana"], tag_out=True)
    second = repair(vent, tech=techs["dana"], tag_out=True)
    done = complete_work_order(first, resolution="Fixed the alarm", **CHECKED)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE and not done.returned and last_note(first) == "Checked after repair"
    assert complete_work_order(second, resolution="Fixed the blower", **CHECKED).returned


def test_how_it_ended_is_required_in_words_naming_the_choices(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, resolution="Fixed") == {"repair_check": "Choose how the repair ended: checked and ready for use or not returned to use."}
    assert refused(wo, resolution="Fixed", repair_check="fine") == {"repair_check": "Choose checked and ready for use or not returned to use."}
    vendor = repair(vent, vendor="Hamilton Medical field service")
    assert refused(vendor, resolution="Fixed") == {
        "repair_check": "Choose how the repair ended: checked and ready for use, checked by the vendor, or not returned to use."}


def test_checked_needs_every_step_recorded(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, resolution="Fixed", repair_check=RepairCheck.PASSED) == {"checklist": completion.NO_CHECKS}
    assert refused(wo, resolution="Fixed", repair_check=RepairCheck.PASSED, results=[{"result": "pass"}]) == {
        "checklist": "Record a result for each of the 7 steps."}
    results = post_repair_results()
    results[2] = {"result": "pass", "reading": ""}
    results[5] = {"result": ""}
    assert refused(wo, resolution="Fixed", repair_check=RepairCheck.PASSED, results=results) == {
        "reading_3": "Step 3: record the reading.", "step_6": "Step 6: choose pass, fail, or N/A."}
    assert refused(wo, resolution="Fixed", repair_check=RepairCheck.PASSED, results="all pass") == {
        "checklist": "Send the checklist as a list: one {result, reading} per step."}


def test_the_visual_and_function_checks_are_never_na_and_the_electrical_ones_may_be(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, resolution="Fixed", repair_check=RepairCheck.PASSED, results=post_repair_results(*["na"] * 7)) == {
        "step_1": "Step 1 is never N/A after a repair: mark it pass or fail.", "step_5": "Step 5 is never N/A after a repair: mark it pass or fail."}
    # a door latch fixed at the bedside: the electrical tests did not apply
    results = post_repair_results("pass", "na", "na", "na", "pass", "na", "pass")
    complete_work_order(wo, resolution="Replaced the door latch", repair_check=RepairCheck.PASSED, results=results)
    assert [s["result"] for s in wo.checklist_results] == ["pass", "na", "na", "na", "pass", "na", "pass"]
    assert [s["reading"] for s in wo.checklist_results] == [""] * 7


def test_a_failed_check_refuses_checked(vent, techs):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    results = post_repair_results("pass", "pass", "fail", "pass", "pass", "pass", "pass")
    assert refused(wo, resolution="Fixed", repair_check=RepairCheck.PASSED, results=results) == {"step_3": completion.FAILED_CHECK}
    assert completion.FAILED_CHECK == ("A failed check means it is not ready for use: keep the repair open and hold the device out, or "
                                       "complete it as not returned to use.")
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE


def test_a_revised_checklist_is_caught_by_its_signature(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, resolution="Fixed", signature="0123456789abcdef", **CHECKED) == {"checklist": completion.REPAIR_REVISED}


def test_a_repairs_resolution_stays_required_whatever_the_outcome(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    for kwargs in (CHECKED, {"repair_check": RepairCheck.NOT_RETURNED}):
        assert refused(wo, resolution="  ", **kwargs) == {"resolution": "Say what was found and done."}


# --- checked by the vendor ------------------------------------------------------------------------------------------------------


def test_checked_by_the_vendor_takes_their_report_number_and_no_steps(vent):
    wo = repair(vent, vendor="Hamilton Medical field service", tag_out=True)
    vendor = {"resolution": "Vendor replaced the blower", "repair_check": RepairCheck.VENDOR_REPORT}
    assert refused(wo, **vendor) == {"checks_reference": "Enter the vendor's service report or field service order number."}
    assert refused(wo, **vendor, checks_reference="FSO 123") == {
        "checks_reference": "Enter the vendor's service report or field service order number as it is printed, without spaces."}
    assert refused(wo, **vendor, checks_reference="F" * 41) == {"checks_reference": "Keep the report number to 40 characters."}
    assert refused(wo, **vendor, checks_reference="FSO-123", results=POST_REPAIR_ALL_PASS) == {
        "checklist": "The vendor's service report is the record of their checks: send no checklist with checked by the vendor."}
    done = complete_work_order(wo, **vendor, checks_reference=" FSO-123 ", results=[{"result": ""}] * 7)  # an untouched checklist is none
    vent.refresh_from_db()
    assert (wo.repair_check, wo.checks_reference, wo.checklist_results) == (RepairCheck.VENDOR_REPORT, "FSO-123", [])
    assert vent.status == AssetStatus.IN_SERVICE and done.returned
    assert last_note(wo) == "Checked by the vendor, report FSO-123; CE-10001 back in service"
    assert repair_checks.checked_by(wo) == "Hamilton Medical field service"


def test_checked_by_the_vendor_only_on_vendor_service_and_never_by_the_vendors_own_account(vent, techs, vendor_user):
    in_house = repair(vent, tech=techs["dana"])
    assert completion.repair_outcomes(in_house) == [RepairCheck.PASSED, RepairCheck.NOT_RETURNED]
    assert refused(in_house, resolution="Fixed", repair_check=RepairCheck.VENDOR_REPORT, checks_reference="FSO-1") == {
        "repair_check": f"{in_house.number} was not done by vendor service. Record the checks after the repair, or choose not returned to use."}
    theirs = repair(vent, vendor="Hamilton Medical field service")
    assert completion.repair_outcomes(theirs) == list(RepairCheck.values)
    assert completion.repair_outcomes(theirs, vendor_user) == [RepairCheck.PASSED, RepairCheck.NOT_RETURNED]
    assert "Checked by the vendor is for the facility's CE staff" in refused(
        theirs, resolution="Fixed", repair_check=RepairCheck.VENDOR_REPORT, checks_reference="FSO-1", by=vendor_user)["repair_check"]
    complete_work_order(theirs, resolution="Replaced the blower", by=vendor_user, **CHECKED)  # their own account records the steps
    assert theirs.repair_check == RepairCheck.PASSED and len(theirs.checklist_results) == 7


def test_a_report_number_only_with_checked_by_the_vendor(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, resolution="Fixed", checks_reference="FSO-1", **CHECKED) == {"checks_reference": completion.REFERENCE_ONLY}
    # with no outcome yet, the outcome is what to fix
    assert set(refused(wo, resolution="Fixed", checks_reference="FSO-1")) == {"repair_check"}


# --- not returned to use --------------------------------------------------------------------------------------------------------


def test_not_returned_takes_a_device_in_service_out_and_never_returns_it(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED, results=POST_REPAIR_ALL_PASS) == {
        "checklist": "A device not returned to use records no checks: send no checklist with not returned to use."}
    done = complete_work_order(wo, resolution="Cracked chassis: beyond economical repair", repair_check=RepairCheck.NOT_RETURNED)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE and done.tagged_out and not done.returned and done.device_changed
    assert (wo.repair_check, wo.tagged_out, wo.checklist_results) == (RepairCheck.NOT_RETURNED, True, [])
    assert last_note(wo) == "Not returned to use: CE-10001 stays out of service"
    assert vent.history.latest("history_date").history_change_reason == f"Not returned to use after repair {wo.number}"


def test_not_returned_keeps_a_tagged_out_or_in_repair_device_out_and_reopening_it_checked_returns_it(vent, pump, techs):
    tagged = repair(vent, tech=techs["dana"], tag_out=True)
    done = complete_work_order(tagged, resolution="Waiting on a decision to replace", repair_check=RepairCheck.NOT_RETURNED)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE and not done.tagged_out and not done.device_changed
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.IN_REPAIR)
    in_repair = repair(pump, tech=techs["dana"])
    done = complete_work_order(in_repair, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED)
    pump.refresh_from_db()
    assert pump.status == AssetStatus.OUT_OF_SERVICE and not done.tagged_out and done.device_changed and in_repair.tagged_out
    # reopened: the repair, still tagged out, holds the device out; completing it checked returns it
    change_status(tagged, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError, match=f"Complete {tagged.number} with its checks after repair"):
        eq.set_status(vent, AssetStatus.IN_SERVICE)
    assert complete_work_order(tagged, resolution="Replaced the board after all", **CHECKED).returned
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and tagged.repair_check == RepairCheck.PASSED


def test_not_returned_on_a_held_device_leaves_it_as_its_hold_keeps_it(vent, techs):
    incident = held_incident(vent)
    wo = incident.work_order
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    complete_work_order(wo, resolution="Failed the bench test", repair_check=RepairCheck.NOT_RETURNED)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert last_note(wo) == "Not returned to use: CE-10001 stays out of service"


def test_not_returned_on_loan_takes_it_out_of_use(vent, techs):
    eq.set_status(vent, AssetStatus.ON_LOAN)
    wo = repair(vent, tech=techs["dana"])
    assert complete_work_order(wo, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED).tagged_out
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE


# --- other types ----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("type_", [WoType.RECALL, WoType.SAFETY])
def test_another_type_records_no_checks_after_repair(vent, techs, type_):
    wo = create_work_order(asset=vent, type=type_, priority="normal", problem="Work", assigned_to=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, resolution="Done", results=POST_REPAIR_ALL_PASS, repair_check=RepairCheck.PASSED, checks_reference="FSO-1")
    assert errors_of(e) == {"pm_result": completion.REPAIR_RESULTS_ONLY, "repair_check": completion.REPAIR_CHECK_ONLY,
                            "checks_reference": completion.REFERENCE_ONLY}
    complete_work_order(wo, resolution="Done")
    assert wo.repair_check == "" and completion.repair_outcomes(wo) == []


# --- keeping the repair open after a failed check ------------------------------------------------------------------------------


def test_keep_open_records_the_failed_checks_and_holds_the_device_out(vent, techs, today):
    wo = repair(vent, tech=techs["dana"])
    results = post_repair_results("pass", "pass", "fail", "pass", "pass", "pass", "pass")
    results[2]["reading"] = "640 µA"
    kept = keep_open(wo, results=results, signature=SIGNATURE)
    vent.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.tagged_out and kept.held and kept.tagged_out
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    assert kept.note.text == (f"Checks after repair failed on {today:%b} {today.day}, {today.year}: 3. Equipment (touch) leakage (N/A as "
                              "for protective earth) (reading 640 µA)")
    assert wo.status_history.latest("created_at").note == "Device tagged out of service: checks after repair failed"
    assert vent.history.latest("history_date").history_change_reason == f"Tagged out: checks after repair {wo.number} failed"
    with pytest.raises(ValidationError, match="Complete .* with its checks after repair"):
        eq.set_status(vent, AssetStatus.IN_SERVICE)
    # put right, checked, and back
    assert complete_work_order(wo, resolution="Replaced the line cord", **CHECKED).returned
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_keep_open_without_the_hold_only_records(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    kept = keep_open(wo, results=[{"result": "fail"}, *[{"result": ""}] * 6], hold=False)  # stopped at the first failure
    vent.refresh_from_db()
    assert not kept.held and not wo.tagged_out and vent.status == AssetStatus.IN_SERVICE
    assert kept.note.text.endswith(": 1. Visual: housing, cords, plugs, mounts, and accessories intact")
    assert not WorkOrderStatusHistory.objects.filter(work_order=wo, note__startswith="Device").exists()


def test_keep_open_refusals(vent, techs, make_user, tenant, other_tenant):
    wo = repair(vent, tech=techs["dana"])
    for results, message in ((POST_REPAIR_ALL_PASS, "No check failed. Complete the repair as checked and ready for use."),
                             ([{"result": ""}] * 7, "Record the check that failed, with its reading when it has one."),
                             (post_repair_results("pass", "fail", *["pass"] * 5)[:3], "Record a result for each of the 7 steps.")):
        with pytest.raises(ValidationError) as e:
            keep_open(wo, results=results)
        assert errors_of(e) == {"checklist": message}
    failed = post_repair_results("fail", *["pass"] * 6)
    failed[1]["reading"] = ""
    with pytest.raises(ValidationError) as e:
        keep_open(wo, results=failed)
    assert errors_of(e) == {"reading_2": "Step 2: record the reading."}
    with pytest.raises(ValidationError) as e:
        keep_open(wo, results=post_repair_results("fail", *["pass"] * 6), signature="0123456789abcdef")
    assert errors_of(e) == {"checklist": completion.REPAIR_REVISED}
    with pytest.raises(PermissionDenied, match="Work orders Edit"):
        keep_open(wo, results=post_repair_results("fail", *["pass"] * 6), by=make_user("analyst"))
    not_started = repair(vent, tech=techs["dana"], start=False)
    with pytest.raises(ValidationError, match="has not been started"):
        keep_open(not_started, results=post_repair_results("fail", *["pass"] * 6))
    recall = create_work_order(asset=vent, type=WoType.RECALL, priority="normal", problem="Recall", assigned_to=techs["dana"])
    with pytest.raises(ValidationError, match="is not a repair"):
        keep_open(recall, results=post_repair_results("fail", *["pass"] * 6))
    with tenant_context(other_tenant), pytest.raises(ValidationError, match="from this facility"):
        keep_open(wo, results=post_repair_results("fail", *["pass"] * 6))
    assert not WorkOrderNote.objects.filter(work_order=wo).exists() and not WorkOrder.objects.get(pk=wo.pk).tagged_out
    keep_open(wo, results=post_repair_results("fail", *["pass"] * 6), by=make_user("technician"))
    assert WorkOrderNote.objects.filter(work_order=wo).count() == 1


def test_keep_open_on_a_device_in_repair_marks_the_repair_without_moving_it(pump, techs):
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.IN_REPAIR)
    wo = repair(pump, tech=techs["dana"])
    kept = keep_open(wo, results=post_repair_results("pass", "fail", *["pass"] * 5))
    pump.refresh_from_db()
    assert kept.held and not kept.tagged_out and wo.tagged_out and pump.status == AssetStatus.IN_REPAIR  # its repair goes on
    assert wo.status_history.latest("created_at").note == "Device held out of service: checks after repair failed"


# --- the doors around the checks ------------------------------------------------------------------------------------------------


def test_a_device_a_repair_holds_out_never_goes_in_service_or_on_loan_by_hand(vent, pump, techs, make_user):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    vent.refresh_from_db()
    message = f"CE-10001 is held out of use by repair {wo.number}. Complete {wo.number} with its checks after repair."
    assert eq.repair_hold_message(vent) == message
    for to in (AssetStatus.IN_SERVICE,):
        with pytest.raises(ValidationError) as e:
            eq.set_status(vent, to)
        assert e.value.messages == [message]
    director = make_user("director")
    assert [a["to"] for a in eq.status_actions(vent, director)] == [AssetStatus.MISSING, AssetStatus.RETIRED]
    eq.set_status(vent, AssetStatus.MISSING)  # other moves stay
    with pytest.raises(ValidationError, match="Complete .* with its checks after repair"):
        eq.set_status(vent, AssetStatus.IN_SERVICE)  # found: still held by its repair
    eq.set_status(vent, AssetStatus.OUT_OF_SERVICE)
    # a device in repair with any open repair
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.IN_REPAIR)
    pump.refresh_from_db()
    open_repair = repair(pump, tech=techs["dana"])
    assert eq.repair_hold_message(pump) == f"CE-10002 is in repair ({open_repair.number}). Complete {open_repair.number} with its checks after repair."
    with pytest.raises(ValidationError, match="is in repair"):
        eq.set_status(pump, AssetStatus.IN_SERVICE)
    assert AssetStatus.IN_SERVICE not in [a["to"] for a in eq.status_actions(pump, director)]
    complete_work_order(open_repair, resolution="Fixed", **CHECKED)
    pump.refresh_from_db()
    assert pump.status == AssetStatus.IN_SERVICE and eq.repair_hold_message(pump) == ""


def test_a_device_tagged_out_by_hand_or_out_with_an_untagged_repair_comes_back_by_hand(vent, techs, make_user):
    eq.set_status(vent, AssetStatus.OUT_OF_SERVICE)
    wo = repair(vent, tech=techs["dana"])  # opened without the tag-out: not why it is out
    assert eq.repair_hold_message(vent) == "" and AssetStatus.IN_SERVICE in [a["to"] for a in eq.status_actions(vent, make_user("director"))]
    eq.set_status(vent, AssetStatus.IN_SERVICE)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and wo.status == WoStatus.IN_PROGRESS


def test_the_drawers_button_and_the_api_refuse_it_too(client, vent, techs, make_user):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    client.force_login(make_user("director"))
    drawer = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert "Return to service" not in drawer
    body = client.post(f"/equipment/{vent.tag}/status/", {"to": AssetStatus.IN_SERVICE}, **HX)
    assert "with its checks after repair" in body.headers.get("HX-Trigger", "") or "with its checks after repair" in body.content.decode()
    r = client.post(f"/api/v1/assets/{vent.id}/status/", {"to": AssetStatus.IN_SERVICE}, content_type="application/json")
    assert r.status_code == 400 and f"Complete {wo.number} with its checks after repair" in str(r.json())
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE


def test_the_devices_import_keeps_the_status_of_a_device_a_repair_holds_out(vent, techs, make_user):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    data = "\r\n".join(["Tag,Manufacturer,Model,Room,Status", "CE-10001,,,7,In service", ""]).encode()
    user = make_user("director")
    run = imports.upload(user, "devices", "inventory.csv", data)
    run = imports.confirm_columns(run, user, imports.mapping_of(run))
    for _ in range(2):
        run = imports.process(run, user)
        while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
            run = imports.process(run, user)
        if run.status == ImportRun.Status.CHECKED:
            run = imports.start_import(run, user)
    notes = {g["note"]: g["keys"] for g in imports.problem_groups(run)}
    assert notes == {f"Status kept: CE-10001 is held out of use by repair {wo.number}. Complete {wo.number} with its checks after repair.":
                     ["CE-10001"]}
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE and vent.room == "7"


# --- an incident's suspect device ------------------------------------------------------------------------------------------------


def _investigated(vent, techs, **completion_kwargs):
    incident = make_incident(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    wo = incident.work_order
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    complete_work_order(wo, resolution="Bench tested", **completion_kwargs)
    return incident, wo


def test_the_suspect_device_returns_to_use_only_after_its_investigation_was_checked(vent, techs):
    incident, wo = _investigated(vent, techs, repair_check=RepairCheck.NOT_RETURNED)
    hold = incident.holds.get()
    with pytest.raises(ValidationError) as e:
        inc.release(hold, Release.RETURN_TO_USE)
    assert e.value.message_dict == {"release": [f"Reopen investigation {wo.number} and complete it with its checks after repair, or release "
                                                "CE-10001 kept out of service."]}
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened
    complete_work_order(wo, resolution="Bench tested again after the fix", **CHECKED)
    inc.release(hold, Release.RETURN_TO_USE)
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.IN_SERVICE


def test_a_vendors_report_lets_it_return_and_a_completion_from_before_slice_30_does_not(vent, pump, techs):
    incident, wo = _investigated(vent, techs, **CHECKED)
    WorkOrder.objects.filter(pk=wo.pk).update(repair_check="")  # completed before slice 30
    with pytest.raises(ValidationError, match=f"Reopen investigation {wo.number}"):
        inc.release(incident.holds.get(), Release.RETURN_TO_USE)
    inc.release(incident.holds.get(), Release.KEEP_OUT)  # kept out of service: allowed
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    other = make_incident(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    assign(other.work_order, vendor_name="BD field service")
    change_status(other.work_order, WoStatus.IN_PROGRESS)
    complete_work_order(other.work_order, resolution="Vendor bench test", repair_check=RepairCheck.VENDOR_REPORT, checks_reference="BD-77")
    inc.release(other.holds.get(), Release.RETURN_TO_USE)
    pump.refresh_from_db()
    assert pump.status == AssetStatus.IN_SERVICE


# --- a vendor loaner's device of ours --------------------------------------------------------------------------------------------


def test_a_device_whose_last_repair_ended_not_returned_is_not_back_for_its_loaner(vent, pump, pump_model, dept, techs, today):
    wo = repair(pump, vendor="BD field service")
    loaner = eq.add_temporary_device(tag="L-1", device_model=pump_model, department=dept, kind=Ownership.LOANER, owner="BD field service",
                                     serial="BD-L-1", arrived_on=today - timedelta(days=3), stands_in_for=pump, added_as=AddedAs.EXISTING,
                                     today=today)

    def back() -> tuple[bool, bool]:
        pump.refresh_from_db()
        return eq.loaner_device_back(pump), Asset.objects.filter(eq.loaner_back_q(), pk=loaner.pk).exists()

    assert back() == (False, False)  # at the vendor
    complete_work_order(wo, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED)
    assert back() == (False, False) and pump.status == AssetStatus.OUT_OF_SERVICE
    eq.set_status(pump, AssetStatus.IN_SERVICE)  # put back by hand: nothing holds it, but its checks were never recorded
    assert back() == (False, False)
    second = repair(pump, tech=techs["dana"])
    complete_work_order(second, resolution="Replaced the pump module", **CHECKED)
    assert back() == (True, True)
    third = repair(pump, tech=techs["dana"])
    complete_work_order(third, resolution="Beyond repair after all", repair_check=RepairCheck.NOT_RETURNED)
    eq.set_status(pump, AssetStatus.RETIRED)
    assert back() == (True, True)  # retired: the loaner can go


# --- the API and the modal ---------------------------------------------------------------------------------------------------


def test_the_api_completes_a_repair_with_its_checks(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"], tag_out=True)

    def post(body):
        return client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "completed", "resolution": "Fixed", **body},
                           content_type="application/json")

    r = post({})
    assert r.status_code == 400 and "repair_check" in r.json()
    r = post({"repair_check": "passed", "results": "everything"})
    assert r.status_code == 400 and r.json() == {"checklist": ["Send the checklist as a list: one {result, reading} per step."]}
    r = post({"repair_check": ["passed"]})
    assert r.status_code == 400 and "repair_check" in r.json()
    r = post({"repair_check": "passed", "results": POST_REPAIR_ALL_PASS})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    wo.refresh_from_db()
    vent.refresh_from_db()
    assert wo.repair_check == RepairCheck.PASSED and vent.status == AssetStatus.IN_SERVICE


def test_the_api_takes_a_vendors_report_number(client, vent, make_user):
    client.force_login(make_user("manager"))
    wo = repair(vent, vendor="Hamilton Medical field service")
    r = client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "completed", "resolution": "Vendor fixed it",
                                                                 "repair_check": "vendor_report", "checks_reference": "FSO 9"},
                    content_type="application/json")
    assert r.status_code == 400 and list(r.json()) == ["checks_reference"]
    r = client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "completed", "resolution": "Vendor fixed it",
                                                                 "repair_check": "vendor_report", "checks_reference": "FSO-9"},
                    content_type="application/json")
    wo.refresh_from_db()
    assert r.status_code == 200 and (wo.repair_check, wo.checks_reference) == (RepairCheck.VENDOR_REPORT, "FSO-9")


def test_the_modal_asks_how_it_ended_and_saves_it(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    body = client.get(f"/work-orders/{wo.number}/complete/", **HX).content.decode()
    assert "After the repair" in body and 'name="repair_check" value="passed"' in body and 'value="vendor_report"' not in body
    assert 'name="checks_reference"' not in body and "Protective earth resistance" in body
    r = client.post(f"/work-orders/{wo.number}/complete/", {"resolution": "Fixed"}, **HX)
    assert "Choose how the repair ended" in r.content.decode()
    r = client.post(f"/work-orders/{wo.number}/complete/", checked_post("pass", "pass", "fail", *["pass"] * 4, resolution="Fixed"), **HX)
    assert "A failed check means it is not ready for use" in r.content.decode()
    r = client.post(f"/work-orders/{wo.number}/complete/", checked_post(resolution="Replaced the sensor"), **HX)
    wo.refresh_from_db()
    assert r.status_code == 200 and wo.repair_check == RepairCheck.PASSED and "Checked after repair; CE-10001 back in service" in r["HX-Trigger"]
    # not returned: the steps left on screen are not sent
    other = repair(vent, tech=techs["dana"])
    r = client.post(f"/work-orders/{other.number}/complete/", checked_post(resolution="Beyond repair", repair_check="not_returned"), **HX)
    other.refresh_from_db()
    assert other.repair_check == RepairCheck.NOT_RETURNED and other.checklist_results == []


def test_the_modal_offers_the_vendors_report_on_vendor_service_to_ce_only(client, vent, make_user, vendor_user):
    wo = repair(vent, vendor="Hamilton Medical field service")
    client.force_login(make_user("manager"))
    body = client.get(f"/work-orders/{wo.number}/complete/", **HX).content.decode()
    assert 'value="vendor_report"' in body and 'name="checks_reference"' in body
    client.force_login(vendor_user)
    body = client.get(f"/work-orders/{wo.number}/complete/", **HX).content.decode()
    assert 'value="vendor_report"' not in body and 'name="checks_reference"' not in body
    client.force_login(make_user("manager", username="mgr2@riverside.example"))
    r = client.post(f"/work-orders/{wo.number}/complete/", {"resolution": "Vendor fixed it", "repair_check": "vendor_report",
                                                             "checks_reference": "FSO-12", "step_1": "pass"}, **HX)
    wo.refresh_from_db()
    assert r.status_code == 200 and (wo.repair_check, wo.checks_reference, wo.checklist_results) == (RepairCheck.VENDOR_REPORT, "FSO-12", [])


# --- the demo ---------------------------------------------------------------------------------------------------------------------


def test_the_demo_seeds_each_outcome(db):
    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        done = WorkOrder.objects.filter(type=WoType.REPAIR, status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))
        assert done.exists() and not done.filter(repair_check="").exists()
        assert set(done.values_list("repair_check", flat=True)) == set(RepairCheck.values)
        checked = done.filter(repair_check=RepairCheck.PASSED)
        assert all(len(w.checklist_results) == 7 and all(s["result"] == "pass" for s in w.checklist_results) for w in checked)
        vendor = done.get(repair_check=RepairCheck.VENDOR_REPORT)
        assert vendor.vendor_service and vendor.checks_reference == seed_demo.VENDOR_REPORT_NUMBER and vendor.checklist_results == []
        bed = done.get(repair_check=RepairCheck.NOT_RETURNED)
        assert bed.asset.device_model.model == seed_demo.NOT_RETURNED_MODEL and bed.asset.status == AssetStatus.RETIRED
        assert bed.resolution == seed_demo.NOT_RETURNED_RESOLUTION and bed.status == WoStatus.CLOSED
        today = timezone.localdate()
        assert bed.completed_on == today - timedelta(days=seed_demo.NOT_RETURNED_DONE_DAYS_AGO)
        reasons = [(r.status, r.history_change_reason) for r in bed.asset.history.order_by("history_date", "history_id")][-2:]
        assert reasons == [(AssetStatus.OUT_OF_SERVICE, f"Not returned to use after repair {bed.number}"),
                           (AssetStatus.RETIRED, seed_demo.NOT_RETURNED_RETIRED)]
        from apps.incidents.models import Incident, Status

        closed = Incident.objects.get(status=Status.CLOSED)
        assert closed.work_order.repair_check == RepairCheck.PASSED
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        done = WorkOrder.objects.filter(type=WoType.REPAIR, status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))
        assert done.count() == 2 and set(done.values_list("repair_check", flat=True)) == {RepairCheck.PASSED}


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_checks_under_the_policies(vent, pump, techs, make_user):
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    other = repair(pump, tech=techs["dana"])
    as_app_role()
    with pytest.raises(ValidationError, match="with its checks after repair"):
        eq.set_status(vent, AssetStatus.IN_SERVICE)
    keep_open(other, results=post_repair_results("pass", "fail", *["pass"] * 5), by=make_user("technician"))
    complete_work_order(other, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED)
    assert complete_work_order(wo, resolution="Fixed", **checked_repair(signature=SIGNATURE)).returned
    vent.refresh_from_db()
    pump.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and pump.status == AssetStatus.OUT_OF_SERVICE
