"""
Slice 26 in the API (apps/api/views.py, apps/api/serializers.py): incoming inspections through the same services as the screens.

- POST /api/v1/assets/ takes incoming_inspection ("" the default, or "waiting") and inspection_due; "waiting" with added_as existing, a
  status, a last PM, or a next PM is refused in words, keyed by each field, all at once. A device reads back awaiting_inspection
  (read-only), status_label, and its open inspection (open_inspection, open_inspection_number), left out for a scoped user who
  cannot see that work order.
- An edit of a waiting device takes no next PM (update_asset's 400); the create-only fields and the flag are refused on an edit.
- POST /api/v1/assets/{id}/status/ keeps the hold (set_status's words); Found and Reinstate bring it back out of service.
- POST /api/v1/assets/{id}/use-before-inspection/ {reason}: Equipment Approve, never a scoped user.
- Work orders read inspection_result and its label (read-only); the completion transition takes inspection_result and the checklist;
  creating a PM or a second inspection on a waiting device is a 400 keyed type; an edit never makes or unmakes an inspection, moves one
  off or onto a waiting device, or puts a PM on one.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from incoming_fixtures import INCOMING_ALL_PASS, incoming_results, waiting_device

from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass, UseBeforeInspection
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.workorders import inspections
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

S = AssetStatus
API = "/api/v1/"
ASSETS, WOS = f"{API}assets/", f"{API}work-orders/"
HAMILTON = "Hamilton Medical field service"


@pytest.fixture
def today(ctx):
    return timezone.localdate()  # the facility's, as the API's services read it


@pytest.fixture
def person(client, make_user):
    """Sign in as a default role, with the company or unit given (a vendor's, a requester's)."""
    n = iter(range(100))

    def _as(slug, company="", department=""):
        user = make_user(slug, username=f"{slug}-{next(n)}@riverside.example")
        user.company, user.department = company, department
        user.save()
        client.force_login(user)
        return user

    return _as


def post(client, url, body=None):
    return client.post(url, body or {}, content_type="application/json")


def patch(client, url, body):
    return client.patch(url, body, content_type="application/json")


def put(client, url, body):
    return client.put(url, body, content_type="application/json")


def asset_url(asset, action=""):
    return f"{ASSETS}{asset.id}/{action}"


def wo_url(wo, action=""):
    return f"{WOS}{wo.id}/{action}"


def body_for(model, dept, tag="NEW-1", **extra):
    return {"tag": tag, "device_model": str(model.id), "department": str(dept.id), **extra}


def reasons(asset):
    return [h.history_change_reason for h in Asset.history.filter(id=asset.id).order_by("history_date", "history_id")]


def the_inspection(asset):
    return inspections.open_inspection(asset)


# --- adding a device ----------------------------------------------------------------------------------------------------------------

def test_a_device_added_waiting_reads_back_its_inspection(client, person, today, dept, vent_model):
    person("technician")  # Equipment Edit adds devices
    r = post(client, ASSETS, body_for(vent_model, dept, incoming_inspection="waiting"))
    assert r.status_code == 201, r.json()
    data = r.json()
    a = Asset.objects.get(tag="NEW-1")
    wo = the_inspection(a)
    assert (data["status"], data["status_label"], data["awaiting_inspection"]) == (S.OUT_OF_SERVICE, "Awaiting inspection", True)
    assert (data["next_pm_on"], data["last_pm_on"], data["added_as"]) == (None, None, "new")
    assert (data["open_inspection"], data["open_inspection_number"]) == (str(wo.id), wo.number)
    assert "incoming_inspection" not in data and "inspection_due" not in data  # taken when added, never read back
    assert (wo.type, wo.status, wo.problem, wo.opened_on) == (WoType.INSPECTION, WoStatus.OPEN, inspections.INCOMING_PROBLEM, today)
    assert wo.due_on == today + timedelta(days=inspections.INSPECTION_DUE_DAYS)
    assert wo.assigned_to is None and not wo.vendor_service  # unassigned: assign/ or take/ next, never a credential bypass
    assert reasons(a) == ["Added, waiting for its incoming inspection"]
    assert client.get(asset_url(a)).json()["open_inspection_number"] == wo.number


def test_the_inspection_due_date_is_the_clients(client, person, today, dept, vent_model):
    person("manager")
    due = today + timedelta(days=12)
    r = post(client, ASSETS, body_for(vent_model, dept, incoming_inspection="waiting", inspection_due=due.isoformat(), installed_on=today.isoformat()))
    assert r.status_code == 201, r.json()
    assert the_inspection(Asset.objects.get(tag="NEW-1")).due_on == due


def test_blank_is_the_old_path(client, person, dept, vent_model):
    person("technician")
    for tag, extra in (("OLD-1", {}), ("OLD-2", {"incoming_inspection": ""}), ("OLD-3", {"incoming_inspection": "", "inspection_due": None})):
        data = post(client, ASSETS, body_for(vent_model, dept, tag, **extra)).json()
        assert (data["status"], data["status_label"], data["awaiting_inspection"], data["open_inspection"]) == (S.IN_SERVICE, "In service", False, None)
        assert data["next_pm_on"] is not None
    assert not WorkOrder.objects.exists()


@pytest.mark.parametrize("extra, keys", [
    ({"added_as": "existing"}, {"added_as"}),
    ({"status": S.IN_SERVICE}, {"status"}),
    ({"status": S.OUT_OF_SERVICE}, {"status"}),
    ({"last_pm_on": "2026-01-05"}, {"last_pm_on"}),
    ({"next_pm_on": "2027-01-05"}, {"next_pm_on"}),
    ({"added_as": "existing", "status": S.IN_SERVICE, "last_pm_on": "2026-01-05", "next_pm_on": "2027-01-05"},
     {"added_as", "status", "last_pm_on", "next_pm_on"}),  # every one at once, never one per try
])
def test_what_a_waiting_device_never_comes_with_is_refused_in_words(client, person, dept, vent_model, extra, keys):
    from apps.api.views import AssetViewSet

    person("director")
    r = post(client, ASSETS, body_for(vent_model, dept, incoming_inspection="waiting", **extra))
    assert r.status_code == 400 and set(r.json()) == keys, r.json()
    assert all(r.json()[k] == [AssetViewSet.NOT_WITH_WAITING[k]] for k in keys)
    assert not Asset.objects.exists() and not WorkOrder.objects.exists()


@pytest.mark.parametrize("extra, key, words", [
    ({"inspection_due": "2026-12-01"}, "inspection_due", "Only a device added waiting"),  # without "waiting": refused, never dropped
    ({"incoming_inspection": "", "inspection_due": "2026-12-01"}, "inspection_due", "Only a device added waiting"),
    ({"incoming_inspection": "passed"}, "incoming_inspection", ""),  # no "passed" shortcut: a pass is a completed inspection
    ({"incoming_inspection": "waiting", "added_as": "imported"}, "added_as", "Settings, Import data"),
])
def test_other_refusals_on_adding(client, person, dept, vent_model, extra, key, words):
    person("director")
    r = post(client, ASSETS, body_for(vent_model, dept, **extra))
    assert r.status_code == 400 and list(r.json()) == [key] and words in r.json()[key][0], r.json()
    assert not Asset.objects.exists()


def test_an_inspection_due_before_today_is_the_services_refusal(client, person, today, dept, vent_model):
    person("director")
    r = post(client, ASSETS, body_for(vent_model, dept, incoming_inspection="waiting", inspection_due=(today - timedelta(days=1)).isoformat()))
    assert r.status_code == 400 and "cannot be due before the device was added" in r.json()["inspection_due"][0]
    assert not Asset.objects.exists()


def test_adding_a_waiting_device_needs_equipment_edit(client, person, dept, vent_model):
    for slug in ("analyst", "requester", "vendor"):
        person(slug, company="Hamilton Medical", department="ICU")
        assert post(client, ASSETS, body_for(vent_model, dept, incoming_inspection="waiting")).status_code == 403, slug
    assert not Asset.objects.exists()


# --- editing a waiting device -------------------------------------------------------------------------------------------------------

def test_an_edit_takes_no_next_pm_on_a_waiting_device(client, person, today, vent_model):
    a = waiting_device(vent_model, today=today)
    person("director")
    r = patch(client, asset_url(a), {"next_pm_on": (today + timedelta(days=30)).isoformat()})
    assert r.status_code == 400 and r.json() == {"next_pm_on": [eq.pm_clock_message(a)]}
    assert the_inspection(a).number in r.json()["next_pm_on"][0]
    r = patch(client, asset_url(a), {"next_pm_on": None, "room": "Biomed bench 2"})  # what it has: accepted
    assert r.status_code == 200 and (r.json()["room"], r.json()["next_pm_on"], r.json()["status_label"]) == ("Biomed bench 2", None, "Awaiting inspection")
    body = client.get(asset_url(a)).json()  # a GET body goes back whole
    body["serial"] = "SN-77"
    r = put(client, asset_url(a), body)
    assert r.status_code == 200 and r.json()["serial"] == "SN-77"
    a.refresh_from_db()
    assert (a.awaiting_inspection, a.next_pm_on, a.status) == (True, None, S.OUT_OF_SERVICE)


@pytest.mark.parametrize("field, value", [("incoming_inspection", "waiting"), ("inspection_due", "2026-12-01")])
def test_the_create_only_fields_are_refused_on_an_edit(client, person, today, vent, field, value):
    from apps.api.views import AssetViewSet

    person("director")
    r = patch(client, asset_url(vent), {field: value, "room": "4 East"})
    assert r.status_code == 400 and r.json() == {field: [AssetViewSet.FIXED_ON_UPDATE[field]]}
    vent.refresh_from_db()
    assert (vent.room, vent.awaiting_inspection) == ("", False) and not WorkOrder.objects.exists()
    assert patch(client, asset_url(vent), {field: "" if field == "incoming_inspection" else None, "room": "4 East"}).status_code == 200


def test_the_flag_is_read_only(client, person, today, vent, vent_model):
    a = waiting_device(vent_model, today=today)
    person("director")
    for device, other in ((a, False), (vent, True), (a, "no")):
        r = patch(client, asset_url(device), {"awaiting_inspection": other, "room": "X"})
        assert r.status_code == 400 and "inspection_result passed" in r.json()["awaiting_inspection"][0], (device.tag, other)
    for device, same in ((a, True), (vent, False), (a, "true")):  # sending back what a GET returned is fine
        assert patch(client, asset_url(device), {"awaiting_inspection": same, "room": "Y"}).status_code == 200, (device.tag, same)
    a.refresh_from_db()
    vent.refresh_from_db()
    assert (a.awaiting_inspection, vent.awaiting_inspection) == (True, False)


# --- the hold, Found, Reinstate -----------------------------------------------------------------------------------------------------

def test_the_status_endpoint_keeps_the_hold_in_the_services_words(client, person, today, vent_model):
    a = waiting_device(vent_model, today=today)
    number = the_inspection(a).number
    person("technician")
    r = post(client, asset_url(a, "status/"), {"to": S.IN_SERVICE})
    assert r.status_code == 400 and r.json() == {"detail": eq.hold_message(a)} and number in r.json()["detail"]
    r = post(client, asset_url(a, "status/"), {"to": S.MISSING})
    assert r.status_code == 200 and (r.json()["status"], r.json()["status_label"]) == (S.MISSING, "Missing")
    assert r.json()["open_inspection_number"] == number  # still open while it is missing
    r = post(client, asset_url(a, "status/"), {"to": S.IN_SERVICE})  # Found never puts it in use
    assert r.status_code == 400 and r.json() == {"detail": eq.hold_message(a)}
    r = post(client, asset_url(a, "status/"), {"to": S.OUT_OF_SERVICE})  # Found: back out of service
    assert r.status_code == 200 and (r.json()["status_label"], r.json()["next_pm_on"]) == ("Awaiting inspection", None)


def test_retiring_cancels_the_inspection_and_reinstating_opens_one(client, person, today, vent_model):
    a = waiting_device(vent_model, today=today)
    first = the_inspection(a)
    person("director")
    r = post(client, asset_url(a, "status/"), {"to": S.RETIRED, "note": "Returned to the vendor"})
    assert r.status_code == 200 and (r.json()["status"], r.json()["awaiting_inspection"], r.json()["open_inspection"]) == (S.RETIRED, True, None)
    first.refresh_from_db()
    assert first.status == WoStatus.CANCELLED
    assert post(client, asset_url(a, "status/"), {"to": S.IN_SERVICE}).status_code == 400
    r = post(client, asset_url(a, "status/"), {"to": S.OUT_OF_SERVICE})  # Reinstate
    again = the_inspection(a)
    assert r.status_code == 200 and again is not None and again.pk != first.pk
    assert (r.json()["status_label"], r.json()["next_pm_on"], r.json()["open_inspection"]) == ("Awaiting inspection", None, str(again.id))


# --- in use before its inspection ---------------------------------------------------------------------------------------------------

def test_use_before_inspection_through_the_api(client, person, today, vent_model):
    a = waiting_device(vent_model, today=today)
    wo = the_inspection(a)
    user = person("director")
    r = post(client, asset_url(a, "use-before-inspection/"), {"reason": UseBeforeInspection.EMERGENCY})
    assert r.status_code == 200, r.json()
    data = r.json()
    assert (data["status"], data["status_label"], data["awaiting_inspection"], data["next_pm_on"]) == (S.IN_SERVICE, "In service", True, None)
    assert data["open_inspection_number"] == wo.number
    wo.refresh_from_db()
    assert (wo.priority, wo.due_on) == (Priority.HIGH, today + timedelta(days=1))
    h = Asset.history.filter(id=a.id).latest("history_date", "history_id")
    assert (h.history_change_reason, h.history_user) == ("In use before its incoming inspection: Emergency clinical need", user)
    r = post(client, asset_url(a, "use-before-inspection/"), {"reason": UseBeforeInspection.EMERGENCY})
    assert r.status_code == 400 and "already in use before its incoming inspection" in r.json()["detail"]


@pytest.mark.parametrize("slug", ["manager", "technician"])
def test_use_before_inspection_needs_equipment_approve(client, person, today, vent_model, slug):
    a = waiting_device(vent_model, today=today)
    person(slug)  # Equipment Edit: tags out, marks missing, but never puts a device in use before its inspection
    r = post(client, asset_url(a, "use-before-inspection/"), {"reason": UseBeforeInspection.EMERGENCY})
    assert r.status_code == 403 and r.json()["detail"] == eq.USE_BEFORE_PERMISSION
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE and reasons(a)[-1] == "Added, waiting for its incoming inspection"


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "because"}, {"reason": ["emergency"]}, {"reason": None}])
def test_use_before_inspection_takes_only_a_listed_reason(client, person, today, vent_model, body):
    a = waiting_device(vent_model, today=today)
    person("director")
    r = post(client, asset_url(a, "use-before-inspection/"), body)
    assert r.status_code == 400 and list(r.json()) == ["reason"], r.json()
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE


def test_use_before_inspection_refuses_a_device_not_waiting_and_another_facilitys(client, person, vent, other_tenant):
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator",
                                       category="Ventilators", risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        theirs = waiting_device(m, "NEW-9", department=d)
    person("director")
    r = post(client, asset_url(vent, "use-before-inspection/"), {"reason": "emergency"})
    assert r.status_code == 400 and r.json()["detail"] == "CE-10001 is not waiting for an incoming inspection."
    assert post(client, asset_url(theirs, "use-before-inspection/"), {"reason": "emergency"}).status_code == 404
    with tenant_context(other_tenant):
        theirs.refresh_from_db()
        assert theirs.status == S.OUT_OF_SERVICE


# --- completing the inspection ------------------------------------------------------------------------------------------------------

def test_assign_then_pass_through_the_api(client, person, today, dept, vent_model, techs):
    person("manager")
    a = Asset.objects.get(pk=post(client, ASSETS, body_for(vent_model, dept, incoming_inspection="waiting")).json()["id"])
    wo = the_inspection(a)
    assert post(client, wo_url(wo, "assign/"), {"technician": str(techs["dana"].id)}).status_code == 200
    person("technician")
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "results": INCOMING_ALL_PASS})
    assert r.status_code == 400 and "inspection_result" in r.json()  # required while the device waits
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "passed",
                                                 "results": incoming_results("pass", "fail", "pass", "pass", "pass", reading="640")})
    assert r.status_code == 400 and "inspection_result" in r.json()  # passed with a failed step
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "passed", "results": INCOMING_ALL_PASS})
    assert r.status_code == 200, r.json()
    data = r.json()
    assert (data["status"], data["inspection_result"], data["inspection_result_label"]) == (WoStatus.COMPLETED, "passed", "Passed")
    assert data["resolution"].startswith("Incoming inspection passed per the incoming checklist")  # the default, all steps recorded
    assert [s["result"] for s in data["checklist_results"]] == ["pass"] * len(inspections.INCOMING_CHECKLIST)
    device = client.get(asset_url(a)).json()
    assert (device["status"], device["status_label"], device["awaiting_inspection"], device["open_inspection"]) == (S.IN_SERVICE, "In service", False, None)
    assert device["next_pm_on"] == add_months(today, a.pm_interval_months).isoformat()  # the PM clock starts at the pass


def test_a_fail_then_the_reinspections_pass(client, person, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = the_inspection(a)
    assign(wo, technician=techs["dana"])
    person("technician")
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "failed",
                                                 "results": incoming_results("pass", "fail", "pass", "pass", "pass", reading="640")})
    assert r.status_code == 400 and "resolution" in r.json()  # a fail says what failed
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "failed", "resolution": "Leakage 640 µA",
                                                 "results": incoming_results("pass", "fail", "pass", "pass", "pass", reading="640")})
    assert r.status_code == 200 and (r.json()["inspection_result"], r.json()["inspection_result_label"]) == ("failed", "Failed")
    again = the_inspection(a)
    assert again.follow_up_of_id == wo.pk and again.assigned_to == techs["dana"] and again.type == WoType.INSPECTION
    device = client.get(asset_url(a)).json()
    assert (device["status_label"], device["open_inspection"], device["open_inspection_number"]) == ("Awaiting inspection", str(again.id), again.number)
    assert client.get(wo_url(again)).json()["follow_up_of"] == str(wo.id)
    r = post(client, wo_url(again, "transition/"), {"status": "completed", "inspection_result": "passed", "results": INCOMING_ALL_PASS})
    assert r.status_code == 200, r.json()
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)
    assert inspections.state(a).passed == again


def test_an_inspection_of_a_device_not_waiting_takes_an_optional_result(client, person, vent, techs):
    first = create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Inspect after the move", assigned_to=techs["dana"])
    second = create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Inspect again", assigned_to=techs["dana"])
    person("technician")
    r = post(client, wo_url(first, "transition/"), {"status": "completed", "resolution": "Checked, fine"})
    assert r.status_code == 200 and (r.json()["inspection_result"], r.json()["inspection_result_label"]) == ("", "")
    r = post(client, wo_url(second, "transition/"), {"status": "completed", "resolution": "Checked, fine", "inspection_result": "passed"})
    assert r.status_code == 200 and r.json()["inspection_result"] == "passed"
    vent.refresh_from_db()
    assert (vent.status, vent.awaiting_inspection) == (S.IN_SERVICE, False)  # moves nothing


def test_the_result_is_read_only_on_an_edit(client, person, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = the_inspection(a)
    person("director")
    for value in ("passed", "failed"):
        r = patch(client, wo_url(wo), {"inspection_result": value, "problem": "Inspect it"})
        assert r.status_code == 400 and "transition" in r.json()["inspection_result"][0]
    assert patch(client, wo_url(wo), {"inspection_result": "", "problem": "Inspect it"}).status_code == 200  # sent back as read
    wo.refresh_from_db()
    assert (wo.inspection_result, wo.problem) == ("", "Inspect it")
    a.refresh_from_db()
    assert a.awaiting_inspection


# --- opening and editing work orders on a waiting device ---------------------------------------------------------------------------

def test_opening_a_pm_or_a_second_inspection_on_a_waiting_device_is_a_400(client, person, today, vent_model):
    a = waiting_device(vent_model, today=today)
    number = the_inspection(a).number
    person("director")
    body = {"asset": str(a.id), "problem": "x", "priority": "normal", "due_on": (today + timedelta(days=3)).isoformat()}
    r = post(client, WOS, {**body, "type": WoType.PM})
    assert r.status_code == 400 and "its PMs start when it passes its incoming inspection" in r.json()["type"][0]
    r = post(client, WOS, {**body, "type": WoType.INSPECTION})
    assert r.status_code == 400 and number in r.json()["type"][0]
    assert WorkOrder.objects.count() == 1
    r = post(client, WOS, {**body, "type": WoType.REPAIR, "problem": "Cracked housing on arrival"})  # a repair is fine
    assert r.status_code == 201, r.json()


def test_an_edit_never_makes_or_unmakes_an_inspection(client, person, today, vent, pump, vent_model):
    a = waiting_device(vent_model, today=today)
    inspection = the_inspection(a)
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Alarm")
    person("director")
    r = patch(client, wo_url(repair), {"type": WoType.INSPECTION})
    assert r.status_code == 400 and "stays a corrective repair" in r.json()["type"][0]
    r = patch(client, wo_url(inspection), {"type": WoType.REPAIR})
    assert r.status_code == 400 and "is an incoming inspection and stays one" in r.json()["type"][0]
    for wo, t in ((repair, WoType.REPAIR), (inspection, WoType.INSPECTION)):
        wo.refresh_from_db()
        assert wo.type == t


def test_an_inspection_stays_with_a_waiting_device_and_never_lands_on_one(client, person, today, vent, pump, vent_model):
    a = waiting_device(vent_model, today=today)
    inspection = the_inspection(a)
    other = create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Inspect after the move")
    person("director")
    r = patch(client, wo_url(inspection), {"asset": str(vent.id)})
    assert r.status_code == 400 and "stays with that device" in r.json()["asset"][0]
    r = patch(client, wo_url(other), {"asset": str(a.id)})
    assert r.status_code == 400 and "NEW-1 is waiting for its incoming inspection" in r.json()["asset"][0]
    assert patch(client, wo_url(other), {"asset": str(pump.id)}).status_code == 200  # neither device waits: as before
    inspection.refresh_from_db()
    other.refresh_from_db()
    assert (inspection.asset_id, other.asset_id) == (a.pk, pump.pk)


def test_an_edit_never_puts_a_pm_on_a_waiting_device(client, person, today, vent, vent_model):
    a = waiting_device(vent_model, today=today)
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Scheduled PM")
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority="normal", problem="Cracked housing")
    moved = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Wrong device picked")
    person("director")
    r = patch(client, wo_url(pm), {"asset": str(a.id)})
    assert r.status_code == 400 and "its PMs start when it passes" in r.json()["asset"][0]
    r = patch(client, wo_url(repair), {"type": WoType.PM})
    assert r.status_code == 400 and "its PMs start when it passes" in r.json()["type"][0]
    assert patch(client, wo_url(moved), {"asset": str(a.id)}).status_code == 200  # a repair may be on it
    assert patch(client, wo_url(repair), {"type": WoType.SAFETY}).status_code == 200
    pm.refresh_from_db()
    assert pm.asset_id == vent.pk and not WorkOrder.objects.filter(asset=a, type=WoType.PM).exists()


# --- scoped users -------------------------------------------------------------------------------------------------------------------

def test_a_vendors_pass_through_the_api_moves_the_device(client, person, today, vent_model):
    a = waiting_device(vent_model, today=today)
    wo = the_inspection(a)
    assign(wo, vendor_name=HAMILTON)
    vendor = person("vendor", company="Hamilton Medical")
    listed = [x for x in client.get(ASSETS).json()["results"] if x["id"] == str(a.id)]
    assert len(listed) == 1 and (listed[0]["open_inspection_number"], listed[0]["status_label"]) == (wo.number, "Awaiting inspection")
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "passed", "results": INCOMING_ALL_PASS})
    assert r.status_code == 200, r.json()
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)
    h = Asset.history.filter(id=a.id).latest("history_date", "history_id")
    assert (h.history_change_reason, h.history_user) == (f"Passed incoming inspection {wo.number}", vendor)
    # Another company sees neither the device nor its inspection.
    person("vendor", company="Acme Biomedical")
    assert client.get(asset_url(a)).status_code == 404 and client.get(wo_url(wo)).status_code == 404


def test_an_inspection_outside_the_share_is_left_out(client, person, today, vent_model, dept):
    """A vendor sees a waiting device through its own repair there; the in-house inspection's id and number are left out, in the list
    and the device alike. The unit's requester sees the device's work orders, the inspection's too."""
    a = waiting_device(vent_model, today=today, department=dept)
    wo = the_inspection(a)
    create_work_order(asset=a, type=WoType.REPAIR, priority="normal", problem="Cracked housing", vendor_service=True, vendor_name=HAMILTON)
    person("vendor", company="Hamilton Medical")
    one = client.get(asset_url(a)).json()
    listed = [x for x in client.get(ASSETS).json()["results"] if x["id"] == str(a.id)][0]
    for data in (one, listed):
        assert (data["open_inspection"], data["open_inspection_number"], data["awaiting_inspection"]) == (None, None, True)
    body = client.get(ASSETS).content.decode() + client.get(asset_url(a)).content.decode()
    assert wo.number not in body and str(wo.id) not in body
    assert post(client, asset_url(a, "use-before-inspection/"), {"reason": "emergency"}).status_code == 403  # never a scoped user's
    person("requester", department="ICU")
    assert client.get(asset_url(a)).json()["open_inspection_number"] == wo.number
    assert [x["open_inspection_number"] for x in client.get(ASSETS).json()["results"] if x["id"] == str(a.id)] == [wo.number]


