"""
PM engine: generate work orders from schedules and compute completion-rate KPIs.
The math matches the mock's Overview so the product and the demo agree.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.equipment.models import Asset, AssetStatus, RiskClass
from apps.notifications import assignments
from apps.workorders.models import OPEN_STATUSES, Priority, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

from .dates import month_bounds
from .schedule import DEFAULT_PM_HOURS, WEEK_DAYS, _planned, week_plan


def _create_pm(asset, as_of: date, by=None):
    """One PM work order for `asset`, due on its next PM date (or today when that has passed), priced from its procedure."""
    proc = asset.device_model.pm_procedure
    priority = Priority.HIGH if asset.device_model.risk_class == RiskClass.LIFE_SUPPORT else Priority.NORMAL
    problem = f"Scheduled {asset.pm_interval_months}-month preventive maintenance" + (f", {proc.code}" if proc else "")
    return create_work_order(asset=asset, type=WoType.PM, priority=priority, problem=problem, requester="PM planner", source=Source.PM_PLANNER,
                             opened_on=as_of, due_on=max(asset.next_pm_on, as_of), created_by=by,
                             estimated_hours=proc.estimated_hours if proc else DEFAULT_PM_HOURS)


@transaction.atomic
def generate_pm_work_orders(as_of: date | None = None, lead_days: int | None = None) -> int:
    """
    Create a PM work order for every active device whose next PM is due within `lead_days`
    and that has no open PM work order yet. Safe to run nightly. Requires a tenant context.
    Takes the planner lock first, as Create and Auto-assign week do: on PostgreSQL two of them at once (the nightly job, the API's
    generate, a planner clicking Create) would otherwise each find no open PM and each create one.
    """
    lock_planner()
    as_of = as_of or timezone.localdate()
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
    credentialed for, or every device when the caller may not assign, are left unassigned for a manager. The day's devices are
    locked first, so a double click, or Auto-assign week running at the same moment, cannot give a device two PM work orders. Slice 20:
    each technician given work is emailed once, listing all of it (apps.notifications.assignments.batch)."""
    from apps.workorders.services import assign

    from .schedule import day_devices, suggestions_for_day

    today = today or timezone.localdate()
    lock_planner()
    devices = list(day_devices(day))
    needing = [a for a in devices if not a.has_open_pm]
    suggested = suggestions_for_day(day, needing, today) if assign_to_technicians else {}
    assigned = 0
    with assignments.batch():  # slice 20: each technician gets one email listing their new PMs, once this commits
        for asset in needing:
            wo = _create_pm(asset, today, by=by)
            tech = suggested.get(asset.id)
            if tech is not None:
                assign(wo, technician=tech, by=by)
                assigned += 1
    return PmBatch(len(needing), assigned, len(devices) - len(needing))


PLANNER_LOCK = "pm-planner"


def lock_planner() -> None:
    """Hold the facility's PM planner lock until the transaction ends: Create (a day) and Auto-assign week take it before they read
    the plan, so a second request (a double click, or both buttons at once) waits here, then reads what the first one committed and
    finds nothing left to do. A row of its own that nothing else locks (a Sequence row that never counts), taken first: locking the
    devices instead would wait in a circle with writers that lock a work order or the numbering before the device (retiring a
    device, a tagged-out repair request). SQLite ignores the lock and runs one writer at a time anyway."""
    from apps.core.models import Sequence
    from apps.tenants.context import get_current_tenant

    tenant = get_current_tenant()
    try:
        with transaction.atomic():  # a savepoint: two first-ever requests may both try to create the row
            Sequence.unscoped.get_or_create(tenant=tenant, key=PLANNER_LOCK)  # unscoped + explicit tenant, as Sequence.next
    except IntegrityError:
        pass
    Sequence.unscoped.select_for_update().get(tenant=tenant, key=PLANNER_LOCK)


# --- Auto-assign week (slice 14) --------------------------------------------------------------------------------------------

@dataclass
class TechnicianShare:
    """What Auto-assign week gives one technician: new PM work orders, open ones already on file, and their estimated hours."""
    technician: object
    new: int = 0
    existing: int = 0
    hours: Decimal = Decimal("0")

    @property
    def count(self) -> int:
        return self.new + self.existing


