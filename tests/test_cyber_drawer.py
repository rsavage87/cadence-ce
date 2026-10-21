"""
Slice 37 wave 2 B: the device drawer's Security tab (apps/web/asset_cyber.py, web/_asset_cyber_tab.html) and Record facts
(apps/web/views_cyber_facts.py, web/_asset_cyber_facts.html).

The tab: offered before History to Device security View with Equipment View, never to a scoped user or a role without both (?tab=security
then reads the Overview); the device's release and since, connected here (its own fact, else "as disclosed"), the two standing lines,
checked on and by; the release card group by group with each group's source, the MDS2's answers by their questions, the MDS2 sentence,
the MDS2 on file for another release; its own history (the device_security area) with Show older through the Record facts URL; a retired
device keeps its facts with no Record facts. Record facts: Device security Edit and Equipment View; the versions of the device's model
(never one entered in error, never another model's); refusals under their fields with what was typed kept; saved, the drawer on the
Security tab, a toast, the events; another facility's device a 404 and its release refused; opened directly, the drawer's page.
"""
import json
import re
from datetime import timedelta

import pytest
from cyber_fixtures import release
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import rules, services
from apps.cybersecurity.models import (
    Connects,
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
    UpdatesBy,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "drawer"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}


# Slice 39 added "overdue" to rules.NEVER_WORDS (the register's voice). The device drawer around the Security tab carries Equipment's own
# PM wording (apps.equipment.services: Mark missing's confirm), which is not the register's: it is left out of the scan.
EQUIPMENT_PM_WORDS = "It counts as overdue for PM until it is found."


def words_ok(text: str) -> list:
    """The never-words (rules.NEVER_WORDS) found in `text`, as whole words in any letter case."""
    text = text.replace(EQUIPMENT_PM_WORDS, "")
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def html(r) -> str:
    return r.content.decode()


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def errors(body: str) -> list[str]:
    return re.findall(r'<span class="err"[^>]*>([^<]*)</span>', body)


def tab(client, asset, **extra):
    return client.get(f"/equipment/{asset.tag}/?tab=security", **{**HX, **extra})


def facts_url(asset) -> str:
    return f"/security/devices/{asset.tag}/facts/"


def post(client, asset, data):
    return client.post(facts_url(asset) + "?tab=security", data, **MODAL)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    def _as(slug):
        user = make_user(slug)
        client.force_login(user)
        return user

    return _as


def custom_user(client, tenant, slug, levels, scope="", **extra):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)
    client.force_login(user)
    return user


@pytest.fixture
def world(ctx, dept, pump_model, pump, today):
    """The pump model's profile (wireless, can store patient data, its MDS2 requested, updates by CE, not segmented) and two versions,
    9.19.2 (its MDS2 on file, security updates ended) and 9.33.1 (embedded OS, its own support group), and a version entered in error;
    CE-10002 on 9.33.1, connected wireless here."""
    profile = release(pump_model, connects=Connects.WIRELESS, patient_data=PatientData.STORES, mds2=Mds2Status.REQUESTED,
                      mds2_requested_on=today - 30 * DAY, updates_by=UpdatesBy.CE, segmented=Segmented.NONE, remote_service=Disclosed.YES,
                      patching=Patching.MANUFACTURER, sbom=Sbom.ON_REQUEST)
    old = release(pump_model, "9.19.2", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001",
                  mds2_released_on=today - 400 * DAY, mds2_url="https://example.com/mds2", security_updates_end_on=today - 10 * DAY)
    new = release(pump_model, "9.33.1", os_family=OsFamily.EMBEDDED, os_name="Demo embedded OS 7", os_support_ends_on=today - 5 * DAY,
                  security_updates_end_on=today + 400 * DAY, support_checked_on=today - 2 * DAY)
    gone = release(pump_model, "9.40", removed_on=today)
    services.record_facts(pump, by=None, release=new, release_since=today - 20 * DAY, network=Network.WIRELESS)
    return {"profile": profile, "old": old, "new": new, "gone": gone, "pump": pump}


# --- the tab ------------------------------------------------------------------------------------------------------------------

