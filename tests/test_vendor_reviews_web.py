"""
Slice 34, wave 2 A: the Contracts screen's Reviews tab and the review's page (apps/web/views_vendor_reviews.py, forms_vendor_reviews.py,
web/vendor_reviews.html, web/_vendor_review_*.html), both prints (web/print_vendor_review.html, web/print_vendor_reviews.html), the batch
committee report, and the contract drawer's review line (views_contracts._drawer_context).

Who: the tab and every review page need Evaluations View with Contracts View (a technician with Contracts View and no Evaluations, a role
with Evaluations and no Contracts, a vendor's account, a requester: refused); Edit drafts, refreshes, writes, discards, and records a
plan's result; Approve signs off, withdraws, and records, corrects, and removes committee reports. The figures are all or nothing behind
their gate (Reports View and Work orders View too). Each action at its level, refusals in words with what was typed kept, the prints'
stamps and their figures gate, the governing body's summary (no summary text, no figures), the drawer line per level, the page whole and
each part, History, junk input never a 500, another facility's review a 404, and on PostgreSQL the screens under the policies. The
services' own rules are tests/test_vendor_reviews.py; the figures' tests/test_vendor_figures.py.
"""
import itertools
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role, User, create_default_roles
from apps.contracts import services as contract_services
from apps.contracts.models import Contract
from apps.equipment.models import Asset, Department, DeviceModel
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import (
    CRITERIA_FIELDS,
    REVIEW_PRIVACY_WARNING,
    Decision,
    Overall,
    PlanResult,
    Rating,
    ReportBody,
    ReviewStatus,
    VendorReview,
    VendorReviewReport,
)
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)
PHILIPS, TECHCARE = "Philips", "TechCare Biomedical"
BASE = "/contracts/reviews/"
_refs = itertools.count(1)


def part(target):
    return {**HX, "HTTP_HX_TARGET": target}


def html(r) -> str:
    return r.content.decode()


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def aware(d, hour=12):
    return timezone.make_aware(datetime.combine(d, time(hour)))


def contract(vendor, start_on, end_on, *, cost="12000.00", reference=None):
    """A contract through the services, its first history row dated before its term (entered at onboarding)."""
    c = contract_services.create_contract(reference=reference or f"SC-{next(_refs):04d}", vendor=vendor, start_on=start_on, end_on=end_on,
                                          annual_cost=Decimal(cost))
    row = Contract.history.filter(id=c.pk).order_by("-history_id").first()
    Contract.history.filter(history_id=row.history_id).update(history_date=aware(start_on - 10 * DAY, 9))
    return c


def job(asset, vendor, done):
    """A vendor service repair completed on `done`."""
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Broken", opened_on=done - 3 * DAY)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done, vendor_service=True, vendor_name=vendor)
    return wo


READY = {**{f: Rating.MET for f in CRITERIA_FIELDS}, "overall": Overall.MEETS, "decision": Decision.RENEW, "approver_name": "Pat Lee",
         "approver_title": "VP, Support Services"}


def signed(r, today, **over):
    r = vr.update_review(r, by=None, **{**READY, **over})
    return vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)


def url(r, *rest) -> str:
    return f"{BASE}{r.number}/" + "".join(f"{x}/" for x in rest)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    end = today.replace(day=1) - DAY
    return vr.period_start_of(end), end


@pytest.fixture
def world(ctx, span, today, pump):
    """Philips: a contract in force through the period and after today, and a vendor repair completed in the period. TechCare: a
    contract in force ending sooner, no jobs."""
    philips = contract(PHILIPS, span[0], today + 200 * DAY, reference="SC-PHIL")
    techcare = contract(TECHCARE, span[0], today + 100 * DAY, reference="SC-TECH")
    repair = job(pump, PHILIPS, span[0] + 40 * DAY)
    return {"philips": philips, "techcare": techcare, "repair": repair}


@pytest.fixture
def review(world, span, today):
    """A draft review of Philips for the 12 months ending at the end of last month, with its figures."""
    return vr.create_review(vendor_names=[PHILIPS], period_end=span[1], by=None, today=today)


@pytest.fixture
def users(make_user):
    return {slug: make_user(slug) for slug in ("director", "manager", "technician", "analyst", "requester", "vendor")}


@pytest.fixture
def custom(ctx, tenant):
    """An account whose custom role holds only `levels` (every other module None)."""
    def _make(slug, levels):
        role = Role.objects.create(name=slug.title(), slug=slug)
        role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
        return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                        role=Role.objects.get(pk=role.pk), first_name=slug.title(), last_name="User")
    return _make


FIGURES = {Module.REPORTS: Level.VIEW, Module.WORKORDERS: Level.VIEW, Module.CONTRACTS: Level.VIEW}


@pytest.fixture
def editor(custom):
    """Evaluations Edit, Contracts View, and the figures' areas: drafts and writes, never signs off."""
    return custom("editor", {**FIGURES, Module.EVALUATIONS: Level.EDIT})


@pytest.fixture
def blind(custom):
    """Evaluations Approve and Contracts View, no Reports View: reads the review, never its figures; may not sign or take them."""
    return custom("blind", {Module.EVALUATIONS: Level.APPROVE, Module.CONTRACTS: Level.VIEW, Module.WORKORDERS: Level.VIEW})


@pytest.fixture
def sign_in(client, users):
    def _as(who):
        user = users[who] if isinstance(who, str) else who
        client.force_login(user)
        return user
    return _as


# --- who reaches the tab --------------------------------------------------------------------------------------------------------

def test_the_tab_needs_evaluations_view_and_contracts_view(client, sign_in, custom, world, review):
    for who in ("technician", "vendor", "requester"):  # Contracts View without Evaluations; a vendor's account; a requester
        sign_in(who)
        assert client.get(BASE).status_code == 403, who
        assert client.get(url(review)).status_code == 403, who
        assert client.get(url(review, "print")).status_code == 403, who
        assert client.get(f"{BASE}summary/print/").status_code == 403, who
    sign_in(custom("evals-only", {Module.EVALUATIONS: Level.APPROVE, Module.REPORTS: Level.VIEW, Module.WORKORDERS: Level.VIEW}))
    for path in (BASE, url(review), url(review, "print"), f"{BASE}summary/print/", f"{BASE}new/"):
        assert client.get(path).status_code == 403, path
    sign_in("analyst")
    assert client.get(BASE).status_code == 200


