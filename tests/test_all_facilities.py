"""
All facilities (slice 22, part B; apps.reports.all_facilities): the Overview's figures for every facility a person has joined, each
read inside that facility with the person's account there (its own today and month, its own rows), side by side with totals whose
rates are worked out from the summed parts and whose alerts are counted once. The page (/overview/all/) and the API
(/api/v1/overview/all-facilities/, session only) show the same numbers. A facility whose Overview the person may not see is listed by
name with no figures and left out of the totals; pending invitations and accounts that cannot sign in are not listed.
"""
import re
import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from functools import partial

import pytest
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import Level, Role, User, create_default_roles
from apps.api import views_all_facilities as api_view
from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.recalls.models import Alert, AlertMatch
from apps.reports import all_facilities as af
from apps.reports.services import overview_kpis
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web import views_all_facilities as web_view
from apps.workorders.models import LaborLine
from apps.workorders.services import change_status, create_work_order

PASSWORD = "Test-Pass-2026-x"
EMAIL = "kim@health.example"
NOW = datetime(2026, 9, 22, 16, 0, tzinfo=dt_timezone.utc)  # Sep 22 in any facility from UTC-12 to UTC+7: both below work in the server's
DAY = date(2026, 9, 22)
PAGE, API = "/overview/all/", "/api/v1/overview/all-facilities/"


@pytest.fixture
def lakeside(db):
    t = Tenant.objects.create(name="Lakeside Surgery Center", slug="lakeside")
    create_default_roles(t)
    return t


def account(tenant, slug, *, username, person=None, pending=False, email=EMAIL):
    role = Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: made outside any facility
    user = User(username=username, email=email, first_name="Kim", last_name="Alvarez", tenant=tenant, role=role, person=person, is_invited=pending)
    if pending:
        user.set_unusable_password()
    else:
        user.set_password(PASSWORD)
    user.last_login = None if pending else timezone.now()
    user._password = None  # created, not changed: nothing to share yet
    user.save()
    return user


@pytest.fixture
def kim(tenant, lakeside):
    """Kim: director at Riverside (where she is signed in), technician at Lakeside (joined)."""
    person = uuid.uuid4()
    return account(tenant, "director", username=EMAIL, person=person), account(lakeside, "technician", username=f"{EMAIL}@lakeside", person=person)


def wo(asset, type_, *, opened, due=None, done=None, hours=0):
    w = create_work_order(asset=asset, type=type_, priority="normal", problem="Check", opened_on=opened, due_on=due)
    if hours:
        LaborLine.objects.create(work_order=w, hours=hours, rate=100)
    if done:
        change_status(w, "in_progress", as_of=opened)
        change_status(w, "completed", as_of=done)
    return w


def match(alert, model, status=AlertMatch.Status.NEEDS_ACTION):
    return AlertMatch.objects.create(alert=alert, device_model=model, status=status)


@pytest.fixture
def shared_alert(db):
    return Alert.objects.create(source=Alert.Source.FDA, external_id="Z-SHARED", manufacturer="BD", product="Pump", title="Shared recall",
                                published_on=DAY - timedelta(days=5))


@pytest.fixture
def riverside_world(tenant, vent, pump, pump_model, shared_alert):
    """Riverside, September 2026 to the 22nd: two devices; one repair of 3 days ($200 of labor); two PMs due (the ventilator's on
    time, the pump's late); one open repair past due; the shared recall needing action."""
    with tenant_context(tenant):
        wo(vent, "repair", opened=date(2026, 9, 18), done=date(2026, 9, 21), hours=2)
        wo(vent, "pm", opened=date(2026, 9, 1), due=date(2026, 9, 10), done=date(2026, 9, 9))
        wo(pump, "pm", opened=date(2026, 9, 1), due=date(2026, 9, 12), done=date(2026, 9, 15))
        wo(pump, "repair", opened=date(2026, 9, 20), due=date(2026, 9, 21))
        match(shared_alert, pump_model)


