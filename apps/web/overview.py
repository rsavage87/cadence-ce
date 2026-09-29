"""Presentation for the Overview screen: KPI tiles, the fleet strip, and chart geometry. The numbers come from reports.services.overview_page."""
import math
from datetime import date

from django.urls import reverse

from apps.equipment.models import RiskClass
from apps.equipment.services import FleetBucket

from . import charts
from .templatetags.web import money_k

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Strip order and colors from the mock's BUCKETS. Retired devices are listed under the strip, not in it.
STRIP = [(FleetBucket.COMPLIANT, "var(--ok)"), (FleetBucket.PM_DUE, "var(--warn)"), (FleetBucket.PM_OVERDUE, "var(--crit)"),
         (FleetBucket.OPEN_RECALL, "var(--info)"), (FleetBucket.IN_REPAIR, "var(--violet)"), (FleetBucket.OUT_OF_SERVICE, "var(--ink)")]


def fleet_strip(buckets: dict) -> dict:
    active = sum(buckets[b] for b, _ in STRIP)
    url = reverse("web:equipment")
    segments = [{"key": b.value, "label": b.label, "color": color, "n": buckets[b], "pct": buckets[b] / active * 100 if active else 0,
                 "url": f"{url}?bucket={b.value}"} for b, color in STRIP]
    return {"active": active, "segments": segments, "retired": buckets[FleetBucket.RETIRED], "retired_url": f"{url}?bucket={FleetBucket.RETIRED.value}"}


def _delta(cur, prev, good_up: bool, fmt, unit=""):
    """The mock's ovDelta: (text, css class) or None when there is no prior month to compare."""
    if prev is None:
        return None
    d = cur - prev
    if abs(d) < 0.05:
        return ("no change vs prior month", "muted")
    good = d > 0 if good_up else d < 0
    return (f"{'▲' if d > 0 else '▼'} {fmt(abs(d))}{unit} vs prior month", "up" if good else "down")


def pct_label(v: float) -> str:
    """A target percentage without a trailing .0: 95.0 -> "95", 97.5 -> "97.5", 99.5 -> "99.5"."""
    return f"{round(float(v), 2):g}"


def _parts(*parts):
    return [p if isinstance(p, tuple) else (p, "") for p in parts if p]


