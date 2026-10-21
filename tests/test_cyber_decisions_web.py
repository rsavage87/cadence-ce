"""
Slice 39 wave 2, the web doors of security decisions (apps/web/views_cyber_decisions.py, forms_cyber_decisions.py, the Security tab's
support decision in asset_cyber.py, a release row's decision in cyber_release_actions.py, and their templates):

- Who: the Decisions tab, a decision's page, its print and CSVs need Device security View and Equipment View; Record decision, Review and
  renew, End, and Entered in error need Device security Approve and Equipment View. A custom role with Device security Approve and no
  Equipment View sees no Decisions tab and gets 403 from every door; Edit reads and never sees an action; scoped users never.
- The Decisions tab: Needs attention (each heading, count, rows, links, Record decision and End for Approve; the empty words) and the
  list with its filters and CSV; the body re-fetches itself (hx-disinherit).
- A decision's page (drawer and full page): the sentence, Check lines, what was decided, the devices with their responses (work order
  and governing decision links), supersedes and superseded by, History; its print and devices' CSV.
- Record decision from each door (a release row, the Security tab, Needs attention, Review and renew), where its save answers, what
  cannot be recorded said before it is tried, a review day already passed said once, and every refusal under its field.
- The Security tab's need, response lines, governing decision and Record decision; a release row's decision or Record decision.
- Fixed query counts for the Decisions tab, the decision page, the Security tab, and the model panel's release rows as decisions and
  devices grow; rules.DECISION_NEVER_WORDS over every new page, modal, print, CSV, and toast (rules.NEVER_WORDS over the whole device
  drawer, which carries Equipment's own PM words).
"""
import json
import re
from datetime import date, timedelta
from html import unescape
from types import SimpleNamespace

import pytest
from csvutil import csv_rows, csv_text
from cyber_fixtures import facts, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    Connects,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    Segmented,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership

HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       DecisionResponse.TAKE_OUT_OF_USE)
SEEN: list = []  # every body, CSV, and toast these tests read, for the never-words test at the end


def html(r) -> str:
    body = unescape(r.content.decode())
    SEEN.append(body)
    return body


def drawer_html(r) -> str:
    """A device drawer: scanned whole with rules.NEVER_WORDS (it carries Equipment's own PM words), its support decision section with
    rules.DECISION_NEVER_WORDS (into SEEN)."""
    body = unescape(r.content.decode())
    assert never(body, rules.NEVER_WORDS) == []
    if "<h3>Support decision" in body:
        SEEN.append(body.split("<h3>Support decision", 1)[1].split('<div class="sec', 1)[0])
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


def number(n: int, today=None) -> str:
    return f"SD-{(today or timezone.localdate()):%y}-{n:04d}"


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
    ago, 5.6 (demo) supported (announced to end in four years). P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, the rental T-1 on 5.2 (due back
    in 20 days)."""
    today = timezone.localdate()
    profile = release(pump_model, "", connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=date(today.year + 4, 3, 31))
    d = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept, serial=f"SN-{tag}") for tag in ("P-1", "P-2", "P-3", "P-4")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(d[tag], r52)
    facts(d["P-4"], r56)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=today + 20 * DAY)
    facts(d["T-1"], r52)
    return SimpleNamespace(today=today, profile=profile, r52=r52, r56=r56, model=pump_model, d=d, dept=dept)


def decide(**fields) -> DeviceSecurityDecision:
    """A decision through the service, as the system (the doors' tests read it back)."""
    values = {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "by": None, **fields}
    return decisions.record_decision(**values)


def urls_for(n: str, floor) -> list:
    """Every decision door: the tab, its CSV, Record decision for each scope, a decision's page, print, devices' CSV, End, Entered in
    error."""
    return ["/security/decisions/", "/security/decisions/export.csv", f"/security/decisions/new/?release={floor.r52.pk}",
            f"/security/decisions/new/?asset={floor.d['P-1'].tag}", f"/security/decisions/new/?model={floor.model.pk}",
            f"/security/decisions/new/?supersedes={n}", f"/security/decisions/{n}/", f"/security/decisions/{n}/print/",
            f"/security/decisions/{n}/devices.csv", f"/security/decisions/{n}/end/", f"/security/decisions/{n}/in-error/"]


# --- who -------------------------------------------------------------------------------------------------------------------------------

def test_approve_without_equipment_view_sees_no_decisions_tab_and_gets_403_everywhere(client, tenant, floor):
    d = decide(release=floor.r52)
    custom_user(client, tenant, "itsec", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.NONE})
    models = html(client.get("/security/models/"))
    assert "/security/decisions/" not in models and ">Decisions</a>" not in models
    for url in urls_for(d.number, floor):
        for headers in ({}, HX, MODAL):
            assert client.get(url, **headers).status_code == 403, url
        assert client.post(url, {"response": KEEP_WITHOUT, "accepted_by": "ce_leader", "reasons": ["security_updates"]}, **MODAL).status_code == 403
    assert DeviceSecurityDecision.objects.count() == 1 and DeviceSecurityDecision.objects.get().ended_on is None


