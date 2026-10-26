"""
Slice 40 scaffold: security advisories stay in their facility; their shape and the advisory decisions' hold in the database on SQLite and
PostgreSQL; the history areas need Device security View only (they name models, never devices); the new words never say what Cadence
never says; a link names a public host only; and the one rule both wave 1 agents read: a device's line under a model row (rules.
advisory_lines) and what answers it (rules.answers_row, rules.advisory_answers), in fixed queries.
"""
from datetime import timedelta

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Level, Module, Role
from apps.core.history import AREAS
from apps.cybersecurity import advisories, rules, services
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
    DecisionEnd,
    DecisionResponse,
    DeviceSecurityDecision,
    InstalledBy,
    Network,
    NotApplicableReason,
    SecurityAdvisory,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.equipment.models import Asset, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.workorders.models import WoStatus

TODAY = timezone.localdate
DAY = timedelta(days=1)


def advisory_decision(adv, n=1, **fields) -> DeviceSecurityDecision:
    today = TODAY()
    values = {"number": f"SD-26-{n:04d}", "applies_to": AppliesTo.ADVISORY, "advisory": adv, "advisory_changes": 0, "reasons": [],
              "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER, "decided_on": today,
              "review_by": today + timedelta(days=365), **fields}
    return DeviceSecurityDecision.objects.create(**values)


def refused(make, **fields) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        make(**fields)


@pytest.fixture
def floor(ctx, dept, pump_model):
    """Six pumps: two on 5.2 (listed), one on 5.4 (not named), one on 5.6 (the fix), one with no release recorded, one rental on 5.2."""
    r52, r54, r56 = release(pump_model, "5.2"), release(pump_model, "5.4"), release(pump_model, "5.6")
    made = {}
    for tag, rel in (("P-1", r52), ("P-2", r52), ("P-3", r54), ("P-4", r56), ("P-5", None)):
        a = Asset.objects.create(tag=tag, device_model=pump_model, department=dept)
        if rel is not None:
            facts(a, rel)
        made[tag] = a
    rental = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL)
    facts(rental, r52)
    made["T-1"] = rental

    class Floor:
        pass

    f = Floor()
    f.model, f.r52, f.r54, f.r56, f.d = pump_model, r52, r54, r56, made
    return f


# --- the models -------------------------------------------------------------------------------------------------------------------------

