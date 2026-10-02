"""Labor and parts on a work order (slice 15, part A): apps.workorders.costs is the only writer of LaborLine and PartLine. Every rule
is tested here, with tenant isolation (another facility's work order, line, and technician), the audit trail, the timeline events,
the labor rates in Settings, and the Reports picking up a line added through the service."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.facility import services as fs
from apps.facility.models import FacilitySettings
from apps.reports.cost import report_spend
from apps.reports.services import ANNUALIZE, cost_of_service, overview_kpis
from apps.tenants.context import tenant_context
from apps.workorders import costs
from apps.workorders.models import LaborLine, PartLine, WoStatus
from apps.workorders.services import change_status, create_work_order, timeline

TODAY = date.today()
OPENED = TODAY - timedelta(days=5)


@pytest.fixture
def kim(make_user):
    return make_user("director")


@pytest.fixture
def wo(ctx, pump, techs):
    return create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch broken", opened_on=OPENED, assigned_to=techs["dana"])


@pytest.fixture
def vendor_wo(ctx, vent):
    return create_work_order(asset=vent, type="repair", priority="high", problem="Flow sensor fault", opened_on=OPENED, vendor_service=True,
                             vendor_name="Hamilton Medical field service")


@pytest.fixture
def theirs(other_tenant):
    """Another facility's work order, technician, and lines."""
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, next_pm_on=TODAY + timedelta(days=30))
        tech = Technician.objects.create(name="Their Tech")
        their_wo = create_work_order(asset=asset, type="repair", priority="normal", problem="Theirs", opened_on=OPENED, assigned_to=tech)
        labor = costs.add_labor(their_wo, hours="1", worked_on=TODAY, by=None)
        part = costs.add_part(their_wo, description="Their part", quantity="1", unit_cost="10", by=None)
    return {"wo": their_wo, "tech": tech, "labor": labor, "part": part}


def _errors(excinfo) -> dict:
    return excinfo.value.message_dict if hasattr(excinfo.value, "error_dict") else {"__all__": excinfo.value.messages}


def labor(wo, by=None, **over):
    kwargs = {"hours": "1.5", "worked_on": TODAY, **over}
    return costs.add_labor(wo, by=by, **kwargs)


def part(wo, by=None, **over):
    kwargs = {"description": "Pump door latch", "quantity": "2", "unit_cost": "42", **over}
    return costs.add_part(wo, by=by, **kwargs)


# --- labor: who, the rate, the audit -----------------------------------------------------------------------------------------------

def test_labor_defaults_to_the_assigned_technician_and_the_settings_rate(wo, techs, kim):
    line = labor(wo, by=kim, description="  Replaced   latch,\ttested ")
    assert line.technician == techs["dana"] and line.rate == Decimal("82.00") and line.hours == Decimal("1.50")
    assert line.worked_on == TODAY and line.description == "Replaced latch, tested" and line.tenant_id == wo.tenant_id
    h = line.history.get()
    assert h.history_type == "+" and h.history_user == kim and h.history_change_reason == "Added"
    assert costs.labor_amount(line) == Decimal("123.00") and wo.labor_cost() == 123.0


def test_vendor_service_is_charged_the_vendor_rate_without_a_technician(vendor_wo, techs):
    line = labor(vendor_wo, hours="2")
    assert line.technician is None and line.rate == Decimal("215.00") and vendor_wo.total_cost() == 430.0
    with pytest.raises(ValidationError) as e:
        labor(vendor_wo, technician=techs["dana"])
    assert _errors(e) == {"technician": ["Vendor service time is logged without a technician."]}


def test_another_technician_can_be_chosen(wo, techs):
    assert labor(wo, technician=techs["tom"]).technician == techs["tom"]


def test_unassigned_work_falls_back_to_the_signed_in_users_own_technician_record(ctx, pump, techs, make_user):
    unassigned = create_work_order(asset=pump, type="repair", priority="normal", problem="Alarm", opened_on=OPENED)
    user = make_user("technician")
    with pytest.raises(ValidationError) as e:
        labor(unassigned, by=user)
    assert _errors(e) == {"technician": ["Choose who did the work."]}
    techs["tom"].user = user
    techs["tom"].save()
    assert costs.default_technician(unassigned, user) == techs["tom"]
    assert labor(unassigned, by=user).technician == techs["tom"]


