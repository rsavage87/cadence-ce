"""
The devices import (slice 23, apps/imports/kinds/devices.py): the columns other systems' exports name, a first import that adds
devices with their models and departments (a check that changes nothing, then the import it promised), statuses through the
device rules (a device retired in the file retired on its day, for its history and the AEM evidence), a re-import that changes only
what the file has and is safe to run again, dates against the device rules left out with a note, unreadable values never made
defaults without one, the levels retiring and the CMS mark need, the facility's own devices only, and the same under PostgreSQL's
row-level security.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.core.history import record_history
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.imports import base, kinds, services
from apps.imports.models import ImportRun
from apps.pm import aem
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.web.views_exports import EQUIPMENT_COLUMNS
from apps.workorders.models import WoStatus
from apps.workorders.services import create_work_order

DEVICES = kinds.get("devices")


def csv_bytes(*lines):
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def run_through(run, user):
    run = services.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = services.process(run, user)
    return run


def check(user, *lines):
    """Upload `lines`, take the columns as matched, and run the check."""
    run = services.upload(user, "devices", "inventory.csv", csv_bytes(*lines))
    return run_through(services.confirm_columns(run, user, services.mapping_of(run)), user)


def imported(user, *lines):
    return run_through(services.start_import(check(user, *lines), user), user)


def notes(run) -> dict:
    return {g["note"]: g for g in services.problem_groups(run)}


@pytest.fixture
def director(make_user):
    return make_user("director")


@pytest.fixture
def today(ctx):
    return timezone.localdate()


# --- columns --------------------------------------------------------------------------------------------------------------------

def test_the_columns_answer_to_other_systems_names_and_never_to_ambiguous_ones():
    header = ["Control No.", "S/N", "Mfr", "Model #", "Nomenclature", "Product Category", "Risk Level", "Cost Center Name", "Sub Location",
              "Equipment Status", "Acceptance Date", "Purchase Price", "PM Frequency", "Last Inspection", "Next PM Due", "Warranty Expiration",
              "OEM Schedule", "Disposal Date"]
    assert base.auto_map(DEVICES.columns, header) == {c.key: i for i, c in enumerate(DEVICES.columns)}
    assert base.auto_map(DEVICES.columns, ["ID", "Vendor", "Location", "Owner", "Unit", "Name", "Type", "Model"]) == {"model": 7}
    cadence = base.auto_map(DEVICES.columns, EQUIPMENT_COLUMNS)  # a file exported from Cadence imports back
    assert set(cadence) == {c.key for c in DEVICES.columns} - {"pm_interval", "oem_schedule", "retired_on"}
    assert [c.label for c in DEVICES.required()] == ["Asset tag", "Manufacturer", "Model"]


# --- a first import -------------------------------------------------------------------------------------------------------------

def test_a_first_import_adds_devices_with_their_models_and_departments(ctx, director, today):
    next_pm = add_months(today, 2)
    lines = ("Control No.,Mfr,Model #,Nomenclature,Category,Risk,Dept,Room,Status,Install Date,Cost,PM Frequency,Next PM Date",
             'CE-1,Zoll,R Series,Defibrillator,Defibrillators,Life Support,Emergency,3,Active,2019-04-01,"$12,500.00",Semi-annual,',
             "CE-2,ZOLL,r  series,,,,emergency,4,Disposed,2018-01-01,,,",
             f"CE-3,BD,Alaris 8015 PCU,Infusion pump,Infusion pumps,High,ICU,,In Repair,,3200,Annual,{next_pm:%m/%d/%Y}",
             "ce-4,BD,Alaris 8015 PCU,,,,,,on loan,,,,")
    run = check(director, *lines)
    assert run.status == "checked" and run.counts == {"create": 4}
    assert not (Asset.objects.exists() or DeviceModel.objects.exists() or Department.objects.exists())  # the check rolled back
    summary = run.summary
    assert summary["created"] == {"Device models": ["Zoll R Series", "BD Alaris 8015 PCU"], "Departments": ["Emergency", "ICU", "Unassigned"]}
    assert summary["values"]["Status"] == {"Active": ["In service", 1], "Disposed": ["Retired", 1], "In Repair": ["Out of service", 1],
                                           "on loan": ["On loan", 1]}
    assert summary["values"]["Risk class"] == {"Life Support": ["Life support", 1], "High": ["High", 1]}
    assert summary["values"]["PM interval"] == {"Semi-annual": ["6 months", 1], "Annual": ["12 months", 1]}
    totals = summary["totals"]
    assert totals["Next PM"] == {"worked out": "2", "due today": "1"}  # CE-1: six months after 2019 has passed; CE-4: a year from today
    assert totals["Devices by status"] == {"In service": "1", "Retired": "1", "Out of service": "1", "On loan": "1"}
    assert Decimal(totals["Acquisition cost"]["in the file"]) == Decimal("15700")
    found = notes(run)
    assert found["No department: added to Unassigned"]["keys"] == ["ce-4"]
    assert found["In repair in the file: out of service here (in Cadence a work order puts a device in repair)"]["lines"] == [4]

    run = run_through(services.start_import(run, director), director)
    assert run.status == "imported" and run.counts == {"create": 4} and run.summary["created"] == summary["created"]
    zoll = DeviceModel.objects.get(manufacturer="Zoll")
    assert (zoll.model, zoll.description, zoll.category, zoll.risk_class, zoll.oem_pm_interval_months) == (
        "R Series", "Defibrillator", "Defibrillators", RiskClass.LIFE_SUPPORT, 6)
    assert DeviceModel.objects.count() == 2 and Department.objects.count() == 3  # never a case variant
    devices = {a.tag: a for a in Asset.objects.select_related("device_model", "department")}
    assert set(devices) == {"CE-1", "CE-2", "CE-3", "ce-4"}  # tags as the file writes them
    assert (devices["CE-1"].status, devices["CE-1"].acquisition_cost, devices["CE-1"].next_pm_on, devices["CE-1"].room) == (
        AssetStatus.IN_SERVICE, Decimal("12500.00"), today, "3")
    assert (devices["CE-2"].status, devices["CE-2"].device_model, devices["CE-2"].department.name, devices["CE-2"].next_pm_on) == (
        AssetStatus.RETIRED, zoll, "Emergency", None)
    assert devices["CE-3"].status == AssetStatus.OUT_OF_SERVICE and devices["CE-3"].next_pm_on == next_pm
    assert devices["ce-4"].status == AssetStatus.ON_LOAN and devices["ce-4"].department.name == "Unassigned"
    assert devices["ce-4"].acquisition_cost == 0 and devices["ce-4"].next_pm_on == add_months(today, 12)


def test_a_device_retired_in_the_file_is_retired_on_its_day(ctx, director, today):
    installed, retired = add_months(today, -72), today - timedelta(days=365)
    run = imported(director, "Asset Tag,Manufacturer,Model,Department,Installed,Status,Retired On",
                   f"R-1,Acme,Vent,ICU,{installed},Retired,{retired}", f"R-2,Acme,Pump,ICU,{installed},Retired,{today + timedelta(days=3)}",
                   f"R-3,Acme,Pump,ICU,{installed},Retired,{installed - timedelta(days=1)}")
    assert run.counts == {"create": 3}
    a = Asset.objects.get(tag="R-1")
    rows = list(Asset.history.filter(id=a.id).order_by("history_date", "history_id").values_list("status", "history_date"))
    assert [s for s, _ in rows] == ["in_service", "retired"] and {timezone.localdate(d) for _, d in rows} == {retired}
    entries, _ = record_history(a)
    assert [e.action for e in entries] == ["changed", "added"]  # the history reads in order
    ev = aem.evidence(a.device_model, today)  # in use from the window's start to the day it was retired, not to today
    assert ev["devices_retired"] == 1 and ev["device_years"] == round((retired - aem.history_start(today)).days / 365.25, 1)
    assert notes(run)["Retired on left out (after today, or before the install date): retired as of today"]["keys"] == ["R-2", "R-3"]
    for tag in ("R-2", "R-3"):
        last = Asset.history.filter(id=Asset.objects.get(tag=tag).id).order_by("-history_date").first()
        assert last.status == "retired" and timezone.localdate(last.history_date) == today


def test_a_retirement_without_its_day_says_it_is_retired_as_of_today(ctx, director, dept, vent_model, today):
    """Review fix: a device retired in the file without a day it can use was retired as of today with no note (the AEM evidence
    counts it in use until then), and one already here retired by the file silently left the file's day unused."""
    here = eq.create_asset(tag="R-9", device_model=vent_model, department=dept)
    run = imported(director, "Asset Tag,Manufacturer,Model,Department,Installed,Status,Retired On",
                   "R-1,Acme,Vent,ICU,2010-01-01,Retired,", "R-2,Acme,Vent,ICU,2010-01-01,Disposed,someday",
                   f"R-9,,,,,Retired,{today - timedelta(days=30)}", "R-3,Acme,Vent,ICU,2010-01-01,In service,",
                   f"R-4,Acme,Vent,ICU,2010-01-01,Retired,{today - timedelta(days=30)}")
    assert run.counts == {"create": 4, "update": 1}
    keys = {note: g["keys"] for note, g in notes(run).items() if "etired" in note}
    assert keys == {"No retired-on date: retired as of today": ["R-1"], "Retired on not read: retired as of today": ["R-2"],
                    "Retired on not used: a device already here is retired as of today": ["R-9"]}
    here.refresh_from_db()
    assert here.status == AssetStatus.RETIRED
    without = imported(director, "Asset Tag,Manufacturer,Model,Status", "R-5,Acme,Vent,Retired")  # no Retired On column at all
    assert notes(without)["No retired-on date: retired as of today"]["keys"] == ["R-5"]


