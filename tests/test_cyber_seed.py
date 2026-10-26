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

Slice 40's security advisories (_security_advisories) follow the decisions, in the order received: the patient monitors' older release
decided not in use here ("Open: ready to close"), the C-arms' patches (the list the other C-arm's patches came from) with the C-arm kept
off the network decided and the advisory closed, one naming no model marked not applicable by a technician, the pumps' older release with
the newer one the fix (slice 38's updates to it answer, the rest "No response recorded": Close refused in words), and the beds with their
models to confirm (the badge's advisory half, the rental bed as its own row). Slice 39's pins count support decisions only
(applies_to=support_ended: the advisory decisions never move them), and the Decisions tab reads as before. The Advisories tab, the advisory
pages, the keyed decision doors, the device drawer's Advisories section, the advisories API, and the nav badge's two halves are slice 40
wave 2's: pending (as above) until merged.
"""
import importlib.util
import re
from collections import Counter
from datetime import timedelta
from html import unescape
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.urls import NoReverseMatch, Resolver404, resolve, reverse
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import User
from apps.cybersecurity import advisories, decisions, rules
from apps.cybersecurity.models import (
    AdvisoryChange,
    AdvisorySource,
    AppliesTo,
    DeviceSecurity,
    DeviceSecurityDecision,
    InstalledBy,
    Mds2Status,
    Network,
    SecurityAdvisory,
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
    """Slice 39's two seeded decisions (for devices past support: slice 40's advisory decisions are never among them): the pumps'
    (decided months ago), then the C-arm's (today)."""
    pumps, c_arm = (DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.SUPPORT_ENDED)
                    .select_related("asset", "device_model", "release", "plan_owner", "recorded_by").order_by("decided_on", "number"))
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
        assert DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.SUPPORT_ENDED).count() == 2  # slice 40's advisory decisions apart
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
            f"ports and hosts it needs). Plan: update by {day(plan_day)} (owner {kim}). Accepted by IT security or the CISO on "
            f"{day(decided)}, as recorded by {rob}; IT exception DEMO-EX-1037.")
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
            f"review by {day(c_arm.review_by)}. Plan: replace, planned for FY{fy}. Accepted by IT security or the CISO on {day(today)}, "
            f"as recorded by {rob}; IT exception DEMO-EX-1042.")
        assert rules.decision_checks(c_arm, today) == []
        # its controls agree with its model's profile: no separate segment (so never "segmented": traffic limited instead), and the
        # manufacturer installs its updates remotely (slice 38's patches), so its remote access off or attended only
        c_arm_profile = rules.profile(on_network.device_model)
        assert c_arm_profile.segmented == Segmented.NONE and rules.SEGMENTED_CONTROL not in c_arm.controls
        assert c_arm_profile.updates_by == UpdatesBy.MANUFACTURER and "remote_access_controlled" in c_arm.controls
        need = rules.support_need(rules.device_standing(on_network, today), today)
        assert (need.key, need.reasons, need.ended) == (rules.NEEDS, (rules.OS_SUPPORT,), (rules.OS_SUPPORT,))
        assert rules.support_need(rules.device_standing(off_network, today), today) is None
        assert not DeviceSecurityDecision.objects.filter(asset=off_network, applies_to=AppliesTo.SUPPORT_ENDED).exists()  # slice 40's is
        # an advisory's (_security_advisories): never a support decision
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
        assert row.words == (f"{tag} (rental, {seed_demo.RENTAL_COMPANY}; due back {day(rental.due_back_on)}): security updates ended "
                             f"after {day(plan_day)}; its owner installs updates: ask the owner to update it, or return it")
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
        assert {(h.history_user_id, h.history_change_reason)
                for h in DeviceSecurityDecision.history.filter(applies_to=AppliesTo.SUPPORT_ENDED)} == {(rob.pk, decisions.RECORDED)}
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


def _advisories() -> list:
    """Slice 40's five seeded advisories, in the order received (seed_demo.ADVISORIES' order)."""
    return list(SecurityAdvisory.objects.select_related("recorded_by", "status_by", "models_confirmed_by").order_by("received_on", "number"))


def _advisory_decisions() -> tuple:
    """Slice 40's two seeded advisory decisions, in the order recorded: the monitors' on the older release (not in use here), then the
    C-arm kept off the network's."""
    not_in_use, off_network = (DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.ADVISORY)
                               .select_related("advisory", "asset", "device_model", "release", "plan_owner", "recorded_by")
                               .order_by("created_at"))
    return not_in_use, off_network


def _numbered(prefix: str, days) -> list:
    """The numbers a sequence gives records recorded in this order on these days: <prefix>-<yy>-<nnnn>, counted per year (SD by the
    year decided, AD by the year received: in early January a day received or decided falls in the year before)."""
    counted, out = Counter(), []
    for d in days:
        counted[d.year] += 1
        out.append(f"{prefix}-{d:%y}-{counted[d.year]:04d}")
    return out


