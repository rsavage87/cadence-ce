"""
Slice 37 review fixes, F2: the Device security screen, its filters, and the device edit's toast. Each test is its finding's own scenario
(the review's C-numbers):

- C15 Add release with a blank version is refused (the profile has its own door), never recorded as the model's profile.
- C21 Re-adding a version entered in error brings back the facts it had; only what was filled in goes over them (the toast says so).
- C20 / C28 A head figure's link keeps every filter it was counted under and adds only what the figure needs; it is left off when the two
  cannot be one query; the page it opens counts what the figure counted.
- C16 / C29 The device-security API reads the Fleet's filters with the screen's parser (apps.cybersecurity.filters): the same query, the
  same devices; an unknown value is a 400.
- C17 / C25 / C30 (screen side) The model panel words the facility group's source as the drawer does (rules.FACILITY_SOURCE).
- C24 The Slice 37 CSS restyles only its own panel's header chips, never every drawer's.
- C26 devices.csv's file name is ASCII, so the Content-Disposition header is never MIME-encoded.
- C8 / C11 / C14 / C18 / C22 / C32 Editing a device's model, which clears its software release, says so in the toast and fires
  cyber-changed, for a reader of the register only.
"""
import json
import re
import uuid
from datetime import timedelta
from html import unescape
from itertools import product
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from cyber_fixtures import facts as record
from cyber_fixtures import release
from django.http import QueryDict
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import filters, rules
from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import (
    Connects,
    DeviceSecurity,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    Patching,
    PatientData,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
)
from apps.equipment.models import Asset, Department, DeviceModel, Ownership, RiskClass
from apps.web import asset_cyber, views_equipment
from apps.web import forms as web_forms
from apps.web import views_cybersecurity as views
from apps.web.forms_cybersecurity import parse_fleet_filters

HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)
DEVICES_API = "/api/v1/device-security/"


def html(r) -> str:
    return r.content.decode()


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def errors(body: str) -> dict:
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="cy-(?:set-)?(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


def never_words(text: str) -> list:
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def custom_user(client, tenant, slug, levels):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
    client.force_login(user)
    return user


@pytest.fixture
def world(ctx, dept, pump_model, vent_model, vent):
    """test_cyber_web's world, with two more facts so every head figure has devices under a filter: P-4 (no release) recorded as
    connected here (wireless), and 9.19.2's OS past its vendor's support too (P-3: past security updates and OS ended).
    Pumps (BD Alaris): a profile (wireless, stores patient data, IT installs updates, not segmented, MDS2 requested); 9.19.2 past its
    security updates, 9.33.1 announced; P-1, P-2 (ICU) and the rental P-R (4 West) on 9.33.1, P-3 (4 West) on 9.19.2, P-4 (4 West) not
    recorded. Ventilators: a profile (wired, all on a segment), 3.1 with its MDS2 on file, the vent on 3.2 recorded as connected (wired).
    A sterilizer with nothing recorded in 4 West."""
    today = timezone.localdate()
    west = Department.objects.create(name="4 West")
    ster = DeviceModel.objects.create(manufacturer="Steris", model="Amsco 400", description="Steam sterilizer", category="Sterilizers",
                                      risk_class=RiskClass.MEDIUM)
    pump_profile = release(pump_model, connects=Connects.WIRELESS, patient_data=PatientData.STORES, updates_by=UpdatesBy.IT,
                           segmented=Segmented.NONE, mds2=Mds2Status.REQUESTED, mds2_requested_on=today - 10 * DAY)
    old = release(pump_model, "9.19.2", os_family=OsFamily.EMBEDDED, os_support_ends_on=today - 60 * DAY,
                  security_updates_end_on=today - 30 * DAY)
    new = release(pump_model, "9.33.1", os_family=OsFamily.EMBEDDED, security_updates_end_on=today + 400 * DAY)
    p1 = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, room="4")
    p2 = Asset.objects.create(tag="P-2", device_model=pump_model, department=dept)
    p3 = Asset.objects.create(tag="P-3", device_model=pump_model, department=west)
    p4 = Asset.objects.create(tag="P-4", device_model=pump_model, department=west)
    rental = Asset.objects.create(tag="P-R", device_model=pump_model, department=west, ownership=Ownership.RENTAL, owner="Acme Rentals")
    for a in (p1, p2, rental):
        record(a, release=new, release_since=today - 5 * DAY)
    record(p3, release=old, release_since=today - 100 * DAY)
    record(p4, network=Network.WIRELESS)
    vent_profile = release(vent_model, connects=Connects.WIRED, segmented=Segmented.ALL)
    r31 = release(vent_model, "3.1", os_family=OsFamily.LINUX, os_name="Linux 4.19", security_updates_end_on=today + 200 * DAY,
                  mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001",
                  mds2_url="https://manufacturer.example/mds2/demo", patching=Patching.MANUFACTURER)
    r32 = release(vent_model, "3.2")
    record(vent, release=r32, network=Network.WIRED)
    s1 = Asset.objects.create(tag="S-1", device_model=ster, department=west)
    return {"today": today, "dept": dept, "west": west, "ster": ster, "old": old, "new": new, "p": [p1, p2, p3, p4], "rental": rental,
            "r31": r31, "r32": r32, "s1": s1, "pump_profile": pump_profile, "vent_profile": vent_profile}


