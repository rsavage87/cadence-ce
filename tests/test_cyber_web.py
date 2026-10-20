"""
Slice 37, wave 2 A: the Device security screen (apps/web/views_cybersecurity.py, forms_cybersecurity.py, web/cybersecurity.html,
web/_cyber_*.html): Fleet (the head figures, one row per model, its filters, its CSV), a model's panel (the profile's card, the
releases, the devices on site, the devices' CSV), Models, Changes for IT and its CSV, the release and profile forms, entered in error,
and Set devices to this release.

Who: Device security View sees the model-level parts; every list naming devices (the Fleet, a panel's devices, devices.csv, Changes
for IT) needs Equipment View too; Device security Edit records releases and the profile; Set devices to this release needs Equipment View
too. The analyst (None by default) and every scoped user (even with a custom Full role) get 403s. Refusals come back under their fields
with what was typed; every page body and toast is scanned for the words the register never says. The rules and services themselves are
tests/test_cyber_rules.py and tests/test_cyber_services.py.
"""
import json
import re
import uuid
from datetime import timedelta
from html import unescape
from pathlib import Path

import pytest
from csvutil import csv_rows
from cyber_fixtures import facts as record
from cyber_fixtures import release
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, reverse
from django.utils import timezone
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import rules
from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import (
    Connects,
    DeviceSecurity,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    PatientData,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership, RiskClass
from apps.tenants.context import tenant_context
from apps.web import views_cybersecurity as views

HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)


def part(target):
    return {**HX, "HTTP_HX_TARGET": target}


def html(r) -> str:
    return r.content.decode()


def never_words(text: str) -> list:
    """rules.NEVER_WORDS found in `text` as whole words, in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def errors(body: str) -> dict:
    """{field name: its error} from a modal (the ids are "cy-<name>_error" or "cy-set-<name>_error"), "" for the one at the top."""
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="cy-(?:set-)?(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def custom_user(client, tenant, slug, levels, scope="", **extra):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)
    client.force_login(user)
    return user


@pytest.fixture
def world(ctx, dept, pump_model, vent_model, vent):
    """Pumps (BD Alaris): a profile (wireless, can store patient data, IT installs updates, not on a separate segment, MDS2 requested),
    9.19.2 past its security updates and 9.33.1 announced; P-1, P-2, and the rental P-R on 9.33.1, P-3 on 9.19.2, P-4 not recorded.
    Ventilators: a profile (wired, all on a segment), MDS2 on file for 3.1 only, the vent on 3.2 and recorded as connected (wired).
    A sterilizer with nothing recorded (connection not recorded) in 4 West. Every device added today (Changes for IT)."""
    today = timezone.localdate()
    west = Department.objects.create(name="4 West")
    ster = DeviceModel.objects.create(manufacturer="Steris", model="Amsco 400", description="Steam sterilizer", category="Sterilizers",
                                      risk_class=RiskClass.MEDIUM)
    pump_profile = release(pump_model, connects=Connects.WIRELESS, patient_data=PatientData.STORES, updates_by=UpdatesBy.IT,
                           segmented=Segmented.NONE, mds2=Mds2Status.REQUESTED, mds2_requested_on=today - 10 * DAY)
    old = release(pump_model, "9.19.2", os_family=OsFamily.EMBEDDED, security_updates_end_on=today - 30 * DAY)
    new = release(pump_model, "9.33.1", os_family=OsFamily.EMBEDDED, security_updates_end_on=today + 400 * DAY)
    p1 = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, room="4")
    p2 = Asset.objects.create(tag="P-2", device_model=pump_model, department=dept)
    p3 = Asset.objects.create(tag="P-3", device_model=pump_model, department=west)
    p4 = Asset.objects.create(tag="P-4", device_model=pump_model, department=west)
    rental = Asset.objects.create(tag="P-R", device_model=pump_model, department=west, ownership=Ownership.RENTAL, owner="Acme Rentals")
    for a in (p1, p2, rental):
        record(a, release=new, release_since=today - 5 * DAY)
    record(p3, release=old, release_since=today - 100 * DAY)
    vent_profile = release(vent_model, connects=Connects.WIRED, segmented=Segmented.ALL)
    r31 = release(vent_model, "3.1", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001",
                  mds2_url="https://manufacturer.example/mds2/demo")
    r32 = release(vent_model, "3.2")
    record(vent, release=r32, network=Network.WIRED)
    s1 = Asset.objects.create(tag="S-1", device_model=ster, department=west)
    return {"today": today, "dept": dept, "west": west, "ster": ster, "old": old, "new": new, "p": [p1, p2, p3, p4], "rental": rental,
            "r31": r31, "r32": r32, "s1": s1, "pump_profile": pump_profile, "vent_profile": vent_profile, "pump_model": pump_model,
            "vent_model": vent_model, "vent": vent}


def panel(pk) -> str:
    return f"/security/models/{pk}/"


def tags_in(r) -> list:
    return [s.asset.tag for s in r.context["devices"]]


def fleet_models(client, query="") -> dict:
    """{(manufacturer, model): devices} of the Fleet's rows for `query`."""
    r = client.get("/security/" + query)
    assert r.status_code == 200
    return {(x["r"].device_model.manufacturer, x["r"].device_model.model): x["r"].devices for x in r.context["rows"]}


# --- Fleet ---------------------------------------------------------------------------------------------------------------------------

