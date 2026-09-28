"""Fleet reports (slice 7): PM compliance summary, reliability by model, replacement planning. Math, tenant isolation, rendering, empty tenant."""
from datetime import date, timedelta

import pytest

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.reports.fleet import replacement_score, report_compliance, report_mtbf, report_replace
from apps.reports.services import ANNUALIZE, run_report
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, PartLine
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 9, 28)


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
    Asset.objects.create(tag="CE-10004", device_model=vent_model, department=dept, status=AssetStatus.RETIRED, next_pm_on=date(2026, 1, 1))
    pump2 = Asset.objects.create(tag="CE-10005", device_model=pump_model, department=dept, next_pm_on=date(2026, 9, 25))
    wo(vent, "pm", date(2026, 9, 1), due=date(2026, 9, 10), completed=date(2026, 9, 9))  # on time
    wo(pump, "pm", date(2026, 9, 5), due=date(2026, 9, 15), completed=date(2026, 9, 20))  # late
    wo(pump2, "pm", date(2026, 9, 20), due=date(2026, 9, 25))  # still open
    wo(pump2, "pm", date(2026, 9, 1), due=date(2026, 9, 12), cancelled=True)  # not counted
    wo(pump, "pm", date(2026, 9, 26), due=date(2026, 10, 2))  # next month
    wo(vent, "repair", date(2026, 9, 3), due=date(2026, 9, 8), completed=date(2026, 9, 9))  # not a PM
    set_next_pm(vent, date(2026, 9, 20))  # overdue
    set_next_pm(pump, date(2026, 10, 15))
    set_next_pm(pump2, date(2026, 10, 25))
    return {"vent": vent, "vent2": vent2, "pump": pump, "pump2": pump2}


def test_compliance_math_by_risk_class(compliance_data):
    r = report_compliance(TODAY)
    by = {c["key"]: c for c in r["classes"]}
    assert [c["key"] for c in r["classes"]] == ["life_support", "high", "medium", "low"]
    ls = by["life_support"]
    assert (ls["devices"], ls["due"], ls["completed"], ls["on_time"], ls["overdue"]) == (2, 1, 1, 1, 1)
    assert ls["compliance_pct"] == 50.0 and ls["target_pct"] == 100 and not ls["meets"]
    hi = by["high"]
    assert (hi["devices"], hi["due"], hi["completed"], hi["on_time"], hi["overdue"]) == (2, 2, 1, 0, 0)
    assert hi["compliance_pct"] == 100.0 and hi["target_pct"] == 100 and hi["meets"]
    for key in ("medium", "low"):
        assert by[key]["devices"] == 0 and by[key]["compliance_pct"] == 100.0 and by[key]["target_pct"] == 95 and by[key]["meets"]
    assert r["month_label"] == "September 2026" and r["today"] == TODAY
    assert r["columns"][0] == "Risk class" and r["rows"][0] == ["Life support", 2, 1, 1, 1, 1, 50.0, 100]
    assert r["rows"][1][0] == "High" and len(r["rows"]) == 4


def test_compliance_ignores_other_tenants(ctx, theirs, vent):
    set_next_pm(vent, TODAY + timedelta(days=10))
    r = run_report("compliance", TODAY)
    ls = r["classes"][0]
    assert (ls["devices"], ls["due"], ls["overdue"], ls["compliance_pct"]) == (1, 0, 0, 100.0)


