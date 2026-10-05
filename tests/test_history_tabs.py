"""Slice 20, part A: the History tab on records (apps.web.history_tabs over apps.core.history). The device and device model drawers
have it as a tab, the work order and contract drawers as a section that loads when asked; each shows its record's changes newest
first (a work order's with its labor and part lines', a model's with its AEM cases'), the latest 50 and then "Show older". Only for
View on the record's module and never for a scoped user (vendor technician, clinical requester): refused server-side, the partials
too. Also the words (labels, money, hours, choices, deleted rows, reasons that only repeat the change), paging past saves that
show nothing, tenant isolation, and the same pages under PostgreSQL's row-level security as the runtime role. Show older carries a
cursor (?older=: the last entry shown), so a change saved meanwhile never repeats or skips one. Never the name of someone of another
facility: the public request form's changes are nobody's ("Cadence"), and a change recorded by another facility's user reads as
"Someone outside this facility". An AEM end's typed reason is on the AEM case (PM View), never in the device model's own history."""
import re
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role, User, create_default_roles
from apps.contracts import services as ct
from apps.contracts.models import ContractType
from apps.core import history
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel
from apps.pm import aem
from apps.pm.models import AemDecision, AemStatus, PmProcedure
from apps.reports import custom
from apps.tenants.context import tenant_context
from apps.web import history_tabs
from apps.workorders import costs
from apps.workorders.models import PartLine, ServiceRequest, WorkOrder
from apps.workorders.services import assign, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
HX_DRAWER = {**HX, "HTTP_HX_TARGET": "drawer"}
HX_HIST = {**HX, "HTTP_HX_TARGET": "hist"}
HX_MORE = {**HX, "HTTP_HX_TARGET": "hist-more"}
TODAY = date.today()
VENDOR = "Hamilton Medical field service"


def entries(body: str) -> int:
    return body.count('class="hist-e"')


def titles(body: str) -> list[str]:
    return re.findall(r'<div class="hist-h"><span class="t">([^<]+)</span>', body)


def older_of(url: str) -> str:
    """The cursor a Show older URL carries (?older=)."""
    return parse_qs(urlparse(url).query)["older"][0]


def custom_user(tenant, levels: dict, slug="custom"):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


@pytest.fixture
def people(make_user):
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    requester = make_user("requester")
    requester.department = "ICU"
    requester.save()
    return {"director": make_user("director"), "vendor": vendor, "requester": requester}


@pytest.fixture
def world(ctx, dept, vent_model, techs, people):
    """CE-10001 (an ICU vent) added by the director, a vendor repair on it for Hamilton (the vendor's company, in the requester's
    unit) with a labor line, a part added and removed, and a contract the device is on."""
    kim = people["director"]
    asset = eq.create_asset(tag="CE-10001", device_model=vent_model, department=dept, by=kim, room="12")
    wo = create_work_order(asset=asset, type="repair", priority="high", problem="Alarm fault", requester="RN Lee")
    assign(wo, vendor_name=VENDOR, by=kim)
    costs.add_labor(wo, hours=Decimal("1.5"), worked_on=TODAY, by=kim)
    part = costs.add_part(wo, description="O2 cell", quantity=Decimal("2"), unit_cost=Decimal("45.50"), part_number="O2-7", by=kim)
    costs.remove_part(PartLine.objects.get(pk=part.pk), by=kim)
    contract = ct.create_contract(reference="SC-2026-118", vendor="Hamilton Medical", type=ContractType.OEM, start_on=TODAY - timedelta(days=100),
                                  end_on=TODAY + timedelta(days=265), annual_cost=Decimal("38500"), by=kim)
    ct.update_contract(contract, by=kim, annual_cost=Decimal("41000"))
    ct.add_asset(contract, Asset.objects.get(pk=asset.pk))
    return {"asset": Asset.objects.get(pk=asset.pk), "wo": wo, "contract": contract, "model": vent_model, "kim": kim}


# --- the device drawer -------------------------------------------------------------------------------------------------------

