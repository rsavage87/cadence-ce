"""
Slice 27, the PM completion window in the reports (apps/reports/fleet.py, operations.py, custom.py; the compliance and technician
templates; the custom report builder's column help). Under a window the PM compliance report counts this month's PMs on time by it and
"Overdue now" and compliance from assets_past_window plus the missing devices, and says what it measures; the technician report and
the custom report's PM on time column judge each PM by its class's window; every figure agrees with apps.pm.services' rates for the same
window; each report reads the window once; and the default window changes nothing (the slice 7 and 18 tests stand unchanged).
"""
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import PmWindow
from apps.pm import windows as W
from apps.pm.dates import month_bounds
from apps.pm.services import assets_past_window, pm_on_time_rate, pm_on_time_series, pm_pending
from apps.reports import custom
from apps.reports.fleet import on_time_words, report_compliance
from apps.reports.operations import report_tech
from apps.tenants.context import tenant_context
from apps.web.reports_fleet import present_compliance
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

K = PmWindow
TODAY = date(2026, 9, 28)
MIXED = W.Windows(W.Window(K.DUE_MONTH), W.Window(K.DAYS_AFTER, 14))  # life support and high: the due month; medium and low: 14 days
KINDS = [W.Window(), W.Window(K.DAYS_AFTER, 1), W.Window(K.DAYS_AFTER, 14), W.Window(K.DAYS_AFTER, 45), W.Window(K.DUE_MONTH),
         W.Window(K.NEXT_MONTH)]
PAIRS = [W.Windows(k, k) for k in KINDS] + [MIXED, W.Windows(W.Window(), W.Window(K.NEXT_MONTH))]


def pair_id(w):
    return f"{w.high.kind}{w.high.days or ''}-{w.other.kind}{w.other.days or ''}"


def set_window(w: W.Windows, by=None):
    """Save `w` as the facility's windows, through the service the Settings panel and the API use."""
    return fac.update_settings(by=by, pm_window_high=w.high.kind, pm_window_high_days=w.high.days, pm_window_other=w.other.kind,
                               pm_window_other_days=w.other.days)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                      risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)


def pm(asset, due, done=None, status=None):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=20), due_on=due)
    fields = {}
    if done:
        fields.update(status=WoStatus.COMPLETED, completed_on=done)
    if status:
        fields["status"] = status
    if fields:
        WorkOrder.objects.filter(pk=wo.pk).update(**fields)
    return wo


# --- the PM compliance report ------------------------------------------------------------------------------------------------


@pytest.fixture
def fleet(ctx, dept, vent_model, monitor_model):
    """Sep 28. Life support: V-1 past its due date inside the due month, V-2 past its window, V-3 missing, V-4 missing and waiting for
    its incoming inspection. Medium: M-1 past its due date inside 14 days, M-2 past them. September's PMs: life support one done after
    its due date inside the month and one open; medium one done 14 days after, one 15, one open inside, one open past."""
    def device(tag, model, next_pm, **extra):
        a = Asset.objects.create(tag=tag, device_model=model, department=dept, **extra)
        Asset.objects.filter(pk=a.pk).update(next_pm_on=next_pm)  # pinned after its PMs exist
        return a

    v1 = device("V-1", vent_model, None)
    v2 = device("V-2", vent_model, None)
    v3 = device("V-3", vent_model, None, status=AssetStatus.MISSING)
    device("V-4", vent_model, None, status=AssetStatus.MISSING, awaiting_inspection=True)
    m1 = device("M-1", monitor_model, None)
    m2 = device("M-2", monitor_model, None)
    pm(v1, date(2026, 9, 5), done=date(2026, 9, 20))
    pm(v2, date(2026, 9, 8))
    pm(m1, date(2026, 9, 2), done=date(2026, 9, 16))
    pm(m1, date(2026, 9, 3), done=date(2026, 9, 18))
    pm(m1, date(2026, 9, 20))
    pm(m2, date(2026, 9, 10))
    for a, next_pm in ((v1, date(2026, 9, 10)), (v2, date(2026, 8, 20)), (v3, date(2026, 10, 30)), (m1, date(2026, 9, 20)),
                       (m2, date(2026, 9, 10))):
        Asset.objects.filter(pk=a.pk).update(next_pm_on=next_pm)
    return {"v1": v1, "v2": v2, "v3": v3, "m1": m1, "m2": m2}


def classes(r) -> dict:
    return {c["key"]: c for c in r["classes"]}


