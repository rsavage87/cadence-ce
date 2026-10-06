"""
My work (slice 24): the technician's own work, phone first (apps.workorders.my_work says whose and groups it). A vendor technician
sees their company's open work; a clinical requester, or someone in the facility with no technician profile, is told what the page
is for. Opening a card, logging time, and completing go through the work order's own endpoints (the drawer and its modals, which
check their own levels); the list re-fetches itself when they change something (the wo-changed event).

Scaffold: part A builds the cards and their actions, part B completing a PM from a card, part C taking unassigned work, part D Scan.
"""
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import Level, Module
from apps.workorders import my_work

from .decorators import web_view
from .htmx import is_partial

BODY = "my-work-body"


def my_work_context(request) -> dict:
    today = timezone.localdate()
    return {"nav_active": "my_work", "today": today, "groups": my_work.groups(request.user, today), "hours": my_work.hours(request.user, today)}


@web_view(Module.WORKORDERS, Level.VIEW, scoped=True)  # narrowed to the user's share (my_work.mine starts from scoping.work_orders)
def my_work_page(request):
    ctx = my_work_context(request)
    if is_partial(request, BODY):
        return render(request, "web/_my_work_body.html", ctx)
    return render(request, "web/my_work.html", ctx)
