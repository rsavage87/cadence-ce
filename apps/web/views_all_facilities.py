"""
All facilities (slice 22): the Overview's figures for every facility the signed-in person has joined, side by side, with totals.
Reached from the facility menu ("All facilities") while working in any one of them. Each facility's figures are read inside that
facility (its own today, time zone, and rows) with the person's account there, which must give that facility's Overview; one that
does not is listed by name with no figures (apps.reports.all_facilities has the rules and the numbers; this module only words them).

Each facility's name is a switch button (POST to the facility switch, opening its Overview); the current one is marked and links to
this facility's Overview. A desktop shows a table with a totals row; a phone shows one card per facility (cadence.css, `.allf`).

Slice 27: when any facility shown counts PMs by a window other than the due date (apps.pm.windows), each facility's PM cells say its
window, and how many PMs due so far are still inside it; the totals' say "each facility judged by its own policy" when the windows
differ. When every facility counts by the due date, nothing is added.
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


EACH_OWN_POLICY = "each facility judged by its own policy"


def _pm_sub(on_time: int, due: int, pending: int) -> str:
    """ "4 of 5 due", and (slice 27) the PMs still inside their window: "4 of 5 due · 2 inside their window"."""
    text = f"{_n(on_time)} of {_n(due)} due"
    return f"{text} · {_n(pending)} inside {'its' if pending == 1 else 'their'} window" if pending else text


def windows_of(f: dict, *, show: bool) -> tuple[str, str]:
    """The window lines under a row's PM on time and Life-support PM cells (slice 27): the facility's window in words, or, on the
    totals, the shared window or EACH_OWN_POLICY when they differ. Blank unless `show` (some facility uses a window other than the
    due date)."""
    if not show:
        return "", ""
    if f.get("pm_windows_differ"):
        return EACH_OWN_POLICY, EACH_OWN_POLICY
    words = f["pm_window"]
    return words["words"], words["high"]


def cells(f: dict, windows: tuple[str, str] = ("", "")) -> list[dict]:
    """One facility's figures, or the totals, as the table's cells: the column, the value, and the line under it (the Overview
    tiles' wording); slice 27, `windows` (windows_of): a further line under the two PM cells, the window they are counted by."""
    repairs = f["repairs_closed"]
    values = [
        (_n(f["active_devices"]), ""),
        (_n(f["open_work_orders"]), f"{_n(f['overdue_work_orders'])} past due · {_n(f['awaiting_parts'])} awaiting parts"),
        (f"{f['pm_rate']:.1f}%", _pm_sub(f["pm_on_time"], f["pm_due"], f.get("pm_pending", 0))),
        (f"{f['life_support_pm_rate']:.1f}%",
         _pm_sub(f["life_support_pm_on_time"], f["life_support_pm_due"], f.get("life_support_pm_pending", 0))),
        (f"{f['uptime_pct']:.2f}%", f"{_n(f['downtime_days'])} of {_n(f['device_days'])} device-days down"),
        (f"{f['mttr_days']:.1f} d", f"{_n(repairs)} repair{'' if repairs == 1 else 's'} closed"),
        (money_k(f["repair_spend"]), "labor and parts"),
        (f"{f['cost_of_service_ratio_pct']:.1f}%", f"{money_k(f['cost_of_service'])} a year of {money_k(f['acquisition_value'])}"),
        (_n(f["alerts_needing_action"]), ""),
    ]
    window = dict(zip(("PM on time", "Life-support PM"), windows, strict=True))
    return [{"label": label, "value": value, "sub": sub, "window": window.get(label, "")}
            for label, (value, sub) in zip(COLUMNS, values, strict=True)]


@web_view(scoped=True)  # shows this facility only through the person's account here, as every other one: see the module docstring
def all_facilities(request):
    data = all_facilities_data(request.user, request.tenant)
    shown = [r for r in data["facilities"] if r["overview"]]
    months = sorted({r["month"] for r in shown})
    show = any(not r["pm_window"]["default"] for r in shown)  # slice 27: some facility counts PMs by another window
    rows = [{"account": r["account"], "name": r["name"], "current": r["current"], "month": r.get("month"),
             "cells": cells(r, windows_of(r, show=show)) if r["overview"] else None} for r in data["facilities"]]
    t = data["totals"]
    return render(request, "web/all_facilities.html", {
        "nav_active": "overview", "all_facilities_page": True, "columns": COLUMNS, "rows": rows,
        # the totals row only adds something with two facilities or more
        "totals": ({"facilities": t["facilities"], "cells": cells(t, windows_of({**t, "pm_window": shown[0]["pm_window"]}, show=show))}
                   if t and t["facilities"] > 1 else None),
        "month": months[0] if len(months) == 1 else None, "months_differ": len(months) > 1,
        "switchable": any(not r["current"] for r in rows), "pm_windows_differ": bool(t and t["pm_windows_differ"]),
    })
