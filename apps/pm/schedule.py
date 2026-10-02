"""
PM schedule read models (slice 9): the month calendar, the devices due on a day, the next 30 days, technician workload
for the next 7 days, and the PM library. Everything reads `Asset.next_pm_on` for active devices, as the mock's
pmByDay does. Read-only; the one state change (creating a day's work orders) is in services.py.

`suggest_technicians` is the assignment rule the schedule shows and the create action applies: among the technicians
credentialed for a device, the one with the fewest hours already on their plate, counting what this batch has just
given them. The mock hashed devices across credentialed technicians; balancing by workload is the same idea, made
deterministic and fair.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Case, Count, Exists, OuterRef, Q, Subquery, Sum, Value, When

from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.equipment.services import RISK_RANK
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

from .dates import month_bounds

DEFAULT_PM_HOURS = Decimal("1")  # a model without a PM procedure; generate_pm_work_orders estimates the same
DAY_LIST_LIMIT = 12  # the mock lists the first twelve devices on a day
CATEGORY_LIMIT = 8
RISK_ORDER = {rc: i for i, rc in enumerate(RiskClass.values)}
# RISK_RANK orders assets (it reads device_model__risk_class); the library orders device models, so it needs its own.
RISK_RANK_MODEL = Case(*[When(risk_class=r, then=Value(i)) for i, r in enumerate(RiskClass.values)], default=Value(9))


def pm_hours(device_model) -> Decimal:
    proc = device_model.pm_procedure
    return proc.estimated_hours if proc else DEFAULT_PM_HOURS


def _active():
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, next_pm_on__isnull=False)


def _open_pm(asset_ref="pk"):
    return WorkOrder.objects.filter(asset_id=OuterRef(asset_ref), type=WoType.PM, status__in=OPEN_STATUSES)


# --- calendar ---------------------------------------------------------------------------------------

def month_calendar(year: int, month: int, today: date) -> dict:
    """Six-or-fewer Sunday-first weeks covering the month. Each cell: date, in_month, is_today, n (active devices whose next PM
    falls that day), life_support and high counts (the mock's coloured dots), and past (a past day that still has PMs on it:
    those devices are overdue, the red count). One grouped query."""
    first, last = month_bounds(year, month)
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)  # back to Sunday
    grid_end = last + timedelta(days=(5 - last.weekday()) % 7)  # forward to Saturday
    counts: dict = {}
    rows = (_active().filter(next_pm_on__gte=grid_start, next_pm_on__lte=grid_end).order_by()
            .values("next_pm_on", "device_model__risk_class").annotate(n=Count("id")))
    for row in rows:
        c = counts.setdefault(row["next_pm_on"], {"n": 0, "life_support": 0, "high": 0})
        c["n"] += row["n"]
        if row["device_model__risk_class"] == RiskClass.LIFE_SUPPORT:
            c["life_support"] += row["n"]
        elif row["device_model__risk_class"] == RiskClass.HIGH:
            c["high"] += row["n"]
    weeks, d = [], grid_start
    while d <= grid_end:
        week = []
        for _ in range(7):
            c = counts.get(d, {"n": 0, "life_support": 0, "high": 0})
            week.append({"date": d, "in_month": d.month == month, "is_today": d == today, "past": d < today and c["n"] > 0, **c})
            d += timedelta(days=1)
        weeks.append(week)
    due_this_month = sum(c["n"] for day, c in counts.items() if first <= day <= last)
    return {"year": year, "month": month, "first": first, "weeks": weeks, "due_this_month": due_this_month}


# --- a day ------------------------------------------------------------------------------------------

def day_devices(day: date):
    """Active devices whose next PM falls on `day`, most critical first. Each carries has_open_pm and, when it has one, who the
    open PM work order is with (open_pm_assignee: a technician's name, or None; open_pm_vendor: True for vendor service)."""
    first_open = _open_pm().order_by("opened_on", "number")
    return (_active().filter(next_pm_on=day).select_related("device_model", "device_model__pm_procedure", "department")
            .annotate(has_open_pm=Exists(_open_pm()), open_pm_assignee=Subquery(first_open.values("assigned_to__name")[:1]),
                      open_pm_vendor=Subquery(first_open.values("vendor_service")[:1]), risk_rank=RISK_RANK)
            .order_by("risk_rank", "tag"))


def open_hours_by_technician() -> dict:
    """{technician id: estimated hours of their open in-house work orders}: what is already on each plate."""
    rows = (WorkOrder.objects.filter(status__in=OPEN_STATUSES, vendor_service=False, assigned_to__isnull=False).order_by()
            .values("assigned_to_id").annotate(h=Sum("estimated_hours")))
    return {row["assigned_to_id"]: row["h"] or Decimal("0") for row in rows}


def _pools(today: date):
    """(active technicians, pool(asset)): the technicians credentialed for a device, worked out once per device model from one
    query of active technicians with their credentials, however many devices are asked about."""
    from apps.credentials.models import Technician
    from apps.credentials.services import qualification

    techs = list(Technician.objects.filter(is_active=True).prefetch_related("credentials"))
    by_model: dict = {}

    def pool(asset):
        if asset.device_model_id not in by_model:
            by_model[asset.device_model_id] = [t for t in techs if qualification(t, asset, today).ok]
        return by_model[asset.device_model_id]

    return techs, pool


def _assign(assets, load: dict, pool, hours_of=None) -> dict:
    """The least-loaded credentialed technician for each device in order (ties by name), adding the device's hours to `load`
    as it goes, so later devices see what earlier ones were given. Mutates `load`."""
    out = {}
    for asset in assets:
        candidates = pool(asset)
        if not candidates:
            out[asset.id] = None
            continue
        tech = min(candidates, key=lambda t: (load.get(t.id, Decimal("0")), t.name))
        load[tech.id] = load.get(tech.id, Decimal("0")) + (hours_of(asset) if hours_of else pm_hours(asset.device_model))
        out[asset.id] = tech
    return out


def suggest_technicians(assets, today: date, load: dict | None = None) -> dict:
    """{asset id: Technician or None}. Among the technicians credentialed for each device, the one with the fewest hours,
    counting open work and what earlier devices in `assets` were given. Ties go by name. None when nobody is credentialed."""
    _techs, pool = _pools(today)
    return _assign(assets, dict(open_hours_by_technician() if load is None else load), pool)


WEEK_DAYS = 7


def _planned(open_pm: dict | None, active_ids: set) -> bool:
    """A device's PM is on someone's plate when its open PM work order is with an active in-house technician or with the vendor.
    An unassigned one (the nightly generate_pm makes those) or one held by a deactivated technician still needs a technician."""
    return bool(open_pm) and (open_pm["vendor_service"] or open_pm["assigned_to_id"] in active_ids)


def week_plan(today: date) -> dict:
    """The one plan the day panel, the create action, and the workload share for the next 7 days (today through today + 6):
    day by day in date order, each day's devices most critical first, every device whose PM is not yet on someone's plate goes
    to the least-loaded credentialed technician, carrying the load forward. Creating a day's work orders assigns what this plan
    suggests, so creating the days in any order ends where the plan says."""
    techs, pool = _pools(today)
    active_ids = {t.id for t in techs}
    devices = list(_active().filter(next_pm_on__gte=today, next_pm_on__lt=today + timedelta(days=WEEK_DAYS))
                   .select_related("device_model", "device_model__pm_procedure").annotate(risk_rank=RISK_RANK)
                   .order_by("next_pm_on", "risk_rank", "tag"))
    open_pm = {}
    for w in (WorkOrder.objects.filter(asset__in=devices, type=WoType.PM, status__in=OPEN_STATUSES).order_by("opened_on", "number")
              .values("asset_id", "assigned_to_id", "vendor_service", "estimated_hours")):
        open_pm.setdefault(w["asset_id"], w)

    def hours_of(asset):
        w = open_pm.get(asset.id)
        return w["estimated_hours"] if w else pm_hours(asset.device_model)

    load = open_hours_by_technician()
    unplanned = [a for a in devices if not _planned(open_pm.get(a.id), active_ids)]
    suggested = _assign(unplanned, load, pool, hours_of)  # already in date, then risk, then tag order
    return {"techs": techs, "active_ids": active_ids, "devices": devices, "open_pm": open_pm, "suggested": suggested, "hours_of": hours_of}


def suggestions_for_day(day: date, assets, today: date) -> dict:
    """The technicians the schedule suggests for `assets` due on `day`: the week plan's inside the next 7 days (so the day panel,
    the create action, and the workload agree), otherwise a fresh least-loaded pick for that day alone."""
    if today <= day < today + timedelta(days=WEEK_DAYS):
        suggested = week_plan(today)["suggested"]
        return {a.id: suggested.get(a.id) for a in assets}
    return suggest_technicians(assets, today)


def day_plan(day: date, today: date) -> dict:
    """What the day panel shows: every device due that day with its hours and the technician the create action would assign,
    the total hours, and how many still need a PM work order (a device with any open one is skipped by the create action)."""
    devices = list(day_devices(day))
    needing = [a for a in devices if not a.has_open_pm]
    suggested = suggestions_for_day(day, needing, today)
    rows = [{"asset": a, "hours": pm_hours(a.device_model), "procedure": a.device_model.pm_procedure, "has_open_pm": a.has_open_pm,
             "open_pm_assignee": a.open_pm_assignee, "open_pm_vendor": bool(a.open_pm_vendor), "technician": suggested.get(a.id)}
            for a in devices]
    return {"day": day, "rows": rows, "count": len(rows), "hours": sum((r["hours"] for r in rows), Decimal("0")), "to_create": len(needing),
            "overdue": day < today}


def open_pm_orders(asset_ids: list) -> dict:
    """{asset id: its open PM work order as a dict} in one query, the earliest opened when there are several (the one the day
    panel reads), with what pm_held() and the route sheets need."""
    out: dict = {}
    rows = (WorkOrder.objects.filter(asset_id__in=asset_ids, type=WoType.PM, status__in=OPEN_STATUSES).order_by("opened_on", "number")
            .values("asset_id", "number", "vendor_service", "vendor_name", "assigned_to_id", "assigned_to__name", "assigned_to__is_active"))
    for w in rows:
        out.setdefault(w["asset_id"], w)
    return out


def pm_held(w: dict | None) -> bool:
    """Whether an open PM work order (open_pm_orders) is on someone's plate: with the vendor, or with an active technician."""
    return bool(w) and (w["vendor_service"] or bool(w["assigned_to_id"] and w["assigned_to__is_active"]))


def suggest_waiting(day: date, today: date, waiting: list, rows: list[dict]) -> dict:
    """Technicians for `waiting`: devices due on `day` whose open PM work order is on nobody's plate (unassigned, or with a
    deactivated technician). Inside the week the week plan already counts the day's other devices (suggestions_for_day). Outside
    it the schedule picks for the day alone, so start from the hours day_plan's `rows` just gave the devices needing a work order:
    the waiting ones are balanced against them, not piled on whoever sorts first by name."""
    if today <= day < today + timedelta(days=WEEK_DAYS):
        return suggestions_for_day(day, waiting, today)
    load = open_hours_by_technician()
    for r in rows:
        if r["technician"] is not None and not r["has_open_pm"]:
            load[r["technician"].id] = load.get(r["technician"].id, Decimal("0")) + r["hours"]
    return suggest_technicians(waiting, today, load=load)


def planned_technicians(day: date, today: date, plan: dict | None = None) -> tuple[dict, dict]:
    """(who does each device due on `day`: {asset id: Technician or None}, the day's open PM work orders): the day panel's pick for a
    device needing a work order, suggest_waiting's for one whose open PM is on nobody's plate. A device whose open PM is held
    (vendor, active technician) has no entry: the work order says who. The route sheets and the device drawer's PM tab read this,
    so both name the technician the PM screen does."""
    plan = plan or day_plan(day, today)
    open_pm = open_pm_orders([r["asset"].id for r in plan["rows"]])
    waiting = [r["asset"] for r in plan["rows"] if r["asset"].id in open_pm and not pm_held(open_pm[r["asset"].id])]
    picks = {r["asset"].id: r["technician"] for r in plan["rows"] if r["asset"].id not in open_pm}
    picks.update(suggest_waiting(day, today, waiting, plan["rows"]) if waiting else {})
    return picks, open_pm


# --- next 30 days and the week's workload ------------------------------------------------------------------

def next_30_days(today: date) -> dict:
    """The mock's outlook: PMs due today through 30 days out, the life-support devices among them, devices already overdue, the
    estimated hours, and the categories with the most PMs coming due."""
    upcoming = _active().filter(next_pm_on__gte=today, next_pm_on__lte=today + timedelta(days=30))
    hours = sum((pm_hours(a.device_model) for a in upcoming.select_related("device_model__pm_procedure").only(
        "device_model__pm_procedure__estimated_hours")), Decimal("0"))
    by_category = list(upcoming.order_by().values("device_model__category").annotate(n=Count("id")).order_by("-n", "device_model__category"))
    return {"due": upcoming.count(), "life_support": upcoming.filter(device_model__risk_class=RiskClass.LIFE_SUPPORT).count(),
            "overdue": _active().filter(next_pm_on__lt=today).count(), "hours": hours,
            "by_category": [(r["device_model__category"], r["n"]) for r in by_category[:CATEGORY_LIMIT]]}


@dataclass
class Load:
    technician: object
    pm_hours: Decimal
    pm_count: int
    repair_hours: Decimal

    @property
    def total(self) -> Decimal:
        return self.pm_hours + self.repair_hours

    @property
    def capacity(self) -> Decimal:
        return self.technician.weekly_capacity_hours

    @property
    def over(self) -> bool:
        return self.total > self.capacity

    @property
    def pct(self) -> float:
        return min(100.0, float(self.total / self.capacity * 100)) if self.capacity else 100.0


def workload_next_7_days(today: date) -> list[Load]:
    """Per active technician: PM hours for devices due in the next 7 days, from the week plan (the assignee of the device's open
    PM work order when that is an active technician; otherwise the technician the plan suggests, which covers the unassigned
    work orders the nightly generate_pm creates), plus the estimated hours of their other open in-house work orders, against
    weekly capacity. Vendor PMs and devices nobody is credentialed for are on nobody's plate."""
    plan = week_plan(today)
    techs = plan["techs"]
    other = {row["assigned_to_id"]: row["h"] or Decimal("0") for row in
             WorkOrder.objects.filter(status__in=OPEN_STATUSES, vendor_service=False, assigned_to__isnull=False).exclude(type=WoType.PM)
             .order_by().values("assigned_to_id").annotate(h=Sum("estimated_hours"))}
    pm = {t.id: [Decimal("0"), 0] for t in techs}
    for a in plan["devices"]:
        w = plan["open_pm"].get(a.id)
        if _planned(w, plan["active_ids"]):
            if not w["vendor_service"]:
                pm[w["assigned_to_id"]][0] += w["estimated_hours"]
                pm[w["assigned_to_id"]][1] += 1
            continue
        tech = plan["suggested"].get(a.id)
        if tech is not None:
            pm[tech.id][0] += plan["hours_of"](a)
            pm[tech.id][1] += 1
    return [Load(t, pm[t.id][0], pm[t.id][1], other.get(t.id, Decimal("0"))) for t in techs]


# --- the PM library --------------------------------------------------------------------------------------

def pm_library() -> list[dict]:
    """Every device model with its PM program: the OEM interval, the interval in force (AEM when approved; never for life
    support, nor for the equipment CMS keeps on the manufacturer's schedule: `oem_required`), and its procedure. Most critical
    first, then category."""
    models = (DeviceModel.objects.select_related("pm_procedure")
              .annotate(devices=Count("assets", filter=Q(assets__status__in=Asset.ACTIVE_STATUSES)), rank=RISK_RANK_MODEL)
              .order_by("rank", "category", "manufacturer", "model"))
    out = []
    for dm in models:
        proc = dm.pm_procedure
        aem = dm.pm_interval_months != dm.oem_pm_interval_months
        out.append({"device_model": dm, "devices": dm.devices, "oem_months": dm.oem_pm_interval_months, "program_months": dm.pm_interval_months,
                    "aem": aem, "oem_required": dm.oem_schedule_required, "procedure": proc, "hours": pm_hours(dm),
                    "steps": len(proc.checklist) if proc else 0})
    return out


def schedule_summary(today: date) -> dict:
    """The page head: PMs due in the next 30 days, their hours, and devices overdue now."""
    n = next_30_days(today)
    return {"due_30": n["due"], "hours_30": n["hours"], "overdue": n["overdue"]}
