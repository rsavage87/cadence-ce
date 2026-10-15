"""
Slice 34, wave 2B: the review of each contracted service vendor over the API (apps/api/views_vendor_reviews.py,
serializers_vendor_reviews.py), read-only.

GET /api/v1/vendor-reviews/ lists the reviews, the latest 12 months first, with no figures and no summary (?status=, ?vendor= any
spelling of a vendor's key, ?search=, ?ordering=); GET .../<id or number>/ one review with its ratings, the vendor's reported figures,
the summary, the sign-off, the previous signed-off review of the vendor, the committee reports, and the figures through their gate,
all or nothing (vendor_figures.cut). Evaluations View and Contracts View (the Reviews tab's doors); scoped users never (a custom scoped
role with Full everywhere neither: a vendor's account never reads its review); writes refused; a fixed number of queries on the list;
tokens read their own facility only; under row-level security on PostgreSQL. The figures are vendor_figures' own, counted for real.
"""
import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import views_vendor_reviews
from apps.api.views_vendor_reviews import VendorReviewViewSet
from apps.contracts.models import Contract
from apps.evaluations import vendor_figures
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import (
    CRITERIA,
    CRITERIA_FIELDS,
    Decision,
    Overall,
    PlanResult,
    Rating,
    ReportBody,
    ReviewStatus,
    StatedInContract,
    VendorReview,
)
from apps.tenants.context import tenant_context
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

API = "/api/v1/vendor-reviews/"
DAY = timedelta(days=1)
SUMMARY = "Zq7Canary: timely service, reports on file."
LIST_FIELDS = {"id", "number", "vendor", "vendor_names", "period_start", "period_end", "period_label", "status", "status_label", "overall",
               "overall_label", "decision", "decision_label", "plan_due_on", "plan_result", "plan_result_label", "plan_closed_on", "signed_on",
               "url"}
READY = {**{f: Rating.MET for f in CRITERIA_FIELDS}, "overall": Overall.MEETS, "decision": Decision.RENEW, "approver_name": "Pat Lee",
         "approver_title": "VP, Support Services"}
EVERY_VIEW = {m: Level.VIEW for m in Module.values}
_refs = itertools.count(1)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def contract(vendor, today):
    return Contract.objects.create(reference=f"SC-{next(_refs):04d}", vendor=vendor, start_on=today - 900 * DAY, end_on=today + 100 * DAY)


def job(asset, vendor, done):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Broken", opened_on=done - 3 * DAY)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done, vendor_service=True, vendor_name=vendor)
    return wo


def signed(names, period_end, *, today, by=None, signed_on=None, **over):
    r = vr.create_review(vendor_names=list(names), period_end=period_end, by=by, today=today)
    r = vr.update_review(r, by=by, **{**READY, **over})
    return vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=signed_on or today, by=by, today=today)


@pytest.fixture
def reviews(ctx, today, pump, make_user):
    """Philips last year, signed off by the CE manager with a corrective action plan since met and reported to the governing body; this
    year's Philips draft (two names), prefilled from it, with its summary and the vendor's reported figures; and Steris signed off."""
    manager = make_user("manager")
    for vendor in ("Philips", "Philips Healthcare", "Steris"):
        contract(vendor, today)
    job(pump, "Philips", today - 60 * DAY)
    first_end = today - 400 * DAY
    before = signed(["Philips"], first_end, today=today, by=manager, signed_on=first_end + 20 * DAY, overall=Overall.PARTIALLY,
                    decision=Decision.CORRECTIVE_PLAN, plan_due_on=first_end + 60 * DAY, rating_invoices=Rating.NOT_MET)
    vr.record_plan_result(before, result=PlanResult.MET, closed_on=first_end + 50 * DAY, by=manager, today=today)
    vr.record_report([before], body=ReportBody.GOVERNING, meeting_on=first_end + 30 * DAY, minutes_reference="GB-2025-03", by=manager,
                     today=today)
    current = vr.create_review(vendor_names=["Philips", "Philips Healthcare"], period_end=vr.next_period_end(first_end), by=None, today=today)
    current = vr.update_review(current, by=None, summary=SUMMARY, stated_in_contract=StatedInContract.PARTLY, uptime_target_pct="98",
                               uptime_reported_pct="98.4", uptime_lowest_pct="91.25", systems_below=1, systems_total=5,
                               vendor_report_reference="UPT-2026-Q4")
    steris = signed(["Steris"], today - 40 * DAY, today=today)
    return {"before": VendorReview.objects.get(pk=before.pk), "current": VendorReview.objects.get(pk=current.pk),
            "steris": VendorReview.objects.get(pk=steris.pk), "manager": manager}


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


