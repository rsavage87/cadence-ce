"""
The survey binder's "Medical equipment inventory" section (slice 25): the devices in use today, by category and risk class, each one
with how it is maintained (CSV), the models whose risk class changed in the period, and the devices marked missing.

Active is every status but retired (the Overview's and Settings' fleet): a device marked missing stays in the inventory until it is
found or retired. Risk class is the model's class today. Strategy is the model's own rule (DeviceModel.pm_interval_months): AEM only
when an approved alternate interval applies, never for a model excluded from AEM (life support, the CMS mark), whatever is on file.
A device's notes are never read: they are free text.

Gaps: GAP an active device with no next PM date (it is on no PM schedule; the device's Edit sets one), but for a new device waiting
for its incoming inspection (slice 26: Asset.awaiting_inspection; its PM schedule starts when the inspection passes, and the incoming
inspection section lists it). FINDING a life-support or high-risk device marked missing, with the day it went missing (from its
history). CHECK a risk-scored model with active devices whose yearly risk review is overdue (equipment.services.risk_review_due),
linked to the model's drawer where the score is reviewed. A device's status is worded as every screen words it (equipment.services
.status_label's rule: "Held for incident" for a device held as evidence (slice 28), first; "Awaiting inspection" for a device out of
service waiting for its incoming inspection).

Slice 29, rentals, vendor loaners, and demo units on site (Asset.ownership not ours: equipment.services.OWNED): on the inventory
while on site, in a table of their own ("Temporary equipment on site": whose, the owner, the agreement, PO, or RMA number, when it
arrived and is due back, the day its incoming inspection passed, and its owner's PM date) with their figures. The figures by class,
the device list, and its strategy are the facility's own devices (ours, as on the Overview): a temporary device is on no PM schedule
of ours, so it is never a no-next-PM gap. Instead, for one not waiting out of use for its incoming inspection (that inspection checks
the owner's PM label, and refuses a pass while its date is past): GAP its owner's PM date is past (record the date on the owner's new
sticker, or return it); GAP a life-support or high-risk one with no owner's PM date recorded, CHECK any other. Devices marked missing
are every device on site, ours or not. A returned one (status retired) is off the inventory, and would read "Returned to owner".

Queries: three for the figures and gaps, plus two when devices are missing (them, and their history in one read), plus one when
temporary devices are on site (them, with the day each passed its incoming inspection); the device list (two) and the risk class
changes (one) are read only when their rows are.
"""
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows, who
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, Ownership, RiskClass
from apps.equipment.services import AWAITING_LABEL, HELD_LABEL, OWNED, RETURNED_LABEL, risk_review_due, risk_review_due_on
from apps.workorders.models import InspectionResult, WorkOrder, WoStatus, WoType

from . import CHECK, DEVICE, FINDING, GAP, Figure, Gap, Period, Section, Table

CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
SERIOUS = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH)  # the classes whose missing devices are findings
CLASS_FIGURES = {RiskClass.LIFE_SUPPORT: "Life-support devices", RiskClass.HIGH: "High-risk devices", RiskClass.MEDIUM: "Medium-risk devices",
                 RiskClass.LOW: "Low-risk devices"}
NOT_RECORDED = "Not recorded"
ROW_CHUNK = 2000

_CLASS_LABELS = dict(RiskClass.choices)
_STATUS_LABELS = dict(AssetStatus.choices)
_ADDED_LABELS = dict(AddedAs.choices)

DEVICE_COLUMNS = ["Tag", "Manufacturer", "Model", "Description", "Category", "Risk class", "Department", "Room", "Status", "Strategy",
                  "PM interval (months)", "Last PM", "Next PM", "Added as"]


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _class(value: str) -> str:
    return _CLASS_LABELS.get(value, value)


