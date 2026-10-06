"""
Labor and parts on a work order (slice 15): adding and removing labor lines (who, when, hours, the rate) and part lines
(description, part number, quantity, unit cost, PO), with the rates from Settings (apps.facility: labor_rate, vendor_labor_rate).
The only code that writes LaborLine and PartLine rows. The Reports' repair spend and cost of service, and the Overview's spend
tile, are built on these lines, so every rule below protects a number someone reports on.

Rules:
- A closed work order is the record: its lines are fixed until it is reopened (Approve). A cancelled one takes none. Open, in
  progress, awaiting parts, and completed work orders take lines (a completed one is waiting for review, and its paperwork may
  lag the work).
- Hours: a number above 0 and at most 24 on one line, at most 2 decimal places (more is refused, not rounded, as Settings does),
  and at most 24 for one technician on one day across all work orders (the time imported from the previous system aside: a line of
  it is a whole job's time on the day it was completed, never one day's work).
- The date worked is never in the future, never before the work order was opened, and never after it was completed.
- In-house work orders take an active technician of this facility: by default the one assigned, else the signed-in user's own
  technician record. Vendor service work orders take none (the vendor's time is the vendor's).
- The rate defaults to Settings: labor_rate in-house, vendor_labor_rate for vendor service. A different rate needs work-order
  Approve (apps/workorders/permissions.py can_set_rate; the view checks, this module takes the rate it is given). A line keeps
  the rate it was logged at: changing Settings later never reprices it.
- Parts: a short description, a quantity above 0 (2 decimals, at most QUANTITY_MAX), a unit cost of 0 or more (2 decimals, at most
  UNIT_COST_MAX), and an optional short part number and PO.
- Free text is one short line (no patient information: the screens say so).
- Another facility's work order, line, or technician is refused. Every add and remove is audited (simple_history: who, and
  "Added" or "Removed"), and the work order's timeline shows both.

Slice 23: the lines of work order history imported from the previous system are written here too, by history's rules
(add_imported_labor, add_imported_part, only on an imported work order; see "importing" below).
"""
import re
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.credentials.models import Technician
from apps.facility.services import RATE_MAX, decimal_places, get_settings

from .models import LaborLine, PartLine, Source, WorkOrder, WoStatus

HOURS_MAX = Decimal("24")  # one line
DAY_HOURS_MAX = Decimal("24")  # one technician, one day, every work order
QUANTITY_MAX = Decimal("9999")
UNIT_COST_MAX = Decimal("999999.99")
PLACES = 2  # hours, rate, quantity, and unit cost all keep cents (the columns' decimal places)
DESCRIPTION_MAX = 120
PART_NUMBER_MAX = 40
PO_NUMBER_MAX = 40
CHANGE_ADDED, CHANGE_REMOVED = "Added", "Removed"
CENT = Decimal("0.01")


# --- display helpers (the drawer, the timeline, and the toasts share them) -------------------------------------------------------