def test_the_seeded_security_advisories(seeded):
    """Slice 40 (_security_advisories, after the decisions and before the vendor review, by the CE manager through
    apps.cybersecurity.advisories and decisions, in the order received, each numbered by the year received): the patient monitors' older
    release with mitigations only, its monitors decided not in use here (in force), its revision recorded today changing only
    mitigations (nothing reopens): "Open: ready to close"; every release of the C-arms with the patch list the other C-arm's patches came
    from ("the list the advisory names"), the C-arm kept off the network decided with controls, and closed; one naming no model marked not
    applicable by a technician; the pumps' older release with the newer one the fix: on the fix, slice 38's open updates to it, the one
    with the version not confirmed, and the rest with no response (slice 39's support decision never answers this need), so Close is
    refused in words; the beds, releases not stated, no fix yet, models to confirm, the rental bed as its own row. Slice 39's Decisions
    tab and badge read as before. Every source a manufacturer's bulletin, its portal, or other, every reference DEMO-ADV-, no link, no CVE
    ID; none of the words a decision's door never says; the North Campus has none."""
    with tenant_context(seeded):
        today = timezone.localdate()
        day = rules.day_words
        rob, kim = manager(), User.objects.get(username="kim@riverside.example")
        index, reason, username = seed_demo.ADVISORY_NOT_APPLICABLE
        technician = User.objects.get(username=f"{username}@riverside.example")
        assert technician.role.slug == "technician" and rob.role.slug == "manager"
        ads = _advisories()
        received = [today - ago * DAY for ago, *_rest in seed_demo.ADVISORIES]
        assert [a.received_on for a in ads] == received == sorted(received)
        assert [a.number for a in ads] == _numbered("AD", received)  # numbered in the order received, by the year received
        monitors, c_arms, no_model, pumps, beds = ads
        assert ads[index] == no_model

        # the sources: fictional (a manufacturer's bulletin, its portal, or other; DEMO-ADV- references; no link), each one's oldest first
        sources = list(AdvisorySource.objects.select_related("advisory").order_by("advisory__received_on", "advisory__number", "created_at"))
        assert [(s.advisory, s.source, s.reference, s.published_on) for s in sources] == [
            (monitors, "manufacturer_bulletin", "DEMO-ADV-0001", today - 30 * DAY),
            (c_arms, "manufacturer_portal", "DEMO-ADV-0002", today - 12 * DAY), (no_model, "other", "DEMO-ADV-0003", None),
            (pumps, "manufacturer_bulletin", "DEMO-ADV-0004", today - 5 * DAY), (pumps, "other", "DEMO-ADV-0004-N", None),
            (beds, "manufacturer_bulletin", "DEMO-ADV-0005", None)]
        assert not [s for s in sources if s.url or s.removed_on or s.advisory_removed or not s.reference.startswith("DEMO-ADV-")]
        assert not [a for a in ads if a.cves or a.removed_on] and {a.recorded_by for a in ads} == {rob}
        # the monitors' bulletin's second revision, recorded today: mitigations only, so nothing reopened and nothing counted as a change
        bulletin = sources[0]
        assert (bulletin.revision, bulletin.updated_on, bulletin.last_change) == ("2", today - 8 * DAY, ["mitigations_changed"])
        assert not [a for a in ads if a.changed_on or a.changes]

        # the models each names (the C-arms' patch list the one the other C-arm's patches came from: slice 38's hand record)
        patch_list = SoftwareUpdate.objects.get(work_order__isnull=True).reference
        assert patch_list == seed_demo._c_arm_patches(today)
        rows = rules.rows_of(ads)
        assert [(r.advisory, r.device_model.model, r.versions, [x.version for x in r.listed], r.remedy, getattr(r.fix_release, "version", None),
                 r.patches_reference) for r in rows] == [
            (monitors, MONITOR, "listed", [seed_demo.MONITOR_OLDER], "mitigations", None, ""),
            (c_arms, C_ARM, "all", [], "patches", None, patch_list),
            (pumps, PUMP, "listed", [seed_demo.PUMP_OLDER], "release", seed_demo.PUMP_NEWER, ""),
            (beds, BED, "not_stated", [], "no_fix_yet", None, "")]
        assert [(a.status, a.models_confirmed_on, a.models_confirmed_by, a.status_on, a.status_by, a.not_applicable_reason) for a in ads] == [
            ("open", received[0], rob, None, None, ""), ("closed", received[1], rob, today, rob, ""),
            ("not_applicable", received[2], technician, received[2], technician, reason), ("open", received[3], rob, None, None, ""),
            ("open", None, None, None, None, "")]

        # the decisions on two of them, numbered after slice 39's by the year decided, in the order recorded
        pumps_support, _c_arm_support = _decisions()
        not_in_use, off_decision = _advisory_decisions()
        decided = today - seed_demo.ADVISORY_MONITORS_DECIDED_DAYS_AGO * DAY
        assert [d.number for d in (pumps_support, _c_arm_support, not_in_use, off_decision)] == _numbered(
            "SD", [pumps_support.decided_on, today, decided, today])
        older_monitor = SoftwareRelease.objects.get(device_model__model=MONITOR, version=seed_demo.MONITOR_OLDER)
        monitors_older = [a for a in ours(MONITOR) if rules.release_of(a) == older_monitor]
        assert len(monitors_older) == 5
        assert (not_in_use.advisory, rules.scope_kind(not_in_use), not_in_use.release, not_in_use.asset) == (
            monitors, rules.RELEASE_SCOPE, older_monitor, None)
        assert {f: getattr(not_in_use, f) for f in seed_demo.ADVISORY_MONITORS_DECISION} == seed_demo.ADVISORY_MONITORS_DECISION
        assert (not_in_use.plan, not_in_use.controls, not_in_use.exception_reference, not_in_use.decided_on, not_in_use.review_by,
                not_in_use.advisory_changes, not_in_use.recorded_by, not_in_use.reasons) == (
            "none", [], "", decided, add_months(decided, 12), 0, rob, [])
        assert sorted(not_in_use.governed_ids) == sorted(str(a.pk) for a in monitors_older)
        assert rules.decision_words(not_in_use, today) == (
            f"Decided {day(decided)} that the part it names is not in use here; review by {day(add_months(decided, 12))}. Decided by the CE "
            f"leader, as recorded by {rob}; no IT exception number recorded.")
        on_network, off_network = ours(C_ARM)[1], ours(C_ARM)[0]
        start = fiscal.start_month()
        fy = fiscal.current_fy(today, start) + seed_demo.DECISION_C_ARM_FY_AHEAD
        assert (off_decision.advisory, rules.scope_kind(off_decision), off_decision.asset, off_decision.governed_ids) == (
            c_arms, rules.DEVICE_SCOPE, off_network, [str(off_network.pk)])
        assert {f: getattr(off_decision, f) for f in seed_demo.ADVISORY_C_ARM_DECISION} == seed_demo.ADVISORY_C_ARM_DECISION
        assert (off_decision.plan_by, off_decision.exception_reference, off_decision.decided_on, off_decision.review_by,
                off_decision.advisory_changes) == (fiscal.fiscal_bounds(fy, start)[1], "", today, add_months(today, 12), 0)
        assert rules.decision_words(off_decision, today) == (
            f"Keep in use with controls (2: taken off the network; physical access and removable media controlled); review by "
            f"{day(add_months(today, 12))}. Plan: replace, planned for FY{fy}. Accepted by the CE leader on {day(today)}, as recorded by "
            f"{rob}; no IT exception number recorded.")
        assert rules.device_standing(off_network, today).connection.words == "Not connected here"  # network_removed rests on it
        # both in force, with no Check: nothing changed since, no plan's date passed, no device joined or connected since
        for d in (not_in_use, off_decision):
            assert (rules.decision_state(d, today), rules.covers(d, today), rules.decision_checks(d, today)) == (rules.IN_FORCE, True, [])
            assert {str(dd.asset.pk) for dd in rules.decision_devices(d, today)} == set(d.governed_ids)
        # slice 39's badge and Decisions tab as before: the advisory decisions add no row
        assert rules.badge(today) == {"count": 1, "hot": True}
        assert [h.key for h in rules.needs_attention(today, user=kim).headings] == ["temporary", "not_confirmed", "review_due", "checks"]

        # each device's line and what answers it
        reading = rules.advisory_reading(ads, today)
        lines = {(ln.row.advisory.number, ln.asset.tag): ln for ln in reading.lines}
        answers = {(a.line.row.advisory.number, a.line.asset.tag): a for a in reading.answers.values()}

        def of(advisory) -> list:
            return [ln for ln in reading.lines if ln.row.advisory_id == advisory.pk]

        def shown(advisory, tag) -> tuple:
            a = answers[(advisory.number, tag)]
            return a.kind, [r.words for r in a.responses]

        # the monitors (every one ours): the five on the older release under the decision, the rest on a release it does not name
        mine = of(monitors)
        assert [ln.asset.tag for ln in mine] == [a.tag for a in ours(MONITOR)]
        assert {ln.asset.tag for ln in mine if ln.kind == rules.NAMED} == {a.tag for a in monitors_older}
        for ln in mine:
            if ln.kind == rules.NAMED:
                assert ln.words == f"On {seed_demo.MONITOR_OLDER}, a release the advisory names"
                assert shown(monitors, ln.asset.tag) == (rules.ANSWER_DECIDED, [rules.decision_words(not_in_use, today)])
            else:
                assert (ln.kind, ln.words) == (rules.OTHER_RELEASE, f"On {seed_demo.MONITOR_NEWER}, a release the advisory does not name")
                assert shown(monitors, ln.asset.tag) == ("", [])
        assert advisories.close_refusal(monitors, today, user=kim) is None

        # the C-arms: the one on the network with the patches on the list it names, the one kept off it under its decision; closed
        assert [ln.asset.tag for ln in of(c_arms)] == [off_network.tag, on_network.tag]
        assert {ln.words for ln in of(c_arms)} == {f"The advisory names every release of the {C_ARM}"}
        patched = today - seed_demo.C_ARM_PATCHED_DAYS_AGO * DAY
        assert shown(c_arms, on_network.tag) == (
            rules.PATCHES_RECORDED, [f"The manufacturer's patches recorded {day(patched)} ({patch_list}), the list the advisory names"])
        assert shown(c_arms, off_network.tag) == (rules.ANSWER_DECIDED, [rules.decision_words(off_decision, today)])

        # the one naming no model: its reason still holds
        assert not of(no_model) and advisories.not_applicable_refusals(no_model, today)[reason] is None

        # the pumps: on the fix, slice 38's updates to it, the rest with no response (slice 39's decision is another need's)
        pump_older, pump_newer = (SoftwareRelease.objects.get(device_model__model=PUMP, version=v)
                                  for v in (seed_demo.PUMP_OLDER, seed_demo.PUMP_NEWER))
        on_site = list(rules.on_site().filter(device_model__model=PUMP).select_related("cyber__release").order_by("tag"))
        mine = of(pumps)
        assert [ln.asset.tag for ln in mine] == [a.tag for a in on_site] and "T-0102" in {a.tag for a in on_site}
        kinds = {ln.asset.tag: ln.kind for ln in mine}
        assert {t for t, k in kinds.items() if k == rules.ON_FIX} == {a.tag for a in on_site if rules.release_of(a) == pump_newer}
        assert {t for t, k in kinds.items() if k == rules.NAMED} == {a.tag for a in on_site if rules.release_of(a) == pump_older}
        assert {t for t, k in kinds.items() if k == rules.LINE_NOT_RECORDED} == {a.tag for a in ours(PUMP)[-4:]}
        assert kinds["T-0102"] == kinds[installed_pump().tag] == rules.ON_FIX and rules.OTHER_RELEASE not in kinds.values()
        assert lines[(pumps.number, "T-0102")].words == f"On {seed_demo.PUMP_NEWER}, the release the advisory names as the fix"
        batch = {r.asset.tag: r.work_order for r in SoftwareUpdate.objects.filter(work_order__isnull=False).select_related("asset", "work_order")}
        none, opened, not_confirmed = [], [], []
        for ln in mine:
            tag = ln.asset.tag
            if ln.kind == rules.ON_FIX:
                assert shown(pumps, tag) == ("", [])
                continue
            assert answers[(pumps.number, tag)].decision is None  # the support decision governs their support, never this need
            wo = batch.get(tag)
            if wo is None:
                assert shown(pumps, tag) == (rules.NO_RESPONSE, [rules.NO_RESPONSE_WORDS])
                none.append(tag)
            elif wo.status in OPEN_STATUSES:
                assert shown(pumps, tag) == (rules.UPDATE_OPEN, [f"Update open to {seed_demo.PUMP_NEWER}, the fix: {wo.number}"])
                opened.append(tag)
            else:
                assert shown(pumps, tag) == (rules.UPDATE_NOT_CONFIRMED, [
                    f"Update done {day(today)}, version not confirmed: Confirm version ({wo.number})"])
                not_confirmed.append(tag)
        assert len(none) > 10 and opened and len(not_confirmed) == 1
        refusal = advisories.close_refusal(pumps, today, user=kim)
        assert refusal.startswith(f"Not closed: {len(none)} devices on a release it names, or with no release recorded, have no response "
                                  f"recorded ({', '.join(none[:10])} and {len(none) - 10} more). Open updates to what the advisory offers")
        assert f"({not_confirmed[0]}): confirm the version on its work order" in refusal

        # the beds: every one on site named, releases not stated, none answered; the rental bed among them
        beds_on_site = [a.tag for a in rules.on_site().filter(device_model__model=BED).order_by("tag")]
        assert [ln.asset.tag for ln in of(beds)] == beds_on_site and "T-0101" in beds_on_site
        assert {(ln.kind, ln.words) for ln in of(beds)} == {(rules.NAMED, f"The advisory names the {BED} without stating releases")}
        assert all(shown(beds, t) == (rules.NO_RESPONSE, [rules.NO_RESPONSE_WORDS]) for t in beds_on_site)
        assert advisories.close_refusal(beds, today, user=kim) == advisories.MODELS_TO_CONFIRM

        # the status words: a reader of devices, and a Device security View reader (never a word derived from a decision)
        counts = rules.advisory_counts(ads, today=today, devices_visible=True)
        na_words = f"Not applicable here ({technician}, {day(received[2])}): It names no model in this facility's catalog"
        assert [counts[a.pk].words for a in ads] == [rules.READY_TO_CLOSE_WORDS, f"Closed ({rob}, {day(today)})", na_words,
                                                     f"Open: {len(none)} devices with no response recorded", rules.MODELS_TO_CONFIRM_WORDS]
        view = rules.advisory_counts(ads, today=today, devices_visible=False)
        assert [view[a.pk].words for a in ads] == [rules.OPEN_WORDS, f"Closed ({rob}, {day(today)})", na_words, rules.OPEN_WORDS,
                                                   rules.MODELS_TO_CONFIRM_WORDS]
        assert not [a for a in ads if view[a.pk].answers is not None]
        assert counts[pumps.pk].lines == {rules.NAMED: len(none) - 4 + len(opened) + 1, rules.LINE_NOT_RECORDED: 4,
                                          rules.ON_FIX: len(on_site) - len(none) - len(opened) - 1, rules.OTHER_RELEASE: 0}
        assert counts[pumps.pk].updates == view[pumps.pk].updates == {rules.UPDATE_OPEN: len(opened), rules.UPDATE_NOT_CONFIRMED: 1}
        assert not [c for a in ads for c in rules.advisory_checks(a, today=today, devices_visible=True)]  # nothing named or added since

        # the Advisories tab's Needs attention as Kim reads it; the badge's advisory half (the beds' models to confirm)
        attention = rules.advisory_attention(today, user=kim)
        headings = {h.key: h for h in attention.headings}
        assert list(headings) == ["to_confirm", "no_response", "temporary", "not_confirmed"]
        assert [r.words for r in headings["to_confirm"].rows] == [rules.advisory_name(beds)] == [f"{beds.number} · DEMO-ADV-0005 · {BED}"]
        beds_ours = [a.tag for a in ours(BED)]
        assert [(r.words, r.count) for r in headings["no_response"].rows] == [
            (f"{pumps.number} · {PUMP}: {len(none)} devices of ours; {len(none) - 4} on a release it names, 4 with no release recorded",
             len(none)),
            (f"{beds.number} · {BED}: {len(beds_ours)} devices of ours; the advisory names the {BED} without stating releases", len(beds_ours))]
        rental = Asset.objects.get(tag="T-0101")
        (row,) = headings["temporary"].rows
        assert row.words == (f"T-0101 (rental, {seed_demo.RENTAL_COMPANY}; due back {day(rental.due_back_on)}): the advisory names the "
                             f"{BED} without stating releases ({beds.number}); its owner installs updates: ask the owner about it, or return it")
        (row,) = headings["not_confirmed"].rows
        assert row.words == (f"{pumps.number} · {not_confirmed[0]}: Update done {day(today)}, version not confirmed: Confirm version "
                             f"({batch[not_confirmed[0]].number})")
        assert rules.advisory_badge(today) == {"count": 1}

        # history: who recorded each step
        assert {h.history_user_id for h in SecurityAdvisory.history.all()} == {rob.pk, technician.pk}
        assert {h.history_user_id for h in AdvisorySource.history.all()} == {rob.pk}
        assert ({(h.history_user_id, h.history_change_reason) for h in SecurityAdvisory.history.filter(id=no_model.pk)} ==
                {(rob.pk, advisories.RECORDED), (technician.pk, advisories.MARKED_NOT_APPLICABLE)})

        # none of the words a decision's door never says, no link, no CVE ID
        said = [rules.HICP_ADVISORIES, rules.HICP_ADVISORY_DECISIONS, *(rules.advisory_name(a) for a in ads)]
        said += [c.words for c in counts.values()] + [c.words for c in view.values()]
        said += [ln.words for ln in reading.lines] + [r.words for a in reading.answers.values() for r in a.responses]
        said += [h.words for h in attention.headings] + [r.words for h in attention.headings for r in h.rows]
        said += [w for a in ads for w in (advisories.close_refusal(a, today, user=kim), advisories.close_refusal(a, today)) if w]
        said += [w for a in ads for w in advisories.not_applicable_refusals(a, today).values() if w]
        for d in (not_in_use, off_decision):
            said += [rules.decision_words(d, today), rules.scope_words(d, advisory=True), rules.plan_words(d), rules.who_words(d),
                     *rules.control_words(d), *(dd.words(today) for dd in rules.decision_devices(d, today))]
        said += [s.get_source_display() for s in sources] + [r.get_versions_display() for r in rows] + [r.get_remedy_display() for r in rows]
        said += [a.get_status_display() for a in ads] + [AdvisoryChange(c).label for c in bulletin.last_change]
        assert not [w for w in said if never_said(w, rules.DECISION_NEVER_WORDS)], [w for w in said if never_said(w, rules.DECISION_NEVER_WORDS)]
        assert not [w for w in said if "http" in w or "CVE" in w]
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not SecurityAdvisory.objects.exists() and not AdvisorySource.objects.exists()


