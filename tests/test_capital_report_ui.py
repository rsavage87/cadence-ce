"""
Slice 36, wave 2A: the Replacement planning report's panel and its print, the device drawer's Costs tab outlook and the Overview's
expected life line, and the CSV's Score version column (the rule itself is pinned in tests/test_capital_priority.py, the report's data in
tests/test_reports_fleet.py).

The panel: the version words and what the score is; the top rows with flag chips (must act apart from for review, in words as well as
colour), "+N unknown" beside a score, "too little history", the estimate's source; fleets by model; the outlook by fiscal year with each
year's dates; what the scores do not know, counted and listed with links only for a reader who may open them; the devices not ranked.
"""
import csv
import io
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from capital_fixtures import TODAY, device, recall_match, work, years_before
from csvutil import csv_rows, csv_text
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.capital import priority
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, LifeSource, Ownership, RiskClass
from apps.facility import services as fac
from apps.imports import base, kinds
from apps.imports import services as imports
from apps.imports.models import ImportRun
from apps.reports.fleet import REPLACE_COLUMNS
from apps.web import asset_tabs
from apps.web.reports_fleet import GAP_LIMIT, UNRANKED_LIMIT
from apps.workorders.models import WoType

HX = {"HTTP_HX_REQUEST": "true"}
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"
TEMPLATES = Path(__file__).resolve().parent.parent / "apps/web/templates/web"


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def custom_user(tenant, levels, slug):
    role = Role.objects.create(name=slug.replace("_", " ").title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


def set_model(dm, **fields):
    """The model's support and life source as Record support would store them, set directly so the report's day (TODAY) is free of the
    clock the service checks checked-on against."""
    DeviceModel.objects.filter(pk=dm.pk).update(**fields)
    dm.refresh_from_db()
    return dm


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Our devices on TODAY: a ventilator past its life whose parts ended (must act), a pump whose support ended (for review), a monitor
    with a confirmed Class I recall (for review) and one installed four months ago (too little history), a pump with no install date and
    a monitor with none (not ranked; the monitor not placed either), a scale with no estimate (not ranked); a rental (never scored)."""
    set_model(vent_model, parts_end_on=TODAY - timedelta(days=30), support_checked_on=TODAY - timedelta(days=10), life_source=LifeSource.MANUFACTURER)
    set_model(pump_model, support_ends_on=TODAY - timedelta(days=100), sale_ended_on=TODAY - timedelta(days=400))
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX450", description="Patient monitor", category="Monitors",
                                         risk_class=RiskClass.MEDIUM, expected_life_years=7, list_cost=12000)
    scale = DeviceModel.objects.create(manufacturer="Acme", model="S1", description="Scale", category="Scales", risk_class=RiskClass.LOW, list_cost=0)
    recall_match(monitor, classification="Class I")
    devices = {
        "vent": device(vent_model, dept, "CE-20001", years=12),
        "pump": device(pump_model, dept, "CE-20002", years=5),
        "monitor": device(monitor, dept, "CE-20003", years=3),
        "new_monitor": device(monitor, dept, "CE-20004", years=0.33),
        "pump_undated": device(pump_model, dept, "CE-20005"),
        "monitor_undated": device(monitor, dept, "CE-20006"),
        "scale": device(scale, dept, "CE-20007", years=2, acquisition_cost=0),
    }
    work(devices["vent"], WoType.PM, TODAY - timedelta(days=1150), completed=TODAY - timedelta(days=1150))  # the facility's first work order
    for days in (40, 200, 500):
        work(devices["pump"], WoType.REPAIR, TODAY - timedelta(days=days), completed=TODAY - timedelta(days=days - 2), labor=((2, 90),))
    Asset.objects.create(tag="T-0001", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="Acme Rentals",
                         installed_on=years_before(9))
    return {**devices, "monitor_model": monitor, "scale_model": scale, "vent_model": vent_model, "pump_model": pump_model}


def panel(client) -> str:
    """The Replacement planning panel of the Reports page, alone (the shell around it is the page's)."""
    r = client.get("/reports/replace/")
    assert r.status_code == 200
    return r.content.decode().split('<div class="panel-b">', 1)[1].split('<aside class="drawer', 1)[0]


# --- the panel ------------------------------------------------------------------------------------------------------------------------


def test_the_panel_says_what_the_score_is_and_ranks_with_chips(client, signed_in, fleet, freeze_today):
    freeze_today(TODAY)
    signed_in("director")
    body = panel(client)
    assert "A recommendation for the capital committee, never a decision." in body and f"{priority.version_words()}." in body
    assert "is never scored as 0 and never spread over the others" in body and "Flags stand apart from the score." in body
    assert "The top 4 ranked are shown; the CSV carries all 7 devices of ours in use" in body and "4 ranked, 3 not ranked." in body
    assert "T-0001" not in body  # a rental is never scored
    # must act (filled) apart from for review (outlined), in words for a screen reader as well as colour
    assert '<span class="cap-vh">Must act: </span><span class="chip crit">Parts no longer available</span>' in body
    assert '<span class="cap-vh">For review: </span><span class="chip warn">Manufacturer support ended</span>' in body
    assert '<span class="cap-vh">For review: </span><span class="chip warn">Confirmed recall</span>' in body
    rows = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert [m for m in re.findall(r'href="/equipment/(CE-\d+)/"', rows)] == ["CE-20001", "CE-20002", "CE-20003", "CE-20004"]
    vent = rows.split('href="/equipment/CE-20001/"', 1)[1].split("</tr>", 1)[0]
    assert "Suggested for FY2026" in vent and '<td class="num down">12.0 of 10 yr</td>' in vent
    assert f"Parts no longer available since {TODAY - timedelta(days=30):%b} {(TODAY - timedelta(days=30)).day}, 2026" in vent
    assert '<small>list price</small>' in vent and 'title="List price, set ' in vent  # its full words in the title
    new = rows.split('href="/equipment/CE-20004/"', 1)[1].split("</tr>", 1)[0]
    assert '<span class="muted">too little history</span>' in new and '<span class="muted">unknown</span>' in new
    p = priority.for_asset(fleet["new_monitor"], TODAY)
    assert p.points_unknown == 60 and f'<td class="num two cap-score"><b>{p.score}</b><small>+60 unknown</small></td>' in new
    pump = rows.split('href="/equipment/CE-20002/"', 1)[1].split("</tr>", 1)[0]
    assert '<td class="num" title="3 in 36 mo, 1.0 a year">3</td>' in pump and "Manufacturer support ended" in pump
    assert re.search(r'<td class="num" title="\d+% a year of replacement \(\$\d[\d,]* a year\)">\d+%</td>', pump)
    assert "Estimated replacement total for these 4:" in body and "Failures are device faults" in body


def test_the_sections_fleets_outlook_gaps_and_not_ranked(client, signed_in, fleet, freeze_today):
    freeze_today(TODAY)
    fac.update_settings(fiscal_year_start_month="7")
    signed_in("director")
    body = panel(client)
    # Fleets by model, each model opening its drawer for a reader with PM View
    monitor_url = reverse("web:pm_model", args=[fleet["monitor_model"].pk])
    assert "Fleets by model" in body and f'<a class="link" href="{monitor_url}" hx-get="{monitor_url}" hx-target="#drawer">Philips IntelliVue MX450</a>' in body
    # Outlook by fiscal year: the current one (July start: FY2027) and four after, each with its dates; not placed counted
    outlook = body.split("Outlook by fiscal year", 1)[1].split("What the scores do not know", 1)[0]
    assert re.findall(r'<td class="two" title="(FY\d{4}: [^"]+)">', outlook) == [
        "FY2027: Jul 1, 2026 to Jun 30, 2027", "FY2028: Jul 1, 2027 to Jun 30, 2028", "FY2029: Jul 1, 2028 to Jun 30, 2029",
        "FY2030: Jul 1, 2029 to Jun 30, 2030", "FY2031: Jul 1, 2030 to Jun 30, 2031"]
    assert 'FY2027 <span class="chip neutral">Current</span><small>Jul 1, 2026 to Jun 30, 2027</small>' in outlook
    assert "Not placed: 1 (no install date, and no support or parts end on record)." in outlook and "The fiscal year starts in July" in outlook
    # What the scores do not know: each gap counted, its devices or models listed with links, and how it is filled in
    gaps = body.split("What the scores do not know", 1)[1].split("Not ranked</h3>", 1)[0]
    assert "No install date: 2 devices" in gaps and "No list price or acquisition cost: 1 device" in gaps
    assert "Where the expected life comes from is not recorded: 6 devices (3 models)" in gaps
    for tag in ("CE-20005", "CE-20006", "CE-20007"):
        assert f'<a class="link" href="/equipment/{tag}/" hx-get="/equipment/{tag}/" hx-target="#drawer">{tag}</a>' in gaps
    assert f'<a class="link" href="{monitor_url}" hx-get="{monitor_url}" hx-target="#drawer">Philips IntelliVue MX450</a> (3)' in gaps
    assert "fill the CSV&#x27;s Installed on column and import the file (Settings, Import data, Devices)" in gaps
    assert "its PM program tab, Record support" in gaps
    # Not ranked, with why and the score so far
    unranked = body.split("Not ranked</h3>", 1)[1]
    assert "Not ranked: no install date" in unranked and "Not ranked: no replacement estimate" in unranked
    assert re.findall(r'href="/equipment/(CE-\d+)/">', unranked) == ["CE-20005", "CE-20006", "CE-20007"]
    # never the words of a plan already made, never a device's status words
    text = re.sub(r"<[^>]+>", " ", body).lower()
    assert not re.search(r"\bdue\b", text) and "scheduled" not in text and "held for incident" not in text


def test_links_follow_what_the_reader_may_open(client, tenant, fleet, freeze_today):
    freeze_today(TODAY)
    monitor_url = reverse("web:pm_model", args=[fleet["monitor_model"].pk])
    # Reports View alone: every tag and model as text, nothing opens a drawer
    client.force_login(custom_user(tenant, {Module.REPORTS: Level.VIEW}, "reports_only"))
    body = panel(client)
    assert "/equipment/" not in body and monitor_url not in body and 'hx-target="#drawer"' not in body
    assert '<span class="tag">CE-20005</span>' in body and "CE-20001 ICU ventilator" in body
    assert "Philips IntelliVue MX450 (3)" in body
    # Equipment View too: devices open, models do not (the model drawer is the PM screen's)
    client.force_login(custom_user(tenant, {Module.REPORTS: Level.VIEW, Module.EQUIPMENT: Level.VIEW}, "reports_equipment"))
    body = panel(client)
    assert 'href="/equipment/CE-20005/"' in body and 'hx-get="/equipment/CE-20001/"' in body and monitor_url not in body
    # PM View instead: models open, devices do not
    client.force_login(custom_user(tenant, {Module.REPORTS: Level.VIEW, Module.PM: Level.VIEW}, "reports_pm"))
    body = panel(client)
    assert monitor_url in body and "/equipment/" not in body
    assert client.get(monitor_url, **HX).status_code == 200  # the link it shows opens


def test_the_gap_lists_and_not_ranked_are_capped_and_the_csv_carries_every_device(client, signed_in, ctx, dept, pump_model, freeze_today):
    freeze_today(TODAY)
    for i in range(UNRANKED_LIMIT + 2):
        device(pump_model, dept, f"CE-3{i:04d}")  # no install date
    signed_in("director")
    body = panel(client)
    gaps = body.split("What the scores do not know", 1)[1].split("Not ranked</h3>", 1)[0]
    first = gaps.split("No install date", 1)[1].split("</li>", 1)[0]
    assert len(re.findall(r'href="/equipment/CE-3\d{4}/"', first)) == GAP_LIMIT
    assert f", and {UNRANKED_LIMIT + 2 - GAP_LIMIT} more (the CSV lists every device)" in first
    assert "BD Alaris 8015 PCU</a> (27)" in gaps.split("Manufacturer support not checked", 1)[1]
    assert f"Showing {UNRANKED_LIMIT} of {UNRANKED_LIMIT + 2}; the CSV lists every one, and its Ranked column says why." in body
    assert "No device can be ranked yet" in body
    assert len(csv_rows(client.get("/reports/replace.csv"))) == UNRANKED_LIMIT + 3


def test_an_empty_fleet_reads_no_active_devices(client, signed_in, ctx, freeze_today):
    freeze_today(TODAY)
    signed_in("director")
    body = panel(client)
    assert '<div class="empty">No active devices.</div>' in body and "Outlook by fiscal year" not in body


# --- the print --------------------------------------------------------------------------------------------------------------------------


def test_the_committees_print_has_every_section_on_light_paper(client, signed_in, fleet, freeze_today):
    freeze_today(TODAY)
    signed_in("analyst")
    r = client.get("/print/reports/replace/")
    assert r.status_code == 200 and "web/_report_replace.html" in [t.name for t in r.templates]
    page = r.content.decode()
    assert "color-scheme:light" in page.split("<style>", 1)[1]  # print_report.html's light tokens
    body = page.split('<div class="rp-body">', 1)[1]
    for words in ("A recommendation for the capital committee, never a decision.", priority.version_words(), "Fleets by model",
                  "Outlook by fiscal year", "What the scores do not know", "Not ranked</h3>", "Not placed: 1",
                  '<span class="chip crit">Parts no longer available</span>', "Estimated replacement total for these 4:"):
        assert words in body, words
    assert 'class="nav"' not in page and "Held for incident" not in body


# --- the device drawer ------------------------------------------------------------------------------------------------------------------


def test_the_costs_tab_outlook_shows_the_score_flags_drivers_and_how_it_is_scored(client, signed_in, vent):
    today = timezone.localdate()
    vent.installed_on = years_before(12, today)
    vent.save()
    eq.update_device_model(vent.device_model, parts_end_on=today - timedelta(days=1), support_checked_on=today)
    signed_in("technician")  # Reports View
    body = client.get(f"/equipment/{vent.tag}/?tab=costs", **HX).content.decode()
    p = priority.for_asset(vent, today)
    assert p.points_unknown and f'{p.score}<small class="cap-unk">+{p.points_unknown} unknown</small>' in body
    fy = p.suggested_fy
    assert fy == today.year  # must act: the current fiscal year (January start)
    assert f'title="FY{fy}: Jan 1, {fy} to Dec 31, {fy}">FY{fy}</div><div class="l">suggested fiscal year, Jan 1, {fy} to Dec 31, {fy}</div>' in body
    assert '<span class="cap-flag"><span class="cap-flag-k">Must act</span><span class="chip crit">Parts no longer available</span></span>' in body
    assert "Driven most by manufacturer support (25.0 points) and age against expected life (24.0 points)." in body
    assert f"list price, set {today:%b} {today.day}, {today.year}" in body  # the estimate's words keep the month's capital
    how = body.split('<details class="cap-how">', 1)[1].split("</details>", 1)[0]
    assert "<summary>How it is scored</summary>" in how and f"{priority.version_words()}." in how
    assert [label for label in re.findall(r"<tr><td>([^<]+)</td>", how)] == [priority.LABELS[k] for k in priority.COMPONENTS]
    assert f"{sum(c.points for c in p.components if c.points is not None):.1f} points known ({p.points_unknown} unknown, never scored as 0" in how
    assert "times 1.0 for its risk class (life support)" in how and "Recorded condition 3 of 5 (undated): shown, never scored." in how
    # no work order in Cadence yet: the window starts today
    assert f"Failures and service cost have too little history: 0 months since {today:%b} {today.day}, {today.year}, and 12 are needed." in how


def test_the_costs_tab_has_no_outlook_for_a_rental_and_the_note_for_a_retired_device(client, signed_in, ctx, dept, pump_model, vent):
    rental = Asset.objects.create(tag="T-0001", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="Acme Rentals",
                                  installed_on=years_before(3, timezone.localdate()))
    signed_in("technician")
    body = client.get(f"/equipment/{rental.tag}/?tab=costs", **HX).content.decode()
    assert "Replacement outlook" not in body and "cap-how" not in body
    body = client.get(f"/equipment/{rental.tag}/", **HX).content.decode()
    assert "cap-life" not in body and "<dt>Expected life</dt>" not in body  # not ours: no life of ours
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.RETIRED)
    body = client.get(f"/equipment/{vent.tag}/?tab=costs", **HX).content.decode()
    assert "Retired. Replacement planning scores devices in use only." in body and "cap-how" not in body and "cap-stats" not in body


def test_the_overview_says_through_which_fiscal_year_the_life_runs_and_where_it_comes_from(client, signed_in, vent, pump):
    Asset.objects.filter(pk=vent.pk).update(installed_on=date(2021, 8, 15))  # 10 years: its life ends Aug 15, 2031
    signed_in("technician")
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert ('<dd class="cap-life" title="Its expected life ends Aug 15, 2031 (FY2031: Jan 1, 2031 to Dec 31, 2031)">10 yr, through FY2031</dd>'
            in body)
    fac.update_settings(fiscal_year_start_month="7")
    set_model(vent.device_model, life_source=LifeSource.MANUFACTURER)
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert ('<dd class="cap-life" title="Its expected life ends Aug 15, 2031 (FY2032: Jul 1, 2031 to Jun 30, 2032)">10 yr, through FY2032'
            "<small>from the manufacturer</small></dd>") in body
    p = priority.for_asset(Asset.objects.get(pk=vent.pk), timezone.localdate(), 7)
    assert p.life_end == date(2031, 8, 15) and asset_tabs.expected_life(p.asset, 7)["fy"] == 2032  # the rule's arithmetic
    set_model(pump.device_model, life_source=LifeSource.AHA)
    body = client.get(f"/equipment/{pump.tag}/", **HX).content.decode()  # no install date: the years alone
    assert '<dd class="cap-life">8 yr<small>from the AHA&#x27;s estimated useful lives</small></dd>' in body


# --- phone width ------------------------------------------------------------------------------------------------------------------------


def _block_a() -> str:
    css = CSS.read_text(encoding="utf-8")
    return css[css.index("/* Slice 36 A:"):css.index("/* Slice 36 A: end */")]


def test_nothing_is_wider_than_a_phone_drawer():
    """The drawer is the phone's width (min(620px, 100%)): no rule of block A fixes a width a 375px phone cannot hold, and the templates
    set no inline width. Wide tables scroll inside their frame (.tbl-wrap), never the page."""
    widths = [int(n) for n in re.findall(r"(?:^|[;{\s])(?:min-|max-)?width:(\d+)px", _block_a())]
    assert widths and max(widths) <= 343  # 375 less the drawer's 16px gutters
    for name in ("_report_replace.html", "_asset_tab_costs.html"):
        assert not re.search(r'style="[^"]*width', (TEMPLATES / name).read_text(encoding="utf-8")), name
    assert ".cap-how>summary{display:flex;align-items:center;min-height:44px}" in _block_a()  # a thumb-sized target


# --- the CSV's Score version, and the devices import reading it back ----------------------------------------------------------------------


def test_the_csv_ends_with_the_score_version_and_reads_back_through_the_devices_import(client, signed_in, fleet, freeze_today):
    freeze_today(TODAY)
    director = signed_in("director")
    rows = csv_rows(client.get("/reports/replace.csv"))
    assert rows[0] == REPLACE_COLUMNS and rows[0][-1] == "Score version" and {r[-1] for r in rows[1:]} == {f"v{priority.SCORE_VERSION}"}
    # The devices import takes the tag, maker, model, install date, and acquisition cost, and leaves the rest unused, Score version too
    assert set(base.auto_map(kinds.get("devices").columns, rows[0])) == {"tag", "manufacturer", "model", "installed", "cost"}
    at = rows[0].index("Installed on")
    for r in rows[1:]:
        if r[0] == "CE-20005":
            r[at] = "2019-05-01"  # the bulk fix for a missing install date
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    run = imports.upload(director, "devices", "replacement.csv", out.getvalue().encode("utf-8"))
    assert "Score version" in imports.mapping_view(run)["unused"]
    run = imports.confirm_columns(run, director, imports.mapping_of(run))
    while run.status == ImportRun.Status.CHECKING:
        run = imports.process(run, director)
    run = imports.start_import(run, director)
    while run.status == ImportRun.Status.IMPORTING:
        run = imports.process(run, director)
    assert run.status == ImportRun.Status.IMPORTED
    assert Asset.objects.get(tag="CE-20005").installed_on == date(2019, 5, 1)
    assert Asset.objects.get(tag="CE-20001").installed_on == fleet["vent"].installed_on and Asset.objects.get(tag="CE-20006").installed_on is None
    assert Asset.objects.get(tag="CE-20007").acquisition_cost == Decimal("0")
    assert "v2" in csv_text(client.get("/reports/replace.csv")).splitlines()[1]
