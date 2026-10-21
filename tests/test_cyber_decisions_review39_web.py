"""
Slice 39 review fixes (the web doors):

- A release row of the model panel says "Devices of ours on it are past their support, or their end is announced, with no decision
  recorded." and offers Record decision only for devices of ours on it with a need that no decision governs (their own, a device scope)
  and no update answers (an open update to a supported release, or one done with the version not confirmed): as Needs attention reads
  them, so the two doors agree, on the seeded demo too (its C-arm on 7.2 (demo) is governed by its own SD-26-0002). Fixed queries as
  such devices grow.
- A review day already passed is said before the save only for a decision the service would take (a dry run, rolled back: nothing kept,
  not even its number); otherwise the service's refusals come first, never the note and then the refusal.
- Review and renew offered from a release row (a decision in force meanwhile) keeps the panel's filters, so the renewal saved answers with
  the panel as it was opened.
- Review and renew of a rental's "Return it to its owner" once the facility kept it starts at No plan recorded, the select showing what it
  sends (never its first option, chosen by nobody).
"""
import re
import threading
from datetime import timedelta
from io import StringIO

import pytest
from cyber_fixtures import facts, release, update_wo
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import needs_postgres
from test_cyber_decisions_web import (  # noqa: F401 (fixtures)
    HX,
    KEEP_WITHOUT,
    MODAL,
    OUT_OF_USE,
    decide,
    errors,
    floor,
    html,
    number,
    signed_in,
    tab,
)

from apps.accounts.models import User
from apps.core.models import Sequence
from apps.cybersecurity import decisions, rules, services
from apps.cybersecurity.models import AcceptedBy, DecisionEnd, DecisionPlan, DeviceSecurityDecision, SoftwareRelease
from apps.equipment import services as eq
from apps.equipment.models import Asset, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

DAY = timedelta(days=1)
NO_DECISION_ROW = "Devices of ours on it are past their support, or their end is announced, with no decision recorded."


def panel(client, model, query: str = ""):
    """The model panel (HTMX) and its release rows' decisions by version."""
    r = client.get(f"/security/models/{model.pk}/{query}", **HX)
    assert r.status_code == 200
    return r, {x["r"].version: x["decision"] for x in r.context["releases"]}


def offered(rows: dict) -> set:
    """The versions whose row offers Record decision."""
    return {version for version, d in rows.items() if d is not None and d["record_url"]}


def attention_releases(user, today, model) -> set:
    """The versions of `model` Needs attention offers Record decision for (its no_decision and ending rows, release scope)."""
    ids = {str(dict(row.record.query)["release"]) for h in rules.needs_attention(today, user=user).headings
           if h.key in ("no_decision", "ending") for row in h.rows if row.record is not None and "release" in dict(row.record.query)}
    return {r.version for r in SoftwareRelease.objects.filter(device_model=model) if str(r.pk) in ids} if ids else set()


# --- a release row: "no decision recorded" only where none governs and no update answers ------------------------------------------------

def test_a_release_row_offers_record_decision_only_while_a_device_has_no_decision_governing_it(client, signed_in, floor):  # noqa: F811
    kim = signed_in("director")
    today = floor.today
    r, rows = panel(client, floor.model)
    assert offered(rows) == {"5.2 (demo)"} == attention_releases(kim, today, floor.model) and NO_DECISION_ROW in html(r)
    decide(asset=floor.d["P-1"])
    decide(asset=floor.d["P-2"])
    r, rows = panel(client, floor.model)
    assert offered(rows) == {"5.2 (demo)"} == attention_releases(kim, today, floor.model)  # P-3 has none
    # P-3's own, past its review: it still governs P-3 (Needs attention lists it under review_due), so no row says "no decision"
    late = decide(asset=floor.d["P-3"], decided_on=today - 400 * DAY, review_by=today - DAY)
    r, rows = panel(client, floor.model)
    body = html(r)
    assert rows["5.2 (demo)"] is None and NO_DECISION_ROW not in body and "Record decision</button>" not in body
    assert attention_releases(kim, today, floor.model) == set()
    assert {h.key for h in rules.needs_attention(today, user=kim).headings} == {"temporary", "review_due"}  # T-1, and P-3's
    # P-3's decision ended: P-3 has none again
    decisions.end_decision(late, by=None)
    assert offered(panel(client, floor.model)[1]) == {"5.2 (demo)"} == attention_releases(kim, today, floor.model)


