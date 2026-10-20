"""
Slice 36's review fixes (C1 to C15), each on its finding's own scenario:

C1  a vendor's work is left out of service cost only when the device's live contract pays for it: full service and parts and labor
    cover the contract's own vendor's work, PM only its PMs, parts only its parts, time and materials nothing; another vendor's job
    always counts.
C2  a live contract with a cost whose share cannot be worked out (no acquisition cost) makes service cost unknown, its 15 points shown
    as unknown (never a silent $0), as the Costs tab says; a $0 contract is a known share of 0.
C3  an imported repair still open is "still open", never also "imported: not recorded then".
C4  "supported" when checked within 12 calendar months, Feb 29 or not.
C5, C10, C11, C13  a support day before 2000 or more than 50 years ahead is refused (the service, the Record support modal, the API),
    and fiscal.describe never raises on a fiscal year beyond the calendar, so stored data never makes the Costs tab a 500.
C7  the rows' last cell (the CSV, the API, the email's attachment) is the rule's version with its weights.
C8  "kept out of use after a repair" reads the device's last repair by the binder's rule (repair_checks.last_completed_first).
C9  (a) the gap reads "not checked in the last 12 months"; (b) a confirmed recall done on this device says so.
C12 too little history names the day the records start and the day there will be enough, never "12 months ... and 12 are needed".
C14 the drawer's drivers round points as the table, the CSV, and the API do (half up).
C15 support's end and parts' end are their last days (passed the day after); a sale's end is its first day.
The day is pinned (the services and the views read the facility's clock).
"""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from capital_fixtures import TODAY, device, recall_match, work, years_before
from csvutil import csv_rows
from django.core.exceptions import ValidationError
from django.utils import timezone
from repair_fixtures import CHECKED, FINDING

from apps.capital import priority as P
from apps.contracts import services as ct_services
from apps.contracts.models import Coverage
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, LifeSource, RiskClass
from apps.facility import fiscal
from apps.facility import services as fac
from apps.pm.dates import add_months
from apps.recalls.models import AlertMatch
from apps.reports.fleet import REPLACE_COLUMNS, report_replace
from apps.web import asset_tabs
from apps.workorders import legacy, repair_checks
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, RepairCheck, RepairFinding, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

START = 7  # the fiscal year starts in July: TODAY is in FY2027
OLD = years_before(5)
HX = {"HTTP_HX_REQUEST": "true"}
YEAR_WRONG = "Enter the day in full (the year looks wrong)."
TOO_FAR = "Leave it blank when no day is announced (a day more than 50 years ahead looks wrong)."


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    """The facility's today is TODAY (the services, the drawer, the report's views and the API read the clock themselves)."""
    real = timezone.localdate
    monkeypatch.setattr(timezone, "localdate", lambda value=None, tz=None: TODAY if value is None else real(value, tz))


@pytest.fixture
def history(ctx, dept, pump_model):
    """The facility's first work order, five years ago: every device's window is the full 36 months."""
    old = device(pump_model, dept, "CE-OLD", years=9)
    work(old, WoType.PM, OLD, completed=OLD)
    return old


@pytest.fixture
def model(ctx):
    """A low-risk model, list price $2,000, its life source recorded and support checked a month ago: only cost can be unknown."""
    return DeviceModel.objects.create(manufacturer="Acme", model="S1", description="Suction pump", category="Suction", risk_class=RiskClass.LOW,
                                      expected_life_years=10, list_cost=Decimal("2000"), life_source=LifeSource.AHA,
                                      support_checked_on=TODAY - timedelta(days=30))


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def score(asset, today=TODAY) -> P.Priority:
    found = P.score_assets(Asset.objects.filter(pk=asset.pk), today, START)
    assert len(found) == 1
    return found[0]


