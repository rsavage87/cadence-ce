"""
Slice 29, wave 1a: the temporary-equipment services (apps/equipment/services.py: add_temporary_device, update_temporary,
return_to_owner, keep_temporary_device, matching_returned) and the guards that keep a rental, vendor loaner, or demo unit off CE's
program elsewhere in equipment (set_status, status_actions, update_asset, pass_incoming_inspection), contracts (add_asset, add_model,
the pickers, the summary), and the devices and contracts importers. The retire body set_status shares with return_to_owner (_retire)
is checked unchanged for a device of ours here and by the existing set_status tests.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.contracts import services as ct
from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import (
    AddedAs,
    Asset,
    AssetStatus,
    Department,
    DeviceModel,
    Ownership,
    ReturnCleaning,
    ReturnData,
    SupportType,
    UseBeforeInspection,
)
from apps.imports.kinds import contracts as contracts_kind
from apps.imports.kinds import devices as devices_kind
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.workorders import inspections
from apps.workorders.completion import checklist_of, complete_work_order, procedure_for
from apps.workorders.models import InspectionResult, Priority, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

S = AssetStatus


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def rental(model, dept, tag="T-0001", *, today, **kw):
    """A rental added new, waiting for its incoming inspection (the default intake)."""
    values = {"tag": tag, "device_model": model, "department": dept, "kind": Ownership.RENTAL, "owner": "Acme Rentals", "serial": "SN-1",
              "owner_reference": "RA-2026-0042", "due_back_on": today + timedelta(days=14), "owner_pm_due_on": today + timedelta(days=60),
              "today": today}
    return eq.add_temporary_device(**{**values, **kw})


def on_site(model, dept, tag="T-0002", *, today, **kw):
    """A temporary device already on site when entered (EXISTING): in service, no inspection."""
    return rental(model, dept, tag, today=today, added_as=AddedAs.EXISTING, arrived_on=today - timedelta(days=10), **kw)


def passed(asset, today):
    """Complete the device's open incoming inspection as passed, by a technician, with every step of the checklist it is done to
    passed (whichever checklist that is: the incoming one, or slice 29's for a temporary device)."""
    wo = inspections.open_inspection(asset)
    assign(wo, technician=Technician.objects.get_or_create(name="Dana Whitfield", defaults={"title": "Lead BMET"})[0])
    results = [{"result": "pass", "reading": "42" if measure else ""} for _text, measure in checklist_of(procedure_for(wo))]
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=results, today=today)
    asset.refresh_from_db()
    return asset


def history(asset):
    return list(Asset.history.filter(id=asset.id).order_by("history_date", "history_id"))


# --- add_temporary_device -----------------------------------------------------------------------------------------------------------

def test_a_new_rental_waits_for_its_incoming_inspection_with_no_pm_and_no_cost(ctx, today, pump_model, dept):
    a = rental(pump_model, dept, today=today)
    assert (a.ownership, a.owner, a.owner_reference, a.serial) == (Ownership.RENTAL, "Acme Rentals", "RA-2026-0042", "SN-1")
    assert (a.arrived_on, a.installed_on, a.due_back_on, a.owner_pm_due_on) == (today, today, today + timedelta(days=14), today + timedelta(days=60))
    assert (a.status, a.awaiting_inspection, a.added_as, a.next_pm_on, a.last_pm_on) == (S.OUT_OF_SERVICE, True, AddedAs.NEW, None, None)
    assert a.acquisition_cost == Decimal("0") and a.support_type == SupportType.OWNER and a.stands_in_for is None
    wo = inspections.open_inspection(a)
    assert wo is not None and wo.type == WoType.INSPECTION and wo.opened_on == today
    assert history(a)[-1].history_change_reason == "Added, waiting for its incoming inspection"
    assert eq.status_label(a) == eq.AWAITING_LABEL


def test_inspect_it_now_also_waits_the_screen_opens_the_inspection(ctx, today, pump_model, dept):
    # "" is Add device's "inspect it now" (slice 26): the device still waits, its inspection open for the screen to assign and record.
    a = rental(pump_model, dept, today=today, incoming_inspection="", inspection_due=today + timedelta(days=2))
    assert a.awaiting_inspection and a.status == S.OUT_OF_SERVICE
    assert inspections.open_inspection(a).due_on == today + timedelta(days=2)


def test_one_already_on_site_is_in_service_with_no_inspection(ctx, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today)
    assert (a.status, a.awaiting_inspection, a.added_as, a.arrived_on, a.installed_on) == (S.IN_SERVICE, False, AddedAs.EXISTING,
                                                                                            today - timedelta(days=10), today - timedelta(days=10))
    assert inspections.open_inspection(a) is None and a.next_pm_on is None
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, "T-9", today=today, added_as=AddedAs.EXISTING)
    assert e.value.message_dict == {"arrived_on": ["Enter the day it arrived."]}


