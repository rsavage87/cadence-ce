"""
Slice 40 skeptic fixes, the advisory rules (apps.cybersecurity.rules) and advisories.py's docstrings, on SQLite and PostgreSQL:

- S2: Needs attention's "Updates done, version not confirmed" and "Patches naming another list" headings and the per-device other-list
  Check read the responses of a line that needs a response and is not answered, whatever its kind (rules.unanswered_responses): a device
  under a decision past its review date beside an update done to confirm, or patches on another list, is listed there again (its
  decision under the decisions past review), while the status words and Close keep reading the kind ("review and renew it"); a covering
  decision or the patches recorded on the list it names still hide them.
- S0: a closed advisory's devices named since are rules.named_since_lines, never close_refusal itself: a closed advisory refused only by
  devices under a decision past review is listed under the decisions past review, never named since, and advisories.py's docstrings
  say so.
Every word these tests read is scanned with rules.DECISION_NEVER_WORDS.
"""
import re
from datetime import timedelta

from cyber_fixtures import make_advisory, model_row, update_wo
from django.utils import timezone
from test_cyber_advisories_rules import close_directly, counts, decision, floor  # noqa: F401 (fixtures)

from apps.cybersecurity import advisories, rules
from apps.cybersecurity.models import (
    AdvisoryRemedy,
    AdvisoryVersions,
    DeviceSecurityDecision,
    InstalledBy,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.workorders.models import WorkOrder, WoStatus

TODAY = timezone.localdate()
DAY = timedelta(days=1)
ALL = AdvisoryVersions.ALL


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def said(text: str) -> str:
    assert not never(text), text
    return text


def answers(adv) -> dict:
    reading = rules.advisory_reading([adv], TODAY)
    return {a.line.asset.tag: a for a in reading.answers.values()}


def headings() -> dict:
    att = rules.advisory_attention(TODAY, user=None)
    for h in att.headings:
        said(h.words)
        for r in h.rows:
            said(r.words)
    return {h.key: h for h in att.headings}


def checks(adv, *, devices_visible=True) -> list:
    return [(c.key, said(c.words), c.tags) for c in rules.advisory_checks(adv, today=TODAY, devices_visible=devices_visible)]


def complete(wo, on=None):
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=on or TODAY)


def lapse(d) -> None:
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(review_by=TODAY - DAY)


def patches(asset, reference: str, on=None) -> SoftwareUpdate:
    """The manufacturer's patches recorded by hand on `asset` (a live record, as updates.record makes one)."""
    return SoftwareUpdate.objects.create(asset=asset, outcome=UpdateOutcome.PATCHES, reference=reference, applied_on=on or TODAY - DAY,
                                         installed_by=InstalledBy.MANUFACTURER)


# --- S2: a decision past review beside an update done to confirm, or patches on another list --------------------------------------------

def test_an_update_done_to_confirm_beside_a_decision_past_review_is_listed_to_confirm(ctx, floor):  # noqa: F811
    """build40's scenario: the advisory lists 5.2 (demo) with 5.6 (demo) the fix; P-2 under its own decision, with an update to the fix
    done and the version not confirmed. While the decision covers, nothing lists it; once past review, Needs attention lists the work
    order to confirm (confirming installed 5.6 answers the line outright) and the decision under the decisions past review, while Close
    and the status words say review and renew it."""
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    for tag in ("P-1", "P-5", "T-1"):
        decision(a, asset=floor.d[tag])
    own = decision(a, asset=floor.d["P-2"])
    wo, _u = update_wo(floor.d["P-2"], floor.r56)
    complete(wo)
    assert answers(a)["P-2"].kind == rules.ANSWER_DECIDED
    h = headings()
    assert "not_confirmed" not in h and "decisions_due" not in h  # a covering decision hides it, as before
    assert said(counts(a).words) == "Open: ready to close"
    lapse(own)
    got = answers(a)["P-2"]
    assert got.kind == rules.ANSWER_REVIEW_DUE  # the kind stays the decision's (review 40 #1)
    assert [r.kind for r in got.responses] == [rules.UPDATE_NOT_CONFIRMED, rules.ANSWER_REVIEW_DUE]
    h = headings()
    [row] = h["not_confirmed"].rows
    assert (row.words, row.tags) == (
        f"{a.number} · P-2: Update done {rules.day_words(TODAY)}, version not confirmed: Confirm version ({wo.number})", ("P-2",))
    assert row.work_order == rules.Link("web:wo", (wo.number,)) and row.link == rules.Link("web:asset", ("P-2",), (("tab", "security"),))
    assert h["not_confirmed"].count == 1 and h["decisions_due"].count == 1
    # the kind's readers are unchanged: the status words and Close name the decision past review
    assert said(counts(a).words) == "Open: 1 device under a decision past its review date"
    assert said(advisories.close_refusal(a, TODAY)) == (
        "Not closed: 1 device is under a decision past its review date (P-2): review and renew it.")
    assert counts(a, devices_visible=False).updates[rules.UPDATE_NOT_CONFIRMED] == 1  # the page's count agrees with Needs attention