@pytest.mark.parametrize("who", ["analyst", "vendor", "requester", "scoped-full"])
def test_no_access_and_scoped_users_are_refused_every_door(client, tenant, make_user, floor, who):
    d = decide(release=floor.r52)
    if who == "scoped-full":
        custom_user(client, tenant, "scoped", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    else:
        user = make_user(who)
        user.company, user.department = "BD", "ICU"
        user.save()
        client.force_login(user)
    for url in urls_for(d.number, floor):
        assert client.get(url, **HX).status_code == 403, url
        assert client.post(url, {}, **MODAL).status_code == 403, url


def test_edit_reads_every_page_and_acts_on_none(client, signed_in, floor):
    d = decide(release=floor.r52)
    signed_in("technician")  # Device security Edit, Equipment Edit
    models = html(client.get("/security/models/"))
    assert 'href="/security/decisions/"' in models  # the tab
    body = html(client.get("/security/decisions/"))
    assert d.number in body and "Record decision" not in body and "Review and renew" not in body
    page = html(client.get(f"/security/decisions/{d.number}/", **HX))
    assert "Review and renew" not in page and "Entered in error" not in page and "End</button>" not in page and "Print" in page
    assert client.get(f"/security/decisions/{d.number}/print/").status_code == 200
    assert client.get(f"/security/decisions/{d.number}/devices.csv").status_code == 200
    for url in urls_for(d.number, floor)[2:6] + urls_for(d.number, floor)[9:]:
        assert client.get(url, **MODAL).status_code == 403, url
        assert client.post(url, {}, **MODAL).status_code == 403, url
    view_only = User.objects.create_user(username="itview@riverside.example", password="Test-Pass-2026-x", tenant=floor.d["P-1"].tenant,
                                         role=_role("itview", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}))
    client.force_login(view_only)
    assert client.get("/security/decisions/").status_code == 200 and client.get(f"/security/decisions/{d.number}/", **HX).status_code == 200
    assert client.get(f"/security/decisions/new/?release={floor.r52.pk}", **MODAL).status_code == 403


def _role(slug, levels):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return role


# --- the Decisions tab -----------------------------------------------------------------------------------------------------------------

def test_needs_attention_lists_what_needs_a_decision_with_its_doors(client, signed_in, floor):
    signed_in("manager")  # Device security Approve, Equipment Edit
    r = client.get("/security/decisions/")
    body = html(r)
    assert r.status_code == 200 and rules.HICP_DECISIONS in body
    att = r.context["attention"]
    keys = [h["key"] for h in att["headings"]]
    assert keys == ["no_decision", "temporary"]
    group = att["headings"][0]
    assert group["count"] == 3 and len(group["rows"]) == 1
    row = group["rows"][0]
    assert row["words"].startswith("Alaris 8015 PCU on 5.2 (demo): 3 devices of ours; security updates ended after ")
    assert row["url"].startswith(f"/security/models/{floor.model.pk}/?") and "standing=updates_ended" in row["url"]
    assert row["record_url"] == f"/security/decisions/new/?release={floor.r52.pk}"
    temp = att["headings"][1]["rows"][0]
    assert temp["words"].startswith("T-1 (rental, Summit Medical Rentals; due back ") and temp["record_url"] == "/security/decisions/new/?asset=T-1"
    assert f'hx-get="{row["record_url"]}" hx-target="#modal-card">Record decision</button>' in body
    assert 'hx-get="/security/decisions/new/?asset=T-1" hx-target="#modal-card">Record decision</button>' in body
    assert rules.ATTENTION_HEADINGS["no_decision"] in body and rules.ATTENTION_HEADINGS["temporary"] in body
    # the technician reads the same rows without the doors
    signed_in("technician")
    body = html(client.get("/security/decisions/"))
    assert rules.ATTENTION_HEADINGS["no_decision"] in body and "Record decision" not in body


def test_needs_attention_says_its_empty_words_and_its_decision_headings(client, signed_in, floor):
    signed_in("director")
    today = floor.today
    for tag in ("P-1", "P-2", "P-3", "T-1"):
        facts(floor.d[tag], floor.r56)
    body = html(client.get("/security/decisions/"))
    assert rules.ATTENTION_EMPTY in body and "All clear" not in body
    # a decision past its review that still governs a device; one to review within 30 days; one that governs no device here now
    facts(floor.d["P-1"], floor.r52)
    late = decide(asset=floor.d["P-1"], decided_on=today - 400 * DAY, review_by=today - 10 * DAY)
    soon = decide(device_model=floor.model, decided_on=today - 300 * DAY, review_by=today + 10 * DAY, reasons=["security_updates"])
    r = client.get("/security/decisions/")
    body = html(r)
    by_key = {h["key"]: h for h in r.context["attention"]["headings"]}
    assert set(by_key) >= {"review_due", "review_soon"}
    assert by_key["review_due"]["rows"][0]["words"].startswith(f"{late.number} · P-1; review due since ")
    assert by_key["review_due"]["rows"][0]["record_url"] == f"/security/decisions/new/?supersedes={late.number}"
    assert by_key["review_due"]["url"] == "/security/decisions/?state=review_due&here=1"  # the list it counts (review 39 F20)
    assert by_key["review_soon"]["rows"][0]["words"].startswith(f"{soon.number} · Every Alaris 8015 PCU of ours")
    assert "Review and renew</button>" in body
    eq.set_status(floor.d["P-1"], AssetStatus.RETIRED)
    r = client.get("/security/decisions/")
    by_key = {h["key"]: h for h in r.context["attention"]["headings"]}
    gone = by_key["no_device"]["rows"][0]
    assert gone["words"].startswith(f"{late.number} · P-1") and gone["end_url"] == f"/security/decisions/{late.number}/end/"
    assert f'hx-get="/security/decisions/{late.number}/end/" hx-target="#modal-card">End</button>' in html(r)


