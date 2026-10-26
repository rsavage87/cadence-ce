"""
Slice 40 wave 2C: security advisories over the API, read only (apps/api/views_cybersecurity.py SecurityAdvisoryViewSet,
serializers_cybersecurity.advisory_data with rules.advisory_counts; build40 section 8), advisory decisions on GET security-decisions/, and
the model delete an advisory holds (apps/api/views.py DeviceModelViewSet.perform_destroy; critique40_locks S1), on SQLite and PostgreSQL.

GET /api/v1/security-advisories/ (Device security View, never a scoped user): every advisory not entered in error (?removed=1 those
too), newest received first, with its name, status and words, sources, the models it names with their counts, every word the rules';
the counts split by reader (line kinds and update counts at View; answer kinds, decisions past review, devices named since, and the
decision-derived status words only with Equipment View too: critique40_rules S2); one by id or number in any letter case, with its device
lines for a reader of devices (null for anyone else); ?status, ?model, ?cve, ?reference, a 400 naming a bad one; a fixed number of
queries for a page and for one as advisories, rows, devices, and decisions grow; writes a 405 at Edit and a 403 in words below it;
another facility's a 404; a token reads only its own facility; rules.DECISION_NEVER_WORDS over every body and the browsable API's
description.

GET /api/v1/security-decisions/: advisory {id, number} or null; ?applies_to, ?advisory; every advisory decision's row reads as the rules'
one-decision readers read it (decisions_now keyed); fixed queries as advisory decisions and advisories grow.

DELETE /api/v1/device-models/<id>/: an advisory naming the model holds it (its number for a reader of advisories); the row locked FOR
UPDATE first; on PostgreSQL, Set models against the delete in both orders answers in words, never a 500.
"""
import re
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import views as api_views
from apps.api import views_cybersecurity as api
from apps.api.permissions import ModulePermission
from apps.cybersecurity import advisories, decisions, rules, services
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryModel,
    AdvisorySource,
    AppliesTo,
    Connects,
    DecisionResponse,
    DeviceSecurityDecision,
    SecurityAdvisory,
    Segmented,
)
from apps.equipment.models import Asset, Department, DeviceModel, Ownership, RiskClass
from apps.tenants.context import tenant_context

ADVISORIES = "/api/v1/security-advisories/"
DECISIONS = "/api/v1/security-decisions/"
DAY = timedelta(days=1)
KEEP_WITHOUT, NOT_IN_USE = DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, DecisionResponse.NOT_IN_USE_HERE
ADVISORY_FIELDS = {"id", "number", "name", "status", "status_label", "status_words", "received_on", "recorded_by", "cves",
                   "models_confirmed_on", "models_confirmed_by", "status_on", "status_by", "not_applicable_reason",
                   "not_applicable_reason_label", "changed_on", "removed_on", "removed_by", "sources", "models", "counts", "url"}
SOURCE_FIELDS = {"id", "source", "source_label", "reference", "url", "published_on", "updated_on", "revision", "last_change",
                 "last_change_words"}
MODEL_FIELDS = {"id", "device_model", "versions", "versions_label", "releases", "remedy", "remedy_label", "fix_release",
                "patches_reference", "counts"}
LINE_FIELDS = {"model_row", "asset", "serial", "department", "ownership", "ownership_label", "connection", "release", "line", "line_label",
               "words", "needs_response", "answer", "answer_label", "responses", "decision", "covers", "url"}
KEEPS = "the register keeps a model's releases and profile, with their history, even a release entered in error"
STAYS = "a security decision stays as recorded, ended or not"
NAMES = "an advisory keeps the models it names as recorded, even one entered in error"


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


def counts_of(found: list) -> dict:
    """[{kind, label, devices}] as {kind: devices}."""
    return {c["kind"]: c["devices"] for c in found}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def world(ctx, dept, pump_model, vent_model, vent, make_user, today):
    """The pumps (a profile: can connect, wireless; all on a medical device segment): 5.2 (demo) past its security updates, 5.4 (demo),
    5.6 (demo); P-1, P-2 of ours on 5.2, P-3 on 5.4, P-4 on 5.6, P-5 with no release recorded; the rental T-1 on 5.2. Through the writers:
    - a1, received 3 days ago (two CVE IDs; a manufacturer bulletin with a link and an "other" source): the pumps, 5.2 listed, the fix
      5.6, models confirmed; P-1's update open to the fix; P-2 under a device decision "not in use here" (in force);
    - a2, received 7 days ago: marked not applicable by Kim (it names no model here);
    - a3, received 10 days ago (a CVE ID a1 has too, seen): the ventilators, releases not stated, mitigations; CE-10001 under a device
      decision; closed by Kim;
    - a4, received yesterday: entered in error;
    - a5, received today: the pumps, 5.4 listed, the manufacturer's patches DEMO-PATCH-2026-10, models to confirm.
    And a support decision for 5.2's pumps (slice 39's key, beside the advisory decisions)."""
    kim = make_user("manager")
    kim.first_name, kim.last_name = "Kim", "Lee"
    kim.save()
    release(pump_model, connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    r54, r56 = release(pump_model, "5.4 (demo)"), release(pump_model, "5.6 (demo)")
    d = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept, serial=f"SN-{tag}")
         for tag in ("P-1", "P-2", "P-3", "P-4", "P-5")}
    for tag, rel in (("P-1", r52), ("P-2", r52), ("P-3", r54), ("P-4", r56)):
        facts(d[tag], rel)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=today + 20 * DAY)
    facts(d["T-1"], r52)
    common = {"by": None, "today": today}
    a1 = advisories.record_advisory(received_on=today - 3 * DAY, cves="CVE-2026-1111, cve-2026-2222", source="manufacturer_bulletin",
                                    reference="DEMO-ADV-0001", url="https://example.com/advisories/demo-adv-0001",
                                    published_on=today - 5 * DAY, **common)
    advisories.add_source(a1, source="other", reference="DEMO-ADV-0001-N", **common)
    advisories.set_model(a1, device_model=pump_model, versions="listed", release_ids=[r52.pk], remedy="release", fix_release=r56.pk,
                         confirm=True, **common)
    wo, _ = update_wo(d["P-1"], r56)
    p2 = decisions.record_decision(asset=d["P-2"], applies_to=AppliesTo.ADVISORY, advisory=a1, response=NOT_IN_USE,
                                   accepted_by=AcceptedBy.CE_LEADER, **common)
    a2 = advisories.record_advisory(received_on=today - 7 * DAY, cves="", source="other", reference="DEMO-ADV-0002", **common)
    advisories.mark_not_applicable(a2, reason="no_model_here", by=kim, today=today)
    a3 = advisories.record_advisory(received_on=today - 10 * DAY, cves="CVE-2026-1111", seen=[a1.number], source="manufacturer_portal",
                                    reference="DEMO-ADV-0003", **common)
    advisories.set_model(a3, device_model=vent_model, versions="not_stated", remedy="mitigations", confirm=True, **common)
    vent_decision = decisions.record_decision(asset=vent, applies_to=AppliesTo.ADVISORY, advisory=a3, response=KEEP_WITHOUT,
                                              accepted_by=AcceptedBy.CE_LEADER, **common)
    advisories.close(a3, by=kim, today=today)
    a4 = advisories.record_advisory(received_on=today - DAY, cves="", source="isac", reference="DEMO-ADV-0004", **common)
    advisories.mark_entered_in_error(a4, by=kim, today=today)
    a5 = advisories.record_advisory(received_on=today, cves="", source="manufacturer_bulletin", reference="DEMO-ADV-0005", **common)
    advisories.set_model(a5, device_model=pump_model, versions="listed", release_ids=[r54.pk], remedy="patches",
                         patches_reference="DEMO-PATCH-2026-10", **common)
    support = decisions.record_decision(release=r52, reasons=["security_updates"], response=KEEP_WITHOUT, accepted_by=AcceptedBy.IT_SECURITY,
                                        **common)
    for a in (a1, a2, a3, a4, a5):
        a.refresh_from_db()
    return SimpleNamespace(kim=kim, r52=r52, r54=r54, r56=r56, d=d, wo=wo, p2=p2, vent=vent, vent_decision=vent_decision, support=support,
                           a1=a1, a2=a2, a3=a3, a4=a4, a5=a5, model=pump_model, vent_model=vent_model, dept=dept)


