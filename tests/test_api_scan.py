"""Slice 19, part C: Scan tag over the API (apps/api/views_scan.py), GET /api/v1/scan/?code=.

The code is read as the Equipment screen's Scan tag reads it (apps/equipment/scan.py: a tag in any letter case, a label's request link
with or without its scheme and host, a link to the device's page, a scanner's trailing CR/LF), and the device comes back as
/api/v1/assets/<id>/ shows it. A code that names none of the user's devices is a 404 in the screen's words. Scoped users are admitted
and look up only their share: a device outside it reads exactly like a tag that does not exist. By session and by token, and as the
runtime role under PostgreSQL's row-level security."""
from urllib.parse import quote

import pytest
from django.urls import reverse
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.equipment import scan as scan_rules
from apps.equipment.models import Asset, Department, DeviceModel
from apps.facility.services import asset_request_url
from apps.tenants.context import tenant_context
from apps.workorders.services import create_work_order

SCAN = "/api/v1/scan/"
HAMILTON = "Hamilton Medical field service"  # how work orders name the vendor persona's company


@pytest.fixture
def world(ctx, dept, vent, pump, vent_model):
    """The ICU's vent (CE-10001, Hamilton's vendor work on it) and pump (CE-10002); Med-Surg's vent (MED-3, Hamilton's too)."""
    med = Department.objects.create(name="Med-Surg 4")
    med_vent = Asset.objects.create(tag="MED-3", device_model=vent_model, department=med)
    for asset in (vent, med_vent):
        create_work_order(asset=asset, type="repair", priority="normal", problem="Alarm", vendor_service=True, vendor_name=HAMILTON)
    return {"vent": vent, "pump": pump, "med_vent": med_vent}


