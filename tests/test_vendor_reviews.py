"""
Slice 34, wave 1A: the review of each contracted service vendor (apps/evaluations/vendor_reviews.py): every write and the read helpers.

Drafting: the names (cleaned, one per vendor key, the first shown), each key naming a contract in force on a day of the period (from
Contract history: deleted contracts, renamed vendors, a contract entered after the period, a typo fixed the day it was entered) or a
vendor job completed in it; the period (12 months ending on a day that has ended, month ends tiling); no key in two live reviews with
overlapping periods; the number from the period's end; the prefill from the previous signed-off review of the same vendor; the first
figures taken. Refresh (a draft only, refused when the figures were taken again or it was signed off meanwhile). The reviewer's fields
(only those given, each rule). The sign-off (the figures seen, every rule, the partial unique constraint in words), the plan's result,
withdraw and discard, the committee reports (several at once, one per review, body, and day; corrected; removed). The read helpers
(vendors_list's rows and next due, previous_approved, current). The levels (the module's label "Evaluations" and Role.levels unchanged;
Edit, Approve, Contracts View, the figures' gate, never a scoped user). Tenant isolation for the three models. On PostgreSQL: two drafts
of one vendor at once, a refresh holding the review while the sign-off waits, two committee batches at once, and the services under the
policies.

The figures are vendor_figures.snapshot's (wave 1B, tests/test_vendor_figures.py): stood in for here, through the module, so these tests
count only the review's own rules.
"""
import importlib
import itertools
import sys
import threading
import time as time_module
import types
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

import apps.evaluations as evaluations_package
from apps.accounts.models import DEFAULT_LEVELS, Level, Module, Role, User
from apps.contracts import services as contract_services
from apps.contracts.models import Contract
from apps.evaluations import permissions as perms
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import (
    CRITERIA_FIELDS,
    REVIEW_SUMMARY_MAX,
    Decision,
    Overall,
    PlanResult,
    Rating,
    ReportBody,
    ReviewStatus,
    StatedInContract,
    VendorReview,
    VendorReviewReport,
    VendorReviewSnapshot,
)
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

DAY = timedelta(days=1)
PHILIPS = "Philips"
_refs = itertools.count(1)


def aware(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class Figures:
    """vendor_figures.snapshot stood in for: each call recorded, its data naming the keys, the period, and the call's number."""

    def __init__(self, module, real):
        self.module, self.real, self.calls = module, real, []

    def snapshot(self, keys, start, end, *, today):
        self.calls.append((list(keys), start, end, today))
        return {"schema": 1, "keys": list(keys), "period": [start.isoformat(), end.isoformat()], "call": len(self.calls)}


@pytest.fixture(autouse=True)
def figures(monkeypatch):
    """Wave 1B's module when it is there (its real snapshot kept as `real`), else a module of our own in its place."""
    try:
        module = importlib.import_module("apps.evaluations.vendor_figures")
        real = getattr(module, "snapshot", None)
    except ModuleNotFoundError:
        module, real = types.ModuleType("apps.evaluations.vendor_figures"), None
        monkeypatch.setitem(sys.modules, "apps.evaluations.vendor_figures", module)
        monkeypatch.setattr(evaluations_package, "vendor_figures", module, raising=False)
    stand_in = Figures(module, real)
    monkeypatch.setattr(module, "snapshot", stand_in.snapshot, raising=False)
    return stand_in


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    """The 12 months ending at the end of last month: (start, end)."""
    end = today.replace(day=1) - DAY
    return vr.period_start_of(end), end


@pytest.fixture
def director(make_user):
    return make_user("director")


@pytest.fixture
def manager(make_user):
    return make_user("manager")


def custom_role(name, levels: dict) -> Role:
    role = Role.objects.create(name=name, slug=name.lower().replace(" ", "-"))
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return Role.objects.get(pk=role.pk)


def user_with(tenant, name, levels: dict) -> User:
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                    role=custom_role(name.title(), levels))


EVERY_VIEW = {m: Level.VIEW for m in Module.values}


def job(asset, vendor, done, *, status=WoStatus.COMPLETED, source=Source.MANUAL, vendor_service=True, type_=WoType.REPAIR):
    """A work order on vendor service (unless said otherwise) completed on `done`."""
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Broken", opened_on=done - 3 * DAY, source=source)
    finished = status in (WoStatus.COMPLETED, WoStatus.CLOSED)
    WorkOrder.objects.filter(pk=wo.pk).update(status=status, completed_on=done if finished else None, vendor_service=vendor_service,
                                              vendor_name=vendor)
    return wo


def date_last_version(contract_id, day):
    """Date a contract's last history row (its last save, or its deletion: history ids count up) on `day`, an hour after the row
    before it when that one is on the same day."""
    rows = list(Contract.history.filter(id=contract_id).order_by("-history_id")[:2])
    hour = 9
    if len(rows) == 2 and timezone.localdate(rows[1].history_date) == day:
        hour = timezone.localtime(rows[1].history_date).hour + 1
    Contract.history.filter(history_id=rows[0].history_id).update(history_date=aware(day, hour))


def contract(vendor, start_on, end_on, *, entered=None, cost="12000.00"):
    """A contract through the services, its first history row dated `entered` (default: before its term)."""
    c = contract_services.create_contract(reference=f"SC-{next(_refs):04d}", vendor=vendor, start_on=start_on, end_on=end_on,
                                          annual_cost=Decimal(cost))
    date_last_version(c.pk, entered or start_on - 10 * DAY)
    return c


def review(span, names=(PHILIPS,), *, by=None, today=None):
    return vr.create_review(vendor_names=list(names), period_end=span[1], by=by, today=today)


READY = {**{f: Rating.MET for f in CRITERIA_FIELDS}, "overall": Overall.MEETS, "decision": Decision.RENEW, "approver_name": "Pat Lee",
         "approver_title": "VP, Support Services"}


def ready(r, *, by=None, **over):
    """Every field the sign-off needs."""
    return vr.update_review(r, by=by, **{**READY, **over})


def signed(r, *, today, by=None, signed_on=None, **over):
    """Ready and signed off on `signed_on` (today by default), with the figures it holds."""
    r = ready(r, by=by, **over)
    return vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=signed_on or today, by=by, today=today)


@pytest.fixture
def philips(span, pump):
    """A Philips vendor job completed in the period."""
    return job(pump, PHILIPS, span[0] + 40 * DAY)


