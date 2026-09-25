from datetime import date, timedelta

from apps.pm.dates import add_months
from apps.pm.services import generate_pm_work_orders, pm_on_time_rate
from apps.workorders.models import WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert add_months(date(2026, 3, 15), -12) == date(2025, 3, 15)


def test_generate_creates_one_open_pm_per_device(ctx, vent, pump):
    assert generate_pm_work_orders(lead_days=21) == 1  # the vent is due in 10 days, the pump in 90
    assert generate_pm_work_orders(lead_days=21) == 0  # idempotent
    wo = WorkOrder.objects.get(asset=vent, type=WoType.PM)
    assert wo.status == WoStatus.OPEN and wo.priority == "high" and wo.due_on == vent.next_pm_on


def test_completing_pm_advances_schedule(ctx, vent):
    generate_pm_work_orders(lead_days=21)
    wo = WorkOrder.objects.get(asset=vent, type=WoType.PM)
    change_status(wo, "in_progress")
    change_status(wo, "completed")
    vent.refresh_from_db()
    assert vent.last_pm_on == date.today()
    assert vent.next_pm_on == add_months(date.today(), 6)  # life support ignores AEM, follows the OEM 6-month interval


def test_aem_interval_applies_to_non_life_support(pump):
    assert pump.pm_interval_months == 18


def test_on_time_rate_counts_only_due_or_completed(ctx, vent, pump):
    today = date.today()
    start = date(today.year, today.month, 1)
    early = WorkOrder.objects.create(asset=vent, type=WoType.PM, problem="PM", opened_on=today - timedelta(days=20), due_on=today - timedelta(days=5))
    change_status(early, "in_progress", as_of=today - timedelta(days=10))
    change_status(early, "completed", as_of=today - timedelta(days=6))
    late = WorkOrder.objects.create(asset=pump, type=WoType.PM, problem="PM", opened_on=today - timedelta(days=20), due_on=today - timedelta(days=3))
    WorkOrder.objects.create(asset=pump, type=WoType.PM, problem="PM", opened_on=today, due_on=today + timedelta(days=10))  # not due yet, excluded
    r = pm_on_time_rate(start - timedelta(days=31), today + timedelta(days=30))
    assert r["due"] == 2 and r["on_time"] == 1 and round(r["rate"]) == 50
    assert late.is_late