def newest_first(*reviews) -> list:
    return [r.number for r in sorted(reviews, key=lambda r: (r.period_end, r.number), reverse=True)]


# --- the list -------------------------------------------------------------------------------------------------------------------

def test_the_list_has_no_figures_and_no_summary(client, make_user, reviews, today):
    signed_in(client, make_user("director"))
    data = ok(client.get(API))
    before, current, steris = reviews["before"], reviews["current"], reviews["steris"]
    assert data["count"] == 3 and [r["number"] for r in data["results"]] == newest_first(before, current, steris)
    for item in data["results"]:
        assert set(item) == LIST_FIELDS
    body = client.get(API).content.decode()
    assert "Zq7Canary" not in body and "jobs_completed" not in body and "UPT-2026-Q4" not in body and "Pat Lee" not in body
    listed = {r["number"]: r for r in data["results"]}
    old = listed[before.number]
    assert (old["vendor"], old["vendor_names"], old["status"], old["status_label"], old["overall"], old["overall_label"], old["decision"],
            old["decision_label"]) == ("Philips", ["Philips"], "approved", "Signed off", "partially", "Partially meets expectations",
                                       "corrective_plan", "Corrective action plan")
    assert (old["plan_due_on"], old["plan_result"], old["plan_result_label"], old["plan_closed_on"], old["signed_on"]) == (
        (before.period_end + 60 * DAY).isoformat(), "met", "Plan met", (before.period_end + 50 * DAY).isoformat(),
        (before.period_end + 20 * DAY).isoformat())
    assert old["period_label"] == vr.period_label(before.period_start, before.period_end)
    assert old["url"] == f"/contracts/reviews/{before.number}/?facility=riverside"
    new = listed[current.number]
    assert (new["vendor_names"], new["status_label"], new["overall"], new["overall_label"], new["decision_label"], new["plan_result_label"],
            new["signed_on"]) == (["Philips", "Philips Healthcare"], "Draft", "", "", "", "", None)  # prefilled: ratings, never these
    for url in (API, f"{API}{current.number}/"):  # the browsable API renders inside the facility too
        assert client.get(url, HTTP_ACCEPT="text/html").status_code == 200, url


def test_the_list_filters(client, make_user, reviews):
    signed_in(client, make_user("director"))
    before, current, steris = reviews["before"], reviews["current"], reviews["steris"]

    def numbers(query):
        return [r["number"] for r in ok(client.get(f"{API}?{query}"))["results"]]

    assert numbers("status=approved") == newest_first(before, steris) and numbers("status=draft") == [current.number]
    assert numbers("status=withdrawn") == [] and numbers("status=all") == newest_first(before, current, steris)
    r = client.get(f"{API}?status=everything")
    assert r.status_code == 400 and "status" in r.json()
    assert numbers("vendor=PHILIPS%20field%20service") == newest_first(before, current)  # any spelling of the key
    assert numbers("vendor=philips%20healthcare") == [current.number] and numbers("vendor=Steris") == [steris.number]
    assert numbers("vendor=Phillips") == [] and numbers("vendor=steris&status=draft") == []
    r = client.get(f"{API}?vendor=%20%20")
    assert r.status_code == 400 and "vendor" in r.json()
    assert numbers("ordering=period_end") == list(reversed(newest_first(before, current, steris)))
    assert numbers(f"search={steris.number.lower()}") == [steris.number]