def test_the_device_drawer_has_a_history_tab_for_equipment_view(client, world, people):
    asset, kim = world["asset"], world["kim"]
    client.force_login(kim)
    r = client.post(f"/equipment/{asset.tag}/status/", {"to": "out_of_service"}, **HX)  # through the screen: the request's user is recorded
    assert r.status_code == 200
    overview = client.get(f"/equipment/{asset.tag}/", **HX_DRAWER)
    tab_button = f'hx-get="/equipment/{asset.tag}/?tab=history" hx-target="#drawer">History</button>'
    assert "history" in overview.context["tabs"] and tab_button in overview.content.decode()
    r = client.get(f"/equipment/{asset.tag}/?tab=history", **HX_DRAWER)
    body = r.content.decode()
    assert r.status_code == 200 and r.context["tab"] == "history" and r.templates[0].name == "web/_asset_drawer.html"
    assert titles(body) == ["Changed", "Changed", "Added"]  # out of service, the contract, added
    latest = r.context["hist"]["rows"][0]["e"]
    assert latest.who == "Director User" and latest.reason == ""  # "Status: Out of service" only repeated the change
    assert [(c.field, c.before, c.after) for c in latest.changes] == [("Status", "In service", "Out of service")]
    assert '<span class="was">In service</span> <span class="to" aria-label="changed to">→</span> Out of service' in body
    assert "<dt>Contract</dt><dd><span class=\"was\">—</span>" in body and "SC-2026-118 · Hamilton Medical" in body
    added = r.context["hist"]["rows"][-1]["e"]
    assert {c.field: c.after for c in added.changes}["Room"] == "12" and "Tag" not in {c.field for c in added.changes}
    assert "Support type" not in body  # it follows the contract, which is shown
    assert r.context["hist"]["more_url"] is None and 'id="hist-more"' not in body
    # Opened directly: the Equipment screen with the drawer open on History
    page = client.get(f"/equipment/{asset.tag}/?tab=history")
    assert page.status_code == 200 and "web/equipment.html" in [t.name for t in page.templates] and entries(page.content.decode()) == 3
    assert 'class="drawer open"' in page.content.decode()


def test_an_analyst_reads_device_history_and_a_role_without_equipment_view_cannot_open_it(client, world, tenant):
    analyst = User.objects.create_user(username="ann@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                       role=Role.objects.get(slug="analyst"))
    client.force_login(analyst)  # Equipment View, read only
    assert client.get(f"/equipment/{world['asset'].tag}/?tab=history", **HX_DRAWER).context["tab"] == "history"
    client.force_login(custom_user(tenant, {Module.WORKORDERS: Level.VIEW}))  # no Equipment View: the drawer itself is refused
    assert client.get(f"/equipment/{world['asset'].tag}/?tab=history", **HX_DRAWER).status_code == 403


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_scoped_users_get_no_device_history(client, world, people, who):
    tag = world["asset"].tag
    client.force_login(people[who])
    drawer = client.get(f"/equipment/{tag}/", **HX_DRAWER)  # their share: the drawer opens, without History
    assert drawer.status_code == 200 and "history" not in drawer.context["tabs"] and "?tab=history" not in drawer.content.decode()
    for url, headers in ((f"/equipment/{tag}/?tab=history", HX_DRAWER), (f"/equipment/{tag}/?tab=history", {}),
                         (f"/equipment/{tag}/?tab=history&older=50", HX_MORE), (f"/equipment/{tag}/?older=50", HX_MORE)):
        assert client.get(url, **headers).status_code == 403, url


# --- the work order drawer ---------------------------------------------------------------------------------------------------

def test_the_work_order_history_interleaves_its_labor_and_parts(client, world):
    wo, kim = world["wo"], world["kim"]
    client.force_login(kim)
    drawer = client.get(f"/work-orders/{wo.number}/", **HX_DRAWER).content.decode()
    assert f'hx-get="/work-orders/{wo.number}/?history=1" hx-target="#hist">Show changes</button>' in drawer
    assert 'class="hist-e"' not in drawer  # loads when asked
    r = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST)
    body = r.content.decode()
    assert r.status_code == 200 and [t.name for t in r.templates] == ["web/_history_entries.html"]
    assert titles(body) == ["Part removed", "Part added", "Labor added", "Changed", "Added"]
    rows = [row["e"] for row in r.context["hist"]["rows"]]
    removed, added_part, labor = rows[0], rows[1], rows[2]
    assert removed.who == "Director User" and {c.field: c.after for c in removed.changes} == {"Part": "O2 cell", "Part number": "O2-7", "Quantity": "2",
                                                                                              "Unit cost": "$45.50"}
    assert {c.field: c.after for c in labor.changes} == {"Date": f"{TODAY:%b} {TODAY.day}, {TODAY.year}", "Time": "1.5 hours", "Rate": "$215/h"}
    assert added_part.reason == "" and labor.reason == ""  # "Added" only repeated the action
    assigned = {c.field: (c.before, c.after) for c in rows[3].changes}
    assert assigned["Vendor"] == ("—", VENDOR) and assigned["Vendor service"] == ("No", "Yes")
    opened = {c.field: c.after for c in rows[4].changes}
    assert opened["Device"] == "CE-10001" and opened["Priority"] == "High" and opened["Problem"] == "Alarm fault" and "Number" not in opened
    # Opened directly: the Work orders screen with the drawer open and its history shown
    page = client.get(f"/work-orders/{wo.number}/?history=1")
    html = page.content.decode()
    assert "web/workorders.html" in [t.name for t in page.templates] and entries(html) == 5 and "Show changes</button>" not in html


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_scoped_users_see_the_work_order_but_not_its_history(client, world, people, who):
    number = world["wo"].number
    client.force_login(people[who])
    drawer = client.get(f"/work-orders/{number}/", **HX_DRAWER)
    assert drawer.status_code == 200 and not drawer.context["can_history"] and "Show changes" not in drawer.content.decode()
    for url, headers in ((f"/work-orders/{number}/?history=1", HX_HIST), (f"/work-orders/{number}/?history=1", HX_DRAWER),
                         (f"/work-orders/{number}/?history=1", {}), (f"/work-orders/{number}/?history=1&older=50", HX_MORE)):
        assert client.get(url, **headers).status_code == 403, url
    if who == "vendor":  # an action's answer re-renders the drawer: asking for history on it shows none (a requester cannot note)
        r = client.post(f"/work-orders/{number}/notes/?history=1", {"text": "On site"}, **HX_DRAWER)
        assert r.status_code == 200 and "hist" not in r.context and 'class="hist-e"' not in r.content.decode()


