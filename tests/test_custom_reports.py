"""Custom reports (slice 18): the engine (apps/reports/custom.py) and its services, permissions, and email.

Every source's columns and filters, relative periods against a pinned today, grouping and its sums and averages, money to the cent as
the work order drawer adds it, sorting, the row limits, every validation refusal, a definition naming anything outside the registry,
a saved definition that no longer fits, tenant isolation, who may build and run, and scheduling, emailing, and deleting one. The
screen, CSV, print page, and builder are tests/test_custom_reports_ui.py."""
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User, create_default_roles
from apps.contracts.models import Contract, ContractType
from apps.contracts.services import add_asset
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.reports import custom
from apps.reports import permissions as perms
from apps.reports import services as rs
from apps.reports import subscriptions as subs
from apps.reports.cost import report_spend
from apps.reports.models import CustomReport, ReportSubscription
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, PartLine, WorkOrder, WoStatus
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 10, 2)  # a Friday
MON = date(2026, 10, 5)
APP = "https://ce.example.org"
PLAIN = (str, int, float, date, bool, type(None))
SECRET = "Jane Roe, bed 4"  # what a requester typed: never in any report


def d(month, day, year=2026):
    return date(year, month, day)


def run(source, columns, filters=None, group_by="", sort="", today=TODAY, limit=custom.MAX_ROWS):
    return custom.run(custom.clean_definition(source, columns, filters or {}, group_by, sort), today, limit)


def table(result) -> list[dict]:
    """Rows as {label: value}."""
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def by(result, column, value) -> dict:
    return next(r for r in table(result) if r[column] == value)


def done(wo, started, completed):
    change_status(wo, WoStatus.IN_PROGRESS, as_of=started)
    change_status(wo, WoStatus.COMPLETED, as_of=completed)
    return wo


def labor(wo, tech, hours, rate, worked_on):
    return LaborLine.objects.create(work_order=wo, technician=tech, hours=Decimal(hours), rate=Decimal(rate), worked_on=worked_on)


def part(wo, description, quantity, unit_cost, part_number="", po_number=""):
    return PartLine.objects.create(work_order=wo, description=description, quantity=Decimal(quantity), unit_cost=Decimal(unit_cost),
                                   part_number=part_number, po_number=po_number)


@pytest.fixture
def world(ctx, dept, vent_model, pump_model, techs, other_tenant):
    """Riverside: a ventilator in the ICU on an OEM contract, a pump in Med-Surg 4, a retired pump; repairs (one by a technician
    with lines that round to the cent, one with a vendor), PMs on time, late, overdue, and not due yet, a cancelled repair, and one
    from last year. The other facility has a device, a work order, and lines of its own."""
    dana, tom = techs["dana"], techs["tom"]
    med = Department.objects.create(name="Med-Surg 4")
    vent = Asset.objects.create(tag="CE-10001", serial="HM-1", device_model=vent_model, department=dept, room="12", acquisition_cost=38000)
    pump = Asset.objects.create(tag="CE-10002", device_model=pump_model, department=med, acquisition_cost=Decimal("3200.50"))
    retired = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, status=AssetStatus.RETIRED, acquisition_cost=0)
    oem = Contract.objects.create(reference="SC-1", vendor="Hamilton Medical", type=ContractType.OEM, start_on=d(1, 1), end_on=d(6, 30, 2027),
                                  annual_cost=9999)
    add_asset(oem, vent)

    r1 = create_work_order(asset=vent, type="repair", priority="high", problem=SECRET, requester="RN Lee", opened_on=d(9, 20), assigned_to=dana)
    labor(r1, dana, "1.25", "82.33", d(9, 21))  # 102.9125 -> 102.91
    for _ in range(3):
        labor(r1, dana, "0.01", "0.50", d(9, 22))  # 0.005 -> 0.01 each line: 0.03, where adding first would give 0.02
        part(r1, "O-ring", "0.5", "0.01")  # the same for parts
    part(r1, "Flow sensor", "2", "45.50", part_number="FS-155", po_number="PO-77")
    done(r1, d(9, 21), d(9, 25))
    r2 = create_work_order(asset=pump, type="repair", priority="critical", problem=SECRET, opened_on=d(9, 28), vendor_service=True,
                           vendor_name="Acme Biomed")
    labor(r2, None, "2", "150", d(9, 29))  # vendor time names no technician
    part(r2, "Door latch", "1", "120")
    change_status(r2, WoStatus.IN_PROGRESS, as_of=d(9, 29))
    pm1 = done(create_work_order(asset=vent, type="pm", priority="normal", problem="PM", opened_on=d(9, 15), due_on=d(9, 30), assigned_to=tom),
               d(9, 29), d(9, 29))
    labor(pm1, tom, "1", "82", d(9, 29))
    pm2 = done(create_work_order(asset=pump, type="pm", priority="normal", problem="PM", opened_on=d(9, 1), due_on=d(9, 15), assigned_to=tom),
               d(9, 20), d(9, 20))
    pm3 = create_work_order(asset=pump, type="pm", priority="normal", problem="PM", opened_on=d(9, 10), due_on=d(9, 25))
    pm4 = create_work_order(asset=vent, type="pm", priority="low", problem="PM", opened_on=d(10, 1), due_on=d(10, 20), assigned_to=dana)
    cx = create_work_order(asset=vent, type="repair", priority="normal", problem=SECRET, opened_on=d(8, 1))
    change_status(cx, WoStatus.CANCELLED)
    old = done(create_work_order(asset=vent, type="repair", priority="normal", problem="Old", opened_on=d(9, 10, 2025), assigned_to=dana),
               d(9, 11, 2025), d(9, 12, 2025))
    labor(old, dana, "2", "80", d(9, 11, 2025))
    # Device dates as the tests read them (completing the PMs above moved them).
    Asset.objects.filter(pk=vent.pk).update(installed_on=d(8, 18, 2024), condition=4, warranty_end=d(1, 31, 2027), last_pm_on=d(9, 29),
                                            next_pm_on=d(3, 29, 2027))
    Asset.objects.filter(pk=pump.pk).update(installed_on=d(10, 2, 2020), condition=2, last_pm_on=d(9, 20), next_pm_on=d(10, 20))

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        theirs = Asset.objects.create(tag="OT-9001", device_model=DeviceModel.objects.create(manufacturer="Zoll", model="R Series",
                                      description="Defibrillator", category="Defibrillators"), department=Department.objects.create(name="ED"),
                                      acquisition_cost=1000)
        tw = create_work_order(asset=theirs, type="repair", priority="normal", problem="Theirs", opened_on=d(9, 25))
        LaborLine.objects.create(work_order=tw, technician=Technician.objects.create(name="Their Tech"), hours=1, rate=99, worked_on=d(9, 26))
        PartLine.objects.create(work_order=tw, description="Their part", quantity=1, unit_cost=55)
    return {"vent": vent, "pump": pump, "retired": retired, "med": med, "icu": dept, "dana": dana, "tom": tom, "r1": r1, "r2": r2, "pm1": pm1,
            "pm2": pm2, "pm3": pm3, "pm4": pm4, "cx": cx, "old": old, "theirs": theirs}


