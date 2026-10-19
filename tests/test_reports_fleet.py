"""Fleet reports (slice 7): PM compliance summary, reliability by model, replacement planning. Math, tenant isolation, rendering, empty tenant."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from csvutil import csv_text
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.capital.priority import version_words
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.reports.fleet import REPAIR_RATE_FACTOR, REPLACE_COLUMNS, report_compliance, report_mtbf, report_replace
from apps.reports.services import run_report
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, PartLine, WorkOrder
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 9, 28)
WINDOW_START = TODAY - timedelta(days=182)  # the first day of the trailing window (Mar 30, 2026); the day before is outside it


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def wo(asset, type, opened, due=None, completed=None, cancelled=False, labor=None, part=None):
    w = create_work_order(asset=asset, type=type, priority="normal", problem="Test", opened_on=opened, due_on=due or opened + timedelta(days=5))
    if labor:
        LaborLine.objects.create(work_order=w, hours=labor[0], rate=labor[1])
    if part:
        PartLine.objects.create(work_order=w, description="Part", quantity=1, unit_cost=part)
    if cancelled:
        change_status(w, "cancelled", as_of=opened)
    elif completed:
        change_status(w, "in_progress", as_of=opened)
        change_status(w, "completed", as_of=completed)
    return w


def set_next_pm(asset, when):
    """Completing a PM reschedules the asset, so tests pin next_pm_on after the work orders exist."""
    Asset.objects.filter(pk=asset.pk).update(next_pm_on=when)


@pytest.fixture
def theirs(other_tenant):
    """The other hospital's fleet: an overdue life-support device with a repair and a PM this month. None of it may leak."""
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="Dräger", model="Evita V800", description="Ventilator", category="Ventilators",
                                        risk_class=RiskClass.LIFE_SUPPORT, expected_life_years=1, list_cost=50000)
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"), condition=1,
                                 installed_on=TODAY - timedelta(days=5000), next_pm_on=TODAY - timedelta(days=30))
        wo(a, "pm", date(2026, 9, 2), due=date(2026, 9, 10), completed=date(2026, 9, 9))
        wo(a, "repair", date(2026, 9, 12), completed=date(2026, 9, 14), labor=(3, 82))
        set_next_pm(a, TODAY - timedelta(days=30))
        return a


# --- compliance ------------------------------------------------------------------------------------------------


@pytest.fixture
def compliance_data(ctx, dept, vent_model, pump_model, vent, pump):
    vent2 = Asset.objects.create(tag="CE-10003", device_model=vent_model, department=dept)  # no PM scheduled: not overdue
    retired = Asset.objects.create(tag="CE-10004", device_model=vent_model, department=dept, status=AssetStatus.RETIRED, next_pm_on=date(2026, 1, 1))
    pump2 = Asset.objects.create(tag="CE-10005", device_model=pump_model, department=dept, next_pm_on=date(2026, 9, 25))
    pump3 = Asset.objects.create(tag="CE-10006", device_model=pump_model, department=dept, next_pm_on=TODAY)  # due today: not overdue yet
    wo(vent, "pm", date(2026, 9, 1), due=date(2026, 9, 10), completed=date(2026, 9, 9))  # on time
    wo(vent2, "pm", date(2026, 9, 2), due=date(2026, 9, 12), completed=date(2026, 9, 12))  # completed on the due date: on time
    wo(retired, "pm", date(2026, 9, 3), due=date(2026, 9, 14))  # a retired device's PM: not counted, like the device
    wo(pump, "pm", date(2026, 9, 5), due=date(2026, 9, 15), completed=date(2026, 9, 20))  # late
    wo(pump2, "pm", date(2026, 9, 20), due=date(2026, 9, 25))  # still open
    wo(pump2, "pm", date(2026, 9, 1), due=date(2026, 9, 12), cancelled=True)  # not counted
    wo(pump, "pm", date(2026, 9, 26), due=date(2026, 10, 2))  # next month
    wo(vent, "repair", date(2026, 9, 3), due=date(2026, 9, 8), completed=date(2026, 9, 9))  # not a PM
    set_next_pm(vent, date(2026, 9, 20))  # overdue
    set_next_pm(vent2, None)  # completing its PM scheduled the next one; keep it unscheduled
    set_next_pm(pump, date(2026, 10, 15))
    set_next_pm(pump2, date(2026, 10, 25))
    return {"vent": vent, "vent2": vent2, "pump": pump, "pump2": pump2, "pump3": pump3, "retired": retired}


