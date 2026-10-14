"""
Slice 30, wave 2B: the survey binder's "Checks after repair before return to use" section (apps/reports/survey/repairs.py), its place
and access (Reports View, Work orders View, Equipment View), and the cover's reworded "not covered".

The section: the repairs completed in the period by outcome (figures), each one listed (number, device, model, class, completed on,
outcome, who checked, the vendor's report number), never what a requester typed; CHECKs only, never a gap or a finding: the repairs with
no outcome (one check for those completed before Cadence recorded checks, one apart for the imported), a device still out of use after
its last repair ended not returned to use (none when retired, being repaired again, or held for an incident), and a device back in use
after one with no PM or incoming inspection since; a fixed number of queries; another facility's repairs never read; the demo's
outcomes; under row-level security as the runtime role.
"""
from datetime import timedelta
from io import StringIO

import pytest
from csvutil import csv_rows
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from incident_fixtures import held_incident, investigated
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import FINDING, POST_REPAIR_ALL_PASS
from survey_helpers import gaps_of, period, rows_of

from apps.accounts.models import Level, Module, Role, User
from apps.credentials.models import Technician
from apps.demo.management.commands import seed_demo
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, Ownership, RiskClass
from apps.reports import survey
from apps.reports.permissions import survey_refusal
from apps.reports.survey import CHECK, DEVICE, NOT_COVERED, WORK_ORDER, repairs
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import findings
from apps.workorders.completion import complete_work_order
from apps.workorders.models import PmResult, Priority, RepairCheck, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

DAY = timedelta(days=1)
SECRET = "Zq7Canary bed 4"  # what a requester typed: never in the binder
VENDOR = "Hamilton Medical field service"
CLASSES = dict(RiskClass.choices)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def done(asset, on, *, tech=None, vendor="", outcome=RepairCheck.PASSED, reference="", tag_out=False) -> WorkOrder:
    """A repair opened two days before `on`, started the day before, and completed on `on` with `outcome`, through the services."""
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem=SECRET, requester=SECRET, opened_on=on - 2 * DAY,
                           tag_out=tag_out)
    if tech is None and not vendor:
        tech = Technician.objects.get_or_create(name="Dana Whitfield", defaults={"title": "Lead BMET"})[0]
    assign(wo, technician=tech, vendor_name=vendor)
    change_status(wo, WoStatus.IN_PROGRESS, as_of=on - DAY)
    extra = {"results": POST_REPAIR_ALL_PASS} if outcome == RepairCheck.PASSED else {"checks_reference": reference} if reference else {}
    complete_work_order(wo, resolution=SECRET, repair_check=outcome, repair_finding=FINDING, today=on, **extra)
    wo.refresh_from_db()
    return wo


def blank(wo, *, imported=False) -> WorkOrder:
    """A completed repair as one completed before slice 30 (or imported from the previous system) reads: no outcome, no steps."""
    fields = {"repair_check": "", "checklist_results": [], **({"source": Source.IMPORTED} if imported else {})}
    WorkOrder.objects.filter(pk=wo.pk).update(**fields)
    wo.refresh_from_db()
    return wo


def build(today, start=None, end=None):
    return repairs.build(period(start or today - 200 * DAY, end or today, today), None)


def figures(section) -> dict:
    return {f.label: f.value for f in section.figures}


def checks(section) -> dict:
    """{record (a device's tag, or "" for a count): text} of the section's checks."""
    return {g.record: g.text for g in section.gaps}


# --- the section ------------------------------------------------------------------------------------------------------------------


