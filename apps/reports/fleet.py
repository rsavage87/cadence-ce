"""
Fleet reports (slice 7): PM compliance summary, reliability by model, replacement planning.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.
The math follows the mock's repContent; departures are noted inline. Everything here is read-only and tenant-scoped.
"""
from datetime import date, timedelta

from django.db.models import Count, F, Q

from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.pm.dates import month_bounds
from apps.reports.services import ANNUALIZE, TRAILING_DAYS
from apps.workorders.models import WorkOrder, WoStatus, WoType

# Survey targets by risk class (the mock: life support and high at 100%, the rest at the hospital policy's 95%).
COMPLIANCE_TARGETS = {RiskClass.LIFE_SUPPORT: 100, RiskClass.HIGH: 100, RiskClass.MEDIUM: 95, RiskClass.LOW: 95}
CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
MTBF_LIMIT = 12
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
    (the share of active devices whose PM is not past due today) against the class's target. Two grouped queries, no per-class work."""
    start, end = month_bounds(today.year, today.month)
    fleet = {row["device_model__risk_class"]: row for row in
             _active_assets().order_by().values("device_model__risk_class").annotate(devices=Count("id"), overdue=Count("id", filter=Q(next_pm_on__lt=today)))}
    pms = {row["asset__device_model__risk_class"]: row for row in
           WorkOrder.objects.filter(type=WoType.PM, due_on__gte=start, due_on__lte=end).exclude(status=WoStatus.CANCELLED)
           .order_by().values("asset__device_model__risk_class")
           .annotate(due=Count("id"), completed=Count("id", filter=Q(completed_on__isnull=False)),
                     on_time=Count("id", filter=Q(completed_on__lte=F("due_on"))))}
    classes = []
    for rc in CLASS_ORDER:
        f, p = fleet.get(rc, {}), pms.get(rc, {})
        devices, overdue = f.get("devices", 0), f.get("overdue", 0)
        pct = (1 - overdue / devices) * 100 if devices else 100.0
        target = COMPLIANCE_TARGETS[rc]
        classes.append({"key": rc.value, "label": rc.label, "devices": devices, "due": p.get("due", 0), "completed": p.get("completed", 0),
                        "on_time": p.get("on_time", 0), "overdue": overdue, "compliance_pct": float(pct), "target_pct": target, "meets": pct >= target})
    return {
        "columns": ["Risk class", "Devices", "PMs due this month", "Completed", "On time", "Overdue now", "Current compliance %", "Target %"],
        "rows": [[c["label"], c["devices"], c["due"], c["completed"], c["on_time"], c["overdue"], c["compliance_pct"], c["target_pct"]] for c in classes],
        "classes": classes, "month_label": today.strftime("%B %Y"), "today": today,
    }


# --- Reliability by model ----------------------------------------------------------------------------------------


def report_mtbf(today: date) -> dict:
    """Models with at least one repair opened in the trailing 182 days, ranked by repairs per device per year. MTBF is fleet-level:
    device-days in the period over repairs. Turnaround and cost average the repairs that have completed."""
    repairs, since = _repairs_in_window(today)
    counts = {row["asset__device_model"]: row["n"] for row in repairs.order_by().values("asset__device_model").annotate(n=Count("id"))}
    models = []
    if counts:
        in_service = {row["device_model"]: row["n"] for row in
                      _active_assets().filter(device_model__in=counts).order_by().values("device_model").annotate(n=Count("id"))}
        done = {}
        for w in repairs.filter(completed_on__isnull=False).annotate(dm_id=F("asset__device_model")).prefetch_related("labor_lines", "part_lines"):
            d = done.setdefault(w.dm_id, {"n": 0, "turnaround": 0, "cost": 0.0})
            d["n"] += 1
            d["turnaround"] += w.turnaround_days
            d["cost"] += w.total_cost()
        for dm in DeviceModel.objects.filter(pk__in=counts):
            n, reps, d = in_service.get(dm.pk, 0), counts[dm.pk], done.get(dm.pk)
            rate = reps / n * ANNUALIZE if n else None
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
    """The mock's score: age against expected life (50%), repairs in the last six months (30%), condition (20%). 0 to 1."""
    age_term = min((age or 0.0) / life, 1.5) / 1.5
    return 0.5 * age_term + 0.3 * min(repairs / 3, 1) + 0.2 * (5 - condition) / 4


def report_replace(today: date) -> dict:
    """Every active device scored for replacement; `rows` carries the full ranked list (the capital request) and `top` the twelve
    the screen shows. One grouped query for repairs, one for the fleet."""
    repairs, _ = _repairs_in_window(today)
    reps = {row["asset"]: row["n"] for row in repairs.order_by().values("asset").annotate(n=Count("id"))}
    scored = []
    for a in _active_assets().select_related("device_model", "department"):
        age = a.age_years(today)
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