@pytest.fixture
def lakeside_world(lakeside, shared_alert):
    """Lakeside: three $10,000 monitors; repairs of 1 and 9 days ($100 and $300 of labor); three PMs due, all on time; one repair
    awaiting parts; a $12,000 contract; the shared recall and one of its own needing action, and a closed one."""
    with tenant_context(lakeside):
        dept = Department.objects.create(name="OR")
        model = DeviceModel.objects.create(manufacturer="Philips", model="MX800", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM,
                                           oem_pm_interval_months=12)
        mons = [Asset.objects.create(tag=f"LK-{i}", device_model=model, department=dept, acquisition_cost=10000, next_pm_on=DAY + timedelta(days=90))
                for i in range(3)]
        wo(mons[0], "repair", opened=date(2026, 9, 5), done=date(2026, 9, 6), hours=1)
        wo(mons[1], "repair", opened=date(2026, 9, 10), done=date(2026, 9, 19), hours=3)
        for i, m in enumerate(mons):
            wo(m, "pm", opened=date(2026, 9, 1), due=date(2026, 9, 2 + i), done=date(2026, 9, 2))
        change_status(wo(mons[2], "repair", opened=date(2026, 9, 20), due=date(2026, 9, 30)), "awaiting_parts", as_of=date(2026, 9, 21))
        Contract.objects.create(reference="LK-1", vendor="Philips", type=ContractType.OEM, start_on=date(2026, 1, 1), end_on=date(2027, 1, 1),
                                annual_cost=12000)
        match(shared_alert, model)
        own = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-LAKE", manufacturer="Philips", product="Monitor", title="Lakeside recall")
        match(own, model)
        done = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-DONE", manufacturer="Philips", product="Monitor", title="Closed recall")
        match(done, model, AlertMatch.Status.CLOSED)


@pytest.fixture
def worlds(riverside_world, lakeside_world):
    return None


@pytest.fixture
def pinned(monkeypatch):
    """The page and the API read every facility at NOW."""
    monkeypatch.setattr(web_view, "all_facilities_data", partial(af.all_facilities, now=NOW))
    monkeypatch.setattr(api_view, "all_facilities", partial(af.all_facilities, now=NOW))


def by_slug(data):
    return {r["slug"]: r for r in data["facilities"]}


# --- the numbers ------------------------------------------------------------------------------------------------------------------

def test_each_facility_is_read_inside_itself_with_the_overviews_numbers(kim, worlds, tenant, lakeside):
    a, b = kim
    rows = by_slug(af.all_facilities(a, tenant, now=NOW))
    river, lake = rows["riverside"], rows["lakeside"]
    assert (river["current"], lake["current"], river["account"], lake["account"]) == (True, False, a.pk, b.pk)
    assert river["name"] == "Riverside Regional" and lake["name"] == "Lakeside Surgery Center"
    assert river["today"] == lake["today"] == DAY and river["month"] == lake["month"] == date(2026, 9, 1)
    assert {k: river[k] for k in ("active_devices", "open_work_orders", "overdue_work_orders", "awaiting_parts", "pm_due", "pm_on_time",
                                  "life_support_pm_due", "life_support_pm_on_time", "repairs_closed", "downtime_days", "device_days")} == {
        "active_devices": 2, "open_work_orders": 1, "overdue_work_orders": 1, "awaiting_parts": 0, "pm_due": 2, "pm_on_time": 1,
        "life_support_pm_due": 1, "life_support_pm_on_time": 1, "repairs_closed": 1, "downtime_days": 3, "device_days": 44}
    assert (river["pm_rate"], river["life_support_pm_rate"], river["mttr_days"], river["repair_spend"], river["alerts_needing_action"]) == (
        50.0, 100.0, 3.0, 200.0, 1)
    assert (lake["active_devices"], lake["open_work_orders"], lake["overdue_work_orders"], lake["awaiting_parts"]) == (3, 1, 0, 1)
    assert (lake["pm_due"], lake["pm_on_time"], lake["pm_rate"], lake["life_support_pm_due"], lake["life_support_pm_rate"]) == (3, 3, 100.0, 0, 100.0)
    assert (lake["repairs_closed"], lake["mttr_days"], lake["repair_spend"], lake["alerts_needing_action"]) == (2, 5.0, 400.0, 2)
    assert lake["acquisition_value"] == 30000 and lake["cost_of_service"] > 12000  # the contract and the annualized labor
    # the Overview's own numbers, each read in its own facility
    for t, row in ((tenant, river), (lakeside, lake)):
        with tenant_context(t):
            k = overview_kpis(2026, 9, DAY)
        assert (row["uptime_pct"], row["mttr_days"], row["repair_spend"]) == (k["uptime_pct"], k["mttr_days"], k["repair_spend"])
        assert (row["cost_of_service"], row["acquisition_value"], row["cost_of_service_ratio_pct"]) == (
            k["cost_of_service"]["total"], k["cost_of_service"]["acquisition"], k["cost_of_service"]["ratio_pct"])
        assert row["alerts_needing_action"] == k["alerts"]["needs_action"]