def test_an_inactive_assigned_technician_is_not_the_default(wo, techs, make_user):
    techs["dana"].is_active = False
    techs["dana"].save()
    user = make_user("technician")
    assert costs.default_technician(wo, user) is None
    with pytest.raises(ValidationError) as e:
        labor(wo, by=user)
    assert _errors(e) == {"technician": ["Choose who did the work."]}
    with pytest.raises(ValidationError) as e:
        labor(wo, technician=techs["dana"])
    assert _errors(e) == {"technician": ["Dana Whitfield is no longer active. Choose another technician."]}


def test_another_facilitys_technician_is_refused(wo, theirs):
    with pytest.raises(ValidationError) as e:
        labor(wo, technician=theirs["tech"])
    assert _errors(e) == {"technician": ["Choose a technician from this facility."]}
    assert not LaborLine.objects.filter(work_order=wo).exists()


# --- labor: hours, dates, the rate ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hours, message", [
    ("", "Enter the hours worked."), (None, "Enter the hours worked."), ("abc", "Enter a number."), ("NaN", "Enter a number."),
    ("Infinity", "Enter a number."), ("1,5", "Enter a number."), (True, "Enter a number."),
    ("0", "Log more than 0 and at most 24 hours on one line."), ("-1", "Log more than 0 and at most 24 hours on one line."),
    ("24.01", "Log more than 0 and at most 24 hours on one line."), ("1e999999999", "Log more than 0 and at most 24 hours on one line."),
    ("1.555", "Use at most 2 decimal places."), ("1.000000000000000000000000000001", "Use at most 2 decimal places."),
    ("1E-400", "Use at most 2 decimal places."),
])
def test_hours_are_a_number_above_0_and_at_most_24_with_2_decimals(wo, hours, message):
    with pytest.raises(ValidationError) as e:
        labor(wo, hours=hours)
    assert _errors(e) == {"hours": [message]}
    assert not LaborLine.objects.exists()


@pytest.mark.parametrize("hours, stored", [("24", Decimal("24")), ("0.01", Decimal("0.01")), (" 1.50 ", Decimal("1.5")), (Decimal("2.250"), Decimal("2.25")),
                                           (3, Decimal("3")), ("1.5e0", Decimal("1.5"))])
def test_hours_in_range_are_stored_as_given(wo, hours, stored):
    line = labor(wo, hours=hours)
    line.refresh_from_db()
    assert line.hours == stored


def test_one_technician_logs_at_most_24_hours_on_one_day(wo, vent, techs, vendor_wo):
    other = create_work_order(asset=vent, type="repair", priority="normal", problem="Other job", opened_on=OPENED, assigned_to=techs["dana"])
    labor(other, hours="20")
    with pytest.raises(ValidationError) as e:
        labor(wo, hours="4.5")
    assert _errors(e) == {"hours": [f"Dana Whitfield already has 20 h logged on {TODAY:%b %-d, %Y}; one day holds at most 24 h."]}
    labor(wo, hours="4")  # exactly 24
    labor(wo, hours="8", worked_on=TODAY - timedelta(days=1))  # another day
    labor(wo, hours="3", technician=techs["tom"])  # another technician
    labor(vendor_wo, hours="8")  # vendor time has no technician to add up
    assert LaborLine.objects.filter(technician=techs["dana"], worked_on=TODAY).count() == 2


@pytest.mark.parametrize("worked_on, message", [
    (TODAY + timedelta(days=1), "The date cannot be in the future."),
    (OPENED - timedelta(days=1), f"was opened on {OPENED:%b %-d, %Y}; time cannot be logged before that."),
    ("", "Enter the date the work was done."), (None, "Enter the date the work was done."), ("2026-02-30", "Enter the date the work was done."),
])
def test_the_date_worked_is_not_in_the_future_or_before_the_work_order_opened(wo, worked_on, message):
    with pytest.raises(ValidationError) as e:
        labor(wo, worked_on=worked_on)
    assert message in _errors(e)["worked_on"][0]


def test_the_date_worked_accepts_iso_text_and_the_opening_day(wo):
    assert labor(wo, worked_on=OPENED.isoformat()).worked_on == OPENED