# --- C15: Add release needs a version -------------------------------------------------------------------------------------------------

def test_c15_add_release_with_a_blank_version_is_refused_never_the_profile(client, make_user, world, pump_model):
    """C15: Add release (no ?profile=1), the version blank or spaces, this version's facts typed: refused under Version in words, nothing
    stored, the model's devices with no release recorded read nothing new. A model with a profile is asked for the version too (never
    "already has a profile"). Record the profile keeps its own door."""
    client.force_login(make_user("technician"))
    ster = world["ster"]
    url = f"/security/models/{ster.pk}/releases/new/"
    r = client.post(url, {"version": "  ", "security_updates_end_on": "2025-01-31", "os_family": "windows"}, **HX)
    found = errors(html(r))
    assert found == {"version": cyber.VERSION_NEEDED} and "HX-Retarget" not in r and toast_of(r) == ""
    assert 'value="2025-01-31"' in html(r)  # what was typed is kept
    assert not SoftwareRelease.objects.filter(device_model=ster).exists()
    assert rules.device_standing(world["s1"], world["today"]).security.key == rules.SECURITY_NOT_RECORDED
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", {"version": "", "os_family": "embedded"}, **HX)
    assert errors(html(r)) == {"version": cyber.VERSION_NEEDED}
    assert SoftwareRelease.objects.filter(device_model=pump_model).count() == 3  # the profile and two releases, as they were
    # the profile's own door still records it
    r = client.post(f"{url}?profile=1", {"connects": "none"}, **HX)
    assert toast_of(r) == "Steris Amsco 400: profile recorded" and SoftwareRelease.objects.get(device_model=ster).version_key == ""


# --- C21: a restore brings back the facts it had ------------------------------------------------------------------------------------

def test_c21_restoring_a_version_entered_in_error_keeps_its_facts(client, make_user, world, vent_model):
    """C21: 3.1 with its MDS2 on file (2019 form, DEMO-MDS2-0001, its https link), its OS, its security date, and a disclosure answer is
    marked entered in error, then added again through Add release with only its version: every fact comes back, and the toast says so.
    Filled in, a field goes over the fact it had; the others stay."""
    client.force_login(make_user("technician"))
    r31, today = world["r31"], world["today"]
    before = {f: getattr(r31, f) for f in ("mds2", "mds2_edition", "mds2_document", "mds2_url", "os_family", "os_name",
                                           "security_updates_end_on", "patching")}
    assert toast_of(client.post(f"/security/releases/{r31.pk}/remove/", **HX)) == "Version 3.1 marked entered in error"
    r = client.post(f"/security/models/{vent_model.pk}/releases/new/", {"version": "3.1"}, **HX)
    assert toast_of(r) == "Version 3.1 restored (it was entered in error), with the facts it had" and r["HX-Retarget"] == "#drawer"
    r31.refresh_from_db()
    assert r31.removed_on is None and {f: getattr(r31, f) for f in before} == before
    assert "MDS2: On file for 3.1" in html(r) and never_words(html(r) + toast_of(r)) == []
    assert r31.history.first().history_change_reason == cyber.RESTORED
    # again, with a fact filled in: it goes over the one it had, the rest stay
    client.post(f"/security/releases/{r31.pk}/remove/", **HX)
    later = today + 300 * DAY
    r = client.post(f"/security/models/{vent_model.pk}/releases/new/", {"version": "3.1", "security_updates_end_on": later.isoformat(),
                                                                         "mds2_document": "", "patching": ""}, **HX)
    assert toast_of(r) == "Version 3.1 restored (it was entered in error), with the facts it had and what you entered over them"
    r31.refresh_from_db()
    assert r31.security_updates_end_on == later and (r31.mds2_document, r31.mds2_url, r31.patching) == (
        before["mds2_document"], before["mds2_url"], before["patching"])