def test_a_release_row_offers_record_decision_only_while_no_update_answers(client, signed_in, floor):  # noqa: F811
    kim = signed_in("director")
    today = floor.today
    old = release(floor.model, "5.0 (demo)", security_updates_end_on=today - 400 * DAY)
    update_wo(floor.d["P-1"], floor.r56)  # open, to a supported release: answers P-1
    update_wo(floor.d["P-2"], floor.r56)
    update_wo(floor.d["P-3"], old)  # to a release past its support: answers nothing
    r, rows = panel(client, floor.model)
    assert offered(rows) == {"5.2 (demo)"} == attention_releases(kim, today, floor.model) and NO_DECISION_ROW in html(r)
    update_wo(floor.d["P-3"], floor.r56)
    r, rows = panel(client, floor.model)
    assert rows["5.2 (demo)"] is None and NO_DECISION_ROW not in html(r) and attention_releases(kim, today, floor.model) == set()


def test_a_release_row_with_the_release_or_model_decision_is_unchanged(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    own = decide(asset=floor.d["P-1"])
    d = decide(release=floor.r52)
    rows = panel(client, floor.model)[1]
    assert rows["5.2 (demo)"]["number"] == d.number and not rows["5.2 (demo)"]["record_url"] and own.number != d.number
    signed_in("technician")  # no Approve: never Record decision, the decision still read
    rows = panel(client, floor.model)[1]
    assert rows["5.2 (demo)"]["number"] == d.number and offered(rows) == set()


def test_the_release_rows_read_a_fixed_number_of_queries_as_answered_devices_grow(client, signed_in, floor, dept):  # noqa: F811
    signed_in("director")
    today = floor.today
    decide(asset=floor.d["P-1"])
    update_wo(floor.d["P-2"], floor.r56)
    before = _count(client, floor.model)
    old = release(floor.model, "5.0 (demo)", security_updates_end_on=today - 400 * DAY)
    for i in range(9):
        a = Asset.objects.create(tag=f"PX-{i}", device_model=floor.model, department=dept)
        facts(a, floor.r52 if i % 2 else old)
        if i % 3 == 0:
            decide(asset=a)
        elif i % 3 == 1:
            update_wo(a, floor.r56)
    after = _count(client, floor.model)
    assert after == before


def _count(client, model) -> int:
    with CaptureQueriesContext(connection) as q:
        panel(client, model)
    return len(q.captured_queries)


@pytest.fixture
def seeded(db):
    call_command("seed_demo", stdout=StringIO())
    return Tenant.objects.get(slug="riverside")


def test_the_seeded_release_rows_agree_with_needs_attention(client, seeded):
    """The seed's C-arm of ours on 7.2 (demo) is governed by its own SD-26-0002: its row offers no decision, and no row of any model
    says "no decision recorded" while Needs attention lists no device of ours so."""
    with tenant_context(seeded):
        today = timezone.localdate()
        kim = User.objects.get(username="kim@riverside.example")
        client.force_login(kim)
        c_arm = DeviceModel.objects.get(model="Cios Spin")
        r, rows = panel(client, c_arm)
        assert rows["7.2 (demo)"] is None and NO_DECISION_ROW not in html(r)
        governing = rules.governing_decisions(Asset.objects.filter(device_model=c_arm, ownership=Ownership.OWNED), today)
        assert {d.number for d in governing.values() if d is not None} == {number(2, today)}
        models = DeviceModel.objects.filter(software_releases__version_key__gt="").distinct()
        assert c_arm in models and len(models) > 3
        for model in models:
            assert offered(panel(client, model)[1]) == attention_releases(kim, today, model), model.model


# --- a review day already passed: said only for a decision the service would take ----------------------------------------------------

def test_the_passed_review_note_comes_only_when_nothing_else_is_refused(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    today = floor.today
    url = "/security/decisions/new/?asset=P-1"

    def first(**fields) -> tuple[str, dict]:
        r = client.post(url, {"reasons": ["security_updates"], **fields}, **MODAL)
        assert r.status_code == 200 and "HX-Retarget" not in r
        body = html(r)
        return body, errors(body)

    # the review past the cap for Take out of use: the refusal now, never the note first
    decided, review = today - 500 * DAY, today - 35 * DAY
    body, found = first(response=OUT_OF_USE, accepted_by=AcceptedBy.COMMITTEE, decided_on=decided.isoformat(), review_by=review.isoformat())
    cap = rules.review_cap(OUT_OF_USE, decided)
    assert found == {"review_by": decisions.REVIEW_CAP.format(day=rules.day_words(cap), months=rules.OUT_OF_USE_CAP_MONTHS)}
    assert decisions.REVIEW_PASSED.format(day=rules.day_words(review)) not in body and "Save again" not in body
    # who accepted it missing: its refusal, no note
    body, found = first(response=KEEP_WITHOUT, decided_on=(today - 400 * DAY).isoformat(), review_by=review.isoformat())
    assert found == {"accepted_by": decisions.ACCEPTED_WORDS} and "Save again" not in body
    # the response missing too, with a blank review day (no default to say): both refusals
    body, found = first(decided_on=(today - 400 * DAY).isoformat())
    assert found == {"response": decisions.RESPONSE_WORDS, "accepted_by": decisions.ACCEPTED_WORDS} and "Save again" not in body
    # a decision in force for the scope meanwhile (under the service's locks): at the top, no note
    live = decide(asset=floor.d["P-1"])
    body, found = first(response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER, decided_on=(today - 400 * DAY).isoformat(),
                        review_by=review.isoformat())
    assert found == {"": decisions.IN_FORCE.format(number=live.number)} and "Save again" not in body
    assert DeviceSecurityDecision.objects.count() == 1


def test_the_note_keeps_nothing_and_the_second_save_records_it(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    today = floor.today
    decided, review = today - 400 * DAY, today - 35 * DAY
    fields = {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.IT_SECURITY,
              "decided_on": decided.isoformat(), "review_by": review.isoformat()}
    body = html(client.post("/security/decisions/new/?asset=P-1", fields, **MODAL))
    assert decisions.REVIEW_PASSED.format(day=rules.day_words(review)) in body and errors(body) == {}
    # the dry run kept nothing: no decision, no history, no number taken
    assert not DeviceSecurityDecision.objects.exists() and not DeviceSecurityDecision.history.exists()
    assert not Sequence.unscoped.filter(key__startswith="decision-").exists()
    r = client.post("/security/decisions/new/?asset=P-1", {**fields, "review_passed": review.isoformat()}, **MODAL)
    d = DeviceSecurityDecision.objects.get()
    assert r["HX-Retarget"] == "#drawer" and d.number == number(1, decided) and d.review_by == review
    # Review and renew with a day passed: the note leaves the decision renewed live, the second save supersedes it
    url = f"/security/decisions/new/?supersedes={d.number}"
    body = html(client.post(url, fields, **MODAL))
    assert decisions.REVIEW_PASSED.format(day=rules.day_words(review)) in body and f"Review and renew {d.number}" in body
    d.refresh_from_db()
    assert d.ended_on is None and DeviceSecurityDecision.objects.count() == 1
    client.post(url, {**fields, "review_passed": review.isoformat()}, **MODAL)
    d.refresh_from_db()
    renewed = DeviceSecurityDecision.objects.get(ended_on__isnull=True)
    assert (d.end_reason, renewed.supersedes, renewed.number) == (DecisionEnd.SUPERSEDED, d, number(2, decided))


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_the_dry_run_holds_no_lock_and_keeps_nothing_in_autocommit(client, signed_in, floor, tenant):  # noqa: F811
    """PostgreSQL, no test transaction around the request (none wraps it in production): the dry run's transaction is rolled back, its
    locks released (another connection takes the register's and the device's rows with NOWAIT), and nothing it wrote is kept."""
    signed_in("director")
    today = floor.today
    with transaction.atomic():
        services.lock_register()  # the register's row exists (committed), so the check below can lock it
    decided, review = today - 400 * DAY, today - 35 * DAY
    fields = {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.IT_SECURITY,
              "decided_on": decided.isoformat(), "review_by": review.isoformat()}
    body = html(client.post("/security/decisions/new/?asset=P-1", fields, **MODAL))
    assert decisions.REVIEW_PASSED.format(day=rules.day_words(review)) in body
    assert not connection.in_atomic_block
    taken, failed = [], []

    def other():
        try:
            with tenant_context(tenant), transaction.atomic():
                Sequence.unscoped.select_for_update(nowait=True).get(tenant=tenant, key=services.REGISTER_LOCK)
                Asset.objects.select_for_update(nowait=True).get(tag="P-1")
                taken.append(True)
        except Exception as e:  # noqa: BLE001 - reported by the test
            failed.append(e)
        finally:
            connection.close()

    t = threading.Thread(target=other)
    t.start()
    t.join(30)
    assert taken == [True] and failed == []
    assert not DeviceSecurityDecision.objects.exists() and not Sequence.unscoped.filter(key__startswith="decision-").exists()
    client.post("/security/decisions/new/?asset=P-1", {**fields, "review_passed": review.isoformat()}, **MODAL)
    assert DeviceSecurityDecision.objects.get().number == number(1, decided)


# --- Review and renew from a release row keeps the panel's filters -------------------------------------------------------------------

def test_review_and_renew_from_a_release_row_answers_with_the_panel_as_it_was_opened(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    today = floor.today
    query = "whose=ours&standing=updates_ended"
    r, rows = panel(client, floor.model, f"?{query}")
    assert r.context["narrowed"] and rows["5.2 (demo)"]["record_url"] == f"/security/decisions/new/?release={floor.r52.pk}&from=panel&{query}"
    old = decide(release=floor.r52)  # someone else records it meanwhile
    r = client.get(rows["5.2 (demo)"]["record_url"], **MODAL)
    renew = r.context["renew_url"]
    assert decisions.IN_FORCE.format(number=old.number) in html(r)
    assert renew == f"/security/decisions/new/?supersedes={old.number}&from=panel&{query}"
    assert f'hx-post="{renew}"' in html(client.get(renew, **MODAL))
    r = client.post(renew, {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER,
                            "decided_on": today.isoformat()}, **MODAL)
    assert r["HX-Retarget"] == "#drawer" and r.context["narrowed"]
    assert (r.context["f"].whose, r.context["f"].standing) == ("ours", "updates_ended")
    assert [s.asset.tag for s in r.context["devices"]] == ["P-1", "P-2", "P-3"]  # never the rental T-1: the panel as it was opened
    assert DeviceSecurityDecision.objects.get(ended_on__isnull=True).supersedes == old
    # from the Security tab and the decision's page, nothing more is carried
    own = decide(asset=floor.d["T-1"])
    r = client.get("/security/decisions/new/?asset=T-1&from=device", **MODAL)
    assert r.context["renew_url"] == f"/security/decisions/new/?supersedes={own.number}&from=device"


# --- Review and renew of a plan no longer offered ----------------------------------------------------------------------------------

def _selected(body: str, name: str) -> tuple:
    """([the values selected], [every value offered]) of the modal's <select name=...>."""
    select = re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', body, re.S).group(1)
    return re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', select), re.findall(r'<option value="([^"]*)"', select)


def test_review_and_renew_of_a_kept_rentals_return_to_owner_starts_at_no_plan(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    today = floor.today
    old = decide(asset=floor.d["T-1"], plan=DecisionPlan.RETURN_TO_OWNER)
    eq.keep_temporary_device(Asset.objects.get(tag="T-1"), acquisition_cost="100.00")
    t1 = Asset.objects.get(tag="T-1")
    assert t1.ownership == Ownership.OWNED
    renew = tab(client, t1).context["cyber"]["decision"]["renew_url"]
    assert renew == f"/security/decisions/new/?supersedes={old.number}&from=device"
    body = html(client.get(renew, **MODAL))
    selected, values = _selected(body, "plan")
    assert selected == [DecisionPlan.NONE] and DecisionPlan.RETURN_TO_OWNER not in values
    assert _selected(body, "plan_owner")[0] == [""] and 'name="plan_by" value=""' in body
    # the modal saved as it shows: No plan recorded
    r = client.post(renew, {"reasons": ["security_updates"], "response": old.response, "plan": selected[0], "accepted_by": old.accepted_by,
                            "decided_on": today.isoformat()}, **MODAL)
    renewed = DeviceSecurityDecision.objects.get(ended_on__isnull=True)
    assert r["HX-Retarget"] == "#drawer" and (renewed.plan, renewed.supersedes) == (DecisionPlan.NONE, old)


def test_review_and_renew_keeps_a_plan_still_offered(client, signed_in, floor):  # noqa: F811
    kim = signed_in("director")
    today = floor.today
    old = decide(asset=floor.d["T-1"], plan=DecisionPlan.RETURN_TO_OWNER)
    body = html(client.get(f"/security/decisions/new/?supersedes={old.number}&from=device", **MODAL))
    assert _selected(body, "plan")[0] == [DecisionPlan.RETURN_TO_OWNER]  # still a rental: its plan kept
    planned = decide(release=floor.r52, plan=DecisionPlan.UPDATE_PLANNED, plan_by=today + 30 * DAY, plan_owner=kim)
    body = html(client.get(f"/security/decisions/new/?supersedes={planned.number}", **MODAL))
    assert _selected(body, "plan")[0] == [DecisionPlan.UPDATE_PLANNED] and _selected(body, "plan_owner")[0] == [str(kim.pk)]
    assert f'name="plan_by" value="{(today + 30 * DAY).isoformat()}"' in body