def test_the_contracts_tabs_show_reviews_only_with_evaluations_view(client, sign_in, world):
    sign_in("technician")
    assert ">Reviews</a>" not in html(client.get("/contracts/"))
    sign_in("analyst")
    body = html(client.get("/contracts/"))
    assert f'href="{BASE}"' in body and ">Reviews</a>" in body


def test_a_scoped_user_is_refused_even_with_every_level(client, make_user, world, review):
    vendor = make_user("vendor")
    role = vendor.role
    role.set_levels({m: Level.FULL for m in Module.values})
    client.force_login(User.objects.get(pk=vendor.pk))
    assert client.get(BASE).status_code == 403 and client.get(url(review)).status_code == 403
    assert client.post(url(review, "refresh"), **HX).status_code == 403


# --- the tab ----------------------------------------------------------------------------------------------------------------------

def test_the_tab_lists_the_vendors_by_next_due(client, sign_in, world, review, today):
    sign_in("analyst")
    r = client.get(BASE)
    assert r.status_code == 200 and [t.name for t in r.templates[:2]] == ["web/vendor_reviews.html", "web/base.html"]
    assert r.context["contracts_tab"] == "reviews" and r.context["nav_active"] == "contracts"
    body = html(r)
    table = body[body.index('<table class="vr-tbl">'):]
    assert table.index(TECHCARE) < table.index(f'<b class="vr-name">{PHILIPS}</b>')  # TechCare's contract ends first
    assert day(today + 100 * DAY) in table and day(today + 200 * DAY) in table
    assert table.count(">Not reviewed</span>") == 2  # a draft is not a review
    assert f'>Draft <a class="link" href="{url(review)}">{review.number}</a>' in table
    assert "SC-PHIL" in table and "1 vendor job completed in the last 12 months" in table
    assert "New review" not in body  # the analyst has View
    assert "Record a report to committee" not in body
    assert "A vendor spelled another way (Philips Healthcare and Philips field service) is another vendor" in body


def test_the_tab_shows_the_last_review_its_decision_and_plan(client, sign_in, world, review, today):
    signed(review, today, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today + 60 * DAY)
    sign_in("manager")
    body = html(client.get(BASE))
    row = body[body.index(f'<b class="vr-name">{PHILIPS}</b>'):]
    row = row[:row.index("</tr>")]
    assert f'<a class="tag" href="{url(review)}">{review.number}</a>' in row and f"Signed off {day(today)}" in row
    assert '<span class="chip warn">Partially meets expectations</span>' in row and "Corrective action plan" in row
    assert f"Plan due {day(today + 60 * DAY)}" in row
    due = vr.next_period_end(review.period_end) + timedelta(days=vr.DUE_AFTER_DAYS)
    assert day(due) in row and "Overdue" not in row
    assert "New review" in body and "Record a report to committee" in body  # the manager has Approve


def test_the_filters_not_reviewed_and_one_vendor(client, sign_in, world, review, today):
    signed(review, today)
    sign_in("analyst")
    r = client.get(f"{BASE}?show=not_reviewed", **part("vr-list"))
    assert r.templates[0].name == "web/_vendor_review_list.html"
    body = html(r)
    assert TECHCARE in body and f'<b class="vr-name">{PHILIPS}</b>' not in body
    assert 'class="pill active"' in body and "Not reviewed <span class=\"vr-n\">1</span>" in body
    body = html(client.get(f"{BASE}?vendor=philips"))  # any letter case: by its key
    assert f'<b class="vr-name">{PHILIPS}</b>' in body and TECHCARE not in body.split('id="vr-list"')[1].split("vr-spelling")[0]
    assert "Showing Philips only." in body
    assert "No contract, vendor job, or review here names Phillips." in html(client.get(f"{BASE}?vendor=Phillips"))
    assert client.get(f"{BASE}?show=everything").status_code == 200  # an unknown filter shows every vendor


# --- New review -------------------------------------------------------------------------------------------------------------------

def test_new_review_offers_the_vendors_spellings_and_the_periods(client, sign_in, world, today):
    sign_in("manager")
    r = client.get(f"{BASE}new/?vendor=philips", **HX)
    assert r.templates[0].name == "web/_vendor_review_new.html"
    body = html(r)
    assert f'<input type="checkbox" name="names" value="{PHILIPS}" checked>' in body
    last = today.replace(day=1) - DAY
    assert f'value="{last.isoformat()}" checked' in body and "Last month&#x27;s end" in body
    assert f'value="{(today - DAY).isoformat()}"' in body and "Yesterday" in body
    assert "Up to SC-PHIL&#x27;s" not in body  # its anniversary's eve is last month's end: offered once
    assert "is another vendor, with no history here" in body
    renewed = contract(PHILIPS, today - 100 * DAY, today + 265 * DAY, reference="SC-PHIL2")
    body = html(client.get(f"{BASE}new/?vendor=philips", **HX))
    assert f'value="{(renewed.start_on - DAY).isoformat()}"' in body and "Up to SC-PHIL2&#x27;s start" in body
    anniversary = contract(PHILIPS, today - 500 * DAY, today + 230 * DAY, reference="SC-PHIL3")
    eve = add_months(anniversary.start_on, 12) - DAY
    body = html(client.get(f"{BASE}new/?vendor=philips", **HX))
    assert f'value="{eve.isoformat()}"' in body and "Up to SC-PHIL3&#x27;s anniversary" in body


