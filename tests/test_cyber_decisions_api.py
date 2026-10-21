"""
Slice 39 wave 2: security decisions over the API, read only (apps/api/views_cybersecurity.py SecurityDecisionViewSet,
serializers_cybersecurity.decision_data with rules.decisions_now; build39 section 8), and the delete refusals a decision adds to the devices' and
models' API (apps/api/views.py: AssetViewSet and DeviceModelViewSet perform_destroy; build39 section 5), on SQLite and PostgreSQL.

GET /api/v1/security-decisions/ (Device security View and Equipment View, never a scoped user): every decision, live and ended, by number,
with its scope, reasons, response, controls, plan, who accepted it, IT's exception number, the days, its state, the sentence, the devices
it governed when recorded and governs now, and its Check lines, each in the rules' words (a test holds every row to decision_devices,
decision_checks, decision_state, and decision_words); one by id or by number in any letter case; ?scope, ?state (each row as
decision_state reads it), ?device (id or tag; a retired device's own decisions); a 400 naming an unknown scope or state; a fixed number
of queries for a page as decisions and devices grow; writes a 405 at Approve and a 403 in words below it; another facility's a 404; a
token reads only its own facility; rules.DECISION_NEVER_WORDS over every body.

DELETE /api/v1/assets/<id>/ and /api/v1/device-models/<id>/: a decision (live or ended) holds its device or model; a reader of decisions
reads its number, anyone else only that the Device security register holds records; beside the work orders, devices, releases, and
profile that hold it too.
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import facts, release
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import views_cybersecurity as api
from apps.api.permissions import ModulePermission
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AppliesTo,
    Connects,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    Segmented,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.workorders.services import create_work_order

DECISIONS = "/api/v1/security-decisions/"
DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       DecisionResponse.TAKE_OUT_OF_USE)
DECISION_FIELDS = {"id", "number", "applies_to", "applies_to_label", "scope", "reasons", "reason_words", "response", "response_label",
                   "controls", "control_words", "controls_reference", "plan", "plan_label", "plan_by", "plan_words", "plan_owner",
                   "accepted_by", "accepted_by_label", "exception_reference", "recorded_by", "decided_on", "review_by", "state",
                   "state_label", "review_words", "ended_on", "end_reason", "end_reason_label", "supersedes", "superseded_by", "sentence",
                   "governed_ids", "governs", "checks"}
KEEPS = "the register keeps a model's releases and profile, with their history, even a release entered in error"
STAYS = "a security decision stays as recorded, ended or not"


def never(text: str) -> list:
    """rules.DECISION_NEVER_WORDS found in `text`, as whole words in any letter case (none may be)."""
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def ok(r):
    assert r.status_code == 200, r.content
    return r.json()


def custom_user(tenant, name, levels, scope="", **extra):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def world(ctx, dept, pump_model, vent_model, vent, make_user, today):
    """The pumps: a profile (can connect, wireless; all on a medical device segment), 5.2 (demo) past its security updates, 5.6 (demo)
    supported; P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, the rental T-0101 on 5.2 (due back in 20 days). The ventilators: the model's
    support ended, CE-10001 connected (wired). Kim (CE manager: Device security Approve) owns a plan. The decisions, through the service:
    - old: 5.2's devices, superseded by `new` (ended);
    - new: 5.2's devices, keep in use with controls (IT's reference CHG0012345), an update planned 20 days ago (owner Kim), past its review
      5 days ago (review_due; the Check "The plan's date ... has passed");
    - rental: T-0101's own, keep in use without added controls, return it to its owner, recorded by Kim, its review the day it is due
      back (review_soon);
    - vent: every Hamilton-G5 of ours, take out of use, a replacement planned (in_force; the Check "still in service");
    - p2: P-2's own, entered in error (ended)."""
    kim = make_user("manager")
    kim.first_name, kim.last_name = "Kim", "Lee"
    kim.save()
    profile = release(pump_model, connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=today + 1400 * DAY)
    pumps = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept) for tag in ("P-1", "P-2", "P-3", "P-4")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(pumps[tag], r52)
    facts(pumps["P-4"], r56)
    rental = Asset.objects.create(tag="T-0101", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                  owner="Summit Medical Rentals", due_back_on=today + 20 * DAY)
    facts(rental, r52)
    DeviceModel.objects.filter(pk=vent_model.pk).update(support_ends_on=today - 10 * DAY)
    facts(vent, None, network=Network.WIRED)
    common = {"reasons": ["security_updates"], "by": None, "today": today}
    old = decisions.record_decision(release=r52, response=KEEP_WITH, controls=["segmented"], accepted_by=AcceptedBy.CE_LEADER,
                                    decided_on=today - 300 * DAY, review_by=today - 200 * DAY, **common)
    new = decisions.record_decision(release=r52, response=KEEP_WITH, controls=["segmented", "traffic_restricted", "recorded_elsewhere"],
                                    controls_reference="CHG0012345", plan=DecisionPlan.UPDATE_PLANNED, plan_by=today - 20 * DAY,
                                    plan_owner=kim, accepted_by=AcceptedBy.IT_SECURITY, exception_reference="DEMO-EX-1037",
                                    decided_on=today - 200 * DAY, review_by=today - 5 * DAY, supersedes=old, **common)
    rental_decision = decisions.record_decision(asset=rental, response=KEEP_WITHOUT, plan=DecisionPlan.RETURN_TO_OWNER,
                                                accepted_by=AcceptedBy.CE_LEADER, reasons=["security_updates"], by=kim, today=today)
    vent_decision = decisions.record_decision(device_model=vent_model, response=OUT_OF_USE, plan=DecisionPlan.REPLACE_PLANNED,
                                              plan_by=today + 300 * DAY, accepted_by=AcceptedBy.COMMITTEE, decided_on=today - DAY, **common)
    p2 = decisions.record_decision(asset=pumps["P-2"], response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER, **common)
    decisions.mark_entered_in_error(p2, by=None, today=today)
    old.refresh_from_db()
    p2.refresh_from_db()
    return {"kim": kim, "profile": profile, "r52": r52, "r56": r56, "pumps": pumps, "rental": rental, "vent": vent, "pump_model": pump_model,
            "vent_model": vent_model, "dept": dept, "old": old, "new": new, "rental_decision": rental_decision, "vent_decision": vent_decision,
            "p2": p2}


