"""
Slice 37 wave 2 B: the never paths of the device security register (build37 "Never paths").

- Nothing reaches the device's or the model's existing doors: AssetSerializer and DeviceModelSerializer keep their slice 36 field sets
  (the register is never nested into them), the Equipment table's and CSV's columns are unchanged, and custom reports keep their sources
  and the devices source its columns and filters.
- Never a scoped user's: every Device security URL and the register's API routes (slice 39: security decisions too) refuse a custom
  role with a scope and Full everywhere (a vendor's company or a requester's unit), every method; the device drawer never offers them
  the Security tab.
- The history areas (software_releases, device_security, and slice 39's device_security_decisions) need Device security View (and
  Equipment View for a device's facts and decisions), and are never in a scoped user's change log.
- No email path imports apps.cybersecurity (apps/notifications, apps/reports/subscriptions.py, apps/portal: their source scanned), and the
  portal, the labels, and Scan render no register field.
- The never-words (rules.NEVER_WORDS) over the drawer's Security tab and Record facts in every state, and over both API bodies.
- Ids from another facility are refused by every writer door: Record facts, and the Device security screen's release and bulk forms
  (nothing changes in either facility, whatever the screen answers).
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from cyber_fixtures import release
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.api import urls as api_urls
from apps.api import views_cybersecurity as api_cyber
from apps.api.serializers import AssetSerializer, DeviceModelSerializer
from apps.core import history
from apps.cybersecurity import decisions, rules, services
from apps.cybersecurity.models import (
    AcceptedBy,
    Connects,
    DecisionResponse,
    DeviceSecurity,
    Disclosed,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.web import urls_cybersecurity

ROOT = Path(__file__).resolve().parent.parent
DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "drawer"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
# Distinctive values of the register: none may show on a door that is not the register's
REGISTER_VALUES = ("9.33.1-DEMO", "Demo embedded OS 7", "DEMO-MDS2-0001", "Connected (wireless)", "Security updates", "MDS2",
                   "Release not recorded", "as disclosed", "software release")


def never(text: str) -> list:
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def custom_role(name, levels, scope=""):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return role


def custom_user(tenant, name, levels, scope="", **extra):
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                    role=custom_role(name, levels, scope), **extra)


@pytest.fixture
def world(ctx, dept, pump_model, pump, today):
    """CE-10002 on 9.33.1-DEMO (embedded OS past its vendor's support), connected wireless; the model's profile with every group set."""
    profile = release(pump_model, connects=Connects.WIRELESS, patient_data=PatientData.STORES, mds2=Mds2Status.ON_FILE,
                      mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001", updates_by=UpdatesBy.CE, segmented=Segmented.SOME,
                      remote_service=Disclosed.YES, patching=Patching.FACILITY, sbom=Sbom.AVAILABLE)
    rel = release(pump_model, "9.33.1-DEMO", os_family=OsFamily.EMBEDDED, os_name="Demo embedded OS 7", os_support_ends_on=today - 5 * DAY,
                  security_updates_end_on=today - DAY)
    services.record_facts(pump, by=None, release=rel, release_since=today - 10 * DAY, network=Network.WIRELESS)
    return {"profile": profile, "release": rel, "pump": pump, "model": pump_model}


@pytest.fixture
def theirs(other_tenant):
    """Another facility's model, release, and device on it."""
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        rel = release(model, "9.1")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        services.record_facts(asset, by=None, release=rel)
    return {"model": model, "release": rel, "asset": asset, "tenant": other_tenant}


# --- nothing reaches the existing doors -------------------------------------------------------------------------------------------

ASSET_FIELDS_36 = {"acquisition_cost", "added_as", "added_as_label", "arrived_on", "awaiting_inspection", "condition", "contract",
                   "contract_reference", "department", "department_name", "device_model", "device_model_detail", "due_back_on", "id",
                   "incident_hold", "incoming_inspection", "inspection_due", "installed_on", "kept_on", "last_pm_on", "next_pm_on", "notes",
                   "open_inspection", "open_inspection_number", "owner", "owner_pm_due_on", "owner_reference", "ownership", "ownership_label",
                   "returned_on", "room", "serial", "stands_in_for", "stands_in_for_tag", "status", "status_label", "support_type", "tag",
                   "under_contract", "updated_at", "warranty_end"}
MODEL_FIELDS_36 = {"aem_interval_months", "category", "description", "expected_life_years", "id", "life_source", "list_cost", "manufacturer",
                   "model", "oem_pm_interval_months", "oem_schedule_required", "parts_end_on", "pm_interval_months", "pm_procedure", "risk_class",
                   "sale_ended_on", "support_checked_on", "support_ends_on", "support_notice", "support_status"}


def test_the_asset_and_model_serializers_keep_their_slice_36_fields():
    asset = AssetSerializer()
    assert set(asset.fields) == ASSET_FIELDS_36
    assert set(DeviceModelSerializer().fields) == MODEL_FIELDS_36
    assert set(asset.fields["device_model_detail"].fields) == MODEL_FIELDS_36  # the nested model: never the register either


def test_the_device_api_reads_no_register_field(client, make_user, world):
    client.force_login(make_user("director"))
    data = client.get(f"/api/v1/assets/{world['pump'].pk}/").json()
    assert set(data) == ASSET_FIELDS_36 - {"incoming_inspection", "inspection_due"}  # those two are write-only
    assert not any(v in str(data) for v in REGISTER_VALUES)
    model = client.get(f"/api/v1/device-models/{world['model'].pk}/")
    assert model.status_code == 200 and not any(v in str(model.json()) for v in REGISTER_VALUES)


def test_the_equipment_columns_are_unchanged():
    from apps.web.views import EQUIPMENT_COLUMNS as TABLE
    from apps.web.views_exports import EQUIPMENT_COLUMNS as CSV

    assert [c[0] for c in TABLE] == ["tag", "device", "location", "risk", "status", "next_pm", "age", "support", "cost"]
    assert CSV == ["Tag", "Serial", "Manufacturer", "Model", "Description", "Category", "Risk class", "Department", "Room", "Status", "Support",
                   "Contract", "Contract end", "Contract expired", "Installed", "Acquisition cost", "Warranty end", "Last PM", "Next PM",
                   "Fleet state", "Open work orders", "Whose", "Owner", "Reference", "Arrived", "Due back"]


def test_custom_reports_keep_their_sources_and_the_devices_columns():
    from apps.reports.custom import SOURCES

    assert list(SOURCES) == ["work_orders", "devices", "labor", "parts"]
    devices = SOURCES["devices"]
    assert [c.key for c in devices.columns] == ["tag", "serial", "device", "manufacturer", "model", "category", "department", "room", "status",
                                                "risk_class", "ownership", "support_type", "contract", "contract_vendor", "contract_end",
                                                "installed", "age", "acquisition_cost", "condition", "warranty_end", "last_pm", "next_pm",
                                                "pm_interval", "oem_schedule", "open_work_orders", "repairs_12mo", "service_cost_12mo"]
    assert [f.key for f in devices.filters] == ["status", "risk_class", "ownership", "support_type", "oem_schedule", "department", "category"]
    for source in SOURCES.values():
        assert not [c.key for c in source.columns if re.search(r"cyber|secur|release|mds2|network", c.key)]


def test_the_equipment_screen_and_its_csv_read_no_register_field(client, make_user, world):
    client.force_login(make_user("director"))
    for url in ("/equipment/", "/export/equipment.csv"):
        r = client.get(url)
        body = b"".join(r.streaming_content).decode() if r.streaming else r.content.decode()
        assert r.status_code == 200 and "CE-10002" in body, url
        assert not [v for v in REGISTER_VALUES if v in body], url


# --- never a scoped user's ----------------------------------------------------------------------------------------------------------

def _web_urls(world) -> list[str]:
    """Every Device security URL (urls_cybersecurity, whatever routes it holds), with a real model, release, and device of this facility
    in its parameters: a UUID is a release's under releases/, else a model's; text is the device's tag; a number 30."""
    out = []
    for p in urls_cybersecurity.urlpatterns:
        route = str(p.pattern)
        pk = str(world["release"].pk if route.startswith("releases/") else world["model"].pk)
        values = {"uuid": pk, "str": world["pump"].tag, "slug": world["pump"].tag.lower(), "int": "30", "path": world["pump"].tag}
        out.append("/security/" + re.sub(r"<(?:(\w+):)?\w+>", lambda m: values.get(m.group(1) or "str", world["pump"].tag), route))
    return out


def _api_urls(world) -> list[str]:
    """The register's API routes, list and one row each: software releases, device security, and (slice 39) security decisions (a
    number: a scoped user is refused before any lookup)."""
    bases = {api_cyber.SoftwareReleaseViewSet: ("software-releases", world["release"].pk),
             api_cyber.DeviceSecurityViewSet: ("device-security", world["pump"].tag),
             api_cyber.SecurityDecisionViewSet: ("security-decisions", "SD-26-0001")}
    out = set()
    for p in api_urls.urlpatterns:
        cls = getattr(p.callback, "cls", None)
        if cls not in bases:
            continue
        base, key = bases[cls]
        out.add(f"/api/v1/{base}/{key}/" if "pk" in str(p.pattern) else f"/api/v1/{base}/")
    return sorted(out)


@pytest.mark.parametrize("scope, attrs", [(DataScope.COMPANY, {"company": "BD"}), (DataScope.DEPARTMENT, {"department": "ICU"})])
def test_every_door_refuses_a_scoped_user_with_device_security_full(client, tenant, world, scope, attrs):
    user = custom_user(tenant, f"scoped{scope}", {m: Level.FULL for m in Module.values}, scope=scope, **attrs)
    client.force_login(user)
    web = _web_urls(world)
    assert len(web) == len(urls_cybersecurity.urlpatterns) >= 12
    before = (SoftwareRelease.objects.count(), list(DeviceSecurity.objects.values_list("release_id", "network", "checked_on")))
    for url in web:
        for headers in ({}, HX, MODAL):
            assert client.get(url, **headers).status_code == 403, url
        assert client.post(url, {"version": "9.99", "release": str(world["release"].pk), "network": "not_connected"}, **MODAL).status_code == 403
    apis = _api_urls(world)
    assert len(apis) == 6  # three viewsets, a list and one row each (slice 39: security-decisions)
    for url in apis:
        for method in ("get", "post", "put", "patch", "delete"):
            r = getattr(client, method)(url, {}, content_type="application/json") if method != "get" else client.get(url)
            assert r.status_code == 403, (method, url)
    assert (SoftwareRelease.objects.count(), list(DeviceSecurity.objects.values_list("release_id", "network", "checked_on"))) == before
    r = client.get(f"/equipment/{world['pump'].tag}/?tab=security", **HX)  # in their share (the ICU's): the drawer, never the tab
    if scope == DataScope.DEPARTMENT:
        assert r.status_code == 200 and "security" not in r.context["tabs"] and r.context["tab"] == "overview"
        assert not [v for v in REGISTER_VALUES if v in r.content.decode()]
    else:
        assert r.status_code == 404  # no work of BD's on it: not in the company's share


@pytest.mark.parametrize("slug", ["vendor", "requester"])
def test_the_default_scoped_roles_are_refused_too(client, make_user, world, slug):
    user = make_user(slug)
    user.company, user.department = "BD", "ICU"
    user.save()
    client.force_login(user)
    for url in [*_web_urls(world), *_api_urls(world)]:
        assert client.get(url).status_code == 403, url


# --- the history areas ------------------------------------------------------------------------------------------------------------

CYBER_AREAS = {"software_releases", "device_security", "device_security_decisions"}  # slice 39: decisions, as a device's facts


def test_the_history_areas_need_the_module_and_equipment_for_a_device(tenant, make_user, world):
    def areas(user):
        return {a.key for a in history.readable_areas(user)} & CYBER_AREAS

    assert areas(make_user("technician")) == CYBER_AREAS
    assert areas(make_user("analyst")) == set()  # Device security None
    assert areas(custom_user(tenant, "models", {Module.CYBERSECURITY: Level.VIEW})) == {"software_releases"}
    assert areas(custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})) == CYBER_AREAS
    full = {m: Level.FULL for m in Module.values}
    for scope in (DataScope.COMPANY, DataScope.DEPARTMENT):
        scoped = custom_user(tenant, f"scoped{scope}", full, scope=scope, company="BD", department="ICU")
        assert history.readable_areas(scoped) == [] and not history.can_read(scoped, "device_security")
        assert not history.can_read(scoped, "software_releases") and not history.can_read(scoped, "device_security_decisions")