def test_a_completed_work_order_takes_no_time_after_its_completion(wo, techs):
    change_status(wo, WoStatus.IN_PROGRESS, as_of=OPENED)
    change_status(wo, WoStatus.COMPLETED, as_of=TODAY - timedelta(days=2))
    with pytest.raises(ValidationError) as e:
        labor(wo, worked_on=TODAY)
    assert _errors(e)["worked_on"] == [f"{wo.number} was completed on {TODAY - timedelta(days=2):%b %-d, %Y}; time cannot be logged after that."]
    assert labor(wo, worked_on=TODAY - timedelta(days=2)).hours == Decimal("1.5")  # the paperwork may lag the work


def test_today_can_be_passed_in(wo):
    with pytest.raises(ValidationError):
        labor(wo, worked_on=TODAY, today=TODAY - timedelta(days=1))


def test_a_different_rate_is_taken_as_given_and_checked(wo):
    assert labor(wo, rate="95.50").rate == Decimal("95.50")
    assert labor(wo, rate="0", hours="1").rate == Decimal("0")  # warranty work at no charge
    assert labor(wo, rate="  ", hours="1").rate == Decimal("82.00")  # blank is the Settings rate
    for rate, message in [("95.555", "Use at most 2 decimal places."), ("-1", "The rate is between $0 and $9,999.99 an hour."),
                          ("10000", "The rate is between $0 and $9,999.99 an hour."), ("abc", "Enter a number.")]:
        with pytest.raises(ValidationError) as e:
            labor(wo, rate=rate, hours="1")
        assert _errors(e) == {"rate": [message]}


def test_a_changed_settings_rate_prices_new_lines_only(wo, vendor_wo, kim):
    first = labor(wo)
    fs.update_settings(by=kim, labor_rate="90", vendor_labor_rate="250.00")
    second = labor(wo, hours="1")
    first.refresh_from_db()
    assert first.rate == Decimal("82.00") and second.rate == Decimal("90.00") and labor(vendor_wo, hours="1").rate == Decimal("250.00")
    assert costs.default_rate(wo) == Decimal("90") and costs.default_rate(vendor_wo) == Decimal("250")


@pytest.mark.parametrize("description, message", [("x" * 121, "Keep the description to 120 characters."),
                                                  ("Latch\x00", "Remove the invisible control character from this text."),
                                                  ("Latch\x07", "Remove the invisible control character from this text.")])
def test_a_labor_description_is_one_short_line(wo, description, message):
    with pytest.raises(ValidationError) as e:
        labor(wo, description=description)
    assert _errors(e) == {"description": [message]}


def test_every_problem_is_reported_at_once(wo):
    with pytest.raises(ValidationError) as e:
        labor(wo, hours="0", worked_on=TODAY + timedelta(days=1), rate="-1", description="x" * 200)
    assert set(_errors(e)) == {"hours", "worked_on", "rate", "description"}


# --- parts -------------------------------------------------------------------------------------------------------------------

def test_a_part_line_is_added_and_audited(wo, kim):
    line = part(wo, by=kim, part_number=" 10013-B ", po_number="PO-5566")
    assert (line.description, line.part_number, line.po_number, line.quantity, line.unit_cost) == ("Pump door latch", "10013-B", "PO-5566", Decimal("2"),
                                                                                                    Decimal("42"))
    assert costs.part_amount(line) == Decimal("84.00") and wo.parts_cost() == 84.0
    h = line.history.get()
    assert h.history_type == "+" and h.history_user == kim and h.history_change_reason == "Added"