def test_an_advisory_and_its_rows_stay_in_their_facility(ctx, floor, other_tenant):
    a = make_advisory(number="AD-26-0001")
    row = model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    advisory_decision(a, release=floor.r52, device_model=floor.model)
    assert (SecurityAdvisory.objects.count(), AdvisorySource.objects.count(), AdvisoryModel.objects.count(),
            AdvisoryRelease.objects.count()) == (1, 1, 1, 1)
    with tenant_context(other_tenant):
        assert not SecurityAdvisory.objects.exists() and not AdvisorySource.objects.exists()
        assert not AdvisoryModel.objects.exists() and not AdvisoryRelease.objects.exists()
        theirs = make_advisory(number="AD-26-0001", reference="DEMO-ADV-0001")  # the same number and reference in another facility
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        model_row(theirs, model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
        assert SecurityAdvisory.objects.count() == 1
    assert row.releases.count() == 1


def test_the_number_is_one_per_facility(ctx):
    make_advisory(number="AD-26-0001")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_advisory(number="AD-26-0001", reference="DEMO-ADV-0002")


def test_status_reason_and_day_hold_together(ctx):
    make = lambda **f: SecurityAdvisory.objects.create(number=f"AD-26-{SecurityAdvisory.objects.count() + 10:04d}",  # noqa: E731
                                                       received_on=TODAY(), **f)
    make(status=AdvisoryStatus.NOT_APPLICABLE, not_applicable_reason=NotApplicableReason.NONE_HERE, status_on=TODAY())
    make(status=AdvisoryStatus.CLOSED, status_on=TODAY())
    make()  # open
    refused(make, status=AdvisoryStatus.NOT_APPLICABLE, status_on=TODAY())  # no reason
    refused(make, status=AdvisoryStatus.CLOSED, not_applicable_reason=NotApplicableReason.NONE_HERE, status_on=TODAY())
    refused(make, status=AdvisoryStatus.CLOSED)  # no day
    refused(make, status_on=TODAY())  # open with a day


def test_a_live_source_reference_is_one_publication(ctx):
    a, b = make_advisory(number="AD-26-0001", reference="DEMO-ADV-0001"), make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002")
    add = lambda **f: AdvisorySource.objects.create(advisory=b, **f)  # noqa: E731
    refused(add, source=AdvisorySourceKind.MANUFACTURER_BULLETIN, reference="demo-adv-0001", reference_key="demo-adv-0001")
    add(source=AdvisorySourceKind.OTHER, reference="DEMO-ADV-0001", reference_key="demo-adv-0001")  # another source kind
    add(source=AdvisorySourceKind.OTHER)  # no reference: never collides
    add(source=AdvisorySourceKind.OTHER)
    first = a.sources.get()
    AdvisorySource.objects.filter(pk=first.pk).update(advisory_removed=True)  # its advisory entered in error frees the reference
    add(source=AdvisorySourceKind.MANUFACTURER_BULLETIN, reference="DEMO-ADV-0001", reference_key="demo-adv-0001")
    refused(add, source=AdvisorySourceKind.MANUFACTURER_BULLETIN, reference="DEMO-ADV-0001", reference_key="demo-adv-0001")


def test_a_model_row_names_a_fix_only_for_listed_releases(ctx, floor):
    a = make_advisory()
    make = lambda **f: AdvisoryModel.objects.create(advisory=a, device_model=floor.model, **f)  # noqa: E731
    refused(make, versions=AdvisoryVersions.LISTED, remedy=AdvisoryRemedy.RELEASE)  # a release remedy without its fix
    refused(make, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.RELEASE, fix_release=floor.r56)  # critique39_scope B3
    refused(make, versions=AdvisoryVersions.LISTED, remedy=AdvisoryRemedy.MITIGATIONS, fix_release=floor.r56)
    refused(make, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.MITIGATIONS, patches_reference="DEMO-PATCH-1")
    make(versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-1")
    refused(make, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)  # one row per model


def test_a_release_is_listed_once_per_row(ctx, floor):
    a = make_advisory()
    row = model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    with pytest.raises(IntegrityError), transaction.atomic():
        AdvisoryRelease.objects.create(advisory=a, advisory_model=row, release=floor.r52)


def test_an_advisory_decision_names_its_advisory_and_one_live_per_scope(ctx, floor):
    a, b = make_advisory(number="AD-26-0001"), make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002")
    p1 = floor.d["P-1"]
    first = advisory_decision(a, 1, asset=p1)
    refused(lambda **f: advisory_decision(a, 9, **f), asset=p1)
    advisory_decision(b, 2, asset=p1)  # another advisory's need
    advisory_decision(a, 3, device_model=floor.model, release=floor.r52)
    refused(lambda **f: advisory_decision(a, 9, **f), device_model=floor.model, release=floor.r52)
    advisory_decision(a, 4, device_model=floor.model)
    refused(lambda **f: advisory_decision(a, 9, **f), device_model=floor.model)
    # a support decision of the same scope is another key
    DeviceSecurityDecision.objects.create(number="SD-26-0005", applies_to=AppliesTo.SUPPORT_ENDED, asset=p1, reasons=["security_updates"],
                                          response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, accepted_by=AcceptedBy.CE_LEADER,
                                          decided_on=TODAY(), review_by=TODAY() + timedelta(days=30))
    DeviceSecurityDecision.objects.filter(pk=first.pk).update(ended_on=TODAY(), end_reason=DecisionEnd.SUPERSEDED)
    advisory_decision(a, 6, asset=p1)  # the ended one no longer counts


def test_the_advisory_is_set_exactly_for_an_advisory_decision_and_not_in_use_here_only_there(ctx, floor):
    a = make_advisory()
    p1 = floor.d["P-1"]
    refused(lambda **f: advisory_decision(a, 9, **f), asset=p1, advisory=None)
    refused(lambda **f: advisory_decision(a, 9, **f), asset=p1, advisory_changes=None)
    refused(lambda **f: advisory_decision(a, 9, **f), asset=p1, applies_to=AppliesTo.SUPPORT_ENDED)  # support with an advisory
    advisory_decision(a, 1, asset=p1, response=DecisionResponse.NOT_IN_USE_HERE)
    with pytest.raises(IntegrityError), transaction.atomic():
        DeviceSecurityDecision.objects.create(number="SD-26-0002", applies_to=AppliesTo.SUPPORT_ENDED, asset=floor.d["P-2"],
                                              reasons=["security_updates"], response=DecisionResponse.NOT_IN_USE_HERE,
                                              accepted_by=AcceptedBy.CE_LEADER, decided_on=TODAY(), review_by=TODAY() + DAY)


# --- areas, levels, words -------------------------------------------------------------------------------------------------------------

def test_the_history_areas_need_device_security_view_only():
    for key, path in (("security_advisories", "cybersecurity.SecurityAdvisory"), ("advisory_sources", "cybersecurity.AdvisorySource"),
                      ("advisory_models", "cybersecurity.AdvisoryModel"), ("advisory_releases", "cybersecurity.AdvisoryRelease")):
        area = AREAS[key]
        assert (area.module, area.also, area.model_path) == ("cybersecurity", (), path)
    assert list(AREAS)[-4:] == ["security_advisories", "advisory_sources", "advisory_models", "advisory_releases"]  # appended last


@pytest.mark.parametrize(("cyber", "corrects"), [(Level.APPROVE, True), (Level.FULL, True), (Level.EDIT, False), (Level.VIEW, False)])
def test_correcting_an_advisory_needs_approve(ctx, make_user, cyber, corrects):
    role = Role.objects.create(name="Custom", slug="custom")
    role.set_levels({Module.CYBERSECURITY: cyber})
    assert cyber_perms.can_correct_advisories(make_user("custom")) is corrects


@pytest.mark.parametrize("slug", ["vendor", "requester"])
def test_a_scoped_user_never_corrects_an_advisory(ctx, make_user, slug):
    Role.objects.get(slug=slug).set_levels({Module.CYBERSECURITY: Level.FULL})
    assert not cyber_perms.can_correct_advisories(make_user(slug))


def _never(text: str) -> list:
    import re

    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def test_the_advisory_words_are_never_words_and_none_of_slice_40s_say_them():
    assert {"affected", "fixed", "exploited", "zero-day", "uncontrolled"} <= set(rules.NEVER_WORDS)
    texts = []
    for choices in (AdvisoryStatus, NotApplicableReason, AdvisorySourceKind, AdvisoryChange, AdvisoryVersions, AdvisoryRemedy, AppliesTo,
                    DecisionResponse):
        texts += [*choices.labels, *choices.values]
    for module in (advisories, rules):
        texts += [v for k, v in vars(module).items() if k.isupper() and isinstance(v, str)]
        texts += [w for k, v in vars(module).items() if k.isupper() and isinstance(v, dict) for w in v.values() if isinstance(w, str)]
    texts += [rules.NOT_IN_USE_HELP, rules.ADVISORY_DECISION_HINT]
    assert [(t, _never(t)) for t in texts if _never(t)] == []


def test_not_in_use_here_reads_as_decided_with_no_plan(ctx, floor):
    a = make_advisory()
    d = advisory_decision(a, asset=floor.d["P-1"], response=DecisionResponse.NOT_IN_USE_HERE, decided_on=TODAY() - DAY,
                          review_by=TODAY() + timedelta(days=364))
    words = rules.decision_words(d, TODAY())
    assert words.startswith(f"Decided {rules.day_words(TODAY() - DAY)} that the part it names is not in use here; review by ")
    assert "Plan" not in words and "Decided by the CE leader" in words and rules.who_verb(d) == "Decided by"
    late = rules.decision_words(d, d.review_by + DAY)
    assert late.startswith(f"Review due since {rules.day_words(d.review_by)}: decided ")


# --- links and the advisory's row -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("link", "ok"), [
    ("https://www.cisa.gov/news-events/ics-medical-advisories/icsma-25-030-01", True),
    ("https://10.1.2.3/notice", False),
    ("https://[2001:db8::1]/notice", False),
    ("https://vendor.example.com:8443/notice", False),
    ("https://intranet/notice", False),
    ("https://share.hospital.local/notice", False),
    ("https://files.corp/notice", False),
    ("http://vendor.example.com/notice", False),
])
def test_a_link_names_a_public_host(link, ok):
    errors = {}
    services._url(link, errors, field="url", words=advisories.LINK_WORDS)
    assert (errors == {}) is ok and (ok or errors == {"url": advisories.LINK_WORDS})


