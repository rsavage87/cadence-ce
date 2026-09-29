"""
PM engine: generate work orders from schedules and compute completion-rate KPIs.
The math matches the mock's Overview so the product and the demo agree.
"""
from datetime import date, timedelta
from typing import NamedTuple

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q

from apps.equipment.models import Asset, RiskClass
from apps.workorders.models import OPEN_STATUSES, Priority, Source, WorkOrder, WoType
from apps.workorders.services import create_work_order

from .dates import month_bounds
from .schedule import DEFAULT_PM_HOURS


def _create_pm(asset, as_of: date, by=None):
    """One PM work order for `asset`, due on its next PM date (or today when that has passed), priced from its procedure."""
    proc = asset.device_model.pm_procedure
    priority = Priority.HIGH if asset.device_model.risk_class == RiskClass.LIFE_SUPPORT else Priority.NORMAL
    problem = f"Scheduled {asset.pm_interval_months}-month preventive maintenance" + (f", {proc.code}" if proc else "")
    return create_work_order(asset=asset, type=WoType.PM, priority=priority, problem=problem, requester="PM planner", source=Source.PM_PLANNER,
                             opened_on=as_of, due_on=max(asset.next_pm_on, as_of), created_by=by,
                             estimated_hours=proc.estimated_hours if proc else DEFAULT_PM_HOURS)


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
        _create_pm(asset, as_of)
        created += 1
    return created


class PmBatch(NamedTuple):
    created: int
    assigned: int  # of the created ones, how many went to a credentialed technician
    skipped: int  # devices due that day that already had an open PM work order


@transaction.atomic
def create_pm_work_orders_for_day(day: date, by=None, assign_to_technicians: bool = True, today: date | None = None) -> PmBatch:
    """The PM schedule's "Create N PM work orders": one PM work order for each active device whose next PM falls on `day` and
    that has no open PM work order yet. With `assign_to_technicians`, each goes to the technician the schedule suggests
    (credentialed, least loaded; the same pick the day panel and the workload show, see schedule.suggestions_for_day),
    recorded like any assignment; devices nobody is
    credentialed for, or every device when the caller may not assign, are left unassigned for a manager."""
    from apps.workorders.services import assign

    from .schedule import day_devices, suggestions_for_day

    today = today or date.today()
    devices = list(day_devices(day))
    needing = [a for a in devices if not a.has_open_pm]
    suggested = suggestions_for_day(day, needing, today) if assign_to_technicians else {}
    assigned = 0
    for asset in needing:
        wo = _create_pm(asset, today, by=by)
        tech = suggested.get(asset.id)
        if tech is not None:
            assign(wo, technician=tech, by=by)
            assigned += 1
    return PmBatch(len(needing), assigned, len(devices) - len(needing))


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


def pm_on_time_series(year: int, month: int, months: int = 12, life_support_only: bool = False, today: date | None = None) -> list[dict]:
    """Monthly on-time rates for the `months` months ending at (year, month): one query over the PM work orders due in the
    range, bucketed by month with pm_due_queryset's rule (due inside the month and, for the current month, already due or
    already completed). Months after `today` have no rate."""
    today = today or date.today()
    points = []
    y, m = year, month
    for _ in range(months):
        points.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    points.reverse()
    first, last = month_bounds(*points[0])[0], month_bounds(*points[-1])[1]
    qs = WorkOrder.objects.filter(type=WoType.PM, due_on__gte=first, due_on__lte=last)
    if life_support_only:
        qs = qs.filter(asset__device_model__risk_class=RiskClass.LIFE_SUPPORT)
    by_month: dict = {}
    for due_on, completed_on in qs.values_list("due_on", "completed_on"):
        by_month.setdefault((due_on.year, due_on.month), []).append((due_on, completed_on))
    out = []
    for y, m in points:
        start, end = month_bounds(y, m)
        if start > today:
            out.append({"year": y, "month": m, "rate": None, "due": 0, "on_time": 0})
            continue
        as_of = min(end, today)
        rows = [(d, c) for d, c in by_month.get((y, m), []) if d <= as_of and (d < as_of or c is not None)]
        due, on_time = len(rows), sum(1 for d, c in rows if c is not None and c <= d)
        out.append({"year": y, "month": m, "due": due, "on_time": on_time, "rate": (on_time / due * 100) if due else 100.0})
    return out


def overdue_assets(as_of: date | None = None):
    as_of = as_of or date.today()
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, next_pm_on__lt=as_of).select_related("device_model", "department")