def test_patches_on_another_list_beside_a_decision_past_review_are_checked_and_listed(ctx, floor):  # noqa: F811
    """Every release, the manufacturer's patches DEMO-PATCH-10; P-1 with patches recorded on DEMO-PATCH-09 under its own decision: hidden
    while it covers; past review, the per-device Check and Needs attention's other list name it. P-2 with the list the advisory names
    recorded too stays answered: never checked or listed."""
    a = make_advisory(received_on=TODAY - 3 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-10")
    patches(floor.d["P-1"], "DEMO-PATCH-09")
    own = decision(a, asset=floor.d["P-1"])
    patches(floor.d["P-2"], "DEMO-PATCH-09")
    patches(floor.d["P-2"], "DEMO-PATCH-10")
    got = answers(a)
    assert (got["P-1"].kind, got["P-2"].kind) == (rules.ANSWER_DECIDED, rules.PATCHES_RECORDED)
    assert rules.PATCHES_OTHER_LIST in {r.kind for r in got["P-2"].responses}  # shown beside the list it names, and answered
    assert checks(a) == []
    assert "other_list" not in headings()
    lapse(own)
    assert answers(a)["P-1"].kind == rules.ANSWER_REVIEW_DUE
    words = f"The manufacturer's patches recorded {rules.day_words(TODAY - DAY)} (DEMO-PATCH-09); the advisory names DEMO-PATCH-10"
    assert checks(a) == [(rules.CHECK_OTHER_LIST, f"P-1: {words}", ("P-1",))]
    assert checks(a, devices_visible=False) == []  # a device's patches: readers of devices only
    h = headings()
    assert [(r.words, r.tags) for r in h["other_list"].rows] == [(f"{a.number} · P-1: {words}", ("P-1",))]
    assert h["decisions_due"].count == 1
    assert "review and renew it" in said(advisories.close_refusal(a, TODAY))


def test_patches_on_another_list_beside_an_open_update_are_checked_and_listed(ctx, floor):  # noqa: F811
    """Whatever the line's kind: P-3 with patches recorded on DEMO-PATCH-09 and an update open to the list the advisory names reads
    "update open" (the status words count it there), and its patches on another list are still checked and listed."""
    a = make_advisory(received_on=TODAY - 3 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-10")
    patches(floor.d["P-3"], "DEMO-PATCH-09")
    wo, _u = update_wo(floor.d["P-3"], reference="DEMO-PATCH-10")
    got = answers(a)["P-3"]
    assert got.kind == rules.UPDATE_OPEN
    assert {r.kind for r in got.responses} == {rules.PATCHES_OTHER_LIST, rules.UPDATE_OPEN}  # shown together (NULLs sort apart by database)
    words = f"The manufacturer's patches recorded {rules.day_words(TODAY - DAY)} (DEMO-PATCH-09); the advisory names DEMO-PATCH-10"
    assert checks(a) == [(rules.CHECK_OTHER_LIST, f"P-3: {words}", ("P-3",))]
    assert [r.tags for r in headings()["other_list"].rows] == [("P-3",)]
    assert counts(a).answers[rules.UPDATE_OPEN] == 1 and counts(a).answers[rules.PATCHES_OTHER_LIST] == 0  # the kind's counts as before
    refusal = said(advisories.close_refusal(a, TODAY))  # Close reads the kind: the update open, never the other list
    assert refusal.endswith("1 device has its update open (P-3): close the advisory when it is done, or record a decision for it.")
    assert "another list" not in refusal


def test_unanswered_responses_read_nothing_from_an_answered_line(ctx, floor):  # noqa: F811
    a = make_advisory()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    wo, _u = update_wo(floor.d["P-1"], floor.r56)
    complete(wo)
    got = answers(a)
    assert [r.number for r in rules.unanswered_responses(got["P-1"], rules.UPDATE_NOT_CONFIRMED)] == [wo.number]
    assert rules.unanswered_responses(got["P-4"], rules.UPDATE_NOT_CONFIRMED) == []  # on the fix: needs no response
    decision(a, asset=floor.d["P-1"])
    assert rules.unanswered_responses(answers(a)["P-1"], rules.UPDATE_NOT_CONFIRMED) == []  # covered: answered


# --- S0: a closed advisory's devices named since are never close_refusal itself -------------------------------------------------------

def test_a_closed_advisory_refused_only_by_a_decision_past_review_is_never_named_since(ctx, floor):  # noqa: F811
    """Closed with a release decision over P-1 and P-2 that then passes its review: close_refusal refuses closing it again (review and
    renew it), yet Needs attention lists it only under the decisions past review, as advisories.py's docstrings now say
    (rules.named_since_lines, never close_refusal, is the predicate it lists a closed advisory by)."""
    a = make_advisory()
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    shared = decision(a, release=floor.r52, device_model=floor.model)
    for tag in ("P-3", "P-4", "P-5", "T-1"):
        decision(a, asset=floor.d[tag])
    close_directly(a, on=TODAY - 2 * DAY)
    lapse(shared)
    assert said(advisories.close_refusal(a, TODAY)) == (
        "Not closed: 2 devices are under a decision past its review date (P-1, P-2): review and renew it.")
    assert rules.named_since_lines(rules.advisory_reading([a], TODAY).answers) == []
    h = headings()
    assert "named_since" not in h and h["decisions_due"].count == 1
    module = " ".join(advisories.__doc__.split())
    assert "through the same predicates (close_refusal" not in module
    assert "a closed advisory's through rules.named_since_lines" in module
    assert "a not-applicable one's through rules.not_applicable_blocks" in module
    doc = " ".join(advisories.close_refusal.__doc__.split())
    assert "Also the predicate Needs attention lists a closed advisory by" not in doc
    assert "Needs attention lists a closed advisory by rules.named_since_lines, never by this reader" in doc