# --- the contract drawer ------------------------------------------------------------------------------------------------------

def test_the_contract_history_for_contracts_view(client, world, make_user, tenant):
    contract = world["contract"]
    client.force_login(make_user("technician"))  # Contracts View
    drawer = client.get(f"/contracts/{contract.pk}/", **HX_DRAWER).content.decode()
    assert f'hx-get="/contracts/{contract.pk}/?history=1" hx-target="#hist">Show changes</button>' in drawer
    r = client.get(f"/contracts/{contract.pk}/?history=1", **HX_HIST)
    body = r.content.decode()
    assert r.status_code == 200 and titles(body) == ["Changed", "Added"]
    change = r.context["hist"]["rows"][0]["e"]
    assert change.who == "Director User" and [(c.field, c.before, c.after) for c in change.changes] == [("Annual cost", "$38,500", "$41,000")]
    added = {c.field: c.after for c in r.context["hist"]["rows"][1]["e"].changes}
    assert added["Type"] == "OEM" and added["Coverage"] == "Full service" and added["Starts"].startswith(f"{TODAY - timedelta(days=100):%b}")
    page = client.get(f"/contracts/{contract.pk}/?history=1")
    assert page.status_code == 200 and "web/contracts.html" in [t.name for t in page.templates] and entries(page.content.decode()) == 2
    client.force_login(custom_user(tenant, {Module.EQUIPMENT: Level.VIEW}))  # no Contracts View: refused, history and all
    assert client.get(f"/contracts/{contract.pk}/?history=1", **HX_HIST).status_code == 403


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_scoped_users_get_no_contract_history(client, world, people, who):
    client.force_login(people[who])
    for headers in (HX_HIST, HX_DRAWER, HX_MORE, {}):
        assert client.get(f"/contracts/{world['contract'].pk}/?history=1", **headers).status_code == 403


# --- the device model drawer --------------------------------------------------------------------------------------------------

@pytest.fixture
def model_world(world, pump_model):
    kim = world["kim"]
    eq.update_device_model(pump_model, by=kim, list_cost=Decimal("3999.99"), expected_life_years=9)
    decision = AemDecision.objects.create(device_model=pump_model, interval_months=24, oem_interval_months=12, rationale="Self-test at power-on",
                                          evidence={"as_of": "2026-07-22", "since": "2023-07-22", "years": 3, "devices_active": 30, "devices_retired": 0,
                                                    "device_years": 76.7, "repairs": 3, "repairs_per_device_year": 0.04, "pm_on_time_pct": 100},
                                          proposed_by=kim, proposed_on=TODAY)
    aem.withdraw(decision, by=kim, reason="Waiting for another year of data")
    return pump_model