def test_the_totals_add_the_parts_and_work_the_rates_out_from_the_sums(kim, worlds, tenant):
    data = af.all_facilities(kim[0], tenant, now=NOW)
    river, lake = by_slug(data)["riverside"], by_slug(data)["lakeside"]
    t = data["totals"]
    assert t["facilities"] == 2
    assert (t["active_devices"], t["open_work_orders"], t["overdue_work_orders"], t["awaiting_parts"]) == (5, 2, 1, 1)
    assert (t["pm_due"], t["pm_on_time"], t["pm_rate"]) == (5, 4, 80.0)  # not the mean of 50% and 100%
    assert (t["life_support_pm_due"], t["life_support_pm_on_time"], t["life_support_pm_rate"]) == (1, 1, 100.0)
    assert t["repairs_closed"] == 3 and t["mttr_days"] == pytest.approx(13 / 3)  # weighted by repairs: not the mean of 3 and 5
    assert t["repair_spend"] == 600.0
    assert (t["downtime_days"], t["device_days"]) == (13, 110) and t["uptime_pct"] == pytest.approx(100 - 13 / 110 * 100)
    assert t["acquisition_value"] == 71200
    assert t["cost_of_service"] == pytest.approx(river["cost_of_service"] + lake["cost_of_service"])
    assert t["cost_of_service_ratio_pct"] == pytest.approx(t["cost_of_service"] / 71200 * 100)
    assert t["cost_of_service_ratio_pct"] != pytest.approx((river["cost_of_service_ratio_pct"] + lake["cost_of_service_ratio_pct"]) / 2)
    # the shared recall needs action in both: one alert, so two in all, not three
    assert (river["alerts_needing_action"], lake["alerts_needing_action"], t["alerts_needing_action"]) == (1, 2, 2)


def test_each_facility_counts_its_own_today_and_month(kim, tenant, lakeside, vent, pump):
    """At 20:00 UTC on September 30, Riverside (Pago Pago, UTC-11) is still in September and Lakeside (Kiritimati, UTC+14) is already
    in October: a repair Lakeside completed on September 30 is last month's there, and Riverside's on the 30th is this month's."""
    Tenant.objects.filter(pk=tenant.pk).update(timezone="Pacific/Pago_Pago")
    Tenant.objects.filter(pk=lakeside.pk).update(timezone="Pacific/Kiritimati")
    with tenant_context(tenant):
        wo(vent, "repair", opened=date(2026, 9, 28), done=date(2026, 9, 30))
    with tenant_context(lakeside):
        model = DeviceModel.objects.create(manufacturer="Philips", model="MX800", description="Monitor", category="Monitors", oem_pm_interval_months=12)
        mon = Asset.objects.create(tag="LK-1", device_model=model, department=Department.objects.create(name="OR"), acquisition_cost=10000)
        wo(mon, "repair", opened=date(2026, 9, 28), done=date(2026, 9, 30))
    a, _b = kim
    rows = by_slug(af.all_facilities(User.objects.get(pk=a.pk), now=datetime(2026, 9, 30, 20, 0, tzinfo=dt_timezone.utc)))
    river, lake = rows["riverside"], rows["lakeside"]
    assert (river["today"], river["month"], river["repairs_closed"], river["device_days"]) == (date(2026, 9, 30), date(2026, 9, 1), 1, 60)
    assert (lake["today"], lake["month"], lake["repairs_closed"], lake["device_days"]) == (date(2026, 10, 1), date(2026, 10, 1), 0, 1)


def test_a_facility_without_its_overview_is_listed_by_name_only(kim, worlds, tenant, lakeside):
    """Kim's Lakeside role gives no Overview: a vendor technician's (only their company's work orders), a clinical requester's, or a
    role with no Reports View. Lakeside is listed by name, with no figures, and left out of the totals."""
    a, b = kim
    for slug in ("vendor", "requester"):
        User.objects.filter(pk=b.pk).update(role=Role.unscoped.get(tenant=lakeside, slug=slug))
        data = af.all_facilities(a, tenant, now=NOW)
        lake = by_slug(data)["lakeside"]
        assert lake == {"account": b.pk, "name": "Lakeside Surgery Center", "slug": "lakeside", "current": False, "overview": False}
        assert data["totals"]["facilities"] == 1 and data["totals"]["active_devices"] == 2
    User.objects.filter(pk=b.pk).update(role=Role.unscoped.get(tenant=lakeside, slug="technician"))
    with tenant_context(lakeside):
        Role.objects.get(slug="technician").set_levels({"reports": Level.NONE})
    assert by_slug(af.all_facilities(a, tenant, now=NOW))["lakeside"]["overview"] is False