def test_the_fleet_shows_the_head_figures_and_one_row_per_model(client, signed_in, world, pump_model, vent_model):
    signed_in("technician")
    r = client.get("/security/")
    body = html(r)
    assert r.status_code == 200 and r.context["nav_active"] == "cybersecurity" and r.context["cyber_tab"] == "fleet"
    assert '<a href="/security/" class="active" aria-current="page">' in body  # the nav's Device security
    assert body.count(rules.HICP_SENTENCE) == 1 and 'class="hint cy-hicp"' in body  # once, small
    figures = {f["key"]: f for f in r.context["figures"]}
    assert [f["key"] for f in r.context["figures"]] == ["no_mds2", "updates_ended", "no_release", "not_segmented", "segment_not_recorded"]
    assert (figures["no_mds2"]["count"], figures["no_mds2"]["requested"]) == (1, 1)  # the pumps (requested); the vents' is on file
    assert figures["updates_ended"]["count"] == 1 and figures["no_release"]["count"] == 1  # P-3 past 9.19.2's; P-4 not recorded
    assert figures["not_segmented"]["count"] == 1 and figures["segment_not_recorded"]["count"] == 0
    for f in figures.values():
        assert escape(f["words"]) in body and escape(f["counts"]) in body
    assert "1 of them requested from the manufacturer" in body
    assert 'href="/security/?connected=either&amp;standing=updates_ended"' in body and 'href="/security/?connected=either&amp;norelease=1"' in body
    assert "7 devices counted: every device on site, ours and temporary (rentals, loaners, demo units), not retired." in body
    assert [x["r"].device_model.pk for x in r.context["rows"]] == [pump_model.pk, vent_model.pk, world["ster"].pk]
    pumps = r.context["rows"][0]
    assert (pumps["r"].devices, pumps["r"].connected, pumps["r"].updates_ended, pumps["r"].os_ended) == (5, 5, 1, 0)
    assert pumps["releases"] == [("9.19.2", 1), ("9.33.1", 3), ("Release not recorded", 1)]
    assert '<li class="muted">Release not recorded <b>1</b></li>' in body and "<li>9.33.1 <b>3</b></li>" in body
    assert (pumps["patient"], pumps["updates_by"], pumps["segmented"]) == ("Can store patient data", "Information technology",
                                                                           "Not on a separate segment")
    assert "Requested from the manufacturer" in body and "On file for 3.1" in body  # the vents run 3.2: the MDS2 is for another version
    assert f'<tr class="row" hx-get="{panel(pump_model.pk)}" hx-target="#drawer">' in body and f'<a class="tag" href="{panel(pump_model.pk)}">' in body
    assert 'id="cy-fleet"' in body and 'hx-trigger="cyber-changed from:body"' in body and 'hx-disinherit="hx-swap hx-target"' in body
    assert 'data-act="with-filters" data-base="/security/export.csv"' in body
    assert ">Fleet</a>" in body and ">Models</a>" in body and ">Changes for IT</a>" in body
    assert never_words(body) == []


def test_the_fleets_filters(client, signed_in, world):
    signed_in("technician")
    bd, ham, ste = ("BD", "Alaris 8015 PCU"), ("Hamilton Medical", "Hamilton-G5"), ("Steris", "Amsco 400")
    west, icu = world["west"].pk, world["dept"].pk
    assert fleet_models(client, "?whose=temporary") == {bd: 1}
    assert fleet_models(client, "?whose=ours") == {bd: 4, ham: 1, ste: 1}
    assert fleet_models(client, f"?dept={west}") == {bd: 3, ste: 1}
    assert fleet_models(client, f"?dept={icu}") == {bd: 2, ham: 1}
    assert fleet_models(client, "?category=Ventilators") == {ham: 1}
    assert fleet_models(client, "?connected=connected") == {ham: 1}  # recorded as connected here
    assert fleet_models(client, "?connected=can_connect") == {bd: 5}  # as disclosed, no fact of its own
    assert fleet_models(client, "?connected=either") == {bd: 5, ham: 1}
    assert fleet_models(client, "?connected=not_recorded") == {ste: 1}
    assert fleet_models(client, "?standing=updates_ended") == {bd: 1}
    assert fleet_models(client, "?standing=updates_end_announced") == {bd: 3}
    assert fleet_models(client, "?standing=security_not_recorded") == {bd: 1, ham: 1, ste: 1}
    assert fleet_models(client, "?norelease=1") == {bd: 1, ste: 1}
    assert fleet_models(client, "?connected=either&norelease=1&whose=ours") == {bd: 1}
    # the figures count the devices the filters keep
    figures = {f["key"]: f for f in client.get("/security/?whose=temporary").context["figures"]}
    assert figures["no_release"]["count"] == 0 and figures["no_release"]["url"] == "/security/?whose=temporary&connected=either&norelease=1"
    # unknown values are dropped, never a 500 (another facility's or an unknown department too)
    assert fleet_models(client, f"?whose=zzz&dept=nope&category=Nope&connected=x&standing=y&norelease=2&page=abc&dept={uuid.uuid4()}") == {
        bd: 5, ham: 1, ste: 1}
    assert client.get("/security/?category=%00").status_code < 500
    # the body alone for the filters' re-fetch, the filters' choices on the page
    r = client.get(f"/security/?dept={west}", **part("cy-fleet"))
    assert "<html" not in html(r) and 'id="cy-fleet"' in html(r) and r.context["rows"][0]["r"].devices == 3
    page = html(client.get("/security/"))
    assert f'<option value="{west}">4 West</option>' in page and '<option value="Sterilizers">Sterilizers</option>' in page
    assert 'name="norelease" value="1"' in page and never_words(page) == []


def test_the_fleet_csv_has_the_tables_columns(client, signed_in, world):
    signed_in("technician")
    rows = csv_rows(client.get("/security/export.csv"))
    assert rows[0] == views.FLEET_COLUMNS
    assert rows[1] == ["BD", "Alaris 8015 PCU", "Infusion pumps", "5", "5", "9.19.2: 1; 9.33.1: 3; Release not recorded: 1", "1", "0",
                       "Requested from the manufacturer", "Can store patient data", "Information technology", "Not on a separate segment"]
    assert rows[2][:2] == ["Hamilton Medical", "Hamilton-G5"] and rows[2][8] == "On file for 3.1" and len(rows) == 4
    assert csv_rows(client.get("/security/export.csv?whose=temporary"))[1][3] == "1" and len(csv_rows(client.get("/security/export.csv?whose=temporary"))) == 2
    # no column beyond what the screen shows: each one past the model's names is a heading of the table
    headings = [unescape(h) for h in re.findall(r"<th[^>]*>([^<]+)</th>", html(client.get("/security/")))]
    assert all(c in headings for c in views.FLEET_COLUMNS[3:])