def test_compliance_math_by_risk_class(compliance_data):
    r = report_compliance(TODAY)
    by = {c["key"]: c for c in r["classes"]}
    assert [c["key"] for c in r["classes"]] == ["life_support", "high", "medium", "low"]
    ls = by["life_support"]
    assert (ls["devices"], ls["due"], ls["completed"], ls["on_time"], ls["overdue"]) == (2, 2, 2, 2, 1)  # the retired device's PM is not due here
    assert ls["compliance_pct"] == 50.0 and ls["target_pct"] == 100 and not ls["meets"]
    hi = by["high"]
    assert (hi["devices"], hi["due"], hi["completed"], hi["on_time"], hi["overdue"]) == (3, 2, 1, 0, 0)  # due today is not overdue
    assert hi["compliance_pct"] == 100.0 and hi["target_pct"] == 100 and hi["meets"]
    for key in ("medium", "low"):
        assert by[key]["devices"] == 0 and by[key]["compliance_pct"] == 100.0 and by[key]["target_pct"] == 95 and by[key]["meets"]
    assert r["month_label"] == "September 2026" and r["today"] == TODAY
    assert r["columns"][0] == "Risk class" and r["rows"][0] == ["Life support", 2, 2, 2, 2, 1, 50.0, 100]
    assert r["rows"][1][0] == "High" and len(r["rows"]) == 4


def test_compliance_pm_on_the_due_date_is_on_time_and_the_day_after_is_not(compliance_data):
    """The rule is completed_on <= due_on: vent2's PM (done on its due date) counts, the pump's (five days late) does not."""
    by = {c["key"]: c for c in report_compliance(TODAY)["classes"]}
    assert by["life_support"]["on_time"] == 2 and by["high"]["on_time"] == 0


def test_compliance_next_pm_today_is_not_overdue_but_yesterday_is(compliance_data):
    """The rule is next_pm_on < today: pump3 (due today) is compliant; move it back a day and it is overdue."""
    assert {c["key"]: c["overdue"] for c in report_compliance(TODAY)["classes"]}["high"] == 0
    set_next_pm(compliance_data["pump3"], TODAY - timedelta(days=1))
    hi = {c["key"]: c for c in report_compliance(TODAY)["classes"]}["high"]
    assert hi["overdue"] == 1 and hi["compliance_pct"] == pytest.approx(200 / 3)


def test_compliance_counts_a_missing_device_as_overdue(compliance_data, dept, vent_model):
    """A device marked missing stays active and is never compliant, even with a PM scheduled in the future (the mock's rule)."""
    Asset.objects.create(tag="CE-10007", device_model=vent_model, department=dept, status=AssetStatus.MISSING, next_pm_on=TODAY + timedelta(days=60))
    ls = report_compliance(TODAY)["classes"][0]
    assert (ls["devices"], ls["overdue"]) == (3, 2) and ls["compliance_pct"] == pytest.approx(100 / 3)


def test_compliance_ignores_a_retired_devices_pm_this_month(compliance_data):
    """PMs due this month are counted on active devices only, so Devices, Overdue, and the PM columns describe the same fleet."""
    ls = report_compliance(TODAY)["classes"][0]
    assert ls["due"] == 2
    Asset.objects.filter(pk=compliance_data["retired"].pk).update(status=AssetStatus.IN_SERVICE)
    ls = report_compliance(TODAY)["classes"][0]
    assert (ls["devices"], ls["due"], ls["overdue"]) == (3, 3, 2)


