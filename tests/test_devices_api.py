"""The device API through the slice 12 services: POST/PUT/PATCH /api/v1/assets/ call create_asset and update_asset, POST
/api/v1/assets/{id}/status/ calls set_status with the levels in apps.equipment.permissions, and POST /api/v1/device-models/ and
/api/v1/departments/ call create_device_model and create_department. Session auth (force_login), as the other API tests."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.api.serializers import AssetSerializer
from apps.api.views import AssetViewSet
from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType
from apps.equipment import services as svc
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import create_work_order

S = AssetStatus
ASSETS = "/api/v1/assets/"
TODAY = date.today()  # the API passes no `today`, so the services use the real one


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def theirs(other_tenant):
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                       risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        a = Asset.objects.create(tag="CE-10001", device_model=m, department=d, next_pm_on=TODAY + timedelta(days=10))
    return {"dept": d, "model": m, "asset": a}


def post(client, url, body):
    return client.post(url, body, content_type="application/json")


def patch(client, url, body):
    return client.patch(url, body, content_type="application/json")


def put(client, url, body):
    return client.put(url, body, content_type="application/json")


def one(asset):
    return f"{ASSETS}{asset.id}/"


def status_url(asset):
    return f"{ASSETS}{asset.id}/status/"


def reasons(asset):
    return [h.history_change_reason for h in Asset.history.filter(id=asset.id).order_by("history_date", "history_id")]


# --- create -----------------------------------------------------------------------------------------------------------------

def test_create_goes_through_the_service(client, signed_in, ctx, dept, vent_model):
    signed_in("technician")
    last = TODAY - timedelta(days=30)
    r = post(client, ASSETS, {"tag": " CE-20001 ", "device_model": str(vent_model.id), "department": str(dept.id), "serial": "SN   4411",
                              "room": "4 West", "last_pm_on": last.isoformat(), "notes": "Came with the spare battery."})
    assert r.status_code == 201, r.json()
    data = r.json()
    assert data["tag"] == "CE-20001" and data["serial"] == "SN 4411"  # the service collapses inner spaces; DRF only trims
    assert data["acquisition_cost"] == "38000.00" and data["status"] == S.IN_SERVICE and data["condition"] == 3
    assert data["next_pm_on"] == add_months(last, 6).isoformat() and data["department_name"] == "ICU"
    assert data["device_model_detail"]["pm_interval_months"] == 6 and data["contract"] is None and data["support_type"] == "in_house"
    a = Asset.objects.get(tag="CE-20001")
    assert str(a.id) == data["id"] and a.tenant_id == ctx.id and reasons(a) == ["Added"]


def test_create_accepts_a_device_waiting_for_inspection_with_its_own_cost_and_next_pm(client, signed_in, ctx, dept, pump_model):
    signed_in("manager")
    due = TODAY + timedelta(days=7)
    r = post(client, ASSETS, {"tag": "CE-20002", "device_model": str(pump_model.id), "department": str(dept.id), "status": S.OUT_OF_SERVICE,
                              "acquisition_cost": "0.00", "next_pm_on": due.isoformat(), "contract": None})
    assert r.status_code == 201, r.json()
    assert (r.json()["status"], r.json()["acquisition_cost"], r.json()["next_pm_on"]) == (S.OUT_OF_SERVICE, "0.00", due.isoformat())


@pytest.mark.parametrize("change, key, text", [
    ({"tag": "ce-10001"}, "tag", "ce-10001 is already on another device."),
    ({"tag": "new"}, "tag", "cannot be used as an asset tag"),
    ({"tag": "CE/9"}, "tag", "spaces or slashes"),
    ({"tag": ""}, "tag", ""),
    ({"status": S.RETIRED}, "status", "A new device is in service or out of service"),
    ({"status": "bogus"}, "status", ""),
    ({"acquisition_cost": "-1.00"}, "acquisition_cost", "cannot be negative"),
    ({"condition": 7}, "condition", "Condition is 1 (poor) to 5 (excellent)."),
    ({"installed_on": (TODAY + timedelta(days=1)).isoformat()}, "installed_on", "cannot be in the future"),
    ({"installed_on": (TODAY - timedelta(days=9)).isoformat(), "warranty_end": (TODAY - timedelta(days=10)).isoformat()}, "warranty_end", "warranty"),
    ({"last_pm_on": (TODAY + timedelta(days=1)).isoformat()}, "last_pm_on", "cannot be in the future"),
    ({"device_model": None}, "device_model", ""),
])
def test_create_field_errors_are_400s(client, signed_in, ctx, vent, dept, vent_model, change, key, text):
    signed_in("technician")
    body = {"tag": "CE-20001", "device_model": str(vent_model.id), "department": str(dept.id), **change}
    r = post(client, ASSETS, body)
    assert r.status_code == 400 and list(r.json()) == [key] and text in r.json()[key][0], r.json()
    assert list(Asset.objects.values_list("tag", flat=True)) == ["CE-10001"]


def test_create_refuses_a_contract(client, signed_in, ctx, dept, vent_model):
    contract = Contract.objects.create(reference="SC-1", vendor="Hamilton", type=ContractType.OEM, start_on=TODAY, end_on=TODAY + timedelta(days=365))
    signed_in("technician")
    r = post(client, ASSETS, {"tag": "CE-20001", "device_model": str(vent_model.id), "department": str(dept.id), "contract": str(contract.id)})
    assert r.status_code == 400 and "add_assets" in r.json()["contract"][0]
    assert not Asset.objects.exists()


def test_create_refuses_another_facilitys_model_or_department(client, signed_in, ctx, dept, vent_model, theirs):
    signed_in("director")
    for body, key in (({"device_model": str(theirs["model"].id), "department": str(dept.id)}, "device_model"),
                      ({"device_model": str(vent_model.id), "department": str(theirs["dept"].id)}, "department")):
        r = post(client, ASSETS, {"tag": "CE-20001", **body})
        assert r.status_code == 400 and list(r.json()) == [key]
    assert not Asset.objects.exists()


def test_the_same_tag_is_fine_in_another_facility(client, signed_in, ctx, dept, vent_model, theirs):
    signed_in("technician")
    r = post(client, ASSETS, {"tag": "ce-10001", "device_model": str(vent_model.id), "department": str(dept.id)})
    assert r.status_code == 201 and r.json()["tag"] == "ce-10001"


# --- update -----------------------------------------------------------------------------------------------------------------

def test_patch_goes_through_the_service(client, signed_in, ctx, vent, pump_model):
    signed_in("technician")
    west = Department.objects.create(name="ICU West")
    r = patch(client, one(vent), {"room": "  12  B ", "serial": "SN 9", "department": str(west.id), "device_model": str(pump_model.id),
                                  "condition": 4, "notes": " Battery replaced. "})
    assert r.status_code == 200, r.json()
    data = r.json()
    assert (data["room"], data["serial"], data["department_name"], data["condition"], data["notes"]) == ("12 B", "SN 9", "ICU West", 4, "Battery replaced.")
    assert data["device_model_detail"]["model"] == "Alaris 8015 PCU" and data["next_pm_on"] == vent.next_pm_on.isoformat()
    assert reasons(vent)[-1] == "Edited"


def test_patch_with_nothing_changed_writes_no_history(client, signed_in, ctx, vent):
    signed_in("technician")
    before = reasons(vent)
    r = patch(client, one(vent), {"room": "", "condition": 3})
    assert r.status_code == 200 and reasons(vent) == before


@pytest.mark.parametrize("field, value", [("tag", "CE-99999"), ("tag", "ce-10001"), ("status", S.RETIRED), ("last_pm_on", TODAY.isoformat())])
def test_patch_refuses_to_change_fixed_fields(client, signed_in, ctx, vent, field, value):
    signed_in("director")
    r = patch(client, one(vent), {field: value, "room": "5 East"})
    assert r.status_code == 400 and list(r.json()) == [field] and r.json()[field][0] == AssetViewSet.FIXED_ON_UPDATE[field]
    vent.refresh_from_db()
    assert (vent.tag, vent.status, vent.last_pm_on, vent.room) == ("CE-10001", S.IN_SERVICE, None, "")


def test_patch_refuses_to_change_the_contract(client, signed_in, ctx, vent):
    contract = Contract.objects.create(reference="SC-1", vendor="Hamilton", type=ContractType.OEM, start_on=TODAY, end_on=TODAY + timedelta(days=365))
    signed_in("director")
    r = patch(client, one(vent), {"contract": str(contract.id)})
    assert r.status_code == 400 and "add_assets" in r.json()["contract"][0]
    ct.add_asset(contract, vent)  # covered through the contracts side: now the same contract may be sent back, and clearing it is refused
    assert patch(client, one(vent), {"contract": str(contract.id), "room": "2"}).status_code == 200
    assert patch(client, one(vent), {"contract": None}).status_code == 400
    vent.refresh_from_db()
    assert vent.contract == contract and vent.room == "2"


def test_a_get_body_can_be_put_back(client, signed_in, ctx, vent):
    signed_in("technician")
    body = client.get(one(vent)).json()
    body["room"] = "3 North"
    r = put(client, one(vent), body)
    assert r.status_code == 200 and r.json()["room"] == "3 North" and r.json()["tag"] == "CE-10001"
    assert reasons(vent)[-1] == "Edited"


@pytest.mark.parametrize("change, key", [
    ({"next_pm_on": None}, "next_pm_on"),
    ({"condition": 0}, "condition"),
    ({"acquisition_cost": "-5"}, "acquisition_cost"),
    ({"installed_on": (TODAY + timedelta(days=2)).isoformat()}, "installed_on"),
    ({"warranty_end": (TODAY - timedelta(days=5000)).isoformat()}, "warranty_end"),
])
def test_update_field_errors_are_400s(client, signed_in, ctx, vent, change, key):
    signed_in("technician")
    r = patch(client, one(vent), change)
    assert r.status_code == 400 and list(r.json()) == [key], r.json()
    assert reasons(vent) == [None]  # only the fixture's own row


def test_update_refuses_another_facilitys_department(client, signed_in, ctx, vent, theirs):
    signed_in("director")
    r = patch(client, one(vent), {"department": str(theirs["dept"].id)})
    assert r.status_code == 400 and list(r.json()) == ["department"]


# --- status -----------------------------------------------------------------------------------------------------------------

def test_status_action_moves_the_device_and_keeps_the_note(client, signed_in, ctx, vent):
    signed_in("technician")
    r = post(client, status_url(vent), {"to": S.OUT_OF_SERVICE, "note": "Cracked housing"})
    assert r.status_code == 200 and r.json()["status"] == S.OUT_OF_SERVICE
    assert reasons(vent)[-1] == "Cracked housing"
    r = client.post(status_url(vent), {"to": S.IN_SERVICE})  # a form-encoded body works too
    assert r.status_code == 200 and r.json()["status"] == S.IN_SERVICE and reasons(vent)[-1] == "Status: In service"


@pytest.mark.parametrize("slug", ["technician", "manager"])
def test_retiring_needs_approve(client, signed_in, ctx, vent, slug):
    signed_in(slug)
    r = post(client, status_url(vent), {"to": S.RETIRED})
    assert r.status_code == 403 and "Approve" in r.json()["detail"]
    vent.refresh_from_db()
    assert vent.status == S.IN_SERVICE


def test_reinstating_needs_approve(client, signed_in, ctx, dept, vent_model):
    a = Asset.objects.create(tag="CE-30001", device_model=vent_model, department=dept, status=S.RETIRED)
    signed_in("technician")
    assert post(client, status_url(a), {"to": S.IN_SERVICE}).status_code == 403
    a.refresh_from_db()
    assert a.status == S.RETIRED


def test_director_retires_and_reinstates(client, signed_in, ctx, vent):
    director = signed_in("director")
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="Semiannual PM")
    r = post(client, status_url(vent), {"to": S.RETIRED, "note": "Replaced"})
    assert r.status_code == 200 and r.json()["status"] == S.RETIRED and r.json()["next_pm_on"] is None
    pm.refresh_from_db()
    last = WorkOrderStatusHistory.objects.filter(work_order=pm).order_by("created_at").last()
    assert pm.status == WoStatus.CANCELLED and (last.note, last.changed_by) == ("Device retired", director)
    r = post(client, status_url(vent), {"to": S.IN_SERVICE})
    assert r.status_code == 200 and r.json()["status"] == S.IN_SERVICE and r.json()["next_pm_on"] == TODAY.isoformat()


def test_retiring_with_open_repair_work_is_a_400_naming_it(client, signed_in, ctx, vent):
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority="high", problem="Alarm fault")
    signed_in("director")
    r = post(client, status_url(vent), {"to": S.RETIRED})
    assert r.status_code == 400 and repair.number in r.json()["detail"]
    vent.refresh_from_db()
    assert vent.status == S.IN_SERVICE


@pytest.mark.parametrize("body, key, text", [
    ({"to": S.IN_REPAIR}, "detail", "CE-10001 cannot go from in service to in repair."),
    ({"to": S.IN_SERVICE}, "detail", "CE-10001 cannot go from in service to in service."),
    ({"to": "bogus"}, "to", "Required; one of"),
    ({}, "to", "Required; one of"),
    ({"to": None}, "to", "Required; one of"),
    ([S.RETIRED], "to", "Required; one of"),
])
def test_refused_or_malformed_status_changes_are_400s(client, signed_in, ctx, vent, body, key, text):
    signed_in("director")
    r = post(client, status_url(vent), body)
    assert r.status_code == 400 and text in str(r.json()[key]), r.json()
    vent.refresh_from_db()
    assert vent.status == S.IN_SERVICE


# --- who may write ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["requester", "analyst", "vendor"])
def test_view_only_roles_read_but_cannot_write(client, signed_in, ctx, vent, dept, vent_model, slug):
    signed_in(slug)
    assert client.get(one(vent)).status_code == 200 and client.get(ASSETS).status_code == 200
    assert post(client, ASSETS, {"tag": "CE-20001", "device_model": str(vent_model.id), "department": str(dept.id)}).status_code == 403
    assert patch(client, one(vent), {"room": "2"}).status_code == 403
    assert post(client, status_url(vent), {"to": S.OUT_OF_SERVICE}).status_code == 403
    assert post(client, "/api/v1/device-models/", {"manufacturer": "A", "model": "B", "description": "C", "category": "D",
                                                    "risk_class": "low"}).status_code == 403
    assert post(client, "/api/v1/departments/", {"name": "Cath Lab"}).status_code == 403
    vent.refresh_from_db()
    assert vent.room == "" and vent.status == S.IN_SERVICE and Asset.objects.count() == 1 and Department.objects.count() == 1


def test_signed_out_gets_nothing(client, ctx, vent):
    assert client.get(ASSETS).status_code in (401, 403)
    assert post(client, status_url(vent), {"to": S.OUT_OF_SERVICE}).status_code in (401, 403)


# --- device models and departments ------------------------------------------------------------------------------------------

def test_device_model_create_goes_through_the_service(client, signed_in, ctx, vent_model):
    signed_in("technician")
    r = post(client, "/api/v1/device-models/", {"manufacturer": "Philips   Healthcare", "model": "IntelliVue  MX450", "description": "Patient monitor",
                                                "category": "Physiologic monitors", "risk_class": RiskClass.HIGH, "oem_pm_interval_months": 12})
    assert r.status_code == 201, r.json()
    assert (r.json()["manufacturer"], r.json()["model"], r.json()["expected_life_years"], r.json()["pm_interval_months"]) == (
        "Philips Healthcare", "IntelliVue MX450", 8, 12)
    assert DeviceModel.objects.get(id=r.json()["id"]).tenant_id == ctx.id


@pytest.mark.parametrize("change, key", [
    ({"manufacturer": "hamilton medical", "model": "HAMILTON-G5"}, "model"),
    ({"risk_class": None}, "risk_class"),
    ({"oem_pm_interval_months": 0}, "oem_pm_interval_months"),
    ({"oem_pm_interval_months": 121}, "oem_pm_interval_months"),
    ({"expected_life_years": 51}, "expected_life_years"),
    ({"list_cost": "-1"}, "list_cost"),
    ({"aem_interval_months": 24}, "aem_interval_months"),
    ({"description": ""}, "description"),
])
def test_device_model_field_errors_are_400s(client, signed_in, ctx, vent_model, change, key):
    signed_in("technician")
    body = {"manufacturer": "Philips", "model": "MX450", "description": "Patient monitor", "category": "Monitors", "risk_class": RiskClass.HIGH, **change}
    body = {k: v for k, v in body.items() if v is not None}
    r = post(client, "/api/v1/device-models/", body)
    assert r.status_code == 400 and list(r.json()) == [key], r.json()
    assert DeviceModel.objects.count() == 1


def test_the_same_model_is_fine_in_another_facility(client, signed_in, ctx, theirs):
    signed_in("technician")
    r = post(client, "/api/v1/device-models/", {"manufacturer": "Hamilton Medical", "model": "Hamilton-G5", "description": "ICU ventilator",
                                                "category": "Ventilators", "risk_class": RiskClass.LIFE_SUPPORT, "oem_pm_interval_months": 6})
    assert r.status_code == 201 and r.json()["id"] != str(theirs["model"].id)


def test_department_create_reuses_a_name_in_any_case(client, signed_in, ctx, dept, theirs):
    signed_in("technician")
    r = post(client, "/api/v1/departments/", {"name": "icu"})
    assert r.status_code == 200 and r.json()["id"] == str(dept.id) and r.json()["name"] == "ICU"
    r = post(client, "/api/v1/departments/", {"name": "  Cardiac   Cath Lab "})
    assert r.status_code == 201 and r.json()["name"] == "Cardiac Cath Lab"
    assert sorted(Department.objects.values_list("name", flat=True)) == ["Cardiac Cath Lab", "ICU"]


@pytest.mark.parametrize("body, key", [({"name": "  "}, "name"), ({}, "name"), ({"name": "Oncology", "cost_center": "4410"}, "cost_center")])
def test_department_errors_are_400s(client, signed_in, ctx, dept, body, key):
    signed_in("technician")
    r = post(client, "/api/v1/departments/", body)
    assert r.status_code == 400 and list(r.json()) == [key]
    assert Department.objects.count() == 1


# --- deleting ---------------------------------------------------------------------------------------------------------------

def test_deleting_what_is_still_in_use_is_a_400_not_a_500(client, signed_in, ctx, vent, dept, vent_model):
    create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Alarm fault")
    signed_in("director")
    r = client.delete(one(vent))
    assert r.status_code == 400 and r.json()["detail"] == "CE-10001 has work orders or service requests, so it cannot be deleted; retire it instead."
    r = client.delete(f"/api/v1/device-models/{vent_model.id}/")
    assert r.status_code == 400 and r.json()["detail"] == "Hamilton Medical Hamilton-G5 has devices, so it cannot be deleted."
    r = client.delete(f"/api/v1/departments/{dept.id}/")
    assert r.status_code == 400 and r.json()["detail"] == "ICU has devices or service requests, so it cannot be deleted."
    assert Asset.objects.count() == DeviceModel.objects.count() == Department.objects.count() == 1


def test_deleting_a_mistaken_entry_still_works_and_needs_full(client, signed_in, ctx, dept, vent_model):
    a = Asset.objects.create(tag="CE-TYPO", device_model=vent_model, department=dept, next_pm_on=TODAY)
    signed_in("manager")
    assert client.delete(one(a)).status_code == 403
    client.logout()
    signed_in("director")
    assert client.delete(one(a)).status_code == 204 and not Asset.objects.exists()


# --- tenant isolation and reads ---------------------------------------------------------------------------------------------

def test_another_facilitys_device_is_a_404(client, signed_in, ctx, vent, theirs):
    signed_in("director")
    a = theirs["asset"]
    assert client.get(one(a)).status_code == 404
    assert patch(client, one(a), {"room": "x"}).status_code == 404
    assert put(client, one(a), {"tag": "CE-10001", "device_model": str(theirs["model"].id), "department": str(theirs["dept"].id)}).status_code == 404
    assert post(client, status_url(a), {"to": S.RETIRED}).status_code == 404
    assert [x["id"] for x in client.get(ASSETS).json()["results"]] == [str(vent.id)]
    assert client.get(f"/api/v1/device-models/{theirs['model'].id}/").status_code == 404
    assert client.get(f"/api/v1/departments/{theirs['dept'].id}/").status_code == 404
    with tenant_context(theirs["asset"].tenant):
        a.refresh_from_db()
        assert (a.status, a.room) == (S.IN_SERVICE, "")


def test_reads_and_filters_are_unchanged(client, signed_in, ctx, vent, pump):
    signed_in("requester")
    assert [x["tag"] for x in client.get(f"{ASSETS}?search=10002").json()["results"]] == ["CE-10002"]
    assert [x["tag"] for x in client.get(f"{ASSETS}?ordering=-tag").json()["results"]] == ["CE-10002", "CE-10001"]
    assert client.get(f"{ASSETS}{vent.id}/qualified_technicians/").status_code == 200


def test_the_device_list_runs_a_fixed_number_of_queries(client, signed_in, ctx, dept, vent_model, pump_model):
    contract = Contract.objects.create(reference="SC-1", vendor="V", type=ContractType.OEM, start_on=TODAY, end_on=TODAY + timedelta(days=365))
    signed_in("technician")

    def count(n):
        for i in range(n):
            a = Asset.objects.create(tag=f"Q-{n}-{i}", device_model=vent_model if i % 2 else pump_model, department=dept, next_pm_on=TODAY)
            if i % 3 == 0:
                ct.add_asset(contract, a)
        with CaptureQueriesContext(connection) as q:
            assert len(client.get(ASSETS).json()["results"]) == Asset.objects.count()
        return len(q)

    assert count(2) == count(6)


def test_the_api_and_the_service_agree_on_what_an_edit_changes():
    writable = {name for name, f in AssetSerializer().fields.items() if not f.read_only}
    assert writable == set(svc.EDITABLE_FIELDS) | set(AssetViewSet.FIXED_ON_UPDATE)
    assert not set(svc.EDITABLE_FIELDS) & set(AssetViewSet.FIXED_ON_UPDATE)


def test_acquisition_cost_round_trips_as_a_decimal(client, signed_in, ctx, vent):
    signed_in("technician")
    assert patch(client, one(vent), {"acquisition_cost": "36500.25"}).json()["acquisition_cost"] == "36500.25"
    vent.refresh_from_db()
    assert vent.acquisition_cost == Decimal("36500.25")
