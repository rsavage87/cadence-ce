"""
All facilities (slice 22): the Overview's figures for every facility the signed-in person has joined, side by side, with totals.
Reached from the facility menu ("All facilities") while working in any one of them. Each facility's figures are read inside that
facility (its own today, time zone, and rows) with the person's account there, which must give that facility's Overview; one that
does not is listed by name with no figures (apps.reports.all_facilities has the rules and the numbers; this module only words them).

Each facility's name is a switch button (POST to the facility switch, opening its Overview); the current one is marked and links to
this facility's Overview. A desktop shows a table with a totals row; a phone shows one card per facility (cadence.css, `.allf`).
"""
from django.shortcuts import render

from apps.reports.all_facilities import all_facilities as all_facilities_data

from .decorators import web_view
from .templatetags.web import money_k

# The table's columns after the facility's name; `cells` gives a row's (or the totals') values in this order.
COLUMNS = ("Active devices", "Open work orders", "PM on time", "Life-support PM", "Fleet uptime", "Mean time to repair", "Repair spend",
           "Cost of service ratio", "Recall alerts needing action")


def _n(v) -> str:
    return f"{v:,}"


def cells(f: dict) -> list[dict]:
    """One facility's figures, or the totals, as the table's cells: the column, the value, and the line under it (the Overview
    tiles' wording)."""
    repairs = f["repairs_closed"]
    values = [
        (_n(f["active_devices"]), ""),
        (_n(f["open_work_orders"]), f"{_n(f['overdue_work_orders'])} past due · {_n(f['awaiting_parts'])} awaiting parts"),
        (f"{f['pm_rate']:.1f}%", f"{_n(f['pm_on_time'])} of {_n(f['pm_due'])} due"),
        (f"{f['life_support_pm_rate']:.1f}%", f"{_n(f['life_support_pm_on_time'])} of {_n(f['life_support_pm_due'])} due"),
        (f"{f['uptime_pct']:.2f}%", f"{_n(f['downtime_days'])} of {_n(f['device_days'])} device-days down"),
        (f"{f['mttr_days']:.1f} d", f"{_n(repairs)} repair{'' if repairs == 1 else 's'} closed"),
        (money_k(f["repair_spend"]), "labor and parts"),
        (f"{f['cost_of_service_ratio_pct']:.1f}%", f"{money_k(f['cost_of_service'])} a year of {money_k(f['acquisition_value'])}"),
        (_n(f["alerts_needing_action"]), ""),
    ]
    return [{"label": label, "value": value, "sub": sub} for label, (value, sub) in zip(COLUMNS, values, strict=True)]


@web_view(scoped=True)  # shows this facility only through the person's account here, as every other one: see the module docstring
def all_facilities(request):
    data = all_facilities_data(request.user, request.tenant)
    shown = [r for r in data["facilities"] if r["overview"]]
    months = sorted({r["month"] for r in shown})
    rows = [{"account": r["account"], "name": r["name"], "current": r["current"], "month": r.get("month"),
             "cells": cells(r) if r["overview"] else None} for r in data["facilities"]]
    t = data["totals"]
    return render(request, "web/all_facilities.html", {
        "nav_active": "overview", "all_facilities_page": True, "columns": COLUMNS, "rows": rows,
        # the totals row only adds something with two facilities or more
        "totals": {"facilities": t["facilities"], "cells": cells(t)} if t and t["facilities"] > 1 else None,
        "month": months[0] if len(months) == 1 else None, "months_differ": len(months) > 1,
        "switchable": any(not r["current"] for r in rows),
    })
