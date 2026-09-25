"""
KPI math for the Overview. Definitions match the mock so the demo and the product agree:

- PM completion on time: PM work orders due in the period (and already due, or completed)
  that were completed on or before their due date.
- Fleet uptime: 1 - (repair downtime days / active devices x days in period).
- MTTR: mean turnaround of repairs completed in the period.
- Cost of service ratio: trailing-6-month service cost annualized, plus active contract
  costs, over the fleet's acquisition value.
"""
from datetime import date, timedelta

from django.db.models import Count, Q
from django.db.models.functions import Coalesce, TruncMonth

from apps.contracts.models import Contract
from apps.equipment.models import Asset, AssetStatus, RiskClass
from apps.equipment.services import fleet_bucket_counts
from apps.pm.dates import month_bounds
from apps.pm.services import overdue_assets, pm_on_time_rate, pm_on_time_series
from apps.recalls.models import AlertMatch
from apps.workorders.models import OPEN_STATUSES, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import PRIORITY_RANK, unassigned_portal_requests


def overview_kpis(year: int, month: int, today: date | None = None) -> dict:
    today = today or date.today()
    start, end = month_bounds(year, month)
    current = (year, month) == (today.year, today.month)
    as_of = today if current else end
    days = as_of.day if current else end.day

    active = Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES)
    active_count = active.count()
    acquisition = float(sum(a.acquisition_cost for a in active.only("acquisition_cost")))

    open_at = WorkOrder.objects.filter(opened_on__lte=as_of).exclude(completed_on__lte=as_of).exclude(status=WoStatus.CANCELLED)
    open_count = open_at.count()
    overdue_count = open_at.filter(due_on__lt=as_of).count()
    awaiting_parts = open_at.filter(status=WoStatus.AWAITING_PARTS).count()

    pm = pm_on_time_rate(start, end, as_of)
    pm_ls = pm_on_time_rate(start, end, as_of, life_support_only=True)

    repairs = list(WorkOrder.objects.filter(type=WoType.REPAIR, completed_on__gte=start, completed_on__lte=as_of).prefetch_related("labor_lines", "part_lines"))
    turnaround = [w.turnaround_days for w in repairs]
    mttr = sum(turnaround) / len(turnaround) if turnaround else 0.0
    spend = sum(w.total_cost() for w in repairs)
    downtime = sum(w.downtime_days for w in repairs)
    uptime = 100.0 - (downtime / (active_count * days) * 100 if active_count and days else 0.0)

    since = today - timedelta(days=182)
    done6 = list(WorkOrder.objects.filter(completed_on__gte=since, completed_on__lte=today).prefetch_related("labor_lines", "part_lines"))
    annualize = 365 / 182
    in_house = sum(w.total_cost() for w in done6 if not w.vendor_service) * annualize
    vendor_tm = sum(w.total_cost() for w in done6 if w.vendor_service) * annualize
    contracts = float(sum(c.annual_cost for c in Contract.objects.filter(end_on__gte=today)))
    total = in_house + vendor_tm + contracts
    cosr = total / acquisition * 100 if acquisition else 0.0

    alerts_received = AlertMatch.objects.filter(alert__published_on__gte=start, alert__published_on__lte=as_of).values("alert").distinct().count()
    alerts_open = AlertMatch.objects.exclude(status__in=[AlertMatch.Status.CLOSED, AlertMatch.Status.NOT_AFFECTED]).values("alert").distinct().count()
    alerts_needing_action = AlertMatch.objects.filter(status=AlertMatch.Status.NEEDS_ACTION).values("alert").distinct().count()

    return {
        "period": {"year": year, "month": month, "start": start, "end": end, "as_of": as_of, "current": current, "days": days},
        "active_devices": active_count,
        "pm_on_time": pm, "pm_on_time_life_support": pm_ls,
        "open_work_orders": open_count, "overdue_work_orders": overdue_count, "awaiting_parts": awaiting_parts,
        "repairs_closed": len(repairs), "mttr_days": mttr, "repair_spend": spend,
        "downtime_days": downtime, "uptime_pct": uptime,
        "cost_of_service": {"in_house": in_house, "vendor_tm": vendor_tm, "contracts": contracts, "total": total, "acquisition": acquisition,
                            "ratio_pct": cosr},
        "alerts": {"received": alerts_received, "open": alerts_open, "needs_action": alerts_needing_action},
    }


