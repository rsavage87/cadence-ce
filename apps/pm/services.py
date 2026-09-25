"""
PM engine: generate work orders from schedules and compute completion-rate KPIs.
The math matches the mock's Overview so the product and the demo agree.
"""
from datetime import date, timedelta

from django.conf import settings
from django.db.models import F, Q

from apps.equipment.models import Asset, RiskClass
from apps.workorders.models import OPEN_STATUSES, Priority, Source, WorkOrder, WoType
from apps.workorders.services import create_work_order

from .dates import month_bounds


def generate_pm_work_orders(as_of: date | None = None, lead_days: int | None = None) -> int:
    """
    Create a PM work order for every active device whose next PM is due within `lead_days`
    and that has no open PM work order yet. Safe to run nightly. Requires a tenant context.
    """
    as_of = as_of or date.today()
    lead_days = settings.PM_LEAD_DAYS if lead_days is None else lead_days
    horizon = as_of + timedelta(days=lead_days)
    created = 0
    assets = (Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, next_pm_on__isnull=False, next_pm_on__lte=horizon)
              .select_related("device_model", "device_model__pm_procedure"))
    for asset in assets:
        if WorkOrder.objects.filter(asset=asset, type=WoType.PM, status__in=OPEN_STATUSES).exists():
            continue
        proc = asset.device_model.pm_procedure
        priority = Priority.HIGH if asset.device_model.risk_class == RiskClass.LIFE_SUPPORT else Priority.NORMAL
        problem = f"Scheduled {asset.pm_interval_months}-month preventive maintenance" + (f", {proc.code}" if proc else "")
        create_work_order(asset=asset, type=WoType.PM, priority=priority, problem=problem, requester="PM planner", source=Source.PM_PLANNER,
                          opened_on=as_of, due_on=max(asset.next_pm_on, as_of), estimated_hours=proc.estimated_hours if proc else 1)
        created += 1
    return created


def pm_due_queryset(start: date, end: date, as_of: date | None = None, life_support_only: bool = False):
    """
    PM work orders that count toward on-time completion for a period:
    due inside [start, end] and, for the current period, already due or already completed.
    """
    as_of = min(end, as_of or date.today())
    qs = WorkOrder.objects.filter(type=WoType.PM, due_on__gte=start, due_on__lte=as_of).filter(Q(due_on__lt=as_of) | Q(completed_on__isnull=False))
    if life_support_only:
        qs = qs.filter(asset__device_model__risk_class=RiskClass.LIFE_SUPPORT)
    return qs


def pm_on_time_rate(start: date, end: date, as_of: date | None = None, life_support_only: bool = False) -> dict:
    due = pm_due_queryset(start, end, as_of, life_support_only)
    total = due.count()
    on_time = due.filter(completed_on__isnull=False, completed_on__lte=F("due_on")).count()
    return {"due": total, "on_time": on_time, "rate": (on_time / total * 100) if total else 100.0}


def pm_on_time_series(year: int, month: int, months: int = 12, life_support_only: bool = False) -> list[dict]:
    """Monthly on-time rates for the `months` months ending at (year, month)."""
    out = []
    y, m = year, month
    points = []
    for _ in range(months):
        points.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for y, m in reversed(points):
        start, end = month_bounds(y, m)
        if start > date.today():
            out.append({"year": y, "month": m, "rate": None, "due": 0, "on_time": 0})
            continue
        r = pm_on_time_rate(start, end, life_support_only=life_support_only)
        out.append({"year": y, "month": m, **r})
    return out


def overdue_assets(as_of: date | None = None):
    as_of = as_of or date.today()
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, next_pm_on__lt=as_of).select_related("device_model", "department")