def test_the_model_history_has_its_aem_cases(client, world, model_world):
    dm = model_world
    client.force_login(world["kim"])
    program = client.get(f"/pm/models/{dm.pk}/", **HX_DRAWER).content.decode()
    assert f'hx-get="/pm/models/{dm.pk}/?tab=history" hx-target="#drawer">History</button>' in program
    r = client.get(f"/pm/models/{dm.pk}/?tab=history", **HX_DRAWER)
    body = r.content.decode()
    assert r.status_code == 200 and r.context["tab"] == "history"
    assert titles(body) == ["AEM case changed", "AEM case added", "Changed", "Added"]
    withdrawn, proposed, edited = (row["e"] for row in r.context["hist"]["rows"][:3])
    assert withdrawn.reason == "Withdrawn" and withdrawn.who == "Director User"
    assert ("Status", "Proposed", "Withdrawn") in [(c.field, c.before, c.after) for c in withdrawn.changes]
    assert {c.field: c.after for c in proposed.changes}["Failure history"] == (
        "Jul 22, 2023 to Jul 22, 2026: 30 devices, 76.7 device-years, 3 repairs (0.04 per device-year), 100% of PMs on time")
    assert "Device model" not in {c.field for c in proposed.changes} and proposed.who == "Cadence"  # saved outside a request
    assert [(c.field, c.before, c.after) for c in edited.changes] == [("Expected life", "8 years", "9 years"), ("List price", "$3,200", "$3,999.99")]
    page = client.get(f"/pm/models/{dm.pk}/?tab=history")
    assert page.status_code == 200 and "web/pm.html" in [t.name for t in page.templates] and entries(page.content.decode()) == 4


def test_pm_view_without_equipment_view_gets_no_model_history(client, world, model_world, tenant):
    dm = model_world
    client.force_login(custom_user(tenant, {Module.PM: Level.VIEW}))
    drawer = client.get(f"/pm/models/{dm.pk}/", **HX_DRAWER)
    assert drawer.status_code == 200 and "?tab=history" not in drawer.content.decode()
    assert [k for k, _label in drawer.context["tabs"]] == ["program", "procedure", "aem"]
    for headers in (HX_DRAWER, HX_MORE, {}):
        assert client.get(f"/pm/models/{dm.pk}/?tab=history", **headers).status_code == 403
    assert client.get(f"/pm/models/{dm.pk}/?tab=aem", **HX_DRAWER).status_code == 200  # the other tabs as before


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_scoped_users_never_open_the_model_drawer(client, world, model_world, people, who):
    client.force_login(people[who])
    assert client.get(f"/pm/models/{model_world.pk}/?tab=history", **HX_DRAWER).status_code == 403


# --- Show older ----------------------------------------------------------------------------------------------------------------

