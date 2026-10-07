"""
Slice 26's scaffold: a device added waiting for its incoming inspection (create_asset's "waiting" path), the read helpers
(apps/workorders/inspections.py), and status_label.
"""
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from incoming_fixtures import waiting_device

from apps.equipment import services as eq
from apps.equipment.models import AddedAs, AssetStatus
from apps.workorders import inspections
from apps.workorders.models import WoStatus, WoType

TODAY = date(2026, 10, 8)


def test_a_new_device_waits_out_of_service_with_its_inspection_open(ctx, vent_model):
    a = waiting_device(vent_model, today=TODAY)
    assert (a.status, a.awaiting_inspection, a.next_pm_on, a.added_as) == (AssetStatus.OUT_OF_SERVICE, True, None, AddedAs.NEW)
    s = inspections.state(a)
    assert s.awaiting and s.open is not None and s.passed is None and s.failed is None
    wo = s.open
    assert (wo.type, wo.status, wo.assigned_to, wo.problem) == (WoType.INSPECTION, WoStatus.OPEN, None, inspections.INCOMING_PROBLEM)
    assert (wo.opened_on, wo.due_on) == (TODAY, TODAY + timedelta(days=inspections.INSPECTION_DUE_DAYS))
    assert inspections.open_inspection(a) == wo
    assert eq.status_label(a) == "Awaiting inspection"
    assert a.history.first().history_change_reason == "Added, waiting for its incoming inspection"


def test_the_waiting_path_refuses_what_does_not_fit(ctx, vent_model):
    with pytest.raises(ValidationError) as e:
        waiting_device(vent_model, today=TODAY, inspection_due=TODAY - timedelta(days=1))
    assert "inspection_due" in e.value.message_dict
    from apps.equipment.models import Department

    dept = Department.objects.create(name="ICU")
    for extra, key in (({"next_pm_on": TODAY + timedelta(days=30)}, "next_pm_on"), ({"added_as": AddedAs.EXISTING}, "incoming_inspection"),
                       ({"incoming_inspection": "maybe"}, "incoming_inspection")):
        kwargs = {"incoming_inspection": eq.INCOMING_WAITING, **extra}
        with pytest.raises(ValidationError) as e:
            eq.create_asset(tag="X-1", device_model=vent_model, department=dept, today=TODAY, **kwargs)
        assert key in e.value.message_dict, extra


def test_added_on_an_earlier_day_opens_its_inspection_that_day(ctx, vent_model):
    a = waiting_device(vent_model, today=TODAY, added_on=TODAY - timedelta(days=12))
    assert inspections.open_inspection(a).opened_on == TODAY - timedelta(days=12)


def test_the_old_path_is_unchanged(ctx, vent_model):
    from apps.equipment.models import Department

    a = eq.create_asset(tag="OLD-1", device_model=vent_model, department=Department.objects.create(name="OR"), today=TODAY)
    assert (a.status, a.awaiting_inspection, inspections.open_inspection(a)) == (AssetStatus.IN_SERVICE, False, None) and a.next_pm_on
    assert eq.status_label(a) == "In service"