@pytest.mark.parametrize("over, field, message", [
    ({"description": "   "}, "description", "Describe the part."),
    ({"description": "x" * 121}, "description", "Keep the description to 120 characters."),
    ({"quantity": ""}, "quantity", "Enter the quantity."),
    ({"quantity": "0"}, "quantity", "Enter a quantity above 0 and at most 9,999."),
    ({"quantity": "-2"}, "quantity", "Enter a quantity above 0 and at most 9,999."),
    ({"quantity": "10000"}, "quantity", "Enter a quantity above 0 and at most 9,999."),
    ({"quantity": "1.255"}, "quantity", "Use at most 2 decimal places."),
    ({"unit_cost": ""}, "unit_cost", "Enter the unit cost (0 for a part at no charge)."),
    ({"unit_cost": "-0.01"}, "unit_cost", "The unit cost is between $0 and $999,999.99."),
    ({"unit_cost": "1000000"}, "unit_cost", "The unit cost is between $0 and $999,999.99."),
    ({"unit_cost": "42.001"}, "unit_cost", "Use at most 2 decimal places."),
    ({"unit_cost": "$42"}, "unit_cost", "Enter a number."),
    ({"part_number": "P" * 41}, "part_number", "Keep the part number to 40 characters."),
    ({"po_number": "P" * 41}, "po_number", "Keep the PO number to 40 characters."),
    ({"po_number": "PO\x00"}, "po_number", "Remove the invisible control character from this text."),
])
def test_part_rules(wo, over, field, message):
    with pytest.raises(ValidationError) as e:
        part(wo, **over)
    assert _errors(e) == {field: [message]}
    assert not PartLine.objects.exists()


def test_part_bounds_are_inclusive(wo):
    assert part(wo, quantity="9999", unit_cost="999999.99").unit_cost == Decimal("999999.99")
    assert part(wo, quantity="0.5", unit_cost="0").unit_cost == Decimal("0")  # a part at no charge (warranty)


# --- which work orders take lines --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [[], [WoStatus.IN_PROGRESS], [WoStatus.AWAITING_PARTS], [WoStatus.IN_PROGRESS, WoStatus.COMPLETED]])
def test_open_and_completed_work_orders_take_lines(wo, path):
    for to in path:
        change_status(wo, to, as_of=OPENED)
    line = labor(wo, worked_on=OPENED)
    costs.remove_labor(line, by=None)
    costs.remove_part(part(wo), by=None)


def test_a_closed_work_order_is_the_record(wo):
    line, p = labor(wo, worked_on=OPENED), part(wo)
    for to in (WoStatus.IN_PROGRESS, WoStatus.COMPLETED, WoStatus.CLOSED):
        change_status(wo, to, as_of=OPENED)
    message = f"{wo.number} is closed: its labor and parts are the record. Reopen it to change them."
    for attempt in (lambda: labor(wo, worked_on=OPENED), lambda: part(wo), lambda: costs.remove_labor(line, by=None),
                    lambda: costs.remove_part(p, by=None)):
        with pytest.raises(ValidationError) as e:
            attempt()
        assert e.value.messages == [message]
    assert LaborLine.objects.count() == 1 and PartLine.objects.count() == 1
    assert costs.locked_reason(wo) == message
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened: the lines can change again
    costs.remove_labor(line, by=None)


def test_a_cancelled_work_order_takes_no_lines(wo):
    change_status(wo, WoStatus.CANCELLED)
    with pytest.raises(ValidationError, match="was cancelled: it takes no labor or parts"):
        labor(wo)
    with pytest.raises(ValidationError, match="was cancelled"):
        part(wo)


def test_the_status_is_read_fresh_not_from_a_stale_copy(wo):
    stale = type(wo).objects.get(pk=wo.pk)
    change_status(wo, WoStatus.CANCELLED)
    with pytest.raises(ValidationError, match="was cancelled"):
        labor(stale)


# --- removing ------------------------------------------------------------------------------------------------------------------

def test_removing_lines_is_audited(wo, kim):
    line, p = labor(wo), part(wo)
    line_id, part_id = line.pk, p.pk
    costs.remove_labor(line, by=kim)
    costs.remove_part(p, by=kim)
    assert not LaborLine.objects.exists() and not PartLine.objects.exists()
    for model, pk in ((LaborLine, line_id), (PartLine, part_id)):
        gone = model.history.get(id=pk, history_type="-")
        assert gone.history_user == kim and gone.history_change_reason == "Removed"
    with pytest.raises(ValidationError, match="may have been removed already"):
        costs.remove_labor(line, by=kim)
    with pytest.raises(ValidationError, match="may have been removed already"):
        costs.remove_part(p, by=kim)


# --- tenant isolation ------------------------------------------------------------------------------------------------------------