def all_keys(source) -> list[str]:
    return [c.key for c in custom.SOURCES[source].columns]


def chunks(keys, size=custom.MAX_COLUMNS):
    return [keys[i:i + size] for i in range(0, len(keys), size)]


# --- the registry ------------------------------------------------------------------------------------------------------------

FORBIDDEN = ("problem", "resolution", "requester", "callback", "reported_location", "notes", "checklist", "annual_cost", "created_by",
             "email", "password", "user", "description", "text")


def test_the_registry_offers_no_free_text_contract_prices_or_users():
    """Only what each source declares can be asked for, and none of it is text a requester typed, a contract's price, or a user. A
    part's description (what was used, typed by staff) is the one description offered; a labor line's notes are not."""
    for spec in custom.SOURCES.values():
        paths = [c.value for c in spec.columns if isinstance(c.value, str)] + [f.lookup for f in spec.filters]
        for path in paths:
            last = path.split("__")[-1]
            allowed = spec.key == custom.Source.PARTS and path == "description" or last == "description" and "device_model" in path
            assert allowed or not any(word == last or word in path.split("__") for word in FORBIDDEN), (spec.key, path)
        keys = [c.key for c in spec.columns]
        assert len(keys) == len(set(keys)) and set(spec.defaults) <= set(keys) and set(spec.dates) <= set(keys)
        assert all(spec.column(k).kind == custom.DATE for k in spec.dates)
        group_keys = [k for k, _ in custom.group_options(spec)]
        assert not set(group_keys) & {k for k in keys if not spec.column(k).group}  # a month key never shadows a column
    assert set(custom.SOURCES) == set(custom.Source.values) == set(perms.SOURCE_NEEDS)


@pytest.mark.parametrize("source", custom.Source.values)
def test_every_column_every_grouping_and_every_sort_runs(world, source):
    """Each source's columns in every combination the builder can ask for run on this database, with plain values: listed, sorted
    by each column both ways, and grouped by each grouping with every column that adds up."""
    spec = custom.SOURCES[source]
    keys = all_keys(source)
    for cols in chunks(keys):
        r = run(source, cols)
        assert r["columns"] == [spec.column(k).label for k in cols] and len(r["kinds"]) == len(cols) and r["total"] >= 1
        assert all(isinstance(v, PLAIN) for row in r["rows"] for v in row)
        for key in cols:
            for sort in (key, f"-{key}"):
                assert run(source, cols, sort=sort)["total"] == r["total"], sort
    adds_up = [k for k in keys if spec.column(k).agg]
    for group, _label in custom.group_options(spec):
        g = run(source, adds_up, group_by=group, sort="-count")
        assert g["grouped"] and g["columns"][:2] == [custom.group_label(spec, group), "Count"]
        assert sum(row[1] for row in g["rows"]) == g["records"] == run(source, ["%s" % keys[0]])["total"]
        assert all(isinstance(v, PLAIN) for row in g["rows"] for v in row)
    assert SECRET not in repr([run(source, cols)["rows"] for cols in chunks(keys)])


# --- each source's values ----------------------------------------------------------------------------------------------------

def test_work_order_columns_read_like_the_screens(world):
    r1, r2 = world["r1"], world["r2"]
    first, second = chunks(all_keys("work_orders"))
    row = by(run("work_orders", first), "Number", r1.number)
    assert row == {"Number": r1.number, "Type": "Corrective repair", "Status": "Completed", "Priority": "High", "Opened from": "Entered by staff",
                   "Device tag": "CE-10001", "Device": "ICU ventilator", "Manufacturer": "Hamilton Medical", "Model": "Hamilton-G5",
                   "Category": "Ventilators", "Department": "ICU", "Risk class": "Life support", "Opened": d(9, 20), "Due": d(9, 22),
                   "Started": d(9, 21), "Completed": d(9, 25), "Days open": 5, "Assigned to": "Dana Whitfield", "Vendor service": False,
                   "Labor hours": 1.28}
    rest = by(run("work_orders", ["number", *second]), "Number", r1.number)
    assert rest == {"Number": r1.number, "Labor cost": 102.94, "Parts cost": 91.03, "Total cost": 193.97, "PM on time": None, "PM result": None}
    vendor = by(run("work_orders", ["number", "status", "assigned_to", "vendor_service", "days_open", "labor_cost", "total_cost"]), "Number", r2.number)
    assert vendor == {"Number": r2.number, "Status": "In progress", "Assigned to": "Acme Biomed", "Vendor service": True, "Days open": 4,
                      "Labor cost": 300.0, "Total cost": 420.0}  # open: opened to today
    on_time = {row["Number"]: row["PM on time"] for row in table(run("work_orders", ["number", "pm_on_time", "days_open"]))}
    assert on_time[world["pm1"].number] is True and on_time[world["pm2"].number] is False  # done late
    assert on_time[world["pm3"].number] is False and on_time[world["pm4"].number] is None  # overdue today; not due yet
    assert on_time[r1.number] is None  # not a PM
    cancelled = by(run("work_orders", ["number", "days_open", "status"]), "Number", world["cx"].number)
    assert cancelled["Days open"] is None and cancelled["Status"] == "Cancelled"


def test_money_is_to_the_cent_as_the_drawer_adds_it(world):
    """Each line rounded half up before any total, as the work order drawer, its print, and the standard reports add them: the r1 lines
    added first and rounded once would make 102.93 and 91.02."""
    r1 = WorkOrder.objects.get(pk=world["r1"].pk)
    row = by(run("work_orders", ["number", "labor_cost", "parts_cost", "total_cost"]), "Number", r1.number)
    assert (row["Labor cost"], row["Parts cost"], row["Total cost"]) == (r1.labor_cost(), r1.parts_cost(), r1.total_cost()) == (102.94, 91.03, 193.97)
    lines = run("labor", ["wo_number", "amount"], filters={"technician": [str(world["dana"].pk)]}, sort="amount")
    assert [r["Amount"] for r in table(lines) if r["Work order"] == r1.number] == [0.01, 0.01, 0.01, 102.91]
    grouped = run("labor", ["amount", "hours"], filters={"wo_type": ["repair"]}, group_by="wo_number")
    assert by(grouped, "Work order", r1.number)["Amount"] == 102.94 and by(grouped, "Work order", r1.number)["Hours"] == 1.28
    parts = run("parts", ["amount"], group_by="wo_number")
    assert by(parts, "Work order", r1.number)["Amount"] == 91.03


def test_a_months_repair_cost_agrees_with_the_repair_spend_trend(world):
    spend = {row[0]: (row[1], row[2]) for row in report_spend(TODAY)["rows"]}
    r = run("work_orders", ["labor_cost", "parts_cost"], filters={"type": ["repair"]}, group_by="completed_month")
    sep = by(r, "Completed (month)", "Sep 2026")
    assert (sep["Labor cost"], sep["Parts cost"]) == spend["Sep 2026"] == (102.94, 91.03)