def test_the_advisorys_row_by_number_in_any_case_and_only_this_facilitys(ctx, other_tenant):
    a = make_advisory(number="AD-26-0007")
    assert services._locked_advisory("ad-26-0007", lock=False) == a and services._locked_advisory(str(a.pk), lock=False) == a
    with tenant_context(other_tenant), pytest.raises(ValidationError) as e:
        services._locked_advisory(a.number, lock=False)
    assert e.value.message_dict == {"advisory": [services.CHOOSE_ADVISORY]}


def test_the_lock_free_read_takes_no_lock(ctx, monkeypatch):
    from django.db.models.query import QuerySet

    a = make_advisory()
    locked = []
    real = QuerySet.select_for_update
    monkeypatch.setattr(QuerySet, "select_for_update", lambda self, *x, **k: locked.append(1) or real(self, *x, **k))
    services._locked_advisory(a.number, lock=False)
    assert locked == []
    with transaction.atomic():
        services._locked_advisory(a.number)
    assert locked == [1]


# --- lines ------------------------------------------------------------------------------------------------------------------------------

def _lines(advisory, **kw):
    rows = rules.rows_of([advisory])
    return {(ln.asset.tag, ln.row.device_model_id): ln for ln in rules.advisory_lines(rows, today=TODAY(), **kw)}


