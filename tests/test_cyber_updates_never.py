"""
Slice 38 wave 2 C: the never paths of software updates (build38 "Doors": Not changed; "Tests": the never-paths extended).

A software update work order is work every work order door shows (its type, "Software update", and its fixed, neutral problem), but what
it is to install and what it did (a version, the manufacturer's patch list, the target, the state's sentence) are the device security
register's, read only with Device security View and Equipment View (permissions.can_view_devices). So for a reader with Work orders View
and Device security None (a custom role linked to the technician assigned, and the analyst default role) and for a scoped vendor account
(its company's updates), with updates open, completed as installed, completed with the version not confirmed, open for patches, and an
install recorded by hand: no version, reference, target sentence, or "software release" on the work order list, its CSV, the drawer, the
print, the API's retrieve and list (software_update and software_choices null), My work, the Overview (the urgent line shows the update's
neutral problem), Scan, and the device drawer. The director, who reads the register, sees them in the API (each value is real).
"""
from datetime import timedelta

import pytest
from cyber_fixtures import facts, release, update_wo
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.credentials.models import Technician
from apps.cybersecurity import permissions as perms
from apps.cybersecurity.models import InstalledBy, UpdateBasis, UpdateOutcome, UpdatesBy
from apps.cybersecurity.updates import NOT_CONFIRMED, PATCHES_PROBLEM, UPDATE_PROBLEM, UpdateChoice, record
from apps.equipment.models import Asset
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, WorkOrder

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "drawer"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
OLDER, NEWER, LISTED, TARGET_LIST = "5.2-NEVER", "5.6-NEVER", "ICAR-NEVER-09", "ICAR-NEVER-10"
# Distinctive values of the updates' records and the rules' words for them: none may show on a door that is not the register's
UPDATE_VALUES = (OLDER, NEWER, LISTED, TARGET_LIST, "software release", "Open: to install", "to apply the manufacturer's patches",
                 "version not confirmed", "Version not confirmed", "validated patch list", "installed by information",
                 "Release installed", "nothing installed")


def custom_user(tenant, name, levels, scope="", **extra):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=name.title(), last_name="User", **extra)


def body(response) -> str:
    if response.streaming:
        return b"".join(response.streaming_content).decode("utf-8-sig")
    return response.content.decode()


def shown(text: str) -> list:
    return [v for v in UPDATE_VALUES if v in text]


