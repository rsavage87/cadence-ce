"""
Slice 40 review fixes, the decision doors and the device's Security tab (review40 part D):

- #11: Record decision's prefill for an advisory scope. A source that copies nothing (another advisory's "not in use here": no response
  copied, so no controls, and its plan is none) prefills nothing: no note, and the scope's own starting values (Keep in use with
  controls, taken off the network, for a scope recorded "Not connected here"). A source that copies anything keeps its note. The source
  rule stays build40 section 7.5's (support first, else the most recently decided advisory decision of the scope).
- #18 (the Security tab's half): the per-advisory "Open software update" leaves out a line answered by an update done with the version
  not confirmed, as the advisory's page does (NO_OPENER): Confirm version first.
- Smoke S2: the Decisions tab shows both HICP sentences with their shared last sentence said once (views_cyber_decisions.hicp_second);
  a page showing one sentence keeps it whole.
- The GET doors touched take no lock (both databases) and open outside any transaction on PostgreSQL; every word read here is scanned
  with rules.DECISION_NEVER_WORDS.
"""
import re
from html import unescape

import pytest
from cyber_fixtures import facts, make_advisory, model_row, update_wo
from django.db.models.query import QuerySet
from pg_helpers import needs_postgres
from test_cyber_advisory_decisions_web import (  # noqa: F401 (fixtures)
    DAY,
    HX,
    KEEP_WITH,
    KEEP_WITHOUT,
    MODAL,
    NOT_IN_USE,
    checked,
    decide,
    floor,
    new_url,
    signed_in,
)

from apps.cybersecurity import rules
from apps.cybersecurity.models import AdvisoryRemedy, AdvisoryVersions, DecisionPlan, Network
from apps.web import asset_cyber, forms_cyber_decisions, views_cyber_advisories, views_cyber_decisions
from apps.workorders.models import WorkOrder, WoStatus

SEEN: list = []
PREFILLED = "Controls and plan prefilled from"


def body_of(r) -> str:
    """The body, kept for the never-words test: a device drawer whole with rules.NEVER_WORDS (it carries Equipment's own PM words), its
    Advisories section kept for DECISION_NEVER_WORDS."""
    text = unescape(r.content.decode())
    if 'data-section="advisories"' in text:
        assert never(text, rules.NEVER_WORDS) == []
        SEEN.append(text.split('data-section="advisories"', 1)[1].split('<div class="sec', 1)[0])
    else:
        SEEN.append(text)
    return text


