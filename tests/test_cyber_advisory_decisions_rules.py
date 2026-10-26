"""
Slice 40 wave 1B, slice 39's decision readers keyed by need (apps.cybersecurity.rules; build40 sections 5.1, 5.2, 5.4; critique40_scope
B2), on SQLite and PostgreSQL:

- An advisory decision never governs a support need, nor a support decision an advisory's; one device past its support and named by two
  advisories has three needs and three governing decisions.
- The Decisions tab's Needs attention: the device headings read support decisions only; the decision headings every live decision, each
  by its own key (an advisory decision's rows start "For AD-26-0003:", Review and renew while a device it governs needs a response to its
  advisory now, End otherwise); the badge over any key.
- decision_devices (its devices' lines and the updates on them), holding_lines, scope_words' advisory prefix, the Check lines of build40
  section 5.4 beside slice 39's (never ALSO_ENDED), decisions_now held to the one-decision readers.
- Fixed queries: needs_attention four with no live advisory decision (slice 39's pin, however many advisories), seven with any;
  decision_devices six for an advisory decision; decisions_now; the badge one; each as decisions, advisories, and devices grow.
- line_updates is advisory_answers' update half (the same responses).
"""
import re
from datetime import date, timedelta

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from test_cyber_advisory_decisions_services import TODAY, floor  # noqa: F401 (fixtures)

from apps.cybersecurity import rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryRemedy,
    AdvisoryVersions,
    AppliesTo,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    InstalledBy,
    Network,
    SecurityAdvisory,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.equipment.models import Asset, AssetStatus, Ownership
from apps.workorders.models import WorkOrder, WoStatus

DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT, NOT_IN_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       DecisionResponse.NOT_IN_USE_HERE)
PAST = {"decided_on": TODAY - 400 * DAY, "review_by": TODAY - DAY}
SEEN: list = []


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def seen(text: str) -> str:
    SEEN.append(text)
    return text


def decide(adv=None, **fields) -> DeviceSecurityDecision:
    """A decision made directly (any state): an advisory's with `adv`, else a support decision; numbered in turn; governing, when
    recorded, every device on site its scope holds unless `governed_ids` says otherwise."""
    scope = rules.scope_devices(asset=fields.get("asset"), device_model=fields.get("device_model"), release=fields.get("release"))
    values = {"number": f"SD-26-{DeviceSecurityDecision.objects.count() + 1:04d}", "response": KEEP_WITHOUT,
              "accepted_by": AcceptedBy.CE_LEADER, "decided_on": TODAY - 10 * DAY, "review_by": TODAY + 300 * DAY,
              "governed_ids": [str(pk) for pk in scope.values_list("pk", flat=True)]}
    if adv is not None:
        values.update(applies_to=AppliesTo.ADVISORY, advisory=adv, advisory_changes=adv.changes, reasons=[])
    else:
        values.update(applies_to=AppliesTo.SUPPORT_ENDED, reasons=["security_updates"])
    return DeviceSecurityDecision.objects.create(**{**values, **fields})


def read(d) -> DeviceSecurityDecision:
    return DeviceSecurityDecision.objects.select_related(*rules._DECISION_RELATED).get(pk=d.pk)


def headings(user=None) -> dict:
    return {h.key: h for h in rules.needs_attention(TODAY, user=user).headings}


def words(heading) -> list:
    return [seen(r.words) for r in heading.rows]


# --- one key at a time -------------------------------------------------------------------------------------------------------------------

def test_an_advisory_decision_never_governs_a_support_need_and_a_support_decision_never_an_advisorys(floor):  # noqa: F811
    on52 = decide(floor.adv, device_model=floor.model, release=floor.r52)
    panel = headings()
    assert words(panel["no_decision"])[0].startswith("Alaris 8015 PCU on 5.2 (demo): 3 devices of ours; security updates ended")
    assert [r.asset.tag for r in panel["temporary"].rows] == ["T-1"]
    assert rules.device_lines(floor.d["P-1"], TODAY, user=None).decision is None  # the Security tab's support decision
    support = decide(device_model=floor.model, release=floor.r52)
    assert "no_decision" not in headings()
    dds = rules.decision_devices(read(on52), TODAY)
    assert [dd.asset.tag for dd in dds if dd.governed] == ["P-1", "P-2", "P-3"]
    assert {dd.asset.tag: dd.governing for dd in rules.decision_devices(read(support), TODAY)}["P-1"] == support
    answers = {a.line.asset.tag: a for a in rules.advisory_answers(rules.advisory_lines(rules.rows_of([floor.adv]), today=TODAY),
                                                                    today=TODAY, fy_start=7).values()}
    assert answers["P-1"].decision == on52 and answers["T-1"].decision is None