def test_kims_nav_badge_counts_both_halves(seeded):
    """Slice 40: Kim (the director: a reader of decisions and an editor of advisories) reads Device security's badge as the sum of its two
    halves (rules.badge: the pumps' decision past its review, hot; rules.advisory_badge: the beds' advisory with models to confirm, never
    hot), titled for both. The nav's advisory half is slice 40 wave 2C's (reports.services._cyber_badge): pending until merged."""
    from apps.reports.services import nav_counts

    with tenant_context(seeded):
        today = timezone.localdate()
        assert (rules.badge(today), rules.advisory_badge(today)) == ({"count": 1, "hot": True}, {"count": 1})
        counts = nav_counts(User.objects.get(username="kim@riverside.example"))
    if (counts["cybersecurity"], counts.get("cybersecurity_title")) == (1, rules.BADGE_TITLE):
        pytest.xfail("slice 40 wave 2C's badge (the advisory half) is not merged here yet")
    assert (counts["cybersecurity"], counts["cybersecurity_hot"], counts["cybersecurity_title"]) == (2, True, rules.BOTH_BADGE_TITLE)


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
        # the drawer's tab, a model's panel, a decision's or an advisory's page, and a modal as the screens fetch them (a partial); every
        # other page whole
        partial = ("tab=" in url or re.search(r"/security/models/[0-9a-f-]{36}/$", url) or re.search(r"/security/decisions/SD-[0-9-]+/$", url)
                   or re.search(r"/security/advisories/AD-[0-9-]+/$", url) or "/new/?" in url)
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
    assert not pending, f"pages not built: {pending}"


