"""
Slice 29, wave 2B: rentals, vendor loaners, and demo units in the survey binder. The inventory lists the ones on site in a table of
their own with their figures, asks each for its owner's PM date (GAP past; none recorded a GAP for life support and high risk, a CHECK
for the rest; nothing asked of one waiting out of use for its incoming inspection), and never calls one a no-next-PM gap; its figures
by class and its device list stay ours. Maintenance never counts one past its PM date (missing or not). The incoming inspection section
reads a new one as any new device, and one entered already on site as counted, never a gap nor a "was it new?" check. The AEM section
counts our devices. The cover names what is still not covered.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from survey_helpers import gaps_of, period, rows_of

from apps.credentials.models import Technician
from apps.equipment import services as eq
from apps.equipment.models import (
    AddedAs,
    Asset,
    AssetStatus,
    DeviceModel,
    Ownership,
    ReturnCleaning,
    ReturnData,
    RiskClass,
    UseBeforeInspection,
)
from apps.pm.models import AemDecision, AemStatus
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, NOT_COVERED, aem, inspections, inventory, maintenance
from apps.tenants.context import tenant_context
from apps.workorders import inspections as incoming
from apps.workorders.completion import checklist_of, complete_work_order, procedure_for
from apps.workorders.models import InspectionResult, WoType
from apps.workorders.services import assign, change_status, create_work_order

S = AssetStatus


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def year(today):
    return period(today - timedelta(days=200), today)


@pytest.fixture
def bed_model(ctx):
    return DeviceModel.objects.create(manufacturer="Hillrom", model="Centrella", description="Smart bed", category="Beds", risk_class=RiskClass.LOW)


def temporary(model, dept, tag, *, today, kind=Ownership.RENTAL, existing=True, arrived_days=20, **kw):
    """A rental (by default) entered as already on site, or new and waiting for its incoming inspection (existing=False)."""
    values = {"tag": tag, "device_model": model, "department": dept, "kind": kind, "owner": "Acme Rentals", "serial": f"SN-{tag}",
              "owner_reference": "RA-2026-0042", "owner_pm_due_on": today + timedelta(days=90), "today": today}
    if existing:
        values.update(added_as=AddedAs.EXISTING, arrived_on=today - timedelta(days=arrived_days))
    return eq.add_temporary_device(**{**values, **kw})


def passed(asset, today):
    """Pass the device's open incoming inspection today, every step of its checklist (the rental checklist) passed."""
    wo = incoming.open_inspection(asset)
    assign(wo, technician=Technician.objects.get_or_create(name="Dana Whitfield", defaults={"title": "Lead BMET"})[0])
    results = [{"result": "pass", "reading": "42" if measure else ""} for _text, measure in checklist_of(procedure_for(wo))]
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=results, today=today)
    asset.refresh_from_db()
    return wo


def figures(section):
    return {f.label: f.value for f in section.figures}


def hints(section):
    return {f.label: f.hint for f in section.figures}


# --- the inventory ----------------------------------------------------------------------------------------------------------------

