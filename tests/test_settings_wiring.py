"""Settings wiring (slice 8): each facility setting reaches the screen the mock shows it on. The portal's callback rule and hotline,
the assignment and portal policy text on work orders, the Overview's targets and budget, the PM trend's target line, and the
compliance report's targets. Every test also changes the other tenant's settings to prove they never apply here."""
from datetime import date, timedelta

import pytest
from django.core.cache import cache

from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.facility import services as fs
from apps.reports.fleet import report_compliance
from apps.reports.services import overview_page
from apps.tenants.context import tenant_context
from apps.web.overview import kpi_tiles, pm_trend_chart
from apps.workorders.models import ServiceRequest
from apps.workorders.services import create_service_request, create_work_order

TODAY = date(2026, 9, 28)
HX = {"HTTP_HX_REQUEST": "true"}
HOTLINE_NOTE = "call the Clinical Engineering hotline at ext. 4400 in addition to submitting this form."
PLAIN_NOTE = "call the Clinical Engineering hotline in addition to submitting this form."


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()  # the portal's per-IP counter lives in the cache; keep each test's posts under the limit
    yield
    cache.clear()


def set_other(other_tenant, **fields):
    """Change the other tenant's settings; nothing here may pick them up."""
    with tenant_context(other_tenant):
        fs.update_settings(**fields)


def portal_post(client, asset, callback=""):
    return client.post("/r/riverside/", {"asset_tag": asset.tag, "department": str(asset.department_id), "problem": "Alarm will not clear",
                                         "urgency": "normal", "callback": callback})


# --- portal -----------------------------------------------------------------------------------------------------


def test_portal_requires_a_callback_by_default(client, ctx, vent, other_tenant):
    set_other(other_tenant, portal_require_callback=False)  # the other tenant's choice changes nothing here
    body = client.get("/r/riverside/").content.decode()
    assert '<label for="id_callback">Callback extension</label>' in body and "(optional)" not in body
    r = portal_post(client, vent)
    assert r.status_code == 200 and "This field is required." in r.content.decode()
    assert not ServiceRequest.objects.exists()
    r = portal_post(client, vent, callback="x2210")
    assert r.status_code == 302 and ServiceRequest.objects.get().callback == "x2210"


def test_portal_accepts_a_request_without_a_callback_once_the_setting_is_off(client, ctx, vent, other_tenant):
    fs.update_settings(portal_require_callback=False)
    set_other(other_tenant, portal_require_callback=True)
    body = client.get("/r/riverside/").content.decode()
    assert '<label for="id_callback">Callback extension (optional)</label>' in body
    r = portal_post(client, vent)
    sr = ServiceRequest.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/r/riverside/done/{sr.number}/" and sr.callback == ""
    assert "We will call: the unit" in client.get(r["Location"]).content.decode()


def test_portal_names_the_hotline_when_one_is_set(client, ctx, vent, other_tenant):
    set_other(other_tenant, portal_hotline="ext. 9999")
    assert PLAIN_NOTE in client.get("/r/riverside/").content.decode()  # without a hotline, today's sentence
    fs.update_settings(portal_hotline="ext. 4400")
    body = client.get("/r/riverside/").content.decode()
    assert HOTLINE_NOTE in body and "9999" not in body
    r = portal_post(client, vent, callback="x2210")
    done = client.get(r["Location"]).content.decode()
    assert "If the situation changes, call the shop at ext. 4400 and give your request number." in done and "9999" not in done


def test_portal_done_page_without_a_hotline_keeps_todays_sentence(client, ctx, vent, other_tenant):
    set_other(other_tenant, portal_hotline="ext. 9999")
    r = portal_post(client, vent, callback="x2210")
    done = client.get(r["Location"]).content.decode()
    assert "If the situation changes, call the shop and give your request number." in done and "9999" not in done


# --- work orders ---------------------------------------------------------------------------------------------------


def test_drawer_assignment_header_shows_the_tenants_policy(client, signed_in, ctx, vent, other_tenant):
    set_other(other_tenant, policy_assignment="Other hospital assignment rule")
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Low tidal volume alarm")
    signed_in("director")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "<h3>Assignment <span>Credentialed technicians first; overrides are flagged on the work order</span></h3>" in body
    assert "credentialed technicians first</span>" not in body and "Other hospital" not in body
    fs.update_settings(policy_assignment="Lead BMET assigns; credentialed technicians only")
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "<h3>Assignment <span>Lead BMET assigns; credentialed technicians only</span></h3>" in body