def test_the_seeded_decision_pages_show_the_decisions_and_never_say_the_words(client, seeded):
    """Slice 39: the Decisions tab (the pumps' decision past its review, the C-arm's, the rental in Needs attention), each decision's
    page (its exception number, its Check, its devices with their updates), the decisions API, and the drawer's decision lines."""
    client.force_login(User.objects.get(username="kim@riverside.example"))
    pending = _read_decision_pages(client, seeded)
    assert not pending, f"pages not built: {pending}"


def _advisory_pages(seeded) -> tuple[list, dict]:
    """Slice 40's seeded pages (build40 sections 7 and 8): the Advisories tab with Needs attention and its CSV, each advisory's page, the
    pumps' devices CSV, the advisory decisions' pages, Record decision for the pumps' older release under their advisory (prefilled from
    the pumps' support decision), the device drawer's Advisories section, the advisories API, and the decisions API by need; and what
    each shows."""
    with tenant_context(seeded):
        monitors, c_arms, no_model, pumps, beds = _advisories()
        not_in_use, off_decision = _advisory_decisions()
        pumps_support, _c_arm = _decisions()
        older = SoftwareRelease.objects.get(device_model__model=PUMP, version=seed_demo.PUMP_OLDER)
        batch = set(SoftwareUpdate.objects.filter(work_order__isnull=False).values_list("asset__tag", flat=True))
        pump = next(a.tag for a in ours(PUMP) if rules.release_of(a) == older and a.tag not in batch)  # no response recorded
        patch_list = SoftwareUpdate.objects.get(work_order__isnull=True).reference
    shows = {
        "/security/advisories/": [monitors.number, beds.number, rules.MODELS_TO_CONFIRM_WORDS, rules.READY_TO_CLOSE_WORDS, "T-0101"],
        "/security/advisories/export.csv": [monitors.number, pumps.number, "DEMO-ADV-0001"],
        f"/security/advisories/{monitors.number}/": [rules.READY_TO_CLOSE_WORDS, "DEMO-ADV-0001", seed_demo.MONITOR_NEWER, not_in_use.number],
        f"/security/advisories/{c_arms.number}/": ["Closed (", patch_list, off_decision.number],
        f"/security/advisories/{no_model.number}/": ["It names no model in this facility's catalog"],
        f"/security/advisories/{pumps.number}/": ["DEMO-ADV-0004-N", pump, rules.NO_RESPONSE_WORDS, f"Update open to {seed_demo.PUMP_NEWER}, the fix"],
        f"/security/advisories/{beds.number}/": [rules.MODELS_TO_CONFIRM_WORDS, "T-0101"],
        f"/security/advisories/{pumps.number}/devices.csv": [pump, rules.NO_RESPONSE_WORDS],
        f"/security/decisions/{not_in_use.number}/": [monitors.number, rules.HICP_ADVISORY_DECISIONS],
        f"/security/decisions/{off_decision.number}/": [c_arms.number, off_decision.asset.tag],
        f"/security/decisions/new/?advisory={pumps.number}&release={older.pk}": [f"prefilled from {pumps_support.number}"],
        f"/equipment/{pump}/?tab=security": [pumps.number, rules.NO_RESPONSE_WORDS],
        "/equipment/T-0101/?tab=security": [beds.number],
        "/api/v1/security-advisories/": [monitors.number, beds.number, "DEMO-ADV-0005"],
        f"/api/v1/security-advisories/{pumps.number}/": [pumps.number, pump],
        "/api/v1/security-decisions/?applies_to=advisory": [not_in_use.number, off_decision.number],
    }
    never = {"/api/v1/security-decisions/?applies_to=advisory": [pumps_support.number]}
    return list(shows), {"shows": shows, "never": never}


