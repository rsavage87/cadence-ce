"""
Slice 36's review fixes (C1 to C15), each on its finding's own scenario:

C1  a vendor's work is left out of service cost only when the device's live contract pays for it: full service and parts and labor
    cover the contract's own vendor's work, PM only its PMs, parts only its parts, time and materials nothing; another vendor's job
    always counts.
C2  a live contract with a cost whose share cannot be worked out (no acquisition cost) makes service cost unknown, its 15 points shown
    as unknown (never a silent $0), as the Costs tab says; a $0 contract is a known share of 0.
C3  an imported repair still open is "still open", never also "imported: not recorded then".
C4  "supported" when checked within 12 calendar months, Feb 29 or not.
C5, C10, C11, C13  a support day with a year that looks wrong or more than 50 years ahead is refused (the service, the Record support
    modal, the API), and fiscal.describe never raises on a fiscal year beyond the calendar, so stored data never makes the Costs tab a 500.
C7  the screen, the print, and the drawer print the rule's version with its weights; the rows' last cell (the CSV, the API, the email's
    attachment) is "v2" (round 3: the weights on every row would take a full-size fleet's CSV past the devices import's 5 MB).
C8  "kept out of use after a repair" reads the device's last repair by the binder's rule (repair_checks.last_completed_first).
C9  (a) the gap reads "not checked in the last 12 months"; (b) a confirmed recall done on this device says so.
C12 too little history names the day the records start and the day there will be enough, never "12 months ... and 12 are needed".
C14 the drawer's drivers round points as the table, the CSV, and the API do (half up).
C15 support's end and parts' end are their last days (passed the day after); a sale's end is its first day.

Rounds 2 and 3 (after the skeptics, each on the skeptic's own scenario; round 3's rule of whose a line is replaced round 2's):
R1  whose a line is: the vendor's only on a work order that is vendor service now (labor with no technician, and every part line);
    labor a technician logged on a vendor repair is ours, and every line of work that is in-house now is ours, whoever billed it before
    it came back (the contract's vendor, or a third party).
R2  what a line is: an imported outside cost, total, or in-house time written as a part line on an imported work order is never a part
    (parts only never leaves it out); the same words typed as a part on live work are a part.
R3  coverage is monotonic: a wider coverage never pays for less on the same records; a part fitted in-house is never the contract's.
R4  a contract that costs $0 pays for nothing recorded: its vendor's work counts, its share is a known $0, and the Costs tab agrees.
R5  a vendor job with no vendor name on a device under a live contract is the contract's vendor's.
R6  a share that cannot be worked out, with the lines counted alone at the top of the scale: known 1.0, "at least N%" (the report's
    cell too); below the top: unknown.
R7  a sale's, support's, or parts' end from 1950 is accepted (a 1990s milestone is real); the check day from 2000; 0028 refused.
R8  an announced sale's end reads "No longer sold from <day>" (its first day).
R9  the Costs tab never prints "suggested fiscal year, " with nothing after it.
R10 the admin shows the expected life and the six support fields read only (update_device_model is their only writer).
R11 enough_history_on(start) is the first day window_of counts enough (a Feb 29 start: Mar 1), and the drawer's words name it; with no
    work order in Cadence yet, the words name no day ("no work orders in Cadence yet").
R12 recalls done on this device are counted ("2 confirmed recalls done on this device").
R13 (round 2's compact version cell on every row) was undone by round 3: see C7.
R14the documented limitations, pinned: a contract whose vendor was renamed since counts the earlier vendor work on top; work moved
    from one vendor to another is judged by the vendor named now; a part fitted in-house before the repair went to the vendor is the
    vendor's.
R15 a contract pays only for work completed on or after its start: a part bought before a parts only contract started, and the same
    vendor's billed work before a full-service contract was signed, are counted; work completed on its first day is the contract's.
R16 a life past the calendar, saved outside update_device_model, never raises: the rule's life end is unknown.
The day is pinned (the services and the views read the facility's clock).
"""
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from capital_fixtures import TODAY, device, recall_match, work, years_before
from csvutil import csv_rows
from django.core.exceptions import ValidationError
from django.utils import timezone
from repair_fixtures import CHECKED, FINDING

