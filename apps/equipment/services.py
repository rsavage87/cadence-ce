"""
Fleet queries for the Equipment screen and the Overview fleet strip.

Every device lands in exactly one fleet bucket, most urgent state first (matches the mock's `bucketOf`):
retired, out of service, in repair, open recall, PM overdue, PM due within 30 days, compliant.
The bucket is a SQL annotation so the strip counts and the `?bucket=` filter always agree.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Case, Count, Exists, F, OuterRef, Q, Value, When

from apps.recalls.models import AlertMatch

from .models import TAG_VALIDATOR, Asset, AssetStatus, Department, DeviceModel, RiskClass, SupportType

PM_DUE_SOON_DAYS = 30


class FleetBucket(models.TextChoices):
    COMPLIANT = "compliant", "Compliant"
    PM_DUE = "pm_due", "PM due within 30 days"
    PM_OVERDUE = "pm_overdue", "PM overdue"
    OPEN_RECALL = "open_recall", "Open recall, action needed"
    IN_REPAIR = "in_repair", "In repair"
    OUT_OF_SERVICE = "out_of_service", "Out of service"
    RETIRED = "retired", "Retired"


class SupportFilter(models.TextChoices):
    IN_HOUSE = "in_house", "In-house"
    UNDER_CONTRACT = "under_contract", "Under contract"
    CONTRACT_EXPIRED = "contract_expired", "Contract expired"
    OEM_CONTRACT = "oem_contract", "OEM contract"
    THIRD_PARTY = "third_party", "Third-party"


RISK_RANK = Case(*[When(device_model__risk_class=r, then=Value(i)) for i, r in enumerate(RiskClass.values)], default=Value(9))


def with_bucket(qs, today: date):
    recall = Exists(AlertMatch.objects.filter(device_model_id=OuterRef("device_model_id"), status=AlertMatch.Status.NEEDS_ACTION))
    return qs.annotate(bucket=Case(
        When(status=AssetStatus.RETIRED, then=Value(FleetBucket.RETIRED)),
        When(status=AssetStatus.OUT_OF_SERVICE, then=Value(FleetBucket.OUT_OF_SERVICE)),
        When(status=AssetStatus.IN_REPAIR, then=Value(FleetBucket.IN_REPAIR)),
        When(recall, then=Value(FleetBucket.OPEN_RECALL)),
        When(next_pm_on__lt=today, then=Value(FleetBucket.PM_OVERDUE)),
        When(next_pm_on__lte=today + timedelta(days=PM_DUE_SOON_DAYS), then=Value(FleetBucket.PM_DUE)),
        default=Value(FleetBucket.COMPLIANT),
        output_field=models.CharField(),
    ))


def fleet_bucket_counts(today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    counts = {b: 0 for b in FleetBucket.values}
    for row in with_bucket(Asset.objects.all(), today).order_by().values("bucket").annotate(n=Count("id")):
        counts[row["bucket"]] = row["n"]
    return counts


def fleet_summary(today: date | None = None) -> dict:
    today = today or date.today()
    active = Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES)
    return {"total": Asset.objects.count(), "active": active.count(), "under_contract": active.filter(contract__end_on__gte=today).count()}


ACTIVE_STATUS_FILTER = "active"  # every status but retired: what the fleet counts on Overview and Settings mean by "devices"


@dataclass
class AssetFilters:
    q: str = ""
    category: str = ""
    status: str = ""
    risk: str = ""
    department: str = ""
    support: str = ""
    overdue: bool = False
    bucket: str = ""
    sort: str = "tag"
    descending: bool = False


def _asc(field):
    return F(field).asc(nulls_last=True)


SORTS = {
    "tag": lambda desc: [F("tag").desc() if desc else F("tag").asc()],
    "device": lambda desc: [F("device_model__description").desc() if desc else F("device_model__description").asc(), "tag"],
    "location": lambda desc: [F("department__name").desc() if desc else F("department__name").asc(), "room", "tag"],
    "risk": lambda desc: [RISK_RANK.desc() if desc else RISK_RANK.asc(), "tag"],
    "status": lambda desc: [F("status").desc() if desc else F("status").asc(), "tag"],
    "next_pm": lambda desc: [F("next_pm_on").desc(nulls_last=True) if desc else _asc("next_pm_on"), "tag"],
    # Age ascending means youngest first, so the install date runs the other way.
    "age": lambda desc: [_asc("installed_on") if desc else F("installed_on").desc(nulls_last=True), "tag"],
    "support": lambda desc: [F("support_type").desc() if desc else F("support_type").asc(), _asc("contract__end_on"), "tag"],
    "cost": lambda desc: [F("acquisition_cost").desc() if desc else F("acquisition_cost").asc(), "tag"],
}


def filter_assets(f: AssetFilters, today: date | None = None):
    """The Equipment table: the mock's toolbar filters, annotated with each device's fleet bucket."""
    today = today or date.today()
    qs = with_bucket(Asset.objects.select_related("device_model", "department", "contract"), today)
    if f.q:
        q = f.q.strip()
        qs = qs.filter(Q(tag__icontains=q) | Q(serial__icontains=q) | Q(device_model__model__icontains=q) | Q(device_model__manufacturer__icontains=q)
                       | Q(device_model__description__icontains=q) | Q(device_model__category__icontains=q) | Q(department__name__icontains=q)
                       | Q(contract__reference__icontains=q))
    if f.category:
        qs = qs.filter(device_model__category=f.category)
    if f.status == ACTIVE_STATUS_FILTER:
        qs = qs.filter(status__in=Asset.ACTIVE_STATUSES)
    elif f.status:
        qs = qs.filter(status=f.status)
    if f.risk:
        qs = qs.filter(device_model__risk_class=f.risk)
    if f.department:
        qs = qs.filter(department__name=f.department)
    if f.support == SupportFilter.UNDER_CONTRACT:
        qs = qs.filter(contract__end_on__gte=today)
    elif f.support == SupportFilter.CONTRACT_EXPIRED:
        qs = qs.filter(contract__end_on__lt=today)
    elif f.support in (SupportType.IN_HOUSE, SupportType.OEM_CONTRACT, SupportType.THIRD_PARTY):
        qs = qs.filter(support_type=f.support)
    if f.overdue:
        qs = qs.filter(status__in=Asset.ACTIVE_STATUSES, next_pm_on__lt=today)
    if f.bucket:
        qs = qs.filter(bucket=f.bucket)
    return qs.order_by(*SORTS.get(f.sort, SORTS["tag"])(f.descending))


def search_assets(q: str, limit: int = 6):
    """Device picker for the new work order form: active devices by tag, serial, or model."""
    q = q.strip()
    if len(q) < 2:
        return Asset.objects.none()
    return (Asset.objects.exclude(status=AssetStatus.RETIRED).select_related("device_model", "department")
            .filter(Q(tag__icontains=q) | Q(serial__icontains=q) | Q(device_model__model__icontains=q) | Q(device_model__description__icontains=q))[:limit])


def asset_service_summary(asset, today: date | None = None) -> dict:
    """Work orders opened on this device in the trailing 182 days, with completed cost and its annualized share of acquisition."""
    from apps.workorders.models import WorkOrder, WoType

    today = today or date.today()
    wos = list(WorkOrder.objects.filter(asset=asset, opened_on__gte=today - timedelta(days=182)).select_related("assigned_to")
               .prefetch_related("labor_lines", "part_lines").order_by("-opened_on", "-created_at"))
    cost = sum(w.total_cost() for w in wos if w.completed_on)
    acquisition = float(asset.acquisition_cost)
    return {
        "work_orders": wos,
        "repairs": sum(1 for w in wos if w.type == WoType.REPAIR),
        "cost": cost,
        "annualized_pct": cost * 365 / 182 / acquisition * 100 if acquisition else None,
    }


# --- adding and changing devices (slice 12) ---------------------------------------------------------------------------------
#
# The Equipment screen's Add device, the drawer's Edit and status buttons, and the API go through these; views never set fields
# on Asset, DeviceModel, or Department themselves. Rules raise ValidationError, keyed by field where a form can show it there.
# Who may do what is in apps/equipment/permissions.py.

NEW_DEVICE_STATUSES = (AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE)  # a new device is in use, or waiting for incoming inspection
# /equipment/new/ adds a device, so no device can be tagged "new"; "." and ".." are path segments browsers rewrite, so a device
# tagged that way could never be opened.
RESERVED_TAGS = {"new", ".", ".."}

# Status changes the drawer and API allow, from -> to. "In repair" is reached through work orders, never set by hand; retiring
# and reinstating need more (permissions.RETIRE_LEVEL) because retiring cancels the device's open PM work orders.
STATUS_CHANGES = {
    AssetStatus.IN_SERVICE: {AssetStatus.OUT_OF_SERVICE, AssetStatus.ON_LOAN, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.ON_LOAN: {AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.OUT_OF_SERVICE: {AssetStatus.IN_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.IN_REPAIR: {AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.MISSING: {AssetStatus.IN_SERVICE, AssetStatus.RETIRED},
    AssetStatus.RETIRED: {AssetStatus.IN_SERVICE},
}

# The drawer's button for each change: (label, style). "Return to service" is the mock's; the others say what happened.
STATUS_ACTION_LABELS = {
    (AssetStatus.OUT_OF_SERVICE, None): ("Tag out of service", "danger"),
    (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN): ("Back from loan", ""),
    (AssetStatus.IN_SERVICE, AssetStatus.MISSING): ("Found", ""),
    (AssetStatus.IN_SERVICE, AssetStatus.RETIRED): ("Reinstate", ""),
    (AssetStatus.IN_SERVICE, None): ("Return to service", ""),
    (AssetStatus.ON_LOAN, None): ("Lend out", ""),
    (AssetStatus.MISSING, None): ("Mark missing", ""),
    (AssetStatus.RETIRED, None): ("Retire", "danger"),
}


def status_action_label(from_status: str, to_status: str) -> tuple[str, str]:
    return STATUS_ACTION_LABELS.get((to_status, from_status)) or STATUS_ACTION_LABELS[(to_status, None)]


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _check_tenant(obj, what: str):
    from apps.tenants.context import get_current_tenant

    tenant = get_current_tenant()
    if obj is None or tenant is None or obj.tenant_id != tenant.id:
        raise ValidationError({what: f"Choose a {what.replace('_', ' ')} from this facility."})


def _check_dates(*, installed_on=None, warranty_end=None, last_pm_on=None, today: date) -> None:
    errors = {}
    if installed_on and installed_on > today:
        errors["installed_on"] = "The install date cannot be in the future."
    if installed_on and warranty_end and warranty_end < installed_on:
        errors["warranty_end"] = "The warranty cannot end before the device was installed."
    if last_pm_on and last_pm_on > today:
        errors["last_pm_on"] = "The last PM cannot be in the future."
    if installed_on and last_pm_on and last_pm_on < installed_on:
        errors["last_pm_on"] = "The last PM cannot be before the device was installed."
    if errors:
        raise ValidationError(errors)


_UNSET = object()


def _check_numbers(*, acquisition_cost=_UNSET, condition=_UNSET) -> None:
    """Only the values passed are checked; a value passed as None is missing (both columns are required)."""
    errors = {}
    if acquisition_cost is None:
        errors["acquisition_cost"] = "Enter the acquisition cost (0 if unknown)."
    elif acquisition_cost is not _UNSET and acquisition_cost < 0:
        errors["acquisition_cost"] = "The acquisition cost cannot be negative."
    if condition is None:
        errors["condition"] = "Choose a condition from 1 (poor) to 5 (excellent)."
    elif condition is not _UNSET and not (isinstance(condition, int) and 1 <= condition <= 5):
        errors["condition"] = "Condition is 1 (poor) to 5 (excellent)."
    if errors:
        raise ValidationError(errors)


def _whole(value, field: str, low: int, high: int, message: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: message})
    if not low <= n <= high or str(value).strip() not in (str(n), f"{n}.0"):
        raise ValidationError({field: message})
    return n


NEXT_PM_YEARS = 10  # a next PM further out is a typo or a "never" placeholder, and breaks the projections that add intervals to it


def _check_next_pm(next_pm_on, today: date) -> None:
    from apps.pm.dates import add_months

    if not next_pm_on:
        return
    if next_pm_on.year < 2000:
        raise ValidationError({"next_pm_on": "Enter the next PM date in full (the year looks wrong)."})
    if next_pm_on > add_months(today, NEXT_PM_YEARS * 12):
        raise ValidationError({"next_pm_on": f"The next PM must be within {NEXT_PM_YEARS} years."})


def first_pm_due(device_model, *, installed_on=None, last_pm_on=None, today: date) -> date:
    """When a new device's first PM falls due: one interval after its last PM, or after its install date; a device with no PM on
    record whose interval since install has already passed is due today (it has never had one here)."""
    from apps.pm.dates import add_months

    interval = device_model.pm_interval_months
    if last_pm_on:
        return add_months(last_pm_on, interval)
    if installed_on:
        return max(add_months(installed_on, interval), today)
    return add_months(today, interval)


def create_department(name: str) -> Department:
    """A department by name, case-insensitively; created when the facility has none by that name."""
    name = _clean_text(name, 80)
    if not name:
        raise ValidationError({"department": "Enter the department's name."})
    existing = Department.objects.filter(name__iexact=name).first()
    return existing or Department.objects.create(name=name)


@transaction.atomic
def create_device_model(*, manufacturer, model, description, category, risk_class, oem_pm_interval_months=12, expected_life_years=8,
                        list_cost=0, by=None) -> DeviceModel:
    """Add a model to the facility's catalog. Life support never goes on AEM (DeviceModel.pm_interval_months), so no AEM here."""
    fields = _model_fields({"manufacturer": manufacturer, "model": model, "description": description, "category": category,
                            "risk_class": risk_class, "oem_pm_interval_months": oem_pm_interval_months, "expected_life_years": expected_life_years,
                            "list_cost": list_cost})
    _check_model_unique(fields["manufacturer"], fields["model"])
    return DeviceModel.objects.create(**fields)


