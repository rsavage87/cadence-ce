"""PM schedule screen (slice 9): the page and its roles, the month calendar, month and day selection, the day panel, the body and
panels partials, the create action and its toasts, and tenant isolation. The read models and the batch service are tested in
test_pm_schedule.py; these tests pin the view's clock (views_pm._today) so fixed dates work on any calendar day."""
import json
import pathlib
import re
from datetime import date
from decimal import Decimal

import pytest

from apps.accounts.models import Level, Role, User, create_default_roles
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.workorders.models import Source, WorkOrder, WoType
from apps.workorders.services import create_work_order

TODAY = date(2026, 9, 29)  # a Tuesday
HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "pm-body"}
PANELS = {**HX, "HTTP_HX_TARGET": "pm-panels"}
SEP30 = "/pm/day/2026-09-30/work-orders/?y=2026&m=9"


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """Pin the schedule's clock to TODAY; call the fixture with another date to move it."""

    def _at(d):
        monkeypatch.setattr("apps.web.views_pm._today", lambda: d)

    _at(TODAY)
    return _at


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def dev(tag, model, dept, day, status=AssetStatus.IN_SERVICE):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=day, status=status)


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Due on Sep 30: two ventilators (life support, 1.5 h procedure) and a pump (high, no procedure: 1 h). Sep 15: a pump, now
    overdue. A retired vent on Sep 30 never counts."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"),
                                                         checklist=["Inspect", "Safety test", "Function test"])
    vent_model.save()
    return {"v1": dev("CE-V1", vent_model, dept, date(2026, 9, 30)), "v2": dev("CE-V2", vent_model, dept, date(2026, 9, 30)),
            "p1": dev("CE-P1", pump_model, dept, date(2026, 9, 30)), "p_over": dev("CE-P2", pump_model, dept, date(2026, 9, 15)),
            "retired": dev("CE-VR", vent_model, dept, date(2026, 9, 30), status=AssetStatus.RETIRED)}


@pytest.fixture
def monitor(ctx, dept):
    """A medium-risk device due Sep 30 that nobody is credentialed for."""
    dm = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
    return dev("CE-M1", dm, dept, date(2026, 9, 30))


def custom_user(tenant, levels, username="custom@riverside.example"):
    role = Role.objects.create(name="Custom", slug=username.split("@")[0])
    role.set_levels(levels)
    return User.objects.create_user(username=username, password="Test-Pass-2026-x", tenant=tenant, role=role)


def cell(body, d, y=2026, m=9):
    """The calendar cell (<a class="day ...">...</a>) for day `d` in the (y, m) view."""
    i = body.index(f'href="/pm/?y={y}&amp;m={m}&amp;day={d.isoformat()}"')
    return body[body.rindex("<a ", 0, i):body.index("</a>", i)]


def toast_of(r):
    return json.loads(r["HX-Trigger"])["toast"]["value"]


# --- page and roles ------------------------------------------------------------------------------------------------

def test_view_roles_see_the_page_and_others_get_403(client, ctx, make_user):
    for slug in ("director", "manager", "technician", "analyst"):
        client.force_login(make_user(slug))
        r = client.get("/pm/")
        assert r.status_code == 200, slug
        assert r.context["nav_active"] == "pm"
    for slug in ("requester", "vendor"):
        client.force_login(make_user(slug))
        assert client.get("/pm/").status_code == 403, slug
        assert client.get("/pm/", **BODY).status_code == 403, slug
        assert client.get("/pm/", **PANELS).status_code == 403, slug


def test_anonymous_is_sent_to_sign_in(client, ctx):
    assert client.get("/pm/").status_code == 302
    assert client.post(SEP30).status_code == 302


def test_page_head_summarises_the_next_30_days(client, signed_in, fleet):
    signed_in("director")
    body = client.get("/pm/").content.decode()
    assert "<h1>PM schedule</h1>" in body
    assert "3 PMs due in the next 30 days (4 h) · 1 overdue · intervals from each model's PM program, AEM where approved" in body
    assert "Auto-assign week" not in body and "Route sheets" not in body  # the mock's toast-only buttons are left out