# --- C20 / C28: a head figure's link opens the devices it counted ------------------------------------------------------------------

LINKED = ("updates_ended", "no_release")
CONNECTED = ["", "either", "connected", "can_connect", "not_connected", "not_recorded"]
STANDING = ["", "updates_ended", "updates_end_announced", "security_not_recorded", "os_ended", "os_end_announced"]


def screen(query: str, today):
    """What the Fleet reads for an address's query: (its filters, its fleet, its figures by key), as views._fleet_context does."""
    f = parse_fleet_filters(QueryDict(query), views._fleet_options())
    fl = rules.fleet(today, views._narrowed(f, today))
    return f, fl, {fig["key"]: fig for fig in views._figures(fl, f)}


def _conflicts(f, key: str) -> bool:
    """The figure's filters and the screen's cannot be one query: a connection that excludes "connected here or able to", or (past
    security updates) another standing beside it."""
    return f.connected in ("not_connected", "not_recorded") or (key == "updates_ended" and f.standing not in ("", "updates_ended"))


def test_c20_c28_head_figure_links_open_the_devices_they_counted(client, make_user, world):
    """C20 / C28: under every connection and standing filter (with release not recorded, and with a department), each linked figure's
    link keeps the filters on screen and adds only what the figure counts: the page it opens counts the same devices, and its own figure
    reads the same. Where the two cannot be one query, the link is left off."""
    today = world["today"]
    west = str(world["west"].pk)
    checked = linked = 0
    for connected, standing, norelease, dept in product(CONNECTED, STANDING, ("", "1"), ("", west)):
        query = urlencode({k: v for k, v in {"dept": dept, "connected": connected, "standing": standing, "norelease": norelease}.items() if v})
        f, _fl, figs = screen(query, today)
        for key in LINKED:
            fig = figs[key]
            checked += 1
            if _conflicts(f, key):
                assert fig["url"] == "", (query, key)
                continue
            assert fig["url"], (query, key)
            linked += 1
            link = urlsplit(fig["url"])
            assert link.path == "/security/"
            landing_f, landing, landing_figs = screen(link.query, today)
            # every filter on screen kept, only what the figure counts added
            for name, value in f.params().items():
                if name not in ("connected", "standing"):
                    assert landing_f.params().get(name) == value, (query, key, name)
            assert landing.devices == fig["count"] == landing_figs[key]["count"], (query, key, fig["url"])
    assert checked == 288 and 0 < linked < checked


def test_c20_c28_the_findings_scenarios_over_http(client, make_user, world):
    """The finding's own pages: a figure counted under "Connected here, as recorded" links there (never widened to "either"), and the page
    it opens reads the same count; "Not connected" and another standing leave the link off."""
    client.force_login(make_user("technician"))

    def figures(url):
        r = client.get(url)
        assert r.status_code == 200
        return {f["key"]: f for f in r.context["figures"]}, r.context["fleet"].devices

    figs, _n = figures("/security/?connected=connected")
    assert figs["no_release"]["count"] == 1  # P-4, recorded as connected here
    assert figs["no_release"]["url"] == "/security/?connected=connected&norelease=1"
    landing, n = figures(figs["no_release"]["url"])
    assert landing["no_release"]["count"] == n == 1
    assert figs["updates_ended"]["url"] == "/security/?connected=connected&standing=updates_ended"
    landing, n = figures(figs["updates_ended"]["url"])
    assert figs["updates_ended"]["count"] == landing["updates_ended"]["count"] == n == 0
    figs, _n = figures("/security/?connected=not_connected")
    assert figs["no_release"]["count"] == figs["updates_ended"]["count"] == 0 and figs["no_release"]["url"] == figs["updates_ended"]["url"] == ""
    figs, _n = figures("/security/?standing=updates_end_announced")
    assert figs["updates_ended"]["url"] == "" and figs["no_release"]["url"] == "/security/?connected=either&standing=updates_end_announced&norelease=1"
    figs, _n = figures("/security/?standing=os_ended")
    assert figs["updates_ended"]["count"] == 1 and figs["updates_ended"]["url"] == ""  # P-3: both, which one standing filter cannot say
    body = html(client.get("/security/?standing=os_ended"))
    assert 'href="/security/?connected=either&amp;standing=updates_ended"' not in body
    figs, _n = figures("/security/?connected=can_connect")
    assert figs["updates_ended"]["url"] == "/security/?connected=can_connect&standing=updates_ended"
    landing, n = figures(figs["updates_ended"]["url"])
    assert figs["updates_ended"]["count"] == landing["updates_ended"]["count"] == n == 1