def _advisory_pending(client, url: str) -> bool:
    """A slice 40 page not built here yet: an Advisories tab page still the scaffold's stub, or an API route not registered (as
    _pending); a decision door not keyed by need yet (wave 2B: views_cyber_decisions.SCOPE_PARAMS without "advisory"), Record decision
    without its prefill (forms_cyber_decisions.prefill_initial); the drawer without the Security tab's Advisories section
    (web/_cyber_advisory_device.html); the decisions API without its ?applies_to= filter (wave 2C)."""
    path = urlsplit(url).path
    if path.startswith("/security/decisions/"):
        from apps.web import forms_cyber_decisions, views_cyber_decisions

        if "advisory" not in views_cyber_decisions.SCOPE_PARAMS:
            return True
        if path.endswith("/new/") and not hasattr(forms_cyber_decisions, "prefill_initial"):
            return True
    if "tab=security" in url:
        try:
            get_template("web/_cyber_advisory_device.html")
        except TemplateDoesNotExist:
            return True
    if "applies_to=" in url:
        r = client.get(url)
        data = r.json() if r.status_code == 200 else []
        rows = data.get("results", []) if isinstance(data, dict) else data
        if rows and "advisory" not in rows[0]:  # a decision's advisory (wave 2C) not served yet, nor its filter
            return True
    return _pending(client, url)


