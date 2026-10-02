"""Slice 16 in the API: a scoped user (the vendor technician sees only work orders assigned to their company and those devices; the
clinical requester only their unit's devices and those devices' work orders; apps.workorders.scoping) gets nothing else.

Closed by default: every endpoint refuses a scoped user (403) whatever their levels, except the actions a view names in
`scoped_actions` (work orders: list, retrieve, create, update, transition, assign; devices: list, retrieve, update, status), which
narrow every row to the share, so a record outside it is a 404 as another facility's is, and search and ordering cannot widen it."""
from datetime import date, timedelta

import pytest

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.api import urls as api_urls
from apps.api.permissions import ModulePermission
from apps.api.views import AssetViewSet, WorkOrderViewSet
from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoStatus
from apps.workorders.services import create_work_order

API = "/api/v1/"
WOS, ASSETS = f"{API}work-orders/", f"{API}assets/"
TODAY = date.today()
ALL_FULL = {m: Level.FULL for m in Module.values}


def wo_url(wo, action=""):
    return f"{WOS}{wo.id}/{action}"


def asset_url(asset, action=""):
    return f"{ASSETS}{asset.id}/{action}"


def post(client, url, body=None):
    return client.post(url, body or {}, content_type="application/json")


def patch(client, url, body):
    return client.patch(url, body, content_type="application/json")


def results(r):
    assert r.status_code == 200, r.content
    return r.json()["results"]


@pytest.fixture
def world(ctx, dept, vent, pump, pump_model, techs):
    """Two units (ICU, Cardiology) and two companies (Hamilton Medical, Acme Biomedical) in one facility."""
    cardio = Department.objects.create(name="Cardiology")
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX800", description="Patient monitor", category="Monitors",
                                         risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    card_mon = Asset.objects.create(tag="CE-20001", device_model=monitor, department=cardio, next_pm_on=TODAY + timedelta(days=40))
    card_pump = Asset.objects.create(tag="CE-20002", device_model=pump_model, department=cardio, next_pm_on=TODAY + timedelta(days=60))

    def wo(asset, problem, **kw):
        return create_work_order(asset=asset, type="repair", priority="normal", problem=problem, **kw)

    wos = {
        "ham_vent": wo(vent, "Flow sensor fault", vendor_service=True, vendor_name="Hamilton Medical field service"),
        "ham_card": wo(card_pump, "Keypad sticks", vendor_service=True, vendor_name="HAMILTON MEDICAL"),
        "acme_card": wo(card_mon, "Monitor shows no display", vendor_service=True, vendor_name="Acme Biomedical"),
        "inhouse_icu": wo(pump, "Occlusion alarm", assigned_to=techs["dana"]),
        "inhouse_card": wo(card_pump, "Door latch loose"),
        # Named after the company but no longer vendor service (worked in-house): not the company's.
        "stale_icu": wo(pump, "Battery will not hold a charge", vendor_name="Hamilton Medical"),
    }
    contract = Contract.objects.create(reference="SC-ACME-1", vendor="Acme Biomedical", type=ContractType.OEM, start_on=TODAY,
                                       end_on=TODAY + timedelta(days=365))
    return {"assets": {"vent": vent, "pump": pump, "card_mon": card_mon, "card_pump": card_pump}, "wos": wos, "cardio": cardio,
            "icu": dept, "monitor": monitor, "contract": contract}


@pytest.fixture
def theirs(other_tenant):
    """Another facility's device and a work order for the same company name."""
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                       risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        a = Asset.objects.create(tag="CE-10001", device_model=m, department=d, next_pm_on=TODAY + timedelta(days=10))
        w = create_work_order(asset=a, type="repair", priority="normal", problem="Their fault", vendor_service=True,
                              vendor_name="Hamilton Medical field service")
    return {"asset": a, "wo": w}


