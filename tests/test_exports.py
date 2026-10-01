"""
CSV exports of the lists (slice 11): Equipment, Work orders, Contracts, and a contract's device list.

Each needs its module's View, honours the list screen's own filters (every matching row, not one page), never shows another
tenant's rows, keeps formula-looking text as text, and runs a fixed number of queries however many rows it writes.
"""
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from csvutil import csv_rows, csv_text
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Level, Module, Role, User
from apps.contracts import services as ct
from apps.contracts.models import ContractType, Coverage
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.recalls.models import Alert
from apps.tenants.context import get_current_tenant, tenant_context
from apps.tenants.management.commands.enable_rls import tenant_scoped_tables
from apps.web.exports import cell, csv_response
from apps.web.views_exports import CONTRACT_COLUMNS, DEVICE_COLUMNS, EQUIPMENT_COLUMNS, WORKORDER_COLUMNS
from apps.workorders.models import LaborLine, PartLine
from apps.workorders.services import assign, change_status, create_work_order

TODAY = date.today()
STAMP = f"{TODAY:%Y-%m-%d}"
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def custom_user(tenant, levels, username):
    role = Role.objects.create(name=username.split("@")[0], slug=username.split("@")[0])
    role.set_levels(levels)
    return User.objects.create_user(username=username, password="Test-Pass-2026-x", tenant=tenant, role=role)


def download(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    return r, csv_rows(r)


def records(rows) -> list[dict]:
    """Data rows as {header: value}."""
    return [dict(zip(rows[0], row)) for row in rows[1:]]


def column(rows, name) -> list[str]:
    return [r[name] for r in records(rows)]


def count_queries(client, url) -> int:
    """Queries for the whole download: the rows are read while the response streams, so the count covers reading it."""
    with CaptureQueriesContext(connection) as q:
        csv_rows(client.get(url))
    return len(q.captured_queries)


# --- data ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def contract(ctx, vent, pump):
    c = ct.create_contract(reference="SC-2026-118", vendor="Hamilton Medical", type=ContractType.OEM, coverage=Coverage.FULL,
                           start_on=TODAY - timedelta(days=100), end_on=TODAY + timedelta(days=265), annual_cost=5000)
    ct.add_asset(c, vent)
    ct.add_asset(c, pump)
    return c


@pytest.fixture
def fleet(ctx, dept, vent, pump, vent_model, pump_model):
    """vent: PM due in 10 days. pump: compliant. CE-10003: PM overdue. CE-10004: retired with a lapsed PM (never overdue).
    CE-10005: in repair with a lapsed PM (overdue, but its fleet state is In repair)."""
    ed = Department.objects.create(name="ED")
    over = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, acquisition_cost=3100, next_pm_on=TODAY - timedelta(days=5))
    retired = Asset.objects.create(tag="CE-10004", device_model=pump_model, department=dept, status=AssetStatus.RETIRED,
                                   next_pm_on=TODAY - timedelta(days=50))
    repair = Asset.objects.create(tag="CE-10005", device_model=vent_model, department=ed, status=AssetStatus.IN_REPAIR, acquisition_cost=41000,
                                  next_pm_on=TODAY - timedelta(days=5))
    return {"vent": vent, "pump": pump, "over": over, "retired": retired, "repair": repair}


@pytest.fixture
def wos(ctx, vent, pump, techs):
    """in_work: Dana, in progress, past due, with labor, parts, and a recall. vendor: open, vendor service. done: Dana, completed.
    nobody: open, unassigned."""
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-TEST-9", classification="Class II", manufacturer="Hamilton Medical",
                                 product="G5", title="Flow sensor recall", published_on=TODAY - timedelta(days=20))
    in_work = create_work_order(asset=vent, type="repair", priority="high", problem="Low tidal volume alarm", requester="ICU charge nurse",
                                opened_on=TODAY - timedelta(days=10), due_on=TODAY - timedelta(days=3), alert=alert)
    assign(in_work, technician=techs["dana"])
    change_status(in_work, "in_progress", as_of=TODAY - timedelta(days=9))
    LaborLine.objects.create(work_order=in_work, hours=Decimal("1.50"), rate=82)
    LaborLine.objects.create(work_order=in_work, hours=Decimal("0.25"), rate=Decimal("215.50"))
    PartLine.objects.create(work_order=in_work, description="Flow sensor", quantity=2, unit_cost=150)
    PartLine.objects.create(work_order=in_work, description="O-ring", quantity=1, unit_cost=Decimal("12.35"))
    vendor = create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch", opened_on=TODAY - timedelta(days=4))
    assign(vendor, vendor_name="BD field service")
    done = create_work_order(asset=pump, type="pm", priority="normal", problem="Annual PM", opened_on=TODAY - timedelta(days=6))
    assign(done, technician=techs["dana"])
    change_status(done, "in_progress", as_of=TODAY - timedelta(days=5))
    change_status(done, "completed", as_of=TODAY - timedelta(days=5))
    nobody = create_work_order(asset=pump, type="repair", priority="low", problem="Screen scratched", opened_on=TODAY - timedelta(days=2))
    return {"in_work": in_work, "vendor": vendor, "done": done, "nobody": nobody, "alert": alert}