def test_a_fixed_number_of_queries_on_the_list(client, make_user, ctx, today, pump):
    signed_in(client, make_user("director"))

    def queries():
        with CaptureQueriesContext(connection) as q:
            assert client.get(API).status_code == 200
        return len(q.captured_queries)

    contract("Philips", today)
    signed(["Philips"], today - 40 * DAY, today=today)
    few = queries()
    for name in ("Steris", "Zoll", "Mindray", "Fresenius"):
        contract(name, today)
        r = signed([name], today - 40 * DAY, today=today)
        vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, by=None, today=today)
    assert ok(client.get(API))["count"] == 5
    assert queries() == few


# --- one review -------------------------------------------------------------------------------------------------------------------

def test_one_review_by_id_or_number(client, make_user, reviews, today, other_tenant):
    signed_in(client, make_user("director"))
    before = reviews["before"]
    assert ok(client.get(f"{API}{before.id}/"))["number"] == before.number
    assert ok(client.get(f"{API}{before.number.lower()}/"))["id"] == str(before.id)  # any letter case
    r = client.get(f"{API}VR-99-0001/")
    assert r.status_code == 404 and r.json()["detail"] == views_vendor_reviews.NOT_FOUND
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        contract("Philips", today)
        theirs = signed(["Philips"], before.period_end, today=today)
    assert theirs.number == before.number  # numbers repeat across facilities
    assert client.get(f"{API}{theirs.id}/").status_code == 404  # another facility's, by id
    assert ok(client.get(f"{API}{theirs.number}/"))["id"] == str(before.id)  # by number: this facility's own


def test_the_ratings_sign_off_previous_and_reports(client, make_user, reviews, today):
    signed_in(client, make_user("director"))
    before, current, manager = reviews["before"], reviews["current"], reviews["manager"]
    data = ok(client.get(f"{API}{before.number}/"))
    assert [r["field"] for r in data["ratings"]] == list(CRITERIA_FIELDS)
    assert data["ratings"][0] == {"field": "rating_timeliness", "label": "Timeliness", "covers": CRITERIA[0][2], "rating": "met",
                                  "rating_label": "Met"}
    assert {r["field"]: r["rating_label"] for r in data["ratings"]}["rating_invoices"] == "Not met"
    assert (data["approver_name"], data["approver_title"], data["signed_by_name"], data["created_by_name"]) == (
        "Pat Lee", "VP, Support Services", str(manager), str(manager))
    assert data["previous"] is None and data["print_url"] == f"/contracts/reviews/{before.number}/print/?facility=riverside"
    [report] = data["reports"]
    assert {k: v for k, v in report.items() if k != "id"} == {"body": "governing", "body_label": "Governing body",
                                                              "meeting_on": (before.period_end + 30 * DAY).isoformat(),
                                                              "minutes_reference": "GB-2025-03", "recorded_by_name": str(manager)}
    now = ok(client.get(f"{API}{current.id}/"))
    assert now["summary"] == SUMMARY and (now["stated_in_contract"], now["stated_in_contract_label"]) == ("partly", "Partly")
    assert (now["uptime_target_pct"], now["uptime_reported_pct"], now["uptime_lowest_pct"], now["systems_below"], now["systems_total"],
            now["response_target_pct"], now["vendor_report_reference"]) == ("98.00", "98.40", "91.25", 1, 5, None, "UPT-2026-Q4")
    assert {r["field"]: r["rating"] for r in now["ratings"]}["rating_invoices"] == "not_met"  # carried from the last one
    assert now["previous"] == {"id": str(before.id), "number": before.number,
                               "period_label": vr.period_label(before.period_start, before.period_end), "overall": "partially",
                               "overall_label": "Partially meets expectations", "decision": "corrective_plan",
                               "decision_label": "Corrective action plan", "plan_result": "met", "plan_result_label": "Plan met",
                               "url": f"/contracts/reviews/{before.number}/?facility=riverside"}
    assert (now["signed_on"], now["signed_by_name"], now["reports"], now["approver_name"]) == (None, None, [], "")