def reasons(r) -> list:
    return list(VendorReview.history.filter(id=r.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


# --- the module, the levels, and the facility's walls ----------------------------------------------------------------------------

def test_the_module_is_named_evaluations_and_the_levels_are_unchanged(ctx):
    assert (Module.EVALUATIONS.value, Module.EVALUATIONS.label) == ("evaluations", "Evaluations")
    for role in Role.objects.filter(slug__in=list(DEFAULT_LEVELS)):
        assert role.levels() == {m: DEFAULT_LEVELS[role.slug].get(m, Level.NONE) for m in Module.values}, role.slug
    levels = {r.slug: (r.level_for(Module.EVALUATIONS), r.level_for(Module.CONTRACTS)) for r in Role.objects.all()}
    assert levels == {"director": (Level.FULL, Level.FULL), "manager": (Level.APPROVE, Level.EDIT), "technician": (Level.NONE, Level.VIEW),
                      "requester": (Level.NONE, Level.NONE), "analyst": (Level.VIEW, Level.VIEW), "vendor": (Level.NONE, Level.NONE)}


def test_who_may_do_what(ctx, make_user):
    director, manager, analyst, technician = (make_user(s) for s in ("director", "manager", "analyst", "technician"))
    assert all(f(director) for f in (perms.can_view_reviews, perms.can_edit_reviews, perms.can_approve_reviews, perms.can_see_vendor_figures))
    assert all(f(manager) for f in (perms.can_view_reviews, perms.can_edit_reviews, perms.can_approve_reviews, perms.can_see_vendor_figures))
    assert perms.can_view_reviews(analyst) and perms.can_see_vendor_figures(analyst) and not perms.can_edit_reviews(analyst)
    assert not perms.can_view_reviews(technician)  # Contracts View, no Evaluations


def test_the_three_models_stay_in_their_facility(ctx, today, span, philips, other_tenant, pump_model):
    r = signed(review(span, today=today), today=today)
    vr.record_report([r], body=ReportBody.GOVERNING, meeting_on=today, by=None, today=today)
    assert (VendorReview.objects.count(), VendorReviewSnapshot.objects.count(), VendorReviewReport.objects.count()) == (1, 1, 1)
    with tenant_context(other_tenant):
        assert not VendorReview.objects.exists() and not VendorReviewSnapshot.objects.exists() and not VendorReviewReport.objects.exists()
        assert vr.current(r.number) is None and vr.vendors_list(today) == []
        with pytest.raises(ValidationError) as e:  # our Philips job is not theirs
            review(span, today=today)
        assert "names Philips" in e.value.message_dict["vendor_names"][0]
        from apps.equipment.models import Asset, Department, DeviceModel

        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", category="Infusion pumps")
        theirs = Asset.objects.create(tag="OT-1", device_model=model, department=Department.objects.create(name="ER"))
        job(theirs, PHILIPS, span[0] + 5 * DAY)
        mine = review(span, today=today)  # the same vendor and months: another facility's review never overlaps ours
        assert mine.number == r.number  # numbers repeat across facilities
        assert VendorReview.objects.get() == mine and VendorReviewSnapshot.objects.get().review == mine
        with pytest.raises(VendorReview.DoesNotExist):
            vr.refresh(r, by=None, today=today)
    assert VendorReview.objects.get() == r and vr.current(r.number) == r


# --- drafting ---------------------------------------------------------------------------------------------------------------------

def test_a_draft_is_numbered_by_its_period_end_and_takes_its_figures(ctx, today, span, philips, director, figures):
    job(philips.asset, "Philips Healthcare", span[0] + 50 * DAY)
    r = vr.create_review(vendor_names=["  Philips ", "PHILIPS  field service", "Philips Healthcare"], period_end=span[1], by=director,
                         today=today)
    assert r.vendor_names == ["Philips", "Philips Healthcare"] and r.vendor_keys == ["philips", "philips healthcare"]
    assert (r.vendor_key, r.vendor, r.period_start, r.period_end) == ("philips", "Philips", span[0], span[1])
    assert (r.status, r.created_by, r.number) == (ReviewStatus.DRAFT, director, f"VR-{span[1].year % 100:02d}-0001")
    snap = r.snapshot
    assert (snap.review, snap.taken_on, snap.taken_by) == (r, today, director)
    assert figures.calls == [(["philips", "philips healthcare"], span[0], span[1], today)] and snap.data["call"] == 1
    assert reasons(r) == ["Drafted", "Figures taken"]
    job(philips.asset, PHILIPS, add_months(span[1], -24) - 10 * DAY)
    two_back = vr.create_review(vendor_names=[PHILIPS], period_end=add_months(span[1], -24), by=None, today=today)
    assert two_back.number == f"VR-{(span[1].year - 2) % 100:02d}-0001"  # each year of the period's end counts on its own
    job(philips.asset, "GE Healthcare", span[0] + 5 * DAY)
    assert vr.create_review(vendor_names=["GE Healthcare"], period_end=span[1], by=None, today=today).number.endswith("-0002")


@pytest.mark.parametrize("end,start", [
    (date(2025, 2, 28), date(2024, 3, 1)),  # month ends tile: Mar 1, 2024 to Feb 28, 2025
    (date(2024, 2, 29), date(2023, 3, 1)),
    (date(2026, 2, 28), date(2025, 3, 1)),
    (date(2025, 12, 31), date(2025, 1, 1)),
    (date(2026, 6, 14), date(2025, 6, 15)),  # a contract's anniversary
])
def test_the_period_is_the_12_months_ending_on_its_end(end, start):
    assert vr.period_start_of(end) == start
    assert vr.next_period_end(vr.period_start_of(end) - DAY) == end


@pytest.mark.parametrize("end,words", [
    (0, "12 months that have ended"),
    (5, "12 months that have ended"),
    ("2025-13-01", "Enter the last day"),
    ("soon", "Enter the last day"),
    (date(2000, 6, 30), "from 2000 on"),
    (date(1, 1, 1), "from 2000 on"),
    (None, "Enter the last day"),
])
def test_the_period_has_ended_before_it_is_drafted(ctx, today, philips, end, words):
    if isinstance(end, int):
        end = today + end * DAY
    with pytest.raises(ValidationError) as e:
        vr.create_review(vendor_names=[PHILIPS], period_end=end, by=None, today=today)
    assert words in e.value.message_dict["period_end"][0]


def test_yesterday_and_an_iso_day_are_accepted(ctx, today, pump):
    job(pump, PHILIPS, today - 3 * DAY)
    r = vr.create_review(vendor_names=PHILIPS, period_end=(today - DAY).isoformat(), by=None, today=today)
    assert (r.period_end, r.vendor_names) == (today - DAY, [PHILIPS])


@pytest.mark.parametrize("names,words", [
    ([], "Enter the vendor"),
    (["  ", ""], "Enter the vendor"),
    (None, "Enter the vendor"),
    (42, "Enter the vendor"),
    (["P" * 121], "120 characters"),
    ([f"Vendor {i}" for i in range(11)], "at most 10 spellings"),
])
def test_the_names_are_checked(ctx, today, span, names, words):
    with pytest.raises(ValidationError) as e:
        vr.create_review(vendor_names=names, period_end=span[1], by=None, today=today)
    assert words in e.value.message_dict["vendor_names"][0]


def test_names_and_period_are_refused_together(ctx, today):
    with pytest.raises(ValidationError) as e:
        vr.create_review(vendor_names=[], period_end=today, by=None, today=today)
    assert set(e.value.message_dict) == {"vendor_names", "period_end"}


def test_a_name_must_name_a_contract_or_a_job_of_the_period(ctx, today, span, philips, figures):
    with pytest.raises(ValidationError) as e:
        review(span, ["Philips", "Phillips", "Siemns"], today=today)
    assert e.value.message_dict["vendor_names"] == [f"No contract in force and no vendor job completed from {vr.period_label(*span)} names "
                                                    "Phillips or Siemns: write the vendor as your contracts and work orders do."]
    assert figures.calls == [] and not VendorReview.objects.exists()


@pytest.mark.parametrize("when,status,source,vendor_service,known", [
    (40, WoStatus.COMPLETED, Source.MANUAL, True, True),
    (40, WoStatus.CLOSED, Source.MANUAL, True, True),
    (40, WoStatus.COMPLETED, Source.IMPORTED, True, True),  # imported jobs are the vendor's work too
    (0, WoStatus.COMPLETED, Source.MANUAL, True, True),  # the period's first day
    (-1, WoStatus.COMPLETED, Source.MANUAL, True, False),  # the day before it
    (40, WoStatus.CANCELLED, Source.MANUAL, True, False),
    (40, WoStatus.IN_PROGRESS, Source.MANUAL, True, False),
    (40, WoStatus.COMPLETED, Source.MANUAL, False, False),  # in-house work
])
def test_which_jobs_name_a_vendor(ctx, today, span, pump, when, status, source, vendor_service, known):
    job(pump, "Stryker", span[0] + when * DAY, status=status, source=source, vendor_service=vendor_service)
    if known:
        assert review(span, ["STRYKER  field service"], today=today).vendor_keys == ["stryker"]
    else:
        with pytest.raises(ValidationError):
            review(span, ["Stryker"], today=today)


def test_the_period_end_day_counts_and_after_it_none(ctx, today, span, pump):
    job(pump, "Stryker", span[1] + DAY)
    with pytest.raises(ValidationError):
        review(span, ["Stryker"], today=today)
    job(pump, "Stryker", span[1])
    assert review(span, ["Stryker"], today=today).period_end == span[1]


def test_contracts_name_a_vendor_from_their_history(ctx, today, span):
    S, E = span
    contract("GE Healthcare", S - 400 * DAY, S)  # in force on the period's first day only
    contract("Old Co", S - 400 * DAY, S - DAY)  # ended the day before
    contract("Future Co", E + DAY, E + 400 * DAY)  # starts after it
    gone = contract("Gone Co", S - 400 * DAY, E + 400 * DAY, entered=S - 400 * DAY)  # deleted mid-period: counts until then
    contract_services.delete_contract(Contract.objects.get(pk=gone.pk))
    date_last_version(gone.pk, S + 30 * DAY)
    early = contract("Early Co", S - 400 * DAY, E + 400 * DAY, entered=S - 400 * DAY)  # deleted before the period: never in force in it
    contract_services.delete_contract(Contract.objects.get(pk=early.pk))
    date_last_version(early.pk, S - 5 * DAY)
    renamed = contract("Philips", S - 400 * DAY, E + 400 * DAY, entered=S - 400 * DAY)  # renamed mid-period: both names count
    contract_services.update_contract(Contract.objects.get(pk=renamed.pk), vendor="Philips Healthcare")
    date_last_version(renamed.pk, S + 100 * DAY)
    onboarded = contract("Draeger", S - 400 * DAY, E + 400 * DAY, entered=today)  # entered after the period: its term covers it
    typo = contract("Phillips Typo", S - 400 * DAY, E + 400 * DAY, entered=today)  # fixed the day it was entered: never counted
    contract_services.update_contract(Contract.objects.get(pk=typo.pk), vendor="Hillrom")
    date_last_version(typo.pk, today)
    assert onboarded.pk
    found = vr.contract_vendors(S, E)
    assert found == {"ge healthcare": ["GE Healthcare"], "gone co": ["Gone Co"], "philips": ["Philips"],
                     "philips healthcare": ["Philips Healthcare"], "draeger": ["Draeger"], "hillrom": ["Hillrom"]}
    # A period entirely before the rename knows only the old name
    assert set(vr.contract_vendors(S, S + 99 * DAY)) == {"ge healthcare", "gone co", "philips", "draeger", "hillrom"}
    assert set(vr.contract_vendors(S + 100 * DAY, E)) == {"philips healthcare", "draeger", "hillrom"}
    r = review(span, ["Philips Healthcare", "Philips", "Gone Co"], today=today)
    assert r.vendor_keys == ["philips healthcare", "philips", "gone co"]
    for name in ("Old Co", "Future Co", "Early Co", "Phillips Typo"):
        with pytest.raises(ValidationError):
            review(span, [name], today=today)


def test_a_renewal_in_place_keeps_its_days(ctx, today, span):
    """Renewed in place (the end moved on), each version counts for the days it stood."""
    S, E = span
    c = contract("Siemens Healthineers", S - 300 * DAY, S + 20 * DAY, entered=S - 300 * DAY)
    contract_services.renew_contract(Contract.objects.get(pk=c.pk), today=S + 10 * DAY)
    date_last_version(c.pk, S + 10 * DAY)
    assert vr.contract_vendors(S + 200 * DAY, E) == {"siemens healthineers": ["Siemens Healthineers"]}
    assert vr.contract_vendors(S, S + 5 * DAY) == {"siemens healthineers": ["Siemens Healthineers"]}


def test_contract_history_is_one_query(ctx, today, span):
    S, E = span
    for i in range(4):
        c = contract(f"Vendor {i}", S - 100 * DAY, E + 100 * DAY)
        contract_services.update_contract(Contract.objects.get(pk=c.pk), annual_cost=Decimal("100.00"))
    with CaptureQueriesContext(connection) as q:
        assert len(vr.contract_vendors(S, E)) == 4
    assert len(q.captured_queries) == 1


# --- one live review per vendor and day -------------------------------------------------------------------------------------------

def test_a_vendor_is_in_one_live_review_for_any_day(ctx, today, span, philips, pump):
    first = review(span, today=today)
    job(pump, "Philips Healthcare", span[0] + 60 * DAY)
    with pytest.raises(ValidationError) as e:
        review(span, ["Philips Healthcare", "philips field service"], today=today)
    assert e.value.message_dict["period_end"] == [f"{first.number} (draft) reviews Philips for {vr.period_label(*span)}, which overlaps these "
                                                  "12 months: a vendor is in one review for any day. Finish or discard it, or choose other "
                                                  "months."]
    later_end = today - DAY
    job(pump, PHILIPS, later_end - 5 * DAY)
    with pytest.raises(ValidationError):  # ending later, still overlapping
        vr.create_review(vendor_names=[PHILIPS], period_end=later_end, by=None, today=today)
    other = review(span, ["Philips Healthcare"], today=today)  # another key: its own review
    assert other.vendor_keys == ["philips healthcare"]
    signed(first, today=today)
    with pytest.raises(ValidationError) as e:
        review(span, [PHILIPS], today=today)
    assert "(signed off) reviews Philips" in e.value.message_dict["period_end"][0] and "withdraw it first" in e.value.message_dict["period_end"][0]
    job(pump, PHILIPS, span[0] - 30 * DAY)
    before = vr.create_review(vendor_names=[PHILIPS], period_end=span[0] - DAY, by=None, today=today)  # the 12 months before: adjacent
    assert before.period_end == span[0] - DAY


def test_withdrawn_and_discarded_reviews_free_their_period(ctx, today, span, philips):
    first = signed(review(span, today=today), today=today)
    vr.withdraw(first, by=None)
    second = review(span, today=today)
    vr.discard(second, by=None)
    third = review(span, today=today)
    assert [r.status for r in (VendorReview.objects.get(pk=first.pk), VendorReview.objects.get(pk=second.pk), third)] == [
        ReviewStatus.WITHDRAWN, ReviewStatus.DISCARDED, ReviewStatus.DRAFT]
    signed(third, today=today)  # the partial unique constraint counts signed-off reviews only


# --- the prefill ------------------------------------------------------------------------------------------------------------------

def test_a_draft_is_prefilled_from_the_previous_signed_off_review_of_the_vendor(ctx, today, span, pump):
    prev_end = span[0] - DAY
    job(pump, PHILIPS, prev_end - 20 * DAY)
    job(pump, PHILIPS, span[0] + 20 * DAY)
    job(pump, "Philips Healthcare", span[0] + 30 * DAY)
    older = vr.create_review(vendor_names=[PHILIPS], period_end=prev_end, by=None, today=today)
    signed(older, today=today, rating_invoices=Rating.NA, rating_quality=Rating.PARTLY, stated_in_contract=StatedInContract.PARTLY,
           uptime_target_pct="97.5", response_target_pct=90, uptime_reported_pct="96", summary="Steady year.",
           overall=Overall.PARTIALLY)
    r = review(span, ["Philips Healthcare", "Philips"], today=today)  # the keys intersect
    assert vr.previous_approved(r) == older
    carried = {f: getattr(r, f) for f in vr.CARRIED}
    assert carried == {**{f: Rating.MET for f in CRITERIA_FIELDS}, "rating_invoices": Rating.NA, "rating_quality": Rating.PARTLY,
                       "stated_in_contract": StatedInContract.PARTLY, "uptime_target_pct": Decimal("97.50"),
                       "response_target_pct": Decimal("90.00")}
    assert (r.overall, r.decision, r.summary, r.uptime_reported_pct, r.approver_name) == ("", "", "", None, "")  # never carried
    assert reasons(r) == [f"Drafted, carried from {older.number}", "Figures taken"]


def test_no_prefill_from_a_draft_a_withdrawn_review_or_another_vendor(ctx, today, span, pump):
    prev_end = span[0] - DAY
    for name in (PHILIPS, "GE Healthcare"):
        job(pump, name, prev_end - 20 * DAY)
        job(pump, name, span[0] + 20 * DAY)
    ge = signed(vr.create_review(vendor_names=["GE Healthcare"], period_end=prev_end, by=None, today=today), today=today)
    gone = signed(vr.create_review(vendor_names=[PHILIPS], period_end=prev_end, by=None, today=today), today=today)
    vr.withdraw(gone, by=None)
    vr.create_review(vendor_names=[PHILIPS], period_end=prev_end, by=None, today=today)  # a draft of those months again
    r = review(span, today=today)
    assert vr.previous_approved(r) is None and r.rating_timeliness == "" and reasons(r) == ["Drafted", "Figures taken"]
    assert vr.previous_approved(review(span, ["GE Healthcare"], today=today)) == ge


# --- the levels -----------------------------------------------------------------------------------------------------------------

def test_drafting_needs_edit_contracts_view_and_the_figures(ctx, today, span, philips, tenant, make_user):
    for user, words in ((make_user("analyst"), "Evaluations Edit and Contracts View"),
                        (user_with(tenant, "nocontracts", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.CONTRACTS: Level.NONE}),
                         "Evaluations Edit and Contracts View"),
                        (user_with(tenant, "noreports", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.REPORTS: Level.NONE}),
                         "Reports View and Work orders View"),
                        (user_with(tenant, "nowork", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.WORKORDERS: Level.NONE}),
                         "Reports View and Work orders View")):
        with pytest.raises(PermissionDenied) as e:
            review(span, by=user, today=today)
        assert words in str(e.value), user
    editor = user_with(tenant, "editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT})
    assert review(span, by=editor, today=today).created_by == editor


def test_a_scoped_user_is_refused_everything(ctx, today, span, philips, make_user):
    r = review(span, today=today)
    ready(r)
    vendor = make_user("vendor")
    vendor.role.set_levels({m: Level.FULL for m in Module.values})
    vendor = User.objects.get(pk=vendor.pk)
    assert not perms.can_view_reviews(vendor)
    calls = [lambda: review(span, by=vendor, today=today), lambda: vr.refresh(r, by=vendor, today=today),
             lambda: vr.update_review(r, by=vendor, summary="x"), lambda: vr.discard(r, by=vendor),
             lambda: vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=vendor, today=today),
             lambda: vr.withdraw(r, by=vendor),
             lambda: vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, by=vendor, today=today),
             lambda: vr.record_plan_result(r, result=PlanResult.MET, closed_on=today, by=vendor, today=today)]
    for call in calls:
        with pytest.raises(PermissionDenied):
            call()


