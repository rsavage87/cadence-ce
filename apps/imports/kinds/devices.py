"""
Devices (slice 23): the facility's equipment inventory from the CMMS it is leaving, one row per device by its asset tag, through the
equipment services (create_asset, update_asset, set_status, create_department, create_device_model) and their rules. Also what
`manage.py import_assets` runs.

A device not here yet is added in service or out of service, then moved to on loan, missing, or retired when the file says so; one
retired in the file is retired on its "retired on" day, so its history reads in order and the AEM evidence counts its years in use
(apps.pm.aem). Its model and department are found by name in any letter case, or added: a model the import adds takes its
description, category, risk class, PM interval, and the CMS mark from the row that first names it; a model already in the catalog
keeps all of those. A device already here (its tag in any letter case) changes only where the file has a value: a column the person
did not choose, or a blank cell, keeps what Cadence has.

Nothing it cannot read becomes a default without a note. A date that breaks the device rules (a last PM before the install date, a
warranty that ends before it, an install date in the future, a next PM more than ten years out) or that means "none" is left out,
and the device still comes in. A status change the rules refuse (retiring a device with open work) is noted and the row's other
changes stay. Retiring, reinstating, and the CMS mark need Equipment Approve, as on the screen. A device retired without a day it
can use is retired as of today, and the row says so: the AEM evidence counts it in use until then.

The check rolls each chunk back (apps.imports.services), so a model or department a row of an earlier chunk adds is not there when
a later chunk is checked, though the import, which committed that chunk, finds it and never reads the later row's model cells. The
check therefore adds it first, quietly, from the row before this chunk that adds it in the import (its notes and values read are
that row's, reported in its own chunk): the first row naming it that the import does not skip whatever its model (a tag missing,
repeated, or refused by the device rules; a value too long; a new device retired by a user who may not retire), whose own values
for the model are accepted. The one skip the check cannot see in an earlier chunk is a line with more values than the file has
columns.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models.functions import Lower

from apps.accounts.models import Level, Module
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import TAG_VALIDATOR, Asset, AssetStatus, Department, DeviceModel, RiskClass

from .. import parse
from ..base import Column, Importer, RowResult, RowSkip

UNASSIGNED = "Unassigned"
UNCATEGORIZED = "Uncategorized"
DEFAULT_INTERVAL = 12

STATUS_WORDS = {
    **dict.fromkeys(("in service", "in-service", "active", "in use"), AssetStatus.IN_SERVICE),
    "in repair": AssetStatus.IN_REPAIR,  # read as out of service: in Cadence a work order puts a device in repair
    **dict.fromkeys(("out of service", "out-of-service", "oos"), AssetStatus.OUT_OF_SERVICE),
    **dict.fromkeys(("on loan", "on-loan", "loaner"), AssetStatus.ON_LOAN),
    "missing": AssetStatus.MISSING,
    **dict.fromkeys(("retired", "disposed", "inactive", "surplus", "salvaged"), AssetStatus.RETIRED),
}
STATUSES_SAID = "in service, out of service, on loan, missing, or retired"
# Words only: a number ("3", an EM score) means something different in every system, so it is not read.
RISK_WORDS = {
    **dict.fromkeys(("life support", "life-support", "critical"), RiskClass.LIFE_SUPPORT),
    **dict.fromkeys(("high", "high risk"), RiskClass.HIGH),
    **dict.fromkeys(("medium", "moderate", "medium risk", "moderate risk"), RiskClass.MEDIUM),
    **dict.fromkeys(("low", "low risk"), RiskClass.LOW),
}

# How the notes and the "Changes" totals name a device's values. DROPPABLE: those the device rules may refuse that a row can do
# without (a date, the cost): a refused one is left out with a note and the rest goes in; a refusal of anything else skips the row.
WORDS = {"serial": "Serial number", "device_model": "Device model", "department": "Department", "room": "Room", "installed_on": "Install date",
         "acquisition_cost": "Acquisition cost", "warranty_end": "Warranty end", "last_pm_on": "Last PM", "next_pm_on": "Next PM",
         "retired_on": "Retired on"}
DROPPABLE = {"installed_on", "warranty_end", "last_pm_on", "next_pm_on", "acquisition_cost", "retired_on"}
SERVICE_KEYS = {"added_on": "retired_on", "changed_on": "retired_on"}  # the services' names for the retirement day
DATES = {"installed": "installed_on", "warranty": "warranty_end", "last_pm": "last_pm_on", "next_pm": "next_pm_on", "retired_on": "retired_on"}
NEW_DEVICE_OUTCOME = {"next_pm_on": "worked out from the PM interval", "retired_on": "retired as of today"}  # else "left blank"


def _words(value) -> str:
    """A name as the equipment services store it: single spaces."""
    return " ".join((value or "").split())


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _tag_refused(tag: str) -> bool:
    """Whether create_asset refuses a new device's tag (which skips the row, and takes back the model and department it added)."""
    try:
        TAG_VALIDATOR(tag)
    except ValidationError:
        return True
    return tag.lower() in eq.RESERVED_TAGS


