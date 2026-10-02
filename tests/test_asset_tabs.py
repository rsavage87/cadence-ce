"""The device drawer's PM schedule and Costs tabs (slice 12): who sees them, OEM and AEM wording, the PM procedure, upcoming and
projected PMs with who does them, PM history, service cost by year with its stats and the contract share, the replacement note,
bounded queries, and tenant isolation."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Level, Module, Role, User
from apps.contracts import services as ct_services
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment import services as eq_services
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.reports.fleet import replacement_score
from apps.tenants.context import tenant_context
from apps.web import asset_tabs
from apps.workorders.models import LaborLine, PartLine, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def custom_user(tenant, levels, slug="custom"):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


def tab(client, asset, name):
    r = client.get(f"/equipment/{asset.tag}/?tab={name}", **HX)
    assert r.status_code == 200
    return r


def work_order(asset, *, type=WoType.PM, opened, technician=None, vendor=""):
    wo = create_work_order(asset=asset, type=type, priority="normal", problem="Scheduled PM" if type == WoType.PM else "Alarm fault", opened_on=opened)
    return assign(wo, technician=technician, vendor_name=vendor) if (technician or vendor) else wo


def completed(asset, on, *, type=WoType.PM, labor=(), parts=(), technician=None, vendor="", resolution=""):
    """A work order opened and completed on `on`, with labor lines (hours, rate) and part lines (quantity, unit cost)."""
    wo = work_order(asset, type=type, opened=on, technician=technician, vendor=vendor)
    for hours, rate in labor:
        LaborLine.objects.create(work_order=wo, hours=Decimal(str(hours)), rate=Decimal(str(rate)))
    for quantity, unit_cost in parts:
        PartLine.objects.create(work_order=wo, description="Part", quantity=Decimal(str(quantity)), unit_cost=Decimal(str(unit_cost)))
    change_status(wo, WoStatus.IN_PROGRESS, as_of=on)
    change_status(wo, WoStatus.COMPLETED, as_of=on)
    if resolution:  # no service writes the resolution yet (the API serializer does); set it as that would
        wo.resolution = resolution
        wo.save(update_fields=["resolution", "updated_at"])
    return wo


def set_next_pm(asset, day):
    asset.next_pm_on = day
    asset.save(update_fields=["next_pm_on", "updated_at"])


# --- who sees the tabs ------------------------------------------------------------------------------------------------------


def test_default_roles_see_the_tabs_their_modules_allow(client, signed_in, vent):
    signed_in("technician")  # PM: Edit, work orders: Edit
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert "?tab=pm" in body and ">PM schedule</button>" in body and "?tab=costs" in body and ">Costs</button>" in body
    assert "Maintenance strategy" in tab(client, vent, "pm").content.decode()
    assert "Service cost by year" in tab(client, vent, "costs").content.decode()

    client.logout()
    user = signed_in("requester")  # PM: None
    user.department = "ICU"  # slice 16: a requester sees their own unit's devices
    user.save()
    r = tab(client, vent, "pm")
    body = r.content.decode()
    assert r.context["tab"] == "overview" and "?tab=pm" not in body and "Maintenance strategy" not in body


def test_pm_view_alone_gives_the_pm_tab_without_costs_or_work_order_links(client, tenant, ctx, vent):
    wo = completed(vent, date.today() - timedelta(days=20))
    client.force_login(custom_user(tenant, {Module.EQUIPMENT: Level.VIEW, Module.PM: Level.VIEW}))
    overview = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert "?tab=pm" in overview and "?tab=costs" not in overview
    wo.resolution = "Replaced flow sensor"
    wo.save(update_fields=["resolution"])
    body = tab(client, vent, "pm").content.decode()
    # Work-order details stay behind Work orders View, as on the drawer's Work orders tab: the dates the device carries are shown
    assert "Maintenance strategy" in body and "PM work order details need access to work orders" in body
    assert wo.number not in body and "Replaced flow sensor" not in body and f"/work-orders/{wo.number}/" not in body
    r = tab(client, vent, "costs")  # typed by hand: the tab does not exist for this role
    assert r.context["tab"] == "overview" and "Service cost by year" not in r.content.decode() and "costs" not in r.context


def test_reports_view_alone_gives_costs_without_the_pm_tab(client, tenant, ctx, vent):
    client.force_login(custom_user(tenant, {Module.EQUIPMENT: Level.VIEW, Module.REPORTS: Level.VIEW}))
    overview = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert "?tab=costs" in overview and "?tab=pm" not in overview
    assert "Service cost by year" in tab(client, vent, "costs").content.decode()
    r = tab(client, vent, "pm")
    assert r.context["tab"] == "overview" and "Maintenance strategy" not in r.content.decode() and "pm" not in r.context


@pytest.mark.parametrize("role", ["vendor", "requester"])
def test_vendor_and_requester_roles_see_no_costs_tab(client, make_user, ctx, vent, role):
    """Their Work orders access does not open the facility's service spend, contract share, or replacement outlook."""
    user = make_user(role)
    user.department, user.company = "ICU", "Hamilton Medical"  # slice 16: their unit's devices, or those with their company's work
    user.save()
    create_work_order(asset=vent, type="repair", priority="normal", problem="Flow sensor fault", vendor_service=True,
                      vendor_name="Hamilton Medical field service")
    client.force_login(user)
    assert "?tab=costs" not in client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert tab(client, vent, "costs").context["tab"] == "overview"


