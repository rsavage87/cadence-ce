"""
The survey binder's "Incoming inspection before first use" section (slice 25; slice 26 records each inspection's result): the devices
added in the period as new to the facility (Asset.added_as NEW, by the facility's day of created_at), each with its incoming inspection
and the day it first went into service.

The evidence is the work orders, never the device's flag (Asset.awaiting_inspection): a device's incoming inspection is the first
Incoming inspection work order completed (or closed; a cancelled one was never done) with result Passed. One completed with no result
recorded counts as passed: inspections completed before Cadence recorded results (slice 25's), or of a device that was not waiting for
one. A failed inspection is listed (the "failed" table, with the re-inspection it opened), never the evidence. With none passed, the
row shows the device's open inspection, else its last failed one. First day in service is the first row of the device's history with
status in service (one read for every device). An inspection completed on the day the device went into service counts as before first
use. A device put in use before its incoming inspection (equipment.services.use_before_inspection: Equipment Approve, a reason from
a fixed list) is read back from its history (workorders.inspections.uses_before).

Only new devices are asked for an inspection. Devices entered as already in use, imported from the previous system, or added before
Cadence recorded how (blank added_as: earlier slices) are counted, never gaps: no false alarm on the day a facility starts. Gaps:
- GAP a new device that went into service with no inspection passed (none recorded, or only failed ones): its drawer is where one is
  opened.
- FINDING a new device put in use before its incoming inspection, with its reason, while the inspection is open and after it passed
  (the device: its history records who approved it and why); a new device inspected after it went into service (the inspection).
- CHECK a new device waiting out of service, never in service, with no inspection work order open or passed (after a fail: no
  re-inspection open); a device entered as already in use whose install date is within RECENT_INSTALL_DAYS before the day it was
  added (was it new?).
A new device retired without ever going into service (returned to the vendor) is counted on its own line, never a gap.

Queries: six at most whatever the number of devices: how every device added in the period was added (grouped), the ones entered as
already in use with a recent install date (only when there are any entered so), the new ones, their inspection work orders, their
history, and their history's uses before inspection.

Slice 29: a rental, vendor loaner, or demo unit added new (it arrived in the period) is a new device like any other here: inspected
before first use (on its rental checklist, workorders.inspections.TEMPORARY_INCOMING), with the same gaps; one returned to its owner
without ever going into service counts with "Returned to the vendor". One entered as already on site (EXISTING) is counted, never a
gap, and never a "was it new?" check: its install date is the day it arrived, which is no sign it was new to the facility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _person, _rows
from apps.equipment.models import AddedAs, Asset, AssetStatus, Ownership, RiskClass
from apps.equipment.services import AWAITING_LABEL, HELD_LABEL, OWNED
from apps.workorders.inspections import uses_before
from apps.workorders.models import OPEN_STATUSES, InspectionResult, WorkOrder, WoStatus, WoType

from . import CHECK, DEVICE, FINDING, GAP, WORK_ORDER, Figure, Gap, Period, Section, Table

DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
RECENT_INSTALL_DAYS = 30  # a device entered as already in use, installed this many days or fewer before it was added: was it new?
NOT_RECORDED = "Not recorded"
UNLISTED_REASON = "a reason not on the list"  # a history row whose reason is no UseBeforeInspection label (_reason)
_CLASS_LABELS = dict(RiskClass.choices)
_STATUS_LABELS = dict(AssetStatus.choices)
_WO_STATUS_LABELS = dict(WoStatus.choices)
_RESULT_LABELS = dict(InspectionResult.choices)

COLUMNS = ["Tag", "Model", "Risk class", "Added on", "Status when added", "Incoming inspection", "Inspection status", "Result", "Inspected by",
           "Inspected on", "First day in service", "Days in service before inspection", "In use before inspection"]
FAILED_COLUMNS = ["Work order", "Device", "Failed on", "Inspected by", "Re-inspection", "Re-inspection status", "Device passed on"]
# The inspection work orders' columns read (staff-written or chosen only: never the problem, resolution, or notes).
_FIELDS = ("id", "asset_id", "number", "status", "due_on", "completed_on", "inspection_result", "follow_up_of_id", "vendor_service", "vendor_name",
           "assigned_to__name")


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def _span(n: int) -> str:
    """How long a device was in use: "less than a day" on the same day, else "N days"."""
    return "less than a day" if n <= 0 else _days(n)


def _added_in(period: Period):
    """Every device added in the period, by the facility's day of created_at (the current time zone is the facility's)."""
    return Asset.objects.filter(created_at__date__gte=period.start, created_at__date__lte=period.end)


@dataclass
class _Inspections:
    """One device's incoming inspection work orders, each a dict of _FIELDS."""
    done: dict | None = None  # the evidence: the first completed with result passed (or none recorded)
    open: dict | None = None  # the first open (by opened day and number)
    failed: list = field(default_factory=list)  # completed with result failed, in order
    follow_ups: dict = field(default_factory=dict)  # {inspection id: the first inspection opened from it (a re-inspection), any status}

    @property
    def last_failed(self) -> dict | None:
        return max(self.failed, key=lambda r: (r["completed_on"], r["number"])) if self.failed else None


