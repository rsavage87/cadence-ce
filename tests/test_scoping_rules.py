"""Slice 16 merge rules: a vendor's failed PM sends its repair to the same vendor and never offers or reveals another company's or the
facility's repair; renaming a department carries its requesters with it."""
import pytest
from django.core.exceptions import ValidationError

from apps.equipment.services import rename_department
from apps.pm.models import PmProcedure
from apps.workorders import scoping
from apps.workorders.completion import complete_work_order, other_open_repair
from apps.workorders.models import PmResult, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Hamilton Medical"
    user.save()
    return user


@pytest.fixture
def vendor_pm(ctx, vent):
    proc = PmProcedure.objects.create(code="HM-G5-PM6", name="Ventilator PM", checklist=["Inspect", "Alarm check"])
    vent.device_model.pm_procedure = proc
    vent.device_model.save()
    pm = create_work_order(asset=vent, type=WoType.PM, priority="high", problem="Scheduled PM")
    assign(pm, vendor_name="Hamilton Medical field service")
    change_status(pm, WoStatus.IN_PROGRESS)
    return pm


def test_a_vendors_failed_pm_sends_its_repair_to_the_same_vendor(vendor_pm, vendor_user):
    done = complete_work_order(vendor_pm, pm_result=PmResult.FAIL, results=[{"result": "fail"}, {"result": "pass"}], by=vendor_user)
    repair = done.follow_up
    assert repair.vendor_service and repair.vendor_name == "Hamilton Medical field service"
    assert scoping.can_see_work_order(vendor_user, repair)  # in their share: they can do the repair they found


def test_a_vendor_is_never_offered_or_told_about_the_facilitys_repair(vendor_pm, vendor_user, vent, techs):
    in_house = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm speaker", assigned_to=techs["dana"])
    assert other_open_repair(vendor_pm) == in_house  # the facility sees it
    assert other_open_repair(vendor_pm, vendor_user) is None  # the vendor does not
    with pytest.raises(ValidationError) as e:
        complete_work_order(vendor_pm, pm_result=PmResult.FAIL, results=[{"result": "fail"}, {"result": "pass"}], open_repair=False, by=vendor_user)
    assert "has no open repair work order" in " ".join(e.value.messages)  # the same answer as when there is none
    complete_work_order(vendor_pm, pm_result=PmResult.FAIL, results=[{"result": "fail"}, {"result": "pass"}], by=vendor_user)
    assert not in_house.notes.exists() and WorkOrder.objects.filter(follow_up_of=vendor_pm, vendor_service=True).count() == 1


def test_renaming_a_department_carries_its_requesters(ctx, dept, vent, make_user, tenant, other_tenant):
    requester = make_user("requester")
    requester.department = dept.name
    requester.save()
    from apps.accounts.models import User

    theirs = User.objects.create_user(username="r@other.example", password="Test-Pass-2026-x", tenant=other_tenant, department=dept.name)
    rename_department(dept, "Medical ICU")
    requester.refresh_from_db()
    theirs.refresh_from_db()
    assert requester.department == "Medical ICU" and scoping.can_see_asset(requester, vent)
    assert theirs.department != "Medical ICU"  # another facility's user keeps theirs
