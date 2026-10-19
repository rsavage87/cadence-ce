"""
Slice 34's review, each fix pinned (the confirmed findings, and the fixes made for them):

#F1 Imported work orders are counted apart (jobs_imported, cost_imported) and in no other figure: an imported vendor PM is never a PM
    assigned to the vendor (on time or late), pending, or on a covered device, and an imported repair is never a later repair.
#F2 provisional_until also covers the PMs due in the period on devices under the vendor's contracts, not assigned to it, still inside
    their window (pm_covered_not_assigned may still change until then), with no query more.
#F3 The sign-off posts the version of the reviewer's fields the modal showed (vendor_reviews.fields_version): a change saved meanwhile is
    refused (REVIEW_CHANGED) and nothing is signed; the approver's name and title are not in it; a POST without one is refused; the
    service without one (the seed, tests) still signs; the modal shows the ratings, the vendor's own figures, and the summary.
#F4 No 12 months end on Feb 28 of a leap year (leap_feb_28): refused keyed period_end, the presets offer Feb 29 instead (or nothing while
    Feb 29 has not ended), so every allowed end's next 12 months start the day after it.
#F5 New review's "Another day" box is outside its radio's label and chooses it (data-chooses, cadence.js); a day typed while a preset is
    ticked is refused keyed period_end with what was typed kept, never dropped.
#F6 The change log reads the vendor review areas only with Contracts View too (Area.also), on the page, its CSV and print, and the API;
    staff-written text (a review's summary, a program evaluation's narrative) never leaves there: "<field> edited (the words are on its
    own page)"; the record's own History keeps the words.
#F7 The facility's PM target from a stored snapshot reads by its decimal text: "97.1%", never a float's binary expansion.
#F8 Refusals name the module by its label: Evaluations View, Edit, Approve; nothing says "Program evaluation View/Edit/Approve".
#F9 A review's history reads its vendor names as a list in words and its percentages with "%"; a report's body is "Committee".
#F10 The contracted services summary print states the renewal exception its Next due column follows.
#F11 Repeat repairs' detail counts one thing on each side: "n of N repairs; k later repairs not recorded", each noun in the singular
    after a count of one ("0 of 1 repair; 1 later repair not recorded").
#F12 A block's change reason fits the history's column without a word cut, and History never repeats it as the entry's reason.
#F13 The contracts table's last column is "Devices on <the day the figures were taken>" on the page and the print, never "Devices today".
Tenancy: a second facility never sees the first's figures, reviews, or change log entries in any of these.
"""
import itertools
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from csvutil import csv_text
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from evaluation_fixtures import draft, last_year
from pg_helpers import as_app_role, needs_postgres
from vendor_review_days import ended

from apps.accounts.models import Level, Module, Role, User, create_default_roles
from apps.api import views_evaluations as api_evaluations
from apps.contracts import services as cs
from apps.contracts.models import Contract
from apps.core import history
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.evaluations import services as ev_services
from apps.evaluations import vendor_figures as vf
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import CRITERIA_FIELDS, Decision, Overall, Rating, ReportBody, ReviewStatus, VendorReview
from apps.facility import services as fac
from apps.facility.models import FacilitySettings, PmWindow
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.web import views_vendor_reviews as views
from apps.workorders import legacy
from apps.workorders.models import Priority, RepairCheck, RepairFinding, Source, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
PHILIPS, TECHCARE = "Philips", "TechCare Biomedical"
KEY = "philips"
BASE = "/contracts/reviews/"
LOG, CSV, PRINT, API = "/users/log/", "/users/log/export.csv", "/users/log/print/", "/api/v1/change-log/"
SUMMARY_WORDS = "Zq7Canary reviewer summary about Philips"
NARRATIVE_WORDS = "Zq7Canary narrative for the committee"
REVIEW_AREAS = ("vendor_reviews", "vendor_review_reports")
APPS = Path(__file__).resolve().parent.parent / "apps"
_refs = itertools.count(1)


# --- small helpers (as tests/test_vendor_figures.py and tests/test_vendor_reviews_web.py have them) -------------------------------

def aware(d, hour=12):
    return timezone.make_aware(datetime.combine(d, time(hour)))


def day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def html(r) -> str:
    return r.content.decode()


def part(target):
    return {**HX, "HTTP_HX_TARGET": target}


def url(r, *rest) -> str:
    return f"{BASE}{r.number}/" + "".join(f"{x}/" for x in rest)


def status_row(wo, to, *, on, frm="", by=None, note="", hour=12):
    row = WorkOrderStatusHistory.objects.create(work_order=wo, from_status=frm, to_status=to, changed_by=by, note=note)
    WorkOrderStatusHistory.objects.filter(pk=row.pk).update(created_at=aware(on, hour))


def job(asset, *, opened, completed=None, vendor=PHILIPS, type_=WoType.REPAIR, due=None, finding=None, by=None):
    """A work order on vendor service under `vendor` ("": in-house), opened on `opened`, completed on `completed` (else in progress);
    a completed repair is checked and found worn out unless `finding` says."""
    repair = type_ == WoType.REPAIR
    done = completed is not None
    wo = WorkOrder.objects.create(asset=asset, type=type_, priority=Priority.NORMAL, status=WoStatus.COMPLETED if done else WoStatus.IN_PROGRESS,
                                  source=Source.MANUAL, problem="Broken", vendor_service=bool(vendor), vendor_name=vendor or "",
                                  opened_on=opened, due_on=due or opened + 5 * DAY, completed_on=completed,
                                  repair_check=RepairCheck.PASSED if repair and done else "",
                                  repair_finding=(RepairFinding.WEAR if finding is None else finding) if repair and done else "")
    status_row(wo, WoStatus.OPEN, on=opened, note="Opened", hour=8)
    if done:
        status_row(wo, WoStatus.COMPLETED, frm=WoStatus.IN_PROGRESS, on=completed, by=by, hour=15)
    return wo


def contract(vendor, start_on, end_on, *, on=None, cost="12000.00", reference=None):
    """A contract through the services, its first history row dated `on` (default: ten days before its term: entered at onboarding)."""
    c = cs.create_contract(reference=reference or f"SC-{next(_refs):04d}", vendor=vendor, start_on=start_on, end_on=end_on,
                           annual_cost=Decimal(cost))
    row = Contract.history.filter(id=c.pk).order_by("-history_id").first()
    Contract.history.filter(history_id=row.history_id).update(history_date=aware(on or start_on - 10 * DAY, 9))
    return c


def settings_on(d, **fields):
    """Change Settings and date the change's history row on `d` (the figures read Settings as they stood at the period's end)."""
    s = fac.update_settings(**fields)
    latest = FacilitySettings.history.order_by("-history_date", "-history_id").first()
    FacilitySettings.history.filter(history_id=latest.history_id).update(history_date=aware(d))
    return s


def fig(data, key) -> dict:
    return vf.by_key(data)[key]


def split(figure, key) -> dict:
    return {s["key"]: s for s in figure["splits"]}[key]


def nd(f) -> tuple:
    return f["num"], f["den"]


def take(span_, today, keys=(KEY,)):
    return vf.snapshot(list(keys), span_[0], span_[1], today=today)