@pytest.fixture
def readers(tenant):
    """Device security View alone (IT's read-only reader: models and releases); View with Equipment View (a reader of devices)."""
    return SimpleNamespace(view_only=custom_user(tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW}),
                           devices=custom_user(tenant, "devices", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}))


def rows_by_number(client, query="") -> dict:
    return {r["number"]: r for r in ok(client.get(ADVISORIES + query))["results"]}


# --- the rows -----------------------------------------------------------------------------------------------------------------------

def test_every_advisory_reads_in_the_rules_words(client, world, readers, today):
    client.force_login(readers.devices)
    data = ok(client.get(ADVISORIES))
    numbers = [r["number"] for r in data["results"]]
    w = world
    assert numbers == [w.a5.number, w.a1.number, w.a2.number, w.a3.number] and data["count"] == 4  # newest received first; a4 left out
    rows = {r["number"]: r for r in data["results"]}
    assert all(set(r) == ADVISORY_FIELDS for r in rows.values())
    assert all(set(s) == SOURCE_FIELDS for r in rows.values() for s in r["sources"])
    assert all(set(m) == MODEL_FIELDS for r in rows.values() for m in r["models"])
    expected = rules.advisory_counts([w.a1, w.a2, w.a3, w.a5], today=today, devices_visible=True)

    a1 = rows[w.a1.number]
    assert a1["name"] == f"{w.a1.number} · DEMO-ADV-0001 · Alaris 8015 PCU" == rules.advisory_name(w.a1)
    assert (a1["status"], a1["status_label"]) == ("open", "Open")
    assert a1["status_words"] == "Open: 2 devices with no response recorded" == expected[w.a1.pk].words
    assert a1["cves"] == ["CVE-2026-1111", "CVE-2026-2222"] and a1["received_on"] == (today - 3 * DAY).isoformat()
    assert (a1["recorded_by"], a1["models_confirmed_on"], a1["status_on"], a1["status_by"]) == (None, today.isoformat(), None, None)
    assert (a1["not_applicable_reason"], a1["not_applicable_reason_label"], a1["removed_on"]) == ("", "", None)
    assert [(s["source"], s["source_label"], s["reference"]) for s in a1["sources"]] == [
        ("manufacturer_bulletin", "Manufacturer bulletin", "DEMO-ADV-0001"), ("other", "Other", "DEMO-ADV-0001-N")]
    assert a1["sources"][0]["url"] == "https://example.com/advisories/demo-adv-0001"
    assert a1["sources"][0]["published_on"] == (today - 5 * DAY).isoformat() and a1["sources"][0]["last_change"] == []
    (row,) = a1["models"]
    assert row["device_model"]["model"] == "Alaris 8015 PCU" and row["device_model"]["id"] == str(w.model.pk)
    assert (row["versions"], row["versions_label"], row["remedy"], row["remedy_label"]) == (
        "listed", "The releases listed", "release", "A release from the manufacturer")
    assert row["releases"] == [{"id": str(w.r52.pk), "version": "5.2 (demo)"}]
    assert row["fix_release"] == {"id": str(w.r56.pk), "version": "5.6 (demo)"} and row["patches_reference"] == ""
    assert counts_of(row["counts"]["lines"]) == {"named": 3, "not_recorded": 1, "on_fix": 1, "other": 1}
    assert [c["label"] for c in row["counts"]["lines"]] == list(rules.LINE_KINDS.values())
    assert counts_of(row["counts"]["updates"]) == {"update_open": 1, "update_not_confirmed": 0}
    assert counts_of(row["counts"]["answers"]) == {"update_open": 1, "update_not_confirmed": 0, "patches_recorded": 0,
                                                   "patches_other_list": 0, "decided": 1, "review_due": 0, "none": 2}
    assert [c["label"] for c in row["counts"]["answers"]] == list(rules.ANSWER_KINDS.values())
    assert (a1["counts"]["reviews_due"], a1["counts"]["named_since"]) == (0, 0)
    assert counts_of(a1["counts"]["lines"]) == counts_of(row["counts"]["lines"])
    assert a1["url"].startswith(f"/security/advisories/{w.a1.number}/")

    a2 = rows[w.a2.number]
    assert (a2["status"], a2["not_applicable_reason"]) == ("not_applicable", "no_model_here")
    assert a2["not_applicable_reason_label"] == "It names no model in this facility's catalog"
    assert a2["status_words"] == (f"Not applicable here (Kim Lee, {rules.day_words(today)}): It names no model in this facility's "
                                  "catalog")
    assert (a2["status_by"], a2["status_on"], a2["models"]) == ("Kim Lee", today.isoformat(), [])
    assert a2["name"] == f"{w.a2.number} · DEMO-ADV-0002 · {rules.NO_MODEL_NAMED}"

    a3 = rows[w.a3.number]
    assert a3["status_words"] == f"Closed (Kim Lee, {rules.day_words(today)})" and a3["cves"] == ["CVE-2026-1111"]
    assert counts_of(a3["models"][0]["counts"]["answers"])["decided"] == 1 and a3["models"][0]["fix_release"] is None

    a5 = rows[w.a5.number]
    assert a5["status_words"] == rules.MODELS_TO_CONFIRM_WORDS and a5["models_confirmed_on"] is None
    assert (a5["models"][0]["remedy"], a5["models"][0]["patches_reference"]) == ("patches", "DEMO-PATCH-2026-10")
    for a in (w.a1, w.a2, w.a3, w.a5):  # every row's words and counts, as rules.advisory_counts reads them for a reader of devices
        r, c = rows[a.number], expected[a.pk]
        assert r["status_words"] == c.words and counts_of(r["counts"]["answers"]) == c.answers
        assert counts_of(r["counts"]["lines"]) == c.lines and counts_of(r["counts"]["updates"]) == c.updates
    assert not never(str(data))


