"""
PM schedule screen (slice 9): the month calendar and the selected day's devices (#pm-body, swapped together so the selection
follows the click), then the lower panels (#pm-panels, apps/web/pm_panels.py). Views parse the month and day, call
apps.pm.schedule and apps.pm.services, and render.

?y=&m= pick the month and ?day=YYYY-MM-DD the selected day; a day alone shows its month, and a day outside the shown month's
grid moves the calendar to it. Month navigation carries no day: the panel then shows today in the current month and the first
of the month otherwise. Anything unparsable or outside 2000 to 2100 falls back to those defaults.

"Create N PM work orders" posts to /pm/day/<YYYY-MM-DD>/work-orders/?y=&m= and answers with the body for the same month and
day, a toast, and `pm-changed` (after settle) so the lower panels re-fetch their workload and outlook.
"""
import re
from datetime import date, timedelta
from decimal import Decimal

from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment.models import RiskClass
from apps.pm import permissions as pm_perms
from apps.pm import schedule as sch
from apps.pm.dates import month_bounds
from apps.pm.services import create_pm_work_orders_for_day
from apps.workorders import permissions as wo_perms

from .decorators import web_view
from .htmx import is_partial, toast
from .pm_panels import panels_context

MIN_YEAR, MAX_YEAR = 2000, 2100
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_INT_RE = re.compile(r"\d{1,4}")


def _today() -> date:
    """The schedule's clock, in one place so tests can pin it."""
    return date.today()


def _parse_day(value) -> date | None:
    """A strict YYYY-MM-DD inside the supported years, else None (date.fromisoformat alone also takes 20260930 and week dates)."""
    if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return d if MIN_YEAR <= d.year <= MAX_YEAR else None


def _parse_month(y, m) -> tuple[int, int] | None:
    if not (isinstance(y, str) and isinstance(m, str) and _INT_RE.fullmatch(y) and _INT_RE.fullmatch(m)):
        return None
    y, m = int(y), int(m)
    return (y, m) if MIN_YEAR <= y <= MAX_YEAR and 1 <= m <= 12 else None


def _in_grid(day: date, year: int, month: int) -> bool:
    """Whether `day` shows in the month's Sunday-first grid (the leading and trailing days of the neighbouring months count)."""
    first, last = month_bounds(year, month)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    end = last + timedelta(days=(5 - last.weekday()) % 7)
    return start <= day <= end


def _selection(params, today: date, day: date | None = None) -> tuple[int, int, date]:
    """(year, month, selected day) from ?y=&m=&day= (or a day from the URL path), with the defaults described above."""
    day = day or _parse_day(params.get("day"))
    month = _parse_month(params.get("y"), params.get("m"))
    if day and (month is None or not _in_grid(day, *month)):
        month = (day.year, day.month)
    year, month = month or (today.year, today.month)
    if day is None:
        day = today if (year, month) == (today.year, today.month) else date(year, month, 1)
    return year, month, day


def _shift(year: int, month: int, by: int) -> tuple[int, int] | None:
    n = year * 12 + (month - 1) + by
    y, m = divmod(n, 12)
    return (y, m + 1) if MIN_YEAR <= y <= MAX_YEAR else None


def _url(year: int, month: int, day: date | None = None) -> str:
    return f"{reverse('web:pm')}?y={year}&m={month}" + (f"&day={day.isoformat()}" if day else "")


def _hours(h: Decimal) -> str:
    """1.50 -> "1.5", 2.00 -> "2": the mock prints the procedure's hours as they are."""
    return format(h.normalize(), "f")


def _row(r: dict) -> dict:
    a, tech = r["asset"], r["technician"]
    risk = a.device_model.risk_class
    held = r.get("open_pm_assignee") or ""
    return {**r, "rail": "crit" if risk == RiskClass.LIFE_SUPPORT else "warn" if risk == RiskClass.HIGH else "ok",
            "tech_first": tech.name.split()[0] if tech and tech.name.split() else "", "hours_label": _hours(r["hours"]),
            "open_pm_first": held.split()[0] if held.split() else ""}


def _body_context(request, today: date, year: int, month: int, day: date) -> dict:
    cal = sch.month_calendar(year, month, today)
    plan = sch.day_plan(day, today)
    for week in cal["weeks"]:
        for cell in week:
            cell["url"] = _url(year, month, cell["date"])
            cell["sel"] = cell["date"] == day
    prev, nxt = _shift(year, month, -1), _shift(year, month, 1)
    n = plan["to_create"]
    confirm = f"Create {n} PM work order{'' if n == 1 else 's'} for {day:%b} {day.day}, {day.year}?"
    if plan["overdue"]:
        confirm += " It will be due today." if n == 1 else " They will be due today."
    return {"nav_active": "pm", "today": today, "cal": cal, "day": day, "plan": plan, "self_url": _url(year, month, day),
            "day_rows": [_row(r) for r in plan["rows"][:sch.DAY_LIST_LIMIT]], "day_more": max(0, plan["count"] - sch.DAY_LIST_LIMIT),
            "day_skipped": plan["count"] - n, "prev_url": _url(*prev) if prev else "", "next_url": _url(*nxt) if nxt else "",
            "create_url": reverse("web:pm_create", args=[day.isoformat()]) + f"?y={year}&m={month}", "create_confirm": confirm,
            "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_create": pm_perms.can_create(request.user)}


def pm_page_context(request, today: date | None = None) -> dict:
    """Everything web/pm.html needs: the page head, the body for the month and day in the address, and the lower panels. Also
    what a model drawer opened directly renders behind it (views_models)."""
    today = today or _today()
    ctx = _body_context(request, today, *_selection(request.GET, today))
    # The body's keys win over the panels' so a name the panels happen to share cannot break the calendar.
    return {**panels_context(request, today), **ctx, "head": sch.schedule_summary(today)}


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def pm_schedule(request):
    today = _today()
    if is_partial(request, "pm-panels"):
        return render(request, "web/_pm_panels.html", panels_context(request, today))
    if is_partial(request, "pm-body"):
        return render(request, "web/_pm_body.html", _body_context(request, today, *_selection(request.GET, today)))
    return render(request, "web/pm.html", pm_page_context(request, today))


def _created_message(batch, may_assign: bool) -> str:
    n = batch.created
    if n == 0:
        return "Every PM on this day already has an open work order" if batch.skipped else "No PMs are due on this day"
    made = f"{n} PM work order{'' if n == 1 else 's'} created"
    if not may_assign:
        return f"{made}; a manager assigns {'it' if n == 1 else 'them'}"
    a = batch.assigned
    if a == 0:
        return f"{made}, none assigned: nobody is credentialed for {'this device' if n == 1 else 'these devices'}"
    return f"{made}, {a} assigned to {'a credentialed technician' if a == 1 else 'credentialed technicians'}"


@require_POST
@web_view(pm_perms.MODULE, pm_perms.CREATE_LEVEL)
def pm_create(request, day):
    d = _parse_day(day)
    if d is None:
        raise Http404("No such day")
    today = _today()
    may_assign = wo_perms.can_assign(request.user)
    batch = create_pm_work_orders_for_day(d, by=request.user, assign_to_technicians=may_assign, today=today)
    response = render(request, "web/_pm_body.html", _body_context(request, today, *_selection(request.GET, today, day=d)))
    toast(response, _created_message(batch, may_assign))
    # The lower panels (#pm-panels listens for pm-changed from:body) re-fetch their workload and outlook once the body has settled.
    return trigger_client_event(response, "pm-changed", after="settle")
