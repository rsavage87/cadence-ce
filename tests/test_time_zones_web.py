"""
The web layer on the facility's day and clock (slice 21, part B). The clock is pinned at 08:30 UTC on Oct 5, 2026: 22:30 on Oct 4
in Honolulu (UTC-10, no daylight saving) and 04:30 on Oct 5 in New York. Each test runs for a facility in each zone at that same
instant: the Honolulu facility's screens read Oct 4 and Honolulu time (the work orders list's overdue flags, the PM calendar's today
and day panel, the Overview's day, month, and attention list, Equipment's PM buckets, a report's as-of day and its CSV's name, the
prints' dates, the device drawer's PM tab, the history's and change log's times), and the New York facility's read Oct 5.

The facility's ventilator CE-10001 (life support) has its PM due Oct 4, and a high-priority repair on it, opened Sep 28, is due
Oct 4: on time and due today in Honolulu, a day late in New York.
"""
import re
from datetime import date, datetime
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from csvutil import csv_rows
from django.utils import timezone

from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.web.exports import cell
from apps.workorders.models import Priority, WoType
from apps.workorders.services import create_work_order

NOW = datetime(2026, 10, 5, 8, 30, tzinfo=dt_timezone.utc)
OCT4, OCT5 = date(2026, 10, 4), date(2026, 10, 5)
HX = {"HTTP_HX_REQUEST": "true"}
HONOLULU = {"zone": "Pacific/Honolulu", "today": OCT4, "day": "Oct 4, 2026", "time": "Oct 4, 2026 10:30 PM", "clock": "10:30 PM", "abbr": "HST",
            "iso": "2026-10-04T22:30-10:00", "c": "2026-10-04T22:30:00-10:00", "late": False}
NEW_YORK = {"zone": "America/New_York", "today": OCT5, "day": "Oct 5, 2026", "time": "Oct 5, 2026 4:30 AM", "clock": "4:30 AM", "abbr": "EDT",
            "iso": "2026-10-05T04:30-04:00", "c": "2026-10-05T04:30:00-04:00", "late": True}


def pin(monkeypatch, at):
    """Pin the clock: timezone.localdate() and localtime() follow django.utils.timezone.now."""
    monkeypatch.setattr(timezone, "now", lambda: at)


@pytest.fixture(params=[HONOLULU, NEW_YORK], ids=["honolulu", "new_york"])
def place(request, tenant, monkeypatch):
    """The facility in one of the two zones, the clock pinned at NOW."""
    tenant.timezone = request.param["zone"]
    tenant.save(update_fields=["timezone"])
    pin(monkeypatch, NOW)
    return request.param


@pytest.fixture
def world(place, tenant, make_user, client):
    """The director signed in, the ventilator due Oct 4, and its repair due Oct 4, all saved at NOW."""
    kim = make_user("director", username="kim@riverside.example")
    client.force_login(kim)  # last_login: NOW
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                           risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6, expected_life_years=10, list_cost=38000)
        vent = Asset.objects.create(tag="CE-10001", device_model=model, department=dept, acquisition_cost=38000, installed_on=date(2023, 1, 10),
                                    next_pm_on=OCT4)
        wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low pressure alarm", opened_on=date(2026, 9, 28),
                               due_on=OCT4, created_by=kim)
    return {"kim": kim, "vent": vent, "wo": wo}


def page(client, url, **headers) -> str:
    response = client.get(url, **headers)
    assert response.status_code == 200, url
    return response.content.decode()


# --- lists and screens -------------------------------------------------------------------------------------------------------

def test_the_work_orders_list_flags_late_work_on_the_facilitys_day(client, place, world):
    html = page(client, "/work-orders/")
    assert f"{1 if place['late'] else 0} past due" in html
    assert ("Oct 4 · 1 d late" in html) is place["late"]
    assert f"{(place['today'] - date(2026, 9, 28)).days} d open" in html  # 6 days in Honolulu, 7 in New York
    drawer = page(client, f"/work-orders/{world['wo'].number}/", **HX)
    assert ("1 d past due" in drawer) is place["late"]
    assert place["time"] in drawer  # the timeline's "Opened" entry, saved at NOW


def test_the_pm_calendars_today_and_day_panel_are_the_facilitys(client, place, world):
    html = page(client, "/pm/")
    today_cell = re.search(r'aria-label="([A-Z][a-z]{2} \d{1,2}, \d{4})[^"]*, today[^"]*" aria-current="date"', html)
    assert today_cell and today_cell.group(1) == place["day"]
    assert f"Due {place['day']}" in html  # the day panel shows today
    # The ventilator falls due on Oct 4: today's panel in Honolulu, and an overdue count on the calendar in New York.
    assert ("CE-10001" in html.split('class="panel pm-day"')[1]) is not place["late"]
    sheets = page(client, "/print/route-sheets/")  # the PM screen's clock: today's sheet
    assert ("No PMs are due on Oct 5, 2026." in sheets) is place["late"]
    assert ("CE-10001" in sheets) is not place["late"]