from apps.accounts.models import User
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
from apps.web import asset_tabs, reports_fleet
from apps.workorders import legacy, repair_checks
from apps.workorders.completion import complete_work_order
from apps.workorders.models import PartLine, Priority, RepairCheck, RepairFinding, WorkOrder, WoStatus, WoType
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


def contract(coverage, *devices, annual="0", vendor="Acme", reference="SC-1", start=None):
    """A contract on the devices, started a year ago unless `start` is given."""
    c = ct_services.create_contract(reference=reference, vendor=vendor, coverage=coverage, start_on=start or TODAY - timedelta(days=365),
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


# --- C1, round 4: every line recorded counts, plus the contract share (the Overview's cost of service, per device) -------------------

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


@pytest.mark.parametrize("coverage", list(Coverage))
@pytest.mark.parametrize("vendor_name", ["Acme", "OtherCo Imaging", ""])
def test_c1_every_line_recorded_counts_whatever_the_contract_covers(history, dept, model, coverage, vendor_name):
    """The review's C1: a time and materials, PM only, or parts only contract dropped the billed work. Round 4: which past lines a contract
    paid for cannot be told from the records, so every line counts on top of the share, as the Overview's cost of service counts it."""
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    contract(coverage, a, annual="100")  # the only device: its share is the $100
    four_vendor_repairs(a, vendor_name)
    p = score(a)
    assert p.extras["contract_share"] == Decimal("100") and p.extras["spent"] == Decimal("4600")
    assert p.service_cost_year == (Decimal("4600") / YEARS + 100).quantize(Decimal("0.01"))
    assert p.component(P.COST).input == "82% a year of replacement ($1,633 a year)" and p.score == 31


def test_c1_a_covered_visit_recorded_at_no_charge_adds_nothing(history, dept, model):
    """How a facility keeps a visit its contract paid for out of every cost figure: its time at a $0 rate and its parts at no charge."""
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    contract(Coverage.FULL, a, annual="100")
    work(a, WoType.REPAIR, TODAY - timedelta(days=30), completed=TODAY - timedelta(days=29), labor=[(5, 0)], parts=[(1, 0)],
         finding=RepairFinding.WEAR, vendor=True, vendor_name="Acme")
    work(a, WoType.REPAIR, TODAY - timedelta(days=20), completed=TODAY - timedelta(days=19), labor=[(2, 90)], finding=RepairFinding.WEAR)
    work(a, WoType.PM, TODAY - timedelta(days=10), completed=TODAY - timedelta(days=10), parts=[(2, 25)], vendor=True, vendor_name="Acme")
    p = score(a)
    assert p.extras["spent"] == Decimal("230") and p.extras["contract_share"] == Decimal("100")


def test_c1_recall_work_never_counts_and_every_other_kind_does(history, dept, model):
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    for kind, cost in ((WoType.RECALL, 1000), (WoType.REPAIR, 100), (WoType.PM, 10), (WoType.SAFETY, 1)):
        work(a, kind, TODAY - timedelta(days=30), completed=TODAY - timedelta(days=29), labor=[(1, cost)],
             finding=RepairFinding.WEAR if kind == WoType.REPAIR else None)
    assert score(a).extras["spent"] == Decimal("111")


def test_c1_a_contract_not_started_yet_is_shared_as_the_costs_tab_shares_it(client, signed_in, history, dept, model):
    """Contract.is_expired, the Costs tab, and cost_of_service read a contract that has not ended; the rule reads the same."""
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("1000"))
    contract(Coverage.FULL, a, annual="100", start=TODAY + timedelta(days=10))
    assert score(a).extras["contract_share"] == Decimal("100")
    signed_in("director")
    assert "$100" in costs_tab(client, a)


