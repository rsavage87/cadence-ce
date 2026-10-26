"""
Slice 40 wave 2B, the decision doors keyed by need (apps/web/views_cyber_decisions.py, forms_cyber_decisions.py, the decision templates;
build40 sections 7.5 and 7.7):

- Who: Record decision for an advisory's devices, Review and renew, End, and Entered in error need Device security Approve and Equipment
  View; an advisory decision's page, print, and CSVs need Device security View and Equipment View; Edit reads and acts on none; Approve
  without Equipment View and scoped users get 403 everywhere.
- The address: `advisory=<number>` with a scope (asset, release, model; any letter case), the advisory alone a 404, Review and renew
  reading the advisory from the decision renewed (a different one a 404), from=advisory answering with the advisory's page
  (views_cyber_advisories.advisory_saved, a marker here until 2A's merge) and opened directly going there.
- The modal by key: four responses and every control for an advisory (three and the support controls for support, unchanged), the
  advisory's hint and HICP's sentence for advisories, its name and the lines that need a response now in place of reasons,
  network_removed's refusal said beside it before it is tried, a scope recorded "Not connected here" starting at taken off the network.
- Keyed reads never mixing: a support decision never blocks an advisory's scope, nor an advisory decision a support scope or another
  advisory's; an advisory decision's page offers its own Review and renew whatever support decides for the same scope.
- The prefill: from the scope's live decision of another key (support first, past its review), its note, who accepted it and IT's
  exception number left blank, the plan's passed day left blank (the service asks for one), a POST adding them recording it.
- The Decisions tab: the Why filter and column, the CSV's 16 columns, both HICP sentences, the subtitle, an advisory decision's review row
  "For AD-...:"; a decision's page and print naming the advisory, what holds now from its lines, the verb from rules.who_verb.
- Fixed queries as advisories, rows, decisions, and devices grow; no lock taken by a GET (both databases; PostgreSQL outside any
  transaction); rules.DECISION_NEVER_WORDS over every page, modal, print, CSV, and toast read here.
"""
import json
import re
from datetime import date, timedelta
from html import unescape
from types import SimpleNamespace

import pytest
from csvutil import csv_rows, csv_text
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.db import connection
from django.db.models.query import QuerySet
from django.http import HttpResponse
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import advisories, decisions, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryRemedy,
    AdvisoryVersions,
    AppliesTo,
    Connects,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    SecurityAdvisory,
    Segmented,
)
from apps.equipment.models import Asset, DeviceModel, Ownership
from apps.web import forms_cyber_decisions, views_cyber_decisions

HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE, NOT_IN_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                                   DecisionResponse.TAKE_OUT_OF_USE, DecisionResponse.NOT_IN_USE_HERE)
SEEN: list = []  # every body, CSV, and toast these tests read, for the never-words test at the end


def html(r) -> str:
    body = unescape(r.content.decode())
    SEEN.append(body)
    return body


def never(text: str, words=rules.DECISION_NEVER_WORDS) -> list:
    return [w for w in words if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    words = triggers(r).get("toast", {}).get("value", "")
    SEEN.append(words)
    return words


def errors(body: str) -> dict:
    """{field: its error} from a decision modal (ids "sd-<name>_error"), "" for the one at the top."""
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="sd-(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


def checked(body: str, name: str) -> list:
    """The values of the radios or checkboxes named `name` that are checked."""
    return [m.group(1) for m in re.finditer(rf'<input type="(?:radio|checkbox)" name="{name}" value="([^"]+)"[^>]*\bchecked', body)]


def value(body: str, name: str) -> str:
    """The value a text or date field shows ("" for none)."""
    found = re.search(rf'<input type="(?:text|date)" name="{name}"(?: value="([^"]*)")?', body)
    assert found, name
    return found.group(1) or ""


def offered(body: str, name: str) -> list:
    return re.findall(rf'<input type="(?:radio|checkbox)" name="{name}" value="([^"]+)"', body)


def custom_user(client, tenant, slug, levels, *, scope="", login=True, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="Person", **fields)
    if login:
        client.force_login(user)
    return user


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def floor(ctx, dept, pump_model):
    """The pump model can connect (wireless, as disclosed), all on a medical device segment; 5.2 (demo) past its security updates a month
    ago (a support need too), 5.4 (demo) and 5.6 (demo) supported. P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, P-5 on 5.4, P-6 with no
    release recorded, the rental T-1 on 5.2 (due back in 20 days). The advisory, received 20 days ago, names the pump on 5.2 (listed),
    the fix 5.6."""
    today = timezone.localdate()
    profile = release(pump_model, "", connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    r54 = release(pump_model, "5.4 (demo)", security_updates_end_on=date(today.year + 3, 3, 31))
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=date(today.year + 4, 3, 31))
    d = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept, serial=f"SN-{tag}")
         for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "P-6")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(d[tag], r52)
    facts(d["P-4"], r56)
    facts(d["P-5"], r54)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=today + 20 * DAY)
    facts(d["T-1"], r52)
    adv = make_advisory(received_on=today - 20 * DAY, reference="DEMO-ADV-0001")
    row = model_row(adv, pump_model, releases=[r52], fix=r56)
    return SimpleNamespace(today=today, profile=profile, r52=r52, r54=r54, r56=r56, model=pump_model, d=d, dept=dept, adv=adv, row=row)