def test_set_status_and_create_asset_refuse_a_day_out_of_range(ctx, dept, vent_model, today):
    with pytest.raises(ValidationError) as e:
        eq.create_asset(tag="X-1", device_model=vent_model, department=dept, added_on=today + timedelta(days=1))
    assert "added_on" in e.value.message_dict
    a = eq.create_asset(tag="X-2", device_model=vent_model, department=dept, installed_on=today - timedelta(days=30))
    for day in (today + timedelta(days=1), today - timedelta(days=31)):
        with pytest.raises(ValidationError) as e:
            eq.set_status(a, AssetStatus.RETIRED, changed_on=day)
        assert "changed_on" in e.value.message_dict
    eq.set_status(a, AssetStatus.MISSING, changed_on=today - timedelta(days=2))
    eq.set_status(a, AssetStatus.IN_SERVICE)  # the date went with its own save only
    dates = list(Asset.history.filter(id=a.id).order_by("history_id").values_list("history_date", flat=True))
    assert timezone.localdate(dates[1]) == today - timedelta(days=2) and timezone.localdate(dates[2]) == today


# --- devices already here -------------------------------------------------------------------------------------------------------

def test_a_re_import_changes_only_what_the_file_has_and_runs_again_harmlessly(ctx, director, dept, pump_model, vent_model, today):
    installed = add_months(today, -24)
    a = eq.create_asset(tag="ce-500", device_model=pump_model, department=dept, serial="S1", room="1", installed_on=installed,
                        acquisition_cost=Decimal("3200"), next_pm_on=add_months(today, 1))
    eq.create_asset(tag="CE-501", device_model=pump_model, department=dept, room="7")
    b = eq.create_asset(tag="CE-502", device_model=pump_model, department=dept)
    last_pm, next_pm = today - timedelta(days=20), add_months(today, 11)
    lines = ("Tag,Manufacturer,Model,Department,Room,Serial,Last PM,Next PM,Cost",
             f"CE-500,bd,ALARIS 8015 PCU,icu,2,,{last_pm},{next_pm},",  # blank serial and cost keep theirs
             "ce-501,,,,,,,,",
             "CE-502,Hamilton Medical,Hamilton-G5,ICU,,,,,",
             "CE-503,Acme,,ICU,,,,,")
    run = check(director, *lines)
    assert run.counts == {"update": 2, "unchanged": 1, "skip": 1}
    assert run.summary["totals"]["Changes"] == {"Room": "1", "Last PM": "1", "Next PM": "1", "Device model": "1"}
    assert notes(run)["A new device needs its manufacturer and model"]["keys"] == ["CE-503"]
    run = run_through(services.start_import(run, director), director)
    a.refresh_from_db()
    b.refresh_from_db()
    assert (a.tag, a.room, a.serial, a.last_pm_on, a.next_pm_on, a.acquisition_cost, a.device_model, a.department) == (
        "ce-500", "2", "S1", last_pm, next_pm, Decimal("3200.00"), pump_model, dept)
    assert b.device_model == vent_model and Asset.objects.get(tag="CE-501").room == "7"
    assert DeviceModel.objects.count() == 2 and Department.objects.count() == 1
    again = imported(director, *lines)
    assert again.counts == {"unchanged": 3, "skip": 1}  # a re-run changes nothing and doubles nothing
    partial = imported(director, "Tag,Manufacturer,Model", "CE-500,BD,", "CE-502,,Hamilton-G5")
    assert partial.counts == {"unchanged": 2}
    assert notes(partial)["Device model unchanged: it needs both the manufacturer and the model"]["count"] == 2


