"""
Slice 37 wave 2 B: the device security register over the API, read only (apps/api/views_cybersecurity.py, serializers_cybersecurity.py).

GET /api/v1/software-releases/ (Device security View): each model's profile and releases with their groups as a device on them reads
them (the profile merged in by group, each with its source), the two standing lines in rules' words, and the devices on each; entered in
error left out unless ?removed=1; ?device_model, ?search; a fixed number of queries. GET /api/v1/device-security/ (Device security View
and Equipment View): every device on site (never a retired one) with its facts and standing words through rules.standings, by id or tag in
any letter case; ?device_model, ?department, ?connected, ?search; a fixed number of queries. Never a scoped user's (a custom scoped role
with Full everywhere neither); a level missing a 403 in words; every write a 405 naming the Device security screen with Edit, a 403
without; another facility's rows a 404; a token reads only its own facility; on PostgreSQL under the policies as the runtime role.
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import release
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import views_cybersecurity as api
from apps.api.permissions import ModulePermission
from apps.cybersecurity import rules, services
from apps.cybersecurity.models import Connects, Mds2Edition, Mds2Status, Network, OsFamily, PatientData, Segmented, UpdatesBy
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.tenants.context import tenant_context

RELEASES = "/api/v1/software-releases/"
DEVICES = "/api/v1/device-security/"
DAY = timedelta(days=1)
RELEASE_FIELDS = {"id", "device_model", "version", "profile", "groups", "security", "os", "devices", "removed_on", "removed_by"}
DEVICE_FIELDS = {"id", "tag", "device_model", "department", "room", "ownership", "ownership_label", "release", "release_since", "release_words",
                 "network", "network_label", "connection", "security", "os", "groups", "checked_on", "checked_by", "url"}
GROUP_KEYS = {"support", "mds2", "disclosure", "facility"}


def never(text: str) -> list:
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def custom_user(tenant, name, levels, scope="", **extra):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)


def ok(r):
    assert r.status_code == 200, r.content
    return r.json()


@pytest.fixture
def world(ctx, dept, pump_model, vent_model, pump, vent, today):
    """The pumps: a profile (wireless, can store patient data, updates by CE, not segmented) and 9.19.2 (MDS2 on file, security updates
    ended) and 9.33.1 (embedded OS past its vendor's support), one entered in error; CE-10002 on 9.33.1 connected wireless, CE-10003 on
    9.19.2, CE-10004 with no release, CE-10005 retired on 9.33.1. The ventilators: nothing on file, CE-10001 not connected here."""
    icu2 = Department.objects.create(name="Med-Surg 4")
    profile = release(pump_model, connects=Connects.WIRELESS, patient_data=PatientData.STORES, updates_by=UpdatesBy.CE,
                      segmented=Segmented.NONE)
    old = release(pump_model, "9.19.2", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001",
                  security_updates_end_on=today - 10 * DAY)
    new = release(pump_model, "9.33.1", os_family=OsFamily.EMBEDDED, os_name="Demo embedded OS 7", os_support_ends_on=today - 5 * DAY)
    gone = release(pump_model, "9.40", removed_on=today)
    second = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=icu2, room="12")
    bare = Asset.objects.create(tag="CE-10004", device_model=pump_model, department=dept)
    retired = Asset.objects.create(tag="CE-10005", device_model=pump_model, department=dept)
    services.record_facts(pump, by=None, release=new, release_since=today - 20 * DAY, network=Network.WIRELESS)
    services.record_facts(second, by=None, release=old)
    services.record_facts(retired, by=None, release=new)
    eq.set_status(retired, AssetStatus.RETIRED)
    services.record_facts(vent, by=None, network=Network.NOT_CONNECTED)
    return {"profile": profile, "old": old, "new": new, "gone": gone, "pump": pump, "second": second, "bare": bare, "retired": retired,
            "vent": vent, "icu2": icu2}


# --- software-releases --------------------------------------------------------------------------------------------------------

def test_the_releases_read_their_groups_standing_and_devices(client, make_user, world, today):
    client.force_login(make_user("technician"))
    data = ok(client.get(RELEASES))
    rows = data["results"]
    assert [(r["device_model"]["model"], r["version"]) for r in rows] == [("Alaris 8015 PCU", ""), ("Alaris 8015 PCU", "9.19.2"),
                                                                          ("Alaris 8015 PCU", "9.33.1")]
    assert all(set(r) == RELEASE_FIELDS and set(r["groups"]) == GROUP_KEYS for r in rows)
    profile, old, new = rows
    assert profile["profile"] and not old["profile"] and profile["devices"] == 1  # CE-10004: no release recorded, reads the profile
    assert (old["devices"], new["devices"]) == (1, 1)  # the retired CE-10005 left out
    assert old["groups"]["mds2"] == {"source": rules.THIS_RELEASE, "mds2": "on_file", "mds2_label": "On file", "mds2_edition": "2019",
                                     "mds2_edition_label": "2019 form (HN 1-2019)", "mds2_document": "DEMO-MDS2-0001",
                                     "mds2_released_on": None, "mds2_requested_on": None, "mds2_url": ""}
    assert old["groups"]["disclosure"]["source"] == rules.PROFILE and old["groups"]["disclosure"]["patient_data_label"] == "Can store patient data"
    assert old["groups"]["facility"] == {"source": rules.PROFILE, "updates_by": "ce", "updates_by_label": "Clinical engineering",
                                         "segmented": "none", "segmented_label": "Not on a separate segment"}
    assert old["security"] == {"key": rules.UPDATES_ENDED, "words": f"Security updates ended after {rules.day_words(today - 10 * DAY)}",
                               "day": (today - 10 * DAY).isoformat()}
    assert new["os"]["key"] == rules.OS_ENDED and new["os"]["words"] == (
        f"The OS vendor's published support for Demo embedded OS 7 ended after {rules.day_words(today - 5 * DAY)}")
    assert new["groups"]["support"]["os_family_label"] == "Embedded or real-time" and new["groups"]["support"]["os_support_ends_on"] == (
        today - 5 * DAY).isoformat()
    assert profile["security"]["words"] == "Security updates end: not recorded" and profile["groups"]["support"]["source"] == rules.NOT_RECORDED
    assert profile["device_model"]["url"] == f"/security/models/{world['pump'].device_model_id}/?facility=riverside"
    assert all(r["removed_on"] is None and r["removed_by"] is None for r in rows)
    assert not never(str(data))


def test_releases_entered_in_error_and_the_filters(client, make_user, world, vent_model):
    client.force_login(make_user("technician"))
    release(vent_model, "3.1")
    rows = ok(client.get(f"{RELEASES}?removed=1"))["results"]
    gone = next(r for r in rows if r["version"] == "9.40")
    assert gone["removed_on"] and gone["security"] is None and gone["os"] is None and gone["groups"]["support"]["source"] == rules.NOT_RECORDED
    assert [r["version"] for r in ok(client.get(f"{RELEASES}?device_model={vent_model.pk}"))["results"]] == ["3.1"]
    assert [r["version"] for r in ok(client.get(f"{RELEASES}?search=9.33"))["results"]] == ["9.33.1"]
    assert len(ok(client.get(f"{RELEASES}?search=hamilton"))["results"]) == 1
    r = client.get(f"{RELEASES}?device_model=nope&removed=yes")
    assert r.status_code == 400 and set(r.json()) == {"device_model", "removed"}
    assert ok(client.get(f"{RELEASES}{world['gone'].pk}/"))["version"] == "9.40"  # one by id, entered in error too
    one = ok(client.get(f"{RELEASES}{world['new'].pk}/"))
    assert set(one) == RELEASE_FIELDS and one["devices"] == 1
    assert client.get(f"{RELEASES}not-an-id/").json()["detail"] == api.RELEASE_NOT_FOUND


def test_the_releases_read_a_fixed_number_of_queries(client, make_user, world, dept, vent_model, pump_model):
    client.force_login(make_user("technician"))

    def counted():
        with CaptureQueriesContext(connection) as q:
            ok(client.get(RELEASES))
        return len(q)

    first = counted()
    release(vent_model, connects=Connects.WIRED)
    for i in range(4):
        r = release(vent_model, f"3.{i}")
        a = Asset.objects.create(tag=f"V-{i}", device_model=vent_model, department=dept)
        services.record_facts(a, by=None, release=r)
        release(pump_model, f"10.{i}")
    assert counted() == first


# --- device-security ----------------------------------------------------------------------------------------------------------

def test_every_device_on_site_reads_its_facts_and_standing(client, make_user, world, today):
    client.force_login(make_user("technician"))
    data = ok(client.get(DEVICES))
    rows = {r["tag"]: r for r in data["results"]}
    assert list(rows) == ["CE-10001", "CE-10002", "CE-10003", "CE-10004"]  # by tag; the retired CE-10005 left out
    assert all(set(r) == DEVICE_FIELDS and set(r["groups"]) == GROUP_KEYS for r in rows.values())
    pump = rows["CE-10002"]
    assert pump["release"] == {"id": str(world["new"].pk), "version": "9.33.1"} and pump["release_since"] == (today - 20 * DAY).isoformat()
    assert pump["release_words"] == f"9.33.1, since {rules.day_words(today - 20 * DAY)}"
    assert (pump["network"], pump["network_label"]) == ("wireless", "Connected (wireless)")
    assert pump["connection"] == {"key": rules.CONNECTED, "words": "Connected (wireless)", "here": True}
    assert pump["security"]["key"] == rules.SECURITY_NOT_RECORDED and pump["os"]["key"] == rules.OS_ENDED
    assert pump["groups"]["support"]["source"] == rules.THIS_RELEASE and pump["groups"]["disclosure"]["source"] == rules.PROFILE
    assert pump["checked_on"] == today.isoformat() and pump["checked_by"] is None
    assert pump["url"] == "/equipment/CE-10002/?tab=security&facility=riverside" and pump["ownership"] == "owned"
    assert pump["department"]["name"] == "ICU"
    bare = rows["CE-10004"]
    assert bare["release"] is None and bare["release_words"] == rules.RELEASE_NOT_RECORDED
    assert bare["connection"] == {"key": rules.CAN_CONNECT, "words": "Can connect (wireless), as disclosed", "here": False}
    assert rows["CE-10003"]["security"]["words"] == f"Security updates ended after {rules.day_words(today - 10 * DAY)}"
    assert rows["CE-10003"]["room"] == "12" and rows["CE-10003"]["department"]["name"] == "Med-Surg 4"
    vent = rows["CE-10001"]
    assert vent["connection"]["key"] == rules.NOT_CONNECTED and vent["groups"]["support"]["source"] == rules.NOT_RECORDED
    assert not never(str(data))
    s = rules.device_standing(world["pump"], today)
    assert (pump["security"]["words"], pump["os"]["words"]) == (s.security.words, s.os.words)  # the drawer's words


def test_the_device_filters(client, make_user, world, pump_model, vent_model):
    client.force_login(make_user("technician"))

    def tags(query):
        return [r["tag"] for r in ok(client.get(DEVICES + query))["results"]]

    assert tags(f"?device_model={pump_model.pk}") == ["CE-10002", "CE-10003", "CE-10004"]
    assert tags(f"?department={world['icu2'].pk}") == ["CE-10003"]
    assert tags("?connected=connected") == ["CE-10002"] and tags("?connected=can_connect") == ["CE-10003", "CE-10004"]
    assert tags("?connected=not_connected") == ["CE-10001"] and tags("?connected=not_recorded") == []
    assert tags(f"?device_model={vent_model.pk}&connected=not_connected") == ["CE-10001"]
    assert tags("?search=10003") == ["CE-10003"]
    r = client.get(f"{DEVICES}?device_model=x&department=y&connected=yes")
    assert r.status_code == 400 and set(r.json()) == {"device_model", "department", "connected"}


def test_one_device_by_id_or_tag(client, make_user, world, other_tenant):
    client.force_login(make_user("technician"))
    assert ok(client.get(f"{DEVICES}{world['pump'].pk}/"))["tag"] == "CE-10002"
    one = ok(client.get(f"{DEVICES}ce-10002/"))
    assert set(one) == DEVICE_FIELDS and one["release"]["version"] == "9.33.1"
    assert client.get(f"{DEVICES}CE-10005/").json()["detail"] == api.DEVICE_NOT_FOUND  # retired: not on site
    assert client.get(f"{DEVICES}{world['retired'].pk}/").status_code == 404
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        their_release = release(model, "9.33.1")
    assert client.get(f"{DEVICES}{theirs.pk}/").status_code == 404 and client.get(f"{DEVICES}THEIRS-1/").status_code == 404
    assert client.get(f"{RELEASES}{their_release.pk}/").status_code == 404
    assert "THEIRS-1" not in str(ok(client.get(DEVICES))) and str(their_release.pk) not in str(ok(client.get(RELEASES)))


def test_the_devices_read_a_fixed_number_of_queries(client, make_user, world, dept, vent_model, pump_model):
    client.force_login(make_user("technician"))

    def counted():
        with CaptureQueriesContext(connection) as q:
            ok(client.get(DEVICES))
        return len(q)

    first = counted()
    release(vent_model, connects=Connects.WIRED)
    for i in range(6):
        model = vent_model if i % 2 else pump_model
        a = Asset.objects.create(tag=f"X-{i}", device_model=model, department=dept)
        services.record_facts(a, by=None, release=release(model, f"20.{i}"), network=Network.WIRED)
    assert counted() == first


# --- who reads it, and nothing is written ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["director", "manager", "technician"])
def test_device_security_readers_read_both(client, make_user, world, slug):
    client.force_login(make_user(slug))
    assert client.get(RELEASES).status_code == 200 and client.get(DEVICES).status_code == 200
    assert client.get(f"{DEVICES}CE-10002/").status_code == 200


def test_the_levels_each_endpoint_needs(client, make_user, tenant, world):
    client.force_login(make_user("analyst"))  # Device security None
    for url, words in ((RELEASES, api.RELEASES_REFUSAL), (f"{RELEASES}{world['new'].pk}/", api.RELEASES_REFUSAL), (DEVICES, api.DEVICES_REFUSAL),
                       (f"{DEVICES}CE-10002/", api.DEVICES_REFUSAL)):
        r = client.get(url)
        assert r.status_code == 403 and r.json()["detail"] == words, url
    client.force_login(custom_user(tenant, "models", {Module.CYBERSECURITY: Level.FULL}))  # no Equipment View: models only
    assert client.get(RELEASES).status_code == 200 and client.get(f"{RELEASES}{world['new'].pk}/").status_code == 200
    for url in (DEVICES, f"{DEVICES}CE-10002/"):
        r = client.get(url)
        assert r.status_code == 403 and r.json()["detail"] == api.DEVICES_REFUSAL
    client.force_login(custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}))
    assert client.get(DEVICES).status_code == 200


def test_scoped_users_are_refused_every_endpoint(client, make_user, tenant, world):
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save()
    company_full = custom_user(tenant, "companyfull", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="BD")
    unit_full = custom_user(tenant, "unitfull", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    urls = (RELEASES, f"{RELEASES}{world['new'].pk}/", DEVICES, f"{DEVICES}CE-10002/", f"{DEVICES}{world['pump'].pk}/")
    for user in (vendor, make_user("requester"), company_full, unit_full):
        client.force_login(user)
        for url in urls:
            r = client.get(url)
            assert r.status_code == 403, (user.username, url)
            if user in (company_full, unit_full):
                assert r.json()["detail"] == ModulePermission.SCOPED_REFUSAL
        for method, url in (("post", RELEASES), ("patch", urls[1]), ("delete", urls[1]), ("post", DEVICES), ("put", urls[3])):
            assert getattr(client, method)(url, {}, content_type="application/json").status_code == 403


def test_every_write_is_refused(client, make_user, tenant, world):
    writes = [("post", RELEASES), ("put", f"{RELEASES}{world['new'].pk}/"), ("patch", f"{RELEASES}{world['new'].pk}/"),
              ("delete", f"{RELEASES}{world['new'].pk}/"), ("post", DEVICES), ("put", f"{DEVICES}CE-10002/"), ("patch", f"{DEVICES}CE-10002/"),
              ("delete", f"{DEVICES}CE-10002/")]
    client.force_login(make_user("director"))  # Full: still no write here
    for method, url in writes:
        r = getattr(client, method)(url, {"version": "9.50", "network": "not_connected"}, content_type="application/json")
        assert r.status_code == 405 and r.json()["detail"] == api.READ_ONLY, (method, url)
    client.force_login(make_user("technician"))  # Edit
    assert all(getattr(client, m)(u, {}, content_type="application/json").status_code == 405 for m, u in writes)
    client.force_login(custom_user(tenant, "itread", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}))
    for method, url in writes:
        r = getattr(client, method)(url, {}, content_type="application/json")
        assert r.status_code == 403 and r.json()["detail"] == api.WRITE_REFUSAL, (method, url)
    client.force_login(custom_user(tenant, "noequip", {Module.CYBERSECURITY: Level.EDIT}))
    r = client.post(DEVICES, {}, content_type="application/json")
    assert r.status_code == 403 and r.json()["detail"] == api.WRITE_REFUSAL
    assert "Device security screen" in api.READ_ONLY and "Device security screen" in api.WRITE_REFUSAL
    world["new"].refresh_from_db()
    assert world["new"].version == "9.33.1" and world["pump"].cyber.network == Network.WIRELESS


def test_the_browsable_api_renders(client, make_user, world):
    client.force_login(make_user("technician"))
    for url in (RELEASES, DEVICES, f"{DEVICES}CE-10002/"):
        r = client.get(url, HTTP_ACCEPT="text/html")
        assert r.status_code == 200 and b"9.33.1" in r.content and b"<html" in r.content, url


def test_a_token_reads_only_its_own_facility(client, make_user, world, other_tenant):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="CE-10002", device_model=model, department=dept)  # the same tag in another facility
        services.record_facts(theirs, by=None, release=release(model, "1.0"))
        role = Role.objects.get(slug="director")
    them = User.objects.create_user(username="director@other.example", password="Test-Pass-2026-x", tenant=other_tenant, role=role)
    auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=them).key}"}
    rows = client.get(DEVICES, **auth).json()["results"]
    assert [(r["tag"], r["release"]["version"]) for r in rows] == [("CE-10002", "1.0")]
    assert rows[0]["url"] == "/equipment/CE-10002/?tab=security&facility=other"
    assert [r["version"] for r in client.get(RELEASES, **auth).json()["results"]] == ["1.0"]
    assert client.get(f"{DEVICES}{world['pump'].pk}/", **auth).status_code == 404
    assert client.get(f"{RELEASES}{world['new'].pk}/", **auth).status_code == 404


@needs_postgres
def test_the_api_under_the_policies_by_token(client, make_user, world, other_tenant):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        their_release = release(model, "9.33.1")
        services.record_facts(theirs, by=None, release=their_release)
    auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=make_user('technician')).key}"}
    as_app_role()
    assert [r["tag"] for r in client.get(DEVICES, **auth).json()["results"]] == ["CE-10001", "CE-10002", "CE-10003", "CE-10004"]
    assert [r["version"] for r in client.get(RELEASES, **auth).json()["results"]] == ["", "9.19.2", "9.33.1"]
    data = client.get(f"{DEVICES}ce-10002/", **auth).json()
    assert data["release"]["version"] == "9.33.1" and data["groups"]["disclosure"]["source"] == rules.PROFILE
    assert client.get(f"{DEVICES}{theirs.pk}/", **auth).status_code == 404
    assert client.get(f"{RELEASES}{their_release.pk}/", **auth).status_code == 404
    assert ok(client.get(f"{RELEASES}{world['old'].pk}/", **auth))["devices"] == 1
