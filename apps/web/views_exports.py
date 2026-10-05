"""
CSV exports of the lists (slice 11): Equipment, Work orders, and Contracts, and one contract's covered devices.

Each list export reads the list screen's own query string (the Export links carry it: cadence.js `with-filters`), parses it with
that screen's parser, and writes every matching row in the screen's order, not one page. Parameters an export does not use (page,
mode, a drawer's tab) are ignored. Counts and costs are SQL annotations, so each export is a fixed number of queries whatever the
row count; the rows are read in chunks while the response streams (apps.web.exports.csv_response re-enters the tenant for that).

Work orders: the address may carry mode=board (the Export link drops it, the click-time address keeps it). Either way the export
is what the List view lists for the same address: the status filter and the open-only switch apply as they do there (open-only
is on unless the address says open=0). The board's toolbar sends neither, so exporting from the board gives the work orders in its
three open columns; the recent work in its completed and closed columns is in the export with open-only off, as in the List view.

Slice 16: a scoped user (apps.workorders.scoping) exports what their lists show, their own devices and work orders; the contract
exports are the facility's and refuse them.
"""
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DecimalField, F, FilteredRelation, OuterRef, Q, Subquery, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.accounts.models import Level, Module
from apps.contracts.models import Contract
from apps.contracts.services import contract_status, filter_contracts
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus
from apps.equipment.services import FleetBucket, filter_assets
from apps.recalls.services import alert_label
from apps.workorders import scoping
from apps.workorders.models import LABOR_AMOUNT, OPEN_STATUSES, PART_AMOUNT, LaborLine, PartLine, WorkOrder
from apps.workorders.services import filter_work_orders

from .decorators import web_view
from .exports import csv_response
from .forms import asset_filter_options, parse_asset_filters, parse_work_order_filters
from .forms_contracts import parse_contract_filters
from .templatetags.scoping_tags import scoped_text
from .views import scoped_assets

CHUNK = 500  # rows fetched per round trip while streaming
CENT = Decimal("0.01")
AMOUNT = DecimalField(max_digits=18, decimal_places=4)


def _filename(what: str, today: date) -> str:
    return f"cadence-{what}-{today:%Y-%m-%d}.csv"


def _money(value) -> Decimal:
    """A summed amount as plain money: 2 places, no currency sign. Nothing summed is 0.00."""
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def _share(annual_cost, acquisition_cost, covered_cost, expired: bool) -> float:
    """A device's slice of its contract's annual cost, by acquisition cost: Contract.cost_share_for, from values already loaded."""
    if expired or not covered_cost:
        return 0.0
    return float(annual_cost) * float(acquisition_cost) / float(covered_cost)


def _covered_cost():
    """Total acquisition cost of the outer contract's covered (not retired) devices: what Contract.cost_share_for divides by."""
    return Subquery(Asset.objects.filter(contract=OuterRef("pk")).exclude(status=AssetStatus.RETIRED).order_by()
                    .values("contract").annotate(s=Sum("acquisition_cost")).values("s"))


# --- Equipment -------------------------------------------------------------------------------------

EQUIPMENT_COLUMNS = ["Tag", "Serial", "Manufacturer", "Model", "Description", "Category", "Risk class", "Department", "Room", "Status", "Support",
                     "Contract", "Contract end", "Contract expired", "Installed", "Acquisition cost", "Warranty end", "Last PM", "Next PM",
                     "Fleet state", "Open work orders"]


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def equipment_csv(request):
    today = timezone.localdate()
    mine = scoped_assets(request.user)
    f = parse_asset_filters(request.GET, asset_filter_options(mine))
    # A scoped user's count of open work orders is of their own, as the device drawer counts them.
    open_wos = (scoping.work_orders(request.user, WorkOrder.objects.filter(asset=OuterRef("pk"), status__in=OPEN_STATUSES)).order_by()
                .values("asset").annotate(n=Count("id")).values("n"))
    assets = filter_assets(f, today, qs=mine).annotate(open_wos=Subquery(open_wos))

    def rows():
        for a in assets.iterator(chunk_size=CHUNK):
            dm = a.device_model
            yield [a.tag, a.serial, dm.manufacturer, dm.model, dm.description, dm.category, dm.get_risk_class_display(), a.department.name, a.room,
                   # The screen marks an ended contract "expired"; the file says so too, or the Support column reads as covered.
                   a.get_status_display(), a.get_support_type_display(), a.contract.reference if a.contract_id else "",
                   a.contract.end_on if a.contract_id else None, (a.contract.end_on < today) if a.contract_id else None, a.installed_on,
                   a.acquisition_cost, a.warranty_end, a.last_pm_on, a.next_pm_on, FleetBucket(a.bucket).label, a.open_wos or 0]

    return csv_response(_filename("equipment", today), EQUIPMENT_COLUMNS, rows())


# --- Work orders -----------------------------------------------------------------------------------

WORKORDER_COLUMNS = ["Number", "Type", "Priority", "Status", "Device tag", "Manufacturer", "Model", "Category", "Department", "Problem", "Requester",
                     "Source", "Assigned to", "Opened", "Due", "Started", "Completed", "Past due", "Estimated hours", "Labor hours", "Labor cost",
                     "Parts cost", "Total cost", "Recall"]


def _line_sum(model, expression):
    """One work order's total over its labor or part lines. A subquery each: joining both line tables at once would multiply rows."""
    return Subquery(model.objects.filter(work_order=OuterRef("pk")).order_by().values("work_order").annotate(s=Sum(expression)).values("s"))