def test_the_last_pm_changes_only_through_the_service_with_its_rules(ctx, dept, pump_model, today):
    a = eq.create_asset(tag="LP-1", device_model=pump_model, department=dept, installed_on=today - timedelta(days=100))
    for bad in (today + timedelta(days=1), today - timedelta(days=101)):
        with pytest.raises(ValidationError) as e:
            eq.update_asset(a, imported_last_pm=bad)
        assert list(e.value.message_dict) == ["last_pm_on"]
    eq.update_asset(a, imported_last_pm=today - timedelta(days=5))
    a.refresh_from_db()
    assert a.last_pm_on == today - timedelta(days=5)
    with pytest.raises(ValidationError) as e:  # a new install date is measured against the new last PM
        eq.update_asset(a, installed_on=today - timedelta(days=3), imported_last_pm=today - timedelta(days=4))
    assert list(e.value.message_dict) == ["last_pm_on"]
    with pytest.raises(ValidationError, match="cannot be changed here: last_pm_on"):  # the screens' and the API's door stays shut
        eq.update_asset(a, last_pm_on=today)


def test_a_status_the_rules_refuse_is_noted_and_the_rows_other_changes_stay(ctx, director, dept, vent_model):
    busy = eq.create_asset(tag="ST-1", device_model=vent_model, department=dept)
    create_work_order(asset=busy, type="repair", priority="normal", problem="Alarm")
    pm_only = eq.create_asset(tag="ST-2", device_model=vent_model, department=dept)
    pm = create_work_order(asset=pm_only, type="pm", priority="normal", problem="PM")
    Asset.objects.create(tag="ST-3", device_model=vent_model, department=dept, status=AssetStatus.IN_REPAIR)  # as a work order leaves it
    missing = eq.create_asset(tag="ST-4", device_model=vent_model, department=dept)
    eq.set_status(missing, AssetStatus.MISSING)
    run = imported(director, "Tag,Manufacturer,Model,Room,Status", "ST-1,,,5,Retired", "ST-2,,,,Retired", "ST-3,,,,In repair",
                   "ST-4,,,,On loan", "ST-5,Hamilton Medical,Hamilton-G5,,Missing")
    assert run.counts == {"update": 2, "unchanged": 2, "create": 1}
    found = notes(run)
    assert found["Status kept: the device has open work orders; complete or cancel them, then retire it"]["keys"] == ["ST-1"]
    assert found["Status kept: Cadence does not move a device from missing to on loan"]["keys"] == ["ST-4"]
    assert not any("In repair" in note for note in found)  # in repair here already
    busy.refresh_from_db()
    pm.refresh_from_db()
    assert busy.status == AssetStatus.IN_SERVICE and busy.room == "5"
    assert Asset.objects.get(tag="ST-2").status == AssetStatus.RETIRED and pm.status == WoStatus.CANCELLED
    assert Asset.objects.get(tag="ST-3").status == AssetStatus.IN_REPAIR and Asset.objects.get(tag="ST-4").status == AssetStatus.MISSING
    added = Asset.objects.get(tag="ST-5")
    assert added.status == AssetStatus.MISSING and list(added.history.values_list("status", flat=True).order_by("history_id")) == [
        "in_service", "missing"]


