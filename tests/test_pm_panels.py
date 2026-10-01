"""PM schedule lower panels and API (slice 9): the 30-day outlook, next week's technician workload, the PM library, the panels'
self-refresh contract with the page, the PM API's calendar, day, and create-for-day actions with their levels, and tenant isolation.

Everything is built relative to date.today() (the panels, the page, and the API all read the real clock), so these pass on any day."""
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.template.loader import render_to_string

from apps.accounts.models import Level, Role, User, create_default_roles
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.web.pm_panels import panels_context
from apps.workorders.models import Source, WorkOrder, WoType
from apps.workorders.services import assign, create_work_order

HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "pm-panels"}


def today():
    return date.today()


def dev(tag, model, dept, due, status=AssetStatus.IN_SERVICE):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=due, status=status)


def panels(t=None) -> str:
    return render_to_string("web/_pm_panels.html", panels_context(None, t or today()))


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Next 7 days: a ventilator tomorrow (life support, 1.5 h procedure) and a pump in two days. Also in 30 days: a pump in 20 days.
    Overdue: a pump three days ago. Outside the window: a pump in 45 days. A retired ventilator tomorrow never counts."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"),
                                                         checklist=["Inspect", "Electrical safety", "Function test"])
    vent_model.save()
    t = today()
    return {"v1": dev("CE-V1", vent_model, dept, t + timedelta(days=1)), "p1": dev("CE-P1", pump_model, dept, t + timedelta(days=2)),
            "p20": dev("CE-P20", pump_model, dept, t + timedelta(days=20)), "p_over": dev("CE-P0", pump_model, dept, t - timedelta(days=3)),
            "p_far": dev("CE-P45", pump_model, dept, t + timedelta(days=45)),
            "retired": dev("CE-VR", vent_model, dept, t + timedelta(days=1), status=AssetStatus.RETIRED)}


# --- the panels' contract with the page -------------------------------------------------------------------------------

def test_panels_root_refetches_itself_on_pm_changed_without_leaking_its_swap(ctx):
    html = panels()
    root = re.search(r"<div id=\"pm-panels\"[^>]*>", html).group(0)
    for attr in ('hx-get="/pm/"', 'hx-trigger="pm-changed from:body, devices-changed from:body, models-changed from:body"', 'hx-target="this"',
                 'hx-swap="outerHTML"', 'hx-disinherit="hx-swap hx-target"'):
        assert attr in root
    assert html.count('id="pm-panels"') == 1


def test_panels_render_inside_the_full_page(client, signed_in, fleet, techs):
    signed_in("manager")
    body = client.get("/pm/").content.decode()
    assert body.count('id="pm-panels"') == 1 and "Next 30 days" in body and "Technician workload, next 7 days" in body and "PM library" in body
    assert "<html" in body and "Dana Whitfield" in body


def test_panels_render_alone_for_their_own_refresh(client, signed_in, fleet, techs):
    signed_in("analyst")  # PM View is enough to read them
    r = client.get("/pm/", **HX)
    body = r.content.decode().strip()
    assert r.status_code == 200 and body.startswith('<div id="pm-panels"') and "<html" not in body and 'id="pm-body"' not in body
    assert 'hx-trigger="pm-changed from:body, devices-changed from:body, models-changed from:body"' in body and 'hx-disinherit="hx-swap hx-target"' in body
    assert "Technician workload" in body


@pytest.mark.parametrize("role", ["requester", "vendor"])
def test_panels_refresh_is_refused_without_pm_view(client, signed_in, fleet, role):
    signed_in(role)
    assert client.get("/pm/", **HX).status_code == 403


# --- next 30 days ----------------------------------------------------------------------------------------------------

