"""
Slice 37's demo: what the CE manager recorded in the device security register (seed_demo's _device_security, after every draw, through
apps.cybersecurity.services), so the Device security screen, the device drawer's Security tab, and the API have something real: the
Fleet tab's head figures each count something, the patient monitors' MDS2 is on file for an older release than most of them run, and
Changes for IT lists the devices added, retired, or returned lately (the fleet came into Cadence the day before the demo's work began:
_fleet_in_cadence), never the whole fleet. Nothing the seed records names a real product's software, operating system, or disclosure,
and none of the words the register never says is on a seeded page. On PostgreSQL the register reads the same as the runtime role and
the screens answer.

Wave 2 builds the screens (A), the drawer's tab and the API (B) beside this seed: a page whose view is still the scaffold's stub (or an
API route not registered yet, or a drawer without the Security tab) is pending, and a test with pages pending is an expected failure
once everything else in it passed; it runs in full the moment they are merged.
"""
import importlib.util
import re
from datetime import timedelta
from html import unescape
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.urls import Resolver404, resolve
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import User
from apps.cybersecurity import rules
from apps.cybersecurity.models import DeviceSecurity, Mds2Status, Network, SoftwareRelease
from apps.demo.management.commands import seed_demo
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import RepairCheck, WorkOrder

PUMP, MONITOR, VENT, BED, C_ARM, STERILIZER, DIALYSIS = ("Alaris 8015 PCU", "IntelliVue MX750", "Hamilton-G5", "Centrella", "Cios Spin",
                                                        "Amsco 400", "2008T BlueStar")
DAY = timedelta(days=1)


@pytest.fixture
def seeded(db):
    call_command("seed_demo", stdout=StringIO())
    return Tenant.objects.get(slug="riverside")


def never_said(text: str) -> list:
    """The words of rules.NEVER_WORDS in `text`, as whole words in any letter case (none: [])."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def ours(model: str) -> list:
    """The model's devices of ours on site, in tag order (the order SECURITY_BULK slices)."""
    return list(Asset.objects.filter(device_model__model=model, ownership=Ownership.OWNED).exclude(status=AssetStatus.RETIRED)
                .order_by("tag"))


def manager() -> User:
    return User.objects.get(username=f"{seed_demo.SECURITY_RECORDED_BY}@riverside.example")


# --- what the seed recorded ---------------------------------------------------------------------------------------------------------

def test_the_ce_manager_recorded_profiles_releases_and_devices_through_the_services(seeded):
    with tenant_context(seeded):
        today = timezone.localdate()
        rows = list(SoftwareRelease.objects.select_related("device_model"))
        profiles = {r.device_model.model: r for r in rows if r.is_profile}
        assert set(profiles) == {m for m, _ in seed_demo.SECURITY_PROFILES} == {PUMP, MONITOR, VENT, BED, C_ARM, STERILIZER}
        versions = sorted((r.device_model.model, r.version) for r in rows if not r.is_profile)
        assert versions == sorted((m, v) for m, v, _ in seed_demo.SECURITY_RELEASES) == [
            (PUMP, "5.2 (demo)"), (PUMP, "5.6 (demo)"), (MONITOR, "3.1 (demo)"), (MONITOR, "3.4 (demo)")]
        # fictional: every version reads "(demo)", every MDS2 document number DEMO-, the OS's name is made up, no link anywhere
        assert all("(demo)" in v for _, v in versions) and not any(r.removed_on for r in rows)
        assert sorted(r.mds2_document for r in rows if r.mds2_document) == ["DEMO-MDS2-0001", "DEMO-MDS2-0002", "DEMO-MDS2-0003"]
        assert not [r for r in rows if r.mds2_url] and {r.os_name for r in rows if r.os_name} == {"Demo embedded OS 7"}
        # the facility's arrangement on a few profiles; the sterilizers do not connect
        assert {m for m, p in profiles.items() if p.updates_by or p.segmented} == {PUMP, MONITOR, C_ARM}
        assert profiles[STERILIZER].connects == "none" and profiles[VENT].mds2 == Mds2Status.REQUESTED
        # the C-arm's operating system, its vendor's support ended (a fictional day, a month's last)
        assert profiles[C_ARM].os_family == "windows" and profiles[C_ARM].os_support_ends_on < today
        assert (profiles[C_ARM].os_support_ends_on + DAY).day == 1

        # Set devices to this release: the pumps (the first 12 of ours by tag on the older release, the last 4 not recorded), every
        # patient monitor of ours (connected here, wired); the vendor loaner pump recorded the day it came; a C-arm off the network
        facts = {s.asset.tag: s for s in DeviceSecurity.objects.select_related("asset", "release", "checked_by")}
        pumps, monitors = ours(PUMP), ours(MONITOR)
        assert [facts[a.tag].release.version for a in pumps[:12]] == ["5.2 (demo)"] * 12
        assert [facts[a.tag].release.version for a in pumps[12:-4]] == ["5.6 (demo)"] * (len(pumps) - 16)
        assert not [a for a in pumps[-4:] if a.tag in facts]
        assert {facts[a.tag].release_since for a in pumps[12:-4]} == {today - 150 * DAY}
        assert [facts[a.tag].release.version for a in monitors] == ["3.1 (demo)"] * 5 + ["3.4 (demo)"] * (len(monitors) - 5)
        assert {facts[a.tag].network for a in monitors} == {Network.WIRED}
        loaner = facts["T-0102"]
        assert (loaner.release.version, loaner.network, loaner.release_since, loaner.checked_on) == (
            "5.6 (demo)", Network.WIRELESS, loaner.asset.arrived_on, loaner.asset.arrived_on)
        c_arm = facts[ours(C_ARM)[0].tag]
        assert (c_arm.release, c_arm.network, c_arm.checked_on) == (None, Network.NOT_CONNECTED, today - 20 * DAY)
        assert len(facts) == len(pumps) - 4 + len(monitors) + 2

        # each by the CE manager, with history saying who; the North Campus has none
        rob = manager()
        assert {s.checked_by_id for s in facts.values()} == {rob.pk}
        assert {h.history_user_id for h in SoftwareRelease.history.all()} == {rob.pk}
        assert {h.history_user_id for h in DeviceSecurity.history.all()} == {rob.pk}
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not SoftwareRelease.objects.exists() and not DeviceSecurity.objects.exists()