def test_only_facilities_the_person_has_joined_and_can_open(kim, tenant, lakeside):
    a, b = kim
    north = Tenant.objects.create(name="North Campus", slug="north")
    east = Tenant.objects.create(name="East Clinic", slug="east")
    create_default_roles(north)
    create_default_roles(east)
    account(north, "director", username=f"{EMAIL}@north", person=a.person, pending=True)  # invited, not joined
    gone = account(east, "director", username=f"{EMAIL}@east", person=a.person)
    User.objects.filter(pk=gone.pk).update(is_active=False)
    assert [r["slug"] for r in af.all_facilities(a, tenant, now=NOW)["facilities"]] == ["lakeside", "riverside"]
    User.objects.filter(pk=gone.pk).update(is_active=True)
    Tenant.objects.filter(pk=east.pk).update(is_active=False)
    Tenant.objects.filter(pk=lakeside.pk).update(is_active=False)
    assert [r["slug"] for r in af.all_facilities(a, tenant, now=NOW)["facilities"]] == ["riverside"]


def test_one_facility_is_one_row(make_user, tenant, vent):
    user = make_user("director")
    data = af.all_facilities(user, tenant, now=NOW)
    assert [(r["slug"], r["current"], r["active_devices"]) for r in data["facilities"]] == [("riverside", True, 1)]
    assert data["totals"]["facilities"] == 1 and data["totals"]["active_devices"] == 1


# --- the page ---------------------------------------------------------------------------------------------------------------------

def test_the_page_lists_each_facility_with_a_switch_and_the_totals(kim, worlds, client, pinned, tenant):
    a, b = kim
    client.force_login(a)
    body = client.get(PAGE).content.decode()
    assert "<h1>All facilities</h1>" in body and "September 2026 to date" in body
    assert '<option value="all" selected>All facilities</option>' in body  # the top bar's facility menu
    # Lakeside's name is a button that switches to Kim's account there and opens its Overview; Riverside is marked current
    form = re.search(r'<form method="post" action="/account/facility/">(.*?)</form>', body.split("<tbody>")[1], re.S).group(1)
    assert f'name="account" value="{b.pk}"' in form and 'name="screen" value="overview"' in form and ">Lakeside Surgery Center</button>" in form
    assert '<a class="allf-name" href="/">Riverside Regional</a>' in body and '<span class="chip acc">Current</span>' in body
    tfoot = body.split("<tfoot>")[1].split("</tfoot>")[0]
    assert "All facilities" in tfoot and "2 facilities added up" in tfoot
    for text in ('data-label="PM on time"><span class="two">80.0%<small>4 of 5 due</small>', ">4.3 d<small>3 repairs closed</small>",
                 '>13 of 110 device-days down<', 'data-label="Recall alerts needing action"><span class="two">2<', ">$600<"):
        assert text in tfoot
    assert '<span class="two">50.0%<small>1 of 2 due</small>' in body and '<span class="two">5.0 d<small>2 repairs closed</small>' in body
    r = client.post("/account/facility/", {"account": b.pk, "screen": "overview"})  # the button
    assert r.status_code == 302 and r["Location"] == "/" and int(client.session["_auth_user_id"]) == b.pk


def test_the_page_names_a_facility_without_its_overview_and_shows_none_of_it(kim, worlds, client, pinned, lakeside):
    a, b = kim
    User.objects.filter(pk=b.pk).update(role=Role.unscoped.get(tenant=lakeside, slug="vendor"))
    client.force_login(a)
    body = client.get(PAGE).content.decode()
    assert "No access to its Overview" in body and "Lakeside Surgery Center" in body
    assert "<tfoot>" not in body and "3 of 3 due" not in body  # one facility with figures: no totals row, nothing of Lakeside's
    # signed in to Lakeside as its vendor technician (a scoped role there), the page opens and shows Lakeside by name only
    client.force_login(b)
    r = client.get(PAGE)
    assert r.status_code == 200
    body = r.content.decode()
    assert "No access to its Overview" in body and '<span class="allf-name">Lakeside Surgery Center</span>' in body
    assert '<span class="two">50.0%<small>1 of 2 due</small>' in body  # Riverside's, read as Kim's director account there


