"""
Custom reports (slice 18): the reports a facility builds for itself (the mock's "Custom report" button on Reports). This module
declares what each source offers, checks a definition, runs it, and holds the services that create, change, and delete one.

A definition is one source, the columns to show, filters, an optional grouping, and a sort (apps.reports.models.CustomReport). It
arrives from a form and is stored as JSON, so it can only ever name what SOURCES declares here: a column is a key of the source's
registry with its own expression, a filter is a key with its own lookup, and nothing else is read. A definition naming anything
outside the registry is refused (clean_definition); it never reaches another field, relation, or table.

The rules:
- No free text a requester or the public typed: a work order's problem, resolution, notes, requester, callback, or reported
  location, and a device's notes, are never offered. Reports are emailed, and the product never risks carrying patient details
  there (CLAUDE.md, "No PHI"). Nor contract prices (they need Contracts View elsewhere), nor anything about users beyond a
  technician's name.
- Sources: work orders, devices, labor lines, part lines. Running one needs View on what it lists (apps/reports/permissions.py).
- Columns: 1 to MAX_COLUMNS, each once, shown in the registry's order. Coded values (type, status, priority, risk class, ...) show
  their labels and sort in their declared order (priority: critical first), text sorts in any letter case, empty values last.
- Filters: multi-select choices, departments (by id, in this facility), categories (this facility's), technicians, yes/no, and one
  date range on one of the source's date columns: a relative period resolved against the run's `today` (so a scheduled report rolls
  forward: the last 30 days are the 30 days ending the day it runs) or fixed from/to dates (YEAR_MIN to YEAR_MAX).
- Grouping by one groupable column, or the month of a date column ("Completed (month)"): the table is the group, a Count, and each
  chosen column that adds up (sums for money, hours, quantities, and counts; averages for days, age, and condition; the share of yes
  for PM on time). Other chosen columns are left out (the result says which). Groups sort by the chosen sort, by default the group.
- Money is to the cent the way the rest of the product adds it: each labor and part line rounded (LABOR_AMOUNT, PART_AMOUNT), and a
  work order's cost the sum of its rounded lines, so a custom report agrees with the work order drawer and the standard reports.
  Everything aggregates in the database (annotations and correlated subqueries: no query per row), the same on SQLite and PostgreSQL.
- At most MAX_ROWS rows are listed (the CSV and the email attachment), with the total and a truncated flag; the screen shows the
  first SCREEN_ROWS of them (apps.web.reports_custom).
- A saved definition that no longer fits the registry (a column dropped in a later release) still runs: unknown parts are skipped
  (_lenient), never a server error.

Running returns what the standard reports return (apps.reports.services, REPORTS): "columns" (labels) and "rows" (plain values: str,
int, float, date, bool, None; money as float, as the standard reports return it), plus what the screen needs: "kinds" (for alignment
and money), "total", "truncated", "grouped", "records", "totals", "left_out", and "description" (the source, filters, grouping, and
sort in words).

Services: create_custom_report, update_custom_report, delete_custom_report. Names are trimmed, 1 to NAME_MAX characters, unique in
the facility in any letter case. Deleting one also removes its email subscriptions. Every change is in the report's history, with
who made it. Who may do each is apps/reports/permissions.py, checked by the views; a builder may only build on what they can see.
"""
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import (
    Avg,
    Case,
    CharField,
    Count,
    DateField,
    DecimalField,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OrderBy,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Lower, NullIf, TruncDate, TruncMonth
from django.utils import timezone
from django.utils.text import slugify

from apps.core.expressions import DayNumber
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass, SupportType
from apps.pm.dates import month_bounds
from apps.pm.services import RETIRED_AND_CANCELLED
from apps.tenants.context import get_current_tenant
from apps.workorders.models import (
    LABOR_AMOUNT,
    OPEN_STATUSES,
    PART_AMOUNT,
    LaborLine,
    PartLine,
    PmResult,
    Priority,
    WorkOrder,
    WoStatus,
    WoType,
)
from apps.workorders.models import Source as Origin

from . import permissions as perms
from .models import CustomReport, ReportSubscription
from .services import REPORT_KEYS

Source = CustomReport.Source
TEMPLATE = "web/_report_custom.html"  # a custom report's panel on the Reports screen and its print page
MAX_ROWS = 10_000  # listed: the CSV, the email attachment
SCREEN_ROWS = 500  # shown on the screen and the print page (apps.web.reports_custom)
PREVIEW_ROWS = 50  # the builder's Preview
MAX_COLUMNS = 20
MAX_FILTER_VALUES = 200
NAME_MAX = CustomReport._meta.get_field("name").max_length
YEAR_MIN, YEAR_MAX = 1950, 2100
TWELVE_MONTHS = 365  # "in the last 12 months": the 365 days ending today

# Column kinds: how a value aligns and reads. Numbers align right; money shows a dollar sign.
TEXT, NUMBER, MONEY, HOURS, QUANTITY, DAYS, YEARS, DATE, YESNO, PERCENT = (
    "text", "number", "money", "hours", "quantity", "days", "years", "date", "yesno", "percent")
NUMERIC_KINDS = {NUMBER, MONEY, HOURS, QUANTITY, DAYS, YEARS, PERCENT}
# How a column adds up in a grouped table; "" leaves it out.
SUM, AVG, SHARE = "sum", "avg", "share"

_MONEY = DecimalField(max_digits=14, decimal_places=2)
_DECIMAL = DecimalField(max_digits=14, decimal_places=2)  # hours and quantities
_INT = IntegerField()
_EPOCH = date(1970, 1, 1)


def _day_number(day: date) -> Value:
    return Value((day - _EPOCH).days, output_field=IntegerField())


_NONE_INT = Value(None, output_field=IntegerField())


# --- the registry -----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str
    value: object  # an ORM path, or a function of the run's `today` returning an expression
    group: bool = False  # the report can group by it
    agg: str = ""  # SUM, AVG, SHARE: how it adds up in a grouped table; "" leaves it out
    choices: type | None = None  # TextChoices of a coded value: shown by label, sorted in declared order
    month: bool = False  # a date the report can group by month
    blank: str = "Not set"  # a group's label when the value is empty


@dataclass(frozen=True)
class Filter:
    key: str
    label: str
    kind: str  # "choice", "yesno", "department", "category", "technician"
    lookup: str
    choices: tuple = ()  # (value, label) for "choice"
    vendor: bool = False  # "technician": also offers "Vendor time" (a line naming no technician)


@dataclass(frozen=True)
class SourceSpec:
    key: str
    label: str
    noun: str  # "312 work orders"
    model: type
    columns: tuple
    filters: tuple
    dates: tuple  # keys of the date columns a period can apply to
    defaults: tuple  # the columns a new report starts with
    order: tuple  # the source's own order, when no sort is chosen

    def column(self, key):
        return next((c for c in self.columns if c.key == key), None)

    def filter(self, key):
        return next((f for f in self.filters if f.key == key), None)


def _choices(cls) -> tuple:
    return tuple(cls.choices)


YES_NO = (("yes", "Yes"), ("no", "No"))
VENDOR = "vendor"  # the technician filter's "Vendor time"
VENDOR_LABEL = "Vendor time"


def _sum_lines(model, amount, link: str, *conditions, **filters):
    """The sum of `amount` over `model`'s lines whose `link` is the outer row: one correlated subquery, 0 when there are none."""
    qs = (model.objects.filter(*conditions, **{link: OuterRef("pk")}, **filters).order_by().values(link)
          .annotate(s=Sum(amount, output_field=_MONEY)).values("s"))
    return Coalesce(Subquery(qs, output_field=_MONEY), Value(0), output_field=_MONEY)


def _count(model, link: str, *conditions, **filters):
    qs = model.objects.filter(*conditions, **{link: OuterRef("pk")}, **filters).order_by().values(link).annotate(n=Count("pk")).values("n")
    return Coalesce(Subquery(qs, output_field=_INT), Value(0), output_field=_INT)


def _labor_cost(today):
    return _sum_lines(LaborLine, LABOR_AMOUNT, "work_order")


def _parts_cost(today):
    return _sum_lines(PartLine, PART_AMOUNT, "work_order")


def _labor_hours(today):
    qs = LaborLine.objects.filter(work_order=OuterRef("pk")).order_by().values("work_order").annotate(s=Sum("hours")).values("s")
    return Coalesce(Subquery(qs, output_field=_DECIMAL), Value(0), output_field=_DECIMAL)


def _total_cost(today):
    return ExpressionWrapper(_labor_cost(today) + _parts_cost(today), output_field=_MONEY)


def _days_open(today):
    """Completed work: opened to completed (its turnaround). Open work: opened to today. Cancelled work has none."""
    return Case(When(status=WoStatus.CANCELLED, then=_NONE_INT),
                When(completed_on__isnull=False, then=DayNumber("completed_on") - DayNumber("opened_on")),
                default=_day_number(today) - DayNumber("opened_on"), output_field=IntegerField())


def _pm_on_time(today):
    """A PM done on or before its due date: yes. Done late, or not done and past due today: no; a PM cancelled on a device still
    in use was missed, so it is no once past due too. Not due yet, cancelled while its device was retired, or not a PM: empty. The rule
    of the PM completion KPI (apps.pm.services.pm_due_queryset, RETIRED_AND_CANCELLED), so the share agrees with the Overview."""
    return Case(When(~Q(type=WoType.PM) | RETIRED_AND_CANCELLED, then=_NONE_INT),
                When(completed_on__isnull=False, completed_on__lte=F("due_on"), then=Value(1)),
                When(completed_on__isnull=False, then=Value(0)),
                When(due_on__lt=today, then=Value(0)),
                default=_NONE_INT, output_field=IntegerField())


def _vendor_service(today):
    return Case(When(vendor_service=True, then=Value(1)), default=Value(0), output_field=IntegerField())


def _assigned_to(today):
    """The vendor's name for vendor service (as work orders name it), else the technician's, else Unassigned."""
    return Case(When(vendor_service=True, vendor_name="", then=Value("Vendor service")),
                When(vendor_service=True, then=F("vendor_name")),
                When(assigned_to__isnull=False, then=F("assigned_to__name")),
                default=Value("Unassigned"), output_field=CharField())


def _labor_who(today):
    """As the work order drawer names a line: its technician, else the work order's vendor (vendor time names no technician)."""
    return Case(When(technician__isnull=False, then=F("technician__name")),
                When(work_order__vendor_name="", then=Value("Vendor service")),
                default=F("work_order__vendor_name"), output_field=CharField())


def _age(today):
    """Years since installed, as Asset.age_years counts them (days / 365.25)."""
    return Cast(_day_number(today) - DayNumber("installed_on"), FloatField()) / Value(365.25)


def _pm_interval(today):
    """The interval in force, as DeviceModel.pm_interval_months decides it: a model excluded from AEM (life support, or marked as
    CMS keeps on the manufacturer's schedule: aem_excluded) keeps the manufacturer's; others take an approved AEM interval when there
    is one."""
    oem = F("device_model__oem_pm_interval_months")
    return Case(When(device_model__risk_class=RiskClass.LIFE_SUPPORT, then=oem), When(device_model__oem_schedule_required=True, then=oem),
                default=Coalesce("device_model__aem_interval_months", "device_model__oem_pm_interval_months"), output_field=IntegerField())


def _oem_schedule(today):
    """Imaging, radiologic, or medical laser equipment the facility marked: CMS keeps it on the manufacturer's schedule."""
    return Case(When(device_model__oem_schedule_required=True, then=Value(1)), default=Value(0), output_field=IntegerField())


def _open_work_orders(today):
    return _count(WorkOrder, "asset", status__in=OPEN_STATUSES)


def _repairs_12mo(today):
    return _count(WorkOrder, "asset", ~Q(status=WoStatus.CANCELLED), type=WoType.REPAIR,
                  opened_on__gte=today - timedelta(days=TWELVE_MONTHS), opened_on__lte=today)


def _service_cost_12mo(today):
    """Labor and parts of the device's work orders completed in the last 12 months (the cost reports' rule: by completion)."""
    window = {"work_order__completed_on__gte": today - timedelta(days=TWELVE_MONTHS), "work_order__completed_on__lte": today}
    return ExpressionWrapper(_sum_lines(LaborLine, LABOR_AMOUNT, "work_order__asset", **window)
                             + _sum_lines(PartLine, PART_AMOUNT, "work_order__asset", **window), output_field=_MONEY)


def _line_amount(expr):
    return lambda today: expr


def _recorded(today):
    return TruncDate("created_at")  # the facility's local day the part was recorded on (TruncDate works in its time zone, active inside it)


def _device_columns(prefix: str) -> list:
    """The device a work order (or its line) is on."""
    return [
        Column("tag", "Device tag", TEXT, f"{prefix}tag"),
        Column("device", "Device", TEXT, f"{prefix}device_model__description", group=True),
        Column("manufacturer", "Manufacturer", TEXT, f"{prefix}device_model__manufacturer", group=True),
        Column("model", "Model", TEXT, f"{prefix}device_model__model", group=True),
        Column("category", "Category", TEXT, f"{prefix}device_model__category", group=True),
        Column("department", "Department", TEXT, f"{prefix}department__name", group=True),
    ]


def _place_filters(prefix: str) -> list:
    return [Filter("department", "Department", "department", f"{prefix}department"),
            Filter("category", "Category", "category", f"{prefix}device_model__category")]


def _work_order_columns() -> list:
    return [
        Column("wo_number", "Work order", TEXT, "work_order__number", group=True),
        Column("wo_type", "Work order type", TEXT, "work_order__type", group=True, choices=WoType),
        Column("wo_completed", "Work order completed", DATE, "work_order__completed_on", month=True, blank="Not completed"),
    ]


WORK_ORDERS = SourceSpec(
    key=Source.WORK_ORDERS, label="Work orders", noun="work orders", model=WorkOrder,
    columns=(
        Column("number", "Number", TEXT, "number"),
        Column("type", "Type", TEXT, "type", group=True, choices=WoType),
        Column("status", "Status", TEXT, "status", group=True, choices=WoStatus),
        Column("priority", "Priority", TEXT, "priority", group=True, choices=Priority),
        Column("origin", "Opened from", TEXT, "source", group=True, choices=Origin),
        *_device_columns("asset__"),
        Column("risk_class", "Risk class", TEXT, "asset__device_model__risk_class", group=True, choices=RiskClass),
        Column("opened", "Opened", DATE, "opened_on", month=True),
        Column("due", "Due", DATE, "due_on", month=True),
        Column("started", "Started", DATE, "started_on", blank="Not started"),
        Column("completed", "Completed", DATE, "completed_on", month=True, blank="Not completed"),
        Column("days_open", "Days open", DAYS, _days_open, agg=AVG),
        Column("assigned_to", "Assigned to", TEXT, _assigned_to, group=True),
        Column("vendor_service", "Vendor service", YESNO, _vendor_service, group=True),
        Column("labor_hours", "Labor hours", HOURS, _labor_hours, agg=SUM),
        Column("labor_cost", "Labor cost", MONEY, _labor_cost, agg=SUM),
        Column("parts_cost", "Parts cost", MONEY, _parts_cost, agg=SUM),
        Column("total_cost", "Total cost", MONEY, _total_cost, agg=SUM),
        Column("pm_on_time", "PM on time", YESNO, _pm_on_time, agg=SHARE),
        Column("pm_result", "PM result", TEXT, "pm_result", group=True, choices=PmResult),
    ),
    filters=(
        Filter("type", "Type", "choice", "type", _choices(WoType)),
        Filter("status", "Status", "choice", "status", _choices(WoStatus)),
        Filter("priority", "Priority", "choice", "priority", _choices(Priority)),
        Filter("origin", "Opened from", "choice", "source", _choices(Origin)),
        Filter("risk_class", "Risk class", "choice", "asset__device_model__risk_class", _choices(RiskClass)),
        Filter("vendor_service", "Vendor service", "yesno", "vendor_service", YES_NO),
        Filter("technician", "Assigned technician", "technician", "assigned_to"),
        *_place_filters("asset__"),
    ),
    dates=("opened", "due", "started", "completed"),
    defaults=("number", "type", "status", "priority", "tag", "device", "department", "opened", "completed", "assigned_to", "total_cost"),
    order=("-opened_on", "-number"),
)

DEVICES = SourceSpec(
    key=Source.DEVICES, label="Devices", noun="devices", model=Asset,
    columns=(
        Column("tag", "Tag", TEXT, "tag"),
        Column("serial", "Serial", TEXT, "serial"),
        *_device_columns("")[1:],
        Column("room", "Room", TEXT, "room"),
        Column("status", "Status", TEXT, "status", group=True, choices=AssetStatus),
        Column("risk_class", "Risk class", TEXT, "device_model__risk_class", group=True, choices=RiskClass),
        Column("support_type", "Support", TEXT, "support_type", group=True, choices=SupportType),
        Column("contract", "Contract", TEXT, "contract__reference", group=True, blank="No contract"),
        Column("contract_vendor", "Contract vendor", TEXT, "contract__vendor", group=True, blank="No contract"),
        Column("contract_end", "Contract ends", DATE, "contract__end_on", month=True, blank="No contract"),
        Column("installed", "Installed", DATE, "installed_on", month=True),
        Column("age", "Age (years)", YEARS, _age, agg=AVG),
        Column("acquisition_cost", "Acquisition cost", MONEY, "acquisition_cost", agg=SUM),
        Column("condition", "Condition (1 to 5)", NUMBER, "condition", agg=AVG),
        Column("warranty_end", "Warranty ends", DATE, "warranty_end", month=True),
        Column("last_pm", "Last PM", DATE, "last_pm_on", month=True),
        Column("next_pm", "Next PM", DATE, "next_pm_on", month=True, blank="Not scheduled"),
        Column("pm_interval", "PM interval (months)", NUMBER, _pm_interval),
        Column("oem_schedule", "On the manufacturer's schedule (CMS)", YESNO, _oem_schedule, group=True),
        Column("open_work_orders", "Open work orders", NUMBER, _open_work_orders, agg=SUM),
        Column("repairs_12mo", "Repairs, last 12 months", NUMBER, _repairs_12mo, agg=SUM),
        Column("service_cost_12mo", "Service cost, last 12 months", MONEY, _service_cost_12mo, agg=SUM),
    ),
    filters=(
        Filter("status", "Status", "choice", "status", _choices(AssetStatus)),
        Filter("risk_class", "Risk class", "choice", "device_model__risk_class", _choices(RiskClass)),
        Filter("support_type", "Support", "choice", "support_type", _choices(SupportType)),
        Filter("oem_schedule", "On the manufacturer's schedule (CMS)", "yesno", "device_model__oem_schedule_required", YES_NO),
        *_place_filters(""),
    ),
    dates=("installed", "warranty_end", "last_pm", "next_pm", "contract_end"),
    defaults=("tag", "device", "manufacturer", "model", "department", "status", "risk_class", "next_pm"),
    order=("tag",),
)

LABOR = SourceSpec(
    key=Source.LABOR, label="Labor (time logged)", noun="labor lines", model=LaborLine,
    columns=(
        Column("date", "Date worked", DATE, "worked_on", month=True),
        Column("who", "Technician or vendor", TEXT, _labor_who, group=True),
        Column("hours", "Hours", HOURS, "hours", agg=SUM),
        Column("rate", "Rate per hour", MONEY, "rate"),
        Column("amount", "Amount", MONEY, _line_amount(LABOR_AMOUNT), agg=SUM),
        *_work_order_columns(),
        *_device_columns("work_order__asset__"),
    ),
    filters=(
        Filter("technician", "Technician", "technician", "technician", vendor=True),
        Filter("wo_type", "Work order type", "choice", "work_order__type", _choices(WoType)),
        *_place_filters("work_order__asset__"),
    ),
    dates=("date", "wo_completed"),
    defaults=("date", "who", "hours", "rate", "amount", "wo_number", "wo_type", "tag"),
    order=("-worked_on", "work_order__number"),
)

PARTS = SourceSpec(
    key=Source.PARTS, label="Parts used", noun="part lines", model=PartLine,
    columns=(
        Column("recorded", "Recorded on", DATE, _recorded, month=True),
        Column("description", "Part", TEXT, "description", group=True),
        Column("part_number", "Part number", TEXT, "part_number", group=True),
        Column("quantity", "Quantity", QUANTITY, "quantity", agg=SUM),
        Column("unit_cost", "Unit cost", MONEY, "unit_cost"),
        Column("amount", "Amount", MONEY, _line_amount(PART_AMOUNT), agg=SUM),
        Column("po_number", "PO number", TEXT, "po_number"),
        *_work_order_columns(),
        *_device_columns("work_order__asset__"),
    ),
    filters=(
        Filter("wo_type", "Work order type", "choice", "work_order__type", _choices(WoType)),
        *_place_filters("work_order__asset__"),
    ),
    dates=("recorded", "wo_completed"),
    defaults=("recorded", "description", "part_number", "quantity", "unit_cost", "amount", "wo_number", "tag"),
    order=("-created_at",),
)

SOURCES = {s.key: s for s in (WORK_ORDERS, DEVICES, LABOR, PARTS)}

PERIODS = {
    "last_7": "Last 7 days", "last_30": "Last 30 days", "last_90": "Last 90 days", "last_365": "Last 365 days",
    "next_30": "Next 30 days", "next_90": "Next 90 days",
    "this_month": "This month", "last_month": "Last month", "this_year": "This year", "last_year": "Last year",
    "custom": "Between dates",
}
CUSTOM_PERIOD = "custom"


# --- what a source offers (the builder reads these) -------------------------------------------------------------------------

def spec_of(source) -> SourceSpec | None:
    return SOURCES.get(source) if isinstance(source, str) else None


def group_options(spec: SourceSpec) -> list[tuple[str, str]]:
    """(key, label) of every grouping: the groupable columns, and "<date> (month)" right after each date that groups by month."""
    out = []
    for c in spec.columns:
        if c.group:
            out.append((c.key, c.label))
        if c.month:
            out.append((f"{c.key}_month", f"{c.label} (month)"))
    return out


def _group_column(spec: SourceSpec, key: str) -> tuple[Column | None, bool]:
    """(column, by month) for a grouping key; (None, False) for one the source does not offer."""
    c = spec.column(key)
    if c is not None and c.group:
        return c, False
    if key.endswith("_month"):
        c = spec.column(key[: -len("_month")])
        if c is not None and c.month:
            return c, True
    return None, False


def group_label(spec: SourceSpec, key: str) -> str:
    c, month = _group_column(spec, key)
    return "" if c is None else f"{c.label} (month)" if month else c.label


def filter_options(f: Filter) -> list[tuple[str, str]]:
    """(value, label) a filter offers now: its choices, or this facility's departments, categories, or technicians."""
    if f.kind in ("choice", "yesno"):
        return list(f.choices)
    if f.kind == "department":
        return [(str(pk), name) for pk, name in Department.objects.order_by(Lower("name"), "pk").values_list("pk", "name")]
    if f.kind == "category":
        return [(c, c) for c in _categories()]
    if f.kind == "technician":
        techs = [(str(pk), name if active else f"{name} (inactive)")
                 for pk, name, active in Technician.objects.order_by("-is_active", Lower("name"), "pk").values_list("pk", "name", "is_active")]
        return techs + ([(VENDOR, VENDOR_LABEL)] if f.vendor else [])
    return []


CATEGORY_MAX = DeviceModel._meta.get_field("category").max_length


def _categories() -> list[str]:
    values = DeviceModel.objects.exclude(category="").order_by().values_list("category", flat=True).distinct()
    return sorted(set(values), key=str.casefold)


# --- periods ------------------------------------------------------------------------------------------------------------------

def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def period_range(date_filter: dict, today: date) -> tuple[date | None, date | None]:
    """The (from, to) days a date filter covers as of `today`, both inclusive; None leaves that end open. The last N days are
    the N days ending today, the next N the N days starting today; a month or a year is the whole of it."""
    period = date_filter.get("period")
    if period == CUSTOM_PERIOD:
        return _iso(date_filter.get("from")), _iso(date_filter.get("to"))
    if period.startswith("last_") and period[5:].isdigit():
        return today - timedelta(days=int(period[5:]) - 1), today
    if period.startswith("next_") and period[5:].isdigit():
        return today, today + timedelta(days=int(period[5:]) - 1)
    if period == "this_month":
        return month_bounds(today.year, today.month)
    if period == "last_month":
        return month_bounds(*_shift_month(today.year, today.month, -1))
    if period == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return None, None


def _iso(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


def day_text(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


# --- checking a definition ---------------------------------------------------------------------------------------------------

def _clean_columns(spec: SourceSpec, columns, errors: dict, strict: bool = True) -> list[str]:
    if isinstance(columns, str):
        columns = [columns]
    if not isinstance(columns, (list, tuple)):
        errors["columns"] = "Choose the columns to show."
        return [] if strict else list(spec.defaults)
    out = []
    for key in columns:
        if not isinstance(key, str) or spec.column(key) is None:
            if strict:
                errors["columns"] = f"A {spec.noun} report has no column “{key}”."
                return []
            continue
        if key in out:
            if strict:
                errors["columns"] = "Choose each column once."
                return []
            continue
        out.append(key)
    if strict and not out:
        errors["columns"] = "Choose at least one column."
    elif strict and len(out) > MAX_COLUMNS:
        errors["columns"] = f"Choose at most {MAX_COLUMNS} columns (this has {len(out)})."
    if not strict:
        out = out[:MAX_COLUMNS] or list(spec.defaults)
    return [c.key for c in spec.columns if c.key in out]  # the registry's order, as the builder shows them


def _filter_values(f: Filter, raw, strict: bool) -> tuple[list[str], str]:
    """(values, problem) for one filter. Lenient (a saved definition being run) drops what does not fit instead."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(v, str) for v in raw):
        return [], f"Choose {f.label.lower()} from the list."
    values = list(dict.fromkeys(v for v in raw if v != ""))
    if len(values) > MAX_FILTER_VALUES:
        if strict:
            return [], f"Choose at most {MAX_FILTER_VALUES} for {f.label.lower()}."
        values = values[:MAX_FILTER_VALUES]
    if f.kind in ("choice", "yesno"):
        allowed = {v for v, _ in f.choices}
        bad = [v for v in values if v not in allowed]
        if bad and strict:
            return [], f"“{bad[0]}” is not one of the choices for {f.label.lower()}."
        return [v for v, _ in f.choices if v in values], ""  # in the declared order, so a definition reads the same however it was sent
    if f.kind == "category":
        # Any category name, not only those in use now: a report saved with a category every model has since left (renamed in Edit
        # details) matches nothing, and the builder keeps showing it checked, so saving an edit never silently widens the report.
        # A name no model has filters nothing in, whatever is sent.
        if strict and any(len(v) > CATEGORY_MAX for v in values):
            return [], "Choose categories this facility has."
        return values, ""
    # department and technician: ids of this facility's rows (and "Vendor time" for labor)
    ids, out = [], []
    for v in values:
        if f.kind == "technician" and f.vendor and v == VENDOR:
            out.append(VENDOR)
            continue
        try:
            pk = uuid.UUID(v)
        except ValueError:
            if strict:
                return [], f"Choose {'departments' if f.kind == 'department' else 'technicians'} from this facility."
            continue
        ids.append(pk)
        out.append(str(pk))
    if strict and ids:
        model = Department if f.kind == "department" else Technician
        if model.objects.filter(pk__in=ids).count() != len(set(ids)):  # another facility's id is not found here
            return [], f"Choose {'departments' if f.kind == 'department' else 'technicians'} from this facility."
    out = list(dict.fromkeys(out))
    return [v for v in out if v != VENDOR] + [v for v in out if v == VENDOR], ""  # "Vendor time" after the technicians


def _clean_date(spec: SourceSpec, raw) -> tuple[dict | None, str]:
    """(the date filter, problem). No period means no date filter."""
    if not isinstance(raw, dict):
        return None, "Choose a date and a period."
    period = raw.get("period") or ""
    if not period:
        return None, ""
    if not isinstance(period, str) or period not in PERIODS:  # JSON from the API can send any type
        return None, "Choose a period from the list."
    field = raw.get("field") or ""
    if not isinstance(field, str) or field not in spec.dates:
        return None, "Choose which date the period applies to."
    out = {"field": field, "period": period}
    if period != CUSTOM_PERIOD:
        return out, ""
    days = {}
    for end in ("from", "to"):
        value = raw.get(end) or ""
        if value in ("", None):
            continue
        day = _iso(value)
        if day is None:
            return None, "Enter dates as year, month, and day (YYYY-MM-DD)."
        if not YEAR_MIN <= day.year <= YEAR_MAX:
            return None, f"Enter dates between {YEAR_MIN} and {YEAR_MAX}."
        days[end] = day
    if not days:
        return None, "Enter a start date, an end date, or both."
    if "from" in days and "to" in days and days["from"] > days["to"]:
        return None, "The start date must be on or before the end date."
    out.update({end: day.isoformat() for end, day in days.items()})
    return out, ""


def _clean_filters(spec: SourceSpec, filters, errors: dict, strict: bool = True) -> dict:
    if filters in (None, ""):
        return {}
    if not isinstance(filters, dict):
        errors["filters"] = "Choose filters from the list."
        return {}
    out = {}
    for key, raw in filters.items():
        if key == "date":
            d, problem = _clean_date(spec, raw)
            if problem and strict:
                errors["date"] = problem
            elif d:
                out["date"] = d
            continue
        f = spec.filter(key) if isinstance(key, str) else None
        if f is None:
            if strict:
                errors["filters"] = f"A {spec.noun} report has no filter “{key}”."
            continue
        values, problem = _filter_values(f, raw, strict)
        if problem and strict:
            errors[f"filter_{key}"] = problem
        elif values:
            out[key] = values
    # the registry's order, so a saved definition reads the same however it was sent
    order = [f.key for f in spec.filters] + ["date"]
    return {k: out[k] for k in order if k in out}


def _shown_keys(spec: SourceSpec, columns: list[str], group_by: str) -> list[str]:
    """What a report's table shows, by key: its columns, or for a grouped one the group, "count", and the columns that add up."""
    if not group_by:
        return list(columns)
    return [group_by, "count", *[k for k in columns if spec.column(k).agg]]


def _clean_group(spec: SourceSpec, group_by, errors: dict, strict: bool = True) -> str:
    if group_by in (None, ""):
        return ""
    if isinstance(group_by, str) and _group_column(spec, group_by)[0] is not None:
        return group_by
    if strict:
        errors["group_by"] = f"A {spec.noun} report cannot be grouped by “{group_by}”."
    return ""


def _clean_sort(spec: SourceSpec, sort, columns: list[str], group_by: str, errors: dict, strict: bool = True) -> str:
    if sort in (None, ""):
        return ""
    if not isinstance(sort, str):
        if strict:
            errors["sort"] = "Choose a column to sort by."
        return ""
    key = sort[1:] if sort.startswith("-") else sort
    if key in _shown_keys(spec, columns, group_by):
        return sort
    if strict:
        known = spec.column(key) is not None or key == "count" or _group_column(spec, key)[0] is not None
        errors["sort"] = "Sort by a column the report shows." if known else f"A {spec.noun} report has no column “{key}”."
    return ""


def clean_definition(source, columns, filters=None, group_by="", sort="") -> dict:
    """Check a definition against the registry and normalize it: {"source", "columns", "filters", "group_by", "sort"}. Raises
    ValidationError keyed by part ("source", "columns", "filters", "filter_<key>", "date", "group_by", "sort") with plain words."""
    spec = spec_of(source)
    if spec is None:
        raise ValidationError({"source": "Choose what the report lists: work orders, devices, labor, or parts."})
    errors: dict = {}
    cols = _clean_columns(spec, columns, errors)
    flt = _clean_filters(spec, filters, errors)
    group = _clean_group(spec, group_by, errors)
    srt = "" if "columns" in errors or "group_by" in errors else _clean_sort(spec, sort, cols, group, errors)
    if errors:
        raise ValidationError(errors)
    return {"source": spec.key, "columns": cols, "filters": flt, "group_by": group, "sort": srt}


def _lenient(source, columns, filters, group_by, sort) -> dict | None:
    """A saved definition as it can run today: what no longer fits the registry is skipped. None for a source no longer offered."""
    spec = spec_of(source)
    if spec is None:
        return None
    ignored: dict = {}
    cols = _clean_columns(spec, columns, ignored, strict=False)
    flt = _clean_filters(spec, filters, ignored, strict=False)
    group = _clean_group(spec, group_by, ignored, strict=False)
    return {"source": spec.key, "columns": cols, "filters": flt, "group_by": group, "sort": _clean_sort(spec, sort, cols, group, ignored, strict=False)}


def definition_of(report: CustomReport) -> dict:
    return {"source": report.source, "columns": report.columns, "filters": report.filters, "group_by": report.group_by, "sort": report.sort}


def runnable(report: CustomReport) -> dict | None:
    """The report's definition as it runs (see _lenient), the builder's starting point for Edit."""
    return _lenient(**definition_of(report))


# --- in words ------------------------------------------------------------------------------------------------------------------

def _value_labels(f: Filter, values: list[str]) -> list[str]:
    if f.kind in ("choice", "yesno"):
        labels = dict(f.choices)
        return [labels.get(v, v) for v in values]
    if f.kind == "category":
        return list(values)
    model = Department if f.kind == "department" else Technician
    names = {str(pk): name for pk, name in model.objects.filter(pk__in=[v for v in values if v != VENDOR]).values_list("pk", "name")}
    gone = "a removed department" if f.kind == "department" else "a removed technician"
    return [VENDOR_LABEL if v == VENDOR else names.get(v, gone) for v in values]


_SORT_WORDS = {TEXT: ("A to Z", "Z to A"), DATE: ("oldest first", "newest first"), YESNO: ("no first", "yes first")}


def _sort_words(spec: SourceSpec, sort: str, group_by: str) -> str:
    desc = sort.startswith("-")
    key = sort.lstrip("-")
    if key == "count":
        label, kind, coded = "Count", NUMBER, False
    elif group_by and key == group_by:
        c, month = _group_column(spec, key)
        label, kind, coded = group_label(spec, key), DATE if month else c.kind, bool(c.choices) and not month
    else:
        c = spec.column(key)
        label = _agg_label(c) if group_by else c.label
        kind, coded = (PERCENT if group_by and c.agg == SHARE else c.kind), bool(c.choices)
    if coded:
        words = ("in order", "in reverse order")
    else:
        words = _SORT_WORDS.get(kind, ("lowest first", "highest first"))
    return f"Sorted by {label}, {words[1] if desc else words[0]}"


def describe(spec: SourceSpec, d: dict, today: date | None = None) -> str:
    """The source, filters, grouping, and sort in words, e.g. "Work orders · Type: Corrective repair · Completed: last 30 days
    (Sep 3 to Oct 2, 2026) · Grouped by Department · Sorted by Total cost, highest first". With no `today`, a relative period is
    named without its dates (it moves with each run)."""
    parts = [spec.label]
    for key, values in d["filters"].items():
        if key == "date":
            col = spec.column(values["field"])
            if values["period"] == CUSTOM_PERIOD:
                start, end = period_range(values, today or timezone.localdate())
                when = (f"{day_text(start)} to {day_text(end)}" if start and end else f"on or after {day_text(start)}" if start
                        else f"on or before {day_text(end)}")
            else:
                when = PERIODS[values["period"]].lower()
                if today is not None:
                    start, end = period_range(values, today)
                    when += f" ({day_text(start)} to {day_text(end)})"
            parts.append(f"{col.label}: {when}")
            continue
        f = spec.filter(key)
        parts.append(f"{f.label}: {', '.join(_value_labels(f, values))}")
    if d["group_by"]:
        parts.append(f"Grouped by {group_label(spec, d['group_by'])}")
    if d["sort"]:
        parts.append(_sort_words(spec, d["sort"], d["group_by"]))
    return " · ".join(parts)


# --- running -------------------------------------------------------------------------------------------------------------------

def _expr(c: Column, today: date):
    return F(c.value) if isinstance(c.value, str) else c.value(today)


def _rank(path: str, choices) -> Case:
    """A coded value's place in its declared order, for sorting. A blank or unknown value has none (NULL), so it sorts with the
    empty values: last, in either direction."""
    return Case(*[When(**{path: v}, then=Value(i)) for i, v in enumerate(choices.values)], default=_NONE_INT, output_field=IntegerField())


def _filtered(spec: SourceSpec, filters: dict, today: date):
    qs = spec.model.objects.all()
    for key, values in filters.items():
        if key == "date":
            start, end = period_range(values, today)
            qs = qs.alias(cr_date=_expr(spec.column(values["field"]), today))
            if start:
                qs = qs.filter(cr_date__gte=start)
            if end:
                qs = qs.filter(cr_date__lte=end)
            continue
        f = spec.filter(key)
        if f.kind == "yesno":
            if set(values) == {"yes"}:
                qs = qs.filter(**{f.lookup: True})
            elif set(values) == {"no"}:
                qs = qs.filter(**{f.lookup: False})
            continue
        if f.kind == "technician":
            ids = [v for v in values if v != VENDOR]
            q = Q(**{f"{f.lookup}__in": ids}) if ids else Q(pk__in=[])
            if VENDOR in values:
                q |= Q(**{f"{f.lookup}__isnull": True})
            qs = qs.filter(q)
            continue
        qs = qs.filter(**{f"{f.lookup}__in": values})
    return qs


def _aggregate(c: Column, today: date):
    expr = _expr(c, today)
    if c.agg == SUM:
        return Sum(expr, output_field=_MONEY if c.kind == MONEY else _DECIMAL if c.kind in (HOURS, QUANTITY) else _INT)
    return Avg(expr, output_field=FloatField())  # AVG, and SHARE (the mean of 1 and 0, as a percent below)


def _agg_label(c: Column) -> str:
    if c.agg == SHARE:
        return f"{c.label} %"
    if c.agg == AVG:
        return f"Average {c.label[0].lower()}{c.label[1:]}"
    return c.label


def _plain(c: Column, v):
    """One value as the rows carry it: labels for coded values, float for decimals, bool for yes/no, None for empty."""
    if v is None:
        return None
    if c.choices is not None:
        return dict(c.choices.choices).get(v, v) if v != "" else None
    if c.kind == YESNO:
        return bool(v)
    if isinstance(v, Decimal):
        return float(v)
    if c.kind == TEXT:
        return v if v != "" else None
    return v


def _plain_agg(c: Column, v):
    if v is None:
        return None
    v = float(v) if isinstance(v, Decimal) else v
    return v * 100 if c.agg == SHARE else v


def _listed(spec: SourceSpec, d: dict, qs, today: date, limit: int) -> dict:
    cols = [spec.column(k) for k in d["columns"]]
    total = qs.count()
    aliases = {f"cr_{c.key}": _expr(c, today) for c in cols}
    order = []
    if d["sort"]:
        c = spec.column(d["sort"].lstrip("-"))
        # Text in any letter case, with blank text read as empty, so blanks sort last as missing values do (and as _grouped sorts them)
        key = _rank(c.value, c.choices) if c.choices else NullIf(Lower(F(f"cr_{c.key}")), Value("")) if c.kind == TEXT else F(f"cr_{c.key}")
        order.append(OrderBy(key, descending=d["sort"].startswith("-"), nulls_last=True))
    raw = qs.annotate(**aliases).order_by(*order, *spec.order, "pk").values_list(*aliases)[:limit]
    rows = [[_plain(c, v) for c, v in zip(cols, r)] for r in raw]
    totals = None
    summed = [c for c in cols if c.agg == SUM]
    if summed and total:
        sums = qs.aggregate(**{f"t_{c.key}": _aggregate(c, today) for c in summed})
        totals = [_plain_agg(c, sums[f"t_{c.key}"]) if c.agg == SUM else None for c in cols]
    return {"columns": [c.label for c in cols], "rows": rows, "kinds": [c.kind for c in cols], "keys": [c.key for c in cols],
            "total": total, "records": total, "truncated": total > limit, "grouped": False, "left_out": [], "totals": totals}


def _group_value_label(c: Column, month: bool, v) -> str:
    if v is None or v == "":
        return c.blank
    if month:
        return f"{v:%b %Y}"
    if c.choices is not None:
        return dict(c.choices.choices).get(v, v)
    if c.kind == YESNO:
        return "Yes" if v else "No"
    return str(v)


def _ordered(items: list, key, descending: bool) -> list:
    """`items` sorted by `key` (empty values last in either direction), keeping their order where keys tie."""
    present = [i for i in items if key(i) is not None]
    present.sort(key=key, reverse=descending)
    return present + [i for i in items if key(i) is None]


def _grouped(spec: SourceSpec, d: dict, qs, today: date, limit: int) -> dict:
    gcol, month = _group_column(spec, d["group_by"])
    gexpr = _expr(gcol, today)
    if month:
        gexpr = TruncMonth(gexpr, output_field=DateField())
    chosen = [spec.column(k) for k in d["columns"]]
    agg_cols = [c for c in chosen if c.agg]
    left_out = [c.label for c in chosen if not c.agg and not (c.key == gcol.key and not month)]
    def aggs() -> dict:
        return {"cr_n": Count("pk"), **{f"cr_{c.key}": _aggregate(c, today) for c in agg_cols}}

    groups = []
    for row in qs.annotate(cr_g=gexpr).values("cr_g").annotate(**aggs()).order_by():
        groups.append({"g": row["cr_g"], "label": _group_value_label(gcol, month, row["cr_g"]), "count": row["cr_n"],
                       **{c.key: _plain_agg(c, row[f"cr_{c.key}"]) for c in agg_cols}})

    # Groups are few (a column's distinct values): they sort here, by the chosen sort, else by the group in its own order.
    groups.sort(key=lambda g: g["label"].casefold())
    sort = d["sort"] or d["group_by"]
    key = sort.lstrip("-")
    if key == d["group_by"]:
        if month or gcol.kind in (DATE, YESNO):
            by = lambda g: g["g"]  # noqa: E731
        elif gcol.choices is not None:
            ranks = {v: i for i, v in enumerate(gcol.choices.values)}
            by = lambda g: None if g["g"] in (None, "") else ranks.get(g["g"], len(ranks))  # noqa: E731
        else:
            by = lambda g: None if g["g"] in (None, "") else str(g["g"]).casefold()  # noqa: E731
    else:
        by = lambda g: g[key]  # noqa: E731
    groups = _ordered(groups, by, sort.startswith("-"))

    total = len(groups)
    shown = groups[:limit]
    whole = qs.aggregate(**aggs())
    columns = [group_label(spec, d["group_by"]), "Count", *[_agg_label(c) for c in agg_cols]]
    kinds = [TEXT, NUMBER, *[PERCENT if c.agg == SHARE else c.kind for c in agg_cols]]
    return {
        "columns": columns, "kinds": kinds, "keys": [d["group_by"], "count", *[c.key for c in agg_cols]],
        "rows": [[g["label"], g["count"], *[g[c.key] for c in agg_cols]] for g in shown],
        "total": total, "records": whole["cr_n"], "truncated": total > limit, "grouped": True, "left_out": left_out,
        "totals": [None, whole["cr_n"], *[_plain_agg(c, whole[f"cr_{c.key}"]) for c in agg_cols]] if total else None,
    }


def _nothing(problem: str) -> dict:  # a report that cannot run: an empty table and why
    return {"columns": [], "rows": [], "kinds": [], "keys": [], "total": 0, "records": 0, "truncated": False, "grouped": False,
            "left_out": [], "totals": None, "description": "", "problem": problem, "source": "", "source_label": "", "noun": "rows",
            "limit": MAX_ROWS}


def run(definition: dict, today: date, limit: int | None = None) -> dict:
    """Run a definition as of `today` (in the current facility). Takes a clean definition or a saved one; what no longer fits the
    registry is skipped. At most `limit` rows (MAX_ROWS by default)."""
    limit = MAX_ROWS if limit is None else limit
    d = _lenient(definition.get("source"), definition.get("columns"), definition.get("filters"), definition.get("group_by"),
                 definition.get("sort"))
    if d is None:
        return _nothing("This report's source is no longer offered, so it lists nothing. Delete it or build it again.")
    spec = SOURCES[d["source"]]
    qs = _filtered(spec, d["filters"], today)
    table = _grouped(spec, d, qs, today, limit) if d["group_by"] else _listed(spec, d, qs, today, limit)
    return {**table, "description": describe(spec, d, today), "problem": "", "source": spec.key, "source_label": spec.label,
            "noun": spec.noun, "limit": limit, "definition": d}


# --- a facility's custom reports by key -----------------------------------------------------------------------------------------

def report_for_key(key) -> CustomReport | None:
    """The current facility's custom report whose key is `key` ("custom-<id>"), or None (another facility's is not found)."""
    if not isinstance(key, str) or not key.startswith(CustomReport.KEY_PREFIX):
        return None
    try:
        pk = uuid.UUID(key[len(CustomReport.KEY_PREFIX):])
    except ValueError:
        return None
    return CustomReport.objects.filter(pk=pk).first()


def file_stem(name: str) -> str:
    """The CSV's name for a custom report, from its name: "Pump repairs" downloads as cadence-pump-repairs-<day>.csv. Only
    letters, digits, and hyphens; never one of the standard reports' names."""
    stem = slugify(name)[:60].strip("-")
    if not stem:
        return "custom-report"
    return f"custom-{stem}" if stem in REPORT_KEYS else stem


def meta_of(report: CustomReport) -> dict:
    """The meta every place that shows or sends a report by key reads (apps.reports.services.find_report)."""
    spec = spec_of(report.source)
    d = runnable(report)
    return {"key": report.key, "title": report.name, "subtitle": describe(spec, d) if d else "Its source is no longer offered",
            "template": TEMPLATE, "custom": True, "source": report.source, "source_label": spec.label if spec else "",
            "needs": perms.needs(report.source), "file_stem": file_stem(report.name), "pk": report.pk}


def meta_for_key(key) -> dict | None:
    report = report_for_key(key)
    return None if report is None else meta_of(report)


def run_key(key: str, today: date) -> dict:
    report = report_for_key(key)
    if report is None:
        return _nothing("This report has been deleted.")
    return run(definition_of(report), today)


def menu() -> list[dict]:
    """The facility's custom reports for the Reports screen's list, by name in any letter case."""
    out = []
    for r in CustomReport.objects.order_by(Lower("name"), "pk").only("pk", "name", "source"):
        spec = spec_of(r.source)
        out.append({"key": r.key, "title": r.name, "subtitle": spec.label if spec else "Source no longer offered"})
    return out


# --- creating, changing, deleting ------------------------------------------------------------------------------------------------

EDITABLE = ("name", "source", "columns", "filters", "group_by", "sort")
LABELS = {"name": "name", "source": "source", "columns": "columns", "filters": "filters", "group_by": "grouping", "sort": "sort"}


def _clean_name(value, errors: dict, current: CustomReport | None = None) -> str:
    name = " ".join(str(value if value is not None else "").split())
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        errors["name"] = "Remove the invisible control character from the name."
    elif not name:
        errors["name"] = "Name the report."
    elif len(name) > NAME_MAX:
        errors["name"] = f"Keep the name to {NAME_MAX} characters."
    else:
        taken = CustomReport.objects.filter(name__iexact=name)
        if current is not None:
            taken = taken.exclude(pk=current.pk)
        if taken.exists():
            errors["name"] = f"Another custom report here is already called {name}."
    return name


def _check(name, definition: dict, by, current: CustomReport | None = None) -> tuple[str, dict]:
    """(name, clean definition), or one ValidationError with every problem, keyed by part."""
    errors: dict = {}
    clean_name = _clean_name(name, errors, current)
    clean = None
    try:
        clean = clean_definition(**definition)
    except ValidationError as e:
        errors.update({k: v[0] for k, v in e.message_dict.items()})
    if by is not None and "source" not in errors:
        refusal = perms.build_refusal(by, definition.get("source"))
        if refusal:
            errors["source"] = refusal
    if errors:
        raise ValidationError(errors)
    return clean_name, clean


def _check_tenant(report: CustomReport) -> None:
    tenant = get_current_tenant()
    if report is None or tenant is None or report.tenant_id != tenant.id:
        raise ValidationError("Choose a custom report from this facility.")


def _save(report: CustomReport, by, reason: str) -> None:
    """Save with the history's reason and author; two people saving the same name at once get the form's error."""
    report._change_reason = reason
    if by is not None:
        report._history_user = by
    try:
        with transaction.atomic():
            report.save()
    except IntegrityError:
        raise ValidationError({"name": f"Another custom report here is already called {report.name}."})
    finally:  # simple_history reads these on every save; never let them reach a later, unrelated one
        report.__dict__.pop("_change_reason", None)
        report.__dict__.pop("_history_user", None)


def create_custom_report(*, name, source, columns, filters=None, group_by="", sort="", by=None) -> CustomReport:
    """Add a custom report to the facility. `by`, when given, must be able to see what the source lists."""
    clean_name, d = _check(name, {"source": source, "columns": columns, "filters": filters, "group_by": group_by, "sort": sort}, by)
    report = CustomReport(name=clean_name, created_by=by, **d)
    _save(report, by, "Added")
    return report


def update_custom_report(report: CustomReport, *, by=None, **fields) -> CustomReport:
    """Change a custom report with create_custom_report's rules; parts not given keep their saved values. Only what differs is
    saved, with the history naming what changed."""
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    _check_tenant(report)
    merged = {**definition_of(report), **{k: v for k, v in fields.items() if k != "name"}}
    clean_name, d = _check(fields.get("name", report.name), merged, by, current=report)
    new = {"name": clean_name, **d}
    changed = [f for f in EDITABLE if getattr(report, f) != new[f]]
    if not changed:
        return report
    for f in changed:
        setattr(report, f, new[f])
    _save(report, by, "Edited: " + ", ".join(LABELS[f] for f in changed))
    return report


def delete_custom_report(report: CustomReport, *, by=None) -> int:
    """Delete a custom report and everyone's email subscriptions to it. Returns how many subscriptions went with it."""
    _check_tenant(report)
    with transaction.atomic():
        removed, _ = ReportSubscription.objects.filter(report=report.key).delete()
        report._change_reason = "Deleted"
        if by is not None:
            report._history_user = by
        report.delete()
    return removed