# --- a model's panel -----------------------------------------------------------------------------------------------------------------

def test_a_models_panel_shows_its_profile_releases_and_devices(client, signed_in, world, pump_model):
    signed_in("technician")
    today = world["today"]
    r = client.get(panel(pump_model.pk), **HX)
    body = html(r)
    assert r.status_code == 200 and "<html" not in body
    assert "BD Alaris 8015 PCU" in body and "5 devices on site" in body and "MDS2: Requested from the manufacturer" in body
    # the profile's card: each group with its source and words, the MDS2 sentence, the facility group
    assert "Edit the profile" in body and escape(rules.MDS2_SENTENCE) in body
    assert "Information technology" in body and "Not on a separate segment" in body and "Who installs updates here?" in body
    assert f"Requested on</dt><dd>{rules.day_words(today - 10 * DAY)}" in body
    assert escape("What can it do with patient data, as the manufacturer discloses?") in body and "Can store patient data" in body
    # the releases: each with its two standing lines, never merged
    assert f"Security updates ended after {rules.day_words(today - 30 * DAY)}" in body
    assert f"Security updates end {rules.day_words(today + 400 * DAY)} (announced)" in body
    assert "1 device on site runs it" in body and "3 devices on site run it" in body and "Operating system support: not recorded" in body
    assert "From the model's disclosure, version not stated" in unescape(body)
    # the devices on site: tag (to the device's Security tab), location, release and since, connection, the two standing lines
    assert tags_in(r) == ["P-1", "P-2", "P-3", "P-4", "P-R"]
    assert 'href="/equipment/P-1/?tab=security" hx-get="/equipment/P-1/?tab=security" hx-target="#drawer">P-1</a>' in body
    assert "ICU · Room 4" in body and f"9.33.1, since {rules.day_words(today - 5 * DAY)}" in body and "Rental · Acme Rentals" in body
    assert '<span class="muted">Release not recorded</span> · Can connect (wireless), as disclosed' in body
    assert f'href="/security/devices.csv?model={pump_model.pk}"' in body
    # Edit's doors
    assert "Add release" in body and "Entered in error" in body and "Set devices to this release" in body and "Set devices to a release" in body
    assert f'hx-get="/security/models/{pump_model.pk}/set-release/?release={world["new"].pk}"' in body
    assert never_words(body) == []
    # opened directly: the Fleet with the panel open
    page = client.get(panel(pump_model.pk))
    assert page.status_code == 200 and 'class="drawer open"' in html(page) and page.context["cyber_tab"] == "fleet"
    assert never_words(html(page)) == []


def test_a_models_panel_carries_the_fleets_filters(client, signed_in, world, pump_model):
    signed_in("technician")
    west = world["west"].pk
    r = client.get(f"/security/?dept={west}")
    assert f'hx-get="{panel(pump_model.pk)}?dept={west}"' in html(r)
    r = client.get(f"{panel(pump_model.pk)}?dept={west}", **HX)
    body = html(r)
    assert tags_in(r) == ["P-3", "P-4", "P-R"] and "Devices on site <span>3 of 5</span>" in body
    assert "Showing the devices the Fleet's filters keep." in body and f'hx-get="{panel(pump_model.pk)}" hx-target="#drawer"' in body
    assert f"/security/devices.csv?model={pump_model.pk}&amp;dept={west}" in body
    assert f'/security/models/{pump_model.pk}/set-release/?dept={west}"' in body  # the modal comes back to the same panel
    assert tags_in(client.get(f"{panel(pump_model.pk)}?norelease=1", **HX)) == ["P-4"]
    assert tags_in(client.get(f"{panel(pump_model.pk)}?whose=temporary", **HX)) == ["P-R"]
    assert tags_in(client.get(f"{panel(pump_model.pk)}?standing=updates_ended", **HX)) == ["P-3"]
    assert "No device of this model on site matches the Fleet's filters." in html(client.get(f"{panel(pump_model.pk)}?connected=connected", **HX))


def test_the_ventilators_panel_shows_the_mds2_for_another_release(client, signed_in, world, vent_model):
    signed_in("technician")
    r = client.get(panel(vent_model.pk), **HX)
    body = html(r)
    assert "MDS2: On file for 3.1" in body
    card31 = next(x["card"] for x in r.context["releases"] if x["r"].version == "3.1")
    mds2 = next(g for g in card31["groups"] if g["key"] == "mds2")
    assert mds2["source"] == "From this release" and ("Form", "2019 form (HN 1-2019)") in mds2["rows"] and ("Document", "DEMO-MDS2-0001") in mds2["rows"]
    assert mds2["url"] == "https://manufacturer.example/mds2/demo"
    assert 'href="https://manufacturer.example/mds2/demo" target="_blank" rel="noopener noreferrer"' in body
    card32 = next(x["card"] for x in r.context["releases"] if x["r"].version == "3.2")
    sources = {g["key"]: g["source"] for g in card32["groups"]}
    assert sources == {"support": "Not recorded", "mds2": "Not recorded", "disclosure": "From the model's disclosure, version not stated"}
    assert "Connected (wired)" in body and "1 device on site runs it" in body and "0 devices on site run it" in body


def test_another_facilitys_or_an_unknown_model_is_a_404(client, signed_in, world, other_tenant):
    signed_in("technician")
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="Theirs", model="X1", description="Pump", category="Infusion pumps")
        their_release = release(theirs, "1.0")
    for url in (panel(theirs.pk), panel(uuid.uuid4()), f"/security/models/{theirs.pk}/releases/new/", f"/security/models/{theirs.pk}/set-release/",
                f"/security/releases/{their_release.pk}/edit/", f"/security/releases/{their_release.pk}/remove/",
                f"/security/devices.csv?model={theirs.pk}", "/security/devices.csv?model=nope", "/security/devices.csv"):
        assert client.get(url, **HX).status_code == 404, url
    assert client.post(f"/security/releases/{their_release.pk}/edit/", {"version": "2.0"}, **HX).status_code == 404
    assert client.post(f"/security/models/{theirs.pk}/set-release/", {"release": str(their_release.pk)}, **HX).status_code == 404
    with tenant_context(other_tenant):
        assert SoftwareRelease.objects.get(pk=their_release.pk).version == "1.0"


