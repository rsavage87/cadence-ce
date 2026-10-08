"""
Slice 27, part A, the Overview and All facilities: the PM tiles count by the facility's PM completion window (apps.pm.windows), read
once for the page and passed down, and under a window other than the due date say how many PMs due so far are still inside it; the
attention list keeps listing devices by their due date and says where each life-support and high-risk device's window stands; the
trend chart names the window; All facilities counts each facility by its own window, shows it beside its PM rate, and says when the
facilities' windows differ. The default window changes none of it.
"""
import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from functools import partial

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Role, User, create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import PmWindow as K
from apps.facility.services import kpi_targets
from apps.reports import all_facilities as af
from apps.reports.services import attention_items, overview_kpis, overview_page
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web import views_all_facilities as web_view
from apps.web.overview import kpi_tiles
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_service_request, create_work_order

TODAY = date(2026, 10, 20)
BY_MONTH = {"pm_window_high": K.DUE_MONTH, "pm_window_other": K.DUE_MONTH}


@pytest.fixture
def director(make_user):
    return make_user("director")


@pytest.fixture
def monitor(ctx, dept):
    model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Patient monitor", category="Monitors",
                                       risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    return Asset.objects.create(tag="CE-30001", device_model=model, department=dept)


def pm(asset, due: date, done: date | None = None):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=10), due_on=due)
    if done:
        WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, started_on=due - timedelta(days=10), completed_on=done)
    return wo


@pytest.fixture
def october(vent, monitor):
    """Today Oct 20: the ventilator's PM due Oct 5 still open and its PM due Oct 3 done Oct 15; the monitor's due Oct 1 done that day and
    its due Oct 10 still open; the pump's due Oct 25, not yet due."""
    pm(vent, date(2026, 10, 5))
    pm(vent, date(2026, 10, 3), date(2026, 10, 15))
    pm(monitor, date(2026, 10, 1), date(2026, 10, 1))
    pm(monitor, date(2026, 10, 10))


def figures(k: dict) -> tuple:
    return tuple((k[key]["due"], k[key]["on_time"], k[key]["pending"]) for key in ("pm_on_time", "pm_on_time_life_support"))


def test_the_default_window_counts_as_before_with_nothing_pending(october):
    k = overview_kpis(2026, 10, TODAY)
    assert figures(k) == ((4, 1, 0), (2, 0, 0))
    assert k["pm_window"] == {"default": True, "uniform": True, "high": "By the due date", "other": "By the due date", "words": "By the due date"}
    parts = [text for t in kpi_tiles({"k": k, "prev": None, "targets": kpi_targets(), "alert_devices_affected": 0})[:2] for text, _ in t["parts"]]
    assert parts == ["1 of 4 PMs due so far this month", "target 95%", "0 of 2 due", "target 100%"]


def test_a_month_window_counts_the_pms_still_inside_it_as_pending(october, director):
    fac.update_settings(by=director, **BY_MONTH)
    k = overview_kpis(2026, 10, TODAY)
    assert figures(k) == ((2, 2, 2), (1, 1, 1))  # done within their month: on time; the two still open are inside October
    tiles = kpi_tiles({"k": k, "prev": None, "targets": kpi_targets(), "alert_devices_affected": 0})
    assert [text for text, _ in tiles[0]["parts"]] == ["2 of 2 PMs due so far this month", "2 still inside their window", "target 95%"]
    assert [text for text, _ in tiles[1]["parts"]] == ["1 of 1 due", "1 still inside its window", "target 100%"]
    assert k["pm_window"]["words"] == "By the end of the due month" and not k["pm_window"]["default"]
    november = overview_kpis(2026, 10, date(2026, 11, 2))  # October's window has closed: every PM counted, nothing pending
    assert figures(november) == ((4, 2, 0), (2, 1, 0))