def test_a_scoped_user_cannot_add_or_put_in_use_whatever_their_levels(client, person, today, vent_model, dept, make_user):
    from apps.accounts.models import DataScope, Level, Module, Role, User

    a = waiting_device(vent_model, today=today, department=dept)
    assign(the_inspection(a), vendor_name=HAMILTON)
    role = Role.objects.create(name="Vendor lead", slug="vendor-lead", scope=DataScope.COMPANY)
    role.set_levels({m: Level.FULL for m in Module.values})
    user = User.objects.create_user(username="lead@riverside.example", password="Test-Pass-2026-x", tenant=a.tenant, role=role,
                                    company="Hamilton Medical")
    client.force_login(user)
    assert client.get(asset_url(a)).status_code == 200  # sees it, through its inspection
    assert post(client, asset_url(a, "use-before-inspection/"), {"reason": "emergency"}).status_code == 403
    assert post(client, ASSETS, body_for(vent_model, dept, "NEW-2", incoming_inspection="waiting")).status_code == 403
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE and not Asset.objects.filter(tag="NEW-2").exists()


# --- reads stay one query per page --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["director", "vendor"])
def test_a_page_of_waiting_devices_reads_their_inspections_at_once(client, person, today, vent_model, slug):
    person(slug, company="Hamilton Medical")

    def count(n):
        for i in range(n):
            a = waiting_device(vent_model, f"Q-{n}-{i}", today=today)
            assign(the_inspection(a), vendor_name=HAMILTON)
        with CaptureQueriesContext(connection) as q:
            results = client.get(ASSETS).json()["results"]
        assert len(results) == Asset.objects.count() and all(x["open_inspection_number"] for x in results)
        return len(q)

    assert count(2) == count(5)


def test_a_reopened_pass_reads_back(client, person, today, vent_model, techs):
    """Reopening a passed inspection (Work orders Edit) and completing it again keeps its pass, as on the screen."""
    a = waiting_device(vent_model, today=today)
    wo = the_inspection(a)
    assign(wo, technician=techs["dana"])
    person("technician")
    assert post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "passed", "results": INCOMING_ALL_PASS}).status_code == 200
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS, note="Reopened to add the serial")
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "inspection_result": "failed", "resolution": "x"})
    assert r.status_code == 400 and "tag the device out and open a repair" in r.json()["inspection_result"][0]
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "results": INCOMING_ALL_PASS})
    assert r.status_code == 200 and r.json()["inspection_result"] == "passed"
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)