def test_the_devices_csv(client, signed_in, world, pump_model):
    signed_in("technician")
    today = world["today"]
    rows = csv_rows(client.get(f"/security/devices.csv?model={pump_model.pk}"))
    assert rows[0] == views.DEVICE_COLUMNS == ["Tag", "Department", "Room", "Release", "Running it since", "Connected here", "Security updates",
                                               "Operating system support"]
    assert [row[0] for row in rows[1:]] == ["P-1", "P-2", "P-3", "P-4", "P-R"]
    assert rows[1] == ["P-1", "ICU", "4", "9.33.1", (today - 5 * DAY).isoformat(), "Can connect (wireless), as disclosed",
                       f"Security updates end {rules.day_words(today + 400 * DAY)} (announced)", "Operating system support: not recorded"]
    assert rows[4][3:5] == ["Release not recorded", ""]
    assert [row[0] for row in csv_rows(client.get(f"/security/devices.csv?model={pump_model.pk}&dept={world['west'].pk}"))[1:]] == ["P-3", "P-4", "P-R"]


# --- Models -----------------------------------------------------------------------------------------------------------------------

def test_the_models_tab(client, signed_in, world, pump_model):
    signed_in("technician")
    zoll = DeviceModel.objects.create(manufacturer="Zoll", model="R Series", description="Defibrillator", category="Defibrillators")
    kept = DeviceModel.objects.create(manufacturer="Mindray", model="BeneView T5", description="Patient monitor", category="Monitors")
    release(kept, connects=Connects.BOTH)  # a profile, no device on site
    r = client.get("/security/models/")
    body = html(r)
    names = [x["dm"].manufacturer for x in r.context["rows"]]
    assert r.status_code == 200 and r.context["cyber_tab"] == "models" and names == ["BD", "Hamilton Medical", "Mindray", "Steris"]
    pumps = r.context["rows"][0]
    assert pumps["profile"] and pumps["versions"] == ["9.19.2", "9.33.1"] and pumps["mds2"] == "Requested from the manufacturer"
    assert [x["dm"].on_site for x in r.context["rows"]] == [5, 1, 0, 1]  # ours and temporary, never a retired one
    eq.set_status(world["p"][3], AssetStatus.RETIRED)
    assert client.get("/security/models/").context["rows"][0]["dm"].on_site == 4
    assert (pumps["connects"], pumps["patient"], pumps["updates_by"], pumps["segmented"]) == (
        "Wireless", "Can store patient data", "Information technology", "Not on a separate segment")
    assert "<th class=\"num\">Devices on site</th>" in body and f'hx-get="{panel(pump_model.pk)}"' in body
    assert "On file for 3.1" in body and "Wired and wireless" in body and never_words(body) == []
    assert not r.context["rows"][3]["profile"]  # the sterilizer: devices on site, nothing recorded
    # a search finds any of the facility's models, each word in the manufacturer or the model
    assert [x["dm"] for x in client.get("/security/models/?q=zoll").context["rows"]] == [zoll]
    assert [x["dm"] for x in client.get("/security/models/?q=bd+alaris").context["rows"]] == [pump_model]
    assert "No model matches" in html(client.get("/security/models/?q=nothing-like-it", **part("cy-models")))
    r = client.get("/security/models/?q=ham", **part("cy-models"))
    assert "<html" not in html(r) and 'id="cy-models"' in html(r) and 'hx-disinherit="hx-swap hx-target"' in html(r)
    assert client.get("/security/models/?q=%00&page=zz").status_code < 500 and client.get("/security/models/?page=zz").status_code == 200


# --- Changes for IT -------------------------------------------------------------------------------------------------------------------

def test_changes_for_it(client, signed_in, world):
    signed_in("technician")
    today = world["today"]
    eq.set_status(world["p"][1], AssetStatus.RETIRED)
    r = client.get("/security/changes/")
    body = html(r)
    assert r.status_code == 200 and r.context["days"] == 30 and r.context["cyber_tab"] == "changes"
    kinds = [(c.asset.tag, c.kind) for c in r.context["page"]]
    assert ("P-2", "retired") in kinds and ("P-R", "added") in kinds and ("CE-10001", "added") in kinds
    assert all(tag != "S-1" for tag, _ in kinds)  # the sterilizer: its model's connection is not recorded
    assert f"Retired {rules.day_words(today)}" in body and f"Added {rules.day_words(today)}" in body
    assert 'href="/equipment/P-R/?tab=security"' in body and "Rental · Acme Rentals" in body
    assert 'aria-current="true">30 days</a>' in body and 'hx-get="/security/changes/?days=7"' in body
    assert 'data-act="with-filters" data-base="/security/changes.csv"' in body and never_words(body) == []
    assert client.get("/security/changes/?days=7").context["days"] == 7 and client.get("/security/changes/?days=90").context["days"] == 90
    assert client.get("/security/changes/?days=31").context["days"] == 30 and client.get("/security/changes/?days=x").context["days"] == 30
    r = client.get("/security/changes/?days=7", **part("cy-changes"))
    assert "<html" not in html(r) and 'id="cy-changes"' in html(r)
    rows = csv_rows(client.get("/security/changes.csv?days=7"))
    assert rows[0] == views.CHANGE_COLUMNS == ["Day", "Tag", "Manufacturer", "Model", "Department", "Room", "What changed", "Connection"]
    p2 = next(row for row in rows[1:] if row[1] == "P-2" and row[6].startswith("Retired"))
    assert p2 == [today.isoformat(), "P-2", "BD", "Alaris 8015 PCU", "ICU", "", f"Retired {rules.day_words(today)}",
                  "Can connect (wireless), as disclosed"]
    assert len(rows) - 1 == len(kinds)
    headings = re.findall(r"<th>([^<]+)</th>", body)
    assert {"Day", "Tag", "Model", "Location", "What changed", "Connection"} <= set(headings)


