"""
Slice 40 wave 2B, the device drawer's Security tab, its "Advisories" section (apps/web/asset_cyber.advisory_lines,
web/_cyber_advisory_device.html; build40 section 7.8, critique40_scope N5):

- Which advisories it lists: every open one naming the device's model; a closed one only where the device needs a response under it, or
  one of its live decisions governs the device; a not-applicable one only where the device is named since (its reason no longer holds
  for it), with Reopen it; never one entered in error. The rest counted in words; none: rules.DEVICE_ADVISORIES_EMPTY.
- What each says: its name (a link to its page), status words (rules.advisory_counts'), CVE IDs (three, then "and N more"), the device's
  line, its responses (an update open, the governing decision with its state and sentence, or "No response recorded"), and its doors by
  level: Record decision (can_decide; while it needs a response and nothing covers it; Review and renew for its own decision past
  review), Open software update with the advisory (can_open_updates; a release or patches remedy, no update open), Reopen it (Edit).
- Keyed reads: the support decision's section and the advisory section each read their own need and decisions.
- A fixed number of queries as advisories, rows, decisions, and devices grow; no lock on the GET (both databases; PostgreSQL outside any
  transaction); the whole drawer scanned with rules.NEVER_WORDS, the section with rules.DECISION_NEVER_WORDS.
"""
import re
from datetime import timedelta
from html import unescape

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.db import connection
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from pg_helpers import needs_postgres
from test_cyber_advisory_decisions_web import custom_user, decide, floor, signed_in  # noqa: F401 (fixtures)

from apps.accounts.models import Level, Module
from apps.cybersecurity import rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryRemedy,
    AdvisoryVersions,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    SecurityAdvisory,
)
from apps.equipment.models import Asset, DeviceModel
from apps.web import asset_cyber

HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
DAY = timedelta(days=1)
SEEN: list = []