def test_a_view_only_reader_reads_nothing_derived_from_a_decision(client, world, readers):
    """Device security View alone: the line kinds and update counts, the stored statuses; never an answer count, a decision's number or
    sentence, a device's tag, or a status word derived from a decision (critique40_rules S2)."""
    client.force_login(readers.view_only)
    rows = rows_by_number(client)
    w = world
    assert rows[w.a1.number]["status_words"] == rules.OPEN_WORDS
    assert rows[w.a3.number]["status_words"].startswith("Closed (Kim Lee, ")
    for r in rows.values():
        assert r["counts"]["answers"] is None and r["counts"]["reviews_due"] is None and r["counts"]["named_since"] is None
        assert all(m["counts"]["answers"] is None for m in r["models"])
    assert counts_of(rows[w.a1.number]["counts"]["lines"]) == {"named": 3, "not_recorded": 1, "on_fix": 1, "other": 1}
    assert counts_of(rows[w.a1.number]["counts"]["updates"]) == {"update_open": 1, "update_not_confirmed": 0}
    one = ok(client.get(f"{ADVISORIES}{w.a1.number}/"))
    assert one["devices"] is None
    body = client.get(f"{ADVISORIES}{w.a1.number}/").content.decode() + client.get(ADVISORIES).content.decode()
    for hidden in (w.p2.number, w.vent_decision.number, "P-1", "T-1", "CE-10001", w.wo.number, "not in use here", "Decided"):
        assert hidden not in body, hidden


def test_one_advisory_with_its_device_lines(client, world, readers, today):
    client.force_login(readers.devices)
    w = world
    one = ok(client.get(f"{ADVISORIES}{w.a1.pk}/"))
    assert set(one) == ADVISORY_FIELDS | {"devices"} and one["number"] == w.a1.number
    assert ok(client.get(f"{ADVISORIES}{w.a1.number.lower()}/")) == one  # any letter case
    assert all(set(line) == LINE_FIELDS for line in one["devices"])
    reading = rules.advisory_reading([w.a1], today)
    assert [(ln["asset"]["tag"], ln["line"], ln["words"]) for ln in one["devices"]] == [
        (ln.asset.tag, ln.kind, ln.words) for ln in reading.lines]
    lines = {ln["asset"]["tag"]: ln for ln in one["devices"]}
    assert (lines["P-1"]["line"], lines["P-1"]["line_label"], lines["P-1"]["answer"], lines["P-1"]["answer_label"]) == (
        "named", "On a release it names", "update_open", "Update open")
    assert lines["P-1"]["responses"] == [{"kind": "update_open", "words": f"Update open to 5.6 (demo), the fix: {w.wo.number}",
                                          "number": w.wo.number}]
    assert lines["P-1"]["words"] == "On 5.2 (demo), a release the advisory names" and lines["P-1"]["serial"] == "SN-P-1"
    assert lines["P-1"]["release"] == {"id": str(w.r52.pk), "version": "5.2 (demo)"} and lines["P-1"]["department"] == "ICU"
    assert lines["P-1"]["connection"]["words"] == "Can connect (wireless), as disclosed"
    assert lines["P-1"]["url"].startswith("/equipment/P-1/?tab=security")
    p2 = lines["P-2"]
    assert (p2["answer"], p2["decision"], p2["covers"]) == ("decided", {"id": str(w.p2.pk), "number": w.p2.number}, True)
    assert p2["responses"] == [{"kind": "decided", "words": rules.decision_words(w.p2, today), "number": w.p2.number}]
    assert (lines["P-3"]["line"], lines["P-3"]["needs_response"], lines["P-3"]["answer"], lines["P-3"]["responses"]) == (
        "other", False, "", [])
    assert (lines["P-4"]["line"], lines["P-4"]["words"]) == ("on_fix", "On 5.6 (demo), the release the advisory names as the fix")
    assert (lines["P-5"]["line"], lines["P-5"]["release"], lines["P-5"]["answer"]) == ("not_recorded", None, "none")
    assert lines["P-5"]["responses"] == [{"kind": "none", "words": rules.NO_RESPONSE_WORDS, "number": None}]
    assert (lines["T-1"]["ownership"], lines["T-1"]["ownership_label"], lines["T-1"]["answer"]) == ("rental", "Rental", "none")
    # not applicable: its lines read with no answer; entered in error: none; closed: its device and decision
    assert ok(client.get(f"{ADVISORIES}{w.a2.number}/"))["devices"] == []
    removed = ok(client.get(f"{ADVISORIES}{w.a4.number}/"))
    assert (removed["status_words"], removed["counts"], removed["devices"], removed["removed_by"]) == (
        rules.ENTERED_IN_ERROR_WORDS, None, [], "Kim Lee")
    closed = ok(client.get(f"{ADVISORIES}{w.a3.number}/"))
    assert [(ln["asset"]["tag"], ln["answer"], ln["decision"]["number"]) for ln in closed["devices"]] == [
        ("CE-10001", "decided", w.vent_decision.number)]
    assert not never(str(one) + str(closed))