def test_the_change_log_shows_each_area_to_its_readers_only(tenant, make_user, world):
    def logged(user) -> set:
        entries, _ = history.change_log(user, limit=200)
        return {e.area for e in entries} & CYBER_AREAS

    # slice 39: a decision for CE-10002 (past its security updates and its OS's support, connected wireless)
    decisions.record_decision(asset=world["pump"], reasons=["security_updates", "os_support"],
                              response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, accepted_by=AcceptedBy.CE_LEADER, by=None)
    assert logged(make_user("director")) == CYBER_AREAS
    assert logged(custom_user(tenant, "models", {Module.CYBERSECURITY: Level.VIEW, Module.USERS: Level.VIEW})) == {"software_releases"}
    assert logged(custom_user(tenant, "noview", {Module.EQUIPMENT: Level.FULL, Module.USERS: Level.FULL})) == set()
    scoped = custom_user(tenant, "scopedfull", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="BD")
    assert history.change_log(scoped, limit=200)[0] == []


def test_the_change_log_screen_never_lists_them_for_a_scoped_user(client, tenant, world):
    scoped = custom_user(tenant, "scopedfull", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="BD")
    client.force_login(scoped)
    for url in ("/users/log/", "/users/log/export.csv", "/users/log/print/"):
        assert client.get(url).status_code == 403, url
    assert client.get("/api/v1/change-log/").status_code == 403