def _assigned_to(wo) -> str:
    if wo.vendor_service:
        return f"Vendor: {wo.vendor_name}" if wo.vendor_name else "Vendor"
    return wo.assigned_to.name if wo.assigned_to_id else ""


@web_view(Module.WORKORDERS, Level.VIEW, scoped=True)
def workorders_csv(request):
    today = timezone.localdate()
    # The same technicians the list's Assigned filter offers (active ones), so an id the list would drop is dropped here too.
    f = parse_work_order_filters(request.GET, {str(pk) for pk in Technician.objects.filter(is_active=True).values_list("pk", flat=True)})
    wos = (filter_work_orders(f, qs=scoping.work_orders(request.user))  # a scoped user's own work orders, as their list shows
           .prefetch_related(None).select_related("alert").defer("alert__raw")  # the notice's source record is not exported
           .annotate(labor_hours=_line_sum(LaborLine, "hours"),
                     labor_amount=_line_sum(LaborLine, LABOR_AMOUNT),  # each line to the cent, as the drawer and the print
                     parts_amount=_line_sum(PartLine, PART_AMOUNT)))

    user, seen = request.user, {}  # a scoped user's problem texts name no work order outside their share (scoping_tags.scoped_text)

    def rows():
        for w in wos.iterator(chunk_size=CHUNK):
            a = w.asset
            dm = a.device_model
            labor, parts = w.labor_amount or 0, w.parts_amount or 0
            yield [w.number, w.get_type_display(), w.get_priority_display(), w.get_status_display(), a.tag, dm.manufacturer, dm.model, dm.category,
                   a.department.name, scoped_text(user, w.problem, seen), w.requester, w.get_source_display(), _assigned_to(w), w.opened_on,
                   w.due_on, w.started_on, w.completed_on, w.status in OPEN_STATUSES and w.due_on < today, w.estimated_hours, _money(w.labor_hours),
                   _money(labor), _money(parts), _money(Decimal(labor) + Decimal(parts)), alert_label(w.alert) if w.alert_id else ""]

    return csv_response(_filename("work-orders", today), WORKORDER_COLUMNS, rows())


# --- Contracts -------------------------------------------------------------------------------------

CONTRACT_COLUMNS = ["Reference", "Vendor", "Type", "Coverage", "Status", "Start", "End", "Annual cost", "Device tag", "Manufacturer", "Model",
                    "Department", "Device status", "Allocated annual cost"]
STATUS_LABELS = dict(AssetStatus.choices)  # the device columns are annotations, not an Asset, so there is no get_status_display()


@web_view(Module.CONTRACTS, Level.VIEW)
def contracts_csv(request):
    today = timezone.localdate()
    listed = filter_contracts(parse_contract_filters(request.GET), today)
    # One row per covered device (a contract without any still gets one row): the listed contracts joined to their covered devices,
    # in the list's order and then the drawer's (model, tag).
    rows_qs = (Contract.objects.filter(pk__in=listed.values("pk"))
               .annotate(device=FilteredRelation("assets", condition=~Q(assets__status=AssetStatus.RETIRED)), covered_cost=_covered_cost(),
                         d_id=F("device__id"), d_tag=F("device__tag"), d_manufacturer=F("device__device_model__manufacturer"),
                         d_model=F("device__device_model__model"), d_department=F("device__department__name"), d_status=F("device__status"),
                         d_cost=F("device__acquisition_cost"))
               .order_by(*listed.query.order_by, "device__device_model__model", "device__tag"))

    def rows():
        for c in rows_qs.iterator(chunk_size=CHUNK):
            row = [c.reference, c.vendor, c.get_type_display(), c.get_coverage_display(), contract_status(c, today)["label"], c.start_on, c.end_on,
                   c.annual_cost]
            if c.d_id is None:
                yield row + [None] * 6
            else:
                yield row + [c.d_tag, c.d_manufacturer, c.d_model, c.d_department, STATUS_LABELS.get(c.d_status, c.d_status),
                             _share(c.annual_cost, c.d_cost, c.covered_cost, c.end_on < today)]

    return csv_response(_filename("contracts", today), CONTRACT_COLUMNS, rows())


DEVICE_COLUMNS = ["Tag", "Serial", "Manufacturer", "Model", "Category", "Department", "Room", "Status", "Acquisition cost", "Allocated annual cost",
                  "Next PM"]


def _file_part(text: str) -> str:
    """Text safe inside a download's file name: ASCII letters, digits, dot, dash, and underscore only."""
    return re.sub(r"[^A-Za-z0-9._-]", "", text) or "contract"


@web_view(Module.CONTRACTS, Level.VIEW)
def contract_devices_csv(request, pk):
    today = timezone.localdate()
    contract = get_object_or_404(Contract.objects, pk=pk)
    devices = contract.covered_assets().select_related("device_model", "department").order_by("device_model__model", "tag")
    covered_cost = devices.aggregate(s=Sum("acquisition_cost"))["s"]
    expired = contract.end_on < today

    def rows():
        for a in devices.iterator(chunk_size=CHUNK):
            dm = a.device_model
            yield [a.tag, a.serial, dm.manufacturer, dm.model, dm.category, a.department.name, a.room, a.get_status_display(), a.acquisition_cost,
                   _share(contract.annual_cost, a.acquisition_cost, covered_cost, expired), a.next_pm_on]

    return csv_response(f"cadence-{_file_part(contract.reference)}-devices-{today:%Y-%m-%d}.csv", DEVICE_COLUMNS, rows())