def test_a_vendor_loaner_stands_in_for_a_device_of_ours(ctx, today, pump_model, dept, pump):
    a = rental(pump_model, dept, today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    assert a.ownership == Ownership.LOANER and a.stands_in_for == pump and list(pump.loaners.all()) == [a]
    assert eq.service_vendor(a) == "BD"


def test_every_refusal_of_the_stay_at_once_keyed_by_field(ctx, today, pump_model, dept):
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, today=today, owner="  ", serial="", owner_reference="RA 2026 42", arrived_on=today + timedelta(days=1),
               owner_pm_due_on=date(1900, 1, 1))
    assert e.value.message_dict == {
        "owner": ["Enter the company that owns it."], "serial": ["Enter the serial number on the unit."],
        "owner_reference": ["Enter the agreement, PO, or RMA number as the owner's paperwork shows it, without spaces."],
        "arrived_on": ["It cannot have arrived after today."],
        "owner_pm_due_on": ["Enter the owner's PM date in full (the year looks wrong)."]}
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, today=today, arrived_on=today - timedelta(days=3), due_back_on=today - timedelta(days=4),
               owner_reference="X" * 41, owner_pm_due_on=today + timedelta(days=366 * 11))
    assert e.value.message_dict == {
        "due_back_on": [f"It cannot be due back before it arrived ({_day(today - timedelta(days=3))})."],
        "owner_reference": ["Keep the agreement, PO, or RMA number to 40 characters."],
        "owner_pm_due_on": ["The owner's PM date must be within 10 years."]}
    assert not Asset.objects.exists()


@pytest.mark.parametrize("kw, key", [
    ({"kind": Ownership.OWNED}, "kind"), ({"kind": "lease"}, "kind"), ({"added_as": AddedAs.IMPORTED}, "added_as"),
    ({"incoming_inspection": "later"}, "incoming_inspection"), ({"inspection_due": date(2000, 1, 1)}, "inspection_due"),
    ({"tag": "T 1"}, "tag"), ({"arrived_on": date(1999, 12, 31)}, "arrived_on"),
])
def test_the_other_refusals(ctx, today, pump_model, dept, kw, key):
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, today=today, **kw)
    assert set(e.value.message_dict) == {key}


def test_a_past_owners_pm_date_is_recorded(ctx, today, pump_model, dept):
    a = rental(pump_model, dept, today=today, owner_pm_due_on=today - timedelta(days=5))
    assert a.owner_pm_due_on == today - timedelta(days=5)  # the incoming inspection then waits for the owner's PM (completion)


def test_what_a_loaner_stands_in_for(ctx, tenant, other_tenant, today, pump_model, vent_model, dept, pump, vent):
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, today=today, stands_in_for=pump)  # a rental stands in for nothing
    assert e.value.message_dict == {"stands_in_for": ["Only a vendor loaner stands in for a device of ours."]}
    other = on_site(pump_model, dept, "T-OTHER", today=today, kind=Ownership.DEMO)
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, today=today, kind=Ownership.LOANER, stands_in_for=other)
    assert e.value.message_dict == {"stands_in_for": ["T-OTHER is not ours: a loaner stands in for a device of ours."]}
    eq.set_status(vent, S.RETIRED, today=today)
    with pytest.raises(ValidationError) as e:
        rental(vent_model, dept, today=today, kind=Ownership.LOANER, stands_in_for=vent)
    assert e.value.message_dict == {"stands_in_for": [f"{vent.tag} is retired: a loaner stands in for a device of ours still in use."]}
    with tenant_context(other_tenant):
        theirs = Asset.objects.create(tag="X-1", device_model=DeviceModel.objects.create(manufacturer="BD", model="X", description="Pump",
                                                                                         category="Pumps"),
                                      department=Department.objects.create(name="ER"))
    with pytest.raises(ValidationError) as e:
        rental(pump_model, dept, today=today, kind=Ownership.LOANER, stands_in_for=theirs)
    assert e.value.message_dict == {"stands_in_for": ["Choose the device of ours it stands in for, from this facility."]}
    with pytest.raises(ValidationError) as e:  # the kind's own refusal says enough
        rental(pump_model, dept, today=today, kind="lease", stands_in_for=pump)
    assert set(e.value.message_dict) == {"kind"}


