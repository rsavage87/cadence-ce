"""
The survey binder's "Incoming inspection before first use" section (slice 25): the devices added in the period as new to the facility
(Asset.added_as NEW, by the facility's day of created_at), each with its incoming inspection and the day it first went into service.

The incoming inspection is the first completed Incoming inspection work order on the device (completed or closed; a cancelled one
was never done), else an open one. First day in service is the first row of the device's history with status in service (one read
for every device). An inspection completed on the day the device went into service counts as before first use.

Only new devices are asked for an inspection. Devices entered as already in use, imported from the previous system, or added before
Cadence recorded how (blank added_as: earlier slices, the demo seed) are counted, never gaps: no false alarm on the day a facility
starts. Gaps: GAP a new device in service with no completed inspection (its drawer is where one is opened); FINDING a new device
inspected after it went into service (the inspection work order); CHECK a new device waiting out of service, never in service, with no
inspection work order open or done.

Queries: four whatever the number of devices: how every device added in the period was added (grouped), the new ones, their
inspection work orders, and their history.
"""
from django.db.models import Count
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows
from apps.equipment.models import AddedAs, Asset, AssetStatus, RiskClass
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoStatus, WoType

from . import CHECK, DEVICE, FINDING, GAP, WORK_ORDER, Figure, Gap, Period, Section, Table

DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
_CLASS_LABELS = dict(RiskClass.choices)
_STATUS_LABELS = dict(AssetStatus.choices)
_WO_STATUS_LABELS = dict(WoStatus.choices)

COLUMNS = ["Tag", "Model", "Risk class", "Added on", "Status when added", "Incoming inspection", "Inspection status", "Inspected on",
           "First day in service", "Days in service before inspection"]


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def _added_in(period: Period):
    """Every device added in the period, by the facility's day of created_at (the current time zone is the facility's)."""
    return Asset.objects.filter(created_at__date__gte=period.start, created_at__date__lte=period.end)


def _inspections(new) -> dict:
    """{device id: (the first completed inspection, or None; the first open one, or None)}, each a dict of number, status, and
    completed_on. One query."""
    out = {}
    rows = (WorkOrder.objects.filter(asset__in=new.values("id"), type=WoType.INSPECTION).exclude(status=WoStatus.CANCELLED)
            .order_by("asset_id", "opened_on", "number").values("asset_id", "number", "status", "completed_on"))
    for row in rows:
        done, open_ = out.get(row["asset_id"], (None, None))
        if row["status"] in DONE and row["completed_on"] is not None:
            if done is None or row["completed_on"] < done["completed_on"]:
                done = row
        elif row["status"] in OPEN_STATUSES and open_ is None:
            open_ = row
        out[row["asset_id"]] = (done, open_)
    return out


def _history(new) -> dict:
    """{device id: (the status of its first history row, the facility's day of its first row in service, or None)}. One read."""
    out = {}
    rows = (_rows(Asset.history.model).filter(id__in=new.values("id")).order_by("id", "history_date", "history_id")
            .values_list("id", "status", "history_date"))
    for pk, status, when in rows:
        first, in_service = out.get(pk, (None, None))
        if first is None:
            first = status
        if in_service is None and status == AssetStatus.IN_SERVICE:
            in_service = local_day(when)
        out[pk] = (first, in_service)
    return out


def build(period: Period, user) -> Section:
    added = _added_in(period)
    how = {key: n for key, n in added.order_by().values_list("added_as").annotate(n=Count("id"))}
    new = added.filter(added_as=AddedAs.NEW)
    devices = list(new.order_by("created_at", "tag").values("id", "tag", "status", "created_at", "device_model__manufacturer",
                                                             "device_model__model", "device_model__risk_class"))
    inspections = _inspections(new) if devices else {}
    history = _history(new) if devices else {}

    rows, gaps = [], []
    before = after = uninspected = waiting = 0
    for d in devices:
        tag, added_on = d["tag"], local_day(d["created_at"])
        first, in_service = history.get(d["id"], (None, None))
        done, open_ = inspections.get(d["id"], (None, None))
        shown = done or open_
        late = (done["completed_on"] - in_service).days if done and in_service and done["completed_on"] > in_service else None
        rows.append([tag, f"{d['device_model__manufacturer']} {d['device_model__model']}",
                     _CLASS_LABELS.get(d["device_model__risk_class"], d["device_model__risk_class"]), added_on,
                     _STATUS_LABELS.get(first, "Not recorded") if first else "Not recorded",
                     shown["number"] if shown else "", _WO_STATUS_LABELS.get(shown["status"], shown["status"]) if shown else "None recorded",
                     done["completed_on"] if done else None, in_service, late])
        device_url = reverse("web:asset", args=[tag])
        if in_service is None:
            waiting += 1
            if done is None and open_ is None and d["status"] == AssetStatus.OUT_OF_SERVICE:
                gaps.append(Gap(CHECK, f"{tag} has waited out of service since it was added on {_day(added_on)}, with no incoming inspection "
                                       "work order open.", device_url, tag))
        elif done is None:
            uninspected += 1
            still = d["status"] == AssetStatus.IN_SERVICE
            lead = f"{tag} has been in service since {_day(in_service)}" if still else f"{tag} went into service on {_day(in_service)}"
            gaps.append(Gap(GAP, f"{lead} with no incoming inspection recorded.", device_url, tag))
        elif late is not None:
            after += 1
            gaps.append(Gap(FINDING, f"{tag} was inspected {_days(late)} after it went into service ({done['number']}, completed "
                                     f"{_day(done['completed_on'])}; in service from {_day(in_service)}).",
                            reverse("web:wo", args=[done["number"]]), done["number"]))
        else:
            before += 1

    figures = [
        Figure("New devices added", len(devices), "added as new to the facility"),
        Figure("Inspected before first use", before),
        Figure("Waiting: never in service yet", waiting),
        Figure("Inspected after first use", after),
        Figure("In service with no incoming inspection", uninspected),
        Figure("Entered as already in use", how.get(AddedAs.EXISTING, 0), "not asked for an incoming inspection"),
        Figure("Imported from the previous system", how.get(AddedAs.IMPORTED, 0), "not asked for an incoming inspection"),
        Figure("Added before Cadence recorded how", how.get("", 0), "not asked for an incoming inspection"),
    ]
    tables = [Table("new_devices", "New devices added in the period", COLUMNS, lambda: iter(rows), count=len(rows), links={0: DEVICE, 5: WORK_ORDER},
                    empty="No device was added as new in this period.")]
    notes = [
        "Only devices added as new to the facility are expected to have an incoming inspection before first use. Devices entered as "
        "already in use, imported from the previous system, or added before Cadence recorded how a device was added are counted above, "
        "never listed as gaps.",
        "The incoming inspection is the first completed Incoming inspection work order on the device; one completed on the day the "
        "device went into service counts as before first use.",
        "The status when added and the first day in service are read from the device's history: a device added in service was in use "
        "from the day it was added.",
        "A device's inspection is shown as it stands today, though the device was added in the period.",
        "Risk class is each model's class today.",
    ]
    return Section(key="inspections", title="Incoming inspection before first use",
                   topic="New equipment is inspected before it is first used on patients.",
                   covers=f"Devices added {period.label}; their inspections {period.as_of_today}", figures=figures, gaps=gaps, tables=tables,
                   notes=notes)