def imported(asset, number, *, type_, opened, due=None, completed=None, vendor="", status=WoStatus.COMPLETED):
    """A work order from the previous system, through the importer (as an onboarding import writes it)."""
    return legacy.import_work_order(asset=asset, legacy_number=number, type=type_, status=status, opened_on=opened, due_on=due,
                                    completed_on=completed, vendor_name=vendor).work_order


READY = {**{f: Rating.MET for f in CRITERIA_FIELDS}, "overall": Overall.MEETS, "decision": Decision.RENEW}


def signed(r, today, **over):
    r = vr.update_review(r, by=None, **{**READY, "approver_name": "Pat Lee", "approver_title": "VP, Support Services", **over})
    return vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)


def custom_user(tenant, slug, levels: dict) -> User:
    """An account whose custom role (not scoped) holds only `levels`, every other module None."""
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{slug}@{tenant.slug}.example", password="Test-Pass-2026-x", tenant=tenant,
                                    role=Role.objects.get(pk=role.pk), first_name=slug.title(), last_name="User")


def reasons(r) -> list:
    return list(VendorReview.history.filter(id=r.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


# --- fixtures ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    """The 12 months ending at the end of last month (the Reviews tab's first preset)."""
    end = ended(today.replace(day=1) - DAY)
    return vr.period_start_of(end), end


@pytest.fixture
def fig_span(today):
    """A 12-month period that ended 40 days ago: its repeat repairs are final."""
    end = today - 40 * DAY
    return add_months(end, -12) + DAY, end


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12)


@pytest.fixture
def monitors(ctx, dept, monitor_model):
    return {f"m{i}": Asset.objects.create(tag=f"CE-2000{i}", device_model=monitor_model, department=dept) for i in range(1, 4)}


@pytest.fixture
def world(ctx, span, today, pump):
    """Philips: a contract in force through the period and after today, and a vendor repair completed in the period. TechCare: a
    contract in force, no jobs."""
    philips = contract(PHILIPS, span[0], today + 200 * DAY, reference="SC-PHIL")
    cs.add_asset(philips, pump)
    techcare = contract(TECHCARE, span[0], today + 100 * DAY, reference="SC-TECH")
    repair = job(pump, opened=span[0] + 37 * DAY, completed=span[0] + 40 * DAY)
    return {"philips": philips, "techcare": techcare, "repair": repair}


@pytest.fixture
def review(world, span, today):
    """A draft review of Philips for the 12 months ending at the end of last month, its figures taken."""
    return vr.create_review(vendor_names=[PHILIPS], period_end=span[1], by=None, today=today)


@pytest.fixture
def users(make_user):
    return {slug: make_user(slug) for slug in ("director", "manager", "technician", "analyst")}


@pytest.fixture
def editor(ctx, tenant):
    """Evaluations Edit with Contracts View and the figures' areas: writes the review's fields, never signs it."""
    return custom_user(tenant, "editor", {Module.EVALUATIONS: Level.EDIT, Module.CONTRACTS: Level.VIEW, Module.REPORTS: Level.VIEW,
                                          Module.WORKORDERS: Level.VIEW})


# --- F1: imported work orders in no figure but their own --------------------------------------------------------------------------

def test_f1_an_imported_vendor_pm_is_counted_apart_and_in_no_pm_figure(ctx, today, fig_span, monitors):
    """The finding's probe: a Philips PM due S+20, completed S+60, imported. Beside it an imported in-house PM on a device under the
    vendor's contract, and an imported vendor PM on one: none is a PM assigned, late on the vendor's side, pending, or covered."""
    S, E = fig_span
    m1, m2, m3 = monitors["m1"], monitors["m2"], monitors["m3"]
    c = contract(PHILIPS, S - 100 * DAY, today + 200 * DAY, reference="SC-1")
    cs.add_asset(c, m2)
    cs.add_asset(c, m3)
    imported(m1, "OLD-PM-1", type_=WoType.PM, opened=S, due=S + 20 * DAY, completed=S + 60 * DAY, vendor=PHILIPS)
    imported(m2, "OLD-PM-2", type_=WoType.PM, opened=S + 10 * DAY, due=S + 30 * DAY, completed=S + 31 * DAY)  # in-house, covered device
    imported(m3, "OLD-PM-3", type_=WoType.PM, opened=S + 10 * DAY, due=S + 40 * DAY, completed=S + 80 * DAY, vendor=PHILIPS)  # covered
    data = take(fig_span, today)
    assert (fig(data, "jobs_imported")["value"], fig(data, "jobs_completed")["value"]) == (2, 0)
    on_time = fig(data, "pm_on_time")
    assert nd(on_time) == (0, 0) and on_time["display"] == "None due"
    assert nd(fig(data, "pm_late_vendor")) == (0, 0) and split(fig(data, "pm_late_vendor"), "none")["value"] == 0
    assert nd(fig(data, "pm_late_facility")) == (0, 0) and nd(fig(data, "pm_late_other")) == (0, 0)
    assert fig(data, "pm_pending")["value"] == 0
    covered = fig(data, "pm_covered_not_assigned")
    assert nd(covered) == (0, 0) and {s["key"]: nd(s) for s in covered["splits"]} == {"completed": (0, 0), "not_completed": (0, 0)}
    assert "Jobs imported from the previous system are counted apart and left out of every other figure." in data["notes"]  # as it says


def test_f1_live_pms_still_count_beside_imported_ones(ctx, today, fig_span, monitors):
    """The filter keeps Cadence's own PMs: a live late Philips PM with no reason is still on the vendor's side, an imported one never."""
    S, E = fig_span
    m1 = monitors["m1"]
    job(m1, type_=WoType.PM, opened=S, due=S + 20 * DAY, completed=S + 60 * DAY)
    imported(m1, "OLD-PM-1", type_=WoType.PM, opened=S + 30 * DAY, due=S + 50 * DAY, completed=S + 90 * DAY, vendor=PHILIPS)
    data = take(fig_span, today)
    assert nd(fig(data, "pm_on_time")) == (0, 1) and split(fig(data, "pm_late_vendor"), "none")["value"] == 1
    assert (fig(data, "jobs_completed")["value"], fig(data, "jobs_imported")["value"]) == (1, 1)


def test_f1_an_imported_repair_is_never_a_later_repair(ctx, today, fig_span, monitors):
    """The finding's second case: a live Philips repair completed S+15, then an imported in-house repair opened S+20 (completed), and
    another imported one still open: neither is a repeat."""
    S, E = fig_span
    m1 = monitors["m1"]
    job(m1, opened=S + 10 * DAY, completed=S + 15 * DAY)
    imported(m1, "OLD-R-1", type_=WoType.REPAIR, opened=S + 20 * DAY, completed=S + 21 * DAY)
    imported(m1, "OLD-R-2", type_=WoType.REPAIR, opened=S + 25 * DAY, status=WoStatus.IN_PROGRESS)
    data = take(fig_span, today)
    assert data["repeats"] == []
    f = fig(data, "repeat_repairs")
    assert nd(f) == (0, 1) and f["detail"] == "0 of 1 repair; 0 later repairs not recorded"
    assert nd(fig(data, "repeat_not_recorded")) == (0, 0) and fig(data, "repeat_other")["value"] == 0
    live = job(m1, vendor="", opened=S + 22 * DAY, completed=S + 23 * DAY, finding=RepairFinding.BATTERY)  # a live one still counts
    data = take(fig_span, today)
    assert [p["later"] for p in data["repeats"]] == [live.number] and fig(data, "repeat_repairs")["detail"].startswith("1 of 1 repair;")


# --- F2: provisional until the covered devices' PMs are counted -------------------------------------------------------------------

def _window_45(today):
    E = today - 35 * DAY
    S = add_months(E, -12) + DAY
    both = {"pm_window_high": PmWindow.DAYS_AFTER, "pm_window_high_days": 45, "pm_window_other": PmWindow.DAYS_AFTER,
            "pm_window_other_days": 45}
    settings_on(S - 10 * DAY, **both)
    return S, E


def test_f2_a_covered_pm_inside_its_window_keeps_the_figures_provisional(ctx, today, monitors):
    """The finding's scenario: a 45-day window, the period ended 35 days ago, a Philips contract on m1, an in-house PM on m1 due the day
    before the period's end and not done, no PM of the vendor's own."""
    S, E = _window_45(today)
    c = contract(PHILIPS, S - 100 * DAY, today + 200 * DAY, reference="SC-1")
    cs.add_asset(c, monitors["m1"])
    due = E - DAY
    job(monitors["m1"], vendor="", type_=WoType.PM, opened=due - 20 * DAY, due=due)
    data = take((S, E), today)
    assert data["provisional_until"] == (due + 45 * DAY).isoformat()  # the PM's window end: E + 44, past E + 30
    assert nd(fig(data, "pm_covered_not_assigned")) == (0, 0) and fig(data, "pm_pending")["value"] == 0  # not the vendor's: never pending
    later = take((S, E), due + 46 * DAY)  # the window has closed: the figure has changed, and is final now
    assert later["provisional_until"] is None and nd(fig(later, "pm_covered_not_assigned")) == (1, 1)
    last_day = take((S, E), due + 45 * DAY)  # its window's last day: still open
    assert last_day["provisional_until"] == (due + 45 * DAY).isoformat()


def test_f2_only_covered_devices_and_only_pms_not_the_vendors(ctx, today, monitors):
    """A PM inside its window on a device under no contract of the vendor's, or another vendor's, keeps nothing provisional; the
    vendor's own pending PM keeps counting as pending."""
    S, E = _window_45(today)
    contract(PHILIPS, S - 100 * DAY, today + 200 * DAY, reference="SC-1")  # covers no device
    ge = contract("GE Healthcare", S - 100 * DAY, today + 200 * DAY, reference="SC-GE")
    cs.add_asset(ge, monitors["m2"])
    job(monitors["m1"], vendor="", type_=WoType.PM, opened=E - 30 * DAY, due=E - DAY)
    job(monitors["m2"], vendor="", type_=WoType.PM, opened=E - 30 * DAY, due=E - DAY)
    data = take((S, E), today)
    assert data["provisional_until"] is None and fig(data, "pm_pending")["value"] == 0
    own = job(monitors["m3"], type_=WoType.PM, opened=E - 30 * DAY, due=E - 2 * DAY)  # the vendor's own
    data = take((S, E), today)
    assert fig(data, "pm_pending")["value"] == 1 and data["provisional_until"] == (own.due_on + 45 * DAY).isoformat()


def test_f2_the_query_count_is_unchanged(ctx, today, monitors, pump):
    """The covered PMs join the pending PMs' grouped query: no query more with them than without, at any size (the vendor has a job
    in each, so every query of the snapshot runs)."""
    S, E = _window_45(today)
    c = contract(PHILIPS, S - 100 * DAY, today + 200 * DAY, reference="SC-1")
    job(pump, opened=S + 10 * DAY, completed=S + 12 * DAY)
    with CaptureQueriesContext(connection) as without:
        assert take((S, E), today)["provisional_until"] is None
    cs.add_asset(c, monitors["m1"])
    job(monitors["m1"], vendor="", type_=WoType.PM, opened=E - 30 * DAY, due=E - DAY)
    with CaptureQueriesContext(connection) as one:
        assert take((S, E), today)["provisional_until"] is not None
    cs.add_asset(c, monitors["m2"])
    cs.add_asset(c, monitors["m3"])
    for i, asset in enumerate((monitors["m2"], monitors["m3"], monitors["m1"])):
        job(asset, vendor="", type_=WoType.PM, opened=E - 40 * DAY, due=E - (3 + i) * DAY)
        job(asset, type_=WoType.PM, opened=E - 40 * DAY, due=E - (6 + i) * DAY)
    with CaptureQueriesContext(connection) as many:
        assert fig(take((S, E), today), "pm_pending")["value"] == 3
    assert len(without) == len(one) == len(many) <= 17, [q["sql"][:120] for q in many.captured_queries]


# --- F3: the sign-off signs what the approver saw --------------------------------------------------------------------------------

def _sign_modal(client, r):
    return html(client.get(url(r, "sign-off") + f"?snapshot={r.snapshot_id}", **HX))


def _version_in(body: str) -> str:
    """The version the modal posts back ("" when it posts none)."""
    found = re.search(r'name="version" value="([0-9a-f]{16})"', body)
    return found.group(1) if found else ""


CHANGED = "The review was changed meanwhile. Read it again, then sign off."


def test_f3_a_change_saved_between_the_modal_and_the_post_is_refused(client, users, editor, world, review, today):
    """The finding's race: the director opens Sign off (all Met, Meets, Renew); meanwhile the editor saves Does not meet, Replace, and
    quality Not met; the director's post is refused, in words, and nothing is signed."""
    vr.update_review(review, by=None, **READY)
    client.force_login(users["director"])
    shown = _version_in(_sign_modal(client, review))
    other = Client()
    other.force_login(editor)
    assert other.post(url(review, "fields", "decision"), {"overall": "does_not_meet", "decision": "replace"}, **HX).status_code == 200
    ratings = {**{f: "met" for f in CRITERIA_FIELDS}, "rating_quality": "not_met"}
    assert other.post(url(review, "fields", "ratings"), ratings, **HX).status_code == 200
    r = client.post(url(review, "sign-off"), {"snapshot_id": str(review.snapshot_id), "version": shown, "signed_on": today.isoformat(),
                                              "approver_name": "Pat Lee", "approver_title": "VP, Support Services"}, **HX)
    body = html(r)
    assert CHANGED in body and "Open the review again" in body and 'type="submit">Sign off<' not in body
    review.refresh_from_db()
    assert review.status == ReviewStatus.DRAFT and review.signed_on is None and review.signed_by is None
    assert review.approver_name == "" and review.approver_title == ""  # the names and the sign-off: both or neither
    assert (review.overall, review.decision, review.rating_quality) == (Overall.DOES_NOT_MEET, Decision.REPLACE, Rating.NOT_MET)
    body = _sign_modal(client, review)  # read again: the new values, with their version
    assert "Does not meet expectations" in body and "Quality of work: not met" in body
    assert shown and _version_in(body) == vr.fields_version(review) != shown and vr.REVIEW_CHANGED == CHANGED


@pytest.mark.parametrize("field, value", [("summary", "Changed meanwhile."), ("plan_due_on", None), ("uptime_reported_pct", "97.5"),
                                          ("vendor_report_reference", "UPT-2026-Q4"), ("stated_in_contract", "partly")])
def test_f3_every_field_the_approver_signs_moves_the_version(ctx, world, review, today, field, value):
    vr.update_review(review, by=None, **READY)
    if field == "plan_due_on":
        vr.update_review(review, by=None, decision=Decision.CORRECTIVE_PLAN)
        value = today + 60 * DAY
    before = vr.fields_version(VendorReview.objects.get(pk=review.pk))
    vr.update_review(review, by=None, **{field: value})
    r = VendorReview.objects.get(pk=review.pk)
    assert vr.fields_version(r) != before
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today, version=before)
    assert e.value.messages == [CHANGED]
    assert VendorReview.objects.get(pk=review.pk).status == ReviewStatus.DRAFT