@pytest.fixture
def theirs(tenant, other_tenant):
    """The other tenant's device, work order, and contract."""
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="Theirs Inc", model="THEIRS-MODEL", description="Theirs device", category="Ventilators")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, acquisition_cost=1000)
        c = ct.create_contract(reference="THEIRS-CT", vendor="Theirs vendor", start_on=TODAY, end_on=TODAY + timedelta(days=300), annual_cost=100)
        ct.add_asset(c, asset)
        create_work_order(asset=asset, type="repair", priority="normal", problem="THEIRS problem")
    return c


# --- permissions ---------------------------------------------------------------------------------------------------

def test_signed_out_is_sent_to_sign_in(client, contract):
    for url in ["/export/equipment.csv", "/export/work-orders.csv", "/export/contracts.csv", f"/export/contracts/{contract.pk}/devices.csv"]:
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login/"), url


def test_each_export_needs_its_modules_view(client, tenant, contract):
    exports = [("/export/equipment.csv", Module.EQUIPMENT), ("/export/work-orders.csv", Module.WORKORDERS), ("/export/contracts.csv", Module.CONTRACTS),
               (f"/export/contracts/{contract.pk}/devices.csv", Module.CONTRACTS)]
    for i, (url, module) in enumerate(exports):
        client.force_login(custom_user(tenant, {**{m: Level.FULL for m in Module.values}, module: Level.NONE}, f"without{i}@riverside.example"))
        assert client.get(url).status_code == 403, url
        client.force_login(custom_user(tenant, {module: Level.VIEW}, f"only{i}@riverside.example"))
        assert client.get(url).status_code == 200, url


def test_default_roles_without_contracts_get_403(client, signed_in, contract):
    for slug in ("requester", "vendor"):
        signed_in(slug)
        assert client.get("/export/contracts.csv").status_code == 403, slug
        assert client.get(f"/export/contracts/{contract.pk}/devices.csv").status_code == 403, slug


# --- the file ------------------------------------------------------------------------------------------------------

def test_files_have_the_bom_the_header_and_a_dated_name(client, signed_in, contract, wos):
    signed_in("analyst")
    files = [("/export/equipment.csv", "cadence-equipment", EQUIPMENT_COLUMNS), ("/export/work-orders.csv", "cadence-work-orders", WORKORDER_COLUMNS),
             ("/export/contracts.csv", "cadence-contracts", CONTRACT_COLUMNS),
             (f"/export/contracts/{contract.pk}/devices.csv", "cadence-SC-2026-118-devices", DEVICE_COLUMNS)]
    for url, name, columns in files:
        r = client.get(url)
        assert r.status_code == 200 and r["Content-Type"] == "text/csv; charset=utf-8", url
        assert r["Content-Disposition"] == f'attachment; filename="{name}-{STAMP}.csv"', url
        raw = b"".join(r.streaming_content)
        assert raw.startswith("﻿".encode() + ",".join(columns).encode() + b"\r\n"), url


def test_contract_device_list_file_name_keeps_only_safe_characters(client, signed_in, ctx, vent):
    c = ct.create_contract(reference='SC 2026/118 "A"; é', vendor="Hamilton Medical", start_on=TODAY, end_on=TODAY + timedelta(days=30))
    odd = ct.create_contract(reference="/// ", vendor="X", start_on=TODAY, end_on=TODAY + timedelta(days=30))
    signed_in("analyst")
    assert client.get(f"/export/contracts/{c.pk}/devices.csv")["Content-Disposition"] == f'attachment; filename="cadence-SC2026118A-devices-{STAMP}.csv"'
    assert client.get(f"/export/contracts/{odd.pk}/devices.csv")["Content-Disposition"] == f'attachment; filename="cadence-contract-devices-{STAMP}.csv"'


