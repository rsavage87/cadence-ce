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

Queries: three for the figures and gaps, plus two when devices are missing (them, and their history in one read); the device list
(two) and the risk class changes (one) are read only when their rows are.
"""
from django.db.models import Count, Q
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows, who
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, RiskClass
from apps.equipment.services import AWAITING_LABEL, HELD_LABEL, risk_review_due, risk_review_due_on

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


def _status(status: str, awaiting: bool, held: bool = False) -> str:
    """A device's status as equipment.services.status_label words it, from the three columns: held first (slice 28)."""
    if held:
        return HELD_LABEL
    return AWAITING_LABEL if awaiting and status == AssetStatus.OUT_OF_SERVICE else _STATUS_LABELS.get(status, status)


# A device with no next PM, so on no PM schedule: a gap, except a new device waiting out of service for its incoming inspection (slice
# 26), whose PM schedule starts when it passes. One in use before its inspection (use_before_inspection) is listed, whatever day it was
# added (review fix: it is on a patient with no PM schedule, and the incoming inspection section only covers devices added in the period).
WAITING_OUT_OF_USE = Q(awaiting_inspection=True, status__in=(AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING))
NO_NEXT_PM = Q(next_pm_on__isnull=True) & ~WAITING_OUT_OF_USE


def strategy(dm: DeviceModel) -> str:
    """"AEM" when an approved alternate interval is the one in force (the model's own rule), else "OEM"."""
    return "AEM" if dm.pm_interval_months != dm.oem_pm_interval_months else "OEM"


def _active():
    return Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES)


def _every_device():
    """Every active device, by tag (the CSV; never printed in full). Two queries whatever the fleet's size: the models, then the
    devices streamed in chunks."""
    models = {dm.pk: dm for dm in DeviceModel.objects.all()}
    rows = (_active().order_by("tag").values_list("tag", "device_model_id", "department__name", "room", "status", "awaiting_inspection",
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


def build(period: Period, user) -> Section:
    today = period.today
    by_category, by_class = {}, {rc: 0 for rc in CLASS_ORDER}
    missing_count = no_pm_count = 0
    grouped = (_active().order_by().values("device_model__category", "device_model__risk_class")
               .annotate(n=Count("id"), missing=Count("id", filter=Q(status=AssetStatus.MISSING)), no_pm=Count("id", filter=NO_NEXT_PM)))
    for row in grouped:
        category, rc, n = row["device_model__category"], row["device_model__risk_class"], row["n"]
        counts = by_category.setdefault(category, {c: 0 for c in CLASS_ORDER})
        counts[rc] = counts.get(rc, 0) + n
        by_class[rc] = by_class.get(rc, 0) + n
        missing_count += row["missing"]
        no_pm_count += row["no_pm"]
    total = sum(by_class.values())

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
    tables = [
        Table("by_category", "Active devices by category and risk class", ["Category"] + [_class(rc) for rc in CLASS_ORDER] + ["Total"],
              lambda: iter(category_rows), count=len(category_rows), empty="No devices in use."),
        Table("devices", "Every active device", DEVICE_COLUMNS, _every_device, count=total, links={0: DEVICE}, printed=False,
              empty="No devices in use."),
        Table("risk_changes", "Risk class changes in the period", ["Model", "From", "To", "Changed on", "By"], lambda: _risk_changes(period),
              empty="No model's risk class changed in this period."),
        Table("missing", "Devices marked missing", ["Tag", "Model", "Risk class", "Missing since"], lambda: iter(missing), count=len(missing),
              links={0: DEVICE}, empty="No device is marked missing."),
    ]
    notes = [
        "Active devices are every device not retired, as on the Overview; a device marked missing stays in the inventory until it is "
        "found or retired.",
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