def test_new_review_drafts_and_opens_the_page(client, sign_in, world, span, today):
    sign_in("manager")
    r = client.post(f"{BASE}new/", {"vendor": "philips", "names": [PHILIPS], "other_names": ["", "", ""], "period": span[1].isoformat()}, **HX)
    review = VendorReview.objects.get()
    assert r["HX-Redirect"] == url(review) and review.status == ReviewStatus.DRAFT
    assert review.vendor_names == [PHILIPS] and (review.period_start, review.period_end) == span and review.snapshot_id
    assert review.created_by.username == "manager@riverside.example"


def test_new_review_refusals_keep_what_was_typed(client, sign_in, world, span):
    sign_in("manager")
    r = client.post(f"{BASE}new/", {"vendor": "philips", "names": [PHILIPS], "other_names": ["Phillips", "", ""], "period": "other",
                                    "period_end": span[1].isoformat()}, **HX)
    body = html(r)
    assert r.status_code == 200 and not VendorReview.objects.exists()
    assert "names Phillips: write the vendor as your contracts and work orders do." in body
    assert 'value="Phillips"' in body and f'name="period_end" value="{span[1].isoformat()}"' in body
    assert 'value="other" checked' in body
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": "other", "period_end": "soon"}, **HX)
    assert "Enter the last day of the 12 months the review covers." in html(r)
    assert 'name="period_end" value="soon"' in html(r)
    r = client.post(f"{BASE}new/", {"names": [], "period": span[1].isoformat()}, **HX)
    assert "Enter the vendor as your work orders and contracts name it." in html(r)
    r = client.post(f"{BASE}new/", {"names": ["Phil\x00ips"], "period": span[1].isoformat()}, **HX)
    assert r.status_code == 200 and "Remove the null character" in html(r)


def test_new_review_overlapping_is_refused_in_words(client, sign_in, world, review, span):
    sign_in("manager")
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": span[1].isoformat()}, **HX)
    assert f"{review.number} (draft) reviews {PHILIPS}" in html(r) and VendorReview.objects.count() == 1


def test_new_review_opened_directly_is_the_tab_with_the_form(client, sign_in, world, span):
    sign_in("manager")
    r = client.get(f"{BASE}new/?vendor=TechCare%20Biomedical")
    body = html(r)
    assert r.status_code == 200 and r.templates[0].name == "web/vendor_reviews.html" and 'id="vr-new-form"' in body
    assert f'<input type="checkbox" name="names" value="{TECHCARE}" checked>' in body and 'id="vr-list"' in body
    r = client.post(f"{BASE}new/", {"names": ["Nobody"], "period": span[1].isoformat()})
    assert r.status_code == 400 and "names Nobody" in html(r)
    r = client.post(f"{BASE}new/", {"names": [TECHCARE], "period": span[1].isoformat()})
    assert r.status_code == 302 and r["Location"] == url(VendorReview.objects.get())


def test_new_review_needs_edit_and_the_figures(client, sign_in, blind, world, span):
    sign_in("analyst")
    assert client.get(f"{BASE}new/", **HX).status_code == 403
    assert client.post(f"{BASE}new/", {"names": [PHILIPS], "period": span[1].isoformat()}, **HX).status_code == 403
    sign_in(blind)  # Approve, but no Reports View: drafting takes the figures
    body = html(client.get(f"{BASE}new/", **HX))
    assert "Drafting a vendor review (it takes the figures) needs Reports View and Work orders View too" in body and "Draft it" not in body
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": span[1].isoformat()}, **HX)
    assert r.status_code == 200 and not VendorReview.objects.exists()


# --- the page and its parts -------------------------------------------------------------------------------------------------------

def test_the_page_whole_and_each_part(client, sign_in, world, review):
    sign_in("analyst")
    r = client.get(url(review))
    assert r.status_code == 200 and [t.name for t in r.templates[:2]] == ["web/vendor_reviews.html", "web/base.html"]
    body = html(r)
    for words in (review.number, "Figures", "The review", "Reported to", "Jobs completed", "Time with the vendor", "Not recorded in Cadence",
                  "Contracts in the period, as Cadence held them", "SC-PHIL", "Previous review"):
        assert words in body, words
    assert "Refresh figures" not in body and "Sign off" not in body and ">Edit</button>" not in body  # View only
    for target, template in (("vr-head", "_vendor_review_head"), ("vr-figures", "_vendor_review_figures"), ("vr-fields", "_vendor_review_fields"),
                             ("vr-reports", "_vendor_review_reports"), ("vr-body", "_vendor_review_body")):
        p = client.get(url(review), **part(target))
        assert p.templates[0].name == f"web/{template}.html", target
        assert "<html" not in html(p)
    assert client.get(f"{BASE}{review.number.lower()}/").status_code == 200  # any letter case
    assert client.get(f"{BASE}VR-99-9999/").status_code == 404


def test_the_figures_are_all_or_nothing(client, sign_in, blind, world, review):
    sign_in(blind)
    body = html(client.get(url(review)))
    assert "The figures are shown only with Evaluations View, Contracts View, Reports View, and Work orders View" in body
    assert "Jobs completed" not in body and "SC-PHIL" not in body.split('id="vr-figures"')[1].split('id="vr-fields"')[0]
    printed = html(client.get(url(review, "print")))
    assert "Figures left out of this copy" in printed and "Jobs completed" not in printed
    sign_in("analyst")
    body = html(client.get(url(review)))
    assert "Jobs completed" in body and "The figures are shown only with" not in body


def test_a_draft_offers_its_actions_by_level(client, sign_in, editor, blind, world, review):
    sign_in(editor)
    body = html(client.get(url(review)))
    assert "Refresh figures" in body and ">Discard</button>" in body and ">Edit</button>" in body
    assert ">Sign off</button>" not in body and "Withdraw" not in body
    sign_in("manager")
    body = html(client.get(url(review)))
    assert f'hx-get="{url(review, "sign-off")}?snapshot={review.snapshot_id}"' in body
    sign_in(blind)  # Approve without the figures' gate: told why, no button
    body = html(client.get(url(review)))
    assert "Signing off a vendor review needs Reports View and Work orders View too" in body and ">Sign off</button>" not in body
    assert "Taking a vendor review&#x27;s figures needs Reports View and Work orders View too" in body and "Refresh figures" not in body