def test_formula_looking_text_is_kept_as_text_and_numbers_are_not(client, signed_in, ctx, dept, techs):
    dm = DeviceModel.objects.create(manufacturer="@Acme", model='=HYPERLINK("http://example.com","click")', description="+Pump", category="Pumps")
    asset = Asset.objects.create(tag="CE-20001", serial="-12", device_model=dm, department=dept, acquisition_cost=Decimal("-250.00"))
    create_work_order(asset=asset, type="repair", priority="normal", problem='=HYPERLINK("http://example.com","x")', requester="-Unit 4")
    c = ct.create_contract(reference="SC-1", vendor="=cmd|' /C calc'!A0", start_on=TODAY, end_on=TODAY + timedelta(days=200), annual_cost=100)
    ct.add_asset(c, asset)
    signed_in("director")
    eq = records(download(client, "/export/equipment.csv")[1])[0]
    assert eq["Model"] == '\'=HYPERLINK("http://example.com","click")' and eq["Manufacturer"] == "'@Acme" and eq["Description"] == "'+Pump"
    assert eq["Serial"] == "'-12" and eq["Acquisition cost"] == "-250.00"  # text that starts with a minus is guarded; a negative number is not
    wo = records(download(client, "/export/work-orders.csv")[1])[0]
    assert wo["Problem"] == '\'=HYPERLINK("http://example.com","x")' and wo["Requester"] == "'-Unit 4" and wo["Model"].startswith("'=")
    ctr = records(download(client, "/export/contracts.csv")[1])[0]
    assert ctr["Vendor"] == "'=cmd|' /C calc'!A0" and ctr["Model"].startswith("'=")
    dev = records(download(client, f"/export/contracts/{c.pk}/devices.csv")[1])[0]
    assert dev["Model"].startswith("'=") and dev["Acquisition cost"] == "-250.00"


def test_cell_guards_text_not_numbers():
    assert cell("=1+1") == "'=1+1" and cell("-3") == "'-3" and cell("\tx") == "'\tx" and cell("a=b") == "a=b"
    assert cell(Decimal("-1.50")) == "-1.50" and cell(-3) == "-3" and cell(-2.5) == "-2.50"
    assert cell(None) == "" and cell(True) == "Yes" and cell(date(2026, 9, 30)) == "2026-09-30"


# --- Equipment -----------------------------------------------------------------------------------------------------

def test_equipment_row_carries_every_column(client, signed_in, contract, vent, wos):
    vent.serial, vent.room, vent.warranty_end, vent.last_pm_on = "G5-55012", "4-112", TODAY + timedelta(days=40), TODAY - timedelta(days=170)
    vent.save()
    create_work_order(asset=vent, type="safety", priority="normal", problem="Annual electrical safety")
    signed_in("analyst")
    rows = download(client, "/export/equipment.csv")[1]
    assert rows[0] == EQUIPMENT_COLUMNS
    assert records(rows)[0] == {
        "Tag": "CE-10001", "Serial": "G5-55012", "Manufacturer": "Hamilton Medical", "Model": "Hamilton-G5", "Description": "ICU ventilator",
        "Category": "Ventilators", "Risk class": "Life support", "Department": "ICU", "Room": "4-112", "Status": "In service", "Support": "OEM contract",
        "Contract": "SC-2026-118", "Contract end": contract.end_on.isoformat(), "Contract expired": "No",
        "Installed": (TODAY - timedelta(days=800)).isoformat(), "Acquisition cost": "38000.00",
        "Warranty end": (TODAY + timedelta(days=40)).isoformat(), "Last PM": (TODAY - timedelta(days=170)).isoformat(),
        "Next PM": (TODAY + timedelta(days=10)).isoformat(), "Fleet state": "PM due within 30 days", "Open work orders": "2",
    }
    pump = records(rows)[1]
    # Two open work orders (vendor, nobody) and one completed; no install date or warranty is an empty cell.
    assert pump["Tag"] == "CE-10002" and pump["Open work orders"] == "2" and pump["Installed"] == "" and pump["Warranty end"] == ""
    assert pump["Fleet state"] == "Compliant" and pump["Risk class"] == "High"
    assert pump["Contract"] == "SC-2026-118" and pump["Contract end"] == contract.end_on.isoformat() and pump["Contract expired"] == "No"


