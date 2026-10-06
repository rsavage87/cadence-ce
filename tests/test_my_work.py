"""
My work (slice 24 scaffold, apps/workorders/my_work.py): whose work, scope first (a vendor's company even when their account carries a
technician profile, nobody's for a requester, else the user's own active technician profile), the groups a technician works from, the
one "due" the badge uses, hours credited to the technician, the nav entry, and signing in to My work.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department
from apps.recalls.models import Alert
from apps.workorders import my_work
from apps.workorders.models import LaborLine, Priority, WoStatus, WoType
from apps.workorders.services import create_work_order

TODAY = date(2026, 10, 6)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(timezone, "localdate", lambda *a, **kw: TODAY)
    return TODAY


@pytest.fixture
def floor(ctx, vent_model, pump_model):
    """Two units, a device in each, and Dana, a technician with an account."""
    icu, ed = Department.objects.create(name="ICU"), Department.objects.create(name="ED")
    vent = Asset.objects.create(tag="ICU-1", device_model=vent_model, department=icu, room="12")
    pump = Asset.objects.create(tag="ED-1", device_model=pump_model, department=ed, room="3")
    pump2 = Asset.objects.create(tag="ED-2", device_model=pump_model, department=ed, room="1")
    return {"vent": vent, "pump": pump, "pump2": pump2}


def tech_for(user, name="Dana Whitfield", **fields):
    return Technician.objects.create(user=user, name=name, **fields)


def wo(asset, *, type=WoType.REPAIR, priority=Priority.NORMAL, due=TODAY, tech=None, status=WoStatus.OPEN, **extra):
    w = create_work_order(asset=asset, type=type, priority=priority, problem="Alarm", assigned_to=tech, opened_on=TODAY - timedelta(days=10),
                          due_on=due, **extra)
    if status != WoStatus.OPEN:
        w.__class__.objects.filter(pk=w.pk).update(status=status)
        w.refresh_from_db()
    return w


def test_whose_work_is_decided_by_scope_first(floor, make_user):
    dana = make_user("technician")
    assert my_work.whose(dana).kind is None and not my_work.has_my_work(dana)  # no profile: no My work
    t = tech_for(dana)
    assert my_work.whose(dana).kind == "technician" and my_work.whose(dana).technician == t
    Technician.objects.filter(pk=t.pk).update(is_active=False)
    assert my_work.whose(dana).kind is None  # a profile no longer active
    requester = make_user("requester")
    tech_for(requester, name="Rita Requester")
    assert my_work.whose(requester).kind is None  # a unit's requester has no My work, whatever their account carries


def test_a_vendor_with_a_technician_profile_sees_only_their_companys_work(floor, make_user, frozen):
    """The leak the design review found: an account moved to the vendor role keeps its technician profile, and work assigned to that
    profile is in-house work outside the vendor's share. My work lists the company's work only."""
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    profile = tech_for(vendor, name="Val Vendor")
    inside = wo(floor["vent"], vendor_service=True, vendor_name="Hamilton Medical")
    outside = wo(floor["pump"], tech=profile)  # in-house, assigned to the profile, on a device outside the share
    assert my_work.whose(vendor).kind == "company"
    assert list(my_work.mine(vendor)) == [inside] and outside not in my_work.mine(vendor)
    assert my_work.due_count(vendor, TODAY) == (1, False)


def test_the_groups_follow_what_a_technician_does_next(floor, make_user, frozen):
    dana = make_user("technician")
    t = tech_for(dana)
    low = wo(floor["pump"], priority=Priority.LOW, due=TODAY - timedelta(days=1), tech=t)
    crit = wo(floor["pump2"], priority=Priority.CRITICAL, due=TODAY + timedelta(days=3), tech=t)
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-1", classification="Class II", manufacturer="BD", product="Pump",
                                 title="Keypad", published_on=TODAY)
    recall_a = wo(floor["pump2"], type=WoType.RECALL, tech=t, alert=alert)
    recall_b = wo(floor["pump"], type=WoType.RECALL, tech=t, alert=alert)
    pm_late = wo(floor["vent"], type=WoType.PM, due=TODAY - timedelta(days=2), tech=t)
    pm_today = wo(floor["pump"], type=WoType.PM, due=TODAY, tech=t)
    pm_soon = wo(floor["pump2"], type=WoType.PM, due=TODAY + timedelta(days=5), tech=t)
    wo(floor["pump2"], type=WoType.PM, due=TODAY + timedelta(days=20), tech=t)
    waiting = wo(floor["vent"], tech=t, status=WoStatus.AWAITING_PARTS)
    wo(floor["vent"], tech=None)  # not theirs
    g = my_work.groups(dana, TODAY)
    assert g.repairs == [crit, low]  # by priority, then due date
    assert [(b["alert"], b["work_orders"]) for b in g.recalls] == [(alert, [recall_a, recall_b])]  # one row per alert, by location (ED room 1, then 3)
    assert g.pms_today == [pm_today, pm_late]  # by location: ED before ICU
    assert g.coming == [pm_soon] and g.later == 1 and g.waiting == [waiting]
    assert my_work.due_count(dana, TODAY) == (5, True)  # low, the two recalls, both PMs due; waiting on parts is not "due"


def test_hours_are_those_credited_to_the_technician(floor, make_user, frozen):
    dana = make_user("technician")
    t = tech_for(dana)
    w = wo(floor["pump"], tech=t)
    for day, hours in ((TODAY, "1.5"), (TODAY - timedelta(days=1), "2"), (TODAY - timedelta(days=9), "4")):
        LaborLine.objects.create(work_order=w, technician=t, worked_on=day, hours=Decimal(hours), rate=Decimal("85"))
    LaborLine.objects.create(work_order=w, technician=None, worked_on=TODAY, hours=Decimal("3"), rate=Decimal("200"))  # vendor time
    assert my_work.hours(dana, TODAY) == {"today": Decimal("1.5"), "week": Decimal("3.5")}  # Monday Oct 5 on
    assert my_work.hours(make_user("vendor"), TODAY) is None


def test_the_page_the_nav_and_the_badge(client, floor, make_user, frozen):
    dana = make_user("technician")
    t = tech_for(dana)
    late = wo(floor["pump"], due=TODAY - timedelta(days=1), tech=t)
    client.force_login(dana)
    r = client.get("/my-work/")
    assert r.status_code == 200 and late.number in r.content.decode()
    nav = {i["key"]: i for i in r.context["shell"]["nav"]}
    assert list(nav)[0] == "my_work" and nav["my_work"]["count"] == 1 and nav["my_work"]["hot"]
    part = client.get("/my-work/", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="my-work-body")
    assert part.status_code == 200 and part.content.decode().lstrip().startswith('<div id="my-work-body"')  # the list alone
    director = make_user("director")
    client.force_login(director)
    assert "my_work" not in {i["key"] for i in client.get("/").context["shell"]["nav"]}
    body = client.get("/my-work/").content.decode()
    assert "no technician profile" in body


def test_signing_in_starts_a_technician_on_my_work(client, floor, make_user):
    dana = make_user("technician")
    tech_for(dana)
    r = client.post("/login/", {"username": dana.username, "password": "Test-Pass-2026-x"})
    assert r.status_code == 302 and r["Location"] == "/my-work/"
    client.logout()
    r = client.post("/login/?next=/equipment/", {"username": dana.username, "password": "Test-Pass-2026-x"})
    assert r["Location"] == "/equipment/"  # a link they followed still wins
    client.logout()
    director = make_user("director")
    assert client.post("/login/", {"username": director.username, "password": "Test-Pass-2026-x"})["Location"] == "/"


@needs_postgres
def test_signing_in_and_the_page_under_the_policies(client, floor, make_user):
    """The landing reads the role inside the facility (the sign-in request began with none set); the page reads only this facility."""
    dana = make_user("technician")
    t = tech_for(dana)
    w = wo(floor["pump"], due=timezone.localdate(), tech=t)
    as_app_role()
    r = client.post("/login/", {"username": dana.username, "password": "Test-Pass-2026-x"})
    assert r["Location"] == "/my-work/"
    assert w.number in client.get("/my-work/").content.decode()