def _read_advisory_pages(client, seeded) -> list:
    """Slice 40's seeded pages as Kim, every door read with rules.DECISION_NEVER_WORDS and the drawer with rules.NEVER_WORDS (it carries
    Equipment's own PM words). Returns the pages still pending."""
    urls, expect = _advisory_pages(seeded)
    return _read(client, urls, expect, pending=_advisory_pending,
                 words=lambda url: rules.NEVER_WORDS if "tab=security" in url else rules.DECISION_NEVER_WORDS)


def test_the_seeded_advisory_pages_show_the_advisories_and_never_say_the_words(client, seeded):
    """Slice 40: the Advisories tab (Needs attention, the list with each one's status words), each advisory's page (its sources, model
    rows, device lines and what answers them, its decisions), the devices CSV, the advisory decisions' pages, Record decision's prefill
    from the pumps' support decision, the drawer's Advisories section, and the APIs. Wave 2's: pending until merged."""
    client.force_login(User.objects.get(username="kim@riverside.example"))
    pending = _read_advisory_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 40 wave 2's pages are not merged here yet: {pending}")


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
    assert not pending, f"pages not built: {pending}"


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
    assert not pending, f"pages not built: {pending}"


@needs_postgres
def test_the_seeded_advisories_read_the_same_under_the_policies(client, seeded):
    """Slice 40: as the runtime role the advisories' readers (each one's status words for both readers, its Checks and Close refusal;
    every device's line and what answers it; the Advisories tab's Needs attention; the badge's two halves; the advisory decisions'
    sentences and Checks) read the seeded rows exactly as the owner does, and the advisory pages answer."""
    def read():
        today = timezone.localdate()
        kim = User.objects.get(username="kim@riverside.example")
        ads = _advisories()
        seen = rules.advisory_counts(ads, today=today, devices_visible=True)
        view = rules.advisory_counts(ads, today=today, devices_visible=False)
        reading = rules.advisory_reading(ads, today)
        return ([(a.number, seen[a.pk].words, view[a.pk].words, [c.words for c in rules.advisory_checks(a, today=today, devices_visible=True)],
                  advisories.close_refusal(a, today, user=kim)) for a in ads],
                sorted((ln.row.advisory.number, ln.asset.tag, ln.kind, ln.words) for ln in reading.lines),
                sorted((a.line.row.advisory.number, a.line.asset.tag, a.kind, tuple(r.words for r in a.responses))
                       for a in reading.answers.values()),
                [(h.key, h.count, [r.words for r in h.rows]) for h in rules.advisory_attention(today, user=kim).headings],
                rules.badge(today), rules.advisory_badge(today),
                [(d.number, rules.decision_words(d, today), [c.words for c in rules.decision_checks(d, today)],
                  [(dd.asset.tag, dd.words(today)) for dd in rules.decision_devices(d, today)]) for d in _advisory_decisions()])

    with tenant_context(seeded):
        before = read()
    assert len(before[0]) == 5 and len(before[1]) > 100 and len(before[6]) == 2
    assert [key for key, _n, _rows in before[3]] == ["to_confirm", "no_response", "temporary", "not_confirmed"]
    assert (before[4], before[5]) == ({"count": 1, "hot": True}, {"count": 1})
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    with tenant_context(seeded):
        assert read() == before
    pending = _read_advisory_pages(client, seeded)
    if pending:
        pytest.xfail(f"slice 40 wave 2's pages are not merged here yet: {pending}")