def _inspections(new) -> dict:
    """{device id: _Inspections}. One query."""
    out: dict = {}
    rows = (WorkOrder.objects.filter(asset__in=new.values("id"), type=WoType.INSPECTION).order_by("asset_id", "opened_on", "number")
            .values(*_FIELDS))
    for row in rows:
        found = out.setdefault(row["asset_id"], _Inspections())
        if row["follow_up_of_id"] is not None:
            found.follow_ups.setdefault(row["follow_up_of_id"], row)
        if row["status"] in DONE and row["completed_on"] is not None:
            if row["inspection_result"] == InspectionResult.FAILED:
                found.failed.append(row)
            elif found.done is None or row["completed_on"] < found.done["completed_on"]:
                found.done = row
        elif row["status"] in OPEN_STATUSES and found.open is None:
            found.open = row
    return out


def _history(new) -> dict:
    """{device id: (its status as its first history row words it, the facility's day of its first row in service, or None)}. One
    read. Worded as equipment.services.status_label words a status: held for an incident first (slice 28), then awaiting inspection."""
    out = {}
    rows = (_rows(Asset.history.model).filter(id__in=new.values("id")).order_by("id", "history_date", "history_id")
            .values_list("id", "status", "awaiting_inspection", "incident_hold", "history_date"))
    for pk, status, awaiting, held, when in rows:
        first, in_service = out.get(pk, (None, None))
        if first is None and held:
            first = HELD_LABEL
        elif first is None:
            first = AWAITING_LABEL if awaiting and status == AssetStatus.OUT_OF_SERVICE else _STATUS_LABELS.get(status, NOT_RECORDED)
        if in_service is None and status == AssetStatus.IN_SERVICE:
            in_service = local_day(when)
        out[pk] = (first, in_service)
    return out


def _recent_installs(added, period: Period) -> list[tuple]:
    """[(tag, the day it was added, days from its install date to that day)] for the devices entered as already in use in the period
    whose install date is at most RECENT_INSTALL_DAYS before the day they were added (or after it). Ours only, and never one we kept
    (slice 29: a temporary device's install date is the day it arrived, no sign it was new). One query."""
    out = []
    rows = (added.filter(OWNED, kept_on__isnull=True, added_as=AddedAs.EXISTING,
                         installed_on__gte=period.start - timedelta(days=RECENT_INSTALL_DAYS))
            .order_by("created_at", "tag").values_list("tag", "installed_on", "created_at"))
    for tag, installed_on, created_at in rows:
        added_on = local_day(created_at)
        days = (added_on - installed_on).days
        if days <= RECENT_INSTALL_DAYS:
            out.append((tag, added_on, days))
    return out


def _recent_install_text(tag: str, added_on, days: int) -> str:
    when = (f"installed {_days(days)} before" if days > 0 else "installed the same day" if days == 0 else f"installed {_days(-days)} after it")
    return f"{tag} was added as already in use on {_day(added_on)} but {when}: was it new? A new device is inspected before first use."


def _result(row) -> str:
    """A completed inspection's result as the binder words it ("" for one not done)."""
    if row is None or row["status"] not in DONE:
        return ""
    return _RESULT_LABELS.get(row["inspection_result"], NOT_RECORDED)


def _inspector(row) -> str:
    """Who did a completed inspection: the vendor for vendor service, else the technician it was assigned to (staff names only)."""
    if row is None or row["status"] not in DONE:
        return ""
    if row["vendor_service"]:
        return row["vendor_name"] or "Vendor"
    return row["assigned_to__name"] or ""


def _reason(use) -> str:
    """Why a device was put in use before its inspection: the listed reason recorded (UseBeforeInspection), never other text a history
    row's reason may hold (a status change's note is typed)."""
    return use.label if use.reason else UNLISTED_REASON