def test_c1_the_rule_reads_no_coverage_rule():
    assert not hasattr(P, "contract_covers")


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
    ("1949-12-31", YEAR_WRONG),  # before SUPPORT_EARLIEST_YEAR (round 2: a 1990s milestone is real history)
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


# --- C7: the rows end with "v2"; the screen, the print, and the drawer print the weights ------------------------------------------------


def test_c7_the_rows_end_with_v2_and_the_screen_print_and_drawer_print_the_weights(client, signed_in, history, dept, model):
    """Skeptics (rest #5, edges2): the weights on every row cost about 90 bytes, and a 20,000-row fleet's CSV, read back through the
    devices import, would pass its 5 MB limit; round 3 puts "v2" back (f"v{SCORE_VERSION}") and drops version_cell."""
    a = device(model, dept, "CE-1", years=6)
    assert not hasattr(P, "version_cell")
    assert {row[-1] for row in report_replace(TODAY)["rows"]} == {"v2"}  # the email's attachment is these rows
    signed_in("director")
    rows = csv_rows(client.get("/reports/replace.csv"))
    assert rows[0] == REPLACE_COLUMNS and rows[0][-1] == "Score version" and {r[-1] for r in rows[1:]} == {"v2"}
    line = next(x for x in csv_text_of(client.get("/reports/replace.csv")).splitlines() if x.startswith("CE-1,"))
    assert line.endswith(",yes,v2")
    data = client.get("/api/v1/reports/replace/").json()
    assert data["columns"][-1] == "Score version" and {r[-1] for r in data["rows"]} == {"v2"}
    words = P.version_words()
    assert words == ("Score v2: age 30, support 25, failures 20, service cost 15, recall 10; times 1.0 life support, 0.95 high, 0.9 medium, "
                     "0.85 low risk")
    assert words in client.get("/reports/replace/").content.decode()
    assert words in client.get("/print/reports/replace/").content.decode()
    assert asset_tabs.replacement_outlook(a, TODAY)["version"] == words


def csv_text_of(response) -> str:
    return b"".join(response.streaming_content).decode("utf-8-sig")


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
    assert (p.component(P.SAFETY).value, p.component(P.SAFETY).input, p.flags) == (0.0, "1 confirmed recall done on this device", ())
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
    assert (s.key, s.words) == ("announced", "No longer sold from Oct 19, 2026")  # round 2: a sale's end is its first day


# === Rounds 2 and 3: the skeptics' scenarios ===================================================================================================


def cost_words(p) -> tuple:
    c = p.component(P.COST)
    return c.value, c.input, c.note


def complete(wo, opened, completed):
    change_status(wo, WoStatus.IN_PROGRESS, as_of=opened)
    change_status(wo, WoStatus.COMPLETED, as_of=completed)
    wo.refresh_from_db()
    return wo


# --- R4: a $0 contract pays for nothing recorded -------------------------------------------------------------------------------------


def test_r4_a_zero_cost_contract_counts_its_vendors_work_with_a_known_zero_share(client, signed_in, history, dept, model):
    """Skeptics (cost #6, #12, #18): no acquisition cost, a live full-service Acme contract left at the form's default $0, and
    $3,000 of Acme repairs (10 h x $150 and a $1,500 part) 60 days ago."""
    a = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    contract(Coverage.FULL, a, annual="0")
    work(a, WoType.REPAIR, TODAY - timedelta(days=60), completed=TODAY - timedelta(days=58), labor=[(10, 150)], parts=[(1, 1500)], vendor=True,
         vendor_name="Acme", finding=RepairFinding.WEAR)
    p = score(a)
    assert p.extras["contract_share"] == Decimal("0") and p.extras["spent"] == Decimal("3000") and p.points_unknown == 0
    assert cost_words(p) == (1.0, "50% a year of replacement ($1,000 a year)", "")
    assert asset_tabs.contract_share(a) == 0.0  # the Costs tab reads it the same way, acquisition cost or not
    signed_in("technician")
    body = costs_tab(client, a)
    assert "cannot be worked out" not in body and "SC-1 · Acme" in body and "shared across its devices by acquisition cost." in body
    assert '<div class="v">$0</div><div class="l">annual contract share</div>' in body
    assert "50% a year of replacement ($1,000 a year)" in cost_row(body) and "points unknown" not in body