def test_refresh_takes_the_figures_again(client, sign_in, editor, world, review):
    first = review.snapshot_id
    sign_in("analyst")
    assert client.post(url(review, "refresh"), **HX).status_code == 403
    sign_in(editor)
    r = client.post(url(review, "refresh"), **HX)
    assert r["HX-Retarget"] == "#vr-body" and toast_of(r) == f"{review.number}: figures taken again"
    review.refresh_from_db()
    assert review.snapshot_id != first


def test_refresh_of_a_signed_off_review_is_refused_in_words(client, sign_in, world, review, today):
    signed(review, today)
    sign_in("manager")
    r = client.post(url(review, "refresh"), **HX)
    assert f"{review.number} is signed off: only a draft&#x27;s figures are taken again." in html(r)
    assert "only a draft's figures are taken again" in toast_of(r)
    r = client.post(url(review, "refresh"))  # scripts off: the page with the refusal at its top
    assert r.status_code == 400 and "only a draft&#x27;s figures are taken again" in html(r)


# --- the reviewer's fields --------------------------------------------------------------------------------------------------------

def test_a_block_is_written_and_saved_on_its_own(client, sign_in, editor, world, review):
    sign_in(editor)
    r = client.get(url(review, "fields", "ratings") + "?edit=1", **HX)
    assert r.templates[0].name == "web/_vendor_review_field.html" and '<select name="rating_timeliness">' in html(r)
    data = {f: "met" for f in CRITERIA_FIELDS}
    r = client.post(url(review, "fields", "ratings"), {**data, "rating_invoices": "partly", "stated_in_contract": "partly"}, **HX)
    assert toast_of(r) == "Ratings saved"
    review.refresh_from_db()
    assert review.rating_timeliness == Rating.MET and review.rating_invoices == Rating.PARTLY and review.stated_in_contract == "partly"
    assert '<span class="chip warn">Partly met</span>' in html(r)
    r = client.post(url(review, "fields", "reported"), {"uptime_target_pct": "98", "uptime_reported_pct": "97.25", "systems_below": "1",
                                                        "systems_total": "5", "vendor_report_reference": "UPT-2026-Q3"}, **HX)
    body = html(r)
    assert "Uptime: target 98%, reported 97.25%" in body and "1 of 5 systems below the guarantee" in body and "UPT-2026-Q3" in body
    review.refresh_from_db()
    assert review.uptime_reported_pct == Decimal("97.25") and review.rating_invoices == Rating.PARTLY  # another block untouched


def test_a_block_refused_keeps_what_was_typed(client, sign_in, editor, world, review):
    sign_in(editor)
    r = client.post(url(review, "fields", "reported"), {"uptime_target_pct": "120", "uptime_reported_pct": "97"}, **HX)
    body = html(r)
    assert "Enter a percentage from 0 to 100, with at most two decimals." in body and 'value="120"' in body and 'value="97"' in body
    assert '<form class="ev-field-form"' in body  # still open
    review.refresh_from_db()
    assert review.uptime_reported_pct is None  # all or none
    r = client.post(url(review, "fields", "reported"), {"uptime_reported_pct": "90", "uptime_lowest_pct": "95"}, **HX)
    assert "The lowest system&#x27;s uptime cannot be above the uptime the vendor reports for all of them." in html(r)
    r = client.post(url(review, "fields", "summary"), {"summary": "x" * 2001}, **HX)
    assert "At most 2,000 characters (2,001 entered)." in html(r)
    r = client.post(url(review, "fields", "summary"), {"summary": "Fine\x00"}, **HX)
    assert r.status_code == 200 and "Remove the null character" in html(r)
    r = client.post(url(review, "fields", "decision"), {"overall": "great", "decision": "renew"}, **HX)
    assert "Choose meets, partially meets, or does not meet expectations." in html(r)
    review.refresh_from_db()
    assert review.decision == "" and review.overall == ""


def test_the_summary_says_what_it_is_for(client, sign_in, editor, world, review):
    sign_in(editor)
    body = html(client.get(url(review, "fields", "summary") + "?edit=1", **HX))
    assert escape(REVIEW_PRIVACY_WARNING) in body and "never emailed" in body and "At most 2000 characters." in body
    r = client.post(url(review, "fields", "summary"), {"summary": "Responsive; one repeat repair to discuss."}, **HX)
    assert toast_of(r) == "Summary saved" and "Responsive; one repeat repair to discuss." in html(r)


def test_a_block_post_leaves_out_what_it_does_not_send(client, sign_in, editor, world, review):
    vr.update_review(review, by=None, overall=Overall.MEETS, decision=Decision.RENEW)
    sign_in(editor)
    r = client.post(url(review, "fields", "decision"), {"decision": "renew_changes"}, **HX)
    assert toast_of(r) == "Overall and decision saved"
    review.refresh_from_db()
    assert (review.overall, review.decision) == (Overall.MEETS, Decision.RENEW_CHANGES)
    r = client.post(url(review, "fields", "decision"), {}, **HX)
    assert "Nothing to save" in html(r)


def test_blocks_by_level_and_status(client, sign_in, world, review, today):
    sign_in("analyst")
    assert client.post(url(review, "fields", "summary"), {"summary": "x"}, **HX).status_code == 403
    assert client.get(url(review, "fields", "summary") + "?edit=1", **HX).status_code == 403
    sign_in("manager")
    assert client.get(url(review, "fields", "nope"), **HX).status_code == 404
    r = client.get(url(review, "fields", "summary"))  # opened directly: the page at the block
    assert r.status_code == 302 and r["Location"] == f"{url(review)}#vr-f-summary"
    signed(review, today)
    r = client.post(url(review, "fields", "summary"), {"summary": "Too late"}, **HX)
    assert "is signed off: only a draft is changed. Withdraw it and draft another to change it." in html(r)
    assert ">Edit</button>" not in html(r)
    body = html(client.get(url(review, "fields", "summary") + "?edit=1", **HX))
    assert "<textarea" not in body  # signed off: read only