def test_a_long_history_shows_the_latest_50_then_older(client, world):
    asset, kim = world["asset"], world["kim"]
    for n in range(55):
        eq.update_asset(Asset.objects.get(pk=asset.pk), room=f"R{n}", by=kim)
    total = 2 + 55  # added, the contract, then the rooms
    client.force_login(kim)
    r = client.get(f"/equipment/{asset.tag}/?tab=history", **HX_DRAWER)
    body = r.content.decode()
    more_url = r.context["hist"]["more_url"]
    cursor = older_of(more_url)
    assert entries(body) == 50 and "R54" in body and more_url == f"/equipment/{asset.tag}/?" + urlencode({"tab": "history", "older": cursor})
    at, rank, _id = history.parse_cursor(cursor)  # the last entry shown: the next page starts right after it
    assert at == r.context["hist"]["rows"][-1]["e"].at and rank == history.RANKS["devices"]
    assert f'<div id="hist-more" class="hist-more"><button class="btn sm" type="button" hx-get="{escape(more_url)}" ' \
           f'hx-target="#hist-more" hx-swap="outerHTML">Show older</button></div>' in body
    more = client.get(more_url, **HX_MORE)
    older = more.content.decode()
    assert [t.name for t in more.templates] == ["web/_history_entries.html"] and entries(older) == total - 50
    assert titles(older)[-1] == "Added" and 'id="hist-more"' not in older and "No changes on record" not in older
    assert "R4</dd>" in older and "R5</dd>" in body  # newest first: R54 down to R5 on the first page, then R4 to R0, the contract, added
    page = client.get(more_url)  # opened directly: the screen with the drawer open at that page
    html = page.content.decode()
    assert entries(html) == total - 50 and "R4</dd>" in html and "R5</dd>" not in html and 'id="hist-more"' not in html
    assert 'class="drawer open"' in html
    nonsense = client.get(f"/equipment/{asset.tag}/?tab=history&older=nonsense", **HX_DRAWER)  # not a cursor: the newest page
    assert nonsense.status_code == 200 and entries(nonsense.content.decode()) == 50 and nonsense.context["hist"]["more_url"] == more_url
    for bad in ("50", "2026-10-05T12:00:00~0~1", "2026-10-05T12:00:00+00:00~99~1", "2026-10-05T12:00:00+00:00~0~x"):  # a count, naive, ...
        assert history.parse_cursor(bad) is None, bad
    long_ago = history.cursor_text((datetime(2000, 1, 1, tzinfo=dt_timezone.utc), 0, 1))  # before anything: nothing older
    assert entries(client.get(f"/equipment/{asset.tag}/?" + urlencode({"tab": "history", "older": long_ago}), **HX_MORE).content.decode()) == 0


def test_a_page_costs_the_same_however_long_the_history(client, world):
    asset, wo, kim = world["asset"], world["wo"], world["kim"]
    client.force_login(kim)

    def queries(url, headers):
        with CaptureQueriesContext(connection) as q:
            assert client.get(url, **headers).status_code == 200
        return len(q.captured_queries)

    device, lines = (f"/equipment/{asset.tag}/?tab=history", HX_DRAWER), (f"/work-orders/{wo.number}/?history=1", HX_HIST)
    before = queries(*device), queries(*lines)
    for n in range(30):
        eq.update_asset(Asset.objects.get(pk=asset.pk), room=f"R{n}", by=kim)
        costs.add_labor(wo, hours=Decimal("0.25"), worked_on=TODAY, by=kim)
    assert (queries(*device), queries(*lines)) == before


def test_a_page_opened_directly_shows_that_page(client, world, monkeypatch):
    monkeypatch.setattr(history_tabs, "PAGE", 2)
    wo = world["wo"]
    client.force_login(world["kim"])
    second = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST).context["hist"]["more_url"]
    page = client.get(second)  # without JavaScript: the Work orders screen with the drawer open on that page, not the newest through it
    html = page.content.decode()
    assert "web/workorders.html" in [t.name for t in page.templates] and titles(html) == ["Labor added", "Changed"]
    third = page.context["hist"]["more_url"]
    assert third.startswith(f"/work-orders/{wo.number}/?history=1&older=") and older_of(third) != older_of(second)
    assert titles(client.get(third).content.decode()) == ["Added"]


def test_the_wo_section_pages_with_the_section_url(client, world, monkeypatch):
    monkeypatch.setattr(history_tabs, "PAGE", 2)
    wo = world["wo"]
    client.force_login(world["kim"])
    first = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST)
    assert titles(first.content.decode()) == ["Part removed", "Part added"]
    section = f"/work-orders/{wo.number}/?history=1&older="
    assert first.context["hist"]["more_url"].startswith(section)
    second = client.get(first.context["hist"]["more_url"], **HX_MORE)
    assert titles(second.content.decode()) == ["Labor added", "Changed"] and second.context["hist"]["more_url"].startswith(section)
    last = client.get(second.context["hist"]["more_url"], **HX_MORE)
    assert titles(last.content.decode()) == ["Added"] and last.context["hist"]["more_url"] is None