def test_each_group_by_its_own_window(october, director):
    fac.update_settings(by=director, pm_window_other=K.DUE_MONTH)  # life support stays by the due date
    k = overview_kpis(2026, 10, TODAY)
    assert figures(k) == ((3, 1, 1), (2, 0, 0))
    assert k["pm_window"]["words"] == "Life support and high risk by the due date; medium and low by the end of the due month"


def test_the_overview_page_reads_settings_once(october, director):
    fac.update_settings(by=director, **BY_MONTH)
    with CaptureQueriesContext(connection) as seen:
        data = overview_page(2026, 10, TODAY)
    assert sum('"facility_facilitysettings"' in q["sql"] for q in seen.captured_queries) == 1
    assert figures(data["k"]) == ((2, 2, 2), (1, 1, 1)) and data["targets"]["pm_on_time"] == 95.0
    point = data["pm_series"][-1]
    assert (point["year"], point["month"], point["due"], point["on_time"]) == (2026, 10, 2, 2)  # the series on the same day and window


def test_the_attention_list_keeps_the_due_date_and_says_where_the_window_stands(ctx, dept, vent_model, pump_model, director):
    def device(tag, model, days_late):
        return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=TODAY - timedelta(days=days_late))

    device("LS-1", vent_model, 12), device("LS-2", vent_model, 40), device("HR-1", pump_model, 3)
    rights = {it["asset"]: it["right"] for it in attention_items(TODAY) if "asset" in it}
    assert rights == {"LS-1": "PM overdue 12 d", "LS-2": "PM overdue 40 d", "HR-1": "PM overdue 3 d"}  # the default: as before
    fac.update_settings(by=director, pm_window_high=K.DAYS_AFTER, pm_window_high_days=30)
    rights = {it["asset"]: it["right"] for it in attention_items(TODAY) if "asset" in it}
    assert rights == {"LS-1": "PM overdue 12 d · on time until Nov 7", "LS-2": "PM overdue 40 d · past its policy window",
                      "HR-1": "PM overdue 3 d · on time until Nov 16"}  # listed by the due date either way: what to do next


@pytest.mark.parametrize("fields", [{}, {"pm_window_high": K.DUE_MONTH}], ids=["default", "due_month"])
def test_a_portal_request_never_stands_in_for_the_window(ctx, dept, pump_model, director, fields):
    """The list names work orders `w` in its loops: a high-risk device listed after a portal request still reads the window."""
    if fields:
        fac.update_settings(by=director, **fields)
    pump = Asset.objects.create(tag="HR-1", device_model=pump_model, department=dept, next_pm_on=TODAY - timedelta(days=3))
    create_service_request(asset=pump, department=dept, problem="Won't power on", urgency="normal")
    items = attention_items(TODAY)
    assert [it.get("asset") or it.get("wo") for it in items][1] == "HR-1" and "wo" in items[0]
    assert items[1]["right"] == ("PM overdue 3 d · on time until Oct 31" if fields else "PM overdue 3 d")


@pytest.fixture
def frozen(monkeypatch):
    """The facility's today is TODAY for the request (the Overview view reads the clock itself)."""
    real = timezone.localdate
    monkeypatch.setattr(timezone, "localdate", lambda value=None, tz=None: TODAY if value is None else real(value, tz))


def test_the_overview_screen_shows_the_pending_line_and_names_the_window(client, october, director, frozen):
    client.force_login(director)
    body = client.get("/").content.decode()
    assert "still inside" not in body and "the PM policy's window" not in body  # the default: as before
    fac.update_settings(by=director, **BY_MONTH)
    body = client.get("/").content.decode()
    assert "2 still inside their window" in body and "1 still inside its window" in body
    assert "share of PMs due that were closed on time (the PM policy's window: by the end of the due month)" in body


def test_the_api_overview_says_the_window_and_the_pending(client, october, director):
    client.force_login(director)
    data = client.get("/api/v1/overview/?y=2026&m=10").json()
    assert data["pm_window"]["default"] is True and data["pm_on_time"]["pending"] == 0 and data["pm_on_time_life_support"]["pending"] == 0