def rows_by_number(client) -> dict:
    data = ok(client.get(DECISIONS))
    return {r["number"]: r for r in data["results"]}


# --- the rows -----------------------------------------------------------------------------------------------------------------------

def test_every_decision_reads_in_the_rules_words(client, world, today):
    client.force_login(world["kim"])
    data = ok(client.get(DECISIONS))
    numbers = [r["number"] for r in data["results"]]
    assert numbers == sorted(numbers) and len(numbers) == 5 and data["count"] == 5  # by number, live and ended
    rows = {r["number"]: r for r in data["results"]}
    assert all(set(r) == DECISION_FIELDS for r in rows.values())
    pumps, r52 = world["pumps"], world["r52"]

    new = rows[world["new"].number]
    assert new["applies_to"] == "support_ended" and new["applies_to_label"] == AppliesTo.SUPPORT_ENDED.label
    assert new["scope"] == {"kind": "release", "label": rules.SCOPE_LABELS["release"], "words": "Alaris 8015 PCU on 5.2 (demo)", "asset": None,
                            "device_model": {"id": str(world["pump_model"].pk), "manufacturer": "BD", "model": "Alaris 8015 PCU"},
                            "release": {"id": str(r52.pk), "version": "5.2 (demo)"}}
    assert new["reasons"] == ["security_updates"] and new["reason_words"] == [rules.REASONS["security_updates"]]
    assert (new["response"], new["response_label"]) == ("keep_with_controls", "Keep in use with controls")
    assert new["controls"] == ["segmented", "traffic_restricted", "recorded_elsewhere"]
    assert new["control_words"] == [rules.CONTROLS["segmented"], rules.CONTROLS["traffic_restricted"],
                                    "Controls recorded in IT's system (CHG0012345)"]
    assert new["controls_reference"] == "CHG0012345"
    plan_day = rules.day_words(today - 20 * DAY)
    assert (new["plan"], new["plan_label"], new["plan_by"]) == ("update_planned", "Update planned, not opened yet",
                                                                 (today - 20 * DAY).isoformat())
    assert new["plan_words"] == f"Plan: update by {plan_day} (owner Kim Lee)" and new["plan_owner"] == "Kim Lee"
    assert (new["accepted_by"], new["accepted_by_label"]) == ("it_security", "IT security or the CISO")
    assert new["exception_reference"] == "DEMO-EX-1037" and new["recorded_by"] is None  # the system
    assert (new["decided_on"], new["review_by"]) == ((today - 200 * DAY).isoformat(), (today - 5 * DAY).isoformat())
    assert (new["state"], new["state_label"]) == (rules.REVIEW_DUE, "Review due")
    assert new["review_words"] == f"Review due since {rules.day_words(today - 5 * DAY)}"
    assert new["sentence"] == rules.decision_words(world["new"], today) and new["sentence"].startswith("Review due since")
    assert (new["supersedes"], new["superseded_by"], new["ended_on"], new["end_reason"]) == (world["old"].number, None, None, "")
    assert new["governed_ids"] == [str(pumps[t].pk) for t in ("P-1", "P-2", "P-3")]
    assert new["governs"] == [{"id": str(pumps[t].pk), "tag": t} for t in ("P-1", "P-2", "P-3")]
    assert new["checks"] == [{"key": rules.CHECK_PLAN_PASSED, "words": f"The plan's date ({plan_day}) has passed", "info": False,
                              "action": "", "tags": []}]

    old = rows[world["old"].number]
    assert (old["state"], old["end_reason"], old["end_reason_label"]) == (rules.ENDED, "superseded", "Superseded by a new decision")
    assert old["superseded_by"] == world["new"].number and old["supersedes"] is None and old["ended_on"] == today.isoformat()
    assert old["review_words"] == f"Superseded {rules.day_words(today)}" and old["governs"] == [] and old["checks"] == []

    rental = rows[world["rental_decision"].number]
    assert rental["scope"] == {"kind": "device", "label": "One device", "words": "T-0101",
                               "asset": {"id": str(world["rental"].pk), "tag": "T-0101"}, "device_model": None, "release": None}
    assert (rental["state"], rental["review_by"]) == (rules.REVIEW_SOON, (today + 20 * DAY).isoformat())  # the day it is due back
    assert rental["plan_words"] == "Plan: return it to its owner" and rental["recorded_by"] == "Kim Lee" and rental["controls"] == []
    assert rental["sentence"].startswith(f"Keep in use without added controls: risk accepted until {rules.day_words(today + 20 * DAY)}.")
    assert rental["governs"] == [{"id": str(world["rental"].pk), "tag": "T-0101"}] and rental["checks"] == []

    vent = rows[world["vent_decision"].number]
    assert vent["scope"]["kind"] == "model" and vent["scope"]["words"] == "Every Hamilton-G5 of ours" and vent["scope"]["release"] is None
    assert vent["state"] == rules.IN_FORCE and vent["plan_words"] == rules.plan_words(world["vent_decision"])
    assert vent["plan_words"].startswith("Plan: replace, planned for FY")
    assert vent["sentence"].startswith(f"Take out of use, decided {rules.day_words(today - DAY)}; review by")
    assert [(c["key"], c["tags"]) for c in vent["checks"]] == [(rules.CHECK_IN_SERVICE, ["CE-10001"])]

    p2 = rows[world["p2"].number]
    assert (p2["state"], p2["end_reason"], p2["end_reason_label"]) == (rules.ENDED, "entered_in_error", "Entered in error")
    assert p2["review_words"] == f"Entered in error {rules.day_words(today)}" and p2["governed_ids"] == [str(pumps["P-2"].pk)]
    assert not never(str(data))