def test_the_tab_shows_the_device_facts_and_standing_in_the_rules_words(client, signed_in, world, today):
    signed_in("technician")
    r = tab(client, world["pump"])
    body = html(r)
    assert r.status_code == 200 and r.context["tab"] == "security" and r.context["tabs"][-2:] == ["security", "history"]
    assert 'hx-get="/equipment/CE-10002/?tab=security" hx-target="#drawer">Security</button>' in body
    s = rules.device_standing(world["pump"], today)
    assert f"9.33.1, since {rules.day_words(today - 20 * DAY)}" in body
    assert "Connected (wireless)" in body
    assert f"Security updates end {rules.day_words(today + 400 * DAY)} (announced)" in body
    assert f"The OS vendor&#x27;s published support for Demo embedded OS 7 ended after {rules.day_words(today - 5 * DAY)}" in body
    assert s.security.words in html(r).replace("&#x27;", "'") and s.os.words in body.replace("&#x27;", "'")
    assert f"<dt>Checked</dt><dd>{rules.day_words(today)}</dd>" in body  # recorded by the system (no one named), today
    # the release card: each group with its source, the MDS2 by the profile (requested), the disclosure by its questions
    assert "Support and security updates <span>this release</span>" in body
    assert "The manufacturer&#x27;s disclosure (MDS2) <span>the model&#x27;s disclosure, version not stated</span>" in body
    assert "What the manufacturer discloses <span>the model&#x27;s disclosure, version not stated</span>" in body
    assert "This facility&#x27;s arrangement <span>the model&#x27;s, for every version</span>" in body
    assert "Requested from the manufacturer" in body and "The model&#x27;s MDS2 on file for" in body and ">9.19.2<" in body
    assert "What can it do with patient data, as the manufacturer discloses?" in body and "Can store patient data" in body  # rules.QUESTIONS
    assert "Who may install its software updates, as the manufacturer discloses?" in body and "The manufacturer or its service only" in body
    assert "Clinical engineering" in body and "Not on a separate segment" in body
    assert "Embedded or real-time: Demo embedded OS 7" in body
    assert "The MDS2 describes what the device can do; whether you may change it is the manufacturer&#x27;s documentation and your contract." in body
    assert 'hx-get="/security/devices/CE-10002/facts/" hx-target="#modal-card">Record facts</button>' in body
    assert f'href="/security/models/{world["pump"].device_model_id}/"' in body
    # its own history: the facts recorded, with the reason
    assert "Facts recorded: release set to 9.33.1" in body and "Connected (wireless)" in body
    assert not words_ok(body)


def test_the_tab_reads_the_mds2_of_the_release_and_its_link(client, signed_in, world, today):
    services.record_facts(world["pump"], by=None, release=world["old"])
    signed_in("technician")
    body = html(tab(client, world["pump"]))
    assert "The manufacturer&#x27;s disclosure (MDS2) <span>this release</span>" in body and "2019 form (HN 1-2019)" in body
    assert "DEMO-MDS2-0001" in body and 'href="https://example.com/mds2" target="_blank" rel="noopener noreferrer"' in body
    assert "MDS2 on file for" not in body
    assert f"Security updates ended after {rules.day_words(today - 10 * DAY)}" in body
    assert "Support and security updates <span>this release</span>" in body
    assert "Operating system support: not recorded" in body  # 9.19.2 has a support group of its own (its security date), no OS day


def test_a_device_with_no_release_reads_the_profile_as_the_model_s(client, signed_in, world, dept, pump_model):
    other = Asset.objects.create(tag="CE-20001", device_model=pump_model, department=dept)
    signed_in("technician")
    body = html(tab(client, other))
    assert "Release not recorded" in body and "Can connect (wireless), as disclosed" in body
    assert "Support and security updates <span>not recorded</span>" in body and "Security updates end: not recorded" in body
    assert "What the manufacturer discloses <span>the model&#x27;s disclosure, version not stated</span>" in body
    assert "Not checked yet" in body and "No facts recorded yet." in body
    assert not words_ok(body)


def test_a_model_with_nothing_on_file(client, signed_in, vent):
    signed_in("technician")
    body = html(tab(client, vent))
    assert "Release not recorded" in body and "Connection not recorded" in body and "Operating system support: not recorded" in body
    assert body.count("<span>not recorded</span>") == 4 and "No facts recorded yet." in body
    assert not words_ok(body)