def test_another_facilitys_work_order_and_lines_are_refused(ctx, theirs, wo):
    with pytest.raises(ValidationError, match="not in this facility"):
        labor(theirs["wo"], technician=None)
    with pytest.raises(ValidationError, match="not in this facility"):
        part(theirs["wo"])
    with pytest.raises(ValidationError, match="not on file here"):
        costs.remove_labor(theirs["labor"], by=None)
    with pytest.raises(ValidationError, match="not on file here"):
        costs.remove_part(theirs["part"], by=None)
    # unscoped: counting across tenants to show nothing of theirs moved
    assert LaborLine.unscoped.filter(tenant=theirs["wo"].tenant).count() == 1 and PartLine.unscoped.filter(tenant=theirs["wo"].tenant).count() == 1


def test_lines_are_tenant_scoped(ctx, theirs, wo):
    labor(wo)
    part(wo)
    assert LaborLine.objects.count() == 1 and PartLine.objects.count() == 1
    assert theirs["labor"].pk not in set(LaborLine.objects.values_list("pk", flat=True))
    with tenant_context(theirs["wo"].tenant):
        assert list(LaborLine.objects.values_list("pk", flat=True)) == [theirs["labor"].pk]
        assert list(PartLine.objects.values_list("pk", flat=True)) == [theirs["part"].pk]


def test_without_a_tenant_nothing_is_written(tenant, wo):
    with tenant_context(None):
        with pytest.raises((ValidationError, RuntimeError)):
            costs.add_part(wo, description="Latch", quantity="1", unit_cost="1", by=None)
    assert not PartLine.unscoped.exists()  # unscoped: proving no row was written anywhere


# --- the timeline --------------------------------------------------------------------------------------------------------------

def test_the_timeline_tells_labor_and_parts_in_time_order(wo, vendor_wo, kim):
    labor(wo, by=kim)
    part(wo, by=kim)
    p2 = part(wo, by=kim, description="Hinge pin", quantity="1", unit_cost="7.5")
    costs.remove_part(p2, by=kim)
    gone = labor(wo, by=kim, hours="0.25", worked_on=OPENED)
    costs.remove_labor(gone, by=kim)
    texts = [e["text"] for e in timeline(wo)]
    assert texts[0].startswith("Opened: Door latch broken")
    assert texts[1:] == ["1.5 h logged by Dana Whitfield", "Part: Pump door latch × 2 ($84.00)", "Part: Hinge pin ($7.50)", "Part removed: Hinge pin ($7.50)",
                         "0.25 h logged by Dana Whitfield", f"Labor removed: 0.25 h logged by Dana Whitfield, worked {OPENED:%b %-d, %Y}"]
    assert {e["who"] for e in timeline(wo)[1:]} == {"Director User"}
    labor(vendor_wo, hours="2")
    last = timeline(vendor_wo)[-1]
    assert (last["who"], last["text"]) == ("System", "2 h vendor time logged")


def test_the_timeline_mixes_lines_with_status_changes_and_notes(wo, kim):
    change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    labor(wo, by=kim)
    change_status(wo, WoStatus.AWAITING_PARTS, by=kim)
    texts = [e["text"] for e in timeline(wo)]
    assert texts[1:] == ["Status changed to in progress", "1.5 h logged by Dana Whitfield", "Status changed to awaiting parts"]


def test_a_line_from_before_lines_were_audited_still_shows(wo, techs):
    line = LaborLine.objects.create(work_order=wo, technician=techs["tom"], worked_on=TODAY, hours=Decimal("2"), rate=Decimal("82"))
    old = PartLine.objects.create(work_order=wo, description="Replacement part", quantity=1, unit_cost=Decimal("40"))
    line.history.all().delete()
    old.history.all().delete()
    entries = timeline(wo)
    assert [e["text"] for e in entries[1:]] == ["2 h logged by Tom Okafor", "Part: Replacement part ($40.00)"]
    assert [e["who"] for e in entries[1:]] == ["Tom Okafor", "System"]


def test_another_facilitys_history_never_reaches_the_timeline(wo, theirs):
    assert [e["text"] for e in timeline(wo)][1:] == []


# --- display helpers -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, text", [(Decimal("1.50"), "1.5"), (Decimal("2.00"), "2"), (Decimal("0.25"), "0.25"), (Decimal("10"), "10"),
                                         (Decimal("100.00"), "100")])
def test_plain_numbers(value, text):
    assert costs.plain(value) == text