def _use_text(tag: str, use, status: str, found: _Inspections, today) -> str:
    """The FINDING for a device put in use before its incoming inspection (its first such use)."""
    approved = f"approved by {_person(use.by)[0]}; " if use.by is not None else ""
    reason = _reason(use)
    done, open_, failed = found.done, found.open, found.last_failed
    if done is not None:
        n = (done["completed_on"] - use.on).days
        return (f"{tag} was in use {_span(n)} before its incoming inspection: {reason} ({approved}in use from {_day(use.on)}; "
                f"{done['number']} passed on {_day(done['completed_on'])}).")
    if open_ is not None:
        tail = f"{open_['number']} is open, due {_day(open_['due_on'])}"
    elif failed is not None:
        tail = f"{failed['number']} failed on {_day(failed['completed_on'])}, and no inspection is open"
    else:
        tail = "no incoming inspection is open"
    if status == AssetStatus.IN_SERVICE:
        return (f"{tag} has been in use {_span((today - use.on).days)} without its incoming inspection: {reason} ({approved}in use from "
                f"{_day(use.on)}; {tail}).")
    return f"{tag} was put in use before its incoming inspection on {_day(use.on)}: {reason} ({approved}{tail})."


def _failed_rows(failed: list) -> list[list]:
    failed.sort(key=lambda f: (f[0]["completed_on"], f[0]["number"]))
    rows = []
    for row, tag, found in failed:
        again = found.follow_ups.get(row["id"])
        done = found.done
        passed_on = done["completed_on"] if done is not None and done["completed_on"] >= row["completed_on"] else None
        rows.append([row["number"], tag, row["completed_on"], _inspector(row), again["number"] if again else "",
                     _WO_STATUS_LABELS.get(again["status"], again["status"]) if again else "", passed_on])
    return rows