def test_one_device_past_support_named_by_two_advisories_has_three_needs_and_three_decisions(floor):  # noqa: F811
    second = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 3 * DAY)
    model_row(second, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    p1 = floor.d["P-1"]
    support = decide(asset=p1)
    first = decide(floor.adv, device_model=floor.model, release=floor.r52)
    own = decide(second, asset=p1, response=NOT_IN_USE)
    standing = rules.device_standing(p1, TODAY)
    assert rules.support_need(standing, TODAY).decidable
    lines = rules.advisory_lines(rules.rows_of([floor.adv, second]), today=TODAY, assets=[p1])
    assert [(ln.row.advisory_id, ln.kind) for ln in lines] == [(floor.adv.pk, rules.NAMED), (second.pk, rules.NAMED)]
    assert rules.governing_decisions([p1], TODAY)[p1.pk] == support
    assert rules.governing_decisions([p1], TODAY, key=rules.advisory_key(floor.adv))[p1.pk] == first
    assert rules.governing_decisions([p1], TODAY, key=rules.advisory_key(second))[p1.pk] == own
    with pytest.raises(ValueError):
        rules._governing([p1], [support, first, own])
    for d in (support, first, own):
        assert {dd.asset.tag: dd.governed for dd in rules.decision_devices(read(d), TODAY)}["P-1"] is True


# --- Needs attention and the badge ---------------------------------------------------------------------------------------------------------

def test_an_advisory_decision_past_review_offers_renew_while_a_device_needs_a_response(floor):  # noqa: F811
    d = decide(floor.adv, device_model=floor.model, release=floor.r52, **PAST)
    row = headings()["review_due"].rows[0]
    assert seen(row.words) == (f"For AD-26-0001: {d.number} · Alaris 8015 PCU on 5.2 (demo): 3 devices of ours on site; review due since "
                               f"{rules.day_words(TODAY - DAY)}")
    assert row.record == rules.Link("web:security_decision_new", (), (("supersedes", d.number),)) and row.end is None
    # an update open to the fix still leaves the need (interim controls), as a support decision's
    for tag in ("P-1", "P-2", "P-3"):
        update_wo(floor.d[tag], floor.r56)
    assert headings()["review_due"].rows[0].record is not None


def test_an_advisory_decision_none_of_whose_devices_needs_a_response_offers_end(floor):  # noqa: F811
    d = decide(floor.adv, device_model=floor.model, **PAST)  # P-1 to P-6 (5.2 and no release recorded need one)
    assert headings()["review_due"].rows[0].record is not None
    for tag in ("P-1", "P-2", "P-3", "P-6"):
        facts(floor.d[tag], floor.r56)
    panel = headings()
    row = panel["review_due"].rows[0]
    assert (row.record, row.end) == (None, rules.Link("web:security_decision_end", (d.number,)))
    assert words(panel["checks"]) == [f"For AD-26-0001: {d.number} · Governs no device that needs a response to AD-26-0001 now: end it"]
    Asset.objects.filter(device_model=floor.model, ownership=Ownership.OWNED).update(status=AssetStatus.RETIRED)
    panel = headings()
    assert words(panel["no_device"])[0].startswith(f"For AD-26-0001: {d.number} · Every Alaris 8015 PCU of ours; review due since ")
    assert "checks" not in panel and "review_due" not in panel


def test_a_patched_device_needs_no_response_to_renew(floor):  # noqa: F811
    adv = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 10 * DAY)
    model_row(adv, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    d = decide(adv, asset=floor.d["P-1"], **PAST)
    assert headings()["review_due"].rows[0].record is not None
    SoftwareUpdate.objects.create(asset=floor.d["P-1"], outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-10", applied_on=TODAY,
                                  installed_by=InstalledBy.MANUFACTURER)
    row = headings()["review_due"].rows[0]
    assert (row.decision, row.record, row.end is not None) == (d, None, True)


def test_the_device_headings_read_support_decisions_only(floor):  # noqa: F811
    before = {k: words(h) for k, h in headings().items()}
    decide(floor.adv, device_model=floor.model, release=floor.r52)
    decide(floor.adv, asset=floor.d["T-1"])
    after = {k: words(h) for k, h in headings().items()}
    assert after == before and set(after) == {"no_decision", "temporary"}


def test_the_badge_counts_decisions_of_every_key_past_review_that_hold_a_device(floor, django_assert_num_queries):  # noqa: F811
    decide(device_model=floor.model, release=floor.r52, **PAST)
    advisory = decide(floor.adv, asset=floor.d["P-1"], **PAST)
    decide(floor.adv, asset=floor.d["P-6"], decided_on=TODAY - 400 * DAY, review_by=TODAY)  # its review is today: not past
    with django_assert_num_queries(1):
        assert rules.badge(TODAY) == {"count": 2, "hot": True}
    assert rules.badge(TODAY)["count"] == len(headings()["review_due"].rows)
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(status=AssetStatus.RETIRED)
    assert rules.badge(TODAY)["count"] == 1 and advisory.number not in {r.decision.number for r in headings()["review_due"].rows}


# --- a decision's devices, what holds now, the scope's words ------------------------------------------------------------------------

def test_an_advisory_decisions_devices_carry_their_lines_and_updates(floor):  # noqa: F811
    d = read(decide(floor.adv, device_model=floor.model))
    decide(floor.adv, asset=floor.d["P-6"])
    wo, _u = update_wo(floor.d["P-1"], floor.r56)
    dds = {dd.asset.tag: dd for dd in rules.decision_devices(d, TODAY)}
    assert list(dds) == ["P-1", "P-2", "P-3", "P-4", "P-5", "P-6"]  # devices of ours (never the rental)
    assert {t: dd.line.kind for t, dd in dds.items()} == {"P-1": rules.NAMED, "P-2": rules.NAMED, "P-3": rules.NAMED, "P-4": rules.ON_FIX,
                                                         "P-5": rules.OTHER_RELEASE, "P-6": rules.LINE_NOT_RECORDED}
    assert all(dd.need is None for dd in dds.values())  # a support need is another key's
    assert seen(dds["P-1"].words(TODAY)) == f"Update open to 5.6 (demo), the fix: {wo.number}; This decision"
    assert dds["P-6"].words(TODAY).startswith("Governed by SD-") and not dds["P-6"].governed
    assert [t for t, dd in dds.items() if dd.needs_response] == ["P-1", "P-2", "P-3", "P-6"]
    assert rules.holding_lines(d, list(dds.values())) == ["On 5.2 (demo), a release the advisory names"]
    on_fix = read(decide(floor.adv, asset=floor.d["P-4"]))
    assert rules.holding_lines(on_fix, rules.decision_devices(on_fix, TODAY)) == []


def test_scope_words_name_the_advisory_only_when_asked(floor):  # noqa: F811
    d = read(decide(floor.adv, device_model=floor.model, release=floor.r52))
    s = read(decide(device_model=floor.model, release=floor.r52))
    assert rules.scope_words(d) == rules.scope_words(s) == "Alaris 8015 PCU on 5.2 (demo)"
    assert seen(rules.scope_words(d, advisory=True)) == "AD-26-0001 · Alaris 8015 PCU on 5.2 (demo)"
    assert rules.scope_words(d, 3, advisory=True) == "AD-26-0001 · Alaris 8015 PCU on 5.2 (demo): 3 devices of ours on site"
    assert rules.scope_words(s, advisory=True) == "Alaris 8015 PCU on 5.2 (demo)"
    own = read(decide(floor.adv, asset=floor.d["P-1"]))
    assert rules.scope_words(own, advisory=True) == "AD-26-0001 · P-1"


def test_not_in_use_here_reads_without_a_plan_and_with_the_day_decided_first(floor, make_user):  # noqa: F811
    kim = make_user("director")
    d = read(decide(floor.adv, asset=floor.d["P-1"], response=NOT_IN_USE, recorded_by=kim, decided_on=TODAY,
                    review_by=date(2027, 10, 21)))
    assert seen(rules.decision_words(d, TODAY)) == ("Decided Oct 21, 2026 that the part it names is not in use here; review by Oct 21, "
                                                    f"2027. Decided by the CE leader, as recorded by {kim}; no IT exception number "
                                                    "recorded.")
    assert seen(rules.decision_words(d, date(2027, 10, 22))) == (
        f"Review due since Oct 21, 2027: decided Oct 21, 2026 that the part it names is not in use here. Decided by the CE leader, as "
        f"recorded by {kim}; no IT exception number recorded.")


# --- the Check lines ------------------------------------------------------------------------------------------------------------------

def checks(d) -> list:
    d = read(d)
    return [(c.key, seen(c.words), c.action) for c in rules.decision_checks(d, TODAY)]


def test_the_advisory_changed_since_the_decision_by_its_count(floor):  # noqa: F811
    d = decide(floor.adv, device_model=floor.model, release=floor.r52)
    assert checks(d) == []
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(changes=3)  # a revision the same day still counts
    assert checks(d) == [(rules.CHECK_ADVISORY_CHANGED, "The advisory changed since this decision (Oct 16, 2026)", "")]
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(changed_on=None)
    assert checks(d) == [(rules.CHECK_ADVISORY_CHANGED, "The advisory changed since this decision", "")]


def test_waiting_for_the_manufacturer_while_the_advisory_names_a_fix(floor):  # noqa: F811
    d = decide(floor.adv, device_model=floor.model, release=floor.r52, plan=DecisionPlan.WAIT_FOR_MANUFACTURER)
    assert checks(d) == [(rules.CHECK_FIX_NAMED, "Plan: wait for the manufacturer, while the advisory now names a fix (5.6 (demo)): open "
                                                 "updates", "")]
    no_fix = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 3 * DAY)
    model_row(no_fix, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    assert checks(decide(no_fix, device_model=floor.model, plan=DecisionPlan.WAIT_FOR_MANUFACTURER)) == []
    # its scope holding no device on site: the row is read for it
    Asset.objects.filter(pk__in=[floor.d[t].pk for t in ("P-1", "P-2", "P-3")]).update(status=AssetStatus.RETIRED)
    assert [c[0] for c in checks(d)] == [rules.CHECK_FIX_NAMED, rules.CHECK_NO_DEVICE]


def test_a_device_no_longer_recorded_not_connected_here(floor):  # noqa: F811
    for tag in ("P-1", "P-2"):
        facts(floor.d[tag], floor.r52, network=Network.NOT_CONNECTED)
    d = decide(floor.adv, asset=floor.d["P-1"], response=KEEP_WITH, controls=["network_removed"])
    assert checks(d) == []
    facts(floor.d["P-1"], floor.r52, network=Network.WIRELESS)
    assert checks(d) == [(rules.CHECK_NETWORK_BACK, "P-1 is no longer recorded Not connected here", "")]
    facts(floor.d["P-1"], floor.r52, network="")  # as the model discloses: not its own fact
    assert checks(d) == [(rules.CHECK_NETWORK_BACK, "P-1 is no longer recorded Not connected here", "")]
    many = decide(floor.adv, device_model=floor.model, release=floor.r52, response=KEEP_WITH, controls=["network_removed"],
                  governed_ids=[str(floor.d[t].pk) for t in ("P-2", "P-3")])
    assert checks(many) == [(rules.CHECK_NETWORK_BACK, "P-3 is no longer recorded Not connected here", "")]  # P-1 its own


def test_no_device_needing_a_response_and_slice_39s_lines_beside_it(floor, make_user):  # noqa: F811
    owner = make_user("manager")
    d = decide(floor.adv, device_model=floor.model, release=floor.r52, governed_ids=[str(floor.d["P-1"].pk)],
               plan=DecisionPlan.UPDATE_PLANNED, plan_by=TODAY - DAY, plan_owner=owner)
    assert checks(d) == [(rules.CHECK_JOINED, "2 devices joined since: P-2, P-3", ""),
                         (rules.CHECK_PLAN_PASSED, "The plan's date (Oct 20, 2026) has passed", "")]  # never ALSO_ENDED (5.2 is past)
    own = decide(floor.adv, asset=floor.d["P-4"])
    assert checks(own) == [(rules.CHECK_NO_NEED, "Governs no device that needs a response to AD-26-0001 now: end it", "end")]
    owner.is_active = False
    owner.save()
    assert checks(d)[-1] == (rules.CHECK_OWNER, rules.OWNER_INACTIVE_WORDS, "")


def test_decision_checks_with_its_devices_read_nothing_more(floor, django_assert_num_queries):  # noqa: F811
    d = read(decide(floor.adv, device_model=floor.model, release=floor.r52, plan=DecisionPlan.WAIT_FOR_MANUFACTURER))
    dds = rules.decision_devices(d, TODAY)
    with django_assert_num_queries(0):
        assert [c.key for c in rules.decision_checks(d, TODAY, devices=dds)] == [rules.CHECK_FIX_NAMED]


# --- a page of decisions --------------------------------------------------------------------------------------------------------------

def _page():
    return list(DeviceSecurityDecision.objects.select_related(*rules._DECISION_RELATED).order_by("number"))


def test_decisions_now_reads_each_decision_as_the_one_decision_readers(floor):  # noqa: F811
    decide(floor.adv, device_model=floor.model, release=floor.r52, plan=DecisionPlan.WAIT_FOR_MANUFACTURER)
    decide(floor.adv, device_model=floor.model, governed_ids=[])
    decide(floor.adv, asset=floor.d["T-1"], response=KEEP_WITH, controls=["network_removed"])
    decide(floor.adv, asset=floor.d["P-4"])
    decide(device_model=floor.model, release=floor.r52)
    decide(floor.adv, asset=floor.d["P-1"], ended_on=TODAY, end_reason="no_longer_needed")
    gone = decide(floor.adv, asset=floor.d["P-2"], plan=DecisionPlan.WAIT_FOR_MANUFACTURER)
    Asset.objects.filter(pk=floor.d["P-2"].pk).update(status=AssetStatus.RETIRED)
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(changes=5)
    page = _page()
    now = rules.decisions_now(page, TODAY)
    for d in page:
        dds = rules.decision_devices(d, TODAY)
        governs = tuple(dd.asset for dd in dds if dd.governed) if d.ended_on is None else ()
        assert now[d.pk] == (governs, tuple(rules.decision_checks(d, TODAY, devices=dds))), d.number
    assert [c.key for c in now[gone.pk][1]] == [rules.CHECK_ADVISORY_CHANGED, rules.CHECK_FIX_NAMED, rules.CHECK_NO_DEVICE]


# --- fixed queries ---------------------------------------------------------------------------------------------------------------------

def _grow(floor, n: int, start: int):  # noqa: F811
    """n more pumps of ours on 5.2, each with its own advisory decision past review and an open update to the fix; a new advisory naming
    the pump with a release decision and a patches record; a new release with a support decision."""
    for i in range(start, start + n):
        a = Asset.objects.create(tag=f"G-{i:03d}", device_model=floor.model, department=floor.dept)
        facts(a, floor.r52)
        decide(floor.adv, asset=a, **PAST)
        update_wo(a, floor.r56)
        adv = make_advisory(number=f"AD-26-{100 + i:04d}", reference=f"DEMO-ADV-{100 + i}", received_on=TODAY - 5 * DAY)
        model_row(adv, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES)
        decide(adv, device_model=floor.model, release=floor.r52, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, **PAST)
        SoftwareUpdate.objects.create(asset=a, outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-1", applied_on=TODAY,
                                      installed_by=InstalledBy.MANUFACTURER)
        r = release(floor.model, f"3.{i} (demo)", security_updates_end_on=TODAY - DAY)
        decide(device_model=floor.model, release=r)


def test_slice_39s_pin_holds_with_advisories_and_no_live_advisory_decision(floor, django_assert_num_queries):  # noqa: F811
    for i in range(3):
        adv = make_advisory(number=f"AD-26-{10 + i:04d}", reference=f"DEMO-ADV-{10 + i}", received_on=TODAY - DAY)
        model_row(adv, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    decide(device_model=floor.model, release=floor.r52, **PAST)
    ended = decide(floor.adv, asset=floor.d["P-1"], ended_on=TODAY, end_reason="no_longer_needed")
    assert ended.ended_on is not None
    with django_assert_num_queries(4):
        rules.needs_attention(TODAY, user=None)


def test_fixed_queries_as_advisory_decisions_advisories_and_devices_grow(floor, django_assert_num_queries):  # noqa: F811
    decide(floor.adv, device_model=floor.model, release=floor.r52, controls=["segmented"], response=KEEP_WITH)
    counts = []
    for n, start in ((2, 0), (5, 10)):
        _grow(floor, n, start)
        d = read(DeviceSecurityDecision.objects.get(number="SD-26-0001"))
        with django_assert_num_queries(6):
            dds = rules.decision_devices(d, TODAY)
        with django_assert_num_queries(0):
            rules.decision_checks(d, TODAY, devices=dds)
        with django_assert_num_queries(7):
            panel = rules.needs_attention(TODAY, user=None)
        with django_assert_num_queries(1):
            rules.badge(TODAY)
        page = _page()
        with django_assert_num_queries(7):  # a page mixing both keys
            rules.decisions_now(page, TODAY)
        advisory_page = [p for p in page if p.applies_to == AppliesTo.ADVISORY]
        with django_assert_num_queries(6):
            rules.decisions_now(advisory_page, TODAY)
        support_page = [p for p in page if p.applies_to == AppliesTo.SUPPORT_ENDED]
        with django_assert_num_queries(3):  # slice 39's
            rules.decisions_now(support_page, TODAY)
        counts.append((len(dds), sum(h.count for h in panel.headings)))
    assert counts[1][0] > counts[0][0] and counts[1][1] > counts[0][1]


# --- line_updates is advisory_answers' update half ------------------------------------------------------------------------------------

def test_line_updates_give_advisory_answers_responses_without_the_decision(floor):  # noqa: F811
    patches = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 10 * DAY)
    model_row(patches, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    update_wo(floor.d["P-1"], floor.r56)  # to the fix: answers AD-26-0001
    update_wo(floor.d["P-2"], floor.r54)  # to a release AD-26-0001 does not name
    done, _u = update_wo(floor.d["P-3"], floor.r56)
    WorkOrder.objects.filter(pk=done.pk).update(status=WoStatus.COMPLETED, completed_on=TODAY)  # not confirmed
    SoftwareUpdate.objects.create(asset=floor.d["P-6"], outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-10", applied_on=TODAY,
                                  installed_by=InstalledBy.MANUFACTURER)
    SoftwareUpdate.objects.create(asset=floor.d["T-1"], outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-09", applied_on=TODAY,
                                  installed_by=InstalledBy.MANUFACTURER)
    update_wo(floor.d["P-5"], reference="DEMO-PATCH-2026-10")
    decide(floor.adv, device_model=floor.model, release=floor.r52)
    decide(patches, asset=floor.d["P-1"])
    lines = rules.advisory_lines(rules.rows_of([floor.adv, patches]), today=TODAY)
    answers = rules.advisory_answers(lines, today=TODAY, fy_start=7)
    updates = rules.line_updates(lines)
    shown = 0
    for (pk, row_pk), answer in answers.items():
        mine = updates.get((pk, row_pk))
        expected = tuple(r for r in answer.responses if r.kind not in (rules.ANSWER_DECIDED, rules.ANSWER_REVIEW_DUE, rules.NO_RESPONSE))
        assert (mine.responses if mine else ()) == expected
        assert rules.needs_now(answer.line, updates) is (answer.line.needs_response and answer.kind != rules.PATCHES_RECORDED)
        shown += len(expected)
    assert shown >= 6


def test_the_new_words_say_no_never_word():
    assert [t for t in SEEN if never(t)] == []
    texts = [rules.ADVISORY_CHANGED_WORDS, rules.ADVISORY_CHANGED_NO_DAY, rules.FIX_NAMED_WORDS, rules.NETWORK_BACK_WORDS,
             rules.NO_NEED_WORDS, rules.FOR_ADVISORY, rules.CHECK_ADVISORY_CHANGED, rules.CHECK_FIX_NAMED, rules.CHECK_NETWORK_BACK,
             rules.CHECK_NO_NEED]
    assert [t for t in texts if never(t)] == []