# --- no email path, the portal, labels, Scan ---------------------------------------------------------------------------------------

def _sources(*paths) -> list[Path]:
    out = []
    for p in paths:
        path = ROOT / p
        out += [path] if path.is_file() else [f for f in path.rglob("*") if f.suffix in (".py", ".html", ".txt") and f.is_file()]
    return out


def test_no_email_path_imports_the_register():
    files = _sources("apps/notifications", "apps/reports/subscriptions.py", "apps/portal")
    assert len(files) > 10 and any(f.name == "daily.py" for f in files) and any(f.name == "subscriptions.py" for f in files)
    found = [str(f.relative_to(ROOT)) for f in files if re.search(r"cybersecurity|SoftwareRelease|DeviceSecurity", f.read_text())]
    assert found == []


def test_the_portal_labels_and_scan_render_no_register_field(client, make_user, world, tenant):
    tag = world["pump"].tag
    r = client.get(f"/r/{tenant.slug}/?asset={tag}")  # the public request form, signed out
    assert r.status_code == 200 and tag in r.content.decode()
    pages = [r]
    client.force_login(make_user("director"))
    pages.append(client.get(f"/r/{tenant.slug}/?asset={tag}"))  # signed in: "Open CE-10002 in Cadence", nothing else
    pages.append(client.get(f"/print/labels/?tag={tag}"))
    pages.append(client.get("/print/labels/"))
    scan = client.get(f"/scan/?code={tag}", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="modal-card")
    assert scan.status_code == 200 and scan.context["tab"] == "overview"
    pages.append(scan)
    assert client.get(f"/scan/?code={tag}").status_code == 302
    for page in pages:
        body = page.content.decode()
        assert page.status_code == 200 and tag in body
        assert not [v for v in REGISTER_VALUES if v in body], page.request["PATH_INFO"]