def test_the_figures_the_table_and_the_checks_for_repairs_with_no_outcome(today, vent, pump, techs):
    T = today
    before_1 = blank(done(vent, T - 150 * DAY))
    before_2 = blank(done(vent, T - 140 * DAY))
    imported = blank(done(vent, T - 130 * DAY), imported=True)
    checked = done(vent, T - 100 * DAY, tech=techs["dana"])
    vendor = done(vent, T - 90 * DAY, vendor=VENDOR, outcome=RepairCheck.VENDOR_REPORT, reference="FSO-26-1182")
    gone = done(pump, T - 80 * DAY, tech=techs["tom"], outcome=RepairCheck.NOT_RETURNED)
    eq.set_status(pump, AssetStatus.RETIRED)  # what became of it: no check
    done(vent, T - 300 * DAY)  # before the period
    reopened = done(vent, T - 60 * DAY)
    change_status(reopened, WoStatus.IN_PROGRESS)  # reopened: not completed now
    create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem=SECRET)  # open

    s = build(T)
    assert (s.key, s.title) == ("repairs", "Checks after repair before return to use")
    assert s.covers == f"Repairs completed {_day(T - 200 * DAY)} to {_day(T)}; devices as of today, {_day(T)}"
    assert figures(s) == {"Repairs completed": 6, "Checked: ready for use": 1, "Checked by the vendor": 1, "Not returned to use": 1,
                          "Completed before Cadence recorded checks": 2, "Imported from the previous system": 1,
                          "Still out of use after not returned": 0, "Back in use with no check since": 0,
                          "What was found recorded": "6 of 6", "Found on a PM": 0}  # slice 33
    table = s.table("repairs")
    assert table.columns == repairs.COLUMNS and table.links == {0: WORK_ORDER, 1: DEVICE} and table.count == 6
    vent_row = ["CE-10001", "Hamilton Medical Hamilton-G5", CLASSES[RiskClass.LIFE_SUPPORT]]
    found = findings.label(FINDING)  # slice 33: what each repair found
    assert rows_of(s, "repairs") == [
        [before_1.number, *vent_row, T - 150 * DAY, "Not recorded: completed before Cadence recorded checks", "", "", found],
        [before_2.number, *vent_row, T - 140 * DAY, "Not recorded: completed before Cadence recorded checks", "", "", found],
        [imported.number, *vent_row, T - 130 * DAY, "Not recorded: imported", "", "", found],
        [checked.number, *vent_row, T - 100 * DAY, "Checked: ready for use", "Dana Whitfield", "", found],
        [vendor.number, *vent_row, T - 90 * DAY, "Checked by the vendor", VENDOR, "FSO-26-1182", found],
        [gone.number, "CE-10002", "BD Alaris 8015 PCU", CLASSES[RiskClass.HIGH], T - 80 * DAY, "Not returned to use", "", "", found],
    ]
    # never a gap or a finding: one check for those completed before Cadence recorded checks, one apart for the imported
    assert [g.kind for g in s.gaps] == [CHECK, CHECK] and all(g.url == "" for g in s.gaps)
    assert s.gaps[0].text == (f"2 repairs completed {_day(T - 150 * DAY)} to {_day(T - 140 * DAY)}, before Cadence recorded checks after a "
                              "repair, record no outcome: have their checks ready from the paper or service records (the table lists them).")
    assert s.gaps[1].text == (f"1 repair imported from the previous system ({_day(T - 130 * DAY)}) records no checks after repair in Cadence: "
                              "have the previous system's records of those checks at hand (the table lists them).")
    assert build(T, start=T - 120 * DAY).gaps == [] and figures(build(T, start=T - 120 * DAY))["Repairs completed"] == 3


def test_an_empty_period(today, vent):
    s = build(today)
    assert figures(s)["Repairs completed"] == 0 and s.gaps == [] and rows_of(s, "repairs") == []
    assert s.table("repairs").empty == "No repair was completed in this period."


def test_a_device_still_out_after_its_repair_ended_not_returned(today, vent, pump, dept, techs):
    T = today
    wo = done(pump, T - 10 * DAY, tech=techs["tom"], outcome=RepairCheck.NOT_RETURNED)
    pump.refresh_from_db()
    assert pump.status == AssetStatus.OUT_OF_SERVICE
    s = build(T)
    assert checks(s) == {"CE-10002": f"CE-10002 has been out of use since {wo.number} ended not returned to use on {_day(T - 10 * DAY)}, "
                                     "with no repair open: retire it, or open a repair and complete it with its checks after repair."}
    gap = s.gaps[0]
    assert (gap.kind, gap.url) == (CHECK, "/equipment/CE-10002/") and figures(s)["Still out of use after not returned"] == 1
    # being repaired again: nothing to decide yet; the repair cancelled: the check is back
    again = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem=SECRET)
    assert build(T).gaps == []
    change_status(again, WoStatus.CANCELLED)
    assert set(checks(build(T))) == {"CE-10002"}
    # the repair after it checked: its last repair returned it
    done(pump, T - DAY, tech=techs["tom"], tag_out=True)
    assert build(T).gaps == []