def test_approve_signs_off_withdraws_and_reports(ctx, today, span, philips, tenant, manager):
    editor = user_with(tenant, "editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT})
    r = review(span, by=editor, today=today)
    ready(r, by=editor)
    with pytest.raises(PermissionDenied) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=editor, today=today)
    assert "Evaluations Approve and Contracts View" in str(e.value)
    blind = user_with(tenant, "blind", {**EVERY_VIEW, Module.EVALUATIONS: Level.APPROVE, Module.REPORTS: Level.NONE})
    with pytest.raises(PermissionDenied) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=blind, today=today)
    assert "Signing off a vendor review needs Reports View" in str(e.value)
    r = vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=manager, today=today)
    assert (r.status, r.signed_by, r.signed_on) == (ReviewStatus.APPROVED, manager, today)
    for call in (lambda: vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, by=editor, today=today),
                 lambda: vr.withdraw(r, by=editor)):
        with pytest.raises(PermissionDenied):
            call()
    [rep] = vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, by=manager, today=today)
    for call in (lambda: vr.correct_report(rep, by=editor, minutes_reference="Q-1"), lambda: vr.remove_report(rep, by=editor)):
        with pytest.raises(PermissionDenied):
            call()
    analyst = User.objects.get(pk=user_with(tenant, "viewer", EVERY_VIEW).pk)
    with pytest.raises(PermissionDenied):
        vr.discard(r, by=analyst)
    assert vr.withdraw(r, by=manager).status == ReviewStatus.WITHDRAWN