@pytest.fixture
def theirs(other_tenant):
    """Another facility's devices: THEIRS-1, and a CE-10001 of its own."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        dept = Department.objects.create(name="ICU")
        return {"only": Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept),
                "same_tag": Asset.objects.create(tag="CE-10001", device_model=model, department=dept)}


def sign_in(client, make_user, slug, tenant_=None, **fields):
    user = make_user(slug, tenant_=tenant_)
    for name, value in fields.items():
        setattr(user, name, value)
    user.save()
    client.force_login(user)
    return user


def scan(client, code, **extra):
    return client.get(SCAN, {"code": code}, **extra)


def found(response, asset) -> dict:
    assert response.status_code == 200, response.content
    data = response.json()
    assert data["id"] == str(asset.pk) and data["tag"] == asset.tag
    return data


def not_found(response, words: str) -> None:
    assert response.status_code == 404, response.content
    assert response.json() == {"detail": words}


def no_device(tag) -> str:
    return scan_rules.NO_DEVICE.format(tag=tag)


def auth(token) -> dict:
    return {"HTTP_AUTHORIZATION": f"Token {token.key}"}


# --- what a code may be ------------------------------------------------------------------------------------------------------------

def test_every_kind_of_code_finds_the_device_as_the_assets_endpoint_shows_it(client, make_user, world):
    sign_in(client, make_user, "technician")
    vent = world["vent"]
    shown = client.get(f"/api/v1/assets/{vent.pk}/").json()
    link = asset_request_url(vent)
    without_scheme = link.split("://", 1)[1]
    for code in ["CE-10001", "ce-10001", "  CE-10001\r\n", "\x02CE-10001\x03\t", link, link.upper(), without_scheme, "/equipment/CE-10001/",
                 "https://cadence.example.org/equipment/ce-10001", f"/r/riverside/?asset={quote('CE-10001')}"]:
        assert found(scan(client, code), vent) == shown, repr(code)


@pytest.mark.parametrize("code, words", [
    ("CE-99999", no_device("CE-99999")),
    ("THEIRS-1", no_device("THEIRS-1")),  # another facility's device is not one of ours
    ("https://cadence.example.org/r/other/?asset=THEIRS-1", scan_rules.ANOTHER_FACILITY),
    ("https://cadence.example.org/r/riverside/", scan_rules.REQUEST_FORM),
    ("https://example.com/some/page", scan_rules.NOT_A_LABEL),
    ("CE-" + "9" * 400, scan_rules.TOO_LONG),
    ("/r/riverside/?asset=CE%0010001", scan_rules.NOT_A_LABEL),  # a NUL decoded from the link: no tag, never echoed back
    ("CE 10001", no_device("CE 10001")),  # tags have no spaces
    ("/equipment/CE%0010001/", scan_rules.NOT_A_LABEL),
])
def test_a_code_that_names_none_is_a_404_in_the_screens_words(client, make_user, world, theirs, code, words):
    sign_in(client, make_user, "director")
    not_found(scan(client, code), words)
    page = client.get(reverse("web:scan"), {"code": code}, HTTP_HX_REQUEST="true")  # the screen says the same
    assert escape(words) in page.content.decode()


def test_a_missing_code_is_a_400(client, make_user, world):
    sign_in(client, make_user, "director")
    for params in ({}, {"code": ""}, {"code": " \r\n"}):
        r = client.get(SCAN, params)
        assert r.status_code == 400 and list(r.json()) == ["code"], params


def test_our_tag_is_ours_even_when_another_facility_has_it_too(client, make_user, world, theirs, other_tenant):
    sign_in(client, make_user, "director")
    found(scan(client, "CE-10001"), world["vent"])
    client.force_login(make_user("director", tenant_=other_tenant))
    found(scan(client, "CE-10001"), theirs["same_tag"])
    found(scan(client, "theirs-1"), theirs["only"])
    not_found(scan(client, "CE-10002"), no_device("CE-10002"))
    not_found(scan(client, asset_request_url(world["pump"])), scan_rules.ANOTHER_FACILITY)


# --- who may scan ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["director", "manager", "technician", "analyst"])
def test_equipment_view_scans(client, make_user, world, role):
    sign_in(client, make_user, role)
    found(scan(client, "CE-10002"), world["pump"])


def test_without_equipment_view_or_signed_out_it_is_refused(client, tenant, world):
    assert scan(client, "CE-10001").status_code == 403  # signed out (SessionAuthentication comes first: no challenge)
    role = Role.objects.create(name="Dispatch", slug="dispatch")
    role.set_levels({Module.WORKORDERS: Level.VIEW})
    client.force_login(User.objects.create_user(username="dispatch@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    assert scan(client, "CE-10001").status_code == 403


def test_a_superuser_with_no_facility_is_told_to_pick_one(client, db):
    client.force_login(User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com"))
    r = scan(client, "CE-10001")
    assert r.status_code == 403 and r.json()["detail"] == "Pick a tenant first (Admin, Tenants)."


# --- scoped users: their share, and nothing about the rest -------------------------------------------------------------------------

def test_the_vendor_finds_only_their_companys_devices(client, make_user, world):
    sign_in(client, make_user, "vendor", company="Hamilton Medical")
    found(scan(client, "CE-10001"), world["vent"])
    found(scan(client, asset_request_url(world["med_vent"])), world["med_vent"])
    outside, missing = scan(client, "CE-10002"), scan(client, "CE-99999")  # the ICU pump has no Hamilton work order
    not_found(outside, no_device("CE-10002"))
    not_found(missing, no_device("CE-99999"))
    assert outside.json()["detail"].replace("CE-10002", "") == missing.json()["detail"].replace("CE-99999", "")  # the same words


def test_the_requester_finds_only_their_units_devices(client, make_user, world):
    sign_in(client, make_user, "requester", department="icu")  # any letter case
    found(scan(client, "CE-10001"), world["vent"])
    found(scan(client, "ce-10002"), world["pump"])
    not_found(scan(client, "MED-3"), no_device("MED-3"))
    not_found(scan(client, "/equipment/MED-3/"), no_device("MED-3"))


def test_a_scoped_role_with_full_levels_still_finds_only_its_share(client, tenant, world):
    role = Role.objects.create(name="Unit lead", slug="unit-lead", scope=DataScope.DEPARTMENT)
    role.set_levels({m: Level.FULL for m in Module.values})
    user = User.objects.create_user(username="lead@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, department="Med-Surg 4")
    client.force_login(user)
    found(scan(client, "MED-3"), world["med_vent"])
    not_found(scan(client, "CE-10001"), no_device("CE-10001"))


# --- by token ------------------------------------------------------------------------------------------------------------------------

def test_by_token(client, make_user, world, theirs, other_tenant):
    mine = Token.objects.create(user=make_user("technician"))
    found(scan(client, "CE-10001", **auth(mine)), world["vent"])
    vendor = make_user("vendor", username="fse@hamilton.example")
    vendor.company = "Hamilton Medical"
    vendor.save()
    not_found(scan(client, "CE-10002", **auth(Token.objects.create(user=vendor))), no_device("CE-10002"))
    other = Token.objects.create(user=make_user("director", tenant_=other_tenant))
    found(scan(client, "CE-10001", **auth(other)), theirs["same_tag"])


@needs_postgres
def test_scan_by_token_under_the_policies(client, tenant, other_tenant, make_user):
    """As the runtime role, with no tenant set before the request: the token's facility, and a scoped user's share."""
    create_default_roles(other_tenant)
    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators")
        icu, med = Department.objects.create(name="ICU"), Department.objects.create(name="Med-Surg 4")
        vent = Asset.objects.create(tag="CE-10001", device_model=model, department=icu)
        Asset.objects.create(tag="MED-3", device_model=model, department=med)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        their_vent = Asset.objects.create(tag="CE-10001", device_model=model, department=Department.objects.create(name="ICU"))
    mine = Token.objects.create(user=make_user("technician"))
    requester = make_user("requester")
    requester.department = "ICU"
    requester.save()
    unit = Token.objects.create(user=requester)
    theirs = Token.objects.create(user=make_user("director", tenant_=other_tenant))
    as_app_role()
    assert scan(client, "ce-10001", **auth(mine)).json()["id"] == str(vent.pk)
    assert scan(client, "MED-3", **auth(mine)).status_code == 200
    assert scan(client, "/equipment/CE-10001/", **auth(unit)).json()["id"] == str(vent.pk)
    not_found(scan(client, "MED-3", **auth(unit)), no_device("MED-3"))
    assert scan(client, "CE-10001", **auth(theirs)).json()["id"] == str(their_vent.pk)
    not_found(scan(client, "MED-3", **auth(theirs)), no_device("MED-3"))