def test_the_director_sees_every_figure(client, make_user, reviews, today):
    signed_in(client, make_user("director"))
    current = reviews["current"]
    figures = ok(client.get(f"{API}{current.number}/"))["figures"]
    stored = current.snapshot.data
    assert figures == {**stored, "left_out": [], "taken_by_name": None}
    by_key = vendor_figures.by_key(figures)
    assert by_key["jobs_completed"]["value"] == 1 and by_key["contracts_in_period"]["value"] == 2 and figures["vendor_keys"] == [
        "philips", "philips healthcare"]
    assert ok(client.get(f"{API}{reviews['before'].id}/"))["figures"]["taken_by_name"] == str(reviews["manager"])


@pytest.mark.parametrize("missing", [Module.REPORTS, Module.WORKORDERS])
def test_the_figures_are_all_or_nothing(client, tenant, reviews, missing):
    reader = signed_in(client, custom_user(tenant, "reader", {**EVERY_VIEW, missing: Level.NONE}))
    data = ok(client.get(f"{API}{reviews['current'].id}/"))
    figures = data["figures"]
    assert figures == {**vendor_figures.cut(reviews["current"].snapshot.data, reader), "taken_by_name": None}
    assert figures["figures"] == [] and figures["contracts"] == [] and figures["names"] == [] and figures["repeats"] == []
    assert figures["left_out"] == [f"Every figure (needs {vendor_figures.needs_words()})"]
    assert (figures["schema"], figures["vendor_keys"]) == (1, ["philips", "philips healthcare"])
    assert data["summary"] == SUMMARY and ok(client.get(API))["count"] == 3  # the review itself is theirs to read


def test_the_analyst_reads_every_figure(client, make_user, reviews):
    signed_in(client, make_user("analyst"))  # Evaluations View, Contracts View, Reports Full, Work orders View
    figures = ok(client.get(f"{API}{reviews['steris'].number}/"))["figures"]
    assert figures["left_out"] == [] and figures["figures"]


# --- doors ----------------------------------------------------------------------------------------------------------------------

def test_without_evaluations_or_contracts_view_it_is_refused_in_words(client, make_user, tenant, reviews):
    one = reviews["current"]
    for user in (make_user("technician"),  # Contracts View, Evaluations None
                 custom_user(tenant, "no-contracts", {**EVERY_VIEW, Module.CONTRACTS: Level.NONE})):
        signed_in(client, user)
        for url in (API, f"{API}{one.id}/", f"{API}{one.number}/"):
            r = client.get(url)
            assert r.status_code == 403 and r.json()["detail"] == views_vendor_reviews.VIEW_REFUSAL, (user, url)


@pytest.mark.parametrize("who, words", [("vendor", views_vendor_reviews.VIEW_REFUSAL), ("requester", views_vendor_reviews.VIEW_REFUSAL),
                                         ("scoped-full", "part of this facility")])
def test_scoped_users_are_refused_whatever_their_levels(client, make_user, tenant, reviews, who, words):
    if who == "scoped-full":  # a vendor's account with Full everywhere, Evaluations and Contracts included: never its own review
        user = custom_user(tenant, "scoped-full", {m: Level.FULL for m in Module.values}, scope=DataScope.COMPANY, company="Philips")
    else:
        user = make_user(who)
    signed_in(client, user)
    for url in (API, f"{API}{reviews['before'].id}/", f"{API}{reviews['before'].number}/", f"{API}?vendor=Philips"):
        r = client.get(url)
        assert r.status_code == 403 and words in r.json()["detail"], url
    assert VendorReviewViewSet.scoped_actions == frozenset()


