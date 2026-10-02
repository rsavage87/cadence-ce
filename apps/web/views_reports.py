"""
Reports screen (slice 7): the mock's eight reports on one page, the list on the left and the selected report on the
right. Every report is as of today; its numbers come from apps.reports.services.run_report and its chart geometry from
apps.web.reports.present. Views parse the key, call the report, and render.

The list links swap #rep-body (list and panel together, so the active item follows the selection) and push the URL,
so /reports/<key>/ renders as a full page too. Each report also downloads as CSV: the same table the API serves.

Schedule (slice 13) emails a report to the signed-in user, every Monday or on the first Monday of each month
(apps.reports.subscriptions). The button says what they have; its modal saves through set_subscription and sends the button
back out of band, so the panel shows the new state.

Custom reports (slice 18) sit in the list under the eight, at /reports/custom-<id>/, and download, print, and schedule the same
way: every view here finds a report by key through apps.reports.services.find_report and runs it through run_any. A custom
report that lists work orders or devices needs View on them too (apps/reports/permissions.py): without it the panel says so
in plain words, and the CSV is refused with the same words. Building one is views_custom_reports.
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django_htmx.http import trigger_client_event

from apps.accounts.models import Level, Module
from apps.reports import custom
from apps.reports import permissions as rep_perms
from apps.reports import services as rs
from apps.reports import subscriptions as subs

from .decorators import web_view
from .exports import csv_response
from .htmx import is_partial, toast
from .reports import present
from .reports_custom import present_custom


def _today() -> date:
    """The reports' clock, in one place so tests can pin it (fixture `freeze_today`)."""
    return date.today()


def _meta(key: str | None) -> dict:
    meta = rs.find_report(key or rs.REPORT_KEYS[0])
    if meta is None:
        raise Http404("No such report")
    return meta


def refused(message: str) -> HttpResponseForbidden:
    """A download or print page the user may not have, refused in plain words."""
    return HttpResponseForbidden(message, content_type="text/plain; charset=utf-8")


def body_context(request, partial: bool) -> dict:
    """What #rep-body needs around its panel: the list (the eight, then the facility's custom reports) and the page's flags."""
    return {"nav_active": "reports", "today": _today(), "reports": rs.REPORTS, "customs": custom.menu(), "partial": partial,
            "can_build": rep_perms.can_build(request.user)}


def report_context(request, meta: dict, today: date) -> dict:
    """One report's panel: its data and presentation, or the refusal when it lists what the user cannot see."""
    user = request.user
    refusal = rep_perms.meta_refusal(user, meta)
    data = {} if refusal else rs.run_any(meta["key"], today)
    if refusal:
        p = {}
    else:
        p = present_custom(data) if meta["custom"] else present(meta["key"], data)
    ctx = {"report": meta, "r": data, "p": p, "refusal": refusal,
           "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW), "can_view_wo": user.has_level(Module.WORKORDERS, Level.VIEW),
           "can_view_recalls": user.has_level(Module.RECALLS, Level.VIEW)}
    return {**ctx, **(_schedule_button(user, meta) if not refusal else {"can_schedule": False})}


@web_view(Module.REPORTS, Level.VIEW)
def reports(request, key=None):
    meta = _meta(key)
    partial = is_partial(request, "rep-body")
    ctx = {**body_context(request, partial), **report_context(request, meta, _today())}
    return render(request, "web/_reports_body.html" if partial else "web/reports.html", ctx)


@web_view(Module.REPORTS, Level.VIEW)
def report_csv(request, key):
    meta = _meta(key)
    refusal = rep_perms.meta_refusal(request.user, meta)
    if refusal:
        return refused(refusal)
    today = _today()
    data = rs.run_any(meta["key"], today)
    return csv_response(rs.csv_filename(meta, today), data["columns"], data["rows"])


# --- Schedule (slice 13) ---------------------------------------------------------------------------------------------------

def _schedule_button(user, meta) -> dict:
    """The Schedule button's state. Shown only to someone who can have reports emailed (in this facility, an email address,
    Reports View, and for a custom report View on what it lists); the service checks the same on save."""
    can = subs.can_schedule(user) and not rep_perms.meta_refusal(user, meta)
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
    refusal = rep_perms.meta_refusal(request.user, meta)
    ctx = {"report": meta, "email": request.user.email, "can_schedule": subs.can_schedule(request.user) and not refusal, "refusal": refusal,
           "options": options, "chosen": current if chosen is None else chosen, "error": error}
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