@dataclass
class WeekAssignment:
    """Auto-assign week over start..end (today through today + 6), from the week plan: the preview, or what was done.

    `shares` are the technicians who get PMs, by name. `uncovered` are the devices nobody is credentialed for, each
    {"asset", "has_open_pm"}: a work order is created for those without one, and every one of them stays unassigned. `held` counts
    the devices whose open PM work order is already with an active technician or the vendor (left as they are). `overdue` counts
    active devices whose next PM is before `start`: they are not in the week plan, so Auto-assign week does not touch them. Of
    those, `overdue_to_create` have no open PM work order (Create on their day makes one) and `overdue_waiting` have one on
    nobody's plate (unassigned, as the nightly generate_pm leaves them, or with a deactivated technician: assign it from Work
    orders); the rest are with a technician or the vendor."""
    start: date
    end: date
    shares: list = field(default_factory=list)
    uncovered: list = field(default_factory=list)
    held: int = 0
    overdue: int = 0
    overdue_to_create: int = 0
    overdue_waiting: int = 0

    @property
    def assigned_new(self) -> int:
        return sum(s.new for s in self.shares)

    @property
    def assigned_existing(self) -> int:
        return sum(s.existing for s in self.shares)

    @property
    def assigned(self) -> int:
        """PMs that go to a technician: new work orders and open ones on nobody's plate."""
        return self.assigned_new + self.assigned_existing

    @property
    def hours(self) -> Decimal:
        return sum((s.hours for s in self.shares), Decimal("0"))

    @property
    def technicians(self) -> int:
        return len(self.shares)

    @property
    def unassigned(self) -> int:
        """Devices left on nobody's plate because nobody is credentialed for them."""
        return len(self.uncovered)

    @property
    def unassigned_new(self) -> int:
        """Of those, the devices with no open PM work order: one is created, unassigned, for a manager."""
        return sum(1 for u in self.uncovered if not u["has_open_pm"])

    @property
    def created(self) -> int:
        """New PM work orders, assigned or not."""
        return self.assigned_new + self.unassigned_new

    @property
    def nothing_to_do(self) -> bool:
        return self.created == 0 and self.assigned == 0


def _week_steps(today: date) -> tuple[WeekAssignment, list]:
    """(the summary, the steps): for each device in the week plan whose PM is on nobody's plate, in the plan's order,
    (asset, its open PM work order as week_plan read it or None, the technician the plan suggests or None). Devices whose open PM is
    with the vendor or an active technician have no step. Read-only."""
    from .schedule import open_pm_orders, pm_held

    plan = week_plan(today)
    overdue_ids = list(overdue_assets(today).values_list("pk", flat=True))
    overdue_open = open_pm_orders(overdue_ids)
    out = WeekAssignment(start=today, end=today + timedelta(days=WEEK_DAYS - 1), overdue=len(overdue_ids),
                         overdue_to_create=sum(1 for pk in overdue_ids if pk not in overdue_open),
                         overdue_waiting=sum(1 for w in overdue_open.values() if not pm_held(w)))
    shares: dict = {}
    steps = []
    for asset in plan["devices"]:
        w = plan["open_pm"].get(asset.id)
        if _planned(w, plan["active_ids"]):
            out.held += 1
            continue
        tech = plan["suggested"].get(asset.id)
        steps.append((asset, w, tech))
        if tech is None:
            out.uncovered.append({"asset": asset, "has_open_pm": w is not None})
            continue
        share = shares.setdefault(tech.id, TechnicianShare(tech))
        if w is None:
            share.new += 1
        else:
            share.existing += 1
        share.hours += plan["hours_of"](asset)
    out.shares = sorted(shares.values(), key=lambda s: s.technician.name)
    return out, steps


def week_assignment_preview(today: date | None = None) -> WeekAssignment:
    """What Auto-assign week would do now. Changes nothing."""
    return _week_steps(today or timezone.localdate())[0]