def test_adding_needs_equipment_edit(ctx, today, pump_model, dept, make_user):
    for slug in ("analyst", "requester", "vendor"):
        with pytest.raises(PermissionDenied):
            rental(pump_model, dept, today=today, by=make_user(slug))
    assert rental(pump_model, dept, today=today, by=make_user("technician")).temporary
    assert eq_perms.TEMPORARY_LEVEL == eq_perms.EDIT_LEVEL and eq_perms.KEEP_LEVEL == eq_perms.RETIRE_LEVEL


# --- matching_returned ------------------------------------------------------------------------------------------------------------

def test_a_unit_here_before_is_found_by_model_and_serial(ctx, today, pump_model, vent_model, dept, pump):
    first = on_site(pump_model, dept, "T-1", today=today, serial="SN-77")
    eq.return_to_owner(first, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED, today=today)
    still_here = on_site(pump_model, dept, "T-2", today=today, serial="SN-77")
    on_site(vent_model, dept, "T-3", today=today, serial="SN-77")
    assert still_here.temporary
    assert list(eq.matching_returned(pump_model, " sn-77 ")) == [first]  # returned only: never one still on site, never another model
    assert not eq.matching_returned(pump_model, "").exists() and not eq.matching_returned(pump_model, "SN-78").exists()
    Asset.objects.filter(pk=pump.pk).update(serial="SN-77", status=S.RETIRED)  # a retired device of ours is no returned unit
    assert list(eq.matching_returned(pump_model, "SN-77")) == [first]


# --- update_temporary ---------------------------------------------------------------------------------------------------------------

def test_the_stay_changes_in_one_history_row(ctx, today, pump_model, dept, pump, make_user):
    tech = make_user("technician")
    a = rental(pump_model, dept, today=today, kind=Ownership.LOANER, owner="BD")
    a = eq.update_temporary(a, owner="  BD   Field Service ", owner_reference="RMA-55", due_back_on=today + timedelta(days=30),
                            owner_pm_due_on=None, stands_in_for=pump, by=tech, today=today)
    assert (a.owner, a.owner_reference, a.due_back_on, a.owner_pm_due_on, a.stands_in_for) == ("BD Field Service", "RMA-55",
                                                                                               today + timedelta(days=30), None, pump)
    last = history(a)[-1]
    assert last.history_change_reason == eq.STAY_REASON and last.history_user == tech
    rows = len(history(a))
    eq.update_temporary(a, owner="BD Field Service", stands_in_for=pump, today=today)  # nothing changed, nothing written
    assert len(history(a)) == rows
    eq.update_temporary(a, stands_in_for=None, due_back_on=None, today=today)
    a.refresh_from_db()
    assert a.stands_in_for is None and a.due_back_on is None


def test_a_stand_in_sent_back_is_no_change_though_the_device_was_retired_since(ctx, today, pump_model, dept, pump, vent):
    a = rental(pump_model, dept, today=today, kind=Ownership.LOANER, stands_in_for=pump)
    eq.set_status(pump, S.RETIRED, today=today)
    eq.update_temporary(a, stands_in_for=pump, due_back_on=today + timedelta(days=3), today=today)
    with pytest.raises(ValidationError) as e:
        eq.update_temporary(a, stands_in_for=a, today=today)
    assert e.value.message_dict == {"stands_in_for": ["A loaner cannot stand in for itself."]}
    eq.update_temporary(a, stands_in_for=vent, today=today)
    a.refresh_from_db()
    assert a.stands_in_for == vent