MODEL_FIELDS = ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "aem_interval_months",
                "expected_life_years", "list_cost", "pm_procedure")
_MODEL_TEXT = {"manufacturer": 120, "model": 120, "description": 200, "category": 80}


def _model_fields(raw: dict) -> dict:
    """Clean and check the model fields present in `raw` (the same rules on create and on change). Raises one ValidationError
    with every problem, keyed by field."""
    out, errors = {}, {}
    for name, limit in _MODEL_TEXT.items():
        if name in raw:
            out[name] = _clean_text(raw[name], limit)
            if not out[name]:
                errors[name] = "This is required."
    if "risk_class" in raw:
        if raw["risk_class"] not in RiskClass.values:
            errors["risk_class"] = "Choose a risk class."
        out["risk_class"] = raw["risk_class"]
    for name, high, message in (("oem_pm_interval_months", 120, "The PM interval is 1 to 120 months."),
                                ("expected_life_years", 50, "Expected life is 1 to 50 years."),
                                ("aem_interval_months", 120, "The AEM interval is 1 to 120 months, or none.")):
        if name not in raw:
            continue
        if name == "aem_interval_months" and raw[name] in (None, ""):
            out[name] = None
            continue
        try:
            out[name] = _whole(raw[name], name, 1, high, message)
        except ValidationError as e:
            errors.update(e.message_dict)
    if "list_cost" in raw:
        try:
            cost = Decimal(str(raw["list_cost"] if raw["list_cost"] not in (None, "") else 0))
        except (InvalidOperation, ValueError):
            cost = None
        if cost is None or not cost.is_finite() or cost < 0:
            errors["list_cost"] = "The list cost is a number, 0 or more."
        out["list_cost"] = cost
    if "pm_procedure" in raw:
        if raw["pm_procedure"] is not None:
            try:
                _check_tenant(raw["pm_procedure"], "pm_procedure")
            except ValidationError as e:
                errors.update(e.message_dict)
        out["pm_procedure"] = raw["pm_procedure"]
    if errors:
        raise ValidationError(errors)
    return out


