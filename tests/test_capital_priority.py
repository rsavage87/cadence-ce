"""
Slice 36, the replacement priority's body (apps.capital.priority.score_assets and for_asset): the devices it scores, each component from
its inputs (age against expected life, manufacturer support, failures, service cost against the replacement estimate, a confirmed
recall), the window, the estimate, flags and gaps, the total with points unknown, the suggested fiscal year, ranking, tenant isolation,
and a fixed number of queries however many devices.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from capital_fixtures import TODAY, device, recall_match, work, years_before

from apps.capital import priority as P
from apps.contracts import services as ct_services
from apps.contracts.models import Contract
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, LifeSource, Ownership, RiskClass
from apps.pm.dates import add_months
from apps.recalls.models import AlertMatch
from apps.tenants.context import tenant_context
from apps.workorders.models import RepairCheck, RepairFinding, WoType

START = 7  # the fiscal year starts in July: TODAY is in FY2027
OLD = years_before(5)  # a work order this old gives the facility a full 36-month window


def score(asset, today=TODAY) -> P.Priority:
    found = P.score_assets(Asset.objects.filter(pk=asset.pk), today, START)
    assert len(found) == 1
    return found[0]


@pytest.fixture
def history(ctx, dept, pump_model):
    """The facility's first work order, five years ago, on a device of its own: every device's window is the full 36 months."""
    old = device(pump_model, dept, "CE-OLD", years=9)
    work(old, WoType.PM, OLD, completed=OLD)
    return old


@pytest.fixture
def model(ctx):
    """A low-risk model with a cheap list price (service cost scores against it) and support checked recently."""
    return DeviceModel.objects.create(manufacturer="Acme", model="S1", description="Suction pump", category="Suction", risk_class=RiskClass.LOW,
                                      expected_life_years=10, list_cost=Decimal("2000"), life_source=LifeSource.AHA,
                                      support_checked_on=TODAY - timedelta(days=30))


# --- what it scores ------------------------------------------------------------------------------------------------------------


def test_only_our_devices_in_use_are_scored(ctx, dept, vent_model, pump_model):
    ours = device(vent_model, dept, "CE-1", years=3)
    missing = device(vent_model, dept, "CE-2", years=3, status=AssetStatus.MISSING)
    retired = device(vent_model, dept, "CE-3", years=3, status=AssetStatus.RETIRED)
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as="existing", arrived_on=TODAY - timedelta(days=5), today=TODAY)
    found = P.score_assets(Asset.objects.all(), TODAY, START)
    assert {p.asset.tag for p in found} == {"CE-1", "CE-2"}
    assert P.for_asset(retired, TODAY, START) is None and P.for_asset(rental, TODAY, START) is None
    assert P.for_asset(ours, TODAY, START).asset.pk == ours.pk and P.for_asset(missing, TODAY, START) is not None
    assert P.score_assets(Asset.objects.filter(pk=retired.pk), TODAY, START) == []


def test_a_device_the_copy_in_hand_shows_is_not_scored_costs_no_query(ctx, dept, pump_model, django_assert_num_queries):
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as="existing", arrived_on=TODAY - timedelta(days=5), today=TODAY)
    with django_assert_num_queries(0):
        assert P.for_asset(rental, TODAY) is None


# --- age against expected life --------------------------------------------------------------------------------------------------


def test_age_against_expected_life(history, dept, vent_model):
    p = score(device(vent_model, dept, "CE-1", years=12))
    age = p.component(P.AGE)
    assert p.age_years == pytest.approx(12.0, abs=0.01) and p.life_years == 10
    assert (age.value, age.input, age.note) == (pytest.approx(0.8, abs=0.001), "12.0 of 10 yr", "life source not recorded")
    assert P.LIFE_SOURCE in p.gaps and p.ranked
    assert p.life_end == add_months(years_before(12), 120)


def test_an_unknown_age_is_never_scored_and_the_device_is_not_ranked(history, dept, model):
    none = score(device(model, dept, "CE-1"))
    ahead = score(device(model, dept, "CE-2", installed_on=TODAY + timedelta(days=1)))
    for p, words in ((none, "no install date"), (ahead, "install date after today")):
        assert p.component(P.AGE).value is None and p.component(P.AGE).input == words and p.age_years is None and p.life_end is None
        assert p.gaps == (P.NO_INSTALL_DATE,) and not p.ranked and p.unranked_reasons == (P.NO_INSTALL_DATE,)
        assert p.points_unknown >= P.WEIGHTS[P.AGE]
    assert ahead.failures is None and ahead.component(P.RELIABILITY).input == P.TOO_LITTLE_HISTORY  # its window has not begun


def test_a_zero_life_reads_as_one_year_and_a_recorded_source_has_no_note(history, dept, model):
    DeviceModel.objects.filter(pk=model.pk).update(expected_life_years=0)  # the services refuse it; a row from before may hold it
    p = score(device(model, dept, "CE-1", years=1))
    assert p.life_years == 1 and p.component(P.AGE).input == "1.0 of 1 yr" and p.component(P.AGE).value == pytest.approx(2 / 3, abs=0.001)
    assert p.life_end == add_months(years_before(1), 12)
    assert p.component(P.AGE).note == "" and P.LIFE_SOURCE not in p.gaps


# --- manufacturer support ---------------------------------------------------------------------------------------------------


def test_support_values_words_flags_and_the_unchecked_gap(history, dept, model, vent_model):
    p = score(device(model, dept, "CE-1", years=3))
    assert (p.component(P.SUPPORT).value, p.support.key, p.flags) == (0.0, "supported", ())
    assert P.SUPPORT_UNCHECKED not in p.gaps
    q = score(device(vent_model, dept, "CE-2", years=3))
    assert q.component(P.SUPPORT).value is None and q.component(P.SUPPORT).input == "Support not checked" and P.SUPPORT_UNCHECKED in q.gaps

    eq.update_device_model(model, support_ends_on=TODAY - timedelta(days=1), parts_end_on=TODAY + timedelta(days=300))
    p = score(p.asset)
    assert p.support.key == "support_ended" and p.component(P.SUPPORT).value == 0.8 and p.flags == (P.SUPPORT_ENDED,) and not p.must_act
    eq.update_device_model(model, parts_end_on=TODAY - timedelta(days=1))
    p = score(p.asset)
    assert p.support.key == "parts_ended" and p.flags == (P.PARTS_ENDED, P.SUPPORT_ENDED) and p.must_act
    assert p.suggested_fy == 2027  # must act: the current fiscal year
    eq.update_device_model(model, support_ends_on=TODAY + timedelta(days=10))  # parts gone, support still on: what the status says
    assert score(p.asset).flags == (P.PARTS_ENDED,)
    eq.update_device_model(model, support_ends_on=None, parts_end_on=date(2028, 3, 31))
    p = score(p.asset)
    assert (p.support.key, p.component(P.SUPPORT).value, p.component(P.SUPPORT).input) == ("announced", 0.2, "Parts available until Mar 31, 2028")
    assert p.suggested_fy == 2028 and p.flags == ()


# --- failures ---------------------------------------------------------------------------------------------------------------


def test_failures_in_the_window_with_what_was_not_recorded(history, dept, model):
    a = device(model, dept, "CE-1", years=12)
    for k in range(3):
        work(a, WoType.REPAIR, TODAY - timedelta(days=30 * (k + 1)), completed=TODAY - timedelta(days=30 * k + 20))  # no finding recorded
    work(a, WoType.REPAIR, TODAY - timedelta(days=200), completed=TODAY - timedelta(days=199), finding=RepairFinding.BATTERY)
    work(a, WoType.REPAIR, TODAY - timedelta(days=300), completed=TODAY - timedelta(days=299), finding=RepairFinding.USE_ERROR)  # no failure
    work(a, WoType.REPAIR, TODAY - timedelta(days=5))  # open: a failure until it is completed with what it found
    work(a, WoType.REPAIR, TODAY - timedelta(days=50), cancelled=True)  # not a failure
    work(a, WoType.REPAIR, add_months(TODAY, -36) - timedelta(days=1), completed=add_months(TODAY, -36))  # opened before the window
    work(a, WoType.REPAIR, add_months(TODAY, -36), completed=add_months(TODAY, -36), imported=True)  # its first day; imported
    work(a, WoType.PM, TODAY - timedelta(days=10), completed=TODAY - timedelta(days=10))  # a PM is no failure
    p = score(a)
    rel = p.component(P.RELIABILITY)
    assert p.window_start == add_months(TODAY, -36) and p.window_years == pytest.approx(1096 / 365.25)
    assert p.failures == 6 and p.unrecorded == 5
    assert rel.input == "6 in 36 mo, 2.0 a year" and rel.value == pytest.approx(6 / (1096 / 365.25) / 3)
    assert rel.note == "5 with no finding recorded (1 still open, 1 imported: not recorded then)"


def test_the_window_starts_at_the_install_date(history, dept, model):
    a = device(model, dept, "CE-1", installed_on=add_months(TODAY, -18))
    work(a, WoType.REPAIR, TODAY - timedelta(days=100), completed=TODAY - timedelta(days=99), finding=RepairFinding.WEAR)
    p = score(a)
    assert p.window_start == add_months(TODAY, -18) and p.failures == 1
    assert p.component(P.RELIABILITY).input == "1 in 18 mo, 0.7 a year" and p.component(P.RELIABILITY).note == ""


def test_the_window_starts_at_the_facilitys_first_work_order(ctx, dept, model):
    a = device(model, dept, "CE-1", years=12)
    first = add_months(TODAY, -20)
    work(a, WoType.PM, first, completed=first)
    p = score(a)
    assert p.window_start == first and p.failures == 0 and p.component(P.RELIABILITY).input == "0 in 20 mo, 0.0 a year"


def test_too_little_history(ctx, dept, model):
    a = device(model, dept, "CE-1", years=12)
    p = score(a)  # no work order in Cadence yet
    assert p.failures is None and p.service_cost_year is None and p.window_years == 0
    assert p.component(P.RELIABILITY).input == p.component(P.COST).input == P.TOO_LITTLE_HISTORY
    assert p.component(P.RELIABILITY).value is None and p.component(P.COST).value is None and p.points_unknown == 35
    work(a, WoType.REPAIR, add_months(TODAY, -12) + timedelta(days=1), completed=add_months(TODAY, -12) + timedelta(days=2))
    assert score(a).failures is None  # a day short of 12 months
    work(a, WoType.PM, add_months(TODAY, -12), completed=add_months(TODAY, -12))
    p = score(a)
    assert p.failures == 1 and p.component(P.RELIABILITY).input == "1 in 12 mo, 1.0 a year"  # a year: the floor


# --- service cost against the replacement estimate -------------------------------------------------------------------------------


def test_service_cost_a_year_against_the_list_price(history, dept, model):
    a = device(model, dept, "CE-1", years=6)
    work(a, WoType.REPAIR, TODAY - timedelta(days=40), completed=TODAY - timedelta(days=38), labor=[(2, 100)], parts=[(1, 300)])
    work(a, WoType.PM, TODAY - timedelta(days=90), completed=TODAY - timedelta(days=90), labor=[(1, 100)])
    work(a, WoType.RECALL, TODAY - timedelta(days=60), completed=TODAY - timedelta(days=59), labor=[(10, 100)])  # never a recall's
    work(a, WoType.REPAIR, add_months(TODAY, -40), completed=add_months(TODAY, -37) - timedelta(days=1), parts=[(1, 999)])  # before
    work(a, WoType.REPAIR, TODAY - timedelta(days=3), labor=[(4, 100)])  # not completed
    p = score(a)
    years = Decimal(1096) / Decimal("365.25")
    expected = (Decimal("600") / years).quantize(Decimal("0.01"))
    assert p.service_cost_year == expected == Decimal("199.95") and p.extras["spent"] == Decimal("600")
    cost = p.component(P.COST)
    ratio = expected / Decimal("2000")
    assert cost.value == pytest.approx(float((ratio - Decimal("0.05")) / Decimal("0.25")))
    assert cost.input == "10% a year of replacement ($200 a year)" and cost.note == ""


def test_a_contracts_share_is_added_to_every_line_recorded(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    b = device(model, dept, "CE-2", years=6, acquisition_cost=Decimal("3000"))
    gone = device(model, dept, "CE-3", years=6, acquisition_cost=Decimal("5000"))
    contract = ct_services.create_contract(reference="SC-1", vendor="Acme", start_on=TODAY - timedelta(days=100), end_on=TODAY + timedelta(days=100),
                                           annual_cost=Decimal("1000"))
    for x in (a, b, gone):
        ct_services.add_asset(contract, x)
    eq.set_status(gone, AssetStatus.RETIRED)  # not covered: no part of the share
    work(a, WoType.REPAIR, TODAY - timedelta(days=40), completed=TODAY - timedelta(days=38), labor=[(5, 100)], vendor=True,
         vendor_name="Acme")  # the contract's vendor: counted (a visit the contract paid for is recorded at $0)
    work(a, WoType.REPAIR, TODAY - timedelta(days=30), completed=TODAY - timedelta(days=28), labor=[(1, 100)])  # in-house: counted
    p = score(a)
    years = Decimal(1096) / Decimal("365.25")
    assert p.extras["contract_share"] == Decimal("250") and p.extras["spent"] == Decimal("600.00")
    assert p.service_cost_year == (Decimal("600") / years + Decimal("250")).quantize(Decimal("0.01"))
    assert p.component(P.COST).note == "with its contract share, $250 a year"

    Contract.objects.filter(pk=contract.pk).update(end_on=TODAY - timedelta(days=1))  # ended: its vendor's work counts, no share
    p = score(a)
    assert p.extras["contract_share"] is None and p.service_cost_year == (Decimal("600") / years).quantize(Decimal("0.01"))
    Contract.objects.filter(pk=contract.pk).update(start_on=TODAY + timedelta(days=1), end_on=TODAY + timedelta(days=300))  # not begun:
    assert score(a).extras["contract_share"] == Decimal("250")  # not ended, as Contract.is_expired and the Costs tab read it


def test_a_device_with_no_acquisition_cost_on_a_contract_none_covered_can_share_has_an_unknown_service_cost(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    contract = ct_services.create_contract(reference="SC-1", vendor="Acme", start_on=TODAY - timedelta(days=100), end_on=TODAY + timedelta(days=100),
                                           annual_cost=Decimal("1000"))
    ct_services.add_asset(contract, a)
    p = score(a)  # its share cannot be worked out (as the Costs tab says): never a silent $0
    cost = p.component(P.COST)
    assert p.extras["contract_share"] is None and p.service_cost_year is None
    assert (cost.value, cost.input) == (None, P.CONTRACT_SHARE_UNKNOWN) and p.points_unknown == P.WEIGHTS[P.COST]


# --- the replacement estimate -------------------------------------------------------------------------------------------------


def test_the_estimate_is_the_list_price_with_the_day_it_was_set(history, dept, model, make_user):
    hist = DeviceModel.history.model
    hist.objects.filter(id=model.pk).update(history_date=local_aware(date(2024, 3, 4)))  # created with its list price
    a = device(model, dept, "CE-1", years=4)
    p = score(a)
    assert (p.estimate.amount, p.estimate.source, p.estimate.set_on, p.estimate.words) == (
        Decimal("2000"), P.ESTIMATE_LIST, date(2024, 3, 4), "List price, set Mar 4, 2024")
    before = set(hist.objects.filter(id=model.pk).values_list("history_id", flat=True))
    eq.update_device_model(model, list_cost="2500")
    hist.objects.filter(id=model.pk).exclude(history_id__in=before).update(history_date=local_aware(date(2025, 5, 6)))
    before = set(hist.objects.filter(id=model.pk).values_list("history_id", flat=True))
    eq.update_device_model(model, description="Suction pump, portable")  # the price unchanged: its day stays
    hist.objects.filter(id=model.pk).exclude(history_id__in=before).update(history_date=local_aware(date(2026, 1, 2)))
    p = score(a)
    assert p.estimate.amount == Decimal("2500") and p.estimate.set_on == date(2025, 5, 6) and p.estimate.words == "List price, set May 6, 2025"
    DeviceModel.objects.filter(pk=model.pk).update(list_cost=Decimal("2600"))  # outside history: no day to show
    p = score(a)
    assert p.estimate.amount == Decimal("2600") and p.estimate.set_on is None and p.estimate.words == "List price"


def local_aware(day: date):
    from datetime import datetime, time

    from django.utils import timezone

    return timezone.make_aware(datetime.combine(day, time(12)))


def test_the_estimate_falls_back_to_the_acquisition_cost_then_to_none(history, dept, model):
    eq.update_device_model(model, list_cost="0")
    a = device(model, dept, "CE-1", installed_on=date(2017, 6, 1), acquisition_cost=Decimal("1500.50"))
    p = score(a)
    assert (p.estimate.amount, p.estimate.source, p.estimate.words) == (Decimal("1500.50"), P.ESTIMATE_ACQUISITION,
                                                                         "Acquisition cost, 2017: a historical price")
    b = score(device(model, dept, "CE-2", acquisition_cost=Decimal("900")))
    assert b.estimate.words == "Acquisition cost, year unknown: a historical price"
    c = device(model, dept, "CE-3", years=4, acquisition_cost=Decimal("0"))
    work(c, WoType.REPAIR, TODAY - timedelta(days=40), completed=TODAY - timedelta(days=38), labor=[(2, 100)])
    p = score(c)
    assert (p.estimate.amount, p.estimate.source, p.estimate.words) == (None, "", "No estimate")
    assert p.gaps == (P.NO_ESTIMATE,) and p.unranked_reasons == (P.NO_ESTIMATE,) and not p.ranked
    assert p.component(P.COST).value is None and p.component(P.COST).input == "$67 a year; no replacement estimate"


# --- a confirmed recall --------------------------------------------------------------------------------------------------------


def test_a_confirmed_recall_not_done_on_the_device(history, dept, model):
    a = device(model, dept, "CE-1", years=4)
    b = device(model, dept, "CE-2", years=4)
    assert score(a).component(P.SAFETY).value == 0.0 and score(a).component(P.SAFETY).input == "no confirmed recall"
    recall_match(model, status=AlertMatch.Status.NEEDS_ACTION)  # not yet reviewed: nothing
    recall_match(model, status=AlertMatch.Status.NOT_AFFECTED)
    assert score(a).component(P.SAFETY).value == 0.0 and score(a).flags == ()
    two = recall_match(model, classification="Class II")
    p = score(a)
    assert (p.component(P.SAFETY).value, p.component(P.SAFETY).input, p.flags) == (0.6, "1 confirmed recall not done on this device", (P.RECALL,))
    one = recall_match(model, classification="Class I")
    p = score(a)
    assert p.component(P.SAFETY).value == 1.0 and p.component(P.SAFETY).input == "2 confirmed recalls not done on this device, Class I"
    for m in (one, two):  # device A's recall work is done; B's is not
        work(a, WoType.RECALL, TODAY - timedelta(days=5), completed=TODAY - timedelta(days=4), alert=m.alert)
    work(b, WoType.RECALL, TODAY - timedelta(days=5), alert=one.alert)  # open
    assert score(a).component(P.SAFETY).value == 0.0 and score(a).flags == ()
    assert score(b).component(P.SAFETY).value == 1.0
    recall_match(model, status=AlertMatch.Status.UNDER_REVIEW)
    p = score(a)
    assert p.component(P.SAFETY).value == 0.0 and p.component(P.SAFETY).note == "recall under review"


# --- kept out of use after a repair ----------------------------------------------------------------------------------------------


def test_kept_out_of_use_after_its_latest_repair(history, dept, model):
    a = device(model, dept, "CE-1", years=4)
    work(a, WoType.REPAIR, TODAY - timedelta(days=60), completed=TODAY - timedelta(days=58), check=RepairCheck.NOT_RETURNED)
    assert score(a).flags == ()  # in service again
    Asset.objects.filter(pk=a.pk).update(status=AssetStatus.OUT_OF_SERVICE)
    p = score(a)
    assert p.flags == (P.NOT_RETURNED,) and p.must_act and p.suggested_fy == 2027
    work(a, WoType.REPAIR, TODAY - timedelta(days=20), completed=TODAY - timedelta(days=18), check=RepairCheck.PASSED)
    assert score(a).flags == ()  # the latest repair decides


# --- the total, the multiplier, the suggested fiscal year ------------------------------------------------------------------------


def test_the_score_is_the_known_points_times_the_multiplier(history, dept, vent_model):
    a = device(vent_model, dept, "CE-1", years=8, acquisition_cost=Decimal("38000"))  # life support: x1.0; support never checked
    work(a, WoType.REPAIR, TODAY - timedelta(days=40), completed=TODAY - timedelta(days=38), finding=RepairFinding.WEAR)
    p = score(a)
    assert p.multiplier == 1.0 and p.points_unknown == 25 and p.points_known == 75
    known = sum(c.points for c in p.components if c.value is not None)
    assert p.score == round(known) and p.score == P.total(p.components, 1.0)[0]
    eq.update_device_model(vent_model, risk_class=RiskClass.MEDIUM, by=None)
    assert score(a).multiplier == 0.9 and score(a).score == P.total(p.components, 0.9)[0]


def test_the_suggested_fiscal_year(history, dept, vent_model):
    a = device(vent_model, dept, "CE-1", installed_on=date(2018, 9, 1))  # life ends Sep 1, 2028: FY2029 with July
    p = score(a)
    assert p.life_end == date(2028, 9, 1) and p.suggested_fy == 2029
    eq.update_device_model(vent_model, support_ends_on=date(2028, 3, 31))
    assert score(a).suggested_fy == 2028
    assert P.score_assets(Asset.objects.filter(pk=a.pk), TODAY, 1)[0].suggested_fy == 2028  # calendar years
    assert P.score_assets(Asset.objects.filter(pk=a.pk), TODAY, 4)[0].suggested_fy == 2028
    assert score(device(vent_model, dept, "CE-2")).suggested_fy == 2028  # no install date: support's end still places it
    eq.update_device_model(vent_model, support_ends_on=None)
    assert score(device(vent_model, dept, "CE-3")).suggested_fy is None


def test_start_is_read_from_settings_when_not_given(history, dept, vent_model):
    from apps.facility import services as fac

    a = device(vent_model, dept, "CE-1", installed_on=date(2018, 9, 1))
    assert P.score_assets(Asset.objects.filter(pk=a.pk), TODAY)[0].suggested_fy == 2028  # January: the calendar year
    fac.update_settings(fiscal_year_start_month="7")
    assert P.score_assets(Asset.objects.filter(pk=a.pk), TODAY)[0].suggested_fy == 2029
    assert P.for_asset(a, TODAY).suggested_fy == 2029


def test_condition_is_shown_never_scored(history, dept, model):
    a = device(model, dept, "CE-1", years=4, condition=1)
    b = device(model, dept, "CE-2", years=4, condition=5)
    pa, pb = score(a), score(b)
    assert (pa.condition, pb.condition) == (1, 5) and pa.score == pb.score


def test_ranking_and_not_ranked(history, dept, model):
    old = device(model, dept, "CE-B", years=14)
    young = device(model, dept, "CE-A", years=1)
    same = device(model, dept, "CE-C", years=14)
    unknown = device(model, dept, "CE-0")
    ranked, rest = P.rank(P.score_assets(Asset.objects.filter(pk__in=[old.pk, young.pk, same.pk, unknown.pk]), TODAY, START))
    assert [p.asset.tag for p in ranked] == ["CE-B", "CE-C", "CE-A"] and [p.asset.tag for p in rest] == ["CE-0"]


# --- tenant isolation -------------------------------------------------------------------------------------------------------------


def test_another_facilitys_records_never_move_a_score(ctx, tenant, other_tenant, dept, model):
    a = device(model, dept, "CE-1", years=12)
    work(a, WoType.PM, add_months(TODAY, -6), completed=add_months(TODAY, -6))  # our history: six months
    before = score(a)
    with tenant_context(other_tenant):
        their_model = DeviceModel.objects.create(manufacturer="Acme", model="S1", description="Suction pump", category="Suction",
                                                 list_cost=Decimal("10"))
        theirs = device(their_model, Department.objects.create(name="ICU"), "CE-1", years=12)
        for k in range(5):
            work(theirs, WoType.REPAIR, OLD + timedelta(days=k), completed=OLD + timedelta(days=k), labor=[(9, 100)])
            work(theirs, WoType.REPAIR, TODAY - timedelta(days=k + 1), completed=TODAY - timedelta(days=k + 1), labor=[(9, 100)])
        recall_match(their_model, classification="Class I")
        contract = ct_services.create_contract(reference="SC-1", vendor="Acme", start_on=TODAY - timedelta(days=9),
                                               end_on=TODAY + timedelta(days=9), annual_cost=Decimal("99999"))
        ct_services.add_asset(contract, theirs)
    after = score(a)
    assert after.window_start == before.window_start == add_months(TODAY, -6) and after.failures is None
    assert (after.score, after.points_unknown, after.flags, after.estimate) == (before.score, before.points_unknown, before.flags, before.estimate)
    assert after.component(P.SAFETY).value == 0.0


# --- a fixed number of queries -----------------------------------------------------------------------------------------------


def _everything(model, dept, tag, contract, alert_match, n):
    """A device with every input the rule reads: failures, labor and parts, a contract, a recall done, a not-returned repair."""
    a = device(model, dept, tag, years=5 + n % 7, acquisition_cost=Decimal(1000 + n))
    ct_services.add_asset(contract, a)
    work(a, WoType.REPAIR, TODAY - timedelta(days=30 + n), completed=TODAY - timedelta(days=29 + n), labor=[(1, 90)], parts=[(1, 20)])
    work(a, WoType.REPAIR, TODAY - timedelta(days=90 + n), completed=TODAY - timedelta(days=88 + n), finding=RepairFinding.BATTERY,
         check=RepairCheck.NOT_RETURNED, vendor=True, labor=[(2, 150)])
    work(a, WoType.REPAIR, TODAY - timedelta(days=200 + n), completed=TODAY - timedelta(days=199 + n), imported=True, parts=[(2, 5)])
    work(a, WoType.PM, TODAY - timedelta(days=10 + n), completed=TODAY - timedelta(days=10 + n), labor=[(1, 80)])
    work(a, WoType.RECALL, TODAY - timedelta(days=5), completed=TODAY - timedelta(days=4), alert=alert_match.alert)
    work(a, WoType.REPAIR, TODAY - timedelta(days=2))
    return a


@pytest.mark.parametrize("devices", [1, 20])
def test_the_same_queries_for_one_device_or_twenty(history, dept, model, vent_model, devices, django_assert_num_queries):
    contract = ct_services.create_contract(reference="SC-1", vendor="Acme", start_on=TODAY - timedelta(days=100), end_on=TODAY + timedelta(days=100),
                                           annual_cost=Decimal("1000"))
    matches = [recall_match(m, classification="Class I") for m in (model, vent_model)]
    recall_match(model, status=AlertMatch.Status.UNDER_REVIEW)
    for n in range(devices):
        m = model if n % 2 == 0 else vent_model
        _everything(m, dept, f"CE-{n:03d}", contract, matches[n % 2], n)
        eq.update_device_model(m, list_cost=str(2000 + n))  # more list price history
    with django_assert_num_queries(P.QUERIES):
        found = P.score_assets(Asset.objects.all(), TODAY, START)
    assert len(found) == devices + 1  # and the old device the history fixture adds
    with django_assert_num_queries(P.QUERIES + 1):  # Settings, for the fiscal year's start
        P.score_assets(Asset.objects.all(), TODAY)
    one = Asset.objects.get(tag="CE-000")
    with django_assert_num_queries(P.QUERIES + 1):
        p = P.for_asset(one, TODAY)
    assert p.failures == 4 and p.unrecorded == 3 and p.extras["contract_share"] is not None and p.component(P.SAFETY).value == 0.0