# --- values it cannot take ------------------------------------------------------------------------------------------------------

def test_dates_against_the_device_rules_are_left_out_and_the_device_still_comes_in(ctx, director, today):
    future, far = today + timedelta(days=30), add_months(today, 12 * 11)
    run = imported(director, "Asset Tag,Manufacturer,Model,Department,Installed,Last PM,Next PM,Warranty End",
                   f"D-1,Acme,Pump,ICU,{future},,,",
                   "D-2,Acme,Pump,ICU,2020-01-01,2019-06-01,,",
                   "D-3,Acme,Pump,ICU,2020-01-01,,,2019-12-31",
                   f"D-4,Acme,Pump,ICU,,,{far},",
                   "D-5,Acme,Pump,ICU,1/1/1900,,12/31/2099,13/45/2020",
                   f"D-6,Acme,Pump,ICU,{future},{today - timedelta(days=10)},,")
    assert run.counts == {"create": 6}
    keys = {note: g["keys"] for note, g in notes(run).items()}
    assert keys["Install date left out: the install date cannot be in the future."] == ["D-1", "D-6"]
    assert keys["Last PM left out: the last PM cannot be before the device was installed."] == ["D-2"]
    assert keys["Warranty end left out: the warranty cannot end before the device was installed."] == ["D-3"]
    assert keys["Next PM left out: the next PM must be within 10 years."] == ["D-4"]
    assert keys["Install date is a placeholder for no date: left blank"] == ["D-5"]
    assert keys["Next PM is a placeholder for no date: worked out from the PM interval"] == ["D-5"]
    assert keys["Warranty end not read: left blank"] == ["D-5"]
    devices = {a.tag: a for a in Asset.objects.all()}
    assert devices["D-1"].installed_on is None and devices["D-2"].last_pm_on is None and devices["D-3"].warranty_end is None
    assert devices["D-4"].next_pm_on == add_months(today, 12) and devices["D-5"].next_pm_on == add_months(today, 12)
    assert devices["D-6"].installed_on is None and devices["D-6"].last_pm_on == today - timedelta(days=10)  # the install date went first
    assert run.summary["totals"]["Next PM"] == {"worked out": "6", "due today": "2"}  # D-2, D-3: a year since 2020 has passed


