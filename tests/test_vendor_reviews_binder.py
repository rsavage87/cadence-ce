"""
Slice 34, wave 2B: the review of each contracted service vendor in the survey binder's "Contracted service and vendor staff" section
(apps/reports/survey/vendors.py).

The vendors table ends with the vendor's last review (the latest signed-off review naming any of its names by key, by the 12 months it
covers): its number, linked for Evaluations View only (the VENDOR_REVIEW link column: the page, the print), and the day it was signed
off; never its ratings, decision, figures, or summary. Two figures: the vendors under contract at the period's end, and those of them
with a signed-off review. CHECKs, never a GAP, judged on the binder's today: a vendor under contract at the period's end whose next
review is overdue (the 12 months after its last one, and 90 days: vendor_reviews' own rule); once a review is signed off, one naming the
vendors under contract at the period's end with none (linking to the Reviews tab's vendors not reviewed); a corrective action plan past
its due day with no result. Drafts, withdrawn, and discarded reviews never count; vendors with jobs but no contract are never checked; a
fixed number of queries; another facility's reviews never read; under row-level security as the runtime role.

The figures are vendor_figures.snapshot's (tests/test_vendor_figures.py): stood in for here, since the section never reads them.
"""
import itertools
from datetime import date, timedelta

import pytest
from csvutil import csv_rows
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of
from vendor_review_days import ended

from apps.accounts.models import Level, Module, Role, User
from apps.contracts.models import Contract
from apps.evaluations import indicators
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import CRITERIA_FIELDS, Decision, Overall, Rating, ReviewStatus, VendorReview
from apps.reports import survey
from apps.reports.survey import CHECK, GAP, VENDOR_REVIEW, vendors
from apps.tenants.context import tenant_context
from apps.vendors.rules import fmt
from apps.web import views_survey
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

DAY = timedelta(days=1)
SECRET = "Zq7Canary summary"  # a review's summary: never in the binder
_refs = itertools.count(1)
READY = {**{f: Rating.MET for f in CRITERIA_FIELDS}, "overall": Overall.MEETS, "decision": Decision.RENEW, "approver_name": "Pat Lee",
         "approver_title": "VP, Support Services"}


@pytest.fixture(autouse=True)
def stand_in_figures(monkeypatch):
    """The review's figures stood in for (the binder never reads them): the services take them through the module."""
    from apps.evaluations import vendor_figures

    monkeypatch.setattr(vendor_figures, "snapshot", lambda keys, start, end, *, today: {"schema": 1, "vendor_keys": list(keys)})


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def contract(vendor, today, *, start=None, end=None):
    """A contract of `vendor` in force from 900 days ago to 100 days ahead unless said otherwise."""
    return Contract.objects.create(reference=f"SC-{next(_refs):04d}", vendor=vendor, start_on=start or today - 900 * DAY,
                                   end_on=end or today + 100 * DAY)


def signed(names, period_end, *, today, signed_on=None, **over):
    """A review of `names` for the 12 months ending `period_end` (the day before when that is Feb 28 of a leap year: vendor_review_days),
    every field the sign-off needs, signed off on `signed_on`."""
    r = vr.create_review(vendor_names=list(names), period_end=ended(period_end), by=None, today=today)
    r = vr.update_review(r, by=None, **{**READY, **over})
    return vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=signed_on or today, by=None, today=today)


def job(asset, vendor, done):
    """A vendor job completed on `done` (a vendor with jobs but no contract)."""
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Broken", opened_on=done - 3 * DAY)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done, vendor_service=True, vendor_name=vendor)
    return wo


def build(today, start=None, end=None, on=None):
    """The section for start..end (the last 200 days by default), judged on `on` (today by default)."""
    return vendors.build(period(start or today - 200 * DAY, end or today, on or today), None)


def row(section, name) -> dict:
    rows = {r[0]: r for r in rows_of(section, "vendors")}
    return dict(zip(vendors.VENDOR_COLUMNS, rows[name]))


def last_review(section, name) -> tuple:
    r = row(section, name)
    return r["Last review"], r["Last review signed off"]


def figures(section) -> dict:
    return {f.label: f.value for f in section.figures}


