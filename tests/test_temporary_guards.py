"""
Slice 29, wave 1b: the guards outside equipment for a temporary device (a rental, vendor loaner, or demo unit; its owner maintains it).

- workorders.services.create_work_order refuses a PM on one, keyed type ("its owner maintains it"), through New work order and the API
  too; other types open as for our devices; completing a PM never sets a temporary device's next PM.
- Its incoming inspection is done to inspections.TEMPORARY_INCOMING (the incoming checklist, the owner's PM label, the model's open
  recalls), never its model's PM procedure; open_for estimates hours from the checklist used.
- Completing its inspection as passed is refused, keyed inspection_result, while its owner's PM date is recorded and past (the screen's
  modal and the API say the same); failed is not; a kept pass is not.
- A recall batch opens its recall work order as vendor service named for its owner (equipment.services.service_vendor), never assigned
  to an in-house technician, and it is never counted unassigned.
- An incident's release refuses return to use while its owner's PM date is past, as a past next PM does for ours.

The devices are added through equipment.services.create_asset's temporary path (the scaffold's); the owner's PM date is changed in place
where equipment.services.update_temporary (wave 1a) would change it.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.credentials.models import Credential
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, Ownership
from apps.incidents import services as inc
from apps.incidents.models import Affected, Outcome, Release
from apps.pm.models import PmProcedure
from apps.recalls import services as rc
from apps.workorders import inspections
from apps.workorders.completion import checklist_of, checklist_signature, complete_work_order, inspection_procedure, procedure_for
from apps.workorders.models import InspectionResult, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, no_pm_message

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
PASSED, FAILED = InspectionResult.PASSED, InspectionResult.FAILED
LEAKAGE = "42"


@pytest.fixture
def today(ctx):
    return timezone.localdate()  # the facility's day, as the services read it


def temporary(device_model, tag="T-1", *, kind=Ownership.RENTAL, owner="Acme Rentals", owner_pm_due_on=None, waiting=True, today=None):
    """A temporary device added through create_asset's temporary path: new and waiting for its incoming inspection, or (waiting=False)
    already on site when entered, in service."""
    today = today or timezone.localdate()
    department = Department.objects.get_or_create(name="Biomed receiving")[0]
    return eq.create_asset(tag=tag, device_model=device_model, department=department, serial=f"SN-{tag}", ownership=kind, owner=owner,
                           owner_reference="RA-2026-0042", arrived_on=today, due_back_on=today + 14 * DAY, owner_pm_due_on=owner_pm_due_on,
                           incoming_inspection=eq.INCOMING_WAITING if waiting else "", added_as=AddedAs.NEW if waiting else AddedAs.EXISTING,
                           today=today)


def results(*values, reading=LEAKAGE) -> list[dict]:
    """Results for the temporary incoming checklist, pass / fail / na per step (all pass when none given), the leakage on step 2."""
    values = values or ("pass",) * len(inspections.TEMPORARY_CHECKLIST)
    return [{"result": v, "reading": reading if measure and v != "na" else ""}
            for v, (_text, measure) in zip(values, inspections.TEMPORARY_CHECKLIST, strict=True)]


def owner_pm_on(asset, day):
    """The owner's PM date recorded anew (equipment.services.update_temporary's field)."""
    Asset.objects.filter(pk=asset.pk).update(owner_pm_due_on=day)
    asset.refresh_from_db()


def with_procedure(model, hours="2.5"):
    proc = PmProcedure.objects.create(code="BD-PCU-PM12", name="Alaris PCU 12-month PM", estimated_hours=Decimal(hours),
                                      checklist=["Inspect and clean", {"text": "Leakage current", "measure": "µA, limit 100"}, "Alarm check"])
    model.pm_procedure = proc
    model.save()
    return proc


def errors_of(e) -> dict:
    return {k: " ".join(v) for k, v in e.value.message_dict.items()}


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


@pytest.fixture
def signed_in(client, make_user):
    n = iter(range(100))

    def _as(slug):
        user = make_user(slug, username=f"{slug}-{next(n)}@riverside.example")
        client.force_login(user)
        return user

    return _as


# --- no PM work orders ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [Ownership.RENTAL, Ownership.LOANER, Ownership.DEMO])
def test_a_temporary_device_gets_no_pm_work_order(today, pump_model, pump, kind):
    t = temporary(pump_model, kind=kind, waiting=False)
    with pytest.raises(ValidationError) as e:
        create_work_order(asset=t, type=WoType.PM, priority="normal", problem="Scheduled PM")
    words = {Ownership.RENTAL: "a rental", Ownership.LOANER: "a vendor loaner", Ownership.DEMO: "a demo or evaluation unit"}[kind]
    assert errors_of(e) == {"type": f"T-1 is {words}: its owner maintains it, so it gets no PM work orders here."} and no_pm_message(t) == errors_of(e)["type"]
    assert not WorkOrder.objects.filter(asset=t).exists()
    for wo_type in (WoType.REPAIR, WoType.RECALL, WoType.SAFETY, WoType.INSPECTION):  # everything else opens as for ours
        assert create_work_order(asset=t, type=wo_type, priority="normal", problem="Check it").asset == t
    assert create_work_order(asset=pump, type=WoType.PM, priority="normal", problem="Scheduled PM").type == WoType.PM  # ours: as always


def test_a_pm_completed_on_a_temporary_device_sets_no_next_pm(today, pump_model):
    """No door opens one; were one there all the same (here written on the device as ours first), its completion records the PM and
    starts no clock."""
    t = temporary(pump_model, waiting=False)
    Asset.objects.filter(pk=t.pk).update(ownership=Ownership.OWNED)
    pm = create_work_order(asset=Asset.objects.get(pk=t.pk), type=WoType.PM, priority="normal", problem="Scheduled PM")
    Asset.objects.filter(pk=t.pk).update(ownership=Ownership.RENTAL)
    pm = WorkOrder.objects.get(pk=pm.pk)
    change_status(pm, WoStatus.IN_PROGRESS)
    change_status(pm, WoStatus.COMPLETED)
    t.refresh_from_db()
    assert t.last_pm_on == today and t.next_pm_on is None


def test_new_work_order_and_the_api_show_the_refusal(client, signed_in, today, pump_model):
    t = temporary(pump_model, waiting=False)
    signed_in("manager")
    r = client.post("/work-orders/new/", {"asset": t.tag, "type": "pm", "priority": "normal", "problem": "Scheduled PM"}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert '<span class="err" role="alert">T-1 is a rental: its owner maintains it, so it gets no PM work orders here.</span>' in body
    signed_in("director")
    body = {"asset": str(t.id), "type": WoType.PM, "problem": "Scheduled PM", "priority": "normal", "due_on": (today + 3 * DAY).isoformat()}
    r = client.post("/api/v1/work-orders/", body, content_type="application/json")
    assert r.status_code == 400 and r.json()["type"] == ["T-1 is a rental: its owner maintains it, so it gets no PM work orders here."]
    assert not WorkOrder.objects.filter(asset=t).exclude(type=WoType.INSPECTION).exists()
    r = client.post("/api/v1/work-orders/", {**body, "type": WoType.REPAIR, "problem": "Rail latch sticks"}, content_type="application/json")
    assert r.status_code == 201, r.json()


# --- the incoming checklist -------------------------------------------------------------------------------------------------------

def test_a_temporary_devices_inspection_is_done_to_the_temporary_checklist(today, pump_model):
    steps = [text for text, _m in inspections.TEMPORARY_CHECKLIST]
    assert steps[:len(inspections.INCOMING_CHECKLIST)] == [text for text, _m in inspections.INCOMING_CHECKLIST]
    assert steps[len(inspections.INCOMING_CHECKLIST):] == ["Owner's PM label current (its due date recorded)"]
    t = temporary(pump_model)
    wo = inspections.open_inspection(t)
    assert procedure_for(wo) is inspections.TEMPORARY_INCOMING and checklist_of(procedure_for(wo)) == inspections.TEMPORARY_CHECKLIST
    assert inspections.is_incoming_checklist(inspections.TEMPORARY_INCOMING) and inspections.TEMPORARY_INCOMING.temporary
    assert not inspections.INCOMING.temporary
    proc = with_procedure(pump_model)  # its model's PM procedure is the owner's to do, never CE's checklist for it
    wo = WorkOrder.objects.get(pk=wo.pk)
    assert procedure_for(wo) is inspections.TEMPORARY_INCOMING
    ours = eq.create_asset(tag="CE-1", device_model=pump_model, department=t.department, incoming_inspection=eq.INCOMING_WAITING)
    assert procedure_for(inspections.open_inspection(ours)) == proc  # ours: as slice 26 had it
    assert inspection_procedure(ours) == proc and inspection_procedure(t) is inspections.TEMPORARY_INCOMING


def test_the_inspections_estimated_hours_follow_the_checklist_used(today, pump_model, vent_model):
    with_procedure(pump_model, hours="2.5")
    t = temporary(pump_model)
    assert inspections.open_inspection(t).estimated_hours == 1  # the temporary checklist, whatever the model's procedure
    ours = eq.create_asset(tag="CE-1", device_model=pump_model, department=t.department, incoming_inspection=eq.INCOMING_WAITING)
    assert inspections.open_inspection(ours).estimated_hours == Decimal("2.5")  # its PM procedure is the checklist
    empty = PmProcedure.objects.create(code="EMPTY", name="No steps yet", estimated_hours=Decimal("3"), checklist=[])
    vent_model.pm_procedure = empty
    vent_model.save()
    vent = eq.create_asset(tag="CE-2", device_model=vent_model, department=t.department, incoming_inspection=eq.INCOMING_WAITING)
    assert inspections.open_inspection(vent).estimated_hours == 1  # the incoming checklist: not the empty procedure's hours


def test_the_temporary_checklist_is_recorded_and_passes(today, pump_model, techs):
    t = temporary(pump_model, owner_pm_due_on=today + 60 * DAY)
    wo = inspections.open_inspection(t)
    assign(wo, technician=techs["dana"])
    done = complete_work_order(wo, inspection_result=PASSED, results=results(),
                               signature=checklist_signature(inspections.TEMPORARY_CHECKLIST))
    wo.refresh_from_db()
    t.refresh_from_db()
    assert done.passed and wo.status == WoStatus.COMPLETED and len(wo.checklist_results) == len(inspections.TEMPORARY_CHECKLIST)
    assert wo.resolution == "Incoming inspection passed per the incoming checklist, all checks passed"
    assert t.status == AssetStatus.IN_SERVICE and not t.awaiting_inspection
    other = temporary(pump_model, tag="T-2")
    owi = inspections.open_inspection(other)
    assign(owi, technician=techs["dana"])
    with pytest.raises(ValidationError) as e:  # the incoming checklist's five steps are not this one
        complete_work_order(owi, inspection_result=PASSED, results=results()[:5])
    assert errors_of(e) == {"checklist": f"Record a result for each of the {len(inspections.TEMPORARY_CHECKLIST)} steps."}
    with pytest.raises(ValidationError) as e:  # results typed against the incoming checklist's steps
        complete_work_order(owi, inspection_result=PASSED, results=results(), signature=checklist_signature(inspections.INCOMING_CHECKLIST))
    assert errors_of(e) == {"checklist": "The procedure's checklist was revised while this was open. Check the steps again."}


# --- the owner's PM date and the pass ---------------------------------------------------------------------------------------------

def test_a_pass_is_refused_while_the_owners_pm_date_is_past(today, pump_model, techs):
    t = temporary(pump_model, owner_pm_due_on=today - DAY)
    wo = inspections.open_inspection(t)
    assign(wo, technician=techs["dana"])
    words = f"The owner's PM was due {day(today - DAY)}: have the owner do it, or record its new date."
    assert inspections.owner_pm_refusal(t, today) == words
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=results())
    assert errors_of(e) == {"inspection_result": words}
    with pytest.raises(ValidationError) as e:  # without the checklist: the same answer, and the resolution's
        complete_work_order(wo, inspection_result=PASSED)
    assert errors_of(e) == {"inspection_result": words, "resolution": "Record the checklist, or say what was checked."}
    with pytest.raises(ValidationError) as e:  # a failed step answers first: Passed is wrong whatever the date
        complete_work_order(wo, inspection_result=PASSED, results=results("pass", "fail", "pass", "pass", "pass", "pass"))
    assert errors_of(e) == {"inspection_result": "Step 2 failed. A device that fails a check fails its incoming inspection: choose Failed."}
    wo.refresh_from_db()
    t.refresh_from_db()
    assert wo.status == WoStatus.OPEN and t.awaiting_inspection and t.status == AssetStatus.OUT_OF_SERVICE  # nothing saved
    owner_pm_on(t, today)  # the owner's new sticker: due today is not past
    assert inspections.owner_pm_refusal(t, today) == ""
    complete_work_order(wo, inspection_result=PASSED, results=results())
    t.refresh_from_db()
    assert t.status == AssetStatus.IN_SERVICE and not t.awaiting_inspection


def test_a_pass_with_no_owners_pm_date_recorded_is_not_refused(today, pump_model, techs):
    t = temporary(pump_model, owner_pm_due_on=None)
    wo = inspections.open_inspection(t)
    assign(wo, technician=techs["dana"])
    assert inspections.owner_pm_refusal(t, today) == ""
    complete_work_order(wo, inspection_result=PASSED, results=results())
    t.refresh_from_db()
    assert not t.awaiting_inspection


def test_a_fail_is_recorded_whatever_the_owners_pm_date(today, pump_model, techs):
    t = temporary(pump_model, owner_pm_due_on=today - 30 * DAY)
    wo = inspections.open_inspection(t)
    assign(wo, technician=techs["dana"])
    done = complete_work_order(wo, inspection_result=FAILED, resolution="Owner's PM label out of date",
                               results=results("pass", "pass", "pass", "pass", "pass", "fail"))
    t.refresh_from_db()
    assert done.reinspection_opened and done.reinspection.type == WoType.INSPECTION and done.reinspection.assigned_to == techs["dana"]
    assert t.awaiting_inspection and t.status == AssetStatus.OUT_OF_SERVICE
    assert procedure_for(done.reinspection) is inspections.TEMPORARY_INCOMING and done.reinspection.estimated_hours == 1


def test_a_kept_pass_is_never_refused(today, pump_model, techs):
    """A reopened inspection whose pass ended the wait stays passed when completed again (slice 26): refusing it would leave nothing to
    complete it with (Failed is refused there)."""
    t = temporary(pump_model, owner_pm_due_on=today + 10 * DAY)
    wo = inspections.open_inspection(t)
    assign(wo, technician=techs["dana"])
    complete_work_order(wo, inspection_result=PASSED, results=results())
    change_status(WorkOrder.objects.get(pk=wo.pk), WoStatus.IN_PROGRESS, note="Reopened to add a reading")
    owner_pm_on(t, today - DAY)
    wo = WorkOrder.objects.get(pk=wo.pk)
    complete_work_order(wo, resolution="Reading added")  # blank keeps Passed
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.inspection_result == PASSED


def test_an_inspection_of_a_device_already_on_site_passes_only_with_the_owners_pm_current(today, pump_model, techs):
    """A device entered as already on site waits for nothing: an inspection on it is optional, and Passed still says the device is fit,
    so it is refused while the owner's PM is past; no result is not."""
    t = temporary(pump_model, owner_pm_due_on=today - DAY, waiting=False)
    wo = create_work_order(asset=t, type=WoType.INSPECTION, priority="normal", problem="Check after the move", assigned_to=techs["dana"])
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, inspection_result=PASSED, results=results())
    assert set(errors_of(e)) == {"inspection_result"}
    complete_work_order(wo, results=results(), resolution="Checked after the move")
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.inspection_result == ""