def test_neither_tab_without_pm_or_reports_view(client, tenant, ctx, vent):
    client.force_login(custom_user(tenant, {Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.EDIT}))
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert "?tab=pm" not in body and "?tab=costs" not in body
    for name in ("pm", "costs"):
        assert tab(client, vent, name).context["tab"] == "overview"


def test_another_tenants_device_is_a_404_on_either_tab(client, signed_in, other_tenant):
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="X", model="Y", description="Pump", category="Pumps")
        theirs = Asset.objects.create(tag="CE-77777", device_model=model, department=Department.objects.create(name="ER"))
    signed_in("director")
    for name in ("pm", "costs"):
        assert client.get(f"/equipment/{theirs.tag}/?tab={name}", **HX).status_code == 404


# --- maintenance strategy -----------------------------------------------------------------------------------------------------


def test_strategy_note_says_oem_or_aem(ctx, vent_model, pump_model):
    assert asset_tabs.strategy_note(vent_model) == "Following the OEM schedule: every 6 months."
    assert asset_tabs.strategy_note(pump_model) == ("AEM program: the PM interval for this model is extended from the OEM's 12 months to 18 months. "
                                                    "Life-support devices are excluded from AEM by policy.")
    shorter = DeviceModel.objects.create(manufacturer="GE", model="B40", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM,
                                         oem_pm_interval_months=12, aem_interval_months=6)
    assert "shortened from the OEM's 12 months to 6 months" in asset_tabs.strategy_note(shorter)
    monthly = DeviceModel.objects.create(manufacturer="GE", model="M1", description="Monitor", category="Monitors", oem_pm_interval_months=1)
    assert asset_tabs.strategy_note(monthly) == "Following the OEM schedule: every 1 month."


def test_life_support_with_an_aem_interval_on_file_still_follows_oem(ctx, vent_model):
    vent_model.aem_interval_months = 12
    vent_model.save()
    proc = PmProcedure.objects.create(code="HAM-G5-PM6", name="Semiannual PM")
    assert asset_tabs.strategy_note(vent_model, proc) == ("Following the OEM schedule: every 6 months per HAM-G5-PM6. "
                                                          "An AEM interval of 12 months is on file, but life-support devices never go on AEM.")


def test_pm_tab_shows_the_aem_note_for_an_aem_model(client, signed_in, pump):
    signed_in("technician")
    body = tab(client, pump, "pm").content.decode()
    assert "extended from the OEM&#x27;s 12 months to 18 months" in body and "<span>every 18 months</span>" in body


# --- the procedure ------------------------------------------------------------------------------------------------------------


def test_procedure_steps_of_both_shapes_and_its_source(client, signed_in, vent, vent_model):
    vent_model.pm_procedure = PmProcedure.objects.create(
        code="HAM-G5-PM6", name="Hamilton-G5 semiannual PM", source_kind=PmProcedure.SourceKind.OEM, source_reference="G5 Service Manual",
        revision="4.1", source_url="https://docs.example.com/g5.pdf", estimated_hours=Decimal("2.50"),
        checklist=["Inspect circuit and filters", {"text": "Leakage current", "measure": "µA, limit 100"}, {"text": "Verify tidal volume", "measure": True}])
    vent_model.save()
    signed_in("technician")
    body = tab(client, vent, "pm").content.decode()
    assert "<h3>PM procedure <span>HAM-G5-PM6 · 2.50 h estimated</span></h3>" in body and "Hamilton-G5 semiannual PM" in body
    assert "<li>Inspect circuit and filters</li>" in body
    assert '<li>Leakage current <span class="muted">· record µA, limit 100</span></li>' in body
    assert '<li>Verify tidal volume <span class="muted">· record a reading</span></li>' in body
    assert "Source: OEM service manual · G5 Service Manual · revision 4.1" in body and 'href="https://docs.example.com/g5.pdf"' in body
    assert "every 6 months per HAM-G5-PM6." in body