def test_each_row_reads_as_the_rules_read_one_decision(client, world, dept, today):
    """The page's batch (decisions_now) and the rules' one-decision readers agree on every row: the devices it governs now, its Check
    lines, its state and sentence, after devices join, leave, and a more specific decision takes one."""
    p5 = Asset.objects.create(tag="P-5", device_model=world["pump_model"], department=dept)
    facts(p5, world["r52"])  # joined since
    eq.set_status(world["pumps"]["P-1"], AssetStatus.RETIRED)  # left
    decisions.record_decision(asset=world["pumps"]["P-3"], response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER,
                              reasons=["security_updates"], by=None, today=today)  # a more specific decision governs P-3
    client.force_login(world["kim"])
    rows = rows_by_number(client)
    assert len(rows) == 6
    for d in DeviceSecurityDecision.objects.all():
        row = rows[d.number]
        assert [g["id"] for g in row["governs"]] == [str(dd.asset.pk) for dd in rules.decision_devices(d, today) if dd.governed], d.number
        assert [(c["key"], c["words"], c["tags"]) for c in row["checks"]] == [(c.key, c.words, list(c.tags))
                                                                             for c in rules.decision_checks(d, today)], d.number
        assert (row["state"], row["sentence"], row["review_words"]) == (rules.decision_state(d, today), rules.decision_words(d, today),
                                                                        rules.review_words(d, today))
    new = rows[world["new"].number]
    assert [g["tag"] for g in new["governs"]] == ["P-2", "P-5"]
    assert "1 device joined since: P-5" in [c["words"] for c in new["checks"]]


