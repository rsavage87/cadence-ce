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

Slice 38's software updates (_software_updates) and slice 39's security decisions (_security_decisions) follow the register: the pumps'
decision past its review (the nav badge hot, its Check that the plan's date has passed, its pumps' open updates beside it), the C-arm's
in force with controls its profile allows, and the rental bed on a release past its security updates with no decision (Needs attention's
rentals row); the Fleet's head figures count the bed too. The decision pages, the device drawer's decision lines, and the decisions API
are slice 39 wave 2's: pending (as above) until merged, and read with rules.DECISION_NEVER_WORDS (the drawer with rules.NEVER_WORDS: it
carries Equipment's own PM words).
"""
import importlib.util
import re
from datetime import timedelta
from html import unescape
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import (
    DeviceSecurity,
    DeviceSecurityDecision,
    InstalledBy,
    Mds2Status,
    Network,
    Segmented,
    SoftwareRelease,
    SoftwareUpdate,
    UpdateBasis,
    UpdateOutcome,
    UpdatesBy,
    UpdateTarget,
)
from apps.cybersecurity.updates import PATCHES_PROBLEM, UPDATE_PROBLEM
from apps.demo.management.commands import seed_demo
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership
from apps.facility import fiscal
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import OPEN_STATUSES, RepairCheck, Source, WorkOrder, WoStatus, WoType

PUMP, MONITOR, VENT, BED, C_ARM, STERILIZER, DIALYSIS = ("Alaris 8015 PCU", "IntelliVue MX750", "Hamilton-G5", "Centrella", "Cios Spin",
                                                        "Amsco 400", "2008T BlueStar")
DAY = timedelta(days=1)


@pytest.fixture
def seeded(db):
    call_command("seed_demo", stdout=StringIO())
    return Tenant.objects.get(slug="riverside")


def never_said(text: str, words=rules.NEVER_WORDS) -> list:
    """The words of `words` (rules.NEVER_WORDS; a decision's door rules.DECISION_NEVER_WORDS) in `text`, as whole words in any letter
    case (none: [])."""
    return [w for w in words if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def ours(model: str) -> list:
    """The model's devices of ours on site, in tag order (the order SECURITY_BULK slices)."""
    return list(Asset.objects.filter(device_model__model=model, ownership=Ownership.OWNED).exclude(status=AssetStatus.RETIRED)
                .order_by("tag"))


def manager() -> User:
    return User.objects.get(username=f"{seed_demo.SECURITY_RECORDED_BY}@riverside.example")


def installed_pump() -> Asset:
    """Slice 38: the pump whose update the technician completed as installed today (_software_updates)."""
    return SoftwareUpdate.objects.get(outcome=UpdateOutcome.INSTALLED, work_order__isnull=False).asset


def installed_by() -> User:
    """Slice 38: the technician who completed it (the batch's assignee)."""
    return SoftwareUpdate.objects.get(outcome=UpdateOutcome.INSTALLED, work_order__isnull=False).recorded_by


# --- what the seed recorded ---------------------------------------------------------------------------------------------------------

def test_the_ce_manager_recorded_profiles_releases_and_devices_through_the_services(seeded):
    with tenant_context(seeded):
        today = timezone.localdate()
        rows = list(SoftwareRelease.objects.select_related("device_model"))
        profiles = {r.device_model.model: r for r in rows if r.is_profile}
        assert set(profiles) == {m for m, _ in seed_demo.SECURITY_PROFILES} == {PUMP, MONITOR, VENT, BED, C_ARM, STERILIZER}
        versions = sorted((r.device_model.model, r.version) for r in rows if not r.is_profile)
        # slice 38: the C-arm's release, added when CE found the other C-arm on it (_software_updates); slice 39: the rental bed's, added
        # when CE recorded it (_security_decisions)
        assert versions == sorted([*((m, v) for m, v, _ in seed_demo.SECURITY_RELEASES), (C_ARM, seed_demo.C_ARM_RELEASE),
                                   (BED, seed_demo.RENTAL_RELEASE[1])]) == [
            (PUMP, "5.2 (demo)"), (PUMP, "5.6 (demo)"), (BED, "2.0 (demo)"), (C_ARM, "7.2 (demo)"), (MONITOR, "3.1 (demo)"),
            (MONITOR, "3.4 (demo)")]
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
        installed = installed_pump()  # slice 38: one of them updated to the newer release today (_software_updates)
        assert installed in pumps[:12] and facts[installed.tag].release_since == today
        assert [facts[a.tag].release.version for a in pumps[:12]] == [
            "5.6 (demo)" if a == installed else "5.2 (demo)" for a in pumps[:12]]
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
        # slice 38: the other C-arm found on its release at the last round of checks, the manufacturer's patches recorded since
        other = facts[ours(C_ARM)[1].tag]
        assert (other.release.version, other.release_since, other.checked_on) == (
            "7.2 (demo)", today - seed_demo.C_ARM_FOUND_DAYS_AGO * DAY, today - seed_demo.C_ARM_PATCHED_DAYS_AGO * DAY)
        # slice 39: the rental bed on the beds' release, connected here (wireless), checked today (_security_decisions)
        rental = facts["T-0101"]
        assert (rental.release.version, rental.network, rental.release_since, rental.checked_on) == (
            "2.0 (demo)", Network.WIRELESS, rental.asset.arrived_on, today)
        assert len(facts) == len(pumps) - 4 + len(monitors) + 4

        # each by the CE manager, with history saying who (slice 38: but the pump the technician updated today, recorded at its
        # completion by him); the North Campus has none
        rob, technician = manager(), installed_by()
        assert {s.checked_by_id for s in facts.values() if s.asset != installed} == {rob.pk}
        assert facts[installed.tag].checked_by == technician
        assert {h.history_user_id for h in SoftwareRelease.history.all()} == {rob.pk}
        assert {h.history_user_id for h in DeviceSecurity.history.all()} == {rob.pk, technician.pk}
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
            # the pumps on the older release (slice 38: one updated today), and slice 39's rental bed on a release past its updates
            "updates_ended": (11 + 1, None),
            # pumps not recorded, the ventilators, the beds (slice 38: the C-arm on the network found; slice 39: the rental bed recorded)
            "no_release": (4 + vents + beds - 1, None),
            "not_segmented": (1, None),  # the C-arms
            "segment_not_recorded": (2, None),  # the ventilators and the beds
        }
        assert all(count for count, _ in figures.values())
        rows = {r.device_model.model: r for r in fleet.rows}
        pumps = rows[PUMP]
        # slice 38: one pump of the older release's updated today (_software_updates)
        assert [(r.version if r else None, n) for r, n in pumps.by_release] == [("5.2 (demo)", 11), ("5.6 (demo)", len(ours(PUMP)) - 16 + 2),
                                                                               (None, 4)]
        assert (pumps.updates_ended, pumps.connected, pumps.devices, pumps.mds2_words) == (11, pumps.devices, len(ours(PUMP)) + 1,
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


def test_the_seeded_software_updates(seeded):
    """Slice 38 (_software_updates, after the register, through apps.cybersecurity.updates): Kim's batch to the pumps' newer release from
    the older one in one department, its 3 or 4 pumps in tag order, each due on its next PM and assigned to a credentialed in-house
    technician; the first completed by him as installed today (the pump's record on the newer release since today), the second with
    the version not confirmed, the rest open; the other C-arm's patches by the manufacturer remotely, recorded by hand on its release.
    The problems and resolutions name no version or reference; no vendor job; nothing on the ventilators; the North Campus has none."""
    with tenant_context(seeded):
        today = timezone.localdate()
        kim = User.objects.get(username="kim@riverside.example")
        rows = list(SoftwareUpdate.objects.select_related("asset", "asset__department", "work_order", "work_order__assigned_to",
                                                         "target_release", "release_after", "recorded_by", "opened_by"))
        batch = sorted((r for r in rows if r.work_order_id), key=lambda r: r.asset.tag)
        pumps = [r.asset for r in batch]
        assert len(batch) in seed_demo.UPDATE_DEPARTMENT_PUMPS and len({p.department_id for p in pumps}) == 1
        assert [p.tag for p in pumps] == sorted(p.tag for p in pumps) and {p.device_model.model for p in pumps} == {PUMP}
        # the department: the first in DEPTS order with 3 or 4 of the pumps on the older release (the batch took all of them)
        assert [w.number for w in (r.work_order for r in batch)] == sorted(r.work_order.number for r in batch)
        wos = [r.work_order for r in batch]
        assert {(w.type, w.source, w.priority, w.problem, w.requester) for w in wos} == {
            (WoType.SOFTWARE_UPDATE, Source.MANUAL, "normal", UPDATE_PROBLEM, str(kim))}
        assert {(r.target, r.target_release.version, r.opened_on, r.opened_by_id) for r in batch} == {
            (UpdateTarget.RELEASE, seed_demo.PUMP_NEWER, today, kim.pk)}
        assert all(w.due_on == (max(p.next_pm_on, today) if p.next_pm_on else today + seed_demo.UPDATE_DUE_DAYS * DAY)
                   for w, p in zip(wos, pumps, strict=True))
        technician = {w.assigned_to for w in wos}
        assert len(technician) == 1 and not any(w.vendor_service for w in wos)
        technician = technician.pop()
        assert technician.user is not None and "Infusion pumps" in {c.value for c in technician.credentials.all()}

        installed, unconfirmed, *still_open = batch
        assert (installed.work_order.status, installed.outcome, installed.basis, installed.applied_on, installed.recorded_by) == (
            WoStatus.COMPLETED, UpdateOutcome.INSTALLED, UpdateBasis.MANUFACTURER_RELEASE, today, technician.user)
        assert installed.release_after.version == seed_demo.PUMP_NEWER
        assert rules.update_words(installed) == f"{seed_demo.PUMP_NEWER} installed {rules.day_words(today)}"
        assert installed.work_order.resolution == "Software update installed; version recorded."
        assert rules.release_of(installed.asset).version == seed_demo.PUMP_NEWER and installed.asset.cyber.release_since == today
        assert (unconfirmed.work_order.status, unconfirmed.outcome, unconfirmed.work_order.resolution) == (
            WoStatus.COMPLETED, "", seed_demo.UPDATE_NOT_CONFIRMED_RESOLUTION)
        assert rules.update_state(unconfirmed) == rules.NOT_CONFIRMED and rules.release_of(unconfirmed.asset).version == seed_demo.PUMP_OLDER
        assert still_open and all(r.work_order.status in OPEN_STATUSES and rules.update_state(r) == rules.OPEN for r in still_open)
        for w in wos:
            assert "demo" not in f"{w.problem} {w.resolution}" and "DEMO" not in f"{w.problem} {w.resolution}"

        hand = [r for r in rows if not r.work_order_id]
        assert len(hand) == 1
        hand = hand[0]
        patched = today - seed_demo.C_ARM_PATCHED_DAYS_AGO * DAY
        listed = (patched.replace(day=1) - DAY).strftime("%Y-%m")
        assert hand.asset == ours(C_ARM)[1] and hand.asset.cyber.network == ""  # the C-arm on the network, never the one kept off it
        assert (hand.outcome, hand.basis, hand.installed_by, hand.reference, hand.applied_on, hand.recorded_by, hand.later_kept) == (
            UpdateOutcome.PATCHES, UpdateBasis.VALIDATED_LIST, InstalledBy.MANUFACTURER, f"DEMO-PATCH-{listed}", patched, manager(), False)
        assert rules.update_words(hand) == (f"Patches on the manufacturer's validated patch list applied {rules.day_words(patched)}, version "
                                            f"unchanged (7.2 (demo)); ref DEMO-PATCH-{listed}; installed by the manufacturer remotely")
        assert not WorkOrder.objects.filter(type=WoType.SOFTWARE_UPDATE, vendor_service=True).exists()
        assert not SoftwareUpdate.objects.filter(asset__device_model__model=VENT).exists()
        assert not WorkOrder.objects.filter(problem=PATCHES_PROBLEM).exists()
        said = [rules.update_words(r) for r in rows] + [w.resolution for w in wos]
        assert not [w for w in said if never_said(w)]
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not SoftwareUpdate.objects.exists()


def _decisions() -> tuple:
    """Slice 39's two seeded decisions: the pumps' (decided months ago), then the C-arm's (today)."""
    pumps, c_arm = DeviceSecurityDecision.objects.select_related("asset", "device_model", "release", "plan_owner", "recorded_by").order_by(
        "decided_on", "number")
    return pumps, c_arm


def test_the_seeded_security_decisions(seeded):
    """Slice 39 (_security_decisions, after the software updates, through apps.cybersecurity.decisions, by the CE manager): the pumps of
    ours on the older release kept in use with controls (release scope), decided a week after their security updates ended, an update
    planned by the last day of last month (Kim's) and reviewed that day: past its review, the nav badge hot, its Check that the plan's date
    has passed, and its pumps' slice 38 updates beside it; the C-arm on the network (never the one kept off it) kept in use with controls
    its profile allows, a replacement planned in the fiscal year after next, in force for 12 months; the rental bed on a release past its
    security updates, connected, with no decision: Needs attention's rentals row. Every token DEMO-, no link, no CVE; none of the words a
    decision never says; the North Campus has none."""
    with tenant_context(seeded):
        today = timezone.localdate()
        day = rules.day_words
        rob, kim = manager(), User.objects.get(username="kim@riverside.example")
        assert DeviceSecurityDecision.objects.count() == 2
        pumps, c_arm = _decisions()
        older = SoftwareRelease.objects.get(device_model__model=PUMP, version=seed_demo.PUMP_OLDER)
        decided = older.security_updates_end_on + seed_demo.DECISION_PUMPS_AFTER_DAYS * DAY
        plan_day = today.replace(day=1) - DAY  # the last day of last month
        # numbered by the year decided, in the order recorded (SD-26-0001 and SD-26-0002 when both fall in one year)
        assert pumps.number == f"SD-{decided:%y}-0001"
        assert c_arm.number == f"SD-{today:%y}-{2 if decided.year == today.year else 1:04d}"

        # the pumps of ours on the older release (release scope), on the medical device segment their profile records
        assert (rules.scope_kind(pumps), pumps.release, pumps.device_model.model, pumps.asset) == (rules.RELEASE_SCOPE, older, PUMP, None)
        assert {f: getattr(pumps, f) for f in seed_demo.DECISION_PUMPS} == seed_demo.DECISION_PUMPS
        assert (pumps.plan_by, pumps.plan_owner, pumps.review_by, pumps.decided_on, pumps.recorded_by, pumps.controls_reference) == (
            plan_day, kim, plan_day, decided, rob, "")
        assert (pumps.ended_on, pumps.end_reason, pumps.supersedes_id, pumps.applies_to) == (None, "", None, "support_ended")
        assert rules.profile(pumps.device_model).segmented == Segmented.ALL and rules.SEGMENTED_CONTROL in pumps.controls
        on_older = [a for a in ours(PUMP) if (runs := rules.release_of(a)) is not None and runs.pk == older.pk]
        assert len(on_older) == 11 and sorted(pumps.governed_ids) == sorted(str(a.pk) for a in on_older)
        # past its review: read first in its sentence, the plan's date passed in its Check, the badge hot
        assert rules.decision_state(pumps, today) == rules.REVIEW_DUE and not rules.covers(pumps, today)
        assert rules.review_words(pumps, today) == f"Review due since {day(plan_day)}"
        assert rules.decision_words(pumps, today) == (
            f"Review due since {day(plan_day)}: keep in use with controls (2: on a medical device network segment; traffic limited to the "
            f"ports and hosts it needs). Plan: update by {day(plan_day)} (owner {kim}). Accepted by IT security or the CISO, as recorded by "
            f"{rob} on {day(decided)}; IT exception DEMO-EX-1037.")
        assert [(c.key, c.words) for c in rules.decision_checks(pumps, today)] == [
            (rules.CHECK_PLAN_PASSED, f"The plan's date ({day(plan_day)}) has passed")]
        assert rules.badge(today) == {"count": 1, "hot": True}
        # its pumps: slice 38's open updates beside the decision, the one done with the version not confirmed its own line; the pump
        # updated today is on the newer release, out of its scope
        devices = rules.decision_devices(pumps, today)
        assert [dd.asset.tag for dd in devices] == [a.tag for a in on_older] and all(dd.governed for dd in devices)
        assert installed_pump().tag not in {dd.asset.tag for dd in devices}
        batch = {r.asset_id: r.work_order for r in SoftwareUpdate.objects.filter(work_order__isnull=False).select_related("work_order")}
        shown = {"open": 0, "not_confirmed": 0}
        for dd in devices:
            words, wo = dd.words(today), batch.get(dd.asset.pk)
            if wo is None:
                assert words == f"Review due since {day(plan_day)}"
            elif wo.status in OPEN_STATUSES:
                assert words == f"Update open to {seed_demo.PUMP_NEWER}: {wo.number}; Review due since {day(plan_day)}"
                shown["open"] += 1
            else:
                assert words == (f"Update done {day(today)}, version not confirmed: Confirm version ({wo.number}); Review due since "
                                 f"{day(plan_day)}")
                shown["not_confirmed"] += 1
        assert shown["open"] >= 1 and shown["not_confirmed"] == 1

        # the C-arm on the network (device scope), its OS vendor's support ended; the one kept off the network needs no decision
        on_network, off_network = ours(C_ARM)[1], ours(C_ARM)[0]
        start = fiscal.start_month()  # July (slice 36)
        fy = fiscal.current_fy(today, start) + seed_demo.DECISION_C_ARM_FY_AHEAD
        assert (rules.scope_kind(c_arm), c_arm.asset, c_arm.device_model, c_arm.release) == (rules.DEVICE_SCOPE, on_network, None, None)
        assert {f: getattr(c_arm, f) for f in seed_demo.DECISION_C_ARM} == seed_demo.DECISION_C_ARM
        assert (c_arm.plan_by, c_arm.plan_owner, c_arm.decided_on, c_arm.review_by, c_arm.recorded_by, c_arm.governed_ids) == (
            fiscal.fiscal_bounds(fy, start)[1], None, today, add_months(today, 12), rob, [str(on_network.pk)])
        assert rules.decision_state(c_arm, today) == rules.IN_FORCE and rules.covers(c_arm, today)
        assert rules.decision_words(c_arm, today) == (
            f"Keep in use with controls (2: traffic limited to the ports and hosts it needs; vendor remote access off, or attended only); "
            f"review by {day(c_arm.review_by)}. Plan: replace, planned for FY{fy}. Accepted by IT security or the CISO, as recorded by "
            f"{rob} on {day(today)}; IT exception DEMO-EX-1042.")
        assert rules.decision_checks(c_arm, today) == []
        # its controls agree with its model's profile: no separate segment (so never "segmented": traffic limited instead), and the
        # manufacturer installs its updates remotely (slice 38's patches), so its remote access off or attended only
        c_arm_profile = rules.profile(on_network.device_model)
        assert c_arm_profile.segmented == Segmented.NONE and rules.SEGMENTED_CONTROL not in c_arm.controls
        assert c_arm_profile.updates_by == UpdatesBy.MANUFACTURER and "remote_access_controlled" in c_arm.controls
        need = rules.support_need(rules.device_standing(on_network, today), today)
        assert (need.key, need.reasons, need.ended) == (rules.NEEDS, (rules.OS_SUPPORT,), (rules.OS_SUPPORT,))
        assert rules.support_need(rules.device_standing(off_network, today), today) is None
        assert not DeviceSecurityDecision.objects.filter(asset=off_network).exists()
        lines = rules.device_lines(on_network, today, user=kim)
        assert [(r.kind, r.number, r.covers) for r in lines.responses] == [(rules.DECIDED, c_arm.number, True)]

        # the rental bed on the beds' release past its security updates, connected here: no decision
        tag, version, _spec, network = seed_demo.RENTAL_RELEASE
        rental = Asset.objects.get(tag=tag)
        release = rules.release_of(rental)
        assert (release.device_model.model, release.version, release.security_updates_end_on, release.support_checked_on) == (
            BED, version, plan_day, today)
        assert (rental.ownership, rental.cyber.network) == (Ownership.RENTAL, network)
        standing = rules.device_standing(rental, today)
        assert (standing.connection.words, standing.security.key) == ("Connected (wireless)", rules.UPDATES_ENDED)
        need = rules.support_need(standing, today)
        assert (need.key, need.reasons) == (rules.NEEDS, (rules.SECURITY_UPDATES,))
        assert rules.governing_decisions([rental], today) == {rental.pk: None}
        lines = rules.device_lines(rental, today, user=kim)
        assert [(r.kind, r.words) for r in lines.responses] == [(rules.NO_DECISION, rules.NO_DECISION_WORDS)] and lines.can_record

        # Needs attention, as Kim reads it: the rental, the pump to confirm, the pumps' decision past its review, and its Check; no
        # device of ours with no decision recorded (every one past its support is governed)
        attention = rules.needs_attention(today, user=kim)
        headings = {h.key: h for h in attention.headings}
        assert list(headings) == ["temporary", "not_confirmed", "review_due", "checks"]
        (row,) = headings["temporary"].rows
        assert row.words == (f"{tag} (rental, {seed_demo.RENTAL_COMPANY}; due back {day(rental.due_back_on)}): security updates ended; its "
                             "owner installs updates: ask the owner to update it, or return it")
        assert (row.asset, row.link, row.record) == (rental, rules.Link(rules.URLS["device"], (tag,), (("tab", "security"),)),
                                                     rules.Link(rules.URLS["record"], (), (("asset", tag),)))
        (unconfirmed,) = headings["not_confirmed"].rows
        assert unconfirmed.asset == SoftwareUpdate.objects.get(work_order__status=WoStatus.COMPLETED, outcome="").asset
        (due,) = headings["review_due"].rows
        assert (due.decision, due.record) == (pumps, rules.Link(rules.URLS["record"], (), (("supersedes", pumps.number),)))
        assert due.words == f"{pumps.number} · {PUMP} on {seed_demo.PUMP_OLDER}: 11 devices of ours on site; review due since {day(plan_day)}"
        (check,) = headings["checks"].rows
        assert check.words == f"{pumps.number} · The plan's date ({day(plan_day)}) has passed"

        # each recorded by the CE manager, its history saying so; a decision's words never say what it never says
        assert {(h.history_user_id, h.history_change_reason) for h in DeviceSecurityDecision.history.all()} == {(rob.pk, decisions.RECORDED)}
        rows = (pumps, c_arm)
        said = [rules.decision_words(d, today) for d in rows] + [rules.review_words(d, today) for d in rows]
        said += [rules.scope_words(d) for d in rows] + [rules.plan_words(d) for d in rows] + [rules.who_words(d) for d in rows]
        said += [w for d in rows for w in (*rules.control_words(d), *rules.reason_words(d))]
        said += [dd.words(today) for d in rows for dd in rules.decision_devices(d, today)]
        said += [h.words for h in attention.headings] + [r.words for h in attention.headings for r in h.rows]
        said += [r.words for a in (on_network, rental) for r in rules.device_lines(a, today, user=kim).responses]
        assert not [w for w in said if never_said(w, rules.DECISION_NEVER_WORDS)]
        tokens = [d.exception_reference for d in rows]
        assert tokens == ["DEMO-EX-1037", "DEMO-EX-1042"] and not [d for d in rows if d.controls_reference]
        assert not [w for w in said if "http" in w or "CVE" in w]
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not DeviceSecurityDecision.objects.exists()


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
        "/equipment/T-0101/?tab=security": ["2.0 (demo)", "Connected (wireless)", "Security updates ended after "],  # slice 39
        "/api/v1/software-releases/": ["DEMO-MDS2-0001", "5.2 (demo)"],
        f"/api/v1/device-security/?device_model={pump_model.pk}": ["5.2 (demo)"],
        "/api/v1/device-security/T-0102/": ["5.6 (demo)", "Connected (wireless)"],
        "/api/v1/software-updates/": ["Open: to install 5.6 (demo)", "version not confirmed", "DEMO-PATCH-", "installed by the manufacturer"],
    }
    never = {"/security/changes/": [fleet_pump], "/security/changes.csv": [fleet_pump]}
    return list(shows), {"shows": shows, "never": never}


