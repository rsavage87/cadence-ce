"""
Slice 40 review fixes, the advisory rules (apps.cybersecurity.rules), on SQLite and PostgreSQL:

- #1: a decision past its review date reads before an update open or done beside it (ANSWER_ORDER): the decisions past review, the
  status words, and Close's "review and renew it" count it; the update still shows among the line's responses.
- #0: one definition of a closed advisory's "devices named since" (named_since_lines) for the status words, the Check, and Needs
  attention: a device under a decision past review is counted only under the decisions past review.
- #2: an update to the manufacturer's patches done before the advisory was received, on a row that names no list, is no response (what
  Confirm version would leave it), never "version not confirmed".
- #3 / #10: an open update's words on a row naming every release, or stating none, never "a release the advisory does not name".
- #4: an update to a release of another model (left open when the device's model was changed) never answers.
- #5: Needs attention's "version not confirmed" rows name, per device and work order, the advisories that work order answers.
- #9: a closed advisory whose models' confirmation was cleared reads "since a change", never a revision nobody recorded.
- #12: a device that joined a release decision since never reads "no longer recorded Not connected here".
- #14: "already had the manufacturer's patches" on another list names the list the advisory names.
- #18: Needs attention never offers Open software update for a temporary device held for an incident.
- The readers outside any transaction on PostgreSQL (as a GET door runs them): no lock.
Every word added is scanned with rules.DECISION_NEVER_WORDS.
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.utils import timezone
from pg_helpers import needs_postgres
from test_cyber_advisories_rules import close_directly, counts, decision, floor  # noqa: F401 (fixtures)
from test_cyber_advisory_decisions_rules import KEEP_WITH, checks, decide
from test_cyber_advisory_decisions_services import floor as decisions_floor  # noqa: F401 (fixtures)

from apps.cybersecurity import advisories, rules
from apps.cybersecurity.models import (
    AdvisoryRemedy,
    AdvisoryVersions,
    DeviceSecurityDecision,
    Network,
    SecurityAdvisory,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.equipment import services as equipment
from apps.equipment.models import Asset
from apps.workorders.models import WorkOrder, WoStatus

TODAY = timezone.localdate()
DAY = timedelta(days=1)
LISTED, ALL, NOT_STATED = AdvisoryVersions.LISTED, AdvisoryVersions.ALL, AdvisoryVersions.NOT_STATED
WORDS: list = []  # every sentence these tests read, scanned at the end of each test that reads it


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def said(text: str) -> str:
    assert not never(text), text
    WORDS.append(text)
    return text


def answers(adv) -> dict:
    reading = rules.advisory_reading([adv], TODAY)
    return {a.line.asset.tag: a for a in reading.answers.values()}


def complete(wo, on=None):
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=on or TODAY)


def headings(user=None) -> dict:
    return {h.key: h for h in rules.advisory_attention(TODAY, user=user).headings}


def lapse(d) -> None:
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(review_by=TODAY - DAY)


# --- #1: a decision past review before an update beside it --------------------------------------------------------------------------

def test_a_decision_past_review_reads_before_an_open_update_beside_it(ctx, floor):  # noqa: F811
    """P-1 under its own decision past review, with an update open to the fix: Close says review and renew it (Record decision is refused
    while that decision is in force), the counts and words count the decision, and the update still shows beside it."""
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    for tag in ("P-2", "P-5", "T-1"):
        decision(a, asset=floor.d[tag])
    own = decision(a, asset=floor.d["P-1"])
    wo, _u = update_wo(floor.d["P-1"], floor.r56)
    assert said(counts(a).words) == "Open: ready to close"  # covered: the update beside the decision is shown, never counted
    lapse(own)
    got = answers(a)["P-1"]
    assert got.kind == rules.ANSWER_REVIEW_DUE and not got.covers
    assert [r.kind for r in got.responses] == [rules.UPDATE_OPEN, rules.ANSWER_REVIEW_DUE]
    assert got.responses[0].words == said(f"Update open to 5.6 (demo), the fix: {wo.number}")
    full = counts(a)
    assert (full.reviews_due, full.answers[rules.ANSWER_REVIEW_DUE], full.answers[rules.UPDATE_OPEN]) == (1, 1, 0)
    assert said(full.words) == "Open: 1 device under a decision past its review date"
    refusal = said(advisories.close_refusal(a, TODAY))
    assert refusal == "Not closed: 1 device is under a decision past its review date (P-1): review and renew it."
    # an update done with the version not confirmed beside it reads the same
    complete(wo)
    got = answers(a)["P-1"]
    assert got.kind == rules.ANSWER_REVIEW_DUE and [r.kind for r in got.responses] == [rules.UPDATE_NOT_CONFIRMED, rules.ANSWER_REVIEW_DUE]
    assert said(counts(a).words) == "Open: 1 device under a decision past its review date"
    # View alone still reads the update from the responses shown, never a word derived from the decision
    view = counts(a, devices_visible=False)
    assert (view.updates[rules.UPDATE_NOT_CONFIRMED], view.words) == (1, "Open")


def test_a_closed_advisorys_decision_past_review_beside_an_update_is_never_named_since(ctx, floor):  # noqa: F811
    """Closed with a release decision over P-1 and P-2, P-1 with an update open to the fix; then the decision passes its review: one
    decision past its review date, no device named since (P-1 was there and decided when it closed)."""
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    shared = decision(a, release=floor.r52, device_model=floor.model)
    for tag in ("P-5", "T-1"):
        decision(a, asset=floor.d[tag])
    update_wo(floor.d["P-1"], floor.r56)
    assert advisories.close_refusal(a, TODAY) is None
    advisories.close(a, by=None, today=TODAY)
    a.refresh_from_db()
    lapse(shared)
    full = counts(a)
    assert (full.named_since, full.reviews_due) == (0, 1)
    assert said(full.words) == f"Closed ({rules.day_words(TODAY)}) · 1 decision past its review date"
    assert [c.key for c in rules.advisory_checks(a, today=TODAY, devices_visible=True)] == []
    h = headings()
    assert "named_since" not in h and h["decisions_due"].count == 1


# --- #0: one definition of "devices named since" --------------------------------------------------------------------------------------

def test_devices_under_a_decision_past_review_are_never_named_since(ctx, floor):  # noqa: F811
    """build40's scenario: every release named, no fix yet; a release decision covers P-1 and P-2, each other device its own; closed;
    then the release decision passes its review. The status words, the Check, and Needs attention agree: no device named since, one
    decision past its review date. A device added since is named since in all three."""
    a = make_advisory()
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    shared = decision(a, release=floor.r52, device_model=floor.model)
    for tag in ("P-3", "P-4", "P-5", "T-1"):
        decision(a, asset=floor.d[tag])
    close_directly(a, on=TODAY - 2 * DAY)
    lapse(shared)
    reading = rules.advisory_reading([a], TODAY)
    assert [x.line.asset.tag for x in rules.close_blockers(reading.answers)[rules.ANSWER_REVIEW_DUE]] == ["P-1", "P-2"]  # Close's
    assert rules.named_since_lines(reading.answers) == []
    full = counts(a)
    assert (full.named_since, full.reviews_due) == (0, 1)
    assert said(full.words) == f"Closed ({rules.day_words(TODAY - 2 * DAY)}) · 1 decision past its review date"
    assert rules.advisory_checks(a, today=TODAY, devices_visible=True) == []
    h = headings()
    assert "named_since" not in h and h["decisions_due"].count == 1
    Asset.objects.create(tag="P-6", device_model=floor.model, department=floor.dept)  # named since
    reading = rules.advisory_reading([a], TODAY)
    assert [x.line.asset.tag for x in rules.named_since_lines(reading.answers)] == ["P-6"]
    full = counts(a)
    assert (full.named_since, full.reviews_due) == (1, 1)
    closed = rules.day_words(TODAY - 2 * DAY)
    assert said(full.words) == f"Closed ({closed}) · 1 device named since · 1 decision past its review date"
    assert [(c.key, said(c.words), c.tags) for c in rules.advisory_checks(a, today=TODAY, devices_visible=True)] == [
        (rules.CHECK_NAMED_SINCE, f"1 device named since it was closed on {closed}: P-6", ("P-6",))]
    [row] = headings()["named_since"].rows
    assert (said(row.words).endswith(f"closed {closed}; 1 device named since"), row.count, row.tags) == (True, 1, ("P-6",))


# --- #2: patches done before the advisory was received ----------------------------------------------------------------------------------

def test_patches_done_before_received_on_a_row_naming_no_list_are_no_response(ctx, floor):  # noqa: F811
    """A patches update completed 60 days before an advisory naming no list was received, nothing recorded: no response (Confirm version
    would record the patches on that day, before received: no response either), so Close and Needs attention never send the reader to
    confirm it. One completed since received waits for its version; on a row naming its list, any day does."""
    a = make_advisory(received_on=TODAY - 2 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES)
    old, u_old = update_wo(floor.d["P-1"], reference="DEMO-PATCH-07")
    complete(old, on=TODAY - 60 * DAY)
    new, _u = update_wo(floor.d["P-2"], reference="DEMO-PATCH-07")
    complete(new, on=TODAY - DAY)
    got = answers(a)
    assert (got["P-1"].kind, [said(r.words) for r in got["P-1"].responses]) == (rules.NO_RESPONSE, [rules.NO_RESPONSE_WORDS])
    assert got["P-2"].kind == rules.UPDATE_NOT_CONFIRMED
    assert said(got["P-2"].responses[0].words) == (f"Update done {rules.day_words(TODAY - DAY)}, version not confirmed: Confirm version "
                                                   f"({new.number})")
    lines = rules.advisory_lines(rules.rows_of([a]), today=TODAY)
    updates = rules.line_updates(lines)  # the update half the decisions read: the same rule
    by_tag = {ln.asset.tag: updates[(ln.asset.pk, ln.row.pk)] for ln in lines}
    assert by_tag["P-1"].responses == () and [r.kind for r in by_tag["P-2"].responses] == [rules.UPDATE_NOT_CONFIRMED]
    assert [r.tags for r in headings()["not_confirmed"].rows] == [("P-2",)]
    refusal = said(advisories.close_refusal(a, TODAY))
    assert "no response recorded (P-1, P-3, P-4, P-5, T-1)" in refusal and "version not confirmed (P-2)" in refusal
    # what Confirm version records for it (the patches, on the work order's completed day) reads the same
    SoftwareUpdate.objects.filter(pk=u_old.pk).update(outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-07", applied_on=TODAY - 60 * DAY)
    assert answers(a)["P-1"].kind == rules.NO_RESPONSE
    named = make_advisory(reference="DEMO-ADV-0002", received_on=TODAY - 2 * DAY)
    model_row(named, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="demo-patch-09")
    early, _u = update_wo(floor.d["P-3"], reference="DEMO-PATCH-09")
    complete(early, on=TODAY - 60 * DAY)
    assert answers(named)["P-3"].kind == rules.UPDATE_NOT_CONFIRMED  # the list it names counts on any day


# --- #3 / #10: an open update's words on a row naming every release, or none --------------------------------------------------------

@pytest.mark.parametrize("versions, expected", [
    (ALL, "Update open to 5.6 (demo), a release the advisory names: {n}"),
    (NOT_STATED, "Update open to 5.6 (demo); the advisory does not state releases: {n}"),
    (LISTED, "Update open to 5.6 (demo), a release the advisory does not name: {n}"),
])
def test_an_open_updates_words_follow_what_the_row_states(ctx, floor, versions, expected):  # noqa: F811
    a = make_advisory()
    model_row(a, floor.model, versions=versions, releases=[floor.r52] if versions == LISTED else (),
              remedy=AdvisoryRemedy.NO_FIX_YET)
    wo, _u = update_wo(floor.d["P-1"], floor.r56)
    got = answers(a)["P-1"]
    [response] = got.responses
    assert said(response.words) == expected.format(n=wo.number)
    answering = versions == LISTED  # a response only where the row lists releases (answers_row)
    assert (got.kind, response.kind) == ((rules.UPDATE_OPEN, rules.UPDATE_OPEN) if answering
                                         else (rules.NO_RESPONSE, rules.UPDATE_ELSEWHERE))
    lines = [ln for ln in rules.advisory_lines(rules.rows_of([a]), today=TODAY) if ln.asset.tag == "P-1"]
    assert [r.words for r in rules.line_updates(lines)[(lines[0].asset.pk, lines[0].row.pk)].responses] == [response.words]


# --- #4: a release of another model never answers ---------------------------------------------------------------------------------------

def test_an_update_to_another_models_release_never_answers(ctx, floor, vent_model):  # noqa: F811
    """P-3's update to the pumps' 5.6 (demo) is left open when its model is changed to the ventilator (the release record cleared): under
    an advisory listing a ventilator release, it is no response, worded as one, and P-3 is listed with no response recorded."""
    v10 = release(vent_model, "1.0 (demo)")
    wo, u = update_wo(floor.d["P-3"], floor.r56)
    equipment.update_asset(floor.d["P-3"], device_model=vent_model)
    p3 = Asset.objects.get(pk=floor.d["P-3"].pk)
    assert p3.device_model_id == vent_model.pk and p3.cyber.release_id is None
    a = make_advisory()
    model_row(a, vent_model, releases=[v10], remedy=AdvisoryRemedy.MITIGATIONS)
    row = rules.rows_of([a])[0]
    assert not rules.answers_row(SoftwareUpdate.objects.select_related("target_release").get(pk=u.pk), row)
    got = answers(a)["P-3"]
    assert got.line.kind == rules.LINE_NOT_RECORDED and got.kind == rules.NO_RESPONSE
    assert [(r.kind, said(r.words)) for r in got.responses] == [
        (rules.UPDATE_ELSEWHERE, f"Update open to 5.6 (demo), a release of another model: {wo.number}")]
    assert said(counts(a).words) == "Open: 1 device with no response recorded"
    assert [r.tags for r in headings()["no_response"].rows] == [("P-3",)]
    complete(wo)  # done with nothing recorded: no response either, never "version not confirmed"
    assert answers(a)["P-3"].kind == rules.NO_RESPONSE


# --- #5: one Needs attention row per device and work order --------------------------------------------------------------------------

def test_version_not_confirmed_rows_name_the_work_order_each_advisory_waits_on(ctx, floor):  # noqa: F811
    first = make_advisory(reference="DEMO-ADV-0001", received_on=TODAY - 5 * DAY)
    model_row(first, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-10")
    second = make_advisory(reference="DEMO-ADV-0002", received_on=TODAY - 4 * DAY)
    model_row(second, floor.model, releases=[floor.r52], fix=floor.r56)
    patches, _u = update_wo(floor.d["P-1"], reference="DEMO-PATCH-10")
    complete(patches, on=TODAY - 2 * DAY)
    to_fix, _u = update_wo(floor.d["P-1"], floor.r56)
    complete(to_fix, on=TODAY - DAY)
    assert answers(first)["P-1"].responses[0].number == patches.number
    assert answers(second)["P-1"].responses[0].number == to_fix.number
    rows = headings()["not_confirmed"].rows
    assert [(said(r.words), r.work_order) for r in rows] == [
        (f"{first.number} · P-1: Update done {rules.day_words(TODAY - 2 * DAY)}, version not confirmed: Confirm version "
         f"({patches.number})", rules.Link("web:wo", (patches.number,))),
        (f"{second.number} · P-1: Update done {rules.day_words(TODAY - DAY)}, version not confirmed: Confirm version ({to_fix.number})",
         rules.Link("web:wo", (to_fix.number,)))]
    third = make_advisory(reference="DEMO-ADV-0003", received_on=TODAY - 3 * DAY)  # another advisory the same work order answers
    model_row(third, floor.model, releases=[floor.r52, floor.r54], fix=floor.r56)
    rows = headings()["not_confirmed"].rows
    assert [r.words.split(" · ")[0] for r in rows] == [first.number, f"{second.number}, {third.number}"]
    assert rows[1].work_order == rules.Link("web:wo", (to_fix.number,))


# --- #9: the words for a confirmation cleared on a closed advisory -------------------------------------------------------------------

def test_a_closed_advisorys_models_to_confirm_since_a_change_of_the_day_received(ctx, floor):  # noqa: F811
    """Close an advisory, then correct its day received (advisories.update_advisory clears the confirmation: critique40_rules B1): no
    revision is recorded, so nothing reads one."""
    a = make_advisory(received_on=TODAY - 5 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        decision(a, asset=floor.d[tag], decided_on=TODAY - 4 * DAY)
    advisories.close(a, by=None, today=TODAY)
    advisories.update_advisory(a, received_on=TODAY - 6 * DAY, by=None, today=TODAY)
    a = SecurityAdvisory.objects.get(pk=a.pk)
    assert (a.status, a.models_confirmed_on, a.changes) == ("closed", None, 1)
    assert not a.sources.exclude(revision="").exists()
    day = rules.day_words(TODAY)
    for visible in (True, False):
        assert said(counts(a, devices_visible=visible).words) == f"Closed ({day}) · models to confirm since a change"
    assert [(c.key, said(c.words)) for c in rules.advisory_checks(a, today=TODAY, devices_visible=True)] == [
        (rules.CHECK_TO_CONFIRM, "Closed, models to confirm since a change: reopen it to confirm them")]
    h = headings()["changed"]
    assert said(h.words) == "Closed, models to confirm since a change" and [r.advisory.pk for r in h.rows] == [a.pk]
    for text in (rules.TO_CONFIRM_SINCE_WORDS, rules.TO_CONFIRM_CHECK_WORDS, rules.ADVISORY_ATTENTION_HEADINGS["changed"]):
        assert "revision" not in text and not never(text)


# --- #14: "already" on another list --------------------------------------------------------------------------------------------------

def test_already_had_another_list_names_the_list_the_advisory_names(ctx, floor):  # noqa: F811
    a = make_advisory(received_on=TODAY - 3 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-10")
    other, u = update_wo(floor.d["P-1"], reference="DEMO-PATCH-09")
    SoftwareUpdate.objects.filter(pk=u.pk).update(outcome=UpdateOutcome.ALREADY, applied_on=TODAY)
    complete(other)
    same, u2 = update_wo(floor.d["P-2"], reference="demo-patch-10")
    SoftwareUpdate.objects.filter(pk=u2.pk).update(outcome=UpdateOutcome.ALREADY, applied_on=TODAY)
    complete(same)
    got = answers(a)
    words = f"Checked {rules.day_words(TODAY)}: already had the manufacturer's patches (DEMO-PATCH-09); the advisory names DEMO-PATCH-10"
    assert (got["P-1"].kind, [said(r.words) for r in got["P-1"].responses]) == (rules.PATCHES_OTHER_LIST, [words])
    assert [(c.key, said(c.words)) for c in rules.advisory_checks(a, today=TODAY, devices_visible=True)] == [
        (rules.CHECK_OTHER_LIST, f"P-1: {words}")]
    assert said(headings()["other_list"].rows[0].words) == f"{a.number} · P-1: {words}"
    assert (got["P-2"].kind, got["P-2"].responses[0].words) == (  # the list it names: as before
        rules.PATCHES_RECORDED, f"Checked {rules.day_words(TODAY)}: already had the manufacturer's patches (demo-patch-10)")


# --- #18: a temporary device held for an incident -------------------------------------------------------------------------------------

def test_no_update_opener_for_a_temporary_device_held_for_an_incident(ctx, floor):  # noqa: F811
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    [row] = headings()["temporary"].rows
    assert row.open_update == rules.Link("web:cybersecurity_update_open", ("T-1",), (("advisory", a.number), ("from", "advisory")))
    Asset.objects.filter(pk=floor.d["T-1"].pk).update(incident_hold=True)  # set_incident_hold's field, made directly
    [row] = headings()["temporary"].rows
    assert row.open_update is None and row.record is not None  # Record decision stays: a decision can be recorded for it


# --- #12: a device that joined a decision since --------------------------------------------------------------------------------------

def test_a_device_that_joined_since_is_never_no_longer_not_connected(decisions_floor):  # noqa: F811
    f = decisions_floor
    for tag in ("P-1", "P-2", "P-3"):
        facts(f.d[tag], f.r52, network=Network.NOT_CONNECTED)
    d = decide(f.adv, device_model=f.model, release=f.r52, response=KEEP_WITH, controls=["network_removed", "physical_controls"])
    assert checks(d) == []
    p9 = Asset.objects.create(tag="P-9", device_model=f.model, department=f.dept)
    facts(p9, f.r52)  # no connection fact of its own
    assert checks(d) == [(rules.CHECK_NETWORK_JOINED, "P-9 joined since and is not recorded Not connected here", ""),
                         (rules.CHECK_JOINED, "1 device joined since: P-9", "")]
    facts(f.d["P-1"], f.r52, network=Network.WIRELESS)  # one it governed when recorded: no longer
    p10 = Asset.objects.create(tag="P-10", device_model=f.model, department=f.dept)
    facts(p10, f.r52, network=Network.WIRELESS)
    assert checks(d)[:2] == [(rules.CHECK_NETWORK_BACK, "P-1 is no longer recorded Not connected here", ""),
                             (rules.CHECK_NETWORK_JOINED, "P-10, P-9 joined since and are not recorded Not connected here", "")]
    facts(p9, f.r52, network=Network.NOT_CONNECTED)
    facts(p10, f.r52, network=Network.NOT_CONNECTED)
    assert [c[0] for c in checks(d)] == [rules.CHECK_NETWORK_BACK, rules.CHECK_JOINED]
    own = decide(f.adv, asset=f.d["P-2"], response=KEEP_WITH, controls=["network_removed"], governed_ids=[])
    facts(f.d["P-2"], f.r52, network="")
    assert checks(own) == [(rules.CHECK_NETWORK_BACK, "P-2 is no longer recorded Not connected here", "")]  # a device's own: recorded
    for text in (rules.NETWORK_JOINED_WORDS.format(tags="P-9", verb="is"), rules.NETWORK_JOINED_WORDS.format(tags="P-9, P-10", verb="are")):
        said(text)


# --- every word, and the readers outside a transaction on PostgreSQL ------------------------------------------------------------------

def test_every_word_added_is_never_a_never_word():
    for text in (rules.TO_CONFIRM_SINCE_WORDS, rules.TO_CONFIRM_CHECK_WORDS, rules.ADVISORY_ATTENTION_HEADINGS["changed"],
                 rules.NETWORK_JOINED_WORDS, *WORDS):
        assert not never(text), text


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_the_readers_outside_a_transaction(ctx, floor, vent_model):  # noqa: F811
    """As a GET door runs them (no transaction around the request): each reader these fixes touch, over every state above, reads without
    a lock (FOR UPDATE outside a transaction is an error on PostgreSQL)."""
    a = make_advisory(received_on=TODAY - 2 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-10")
    b = make_advisory(reference="DEMO-ADV-0002")
    model_row(b, floor.model, releases=[floor.r52], fix=floor.r56)
    lapse(decision(b, asset=floor.d["P-1"]))
    update_wo(floor.d["P-1"], floor.r56)
    old, _u = update_wo(floor.d["P-2"], reference="DEMO-PATCH-10")
    complete(old, on=TODAY - 60 * DAY)
    Asset.objects.filter(pk=floor.d["T-1"].pk).update(incident_hold=True)
    c = make_advisory(reference="DEMO-ADV-0003")
    model_row(c, floor.model, versions=NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET)
    shared = decision(c, release=floor.r52, device_model=floor.model, controls=["network_removed"], response=KEEP_WITH,
                      governed_ids=[str(floor.d["P-1"].pk)])
    close_directly(c)
    lapse(shared)
    assert answers(b)["P-1"].kind == rules.ANSWER_REVIEW_DUE
    assert counts(b).reviews_due == 1 and "review and renew it" in advisories.close_refusal(b, TODAY)
    assert (counts(c).named_since, counts(c).reviews_due) == (4, 1)  # P-1 and P-2 the release's, lapsed; the rest none
    assert [ck.key for ck in rules.advisory_checks(c, today=TODAY, devices_visible=True)] == [rules.CHECK_NAMED_SINCE]
    assert [ck.key for ck in rules.decision_checks(DeviceSecurityDecision.objects.select_related(*rules._DECISION_RELATED)
                                                   .get(pk=shared.pk), TODAY)][:1] == [rules.CHECK_NETWORK_BACK]
    att = headings()
    assert att["temporary"].rows and all(r.open_update is None for r in att["temporary"].rows)
    assert all(not never(r.words) for h in att.values() for r in h.rows)