def test_a_checklist_saved_as_text_and_a_source_that_is_not_a_web_link(ctx, vent, vent_model):
    vent_model.pm_procedure = PmProcedure.objects.create(code="P1", name="PM", checklist="Inspect\n\nTest alarms", source_url="javascript:alert(1)")
    vent_model.save()
    pm = asset_tabs.pm_tab(vent)["pm"]
    assert [s["text"] for s in pm["steps"]] == ["Inspect", "Test alarms"] and pm["source_url"] == ""


def test_no_procedure_on_file_and_a_procedure_without_steps(client, signed_in, vent, vent_model):
    signed_in("technician")
    body = tab(client, vent, "pm").content.decode()
    assert "No PM procedure on file for this model." in body and "estimated at 1 hour," in body and 'class="check"' not in body
    vent_model.pm_procedure = PmProcedure.objects.create(code="P1", name="PM", checklist=[])
    vent_model.save()
    assert "This procedure has no steps on file." in tab(client, vent, "pm").content.decode()


# --- upcoming ---------------------------------------------------------------------------------------------------------------


def test_next_pm_and_two_projected_with_the_suggested_technician(client, signed_in, vent, techs):
    today = date.today()
    up = asset_tabs.pm_tab(vent)["pm"]["upcoming"]
    first = today + timedelta(days=10)
    assert [r["on"] for r in up["rows"]] == [first, add_months(first, 6), add_months(add_months(first, 6), 6)]
    assert [r["overdue"] for r in up["rows"]] == [False, False, False] and up["open_pm"] is None
    assert up["who"] == {"kind": "suggested", "name": "Dana Whitfield"}  # the only one credentialed for the Hamilton-G5
    signed_in("technician")
    body = tab(client, vent, "pm").content.decode()
    assert f"{first:%b} {first.day}, {first.year}" in body and "Next due · Dana Whitfield, suggested" in body
    assert body.count("Projected · Dana Whitfield, suggested") == 2 and "Due in 10 d" in body
    assert "The technician is the one the PM schedule suggests" in body


def test_the_suggestion_is_the_least_loaded_credentialed_technician(ctx, pump, techs):
    # Both are credentialed for infusion pumps; ties go by name, so Dana, until she has more open work than Tom.
    assert asset_tabs.pm_tab(pump)["pm"]["upcoming"]["who"]["name"] == "Dana Whitfield"
    other = Asset.objects.create(tag="CE-20000", device_model=pump.device_model, department=pump.department, next_pm_on=date.today() + timedelta(days=200))
    work_order(other, type=WoType.REPAIR, opened=date.today(), technician=techs["dana"])
    assert asset_tabs.pm_tab(pump)["pm"]["upcoming"]["who"]["name"] == "Tom Okafor"


def test_inside_the_week_the_suggestion_follows_the_pm_schedules_week_plan(ctx, pump, techs):
    """Two pumps due tomorrow: the week plan gives the first (by tag) to Dana and the next to Tom, and the drawer says the same."""
    from apps.pm.schedule import week_plan

    tomorrow = date.today() + timedelta(days=1)
    set_next_pm(pump, tomorrow)
    second = Asset.objects.create(tag="CE-10003", device_model=pump.device_model, department=pump.department, next_pm_on=tomorrow)
    plan = week_plan(date.today())["suggested"]
    for asset in (pump, second):
        assert asset_tabs.pm_tab(asset)["pm"]["upcoming"]["who"]["name"] == plan[asset.id].name
    assert {plan[pump.id].name, plan[second.id].name} == {"Dana Whitfield", "Tom Okafor"}