def decide(adv=None, **fields) -> DeviceSecurityDecision:
    """A decision through the service, as the system: an advisory's with `adv`, else a support decision."""
    values = {"response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "by": None, **fields}
    if adv is not None:
        values.update(applies_to=AppliesTo.ADVISORY, advisory=adv)
    else:
        values.setdefault("reasons", ["security_updates"])
    return decisions.record_decision(**values)


def new_url(floor, **query) -> str:
    """Record decision's address for the advisory (its number in lower case: any letter case) and a scope."""
    params = "&".join(f"{k}={v}" for k, v in query.items())
    return f"/security/decisions/new/?advisory={floor.adv.number.lower()}&{params}"


def urls_for(n: str, floor) -> list:
    return ["/security/decisions/", "/security/decisions/export.csv?why=advisory", new_url(floor, release=floor.r52.pk),
            new_url(floor, asset="P-1"), new_url(floor, model=floor.model.pk), f"/security/decisions/new/?supersedes={n}",
            f"/security/decisions/{n}/", f"/security/decisions/{n}/print/", f"/security/decisions/{n}/devices.csv",
            f"/security/decisions/{n}/end/", f"/security/decisions/{n}/in-error/"]


# --- who -------------------------------------------------------------------------------------------------------------------------------

def test_approve_without_equipment_view_and_scoped_users_get_403_from_every_advisory_door(client, tenant, make_user, floor):
    d = decide(floor.adv, release=floor.r52)
    custom_user(client, tenant, "itsec", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.NONE})
    for url in urls_for(d.number, floor):
        for headers in ({}, HX, MODAL):
            assert client.get(url, **headers).status_code == 403, url
        assert client.post(url, {"response": KEEP_WITHOUT, "accepted_by": "ce_leader"}, **MODAL).status_code == 403
    custom_user(client, tenant, "scoped", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    for url in urls_for(d.number, floor):
        assert client.get(url, **HX).status_code == 403, url
        assert client.post(url, {}, **MODAL).status_code == 403, url
    for who in ("analyst", "vendor", "requester"):
        client.force_login(make_user(who))
        assert all(client.get(url, **HX).status_code == 403 for url in urls_for(d.number, floor)), who
    assert DeviceSecurityDecision.objects.count() == 1 and DeviceSecurityDecision.objects.get().ended_on is None


def test_view_and_edit_read_an_advisory_decision_and_act_on_none(client, tenant, signed_in, floor):
    d = decide(floor.adv, release=floor.r52)
    for user in (signed_in("technician"), custom_user(client, tenant, "itview", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})):
        client.force_login(user)
        page = html(client.get(f"/security/decisions/{d.number}/", **HX))
        assert floor.adv.number in page and "Review and renew" not in page and "Entered in error" not in page and "End</button>" not in page
        assert client.get(f"/security/decisions/{d.number}/print/").status_code == 200
        assert client.get(f"/security/decisions/{d.number}/devices.csv").status_code == 200
        assert client.get("/security/decisions/?why=advisory").status_code == 200
        urls = urls_for(d.number, floor)
        for url in urls[2:6] + urls[9:]:
            assert client.get(url, **MODAL).status_code == 403, url
            assert client.post(url, {}, **MODAL).status_code == 403, url


# --- the address ---------------------------------------------------------------------------------------------------------------------

def test_the_address_names_the_advisory_with_a_scope(client, signed_in, floor):
    signed_in("manager")
    n = floor.adv.number
    assert client.get(f"/security/decisions/new/?advisory={n}", **MODAL).status_code == 404  # the advisory alone
    assert client.get(new_url(floor, release=floor.r52.pk), **MODAL).status_code == 200
    for query in (f"advisory=AD-00-0000&release={floor.r52.pk}", f"advisory={n}&asset=NOPE"):
        assert client.get(f"/security/decisions/new/?{query}", **MODAL).status_code == 404, query
    assert client.get(f"/security/decisions/new/?advisory={n}%00&asset=P-1", **MODAL).status_code in (400, 404)  # a NUL: never a 500
    d = decide(floor.adv, release=floor.r52, decided_on=floor.today - 15 * DAY, review_by=floor.today - DAY)
    support = decide(release=floor.r52)
    other = make_advisory(received_on=floor.today - 3 * DAY, reference="DEMO-ADV-0002")
    # Review and renew reads the advisory from the decision renewed: the same one in any letter case, never another
    assert client.get(f"/security/decisions/new/?supersedes={d.number}&advisory={n.lower()}", **MODAL).status_code == 200
    assert client.get(f"/security/decisions/new/?supersedes={d.number}&advisory={other.number}", **MODAL).status_code == 404
    assert client.get(f"/security/decisions/new/?supersedes={support.number}&advisory={n}", **MODAL).status_code == 404
    r = client.get(f"/security/decisions/new/?supersedes={d.number}", **MODAL)
    assert r.context["scope"].advisory == floor.adv and r.context["form"].fields["response"].widget.choices[-1][0] == NOT_IN_USE
    # opened directly: where it answers
    assert client.get(new_url(floor, release=floor.r52.pk) + "&from=advisory")["Location"] == f"/security/advisories/{n}/"
    assert client.get(new_url(floor, asset="P-1") + "&from=device")["Location"] == "/equipment/P-1/?tab=security"
    assert client.get(f"/security/decisions/new/?supersedes={d.number}&from=advisory")["Location"] == f"/security/advisories/{n}/"
    # the advisory and where it answers never leak into the addresses it leads to (Review and renew keeps the rest)
    body = html(client.get(new_url(floor, release=floor.r52.pk) + "&from=panel&whose=ours", **MODAL))
    assert decisions.IN_FORCE.format(number=d.number) in body
    assert client.get(new_url(floor, release=floor.r52.pk) + "&from=panel&whose=ours", **MODAL).context["renew_url"] == (
        f"/security/decisions/new/?supersedes={d.number}&from=panel&whose=ours")