def test_a_re_import_leaves_out_a_future_install_date_before_measuring_the_last_pm_from_it(ctx, director, dept, pump_model, today):
    """Review fix: for a device already here, update_asset measured the file's last PM against the file's install date before
    refusing that install date (in the future), so the last PM was left out, blamed on the install date, and then the install
    date too; a new device kept its last PM. The install date goes first for both."""
    here = eq.create_asset(tag="B-1", device_model=pump_model, department=dept)
    future, last_pm = today + timedelta(days=30), today - timedelta(days=10)
    with pytest.raises(ValidationError) as e:
        eq.update_asset(here, installed_on=future, imported_last_pm=last_pm)
    assert list(e.value.message_dict) == ["installed_on"]
    run = imported(director, "Asset Tag,Manufacturer,Model,Department,Installed,Last PM", f"B-1,,,,{future},{last_pm}",
                   f"B-2,BD,Alaris 8015 PCU,ICU,{future},{last_pm}")
    assert run.counts == {"update": 1, "create": 1}
    assert {note: g["keys"] for note, g in notes(run).items()} == {"Install date left out: the install date cannot be in the future.": ["B-1", "B-2"]}
    assert {a.tag: (a.installed_on, a.last_pm_on) for a in Asset.objects.all()} == {"B-1": (None, last_pm), "B-2": (None, last_pm)}


def test_a_later_chunk_is_checked_as_the_import_finds_what_earlier_chunks_add(ctx, director, monkeypatch, today):
    """Review fix: the check rolls each chunk back, so a model or department a row of an earlier chunk adds was not there when a
    later chunk was checked, and the later chunk's first row naming it added it again from its own cells: an unreadable CMS mark
    skipped it, its PM interval worked out its next PM, and a department in another letter case was listed as added again. The
    import finds them, and never reads those cells. The check now adds them first, quietly, from the row that adds them in the
    import: not a row the import skips (a tag the device rules refuse, or a model it cannot add, takes back what its row added)."""
    monkeypatch.setattr(services, "CHUNK", 2)
    installed = add_months(today, -4)
    run = check(director, "Asset Tag,Manufacturer,Model,Department,OEM Schedule Required,PM Interval,Risk,Installed",
                f"BAD TAG,Acme,M1,Oncology,,3,,{installed}",
                f"C-1,Acme,M1,ICU,no,6,High,{installed}",  # adds M1, every 6 months: its next PM is two months out
                f"C-2,acme,m1,icu,maybe,3,,{installed}",   # the next chunk: M1 and ICU are found, these cells are not read
                f"C-5,Acme,M2,Surgery,maybe,,,{installed}",  # skipped for the mark of the model it would add, before its department
                f"C-3,ACME,M1,Oncology,,,,{installed}",    # Oncology: the refused row's went with it, so this row adds it
                f"C-4,BD,M3,surgery,,12,Low,{installed}")  # and this one adds Surgery, as it spells it
    checked = (dict(run.counts), run.results, run.summary)
    run = run_through(services.start_import(run, director), director)
    assert (dict(run.counts), run.results, run.summary) == checked
    assert run.counts == {"skip": 2, "create": 4} and [r[1] for r in run.results if r[2] == "skip"] == ["BAD TAG", "C-5"]
    assert run.summary["created"] == {"Device models": ["Acme M1", "BD M3"], "Departments": ["ICU", "Oncology", "surgery"]}
    assert run.summary["totals"]["Next PM"] == {"worked out": "4"}
    assert run.summary["values"]["PM interval"] == {"6": ["6 months", 1], "12": ["12 months", 1]}
    m1 = DeviceModel.objects.get(model="M1")
    assert (m1.oem_pm_interval_months, m1.risk_class, Department.objects.count()) == (6, RiskClass.HIGH, 3)


