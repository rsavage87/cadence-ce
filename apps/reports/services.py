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

from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth

from apps.contracts.models import Contract
from apps.equipment.models import Asset, AssetStatus, RiskClass
from apps.equipment.services import fleet_bucket_counts
from apps.facility.services import kpi_targets
from apps.pm.dates import month_bounds
from apps.pm.services import overdue_assets, pm_on_time_rate, pm_on_time_series
from apps.recalls.models import AlertMatch
from apps.workorders.models import OPEN_STATUSES, LaborLine, PartLine, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import PRIORITY_RANK, unassigned_portal_requests

TRAILING_DAYS = 182  # the "6 months" every service-cost figure annualizes from (365/182): the Overview tile and the cost reports agree
ANNUALIZE = 365 / TRAILING_DAYS


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

    cost = cost_of_service(today, acquisition)

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
        "cost_of_service": cost,
        "alerts": {"received": alerts_received, "open": alerts_open, "needs_action": alerts_needing_action},
    }


def cost_of_service(today: date, acquisition: float | None = None) -> dict:
    """The annualized service cost behind the cost-of-service ratio: work orders completed in the trailing 182 days
    (in-house, and vendor time and materials) scaled to a year, plus the annual cost of contracts that have not ended,
    over the acquisition value of the active fleet. The Overview tile and the cost reports share this. Two grouped
    queries and two aggregates, whatever the fleet size: no work order is instantiated."""
    if acquisition is None:
        acquisition = float(Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).aggregate(s=Sum("acquisition_cost"))["s"] or 0)
    since = today - timedelta(days=TRAILING_DAYS)
    money = DecimalField(max_digits=14, decimal_places=2)
    by_vendor = {False: 0.0, True: 0.0}
    for model, expr in ((LaborLine, Sum(F("hours") * F("rate"), output_field=money)), (PartLine, Sum(F("quantity") * F("unit_cost"), output_field=money))):
        rows = (model.objects.filter(work_order__completed_on__gte=since, work_order__completed_on__lte=today)
                .order_by().values("work_order__vendor_service").annotate(v=expr))
        for row in rows:
            by_vendor[bool(row["work_order__vendor_service"])] += float(row["v"] or 0)
    in_house, vendor_tm = by_vendor[False] * ANNUALIZE, by_vendor[True] * ANNUALIZE
    contracts = float(Contract.objects.filter(end_on__gte=today).aggregate(s=Sum("annual_cost"))["s"] or 0)
    total = in_house + vendor_tm + contracts
    return {"in_house": in_house, "vendor_tm": vendor_tm, "contracts": contracts, "total": total, "acquisition": acquisition,
            "ratio_pct": total / acquisition * 100 if acquisition else 0.0}


def open_work_orders_count() -> int:
    return WorkOrder.objects.filter(status__in=OPEN_STATUSES).count()


# --- Overview screen ----------------------------------------------------------------------------
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


NAV_CONTRACT_DAYS = 30  # the nav badge counts contracts ending within 30 days, plus expired ones that still cover devices


def contracts_needing_attention(today: date | None = None):
    today = today or date.today()
    covered = Count("assets", filter=~Q(assets__status=AssetStatus.RETIRED))
    expired = Contract.objects.filter(end_on__lt=today).annotate(devices=covered).filter(devices__gt=0)
    ending = Contract.objects.filter(end_on__gte=today, end_on__lte=today + timedelta(days=NAV_CONTRACT_DAYS))
    return expired.count() + ending.count()


def nav_counts() -> dict:
    contracts = contracts_needing_attention()
    recalls = AlertMatch.objects.filter(status=AlertMatch.Status.NEEDS_ACTION).count()
    return {
        "equipment": Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).count(),
        "workorders": WorkOrder.objects.filter(status__in=OPEN_STATUSES).count(),
        "workorders_hot": unassigned_portal_requests().exists(),
        "contracts": contracts or None,  # no badge when nothing needs attention (matches the mock)
        "contracts_hot": bool(contracts),
        "recalls": recalls or None,  # badge only while something needs action (matches the mock)
        "recalls_hot": bool(recalls),
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
    """Everything the Overview screen shows for one month. Fleet state and attention items are always as of today; the targets are
    the tenant's, from Settings (apps.facility)."""
    today = today or date.today()
    k = overview_kpis(year, month, today)
    py, pm = _shift_month(year, month, -1)
    # No deltas against a month before the tenant has any history; they would compare against zeros.
    has_prev = WorkOrder.objects.filter(opened_on__lte=month_bounds(py, pm)[1]).exists()
    prev = overview_kpis(py, pm, today=today) if has_prev else None
    start, as_of = k["period"]["start"], k["period"]["as_of"]
    affected = Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, device_model__alert_matches__status=AlertMatch.Status.NEEDS_ACTION).distinct().count()
    return {
        "k": k, "prev": prev, "targets": kpi_targets(), "alert_devices_affected": affected,
        "buckets": fleet_bucket_counts(today),
        "pm_series": pm_on_time_series(year, month), "pm_series_life_support": pm_on_time_series(year, month, life_support_only=True),
        "attention": attention_items(today),
        "opened_by_type": work_orders_opened_by_type(year, month),
        "spend_by_category": repair_spend_by_category(start, as_of),
        "recent": recent_activity(start, as_of),
    }


# --- Reports screen (slice 7) ---------------------------------------------------------------------
# The mock's eight reports, in its order. The numbers for each live next door: cost.py (cosr, spend, contract),
# fleet.py (compliance, mtbf, replace), and operations.py (tech, recall). This catalog is what the screen, the CSV
# download, and the API iterate. Every report function takes `today` and returns a dict with "columns" and "rows"
# (its table as plain values: str, int, float, date, or None; what the CSV and the API serve) plus whatever its
# template needs. Chart geometry is presentation and lives in apps/web/reports.py.

REPORTS = [
    {"key": "cosr", "title": "Cost of service ratio by category", "subtitle": "Annualized service cost against acquisition value"},
    {"key": "compliance", "title": "PM compliance summary", "subtitle": "Survey-ready view by risk class (EC.02.04.03)"},
    {"key": "mtbf", "title": "Reliability by model", "subtitle": "Failures, mean time between failures, turnaround"},
    {"key": "replace", "title": "Replacement planning", "subtitle": "Devices scoring highest on age, failures, and condition"},
    {"key": "spend", "title": "Repair spend trend", "subtitle": "Labor and parts by month"},
    {"key": "contract", "title": "Contract vs in-house", "subtitle": "Where the service dollars go"},
    {"key": "tech", "title": "Technician productivity", "subtitle": "Work closed, hours, on-time rate, last 30 days"},
    {"key": "recall", "title": "Recall response log", "subtitle": "Every alert and how it was handled"},
]
for _r in REPORTS:
    _r["template"] = f"web/_report_{_r['key']}.html"
REPORT_KEYS = [r["key"] for r in REPORTS]


def report_meta(key: str) -> dict | None:
    return next((r for r in REPORTS if r["key"] == key), None)


def run_report(key: str, today: date | None = None) -> dict:
    """The data for one report as of `today`. Requires a tenant context, like everything else here."""
    from . import cost, fleet, operations  # here, not at the top: the report modules import helpers from this module

    functions = {"cosr": cost.report_cosr, "spend": cost.report_spend, "contract": cost.report_contract,
                 "compliance": fleet.report_compliance, "mtbf": fleet.report_mtbf, "replace": fleet.report_replace,
                 "tech": operations.report_tech, "recall": operations.report_recall}
    return functions[key](today or date.today())