def test_f3_the_approvers_name_and_title_leave_the_version_as_it_was(client, users, world, review, today):
    vr.update_review(review, by=None, **READY)
    r = VendorReview.objects.get(pk=review.pk)
    before = vr.fields_version(r)
    vr.update_review(r, by=None, approver_name="Somebody Else", approver_title="Interim")
    assert vr.fields_version(VendorReview.objects.get(pk=review.pk)) == before
    client.force_login(users["director"])
    shown = _version_in(_sign_modal(client, review))
    assert shown == before
    r = client.post(url(review, "sign-off"), {"snapshot_id": str(review.snapshot_id), "version": shown, "signed_on": today.isoformat(),
                                              "approver_name": "Pat Lee", "approver_title": "VP, Support Services"}, **HX)
    assert r.status_code == 200 and CHANGED not in html(r)
    review.refresh_from_db()
    assert review.status == ReviewStatus.APPROVED and (review.approver_name, review.approver_title) == ("Pat Lee", "VP, Support Services")


def test_f3_a_post_without_a_version_is_refused(client, users, world, review, today):
    vr.update_review(review, by=None, **READY)
    client.force_login(users["director"])
    for version in (None, "", "0123456789abcdef", "junk"):
        data = {"snapshot_id": str(review.snapshot_id), "signed_on": today.isoformat(), "approver_name": "Pat Lee"}
        if version is not None:
            data["version"] = version
        body = html(client.post(url(review, "sign-off"), data, **HX))
        assert CHANGED in body and 'type="submit">Sign off<' not in body, version
        review.refresh_from_db()
        assert review.status == ReviewStatus.DRAFT and review.approver_name == "", version
    r = client.post(url(review, "sign-off"), {"snapshot_id": str(review.snapshot_id), "signed_on": today.isoformat()})  # scripts off
    assert r.status_code == 400 and VendorReview.objects.get(pk=review.pk).status == ReviewStatus.DRAFT