# --- the never-words on every door of this wave -----------------------------------------------------------------------------------

@pytest.fixture
def every_state(world, ctx, dept, vent_model, pump_model, today):
    """Devices whose words cover every key the rules can produce: updates ended, announced, the model's support ended, not recorded;
    the OS ended, announced, none, not recorded; connected, not connected here, can connect, does not connect, not recorded; each MDS2
    status; each disclosure answer; a retired one, a returned rental."""
    vent_model.support_ends_on = today - 30 * DAY
    vent_model.save()
    vent_profile = release(vent_model, connects=Connects.NONE, patient_data=PatientData.NONE, mds2=Mds2Status.NONE_FROM_MANUFACTURER,
                           remote_service=Disclosed.NO, patching=Patching.NONE, sbom=Sbom.NOT_AVAILABLE, updates_by=UpdatesBy.MANUFACTURER,
                           segmented=Segmented.NONE)
    vent_rel = release(vent_model, "3.1", os_family=OsFamily.NONE)
    other = release(pump_model, "9.40", os_family=OsFamily.WINDOWS, os_name="Windows 10 IoT Enterprise LTSC 2021",
                    os_support_ends_on=today + 900 * DAY, security_updates_end_on=today + 400 * DAY, mds2=Mds2Status.REQUESTED,
                    mds2_requested_on=today - 3 * DAY, patient_data=PatientData.HANDLES, connects=Connects.BOTH, patching=Patching.NONE,
                    sbom=Sbom.ON_REQUEST)
    devices = {"ended": world["pump"]}
    for tag, model, rel, network in (("S-ANN", pump_model, other, Network.INTERMITTENT), ("S-VENT", vent_model, vent_rel, ""),
                                     ("S-NONE", pump_model, None, Network.NOT_CONNECTED), ("S-BARE", pump_model, None, ""),
                                     ("S-BOTH", pump_model, other, Network.BOTH), ("S-RET", pump_model, other, Network.WIRED)):
        a = Asset.objects.create(tag=tag, device_model=model, department=dept)
        if rel is not None or network:
            services.record_facts(a, by=None, release=rel, network=network)
        devices[tag] = a
    eq.set_status(devices["S-RET"], AssetStatus.RETIRED)
    bare_model = DeviceModel.objects.create(manufacturer="Steris", model="Amsco 400", description="Steam sterilizer", category="Sterilizers")
    devices["S-NOTHING"] = Asset.objects.create(tag="S-NOTHING", device_model=bare_model, department=dept)
    rental = eq.add_temporary_device(device_model=pump_model, department=dept, tag="S-RENT", kind=Ownership.RENTAL, owner="Rent Co",
                                     serial="RENT-1", arrived_on=today - 9 * DAY, added_as="existing", incoming_inspection="")
    services.record_facts(rental, by=None, release=other)
    devices["S-RENT"] = rental
    return {"devices": devices, "vent_profile": vent_profile}