# --- C16 / C29: the API reads the Fleet's filters -------------------------------------------------------------------------------------

def api_tags(client, query: str) -> list:
    r = client.get(f"{DEVICES_API}?{query}")
    assert r.status_code == 200, (query, r.content)
    return sorted(row["tag"] for row in r.json()["results"])


def screen_tags(query: str, today) -> list:
    f = parse_fleet_filters(QueryDict(query), views._fleet_options())
    return sorted(a.tag for a in views._narrowed(f, today))


def test_c16_c29_the_device_security_api_answers_the_fleets_filters(client, make_user, world):
    """C16 / C29: the Fleet's queries (and each head figure's link) asked of the API answer the devices the screen lists for them, never
    the whole fleet; "either" is a value; an unknown value is a 400 naming its parameter."""
    client.force_login(make_user("technician"))
    today = world["today"]
    west = world["west"].pk
    queries = ["", "connected=either", "connected=either&standing=updates_ended", "connected=either&norelease=1", "whose=ours",
               "whose=temporary", "standing=updates_ended", "standing=os_ended", "standing=updates_end_announced", "norelease=1",
               "category=Ventilators", "category=Infusion+pumps&whose=ours", f"dept={west}", f"dept={west}&connected=either&norelease=1",
               "connected=connected", "connected=can_connect", "connected=not_recorded", "standing=security_not_recorded&whose=ours"]
    for query in queries:
        assert api_tags(client, query) == screen_tags(query, today), query
    assert api_tags(client, "standing=updates_ended") == ["P-3"] and api_tags(client, "whose=temporary") == ["P-R"]
    assert api_tags(client, "norelease=1") == ["P-4", "S-1"] and api_tags(client, "category=Nothing") == []
    # every head figure's link, under a few filters: the API answers what the figure counted
    for query in ("", "whose=ours", f"dept={west}", "connected=connected", "standing=os_ended", "norelease=1"):
        _f, _fl, figs = screen(query, today)
        for key in LINKED:
            if figs[key]["url"]:
                link = urlsplit(figs[key]["url"]).query
                assert len(api_tags(client, link)) == figs[key]["count"], (query, key)
                assert api_tags(client, link) == screen_tags(link, today)
    # the API's own name for the department, and its others, as before
    assert api_tags(client, f"department={west}") == api_tags(client, f"dept={west}") == ["P-3", "P-4", "P-R", "S-1"]
    assert api_tags(client, f"department={west}&dept={west}") == ["P-3", "P-4", "P-R", "S-1"]
    # unknown values: a 400 naming each parameter, never the whole fleet
    r = client.get(f"{DEVICES_API}?whose=mine&standing=bad&connected=yes&norelease=2&dept=nope")
    assert r.status_code == 400 and r.json() == {"whose": [filters.WHOSE_WORDS], "standing": [filters.STANDING_WORDS],
                                                 "connected": [filters.CONNECTED_WORDS], "norelease": [filters.NORELEASE_WORDS],
                                                 "dept": [filters.DEPARTMENT_WORDS]}
    r = client.get(f"{DEVICES_API}?department={uuid.uuid4()}&dept={west}")
    assert r.status_code == 400 and r.json() == {"department": [filters.DEPARTMENT_TWICE]}
    assert never_words(filters.WHOSE_WORDS + filters.STANDING_WORDS + filters.CONNECTED_WORDS + filters.NORELEASE_WORDS
                       + filters.DEPARTMENT_WORDS + filters.DEPARTMENT_TWICE + filters.CATEGORY_WORDS) == []