# --- refresh ----------------------------------------------------------------------------------------------------------------------

def test_refresh_takes_new_figures_on_a_draft_only(ctx, today, span, philips, director, figures):
    r = review(span, today=today)
    first = r.snapshot_id
    r = vr.refresh(r, by=director, today=today)
    assert r.snapshot_id != first and r.snapshot.taken_by == director and r.snapshot.data["call"] == 2
    assert VendorReviewSnapshot.objects.filter(review=r).count() == 2 and reasons(r)[-1] == "Figures taken again"
    signed(r, today=today)
    with pytest.raises(ValidationError) as e:
        vr.refresh(r, by=director, today=today)
    assert e.value.messages == [f"{r.number} is signed off: only a draft's figures are taken again."]


def test_refresh_is_refused_when_the_figures_were_taken_meanwhile(ctx, today, span, philips, figures, monkeypatch):
    r = review(span, today=today)

    def another_refresh_lands(keys, start, end, *, today):
        other = VendorReviewSnapshot.objects.create(review=r, taken_at=timezone.now(), taken_on=today, data={"schema": 1})
        VendorReview.objects.filter(pk=r.pk).update(snapshot=other)
        return {"schema": 1}

    monkeypatch.setattr(figures.module, "snapshot", another_refresh_lands)
    with pytest.raises(ValidationError) as e:
        vr.refresh(r, by=None, today=today)
    assert e.value.messages == [vr.FIGURES_CHANGED]


def test_refresh_is_refused_when_it_is_signed_off_meanwhile(ctx, today, span, philips, figures, monkeypatch):
    r = ready(review(span, today=today))
    before = r.snapshot_id

    def signed_meanwhile(keys, start, end, *, today):
        vr.sign_off(r, snapshot_id=before, signed_on=today, by=None, today=today)
        return {"schema": 1}

    monkeypatch.setattr(figures.module, "snapshot", signed_meanwhile)
    with pytest.raises(ValidationError) as e:
        vr.refresh(r, by=None, today=today)
    assert "is signed off: only a draft's figures are taken again" in e.value.messages[0]
    r.refresh_from_db()
    assert (r.status, r.snapshot_id) == (ReviewStatus.APPROVED, before)


def test_the_real_figures_when_wave_1b_is_merged(ctx, today, span, philips, figures, monkeypatch):
    if figures.real is None:
        pytest.skip("apps.evaluations.vendor_figures arrives with wave 1B")
    monkeypatch.setattr(figures.module, "snapshot", figures.real)
    r = review(span, today=today)
    assert r.snapshot.data["schema"] == 1 and figures.calls == []


