"""
Fleet queries for the Equipment screen and the Overview fleet strip.

Every device lands in exactly one fleet bucket, most urgent state first (matches the mock's `bucketOf`):
retired, out of service, in repair, open recall, PM overdue, PM due within 30 days, compliant.
The bucket is a SQL annotation so the strip counts and the `?bucket=` filter always agree.
"""
from dataclasses import dataclass
from datetime import date, timedelta

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
RESERVED_TAGS = {"new"}  # /equipment/new/ adds a device, so no device can be tagged "new"

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


def _check_numbers(*, acquisition_cost=None, condition=None) -> None:
    errors = {}
    if acquisition_cost is not None and acquisition_cost < 0:
        errors["acquisition_cost"] = "The acquisition cost cannot be negative."
    if condition is not None and not 1 <= condition <= 5:
        errors["condition"] = "Condition is 1 (poor) to 5 (excellent)."
    if errors:
        raise ValidationError(errors)


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
    fields = {"manufacturer": _clean_text(manufacturer, 120), "model": _clean_text(model, 120), "description": _clean_text(description, 200),
              "category": _clean_text(category, 80)}
    errors = {k: "This is required." for k, v in fields.items() if not v}
    if risk_class not in RiskClass.values:
        errors["risk_class"] = "Choose a risk class."
    if not 1 <= int(oem_pm_interval_months or 0) <= 120:
        errors["oem_pm_interval_months"] = "The PM interval is 1 to 120 months."
    if not 1 <= int(expected_life_years or 0) <= 50:
        errors["expected_life_years"] = "Expected life is 1 to 50 years."
    if list_cost is not None and list_cost < 0:
        errors["list_cost"] = "The list cost cannot be negative."
    if errors:
        raise ValidationError(errors)
    if DeviceModel.objects.filter(manufacturer__iexact=fields["manufacturer"], model__iexact=fields["model"]).exists():
        raise ValidationError({"model": f"{fields['manufacturer']} {fields['model']} is already in the catalog; choose it from the list."})
    return DeviceModel.objects.create(risk_class=risk_class, oem_pm_interval_months=int(oem_pm_interval_months),
                                      expected_life_years=int(expected_life_years), list_cost=list_cost or 0, **fields)


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
    merged = {f: fields.get(f, getattr(asset, f)) for f in ("installed_on", "warranty_end")}
    _check_dates(**merged, last_pm_on=asset.last_pm_on, today=today)
    _check_numbers(acquisition_cost=fields.get("acquisition_cost"), condition=fields.get("condition"))
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
    return asset


@transaction.atomic
def set_status(asset: Asset, to_status: str, *, by=None, note: str = "", today: date | None = None) -> Asset:
    """Move a device along STATUS_CHANGES. Retiring cancels its open PM work orders that can be cancelled and refuses while any
    other work is open (finish or cancel it first); the device leaves the PM schedule. Reinstating puts it back with a PM due today.
    `note` is kept in the device's history."""
    from apps.workorders.models import OPEN_STATUSES, WoStatus, WoType
    from apps.workorders.services import change_status

    today = today or date.today()
    if to_status not in STATUS_CHANGES.get(asset.status, set()):
        raise ValidationError(f"{asset.tag} cannot go from {asset.get_status_display().lower()} to {AssetStatus(to_status).label.lower()}.")
    if to_status == AssetStatus.RETIRED:
        open_wos = list(asset.work_orders.filter(status__in=OPEN_STATUSES).order_by("number"))
        blocking = [w for w in open_wos if not (w.type == WoType.PM and w.status in (WoStatus.OPEN, WoStatus.AWAITING_PARTS))]
        if blocking:
            numbers = ", ".join(w.number for w in blocking)
            raise ValidationError(f"{asset.tag} has open work: {numbers}. Finish or cancel it before retiring the device.")
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

    order = [AssetStatus.OUT_OF_SERVICE, AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN, AssetStatus.MISSING, AssetStatus.RETIRED]
    out = []
    for to in order:
        if to not in STATUS_CHANGES.get(asset.status, set()) or not perms.can_set_status(user, asset.status, to):
            continue
        label, style = status_action_label(asset.status, to)
        confirm = ""
        if to == AssetStatus.RETIRED:
            confirm = f"Retire {asset.tag}? Its open PM work orders are cancelled and it leaves the PM schedule."
        elif to == AssetStatus.MISSING:
            confirm = f"Mark {asset.tag} missing? It counts as overdue for PM until it is found."
        out.append({"to": to, "label": label, "style": style, "confirm": confirm})
    return out