def test_one_decision_by_id_or_number(client, world, other_tenant):
    client.force_login(world["kim"])
    by_id = ok(client.get(f"{DECISIONS}{world['new'].pk}/"))
    assert set(by_id) == DECISION_FIELDS and by_id["number"] == world["new"].number
    assert ok(client.get(f"{DECISIONS}{world['new'].number.lower()}/")) == by_id  # any letter case
    assert ok(client.get(f"{DECISIONS}{world['old'].number}/"))["state"] == rules.ENDED  # an ended one too
    missing = client.get(f"{DECISIONS}SD-99-9999/")
    assert missing.status_code == 404 and missing.json()["detail"] == api.DECISION_NOT_FOUND
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=Department.objects.create(name="Their ICU"))
        their = DeviceSecurityDecision.objects.create(number="SD-26-0777", applies_to=AppliesTo.SUPPORT_ENDED, asset=theirs,
                                                      reasons=["security_updates"], response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER,
                                                      decided_on=timezone.localdate(), review_by=timezone.localdate() + 100 * DAY)
    assert client.get(f"{DECISIONS}{their.pk}/").status_code == 404 and client.get(f"{DECISIONS}SD-26-0777/").status_code == 404
    assert "SD-26-0777" not in str(ok(client.get(DECISIONS))) and "THEIRS-1" not in str(ok(client.get(DECISIONS)))


# --- the filters ----------------------------------------------------------------------------------------------------------------------