def test_event_texts():
    assert costs.labor_event(Decimal("1.50"), "Dana Whitfield") == "1.5 h logged by Dana Whitfield"
    assert costs.labor_event(Decimal("2"), "") == "2 h vendor time logged"
    assert costs.part_event("Pump door latch", Decimal("2.00"), Decimal("42.00")) == "Pump door latch × 2 ($84.00)"
    assert costs.part_event("Fuse", Decimal("1"), Decimal("1234.5")) == "Fuse ($1,234.50)"
    assert costs.money_text(Decimal("0.125")) == "$0.13"


# --- the Reports pick up lines added through the service -----------------------------------------------------------------------

def test_repair_spend_and_cost_of_service_pick_up_a_line_added_through_the_service(wo, vendor_wo, kim):
    change_status(wo, WoStatus.IN_PROGRESS, as_of=OPENED)
    labor(wo, by=kim, worked_on=OPENED)  # 1.5 h at $82 = $123
    change_status(wo, WoStatus.COMPLETED)
    part(wo, by=kim)  # added once completed (the paperwork lags): $84
    change_status(vendor_wo, WoStatus.IN_PROGRESS, as_of=OPENED)
    change_status(vendor_wo, WoStatus.COMPLETED)
    labor(vendor_wo, hours="2", by=kim)  # $430 vendor time and materials

    spend = report_spend(TODAY)
    assert spend["months"][-1]["labor"] == 123.0 + 430.0 and spend["months"][-1]["parts"] == 84.0
    cos = cost_of_service(TODAY)
    assert cos["in_house"] == pytest.approx((123 + 84) * ANNUALIZE) and cos["vendor_tm"] == pytest.approx(430 * ANNUALIZE)
    assert overview_kpis(TODAY.year, TODAY.month, TODAY)["repair_spend"] == pytest.approx(123 + 84 + 430)


# --- the labor rates in Settings ---------------------------------------------------------------------------------------------------

def test_the_rates_default_to_the_mocks(ctx):
    assert fs.labor_rates() == {"in_house": Decimal("82.00"), "vendor": Decimal("215.00")}


def test_saving_the_rates_is_audited(ctx, kim):
    s = fs.update_settings(by=kim, labor_rate="90.50", vendor_labor_rate="0")
    s.refresh_from_db()
    assert s.labor_rate == Decimal("90.50") and s.vendor_labor_rate == Decimal("0") and fs.labor_rates()["in_house"] == Decimal("90.5")
    h = s.history.first()
    assert h.history_user == kim and h.labor_rate == Decimal("90.50")
    assert fs.update_settings(labor_rate="9999.99").labor_rate == Decimal("9999.99")


@pytest.mark.parametrize("fields, message", [
    ({"labor_rate": ""}, "In-house labor rate is required."),
    ({"vendor_labor_rate": None}, "Vendor labor rate is required."),
    ({"labor_rate": "-1"}, "In-house labor rate must be between $0 and $9,999.99 an hour."),
    ({"vendor_labor_rate": "10000"}, "Vendor labor rate must be between $0 and $9,999.99 an hour."),
    ({"labor_rate": "82.555"}, "Use at most 2 decimal places."),
    ({"labor_rate": "82.000000000000000000000000000001"}, "Use at most 2 decimal places."),  # never rounded to 82
    ({"labor_rate": "1e9999999"}, "In-house labor rate must be between $0 and $9,999.99 an hour."),  # no overflow, no 500
    ({"labor_rate": "abc"}, "Enter a number."),
    ({"target_mttr_days": "1e9999999"}, "Mean time to repair target must be between 0.5 and 30."),
    ({"repair_budget_monthly": "52000.0000000000000000000000000001"}, "Use at most 2 decimal places."),
])
def test_rates_are_refused_not_rounded(ctx, fields, message):
    with pytest.raises(ValidationError) as e:
        fs.update_settings(**fields)
    assert list(e.value.message_dict.values())[0] == [message] and FacilitySettings.objects.count() == 0


def test_the_rates_are_per_facility(ctx, other_tenant):
    fs.update_settings(labor_rate="95")
    with tenant_context(other_tenant):
        assert fs.labor_rates()["in_house"] == Decimal("82.00")