def test_device_columns_read_like_the_device(world):
    first, second = chunks(all_keys("devices"))
    assert by(run("devices", first), "Tag", "CE-10001") == {
        "Tag": "CE-10001", "Serial": "HM-1", "Device": "ICU ventilator", "Manufacturer": "Hamilton Medical", "Model": "Hamilton-G5",
        "Category": "Ventilators", "Department": "ICU", "Room": "12", "Status": "In service", "Risk class": "Life support",
        "Whose": "Ours",  # slice 29
        "Support": "OEM contract", "Contract": "SC-1", "Contract vendor": "Hamilton Medical", "Contract ends": d(6, 30, 2027),
        "Installed": d(8, 18, 2024), "Age (years)": pytest.approx((TODAY - d(8, 18, 2024)).days / 365.25), "Acquisition cost": 38000.0,
        "Condition (1 to 5)": 4, "Warranty ends": d(1, 31, 2027)}
    rows = {r["Tag"]: r for r in table(run("devices", ["tag", *second]))}
    # The vent: pm4 open; r1 is the one repair opened in 12 months (cx was cancelled, `old` is older); r1 and pm1 completed in them.
    assert rows["CE-10001"] == {"Tag": "CE-10001", "Last PM": d(9, 29), "Next PM": d(3, 29, 2027), "PM interval (months)": 6,
                                "On the manufacturer's schedule (CMS)": False, "Open work orders": 1, "Repairs, last 12 months": 1,
                                "Service cost, last 12 months": 275.97}
    assert rows["CE-10002"]["PM interval (months)"] == 18 and rows["CE-10002"]["Open work orders"] == 2  # r2 in progress, pm3 open
    assert rows["CE-10002"]["Service cost, last 12 months"] == 0.0  # r2 is not completed; pm2 had no lines
    assert rows["CE-10003"]["Next PM"] is None and rows["CE-10003"]["Open work orders"] == 0
    assert by(run("devices", ["tag", "support_type", "contract", "age", "warranty_end"]), "Tag", "CE-10002") == {
        "Tag": "CE-10002", "Support": "In-house", "Contract": None, "Age (years)": pytest.approx(6.0, abs=0.01), "Warranty ends": None}
    for asset in Asset.objects.select_related("device_model"):  # the interval in force, as the device's own property decides it
        assert by(run("devices", ["tag", "pm_interval"]), "Tag", asset.tag)["PM interval (months)"] == asset.pm_interval_months


def test_labor_and_part_columns(world):
    r1, r2 = world["r1"], world["r2"]
    rows = table(run("labor", all_keys("labor")))
    first = next(r for r in rows if r["Work order"] == r1.number and r["Hours"] == 1.25)
    assert first == {"Date worked": d(9, 21), "Technician or vendor": "Dana Whitfield", "Hours": 1.25, "Rate per hour": 82.33, "Amount": 102.91,
                     "Work order": r1.number, "Work order type": "Corrective repair", "Work order completed": d(9, 25), "Device tag": "CE-10001",
                     "Device": "ICU ventilator", "Manufacturer": "Hamilton Medical", "Model": "Hamilton-G5", "Category": "Ventilators",
                     "Department": "ICU"}
    vendor = next(r for r in rows if r["Work order"] == r2.number)
    assert vendor["Technician or vendor"] == "Acme Biomed" and vendor["Amount"] == 300.0 and vendor["Work order completed"] is None
    parts = table(run("parts", all_keys("parts")))
    sensor = next(r for r in parts if r["Part"] == "Flow sensor")
    line = PartLine.objects.get(description="Flow sensor")
    assert sensor == {"Recorded on": timezone.localdate(line.created_at), "Part": "Flow sensor", "Part number": "FS-155", "Quantity": 2.0,
                      "Unit cost": 45.5, "Amount": 91.0, "PO number": "PO-77", "Work order": r1.number, "Work order type": "Corrective repair",
                      "Work order completed": d(9, 25), "Device tag": "CE-10001", "Device": "ICU ventilator", "Manufacturer": "Hamilton Medical",
                      "Model": "Hamilton-G5", "Category": "Ventilators", "Department": "ICU"}
    assert len(parts) == 5 and {r["Part"] for r in parts} == {"O-ring", "Flow sensor", "Door latch"}  # none of the other facility's


# --- filters -----------------------------------------------------------------------------------------------------------------

def numbers(result) -> set:
    return {row[0] for row in result["rows"]}


def test_work_order_filters(world):
    w = world
    every = numbers(run("work_orders", ["number"]))
    assert every == {w[k].number for k in ("r1", "r2", "pm1", "pm2", "pm3", "pm4", "cx", "old")}
    f = lambda **filters: numbers(run("work_orders", ["number"], filters=filters))  # noqa: E731
    assert f(type=["pm"]) == {w["pm1"].number, w["pm2"].number, w["pm3"].number, w["pm4"].number}
    assert f(status=["open", "in_progress", "awaiting_parts"]) == {w["r2"].number, w["pm3"].number, w["pm4"].number}
    assert f(priority=["critical", "high"]) == {w["r1"].number, w["r2"].number}
    assert f(origin=["portal"]) == set() and f(origin=["manual"]) == every
    assert f(risk_class=["high"]) == {w["r2"].number, w["pm2"].number, w["pm3"].number}
    assert f(vendor_service=["yes"]) == {w["r2"].number} and f(vendor_service=["no"]) == every - {w["r2"].number}
    assert f(vendor_service=["yes", "no"]) == every
    assert f(technician=[str(w["tom"].pk)]) == {w["pm1"].number, w["pm2"].number}
    assert f(department=[str(w["med"].pk)]) == {w["r2"].number, w["pm2"].number, w["pm3"].number}
    assert f(category=["Ventilators"]) == {w[k].number for k in ("r1", "pm1", "pm4", "cx", "old")}
    assert f(type=["pm"], technician=[str(w["dana"].pk)]) == {w["pm4"].number}  # filters combine


def test_device_labor_and_part_filters(world):
    w = world
    tags = lambda **filters: numbers(run("devices", ["tag"], filters=filters))  # noqa: E731
    assert tags() == {"CE-10001", "CE-10002", "CE-10003"} and tags(status=["retired"]) == {"CE-10003"}
    assert tags(risk_class=["life_support"]) == {"CE-10001"} and tags(support_type=["oem_contract"]) == {"CE-10001"}
    assert tags(department=[str(w["icu"].pk)]) == {"CE-10001", "CE-10003"} and tags(category=["Infusion pumps"]) == {"CE-10002", "CE-10003"}
    DeviceModel.objects.filter(pk=w["vent"].device_model_id).update(oem_schedule_required=True)
    assert tags(oem_schedule=["yes"]) == {"CE-10001"} and tags(oem_schedule=["no"]) == {"CE-10002", "CE-10003"}
    assert by(run("devices", ["tag", "oem_schedule"]), "Tag", "CE-10001")["On the manufacturer's schedule (CMS)"] is True
    hours = lambda **filters: sorted(row[0] for row in run("labor", ["hours"], filters=filters)["rows"])  # noqa: E731
    assert hours(technician=["vendor"]) == [2.0]
    assert hours(technician=[str(w["tom"].pk), "vendor"]) == [1.0, 2.0]
    assert hours(wo_type=["pm"]) == [1.0] and hours(department=[str(w["med"].pk)]) == [2.0]
    amounts = lambda **filters: sorted(row[0] for row in run("parts", ["amount"], filters=filters)["rows"])  # noqa: E731
    assert amounts(category=["Infusion pumps"]) == [120.0] and amounts(wo_type=["pm"]) == []
    assert amounts(department=[str(w["icu"].pk)]) == [0.01, 0.01, 0.01, 91.0]