# --- who sees and does what -------------------------------------------------------------------------------------------------------

def screen_urls(world):
    pm, rel = world["pump_model"].pk, world["new"].pk
    return [panel(pm), f"/security/models/{pm}/releases/new/", f"/security/models/{pm}/releases/new/?profile=1",
            f"/security/releases/{rel}/edit/", f"/security/releases/{rel}/remove/", f"/security/models/{pm}/set-release/",
            "/security/", "/security/models/", "/security/changes/", "/security/export.csv", f"/security/devices.csv?model={pm}",
            "/security/changes.csv"]


def every_url():
    """Every Device security URL (urls_cybersecurity), with dummy ids."""
    from apps.web import urls_cybersecurity

    out = []
    for p in urls_cybersecurity.urlpatterns:
        assert isinstance(p, URLPattern)
        kwargs = {name: (str(uuid.uuid4()) if "UUID" in type(conv).__name__ else "CE-10001") for name, conv in p.pattern.converters.items()}
        out.append(reverse(f"web:{p.name}", kwargs=kwargs))
    return out


def test_a_technician_sees_every_tab_and_edits(client, signed_in, world):
    signed_in("technician")
    for url in screen_urls(world):
        r = client.get(url, **(HX if "/releases/" in url or "set-release" in url else {}))
        assert r.status_code == 200, url
        if "csv" not in url:
            assert never_words(html(r)) == [], url


def test_the_analyst_is_refused_everywhere(client, signed_in, world):
    signed_in("analyst")  # Device security None by default
    for url in every_url() + screen_urls(world):
        assert client.get(url).status_code == 403, url
        assert client.get(url, **HX).status_code == 403, url
        assert client.post(url, {}, **HX).status_code == 403, url
    from apps.web.context_processors import nav_entries

    assert "cybersecurity" not in [row[0] for row in nav_entries(User.objects.get(username="analyst@riverside.example"))]


@pytest.mark.parametrize("scope,extra", [(DataScope.COMPANY, {"company": "BD"}), (DataScope.DEPARTMENT, {"department": "ICU"})])
def test_a_scoped_user_is_refused_everywhere_even_with_full_levels(client, tenant, world, scope, extra):
    custom_user(client, tenant, f"scoped-{scope}", {m: Level.FULL for m in Module.values}, scope=scope, **extra)
    for url in every_url() + screen_urls(world):
        assert client.get(url).status_code == 403, url
        assert client.get(url, **HX).status_code == 403, url
        assert client.post(url, {"release": str(world["new"].pk)}, **HX).status_code == 403, url
    assert not SoftwareRelease.objects.filter(removed_on__isnull=False).exists()


def test_device_security_view_without_equipment_view_sees_models_only(client, tenant, world, pump_model):
    custom_user(client, tenant, "it-reader", {Module.CYBERSECURITY: Level.VIEW})
    r = client.get("/security/")
    assert r.status_code == 302 and r["Location"] == "/security/models/"  # the nav's link opens Models
    assert client.get("/security/", **HX).status_code == 403
    r = client.get("/security/models/")
    body = html(r)
    assert r.status_code == 200 and ">Fleet</a>" not in body and ">Changes for IT</a>" not in body and ">Models</a>" in body
    assert "Devices on site" not in body and "BD" in body
    r = client.get(panel(pump_model.pk), **HX)
    body = html(r)
    assert r.status_code == 200 and "Information technology" in body and "9.33.1" in body
    assert r.context["devices"] == [] and "Devices on site" not in body and "P-1" not in body and "devices on site run" not in body
    assert "Add release" not in body and "Edit the profile" not in body and "Set devices" not in body
    page = client.get(panel(pump_model.pk))
    assert page.status_code == 200 and page.context["cyber_tab"] == "models" and 'class="drawer open"' in html(page)
    for url in ("/security/export.csv", f"/security/devices.csv?model={pump_model.pk}", "/security/changes/", "/security/changes.csv",
                f"/security/models/{pump_model.pk}/releases/new/", f"/security/models/{pump_model.pk}/set-release/"):
        assert client.get(url, **HX).status_code == 403, url


def test_device_security_edit_without_equipment_view_records_releases_but_sets_no_devices(client, tenant, world, pump_model):
    custom_user(client, tenant, "it-editor", {Module.CYBERSECURITY: Level.EDIT})
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", {"version": "10.0"}, **HX)
    assert toast_of(r) == "Version 10.0 added" and SoftwareRelease.objects.filter(version="10.0").exists()
    body = html(r)
    assert "Add release" in body and "Set devices" not in body and "P-1" not in body
    assert client.get(f"/security/models/{pump_model.pk}/set-release/", **HX).status_code == 403
    assert client.post(f"/security/models/{pump_model.pk}/set-release/", {"release": str(world["new"].pk)}, **HX).status_code == 403
    assert not DeviceSecurity.objects.filter(asset=world["p"][3]).exists()  # P-4 still has nothing recorded