def test_overdue_next_pm_is_red_and_the_projection_starts_today(client, signed_in, vent, techs):
    today = date.today()
    set_next_pm(vent, today - timedelta(days=20))
    rows = asset_tabs.pm_tab(vent)["pm"]["upcoming"]["rows"]
    assert rows[0]["overdue"] and rows[1]["on"] == add_months(today, 6) and rows[2]["on"] == add_months(add_months(today, 6), 6)
    signed_in("technician")
    body = tab(client, vent, "pm").content.decode()
    assert '<span class="rail crit"></span>' in body and '<div class="t down">' in body and "Overdue 20 d" in body
    assert "the overdue one today" in body


def test_an_open_pm_work_order_shows_its_assignee_and_number(client, signed_in, pump, techs):
    today = date.today()
    later = work_order(pump, opened=today, technician=techs["dana"])
    first = work_order(pump, opened=today - timedelta(days=3), technician=techs["tom"])  # the earliest open one is the next PM's
    up = asset_tabs.pm_tab(pump)["pm"]["upcoming"]
    assert up["open_pm"].number == first.number and up["who"] == {"kind": "assigned", "name": "Tom Okafor"}
    signed_in("technician")
    body = tab(client, pump, "pm").content.decode()
    assert "Next due · Tom Okafor<" in body and f'hx-get="/work-orders/{first.number}/" hx-target="#drawer">{first.number}</a>' in body
    assert f"The next PM is on {first.number}." in body and later.number in body  # the other one is in the history


def test_an_unassigned_open_pm_shows_the_suggestion_and_a_vendor_one_the_vendor(ctx, pump, techs):
    wo = work_order(pump, opened=date.today())
    up = asset_tabs.pm_tab(pump)["pm"]["upcoming"]
    assert up["open_pm"].number == wo.number and up["who"] == {"kind": "suggested", "name": "Dana Whitfield"}
    assign(wo, vendor_name="BD Field Service")
    assert asset_tabs.pm_tab(pump)["pm"]["upcoming"]["who"] == {"kind": "vendor", "name": "BD Field Service"}


def test_nobody_credentialed(client, signed_in, vent):
    signed_in("technician")
    body = tab(client, vent, "pm").content.decode()
    assert "Next due · No credentialed technician" in body and "No active technician is credentialed for this device." in body


def test_a_retired_device_is_not_scheduled(client, signed_in, vent):
    eq_services.set_status(vent, AssetStatus.RETIRED)
    pm = asset_tabs.pm_tab(vent)["pm"]["upcoming"]
    assert pm["rows"] == [] and pm["unscheduled"] == "Retired devices are not scheduled."
    signed_in("director")
    body = tab(client, vent, "pm").content.decode()
    assert "Retired devices are not scheduled." in body and "Next due" not in body


def test_a_device_in_use_without_a_next_pm_date(ctx, vent):
    set_next_pm(vent, None)
    assert asset_tabs.pm_tab(vent)["pm"]["upcoming"]["unscheduled"] == "No next PM date on file for this device."


# --- history ------------------------------------------------------------------------------------------------------------------


def test_history_lists_the_devices_pm_work_orders_newest_first(client, signed_in, vent, techs):
    today = date.today()
    old = completed(vent, date(today.year - 1, 3, 10), technician=techs["dana"], labor=[(1.5, 82), (0.5, 82)], resolution="Pass. Replaced the O2 cell.")
    recent = completed(vent, today - timedelta(days=40), vendor="Hamilton Service")
    cancelled = work_order(vent, opened=today - timedelta(days=200))
    change_status(cancelled, WoStatus.CANCELLED)
    open_pm = work_order(vent, opened=today)
    repair = completed(vent, today - timedelta(days=5), type=WoType.REPAIR, labor=[(3, 82)])

    rows = asset_tabs.pm_tab(vent)["pm"]["history"]
    assert [h["wo"].number for h in rows] == [open_pm.number, recent.number, cancelled.number, old.number]  # no repair
    assert [h["who"] for h in rows] == ["", "Hamilton Service", "", "Dana Whitfield"]
    assert [h["result"] for h in rows] == ["Open", "Completed", "Cancelled", "Pass. Replaced the O2 cell."]
    assert [h["hours"] for h in rows] == [None, None, None, Decimal("2.00")]

    signed_in("technician")
    body = tab(client, vent, "pm").content.decode()
    assert "<span>4 PM work orders</span>" in body and repair.number not in body.split("<h3>History")[1]
    assert f'<a class="link" href="/work-orders/{old.number}/" hx-get="/work-orders/{old.number}/" hx-target="#drawer">{old.number}</a>' in body
    assert f"Mar 10, {today.year - 1}" in body and "Pass. Replaced the O2 cell." in body and '<td class="num">2.00</td>' in body
    assert 'Hamilton Service <span class="muted">· vendor</span>' in body and '<span class="muted">Unassigned</span>' in body
    assert '<span class="chip neutral">Open</span>' in body and '<span class="chip neutral">Cancelled</span>' in body


