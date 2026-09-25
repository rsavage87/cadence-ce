from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Level
from apps.credentials.services import ranked_technicians
from apps.workorders.models import Priority, WoStatus
from apps.workorders.permissions import can_assign, can_transition, transition_level
from apps.workorders.services import (
    WorkOrderFilters,
    add_note,
    assign,
    board_columns,
    change_status,
    create_service_request,
    create_work_order,
    filter_work_orders,
    timeline,
    unassigned_portal_requests,
)

TODAY = date.today()


def _numbers(qs):
    return [w.number for w in qs]


def test_list_defaults_to_open_work_orders(ctx, vent):
    open_ = create_work_order(asset=vent, type="repair", priority="normal", problem="Open")
    done = create_work_order(asset=vent, type="repair", priority="normal", problem="Done")
    change_status(done, "in_progress")
    change_status(done, "completed")
    assert _numbers(filter_work_orders(WorkOrderFilters())) == [open_.number]
    assert set(_numbers(filter_work_orders(WorkOrderFilters(open_only=False)))) == {open_.number, done.number}
    assert _numbers(filter_work_orders(WorkOrderFilters(open_only=False, status=WoStatus.COMPLETED))) == [done.number]


def test_unassigned_filter_excludes_vendor_dispatch(ctx, vent, techs):
    nobody = create_work_order(asset=vent, type="repair", priority="normal", problem="Nobody")
    vendor = create_work_order(asset=vent, type="repair", priority="normal", problem="Vendor")
    assign(vendor, vendor_name="Hamilton service")
    tech = create_work_order(asset=vent, type="repair", priority="normal", problem="Tech")
    assign(tech, technician=techs["dana"])
    assert _numbers(filter_work_orders(WorkOrderFilters(assigned="unassigned"))) == [nobody.number]
    assert _numbers(filter_work_orders(WorkOrderFilters(assigned=str(techs["dana"].id)))) == [tech.number]


def test_search_matches_number_tag_and_problem(ctx, vent, pump):
    a = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm on startup")
    b = create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch")
    assert _numbers(filter_work_orders(WorkOrderFilters(q="latch"))) == [b.number]
    assert _numbers(filter_work_orders(WorkOrderFilters(q=vent.tag))) == [a.number]
    assert _numbers(filter_work_orders(WorkOrderFilters(q=a.number))) == [a.number]


def test_board_sorts_by_priority_then_due_and_hides_old_done_work(ctx, vent, pump):
    low = create_work_order(asset=pump, type="repair", priority=Priority.LOW, problem="Low")
    crit = create_work_order(asset=vent, type="repair", priority=Priority.CRITICAL, problem="Crit")
    high_late = create_work_order(asset=pump, type="repair", priority=Priority.HIGH, problem="High soon", due_on=TODAY)
    old = create_work_order(asset=pump, type="repair", priority="normal", problem="Old", opened_on=TODAY - timedelta(days=30))
    change_status(old, "in_progress", as_of=TODAY - timedelta(days=29))
    change_status(old, "completed", as_of=TODAY - timedelta(days=20))
    recent = create_work_order(asset=pump, type="repair", priority="normal", problem="Recent")
    change_status(recent, "in_progress")
    change_status(recent, "completed")
    cols = {c["name"]: c for c in board_columns(WorkOrderFilters())}
    assert _numbers(cols["Open"]["items"]) == [crit.number, high_late.number, low.number]
    assert _numbers(cols["Completed, last 7 days"]["items"]) == [recent.number] and cols["Completed, last 7 days"]["count"] == 1


def test_board_ignores_the_status_filters(ctx, vent):
    create_work_order(asset=vent, type="repair", priority="normal", problem="Open")
    cols = board_columns(WorkOrderFilters(status=WoStatus.CLOSED, open_only=True))
    assert cols[0]["count"] == 1


def test_notes_are_trimmed_and_bounded(ctx, vent):
    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm")
    with pytest.raises(ValidationError):
        add_note(wo, "   ")
    with pytest.raises(ValidationError):
        add_note(wo, "x" * 1001)
    note = add_note(wo, "  Ordered flow sensor  ")
    assert note.text == "Ordered flow sensor" and note.tenant_id == wo.tenant_id


def test_timeline_merges_history_and_notes_in_order(ctx, vent, techs, make_user):
    kim = make_user("manager")
    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm", requester="RN, ICU")
    assign(wo, technician=techs["tom"], by=kim)
    change_status(wo, "in_progress", by=kim)
    add_note(wo, "Ordered sensor", by=kim)
    texts = [e["text"] for e in timeline(wo)]
    assert texts == ["Opened: Alarm", "Assigned to Tom Okafor (override: not credentialed for this device)", "Status changed to in progress", "Ordered sensor"]
    assert timeline(wo)[0]["who"] == "RN, ICU" and timeline(wo)[-1]["who"] == "Manager User"


def test_portal_requests_show_as_unassigned_until_dispatched(ctx, vent, dept, techs):
    sr = create_service_request(asset=vent, department=dept, problem="Screen frozen", urgency="high")
    create_work_order(asset=vent, type="pm", priority="normal", problem="PM")  # unassigned, but not from the portal
    assert list(unassigned_portal_requests()) == [sr.work_order]
    assign(sr.work_order, technician=techs["dana"])
    assert not unassigned_portal_requests().exists()


def test_closing_and_reopening_need_approve(ctx, make_user):  # levels are read through tenant-scoped permissions
    tech, manager = make_user("technician"), make_user("manager")
    assert transition_level(WoStatus.COMPLETED, WoStatus.CLOSED) == Level.APPROVE
    assert transition_level(WoStatus.CLOSED, WoStatus.IN_PROGRESS) == Level.APPROVE
    assert transition_level(WoStatus.OPEN, WoStatus.IN_PROGRESS) == Level.EDIT
    assert can_transition(tech, WoStatus.IN_PROGRESS, WoStatus.COMPLETED)
    assert not can_transition(tech, WoStatus.COMPLETED, WoStatus.CLOSED)
    assert can_transition(manager, WoStatus.COMPLETED, WoStatus.CLOSED)
    assert not can_assign(tech) and can_assign(manager)


def test_ranked_technicians_lists_everyone_credentialed_first(vent, techs):
    ranked = [(t.name, q.ok) for t, q in ranked_technicians(vent)]
    assert ranked == [("Dana Whitfield", True), ("Tom Okafor", False)]
