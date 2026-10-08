"""
Work order history and open work from the previous system (slice 23): one row per work order, by its number there. Each row
becomes one work order in its final state, with its hours and costs, through apps.workorders.legacy (what is recorded, and the rows
it refuses, are described there); this module reads the file's values and reports on them.

- The columns are coded values and numbers only: the old system's problem, work done, comments, requester, and caller never come
  across (they may name a patient: CLAUDE.md, non-negotiable 6), so no column or alias reads them.
- Type, status, and priority are read by the words exports use (TYPE_WORDS, STATUS_WORDS, PRIORITY_WORDS), each value's reading
  recorded for the screen; an unknown type or status skips the row, an unknown priority is normal with a note.
- Dates in any shape apps.imports.parse reads; a due date may be a month alone (a PM scheduled for March is due on the 31st).
  The opened date, and a done work order's completed date, are needed (a row without one is skipped); a due date is needed on a
  PM (PM on-time is counted by it) and otherwise worked out from the priority, with a note when it could not be read.
- The technician by name, "First Last" or "Last, First", any letter case, active or not, compared as the technicians import
  compares names (person_key); one not found, or two with the name, leaves the work order without one, with a note (its in-house
  hours are then an in-house cost line: apps.workorders.legacy). A vendor makes it vendor service.
- Costs: a value that cannot be read is left out with a note, never read as 0.
- A re-run: a number already imported on the same device is left as it is ("Already imported", and a note when it is still open
  here and done in the file); on another device it is skipped. A number on more than one row of the file skips every one of them
  (prepare): a work order is one row, its labor lines added up first, and importing the first row alone would leave its costs
  short with no re-run to fix them.

The check runs every row through the same service with a stand-in number (ctx.check), so it takes no lock on the facility's
numbering. The summary adds up what the person reconciles with the old system: work orders by type, year, and status, hours,
labor, parts, outside service, and a total kept alone, the cents the labor rate's rounding moved, and PM history on time or late
(slice 27: within the facility's PM window for the model's class, apps.pm.windows, as the Overview and the reports count it).
"""
from decimal import Decimal

from django.db.models.functions import Lower

from apps.accounts.models import Level, Module
from apps.credentials import services as credentials
from apps.credentials.models import Technician
from apps.equipment.models import Asset
from apps.facility.services import get_settings
from apps.pm.windows import windows
from apps.workorders import legacy
from apps.workorders.models import OPEN_STATUSES, Priority, WoStatus, WoType

from .. import parse
from ..base import Column, Importer, RowSkip


def _words(choices, extra: dict) -> dict:
    """Lowercased word -> slug: each choice's slug and label, and the words other systems use for it."""
    words = {}
    for value, label in choices.choices:
        words[value.replace("_", " ")] = value
        words[label.lower()] = value
    return {**words, **extra}


def _all(slug: str, *words: str) -> dict:
    return {w: slug for w in words}


TYPE_WORDS = _words(WoType, {
    **_all(WoType.PM, "pm", "ppm", "preventive", "preventative", "preventive maintenance", "preventative maintenance", "planned maintenance",
           "scheduled maintenance", "routine maintenance", "pm inspection"),
    **_all(WoType.REPAIR, "repair", "corrective", "corrective maintenance", "cm", "unscheduled", "unscheduled maintenance", "breakdown",
           "service call", "trouble call", "service request"),
    **_all(WoType.INSPECTION, "inspection", "incoming", "incoming inspection", "initial inspection", "acceptance", "acceptance test",
           "acceptance testing", "install", "installation", "new equipment"),
    **_all(WoType.RECALL, "recall", "recall action", "field action", "field correction", "field corrective action", "fca", "hazard alert",
           "safety alert"),
    **_all(WoType.SAFETY, "safety", "safety check", "safety test", "electrical safety", "electrical safety test", "electrical safety check", "est"),
})
STATUS_WORDS = _words(WoStatus, {
    **_all(WoStatus.OPEN, "open", "new", "assigned", "pending", "not started", "scheduled", "requested", "submitted"),
    **_all(WoStatus.IN_PROGRESS, "in progress", "in-progress", "started", "working", "in process", "wip"),
    **_all(WoStatus.AWAITING_PARTS, "awaiting parts", "waiting on parts", "waiting for parts", "pending parts", "parts on order",
           "parts ordered"),
    **_all(WoStatus.COMPLETED, "completed", "complete", "done", "finished", "resolved", "work complete", "work completed"),
    **_all(WoStatus.CLOSED, "closed", "closed complete", "closed - complete"),
    **_all(WoStatus.CANCELLED, "cancelled", "canceled", "cancel", "void", "voided"),
})
PRIORITY_WORDS = _words(Priority, {
    **_all(Priority.CRITICAL, "critical", "emergency", "urgent", "stat", "immediate"),
    **_all(Priority.NORMAL, "normal", "medium", "routine", "standard", "moderate"),
})