# --- the sign-off -----------------------------------------------------------------------------------------------------------------

def test_the_sign_off_says_what_is_missing_first(client, sign_in, world, review):
    sign_in("manager")
    body = html(client.get(url(review, "sign-off") + f"?snapshot={review.snapshot_id}", **HX))
    assert f"Before {review.number} can be signed off:" in body
    assert "Rate timeliness, quality of work, documentation, staff evidence, user satisfaction, invoices" in body
    assert "Choose the overall rating." in body and 'type="submit">Sign off<' not in body
    vr.update_review(review, by=None, **{**READY, "decision": Decision.CORRECTIVE_PLAN})
    body = html(client.get(url(review, "sign-off") + f"?snapshot={review.snapshot_id}", **HX))
    assert "Enter the day the corrective action plan is due." in body


def test_the_sign_off_posts_the_figures_shown(client, sign_in, world, review, today):
    vr.update_review(review, by=None, **{k: v for k, v in READY.items() if not k.startswith("approver")})
    sign_in("manager")
    body = html(client.get(url(review, "sign-off") + f"?snapshot={review.snapshot_id}", **HX))
    assert f'name="snapshot_id" value="{review.snapshot_id}"' in body and f'name="signed_on" value="{today.isoformat()}"' in body
    r = client.post(url(review, "sign-off"), {"snapshot_id": str(review.snapshot_id), "signed_on": today.isoformat(),
                                              "approver_name": "Pat Lee", "approver_title": "VP, Support Services"}, **HX)
    assert r["HX-Retarget"] == "#vr-body" and toast_of(r) == f"{review.number} signed off"
    review.refresh_from_db()
    assert review.status == ReviewStatus.APPROVED and review.signed_on == today and review.approver_name == "Pat Lee"
    assert review.signed_by.username == "manager@riverside.example"
    body = html(r)
    assert f"Signed off {day(today)} by Pat Lee, VP, Support Services" in body and "Recorded in Cadence by Manager User" in body


def test_figures_taken_again_meanwhile_are_refused_and_nothing_is_saved(client, sign_in, world, review, today):
    vr.update_review(review, by=None, **{k: v for k, v in READY.items() if not k.startswith("approver")})
    shown = review.snapshot_id
    vr.refresh(review, by=None, today=today)
    sign_in("manager")
    r = client.post(url(review, "sign-off"), {"snapshot_id": str(shown), "signed_on": today.isoformat(), "approver_name": "Pat Lee"}, **HX)
    assert vr.FIGURES_CHANGED in html(r) and "Open the review again" in html(r)
    review.refresh_from_db()
    assert review.status == ReviewStatus.DRAFT and review.approver_name == ""  # the approver's name and the sign-off: both or neither
    body = html(client.get(url(review, "sign-off") + f"?snapshot={shown}", **HX))  # a page drawn before the refresh
    assert vr.FIGURES_CHANGED in body and 'type="submit">Sign off<' not in body


def test_sign_off_refusals_on_their_fields(client, sign_in, world, review, today):
    vr.update_review(review, by=None, **{k: v for k, v in READY.items() if not k.startswith("approver")})
    sign_in("manager")
    snap = str(review.snapshot_id)
    r = client.post(url(review, "sign-off"), {"snapshot_id": snap, "signed_on": (today + DAY).isoformat(), "approver_name": ""}, **HX)
    body = html(r)
    assert "The sign-off cannot be dated after today." in body and "Enter the approver&#x27;s name as they sign." in body
    assert f'value="{(today + DAY).isoformat()}"' in body
    r = client.post(url(review, "sign-off"), {"snapshot_id": snap, "signed_on": today.isoformat(), "approver_name": "P" * 121}, **HX)
    assert "At most 120 characters (121 entered)." in html(r)
    review.refresh_from_db()
    assert review.status == ReviewStatus.DRAFT


def test_sign_off_needs_approve_and_the_figures(client, sign_in, editor, blind, world, review, today):
    vr.update_review(review, by=None, **READY)
    data = {"snapshot_id": str(review.snapshot_id), "signed_on": today.isoformat(), "approver_name": "Pat Lee"}
    sign_in(editor)
    assert client.get(url(review, "sign-off"), **HX).status_code == 403
    assert client.post(url(review, "sign-off"), data, **HX).status_code == 403
    sign_in(blind)
    r = client.post(url(review, "sign-off"), data, **HX)
    assert "Signing off a vendor review needs Reports View and Work orders View too" in html(r)
    review.refresh_from_db()
    assert review.status == ReviewStatus.DRAFT
    sign_in("manager")
    assert client.get(url(review, "sign-off")).status_code == 302  # a modal's address opened directly: the page


# --- withdraw, discard, the plan's result -----------------------------------------------------------------------------------------

def test_withdraw_a_signed_off_review(client, sign_in, editor, world, review, today):
    signed(review, today)
    sign_in(editor)
    assert client.post(url(review, "withdraw"), **HX).status_code == 403
    sign_in("manager")
    body = html(client.get(url(review, "withdraw"), **HX))
    assert f"{review.number} stays readable" in body and "Withdraw" in body
    r = client.post(url(review, "withdraw"), **HX)
    assert toast_of(r) == f"{review.number} withdrawn"
    review.refresh_from_db()
    assert review.status == ReviewStatus.WITHDRAWN
    assert "Withdrawn: kept readable" in html(client.get(url(review)))
    r = client.post(url(review, "withdraw"), **HX)
    assert f"{review.number} is withdrawn already." in html(r)


def test_discard_a_draft(client, sign_in, world, review, today):
    sign_in("analyst")
    assert client.post(url(review, "discard"), **HX).status_code == 403
    sign_in("manager")
    r = client.post(url(review, "discard"), **HX)
    assert toast_of(r) == f"{review.number} discarded"
    review.refresh_from_db()
    assert review.status == ReviewStatus.DISCARDED and "Discarded: kept, and listed nowhere." in html(client.get(url(review)))
    r = client.post(url(review, "withdraw"), **HX)
    assert f"{review.number} is discarded already." in html(r)