def build(period: Period, user) -> Section:
    today = period.today
    added = _added_in(period)
    how = {key: n for key, n in added.order_by().values_list("added_as").annotate(n=Count("id"))}
    recent = _recent_installs(added, period) if how.get(AddedAs.EXISTING) else []
    new = added.filter(added_as=AddedAs.NEW)
    devices = list(new.order_by("created_at", "tag").values("id", "tag", "status", "created_at", "device_model__manufacturer",
                                                             "device_model__model", "device_model__risk_class", "ownership"))
    temporary = sum(1 for d in devices if d["ownership"] != Ownership.OWNED)  # slice 29: read as any new device
    inspections = _inspections(new) if devices else {}
    history = _history(new) if devices else {}
    used = uses_before([d["id"] for d in devices]) if devices else {}

    rows, gaps, failed = [], [], []
    before = after = uninspected = waiting = waiting_open = returned = in_use = 0
    for d in devices:
        tag, added_on = d["tag"], local_day(d["created_at"])
        first, in_service = history.get(d["id"], (None, None))
        found = inspections.get(d["id"]) or _Inspections()
        done, open_, last_failed = found.done, found.open, found.last_failed
        failed += [(row, tag, found) for row in found.failed]
        uses = used.get(d["id"])
        shown = done or open_ or last_failed
        late = (done["completed_on"] - in_service).days if done and in_service and done["completed_on"] > in_service else None
        rows.append([tag, f"{d['device_model__manufacturer']} {d['device_model__model']}",
                     _CLASS_LABELS.get(d["device_model__risk_class"], d["device_model__risk_class"]), added_on, first or NOT_RECORDED,
                     shown["number"] if shown else "", _WO_STATUS_LABELS.get(shown["status"], shown["status"]) if shown else "None recorded",
                     _result(shown), _inspector(shown), shown["completed_on"] if shown and shown["status"] in DONE else None, in_service, late,
                     _reason(uses[0]) if uses else ""])
        device_url = reverse("web:asset", args=[tag])
        if uses:
            in_use += 1
            gaps.append(Gap(FINDING, _use_text(tag, uses[0], d["status"], found, today), device_url, tag))
        elif in_service is None:
            if d["status"] == AssetStatus.RETIRED:
                returned += 1
                continue
            waiting += 1
            waiting_open += open_ is not None
            if done is None and open_ is None and d["status"] == AssetStatus.OUT_OF_SERVICE:
                if last_failed is not None:
                    text = (f"{tag} failed its incoming inspection {last_failed['number']} on {_day(last_failed['completed_on'])} and has "
                            f"waited out of service since, with no re-inspection open.")
                else:
                    text = f"{tag} has waited out of service since it was added on {_day(added_on)}, with no incoming inspection work order open."
                gaps.append(Gap(CHECK, text, device_url, tag))
        elif done is None:
            uninspected += 1
            still = d["status"] == AssetStatus.IN_SERVICE
            lead = f"{tag} has been in service since {_day(in_service)}" if still else f"{tag} went into service on {_day(in_service)}"
            if last_failed is not None:
                text = (f"{lead} with no incoming inspection passed: {last_failed['number']} failed on "
                        f"{_day(last_failed['completed_on'])}.")
            else:
                text = f"{lead} with no incoming inspection recorded."
            gaps.append(Gap(GAP, text, device_url, tag))
        elif late is not None:
            after += 1
            gaps.append(Gap(FINDING, f"{tag} was inspected {_days(late)} after it went into service ({done['number']}, completed "
                                     f"{_day(done['completed_on'])}; in service from {_day(in_service)}).",
                            reverse("web:wo", args=[done["number"]]), done["number"]))
        else:
            before += 1
    gaps += [Gap(CHECK, _recent_install_text(tag, added_on, days), reverse("web:asset", args=[tag]), tag) for tag, added_on, days in recent]
    failed_rows = _failed_rows(failed)

    existing_hint = "not asked for an incoming inspection"
    if recent:
        existing_hint += f"; {len(recent)} installed within {RECENT_INSTALL_DAYS} days before they were added"
    new_hint = "added as new to the facility"
    if temporary:
        new_hint += f"; {temporary} of them {'a rental, vendor loaner, or demo unit' if temporary == 1 else 'rentals, vendor loaners, or demo units'}"
    figures = [
        Figure("New devices added", len(devices), new_hint),
        Figure("Inspected before first use", before),
        Figure("Waiting: never in service yet", waiting, f"{waiting_open} with an incoming inspection open"),
        Figure("Returned to the vendor", returned, "retired without ever going into service"),
        Figure("Inspected after first use", after),
        Figure("In use before its incoming inspection", in_use, "put in use by exception, with a reason"),
        Figure("In service with no incoming inspection passed", uninspected),
        Figure("Failed incoming inspections", len(failed_rows), "listed; never counted as the inspection"),
        Figure("Entered as already in use", how.get(AddedAs.EXISTING, 0), existing_hint),
        Figure("Imported from the previous system", how.get(AddedAs.IMPORTED, 0), "not asked for an incoming inspection"),
        Figure("Added before Cadence recorded how", how.get("", 0), "not asked for an incoming inspection"),
    ]
    tables = [
        Table("new_devices", "New devices added in the period", COLUMNS, lambda: iter(rows), count=len(rows), links={0: DEVICE, 5: WORK_ORDER},
              empty="No device was added as new in this period."),
        Table("failed", "Failed incoming inspections", FAILED_COLUMNS, lambda: iter(failed_rows), count=len(failed_rows),
              links={0: WORK_ORDER, 1: DEVICE, 4: WORK_ORDER}, empty="No incoming inspection of these devices failed."),
    ]
    notes = [
        "Only devices added as new to the facility are expected to have an incoming inspection before first use. Devices entered as "
        "already in use, imported from the previous system, or added before Cadence recorded how a device was added are counted above, "
        f"never listed as gaps; one entered as already in use with an install date within {RECENT_INSTALL_DAYS} days of the day it was "
        "added is listed as a check, in case it was new.",
        "The incoming inspection is the first completed Incoming inspection work order on the device whose result is Passed; one completed "
        "with no result recorded (before Cadence recorded results) counts as passed. One completed on the day the device went into "
        "service counts as before first use. A failed inspection is listed with its re-inspection, never counted as the inspection.",
        "A device put in use before its incoming inspection, by a manager's exception with its reason, is listed while its inspection is "
        "open and after it passed: when it went into use, why, and who approved it are read from the device's history.",
        "Returned to the vendor: a new device retired without ever going into service (or, for a rental, vendor loaner, or demo unit, "
        "returned to its owner).",
        "Rentals, vendor loaners, and demo units that arrived in the period are new devices here like any other, inspected before first "
        "use on the rental checklist (the incoming checks plus the owner's PM label); one entered after it was already on site is "
        "counted as entered already in use.",
        "Inspected by: the technician the inspection was assigned to, or the vendor for vendor service.",
        "The status when added and the first day in service are read from the device's history: a device added in service was in use "
        "from the day it was added.",
        "A device's inspection is shown as it stands today, though the device was added in the period.",
        "Risk class is each model's class today.",
    ]
    return Section(key="inspections", title="Incoming inspection before first use",
                   topic="New equipment is inspected before it is first used on patients.",
                   covers=f"Devices added {period.label}; their inspections {period.as_of_today}", figures=figures, gaps=gaps, tables=tables,
                   notes=notes)