def test_f3_the_service_without_a_version_still_signs(ctx, world, review, today):
    """None is the seed's and the tests' (nobody was shown a page); a version given is checked."""
    r = vr.update_review(review, by=None, **READY, approver_name="Pat Lee")
    with pytest.raises(ValidationError):
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today, version="")
    r = vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert r.status == ReviewStatus.APPROVED and r.signed_on == today


def test_f3_the_service_signs_with_the_version_shown(ctx, world, review, today):
    r = vr.update_review(review, by=None, **READY, approver_name="Pat Lee")
    r = vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today, version=vr.fields_version(r))
    assert r.status == ReviewStatus.APPROVED


def test_f3_the_modal_shows_the_ratings_the_vendors_figures_and_the_summary(client, users, world, review, today):
    vr.update_review(review, by=None, **{**READY, "rating_invoices": Rating.PARTLY, "rating_user_satisfaction": Rating.NA},
                     stated_in_contract="partly", uptime_target_pct="98", uptime_reported_pct="97.25", vendor_report_reference="UPT-2026-Q3",
                     summary="Responsive; one repeat repair to discuss.")
    client.force_login(users["director"])
    body = _sign_modal(client, review)
    for words in ("Timeliness: met", "Quality of work: met", "Documentation: met", "Staff evidence: met", "User satisfaction: not applicable",
                  "Invoices: partly met", "Expectations stated in the contract: partly", "Uptime: target 98%, reported 97.25%",
                  "Vendor&#x27;s report: UPT-2026-Q3", "Responsive; one repeat repair to discuss.", "Meets expectations; Renew",
                  f'name="version" value="{vr.fields_version(VendorReview.objects.get(pk=review.pk))}"', 'type="submit">Sign off<'):
        assert words in body, words
    vr.update_review(review, by=None, summary="", uptime_target_pct=None, uptime_reported_pct=None, vendor_report_reference="")
    body = _sign_modal(client, review)
    assert "Not written" in body and "Not recorded" in body


def test_f3_another_facilitys_review_has_no_sign_off(client, ctx, other_tenant, span, today, make_user):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="GE", model="Dash 5000", description="Monitor", category="Monitors")
        theirs_asset = Asset.objects.create(tag="THEIRS-1", device_model=model, department=Department.objects.create(name="ICU"))
        contract(PHILIPS, span[0], today + 100 * DAY)
        job(theirs_asset, opened=span[0] + 10 * DAY, completed=span[0] + 12 * DAY)
        theirs = vr.update_review(vr.create_review(vendor_names=[PHILIPS], period_end=span[1], by=None, today=today), by=None, **READY)
        version = vr.fields_version(theirs)
    client.force_login(make_user("director"))
    assert client.get(url(theirs, "sign-off") + f"?snapshot={theirs.snapshot_id}", **HX).status_code == 404
    r = client.post(url(theirs, "sign-off"), {"snapshot_id": str(theirs.snapshot_id), "version": version, "signed_on": today.isoformat(),
                                              "approver_name": "Pat Lee"}, **HX)
    assert r.status_code == 404
    with tenant_context(other_tenant):
        assert VendorReview.objects.get(pk=theirs.pk).status == ReviewStatus.DRAFT


# --- F4: Feb 28 of a leap year ----------------------------------------------------------------------------------------------------

FAR = date(2100, 1, 1)  # a "today" after every day tested here


def _allowed(end: date) -> bool:
    try:
        vr._clean_period_end(end, FAR)
    except ValidationError:
        return False
    return True


def test_f4_leap_feb_28():
    assert vr.leap_feb_28(date(2024, 2, 28)) and vr.leap_feb_28(date(2028, 2, 28)) and vr.leap_feb_28(date(2000, 2, 28))
    for d in (date(2023, 2, 28), date(2025, 2, 28), date(2100, 2, 28), date(2024, 2, 29), date(2024, 2, 27), date(2024, 3, 28)):
        assert not vr.leap_feb_28(d), d


def test_f4_a_period_cannot_end_on_feb_28_of_a_leap_year():
    with pytest.raises(ValidationError) as e:
        vr._clean_period_end(date(2024, 2, 28), FAR)
    assert list(e.value.message_dict) == ["period_end"]
    assert "Feb 29, 2024" in e.value.message_dict["period_end"][0] and "Feb 27" in e.value.message_dict["period_end"][0]
    assert vr._clean_period_end(date(2024, 2, 29), FAR) == (date(2023, 3, 1), date(2024, 2, 29))
    assert vr._clean_period_end(date(2024, 2, 27), FAR) == (date(2023, 2, 28), date(2024, 2, 27))
    assert vr._clean_period_end(date(2025, 2, 28), FAR) == (date(2024, 3, 1), date(2025, 2, 28))
    with pytest.raises(ValidationError):  # the ISO text is refused the same way
        vr._clean_period_end("2024-02-28", FAR)