def test_equipment_says_when_a_devices_contract_has_ended(client, signed_in, contract, vent):
    """The screen marks an ended contract "expired"; without the end date and flag the file's Support column reads as covered."""
    contract.start_on, contract.end_on = TODAY - timedelta(days=400), TODAY - timedelta(days=1)
    contract.save()
    signed_in("analyst")
    row = records(download(client, "/export/equipment.csv?support=contract_expired")[1])
    ended = (TODAY - timedelta(days=1)).isoformat()
    assert [(r["Tag"], r["Support"], r["Contract"], r["Contract end"], r["Contract expired"]) for r in row] == [
        ("CE-10001", "OEM contract", "SC-2026-118", ended, "Yes"), ("CE-10002", "OEM contract", "SC-2026-118", ended, "Yes")]


def test_equipment_export_has_the_lists_rows_in_the_lists_order(client, signed_in, fleet):
    signed_in("technician")
    every = ["CE-10001", "CE-10002", "CE-10003", "CE-10004", "CE-10005"]
    expected = {
        "": every, "?bucket=pm_due": ["CE-10001"], "?bucket=pm_overdue": ["CE-10003"], "?overdue=1": ["CE-10003", "CE-10005"],
        "?bucket=retired": ["CE-10004"], "?bucket=in_repair": ["CE-10005"], "?status=active": ["CE-10001", "CE-10002", "CE-10003", "CE-10005"],
        "?risk=life_support": ["CE-10001", "CE-10005"], "?dept=ED": ["CE-10005"], "?q=alaris&support=in_house": ["CE-10002", "CE-10003", "CE-10004"],
        "?category=Ventilators&sort=cost&dir=desc": ["CE-10005", "CE-10001"], "?sort=cost": ["CE-10004", "CE-10003", "CE-10002", "CE-10001", "CE-10005"],
        # The list's other parameters (page, a drawer tab, another screen's month) change nothing; unknown values are dropped.
        "?overdue=1&page=3&tab=wo&y=2026&m=2&mode=board": ["CE-10003", "CE-10005"], "?bucket=bogus&risk=nope": every,
    }
    for query, tags in expected.items():
        assert column(download(client, f"/export/equipment.csv{query}")[1], "Tag") == tags, query
        assert [a.tag for a in client.get(f"/equipment/{query}").context["page"]] == tags, query
    states = dict(zip(*[column(download(client, "/export/equipment.csv")[1], k) for k in ("Tag", "Fleet state")]))
    assert states == {"CE-10001": "PM due within 30 days", "CE-10002": "Compliant", "CE-10003": "PM overdue", "CE-10004": "Retired", "CE-10005": "In repair"}


def test_equipment_export_has_every_row_not_one_page(client, signed_in, ctx, dept, pump_model):
    for i in range(30):
        Asset.objects.create(tag=f"CE-3{i:04d}", device_model=pump_model, department=dept)
    signed_in("analyst")
    assert len(client.get("/equipment/?page=2").context["page"].object_list) == 5
    tags = column(download(client, "/export/equipment.csv?page=2")[1], "Tag")
    assert len(tags) == 30 and tags == sorted(tags)


# --- Work orders ---------------------------------------------------------------------------------------------------

def test_work_order_row_carries_every_column(client, signed_in, wos):
    signed_in("analyst")
    rows = download(client, "/export/work-orders.csv?open=0")[1]
    assert rows[0] == WORKORDER_COLUMNS
    by_number = {r["Number"]: r for r in records(rows)}
    assert by_number[wos["in_work"].number] == {
        "Number": wos["in_work"].number, "Type": "Corrective repair", "Priority": "High", "Status": "In progress", "Device tag": "CE-10001",
        "Manufacturer": "Hamilton Medical", "Model": "Hamilton-G5", "Category": "Ventilators", "Department": "ICU", "Problem": "Low tidal volume alarm",
        "Requester": "ICU charge nurse", "Source": "Entered by staff", "Assigned to": "Dana Whitfield", "Opened": (TODAY - timedelta(days=10)).isoformat(),
        "Due": (TODAY - timedelta(days=3)).isoformat(), "Started": (TODAY - timedelta(days=9)).isoformat(), "Completed": "", "Past due": "Yes",
        "Estimated hours": "1.00", "Labor hours": "1.75", "Labor cost": "176.88", "Parts cost": "312.35", "Total cost": "489.23", "Recall": "FDA Z-TEST-9",
    }
    vendor, done, nobody = by_number[wos["vendor"].number], by_number[wos["done"].number], by_number[wos["nobody"].number]
    assert vendor["Assigned to"] == "Vendor: BD field service" and vendor["Past due"] == "No" and vendor["Total cost"] == "0.00" and vendor["Recall"] == ""
    assert done["Status"] == "Completed" and done["Completed"] == (TODAY - timedelta(days=5)).isoformat() and done["Past due"] == "No"
    assert done["Type"] == "Preventive maintenance" and done["Labor hours"] == "0.00"
    assert nobody["Assigned to"] == "" and nobody["Priority"] == "Low"


