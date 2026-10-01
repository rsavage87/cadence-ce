"""PM route sheets, report PDFs, and printing the app's own pages (slice 11): permissions, who each device's sheet belongs to, order,
day and week scope, tenant isolation, bounded queries, every report on paper, the buttons that open them, and cadence.css's print
section. The route sheet tests pin the PM screen's clock (views_pm._today) so fixed dates work on any calendar day."""
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Level, Module, Role, User
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.pm.models import PmProcedure
from apps.reports.services import REPORTS
from apps.tenants.context import tenant_context
from apps.workorders.models import WoType
from apps.workorders.services import assign, create_work_order

TODAY = date(2026, 9, 29)  # a Tuesday
SEP30 = date(2026, 9, 30)
URL = "/print/route-sheets/"
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """Pin the PM screen's clock, which the route sheets read, to TODAY; call the fixture with another date to move it."""

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


def custom_user(tenant, levels, username="custom@riverside.example"):
    role = Role.objects.create(name="Custom", slug=username.split("@")[0])
    role.set_levels(levels)
    return User.objects.create_user(username=username, password="Test-Pass-2026-x", tenant=tenant, role=role)


def dev(tag, model, dept, day, room=""):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, room=room, next_pm_on=day)


def pm_wo(asset, technician=None, vendor="", problem="PM"):
    wo = create_work_order(asset=asset, type=WoType.PM, priority="normal", problem=problem)
    return assign(wo, technician=technician, vendor_name=vendor) if (technician or vendor) else wo


def sheets(body):
    """[(sheet name, [tags in row order], the sheet's html)] in page order."""
    out = []
    for part in body.split('<section class="sheet rs">')[1:]:
        part = part.split("</section>")[0]
        out.append((re.search(r"<h1>(.*?)</h1>", part).group(1), re.findall(r'<td class="tag">(.*?)</td>', part), part))
    return out


@pytest.fixture
def crew(ctx):
    """Dana is credentialed for the Hamilton-G5, Tom for infusion pumps, nobody for monitors. No expiry dates, so any day works."""
    dana = Technician.objects.create(name="Dana Whitfield", title="Lead BMET")
    Credential.objects.create(technician=dana, scope=Scope.MODEL, value="Hamilton-G5")
    tom = Technician.objects.create(name="Tom Okafor", title="BMET I")
    Credential.objects.create(technician=tom, scope=Scope.CATEGORY, value="Infusion pumps")
    return {"dana": dana, "tom": tom}


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors", risk_class=RiskClass.MEDIUM)