def test_the_plans_result(client, sign_in, world, review, today):
    signed(review, today, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today + 60 * DAY)
    sign_in("analyst")
    assert client.post(url(review, "plan"), {"plan_result": "met", "plan_closed_on": today.isoformat()}, **HX).status_code == 403
    sign_in("manager")
    page = html(client.get(url(review)))
    assert ">Plan result</button>" in page and f"Plan due {day(today + 60 * DAY)}" in page
    r = client.post(url(review, "plan"), {"plan_result": "", "plan_closed_on": today.isoformat()}, **HX)
    assert "Choose plan met, plan not met, or contract ended." in html(r)
    r = client.post(url(review, "plan"), {"plan_result": "met", "plan_closed_on": (today + DAY).isoformat()}, **HX)
    assert "The plan cannot be closed after today." in html(r) and f'value="{(today + DAY).isoformat()}"' in html(r)
    r = client.post(url(review, "plan"), {"plan_result": "met", "plan_closed_on": today.isoformat()}, **HX)
    assert toast_of(r) == f"{review.number}: plan result recorded" and f"Plan met {day(today)}" in html(r)
    review.refresh_from_db()
    assert review.plan_result == PlanResult.MET
    body = html(client.get(url(review, "plan"), **HX))
    assert "Clear the result" in body
    r = client.post(url(review, "plan"), {"plan_result": "", "plan_closed_on": ""}, **HX)
    assert toast_of(r) == f"{review.number}: plan result cleared"
    review.refresh_from_db()
    assert review.plan_result == ""


def test_a_review_without_a_plan_has_no_plan_result(client, sign_in, world, review, today):
    signed(review, today)
    sign_in("manager")
    assert ">Plan result</button>" not in html(client.get(url(review)))
    r = client.post(url(review, "plan"), {"plan_result": "met", "plan_closed_on": today.isoformat()}, **HX)
    assert "is signed off without a corrective action plan" in html(r)


# --- the committees it was reported to --------------------------------------------------------------------------------------------

def test_record_correct_and_remove_a_committee_report(client, sign_in, editor, world, review, today):
    signed(review, today)
    sign_in(editor)
    assert client.get(url(review, "reports", "new"), **HX).status_code == 403
    sign_in("manager")
    r = client.post(url(review, "reports", "new"), {"body": "governing", "meeting_on": today.isoformat(), "minutes_reference": "GB 1"}, **HX)
    assert "without spaces" in html(r) and 'value="GB 1"' in html(r)  # the minutes' reference is a token
    r = client.post(url(review, "reports", "new"), {"body": "governing", "meeting_on": (today - DAY).isoformat(), "minutes_reference": "GB-1"}, **HX)
    body = html(r)
    assert "the meeting that received it is on or after that day" in body and f'value="{(today - DAY).isoformat()}"' in body
    assert 'value="GB-1"' in body and not VendorReviewReport.objects.exists()
    r = client.post(url(review, "reports", "new"), {"body": "governing", "meeting_on": today.isoformat(), "minutes_reference": "GB-2026-03"}, **HX)
    assert r["HX-Retarget"] == "#vr-reports" and toast_of(r) == "Committee report recorded"
    rep = VendorReviewReport.objects.get()
    assert (rep.body, rep.meeting_on, rep.minutes_reference) == (ReportBody.GOVERNING, today, "GB-2026-03")
    assert f"Met {day(today)} · minutes GB-2026-03 · recorded by Manager User" in html(r)
    r = client.post(url(review, "reports", rep.pk), {"body": "quality", "meeting_on": today.isoformat(), "minutes_reference": ""}, **HX)
    assert toast_of(r) == "Committee report corrected"
    rep.refresh_from_db()
    assert rep.body == ReportBody.QUALITY and rep.minutes_reference == ""
    r = client.post(url(review, "reports", rep.pk, "remove"), **HX)
    assert toast_of(r) == "Committee report removed" and not VendorReviewReport.objects.exists()
    assert client.post(url(review, "reports", rep.pk, "remove"), **HX).status_code == 404


def test_a_draft_takes_no_committee_report(client, sign_in, world, review, today):
    sign_in("manager")
    assert "Record a report</button>" not in html(client.get(url(review)))
    r = client.post(url(review, "reports", "new"), {"body": "governing", "meeting_on": today.isoformat()}, **HX)
    assert f"{review.number} is draft: only a signed-off review is reported to a committee." in html(r)


def test_record_a_report_to_committee_for_several(client, sign_in, world, review, span, today):
    signed(review, today)
    tech = signed(vr.create_review(vendor_names=[TECHCARE], period_end=span[1], by=None, today=today), today)
    sign_in("analyst")
    assert client.get(f"{BASE}committee/", **HX).status_code == 403
    sign_in("manager")
    body = html(client.get(f"{BASE}committee/", **HX))
    assert f'value="{review.pk}" checked' in body and f'value="{tech.pk}" checked' in body  # each vendor's last, not yet reported
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": (today + DAY).isoformat(), "reviews": [str(review.pk)]}, **HX)
    body = html(r)
    assert "The meeting cannot be after today" in body and f'value="{review.pk}" checked' in body and f'value="{tech.pk}">' in body
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": today.isoformat(), "reviews": ["junk"]}, **HX)
    assert "Choose reviews from the list." in html(r)
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": today.isoformat()}, **HX)
    assert "Choose the reviews the committee received." in html(r)
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": today.isoformat(), "minutes_reference": "GB-2026-03",
                                          "reviews": [str(review.pk), str(tech.pk)]}, **HX)
    assert toast_of(r) == "Reported to the governing body: 2 reviews" and "modal-close" in triggers(r)
    assert VendorReviewReport.objects.filter(body=ReportBody.GOVERNING, meeting_on=today).count() == 2
    body = html(client.get(f"{BASE}committee/", **HX))
    assert f'value="{review.pk}" checked' not in body and "reported to Governing body" in body
    body = html(client.get(f"{BASE}committee/?body=quality", **HX))
    assert f'value="{review.pk}" checked' in body  # not yet reported to quality
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": today.isoformat(), "reviews": [str(review.pk)]}, **HX)
    assert f"{review.number} is recorded as reported to the governing body on {day(today)} already." in html(r)