def open_work_orders_count() -> int:
    return WorkOrder.objects.filter(status__in=OPEN_STATUSES).count()


# --- Overview screen ----------------------------------------------------------------------------
# Targets shown on the KPI tiles. Hard-coded until editable policy lands (slice 8).
KPI_TARGETS = {"pm_on_time": 95.0, "pm_on_time_life_support": 100.0, "uptime_pct": 99.5, "mttr_days": 3.0}
ATTENTION_CONTRACT_DAYS = 30  # the mock flags contracts ending within 30 days (the contracts screen warns at 90)
ATTENTION_AWAITING_PARTS_DAYS = 7
ATTENTION_HIGH_RISK_LIMIT = 3


def _item(rail, title, sub, right, **link):
    return {"rail": rail, "title": title, "sub": sub, "right": right, **link}


def attention_items(today: date | None = None) -> list[dict]:
    """The Overview's "Needs attention" list, in the mock's order. Each item carries one link key: asset, wo, contract, or recall."""
    today = today or date.today()
    items = []
    overdue = overdue_assets(today).order_by("next_pm_on", "tag")
    for a in overdue.filter(device_model__risk_class=RiskClass.LIFE_SUPPORT):
        items.append(_item("crit", f"{a.tag} · {a.device_model.description}", f"{a.device_model} · {a.department} · life support",
                           f"PM overdue {(today - a.next_pm_on).days} d", asset=a.tag))

    active_q = Q(device_model__assets__status__in=Asset.ACTIVE_STATUSES)
    for m in (AlertMatch.objects.filter(status=AlertMatch.Status.NEEDS_ACTION).select_related("alert", "device_model")
              .annotate(devices=Count("device_model__assets", filter=active_q)).order_by("-alert__published_on")):
        label = " ".join(p for p in (m.alert.get_source_display(), m.alert.classification) if p)
        items.append(_item("warn", f"{label}: {m.device_model}", m.alert.title, f"{m.devices} devices", recall=str(m.id)))

    covered = Count("assets", filter=~Q(assets__status=AssetStatus.RETIRED))
    for c in Contract.objects.filter(end_on__lt=today).annotate(devices=covered).filter(devices__gt=0).order_by("-end_on"):
        items.append(_item("warn", f"{c.reference} · {c.vendor} expired {c.end_on:%b} {c.end_on.day}",
                           f"{c.devices} devices no longer covered · {c.get_coverage_display()}", "Renew or reassign", contract=str(c.id)))
    ending = Contract.objects.filter(end_on__gte=today, end_on__lte=today + timedelta(days=ATTENTION_CONTRACT_DAYS))
    for c in ending.annotate(devices=covered).order_by("end_on"):
        items.append(_item("warn", f"{c.reference} · {c.vendor} ends {c.end_on:%b} {c.end_on.day}", f"{c.devices} devices · {c.get_coverage_display()}",
                           f"{(c.end_on - today).days} d left", contract=str(c.id)))

    wo_related = ("asset", "asset__device_model", "asset__department")
    portal = list(unassigned_portal_requests().select_related(*wo_related).order_by(PRIORITY_RANK, "opened_on"))
    for w in portal:
        items.append(_item("crit" if w.priority == Priority.CRITICAL else "warn", f"{w.number} · {w.asset.device_model.description}, {w.asset.department}",
                           f"Portal request, unassigned: {w.problem}", f"{w.get_priority_display()} · {(today - w.opened_on).days} d open", wo=w.number))

    for a in overdue.filter(device_model__risk_class=RiskClass.HIGH)[:ATTENTION_HIGH_RISK_LIMIT]:
        items.append(_item("warn", f"{a.tag} · {a.device_model.description}", f"{a.device_model} · {a.department} · high risk",
                           f"PM overdue {(today - a.next_pm_on).days} d", asset=a.tag))

    urgent = (WorkOrder.objects.filter(status__in=OPEN_STATUSES).filter(Q(priority=Priority.CRITICAL) | Q(priority=Priority.HIGH, due_on__lt=today))
              .exclude(pk__in=[w.pk for w in portal]).select_related(*wo_related).order_by(PRIORITY_RANK, "due_on"))
    for w in urgent:
        items.append(_item("crit" if w.priority == Priority.CRITICAL else "warn", f"{w.number} · {w.asset.device_model.description}, {w.asset.department}",
                           w.problem, f"{w.get_status_display()} · {(today - w.opened_on).days} d open", wo=w.number))

    stale = today - timedelta(days=ATTENTION_AWAITING_PARTS_DAYS)
    for w in WorkOrder.objects.filter(status=WoStatus.AWAITING_PARTS, opened_on__lt=stale).select_related(*wo_related).order_by("opened_on"):
        items.append(_item("warn", f"{w.number} · {w.asset.device_model.description}, {w.asset.department}", "Awaiting parts",
                           f"{(today - w.opened_on).days} d open", wo=w.number))
    return items


