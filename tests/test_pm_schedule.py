"""PM schedule services (slice 9): the month calendar, a day's devices and the suggested technicians, creating a day's PM work
orders, the 30-day outlook, next week's workload, the PM library, the nav badge, and tenant isolation."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm import schedule as sch
from apps.pm.models import PmProcedure
from apps.pm.services import create_pm_work_orders_for_day, generate_pm_work_orders
from apps.reports.services import nav_counts
from apps.tenants.context import tenant_context
from apps.workorders.models import Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, create_work_order

TODAY = date(2026, 9, 29)  # a Tuesday


def dev(tag, model, dept, day, status=AssetStatus.IN_SERVICE):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=day, status=status)


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Due on Sep 30: two ventilators (life support) and a pump (high). Sep 15: a pump, now overdue. A retired vent on Sep 30 never counts."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"),
                                                         checklist=["Inspect", "Safety test", "Function test"])
    vent_model.save()
    return {"v1": dev("CE-V1", vent_model, dept, date(2026, 9, 30)), "v2": dev("CE-V2", vent_model, dept, date(2026, 9, 30)),
            "p1": dev("CE-P1", pump_model, dept, date(2026, 9, 30)), "p_over": dev("CE-P2", pump_model, dept, date(2026, 9, 15)),
            "retired": dev("CE-VR", vent_model, dept, date(2026, 9, 30), status=AssetStatus.RETIRED)}


# --- calendar -----------------------------------------------------------------------------------------------

def test_calendar_covers_whole_weeks_sunday_first(ctx):
    cal = sch.month_calendar(2026, 9, TODAY)
    days = [c["date"] for w in cal["weeks"] for c in w]
    assert days[0] == date(2026, 8, 30) and days[-1] == date(2026, 10, 3) and len(days) == 35 and all(len(w) == 7 for w in cal["weeks"])
    assert days[0].weekday() == 6 and [c["in_month"] for c in cal["weeks"][0]] == [False, False, True, True, True, True, True]
    feb = sch.month_calendar(2026, 2, TODAY)  # Feb 2026 starts on a Sunday and has exactly four weeks
    assert len(feb["weeks"]) == 4 and feb["weeks"][0][0]["date"] == date(2026, 2, 1)


def test_calendar_counts_risk_dots_and_red_past_days(fleet):
    cal = sch.month_calendar(2026, 9, TODAY)
    cells = {c["date"]: c for w in cal["weeks"] for c in w}
    assert {k: cells[date(2026, 9, 30)][k] for k in ("n", "life_support", "high", "past", "is_today")} == \
        {"n": 3, "life_support": 2, "high": 1, "past": False, "is_today": False}
    assert cells[date(2026, 9, 15)]["n"] == 1 and cells[date(2026, 9, 15)]["past"] is True  # still due in the past: overdue, red
    assert cells[TODAY]["is_today"] and cells[TODAY]["past"] is False and cal["due_this_month"] == 4


# --- a day and the suggested technicians -------------------------------------------------------------------------

def test_day_devices_are_most_critical_first_and_flag_open_pms(fleet):
    create_work_order(asset=fleet["v2"], type=WoType.PM, priority="high", problem="PM")
    rows = list(sch.day_devices(date(2026, 9, 30)))
    assert [a.tag for a in rows] == ["CE-V1", "CE-V2", "CE-P1"] and [a.has_open_pm for a in rows] == [False, True, False]


def test_suggestions_balance_credentialed_technicians_by_hours(fleet, techs):
    """Dana is credentialed for the ventilator model and for pumps; Tom only for pumps. Tom already has 3 hours open."""
    wo = create_work_order(asset=fleet["p_over"], type=WoType.REPAIR, priority="normal", problem="Keypad", estimated_hours=Decimal("3"))
    assign(wo, technician=techs["tom"])
    plan = sch.day_plan(date(2026, 9, 30), TODAY)
    who = {r["asset"].tag: (r["technician"].name if r["technician"] else None) for r in plan["rows"]}
    # vents only Dana can do (1.5 h each); the pump then goes to the lighter plate: Tom at 3 h vs Dana at 3 h -> tie on hours, name wins
    assert who == {"CE-V1": "Dana Whitfield", "CE-V2": "Dana Whitfield", "CE-P1": "Dana Whitfield"}
    assert plan["count"] == 3 and plan["to_create"] == 3 and plan["hours"] == Decimal("4.0") and plan["overdue"] is False


def test_nobody_credentialed_means_no_suggestion(fleet):
    plan = sch.day_plan(date(2026, 9, 30), TODAY)
    assert all(r["technician"] is None for r in plan["rows"])


# --- creating a day's work orders ------------------------------------------------------------------------------------

def test_create_for_day_makes_one_pm_each_assigns_and_skips_open_ones(fleet, techs, make_user):
    kim = make_user("director")
    create_work_order(asset=fleet["v2"], type=WoType.PM, priority="high", problem="Already planned")
    batch = create_pm_work_orders_for_day(date(2026, 9, 30), by=kim, today=TODAY)
    assert batch == (2, 2, 1)
    made = WorkOrder.objects.filter(source=Source.PM_PLANNER).order_by("asset__tag")
    assert [w.asset.tag for w in made] == ["CE-P1", "CE-V1"]
    v1 = made.get(asset=fleet["v1"])
    assert (v1.type, v1.priority, v1.opened_on, v1.due_on, v1.estimated_hours, v1.created_by) == (WoType.PM, "high", TODAY, date(2026, 9, 30),
                                                                                                    Decimal("1.5"), kim)
    assert v1.problem == "Scheduled 6-month preventive maintenance, HA-G5-PM6" and v1.assigned_to == techs["dana"]
    assert v1.status_history.filter(note__startswith="Assigned to").exists()  # recorded like any assignment
    assert create_pm_work_orders_for_day(date(2026, 9, 30), today=TODAY) == (0, 0, 3)  # running it again creates nothing


def test_create_for_an_overdue_day_is_due_today_and_can_leave_work_unassigned(fleet, techs):
    batch = create_pm_work_orders_for_day(date(2026, 9, 15), assign_to_technicians=False, today=TODAY)
    wo = WorkOrder.objects.get(asset=fleet["p_over"], type=WoType.PM)
    assert batch == (1, 0, 0) and wo.due_on == TODAY and wo.assigned_to is None and wo.status == WoStatus.OPEN


def test_nightly_generation_still_works_through_the_shared_helper(fleet):
    assert generate_pm_work_orders(as_of=TODAY, lead_days=1) == 4  # Sep 15 overdue plus the three on Sep 30
    assert WorkOrder.objects.filter(type=WoType.PM, asset=fleet["v1"]).get().estimated_hours == Decimal("1.5")


# --- next 30 days, workload, library ----------------------------------------------------------------------------------

def test_next_30_days(fleet, dept, pump_model):
    dev("CE-FAR", pump_model, dept, TODAY + timedelta(days=31))  # outside the window
    n = sch.next_30_days(TODAY)
    assert (n["due"], n["life_support"], n["overdue"], n["hours"]) == (3, 2, 1, Decimal("4.0"))
    assert n["by_category"] == [("Ventilators", 2), ("Infusion pumps", 1)]  # most PMs first


def test_workload_counts_assigned_open_pms_suggestions_and_repairs(fleet, techs):
    open_pm = create_work_order(asset=fleet["v1"], type=WoType.PM, priority="high", problem="PM", estimated_hours=Decimal("2"))
    assign(open_pm, technician=techs["tom"])  # an override: Tom is not credentialed for the vent, but the work order is his
    repair = create_work_order(asset=fleet["p_over"], type=WoType.REPAIR, priority="normal", problem="Door", estimated_hours=Decimal("4"))
    assign(repair, technician=techs["dana"])
    loads = {w.technician.name: w for w in sch.workload_next_7_days(TODAY)}
    tom, dana = loads["Tom Okafor"], loads["Dana Whitfield"]
    assert (tom.pm_hours, tom.pm_count, tom.repair_hours) == (Decimal("3"), 2, Decimal("0"))  # v1 as assigned, then the pump (lighter plate)
    assert (dana.pm_hours, dana.pm_count, dana.repair_hours) == (Decimal("1.5"), 1, Decimal("4"))  # v2: only Dana is credentialed
    assert dana.total == Decimal("5.5") and dana.capacity == Decimal("32") and not dana.over and round(dana.pct, 1) == 17.2


def test_pm_library_orders_by_risk_and_marks_aem(fleet, pump_model):
    rows = sch.pm_library()
    assert [r["device_model"].model for r in rows] == ["Hamilton-G5", "Alaris 8015 PCU"]
    vent, pump = rows
    assert (vent["oem_months"], vent["program_months"], vent["aem"], vent["steps"], vent["hours"], vent["devices"]) == (6, 6, False, 3, Decimal("1.5"), 2)
    assert (pump["oem_months"], pump["program_months"], pump["aem"], pump["procedure"], pump["hours"]) == (12, 18, True, None, Decimal("1"))


def test_life_support_never_runs_on_aem_in_the_library(ctx, vent_model):
    vent_model.aem_interval_months = 12
    vent_model.save()
    row = sch.pm_library()[0]
    assert row["program_months"] == 6 and row["aem"] is False


def test_nav_badge_counts_overdue_devices(fleet):
    counts = nav_counts()
    assert counts["pm"] == 1 and counts["pm_hot"] is True


def test_nav_badge_is_absent_without_overdue_devices(ctx, vent):
    assert nav_counts()["pm"] is None and nav_counts()["pm_hot"] is False


# --- tenant isolation ------------------------------------------------------------------------------------------------

def test_other_tenants_devices_and_technicians_never_appear(fleet, techs, other_tenant):
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Hamilton-G5", description="Vent", category="Ventilators", risk_class=RiskClass.LIFE_SUPPORT)
        Asset.objects.create(tag="THEIRS", device_model=dm, department=Department.objects.create(name="ICU"), next_pm_on=date(2026, 9, 30))
        theirs = Technician.objects.create(name="Aaron Theirs")
        Credential.objects.create(technician=theirs, scope=Scope.CATEGORY, value="Ventilators")
    cells = {c["date"]: c for w in sch.month_calendar(2026, 9, TODAY)["weeks"] for c in w}
    assert cells[date(2026, 9, 30)]["n"] == 3
    plan = sch.day_plan(date(2026, 9, 30), TODAY)
    assert "THEIRS" not in [r["asset"].tag for r in plan["rows"]] and all(r["technician"] != theirs for r in plan["rows"])
    assert "Aaron Theirs" not in [w.technician.name for w in sch.workload_next_7_days(TODAY)]
    assert create_pm_work_orders_for_day(date(2026, 9, 30), today=TODAY).created == 3
    assert sch.next_30_days(TODAY)["due"] == 3 and len(sch.pm_library()) == 2