def review_checks(section) -> list:
    """The section's review checks (the slice 31 items left out)."""
    return [g for g in gaps_of(section, CHECK) if "review" in g.text]


def custom_user(tenant, slug, levels) -> User:
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


# --- the last review column ---------------------------------------------------------------------------------------------------------

def test_the_last_review_is_the_latest_signed_off_one_naming_the_vendor(today):
    contract("Philips", today)
    contract("Philips Healthcare", today)
    contract("Steris", today)
    first_end = ended(today - 400 * DAY)
    older = signed(["Philips"], first_end, today=today, signed_on=first_end + 20 * DAY)
    newer = signed(["Philips field service", "Philips Healthcare"], vr.next_period_end(first_end), today=today)  # "philips" is Philips's key
    s = build(today)
    assert vendors.VENDOR_COLUMNS[-2:] == ["Last review", "Last review signed off"] and vendors.LAST_REVIEW == 10
    assert last_review(s, "Philips") == last_review(s, "Philips Healthcare") == (newer.number, today)  # every name it gives, by key
    assert last_review(s, "Steris") == ("", None)
    vr.withdraw(newer, by=None)  # withdrawn: no longer its last review
    s = build(today)
    assert last_review(s, "Philips") == (older.number, first_end + 20 * DAY) and last_review(s, "Philips Healthcare") == ("", None)
    draft = vr.create_review(vendor_names=["Philips Healthcare"], period_end=vr.next_period_end(first_end), by=None, today=today)
    assert last_review(build(today), "Philips Healthcare") == ("", None)  # a draft never is
    vr.discard(draft, by=None)
    assert last_review(build(today), "Philips Healthcare") == ("", None)
    assert figures(build(today))["Vendors under contract with a signed-off review"] == 1


def test_never_its_ratings_decision_figures_or_summary(today, pump):
    contract("Philips", today)
    contract("Steris", today)
    signed(["Philips"], today - 40 * DAY, today=today, overall=Overall.DOES_NOT_MEET, decision=Decision.REPLACE, summary=SECRET)
    signed(["Steris"], today - 40 * DAY, today=today, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN,
           plan_due_on=today + 30 * DAY, rating_invoices=Rating.NOT_MET)
    s = build(today)
    rows = [c for t in s.tables for r in rows_of(s, t.key) for c in r]
    shown = " ".join([*(g.text + g.record for g in s.gaps), *(f"{f.label} {f.value} {f.hint}" for f in s.figures), *(str(c) for c in rows)])
    assert SECRET not in " ".join([shown, *s.notes, s.covers])
    for words in [*Overall.labels, *Decision.labels, *Rating.labels, "replace or end", "renew", "meets"]:
        assert words.lower() not in shown.lower(), words


def test_the_number_links_to_the_review_for_evaluations_view_only(client, today, ctx, make_user):
    contract("Philips", today)
    r = signed(["Philips"], today - 40 * DAY, today=today)
    s = build(today)
    table = s.table("vendors")
    assert table.links == {vendors.LAST_REVIEW: VENDOR_REVIEW}
    url = reverse("web:vendor_review", args=[r.number])
    director, technician = make_user("director"), make_user("technician")  # the technician: Contracts View, Evaluations None
    philips = next(x for x in table.rows() if x[0] == "Philips")
    cells = views_survey.cells(philips, table.links, views_survey.linkable(director))
    assert (cells[10]["text"], cells[10]["url"], cells[10]["drawer"], cells[11]["text"]) == (r.number, url, False, fmt(today))
    assert views_survey.cells(philips, table.links, views_survey.linkable(technician))[10]["url"] == ""
    assert views_survey.cells(["x"] * 10 + ["", None], table.links, views_survey.linkable(director))[10]["url"] == ""  # not reviewed
    client.force_login(director)
    assert f'<a class="link" href="{url}">{r.number}</a>' in client.get("/reports/survey/").content.decode()  # a page, never a drawer
    assert f'<a href="{url}?facility=riverside">{r.number}</a>' in client.get("/print/survey/").content.decode()
    client.force_login(technician)
    body = client.get("/reports/survey/").content.decode()
    assert r.number in body and url not in body
    csv = csv_rows(client.get("/reports/survey/vendors/vendors.csv"))
    assert csv[0][-2:] == ["Last review", "Last review signed off"] and csv[1][-2:] == [r.number, today.isoformat()]