# --- R6: an unknown share with the counted lines already at the top -----------------------------------------------------------------


def test_r6_the_lines_alone_at_the_top_are_known_whatever_the_share(client, signed_in, history, dept, model):
    """Skeptic (cost #5): CE-1 and CE-2 alike, each the only device on its own live T&M Acme contract at $1,800 a year, four Acme repairs
    ($4,600 billed); CE-1 has no acquisition cost, CE-2 one of $1,000."""
    one = device(model, dept, "CE-1", years=6, acquisition_cost=Decimal("0"))
    two = device(model, dept, "CE-2", years=6, acquisition_cost=Decimal("1000"))
    contract(Coverage.TM, one, annual="1800", reference="SC-1")
    contract(Coverage.TM, two, annual="1800", reference="SC-2")
    four_vendor_repairs(one, "Acme")
    four_vendor_repairs(two, "Acme")
    p1, p2 = score(one), score(two)
    assert cost_words(p1) == (1.0, "at least 77% a year of replacement ($1,533 a year before its contract share)", P.CONTRACT_SHARE_UNKNOWN)
    assert p1.extras["cost_at_least"] is True and p1.extras["contract_share"] is None and p1.service_cost_year is None
    assert p1.points_unknown == 0 and p1.component(P.COST).points == 15
    assert cost_words(p2)[:2] == (1.0, "167% a year of replacement ($3,333 a year)") and p2.extras["cost_at_least"] is False
    assert p1.score == p2.score == 31  # never 13 points lower for a field that only splits the contract
    assert reports_fleet.cost_pct(p1) == "at least 77%" and reports_fleet.cost_pct(p2) == "167%"
    signed_in("director")
    body = client.get("/reports/replace/").content.decode()
    assert '<td class="num" title="at least 77% a year of replacement ($1,533 a year before its contract share)">at least 77%</td>' in body
    assert "at least 77% a year of replacement ($1,533 a year before its contract share)" in cost_row(costs_tab(client, one))


def test_r6_below_the_top_the_unknown_share_could_still_move_the_value(history, dept, model):
    """A year's window (the rate divides by one year): $599.99 counted is under 30% of $2,000, unknown; one more cent reaches the top."""
    a = device(model, dept, "CE-1", installed_on=add_months(TODAY, -12), acquisition_cost=Decimal("0"))
    contract(Coverage.TM, a, annual="1800")
    wo = work(a, WoType.REPAIR, TODAY - timedelta(days=10), completed=TODAY - timedelta(days=9), parts=[(1, "599.99")], vendor=True,
              vendor_name="Acme", finding=RepairFinding.WEAR)
    p = score(a)
    assert p.window_years < 1 and p.extras["spent"] == Decimal("599.99")
    assert cost_words(p) == (None, P.CONTRACT_SHARE_UNKNOWN, "") and p.points_unknown == 15
    assert p.extras["cost_at_least"] is False and p.extras["cost_ratio"] is None and reports_fleet.cost_pct(p) == ""
    PartLine.objects.create(work_order=wo, description="Fuse", quantity=Decimal("1"), unit_cost=Decimal("0.01"))
    p = score(a)
    assert cost_words(p) == (1.0, "at least 30% a year of replacement ($600 a year before its contract share)", P.CONTRACT_SHARE_UNKNOWN)
    assert p.points_unknown == 0 and reports_fleet.cost_pct(p) == "at least 30%"