def test_another_facilitys_advisory_is_a_404(client, world, readers, other_tenant):
    client.force_login(readers.devices)
    missing = client.get(f"{ADVISORIES}AD-99-9999/")
    assert missing.status_code == 404 and missing.json()["detail"] == api.ADVISORY_NOT_FOUND
    with tenant_context(other_tenant):
        theirs = make_advisory(number="AD-26-0777", reference="THEIR-ADV-1", cves=["CVE-2026-1111"])
    assert client.get(f"{ADVISORIES}{theirs.pk}/").status_code == 404 and client.get(f"{ADVISORIES}AD-26-0777/").status_code == 404
    body = client.get(ADVISORIES + "?cve=CVE-2026-1111").content.decode()
    assert "AD-26-0777" not in body and "THEIR-ADV-1" not in body


# --- the filters ----------------------------------------------------------------------------------------------------------------------

def test_the_filters(client, world, readers, vent_model):
    client.force_login(readers.view_only)
    w = world

    def numbers(query) -> set:
        return {r["number"] for r in ok(client.get(ADVISORIES + query))["results"]}

    assert numbers("?status=open") == {w.a1.number, w.a5.number} and numbers("?status=closed") == {w.a3.number}
    assert numbers("?status=not_applicable") == {w.a2.number}
    assert numbers(f"?model={w.model.pk}") == {w.a1.number, w.a5.number} and numbers(f"?model={vent_model.pk}") == {w.a3.number}
    assert numbers("?cve=cve-2026-1111") == {w.a1.number, w.a3.number} and numbers("?cve=CVE-2026-2222") == {w.a1.number}
    assert numbers("?cve=CVE-2026-9999") == set()
    assert numbers("?reference=demo-adv-0001-n") == {w.a1.number} and numbers("?reference=DEMO-ADV-0003") == {w.a3.number}
    assert numbers("?reference=DEMO-ADV") == set()  # exact, never a part
    assert numbers("?removed=1") == {w.a1.number, w.a2.number, w.a3.number, w.a4.number, w.a5.number}
    assert numbers("?removed=1&reference=demo-adv-0004") == {w.a4.number} and numbers("?reference=demo-adv-0004") == set()
    assert numbers(f"?status=open&model={w.model.pk}&cve=CVE-2026-1111") == {w.a1.number}
    # a removed source never matches
    AdvisorySource.objects.filter(reference="DEMO-ADV-0001-N").update(removed_on=timezone.localdate())
    assert numbers("?reference=demo-adv-0001-n") == set()
    r = client.get(f"{ADVISORIES}?status=resolved&model=nope&cve=(Critical)&removed=yes")
    assert r.status_code == 400 and r.json() == {"status": [api.ADVISORY_STATUS_WORDS], "model": ["A device model's id."],
                                                 "cve": [api.CVE_FILTER_WORDS], "removed": [api.REMOVED_ADVISORIES_WORDS]}
    assert api.ADVISORY_STATUS_WORDS == "One of open, not_applicable, closed." and api.CVE_FILTER_WORDS == "A CVE ID, as CVE-YYYY-NNNN."


# --- who reads it, and nothing is written ---------------------------------------------------------------------------------------------

def test_the_levels(client, make_user, tenant, world, readers):
    detail = f"{ADVISORIES}{world.a1.number}/"
    for user in (make_user("analyst"),  # Device security None
                 custom_user(tenant, "equipment-only", {Module.EQUIPMENT: Level.FULL})):
        client.force_login(user)
        for url in (ADVISORIES, detail):
            r = client.get(url)
            assert r.status_code == 403 and r.json()["detail"] == api.ADVISORIES_REFUSAL, (user.username, url)
    for user in (readers.view_only, readers.devices, make_user("technician"), make_user("director"), world.kim):
        client.force_login(user)
        assert client.get(ADVISORIES).status_code == 200 and client.get(detail).status_code == 200, user.username