def test_history_shows_the_most_recent_and_says_so(ctx, vent, monkeypatch):
    monkeypatch.setattr(asset_tabs, "HISTORY_LIMIT", 2)
    today = date.today()
    wos = [completed(vent, today - timedelta(days=d)) for d in (300, 200, 100)]
    pm = asset_tabs.pm_tab(vent)["pm"]
    assert [h["wo"].number for h in pm["history"]] == [wos[2].number, wos[1].number] and pm["history_total"] == 3 and pm["history_more"]


def test_no_pm_history(client, signed_in, vent):
    signed_in("technician")
    assert "No PM work orders on file for this device." in tab(client, vent, "pm").content.decode()


# --- service cost by year ---------------------------------------------------------------------------------------------------


def test_cost_by_year_counts_labor_and_parts_of_work_orders_completed_each_year(client, signed_in, vent):
    y = date.today().year
    vent.installed_on = date(y - 7, 5, 1)
    vent.save()
    completed(vent, date(y, 1, 1), type=WoType.REPAIR, labor=[(2, 82)], parts=[(2, 150)])  # 164 + 300
    completed(vent, date(y - 1, 6, 15), labor=[(1.5, 82)])  # 123
    completed(vent, date(y - 3, 2, 2), type=WoType.REPAIR, parts=[(1, 1000)])
    completed(vent, date(y - 6, 2, 2), type=WoType.REPAIR, parts=[(1, 5000)])  # before the window
    in_progress = work_order(vent, type=WoType.REPAIR, opened=date(y, 1, 1))
    LaborLine.objects.create(work_order=in_progress, hours=3, rate=82)  # not completed, so not a cost yet

    c = asset_tabs.costs_tab(vent)["costs"]
    assert c["years"] == [y - 4, y - 3, y - 2, y - 1, y]
    assert c["by_year"] == {y - 4: 0.0, y - 3: 1000.0, y - 2: 0.0, y - 1: 123.0, y: 464.0}
    assert c["total"] == 1587.0 and c["pct"] == pytest.approx(1587 / 38000 * 100)
    assert [r["full_label"] for r in c["chart"]["rows"]] == [str(v) for v in c["years"]]

    signed_in("technician")
    body = tab(client, vent, "costs").content.decode()
    assert '<div class="v">$1,587</div><div class="l">service cost since ' + str(y - 4) in body
    assert '<div class="v">4.2%</div><div class="l">of acquisition cost</div>' in body
    assert f"<title>{y - 3}: $1,000</title>" in body and f"<title>{y}: $464</title>" in body and f"<title>{y - 2}: $0</title>" in body
    assert "Not under a service contract." in body


def test_a_device_installed_this_year_shows_one_year(ctx, vent):
    y = date.today().year
    vent.installed_on = date(y, 1, 1)
    vent.save()
    assert asset_tabs.costs_tab(vent)["costs"]["years"] == [y]
    vent.installed_on = date(y + 1, 6, 1)  # dated ahead by mistake: still this year
    assert asset_tabs.costs_tab(vent)["costs"]["years"] == [y]


def test_no_install_date_shows_five_years_and_no_cost_shows_no_chart(client, signed_in, pump):
    y = date.today().year
    c = asset_tabs.costs_tab(pump)["costs"]
    assert c["years"] == list(range(y - 4, y + 1)) and c["total"] == 0 and c["chart"] is None and c["pct"] == 0.0
    signed_in("technician")
    body = tab(client, pump, "costs").content.decode()
    assert f"No labor or parts on work orders completed since {y - 4}." in body and '<div class="v">0.0%</div>' in body


def test_no_acquisition_cost_leaves_the_percentage_blank(client, signed_in, vent):
    vent.acquisition_cost = 0
    vent.save()
    completed(vent, date.today(), labor=[(1, 82)])
    assert asset_tabs.costs_tab(vent)["costs"]["pct"] is None
    signed_in("technician")
    body = tab(client, vent, "costs").content.decode()
    assert '<div class="v">—</div><div class="l">of acquisition cost</div>' in body and "No acquisition cost on file" in body