# --- R7: milestone days from 1950, the check day from 2000 -----------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["sale_ended_on", "support_ends_on", "parts_end_on"])
def test_r7_a_1990s_milestone_is_accepted_and_a_two_digit_year_refused(ctx, vent_model, field):
    for day in (date(1999, 12, 31), "1998-06-30", date(1950, 1, 1)):
        eq.update_device_model(vent_model, **{field: day})
        assert getattr(DeviceModel.objects.get(pk=vent_model.pk), field) == (date.fromisoformat(day) if isinstance(day, str) else day)
    for typed in ("0028-03-31", date(28, 3, 31), date(1949, 12, 31)):
        with pytest.raises(ValidationError) as e:
            eq.update_device_model(vent_model, **{field: typed})
        assert e.value.message_dict == {field: [YEAR_WRONG]}
    assert getattr(DeviceModel.objects.get(pk=vent_model.pk), field) == date(1950, 1, 1)
    assert eq.SUPPORT_EARLIEST_YEAR == 1950


def test_r7_the_day_support_was_checked_is_still_from_2000(ctx, vent_model):
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(vent_model, support_checked_on=date(1999, 12, 31))
    assert e.value.message_dict == {"support_checked_on": [YEAR_WRONG]}
    eq.update_device_model(vent_model, support_checked_on=date(2000, 1, 1))
    assert DeviceModel.objects.get(pk=vent_model.pk).support_checked_on == date(2000, 1, 1)


def test_r7_a_1990s_sale_and_support_end_save_through_the_modal_and_the_api_and_score(client, signed_in, history, dept, vent_model):
    """Skeptic (support #2, #6, #10): a sterilizer still in use whose sale ended Jun 30, 1998 and support Jun 30, 1999."""
    a = device(vent_model, dept, "CE-1", years=8)
    signed_in("director")
    r = client.post(f"/pm/models/{vent_model.pk}/support/", {"life_source": "", "sale_ended_on": "1998-06-30", "support_ends_on": "",
                                                              "parts_end_on": "", "support_notice": "",
                                                              "support_checked_on": TODAY.isoformat()}, **HX)
    assert r.status_code == 200 and YEAR_WRONG not in r.content.decode()
    assert DeviceModel.objects.get(pk=vent_model.pk).sale_ended_on == date(1998, 6, 30)
    assert score(a).support.words == "No longer sold since Jun 30, 1998" and score(a).component(P.SUPPORT).value == 0.4
    r = client.patch(f"/api/v1/device-models/{vent_model.pk}/", json.dumps({"support_ends_on": "1999-06-30"}), content_type="application/json")
    assert r.status_code == 200, r.json()
    p = score(a)
    assert (p.support.key, p.support.words) == ("support_ended", "Manufacturer support ended Jun 30, 1999") and P.SUPPORT_ENDED in p.flags


# --- R8: an announced sale's end is its first day -----------------------------------------------------------------------------------


def test_r8_an_announced_sale_end_reads_no_longer_sold_from(history, dept, model):
    """Skeptic (support #3): No longer sold from Oct 20, 2026, read on Oct 19, and tied with support's last day."""
    tomorrow = TODAY + timedelta(days=1)
    eq.update_device_model(model, sale_ended_on=tomorrow)
    s = eq.support_status(model, TODAY)
    assert (s.key, s.value, s.words) == ("announced", 0.2, "No longer sold from Oct 20, 2026")
    eq.update_device_model(model, support_ends_on=tomorrow)
    assert eq.support_status(model, TODAY).words == "No longer sold from Oct 20, 2026"
    device(model, dept, "CE-1", years=3)
    row = next(r for r in report_replace(TODAY)["rows"] if r[0] == "CE-1")
    assert row[REPLACE_COLUMNS.index("Support")] == "No longer sold from Oct 20, 2026"
    s = eq.support_status(model, tomorrow)
    assert (s.key, s.words) == ("sale_ended", "No longer sold since Oct 20, 2026")


# --- R9: no dangling comma on the Costs tab ------------------------------------------------------------------------------------------