def kpi_tiles(data: dict, recalls_url: str | None = None, pm_url: str | None = None) -> list[dict]:
    """`recalls_url` and `pm_url` are None for users without View on those modules; a tile without a url renders static."""
    k, p, t = data["k"], data["prev"], data["targets"]
    cur = k["period"]["current"]
    pv = (lambda f: f(p)) if p else (lambda f: None)
    eq, wo = reverse("web:equipment"), reverse("web:workorders")

    def report(key):  # the Overview needs reports View, so these links always work for whoever sees the tiles
        return reverse("web:report", args=[key])

    return [
        {"label": "PM completion on time", "value": f"{k['pm_on_time']['rate']:.1f}", "unit": "%", **({"url": pm_url} if pm_url else {}),
         "parts": _parts(_delta(k["pm_on_time"]["rate"], pv(lambda p: p["pm_on_time"]["rate"]), True, lambda v: f"{v:.1f}", " pts"),
                         f"{k['pm_on_time']['on_time']} of {k['pm_on_time']['due']} PMs due {'so far this month' if cur else 'in month'}",
                         f"target {pct_label(t['pm_on_time'])}%")},
        {"label": "Life-support PM completion", "value": f"{k['pm_on_time_life_support']['rate']:.1f}", "unit": "%",
         "url": f"{eq}?bucket={FleetBucket.PM_OVERDUE.value}&risk={RiskClass.LIFE_SUPPORT.value}",
         "parts": _parts(_delta(k["pm_on_time_life_support"]["rate"], pv(lambda p: p["pm_on_time_life_support"]["rate"]), True, lambda v: f"{v:.1f}", " pts"),
                         f"{k['pm_on_time_life_support']['on_time']} of {k['pm_on_time_life_support']['due']} due",
                         f"target {pct_label(t['pm_on_time_life_support'])}%")},
        {"label": "Fleet uptime", "value": f"{k['uptime_pct']:.2f}", "unit": "%", "url": report("mtbf"),
         "parts": _parts(_delta(k["uptime_pct"], pv(lambda p: p["uptime_pct"]), True, lambda v: f"{v:.2f}", " pts"),
                         f"{k['downtime_days']} device-days down of {k['active_devices'] * k['period']['days']:,}", f"target {pct_label(t['uptime_pct'])}%")},
        {"label": "Open work orders" if cur else "Open work orders at month end", "value": str(k["open_work_orders"]), "url": wo,
         "parts": _parts(_delta(k["open_work_orders"], pv(lambda p: p["open_work_orders"]), False, lambda v: str(round(v))),
                         f"{k['overdue_work_orders']} past due", f"{k['awaiting_parts']} awaiting parts")},
        {"label": "Mean time to repair", "value": f"{k['mttr_days']:.1f}", "unit": "days", "url": report("mtbf"),
         "parts": _parts(_delta(k["mttr_days"], pv(lambda p: p["mttr_days"]), False, lambda v: f"{v:.1f}", " d"),
                         f"{k['repairs_closed']} repairs closed", f"target {t['mttr_days']:.1f}")},
        {"label": "Repair spend, month to date" if cur else "Repair spend", "value": money_k(k["repair_spend"]), "url": report("spend"),
         "parts": _parts(_delta(k["repair_spend"], pv(lambda p: p["repair_spend"]), False, money_k), "labor and parts",
                         f"budget {money_k(t['repair_budget_monthly'])} per month" if t.get("repair_budget_monthly") is not None else None)},
        {"label": "Cost of service ratio, annualized", "value": f"{k['cost_of_service']['ratio_pct']:.1f}", "unit": "%", "url": report("cosr"),
         "parts": _parts("trailing 6 months", f"of {money_k(k['cost_of_service']['acquisition'])} acquisition value", "benchmark 5 to 7%")},
        {"label": "Recall alerts received", "value": str(k["alerts"]["received"]), **({"url": recalls_url} if recalls_url else {}),
         "parts": _parts(f"{k['alerts']['open']} open today", f"{k['alerts']['needs_action']} need action",
                         f"{data['alert_devices_affected']} devices affected")},
    ]


def _month_label(y, m, first):
    return MONTHS[m - 1] + (f" '{y % 100:02d}" if first or m == 1 else "")


def pm_trend_chart(all_series: list[dict], ls_series: list[dict], target: float = 95.0) -> dict:
    """12-month PM on-time rate against the PM completion target (Settings). Months with no PMs due are gaps, not 100%."""
    labels = [_month_label(r["year"], r["month"], i == 0) for i, r in enumerate(all_series)]
    values = [r["rate"] if r["due"] else None for r in all_series]
    ls_values = [r["rate"] if r["due"] else None for r in ls_series]
    present = [v for v in values + ls_values if v is not None]
    # The mock fixes the axis at 90 to 100; widen it in 5-point steps if a month falls below, or so the target line sits
    # above the floor rather than on it (a target of 90 gives 85; the lowest target Settings allows, 50, gives 45).
    y_min = min(90.0, (min(present) // 5) * 5 if present else 90.0, math.ceil(target / 5) * 5 - 5.0)
    return charts.line(labels, [{"name": "All devices", "color": "var(--accent)", "dash": False, "values": values},
                                {"name": "Life support", "color": "var(--crit)", "dash": True, "values": ls_values}],
                       y_min=y_min, y_max=100, fmt=lambda v: f"{v:.0f}%", target=target, target_label=f"Target {pct_label(target)}%")


def opened_by_type_chart(opened: dict) -> dict:
    colors = {"pm": "var(--accent)", "repair": "var(--warn)", "other": "var(--line-2)"}
    return charts.stacked_bars([MONTHS[m - 1] for _, m in opened["months"]], [{**s, "color": colors[s["key"]]} for s in opened["series"]])


def spend_chart(spend: list[tuple[str, float]]) -> dict | None:
    return charts.hbars(spend, fmt=money_k) if spend else None


def month_options(today: date, first_year: int) -> dict:
    return {"months": list(enumerate(MONTHS, start=1)), "years": list(range(min(first_year, today.year), today.year + 1))}