def test_work_orders_note_ends_with_the_portal_policy(client, signed_in, ctx, vent, dept, other_tenant):
    set_other(other_tenant, policy_portal="Other hospital triage rule")
    signed_in("technician")
    assert "waiting for assignment" not in client.get("/work-orders/").content.decode()
    create_service_request(asset=vent, department=dept, problem="Alarm", urgency="normal")
    body = client.get("/work-orders/").content.decode()
    assert "1 request from the service portal is waiting for assignment (policy: Triage within 30 minutes during shop hours). " in body
    assert ">Show it</a>" in body and "Other hospital" not in body
    fs.update_settings(policy_portal="ED requests triaged 7 a.m. to 7 p.m.")  # quoted as written: case (ED) and abbreviations kept
    create_service_request(asset=vent, department=dept, problem="Second alarm", urgency="normal")
    body = client.get("/work-orders/").content.decode()
    assert "2 requests from the service portal are waiting for assignment (policy: ED requests triaged 7 a.m. to 7 p.m.). " in body
    assert ">Show them</a>" in body


# --- Overview ------------------------------------------------------------------------------------------------------


def tile(tiles, label):
    return [text for text, _css in next(t for t in tiles if t["label"].startswith(label))["parts"]]


def test_overview_tiles_show_the_default_targets_and_no_budget_without_one(ctx, vent, other_tenant):
    set_other(other_tenant, target_pm_pct="80", repair_budget_monthly="99000")
    data = overview_page(TODAY.year, TODAY.month, TODAY)
    assert data["targets"] == fs.kpi_targets() and data["targets"]["pm_on_time"] == 95.0
    tiles = kpi_tiles(data)
    assert tile(tiles, "PM completion on time")[-1] == "target 95%"
    assert tile(tiles, "Life-support PM completion")[-1] == "target 100%"
    assert tile(tiles, "Fleet uptime")[-1] == "target 99.5%"
    assert tile(tiles, "Mean time to repair")[-1] == "target 3.0"
    assert tile(tiles, "Repair spend")[-1] == "labor and parts" and not any("budget" in p for p in tile(tiles, "Repair spend"))


def test_overview_tiles_follow_the_settings(ctx, vent, other_tenant):
    set_other(other_tenant, target_pm_pct="80", target_uptime_pct="95", target_mttr_days="9", repair_budget_monthly="1000")
    fs.update_settings(target_pm_pct="97.5", target_uptime_pct="99.9", target_mttr_days="2.5", repair_budget_monthly="52000")
    tiles = kpi_tiles(overview_page(TODAY.year, TODAY.month, TODAY))
    assert tile(tiles, "PM completion on time")[-1] == "target 97.5%"
    assert tile(tiles, "Life-support PM completion")[-1] == "target 100%"  # life support stays at 100 whatever the policy target
    assert tile(tiles, "Fleet uptime")[-1] == "target 99.9%"
    assert tile(tiles, "Mean time to repair")[-1] == "target 2.5"
    assert tile(tiles, "Repair spend")[-2:] == ["labor and parts", "budget $52.0k per month"]


def test_overview_page_renders_the_targets_budget_and_trend_line(client, signed_in, ctx, vent, other_tenant):
    set_other(other_tenant, target_pm_pct="80")
    fs.update_settings(target_pm_pct="90", repair_budget_monthly="52000")
    signed_in("director")
    r = client.get("/")
    body = r.content.decode()
    assert r.status_code == 200 and "target 90%" in body and "budget $52.0k per month" in body and "Target 90%" in body
    assert r.context["pm_chart"]["target_label"] == "Target 90%" and "Target 80%" not in body


# --- PM trend chart ------------------------------------------------------------------------------------------------


def series(rate=100.0):
    return [{"year": 2026, "month": m, "due": 4, "on_time": 4, "rate": rate} for m in range(1, 13)]


def test_pm_trend_chart_draws_the_target_line_at_the_setting():
    default = pm_trend_chart(series(), series())
    assert default["target_label"] == "Target 95%" and default["grid"][0]["label"] == "90%"  # the mock's 90 to 100 axis is unchanged
    c = pm_trend_chart(series(), series(), target=97.5)
    top, bottom = c["grid"][-1]["y"], c["grid"][0]["y"]
    assert c["target_label"] == "Target 97.5%" and c["grid"][0]["label"] == "90%"
    assert c["target_y"] == pytest.approx(bottom - (bottom - top) * 0.75, abs=0.1)  # 97.5 is three quarters of the way up 90..100
    assert c["target_y"] < default["target_y"]  # a higher target sits higher on the chart