@pytest.fixture
def person(client, tenant, make_user):
    """Sign in as a default role (by slug) or a custom role (a Role), with the company and department given."""
    n = iter(range(1000))

    def _as(role, company="", department=""):
        if isinstance(role, str):
            user = make_user(role, username=f"{role}-{next(n)}@riverside.example")
        else:
            user = User.objects.create_user(username=f"{role.slug}-{next(n)}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
        user.company, user.department = company, department
        user.save()
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def custom_role(ctx):
    def _make(slug, scope, levels):
        role = Role.objects.create(name=slug.replace("-", " ").title(), slug=slug, scope=scope)
        role.set_levels(levels)
        return role

    return _make


# Who sees what in `world`.
SHARES = {
    "vendor": ({"ham_vent", "ham_card"}, {"vent", "card_pump"}),
    "requester": ({"ham_vent", "inhouse_icu", "stale_icu"}, {"vent", "pump"}),
}
SIGN_IN = {"vendor": {"company": "Hamilton Medical"}, "requester": {"department": "icu"}}  # any letter case


def expect(world, who):
    wo_keys, asset_keys = SHARES[who]
    return {str(world["wos"][k].id) for k in wo_keys}, {str(world["assets"][k].id) for k in asset_keys}


def out_of_share(world, who):
    wo_keys, asset_keys = SHARES[who]
    return ([w for k, w in world["wos"].items() if k not in wo_keys], [a for k, a in world["assets"].items() if k not in asset_keys])


# --- lists ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_lists_hold_only_the_share(client, person, world, theirs, who):
    person(who, **SIGN_IN[who])
    wo_ids, asset_ids = expect(world, who)
    r = client.get(WOS)
    assert {w["id"] for w in results(r)} == wo_ids and r.json()["count"] == len(wo_ids)
    r = client.get(ASSETS)
    assert {a["id"] for a in results(r)} == asset_ids and r.json()["count"] == len(asset_ids)
    # Nothing outside the share is named anywhere in either list: not its id, not its number, not its tag.
    wos_out, assets_out = out_of_share(world, who)
    body = client.get(WOS).content.decode() + client.get(ASSETS).content.decode()
    for w in wos_out:
        assert str(w.id) not in body and f'"{w.number}"' not in body, w.problem
    for a in assets_out:
        assert str(a.id) not in body and a.tag not in body, a.tag
    assert str(theirs["wo"].id) not in body and str(theirs["asset"].id) not in body  # numbers and tags repeat across facilities


def test_a_facility_wide_role_still_sees_everything(client, person, world, theirs):
    for slug in ("director", "manager", "technician", "analyst"):
        person(slug)
        assert {w["id"] for w in results(client.get(WOS))} == {str(w.id) for w in world["wos"].values()}, slug
        assert {a["id"] for a in results(client.get(ASSETS))} == {str(a.id) for a in world["assets"].values()}, slug
        assert client.get(wo_url(world["wos"]["acme_card"])).status_code == 200
        assert client.get(wo_url(theirs["wo"])).status_code == 404  # tenant isolation as before


def test_a_role_set_to_the_whole_facility_sees_everything(client, person, world):
    """The scope is the role's: a facility that sets its vendor role to the whole facility opens it (blank takes the default)."""
    vendor = Role.objects.get(slug="vendor")
    vendor.scope = DataScope.FACILITY
    vendor.save()
    person("vendor", company="Hamilton Medical")
    assert len(results(client.get(WOS))) == len(world["wos"]) and len(results(client.get(ASSETS))) == len(world["assets"])


@pytest.mark.parametrize("who, attrs", [("vendor", {}), ("vendor", {"company": "  "}), ("vendor", {"company": "Nobody Inc"}),
                                        ("requester", {}), ("requester", {"department": "Oncology"})])
def test_a_scoped_user_without_a_company_or_unit_sees_nothing(client, person, world, who, attrs):
    person(who, **attrs)
    for url in (WOS, ASSETS, f"{WOS}?search=CE-", f"{ASSETS}?search=CE-"):
        r = client.get(url)
        assert results(r) == [] and r.json()["count"] == 0, url
    for w in world["wos"].values():
        assert client.get(wo_url(w)).status_code == 404
    for a in world["assets"].values():
        assert client.get(asset_url(a)).status_code == 404


# --- search, ordering, and other query strings ------------------------------------------------------------------------------

def test_search_and_ordering_cannot_widen_the_vendors_share(client, person, world):
    person("vendor", company="Hamilton Medical")
    wos, a = world["wos"], world["assets"]
    assert [w["id"] for w in results(client.get(f"{WOS}?search={wos['acme_card'].number}"))] == []
    assert [w["id"] for w in results(client.get(f"{WOS}?search=no display"))] == []  # acme_card's problem
    assert [w["id"] for w in results(client.get(f"{WOS}?search=CE-20002"))] == [str(wos["ham_card"].id)]  # not inhouse_card on that device
    assert {w["id"] for w in results(client.get(f"{WOS}?ordering=-due_on"))} == {str(wos["ham_vent"].id), str(wos["ham_card"].id)}
    # Query strings the API does not filter by are ignored, never read as a wider filter.
    assert {w["id"] for w in results(client.get(f"{WOS}?vendor_name=Acme+Biomedical&asset={a['card_mon'].id}&scope=facility"))} == \
        {str(wos["ham_vent"].id), str(wos["ham_card"].id)}
    assert [x["tag"] for x in results(client.get(f"{ASSETS}?search=Alaris"))] == ["CE-20002"]  # the ICU pump is the same model
    assert [x["tag"] for x in results(client.get(f"{ASSETS}?search=Philips"))] == []
    assert [x["tag"] for x in results(client.get(f"{ASSETS}?ordering=-tag"))] == ["CE-20002", "CE-10001"]
    # The format suffix and ?format= reach the same narrowed list.
    assert {w["id"] for w in client.get(f"{API}work-orders.json").json()["results"]} == {str(wos["ham_vent"].id), str(wos["ham_card"].id)}
    assert len(client.get(f"{ASSETS}?format=json").json()["results"]) == 2


def test_search_and_ordering_cannot_widen_the_requesters_share(client, person, world):
    person("requester", department="ICU")
    wos = world["wos"]
    assert results(client.get(f"{ASSETS}?search=Cardiology")) == []
    assert [x["tag"] for x in results(client.get(f"{ASSETS}?search=CE-&ordering=-tag"))] == ["CE-10002", "CE-10001"]
    assert results(client.get(f"{WOS}?search=CE-200")) == []
    assert results(client.get(f"{WOS}?search={wos['inhouse_card'].number}")) == []
    assert results(client.get(f"{WOS}?search=latch")) == []  # inhouse_card's problem
    assert [w["id"] for w in results(client.get(f"{WOS}?search=charge"))] == [str(wos["stale_icu"].id)]
    assert {w["id"] for w in results(client.get(f"{WOS}?ordering=priority"))} == expect(world, "requester")[0]


# --- single records and actions on them --------------------------------------------------------------------------------------

def test_the_vendor_works_only_their_companys_work_orders(client, person, world, theirs):
    person("vendor", company="hamilton medical")  # work orders: Edit
    wos_out, assets_out = out_of_share(world, "vendor")
    before = {w.id: (w.status, w.problem, w.vendor_service, w.vendor_name) for w in wos_out}
    for w in wos_out + [theirs["wo"]]:
        assert client.get(wo_url(w)).status_code == 404, w.problem
        assert patch(client, wo_url(w), {"problem": "Mine now"}).status_code == 404
        assert post(client, wo_url(w, "transition/"), {"status": WoStatus.IN_PROGRESS}).status_code == 404
        assert post(client, wo_url(w, "assign/"), {"vendor_name": "Hamilton Medical"}).status_code == 404
    for a in assets_out + [theirs["asset"]]:
        assert client.get(asset_url(a)).status_code == 404
    for w in wos_out:
        w.refresh_from_db()
        assert (w.status, w.problem, w.vendor_service, w.vendor_name) == before[w.id]
    # Their own: read, update, and move along; assigning still needs Approve.
    mine = world["wos"]["ham_vent"]
    assert client.get(wo_url(mine)).status_code == 200 and client.get(asset_url(world["assets"]["vent"])).status_code == 200
    assert patch(client, wo_url(mine), {"problem": "Flow sensor fault, replaced"}).status_code == 200
    assert post(client, wo_url(mine, "transition/"), {"status": WoStatus.IN_PROGRESS}).status_code == 200
    assert post(client, wo_url(mine, "assign/"), {"vendor_name": "Acme Biomedical"}).status_code == 403
    # Reassigning by PATCH is refused as for everyone (the assign action), so the vendor cannot move work out of or into the share.
    assert patch(client, wo_url(mine), {"vendor_name": "Acme Biomedical"}).status_code == 400
    mine.refresh_from_db()
    assert (mine.status, mine.vendor_name) == (WoStatus.IN_PROGRESS, "Hamilton Medical field service")


def test_the_requester_reads_only_their_units_work(client, person, world, theirs):
    person("requester", department="ICU")  # work orders: Request, equipment: View
    wos_out, assets_out = out_of_share(world, "requester")
    for w in wos_out + [theirs["wo"]]:
        assert client.get(wo_url(w)).status_code == 404
    for a in assets_out + [theirs["asset"]]:
        assert client.get(asset_url(a)).status_code == 404
    mine = world["wos"]["inhouse_icu"]
    assert client.get(wo_url(mine)).status_code == 200 and client.get(asset_url(world["assets"]["pump"])).status_code == 200
    # Their levels refuse every write, in their unit or not, before any record is looked up.
    for w in (mine, world["wos"]["acme_card"]):
        assert patch(client, wo_url(w), {"problem": "x"}).status_code == 403
        assert post(client, wo_url(w, "transition/"), {"status": WoStatus.IN_PROGRESS}).status_code == 403
    assert patch(client, asset_url(world["assets"]["pump"]), {"room": "4"}).status_code == 403


def test_a_scoped_role_with_approve_assigns_only_in_its_share(client, person, custom_role, world):
    person(custom_role("vendor-lead", DataScope.COMPANY, ALL_FULL), company="Hamilton Medical")
    assert post(client, wo_url(world["wos"]["acme_card"], "assign/"), {"vendor_name": "Hamilton Medical"}).status_code == 404
    r = post(client, wo_url(world["wos"]["ham_card"], "assign/"), {"vendor_name": "Hamilton Medical field service"})
    assert r.status_code == 200 and r.json()["vendor_name"] == "Hamilton Medical field service"
    world["wos"]["acme_card"].refresh_from_db()
    assert world["wos"]["acme_card"].vendor_name == "Acme Biomedical"


# --- creating work orders ---------------------------------------------------------------------------------------------------

def test_a_company_scoped_user_cannot_create_work_orders(client, person, custom_role, world):
    before = WorkOrder.objects.count()
    body = {"asset": str(world["assets"]["vent"].id), "problem": "Alarm", "type": "repair", "priority": "normal", "due_on": TODAY.isoformat()}
    person("vendor", company="Hamilton Medical")  # work orders: Edit, which would otherwise allow it
    r = post(client, WOS, body)
    assert r.status_code == 403 and "assigned to your company" in r.json()["detail"]
    person(custom_role("vendor-lead", DataScope.COMPANY, ALL_FULL), company="Hamilton Medical")
    assert post(client, WOS, body).status_code == 403
    assert WorkOrder.objects.count() == before


def test_a_department_scoped_user_creates_only_on_their_units_devices(client, person, custom_role, world, theirs):
    before = WorkOrder.objects.count()
    a = world["assets"]

    def body(asset):
        return {"asset": str(asset.id), "problem": "Alarm", "type": "repair", "priority": "normal", "due_on": (TODAY + timedelta(days=7)).isoformat()}

    person("requester", department="ICU")  # work orders: Request, so not through the API, as before
    assert post(client, WOS, body(a["vent"])).status_code == 403
    person(custom_role("unit-lead", DataScope.DEPARTMENT, {"workorders": Level.EDIT, "equipment": Level.VIEW}), department="ICU")
    for asset in (a["card_mon"], a["card_pump"], theirs["asset"]):
        r = post(client, WOS, body(asset))
        # Read as an unknown device, word for word as another facility's: the refusal does not say the device exists.
        assert r.status_code == 400 and r.json() == {"asset": [f'Invalid pk "{asset.id}" - object does not exist.']}, asset.tag
    assert WorkOrder.objects.count() == before
    r = post(client, WOS, body(a["vent"]))
    assert r.status_code == 201 and r.json()["asset_tag"] == "CE-10001", r.content
    created = WorkOrder.objects.get(pk=r.json()["id"])
    # It cannot then be moved onto a device outside the unit; within it, it can.
    assert patch(client, wo_url(created), {"asset": str(a["card_mon"].id)}).status_code == 400
    assert patch(client, wo_url(created), {"asset": str(a["pump"].id)}).status_code == 200
    created.refresh_from_db()
    assert created.asset == a["pump"]


# --- devices ----------------------------------------------------------------------------------------------------------------

def test_scoped_device_edits_stay_at_their_levels_and_in_the_share(client, person, custom_role, world):
    a = world["assets"]
    person(custom_role("unit-tech", DataScope.DEPARTMENT, {"equipment": Level.EDIT, "workorders": Level.VIEW}), department="ICU")
    assert patch(client, asset_url(a["vent"]), {"room": "ICU-4"}).status_code == 200
    assert post(client, asset_url(a["vent"], "status/"), {"to": AssetStatus.OUT_OF_SERVICE}).status_code == 200
    assert post(client, asset_url(a["vent"], "status/"), {"to": AssetStatus.RETIRED}).status_code == 403  # Approve, as before
    for other in (a["card_mon"], a["card_pump"]):
        assert patch(client, asset_url(other), {"room": "Mine"}).status_code == 404
        assert post(client, asset_url(other, "status/"), {"to": AssetStatus.OUT_OF_SERVICE}).status_code == 404
    r = patch(client, asset_url(a["pump"]), {"department": str(world["cardio"].id)})
    assert r.status_code == 403 and "whole facility" in r.json()["detail"]
    assert patch(client, asset_url(a["pump"]), {"department": str(world["icu"].id), "room": "ICU-5"}).status_code == 200  # unchanged is fine
    for x in a.values():
        x.refresh_from_db()
    assert (a["vent"].room, a["vent"].status, a["pump"].department_id, a["pump"].room) == ("ICU-4", AssetStatus.OUT_OF_SERVICE, world["icu"].id, "ICU-5")
    assert {x.room for x in (a["card_mon"], a["card_pump"])} == {""}
    assert {x.status for x in (a["card_mon"], a["card_pump"])} == {AssetStatus.IN_SERVICE}


def test_a_company_scoped_device_edit_reaches_only_its_companys_devices(client, person, custom_role, world):
    a = world["assets"]
    person(custom_role("vendor-eq", DataScope.COMPANY, {"equipment": Level.EDIT, "workorders": Level.EDIT}), company="Hamilton Medical")
    assert patch(client, asset_url(a["card_pump"]), {"room": "C-2"}).status_code == 200
    assert patch(client, asset_url(a["pump"]), {"room": "C-2"}).status_code == 404  # same model, no Hamilton work on it
    a["pump"].refresh_from_db()
    assert a["pump"].room == ""


# --- every other endpoint is closed to scoped users, whatever their levels ----------------------------------------------------

def closed_endpoints(world, pump_recall, techs):
    """Every endpoint that does not narrow to the share: (method, url, body)."""
    a, w, c = world["assets"]["vent"], world["wos"]["ham_vent"], world["contract"]
    day = (TODAY + timedelta(days=1)).isoformat()
    cred = techs["dana"].credentials.first()
    return [
        ("get", f"{API}departments/", None), ("get", f"{API}departments/{world['icu'].id}/", None),
        ("post", f"{API}departments/", {"name": "Oncology"}), ("patch", f"{API}departments/{world['icu'].id}/", {"cost_center": "9"}),
        ("delete", f"{API}departments/{world['cardio'].id}/", None),
        ("get", f"{API}device-models/", None), ("get", f"{API}device-models/{world['monitor'].id}/", None),
        ("post", f"{API}device-models/", {"manufacturer": "A", "model": "B", "description": "C", "category": "D", "risk_class": "low"}),
        ("patch", f"{API}device-models/{world['monitor'].id}/", {"description": "x"}),
        ("post", ASSETS, {"tag": "CE-30001", "device_model": str(world["monitor"].id), "department": str(world["icu"].id)}),
        ("delete", asset_url(a), None), ("get", asset_url(a, "qualified_technicians/"), None),
        ("delete", wo_url(w), None),
        ("get", f"{API}contracts/", None), ("get", f"{API}contracts/{c.id}/", None), ("patch", f"{API}contracts/{c.id}/", {"notes": "x"}),
        ("post", f"{API}contracts/{c.id}/add_assets/", {"asset_ids": [str(a.id)]}), ("post", f"{API}contracts/{c.id}/remove_asset/", {"asset_id": str(a.id)}),
        ("get", f"{API}technicians/", None), ("get", f"{API}technicians/{techs['dana'].id}/", None),
        ("get", f"{API}credentials/", None), ("delete", f"{API}credentials/{cred.id}/", None),
        ("get", f"{API}alert-matches/", None), ("get", f"{API}alert-matches/{pump_recall.id}/", None),
        ("post", f"{API}alert-matches/{pump_recall.id}/transition/", {"status": "under_review"}),
        ("post", f"{API}alert-matches/{pump_recall.id}/work-orders/", None),
        ("get", f"{API}overview/", None), ("get", f"{API}reports/", None), ("get", f"{API}reports/cosr/", None),
        ("get", f"{API}pm/calendar/", None), ("get", f"{API}pm/day/?day={day}", None),
        ("post", f"{API}pm/create-for-day/", {"day": day}), ("post", f"{API}pm/generate/", None),
        ("get", f"{API}settings/", None), ("patch", f"{API}settings/", {"portal_hotline": "x"}), ("post", f"{API}settings/reset-policy/", None),
    ]


def call(client, method, url, body):
    if method == "get":
        return client.get(url)
    if method == "delete":
        return client.delete(url)
    return getattr(client, method)(url, body or {}, content_type="application/json")


@pytest.mark.parametrize("scope, attrs", [(DataScope.COMPANY, {"company": "Hamilton Medical"}), (DataScope.DEPARTMENT, {"department": "ICU"})])
def test_every_other_endpoint_refuses_a_scoped_role_with_full_levels(client, person, custom_role, world, pump_recall, techs, scope, attrs):
    """A custom scoped role given Full everywhere (Contracts, Reports, PM, Settings...) still reads nothing facility-wide."""
    person(custom_role(f"scoped-{scope}", scope, ALL_FULL), **attrs)
    counts = (WorkOrder.objects.count(), Asset.objects.count(), Department.objects.count(), DeviceModel.objects.count())
    for method, url, body in closed_endpoints(world, pump_recall, techs):
        r = call(client, method, url, body)
        assert r.status_code == 403, (method, url, r.status_code)
        assert "part of this facility" in r.json()["detail"], url
    assert (WorkOrder.objects.count(), Asset.objects.count(), Department.objects.count(), DeviceModel.objects.count()) == counts
    pump_recall.refresh_from_db()
    assert pump_recall.status == "needs_action" and world["contract"].covered_assets().count() == 0


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_every_other_endpoint_refuses_the_default_scoped_roles(client, person, world, pump_recall, techs, who):
    person(who, **SIGN_IN[who])
    for method, url, body in closed_endpoints(world, pump_recall, techs):
        assert call(client, method, url, body).status_code == 403, (method, url)


def test_the_same_endpoints_still_answer_a_facility_wide_role(client, person, world, pump_recall, techs):
    person("director")
    for method, url, body in closed_endpoints(world, pump_recall, techs):
        if method == "get":
            assert call(client, method, url, body).status_code == 200, url


# --- the switch itself ---------------------------------------------------------------------------------------------------------

def _api_views():
    for p in api_urls.urlpatterns:
        cls = getattr(p.callback, "cls", None)
        if p.name != "api-root":  # the router's index lists the endpoint links, no facility data
            yield p, cls


def test_every_api_endpoint_goes_through_the_module_permission():
    """The scope switch lives in ModulePermission, so every endpoint must check it (a new one without it would be open)."""
    for p, cls in _api_views():
        assert cls is not None and ModulePermission in cls.permission_classes, p.pattern


def test_only_work_orders_and_devices_opt_in():
    """A view that names scoped actions must narrow its rows to the share and have a leak test here."""
    opted = {cls for _p, cls in _api_views() if getattr(cls, "scoped_actions", None)}
    assert opted == {AssetViewSet, WorkOrderViewSet}
    assert WorkOrderViewSet.scoped_actions == {"list", "retrieve", "create", "update", "partial_update", "transition", "assign"}
    assert AssetViewSet.scoped_actions == {"list", "retrieve", "update", "partial_update", "change_status"}