TYPE_HELP = "repair (corrective), PM (preventive), inspection (incoming, install), recall, or safety check"
STATUS_HELP = "open, in progress, awaiting parts, completed, closed, or cancelled"
REPEATED = "This work order number is on more than one row: one row per work order (add up labor lines first)"
ALREADY = "Already imported"
STILL_OPEN = "Still open here; done in the file"
NOT_A_DEVICE = "No device with this tag here: import devices first"
ANOTHER_DEVICE = "This number belongs to another device's work order"
COSTS = (("hours", "Labor hours"), ("labor", "Labor cost"), ("parts", "Parts cost"), ("outside", "Outside cost"), ("total", "Total cost"))


def person_key(name: str) -> str:
    """A name compared with technicians': as the technicians import compares them (credentials.name_key: any letter case, "Last,
    First" read as "First Last", a comma before a suffix or a credential is not)."""
    return credentials.name_key(name)


class WorkOrdersImporter(Importer):
    kind = "work_orders"
    label = "Work orders"
    module = Module.WORKORDERS
    level = Level.APPROVE
    key = "legacy_number"
    order = 40
    chunk = 100  # a row is a work order, its status history, and up to four lines with their audit records
    description = ("Work order history and open work from your previous system, one row per work order, with its hours and costs. "
                   "Each keeps its number from that system; one already imported is left as it is.")
    columns = [
        Column("legacy_number", "Work order number", ("work order number", "wo number", "wo #", "work order #", "wo no", "work order no",
                                                      "work order", "wo", "wo id", "work order id", "wo num", "workorder", "workorder number",
                                                      "previous number", "legacy number", "old number"),
               required=True, max_length=legacy.NUMBER_MAX, help="Its number in the previous system: kept, shown, and searchable", example="10423"),
        Column("tag", "Asset tag", ("asset tag", "tag", "tag number", "asset #", "asset id", "asset number", "control number", "control no",
                                    "control #", "equipment #", "equipment id", "equipment number", "equip #", "ce number", "ce #"),
               required=True, max_length=Asset._meta.get_field("tag").max_length, help="A device already in Cadence", example="CE-10042"),
        Column("type", "Type", ("type", "work order type", "wo type", "work type", "order type", "job type", "service type"), required=True,
               help=TYPE_HELP, example="Repair"),
        Column("priority", "Priority", ("priority", "wo priority", "urgency"), help="critical, high, normal, or low (blank: normal)",
               example="Normal"),
        Column("status", "Status", ("status", "wo status", "work order status", "state"), required=True,
               help=f"{STATUS_HELP} (cancelled ones are not imported, nor open PMs)", example="Closed"),
        Column("opened", "Opened", ("opened", "date opened", "opened on", "open date", "opened date", "created", "date created", "created date",
                                    "created on", "request date", "date requested", "reported date", "date reported", "date initiated"),
               required=True, help="The day it was opened", example="2024-03-04"),
        Column("due", "Due", ("due", "due date", "date due", "due on", "scheduled date", "sched date", "scheduled", "pm due", "pm due date",
                              "target date"), help="Needed on PMs; a month alone means its last day", example="2024-03-31"),
        Column("completed", "Completed", ("completed", "date completed", "completed on", "completed date", "completion date", "date closed",
                                          "closed date", "close date", "closed on", "date finished", "finished", "done date"),
               help="Needed on completed and closed ones", example="2024-03-06"),
        Column("technician", "Technician", ("technician", "tech", "technician name", "tech name", "assigned to", "assigned technician",
                                            "assigned tech", "performed by", "serviced by", "employee"),
               help="By name, as on the Technicians list (former staff too)", example="Dana Whitfield"),
        Column("vendor", "Vendor", ("vendor", "vendor name", "service vendor", "outside vendor", "service provider", "contractor"),
               max_length=legacy.VENDOR_NAME_MAX, help="For vendor service", example=""),
        Column("hours", "Labor hours", ("labor hours", "hours", "labor hrs", "total hours", "hours worked", "labor time", "time spent"),
               help="Hours in all", example="1.5"),
        Column("labor", "Labor cost", ("labor cost", "labor $", "labor amount", "labor total", "labor charges"), help="Labor in dollars",
               example="123.00"),
        Column("parts", "Parts cost", ("parts cost", "parts $", "parts amount", "parts total", "part cost", "material cost", "materials cost"),
               help="Parts in dollars", example="84.00"),
        Column("outside", "Outside cost", ("outside cost", "outside service cost", "outside services", "vendor cost", "vendor charges",
                                           "vendor amount", "contract labor"), help="Vendor or outside service in dollars", example=""),
        Column("total", "Total cost", ("total cost", "total", "total $", "cost", "wo cost", "total amount", "grand total"),
               help="Only needed when the file has no breakdown", example="207.00"),
    ]

    def prepare(self, rows: list[dict]) -> dict[int, str]:
        """Every row of a number that comes more than once (any letter case): see the module's docstring."""
        lines: dict = {}
        for i, row in enumerate(rows):
            number = (row.get(self.key) or "").strip().lower()
            if number:
                lines.setdefault(number, []).append(i)
        return {i: REPEATED for indexes in lines.values() if len(indexes) > 1 for i in indexes}

    def load(self, ctx, rows: list[dict]) -> None:
        ctx.cache["existing"] = legacy.find_many(row.get("legacy_number") for row in rows)
        tags = {row["tag"].lower() for row in rows if row.get("tag")}
        ctx.cache["assets"] = ({a.tag.lower(): a for a in Asset.objects.annotate(tag_lower=Lower("tag")).filter(tag_lower__in=tags)
                                .select_related("device_model")}  # the model's class: a PM's on-time window (_count)
                               if tags else {})
        technicians: dict = {}
        for t in Technician.objects.all():
            technicians.setdefault(person_key(t.name), []).append(t)
        ctx.cache["technicians"] = technicians
        ctx.cache["settings"] = get_settings()
        ctx.cache["windows"] = windows(ctx.cache["settings"])  # slice 27: the PM history's On time / Late, as every on-time figure counts

    # --- reading one row ------------------------------------------------------------------------------------------------------

    def _choice(self, ctx, row: dict, key: str, words: dict, choices) -> str | None:
        """A choice column's slug (None when blank), its reading recorded for the screen. Raises parse.Unreadable."""
        value = row.get(key) or ""
        slug = parse.parse_choice(value, words, what=self.column(key).label.lower())
        if slug is not None:
            ctx.read_as(self.column(key).label, value, choices(slug).label)
        return slug

    def _date(self, row: dict, key: str, *, month_end: bool = False, earliest=None):
        """(date or None, whether a value was there and could not be read: unreadable, a "none" placeholder, or before `earliest`)."""
        try:
            day = parse.parse_date(row.get(key) or "", month_end=month_end)
        except parse.Unreadable:
            return None, True
        if day is not None and earliest is not None and day < earliest:
            return None, True
        return day, False

    def _costs(self, row: dict, result) -> legacy.Costs:
        found = {}
        for key, label in COSTS:
            if key not in row:
                continue
            try:
                found[key] = (parse.parse_decimal(row[key], low=Decimal(0), what="number of hours") if key == "hours"
                              else parse.parse_money(row[key], high=None))  # a cost above what a line holds is the service's refusal
            except parse.Unreadable:
                result.warn(f"{label} not read: left out")
        return legacy.Costs(**found)

    def _technician(self, ctx, row: dict, result):
        name = row.get("technician") or ""
        if not name:
            return None
        if row.get("vendor"):
            result.warn("A technician and a vendor: imported as vendor service")
            return None
        matches = ctx.cache["technicians"].get(person_key(name), [])
        if len(matches) == 1:
            return matches[0]
        result.warn("Two technicians have this name: imported without one" if matches else "Technician not found here: imported without one")
        return None

    def apply(self, ctx, row, result):
        number = row.get("legacy_number") or ""
        tag = row.get("tag") or ""
        if not number:
            raise RowSkip("No work order number")
        if not tag:
            raise RowSkip("No asset tag")
        existing = ctx.cache["existing"].get(number.lower())
        if existing is not None:
            if existing.asset.tag.lower() != tag.lower():
                raise RowSkip(ANOTHER_DEVICE)
            result.warn(ALREADY)
            try:
                done_in_file = parse.parse_choice(row.get("status") or "", STATUS_WORDS, what="status") in legacy.DONE
            except parse.Unreadable:
                done_in_file = False
            if existing.status in OPEN_STATUSES and done_in_file:
                result.warn(STILL_OPEN)
            return  # unchanged: what a re-run finds is never changed, so a file imported twice records each work order once

        try:
            status = self._choice(ctx, row, "status", STATUS_WORDS, WoStatus)
        except parse.Unreadable:
            raise RowSkip(f"Status not read: use {STATUS_HELP}") from None
        if status is None:
            raise RowSkip("No status")
        if status == WoStatus.CANCELLED:
            raise RowSkip(legacy.CANCELLED)
        try:
            wo_type = self._choice(ctx, row, "type", TYPE_WORDS, WoType)
        except parse.Unreadable:
            raise RowSkip(f"Type not read: use {TYPE_HELP}") from None
        if wo_type is None:
            raise RowSkip("No type")
        if wo_type == WoType.PM and status in OPEN_STATUSES:
            raise RowSkip(legacy.OPEN_PM)
        asset =ctx.cache["assets"].get(tag.lower())
        if asset is None:
            raise RowSkip(NOT_A_DEVICE)

        try:
            priority = self._choice(ctx, row, "priority", PRIORITY_WORDS, Priority) or Priority.NORMAL
        except parse.Unreadable:
            priority = Priority.NORMAL
            result.warn("Priority not read: set to normal")
        opened, unread = self._date(row, "opened")
        if unread:
            raise RowSkip("Opened date not read")
        if opened is None:
            raise RowSkip("No opened date")
        due, unread = self._date(row, "due", month_end=True, earliest=legacy.EARLIEST)  # a due date before 2000 is a typo or a "none"
        if unread:
            if wo_type == WoType.PM:
                raise RowSkip("Due date not read: a PM needs it (PM on-time is counted by it)")
            result.warn("Due date not read: worked out from the priority")
        completed, unread = self._date(row, "completed")
        if status in legacy.DONE:
            if unread:
                raise RowSkip("Completed date not read")
        elif completed is not None or unread:
            completed = None
            result.warn("Completed date left out: the work order is still open")
        technician = self._technician(ctx, row, result)

        imported = legacy.import_work_order(
            asset=asset, legacy_number=number, type=wo_type, status=status, priority=priority, opened_on=opened, due_on=due,
            completed_on=completed, technician=technician, vendor_name=row.get("vendor") or "", costs=self._costs(row, result), by=ctx.user,
            placeholder=ctx.check, today=ctx.today, settings=ctx.cache["settings"])
        result.outcome = result.CREATE
        for note in imported.notes:
            result.warn(note)
        self._count(ctx, imported)

    def _count(self, ctx, imported) -> None:
        """The work order's part of the summary the person reconciles with the old system."""
        wo = imported.work_order
        ctx.total("Work orders by type", wo.get_type_display())
        ctx.total("Work orders by year", str(wo.opened_on.year))
        ctx.total("Work orders by status", wo.get_status_display())
        for label, amount in (("Labor hours", imported.hours), ("Labor $", imported.labor), ("Parts $", imported.parts),
                              ("Outside service $", imported.outside), ("Total only $", imported.total_only)):
            if amount:
                ctx.total("Costs", label, amount)
        if imported.drift:
            ctx.total("Costs", "Labor rate rounding (cents)", imported.drift * 100)
        if wo.type == WoType.PM and wo.completed_on:
            # Slice 27: within the facility's PM window for the model's class (apps.pm.windows), as the Overview will count it
            on_time = wo.completed_on <= ctx.cache["windows"].end(wo.due_on, wo.asset.device_model.risk_class)
            ctx.total("PM history", "On time" if on_time else "Late")