def test_compliance_renders_with_chips_targets_and_trend(client, signed_in, compliance_data):
    signed_in("director")
    r = client.get("/reports/compliance/")
    assert r.status_code == 200
    body = r.content.decode()
    assert '<span class="chip crit">Life support</span>' in body and '<span class="chip warn">High</span>' in body
    assert '<td class="num down"><b>50.0%</b></td><td class="muted">100%</td>' in body
    assert '<td class="num up"><b>100.0%</b></td><td class="muted">95%</td>' in body
    assert '<td class="num down">1</td>' in body  # the overdue count is red
    assert "Devices marked missing are still counted as active" in body and "PM completion rate by month" in body
    assert 'aria-label="PM completion rate by month"' in body and "Target 95%" in body
    assert r.context["p"]["chart"]["paths"][0]["name"] == "All devices"
    csv = client.get("/reports/compliance.csv").content.decode()
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
    assert v["device_model"] == mtbf_data["vent_model"] and (v["in_service"], v["repairs"]) == (2, 2)
    assert v["rate"] == pytest.approx(2 / 2 * ANNUALIZE) and v["mtbf_days"] == pytest.approx(182.0) and v["flagged"]
    assert v["turnaround"] == 4.0 and v["cost"] == 264.0
    assert p["device_model"] == mtbf_data["pump_model"] and (p["in_service"], p["repairs"]) == (3, 1)
    assert p["rate"] == pytest.approx(1 / 3 * ANNUALIZE) and p["mtbf_days"] == pytest.approx(546.0) and not p["flagged"]
    assert p["turnaround"] == 2.0 and p["cost"] == 0.0
    assert o["device_model"] == mtbf_data["old_model"] and o["in_service"] == 0 and o["rate"] is None and o["mtbf_days"] is None and not o["flagged"]
    assert o["turnaround"] == 1.0 and o["cost"] == 82.0
    assert r["columns"][:3] == ["Manufacturer", "Model", "Device"]
    assert r["rows"][0] == ["Hamilton Medical", "Hamilton-G5", "ICU ventilator", 2, 2, pytest.approx(ANNUALIZE), 182, 4.0, 264.0]
    assert r["rows"][2][5] is None and r["rows"][2][6] is None


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


def test_mtbf_ignores_other_tenants(ctx, theirs, vent):
    wo(vent, "repair", date(2026, 9, 1), completed=date(2026, 9, 5))
    r = run_report("mtbf", TODAY)
    assert r["total_models"] == 1 and r["models"][0]["device_model"] == vent.device_model and r["models"][0]["in_service"] == 1


def test_mtbf_renders_the_table_and_hint(client, signed_in, mtbf_data):
    signed_in("director")
    r = client.get("/reports/mtbf/")
    assert r.status_code == 200
    body = r.content.decode()
    assert ('<td class="two">Hamilton Medical Hamilton-G5<small>ICU ventilator</small></td><td class="num">2</td><td class="num">2</td>'
            '<td class="num down">2.01</td><td class="num">182</td><td class="num">4.0 d</td><td class="num">$264</td>') in body
    assert '<td class="num">0.67</td><td class="num">546</td><td class="num">2.0 d</td><td class="num">$0</td>' in body
    assert '<td class="num">—</td><td class="num">—</td><td class="num">1.0 d</td><td class="num">$82</td>' in body
    assert "Ranked by repairs per device per year; repairs opened since Mar 30, 2026." in body and "showing" not in body
    csv = client.get("/reports/mtbf.csv").content.decode()
    assert csv.startswith("Manufacturer,Model,Device,In service,\"Repairs, 6 mo\",Repairs per device per year,MTBF days,Avg turnaround days,Avg repair cost")


def test_mtbf_renders_the_empty_state(client, signed_in, vent):
    signed_in("director")
    assert "No repairs opened in the last 6 months." in client.get("/reports/mtbf/").content.decode()


# --- replace -----------------------------------------------------------------------------------------------------


def test_replacement_score_terms():
    assert replacement_score(None, 10, 0, 5) == 0.0
    assert replacement_score(12, 10, 3, 1) == pytest.approx(0.9)  # 1.2x life, repairs at the cap of 3, worst condition
    assert replacement_score(20, 10, 9, 1) == pytest.approx(1.0)  # age capped at 1.5x life, repairs capped at 3
    assert replacement_score(5, 10, 0, 3) == pytest.approx(0.5 * 0.5 / 1.5 + 0.2 * 0.5)


@pytest.fixture
def replace_data(ctx, dept, vent_model, pump_model, vent, pump):
    Asset.objects.filter(pk=vent.pk).update(installed_on=TODAY - timedelta(days=round(12 * 365.25)), condition=1)  # 12 of 10 years, poor
    Asset.objects.filter(pk=pump.pk).update(installed_on=None, condition=5)  # no install date, excellent: scores 0
    for i in range(3):
        wo(vent, "repair", date(2026, 9, 1 + i), completed=date(2026, 9, 5 + i))
    wo(vent, "repair", date(2026, 1, 1))  # before the window
    wo(vent, "repair", date(2026, 9, 10), cancelled=True)
    no_list = DeviceModel.objects.create(manufacturer="Acme", model="Z", description="Scale", category="Scales", expected_life_years=0, list_cost=0)
    Asset.objects.create(tag="CE-10003", device_model=no_list, department=dept, installed_on=TODAY - timedelta(days=730), acquisition_cost=1000, condition=3)
    Asset.objects.create(tag="CE-10004", device_model=pump_model, department=dept, status=AssetStatus.RETIRED, installed_on=date(2000, 1, 1), condition=1)
    return vent