def _check_model_unique(manufacturer: str, model: str, exclude=None) -> None:
    qs = DeviceModel.objects.filter(manufacturer__iexact=manufacturer, model__iexact=model)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    if qs.exists():
        raise ValidationError({"model": f"{manufacturer} {model} is already in the catalog; choose it from the list."})


@transaction.atomic
def update_device_model(device_model: DeviceModel, *, by=None, **fields) -> DeviceModel:
    """Change a catalog model with create_device_model's rules (and an AEM interval, which life-support models ignore). The name
    stays unique in any letter case. A new interval moves no device's next PM by itself: it applies from each device's next PM."""
    unknown = set(fields) - set(MODEL_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    cleaned = _model_fields(fields)
    if "aem_interval_months" in cleaned and cleaned["aem_interval_months"] != device_model.aem_interval_months:
        # Only an approved AEM case sets it (apps.pm.aem); sending back the current value is fine.
        raise ValidationError({"aem_interval_months": "An AEM interval is set by approving an AEM proposal (PM schedule, PM library)."})
    if "manufacturer" in cleaned or "model" in cleaned:
        _check_model_unique(cleaned.get("manufacturer", device_model.manufacturer), cleaned.get("model", device_model.model), exclude=device_model)
    changed = [f for f, v in cleaned.items() if getattr(device_model, f) != v]
    for f in changed:
        setattr(device_model, f, cleaned[f])
    if changed:
        device_model.save()
    if {"risk_class", "oem_pm_interval_months"} & set(changed):
        from apps.pm import aem  # pm imports equipment; imported here to keep the two apps' modules loadable in any order

        aem.model_changed(device_model, changed=changed, by=by)
    return device_model


def rename_department(department: Department, name: str) -> Department:
    """Rename a department; the name stays unique in the facility in any letter case."""
    name = _clean_text(name, 80)
    if not name:
        raise ValidationError({"name": "Enter the department's name."})
    if Department.objects.filter(name__iexact=name).exclude(pk=department.pk).exists():
        raise ValidationError({"name": f"{name} is already a department here."})
    if name != department.name:
        department.name = name
        department.save(update_fields=["name"])
    return department


@transaction.atomic
def create_asset(*, tag, device_model, department, serial="", room="", installed_on=None, acquisition_cost=None, warranty_end=None, condition=3,
                 last_pm_on=None, next_pm_on=None, notes="", status=AssetStatus.IN_SERVICE, by=None, today: date | None = None) -> Asset:
    """Add a device. Its first PM is `next_pm_on` when given, else first_pm_due(); its acquisition cost defaults to the model's list
    cost. Tags are unique in the facility in any letter case and can never change afterwards (they are on the sticker and in URLs)."""
    today = today or date.today()
    tag = (tag or "").strip()
    if not tag:
        raise ValidationError({"tag": "Enter the asset tag from the CE sticker."})
    try:
        TAG_VALIDATOR(tag)
    except ValidationError as e:
        raise ValidationError({"tag": e.messages[0]})
    if len(tag) > 40:
        raise ValidationError({"tag": "Asset tags are at most 40 characters."})
    if tag.lower() in RESERVED_TAGS:
        raise ValidationError({"tag": f'"{tag}" cannot be used as an asset tag.'})
    _check_tenant(device_model, "device_model")
    _check_tenant(department, "department")
    if status not in NEW_DEVICE_STATUSES:
        raise ValidationError({"status": "A new device is in service or out of service (waiting for incoming inspection)."})
    if acquisition_cost is None:
        acquisition_cost = device_model.list_cost
    _check_dates(installed_on=installed_on, warranty_end=warranty_end, last_pm_on=last_pm_on, today=today)
    _check_numbers(acquisition_cost=acquisition_cost, condition=condition)
    _check_next_pm(next_pm_on, today)
    if Asset.objects.filter(tag__iexact=tag).exists():
        raise ValidationError({"tag": f"{tag} is already on another device."})
    asset = Asset(tag=tag, device_model=device_model, department=department, serial=_clean_text(serial, 80), room=_clean_text(room, 40),
                  installed_on=installed_on, acquisition_cost=acquisition_cost, warranty_end=warranty_end, condition=condition,
                  last_pm_on=last_pm_on, status=status, notes=(notes or "").strip(),
                  next_pm_on=next_pm_on or first_pm_due(device_model, installed_on=installed_on, last_pm_on=last_pm_on, today=today))
    asset._change_reason = "Added"
    asset.save()
    return asset


EDITABLE_FIELDS = ("serial", "device_model", "department", "room", "installed_on", "acquisition_cost", "warranty_end", "condition", "next_pm_on", "notes")


@transaction.atomic
def update_asset(asset: Asset, *, by=None, today: date | None = None, **fields) -> Asset:
    """Change a device's details. Not its tag (on the sticker and in URLs), status (set_status), or contract (the contracts screen).
    A new model does not move the next PM by itself; change next_pm_on alongside it when the interval differs."""
    today = today or date.today()
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    if "device_model" in fields:
        _check_tenant(fields["device_model"], "device_model")
    if "department" in fields:
        _check_tenant(fields["department"], "department")
    if "next_pm_on" in fields and fields["next_pm_on"] is None and asset.status != AssetStatus.RETIRED:
        raise ValidationError({"next_pm_on": "A device in use needs a next PM date."})
    _check_next_pm(fields.get("next_pm_on"), today)
    # The install and warranty dates only when one of them changes: a stored pair that breaks the rule (imported data) must not
    # block an edit of the room or the notes.
    if any(f in fields and fields[f] != getattr(asset, f) for f in ("installed_on", "warranty_end")):
        _check_dates(**{f: fields.get(f, getattr(asset, f)) for f in ("installed_on", "warranty_end")}, today=today)
    # The last PM against the install date only when the install date changes, and on the field the form has: a device added
    # before this rule (import, the demo) may have an older PM on record, and must stay editable.
    new_install = fields.get("installed_on")
    if new_install and new_install != asset.installed_on and asset.last_pm_on and asset.last_pm_on < new_install:
        raise ValidationError({"installed_on": f"The install date cannot be after the last PM on record ({asset.last_pm_on:%b %-d, %Y})."})
    _check_numbers(**{k: fields[k] for k in ("acquisition_cost", "condition") if k in fields})
    for name in ("serial", "room"):
        if name in fields:
            fields[name] = _clean_text(fields[name], 80 if name == "serial" else 40)
    if "notes" in fields:
        fields["notes"] = (fields["notes"] or "").strip()
    changed = [f for f, v in fields.items() if getattr(asset, f) != v]
    for f in changed:
        setattr(asset, f, fields[f])
    if changed:
        asset._change_reason = "Edited"
        asset.save()
    if "next_pm_on" in changed and asset.next_pm_on:
        _move_open_pm(asset, by=by)
    return asset


def _move_open_pm(asset: Asset, by=None) -> None:
    """A rescheduled PM moves the device's open PM work order with it, so the PM is not counted as missed on the old date."""
    from apps.workorders.models import OPEN_STATUSES, WorkOrderStatusHistory, WoType

    for wo in asset.work_orders.filter(type=WoType.PM, status__in=OPEN_STATUSES).exclude(due_on=asset.next_pm_on):
        old = wo.due_on
        wo.due_on = asset.next_pm_on
        wo.save(update_fields=["due_on", "updated_at"])
        WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status=wo.status, to_status=wo.status, changed_by=by,
                                              note=f"Due date moved from {old:%b %-d, %Y} to {wo.due_on:%b %-d, %Y} with the device's next PM")