@pytest.fixture
def floor(ctx, dept, vent_model, pump_model, monitor_model, crew):
    """Due Sep 30. Ventilators have a 1.5 h procedure; pumps and the monitor have none (1 h).
    V1 (ICU A03) has an open PM with Tom, though the plan would suggest Dana; V2 (Emergency) has none, so Dana is suggested;
    P1 (ICU) has an open PM with the vendor; P2 (ICU B12) and P3 (Emergency) have none, so Tom is suggested; M1 nobody can take."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"),
                                                         checklist=["Inspect", "Safety test"])
    vent_model.save()
    ed = Department.objects.create(name="Emergency")
    f = {"v1": dev("CE-V1", vent_model, dept, SEP30, "A03"), "v2": dev("CE-V2", vent_model, ed, SEP30, "Bay 2"),
         "p1": dev("CE-P1", pump_model, dept, SEP30, "C01"), "p2": dev("CE-P2", pump_model, dept, SEP30, "B12"),
         "p3": dev("CE-P3", pump_model, ed, SEP30, "Z9"), "m1": dev("CE-M1", monitor_model, dept, SEP30)}
    f["v1_wo"] = pm_wo(f["v1"], technician=crew["tom"])
    f["p1_wo"] = pm_wo(f["p1"], vendor="BD Field Service")
    return f


# --- route sheets: access ----------------------------------------------------------------------------------------------

def test_route_sheets_need_pm_view(client, ctx, make_user):
    for slug in ("director", "manager", "technician", "analyst"):
        client.force_login(make_user(slug))
        assert client.get(URL).status_code == 200, slug
    for slug in ("requester", "vendor"):  # PM: None in the default matrix
        client.force_login(make_user(slug))
        assert client.get(URL).status_code == 403, slug
        assert client.get(URL + "?scope=week").status_code == 403, slug


def test_anonymous_is_sent_to_sign_in(client, ctx):
    assert client.get(URL).status_code == 302
    assert client.get("/print/reports/cosr/").status_code == 302


# --- route sheets: who does each device, and in what order ---------------------------------------------------------------

def test_each_device_goes_on_the_sheet_of_whoever_does_it(client, signed_in, floor):
    signed_in("technician")
    r = client.get(URL + "?day=2026-09-30")
    assert r.status_code == 200 and r.templates[0].name == "web/print_route_sheets.html"
    got = sheets(r.content.decode())
    # Technicians by name, then the vendor, then nobody. An open PM with Tom stays with Tom although the plan would suggest Dana.
    assert [(name, tags) for name, tags, _ in got] == [
        ("Dana Whitfield", ["CE-V2"]),
        ("Tom Okafor", ["CE-P3", "CE-V1", "CE-P2"]),  # by department (Emergency, ICU), then room (A03, B12), then tag
        ("Vendor service", ["CE-P1"]),
        ("No credentialed technician", ["CE-M1"]),
    ]
    dana, tom, vendor, nobody = (html for _, _, html in got)
    assert "<b>1 device · 1.5 h</b>" in dana and "<b>3 devices · 3.5 h</b>" in tom and "<b>1 device · 1 h</b>" in nobody
    assert "PM route · Wednesday, September 30, 2026" in tom
    # The open PM work order's number; devices without one say so.
    assert f'<span class="mono">{floor["v1_wo"].number}</span>' in tom and tom.count("None yet") == 2
    assert f'<span class="mono">{floor["p1_wo"].number}</span>' in vendor and "These PM work orders are with the vendor." in vendor
    assert "No active technician is credentialed for these devices." in nobody
    # Each row: box, tag, device, risk, department, room, procedure, hours, work order, and blank Initials and Time to fill in.
    assert "<th>Initials</th><th>Time</th>" in tom and tom.count('<td class="write"></td>') == 6 and tom.count('<span class="box"></span>') == 3
    assert "Hamilton Medical Hamilton-G5<small>ICU ventilator</small>" in tom and '<span class="chip bad">Life support</span>' in tom
    assert '<span class="chip warn">High</span>' in tom and "<td>Emergency</td>" in tom and "<td>A03</td>" in tom
    assert '<td class="code">HA-G5-PM6</td>' in tom and '<td class="num">1.5</td>' in tom and "No procedure" in tom
    assert "<td>Medium</td>" in nobody and "<td>—</td>" in nobody  # M1 has no room
    assert '<div class="sign"><div>Technician signature</div>' in tom  # a sign-off line at the end of every sheet
    assert "Date</th>" not in tom and "Overdue" not in r.content.decode()  # the day scope has no date column; Sep 30 is not past


def test_an_open_pm_on_nobodys_plate_goes_to_the_suggested_technician(client, signed_in, floor, crew):
    """An unassigned open PM (the nightly job makes those) or one held by a deactivated technician is not on anyone's plate yet:
    the device goes where the schedule suggests, and the sheet shows the work order and who holds it."""
    unassigned = pm_wo(floor["p2"])
    gone = Technician.objects.create(name="Sam Left", is_active=False)
    held = pm_wo(floor["v2"], technician=gone)
    nobody = pm_wo(floor["m1"])
    signed_in("manager")
    got = {name: html for name, _, html in sheets(client.get(URL + "?day=2026-09-30").content.decode())}
    assert "Sam Left" not in got
    assert f'<span class="mono">{unassigned.number}</span><small>unassigned</small>' in got["Tom Okafor"]
    assert f'<span class="mono">{held.number}</span><small>with Sam Left, inactive</small>' in got["Dana Whitfield"]
    assert f'<span class="mono">{nobody.number}</span><small>unassigned</small>' in got["No credentialed technician"]


def test_a_vendor_pm_assigned_back_in_house_goes_to_the_technician(client, signed_in, floor, crew):
    assign(floor["p1_wo"], technician=crew["dana"])
    signed_in("director")
    names = [name for name, _, _ in sheets(client.get(URL + "?day=2026-09-30").content.decode())]
    assert names == ["Dana Whitfield", "Tom Okafor", "No credentialed technician"]


# --- route sheets: the day ----------------------------------------------------------------------------------------------

def test_the_day_defaults_to_today_and_ignores_the_pm_screens_other_parameters(client, signed_in, ctx, dept, pump_model, crew):
    dev("CE-TODAY", pump_model, dept, TODAY)
    dev("CE-SEP30", pump_model, dept, SEP30)
    signed_in("technician")
    for query in ("", "?day=", "?day=2026-13-40", "?day=20260930", "?day=2026-9-30", "?day=abc", "?day=1999-09-30", "?y=2026&m=11", "?page=2&mode=board"):
        r = client.get(URL + query)
        assert r.status_code == 200 and r.context["day"] == TODAY, query
        assert [t for _, tags, _ in sheets(r.content.decode()) for t in tags] == ["CE-TODAY"], query
    r = client.get(URL + "?y=2026&m=11&day=2026-09-30")  # the PM screen's month is ignored; its selected day is what prints
    assert r.context["day"] == SEP30 and [t for _, tags, _ in sheets(r.content.decode()) for t in tags] == ["CE-SEP30"]


def test_a_past_day_prints_its_overdue_devices_marked_overdue(client, signed_in, ctx, dept, pump_model, crew):
    dev("CE-LATE1", pump_model, dept, date(2026, 9, 15))
    dev("CE-LATE2", pump_model, dept, date(2026, 9, 15))
    signed_in("technician")
    body = client.get(URL + "?day=2026-09-15").content.decode()
    [(name, tags, html)] = sheets(body)
    assert name == "Tom Okafor" and tags == ["CE-LATE1", "CE-LATE2"]
    assert '<span class="chip bad">Overdue</span>' in html and "These devices are overdue: the PM fell due on Sep 15." in html


def test_a_day_with_nothing_due_prints_one_sheet_saying_so(client, signed_in, floor):
    signed_in("technician")
    body = client.get(URL + "?day=2026-10-03").content.decode()
    assert body.count('<section class="sheet rs">') == 1 and "No PMs are due on Oct 3, 2026." in body
    assert "CE-" not in body and "Overdue" not in body and "0 devices" in body
    week = client.get(URL + "?scope=week&day=2026-10-03")
    assert week.status_code == 200  # the week still has Sep 30's devices


def test_bar_toggles_day_and_week_keeping_the_day(client, signed_in, floor):
    signed_in("technician")
    body = client.get(URL + "?day=2026-09-30").content.decode()
    assert '<a class="pbtn active" href="/print/route-sheets/?day=2026-09-30" aria-current="true">Day</a>' in body
    assert '<a class="pbtn" href="/print/route-sheets/?scope=week&amp;day=2026-09-30" title="Today and the next 6 days">Week</a>' in body
    assert "<title>Route sheets · Sep 30, 2026 · Riverside Regional</title>" in body and "6 devices on 4 sheets" in body
    week = client.get(URL + "?scope=week&day=2026-09-30").content.decode()
    assert '<a class="pbtn" href="/print/route-sheets/?day=2026-09-30">Day</a>' in week
    assert 'aria-current="true" title="Today and the next 6 days">Week</a>' in week


# --- route sheets: the week ---------------------------------------------------------------------------------------------------

def test_week_scope_prints_the_week_plan_by_technician_across_days(client, signed_in, ctx, dept, pump_model, monitor_model, crew):
    ed = Department.objects.create(name="Emergency")
    dev("CE-W1", pump_model, dept, TODAY + timedelta(days=3), "A1")      # Fri Oct 2
    dev("CE-W2", pump_model, ed, TODAY + timedelta(days=3), "Z9")        # Fri Oct 2, Emergency before ICU
    dev("CE-W3", pump_model, dept, TODAY, "B2")                          # today
    dev("CE-W4", pump_model, dept, TODAY + timedelta(days=6), "A1")      # Mon Oct 5, the last day of the week
    dev("CE-W5", monitor_model, dept, TODAY + timedelta(days=1))         # nobody credentialed
    dev("CE-LATER", pump_model, dept, TODAY + timedelta(days=7))         # next week
    dev("CE-PAST", pump_model, dept, TODAY - timedelta(days=1))          # overdue, not in the week plan
    wo = pm_wo(Asset.objects.get(tag="CE-W4"), technician=crew["dana"])  # held by Dana, though only Tom is credentialed
    signed_in("technician")
    r = client.get(URL + "?scope=week&day=2026-09-30")
    assert r.status_code == 200 and r.context["week"] and (r.context["start"], r.context["end"]) == (TODAY, date(2026, 10, 5))
    got = sheets(r.content.decode())
    assert [(name, tags) for name, tags, _ in got] == [
        ("Dana Whitfield", ["CE-W4"]),
        ("Tom Okafor", ["CE-W3", "CE-W2", "CE-W1"]),  # by date, then department, room, and tag
        ("No credentialed technician", ["CE-W5"]),
    ]
    dana, tom, _ = (html for _, _, html in got)
    assert "<th>Date</th>" in tom and '<td class="day">Tue Sep 29</td>' in tom and '<td class="day">Fri Oct 2</td>' in tom
    assert '<td class="day">Mon Oct 5</td>' in dana and f'<span class="mono">{wo.number}</span>' in dana
    assert "Tue, Sep 29 to Mon, Oct 5, 2026" in tom and "<b>3 devices · 3 h</b>" in tom
    assert "Overdue" not in r.content.decode() and "<title>Route sheets · Sep 29 to Oct 5, 2026 · Riverside Regional</title>" in r.content.decode()


def test_an_empty_week_prints_one_sheet_saying_so(client, signed_in, ctx):
    signed_in("technician")
    body = client.get(URL + "?scope=week").content.decode()
    assert body.count('<section class="sheet rs">') == 1 and "No PMs are due from Sep 29 to Oct 5, 2026." in body


# --- route sheets: isolation and cost ------------------------------------------------------------------------------------------

def test_another_tenants_devices_never_appear(client, signed_in, floor, other_tenant, pump_model):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Other ICU")
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        tech = Technician.objects.create(name="Olga Elsewhere")
        Credential.objects.create(technician=tech, scope=Scope.CATEGORY, value="Infusion pumps")
        pm_wo(dev("CE-THEIRS", dm, dept, SEP30), technician=tech, problem="Their PM")  # numbers are per tenant: theirs is WO-..-0001 too
    signed_in("director")
    for query in ("?day=2026-09-30", "?scope=week"):
        body = client.get(URL + query).content.decode()
        assert "CE-THEIRS" not in body and "Olga Elsewhere" not in body and "Other ICU" not in body
        assert "CE-V1" in body and [name for name, _, _ in sheets(body)][-1] == "No credentialed technician"


def test_queries_do_not_grow_with_the_devices(client, signed_in, ctx, dept, pump_model, monitor_model, crew):
    """1 device of each kind, then 15: the same number of queries for the day and the week (no query per row)."""
    signed_in("director")
    gone = Technician.objects.create(name="Sam Left", is_active=False)
    n = 0

    def add(k):
        nonlocal n
        for _ in range(k):
            n += 1
            day = TODAY + timedelta(days=1)
            pm_wo(dev(f"CE-H{n}", pump_model, dept, day, f"R{n}"), technician=crew["tom"])
            pm_wo(dev(f"CE-V{n}", pump_model, dept, day), vendor="BD Field Service")
            pm_wo(dev(f"CE-U{n}", pump_model, dept, day))
            pm_wo(dev(f"CE-I{n}", pump_model, dept, day), technician=gone)
            dev(f"CE-S{n}", pump_model, dept, day)
            dev(f"CE-N{n}", monitor_model, dept, day)

    def count(query):
        with CaptureQueriesContext(connection) as q:
            assert client.get(URL + query).status_code == 200
        return len(q)

    add(1)
    client.get(URL)  # warm up: the first request of a test may fill per-process caches
    small = {query: count(query) for query in ("?day=2026-09-30", "?scope=week")}
    add(14)
    assert {query: count(query) for query in ("?day=2026-09-30", "?scope=week")} == small
    assert client.get(URL + "?day=2026-09-30").content.decode().count('<span class="box"></span>') == 90


# --- report PDFs -----------------------------------------------------------------------------------------------------------

DISTINCT = {  # text only that report's partial prints, even for a small fleet with no work orders
    "cosr": "Red marker: 6% benchmark midpoint.",
    "compliance": "Compliance here means the share of active devices whose PM is not past due today.",
    "mtbf": "No repairs opened in the last 6 months.",
    "replace": "Score weighs age against expected life (50%)",
    "spend": "Repair work orders only; PM labor and service contracts are excluded.",
    "contract": "Contract cost is the annual cost of contracts that have not ended",
    "tech": "Vendor-performed work is excluded.",
    "recall": "This log is what a surveyor asks for",
}


def test_every_report_prints_with_its_title_and_content(client, signed_in, vent, pump, freeze_today):
    freeze_today(TODAY)
    signed_in("analyst")
    assert set(DISTINCT) == {m["key"] for m in REPORTS}
    for meta in REPORTS:
        r = client.get(f"/print/reports/{meta['key']}/")
        assert r.status_code == 200, meta["key"]
        body = r.content.decode()
        assert [t.name for t in r.templates[:2]] == ["web/print_report.html", "web/print_base.html"], meta["key"]
        assert f"web/_report_{meta['key']}.html" in [t.name for t in r.templates], meta["key"]
        assert f"<title>{meta['title']} · Riverside Regional</title>" in body
        assert f"<h1>{meta['title']}</h1>" in body and f'<p class="rp-sub">{meta["subtitle"]}</p>' in body
        assert "Riverside Regional · As of Sep 29, 2026" in body and DISTINCT[meta["key"]] in body, meta["key"]
        assert 'data-print' in body and "Save as PDF" in body
        assert 'class="nav"' not in body and 'class="topbar"' not in body  # no app shell
    body = client.get("/print/reports/compliance/").content.decode()
    assert '<svg class="chart"' in body and 'aria-label="PM completion rate by month"' in body
    assert "CE-10001" in client.get("/print/reports/replace/").content.decode()


def test_report_print_loads_the_app_stylesheet_in_the_light_theme(client, signed_in):
    signed_in("director")
    body = client.get("/print/reports/spend/").content.decode()
    head = body.split("</head>")[0]
    # print.css first, then cadence.css for the charts' tokens, then the page's own rules that keep it light and in print.css's layout
    assert head.index("web/print.css") < head.index("web/cadence.css") < head.index("<style>")
    style = head.split("<style>")[1]
    assert ':root,:root:not([data-theme="light"]),:root[data-theme="dark"]{' in style and "color-scheme:light" in style
    assert "--accent:#0B6F86" in style and "--ok:#1E7A3F" in style and "html,body{height:auto" in style
    assert ".rp-body a,.rp-body a.link,.rp-body a.tag{color:inherit;text-decoration:none}" in style


def test_report_print_keeps_the_screens_permission_flags(client, signed_in, tenant, pump_recall, freeze_today):
    freeze_today(TODAY)
    signed_in("analyst")  # recalls: View
    body = client.get("/print/reports/recall/").content.decode()
    assert "Alaris 8015 PCU" in body and '<a class="link" href="' in body and "Each row links to the alert" in body
    client.force_login(custom_user(tenant, {Module.REPORTS: Level.VIEW}))
    body = client.get("/print/reports/recall/").content.decode()
    assert "Alaris 8015 PCU" in body and '<a class="link"' not in body and "Each row links to the alert" not in body


def test_report_print_unknown_key_404_and_without_reports_view_403(client, ctx, make_user):
    client.force_login(make_user("director"))
    assert client.get("/print/reports/nope/").status_code == 404
    for slug in ("requester", "vendor"):  # Reports: None
        client.force_login(make_user(slug))
        assert client.get("/print/reports/cosr/").status_code == 403, slug
        assert client.get("/print/reports/nope/").status_code == 403, slug  # permission first: the key is not revealed


# --- the buttons that open them --------------------------------------------------------------------------------------------

def test_recalls_page_shows_response_log_only_with_reports_view(client, signed_in, tenant, pump_recall):
    signed_in("technician")  # recalls: View, reports: View
    body = client.get("/recalls/").content.decode()
    assert 'href="/print/reports/recall/" target="_blank" rel="noopener"' in body and " Response log</a>" in body
    client.force_login(custom_user(tenant, {Module.RECALLS: Level.VIEW}))
    r = client.get("/recalls/")
    assert r.status_code == 200 and "Response log" not in r.content.decode() and "/print/reports/" not in r.content.decode()


def test_reports_screen_links_each_report_to_its_pdf(client, signed_in):
    signed_in("analyst")
    for meta in REPORTS:
        body = client.get(f"/reports/{meta['key']}/").content.decode()
        assert f'<a class="btn sm" href="/print/reports/{meta["key"]}/" target="_blank" rel="noopener"' in body, meta["key"]


def test_pm_screen_links_route_sheets_with_its_filters(client, signed_in, ctx):
    signed_in("technician")
    body = client.get("/pm/?day=2026-09-30").content.decode()
    assert 'href="/print/route-sheets/?day=2026-09-30" data-act="with-filters" data-base="/print/route-sheets/" target="_blank"' in body


def test_overview_export_prints_the_overview(client, signed_in):
    signed_in("director")
    body = client.get("/").content.decode()
    button = re.search(r'<button class="btn" type="button" data-act="print"[^>]*>.*?</button>', body).group(0)
    assert button.endswith(" Export</button>") and "Print this overview, or save it as a PDF" in button


# --- printing the app's own pages --------------------------------------------------------------------------------------------

def _print_section():
    css = CSS.read_text()
    marker = "/* Slice 11: printing app pages */"
    assert css.count(marker) == 1
    return css[css.index(marker):]


def test_cadence_css_ends_with_the_print_section():
    section = _print_section()
    block = section[section.index("@media print{"):]
    # The @media print block closes at the very end of the file: nothing after the section.
    depth, end = 0, None
    for i, ch in enumerate(block):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0 and ch == "}":
            end = i
            break
    assert end is not None and block[end + 1:].strip() == ""


def test_print_section_hides_the_shell_and_forces_the_light_theme():
    block = _print_section().split("@media print{", 1)[1]
    hidden = next(rule for rule in block.split("}") if "display:none!important" in rule).split("{")[0].strip().split(",")
    for selector in (".topbar", ".nav", ".toolbar", ".page-head .actions", ".drawer", ".modal", ".scrim", ".toast", ".btn"):
        assert selector in hidden, selector
    assert ':root,:root:not([data-theme="light"]),:root[data-theme="dark"]{' in block and "color-scheme:light" in block
    assert "print-color-adjust:exact" in block and ".view{overflow:visible;padding:0}" in block and "html,body,.app{height:auto" in block
    assert "break-inside:avoid" in block and ".kpi," in block and ".panel," in block