def test_replace_math_ranking_and_estimates(replace_data):
    r = report_replace(TODAY)
    assert r["scored_count"] == 3 and len(r["top"]) == 3 and len(r["rows"]) == 3
    v, z, p = r["top"]
    assert v["asset"].tag == "CE-10001" and v["age"] == pytest.approx(12.0, abs=0.01) and v["life"] == 10 and v["repairs"] == 3 and v["condition"] == 1
    assert v["score"] == pytest.approx(0.9) and v["score_pct"] == 90 and v["over_life"] and v["estimate"] == pytest.approx(38000 * 1.05) and not v["fallback"]
    assert z["asset"].tag == "CE-10003" and z["life"] == 1 and z["age"] == pytest.approx(2.0, abs=0.01) and z["over_life"]
    assert z["score"] == pytest.approx(0.5 + 0.1) and z["estimate"] == pytest.approx(1050.0) and z["fallback"]
    assert p["asset"].tag == "CE-10002" and p["age"] is None and p["score"] == 0.0 and p["score_pct"] == 0 and not p["over_life"]
    assert r["total"] == pytest.approx(38000 * 1.05 + 1050 + 3200 * 1.05) and r["fallback_count"] == 1
    assert r["columns"][0] == "Asset tag" and r["rows"][0][:5] == ["CE-10001", "ICU ventilator", "Hamilton Medical", "Hamilton-G5", "ICU"]
    assert r["rows"][0][6:] == [10, 3, 1, 90, pytest.approx(39900.0)] and r["rows"][2][5] is None


def test_replace_shows_twelve_but_exports_everyone(ctx, dept, pump_model):
    for i in range(15):
        Asset.objects.create(tag=f"CE-3{i:04d}", device_model=pump_model, department=dept, installed_on=TODAY - timedelta(days=100 * i), condition=3)
    r = report_replace(TODAY)
    assert r["scored_count"] == 15 and len(r["rows"]) == 15 and len(r["top"]) == 12
    assert r["top"][0]["asset"].tag == "CE-30014" and [x[0] for x in r["rows"][-2:]] == ["CE-30001", "CE-30000"]
    assert r["total"] == pytest.approx(12 * 3200 * 1.05)


def test_replace_ignores_other_tenants(ctx, theirs, vent):
    r = run_report("replace", TODAY)
    assert r["scored_count"] == 1 and r["top"][0]["asset"].tag == "CE-10001"


def test_replace_renders_rows_that_open_the_drawer(client, signed_in, replace_data):
    signed_in("director")
    r = client.get("/reports/replace/")
    assert r.status_code == 200
    body = r.content.decode()
    assert '<tr class="row" hx-get="/equipment/CE-10001/" hx-target="#drawer">' in body
    assert '<a class="tag" href="/equipment/CE-10001/">CE-10001</a> ICU ventilator<small>Hamilton Medical Hamilton-G5</small>' in body
    assert '<td class="num down">12.0 yr</td>' in body and '<td class="num">10 yr</td><td class="num">3</td><td>1 of 5</td>' in body
    assert '<td class="num"><b>90</b></td><td class="num">$39,900</td>' in body and '<td class="num">—</td>' in body
    assert "for these 3: <b style=\"color:var(--ink)\">$44,310</b>" in body and "1 device without a list price" in body
    assert "the CSV carries the full ranked list of 3 active devices" in body
    csv = client.get("/reports/replace.csv").content.decode()
    assert csv.startswith('Asset tag,Device,Manufacturer,Model,Location,Age years,Expected life years,"Repairs, 6 mo",Condition,Score,Est. replacement')
    assert csv.splitlines()[1].startswith("CE-10001,ICU ventilator,Hamilton Medical,Hamilton-G5,ICU,")


def test_replace_rows_are_plain_without_equipment_view(client, signed_in, replace_data):
    from apps.accounts.models import Level, Role

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
