import pytest
from django.core.exceptions import ValidationError

from apps.equipment.models import AssetStatus
from apps.workorders.models import LaborLine, PartLine, Urgency, WoStatus
from apps.workorders.services import assign, change_status, create_service_request, create_work_order


def test_numbers_are_sequential_per_tenant(ctx, vent, pump):
    a = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm")
    b = create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch")
    assert a.number.startswith("WO-") and int(b.number.split("-")[-1]) == int(a.number.split("-")[-1]) + 1


def test_illegal_transition_rejected(ctx, vent):
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm")
    with pytest.raises(ValidationError):
        change_status(wo, WoStatus.CLOSED)


def test_costs_sum_labor_and_parts(ctx, vent):
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm")
    LaborLine.objects.create(work_order=wo, hours=2, rate=82)
    PartLine.objects.create(work_order=wo, description="Flow sensor", quantity=2, unit_cost=150)
    assert wo.total_cost() == 2 * 82 + 2 * 150


def test_repair_completion_returns_device_to_service(ctx, vent):
    wo = create_work_order(asset=vent, type="repair", priority="critical", problem="Dead", tag_out=True)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    change_status(wo, "in_progress")
    change_status(wo, "completed")
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE


def test_assignment_flags_uncredentialed_override(ctx, vent, techs):
    wo = create_work_order(asset=vent, type="repair", priority="high", problem="Alarm")
    assign(wo, technician=techs["tom"])  # Tom has no ventilator credential
    assert "override" in wo.status_history.last().note
    assign(wo, technician=techs["dana"])
    assert "override" not in wo.status_history.last().note


def test_portal_request_creates_unassigned_work_order(ctx, vent, dept):
    sr = create_service_request(asset=vent, department=dept, problem="Screen frozen", urgency=Urgency.CRITICAL, requester_name="A Nurse", callback="x4411",
                                tagged_out=True)
    assert sr.number.startswith("SR-")
    assert sr.work_order.priority == "critical" and sr.work_order.assigned_to is None and sr.work_order.source == "portal"
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE


def test_work_cannot_complete_before_it_was_opened(ctx, vent):
    from datetime import date, timedelta

    import pytest
    from django.core.exceptions import ValidationError

    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Booked ahead", opened_on=date.today() + timedelta(days=5))
    with pytest.raises(ValidationError, match="before it was opened"):
        change_status(wo, "in_progress")
    with pytest.raises(ValidationError):
        change_status(wo, "completed", as_of=date.today() + timedelta(days=4))
    assert wo.turnaround_days is None and change_status(wo, "in_progress", as_of=wo.opened_on).started_on == wo.opened_on