@transaction.atomic
def assign_week(*, by=None, today: date | None = None) -> WeekAssignment:
    """The PM schedule's "Auto-assign week": put every PM due today through today + 6 on a technician's plate, as the week plan
    (schedule.week_plan, the one the day panel, Create, and the workload show) suggests. A device with no open PM work order gets one
    (as Create does) assigned to the suggested technician; an open PM work order on nobody's plate (unassigned, like those the nightly
    generate_pm creates, or with a deactivated technician) is assigned to the suggested technician; one with the vendor or an active
    technician is left alone. Nobody credentialed: the work order is created if missing and left unassigned. Assignments are recorded
    like any other (workorders.services.assign). The planner lock is taken first (lock_planner), so a second request (a double click)
    waits, then finds everything on someone's plate and does nothing. Slice 20: each technician given work is emailed once, listing
    all of it (apps.notifications.assignments.batch). Returns what was done."""
    from apps.workorders.services import assign

    today = today or timezone.localdate()
    lock_planner()
    result, steps = _week_steps(today)
    waiting = _first_open_pms([asset.id for asset, w, tech in steps if w is not None and tech is not None])
    with assignments.batch():  # slice 20: each technician gets one email listing the week's PMs now theirs, once this commits
        for asset, w, tech in steps:
            if w is None:
                wo = _create_pm(asset, today, by=by)
            else:
                wo = waiting.get(asset.id)  # None only if it was closed a moment ago, after the plan was read
            if wo is not None and tech is not None:
                assign(wo, technician=tech, by=by)
    return result


def _first_open_pms(asset_ids: list) -> dict:
    """{asset id: its open PM work order}, the earliest opened when there are several: the one week_plan reads."""
    out: dict = {}
    for wo in (WorkOrder.objects.filter(asset_id__in=asset_ids, type=WoType.PM, status__in=OPEN_STATUSES)
               .select_related("asset__device_model", "tenant").order_by("opened_on", "number")):
        out.setdefault(wo.asset_id, wo)
    return out


# A PM cancelled on a device that is now retired (retiring cancels them, equipment.services.set_status) is not a missed PM: the
# device left the fleet before the PM was due. A cancelled PM on a device still in use is missed, and stays counted.
RETIRED_AND_CANCELLED = Q(status=WoStatus.CANCELLED, asset__status=AssetStatus.RETIRED)


def pm_due_queryset(start: date, end: date, as_of: date | None = None, life_support_only: bool = False):
    """
    PM work orders that count toward on-time completion for a period:
    due inside [start, end] and, for the current period, already due or already completed.
    """
    as_of = min(end, as_of or timezone.localdate())
    qs = (WorkOrder.objects.filter(type=WoType.PM, due_on__gte=start, due_on__lte=as_of).filter(Q(due_on__lt=as_of) | Q(completed_on__isnull=False))
          .exclude(RETIRED_AND_CANCELLED))
    if life_support_only:
        qs = qs.filter(asset__device_model__risk_class=RiskClass.LIFE_SUPPORT)
    return qs


def missed_pms(today: date | None = None):
    """PM work orders that missed their due date as of `today` (slice 25): due before today and not completed by it, whether still
    open, completed late, or cancelled. RETIRED_AND_CANCELLED is the one exclusion, as in pm_due_queryset, so these are exactly the
    PMs the on-time figures count as not on time. Why one was late (WorkOrder.late_reason) is recorded on these and only these
    (apps.workorders.services.set_late_reason)."""
    today = today or timezone.localdate()
    return (WorkOrder.objects.filter(type=WoType.PM, due_on__lt=today).exclude(completed_on__lte=F("due_on"))
            .exclude(RETIRED_AND_CANCELLED))


def missed_due_date(wo, today: date | None = None) -> bool:
    """Whether `wo` is one of missed_pms(today)."""
    return wo.type == WoType.PM and missed_pms(today).filter(pk=wo.pk).exists()


def pm_on_time_rate(start: date, end: date, as_of: date | None = None, life_support_only: bool = False) -> dict:
    due = pm_due_queryset(start, end, as_of, life_support_only)
    total = due.count()
    on_time = due.filter(completed_on__isnull=False, completed_on__lte=F("due_on")).count()
    return {"due": total, "on_time": on_time, "rate": (on_time / total * 100) if total else 100.0}


def pm_on_time_series(year: int, month: int, months: int = 12, life_support_only: bool = False, today: date | None = None) -> list[dict]:
    """Monthly on-time rates for the `months` months ending at (year, month): one query over the PM work orders due in the
    range, bucketed by month with pm_due_queryset's rule (due inside the month and, for the current month, already due or
    already completed). Months after `today` have no rate."""
    today = today or timezone.localdate()
    points = []
    y, m = year, month
    for _ in range(months):
        points.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    points.reverse()
    first, last = month_bounds(*points[0])[0], month_bounds(*points[-1])[1]
    qs = WorkOrder.objects.filter(type=WoType.PM, due_on__gte=first, due_on__lte=last).exclude(RETIRED_AND_CANCELLED)
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
    as_of = as_of or timezone.localdate()
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, next_pm_on__lt=as_of).select_related("device_model", "department")