def test_work_order_export_has_the_lists_rows(client, signed_in, wos, techs):
    signed_in("technician")
    n = {k: w.number for k, w in wos.items() if k != "alert"}
    dana = techs["dana"].id
    expected = {
        "": [n["nobody"], n["vendor"], n["in_work"]],  # open only, newest first
        f"?assigned={dana}": [n["in_work"]],
        f"?assigned={dana}&open=0&open=1": [n["in_work"]],  # the form's hidden open=0 comes first; the last value wins
        f"?assigned={dana}&open=0": [n["done"], n["in_work"]],
        "?assigned=unassigned": [n["nobody"]],  # vendor service is not unassigned
        "?status=completed&open=0": [n["done"]],
        "?type=pm&open=0": [n["done"]],
        "?q=latch": [n["vendor"]],
        "?assigned=not-a-technician&page=4": [n["nobody"], n["vendor"], n["in_work"]],
    }
    for query, numbers in expected.items():
        assert column(download(client, f"/export/work-orders.csv{query}")[1], "Number") == numbers, query
        assert [w.number for w in client.get(f"/work-orders/{query}").context["page"]] == numbers, query


def test_board_address_exports_what_the_list_lists(client, signed_in, wos, techs):
    """The Export click sends the address as it is, mode=board included: the export is the List view of the same address."""
    signed_in("technician")
    dana = techs["dana"].id
    for query in ["?mode=board", f"?mode=board&assigned={dana}", f"?mode=board&assigned={dana}&open=0", "?mode=board&status=completed&open=0"]:
        list_query = query.replace("mode=board&", "").replace("?mode=board", "")
        numbers = [w.number for w in client.get(f"/work-orders/{list_query}").context["page"]]
        assert column(download(client, f"/export/work-orders.csv{query}")[1], "Number") == numbers, query
    # The board itself shows the completed work order in its done column; the export of its address is the open work.
    assert column(download(client, "/export/work-orders.csv?mode=board")[1], "Number") == [wos["nobody"].number, wos["vendor"].number, wos["in_work"].number]


def test_an_inactive_technicians_filter_is_dropped_like_the_list_drops_it(client, signed_in, wos, techs):
    tom = techs["tom"]
    tom.is_active = False
    tom.save()
    signed_in("analyst")
    assert len(column(download(client, f"/export/work-orders.csv?assigned={tom.id}")[1], "Number")) == 3


# --- Contracts -----------------------------------------------------------------------------------------------------

@pytest.fixture
def contracts(contract, ctx, dept, vent_model, pump_model):
    """SC-2026-118 (active: vent and pump, plus a retired pump that is not covered), OLD-9 (expired, one device), NEW-1 (no devices)."""
    retired = Asset.objects.create(tag="CE-10009", device_model=pump_model, department=dept, acquisition_cost=9999)
    ct.add_asset(contract, retired)
    retired.status = AssetStatus.RETIRED
    retired.save()
    mon_model = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                           risk_class=RiskClass.MEDIUM)
    mon = Asset.objects.create(tag="CE-10007", device_model=mon_model, department=Department.objects.create(name="Cardiology"), acquisition_cost=6000,
                               status=AssetStatus.ON_LOAN)
    old = ct.create_contract(reference="OLD-9", vendor="Steris", type=ContractType.THIRD_PARTY, coverage=Coverage.PARTS, annual_cost=1200,
                             start_on=TODAY - timedelta(days=400), end_on=TODAY - timedelta(days=3))
    ct.add_asset(old, mon)
    new = ct.create_contract(reference="NEW-1", vendor="Draeger", coverage=Coverage.PM_ONLY, annual_cost=800, start_on=TODAY,
                             end_on=TODAY + timedelta(days=500))
    return {"active": contract, "old": old, "new": new, "mon": mon}