def test_the_stays_refusals(ctx, today, pump_model, dept, pump, make_user):
    a = rental(pump_model, dept, today=today)
    with pytest.raises(ValidationError) as e:
        eq.update_temporary(a, arrived_on=today, ownership=Ownership.OWNED, today=today)
    assert e.value.messages == ["These cannot be changed here: arrived_on, ownership."]
    with pytest.raises(ValidationError) as e:
        eq.update_temporary(a, owner="", due_back_on=today - timedelta(days=1), stands_in_for=pump, today=today)
    assert e.value.message_dict == {"owner": ["Enter the company that owns it."],
                                    "due_back_on": [f"It cannot be due back before it arrived ({_day(today)})."],
                                    "stands_in_for": ["Only a vendor loaner stands in for a device of ours."]}
    with pytest.raises(PermissionDenied):
        eq.update_temporary(a, owner="Other", by=make_user("analyst"), today=today)
    with pytest.raises(ValidationError) as e:
        eq.update_temporary(pump, owner="Other", today=today)
    assert e.value.messages == [f"{pump.tag} is ours, not a rental, vendor loaner, or demo unit."]
    b = on_site(pump_model, dept, today=today)
    eq.return_to_owner(b, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    with pytest.raises(ValidationError) as e:
        eq.update_temporary(b, owner="Other", today=today)
    assert e.value.messages == [f"T-0002 was returned to its owner on {_day(today)}."]
    c = on_site(pump_model, dept, "T-0003", today=today)
    eq.keep_temporary_device(c, acquisition_cost=Decimal("900"), today=today)
    with pytest.raises(ValidationError) as e:
        eq.update_temporary(c, owner="Other", today=today)
    assert e.value.messages == [f"T-0003 was kept by the facility on {_day(today)}: it is ours now."]


# --- return_to_owner ----------------------------------------------------------------------------------------------------------------

def test_a_device_still_waiting_goes_back_with_its_inspection_cancelled(ctx, today, pump_model, dept, make_user):
    tech = make_user("technician")
    a = rental(pump_model, dept, today=today)
    wo = inspections.open_inspection(a)
    a = eq.return_to_owner(a, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NOT_APPLICABLE, by=tech, today=today)
    assert (a.status, a.returned_on, a.return_cleaning, a.return_data, a.next_pm_on) == (S.RETIRED, today, ReturnCleaning.DECONTAMINATED,
                                                                                         ReturnData.NOT_APPLICABLE, None)
    assert a.temporary and eq.status_label(a) == eq.RETURNED_LABEL == "Returned to owner"
    assert dict(eq.with_bucket(Asset.objects.all(), today).values_list("tag", "bucket")) == {a.tag: eq.FleetBucket.RETURNED}
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED and wo.status_history.get(to_status=WoStatus.CANCELLED).note == "Device returned to its owner"
    last = history(a)[-1]
    assert (last.history_change_reason, last.history_user, last.status) == (eq.RETURNED_REASON, tech, S.RETIRED)
    assert eq.status_actions(a, make_user("director")) == []


def test_one_in_service_goes_back_on_an_earlier_day(ctx, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today)
    a = eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, on=today - timedelta(days=2), today=today)
    assert (a.status, a.returned_on) == (S.RETIRED, today - timedelta(days=2))


def test_open_work_keeps_it_here_named_in_words(ctx, today, pump_model, dept, techs):
    a = on_site(pump_model, dept, today=today)
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm sounds", assigned_to=techs["tom"])
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    assert e.value.messages == [f"T-0002 has open work: {repair.number}. Complete it, or cancel it (an in-progress work order goes back to "
                                "open first), before returning it to its owner."]
    a.refresh_from_db()
    assert a.status == S.IN_SERVICE and a.returned_on is None
    change_status(repair, WoStatus.CANCELLED)
    assert eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today).status == S.RETIRED


def test_an_inspection_in_progress_keeps_it_here(ctx, today, pump_model, dept, techs):
    a = rental(pump_model, dept, today=today)
    wo = inspections.open_inspection(a)
    wo.assigned_to = techs["tom"]
    wo.save()
    change_status(wo, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    assert wo.number in e.value.messages[0] and "before returning it to its owner" in e.value.messages[0]


def test_the_returns_refusals(ctx, today, pump_model, dept, pump, make_user):
    a = on_site(pump_model, dept, today=today)
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning="wiped", data="", on=today + timedelta(days=1), today=today)
    assert e.value.message_dict == {"cleaning": ["Say how it was cleaned before it left."], "data": ["Say what was done about patient data on it."],
                                    "on": ["It cannot go back on a day after today."]}
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, on=a.arrived_on - timedelta(days=1), today=today)
    assert e.value.message_dict == {"on": [f"It cannot go back before it arrived ({_day(a.arrived_on)})."]}
    with pytest.raises(PermissionDenied):
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, by=make_user("analyst"), today=today)
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(pump, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    assert e.value.messages == [f"{pump.tag} is ours, not a rental, vendor loaner, or demo unit."]
    eq.set_status(a, S.MISSING, today=today)
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    # review fix: a missing unit leaves only as not in hand (lost, settled with its owner)
    assert e.value.message_dict == {"cleaning": ["T-0002 is missing: mark it found before it goes back to its owner, or return it as not in hand "
                                                 "(lost, settled with its owner)."]}
    eq.set_status(a, S.IN_SERVICE, today=today)
    eq.set_incident_hold(a)
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    assert e.value.messages == [eq.held_message(a)]
    eq.clear_incident_hold(a, to_status=S.IN_SERVICE)
    eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, by=make_user("technician"), today=today)
    with pytest.raises(ValidationError) as e:
        eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    assert e.value.messages == [f"T-0002 was returned to its owner on {_day(today)}."]


# --- keep_temporary_device ----------------------------------------------------------------------------------------------------------

