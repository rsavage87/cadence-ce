"""
Asset labels and the work-order print (slice 11). Both open in a new tab on web/print_base.html; the browser's print
dialog prints them or saves a PDF.

Labels carry the device's request link as a QR code (apps.facility.services.asset_request_url, the same link as the device
drawer's Request link), so unit staff can scan a device and report a problem. ?tag= prints one device; otherwise the
Equipment list's own filters pick the devices, in the list's order (its page and the other screens' parameters are ignored).

A completed PM prints its result and its checklist as recorded (slice 15), and a failed PM and its follow-up repair name each other.

Slice 16: a scoped user (apps.workorders.scoping) prints labels for their own devices and their own work orders; another tag or
number is a 404, and a failed PM and its repair name each other only when both are theirs.

Slice 26: an incoming inspection prints like a PM: open, its checklist to tick (completion.procedure_for: the model's PM procedure
when it has steps, else the incoming checklist); completed, its result, who inspected it and when, and the steps as recorded. A failed
inspection and its re-inspection name each other ("Opened from: Failed incoming inspection WO-...").
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.accounts.models import Level, Module
from apps.equipment.models import Asset
from apps.equipment.services import filter_assets
from apps.facility.services import asset_request_url, get_settings
from apps.workorders import completion, inspections, scoping
from apps.workorders import services as wo_services
from apps.workorders.models import OPEN_STATUSES, LaborLine, PartLine, WorkOrder, WoType

from .decorators import web_view
from .forms import asset_filter_options, parse_asset_filters
from .qr import qr_svg

# --- asset labels ---------------------------------------------------------------------------------

# Every label's QR code is drawn in the request (about 5 ms each), so a print job stops here; a bigger list is a filter to narrow.
LABELS_MAX = 600
LABEL, SHEET = "label", "sheet"  # one 2.25 x 1.25 in label per page (thermal printer), or US Letter sheets of 2 x 4 in labels
PER_SHEET = 10  # Avery 5163: 2 columns x 5 rows


def url_size(short_url: str) -> str:
    """How the printed URL fits on one line (print_labels.html): "" fits both layouts at their normal size; "long" needs the
    small label's smaller face; "wide" is left off the small label and smaller on a sheet; "xwide" is left off both. One line
    is about 48 characters on the 2.25 in label at 5 pt (56 at 4.3 pt), and 62 on the 4 in label at 7 pt (72 at 6 pt)."""
    n = len(short_url)
    return "" if n <= 48 else "long" if n <= 56 else "wide" if n <= 72 else "xwide"


def _label(asset) -> dict:
    url = asset_request_url(asset)
    # Printed under the code for people without a scanner: browsers add the scheme themselves.
    short = url.split("://", 1)[-1]
    return {"asset": asset, "url": url, "short_url": short, "url_size": url_size(short),
            "qr": qr_svg(url, f"QR code: report a problem with {asset.tag}")}


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def labels(request):
    tag = request.GET.get("tag", "").strip()
    too_many = 0
    mine = scoping.assets(request.user) if scoping.is_scoped(request.user) else None  # None: the facility's list, as the services take it
    if tag:
        assets = [get_object_or_404(scoping.assets(request.user, Asset.objects.select_related("device_model", "department", "tenant")), tag=tag)]
    else:
        qs = filter_assets(parse_asset_filters(request.GET, asset_filter_options(mine)), qs=mine).select_related("tenant")
        assets = list(qs[:LABELS_MAX + 1])
        if len(assets) > LABELS_MAX:
            too_many, assets = qs.count(), []
    layout = request.GET.get("layout")
    if layout not in (LABEL, SHEET):
        layout = LABEL if tag else SHEET
    items = [_label(a) for a in assets]
    pages = [items[i:i + PER_SHEET] for i in range(0, len(items), PER_SHEET)] if layout == SHEET else [[it] for it in items]
    return render(request, "web/print_labels.html", {
        "tag": tag, "layout": layout, "labels": items, "pages": pages, "too_many": too_many, "labels_max": LABELS_MAX,
        "facility": request.tenant.name, "hotline": get_settings().portal_hotline,
    })


# --- work-order print -----------------------------------------------------------------------------

def _cents(amount) -> Decimal:
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


RULED_LINES = 8  # blank lines to write on: findings while a repair is open, or the steps when no PM procedure is on file
BLANK_ROWS = 2  # empty labor and parts rows while the work order is open


def _step(step) -> dict:
    if not isinstance(step, dict):
        return {"text": str(step), "measured": False, "measure": ""}
    measure = step.get("measure")
    # Any reading counts but an absent one (None, False, ""): 0 and 0.3 are limits too, and True means "record a value".
    measured = measure is not None and measure is not False and measure != ""
    text = step.get("text")
    if text in (None, ""):  # another key ({"step": ...}) or none: print what there is rather than an empty row
        text = "; ".join(str(v) for k, v in step.items() if k != "measure" and v not in (None, ""))
    return {"text": str(text), "measured": measured, "measure": "" if measure is True or not measured else str(measure)}


def checklist_steps(procedure) -> list[dict]:
    """A procedure's steps for the print. A step is a string, or {"text": ..., "measure": ...} when a reading is recorded;
    `measure` is what to record (e.g. "µA, limit 100"). Nothing in the JSON is dropped: a dict without "text" prints its other
    values, and a checklist saved as one string prints a step per line. Slice 26: the incoming checklist (inspections.INCOMING)
    keeps its steps in a tuple."""
    if procedure is None or procedure.checklist in (None, "", [], (), {}):
        return []
    steps = procedure.checklist
    if isinstance(steps, str):
        steps = [line.strip() for line in steps.splitlines() if line.strip()]
    elif not isinstance(steps, (list, tuple)):
        steps = [steps]
    return [_step(s) for s in steps]


def inspector(wo) -> str:
    """Who did an incoming inspection (slice 26; the print and the drawer's results say so): the vendor for vendor service (their
    field engineer or physicist), else the technician assigned."""
    if wo.vendor_service:
        return wo.vendor_name or "Vendor"
    return wo.assigned_to.name if wo.assigned_to_id else ""


@web_view(Module.WORKORDERS, Level.VIEW, scoped=True)
def wo_print(request, number):
    # Scoped like the drawer's lookup (views.get_wo): another tenant's number, or one outside a scoped user's share, is a 404.
    user = request.user
    labor_lines = Prefetch("labor_lines", queryset=LaborLine.objects.select_related("technician").order_by("worked_on", "created_at"))
    part_lines = Prefetch("part_lines", queryset=PartLine.objects.order_by("created_at"))
    wo = get_object_or_404(scoping.work_orders(user, WorkOrder.objects.select_related(
        "asset", "asset__device_model", "asset__device_model__pm_procedure", "asset__department", "asset__contract", "assigned_to", "alert",
        "follow_up_of").prefetch_related(labor_lines, part_lines)), number=number)
    today = timezone.localdate()
    is_open = wo.status in OPEN_STATUSES
    is_pm, is_inspection = wo.type == WoType.PM, wo.type == WoType.INSPECTION
    done = wo.status in completion.DONE_STATUSES
    # The procedure a PM is done to; slice 26, an inspection's (its model's PM procedure when that has steps, else the incoming checklist)
    procedure = completion.procedure_for(wo) if is_pm or is_inspection else None
    incoming = inspections.is_incoming_checklist(procedure)
    # Slice 15: a completed PM prints what was recorded (completion.recorded_steps: the checklist as it was, with each step's
    # result and reading) instead of boxes to tick; an open one prints the procedure's checklist as it is now. Slice 26: an
    # inspection likewise, recorded once it has its result or its steps (its checklist is optional; so is its result on a device
    # that was not waiting for it).
    if is_inspection:
        recorded = done and (bool(wo.inspection_result) or bool(wo.checklist_results))
    else:
        recorded = is_pm and done and bool(wo.pm_result)
    # Each line to the cent first, and the totals from those, so the printed columns add up to the printed totals.
    labor = [(line, _cents(line.hours * line.rate)) for line in wo.labor_lines.all()]
    parts = [(line, _cents(line.quantity * line.unit_cost)) for line in wo.part_lines.all()]
    labor_total = sum((cost for _line, cost in labor), Decimal(0))
    parts_total = sum((cost for _line, cost in parts), Decimal(0))
    return render(request, "web/print_wo.html", {
        "wo": wo, "asset": wo.asset, "facility": request.tenant.name, "today": today, "is_open": is_open, "is_pm": is_pm,
        "is_inspection": is_inspection, "checked": is_pm or is_inspection, "incoming": incoming,
        "inspector": inspector(wo) if is_inspection else "",
        "past_due_days": (today - wo.due_on).days if is_open and wo.due_on < today else 0,
        # The procedure on file (never the incoming checklist, which is no procedure: no code, source, or revision to print)
        "procedure": None if incoming else procedure, "steps": checklist_steps(procedure),
        "recorded": recorded, "recorded_steps": completion.recorded_steps(wo) if recorded else [],
        "unrecorded": (is_pm or is_inspection) and done and not recorded,  # completed before results were recorded
        "follow_ups": list(scoping.work_orders(user, wo.follow_ups.order_by("opened_on", "number"))) if is_pm or is_inspection else [],
        "follow_up_of": wo.follow_up_of if wo.follow_up_of_id and scoping.can_see_work_order(user, wo.follow_up_of) else None,
        "labor": labor, "parts": parts, "labor_hours": sum((line.hours for line, _cost in labor), Decimal(0)),
        "labor_total": labor_total, "parts_total": parts_total, "total": labor_total + parts_total,
        "timeline": wo_services.timeline(wo), "rules": range(RULED_LINES), "blank_rows": range(BLANK_ROWS) if is_open else range(0),
    })