# --- the calendar --------------------------------------------------------------------------------------------------

def test_calendar_marks_today_selection_dots_counts_and_red_past_days(client, signed_in, fleet):
    signed_in("technician")
    body = client.get("/pm/").content.decode()
    assert "<h2>September 2026</h2>" in body and "4 due this month" in body
    assert [d in body for d in ("<div class=\"dow\">Sun</div>", "<div class=\"dow\">Sat</div>")] == [True, True]
    today = cell(body, TODAY)
    assert today.startswith('<a class="day today sel"') and 'aria-current="date"' in today  # no ?day: today is selected
    sep30 = cell(body, date(2026, 9, 30))
    assert sep30.startswith('<a class="day"') and 'title="2 life support"' in sep30 and 'title="1 high risk"' in sep30
    assert '<span class="cnt">3</span>' in sep30 and 'hx-target="#pm-body" hx-swap="outerHTML" hx-push-url="true"' in sep30
    sep15 = cell(body, date(2026, 9, 15))
    assert '<span class="cnt down">1</span>' in sep15 and "life support" not in sep15 and 'title="1 high risk"' in sep15
    assert cell(body, date(2026, 8, 30)).startswith('<a class="day out"') and cell(body, date(2026, 10, 3)).startswith('<a class="day out"')
    assert '<span class="cnt' not in cell(body, date(2026, 9, 1))
    assert "Life support due" in body and "High risk due" in body and "Red count: past due" in body


def test_selecting_a_day_moves_the_selection(client, signed_in, fleet):
    signed_in("director")
    body = client.get("/pm/?y=2026&m=9&day=2026-09-30").content.decode()
    assert cell(body, date(2026, 9, 30)).startswith('<a class="day sel"')
    assert cell(body, TODAY).startswith('<a class="day today"') and "sel" not in cell(body, TODAY).split(">")[0]
    assert "<h2>Due Sep 30, 2026</h2>" in body


def test_month_navigation_crosses_year_boundaries_both_ways(client, signed_in, ctx, today):
    today(date(2026, 12, 15))
    signed_in("director")
    body = client.get("/pm/").content.decode()
    assert "<h2>December 2026</h2>" in body and "<h2>Due Dec 15, 2026</h2>" in body
    nxt, prev = "/pm/?y=2027&amp;m=1", "/pm/?y=2026&amp;m=11"
    assert f'href="{nxt}" hx-get="{nxt}" hx-target="#pm-body" hx-swap="outerHTML" hx-push-url="true" aria-label="Next month"' in body
    assert f'href="{prev}" hx-get="{prev}"' in body
    jan = client.get("/pm/?y=2027&m=1", **BODY).content.decode()
    assert "<h2>January 2027</h2>" in jan and 'href="/pm/?y=2026&amp;m=12"' in jan and 'href="/pm/?y=2027&amp;m=2"' in jan
    assert "<h2>Due Jan 1, 2027</h2>" in jan  # another month without a day: its first day
    back = client.get("/pm/?y=2026&m=12", **BODY).content.decode()
    assert "<h2>Due Dec 15, 2026</h2>" in back  # the current month without a day: today