# --- dates --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("period, start, end", [
    ("last_7", d(9, 26), TODAY), ("last_30", d(9, 3), TODAY), ("last_90", d(7, 5), TODAY), ("last_365", d(10, 3, 2025), TODAY),
    ("next_30", TODAY, d(10, 31)), ("next_90", TODAY, d(12, 30)),
    ("this_month", d(10, 1), d(10, 31)), ("last_month", d(9, 1), d(9, 30)), ("this_year", d(1, 1), d(12, 31)),
    ("last_year", d(1, 1, 2025), d(12, 31, 2025)),
])
def test_relative_periods_resolve_against_the_runs_today(period, start, end):
    assert custom.period_range({"field": "completed", "period": period}, TODAY) == (start, end)


def test_last_month_in_january_is_december():
    assert custom.period_range({"period": "last_month"}, d(1, 15, 2027)) == (d(12, 1), d(12, 31))


def test_a_scheduled_period_rolls_forward_and_fixed_dates_do_not(world):
    w = world
    last_7 = {"date": {"field": "completed", "period": "last_7"}}
    assert numbers(run("work_orders", ["number"], filters=last_7)) == {w["pm1"].number}  # Sep 26 to Oct 2: pm1 on Sep 29
    assert numbers(run("work_orders", ["number"], filters=last_7, today=d(10, 9))) == set()  # a week on: nothing completed since
    fixed = {"date": {"field": "completed", "period": "custom", "from": "2026-09-20", "to": "2026-09-25"}}
    assert numbers(run("work_orders", ["number"], filters=fixed)) == {w["r1"].number, w["pm2"].number}
    assert numbers(run("work_orders", ["number"], filters=fixed, today=d(12, 1))) == {w["r1"].number, w["pm2"].number}
    open_ended = {"date": {"field": "opened", "period": "custom", "from": "2026-09-28"}}
    assert numbers(run("work_orders", ["number"], filters=open_ended)) == {w["r2"].number, w["pm4"].number}
    due_soon = {"date": {"field": "next_pm", "period": "next_30"}}
    assert numbers(run("devices", ["tag"], filters=due_soon)) == {"CE-10002"}
    worked = {"date": {"field": "date", "period": "last_year"}}
    assert [row[0] for row in run("labor", ["hours"], filters=worked)["rows"]] == [2.0]
    recorded_today = {"date": {"field": "recorded", "period": "custom", "from": timezone.localdate().isoformat(), "to": timezone.localdate().isoformat()}}
    assert run("parts", ["amount"], filters=recorded_today)["total"] == 5


# --- grouping ----------------------------------------------------------------------------------------------------------------

def test_grouping_counts_sums_averages_and_shares(world):
    r = run("work_orders", ["number", "status", "labor_hours", "total_cost", "days_open", "pm_on_time"], group_by="assigned_to")
    assert r["columns"] == ["Assigned to", "Count", "Average days open", "Labor hours", "Total cost", "PM on time %"]
    assert r["kinds"] == ["text", "number", "days", "hours", "money", "percent"] and r["left_out"] == ["Number", "Status"]
    rows = {row["Assigned to"]: row for row in table(r)}
    assert list(rows) == ["Acme Biomed", "Dana Whitfield", "Tom Okafor", "Unassigned"]  # by the group, A to Z
    assert rows["Dana Whitfield"] == {"Assigned to": "Dana Whitfield", "Count": 3, "Labor hours": 3.28, "Total cost": 353.97,
                                      "Average days open": pytest.approx((5 + 1 + 2) / 3), "PM on time %": None}  # r1, pm4 (open 1 day), old
    assert rows["Tom Okafor"]["PM on time %"] == 50.0 and rows["Tom Okafor"]["Average days open"] == pytest.approx((14 + 19) / 2)
    assert rows["Unassigned"]["Count"] == 2 and rows["Unassigned"]["Average days open"] == 22.0  # pm3 open 22 days; cx cancelled has none
    assert r["records"] == 8 and r["total"] == 4 and not r["truncated"]
    assert r["totals"] == [None, 8, pytest.approx((5 + 4 + 14 + 19 + 22 + 1 + 2) / 7), 6.28, 855.97, pytest.approx(100 / 3)]


def test_grouping_by_month_and_by_a_coded_value(world):
    r = run("work_orders", ["completed", "total_cost"], group_by="completed_month", sort="completed_month")
    assert [row[0] for row in r["rows"]] == ["Sep 2025", "Sep 2026", "Not completed"]  # in date order; empty last
    assert r["left_out"] == ["Completed"]
    assert by(r, "Completed (month)", "Sep 2026") == {"Completed (month)": "Sep 2026", "Count": 3, "Total cost": 275.97}
    p = run("work_orders", ["number"], group_by="priority")
    assert [row[0] for row in p["rows"]] == ["Critical", "High", "Normal", "Low"] and p["left_out"] == ["Number"]  # declared order
    assert [row[0] for row in run("work_orders", ["number"], group_by="priority", sort="-priority")["rows"]] == ["Low", "Normal", "High", "Critical"]
    v = run("work_orders", ["vendor_service"], group_by="vendor_service")
    assert [row[:2] for row in v["rows"]] == [["No", 7], ["Yes", 1]] and v["left_out"] == []  # the group column is shown, not left out


def test_device_and_line_groupings(world):
    r = run("devices", ["acquisition_cost", "age", "condition", "open_work_orders"], group_by="department")
    icu = by(r, "Department", "ICU")
    assert icu == {"Department": "ICU", "Count": 2, "Acquisition cost": 38000.0, "Average age (years)": pytest.approx((TODAY - d(8, 18, 2024)).days / 365.25),
                   "Average condition (1 to 5)": 3.5, "Open work orders": 1}  # the retired pump has no installed date: not in the age average
    assert by(r, "Department", "Med-Surg 4")["Acquisition cost"] == 3200.5
    who = run("labor", ["hours", "amount"], group_by="who", sort="-amount")
    assert [row[:4] for row in who["rows"]] == [["Acme Biomed", 1, 2.0, 300.0], ["Dana Whitfield", 5, 3.28, 262.94], ["Tom Okafor", 1, 1.0, 82.0]]
    assert who["totals"] == [None, 7, 6.28, 644.94]
    parts = run("parts", ["quantity", "amount"], group_by="description", sort="-count")
    assert parts["rows"][0] == ["O-ring", 3, 1.5, 0.03]


# --- sorting -----------------------------------------------------------------------------------------------------------------