@needs_postgres
def test_advisories_written_under_the_policies(seeded):
    """Slice 40 (critique40_locks S9, at the services: the doors' own walk comes with wave 2's screens): as the runtime role the advisory
    writers and a decision for an advisory's devices write this facility's rows only, each stamped with its tenant and passing WITH CHECK,
    with their history; the other facility's rows untouched. Record advisory, Add source, Set models with the fix, Record decision for
    every pump of ours, Close."""
    from apps.cybersecurity.models import AdvisoryModel, AdvisoryRelease

    north = Tenant.objects.exclude(pk=seeded.pk).get()

    def counts(tenant) -> dict:
        with tenant_context(tenant):
            return {"advisories": SecurityAdvisory.objects.count(), "sources": AdvisorySource.objects.count(),
                    "rows": AdvisoryModel.objects.count(), "releases": AdvisoryRelease.objects.count(),
                    "decisions": DeviceSecurityDecision.objects.count(),
                    "history": SecurityAdvisory.history.filter(tenant_id=tenant.pk).count()
                    + AdvisorySource.history.filter(tenant_id=tenant.pk).count()}

    before = {t.slug: counts(t) for t in (seeded, north)}
    as_app_role()
    with tenant_context(seeded):
        today = timezone.localdate()
        rob = manager()
        pump_model = DeviceModel.objects.get(model=PUMP)
        older, newer = (SoftwareRelease.objects.get(device_model=pump_model, version=v) for v in (seed_demo.PUMP_OLDER, seed_demo.PUMP_NEWER))
        ad = advisories.record_advisory(received_on=today, cves="CVE-2026-0001", source="manufacturer_bulletin", reference="DEMO-ADV-9001",
                                        by=rob, today=today)
        advisories.add_source(ad, source="other", reference="DEMO-ADV-9001-N", by=rob, today=today)
        advisories.set_model(ad, device_model=pump_model, versions="listed", release_ids=[older], remedy="release", fix_release=newer,
                             confirm=True, by=rob, today=today)
        decision = decisions.record_decision(device_model=pump_model, applies_to=AppliesTo.ADVISORY, advisory=ad,
                                             response="keep_with_controls", controls=["traffic_restricted"], accepted_by="ce_leader",
                                             by=rob, today=today)
        advisories.close(ad, by=rob, today=today)
        ad.refresh_from_db()
        assert (ad.status, decision.advisory_id) == ("closed", ad.pk)
        written = [ad, decision, *AdvisorySource.objects.filter(advisory=ad), *AdvisoryModel.objects.filter(advisory=ad),
                   *AdvisoryRelease.objects.filter(advisory=ad)]
        assert len(written) == 6 and {row.tenant_id for row in written} == {seeded.pk}
    after = {t.slug: counts(t) for t in (seeded, north)}
    assert after[north.slug] == before[north.slug]  # the other facility: nothing written, nothing changed
    mine, was = after[seeded.slug], before[seeded.slug]
    assert {k: mine[k] - was[k] for k in ("advisories", "sources", "rows", "releases", "decisions")} == {
        "advisories": 1, "sources": 2, "rows": 1, "releases": 1, "decisions": 1}
    assert mine["history"] - was["history"] >= 5, (was, mine)