@pytest.mark.parametrize("target, floor", [(95.0, "90%"), (90.0, "85%"), (92.5, "90%"), (50.0, "45%")])
def test_pm_trend_chart_keeps_the_target_line_inside_the_axis(target, floor):
    c = pm_trend_chart(series(), series(), target=target)
    top, bottom = c["grid"][-1]["y"], c["grid"][0]["y"]
    assert c["grid"][0]["label"] == floor and top <= c["target_y"] < bottom  # above the floor, never on or below it
    assert c["target_label"] == f"Target {target:g}%"


def test_pm_trend_chart_floor_still_widens_for_a_low_month():
    c = pm_trend_chart(series(72.0), series(), target=95.0)
    assert c["grid"][0]["label"] == "70%"


# --- PM compliance report -------------------------------------------------------------------------------------------


@pytest.fixture
def medium_fleet(ctx, dept, vent, pump):
    """Ten medium-risk devices with one overdue (90% compliant) and two low-risk devices, both current (100%)."""
    medium = DeviceModel.objects.create(manufacturer="Welch Allyn", model="Connex 6000", description="Vital signs monitor", category="Monitors",
                                        risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    low = DeviceModel.objects.create(manufacturer="Stryker", model="S3 Bed", description="Medical bed", category="Beds", risk_class=RiskClass.LOW,
                                     oem_pm_interval_months=12)
    for i in range(10):
        Asset.objects.create(tag=f"CE-2{i:04d}", device_model=medium, department=dept,
                             next_pm_on=TODAY - timedelta(days=3) if i == 0 else TODAY + timedelta(days=30))
    for i in range(2):
        Asset.objects.create(tag=f"CE-3{i:04d}", device_model=low, department=dept, next_pm_on=TODAY + timedelta(days=30))
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=TODAY + timedelta(days=10))
    Asset.objects.filter(pk=pump.pk).update(next_pm_on=TODAY + timedelta(days=10))


def classes():
    return {c["key"]: c for c in report_compliance(TODAY)["classes"]}


def test_compliance_targets_follow_the_pm_target_for_medium_and_low_only(medium_fleet, other_tenant):
    set_other(other_tenant, target_pm_pct="50")
    by = classes()
    assert [by[k]["target_pct"] for k in ("life_support", "high", "medium", "low")] == [100.0, 100.0, 95.0, 95.0]
    assert by["medium"]["compliance_pct"] == pytest.approx(90.0) and not by["medium"]["meets"]
    assert by["low"]["meets"] and by["life_support"]["meets"] and by["high"]["meets"]
    fs.update_settings(target_pm_pct="90")
    by = classes()
    assert [by[k]["target_pct"] for k in ("life_support", "high", "medium", "low")] == [100.0, 100.0, 90.0, 90.0]
    assert by["medium"]["meets"]  # 90% now meets the policy target
    r = report_compliance(TODAY)
    assert r["policy_target_pct"] == 90.0 and r["rows"][2][-1] == 90.0 and r["rows"][0][-1] == 100.0


def test_compliance_life_support_and_high_stay_at_100_whatever_the_policy(ctx, dept, vent, pump, other_tenant):
    set_other(other_tenant, target_pm_pct="100")
    fs.update_settings(target_pm_pct="50")
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=TODAY - timedelta(days=1))  # 1 of 1 life-support devices overdue
    by = classes()
    assert by["life_support"]["target_pct"] == 100.0 and not by["life_support"]["meets"]
    assert by["high"]["target_pct"] == 100.0 and by["medium"]["target_pct"] == 50.0


def test_compliance_report_renders_the_policy_target(client, signed_in, medium_fleet, other_tenant, freeze_today):
    freeze_today(TODAY)
    set_other(other_tenant, target_pm_pct="80")
    fs.update_settings(target_pm_pct="97.5")
    signed_in("director")
    r = client.get("/reports/compliance/")
    body = r.content.decode()
    assert r.status_code == 200
    assert '<td class="num down"><b>90.0%</b></td><td class="muted">97.5%</td>' in body  # medium misses 97.5
    assert '<td class="num up"><b>100.0%</b></td><td class="muted">100%</td>' in body  # life support and high stay at 100
    assert "other equipment follows the hospital policy target of 97.5% with completion within the due month" in body
    assert "Target 97.5%" in body and "Target 80%" not in body and "target of 80%" not in body
    assert r.context["p"]["chart"]["target_label"] == "Target 97.5%"
    csv = client.get("/reports/compliance.csv").content.decode().splitlines()
    assert csv[3].startswith("Medium,10,") and csv[3].endswith(",90.00,97.50") and csv[1].endswith(",100.00")


