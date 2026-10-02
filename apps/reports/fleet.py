"""
Fleet reports (slice 7): PM compliance summary, reliability by model, replacement planning.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.
The math follows the mock's repContent; departures are noted inline. Everything here is read-only and tenant-scoped.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum

from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass
from apps.facility.services import compliance_targets
from apps.pm.dates import month_bounds
from apps.reports.services import TRAILING_DAYS
from apps.workorders.models import LABOR_AMOUNT, PART_AMOUNT, LaborLine, PartLine, WorkOrder, WoStatus, WoType

CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
MTBF_LIMIT = 12
REPAIR_RATE_FACTOR = 2  # the mock's x2: two 182-day halves make its year, the same base MTBF uses (n * 182 / repairs)
REPLACE_LIMIT = 12
REPLACEMENT_MARKUP = 1.05  # list price plus 5%, as the mock estimates


def _active_assets():
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES)


def _repairs_in_window(today: date):
    """Repair work orders opened in the trailing 182 days. A cancelled repair was not a failure, so it is left out."""
    since = today - timedelta(days=TRAILING_DAYS)
    return WorkOrder.objects.filter(type=WoType.REPAIR, opened_on__gte=since, opened_on__lte=today).exclude(status=WoStatus.CANCELLED), since


# --- PM compliance summary ---------------------------------------------------------------------------------------


def report_compliance(today: date) -> dict:
    """One row per risk class: active devices, this month's PMs (due, completed, on time), devices overdue now, and compliance
    (the share of active devices whose PM is not past due today) against the class's target. Two grouped queries, no per-class work.
    A device marked missing stays in the active count and is always overdue (the mock's rule: it can never be shown compliant), and
    this month's PMs are counted on active devices only, so every column describes the same fleet."""
    start, end = month_bounds(today.year, today.month)
    overdue_q = Q(next_pm_on__lt=today) | Q(status=AssetStatus.MISSING)
    fleet = {row["device_model__risk_class"]: row for row in
             _active_assets().order_by().values("device_model__risk_class").annotate(devices=Count("id"), overdue=Count("id", filter=overdue_q))}
    pms = {row["asset__device_model__risk_class"]: row for row in
           WorkOrder.objects.filter(type=WoType.PM, due_on__gte=start, due_on__lte=end, asset__status__in=Asset.ACTIVE_STATUSES)
           .exclude(status=WoStatus.CANCELLED).order_by().values("asset__device_model__risk_class")
           .annotate(due=Count("id"), completed=Count("id", filter=Q(completed_on__isnull=False)),
                     on_time=Count("id", filter=Q(completed_on__lte=F("due_on"))))}
    # Survey targets by risk class: life support and high at 100%, medium and low at the tenant's PM completion target (Settings).
    targets = compliance_targets()
    classes = []
    for rc in CLASS_ORDER:
        f, p = fleet.get(rc, {}), pms.get(rc, {})
        devices, overdue = f.get("devices", 0), f.get("overdue", 0)
        pct = (1 - overdue / devices) * 100 if devices else 100.0
        target = targets[rc]
        # Exact: 17 of 250 overdue is 93.2% exactly, which float division makes 93.19999... and would score as a miss.
        meets = Decimal((devices - overdue) * 100) >= Decimal(str(target)) * devices if devices else True
        classes.append({"key": rc.value, "label": rc.label, "devices": devices, "due": p.get("due", 0), "completed": p.get("completed", 0),
                        "on_time": p.get("on_time", 0), "overdue": overdue, "compliance_pct": float(pct), "target_pct": target, "meets": meets})
    return {
        "columns": ["Risk class", "Devices", "PMs due this month", "Completed", "On time", "Overdue now", "Current compliance %", "Target %"],
        "rows": [[c["label"], c["devices"], c["due"], c["completed"], c["on_time"], c["overdue"], c["compliance_pct"], c["target_pct"]] for c in classes],
        "classes": classes, "month_label": today.strftime("%B %Y"), "today": today, "policy_target_pct": targets[RiskClass.MEDIUM],
    }


# --- Reliability by model ----------------------------------------------------------------------------------------


def report_mtbf(today: date) -> dict:
    """Models with at least one repair opened in the trailing 182 days, ranked by repairs per device per year (the mock's x2 of the
    six-month count). MTBF is fleet-level: device-days in the period over repairs. Turnaround and cost average the repairs that have
    completed. Six queries whatever the fleet size: repairs and devices per model, a dates-only pass for turnaround, one grouped sum
    each over labor and part lines for cost, and the models; no work order is instantiated."""
    repairs, since = _repairs_in_window(today)
    counts = {row["asset__device_model"]: row["n"] for row in repairs.order_by().values("asset__device_model").annotate(n=Count("id"))}
    models = []
    if counts:
        in_service = {row["device_model"]: row["n"] for row in
                      _active_assets().filter(device_model__in=counts).order_by().values("device_model").annotate(n=Count("id"))}
        done = {}
        for row in repairs.filter(completed_on__isnull=False).order_by().values("asset__device_model", "opened_on", "completed_on"):
            d = done.setdefault(row["asset__device_model"], {"n": 0, "turnaround": 0, "cost": 0.0})
            d["n"] += 1
            d["turnaround"] += max((row["completed_on"] - row["opened_on"]).days, 0)  # the service refuses completion before opening; clamp anyway
        money = DecimalField(max_digits=14, decimal_places=2)
        # The same repairs as `done` (type, window, not cancelled, completed), reached from the lines so no work order is instantiated.
        line_filter = {"work_order__type": WoType.REPAIR, "work_order__opened_on__gte": since, "work_order__opened_on__lte": today,
                       "work_order__completed_on__isnull": False}
        for model, expr in ((LaborLine, Sum(LABOR_AMOUNT, output_field=money)), (PartLine, Sum(PART_AMOUNT, output_field=money))):
            rows = (model.objects.filter(**line_filter).exclude(work_order__status=WoStatus.CANCELLED)
                    .order_by().values("work_order__asset__device_model").annotate(v=expr))
            for row in rows:
                if row["work_order__asset__device_model"] in done:  # always true (same filter as `done`); guard the lookup anyway
                    done[row["work_order__asset__device_model"]]["cost"] += float(row["v"] or 0)
        for dm in DeviceModel.objects.filter(pk__in=counts):
            n, reps, d = in_service.get(dm.pk, 0), counts[dm.pk], done.get(dm.pk)
            rate = reps / n * REPAIR_RATE_FACTOR if n else None
            models.append({"device_model": dm, "in_service": n, "repairs": reps, "rate": rate, "mtbf_days": n * TRAILING_DAYS / reps if n else None,
                           "turnaround": d["turnaround"] / d["n"] if d else 0.0, "cost": d["cost"] / d["n"] if d else 0.0,
                           "flagged": rate is not None and rate > 1})
        # Highest failure rate first; models with nothing in service (a repair on a since-retired device) have no rate and go last.
        models.sort(key=lambda m: (m["rate"] is None, -(m["rate"] or 0.0), -m["repairs"], m["device_model"].manufacturer, m["device_model"].model))
    # The screen shows the top MTBF_LIMIT; the CSV (`rows`) carries every model with repairs, like the replacement list.
    return {
        "columns": ["Manufacturer", "Model", "Device", "In service", "Repairs, 6 mo", "Repairs per device per year", "MTBF days", "Avg turnaround days",
                    "Avg repair cost"],
        "rows": [[m["device_model"].manufacturer, m["device_model"].model, m["device_model"].description, m["in_service"], m["repairs"], m["rate"],
                  round(m["mtbf_days"]) if m["mtbf_days"] is not None else None, m["turnaround"], m["cost"]] for m in models],
        "models": models[:MTBF_LIMIT], "total_models": len(models), "since": since,
    }


# --- Replacement planning ----------------------------------------------------------------------------------------


def replacement_score(age: float | None, life: int, repairs: int, condition: int) -> float:
    """The mock's score: age against expected life (50%), repairs in the last six months (30%), condition (20%). 0 to 1.
    A device installed in the future (a typo, or a record dated ahead) has no age yet: its age term is 0, never negative."""
    age_term = min(max(age or 0.0, 0.0) / life, 1.5) / 1.5
    return 0.5 * age_term + 0.3 * min(repairs / 3, 1) + 0.2 * (5 - condition) / 4


def report_replace(today: date) -> dict:
    """Every active device scored for replacement; `rows` carries the full ranked list (the capital request) and `top` the twelve
    the screen shows. One grouped query for repairs, one for the fleet."""
    repairs, _ = _repairs_in_window(today)
    reps = {row["asset"]: row["n"] for row in repairs.order_by().values("asset").annotate(n=Count("id"))}
    scored = []
    for a in _active_assets().select_related("device_model", "department"):
        age = a.age_years(today)
        if age is not None:
            age = max(age, 0.0)  # a future installed_on reads as 0.0 yr on the screen and in the CSV, not a negative age
        life = a.device_model.expected_life_years or 1  # a zero life would divide by zero; treat it as one year
        n = reps.get(a.pk, 0)
        condition = min(5, max(1, a.condition or 1))
        score = replacement_score(age, life, n, condition)
        list_cost = float(a.device_model.list_cost or 0)
        estimate = (list_cost or float(a.acquisition_cost or 0)) * REPLACEMENT_MARKUP
        scored.append({"asset": a, "age": age, "life": life, "repairs": n, "condition": condition, "score": score, "score_pct": round(score * 100),
                       "estimate": estimate, "over_life": age is not None and age > life, "fallback": not list_cost})
    scored.sort(key=lambda s: (-s["score"], s["asset"].tag))
    top = scored[:REPLACE_LIMIT]
    return {
        "columns": ["Asset tag", "Device", "Manufacturer", "Model", "Location", "Age years", "Expected life years", "Repairs, 6 mo", "Condition", "Score",
                    "Est. replacement"],
        "rows": [[s["asset"].tag, s["asset"].device_model.description, s["asset"].device_model.manufacturer, s["asset"].device_model.model,
                  s["asset"].department.name, s["age"], s["life"], s["repairs"], s["condition"], s["score_pct"], s["estimate"]] for s in scored],
        "top": top, "total": sum(s["estimate"] for s in top), "scored_count": len(scored), "fallback_count": sum(1 for s in top if s["fallback"]),
    }