@needs_postgres
def test_the_advisory_readers_under_the_policies(seeded):
    """Slice 40 (critique40_locks S9): the other facility's advisory naming a model of the same name (the North Campus's pumps), with a
    reference and a number ours carry too, never reaches this facility's readers as the runtime role: the lines, the counts, Needs
    attention, and the badge's advisory half read exactly as the owner reads them, and its row is refused in words by id
    (services._locked_advisory, lock-free and locked), while its number names ours."""
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from apps.cybersecurity import services

    north = Tenant.objects.exclude(pk=seeded.pk).get()
    with tenant_context(north):
        today = timezone.localdate()
        theirs = advisories.record_advisory(received_on=today, cves="CVE-2026-0001", source="manufacturer_bulletin",
                                            reference="DEMO-ADV-0004", by=None, today=today)
        advisories.set_model(theirs, device_model=DeviceModel.objects.get(model=PUMP), versions="all", remedy="no_fix_yet", by=None,
                             today=today)

    def read():
        kim = User.objects.get(username="kim@riverside.example")
        found = SecurityAdvisory.objects.all()
        reading = rules.advisory_reading(found, today)
        counts = rules.advisory_counts(found, today=today, devices_visible=True)
        return ({a.number for a in found}, sorted((ln.row.advisory.number, ln.asset.tag, ln.kind) for ln in reading.lines),
                sorted((a.number, counts[a.pk].words) for a in found),
                [(h.key, h.count, [r.words for r in h.rows]) for h in rules.advisory_attention(today, user=kim).headings],
                rules.advisory_badge(today), {ln.asset.tenant_id for ln in reading.lines})

    with tenant_context(seeded):
        before = read()
    assert theirs.number in before[0] and before[4] == {"count": 1} and before[5] == {seeded.pk}
    as_app_role()
    with tenant_context(seeded):
        assert read() == before
        assert not SecurityAdvisory.objects.filter(pk=theirs.pk).exists()
        for lock in (False, True):
            with pytest.raises(ValidationError) as refused, transaction.atomic():
                services._locked_advisory(theirs.pk, lock=lock)
            assert refused.value.message_dict == {"advisory": [services.CHOOSE_ADVISORY]}
        ours = services._locked_advisory(theirs.number, lock=False)
        assert ours.pk != theirs.pk and ours.tenant_id == seeded.pk