def test_sorting_text_in_any_case_codes_in_order_and_empty_values_last(world):
    w = world
    Asset.objects.create(tag="ce-09999", device_model=w["vent"].device_model, department=w["icu"])
    assert [row[0] for row in run("devices", ["tag"], sort="tag")["rows"]] == ["ce-09999", "CE-10001", "CE-10002", "CE-10003"]
    assert [row[0] for row in run("devices", ["tag"], sort="-tag")["rows"]] == ["CE-10003", "CE-10002", "CE-10001", "ce-09999"]
    prio = [row[1] for row in run("work_orders", ["number", "priority"], sort="priority")["rows"]]
    assert prio == ["Critical", "High"] + ["Normal"] * 5 + ["Low"]
    completed = [row[1] for row in run("work_orders", ["number", "completed"], sort="completed")["rows"]]
    assert completed[:4] == [d(9, 12, 2025), d(9, 20), d(9, 25), d(9, 29)] and completed[4:] == [None] * 4
    assert [row[1] for row in run("work_orders", ["number", "completed"], sort="-completed")["rows"]][:1] == [d(9, 29)]  # empty still last
    costs = [row[1] for row in run("work_orders", ["number", "total_cost"], sort="-total_cost")["rows"]]
    assert costs == sorted(costs, reverse=True) and costs[0] == 420.0
    default = [row[0] for row in run("work_orders", ["number", "opened"])["rows"]]
    assert default[0] == w["pm4"].number and default[-1] == w["old"].number  # the source's own order: newest opened first


# --- limits ------------------------------------------------------------------------------------------------------------------

def test_a_run_takes_the_same_few_queries_however_many_rows(world):
    """Everything aggregates in the database: no query per row, listed or grouped."""
    def every_source() -> int:
        with CaptureQueriesContext(connection) as queries:
            for source, spec in custom.SOURCES.items():
                run(source, all_keys(source)[:custom.MAX_COLUMNS])
                run(source, [k for k in all_keys(source) if spec.column(k).agg], group_by=custom.group_options(spec)[0][0])
        return len(queries.captured_queries)

    before = every_source()
    for i in range(15):  # more devices, work orders, and lines
        asset = Asset.objects.create(tag=f"CE-2{i:04d}", device_model=world["vent"].device_model, department=world["med"], acquisition_cost=100)
        wo = create_work_order(asset=asset, type="repair", priority="normal", problem="More", opened_on=d(9, 1), assigned_to=world["tom"])
        labor(wo, world["tom"], "1", "50", d(9, 2))
        part(wo, "Fuse", "1", "2")
    assert every_source() == before <= 4 * 2 * 3  # per source: count, rows, totals listed; groups and totals grouped



def test_the_row_limits(world):
    assert (custom.MAX_ROWS, custom.SCREEN_ROWS, custom.PREVIEW_ROWS, custom.MAX_COLUMNS) == (10_000, 500, 50, 20)
    r = run("work_orders", ["number"], limit=3)
    assert len(r["rows"]) == 3 and r["total"] == 8 and r["truncated"] and r["limit"] == 3
    assert not run("work_orders", ["number"])["truncated"]
    g = run("work_orders", ["number"], group_by="type", limit=1)
    assert len(g["rows"]) == 1 and g["total"] == 2 and g["truncated"] and g["records"] == 8


# --- checking a definition ----------------------------------------------------------------------------------------------------

REFUSALS = [
    ({"source": "users"}, "source", "Choose what the report lists"),
    ({"source": None}, "source", "Choose what the report lists"),
    ({"columns": []}, "columns", "Choose at least one column."),
    ({"columns": "number"}, None, None),  # one column as a string is fine
    ({"columns": {"number": 1}}, "columns", "Choose the columns to show."),
    ({"columns": ["number", "number"]}, "columns", "Choose each column once."),
    ({"columns": all_keys("work_orders")[:21]}, "columns", "Choose at most 20 columns (this has 21)."),
    ({"columns": ["number", "problem"]}, "columns", "A work orders report has no column “problem”."),
    ({"filters": ["type"]}, "filters", "Choose filters from the list."),
    ({"filters": {"requester": ["RN Lee"]}}, "filters", "A work orders report has no filter “requester”."),
    ({"filters": {"type": ["repair", "nope"]}}, "filter_type", "“nope” is not one of the choices for type."),
    ({"filters": {"type": [1]}}, "filter_type", "Choose type from the list."),
    ({"filters": {"vendor_service": ["maybe"]}}, "filter_vendor_service", "“maybe” is not one of the choices for vendor service."),
    ({"filters": {"department": ["not-an-id"]}}, "filter_department", "Choose departments from this facility."),
    ({"filters": {"department": ["7d9f2c1e-0000-4000-8000-000000000000"]}}, "filter_department", "Choose departments from this facility."),
    ({"filters": {"technician": ["vendor"]}}, "filter_technician", "Choose technicians from this facility."),  # vendor time is labor's
    ({"filters": {"category": ["S" * 81]}}, "filter_category", "Choose categories this facility has."),
    ({"filters": {"type": ["repair"] * 201}}, None, None),  # duplicates collapse
    ({"filters": {"date": {"field": "completed", "period": "fortnight"}}}, "date", "Choose a period from the list."),
    ({"filters": {"date": {"field": "requested", "period": "last_7"}}}, "date", "Choose which date the period applies to."),
    ({"filters": {"date": {"field": "completed", "period": "custom"}}}, "date", "Enter a start date, an end date, or both."),
    ({"filters": {"date": {"field": "completed", "period": "custom", "from": "10/01/2026"}}}, "date", "Enter dates as year, month, and day"),
    ({"filters": {"date": {"field": "completed", "period": "custom", "from": "1066-10-14"}}}, "date", "Enter dates between 1950 and 2100."),
    ({"filters": {"date": {"field": "completed", "period": "custom", "from": "2026-10-02", "to": "2026-10-01"}}}, "date",
     "The start date must be on or before the end date."),
    ({"filters": {"date": "last_7"}}, "date", "Choose a date and a period."),
    ({"filters": {"date": {"field": "completed", "period": ""}}}, None, None),  # no period: no date filter
    ({"group_by": "requester"}, "group_by", "A work orders report cannot be grouped by “requester”."),
    ({"group_by": "number"}, "group_by", "cannot be grouped by “number”"),  # not a groupable column
    ({"group_by": "opened_month"}, None, None),
    ({"group_by": "started_month"}, "group_by", "cannot be grouped by “started_month”"),
    ({"sort": "problem"}, "sort", "A work orders report has no column “problem”."),
    ({"sort": "priority"}, "sort", "Sort by a column the report shows."),  # a column not chosen
    ({"sort": "-number"}, None, None),
    ({"group_by": "type", "sort": "number"}, "sort", "Sort by a column the report shows."),  # left out of the grouped table
    ({"group_by": "type", "sort": "-count"}, None, None),
    ({"group_by": "type", "columns": ["number", "total_cost"], "sort": "-total_cost"}, None, None),
    ({"sort": "count"}, "sort", "Sort by a column the report shows."),
    ({"sort": ["number"]}, "sort", "Choose a column to sort by."),
]