def test_compliance_ignores_other_tenants(ctx, theirs, vent):
    set_next_pm(vent, TODAY + timedelta(days=10))
    r = run_report("compliance", TODAY)
    ls = r["classes"][0]
    assert (ls["devices"], ls["due"], ls["overdue"], ls["compliance_pct"]) == (1, 0, 0, 100.0)


def test_compliance_renders_with_chips_targets_and_trend(client, signed_in, compliance_data, freeze_today):
    freeze_today(TODAY)
    signed_in("director")
    r = client.get("/reports/compliance/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Fleet as of Sep 28, 2026 · PMs due in September 2026" in body
    assert '<span class="chip crit">Life support</span>' in body and '<span class="chip warn">High</span>' in body
    assert '<td class="num down"><b>50.0%</b></td><td class="muted">100%</td>' in body
    assert '<td class="num up"><b>100.0%</b></td><td class="muted">95%</td>' in body
    assert '<td class="num down">1</td>' in body  # the overdue count is red
    assert ("Devices marked missing are still counted as active and are never counted as compliant, except a new device waiting for its incoming "
            "inspection, whose PMs have not started.") in body and "PM completion rate by month" in body
    assert 'aria-label="PM completion rate by month"' in body and "Target 95%" in body
    chart = r.context["p"]["chart"]
    assert chart["paths"][0]["name"] == "All devices" and chart["xlabels"][0]["label"] == "Oct '25"  # 12 months ending at the report's month
    csv = csv_text(client.get("/reports/compliance.csv"))
    assert csv.startswith("Risk class,Devices,PMs due this month,Completed,On time,Overdue now,Current compliance %,Target %")


# --- mtbf --------------------------------------------------------------------------------------------------------


@pytest.fixture
def mtbf_data(ctx, dept, vent_model, pump_model, vent, pump):
    Asset.objects.create(tag="CE-10003", device_model=vent_model, department=dept)
    Asset.objects.create(tag="CE-10005", device_model=pump_model, department=dept)
    Asset.objects.create(tag="CE-10006", device_model=pump_model, department=dept)
    old_model = DeviceModel.objects.create(manufacturer="Acme", model="Mon-1", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
    old = Asset.objects.create(tag="CE-10099", device_model=old_model, department=dept, status=AssetStatus.RETIRED)
    wo(vent, "repair", date(2026, 9, 1), completed=date(2026, 9, 5), labor=(2, 82), part=100)  # turnaround 4, cost 264
    wo(vent, "repair", date(2026, 8, 1))  # open: counts as a failure, not in the averages
    wo(vent, "repair", WINDOW_START)  # opened on the first day of the window: counted
    wo(vent, "repair", WINDOW_START - timedelta(days=1))  # the day before the window: not counted
    wo(vent, "repair", date(2026, 3, 1), completed=date(2026, 3, 3), labor=(9, 82))  # before the window
    wo(vent, "repair", date(2026, 9, 10), cancelled=True)  # not a failure
    wo(vent, "pm", date(2026, 9, 10), completed=date(2026, 9, 11), labor=(9, 82))  # not a repair
    wo(pump, "repair", date(2026, 9, 10), completed=date(2026, 9, 12))  # turnaround 2, no cost
    wo(old, "repair", date(2026, 9, 15), completed=date(2026, 9, 16), labor=(1, 82))  # nothing in service: no rate
    return {"vent_model": vent_model, "pump_model": pump_model, "old_model": old_model}


def test_mtbf_math_and_ranking(mtbf_data):
    r = report_mtbf(TODAY)
    assert r["since"] == date(2026, 3, 30) and r["total_models"] == 3
    v, p, o = r["models"]
    assert v["device_model"] == mtbf_data["vent_model"] and (v["in_service"], v["repairs"]) == (2, 3)  # the window's first day counts, the day before does not
    assert v["rate"] == pytest.approx(3 / 2 * REPAIR_RATE_FACTOR) == 3.0 and v["mtbf_days"] == pytest.approx(2 * 182 / 3) and v["flagged"]
    assert v["turnaround"] == 4.0 and v["cost"] == 264.0
    assert p["device_model"] == mtbf_data["pump_model"] and (p["in_service"], p["repairs"]) == (3, 1)
    assert p["rate"] == pytest.approx(1 / 3 * REPAIR_RATE_FACTOR) and p["mtbf_days"] == pytest.approx(546.0) and not p["flagged"]
    assert p["turnaround"] == 2.0 and p["cost"] == 0.0
    assert o["device_model"] == mtbf_data["old_model"] and o["in_service"] == 0 and o["rate"] is None and o["mtbf_days"] is None and not o["flagged"]
    assert o["turnaround"] == 1.0 and o["cost"] == 82.0
    assert r["columns"][:3] == ["Manufacturer", "Model", "Device"]
    # slice 33: repairs created without a finding (and open ones) are failures, so failures equal repairs here
    assert (v["failures"], p["failures"], o["failures"]) == (3, 1, 1)
    assert r["rows"][0] == ["Hamilton Medical", "Hamilton-G5", "ICU ventilator", 2, 3, 3, 3.0, 121, 4.0, 264.0]
    assert r["rows"][2][6] is None and r["rows"][2][7] is None


def test_mtbf_one_repair_on_two_devices_is_the_boundary(ctx, dept, vent_model, vent, client, signed_in, freeze_today):
    """The mock's x2: one repair on two devices in six months is exactly one per device per year, which is not above one and not flagged,
    and its MTBF is the 364 device-days the two devices contributed."""
    Asset.objects.create(tag="CE-10003", device_model=vent_model, department=dept)
    wo(vent, "repair", date(2026, 9, 1), completed=date(2026, 9, 3))
    m = report_mtbf(TODAY)["models"][0]
    assert m["rate"] == 1.0 and not m["flagged"] and m["mtbf_days"] == 364.0
    freeze_today(TODAY)
    signed_in("director")
    body = client.get("/reports/mtbf/").content.decode()
    assert '<td class="num">2</td><td class="num">1</td><td class="num">1</td><td class="num">1.00</td><td class="num">364</td>' in body
    assert "num down" not in body


def test_mtbf_keeps_the_top_twelve(ctx, dept):
    for i in range(14):
        dm = DeviceModel.objects.create(manufacturer="M", model=f"X{i:02d}", description="D", category="C")
        a = Asset.objects.create(tag=f"CE-2{i:04d}", device_model=dm, department=dept)
        for _ in range(i + 1):  # X13 fails most often
            wo(a, "repair", date(2026, 9, 20))
    r = report_mtbf(TODAY)
    assert r["total_models"] == 14 and len(r["models"]) == 12 and len(r["rows"]) == 14  # the CSV keeps every model
    assert [row[1] for row in r["rows"][-2:]] == ["X01", "X00"]
    assert r["models"][0]["device_model"].model == "X13" and r["models"][-1]["device_model"].model == "X02"


def test_mtbf_query_count_does_not_grow_with_repairs(mtbf_data, ctx, dept, pump_model, monkeypatch):
    """Turnaround and cost are aggregated in SQL: six queries for three models, still six with many more repairs, lines, and models,
    and no work order is instantiated along the way (the dates come from a values() pass, the costs from grouped sums over the lines)."""

    def no_work_orders(cls, *args, **kwargs):
        raise AssertionError("report_mtbf must not instantiate work orders")

    def run():
        with monkeypatch.context() as m:
            m.setattr(WorkOrder, "from_db", classmethod(no_work_orders))
            with CaptureQueriesContext(connection) as captured:
                return report_mtbf(TODAY), len(captured)

    before, small = run()
    for i in range(8):
        dm = DeviceModel.objects.create(manufacturer="M", model=f"X{i:02d}", description="D", category="C")
        a = Asset.objects.create(tag=f"CE-2{i:04d}", device_model=dm, department=dept)
        for j in range(3):
            wo(a, "repair", date(2026, 9, 1 + j), completed=date(2026, 9, 3 + j), labor=(1 + j, 82), part=50 * j)
    after, large = run()
    assert after["total_models"] == 11 and small == large <= 6
    x = next(m for m in after["models"] if m["device_model"].model == "X07")
    assert x["turnaround"] == 2.0 and x["cost"] == pytest.approx((82 + 164 + 246 + 50 + 100) / 3)  # each line summed once per model
    assert [m["device_model"] for m in before["models"]] == [m["device_model"] for m in after["models"] if m["device_model"].manufacturer != "M"]


def test_mtbf_ignores_other_tenants(ctx, theirs, vent):
    wo(vent, "repair", date(2026, 9, 1), completed=date(2026, 9, 5))
    r = run_report("mtbf", TODAY)
    assert r["total_models"] == 1 and r["models"][0]["device_model"] == vent.device_model and r["models"][0]["in_service"] == 1


def test_mtbf_renders_the_table_and_hint(client, signed_in, mtbf_data, freeze_today):
    freeze_today(TODAY)
    signed_in("director")
    r = client.get("/reports/mtbf/")
    assert r.status_code == 200
    body = r.content.decode()
    assert ('<td class="two">Hamilton Medical Hamilton-G5<small>ICU ventilator</small></td><td class="num">2</td><td class="num">3</td>'
            '<td class="num">3</td><td class="num down">3.00</td><td class="num">121</td><td class="num">4.0 d</td><td class="num">$264</td>') in body
    assert '<td class="num">0.67</td><td class="num">546</td><td class="num">2.0 d</td><td class="num">$0</td>' in body
    assert '<td class="num">—</td><td class="num">—</td><td class="num">1.0 d</td><td class="num">$82</td>' in body
    assert "Ranked by failures per device per year; repairs opened since Mar 30, 2026." in body and "showing" not in body
    assert "The yearly rate doubles the six-month count per device in service" in body
    assert "What was found is recorded on 0 of the 3 repairs completed in this window: the others count as failures." in body  # slice 33
    csv = csv_text(client.get("/reports/mtbf.csv"))
    assert csv.startswith("Manufacturer,Model,Device,In service,\"Repairs, 6 mo\",\"Failures, 6 mo\",Failures per device per year,MTBF days,"
                          "Avg turnaround days,Avg repair cost")
    assert csv.splitlines()[1] == "Hamilton Medical,Hamilton-G5,ICU ventilator,2,3,3,3.00,121,4.00,264.00"


def test_mtbf_renders_the_empty_state(client, signed_in, vent):
    signed_in("director")
    assert "No repairs opened in the last 6 months." in client.get("/reports/mtbf/").content.decode()


# --- replace -----------------------------------------------------------------------------------------------------
# Slice 36: the report reads the replacement priority rule (apps.capital.priority; its body is pinned in tests/test_capital_priority.py).
# Here: the rows' contract (the CSV, the email attachment, the API), the sections' data, the screen, and a fixed number of queries.

VENT_INSTALLED = date(2014, 9, 28)  # 12.0 years before TODAY
SCALE_INSTALLED = date(2024, 9, 28)  # 730 days before TODAY


@pytest.fixture
def replace_data(ctx, dept, vent_model, pump_model, vent, pump):
    Asset.objects.filter(pk=vent.pk).update(installed_on=VENT_INSTALLED, condition=1)  # 12 of 10 years, poor (never scored)
    Asset.objects.filter(pk=pump.pk).update(installed_on=None, condition=5)  # no install date: not ranked
    wo(vent, "pm", date(2022, 1, 3), completed=date(2022, 1, 3))  # the facility's first work order: every window is the full 36 months
    for i in range(3):
        wo(vent, "repair", date(2026, 9, 1 + i), completed=date(2026, 9, 5 + i), labor=(2, 100))  # failures, no finding recorded
    wo(vent, "repair", date(2026, 9, 10), cancelled=True)  # not a failure
    no_list = DeviceModel.objects.create(manufacturer="Acme", model="Z", description="Scale", category="Scales", expected_life_years=0, list_cost=0)
    Asset.objects.create(tag="CE-10003", device_model=no_list, department=dept, installed_on=SCALE_INSTALLED, acquisition_cost=1000, condition=3)
    Asset.objects.create(tag="CE-10004", device_model=pump_model, department=dept, status=AssetStatus.RETIRED, installed_on=date(2000, 1, 1), condition=1)
    return vent


def test_replace_rows_rank_on_the_rule_with_unknowns_blank(replace_data):
    r = report_replace(TODAY)
    assert r["scored_count"] == 3 and r["ranked_count"] == 2 and len(r["rows"]) == 3
    assert [p.asset.tag for p in r["top"]] == ["CE-10001", "CE-10003"] and [p.asset.tag for p in r["unranked"]] == ["CE-10002"]
    assert r["columns"] == REPLACE_COLUMNS and r["columns"][:11] == [
        "Asset tag", "Device", "Manufacturer", "Model", "Location", "Age years", "Expected life years", "Failures, 36 mo", "Condition", "Score",
        "Est. replacement"]
    vent, scale, pump = r["rows"]
    assert vent == ["CE-10001", "ICU ventilator", "Hamilton Medical", "Hamilton-G5", "ICU", pytest.approx(12.0), 10, 3, 1, 31, Decimal("38000"), 25,
                    Decimal("24.0"), None, Decimal("6.7"), Decimal("0.0"), Decimal("0.0"), Decimal("1.0"), "", "Support not checked", "FY2026",
                    "list price", VENT_INSTALLED, Decimal("38000"), "yes"]
    assert scale == ["CE-10003", "Scale", "Acme", "Z", "ICU", pytest.approx(730 / 365.25), 1, 0, 3, 27, Decimal("1000"), 25, Decimal("30.0"), None,
                     Decimal("0.0"), Decimal("0.0"), Decimal("0.0"), Decimal("0.9"), "", "Support not checked", "FY2026", "acquisition cost",
                     SCALE_INSTALLED, Decimal("1000"), "yes"]
    assert pump == ["CE-10002", "Infusion pump", "BD", "Alaris 8015 PCU", "ICU", None, 8, 0, 5, 0, Decimal("3200"), 55, None, None, Decimal("0.0"),
                    Decimal("0.0"), Decimal("0.0"), Decimal("0.95"), "", "Support not checked", None, "list price", None, Decimal("3200"),
                    "no: no install date"]
    assert r["total"] == Decimal("39000") and r["version"] == version_words() and r["start"] == 1 and r["today"] == TODAY


def test_replace_sections_fleets_outlook_gaps(replace_data):
    r = report_replace(TODAY)
    fleets = [(f["device_model"].model, f["devices"], f["ranked"], f["past_life"], f["median_score"], f["total"]) for f in r["fleets"]]
    assert fleets == [("Hamilton-G5", 1, 1, 1, 31, Decimal("38000")), ("Z", 1, 1, 1, 27, Decimal("1000")), ("Alaris 8015 PCU", 1, 0, 0, None, Decimal("3200"))]
    vent = r["fleets"][0]
    assert vent["failures_per_device_year"] == pytest.approx(3 / (1096 / 365.25)) and vent["support"].words == "Support not checked"
    assert [(y["label"], y["devices"], y["total"]) for y in r["outlook"]["years"]] == [
        ("FY2026", 2, Decimal("39000")), ("FY2027", 0, 0), ("FY2028", 0, 0), ("FY2029", 0, 0), ("FY2030", 0, 0)]
    assert r["outlook"]["not_placed"] == 1 and r["outlook"]["later"] == 0
    assert [(g["key"], g["devices"], g["models"]) for g in r["gaps"]] == [
        ("no_install_date", 1, None), ("no_estimate", 0, None), ("life_source", 3, 3), ("support_unchecked", 3, 3)]


def test_the_outlook_follows_the_fiscal_year(replace_data, vent_model):
    from apps.equipment import services as eq
    from apps.facility import services as fac

    fac.update_settings(fiscal_year_start_month="7")
    eq.update_device_model(vent_model, support_ends_on=date(2029, 3, 1), support_checked_on=date(2026, 9, 1))
    Asset.objects.filter(tag="CE-10001").update(installed_on=date(2020, 1, 1))  # its life ends Jan 1, 2030
    r = report_replace(TODAY)
    assert r["start"] == 7 and [y["label"] for y in r["outlook"]["years"]] == ["FY2027", "FY2028", "FY2029", "FY2030", "FY2031"]
    assert [y["devices"] for y in r["outlook"]["years"]] == [1, 0, 1, 0, 0]  # the scale now; the ventilator by its support's end
    rows = {row[0]: row for row in r["rows"]}
    assert rows["CE-10001"][20] == "FY2029" and rows["CE-10003"][20] == "FY2027" and rows["CE-10002"][20] is None
    assert rows["CE-10001"][19] == "Support ends Mar 1, 2029"


def test_replace_lists_a_future_install_date_apart(ctx, dept, pump_model, client, signed_in, freeze_today):
    """A device dated ahead has no age yet: not ranked, its age a blank cell (never 0 or negative)."""
    Asset.objects.create(tag="CE-10010", device_model=pump_model, department=dept, installed_on=TODAY + timedelta(days=200), condition=5)
    r = report_replace(TODAY)
    assert r["top"] == [] and r["unranked"][0].asset.tag == "CE-10010" and r["rows"][0][5] is None and r["rows"][0][-1] == "no: no install date"
    assert r["unranked"][0].component("age").input == "install date after today"
    freeze_today(TODAY)
    signed_in("director")
    body = client.get("/reports/replace/").content.decode()
    assert "No device can be ranked yet" in body and "Not ranked: no install date" in body
    assert csv_text(client.get("/reports/replace.csv")).splitlines()[1].startswith("CE-10010,Infusion pump,BD,Alaris 8015 PCU,ICU,,8,,5,0,3200.00,")


def test_replace_shows_twelve_but_exports_everyone(ctx, dept, pump_model):
    for i in range(15):
        Asset.objects.create(tag=f"CE-3{i:04d}", device_model=pump_model, department=dept, installed_on=TODAY - timedelta(days=100 * i), condition=3)
    r = report_replace(TODAY)
    assert r["scored_count"] == 15 and len(r["rows"]) == 15 and len(r["top"]) == 12
    assert r["top"][0].asset.tag == "CE-30014" and [x[0] for x in r["rows"][-3:]] == ["CE-30001", "CE-30002", "CE-30000"]  # 1, 1 (by tag), 0
    assert r["total"] == 12 * Decimal("3200")


def test_replace_runs_the_same_queries_for_two_devices_or_twelve(ctx, dept, pump_model, vent_model):
    def count():
        with CaptureQueriesContext(connection) as q:
            report_replace(TODAY)
        return len(q.captured_queries)

    for i in range(2):
        a = Asset.objects.create(tag=f"CE-5{i:04d}", device_model=pump_model, department=dept, installed_on=TODAY - timedelta(days=900), condition=3)
        wo(a, "repair", TODAY - timedelta(days=30), completed=TODAY - timedelta(days=29), labor=(1, 80), part=40)
    small = count()
    for i in range(2, 12):
        a = Asset.objects.create(tag=f"CE-5{i:04d}", device_model=vent_model if i % 2 else pump_model, department=dept,
                                 installed_on=TODAY - timedelta(days=300 * i), condition=3)
        wo(a, "repair", TODAY - timedelta(days=30 + i), completed=TODAY - timedelta(days=29), labor=(1, 80), part=40)
        wo(a, "pm", TODAY - timedelta(days=60 + i), completed=TODAY - timedelta(days=59))
    assert count() == small


def test_replace_ignores_other_tenants(ctx, theirs, vent):
    r = run_report("replace", TODAY)
    assert r["scored_count"] == 1 and r["top"][0].asset.tag == "CE-10001"


def test_replace_renders_rows_that_open_the_drawer(client, signed_in, replace_data, freeze_today):
    freeze_today(TODAY)
    signed_in("director")
    r = client.get("/reports/replace/")
    assert r.status_code == 200
    body = r.content.decode()
    assert '<tr class="row" hx-get="/equipment/CE-10001/" hx-target="#drawer">' in body
    assert '<a class="tag" href="/equipment/CE-10001/">CE-10001</a> ICU ventilator<small>Hamilton Medical Hamilton-G5 · Suggested for FY2026</small>' in body
    assert '<td class="num down">12.0 of 10 yr</td>' in body and "<td>Support not checked</td>" in body
    assert '<td class="num"><b>31</b><small>25 points unknown</small></td>' in body and "$38,000" in body
    assert "for these 2: <b style=\"color:var(--ink)\">$39,000</b>" in body and "no markup" in body and "plus 5%" not in body
    assert version_words() in body and "The top 2 ranked are shown; the CSV carries all 3 devices of ours in use" in body
    assert "Fleets by model" in body and "Outlook by fiscal year" in body and 'title="FY2026: Jan 1, 2026 to Dec 31, 2026"' in body
    assert "What the scores do not know" in body and "No install date: 1 device" in body
    assert "Not ranked: no install date" in body and "twelve" not in body
    csv = csv_text(client.get("/reports/replace.csv"))
    assert csv.startswith('Asset tag,Device,Manufacturer,Model,Location,Age years,Expected life years,"Failures, 36 mo",Condition,Score,'
                          "Est. replacement,Points unknown,Age points,Support points,Failure points,Service cost points,Recall points,Multiplier,"
                          "Flags,Support,Suggested fiscal year,Estimate source,Installed on,Acquisition cost,Ranked")
    assert csv.splitlines()[1] == ("CE-10001,ICU ventilator,Hamilton Medical,Hamilton-G5,ICU,12.00,10,3,1,31,38000.00,25,24.0,,6.7,0.0,0.0,1.0,,"
                                   "Support not checked,FY2026,list price,2014-09-28,38000.00,yes")


def test_the_api_serves_the_csvs_rows(client, signed_in, replace_data, monkeypatch):
    monkeypatch.setattr("apps.api.views_reports._today", lambda: TODAY)
    signed_in("director")
    data = client.get("/api/v1/reports/replace/").json()
    assert data["columns"] == REPLACE_COLUMNS
    assert data["rows"][0] == ["CE-10001", "ICU ventilator", "Hamilton Medical", "Hamilton-G5", "ICU", 12.0, 10, 3, 1, 31, 38000.0, 25, 24.0, None,
                               6.7, 0.0, 0.0, 1.0, "", "Support not checked", "FY2026", "list price", "2014-09-28", 38000.0, "yes"]
    assert data["rows"][2][5] is None and data["rows"][2][-1] == "no: no install date"


def test_replace_rows_are_plain_without_equipment_view(client, signed_in, replace_data, freeze_today):
    from apps.accounts.models import Level, Role

    freeze_today(TODAY)
    role = Role.objects.create(name="Reports only", slug="reports_only")
    role.set_levels({"reports": Level.VIEW})
    signed_in("reports_only")
    body = client.get("/reports/replace/").content.decode()
    assert 'hx-target="#drawer"' not in body and 'href="/equipment/CE-10001/"' not in body and "CE-10001 ICU ventilator" in body


# --- empty tenant ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["compliance", "mtbf", "replace"])
def test_fleet_reports_render_for_an_empty_tenant(client, signed_in, ctx, key):
    r = run_report(key, TODAY)
    assert r["columns"] and (r["rows"] == [] or key == "compliance")
    signed_in("director")
    page = client.get(f"/reports/{key}/")
    assert page.status_code == 200
    body = page.content.decode()
    if key == "compliance":
        assert body.count('<b>100.0%</b>') == 4 and "down" not in body.split('<table>')[1].split("</table>")[0]
    elif key == "mtbf":
        assert "No repairs opened in the last 6 months." in body
    else:
        assert "No active devices." in body
    assert client.get(f"/reports/{key}.csv").status_code == 200