def cents(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def labor_amount(line) -> Decimal:
    return cents(line.hours * line.rate)


def part_amount(line) -> Decimal:
    return cents(line.quantity * line.unit_cost)


def plain(value) -> str:
    """A Decimal as a person writes it: 1.50 -> "1.5", 2.00 -> "2", 0.25 -> "0.25"."""
    d = Decimal(value)
    if d == d.to_integral_value():
        return str(d.to_integral_value())
    return format(d.normalize(), "f")


_THOUSANDS = re.compile(r"\d{1,3}(,\d{3})+(\.\d+)?")


def read_money(value: str) -> str:
    """A typed amount as the drawer's and the API's money boxes read it: a pasted "$1,250.00" loses the dollar sign, and a comma
    goes only as a thousands separator. Anything else ("1,5" meaning 1.5) reaches the service unchanged and is refused there, never
    read as a number ten times larger."""
    text = (value or "").strip().removeprefix("$").strip()
    return text.replace(",", "") if _THOUSANDS.fullmatch(text) else text


def money_text(value) -> str:
    return f"${cents(value):,.2f}"


def labor_event(hours, technician_name: str) -> str:
    """'1.5 h logged by Dana Whitfield', or '2 h vendor time logged' for a line without a technician."""
    return f"{plain(hours)} h logged by {technician_name}" if technician_name else f"{plain(hours)} h vendor time logged"


def part_event(description: str, quantity, unit_cost) -> str:
    """'Pump door latch × 2 ($84.00)'; a quantity of 1 is not spelled out."""
    times = "" if Decimal(quantity) == 1 else f" × {plain(quantity)}"
    return f"{description}{times} ({money_text(Decimal(quantity) * Decimal(unit_cost))})"


# --- reading -------------------------------------------------------------------------------------------------------------------

def locked_reason(wo) -> str:
    """Why lines cannot be added to or removed from `wo` now, or "" when they can."""
    if wo.status == WoStatus.CLOSED:
        return f"{wo.number} is closed: its labor and parts are the record. Reopen it to change them."
    if wo.status == WoStatus.CANCELLED:
        return f"{wo.number} was cancelled: it takes no labor or parts."
    return ""


def default_rate(wo, settings=None) -> Decimal:
    """The Settings rate a new line on `wo` is charged at: vendor service or in-house."""
    s = settings or get_settings()
    return s.vendor_labor_rate if wo.vendor_service else s.labor_rate


def default_technician(wo, by=None) -> Technician | None:
    """Who a new in-house line is for unless someone else is chosen: the assigned technician while active, else the signed-in
    user's own technician record. None for vendor service, or when neither exists."""
    if wo.vendor_service:
        return None
    if wo.assigned_to_id:
        assigned = Technician.objects.filter(pk=wo.assigned_to_id, is_active=True).first()
        if assigned is not None:
            return assigned
    if by is not None and getattr(by, "pk", None):
        return Technician.objects.filter(user_id=by.pk, is_active=True).first()
    return None


# --- parsing -------------------------------------------------------------------------------------------------------------------

def _decimal(value, field: str, errors: dict, *, required: str, low: Decimal, high: Decimal, low_inclusive: bool, range_message: str):
    """A number from a form or a caller: blank is `required`; more than PLACES decimals is refused (never rounded); outside the
    range is `range_message`. Returns the value at PLACES decimals, or None with the error recorded."""
    if value is None or (isinstance(value, str) and not value.strip()):
        errors[field] = required
        return None
    if isinstance(value, bool):
        errors[field] = "Enter a number."
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        errors[field] = "Enter a number."
        return None
    if not d.is_finite():
        errors[field] = "Enter a number."
        return None
    if decimal_places(d) > PLACES:
        errors[field] = f"Use at most {PLACES} decimal places."
        return None
    if (d < low if low_inclusive else d <= low) or d > high:  # compared before any arithmetic, so a huge exponent never overflows
        errors[field] = range_message
        return None
    return d.quantize(CENT) + 0  # + 0 drops a negative zero's sign


def _text(value, field: str, errors: dict, *, limit: int, what: str) -> str:
    """One line, stray whitespace collapsed; control characters refused (Postgres cannot store NUL, and none belong here)."""
    text = " ".join(str(value or "").split())
    if any(unicodedata.category(c) == "Cc" for c in text):
        errors[field] = "Remove the invisible control character from this text."
    elif len(text) > limit:
        errors[field] = f"Keep the {what} to {limit} characters."
    return text


def _date(value, errors: dict) -> date | None:
    if isinstance(value, datetime):
        value = timezone.localdate(value) if timezone.is_aware(value) else value.date()  # an aware moment: the facility's day
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    errors["worked_on"] = "Enter the date the work was done."
    return None


# --- writing -------------------------------------------------------------------------------------------------------------------

def _lock_work_order(pk) -> WorkOrder:
    """The work order as it is now, locked for this transaction (a no-op on SQLite), refused when another facility's (the scoped
    manager cannot see it) or when its status takes no lines."""
    current = WorkOrder.objects.select_for_update().filter(pk=pk).first()
    if current is None:
        raise ValidationError("That work order is not in this facility.")
    reason = locked_reason(current)
    if reason:
        raise ValidationError(reason)
    return current


def _technician(wo: WorkOrder, technician, by, errors: dict) -> Technician | None:
    """The technician a new line on `wo` is for, locked so two lines for one person and day are checked one after the other."""
    if wo.vendor_service:
        if technician is not None:
            errors["technician"] = "Vendor service time is logged without a technician."
        return None
    if technician is None:
        technician = default_technician(wo, by)
        if technician is None:
            errors["technician"] = "Choose who did the work."
            return None
    tech = Technician.objects.select_for_update().filter(pk=technician.pk).first()
    if tech is None:
        errors["technician"] = "Choose a technician from this facility."
    elif not tech.is_active:
        errors["technician"] = f"{tech.name} is no longer active. Choose another technician."
        return None
    return tech


def _save(line, by, reason: str, at=None) -> None:
    """Save `line` with its audit record: who (`by`), why (`reason`), and when (`at`, only for an imported line: the day the work
    was done; otherwise now)."""
    line._change_reason = reason
    if by is not None:
        line._history_user = by
    if at is not None:
        line._history_date = at
    try:
        line.save()
    finally:  # simple_history reads these on every save; never let them reach a later, unrelated one
        line.__dict__.pop("_change_reason", None)
        line.__dict__.pop("_history_user", None)
        line.__dict__.pop("_history_date", None)


def _delete(line, by) -> None:
    line._change_reason = CHANGE_REMOVED
    if by is not None:
        line._history_user = by
    line.delete()


@transaction.atomic
def add_labor(wo, *, hours, worked_on, technician=None, rate=None, description="", by, today=None) -> LaborLine:
    """Log time on `wo`. `technician` None means the default (default_technician); `rate` None or blank means Settings'
    (default_rate). Raises ValidationError keyed by these argument names, or a plain one when the work order takes no lines."""
    today = today or timezone.localdate()
    current = _lock_work_order(wo.pk)
    errors: dict = {}
    worked = _date(worked_on, errors)
    if worked is not None:
        if worked > today:
            errors["worked_on"] = "The date cannot be in the future."
        elif worked < current.opened_on:
            errors["worked_on"] = f"{current.number} was opened on {current.opened_on:%b %-d, %Y}; time cannot be logged before that."
        elif current.completed_on and worked > current.completed_on:
            errors["worked_on"] = f"{current.number} was completed on {current.completed_on:%b %-d, %Y}; time cannot be logged after that."
    h = _decimal(hours, "hours", errors, required="Enter the hours worked.", low=Decimal(0), high=HOURS_MAX, low_inclusive=False,
                 range_message=f"Log more than 0 and at most {plain(HOURS_MAX)} hours on one line.")
    tech = _technician(current, technician, by, errors)
    if rate is None or (isinstance(rate, str) and not rate.strip()):
        r = default_rate(current)
    else:
        r = _decimal(rate, "rate", errors, required="Enter the hourly rate.", low=Decimal(0), high=RATE_MAX, low_inclusive=True,
                     range_message=f"The rate is between $0 and {money_text(RATE_MAX)} an hour.")
    text = _text(description, "description", errors, limit=DESCRIPTION_MAX, what="description")
    if tech is not None and worked is not None and h is not None and not errors:
        day = LaborLine.objects.filter(technician=tech, worked_on=worked).exclude(work_order__source=Source.IMPORTED, description=IMPORTED_LABOR)
        logged = day.aggregate(h=Sum("hours"))["h"] or Decimal(0)
        if logged + h > DAY_HOURS_MAX:
            errors["hours"] = (f"{tech.name} already has {plain(logged)} h logged on {worked:%b %-d, %Y}; "
                               f"one day holds at most {plain(DAY_HOURS_MAX)} h.")
    if errors:
        raise ValidationError(errors)
    line = LaborLine(tenant_id=current.tenant_id, work_order=current, technician=tech, worked_on=worked, hours=h, rate=r, description=text)
    _save(line, by, CHANGE_ADDED)
    return line


@transaction.atomic
def add_part(wo, *, description, quantity, unit_cost, part_number="", po_number="", by) -> PartLine:
    """Add a part line to `wo`. Raises ValidationError keyed by these argument names, or a plain one when the work order takes
    no lines."""
    current = _lock_work_order(wo.pk)
    errors: dict = {}
    text = _text(description, "description", errors, limit=DESCRIPTION_MAX, what="description")
    if not text and "description" not in errors:
        errors["description"] = "Describe the part."
    q = _decimal(quantity, "quantity", errors, required="Enter the quantity.", low=Decimal(0), high=QUANTITY_MAX, low_inclusive=False,
                 range_message=f"Enter a quantity above 0 and at most {QUANTITY_MAX:,}.")
    c = _decimal(unit_cost, "unit_cost", errors, required="Enter the unit cost (0 for a part at no charge).", low=Decimal(0), high=UNIT_COST_MAX,
                 low_inclusive=True, range_message=f"The unit cost is between $0 and {money_text(UNIT_COST_MAX)}.")
    number = _text(part_number, "part_number", errors, limit=PART_NUMBER_MAX, what="part number")
    po = _text(po_number, "po_number", errors, limit=PO_NUMBER_MAX, what="PO number")
    if errors:
        raise ValidationError(errors)
    line = PartLine(tenant_id=current.tenant_id, work_order=current, description=text, part_number=number, quantity=q, unit_cost=c, po_number=po)
    _save(line, by, CHANGE_ADDED)
    return line


def _remove(model, line, by, missing: str) -> None:
    work_order_id = model.objects.filter(pk=line.pk).values_list("work_order_id", flat=True).first()
    if work_order_id is None:  # another facility's line (the scoped manager cannot see it), or already removed
        raise ValidationError(missing)
    _lock_work_order(work_order_id)  # the work order first, then the line: the order add_labor locks in
    current = model.objects.select_for_update().filter(pk=line.pk).first()
    if current is None:
        raise ValidationError(missing)
    _delete(current, by)


@transaction.atomic
def remove_labor(line, *, by) -> None:
    _remove(LaborLine, line, by, "That labor line is not on file here. It may have been removed already.")


@transaction.atomic
def remove_part(line, *, by) -> None:
    _remove(PartLine, line, by, "That part line is not on file here. It may have been removed already.")


# --- importing (slice 23) ------------------------------------------------------------------------------------------------------
#
# Work order history from the previous system (apps.workorders.legacy) arrives done: its lines are recorded by history's rules, not
# today's. Only on a work order that was imported (Source.IMPORTED), whatever its status (it is created closed, and its lines come
# with it). No daily or per-line 24-hour cap (an old system's line may be a whole job's time, so add_labor's day total leaves these
# lines out), no active-technician rule (former staff did the work), no default technician (never the person importing), and the
# worked day is the work order's own: completed, else opened. In-house time still names its technician: a labor line without one is
# vendor time wherever it is read (cost of service, the drawer, the timeline, the custom labor report), so in-house hours that name
# nobody here are an in-house cost line instead (add_imported_unnamed_labor). The limits are the columns' (numeric sizes), so
# PostgreSQL never refuses a line. Each line's record and its audit record carry `at`, a moment on that day, so the timeline and the
# History section read in business order.

def _column_max(model, name: str) -> Decimal:
    """The largest value a numeric column holds: 9999.99 for numeric(6, 2)."""
    f = model._meta.get_field(name)
    return Decimal(10) ** (f.max_digits - f.decimal_places) - Decimal(1).scaleb(-f.decimal_places)


IMPORT_HOURS_MAX = _column_max(LaborLine, "hours")
IMPORT_RATE_MAX = _column_max(LaborLine, "rate")
IMPORT_AMOUNT_MAX = _column_max(PartLine, "unit_cost")  # a cost is one part line, quantity 1
IMPORTED_LABOR = "Labor (imported)"
UNNAMED_LABOR = "In-house labor (imported)"  # in-house hours with no technician here: a part line of hours × rate


def _imported(wo) -> WorkOrder:
    current = WorkOrder.objects.select_for_update().filter(pk=wo.pk).first()
    if current is None:
        raise ValidationError("That work order is not in this facility.")
    if current.source != Source.IMPORTED:
        raise ValidationError(f"{current.number} was not imported: its labor and parts are logged as the work is done.")
    return current


def _imported_amount(value, *, what: str, low: Decimal, high: Decimal, low_inclusive: bool) -> Decimal:
    errors: dict = {}
    d = _decimal(value, "value", errors, required=f"Enter the {what}.", low=low, high=high, low_inclusive=low_inclusive,
                 range_message=f"The {what} must be {'0 or more' if low_inclusive else 'above 0'} and at most {high:,}.")
    if errors:
        raise ValidationError(errors["value"])
    return d


def _backdate(model, line, at) -> None:
    model.objects.filter(pk=line.pk).update(created_at=at)  # the row's own timestamp (auto_now_add) follows its audit record
    line.created_at = at


@transaction.atomic
def add_imported_labor(wo, *, hours, rate, technician=None, at, by=None) -> LaborLine:
    """Time from the previous system on an imported work order: `hours` (above 0, at most IMPORT_HOURS_MAX) at `rate` (0 to
    IMPORT_RATE_MAX) for `technician` (this facility's, active or not), or none on vendor service (the vendor's time). In-house
    time that names nobody here is add_imported_unnamed_labor's. Raises ValidationError in words."""
    current = _imported(wo)
    h = _imported_amount(hours, what="labor hours", low=Decimal(0), high=IMPORT_HOURS_MAX, low_inclusive=False)
    r = _imported_amount(rate, what="labor rate", low=Decimal(0), high=IMPORT_RATE_MAX, low_inclusive=True)
    if technician is None and not current.vendor_service:
        raise ValidationError("In-house time is recorded with its technician: a line without one is vendor time.")
    if technician is not None:
        if current.vendor_service:
            raise ValidationError("Vendor service time is recorded without a technician.")
        if not Technician.objects.filter(pk=technician.pk).exists():  # the scoped manager: another facility's is not found
            raise ValidationError("Choose a technician from this facility.")
    line = LaborLine(tenant_id=current.tenant_id, work_order=current, technician=technician, worked_on=current.completed_on or current.opened_on,
                     hours=h, rate=r, description=IMPORTED_LABOR)
    _save(line, by, CHANGE_ADDED, at=at)
    _backdate(LaborLine, line, at)
    return line


@transaction.atomic
def add_imported_unnamed_labor(wo, *, hours, rate, at, by=None) -> PartLine:
    """In-house time from the previous system that names no technician here (the cell was blank, or names nobody or two people) on
    an imported in-house work order: one part line, UNNAMED_LABOR, `hours` (above 0, at most IMPORT_HOURS_MAX) as its quantity at
    `rate` (0 to IMPORT_RATE_MAX), so it costs what a labor line would and stays in-house spend (cost of service counts a part line
    by its work order) instead of reading as vendor time. Raises ValidationError in words."""
    current = _imported(wo)
    h = _imported_amount(hours, what="labor hours", low=Decimal(0), high=IMPORT_HOURS_MAX, low_inclusive=False)
    r = _imported_amount(rate, what="labor rate", low=Decimal(0), high=IMPORT_RATE_MAX, low_inclusive=True)
    if current.vendor_service:
        raise ValidationError("Vendor service time is recorded as a labor line without a technician.")
    line = PartLine(tenant_id=current.tenant_id, work_order=current, description=UNNAMED_LABOR, quantity=h, unit_cost=r)
    _save(line, by, CHANGE_ADDED, at=at)
    _backdate(PartLine, line, at)
    return line


@transaction.atomic
def add_imported_part(wo, *, description, amount, at, by=None) -> PartLine:
    """A cost from the previous system on an imported work order, as one part line (quantity 1 at `amount`, above 0 and at most
    IMPORT_AMOUNT_MAX): its parts, its outside service, or a total the old system kept alone. Raises ValidationError in words."""
    current = _imported(wo)
    c = _imported_amount(amount, what="amount", low=Decimal(0), high=IMPORT_AMOUNT_MAX, low_inclusive=False)
    errors: dict = {}
    text = _text(description, "description", errors, limit=DESCRIPTION_MAX, what="description")
    if errors or not text:
        raise ValidationError(errors.get("description", "Describe the cost."))
    line = PartLine(tenant_id=current.tenant_id, work_order=current, description=text, quantity=Decimal(1), unit_cost=c)
    _save(line, by, CHANGE_ADDED, at=at)
    _backdate(PartLine, line, at)
    return line