def test_contract_share_for_a_live_contract_and_none_for_an_expired_one(client, signed_in, vent, pump):
    today = date.today()
    contract = ct_services.create_contract(reference="SC-2026-118", vendor="Hamilton Medical", start_on=today - timedelta(days=100),
                                           end_on=today + timedelta(days=200), annual_cost=Decimal("10000"))
    ct_services.add_asset(contract, vent)
    ct_services.add_asset(contract, pump)
    share = 10000 * 38000 / (38000 + 3200)  # Contract.cost_share_for: by acquisition cost
    assert asset_tabs.costs_tab(vent)["costs"]["share"] == pytest.approx(share)
    signed_in("technician")
    body = tab(client, vent, "costs").content.decode()
    assert '<div class="v">$9,223</div><div class="l">annual contract share</div>' in body
    assert "SC-2026-118 · Hamilton Medical: $10,000 a year, shared across its devices by acquisition cost." in body

    ct_services.update_contract(contract, start_on=today - timedelta(days=400), end_on=today - timedelta(days=1))
    vent.refresh_from_db()
    assert asset_tabs.costs_tab(vent)["costs"]["share"] is None
    body = tab(client, vent, "costs").content.decode()
    assert '<div class="v">—</div><div class="l">annual contract share</div>' in body and "SC-2026-118 · Hamilton Medical ended" in body


def test_a_retired_device_has_no_contract_share(ctx, vent):
    today = date.today()
    contract = ct_services.create_contract(reference="SC-1", vendor="Hamilton Medical", start_on=today, end_on=today + timedelta(days=200),
                                           annual_cost=Decimal("5000"))
    ct_services.add_asset(contract, vent)
    eq_services.set_status(vent, AssetStatus.RETIRED)
    c = asset_tabs.costs_tab(vent)["costs"]
    assert c["share"] is None and c["outlook"]["note"] == "Retired. Replacement planning scores devices in use only."


# --- replacement outlook ------------------------------------------------------------------------------------------------------


def years_ago(n: float) -> date:
    return date.today() - timedelta(days=round(n * 365.25))


def test_replacement_note_for_a_device_with_life_left(ctx, vent):
    o = asset_tabs.costs_tab(vent)["costs"]["outlook"]
    age = vent.age_years()
    score = round(replacement_score(age, 10, 0, 3) * 100)
    assert o["score_pct"] == score
    assert o["note"] == (f"About {10 - age:.1f} years of its 10-year expected life remaining. Replacement score {score} of 100, from age, "
                         "no repairs in the last 6 months, and condition 3 of 5. No replacement action needed.")


def test_replacement_note_past_and_near_expected_life(ctx, vent):
    vent.installed_on = years_ago(12)
    vent.save()
    note = asset_tabs.costs_tab(vent)["costs"]["outlook"]["note"]
    assert note.startswith("Past its 10-year expected life by 2.0 years.") and note.endswith("A replacement candidate; estimated replacement $39,900.")
    vent.installed_on = years_ago(9)
    vent.save()
    note = asset_tabs.costs_tab(vent)["costs"]["outlook"]["note"]
    assert note.startswith("Within 1.0 years of the end of its 10-year expected life.")
    assert note.endswith("Flag it for the next capital cycle; estimated replacement $39,900.")


def test_replacement_note_counts_recent_repairs_and_flags_a_young_device_in_poor_shape(client, signed_in, vent):
    today = date.today()
    for days in (10, 40, 90):
        work_order(vent, type=WoType.REPAIR, opened=today - timedelta(days=days))
    cancelled = work_order(vent, type=WoType.REPAIR, opened=today - timedelta(days=20))
    change_status(cancelled, WoStatus.CANCELLED)  # not a failure
    work_order(vent, type=WoType.REPAIR, opened=today - timedelta(days=200))  # outside the six months
    vent.condition = 1
    vent.save()
    o = asset_tabs.costs_tab(vent)["costs"]["outlook"]
    assert o["repairs"] == 3 and o["score_pct"] == round(replacement_score(vent.age_years(), 10, 3, 1) * 100) and o["score_pct"] >= 50
    assert "3 repairs in the last 6 months, and condition 1 of 5. Repairs and condition score it high for its age; review its repair history." in o["note"]
    signed_in("technician")
    assert "Replacement score " + str(o["score_pct"]) + " of 100" in tab(client, vent, "costs").content.decode()