# --- the reviewer's fields --------------------------------------------------------------------------------------------------------

def test_update_saves_only_the_fields_given(ctx, today, span, philips, director):
    r = review(span, today=today)
    stale = VendorReview.objects.get(pk=r.pk)
    vr.update_review(r, by=director, rating_timeliness=Rating.PARTLY, summary="  Slow parts in spring.  ")
    vr.update_review(stale, by=director, overall=Overall.PARTIALLY)  # a copy read before: the summary stays
    r.refresh_from_db()
    assert (r.rating_timeliness, r.summary, r.overall) == (Rating.PARTLY, "Slow parts in spring.", Overall.PARTIALLY)
    assert reasons(r)[-2:] == ["Changed: Timeliness, Summary", "Overall changed"]
    vr.update_review(r, by=director, overall=Overall.PARTIALLY)  # nothing changed: nothing written
    assert len(reasons(r)) == 4
    vr.update_review(r, by=director, summary=None, rating_timeliness="")  # cleared
    r.refresh_from_db()
    assert (r.summary, r.rating_timeliness) == ("", "")


def test_the_vendors_reported_figures(ctx, today, span, philips):
    r = review(span, today=today)
    r = vr.update_review(r, by=None, uptime_target_pct="98", uptime_reported_pct=Decimal("97.25"), uptime_lowest_pct=91.5,
                         systems_below="1", systems_total=5, response_target_pct="95.00", response_reported_pct=100,
                         vendor_report_reference="UPT-2026-Q4")
    r.refresh_from_db()
    assert (r.uptime_target_pct, r.uptime_reported_pct, r.uptime_lowest_pct, r.systems_below, r.systems_total) == (
        Decimal("98.00"), Decimal("97.25"), Decimal("91.50"), 1, 5)
    assert (r.response_target_pct, r.response_reported_pct, r.vendor_report_reference) == (Decimal("95.00"), Decimal("100.00"), "UPT-2026-Q4")
    with pytest.raises(ValidationError) as e:  # the reported figure moved below the lowest system's
        vr.update_review(r, by=None, uptime_reported_pct="90")
    assert list(e.value.message_dict) == ["uptime_reported_pct"]
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, uptime_lowest_pct="99")
    assert "cannot be above the uptime the vendor reports" in e.value.message_dict["uptime_lowest_pct"][0]
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, systems_total=0)
    assert list(e.value.message_dict) == ["systems_total"]
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, systems_below=6)
    assert "cannot be more than the systems" in e.value.message_dict["systems_below"][0]
    r = vr.update_review(r, by=None, uptime_lowest_pct=None, uptime_reported_pct="90")  # either alone is fine
    assert (r.uptime_lowest_pct, r.uptime_reported_pct) == (None, Decimal("90.00"))


@pytest.mark.parametrize("field,value,words", [
    ("rating_timeliness", "good", "Choose met, partly met"),
    ("stated_in_contract", "maybe", "Choose yes, partly, or no."),
    ("overall", "exceeds", "Choose meets, partially meets"),
    ("decision", "renegotiate", "Choose renew"),
    ("uptime_target_pct", "101", "from 0 to 100"),
    ("uptime_target_pct", "-1", "from 0 to 100"),
    ("uptime_target_pct", "97.125", "at most two decimals"),
    ("uptime_reported_pct", "ninety", "from 0 to 100"),
    ("response_reported_pct", "NaN", "from 0 to 100"),
    ("response_target_pct", True, "from 0 to 100"),
    ("systems_total", "2.5", "whole number of systems"),
    ("systems_total", -1, "whole number of systems"),
    ("systems_below", 40000, "whole number of systems"),
    ("vendor_report_reference", "UPT 2026", "without spaces"),
    ("vendor_report_reference", "U" * 41, "At most 40 characters"),
    ("summary", "x" * (REVIEW_SUMMARY_MAX + 1), "At most 2,000 characters (2,001 entered)."),
    ("summary", 7, "Enter text."),
    ("approver_name", "P" * 121, "At most 120 characters"),
    ("approver_title", "T" * 121, "At most 120 characters"),
    ("plan_due_on", "next week", "Enter the day the plan is due."),
    ("plan_due_on", date(1999, 12, 31), "from 2000 on"),
])
def test_each_field_is_checked(ctx, today, span, philips, field, value, words):
    r = review(span, today=today)
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, **{field: value})
    assert words in e.value.message_dict[field][0]


def test_problems_come_together_and_names_are_fixed(ctx, today, span, philips):
    r = review(span, today=today)
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, overall="x", uptime_target_pct="200", summary="fine")
    assert set(e.value.message_dict) == {"overall", "uptime_target_pct"}
    r.refresh_from_db()
    assert r.summary == ""  # nothing written
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, vendor_names=["GE"])
    assert "fixed when it is drafted" in e.value.messages[0]
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, plan_result=PlanResult.MET)
    assert "has no plan_result to change here" in e.value.messages[0]


def test_only_a_draft_is_changed(ctx, today, span, philips):
    r = signed(review(span, today=today), today=today)
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, summary="Later thoughts")
    assert e.value.messages == [f"{r.number} is signed off: only a draft is changed. Withdraw it and draft another to change it."]
    vr.withdraw(r, by=None)
    with pytest.raises(ValidationError) as e:
        vr.update_review(r, by=None, summary="Later thoughts")
    assert e.value.messages == [f"{r.number} is withdrawn: only a draft is changed."]


# --- the sign-off -----------------------------------------------------------------------------------------------------------------

def test_the_sign_off_holds_the_figures_the_approver_saw(ctx, today, span, philips, manager):
    r = ready(review(span, today=today))
    seen = r.snapshot_id
    vr.refresh(r, by=None, today=today)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=seen, signed_on=today, by=manager, today=today)
    assert e.value.messages == [vr.FIGURES_CHANGED]
    for posted in (None, "not-an-id", ""):
        with pytest.raises(ValidationError):
            vr.sign_off(r, snapshot_id=posted, signed_on=today, by=manager, today=today)
    r.refresh_from_db()
    r = vr.sign_off(r, snapshot_id=str(r.snapshot_id).upper(), signed_on=today.isoformat(), by=manager, today=today)
    assert (r.status, r.signed_on, r.signed_by, r.snapshot_id) != (ReviewStatus.DRAFT, None, None, seen)
    assert r.status == ReviewStatus.APPROVED and reasons(r)[-1] == "Signed off"


def test_the_sign_off_needs_every_rating_overall_decision_and_approver(ctx, today, span, philips):
    r = review(span, today=today)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert set(e.value.message_dict) == {*CRITERIA_FIELDS, "overall", "decision", "approver_name"}
    assert e.value.message_dict["rating_invoices"] == ["Rate invoices (not applicable is a rating)."]
    vr.update_review(r, by=None, **{**READY, "rating_invoices": Rating.NA})
    assert vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today).status == ReviewStatus.APPROVED


@pytest.mark.parametrize("decision,ok", [
    (Decision.RENEW, False),
    (Decision.RENEW_CHANGES, False),
    (Decision.CORRECTIVE_PLAN, True),
    (Decision.REPLACE, True),
    (Decision.IN_HOUSE, True),
])
def test_does_not_meet_needs_a_plan_replacing_or_in_house(ctx, today, span, philips, decision, ok):
    r = ready(review(span, today=today), overall=Overall.DOES_NOT_MEET, decision=decision,
              plan_due_on=today + 60 * DAY if decision == Decision.CORRECTIVE_PLAN else None)
    if ok:
        assert vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today).status == ReviewStatus.APPROVED
    else:
        with pytest.raises(ValidationError) as e:
            vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
        assert "does not meet expectations needs a corrective action plan" in e.value.message_dict["decision"][0]


