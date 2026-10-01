"""
PM route sheets and report PDFs (slice 11): printable pages on web/print_base.html, opened in a new tab.

Route sheets (PM View) put the PM schedule's plan on paper, one sheet per person doing the work. The day is the one the PM
screen's day panel shows for the same address (?day=, else today in the current month or the 1st of another ?y=&m= month),
and the sheets print the devices that panel shows
(apps.pm.schedule.day_plan), and a past day prints its overdue devices, marked overdue. ?scope=week prints the week plan instead
(today through today + 6, apps.pm.schedule.week_plan), a technician's days on one sheet. A device goes to:
  1. the technician holding its open PM work order;
  2. "Vendor service" when that work order is with the vendor;
  3. otherwise the technician the schedule suggests (credentialed, least loaded);
  4. otherwise "No credentialed technician".
An open PM work order that is unassigned, or held by a deactivated technician, is on nobody's plate yet (the week plan and the
workload panel read it the same way), so its device goes to the suggested technician and the sheet shows who holds the work order.

Report PDFs (Reports View) lay out one report of the Reports screen for paper: the same numbers, presentation, and partial, with the
app's stylesheet for the charts' colours. "Save as PDF" in the browser's print dialog makes the PDF.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import prefetch_related_objects
from django.http import Http404
from django.shortcuts import render

from apps.accounts.models import Level, Module
from apps.equipment.models import RiskClass
from apps.pm import permissions as pm_perms
from apps.pm import schedule as sch
from apps.reports import services as rs
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

from . import views_pm, views_reports
from .decorators import web_view
from .reports import present

# Sheet keys sort technicians by name, then the vendor's sheet, then the devices nobody can take.
TECH, VENDOR, NOBODY = 0, 1, 2
VENDOR_KEY = (VENDOR, "Vendor service", 0)
NOBODY_KEY = (NOBODY, "No credentialed technician", 0)
RISK_CSS = {RiskClass.LIFE_SUPPORT: "bad", RiskClass.HIGH: "warn"}


# --- route sheets ----------------------------------------------------------------------------------------------------

def _open_pms(asset_ids: list) -> dict:
    """{asset id: its open PM work order} in one query, the earliest opened when there are several (the one the day panel reads)."""
    out: dict = {}
    rows = (WorkOrder.objects.filter(asset_id__in=asset_ids, type=WoType.PM, status__in=OPEN_STATUSES).order_by("opened_on", "number")
            .values("asset_id", "number", "vendor_service", "assigned_to_id", "assigned_to__name", "assigned_to__is_active"))
    for w in rows:
        out.setdefault(w["asset_id"], w)
    return out


def _held(w: dict | None) -> bool:
    """Whether the open PM work order is on someone's plate: with the vendor, or with an active technician."""
    return bool(w) and (w["vendor_service"] or bool(w["assigned_to_id"] and w["assigned_to__is_active"]))


def _sheet_key(w: dict | None, suggested) -> tuple:
    if _held(w):
        return VENDOR_KEY if w["vendor_service"] else (TECH, w["assigned_to__name"], w["assigned_to_id"])
    if suggested is not None:
        return (TECH, suggested.name, suggested.id)
    return NOBODY_KEY


def _wo_note(w: dict | None) -> str:
    """Under the work order number, when the work order is not with the sheet's technician or the vendor."""
    if not w or _held(w):
        return ""
    return f"with {w['assigned_to__name']}, inactive" if w["assigned_to_id"] else "unassigned"


def _row(asset, w: dict | None) -> dict:
    dm = asset.device_model
    hours = sch.pm_hours(dm)  # the procedure's estimate, as the day panel shows it
    return {"asset": asset, "day": asset.next_pm_on, "department": asset.department.name, "room": asset.room,
            "risk": RiskClass(dm.risk_class).label, "risk_css": RISK_CSS.get(dm.risk_class, ""), "procedure": dm.pm_procedure,
            "hours": hours, "hours_label": views_pm._hours(hours), "wo": w["number"] if w else "", "wo_note": _wo_note(w)}