def test_outlook_stats_and_chart_by_category(fleet):
    ctx_ = panels_context(None, today())
    o = ctx_["outlook"]
    assert (o["due"], o["life_support"], o["overdue"]) == (3, 1, 1)
    assert [(r["full_label"], r["value"]) for r in o["chart"]["rows"]] == [("Infusion pumps", "2"), ("Ventilators", "1")]  # most first
    html = panels()
    assert re.findall(r'<div class="v[^"]*">(\d+)</div><div class="l">([^<]+)</div>', html) == [("3", "PMs due"), ("1", "life-support devices"),
                                                                                                 ("1", "already overdue")]
    assert '<div class="v down">1</div><div class="l">already overdue</div>' in html
    assert 'aria-label="PMs due in 30 days by category"' in html and ">Infusion pumps</text>" in html and ">Ventilators</text>" in html
    assert "Nothing is due in the next 30 days." not in html


def test_outlook_empty_state_and_quiet_overdue(ctx, dept, pump_model):
    dev("CE-FAR", pump_model, dept, today() + timedelta(days=60))
    html = panels()
    assert "Nothing is due in the next 30 days." in html and "<svg" not in html
    assert '<div class="v">0</div><div class="l">already overdue</div>' in html  # zero overdue is not red


# --- technician workload ---------------------------------------------------------------------------------------------

def test_workload_rows_over_capacity_and_hint_numbers(fleet, techs):
    """Dana is the only one credentialed for the vent; Tom already has a 3 h repair and is assigned the PM of a pump
    due in three days, so the pump due in two days goes to Dana (lighter plate). Tom's 2 h week is over capacity."""
    tom = techs["tom"]
    Technician.objects.filter(pk=tom.pk).update(weekly_capacity_hours=Decimal("2"))
    t = today()
    extra = dev("CE-P3", fleet["p1"].device_model, fleet["p1"].department, t + timedelta(days=3))
    assign(create_work_order(asset=extra, type=WoType.PM, priority="normal", problem="PM", estimated_hours=Decimal("1")), technician=tom)
    assign(create_work_order(asset=fleet["p_over"], type=WoType.REPAIR, priority="normal", problem="Door latch", estimated_hours=Decimal("3")),
           technician=tom)
    Technician.objects.create(name="Former Tech", title="BMET I", is_active=False)
    rows = {w["technician"].name: w for w in panels_context(None, t)["workload"]}
    assert set(rows) == {"Dana Whitfield", "Tom Okafor"}  # active technicians only
    assert (rows["Tom Okafor"]["over"], rows["Tom Okafor"]["width"]) == (True, "100.0")
    assert (rows["Dana Whitfield"]["over"], rows["Dana Whitfield"]["width"]) == (False, "7.8")  # 2.5 of 32 h
    html = panels(t)
    blocks = dict(re.findall(r'<b style="font-weight:500">([^<]+)</b>(.*?)</div>\s*</div>', html, re.S))
    assert '<span class="down">4.0 of 2 h</span>' in blocks["Tom Okafor"] and "background:var(--crit)" in blocks["Tom Okafor"]
    assert "1 PM (1.0 h) · other open work 3.0 h" in blocks["Tom Okafor"] and "· BMET I" in blocks["Tom Okafor"]
    assert '<span class="muted">2.5 of 32 h</span>' in blocks["Dana Whitfield"] and "width:7.8%;background:var(--accent)" in blocks["Dana Whitfield"]
    assert "2 PMs (2.5 h) · other open work 0.0 h" in blocks["Dana Whitfield"]
    assert "Former Tech" not in html and "counts for the technician its open work order is assigned to" in html


def test_workload_empty_state(fleet):
    html = panels()
    assert "No active technicians." in html and "counts for the technician its open work order is assigned to" not in html


# --- the PM library ---------------------------------------------------------------------------------------------------