def test_the_filters(client, world, today):
    client.force_login(world["kim"])

    def numbers(query) -> set:
        return {r["number"] for r in ok(client.get(DECISIONS + query))["results"]}

    w = {k: world[k].number for k in ("old", "new", "rental_decision", "vent_decision", "p2")}
    assert numbers("?scope=device") == {w["rental_decision"], w["p2"]}
    assert numbers("?scope=release") == {w["old"], w["new"]} and numbers("?scope=model") == {w["vent_decision"]}
    every = list(DeviceSecurityDecision.objects.all())
    for state in rules.DECISION_STATES:  # each row as decision_state reads it, on the facility's today
        expected = {d.number for d in every if rules.decision_state(d, today) == state}
        assert expected and numbers(f"?state={state}") == expected, state
    assert numbers("?scope=release&state=ended") == {w["old"]}
    # a device: its own decisions, and a release's or a model's whose scope holds it now (ours, on site)
    assert numbers("?device=P-1") == {w["old"], w["new"]}
    assert numbers("?device=p-2") == {w["old"], w["new"], w["p2"]}  # any letter case
    assert numbers(f"?device={world['rental'].pk}") == {w["rental_decision"]}  # a rental: only its own (never a release's)
    assert numbers("?device=CE-10001") == {w["vent_decision"]} and numbers("?device=P-4") == set() and numbers("?device=NOPE") == set()
    assert numbers("?device=P-2&state=ended") == {w["old"], w["p2"]}
    eq.set_status(world["pumps"]["P-2"], AssetStatus.RETIRED)
    assert numbers("?device=P-2") == {w["p2"]}  # retired: its own decisions still, no longer in the release's scope
    r = client.get(f"{DECISIONS}?scope=fleet&state=overdue&device=P-1")
    assert r.status_code == 400 and r.json() == {"scope": [api.SCOPE_WORDS], "state": [api.DECISION_STATE_WORDS]}
    assert api.SCOPE_WORDS == "One of device, release, model." and api.DECISION_STATE_WORDS == "One of in_force, review_soon, review_due, ended."


# --- who reads it, and nothing is written ---------------------------------------------------------------------------------------------

def test_the_levels(client, make_user, tenant, world):
    detail = f"{DECISIONS}{world['new'].pk}/"
    for user in (make_user("analyst"),  # Device security None
                 custom_user(tenant, "models", {Module.CYBERSECURITY: Level.FULL})):  # no Equipment View: a decision can name a device
        client.force_login(user)
        for url in (DECISIONS, detail):
            r = client.get(url)
            assert r.status_code == 403 and r.json()["detail"] == api.DECISIONS_REFUSAL, (user.username, url)
    for user in (custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}), make_user("technician"),
                 make_user("director"), world["kim"]):
        client.force_login(user)
        assert client.get(DECISIONS).status_code == 200 and client.get(detail).status_code == 200, user.username


def test_scoped_users_are_refused_every_door(client, make_user, tenant, world):
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save()
    requester = make_user("requester")
    requester.department = "ICU"
    requester.save()
    company_full = custom_user(tenant, "companyfull", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="BD")
    unit_full = custom_user(tenant, "unitfull", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    detail = f"{DECISIONS}{world['rental_decision'].number}/"
    for user in (vendor, requester, company_full, unit_full):
        client.force_login(user)
        for url in (DECISIONS, detail, f"{DECISIONS}?device=T-0101"):
            r = client.get(url)
            assert r.status_code == 403, (user.username, url)
            if user in (company_full, unit_full):
                assert r.json()["detail"] == ModulePermission.SCOPED_REFUSAL
        for method, url in (("post", DECISIONS), ("patch", detail), ("delete", detail)):
            assert getattr(client, method)(url, {}, content_type="application/json").status_code == 403


def test_every_write_is_refused(client, make_user, tenant, world):
    detail = f"{DECISIONS}{world['new'].pk}/"
    writes = [("post", DECISIONS), ("put", detail), ("patch", detail), ("delete", detail)]
    before = sorted(DeviceSecurityDecision.objects.values_list("number", "ended_on", "review_by"))
    for user in (world["kim"], make_user("director")):  # Approve and Full: still no write here
        client.force_login(user)
        for method, url in writes:
            r = getattr(client, method)(url, {"review_by": "2030-01-01", "end_reason": "no_longer_needed"}, content_type="application/json")
            assert r.status_code == 405 and r.json()["detail"] == api.DECISIONS_READ_ONLY, (method, url)
    for user in (make_user("technician"),  # Edit: a write is Approve's
                 custom_user(tenant, "itread", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}),
                 custom_user(tenant, "noequip", {Module.CYBERSECURITY: Level.APPROVE})):  # Approve with no Equipment View
        client.force_login(user)
        for method, url in writes:
            r = getattr(client, method)(url, {}, content_type="application/json")
            assert r.status_code == 403 and r.json()["detail"] == api.DECISIONS_WRITE_REFUSAL, (user.username, method, url)
    assert sorted(DeviceSecurityDecision.objects.values_list("number", "ended_on", "review_by")) == before
    assert "Decisions tab" in api.DECISIONS_READ_ONLY and "Device security Approve" in api.DECISIONS_WRITE_REFUSAL