def test_a_rental_still_out_goes_back_to_its_owner_and_a_held_device_waits_for_its_incident(today, dept, vent, techs):
    T = today
    rental_model = DeviceModel.objects.create(manufacturer="Hillrom", model="Centrella", description="Smart bed", category="Beds",
                                              risk_class=RiskClass.LOW)
    rental = eq.add_temporary_device(tag="R-0001", device_model=rental_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-R-0001", owner_reference="RA-2026-0042", owner_pm_due_on=T + 90 * DAY, today=T,
                                     added_as=AddedAs.EXISTING, arrived_on=T - 20 * DAY)
    wo = done(rental, T - 5 * DAY, vendor="Acme Rentals", outcome=RepairCheck.NOT_RETURNED)
    assert checks(build(T))["R-0001"].endswith(f"since {wo.number} ended not returned to use on {_day(T - 5 * DAY)}, with no repair open: "
                                               "return it to its owner, or open a repair and complete it with its checks after repair.")
    # an incident's investigation that ended not returned: the incident decides its return
    incident = held_incident(vent, occurred_on=T - 3 * DAY)
    investigated(incident.work_order, today=T - DAY, repair_check=RepairCheck.NOT_RETURNED, results=None, resolution="Bench test failed")
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert set(checks(build(T))) == {"R-0001"}


def test_a_device_back_in_use_with_no_check_since(today, vent, techs):
    T = today
    wo = done(vent, T - 20 * DAY, tech=techs["dana"], outcome=RepairCheck.NOT_RETURNED)
    eq.set_status(vent, AssetStatus.IN_SERVICE)  # by hand: no repair holds it once the repair ended
    s = build(T)
    text = (f"CE-10001 is in service, but its last repair, {wo.number}, ended not returned to use on {_day(T - 20 * DAY)}, and no PM or "
            "incoming inspection has been done since: have it checked (a repair completed with its checks after repair), or take it out of "
            "service.")
    assert checks(s) == {"CE-10001": text} and figures(s)["Back in use with no check since"] == 1
    eq.set_status(vent, AssetStatus.ON_LOAN)
    assert checks(build(T))["CE-10001"].startswith("CE-10001 is on loan, but its last repair")
    eq.set_status(vent, AssetStatus.IN_SERVICE)
    # a failed PM is no check (kept in use, its repair open), a PM that passed since is
    failed = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Semiannual PM", opened_on=T - 5 * DAY)
    assign(failed, technician=techs["dana"])
    complete_work_order(failed, pm_result=PmResult.FAIL, resolution="Alarm test failed", tag_out=False, today=T - 4 * DAY)
    assert set(checks(build(T))) == {"CE-10001"}
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Semiannual PM", opened_on=T - 3 * DAY)
    assign(pm, technician=techs["dana"])
    complete_work_order(pm, pm_result=PmResult.PASS, today=T - 2 * DAY)
    assert build(T).gaps == [] and figures(build(T))["Back in use with no check since"] == 0


def test_a_pm_on_the_day_the_repair_ended_counts_as_since(today, vent, techs):
    T = today
    done(vent, T - 6 * DAY, tech=techs["dana"], outcome=RepairCheck.NOT_RETURNED)
    eq.set_status(vent, AssetStatus.IN_SERVICE)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Semiannual PM", opened_on=T - 8 * DAY)
    assign(pm, technician=techs["dana"])
    complete_work_order(pm, pm_result=PmResult.PASS, today=T - 7 * DAY)  # the day before: not since
    assert set(checks(build(T))) == {"CE-10001"}
    other = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Semiannual PM", opened_on=T - 8 * DAY)
    assign(other, technician=techs["dana"])
    complete_work_order(other, pm_result=PmResult.PASS, today=T - 6 * DAY)
    assert build(T).gaps == []


def test_nothing_a_requester_typed_and_a_fixed_number_of_queries(today, vent, pump, techs):
    T = today

    def read() -> int:
        with CaptureQueriesContext(connection) as q:
            s = build(T)
            rows = rows_of(s, "repairs")
        text = " ".join([*(g.text for g in s.gaps), *(str(f.value) + f.hint for f in s.figures), *s.notes, *(str(c) for r in rows for c in r)])
        assert SECRET not in text
        return len(q)

    done(vent, T - 30 * DAY, tech=techs["dana"])
    small = read()
    for n in range(3):
        done(vent, T - (40 + n) * DAY, vendor=VENDOR, outcome=RepairCheck.VENDOR_REPORT, reference=f"FSO-{n}")
        blank(done(vent, T - (50 + n) * DAY), imported=bool(n % 2))
    done(pump, T - 10 * DAY, tech=techs["tom"], outcome=RepairCheck.NOT_RETURNED)
    eq.set_status(pump, AssetStatus.IN_SERVICE)
    assert read() == small <= 4  # slice 33: the findings, one grouped query