def test_the_inventory_lists_temporary_equipment_on_site_with_its_owners_pm_gaps(ctx, today, year, dept, vent, pump, vent_model, pump_model,
                                                                                 bed_model):
    temporary(pump_model, dept, "R-1", today=today)  # owner's PM date ahead: nothing to ask
    new = temporary(pump_model, dept, "R-2", today=today, existing=False, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    wo = passed(new, today)
    temporary(vent_model, dept, "R-3", today=today, existing=False, owner_pm_due_on=None)  # waiting out of use: its inspection asks
    temporary(vent_model, dept, "R-4", today=today, owner_pm_due_on=None)  # life support, none recorded: a gap
    temporary(bed_model, dept, "R-5", today=today, kind=Ownership.DEMO, owner="Hillrom", owner_pm_due_on=None)  # low risk: a check
    temporary(pump_model, dept, "R-6", today=today, owner_pm_due_on=today - timedelta(days=3))  # past: a gap
    gone = temporary(vent_model, dept, "R-7", today=today, owner_pm_due_on=None)
    eq.return_to_owner(gone, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.CLEARED, today=today)  # off the inventory
    s = inventory.build(year, None)

    f = figures(s)
    assert (f["Active devices"], f["Life-support devices"], f["High-risk devices"], f["Low-risk devices"]) == (2, 1, 1, 0)  # ours
    assert f["Temporary devices on site"] == 6 and f["Temporary devices past due back"] == 0
    assert hints(s)["Temporary devices on site"] == "4 rentals, 1 vendor loaner, 1 demo or evaluation unit; maintained by their owners"
    assert rows_of(s, "by_category") == [["Infusion pumps", 0, 1, 0, 0, 1], ["Ventilators", 1, 0, 0, 0, 1]]  # no "Beds": only a demo unit
    assert [r[0] for r in rows_of(s, "devices")] == ["CE-10001", "CE-10002"] and s.table("devices").count == 2
    table = s.table("temporary")
    assert table.count == 6 and table.links == {0: DEVICE} and table.columns == inventory.TEMPORARY_COLUMNS
    rows = {r[0]: r for r in rows_of(s, "temporary")}
    assert list(rows) == ["R-1", "R-2", "R-3", "R-4", "R-5", "R-6"]
    assert rows["R-1"] == ["R-1", "BD Alaris 8015 PCU", "High", "ICU", "In service", "Rental", "Acme Rentals", "RA-2026-0042",
                           today - timedelta(days=20), None, inventory.ALREADY_ON_SITE, today + timedelta(days=90)]
    assert rows["R-2"][4:7] == ["In service", "Vendor loaner", "BD"] and rows["R-2"][10] == wo.completed_on == today
    assert rows["R-3"][4] == "Awaiting inspection" and rows["R-3"][10] is None and rows["R-5"][5] == "Demo or evaluation unit"
    assert [(g.kind, g.record, g.url) for g in s.gaps] == [(GAP, "R-4", "/equipment/R-4/"), (CHECK, "R-5", "/equipment/R-5/"),
                                                           (GAP, "R-6", "/equipment/R-6/")]
    assert s.gaps[0].text == ("R-4 (Hamilton Medical Hamilton-G5, life support, rental from Acme Rentals) is on site with no owner's PM date "
                              "recorded: record the date on its owner's PM sticker.")
    assert s.gaps[2].text == (f"R-6 (BD Alaris 8015 PCU, high risk, rental from Acme Rentals) is on site with its owner's PM past due since "
                              f"{inventory._day(today - timedelta(days=3))}: record the date on the owner's new PM sticker, or return it to "
                              "its owner.")
    assert s.gaps[1].text.startswith("R-5 (Hillrom Centrella, low risk, demo or evaluation unit from Hillrom) is on site with no owner's PM")
    assert "RA-2026-0042" not in repr(s.gaps)


def test_a_missing_temporary_device_is_listed_missing_never_as_a_pm_gap(ctx, today, year, dept, vent, vent_model):
    demo = temporary(vent_model, dept, "D-1", today=today, kind=Ownership.DEMO, owner="Acme Demo")
    eq.set_status(demo, S.MISSING, today=today)
    s = inventory.build(year, None)
    assert figures(s)["Devices marked missing"] == 1 and figures(s)["Active devices"] == 1
    assert [r[0] for r in rows_of(s, "missing")] == ["D-1"] and rows_of(s, "temporary")[0][4] == "Missing"
    assert [(g.kind, g.record) for g in s.gaps] == [(FINDING, "D-1")]  # a lost life-support unit: whoever owns it


def test_a_temporary_device_in_use_before_its_inspection_is_asked_for_its_owners_pm_date(ctx, today, year, dept, vent_model, make_user):
    a = temporary(vent_model, dept, "R-1", today=today, existing=False, owner_pm_due_on=None)
    eq.use_before_inspection(a, UseBeforeInspection.LOANER_RENTAL, today=today)
    s = inventory.build(year, None)
    assert [(g.kind, g.record) for g in s.gaps] == [(GAP, "R-1")]  # in use: no longer waiting; never "no next PM"
    assert "no owner's PM date recorded" in s.gaps[0].text


def test_the_status_words_for_a_returned_device():
    assert inventory._status(S.RETIRED, False, temporary=True) == eq.RETURNED_LABEL
    assert inventory._status(S.RETIRED, False) == "Retired" and inventory._status(S.RETIRED, False, True, True) == eq.HELD_LABEL


def test_the_inventory_reads_a_fixed_number_of_queries_with_temporary_devices(ctx, today, year, dept, pump, pump_model, vent_model):
    def build(n):
        for i in range(n):
            temporary(pump_model, dept, f"R-{n}-{i}", today=today, owner_pm_due_on=None if i % 2 else today - timedelta(days=1))
            w = temporary(vent_model, dept, f"N-{n}-{i}", today=today, existing=False)
            passed(w, today)
        with CaptureQueriesContext(connection) as q:
            s = inventory.build(year, None)
            for t in s.tables:
                list(t.rows())
        assert s.table("temporary").count == 2 * Asset.objects.filter(tag__startswith="N-").count()
        return len(q)

    assert build(2) == build(6) <= 10


# --- maintenance --------------------------------------------------------------------------------------------------------------------

def test_maintenance_never_counts_a_temporary_device_past_its_pm(ctx, today, year, dept, vent, vent_model, pump_model):
    eq.update_asset(vent, next_pm_on=today + timedelta(days=30), today=today)

    def snapshot():
        s = maintenance.build(year, None)
        return figures(s), [(g.kind, g.record) for g in s.gaps], {t.key: rows_of(s, t.key) for t in s.tables}

    before = snapshot()
    lost = temporary(vent_model, dept, "D-1", today=today, kind=Ownership.DEMO, owner="Acme Demo")
    eq.set_status(lost, S.MISSING, today=today)  # a device of ours missing would be past its PM: a temporary one is not ours to PM
    temporary(vent_model, dept, "R-1", today=today, owner_pm_due_on=today - timedelta(days=40))
    fix = create_work_order(asset=Asset.objects.get(tag="R-1"), type=WoType.REPAIR, priority="normal", problem="Alarm")
    change_status(fix, "in_progress", as_of=today)
    assert snapshot() == before
    eq.set_status(vent, S.MISSING, today=today)  # ours missing: past, as before slice 29
    assert [(g.kind, g.record) for g in maintenance.build(year, None).gaps] == [(GAP, "CE-10001")]


# --- incoming inspection ------------------------------------------------------------------------------------------------------------

def test_a_temporary_device_is_read_as_any_new_device(ctx, today, year, dept, vent_model, pump_model, make_user):
    ok = temporary(pump_model, dept, "R-1", today=today, existing=False)
    passed(ok, today)
    early = temporary(vent_model, dept, "R-2", today=today, existing=False)
    eq.use_before_inspection(early, UseBeforeInspection.LOANER_RENTAL, by=make_user("director"), today=today)
    back = temporary(pump_model, dept, "R-3", today=today, existing=False)
    eq.return_to_owner(back, cleaning=ReturnCleaning.LABELED, data=ReturnData.NOT_APPLICABLE, today=today)  # never used: went back
    temporary(pump_model, dept, "R-4", today=today, arrived_days=2)  # already on site: counted, never "was it new?"
    kept = temporary(pump_model, dept, "R-5", today=today, arrived_days=2)
    eq.keep_temporary_device(kept, acquisition_cost=Decimal("0"), today=today)  # ours now, and still no "was it new?"
    eq.create_asset(tag="CE-1", device_model=pump_model, department=dept, added_as=AddedAs.EXISTING, installed_on=today - timedelta(days=3),
                    today=today)  # ours, installed three days before: the check stays
    s = inspections.build(year, None)
    f = figures(s)
    assert (f["New devices added"], f["Inspected before first use"], f["In use before its incoming inspection"], f["Returned to the vendor"],
            f["Entered as already in use"]) == (3, 1, 1, 1, 3)
    assert hints(s)["New devices added"] == "added as new to the facility; 3 of them rentals, vendor loaners, or demo units"
    assert [(g.kind, g.record) for g in s.gaps] == [(FINDING, "R-2"), (CHECK, "CE-1")]
    assert "Loaner or rental needed now" in s.gaps[0].text
    rows = {r[0]: r for r in rows_of(s, "new_devices")}
    assert rows["R-1"][7] == "Passed" and rows["R-3"][4] == "Awaiting inspection"


# --- AEM, the cover, isolation -------------------------------------------------------------------------------------------------------

def test_the_aem_section_counts_our_devices(ctx, today, year, dept, pump, pump_model, make_user):
    AemDecision.objects.create(device_model=pump_model, interval_months=18, oem_interval_months=12, status=AemStatus.APPROVED,
                               rationale="History", proposed_on=today - timedelta(days=60), decided_on=today - timedelta(days=50))
    rental = temporary(pump_model, dept, "R-1", today=today)
    for asset in (pump, rental):
        fix = create_work_order(asset=asset, type=WoType.REPAIR, priority="normal", problem="Alarm", opened_on=today - timedelta(days=10))
        change_status(fix, "in_progress", as_of=today - timedelta(days=10))
    s = aem.build(year, None)
    assert figures(s)["Devices on AEM today"] == 1
    [row] = rows_of(s, "in_force")
    assert row[3] == 1 and row[-2:] == [1, 0]  # active devices and repairs since approval: ours


def test_the_cover_names_what_is_still_not_covered():
    assert {"Rental, loaner, and vendor-owned equipment never entered in Cadence", "Patients' own equipment",
            "Loaner surgical instrument sets"} <= set(NOT_COVERED)


def test_isolation_another_facilitys_temporary_devices_stay_there(ctx, today, year, dept, pump_model, other_tenant):
    temporary(pump_model, dept, "R-1", today=today, owner_pm_due_on=None)
    with tenant_context(other_tenant):
        s = inventory.build(year, None)
        assert figures(s)["Temporary devices on site"] == 0 and rows_of(s, "temporary") == [] and gaps_of(s) == []