def test_c16_the_filters_module_is_the_screens_and_imports_neither_door():
    """One parser: the screen's Whose slugs are the Equipment screen's, and apps.cybersecurity.filters imports neither apps.web nor
    apps.api (the API never imports apps.web)."""
    assert (filters.WHOSE_OURS, filters.WHOSE_TEMPORARY) == (web_forms.WHOSE_OURS, web_forms.WHOSE_TEMPORARY)
    source = Path("apps/cybersecurity/filters.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+apps\.(web|api)\b", source, re.MULTILINE)
    api_source = Path("apps/api/views_cybersecurity.py").read_text()
    assert "filters.parse_strict" in api_source and not re.search(r"^\s*(from|import)\s+apps\.web\b", api_source, re.MULTILINE)


# --- C17 / C25 / C30: the facility group's source ----------------------------------------------------------------------------------

def test_c17_c25_c30_the_panel_words_the_facilitys_arrangement_as_the_drawer(client, make_user, world, pump_model):
    """C17 / C25 / C30: the pump profile records who installs updates here and the segment. The panel's profile card heads "This
    facility's arrangement" with the facility's own source (rules.FACILITY_SOURCE, the drawer's words), never the manufacturer's
    disclosure; the disclosure group keeps its own."""
    client.force_login(make_user("technician"))
    r = client.get(f"/security/models/{pump_model.pk}/", **HX)
    sources = {g["key"]: g["source"] for g in r.context["profile_card"]["groups"]}
    assert sources["facility"] == "The model's, for every version" == views.FACILITY_SOURCE
    assert sources["facility"].casefold() == rules.FACILITY_SOURCE.casefold() == asset_cyber.FACILITY_SOURCE.casefold()
    assert sources["disclosure"] == "From the model's disclosure, version not stated"
    body = unescape(html(r))
    assert '<span class="t">This facility\'s arrangement</span><span class="cy-src">The model\'s, for every version</span>' in body
    # a profile with no facility facts reads "Not recorded" there
    r = client.get(f"/security/models/{world['ster'].pk}/", **HX)
    assert {g["key"]: g["source"] for g in r.context["profile_card"]["groups"]}["facility"] == "Not recorded"


# --- C24: the Slice 37 CSS stays in its own panel ------------------------------------------------------------------------------------

def test_c24_the_slice_37_css_never_restyles_another_drawers_header():
    """C24: every selector of the Slice 37 A block names one of its own (cy-) classes, so no rule there reaches the device, work order,
    contract, incident, model, test equipment, or vendor engineer drawers' header chips; the panel's header carries its own class."""
    css = Path("apps/web/static/web/cadence.css").read_text()
    block = css[css.index("/* Slice 37 A"):css.index("/* Slice 37 A: end */")]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    selectors = []
    for head in re.findall(r"([^{}]+)\{", block):
        head = head.strip()
        if head.startswith("@media"):
            continue
        selectors += [s.strip() for s in head.split(",")]
    assert selectors and [s for s in selectors if not re.search(r"\.cy-[\w-]+", s)] == []
    assert ".cy-panel-h .chips .chip" in selectors and ".dr-h .chips .chip" not in selectors
    panel = Path("apps/web/templates/web/_cyber_model.html").read_text()
    assert '<div class="dr-h cy-panel-h">' in panel
    for name in ("_asset_drawer", "_wo_drawer", "_contract_drawer", "_incident_drawer", "_model_drawer", "_te_drawer", "_vendor_engineer_drawer"):
        assert "cy-panel-h" not in Path(f"apps/web/templates/web/{name}.html").read_text(), name