@pytest.mark.parametrize("change, part, message", REFUSALS)
def test_clean_definition_refuses_in_plain_words(world, change, part, message):
    definition = {"source": "work_orders", "columns": ["number", "type"], "filters": {}, "group_by": "", "sort": "", **change}
    if part is None:
        clean = custom.clean_definition(**definition)
        assert clean["source"] == "work_orders" and custom.run(clean, TODAY)["total"] >= 1
        return
    with pytest.raises(ValidationError) as e:
        custom.clean_definition(**definition)
    assert message in e.value.message_dict[part][0], e.value.message_dict


def test_another_facilitys_department_and_technician_are_refused(world, other_tenant):
    with tenant_context(other_tenant):
        ed = Department.objects.get(name="ED")
        theirs = Technician.objects.get(name="Their Tech")
    with pytest.raises(ValidationError, match="departments from this facility"):
        custom.clean_definition("work_orders", ["number"], {"department": [str(ed.pk)]})
    with pytest.raises(ValidationError, match="technicians from this facility"):
        custom.clean_definition("labor", ["hours"], {"technician": [str(theirs.pk)]})


def test_clean_definition_normalizes(world):
    clean = custom.clean_definition("work_orders", ["total_cost", "number"], {"date": {"field": "completed", "period": "custom", "from": "2026-09-01",
                                    "to": "", "extra": "x"}, "type": ["pm", "repair", "pm"], "status": []}, "", "-total_cost")
    assert clean == {"source": "work_orders", "columns": ["number", "total_cost"],  # the registry's order
                     "filters": {"type": ["pm", "repair"], "date": {"field": "completed", "period": "custom", "from": "2026-09-01"}},
                     "group_by": "", "sort": "-total_cost"}
    dept = custom.clean_definition("devices", ["tag"], {"department": [str(world["icu"].pk).upper()]})
    assert dept["filters"] == {"department": [str(world["icu"].pk)]}


# --- nothing outside the registry, and a stale definition ------------------------------------------------------------------------

def test_a_saved_definition_naming_fields_outside_the_registry_never_reads_them(world):
    """Rows written past the service (the admin, an old release, a hand edit) run with only what the registry declares."""
    report = CustomReport.objects.create(name="Hostile", source="work_orders", columns=["number", "problem", "requester__icontains", "asset__notes"],
                                         filters={"problem__icontains": ["Jane"], "type": ["repair", "nope"], "requester": "RN Lee",
                                                  "date": {"field": "problem", "period": "last_7"}},
                                         group_by="problem", sort="-requester")
    r = custom.run(custom.definition_of(report), TODAY)
    assert r["columns"] == ["Number"] and not r["grouped"]
    assert numbers(r) == {world["r1"].number, world["r2"].number, world["cx"].number, world["old"].number}  # repairs: the one filter left
    assert SECRET not in repr(r) and "RN Lee" not in repr(r)


def test_a_stale_definition_still_runs(world):
    gone = CustomReport.objects.create(name="Old columns", source="work_orders", columns=["dropped_column"], filters={"dropped": ["x"]},
                                       group_by="dropped_month", sort="-dropped")
    r = custom.run_key(gone.key, TODAY)
    assert r["columns"] == [custom.WORK_ORDERS.column(k).label for k in custom.WORK_ORDERS.defaults] and r["total"] == 8 and not r["problem"]
    no_source = CustomReport.objects.create(name="No source", source="contracts", columns=["reference"])
    r = custom.run_key(no_source.key, TODAY)
    assert r["rows"] == [] and r["columns"] == [] and "source is no longer offered" in r["problem"]
    assert rs.find_report(no_source.key)["subtitle"] == "Its source is no longer offered"
    assert custom.run_key("custom-00000000-0000-4000-8000-000000000000", TODAY)["problem"] == "This report has been deleted."


# --- in words ----------------------------------------------------------------------------------------------------------------

def test_a_definition_in_words(world):
    spec = custom.WORK_ORDERS
    definition = custom.clean_definition("work_orders", ["number", "total_cost"], {"type": ["repair", "pm"], "department": [str(world["icu"].pk)],
                                         "date": {"field": "completed", "period": "last_30"}}, "assigned_to", "-total_cost")
    assert custom.describe(spec, definition, TODAY) == ("Work orders · Type: Preventive maintenance, Corrective repair · Department: ICU · "
                                                        "Completed: last 30 days (Sep 3, 2026 to Oct 2, 2026) · Grouped by Assigned to · "
                                                        "Sorted by Total cost, highest first")
    assert custom.describe(spec, definition).endswith("Completed: last 30 days · Grouped by Assigned to · Sorted by Total cost, highest first")
    fixed = custom.clean_definition("labor", ["hours"], {"technician": ["vendor", str(world["dana"].pk)], "date": {"field": "date",
                                    "period": "custom", "to": "2026-09-30"}}, "", "hours")
    assert custom.describe(custom.LABOR, fixed) == ("Labor (time logged) · Technician: Dana Whitfield, Vendor time · Date worked: on or before "
                                                    "Sep 30, 2026 · Sorted by Hours, lowest first")


def test_file_stems_are_safe_recognisable_and_never_a_standard_reports():
    assert custom.file_stem("Pump repairs, Q3 / ICU") == "pump-repairs-q3-icu"
    assert custom.file_stem("Spend") == "custom-spend" and custom.file_stem("Ποσά") == "custom-report"
    assert custom.file_stem('"; rm -rf / <script>') == "rm-rf-script"


# --- tenant isolation ----------------------------------------------------------------------------------------------------------

def test_custom_reports_are_scoped_to_their_facility(world, other_tenant, make_user):
    ours = custom.create_custom_report(name="Ours", source="work_orders", columns=["number"])
    with tenant_context(other_tenant):
        theirs = custom.create_custom_report(name="Ours", source="devices", columns=["tag"])  # the same name in another facility is fine
        assert [m["key"] for m in custom.menu()] == [theirs.key] and rs.find_report(ours.key) is None
        assert numbers(custom.run_key(theirs.key, TODAY)) == {"OT-9001"}
        assert custom.run_key(ours.key, TODAY)["problem"] == "This report has been deleted."  # not found here
    assert [m["key"] for m in custom.menu()] == [ours.key] and rs.find_report(theirs.key) is None
    assert CustomReport.objects.count() == 1 and CustomReport.unscoped.count() == 2  # unscoped: the test proves both rows exist
    for source in custom.Source.values:  # no source shows the other facility's rows
        everything = repr([custom.run({"source": source, "columns": cols}, TODAY)["rows"] for cols in chunks(all_keys(source))])
        assert "OT-9001" not in everything and "Zoll" not in everything and "Their" not in everything and "ED" not in everything.split("'")
    with pytest.raises(ValidationError):
        with tenant_context(other_tenant):
            custom.update_custom_report(ours, name="Mine now")
    with tenant_context(other_tenant):
        with pytest.raises(ValidationError):
            custom.delete_custom_report(ours)
    assert CustomReport.objects.get(pk=ours.pk).name == "Ours"