def test_the_never_words_on_the_drawer_tab_and_record_facts(client, make_user, every_state, world, today):
    client.force_login(make_user("director"))
    seen = set()
    for tag, a in every_state["devices"].items():
        r = client.get(f"/equipment/{a.tag}/?tab=security", **HX)
        body = r.content.decode()
        assert r.status_code == 200 and r.context["tab"] == "security", tag
        assert never(body) == [], (tag, never(body))
        s = r.context["cyber"]["standing"]
        seen |= {s.security.key, s.os.key, s.connection.key}
        if a.status != AssetStatus.RETIRED:
            modal = client.get(f"/security/devices/{a.tag}/facts/", **MODAL).content.decode()
            assert never(modal) == [], tag
    assert seen >= {rules.UPDATES_ENDED, rules.UPDATES_END_ANNOUNCED, rules.MODEL_SUPPORT_ENDED, rules.SECURITY_NOT_RECORDED, rules.OS_ENDED,
                    rules.OS_END_ANNOUNCED, rules.NO_OS, rules.OS_NOT_RECORDED, rules.CONNECTED, rules.NOT_CONNECTED, rules.CAN_CONNECT,
                    rules.CONNECTION_NOT_RECORDED}
    later = (today + DAY).isoformat()
    refused = client.post(f"/security/devices/{world['pump'].tag}/facts/?tab=security",
                          {"release": "x", "release_since": later, "network": "x", "checked_on": "1800-01-01"}, **MODAL)
    assert refused.status_code == 200 and refused.context["form"].errors and never(refused.content.decode()) == []
    saved = client.post(f"/security/devices/{world['pump'].tag}/facts/?tab=security", {"release": "", "network": "not_connected"}, **MODAL)
    assert saved.status_code == 200 and never(saved.content.decode() + saved.get("HX-Trigger", "")) == []
    retired = client.post("/security/devices/S-RET/facts/?tab=security", {"release": ""}, **MODAL)
    assert "S-RET is retired" in retired.content.decode() and never(retired.content.decode()) == []


