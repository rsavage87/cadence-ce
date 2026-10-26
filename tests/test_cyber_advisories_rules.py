"""
Slice 40 wave 1A, the advisories' readers in apps.cybersecurity.rules, on SQLite and PostgreSQL:

- advisory_counts: the line kinds and update counts every Device security reader sees; the answer kinds, decisions past review, and
  devices named since only for a reader of devices (can_view_devices); a View-only reader reads no decision-derived count or word.
- status_words: an open advisory's words in order (models to confirm, no response, a decision past review, patches on another list, an
  update not confirmed, updates open, ready to close), not applicable, closed with what holds now, entered in error.
- close_blockers and not_applicable_blocks: the one predicate each, Close's and Not applicable's, also "devices named since".
- advisory_checks: Release added since (by identity), patches on another list, closed with models to confirm, devices named since.
- advisory_attention: each heading, its rows' words and links, and who reads it; advisory_badge.
- Fixed queries as advisories, model rows, devices, and decisions grow (before == after), and the badge's one query.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import advisories, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryRemedy,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    DecisionResponse,
    DeviceSecurityDecision,
    InstalledBy,
    NotApplicableReason,
    SecurityAdvisory,
    SoftwareRelease,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.equipment.models import Asset, DeviceModel, Ownership
from apps.workorders.models import WorkOrder, WoStatus

TODAY = timezone.localdate()
YY = f"{TODAY:%y}"
DAY = timedelta(days=1)
LISTED, ALL, NOT_STATED = AdvisoryVersions.LISTED, AdvisoryVersions.ALL, AdvisoryVersions.NOT_STATED


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def user_with(tenant, slug, levels) -> User:
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password=None, tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User")


def decision(adv, **fields) -> DeviceSecurityDecision:
    n = DeviceSecurityDecision.objects.count() + 1
    values = {"number": f"SD-{YY}-{n:04d}", "applies_to": AppliesTo.ADVISORY, "advisory": adv, "advisory_changes": adv.changes,
              "reasons": [], "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER,
              "decided_on": TODAY - 10 * DAY, "review_by": TODAY + 300 * DAY, **fields}
    return DeviceSecurityDecision.objects.create(**values)


def end(d) -> None:
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(ended_on=TODAY, end_reason="no_longer_needed")


def close_directly(adv, *, on=None, by=None) -> None:
    SecurityAdvisory.objects.filter(pk=adv.pk).update(status=AdvisoryStatus.CLOSED, status_on=on or TODAY - DAY, status_by=by)
    adv.refresh_from_db()


def counts(adv, *, devices_visible=True):
    return rules.advisory_counts([adv], today=TODAY, devices_visible=devices_visible)[adv.pk]


@pytest.fixture
def floor(ctx, dept, pump_model):
    """As the scaffold's: P-1, P-2 of ours on 5.2 (demo), P-3 on 5.4 (demo), P-4 on 5.6 (demo), P-5 with no release recorded; the rental
    T-1 on 5.2 (demo), due back in 20 days."""
    r52, r54, r56 = release(pump_model, "5.2 (demo)"), release(pump_model, "5.4 (demo)"), release(pump_model, "5.6 (demo)")
    d = {}
    for tag, rel in (("P-1", r52), ("P-2", r52), ("P-3", r54), ("P-4", r56), ("P-5", None)):
        d[tag] = Asset.objects.create(tag=tag, device_model=pump_model, department=dept)
        if rel is not None:
            facts(d[tag], rel)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=TODAY + 20 * DAY)
    facts(d["T-1"], r52)
    return SimpleNamespace(model=pump_model, r52=r52, r54=r54, r56=r56, d=d, dept=dept)


@pytest.fixture
def readers(tenant):
    """Device security View alone; View with Equipment View (no Work orders); Approve with Equipment and Work orders (can_decide, can
    open updates); Edit with Equipment View (IT's analyst: opens nothing without Work orders Edit)."""
    return SimpleNamespace(
        view_only=user_with(tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW}),
        devices=user_with(tenant, "devices", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}),
        decider=user_with(tenant, "decider", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.VIEW,
                                              Module.WORKORDERS: Level.APPROVE}),
        it=user_with(tenant, "it-security", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW}),
        none=user_with(tenant, "no-cyber", {Module.EQUIPMENT: Level.VIEW}))