# --- the checks -----------------------------------------------------------------------------------------------------------------

def test_no_review_check_before_the_first_sign_off(today):
    contract("Philips", today)
    contract("Steris", today)
    draft = vr.create_review(vendor_names=["Philips"], period_end=ended(today - 40 * DAY), by=None, today=today)
    s = build(today)
    assert review_checks(s) == [] and s.gaps == []
    assert (figures(s)["Vendors under contract"], figures(s)["Vendors under contract with a signed-off review"]) == (2, 0)
    vr.discard(draft, by=None)
    withdrawn = signed(["Philips"], today - 40 * DAY, today=today)
    vr.withdraw(withdrawn, by=None)
    assert build(today).gaps == []  # a withdrawn review is not a signed-off one


def test_once_one_is_signed_off_one_check_names_the_vendors_under_contract_with_none(today, ctx, pump, make_user):
    contract("Philips", today)
    contract("Steris", today)
    contract("Hamilton Medical", today)
    contract("Hamilton Medical", today, start=today - 300 * DAY, end=today - 10 * DAY)  # a second one, ended: still under contract
    contract("GE HealthCare", today, start=today - 300 * DAY, end=today - 10 * DAY)  # ended before the period's end
    contract("Siemens Healthineers", today, start=today + 5 * DAY, end=today + 400 * DAY)  # not started yet
    job(pump, "BD field service", today - 20 * DAY)  # jobs but no contract: never checked
    signed(["Philips"], today - 40 * DAY, today=today)
    s = build(today)
    (check,) = review_checks(s)
    assert (check.kind, check.text, check.url, check.record) == (
        CHECK, f"2 vendors with a contract in force on {fmt(today)} have no signed-off review: Hamilton Medical and Steris. Review each in "
               "Contracts, Reviews, or be ready to explain.", "/contracts/reviews/?show=not_reviewed", "Vendor reviews")
    assert (figures(s)["Vendors under contract"], figures(s)["Vendors under contract with a signed-off review"]) == (3, 1)
    assert survey.gap_links(make_user("director"))(check) == "/contracts/reviews/?show=not_reviewed"
    assert survey.gap_links(make_user("technician"))(check) == ""  # the Reviews tab needs Evaluations View
    # a binder of an earlier period: the vendors under contract at its end, judged today
    earlier = build(today, today - 300 * DAY, today - 20 * DAY)
    assert review_checks(earlier)[0].text.startswith(f"3 vendors with a contract in force on {fmt(today - 20 * DAY)} have no signed-off review: "
                                                     "GE HealthCare, Hamilton Medical, and Steris.")
    assert [g.record for g in gaps_of(s, GAP)] == ["BD field service"]  # slice 31's: its job has no evidence on file; a review is never a GAP


def test_the_check_names_five_vendors_then_how_many_more(today):
    for name in ("Philips", "Steris", "Hamilton Medical", "GE HealthCare", "Siemens Healthineers", "Fresenius", "Mindray", "Zoll"):
        contract(name, today)
    signed(["Philips"], today - 40 * DAY, today=today)
    assert review_checks(build(today))[0].text == (
        f"7 vendors with a contract in force on {fmt(today)} have no signed-off review: Fresenius, GE HealthCare, Hamilton Medical, Mindray, "
        "Siemens Healthineers, and 2 more. Review each in Contracts, Reviews, or be ready to explain.")
    for name in ("Fresenius", "GE HealthCare", "Hamilton Medical", "Mindray", "Siemens Healthineers", "Zoll"):
        signed([name], today - 40 * DAY, today=today)
    assert review_checks(build(today))[0].text == (
        f"1 vendor with a contract in force on {fmt(today)} has no signed-off review: Steris. Review it in Contracts, Reviews, or be ready "
        "to explain.")
    signed(["Steris"], today - 40 * DAY, today=today)
    assert review_checks(build(today)) == []


