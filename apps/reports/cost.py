"""
Cost reports (slice 7): cost of service ratio by category, repair spend trend, contract vs in-house.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.

All three share the Overview's definition of service cost (services.cost_of_service): work orders completed in the
trailing 182 days annualized, plus the annual cost of contracts that have not ended. The mock modeled contract cost
as 7% (OEM) and 4% (third-party) of acquisition value; here it is the real annual_cost of the contract records, so the
category and support-model breakdowns add up to the fleet-wide total exactly.

Everything aggregates in the database (one query per line type or grouping) and finishes in Python, so a large fleet
renders without touching each work order.
"""
from datetime import date, timedelta

from django.db.models import Count, DecimalField, F, Sum
from django.db.models.functions import TruncMonth

from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, SupportType
from apps.pm.dates import month_bounds
from apps.reports.services import ANNUALIZE, TRAILING_DAYS, _shift_month, cost_of_service
from apps.workorders.models import LaborLine, PartLine, WoType

BENCHMARK_PCT = 6.0  # the mock's red marker: midpoint of the 5 to 7% cost-of-service benchmark
SPEND_MONTHS = 6

_MONEY = DecimalField(max_digits=14, decimal_places=2)
_LABOR = Sum(F("hours") * F("rate"), output_field=_MONEY)
_PARTS = Sum(F("quantity") * F("unit_cost"), output_field=_MONEY)


def _completed_cost_by(group: str, today: date, since: date, wo_type: str | None = None) -> dict:
    """Labor plus parts of work orders completed in [since, today], summed per value of `group` (a lookup from the line's
    work order, e.g. "asset__device_model__category"). Two queries, one per line type, whatever the fleet size."""
    totals: dict = {}
    for model, expr in ((LaborLine, _LABOR), (PartLine, _PARTS)):
        qs = model.objects.filter(work_order__completed_on__gte=since, work_order__completed_on__lte=today)
        if wo_type:
            qs = qs.filter(work_order__type=wo_type)
        key = f"work_order__{group}"
        for row in qs.order_by().values(key).annotate(v=expr):
            totals[row[key]] = totals.get(row[key], 0.0) + float(row["v"] or 0)
    return totals


def _active_by(group: str) -> dict:
    """{group value: (devices, acquisition value)} over the active fleet."""
    rows = Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).order_by().values(group).annotate(n=Count("id"), acq=Sum("acquisition_cost"))
    return {row[group]: (row["n"], float(row["acq"] or 0)) for row in rows}


# --- cosr: cost of service ratio by category --------------------------------------------------------

def _contract_cost_by_category(today: date) -> tuple[dict, float]:
    """Each live contract's annual cost split across its covered active devices by acquisition cost (the rule behind
    Contract.cost_share_for), rolled up by category. Money on contracts that cover no active devices, or only devices
    with no acquisition cost, cannot be attributed to a category and comes back as the second value."""
    annual = {c.id: float(c.annual_cost) for c in Contract.objects.filter(end_on__gte=today).only("id", "annual_cost")}
    covered = (Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, contract_id__in=list(annual)).order_by()
               .values("contract_id", "device_model__category").annotate(acq=Sum("acquisition_cost")))
    by_contract: dict = {}
    for row in covered:
        by_contract.setdefault(row["contract_id"], []).append((row["device_model__category"], float(row["acq"] or 0)))
    by_category: dict = {}
    unallocated = 0.0
    for cid, cost in annual.items():
        shares = by_contract.get(cid, [])
        total = sum(acq for _, acq in shares)
        if not total:
            unallocated += cost
            continue
        for category, acq in shares:
            by_category[category] = by_category.get(category, 0.0) + cost * acq / total
    return by_category, unallocated