def _ends():
    """Every day of February and March of 2023-2029, and every month's last day."""
    out = set()
    for year in range(2023, 2030):
        d = date(year, 2, 1)
        while d < date(year, 4, 1):
            out.add(d)
            d += DAY
        for month in range(1, 13):
            out.add(add_months(date(year, month, 1), 1) - DAY)
    return sorted(out)


def test_f4_every_allowed_ends_next_twelve_months_start_the_day_after_it():
    checked = 0
    for e in _ends():
        if not _allowed(e):
            assert vr.leap_feb_28(e), e
            continue
        n = vr.next_period_end(e)
        if not _allowed(n):
            continue
        checked += 1
        assert vr.period_start_of(n) == e + DAY, (e, n)
    assert checked > 400
    assert vr.next_period_end(date(2024, 2, 29)) == date(2025, 2, 28) and vr.next_period_end(date(2023, 2, 28)) == date(2024, 2, 29)


def test_f4_the_presets_offer_feb_29_or_nothing():
    feb_29_2020 = SimpleNamespace(start_on=date(2020, 2, 29), reference="SC-1")
    row = vr.VendorRow(key=KEY, name=PHILIPS, contracts=[feb_29_2020])
    after = views.presets(date(2024, 3, 5), row)  # the contract's anniversary eve is Feb 28, 2024: offered as Feb 29
    values = [p["value"] for p in after]
    assert "2024-02-29" in values and "2024-02-28" not in values
    assert next(p for p in after if p["value"] == "2024-02-29")["period"] == "Mar 1, 2023 to Feb 29, 2024"
    on = views.presets(date(2024, 2, 29), row)  # yesterday and the anniversary eve are Feb 28: Feb 29 has not ended, so neither
    assert [p["value"] for p in on] == ["2024-01-31"]
    on = views.presets(date(2024, 2, 29), None)
    assert [(p["value"], p["label"]) for p in on] == [("2024-01-31", "Last month's end")]
    start_2024 = SimpleNamespace(start_on=date(2024, 2, 29), reference="SC-2")  # its start's eve, Feb 28, 2024
    values = [p["value"] for p in views.presets(date(2024, 3, 10), vr.VendorRow(key=KEY, name=PHILIPS, contracts=[start_2024]))]
    assert values.count("2024-02-29") == 1 and "2024-02-28" not in values
    for today in (date(2024, 2, 29), date(2024, 3, 1), date(2028, 2, 29), date(2028, 3, 15), date(2026, 10, 17)):
        for p in views.presets(today, row):
            assert _allowed(date.fromisoformat(p["value"])), (today, p)


def test_f4_the_twelve_months_after_a_review_ending_feb_29_tile(ctx, dept, pump_model):
    """Stryker's reviews: one ending Feb 29, 2024 signed off; the preset after it ends Feb 28, 2025 and starts Mar 1, 2024; Feb 28, 2024
    is refused when drafting."""
    device = Asset.objects.create(tag="CE-30001", device_model=pump_model, department=dept)
    contract("Stryker", date(2022, 1, 1), date(2027, 12, 31), on=date(2021, 12, 20))
    job(device, vendor="Stryker", opened=date(2023, 6, 1), completed=date(2023, 6, 3))
    with pytest.raises(ValidationError) as e:
        vr.create_review(vendor_names=["Stryker"], period_end=date(2024, 2, 28), by=None)
    assert "period_end" in e.value.message_dict and not VendorReview.objects.exists()
    first = signed(vr.create_review(vendor_names=["Stryker"], period_end=date(2024, 2, 29), by=None), timezone.localdate())
    assert (first.period_start, first.period_end) == (date(2023, 3, 1), date(2024, 2, 29))
    row = next(r for r in vr.vendors_list() if r.key == "stryker")
    offered = next(p for p in views.presets(timezone.localdate(), row) if p["label"] == f"The 12 months after {first.number}")
    assert offered["value"] == "2025-02-28" and offered["period"] == "Mar 1, 2024 to Feb 28, 2025"
    second = vr.create_review(vendor_names=["Stryker"], period_end=date.fromisoformat(offered["value"]), by=None)
    assert (second.period_start, second.period_end) == (date(2024, 3, 1), date(2025, 2, 28))  # no day twice, Feb 29 once


# --- F5: the day typed in Another day ---------------------------------------------------------------------------------------------

def test_f5_the_date_box_is_outside_the_label_and_chooses_its_radio(client, users, world):
    client.force_login(users["manager"])
    body = html(client.get(f"{BASE}new/?vendor=philips", **HX))
    periods = body[body.index('<fieldset class="full vr-periods"'):]
    periods = periods[:periods.index("</fieldset>")]
    labels = re.findall(r"<label\b[^>]*>.*?</label>", periods, flags=re.S)
    assert labels and not any('name="period_end"' in label for label in labels)  # clicking the box never stays a preset's click
    other = next(label for label in labels if 'value="other"' in label)
    assert 'id="vr-period-other"' in other
    box = re.search(r'<input type="date" name="period_end"[^>]*>', periods).group(0)
    assert 'data-chooses="vr-period-other"' in box


def test_f5_cadence_js_chooses_the_radio_named_by_data_chooses():
    js = (APPS / "web" / "static" / "web" / "cadence.js").read_text()
    handler = js[js.index("[data-chooses]") - 200:js.index("[data-chooses]") + 400]
    assert "dataset.chooses" in handler and "getElementById" in handler and ".checked = true" in handler
    assert 'addEventListener("input", choose)' in js and 'addEventListener("change", choose)' in js
    assert 'addEventListener("focusin", choose)' not in js  # tabbing past the box keeps a preset ticked (the fix's own review)
    assert "box.value" in handler  # an empty box never ticks Another day


def test_f5_a_day_typed_while_a_preset_is_ticked_is_refused_with_what_was_typed(client, users, world, span):
    client.force_login(users["manager"])
    typed = ended(span[1] - 39 * DAY).isoformat()
    r = client.post(f"{BASE}new/", {"vendor": "philips", "names": [PHILIPS], "other_names": ["", "", ""], "period": span[1].isoformat(),
                                    "period_end": typed}, **HX)
    body = html(r)
    assert r.status_code == 200 and not VendorReview.objects.exists()
    assert "You typed a day but another choice is ticked: tick Another day to use the day typed, or clear it." in body
    assert list(r.context["form"].errors) == ["period_end"]
    assert f'name="period_end" value="{typed}"' in body
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": span[1].isoformat(), "period_end": typed})  # scripts off: the tab
    assert r.status_code == 400 and not VendorReview.objects.exists()
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": "other", "period_end": typed}, **HX)  # Another day ticked: that day
    review = VendorReview.objects.get()
    assert r["HX-Redirect"] == url(review) and review.period_end == date.fromisoformat(typed)