def _status(status: str, awaiting: bool, held: bool = False, temporary: bool = False) -> str:
    """A device's status as equipment.services.status_label words it, from its columns: held first (slice 28), then a temporary
    device that went back to its owner (slice 29: "Returned to owner", never "Retired"), then awaiting inspection."""
    if held:
        return HELD_LABEL
    if temporary and status == AssetStatus.RETIRED:
        return RETURNED_LABEL
    return AWAITING_LABEL if awaiting and status == AssetStatus.OUT_OF_SERVICE else _STATUS_LABELS.get(status, status)


# A device with no next PM, so on no PM schedule: a gap, except a new device waiting out of service for its incoming inspection (slice
# 26), whose PM schedule starts when it passes. One in use before its inspection (use_before_inspection) is listed, whatever day it was
# added (review fix: it is on a patient with no PM schedule, and the incoming inspection section only covers devices added in the period).
# Slice 29: ours only. A rental, vendor loaner, or demo unit is on no PM schedule of ours by design (its owner maintains it): its
# owner's PM date is what the binder asks of it (_temporary_gaps).
WAITING_OUT_OF_USE = Q(awaiting_inspection=True, status__in=(AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING))
NO_NEXT_PM = OWNED & Q(next_pm_on__isnull=True) & ~WAITING_OUT_OF_USE


def strategy(dm: DeviceModel) -> str:
    """"AEM" when an approved alternate interval is the one in force (the model's own rule), else "OEM"."""
    return "AEM" if dm.pm_interval_months != dm.oem_pm_interval_months else "OEM"


def _active():
    """Every device on site: every status but retired, ours or not (slice 29)."""
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES)


def _every_device():
    """Every active device of ours, by tag (the CSV; never printed in full). Two queries whatever the fleet's size: the models, then the
    devices streamed in chunks. Slice 29: the temporary devices on site have their own table."""
    models = {dm.pk: dm for dm in DeviceModel.objects.all()}
    rows = (_active().filter(OWNED).order_by("tag").values_list("tag", "device_model_id", "department__name", "room", "status", "awaiting_inspection",
                                                  "incident_hold", "last_pm_on", "next_pm_on", "added_as"))
    for tag, dm_id, department, room, status, awaiting, held, last_pm, next_pm, added_as in rows.iterator(chunk_size=ROW_CHUNK):
        dm = models[dm_id]
        yield [tag, dm.manufacturer, dm.model, dm.description, dm.category, _class(dm.risk_class), department, room,
               _status(status, awaiting, held), strategy(dm), dm.pm_interval_months, last_pm, next_pm, _ADDED_LABELS.get(added_as, NOT_RECORDED)]


def _risk_changes(period: Period):
    """Each change of a model's risk class whose day (the facility's) is in the period, from the models' history: one read of this
    facility's rows, in order, each compared with the row before it."""
    rows = (_rows(DeviceModel.history.model).select_related("history_user").order_by("id", "history_date", "history_id"))
    out, before = [], {}
    for rec in rows.iterator(chunk_size=ROW_CHUNK):
        previous = before.get(rec.id)
        before[rec.id] = rec.risk_class
        if rec.history_type != "~" or previous is None or previous == rec.risk_class:
            continue
        day = local_day(rec.history_date)
        if period.start <= day <= period.end:
            out.append((day, f"{rec.manufacturer} {rec.model}", _class(previous), _class(rec.risk_class), who(rec)))
    out.sort(key=lambda r: (r[0], r[1]))
    for day, name, old, new, by in out:
        yield [name, old, new, day, by]


def _missing_since(asset_ids) -> dict:
    """{device id: the facility's day it last went missing}, from the devices' history (one read)."""
    out, previous = {}, {}
    rows = (_rows(Asset.history.model).filter(id__in=asset_ids).order_by("id", "history_date", "history_id")
            .values_list("id", "status", "history_date"))
    for pk, status, when in rows:
        if status == AssetStatus.MISSING and previous.get(pk) != AssetStatus.MISSING:
            out[pk] = local_day(when)
        previous[pk] = status
    return out


# --- temporary equipment on site (slice 29) -----------------------------------------------------------------------------------