def test_the_never_words_on_the_api_bodies(client, make_user, every_state):
    client.force_login(make_user("director"))
    bodies = [client.get(url).content.decode() for url in ("/api/v1/software-releases/", "/api/v1/software-releases/?removed=1",
                                                            "/api/v1/device-security/", "/api/v1/device-security/S-ANN/")]
    assert all(never(b) == [] for b in bodies), [never(b) for b in bodies]
    for url in ("/api/v1/device-security/", "/api/v1/software-releases/"):
        for method in ("post", "delete"):
            assert never(getattr(client, method)(url, {}, content_type="application/json").content.decode()) == []
    client.force_login(make_user("analyst"))
    assert never(client.get("/api/v1/device-security/").content.decode()) == []
    assert never(api_cyber.READ_ONLY + api_cyber.WRITE_REFUSAL + api_cyber.RELEASES_REFUSAL + api_cyber.DEVICES_REFUSAL) == []


# --- ids from another facility, every writer door ---------------------------------------------------------------------------------

def _state(tenant_=None):
    def read():
        return (sorted(SoftwareRelease.objects.values_list("pk", "version", "removed_on", "os_name")),
                sorted(DeviceSecurity.objects.values_list("asset_id", "release_id", "network", "checked_on")))

    if tenant_ is None:
        return read()
    with tenant_context(tenant_):
        return read()


def test_every_writer_door_refuses_another_facility_s_ids(client, make_user, world, theirs):
    client.force_login(make_user("director"))
    mine, others = _state(), _state(theirs["tenant"])
    their_model, their_release, their_asset = theirs["model"], theirs["release"], theirs["asset"]
    fields = {"version": "9.99", "os_name": "Other OS", "release": str(their_release.pk), "device_model": str(their_model.pk),
              "network": "not_connected", "asset": str(their_asset.pk), "assets": str(their_asset.pk), "department": "",
              "release_since": "", "checked_on": ""}
    doors = [
        f"/security/devices/{their_asset.tag}/facts/",  # Record facts: their device
        f"/security/models/{their_model.pk}/releases/new/",  # add a release to their model
        f"/security/releases/{their_release.pk}/edit/",  # edit their release
        f"/security/releases/{their_release.pk}/remove/",  # mark their release entered in error
        f"/security/models/{their_model.pk}/set-release/",  # set their model's devices
        f"/security/models/{world['model'].pk}/set-release/",  # set our devices to their release
        f"/security/devices/{world['pump'].tag}/facts/?tab=security",  # Record facts: our device, their release
    ]
    for url in doors:
        r = client.post(url, fields, **MODAL)
        assert r.status_code in (200, 403, 404) and "HX-Retarget" not in r, (url, r.status_code)
    r = client.post(f"/security/devices/{world['pump'].tag}/facts/?tab=security", fields, **MODAL)
    assert r.context["form"].errors == {"release": [services.CHOOSE_VERSION]}
    assert client.post(f"/security/devices/{their_asset.tag}/facts/", fields, **MODAL).status_code == 404
    assert (_state(), _state(theirs["tenant"])) == (mine, others)