def test_a_vendor_under_contract_whose_next_review_is_overdue(today, pump):
    contract("Philips", today)
    contract("Steris", today, start=today - 900 * DAY, end=today - 30 * DAY)  # ended: its review is not chased
    end = ended(today - 470 * DAY)
    r = signed(["Philips"], end, today=today, signed_on=end + 15 * DAY)
    signed(["Steris"], end, today=today, signed_on=end + 15 * DAY)
    job(pump, "BD field service", end - 20 * DAY)
    signed(["BD field service"], end, today=today, signed_on=end + 15 * DAY)  # jobs but no contract: never chased
    due = vendors.review_due(r)
    assert due == vr.next_period_end(end) + 90 * DAY < today
    (check,) = review_checks(build(today, on=today))
    assert (check.kind, check.text, check.url, check.record) == (
        CHECK, f"Philips: no signed-off review of the 12 months after {fmt(r.period_start)} to {fmt(end)} ({r.number}, the last), 90 days "
               f"after they ended on {fmt(vr.next_period_end(end))}. Review the vendor in Contracts, Reviews, or be ready to explain.",
        "/contracts/reviews/", "Vendor reviews")
    assert review_checks(build(today, today - 400 * DAY, due, on=due)) == []  # on the day it falls due: not yet overdue
    assert len(review_checks(build(today, today - 400 * DAY, due, on=due + DAY))) == 1
    # the next review signed off: nothing overdue, and the vendor has a review
    signed(["Philips"], vr.next_period_end(end), today=today)
    assert review_checks(build(today)) == []


def test_one_overdue_check_per_review_whatever_names_it_gives(today):
    contract("Philips", today)
    contract("Philips Healthcare", today)  # its own row in the table (another key), the same review
    end = ended(today - 470 * DAY)
    r = signed(["Philips Healthcare", "Philips"], end, today=today, signed_on=end + 15 * DAY)
    (check,) = review_checks(build(today))
    assert check.text.startswith(f"Philips and Philips Healthcare: no signed-off review of the 12 months after {fmt(r.period_start)} to "
                                 f"{fmt(end)} ({r.number}, the last)")
    assert last_review(build(today), "Philips") == last_review(build(today), "Philips Healthcare") == (r.number, end + 15 * DAY)


def test_a_corrective_action_plan_past_its_due_day_with_no_result(today, ctx, make_user):
    contract("Philips", today)
    contract("Steris", today)
    r = signed(["Philips"], today - 40 * DAY, today=today, signed_on=today - 30 * DAY, overall=Overall.PARTIALLY,
               decision=Decision.CORRECTIVE_PLAN, plan_due_on=today - DAY)
    other = signed(["Steris"], today - 40 * DAY, today=today, signed_on=today - 30 * DAY, overall=Overall.PARTIALLY,
                   decision=Decision.CORRECTIVE_PLAN, plan_due_on=today)  # due today: not past
    s = build(today)
    (check,) = review_checks(s)
    url = reverse("web:vendor_review", args=[r.number])
    assert (check.kind, check.text, check.url, check.record) == (
        CHECK, f"Philips: the corrective action plan of {r.number} was due on {fmt(today - DAY)} and no result is recorded. Record its "
               "result on the review, or be ready to explain.", url, r.number)
    assert survey.gap_links(make_user("analyst"))(check) == url and survey.gap_links(make_user("technician"))(check) == ""
    assert len(review_checks(build(today, on=today + DAY))) == 2  # tomorrow, Steris's too, in the order they fell due
    assert [g.record for g in review_checks(build(today, on=today + DAY))] == [r.number, other.number]
    vr.record_plan_result(r, result="met", closed_on=today, by=None, today=today)
    assert review_checks(build(today)) == []
    vr.record_plan_result(r, result="", closed_on=None, by=None, today=today)  # cleared: entered in error
    assert len(review_checks(build(today))) == 1
    vr.withdraw(r, by=None)  # a withdrawn review's plan is no longer followed here (Philips has no signed-off review now)
    assert [g.record for g in review_checks(build(today))] == ["Vendor reviews"]


