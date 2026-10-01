"""
Reports screen (slice 7): the mock's eight reports on one page, the list on the left and the selected report on the
right. Every report is as of today; its numbers come from apps.reports.services.run_report and its chart geometry from
apps.web.reports.present. Views parse the key, call the report, and render.

The list links swap #rep-body (list and panel together, so the active item follows the selection) and push the URL,
so /reports/<key>/ renders as a full page too. Each report also downloads as CSV: the same table the API serves.

Schedule (slice 13) emails a report to the signed-in user, every Monday or on the first Monday of each month
(apps.reports.subscriptions). The button says what they have; its modal saves through set_subscription and sends the button
back out of band, so the panel shows the new state.
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django_htmx.http import trigger_client_event

from apps.accounts.models import Level, Module
from apps.reports import services as rs
from apps.reports import subscriptions as subs

from .decorators import web_view
from .exports import csv_response
from .htmx import is_partial, toast
from .reports import present


def _today() -> date:
    """The reports' clock, in one place so tests can pin it (fixture `freeze_today`)."""
    return date.today()


def _meta(key: str | None) -> dict:
    meta = rs.report_meta(key or rs.REPORT_KEYS[0])
    if meta is None:
        raise Http404("No such report")
    return meta


@web_view(Module.REPORTS, Level.VIEW)
def reports(request, key=None):
    meta = _meta(key)
    today = _today()
    data = rs.run_report(meta["key"], today)
    partial = is_partial(request, "rep-body")
    ctx = {"nav_active": "reports", "today": today, "reports": rs.REPORTS, "report": meta, "r": data, "p": present(meta["key"], data), "partial": partial,
           "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW),
           "can_view_recalls": request.user.has_level(Module.RECALLS, Level.VIEW), **_schedule_button(request.user, meta)}
    return render(request, "web/_reports_body.html" if partial else "web/reports.html", ctx)


@web_view(Module.REPORTS, Level.VIEW)
def report_csv(request, key):
    meta = _meta(key)
    today = _today()
    data = rs.run_report(meta["key"], today)
    return csv_response(f"cadence-{meta['key']}-{today:%Y-%m-%d}.csv", data["columns"], data["rows"])


# --- Schedule (slice 13) ---------------------------------------------------------------------------------------------------

def _schedule_button(user, meta) -> dict:
    """The Schedule button's state. Shown only to someone who can have reports emailed (in this facility, an email address,
    Reports View); the service checks the same on save."""
    can = subs.can_schedule(user)
    sub = subs.subscription_for(user, meta["key"]) if can else None
    return {"can_schedule": can, "schedule_sub": sub, "schedule_label": f"Scheduled {subs.SHORT[sub.frequency]}" if sub else "Schedule"}


def _schedule_modal(request, meta, error: str = "", chosen: str | None = None):
    today = subs.local_today()  # the daily job's clock, not the reports': it decides when the first email goes out
    sub = subs.subscription_for(request.user, meta["key"])
    current = sub.frequency if sub else ""
    options = [{"value": "", "label": "Off", "hint": "Not emailed"}]
    for value, label in subs.Frequency.choices:
        on = subs.first_send_on(value, today, sub.last_sent_on if sub else None)
        first = "Next" if value == current else "First"
        options.append({"value": value, "label": label, "hint": f"{first} email {on:%A}, {on:%B} {on.day}, {on.year}"})
    ctx = {"report": meta, "email": request.user.email, "can_schedule": subs.can_schedule(request.user), "options": options,
           "chosen": current if chosen is None else chosen, "error": error}
    return render(request, "web/_report_schedule.html", ctx)


@web_view(Module.REPORTS, Level.VIEW)
def report_schedule(request, key):
    """The Schedule modal: Off, Every Monday, or First Monday of each month, to the user's own email. Only ever a modal, so a
    direct visit goes to the report."""
    meta = _meta(key)
    if request.method != "POST":
        if not request.htmx:
            return redirect("web:report", key=meta["key"])
        return _schedule_modal(request, meta)
    frequency = request.POST.get("frequency", "")
    had = subs.subscription_for(request.user, meta["key"]) is not None
    try:
        sub = subs.set_subscription(request.user, meta["key"], frequency)
    except ValidationError as e:
        return _schedule_modal(request, meta, error=e.messages[0], chosen=frequency)
    if sub:
        message = f"Scheduled: {meta['title']}, {subs.WHEN[sub.frequency]} to {request.user.email}"
    elif had:
        message = f"{meta['title']} will no longer be emailed"
    else:
        message = f"{meta['title']} is not scheduled"
    # The button comes back out of band and the modal card empties; it closes after settle (closing first would detach the form
    # that sent this request and cancel the swap).
    response = render(request, "web/_report_schedule_button.html", {"report": meta, "oob": True, **_schedule_button(request.user, meta)})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")