def test_compliance_follows_the_window(fleet):
    default = classes(report_compliance(TODAY))
    assert [default["life_support"][k] for k in ("devices", "due", "completed", "on_time", "overdue", "inside", "pending")] == [4, 2, 1, 0, 3, 0, 0]
    assert [default["medium"][k] for k in ("devices", "due", "completed", "on_time", "overdue")] == [2, 4, 2, 0, 2]
    set_window(MIXED)
    r = report_compliance(TODAY)
    by = classes(r)
    ls, med = by["life_support"], by["medium"]
    # V-1's September PM was done after its due date inside the month: on time. V-1 itself is past its due date, inside its window: not
    # overdue. V-2 is past its window; V-3 is missing; V-4 waits for its incoming inspection (never overdue).
    assert [ls[k] for k in ("devices", "due", "completed", "on_time", "overdue", "inside", "pending")] == [4, 2, 1, 1, 2, 1, 1]
    assert ls["compliance_pct"] == 50.0 and not ls["meets"]
    # Medium, 14 days: done 14 days after on time, 15 not; M-1 inside its 14 days, M-2 past them.
    assert [med[k] for k in ("devices", "due", "completed", "on_time", "overdue", "inside", "pending")] == [2, 4, 2, 1, 1, 1, 1]
    assert (r["inside"], r["pending"]) == (2, 2)
    assert r["window"] == {"is_default": False, "words": "by the end of the due month for life support and high risk, within 14 days after "
                                                         "the due date for medium and low risk"}
    assert r["rows"][0] == ["Life support", 4, 2, 1, 1, 2, 50.0, 100]  # the columns and CSV keep their shape


def test_overdue_now_is_assets_past_window_plus_the_missing_devices(fleet):
    for w in PAIRS:
        set_window(w)
        by = classes(report_compliance(TODAY))
        for rc in (RiskClass.LIFE_SUPPORT, RiskClass.MEDIUM):
            past = set(assets_past_window(TODAY, w=w).filter(device_model__risk_class=rc).values_list("tag", flat=True))
            missing = set(Asset.objects.filter(device_model__risk_class=rc, status=AssetStatus.MISSING, awaiting_inspection=False)
                          .values_list("tag", flat=True))
            assert by[rc.value]["overdue"] == len(past | missing), (pair_id(w), rc)


def test_this_months_on_time_agrees_with_the_rate_for_the_same_window(fleet):
    """report_compliance counts every PM due this month (open ones too) on active devices; its On time is the rate's on-time count."""
    start, end = month_bounds(2026, 9)
    for w in PAIRS:
        set_window(w)
        r = report_compliance(TODAY)
        assert sum(c["on_time"] for c in r["classes"]) == pm_on_time_rate(start, end, TODAY, w=w)["on_time"], pair_id(w)
        assert r["pending"] == pm_pending(start, end, TODAY, w=w), pair_id(w)


def test_the_report_and_its_chart_read_the_window_once(fleet):
    set_window(MIXED)
    with CaptureQueriesContext(connection) as q:
        r = report_compliance(TODAY)
        p = present_compliance(r)
    assert sum("facilitysettings" in x["sql"] for x in q.captured_queries) == 1
    # The chart's series are the window's: the September point is pm_on_time_series' with the same window.
    point = pm_on_time_series(2026, 9, months=1, today=TODAY, w=MIXED)[0]
    assert p["chart"] and point["due"] == pm_on_time_rate(date(2026, 9, 1), date(2026, 9, 30), TODAY, w=MIXED)["due"]


def test_the_note_says_what_is_measured(fleet, client, signed_in, freeze_today):
    freeze_today(TODAY)
    signed_in("director")
    body = client.get("/reports/compliance/").content.decode()
    assert "Compliance here means the share of active devices whose PM is not past due today." in body  # the default's words
    set_window(MIXED)
    body = client.get("/reports/compliance/").content.decode()
    assert "not past due today" not in body
    assert ("Compliance here means the share of active devices whose PM is not past its window today: this facility counts a PM on time "
            "when it is done by the end of the due month for life support and high risk, within 14 days after the due date for medium and "
            "low risk (Settings).") in body
    assert "2 devices past their due date but still inside their window are not counted as overdue." in body
    assert "2 PMs due this month are past their due date and still inside their window: not on time yet, and not late." in body
    assert "Survey binder</a> lists every PM not done within its window" in body
    printed = client.get("/print/reports/compliance/").content.decode()
    assert "not past its window today" in printed and "Survey binder</a> lists" not in printed


def test_another_facilitys_window_is_never_read(fleet, other_tenant):
    with tenant_context(other_tenant):
        set_window(W.Windows(W.Window(K.NEXT_MONTH), W.Window(K.NEXT_MONTH)))
    r = report_compliance(TODAY)
    assert r["window"]["is_default"] and classes(r)["life_support"]["overdue"] == 3


# --- the technician report ---------------------------------------------------------------------------------------------------


def closed(asset, tech, due, done):
    wo = create_work_order(asset=asset, type=WoType.PM, priority="normal", problem="PM", opened_on=due - timedelta(days=10), due_on=due,
                           assigned_to=tech)
    change_status(wo, "in_progress", as_of=due - timedelta(days=10))
    change_status(wo, "completed", as_of=done)
    return wo


