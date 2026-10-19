"""
Slice 32: the program's annual evaluation over the API (apps/api/views_evaluations.py, serializers_evaluations.py), read-only.

GET /api/v1/program-evaluations/ lists the evaluations newest period first with no figures and no text; GET .../<id or number>/ one
evaluation (by number its version of record, ?version= a given one) with its text, objectives, approvals, and the figures cut to the
reader (apps.evaluations.indicators.cut). Evaluations View; scoped users never (a custom scoped role with Full everywhere
neither); writes are refused; a fixed number of queries on the list; tokens read their own facility only; under row-level security
on PostgreSQL.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from evaluation_fixtures import approved, draft, last_year, results_for_all, year_before
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.api import views_evaluations
from apps.evaluations import indicators, services
from apps.evaluations.models import NARRATIVE, Body, ObjectiveKind, ObjectiveResult, ProgramEvaluation, Status
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

API = "/api/v1/program-evaluations/"
LIST_FIELDS = {"id", "number", "version", "label", "period_start", "period_end", "period_label", "status", "status_label", "sent_on",
               "approved_on", "amends", "amends_label", "approvals", "url"}
EVERY_VIEW = {m: Level.VIEW for m in Module.values}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def evaluations(ctx, today):
    """Last year's evaluation approved by the EOC committee and then the governing body, with its text, an objective of its own
    reviewed, and one set for this year; this year's draft (its review objective carried from last year's next one), with two parts
    of its text written and the plan reviewed."""
    start, _end = last_year(today)
    before = approved(start=year_before(start), today=today, meeting_on=today - timedelta(days=5),
                      next_objectives=["Cut the repair turnaround to 3 days"])
    services.record_approval(before, body=Body.OTHER, body_name="Board quality committee", meeting_on=today, chair_name="Pat Lee",
                             chair_title="Chair", minutes_reference="BQC-2026-04", by=None, today=today)
    current = draft(start=start, today=today)
    services.update_text(current, "summary", "Steady year; see the figures.", by=None)
    services.update_text(current, "cybersecurity", "Patched every networked pump.", by=None)
    services.update_text(current, "plan_title", "Medical Equipment Management Plan", by=None)
    services.update_text(current, "plan_version", "v4", by=None)
    services.update_text(current, "inventory_reviewed_on", today - timedelta(days=10), by=None)
    results_for_all(current, result=ObjectiveResult.PARTLY)
    services.add_objective(current, kind=ObjectiveKind.NEXT, text="Inspect every new device before first use", target="100%", by=None)
    current.refresh_from_db()
    before.refresh_from_db()
    return {"before": before, "current": current}


def custom_user(tenant, name, levels, scope="", **extra):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)


def signed_in(client, user):
    client.force_login(user)
    return user


def ok(r):
    assert r.status_code == 200, r.content
    return r.json()


# --- the list -------------------------------------------------------------------------------------------------------------------

def test_the_list_has_no_figures_and_no_text(client, make_user, evaluations, today):
    signed_in(client, make_user("director"))
    data = ok(client.get(API))
    before, current = evaluations["before"], evaluations["current"]
    assert data["count"] == 2 and [e["number"] for e in data["results"]] == [current.number, before.number]  # newest period first
    for item in data["results"]:
        assert set(item) == LIST_FIELDS
    body = client.get(API).content.decode()
    assert "Steady year" not in body and "Patched every" not in body and "pm_high" not in body and "Cut the repair" not in body
    first, second = data["results"]
    assert (first["status"], first["status_label"], first["approvals"], first["sent_on"], first["approved_on"]) == (
        "draft", "Draft", [], None, None)
    assert first["period_label"] == services.period_label(current.period_start, current.period_end)
    assert first["url"] == f"/reports/evaluations/{current.number}/v1/?facility=riverside"
    assert (second["status"], second["approved_on"], second["label"]) == ("approved", (today - timedelta(days=5)).isoformat(), before.number)
    assert second["approvals"] == [
        {"body": "eoc", "committee": "Environment of care or safety committee", "meeting_on": (today - timedelta(days=5)).isoformat()},
        {"body": "other", "committee": "Board quality committee", "meeting_on": today.isoformat()}]
    for url in (API, f"{API}{current.number}/"):  # the browsable API renders inside the facility too
        assert client.get(url, HTTP_ACCEPT="text/html").status_code == 200, url


def test_the_list_filters_by_status(client, make_user, evaluations):
    signed_in(client, make_user("director"))
    assert [e["status"] for e in ok(client.get(f"{API}?status=approved"))["results"]] == ["approved"]
    assert ok(client.get(f"{API}?status=discarded"))["results"] == []
    r = client.get(f"{API}?status=everything")
    assert r.status_code == 400 and "status" in r.json()
    assert [e["number"] for e in ok(client.get(f"{API}?ordering=period_start"))["results"]] == [
        evaluations["before"].number, evaluations["current"].number]


def test_a_fixed_number_of_queries_on_the_list(client, make_user, ctx, today):
    signed_in(client, make_user("director"))
    first = last_year(today)[0]

    def queries():
        with CaptureQueriesContext(connection) as q:
            assert client.get(API).status_code == 200
        return len(q.captured_queries)

    approved(start=add_months(first, -12), today=today)
    draft(start=first, today=today)
    few = queries()
    for n in (24, 36, 48):
        ev = approved(start=add_months(first, -n), today=today)
        services.record_approval(ev, body=Body.GOVERNING, meeting_on=today, by=None, today=today)
    services.amend(ev, by=None, today=today)
    assert ok(client.get(API))["count"] == 6
    assert queries() == few


# --- one evaluation -------------------------------------------------------------------------------------------------------------

def test_one_evaluation_by_id_by_number_and_by_version(client, make_user, evaluations, today, other_tenant):
    signed_in(client, make_user("director"))
    before = evaluations["before"]
    assert ok(client.get(f"{API}{before.id}/"))["id"] == str(before.id)
    amendment = services.amend(before, by=None, today=today)
    shown = ok(client.get(f"{API}{before.number.lower()}/"))  # any letter case: the version of record, while the amendment is drafted
    assert (shown["id"], shown["version"], shown["status"]) == (str(before.id), 1, "approved")
    v2 = ok(client.get(f"{API}{before.number}/?version=2"))
    assert (v2["id"], v2["label"], v2["status"], v2["amends"], v2["amends_label"]) == (
        str(amendment.id), f"{before.number} v2", "draft", str(before.id), before.number)
    assert v2["url"] == f"/reports/evaluations/{before.number}/v2/?facility=riverside"
    assert v2["print_url"] == f"/print/evaluations/{before.number}/v2/?facility=riverside"
    assert client.get(f"{API}{before.number}/?version=two").status_code == 400
    assert client.get(f"{API}{before.number}/?version=7").status_code == 404
    assert client.get(f"{API}EV-99-0001/").json()["detail"] == views_evaluations.NOT_FOUND
    with tenant_context(other_tenant):
        theirs = draft(today=today)
    assert theirs.number == evaluations["current"].number  # numbers repeat across facilities
    assert client.get(f"{API}{theirs.id}/").status_code == 404  # another facility's, by id
    assert ok(client.get(f"{API}{theirs.number}/"))["id"] == str(evaluations["current"].id)  # by number: this facility's own


def test_the_text_objectives_and_approvals(client, make_user, evaluations, today):
    signed_in(client, make_user("director"))
    current, before = evaluations["current"], evaluations["before"]
    data = ok(client.get(f"{API}{current.number}/"))
    assert [t["field"] for t in data["text"]] == [f for f, _label, _help in NARRATIVE]
    text = {t["field"]: t for t in data["text"]}
    assert text["summary"] == {"field": "summary", "label": "Summary", "text": "Steady year; see the figures."}
    assert text["cybersecurity"]["text"] == "Patched every networked pump." and text["went_well"]["text"] == ""
    assert data["plan"] == {"title": "Medical Equipment Management Plan", "version": "v4", "effective_on": None}
    assert data["inventory_reviewed_on"] == (today - timedelta(days=10)).isoformat()
    [review] = data["objectives"]["review"]
    assert (review["text"], review["result"], review["result_label"], review["carried"]) == (
        "Cut the repair turnaround to 3 days", "partly", "Partly met", True)
    assert data["objectives"]["next"] == [{"id": data["objectives"]["next"][0]["id"], "position": 1,
                                           "text": "Inspect every new device before first use", "target": "100%"}]
    assert data["approvals"] == [] and data["created_by_name"] is None
    approvals = ok(client.get(f"{API}{before.id}/"))["approvals"]
    assert [(a["committee"], a["approves"]) for a in approvals] == [("Environment of care or safety committee", True),
                                                                    ("Board quality committee", False)]
    assert (approvals[1]["body_label"], approvals[1]["chair_name"], approvals[1]["chair_title"], approvals[1]["minutes_reference"]) == (
        "Another committee", "Pat Lee", "Chair", "BQC-2026-04")


def test_the_director_sees_every_figure(client, make_user, evaluations):
    director = signed_in(client, make_user("director"))
    figures = ok(client.get(f"{API}{evaluations['current'].id}/"))["figures"]
    stored = evaluations["current"].snapshot.data
    assert figures["left_out"] == [] and figures["complete"] is True
    assert [i["key"] for i in figures["indicators"]] == [i.key for i in indicators.INDICATORS]
    assert (figures["met"], figures["judged"]) == indicators.tally(stored["indicators"])
    assert figures["taken_on"] == stored["taken_on"] and figures["taken_by_name"] is None
    assert figures["tables"] == stored["tables"] and figures["binder_gaps"] == stored["binder_gaps"]
    assert figures["previous"]["source"] == "approved" and figures["previous"]["number"] == evaluations["before"].number
    assert indicators.missing_for(director) == []


@pytest.mark.parametrize("missing, gone, words", [
    (Module.INCIDENTS, "incidents_recorded", "Device incidents (needs Incidents View)"),
    (Module.CONTRACTS, "repair_spend", "Cost (needs Contracts View)"),
    (Module.USERS, "staff_lapse_work", "Staff and vendors: Work completed without a credential for the device (needs Users and access View)"),
])
def test_the_figures_are_cut_to_the_reader(client, tenant, evaluations, missing, gone, words):
    reader = signed_in(client, custom_user(tenant, "reader", {**EVERY_VIEW, missing: Level.NONE}))
    figures = ok(client.get(f"{API}{evaluations['current'].id}/"))["figures"]
    keys = {i["key"] for i in figures["indicators"]}
    assert gone not in keys and "pm_high" in keys and words in figures["left_out"] and figures["complete"] is False
    assert gone not in figures["previous"]["values"]
    assert (figures["met"], figures["judged"]) == indicators.tally(figures["indicators"])  # counts only what is shown
    assert figures == {**indicators.cut(evaluations["current"].snapshot.data, reader), "taken_on": figures["taken_on"],
                       "taken_by_name": None, "complete": False, "met": figures["met"], "judged": figures["judged"]}


def test_the_analyst_reads_it_without_the_staff_figures(client, make_user, evaluations):
    signed_in(client, make_user("analyst"))  # Evaluations View, Reports Full, no Users and access
    data = ok(client.get(f"{API}{evaluations['current'].number}/"))
    assert data["text"][0]["text"] == "Steady year; see the figures."
    assert "staff_lapse_work" not in {i["key"] for i in data["figures"]["indicators"]} and "staff" not in data["figures"]["binder_gaps"]


def test_without_reports_view_the_text_but_no_figures(client, tenant, evaluations):
    signed_in(client, custom_user(tenant, "no-reports", {**EVERY_VIEW, Module.REPORTS: Level.NONE}))
    data = ok(client.get(f"{API}{evaluations['current'].id}/"))
    assert data["text"][0]["text"] == "Steady year; see the figures." and data["objectives"]["review"]
    f = data["figures"]
    assert f["indicators"] == [] and f["tables"] == {} and f["binder_gaps"] == {} and f["previous"]["values"] == {} and f["settings"] == {}
    assert f["left_out"] == ["Every figure (needs Reports View)"] and (f["met"], f["judged"]) == (0, 0)
    assert ok(client.get(API))["count"] == 2


# --- doors ----------------------------------------------------------------------------------------------------------------------

def test_without_program_evaluation_view_it_is_refused_in_words(client, make_user, evaluations):
    signed_in(client, make_user("technician"))  # Program evaluation None
    for url in (API, f"{API}{evaluations['current'].id}/", f"{API}{evaluations['current'].number}/"):
        r = client.get(url)
        assert r.status_code == 403 and r.json()["detail"] == views_evaluations.VIEW_REFUSAL, url


@pytest.mark.parametrize("who, words", [("vendor", views_evaluations.VIEW_REFUSAL), ("requester", views_evaluations.VIEW_REFUSAL),
                                         ("scoped-full", "part of this facility")])
def test_scoped_users_are_refused_whatever_their_levels(client, make_user, tenant, evaluations, who, words):
    if who == "scoped-full":  # a custom scoped role with Full everywhere, Program evaluation included
        user = custom_user(tenant, "scoped-full", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="Philips")
    else:
        user = make_user(who)
    signed_in(client, user)
    for url in (API, f"{API}{evaluations['current'].id}/", f"{API}{evaluations['before'].number}/"):
        r = client.get(url)
        assert r.status_code == 403 and words in r.json()["detail"], url


def test_it_is_read_only(client, make_user, evaluations):
    one = f"{API}{evaluations['current'].id}/"
    signed_in(client, make_user("director"))
    assert client.post(API, {"period_start": "2025-01-01"}, content_type="application/json").status_code == 405
    assert client.patch(one, {"summary": "Changed"}, content_type="application/json").status_code == 405
    assert client.put(one, {"summary": "Changed"}, content_type="application/json").status_code == 405
    assert client.delete(one).status_code == 405
    signed_in(client, make_user("analyst"))  # View only: refused at the level, in words
    for r in (client.post(API, {}, content_type="application/json"), client.patch(one, {}, content_type="application/json"),
              client.delete(one)):
        assert r.status_code == 403 and r.json()["detail"] == views_evaluations.WRITE_REFUSAL
    current = ProgramEvaluation.objects.get(pk=evaluations["current"].pk)
    assert (current.summary, current.status, ProgramEvaluation.objects.count()) == ("Steady year; see the figures.", Status.DRAFT, 2)


def test_tokens_read_their_own_facility_only(client, make_user, tenant, other_tenant, evaluations, today):
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        theirs = draft(today=today)
    mine, vendor = Token.objects.create(user=make_user("director")), Token.objects.create(user=make_user("vendor"))
    other = Token.objects.create(user=make_user("director", other_tenant))
    client.logout()
    listed = ok(client.get(API, HTTP_AUTHORIZATION=f"Token {mine.key}"))
    assert {e["id"] for e in listed["results"]} == {str(evaluations["before"].id), str(evaluations["current"].id)}
    assert [e["id"] for e in ok(client.get(API, HTTP_AUTHORIZATION=f"Token {other.key}"))["results"]] == [str(theirs.id)]
    assert ok(client.get(f"{API}{theirs.id}/", HTTP_AUTHORIZATION=f"Token {other.key}"))["url"].endswith("?facility=other")
    assert client.get(f"{API}{theirs.id}/", HTTP_AUTHORIZATION=f"Token {mine.key}").status_code == 404
    assert client.get(API, HTTP_AUTHORIZATION=f"Token {vendor.key}").status_code == 403


@needs_postgres
def test_the_api_under_the_policies(client, make_user, evaluations, other_tenant, today):
    with tenant_context(other_tenant):
        theirs = draft(today=today)
    signed_in(client, make_user("director"))
    as_app_role()
    assert {e["id"] for e in ok(client.get(API))["results"]} == {str(evaluations["before"].id), str(evaluations["current"].id)}
    data = ok(client.get(f"{API}{evaluations['current'].number}/"))
    assert data["figures"]["left_out"] == [] and len(data["figures"]["indicators"]) == len(indicators.INDICATORS)
    assert data["objectives"]["review"][0]["carried"] is True
    assert client.get(f"{API}{theirs.id}/").status_code == 404