def test_a_listed_rows_lines_read_the_fix_before_the_releases_it_names(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    lines = {tag: ln for (tag, _m), ln in _lines(a).items()}
    assert {tag: ln.kind for tag, ln in lines.items()} == {
        "P-1": rules.NAMED, "P-2": rules.NAMED, "P-3": rules.OTHER_RELEASE, "P-4": rules.ON_FIX, "P-5": rules.LINE_NOT_RECORDED,
        "T-1": rules.NAMED}
    assert lines["P-1"].words == "On 5.2, a release the advisory names"
    assert lines["P-3"].words == "On 5.4, a release the advisory does not name"
    assert lines["P-4"].words == "On 5.6, the release the advisory names as the fix"
    assert lines["P-5"].words == "Release not recorded: it may run a release the advisory names"
    assert lines["P-1"].place.startswith("ICU · ") and [t for t, ln in lines.items() if ln.needs_response] == ["P-1", "P-2", "P-5", "T-1"]


@pytest.mark.parametrize(("versions", "words"), [(AdvisoryVersions.ALL, "The advisory names every release of the Alaris 8015 PCU"),
                                                 (AdvisoryVersions.NOT_STATED, "The advisory names the Alaris 8015 PCU without stating releases")])
def test_every_release_or_releases_not_stated_name_every_device(ctx, floor, versions, words):
    a = make_advisory()
    model_row(a, floor.model, versions=versions, remedy=AdvisoryRemedy.NO_FIX_YET)
    lines = _lines(a)
    assert {ln.kind for ln in lines.values()} == {rules.NAMED} and {ln.words for ln in lines.values()} == {words}
    assert len(lines) == 6


def test_a_retired_device_or_another_model_has_no_line(ctx, floor, vent):
    from apps.equipment.models import AssetStatus

    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    Asset.objects.filter(pk=floor.d["P-2"].pk).update(status=AssetStatus.RETIRED)
    assert "P-2" not in {t for t, _m in _lines(a)} and "CE-10001" not in {t for t, _m in _lines(a)}
    assert set(t for t, _m in _lines(a, assets=[floor.d["P-1"]])) == {"P-1"}


# --- answers --------------------------------------------------------------------------------------------------------------------------

def _answers(advisory, today=None):
    rows = rules.rows_of([advisory])
    lines = rules.advisory_lines(rows, today=today or TODAY())
    return {ln.asset.tag: a for (pk, _r), a in rules.advisory_answers(lines, today=today or TODAY()).items()
            for ln in [a.line]}


def _complete(wo, **fields):
    from apps.workorders.models import WorkOrder

    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=TODAY(), **fields)