def test_replacement_note_without_an_install_date_or_a_list_price(ctx, dept):
    model = DeviceModel.objects.create(manufacturer="Acme", model="A1", description="Suction pump", category="Suction", expected_life_years=8, list_cost=0)
    asset = Asset.objects.create(tag="CE-30000", device_model=model, department=dept, acquisition_cost=1000, next_pm_on=date.today())
    note = asset_tabs.costs_tab(asset)["costs"]["outlook"]["note"]
    assert note.startswith("No install date on file, so its age against its 8-year expected life is unknown.")
    asset.installed_on = years_ago(9)
    asset.save()
    note = asset_tabs.costs_tab(asset)["costs"]["outlook"]["note"]
    assert note.endswith("A replacement candidate; estimated replacement $1,050 (from its acquisition cost; the model has no list price).")


# --- bounded queries --------------------------------------------------------------------------------------------------------


def _queries(client, asset, name) -> int:
    with CaptureQueriesContext(connection) as q:
        tab(client, asset, name)
    return len(q.captured_queries)


def test_the_pm_tab_runs_the_same_queries_for_one_pm_or_many(client, signed_in, vent, techs):
    """Inside the next 7 days the suggestion comes from the week plan, after it from a least-loaded pick: each path is a fixed set."""
    vent_model = vent.device_model
    vent_model.pm_procedure = PmProcedure.objects.create(code="P1", name="PM", checklist=["One", {"text": "Two", "measure": "V"}])
    vent_model.save()
    today = date.today()
    signed_in("technician")

    def counts():
        out = {}
        for due in (today + timedelta(days=2), today + timedelta(days=40)):
            set_next_pm(vent, due)
            out[due] = _queries(client, vent, "pm")
        return out

    completed(vent, today - timedelta(days=400), technician=techs["dana"], labor=[(1, 82)])
    work_order(vent, opened=today)  # open and unassigned, so the suggestion is worked out
    one = counts()
    extra = Technician.objects.create(name="Avery Lee")
    Credential.objects.create(technician=extra, scope=Scope.CATEGORY, value="Ventilators")
    for i, tech in enumerate([techs["dana"], extra, None, techs["tom"], extra, None]):
        completed(vent, today - timedelta(days=300 - i * 30), technician=tech, vendor="" if tech or i % 2 else "Hamilton Service", labor=[(1, 82), (0.5, 82)])
    work_order(vent, opened=today - timedelta(days=1))
    for k in range(3):  # more devices in the week plan
        Asset.objects.create(tag=f"CE-4000{k}", device_model=vent_model, department=vent.department, next_pm_on=today + timedelta(days=k))
    assert counts() == one
    with CaptureQueriesContext(connection) as q:
        asset_tabs.pm_tab(Asset.objects.select_related("device_model").get(pk=vent.pk))
    # the procedure, the PM work orders, their labor hours, and the week plan's technicians, credentials, devices, open PMs, open hours
    assert len(q.captured_queries) <= 14  # the day plan the PM screen uses (its technicians, credentials, open work), once


def test_the_costs_tab_runs_the_same_queries_for_one_work_order_or_many(client, signed_in, vent, pump):
    today = date.today()
    signed_in("technician")
    ct = ct_services.create_contract(reference="SC-9", vendor="Hamilton Medical", start_on=today, end_on=today + timedelta(days=90), annual_cost=1000)
    ct_services.add_asset(ct, vent)
    ct_services.add_asset(ct, pump)
    completed(vent, today, type=WoType.REPAIR, labor=[(1, 82)], parts=[(1, 10)])
    one = _queries(client, vent, "costs")
    for years_back in range(5):
        for k in range(3):
            completed(vent, date(today.year - years_back, 1, 1 + k), type=WoType.REPAIR, labor=[(1, 82), (2, 90)], parts=[(1, 10), (3, 4)])
    for k in range(4):
        work_order(vent, type=WoType.REPAIR, opened=today - timedelta(days=k))
    assert _queries(client, vent, "costs") == one
    vent.refresh_from_db()
    asset = Asset.objects.select_related("device_model", "contract").get(pk=vent.pk)
    with CaptureQueriesContext(connection) as q:
        asset_tabs.costs_tab(asset)
    assert len(q.captured_queries) <= 4  # labor by year, parts by year, the contract's covered devices, recent repairs