def _read(client, urls, expect, *, pending=_pending, words=lambda url: rules.NEVER_WORDS) -> list:
    """Each page as the signed-in client, answering 200 with what it shows and none of the words `words(url)` names. Returns the
    pages still pending."""
    left = []
    for url in urls:
        if pending(client, url):
            left.append(url)
            continue
        # the drawer's tab and a model's panel as the screens fetch them (a partial); every other page whole
        partial = "tab=" in url or re.search(r"/security/models/[0-9a-f-]{36}/$", url)
        r = client.get(url, HTTP_HX_REQUEST="true") if partial else client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        shown = _shown(r)
        assert not never_said(shown, words(url)), (url, never_said(shown, words(url)))
        body = _body(r) if hasattr(r, "streaming_content") else shown
        for text in expect["shows"][url]:
            assert text in body or text in shown, (url, text)
        for text in expect["never"].get(url, []):
            assert text not in body, (url, text)
    return left


def _read_pages(client, seeded) -> list:
    """Each seeded register page as Kim (the director: every level), answering 200 with what it shows and none of the never-words.
    Returns the pages still pending."""
    urls, expect = _pages(seeded)
    return _read(client, urls, expect)


def _decision_pages(seeded) -> tuple[list, dict]:
    """Slice 39's seeded pages (build39 section 7 and 8): the Decisions tab with Needs attention, each decision's page, the decisions
    API, and the device drawer's decision lines (the C-arm's decision; the rental's "No decision recorded"), and what each shows."""
    with tenant_context(seeded):
        today = timezone.localdate()
        pumps, c_arm = _decisions()
        plan_day = rules.day_words(pumps.plan_by)
        start = fiscal.start_month()
        replace = rules.plan_words(c_arm, fy_start=start)
        pump = rules.decision_devices(pumps, today)[0].asset.tag
        c_arm_tag = c_arm.asset.tag
    shows = {
        "/security/decisions/": [pumps.number, c_arm.number, f"Review due since {plan_day}", "DEMO-EX-1037", "DEMO-EX-1042", "T-0101"],
        f"/security/decisions/{pumps.number}/": [pumps.number, "DEMO-EX-1037", f"The plan's date ({plan_day}) has passed", pump,
                                                 f"Update open to {seed_demo.PUMP_NEWER}"],
        f"/security/decisions/{c_arm.number}/": [c_arm.number, "DEMO-EX-1042", replace, c_arm_tag],
        "/api/v1/security-decisions/": [pumps.number, c_arm.number, "DEMO-EX-1037", "DEMO-EX-1042"],
        f"/equipment/{c_arm_tag}/?tab=security": [c_arm.number],
        "/equipment/T-0101/?tab=security": [rules.NO_DECISION_WORDS],
    }
    return list(shows), {"shows": shows, "never": {}}