def contract(coverage, *devices, annual="0", vendor="Acme"):
    c = ct_services.create_contract(reference="SC-1", vendor=vendor, coverage=coverage, start_on=TODAY - timedelta(days=100),
                                    end_on=TODAY + timedelta(days=100), annual_cost=Decimal(annual))
    for d in devices:
        ct_services.add_asset(c, d)
    return c


def costs_tab(client, asset) -> str:
    r = client.get(f"/equipment/{asset.tag}/?tab=costs", **HX)
    assert r.status_code == 200
    return r.content.decode()


def cost_row(body: str) -> str:
    """The "How it is scored" row of service cost."""
    how = body.split('<details class="cap-how">', 1)[1]
    return how.split("<td>Service cost a year against replacement cost</td>", 1)[1].split("</tr>", 1)[0]


# --- C1: a vendor's work against what the live contract covers -------------------------------------------------------------------------

YEARS = Decimal(1096) / Decimal("365.25")  # the full 36-month window before TODAY


def four_vendor_repairs(asset, vendor_name):
    """The scenario's year: four vendor repairs, each 5 h x $150 and a $400 part ($4,600: $3,000 labor, $1,600 parts), found worn."""
    for k in range(4):
        work(asset, WoType.REPAIR, TODAY - timedelta(days=40 + 60 * k), completed=TODAY - timedelta(days=38 + 60 * k), labor=[(5, 150)],
             parts=[(1, 400)], finding=RepairFinding.WEAR, vendor=True, vendor_name=vendor_name)


