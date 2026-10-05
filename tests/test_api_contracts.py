"""Contracts over the API through apps.contracts.services (slice 19, part A; apps/api/views_contracts.py), as the Contracts screen changes
them (apps/web/views_contracts.py): create_contract, update_contract, renew_contract, delete_contract, add_asset / add_model, and
remove_asset, at the screen's levels (Edit to change, Full to delete), by session and by token. Closed to scoped users
(tests/test_scoping_api.py)."""
from datetime import date, timedelta

import pytest
from django.test import Client
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import User, create_default_roles
from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType, Coverage
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, SupportType
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

API = "/api/v1/contracts/"
HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
NEW = {"reference": "SC-2026-200", "vendor": "BD", "type": ContractType.THIRD_PARTY, "coverage": Coverage.PARTS_LABOR,
       "start_on": TODAY.isoformat(), "end_on": (TODAY + timedelta(days=365)).isoformat(), "annual_cost": "1200.00", "notes": "Loaner in 24 h"}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def contract(ctx, vent):
    c = ct.create_contract(reference="SC-2026-118", vendor="Hamilton Medical", type=ContractType.OEM, start_on=TODAY - timedelta(days=100),
                           end_on=TODAY + timedelta(days=265), annual_cost=5000, notes="Loaner within 24 h")
    ct.add_asset(c, vent)
    return c