def test_the_it_security_role_sees_and_records(client, tenant, world, pump_model):
    """README's IT security role: Device security Edit and Equipment View."""
    custom_user(client, tenant, "it-security", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    assert client.get("/security/").status_code == 200 and client.get("/security/changes/").status_code == 200
    r = client.post(f"/security/models/{pump_model.pk}/set-release/", {"release": str(world["new"].pk), "network": "keep"}, **HX)
    assert toast_of(r) == "2 devices of BD Alaris 8015 PCU set to 9.33.1 (3 already were)"


def test_a_viewer_gets_no_edit_doors(client, tenant, world, pump_model):
    custom_user(client, tenant, "viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    body = html(client.get(panel(pump_model.pk), **HX))
    assert "P-1" in body and "Add release" not in body and "Entered in error" not in body and "Set devices" not in body
    assert client.get(f"/security/models/{pump_model.pk}/releases/new/", **HX).status_code == 403
    assert client.post(f"/security/releases/{world['r31'].pk}/remove/", **HX).status_code == 403
    assert SoftwareRelease.objects.get(pk=world["r31"].pk).removed_on is None


# --- a release and the profile -----------------------------------------------------------------------------------------------------

def test_the_release_and_profile_forms_offer_their_own_fields(client, signed_in, world, pump_model):
    signed_in("technician")
    versioned = html(client.get(f"/security/models/{pump_model.pk}/releases/new/", **HX))
    assert 'name="version"' in versioned and 'name="updates_by"' not in versioned and 'name="segmented"' not in versioned
    assert "<legend>The manufacturer&#x27;s disclosure (MDS2)</legend>" in versioned and 'name="mds2_url"' in versioned
    assert escape("Can it connect to a network, as the manufacturer discloses?") in versioned and ' required' not in versioned
    assert '<option value="" selected>Not recorded</option>' in versioned and never_words(versioned) == []
    profile = html(client.get(f"/security/models/{world['ster'].pk}/releases/new/?profile=1", **HX))
    assert 'name="version"' not in profile and 'name="updates_by"' in profile and "Who installs updates here?" in profile
    assert "Record the profile" in profile and f'hx-post="/security/models/{world["ster"].pk}/releases/new/?profile=1"' in profile
    # a model with a profile already (a panel left open): the form edits it, and saves through Edit with the panel's filters
    again = html(client.get(f"/security/models/{pump_model.pk}/releases/new/?profile=1&dept={world['west'].pk}", **HX))
    assert "Edit the profile" in again and '<option value="it" selected>Information technology</option>' in again
    assert f'hx-post="/security/releases/{world["pump_profile"].pk}/edit/?dept={world["west"].pk}"' in again
    edit = html(client.get(f"/security/releases/{world['r31'].pk}/edit/", **HX))
    assert 'value="3.1"' in edit and 'value="DEMO-MDS2-0001"' in edit and 'value="https://manufacturer.example/mds2/demo"' in edit


def test_a_release_refused_keeps_what_was_typed(client, signed_in, world, pump_model):
    signed_in("technician")
    today = world["today"]
    post = {"version": "9.33.1", "os_family": "embedded", "os_name": "Demo OS 2", "support_checked_on": (today + DAY).isoformat(),
            "mds2": "on_file", "mds2_edition": "", "mds2_url": "http://manufacturer.example/x", "connects": "nope"}
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", post, **HX)
    body = html(r)
    found = errors(body)
    assert found["version"] == cyber.VERSION_ON_FILE.format(version="9.33.1")
    assert found["support_checked_on"] == "The day support was checked cannot be after today."
    assert found["mds2_edition"] == cyber.EDITION_NEEDED and found["mds2_url"] == cyber.URL_WORDS and found["connects"] == cyber.CHOICE_WORDS
    assert 'value="Demo OS 2"' in body and 'value="http://manufacturer.example/x"' in body and f'value="{(today + DAY).isoformat()}"' in body
    assert "autofocus" in re.search(r'<input[^>]*name="version"[^>]*>', body).group(0)  # the first field refused, focused
    assert "HX-Retarget" not in r and never_words(body) == []
    assert SoftwareRelease.objects.filter(device_model=pump_model).count() == 3
    # the text rules, and a facility answer on a versioned release (never offered, refused if sent)
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", {"version": "admin@device", "os_name": "a;b"}, **HX)
    found = errors(html(r))
    assert found["version"].startswith("Enter the version as the manufacturer writes it") and found["os_name"].startswith("Enter the operating")
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", {"version": "11.0", "mds2": "requested",
                                                                         "mds2_document": "DOC-1", "mds2_requested_on": today.isoformat()}, **HX)
    assert errors(html(r)) == {"mds2_document": cyber.ON_FILE_ONLY}
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", {"version": "11.0", "os_family": "none", "os_name": "Something"}, **HX)
    assert errors(html(r)) == {"os_name": cyber.NO_OS_NAME}