def test_c1_with_no_contract_the_vendors_work_counts(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    four_vendor_repairs(a, "Acme")
    p = score(a)
    cost = p.component(P.COST)
    assert p.extras["spent"] == Decimal("4600") and p.service_cost_year == Decimal("1532.98")
    assert (cost.value, cost.input, p.score) == (1.0, "77% a year of replacement ($1,533 a year)", 31)


@pytest.mark.parametrize("coverage, vendor_name, spent", [
    (Coverage.TM, "Acme", "4600"),  # time and materials: each job is billed, the contract pays for none of it
    (Coverage.PM_ONLY, "Acme", "4600"),  # PM only: a repair is billed on top
    (Coverage.PARTS, "Acme", "3000"),  # parts only: the vendor's labor is billed on top, its parts are the contract's
    (Coverage.FULL, "Philips", "4600"),  # another vendor's job: never the contract's
    (Coverage.PARTS_LABOR, "Philips field service", "4600"),
    (Coverage.FULL, "Acme", "0"),  # full service by the contract's own vendor: the share pays for it
    (Coverage.PARTS_LABOR, "ACME  Field Service", "0"),  # the same vendor written another way (vendors.rules.vendor_key)
])
def test_c1_a_vendors_work_is_left_out_only_when_the_live_contract_pays_for_it(history, dept, model, coverage, vendor_name, spent):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    contract(coverage, a, annual="0")  # a rate agreement: no annual fee
    four_vendor_repairs(a, vendor_name)
    p = score(a)
    assert p.extras["contract_share"] == Decimal("0") and p.extras["spent"] == Decimal(spent)
    assert p.service_cost_year == (Decimal(spent) / YEARS).quantize(Decimal("0.01"))
    if spent == "4600":  # as if there were no contract: the scenario's $1,533 a year, never $0
        assert p.component(P.COST).input == "77% a year of replacement ($1,533 a year)" and p.score == 31
    elif spent == "0":
        assert p.component(P.COST).input == "0% a year of replacement ($0 a year)" and p.component(P.COST).value == 0.0


def test_c1_pm_only_pays_for_its_vendors_pms_and_time_and_materials_for_none(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    c = contract(Coverage.PM_ONLY, a, annual="0")
    work(a, WoType.PM, TODAY - timedelta(days=30), completed=TODAY - timedelta(days=30), labor=[(2, 150)], vendor=True, vendor_name="Acme")
    work(a, WoType.REPAIR, TODAY - timedelta(days=20), completed=TODAY - timedelta(days=19), labor=[(1, 150)], vendor=True, vendor_name="Acme",
         finding=RepairFinding.WEAR)
    assert score(a).extras["spent"] == Decimal("150")  # the PM is the contract's, the repair is billed
    ct_services.update_contract(c, coverage=Coverage.TM)
    assert score(a).extras["spent"] == Decimal("450")


def test_c1_contract_covers():
    for wo_type in (WoType.REPAIR, WoType.PM):
        for part in (False, True):
            assert P.contract_covers(Coverage.FULL, wo_type, part) and P.contract_covers(Coverage.PARTS_LABOR, wo_type, part)
            assert not P.contract_covers(Coverage.TM, wo_type, part)
            assert P.contract_covers(Coverage.PARTS, wo_type, part) == part
            assert P.contract_covers(Coverage.PM_ONLY, wo_type, part) == (wo_type == WoType.PM)


# --- C2: a share that cannot be worked out is unknown, never $0 -----------------------------------------------------------------------


def test_c2_no_acquisition_cost_under_a_live_contract_with_a_cost_reads_unknown(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))  # the field's default: not on file
    contract(Coverage.FULL, a, annual="1800")
    work(a, WoType.REPAIR, TODAY - timedelta(days=40), completed=TODAY - timedelta(days=38), labor=[(10, 150)], vendor=True, vendor_name="Acme",
         finding=RepairFinding.WEAR)
    p = score(a)
    cost = p.component(P.COST)
    assert (cost.value, cost.input, cost.points) == (None, P.CONTRACT_SHARE_UNKNOWN, None)
    assert p.points_unknown == 15 and p.service_cost_year is None and p.extras["contract_share"] is None
    assert P.CONTRACT_SHARE_UNKNOWN == "its contract share cannot be worked out: no acquisition cost on file"


def test_c2_every_device_on_a_contract_with_no_acquisition_cost_reads_unknown(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    b = device(model, dept, "CE-2", years=6, acquisition_cost=Decimal("0"))
    contract(Coverage.FULL, a, b, annual="10000")  # the covered total is 0: the $10,000 is allocated to nobody
    for p in P.score_assets(Asset.objects.filter(pk__in=[a.pk, b.pk]), TODAY, START):
        assert p.component(P.COST).value is None and p.component(P.COST).input == P.CONTRACT_SHARE_UNKNOWN and p.points_unknown == 15


def test_c2_a_zero_cost_contract_is_a_known_share_of_zero(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    contract(Coverage.FULL, a, annual="0")
    p = score(a)
    cost = p.component(P.COST)
    assert p.extras["contract_share"] == Decimal("0") and p.service_cost_year == Decimal("0.00") and p.points_unknown == 0
    assert (cost.value, cost.input, cost.note) == (0.0, "0% a year of replacement ($0 a year)", "")


def test_c2_too_little_history_is_said_first(ctx, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    contract(Coverage.FULL, a, annual="1800")
    assert score(a).component(P.COST).input == P.TOO_LITTLE_HISTORY  # no work order in Cadence yet


def test_c2_the_costs_tab_and_its_outlook_say_the_same(client, signed_in, history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    contract(Coverage.FULL, a, annual="1800")
    signed_in("technician")
    body = costs_tab(client, a)
    assert "Under SC-1 · Acme, but with no acquisition cost on file its share of the contract cannot be worked out." in body
    row = cost_row(body)
    assert P.CONTRACT_SHARE_UNKNOWN in row and '<span class="muted">unknown</span>' in row and "0% a year" not in row
    assert "15 points unknown" in body


# --- C3: an imported repair still open ----------------------------------------------------------------------------------------------


def test_c3_an_open_imported_repair_is_still_open_never_not_recorded_then(history, dept, model):
    a = device(model, dept, "CE-1", years=6)
    legacy.import_work_order(asset=a, legacy_number="OLD-1", type=WoType.REPAIR, status=WoStatus.OPEN, opened_on=TODAY - timedelta(days=40),
                             today=TODAY)
    p = score(a)
    assert (p.failures, p.unrecorded, p.extras["still_open"], p.extras["imported"]) == (1, 1, 1, 0)
    assert p.component(P.RELIABILITY).note == "1 with no finding recorded (1 still open)"
    legacy.import_work_order(asset=a, legacy_number="OLD-2", type=WoType.REPAIR, status=WoStatus.CLOSED, opened_on=TODAY - timedelta(days=200),
                             completed_on=TODAY - timedelta(days=199), today=TODAY)
    p = score(a)
    assert p.component(P.RELIABILITY).note == "2 with no finding recorded (1 still open, 1 imported: not recorded then)"


# --- C4: checked within 12 calendar months --------------------------------------------------------------------------------------------


def unsaved_model(**kw):
    defaults = {"sale_ended_on": None, "support_ends_on": None, "parts_end_on": None, "support_checked_on": None}
    return DeviceModel(manufacturer="X", model="Y", description="Z", category="C", risk_class=RiskClass.LOW, **{**defaults, **kw})


@pytest.mark.parametrize("today", [date(2028, 10, 19), date(2028, 3, 1), date(2029, 2, 28), date(2027, 10, 19)])
def test_c4_a_check_twelve_months_ago_is_still_checked_across_feb_29(today):
    checked = add_months(today, -12)  # 366 days back for the first three (Feb 29, 2028 between), 365 for the last
    s = eq.support_status(unsaved_model(support_checked_on=checked), today)
    assert (s.key, s.value, s.words) == ("supported", 0.0, f"Supported, checked {checked:%b} {checked.day}, {checked.year}")
    before = checked - timedelta(days=1)
    s = eq.support_status(unsaved_model(support_checked_on=before), today)
    assert (s.key, s.words) == ("unknown", f"Support last checked {before:%b} {before.day}, {before.year}")


def test_c4_the_rule_scores_the_leap_anniversary_as_supported(history, dept, model):
    DeviceModel.objects.filter(pk=model.pk).update(support_checked_on=date(2027, 10, 19))
    p = score(device(model, dept, "CE-1", years=3), today=date(2028, 10, 19))
    assert p.support.key == "supported" and p.component(P.SUPPORT).value == 0.0 and P.SUPPORT_UNCHECKED not in p.gaps


# --- C5, C10, C11, C13: support days with a year that looks wrong ------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["sale_ended_on", "support_ends_on", "parts_end_on"])
@pytest.mark.parametrize("typed, words", [
    ("0028-03-31", YEAR_WRONG),  # Chrome's date box, the year typed "28"
    ("1999-12-31", YEAR_WRONG),
    (date(28, 3, 31), YEAR_WRONG),  # as the API's date field passes it
    ("9999-12-31", TOO_FAR),  # the "never" placeholder
    (date(9999, 12, 31), TOO_FAR),
])
def test_c5_the_service_refuses_a_support_day_whose_year_looks_wrong(ctx, vent_model, field, typed, words):
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(vent_model, **{field: typed})
    assert e.value.message_dict == {field: [words]}
    assert getattr(DeviceModel.objects.get(pk=vent_model.pk), field) is None


@pytest.mark.parametrize("typed", ["0026-10-19", "1999-12-31"])
def test_c10_the_day_support_was_checked_with_a_wrong_year_is_refused(ctx, vent_model, typed):
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(vent_model, support_checked_on=typed)
    assert e.value.message_dict == {"support_checked_on": [YEAR_WRONG]}


@pytest.mark.parametrize("field", ["sale_ended_on", "support_ends_on", "parts_end_on"])
def test_c5_a_far_but_plausible_support_day_is_accepted(ctx, vent_model, field):
    for day in (add_months(TODAY, 49 * 12), add_months(TODAY, 50 * 12), date(2000, 1, 1)):
        eq.update_device_model(vent_model, **{field: day})
        assert getattr(DeviceModel.objects.get(pk=vent_model.pk), field) == day
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(vent_model, **{field: add_months(TODAY, 50 * 12) + timedelta(days=1)})
    assert e.value.message_dict == {field: [TOO_FAR]}


def test_c10_record_support_shows_the_refusal_under_the_field_and_saves_nothing(client, signed_in, vent_model):
    signed_in("director")
    r = client.post(f"/pm/models/{vent_model.pk}/support/", {"life_source": "", "sale_ended_on": "", "support_ends_on": "",
                                                              "parts_end_on": "0028-03-31", "support_notice": "",
                                                              "support_checked_on": TODAY.isoformat()}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert body.split('id="sp-parts_end_on_error">', 1)[1].split("</span>", 1)[0] == YEAR_WRONG
    assert DeviceModel.objects.get(pk=vent_model.pk).parts_end_on is None


def test_c13_the_api_refuses_the_never_placeholder(client, ctx, make_user, vent_model):
    client.force_login(make_user("director"))
    r = client.patch(f"/api/v1/device-models/{vent_model.pk}/", json.dumps({"support_ends_on": "9999-12-31"}), content_type="application/json")
    assert r.status_code == 400 and r.json() == {"support_ends_on": [TOO_FAR]}
    assert DeviceModel.objects.get(pk=vent_model.pk).support_ends_on is None


def test_c11_describe_names_a_fiscal_year_beyond_the_calendar_without_its_dates():
    assert fiscal.describe(10000, 7) == "FY10000"
    assert fiscal.describe(9999, 1) == "FY9999: Jan 1, 9999 to Dec 31, 9999"
    assert fiscal.describe(2027, 7) == "FY2027: Jul 1, 2026 to Jun 30, 2027"


def test_c11_the_costs_tab_answers_for_a_far_support_day_and_a_stored_placeholder(client, signed_in, history, dept, vent_model):
    fac.update_settings(fiscal_year_start_month="7")
    a = device(vent_model, dept, "CE-77")  # no install date: support's end is the only day to place it by
    far = add_months(TODAY, 49 * 12)
    eq.update_device_model(vent_model, support_ends_on=far)
    signed_in("technician")
    fy = fiscal.fiscal_year_of(far, 7)
    assert f"FY{fy}" in costs_tab(client, a)
    DeviceModel.objects.filter(pk=vent_model.pk).update(support_ends_on=date(9999, 12, 31))  # a row from before the bound
    assert P.for_asset(Asset.objects.get(pk=a.pk), TODAY).suggested_fy == 10000
    assert ">FY10000</div>" in costs_tab(client, a)


# --- C7: the rows carry the version with its weights --------------------------------------------------------------------------------


def test_c7_the_csv_and_api_rows_end_with_the_version_and_its_weights(client, signed_in, history, dept, model):
    device(model, dept, "CE-1", years=6)
    words = P.version_words()
    assert words.startswith("Score v2: age 30, support 25, failures 20, service cost 15, recall 10; times 1.0 life support")
    assert {row[-1] for row in report_replace(TODAY)["rows"]} == {words}  # the email's attachment is these rows
    signed_in("director")
    rows = csv_rows(client.get("/reports/replace.csv"))
    assert rows[0] == REPLACE_COLUMNS and rows[0][-1] == "Score version" and {r[-1] for r in rows[1:]} == {words}
    data = client.get("/api/v1/reports/replace/").json()
    assert data["columns"][-1] == "Score version" and {r[-1] for r in data["rows"]} == {words}


# --- C8: the device's last repair by the binder's rule ------------------------------------------------------------------------------


def tagged_out_repair(asset, tech) -> WorkOrder:
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low tidal volume alarm", tag_out=True)
    assign(wo, technician=tech)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def not_returned(wo):
    return complete_work_order(wo, resolution="Blower failed: beyond repair", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING)


def last_repair(asset) -> WorkOrder:
    done = WorkOrder.objects.filter(asset=asset, type=WoType.REPAIR, status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))
    return repair_checks.last_completed_first(done).first()


def test_c8_the_lower_number_completed_last_as_checked_clears_the_flag(vent, techs):
    """The scenario: WO-2 (created later) completed first as not returned, then WO-1 completed the same day as checked; WO-3 still
    holds the device out."""
    one, two, three = (tagged_out_repair(vent, techs["dana"]) for _ in range(3))
    not_returned(two)
    complete_work_order(one, resolution="Replaced the flow sensor", **CHECKED)
    vent.refresh_from_db()
    assert one.completed_on == two.completed_on == TODAY and vent.status == AssetStatus.OUT_OF_SERVICE and three.status == WoStatus.IN_PROGRESS
    assert last_repair(vent).pk == one.pk  # the binder's "last repair"
    p = P.for_asset(vent, TODAY, START)
    assert P.NOT_RETURNED not in p.flags and not p.must_act


def test_c8_the_lower_number_completed_last_not_returned_raises_the_flag(vent, techs):
    one, two = (tagged_out_repair(vent, techs["dana"]) for _ in range(2))
    complete_work_order(two, resolution="Replaced the flow sensor", **CHECKED)  # WO-1 still holds the device out
    not_returned(one)
    vent.refresh_from_db()
    assert one.completed_on == two.completed_on == TODAY and vent.status == AssetStatus.OUT_OF_SERVICE
    assert last_repair(vent).pk == one.pk
    p = P.for_asset(vent, TODAY, START)
    assert P.NOT_RETURNED in p.flags and p.must_act and p.suggested_fy == 2027


# --- C9: an old check, a recall done ------------------------------------------------------------------------------------------------


def test_c9a_an_old_check_is_not_checked_in_the_last_12_months(client, signed_in, history, dept, model):
    DeviceModel.objects.filter(pk=model.pk).update(support_checked_on=date(2025, 9, 1))  # 13 months before TODAY
    p = score(device(model, dept, "CE-1", years=3))
    assert p.support.words == "Support last checked Sep 1, 2025" and P.SUPPORT_UNCHECKED in p.gaps
    assert P.GAP_WORDS[P.SUPPORT_UNCHECKED] == "Manufacturer support not checked in the last 12 months"
    gap = next(g for g in report_replace(TODAY)["gaps"] if g["key"] == P.SUPPORT_UNCHECKED)
    assert gap["words"] == "Manufacturer support not checked in the last 12 months" and model in gap["device_models"]
    assert gap["devices"] == 2  # and the history fixture's pump, its model never checked
    signed_in("director")
    assert "Manufacturer support not checked in the last 12 months: 2 devices" in client.get("/reports/replace/").content.decode()


def test_c9b_a_confirmed_recall_done_on_this_device_says_so(history, dept, model):
    a = device(model, dept, "CE-1", years=4)
    b = device(model, dept, "CE-2", years=4)
    assert score(a).component(P.SAFETY).input == "no confirmed recall"
    recall_match(model, status=AlertMatch.Status.NEEDS_ACTION)
    assert score(a).component(P.SAFETY).input == "no confirmed recall"  # not yet confirmed
    m = recall_match(model, classification="Class I")  # confirmed: in progress
    work(a, WoType.RECALL, TODAY - timedelta(days=5), completed=TODAY - timedelta(days=4), alert=m.alert)  # done on A, not on B
    p = score(a)
    assert (p.component(P.SAFETY).value, p.component(P.SAFETY).input, p.flags) == (0.0, "confirmed recall done on this device", ())
    assert score(b).component(P.SAFETY).input == "1 confirmed recall not done on this device, Class I"


# --- C12: too little history in words ------------------------------------------------------------------------------------------------


def test_c12_a_device_days_short_of_a_year_reads_when_it_will_have_enough(history, dept, model):
    a = device(model, dept, "CE-21", installed_on=date(2025, 10, 20))  # 364 days before TODAY
    p = score(a)
    assert p.failures is None and p.extras["window_months"] == 12  # the rounded count that read as the minimum
    words = asset_tabs.replacement_outlook(a, TODAY)["window_words"]
    assert words == ("Failures and service cost have too little history: the records start Oct 20, 2025, and 12 months are needed "
                     "(from Oct 20, 2026).")
    assert "12 are needed" not in words and "12 months since" not in words


# --- C14: one rounding of points ------------------------------------------------------------------------------------------------------


def test_c14_eleven_and_a_quarter_points_read_eleven_point_three_everywhere(client, signed_in, history, dept, vent_model):
    DeviceModel.objects.filter(pk=vent_model.pk).update(list_cost=Decimal("2000"))  # life support: times 1.0
    a = device(vent_model, dept, "CE-1", years=8, acquisition_cost=Decimal("1000"))
    contract(Coverage.FULL, a, annual="475")  # the only device: its share is $475 a year, 23.75% of $2,000
    p = score(a)
    assert p.component(P.COST).value == 0.75 and p.component(P.COST).points == 11.25  # an exact binary tie
    assert P.points_rounded(11.25) == Decimal("11.3") and P.points_rounded(None) is None
    signed_in("director")
    body = costs_tab(client, a)
    assert "Driven most by age against expected life (16.0 points) and service cost a year against replacement cost (11.3 points)." in body
    assert cost_row(body).endswith('<td class="num">15</td><td class="num">11.3</td>')
    rows = csv_rows(client.get("/reports/replace.csv"))
    row = next(r for r in rows if r[0] == "CE-1")
    assert row[rows[0].index("Service cost points")] == "11.3"


# --- C15: last days ----------------------------------------------------------------------------------------------------------------


def test_c15_parts_available_until_today_are_announced_and_passed_tomorrow(history, dept, model):
    eq.update_device_model(model, parts_end_on=TODAY)
    s = eq.support_status(model, TODAY)
    assert (s.key, s.value, s.words) == ("announced", 0.2, "Parts available until Oct 19, 2026")
    a = device(model, dept, "CE-1", years=3)
    p = score(a)
    assert p.flags == () and not p.must_act
    s = eq.support_status(model, TODAY + timedelta(days=1))
    assert (s.key, s.value, s.words) == ("parts_ended", 1.0, "Parts no longer available after Oct 19, 2026")
    p = score(a, today=TODAY + timedelta(days=1))
    assert p.flags == (P.PARTS_ENDED,) and p.must_act


def test_c15_support_ending_today_is_announced_and_passed_tomorrow(history, dept, model):
    eq.update_device_model(model, support_ends_on=TODAY)
    s = eq.support_status(model, TODAY)
    assert (s.key, s.value, s.words) == ("announced", 0.2, "Support ends Oct 19, 2026")
    a = device(model, dept, "CE-1", years=3)
    assert score(a).flags == ()
    s = eq.support_status(model, TODAY + timedelta(days=1))
    assert (s.key, s.value, s.words) == ("support_ended", 0.8, "Manufacturer support ended Oct 19, 2026")
    assert score(a, today=TODAY + timedelta(days=1)).flags == (P.SUPPORT_ENDED,)


def test_c15_a_sale_ending_today_has_passed(history, dept, model):
    eq.update_device_model(model, sale_ended_on=TODAY)
    s = eq.support_status(model, TODAY)
    assert (s.key, s.value, s.words) == ("sale_ended", 0.4, "No longer sold since Oct 19, 2026")
    s = eq.support_status(model, TODAY - timedelta(days=1))
    assert (s.key, s.words) == ("announced", "Sale ends Oct 19, 2026")
