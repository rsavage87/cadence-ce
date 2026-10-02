"""
Labor and parts in the work order drawer (slice 15). Views parse input, call apps.workorders.costs, and answer with the drawer.

Who may do what (apps/workorders/permissions.py), checked here on every request, GET and POST: work-order Edit (can_record_work)
logs time, adds parts, and removes either; a rate other than the Settings one needs Approve (can_set_rate), and the rate box is
only offered to those who have it. Requesters and analysts get a 403; another facility's work order or line is a 404 (the scoped
managers cannot see it). A closed or cancelled work order takes no lines (apps.workorders.costs says why).

Log time and Add part are modals (#modal-card): a save swaps the work order's drawer into #drawer instead (HX-Retarget), toasts,
fires `wo-changed` (the lists' cost column re-fetches on it), then closes the modal after settle. A Remove button sits in the
drawer and answers with the drawer.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.credentials.models import Technician
from apps.workorders import costs
from apps.workorders import permissions as wo_perms
from apps.workorders.models import LaborLine, PartLine

from .decorators import web_view
from .forms_wo_costs import LaborForm, PartForm, money_text
from .htmx import toast
from .views import _get_wo, _render_wo_drawer


def costs_context(request, wo) -> dict:
    """The drawer's Cost section (_wo_costs.html), under "costs" so nothing collides with the drawer's own keys. Each line is
    worked to the cent first and the totals from those, so the lines on screen add up to the totals on screen."""
    labor = sorted(wo.labor_lines.all(), key=lambda line: (line.worked_on, line.created_at))
    parts = sorted(wo.part_lines.all(), key=lambda line: line.created_at)
    tech_ids = {line.technician_id for line in labor if line.technician_id}
    names = {t.pk: t.name for t in Technician.objects.filter(pk__in=tech_ids)} if tech_ids else {}
    vendor = wo.vendor_name or "Vendor service"
    labor_rows = [{"line": line, "who": names.get(line.technician_id) or vendor, "hours": costs.plain(line.hours), "amount": costs.labor_amount(line)}
                  for line in labor]
    part_rows = [{"line": line, "quantity": costs.plain(line.quantity), "amount": costs.part_amount(line),
                  "event": costs.part_event(line.description, line.quantity, line.unit_cost)} for line in parts]
    labor_total = sum((row["amount"] for row in labor_rows), Decimal(0))
    parts_total = sum((row["amount"] for row in part_rows), Decimal(0))
    rates = {line.rate for line in labor}
    can_record = wo_perms.can_record_work(request.user)
    locked = costs.locked_reason(wo)
    return {"costs": {
        "labor": labor_rows, "parts": part_rows, "hours": costs.plain(sum((line.hours for line in labor), Decimal(0))),
        "labor_total": labor_total, "parts_total": parts_total, "total": labor_total + parts_total,
        "rate": next(iter(rates)) if len(rates) == 1 else None,  # one rate for every line: the stat says "at $82.00/h"
        "can_record": can_record and not locked, "locked": locked if can_record else "",  # why there is no Log time button
    }}


def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _changed(response, message: str):
    trigger_client_event(response, "wo-changed", {})
    return toast(response, message)


def _saved(request, wo, message: str):
    """A modal saved: show the work order's drawer, refresh the lists, toast, then close the modal. After settle: closing it
    first would detach the form that sent this request, which cancels the swap and loses the other events."""
    response = _changed(retarget(_render_wo_drawer(request, wo), "#drawer"), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _modal(request, template, wo, form, **extra):
    if form is not None and form.is_bound:
        form.focus_first_error()
    return render(request, template, {"wo": wo, "form": form, "locked": costs.locked_reason(wo), **extra})


# --- Log time --------------------------------------------------------------------------------------------------

def _labor_form(request, wo, data=None):
    return LaborForm(data, wo=wo, user=request.user, default_rate=costs.default_rate(wo), can_set_rate=wo_perms.can_set_rate(request.user))


def _labor_modal(request, wo, form):
    return _modal(request, "web/_wo_labor.html", wo, form, rate=costs.default_rate(wo))


def _check_rate(request, wo):
    """A rate other than the Settings one needs work-order Approve. Blank, or the Settings rate typed out, is anyone's (and the
    form ignores a rate from anyone without Approve, so their line is charged at the Settings rate whatever was posted)."""
    text = money_text(request.POST.get("rate", ""))
    if not text or wo_perms.can_set_rate(request.user):
        return
    try:
        same = Decimal(text) == costs.default_rate(wo)
    except (InvalidOperation, ValueError):
        same = False
    _require(same)


@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def labor_add(request, number):
    wo = _get_wo(number)
    if request.method != "POST":
        if not request.htmx:  # only ever a modal: a direct visit opens the work order
            return redirect("web:wo", number=wo.number)
        return _labor_modal(request, wo, _labor_form(request, wo))
    _check_rate(request, wo)
    form = _labor_form(request, wo, request.POST)
    line = None
    if form.is_valid():
        try:
            line = costs.add_labor(wo, by=request.user, **form.service_kwargs())
        except ValidationError as e:
            form.add_service_errors(e)
    if line is None:
        return _labor_modal(request, wo, form)
    return _saved(request, wo, f"{costs.plain(line.hours)} h logged on {wo.number}")


@require_POST
@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def labor_delete(request, number, pk):
    wo = _get_wo(number)
    line = get_object_or_404(LaborLine.objects, pk=pk, work_order=wo)
    try:
        costs.remove_labor(line, by=request.user)
    except ValidationError as e:
        return toast(_render_wo_drawer(request, wo), e.messages[0])
    return _changed(_render_wo_drawer(request, wo), "Labor line removed")


# --- Add part --------------------------------------------------------------------------------------------------

def _part_modal(request, wo, form):
    return _modal(request, "web/_wo_part.html", wo, form)


@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def part_add(request, number):
    wo = _get_wo(number)
    if request.method != "POST":
        if not request.htmx:
            return redirect("web:wo", number=wo.number)
        return _part_modal(request, wo, PartForm())
    form = PartForm(request.POST)
    line = None
    if form.is_valid():
        try:
            line = costs.add_part(wo, by=request.user, **form.service_kwargs())
        except ValidationError as e:
            form.add_service_errors(e)
    if line is None:
        return _part_modal(request, wo, form)
    return _saved(request, wo, "Part added")


@require_POST
@web_view(wo_perms.MODULE, wo_perms.RECORD_LEVEL)
def part_delete(request, number, pk):
    wo = _get_wo(number)
    line = get_object_or_404(PartLine.objects, pk=pk, work_order=wo)
    try:
        costs.remove_part(line, by=request.user)
    except ValidationError as e:
        return toast(_render_wo_drawer(request, wo), e.messages[0])
    return _changed(_render_wo_drawer(request, wo), "Part removed")