def test_a_day_alone_shows_its_month_and_a_day_off_the_grid_moves_the_month(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/pm/?day=2027-02-10").content.decode()
    assert "<h2>February 2027</h2>" in body and "<h2>Due Feb 10, 2027</h2>" in body
    body = client.get("/pm/?y=2026&m=1&day=2026-09-30").content.decode()
    assert "<h2>September 2026</h2>" in body and "<h2>Due Sep 30, 2026</h2>" in body
    # a trailing day of the next month is on September's grid: the month stays, the day is selected
    body = client.get("/pm/?y=2026&m=9&day=2026-10-02").content.decode()
    assert "<h2>September 2026</h2>" in body and cell(body, date(2026, 10, 2)).startswith('<a class="day out sel"')


@pytest.mark.parametrize("qs", ["y=abc&m=9", "y=2026&m=13", "y=2026&m=0", "y=1999&m=5", "y=2101&m=1", "y=-1&m=5", "y=2026", "m=4",
                                "y=2026.5&m=3", "y=99999&m=1", "day=2026-02-30", "day=garbage", "day=20260930", "day=2026-W40-2",
                                "day=1999-12-31", "day=2101-01-01", "day=", "y=&m=&day="])
def test_invalid_parameters_fall_back_to_today(client, signed_in, ctx, qs):
    signed_in("director")
    r = client.get(f"/pm/?{qs}")
    assert r.status_code == 200
    body = r.content.decode()
    assert "<h2>September 2026</h2>" in body and "<h2>Due Sep 29, 2026</h2>" in body


def test_an_invalid_month_with_a_valid_day_follows_the_day(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/pm/?y=2026&m=13&day=2026-11-05").content.decode()
    assert "<h2>November 2026</h2>" in body and "<h2>Due Nov 5, 2026</h2>" in body


def test_navigation_stops_at_the_supported_years(client, signed_in, ctx):
    signed_in("director")
    first = client.get("/pm/?y=2000&m=1").content.decode()
    assert '<button class="btn sm" type="button" disabled aria-label="Previous month">' in first and 'href="/pm/?y=2000&amp;m=2"' in first
    last = client.get("/pm/?y=2100&m=12").content.decode()
    assert '<button class="btn sm" type="button" disabled aria-label="Next month">' in last and 'href="/pm/?y=2100&amp;m=11"' in last


# --- partials ------------------------------------------------------------------------------------------------------

def test_body_partial_is_the_calendar_and_day_panel_only(client, signed_in, fleet):
    signed_in("director")
    body = client.get("/pm/?y=2026&m=9&day=2026-09-30", **BODY).content.decode().strip()
    assert body.startswith('<div id="pm-body" class="grid g-32">')
    assert "<html" not in body and "intervals from each model" not in body and 'id="pm-panels"' not in body
    assert "<h2>Due Sep 30, 2026</h2>" in body


def test_panels_partial_renders_the_panels_contract(client, signed_in, ctx, monkeypatch):
    seen = []

    def fake(request, today):
        seen.append(today)
        return {}

    monkeypatch.setattr("apps.web.views_pm.panels_context", fake)
    signed_in("analyst")
    body = client.get("/pm/", **PANELS).content.decode()
    assert 'id="pm-panels"' in body and 'id="pm-body"' not in body and "<html" not in body
    assert seen == [TODAY]


def test_full_page_includes_the_body_then_the_panels(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/pm/").content.decode()
    assert body.index('id="pm-body"') < body.index('id="pm-panels"')


def test_pm_templates_follow_the_self_swapping_wrapper_rule():
    """#pm-body is swapped by its links (they carry hx-swap), so it has none to leak; any wrapper that swaps itself disinherits."""
    folder = pathlib.Path("apps/web/templates/web")
    for path in [folder / "pm.html", *folder.glob("_pm_*.html")]:
        text = path.read_text()
        for m in re.finditer(r"<(\w+)[^>]*hx-target=\"this\"[^>]*>", text):
            tail = m.group(0).split("hx-disinherit")[1] if "hx-disinherit" in m.group(0) else ""
            assert "hx-swap" in tail and "hx-target" in tail, f"{path.name}: {m.group(0)[:80]}"
    root = re.search(r'<div id="pm-body"[^>]*>', pathlib.Path("apps/web/templates/web/_pm_body.html").read_text()).group(0)
    assert "hx-swap" not in root and "hx-target" not in root


# --- the day panel -------------------------------------------------------------------------------------------------

def test_day_list_is_most_critical_first_with_suggested_technicians(client, signed_in, fleet, techs, monitor):
    signed_in("director")
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert "<h2>Due Sep 30, 2026</h2>" in body and "4 PMs · 5.0 h" in body
    order = [body.index(f'<a class="tag" href="/equipment/{t}/">') for t in ("CE-V1", "CE-V2", "CE-P1", "CE-M1")]
    assert order == sorted(order) and "CE-VR" not in body  # retired devices never show
    rows = body.split('<div class="li"')[1:]
    assert len(rows) == 4
    assert '<span class="rail crit">' in rows[0] and '<span class="rail warn">' in rows[2] and '<span class="rail ok">' in rows[3]
    assert "CE-V1</a> · ICU ventilator" in rows[0] and "Hamilton Medical Hamilton-G5 · ICU · HA-G5-PM6" in rows[0]
    assert "BD Alaris 8015 PCU · ICU · No PM procedure" in rows[2]
    # Dana is the only one credentialed for the vents (1.5 h each); the pump then goes to Tom's lighter plate
    assert '<span title="Dana Whitfield">Dana</span><br>1.5 h' in rows[0] and '<span title="Dana Whitfield">Dana</span><br>1.5 h' in rows[1]
    assert '<span title="Tom Okafor">Tom</span><br>1 h' in rows[2]
    assert '<span class="down">Nobody credentialed</span><br>1 h' in rows[3]


def test_a_device_with_an_open_pm_shows_the_chip(client, signed_in, fleet, techs):
    create_work_order(asset=fleet["v2"], type=WoType.PM, priority="high", problem="Already planned")
    signed_in("director")
    body = client.get("/pm/?day=2026-09-30").content.decode()
    rows = body.split('<div class="li"')[1:]
    assert '<span class="chip info">PM open</span><br>1.5 h' in rows[1] and "PM open" not in rows[0] + rows[2]
    assert "Create 2 PM work orders</button>" in body and "1 already has an open PM work order" in body


def test_long_days_are_truncated(client, signed_in, ctx, dept, pump_model):
    for i in range(14):
        dev(f"CE-X{i:02d}", pump_model, dept, date(2026, 10, 6))
    signed_in("director")
    body = client.get("/pm/?day=2026-10-06").content.decode()
    assert body.count('<div class="li"') == 12 and "2 more on this day" in body and "14 PMs · 14.0 h" in body
    assert "Create 14 PM work orders</button>" in body  # the button covers every device, not just the listed ones


def test_empty_day(client, signed_in, fleet):
    signed_in("director")
    body = client.get("/pm/?day=2026-09-29").content.decode()
    assert "No PMs scheduled on this day." in body and "0 PMs · 0.0 h" in body
    assert "Create " not in body and "more on this day" not in body


def test_rows_open_the_device_drawer_only_with_equipment_view(client, ctx, tenant, fleet):
    user = custom_user(tenant, {"pm": Level.VIEW})
    client.force_login(user)
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert "/equipment/CE-V1/" not in body and '<div class="li static">' in body and "CE-V1 · ICU ventilator" in body


def test_rows_link_to_the_drawer(client, signed_in, fleet):
    signed_in("analyst")
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert '<div class="li" hx-get="/equipment/CE-V1/" hx-target="#drawer">' in body and '<a class="tag" href="/equipment/CE-V1/">CE-V1</a>' in body


def test_past_days_say_the_devices_are_overdue(client, signed_in, fleet):
    signed_in("manager")
    body = client.get("/pm/?day=2026-09-15").content.decode()
    assert "This device is overdue: the PM fell due on Sep 15. Work orders created now are due today." in body
    assert 'hx-confirm="Create 1 PM work order for Sep 15, 2026? They will be due today."' in body
    assert "overdue: the PM fell due" not in client.get("/pm/?day=2026-09-01").content.decode()  # a past day with nothing on it


# --- the create button ------------------------------------------------------------------------------------------------

def test_create_button_only_for_approve_roles(client, make_user, fleet):
    for slug in ("director", "manager"):
        client.force_login(make_user(slug))
        body = client.get("/pm/?day=2026-09-30").content.decode()
        assert ('<button class="btn primary sm" type="button" hx-post="/pm/day/2026-09-30/work-orders/?y=2026&amp;m=9" hx-target="#pm-body" '
                'hx-swap="outerHTML" hx-confirm="Create 3 PM work orders for Sep 30, 2026?">Create 3 PM work orders</button>') in body, slug
    for slug in ("technician", "analyst"):
        client.force_login(make_user(slug))
        body = client.get("/pm/?day=2026-09-30").content.decode()
        assert "hx-post" not in body and "Create 3" not in body, slug


def test_create_button_hides_when_every_device_has_an_open_pm(client, signed_in, fleet):
    for a in ("v1", "v2", "p1"):
        create_work_order(asset=fleet[a], type=WoType.PM, priority="normal", problem="Planned")
    signed_in("director")
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert "hx-post" not in body and "Every PM on this day already has an open work order" in body


# --- the create action --------------------------------------------------------------------------------------------------

def test_create_makes_assigns_toasts_and_refreshes_the_panels(client, signed_in, fleet, techs, monitor):
    kim = signed_in("director")
    r = client.post(SEP30, **BODY)
    assert r.status_code == 200
    assert toast_of(r) == "4 PM work orders created, 3 assigned to credentialed technicians"
    assert "pm-changed" in json.loads(r["HX-Trigger-After-Settle"])
    made = WorkOrder.objects.filter(source=Source.PM_PLANNER, type=WoType.PM)
    assert made.count() == 4 and set(made.values_list("created_by", flat=True)) == {kim.pk}
    assert {w.asset.tag: (w.assigned_to.name if w.assigned_to else None) for w in made} == \
        {"CE-V1": "Dana Whitfield", "CE-V2": "Dana Whitfield", "CE-P1": "Tom Okafor", "CE-M1": None}
    body = r.content.decode().strip()
    assert body.startswith('<div id="pm-body"') and "<h2>September 2026</h2>" in body and "<h2>Due Sep 30, 2026</h2>" in body
    assert body.count('<span class="chip info">PM open</span>') == 4 and "hx-post" not in body
    assert "Every PM on this day already has an open work order" in body

    again = client.post(SEP30, **BODY)
    assert toast_of(again) == "Every PM on this day already has an open work order" and made.count() == 4


def test_create_keeps_the_month_the_page_showed(client, signed_in, fleet):
    signed_in("director")
    body = client.post("/pm/day/2026-10-02/work-orders/?y=2026&m=9", **BODY).content.decode()
    assert "<h2>September 2026</h2>" in body and cell(body, date(2026, 10, 2)).startswith('<a class="day out sel"')


def test_create_toast_wording(client, signed_in, ctx, dept, pump_model, fleet, techs):
    signed_in("manager")
    dev("CE-ONE", pump_model, dept, date(2026, 10, 7))
    r = client.post("/pm/day/2026-10-07/work-orders/", **BODY)
    assert toast_of(r) == "1 PM work order created, 1 assigned to a credentialed technician"
    assert toast_of(client.post("/pm/day/2026-10-08/work-orders/", **BODY)) == "No PMs are due on this day"


def test_create_without_credentialed_technicians_leaves_work_unassigned(client, signed_in, fleet):
    signed_in("director")
    r = client.post(SEP30, **BODY)
    assert toast_of(r) == "3 PM work orders created, none assigned: nobody is credentialed for these devices"
    assert not WorkOrder.objects.filter(assigned_to__isnull=False).exists()


def test_create_for_an_overdue_day_is_due_today(client, signed_in, fleet, techs):
    signed_in("director")
    client.post("/pm/day/2026-09-15/work-orders/?y=2026&m=9", **BODY)
    wo = WorkOrder.objects.get(asset=fleet["p_over"], type=WoType.PM)
    assert wo.due_on == TODAY and wo.opened_on == TODAY


def test_pm_approve_without_work_order_approve_creates_unassigned_work(client, ctx, tenant, fleet, techs):
    client.force_login(custom_user(tenant, {"pm": Level.APPROVE, "workorders": Level.EDIT, "equipment": Level.VIEW}))
    assert "Create 3 PM work orders</button>" in client.get("/pm/?day=2026-09-30").content.decode()
    r = client.post(SEP30, **BODY)
    assert r.status_code == 200 and toast_of(r) == "3 PM work orders created; a manager assigns them"
    assert WorkOrder.objects.filter(type=WoType.PM).count() == 3 and not WorkOrder.objects.filter(assigned_to__isnull=False).exists()
    r = client.post("/pm/day/2026-09-15/work-orders/", **BODY)
    assert toast_of(r) == "1 PM work order created; a manager assigns it"


def test_create_is_post_only_and_needs_pm_approve(client, make_user, fleet):
    client.force_login(make_user("director"))
    assert client.get(SEP30).status_code == 405
    for slug in ("technician", "analyst", "requester", "vendor"):
        client.force_login(make_user(slug))
        assert client.post(SEP30, **BODY).status_code == 403, slug
    assert not WorkOrder.objects.exists()


@pytest.mark.parametrize("day", ["2026-13-01", "2026-02-30", "garbage", "20260930", "2026-W40-2", "1999-12-31", "2101-01-01"])
def test_create_with_a_malformed_day_is_404(client, signed_in, fleet, day):
    signed_in("director")
    assert client.post(f"/pm/day/{day}/work-orders/", **BODY).status_code == 404
    assert not WorkOrder.objects.exists()


# --- tenant isolation -----------------------------------------------------------------------------------------------

@pytest.fixture
def theirs(other_tenant):
    """Another hospital: a ventilator due Sep 30 (a day we also have) and one due Sep 20 (a day only they have), a credentialed technician."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="Their ventilator", category="Ventilators",
                                        risk_class=RiskClass.LIFE_SUPPORT)
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=dept, next_pm_on=date(2026, 9, 30))
        b = Asset.objects.create(tag="THEIRS-2", device_model=dm, department=dept, next_pm_on=date(2026, 9, 20))
        tech = Technician.objects.create(name="Aaron Theirs")
        Credential.objects.create(technician=tech, scope=Scope.CATEGORY, value="Ventilators")
    return {"a": a, "b": b, "tech": tech}


def test_other_tenants_devices_never_appear(client, signed_in, fleet, techs, theirs):
    signed_in("director")
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert "THEIRS" not in body and "Their ventilator" not in body and "Aaron" not in body and "3 PMs · 4.0 h" in body
    assert '<span class="cnt">3</span>' in cell(body, date(2026, 9, 30)) and '<span class="cnt' not in cell(body, date(2026, 9, 20))
    assert "3 PMs due in the next 30 days" in body and "4 due this month" in body
    day20 = client.get("/pm/?day=2026-09-20", **BODY).content.decode()
    assert "No PMs scheduled on this day." in day20 and "hx-post" not in day20


def test_a_day_only_another_tenant_has_creates_nothing(client, signed_in, fleet, theirs, other_tenant):
    signed_in("director")
    r = client.post("/pm/day/2026-09-20/work-orders/", **BODY)
    assert r.status_code == 200 and toast_of(r) == "No PMs are due on this day"
    client.post(SEP30, **BODY)
    assert WorkOrder.objects.filter(type=WoType.PM).count() == 3
    with tenant_context(other_tenant):
        assert not WorkOrder.objects.exists()


def test_another_tenants_user_sees_only_their_own(client, make_user, fleet, theirs, other_tenant):
    client.force_login(make_user("director", tenant_=other_tenant, username="dir@other.example"))
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert "THEIRS-1" in body and "CE-V1" not in body and "1 PM · 1.0 h" in body