def test_the_model_s_support_reads_when_the_release_has_no_date(client, signed_in, vent, vent_model, today):
    vent_model.support_ends_on = today - 40 * DAY
    vent_model.save()
    services.record_facts(vent, by=None, release=release(vent_model, "3.1", os_family=OsFamily.NONE))
    signed_in("technician")
    body = html(tab(client, vent))
    assert f"Manufacturer support ended {eq._day(today - 40 * DAY)} (the model&#x27;s)" in body and "No operating system" in body


@pytest.mark.parametrize("slug", ["director", "manager", "technician"])
def test_device_security_readers_get_the_tab(client, signed_in, world, slug):
    signed_in(slug)
    assert "security" in tab(client, world["pump"]).context["tabs"]


def test_the_tab_is_never_offered_without_both_levels_or_to_a_scoped_user(client, signed_in, tenant, world):
    signed_in("analyst")  # Equipment View, Device security None
    r = tab(client, world["pump"])
    assert r.status_code == 200 and "security" not in r.context["tabs"] and r.context["tab"] == "overview"
    assert ">Security</button>" not in html(r) and "9.33.1" not in html(r)
    custom_user(client, tenant, "itsec", {Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.NONE})
    assert tab(client, world["pump"]).status_code == 403  # the drawer itself is Equipment View's
    for scope, extra in ((DataScope.COMPANY, {"company": "BD"}), (DataScope.DEPARTMENT, {"department": "ICU"})):
        custom_user(client, tenant, f"scoped{scope}", {m: Level.FULL for m in Module.values}, scope=scope, **extra)
        r = tab(client, world["pump"])
        if r.status_code == 200:  # in their share: the drawer, never the tab
            assert "security" not in r.context["tabs"] and r.context["tab"] == "overview" and "9.33.1" not in html(r)
        else:
            assert r.status_code == 404
        assert client.get(facts_url(world["pump"]), **MODAL).status_code == 403
        assert client.get(facts_url(world["pump"]) + "?history=1", **{**HX, "HTTP_HX_TARGET": "hist-more"}).status_code == 403