def never(text: str, words=rules.DECISION_NEVER_WORDS) -> list:
    return [w for w in words if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def tab(client, tag):
    r = client.get(f"/equipment/{tag}/?tab=security", **HX)
    assert r.status_code == 200
    return r


def section(r) -> str:
    """The Advisories section of a device drawer: the whole drawer scanned with rules.NEVER_WORDS (it carries Equipment's own PM words),
    the section kept for DECISION_NEVER_WORDS (SEEN)."""
    body = unescape(r.content.decode())
    assert never(body, rules.NEVER_WORDS) == []
    assert body.index("<h3>Advisories") > body.index("<h3>This device")
    if "<h3>Support decision" in body:
        assert body.index("<h3>Support decision") < body.index("<h3>Advisories")
    part = body.split('data-section="advisories"', 1)[1].split('<div class="sec', 1)[0]
    SEEN.append(part)
    return part


def items(r) -> dict:
    return {x["number"]: x for x in r.context["cyber"]["advisories"]["items"]}


def open_url(tag, number) -> str:
    return f"{reverse('web:cybersecurity_update_open', args=[tag])}?advisory={number}"


# --- what it lists and says ------------------------------------------------------------------------------------------------------------

def test_an_open_advisory_reads_the_devices_line_and_responses_with_its_doors(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    n = floor.adv.number
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(cves=[f"CVE-2026-{1000 + i}" for i in range(5)])
    r = tab(client, "P-1")
    body = section(r)
    x = items(r)[n]
    name = rules.advisory_name(SecurityAdvisory.objects.get(pk=floor.adv.pk))
    assert f'<a class="link" href="/security/advisories/{n}/" hx-get="/security/advisories/{n}/" hx-target="#drawer">{name}</a>' in body
    assert x["status"] == "Open: 5 devices with no response recorded" and x["status"] in body
    assert x["cves"] == "CVE-2026-1000, CVE-2026-1001, CVE-2026-1002 and 2 more"
    assert x["line"] == "On 5.2 (demo), a release the advisory names" and rules.NO_RESPONSE_WORDS in body
    assert x["record_url"] == f"/security/decisions/new/?advisory={n}&asset=P-1&from=device" and "Record decision</button>" in body
    assert x["open_url"] == open_url("P-1", n) and "Open software update</button>" in body and not x["renew_url"] and not x["reopen_url"]
    # an update open to the fix: shown with its work order; a decision may still be recorded beside it (interim controls)
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    r = tab(client, "P-1")
    body = section(r)
    x = items(r)[n]
    assert f'Update open to 5.6 (demo), the fix: <a class="link" href="/work-orders/{wo.number}/"' in body
    assert not x["open_url"] and x["record_url"] and rules.NO_RESPONSE_WORDS not in body
    # a release decision covering it: its number, state, and sentence; nothing more to record here
    d = decide(floor.adv, release=floor.r52, response=DecisionResponse.NOT_IN_USE_HERE)
    r = tab(client, "P-1")
    body = section(r)
    assert f'<a class="link" href="/security/decisions/{d.number}/" hx-get="/security/decisions/{d.number}/" hx-target="#drawer">{d.number}</a>' in body
    assert "In force" in body and rules.decision_words(d, floor.today) in body
    assert not items(r)[n]["record_url"] and not items(r)[n]["renew_url"]


def test_past_review_it_offers_a_device_decision_or_the_renewal_of_its_own(client, signed_in, floor):  # noqa: F811
    signed_in("manager")
    today = floor.today
    n = floor.adv.number
    release_wide = decide(floor.adv, release=floor.r52, decided_on=today - 15 * DAY, review_by=today - DAY)
    r = tab(client, "P-2")
    body = section(r)
    x = items(r)[n]
    assert "Review due" in body and f"Review due since {rules.day_words(today - DAY)}" in body and release_wide.number in body
    assert x["record_url"] == f"/security/decisions/new/?advisory={n}&asset=P-2&from=device" and not x["renew_url"]
    own = decide(floor.adv, asset=floor.d["P-2"], decided_on=today - 15 * DAY, review_by=today - DAY)
    x = items(tab(client, "P-2"))[n]
    assert x["renew_url"] == f"/security/decisions/new/?supersedes={own.number}&from=device" and not x["record_url"]
    # recorded from the tab: the drawer again, on the tab, the decision covering the device
    r = client.post(f"/security/decisions/new/?supersedes={own.number}&from=device",
                    {"response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER,
                     "decided_on": today.isoformat()}, **MODAL)
    renewed = DeviceSecurityDecision.objects.get(supersedes=own)
    assert r.context["tab"] == "security" and renewed.number in section(r)
    assert not items(r)[n]["record_url"] and not items(r)[n]["renew_url"]


def test_a_device_needing_no_response_reads_its_line_and_no_doors(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    n = floor.adv.number
    for tag, line in (("P-4", "On 5.6 (demo), the release the advisory names as the fix"),
                      ("P-5", "On 5.4 (demo), a release the advisory does not name")):
        r = tab(client, tag)
        section(r)
        x = items(r)[n]
        assert x["line"] == line and x["responses"] == [] and not (x["record_url"] or x["open_url"] or x["renew_url"])
    x = items(tab(client, "P-6"))[n]
    assert x["line"] == "Release not recorded: it may run a release the advisory names" and x["record_url"] and x["open_url"]
    # a temporary device: its own decision only (the release's never governs it)
    decide(floor.adv, release=floor.r52)
    x = items(tab(client, "T-1"))[n]
    assert x["record_url"] == f"/security/decisions/new/?advisory={n}&asset=T-1&from=device"


def test_closed_and_not_applicable_advisories_are_listed_only_where_they_name_the_device(client, signed_in, floor):  # noqa: F811
    signed_in("technician")  # Device security Edit, Equipment Edit, Work orders Edit
    today = floor.today
    closed = make_advisory(received_on=today - 10 * DAY, reference="DEMO-ADV-0002", status="closed", status_on=today - DAY)
    model_row(closed, floor.model, releases=[floor.r52], fix=floor.r56)
    na = make_advisory(received_on=today - 8 * DAY, reference="DEMO-ADV-0003", status="not_applicable",
                       not_applicable_reason="releases_not_here", status_on=today - DAY)
    model_row(na, floor.model, releases=[floor.r52], remedy=AdvisoryRemedy.MITIGATIONS)
    gone = make_advisory(received_on=today - 5 * DAY, reference="DEMO-ADV-0004", removed_on=today)
    model_row(gone, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    # P-1 on a release both name: the closed one needs a response from it, the not-applicable one names it since (Reopen it, Edit)
    r = tab(client, "P-1")
    body = section(r)
    found = items(r)
    assert set(found) == {floor.adv.number, closed.number, na.number} and gone.number not in body
    assert found[closed.number]["status"].startswith("Closed (") and "devices named since" in found[closed.number]["status"]
    assert found[na.number]["reopen_url"] == f"/security/advisories/{na.number}/reopen/" and "Reopen it</button>" in body
    assert found[na.number]["status"].startswith("Not applicable here (") and not found[na.number]["record_url"]
    assert not found[floor.adv.number]["record_url"] and found[floor.adv.number]["open_url"]  # Edit: no decision, an update yes
    assert r.context["cyber"]["advisories"]["rest"] == ""
    # P-4 on the fix: only the open one; the other two counted
    r = tab(client, "P-4")
    body = section(r)
    assert set(items(r)) == {floor.adv.number}
    assert asset_cyber.rest_words(2, listed=True) in body
    assert asset_cyber.rest_words(2, listed=True) == "2 other advisories name this model: closed or not applicable, with nothing listed for this device."
    # a closed advisory whose own decision governs the device still (recorded on 5.2, the device since updated to the fix): listed,
    # with its decision
    signed_in("director")
    facts(floor.d["P-4"], floor.r52)
    own = decide(closed, asset=floor.d["P-4"])
    facts(floor.d["P-4"], floor.r56)
    r = tab(client, "P-4")
    assert set(items(r)) == {floor.adv.number, closed.number} and own.number in section(r)
    assert asset_cyber.rest_words(1, listed=True) in section(r)
    # nothing open: the rest counted without "other"
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(status="closed", status_on=today)
    r = tab(client, "P-5")  # on 5.4: a release none of the three names
    assert items(r) == {} and asset_cyber.rest_words(3, listed=False) in section(r)
    assert asset_cyber.rest_words(1, listed=False) == "1 advisory names this model: closed or not applicable, with nothing listed for this device."


def test_a_model_no_advisory_names_reads_the_empty_words(client, signed_in, floor, vent_model, dept):  # noqa: F811
    signed_in("director")
    Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)
    r = tab(client, "V-1")
    assert rules.DEVICE_ADVISORIES_EMPTY in section(r) and items(r) == {}


def test_who_sees_which_doors(client, tenant, signed_in, floor):  # noqa: F811
    n = floor.adv.number
    custom_user(client, tenant, "itview", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    wo, _ = update_wo(floor.d["P-2"], floor.r56)
    r = tab(client, "P-2")
    body = section(r)
    x = items(r)[n]
    assert not (x["record_url"] or x["open_url"] or x["renew_url"] or x["reopen_url"]) and "<button" not in body
    assert f"Update open to 5.6 (demo), the fix: {wo.number}" in body and f'href="/work-orders/{wo.number}/"' not in body  # no Work orders View
    signed_in("technician")
    x = items(tab(client, "P-1"))[n]
    assert x["open_url"] == open_url("P-1", n) and not x["record_url"]
    signed_in("manager")
    x = items(tab(client, "P-1"))[n]
    assert x["record_url"] and x["open_url"]
    # a device held as evidence: no update opened from here
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(incident_hold=True)
    assert not items(tab(client, "P-1"))[n]["open_url"]


def test_each_section_reads_its_own_need(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    support = decide(release=floor.r52)
    r = tab(client, "P-1")
    body = unescape(r.content.decode())
    support_part = body.split("<h3>Support decision", 1)[1].split('<div class="sec', 1)[0]
    assert support.number in support_part and support.number not in section(r)
    assert items(r)[floor.adv.number]["record_url"]  # the support decision answers no advisory
    adv_d = decide(floor.adv, release=floor.r52)
    r = tab(client, "P-1")
    body = unescape(r.content.decode())
    assert adv_d.number not in body.split("<h3>Support decision", 1)[1].split('<div class="sec', 1)[0] and adv_d.number in section(r)
    assert r.context["cyber"]["decision"]["rows"][-1]["line"]["number"] == support.number


# --- fixed queries, locks, words ---------------------------------------------------------------------------------------------------------

def _count(client, url) -> int:
    with CaptureQueriesContext(connection) as q:
        assert client.get(url, **HX).status_code == 200
    return len(q.captured_queries)


def test_the_section_reads_a_fixed_number_of_queries_as_advisories_rows_decisions_and_devices_grow(client, signed_in, floor, dept):  # noqa: F811
    kim = signed_in("director")
    today = floor.today
    closed = make_advisory(received_on=today - 10 * DAY, reference="DEMO-ADV-0002", status="closed", status_on=today - DAY)
    model_row(closed, floor.model, releases=[floor.r52], fix=floor.r56)
    na = make_advisory(received_on=today - 8 * DAY, reference="DEMO-ADV-0003", status="not_applicable", not_applicable_reason="none_here",
                       status_on=today - DAY)
    model_row(na, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.MITIGATIONS)
    decide(floor.adv, release=floor.r52, plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 400 * DAY, plan_owner=kim)
    facts(floor.d["P-4"], floor.r52)  # recorded on 5.2, the device since updated to the fix: its decision still governs it
    decide(closed, asset=floor.d["P-4"], plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 400 * DAY, plan_owner=kim)
    facts(floor.d["P-4"], floor.r56)
    update_wo(floor.d["P-1"], floor.r56)
    urls = [f"/equipment/{tag}/?tab=security" for tag in ("P-1", "P-4", "P-6", "T-1")]
    before = [_count(client, url) for url in urls]
    other = DeviceModel.objects.create(manufacturer="Mindray", model="BeneView T5", description="Patient monitor", category="Monitors")
    release(other, "")
    release(other, "1.0 (demo)")
    for i in range(4):
        p = Asset.objects.create(tag=f"PX-{i}", device_model=floor.model, department=dept)
        facts(p, floor.r52)
        Asset.objects.create(tag=f"M-{i}", device_model=other, department=dept)
        if i % 2:
            update_wo(p, floor.r56)
    for i, status in enumerate(("open", "closed", "not_applicable", "open")):
        extra = {"status_on": today} if status != "open" else {}
        if status == "not_applicable":
            extra["not_applicable_reason"] = "none_here"
        more = make_advisory(received_on=today - 5 * DAY, reference=f"DEMO-ADV-01{i}", cves=["CVE-2026-2000"], status=status, **extra)
        model_row(more, floor.model, releases=[floor.r52], fix=floor.r56)
        model_row(more, other, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
        if status != "not_applicable":
            decide(more, release=floor.r52, plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 500 * DAY, plan_owner=kim)
            decide(more, asset=floor.d["P-1"])
    after = [_count(client, url) for url in urls]
    assert after == before, list(zip(urls, before, after))


def test_the_tab_takes_no_lock(client, signed_in, floor, monkeypatch):  # noqa: F811
    signed_in("director")
    decide(floor.adv, asset=floor.d["P-1"])
    locked = []
    real = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        locked.append(self.model.__name__)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    for tag in ("P-1", "P-2", "P-4", "T-1"):
        tab(client, tag)
    assert locked == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_the_tab_opens_outside_a_transaction(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    decide(floor.adv, asset=floor.d["P-1"])
    for tag in ("P-1", "P-2", "P-4", "P-6", "T-1"):
        assert client.get(f"/equipment/{tag}/?tab=security", **HX).status_code == 200, tag


def test_never_words_in_every_state(client, signed_in, floor):  # noqa: F811
    """Run last in this file: the section in every state read above, and a few more."""
    kim = signed_in("director")
    today = floor.today
    facts(floor.d["P-3"], floor.r52, network=Network.NOT_CONNECTED)
    decide(floor.adv, asset=floor.d["P-3"], response=DecisionResponse.KEEP_WITH_CONTROLS, controls=list(rules.CONTROLS)[:-1],
           plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=kim, decided_on=today - 15 * DAY, review_by=today - DAY)
    patches = make_advisory(received_on=today - 9 * DAY, reference="DEMO-ADV-0005", cves=["CVE-2026-3000"])
    model_row(patches, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    update_wo(floor.d["P-1"], reference="DEMO-PATCH-2026-10")
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "P-6", "T-1"):
        section(tab(client, tag))
    SEEN.extend([asset_cyber.REST_WORDS, asset_cyber.rest_words(3, listed=True), asset_cyber.cves_words([])])
    hits = [(w, re.search(rf"(?i).{{0,80}}{re.escape(w)}.{{0,80}}", text).group(0)) for text in SEEN for w in never(text)]
    assert hits == [] and len(SEEN) > 15
