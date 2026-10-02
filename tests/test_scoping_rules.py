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


# --- slice 16 review -----------------------------------------------------------------------------------------------------------------

def test_a_user_whose_role_becomes_scoped_gets_no_more_report_emails(ctx, make_user, monkeypatch, mailoutbox):
    """Reports are the whole facility's; a scoped role never sees them, by email no more than on the screen."""
    from datetime import date

    from apps.accounts.models import DataScope, Level, Role
    from apps.reports import subscriptions as subs
    from apps.reports.models import ReportSubscription

    monkeypatch.setattr(subs, "local_today", lambda: date(2026, 10, 5))
    role = Role.objects.create(name="Vendor liaison", slug="liaison", scope=DataScope.FACILITY)
    role.set_levels({"reports": Level.VIEW, "workorders": Level.VIEW})
    user = make_user("analyst")
    user.role, user.email, user.company = role, "liaison@acme.example", "Acme Biomed"
    user.save()
    subs.set_subscription(user, "tech", "weekly")
    ReportSubscription.objects.filter(user=user).update(start_on=None)
    role.scope = DataScope.COMPANY
    role.save()
    user.refresh_from_db()
    sub = ReportSubscription.objects.get(user=user)
    assert subs.skip_reason(sub, ctx) == "scoped" and not subs.can_schedule(user)
    subs.send_due(date(2026, 10, 5))
    assert not mailoutbox


def test_a_reopened_vendor_pm_failing_again_never_opens_a_second_repair(vendor_pm, vendor_user, vent):
    """The PM's own repair went to CE (a PM-only contract), outside the vendor's share: failing again records on it all the same."""
    from datetime import date, timedelta

    from apps.contracts.models import Contract, ContractType, Coverage

    vent.contract = Contract.objects.create(reference="SC-PM", vendor="Hamilton Medical", type=ContractType.OEM, coverage=Coverage.PM_ONLY,
                                            start_on=date.today() - timedelta(days=30), end_on=date.today() + timedelta(days=300))
    vent.save()
    fail = [{"result": "fail"}, {"result": "pass"}]
    first = complete_work_order(vendor_pm, pm_result=PmResult.FAIL, results=fail, by=vendor_user).follow_up
    assert not first.vendor_service and not scoping.can_see_work_order(vendor_user, first)
    change_status(vendor_pm, WoStatus.IN_PROGRESS)
    done = complete_work_order(vendor_pm, pm_result=PmResult.FAIL, results=fail, by=vendor_user)
    assert done.follow_up is None and done.repair == first
    assert WorkOrder.objects.filter(follow_up_of=vendor_pm, type=WoType.REPAIR).count() == 1


def test_the_complete_modal_names_the_vendor_a_failed_vendor_pm_goes_to(client, vendor_pm, vendor_user):
    client.force_login(vendor_user)
    body = client.get(f"/work-orders/{vendor_pm.number}/complete/", HTTP_HX_REQUEST="true").content.decode()
    assert "A repair work order is opened for the failed steps, sent to Hamilton Medical field service." in body


def test_scoped_users_do_not_read_the_facilitys_technician_roster(client, vendor_pm, vendor_user, vent, techs):
    client.force_login(vendor_user)
    drawer = client.get(f"/equipment/{vent.tag}/", HTTP_HX_REQUEST="true").content.decode()
    page = client.get("/work-orders/").content.decode()
    for body in (drawer, page):
        assert "Dana Whitfield" not in body and "Tom Okafor" not in body
    assert "Who can service this" not in drawer and "Clinical Engineering · " not in page  # the nav footer's head count


def test_a_company_matches_the_vendor_name_exactly_as_stored(ctx, vent, make_user):
    """A contract's vendor typed with two spaces: the user's company keeps them, so the work orders match (any case)."""
    from apps.accounts.services import _clean_company

    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="x")
    assign(wo, vendor_name="Acme  Imaging ")  # a stray space from the API is trimmed
    wo.refresh_from_db()
    assert wo.vendor_name == "Acme  Imaging"
    user = make_user("vendor")
    user.company = _clean_company("  acme  IMAGING ")
    user.save()
    assert scoping.can_see_work_order(user, wo)