def test_the_list_its_filters_and_its_csv(client, signed_in, floor):
    signed_in("technician")
    today = floor.today
    a = decide(release=floor.r52, response=KEEP_WITH, controls=["traffic_restricted"], exception_reference="DEMO-EX-1037",
               accepted_by=AcceptedBy.IT_SECURITY)
    b = decide(asset=floor.d["T-1"], decided_on=today - 300 * DAY, review_by=today + 5 * DAY)
    c = decide(device_model=floor.model, decided_on=today - 400 * DAY, review_by=today - 3 * DAY)
    e = decide(asset=floor.d["P-1"])
    decisions.end_decision(e, by=None)
    r = client.get("/security/decisions/")
    body = html(r)
    assert [row["d"].number for row in r.context["rows"]] == [c.number, b.number, a.number]  # not ended, by review day
    assert "Review due since" in body and "Review within 30 days" in body and "In force" in body and "DEMO-EX-1037" in body
    assert 'id="cy-decisions"' in body and 'hx-trigger="cyber-changed from:body"' in body and 'hx-disinherit="hx-swap hx-target"' in body

    def numbers(query):
        return [row["d"].number for row in client.get(f"/security/decisions/?{query}").context["rows"]]

    assert numbers("state=ended") == [e.number] and numbers("state=all") == [c.number, b.number, a.number, e.number]
    assert numbers("state=review_due") == [c.number] and numbers("state=review_soon") == [b.number] and numbers("state=in_force") == [a.number]
    assert numbers("scope=device&state=all") == [b.number, e.number] and numbers("scope=release") == [a.number]
    assert numbers("scope=model") == [c.number] and numbers(f"response={KEEP_WITH}") == [a.number]
    assert numbers("ref=ex-1037") == [a.number] and numbers("ref=nothing") == [] and numbers("state=bogus&scope=x") == [c.number, b.number, a.number]
    part = client.get("/security/decisions/?state=ended", **{**HX, "HTTP_HX_TARGET": "cy-decisions"})
    assert "<html" not in html(part) and 'id="cy-decisions"' in html(part)
    rows = csv_rows(client.get("/security/decisions/export.csv?state=all"))
    SEEN.append(csv_text(client.get("/security/decisions/export.csv?state=all")))
    assert rows[0] == ["Number", "State", "Scope", "Reasons", "Decision", "Controls", "IT's reference for the controls", "Plan", "Accepted by",
                       "IT exception number", "Recorded by", "Decided on", "Review by", "Ended"]
    by_number = {row[0]: row for row in rows[1:]}
    assert set(by_number) == {a.number, b.number, c.number, e.number}
    assert by_number[a.number][1:6] == ["In force", "Alaris 8015 PCU on 5.2 (demo)", rules.REASONS["security_updates"],
                                        "Keep in use with controls", rules.CONTROLS["traffic_restricted"]]
    assert by_number[a.number][9] == "DEMO-EX-1037" and by_number[e.number][13].startswith("Ended ")
    assert len(csv_rows(client.get("/security/decisions/export.csv?state=ended"))) == 2


# --- a decision's page -----------------------------------------------------------------------------------------------------------------

def test_the_decision_page_reads_in_rules_words(client, signed_in, make_user, floor):
    kim = signed_in("director")
    today = floor.today
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    d = decide(release=floor.r52, response=KEEP_WITH, controls=["segmented", "traffic_restricted"], plan=DecisionPlan.UPDATE_PLANNED,
               plan_by=today + 60 * DAY, plan_owner=kim, accepted_by=AcceptedBy.IT_SECURITY, exception_reference="DEMO-EX-1037", by=kim)
    own = decide(asset=floor.d["P-2"])
    r = client.get(f"/security/decisions/{d.number.lower()}/", **HX)  # any letter case
    body = html(r)
    assert r.status_code == 200 and "<html" not in body
    assert rules.decision_words(d, today) in body and rules.scope_words(d, 3) in body
    assert rules.REASONS["security_updates"] in body and "Security updates ended after" in body  # recorded, and what holds now
    assert all(w in body for w in rules.control_words(d)) and rules.plan_words(d) in body and "DEMO-EX-1037" in body
    assert f'<a class="link" href="/work-orders/{wo.number}/" hx-get="/work-orders/{wo.number}/" hx-target="#drawer">{wo.number}</a>' in body
    assert f'Governed by <a class="link" href="/security/decisions/{own.number}/"' in body
    rows = {row["a"].tag: row for row in r.context["sd_devices"]}
    assert set(rows) == {"P-1", "P-2", "P-3"} and rows["P-3"]["words"] == "This decision" and rows["P-3"]["governed"]
    assert rows["P-1"]["words"] == f"Update open to 5.6 (demo): {wo.number}; This decision" and not rows["P-2"]["governed"]
    assert rows["P-1"]["release"].startswith("5.2 (demo)") and rows["P-1"]["connection"] == "Can connect (wireless), as disclosed"
    for action in ("Review and renew", "Entered in error", f"/security/decisions/{d.number}/end/", f"/security/decisions/{d.number}/print/?facility=riverside"):
        assert action in body, action
    # the full page: the tab with the drawer open
    full = html(client.get(f"/security/decisions/{d.number}/"))
    assert '<aside class="drawer open"' in full and rules.decision_words(d, today) in full and 'id="cy-decisions"' in full
    assert client.get("/security/decisions/SD-99-9999/", **HX).status_code == 404
    # the Check lines: the plan's day passed (a decision recorded with past days), with End where it answers one
    old = decide(asset=floor.d["P-3"], plan=DecisionPlan.UPDATE_PLANNED, plan_by=today - 5 * DAY, decided_on=today - 30 * DAY)
    body = html(client.get(f"/security/decisions/{old.number}/", **HX))
    assert rules.PLAN_PASSED_WORDS.format(day=rules.day_words(today - 5 * DAY)) in body
    eq.set_status(floor.d["P-3"], AssetStatus.RETIRED)
    body = html(client.get(f"/security/decisions/{old.number}/", **HX))
    assert rules.NO_DEVICE_WORDS in body and "End it</button>" in body and "No device on site in its scope now." in body