# --- All facilities ---------------------------------------------------------------------------------------------------------------

NOW = datetime(2026, 9, 22, 16, 0, tzinfo=dt_timezone.utc)
EMAIL = "kim@health.example"


def account(tenant, slug, *, username, person):
    user = User(username=username, email=EMAIL, first_name="Kim", last_name="Alvarez", tenant=tenant,
                role=Role.unscoped.get(tenant=tenant, slug=slug), person=person)  # unscoped: made outside any facility
    user.set_password("Test-Pass-2026-x")
    user.last_login = timezone.now()
    user._password = None  # created, not changed: nothing to share
    user.save()
    return user


@pytest.fixture
def two(tenant, vent):
    """Kim: director at Riverside (the ventilator's PM due Sep 10, still open), technician at Lakeside (a monitor's PM due Sep 10, still
    open); Lakeside counts medium and low risk by the end of the due month, Riverside by the due date."""
    lakeside = Tenant.objects.create(name="Lakeside Surgery Center", slug="lakeside")
    create_default_roles(lakeside)
    person = uuid.uuid4()
    kim = account(tenant, "director", username=EMAIL, person=person)
    account(lakeside, "technician", username=f"{EMAIL}@lakeside", person=person)
    with tenant_context(tenant):
        pm(vent, date(2026, 9, 10))
    with tenant_context(lakeside):
        model = DeviceModel.objects.create(manufacturer="Philips", model="MX800", description="Monitor", category="Monitors",
                                           risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
        pm(Asset.objects.create(tag="LK-1", device_model=model, department=Department.objects.create(name="OR")), date(2026, 9, 10))
        fac.update_settings(pm_window_other=K.DUE_MONTH)
    return kim, lakeside


def test_each_facility_is_counted_by_its_own_window(two, tenant):
    kim, lakeside = two
    data = af.all_facilities(kim, tenant, now=NOW)
    river, lake = ({r["slug"]: r for r in data["facilities"]}[slug] for slug in ("riverside", "lakeside"))
    assert (river["pm_due"], river["pm_on_time"], river["pm_pending"], river["pm_window"]["default"]) == (1, 0, 0, True)
    assert (lake["pm_due"], lake["pm_on_time"], lake["pm_pending"], lake["pm_window"]["other"]) == (0, 0, 1, "By the end of the due month")
    t = data["totals"]
    assert (t["pm_due"], t["pm_on_time"], t["pm_pending"], t["pm_windows_differ"]) == (1, 0, 1, True)
    assert set(af.FIGURES).isdisjoint({"pm_window", "pm_pending", "life_support_pm_pending"})  # the API's figures are as they were


def test_the_page_shows_each_window_beside_the_pm_rate_and_says_when_they_differ(client, two, monkeypatch):
    kim, lakeside = two
    monkeypatch.setattr(web_view, "all_facilities_data", partial(af.all_facilities, now=NOW))
    client.force_login(kim)
    body = client.get("/overview/all/").content.decode()
    assert '<small>0 of 1 due</small><small class="allf-window">By the due date</small>' in body  # Riverside's
    assert ('<small>0 of 0 due · 1 inside its window</small><small class="allf-window">Life support and high risk by the due date; medium and '
            'low by the end of the due month</small>') in body  # Lakeside's
    tfoot = body.split("<tfoot>")[1].split("</tfoot>")[0]
    assert f'<small class="allf-window">{web_view.EACH_OWN_POLICY}</small>' in tfoot
    assert "the PM totals add up PMs each facility judged by its own policy" in body
    with tenant_context(lakeside):
        fac.update_settings(pm_window_other=K.DUE_DATE)  # both by the due date again: nothing added
    body = client.get("/overview/all/").content.decode()
    assert "allf-window" not in body and "judged by its own policy" not in body and '<small>0 of 1 due</small></span>' in body