# --- tenant isolation -------------------------------------------------------------------------------------------------------


def test_another_tenants_work_orders_are_never_counted(ctx, tenant, other_tenant, vent):
    today = date.today()
    ours = completed(vent, today, labor=[(1, 100)])
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                           risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        theirs = Asset.objects.create(tag=vent.tag, device_model=model, department=Department.objects.create(name="ICU"), acquisition_cost=1,
                                      next_pm_on=today)
        completed(theirs, today, labor=[(10, 100)], parts=[(1, 5000)], resolution="Theirs")
        completed(theirs, today - timedelta(days=30), type=WoType.REPAIR, parts=[(1, 700)])
    # unscoped: plants another tenant's lines on our work order, which row-level security hides in Postgres; the scoped managers must too
    LaborLine.unscoped.create(tenant=other_tenant, work_order=ours, hours=50, rate=100)
    PartLine.unscoped.create(tenant=other_tenant, work_order=ours, description="Not ours", quantity=1, unit_cost=9999)

    c = asset_tabs.costs_tab(vent)["costs"]
    assert c["total"] == 100.0 and c["by_year"][today.year] == 100.0 and c["outlook"]["repairs"] == 0
    history = asset_tabs.pm_tab(vent)["pm"]["history"]
    assert [h["wo"].number for h in history] == [ours.number] and history[0]["hours"] == Decimal("1.00")


# --- review fixes -----------------------------------------------------------------------------------------------------------

def test_the_costs_tab_shows_the_contract_price_only_with_contracts_view(client, tenant, ctx, vent):
    from apps.contracts.models import Contract

    c = Contract.objects.create(reference="SC-SECRET", vendor="Hamilton", annual_cost=Decimal("123456"), start_on=date.today() - timedelta(days=30),
                                end_on=date.today() + timedelta(days=300))
    c.add_assets([vent])
    client.force_login(custom_user(tenant, {Module.EQUIPMENT: Level.VIEW, Module.REPORTS: Level.VIEW}, slug="analyst2"))
    body = tab(client, vent, "costs").content.decode()
    assert "SC-SECRET" in body and "123,456" not in body
    client.force_login(custom_user(tenant, {Module.EQUIPMENT: Level.VIEW, Module.REPORTS: Level.VIEW, Module.CONTRACTS: Level.VIEW}, slug="buyer"))
    assert "123,456" in tab(client, vent, "costs").content.decode()


def test_no_acquisition_cost_means_no_contract_share_rather_than_zero(client, signed_in, ctx, vent):
    from apps.contracts.models import Contract

    c = Contract.objects.create(reference="SC-1", vendor="Hamilton", annual_cost=Decimal("10000"), start_on=date.today() - timedelta(days=30),
                                end_on=date.today() + timedelta(days=300))
    c.add_assets([vent])
    Asset.objects.filter(pk=vent.pk).update(acquisition_cost=0)
    signed_in("director")
    r = tab(client, vent, "costs")
    assert r.context["costs"]["share"] is None and "its share of the contract cannot be worked out" in r.content.decode()


def test_the_pm_tab_names_the_technician_the_pm_screen_plans(client, signed_in, ctx, dept, pump_model, techs):
    """Beyond the week, and for an open PM held by a deactivated technician, the tab names whom the day panel plans for that day."""
    from apps.pm import schedule as sch

    day = date.today() + timedelta(days=40)
    first = Asset.objects.create(tag="CE-81001", device_model=pump_model, department=dept, next_pm_on=day)
    second = Asset.objects.create(tag="CE-81002", device_model=pump_model, department=dept, next_pm_on=day)
    signed_in("director")
    planned, _ = sch.planned_technicians(day, date.today())
    for a in (first, second):
        assert tab(client, a, "pm").context["pm"]["upcoming"]["who"]["name"] == planned[a.id].name
    assert planned[first.id] != planned[second.id]  # two free technicians: the day is shared out, not piled on one
    soon = date.today() + timedelta(days=1)
    set_next_pm(first, soon)
    wo = work_order(first, opened=date.today(), technician=techs["dana"])
    techs["dana"].is_active = False
    techs["dana"].save(update_fields=["is_active"])
    who = tab(client, first, "pm").context["pm"]["upcoming"]["who"]
    assert who["kind"] == "suggested" and who["name"] == sch.planned_technicians(soon, date.today())[0][first.id].name and wo.number
