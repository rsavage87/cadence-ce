"""
The survey binder's "Scheduled maintenance (PM) completion" section (slice 25, part 1; apps/reports/survey/maintenance.py), and the
shared on-time rule it rests on (apps.pm.services.RETIRED_AND_CANCELLED: a cancelled PM is excused only when its device went into
retirement on or before the PM's due date).

Totals are pm_on_time_rate's for the same days; the custom report's PM on time column, pm_on_time_series, and the Overview agree on a
PM cancelled on a device retired before, after, or retired-reinstated-retired around its due date; PMs not on time with why and
when the reason was recorded; due dates moved after they had passed, read from real history (update_asset moving a device's next PM,
and the API's due_on change); failed PMs and their repairs; life-support and high-risk devices past their PM date; the gaps and
findings (never for imported PMs); another facility's history never read; nothing a requester typed; a fixed number of queries.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.dates import month_bounds
from apps.pm.services import missed_pms, pm_due_queryset, pm_on_time_rate, pm_on_time_series
from apps.reports import custom
from apps.reports.services import overview_kpis
from apps.reports.survey import FINDING, GAP, maintenance
from apps.tenants.context import tenant_context
from apps.workorders.models import LateReason, PmResult, Priority, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order, set_late_reason

SECRET = "Jane Roe bed 4 canary"  # what a requester typed: never in the binder


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def aware(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def device(tag, model, dept, added, **fields):
    """A device whose history starts on `added` (the rule reads the day it went into retirement from its history)."""
    a = Asset.objects.create(tag=tag, device_model=model, department=dept, notes=SECRET, **fields)
    Asset.history.filter(id=a.pk).update(history_date=aware(added, 9))
    return a


def pm(asset, due, done=None, status=None, reason="", source=Source.PM_PLANNER, result=""):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem=SECRET, requester=SECRET, opened_on=due - timedelta(days=20),
                           due_on=due, source=source, callback=SECRET, reported_location=SECRET)
    fields = {}
    if done:
        fields.update(status=WoStatus.COMPLETED, completed_on=done, resolution=SECRET)
    if status:
        fields["status"] = status
    if reason:
        fields["late_reason"] = reason
    if result:
        fields["pm_result"] = result
    if fields:
        WorkOrder.objects.filter(pk=wo.pk).update(**fields)
        wo.refresh_from_db()
    return wo


@pytest.fixture
def models(ctx, vent_model, pump_model):
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                         risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    thermo = DeviceModel.objects.create(manufacturer="Welch Allyn", model="SureTemp", description="Thermometer", category="Thermometers",
                                        risk_class=RiskClass.LOW, oem_pm_interval_months=12)
    return {"ls": vent_model, "high": pump_model, "medium": monitor, "low": thermo}


@pytest.fixture
def world(ctx, today, dept, models):
    """Thirteen PMs due in the last 100 days across the four classes, six on time; a few outside the period; and devices past their
    PM date today (a life-support one with its PM open, a high-risk one marked missing, a medium one)."""
    T, added = today, today - timedelta(days=400)
    v1 = device("CE-1", models["ls"], dept, added, next_pm_on=T - timedelta(days=30))
    p1 = device("CE-2", models["high"], dept, added, next_pm_on=T + timedelta(days=60))
    p2 = device("CE-3", models["high"], dept, added, next_pm_on=T + timedelta(days=40), status=AssetStatus.MISSING)
    m1 = device("CE-4", models["medium"], dept, added, next_pm_on=T - timedelta(days=5))
    l1 = device("CE-5", models["low"], dept, added, next_pm_on=T + timedelta(days=100))
    w = {"v1": v1, "p1": p1, "p2": p2, "m1": m1, "l1": l1}
    w["a"] = pm(v1, T - timedelta(days=60), done=T - timedelta(days=62))
    w["b"] = pm(v1, T - timedelta(days=40), done=T - timedelta(days=28))
    w["c"] = pm(v1, T - timedelta(days=30))
    w["imp"] = pm(v1, T - timedelta(days=70), done=T - timedelta(days=65), source=Source.IMPORTED)
    w["d"] = pm(p1, T - timedelta(days=50), done=T - timedelta(days=45), reason=LateReason.STAFFING)
    w["e"] = pm(p1, T - timedelta(days=20), status=WoStatus.CANCELLED)
    w["f"] = pm(p1, T - timedelta(days=19), done=T - timedelta(days=15), reason=LateReason.SCHEDULING)
    for days in (80, 70, 60, 50):
        pm(m1, T - timedelta(days=days), done=T - timedelta(days=days))
    w["m_late"] = pm(m1, T - timedelta(days=45), done=T - timedelta(days=40))
    pm(l1, T - timedelta(days=30), done=T - timedelta(days=31))
    pm(v1, T - timedelta(days=150), done=T - timedelta(days=120))  # due before the period
    pm(l1, T + timedelta(days=5))  # not due yet
    return w


def build(today, days=100):
    return maintenance.build(period(today - timedelta(days=days), today), None)


def by_class(section) -> dict:
    return {row[0]: row[1:] for row in rows_of(section, "by_class")}


def test_totals_are_pm_on_time_rates_for_the_same_days(world, today):
    s = build(today)
    start = today - timedelta(days=100)
    kpi, kpi_ls = pm_on_time_rate(start, today, today), pm_on_time_rate(start, today, today, life_support_only=True)
    classes = by_class(s)
    assert sum(r[0] for r in classes.values()) == kpi["due"] == 13 and sum(r[1] for r in classes.values()) == kpi["on_time"] == 6
    assert (classes["Life support"][0], classes["Life support"][1]) == (kpi_ls["due"], kpi_ls["on_time"]) == (4, 1)
    assert classes == {
        # PMs due, on time, not on time, on time %, target %, met, devices past their PM date today
        "Life support": [4, 1, 3, Decimal("25.0"), Decimal("100.0"), "No", 1],
        "High": [3, 0, 3, Decimal("0.0"), Decimal("100.0"), "No", 1],
        "Medium": [5, 4, 1, Decimal("80.0"), Decimal("95.0"), "No", 1],
        "Low": [1, 1, 0, Decimal("100.0"), Decimal("95.0"), "Yes", 0],
    }
    figures = {f.label: f for f in s.figures}
    assert figures["PMs due in the period"].value == 13 and figures["Completed on time"].value == "46.1%"  # 6 of 13, cut not rounded
    assert figures["PMs on time, medium risk"].hint == "4 of 5; target 95%, not met"
    assert figures["Devices past their PM date today"].value == 3
    assert s.table("not_on_time").count == 7 == len(rows_of(s, "not_on_time"))
    missed = pm_due_queryset(start, today, today).filter(pk__in=missed_pms(today))
    assert {r[0] for r in rows_of(s, "not_on_time")} == set(missed.values_list("number", flat=True))


def test_pms_not_on_time_say_why_and_connect_a_cancelled_pm_to_the_work(world, today):
    T, w = today, world
    rows = {r[0]: r for r in rows_of(build(today), "not_on_time")}
    assert list(rows) == [w[k].number for k in ("imp", "d", "m_late", "b", "c", "e", "f")]  # by due date
    e = rows[w["e"].number]
    # work order, device, model, class, due, completed on, days late, done later on, done on work order, why late, reason recorded on
    assert e == [w["e"].number, "CE-2", "BD Alaris 8015 PCU", "High", T - timedelta(days=20), "Cancelled", 20, T - timedelta(days=15), w["f"].number,
                 "", None]
    # Still open, 30 days past due; the device's last PM (the one before, done late) was two days after this one fell due.
    assert rows[w["c"].number][5:9] == ["Open", 30, T - timedelta(days=28), w["b"].number]
    assert rows[w["b"].number][5:8] == [T - timedelta(days=28), 12, T - timedelta(days=28)]  # its own completion
    assert rows[w["imp"].number][9] == "Imported from the previous system"
    assert rows[w["d"].number][9] == "Staffing or workload"
    assert maintenance.build(period(T - timedelta(days=100), T), None).table("not_on_time").links == {0: "work_order", 1: "device", 8: "work_order"}


def test_gaps_and_findings(world, today):
    T, w = today, world
    s = build(today)
    gaps = [(g.text, g.url, g.record) for g in gaps_of(s, GAP)]
    b, c, e = w["b"].number, w["c"].number, w["e"].number
    assert gaps == [
        (f"{b} on CE-1 was done 12 days late with no reason recorded", f"/work-orders/{b}/", b),
        (f"{c} on CE-1 is 30 days past its due date with no reason recorded", f"/work-orders/{c}/", c),
        (f"{e} on CE-2 was cancelled without being done (due {(T - timedelta(days=20)):%b} {(T - timedelta(days=20)).day}, "
         f"{(T - timedelta(days=20)).year}) with no reason recorded", f"/work-orders/{e}/", e),
        (f"CE-1 (life support) is 30 days past its PM date ({(T - timedelta(days=30)):%b} {(T - timedelta(days=30)).day}, "
         f"{(T - timedelta(days=30)).year}); {c} is open", "/equipment/CE-1/", "CE-1"),
        ("CE-3 (high risk) is marked missing; a missing device counts as past its PM date until it is found", "/equipment/CE-3/", "CE-3"),
    ]  # the imported PM and the ones with a reason never; the medium device past its date is counted, not listed
    findings = gaps_of(s, FINDING)
    assert [(g.text, g.url) for g in findings] == [("Medium risk: 80% on time against the facility's 95% target", "")]
    set_late_reason(w["b"], LateReason.DEVICE_IN_USE, today=T)
    assert w["b"].number not in " ".join(g.text for g in build(today).gaps)


def test_a_reason_recorded_after_completion_shows_its_day(ctx, today, dept, models):
    T = today
    v = device("CE-9", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    later = pm(v, T - timedelta(days=40), done=T - timedelta(days=35))
    set_late_reason(later, LateReason.OTHER, today=T)  # recorded today, after it was completed
    early = pm(v, T - timedelta(days=10))
    set_late_reason(early, LateReason.NOT_LOCATED, today=T)  # recorded while still open
    WorkOrder.objects.filter(pk=early.pk).update(status=WoStatus.COMPLETED, completed_on=T)
    rows = {r[0]: r for r in rows_of(build(today), "not_on_time")}
    assert rows[later.number][9:] == ["Other (see the work order's notes)", T]
    assert rows[early.number][9:] == ["Device could not be located", None]


def test_failed_pms_and_their_repairs(ctx, today, dept, models):
    T = today
    v = device("CE-9", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    failed = pm(v, T - timedelta(days=20), done=T - timedelta(days=21), result=PmResult.FAIL)
    repair = create_work_order(asset=v, type=WoType.REPAIR, priority=Priority.HIGH, problem=SECRET, opened_on=T - timedelta(days=21), follow_up_of=failed)
    WorkOrder.objects.filter(pk=repair.pk).update(status=WoStatus.COMPLETED, completed_on=T - timedelta(days=18))
    alone = pm(v, T - timedelta(days=5), done=T - timedelta(days=5), result=PmResult.FAIL)
    pm(v, T - timedelta(days=200), done=T - timedelta(days=200), result=PmResult.FAIL)  # before the period
    s = build(today)
    assert rows_of(s, "failed") == [[failed.number, "CE-9", T - timedelta(days=21), repair.number, "Completed", T - timedelta(days=18)],
                                    [alone.number, "CE-9", T - timedelta(days=5), "", "", None]]
    assert s.table("failed").count == 2 and s.table("failed").links == {0: "work_order", 1: "device", 3: "work_order"}


def test_life_support_and_high_risk_devices_past_their_pm_date(world, today):
    T, w = today, world
    s = build(today)
    assert rows_of(s, "past_pm_date") == [
        ["CE-1", "Hamilton Medical Hamilton-G5", "Life support", "In service", T - timedelta(days=30), 30, w["c"].number],
        ["CE-3", "BD Alaris 8015 PCU", "High", "Missing", T + timedelta(days=40), None, ""],
    ]
    assert "as of today" in s.covers and s.covers.startswith(period(T - timedelta(days=100), T).label)


# --- the shared rule: a PM cancelled on a retired device --------------------------------------------------------------------------


@pytest.fixture
def retirements(ctx, today, dept, models):
    """Three life-support devices, each with a PM cancelled by retiring it: one retired before the PM fell due, one two months overdue
    and then retired, and one retired, reinstated, missed, and retired again. And one PM done on time, so the rates are not all zero."""
    T = today
    before = device("R-1", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    early = pm(before, T - timedelta(days=30))
    eq.set_status(before, AssetStatus.RETIRED, changed_on=T - timedelta(days=32), today=T)  # cancels `early`
    after = device("R-2", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    overdue = pm(after, T - timedelta(days=80))
    eq.set_status(after, AssetStatus.RETIRED, changed_on=T - timedelta(days=20), today=T)
    again = device("R-3", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    eq.set_status(again, AssetStatus.RETIRED, changed_on=T - timedelta(days=90), today=T)
    eq.set_status(again, AssetStatus.IN_SERVICE, changed_on=T - timedelta(days=85), today=T)
    missed = pm(again, T - timedelta(days=50))
    eq.set_status(again, AssetStatus.RETIRED, changed_on=T - timedelta(days=10), today=T)
    ok = device("R-4", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    done = pm(ok, T - timedelta(days=40), done=T - timedelta(days=41))
    for wo in (early, overdue, missed):
        wo.refresh_from_db()
        assert wo.status == WoStatus.CANCELLED
    return {"early": early, "overdue": overdue, "missed": missed, "done": done}


def test_a_pm_cancelled_by_retirement_is_excused_only_when_the_device_retired_by_its_due_date(retirements, today):
    r = retirements
    start = today - timedelta(days=100)
    counted = set(pm_due_queryset(start, today, today).values_list("number", flat=True))
    assert counted == {r["overdue"].number, r["missed"].number, r["done"].number}
    assert set(missed_pms(today).values_list("number", flat=True)) == {r["overdue"].number, r["missed"].number}
    assert pm_on_time_rate(start, today, today) == {"due": 3, "on_time": 1, "rate": pytest.approx(100 / 3)}
    s = build(today)
    rows = {row[0]: row for row in rows_of(s, "not_on_time")}
    assert set(rows) == {r["overdue"].number, r["missed"].number} and rows[r["overdue"].number][5] == "Cancelled"
    assert by_class(s)["Life support"][:2] == [3, 1]


def test_the_custom_report_the_series_and_the_overview_agree_on_it(retirements, today):
    r = retirements
    on_time = {row[0]: row[1] for row in custom.run(custom.clean_definition("work_orders", ["number", "pm_on_time"]), today)["rows"]}
    assert on_time[r["early"].number] is None and on_time[r["overdue"].number] is False and on_time[r["missed"].number] is False
    assert on_time[r["done"].number] is True
    for point in pm_on_time_series(today.year, today.month, months=6, today=today):
        start, end = month_bounds(point["year"], point["month"])
        if start > today:
            continue
        kpi = pm_on_time_rate(start, end, today)
        assert (point["due"], point["on_time"]) == (kpi["due"], kpi["on_time"]), point
        overview = overview_kpis(point["year"], point["month"], today=today)["pm_on_time"]
        assert (overview["due"], overview["on_time"]) == (kpi["due"], kpi["on_time"])
    total = sum(p["due"] for p in pm_on_time_series(today.year, today.month, months=6, today=today))
    assert total == 3  # the overdue one, the missed one, and the one done: the early one is excused everywhere


# --- due dates moved after they had passed (real history) -----------------------------------------------------------------------


def test_due_dates_moved_after_they_had_passed_come_from_history(ctx, today, dept, models, client, make_user):
    T = today
    director = make_user("director")
    v = device("CE-20", models["ls"], dept, T - timedelta(days=300), next_pm_on=T - timedelta(days=10))
    moved = pm(v, T - timedelta(days=10))
    eq.update_asset(v, next_pm_on=T + timedelta(days=5), today=T)  # moves the open PM with the device's next PM
    p = device("CE-21", models["high"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=60))
    api = pm(p, T - timedelta(days=5))
    client.force_login(director)
    r = client.patch(f"/api/v1/work-orders/{api.pk}/", {"due_on": (T + timedelta(days=3)).isoformat()}, content_type="application/json")
    assert r.status_code == 200, r.content
    m = device("CE-22", models["medium"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=60))
    medium = pm(m, T - timedelta(days=4))
    eq.update_asset(m, next_pm_on=T + timedelta(days=9), today=T)
    early = pm(v, T - timedelta(days=60), done=T - timedelta(days=40))  # moved the day before it fell due: never listed
    WorkOrder.objects.filter(pk=early.pk).update(due_on=T - timedelta(days=55))
    WorkOrder.history.filter(id=early.pk).update(history_date=aware(T - timedelta(days=70)))  # its creation and the move, both early
    s = build(today)
    rows = rows_of(s, "due_moved")
    assert sorted(rows) == sorted([
        [moved.number, "CE-20", T - timedelta(days=10), T + timedelta(days=5), T, "Cadence"],  # no request: a job's change
        [api.number, "CE-21", T - timedelta(days=5), T + timedelta(days=3), T, "Director User"],
        [medium.number, "CE-22", T - timedelta(days=4), T + timedelta(days=9), T, "Cadence"],
    ])
    findings = {g.record: g for g in gaps_of(s, FINDING)}
    assert set(findings) == {moved.number, api.number}  # life support and high risk only
    d0, d1 = T - timedelta(days=10), T + timedelta(days=5)
    assert findings[moved.number].text == (f"{moved.number} on CE-20: its due date was moved from {d0:%b} {d0.day}, {d0.year} to "
                                           f"{d1:%b} {d1.day}, {d1.year} on {T:%b} {T.day}, {T.year}, after it had passed")
    assert findings[moved.number].url == f"/work-orders/{moved.number}/"
    # They count against their current due date (after today: not counted yet), as the Overview counts them.
    assert not pm_due_queryset(T - timedelta(days=100), T, T).filter(pk__in=[moved.pk, api.pk, medium.pk]).exists()


def test_another_facilitys_history_is_never_read(ctx, today, tenant, other_tenant, dept, models):
    T = today
    v = device("CE-20", models["ls"], dept, T - timedelta(days=300), next_pm_on=T - timedelta(days=10))
    ours = pm(v, T - timedelta(days=10))
    eq.update_asset(v, next_pm_on=T + timedelta(days=5), today=T)
    with tenant_context(other_tenant):
        their_model = DeviceModel.objects.create(manufacturer="Zoll", model="R", description="Defib", category="Defibrillators",
                                                 risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        theirs = Asset.objects.create(tag="OT-1", device_model=their_model, department=Department.objects.create(name="ED"), next_pm_on=T - timedelta(days=12))
        twos = [pm(theirs, T - timedelta(days=12)), pm(theirs, T - timedelta(days=30), done=T - timedelta(days=20))]
        eq.update_asset(theirs, next_pm_on=T + timedelta(days=8), today=T)
        set_late_reason(twos[1], LateReason.STAFFING, today=T)
    s = build(today)
    assert [r[1] for r in rows_of(s, "due_moved")] == ["CE-20"] and [g.record for g in gaps_of(s, FINDING)] == [ours.number]
    assert all(r[1] != "OT-1" for t in s.tables for r in t.rows())
    assert "OT-1" not in " ".join(g.text for g in s.gaps)


# --- no requester text, and a fixed number of queries -----------------------------------------------------------------------------


def test_nothing_a_requester_typed(world, today):
    s = build(today)
    shown = [s.topic, s.covers, *s.notes, *(str(f.value) + f.hint for f in s.figures), *(g.text for g in s.gaps)]
    shown += [str(v) for t in s.tables for row in t.rows() for v in row]
    assert not any(SECRET in text for text in shown)


def _make(n, today, dept, models, offset):
    T = today
    for i in range(n):
        for key in ("ls", "high", "medium"):
            a = device(f"Q-{offset}-{key}-{i}", models[key], dept, T - timedelta(days=300), next_pm_on=T - timedelta(days=3))
            late = pm(a, T - timedelta(days=30), done=T - timedelta(days=25), reason=LateReason.STAFFING if i % 2 else "")
            set_late_reason(late, LateReason.OTHER, today=T)
            pm(a, T - timedelta(days=60), status=WoStatus.CANCELLED)
            failed = pm(a, T - timedelta(days=50), done=T - timedelta(days=50), result=PmResult.FAIL)
            create_work_order(asset=a, type=WoType.REPAIR, priority=Priority.HIGH, problem="x", opened_on=T - timedelta(days=50), follow_up_of=failed)
            pm(a, T - timedelta(days=2))
            eq.update_asset(a, next_pm_on=T + timedelta(days=30), today=T)  # moves the open PM after its due date


def test_a_fixed_number_of_queries(ctx, today, dept, models):
    def queries():
        with CaptureQueriesContext(connection) as q:
            s = build(today)
            for t in s.tables:
                list(t.rows())
        return len(q.captured_queries), s

    _make(2, today, dept, models, "a")
    few, s = queries()
    assert len(rows_of(s, "due_moved")) == 6 and len(rows_of(s, "failed")) == 6
    _make(17, today, dept, models, "b")
    many, s = queries()
    assert len(rows_of(s, "due_moved")) == 57 and len(rows_of(s, "not_on_time")) == 2 * 57
    assert many == few, (few, many)


# --- under row-level security -----------------------------------------------------------------------------------------------------


@needs_postgres
def test_history_is_read_under_the_policies(ctx, tenant, other_tenant, today, dept, models):
    """As the runtime role, the section reads this facility's work order history (moves, when a reason was recorded) and the device
    history the retirement rule reads."""
    T = today
    v = device("CE-20", models["ls"], dept, T - timedelta(days=300), next_pm_on=T - timedelta(days=10))
    moved = pm(v, T - timedelta(days=10))
    eq.update_asset(v, next_pm_on=T + timedelta(days=5), today=T)
    late = pm(v, T - timedelta(days=40), done=T - timedelta(days=35))
    set_late_reason(late, LateReason.OTHER, today=T)
    gone = device("R-1", models["ls"], dept, T - timedelta(days=300), next_pm_on=T + timedelta(days=100))
    excused = pm(gone, T - timedelta(days=30))
    eq.set_status(gone, AssetStatus.RETIRED, changed_on=T - timedelta(days=32), today=T)
    as_app_role()
    with tenant_context(tenant):
        s = build(today)
        assert [r[0] for r in rows_of(s, "due_moved")] == [moved.number]
        assert {r[0]: r[10] for r in rows_of(s, "not_on_time")}[late.number] == T
        assert not pm_due_queryset(T - timedelta(days=100), T, T).filter(pk=excused.pk).exists()