def test_it_is_read_only(client, make_user, tenant, reviews):
    one = f"{API}{reviews['current'].id}/"
    signed_in(client, make_user("director"))
    assert client.post(API, {"vendor_names": ["Philips"]}, content_type="application/json").status_code == 405
    assert client.patch(one, {"summary": "Changed"}, content_type="application/json").status_code == 405
    assert client.put(one, {"summary": "Changed"}, content_type="application/json").status_code == 405
    assert client.delete(one).status_code == 405
    for user in (make_user("analyst"),  # View only: refused at the level, in words
                 custom_user(tenant, "edit-no-contracts", {**EVERY_VIEW, Module.EVALUATIONS: Level.APPROVE, Module.CONTRACTS: Level.NONE})):
        signed_in(client, user)
        for r in (client.post(API, {}, content_type="application/json"), client.patch(one, {}, content_type="application/json"),
                  client.delete(one)):
            assert r.status_code == 403 and r.json()["detail"] == views_vendor_reviews.WRITE_REFUSAL, user
    current = VendorReview.objects.get(pk=reviews["current"].pk)
    assert (current.summary, current.status, VendorReview.objects.count()) == (SUMMARY, ReviewStatus.DRAFT, 3)


def test_tokens_read_their_own_facility_only(client, make_user, tenant, other_tenant, reviews, today):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        contract("Philips", today)
        theirs = signed(["Philips"], today - 40 * DAY, today=today)
    mine, vendor = Token.objects.create(user=make_user("director")), Token.objects.create(user=make_user("vendor"))
    other = Token.objects.create(user=make_user("director", other_tenant))
    client.logout()
    listed = ok(client.get(API, HTTP_AUTHORIZATION=f"Token {mine.key}"))
    assert {r["id"] for r in listed["results"]} == {str(reviews[k].id) for k in ("before", "current", "steris")}
    assert [r["id"] for r in ok(client.get(API, HTTP_AUTHORIZATION=f"Token {other.key}"))["results"]] == [str(theirs.id)]
    assert ok(client.get(f"{API}{theirs.id}/", HTTP_AUTHORIZATION=f"Token {other.key}"))["url"].endswith("?facility=other")
    assert client.get(f"{API}{theirs.id}/", HTTP_AUTHORIZATION=f"Token {mine.key}").status_code == 404
    assert client.get(API, HTTP_AUTHORIZATION=f"Token {vendor.key}").status_code == 403


def test_the_reported_figures_keep_two_decimals(client, make_user, reviews):
    signed_in(client, make_user("director"))
    vr.update_review(reviews["current"], by=None, response_target_pct=Decimal("95"), response_reported_pct="97.5")
    data = ok(client.get(f"{API}{reviews['current'].number}/"))
    assert (data["response_target_pct"], data["response_reported_pct"]) == ("95.00", "97.50")


@needs_postgres
def test_the_api_under_the_policies(client, make_user, reviews, other_tenant, today):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        contract("Philips", today)
        theirs = signed(["Philips"], today - 40 * DAY, today=today)
    signed_in(client, make_user("director"))
    as_app_role()
    assert {r["id"] for r in ok(client.get(API))["results"]} == {str(reviews[k].id) for k in ("before", "current", "steris")}
    data = ok(client.get(f"{API}{reviews['current'].number}/"))
    assert data["figures"]["left_out"] == [] and data["figures"]["figures"] and data["previous"]["number"] == reviews["before"].number
    assert ok(client.get(f"{API}{reviews['before'].id}/"))["reports"][0]["minutes_reference"] == "GB-2025-03"
    assert client.get(f"{API}{theirs.id}/").status_code == 404
    assert [r["number"] for r in ok(client.get(f"{API}?vendor=philips"))["results"]] == newest_first(reviews["before"], reviews["current"])
