"""
Slice 26, the device's side of incoming inspections (apps/equipment/services.py, apps/workorders/services.py, the devices importer,
apps/workorders/inspections.py's read helpers): the invariant (a device waiting for its incoming inspection has no next PM and is out
of service, missing, or retired, unless use_before_inspection put it in service) after every writer; the hold through every service
door; Found and Reinstate; retiring a waiting device; update_asset; use_before_inspection and then a pass; pass_incoming_inspection
on the row as it is; no PM and never a second inspection on a waiting device; the PM machinery never seeing one; the drawer's banner.
The completion side is tests/test_incoming_completion.py.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from incoming_fixtures import INCOMING_ALL_PASS, failed_device, in_use_device, passed_device, waiting_device

from apps.accounts.models import Level, Module, Role, User
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, UseBeforeInspection
from apps.pm import aem, schedule
from apps.pm.dates import add_months
from apps.pm.services import generate_pm_work_orders, overdue_assets, week_assignment_preview
from apps.reports.fleet import report_compliance
from apps.tenants.context import tenant_context
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import InspectionResult, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_service_request, create_work_order

S = AssetStatus


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def history(asset):
    return list(Asset.history.filter(id=asset.id).order_by("history_date", "history_id"))


def assert_invariant(asset):
    """Waiting implies no next PM and out of use, unless use_before_inspection put it in service (its history says so)."""
    asset.refresh_from_db()
    if not asset.awaiting_inspection:
        return
    assert asset.next_pm_on is None, asset.tag
    if asset.status == S.IN_SERVICE:
        assert inspections.uses_before([asset.pk]).get(asset.pk), f"{asset.tag} is in service while waiting, never put in use before it"
    else:
        assert asset.status in (S.OUT_OF_SERVICE, S.MISSING, S.RETIRED), asset.tag


def open_number(asset) -> str:
    return inspections.open_inspection(asset).number


# --- update_asset -----------------------------------------------------------------------------------------------------------------

def test_a_waiting_device_is_edited_with_its_blank_next_pm_and_refuses_a_date(ctx, today, vent_model, dept):
    a = waiting_device(vent_model, today=today)
    number = open_number(a)
    # Edit details sends every editable field, the blank next PM among them: the serial and the room save.
    eq.update_asset(a, serial="SW-REPLACED-1", room="Biomed bench", department=dept, next_pm_on=None, notes="", today=today)
    a.refresh_from_db()
    assert (a.serial, a.room, a.department, a.next_pm_on, a.status, a.awaiting_inspection) == ("SW-REPLACED-1", "Biomed bench", dept, None,
                                                                                                S.OUT_OF_SERVICE, True)
    with pytest.raises(ValidationError) as e:
        eq.update_asset(a, next_pm_on=today + timedelta(days=30), room="Somewhere else", today=today)
    assert e.value.message_dict == {"next_pm_on": [f"Its PM schedule starts when it passes its incoming inspection {number}."]}
    a.refresh_from_db()
    assert a.room == "Biomed bench" and a.next_pm_on is None
    assert_invariant(a)
    # With no inspection open the words still hold, without a number.
    change_status(inspections.open_inspection(a), WoStatus.CANCELLED)
    with pytest.raises(ValidationError) as e:
        eq.update_asset(a, next_pm_on=today + timedelta(days=30), today=today)
    assert e.value.message_dict == {"next_pm_on": ["Its PM schedule starts when it passes its incoming inspection."]}


def test_a_device_in_use_still_needs_its_next_pm(ctx, today, vent):
    with pytest.raises(ValidationError) as e:
        eq.update_asset(vent, next_pm_on=None, today=today)
    assert e.value.message_dict == {"next_pm_on": ["A device in use needs a next PM date."]}


def test_a_pass_then_sets_the_next_pm_through_the_service(ctx, today, vent_model, techs):
    a, wo = passed_device(vent_model, technician=techs["dana"], today=today)
    assert (a.awaiting_inspection, a.status, a.next_pm_on) == (False, S.IN_SERVICE, add_months(today, vent_model.pm_interval_months))
    eq.update_asset(a, next_pm_on=today + timedelta(days=20), today=today)  # an ordinary device now
    a.refresh_from_db()
    assert a.next_pm_on == today + timedelta(days=20)


# --- set_status: the hold, Found, Reinstate, retiring ----------------------------------------------------------------------------

def _waiting_in(status, model, today, by=None):
    """A waiting device moved into `status` through the services."""
    if status == S.IN_SERVICE:
        return in_use_device(model, "W-1", today=today)
    a = waiting_device(model, "W-1", today=today)
    if status in (S.MISSING, S.RETIRED):
        eq.set_status(a, status, today=today, by=by)
    return a


EDGES = [(f, t) for f in (S.OUT_OF_SERVICE, S.MISSING, S.RETIRED, S.IN_SERVICE) for t in sorted(eq.STATUS_CHANGES[f])]


@pytest.mark.parametrize("from_, to", EDGES)
def test_every_status_change_on_a_waiting_device(ctx, today, vent_model, from_, to):
    a = _waiting_in(from_, vent_model, today)
    assert a.status == from_
    if to in eq.HOLD:
        number = inspections.open_inspection(a)
        with pytest.raises(ValidationError) as e:
            eq.set_status(a, to, today=today)
        if number is not None:
            assert e.value.messages == [f"W-1 is waiting for its incoming inspection ({number.number}): it goes into service when that "
                                        "inspection passes."]
        else:  # retired: its inspection was cancelled with the retirement
            assert e.value.messages == ["W-1 is waiting for its incoming inspection, and none is open. Open one: the device goes into "
                                        "service when its incoming inspection passes."]
        a.refresh_from_db()
        assert a.status == from_
    else:
        eq.set_status(a, to, today=today)
        a.refresh_from_db()
        assert a.status == to and a.awaiting_inspection  # the flag never changes here
    assert_invariant(a)


def test_found_keeps_the_device_waiting_with_its_inspection(ctx, today, vent_model):
    a = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(a)
    eq.set_status(a, S.MISSING, today=today)
    eq.set_status(a, S.OUT_OF_SERVICE, today=today)
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.OUT_OF_SERVICE, True, None)
    assert inspections.open_inspection(a) == wo  # the same inspection, still open


def test_retiring_a_waiting_device_cancels_its_inspection_and_reinstating_opens_one(ctx, today, vent_model, make_user):
    director = make_user("director")
    a = waiting_device(vent_model, today=today)
    first = inspections.open_inspection(a)
    eq.set_status(a, S.RETIRED, by=director, note="Returned to the vendor", today=today)
    a.refresh_from_db()
    first.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.RETIRED, True, None)  # retired, and still never inspected
    assert first.status == WoStatus.CANCELLED and first.status_history.last().note == "Device retired"
    assert inspections.open_inspection(a) is None
    eq.set_status(a, S.OUT_OF_SERVICE, by=director, today=today)  # the vendor sent it back: Reinstate
    a.refresh_from_db()
    second = inspections.open_inspection(a)
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.OUT_OF_SERVICE, True, None)
    assert second is not None and second != first and second.created_by == director and second.assigned_to is None
    assert_invariant(a)


def test_retiring_a_waiting_device_refuses_while_its_inspection_is_in_progress(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError, match=f"has open work: {wo.number}"):
        eq.set_status(a, S.RETIRED, today=today)
    # On a device that does not wait, an open inspection blocks retiring as any work does (slice 12's rule).
    other = eq.create_asset(tag="OLD-1", device_model=vent_model, department=a.department, today=today)
    hand = create_work_order(asset=other, type=WoType.INSPECTION, priority="normal", problem="Post-repair inspection")
    with pytest.raises(ValidationError, match=f"has open work: {hand.number}"):
        eq.set_status(other, S.RETIRED, today=today)


def test_reinstating_a_device_that_does_not_wait_out_of_service_gives_it_a_pm_due_today(ctx, today, vent):
    eq.set_status(vent, S.RETIRED, today=today)
    eq.set_status(vent, S.OUT_OF_SERVICE, today=today)
    vent.refresh_from_db()
    assert (vent.status, vent.next_pm_on, vent.awaiting_inspection) == (S.OUT_OF_SERVICE, today, False)
    assert inspections.open_inspection(vent) is None


# --- status_actions ---------------------------------------------------------------------------------------------------------------

def _shape(asset, user):
    return [(x["to"], x["label"], x["style"]) for x in eq.status_actions(asset, user)]


def test_the_drawer_offers_a_waiting_device_found_and_reinstate_never_in_service(ctx, today, vent_model, make_user):
    tech, director = make_user("technician"), make_user("director")
    a = waiting_device(vent_model, today=today)
    assert _shape(a, tech) == [(S.MISSING, "Mark missing", "")]
    assert _shape(a, director) == [(S.MISSING, "Mark missing", ""), (S.RETIRED, "Retire", "danger")]
    confirms = {x["to"]: x["confirm"] for x in eq.status_actions(a, director)}
    assert confirms == {S.MISSING: "Mark NEW-1 missing? Its incoming inspection stays open until it is found.",
                        S.RETIRED: "Retire NEW-1? Its open incoming inspection is cancelled: retire a new device when it goes back to the vendor."}
    eq.set_status(a, S.MISSING, today=today)
    assert _shape(a, tech) == [(S.OUT_OF_SERVICE, "Found", "")]
    assert _shape(a, director) == [(S.OUT_OF_SERVICE, "Found", ""), (S.RETIRED, "Retire", "danger")]
    eq.set_status(a, S.RETIRED, today=today)
    assert _shape(a, tech) == []
    assert _shape(a, director) == [(S.OUT_OF_SERVICE, "Reinstate", "")]
    used = in_use_device(vent_model, "W-2", today=today)
    assert _shape(used, director) == [(S.OUT_OF_SERVICE, "Tag out of service", "danger"), (S.MISSING, "Mark missing", ""),
                                      (S.RETIRED, "Retire", "danger")]  # never on loan
    for slug in ("requester", "analyst", "vendor"):
        assert eq.status_actions(a, make_user(slug)) == []


def test_a_device_that_does_not_wait_is_never_offered_the_waiting_moves(ctx, today, vent, make_user):
    director = make_user("director")
    eq.set_status(vent, S.MISSING, today=today)
    assert _shape(vent, director) == [(S.IN_SERVICE, "Found", ""), (S.RETIRED, "Retire", "danger")]
    eq.set_status(vent, S.RETIRED, today=today)
    assert _shape(vent, director) == [(S.IN_SERVICE, "Reinstate", "")]


def test_the_toasts_word_the_waiting_moves():
    from apps.web.views_equipment import status_toast

    assert status_toast("CE-1", S.MISSING, S.OUT_OF_SERVICE) == "CE-1 found; it stays out of service until its incoming inspection passes"
    assert status_toast("CE-1", S.RETIRED, S.OUT_OF_SERVICE) == "CE-1 reinstated; it stays out of service until its incoming inspection passes"
    assert status_toast("CE-1", S.IN_SERVICE, S.OUT_OF_SERVICE) == "CE-1 tagged out of service"


# --- work orders on a waiting device ----------------------------------------------------------------------------------------------

def test_no_pm_work_order_on_a_waiting_device(ctx, today, vent_model):
    for a in (waiting_device(vent_model, "W-1", today=today), in_use_device(vent_model, "W-2", today=today)):
        with pytest.raises(ValidationError) as e:
            create_work_order(asset=a, type=WoType.PM, priority="normal", problem="Scheduled PM")
        assert e.value.message_dict == {"type": [f"{a.tag} is waiting for its incoming inspection: its PMs start when it passes its incoming "
                                                 "inspection."]}
        assert not WorkOrder.objects.filter(asset=a, type=WoType.PM).exists()


def test_never_a_second_open_inspection_on_a_waiting_device(ctx, today, vent_model, vent):
    a = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(a)
    with pytest.raises(ValidationError) as e:
        create_work_order(asset=a, type=WoType.INSPECTION, priority="normal", problem="Incoming")
    assert e.value.message_dict == {"type": [f"NEW-1 already has its incoming inspection open: {wo.number}."]}
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority="normal", problem="Dented on arrival")  # other work is fine
    assert repair.type == WoType.REPAIR
    change_status(wo, WoStatus.CANCELLED)
    again = create_work_order(asset=a, type=WoType.INSPECTION, priority="normal", problem="Incoming, again")
    assert inspections.open_inspection(a) == again
    # A device that does not wait may carry any number of inspections (a post-repair check, a hand-made one).
    for _ in range(2):
        create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Post-repair inspection")
    assert WorkOrder.objects.filter(asset=vent, type=WoType.INSPECTION, status=WoStatus.OPEN).count() == 2


# --- _on_completed: the hold, and a repair holding the device ---------------------------------------------------------------------

def test_a_tagged_out_portal_repair_never_puts_a_waiting_device_in_service(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    sr = create_service_request(asset=a, department=a.department, problem="Cracked display", urgency="normal", tagged_out=True)
    repair = sr.work_order
    assign(repair, technician=techs["dana"])
    change_status(repair, WoStatus.IN_PROGRESS)
    complete_work_order(repair, resolution="Replaced the display")
    assert_invariant(a)
    assert a.status == S.OUT_OF_SERVICE and a.awaiting_inspection
    # Then the inspection passes: nothing holds it any more, it goes in service.
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS)
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection) == (S.IN_SERVICE, False)


def test_a_pass_while_a_repair_holds_the_device_waits_for_the_repair(ctx, today, vent_model, techs):
    a = waiting_device(vent_model, today=today)
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority="normal", problem="Cracked display", tag_out=True, assigned_to=techs["dana"])
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    done = complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS)
    a.refresh_from_db()
    # It passed: the flag clears and the PM clock starts, but the open tagged-out repair keeps it out of service.
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.OUT_OF_SERVICE, False, add_months(today, vent_model.pm_interval_months))
    assert done.passed and done.device_changed
    change_status(repair, WoStatus.IN_PROGRESS)
    complete_work_order(repair, resolution="Replaced the display")
    a.refresh_from_db()
    assert a.status == S.IN_SERVICE  # the last thing holding it out was the repair


def test_an_imported_open_repair_never_puts_a_waiting_device_in_service(ctx, today, vent_model, techs):
    """A repair imported open from the previous system on a device out of service is what holds it out (legacy.import_work_order tags
    it out); completing it here leaves a waiting device out all the same."""
    from apps.workorders.legacy import import_work_order

    a = waiting_device(vent_model, today=today)
    repair = import_work_order(asset=a, legacy_number="OLD-77", type=WoType.REPAIR, status=WoStatus.IN_PROGRESS, opened_on=today,
                               technician=techs["dana"], today=today).work_order
    assert repair.tagged_out
    complete_work_order(repair, resolution="Replaced the power supply")
    assert_invariant(a)
    assert a.status == S.OUT_OF_SERVICE


def test_a_failed_pms_repair_never_puts_a_waiting_device_in_service(ctx, today, vent_model, techs):
    """No door opens a PM on a waiting device; a failed PM's repair (tagged out, follow_up_of a PM) reaching one anyway (here made by
    hand) leaves it out all the same."""
    a = waiting_device(vent_model, today=today)
    pm = WorkOrder.objects.create(asset=a, type=WoType.PM, priority="normal", problem="Hand-made", due_on=today, status=WoStatus.COMPLETED,
                                  pm_result="fail", completed_on=today)
    repair = create_work_order(asset=a, type=WoType.REPAIR, priority="high", problem=f"PM {pm.number} failed", tag_out=True, follow_up_of=pm,
                               assigned_to=techs["dana"])
    change_status(repair, WoStatus.IN_PROGRESS)
    complete_work_order(repair, resolution="Replaced the line cord")
    assert_invariant(a)
    assert a.status == S.OUT_OF_SERVICE


def test_a_completed_pm_on_a_waiting_device_never_starts_its_pm_clock(ctx, today, vent_model, techs):
    """The PM branch of _on_completed, reached only through a work order whose type was changed by hand: no next PM while it waits."""
    a = waiting_device(vent_model, today=today)
    pm = WorkOrder.objects.create(asset=a, type=WoType.PM, priority="normal", problem="Hand-made", due_on=today, assigned_to=techs["dana"])
    change_status(pm, WoStatus.IN_PROGRESS)
    change_status(pm, WoStatus.COMPLETED)
    a.refresh_from_db()
    assert a.next_pm_on is None and a.last_pm_on == today
    assert_invariant(a)


# --- use_before_inspection --------------------------------------------------------------------------------------------------------

def test_use_before_inspection_needs_approve_and_a_listed_reason(ctx, today, vent_model, make_user):
    a = waiting_device(vent_model, today=today)
    for slug in ("technician", "manager", "vendor", "requester", "analyst"):
        with pytest.raises(PermissionDenied):
            eq.use_before_inspection(a, UseBeforeInspection.EMERGENCY, by=make_user(slug), today=today)
    director = make_user("director")
    for bad in ("", "the patient in bed 4 needed it", None):
        with pytest.raises(ValidationError) as e:
            eq.use_before_inspection(a, bad, by=director, today=today)
        assert e.value.message_dict == {"reason": ["Choose why the device goes into use before its incoming inspection."]}
    a.refresh_from_db()
    assert a.status == S.OUT_OF_SERVICE and len(history(a)) == 1
    assert eq_perms.USE_BEFORE_INSPECTION_LEVEL == eq_perms.RETIRE_LEVEL == Level.APPROVE
    plus = Role.objects.create(name="CE manager (approves)", slug="manager-plus")
    plus.set_levels({Module.EQUIPMENT: Level.APPROVE})
    approver = User.objects.create_user(username="plus@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=plus)
    assert eq_perms.can_use_before_inspection(approver) and not eq_perms.can_use_before_inspection(make_user("technician", username="t2@x.example"))


def test_use_before_inspection_puts_it_in_service_still_waiting_with_its_inspection_due_tomorrow(ctx, today, vent_model, make_user):
    director = make_user("director")
    a = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(a)
    returned = eq.use_before_inspection(a, UseBeforeInspection.LOANER_RENTAL, by=director, today=today)
    assert returned is a
    a.refresh_from_db()
    wo.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.IN_SERVICE, True, None)
    last = history(a)[-1]
    assert last.history_change_reason == "In use before its incoming inspection: Loaner or rental needed now" and last.history_user == director
    assert (wo.priority, wo.due_on) == (Priority.HIGH, today + timedelta(days=1))
    note = wo.status_history.last()
    assert note.changed_by == director and note.from_status == note.to_status == WoStatus.OPEN
    assert note.note == (f"Device put in use before its incoming inspection: Loaner or rental needed now. High priority, due "
                         f"{wo.due_on:%b} {wo.due_on.day}, {wo.due_on.year}: inspect it where it is.")
    uses = inspections.uses_before([a.pk])[a.pk]
    assert [(u.on, u.reason, u.label, u.by) for u in uses] == [(today, UseBeforeInspection.LOANER_RENTAL, "Loaner or rental needed now", director)]
    assert_invariant(a)
    assert eq.status_label(a) == "In service"


def test_use_before_inspection_opens_an_inspection_when_none_is_open_and_keeps_an_earlier_due_date(ctx, today, vent_model):
    a = waiting_device(vent_model, today=today)
    change_status(inspections.open_inspection(a), WoStatus.CANCELLED)
    eq.use_before_inspection(a, UseBeforeInspection.ARRIVED_IN_USE, today=today)
    wo = inspections.open_inspection(a)
    assert wo is not None and (wo.priority, wo.due_on, wo.opened_on) == (Priority.HIGH, today + timedelta(days=1), today)
    # Overdue already: the earlier due date stays (moving it later would hide that it is late).
    b = waiting_device(vent_model, "NEW-2", today=today, added_on=today - timedelta(days=9))
    overdue = inspections.open_inspection(b)
    assert overdue.due_on == today - timedelta(days=4)
    eq.use_before_inspection(b, UseBeforeInspection.EMERGENCY, today=today)
    overdue.refresh_from_db()
    assert (overdue.priority, overdue.due_on) == (Priority.HIGH, today - timedelta(days=4))


def test_use_before_inspection_from_missing_and_its_refusals(ctx, today, vent_model, vent):
    a = waiting_device(vent_model, today=today)
    eq.set_status(a, S.MISSING, today=today)
    eq.use_before_inspection(a, UseBeforeInspection.ARRIVED_IN_USE, today=today)  # found on the unit, in use
    a.refresh_from_db()
    assert a.status == S.IN_SERVICE and a.awaiting_inspection
    with pytest.raises(ValidationError, match="NEW-1 is already in use before its incoming inspection."):
        eq.use_before_inspection(a, UseBeforeInspection.EMERGENCY, today=today)
    with pytest.raises(ValidationError, match="CE-10001 is not waiting for an incoming inspection."):
        eq.use_before_inspection(vent, UseBeforeInspection.EMERGENCY, today=today)
    b = waiting_device(vent_model, "NEW-2", today=today)
    eq.set_status(b, S.RETIRED, today=today)
    with pytest.raises(ValidationError, match="NEW-2 is retired. Reinstate it first."):
        eq.use_before_inspection(b, UseBeforeInspection.EMERGENCY, today=today)


def test_use_before_then_a_pass_clears_the_flag_and_starts_the_clock_with_no_status_change(ctx, today, vent_model, techs):
    a = in_use_device(vent_model, today=today)
    rows = len(history(a))
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    done = complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS)
    a.refresh_from_db()
    assert (a.status, a.awaiting_inspection, a.next_pm_on) == (S.IN_SERVICE, False, add_months(today, vent_model.pm_interval_months))
    assert done.passed and not done.tagged_out
    new_rows = history(a)[rows:]
    assert [(h.status, h.history_change_reason) for h in new_rows] == [(S.IN_SERVICE, f"Passed incoming inspection {wo.number}")]
    # The record of its use before the inspection stays for the survey binder.
    assert [u.reason for u in inspections.uses_before([a.pk])[a.pk]] == [UseBeforeInspection.EMERGENCY]


# --- pass_incoming_inspection -----------------------------------------------------------------------------------------------------

def test_the_pass_is_one_save_with_one_history_row(ctx, today, vent_model, techs, make_user):
    tech_user = make_user("technician")
    a = waiting_device(vent_model, today=today)
    rows = len(history(a))
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS, by=tech_user)
    new_rows = history(a)[rows:]
    assert len(new_rows) == 1
    h = new_rows[0]
    assert (h.status, h.awaiting_inspection, h.next_pm_on, h.history_change_reason, h.history_user) == (
        S.IN_SERVICE, False, add_months(today, vent_model.pm_interval_months), f"Passed incoming inspection {wo.number}", tech_user)


def test_a_device_no_longer_waiting_moves_nothing_on_a_second_pass(ctx, today, vent_model, techs):
    """The row is locked and read again: a copy that still says waiting (a second pass at the same moment) moves nothing."""
    a = waiting_device(vent_model, today=today)
    stale = Asset.objects.get(pk=a.pk)
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS)
    a.refresh_from_db()
    eq.set_status(a, S.OUT_OF_SERVICE, today=today)  # tagged out since, for a broken cord
    rows = len(history(a))
    assert stale.awaiting_inspection  # the copy from before the pass
    eq.pass_incoming_inspection(stale, wo, on=today + timedelta(days=3))
    a.refresh_from_db()
    assert (a.status, a.next_pm_on) == (S.OUT_OF_SERVICE, add_months(today, vent_model.pm_interval_months)) and len(history(a)) == rows


def test_a_pass_dated_on_an_earlier_day_dates_its_history_row_that_day(ctx, today, vent_model, techs):
    added = today - timedelta(days=12)
    a = waiting_device(vent_model, today=today, added_on=added)
    wo = inspections.open_inspection(a)
    assign(wo, technician=techs["dana"])
    complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS, today=added + timedelta(days=2))
    last = history(a)[-1]
    assert timezone.localtime(last.history_date).date() == added + timedelta(days=2) and last.status == S.IN_SERVICE
    a.refresh_from_db()
    assert a.next_pm_on == add_months(added + timedelta(days=2), vent_model.pm_interval_months)


# --- the PM machinery never sees a waiting device -----------------------------------------------------------------------------

def test_the_pm_machinery_never_sees_a_waiting_device_installed_three_years_ago(ctx, today, vent_model, pump_model):
    installed = today - timedelta(days=3 * 365)
    assert eq.first_pm_due(vent_model, installed_on=installed, today=today) == today  # what an ordinary device would get: due today
    waiting = waiting_device(vent_model, "OLD-NEW-1", today=today, installed_on=installed)
    used = eq.use_before_inspection(waiting_device(vent_model, "OLD-NEW-2", today=today, installed_on=installed), UseBeforeInspection.EMERGENCY,
                                    today=today)
    pumps = waiting_device(pump_model, "OLD-NEW-3", today=today, installed_on=installed)
    ours = {waiting.pk, used.pk, pumps.pk}
    assert generate_pm_work_orders(as_of=today, lead_days=400) == 0
    assert not WorkOrder.objects.filter(type=WoType.PM).exists()
    for day in (today, today + timedelta(days=30)):
        assert not ours & {a.pk for a in schedule.day_devices(day)}
    assert not ours & {a.pk for a in overdue_assets(today)}
    preview = week_assignment_preview(today)
    assert preview.overdue == 0 and not preview.uncovered and not preview.shares
    classes = {c["key"]: c for c in report_compliance(today)["classes"]}
    assert classes["life_support"]["overdue"] == 0 and classes["high"]["overdue"] == 0
    assert not ours & {a.pk for a, _day in aem.pull_in_plan(pump_model, 6, today=today)}
    assert not ours & {a.pk for a, _day in aem.pull_in_plan(vent_model, 3, today=today)}
    eq.set_status(waiting, S.MISSING, today=today)  # missing too: still no PM, nothing generated
    assert generate_pm_work_orders(as_of=today, lead_days=400) == 0


# --- the devices importer -----------------------------------------------------------------------------------------------------------

def _import(user, *lines):
    from apps.imports import services as imports
    from apps.imports.models import ImportRun

    def through(run):
        run = imports.process(run, user)
        while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
            run = imports.process(run, user)
        return run

    run = imports.upload(user, "devices", "inventory.csv", ("\r\n".join(lines) + "\r\n").encode("utf-8"))
    run = through(imports.confirm_columns(run, user, imports.mapping_of(run)))
    run = through(imports.start_import(run, user))
    return run, {g["note"]: g for g in imports.problem_groups(run)}


def test_a_reimport_keeps_a_waiting_device_waiting_and_says_why(ctx, today, vent_model, make_user):
    director = make_user("director")
    a = waiting_device(vent_model, "CE-11001", today=today)
    number = open_number(a)
    next_pm = today + timedelta(days=60)
    run, notes = _import(director, "Tag,Manufacturer,Model,Status,Room,Next PM",
                         f"CE-11001,Hamilton Medical,Hamilton-G5,In service,7,{next_pm:%Y-%m-%d}")
    assert run.status == "imported"
    assert notes[f"Next PM left out: its PM schedule starts when it passes its incoming inspection {number}."]["keys"] == ["CE-11001"]
    assert notes[f"Status kept: CE-11001 is waiting for its incoming inspection ({number}): it goes into service when that inspection "
                 "passes."]["keys"] == ["CE-11001"]
    a.refresh_from_db()
    assert (a.room, a.status, a.next_pm_on, a.awaiting_inspection) == ("7", S.OUT_OF_SERVICE, None, True)  # the room still changed
    assert_invariant(a)


# --- the drawer's banner ------------------------------------------------------------------------------------------------------------

def _day(d):
    return f"{d:%b} {d.day}, {d.year}"


def test_the_banner_for_a_waiting_device(ctx, today, vent_model):
    a = waiting_device(vent_model, today=today)
    wo = inspections.open_inspection(a)
    b = inspections.banner(a)
    assert b.awaiting and b.open == wo and b.open_number == wo.number and b.open_due_on == wo.due_on and not b.offer_open
    assert b.lines == [f"Waiting for its incoming inspection: {wo.number}, due {_day(wo.due_on)}."]
    change_status(wo, WoStatus.CANCELLED)
    b = inspections.banner(a)
    assert b.offer_open and b.open is None and b.lines == ["Waiting for its incoming inspection; none is open."]


def test_the_banner_after_a_fail_and_in_use(ctx, today, vent_model, techs, make_user):
    a, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)
    b = inspections.banner(a)
    assert (b.failed, b.failed_on, b.open) == (failed, today, again)
    assert b.lines == [f"Failed its incoming inspection on {_day(today)} ({failed.number}); re-inspection {again.number} open, due "
                       f"{_day(again.due_on)}."]
    used = in_use_device(vent_model, "NEW-9", today=today, by=make_user("director"))
    b = inspections.banner(used)
    assert b.in_use is not None and b.in_use.reason == UseBeforeInspection.EMERGENCY
    assert b.lines == [f"In use before its incoming inspection: Emergency clinical need (since {_day(today)}).",
                       f"Waiting for its incoming inspection: {b.open_number}, due {_day(today + timedelta(days=1))}."]


def test_the_banner_of_a_device_that_passed_has_no_lines_but_names_the_evidence(ctx, today, vent_model, techs):
    a, wo = passed_device(vent_model, technician=techs["dana"], today=today)
    b = inspections.banner(a)
    assert not b.awaiting and b.lines == [] and (b.passed, b.passed_number, b.passed_on) == (wo, wo.number, today)
    assert inspections.banner(Asset.objects.create(tag="X-1", device_model=vent_model, department=a.department)).lines == []


def test_the_banner_masks_numbers_outside_a_scoped_users_share(ctx, today, vent_model, techs, make_user):
    vendor = make_user("vendor")
    vendor.company = "Hamilton Service"
    vendor.save()
    a, failed, again = failed_device(vent_model, technician=techs["dana"], today=today)  # in-house work: not the vendor's
    b = inspections.banner(a, vendor)
    assert (b.failed, b.failed_number, b.open, b.open_number) == (None, "", None, "")
    assert b.lines == [f"Failed its incoming inspection on {_day(today)}; re-inspection open, due {_day(again.due_on)}."]
    assign(again, vendor_name="Hamilton Service")  # now the vendor's: they see its number
    b = inspections.banner(a, vendor)
    assert b.open == again and b.failed is None
    assert b.lines == [f"Failed its incoming inspection on {_day(today)}; re-inspection {again.number} open, due {_day(again.due_on)}."]
    w = waiting_device(vent_model, "NEW-8", today=today)
    assert inspections.banner(w, vendor).lines == [f"Waiting for its incoming inspection, due {_day(inspections.open_inspection(w).due_on)}."]


def test_waiting_for_inspector_counts_unassigned_inspections_of_waiting_devices(ctx, today, vent_model, vent, techs):
    a = waiting_device(vent_model, "W-1", today=today)
    b = waiting_device(vent_model, "W-2", today=today)
    assign(inspections.open_inspection(b), technician=techs["dana"])
    create_work_order(asset=vent, type=WoType.INSPECTION, priority="normal", problem="Post-repair inspection")  # not a waiting device
    assert list(inspections.waiting_for_inspector()) == [inspections.open_inspection(a)]


# --- tenant isolation ---------------------------------------------------------------------------------------------------------------

def test_reads_stay_in_the_facility(ctx, tenant, other_tenant, today):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = waiting_device(model, "NEW-1", department=dept, today=today)
        eq.use_before_inspection(theirs, UseBeforeInspection.EMERGENCY, today=today)
        their_wo = inspections.open_inspection(theirs)
    # Ours, inside our facility: their device's history and work orders are not read.
    assert inspections.uses_before([theirs.pk]) == {}
    assert not inspections.waiting_for_inspector().filter(pk=their_wo.pk).exists()
    assert not inspections.incoming(theirs).exists()