def test_another_facilitys_review_cannot_be_reported(client, sign_in, world, review, today, other_tenant):
    signed(review, today)
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        theirs = contract(PHILIPS, review.period_start, today + 100 * DAY)
        other = vr.create_review(vendor_names=[PHILIPS], period_end=review.period_end, by=None, today=today)
        assert theirs and other
    sign_in("manager")
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": today.isoformat(), "reviews": [str(review.pk), str(other.pk)]}, **HX)
    assert "Choose reviews from the list." in html(r) and not VendorReviewReport.objects.exists()


# --- the prints -------------------------------------------------------------------------------------------------------------------

def test_the_reviews_print(client, sign_in, world, review, today):
    vr.update_review(review, by=None, summary="Responsive this year.")
    sign_in("analyst")
    printed = html(client.get(url(review, "print")))
    assert '<p class="vrp-stamp draft">Draft</p>' in printed and "Contracted service review" in printed
    assert "The figures come from Cadence&#x27;s records of the vendor&#x27;s work, dated when they were entered." in printed
    assert "Responsive this year." in printed and "Jobs completed" in printed and "Figures left out" not in printed
    signed(review, today, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today + 60 * DAY)
    VendorReview.objects.filter(pk=review.pk).update(signed_by=VendorReview.objects.get(pk=review.pk).created_by)
    vr.record_report([review], body=ReportBody.GOVERNING, meeting_on=today, minutes_reference="GB-2026-03", by=None, today=today)
    printed = html(client.get(url(review, "print")))
    assert f'<p class="vrp-stamp approved">Signed off {day(today)} by Pat Lee, VP, Support Services</p>' in printed
    assert "Recorded in Cadence by" in printed and "Partially meets expectations" in printed and f"Plan due {day(today + 60 * DAY)}" in printed
    assert "GB-2026-03" in printed and "Governing body" in printed
    for never in ("meets A-0084", "uptime of", "qualified"):
        assert never not in printed


def test_the_contracted_services_summary(client, sign_in, world, review, today):
    signed(review, today, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today + 60 * DAY,
           summary="Private words about the vendor.")
    vr.record_report([review], body=ReportBody.QUALITY, meeting_on=today, minutes_reference="QC-7", by=None, today=today)
    sign_in("analyst")
    r = client.get(f"{BASE}summary/print/")
    printed = html(r)
    assert r.templates[0].name == "web/print_vendor_reviews.html" and "Contracted services summary" in printed
    assert f"as of {day(today)}" in printed
    philips = printed[printed.index(f"<b>{PHILIPS}</b>"):]
    philips = philips[:philips.index("</tr>")]
    assert "SC-PHIL" in philips and f"{day(world['philips'].start_on)} to {day(world['philips'].end_on)}" in philips
    assert review.number in philips and "Partially meets expectations" in philips and "Corrective action plan" in philips
    assert f"Plan due {day(today + 60 * DAY)}" in philips and f"Quality oversight (QAPI) {day(today)} · minutes QC-7" in philips
    not_reviewed = printed[printed.index("Contracted vendors not reviewed"):printed.index("Corrective action plans open")]
    assert TECHCARE in not_reviewed and "SC-TECH" in not_reviewed and PHILIPS not in not_reviewed
    plans = printed[printed.index("Corrective action plans open"):]
    assert review.number in plans
    assert "Private words about the vendor." not in printed and "Jobs completed" not in printed  # no summary text, no figures
    for elsewhere in (BASE, f"/contracts/{world['philips'].pk}/"):  # nor on the tab or in the contract drawer: the review's page only
        assert "Private words about the vendor." not in html(client.get(elsewhere, **HX)), elsewhere
    assert "Private words about the vendor." in html(client.get(url(review)))


# --- the contract drawer's line ---------------------------------------------------------------------------------------------------

def test_the_contract_drawers_review_line(client, sign_in, world, review, today):
    c = world["philips"]
    sign_in("technician")  # Contracts View, no Evaluations: no line
    assert "vr-ct-line" not in html(client.get(f"/contracts/{c.pk}/", **HX))
    sign_in("analyst")
    body = html(client.get(f"/contracts/{c.pk}/", **HX))
    assert f"Not reviewed; draft {review.number} open; next due {day(c.end_on)}" in body
    assert f'href="{BASE}?vendor={PHILIPS}"' in body
    signed(review, today, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today + 60 * DAY)
    body = html(client.get(f"/contracts/{c.pk}/", **HX))
    due = vr.next_period_end(review.period_end) + timedelta(days=vr.DUE_AFTER_DAYS)
    assert f"Last review {review.number}, partially meets, plan due {day(today + 60 * DAY)}; next due {day(due)}" in body
    assert f'href="{url(review)}"' in body
    body = html(client.get(f"/contracts/{world['techcare'].pk}/", **HX))
    assert f"Not reviewed; next due {day(world['techcare'].end_on)}" in body


def test_the_drawer_line_says_overdue(client, sign_in, ctx, today):
    c = contract("Acme Imaging", today - 900 * DAY, today + 200 * DAY)
    old = signed(vr.create_review(vendor_names=["Acme Imaging"], period_end=today - 500 * DAY, by=None, today=today), today)
    due = vr.next_period_end(old.period_end) + timedelta(days=vr.DUE_AFTER_DAYS)
    assert due < today
    sign_in("analyst")
    body = html(client.get(f"/contracts/{c.pk}/", **HX))
    assert f"Last review {old.number}, meets, renew; next due {day(due)} (overdue)" in body and "vr-ct-line warn" in body
    ended = contract("Gone Imaging", today - 400 * DAY, today - 5 * DAY)  # ended in the last 12 months, never reviewed: no day due
    body = html(client.get(f"/contracts/{ended.pk}/", **HX))
    assert ">Not reviewed</a>" in body and "vr-ct-line warn" not in body


