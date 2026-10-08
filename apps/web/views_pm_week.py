"""
Auto-assign week on the PM schedule (slice 14): the mock's button. GET shows what it would do (a modal in #modal-card), read from the
same week plan the day panel, Create, and the workload show; POST does it through apps.pm.services.assign_week and answers like
"Create N PM work orders": a toast, `pm-changed` (the lower panels re-fetch their workload and outlook), and `pm-assigned` (the body's
hidden refresher re-fetches the calendar and day list for the month and day on screen, so the rows name their new assignees), then
closes the modal after settle. The POST answers the modal, not the body, so nothing is fetched twice; Create answers with the body
itself and fires no `pm-assigned`.

Both need pm_perms.can_assign_week (PM Approve and the work-order assign level), checked here on every request; the button is hidden
without it too, but hiding a button is not access control. The API's GET /api/v1/pm/week/ (the preview) and POST
/api/v1/pm/assign-week/ (apps/api/views_pm.py) call the same services with the same permission.
"""
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django_htmx.http import trigger_client_event

from apps.pm import permissions as pm_perms
from apps.pm.services import WeekAssignment, assign_week, week_assignment_preview

from . import views_pm
from .decorators import web_view
from .htmx import toast

TEMPLATE = "web/_pm_week_assign.html"
UNCOVERED_LIMIT = 8  # devices nobody is credentialed for, listed by tag; the rest are counted


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def assigned_message(done: WeekAssignment) -> str:
    """The toast after the POST, in the mock's words ("Auto-assign balanced N PMs across M technicians by credential, zone, and
    workload"; there are no zones), plus what nobody is credentialed for, and (slice 28) the devices held as evidence for an incident
    investigation whenever any is due: their PMs are with nobody and wait for the incident's release, so "every PM due this week is
    already with a technician or the vendor" is never said while one is (review fix)."""
    u = done.unassigned
    on_hold = len(getattr(done, "on_hold", ()))  # slice 28: devices held as evidence (a summary from before slice 28 has none)
    held_part = f"{held_devices(on_hold)} due this week {'is' if on_hold == 1 else 'are'} held for an incident investigation" if on_hold else ""
    if done.assigned:
        message = f"Auto-assign balanced {_plural(done.assigned, 'PM')} across {_plural(done.technicians, 'technician')} by credential and workload"
        if u:
            message += f"; {u} left unassigned: nobody is credentialed for {'it' if u == 1 else 'them'}"
        return f"{message}; {held_part}" if held_part else message
    if done.created:
        c = done.created
        message = f"{_plural(c, 'PM work order')} created, none assigned: nobody is credentialed for {'this device' if c == 1 else 'these devices'}"
        return f"{message}; {held_part}" if held_part else message
    if u:
        message = f"Nothing to assign: {_plural(u, 'PM')} this week still {'needs' if u == 1 else 'need'} a credentialed technician"
        return f"{message}; {held_part}" if held_part else message
    if on_hold and done.held:  # some with someone, the rest held as evidence
        return (f"Nothing to assign: {_plural(done.held, 'PM')} due this week {'is' if done.held == 1 else 'are'} already with a technician "
                f"or the vendor, and {held_part}")
    if on_hold:  # only devices held as evidence are due
        return f"Nothing to assign: {held_part}"
    if done.held:
        return "Nothing to assign: every PM due this week is already with a technician or the vendor"
    return "Nothing to assign: no PMs are due this week"


def held_devices(n: int) -> str:
    return "the device" if n == 1 else f"the {n} devices"


def _context(w: WeekAssignment) -> dict:
    if w.assigned:
        confirm = f"Assign {_plural(w.assigned, 'PM')}"
    else:
        confirm = f"Create {_plural(w.created, 'PM work order')}"
    return {"w": w, "uncovered": w.uncovered[:UNCOVERED_LIMIT], "uncovered_more": max(0, w.unassigned - UNCOVERED_LIMIT),
            "confirm": confirm,
            # slice 28: the devices held as evidence, by tag (the rest counted): nobody may start their PMs, so none is created or assigned
            "on_hold": w.on_hold[:UNCOVERED_LIMIT], "on_hold_more": max(0, len(w.on_hold) - UNCOVERED_LIMIT)}


@require_http_methods(["GET", "POST"])
@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def week_assign(request):
    if not pm_perms.can_assign_week(request.user):
        raise PermissionDenied
    today = views_pm._today()  # the PM screen's clock, so the modal and the schedule agree on what this week is
    if request.method != "POST":
        return render(request, TEMPLATE, _context(week_assignment_preview(today)))
    done = assign_week(by=request.user, today=today)
    response = toast(HttpResponse(""), assigned_message(done))
    if not done.nothing_to_do:
        for event in ("pm-changed", "pm-assigned"):
            trigger_client_event(response, event, {})
    # After settle: closing the modal first would detach the button that sent this request, which cancels the swap and the events.
    return trigger_client_event(response, "modal-close", {}, after="settle")