def test_adding_a_release_answers_with_the_panel(client, signed_in, world, pump_model):
    user = signed_in("technician")
    today = world["today"]
    r = client.post(f"/security/models/{pump_model.pk}/releases/new/", {
        "version": " V10.2.0 ", "os_family": "embedded", "security_updates_end_on": (today + 900 * DAY).isoformat(),
        "support_checked_on": today.isoformat(), "mds2": "on_file", "mds2_edition": "2019", "mds2_document": "DEMO-MDS2-0002",
        "mds2_url": "https://manufacturer.example/mds2/10", "patching": "manufacturer_only"}, **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and toast_of(r) == "Version V10.2.0 added"
    assert "cyber-changed" in triggers(r) and "modal-close" in json.loads(r["HX-Trigger-After-Settle"])
    added = SoftwareRelease.objects.get(device_model=pump_model, version_key="10.2.0")
    assert (added.mds2, added.mds2_edition, added.patching, added.support_checked_on) == ("on_file", "2019", "manufacturer_only", today)
    assert added.history.first().history_user == user
    body = html(r)
    assert "V10.2.0" in body and "BD Alaris 8015 PCU" in body and never_words(body + toast_of(r)) == []
    # the same version in another spelling is on file already
    assert errors(html(client.post(f"/security/models/{pump_model.pk}/releases/new/", {"version": "10.2.0"}, **HX)))["version"] == \
        cyber.VERSION_ON_FILE.format(version="V10.2.0")


def test_recording_and_editing_the_profile(client, signed_in, world):
    signed_in("technician")
    ster = world["ster"]
    r = client.post(f"/security/models/{ster.pk}/releases/new/?profile=1", {"connects": "none", "updates_by": "ce", "segmented": ""}, **HX)
    assert toast_of(r) == "Steris Amsco 400: profile recorded" and r["HX-Retarget"] == "#drawer"
    profile = SoftwareRelease.objects.get(device_model=ster, version_key="")
    assert (profile.connects, profile.updates_by) == ("none", "ce")
    assert "Edit the profile" in html(r) and "Clinical engineering" in html(r)
    # a second profile (a form left open): refused at the top, in the service's words
    r = client.post(f"/security/models/{ster.pk}/releases/new/?profile=1", {"connects": "wired"}, **HX)
    assert errors(html(r)) == {"": cyber.PROFILE_EXISTS}
    r = client.post(f"/security/releases/{profile.pk}/edit/", {"connects": "none", "updates_by": "department", "segmented": "none"}, **HX)
    assert toast_of(r) == "Steris Amsco 400: profile saved"
    profile.refresh_from_db()
    assert (profile.updates_by, profile.segmented, profile.version) == ("department", "none", "")
    assert client.get(f"/security/releases/{profile.pk}/remove/", **HX).context["blocker"] == cyber.PROFILE_KEPT
    r = client.post(f"/security/releases/{profile.pk}/remove/", **HX)
    assert errors(html(r)) == {"": cyber.PROFILE_KEPT} and SoftwareRelease.objects.get(pk=profile.pk).removed_on is None


def test_editing_a_release(client, signed_in, world):
    signed_in("technician")
    new, r31 = world["new"], world["r31"]
    # devices run 9.33.1: its version stays (the service's words, under the field, the typed text kept)
    r = client.post(f"/security/releases/{new.pk}/edit/", {"version": "9.33.2", "os_family": "embedded"}, **HX)
    assert errors(html(r)) == {"version": cyber.VERSION_NAMED} and 'value="9.33.2"' in html(r)
    r = client.post(f"/security/releases/{r31.pk}/edit/", {"version": "3.1.1", "mds2": "on_file", "mds2_edition": "2013",
                                                           "mds2_document": "DEMO-MDS2-0001"}, **HX)
    assert toast_of(r) == "Version 3.1.1 saved"
    r31.refresh_from_db()
    assert (r31.version, r31.mds2_edition, r31.mds2_url) == ("3.1.1", "2013", "")  # every field as typed: the link left blank is cleared
    assert "MDS2: On file for 3.1.1" in html(r)


def test_entered_in_error_and_restored(client, signed_in, world, vent_model):
    signed_in("technician")
    new, r31 = world["new"], world["r31"]
    r = client.get(f"/security/releases/{new.pk}/remove/", **HX)
    assert r.context["blocker"] == "3 devices run this release: set them to another first." and "Mark entered in error" not in html(r)
    r = client.post(f"/security/releases/{new.pk}/remove/", **HX)
    assert errors(html(r)) == {"": "3 devices run this release: set them to another first."}
    assert SoftwareRelease.objects.get(pk=new.pk).removed_on is None
    r = client.get(f"/security/releases/{r31.pk}/remove/", **HX)
    assert r.context["blocker"] == "" and "Adding version 3.1 again restores it." in html(r) and never_words(html(r)) == []
    r = client.post(f"/security/releases/{r31.pk}/remove/", **HX)
    assert toast_of(r) == "Version 3.1 marked entered in error" and r["HX-Retarget"] == "#drawer"
    assert SoftwareRelease.objects.get(pk=r31.pk).removed_on == world["today"]
    assert "3.1" not in [x["r"].version for x in r.context["releases"]] and "MDS2: Not recorded" in html(r)
    assert client.get(f"/security/releases/{r31.pk}/edit/", **HX).status_code == 404  # hidden, never named
    assert client.post(f"/security/releases/{r31.pk}/remove/", **HX).status_code == 404
    r = client.post(f"/security/models/{vent_model.pk}/releases/new/", {"version": "3.1", "mds2": "on_file", "mds2_edition": "2019"}, **HX)
    assert toast_of(r) == "Version 3.1 restored (it was entered in error)"
    assert SoftwareRelease.objects.get(pk=r31.pk).removed_on is None and SoftwareRelease.objects.filter(device_model=vent_model).count() == 3


# --- Set devices to this release ---------------------------------------------------------------------------------------------------

def test_set_devices_to_this_release(client, signed_in, world, pump_model):
    user = signed_in("technician")
    today, new, old = world["today"], world["new"], world["old"]
    url = f"/security/models/{pump_model.pk}/set-release/"
    r = client.get(f"{url}?release={new.pk}", **HX)
    body = html(r)
    assert f'<option value="{new.pk}" selected>9.33.1</option>' in body and "5 devices of this model on site, ours and temporary." in body
    assert '<option value="keep" selected>' in body and f'<option value="{world["west"].pk}">4 West only</option>' in body
    assert never_words(body) == []
    r = client.post(url, {"release": str(new.pk), "department": "", "release_since": "", "network": "keep"}, **HX)
    assert toast_of(r) == "2 devices of BD Alaris 8015 PCU set to 9.33.1 (3 already were)" and r["HX-Retarget"] == "#drawer"
    assert "cyber-changed" in triggers(r) and never_words(html(r) + toast_of(r)) == []
    p3, p4 = (DeviceSecurity.objects.get(asset=a) for a in world["p"][2:])
    assert (p3.release, p3.release_since, p3.checked_by, p4.release, p4.release_since) == (new, today, user, new, today)
    assert DeviceSecurity.objects.get(asset=world["p"][0]).release_since == today - 5 * DAY  # already on it: left as it was
    # one department, a network fact, a day
    r = client.post(url, {"release": str(old.pk), "department": str(world["dept"].pk), "release_since": (today - 2 * DAY).isoformat(),
                          "network": "wireless"}, **HX)
    assert toast_of(r) == "2 devices of BD Alaris 8015 PCU set to 9.19.2"
    p1 = DeviceSecurity.objects.get(asset=world["p"][0])
    assert (p1.release, p1.release_since, p1.network) == (old, today - 2 * DAY, Network.WIRELESS)
    assert DeviceSecurity.objects.get(asset=world["rental"]).release == new
    r = client.post(url, {"release": str(old.pk), "department": str(world["dept"].pk), "release_since": (today - 2 * DAY).isoformat(),
                          "network": "keep"}, **HX)
    assert toast_of(r) == "Nothing changed: all 2 devices were already on 9.19.2 as chosen"


def test_set_devices_refusals_come_back_under_their_fields(client, signed_in, world, pump_model, vent_model, monkeypatch):
    signed_in("technician")
    today = world["today"]
    url = f"/security/models/{pump_model.pk}/set-release/"
    r = client.post(url, {"release": "", "network": "keep"}, **HX)
    assert errors(html(r)) == {"release": cyber.CHOOSE_MODEL_VERSION} and "HX-Retarget" not in r
    r = client.post(url, {"release": str(world["r32"].pk), "network": "keep"}, **HX)  # another model's release
    assert errors(html(r)) == {"release": cyber.CHOOSE_MODEL_VERSION}
    r = client.post(url, {"release": str(world["new"].pk), "release_since": (today + DAY).isoformat(), "network": "bogus"}, **HX)
    found = errors(html(r))
    assert found == {"release_since": "Running it since cannot be after today.", "network": cyber.NETWORK_WORDS}
    assert f'value="{(today + DAY).isoformat()}"' in html(r) and f'<option value="{world["new"].pk}" selected>' in html(r)
    r = client.post(url, {"release": str(world["new"].pk), "department": str(uuid.uuid4()), "network": "keep"}, **HX)
    assert errors(html(r)) == {"department": "Choose every department, or one from the list."}
    monkeypatch.setattr(cyber, "BULK_MAX", 2)
    r = client.post(url, {"release": str(world["new"].pk), "department": "", "network": "keep"}, **HX)
    assert errors(html(r)) == {"department": cyber.TOO_MANY}
    assert DeviceSecurity.objects.filter(release=world["new"]).count() == 3  # nothing saved
    # a model with no release recorded: the modal says so
    assert "No release of this model is recorded yet." in html(client.get(f"/security/models/{world['ster'].pk}/set-release/", **HX))
    # a retired device is not on site: never set
    eq.set_status(world["p"][3], AssetStatus.RETIRED)
    monkeypatch.setattr(cyber, "BULK_MAX", 500)
    r = client.post(url, {"release": str(world["new"].pk), "network": "keep"}, **HX)
    assert toast_of(r) == "1 device of BD Alaris 8015 PCU set to 9.33.1 (3 already were)"
    assert not DeviceSecurity.objects.filter(asset=world["p"][3]).exists()


# --- fixed queries, phone width, the stylesheet -------------------------------------------------------------------------------------

def test_the_fleet_and_a_panel_read_in_a_fixed_number_of_queries(client, signed_in, world, pump_model):
    signed_in("technician")

    def count(url, **headers):
        with CaptureQueriesContext(connection) as q:
            assert client.get(url, **headers).status_code == 200
        return len(q.captured_queries)

    before = (count("/security/"), count(panel(pump_model.pk), **HX), count("/security/?norelease=1"), count("/security/changes/"))
    monitor = DeviceModel.objects.create(manufacturer="Mindray", model="BeneView T5", description="Patient monitor", category="Monitors")
    release(monitor, connects=Connects.BOTH)
    m1 = release(monitor, "2.0")
    for i in range(12):
        a = Asset.objects.create(tag=f"M-{i}", device_model=monitor, department=world["west"])
        record(a, release=m1 if i % 2 else None)
        b = Asset.objects.create(tag=f"PX-{i}", device_model=pump_model, department=world["dept"])
        record(b, release=world["new"], network=Network.WIRELESS)
    after = (count("/security/"), count(panel(pump_model.pk), **HX), count("/security/?norelease=1"), count("/security/changes/"))
    assert after == before


def test_phone_width_tables_scroll_in_their_frame_and_the_css_block(client, signed_in, world, pump_model):
    signed_in("technician")
    for url in ("/security/", "/security/models/", "/security/changes/"):
        body = html(client.get(url))
        assert body.count("<table") == body.count('<div class="tbl-wrap">') >= 1, url
    assert "<table" not in html(client.get(panel(pump_model.pk), **HX))  # the panel's devices are a list: nothing to scroll sideways
    css = Path("apps/web/static/web/cadence.css").read_text()
    start, end, prints = css.index("/* Slice 37 A"), css.index("/* Slice 37 A: end */"), css.index("/* Slice 11: printing app pages */")
    block = css[start:end]
    assert start < end < prints and block.count("{") == block.count("}") and "@media (max-width:760px)" in block


# --- PostgreSQL: the screens under the policies --------------------------------------------------------------------------------

@needs_postgres
def test_the_screens_and_their_writes_under_row_level_security(client, signed_in, world, tenant, pump_model, vent_model):
    signed_in("technician")
    as_app_role()
    assert "BD Alaris 8015 PCU" in html(client.get("/security/"))
    assert "Hamilton Medical" in html(client.get("/security/models/")) and client.get("/security/changes/").status_code == 200
    assert tags_in(client.get(panel(pump_model.pk), **HX)) == ["P-1", "P-2", "P-3", "P-4", "P-R"]
    assert len(csv_rows(client.get("/security/export.csv"))) == 4 and len(csv_rows(client.get(f"/security/devices.csv?model={pump_model.pk}"))) == 6
    assert len(csv_rows(client.get("/security/changes.csv"))) > 1
    r = client.post(f"/security/models/{vent_model.pk}/releases/new/", {"version": "3.3", "os_family": "linux"}, **HX)
    assert toast_of(r) == "Version 3.3 added"
    r = client.post(f"/security/models/{pump_model.pk}/set-release/", {"release": str(world["new"].pk), "network": "keep"}, **HX)
    assert toast_of(r) == "2 devices of BD Alaris 8015 PCU set to 9.33.1 (3 already were)"
    with tenant_context(tenant):
        assert DeviceSecurity.objects.filter(release=world["new"]).count() == 5
