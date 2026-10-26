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
- Slice 40, security advisories (build40 section 11): the walkers cover every module mounted under /security/ (the screen, the Decisions
  tab, the Advisories tab) and the API's security-advisories (by id and by number) and an advisory's decisions; rules.DECISION_NEVER_WORDS
  over every Advisories door (each route of urls_cyber_advisories on an advisory in each state, opened at each level that sees it, its
  refusals showing), every save's answer and toast (a refusal repeating the CVE text a person typed allowed for), Record decision for an
  advisory's scopes and the advisory decisions' pages, slice 38's update doors opened from an advisory, the API's bodies, and the module
  constants and choices; the Security tab's Advisories section with the drawer whole under rules.NEVER_WORDS; the history areas; nothing
  of an advisory on the device's doors, the portal, labels, or Scan; and another facility's advisory, sources, model rows, models, and
  releases never reached by an advisory door.
"""
import json
import re
import uuid
from datetime import timedelta
from html import unescape
from pathlib import Path

import pytest
from cyber_fixtures import release
from django.urls import URLResolver
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.api import urls as api_urls
from apps.api import views_cybersecurity as api_cyber
from apps.api.serializers import AssetSerializer, DeviceModelSerializer
from apps.core import history
from apps.cybersecurity import advisories, decisions, rules, services, updates
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryChange,
    AdvisoryModel,
    AdvisoryRelease,
    AdvisoryRemedy,
    AdvisorySource,
    AdvisorySourceKind,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    Connects,
    DecisionResponse,
    DeviceSecurity,
    DeviceSecurityDecision,
    Disclosed,
    Mds2Edition,
    Mds2Status,
    Network,
    NotApplicableReason,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    SecurityAdvisory,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
    UpdateTarget,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.web import urls as web_urls
from apps.web import urls_cyber_advisories, urls_cybersecurity
from apps.workorders.models import Priority

ROOT = Path(__file__).resolve().parent.parent
DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "drawer"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
# Distinctive values of the register: none may show on a door that is not the register's
REGISTER_VALUES = ("9.33.1-DEMO", "Demo embedded OS 7", "DEMO-MDS2-0001", "Connected (wireless)", "Security updates", "MDS2",
                   "Release not recorded", "as disclosed", "software release")


def never(text: str, words=rules.NEVER_WORDS) -> list:
    return [w for w in words if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def decision_never(text: str) -> list:
    """rules.DECISION_NEVER_WORDS (NEVER_WORDS and "overdue"): every advisory and decision door, its CSVs, toasts, and API bodies."""
    return never(unescape(text), rules.DECISION_NEVER_WORDS)


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
def advisory_world(world, ctx, dept, pump_model, vent_model, make_user, today):
    """Slice 40, world's facility with security advisories, through the services as the CE manager records them. The pumps' releases
    9.40-DEMO and 9.50-DEMO beside world's 9.33.1-DEMO, and a pump on each line kind: CE-10002 and A-UPD (an update open to the fix) on
    the release the first advisory names, A-OFF on it recorded Not connected here, A-FIX on the fix, A-OTHER on a release it does not
    name, A-NONE with no release recorded, the rental A-RENT on the named release; the ventilator A-VENT on 3.2-DEMO. One advisory per
    state, in the order received:
    - the first (30 days ago; four CVE IDs; a bulletin with its link and a revision, and an ISAC alert): the pumps on 9.33.1-DEMO, the
      fix 9.50-DEMO, its models confirmed, then 9.60-DEMO added ("Release added since"); decided for A-OFF (device scope, taken off the
      network, past its review), for 9.33.1-DEMO (in force), and for the pumps (not in use here: A-NONE); Close refused (the rental).
    - the second: no model named, marked not applicable (it names no model in this facility's catalog).
    - the third: the ventilators on 3.1-DEMO (none on it here), mitigations only, closed.
    - the fourth: entered in error.
    - the fifth (today, sharing a CVE ID with the first, shown): the pumps without stating releases, no fix yet, models to confirm."""
    manager = make_user("manager")  # Device security Approve, Equipment Edit, Work orders Approve
    r9331 = world["release"]
    r940, r950 = (services.add_release(pump_model, version=v, profile=False, by=manager, today=today) for v in ("9.40-DEMO", "9.50-DEMO"))
    v31, v32 = (services.add_release(vent_model, version=v, profile=False, by=manager, today=today) for v in ("3.1-DEMO", "3.2-DEMO"))
    devices = {"CE-10002": world["pump"]}
    for tag, model, rel, network in (("A-UPD", pump_model, r9331, ""), ("A-OFF", pump_model, r9331, Network.NOT_CONNECTED),
                                     ("A-FIX", pump_model, r950, ""), ("A-OTHER", pump_model, r940, ""), ("A-NONE", pump_model, None, ""),
                                     ("A-VENT", vent_model, v32, "")):
        devices[tag] = Asset.objects.create(tag=tag, device_model=model, department=dept, serial=f"SN-{tag}")
        if rel is not None or network:
            services.record_facts(devices[tag], by=manager, release=rel, network=network, today=today)
    devices["A-RENT"] = eq.add_temporary_device(device_model=pump_model, department=dept, tag="A-RENT", kind=Ownership.RENTAL,
                                                owner="Rent Co", serial="RENT-2", arrived_on=today - 9 * DAY, added_as="existing",
                                                incoming_inspection="")
    services.record_facts(devices["A-RENT"], by=manager, release=r9331, today=today)
    updates.open_update(devices["A-UPD"], target=UpdateTarget.RELEASE, target_release=r950, due_on=today + 30 * DAY,
                        priority=Priority.NORMAL, by=manager, today=today)

    first = advisories.record_advisory(received_on=today - 30 * DAY, cves="CVE-2026-1001, CVE-2026-1002\nCVE-2026-1003 CVE-2026-1004",
                                       source=AdvisorySourceKind.MANUFACTURER_BULLETIN, reference="DEMO-ADV-0001",
                                       url="https://www.example.com/advisories/demo-adv-0001", published_on=today - 32 * DAY, by=manager,
                                       today=today)
    advisories.add_source(first, source=AdvisorySourceKind.ISAC, reference="DEMO-ISAC-0001", by=manager, today=today)
    advisories.record_revision(first.sources.order_by("created_at", "pk").first(), revision="2", updated_on=today - 10 * DAY,
                               last_change=[AdvisoryChange.MITIGATIONS_CHANGED, AdvisoryChange.FIX_AVAILABLE], by=manager, today=today)
    advisories.set_model(first, device_model=pump_model, versions=AdvisoryVersions.LISTED, release_ids=[r9331.pk],
                         remedy=AdvisoryRemedy.RELEASE, fix_release=r950.pk, confirm=True, by=manager, today=today)
    services.add_release(pump_model, version="9.60-DEMO", profile=False, by=manager, today=today)
    keyed = {"applies_to": AppliesTo.ADVISORY, "advisory": first, "reasons": [], "accepted_by": AcceptedBy.CE_LEADER, "by": manager}
    off = decisions.record_decision(asset=devices["A-OFF"], response=DecisionResponse.KEEP_WITH_CONTROLS, controls=["network_removed"],
                                    decided_on=today - 20 * DAY, review_by=today - DAY, today=today - 20 * DAY, **keyed)
    on_release = decisions.record_decision(release=r9331, response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                           exception_reference="DEMO-EX-0040", today=today, **keyed)
    on_model = decisions.record_decision(device_model=pump_model, response=DecisionResponse.NOT_IN_USE_HERE, today=today, **keyed)

    second = advisories.record_advisory(received_on=today - 7 * DAY, cves="", source=AdvisorySourceKind.OTHER, reference="DEMO-ADV-0002",
                                        by=manager, today=today)
    advisories.mark_not_applicable(second, reason=NotApplicableReason.NO_MODEL_HERE, by=manager, today=today)
    third = advisories.record_advisory(received_on=today - 5 * DAY, cves="CVE-2026-2001", source=AdvisorySourceKind.MANUFACTURER_PORTAL,
                                       reference="DEMO-ADV-0003", by=manager, today=today)
    advisories.set_model(third, device_model=vent_model, versions=AdvisoryVersions.LISTED, release_ids=[v31.pk],
                         remedy=AdvisoryRemedy.MITIGATIONS, confirm=True, by=manager, today=today)
    advisories.close(third, by=manager, today=today)
    fourth = advisories.record_advisory(received_on=today - 2 * DAY, cves="CVE-2026-4001", source=AdvisorySourceKind.FDA_COMMUNICATION,
                                        reference="DEMO-ADV-0004", by=manager, today=today)
    advisories.mark_entered_in_error(fourth, by=manager, today=today)
    fifth = advisories.record_advisory(received_on=today, cves="CVE-2026-1001 CVE-2026-5001", source=AdvisorySourceKind.MANUFACTURER_BULLETIN,
                                       reference="DEMO-ADV-0005", seen=(first.number,), by=manager, today=today)
    advisories.set_model(fifth, device_model=pump_model, versions=AdvisoryVersions.NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET,
                         confirm=False, by=manager, today=today)
    found = [SecurityAdvisory.objects.get(pk=a.pk) for a in (first, second, third, fourth, fifth)]
    assert [a.status for a in found] == ["open", "not_applicable", "closed", "open", "open"] and found[3].removed_on == today
    assert found[0].models_confirmed_on == today and found[4].models_confirmed_on is None
    return {**world, "manager": manager, "vent_model": vent_model, "releases": {"9.33.1": r9331, "9.40": r940, "9.50": r950, "3.1": v31},
            "devices": devices, "advisories": found, "decisions": [off, on_release, on_model]}


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

def _security_modules() -> list:
    """(prefix, routes) of every module apps/web/urls.py mounts under /security/: the Device security screen (urls_cybersecurity, slice
    37), the Decisions tab (urls_cyber_decisions, slice 39), the Advisories tab (urls_cyber_advisories, slice 40), and any mounted there
    next."""
    return [("/" + str(p.pattern), p.url_patterns) for p in web_urls.urlpatterns
            if isinstance(p, URLResolver) and str(p.pattern).startswith("security/")]


def _web_urls(aw) -> list[str]:
    """Every Device security URL (each module mounted under /security/, whatever routes it holds), with real rows of this facility in its
    parameters: a number is the first advisory decision's under decisions/, else the first advisory's; a source or model row id the
    first advisory's; another UUID is a release's under releases/, else a model's; other text is the device's tag; a number 30."""
    first = aw["advisories"][0]
    rows = {"source_id": first.sources.order_by("created_at", "pk").first().pk, "row_id": first.model_rows.get().pk}
    out = []
    for prefix, routes in _security_modules():
        for p in routes:
            route = str(p.pattern)

            def value(m, prefix=prefix, route=route) -> str:
                conv, name = m.group(1) or "str", m.group(2)
                if name == "number":
                    return aw["decisions"][0].number if prefix.endswith("/decisions/") else first.number
                if name in rows:
                    return str(rows[name])
                if conv == "uuid":
                    return str(aw["release"].pk if route.startswith("releases/") else aw["model"].pk)
                return "30" if conv == "int" else aw["pump"].tag

            out.append(prefix + re.sub(r"<(?:(\w+):)?(\w+)>", value, route))
    return out


def _api_urls(aw) -> list[str]:
    """The register's API routes, list and one row each: software releases, device security, (slice 39) security decisions (a number: a
    scoped user is refused before any lookup), and (slice 40) security advisories by id and by number, and the decisions' list for an
    advisory (?applies_to=advisory, ?advisory=<number>)."""
    first = aw["advisories"][0]
    bases = {api_cyber.SoftwareReleaseViewSet: ("software-releases", [aw["release"].pk]),
             api_cyber.DeviceSecurityViewSet: ("device-security", [aw["pump"].tag]),
             api_cyber.SecurityDecisionViewSet: ("security-decisions", [aw["decisions"][0].number]),
             api_cyber.SecurityAdvisoryViewSet: ("security-advisories", [first.pk, first.number])}
    filters = {api_cyber.SecurityDecisionViewSet: ["?applies_to=advisory", f"?advisory={first.number}"]}
    out = set()
    for p in api_urls.urlpatterns:
        cls = getattr(p.callback, "cls", None)
        if cls not in bases:
            continue
        base, keys = bases[cls]
        if "pk" in str(p.pattern):
            out |= {f"/api/v1/{base}/{key}/" for key in keys}
        else:
            out |= {f"/api/v1/{base}/{query}" for query in ("", *filters.get(cls, ()))}
    return sorted(out)


@pytest.mark.parametrize("scope, attrs", [(DataScope.COMPANY, {"company": "BD"}), (DataScope.DEPARTMENT, {"department": "ICU"})])
def test_every_door_refuses_a_scoped_user_with_device_security_full(client, tenant, advisory_world, scope, attrs):
    world = advisory_world
    user = custom_user(tenant, f"scoped{scope}", {m: Level.FULL for m in Module.values}, scope=scope, **attrs)
    client.force_login(user)
    web = _web_urls(world)
    modules = _security_modules()
    assert [prefix for prefix, _routes in modules] == ["/security/decisions/", "/security/advisories/", "/security/"]
    assert len(web) == sum(len(routes) for _prefix, routes in modules) >= len(urls_cybersecurity.urlpatterns) + 8 + 17
    assert len(urls_cyber_advisories.urlpatterns) == 17

    def state():
        return (SoftwareRelease.objects.count(), list(DeviceSecurity.objects.values_list("release_id", "network", "checked_on")),
                sorted(SecurityAdvisory.objects.values_list("number", "status", "removed_on", "models_confirmed_on", "changes")),
                AdvisorySource.objects.count(), AdvisoryModel.objects.count(), AdvisoryRelease.objects.count(),
                sorted(DeviceSecurityDecision.objects.values_list("number", "ended_on")))

    before = state()
    for url in web:
        for headers in ({}, HX, MODAL):
            assert client.get(url, **headers).status_code == 403, url
        assert client.post(url, {"version": "9.99", "release": str(world["release"].pk), "network": "not_connected", "received_on": "",
                                 "cves": "CVE-2026-9999", "source": "other", "reference": "DEMO-ADV-9999"}, **MODAL).status_code == 403
    apis = _api_urls(world)
    # Four viewsets, a list and one row each, the advisories' row by id and by number, the decisions' list for an advisory both ways:
    # software-releases 2, device-security 2, security-decisions 4 (slice 39; slice 40's two filters), security-advisories 3 (slice 40)
    assert len(apis) == 11
    for url in apis:
        for method in ("get", "post", "put", "patch", "delete"):
            r = getattr(client, method)(url, {}, content_type="application/json") if method != "get" else client.get(url)
            assert r.status_code == 403, (method, url)
    assert state() == before
    r = client.get(f"/equipment/{world['pump'].tag}/?tab=security", **HX)  # in their share (the ICU's): the drawer, never the tab
    if scope == DataScope.DEPARTMENT:
        assert r.status_code == 200 and "security" not in r.context["tabs"] and r.context["tab"] == "overview"
        body = r.content.decode()
        assert not [v for v in REGISTER_VALUES if v in body]
        assert not [a.number for a in world["advisories"] if a.number in body] and "DEMO-ADV" not in body and "CVE-2026" not in body
    else:
        assert r.status_code == 404  # no work of BD's on it: not in the company's share


@pytest.mark.parametrize("slug", ["vendor", "requester"])
def test_the_default_scoped_roles_are_refused_too(client, make_user, advisory_world, slug):
    user = make_user(slug)
    user.company, user.department = "BD", "ICU"
    user.save()
    client.force_login(user)
    for url in [*_web_urls(advisory_world), *_api_urls(advisory_world)]:
        assert client.get(url).status_code == 403, url


# --- the history areas ------------------------------------------------------------------------------------------------------------

CYBER_AREAS = {"software_releases", "device_security", "device_security_decisions"}  # slice 39: decisions, as a device's facts
# Slice 40: an advisory, its sources, the models and releases it names (Device security View: they name models, never devices)
ADVISORY_AREAS = {"security_advisories", "advisory_sources", "advisory_models", "advisory_releases"}
MODEL_AREAS = {"software_releases", *ADVISORY_AREAS}  # what Device security View reads without Equipment View


def test_the_history_areas_need_the_module_and_equipment_for_a_device(tenant, make_user, world):
    def areas(user):
        return {a.key for a in history.readable_areas(user)} & (CYBER_AREAS | ADVISORY_AREAS)

    assert areas(make_user("technician")) == CYBER_AREAS | ADVISORY_AREAS
    assert areas(make_user("analyst")) == set()  # Device security None
    assert areas(custom_user(tenant, "models", {Module.CYBERSECURITY: Level.VIEW})) == MODEL_AREAS
    assert areas(custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})) == CYBER_AREAS | ADVISORY_AREAS
    assert areas(custom_user(tenant, "devonly", {Module.EQUIPMENT: Level.FULL})) == set()
    full = {m: Level.FULL for m in Module.values}
    for scope in (DataScope.COMPANY, DataScope.DEPARTMENT):
        scoped = custom_user(tenant, f"scoped{scope}", full, scope=scope, company="BD", department="ICU")
        assert history.readable_areas(scoped) == [] and not history.can_read(scoped, "device_security")
        assert not history.can_read(scoped, "software_releases") and not history.can_read(scoped, "device_security_decisions")
        assert not [area for area in ADVISORY_AREAS if history.can_read(scoped, area)]


def test_the_change_log_shows_each_area_to_its_readers_only(tenant, make_user, world, today):
    def logged(user) -> set:
        entries, _ = history.change_log(user, limit=200)
        return {e.area for e in entries} & (CYBER_AREAS | ADVISORY_AREAS)

    # slice 39: a decision for CE-10002 (past its security updates and its OS's support, connected wireless)
    decisions.record_decision(asset=world["pump"], reasons=["security_updates", "os_support"],
                              response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, accepted_by=AcceptedBy.CE_LEADER, by=None)
    # slice 40: an advisory naming the pumps on CE-10002's release, with its source
    advisory = advisories.record_advisory(received_on=today, cves="CVE-2026-1001", source=AdvisorySourceKind.MANUFACTURER_BULLETIN,
                                          reference="DEMO-ADV-0001", by=None, today=today)
    advisories.set_model(advisory, device_model=world["model"], versions=AdvisoryVersions.LISTED, release_ids=[world["release"].pk],
                         remedy=AdvisoryRemedy.MITIGATIONS, confirm=True, by=None, today=today)
    assert logged(make_user("director")) == CYBER_AREAS | ADVISORY_AREAS
    assert logged(custom_user(tenant, "models", {Module.CYBERSECURITY: Level.VIEW, Module.USERS: Level.VIEW})) == MODEL_AREAS
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
    found = [str(f.relative_to(ROOT)) for f in files
             if re.search(r"cybersecurity|SoftwareRelease|DeviceSecurity|SecurityAdvisor|Advisory(Source|Model|Release)", f.read_text())]
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


def _advisory_values(aw) -> list[str]:
    """Distinctive values of slice 40's advisories and their decisions: none may show on a door that is not the register's."""
    return [*(a.number for a in aw["advisories"]), *(d.number for d in aw["decisions"]), "DEMO-ADV-", "DEMO-ISAC-", "CVE-2026-",
            "DEMO-EX-0040", "9.50-DEMO", "www.example.com/advisories"]


def test_nothing_of_an_advisory_reaches_the_device_s_doors_the_portal_labels_or_scan(client, make_user, advisory_world, tenant):
    """Slice 40: never the device's own API record or the model's, the Equipment list or its CSV, the portal, labels, or Scan (build40's
    ground rules), for a device an advisory names and a decision under it governs."""
    values = _advisory_values(advisory_world)
    pages = []
    for tag in ("CE-10002", "A-OFF", "A-RENT"):
        pages.append(client.get(f"/r/{tenant.slug}/?asset={tag}"))  # the public request form, signed out
    client.force_login(make_user("director"))
    for tag in ("CE-10002", "A-OFF", "A-RENT"):
        asset = advisory_world["devices"][tag]
        pages += [client.get(f"/r/{tenant.slug}/?asset={tag}"), client.get(f"/print/labels/?tag={tag}"),
                  client.get(f"/api/v1/assets/{asset.pk}/"), client.get(f"/scan/?code={tag}", **MODAL)]
    pages += [client.get("/print/labels/"), client.get(f"/api/v1/device-models/{advisory_world['model'].pk}/"), client.get("/equipment/"),
              client.get("/export/equipment.csv"), client.get("/api/v1/assets/")]
    for page in pages:
        body = b"".join(page.streaming_content).decode() if page.streaming else page.content.decode()
        assert page.status_code == 200, page.request["PATH_INFO"]
        assert not [v for v in values if v in body], (page.request["PATH_INFO"], [v for v in values if v in body])


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


# --- slice 40: the never-words on every advisory door ------------------------------------------------------------------------------

# Who may open each route of urls_cyber_advisories (apps.cybersecurity.permissions), by its name: every route is classified here, so a
# route added later fails the walk until it is
ADVISORY_LEVELS = {
    "security_advisories": cyber_perms.can_view, "security_advisories_csv": cyber_perms.can_view, "security_advisory": cyber_perms.can_view,
    "security_advisory_devices_csv": cyber_perms.can_view_devices, "security_advisory_close": cyber_perms.can_decide,
    "security_advisory_in_error": cyber_perms.can_correct_advisories,
    **dict.fromkeys(("security_advisory_new", "security_advisory_edit", "security_advisory_source_new", "security_advisory_source_edit",
                     "security_advisory_revision", "security_advisory_source_remove", "security_advisory_models",
                     "security_advisory_model_remove", "security_advisory_confirm", "security_advisory_not_applicable",
                     "security_advisory_reopen"), cyber_perms.can_edit),
}
# More addresses of a route: the list's filters, the page's History, Set models' first and second steps
ADVISORY_EXTRA = {"security_advisories": ["?status=all", "?status=closed", "?status=not_applicable", "?to_confirm=1", "?q=CVE-2026-1001",
                                          "?q=demo-isac-0001", "?status=bogus"],
                  "security_advisories_csv": ["?status=all", "?to_confirm=1"], "security_advisory": ["?history=1"],
                  "security_advisory_models": ["?model={pump}", "?model={vent}", "?os=1"]}


def _advisory_doors(aw) -> list:
    """(route name, address, headers) for every route of urls_cyber_advisories: the tab and its CSV with their filters, and each route
    with a number on each advisory of advisory_world (one in each state; the closed one's number lower-cased: any letter case), with its
    first source and its model row (none named: a row of none of its own, answered in words). The page as the drawer and as a full page,
    the modals as htmx opens them, the CSVs as downloads."""
    out = []
    for p in urls_cyber_advisories.urlpatterns:
        route, name = str(p.pattern), p.name
        assert name in ADVISORY_LEVELS, name
        if name == "security_advisory":
            headers = [HX, {}]
        elif name in ("security_advisories", "security_advisories_csv") or name.endswith("_csv"):
            headers = [{}]
        else:
            headers = [MODAL]
        for a in aw["advisories"] if "<str:number>" in route else [None]:
            values = {}
            if a is not None:
                row = a.model_rows.first()
                values = {"number": a.number.lower() if a.status == AdvisoryStatus.CLOSED else a.number,
                          "source_id": str(a.sources.order_by("created_at", "pk").first().pk), "row_id": str(row.pk if row else uuid.uuid4())}
            path = "/security/advisories/" + re.sub(r"<(?:\w+:)?(\w+)>", lambda m, v=values: v[m.group(1)], route)
            for extra in ("", *ADVISORY_EXTRA.get(name, ())):
                extra = extra.format(pump=aw["model"].pk, vent=aw["vent_model"].pk)
                for h in headers if extra != "?history=1" else [HX]:
                    out.append((name, path + extra, h))
    return out


def _body(r) -> str:
    return unescape(b"".join(r.streaming_content).decode() if r.streaming else r.content.decode())


@pytest.fixture
def locks(monkeypatch) -> list:
    """The models select_for_update was called on from here on: a GET door takes no lock (FOR UPDATE outside a transaction is an error on
    PostgreSQL; SQLite has none to catch it)."""
    from django.db.models.query import QuerySet

    taken, real = [], QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        taken.append(self.model.__name__)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    return taken


def _readers(tenant, make_user) -> dict:
    """One reader per level that sees the Advisories tab: View; View and Equipment View (devices); Edit; Edit with Equipment View and
    Work orders Edit (the update doors); Approve (corrects, never decides: no Equipment View); Approve with Equipment View (decides); the
    director (Full)."""
    view = {Module.CYBERSECURITY: Level.VIEW}
    return {"view": custom_user(tenant, "advview", view),
            "devices": custom_user(tenant, "advdevices", {**view, Module.EQUIPMENT: Level.VIEW}),
            "edit": custom_user(tenant, "advedit", {Module.CYBERSECURITY: Level.EDIT}),
            "updates": custom_user(tenant, "advupdates", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW,
                                                          Module.WORKORDERS: Level.EDIT}),
            "approve": custom_user(tenant, "advapprove", {Module.CYBERSECURITY: Level.APPROVE}),
            "decide": custom_user(tenant, "advdecide", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.VIEW,
                                                        Module.WORKORDERS: Level.VIEW, Module.USERS: Level.VIEW}),
            "director": make_user("director")}


def test_the_never_words_on_every_advisory_door_at_each_level(client, tenant, make_user, advisory_world, locks):
    """Every route of urls_cyber_advisories on an advisory in each state, opened at each level: a 200 for a level that sees it, read with
    rules.DECISION_NEVER_WORDS; a 403 for one that does not; never a lock. The refusals the modals say before they are tried show (the
    lock-free readers): a live decision under it, Close's lines, closed, already not applicable, entered in error, the last source."""
    aw = advisory_world
    first, second, third, fourth, _fifth = aw["advisories"]
    doors = _advisory_doors(aw)
    assert len({name for name, _url, _h in doors}) == 17
    shown = {}
    readers = _readers(tenant, make_user)
    locks.clear()
    for who, user in readers.items():
        client.force_login(user)
        for name, url, headers in doors:
            r = client.get(url, **headers)
            allowed = ADVISORY_LEVELS[name](user)
            assert r.status_code == (200 if allowed else 403), (who, url, headers, r.status_code)
            if r.status_code == 200:
                body = _body(r)
                assert decision_never(body) == [], (who, url, decision_never(body))
                shown.setdefault(who, []).append(body)
    assert locks == []
    director, decide, devices, view = ("\n".join(shown[k]) for k in ("director", "decide", "devices", "view"))
    for words in (advisories.DECISION_UNDER.format(decision=aw["decisions"][0].number),  # Not applicable, Entered in error: the first's
                  advisories.CLOSE_PREFIX, advisories.ALREADY_OPEN.format(number=first.number),
                  advisories.ALREADY_NOT_APPLICABLE.format(number=second.number), advisories.LAST_SOURCE,
                  advisories.ALREADY_CLOSED.format(number=third.number), advisories.REOPEN_TO_CHANGE.format(number=third.number),
                  advisories.ENTERED_IN_ERROR.format(number=fourth.number), "Release added since: 9.60-DEMO",
                  rules.HICP_ADVISORIES):
        assert words in director, words
    assert advisories.CLOSE_PREFIX in decide and "A-RENT" in decide  # Close names the devices to a reader of them
    assert "A-RENT" in devices and "A-RENT" not in view and aw["decisions"][1].number not in view  # View alone: no device, no decision


def test_the_never_words_on_the_security_tab_s_advisories(client, tenant, make_user, advisory_world, locks):
    """The device drawer's Security tab for every device of advisory_world, as each reader of devices: the drawer whole with
    rules.NEVER_WORDS (it carries Equipment's own PM words), its decision, advisory, and update sections with DECISION_NEVER_WORDS; never
    a lock."""
    readers = _readers(tenant, make_user)
    locks.clear()
    seen = set()
    for who in ("devices", "updates", "decide", "director"):
        client.force_login(readers[who])
        for tag in advisory_world["devices"]:
            r = client.get(f"/equipment/{tag}/?tab=security", **HX)
            body = unescape(r.content.decode())
            assert r.status_code == 200 and r.context["tab"] == "security", (who, tag)
            assert never(body) == [], (who, tag, never(body))
            assert "<h3>Advisories" in body, (who, tag)
            for part in body.split('<div class="sec')[1:]:
                if re.match(r'[^>]*>\s*<h3>(Advisories|Support decision|Software updates)', part):
                    assert decision_never(part) == [], (who, tag, decision_never(part))
            seen |= {x["number"] for x in r.context["cyber"]["advisories"]["items"]}
    assert seen == {advisory_world["advisories"][0].number, advisory_world["advisories"][4].number} and locks == []


def test_the_never_words_on_an_advisory_s_decision_and_update_doors(client, tenant, make_user, advisory_world, locks):
    """Record decision for each of an advisory's scopes (a device, a release, the model; offered, prefilled, or refused in words), Review
    and renew, the advisory decisions' pages, prints, devices' CSVs, End and Entered in error, the Decisions tab and its CSV filtered to
    advisories, and slice 38's update doors opened from an advisory: rules.DECISION_NEVER_WORDS at each level that opens them; never a
    lock."""
    aw = advisory_world
    first, second, third, fourth, fifth = (a.number for a in aw["advisories"])
    rel, pump, vent = aw["releases"], aw["model"].pk, aw["vent_model"].pk
    record = [f"?advisory={first}&asset=A-RENT&from=advisory", f"?advisory={first}&asset=A-OFF", f"?advisory={first}&asset=A-OTHER",
              f"?advisory={first}&asset=A-NONE&from=device", f"?advisory={first}&release={rel['9.33.1'].pk}&from=advisory",
              f"?advisory={first}&release={rel['9.40'].pk}", f"?advisory={first}&release={rel['9.50'].pk}", f"?advisory={first}&model={pump}",
              f"?advisory={fifth}&asset=CE-10002", f"?advisory={fifth}&release={rel['9.33.1'].pk}&from=panel", f"?advisory={fifth}&model={pump}",
              f"?advisory={third}&model={vent}", f"?advisory={second}&model={pump}", f"?advisory={fourth}&model={pump}",
              f"?supersedes={aw['decisions'][0].number}&from=advisory", f"?supersedes={aw['decisions'][1].number}"]
    decide = [f"/security/decisions/new/{q}" for q in record]
    pages = []
    for d in aw["decisions"]:
        base = f"/security/decisions/{d.number}/"
        decide += [f"{base}end/", f"{base}in-error/"]
        pages += [(base, HX), (base, {}), (f"{base}print/", {}), (f"{base}devices.csv", {}), (f"{base}?history=1", HX)]
    pages += [(f"/security/decisions/{q}", {}) for q in ("?why=advisory", "?why=advisory&state=review_due&here=1", "?why=support",
                                                          "export.csv?why=advisory", "export.csv")]
    opens = [f"/security/devices/{tag}/updates/open/?advisory={first}&from=advisory" for tag in ("CE-10002", "A-NONE", "A-RENT", "A-UPD")]
    opens += [f"/security/devices/A-NONE/updates/open/?advisory={fifth}&from=advisory",
              f"/security/releases/{rel['9.50'].pk}/open-updates/?advisory={first}&from=advisory"]
    readers = _readers(tenant, make_user)
    locks.clear()
    bodies = []
    for who in ("devices", "updates", "decide", "director"):
        user = readers[who]
        client.force_login(user)
        doors = [(url, headers, cyber_perms.can_view_devices(user)) for url, headers in pages]
        doors += [(url, MODAL, cyber_perms.can_decide(user)) for url in decide]
        doors += [(url, MODAL, cyber_perms.can_open_updates(user)) for url in opens]
        for url, headers, allowed in doors:
            r = client.get(url, **headers)
            assert r.status_code == (200 if allowed else 403), (who, url, r.status_code)
            if allowed:
                body = _body(r)
                assert decision_never(body) == [], (who, url, decision_never(body))
                bodies.append(body)
    assert locks == []
    text = "\n".join(bodies)
    for words in (rules.HICP_ADVISORY_DECISIONS, rules.ADVISORY_DECISION_HINT.format(number=first), "Security advisory " + first):
        assert words in text, words


def test_the_never_words_on_the_advisory_api(client, tenant, make_user, advisory_world):
    """The API's security advisories (the list with each filter, each by id and by number, with device lines for a reader of them) and the
    decisions' list for advisories, as View alone and as the director, its refusals and 400s too: rules.DECISION_NEVER_WORDS."""
    aw = advisory_world
    urls = ["/api/v1/security-advisories/", "/api/v1/security-advisories/?removed=1", "/api/v1/security-advisories/?status=closed",
            "/api/v1/security-advisories/?cve=CVE-2026-1001", f"/api/v1/security-advisories/?model={aw['model'].pk}",
            "/api/v1/security-advisories/?reference=demo-adv-0001", "/api/v1/security-advisories/?status=bogus",
            "/api/v1/security-advisories/?cve=critical", "/api/v1/security-decisions/?applies_to=advisory",
            f"/api/v1/security-decisions/?advisory={aw['advisories'][0].number}", "/api/v1/security-decisions/?applies_to=bogus"]
    for a in aw["advisories"]:
        urls += [f"/api/v1/security-advisories/{a.pk}/", f"/api/v1/security-advisories/{a.number.lower()}/"]
    urls += [f"/api/v1/security-decisions/{d.number}/" for d in aw["decisions"]]
    readers = _readers(tenant, make_user)
    for who in ("view", "devices", "director"):
        client.force_login(readers[who])
        for url in urls:
            r = client.get(url)
            decision_api = "security-decisions" in url
            expected = 403 if decision_api and who == "view" else 400 if "bogus" in url or "critical" in url else 200
            assert r.status_code == expected, (who, url, r.status_code)
            assert decision_never(r.content.decode()) == [], (who, url, decision_never(r.content.decode()))
        for method in ("post", "put", "patch", "delete"):
            r = getattr(client, method)(f"/api/v1/security-advisories/{aw['advisories'][0].pk}/", {}, content_type="application/json")
            assert r.status_code in (403, 405) and decision_never(r.content.decode()) == [], (who, method)
    client.force_login(readers["director"])
    data = client.get(f"/api/v1/security-advisories/{aw['advisories'][0].number}/").json()
    assert {d["asset"]["tag"] for d in data["devices"]} == {"CE-10002", "A-OFF", "A-RENT", "A-NONE", "A-FIX", "A-OTHER", "A-UPD"}


def _constants(module) -> list:
    """A module's upper-case words: strings, the strings of dicts and of tuples or lists (never-word lists themselves left out)."""
    texts = []
    for name, value in vars(module).items():
        if not name.isupper() or name in ("NEVER_WORDS", "DECISION_NEVER_WORDS", "NEVER", "NEVER_WORDS_ADVISORY"):
            continue
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            texts += [v for v in value.values() if isinstance(v, str)]
        elif isinstance(value, (tuple, list)):
            texts += [v for v in value if isinstance(v, str)]
    return texts


def test_the_never_words_on_slice_40s_constants_and_choices():
    """advisories.py's words and rules.py's (its slice 40 sections among them: the tab's and the decisions' HICP sentences, the line,
    response, and status words, the empty states, the badge titles), the API's advisory words, and every choice slice 40 added (labels and
    slugs): rules.DECISION_NEVER_WORDS. CVE_WORDS repeats the piece a person typed: its template is scanned here, a refusal echoing a never
    word they typed is allowed for at the door (test_the_never_words_on_every_advisory_save)."""
    from apps.web import views_cyber_advisories

    texts = _constants(advisories) + _constants(rules) + _constants(views_cyber_advisories)
    texts += [v for k, v in vars(api_cyber).items() if k.startswith(("ADVISOR", "CVE_", "REMOVED_ADVISOR", "APPLIES_TO")) and isinstance(v, str)]
    for choices in (AdvisoryStatus, NotApplicableReason, AdvisorySourceKind, AdvisoryChange, AdvisoryVersions, AdvisoryRemedy, AppliesTo,
                    DecisionResponse):
        texts += [*choices.labels, *choices.values]
    slice_40 = (rules.HICP_ADVISORIES, rules.HICP_ADVISORY_DECISIONS, rules.ADVISORIES_EMPTY, rules.DEVICE_ADVISORIES_EMPTY,
                rules.ADVISORY_BADGE_TITLE, rules.BOTH_BADGE_TITLE, *rules.LINE_KINDS.values(), *rules.ANSWER_KINDS.values(),
                advisories.CVE_WORDS, advisories.CLOSE_NONE, advisories.NOT_APPLICABLE_NOTE, advisories.ISAC_HELP)
    assert set(slice_40) <= set(texts) and len(texts) > 300
    hits = sorted({(w, t[:90]) for t in texts for w in decision_never(t)})
    assert hits == []
    assert not [t for t in _constants(advisories) + _constants(rules) if re.search(r"CVE-\d{4}-\d", t)]  # never a concrete CVE ID


def test_the_never_words_on_every_advisory_save(client, make_user, advisory_world, today):
    """Each advisory writer through its door as the director, refused and saved: the answer (the modal again with the writer's words, or
    the advisory's page into #drawer) and its toast, rules.DECISION_NEVER_WORDS. A CVE refusal repeats the piece typed, "(Critical)":
    allowed for, and nothing else."""
    aw = advisory_world
    client.force_login(make_user("director"))
    answers = []

    def post(url, data, *, saved):
        r = client.post(url, data, **MODAL)
        assert r.status_code == 200, (url, r.status_code)
        assert (r.get("HX-Retarget") == "#drawer") is saved, (url, r.content.decode()[:600])
        toast = " ".join(json.loads(r[h]).get("toast", {}).get("value", "") for h in ("HX-Trigger", "HX-Trigger-After-Settle") if h in r)
        assert not saved or toast, url
        answers.append((url, unescape(r.content.decode()) + "\n" + toast))
        return r

    typed = {"received_on": today.isoformat(), "cves": "CVE-2026-6001 (Critical)", "source": "manufacturer_bulletin",
             "reference": "DEMO-ADV-0006", "url": "", "published_on": ""}
    refused = post("/security/advisories/new/", typed, saved=False)
    assert advisories.CVE_WORDS.format(piece="(Critical)") in unescape(refused.content.decode())
    answers[-1] = (answers[-1][0], answers[-1][1].replace("(Critical)", ""))  # the piece as typed, said back: allowed for
    post("/security/advisories/new/", {**typed, "cves": "CVE-2026-1002 CVE-2026-6001", "url": "http://10.0.0.5/x"}, saved=False)
    post("/security/advisories/new/", {**typed, "cves": "CVE-2026-1002 CVE-2026-6001", "seen": aw["advisories"][0].number}, saved=True)
    new = SecurityAdvisory.objects.get(sources__reference="DEMO-ADV-0006")
    base = f"/security/advisories/{new.number}"
    post(f"{base}/sources/new/", {"source": "other", "reference": "DEMO-ADV-0006-N", "cves": "CVE-2026-6002"}, saved=True)
    source = new.sources.order_by("created_at", "pk").first()
    post(f"{base}/sources/{source.pk}/revision/", {"revision": "B", "updated_on": (today + DAY).isoformat()}, saved=False)
    post(f"{base}/sources/{source.pk}/revision/", {"revision": "B", "last_change": ["products_changed"]}, saved=True)
    models = f"{base}/models/?model={aw['model'].pk}"
    post(models, {"versions": "listed", "remedy": "release", "fix_release": str(aw["releases"]["9.33.1"].pk),
                  "release_ids": [str(aw["releases"]["9.33.1"].pk)]}, saved=False)
    post(models, {"action": "add_version", "add_version": "9.70-DEMO", "versions": "listed", "remedy": "release"}, saved=False)
    post(models, {"versions": "listed", "remedy": "patches", "patches_reference": "DEMO-PATCH-2026-10",
                  "release_ids": [str(aw["releases"]["9.40"].pk)]}, saved=True)
    post(f"{base}/close/", {}, saved=False)  # models to confirm
    post(f"{base}/confirm/", {}, saved=True)
    post(f"{base}/close/", {}, saved=False)  # A-OTHER: no response recorded
    post(f"{base}/not-applicable/", {"not_applicable_reason": "none_here"}, saved=False)
    post(f"{base}/edit/", {"received_on": today.isoformat(), "cves": "CVE-2026-6001 CVE-2026-6002 CVE-2026-6003"}, saved=True)
    post(f"{base}/models/{new.model_rows.get().pk}/remove/", {}, saved=True)
    post(f"{base}/not-applicable/", {"not_applicable_reason": "no_model_here"}, saved=True)
    post(f"{base}/reopen/", {}, saved=True)
    post(f"{base}/sources/{source.pk}/remove/", {}, saved=True)
    post(f"{base}/in-error/", {}, saved=True)
    first = aw["advisories"][0].number
    post(f"/security/advisories/{first}/in-error/", {}, saved=False)  # a live decision under it
    post(f"/security/advisories/{first}/close/", {}, saved=False)
    post(f"/security/advisories/{aw['advisories'][2].number}/reopen/", {}, saved=True)
    post(f"/security/decisions/new/?advisory={first}&asset=A-RENT&from=advisory",
         {"response": "keep_with_controls", "controls": ["physical_controls"], "accepted_by": "ce_leader", "decided_on": today.isoformat(),
          "plan": "return_to_owner"}, saved=True)
    post(f"/security/decisions/new/?advisory={aw['advisories'][4].number}&release={aw['releases']['9.33.1'].pk}&from=advisory",
         {"response": "not_in_use_here", "plan": "replace_planned", "accepted_by": "ce_leader", "decided_on": today.isoformat()}, saved=False)
    hits = [(url, decision_never(text)) for url, text in answers if decision_never(text)]
    assert hits == [] and len(answers) >= 24


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


def _advisory_state(tenant_):
    with tenant_context(tenant_):
        return (sorted(SecurityAdvisory.objects.values_list("pk", "number", "status", "cves", "models_confirmed_on", "removed_on", "changes")),
                sorted(AdvisorySource.objects.values_list("pk", "reference", "revision", "removed_on")),
                sorted((str(pk), str(dm), versions, remedy, str(fix)) for pk, dm, versions, remedy, fix in
                       AdvisoryModel.objects.values_list("pk", "device_model_id", "versions", "remedy", "fix_release_id")),
                sorted(str(pk) for pk in AdvisoryRelease.objects.values_list("release_id", flat=True)),
                sorted(DeviceSecurityDecision.objects.values_list("number", "ended_on")),
                SoftwareRelease.objects.count(), sorted(str(pk) for pk in DeviceSecurity.objects.values_list("release_id", flat=True)))


def test_no_advisory_door_reaches_another_facility_s_rows(client, make_user, advisory_world, theirs, today):
    """Slice 40: another facility's advisory (numbered as this facility's first: each facility counts its own), its source, model row,
    model, release, device, and decision, through every advisory door that takes an id, GET and POST: a 404 (or this facility's own
    advisory by its number, the id refused in words), nothing written in either facility; the API answers a 404 for their advisory's id
    and lists none of theirs."""
    aw = advisory_world
    with tenant_context(theirs["tenant"]):
        their = advisories.record_advisory(received_on=today - 30 * DAY, cves="CVE-2026-1001", source=AdvisorySourceKind.MANUFACTURER_BULLETIN,
                                           reference="DEMO-ADV-0001", by=None, today=today)
        their_row = advisories.set_model(their, device_model=theirs["model"], versions=AdvisoryVersions.LISTED,
                                         release_ids=[theirs["release"].pk], remedy=AdvisoryRemedy.MITIGATIONS, confirm=True, by=None,
                                         today=today)
        their_source = their.sources.get()
        their_decision = decisions.record_decision(asset=theirs["asset"], applies_to=AppliesTo.ADVISORY, advisory=their, reasons=[],
                                                   response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, accepted_by=AcceptedBy.CE_LEADER,
                                                   by=None, today=today)
    first = aw["advisories"][0]
    assert their.number == first.number and their_decision.number == aw["decisions"][0].number  # numbers repeat across facilities
    client.force_login(make_user("director"))
    mine, others = _advisory_state(aw["model"].tenant), _advisory_state(theirs["tenant"])
    n, model, rel = first.number, theirs["model"].pk, theirs["release"].pk
    fields = {"source": "other", "reference": "DEMO-ADV-0099", "revision": "3", "last_change": ["products_changed"], "versions": "listed",
              "release_ids": [str(rel)], "remedy": "release", "fix_release": str(rel), "add_version": "9.99", "action": "",
              "response": "keep_without_added_controls", "accepted_by": "ce_leader", "decided_on": today.isoformat(), "target": "release",
              "target_release": str(rel), "due_on": (today + DAY).isoformat(), "priority": "normal", "from_releases": [str(rel)]}
    not_found = [f"/security/advisories/{n}/sources/{their_source.pk}/edit/", f"/security/advisories/{n}/sources/{their_source.pk}/revision/",
                 f"/security/advisories/{n}/sources/{their_source.pk}/remove/", f"/security/advisories/{n}/models/?model={model}",
                 f"/security/decisions/new/?advisory={n}&release={rel}", f"/security/decisions/new/?advisory={n}&model={model}",
                 f"/security/decisions/new/?advisory={n}&asset={theirs['asset'].tag}", f"/security/decisions/new/?advisory={their.pk}&model={model}",
                 f"/security/devices/{theirs['asset'].tag}/updates/open/?advisory={n}&from=advisory",
                 f"/security/releases/{rel}/open-updates/?advisory={n}&from=advisory"]
    for url in not_found:
        assert client.get(url, **MODAL).status_code == 404, url
        assert client.post(url, fields, **MODAL).status_code == 404, url
    gone = f"/security/advisories/{n}/models/{their_row.pk}/remove/"  # a row of none of ours: said in words, never removed
    assert advisories.ROW_GONE.format(number=n) in unescape(client.get(gone, **MODAL).content.decode())
    assert client.post(gone, {}, **MODAL).status_code == 404
    # our advisory, their release: refused in the writer's words
    r = client.post(f"/security/advisories/{n}/models/?model={aw['model'].pk}",
                    {"versions": "listed", "release_ids": [str(rel)], "remedy": "mitigations", "confirm": "on"}, **MODAL)
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert advisories.CHOOSE_MODEL_RELEASE.format(model=aw["model"].model) in unescape(r.content.decode())
    page = unescape(client.get(f"/security/advisories/{n}/", **HX).content.decode())
    assert "THEIRS-1" not in page and their_decision.number in page  # their decision's number is ours too: our first's
    assert client.get(f"/api/v1/security-advisories/{their.pk}/").status_code == 404
    assert client.get(f"/api/v1/security-advisories/?model={model}").json()["count"] == 0
    assert str(their.pk) not in json.dumps(client.get("/api/v1/security-advisories/?removed=1").json())
    assert str(their_decision.pk) not in json.dumps(client.get("/api/v1/security-decisions/?applies_to=advisory").json())
    assert (_advisory_state(aw["model"].tenant), _advisory_state(theirs["tenant"])) == (mine, others)