def test_keeping_one_makes_it_ours_with_its_acceptance_pm_today(ctx, today, pump_model, dept, pump, make_user):
    director = make_user("director")
    a = passed(rental(pump_model, dept, today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump), today)
    assert (a.awaiting_inspection, a.status, a.next_pm_on) == (False, S.IN_SERVICE, None)
    a = eq.keep_temporary_device(a, acquisition_cost="2750.00", warranty_end=today + timedelta(days=365), by=director, today=today)
    assert (a.ownership, a.kept_on, a.acquisition_cost, a.next_pm_on, a.warranty_end) == (Ownership.OWNED, today, Decimal("2750.00"), today,
                                                                                          today + timedelta(days=365))
    assert not a.temporary and a.support_type == SupportType.IN_HOUSE and a.stands_in_for is None
    assert (a.owner, a.arrived_on, a.owner_reference) == ("BD", today, "RA-2026-0042")  # the stay stays, as history
    last = history(a)[-1]
    assert (last.history_change_reason, last.history_user, last.ownership) == (eq.KEPT_REASON, director, Ownership.OWNED)
    assert eq.status_label(a) == "In service" and eq.service_vendor(a) == "BD field service"
    # On CE's program now: its acceptance PM is due today.
    assert dict(eq.with_bucket(Asset.objects.filter(pk=a.pk), today).values_list("tag", "bucket")) == {a.tag: eq.FleetBucket.PM_DUE}
    # Ours now: the guards no longer apply.
    eq.update_asset(a, next_pm_on=today + timedelta(days=30), acquisition_cost=Decimal("2800"), today=today)
    eq.set_status(a, S.ON_LOAN, today=today)
    eq.set_status(a, S.IN_SERVICE, today=today)
    assert eq.set_status(a, S.RETIRED, by=director, today=today).status == S.RETIRED
    assert eq.status_label(a) == "Retired"


def test_keeping_one_already_on_site_with_its_own_next_pm(ctx, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today, kind=Ownership.DEMO)
    a = eq.keep_temporary_device(a, acquisition_cost=0, next_pm_on=today + timedelta(days=7), today=today)
    assert (a.ownership, a.acquisition_cost, a.next_pm_on) == (Ownership.OWNED, Decimal("0"), today + timedelta(days=7))


def test_keeping_needs_equipment_approve(ctx, today, pump_model, dept, make_user):
    a = on_site(pump_model, dept, today=today)
    for slug in ("manager", "technician", "analyst"):
        with pytest.raises(PermissionDenied):
            eq.keep_temporary_device(a, acquisition_cost=1, by=make_user(slug), today=today)
    assert eq.keep_temporary_device(a, acquisition_cost=1, by=make_user("director"), today=today).ownership == Ownership.OWNED


def test_the_keeps_refusals(ctx, today, pump_model, dept, pump):
    a = rental(pump_model, dept, today=today)
    number = inspections.open_inspection(a).number
    with pytest.raises(ValidationError) as e:
        eq.keep_temporary_device(a, acquisition_cost=100, today=today)
    assert e.value.messages == [f"T-0001 has not passed its incoming inspection ({number}): keep it once it passes."]
    b = on_site(pump_model, dept, today=today)
    for cost, message in ((None, "Enter what the facility paid for it (0 if nothing)."), ("", "Enter what the facility paid for it (0 if nothing)."),
                          ("-1", "The acquisition cost is a number, 0 or more."), ("lots", "The acquisition cost is a number, 0 or more.")):
        with pytest.raises(ValidationError) as e:
            eq.keep_temporary_device(b, acquisition_cost=cost, today=today)
        assert e.value.message_dict == {"acquisition_cost": [message]}
    with pytest.raises(ValidationError) as e:
        eq.keep_temporary_device(b, acquisition_cost=1, next_pm_on=today - timedelta(days=1), today=today)
    assert e.value.message_dict == {"next_pm_on": ["Its first PM as ours cannot be before today."]}
    with pytest.raises(ValidationError) as e:
        eq.keep_temporary_device(b, acquisition_cost=1, warranty_end=b.arrived_on - timedelta(days=1), today=today)
    assert e.value.message_dict == {"warranty_end": ["The warranty cannot end before the device was installed."]}
    eq.set_status(b, S.MISSING, today=today)
    with pytest.raises(ValidationError) as e:
        eq.keep_temporary_device(b, acquisition_cost=1, today=today)
    assert e.value.messages == ["T-0002 is missing: mark it found before keeping it."]
    eq.set_status(b, S.OUT_OF_SERVICE, today=today)
    eq.set_incident_hold(b)
    with pytest.raises(ValidationError) as e:
        eq.keep_temporary_device(b, acquisition_cost=1, today=today)
    assert e.value.messages == [eq.held_message(b)]
    with pytest.raises(ValidationError) as e:
        eq.keep_temporary_device(pump, acquisition_cost=1, today=today)
    assert e.value.messages == [f"{pump.tag} is ours, not a rental, vendor loaner, or demo unit."]
    b.refresh_from_db()
    assert b.temporary and b.kept_on is None