def test_values_it_cannot_read_are_never_defaults_without_a_note(ctx, director, dept, pump_model):
    eq.create_asset(tag="U-9", device_model=pump_model, department=dept, acquisition_cost=Decimal("3200"))
    run = imported(director, "Asset Tag,Manufacturer,Model,Department,Risk,PM Interval,Cost,OEM Schedule Required,Status,Installed",
                   "U-1,Acme,M1,ICU,3,0,abc,,Surplus-ish,13/45/2020",
                   "U-2,Acme,M2,ICU,high,6,,maybe,,",
                   "U-3,Acme,M1,ICU,,,,maybe,,",  # M1 is in the catalog by now: its mark is not read again
                   "U-4,Acme,M3,ICU,,,,,,",
                   "U-9,,,,,,abc,,Surplus-ish,")
    assert run.counts == {"create": 3, "skip": 1, "unchanged": 1}
    keys = {note: g["keys"] for note, g in notes(run).items()}
    assert keys["OEM schedule required not read: use yes or no (blank is no)"] == ["U-2"]
    assert keys["Risk class not read: set it on the model"] == ["U-1"]
    assert keys["No risk class: the new model is medium; set it on the model"] == ["U-4"]
    assert keys["PM interval not read: the new model is every 12 months; set it on the model"] == ["U-1"]
    assert keys["No PM interval: the new model is every 12 months; set it on the model"] == ["U-4"]
    assert keys["Acquisition cost not read: the model's list cost is used"] == ["U-1"]
    assert keys["Acquisition cost not read: unchanged"] == ["U-9"]
    assert keys["Status not read (in service, out of service, on loan, missing, or retired): added in service"] == ["U-1"]
    assert keys["Status not read (in service, out of service, on loan, missing, or retired): unchanged"] == ["U-9"]
    assert keys["Install date not read: left blank"] == ["U-1"]
    assert run.summary["values"]["Status"] == {"Surplus-ish": ["Not read", 2]}
    m1 = DeviceModel.objects.get(model="M1")
    assert (m1.risk_class, m1.oem_pm_interval_months, m1.oem_schedule_required) == (RiskClass.MEDIUM, 12, False)
    assert not DeviceModel.objects.filter(model="M2").exists() and Asset.objects.get(tag="U-9").acquisition_cost == Decimal("3200.00")


# --- levels ---------------------------------------------------------------------------------------------------------------------