def test_the_page_for_one_facility_is_one_row(make_user, client, pinned, vent):
    client.force_login(make_user("technician"))
    body = client.get(PAGE).content.decode()
    assert body.count('<th scope="row"') == 1 and "<tfoot>" not in body and "/account/facility/" not in body.split('<main class="view"')[1]
    assert "Each facility you join appears here beside this one." in body


def test_the_page_says_when_facilities_are_in_different_months(kim, client, monkeypatch, tenant, lakeside):
    Tenant.objects.filter(pk=tenant.pk).update(timezone="Pacific/Pago_Pago")
    Tenant.objects.filter(pk=lakeside.pk).update(timezone="Pacific/Kiritimati")
    monkeypatch.setattr(web_view, "all_facilities_data", partial(af.all_facilities, now=datetime(2026, 9, 30, 20, 0, tzinfo=dt_timezone.utc)))
    client.force_login(kim[0])
    body = client.get(PAGE).content.decode()
    assert "each in its own current month to date" in body
    assert "<small>September 2026 to date</small>" in body and "<small>October 2026 to date</small>" in body


def test_a_superuser_sees_the_facility_they_work_in(client, tenant, vent):
    root = User.objects.create_superuser(username="root", password=PASSWORD, email="root@example.com")
    client.force_login(root)
    session = client.session
    session["tenant_id"] = str(tenant.id)
    session.save()
    body = client.get(PAGE).content.decode()
    assert body.count('<th scope="row"') == 1 and "Riverside Regional" in body
    r = client.get(API)
    assert r.status_code == 200 and [f["slug"] for f in r.json()["facilities"]] == ["riverside"]


# --- the API ----------------------------------------------------------------------------------------------------------------------

def test_the_api_serves_the_pages_numbers_by_session_only(kim, worlds, client, pinned, tenant):
    a, b = kim
    client.force_login(a)
    data = client.get(API).json()
    lake, river = data["facilities"]
    assert (lake["slug"], lake["current"], river["slug"], river["current"]) == ("lakeside", False, "riverside", True)
    assert "account" not in lake and lake["today"] == "2026-09-22" and lake["month"] == "2026-09"
    expected = af.all_facilities(a, tenant, now=NOW)
    for got, row in zip(data["facilities"], expected["facilities"], strict=True):
        assert {k: got[k] for k in af.FIGURES} == {k: round(row[k], 2) if isinstance(row[k], float) else row[k] for k in af.FIGURES}
    t = data["totals"]
    assert (t["facilities"], t["pm_rate"], t["mttr_days"], t["alerts_needing_action"]) == (2, 80.0, 4.33, 2)
    assert t["uptime_pct"] == round(100 - 13 / 110 * 100, 2)
    User.objects.filter(pk=b.pk).update(role=Role.unscoped.get(tenant=b.tenant, slug="requester"))
    lake = client.get(API).json()["facilities"][0]
    assert lake["overview"] is False and all(lake[k] is None for k in af.FIGURES) and lake["month"] is None
    token = Token.objects.create(user=a)
    client.logout()
    r = client.get(API, HTTP_AUTHORIZATION=f"Token {token.key}")
    assert r.status_code == 403 and "sign in with a session" in r.json()["detail"]
    assert client.get(API).status_code in (401, 403)


# --- under row-level security -----------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_page_and_the_api_read_each_facility_under_the_policies(kim, worlds, client, pinned):
    """As the runtime role, a read of Lakeside's rows from Riverside finds nothing: Lakeside's figures on the page and in the API
    show each facility was read inside itself."""
    a, _b = kim
    as_app_role()
    client.force_login(a)
    body = client.get(PAGE).content.decode()
    assert '<span class="two">100.0%<small>3 of 3 due</small>' in body and '<span class="two">5.0 d<small>2 repairs closed</small>' in body
    assert '<span class="two">50.0%<small>1 of 2 due</small>' in body and "2 facilities added up" in body
    data = client.get(API).json()
    lake, river = data["facilities"]
    assert (lake["active_devices"], lake["pm_on_time"], lake["alerts_needing_action"], lake["acquisition_value"]) == (3, 3, 2, 30000.0)
    assert (river["active_devices"], river["pm_on_time"], river["alerts_needing_action"]) == (2, 1, 1)
    assert (data["totals"]["active_devices"], data["totals"]["alerts_needing_action"]) == (5, 2)
    assert client.get("/work-orders/").status_code == 200  # the request is back in Riverside afterwards