def test_the_overview_is_as_of_the_facilitys_day(client, place, world):
    html = page(client, "/")
    assert f"fleet status and attention items are as of today, {place['day']}" in html
    assert "October 2026 (month to date)" in html
    # Attention: the life-support PM and the high-priority repair are overdue only once the facility's Oct 4 is over.
    assert ("PM overdue 1 d" in html) is place["late"]
    assert ("Open · 7 d open" in html) is place["late"]


def test_the_overviews_month_turns_at_the_facilitys_midnight(client, place, world, monkeypatch):
    pin(monkeypatch, datetime(2026, 11, 1, 8, 30, tzinfo=dt_timezone.utc))  # Oct 31, 22:30 in Honolulu; Nov 1 in New York
    client.force_login(world["kim"])  # the session from NOW has run out by then
    html = page(client, "/")
    assert ("November 2026 (month to date)" if place["late"] else "October 2026 (month to date)") in html


def test_equipment_buckets_and_stickers_count_from_the_facilitys_day(client, place, world):
    html = page(client, "/equipment/")
    assert ("Overdue 1 d" if place["late"] else "Due today") in html
    table = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "eq-table"}
    assert ("CE-10001" in page(client, "/equipment/?bucket=pm_overdue", **table)) is place["late"]
    assert ("CE-10001" in page(client, "/equipment/?bucket=pm_due", **table)) is not place["late"]
    response = client.get("/export/equipment.csv")
    assert f'filename="cadence-equipment-{place["today"]:%Y-%m-%d}.csv"' in response["Content-Disposition"]
    row = dict(zip(*csv_rows(response)[:2]))
    assert row["Fleet state"] == ("PM overdue" if place["late"] else "PM due within 30 days")


def test_a_report_is_as_of_the_facilitys_day_and_its_csv_is_named_for_it(client, place, world):
    html = page(client, "/reports/compliance/")
    assert f"as of today, {place['day']}" in html and f"Fleet as of {place['day']}" in html
    response = client.get("/reports/compliance.csv")
    assert response["Content-Disposition"] == f'attachment; filename="cadence-compliance-{place["today"]:%Y-%m-%d}.csv"'
    rows = csv_rows(response)
    life = dict(zip(rows[0], next(r for r in rows[1:] if r[0] == RiskClass.LIFE_SUPPORT.label)))
    assert life["Overdue now"] == ("1" if place["late"] else "0")
    printed = page(client, "/print/reports/compliance/")
    assert f"As of {place['day']}" in printed


def test_prints_carry_the_facilitys_date_and_time(client, place, world):
    html = page(client, f"/print/work-orders/{world['wo'].number}/")
    assert f"printed {place['time']} {place['abbr']} by" in html
    assert ("1 d late" in html) is place["late"]
    assert place["time"] in html.split("printed")[0]  # the timeline's "Opened" entry


def test_the_device_drawers_pm_tab_counts_from_the_facilitys_day(client, place, world):
    html = page(client, "/equipment/CE-10001/?tab=pm", **HX)
    assert ("Overdue 1 d" if place["late"] else "Due today") in html
    assert ("the overdue one today" in html) is place["late"]


def test_history_and_the_change_log_read_in_the_facilitys_time(client, place, world):
    hist = page(client, "/equipment/CE-10001/?tab=history", **HX)
    assert f'<time datetime="{place["c"]}">{place["time"]}</time>' in hist
    log = page(client, "/users/log/")
    assert f"{place['day']}<small>{place['clock']}</small>" in log
    response = client.get("/users/log/export.csv")
    assert f'filename="cadence-change-log-{place["today"]:%Y-%m-%d}.csv"' in response["Content-Disposition"]
    rows = csv_rows(response)
    device = next(dict(zip(rows[0], r)) for r in rows[1:] if r[3] == "CE-10001")
    assert device["When"] == place["iso"]
    # From and To are the facility's days: NOW is on Oct 4 in Honolulu, on Oct 5 in New York.
    oct4 = page(client, "/users/log/?from=2026-10-04&to=2026-10-04")
    assert ("CE-10001" in oct4) is not place["late"]


def test_last_active_and_date_pickers_use_the_facilitys_day(client, place, world):
    assert f"Today, {place['clock']}" in page(client, "/users/")  # the director's last sign-in, at NOW
    form = page(client, "/equipment/new/", **HX)
    assert form.count(f'max="{place["today"].isoformat()}"') == 2  # installed and last PM: no future dates


def test_csv_times_are_written_in_the_zone_at_work():
    with timezone.override(ZoneInfo("Pacific/Honolulu")):
        assert cell(NOW) == "2026-10-04T22:30-10:00"
    with timezone.override(ZoneInfo("America/New_York")):
        assert cell(NOW) == "2026-10-05T04:30-04:00"
    assert cell(datetime(2026, 10, 4, 22, 30)) == "2026-10-04T22:30"  # a naive time is written as it is