def test_a_corrective_plan_is_due_after_the_sign_off_and_only_a_plan_has_a_due_day(ctx, today, span, philips):
    r = ready(review(span, today=today), overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert e.value.message_dict == {"plan_due_on": ["Enter the day the corrective action plan is due."]}
    vr.update_review(r, by=None, plan_due_on=today)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert "due after the day it is signed off" in e.value.message_dict["plan_due_on"][0]
    vr.update_review(r, by=None, decision=Decision.RENEW_CHANGES)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert "Only a corrective action plan has a due day" in e.value.message_dict["plan_due_on"][0]
    vr.update_review(r, by=None, decision=Decision.CORRECTIVE_PLAN, plan_due_on=(today + DAY).isoformat())
    r = vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert (r.status, r.plan_due_on) == (ReviewStatus.APPROVED, today + DAY)


@pytest.mark.parametrize("signed_on,words", [
    (1, "cannot be dated after today"),
    ("end-1", "the sign-off is on or after that day"),
    (None, "Enter the day the review was signed off."),
    ("someday", "Enter the day the review was signed off."),
])
def test_the_sign_off_day(ctx, today, span, philips, signed_on, words):
    r = ready(review(span, today=today))
    if signed_on == 1:
        signed_on = today + DAY
    elif signed_on == "end-1":
        signed_on = span[1] - DAY
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=signed_on, by=None, today=today)
    assert words in e.value.message_dict["signed_on"][0]
    assert vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=span[1], by=None, today=today).signed_on == span[1]  # its last day


def test_only_a_draft_is_signed_off_and_the_constraint_reads_in_words(ctx, today, span, philips):
    r = signed(review(span, today=today), today=today)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)
    assert e.value.messages == [f"{r.number} is signed off: only a draft is signed off."]
    # A draft made around the services (never through them: the overlap check refuses it) meets the partial unique constraint
    twin = VendorReview.objects.create(number="VR-99-0001", vendor_names=["Philips"], vendor_keys=["philips"], vendor_key="philips",
                                       period_start=r.period_start, period_end=r.period_end, **READY)
    snap = VendorReviewSnapshot.objects.create(review=twin, taken_at=timezone.now(), taken_on=today, data={})
    VendorReview.objects.filter(pk=twin.pk).update(snapshot=snap)
    with pytest.raises(ValidationError) as e:
        vr.sign_off(twin, snapshot_id=snap.pk, signed_on=today, by=None, today=today)
    assert e.value.messages == [f"Another review of Philips for the 12 months from {vr.day_words(r.period_start)} is signed off already."]
    assert VendorReview.objects.get(pk=twin.pk).status == ReviewStatus.DRAFT


# --- the plan's result ------------------------------------------------------------------------------------------------------------

def test_the_plan_result(ctx, today, span, philips, make_user):
    technician = make_user("technician")
    signed_day = today - 10 * DAY if today - 10 * DAY >= span[1] else span[1]
    r = signed(review(span, today=today), today=today, signed_on=signed_day, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN,
               plan_due_on=today + 30 * DAY)
    with pytest.raises(PermissionDenied):
        vr.record_plan_result(r, result=PlanResult.MET, closed_on=today, by=technician, today=today)
    with pytest.raises(ValidationError) as e:
        vr.record_plan_result(r, result="done", closed_on=today + DAY, by=None, today=today)
    assert set(e.value.message_dict) == {"plan_result", "plan_closed_on"}  # together
    assert "plan met, plan not met, or contract ended" in e.value.message_dict["plan_result"][0]
    with pytest.raises(ValidationError) as e:
        vr.record_plan_result(r, result=PlanResult.MET, closed_on=today + DAY, by=None, today=today)
    assert e.value.message_dict == {"plan_closed_on": ["The plan cannot be closed after today."]}
    with pytest.raises(ValidationError) as e:
        vr.record_plan_result(r, result=PlanResult.MET, closed_on=None, by=None, today=today)
    assert e.value.message_dict == {"plan_closed_on": ["Enter the day the plan was closed."]}
    if signed_day > span[1]:
        with pytest.raises(ValidationError) as e:
            vr.record_plan_result(r, result=PlanResult.MET, closed_on=signed_day - DAY, by=None, today=today)
        assert "its plan closes on or after that day" in e.value.message_dict["plan_closed_on"][0]
    manager = make_user("manager")
    r = vr.record_plan_result(r, result=PlanResult.NOT_MET, closed_on=today, by=manager, today=today)
    r = vr.record_plan_result(r, result=PlanResult.MET, closed_on=today.isoformat(), by=manager, today=today)
    assert (r.plan_result, r.plan_closed_on) == (PlanResult.MET, today)
    r = vr.record_plan_result(r, result="", closed_on=None, by=manager, today=today)
    assert (r.plan_result, r.plan_closed_on) == ("", None)
    assert reasons(r)[-3:] == ["Plan result: Plan not met", "Plan result: Plan met", "Plan result cleared: entered in error"]


def test_only_a_signed_off_plan_has_a_result(ctx, today, span, philips, pump):
    r = review(span, today=today)
    with pytest.raises(ValidationError) as e:
        vr.record_plan_result(r, result=PlanResult.MET, closed_on=today, by=None, today=today)
    assert e.value.messages == [f"{r.number} is draft: only a signed-off review with a corrective action plan has a plan result."]
    signed(r, today=today)
    with pytest.raises(ValidationError) as e:
        vr.record_plan_result(r, result=PlanResult.MET, closed_on=today, by=None, today=today)
    assert "is signed off without a corrective action plan" in e.value.messages[0]


# --- withdraw and discard ---------------------------------------------------------------------------------------------------------

def test_withdraw_keeps_the_review_and_its_reports(ctx, today, span, philips):
    r = review(span, today=today)
    with pytest.raises(ValidationError) as e:
        vr.withdraw(r, by=None)
    assert e.value.messages == [f"{r.number} is a draft: discard it instead."]
    signed(r, today=today)
    vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, by=None, today=today)
    with pytest.raises(ValidationError) as e:
        vr.discard(r, by=None)
    assert e.value.messages == [f"{r.number} is signed off: withdraw it instead (Evaluations Approve)."]
    r = vr.withdraw(r, by=None)
    assert r.status == ReviewStatus.WITHDRAWN and r.reports.count() == 1 and reasons(r)[-1] == "Withdrawn"
    for call, words in ((vr.withdraw, "is withdrawn already."), (vr.discard, "is withdrawn: only a draft is discarded.")):
        with pytest.raises(ValidationError) as e:
            call(r, by=None)
        assert e.value.messages == [f"{r.number} {words}"]
    with pytest.raises(ValidationError):
        vr.record_report([r], body=ReportBody.GOVERNING, meeting_on=today, by=None, today=today)


def test_discard_a_draft(ctx, today, span, philips):
    r = vr.discard(review(span, today=today), by=None)
    assert r.status == ReviewStatus.DISCARDED and reasons(r)[-1] == "Discarded"
    with pytest.raises(ValidationError) as e:
        vr.discard(r, by=None)
    assert e.value.messages == [f"{r.number} is discarded already."]
    for call in (lambda: vr.refresh(r, by=None, today=today), lambda: vr.update_review(r, by=None, summary="x"),
                 lambda: vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=None, today=today)):
        with pytest.raises(ValidationError):
            call()