class _Later:
    """The row's part of the run's summary, handed to the chunk's Context only once the row has gone in: a row skipped half way
    (its savepoint rolled back) adds no department, model, or total that will never exist."""

    def __init__(self, ctx):
        self.ctx, self.calls = ctx, []

    def total(self, group, label, amount=1):
        self.calls.append((self.ctx.total, (group, label, amount)))

    def add_created(self, group, name):
        self.calls.append((self.ctx.add_created, (group, name)))

    def read_as(self, column, value, shown):
        self.calls.append((self.ctx.read_as, (column, value, shown)))

    def flush(self):
        for call, args in self.calls:
            call(*args)


class DevicesImporter(Importer):
    kind = "devices"
    label = "Devices"
    module = Module.EQUIPMENT
    level = Level.EDIT
    key = "tag"
    order = 10
    description = ("Your equipment inventory: one row per device, by its asset tag. Devices already here are updated where the file has "
                   "a value; models and departments it does not find are added.")
    columns = [
        Column("tag", "Asset tag", ("asset tag", "tag", "control number", "control no", "control #", "equipment #", "equipment id", "equip #",
                                    "asset id", "asset number", "ce number", "ce #"), required=True, max_length=40, example="CE-10042",
               help="The number on the CE sticker. A device already here is found by it in any letter case."),
        Column("serial", "Serial number", ("serial number", "serial", "serial #", "serial no", "sn", "s/n"), max_length=80, example="AL8015-22914"),
        Column("manufacturer", "Manufacturer", ("manufacturer", "mfr", "mfg", "make", "manufacturer name"), required=True, max_length=120,
               example="BD", help="With the model, names the device model; a device already here keeps its model when either is blank."),
        Column("model", "Model", ("model", "model number", "model #", "model no", "model name"), required=True, max_length=120,
               example="Alaris 8015 PCU"),
        Column("description", "Description", ("description", "device description", "device name", "nomenclature", "equipment type", "device type"),
               max_length=200, example="Infusion pump", help="What the device is. Read for a model the import adds."),
        Column("category", "Category", ("category", "device category", "product category"), max_length=80, example="Infusion pumps",
               help="Read for a model the import adds."),
        Column("risk", "Risk class", ("risk class", "risk", "risk level", "risk category"), example="High",
               help="Life support, high, medium, or low (words; a number is not read). Read for a model the import adds."),
        Column("department", "Department", ("department", "dept", "department name", "cost center name"),
               max_length=80, example="ICU", help="A new device without one goes to Unassigned."),  # 80: what create_department keeps
        Column("room", "Room", ("room", "room number", "sub location", "sublocation"), max_length=40, example="4B"),
        Column("status", "Status", ("status", "asset status", "device status", "equipment status"), example="In service",
               help=f"{STATUSES_SAID.capitalize()}. Blank: a new device is in service, one already here keeps its status."),
        Column("installed", "Installed", ("installed", "install date", "installed on", "installation date", "acceptance date", "in service date",
                                          "purchase date"), example="2021-03-15"),
        Column("cost", "Acquisition cost", ("acquisition cost", "cost", "purchase cost", "purchase price"), example="3200.00"),
        Column("pm_interval", "PM interval", ("pm interval", "pm frequency", "interval", "pm months", "pm interval months"), example="12",
               help="Months, or words such as annual or quarterly. Read for a model the import adds."),
        Column("last_pm", "Last PM", ("last pm", "last pm date", "last inspection", "last inspection date"), example="2026-03-02"),
        Column("next_pm", "Next PM", ("next pm", "next pm date", "next pm due", "next due", "due date"), example="2027-03-02",
               help="Blank: worked out from the last PM or the install date and the model's interval, as Add device does."),
        Column("warranty", "Warranty end", ("warranty end", "warranty", "warranty expiration", "warranty expires", "warranty end date"),
               example="2024-03-15"),
        Column("oem_schedule", "OEM schedule required", ("oem schedule required", "manufacturer's schedule required", "manufacturer schedule required",
                                                         "oem schedule", "cms oem schedule"), example="no",
               help="Yes for imaging, radiologic, and medical laser equipment (CMS keeps it on the manufacturer's schedule). Read for a model "
                    "the import adds; needs Equipment Approve."),
        Column("retired_on", "Retired on", ("retired on", "retirement date", "retired date", "date retired", "disposal date", "disposed on"),
               example="", help="The day a retired device left service."),
    ]

    # --- lookups ------------------------------------------------------------------------------------------------------------------

    def load(self, ctx, rows):
        """The chunk's devices, its models and departments already in the catalog, and what the importing user may do. Only rows
        that were here before the chunk are kept: one a row adds could be rolled back with that row, so later rows look it up again."""
        tags = {r["tag"].lower() for r in rows if r.get("tag")}
        ctx.cache["assets"] = {a.tag.lower(): a for a in Asset.objects.annotate(tag_lower=Lower("tag")).filter(tag_lower__in=tags)
                               .select_related("device_model", "department")}
        ctx.cache["departments"] = {d.name.lower(): d for d in Department.objects.all()}
        makers = {_words(r.get("manufacturer")).lower() for r in rows if r.get("manufacturer")}
        ctx.cache["models"] = {(m.manufacturer.lower(), m.model.lower()): m
                               for m in DeviceModel.objects.annotate(maker=Lower("manufacturer")).filter(maker__in=makers)}
        ctx.cache["level"] = Level.FULL if ctx.user is None else ctx.user.level_for(eq_perms.MODULE)  # None: the command line, may do all

    def _may(self, ctx, needed: int) -> bool:
        return ctx.cache["level"] >= needed

    def _department(self, ctx, name: str):
        return ctx.cache["departments"].get(name.lower()) or Department.objects.filter(name__iexact=name).first()

    def _device_model(self, ctx, manufacturer: str, model: str):
        return (ctx.cache["models"].get((manufacturer.lower(), model.lower()))
                or DeviceModel.objects.filter(manufacturer__iexact=manufacturer, model__iexact=model).first())

    # --- one row --------------------------------------------------------------------------------------------------------------------

    def apply(self, ctx, row, result):
        ctx.cache.setdefault("first", ctx.index)  # the chunk's first row: the check reads the rows before it (_earlier)
        tag = row.get("tag") or ""
        if not tag:
            raise RowSkip("No asset tag: a device is found or added by its tag")
        later = _Later(ctx)
        asset = ctx.cache["assets"].get(tag.lower())
        if asset is None:
            self._add(ctx, row, result, later, tag)
        else:
            self._change(ctx, row, result, later, asset)
        later.flush()

    def _add(self, ctx, row, result, later, tag):
        status = self._status(row, result, later, current=None) or AssetStatus.IN_SERVICE
        if status == AssetStatus.RETIRED and not self._may(ctx, eq_perms.RETIRE_LEVEL):
            raise RowSkip("Retired in the file: adding a retired device needs Equipment Approve")
        device_model = self._model(ctx, row, result, later, adding=True)
        department = self._department_of(ctx, row, result, later, adding=True)
        # A retired device has no next PM; the retirement day is read only for a device that is retired.
        unused = ("next_pm_on",) if status == AssetStatus.RETIRED else ("retired_on",)
        values = {k: v for k, v in self._dates(row, result, kept=False, unused=unused).items() if v is not None}
        if status == AssetStatus.RETIRED and not row.get("retired_on"):
            result.warn("No retired-on date: retired as of today")
        cost = self._cost(row, result, later, kept=False)
        if cost is not None:
            values["acquisition_cost"] = cost
        then = status if status in (AssetStatus.ON_LOAN, AssetStatus.MISSING, AssetStatus.RETIRED) else None

        def add(v):
            asset = eq.create_asset(tag=tag, device_model=device_model, department=department, serial=_words(row.get("serial")),
                                    room=_words(row.get("room")), status=AssetStatus.OUT_OF_SERVICE if status == AssetStatus.OUT_OF_SERVICE
                                    else AssetStatus.IN_SERVICE, by=ctx.user, today=ctx.today, added_on=v.get("retired_on"),
                                    **{k: x for k, x in v.items() if k != "retired_on"})
            if then:
                eq.set_status(asset, then, by=ctx.user, note="Imported", today=ctx.today, changed_on=v.get("retired_on"))
            return asset

        asset = self._attempt(add, values, result)
        if status != AssetStatus.RETIRED:
            if "next_pm_on" not in values:
                later.total("Next PM", "worked out")
            self._count_due(ctx, asset, later)
        result.outcome = result.CREATE
        later.total("Devices by status", asset.get_status_display())

    def _change(self, ctx, row, result, later, asset):
        changed = []
        status = self._status(row, result, later, current=asset.status)
        if status and status != asset.status and self._set_status(ctx, asset, status, result):
            changed.append("Status")
            if status == AssetStatus.RETIRED and row.get("retired_on"):
                result.warn("Retired on not used: a device already here is retired as of today")
        fields = {}
        device_model = self._model(ctx, row, result, later, adding=False)
        if device_model is not None and device_model.pk != asset.device_model_id:
            fields["device_model"] = device_model
        department = self._department_of(ctx, row, result, later, adding=False)
        if department is not None and department.pk != asset.department_id:
            fields["department"] = department
        for key in ("serial", "room"):
            value = _words(row.get(key))
            if value and value != getattr(asset, key):
                fields[key] = value
        # A retired device has no next PM. The retirement day dates a device the import adds already retired; one here is retired
        # (when the file says so) as of today, after the history it has in Cadence.
        unused = ("retired_on", "next_pm_on") if asset.status == AssetStatus.RETIRED else ("retired_on",)
        for name, value in self._dates(row, result, kept=True, unused=unused).items():
            if value is not None and value != getattr(asset, name):
                fields[name] = value
        cost = self._cost(row, result, later, kept=True)
        if cost is not None and cost != asset.acquisition_cost:
            fields["acquisition_cost"] = cost
        if fields:
            def update(v):
                last_pm = {"imported_last_pm": v["last_pm_on"]} if "last_pm_on" in v else {}
                return eq.update_asset(asset, by=ctx.user, today=ctx.today, **last_pm, **{k: x for k, x in v.items() if k != "last_pm_on"})

            self._attempt(update, fields, result)
            changed += [WORDS[f] for f in fields]
        for label in changed:
            later.total("Changes", label)
        if changed:
            result.outcome = result.UPDATE
            if "Next PM" in changed or "Status" in changed:
                self._count_due(ctx, asset, later)
        later.total("Devices by status", asset.get_status_display())

    def _set_status(self, ctx, asset, status, result) -> bool:
        """Move a device already here to the file's status through the drawer's rules; a change they refuse is noted and the row's
        other changes stay (set_status is atomic: a refusal rolls back only its own savepoint)."""
        if status == AssetStatus.OUT_OF_SERVICE and asset.status == AssetStatus.IN_REPAIR:
            return False  # already out of use, in repair through a work order here
        if not self._may(ctx, eq_perms.status_level(asset.status, status)):
            result.warn("Status kept: retiring or reinstating a device needs Equipment Approve")
            return False
        was = asset.get_status_display().lower()
        try:
            eq.set_status(asset, status, by=ctx.user, note="Imported", today=ctx.today)
        except ValidationError:
            asset.refresh_from_db()
            if status in eq.STATUS_CHANGES.get(asset.status, ()):
                result.warn("Status kept: the device has open work orders; complete or cancel them, then retire it")
            else:
                result.warn(f"Status kept: Cadence does not move a device from {was} to {AssetStatus(status).label.lower()}")
            return False
        return True

    def _attempt(self, call, values: dict, result):
        """call(values) in the services; a refusal of values the row can do without (a date against the device rules, a cost) leaves
        those out with a note and tries again. The install date goes first, since the other dates' rules are measured from it. A
        refusal of anything else is the row's (skipped). Each try is a savepoint, so a refused one leaves nothing behind (a device added
        and then refused its status)."""
        while True:
            try:
                with transaction.atomic():
                    return call(values)
            except ValidationError as e:
                if not hasattr(e, "error_dict"):
                    raise
                refused = {SERVICE_KEYS.get(k, k): messages for k, messages in e.message_dict.items()}
                if not refused or any(k not in DROPPABLE or k not in values for k in refused):
                    raise
                for name in (["installed_on"] if "installed_on" in refused else refused):
                    if name == "retired_on":
                        result.warn("Retired on left out (after today, or before the install date): retired as of today")
                    else:
                        result.warn(f"{WORDS[name]} left out: {_lower_first(refused[name][0])}")
                    del values[name]

    def _count_due(self, ctx, asset, later):
        """How many PMs the import puts due at once: on the import day, or already past."""
        if asset.status == AssetStatus.RETIRED or not asset.next_pm_on:
            return
        if asset.next_pm_on == ctx.today:
            later.total("Next PM", "due today")
        elif asset.next_pm_on < ctx.today:
            later.total("Next PM", "past due")

    # --- reading values -------------------------------------------------------------------------------------------------------------

    def _status(self, row, result, later, *, current):
        """The file's status (a slug), or None for blank or unreadable. "In repair" reads as out of service: in Cadence a work order
        puts a device in repair."""
        value = row.get("status") or ""
        try:
            status = parse.parse_choice(value, STATUS_WORDS, what="status")
        except parse.Unreadable:
            later.read_as("Status", value, "Not read")
            result.warn(f"Status not read ({STATUSES_SAID}): {'unchanged' if current else 'added in service'}")
            return None
        if status is None:
            return None
        if status == AssetStatus.IN_REPAIR:
            status = AssetStatus.OUT_OF_SERVICE
            if current != AssetStatus.IN_REPAIR:
                result.warn("In repair in the file: out of service here (in Cadence a work order puts a device in repair)")
        later.read_as("Status", value, AssetStatus(status).label)
        return status

    def _dates(self, row, result, *, kept: bool, unused=()) -> dict:
        """The date columns the file has, by field name (but those `unused`): None for a blank cell, and for a "none" placeholder or
        a value it cannot read (with a note)."""
        out = {}
        for key, name in DATES.items():
            if key not in row or name in unused:
                continue
            outcome = "unchanged" if kept else NEW_DEVICE_OUTCOME.get(name, "left blank")
            try:
                out[name] = parse.parse_date(row[key])
            except parse.Placeholder:
                out[name] = None
                result.warn(f"{WORDS[name]} is a placeholder for no date: {outcome}")
            except parse.Unreadable:
                out[name] = None
                result.warn(f"{WORDS[name]} not read: {outcome}")
        return out

    def _cost(self, row, result, later, *, kept: bool):
        try:
            cost = parse.parse_money(row.get("cost") or "")
        except parse.Unreadable:
            result.warn("Acquisition cost not read: " + ("unchanged" if kept else "the model's list cost is used"))
            return None
        if cost is not None:
            later.total("Acquisition cost", "in the file", cost)
        return cost

    def _department_of(self, ctx, row, result, later, *, adding: bool):
        """The row's department, found in any letter case or added; None (kept) for a blank one on a device already here."""
        name = _words(row.get("department"))
        if not name:
            if not adding:
                return None
            result.warn(f"No department: added to {UNASSIGNED}")
            name = UNASSIGNED
        found = self._department(ctx, name)
        if found is None and ctx.check:
            found = self._department_added_earlier(ctx, name)
        if found is not None:
            return found
        department = eq.create_department(name)
        later.add_created("Departments", department.name)
        return department

    def _model(self, ctx, row, result, later, *, adding: bool, before: int | None = None):
        """The row's device model, found by manufacturer and model in any letter case, or added with the row's details. None (kept)
        when either is blank on a device already here. `before`: the check's copy of an earlier row (_department_added_earlier),
        which finds only what the rows before that one add."""
        manufacturer, model = _words(row.get("manufacturer")), _words(row.get("model"))
        if not manufacturer or not model:
            if adding:
                raise RowSkip("A new device needs its manufacturer and model")
            if manufacturer or model:
                result.warn("Device model unchanged: it needs both the manufacturer and the model")
            return None
        found = self._device_model(ctx, manufacturer, model)
        if found is None and ctx.check:
            found = self._model_added_earlier(ctx, manufacturer, model, before)
        if found is not None:
            return found
        return self._add_model(ctx, row, result, later, manufacturer, model)

    def _add_model(self, ctx, row, result, later, manufacturer: str, model: str):
        mark = self._mark(ctx, row, result, later)
        device_model = eq.create_device_model(manufacturer=manufacturer, model=model, description=_words(row.get("description")) or model,
                                              category=_words(row.get("category")) or UNCATEGORIZED, risk_class=self._risk(row, result, later),
                                              oem_pm_interval_months=self._interval(row, result, later), oem_schedule_required=mark, by=ctx.user)
        later.add_created("Device models", f"{device_model.manufacturer} {device_model.model}")
        return device_model

    # --- the check: what earlier chunks add -------------------------------------------------------------------------------------

    def _earlier(self, ctx, key: tuple) -> list[tuple[int, dict]]:
        """In the check, the rows before this chunk naming `key` (("model", manufacturer, model) or ("department", name), in lower
        case), with their places in the file, leaving out those the import skips whatever they name: no tag, a tag the file repeats
        (prepare), a value too long for its column. Indexed once per chunk."""
        if "earlier" not in ctx.cache:
            rows = ctx.rows[:ctx.cache["first"]]
            repeated, named = self.prepare(rows), {}
            for i, row in enumerate(rows):
                if not row.get("tag") or i in repeated or any(c.max_length and len(row.get(c.key) or "") > c.max_length for c in self.columns):
                    continue
                manufacturer, model, department = (_words(row.get(k)).lower() for k in ("manufacturer", "model", "department"))
                if manufacturer and model:
                    named.setdefault(("model", manufacturer, model), []).append((i, row))
                if department:
                    named.setdefault(("department", department), []).append((i, row))
            ctx.cache["earlier"] = named
        return ctx.cache["earlier"].get(key, [])

    def _adding(self, ctx, row) -> bool | None:
        """For a row before this chunk: whether the import adds its device (else it changes one here), or None when the import skips
        it, taking back what it added: a new device's tag the device rules refuse, or one retired in the file by a user who may not."""
        if Asset.objects.filter(tag__iexact=row["tag"]).exists():
            return False
        try:
            retired = parse.parse_choice(row.get("status") or "", STATUS_WORDS, what="status") == AssetStatus.RETIRED
        except parse.Unreadable:
            retired = False
        if _tag_refused(row["tag"]) or (retired and not self._may(ctx, eq_perms.RETIRE_LEVEL)):
            return None
        return True

    def _model_added_earlier(self, ctx, manufacturer: str, model: str, before: int | None):
        """The check's copy of a model a row before this chunk (or before `before`) adds in the import: added again from the first
        such row whose values for it are accepted, with those values. Quietly: that row's notes, values read, and the model's place
        among those added were reported in its own chunk, and the CMS mark's refusal is that row's. None when no row adds it."""
        for i, earlier in self._earlier(ctx, ("model", manufacturer.lower(), model.lower())):
            if before is not None and i >= before:
                break
            if self._adding(ctx, earlier) is None:
                continue
            try:
                with transaction.atomic():
                    return self._add_model(ctx, earlier, RowResult(0, ""), _Later(ctx), _words(earlier["manufacturer"]), _words(earlier["model"]))
            except (RowSkip, ValidationError, PermissionDenied):
                continue  # the import skips that row too (an unreadable CMS mark): the next row naming the model adds it
        return None

    def _department_added_earlier(self, ctx, name: str):
        """The check's copy of a department a row before this chunk adds in the import: the first such row that gets as far as its
        department (its model found or added, as _model_added_earlier finds them), with its spelling. Quietly, as for models."""
        for i, earlier in self._earlier(ctx, ("department", name.lower())):
            adding = self._adding(ctx, earlier)
            if adding is None:
                continue
            try:
                with transaction.atomic():
                    self._model(ctx, earlier, RowResult(0, ""), _Later(ctx), adding=adding, before=i)
                    return eq.create_department(_words(earlier["department"]))
            except (RowSkip, ValidationError, PermissionDenied):
                continue
        return None

    def _risk(self, row, result, later) -> str:
        value = row.get("risk") or ""
        try:
            risk = parse.parse_choice(value, RISK_WORDS, what="risk class")
        except parse.Unreadable:
            later.read_as("Risk class", value, "Not read")
            result.warn("Risk class not read: set it on the model")
            return RiskClass.MEDIUM
        if risk is None:
            result.warn("No risk class: the new model is medium; set it on the model")
            return RiskClass.MEDIUM
        later.read_as("Risk class", value, RiskClass(risk).label)
        return risk

    def _interval(self, row, result, later) -> int:
        value = row.get("pm_interval") or ""
        try:
            months = parse.parse_interval_months(value)
        except parse.Unreadable:
            later.read_as("PM interval", value, "Not read")
            result.warn(f"PM interval not read: the new model is every {DEFAULT_INTERVAL} months; set it on the model")
            return DEFAULT_INTERVAL
        if months is None:
            result.warn(f"No PM interval: the new model is every {DEFAULT_INTERVAL} months; set it on the model")
            return DEFAULT_INTERVAL
        later.read_as("PM interval", value, f"{months} month{'' if months == 1 else 's'}")
        return months

    def _mark(self, ctx, row, result, later) -> bool:
        """The CMS mark for a model the row adds. Unreadable skips the row: an imaging model left unmarked could go on AEM."""
        value = row.get("oem_schedule") or ""
        try:
            mark = parse.parse_yes_no(value)
        except parse.Unreadable:
            raise RowSkip("OEM schedule required not read: use yes or no (blank is no)") from None
        if value:
            later.read_as("OEM schedule required", value, "Yes" if mark else "No")
        if mark and not self._may(ctx, eq_perms.OEM_SCHEDULE_LEVEL):
            result.warn("OEM schedule required not set on the new model: needs Equipment Approve")
            return False
        return bool(mark)