def test_show_older_after_a_new_change_repeats_and_skips_nothing(client, world, monkeypatch):
    """The next page starts after the last entry shown (a cursor), not a count of entries: a labor line added while the first page
    is on screen neither pushes "Part added" onto the next page again nor hides anything."""
    monkeypatch.setattr(history_tabs, "PAGE", 2)
    wo, kim = world["wo"], world["kim"]
    client.force_login(kim)
    first = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST)
    assert titles(first.content.decode()) == ["Part removed", "Part added"]
    costs.add_labor(wo, hours=Decimal("0.25"), worked_on=TODAY, by=kim)  # the newest change now
    second = client.get(first.context["hist"]["more_url"], **HX_MORE)
    assert titles(second.content.decode()) == ["Labor added", "Changed"]
    assert {c.field: c.after for c in second.context["hist"]["rows"][0]["e"].changes}["Time"] == "1.5 hours"  # the older line, not the new one
    last = client.get(second.context["hist"]["more_url"], **HX_MORE)
    assert titles(last.content.decode()) == ["Added"] and last.context["hist"]["more_url"] is None
    newest = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST)  # from the top again: the new line first
    assert {c.field: c.after for c in newest.context["hist"]["rows"][0]["e"].changes}["Time"] == "0.25 hours"


# --- the reader under the tabs --------------------------------------------------------------------------------------------------

def test_pages_read_past_saves_that_show_nothing(world):
    asset, kim = world["asset"], world["kim"]
    for room in ("20", "21"):
        eq.update_asset(Asset.objects.get(pk=asset.pk), room=room, by=kim)
        for _ in range(3):
            Asset.objects.get(pk=asset.pk).save()  # nothing shown changed: these saves show no entry
    got, seen, after = [], [], None
    while True:
        page, after = history.record_history(Asset.objects.get(pk=asset.pk), limit=2, after=after)
        assert page or after is None
        seen.append(len(page))
        got += [e for e in page]
        if after is None:
            break
    assert [c.after for e in got for c in e.changes if c.field == "Room"][:2] == ["21", "20"]
    assert [e.action for e in got] == ["changed", "changed", "changed", "added"] and seen == [2, 2]


def test_the_words(world, people):
    kim = world["kim"]
    report = custom.create_custom_report(name="PM hours", source="labor", columns=["date", "who", "hours"], by=kim)
    custom.update_custom_report(report, by=kim, columns=["date", "who", "hours", "amount"], filters={"wo_type": ["pm"]}, sort="-hours")
    custom.update_custom_report(report, by=kim, group_by="who", sort="")
    newest, middle, added = history.entries_for(report)
    assert {c.field: c.after for c in added.changes} == {"Name": "PM hours", "Source": "Labor (time logged)",
                                                         "Columns": "Date worked, Technician or vendor, Hours"}
    assert {c.field: (c.before, c.after) for c in middle.changes} == {
        "Columns": ("Date worked, Technician or vendor, Hours", "Date worked, Technician or vendor, Hours, Amount"),
        "Filters": ("None", "Work order type: Preventive maintenance"), "Sorted by": ("The source's order", "Hours, highest first")}
    assert middle.reason == "" and {c.field: (c.before, c.after) for c in newest.changes} == {
        "Grouped by": ("Not grouped", "Technician or vendor"), "Sorted by": ("Hours, highest first", "The source's order")}
    # A deleted contract still reads by its name on the devices it covered
    contract = world["contract"]
    ct.delete_contract(contract)
    latest = history.entries_for(Asset.objects.get(pk=world["asset"].pk))[0]
    assert [(c.field, c.before, c.after) for c in latest.changes] == [("Contract", "SC-2026-118 · Hamilton Medical (deleted)", "—")]
    # A procedure's checklist reads as its steps; reworded, the same count says so
    proc =PmProcedure.objects.create(code="HAM-PM6", name="Vent PM", checklist=["Inspect", "Test alarms"])
    proc.checklist = ["Inspect housing", "Test alarms"]
    proc.save()
    assert [(c.field, c.before, c.after) for c in history.entries_for(proc)[0].changes] == [("Checklist", "2 steps", "2 steps (edited)")]


def test_scoped_users_have_no_history_area(world, people):
    assert history.readable_areas(people["vendor"]) == [] and history.readable_areas(people["requester"]) == []
    assert not history.can_read(people["vendor"], "work_orders") and history.can_read(people["director"], "work_orders")
    assert history.change_log(people["vendor"]) == ([], None)


