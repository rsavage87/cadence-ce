"""
Slice 26 review fixes: update_asset and set_status work on the device's row as it is (a copy read before a passed inspection never
puts the wait back); a failed step needs the result Failed; only the inspection whose pass ended the wait stays passed when reopened,
and on it a failed step gets the kept-pass answer; a device in use before its inspection is a binder gap whatever day it was added; a
checklist that is not a list is a 400 over the API.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from incoming_fixtures import in_use_device, incoming_results, passed_device
from survey_helpers import gaps_of, period

from apps.credentials.models import Technician
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department
from apps.reports.survey import GAP, inventory
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import InspectionResult, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order


@pytest.fixture
def dana(ctx):
    return Technician.objects.create(name="Dana Whitfield")


def _complete_inspection(asset, dana, **kwargs):
    wo = inspections.open_inspection(asset)
    assign(wo, technician=dana)
    return wo, complete_work_order(wo, **kwargs)


@pytest.mark.parametrize("write", ["edit", "status"])
def test_a_copy_read_before_the_pass_never_puts_the_wait_back(ctx, vent_model, dana, write):
    asset = in_use_device(vent_model, "STALE-1")
    stale = Asset.objects.get(pk=asset.pk)  # read by a page, the API, or an import chunk before the inspection passes
    _complete_inspection(asset, dana, inspection_result=InspectionResult.PASSED, results=incoming_results())
    passed = Asset.objects.get(pk=asset.pk)
    assert not passed.awaiting_inspection and passed.next_pm_on
    if write == "edit":
        out = eq.update_asset(stale, room="12")
        assert out is stale and stale.room == "12"
    else:
        eq.set_status(stale, AssetStatus.MISSING)
        assert stale.status == AssetStatus.MISSING
    now = Asset.objects.get(pk=asset.pk)
    assert not now.awaiting_inspection and now.next_pm_on == passed.next_pm_on
    assert stale.awaiting_inspection is False  # the caller's copy is read again


def test_a_failed_step_needs_the_result_failed(ctx, vent_model, dana):
    asset = eq.create_asset(tag="NOW-1", device_model=vent_model, department=Department.objects.create(name="ICU"))  # never waited
    wo = create_work_order(asset=asset, type=WoType.INSPECTION, priority=Priority.NORMAL, problem="Inspection", assigned_to=dana)
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, results=incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"), resolution="Leakage high")
    assert "Failed" in e.value.message_dict["inspection_result"][0]
    complete_work_order(wo, results=incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"), resolution="Leakage high",
                        inspection_result=InspectionResult.FAILED)
    wo.refresh_from_db()
    assert wo.inspection_result == InspectionResult.FAILED and Asset.objects.get(pk=asset.pk).status == AssetStatus.IN_SERVICE  # moved nothing


def test_a_pass_that_changed_nothing_may_be_corrected(ctx, vent_model, dana, make_user):
    asset = eq.create_asset(tag="NOW-2", device_model=vent_model, department=Department.objects.create(name="ICU"))  # never waited
    wo = create_work_order(asset=asset, type=WoType.INSPECTION, priority=Priority.NORMAL, problem="Loaner back", assigned_to=dana)
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, resolution="Checked")
    wo.refresh_from_db()
    assert not inspections.pass_cleared_flag(wo)
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened
    complete_work_order(WorkOrder.objects.get(pk=wo.pk), inspection_result=InspectionResult.FAILED, resolution="Alarm actually failed")
    assert WorkOrder.objects.get(pk=wo.pk).inspection_result == InspectionResult.FAILED


def test_the_pass_that_ended_the_wait_stays_passed_with_one_answer(ctx, vent_model, dana, client, make_user):
    asset, wo = passed_device(vent_model, technician=dana)
    assert inspections.pass_cleared_flag(wo)
    change_status(wo, WoStatus.IN_PROGRESS)
    for kwargs in ({"inspection_result": InspectionResult.FAILED, "resolution": "x"},
                   {"results": incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"), "resolution": "x"}):
        with pytest.raises(ValidationError) as e:
            complete_work_order(WorkOrder.objects.get(pk=wo.pk), **kwargs)
        assert "stays passed" in e.value.message_dict["inspection_result"][0], kwargs
    client.force_login(make_user("director"))
    body = client.get(f"/work-orders/{wo.number}/complete/", HTTP_HX_REQUEST="true").content.decode()
    assert "no longer waits for its incoming inspection, so it stays passed" in body


def test_a_device_in_use_before_its_inspection_is_a_gap_whatever_day_it_was_added(ctx, vent_model):
    today = timezone.localdate()
    asset = in_use_device(vent_model, "OLD-W", today=today)
    Asset.objects.filter(pk=asset.pk).update(created_at=timezone.now() - timedelta(days=400))
    s = inventory.build(period(today - timedelta(days=30), today), None)
    gap = [g for g in gaps_of(s, GAP) if g.record == "OLD-W"]
    assert gap and "in use before its incoming inspection" in gap[0].text
    waiting = eq.create_asset(tag="WAIT-1", device_model=vent_model, department=asset.department, incoming_inspection=eq.INCOMING_WAITING)
    assert not [g for g in gaps_of(inventory.build(period(today - timedelta(days=30), today), None), GAP) if g.record == waiting.tag]


def test_a_checklist_that_is_not_a_list_is_a_400(ctx, vent_model, dana, client, make_user):
    asset = in_use_device(vent_model, "API-1")
    wo = inspections.open_inspection(asset)
    assign(wo, technician=dana)
    client.force_login(make_user("director"))
    r = client.post(f"/api/v1/work-orders/{wo.pk}/transition/", {"status": "completed", "inspection_result": "passed", "results": 5},
                    content_type="application/json")
    assert r.status_code == 400 and "checklist" in r.json()