# --- review fixes -------------------------------------------------------------------------------------------------------------

def test_department_links_prefill_the_department(client, ctx, vent, other_tenant):
    from apps.equipment.models import Department

    med = Department.objects.create(name="Med/Surg 3E")
    icu_upper, icu_lower = Department.objects.get(name="ICU"), Department.objects.create(name="Icu")
    with tenant_context(other_tenant):
        Department.objects.create(name="Oncology")  # the other hospital's department never pre-fills here
    body = client.get("/r/riverside/?dept=Med%2FSurg+3E").content.decode()
    assert f'<option value="{med.pk}" selected>Med/Surg 3E</option>' in body
    assert f'<option value="{icu_lower.pk}" selected>Icu</option>' in client.get("/r/riverside/?dept=Icu").content.decode()  # exact name first
    assert f'<option value="{icu_upper.pk}" selected>ICU</option>' in client.get("/r/riverside/?dept=icu").content.decode()  # then any case
    select = client.get("/r/riverside/?dept=Oncology").content.decode().split('name="department"')[1].split("</select>")[0]
    assert select.count(" selected>") == 1 and '<option value="" selected>' in select and "Oncology" not in select  # nothing pre-filled


def test_rate_limit_message_names_the_hotline(client, ctx, vent, other_tenant, settings):
    settings.PORTAL_RATE_LIMIT_PER_HOUR = 0
    set_other(other_tenant, portal_hotline="ext. 9")
    r = portal_post(client, vent, callback="x1234")
    assert r.status_code == 429 and r.content.decode() == "Too many requests from this location. Please call the Clinical Engineering shop."
    fs.update_settings(portal_hotline="ext. 4400")
    r = portal_post(client, vent, callback="x1234")
    assert r.content.decode() == "Too many requests from this location. Please call the Clinical Engineering shop at ext. 4400."


def test_compliance_meets_an_exactly_hit_target(ctx, dept):
    """40 medium-risk devices, 17 overdue: exactly 57.5% compliant. Float division gives 57.4999..., which used to score a miss."""
    dm = DeviceModel.objects.create(manufacturer="M", model="Med", description="Pump", category="C", risk_class=RiskClass.MEDIUM)
    past, future = date.today() - timedelta(days=3), date.today() + timedelta(days=60)
    Asset.objects.bulk_create([Asset(tenant=ctx, tag=f"CE-M{i:03d}", device_model=dm, department=dept, next_pm_on=past if i < 17 else future)
                               for i in range(40)])
    fs.update_settings(target_pm_pct="57.5")
    row = next(c for c in report_compliance(date.today())["classes"] if c["key"] == RiskClass.MEDIUM)
    assert row["devices"] == 40 and row["overdue"] == 17 and row["compliance_pct"] < 57.5 and row["meets"] is True
    fs.update_settings(target_pm_pct="57.6")
    assert next(c for c in report_compliance(date.today())["classes"] if c["key"] == RiskClass.MEDIUM)["meets"] is False


def test_technician_pm_on_time_is_judged_against_the_tenants_target(ctx, vent, techs, other_tenant):
    from apps.reports.operations import report_tech
    from apps.workorders.services import assign, change_status

    today = date.today()
    for due, done in ((today - timedelta(days=5), today - timedelta(days=6)), (today - timedelta(days=5), today - timedelta(days=2))):
        wo = create_work_order(asset=vent, type="pm", priority="normal", problem="PM", opened_on=today - timedelta(days=10), due_on=due)
        assign(wo, technician=techs["dana"])
        change_status(wo, "in_progress", as_of=today - timedelta(days=10))
        change_status(wo, "completed", as_of=done)
    set_other(other_tenant, target_pm_pct="50")

    def dana():
        return next(t for t in report_tech(today)["technicians"] if t["technician"] == techs["dana"])

    assert dana()["pm_on_time_pct"] == 50.0 and dana()["pm_meets"] is False  # the default 95% target
    fs.update_settings(target_pm_pct="50")
    assert dana()["pm_meets"] is True