@pytest.fixture
def theirs(tenant, other_tenant):
    """A contract, device model, and device that belong to the other facility."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d)
        c = ct.create_contract(reference="THEIRS-CT", vendor="X", start_on=TODAY, end_on=TODAY + timedelta(days=300))
        ct.add_asset(c, asset)
    return {"contract": c, "model": dm, "asset": asset}


def one(c, action=""):
    return f"{API}{c.pk}/{action}"


def post(client, address, body=None, **extra):
    return client.post(address, body if body is not None else {}, content_type="application/json", **extra)


def patch(client, address, body, **extra):
    return client.patch(address, body, content_type="application/json", **extra)


def token(make_user, slug, tenant_=None):
    return {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=make_user(slug, tenant_)).key}"}


def history_users(c):
    return [h.history_user for h in Contract.history.filter(id=c.pk).order_by("history_date", "history_id")]


# --- reading ---------------------------------------------------------------------------------------------------------------------

def test_the_list_counts_the_devices_each_contract_covers(client, signed_in, contract, dept, pump_model, pump, theirs):
    ct.add_asset(contract, pump)
    retired = Asset.objects.create(tag="CE-10009", device_model=pump_model, department=dept, contract=contract)
    retired.status = AssetStatus.RETIRED
    retired.save()
    empty = ct.create_contract(reference="SC-EMPTY", vendor="Steris", start_on=TODAY, end_on=TODAY + timedelta(days=30))
    signed_in("technician")  # Contracts View
    rows = client.get(API).json()["results"]
    assert [(r["reference"], r["device_count"], r["status"]) for r in rows] == [("SC-EMPTY", 0, "ending"), ("SC-2026-118", 2, "active")]
    assert client.get(one(contract)).json()["device_count"] == contract.covered_assets().count() == 2
    assert client.get(one(empty)).json()["device_count"] == 0
    assert [r["reference"] for r in client.get(f"{API}?search=steris").json()["results"]] == ["SC-EMPTY"]


# --- create, update, renew, delete -----------------------------------------------------------------------------------------------

def test_create_goes_through_the_service(client, signed_in, ctx):
    user = signed_in("manager")  # Contracts Edit
    r = post(client, API, {**NEW, "reference": "  SC-2026-200 ", "vendor": " BD "})
    assert r.status_code == 201, r.content
    c = Contract.objects.get()
    data = r.json()
    assert (data["id"], data["reference"], data["vendor"], data["type"], data["coverage"], data["annual_cost"], data["device_count"]) == \
        (str(c.pk), "SC-2026-200", "BD", "third_party", "parts_labor", "1200.00", 0)  # trimmed, as the service saves it
    assert c.tenant_id == ctx.id and history_users(c) == [user]


def test_the_api_and_the_screen_create_the_same_contract(client, signed_in, ctx):
    signed_in("manager")
    assert client.post("/contracts/new/", {**NEW, "reference": "WEB-1"}, **HX).status_code == 200
    assert post(client, API, {**NEW, "reference": "API-1"}).status_code == 201
    web, api = Contract.objects.get(reference="WEB-1"), Contract.objects.get(reference="API-1")
    fields = ("vendor", "type", "coverage", "start_on", "end_on", "annual_cost", "notes")
    assert [getattr(web, f) for f in fields] == [getattr(api, f) for f in fields]


@pytest.mark.parametrize("change, field, text", [
    ({"reference": "sc-2026-118"}, "reference", "sc-2026-118 is already used by another contract."),  # any letter case
    ({"reference": "  "}, "reference", ""),
    ({"vendor": ""}, "vendor", ""),
    ({"end_on": (TODAY - timedelta(days=1)).isoformat()}, "end_on", "The end date must be on or after the start date."),
    ({"annual_cost": "-5"}, "annual_cost", "Annual cost cannot be negative."),
    ({"type": "in_house"}, "type", ""),
    ({"coverage": "everything"}, "coverage", ""),
    ({"start_on": "next week"}, "start_on", ""),
    ({"notes": "x" * 501}, "notes", "500"),
    ({"device_ids": ["x"]}, "device_ids", "Unknown field."),
])
def test_create_refusals_are_400s_keyed_by_field(client, signed_in, contract, change, field, text):
    signed_in("manager")
    r = post(client, API, {**NEW, **change})
    assert r.status_code == 400 and list(r.json()) == [field] and text in r.json()[field][0], r.json()
    assert list(Contract.objects.values_list("reference", flat=True)) == ["SC-2026-118"]


def test_update_goes_through_the_service_and_moves_the_support_type(client, signed_in, contract, vent):
    user = signed_in("manager")
    assert vent.support_type == SupportType.OEM_CONTRACT
    r = patch(client, one(contract), {"type": ContractType.THIRD_PARTY, "vendor": "TriMedx"})
    assert r.status_code == 200 and (r.json()["type"], r.json()["vendor"], r.json()["device_count"]) == ("third_party", "TriMedx", 1)
    vent.refresh_from_db()
    assert vent.support_type == SupportType.THIRD_PARTY  # update_contract re-saves the devices; a plain save would not
    assert history_users(contract)[-1] == user


def test_put_takes_back_what_get_returned(client, signed_in, contract):
    signed_in("manager")
    body = client.get(one(contract)).json()
    body["notes"] = "Response within 4 h"
    r = client.put(one(contract), body, content_type="application/json")
    assert r.status_code == 200, r.content
    contract.refresh_from_db()
    assert contract.notes == "Response within 4 h" and contract.reference == "SC-2026-118"


@pytest.mark.parametrize("change, field", [({"reference": "SC-OTHER"}, "reference"), ({"end_on": "2001-01-01"}, "end_on"),
                                           ({"annual_cost": "-1"}, "annual_cost"), ({"assets": []}, "assets")])
def test_update_refusals_are_400s_and_change_nothing(client, signed_in, contract, change, field):
    ct.create_contract(reference="SC-OTHER", vendor="X", start_on=TODAY, end_on=TODAY + timedelta(days=10))
    signed_in("manager")
    r = patch(client, one(contract), {"vendor": "Changed", **change})
    assert r.status_code == 400 and field in r.json(), r.json()
    contract.refresh_from_db()
    assert (contract.vendor, contract.reference, contract.annual_cost) == ("Hamilton Medical", "SC-2026-118", 5000)


def test_renew(client, signed_in, contract, ctx):
    user = signed_in("manager")
    end = contract.end_on
    r = post(client, one(contract, "renew/"))
    assert r.status_code == 200 and r.json()["end_on"] == add_months(end, 12).isoformat()
    lapsed = ct.create_contract(reference="OLD", vendor="X", start_on=TODAY - timedelta(days=500), end_on=TODAY - timedelta(days=30))
    assert post(client, one(lapsed, "renew/")).json()["end_on"] == add_months(TODAY, 12).isoformat()  # from today, as on the screen
    future = ct.create_contract(reference="SOON", vendor="X", start_on=TODAY + timedelta(days=30), end_on=TODAY + timedelta(days=60))
    assert post(client, one(future, "renew/")).json()["start_on"] == TODAY.isoformat()
    assert history_users(contract)[-1] == user
    r = post(client, one(contract, "renew/"), {"months": 24})
    assert r.status_code == 400 and r.json() == {"months": ["Unknown field."]}
    contract.refresh_from_db()
    assert contract.end_on == add_months(end, 12)


def test_the_api_and_the_screen_renew_alike(client, signed_in, contract, ctx):
    twin = ct.create_contract(reference="TWIN", vendor="X", start_on=contract.start_on, end_on=contract.end_on)
    signed_in("manager")
    assert client.post(f"/contracts/{twin.pk}/renew/", **HX).status_code == 200
    assert post(client, one(contract, "renew/")).status_code == 200
    contract.refresh_from_db()
    twin.refresh_from_db()
    assert (contract.start_on, contract.end_on) == (twin.start_on, twin.end_on)


def test_delete_needs_full_and_returns_the_devices_to_in_house(client, signed_in, contract, vent, pump):
    ct.add_asset(contract, pump)
    pump.status = AssetStatus.RETIRED
    pump.save()
    signed_in("manager")  # Contracts Edit: not enough, as on the screen
    r = client.delete(one(contract))
    assert r.status_code == 403 and Contract.objects.exists()
    signed_in("director")
    r = client.delete(one(contract))
    assert r.status_code == 200 and r.json() == {"id": str(contract.pk), "reference": "SC-2026-118", "uncovered": 1}  # the retired pump does not count
    vent.refresh_from_db()
    assert not Contract.objects.exists() and vent.contract is None and vent.support_type == SupportType.IN_HOUSE


# --- the devices it covers -------------------------------------------------------------------------------------------------------

def test_add_devices_by_id(client, signed_in, contract, dept, pump_model, vent, pump):
    other = ct.create_contract(reference="B", vendor="BD", start_on=TODAY, end_on=TODAY + timedelta(days=100))
    moved = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept)
    ct.add_asset(other, moved)
    signed_in("manager")
    r = post(client, one(contract, "add_assets/"), {"asset_ids": [str(pump.pk), str(moved.pk).upper(), str(vent.pk), str(pump.pk)]})
    assert r.status_code == 200, r.content
    assert r.json() == {"added": 2, "device_count": 3, "devices": [{"asset_id": str(pump.pk), "tag": "CE-10002", "from_contract": None},
                                                                    {"asset_id": str(moved.pk), "tag": "CE-10003", "from_contract": "B"}]}
    for a in (pump, moved):
        a.refresh_from_db()
        assert a.contract == contract and a.support_type == SupportType.OEM_CONTRACT
    assert other.covered_assets().count() == 0


def test_add_every_device_of_a_model(client, signed_in, contract, dept, pump_model, pump):
    other = ct.create_contract(reference="B", vendor="BD", start_on=TODAY, end_on=TODAY + timedelta(days=100))
    on_other = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept)
    ct.add_asset(other, on_other)
    Asset.objects.create(tag="CE-10004", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    signed_in("manager")
    r = post(client, one(contract, "add_assets/"), {"device_model": str(pump_model.pk)})
    assert r.status_code == 200 and r.json() == {"added": 2, "device_count": 3, "devices": [
        {"asset_id": str(pump.pk), "tag": "CE-10002", "from_contract": None}, {"asset_id": str(on_other.pk), "tag": "CE-10003", "from_contract": "B"}]}
    assert post(client, one(contract, "add_assets/"), {"device_model": str(pump_model.pk)}).json() == {"added": 0, "devices": [], "device_count": 3}


def test_the_api_and_the_screen_add_alike(client, signed_in, contract, dept, pump_model, pump):
    twin = ct.create_contract(reference="TWIN", vendor="X", start_on=TODAY, end_on=TODAY + timedelta(days=100))
    signed_in("manager")
    assert client.post(f"/contracts/{twin.pk}/add-model/", {"device_model": str(pump_model.pk)}, **HX).status_code == 200
    web = set(twin.covered_assets().values_list("tag", flat=True))
    assert post(client, one(contract, "add_assets/"), {"device_model": str(pump_model.pk)}).json()["added"] == len(web) == 1
    assert set(contract.covered_assets().values_list("tag", flat=True)) == web | {"CE-10001"}


def test_a_retired_device_refuses_the_whole_batch(client, signed_in, contract, dept, pump_model, pump):
    retired = Asset.objects.create(tag="CE-10009", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    signed_in("manager")
    r = post(client, one(contract, "add_assets/"), {"asset_ids": [str(pump.pk), str(retired.pk)]})
    assert r.status_code == 400 and r.json() == {"asset_ids": ["CE-10009 is retired and cannot be put on a contract."]}
    pump.refresh_from_db()
    assert pump.contract is None and contract.covered_assets().count() == 1  # all or none


@pytest.mark.parametrize("body, field, text", [
    ({}, "asset_ids", "Send asset_ids (a list of device ids) or device_model"),
    ({"asset_ids": []}, "asset_ids", "Send asset_ids"),
    ({"asset_ids": "CE-10002"}, "asset_ids", "Send a list of device ids."),
    ({"asset_ids": [1, 2]}, "asset_ids", "Send a list of device ids."),
    ({"asset_ids": ["not-an-id"]}, "asset_ids", "Not a device in this facility: not-an-id."),
    ({"device_model": "not-an-id"}, "device_model", "Choose a device model from this facility."),
    ({"asset_ids": ["x"], "device_model": "y"}, "detail", "Send asset_ids or device_model, not both."),
    ({"asset_ids": ["x"], "tag": "CE-10002"}, "tag", "Unknown field."),
])
def test_add_refusals_are_400s(client, signed_in, contract, pump, body, field, text):
    signed_in("manager")
    r = post(client, one(contract, "add_assets/"), body)
    assert r.status_code == 400 and field in r.json() and text in str(r.json()[field]), r.json()
    pump.refresh_from_db()
    assert pump.contract is None


def test_another_facilitys_devices_and_models_are_unknown(client, signed_in, contract, theirs):
    signed_in("director")
    r = post(client, one(contract, "add_assets/"), {"asset_ids": [str(theirs["asset"].pk)]})
    assert r.status_code == 400 and r.json() == {"asset_ids": [f"Not a device in this facility: {theirs['asset'].pk}."]}
    r = post(client, one(contract, "add_assets/"), {"device_model": str(theirs["model"].pk)})
    assert r.status_code == 400 and "device_model" in r.json()
    r = post(client, one(contract, "remove_asset/"), {"asset_id": str(theirs["asset"].pk)})
    assert r.status_code == 400 and r.json() == {"asset_id": ["Choose a device from this facility."]}
    with tenant_context(theirs["contract"].tenant):
        assert Asset.objects.get().contract_id == theirs["contract"].pk


def test_remove_a_device(client, signed_in, contract, vent, pump):
    signed_in("manager")
    r = post(client, one(contract, "remove_asset/"), {"asset_id": str(pump.pk)})
    assert r.status_code == 400 and r.json() == {"asset_id": ["CE-10002 is not on SC-2026-118."]}
    r = post(client, one(contract, "remove_asset/"), {"asset_id": str(vent.pk)})
    assert r.status_code == 200 and r.json() == {"removed": {"asset_id": str(vent.pk), "tag": "CE-10001"}, "device_count": 0}
    vent.refresh_from_db()
    assert vent.contract is None and vent.support_type == SupportType.IN_HOUSE
    assert post(client, one(contract, "remove_asset/"), {}).json() == {"asset_id": ["Choose a device from this facility."]}
    assert post(client, one(contract, "remove_asset/"), {"asset_id": str(vent.pk), "force": True}).json() == {"force": ["Unknown field."]}


# --- who may do what -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role, may_read, may_change, may_delete", [
    ("director", True, True, True), ("manager", True, True, False), ("technician", True, False, False), ("analyst", True, False, False),
    ("requester", False, False, False), ("vendor", False, False, False),
])
def test_every_endpoint_checks_the_level_server_side(client, make_user, contract, vent, pump_model, role, may_read, may_change, may_delete):
    user = make_user(role)
    user.company, user.department = "Hamilton Medical", "ICU"  # the scoped roles are refused whatever their share
    user.save()
    client.force_login(user)
    assert client.get(API).status_code == (200 if may_read else 403)
    assert client.get(one(contract)).status_code == (200 if may_read else 403)
    change = 200 if may_change else 403
    assert post(client, API, NEW).status_code == (201 if may_change else 403)
    assert patch(client, one(contract), {"notes": "x"}).status_code == change
    assert post(client, one(contract, "renew/")).status_code == change
    assert post(client, one(contract, "add_assets/"), {"device_model": str(pump_model.pk)}).status_code == change
    assert post(client, one(contract, "remove_asset/"), {"asset_id": str(vent.pk)}).status_code == change
    assert client.delete(one(contract)).status_code == (200 if may_delete else 403)
    assert Contract.objects.filter(pk=contract.pk).exists() != may_delete


def test_another_facilitys_contract_is_404(client, signed_in, contract, vent, theirs):
    signed_in("director")
    c = theirs["contract"]
    assert client.get(one(c)).status_code == 404
    assert patch(client, one(c), {"notes": "x"}).status_code == 404
    assert post(client, one(c, "renew/")).status_code == 404
    assert post(client, one(c, "add_assets/"), {"asset_ids": [str(vent.pk)]}).status_code == 404
    assert post(client, one(c, "remove_asset/"), {"asset_id": str(theirs["asset"].pk)}).status_code == 404
    assert client.delete(one(c)).status_code == 404
    assert [r["reference"] for r in client.get(API).json()["results"]] == ["SC-2026-118"]
    with tenant_context(c.tenant):
        c.refresh_from_db()
        assert c.notes == "" and Asset.objects.get().contract_id == c.pk


def test_the_browsable_api_renders_and_its_form_posts_through_the_service(client, signed_in, contract):
    user = signed_in("manager")
    r = client.get(API, HTTP_ACCEPT="text/html")
    assert r.status_code == 200 and "SC-2026-118" in r.content.decode()
    assert client.get(one(contract), HTTP_ACCEPT="text/html").status_code == 200
    # The HTML form posts form-encoded with its CSRF token (enforced here, as in a browser), which is not refused as an unknown field.
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(user)
    browser.get(API, HTTP_ACCEPT="text/html")
    form = {**NEW, "reference": "FORM-1", "csrfmiddlewaretoken": browser.cookies["csrftoken"].value}
    r = browser.post(API, form, HTTP_ACCEPT="application/json")
    assert r.status_code == 201, r.content
    assert history_users(Contract.objects.get(reference="FORM-1")) == [user]


def test_a_superuser_without_a_facility_is_told_to_pick_one(client, db):
    client.force_login(User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com"))
    for r in (client.get(API), post(client, API, NEW)):
        assert r.status_code == 403 and "Pick a tenant first" in r.json()["detail"]
    assert not Contract.unscoped.exists()  # unscoped: nothing written in any facility


# --- tokens ----------------------------------------------------------------------------------------------------------------------

def test_every_write_by_token(client, make_user, tenant, contract, vent, pump, pump_model):
    """Token requests carry no session: the views set the token's facility (TenantAPIMixin), and every write lands in it."""
    auth = token(make_user, "director")
    r = post(client, API, NEW, **auth)
    assert r.status_code == 201, r.content
    new = r.json()["id"]
    assert patch(client, f"{API}{new}/", {"vendor": "BD Services"}, **auth).json()["vendor"] == "BD Services"
    assert post(client, f"{API}{new}/renew/", **auth).status_code == 200
    assert post(client, f"{API}{new}/add_assets/", {"asset_ids": [str(pump.pk)]}, **auth).json()["added"] == 1
    assert post(client, one(contract, "add_assets/"), {"device_model": str(pump_model.pk)}, **auth).json()["devices"][0]["from_contract"] == "SC-2026-200"
    assert post(client, one(contract, "remove_asset/"), {"asset_id": str(vent.pk)}, **auth).status_code == 200
    assert client.delete(one(contract), **auth).json()["uncovered"] == 1
    with tenant_context(tenant):
        c = Contract.objects.get()
        assert (str(c.pk), c.vendor, c.end_on) == (new, "BD Services", add_months(TODAY + timedelta(days=365), 12))
        assert not Asset.objects.filter(contract__isnull=False).exists()
    analyst = token(make_user, "analyst")
    assert client.get(API, **analyst).status_code == 200 and post(client, API, {**NEW, "reference": "X"}, **analyst).status_code == 403