def test_r9_the_costs_tab_never_prints_a_dangling_comma_for_a_stored_fy10000(client, signed_in, history, dept, vent_model):
    """Skeptic (support #4): support_ends_on 9999-12-31 stored before the bound, a July fiscal year, a device with no install date."""
    fac.update_settings(fiscal_year_start_month="7")
    a = device(vent_model, dept, "CE-77")
    DeviceModel.objects.filter(pk=vent_model.pk).update(support_ends_on=date(9999, 12, 31))
    signed_in("technician")
    body = costs_tab(client, a)
    assert '<div class="v">FY10000</div><div class="l">suggested fiscal year</div>' in body and "suggested fiscal year, " not in body
    eq.update_device_model(vent_model, support_ends_on=date(2030, 3, 31))
    body = costs_tab(client, a)
    assert ('<div class="v" title="FY2030: Jul 1, 2029 to Jun 30, 2030">FY2030</div><div class="l">suggested fiscal year, Jul 1, 2029 to '
            'Jun 30, 2030</div>') in body


# --- R10: the admin never writes the expected life or the support fields --------------------------------------------------------------


def test_r10_the_admin_shows_expected_life_and_the_six_support_fields_read_only(client, ctx, vent_model):
    """Skeptics (support #9, edges2 #1): a staff user's /admin/ change page would save a 9999-12-31 without _support_fields' bounds, and
    an expected life of 32767 years without update_device_model's 1 to 50 (the rule's date arithmetic then went past the calendar)."""
    client.force_login(User.objects.create_superuser("root", "root@example.com", "Root-Pass-2026", tenant=ctx))
    url = f"/admin/equipment/devicemodel/{vent_model.pk}/change/"
    r = client.get(url)
    body = r.content.decode()
    assert r.status_code == 200 and 'name="manufacturer"' in body  # the change form itself
    for name in ("expected_life_years", *eq.SUPPORT_FIELDS):
        assert f'name="{name}"' not in body, name
    form = r.context["adminform"].form
    data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
    r = client.post(url, {**data, "expected_life_years": "32767", "sale_ended_on": "9999-12-31", "description": "ICU ventilator, adult"})
    assert r.status_code == 302
    saved = DeviceModel.objects.get(pk=vent_model.pk)
    assert saved.description == "ICU ventilator, adult"  # the admin still saves what it may
    assert (saved.expected_life_years, saved.sale_ended_on) == (10, None)


# --- R11: the first day with enough history, by the window's own test ------------------------------------------------------------------


def test_r11_enough_history_on_is_the_first_day_window_of_counts_enough():
    assert P.enough_history_on(date(2028, 2, 29)) == date(2029, 3, 1)  # never Feb 28: window_of still reads too little then
    assert P.enough_history_on(date(2025, 10, 20)) == date(2026, 10, 20)
    assert P.enough_history_on(date(2027, 2, 28)) == date(2028, 2, 28)
    assert P.enough_history_on(date(2024, 1, 31)) == date(2025, 1, 31)
    day = date(2023, 1, 1)
    while day < date(2025, 1, 1):  # every start across a leap year
        on = P.enough_history_on(day)
        started = SimpleNamespace(installed_on=day)
        assert P.window_of(started, on, date(2000, 1, 1))[2] and not P.window_of(started, on - timedelta(days=1), date(2000, 1, 1))[2], day
        day += timedelta(days=1)


def test_r11_the_drawer_names_the_day_the_rule_counts_enough_for_a_feb_29_start(history, dept, model):
    """Skeptic (rest #1): a device installed Feb 29, 2028, read on Feb 28, 2029, and on Mar 1."""
    a = device(model, dept, "CE-29", installed_on=date(2028, 2, 29))
    feb_28, mar_1 = date(2029, 2, 28), date(2029, 3, 1)
    assert score(a, today=feb_28).failures is None
    assert asset_tabs.replacement_outlook(a, feb_28)["window_words"] == (
        "Failures and service cost have too little history: the records start Feb 29, 2028, and 12 months are needed (from Mar 1, 2029).")
    assert score(a, today=mar_1).failures == 0
    assert asset_tabs.replacement_outlook(a, mar_1)["window_words"] == "Failures and service cost count the 12 months since Feb 29, 2028."