def test_another_facilitys_repairs_stay_there(today, ctx, vent, techs, other_tenant):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="OR")
        model = DeviceModel.objects.create(manufacturer="GE", model="Carescape B650", description="Monitor", category="Monitors",
                                           risk_class=RiskClass.HIGH)
        theirs = Asset.objects.create(tag="OT-1", device_model=model, department=dept)
        tech = Technician.objects.create(name="Pat Lee", title="BMET")
        done(theirs, today - 5 * DAY, tech=tech, outcome=RepairCheck.NOT_RETURNED)
        eq.set_status(theirs, AssetStatus.IN_SERVICE)
        assert figures(build(today))["Repairs completed"] == 1 and set(checks(build(today))) == {"OT-1"}
    s = build(today)
    assert figures(s)["Repairs completed"] == 0 and s.gaps == [] and rows_of(s, "repairs") == []


# --- its place, access, and the cover -------------------------------------------------------------------------------------------


def test_the_section_follows_incoming_inspections_and_needs_work_orders_and_equipment_view(ctx, make_user):
    keys = survey.section_keys()
    assert keys[keys.index("inspections") + 1] == "repairs" and survey.section_title("repairs") == "Checks after repair before return to use"
    for slug in ("director", "manager", "technician", "analyst"):
        assert survey_refusal(make_user(slug), "repairs") == "", slug
    for module, missing in ((Module.WORKORDERS, "Equipment"), (Module.EQUIPMENT, "Work orders")):
        role = Role.objects.create(name=f"Reports and {module}", slug=f"reports-{module}")
        role.set_levels({Module.REPORTS: Level.VIEW, module: Level.VIEW})
        user = User.objects.create_user(username=f"{module}@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
        assert survey_refusal(user, "repairs") == f"Needs {missing} View, which your role does not have.", module


def test_the_cover_names_what_is_still_not_covered():
    assert not any("Checks done after a repair" in item for item in NOT_COVERED)
    assert ("Checks after a recall action or software upgrade; devices returned to service by hand without a repair work order; calibration "
            "of electrical safety analyzers; a medical physicist's evaluation of imaging equipment after a major repair") in NOT_COVERED


def test_the_page_the_csv_and_the_api_serve_it(client, today, vent, techs, make_user):
    wo = done(vent, today - 30 * DAY, vendor=VENDOR, outcome=RepairCheck.VENDOR_REPORT, reference="FSO-26-77")
    client.force_login(make_user("director"))
    body = client.get("/reports/survey/").content.decode()
    assert 'id="sv-repairs"' in body and "Checks after repair before return to use" in body
    rows = csv_rows(client.get("/reports/survey/repairs/repairs.csv"))
    assert rows[0] == repairs.COLUMNS and rows[1][0] == wo.number
    assert rows[1][5:] == ["Checked by the vendor", VENDOR, "FSO-26-77", findings.label(FINDING)]
    data = client.get("/api/v1/survey/repairs/").json()
    assert data["key"] == "repairs" and {f["label"]: f["value"] for f in data["figures"]}["Checked by the vendor"] == 1


# --- the demo -------------------------------------------------------------------------------------------------------------------


def test_the_demo_shows_each_outcome(db):
    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        s = repairs.build(survey.default_period(today), None)
        found = figures(s)
        assert found["Checked by the vendor"] == 1 and found["Not returned to use"] == 1 and found["Checked: ready for use"] >= 1
        assert found["Completed before Cadence recorded checks"] == 0 and found["Imported from the previous system"] == 0
        assert s.gaps == []  # the bed found beyond repair was retired
        rows = rows_of(s, "repairs")
        assert [r[5:8] for r in rows if r[7]] == [["Checked by the vendor", r[6], seed_demo.VENDOR_REPORT_NUMBER] for r in rows if r[7]]
        assert [r[2] for r in rows if r[5] == "Not returned to use"] == [f"Hillrom {seed_demo.NOT_RETURNED_MODEL}"]


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_section_under_the_policies(client, today, vent, pump, techs, make_user):
    wo = done(pump, today - 10 * DAY, tech=techs["tom"], outcome=RepairCheck.NOT_RETURNED)
    done(vent, today - 20 * DAY, tech=techs["dana"])
    eq.set_status(pump, AssetStatus.IN_SERVICE)
    client.force_login(make_user("director"))
    as_app_role()
    s = build(today)
    assert figures(s)["Repairs completed"] == 2 and [g.record for g in gaps_of(s, CHECK)] == ["CE-10002"]
    assert wo.number in s.gaps[0].text and len(rows_of(s, "repairs")) == 2
    data = client.get("/api/v1/survey/repairs/").json()
    assert data["key"] == "repairs" and [g["record"] for g in data["gaps"]] == ["CE-10002"]