# --- History, junk, isolation -----------------------------------------------------------------------------------------------------

def test_history_carries_the_committee_reports(client, sign_in, world, review, today):
    signed(review, today)
    vr.record_report([review], body=ReportBody.GOVERNING, meeting_on=today, by=None, today=today)
    sign_in("analyst")
    r = client.get(url(review) + "?history=1", **part("hist"))
    body = html(r)
    assert r.templates[0].name == "web/_history_entries.html"
    for words in ("Drafted", "Figures taken", "Signed off", "Committee report added", "Reported to the governing body"):
        assert words in body, words
    full = client.get(url(review) + "?history=1")
    assert full.status_code == 200 and "Reported to the governing body" in html(full)


@pytest.mark.parametrize("method, path, data", [
    ("get", "?show=%3Cscript%3E&vendor=%00", {}),
    ("get", "new/?vendor=%00&period=junk&period_end=%00", {}),
    ("post", "new/", {"names": ["P" * 500], "period": "other", "period_end": "9999-99-99"}),
    ("post", "new/", {"names": [PHILIPS], "period": "0001-01-01"}),
    ("post", "new/", {"names": [PHILIPS], "period": "9999-12-31"}),
    ("post", "committee/", {"body": "nobody", "meeting_on": "0000-00-00", "reviews": ["junk", "00000000-0000-0000-0000-000000000000"]}),
    ("get", "committee/?body=%00", {}),
    ("post", "{n}/fields/reported/", {"uptime_target_pct": "1e400", "systems_total": "99999999999", "response_target_pct": "NaN"}),
    ("post", "{n}/fields/ratings/", {"rating_timeliness": "<script>", "stated_in_contract": "\x00"}),
    ("post", "{n}/fields/decision/", {"plan_due_on": "0000-00-00"}),
    ("post", "{n}/fields/nope/", {}),
    ("post", "{n}/sign-off/", {"snapshot_id": "junk", "signed_on": "junk", "approver_name": "\x00"}),
    ("get", "{n}/sign-off/?snapshot=%00", {}),
    ("post", "{n}/plan/", {"plan_result": "junk", "plan_closed_on": "x"}),
    ("post", "{n}/reports/new/", {"body": "x", "meeting_on": "0000-00-00", "minutes_reference": "a b"}),
    ("post", "{n}/reports/00000000-0000-0000-0000-000000000000/remove/", {}),
    ("get", "{n}/?older=garbage&history=1", {}),
    ("get", "VR-99-9999/print/", {}),
    ("post", "%3Cscript%3E/withdraw/", {}),
])
def test_junk_is_never_a_500(client, sign_in, world, review, method, path, data):
    sign_in("director")
    r = getattr(client, method)(BASE + path.replace("{n}", review.number), data, **HX)
    assert r.status_code in (200, 302, 400, 403, 404, 405), (path, r.status_code)


def test_another_facilitys_review_is_a_404(client, sign_in, ctx, other_tenant, today, span):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="GE", model="Dash 5000", description="Monitor", category="Monitors")
        Asset.objects.create(tag="THEIRS-1", device_model=model, department=Department.objects.create(name="ICU"))
        contract(PHILIPS, span[0], today + 100 * DAY)
        theirs = vr.create_review(vendor_names=[PHILIPS], period_end=span[1], by=None, today=today)
    sign_in("director")
    assert not VendorReview.objects.exists()
    for path in ("", "print/", "sign-off/", "fields/summary/?edit=1"):
        assert client.get(f"{BASE}{theirs.number}/{path}", **HX).status_code == 404, path
    assert client.post(f"{BASE}{theirs.number}/discard/", **HX).status_code == 404
    assert theirs.number not in html(client.get(BASE))


# --- under PostgreSQL's row-level security -------------------------------------------------------------------------------------

@needs_postgres
def test_the_screens_under_the_policies(client, sign_in, world, span, today):
    """The tab, New review, the page and a block, the sign-off, a committee report, the batch, both prints, and the drawer line, as
    the runtime role inside this facility."""
    sign_in("manager")
    contract_pk = world["philips"].pk
    as_app_role()  # from here, only requests: each sets and clears the facility the policies read
    assert PHILIPS in html(client.get(BASE))
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": span[1].isoformat()}, **HX)
    page = r["HX-Redirect"]
    number = page.rstrip("/").rsplit("/", 1)[1]
    body = html(client.get(page))
    assert "Jobs completed" in body
    snapshot = re.search(r"sign-off/\?snapshot=([0-9a-f-]{36})", body).group(1)
    data = {**{f: "met" for f in CRITERIA_FIELDS}}
    assert toast_of(client.post(f"{page}fields/ratings/", data, **HX)) == "Ratings saved"
    assert toast_of(client.post(f"{page}fields/decision/", {"overall": "meets", "decision": "renew"}, **HX)) == "Overall and decision saved"
    r = client.post(f"{page}sign-off/", {"snapshot_id": snapshot, "signed_on": today.isoformat(), "approver_name": "Pat Lee"}, **HX)
    assert toast_of(r) == f"{number} signed off"
    ticked = re.search(r'name="reviews" value="([0-9a-f-]{36})" checked', html(client.get(f"{BASE}committee/", **HX))).group(1)
    r = client.post(f"{BASE}committee/", {"body": "governing", "meeting_on": today.isoformat(), "reviews": [ticked]}, **HX)
    assert toast_of(r) == "Reported to the governing body: 1 review"
    assert "Signed off" in html(client.get(f"{page}print/")) and number in html(client.get(f"{BASE}summary/print/"))
    assert f"Last review {number}, meets, renew" in html(client.get(f"/contracts/{contract_pk}/", **HX))
