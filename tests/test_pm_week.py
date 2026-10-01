"""Auto-assign week (slice 14, apps.pm.services.week_assignment_preview and assign_week): every device in the week plan ends with the
technician the plan suggested (new work orders created, open ones on nobody's plate assigned, held ones left alone, nobody credentialed
left unassigned), the day panel and the workload agree before and after, a second run does nothing, overdue devices are counted and
left alone, the planner lock is taken before the plan is read, and another facility is never touched or counted."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db.models.query import QuerySet

from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm import schedule as sch
from apps.pm import services
from apps.pm.models import PmProcedure
from apps.pm.services import assign_week, create_pm_work_orders_for_day, generate_pm_work_orders, week_assignment_preview
from apps.tenants.context import tenant_context
from apps.workorders.models import OPEN_STATUSES, Source, WorkOrder, WorkOrderStatusHistory, WoType
from apps.workorders.services import assign, create_work_order

TODAY = date(2026, 9, 29)  # a Tuesday; the week is Sep 29 through Oct 5
SEP30 = date(2026, 9, 30)


def dev(tag, model, dept, day, status=AssetStatus.IN_SERVICE):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=day, status=status)


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Due Sep 30: two ventilators (life support, 1.5 h procedure; only Dana is credentialed) and a pump (high, no procedure: 1 h;
    Dana and Tom). Sep 15: a pump, now overdue. A retired vent on Sep 30 and a pump due Oct 6 (day 7) are outside the week."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"),
                                                         checklist=["Inspect", "Safety test", "Function test"])
    vent_model.save()
    return {"v1": dev("CE-V1", vent_model, dept, SEP30), "v2": dev("CE-V2", vent_model, dept, SEP30), "p1": dev("CE-P1", pump_model, dept, SEP30),
            "p_over": dev("CE-P2", pump_model, dept, date(2026, 9, 15)), "retired": dev("CE-VR", vent_model, dept, SEP30, status=AssetStatus.RETIRED),
            "later": dev("CE-P9", pump_model, dept, TODAY + timedelta(days=7))}


@pytest.fixture
def monitor(ctx, dept):
    """A medium-risk device due Sep 30 that nobody is credentialed for."""
    dm = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
    return dev("CE-M1", dm, dept, SEP30)


def open_pm(asset):
    return WorkOrder.objects.filter(asset=asset, type=WoType.PM, status__in=OPEN_STATUSES).order_by("opened_on", "number").first()


def holders() -> dict:
    """{tag: who holds the device's open PM work order}: a technician's name, "vendor", None (unassigned), or "-" (no open PM)."""
    out = {}
    for a in Asset.objects.all():
        w = open_pm(a)
        out[a.tag] = "-" if w is None else "vendor" if w.vendor_service else (w.assigned_to.name if w.assigned_to else None)
    return out


def suggested_names(today=TODAY) -> dict:
    """{tag: the technician's name, or None} for every device the week plan suggests someone for (its PM is on nobody's plate)."""
    plan = sch.week_plan(today)
    tags = {a.id: a.tag for a in plan["devices"]}
    return {tags[i]: (t.name if t else None) for i, t in plan["suggested"].items()}


def plates(today=TODAY) -> dict:
    return {w.technician.name: (w.pm_count, w.pm_hours) for w in sch.workload_next_7_days(today)}


def shares(w) -> dict:
    return {s.technician.name: (s.new, s.existing, s.count, s.hours) for s in w.shares}


# --- the preview ---------------------------------------------------------------------------------------------------------------

def test_the_preview_reads_the_week_plan_and_changes_nothing(fleet, techs, monitor):
    w = week_assignment_preview(TODAY)
    assert (w.start, w.end) == (TODAY, date(2026, 10, 5))
    # vents only Dana can do (1.5 h each); the pump goes to Tom's lighter plate; nobody does the monitor
    assert shares(w) == {"Dana Whitfield": (2, 0, 2, Decimal("3.0")), "Tom Okafor": (1, 0, 1, Decimal("1"))}
    assert [u["asset"].tag for u in w.uncovered] == ["CE-M1"] and w.uncovered[0]["has_open_pm"] is False
    assert (w.created, w.assigned, w.assigned_new, w.assigned_existing, w.unassigned, w.unassigned_new) == (4, 3, 3, 0, 1, 1)
    assert (w.held, w.overdue, w.technicians, w.hours, w.nothing_to_do) == (0, 1, 2, Decimal("4.0"), False)
    assert not WorkOrder.objects.exists()


def test_an_empty_week_has_nothing_to_do(ctx, techs):
    w = week_assignment_preview(TODAY)
    assert w.nothing_to_do and (w.created, w.assigned, w.unassigned, w.held, w.overdue) == (0, 0, 0, 0, 0) and w.shares == []


# --- assigning --------------------------------------------------------------------------------------------------------------------

def test_assign_week_creates_and_assigns_what_the_plan_suggests(fleet, techs, monitor, make_user):
    kim = make_user("director")
    before = suggested_names()
    assert before == {"CE-V1": "Dana Whitfield", "CE-V2": "Dana Whitfield", "CE-P1": "Tom Okafor", "CE-M1": None}
    done = assign_week(by=kim, today=TODAY)
    assert (done.created, done.assigned, done.unassigned, done.held, done.overdue) == (4, 3, 1, 0, 1)
    assert shares(done) == {"Dana Whitfield": (2, 0, 2, Decimal("3.0")), "Tom Okafor": (1, 0, 1, Decimal("1"))}
    h = holders()
    assert {tag: h[tag] for tag in before} == before  # each device is with exactly the technician the plan suggested
    assert h["CE-P2"] == "-" and h["CE-VR"] == "-" and h["CE-P9"] == "-"  # overdue, retired, and day 7 are outside the week
    made = WorkOrder.objects.filter(type=WoType.PM)
    assert made.count() == 4 and set(made.values_list("created_by", flat=True)) == {kim.pk}
    assert set(made.values_list("source", flat=True)) == {Source.PM_PLANNER}
    v1 = open_pm(fleet["v1"])
    assert (v1.opened_on, v1.due_on, v1.estimated_hours, v1.priority) == (TODAY, SEP30, Decimal("1.5"), "high")
    assert v1.problem == "Scheduled 6-month preventive maintenance, HA-G5-PM6"
    note = v1.status_history.get(note__startswith="Assigned to")
    assert note.note == "Assigned to Dana Whitfield" and note.changed_by == kim  # recorded like any assignment
    assert not open_pm(monitor).status_history.filter(note__startswith="Assigned").exists()


def test_a_second_run_creates_and_assigns_nothing(fleet, techs, monitor):
    assign_week(today=TODAY)
    wos, history = WorkOrder.objects.count(), WorkOrderStatusHistory.objects.count()
    again = assign_week(today=TODAY)
    assert again.nothing_to_do and (again.created, again.assigned, again.held, again.unassigned) == (0, 0, 3, 1)
    assert WorkOrder.objects.count() == wos and WorkOrderStatusHistory.objects.count() == history
    assert week_assignment_preview(TODAY).nothing_to_do


def test_the_day_panel_and_the_workload_agree_before_and_after(fleet, techs, monitor):
    workload, panel = plates(), sch.day_plan(SEP30, TODAY)
    named = {r["asset"].tag: (r["technician"].name if r["technician"] else None) for r in panel["rows"]}
    assign_week(today=TODAY)
    assert plates() == workload  # the workload already counted what the plan suggested
    after = sch.day_plan(SEP30, TODAY)
    assert {r["asset"].tag: r["open_pm_assignee"] for r in after["rows"]} == named  # the day list now names them from the work orders
    assert after["to_create"] == 0 and all(r["has_open_pm"] for r in after["rows"])
    picks, _open = sch.planned_technicians(SEP30, TODAY)  # route sheets and the device drawer: CE-M1 is the only device still waiting
    assert picks == {monitor.id: None}


def test_the_plan_carries_load_across_the_week(ctx, dept, pump_model, techs):
    """Both technicians do pumps and start empty: day 1's pump goes to Dana (tie, by name), day 2's to Tom, day 3's to Dana."""
    for i in (1, 2, 3):
        dev(f"CE-D{i}", pump_model, dept, TODAY + timedelta(days=i))
    done = assign_week(today=TODAY)
    assert holders() == {"CE-D1": "Dana Whitfield", "CE-D2": "Tom Okafor", "CE-D3": "Dana Whitfield"}
    assert shares(done) == {"Dana Whitfield": (2, 0, 2, Decimal("2")), "Tom Okafor": (1, 0, 1, Decimal("1"))}


def test_creating_one_day_first_then_assigning_the_week_ends_where_the_plan_said(ctx, dept, pump_model, techs):
    for i in (1, 2, 3):
        dev(f"CE-D{i}", pump_model, dept, TODAY + timedelta(days=i))
    before = suggested_names()
    create_pm_work_orders_for_day(TODAY + timedelta(days=2), today=TODAY)
    done = assign_week(today=TODAY)
    assert (done.created, done.assigned, done.held) == (2, 2, 1) and holders() == before


# --- open PM work orders -------------------------------------------------------------------------------------------------------------

def test_open_pms_on_nobodys_plate_are_assigned_and_held_ones_left_alone(fleet, techs, dept, pump_model, make_user):
    """v1: with a deactivated technician (replanned). v2: the vendor's (held). p1 and the overdue pump: unassigned, from the nightly
    job. CE-P3 (Oct 1): with Tom (held)."""
    kim = make_user("manager")
    p3 = dev("CE-P3", pump_model, dept, date(2026, 10, 1))
    assert generate_pm_work_orders(as_of=TODAY, lead_days=21) == 6  # v1, v2, p1, p3, the overdue pump, and CE-P9 (day 7, outside the week)
    gone = Technician.objects.create(name="Gone Tech")
    assign(open_pm(fleet["v1"]), technician=gone)
    Technician.objects.filter(pk=gone.pk).update(is_active=False)
    assign(open_pm(fleet["v2"]), vendor_name="Hamilton Medical")
    assign(open_pm(p3), technician=techs["tom"])
    WorkOrder.objects.filter(pk=open_pm(fleet["p1"]).pk).update(estimated_hours=Decimal("2.5"))
    before, wos = suggested_names(), WorkOrder.objects.count()
    assert before == {"CE-V1": "Dana Whitfield", "CE-P1": "Tom Okafor"}  # only Dana does vents (1.5 h); Tom's 1 h (p3) is then lighter
    done = assign_week(by=kim, today=TODAY)
    assert (done.created, done.assigned, done.assigned_existing, done.held, done.unassigned, done.overdue) == (0, 2, 2, 2, 0, 1)
    assert (done.overdue_to_create, done.overdue_waiting) == (0, 1)  # the overdue pump's PM is open, on nobody's plate
    assert shares(done) == {"Dana Whitfield": (0, 1, 1, Decimal("1.5")), "Tom Okafor": (0, 1, 1, Decimal("2.5"))}  # the work orders' own hours
    h = holders()
    assert (h["CE-V1"], h["CE-P1"], h["CE-V2"], h["CE-P3"]) == ("Dana Whitfield", "Tom Okafor", "vendor", "Tom Okafor")
    assert h["CE-P2"] is None and h["CE-P9"] is None  # overdue, and day 7: not in the week, left unassigned
    assert WorkOrder.objects.count() == wos  # nothing new: the open ones were assigned
    v1 = open_pm(fleet["v1"])
    assert v1.status_history.filter(note="Assigned to Dana Whitfield", changed_by=kim).exists()
    assert open_pm(fleet["v2"]).vendor_name == "Hamilton Medical" and not open_pm(fleet["v2"]).status_history.filter(changed_by=kim).exists()


def test_nobody_credentialed_creates_if_missing_and_leaves_unassigned(fleet, techs, monitor, dept):
    """CE-M1 has no open PM: one is created, unassigned. CE-M2 has an open PM with a deactivated technician and nobody else can do it:
    it stays as it is. Both are counted as left unassigned."""
    m2 = dev("CE-M2", monitor.device_model, dept, date(2026, 10, 2))
    gone = Technician.objects.create(name="Gone Tech")
    assign(create_work_order(asset=m2, type=WoType.PM, priority="normal", problem="PM"), technician=gone)
    Technician.objects.filter(pk=gone.pk).update(is_active=False)
    done = assign_week(today=TODAY)
    assert [(u["asset"].tag, u["has_open_pm"]) for u in done.uncovered] == [("CE-M1", False), ("CE-M2", True)]
    assert (done.unassigned, done.unassigned_new, done.created) == (2, 1, 4)
    assert holders()["CE-M1"] is None and holders()["CE-M2"] == "Gone Tech"
    assert WorkOrder.objects.filter(asset=m2).count() == 1


def test_without_technicians_everything_is_created_unassigned(fleet):
    done = assign_week(today=TODAY)
    assert (done.created, done.assigned, done.unassigned, done.technicians) == (3, 0, 3, 0)
    assert WorkOrder.objects.filter(type=WoType.PM).count() == 3 and not WorkOrder.objects.filter(assigned_to__isnull=False).exists()


def test_closed_and_cancelled_pms_do_not_stand_for_this_one(fleet, techs):
    from apps.workorders.models import WoStatus

    done_wo = create_work_order(asset=fleet["v1"], type=WoType.PM, priority="high", problem="Last PM")
    WorkOrder.objects.filter(pk=done_wo.pk).update(status=WoStatus.CLOSED, assigned_to=techs["dana"])
    assert assign_week(today=TODAY).created == 3 and holders()["CE-V1"] == "Dana Whitfield"
    assert WorkOrder.objects.filter(asset=fleet["v1"]).count() == 2


# --- double click -----------------------------------------------------------------------------------------------------------------

def test_the_planner_lock_is_taken_before_the_plan_is_read(fleet, techs, monkeypatch):
    calls = []
    real_lock, real_plan = QuerySet.select_for_update, services.week_plan

    def lock(qs, *args, **kwargs):
        calls.append(("lock", qs.model.__name__))
        return real_lock(qs, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", lock)
    monkeypatch.setattr(services, "week_plan", lambda today: calls.append(("plan",)) or real_plan(today))
    assign_week(today=TODAY)
    # The facility's planner row, nothing else: locking the devices would wait in a circle with retiring one or a tagged-out request.
    assert calls[:2] == [("lock", "Sequence"), ("plan",)] and ("lock", "Asset") not in calls


def test_creating_a_day_takes_the_same_planner_lock_first(fleet, techs, monkeypatch):
    calls = []
    real_lock = QuerySet.select_for_update

    def lock(qs, *args, **kwargs):
        calls.append(qs.model.__name__)
        return real_lock(qs, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", lock)
    assert create_pm_work_orders_for_day(SEP30, today=TODAY) == (3, 3, 0)
    assert calls[0] == "Sequence" and "Asset" not in calls
    from apps.core.models import Sequence

    assert Sequence.objects.get(key=services.PLANNER_LOCK).value == 0  # a lock row, never a counter


# --- tenant isolation -------------------------------------------------------------------------------------------------------------

def test_another_facility_is_never_touched_or_counted(fleet, techs, monitor, other_tenant):
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Their pump", category="Infusion pumps",
                                        risk_class=RiskClass.HIGH)
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, next_pm_on=SEP30)
        waiting = Asset.objects.create(tag="THEIRS-2", device_model=dm, department=d, next_pm_on=date(2026, 10, 1))
        Asset.objects.create(tag="THEIRS-OLD", device_model=dm, department=d, next_pm_on=date(2026, 9, 1))  # overdue there
        their_wo = create_work_order(asset=waiting, type=WoType.PM, priority="normal", problem="PM")
        aaron = Technician.objects.create(name="Aaron Theirs")
        Credential.objects.create(technician=aaron, scope=Scope.CATEGORY, value="Infusion pumps")
        Credential.objects.create(technician=aaron, scope=Scope.CATEGORY, value="Monitors")
    w = week_assignment_preview(TODAY)
    assert (w.created, w.assigned, w.unassigned, w.held, w.overdue) == (4, 3, 1, 0, 1) and "Aaron Theirs" not in shares(w)
    done = assign_week(today=TODAY)
    assert (done.created, done.assigned, done.unassigned, done.overdue) == (4, 3, 1, 1)
    assert not WorkOrder.objects.filter(assigned_to=aaron).exists() and holders()["CE-M1"] is None  # their technician never gets ours
    with tenant_context(other_tenant):
        assert not WorkOrder.objects.filter(asset=theirs).exists()
        their_wo.refresh_from_db()
        assert their_wo.assigned_to is None and WorkOrder.objects.count() == 1
        assert not WorkOrderStatusHistory.objects.filter(note__startswith="Assigned").exists()