# --- permissions ---------------------------------------------------------------------------------------------------------------

def custom_user(tenant, levels, slug="custom"):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    email=f"{slug}@riverside.example")


@pytest.mark.parametrize("role, builds", [("director", True), ("analyst", True), ("manager", False), ("technician", False),
                                          ("requester", False), ("vendor", False)])
def test_reports_edit_builds_by_default_the_director_and_finance(ctx, make_user, role, builds):
    assert perms.can_build(make_user(role)) is builds


def test_a_source_needs_view_on_what_it_lists(ctx, tenant):
    reports_only = custom_user(tenant, {Module.REPORTS: Level.EDIT})
    devices_too = custom_user(tenant, {Module.REPORTS: Level.EDIT, Module.EQUIPMENT: Level.VIEW}, slug="devices")
    for source in ("work_orders", "labor", "parts"):
        assert perms.needs(source) == ((Module.WORKORDERS, Level.VIEW),)
        assert "needs Work orders View" in perms.refusal(reports_only, source) and perms.refusal(devices_too, source)
        assert perms.build_refusal(devices_too, source).startswith("You need Work orders View to build a report that lists")
    assert perms.needs("devices") == ((Module.EQUIPMENT, Level.VIEW),) and perms.refusal(devices_too, "devices") == ""
    assert perms.refusal(reports_only, "devices") == "This report lists devices, so it needs Equipment View, which your role does not have."
    assert perms.needs("contracts") == () and perms.refusal(reports_only, "contracts") == ""
    assert perms.meta_refusal(reports_only, rs.find_report("cosr")) == ""  # the standard reports need Reports View only


def test_a_builder_may_only_build_on_what_they_can_see(world, tenant):
    devices_only = custom_user(tenant, {Module.REPORTS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    with pytest.raises(ValidationError) as e:
        custom.create_custom_report(name="WOs", source="work_orders", columns=["number"], by=devices_only)
    assert e.value.message_dict == {"source": ["You need Work orders View to build a report that lists work orders."]}
    report = custom.create_custom_report(name="Fleet", source="devices", columns=["tag"], by=devices_only)
    with pytest.raises(ValidationError, match="Work orders View"):
        custom.update_custom_report(report, source="labor", columns=["hours"], by=devices_only)


# --- the services ------------------------------------------------------------------------------------------------------------

def test_names_are_trimmed_bounded_and_unique_in_any_case(world, make_user):
    kim = make_user("analyst")
    report = custom.create_custom_report(name="  Pump   repairs ", source="work_orders", columns=["number"], by=kim)
    assert report.name == "Pump repairs" and report.created_by == kim and report.key == f"custom-{report.pk}"
    for name, message in [("", "Name the report."), ("   ", "Name the report."), ("x" * 81, "Keep the name to 80 characters."),
                          ("PUMP REPAIRS", "Another custom report here is already called PUMP REPAIRS."), ("Bad\x00name", "invisible control")]:
        with pytest.raises(ValidationError) as e:
            custom.create_custom_report(name=name, source="work_orders", columns=["number"])
        assert message in e.value.message_dict["name"][0], name
    assert custom.create_custom_report(name="x" * 80, source="devices", columns=["tag"]).name == "x" * 80
    with pytest.raises(ValidationError) as e:  # every problem at once
        custom.create_custom_report(name="", source="work_orders", columns=[], group_by="nope")
    assert set(e.value.message_dict) == {"name", "columns", "group_by"}


def test_changes_are_audited_and_only_what_differs_is_saved(world, make_user):
    kim = make_user("analyst")
    report = custom.create_custom_report(name="Repairs", source="work_orders", columns=["number"], filters={"type": ["repair"]}, by=kim)
    custom.update_custom_report(report, name="Repairs", columns=["number"], filters={"type": ["repair"]}, by=kim)  # nothing changes
    assert report.history.count() == 1
    custom.update_custom_report(report, columns=["number", "total_cost"], sort="-total_cost", by=kim)
    custom.update_custom_report(report, name="Repair costs", group_by="department", sort="-total_cost", by=kim)
    report.refresh_from_db()
    assert (report.name, report.columns, report.filters, report.group_by, report.sort) == (
        "Repair costs", ["number", "total_cost"], {"type": ["repair"]}, "department", "-total_cost")
    reasons = [(h.history_change_reason, h.history_user) for h in report.history.order_by("history_date")]
    assert reasons == [("Added", kim), ("Edited: columns, sort", kim), ("Edited: name, grouping", kim)]
    with pytest.raises(ValidationError, match="cannot be changed here: created_by"):
        custom.update_custom_report(report, created_by=None)
    with pytest.raises(ValidationError) as e:  # a new source re-checks the columns that came with the old one
        custom.update_custom_report(report, source="devices")
    assert "A devices report has no column “number”." in e.value.message_dict["columns"]
    pk = report.pk
    custom.delete_custom_report(report, by=kim)
    gone = CustomReport.history.filter(id=pk).order_by("-history_date").first()
    assert (gone.history_type, gone.history_change_reason, gone.history_user) == ("-", "Deleted", kim)


def test_deleting_a_report_removes_its_email_subscriptions(world, make_user):
    kim, nia = make_user("analyst"), make_user("director")
    for user in (kim, nia):
        user.email = user.username
        user.save()
    report = custom.create_custom_report(name="Repairs", source="work_orders", columns=["number"])
    other = custom.create_custom_report(name="Fleet", source="devices", columns=["tag"])
    subs.set_subscription(kim, report.key, "weekly")
    subs.set_subscription(nia, report.key, "monthly")
    subs.set_subscription(kim, other.key, "weekly")
    subs.set_subscription(kim, "cosr", "weekly")
    assert custom.delete_custom_report(report) == 2
    assert sorted(ReportSubscription.objects.values_list("report", flat=True)) == sorted([other.key, "cosr"])
    assert not CustomReport.objects.filter(pk=report.pk).exists() and rs.find_report(report.key) is None


# --- the resolver ---------------------------------------------------------------------------------------------------------------

def test_find_report_and_run_any_know_both_kinds_and_the_eight_stay_eight(world):
    report = custom.create_custom_report(name="Pump repairs", source="work_orders", columns=["number", "total_cost"], filters={"type": ["repair"]})
    meta = rs.find_report(report.key)
    assert meta == {"key": report.key, "title": "Pump repairs", "subtitle": "Work orders · Type: Corrective repair", "template": "web/_report_custom.html",
                    "custom": True, "source": "work_orders", "source_label": "Work orders", "needs": ((Module.WORKORDERS, Level.VIEW),),
                    "file_stem": "pump-repairs", "pk": report.pk}
    assert rs.find_report(report.key.upper().replace("CUSTOM", "custom"))["key"] == report.key  # the id in any case
    standard = rs.find_report("cosr")
    assert standard["custom"] is False and standard["needs"] == () and standard["file_stem"] == "cosr" and "custom" not in rs.REPORTS[0]
    assert rs.run_any(report.key, TODAY)["columns"] == ["Number", "Total cost"] and rs.run_any("cosr", TODAY)["columns"][0] == "Category"
    assert rs.csv_filename(meta, MON) == "cadence-pump-repairs-2026-10-05.csv" and rs.csv_filename(standard, MON) == "cadence-cosr-2026-10-05.csv"
    assert rs.report_meta(report.key) is None and report.key not in rs.REPORT_KEYS  # the API's catalog keeps the eight
    for key in ("custom-", "custom-nope", "custom-../../etc", "", None, "COSR"):
        assert rs.find_report(key) is None, key


# --- scheduling and emailing --------------------------------------------------------------------------------------------------

@pytest.fixture
def mail_ready(settings, monkeypatch):
    settings.APP_BASE_URL = APP
    monkeypatch.setattr(subs, "local_today", lambda: TODAY)


def test_a_custom_report_is_scheduled_and_emailed_like_the_others(client, world, make_user, mail_ready, mailoutbox, freeze_today):
    kim = make_user("analyst", username="kim@riverside.example")
    kim.email, kim.first_name = kim.username, "Kim"
    kim.save()
    report = custom.create_custom_report(name="Repairs <this month>", source="work_orders", columns=["number", "device", "total_cost"],
                                         filters={"type": ["repair"], "date": {"field": "opened", "period": "last_365"}}, sort="-total_cost")
    sub = subs.set_subscription(kim, report.key, "weekly")
    assert sub.report == report.key and subs.subscription_for(kim, report.key) == sub
    assert subs.send_report_email(sub, MON) is True
    mail = mailoutbox[0]
    assert mail.subject == "Repairs <this month> · Riverside Regional · Oct 5, 2026"
    assert "Repairs <this month>\nWork orders · Type: Corrective repair · Opened: last 365 days · Sorted by Total cost, highest first\n" in mail.body
    assert "attached as cadence-repairs-this-month-2026-10-05.csv (3 rows)" in mail.body
    assert f"{APP}/print/reports/{report.key}/" in mail.body and f"{APP}/reports/{report.key}/" in mail.body
    filename, content, mimetype = mail.attachments[0]
    assert (filename, mimetype) == ("cadence-repairs-this-month-2026-10-05.csv", "text/csv")
    assert SECRET not in mail.body and SECRET not in content
    client.force_login(kim)
    freeze_today(MON)
    download = client.get(f"/reports/{report.key}.csv")
    assert download["Content-Disposition"] == 'attachment; filename="cadence-repairs-this-month-2026-10-05.csv"'
    assert content.encode("utf-8") == b"".join(download.streaming_content)  # byte for byte
    assert content.splitlines()[:2] == ["﻿Number,Device,Total cost", f"{world['r2'].number},Infusion pump,420.00"]


def test_the_daily_send_includes_custom_reports_and_skips_who_cannot_see_them(world, tenant, make_user, mail_ready, mailoutbox):
    kim = make_user("analyst", username="kim@riverside.example")
    lee = custom_user(tenant, {Module.REPORTS: Level.VIEW, Module.WORKORDERS: Level.VIEW}, slug="lee")
    kim.email = kim.username
    kim.save()
    report = custom.create_custom_report(name="Open work", source="work_orders", columns=["number"], filters={"status": ["open"]})
    fleet = custom.create_custom_report(name="Fleet", source="devices", columns=["tag"])
    subs.set_subscription(kim, report.key, "weekly")
    subs.set_subscription(lee, report.key, "weekly")
    with pytest.raises(ValidationError, match="You need Equipment View to have this report emailed: it lists devices."):
        subs.set_subscription(lee, fleet.key, "weekly")
    # Lee loses Work orders View after subscribing: skipped, never sent
    lee.role.set_levels({Module.WORKORDERS: Level.NONE})
    leftover = ReportSubscription.objects.create(user=kim, report="custom-00000000-0000-4000-8000-000000000000", frequency="weekly")
    summary = subs.send_due(MON)
    river = next(t for t in summary["tenants"] if t["slug"] == "riverside")
    assert dict(river["skipped"]) == {"no_source_access": 1, "unknown_report": 1} and river["sent"] == 1
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]] and mailoutbox[0].subject.startswith("Open work · ")
    assert subs.SKIP_REASONS["no_source_access"] == "cannot see what the report lists"
    leftover.refresh_from_db()
    assert leftover.last_sent_on is None