def test_the_fleet_tab_head_figures_each_count_something(seeded):
    with tenant_context(seeded):
        today = timezone.localdate()
        fleet = rules.fleet(today)
        figures = {k: (f["count"], f.get("requested")) for k, f in fleet.figures.items()}
        vents = len(ours(VENT))  # with the new ventilator (slice 26)
        beds = len(ours(BED)) + 1  # with the rental bed (slice 29); the bed beyond repair is retired
        assert figures == {
            "no_mds2": (2, 1),  # the beds (none recorded) and the ventilators (requested from the manufacturer)
            "updates_ended": (12, None),  # the pumps on the older release
            "no_release": (4 + vents + beds + 1, None),  # pumps not recorded, the ventilators, the beds, the C-arm on the network
            "not_segmented": (1, None),  # the C-arms
            "segment_not_recorded": (2, None),  # the ventilators and the beds
        }
        assert all(count for count, _ in figures.values())
        rows = {r.device_model.model: r for r in fleet.rows}
        pumps = rows[PUMP]
        assert [(r.version if r else None, n) for r, n in pumps.by_release] == [("5.2 (demo)", 12), ("5.6 (demo)", len(ours(PUMP)) - 16 + 1),
                                                                               (None, 4)]
        assert (pumps.updates_ended, pumps.connected, pumps.devices, pumps.mds2_words) == (12, pumps.devices, len(ours(PUMP)) + 1,
                                                                                           "On file for 5.6 (demo)")
        # the patient monitors: the MDS2 on file names the older release; most of them run the newer one
        monitors = rows[MONITOR]
        assert monitors.mds2 == Mds2Status.ON_FILE and monitors.mds2_versions == ("3.1 (demo)",)
        assert monitors.mds2_words == "On file for 3.1 (demo)"
        assert [(r.version, n) for r, n in monitors.by_release] == [("3.1 (demo)", 5), ("3.4 (demo)", len(ours(MONITOR)) - 5)]
        # the C-arms: the OS vendor's support ended for both; one connected as disclosed, the other kept off the network
        assert (rows[C_ARM].os_ended, rows[C_ARM].connected, rows[C_ARM].mds2_words) == (2, 1, "On file")
        # the dialysis machines' and sterilizers' manufacturer support ended (slice 36), but neither can connect as recorded: their
        # devices are counted on their rows, never in the head figure about devices that can connect
        assert (rows[DIALYSIS].updates_ended, rows[STERILIZER].updates_ended) == (8, 3)
        assert not rows[DIALYSIS].can_connect and not rows[STERILIZER].can_connect

        # the standing lines a device reads, the manufacturer's and the OS vendor's apart
        standings = rules.standings(rules.on_site(), today)
        by_tag = {s.asset.tag: s for s in standings.values()}
        older, newer = by_tag[ours(PUMP)[0].tag], by_tag[ours(PUMP)[12].tag]
        assert older.security.key == rules.UPDATES_ENDED and older.security.words.startswith("Security updates ended after ")
        assert newer.security.key == rules.UPDATES_END_ANNOUNCED and newer.security.words.endswith(" (announced)")
        assert by_tag[ours(PUMP)[-1].tag].release_words == rules.RELEASE_NOT_RECORDED
        c_arm = by_tag[ours(C_ARM)[0].tag]
        assert c_arm.os.key == rules.OS_ENDED and "Demo embedded OS 7" in c_arm.os.words and c_arm.connection.words == "Not connected here"
        assert c_arm.facts.sources["support"] == rules.PROFILE
        assert by_tag["T-0102"].connection.words == "Connected (wireless)"