def test_supersedes_history_print_and_devices_csv(client, signed_in, floor):
    signed_in("director")
    first = decide(release=floor.r52, exception_reference="DEMO-EX-1037")
    second = decide(release=floor.r52, supersedes=first, exception_reference="DEMO-EX-1040")
    body = html(client.get(f"/security/decisions/{first.number}/", **HX))
    assert f'href="/security/decisions/{second.number}/"' in body and "Superseded " in body and "Review and renew" not in body
    body = html(client.get(f"/security/decisions/{second.number}/", **HX))
    assert f'href="/security/decisions/{first.number}/"' in body and "Supersedes" in body
    assert f'hx-get="/security/decisions/{second.number}/?history=1" hx-target="#hist"' in body
    hist = client.get(f"/security/decisions/{first.number}/?history=1", **{**HX, "HTTP_HX_TARGET": "hist"})
    assert "<html" not in html(hist) and decisions.SUPERSEDED_BY.format(number=second.number) in html(hist)
    p = client.get(f"/security/decisions/{second.number}/print/")
    page = html(p)
    assert p.status_code == 200 and "print-bar" in page and rules.decision_words(second, floor.today) in page
    assert "As recorded in Cadence; IT's exception record (DEMO-EX-1040) is the system of record." in page and rules.HICP_DECISIONS in page
    assert all(t in page for t in ("P-1", "SN-P-1", "Alaris 8015 PCU", "ICU", "Can connect (wireless), as disclosed", "5.2 (demo)"))
    assert "T-1" not in page  # a release decision is for devices of ours
    rows = csv_rows(client.get(f"/security/decisions/{second.number}/devices.csv"))
    SEEN.append(csv_text(client.get(f"/security/decisions/{second.number}/devices.csv")))
    assert rows[0] == ["Tag", "Serial", "Manufacturer", "Model", "Department", "Connection", "Release", "Response"]
    assert [row[0] for row in rows[1:]] == ["P-1", "P-2", "P-3"] and rows[1][1] == "SN-P-1" and rows[1][7] == "This decision"


# --- Record decision -------------------------------------------------------------------------------------------------------------------