def _sheets(items: list[tuple[tuple, dict]], with_day: bool) -> list[dict]:
    """Group (sheet key, row) pairs into sheets in key order, each sheet's rows by department, room, and tag (date first for a week)."""
    groups: dict = {}
    for key, row in items:
        groups.setdefault(key, []).append(row)

    def row_order(r):
        place = (r["department"].casefold(), r["room"].casefold(), r["asset"].tag)
        return (r["day"], *place) if with_day else place

    sheets = []
    for key in sorted(groups, key=lambda k: (k[0], k[1].casefold(), k[2])):
        rows = sorted(groups[key], key=row_order)
        hours = sum((r["hours"] for r in rows), Decimal("0"))
        sheets.append({"name": key[1], "kind": {TECH: "tech", VENDOR: "vendor", NOBODY: "nobody"}[key[0]], "rows": rows,
                       "count": len(rows), "hours": views_pm._hours(hours)})
    return sheets


def _suggest_waiting(day: date, today: date, waiting: list, rows: list[dict]) -> dict:
    """Technicians for devices whose open PM work order is on nobody's plate. Inside the week the week plan already counts the
    day's other devices (sch.suggestions_for_day). Outside it the schedule picks for the day alone, so start from the hours the
    day panel just gave the devices needing a work order: the waiting ones are balanced against them, not piled on whoever
    sorts first by name."""
    if today <= day < today + timedelta(days=sch.WEEK_DAYS):
        return sch.suggestions_for_day(day, waiting, today)
    load = sch.open_hours_by_technician()
    for r in rows:
        if r["technician"] is not None and not r["has_open_pm"]:
            load[r["technician"].id] = load.get(r["technician"].id, Decimal("0")) + r["hours"]
    return sch.suggest_technicians(waiting, today, load=load)


def day_sheets(day: date, today: date) -> list[dict]:
    """The day panel's devices (sch.day_plan) on sheets. The plan suggests a technician only for devices with no open PM work order,
    so the devices whose open one is on nobody's plate ask the schedule the same way (bounded: one more plan, not one per device)."""
    plan = sch.day_plan(day, today)
    open_pm = _open_pms([r["asset"].id for r in plan["rows"]])
    waiting = [r["asset"] for r in plan["rows"] if r["asset"].id in open_pm and not _held(open_pm[r["asset"].id])]
    more = _suggest_waiting(day, today, waiting, plan["rows"]) if waiting else {}
    items = []
    for r in plan["rows"]:
        a = r["asset"]
        w = open_pm.get(a.id)
        items.append((_sheet_key(w, r["technician"] or more.get(a.id)), _row(a, w)))
    return _sheets(items, with_day=False)


def week_sheets(today: date) -> list[dict]:
    """The week plan (today through today + 6) on sheets, a technician's days together."""
    plan = sch.week_plan(today)
    devices = plan["devices"]
    prefetch_related_objects(devices, "department")  # the plan's query does not join departments: one query, not one per device
    open_pm = _open_pms([a.id for a in devices])
    items = [(_sheet_key(open_pm.get(a.id), plan["suggested"].get(a.id)), _row(a, open_pm.get(a.id))) for a in devices]
    return _sheets(items, with_day=True)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def route_sheets(request):
    today = views_pm._today()  # the PM screen's clock, so the sheets and the screen agree on what today is
    _y, _m, day = views_pm._selection(request.GET, today)  # the day the PM screen's panel shows for the same address
    week = request.GET.get("scope") == "week"
    if week:
        start, end = today, today + timedelta(days=sch.WEEK_DAYS - 1)
        sheets = week_sheets(today)
    else:
        start = end = day
        sheets = day_sheets(day, today)
    ctx = {"today": today, "day": day, "week": week, "start": start, "end": end, "sheets": sheets, "overdue": not week and day < today,
           "devices": sum(s["count"] for s in sheets)}
    return render(request, "web/print_route_sheets.html", ctx)


# --- report PDFs ----------------------------------------------------------------------------------------------------

@web_view(Module.REPORTS, Level.VIEW)
def report_print(request, key):
    meta = rs.report_meta(key)
    if meta is None:
        raise Http404("No such report")
    today = views_reports._today()  # the Reports screen's clock, so the paper and the screen show the same numbers
    data = rs.run_report(meta["key"], today)
    ctx = {"today": today, "report": meta, "r": data, "p": present(meta["key"], data),
           "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW),
           "can_view_recalls": request.user.has_level(Module.RECALLS, Level.VIEW)}
    return render(request, "web/print_report.html", ctx)