# --- fixed queries ---------------------------------------------------------------------------------------------------------------------

def test_a_page_and_one_decision_read_a_fixed_number_of_queries(client, world, dept, vent_model, today):
    client.force_login(world["kim"])

    def counted(url):
        with CaptureQueriesContext(connection) as q:
            ok(client.get(url))
        return len(q.captured_queries)

    urls = (DECISIONS, f"{DECISIONS}?state=review_due", f"{DECISIONS}?device=P-2", f"{DECISIONS}{world['new'].number}/",
            f"{DECISIONS}{world['vent_decision'].number}/")
    before = [counted(u) for u in urls]
    for i in range(4):
        model = DeviceModel.objects.create(manufacturer="Demo", model=f"Bed {i}", description="Bed", category="Beds",
                                           support_ends_on=today - 30 * DAY)
        for n in range(3):
            facts(Asset.objects.create(tag=f"B{i}-{n}", device_model=model, department=dept), None, network=Network.WIRELESS)
        # a replacement planned (in force) or waiting for the manufacturer (past review): the fiscal year is read once a page, and only
        # for a page that plans a replacement (the review_due page never does, before or after)
        replacing = bool(i % 2)
        decisions.record_decision(device_model=model, response=KEEP_WITH, controls=["traffic_restricted"],
                                  plan=DecisionPlan.REPLACE_PLANNED if replacing else DecisionPlan.WAIT_FOR_MANUFACTURER,
                                  plan_by=today + 400 * DAY if replacing else None, plan_owner=world["kim"], accepted_by=AcceptedBy.COMMITTEE,
                                  reasons=["security_updates"], decided_on=today - 100 * DAY,
                                  review_by=today + 100 * DAY if replacing else today - DAY, by=None, today=today)
        p = Asset.objects.create(tag=f"PX-{i}", device_model=world["pump_model"], department=dept)
        facts(p, world["r52"])  # joined since
    decisions.record_decision(asset=world["pumps"]["P-2"], response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER,
                              reasons=["security_updates"], by=None, today=today)
    assert ok(client.get(DECISIONS))["count"] == 10
    assert [counted(u) for u in urls] == before


# --- never words, the browsable API, tokens -----------------------------------------------------------------------------------------

def test_the_never_words_on_every_body(client, make_user, tenant, world):
    client.force_login(world["kim"])
    bodies = [client.get(url).content.decode() for url in (
        DECISIONS, *(f"{DECISIONS}?state={s}" for s in rules.DECISION_STATES), f"{DECISIONS}?scope=model", f"{DECISIONS}?device=P-2",
        *(f"{DECISIONS}{world[k].number}/" for k in ("old", "new", "rental_decision", "vent_decision", "p2")),
        f"{DECISIONS}SD-99-9999/", f"{DECISIONS}?scope=x&state=y")]
    bodies += [getattr(client, m)(DECISIONS, {}, content_type="application/json").content.decode() for m in ("post", "delete")]
    client.force_login(make_user("analyst"))
    bodies += [client.get(DECISIONS).content.decode(), client.post(DECISIONS, {}, content_type="application/json").content.decode()]
    client.force_login(make_user("technician"))
    bodies.append(client.delete(f"{DECISIONS}{world['new'].pk}/").content.decode())
    assert [never(b) for b in bodies] == [[]] * len(bodies)
    words = (api.DECISIONS_REFUSAL, api.DECISIONS_READ_ONLY, api.DECISIONS_WRITE_REFUSAL, api.DECISION_NOT_FOUND, api.SCOPE_WORDS,
             api.DECISION_STATE_WORDS)
    assert never(" ".join(words)) == []


def test_the_browsable_api_renders(client, world):
    client.force_login(world["kim"])
    for url in (DECISIONS, f"{DECISIONS}{world['new'].number}/"):
        r = client.get(url, HTTP_ACCEPT="text/html")
        assert r.status_code == 200 and world["new"].number.encode() in r.content and b"<html" in r.content, url