def test_pm_on_time_counts_a_cancelled_pm_as_the_kpi_does(world):
    """A PM cancelled on a device still in use was missed: the PM completion KPI counts it, and so does the custom report's share.
    One cancelled on a device retired on or before its due date is left out of both (RETIRED_AND_CANCELLED, slice 25: the device's
    history dates its retirement)."""
    from datetime import datetime, time

    from apps.equipment.services import set_status
    from apps.pm.services import pm_on_time_rate

    missed = create_work_order(asset=world["vent"], type="pm", priority="normal", problem="PM", opened_on=d(9, 2), due_on=d(9, 10))
    change_status(missed, WoStatus.CANCELLED)
    later = Asset.objects.create(tag="CE-10004", device_model=world["vent"].device_model, department=world["icu"])
    Asset.history.filter(id=later.pk).update(history_date=timezone.make_aware(datetime.combine(d(9, 1), time(9))))  # added Sep 1
    gone = create_work_order(asset=later, type="pm", priority="normal", problem="PM", opened_on=d(9, 2), due_on=d(9, 12))
    change_status(gone, WoStatus.CANCELLED)
    set_status(later, AssetStatus.RETIRED, changed_on=d(9, 11), today=TODAY)  # retired the day before the PM was due
    on_time = {row["Number"]: row["PM on time"] for row in table(run("work_orders", ["number", "pm_on_time"]))}
    assert on_time[missed.number] is False and on_time[gone.number] is None
    kpi = pm_on_time_rate(d(9, 1), d(9, 30), TODAY)
    r = run("work_orders", ["pm_on_time"], filters={"type": ["pm"], "date": {"field": "due", "period": "last_month"}}, group_by="type")
    assert kpi == {"due": 4, "on_time": 1, "rate": 25.0}
    assert r["columns"] == ["Type", "Count", "PM on time %"] and r["rows"] == [["Preventive maintenance", 5, 25.0]]  # every PM due, one share


def test_blank_text_and_blank_codes_sort_last_either_way(world):
    """A blank serial or a PM result not recorded yet reads as empty, and empty values sort last in either direction."""
    assert [row[1] for row in run("devices", ["tag", "serial"], sort="serial")["rows"]] == ["HM-1", None, None]
    assert [row[1] for row in run("devices", ["tag", "serial"], sort="-serial")["rows"]] == ["HM-1", None, None]
    WorkOrder.objects.filter(pk=world["pm1"].pk).update(pm_result="pass")
    WorkOrder.objects.filter(pk=world["pm2"].pk).update(pm_result="fail")
    for sort in ("pm_result", "-pm_result"):
        results = [row[1] for row in run("work_orders", ["number", "pm_result"], sort=sort)["rows"]]
        assert all(results[:2]) and not any(results[2:]), (sort, results)