def test_the_owners_pm_date_of_a_device_of_ours_is_not_read(today, pump_model):
    """A kept device is ours (its stay's dates stay as history): its owner's PM date never refuses anything."""
    t = temporary(pump_model, owner_pm_due_on=today - DAY)
    Asset.objects.filter(pk=t.pk).update(ownership=Ownership.OWNED)
    t.refresh_from_db()
    assert inspections.owner_pm_refusal(t, today) == ""
    assert inspection_procedure(t) is inspections.INCOMING


def test_the_modal_and_the_api_refuse_the_pass(client, signed_in, today, pump_model, techs):
    t = temporary(pump_model, owner_pm_due_on=today - DAY)
    wo = inspections.open_inspection(t)
    assign(wo, technician=techs["dana"])
    signed_in("technician")
    r = client.get(f"/work-orders/{wo.number}/complete/", **HX)
    assert [row["text"] for row in r.context["rows"]] == [text for text, _m in inspections.TEMPORARY_CHECKLIST]
    steps = checklist_of(procedure_for(wo))
    data = {"signature": checklist_signature(steps), "inspection_result": PASSED}
    for n, (_text, measure) in enumerate(steps, 1):
        data[f"step_{n}"], data[f"reading_{n}"] = "pass", LEAKAGE if measure is not None else ""
    r = client.post(f"/work-orders/{wo.number}/complete/", data, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert f"The owner&#x27;s PM was due {day(today - DAY)}: have the owner do it, or record its new date." in body
    url = f"/api/v1/work-orders/{wo.id}/transition/"
    r = client.post(url, json.dumps({"status": "completed", "inspection_result": "passed", "results": results()}), content_type="application/json")
    assert r.status_code == 400 and r.json() == {"inspection_result": [f"The owner's PM was due {day(today - DAY)}: have the owner do it, or "
                                                                       "record its new date."]}
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN
    owner_pm_on(t, today + 90 * DAY)
    r = client.post(url, json.dumps({"status": "completed", "inspection_result": "passed", "results": results()}), content_type="application/json")
    assert r.status_code == 200, r.json()


# --- recalls ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def mixed(today, pump_model, pump):
    """Ours (CE-10002), a rental, a vendor loaner of Baxter's, and a rental already returned (status retired: not affected)."""
    rental = temporary(pump_model, "T-1", waiting=False)
    loaner = temporary(pump_model, "T-2", kind=Ownership.LOANER, owner="BD field service loaner pool", waiting=False)
    gone = temporary(pump_model, "T-3", waiting=False)
    Asset.objects.filter(pk=gone.pk).update(status=AssetStatus.RETIRED, returned_on=today)
    return pump, rental, loaner


def test_a_recall_on_a_temporary_device_goes_to_its_owner(pump_recall, mixed, techs):
    ours, rental, loaner = mixed
    assert rc.create_recall_work_orders(pump_recall) == (3, 0)
    wos = {wo.asset.tag: wo for wo in WorkOrder.objects.filter(alert=pump_recall.alert).select_related("asset", "assigned_to")}
    assert set(wos) == {"CE-10002", "T-1", "T-2"}
    assert wos["CE-10002"].assigned_to is not None and not wos["CE-10002"].vendor_service
    for tag, owner in (("T-1", "Acme Rentals"), ("T-2", "BD field service loaner pool")):
        wo = wos[tag]
        assert (wo.vendor_service, wo.vendor_name, wo.assigned_to_id, wo.type) == (True, owner, None, WoType.RECALL)
        assert wo.status_history.filter(note=f"Assigned to vendor: {owner}").exists()
    assert rc.unassigned_recall_work_orders(pump_recall) == 0


def test_a_recall_with_no_technician_counts_only_ours_unassigned(pump_recall, mixed):
    Credential.objects.all().delete()
    assert rc.create_recall_work_orders(pump_recall) == (3, 1)
    assert rc.unassigned_recall_work_orders(pump_recall) == 1
    assert set(WorkOrder.objects.filter(alert=pump_recall.alert, vendor_service=True).values_list("asset__tag", flat=True)) == {"T-1", "T-2"}


def test_the_api_batch_names_the_owner(client, signed_in, pump_recall, mixed, techs):
    signed_in("manager")
    r = client.post(f"/api/v1/alert-matches/{pump_recall.id}/work-orders/")
    assert r.status_code == 200 and (r.json()["created"], r.json()["unassigned"]) == (3, 0)
    assert WorkOrder.objects.get(alert=pump_recall.alert, asset__tag="T-1").vendor_name == "Acme Rentals"


# --- an incident's release --------------------------------------------------------------------------------------------------------

def _investigated(asset):
    incident = inc.record_incident(asset=asset, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    wo = incident.work_order
    change_status(wo, WoStatus.IN_PROGRESS)
    change_status(wo, WoStatus.COMPLETED)
    return incident, incident.holds.get()


def test_return_to_use_waits_for_the_owners_pm(today, pump_model):
    t = temporary(pump_model, owner_pm_due_on=today - DAY, waiting=False)
    _incident, hold = _investigated(t)
    with pytest.raises(ValidationError) as e:
        inc.release(hold, Release.RETURN_TO_USE)
    assert errors_of(e) == {"release": f"T-1's owner's PM was due {day(today - DAY)}: release it kept out of service, have the owner do the "
                                       "PM or record its new date, then return it to service from its drawer."}
    t.refresh_from_db()
    assert t.incident_hold and t.status == AssetStatus.OUT_OF_SERVICE
    owner_pm_on(t, today)
    inc.release(hold, Release.RETURN_TO_USE)
    t.refresh_from_db()
    assert not t.incident_hold and t.status == AssetStatus.IN_SERVICE


def test_return_to_use_with_no_owners_pm_date_and_keep_out_are_not_refused(today, pump_model):
    t = temporary(pump_model, waiting=False)
    _incident, hold = _investigated(t)
    inc.release(hold, Release.RETURN_TO_USE)
    t.refresh_from_db()
    assert t.status == AssetStatus.IN_SERVICE
    late = temporary(pump_model, "T-2", owner_pm_due_on=today - 5 * DAY, waiting=False)
    _incident, hold = _investigated(late)
    inc.release(hold, Release.KEEP_OUT)  # kept out: nothing to wait for
    late.refresh_from_db()
    assert not late.incident_hold and late.status == AssetStatus.OUT_OF_SERVICE


def test_the_release_screen_says_so(client, signed_in, today, pump_model):
    t = temporary(pump_model, owner_pm_due_on=today - DAY, waiting=False)
    incident, hold = _investigated(t)
    signed_in("manager")
    r = client.post(f"/incidents/{incident.number}/holds/{hold.pk}/release/", {"release": Release.RETURN_TO_USE}, **HX)
    assert "HX-Retarget" not in r and f"T-1&#x27;s owner&#x27;s PM was due {day(today - DAY)}" in r.content.decode()