def test_changes_for_it_lists_the_devices_changed_lately_never_the_whole_fleet(seeded):
    with tenant_context(seeded):
        today = timezone.localdate()
        bed = WorkOrder.objects.get(repair_check=RepairCheck.NOT_RETURNED).asset  # slice 30's bed beyond repair, retired today

        def changes(days):
            return [(c.asset.tag, c.kind, c.day) for c in rules.changes_for_it(today, days)]

        recent = [(bed.tag, rules.RETIRED, today), ("CE-11003", rules.ADDED, today - 6 * DAY), ("T-0102", rules.ADDED, today - 6 * DAY),
                  ("T-0101", rules.ADDED, today - 8 * DAY), ("CE-11004", rules.ADDED, today - 9 * DAY)]
        assert changes(30) == recent
        assert changes(7) == recent[:3]
        returned = Asset.objects.get(tag="T-0098")  # the surge ventilator, back with its owner 35 days ago
        assert changes(90) == [*recent, ("T-0098", rules.RETURNED, returned.returned_on), ("T-0098", rules.ADDED, returned.arrived_on)]
        # the fleet came into Cadence the day before the demo's work began: its "Added" rows are dated so, before its other rows
        fleet = Asset.objects.filter(added_as="", ownership=Ownership.OWNED)
        added = today - seed_demo.FLEET_ADDED_DAYS_AGO * DAY
        firsts = {}
        for h in Asset.history.filter(id__in=fleet.values("id")).order_by("id", "history_date", "history_id"):
            firsts.setdefault(h.id, h)
        assert len(firsts) == fleet.count() == 196
        assert {(h.history_type, timezone.localdate(h.history_date)) for h in firsts.values()} == {("+", added)}
        assert all(a.installed_on < added for a in fleet)
        assert {c.asset.tag for c in rules.changes_for_it(today, 90) if c.asset.added_as == ""} == {bed.tag}


def test_none_of_the_seeded_words_is_one_the_register_never_says(seeded):
    """Every words string the register reads on the seeded data: each device's standing lines, connection, and facts' sources; the
    fleet's figures, their counts, and each row's MDS2; Changes for IT; the choices' labels the seed recorded."""
    with tenant_context(seeded):
        today = timezone.localdate()
        said = {rules.HICP_SENTENCE, rules.MDS2_SENTENCE}
        for s in rules.standings(rules.on_site(), today).values():
            said |= {s.release_words, s.connection.words, s.security.words, s.os.words, *s.facts.sources.values()}
        fleet = rules.fleet(today)
        said |= {w for f in fleet.figures.values() for w in (f["words"], f["counts"])} | {r.mds2_words for r in fleet.rows}
        for days in rules.CHANGES_DAYS:
            said |= {w for c in rules.changes_for_it(today, days) for w in (c.words, c.connection.words)}
        for r in SoftwareRelease.objects.all():
            said |= {r.version, r.os_name, r.mds2_document}
            said |= {getattr(r, f"get_{f.name}_display")() for f in SoftwareRelease._meta.concrete_fields if f.choices}
        said |= {s.get_network_display() for s in DeviceSecurity.objects.all()}
    assert len(said) > 30 and not [w for w in said if never_said(w)], sorted(w for w in said if never_said(w))


# --- the seeded pages -----------------------------------------------------------------------------------------------------------

def _pending(client, url: str) -> bool:
    """A page wave 2 has not built here yet: a URL whose view is the scaffold's stub, an API route not registered, a drawer with no
    Security tab."""
    path = urlsplit(url).path
    try:
        view = resolve(path).func
    except Resolver404:
        return True
    if getattr(view, "__name__", "") == "_todo":
        return True
    if "tab=security" in url:
        return importlib.util.find_spec("apps.web.asset_cyber") is None or client.get(url, HTTP_HX_REQUEST="true").context["tab"] != "security"
    return False


def _body(response) -> str:
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content).decode("utf-8-sig")
    return response.content.decode()