def test_an_update_answers_by_where_it_leaves_the_device(ctx, floor):
    """critique40_practice B1: an update to the fix or to a release the advisory does not name answers; one to a release it names does
    not; the link is never read."""
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52, floor.r54], fix=floor.r56)
    row = rules.rows_of([a])[0]
    r58 = release(floor.model, "5.8")
    wo_fix, to_fix = update_wo(floor.d["P-1"], floor.r56)
    wo_other, to_other = update_wo(floor.d["P-2"], r58)
    assert rules.answers_row(to_fix, row) and rules.answers_row(to_other, row)
    _wo_listed, to_listed = update_wo(floor.d["P-5"], floor.r54)
    assert not rules.answers_row(to_listed, row)
    got = _answers(a)
    assert got["P-1"].kind == rules.UPDATE_OPEN and got["P-1"].responses[0].words == f"Update open to 5.6, the fix: {wo_fix.number}"
    assert got["P-5"].kind == rules.NO_RESPONSE
    assert [r.kind for r in got["P-5"].responses] == [rules.UPDATE_ELSEWHERE]
    assert got["P-5"].responses[0].words.startswith("Update open to 5.4, a release the advisory names: ")
    assert got["P-2"].kind == rules.UPDATE_OPEN
    assert got["P-2"].responses[0].words == f"Update open to 5.8, a release the advisory does not name: {wo_other.number}"
    assert got["P-3"].kind == rules.NO_RESPONSE  # on 5.4, listed here
    assert got["P-4"].kind == ""  # on the fix: needs none