def never(text: str, words=rules.DECISION_NEVER_WORDS) -> list:
    return [w for w in words if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def other_advisory(floor, reference="DEMO-ADV-0002", days=5):  # noqa: F811
    """A second advisory naming the pumps (every release, no fix yet), received `days` ago."""
    a = make_advisory(received_on=floor.today - days * DAY, reference=reference)
    model_row(a, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    return a


# --- #11: a source that copies nothing prefills nothing -----------------------------------------------------------------------------

def test_prefill_copies_reads_what_was_copied(floor):  # noqa: F811
    assert forms_cyber_decisions.prefill_copies({}) is False
    other = other_advisory(floor)
    key = rules.advisory_key(floor.adv)
    not_in_use = decide(other, asset=floor.d["P-1"], response=NOT_IN_USE)
    assert forms_cyber_decisions.prefill_copies(forms_cyber_decisions.prefill_initial(not_in_use, floor.today, key)) is False
    kept = decide(other, asset=floor.d["P-2"], response=KEEP_WITHOUT)
    assert forms_cyber_decisions.prefill_copies(forms_cyber_decisions.prefill_initial(kept, floor.today, key)) is True
    for values in ({"plan": DecisionPlan.UPDATE_PLANNED}, {"controls": ["monitored"]}, {"plan_owner": "7"}, {"plan_by": "2027-01-01"}):
        assert forms_cyber_decisions.prefill_copies({"plan": DecisionPlan.NONE, **values}) is True, values


def test_a_not_in_use_source_leaves_a_not_connected_device_starting_off_the_network(client, signed_in, floor):  # noqa: F811
    """P-1 recorded "Not connected here": another advisory's "not in use here" for it neither notes a prefill nor displaces the start."""
    signed_in("director")
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    source = decide(other_advisory(floor), asset=floor.d["P-1"], response=NOT_IN_USE)
    r = client.get(new_url(floor, asset="P-1") + "&from=device", **MODAL)
    body = body_of(r)
    assert r.status_code == 200 and "<form" in body
    assert r.context["prefill_note"] == "" and PREFILLED not in body and source.number not in body
    assert checked(body, "response") == [KEEP_WITH] and checked(body, "controls") == [rules.NETWORK_REMOVED]
    assert r.context["network_note"] == ""


def test_a_not_in_use_source_at_release_scope_shows_no_note_and_nothing_prefilled(client, signed_in, floor):  # noqa: F811
    """The finding's scene: another advisory's live "not in use here" on 5.2 (demo); Record decision for this advisory on 5.2."""
    kim = signed_in("director")
    source = decide(other_advisory(floor), release=floor.r52, response=NOT_IN_USE)
    url = new_url(floor, release=floor.r52.pk)
    r = client.get(url, **MODAL)
    body = body_of(r)
    assert r.context["prefill_note"] == "" and PREFILLED not in body and source.number not in body
    assert checked(body, "response") == [] and checked(body, "controls") == [] and '<option value="none" selected>' in body
    # the source rule stays build40's: the most recently decided advisory decision of the scope, never an older one that would copy
    older = other_advisory(floor, reference="DEMO-ADV-0003", days=30)
    decide(older, release=floor.r52, response=KEEP_WITH, controls=["monitored"], decided_on=floor.today - 25 * DAY)
    r = client.get(url, **MODAL)
    assert r.context["prefill_note"] == "" and checked(body_of(r), "controls") == []
    # a support decision of the scope comes first and copies: its note
    support = decide(release=floor.r52, response=KEEP_WITH, controls=["segmented"], plan=DecisionPlan.UPDATE_PLANNED,
                     plan_by=floor.today + 60 * DAY, plan_owner=kim)
    r = client.get(url, **MODAL)
    body = body_of(r)
    assert r.context["prefill_note"] == forms_cyber_decisions.prefill_note(support, floor.today) and PREFILLED in body
    assert checked(body, "response") == [KEEP_WITH] and checked(body, "controls") == ["segmented"]


def test_a_source_that_copies_keeps_its_note_over_the_not_connected_start(client, signed_in, floor):  # noqa: F811
    """build40 section 7.5's order unchanged where the source gives something: its values and its note, no network_removed preselected."""
    signed_in("director")
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    source = decide(other_advisory(floor), asset=floor.d["P-1"], response=KEEP_WITHOUT)
    r = client.get(new_url(floor, asset="P-1"), **MODAL)
    body = body_of(r)
    assert r.context["prefill_note"] == forms_cyber_decisions.prefill_note(source, floor.today) and PREFILLED in body
    assert checked(body, "response") == [KEEP_WITHOUT] and checked(body, "controls") == []


# --- #18: the Security tab's opener as the advisory page's ----------------------------------------------------------------------------

def _tab_items(client, tag) -> dict:
    r = client.get(f"/equipment/{tag}/?tab=security", **HX)
    assert r.status_code == 200
    body_of(r)
    return {x["number"]: x for x in r.context["cyber"]["advisories"]["items"]}


def _page_openers(client, number) -> dict:
    r = client.get(f"/security/advisories/{number}/", **HX)
    assert r.status_code == 200
    body_of(r)
    return {ln["asset"].tag: ln["open_url"] for row in r.context["ad_rows"] for ln in row["needing"]}


def test_the_tab_offers_no_update_where_one_done_waits_for_its_version(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    n = floor.adv.number
    assert asset_cyber.NO_OPENER == views_cyber_advisories.NO_OPENER  # one rule on both doors
    wo, _u = update_wo(floor.d["P-2"], floor.r56)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=floor.today)
    x = _tab_items(client, "P-2")[n]
    assert [r["kind"] for r in x["responses"]] == [rules.UPDATE_NOT_CONFIRMED]
    assert x["open_url"] == "" and x["record_url"]  # a decision may still be recorded beside it
    body = body_of(client.get("/equipment/P-2/?tab=security", **HX))
    part = body.split('data-section="advisories"', 1)[1].split('<div class="sec', 1)[0]
    assert "Open software update</button>" not in part and "version not confirmed" in part
    # every device's line: the tab offers the opener exactly where the advisory's page does
    page = _page_openers(client, n)
    assert page["P-2"] == "" and page["P-1"]
    for tag in page:
        tab = _tab_items(client, tag).get(n)
        assert bool(tab and tab["open_url"]) == bool(page[tag]), tag
    # Confirm version done elsewhere or not, a device with no update keeps its opener
    assert _tab_items(client, "P-1")[n]["open_url"] == f"/security/devices/P-1/updates/open/?advisory={n}"


# --- smoke S2: the Decisions tab's two HICP sentences ----------------------------------------------------------------------------------

def test_the_shared_last_sentence_ends_both_hicp_sentences():
    """hicp_second drops the tail only while both constants end with it: an edit of either that moves it shows here."""
    tail = views_cyber_decisions.HICP_TAIL
    assert rules.HICP_DECISIONS.endswith(" " + tail) and rules.HICP_ADVISORY_DECISIONS.endswith(" " + tail)
    second = views_cyber_decisions.hicp_second(rules.HICP_DECISIONS, rules.HICP_ADVISORY_DECISIONS)
    assert second == rules.HICP_ADVISORY_DECISIONS[: -len(tail) - 1] and second.endswith("where a patch cannot fix it.")
    assert tail not in second and never(second) == []
    # either without it: the second whole
    assert views_cyber_decisions.hicp_second("Something else.", rules.HICP_ADVISORY_DECISIONS) == rules.HICP_ADVISORY_DECISIONS
    assert views_cyber_decisions.hicp_second(rules.HICP_DECISIONS, "Something else.") == "Something else."


def test_the_decisions_tab_says_the_shared_sentence_once(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    tail = views_cyber_decisions.HICP_TAIL
    d = decide(floor.adv, release=floor.r52)
    r = client.get("/security/decisions/")
    body = body_of(r)
    second = views_cyber_decisions.hicp_second(rules.HICP_DECISIONS, rules.HICP_ADVISORY_DECISIONS)
    assert rules.HICP_DECISIONS in body and second in body and r.context["hicp_advisories"] == second
    assert body.count(tail) == 1 and body.index(rules.HICP_DECISIONS) < body.index(second)
    assert rules.HICP_ADVISORY_DECISIONS not in body
    partial = body_of(client.get("/security/decisions/", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="cy-decisions"))
    assert partial.count(tail) == 1 and second in partial
    # a page showing one sentence keeps it whole: the advisory decision's page and print, Record decision
    for url, headers in ((f"/security/decisions/{d.number}/", HX), (f"/security/decisions/{d.number}/print/", {}),
                         (new_url(floor, asset="P-6"), MODAL)):
        assert rules.HICP_ADVISORY_DECISIONS in body_of(client.get(url, **headers)), url


# --- locks, PostgreSQL, words ------------------------------------------------------------------------------------------------------------

def _doors(floor) -> list:  # noqa: F811
    """The GET doors these fixes touch, in the states they change."""
    return [(new_url(floor, asset="P-1"), MODAL), (new_url(floor, release=floor.r52.pk), MODAL), (new_url(floor, asset="P-2"), MODAL),
            ("/security/decisions/", {}), ("/security/decisions/", HX), ("/equipment/P-2/?tab=security", HX),
            ("/equipment/P-1/?tab=security", HX), (f"/security/advisories/{floor.adv.number}/", HX)]


def _scene(floor):  # noqa: F811
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    other = other_advisory(floor)
    decide(other, asset=floor.d["P-1"], response=NOT_IN_USE)
    decide(other, release=floor.r52, response=NOT_IN_USE)
    decide(other, asset=floor.d["P-2"], response=KEEP_WITHOUT)
    wo, _u = update_wo(floor.d["P-2"], floor.r56)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=floor.today)


@pytest.mark.django_db
def test_the_doors_touched_take_no_lock(client, signed_in, floor, monkeypatch):  # noqa: F811
    signed_in("director")
    _scene(floor)
    locked = []
    real = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        locked.append(self.model.__name__)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    for url, headers in _doors(floor):
        r = client.get(url, **headers)
        assert r.status_code == 200, url
        body_of(r)
    assert locked == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_the_doors_touched_open_outside_a_transaction(client, signed_in, floor):  # noqa: F811
    """No test transaction wraps the request here, as none wraps it in production (FOR UPDATE there is an error on PostgreSQL)."""
    signed_in("director")
    _scene(floor)
    for url, headers in _doors(floor):
        r = client.get(url, **headers)
        assert r.status_code == 200, url
    r = client.get(new_url(floor, asset="P-1"), **MODAL)
    assert r.context["prefill_note"] == "" and checked(unescape(r.content.decode()), "controls") == [rules.NETWORK_REMOVED]
    r = client.get(new_url(floor, asset="P-2"), **MODAL)
    assert r.context["prefill_note"].startswith(PREFILLED)
    x = {x["number"]: x for x in client.get("/equipment/P-2/?tab=security", **HX).context["cyber"]["advisories"]["items"]}
    assert x[floor.adv.number]["open_url"] == ""
    body = unescape(client.get("/security/decisions/").content.decode())
    assert body.count(views_cyber_decisions.HICP_TAIL) == 1


def test_never_words_on_what_these_fixes_show(client, signed_in, floor):  # noqa: F811
    """Run last in this file: every body read above, and these doors in the fixed states."""
    signed_in("director")
    _scene(floor)
    for url, headers in _doors(floor):
        body_of(client.get(url, **headers))
    SEEN.extend([views_cyber_decisions.hicp_second(rules.HICP_DECISIONS, rules.HICP_ADVISORY_DECISIONS), views_cyber_decisions.HICP_TAIL])
    hits = [(w, re.search(rf"(?i).{{0,80}}{re.escape(w)}.{{0,80}}", text).group(0)) for text in SEEN for w in never(text)]
    assert hits == [] and len(SEEN) > 10