def test_r11_with_no_work_orders_in_cadence_yet_the_drawer_names_no_day(client, signed_in, ctx, dept, model):
    """Skeptic (edges2 #4): a new facility, its devices imported, no work order yet: window_of's start is today every day, so the drawer
    once promised enough history "from" a day a year off that moved every day. It names no day until the first work order."""
    a = device(model, dept, "CE-1", years=6)
    words = "Failures and service cost have too little history: no work orders in Cadence yet."
    for today in (TODAY, TODAY + timedelta(days=365)):
        p = score(a, today=today)
        assert p.failures is None and p.extras["first_day"] is None
        assert asset_tabs.replacement_outlook(a, today)["window_words"] == words
    signed_in("technician")
    body = costs_tab(client, a)
    assert words in body and "records start" not in body
    work(a, WoType.PM, TODAY - timedelta(days=10), completed=TODAY - timedelta(days=10))  # the facility's first work order
    assert score(a).extras["first_day"] == TODAY - timedelta(days=10)
    assert asset_tabs.replacement_outlook(a, TODAY)["window_words"] == (
        "Failures and service cost have too little history: the records start Oct 9, 2026, and 12 months are needed (from Oct 9, 2027).")


# --- R12: recalls done on this device, counted ---------------------------------------------------------------------------------------


def test_r12_two_confirmed_recalls_done_on_this_device_read_plural(history, dept, model):
    """Skeptic (rest #2): two in-progress confirmed recalls on the model (Class I and Class II), both done on this device."""
    a = device(model, dept, "CE-1", years=4)
    b = device(model, dept, "CE-2", years=4)
    one = recall_match(model, classification="Class I")
    two = recall_match(model)
    work(a, WoType.RECALL, TODAY - timedelta(days=5), completed=TODAY - timedelta(days=4), alert=one.alert)
    assert score(a).component(P.SAFETY).input == "1 confirmed recall not done on this device"
    work(a, WoType.RECALL, TODAY - timedelta(days=3), completed=TODAY - timedelta(days=2), alert=two.alert)
    p = score(a)
    assert (p.component(P.SAFETY).value, p.component(P.SAFETY).input, p.flags) == (0.0, "2 confirmed recalls done on this device", ())
    assert score(b).component(P.SAFETY).input == "2 confirmed recalls not done on this device, Class I"


# --- R16: a life past the calendar never raises ----------------------------------------------------------------------------------------


def test_r16_a_life_of_10000_years_saved_outside_the_service_never_raises(client, signed_in, history, dept, model):
    """Skeptic (edges2 #1): an expected life saved outside update_device_model's 1 to 50 years (as the admin once could): the install day
    plus the life is past the calendar. The rule reads its life end as unknown; the report, its CSV, print, and API, and the device's
    Costs tab answer."""
    a = device(model, dept, "CE-1", installed_on=date(2021, 3, 1))
    DeviceModel.objects.filter(pk=model.pk).update(expected_life_years=10000)
    p = score(a)
    assert (p.life_years, p.life_end) == (10000, None) and p.component(P.AGE).input == "5.6 of 10000 yr" and p.component(P.AGE).value < 0.001
    assert next(r for r in report_replace(TODAY)["rows"] if r[0] == "CE-1")[REPLACE_COLUMNS.index("Expected life years")] == 10000
    signed_in("director")
    for url in ("/reports/replace/", "/reports/replace.csv", "/print/reports/replace/", "/api/v1/reports/replace/"):
        assert client.get(url).status_code == 200, url
    assert "5.6 of 10000 yr" in costs_tab(client, a)


def test_r16_the_device_drawer_answers_for_a_life_of_10000_years(client, signed_in, history, dept, model):
    """The same stored life on the device drawer's Overview (its Expected life line, the rule's arithmetic): a 200, never a 500."""
    a = device(model, dept, "CE-1", installed_on=date(2021, 3, 1))
    DeviceModel.objects.filter(pk=model.pk).update(expected_life_years=10000)
    signed_in("director")
    r = client.get(f"/equipment/{a.tag}/", **HX)
    assert r.status_code == 200 and "10000 yr" in r.content.decode()