TEMPORARY_COLUMNS = ["Tag", "Model", "Risk class", "Department", "Status", "Whose", "Owner", "Reference", "Arrived", "Due back",
                     "Incoming inspection passed on", "Owner's PM due"]
ALREADY_ON_SITE = "Already on site when entered"  # an EXISTING one: counted by the incoming inspection section, never asked for one
_INSPECTED = (WoStatus.COMPLETED, WoStatus.CLOSED)
_WHOSE = dict(Ownership.choices)


def _passed_on() -> Subquery:
    """The day the device's incoming inspection passed: its first Incoming inspection work order completed (or closed) with result
    Passed or none recorded, never Failed (the incoming inspection section's evidence rule). A correlated subquery."""
    return Subquery(WorkOrder.objects.filter(asset=OuterRef("pk"), type=WoType.INSPECTION, status__in=_INSPECTED, completed_on__isnull=False)
                    .exclude(inspection_result=InspectionResult.FAILED).order_by("completed_on", "number").values("completed_on")[:1])


def _temporary_on_site() -> list[dict]:
    """Every rental, vendor loaner, and demo unit on site (not returned), by tag, with the day it passed its incoming inspection. One
    query. Never the device's notes."""
    return list(_active().exclude(OWNED).annotate(passed_on=_passed_on()).order_by("tag").values(
        "tag", "status", "awaiting_inspection", "incident_hold", "ownership", "owner", "owner_reference", "arrived_on", "due_back_on",
        "owner_pm_due_on", "added_as", "passed_on", dept=F("department__name"), make=F("device_model__manufacturer"),
        model=F("device_model__model"), rc=F("device_model__risk_class")))


def _waiting_out_of_use(d: dict) -> bool:
    """WAITING_OUT_OF_USE for one row: new, waiting out of service (or missing) for its incoming inspection, never put in use."""
    return d["awaiting_inspection"] and d["status"] in (AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING)


def _temporary_row(d: dict) -> list:
    passed = d["passed_on"] or (ALREADY_ON_SITE if d["added_as"] == AddedAs.EXISTING else None)
    return [d["tag"], f"{d['make']} {d['model']}", _class(d["rc"]), d["dept"],
            _status(d["status"], d["awaiting_inspection"], d["incident_hold"], temporary=True), _WHOSE.get(d["ownership"], d["ownership"]),
            d["owner"], d["owner_reference"], d["arrived_on"], d["due_back_on"], passed, d["owner_pm_due_on"]]


