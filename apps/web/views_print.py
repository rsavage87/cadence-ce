"""
Asset labels and the work-order print (slice 11). Both open in a new tab on web/print_base.html; the browser's print
dialog prints them or saves a PDF.

Labels carry the device's request link as a QR code (apps.facility.services.asset_request_url, the same link as the device
drawer's Request link), so unit staff can scan a device and report a problem. ?tag= prints one device; otherwise the
Equipment list's own filters pick the devices, in the list's order (its page and the other screens' parameters are ignored).
"""
from datetime import date
from decimal import Decimal

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render

from apps.accounts.models import Level, Module
from apps.equipment.models import Asset
from apps.equipment.services import filter_assets
from apps.facility.services import asset_request_url, get_settings
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


def _label(asset) -> dict:
    url = asset_request_url(asset)
    # Printed under the code for people without a scanner: browsers add the scheme themselves.
    return {"asset": asset, "url": url, "short_url": url.split("://", 1)[-1], "qr": qr_svg(url, f"QR code: report a problem with {asset.tag}")}


@web_view(Module.EQUIPMENT, Level.VIEW)
def labels(request):
    tag = request.GET.get("tag", "").strip()
    too_many = 0
    if tag:
        assets = [get_object_or_404(Asset.objects.select_related("device_model", "department", "tenant"), tag=tag)]
    else:
        qs = filter_assets(parse_asset_filters(request.GET, asset_filter_options())).select_related("tenant")
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

RULED_LINES = 8  # blank lines to write on: findings while a repair is open, or the steps when no PM procedure is on file
BLANK_ROWS = 2  # empty labor and parts rows while the work order is open


def checklist_steps(procedure) -> list[dict]:
    """A procedure's steps for the print. A step is a string, or {"text": ..., "measure": ...} when a reading is recorded;
    `measure` is what to record (e.g. "µA, limit 100"). Anything else in the JSON is printed as text rather than dropped."""
    steps = procedure.checklist if procedure is not None and isinstance(procedure.checklist, list) else []
    out = []
    for step in steps:
        if isinstance(step, dict):
            measure = step.get("measure")
            out.append({"text": str(step.get("text") or ""), "measured": measure not in (None, False, ""),
                        "measure": measure if isinstance(measure, str) else ""})
        else:
            out.append({"text": str(step), "measured": False, "measure": ""})
    return out


@web_view(Module.WORKORDERS, Level.VIEW)
def wo_print(request, number):
    # Tenant-scoped like the drawer's lookup (views._get_wo): another tenant's number is a 404.
    labor_lines = Prefetch("labor_lines", queryset=LaborLine.objects.select_related("technician").order_by("worked_on", "created_at"))
    part_lines = Prefetch("part_lines", queryset=PartLine.objects.order_by("created_at"))
    wo = get_object_or_404(WorkOrder.objects.select_related("asset", "asset__device_model", "asset__device_model__pm_procedure", "asset__department",
                                                            "asset__contract", "assigned_to", "alert").prefetch_related(labor_lines, part_lines),
                           number=number)
    today = date.today()
    is_open = wo.status in OPEN_STATUSES
    is_pm = wo.type == WoType.PM
    procedure = wo.asset.device_model.pm_procedure if is_pm else None
    labor = [(line, line.hours * line.rate) for line in wo.labor_lines.all()]
    parts = [(line, line.quantity * line.unit_cost) for line in wo.part_lines.all()]
    labor_total = sum((cost for _line, cost in labor), Decimal(0))
    parts_total = sum((cost for _line, cost in parts), Decimal(0))
    return render(request, "web/print_wo.html", {
        "wo": wo, "asset": wo.asset, "facility": request.tenant.name, "today": today, "is_open": is_open, "is_pm": is_pm,
        "past_due_days": (today - wo.due_on).days if is_open and wo.due_on < today else 0,
        "procedure": procedure, "steps": checklist_steps(procedure),
        "labor": labor, "parts": parts, "labor_hours": sum((line.hours for line, _cost in labor), Decimal(0)),
        "labor_total": labor_total, "parts_total": parts_total, "total": labor_total + parts_total,
        "timeline": wo_services.timeline(wo), "rules": range(RULED_LINES), "blank_rows": range(BLANK_ROWS) if is_open else range(0),
    })