# --- the guards: set_status, status_actions, update_asset, the pass ------------------------------------------------------------------

@pytest.mark.parametrize("to, words", [
    (S.RETIRED, "T-0002 is a rental: it leaves by Return to owner, never by retiring."),
    (S.ON_LOAN, "T-0002 is a rental, not ours to lend out. When it leaves, use Return to owner."),
])
def test_a_temporary_device_is_never_retired_or_lent_out(ctx, today, pump_model, dept, make_user, to, words):
    a = on_site(pump_model, dept, today=today)
    with pytest.raises(ValidationError) as e:
        eq.set_status(a, to, by=make_user("director"), today=today)
    assert e.value.messages == [words]
    a.refresh_from_db()
    assert a.status == S.IN_SERVICE


@pytest.mark.parametrize("to", [S.IN_SERVICE, S.OUT_OF_SERVICE])
def test_a_returned_device_is_never_reinstated(ctx, today, pump_model, dept, to):
    a = eq.return_to_owner(on_site(pump_model, dept, today=today), cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    with pytest.raises(ValidationError) as e:
        eq.set_status(a, to, today=today)
    assert e.value.messages == ["T-0002 was returned to its owner. If the unit is back, add it again with Add rental or loaner."]


def test_a_returned_device_is_never_called_retired(ctx, today, pump_model, dept, make_user):
    a = eq.return_to_owner(rental(pump_model, dept, today=today), cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    assert a.awaiting_inspection  # gone back before its inspection: only a pass clears the flag (set_status's retire keeps it too)
    with pytest.raises(ValidationError) as e:
        eq.set_incident_hold(a)
    assert e.value.messages == ["T-0001 was returned to its owner: record the incident without holding it."]
    with pytest.raises(ValidationError) as e:
        eq.use_before_inspection(a, UseBeforeInspection.LOANER_RENTAL, by=make_user("director"), today=today)
    assert e.value.messages == ["T-0001 was returned to its owner."]


def test_the_everyday_moves_still_work(ctx, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today, kind=Ownership.DEMO)
    for to in (S.OUT_OF_SERVICE, S.IN_SERVICE, S.MISSING, S.IN_SERVICE):
        assert eq.set_status(a, to, today=today).status == to
    assert a.next_pm_on is None  # back in service: still no PM of ours


def test_the_drawer_offers_no_retire_and_no_lend(ctx, today, pump_model, dept, make_user):
    director = make_user("director")
    a = on_site(pump_model, dept, today=today)
    actions = eq.status_actions(a, director)
    assert [x["label"] for x in actions] == ["Tag out of service", "Mark missing"]
    assert actions[1]["confirm"] == "Mark T-0002 missing? It stays on the inventory until it is found and returned to its owner."
    w = rental(pump_model, dept, "T-W", today=today)
    assert [x["label"] for x in eq.status_actions(w, director)] == ["Mark missing"]  # waiting: never in service by hand, never retired


def test_update_asset_keeps_a_temporary_device_off_the_pm_program(ctx, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today)
    # Edit details sends every editable field, the blank next PM and the cost of 0 among them: the room saves.
    eq.update_asset(a, room="4B", next_pm_on=None, acquisition_cost=Decimal("0.00"), notes="", today=today)
    a.refresh_from_db()
    assert (a.room, a.next_pm_on, a.acquisition_cost) == ("4B", None, Decimal("0"))
    message = "Its owner maintains it: Cadence records the owner's PM date from its sticker, never a PM date of ours."
    assert eq.pm_clock_message(a) == message
    with pytest.raises(ValidationError) as e:
        eq.update_asset(a, next_pm_on=today + timedelta(days=30), today=today)
    assert e.value.message_dict == {"next_pm_on": [message]}
    with pytest.raises(ValidationError) as e:
        eq.update_asset(a, acquisition_cost=Decimal("1200"), today=today)
    assert e.value.message_dict == {"acquisition_cost": ["T-0002 is a rental, not ours: its acquisition cost stays 0. If the facility buys "
                                                         "it, Keep it records the price."]}
    with pytest.raises(ValidationError) as e:
        eq.update_asset(a, imported_last_pm=today - timedelta(days=3), today=today)
    assert e.value.message_dict == {"last_pm_on": [message]}
    a.refresh_from_db()
    assert (a.next_pm_on, a.last_pm_on, a.acquisition_cost) == (None, None, Decimal("0"))


def test_a_temporary_devices_pass_starts_no_pm_clock(ctx, today, pump_model, dept, vent_model):
    a = passed(rental(pump_model, dept, today=today), today)
    assert (a.awaiting_inspection, a.status, a.next_pm_on, a.last_pm_on) == (False, S.IN_SERVICE, None, None)
    assert history(a)[-1].history_change_reason.startswith("Passed incoming inspection ")
    ours = passed(eq.create_asset(tag="CE-NEW", device_model=vent_model, department=dept, incoming_inspection=eq.INCOMING_WAITING,
                                  today=today), today)
    assert ours.next_pm_on == add_months(today, vent_model.pm_interval_months)  # ours as before


def test_the_buckets_of_temporary_devices_through_the_services(ctx, today, pump_model, dept, pump):
    on_site(pump_model, dept, today=today)  # in service: owner maintains it, never PM due or overdue
    rental(pump_model, dept, "T-W", today=today)  # waiting: out of service
    returned = on_site(pump_model, dept, "T-R", today=today)
    eq.return_to_owner(returned, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    counts = eq.fleet_bucket_counts(today)
    assert (counts[eq.FleetBucket.TEMPORARY], counts[eq.FleetBucket.OUT_OF_SERVICE], counts[eq.FleetBucket.RETURNED],
            counts[eq.FleetBucket.RETIRED]) == (1, 1, 1, 0)


# --- the retire body, unchanged for a device of ours ---------------------------------------------------------------------------------

def test_retiring_a_device_of_ours_still_cancels_its_pm_and_refuses_other_work(ctx, today, vent, techs, make_user):
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", due_on=today)
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    with pytest.raises(ValidationError) as e:
        eq.set_status(vent, S.RETIRED, today=today)
    assert e.value.messages == [f"{vent.tag} has open work: {repair.number}. Complete it, or cancel it (an in-progress work order goes "
                                "back to open first), before retiring the device."]
    change_status(repair, WoStatus.CANCELLED)
    vent = eq.set_status(vent, S.RETIRED, note="End of life", today=today)
    pm.refresh_from_db()
    assert (vent.status, vent.next_pm_on, pm.status) == (S.RETIRED, None, WoStatus.CANCELLED)
    assert pm.status_history.get(to_status=WoStatus.CANCELLED).note == "Device retired"
    assert history(vent)[-1].history_change_reason == "End of life" and eq.status_label(vent) == "Retired"
    vent = eq.set_status(vent, S.IN_SERVICE, by=make_user("director"), today=today)
    assert vent.next_pm_on == today


# --- contracts --------------------------------------------------------------------------------------------------------------------

def _contract(ref="SC-1"):
    return Contract.objects.create(reference=ref, vendor="BioServ", start_on=date(2026, 1, 1), end_on=date(2099, 1, 1), annual_cost=1000)


def test_a_temporary_device_goes_on_no_contract(ctx, today, pump_model, dept, pump):
    contract = _contract()
    a = on_site(pump_model, dept, today=today)
    with pytest.raises(ValidationError) as e:
        ct.add_asset(contract, a)
    assert e.value.messages == ["T-0002 is a rental: its owner maintains it, so it goes on no service contract here."]
    eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    with pytest.raises(ValidationError) as e:
        ct.add_asset(contract, a)
    assert e.value.messages == ["T-0002 was returned to its owner and cannot be put on a contract."]
    a.refresh_from_db()
    assert a.contract_id is None and a.support_type == SupportType.OWNER


def test_add_model_and_the_pickers_leave_temporary_devices_out(ctx, today, pump_model, dept, pump):
    contract = _contract()
    a = on_site(pump_model, dept, today=today, serial="AL-1")
    assert list(ct.model_devices(contract, pump_model)) == [pump]
    assert [(m, m.n) for m in ct.model_options(contract)] == [(pump_model, 1)]
    assert list(ct.pick_devices(contract, "AL-1")) == [] and list(ct.pick_devices(contract, "Alaris")) == [pump]
    assert ct.add_model(contract, pump_model) == 1
    a.refresh_from_db()
    pump.refresh_from_db()
    assert a.contract_id is None and a.support_type == SupportType.OWNER and pump.contract == contract
    assert list(ct.model_options(contract)) == []


def test_the_contracts_summary_counts_our_fleet(ctx, today, pump_model, dept, pump, vent):
    on_site(pump_model, dept, today=today)
    ct.add_asset(_contract(), pump)
    s = ct.contracts_summary(today)
    assert (s["active_fleet"], s["covered"], s["covered_pct"]) == (2, 1, 50.0)


# --- the importers ------------------------------------------------------------------------------------------------------------------

def _import(user, kind, *lines):
    from apps.imports import services as imports
    from apps.imports.models import ImportRun

    def through(run):
        run = imports.process(run, user)
        while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
            run = imports.process(run, user)
        return run

    run = imports.upload(user, kind, f"{kind}.csv", ("\r\n".join(lines) + "\r\n").encode("utf-8"))
    run = through(imports.confirm_columns(run, user, imports.mapping_of(run)))
    run = through(imports.start_import(run, user))
    return run, {g["note"]: g for g in imports.problem_groups(run)}


def test_loaner_is_no_longer_read_as_on_loan(ctx, today, pump, make_user):
    run, notes = _import(make_user("director"), "devices", "Tag,Manufacturer,Model,Status",
                         "CE-NEW-1,BD,Alaris 8015 PCU,Loaner", f"{pump.tag},BD,Alaris 8015 PCU,Rental", "CE-NEW-2,BD,Alaris 8015 PCU,On loan")
    assert run.status == "imported"
    assert notes[f"{devices_kind.TEMPORARY_STATUS_NOTE}: added in service"]["keys"] == ["CE-NEW-1"]
    assert notes[f"{devices_kind.TEMPORARY_STATUS_NOTE}: unchanged"]["keys"] == [pump.tag]
    new = Asset.objects.get(tag="CE-NEW-1")
    assert (new.status, new.ownership) == (S.IN_SERVICE, Ownership.OWNED)  # the import adds only devices of ours
    assert Asset.objects.get(tag="CE-NEW-2").status == S.ON_LOAN
    pump.refresh_from_db()
    assert pump.status == S.IN_SERVICE


def test_a_reimport_never_changes_a_temporary_devices_stay_or_puts_it_on_the_pm_program(ctx, today, pump_model, dept, make_user):
    a = on_site(pump_model, dept, today=today)
    next_pm, last_pm = today + timedelta(days=60), today - timedelta(days=30)
    run, notes = _import(make_user("director"), "devices", "Tag,Manufacturer,Model,Status,Room,Next PM,Last PM,Acquisition cost",
                         f"T-0002,BD,Alaris 8015 PCU,Retired,7,{next_pm:%Y-%m-%d},{last_pm:%Y-%m-%d},4500.00")
    assert run.status == "imported"
    assert set(notes) == {devices_kind.TEMPORARY_NOTE, devices_kind.TEMPORARY_KEPT_NOTE,
                          "Last PM, Next PM, Acquisition cost left out: its owner maintains it (no PM dates or cost of ours)"}
    a.refresh_from_db()
    assert (a.room, a.status, a.next_pm_on, a.last_pm_on, a.acquisition_cost) == ("7", S.IN_SERVICE, None, None, Decimal("0"))
    assert (a.ownership, a.owner, a.owner_reference, a.arrived_on) == (Ownership.RENTAL, "Acme Rentals", "RA-2026-0042", today - timedelta(days=10))


def test_a_reimport_never_reinstates_a_returned_device(ctx, today, pump_model, dept, make_user):
    a = eq.return_to_owner(on_site(pump_model, dept, today=today), cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    run, notes = _import(make_user("director"), "devices", "Tag,Manufacturer,Model,Status", "T-0002,BD,Alaris 8015 PCU,In service")
    assert set(notes) == {devices_kind.TEMPORARY_NOTE, devices_kind.RETURNED_KEPT_NOTE}
    a.refresh_from_db()
    assert a.status == S.RETIRED
    assert run.summary["totals"]["Devices by status"] == {eq.RETURNED_LABEL: "1"}


def test_the_contracts_import_leaves_a_temporary_device_off(ctx, today, pump_model, dept, pump, make_user):
    on_site(pump_model, dept, today=today)
    run, notes = _import(make_user("director"), "contracts", "Reference,Vendor,Start date,End date,Asset tag",
                         "SC-9,BioServ,2026-01-01,2030-12-31,T-0002", f"SC-9,,,,{pump.tag}")
    assert run.status == "imported"
    assert notes[contracts_kind.TEMPORARY_NOTE]["keys"] == ["SC-9"]
    contract = Contract.objects.get(reference="SC-9")
    assert list(contract.assets.values_list("tag", flat=True)) == [pump.tag]


# --- the facility's own -----------------------------------------------------------------------------------------------------------

def test_the_services_stay_in_the_facility(ctx, tenant, other_tenant, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today, serial="SN-X")
    eq.return_to_owner(a, cleaning=ReturnCleaning.LABELED, data=ReturnData.CLEARED, today=today)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Pumps")
        assert not eq.matching_returned(model, "SN-X").exists() and not Asset.objects.filter(pk=a.pk).exists()
        with pytest.raises(ValidationError) as e:
            eq.add_temporary_device(tag="T-0002", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme",
                                    serial="SN-X")
        assert set(e.value.message_dict) == {"device_model"}