def test_f5_a_preset_with_the_same_day_or_a_blank_box_still_drafts(client, users, world, span):
    client.force_login(users["manager"])
    r = client.post(f"{BASE}new/", {"names": [PHILIPS], "period": span[1].isoformat(), "period_end": span[1].isoformat()}, **HX)
    philips = VendorReview.objects.get(vendor_key=KEY)
    assert r["HX-Redirect"] == url(philips) and (philips.period_start, philips.period_end) == span
    r = client.post(f"{BASE}new/", {"names": [TECHCARE], "period": span[1].isoformat(), "period_end": ""}, **HX)
    tech = VendorReview.objects.get(vendor_key="techcare biomedical")
    assert r["HX-Redirect"] == url(tech) and tech.period_end == span[1]


# --- F6: the change log, Contracts View, and staff-written text -------------------------------------------------------------------

@pytest.fixture
def reviewed(world, review, today):
    """The review with its summary written, signed off, and reported to the quality committee."""
    vr.update_review(review, by=None, summary=SUMMARY_WORDS)
    r = signed(VendorReview.objects.get(pk=review.pk), today)
    vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, minutes_reference="QC-77", by=None, today=today)
    return r


@pytest.fixture
def no_contracts(tenant):
    """Users and access View and Evaluations View, Contracts None, not scoped (a quality role auditing access)."""
    return custom_user(tenant, "quality-auditor", {Module.USERS: Level.VIEW, Module.EVALUATIONS: Level.VIEW, Module.REPORTS: Level.VIEW})


@pytest.fixture
def with_contracts(tenant):
    return custom_user(tenant, "contract-auditor", {Module.USERS: Level.VIEW, Module.EVALUATIONS: Level.VIEW, Module.CONTRACTS: Level.VIEW})


def _log_everywhere(client) -> dict:
    """The change log as the signed-in reader gets it: the page, the CSV, the print, and the API."""
    return {"page": html(client.get(LOG)), "csv": csv_text(client.get(CSV)), "print": html(client.get(PRINT)),
            "api": json.dumps(client.get(API).json())}


def test_f6_the_review_areas_need_contracts_view_too(ctx, no_contracts, with_contracts):
    keys = {a.key for a in history.readable_areas(no_contracts)}
    assert "evaluations" in keys and not keys & set(REVIEW_AREAS)
    assert not any(history.can_read(no_contracts, k) for k in REVIEW_AREAS) and history.can_read(no_contracts, "evaluations")
    keys = {a.key for a in history.readable_areas(with_contracts)}
    assert set(REVIEW_AREAS) <= keys and all(history.can_read(with_contracts, k) for k in REVIEW_AREAS)
    assert all(history.ALL_AREAS[k].also == ("contracts",) for k in REVIEW_AREAS)


def test_f6_without_contracts_view_the_change_log_never_shows_a_review(client, ctx, reviewed, no_contracts):
    client.force_login(no_contracts)
    assert client.get(url(reviewed)).status_code == 403  # as the review's own page
    for where, text in _log_everywhere(client).items():
        for never in (reviewed.number, SUMMARY_WORDS, "QC-77", "Vendor review"):
            assert never not in text, (where, never)
    for area in REVIEW_AREAS:
        r = client.get(f"{API}?area={area}")
        assert r.status_code == 400 and "area" in r.json(), area
        page = html(client.get(f"{LOG}?area={area}"))
        assert reviewed.number not in page and SUMMARY_WORDS not in page
    assert client.get(url(reviewed) + "?history=1", **part("hist")).status_code == 403


def test_f6_with_contracts_view_the_reviews_show_but_never_the_summarys_words(client, ctx, reviewed, with_contracts):
    client.force_login(with_contracts)
    note = f"Summary {history.PRIVATE_NOTE}"
    assert note == "Summary edited (the words are on its own page)"
    logs = _log_everywhere(client)
    for where, text in logs.items():
        assert reviewed.number in text and "QC-77" in text, where
        assert SUMMARY_WORDS not in text and "Zq7Canary" not in text, where
        assert note in text, where
    entries = client.get(f"{API}?area=vendor_reviews").json()["results"]
    edited = [e for e in entries if note in e["reason"]]
    assert len(edited) == 1 and all(c["field"] != "Summary" for e in entries for c in e["changes"])
    own = html(client.get(url(reviewed) + "?history=1", **part("hist")))  # the review's own History keeps the words
    assert SUMMARY_WORDS in own and history.PRIVATE_NOTE not in own
    assert any(c.field == "Summary" and c.after == SUMMARY_WORDS for e in history.entries_for(reviewed) for c in e.changes)


def test_f6_a_program_evaluations_narrative_never_leaves_in_the_change_log(client, ctx, users, today):
    ev = draft(start=last_year(today)[0], today=today)
    ev_services.update_text(ev, "went_well", NARRATIVE_WORDS, by=None)
    ev_services.update_text(ev, "summary", NARRATIVE_WORDS + " (summary)", by=None)
    client.force_login(users["director"])
    logs = _log_everywhere(client)
    for where, text in logs.items():
        assert ev.number in text and "Zq7Canary" not in text, where
        assert f"What went well {history.PRIVATE_NOTE}" in text and f"Summary {history.PRIVATE_NOTE}" in text, where
    own = [c.after for e in history.entries_for(ev) for c in e.changes]
    assert NARRATIVE_WORDS in own and NARRATIVE_WORDS + " (summary)" in own


def test_f6_other_changes_of_the_same_save_still_show(ctx, world, review, today, with_contracts):
    """A save that changes the summary and another field: the other field's change shows, the summary's only as edited."""
    vr.update_review(review, by=None, summary=SUMMARY_WORDS, overall=Overall.PARTIALLY)
    entries, _more = history.change_log(with_contracts, areas=["vendor_reviews"])
    (both,) = [e for e in entries if any(c.field == "Overall" for c in e.changes)]
    assert [(c.field, c.after) for c in both.changes] == [("Overall", "Partially meets expectations")]
    assert both.reason == f"Summary {history.PRIVATE_NOTE}"


@needs_postgres
def test_f6_the_change_log_under_the_policies(client, ctx, reviewed, no_contracts, with_contracts):
    client.force_login(no_contracts)
    as_app_role()
    assert reviewed.number not in html(client.get(LOG)) and client.get(f"{API}?area=vendor_reviews").status_code == 400
    client.force_login(with_contracts)
    page = html(client.get(LOG))
    assert reviewed.number in page and SUMMARY_WORDS not in page and f"Summary {history.PRIVATE_NOTE}" in page
    assert SUMMARY_WORDS not in csv_text(client.get(CSV))


# --- F7: the PM target's words ----------------------------------------------------------------------------------------------------

def test_f7_a_pm_target_of_97_1_reads_97_1_percent(client, users, world, span, today):
    settings_on(span[0] - 10 * DAY, target_pm_pct=Decimal("97.1"))
    r = vr.create_review(vendor_names=[PHILIPS], period_end=span[1], by=None, today=today)
    assert r.snapshot.data["settings"]["pm_targets"]["other"] == 97.1
    client.force_login(users["analyst"])
    for body in (html(client.get(url(r))), html(client.get(url(r), **part("vr-figures")))):
        assert "and 97.1% for the rest" in body and "97.0999" not in body
    assert views._number(97.1) == "97.1" and views._number(95.3) == "95.3" and views._number(100.0) == "100"
    assert views._number(Decimal("97.50")) == "97.5" and views._pct(92.3) == "92.3%"