def test_another_facility_never_shows(client, world, other_tenant, make_user):
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = eq.create_asset(tag="CE-10001", device_model=model, department=Department.objects.create(name="ICU"), room="THEIR-ROOM")
        eq.update_asset(theirs, room="THEIR-ROOM-2")
        assert len(history.entries_for(theirs)) == 2
        assert history.record_history(world["asset"])[0] == []  # our device, read inside their facility: nothing
    client.force_login(world["kim"])
    body = client.get("/equipment/CE-10001/?tab=history", **HX_DRAWER).content.decode()
    assert "THEIR-ROOM" not in body and entries(body) == 2
    client.force_login(make_user("director", tenant_=other_tenant, username="their-director@other.example"))
    assert client.get(f"/work-orders/{world['wo'].number}/?history=1", **HX_HIST).status_code == 404
    assert client.get(f"/contracts/{world['contract'].pk}/?history=1", **HX_HIST).status_code == 404


# --- who: never a name from another facility ---------------------------------------------------------------------------------------

@pytest.fixture
def stranger(other_tenant, make_user):
    """A director of another facility, by a name that must never show here."""
    create_default_roles(other_tenant)
    user = make_user("director", tenant_=other_tenant, username="sandra@other.example")
    user.first_name, user.last_name = "Sandra", "Stranger"
    user.save()
    return user


def portal_post(client, asset, **changes):
    data = {"asset_tag": asset.tag, "department": str(asset.department_id), "room": "Bed 4", "requester_name": "RN Lee", "callback": "x2210",
            "problem": "Alarm will not clear", "urgency": "high", "tagged_out": "on", **changes}
    return client.post(f"/r/{asset.tenant.slug}/", data)


def test_the_public_form_records_nobody_even_when_someone_is_signed_in_elsewhere(client, world, stranger):
    """Someone signed in at another facility sends this facility's public request form with "tagged out": the work order and the
    device's status change are recorded as nobody's (the form is anonymous), so their History shows "Cadence", never that name."""
    asset, kim = world["asset"], world["kim"]
    client.force_login(stranger)
    assert portal_post(client, asset).status_code == 302
    wo = ServiceRequest.objects.select_related("work_order").get().work_order
    assert Asset.objects.get(pk=asset.pk).status == "out_of_service"
    assert [rec.history_user_id for rec in WorkOrder.history.filter(id=wo.pk)] == [None]
    tagged = Asset.history.filter(id=asset.pk).order_by("-history_date", "-history_id").first()
    assert tagged.status == "out_of_service" and tagged.history_user_id is None
    client.force_login(kim)
    w = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST)
    opened = w.context["hist"]["rows"][-1]["e"]
    assert opened.action == "added" and (opened.who, opened.who_id) == (history.SYSTEM, None)
    d = client.get(f"/equipment/{asset.tag}/?tab=history", **HX_DRAWER)
    out = d.context["hist"]["rows"][0]["e"]
    assert ("Status", "In service", "Out of service") in [(c.field, c.before, c.after) for c in out.changes] and (out.who, out.who_id) == (history.SYSTEM, None)
    for body in (w.content.decode(), d.content.decode()):
        assert "Stranger" not in body and "Sandra" not in body and history.OUTSIDE not in body
    assert portal_post(client, asset, tagged_out="").status_code == 302  # signed in here, the form is still anonymous
    assert WorkOrder.history.filter(history_type="+").exclude(id=wo.pk).exclude(id=world["wo"].pk).get().history_user_id is None


def test_a_change_recorded_for_someone_of_another_facility_reads_as_someone_outside(client, world, stranger):
    asset, kim = world["asset"], world["kim"]
    eq.update_asset(Asset.objects.get(pk=asset.pk), room="30", by=kim)
    Asset.history.filter(id=asset.pk, room="30").update(history_user=stranger)  # as a save from the portal was recorded before the fix
    client.force_login(kim)
    r = client.get(f"/equipment/{asset.tag}/?tab=history", **HX_DRAWER)
    latest = r.context["hist"]["rows"][0]["e"]
    assert (latest.who, latest.who_id) == (history.OUTSIDE, None) and [(c.field, c.after) for c in latest.changes] == [("Room", "30")]
    body = r.content.decode()
    assert "Someone outside this facility" in body and "Stranger" not in body and "Sandra" not in body
    assert all(row["e"].who == "Director User" for row in r.context["hist"]["rows"][1:] if row["e"].who_id)  # this facility's people by name


# --- AEM cases: the end's reason, and a model deleted since --------------------------------------------------------------------------