def test_a_reader_without_edit_sees_no_record_facts_and_is_refused_it(client, tenant, world):
    custom_user(client, tenant, "itread", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    body = html(tab(client, world["pump"]))
    assert "9.33.1, since" in body and "Record facts" not in body
    assert client.get(facts_url(world["pump"]), **MODAL).status_code == 403
    assert post(client, world["pump"], {"release": "", "network": "not_connected"}).status_code == 403
    assert DeviceSecurity.objects.get(asset=world["pump"]).network == Network.WIRELESS


def test_the_it_security_role_records_facts_without_equipment_edit(client, tenant, world, today):
    user = custom_user(client, tenant, "itsec", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    r = post(client, world["pump"], {"release": str(world["new"].pk), "release_since": "", "network": "not_connected", "checked_on": ""})
    assert r.status_code == 200, html(r)
    sec = DeviceSecurity.objects.get(asset=world["pump"])
    assert (sec.network, sec.checked_by, sec.checked_on) == (Network.NOT_CONNECTED, user, today)


def test_a_retired_device_keeps_its_facts_with_no_record_facts(client, signed_in, world, today):
    eq.set_status(world["pump"], AssetStatus.RETIRED, by=None)
    signed_in("technician")
    body = html(tab(client, world["pump"]))
    assert "Retired: its facts stay as they were." in body and "9.33.1, since" in body and "Record facts" not in body
    r = post(client, world["pump"], {"release": str(world["new"].pk), "network": "not_connected"})
    assert r.status_code == 200 and "CE-10002 is retired: its facts stay as they were." in html(r)
    assert DeviceSecurity.objects.get(asset=world["pump"]).network == Network.WIRELESS


# --- Record facts ----------------------------------------------------------------------------------------------------------------

def test_the_modal_offers_the_model_s_versions_and_today(client, signed_in, world, today):
    signed_in("technician")
    r = client.get(facts_url(world["pump"]), **MODAL)
    body = html(r)
    assert r.status_code == 200 and "CE-10002: record facts" in body
    assert f'<option value="{world["new"].pk}" selected>9.33.1</option>' in body and f'<option value="{world["old"].pk}">9.19.2</option>' in body
    assert "9.40" not in body and '<option value="">Release not recorded</option>' in body  # never one entered in error
    assert '<option value="" >' not in body and "As the model discloses (Can connect (wireless), as disclosed)" in body
    assert '<option value="wireless" selected>Connected (wireless)</option>' in body
    assert f'name="checked_on" value="{today.isoformat()}"' in body and 'type="date"' in body
    assert 'hx-post="/security/devices/CE-10002/facts/?tab=security"' in body
    assert not words_ok(body)


def test_a_model_with_no_versions_says_where_to_add_one(client, signed_in, vent):
    signed_in("technician")
    body = html(client.get(facts_url(vent), **MODAL))
    assert "No version of Hamilton Medical Hamilton-G5 is on file yet: add one on the Device security screen&#x27;s Models tab." in body
    assert body.count("<option") == 1 + 1 + len(Network.choices)  # Release not recorded; as disclosed and the network choices


def test_saving_shows_the_tab_again_with_a_toast(client, signed_in, world, today):
    user = signed_in("technician")
    r = post(client, world["pump"], {"release": str(world["old"].pk), "release_since": (today - 3 * DAY).isoformat(),
                                     "network": "wired", "checked_on": (today - DAY).isoformat()})
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    t = triggers(r)
    assert t["toast"]["value"] == "Facts recorded for CE-10002; release set to 9.19.2" and "cyber-changed" in t and "modal-close" in t
    body = html(r)
    assert r.context["tab"] == "security" and f"9.19.2, since {rules.day_words(today - 3 * DAY)}" in body and "Connected (wired)" in body
    assert f"<dt>Checked</dt><dd>{rules.day_words(today - DAY)} by Technician User</dd>" in body
    sec = DeviceSecurity.objects.get(asset=world["pump"])
    assert (sec.release, sec.release_since, sec.network, sec.checked_on, sec.checked_by) == (world["old"], today - 3 * DAY, Network.WIRED,
                                                                                               today - DAY, user)
    assert sec.history.order_by("-history_date").first().history_change_reason == "Facts recorded: release set to 9.19.2"
    r = post(client, world["pump"], {"release": str(world["old"].pk), "network": "wired"})
    assert triggers(r)["toast"]["value"] == "Facts recorded for CE-10002"
    r = post(client, world["pump"], {"release": "", "network": ""})
    assert triggers(r)["toast"]["value"] == "Facts recorded for CE-10002; release cleared"
    sec.refresh_from_db()
    assert (sec.release, sec.release_since, sec.network) == (None, None, "")
    assert "Release not recorded" in html(r) and "Can connect (wireless), as disclosed" in html(r)


def test_a_new_release_runs_since_today_by_default(client, signed_in, world, today):
    signed_in("technician")
    post(client, world["pump"], {"release": str(world["old"].pk), "release_since": "", "network": "wireless", "checked_on": ""})
    sec = DeviceSecurity.objects.get(asset=world["pump"])
    assert (sec.release_since, sec.checked_on) == (today, today)


def test_refusals_come_back_under_their_fields_with_what_was_typed(client, signed_in, world, today, vent_model):
    signed_in("technician")
    vent_release = release(vent_model, "3.1")
    later = (today + DAY).isoformat()
    r = post(client, world["pump"], {"release": str(vent_release.pk), "release_since": later, "network": "bluetooth", "checked_on": later})
    body = html(r)
    assert r.status_code == 200 and "HX-Retarget" not in r
    # the days and the network first, all at once, each under its field (the release is checked once they pass)
    assert errors(body) == ["Running it since cannot be after today.", services.NETWORK_WORDS, "The day checked cannot be after today."]
    assert f'value="{later}"' in body and body.count(f'value="{later}"') == 2 and 'autofocus' in body
    r = post(client, world["pump"], {"release": str(vent_release.pk), "network": "wired"})
    assert r.context["form"].errors == {"release": [services.CHOOSE_VERSION]}
    assert f'<option value="{world["new"].pk}">9.33.1</option>' in html(r)  # the device's own versions still offered
    r = post(client, world["pump"], {"release": "", "release_since": (today - DAY).isoformat()})
    assert r.context["form"].errors == {"release_since": [services.NO_RELEASE_SINCE]}
    r = post(client, world["pump"], {"release": "not-an-id", "release_since": "13/45/2026", "checked_on": "1900-01-01"})
    assert set(r.context["form"].errors) == {"release_since", "checked_on"} and r.status_code == 200
    r = post(client, world["pump"], {"release": "not-an-id"})
    assert r.context["form"].errors == {"release": [services.CHOOSE_VERSION]}
    r = post(client, world["pump"], {"release": str(world["gone"].pk)})  # entered in error: never named
    assert r.context["form"].errors == {"release": [services.CHOOSE_VERSION]}
    sec = DeviceSecurity.objects.get(asset=world["pump"])
    assert (sec.release, sec.network) == (world["new"], Network.WIRELESS)
    assert not words_ok(html(r))


def test_another_facility_s_device_and_release_are_refused(client, signed_in, world, other_tenant):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        their_release = release(model, "9.33.1")
    signed_in("technician")
    assert client.get(facts_url(theirs), **MODAL).status_code == 404
    assert post(client, theirs, {"release": ""}).status_code == 404
    assert client.get(f"/equipment/{theirs.tag}/?tab=security", **HX).status_code == 404
    r = post(client, world["pump"], {"release": str(their_release.pk)})
    assert r.context["form"].errors == {"release": [services.CHOOSE_VERSION]}
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["new"]
    with tenant_context(other_tenant):
        assert not DeviceSecurity.objects.exists()


def test_opened_directly_it_shows_the_drawer_s_page(client, signed_in, world):
    signed_in("technician")
    r = client.get(facts_url(world["pump"]))
    assert r.status_code == 302 and r["Location"] == "/equipment/CE-10002/?tab=security"
    r = client.get("/equipment/CE-10002/?tab=security")
    body = html(r)
    assert r.status_code == 200 and r.context["drawer_template"] == "web/_asset_drawer.html" and "9.33.1, since" in body
    assert client.get(facts_url(world["pump"]) + "?history=1").status_code == 302


def test_a_model_change_reads_release_not_recorded(client, signed_in, world, vent_model):
    signed_in("technician")
    eq.update_asset(world["pump"], by=None, device_model=vent_model)
    body = html(tab(client, Asset.objects.get(pk=world["pump"].pk)))
    assert "Release not recorded" in body and "Device model changed" in body and "Connected (wireless)" in body


# --- the history pages -----------------------------------------------------------------------------------------------------------

def test_the_history_pages_through_the_record_facts_url(client, signed_in, tenant, world, today):
    pump = world["pump"]
    for i in range(55):  # 56 entries with the world's first: more than a page
        services.record_facts(pump, by=None, network=Network.WIRELESS if i % 2 else Network.WIRED)
    signed_in("technician")
    r = tab(client, pump)
    hist = r.context["cyber"]["hist"]
    assert len(hist["rows"]) == 50 and hist["more_url"].startswith("/security/devices/CE-10002/facts/?history=1&older=")
    assert f'hx-get="{hist["more_url"].replace("&", "&amp;")}" hx-target="#hist-more"' in html(r)
    more = client.get(hist["more_url"], **{**HX, "HTTP_HX_TARGET": "hist-more"})
    assert more.status_code == 200 and len(more.context["hist"]["rows"]) == 6 and more.context["hist"]["more_url"] is None
    assert "<html" not in html(more) and "Set with" not in html(more) and "release set to 9.33.1" in html(more)
    custom_user(client, tenant, "itread", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    assert client.get(hist["more_url"], **{**HX, "HTTP_HX_TARGET": "hist-more"}).status_code == 200  # View pages the history
    custom_user(client, tenant, "noequip", {Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.NONE})
    assert client.get(hist["more_url"], **{**HX, "HTTP_HX_TARGET": "hist-more"}).status_code == 403


def test_the_tab_reads_a_fixed_number_of_queries(client, signed_in, world, dept, pump_model):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    signed_in("technician")

    def counted():
        with CaptureQueriesContext(connection) as q:
            assert tab(client, world["pump"]).status_code == 200
        return len(q)

    services.record_facts(world["pump"], by=None, network=Network.WIRED)  # past its first history row (history reads the one before)
    first = counted()
    for i in range(5):
        services.record_facts(world["pump"], by=None, network=Network.WIRELESS if i % 2 else Network.BOTH)
        release(pump_model, f"10.{i}")
        Asset.objects.create(tag=f"CE-3000{i}", device_model=pump_model, department=dept)
    assert counted() == first
