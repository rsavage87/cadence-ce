"""The PM schedule's lower panels (slice 9): next 30 days, technician workload for the next 7 days, and the PM library.

`panels_context(request, today)` returns everything `_pm_panels.html` needs. Presentation only: the numbers come from
apps.pm.schedule; this module adds the chart geometry and the display values (bar widths) the template cannot compute.
The page includes the panels once, and they re-fetch themselves (HX-Target `pm-panels`) when the page fires `pm-changed`.
"""
from apps.pm import schedule as sch

from . import charts


def _outlook(today) -> dict:
    n = sch.next_30_days(today)
    chart = charts.hbars(n["by_category"], fmt=lambda v: f"{v:.0f}") if n["by_category"] else None
    return {**n, "chart": chart}


def _workload(today) -> list[dict]:
    # One row per active technician, as the mock: the bar's width is the share of weekly capacity, capped at 100%.
    return [{"technician": ld.technician, "total": ld.total, "capacity": ld.capacity, "over": ld.over, "width": f"{ld.pct:.1f}",
             "pm_count": ld.pm_count, "pm_hours": ld.pm_hours, "repair_hours": ld.repair_hours} for ld in sch.workload_next_7_days(today)]


def _library() -> dict:
    rows = sch.pm_library()
    return {"rows": rows, "models": len(rows), "with_procedure": sum(1 for r in rows if r["procedure"] is not None)}


def panels_context(request, today) -> dict:
    return {"outlook": _outlook(today), "workload": _workload(today), "library": _library()}