def test_an_aem_ends_typed_reason_stays_on_the_case_not_in_the_models_history(client, world, model_world, tenant):
    dm, kim = model_world, world["kim"]
    why = "Repairs rose after the firmware update"
    decision = AemDecision.objects.create(device_model=dm, status=AemStatus.APPROVED, interval_months=18, oem_interval_months=12, rationale="On file",
                                          proposed_by=kim, proposed_on=TODAY - timedelta(days=60), decided_on=TODAY - timedelta(days=30),
                                          decision_note="EMC minutes, item 4")
    aem.end(decision, by=kim, reason=why)
    own = history.entries_for(DeviceModel.objects.get(pk=dm.pk))
    assert own[0].reason == "AEM ended" and [(c.field, c.before, c.after) for c in own[0].changes] == [("AEM interval", "18 months", "—")]
    assert not any(why in e.reason or any(why in c.after or why in c.before for c in e.changes) for e in own)
    case = history.entries_for(AemDecision.objects.get(pk=decision.pk))[0]
    assert ("Why it ended", why) in [(c.field, c.after) for c in case.changes]
    # Equipment View reads the model's history in the change log; without PM View the case (and its reason) never shows there
    auditor = custom_user(tenant, {Module.USERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW}, slug="auditor")
    log, _ = history.change_log(auditor)
    assert "device_models" in {e.area for e in log} and "aem" not in {e.area for e in log}
    assert why not in repr(log)
    client.force_login(kim)  # PM View and Equipment View: the model's History tab has the case with its reason
    tab = client.get(f"/pm/models/{dm.pk}/?tab=history", **HX_DRAWER).content.decode()
    assert why in tab and "AEM ended" in tab


def test_the_records_of_deleted_rows_keep_their_names_and_lose_their_links(world, model_world):
    kim, wo = world["kim"], world["wo"]
    name = f"{model_world.manufacturer} {model_world.model}"
    DeviceModel.objects.get(pk=model_world.pk).delete()  # its AEM cases go with it; their history stays
    cases, _ = history.change_log(kim, areas=["aem"])
    assert [e.action for e in cases] == ["deleted", "changed", "added"]
    assert all(e.record == f"AEM · {name} (deleted)" and e.url is None for e in cases)
    number = wo.number
    WorkOrder.objects.get(pk=wo.pk).delete()  # its labor and part lines go with it
    lines, _ = history.change_log(kim, areas=["labor", "parts"])
    assert {e.record for e in lines} == {f"{number} (deleted) labor", f"{number} (deleted) part"} and all(e.url is None for e in lines)


# --- PostgreSQL, as the runtime role ---------------------------------------------------------------------------------------------

@needs_postgres
def test_the_history_tabs_under_row_level_security(client, world, model_world, people, other_tenant, monkeypatch):
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = Asset.objects.create(tag="CE-10001", device_model=model, department=Department.objects.create(name="ICU"), room="THEIR-ROOM")
        theirs.room = "THEIR-ROOM-2"
        theirs.save()
    ct.delete_contract(world["contract"])  # the device's history names it from the contract's own history, under the policies
    client.force_login(world["kim"])
    as_app_role()
    asset, wo, dm = world["asset"], world["wo"], model_world
    client.post(f"/equipment/{asset.tag}/status/", {"to": "out_of_service"}, **HX)
    r = client.get(f"/equipment/{asset.tag}/?tab=history", **HX_DRAWER)
    body = r.content.decode()
    assert r.status_code == 200 and titles(body)[:2] == ["Changed", "Changed"] and "THEIR-ROOM" not in body
    assert "SC-2026-118 · Hamilton Medical (deleted)" in body and r.context["hist"]["rows"][0]["e"].who == "Director User"
    assert client.get(f"/equipment/{asset.tag}/?tab=history").status_code == 200
    w = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST).content.decode()
    assert titles(w) == ["Part removed", "Part added", "Labor added", "Changed", "Added"]
    m = client.get(f"/pm/models/{dm.pk}/?tab=history", **HX_DRAWER).content.decode()
    assert titles(m) == ["AEM case changed", "AEM case added", "Changed", "Added"]
    monkeypatch.setattr(history_tabs, "PAGE", 1)
    after_first = client.get(f"/equipment/{asset.tag}/?tab=history", **HX_DRAWER).context["hist"]["more_url"]  # the cursor of the newest entry
    monkeypatch.setattr(history_tabs, "PAGE", 50)
    more = client.get(after_first, **HX_MORE).content.decode()
    assert entries(more) == len(titles(body)) - 1
    client.force_login(people["vendor"])
    assert client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST).status_code == 403