def nav_counts() -> dict:
    return {
        "equipment": Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).count(),
        "workorders": WorkOrder.objects.filter(status__in=OPEN_STATUSES).count(),
        "workorders_hot": unassigned_portal_requests().exists(),
    }


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    i = year * 12 + (month - 1) + delta
    return i // 12, i % 12 + 1


WO_TYPE_SERIES = [("pm", "Preventive maintenance", (WoType.PM,)), ("repair", "Corrective repair", (WoType.REPAIR,)),
                  ("other", "Inspection, safety, and recall", (WoType.INSPECTION, WoType.SAFETY, WoType.RECALL))]


def work_orders_opened_by_type(year: int, month: int, months: int = 6) -> dict:
    points = [_shift_month(year, month, -i) for i in reversed(range(months))]
    first, _ = month_bounds(*points[0])
    _, last = month_bounds(*points[-1])
    counts = {}
    for row in (WorkOrder.objects.filter(opened_on__gte=first, opened_on__lte=last).annotate(mo=TruncMonth("opened_on"))
                .order_by().values("mo", "type").annotate(n=Count("id"))):
        counts[(row["mo"].year, row["mo"].month, row["type"])] = row["n"]
    return {"months": points, "series": [{"key": key, "name": name, "values": [sum(counts.get((y, m, t), 0) for t in types) for y, m in points]}
                                         for key, name, types in WO_TYPE_SERIES]}


def repair_spend_by_category(start: date, as_of: date, limit: int = 8) -> list[tuple[str, float]]:
    spend = {}
    for w in (WorkOrder.objects.filter(type=WoType.REPAIR, completed_on__gte=start, completed_on__lte=as_of)
              .select_related("asset__device_model").prefetch_related("labor_lines", "part_lines")):
        cat = w.asset.device_model.category
        spend[cat] = spend.get(cat, 0.0) + w.total_cost()
    return sorted(spend.items(), key=lambda kv: -kv[1])[:limit]


def recent_activity(start: date, as_of: date, limit: int = 8):
    return list(WorkOrder.objects.annotate(event_on=Coalesce("completed_on", "started_on", "opened_on"))
                .filter(event_on__gte=start, event_on__lte=as_of)
                .select_related("asset", "asset__device_model", "asset__department", "assigned_to").order_by("-event_on", "-updated_at")[:limit])


def overview_page(year: int, month: int, today: date | None = None) -> dict:
    """Everything the Overview screen shows for one month. Fleet state and attention items are always as of today."""
    today = today or date.today()
    k = overview_kpis(year, month, today)
    py, pm = _shift_month(year, month, -1)
    # No deltas against a month before the tenant has any history; they would compare against zeros.
    has_prev = WorkOrder.objects.filter(opened_on__lte=month_bounds(py, pm)[1]).exists()
    prev = overview_kpis(py, pm, today=today) if has_prev else None
    start, as_of = k["period"]["start"], k["period"]["as_of"]
    affected = Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, device_model__alert_matches__status=AlertMatch.Status.NEEDS_ACTION).distinct().count()
    return {
        "k": k, "prev": prev, "targets": KPI_TARGETS, "alert_devices_affected": affected,
        "buckets": fleet_bucket_counts(today),
        "pm_series": pm_on_time_series(year, month), "pm_series_life_support": pm_on_time_series(year, month, life_support_only=True),
        "attention": attention_items(today),
        "opened_by_type": work_orders_opened_by_type(year, month),
        "spend_by_category": repair_spend_by_category(start, as_of),
        "recent": recent_activity(start, as_of),
    }