# --- F8: the module's label in every refusal --------------------------------------------------------------------------------------

def test_f8_the_program_evaluation_api_refusals_say_evaluations(client, users):
    client.force_login(users["technician"])  # Evaluations None
    r = client.get("/api/v1/program-evaluations/")
    assert r.status_code == 403 and r.json()["detail"] == "Reading program evaluations needs Evaluations View."
    assert api_evaluations.VIEW_REFUSAL == "Reading program evaluations needs Evaluations View."
    client.force_login(users["analyst"])  # Evaluations View: a write is refused naming Edit
    r = client.post("/api/v1/program-evaluations/", {}, content_type="application/json")
    assert r.status_code == 403 and r.json()["detail"].endswith("needs Evaluations Edit.")


def test_f8_the_services_refusals_say_evaluations(ctx, tenant, users, today):
    with pytest.raises(PermissionDenied) as e:
        ev_services.create_draft(period_start=last_year(today)[0], by=users["technician"], today=today)
    assert str(e.value) == "Drafting and writing a program evaluation needs Evaluations Edit."
    ev = draft(today=today)
    writer = custom_user(tenant, "writer", {Module.EVALUATIONS: Level.EDIT, Module.REPORTS: Level.VIEW})
    with pytest.raises(PermissionDenied) as e:
        ev_services.record_approval(ev, body="eoc", meeting_on=today, by=writer, today=today)
    assert str(e.value) == "Recording a committee's approval, or amending an approved evaluation, needs Evaluations Approve."
    assert "Evaluations Edit" in vr.EDIT_PERMISSION and "Evaluations Approve" in vr.APPROVE_PERMISSION