# --- counts and status words ------------------------------------------------------------------------------------------------------------

def test_counts_split_by_reader(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    update_wo(floor.d["P-1"], floor.r56)
    decision(a, asset=floor.d["P-2"])
    seen = counts(a, devices_visible=False)
    assert seen.lines == {rules.NAMED: 3, rules.LINE_NOT_RECORDED: 1, rules.ON_FIX: 1, rules.OTHER_RELEASE: 1}
    assert seen.updates == {rules.UPDATE_OPEN: 1, rules.UPDATE_NOT_CONFIRMED: 0}
    assert (seen.answers, seen.reviews_due, seen.named_since, seen.words) == (None, 0, 0, "Open")  # never decision-derived
    assert seen.rows[0].answers is None and seen.rows[0].lines == seen.lines
    full = counts(a)
    assert full.answers == {rules.UPDATE_OPEN: 1, rules.UPDATE_NOT_CONFIRMED: 0, rules.PATCHES_RECORDED: 0, rules.PATCHES_OTHER_LIST: 0,
                            rules.ANSWER_DECIDED: 1, rules.ANSWER_REVIEW_DUE: 0, rules.NO_RESPONSE: 2}
    assert full.words == "Open: 2 devices with no response recorded"
    # a decision before an update hides nothing from View's update count: the update is counted from the responses shown
    decision(a, asset=floor.d["P-1"])
    assert counts(a, devices_visible=False).updates[rules.UPDATE_OPEN] == 1 and counts(a).answers[rules.UPDATE_OPEN] == 0


def test_an_open_advisorys_status_words_in_order(ctx, floor):
    unconfirmed = make_advisory(reference="DEMO-ADV-0001")
    model_row(unconfirmed, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET, confirm=False)
    assert counts(unconfirmed).words == counts(unconfirmed, devices_visible=False).words == "Models to confirm"
    nothing = make_advisory(reference="DEMO-ADV-0002", models_confirmed_on=TODAY)  # reopened from "no model here": confirmed, no row
    assert counts(nothing).words == "Open"
    a = make_advisory(reference="DEMO-ADV-0003")
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)

    def words():
        assert counts(a, devices_visible=False).words == "Open"
        return rules.status_words(SecurityAdvisory.objects.get(pk=a.pk), devices_visible=True)

    assert words() == "Open: 4 devices with no response recorded"
    p5 = decision(a, asset=floor.d["P-5"])
    t1 = decision(a, asset=floor.d["T-1"])
    wo1, _u = update_wo(floor.d["P-1"], floor.r56)
    wo2, _u = update_wo(floor.d["P-2"], floor.r56)
    assert words() == "Open: 2 updates open"
    WorkOrder.objects.filter(pk=wo2.pk).update(status=WoStatus.COMPLETED, completed_on=TODAY)
    assert words() == "Open: 1 update done, version not confirmed"
    DeviceSecurityDecision.objects.filter(pk=t1.pk).update(review_by=TODAY - DAY)
    assert words() == "Open: 1 device under a decision past its review date"
    DeviceSecurityDecision.objects.filter(pk=p5.pk).update(review_by=TODAY - DAY)
    assert words() == "Open: 2 devices under decisions past their review date"
    facts(floor.d["P-3"], floor.r52)  # named now, nothing answers it
    assert words() == "Open: 1 device with no response recorded"
    facts(floor.d["P-3"], floor.r54)
    DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.ADVISORY, review_by__lt=TODAY).update(review_by=TODAY + DAY)
    decision(a, asset=floor.d["P-1"])
    decision(a, asset=floor.d["P-2"])  # interim decisions beside the updates
    assert words() == "Open: ready to close"
    assert advisories.close_refusal(a, TODAY) is None  # the words and Close agree
    patches = make_advisory(reference="DEMO-ADV-0004", received_on=TODAY - 5 * DAY)
    model_row(patches, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        SoftwareUpdate.objects.create(asset=floor.d[tag], outcome=UpdateOutcome.PATCHES, applied_on=TODAY - DAY,
                                      reference="DEMO-PATCH-2026-10" if tag != "P-3" else "DEMO-PATCH-2026-09",
                                      installed_by=InstalledBy.MANUFACTURER)
    assert counts(patches).words == "Open: 1 device with patches naming another list"
    assert wo1.pk


def test_closed_not_applicable_and_entered_in_error_words(ctx, floor, make_user):
    kim = make_user("director")
    a = make_advisory()
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        decision(a, asset=floor.d[tag])
    close_directly(a, on=TODAY - 3 * DAY, by=kim)
    day = rules.day_words(TODAY - 3 * DAY)
    assert counts(a).words == counts(a, devices_visible=False).words == f"Closed ({kim}, {day})"
    Asset.objects.create(tag="P-6", device_model=floor.model, department=floor.dept)  # named since
    DeviceSecurityDecision.objects.filter(asset=floor.d["P-1"]).update(review_by=TODAY - DAY)
    SecurityAdvisory.objects.filter(pk=a.pk).update(models_confirmed_on=None)
    a.refresh_from_db()
    full = counts(a)
    assert (full.named_since, full.reviews_due) == (1, 1)
    assert full.words == (f"Closed ({kim}, {day}) · 1 device named since · models to confirm since a revision · 1 decision past its "
                          "review date")
    assert counts(a, devices_visible=False).words == f"Closed ({kim}, {day}) · models to confirm since a revision"
    na = make_advisory(reference="DEMO-ADV-0002", status=AdvisoryStatus.NOT_APPLICABLE,
                       not_applicable_reason=NotApplicableReason.NO_MODEL_HERE, status_on=TODAY)
    assert counts(na).words == f"Not applicable here ({rules.day_words(TODAY)}): It names no model in this facility's catalog"
    gone = make_advisory(reference="DEMO-ADV-0003", removed_on=TODAY)
    model_row(gone, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    got = counts(gone)
    assert (got.words, got.rows, got.lines[rules.NAMED]) == ("Entered in error", (), 0)


# --- the predicates ---------------------------------------------------------------------------------------------------------------------

def test_one_predicate_for_close_and_for_devices_named_since(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    reading = rules.advisory_reading([a], TODAY)
    blockers = rules.close_blockers(reading.answers)
    assert list(blockers) == [rules.NO_RESPONSE] and [x.line.asset.tag for x in blockers[rules.NO_RESPONSE]] == ["P-1", "P-2", "P-5", "T-1"]
    assert rules.not_applicable_blocks("no_model_here", reading.rows, reading.lines)
    assert rules.not_applicable_blocks("none_here", reading.rows, reading.lines)
    assert rules.not_applicable_blocks("releases_not_here", reading.rows, reading.lines)
    assert rules.not_applicable_blocks("none_here", [], []) and not rules.not_applicable_blocks("no_model_here", [], [])
    for tag in ("P-1", "P-2", "T-1"):
        facts(floor.d[tag], floor.r54)
    facts(floor.d["P-5"], floor.r56)
    reading = rules.advisory_reading([a], TODAY)
    assert rules.close_blockers(reading.answers) == {}
    assert not rules.not_applicable_blocks("releases_not_here", reading.rows, reading.lines)
    na = make_advisory(reference="DEMO-ADV-0002", status=AdvisoryStatus.NOT_APPLICABLE,
                       not_applicable_reason=NotApplicableReason.RELEASES_NOT_HERE, status_on=TODAY)
    model_row(na, floor.model, releases=[floor.r52], fix=floor.r56)
    reading = rules.advisory_reading([na], TODAY)
    assert reading.answers == {} and len(reading.lines) == 6  # a not-applicable advisory has no needs, only lines


# --- Checks -----------------------------------------------------------------------------------------------------------------------------

def test_a_release_added_since_by_identity(ctx, floor):
    gone = release(floor.model, "4.9 (demo)", removed_on=TODAY)  # entered in error when the models were set
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    assert rules.advisory_checks(a, today=TODAY, devices_visible=False) == []
    r58 = release(floor.model, "5.8 (demo)")
    SoftwareRelease.objects.filter(pk=gone.pk).update(removed_on=None)  # restored: never seen, whatever its created_at
    checks = rules.advisory_checks(a, today=TODAY, devices_visible=False)
    assert [(c.key, c.words) for c in checks] == [
        (rules.CHECK_RELEASE_ADDED, "Releases added since: 4.9 (demo), 5.8 (demo), not listed or named as the fix")]
    advisories.set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r52, r58], remedy="release",
                         fix_release=floor.r56, by=None, today=TODAY)
    assert rules.advisory_checks(a, today=TODAY, devices_visible=False) == []  # seen now
    other = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8100", description="Pump module", category="Infusion pumps")
    r1 = release(other, "1.0 (demo)")
    model_row(a, other, releases=[r1], remedy=AdvisoryRemedy.MITIGATIONS)
    release(other, "1.1 (demo)")
    assert [c.words for c in rules.advisory_checks(a, today=TODAY, devices_visible=False)] == [
        "Release added since: 1.1 (demo), not listed or named as the fix (Alaris 8100)"]
    whole = make_advisory(reference="DEMO-ADV-0002")
    model_row(whole, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    release(floor.model, "5.9 (demo)")
    assert rules.advisory_checks(whole, today=TODAY, devices_visible=False) == []  # every release: none is added since


def test_the_other_checks(ctx, floor):
    a = make_advisory(received_on=TODAY - 5 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    SoftwareUpdate.objects.create(asset=floor.d["P-3"], outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-09",
                                  applied_on=TODAY - DAY, installed_by=InstalledBy.MANUFACTURER)
    checks = rules.advisory_checks(a, today=TODAY, devices_visible=True)
    assert [(c.key, c.tags) for c in checks] == [(rules.CHECK_OTHER_LIST, ("P-3",))]
    assert checks[0].words == (f"P-3: The manufacturer's patches recorded {rules.day_words(TODAY - DAY)} (DEMO-PATCH-2026-09); the "
                               "advisory names DEMO-PATCH-2026-10")
    assert rules.advisory_checks(a, today=TODAY, devices_visible=False) == []  # a device's patches: readers of devices only
    shut = make_advisory(reference="DEMO-ADV-0002", models_confirmed_on=None)
    model_row(shut, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET, confirm=False)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5"):
        decision(shut, asset=floor.d[tag])
    close_directly(shut, on=TODAY - 2 * DAY)
    checks = rules.advisory_checks(shut, today=TODAY, devices_visible=True)
    assert [(c.key, c.words) for c in checks] == [
        (rules.CHECK_TO_CONFIRM, rules.TO_CONFIRM_CHECK_WORDS),
        (rules.CHECK_NAMED_SINCE, f"1 device named since it was closed on {rules.day_words(TODAY - 2 * DAY)}: T-1")]
    assert [c.key for c in rules.advisory_checks(shut, today=TODAY, devices_visible=False)] == [rules.CHECK_TO_CONFIRM]
    na = make_advisory(reference="DEMO-ADV-0003", status=AdvisoryStatus.NOT_APPLICABLE,
                       not_applicable_reason=NotApplicableReason.RELEASES_NOT_HERE, status_on=TODAY - 4 * DAY)
    model_row(na, floor.model, releases=[floor.r54], remedy=AdvisoryRemedy.MITIGATIONS)
    assert [c.words for c in rules.advisory_checks(na, today=TODAY, devices_visible=True)] == [
        f"2 devices named since it was marked not applicable on {rules.day_words(TODAY - 4 * DAY)}: P-3, P-5"]  # P-5: none recorded
    assert rules.advisory_checks(SecurityAdvisory(number="AD-00-0000", received_on=TODAY, removed_on=TODAY), today=TODAY,
                                 devices_visible=True) == []


# --- Needs attention --------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def scene(floor, vent_model, make_user):
    """An advisory for each heading:
    - AD-1 open, models to confirm: the ventilators, every release, no fix yet (V-1 ours);
    - AD-2 open: the pumps on 5.2 (demo), the fix 5.6 (demo); P-1 an update open to the fix, P-2 one done with the version not confirmed,
      P-5 no release recorded, T-1 the rental; 5.8 (demo) added since;
    - AD-3 open: the ventilators, the patches DEMO-PATCH-2026-10, V-1 on another list;
    - AD-4 closed, the pumps not stated, each decided, then P-6 added (named since); AD-5 closed with its models to confirm;
    - AD-6 not applicable (releases not here), the pumps on 5.4 (demo): P-3 is on it;
    - an advisory decision of AD-2 for P-1 past its review."""
    kim = make_user("director")
    v1 = Asset.objects.create(tag="V-1", device_model=vent_model, department=floor.dept)
    a1 = make_advisory(reference="DEMO-ADV-0001", received_on=TODAY - 9 * DAY)
    model_row(a1, vent_model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET, confirm=False)
    a2 = make_advisory(reference="DEMO-ADV-0002", received_on=TODAY - 8 * DAY)
    model_row(a2, floor.model, releases=[floor.r52], fix=floor.r56)
    r58 = release(floor.model, "5.8 (demo)")
    _wo1, _u = update_wo(floor.d["P-1"], floor.r56)
    wo2, _u = update_wo(floor.d["P-2"], floor.r56)
    WorkOrder.objects.filter(pk=wo2.pk).update(status=WoStatus.COMPLETED, completed_on=TODAY - DAY)
    a3 = make_advisory(reference="DEMO-ADV-0003", received_on=TODAY - 7 * DAY)
    model_row(a3, vent_model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    SoftwareUpdate.objects.create(asset=v1, outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-09", applied_on=TODAY - DAY,
                                  installed_by=InstalledBy.MANUFACTURER)
    a4 = make_advisory(reference="DEMO-ADV-0004", received_on=TODAY - 6 * DAY)
    model_row(a4, floor.model, versions=NOT_STATED, remedy=AdvisoryRemedy.MITIGATIONS)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        decision(a4, asset=floor.d[tag])
    close_directly(a4, on=TODAY - 5 * DAY, by=kim)
    p6 = Asset.objects.create(tag="P-6", device_model=floor.model, department=floor.dept)
    a5 = make_advisory(reference="DEMO-ADV-0005", received_on=TODAY - 5 * DAY)
    close_directly(a5, on=TODAY - 4 * DAY)
    a6 = make_advisory(reference="DEMO-ADV-0006", received_on=TODAY - 4 * DAY, status=AdvisoryStatus.NOT_APPLICABLE,
                       not_applicable_reason=NotApplicableReason.RELEASES_NOT_HERE, status_on=TODAY - 3 * DAY, models_confirmed_on=TODAY)
    model_row(a6, floor.model, releases=[floor.r54], remedy=AdvisoryRemedy.MITIGATIONS)
    due = decision(a2, asset=floor.d["P-1"], decided_on=TODAY - 400 * DAY, review_by=TODAY - DAY)
    return SimpleNamespace(a1=a1, a2=a2, a3=a3, a4=a4, a5=a5, a6=a6, v1=v1, p6=p6, wo2=wo2, due=due, r58=r58, kim=kim)


def _rows(att) -> dict:
    return {h.key: h for h in att.headings}


def test_needs_attention_lists_each_heading_in_order(scene, floor):
    att = rules.advisory_attention(TODAY, user=None)
    assert [h.key for h in att.headings] == list(rules.ADVISORY_ATTENTION_HEADINGS)
    h = _rows(att)
    assert [r.words for r in h["to_confirm"].rows] == [f"{scene.a1.number} · DEMO-ADV-0001 · Hamilton-G5"]
    assert h["to_confirm"].rows[0].link == rules.Link("web:security_advisory", (scene.a1.number,))
    # ours: P-5 and P-6 (P-1's update is open to the fix and its decision is past review; P-2 waits for its version)
    assert [(r.words, r.count, r.tags) for r in h["no_response"].rows] == [
        (f"{scene.a1.number} · Hamilton-G5: 1 device of ours; the advisory names every release of the Hamilton-G5", 1, ("V-1",)),
        (f"{scene.a2.number} · Alaris 8015 PCU: 2 devices of ours; 2 with no release recorded", 2, ("P-5", "P-6"))]
    assert h["no_response"].rows[1].record == rules.Link("web:security_decision_new", (), (("advisory", scene.a2.number),
                                                                                          ("model", floor.model.pk)))
    temporary = h["temporary"].rows[0]
    assert temporary.words == (f"T-1 (rental, Summit Medical Rentals; due back {rules.day_words(TODAY + 20 * DAY)}): on 5.2 (demo), a "
                               f"release the advisory names ({scene.a2.number}); its owner installs updates: ask the owner to update it, "
                               "or return it")
    assert temporary.record == rules.Link("web:security_decision_new", (), (("advisory", scene.a2.number), ("asset", "T-1")))
    assert temporary.open_update == rules.Link("web:cybersecurity_update_open", ("T-1",), (("advisory", scene.a2.number),
                                                                                           ("from", "advisory")))
    assert temporary.link == rules.Link("web:asset", ("T-1",), (("tab", "security"),))
    [waiting] = h["not_confirmed"].rows
    assert waiting.words == (f"{scene.a2.number} · P-2: Update done {rules.day_words(TODAY - DAY)}, version not confirmed: Confirm version "
                             f"({scene.wo2.number})")
    assert waiting.work_order == rules.Link("web:wo", (scene.wo2.number,))
    assert [r.words for r in h["other_list"].rows] == [
        f"{scene.a3.number} · V-1: The manufacturer's patches recorded {rules.day_words(TODAY - DAY)} (DEMO-PATCH-2026-09); the advisory "
        "names DEMO-PATCH-2026-10"]
    assert [(r.words, r.tags) for r in h["named_since"].rows] == [
        (f"{scene.a4.number} · DEMO-ADV-0004 · Alaris 8015 PCU: closed {rules.day_words(TODAY - 5 * DAY)}; 1 device named since", ("P-6",)),
        (f"{scene.a6.number} · DEMO-ADV-0006 · Alaris 8015 PCU: not applicable here since {rules.day_words(TODAY - 3 * DAY)}; 3 devices "
         "named since", ("P-3", "P-5", "P-6"))]
    assert [r.words for r in h["changed"].rows] == [
        f"{scene.a5.number} · DEMO-ADV-0005 · no model of this facility's catalog: closed {rules.day_words(TODAY - 4 * DAY)}"]
    assert [r.words for r in h["checks"].rows] == [f"{scene.a2.number} · Release added since: 5.8 (demo), not listed or named as the fix"]
    [due] = h["decisions_due"].rows
    assert (due.words, due.count) == ("1 advisory decision past its review date", 1)
    assert due.link == rules.Link("web:security_decisions", (), (("why", "advisory"), ("state", "review_due"), ("here", "1")))
    for heading in att.headings:
        assert heading.count == sum(r.count for r in heading.rows)
        assert not never(heading.words) and not [r.words for r in heading.rows if never(r.words)]


def test_needs_attention_by_reader(scene, readers):
    assert rules.advisory_attention(TODAY, user=readers.none).headings == ()
    view = rules.advisory_attention(TODAY, user=readers.view_only)
    assert [h.key for h in view.headings] == ["to_confirm", "changed", "checks"]  # nothing read from a device or a decision
    devices = _rows(rules.advisory_attention(TODAY, user=readers.devices))
    assert list(devices) == list(rules.ADVISORY_ATTENTION_HEADINGS)
    assert all(r.record is None and r.open_update is None for h in devices.values() for r in h.rows)
    assert devices["not_confirmed"].rows[0].work_order is None  # no Work orders View
    decider = _rows(rules.advisory_attention(TODAY, user=readers.decider))
    assert decider["temporary"].rows[0].record is not None and decider["temporary"].rows[0].open_update is not None
    assert decider["not_confirmed"].rows[0].work_order is not None
    it = _rows(rules.advisory_attention(TODAY, user=readers.it))
    assert it["temporary"].rows[0].record is None and it["temporary"].rows[0].open_update is None  # neither decides nor opens


def test_no_fix_asks_the_owner_and_offers_no_update(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, versions=NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET)
    [row] = _rows(rules.advisory_attention(TODAY, user=None))["temporary"].rows
    assert row.words.endswith("its owner installs updates: ask the owner about it, or return it") and row.open_update is None
    assert "the advisory names the Alaris 8015 PCU without stating releases" in row.words
    wo, _u = update_wo(floor.d["T-1"], floor.r54)  # an open update that does not answer: Open software update would be refused
    model_row(make_advisory(reference="DEMO-ADV-0002"), floor.model, releases=[floor.r52, floor.r54], fix=floor.r56)
    rows = _rows(rules.advisory_attention(TODAY, user=None))["temporary"].rows
    assert [r.open_update for r in rows] == [None, None] and wo.pk


def test_nothing_listed(ctx, floor):
    att = rules.advisory_attention(TODAY, user=None)
    assert att.empty and att.empty_words == "Nothing listed from what is recorded here."


def test_the_badges_advisory_half(ctx, floor):
    make_advisory(reference="DEMO-ADV-0001")  # open, models to confirm
    make_advisory(reference="DEMO-ADV-0002", models_confirmed_on=TODAY)
    make_advisory(reference="DEMO-ADV-0003", removed_on=TODAY)
    make_advisory(reference="DEMO-ADV-0004", status=AdvisoryStatus.CLOSED, status_on=TODAY)  # its models to confirm: another heading
    with CaptureQueriesContext(connection) as q:
        assert rules.advisory_badge(TODAY) == {"count": 1}
    assert len(q.captured_queries) == 1
    assert not never(rules.ADVISORY_BADGE_TITLE) and not never(rules.BOTH_BADGE_TITLE)


# --- fixed queries ------------------------------------------------------------------------------------------------------------------------

def _grow(floor, vent_model, n: int) -> None:
    """Another advisory naming two models with listed releases and decisions, devices of each model, patches, and an update."""
    a = make_advisory(reference=f"DEMO-ADV-1{n:03d}", received_on=TODAY - n * DAY)
    model_row(a, floor.model, releases=[floor.r52, floor.r54], fix=floor.r56)
    model_row(a, vent_model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-1")
    for i in range(2):
        v = Asset.objects.create(tag=f"V-{n}-{i}", device_model=vent_model, department=floor.dept)
        SoftwareUpdate.objects.create(asset=v, outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-0", applied_on=TODAY,
                                      installed_by=InstalledBy.MANUFACTURER)
        p = Asset.objects.create(tag=f"Q-{n}-{i}", device_model=floor.model, department=floor.dept)
        facts(p, floor.r52)
    decision(a, release=floor.r52, device_model=floor.model, review_by=TODAY - DAY)
    decision(a, asset=floor.d["T-1"], plan="replace_planned", plan_by=TODAY + 400 * DAY)
    if n == 1:
        update_wo(floor.d["P-3"], floor.r56)
    shut = make_advisory(reference=f"DEMO-ADV-2{n:03d}", status=AdvisoryStatus.CLOSED, status_on=TODAY)
    model_row(shut, floor.model, versions=ALL, remedy=AdvisoryRemedy.MITIGATIONS)
    na = make_advisory(reference=f"DEMO-ADV-3{n:03d}", status=AdvisoryStatus.NOT_APPLICABLE, status_on=TODAY,
                       not_applicable_reason=NotApplicableReason.NONE_HERE)
    model_row(na, vent_model, versions=NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET)


def _queries(fn) -> int:
    with CaptureQueriesContext(connection) as q:
        fn()
    return len(q.captured_queries)


def test_every_reader_reads_a_fixed_number_of_queries(ctx, floor, vent_model, readers):
    _grow(floor, vent_model, 1)
    first = SecurityAdvisory.objects.order_by("number").first()

    def measure():
        found = list(SecurityAdvisory.objects.all())
        return {
            "counts": _queries(lambda: rules.advisory_counts(found, today=TODAY, devices_visible=True)),
            "counts_view": _queries(lambda: rules.advisory_counts(found, today=TODAY, devices_visible=False)),
            "counts_qs": _queries(lambda: rules.advisory_counts(SecurityAdvisory.objects.all(), today=TODAY, devices_visible=True)),
            "attention": _queries(lambda: rules.advisory_attention(TODAY, user=None)),
            "attention_view": _queries(lambda: rules.advisory_attention(TODAY, user=readers.view_only)),
            "checks": _queries(lambda: rules.advisory_checks(first, today=TODAY, devices_visible=True)),
            "close": _queries(lambda: advisories.close_refusal(first, TODAY, user=readers.decider)),
            "not_applicable": _queries(lambda: advisories.not_applicable_refusals(first, TODAY)),
        }

    before = measure()
    for n in range(2, 5):
        _grow(floor, vent_model, n)
    after = measure()
    assert before == after
    # advisories, (their people), rows 2, devices 1, profiles 1, updates 1, decisions 1, the fiscal year 1
    assert after["counts"] == 8 and after["counts_view"] == 8
    # advisories, sources, rows 2, devices 1, profiles 1, updates 1, decisions 1, the fiscal year 1, releases 1, decisions past review 1
    assert after["attention"] == 11
    assert after["attention_view"] == 6 + 5  # the reader's six level reads; advisories, sources, rows 2, releases 1
    assert after["checks"] == 8 and after["not_applicable"] == 4
    assert after["close"] == 7 + 2  # rows 2, devices 1, profiles 1, updates 1, decisions 1, the fiscal year 1; the reader's two level reads
