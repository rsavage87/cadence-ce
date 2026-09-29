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


def test_nav_badge_counts_overdue_devices(ctx, dept, pump_model, other_tenant):
    """The badge reads the real clock, so this fleet is built around it: two overdue, one due today (not overdue), one ahead."""
    now = date.today()
    for tag, day in (("CE-O1", now - timedelta(days=3)), ("CE-O2", now - timedelta(days=40)), ("CE-T0", now), ("CE-F1", now + timedelta(days=5))):
        dev(tag, pump_model, dept, day)
    dev("CE-OR", pump_model, dept, now - timedelta(days=9), status=AssetStatus.RETIRED)  # retired: never overdue
    with tenant_context(other_tenant):  # another hospital's overdue device never inflates this badge
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C")
        Asset.objects.create(tag="THEIRS", device_model=dm, department=Department.objects.create(name="ICU"), next_pm_on=now - timedelta(days=2))
    counts = nav_counts()
    assert counts["pm"] == 2 and counts["pm_hot"] is True


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


# --- review fixes: one plan for the day panel, the create action, and the workload; windows; what blocks a device ----------

def plates(today=TODAY):
    return {w.technician.name: (w.pm_count, w.pm_hours) for w in sch.workload_next_7_days(today)}


def test_workload_is_unchanged_after_the_nightly_job_creates_unassigned_pms(fleet, techs):
    """generate_pm (21-day lead) makes unassigned PM work orders for everything due this week; they must stay on someone's plate."""
    before = plates()
    assert sum(n for n, _h in before.values()) == 3  # v1, v2, and p1, all due tomorrow
    assert generate_pm_work_orders(as_of=TODAY, lead_days=21) == 4 and not WorkOrder.objects.filter(assigned_to__isnull=False).exists()
    assert plates() == before


def test_open_pms_with_a_deactivated_technician_are_replanned_and_vendor_pms_are_nobodys(fleet, techs):
    gone = Technician.objects.create(name="Gone Tech", is_active=True)
    assign(create_work_order(asset=fleet["v1"], type=WoType.PM, priority="high", problem="PM"), technician=gone)
    Technician.objects.filter(pk=gone.pk).update(is_active=False)
    assign(create_work_order(asset=fleet["v2"], type=WoType.PM, priority="high", problem="PM"), vendor_name="Hamilton Medical")
    p = plates()
    assert "Gone Tech" not in p and p["Dana Whitfield"][0] + p["Tom Okafor"][0] == 2  # v1 (replanned) and p1; v2 is the vendor's


def test_the_day_panel_the_create_action_and_the_workload_name_the_same_technician(ctx, dept, pump_model, techs):
    """Both technicians do pumps and start empty. Tomorrow's pump goes to Dana (tie, by name); the next day's to Tom, because the
    plan carries tomorrow's hour forward. Every view of the plan says so, and creating the later day first changes nothing."""
    d1, d2 = TODAY + timedelta(days=1), TODAY + timedelta(days=2)
    a, b = dev("CE-A", pump_model, dept, d1), dev("CE-B", pump_model, dept, d2)
    assert sch.day_plan(d2, TODAY)["rows"][0]["technician"] == techs["tom"]
    assert plates()["Tom Okafor"] == (1, Decimal("1")) and plates()["Dana Whitfield"] == (1, Decimal("1"))
    create_pm_work_orders_for_day(d2, today=TODAY)
    assert WorkOrder.objects.get(asset=b).assigned_to == techs["tom"]
    assert sch.day_plan(d1, TODAY)["rows"][0]["technician"] == techs["dana"]
    create_pm_work_orders_for_day(d1, today=TODAY)
    assert WorkOrder.objects.get(asset=a).assigned_to == techs["dana"]


def test_procedure_hours_decide_who_is_lighter(fleet, techs):
    """Tom starts with 2 h open. The two ventilators (1.5 h each, only Dana is credentialed) put Dana at 3 h, so the pump goes to Tom.
    Counting a flat hour per device would leave Dana at 2 h and hand her the pump on the name tie."""
    assign(create_work_order(asset=fleet["p_over"], type=WoType.REPAIR, priority="normal", problem="Door", estimated_hours=Decimal("2")),
           technician=techs["tom"])
    who = {r["asset"].tag: r["technician"].name for r in sch.day_plan(date(2026, 9, 30), TODAY)["rows"]}
    assert who == {"CE-V1": "Dana Whitfield", "CE-V2": "Dana Whitfield", "CE-P1": "Tom Okafor"}


def test_suggestions_cost_the_same_queries_for_many_device_models(ctx, dept, techs):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def queries(n):
        for i in range(n):
            dm = DeviceModel.objects.create(manufacturer="M", model=f"N{n}-{i}", description="Pump", category="Infusion pumps")
            dev(f"CE-N{n}-{i}", dm, dept, TODAY + timedelta(days=10 + n))
        with CaptureQueriesContext(connection) as q:
            sch.day_plan(TODAY + timedelta(days=10 + n), TODAY)
        return len(q)

    assert queries(3) == queries(15)  # technicians and their credentials are read once, not once per device model


def test_the_30_day_window_includes_today_and_day_30(ctx, dept, pump_model):
    for tag, n in (("CE-D0", 0), ("CE-D30", 30), ("CE-D31", 31), ("CE-DM1", -1)):
        dev(tag, pump_model, dept, TODAY + timedelta(days=n))
    out = sch.next_30_days(TODAY)
    assert (out["due"], out["overdue"]) == (2, 1)  # today and day 30 are due; yesterday is overdue; day 31 is not yet counted


def test_the_7_day_workload_window_is_today_through_day_6(ctx, dept, pump_model, techs):
    for tag, n in (("CE-W0", 0), ("CE-W6", 6), ("CE-W7", 7)):
        dev(tag, pump_model, dept, TODAY + timedelta(days=n))
    assert sum(n for n, _h in plates().values()) == 2


def test_only_an_open_pm_blocks_a_device(fleet):
    """An open repair on a device, or a PM that was cancelled or closed, does not stand for this PM."""
    create_work_order(asset=fleet["v1"], type=WoType.REPAIR, priority="normal", problem="Alarm")
    cancelled = create_work_order(asset=fleet["v2"], type=WoType.PM, priority="high", problem="PM")
    WorkOrder.objects.filter(pk=cancelled.pk).update(status=WoStatus.CANCELLED)
    assert sch.day_plan(date(2026, 9, 30), TODAY)["to_create"] == 3
    assert create_pm_work_orders_for_day(date(2026, 9, 30), today=TODAY) == (3, 0, 0)


def test_neighbouring_months_on_the_grid_are_not_due_this_month(fleet, dept, pump_model):
    dev("CE-OCT2", pump_model, dept, date(2026, 10, 2))  # on September's grid, in October
    cal = sch.month_calendar(2026, 9, TODAY)
    cells = {c["date"]: c for w in cal["weeks"] for c in w}
    assert cells[date(2026, 10, 2)]["n"] == 1 and cells[date(2026, 10, 2)]["in_month"] is False and cal["due_this_month"] == 4


def test_a_device_due_today_is_not_overdue(ctx, dept, pump_model):
    dev("CE-TODAY", pump_model, dept, TODAY)
    cells = {c["date"]: c for w in sch.month_calendar(2026, 9, TODAY)["weeks"] for c in w}
    assert cells[TODAY]["n"] == 1 and cells[TODAY]["past"] is False and sch.day_plan(TODAY, TODAY)["overdue"] is False
    create_pm_work_orders_for_day(TODAY, today=TODAY)
    assert WorkOrder.objects.get(asset__tag="CE-TODAY").due_on == TODAY