def test_an_update_done_with_the_version_not_confirmed_is_its_own_state(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    wo, _u = update_wo(floor.d["P-1"], floor.r56)
    _complete(wo)
    got = _answers(a)
    assert got["P-1"].kind == rules.UPDATE_NOT_CONFIRMED
    assert got["P-1"].responses[0].words.endswith(f"version not confirmed: Confirm version ({wo.number})")


def _hand_patches(asset, reference, applied_on):
    return SoftwareUpdate.objects.create(asset=asset, outcome=UpdateOutcome.PATCHES, reference=reference, applied_on=applied_on,
                                         installed_by=InstalledBy.MANUFACTURER)


def test_patches_answer_on_the_list_the_advisory_names_on_any_day(ctx, floor):
    """critique40_rules B1: the list named, on any day; another list since received is its own state (it blocks Close)."""
    a = make_advisory(received_on=TODAY() - 10 * DAY)
    model_row(a, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    _hand_patches(floor.d["P-1"], "demo-patch-2026-10", TODAY() - 40 * DAY)  # before received, the same list: answers
    _hand_patches(floor.d["P-2"], "DEMO-PATCH-2026-09", TODAY() - 2 * DAY)  # since received, another list
    _hand_patches(floor.d["P-3"], "DEMO-PATCH-2026-09", TODAY() - 20 * DAY)  # before received, another list: nothing
    got = _answers(a)
    assert got["P-1"].kind == rules.PATCHES_RECORDED and got["P-1"].responses[0].words.endswith("the list the advisory names")
    assert got["P-2"].kind == rules.PATCHES_OTHER_LIST and "the advisory names DEMO-PATCH-2026-10" in got["P-2"].responses[0].words
    assert got["P-3"].kind == rules.NO_RESPONSE


def test_patches_with_no_list_named_answer_from_the_day_received(ctx, floor):
    a = make_advisory(received_on=TODAY() - 10 * DAY)
    model_row(a, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES)
    _hand_patches(floor.d["P-1"], "DEMO-PATCH-2026-10", TODAY() - 10 * DAY)
    _hand_patches(floor.d["P-2"], "DEMO-PATCH-2026-09", TODAY() - 11 * DAY)
    gone = _hand_patches(floor.d["P-3"], "DEMO-PATCH-2026-10", TODAY())
    SoftwareUpdate.objects.filter(pk=gone.pk).update(removed_on=TODAY())  # entered in error never counts
    got = _answers(a)
    assert got["P-1"].kind == rules.PATCHES_RECORDED and got["P-1"].responses[0].words.endswith("since this advisory was received")
    assert (got["P-2"].kind, got["P-3"].kind) == (rules.NO_RESPONSE, rules.NO_RESPONSE)


def test_already_on_a_patches_target_answers(ctx, floor):
    a = make_advisory(received_on=TODAY() - 3 * DAY)
    model_row(a, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    wo, u = update_wo(floor.d["P-1"], reference="DEMO-PATCH-2026-10")
    assert rules.answers_row(u, rules.rows_of([a])[0])
    assert _answers(a)["P-1"].kind == rules.UPDATE_OPEN
    SoftwareUpdate.objects.filter(pk=u.pk).update(outcome=UpdateOutcome.ALREADY, applied_on=TODAY())
    _complete(wo)
    got = _answers(a)["P-1"]
    assert got.kind == rules.PATCHES_RECORDED and got.responses[0].words.startswith(f"Checked {rules.day_words(TODAY())}: already had")


def test_a_decision_covers_until_its_review_and_a_temporary_device_only_by_its_own(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    d = advisory_decision(a, 1, device_model=floor.model, release=floor.r52, decided_on=TODAY() - 30 * DAY, review_by=TODAY() + DAY)
    got = _answers(a)
    assert (got["P-1"].kind, got["P-1"].decision, got["P-1"].covers) == (rules.ANSWER_DECIDED, d, True)
    assert got["T-1"].kind == rules.NO_RESPONSE and got["T-1"].decision is None  # a rental: only its own decision governs it
    later = _answers(a, today=TODAY() + 2 * DAY)
    assert (later["P-1"].kind, later["P-1"].covers) == (rules.ANSWER_REVIEW_DUE, False)
    assert later["P-1"].responses[-1].words.startswith("Review due since ")


def test_an_open_update_and_a_covering_decision_show_together(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    advisory_decision(a, 1, asset=floor.d["P-1"])
    update_wo(floor.d["P-1"], floor.r56)
    got = _answers(a)["P-1"]
    assert got.kind == rules.ANSWER_DECIDED and [r.kind for r in got.responses] == [rules.UPDATE_OPEN, rules.ANSWER_DECIDED]


def test_a_support_decision_never_answers_an_advisory_need(ctx, floor):
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    DeviceSecurityDecision.objects.create(number="SD-26-0005", applies_to=AppliesTo.SUPPORT_ENDED, asset=floor.d["P-1"],
                                          reasons=["security_updates"], response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                          accepted_by=AcceptedBy.CE_LEADER, decided_on=TODAY(), review_by=TODAY() + timedelta(days=30))
    assert _answers(a)["P-1"].kind == rules.NO_RESPONSE


def test_governing_never_mixes_keys(ctx, floor):
    a = make_advisory()
    support = DeviceSecurityDecision.objects.create(number="SD-26-0005", applies_to=AppliesTo.SUPPORT_ENDED, asset=floor.d["P-1"],
                                                    reasons=["security_updates"], response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                                    accepted_by=AcceptedBy.CE_LEADER, decided_on=TODAY(), review_by=TODAY() + DAY)
    theirs = advisory_decision(a, 1, asset=floor.d["P-1"])
    with pytest.raises(ValueError):
        rules._governing([floor.d["P-1"]], [support, theirs])
    assert rules.governing_decisions([floor.d["P-1"]], TODAY()) == {floor.d["P-1"].pk: support}
    assert rules.governing_decisions([floor.d["P-1"]], TODAY(), key=rules.advisory_key(a)) == {floor.d["P-1"].pk: theirs}
    assert rules.key_of(theirs) == ("advisory", a.pk) and rules.key_of(support) == rules.SUPPORT_KEY


def test_lines_and_answers_read_a_fixed_number_of_queries(ctx, floor, pump_model):
    def read():
        found = list(SecurityAdvisory.objects.all())
        with CaptureQueriesContext(connection) as q:
            rows = rules.rows_of(found)
            lines = rules.advisory_lines(rows, today=TODAY())
            rules.advisory_answers(lines, today=TODAY(), fy_start=7)
        return len(q.captured_queries), len(lines)

    a = make_advisory(number="AD-26-0001")
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    update_wo(floor.d["P-1"], floor.r56)
    advisory_decision(a, 1, asset=floor.d["P-2"])
    before = read()
    b = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002")
    vent_model = DeviceModel.objects.create(manufacturer="Hamilton", model="G5", description="Vent", category="Ventilators")
    for i in range(3):
        v = Asset.objects.create(tag=f"V-{i}", device_model=vent_model, department=Department.objects.first())
        facts(v, network=Network.NOT_CONNECTED)
    model_row(b, vent_model, versions=AdvisoryVersions.NOT_STATED, remedy=AdvisoryRemedy.MITIGATIONS)
    model_row(b, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES)
    advisory_decision(b, 2, device_model=vent_model)
    update_wo(floor.d["P-3"], reference="DEMO-PATCH-1")
    after = read()
    assert after[0] == before[0] == 6 and after[1] > before[1]