def test_contract_rows_are_one_per_covered_device(client, signed_in, contracts, vent, pump):
    signed_in("technician")
    rows = download(client, "/export/contracts.csv")[1]
    assert rows[0] == CONTRACT_COLUMNS
    recs = records(rows)
    assert [(r["Reference"], r["Device tag"]) for r in recs] == [("OLD-9", "CE-10007"), ("SC-2026-118", "CE-10002"), ("SC-2026-118", "CE-10001"),
                                                                ("NEW-1", "")]
    active = contracts["active"]
    common = {"Reference": "SC-2026-118", "Vendor": "Hamilton Medical", "Type": "OEM", "Coverage": "Full service", "Status": "Active",
              "Start": active.start_on.isoformat(), "End": active.end_on.isoformat(), "Annual cost": "5000.00"}
    assert recs[1] == {**common, "Device tag": "CE-10002", "Manufacturer": "BD", "Model": "Alaris 8015 PCU", "Department": "ICU", "Device status": "In service",
                       "Allocated annual cost": f"{active.cost_share_for(pump):.2f}"}
    assert recs[2]["Allocated annual cost"] == f"{active.cost_share_for(vent):.2f}" == "4611.65" and recs[1]["Allocated annual cost"] == "388.35"
    old = recs[0]
    assert old["Status"] == ct.contract_status(contracts["old"])["label"] and old["Status"].startswith("Expired")
    assert old["Type"] == "Third-party" and old["Coverage"] == "Parts only" and old["Device status"] == "On loan" and old["Allocated annual cost"] == "0.00"
    assert recs[3] == {"Reference": "NEW-1", "Vendor": "Draeger", "Type": "OEM", "Coverage": "Preventive maintenance only", "Status": "Active",
                       "Start": TODAY.isoformat(), "End": (TODAY + timedelta(days=500)).isoformat(), "Annual cost": "800.00", "Device tag": "",
                       "Manufacturer": "", "Model": "", "Department": "", "Device status": "", "Allocated annual cost": ""}


def test_contract_export_honours_the_lists_filters(client, signed_in, contracts):
    signed_in("analyst")
    expected = {"?status=expired": ["OLD-9"], "?status=active": ["SC-2026-118", "NEW-1"], "?type=third_party": ["OLD-9"], "?q=philips": ["OLD-9"],
                "?q=draeger&status=active&page=2": ["NEW-1"], "?status=ending": [], "?status=bogus&type=nope": ["OLD-9", "SC-2026-118", "NEW-1"]}
    for query, refs in expected.items():
        exported = list(dict.fromkeys(column(download(client, f"/export/contracts.csv{query}")[1], "Reference")))
        assert exported == refs, query
        assert [c.reference for c in client.get(f"/contracts/{query}").context["page"]] == refs, query


def test_contract_device_list(client, signed_in, contracts, vent, pump):
    signed_in("technician")
    active = contracts["active"]
    rows = download(client, f"/export/contracts/{active.pk}/devices.csv")[1]
    assert rows[0] == DEVICE_COLUMNS
    # Covered devices only (the retired pump is not), by model then tag as the drawer lists them.
    assert records(rows) == [
        {"Tag": "CE-10002", "Serial": "", "Manufacturer": "BD", "Model": "Alaris 8015 PCU", "Category": "Infusion pumps", "Department": "ICU", "Room": "",
         "Status": "In service", "Acquisition cost": "3200.00", "Allocated annual cost": "388.35", "Next PM": (TODAY + timedelta(days=90)).isoformat()},
        {"Tag": "CE-10001", "Serial": "", "Manufacturer": "Hamilton Medical", "Model": "Hamilton-G5", "Category": "Ventilators", "Department": "ICU",
         "Room": "", "Status": "In service", "Acquisition cost": "38000.00", "Allocated annual cost": "4611.65",
         "Next PM": (TODAY + timedelta(days=10)).isoformat()},
    ]
    old = records(download(client, f"/export/contracts/{contracts['old'].pk}/devices.csv")[1])
    assert [(r["Tag"], r["Allocated annual cost"]) for r in old] == [("CE-10007", "0.00")]  # an expired contract allocates nothing
    assert download(client, f"/export/contracts/{contracts['new'].pk}/devices.csv")[1] == [DEVICE_COLUMNS]