def _decision_pending(client, url: str) -> bool:
    """A slice 39 page not built here yet: its route not there (as _pending), or, for the drawer's tab (built in slice 37), the
    decision page its lines link to not there."""
    if "tab=security" in url:
        try:
            reverse(rules.URLS["decision"], args=["SD-00-0000"])
        except NoReverseMatch:
            return True
    return _pending(client, url)


def _read_decision_pages(client, seeded) -> list:
    """Slice 39's seeded pages as Kim, a decision's doors read with rules.DECISION_NEVER_WORDS and the drawer with rules.NEVER_WORDS
    (it carries Equipment's own PM words). Returns the pages still pending."""
    urls, expect = _decision_pages(seeded)
    return _read(client, urls, expect, pending=_decision_pending,
                 words=lambda url: rules.NEVER_WORDS if "tab=security" in url else rules.DECISION_NEVER_WORDS)


def test_the_seeded_pages_show_the_register_and_never_say_the_words(client, seeded):
    client.force_login(User.objects.get(username="kim@riverside.example"))
    pending = _read_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 37 wave 2's pages are not merged here yet: {pending}")


def test_the_seeded_decision_pages_show_the_decisions_and_never_say_the_words(client, seeded):
    """Slice 39: the Decisions tab (the pumps' decision past its review, the C-arm's, the rental in Needs attention), each decision's
    page (its exception number, its Check, its devices with their updates), the decisions API, and the drawer's decision lines."""
    client.force_login(User.objects.get(username="kim@riverside.example"))
    pending = _read_decision_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 39 wave 2's pages are not merged here yet: {pending}")


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


@needs_postgres
def test_the_seeded_decisions_read_the_same_under_the_policies(client, seeded):
    """Slice 39: as the runtime role the decisions' readers (each decision's sentence, Check lines, and devices; Needs attention; the
    badge) read the seeded rows exactly as the owner does, and the decision pages answer."""
    def read():
        today = timezone.localdate()
        kim = User.objects.get(username="kim@riverside.example")
        decided = [(d.number, rules.decision_words(d, today), [c.words for c in rules.decision_checks(d, today)],
                    [(dd.asset.tag, dd.words(today)) for dd in rules.decision_devices(d, today)]) for d in _decisions()]
        attention = [(h.key, h.count, [r.words for r in h.rows]) for h in rules.needs_attention(today, user=kim).headings]
        return decided, attention, rules.badge(today)

    with tenant_context(seeded):
        before = read()
    assert len(before[0]) == 2 and len(before[0][0][3]) == 11 and before[2] == {"count": 1, "hot": True}
    assert [key for key, _n, _rows in before[1]] == ["temporary", "not_confirmed", "review_due", "checks"]
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    with tenant_context(seeded):
        assert read() == before
    pending = _read_decision_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 39 wave 2's pages are not merged here yet: {pending}")