def test_scoped_users_are_refused_every_door(client, make_user, tenant, world):
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save()
    requester = make_user("requester")
    requester.department = "ICU"
    requester.save()
    company_full = custom_user(tenant, "companyfull", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="BD")
    unit_full = custom_user(tenant, "unitfull", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    detail = f"{ADVISORIES}{world.a1.number}/"
    for user in (vendor, requester, company_full, unit_full):
        client.force_login(user)
        for url in (ADVISORIES, detail, f"{ADVISORIES}?model={world.model.pk}", f"{ADVISORIES}{world.a1.pk}/"):
            r = client.get(url)
            assert r.status_code == 403, (user.username, url)
            if user in (company_full, unit_full):
                assert r.json()["detail"] == ModulePermission.SCOPED_REFUSAL
        for method, url in (("post", ADVISORIES), ("patch", detail), ("delete", detail)):
            assert getattr(client, method)(url, {}, content_type="application/json").status_code == 403
    assert api.SecurityAdvisoryViewSet.scoped_actions == frozenset()


def test_every_write_is_refused(client, make_user, tenant, world, readers):
    detail = f"{ADVISORIES}{world.a1.pk}/"
    writes = [("post", ADVISORIES), ("put", detail), ("patch", detail), ("delete", detail)]

    def state():
        return (sorted(SecurityAdvisory.objects.values_list("number", "status", "removed_on", "models_confirmed_on")),
                AdvisorySource.objects.count(), AdvisoryModel.objects.count())

    before = state()
    for user in (make_user("technician"), world.kim, make_user("director")):  # Edit, Approve, Full: still no write here
        client.force_login(user)
        for method, url in writes:
            r = getattr(client, method)(url, {"status": "closed", "received_on": "2026-01-01"}, content_type="application/json")
            assert r.status_code == 405 and r.json()["detail"] == api.ADVISORIES_READ_ONLY, (user.username, method, url)
    for user in (readers.view_only, readers.devices):  # below Edit
        client.force_login(user)
        for method, url in writes:
            r = getattr(client, method)(url, {}, content_type="application/json")
            assert r.status_code == 403 and r.json()["detail"] == api.ADVISORIES_WRITE_REFUSAL, (user.username, method, url)
    assert state() == before
    assert "Advisories tab" in api.ADVISORIES_READ_ONLY and "Device security Edit" in api.ADVISORIES_WRITE_REFUSAL


# --- fixed queries ---------------------------------------------------------------------------------------------------------------------

def _grow(world, dept, today, n=3):
    """More advisories (open, closed, one entered in error), each naming a new model with devices on site, two decisions each, an update
    open, and a second model row on a1's advisory."""
    for i in range(n):
        model = DeviceModel.objects.create(manufacturer="Demo", model=f"Monitor {i}", description="Monitor", category="Monitors")
        r1, r2 = release(model, "1.0 (demo)"), release(model, "2.0 (demo)")
        devices = [Asset.objects.create(tag=f"M{i}-{k}", device_model=model, department=dept) for k in range(3)]
        for k, dev in enumerate(devices):
            facts(dev, r1 if k else None)
        a = advisories.record_advisory(received_on=today - (20 + i) * DAY, cves=f"CVE-2026-90{i}0", source="manufacturer_bulletin",
                                       reference=f"DEMO-ADV-1{i}", by=None, today=today)
        advisories.set_model(a, device_model=model, versions="listed", release_ids=[r1.pk], remedy="release", fix_release=r2.pk,
                             confirm=True, by=None, today=today)
        update_wo(devices[1], r2)
        decisions.record_decision(asset=devices[0], applies_to=AppliesTo.ADVISORY, advisory=a, response=NOT_IN_USE,
                                  accepted_by=AcceptedBy.CE_LEADER, by=None, today=today)
        decisions.record_decision(release=r1, device_model=model, applies_to=AppliesTo.ADVISORY, advisory=a, response=KEEP_WITHOUT,
                                  accepted_by=AcceptedBy.CE_LEADER, by=None, today=today)
        if i == 1:
            removed = advisories.record_advisory(received_on=today - 30 * DAY, cves="", source="other", reference=f"DEMO-ADV-2{i}",
                                                 by=None, today=today)
            advisories.mark_entered_in_error(removed, by=None, today=today)
    other = DeviceModel.objects.create(manufacturer="Demo", model="Bed X", description="Bed", category="Beds", risk_class=RiskClass.LOW)
    Asset.objects.create(tag="BX-1", device_model=other, department=dept)
    advisories.set_model(world.a1, device_model=other, versions="not_stated", remedy="no_fix_yet", confirm=True, by=None, today=today)


def test_a_page_and_one_advisory_read_a_fixed_number_of_queries(client, world, readers, dept, today):
    def counted(url):
        with CaptureQueriesContext(connection) as q:
            ok(client.get(url))
        return len(q.captured_queries)

    urls = (ADVISORIES, f"{ADVISORIES}?removed=1", f"{ADVISORIES}?cve=CVE-2026-1111", f"{ADVISORIES}?model={world.model.pk}",
            f"{ADVISORIES}{world.a1.number}/", f"{ADVISORIES}{world.a3.number}/", f"{ADVISORIES}{world.a4.number}/")
    before = {}
    for who in ("view_only", "devices"):
        client.force_login(getattr(readers, who))
        before[who] = [counted(u) for u in urls]
    _grow(world, dept, today)
    for who in ("view_only", "devices"):
        client.force_login(getattr(readers, who))
        assert [counted(u) for u in urls] == before[who], who
    assert ok(client.get(ADVISORIES))["count"] == 4 + 3


# --- never words, the browsable API, tokens -------------------------------------------------------------------------------------------

def test_the_never_words_on_every_body(client, make_user, world, readers):
    w = world
    urls = (ADVISORIES, f"{ADVISORIES}?removed=1", *(f"{ADVISORIES}?status={s}" for s in ("open", "closed", "not_applicable")),
            *(f"{ADVISORIES}{a.number}/" for a in (w.a1, w.a2, w.a3, w.a4, w.a5)), f"{ADVISORIES}AD-99-9999/",
            f"{ADVISORIES}?status=x&cve=y")
    bodies = []
    for user in (readers.view_only, readers.devices, w.kim):
        client.force_login(user)
        bodies += [client.get(url).content.decode() for url in urls]
        bodies += [getattr(client, m)(ADVISORIES, {}, content_type="application/json").content.decode() for m in ("post", "delete")]
    client.force_login(make_user("analyst"))
    bodies.append(client.get(ADVISORIES).content.decode())
    assert [never(b) for b in bodies] == [[]] * len(bodies)
    words = (api.ADVISORIES_REFUSAL, api.ADVISORIES_READ_ONLY, api.ADVISORIES_WRITE_REFUSAL, api.ADVISORY_NOT_FOUND,
             api.ADVISORY_STATUS_WORDS, api.CVE_FILTER_WORDS, api.REMOVED_ADVISORIES_WORDS, api.APPLIES_TO_WORDS,
             api.SecurityAdvisoryViewSet.__doc__, api.SecurityDecisionViewSet.__doc__, api_views.MODEL_KEPT)
    assert never(" ".join(words)) == []


def test_the_browsable_api_renders_and_its_description_never_says_the_words(client, world):
    client.force_login(world.kim)
    for url in (ADVISORIES, f"{ADVISORIES}{world.a1.number}/", DECISIONS):
        r = client.get(url, HTTP_ACCEPT="text/html")
        assert r.status_code == 200 and b"<html" in r.content, url
        description = re.search(r'<div class="pull-left">(.*?)</div>', r.content.decode(), re.S)
        assert description and never(description.group(1)) == [], url
    assert world.a1.number.encode() in client.get(ADVISORIES, HTTP_ACCEPT="text/html").content


def _theirs(other_tenant):
    """Another facility's advisory on a model named as ours, with its director's token."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        today = timezone.localdate()
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        rel = release(model, "5.2 (demo)")
        asset = Asset.objects.create(tag="P-1", device_model=model, department=Department.objects.create(name="Their ICU"))
        facts(asset, rel)
        advisory = make_advisory(number="AD-26-0777", reference="THEIR-ADV-1", received_on=today)
        model_row(advisory, model, releases=[rel], remedy="mitigations")
        role = Role.objects.get(slug="director")
    them = User.objects.create_user(username="director@other.example", password="Test-Pass-2026-x", tenant=other_tenant, role=role)
    return advisory, {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=them).key}"}


def test_a_token_reads_only_its_own_facility(client, world, other_tenant):
    advisory, auth = _theirs(other_tenant)
    rows = client.get(ADVISORIES, **auth).json()["results"]
    assert [(r["number"], r["name"]) for r in rows] == [("AD-26-0777", "AD-26-0777 · THEIR-ADV-1 · Alaris 8015 PCU")]
    assert counts_of(rows[0]["counts"]["lines"])["named"] == 1
    assert client.get(f"{ADVISORIES}{world.a1.pk}/", **auth).status_code == 404
    mine = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=world.kim).key}"}
    assert client.get(f"{ADVISORIES}{advisory.pk}/", **mine).status_code == 404
    assert "THEIR-ADV-1" not in client.get(ADVISORIES, **mine).content.decode()


@needs_postgres
def test_the_api_under_the_policies_by_token(client, world, other_tenant, today):
    advisory, _ = _theirs(other_tenant)
    auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=world.kim).key}"}
    as_app_role()
    rows = client.get(ADVISORIES, **auth).json()["results"]
    assert {r["number"] for r in rows} == {world.a1.number, world.a2.number, world.a3.number, world.a5.number}
    one = client.get(f"{ADVISORIES}{world.a1.number}/", **auth).json()
    assert sorted(ln["asset"]["tag"] for ln in one["devices"]) == ["P-1", "P-2", "P-3", "P-4", "P-5", "T-1"]
    assert client.get(f"{ADVISORIES}{advisory.pk}/", **auth).status_code == 404
    decided = client.get(f"{DECISIONS}?applies_to=advisory", **auth).json()["results"]
    assert {(r["number"], r["advisory"]["number"]) for r in decided} == {(world.p2.number, world.a1.number),
                                                                        (world.vent_decision.number, world.a3.number)}


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_every_get_outside_a_transaction(client, tenant, make_user):
    """As a GET runs (autocommit, no transaction): no reader takes a lock (FOR UPDATE there is PostgreSQL's error, never SQLite's)."""
    with tenant_context(tenant):
        today = timezone.localdate()
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH)
        r52, r56 = release(model, "5.2 (demo)"), release(model, "5.6 (demo)")
        devices = [Asset.objects.create(tag=f"P-{i}", device_model=model, department=dept) for i in (1, 2, 3)]
        facts(devices[0], r52)
        facts(devices[1], r56)
        a = advisories.record_advisory(received_on=today, cves="", source="other", reference="DEMO-ADV-0001", by=None, today=today)
        advisories.set_model(a, device_model=model, versions="listed", release_ids=[r52.pk], remedy="release", fix_release=r56.pk,
                             confirm=True, by=None, today=today)
        decisions.record_decision(asset=devices[0], applies_to=AppliesTo.ADVISORY, advisory=a, response=NOT_IN_USE,
                                  accepted_by=AcceptedBy.CE_LEADER, by=None, today=today)
    client.force_login(make_user("director"))
    for url in (ADVISORIES, f"{ADVISORIES}{a.number}/", f"{ADVISORIES}?cve=CVE-2026-0001", f"{DECISIONS}?applies_to=advisory",
                f"{DECISIONS}?advisory={a.number}"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.content)
    assert [ln["answer"] for ln in client.get(f"{ADVISORIES}{a.number}/").json()["devices"]] == ["decided", "", "none"]


# --- advisory decisions on GET security-decisions/ -----------------------------------------------------------------------------------

def test_advisory_decisions_read_their_advisory(client, world, readers, today):
    client.force_login(readers.devices)
    w = world
    rows = {r["number"]: r for r in ok(client.get(DECISIONS))["results"]}
    assert rows[w.p2.number]["advisory"] == {"id": str(w.a1.pk), "number": w.a1.number}
    assert rows[w.vent_decision.number]["advisory"] == {"id": str(w.a3.pk), "number": w.a3.number}
    assert rows[w.support.number]["advisory"] is None
    p2 = rows[w.p2.number]
    assert (p2["applies_to"], p2["applies_to_label"], p2["reasons"], p2["reason_words"]) == ("advisory", "A security advisory", [], [])
    assert (p2["response"], p2["response_label"]) == ("not_in_use_here", "Decided the part it names is not in use here")
    assert p2["scope"]["words"] == "P-2" and p2["sentence"] == rules.decision_words(w.p2, today)
    assert p2["sentence"].startswith(f"Decided {rules.day_words(today)} that the part it names is not in use here; review by")
    assert p2["governs"] == [{"id": str(w.d["P-2"].pk), "tag": "P-2"}] and p2["checks"] == []

    def numbers(query) -> set:
        return {r["number"] for r in ok(client.get(DECISIONS + query))["results"]}

    assert numbers("?applies_to=advisory") == {w.p2.number, w.vent_decision.number}
    assert numbers("?applies_to=support_ended") == {w.support.number}
    assert numbers(f"?advisory={w.a1.number.lower()}") == {w.p2.number} and numbers(f"?advisory={w.a3.pk}") == {w.vent_decision.number}
    assert numbers("?advisory=AD-99-9999") == set() and numbers(f"?advisory={w.a2.number}") == set()
    assert numbers("?device=P-2") == {w.p2.number, w.support.number}  # its own advisory decision, and its release's support one
    assert numbers("?device=P-2&applies_to=advisory&scope=device") == {w.p2.number}
    r = client.get(f"{DECISIONS}?applies_to=advisories&scope=fleet")
    assert r.status_code == 400 and r.json() == {"scope": [api.SCOPE_WORDS], "applies_to": [api.APPLIES_TO_WORDS]}
    assert api.APPLIES_TO_WORDS == "One of support_ended, advisory."
    one = ok(client.get(f"{DECISIONS}{w.p2.number}/"))
    assert one == rows[w.p2.number]


def test_each_advisory_decision_reads_as_the_rules_read_one(client, world, readers, dept, today):
    """The page's batch (rules.decisions_now, keyed) and the one-decision readers agree on every row, advisory ones too: after the
    advisory changes (a revision), a device joins the release's scope, and one leaves its line (moved onto the fix)."""
    w = world
    rel = decisions.record_decision(release=w.r52, device_model=w.model, applies_to=AppliesTo.ADVISORY, advisory=w.a1, response=KEEP_WITHOUT,
                                    accepted_by=AcceptedBy.CE_LEADER, by=None, today=today)
    model_wide = decisions.record_decision(device_model=w.model, applies_to=AppliesTo.ADVISORY, advisory=w.a1,
                                           response=DecisionResponse.TAKE_OUT_OF_USE, accepted_by=AcceptedBy.COMMITTEE, by=None, today=today)
    p6 = Asset.objects.create(tag="P-6", device_model=w.model, department=dept)
    facts(p6, w.r52)  # joined since
    facts(w.d["P-1"], w.r56)  # now on the fix: no longer needs a response
    source = AdvisorySource.objects.filter(advisory=w.a1).order_by("created_at").first()
    advisories.record_revision(source, revision="2", updated_on=today, last_change=["products_changed"], by=None, today=today)
    client.force_login(readers.devices)
    rows = {r["number"]: r for r in ok(client.get(DECISIONS))["results"]}
    assert len(rows) == 5
    for d in DeviceSecurityDecision.objects.select_related("advisory"):
        row = rows[d.number]
        assert [g["id"] for g in row["governs"]] == [str(dd.asset.pk) for dd in rules.decision_devices(d, today) if dd.governed], d.number
        assert [(c["key"], c["words"], c["tags"]) for c in row["checks"]] == [(c.key, c.words, list(c.tags))
                                                                             for c in rules.decision_checks(d, today)], d.number
        assert (row["state"], row["sentence"], row["review_words"]) == (rules.decision_state(d, today), rules.decision_words(d, today),
                                                                        rules.review_words(d, today))
    assert rules.CHECK_ADVISORY_CHANGED in [c["key"] for c in rows[rel.number]["checks"]]
    assert [g["tag"] for g in rows[rel.number]["governs"]] == ["P-6"]  # P-1 left 5.2; P-2 under its own decision
    assert "1 device joined since: P-6" in [c["words"] for c in rows[rel.number]["checks"]]
    # every pump of ours not under a more specific decision of a1's (P-2's own, 5.2's): governing reads the scope, never the line
    assert [g["tag"] for g in rows[model_wide.number]["governs"]] == ["P-1", "P-3", "P-4", "P-5"]
    assert not never(str(rows))


def test_the_decisions_page_reads_a_fixed_number_of_queries_as_advisories_grow(client, world, readers, dept, today):
    client.force_login(readers.devices)

    def counted(url):
        with CaptureQueriesContext(connection) as q:
            ok(client.get(url))
        return len(q.captured_queries)

    urls = (DECISIONS, f"{DECISIONS}?applies_to=advisory", f"{DECISIONS}?device=P-2", f"{DECISIONS}{world.p2.number}/",
            f"{DECISIONS}?advisory={world.a1.number}")
    before = [counted(u) for u in urls]
    _grow(world, dept, today)
    assert ok(client.get(DECISIONS))["count"] == 3 + 6
    assert [counted(u) for u in urls] == before


# --- the model delete an advisory holds (critique40_locks S1) -------------------------------------------------------------------------

def _delete(client, model):
    r = client.delete(f"/api/v1/device-models/{model.pk}/")
    assert r.status_code == 400, r.content
    detail = r.json()["detail"]
    assert never(detail) == []
    return detail


@pytest.fixture
def outsider(tenant):
    """Equipment Full with no Device security level: deletes models, never reads the register."""
    return custom_user(tenant, "equipment-only", {Module.EQUIPMENT: Level.FULL})


@pytest.fixture
def bed(ctx):
    """A model no device, release, or decision holds."""
    return DeviceModel.objects.create(manufacturer="Demo", model="Bed 1", description="Bed", category="Beds", risk_class=RiskClass.LOW)


def test_a_model_an_advisory_names_is_never_deleted(client, world, bed, tenant, outsider, today):
    a = advisories.record_advisory(received_on=today, cves="", source="other", reference="DEMO-ADV-0100", by=None, today=today)
    advisories.set_model(a, device_model=bed, versions="not_stated", remedy="no_fix_yet", by=None, today=today)
    # a reader of advisories (Device security View) reads its number; a model-level word, never a device's
    reader = custom_user(tenant, "models-and-register", {Module.EQUIPMENT: Level.FULL, Module.CYBERSECURITY: Level.VIEW})
    client.force_login(reader)
    assert _delete(client, bed) == f"Demo Bed 1 has a security advisory naming it ({a.number}), so it cannot be deleted: {NAMES}."
    client.force_login(outsider)
    assert _delete(client, bed) == "Demo Bed 1 has records in the Device security register, so it cannot be deleted."
    # entered in error: it still keeps the models it named
    advisories.mark_entered_in_error(a, by=None, today=today)
    client.force_login(reader)
    assert _delete(client, bed).startswith(f"Demo Bed 1 has a security advisory naming it ({a.number})")
    # a second advisory, and the register's release and a device: every part named, in order
    b = advisories.record_advisory(received_on=today, cves="", source="other", reference="DEMO-ADV-0101", by=None, today=today)
    advisories.set_model(b, device_model=bed, versions="not_stated", remedy="mitigations", by=None, today=today)
    release(bed, "1.0 (demo)")
    Asset.objects.create(tag="B-1", device_model=bed, department=world.dept)
    first, second = sorted((a.number, b.number))
    assert _delete(client, bed) == (f"Demo Bed 1 has devices, and a software release in the Device security register, and security "
                                    f"advisories naming it ({first}, {second}), so it cannot be deleted: {KEEPS}; {NAMES}.")
    assert DeviceModel.objects.filter(pk=bed.pk).exists()


def test_a_model_with_a_decision_and_an_advisory(client, world, make_user, tenant):
    """The pumps: devices, releases, the support decision, a1's device decision (P-2's own: never the model's), a1 and a5 naming it."""
    client.force_login(make_user("director"))
    numbers = ", ".join(sorted((world.a1.number, world.a5.number)))
    detail = _delete(client, world.model)
    assert detail == (f"BD Alaris 8015 PCU has devices, and software releases and a profile in the Device security register, and a "
                      f"security decision ({world.support.number}), and security advisories naming it ({numbers}), so it cannot be "
                      f"deleted: {KEEPS}; {STAYS}; {NAMES}.")
    # the ventilators: a device's own decision never holds its model; the advisory naming it does
    assert _delete(client, world.vent_model) == (f"Hamilton Medical Hamilton-G5 has devices, and a security advisory naming it "
                                                 f"({world.a3.number}), so it cannot be deleted: {NAMES}.")
    client.force_login(custom_user(tenant, "equipment-only", {Module.EQUIPMENT: Level.FULL}))
    assert _delete(client, world.model) == "BD Alaris 8015 PCU has devices, and records in the Device security register, so it cannot be deleted."


def test_the_delete_locks_the_model_first(client, world, bed, make_user, monkeypatch):
    """The model's row FOR UPDATE before the collector reads what holds it (critique40_locks S1), in the delete's transaction."""
    seen = []
    real = DeviceModel.delete

    def spying(self, *args, **kwargs):
        seen.append(("delete", self.pk, connection.in_atomic_block))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(DeviceModel, "delete", spying)
    client.force_login(make_user("director"))
    with CaptureQueriesContext(connection) as q:
        assert client.delete(f"/api/v1/device-models/{bed.pk}/").status_code == 204
    sql = [x["sql"] for x in q.captured_queries]
    locked = [i for i, s in enumerate(sql) if "FOR UPDATE" in s and "equipment_devicemodel" in s]
    deleted = [i for i, s in enumerate(sql) if s.startswith("DELETE") and "equipment_devicemodel" in s]
    if connection.features.has_select_for_update:  # PostgreSQL: the lock, then the delete
        assert locked and deleted and locked[0] < deleted[0]
    assert seen == [("delete", bed.pk, True)] and not DeviceModel.objects.filter(pk=bed.pk).exists()


def test_a_row_naming_the_model_at_commit_answers_in_words(client, bed, make_user, monkeypatch):
    """The backstop: an IntegrityError at the delete's commit (a row naming the model committed meanwhile) is a 400 in words."""
    def failing(self, *args, **kwargs):
        raise IntegrityError("update or delete on table violates foreign key constraint")

    monkeypatch.setattr(DeviceModel, "delete", failing)
    client.force_login(make_user("director"))
    r = client.delete(f"/api/v1/device-models/{bed.pk}/")
    assert r.status_code == 400 and r.json()["detail"] == "Demo Bed 1 is in use, so it cannot be deleted."
    assert api_views.MODEL_KEPT == "{} is in use, so it cannot be deleted."


# --- PostgreSQL: Set models against a model's delete, two threads -------------------------------------------------------------------

def _thread(target, errors, results, key):
    def run():
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '20s'")  # a wrong order fails, never hangs the suite
            results[key] = target()
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors[key] = e
        finally:
            connection.close()
    return threading.Thread(target=run, name=key)


def _both(first, second, errors, results):
    a, b = _thread(first, errors, results, "first"), _thread(second, errors, results, "second")
    a.start()
    b.start()
    for t in (a, b):
        t.join(60)
    assert not a.is_alive() and not b.is_alive()  # never a hang


def _waiting(timeout: float = 10) -> list:
    """What another connection to this database waits for, as soon as one does: [(table, tuple lock mode)] for a row lock, else
    [("statement", its first 27 characters)], else [] after `timeout` (tests/test_cyber_decisions_locks.py's)."""
    import time

    deadline = time.monotonic() + timeout
    with connection.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute("SELECT coalesce(l.relation::regclass::text, 'statement'), coalesce(l.mode, left(a.query, 27)) "
                        "FROM pg_stat_activity a LEFT JOIN pg_locks l ON l.pid = a.pid AND l.locktype = 'tuple' "
                        "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() AND a.wait_event_type = 'Lock'")
            found = cur.fetchall()
            if found:
                return [tuple(row) for row in found]
            time.sleep(0.05)
    return []


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["set_model", "delete"])
def test_set_model_and_a_model_delete(tenant, make_user, monkeypatch, first_is):
    """Set models naming a model no device, release, or decision holds, against the API's delete of that model (critique40_locks B3.1):
    - Set models first, paused after its row's insert (holding the register and the advisory, nothing on the model: a deferred foreign
      key takes it KEY SHARE only at COMMIT): the delete never waits (it reads no uncommitted row) and commits; Set models' COMMIT then
      finds the model gone and answers RACE keyed device_model.
    - The delete first, paused between its collector's read and its DELETE, holding the model FOR UPDATE: Set models runs to its COMMIT,
      which waits on the model row (seen: equipment_devicemodel); the delete commits; Set models answers RACE. Without the lock, Set models
      would commit between the read and the DELETE, and the delete's COMMIT would fail (a 500).
    Both finish, never a 500 or a deadlock, and every refusal is in words."""
    from django.db.models import deletion

    today = timezone.localdate()
    with tenant_context(tenant):
        from apps.core.models import Sequence

        model = DeviceModel.objects.create(manufacturer="Demo", model="Bed 1", description="Bed", category="Beds", risk_class=RiskClass.LOW)
        a = advisories.record_advisory(received_on=today, cves="", source="other", reference="DEMO-ADV-0001", by=None, today=today)
        with transaction.atomic():
            services.lock_register()
            Sequence.objects.get_or_create(key=f"advisory-{today.year % 100:02d}")
    director = make_user("director")
    web = Client()
    web.force_login(director)
    started, seen, deleted = threading.Event(), {}, threading.Event()
    if first_is == "set_model":
        real_save = services._save

        def holding(obj, by, reason):  # Set models pauses after its row's insert, holding the register and the advisory
            out = real_save(obj, by, reason)
            if threading.current_thread().name == "first" and isinstance(obj, AdvisoryModel) and not started.is_set():
                started.set()
                seen["deleted_meanwhile"] = deleted.wait(20)  # the delete never waits on anything Set models holds
            return out

        monkeypatch.setattr(services, "_save", holding)
    else:
        real_delete = deletion.Collector.delete

        def paused(self, *args, **kwargs):  # the delete pauses after its collector's read, before its DELETE
            if threading.current_thread().name == "first" and not started.is_set():
                started.set()
                seen["waiting"] = _waiting()
            return real_delete(self, *args, **kwargs)

        monkeypatch.setattr(deletion.Collector, "delete", paused)
    errors, results = {}, {}

    def setting():
        if first_is == "delete":
            assert started.wait(20)
        with tenant_context(tenant):
            return advisories.set_model(a.pk, device_model=model.pk, versions="not_stated", remedy="no_fix_yet", by=None, today=today).pk

    def deleting():
        if first_is == "set_model":
            assert started.wait(20)
        try:
            r = web.delete(f"/api/v1/device-models/{model.pk}/")
            return r.status_code, r.json() if r.content else None
        finally:
            deleted.set()

    first, second = (setting, deleting) if first_is == "set_model" else (deleting, setting)
    _both(first, second, errors, results)
    key = "first" if first_is == "set_model" else "second"
    other = "second" if key == "first" else "first"
    assert set(errors) <= {key}, errors
    assert results[other] == (204, None)  # the delete commits
    refusal = errors[key]
    assert isinstance(refusal, ValidationError) and refusal.message_dict == {"device_model": [advisories.RACE]}
    if first_is == "set_model":
        assert seen["deleted_meanwhile"] is True
    else:
        assert {table for table, _mode in seen["waiting"]} == {"equipment_devicemodel"}, seen  # Set models' COMMIT, on the model only
    with tenant_context(tenant):
        assert not DeviceModel.objects.filter(pk=model.pk).exists() and not AdvisoryModel.objects.exists()


@needs_postgres
def test_the_delete_refusal_under_the_policies(client, world, make_user):
    client.force_login(make_user("director"))
    as_app_role()
    assert _delete(client, world.vent_model).endswith(f"({world.a3.number}), so it cannot be deleted: {NAMES}.")