# --- tenants -------------------------------------------------------------------------------------------------------

def test_exports_never_include_another_tenants_rows(client, signed_in, contracts, wos, theirs):
    signed_in("director")
    for url in ["/export/equipment.csv", "/export/work-orders.csv?open=0", "/export/contracts.csv", f"/export/contracts/{contracts['active'].pk}/devices.csv"]:
        text = csv_text(client.get(url))
        assert "THEIRS" not in text and "Theirs" not in text, url
        assert "CE-10001" in text or "CE-10002" in text, url
    assert client.get(f"/export/contracts/{theirs.pk}/devices.csv").status_code == 404


class _RlsStandIn:
    """Stands in for the Postgres row-level security policy on SQLite (as tests/test_rls_paths.py does): notes any query on a
    tenant-scoped table made while set_db_tenant was last told None."""

    def __init__(self):
        self.tenant = None
        self.violations = []
        self._pattern = re.compile(r'"(' + "|".join(map(re.escape, tenant_scoped_tables())) + r')"')

    def set_db_tenant(self, tenant):
        self.tenant = tenant

    def guard(self, execute, sql, params, many, context):
        if self.tenant is None and self._pattern.search(sql):
            self.violations.append(sql[:200])
        return execute(sql, params, many, context)


@pytest.fixture
def rls(monkeypatch):
    stand_in = _RlsStandIn()
    monkeypatch.setattr("apps.tenants.context.set_db_tenant", stand_in.set_db_tenant)
    monkeypatch.setattr("apps.tenants.middleware.set_db_tenant", stand_in.set_db_tenant)
    return stand_in


def test_rows_are_read_inside_the_tenant_while_the_response_streams(client, signed_in, contracts, wos, rls):
    """The rows are read after TenantMiddleware has reset the tenant; under RLS they would come back empty unless the writer
    re-enters the request's tenant for the stream, and leaves it afterwards."""
    signed_in("director")
    urls = ["/export/equipment.csv", "/export/work-orders.csv", "/export/contracts.csv", f"/export/contracts/{contracts['active'].pk}/devices.csv"]
    with tenant_context(None), connection.execute_wrapper(rls.guard):
        for url in urls:
            r = client.get(url)
            assert rls.tenant is None, url  # the middleware has reset it before the body is read
            assert len(csv_rows(r)) > 1, url
            assert rls.tenant is None and get_current_tenant() is None, url
    assert rls.violations == []


def test_the_stand_in_sees_a_query_made_with_no_tenant(db, rls):
    with connection.execute_wrapper(rls.guard):
        list(Asset.unscoped.all())  # unscoped, no tenant set: what the policy would refuse
    assert rls.violations and "equipment_asset" in rls.violations[0]


def test_the_writer_streams_inside_the_tenant_it_was_made_in(tenant):
    seen = []

    def rows():
        seen.append(get_current_tenant())
        yield ["x"]

    with tenant_context(tenant):
        r = csv_response("x.csv", ["A"], rows())
    assert get_current_tenant() is None
    assert csv_text(r) == "A\r\nx\r\n"
    assert seen == [tenant] and get_current_tenant() is None
    assert csv_text(csv_response("y.csv", ["A"], [[1]])) == "A\r\n1\r\n"  # made with no tenant: written as is


# --- query counts --------------------------------------------------------------------------------------------------

def test_equipment_and_contract_queries_do_not_grow_with_rows(client, signed_in, contract, dept, pump_model, techs):
    signed_in("director")
    urls = ["/export/equipment.csv", "/export/contracts.csv", f"/export/contracts/{contract.pk}/devices.csv"]
    for url in urls:
        count_queries(client, url)  # warm up
    before = {url: count_queries(client, url) for url in urls}
    for i in range(14):
        a = Asset.objects.create(tag=f"CE-4{i:04d}", device_model=pump_model, department=dept, acquisition_cost=1000 + i)
        ct.add_asset(contract, a)
        create_work_order(asset=a, type="repair", priority="normal", problem="Check")
        c = ct.create_contract(reference=f"SC-X{i}", vendor="V", start_on=TODAY, end_on=TODAY + timedelta(days=90 + i), annual_cost=100)
        ct.add_asset(c, Asset.objects.create(tag=f"CE-5{i:04d}", device_model=pump_model, department=dept))
    after = {url: count_queries(client, url) for url in urls}
    assert after == before
    assert len(csv_rows(client.get("/export/equipment.csv"))) == 1 + 2 + 28 and len(csv_rows(client.get("/export/contracts.csv"))) == 1 + 16 + 14