def test_the_rules_are_vendor_reviews_own(today):
    assert vendors.DUE_AFTER_DAYS == vr.DUE_AFTER_DAYS
    for end in [date(2024, 2, 29), date(2025, 2, 28), date(2026, 1, 31), date(2026, 12, 31), today - DAY, *(today - n * 37 * DAY for n in range(20))]:
        assert vendors.next_period_end(end) == vr.next_period_end(end), end
    contract("Philips", today)
    end = ended(today - 470 * DAY)
    signed(["Philips"], end, today=today, signed_on=end + 15 * DAY)
    (philips,) = [r for r in vr.vendors_list(today) if r.key == "philips"]
    assert philips.overdue and len(review_checks(build(today))) == 1  # the Reviews tab says overdue too


# --- reading ---------------------------------------------------------------------------------------------------------------------

def test_a_fixed_number_of_queries_with_reviews(today, pump):
    def read() -> int:
        with CaptureQueriesContext(connection) as q:
            s = build(today)
            for t in s.tables:
                rows_of(s, t.key)
        return len(q)

    contract("Philips", today)
    signed(["Philips"], today - 40 * DAY, today=today)
    small = read()
    for i in range(12):
        name = f"Vendor {chr(65 + i)}"
        contract(name, today)
        job(pump, name, today - (i + 5) * DAY)
        signed([name], today - (40 + i) * DAY, today=today, decision=Decision.CORRECTIVE_PLAN, overall=Overall.PARTIALLY,
               signed_on=today - (30 - i) * DAY, plan_due_on=today - (29 - i) * DAY if i < 6 else today + DAY)
    s = build(today)
    assert len(review_checks(s)) == 6
    assert read() == small <= 7


def test_another_facilitys_reviews_stay_there(today, other_tenant):
    contract("Philips", today)
    contract("Steris", today)
    with tenant_context(other_tenant):
        contract("Philips", today)
        theirs = signed(["Philips"], today - 40 * DAY, today=today)
        assert last_review(build(today), "Philips") == (theirs.number, today)
    s = build(today)
    assert last_review(s, "Philips") == ("", None) and s.gaps == []
    assert VendorReview.objects.count() == 0 and VendorReview.unscoped.filter(status=ReviewStatus.APPROVED).count() == 1  # unscoped: both


def test_the_program_evaluation_counts_the_checks(today):
    contract("Philips", today)
    contract("Steris", today)
    signed(["Philips"], today - 40 * DAY, today=today)
    assert indicators.binder_gaps(period(today - 200 * DAY, today, today))["vendors"] == {"gap": 0, "finding": 0, "check": 1}


def test_the_page_and_the_api_show_the_checks(client, today, make_user):
    contract("Philips", today)
    contract("Steris", today)
    signed(["Philips"], today - 40 * DAY, today=today)
    client.force_login(make_user("director"))
    body = client.get("/reports/survey/").content.decode()
    assert "1 vendor with a contract in force on" in body and "/contracts/reviews/?show=not_reviewed" in body
    data = client.get("/api/v1/survey/vendors/").json()
    assert [g["url"] for g in data["gaps"]] == ["/contracts/reviews/?show=not_reviewed&facility=riverside"]
    assert data["tables"][0]["columns"][-2:] == ["Last review", "Last review signed off"]


@needs_postgres
def test_the_reviews_in_the_section_under_the_policies(client, today, make_user):
    contract("Philips", today)
    contract("Steris", today)
    r = signed(["Philips"], today - 40 * DAY, today=today, signed_on=today - 30 * DAY, overall=Overall.PARTIALLY,
               decision=Decision.CORRECTIVE_PLAN, plan_due_on=today - DAY)
    client.force_login(make_user("director"))
    as_app_role()
    s = build(today)
    assert last_review(s, "Philips") == (r.number, today - 30 * DAY)
    assert [g.record for g in review_checks(s)] == ["Vendor reviews", r.number]
    data = client.get("/api/v1/survey/vendors/").json()
    assert [g["record"] for g in data["gaps"]] == ["Vendor reviews", r.number]
    assert client.get("/reports/survey/vendors/vendors.csv").status_code == 200