def report_cosr(today: date) -> dict:
    since = today - timedelta(days=TRAILING_DAYS)
    fleet_by_category = _active_by("device_model__category")
    wo_cost = _completed_cost_by("asset__device_model__category", today, since)
    contract_cost, unallocated = _contract_cost_by_category(today)
    # Work on devices whose category has no active device left (all retired) is real spend with no bar to sit on.
    unallocated += sum(v for c, v in wo_cost.items() if c not in fleet_by_category) * ANNUALIZE

    categories = []
    for category, (devices, acquisition) in fleet_by_category.items():
        service = wo_cost.get(category, 0.0) * ANNUALIZE + contract_cost.get(category, 0.0)
        categories.append({"label": category, "devices": devices, "acquisition": acquisition, "service": service,
                           "ratio_pct": service / acquisition * 100 if acquisition else 0.0})
    categories.sort(key=lambda c: (-c["ratio_pct"], c["label"]))
    fleet = cost_of_service(today, sum(c["acquisition"] for c in categories))
    return {
        "columns": ["Category", "Devices", "Acquisition value", "Annual service cost", "Ratio %"],
        "rows": [[c["label"], c["devices"], c["acquisition"], c["service"], round(c["ratio_pct"], 1)] for c in categories],
        "categories": categories, "fleet": fleet, "unallocated": unallocated, "benchmark": BENCHMARK_PCT,
    }


# --- spend: repair spend trend ------------------------------------------------------------------------

def report_spend(today: date) -> dict:
    points = [_shift_month(today.year, today.month, -i) for i in reversed(range(SPEND_MONTHS))]
    first, _ = month_bounds(*points[0])
    _, last = month_bounds(*points[-1])
    totals: dict = {}
    for name, model, expr in (("labor", LaborLine, _LABOR), ("parts", PartLine, _PARTS)):
        qs = (model.objects.filter(work_order__type=WoType.REPAIR, work_order__completed_on__gte=first, work_order__completed_on__lte=last)
              .annotate(mo=TruncMonth("work_order__completed_on")).order_by().values("mo").annotate(v=expr))
        for row in qs:
            totals[(row["mo"].year, row["mo"].month, name)] = float(row["v"] or 0)
    months = []
    for y, m in points:
        labor, parts = totals.get((y, m, "labor"), 0.0), totals.get((y, m, "parts"), 0.0)
        months.append({"year": y, "month": m, "label": f"{date(y, m, 1):%b %Y}", "labor": labor, "parts": parts, "total": labor + parts})
    six_months = sum(x["total"] for x in months)
    return {
        "columns": ["Month", "Labor", "Parts", "Total"],
        "rows": [[x["label"], x["labor"], x["parts"], x["total"]] for x in months],
        "months": months, "month_to_date": months[-1]["total"], "six_months": six_months, "monthly_average": six_months / SPEND_MONTHS,
    }


# --- contract: contract vs in-house -------------------------------------------------------------------

SUPPORT_ORDER = [SupportType.IN_HOUSE, SupportType.OEM_CONTRACT, SupportType.THIRD_PARTY]
SUPPORT_CONTRACT_TYPE = {SupportType.OEM_CONTRACT: ContractType.OEM, SupportType.THIRD_PARTY: ContractType.THIRD_PARTY}


def report_contract(today: date) -> dict:
    since = today - timedelta(days=TRAILING_DAYS)
    fleet_by_support = _active_by("support_type")
    wo_cost = _completed_cost_by("asset__support_type", today, since)
    live = Contract.objects.filter(end_on__gte=today).order_by().values("type").annotate(v=Sum("annual_cost"))
    contract_cost = {row["type"]: float(row["v"] or 0) for row in live}
    support = []
    for st in SUPPORT_ORDER:
        devices, acquisition = fleet_by_support.get(st, (0, 0.0))
        ctype = SUPPORT_CONTRACT_TYPE.get(st)
        annual = wo_cost.get(st, 0.0) * ANNUALIZE + (contract_cost.get(ctype, 0.0) if ctype else 0.0)
        support.append({"key": st.value, "label": st.label, "devices": devices, "acquisition": acquisition, "annual": annual,
                        "ratio_pct": annual / acquisition * 100 if acquisition else 0.0})
    fleet = cost_of_service(today, sum(s["acquisition"] for s in support))
    return {
        "columns": ["Support model", "Devices", "Acquisition value", "Annual cost", "Ratio %"],
        "rows": [[s["label"], s["devices"], s["acquisition"], s["annual"], round(s["ratio_pct"], 1)] for s in support],
        "support": support, "fleet": fleet,
    }