def test_retiring_reinstating_and_the_cms_mark_need_equipment_approve(ctx, make_user, director, dept, vent_model):
    tech = make_user("technician")  # Equipment Edit: may import devices, not retire them or set the mark
    in_use = eq.create_asset(tag="L-1", device_model=vent_model, department=dept)
    retired = eq.create_asset(tag="L-2", device_model=vent_model, department=dept)
    eq.set_status(retired, AssetStatus.RETIRED)
    lines = ("Tag,Manufacturer,Model,Department,Room,Status,OEM Schedule Required", "L-1,,,,6,Retired,", "L-2,,,,,In service,",
             "L-3,GE HealthCare,Revolution CT,Radiology,,In service,yes", "L-4,Acme,Old,ICU,,Disposed,")
    run = imported(tech, *lines)
    assert run.counts == {"update": 1, "unchanged": 1, "create": 1, "skip": 1}
    keys = {note: g["keys"] for note, g in notes(run).items()}
    assert keys["Status kept: retiring or reinstating a device needs Equipment Approve"] == ["L-1", "L-2"]
    assert keys["OEM schedule required not set on the new model: needs Equipment Approve"] == ["L-3"]
    assert keys["Retired in the file: adding a retired device needs Equipment Approve"] == ["L-4"]
    in_use.refresh_from_db()
    assert in_use.status == AssetStatus.IN_SERVICE and in_use.room == "6"
    assert Asset.objects.get(tag="L-2").status == AssetStatus.RETIRED and not DeviceModel.objects.get(model="Revolution CT").oem_schedule_required
    Asset.objects.filter(tag="L-3").delete()  # test setup: the director imports the same file afresh
    DeviceModel.objects.filter(model="Revolution CT").delete()
    run = imported(director, *lines)
    assert run.counts == {"update": 2, "create": 2}
    assert Asset.objects.get(tag="L-1").status == AssetStatus.RETIRED and Asset.objects.get(tag="L-2").status == AssetStatus.IN_SERVICE
    assert DeviceModel.objects.get(model="Revolution CT").oem_schedule_required and Asset.objects.get(tag="L-4").status == AssetStatus.RETIRED


def test_an_import_finds_only_its_own_facilitys_devices(ctx, director, other_tenant):
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="Acme", model="M1", description="Pump", category="Pumps")
        Asset.objects.create(tag="CE-1", device_model=m, department=d, room="9")
    run = imported(director, "Tag,Manufacturer,Model,Department,Room", "ce-1,Acme,M1,ICU,4")
    assert run.counts == {"create": 1} and Asset.objects.get().room == "4"
    with tenant_context(other_tenant):
        assert Asset.objects.get().room == "9" and DeviceModel.objects.count() == Department.objects.count() == 1


# --- under row-level security (PostgreSQL) -------------------------------------------------------------------------------------

@needs_postgres
def test_a_check_and_an_import_under_the_policies(ctx, director, dept, pump_model, today):
    eq.create_asset(tag="PG-1", device_model=pump_model, department=dept)
    as_app_role()
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # a row pointing at one a skipped row rolled back would fail now, not at commit
    lines = ("Asset Tag,Manufacturer,Model,Department,Room,Status,Installed,Retired On",
             "BAD TAG,Acme,M9,Oncology,,,,",  # adds the model and the department, then is refused: both roll back with it
             "PG-2,Acme,M9,Oncology,1,,,",
             "pg-1,BD,Alaris 8015 PCU,icu,8,,,",
             f"PG-3,Acme,M9,Oncology,,Retired,2018-01-01,{today - timedelta(days=200)}")
    run = check(director, *lines)
    assert run.counts == {"skip": 1, "create": 2, "update": 1} and not Asset.objects.filter(tag="PG-2").exists()
    run = run_through(services.start_import(run, director), director)
    assert run.status == "imported" and run.counts == {"skip": 1, "create": 2, "update": 1}
    assert Asset.objects.get(tag="PG-2").department.name == "Oncology" and Asset.objects.get(tag="PG-1").room == "8"
    assert Department.objects.filter(name__iexact="oncology").count() == 1 and DeviceModel.objects.filter(model="M9").count() == 1
    retired = Asset.objects.get(tag="PG-3")
    assert timezone.localdate(retired.history.latest("history_date").history_date) == today - timedelta(days=200)


@needs_postgres
def test_values_longer_than_their_columns_skip_the_row_under_the_policies(ctx, director):
    as_app_role()
    run = imported(director, "Asset Tag,Manufacturer,Model,Department,Serial,Description",
                   f"L-1,Acme,M1,{'D' * 81},,", f"L-2,Acme,M1,ICU,{'S' * 81},", f"{'T' * 41},Acme,M1,ICU,,", f"L-3,Acme,M2,ICU,,{'x' * 201}",
                   "L-4,Acme,M1,ICU,ok,Pump")
    assert run.counts == {"skip": 4, "create": 1}
    assert {note for note, g in notes(run).items() if g["skipped"]} == {
        "Department is longer than 80 characters", "Serial number is longer than 80 characters", "Asset tag is longer than 40 characters",
        "Description is longer than 200 characters"}
    assert list(Asset.objects.values_list("tag", flat=True)) == ["L-4"]