# --- the modal by key ------------------------------------------------------------------------------------------------------------------

def test_record_for_an_advisory_release_reads_its_own_need_and_records_it(client, signed_in, floor):
    kim = signed_in("director")
    today = floor.today
    url = new_url(floor, release=floor.r52.pk)
    r = client.get(url, **MODAL)
    body = html(r)
    assert r.status_code == 200 and "<form" in body
    assert rules.ADVISORY_DECISION_HINT.format(number=floor.adv.number) in body and rules.HICP_ADVISORY_DECISIONS in body
    assert "For devices past the manufacturer's" not in body and rules.NETWORK_REMOVED_HINT not in body
    assert rules.advisory_name(floor.adv) in body and "On 5.2 (demo), a release the advisory names" in body
    assert 'name="reasons"' not in body and rules.REASONS["security_updates"] not in body
    assert offered(body, "response") == [KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE, NOT_IN_USE] and rules.NOT_IN_USE_HELP in body
    assert offered(body, "controls") == list(rules.CONTROLS) and checked(body, "controls") == [] and checked(body, "response") == []
    assert "Not in use here: " in body and f"Record decision · {floor.adv.number} · Alaris 8015 PCU on 5.2 (demo): 3 devices of ours" in body
    # network_removed: each device it would govern needs its own fact "Not connected here", said beside it before it is tried
    words = decisions.NETWORK_NOT_RECORDED_MANY.format(tags="P-1, P-2, P-3")
    assert r.context["network_note"] == words and f'data-network-note>{words}</span>' in body
    refused = html(client.post(url, {"response": KEEP_WITH, "controls": ["network_removed"], "accepted_by": "ce_leader",
                                     "decided_on": today.isoformat()}, **MODAL))
    assert errors(refused) == {"controls": words}
    # a support reason sent is refused under its field
    assert errors(html(client.post(url, {"response": KEEP_WITHOUT, "accepted_by": "ce_leader", "reasons": ["security_updates"]}, **MODAL))) == {
        "reasons": decisions.ADVISORY_REASONS}
    assert errors(html(client.post(url, {"response": NOT_IN_USE, "accepted_by": "ce_leader", "plan": DecisionPlan.WAIT_FOR_MANUFACTURER},
                                   **MODAL))) == {"plan": decisions.NOT_IN_USE_PLAN}
    r = client.post(url, {"response": NOT_IN_USE, "accepted_by": AcceptedBy.CE_LEADER, "plan": DecisionPlan.NONE,
                          "decided_on": today.isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{d.number} recorded for {floor.adv.number} · Alaris 8015 PCU on 5.2 (demo)."
    assert (d.applies_to, d.advisory, d.reasons, d.response, d.recorded_by, d.advisory_changes) == (
        AppliesTo.ADVISORY, floor.adv, [], NOT_IN_USE, kim, floor.adv.changes)
    assert d.review_by == rules.review_default(NOT_IN_USE, today) and r.context["d"] == d
    page = html(r)
    assert rules.decision_words(d, today) in page and "Decided by" in page and rules.HICP_ADVISORY_DECISIONS in page


def test_a_support_scope_keeps_its_three_responses_and_its_controls(client, signed_in, floor):
    signed_in("manager")
    body = html(client.get(f"/security/decisions/new/?release={floor.r52.pk}", **MODAL))
    assert offered(body, "response") == [KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE] and NOT_IN_USE not in body
    assert offered(body, "controls") == list(rules.SUPPORT_CONTROLS) and rules.NETWORK_REMOVED_HINT in body
    assert rules.HICP_ADVISORY_DECISIONS not in body and "Not in use here" not in body and "data-network-note" not in body
    assert '<input type="hidden" name="reasons" value="security_updates">' in body
    help_text = forms_cyber_decisions.review_help(floor.today)
    assert "Not in use here" not in help_text and "Not in use here: " in forms_cyber_decisions.review_help(floor.today, advisory=True)


def test_a_scope_recorded_not_connected_here_starts_at_taken_off_the_network(client, signed_in, floor):
    signed_in("director")
    today = floor.today
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    url = new_url(floor, asset="P-1") + "&from=device"
    r = client.get(url, **MODAL)
    body = html(r)
    assert checked(body, "response") == [KEEP_WITH] and checked(body, "controls") == ["network_removed"] and r.context["network_note"] == ""
    r = client.post(url, {"response": KEEP_WITH, "controls": ["network_removed"], "accepted_by": AcceptedBy.CE_LEADER,
                          "decided_on": today.isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert d.controls == ["network_removed"] and d.asset == floor.d["P-1"] and r.context["tab"] == "security"
    assert toast_of(r) == f"{d.number} recorded for {floor.adv.number} · P-1."
    # a device recorded connected: nothing preselected, its words beside the control
    facts(floor.d["P-2"], floor.r52, network=Network.WIRELESS)
    r = client.get(new_url(floor, asset="P-2"), **MODAL)
    body = html(r)
    assert checked(body, "response") == [] and checked(body, "controls") == []
    assert r.context["network_note"] == decisions.NETWORK_CONNECTED.format(tag="P-2", connection="connected (wireless)")


def test_what_cannot_be_recorded_for_an_advisory_is_said_before_it_is_tried(client, signed_in, floor, vent_model, dept):
    signed_in("director")

    def said(url) -> str:
        r = client.get(url, **MODAL)
        assert r.status_code == 200, url
        body = html(r)
        assert "<form" not in body, url
        return body

    n = floor.adv.number
    assert decisions.ON_FIX.format(tag="P-4", version="5.6 (demo)", number=n) in said(new_url(floor, asset="P-4"))
    assert decisions.NOT_NAMED.format(tag="P-5", version="5.4 (demo)", number=n) in said(new_url(floor, asset="P-5"))
    assert decisions.RELEASE_IS_FIX.format(version="5.6 (demo)", number=n) in said(new_url(floor, release=floor.r56.pk))
    assert decisions.RELEASE_NOT_NAMED.format(number=n, version="5.4 (demo)") in said(new_url(floor, release=floor.r54.pk))
    Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)
    assert decisions.MODEL_NOT_NAMED.format(number=n, model="Hamilton-G5") in said(new_url(floor, model=vent_model.pk))
    # a device with no release recorded needs a response: the form, never "record it first"
    assert "<form" in html(client.get(new_url(floor, asset="P-6"), **MODAL))
    # the advisory itself: marked not applicable (no live decision names it)
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(status="not_applicable", not_applicable_reason="none_here",
                                                            status_on=floor.today)
    assert decisions.ADVISORY_NOT_APPLICABLE.format(number=n) in said(new_url(floor, asset="P-1"))


# --- keyed reads never mixing ------------------------------------------------------------------------------------------------------------

def test_a_decision_of_one_need_never_blocks_or_answers_another(client, signed_in, floor):
    signed_in("director")
    a = decide(floor.adv, release=floor.r52)
    # the support scope for 5.2: its own need, never the advisory's decision
    body = html(client.get(f"/security/decisions/new/?release={floor.r52.pk}", **MODAL))
    assert "<form" in body and a.number not in body
    # the advisory's scope for 5.2: its own decision in force, Review and renew for it
    r = client.get(new_url(floor, release=floor.r52.pk), **MODAL)
    assert decisions.IN_FORCE.format(number=a.number) in html(r) and r.context["renew_url"] == f"/security/decisions/new/?supersedes={a.number}"
    support = decide(release=floor.r52)
    r = client.get(f"/security/decisions/new/?release={floor.r52.pk}", **MODAL)
    assert decisions.IN_FORCE.format(number=support.number) in html(r) and a.number not in html(r)
    r = client.get(new_url(floor, release=floor.r52.pk), **MODAL)
    assert decisions.IN_FORCE.format(number=a.number) in html(r) and support.number not in html(r)
    # each decision's page offers its own Review and renew; its devices read its own need
    for d in (a, support):
        page = client.get(f"/security/decisions/{d.number}/", **HX)
        assert page.context["renew_url"] == f"/security/decisions/new/?supersedes={d.number}" and page.context["renew_refusal"] == ""
    rows = {row["a"].tag: row for row in client.get(f"/security/decisions/{a.number}/", **HX).context["sd_devices"]}
    assert set(rows) == {"P-1", "P-2", "P-3"} and all(r["governed"] and r["words"] == "This decision" for r in rows.values())
    assert rows["P-1"]["line"] == "On 5.2 (demo), a release the advisory names"
    # a second advisory naming the pumps: neither decision blocks it (the support one prefills it)
    second = make_advisory(received_on=floor.today - 3 * DAY, reference="DEMO-ADV-0002")
    model_row(second, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    r = client.get(f"/security/decisions/new/?advisory={second.number}&release={floor.r52.pk}", **MODAL)
    body = html(r)
    assert "<form" in body and r.context["prefill_note"].startswith(f"Controls and plan prefilled from {support.number} ")


# --- the prefill -------------------------------------------------------------------------------------------------------------------------

def test_the_prefill_from_a_support_decision_past_its_review(client, signed_in, floor):
    """The seeded pumps' case (build40 section 7.5): SD-0001 for the pumps of ours on 5.2, kept in use with controls, an update planned
    by a day already passed (Kim its owner), accepted by IT security with its exception number, past its review."""
    kim = signed_in("director")
    today = floor.today
    passed = today - 10 * DAY
    sd = decide(release=floor.r52, response=KEEP_WITH, controls=["segmented", "traffic_restricted"], plan=DecisionPlan.UPDATE_PLANNED,
                plan_by=passed, plan_owner=kim, accepted_by=AcceptedBy.IT_SECURITY, exception_reference="DEMO-EX-1037",
                decided_on=today - 400 * DAY, review_by=passed)
    url = new_url(floor, release=floor.r52.pk)
    r = client.get(url, **MODAL)
    body = html(r)
    note = forms_cyber_decisions.PREFILL_NOTE.format(number=sd.number, review=f"review due since {rules.day_words(passed)}")
    assert note in body and r.context["prefill_note"] == note
    assert checked(body, "response") == [KEEP_WITH] and checked(body, "controls") == ["segmented", "traffic_restricted"]
    assert '<option value="update_planned" selected>' in body and f'<option value="{kim.pk}" selected>' in body
    assert value(body, "plan_by") == "" and value(body, "exception_reference") == ""  # the plan's day passed; IT's own
    assert '<option value="" selected>Choose…</option>' in body and "DEMO-EX-1037" not in body  # who accepted it: for this advisory
    assert value(body, "decided_on") == today.isoformat() and value(body, "review_by") == ""
    base = {"response": KEEP_WITH, "controls": ["segmented", "traffic_restricted"], "plan": DecisionPlan.UPDATE_PLANNED,
            "plan_owner": str(kim.pk), "decided_on": today.isoformat(), "accepted_by": AcceptedBy.IT_SECURITY}
    # adding who accepted it alone: the plan's day is asked for (it passed), under its field
    assert errors(html(client.post(url, base, **MODAL))) == {"plan_by": decisions.PLAN_DAY[DecisionPlan.UPDATE_PLANNED]}
    r = client.post(url, {**base, "plan_by": (today + 30 * DAY).isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get(applies_to=AppliesTo.ADVISORY)
    assert r["HX-Retarget"] == "#drawer" and (d.controls, d.plan, d.plan_owner, d.accepted_by, d.exception_reference) == (
        ["segmented", "traffic_restricted"], DecisionPlan.UPDATE_PLANNED, kim, AcceptedBy.IT_SECURITY, "")
    sd.refresh_from_db()
    assert sd.ended_on is None  # the support decision stays: another need's


def test_the_prefill_reads_the_latest_advisory_decision_and_copies_only_what_the_form_offers(client, signed_in, floor):
    kim = signed_in("director")
    today = floor.today
    older = make_advisory(received_on=today - 40 * DAY, reference="DEMO-ADV-0003")
    model_row(older, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.MITIGATIONS)
    newer = make_advisory(received_on=today - 30 * DAY, reference="DEMO-ADV-0004")
    model_row(newer, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_PLANNED)
    decide(older, asset=floor.d["P-1"], response=KEEP_WITH, controls=["monitored"], decided_on=today - 35 * DAY)
    latest = decide(newer, asset=floor.d["P-1"], response=NOT_IN_USE, decided_on=today - 25 * DAY)
    r = client.get(new_url(floor, asset="P-1"), **MODAL)
    body = html(r)
    assert r.context["prefill_note"] == forms_cyber_decisions.PREFILL_NOTE.format(
        number=latest.number, review=f"review by {rules.day_words(latest.review_by)}")
    assert checked(body, "response") == [] and checked(body, "controls") == []  # "not in use here" is never another advisory's judgment
    facts(floor.d["P-2"], floor.r52, network=Network.NOT_CONNECTED)  # network_removed needs the device's own fact
    source = decide(newer, asset=floor.d["P-2"], response=KEEP_WITH, controls=["network_removed", "recorded_elsewhere"],
                    controls_reference="CHG-77", plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 200 * DAY, plan_owner=kim)
    initial = forms_cyber_decisions.prefill_initial(source, today, rules.advisory_key(floor.adv))
    assert (initial["response"], initial["controls"], initial["controls_reference"], initial["plan_by"]) == (
        KEEP_WITH, ["network_removed", "recorded_elsewhere"], "CHG-77", (today + 200 * DAY).isoformat())
    support = forms_cyber_decisions.prefill_initial(source, today, rules.SUPPORT_KEY)  # a support form never offers network_removed
    assert support["controls"] == ["recorded_elsewhere"] and support["accepted_by"] == "" and support["exception_reference"] == ""
    body = html(client.get(new_url(floor, asset="P-2"), **MODAL))
    assert checked(body, "controls") == ["network_removed", "recorded_elsewhere"] and 'value="CHG-77"' in body
    assert f'name="plan_by" value="{(today + 200 * DAY).isoformat()}"' in body


# --- where it answers ----------------------------------------------------------------------------------------------------------------

def test_from_advisory_answers_with_the_advisory_page(client, signed_in, floor, monkeypatch):
    signed_in("manager")
    seen = []

    def marker(request, advisory, message):
        seen.append((advisory, message))
        return HttpResponse("advisory page")

    monkeypatch.setattr("apps.web.views_cyber_advisories.advisory_saved", marker)
    url = new_url(floor, model=floor.model.pk) + "&from=advisory"
    r = client.post(url, {"response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "decided_on": floor.today.isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r.content == b"advisory page" and seen == [(floor.adv, f"{d.number} recorded for {floor.adv.number} · Every Alaris 8015 PCU of ours.")]
    assert (d.device_model, d.asset, d.release) == (floor.model, None, None)
    # without an origin: the new decision's page
    second = make_advisory(received_on=floor.today - DAY, reference="DEMO-ADV-0009")
    model_row(second, floor.model, versions=AdvisoryVersions.NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET)
    r = client.post(f"/security/decisions/new/?advisory={second.number}&asset=T-1",
                    {"response": KEEP_WITHOUT, "plan": DecisionPlan.RETURN_TO_OWNER, "accepted_by": AcceptedBy.CE_LEADER}, **MODAL)
    t = DeviceSecurityDecision.objects.get(asset=floor.d["T-1"])
    assert r.context["d"] == t and t.review_by == floor.d["T-1"].due_back_on and t.plan == DecisionPlan.RETURN_TO_OWNER


def test_review_and_renew_an_advisory_decision(client, signed_in, floor):
    signed_in("director")
    today = floor.today
    old = decide(floor.adv, release=floor.r52, response=KEEP_WITH, controls=["traffic_restricted"], exception_reference="DEMO-EX-2001",
                 decided_on=today - 15 * DAY, review_by=today - DAY)
    url = f"/security/decisions/new/?supersedes={old.number}"
    body = html(client.get(url, **MODAL))
    assert f"Review and renew {old.number} · {floor.adv.number} · Alaris 8015 PCU on 5.2 (demo)" in body
    assert f"Recording this ends {old.number} as superseded." in body and rules.HICP_ADVISORY_DECISIONS in body
    assert checked(body, "response") == [KEEP_WITH] and "DEMO-EX-2001" not in body and "data-prefill" not in body
    r = client.post(url, {"response": KEEP_WITH, "controls": ["traffic_restricted"], "accepted_by": AcceptedBy.IT_SECURITY,
                          "decided_on": today.isoformat()}, **MODAL)
    renewed = DeviceSecurityDecision.objects.get(ended_on__isnull=True)
    assert toast_of(r) == f"{renewed.number} recorded for {floor.adv.number} · Alaris 8015 PCU on 5.2 (demo), superseding {old.number}."
    assert renewed.supersedes == old and renewed.advisory == floor.adv and renewed.applies_to == AppliesTo.ADVISORY


# --- the Decisions tab, a decision's page and print ----------------------------------------------------------------------------------------

def test_the_decisions_tab_says_why_and_lists_by_it(client, signed_in, floor):
    signed_in("manager")
    today = floor.today
    a = decide(floor.adv, release=floor.r52, decided_on=today - 15 * DAY, review_by=today - DAY)
    s = decide(device_model=floor.model, decided_on=today - 400 * DAY, review_by=today + 100 * DAY)
    r = client.get("/security/decisions/")
    body = html(r)
    assert rules.HICP_DECISIONS in body and rules.HICP_ADVISORY_DECISIONS in body
    assert "devices past their support, and every decision to review" in body
    rows = {row["d"].number: row for row in r.context["rows"]}
    assert rows[a.number]["why"] == f"Security advisory {floor.adv.number}" and rows[s.number]["why"] == AppliesTo.SUPPORT_ENDED.label
    assert "<th>Why</th>" in body and f"Security advisory {floor.adv.number}" in body

    def numbers(query):
        return [row["d"].number for row in client.get(f"/security/decisions/?{query}").context["rows"]]

    assert numbers("why=advisory") == [a.number] and numbers("why=support") == [s.number] and set(numbers("why=bogus")) == {a.number, s.number}
    assert numbers("why=advisory&state=review_due&here=1") == [a.number]
    listed = html(client.get("/security/decisions/?why=advisory&state=review_due&here=1"))
    assert "?state=review_due&amp;why=advisory" in listed or "?state=review_due&why=advisory" in listed
    # Needs attention: the advisory decision past review, "For AD-...:", Review and renew (a device it governs needs a response)
    by_key = {h["key"]: h for h in r.context["attention"]["headings"]}
    row = by_key["review_due"]["rows"][0]
    assert row["words"].startswith(f"For {floor.adv.number}: {a.number} · Alaris 8015 PCU on 5.2 (demo): 3 devices of ours on site; review due since")
    assert row["record_url"] == f"/security/decisions/new/?supersedes={a.number}"
    csv = csv_rows(client.get("/security/decisions/export.csv?state=all"))
    SEEN.append(csv_text(client.get("/security/decisions/export.csv?state=all")))
    assert csv[0] == views_cyber_decisions.LIST_COLUMNS and len(csv[0]) == 16 and csv[0][-2:] == ["Why", "Advisory"]
    by_number = {line[0]: line for line in csv[1:]}
    assert by_number[a.number][-2:] == [f"Security advisory {floor.adv.number}", floor.adv.number] and by_number[a.number][3] == ""
    assert by_number[s.number][-2:] == [AppliesTo.SUPPORT_ENDED.label, ""]
    assert [line[0] for line in csv_rows(client.get("/security/decisions/export.csv?why=advisory"))[1:]] == [a.number]


def test_an_advisory_decisions_page_print_and_devices_csv(client, signed_in, floor):
    kim = signed_in("director")
    today = floor.today
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    d = decide(floor.adv, release=floor.r52, response=NOT_IN_USE, by=kim)
    r = client.get(f"/security/decisions/{d.number.lower()}/", **HX)
    body = html(r)
    name = rules.advisory_name(floor.adv)
    adv_url = f"/security/advisories/{floor.adv.number}/"
    assert f'Security advisory <a class="link" href="{adv_url}" hx-get="{adv_url}" hx-target="#drawer">{floor.adv.number}</a>' in body
    assert f'<dt>For</dt><dd data-advisory-name><a class="link" href="{adv_url}"' in body and name in body
    assert "Reasons recorded" not in body and rules.HICP_ADVISORY_DECISIONS in body and rules.HICP_DECISIONS not in body
    assert rules.decision_words(d, today) in body and "<dt>Decided by</dt>" in body and rules.who_verb(d) == "Decided by"
    assert r.context["now_lines"] == ["On 5.2 (demo), a release the advisory names"]
    rows = {row["a"].tag: row for row in r.context["sd_devices"]}
    assert rows["P-1"]["words"] == f"Update open to 5.6 (demo), the fix: {wo.number}; This decision"
    assert f'<a class="link" href="/work-orders/{wo.number}/" hx-get="/work-orders/{wo.number}/" hx-target="#drawer">{wo.number}</a>' in body
    # the Check lines by key: the advisory changed since
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(changes=floor.adv.changes + 1, changed_on=today)
    assert rules.ADVISORY_CHANGED_WORDS.format(day=rules.day_words(today)) in html(client.get(f"/security/decisions/{d.number}/", **HX))
    p = client.get(f"/security/decisions/{d.number}/print/")
    page = html(p)
    assert p.status_code == 200 and f"For: {name}" in page and rules.HICP_ADVISORY_DECISIONS in page and rules.HICP_DECISIONS not in page
    assert "<h2>Reasons</h2>" not in page and f"<dd>Security advisory {floor.adv.number}</dd>" in page
    assert "On 5.2 (demo), a release the advisory names" in page
    csv = csv_rows(client.get(f"/security/decisions/{d.number}/devices.csv"))
    SEEN.append(csv_text(client.get(f"/security/decisions/{d.number}/devices.csv")))
    assert csv[0] == views_cyber_decisions.DEVICE_COLUMNS and [line[0] for line in csv[1:]] == ["P-1", "P-2", "P-3"]
    # ended, then its advisory entered in error: its name says so
    decisions.end_decision(d, by=None)
    advisories.mark_entered_in_error(SecurityAdvisory.objects.get(pk=floor.adv.pk), by=None)
    body = html(client.get(f"/security/decisions/{d.number}/", **HX))
    assert f"{name}{rules.ENTERED_IN_ERROR_SINCE}" in body


# --- fixed queries and locks ------------------------------------------------------------------------------------------------------------

def _count(client, url, **headers) -> int:
    with CaptureQueriesContext(connection) as q:
        assert client.get(url, **headers).status_code == 200, url
    return len(q.captured_queries)


def test_the_doors_read_a_fixed_number_of_queries_as_advisories_rows_decisions_and_devices_grow(client, signed_in, floor, dept):
    kim = signed_in("director")
    today = floor.today
    decide(release=floor.r52, response=KEEP_WITH, controls=["segmented"], plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 400 * DAY,
           plan_owner=kim)
    a = decide(floor.adv, device_model=floor.model, plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 400 * DAY, plan_owner=kim,
               decided_on=today - 15 * DAY, review_by=today - DAY)
    decide(floor.adv, asset=floor.d["T-1"])
    update_wo(floor.d["P-1"], floor.r56)
    urls = [("/security/decisions/", {}), ("/security/decisions/?why=advisory", {}), (f"/security/decisions/{a.number}/", HX),
            (f"/security/decisions/{a.number}/print/", {}), (new_url(floor, release=floor.r52.pk), MODAL),
            (new_url(floor, asset="P-2"), MODAL), (f"/security/decisions/new/?supersedes={a.number}", MODAL)]
    before = [_count(client, url, **h) for url, h in urls]
    other = DeviceModel.objects.create(manufacturer="Mindray", model="BeneView T5", description="Patient monitor", category="Monitors")
    release(other, "", connects=Connects.BOTH)
    old = release(other, "1.0 (demo)", security_updates_end_on=today - 100 * DAY)
    for i in range(5):
        p = Asset.objects.create(tag=f"PX-{i}", device_model=floor.model, department=dept)
        facts(p, floor.r52)
        m = Asset.objects.create(tag=f"M-{i}", device_model=other, department=dept)
        facts(m, old)
        if i % 2:
            update_wo(p, floor.r56)
    for i in range(3):
        more = make_advisory(received_on=today - 10 * DAY, reference=f"DEMO-ADV-01{i}")
        model_row(more, floor.model, releases=[floor.r52], fix=floor.r56)
        model_row(more, other, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
        decide(more, release=floor.r52, plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 500 * DAY, plan_owner=kim)
        decide(more, asset=Asset.objects.get(tag=f"M-{i}"))
    decide(floor.adv, asset=floor.d["P-3"], response=OUT_OF_USE)
    after = [_count(client, url, **h) for url, h in urls]
    assert after == before, list(zip([u for u, _ in urls], before, after))


def test_the_advisory_doors_take_no_lock_on_a_get(client, signed_in, floor, monkeypatch):
    """Both databases: every GET reads without FOR UPDATE (refusal_for, would_govern, network_refusal, the prefill's source)."""
    signed_in("director")
    sd = decide(release=floor.r52, decided_on=floor.today - 400 * DAY, review_by=floor.today - DAY)
    a = decide(floor.adv, asset=floor.d["P-1"])
    locked = []
    real = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        locked.append(self.model.__name__)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    for url, headers in ((new_url(floor, release=floor.r52.pk), MODAL), (new_url(floor, asset="P-1"), MODAL),
                         (new_url(floor, model=floor.model.pk), MODAL), (f"/security/decisions/new/?supersedes={a.number}", MODAL),
                         (f"/security/decisions/{a.number}/", HX), (f"/security/decisions/{a.number}/print/", {}),
                         (f"/security/decisions/{sd.number}/", HX), ("/security/decisions/", {})):
        assert client.get(url, **headers).status_code == 200, url
    assert locked == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_every_advisory_decision_door_opens_outside_a_transaction(client, signed_in, floor):
    """No test transaction wraps the request here, as none wraps it in production (FOR UPDATE there is an error on PostgreSQL)."""
    signed_in("director")
    decide(release=floor.r52, decided_on=floor.today - 400 * DAY, review_by=floor.today - DAY)
    a = decide(floor.adv, asset=floor.d["P-1"])
    for url, headers in ((new_url(floor, release=floor.r52.pk), MODAL), (new_url(floor, asset="P-2"), MODAL),
                         (new_url(floor, model=floor.model.pk), MODAL), (new_url(floor, asset="P-1"), MODAL),
                         (f"/security/decisions/new/?supersedes={a.number}", MODAL), (f"/security/decisions/{a.number}/", HX),
                         (f"/security/decisions/{a.number}/", {}), (f"/security/decisions/{a.number}/print/", {}),
                         (f"/security/decisions/{a.number}/devices.csv", {}), ("/security/decisions/?why=advisory", {}),
                         ("/security/decisions/export.csv?why=advisory", {}), (f"/security/decisions/{a.number}/end/", MODAL)):
        assert client.get(url, **headers).status_code == 200, url


# --- words -----------------------------------------------------------------------------------------------------------------------------

def test_never_words_on_every_advisory_decision_door(client, signed_in, floor):
    """Run last in this file: every body, CSV, and toast the tests above read is in SEEN; this test adds each door in more states."""
    kim = signed_in("director")
    today = floor.today
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    with_controls = decide(floor.adv, asset=floor.d["P-1"], response=KEEP_WITH, controls=list(rules.CONTROLS)[:-1],
                           plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=kim, decided_on=today - 15 * DAY, review_by=today - DAY)
    out = decide(floor.adv, asset=floor.d["T-1"], response=OUT_OF_USE, plan=DecisionPlan.RETURN_TO_OWNER)
    model = decide(floor.adv, device_model=floor.model, response=NOT_IN_USE)
    for n in (with_controls.number, out.number, model.number):
        for url, headers in ((f"/security/decisions/{n}/", HX), (f"/security/decisions/{n}/", {}), (f"/security/decisions/{n}/print/", {}),
                             (f"/security/decisions/{n}/end/", MODAL), (f"/security/decisions/{n}/in-error/", MODAL),
                             (f"/security/decisions/new/?supersedes={n}", MODAL)):
            html(client.get(url, **headers))
        SEEN.append(csv_text(client.get(f"/security/decisions/{n}/devices.csv")))
    for url in ("/security/decisions/?why=advisory", "/security/decisions/?state=all", new_url(floor, asset="P-2"),
                new_url(floor, asset="P-6"), new_url(floor, asset="P-4"), new_url(floor, release=floor.r52.pk)):
        html(client.get(url, **(MODAL if "new" in url else {})))
    SEEN.append(csv_text(client.get("/security/decisions/export.csv?state=all")))
    SEEN.extend([forms_cyber_decisions.PREFILL_NOTE, forms_cyber_decisions.review_help(today, advisory=True), views_cyber_decisions.WHY_ADVISORY,
                 *(label for _value, label in views_cyber_decisions.WHY_FILTERS)])
    hits = [(w, re.search(rf"(?i).{{0,80}}{re.escape(w)}.{{0,80}}", text).group(0)) for text in SEEN for w in never(text)]
    assert hits == [] and len(SEEN) > 40