@needs_postgres
def test_the_endpoints_by_token_under_the_policies(client, make_user, tenant, contract, vent, pump, pump_model, theirs):
    """As the runtime role, by token: every contract change reads and writes inside the token's facility, and another facility's
    contract, device, and model stay out of reach."""
    auth = token(make_user, "director")
    as_app_role()
    r = post(client, API, NEW, **auth)
    assert r.status_code == 201, r.content
    new = r.json()["id"]
    assert patch(client, f"{API}{new}/", {"type": ContractType.OEM}, **auth).status_code == 200
    assert post(client, f"{API}{new}/renew/", **auth).status_code == 200
    assert post(client, f"{API}{new}/add_assets/", {"asset_ids": [str(pump.pk)]}, **auth).json()["added"] == 1
    assert post(client, one(contract, "add_assets/"), {"device_model": str(pump_model.pk)}, **auth).json()["added"] == 1
    assert post(client, one(contract, "add_assets/"), {"asset_ids": [str(theirs["asset"].pk)]}, **auth).status_code == 400
    assert post(client, one(contract, "remove_asset/"), {"asset_id": str(vent.pk)}, **auth).status_code == 200
    assert client.get(one(theirs["contract"]), **auth).status_code == 404
    assert [c["reference"] for c in client.get(API, **auth).json()["results"]] == ["SC-2026-118", "SC-2026-200"]
    assert client.delete(one(contract), **auth).json()["uncovered"] == 1
    with tenant_context(tenant):
        c = Contract.objects.get()
        assert str(c.pk) == new and c.type == ContractType.OEM and not Asset.objects.filter(contract__isnull=False).exists()
    with tenant_context(theirs["contract"].tenant):
        assert Asset.objects.get().contract_id == theirs["contract"].pk