@transaction.atomic
def set_status(asset: Asset, to_status: str, *, by=None, note: str = "", today: date | None = None) -> Asset:
    """Move a device along STATUS_CHANGES. Retiring cancels its open PM work orders that can be cancelled and refuses while any
    other work is open (finish or cancel it first); the device leaves the PM schedule. Reinstating puts it back with a PM due today.
    `note` is kept in the device's history."""
    from apps.workorders.models import OPEN_STATUSES, WoStatus, WoType
    from apps.workorders.services import change_status

    today = today or date.today()
    if to_status not in AssetStatus.values:
        raise ValidationError("Choose a device status.")
    if to_status not in STATUS_CHANGES.get(asset.status, set()):
        raise ValidationError(f"{asset.tag} cannot go from {asset.get_status_display().lower()} to {AssetStatus(to_status).label.lower()}.")
    if to_status == AssetStatus.RETIRED:
        open_wos = list(asset.work_orders.filter(status__in=OPEN_STATUSES).order_by("number"))
        blocking = [w for w in open_wos if not (w.type == WoType.PM and w.status in (WoStatus.OPEN, WoStatus.AWAITING_PARTS))]
        if blocking:
            numbers = ", ".join(w.number for w in blocking)
            raise ValidationError(f"{asset.tag} has open work: {numbers}. Complete it, or cancel it (an in-progress work order goes "
                                  f"back to open first), before retiring the device.")
        for w in open_wos:
            change_status(w, WoStatus.CANCELLED, by=by, note="Device retired")
        asset.next_pm_on = None
    elif asset.status == AssetStatus.RETIRED:
        asset.next_pm_on = today  # back in use: inspect it before anyone relies on it
    asset.status = to_status
    asset._change_reason = (note or "").strip()[:100] or f"Status: {AssetStatus(to_status).label}"
    asset.save()
    return asset


def status_actions(asset: Asset, user) -> list[dict]:
    """The status buttons the drawer shows this user for this device: [{to, label, style, confirm}], most likely first."""
    from . import permissions as perms

    level = user.level_for(perms.MODULE)  # once, not once per button
    # A device in use is most likely being tagged out; one that is not is most likely coming back (the mock's single button).
    in_use = asset.status in (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN)
    order = ([AssetStatus.OUT_OF_SERVICE, AssetStatus.IN_SERVICE] if in_use else [AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE]) + [
        AssetStatus.ON_LOAN, AssetStatus.MISSING, AssetStatus.RETIRED]
    out = []
    for to in order:
        if to not in STATUS_CHANGES.get(asset.status, set()) or level < perms.status_level(asset.status, to):
            continue
        label, style = status_action_label(asset.status, to)
        confirm = ""
        if to == AssetStatus.RETIRED:
            confirm = f"Retire {asset.tag}? Its open PM work orders are cancelled and it leaves the PM schedule."
        elif to == AssetStatus.MISSING:
            confirm = f"Mark {asset.tag} missing? It counts as overdue for PM until it is found."
        out.append({"to": to, "label": label, "style": style, "confirm": confirm})
    return out