def test_the_technician_report_judges_each_pm_by_its_class_window(ctx, dept, vent, pump, monitor_model, techs):
    dana = techs["dana"]
    monitor = Asset.objects.create(tag="M-1", device_model=monitor_model, department=dept)
    closed(vent, dana, date(2026, 9, 5), date(2026, 9, 20))  # life support, 15 days late: inside the due month
    closed(pump, dana, date(2026, 9, 1), date(2026, 9, 10))  # high risk, 9 days: inside the due month
    closed(monitor, dana, date(2026, 9, 1), date(2026, 9, 16))  # medium, 15 days: past 14

    def row():
        return next(t for t in report_tech(TODAY)["technicians"] if t["technician"] == dana)

    assert (row()["pms"], row()["pm_on_time_pct"]) == (3, 0.0)  # by the due date
    set_window(MIXED)
    assert row()["pm_on_time_pct"] == pytest.approx(200 / 3) and not row()["pm_meets"]
    set_window(W.Windows(W.Window(K.DAYS_AFTER, 15), W.Window(K.DAYS_AFTER, 15)))
    assert row()["pm_on_time_pct"] == 100.0 and row()["pm_meets"]
    r = report_tech(TODAY)
    assert r["window"] == {"is_default": False, "words": "within 15 days after the due date"}


def test_the_technician_report_reads_settings_once_and_says_the_window(ctx, techs, client, signed_in, freeze_today):
    with CaptureQueriesContext(connection) as q:
        report_tech(TODAY)
    assert sum("facilitysettings" in x["sql"] for x in q.captured_queries) == 1
    freeze_today(TODAY)
    signed_in("director")
    assert "a PM counts as on time" not in client.get("/reports/tech/").content.decode()
    set_window(MIXED)
    body = client.get("/reports/tech/").content.decode()
    assert ("a PM counts as on time when it is done by the end of the due month for life support and high risk, within 14 days after the "
            "due date for medium and low risk (Settings).") in body


# --- the custom report's PM on time ------------------------------------------------------------------------------------------

DUES = [date(2026, 1, 31), date(2026, 2, 28), date(2026, 11, 10), date(2026, 12, 31), date(2027, 1, 3)]
AFTER = [0, 1, 14, 15, 29, 31, 45, 46, None]
RUN_DAY = date(2027, 1, 20)


@pytest.fixture
def grid(ctx, dept, vent_model, monitor_model):
    rows = []
    for model in (vent_model, monitor_model):
        device = Asset.objects.create(tag=f"G-{model.model}", device_model=model, department=dept)
        for due in DUES:
            for after in AFTER:
                done = None if after is None else due + timedelta(days=after)
                rows.append((pm(device, due, done=done).number, due, done, model.risk_class))
    return rows


@pytest.mark.parametrize("w", PAIRS, ids=pair_id)
def test_the_custom_column_follows_the_window(grid, w):
    set_window(w)
    got = {row[0]: row[1] for row in custom.run(custom.clean_definition("work_orders", ["number", "pm_on_time"]), RUN_DAY)["rows"]}
    for number, due, done, rc in grid:
        end = w.end(due, rc)
        expected = (done <= end) if done else (False if end < RUN_DAY else None)  # open: no once its window closed, else empty
        assert got[number] is expected, (number, due, done, rc)
    # The grouped share for each month agrees with the PM completion rate for the same days and window.
    for y, m in ((2026, 1), (2026, 2), (2026, 11), (2026, 12), (2027, 1)):
        start, end = month_bounds(y, m)
        kpi = pm_on_time_rate(start, end, RUN_DAY, w=w)
        r = custom.run(custom.clean_definition("work_orders", ["pm_on_time"], {"type": ["pm"], "date": {"field": "due", "period": "custom",
                                               "from": start.isoformat(), "to": end.isoformat()}}, "type"), RUN_DAY)
        share = r["rows"][0][2] if r["rows"] else None
        assert share == (pytest.approx(kpi["rate"]) if kpi["due"] else None), (y, m, kpi)


def test_a_run_reads_the_window_once_and_only_when_it_shows_pm_on_time(grid):
    set_window(MIXED)

    def settings_reads(columns, group_by=""):
        with CaptureQueriesContext(connection) as q:
            custom.run(custom.clean_definition("work_orders", columns, {}, group_by), RUN_DAY)
        return sum("facilitysettings" in x["sql"] for x in q.captured_queries)

    assert settings_reads(["number", "pm_on_time", "status"]) == 1
    assert settings_reads(["pm_on_time"], group_by="status") == 1
    assert settings_reads(["number", "status"]) == 0


def test_the_builder_explains_the_pm_on_time_column(client, signed_in):
    signed_in("director")
    body = client.get("/reports/custom/new/?source=work_orders").content.decode()
    assert "PM on time counts as the Overview does, by the facility's PM completion window in Settings" in body
    devices = client.get("/reports/custom/fields/?source=devices", HTTP_HX_REQUEST="true").content.decode()
    assert "Columns" in devices and "PM on time counts" not in devices


def test_on_time_words():
    assert on_time_words(W.DEFAULT) == "by the due date"
    assert on_time_words(W.Windows(W.Window(K.NEXT_MONTH), W.Window(K.NEXT_MONTH))) == "by the end of the month after the due month"
    assert on_time_words(W.Windows(W.Window(), W.Window(K.DAYS_AFTER, 1))) == ("by the due date for life support and high risk, within 1 day "
                                                                              "after the due date for medium and low risk")