def test_f8_nothing_says_program_evaluation_view_edit_or_approve():
    stale = re.compile(r"Program evaluation (View|Edit|Approve)")
    found = [str(p.relative_to(APPS)) for p in APPS.rglob("*") if p.suffix in (".py", ".html", ".js", ".txt")
             and stale.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert found == []
    assert Module.EVALUATIONS.label == "Evaluations"


# --- F9: the history's words ------------------------------------------------------------------------------------------------------

def _changes(entry) -> list[tuple]:
    return [(c.field, c.before, c.after) for c in entry.changes]


def test_f9_vendor_names_and_percentages_in_words(ctx, world, span, today):
    contract("Philips Healthcare", span[0], today + 50 * DAY)
    r = vr.create_review(vendor_names=[PHILIPS, "Philips Healthcare"], period_end=span[1], by=None, today=today)
    drafted = history.entries_for(r)[-1]
    assert ("Vendor names", "", "Philips, Philips Healthcare") in _changes(drafted)
    assert not any("[" in c.after for c in drafted.changes)
    vr.update_review(r, by=None, uptime_target_pct="98", uptime_reported_pct="97.25", uptime_lowest_pct="90", response_target_pct="95",
                     response_reported_pct="96.5")
    (saved,) = history.entries_for(VendorReview.objects.get(pk=r.pk))[:1]
    assert {(f, b, a) for f, b, a in _changes(saved)} == {
        ("Uptime target", history.EMPTY, "98%"), ("Uptime reported by the vendor", history.EMPTY, "97.25%"),
        ("Lowest system's uptime", history.EMPTY, "90%"), ("Response target", history.EMPTY, "95%"),
        (history.LABELS["evaluations.vendorreview"]["response_reported_pct"], history.EMPTY, "96.5%")}
    vr.update_review(r, by=None, uptime_target_pct="99.5")
    assert _changes(history.entries_for(VendorReview.objects.get(pk=r.pk))[0]) == [("Uptime target", "98%", "99.5%")]


def test_f9_a_reports_body_reads_committee(client, users, world, review, today):
    r = signed(review, today)
    (rep,) = vr.record_report([r], body=ReportBody.GOVERNING, meeting_on=today, by=None, today=today)
    vr.correct_report(rep, by=None, today=today, body=ReportBody.QUALITY)
    (corrected,) = history.entries_for(rep)[:1]
    assert _changes(corrected) == [("Committee", "Governing body", "Quality oversight (QAPI)")]
    added = history.entries_for(rep)[-1]
    assert "Committee" in [c.field for c in added.changes] and "Body" not in [c.field for c in added.changes]
    client.force_login(users["director"])
    body = html(client.get(url(r) + "?history=1", **part("hist")))
    assert "Committee" in body and "Body:" not in body


# --- F10: the summary print's rule ------------------------------------------------------------------------------------------------

def test_f10_the_summary_print_states_the_renewal_exception(client, users, world, review, today):
    """The finding's case: the Philips contract ends before the next review's day; the review whose 12 months ended within the year
    before the contract's end covers its renewal, so Next due is the review's, and the sentence says why."""
    signed(review, today)
    client.force_login(users["analyst"])
    printed = html(client.get(f"{BASE}summary/print/"))
    assert (f"A review is due {vr.DUE_AFTER_DAYS} days after the 12 months after the last signed-off review end, or by the end of a "
            "contract in force, whichever comes first (a review whose 12 months ended within the year before that contract ends covers "
            "its renewal).") in printed
    row = printed[printed.index(f"<b>{PHILIPS}</b>"):]
    row = row[:row.index("</tr>")]
    due = vr.next_period_end(review.period_end) + timedelta(days=vr.DUE_AFTER_DAYS)
    assert world["philips"].end_on < due and day(due) in row


# --- F11: repeat repairs' detail --------------------------------------------------------------------------------------------------

def test_f11_the_detail_counts_repairs_on_one_side_and_later_repairs_on_the_other(ctx, today, fig_span, monitors):
    """The finding's scenario: one Philips repair, then two later repairs of the device within 30 days, one with no finding and one
    not stated by the vendor."""
    S, E = fig_span
    m1 = monitors["m1"]
    job(m1, opened=S + 10 * DAY, completed=S + 15 * DAY)
    job(m1, vendor="", opened=S + 20 * DAY, completed=S + 21 * DAY, finding="")
    job(m1, vendor="", opened=S + 25 * DAY, completed=S + 26 * DAY, finding=RepairFinding.NOT_STATED)
    data = take(fig_span, today)
    f = fig(data, "repeat_repairs")
    assert nd(f) == (1, 1) and f["detail"] == "1 of 1 repair; 2 later repairs not recorded"
    assert nd(fig(data, "repeat_not_recorded")) == (2, 2)


def test_f11_one_later_repair_reads_in_the_singular(ctx, today, fig_span, monitors):
    S, E = fig_span
    job(monitors["m1"], opened=S + 10 * DAY, completed=S + 15 * DAY)
    job(monitors["m2"], opened=S + 10 * DAY, completed=S + 15 * DAY)
    job(monitors["m1"], vendor="", opened=S + 20 * DAY)  # still open: unknown
    job(monitors["m2"], vendor="", opened=S + 20 * DAY, completed=S + 21 * DAY, finding=RepairFinding.BATTERY)  # recorded
    f = fig(take(fig_span, today), "repeat_repairs")
    assert nd(f) == (2, 2) and f["detail"] == "2 of 2 repairs; 1 later repair not recorded"


# --- F12: a block's change reason -------------------------------------------------------------------------------------------------

LABELS = history.LABELS["evaluations.vendorreview"]


def _whole_words(reason: str, labels: list[str]) -> bool:
    """The reason names whole labels, in order, maybe ending "and N more" for the rest."""
    if not reason.startswith("Changed: "):
        return reason in (f"{labels[0]} changed", f"Changed {len(labels)} fields")
    parts = reason.removeprefix("Changed: ").split(", ")
    tail = re.fullmatch(r"and (\d+) more", parts[-1])
    named = parts[:-1] if tail else parts
    return named == labels[:len(named)] and (int(tail.group(1)) if tail else 0) == len(labels) - len(named)


def test_f12_saving_the_whole_ratings_block_stores_a_reason_that_fits(client, editor, world, review):
    client.force_login(editor)
    data = {**{f: "met" for f in CRITERIA_FIELDS}, "stated_in_contract": "partly"}
    assert client.post(url(review, "fields", "ratings"), data, **HX).status_code == 200
    reason = reasons(review)[-1]
    labels = [LABELS[f] for f in (*CRITERIA_FIELDS, "stated_in_contract")]
    assert len(f"Changed: {', '.join(labels)}") > vr.CHANGE_REASON_MAX  # the whole list would not fit
    assert len(reason) <= vr.CHANGE_REASON_MAX and _whole_words(reason, labels), reason
    assert reason == "Changed: Timeliness, Quality of work, Documentation, Staff evidence, User satisfaction, and 2 more"
    (entry,) = history.entries_for(VendorReview.objects.get(pk=review.pk))[:1]
    assert entry.reason == "" and len(entry.changes) == 7  # the entry lists every change; the reason repeats nothing
    body = html(client.get(url(review) + "?history=1", **part("hist")))
    assert "Changed:" not in body and "and 2 more" not in body


def test_f12_the_vendors_figures_block_too(client, editor, world, review):
    client.force_login(editor)
    data = {"uptime_target_pct": "98", "uptime_reported_pct": "97.25", "uptime_lowest_pct": "90", "systems_below": "1", "systems_total": "5",
            "response_target_pct": "95", "response_reported_pct": "96", "vendor_report_reference": "UPT-2026-Q3"}
    assert client.post(url(review, "fields", "reported"), data, **HX).status_code == 200
    reason = reasons(review)[-1]
    fields = ("uptime_target_pct", "uptime_reported_pct", "uptime_lowest_pct", "systems_below", "systems_total", "response_target_pct",
              "response_reported_pct", "vendor_report_reference")
    stored = VendorReview.history.filter(id=review.pk).order_by("-history_date", "-history_id").first()
    assert len(reason) <= vr.CHANGE_REASON_MAX and reason.endswith(" more") and stored.history_change_reason == reason
    assert _whole_words(reason, [LABELS[f] for f in fields]), reason
    assert history.entries_for(VendorReview.objects.get(pk=review.pk))[0].reason == ""


def test_f12_the_reason_rule():
    assert vr._changed_reason(["Overall"]) == "Overall changed"
    assert vr._changed_reason(["Overall", "Decision"]) == "Changed: Overall, Decision"
    long = [f"Field number {i} with a long label" for i in range(10)]
    reason = vr._changed_reason(long)
    assert len(reason) <= vr.CHANGE_REASON_MAX and _whole_words(reason, long)
    huge = ["x" * 95, "y" * 95]
    assert vr._changed_reason(huge) == "Changed 2 fields"


def test_f12_history_drops_a_reason_that_only_names_the_change(ctx, world, review):
    vr.update_review(review, by=None, overall=Overall.MEETS)
    assert reasons(review)[-1] == "Overall changed"
    assert history.entries_for(VendorReview.objects.get(pk=review.pk))[0].reason == ""
    vr.update_review(review, by=None, overall=Overall.PARTIALLY, decision=Decision.RENEW_CHANGES)
    assert reasons(review)[-1] == "Changed: Overall, Decision"
    assert history.entries_for(VendorReview.objects.get(pk=review.pk))[0].reason == ""


# --- F13: the contracts table's day -----------------------------------------------------------------------------------------------

def test_f13_the_devices_column_names_the_day_the_figures_were_taken(client, users, world, review, today):
    taken = review.snapshot.taken_on
    VendorReview.snapshot.field.related_model.objects.filter(pk=review.snapshot_id).update(taken_on=taken - 20 * DAY)  # an older copy
    client.force_login(users["analyst"])
    for body in (html(client.get(url(review))), html(client.get(url(review), **part("vr-figures"))), html(client.get(url(review, "print")))):
        assert f'<th class="num">Devices on {day(taken - 20 * DAY)}</th>' in body
        assert "Devices today" not in body and "SC-PHIL" in body


# --- a second facility ------------------------------------------------------------------------------------------------------------

def test_a_second_facility_never_sees_the_firsts(client, ctx, tenant, other_tenant, today, monitors, reviewed, make_user):
    """The first facility: imported vendor work, a covered PM inside a 45-day window, a signed-off review with its summary and a report,
    a program evaluation's narrative. The second: none of it in its figures, its reviews, or its change log."""
    S, E = _window_45(today)
    c = contract(PHILIPS, S - 100 * DAY, today + 200 * DAY, reference="SC-OURS")
    cs.add_asset(c, monitors["m1"])
    job(monitors["m1"], vendor="", type_=WoType.PM, opened=E - 30 * DAY, due=E - DAY)
    imported(monitors["m2"], "OLD-PM-1", type_=WoType.PM, opened=S, due=S + 20 * DAY, completed=S + 60 * DAY, vendor=PHILIPS)
    ev = draft(today=today)
    ev_services.update_text(ev, "went_well", NARRATIVE_WORDS, by=None)
    ours = take((S, E), today)
    assert fig(ours, "jobs_imported")["value"] == 1 and ours["provisional_until"] is not None

    create_default_roles(other_tenant)
    theirs_director = make_user("director", tenant_=other_tenant, username="director@other.example")
    with tenant_context(other_tenant):
        theirs = take((S, E), today)
        assert fig(theirs, "jobs_imported")["value"] == 0 and theirs["provisional_until"] is None and theirs["contracts"] == []
        assert not VendorReview.objects.exists() and vr.vendors_list(today) == []
        entries = history.change_log(theirs_director)[0]
        assert not [e for e in entries if e.area in (*REVIEW_AREAS, "evaluations", "work_orders", "contracts")]
    client.force_login(theirs_director)
    for where, text in _log_everywhere(client).items():
        for never in (reviewed.number, ev.number, SUMMARY_WORDS, "Zq7Canary", "QC-77", history.PRIVATE_NOTE):
            assert never not in text, (where, never)
    assert client.get(url(reviewed)).status_code == 404 and client.get(url(reviewed, "sign-off"), **HX).status_code == 404
    assert reviewed.number not in html(client.get(BASE)) and reviewed.number not in html(client.get(f"{BASE}summary/print/"))