def test_library_rows_intervals_procedures_and_counts(fleet, pump_model, vent_model):
    vent_model.aem_interval_months = 12  # set, but life support never runs on AEM
    vent_model.save()
    DeviceModel.objects.create(manufacturer="Welch Allyn", model="Spot 4400", description="Vital signs monitor", category="Monitors",
                               risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    html = panels()
    assert "1 of 3 models has a PM procedure · from OEM service manuals, ECRI, or written in-house" in html and "Sync now" not in html
    rows = re.findall(r"<tr class=\"row\" hx-get=\"/pm/models/[0-9a-f-]+/\" hx-target=\"#drawer\"><td class=\"two\">(.*?)</tr>", html, re.S)
    assert [re.match(r"([^<]+)<small>", r).group(1) for r in rows] == ["Hamilton Medical Hamilton-G5", "BD Alaris 8015 PCU", "Welch Allyn Spot 4400"]
    vent, pump, monitor = rows
    assert "<small>ICU ventilator</small>" in vent and "Life support" in vent and "<td>6 mo</td>" in vent
    assert '<span class="muted">OEM interval</span>' in vent and "AEM" not in vent
    assert '<td class="two">HA-G5-PM6<small>OEM service manual</small></td>' in vent and '<td class="num">1.50</td>' in vent and "3 steps" in vent
    assert vent.strip().endswith('<td class="num">1</td>')  # the retired ventilator is not counted
    assert '<span class="chip acc">AEM 18 mo</span>' in pump and "<td>12 mo</td>" in pump and '<span class="muted">No procedure</span>' in pump
    assert '<td class="num muted">1.00</td>' in pump and pump.strip().endswith('<td class="num">4</td>')  # four active pumps
    assert '<span class="muted">OEM interval</span>' in monitor and "No procedure" in monitor and monitor.strip().endswith('<td class="num">0</td>')


def test_library_empty_state(ctx):
    assert "No device models yet." in panels() and "0 of 0 models have a PM procedure" in panels()


# --- the API ----------------------------------------------------------------------------------------------------------

def test_api_calendar_shape(client, signed_in, fleet):
    signed_in("analyst")
    t = today()
    tomorrow = t + timedelta(days=1)
    data = client.get(f"/api/v1/pm/calendar/?y={tomorrow.year}&m={tomorrow.month}").json()
    assert (data["year"], data["month"]) == (tomorrow.year, tomorrow.month) and all(len(w) == 7 for w in data["weeks"])
    cells = {c["date"]: c for w in data["weeks"] for c in w}
    assert cells[tomorrow.isoformat()] == {"date": tomorrow.isoformat(), "in_month": True, "is_today": False, "past": False, "n": 1,
                                           "life_support": 1, "high": 0}
    assert data["weeks"][0][0]["date"] <= f"{tomorrow.year}-{tomorrow.month:02d}-01"
    default = client.get("/api/v1/pm/calendar/").json()  # this month by default
    in_this_month = [d for d in (t + timedelta(days=n) for n in (1, 2, 20, 45)) if (d.year, d.month) == (t.year, t.month)]
    past_this_month = 1 if (t - timedelta(days=3)).month == t.month else 0  # the overdue pump, when three days ago is still this month
    assert (default["year"], default["month"]) == (t.year, t.month) and default["due_this_month"] == len(in_this_month) + past_this_month
    assert any(c["is_today"] for w in default["weeks"] for c in w)


@pytest.mark.parametrize("query", ["y=abc&m=1", "y=2026&m=13", "y=2026&m=0", "y=10000&m=1", "y=1&m=1", "m=x"])
def test_api_calendar_rejects_bad_parameters(client, signed_in, ctx, query):
    signed_in("director")
    assert client.get(f"/api/v1/pm/calendar/?{query}").status_code == 400


def test_api_day_plan_shape(client, signed_in, fleet, techs):
    signed_in("director")
    t = today()
    day = t + timedelta(days=1)
    data = client.get(f"/api/v1/pm/day/?day={day.isoformat()}").json()
    assert (data["day"], data["overdue"], data["count"], data["hours"], data["to_create"]) == (day.isoformat(), False, 1, "1.50", 1)
    v = data["devices"][0]
    assert v == {"asset_id": str(fleet["v1"].id), "tag": "CE-V1", "description": "ICU ventilator", "manufacturer": "Hamilton Medical",
                 "model": "Hamilton-G5", "department": "ICU", "risk_class": "life_support", "hours": "1.50", "procedure": "HA-G5-PM6",
                 "has_open_pm": False, "technician": {"id": str(techs["dana"].id), "name": "Dana Whitfield"}}
    over = client.get(f"/api/v1/pm/day/?day={(t - timedelta(days=3)).isoformat()}").json()
    assert over["overdue"] is True and over["devices"][0]["procedure"] is None and over["devices"][0]["hours"] == "1.00"


def test_api_day_plan_without_a_credentialed_technician_and_with_an_open_pm(client, signed_in, fleet):
    signed_in("director")
    create_work_order(asset=fleet["p1"], type=WoType.PM, priority="normal", problem="PM")
    data = client.get(f"/api/v1/pm/day/?day={(today() + timedelta(days=2)).isoformat()}").json()
    assert data["to_create"] == 0 and data["devices"][0]["has_open_pm"] is True and data["devices"][0]["technician"] is None


@pytest.mark.parametrize("query", ["", "?day=", "?day=2026-02-30", "?day=20260930", "?day=2026-9-3", "?day=tomorrow"])
def test_api_day_rejects_a_missing_or_malformed_day(client, signed_in, ctx, query):
    signed_in("director")
    assert client.get(f"/api/v1/pm/day/{query}").status_code == 400


def test_api_create_for_day_creates_assigns_and_skips(client, signed_in, fleet, techs):
    kim = signed_in("manager")
    day = (today() + timedelta(days=1)).isoformat()
    r = client.post("/api/v1/pm/create-for-day/", {"day": day}, content_type="application/json")
    assert r.status_code == 200 and r.json() == {"created": 1, "assigned": 1, "skipped": 0}
    wo = WorkOrder.objects.get(asset=fleet["v1"], type=WoType.PM)
    assert (wo.assigned_to, wo.created_by, wo.source) == (techs["dana"], kim, Source.PM_PLANNER)
    again = client.post("/api/v1/pm/create-for-day/", {"day": day}, content_type="application/json")
    assert again.json() == {"created": 0, "assigned": 0, "skipped": 1}


@pytest.mark.parametrize("body", [{}, {"day": ""}, {"day": "2026-13-01"}, {"day": 20260930}, ["2026-09-30"]])
def test_api_create_for_day_rejects_a_malformed_day(client, signed_in, fleet, body):
    signed_in("director")
    assert client.post("/api/v1/pm/create-for-day/", body, content_type="application/json").status_code == 400
    assert not WorkOrder.objects.exists()


@pytest.mark.parametrize("role, read, create", [("director", 200, 200), ("manager", 200, 200), ("technician", 200, 403), ("analyst", 200, 403),
                                               ("requester", 403, 403), ("vendor", 403, 403)])
def test_api_levels(client, signed_in, fleet, role, read, create):
    signed_in(role)
    day = (today() + timedelta(days=1)).isoformat()
    assert client.get("/api/v1/pm/calendar/").status_code == read
    assert client.get(f"/api/v1/pm/day/?day={day}").status_code == read
    assert client.post("/api/v1/pm/create-for-day/", {"day": day}, content_type="application/json").status_code == create
    assert WorkOrder.objects.exists() == (create == 200)


def test_api_create_leaves_work_unassigned_without_work_order_approve(client, fleet, techs, tenant):
    planner = Role.objects.create(name="PM planner", slug="pm-planner")
    planner.set_levels({"pm": Level.APPROVE, "workorders": Level.EDIT})
    client.force_login(User.objects.create_user(username="planner@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=planner))
    r = client.post("/api/v1/pm/create-for-day/", {"day": (today() + timedelta(days=1)).isoformat()}, content_type="application/json")
    assert r.status_code == 200 and r.json() == {"created": 1, "assigned": 0, "skipped": 0}
    assert WorkOrder.objects.get(asset=fleet["v1"], type=WoType.PM).assigned_to is None


def test_api_needs_a_tenant(client, db, django_user_model):
    root = django_user_model.objects.create_superuser(username="root@example.com", password="Test-Pass-2026-x")
    client.force_login(root)
    day = today().isoformat()
    assert client.get("/api/v1/pm/calendar/").status_code == 403
    assert client.get(f"/api/v1/pm/day/?day={day}").status_code == 403
    assert client.post("/api/v1/pm/create-for-day/", {"day": day}, content_type="application/json").status_code == 403


# --- tenant isolation -------------------------------------------------------------------------------------------------

@pytest.fixture
def theirs(other_tenant):
    """Another hospital with a ventilator due tomorrow, an overdue pump, a credentialed technician, and a procedure."""
    create_default_roles(other_tenant)
    t = today()
    with tenant_context(other_tenant):
        proc = PmProcedure.objects.create(code="THEIR-PM", name="Theirs", estimated_hours=Decimal("9"), checklist=["x"])
        dm = DeviceModel.objects.create(manufacturer="Dräger", model="Evita V800", description="Their ventilator", category="Their vents",
                                        risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6, pm_procedure=proc)
        icu = Department.objects.create(name="Their ICU")
        vent = dev("THEIRS-1", dm, icu, t + timedelta(days=1))
        dev("THEIRS-2", dm, icu, t - timedelta(days=5))
        tech = Technician.objects.create(name="Aaron Theirs", title="Their BMET")
        Credential.objects.create(technician=tech, scope=Scope.CATEGORY, value="Their vents")
    return {"tenant": other_tenant, "vent": vent, "tech": tech}


def test_panels_never_show_another_tenants_devices_technicians_or_models(fleet, techs, theirs):
    html = panels()
    for s in ("Aaron Theirs", "Their BMET", "Evita V800", "THEIR-PM", "Their vents"):
        assert s not in html
    o = panels_context(None, today())["outlook"]
    assert (o["due"], o["life_support"], o["overdue"]) == (3, 1, 1)


def test_api_never_shows_or_touches_another_tenants_devices(client, signed_in, fleet, techs, theirs, make_user):
    signed_in("director")
    t = today()
    day = (t + timedelta(days=1)).isoformat()
    tomorrow = t + timedelta(days=1)
    cal = client.get(f"/api/v1/pm/calendar/?y={tomorrow.year}&m={tomorrow.month}").json()  # tomorrow's own month: always on the grid
    assert {c["date"]: c["n"] for w in cal["weeks"] for c in w}[day] == 1  # our vent only, never their ventilator due the same day
    plan = client.get(f"/api/v1/pm/day/?day={day}").json()
    assert [d["tag"] for d in plan["devices"]] == ["CE-V1"] and plan["devices"][0]["technician"]["name"] == "Dana Whitfield"
    assert client.post("/api/v1/pm/create-for-day/", {"day": day}, content_type="application/json").json()["created"] == 1
    with tenant_context(theirs["tenant"]):
        assert not WorkOrder.objects.exists()  # their ventilator due the same day was not planned by our director
    # and their director sees only their own
    client.force_login(make_user("director", tenant_=theirs["tenant"]))
    plan = client.get(f"/api/v1/pm/day/?day={day}").json()
    assert [d["tag"] for d in plan["devices"]] == ["THEIRS-1"] and plan["devices"][0]["technician"]["name"] == "Aaron Theirs"
    assert client.post("/api/v1/pm/create-for-day/", {"day": day}, content_type="application/json").json() == {"created": 1, "assigned": 1,
                                                                                                                 "skipped": 0}
    with tenant_context(theirs["tenant"]):
        assert list(WorkOrder.objects.values_list("asset__tag", flat=True)) == ["THEIRS-1"]
    assert WorkOrder.objects.count() == 1  # ours, untouched