def test_work_order_queries_do_not_grow_with_rows(client, signed_in, ctx, vent, techs):
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-Q-1", manufacturer="Hamilton Medical", product="G5", title="T")

    def add(i):
        wo = create_work_order(asset=vent, type="repair", priority="normal", problem=f"Problem {i}", alert=alert)
        assign(wo, technician=techs["dana"]) if i % 2 else assign(wo, vendor_name="Vendor")
        LaborLine.objects.create(work_order=wo, hours=1, rate=82)
        PartLine.objects.create(work_order=wo, description="Part", quantity=1, unit_cost=10)

    add(0)
    signed_in("director")
    count_queries(client, "/export/work-orders.csv")  # warm up
    one = count_queries(client, "/export/work-orders.csv")
    for i in range(1, 15):
        add(i)
    assert count_queries(client, "/export/work-orders.csv") == one
    assert len(csv_rows(client.get("/export/work-orders.csv"))) == 16


# --- the Export links ----------------------------------------------------------------------------------------------

def link(body, base):
    """The <a> that exports to `base`."""
    m = re.search(r'<a [^>]*data-base="' + re.escape(base) + r'"[^>]*>', body) or re.search(r'<a [^>]*href="' + re.escape(base) + r'[^"]*"[^>]*>', body)
    assert m, base
    return m.group(0)


def test_list_export_links_carry_the_lists_filters(client, signed_in, contract, wos):
    signed_in("technician")
    # The first href is the address the page was rendered at, without its page (and the board's mode); cadence.js swaps in the
    # address at click time, since HTMX filter changes update the address but not the link.
    eq = link(client.get("/equipment/?risk=high&page=2&q=pump").content.decode(), "/export/equipment.csv")
    assert 'href="/export/equipment.csv?risk=high&amp;q=pump"' in eq and 'data-base="/export/equipment.csv"' in eq
    wo = link(client.get("/work-orders/?mode=board&assigned=unassigned&page=2").content.decode(), "/export/work-orders.csv")
    assert 'href="/export/work-orders.csv?assigned=unassigned"' in wo and 'data-base="/export/work-orders.csv"' in wo
    ctr = link(client.get("/contracts/?status=expired&page=2").content.decode(), "/export/contracts.csv")
    assert 'href="/export/contracts.csv?status=expired"' in ctr and 'data-base="/export/contracts.csv"' in ctr
    for a in (eq, wo, ctr):
        assert 'data-act="with-filters"' in a and " download" in a
    plain = link(client.get("/equipment/").content.decode(), "/export/equipment.csv")
    assert 'href="/export/equipment.csv"' in plain


def test_contract_drawer_offers_the_device_list_to_view_only_roles(client, signed_in, contract):
    signed_in("technician")  # Contracts View, cannot edit
    body = client.get(f"/contracts/{contract.pk}/", **HX).content.decode()
    a = link(body, f"/export/contracts/{contract.pk}/devices.csv")
    assert " download" in a and "Device list" in body and "Renew 12 months" not in body
    assert client.get(f"/export/contracts/{contract.pk}/devices.csv").status_code == 200


def test_formula_text_after_a_semicolon_or_line_break_stays_text():
    """Excel set to split on ";" (many regions) cuts a field at the semicolon whatever the quoting, so every piece that would
    start with a formula character gets an apostrophe too. Plain text and numbers are untouched."""
    import csv

    from apps.web.exports import csv_line

    line = csv_line(["ICU", "Pump alarm;=WEBSERVICE(CHAR(104)&A2);", "Nurse;  @SUM(1)", "Line 1\n+cmd", "a;b", "5 to 10", -3, None])
    for delimiter in (",", ";"):  # how a comma Excel and a semicolon Excel split it
        cells = [c for row in csv.reader(line.splitlines(), delimiter=delimiter) for c in row]
        assert not any(c.startswith(("=", "+", "-", "@")) and c != "-3" for c in cells), (delimiter, cells)
    assert "Pump alarm;'=WEBSERVICE(CHAR(104)&A2);" in line and "a;b" in line and ",-3," in line