def _temporary_gaps(devices: list[dict], today) -> list[Gap]:
    """The owner's PM date of each temporary device in use or ready for it (not one waiting out of use for its incoming inspection,
    whose checklist checks the owner's PM label and whose pass waits for a current date): GAP when it is past (the owner does the PM,
    or the date on their new sticker is recorded; else it goes back); GAP when none is recorded on a life-support or high-risk one,
    CHECK on any other."""
    gaps = []
    for d in devices:
        if _waiting_out_of_use(d):
            continue
        tag, rc = d["tag"], d["rc"]
        whose = f"{_WHOSE.get(d['ownership'], d['ownership']).lower()} from {d['owner']}" if d["owner"] else _WHOSE.get(d["ownership"], "").lower()
        risk = "life support" if rc == RiskClass.LIFE_SUPPORT else f"{_class(rc).lower()} risk"
        what = f"{tag} ({d['make']} {d['model']}, {risk}, {whose})"
        due, url = d["owner_pm_due_on"], reverse("web:asset", args=[tag])
        if due is not None and due < today:
            gaps.append(Gap(GAP, f"{what} is on site with its owner's PM past due since {_day(due)}: record the date on the owner's new PM "
                                 "sticker, or return it to its owner.", url, tag))
        elif due is None:
            gaps.append(Gap(GAP if rc in SERIOUS else CHECK, f"{what} is on site with no owner's PM date recorded: record the date on its "
                                                               "owner's PM sticker.", url, tag))
    return gaps


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def build(period: Period, user) -> Section:
    today = period.today
    by_category, by_class = {}, {rc: 0 for rc in CLASS_ORDER}
    missing_count = no_pm_count = temporary_count = 0
    # One grouped read of every device on site: ours by category and class (the figures and the table), the temporary ones counted
    # apart (slice 29), and the missing and no-next-PM counts over both (NO_NEXT_PM is ours only).
    grouped = (_active().order_by().values("device_model__category", "device_model__risk_class")
               .annotate(n=Count("id", filter=OWNED), temporary=Count("id", filter=~OWNED),
                         missing=Count("id", filter=Q(status=AssetStatus.MISSING)), no_pm=Count("id", filter=NO_NEXT_PM)))
    for row in grouped:
        category, rc, n = row["device_model__category"], row["device_model__risk_class"], row["n"]
        if n:
            counts = by_category.setdefault(category, {c: 0 for c in CLASS_ORDER})
            counts[rc] = counts.get(rc, 0) + n
            by_class[rc] = by_class.get(rc, 0) + n
        temporary_count += row["temporary"]
        missing_count += row["missing"]
        no_pm_count += row["no_pm"]
    total = sum(by_class.values())
    temporary = _temporary_on_site() if temporary_count else []

    in_use = list(DeviceModel.objects.annotate(active=Count("assets", filter=Q(assets__status__in=Asset.ACTIVE_STATUSES)))
                  .filter(active__gt=0).order_by("manufacturer", "model"))
    unscored = sum(1 for dm in in_use if dm.risk_score is None)

    gaps = []
    if no_pm_count:
        for tag, mfr, model, awaiting in (_active().filter(NO_NEXT_PM).order_by("tag")
                                          .values_list("tag", "device_model__manufacturer", "device_model__model", "awaiting_inspection")):
            if awaiting:  # in use before its incoming inspection: its PM schedule starts when the inspection passes
                text = (f"{tag} ({mfr} {model}) is in use before its incoming inspection, so it is on no PM schedule until that inspection "
                        "passes. Complete its inspection.")
            else:
                text = f"{tag} ({mfr} {model}) is in use with no next PM date, so it is on no PM schedule. Set its next PM."
            gaps.append(Gap(GAP, text, reverse("web:asset", args=[tag]), tag))

    missing = []
    if missing_count:
        devices = list(Asset.objects.filter(status=AssetStatus.MISSING).order_by("tag")
                       .values_list("id", "tag", "device_model__manufacturer", "device_model__model", "device_model__risk_class"))
        since = _missing_since([d[0] for d in devices])
        for pk, tag, mfr, model, rc in devices:
            day = since.get(pk)
            missing.append([tag, f"{mfr} {model}", _class(rc), day])
            if rc in SERIOUS:
                when = f"since {_day(day)}" if day else "(since when is not recorded)"
                gaps.append(Gap(FINDING, f"{tag} ({mfr} {model}, {_class(rc).lower()}) has been missing {when}.",
                                reverse("web:asset", args=[tag]), tag))
    gaps += _temporary_gaps(temporary, today)

    for dm in in_use:
        if dm.risk_score is not None and risk_review_due(dm, today):
            due = risk_review_due_on(dm)
            last = f"last reviewed {_day(dm.risk_reviewed_on)}" if dm.risk_reviewed_on else "no review date on record"
            when = f"was due {_day(due)}" if due else "is due"
            gaps.append(Gap(CHECK, f"{dm.manufacturer} {dm.model}'s risk score {when} for the facility's yearly review ({last}).",
                            reverse("web:pm_model", args=[dm.pk]), f"{dm.manufacturer} {dm.model}"))

    categories = sorted(by_category)
    category_rows = [[c] + [by_category[c][rc] for rc in CLASS_ORDER] + [sum(by_category[c].values())] for c in categories]
    figures = [Figure("Active devices", total, "every status but retired")]
    figures += [Figure(CLASS_FIGURES[rc], by_class[rc]) for rc in CLASS_ORDER]
    figures += [Figure("Models not risk-scored", unscored, "models in use whose risk class was set by hand"),
                Figure("Devices marked missing", missing_count, "still in the inventory until found or retired")]
    kinds = {k: sum(1 for d in temporary if d["ownership"] == k) for k in Ownership.values}
    temporary_rows = [_temporary_row(d) for d in temporary]
    figures += [
        Figure("Temporary devices on site", temporary_count,
               f"{_plural(kinds[Ownership.RENTAL], 'rental', 'rentals')}, {_plural(kinds[Ownership.LOANER], 'vendor loaner', 'vendor loaners')}, "
               f"{_plural(kinds[Ownership.DEMO], 'demo or evaluation unit', 'demo or evaluation units')}; maintained by their owners"),
        Figure("Temporary devices past due back", sum(1 for d in temporary if d["due_back_on"] and d["due_back_on"] < today),
               "still on site after the day they were due back"),
    ]
    tables = [
        Table("by_category", "Active devices by category and risk class", ["Category"] + [_class(rc) for rc in CLASS_ORDER] + ["Total"],
              lambda: iter(category_rows), count=len(category_rows), empty="No devices in use."),
        Table("devices", "Every active device", DEVICE_COLUMNS, _every_device, count=total, links={0: DEVICE}, printed=False,
              empty="No devices in use."),
        Table("risk_changes", "Risk class changes in the period", ["Model", "From", "To", "Changed on", "By"], lambda: _risk_changes(period),
              empty="No model's risk class changed in this period."),
        Table("missing", "Devices marked missing", ["Tag", "Model", "Risk class", "Missing since"], lambda: iter(missing), count=len(missing),
              links={0: DEVICE}, empty="No device is marked missing."),
        Table("temporary", "Temporary equipment on site", TEMPORARY_COLUMNS, lambda: iter(temporary_rows), count=len(temporary_rows),
              links={0: DEVICE}, empty="No rental, vendor loaner, or demo unit is on site."),
    ]
    notes = [
        "Active devices are every device of the facility's own not retired (owned, leased, or placed: on its PM program), as on the "
        "Overview; a device marked missing stays in the inventory until it is found or retired.",
        "Rentals, vendor loaners, and demo or evaluation units are on the inventory while on site, in their own table: their owners "
        "maintain them, so they are on no PM schedule of the facility's. The binder asks each for its owner's PM date, from the owner's "
        "sticker: one past is a gap (the owner does the PM, or the new date is recorded; else it goes back), and none recorded is a gap "
        "for life-support and high-risk equipment and a check for the rest. One waiting out of use for its incoming inspection is not "
        "asked yet: that inspection checks the owner's PM label. Devices marked missing include them.",
        "Incoming inspection passed on: the day the first incoming inspection of the unit passed, as the incoming inspection section "
        f"reads it; \"{ALREADY_ON_SITE}\" for one entered after it was already in use here.",
        "Risk class is each model's class today: set by its risk score when the model is scored, else by hand.",
        "Strategy: OEM follows the manufacturer's PM interval; AEM an alternate interval the facility approved. Life-support models and "
        "equipment CMS keeps on the manufacturer's schedule never use one, whatever interval is on file.",
        "Added as: new to the facility, already in use here when entered, or imported from the previous system; \"Not recorded\" for "
        "devices added before Cadence recorded how.",
        "A new device waiting for its incoming inspection, out of service, has no next PM date until the inspection passes (its PM "
        "schedule starts then), so it is not listed for one; the incoming inspection section lists it. One put in use before its "
        "inspection is listed, whatever day it was added: it is in use on no PM schedule. Its status reads \"Awaiting inspection\" while it "
        "waits out of service.",
        "Missing since is the day the device was last marked missing, from its history.",
        "The full device list is in its CSV.",
    ]
    return Section(key="inventory", title="Medical equipment inventory",
                   topic="The facility's inventory of medical equipment: every device in use, its risk class, and how it is maintained.",
                   covers=f"Devices {period.as_of_today}; risk class changes {period.label}", figures=figures, gaps=gaps, tables=tables,
                   notes=notes)