def _shown(response) -> str:
    """What a reader sees: a CSV or the API's JSON whole; a page's text (inside <main> when it is a whole page) with the titles,
    labels, and placeholders its elements carry, never its markup (an input's `required` attribute is not a word on the page)."""
    text = _body(response)
    if "html" not in response.get("Content-Type", ""):
        return text
    main = re.search(r"(?is)<main\b[^>]*>(.*)</main>", text)
    html = main.group(1) if main else text
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    attributes = " ".join(re.findall(r'(?i)\b(?:title|aria-label|placeholder|alt)="([^"]*)"', html))
    return unescape(re.sub(r"(?s)<[^>]+>", " ", html) + " " + attributes)


def _pages(seeded) -> tuple[list, dict]:
    """The seeded pages to read, and the tags and words each shows."""
    with tenant_context(seeded):
        pump_model = DeviceModel.objects.get(model=PUMP)
        monitor_model = DeviceModel.objects.get(model=MONITOR)
        older_pump, c_arm = ours(PUMP)[0].tag, ours(C_ARM)[0].tag
        fleet_pump = ours(PUMP)[20].tag
        bed = WorkOrder.objects.get(repair_check=RepairCheck.NOT_RETURNED).asset.tag
    recent = [bed, "CE-11003", "T-0102", "T-0101", "CE-11004"]
    shows = {
        "/security/": ["5.6 (demo)", "On file for 3.1 (demo)", rules.HICP_SENTENCE],
        "/security/export.csv": ["5.2 (demo)", "On file for 3.1 (demo)"],
        "/security/models/": [PUMP, C_ARM],
        f"/security/models/{pump_model.pk}/": [older_pump, "5.2 (demo)", "Security updates ended after "],
        f"/security/models/{monitor_model.pk}/": ["3.1 (demo)", "3.4 (demo)"],
        f"/security/devices.csv?model={pump_model.pk}": [older_pump, "T-0102", "5.6 (demo)"],
        "/security/changes/": recent,
        "/security/changes.csv": recent,
        f"/equipment/{older_pump}/?tab=security": ["5.2 (demo)", "Security updates ended after "],
        f"/equipment/{c_arm}/?tab=security": ["Not connected here", "Demo embedded OS 7"],
        "/equipment/T-0102/?tab=security": ["5.6 (demo)", "Connected (wireless)"],
        "/api/v1/software-releases/": ["DEMO-MDS2-0001", "5.2 (demo)"],
        f"/api/v1/device-security/?model={pump_model.pk}": ["5.2 (demo)"],
        "/api/v1/device-security/T-0102/": ["5.6 (demo)", "Connected (wireless)"],
    }
    never = {"/security/changes/": [fleet_pump], "/security/changes.csv": [fleet_pump]}
    return list(shows), {"shows": shows, "never": never}


def _read_pages(client, seeded) -> list:
    """Each seeded page as Kim (the director: every level), answering 200 with what it shows and none of the never-words. Returns
    the pages still pending."""
    urls, expect = _pages(seeded)
    pending = []
    for url in urls:
        if _pending(client, url):
            pending.append(url)
            continue
        # the drawer's tab and a model's panel as the screens fetch them (a partial); every other page whole
        partial = "tab=" in url or re.search(r"/security/models/[0-9a-f-]{36}/$", url)
        r = client.get(url, HTTP_HX_REQUEST="true") if partial else client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        shown = _shown(r)
        assert not never_said(shown), (url, never_said(shown))
        body = _body(r) if hasattr(r, "streaming_content") else shown
        for text in expect["shows"][url]:
            assert text in body or text in shown, (url, text)
        for text in expect["never"].get(url, []):
            assert text not in body, (url, text)
    return pending


def test_the_seeded_pages_show_the_register_and_never_say_the_words(client, seeded):
    client.force_login(User.objects.get(username="kim@riverside.example"))
    pending = _read_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 37 wave 2's pages are not merged here yet: {pending}")


# --- under row-level security ---------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_seeded_register_reads_the_same_under_the_policies(client, seeded):
    """As the runtime role the register's rules read the seeded rows exactly as the owner does (the device history Changes for IT
    reads is a historical table: no policy, filtered by this facility), and the screens, the drawer's tab, and the API answer."""
    def read():
        today = timezone.localdate()
        fleet = rules.fleet(today)
        return (sorted((s.asset.tag, s.release_words, s.connection.words, s.security.words, s.os.words, tuple(sorted(s.facts.sources.items())))
                       for s in rules.standings(rules.on_site(), today).values()),
                [(r.device_model.model, r.devices, r.connected, [(x.version if x else None, n) for x, n in r.by_release], r.mds2_words)
                 for r in fleet.rows],
                sorted((k, f["count"]) for k, f in fleet.figures.items()),
                [(c.asset.tag, c.words) for c in rules.changes_for_it(today, 90)])

    with tenant_context(seeded):
        before = read()
    assert len(before[0]) == 202 and before[3]
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    with tenant_context(seeded):
        assert read() == before
    pending = _read_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 37 wave 2's pages are not merged here yet: {pending}")