# --- the committees ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def two_signed(today, span, philips, pump):
    job(pump, "GE Healthcare", span[0] + 10 * DAY)
    a = signed(review(span, today=today), today=today)
    b = signed(review(span, ["GE Healthcare"], today=today), today=today)
    return a, b


def test_one_meeting_for_several_reviews(ctx, today, two_signed, manager):
    a, b = two_signed
    reports = vr.record_report([b, a, a], body=ReportBody.GOVERNING, meeting_on=today, minutes_reference="BOT-2026-10", by=manager,
                               today=today)
    assert [(rep.review, rep.body, rep.meeting_on, rep.minutes_reference, rep.recorded_by) for rep in reports] == [
        (a, ReportBody.GOVERNING, today, "BOT-2026-10", manager), (b, ReportBody.GOVERNING, today, "BOT-2026-10", manager)]
    assert list(VendorReviewReport.history.filter(id=reports[0].pk).values_list("history_change_reason", flat=True)) == [
        "Reported to the governing body"]
    quality = vr.record_report(a, body=ReportBody.QUALITY, meeting_on=today, by=manager, today=today)  # another body, the same day
    assert len(quality) == 1 and VendorReviewReport.objects.count() == 3
    with pytest.raises(ValidationError) as e:  # all or nothing: b is reported to the governing body that day already
        vr.record_report([a, b], body=ReportBody.GOVERNING, meeting_on=today, by=manager, today=today)
    assert e.value.message_dict["reviews"] == [f"{a.number} is recorded as reported to the governing body on {vr.day_words(today)} already.",
                                               f"{b.number} is recorded as reported to the governing body on {vr.day_words(today)} already."]
    assert VendorReviewReport.objects.count() == 3


def test_a_report_is_checked(ctx, today, span, two_signed, pump):
    a, b = two_signed
    job(pump, "Stryker", span[0] + 10 * DAY)
    draft = review(span, ["Stryker"], today=today)
    with pytest.raises(ValidationError) as e:
        vr.record_report([a, draft], body=ReportBody.QUALITY, meeting_on=today, by=None, today=today)
    assert e.value.message_dict["reviews"] == [f"{draft.number} is draft: only a signed-off review is reported to a committee."]
    with pytest.raises(ValidationError) as e:
        vr.record_report([a], body=ReportBody.QUALITY, meeting_on=today - DAY, by=None, today=today)
    assert e.value.message_dict["meeting_on"] == [f"{a.number} was signed off on {vr.day_words(today)}: the meeting that received it is on or "
                                                  "after that day."]
    with pytest.raises(ValidationError) as e:
        vr.record_report([a], body="board", meeting_on=today + DAY, minutes_reference="BOT 1", by=None, today=today)
    assert set(e.value.message_dict) == {"body", "meeting_on", "minutes_reference"}
    with pytest.raises(ValidationError) as e:
        vr.record_report([], body=ReportBody.QUALITY, meeting_on=today, by=None, today=today)
    assert e.value.message_dict == {"reviews": ["Choose the reviews the committee received."]}
    assert not VendorReviewReport.objects.exists()


def test_correcting_and_removing_a_report(ctx, today, two_signed, manager):
    a, b = two_signed
    [rep] = vr.record_report([a], body=ReportBody.QUALITY, meeting_on=today, by=manager, today=today)
    vr.record_report([a], body=ReportBody.GOVERNING, meeting_on=today, by=manager, today=today)
    rep = vr.correct_report(rep, by=manager, today=today, minutes_reference=" QAPI-2026-10 ")
    assert rep.minutes_reference == "QAPI-2026-10"
    with pytest.raises(ValidationError) as e:  # the governing body's report of that day is on file
        vr.correct_report(rep, by=manager, today=today, body=ReportBody.GOVERNING)
    assert "already" in e.value.message_dict["meeting_on"][0]
    with pytest.raises(ValidationError) as e:
        vr.correct_report(rep, by=manager, today=today, meeting_on=today - DAY)
    assert "signed off on" in e.value.message_dict["meeting_on"][0]
    with pytest.raises(ValidationError) as e:
        vr.correct_report(rep, by=manager, today=today, review=b)
    assert e.value.messages == ["A committee report has no review."]
    vr.remove_report(rep, by=manager)
    assert not VendorReviewReport.objects.filter(pk=rep.pk).exists()
    assert list(VendorReviewReport.history.filter(id=rep.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True)) == [
        "Reported to the quality oversight committee (QAPI)", "Report corrected", "Removed: entered in error"]
    with pytest.raises(ValidationError) as e:
        vr.remove_report(rep, by=manager)
    assert e.value.messages == ["That report is not on file: it was removed meanwhile."]


# --- reading ----------------------------------------------------------------------------------------------------------------------

def test_current_finds_a_review_by_its_number(ctx, today, span, philips):
    r = review(span, today=today)
    assert vr.current(r.number) == r and vr.current(f" {r.number.lower()} ") == r
    assert vr.current("VR-00-9999") is None and vr.current("") is None and vr.current(None) is None


def test_the_vendors_list(ctx, today, pump):
    year_ago = add_months(today + DAY, -12)
    contract("Philips Healthcare", today - 200 * DAY, today + 100 * DAY)  # in force today
    contract("GE Healthcare", today - 500 * DAY, today - 90 * DAY)  # ended in the last 12 months
    contract("Old Co", today - 900 * DAY, year_ago - DAY)  # ended before them
    job(pump, "TechCare", today - 20 * DAY)
    job(pump, "TECHCARE field service", today - 40 * DAY)
    job(pump, "TechCare", today - 50 * DAY)
    job(pump, "Old Jobs Inc", year_ago - 5 * DAY)  # before the 12 months
    job(pump, "", today - 5 * DAY)  # vendor not named: never a row
    job(pump, "Reviewed Co", today - 500 * DAY)  # long ago, but reviewed
    reviewed = vr.create_review(vendor_names=["Reviewed Co"], period_end=today - 450 * DAY, by=None, today=today)
    job(pump, "Discarded Co", today - 500 * DAY)
    vr.discard(vr.create_review(vendor_names=["Discarded Co"], period_end=today - 450 * DAY, by=None, today=today), by=None)
    with CaptureQueriesContext(connection) as q:
        rows = vr.vendors_list(today)
    assert len(q.captured_queries) == 4
    by_key = {row.key: row for row in rows}
    assert set(by_key) == {"philips healthcare", "ge healthcare", "techcare", "reviewed co"}
    p, ge, tc, rc = (by_key[k] for k in ("philips healthcare", "ge healthcare", "techcare", "reviewed co"))
    assert (p.name, p.contracted, [c.vendor for c in p.contracts], p.jobs, p.reviewed, p.next_due, p.overdue) == (
        "Philips Healthcare", True, ["Philips Healthcare"], 0, False, today + 100 * DAY, False)
    assert (ge.contracted, ge.contracts, ge.next_due) == (True, [], None)  # ended: due when reviewed, none before
    assert (tc.name, tc.names, tc.jobs, tc.contracted, tc.next_due) == ("TechCare", ["TechCare", "TECHCARE field service"], 3, False, None)
    assert (rc.draft, rc.last_review, rc.jobs, rc.contracted) == (reviewed, None, 0, False)
    assert [row.key for row in rows] == ["philips healthcare", "ge healthcare", "reviewed co", "techcare"]  # due first, then by name