@pytest.fixture
def world(ctx, dept, pump_model, pump, techs, today, make_user):
    """The pumps (CE installs their updates here) on OLDER, NEWER on file. Tom's: CE-10002's update to NEWER open, high priority and past
    due (the Overview's urgent line), CE-10003's completed as installed, CE-10007's completed with the version not confirmed, CE-10006's
    open for patches (TARGET_LIST). BD's (vendor service): CE-10004's open, CE-10005's completed as installed. CE-10008: patches recorded
    by hand (LISTED, by information technology)."""
    release(pump_model, updates_by=UpdatesBy.CE)
    older, newer = release(pump_model, OLDER), release(pump_model, NEWER)
    director = make_user("director")
    devices = {"CE-10002": pump}
    for n in range(3, 9):
        devices[f"CE-1000{n}"] = Asset.objects.create(tag=f"CE-1000{n}", device_model=pump_model, department=dept,
                                                      installed_on=today - 400 * DAY)
    for a in devices.values():
        facts(a, older, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    due = today + 30 * DAY
    wos = {}
    wos["open"], _ = update_wo(pump, newer, assigned_to=techs["tom"], priority=Priority.HIGH, opened_on=today - 5 * DAY, due_on=due)
    WorkOrder.objects.filter(pk=wos["open"].pk).update(due_on=today - DAY)  # past due: the Overview's urgent line
    wos["installed"], _ = update_wo(devices["CE-10003"], newer, assigned_to=techs["tom"], opened_on=today, due_on=due)
    wos["vendor_open"], _ = update_wo(devices["CE-10004"], newer, vendor_service=True, vendor_name="BD", opened_on=today, due_on=due)
    wos["vendor_done"], _ = update_wo(devices["CE-10005"], newer, vendor_service=True, vendor_name="BD", opened_on=today, due_on=due)
    wos["patches"], _ = update_wo(devices["CE-10006"], reference=TARGET_LIST, assigned_to=techs["tom"], opened_on=today, due_on=due)
    wos["not_confirmed"], _ = update_wo(devices["CE-10007"], newer, assigned_to=techs["tom"], opened_on=today, due_on=due)
    installed = UpdateChoice(UpdateOutcome.INSTALLED, release_id=str(newer.pk), basis=UpdateBasis.MANUFACTURER_RELEASE)
    complete_work_order(wos["installed"], software=installed, by=director, today=today)
    complete_work_order(wos["vendor_done"], software=installed, by=director, today=today)
    complete_work_order(wos["not_confirmed"], software=UpdateChoice(NOT_CONFIRMED), resolution="Loaded; version to be checked", by=director,
                        today=today)
    record(devices["CE-10008"], UpdateChoice(UpdateOutcome.PATCHES, reference=LISTED, basis=UpdateBasis.VALIDATED_LIST),
           installed_by=InstalledBy.IT, applied_on=today - 2 * DAY, by=director, today=today)
    for wo in wos.values():
        wo.refresh_from_db()
    return {"wos": wos, "devices": devices, "director": director}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def _readers(tenant, make_user, techs) -> dict:
    """The custom role (Work orders View, Equipment View, Reports View; Device security None) linked to Tom, the technician assigned;
    the analyst default role; a scoped vendor account of BD."""
    custom = custom_user(tenant, "woview", {Module.WORKORDERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW, Module.REPORTS: Level.VIEW})
    Technician.objects.filter(pk=techs["tom"].pk).update(user=custom)
    analyst = make_user("analyst")
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save(update_fields=["company"])
    for user in (custom, analyst, vendor):
        assert not perms.can_view_devices(user)
    return {"custom": custom, "analyst": analyst, "vendor": vendor}


def test_the_values_are_real_for_a_reader_of_the_register(client, world):
    """The control: the director, who reads the register, sees the versions, the lists, and the sentences in the API."""
    client.force_login(world["director"])
    seen = "".join(body(client.get(f"/api/v1/work-orders/{wo.pk}/")) for wo in world["wos"].values())
    seen += body(client.get("/api/v1/software-updates/"))
    assert set(shown(seen)) >= {OLDER, NEWER, LISTED, TARGET_LIST, "Open: to install", "to apply the manufacturer's patches",
                                "version not confirmed", "validated patch list", "installed by information"}


@pytest.mark.parametrize("who", ["custom", "analyst", "vendor"])
def test_no_work_order_door_shows_what_an_update_installs(client, tenant, make_user, techs, world, who):
    reader = _readers(tenant, make_user, techs)[who]
    client.force_login(reader)
    wos = world["wos"]
    theirs = [wos["vendor_open"], wos["vendor_done"]] if who == "vendor" else list(wos.values())
    pages = {}
    for url in ("/work-orders/", "/work-orders/?open=0", "/work-orders/?open=0&mode=board", "/export/work-orders.csv",
                "/export/work-orders.csv?open=0", "/api/v1/work-orders/", "/my-work/"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        pages[url] = body(r)
    listed = pages["/work-orders/?open=0"] + pages["/export/work-orders.csv?open=0"] + pages["/api/v1/work-orders/"]
    assert all(wo.number in listed for wo in theirs)  # the doors list the updates: the type and the neutral problem only
    assert "Software update" in pages["/work-orders/?open=0"] and UPDATE_PROBLEM in pages["/export/work-orders.csv?open=0"]
    for wo in theirs:
        for url in (f"/work-orders/{wo.number}/", f"/print/work-orders/{wo.number}/", f"/api/v1/work-orders/{wo.pk}/"):
            r = client.get(url)
            assert r.status_code == 200, (url, r.status_code)
            pages[url] = body(r)
            assert wo.number in pages[url], url
        drawer = client.get(f"/work-orders/{wo.number}/", **HX)
        assert drawer.status_code == 200
        pages[f"drawer {wo.number}"] = body(drawer)
        data = client.get(f"/api/v1/work-orders/{wo.pk}/").json()
        assert data["software_update"] is None and data["software_choices"] is None
        assert data["problem"] in (UPDATE_PROBLEM, PATCHES_PROBLEM)
    assert all(r["software_update"] is None for r in client.get("/api/v1/work-orders/").json()["results"])
    if who == "custom":  # Tom's account: My work lists his updates
        assert wos["open"].number in pages["/my-work/"] and wos["patches"].number in pages["/my-work/"]
    if who == "vendor":
        assert wos["vendor_open"].number in pages["/my-work/"]
    found = {url: shown(text) for url, text in pages.items() if shown(text)}
    assert found == {}


@pytest.mark.parametrize("who", ["custom", "analyst", "vendor"])
def test_the_overview_scan_and_the_device_drawer_show_none_of_it(client, tenant, make_user, techs, world, who):
    reader = _readers(tenant, make_user, techs)[who]
    client.force_login(reader)
    wos, devices = world["wos"], world["devices"]
    pages = {}
    overview = client.get("/")
    if who == "vendor":
        assert overview.status_code == 302  # the whole facility's: sent on to their first screen
    else:
        assert overview.status_code == 200 and wos["open"].number in body(overview)  # the urgent line: number and neutral problem
        pages["/"] = body(overview)
    tags = ["CE-10004", "CE-10005"] if who == "vendor" else list(devices)
    for tag in tags:
        scan = client.get(f"/scan/?code={tag}", **MODAL)
        assert scan.status_code == 200, tag
        pages[f"scan {tag}"] = body(scan)
        drawer = client.get(f"/equipment/{tag}/", **HX)
        assert drawer.status_code == 200 and "security" not in drawer.context["tabs"], tag
        pages[f"device {tag}"] = body(drawer)
        for tab in ("history", "costs", "pm"):
            r = client.get(f"/equipment/{tag}/?tab={tab}", **HX)
            if r.status_code == 200:
                pages[f"device {tag} {tab}"] = body(r)
    if who == "custom":  # Tom's one open work order on the pump: the label's scan opens it
        assert wos["open"].number in pages["scan CE-10002"]
    found = {url: shown(text) for url, text in pages.items() if shown(text)}
    assert found == {}


def test_a_scoped_user_with_device_security_full_reads_null_too(client, tenant, world):
    """A custom role with a scope and Full everywhere (Device security Full too): never the register's, so never a version."""
    scoped = custom_user(tenant, "scopedfull", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="BD")
    assert not perms.can_view_devices(scoped)
    client.force_login(scoped)
    for wo in (world["wos"]["vendor_open"], world["wos"]["vendor_done"]):
        data = client.get(f"/api/v1/work-orders/{wo.pk}/").json()
        assert data["software_update"] is None and data["software_choices"] is None
        for url in (f"/work-orders/{wo.number}/", f"/print/work-orders/{wo.number}/"):
            r = client.get(url)
            assert r.status_code == 200 and shown(body(r)) == [], url
    assert client.get("/api/v1/software-updates/").status_code == 403