# --- C26: an ASCII file name -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("manufacturer, model, stem", [
    ("Dräger", "Evita V500 Œ", "drager-evita-v500"),
    ("Hamilton", "μCor", "hamilton-cor"),
    ("BD", "Alaris 8015 PCU", "bd-alaris-8015-pcu"),
])
def test_c26_the_devices_csv_file_name_is_ascii(client, make_user, world, pump_model, manufacturer, model, stem):
    """C26: a model named with letters outside ASCII (Latin-1's "ä", or "Œ" and "μ" outside it) downloads under an ASCII name, so the
    Content-Disposition header goes out as written (never MIME-encoded as a whole, which browsers cannot read)."""
    client.force_login(make_user("technician"))
    DeviceModel.objects.filter(pk=pump_model.pk).update(manufacturer=manufacturer, model=model)
    r = client.get(f"/security/devices.csv?model={pump_model.pk}")
    header = r["Content-Disposition"]
    assert r.status_code == 200 and header.isascii() and not header.startswith("=?")
    assert header == f'attachment; filename="cadence-device-security-{stem}-{world["today"]:%Y-%m-%d}.csv"'
    b"".join(r.streaming_content)


def test_c26_a_model_with_no_ascii_letter_is_named_by_its_id(client, make_user, world, pump_model):
    client.force_login(make_user("technician"))
    DeviceModel.objects.filter(pk=pump_model.pk).update(manufacturer="Ωμέγα", model="Σ")
    r = client.get(f"/security/devices.csv?model={pump_model.pk}")
    assert r["Content-Disposition"] == f'attachment; filename="cadence-device-security-{pump_model.pk}-{world["today"]:%Y-%m-%d}.csv"'
    b"".join(r.streaming_content)


# --- C8 / C11 / C14 / C18 / C22 / C32: the device edit's toast ---------------------------------------------------------------------

def edit_post(asset, **over) -> dict:
    data = {"serial": asset.serial, "device_model": str(asset.device_model_id), "department": str(asset.department_id), "room": asset.room,
            "installed_on": asset.installed_on.isoformat() if asset.installed_on else "", "acquisition_cost": str(asset.acquisition_cost),
            "warranty_end": asset.warranty_end.isoformat() if asset.warranty_end else "", "condition": str(asset.condition),
            "next_pm_on": asset.next_pm_on.isoformat() if asset.next_pm_on else "", "notes": asset.notes}
    data.update(over)
    return data


@pytest.mark.parametrize("role", ["technician", "director"])
def test_c8_editing_the_model_says_the_release_was_cleared(client, make_user, world, vent, pump_model, role):
    """C8 / C11 / C14 / C18 / C22 / C32: the vent runs 3.2 of its model; Edit details changes its model to the pumps'. The release and
    its day are cleared (history "Device model changed"), the toast says so, and cyber-changed refetches the Device security lists."""
    client.force_login(make_user(role))
    DeviceSecurity.objects.filter(asset=vent).update(release_since=world["today"] - 3 * DAY)
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, device_model=str(pump_model.pk)), **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    sec = DeviceSecurity.objects.get(asset=vent)
    assert (sec.release, sec.release_since) == (None, None) and sec.history.first().history_change_reason == cyber.DEVICE_MODEL_CHANGED
    t = triggers(r)
    assert toast_of(r) == "CE-10001 updated; its software release was cleared (Device security)"
    assert "cyber-changed" in t and "devices-changed" in t and never_words(toast_of(r)) == []
    # edited again with no release left to clear: the toast as it always was
    vent.refresh_from_db()
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, room="9"), **HX)
    assert toast_of(r) == "CE-10001 updated" and "cyber-changed" not in triggers(r)


def test_c8_a_reader_without_the_register_is_told_nothing_about_it(client, tenant, world, vent, pump_model):
    """An Equipment Edit role without Device security View changes the model: the release is cleared all the same, and the toast stays
    "<tag> updated" with no cyber-changed (nothing about the register reaches someone who may not read it)."""
    custom_user(client, tenant, "equipment-editor", {Module.EQUIPMENT: Level.EDIT})
    r = client.post(f"/equipment/{vent.tag}/edit/", edit_post(vent, device_model=str(pump_model.pk)), **HX)
    assert r.status_code == 200 and DeviceSecurity.objects.get(asset=vent).release is None
    assert toast_of(r) == "CE-10001 updated" and "cyber-changed" not in triggers(r)
    assert views_equipment.RELEASE_CLEARED not in r.content.decode()