def test_next_due_and_the_open_plan(ctx, today, pump):
    def signed_for(name, end, **over):
        job(pump, name, end - 5 * DAY)
        return signed(vr.create_review(vendor_names=[name], period_end=end, by=None, today=today), today=today, signed_on=end + 10 * DAY,
                      **over)

    # Reviewed for the coming renewal: the contract's end is covered, the next 12 months' review is due next
    contract("Stryker", today - 300 * DAY, today + 60 * DAY)
    stryker = signed_for("Stryker", today - 40 * DAY)
    # Reviewed long before the contract ends: its end comes first
    contract("Hillrom", today - 700 * DAY, today + 60 * DAY)
    signed_for("Hillrom", today - 380 * DAY)
    # Reviewed long ago and no contract: overdue; on a plan past its day
    old = signed_for("Draeger", today - 600 * DAY, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today - 500 * DAY)
    rows = {row.key: row for row in vr.vendors_list(today)}
    s, h, d = rows["stryker"], rows["hillrom"], rows["draeger"]
    assert (s.last_review, s.next_due, s.overdue) == (stryker, vr.next_period_end(today - 40 * DAY) + 90 * DAY, False)
    assert (h.next_due, h.overdue) == (today + 60 * DAY, False)
    assert (d.next_due, d.overdue, d.open_plan, d.plan_overdue) == (vr.next_period_end(today - 600 * DAY) + 90 * DAY, True, old, True)
    vr.record_plan_result(old, result=PlanResult.CONTRACT_ENDED, closed_on=today, by=None, today=today)
    d = {row.key: row for row in vr.vendors_list(today)}["draeger"]
    assert (d.open_plan, d.plan_overdue, d.last_review) == (None, False, old)
    vr.withdraw(stryker, by=None)  # withdrawn: no longer its last review
    s = {row.key: row for row in vr.vendors_list(today)}["stryker"]
    assert (s.last_review, s.reviewed, s.next_due) == (None, False, today + 60 * DAY)


# --- PostgreSQL ---------------------------------------------------------------------------------------------------------------------

def _race(tenant, work, n=2):
    errors, done = [], []

    def run(i):
        try:
            with tenant_context(tenant):
                done.append(work(i))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return done, errors


def _setup(tenant):
    """Philips and Philips Healthcare jobs in the 12 months ending at the end of last month; the period's end."""
    from apps.equipment.models import Asset, Department, DeviceModel

    with tenant_context(tenant):
        today = timezone.localdate()
        end = today.replace(day=1) - DAY
        model = DeviceModel.objects.create(manufacturer="Philips", model="MX450", category="Monitors")
        asset = Asset.objects.create(tag="CE-1", device_model=model, department=Department.objects.create(name="ICU"))
        job(asset, PHILIPS, end - 30 * DAY)
        job(asset, "Philips Healthcare", end - 20 * DAY)
    return today, end


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_drafts_of_one_vendor_at_once(tenant, figures, monkeypatch):
    """Both pass the early check and count; the facility's lock lets one in, and the other finds it and is refused."""
    today, end = _setup(tenant)
    barrier = threading.Barrier(2, timeout=30)

    def counted_together(keys, start, end_, *, today):
        barrier.wait()
        return {"schema": 1}

    monkeypatch.setattr(figures.module, "snapshot", counted_together)
    names = [[PHILIPS], ["Philips Healthcare", "philips"]]
    done, errors = _race(tenant, lambda i: vr.create_review(vendor_names=names[i], period_end=end, by=None, today=today))
    assert len(done) == 1 and len(errors) == 1, errors
    assert "a vendor is in one review for any day" in errors[0].message_dict["period_end"][0]
    with tenant_context(tenant):
        assert VendorReview.objects.count() == 1 and VendorReviewSnapshot.objects.count() == 1


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_sign_off_waits_for_a_refresh_holding_the_review(tenant, figures, monkeypatch):
    """The refresh holds the review's row while it saves new figures; the sign-off posted with the old figures waits for it, then reads
    the new figures and is refused: the review stays a draft with the refreshed figures."""
    today, end = _setup(tenant)
    with tenant_context(tenant):
        r = ready(vr.create_review(vendor_names=[PHILIPS], period_end=end, by=None, today=today))
        seen = r.snapshot_id
    holding = threading.Event()
    real_take = vr._take

    def take_and_hold(*args, **kwargs):
        snap = real_take(*args, **kwargs)
        holding.set()
        time_module.sleep(1.5)  # the sign-off reaches the row's lock meanwhile
        return snap

    monkeypatch.setattr(vr, "_take", take_and_hold)

    def work(i):
        if i == 0:
            return vr.refresh(r, by=None, today=today)
        assert holding.wait(30)
        return vr.sign_off(r, snapshot_id=seen, signed_on=today, by=None, today=today)

    done, errors = _race(tenant, work)
    assert len(done) == 1 and len(errors) == 1 and errors[0].messages == [vr.FIGURES_CHANGED]
    with tenant_context(tenant):
        r.refresh_from_db()
        assert r.status == ReviewStatus.DRAFT and r.snapshot_id not in (None, seen) and r.snapshot_id == done[0].snapshot_id


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_committee_batches_at_once(tenant, monkeypatch):
    """Both check their fields together, then each locks the review: the second finds the first's report and writes nothing."""
    today, end = _setup(tenant)
    with tenant_context(tenant):
        r = signed(vr.create_review(vendor_names=[PHILIPS], period_end=end, by=None, today=today), today=today)
    barrier = threading.Barrier(2, timeout=30)
    real_clean = vr._clean_report

    def cleaned_together(values, today_):
        out = real_clean(values, today_)
        barrier.wait()
        return out

    monkeypatch.setattr(vr, "_clean_report", cleaned_together)
    done, errors = _race(tenant, lambda i: vr.record_report([r], body=ReportBody.GOVERNING, meeting_on=today, by=None, today=today))
    assert len(done) == 1 and len(errors) == 1 and "already" in errors[0].message_dict["reviews"][0]
    with tenant_context(tenant):
        assert VendorReviewReport.objects.count() == 1


@needs_postgres
def test_the_services_under_the_policies(ctx, today, span, philips, other_tenant, manager):
    S, E = span
    contract("Philips Healthcare", S - 100 * DAY, today + 100 * DAY)
    with tenant_context(other_tenant):
        contract("Philips", S - 100 * DAY, today + 100 * DAY)
        theirs = vr.create_review(vendor_names=[PHILIPS], period_end=E, by=None, today=today)
    as_app_role()
    r = vr.create_review(vendor_names=["Philips Healthcare", PHILIPS], period_end=E, by=manager, today=today)
    assert r.number == theirs.number and VendorReview.objects.get() == r
    assert vr.contract_vendors(S, E) == {"philips healthcare": ["Philips Healthcare"]}
    r = vr.refresh(r, by=manager, today=today)
    r = ready(r, by=manager, overall=Overall.PARTIALLY, decision=Decision.CORRECTIVE_PLAN, plan_due_on=today + 30 * DAY,
              uptime_reported_pct="97")
    r = vr.sign_off(r, snapshot_id=r.snapshot_id, signed_on=today, by=manager, today=today)
    [rep] = vr.record_report([r], body=ReportBody.QUALITY, meeting_on=today, by=manager, today=today)
    vr.correct_report(rep, by=manager, today=today, minutes_reference="QAPI-1")
    vr.record_plan_result(r, result=PlanResult.MET, closed_on=today, by=manager, today=today)
    rows = {row.key: row for row in vr.vendors_list(today)}
    assert rows["philips healthcare"].last_review == r and rows["philips"].last_review == r
    assert vr.previous_approved(r) is None and vr.current(r.number) == r
    vr.remove_report(rep, by=manager)
    assert vr.withdraw(r, by=manager).status == ReviewStatus.WITHDRAWN