def test_record_from_a_release_row_answers_with_the_panel(client, signed_in, make_user, floor):
    kim = signed_in("director")
    today = floor.today
    url = f"/security/decisions/new/?release={floor.r52.pk}&from=panel&whose=ours"
    r = client.get(url, **MODAL)
    body = html(r)
    assert r.status_code == 200 and '<input type="hidden" name="reasons" value="security_updates">' in body
    assert rules.REASONS["security_updates"] in body and rules.NETWORK_REMOVED_HINT in body and rules.CONTROLS_REFERENCE_HELP in body
    assert "network_removed" not in body and all(words in body for words in rules.SUPPORT_CONTROLS.values())
    assert rules.review_rule_words(KEEP_WITH)[3:] in body and f'value="{today.isoformat()}"' in body
    assert "Return it to its owner" not in body  # a release's decision
    assert client.get(url).status_code == 302  # opened directly: the panel
    r = client.post(url, {"reasons": ["security_updates"], "response": KEEP_WITH, "controls": ["traffic_restricted", "segmented"],
                          "plan": DecisionPlan.UPDATE_PLANNED, "plan_by": (today + 90 * DAY).isoformat(), "plan_owner": str(kim.pk),
                          "accepted_by": AcceptedBy.IT_SECURITY, "exception_reference": "EX-1037", "decided_on": today.isoformat(),
                          "review_by": ""}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{number(1)} recorded for Alaris 8015 PCU on 5.2 (demo)."
    assert {"cyber-changed", "modal-close"} <= set(triggers(r))
    panel = html(r)
    assert "Alaris 8015 PCU" in panel and f'Security decision <a class="link" href="/security/decisions/{d.number}/"' in panel
    assert (d.release, d.response, d.controls, d.plan_owner, d.recorded_by, d.review_by) == (
        floor.r52, KEEP_WITH, ["segmented", "traffic_restricted"], kim, kim, rules.review_default(KEEP_WITH, today))
    # the scope already decided: said before it is tried, with Review and renew
    r = client.get(url, **MODAL)
    body = html(r)
    assert decisions.IN_FORCE.format(number=d.number) in body and "Review and renew</button>" in body and "<form" not in body
    assert r.context["renew_url"] == f"/security/decisions/new/?supersedes={d.number}&from=panel&whose=ours"  # the panel's filters kept


def test_record_from_the_security_tab_answers_with_the_device_drawer(client, signed_in, floor):
    signed_in("manager")
    tag = floor.d["T-1"].tag
    url = f"/security/decisions/new/?asset={tag}&from=device"
    body = html(client.get(url, **MODAL))
    assert "Return it to its owner" in body and "the day it is due back" in body  # a rental's own decision
    r = client.post(url, {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "plan": DecisionPlan.RETURN_TO_OWNER,
                          "accepted_by": AcceptedBy.CE_LEADER, "decided_on": floor.today.isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{d.number} recorded for T-1."
    drawer = drawer_html(r)
    assert r.context["tab"] == "security" and r.context["asset"].tag == "T-1" and f'href="/security/decisions/{d.number}/"' in drawer
    assert d.review_by == floor.d["T-1"].due_back_on and d.plan == DecisionPlan.RETURN_TO_OWNER


def test_record_from_needs_attention_and_review_and_renew_answer_with_the_decision(client, signed_in, floor):
    signed_in("director")
    today = floor.today
    r = client.post(f"/security/decisions/new/?model={floor.model.pk}", {
        "reasons": ["security_updates"], "response": OUT_OF_USE, "accepted_by": AcceptedBy.COMMITTEE, "decided_on": today.isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{d.number} recorded for Every Alaris 8015 PCU of ours." and r.context["d"] == d
    assert d.review_by == rules.review_default(OUT_OF_USE, today) and d.device_model == floor.model and d.release is None
    # Review and renew: prefilled from it, IT's exception number blank
    url = f"/security/decisions/new/?supersedes={d.number}"
    body = html(client.get(url, **MODAL))
    assert f"Review and renew {d.number}" in body and f'value="{OUT_OF_USE}" ' in body and "checked" in body.split(OUT_OF_USE)[1][:80]
    assert '<option value="committee" selected>' in body and 'name="exception_reference" ' in body
    assert f"Recording this ends {d.number} as superseded." in body
    r = client.post(url, {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.COMMITTEE,
                          "exception_reference": "DEMO-EX-2001", "decided_on": today.isoformat()}, **MODAL)
    renewed = DeviceSecurityDecision.objects.get(ended_on__isnull=True)
    assert toast_of(r) == f"{renewed.number} recorded for Every Alaris 8015 PCU of ours, superseding {d.number}."
    assert r.context["d"] == renewed and renewed.supersedes == d
    d.refresh_from_db()
    assert (d.ended_on, d.end_reason) == (today, DecisionEnd.SUPERSEDED)
    body = html(client.get(url, **MODAL))  # the one ended cannot be renewed again
    assert decisions.ALREADY_ENDED.format(number=d.number, day=rules.day_words(today)) in body and "<form" not in body
    assert client.get(url).status_code == 302 and client.get("/security/decisions/new/", **MODAL).status_code == 404


def test_what_cannot_be_recorded_is_said_before_it_is_tried(client, signed_in, floor, vent_model, dept):
    signed_in("director")

    def said(query) -> str:
        r = client.get(f"/security/decisions/new/?{query}", **MODAL)
        assert r.status_code == 200
        body = html(r)
        assert "<form" not in body, query
        return body

    assert decisions.NOT_NEEDED.format(name="P-4") in said("asset=P-4")
    facts(floor.d["P-3"], floor.r52, network=Network.NOT_CONNECTED)
    assert decisions.NOT_CONNECTED.format(tag="P-3") in said("asset=p-3")  # any letter case
    vent = Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)
    assert decisions.CONNECTION_UNKNOWN.format(tag="V-1") in said("asset=V-1")
    eq.set_status(floor.d["P-2"], AssetStatus.RETIRED)
    assert decisions.RETIRED.format(tag="P-2") in said("asset=P-2")
    empty = release(floor.model, "4.0 (demo)", security_updates_end_on=floor.today - 400 * DAY)
    assert decisions.NO_DEVICE_OURS.format(version="4.0 (demo)") in said(f"release={empty.pk}")
    unused = DeviceModel.objects.create(manufacturer="Acme", model="Unused 1", description="Monitor", category="Monitors")
    assert decisions.NO_MODEL_DEVICE.format(model="Unused 1") in said(f"model={unused.pk}")
    assert decisions.NONE_NEEDED in said(f"release={floor.r56.pk}") and decisions.NONE_NEEDED in said(f"model={vent_model.pk}")
    for query in ("asset=NOPE", f"release={vent.pk}", "model=nope", "supersedes=SD-00-0000"):
        assert client.get(f"/security/decisions/new/?{query}", **MODAL).status_code == 404, query


def test_every_refusal_comes_back_under_its_field(client, signed_in, floor, make_user):
    signed_in("director")
    today = floor.today
    url = f"/security/decisions/new/?release={floor.r52.pk}"
    base = {"reasons": ["security_updates"], "decided_on": today.isoformat()}

    def refused(**fields) -> dict:
        r = client.post(url, {**base, **fields}, **MODAL)
        assert r.status_code == 200 and "HX-Retarget" not in r, fields
        found = errors(html(r))
        assert found and not [w for w in found.values() if never(w)], found
        return found

    assert refused() == {"response": decisions.RESPONSE_WORDS, "accepted_by": decisions.ACCEPTED_WORDS}
    ok = {"response": KEEP_WITH, "accepted_by": AcceptedBy.CE_LEADER}
    assert refused(**ok) == {"controls": decisions.CONTROLS_NEEDED}
    assert refused(**ok, controls=["network_removed"]) == {"controls": rules.NETWORK_REMOVED_HINT}
    assert refused(**ok, controls=["bogus"]) == {"controls": decisions.CONTROLS_WORDS}
    assert refused(**ok, controls=["recorded_elsewhere"]) == {"controls_reference": decisions.REFERENCE_NEEDED}
    assert refused(**ok, controls=["monitored"], controls_reference="CHG1") == {"controls_reference": decisions.REFERENCE_ONLY_WITH}
    assert refused(**ok, controls=["monitored"], exception_reference="10.1.2.0/24") == {"exception_reference": decisions.NOT_AN_ADDRESS}
    assert refused(**ok, controls=["monitored"], exception_reference="VLAN20") == {"exception_reference": decisions.NOT_AN_ADDRESS}
    assert refused(**ok, controls=["monitored"], exception_reference="two words") == {"exception_reference": decisions.REFERENCE_WORDS}
    assert refused(response=KEEP_WITHOUT, accepted_by="ce_leader", controls=["monitored"]) == {"controls": decisions.CONTROLS_ONLY_WITH}
    plain = {"response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER}
    assert refused(**plain, plan=DecisionPlan.UPDATE_PLANNED) == {"plan_by": decisions.PLAN_DAY[DecisionPlan.UPDATE_PLANNED]}
    assert refused(**plain, plan=DecisionPlan.NONE, plan_by=today.isoformat()) == {"plan_by": decisions.PLAN_DAY_ONLY}
    assert refused(**plain, plan=DecisionPlan.REPLACE_PLANNED, plan_by=(today - 5 * DAY).isoformat()) == {"plan_by": decisions.PLAN_BEFORE}
    assert refused(**plain, plan="bogus") == {"plan": decisions.PLAN_WORDS}
    assert refused(**plain, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner="999999") == {"plan_owner": decisions.CHOOSE_OWNER}
    analyst = make_user("analyst")  # no Device security View: never an owner
    assert refused(**plain, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=str(analyst.pk)) == {"plan_owner": decisions.CHOOSE_OWNER}
    assert refused(**plain, plan_owner=str(User.objects.get(username="director@riverside.example").pk)) == {
        "plan_owner": decisions.OWNER_ONLY_WITH}
    assert refused(**plain, plan=DecisionPlan.RETURN_TO_OWNER) == {"plan": decisions.RETURN_ONLY_TEMPORARY}
    assert refused(**plain, decided_on=(today + DAY).isoformat()) == {"decided_on": "The day decided cannot be after today."}
    assert refused(**plain, review_by=today.isoformat()) == {"review_by": decisions.REVIEW_AFTER}
    cap = rules.review_cap(KEEP_WITHOUT, today)
    assert refused(**plain, review_by=(cap + DAY).isoformat()) == {
        "review_by": decisions.REVIEW_CAP.format(day=rules.day_words(cap), months=rules.REVIEW_CAP_MONTHS)}
    assert refused(**plain, reasons=["os_support"]) == {"reasons": decisions.REASONS_NOW.format(
        words=rules.REASONS["security_updates"][:1].lower() + rules.REASONS["security_updates"][1:])}
    floor.profile.segmented = Segmented.NONE
    floor.profile.save()
    assert refused(**ok, controls=["segmented"]) == {"controls": decisions.SEGMENT_REFUSAL}
    # what was chosen is kept
    r = client.post(url, {**base, **plain, "plan": DecisionPlan.UPDATE_PLANNED, "exception_reference": "EX-77"}, **MODAL)
    assert '<option value="update_planned" selected>' in html(r) and 'value="EX-77"' in html(r) and 'autofocus' in html(r)
    # a decision in force meanwhile: at the top
    decide(release=floor.r52)
    assert refused(**plain) == {"": decisions.IN_FORCE.format(number=number(1))}
    assert DeviceSecurityDecision.objects.count() == 1


def test_a_review_day_already_passed_is_said_once_before_it_is_saved(client, signed_in, floor):
    signed_in("director")
    today = floor.today
    url = "/security/decisions/new/?asset=P-1"
    fields = {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.IT_SECURITY,
              "decided_on": (today - 400 * DAY).isoformat(), "review_by": (today - 35 * DAY).isoformat()}
    r = client.post(url, fields, **MODAL)
    body = html(r)
    note = decisions.REVIEW_PASSED.format(day=rules.day_words(today - 35 * DAY))
    assert note in body and not DeviceSecurityDecision.objects.exists() and errors(body) == {}
    assert f'name="review_passed" value="{(today - 35 * DAY).isoformat()}"' in body
    r = client.post(url, {**fields, "review_passed": (today - 35 * DAY).isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r["HX-Retarget"] == "#drawer" and d.review_by == today - 35 * DAY
    assert rules.decision_words(d, today).startswith(f"Review due since {rules.day_words(today - 35 * DAY)}")
    # a blank review day whose default has passed is said too
    fields = {"reasons": ["security_updates"], "response": OUT_OF_USE, "accepted_by": AcceptedBy.COMMITTEE,
              "decided_on": (today - 200 * DAY).isoformat()}
    body = html(client.post("/security/decisions/new/?asset=P-2", fields, **MODAL))
    default = rules.review_default(OUT_OF_USE, today - 200 * DAY)
    assert decisions.REVIEW_PASSED.format(day=rules.day_words(default)) in body
    r = client.post("/security/decisions/new/?asset=P-2", {**fields, "review_passed": default.isoformat()}, **MODAL)
    assert r["HX-Retarget"] == "#drawer" and DeviceSecurityDecision.objects.get(asset=floor.d["P-2"]).review_by == default


# --- End, Entered in error ---------------------------------------------------------------------------------------------------------------

def test_end_and_entered_in_error(client, signed_in, floor):
    signed_in("manager")
    today = floor.today
    d = decide(release=floor.r52)
    body = html(client.get(f"/security/decisions/{d.number}/end/", **MODAL))
    assert "End it</button>" in body and "no longer needed" in body
    assert client.get(f"/security/decisions/{d.number}/end/").status_code == 302
    r = client.post(f"/security/decisions/{d.number}/end/", {}, **MODAL)
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{d.number} ended: no longer needed." and r.context["d"] == d
    d.refresh_from_db()
    assert (d.ended_on, d.end_reason) == (today, DecisionEnd.NO_LONGER_NEEDED)
    body = html(client.get(f"/security/decisions/{d.number}/end/", **MODAL))
    assert decisions.ALREADY_ENDED.format(number=d.number, day=rules.day_words(today)) in body and "<form" not in body
    r = client.post(f"/security/decisions/{d.number}/in-error/", {}, **MODAL)  # ended meanwhile: the service's words at the top
    assert errors(html(r)) == {"": decisions.ALREADY_ENDED.format(number=d.number, day=rules.day_words(today))}
    other = decide(asset=floor.d["T-1"])
    body = html(client.get(f"/security/decisions/{other.number}/in-error/", **MODAL))
    assert "Mark entered in error</button>" in body
    r = client.post(f"/security/decisions/{other.number}/in-error/", {}, **MODAL)
    assert toast_of(r) == f"{other.number} marked entered in error."
    other.refresh_from_db()
    assert other.end_reason == DecisionEnd.ENTERED_IN_ERROR
    page = html(client.get(f"/security/decisions/{other.number}/", **HX))
    assert "Entered in error " in page and "Review and renew" not in page


# --- the Security tab and a release row ------------------------------------------------------------------------------------------------

def tab(client, asset):
    return client.get(f"/equipment/{asset.tag}/?tab=security", **HX)


def test_the_security_tab_shows_the_need_the_responses_and_the_decision(client, signed_in, floor):
    signed_in("director")
    p1 = floor.d["P-1"]
    r = tab(client, p1)
    body = drawer_html(r)
    dec = r.context["cyber"]["decision"]
    assert dec["need"].key == rules.NEEDS and dec["need"].words in body and rules.NO_DECISION_WORDS in body
    assert dec["record_url"] == "/security/decisions/new/?asset=P-1&from=device" and "Record decision</button>" in body
    assert tab(client, floor.d["P-4"]).context["cyber"]["decision"] is None  # supported: nothing to show
    wo, _ = update_wo(p1, floor.r56)
    body = drawer_html(tab(client, p1))
    assert f'Update open to 5.6 (demo): <a class="link" href="/work-orders/{wo.number}/"' in body and rules.NO_DECISION_WORDS not in body
    d = decide(release=floor.r52)
    r = tab(client, p1)
    body = drawer_html(r)
    assert f'<a class="link" href="/security/decisions/{d.number}/" hx-get="/security/decisions/{d.number}/" hx-target="#drawer">{d.number}</a>' in body
    assert rules.decision_words(d, floor.today) in body and "In force" in body
    assert r.context["cyber"]["decision"]["record_url"]  # a device decision may still be recorded beside the release's
    own = decide(asset=p1)
    dec = tab(client, p1).context["cyber"]["decision"]
    assert dec["renew_url"] == f"/security/decisions/new/?supersedes={own.number}&from=device" and not dec["record_url"]
    drawer_html(tab(client, p1))  # the whole drawer with rules.NEVER_WORDS, its decision section with DECISION_NEVER_WORDS
    signed_in("technician")
    body = drawer_html(tab(client, p1))
    assert own.number in body and "Record decision" not in body and "Review and renew" not in body


def test_the_security_tab_history_is_the_facts_own_and_the_decision_page_has_its_own(client, signed_in, floor):
    """Pinned (build39 section 9): the Security tab's History stays the device's DeviceSecurity row's (history_tabs.RELATED keys a related
    area by the record's own id, and that record is not the device), never a device-scope decision's; the decision's page has its own,
    linked from the tab by its number."""
    signed_in("director")
    d = decide(asset=floor.d["P-1"])
    decisions.end_decision(d, by=None)
    again = decide(asset=floor.d["P-1"])
    hist = tab(client, floor.d["P-1"]).context["cyber"]["hist"]
    assert hist["rows"] and {row["e"].area for row in hist["rows"]} == {"device_security"}
    page = client.get(f"/security/decisions/{d.number}/?history=1", **{**HX, "HTTP_HX_TARGET": "hist"})
    assert [row["e"].reason for row in page.context["hist"]["rows"]] == [decisions.ENDED_NO_LONGER_NEEDED, decisions.RECORDED]
    assert again.number in drawer_html(tab(client, floor.d["P-1"]))


def test_a_release_row_shows_its_decision_or_record_decision(client, signed_in, floor):
    signed_in("director")
    url = f"/security/models/{floor.model.pk}/"
    r = client.get(url, **HX)
    rows = {x["r"].version: x["decision"] for x in r.context["releases"]}
    assert rows["5.2 (demo)"]["record_url"].startswith(f"/security/decisions/new/?release={floor.r52.pk}&from=panel")
    assert rows["5.6 (demo)"] is None and "Record decision</button>" in html(r)
    signed_in("technician")
    assert "Record decision" not in html(client.get(url, **HX))
    d = decide(release=floor.r52)
    body = html(client.get(url, **HX))
    assert f'Security decision <a class="link" href="/security/decisions/{d.number}/"' in body and rules.review_words(d, floor.today) in body
    model_wide = decide(device_model=floor.model)
    rows = {x["r"].version: x["decision"] for x in client.get(url, **HX).context["releases"]}
    assert rows["5.2 (demo)"]["number"] == d.number and rows["5.6 (demo)"]["number"] == model_wide.number and rows["5.6 (demo)"]["model_wide"]
    # Device security View without Equipment View: the panel, no decisions
    custom_user(client, floor.d["P-1"].tenant, "modelsonly", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.NONE})
    r = client.get(url, **HX)
    assert r.status_code == 200 and all(x["decision"] is None for x in r.context["releases"]) and d.number not in html(r)


# --- fixed queries ---------------------------------------------------------------------------------------------------------------------

def _count(client, url, **headers) -> int:
    with CaptureQueriesContext(connection) as q:
        assert client.get(url, **headers).status_code == 200, url
    return len(q.captured_queries)


def test_the_doors_read_a_fixed_number_of_queries_as_decisions_and_devices_grow(client, signed_in, floor, dept):
    kim = signed_in("director")
    today = floor.today
    d = decide(release=floor.r52, plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 400 * DAY, plan_owner=kim)
    decide(asset=floor.d["T-1"])
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    urls = [("/security/decisions/", {}), ("/security/decisions/?state=all", {}), (f"/security/decisions/{d.number}/", HX),
            (f"/equipment/{floor.d['P-2'].tag}/?tab=security", HX), (f"/security/models/{floor.model.pk}/", HX),
            (f"/security/decisions/{d.number}/print/", {})]
    before = [_count(client, url, **h) for url, h in urls]
    other = DeviceModel.objects.create(manufacturer="Mindray", model="BeneView T5", description="Patient monitor", category="Monitors")
    release(other, "", connects=Connects.BOTH)
    old = release(other, "1.0 (demo)", security_updates_end_on=today - 100 * DAY)
    for i in range(6):
        a = Asset.objects.create(tag=f"PX-{i}", device_model=floor.model, department=dept)
        facts(a, floor.r52)
        m = Asset.objects.create(tag=f"M-{i}", device_model=other, department=dept)
        facts(m, old)
        if i % 2:
            update_wo(a, floor.r56)
    decide(asset=Asset.objects.get(tag="M-0"), plan=DecisionPlan.UPDATE_PLANNED, plan_by=today + 30 * DAY)
    decide(device_model=other, decided_on=today - 400 * DAY, review_by=today - DAY)
    decide(asset=Asset.objects.get(tag="PX-0"), response=OUT_OF_USE)
    # P-2 now governed by its own decision (a replacement planned, as the release's was: its words read the fiscal year once either way)
    decide(asset=floor.d["P-2"], plan=DecisionPlan.REPLACE_PLANNED, plan_by=today + 700 * DAY, plan_owner=kim)
    after = [_count(client, url, **h) for url, h in urls]
    assert after == before, list(zip([u for u, _ in urls], before, after))


# --- words -----------------------------------------------------------------------------------------------------------------------------

def test_never_words_on_every_page_modal_print_csv_and_toast(client, signed_in, floor):
    """Run last in this file: every body, CSV, and toast the tests above read is in SEEN; this test adds each door in more states."""
    signed_in("director")
    today = floor.today
    d = decide(release=floor.r52, response=KEEP_WITH, controls=list(rules.SUPPORT_CONTROLS)[:-1], plan=DecisionPlan.REPLACE_PLANNED,
               plan_by=today + 500 * DAY, decided_on=today - 300 * DAY, review_by=today - DAY)
    out = decide(asset=floor.d["T-1"], response=OUT_OF_USE, plan=DecisionPlan.RETURN_TO_OWNER)
    for n in (d.number, out.number):
        for url, headers in ((f"/security/decisions/{n}/", HX), (f"/security/decisions/{n}/", {}), (f"/security/decisions/{n}/print/", {}),
                             (f"/security/decisions/{n}/end/", MODAL), (f"/security/decisions/{n}/in-error/", MODAL),
                             (f"/security/decisions/new/?supersedes={n}", MODAL)):
            html(client.get(url, **headers))
        SEEN.append(csv_text(client.get(f"/security/decisions/{n}/devices.csv")))
    for url in ("/security/decisions/", "/security/decisions/?state=all", "/security/decisions/new/?asset=P-1",
                f"/security/decisions/new/?model={floor.model.pk}", "/security/decisions/new/?asset=P-4"):
        html(client.get(url, **(MODAL if "new" in url else {})))
    SEEN.append(csv_text(client.get("/security/decisions/export.csv?state=all")))
    hits = [(w, re.search(rf"(?i).{{0,80}}{re.escape(w)}.{{0,80}}", text).group(0)) for text in SEEN for w in never(text)]
    assert hits == [] and len(SEEN) > 40