def _theirs(other_tenant):
    """Another facility's pump past its security updates with a device-scope decision numbered as one of ours may be."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        today = timezone.localdate()
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        release(model, connects=Connects.WIRELESS)
        rel = release(model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
        asset = Asset.objects.create(tag="P-1", device_model=model, department=Department.objects.create(name="Their ICU"))
        facts(asset, rel)
        decision = decisions.record_decision(asset=asset, response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER,
                                             reasons=["security_updates"], exception_reference="THEIR-EX-1", by=None, today=today)
        role = Role.objects.get(slug="director")
    them = User.objects.create_user(username="director@other.example", password="Test-Pass-2026-x", tenant=other_tenant, role=role)
    return decision, {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=them).key}"}


def test_a_token_reads_only_its_own_facility(client, world, other_tenant):
    decision, auth = _theirs(other_tenant)
    rows = client.get(DECISIONS, **auth).json()["results"]
    assert [(r["number"], r["scope"]["words"], r["exception_reference"]) for r in rows] == [(decision.number, "P-1", "THEIR-EX-1")]
    assert client.get(f"{DECISIONS}{world['new'].pk}/", **auth).status_code == 404
    assert client.get(f"{DECISIONS}?device=P-2", **auth).json()["results"] == []  # our P-2: not theirs
    mine = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=world['kim']).key}"}
    assert client.get(f"{DECISIONS}{decision.pk}/", **mine).status_code == 404
    assert "THEIR-EX-1" not in client.get(DECISIONS, **mine).content.decode()


@needs_postgres
def test_the_api_under_the_policies_by_token(client, world, other_tenant, today):
    decision, _ = _theirs(other_tenant)
    auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=world['kim']).key}"}
    as_app_role()
    rows = client.get(DECISIONS, **auth).json()["results"]
    assert {r["number"] for r in rows} == {world[k].number for k in ("old", "new", "rental_decision", "vent_decision", "p2")}
    new = next(r for r in rows if r["number"] == world["new"].number)
    assert [g["tag"] for g in new["governs"]] == ["P-1", "P-2", "P-3"] and new["checks"][0]["key"] == rules.CHECK_PLAN_PASSED
    assert client.get(f"{DECISIONS}{world['new'].number}/", **auth).json()["sentence"] == rules.decision_words(world["new"], today)
    assert {r["number"] for r in client.get(f"{DECISIONS}?device=p-2&state=ended", **auth).json()["results"]} == {
        world["old"].number, world["p2"].number}
    assert client.get(f"{DECISIONS}{decision.pk}/", **auth).status_code == 404


# --- the delete refusals (build39 section 5) -----------------------------------------------------------------------------------------

def _delete(client, what):
    url = f"/api/v1/assets/{what.pk}/" if isinstance(what, Asset) else f"/api/v1/device-models/{what.pk}/"
    r = client.delete(url)
    assert r.status_code == 400, r.content
    detail = r.json()["detail"]
    assert never(detail) == []
    return detail


@pytest.fixture
def outsider(tenant):
    """Equipment Full with no Device security level: deletes devices and models, never reads the register."""
    return custom_user(tenant, "equipment-only", {Module.EQUIPMENT: Level.FULL})


@pytest.fixture(params=["director", "equipment-and-register"])
def reader(request, make_user, tenant):
    """A reader of decisions who may delete: the director, or Equipment Full with Device security View."""
    if request.param == "director":
        return make_user("director")
    return custom_user(tenant, "equipment-and-register", {Module.EQUIPMENT: Level.FULL, Module.CYBERSECURITY: Level.VIEW})


def test_a_device_with_a_decision_is_never_deleted(client, world, reader, outsider):
    rental, number = world["rental"], world["rental_decision"].number
    client.force_login(reader)
    assert _delete(client, rental) == f"T-0101 has a security decision ({number}), so it cannot be deleted; retire it instead."
    # an ended decision holds its device too (P-2's, entered in error)
    assert _delete(client, world["pumps"]["P-2"]) == (f"P-2 has a security decision ({world['p2'].number}), so it cannot be deleted; "
                                                      "retire it instead.")
    client.force_login(outsider)
    for asset in (rental, world["pumps"]["P-2"]):
        detail = _delete(client, asset)
        assert detail == f"{asset.tag} has records in the Device security register, so it cannot be deleted; retire it instead."
        assert "SD-" not in detail
    assert Asset.objects.filter(pk__in=[rental.pk, world["pumps"]["P-2"].pk]).count() == 2


def test_a_device_with_work_orders_and_a_decision(client, world, reader, outsider):
    p2 = world["pumps"]["P-2"]
    create_work_order(asset=p2, type="repair", priority="normal", problem="Door latch loose")
    client.force_login(reader)
    assert _delete(client, p2) == (f"P-2 has work orders or service requests, and a security decision ({world['p2'].number}), so it cannot "
                                   "be deleted; retire it instead.")
    client.force_login(outsider)
    assert _delete(client, p2) == ("P-2 has work orders or service requests, and records in the Device security register, so it cannot be "
                                   "deleted; retire it instead.")
    # work orders alone keep their words
    p4 = world["pumps"]["P-4"]
    create_work_order(asset=p4, type="repair", priority="normal", problem="Keypad")
    assert _delete(client, p4) == "P-4 has work orders or service requests, so it cannot be deleted; retire it instead."


def test_a_model_with_a_decision_and_no_release(client, world, dept, reader, outsider, today):
    """A model whose devices were deleted after its decision (nothing else holds it): only the decision does."""
    model = DeviceModel.objects.create(manufacturer="Demo", model="Bed 1", description="Bed", category="Beds", support_ends_on=today - 30 * DAY)
    bed = Asset.objects.create(tag="B-1", device_model=model, department=dept)
    facts(bed, None, network=Network.WIRELESS)
    decision = decisions.record_decision(device_model=model, response=KEEP_WITHOUT, accepted_by=AcceptedBy.COMMITTEE,
                                         reasons=["security_updates"], by=None, today=today)
    client.force_login(reader)
    assert client.delete(f"/api/v1/assets/{bed.pk}/").status_code == 204  # the model's decision never holds a device
    assert _delete(client, model) == f"Demo Bed 1 has a security decision ({decision.number}), so it cannot be deleted: {STAYS}."
    client.force_login(outsider)
    assert _delete(client, model) == "Demo Bed 1 has records in the Device security register, so it cannot be deleted."
    assert DeviceModel.objects.filter(pk=model.pk).exists()


def test_a_model_with_devices_releases_and_decisions(client, world, reader, outsider):
    client.force_login(reader)
    numbers = sorted((world["old"].number, world["new"].number))
    assert _delete(client, world["pump_model"]) == (
        f"BD Alaris 8015 PCU has devices, and software releases and a profile in the Device security register, and security decisions "
        f"({', '.join(numbers)}), so it cannot be deleted: {KEEPS}; {STAYS}.")
    assert _delete(client, world["vent_model"]) == (
        f"Hamilton Medical Hamilton-G5 has devices, and a security decision ({world['vent_decision'].number}), so it cannot be deleted: "
        f"{STAYS}.")
    client.force_login(outsider)
    for model in (world["pump_model"], world["vent_model"]):
        assert _delete(client, model) == f"{model} has devices, and records in the Device security register, so it cannot be deleted."
    assert DeviceModel.objects.filter(pk__in=[world["pump_model"].pk, world["vent_model"].pk]).count() == 2


@needs_postgres
def test_the_delete_refusals_under_the_policies(client, world, make_user):
    client.force_login(make_user("director"))
    as_app_role()
    assert _delete(client, world["rental"]) == (f"T-0101 has a security decision ({world['rental_decision'].number}), so it cannot be "
                                                "deleted; retire it instead.")
    assert _delete(client, world["vent_model"]).endswith(f"so it cannot be deleted: {STAYS}.")
