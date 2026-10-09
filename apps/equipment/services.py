"""
Fleet queries for the Equipment screen and the Overview fleet strip.

Every device lands in exactly one fleet bucket, most urgent state first (matches the mock's `bucketOf`):
retired, out of service, in repair, open recall, PM overdue, PM due within 30 days, compliant.
The bucket is a SQL annotation so the strip counts and the `?bucket=` filter always agree.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import Case, Count, Exists, F, OuterRef, Q, Value, When
from django.utils import timezone

from apps.recalls.models import AlertMatch

from . import permissions
from .models import (
    OWNER_REFERENCE_VALIDATOR,
    TAG_VALIDATOR,
    TEMPORARY,
    AddedAs,
    Asset,
    AssetStatus,
    Department,
    DeviceModel,
    Ownership,
    ReturnCleaning,
    ReturnData,
    RiskClass,
    SupportType,
    UseBeforeInspection,
)

PM_DUE_SOON_DAYS = 30


class FleetBucket(models.TextChoices):
    COMPLIANT = "compliant", "Compliant"
    PM_DUE = "pm_due", "PM due within 30 days"
    PM_OVERDUE = "pm_overdue", "PM overdue"
    OPEN_RECALL = "open_recall", "Open recall, action needed"
    IN_REPAIR = "in_repair", "In repair"
    OUT_OF_SERVICE = "out_of_service", "Out of service"
    TEMPORARY = "temporary", "Temporary on site"  # slice 29: a rental, vendor loaner, or demo unit on site (its owner maintains it)
    RETIRED = "retired", "Retired"
    RETURNED = "returned", "Returned to owner"  # slice 29: a temporary device that went back (status retired)


# Slice 29, the counting rule: figures about CE's maintenance program and the facility's own fleet (PM compliance, AEM evidence, COSR,
# replacement planning, the support rows, uptime, MTBF, the PM library's counts, the Overview's active devices) count our devices only;
# figures about CE's work and safety (work orders, MTTR, technicians, spend, recalls, incidents) count every device. A rental, vendor
# loaner, or demo unit is maintained by its owner.
OWNED = Q(ownership=Ownership.OWNED)
WORK_ORDER_OWNED = Q(asset__ownership=Ownership.OWNED)


def owner_maintains(asset) -> bool:
    """A temporary device (slice 29): no next PM and no PM work orders; its owner's PM date (owner_pm_due_on) is what Cadence knows."""
    return getattr(asset, "ownership", Ownership.OWNED) != Ownership.OWNED


def service_vendor(asset) -> str:
    """Who a work order's vendor service names for `asset`: a temporary device's owner (slice 29), else its contract's vendor, else
    "<manufacturer> field service" (workorders.scoping matches a vendor account's company against it). Was web.forms.vendor_name_for;
    the screens and the API share it."""
    if owner_maintains(asset) and asset.owner:
        return asset.owner
    return asset.contract.vendor if asset.contract_id else f"{asset.device_model.manufacturer} field service"


class SupportFilter(models.TextChoices):
    IN_HOUSE = "in_house", "In-house"
    UNDER_CONTRACT = "under_contract", "Under contract"
    CONTRACT_EXPIRED = "contract_expired", "Contract expired"
    OEM_CONTRACT = "oem_contract", "OEM contract"
    THIRD_PARTY = "third_party", "Third-party"


RISK_RANK = Case(*[When(device_model__risk_class=r, then=Value(i)) for i, r in enumerate(RiskClass.values)], default=Value(9))


def with_bucket(qs, today: date):
    recall = Exists(AlertMatch.objects.filter(device_model_id=OuterRef("device_model_id"), status=AlertMatch.Status.NEEDS_ACTION))
    temporary = ~OWNED
    return qs.annotate(bucket=Case(
        When(Q(status=AssetStatus.RETIRED) & temporary, then=Value(FleetBucket.RETURNED)),
        When(status=AssetStatus.RETIRED, then=Value(FleetBucket.RETIRED)),
        When(status=AssetStatus.OUT_OF_SERVICE, then=Value(FleetBucket.OUT_OF_SERVICE)),
        When(status=AssetStatus.IN_REPAIR, then=Value(FleetBucket.IN_REPAIR)),
        When(recall, then=Value(FleetBucket.OPEN_RECALL)),
        When(temporary, then=Value(FleetBucket.TEMPORARY)),  # slice 29: never PM due or overdue (its owner maintains it)
        When(next_pm_on__lt=today, then=Value(FleetBucket.PM_OVERDUE)),
        When(next_pm_on__lte=today + timedelta(days=PM_DUE_SOON_DAYS), then=Value(FleetBucket.PM_DUE)),
        default=Value(FleetBucket.COMPLIANT),
        output_field=models.CharField(),
    ))


def fleet_bucket_counts(today: date | None = None) -> dict[str, int]:
    today = today or timezone.localdate()
    counts = {b: 0 for b in FleetBucket.values}
    for row in with_bucket(Asset.objects.all(), today).order_by().values("bucket").annotate(n=Count("id")):
        counts[row["bucket"]] = row["n"]
    return counts


def fleet_summary(today: date | None = None, qs=None) -> dict:
    """The Equipment page head's counts, over `qs` (default the whole fleet; a scoped user's devices, apps.workorders.scoping)."""
    today = today or timezone.localdate()
    devices = Asset.objects.all() if qs is None else qs
    active = devices.filter(status__in=Asset.ACTIVE_STATUSES)
    return {"total": devices.count(), "active": active.count(), "under_contract": active.filter(contract__end_on__gte=today).count()}


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


def filter_assets(f: AssetFilters, today: date | None = None, qs=None):
    """The Equipment table: the mock's toolbar filters, annotated with each device's fleet bucket. Over `qs` when given (a scoped
    user's devices, apps.workorders.scoping), else the whole fleet."""
    today = today or timezone.localdate()
    qs = with_bucket((Asset.objects.all() if qs is None else qs).select_related("device_model", "department", "contract"), today)
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


def search_assets(q: str, limit: int = 6, qs=None):
    """Device picker for the new work order form: active devices by tag, serial, or model. Among `qs` when given (a scoped user's
    devices, apps.workorders.scoping), else the whole fleet."""
    q = q.strip()
    if len(q) < 2:
        return Asset.objects.none()
    return ((Asset.objects.all() if qs is None else qs).exclude(status=AssetStatus.RETIRED).select_related("device_model", "department")
            .filter(Q(tag__icontains=q) | Q(serial__icontains=q) | Q(device_model__model__icontains=q) | Q(device_model__description__icontains=q))[:limit])


def asset_service_summary(asset, today: date | None = None, work_orders=None) -> dict:
    """Work orders opened on this device in the trailing 182 days, with completed cost and its annualized share of acquisition.
    Among `work_orders` when given (a scoped user's, apps.workorders.scoping), so every figure counts only those; else all of them."""
    from apps.workorders.models import WorkOrder, WoType

    today = today or timezone.localdate()
    base = WorkOrder.objects.all() if work_orders is None else work_orders
    wos = list(base.filter(asset=asset, opened_on__gte=today - timedelta(days=182)).select_related("assigned_to")
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

INCOMING_WAITING = "waiting"  # create_asset's incoming_inspection (slice 26)
AWAITING_LABEL = "Awaiting inspection"
HELD_LABEL = "Held for incident"  # slice 28: a device held as evidence (Asset.incident_hold); it wins over AWAITING_LABEL
RETURNED_LABEL = "Returned to owner"  # slice 29: a temporary device that went back (status retired, ownership not ours)
HELD_REASON = "Held for an incident investigation"  # the device's history (Equipment View): never the incident's number
RELEASED_REASON = "Hold released"
NEW_DEVICE_STATUSES = (AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE)  # a new device is in use, or waiting for incoming inspection
# /equipment/new/ adds a device, so no device can be tagged "new"; "." and ".." are path segments browsers rewrite, so a device
# tagged that way could never be opened.
RESERVED_TAGS = {"new", ".", ".."}

# Status changes the drawer and API allow, from -> to. "In repair" is reached through work orders, never set by hand; retiring
# and reinstating need more (permissions.RETIRE_LEVEL) because retiring cancels the device's open PM work orders.
# Slice 26: a device waiting for its incoming inspection (Asset.awaiting_inspection) never goes in service or on loan by hand (HOLD:
# only its passed inspection or use_before_inspection puts it in use), so Found and Reinstate bring it back out of service instead
# (AWAITING_ONLY: the drawer offers those two moves only for such a device; set_status takes them for any).
STATUS_CHANGES = {
    AssetStatus.IN_SERVICE: {AssetStatus.OUT_OF_SERVICE, AssetStatus.ON_LOAN, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.ON_LOAN: {AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.OUT_OF_SERVICE: {AssetStatus.IN_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.IN_REPAIR: {AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED},
    AssetStatus.MISSING: {AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE, AssetStatus.RETIRED},
    AssetStatus.RETIRED: {AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE},
}
HOLD = (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN)  # what a device waiting for its incoming inspection is never moved to by hand
AWAITING_ONLY = {(AssetStatus.MISSING, AssetStatus.OUT_OF_SERVICE), (AssetStatus.RETIRED, AssetStatus.OUT_OF_SERVICE)}
USE_BEFORE_FROM = (AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING)  # where use_before_inspection takes a waiting device from

# The drawer's button for each change: (label, style). "Return to service" is the mock's; the others say what happened.
STATUS_ACTION_LABELS = {
    (AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING): ("Found", ""),  # slice 26: a device waiting for its incoming inspection
    (AssetStatus.OUT_OF_SERVICE, AssetStatus.RETIRED): ("Reinstate", ""),  # slice 26: likewise
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


def _check_last_pm(last_pm_on, *, installed_on, today: date) -> None:
    """The last PM's own rules (_check_dates's: not in the future, not before the install date), whatever else is wrong with the
    install date on file."""
    try:
        _check_dates(installed_on=installed_on, last_pm_on=last_pm_on, today=today)
    except ValidationError as e:
        if "last_pm_on" in e.message_dict:
            raise ValidationError({"last_pm_on": e.message_dict["last_pm_on"]}) from None


def _save_on(obj, day: date | None) -> None:
    """Save `obj`; with `day`, the history row it writes is dated that day (noon, in the facility's time zone) instead of now: what
    an import from the previous system says happened on an earlier day. The date goes with this one save (simple_history would
    reuse it for every later save of the same object)."""
    if day is not None:
        obj._history_date = timezone.make_aware(datetime.combine(day, time(12)))
    try:
        obj.save()
    finally:
        obj.__dict__.pop("_history_date", None)


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
    except (TypeError, ValueError, OverflowError):  # OverflowError: a JSON number too large becomes infinity
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
                        list_cost=0, oem_schedule_required=False, by=None) -> DeviceModel:
    """Add a model to the facility's catalog. No AEM here: an AEM interval is approved on the model's AEM tab (apps.pm.aem), and
    life support and a model marked oem_schedule_required never go on it (DeviceModel.pm_interval_months). Marking a new model
    needs Equipment Approve when a user adds it (_check_oem_schedule_level)."""
    fields = _model_fields({"manufacturer": manufacturer, "model": model, "description": description, "category": category,
                            "risk_class": risk_class, "oem_pm_interval_months": oem_pm_interval_months, "expected_life_years": expected_life_years,
                            "list_cost": list_cost, "oem_schedule_required": oem_schedule_required})
    if fields["oem_schedule_required"]:
        _check_oem_schedule_level(by)
    _check_model_unique(fields["manufacturer"], fields["model"])
    return DeviceModel.objects.create(**fields)


MODEL_FIELDS = ("manufacturer", "model", "description", "category", "risk_class", "oem_pm_interval_months", "aem_interval_months",
                "expected_life_years", "list_cost", "pm_procedure", "oem_schedule_required")
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
    if "oem_schedule_required" in raw:
        if not isinstance(raw["oem_schedule_required"], bool):
            errors["oem_schedule_required"] = "Say whether the manufacturer's schedule is required: yes or no."
        out["oem_schedule_required"] = raw["oem_schedule_required"]
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


# CMS (S&C 14-07) keeps imaging, radiologic, and medical laser equipment on the manufacturer's schedule: whether a model is such
# equipment decides compliance, as its risk class does, so a user needs Equipment Approve to set or clear the mark.
OEM_SCHEDULE_PERMISSION = ("Marking a model as keeping the manufacturer's schedule (CMS: imaging, radiologic, medical laser), or clearing "
                           "the mark, needs Equipment Approve.")
OEM_SCHEDULE_MARKED = "Manufacturer's schedule required (CMS)"
OEM_SCHEDULE_CLEARED = "Manufacturer's schedule (CMS) no longer required"


def _check_oem_schedule_level(by) -> None:
    """The door every change of the mark goes through: Add model, Edit details, and the API ask first, and anything else a user
    reaches lands here. A change with no user (the importer, the demo seed, a shell) is the operator's own."""
    if by is not None and not permissions.can_set_oem_schedule(by):
        raise PermissionDenied(OEM_SCHEDULE_PERMISSION)


def _check_model_unique(manufacturer: str, model: str, exclude=None) -> None:
    qs = DeviceModel.objects.filter(manufacturer__iexact=manufacturer, model__iexact=model)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    if qs.exists():
        raise ValidationError({"model": f"{manufacturer} {model} is already in the catalog; choose it from the list."})


@transaction.atomic
def update_device_model(device_model: DeviceModel, *, by=None, **fields) -> DeviceModel:
    """Change a catalog model with create_device_model's rules. The name stays unique in any letter case. The AEM interval is not
    changed here (apps.pm.aem: sending back the current value is fine), nor an OEM interval equal to the AEM interval in force (that
    would end a committee decision: End AEM first). A new interval moves no device's next PM by itself: it applies from each
    device's next PM.
    A scored model's risk class follows its score (set_risk_score), so a new class that disagrees with the score's band is refused;
    an unscored model's class can change here (the screen and the API ask for Equipment Approve for that).
    Setting or clearing oem_schedule_required needs Equipment Approve from `by` (PermissionDenied otherwise; sending back the
    current value is fine). Marking a model on AEM ends its AEM and brings its devices' next PMs in (apps.pm.aem.model_changed, as
    for life support); clearing the mark changes no interval: AEM is proposed and approved again."""
    unknown = set(fields) - set(MODEL_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    cleaned = _model_fields(fields)
    if "aem_interval_months" in cleaned and cleaned["aem_interval_months"] != device_model.aem_interval_months:
        # Only an approved AEM case sets it (apps.pm.aem); sending back the current value is fine.
        raise ValidationError({"aem_interval_months": "An AEM interval is set by approving an AEM proposal (PM schedule, PM library)."})
    cleaned.pop("aem_interval_months", None)  # never written here, so a value sent back from a stale read cannot undo an approval
    mark = cleaned.get("oem_schedule_required")
    if mark is not None and mark != device_model.oem_schedule_required:
        _check_oem_schedule_level(by)
    oem = cleaned.get("oem_pm_interval_months")
    if (oem is not None and oem != device_model.oem_pm_interval_months and device_model.aem_interval_months == oem
            and not device_model.aem_excluded):
        # It would leave the AEM interval nothing to change, which ends a committee decision: that is End AEM's (PM Approve).
        raise ValidationError({"oem_pm_interval_months": f"This model is on an AEM interval of {oem} months. End the AEM on the model's "
                                                         "AEM tab first, then change the OEM interval."})
    score = device_model.risk_score
    if "risk_class" in cleaned and cleaned["risk_class"] != device_model.risk_class and score is not None and cleaned["risk_class"] != risk_band(score):
        # Sending back the current class is fine, as is moving a stray class onto the score's band.
        raise ValidationError({"risk_class": f"This model's risk class follows its risk score ({score}, {RiskClass(risk_band(score)).label}). "
                                             "Change the risk score to change the class."})
    if "manufacturer" in cleaned or "model" in cleaned:
        _check_model_unique(cleaned.get("manufacturer", device_model.manufacturer), cleaned.get("model", device_model.model), exclude=device_model)
    reason = ""
    if mark is not None and mark != device_model.oem_schedule_required:
        reason = OEM_SCHEDULE_MARKED if mark else OEM_SCHEDULE_CLEARED  # the history says what the change was about
    _save_model(device_model, cleaned, by=by, reason=reason)
    return device_model


def _save_model(device_model: DeviceModel, values: dict, *, by=None, reason: str = "") -> list[str]:
    """Set the values that differ and save once; then tell apps.pm.aem when the risk class, the OEM interval, or the CMS mark
    changed (a model that became life support or was marked oem_schedule_required leaves AEM). The one path every model change
    takes. Returns the fields that changed."""
    # Onto the row as it is now, locked: the caller's copy may predate another change (an AEM approval sets aem_interval_months),
    # and saving that copy whole would write the old value back. The caller's copy is refreshed afterwards.
    fresh = DeviceModel.objects.select_for_update().get(pk=device_model.pk)
    changed = [f for f, v in values.items() if getattr(fresh, f) != v]
    previous = {f: getattr(fresh, f) for f in changed}
    for f in changed:
        setattr(fresh, f, values[f])
    if changed:
        # The history entry's reason and user: given here, or set on the caller's copy (apps.pm.procedures does).
        reason = reason or device_model.__dict__.get("_change_reason", "")
        if reason:
            fresh._change_reason = reason
        user = by if by is not None else device_model.__dict__.get("_history_user")
        if user is not None:
            fresh._history_user = user
        fresh.save()
    if {"risk_class", "oem_pm_interval_months", "oem_schedule_required"} & set(changed):
        from apps.pm import aem  # pm imports equipment; imported here to keep the two apps' modules loadable in any order

        effect = aem.model_changed(fresh, changed=changed, by=by, previous=previous)
    else:
        effect = None
    device_model.refresh_from_db()
    # What the AEM program did about it, for the screen to say (a model scored into life support or marked for the manufacturer's
    # schedule leaves AEM, its PMs come in).
    device_model.aem_effect = effect
    return changed


# --- risk scoring (slice 14) ---------------------------------------------------------------------------------------------
#
# The Settings rubric (apps.facility.services.RISK_RUBRIC): four parts, each a whole number in its range, added up. The total's
# band is the model's risk class; RISK_SCORE_BANDS is apps.facility.services.RISK_BANDS in numbers (a test keeps the two in
# step). Assigned at intake, reviewed yearly: risk_reviewed_on is when the score was last set or confirmed. Who may score:
# apps.equipment.permissions.can_set_risk (Approve).

# (keyword, model field, label, lowest, highest)
RISK_PARTS = (
    ("function", "risk_function", "Clinical function", 1, 10),
    ("physical", "risk_physical", "Physical risk of failure", 1, 5),
    ("maintenance", "risk_maintenance", "Maintenance requirement", 1, 5),
    ("incidents", "risk_incidents", "Incident history", 0, 2),
)
# (lowest score in the band, class), highest band first: 16 and above, 12 to 15, 9 to 11, 8 and below.
RISK_SCORE_BANDS = ((16, RiskClass.LIFE_SUPPORT), (12, RiskClass.HIGH), (9, RiskClass.MEDIUM), (0, RiskClass.LOW))
RISK_REVIEW_MONTHS = 12


def risk_band(score: int) -> str:
    """The risk class a score falls in."""
    return next(rc for low, rc in RISK_SCORE_BANDS if score >= low).value


def risk_review_due_on(device_model: DeviceModel) -> date | None:
    """When the model's yearly risk review falls due; None when it was never scored (due now)."""
    from apps.pm.dates import add_months

    reviewed = device_model.risk_reviewed_on
    return add_months(reviewed, RISK_REVIEW_MONTHS) if reviewed else None


def risk_review_due(device_model: DeviceModel, today: date | None = None) -> bool:
    """Never reviewed, or a year or more since the last review."""
    due = risk_review_due_on(device_model)
    return due is None or due <= (today or timezone.localdate())


@transaction.atomic
def set_risk_score(device_model: DeviceModel, *, function, physical, maintenance, incidents, by=None, today: date | None = None) -> DeviceModel:
    """Score a model with the rubric: store the four parts, mark it reviewed today, and set its risk class to the score's band
    (through _save_model, so apps.pm.aem hears of a new class). Saving the same score again is the yearly review: only the date
    changes. Errors are keyed by the keyword names."""
    today = today or timezone.localdate()
    raw = {"function": function, "physical": physical, "maintenance": maintenance, "incidents": incidents}
    values, errors = {}, {}
    for key, field, label, low, high in RISK_PARTS:
        try:
            values[field] = _whole(raw[key], key, low, high, f"{label} is a whole number from {low} to {high}.")
        except ValidationError as e:
            errors.update(e.message_dict)
    if errors:
        raise ValidationError(errors)
    score = sum(values.values())
    band = risk_band(score)
    review = device_model.risk_score is not None and all(getattr(device_model, f) == v for f, v in values.items())
    reason = f"Risk score reviewed: {score}" if review else f"Risk scored {score} ({RiskClass(band).label})"
    _save_model(device_model, {**values, "risk_reviewed_on": today, "risk_class": band}, by=by, reason=reason)
    return device_model


@transaction.atomic
def clear_risk_score(device_model: DeviceModel, *, by=None) -> DeviceModel:
    """Back to unscored: the parts and the review date go; the risk class stays as it is until the model is scored again."""
    _save_model(device_model, {field: None for _key, field, *_rest in RISK_PARTS} | {"risk_reviewed_on": None}, by=by, reason="Risk score cleared")
    return device_model


def rename_department(department: Department, name: str) -> Department:
    """Rename a department; the name stays unique in the facility in any letter case."""
    name = _clean_text(name, 80)
    if not name:
        raise ValidationError({"name": "Enter the department's name."})
    if Department.objects.filter(name__iexact=name).exclude(pk=department.pk).exists():
        raise ValidationError({"name": f"{name} is already a department here."})
    if name != department.name:
        old = department.name
        department.name = name
        department.save(update_fields=["name"])
        # A clinical requester's unit is the department's name (User.department, apps.workorders.scoping): they follow the rename
        # rather than suddenly seeing nothing. Users are not tenant rows, so the tenant is explicit.
        from apps.accounts.models import User

        User.objects.filter(tenant_id=department.tenant_id, department__iexact=old).update(department=name)
    return department


@transaction.atomic
def create_asset(*, tag, device_model, department, serial="", room="", installed_on=None, acquisition_cost=None, warranty_end=None, condition=3,
                 last_pm_on=None, next_pm_on=None, notes="", status=AssetStatus.IN_SERVICE, by=None, today: date | None = None,
                 added_on: date | None = None, added_as: str = AddedAs.NEW, incoming_inspection: str = "",
                 inspection_due: date | None = None, ownership: str = Ownership.OWNED, owner: str = "", owner_reference: str = "",
                 arrived_on: date | None = None, due_back_on: date | None = None, owner_pm_due_on: date | None = None,
                 stands_in_for=None) -> Asset:
    """Add a device. Its first PM is `next_pm_on` when given, else first_pm_due(); its acquisition cost defaults to the model's list
    cost. Tags are unique in the facility in any letter case and can never change afterwards (they are on the sticker and in URLs).
    `added_on` dates the history's "added" row on an earlier day: the importer (apps.imports.kinds.devices) adds a device retired
    years ago in the previous system with both its rows on the day it was retired (set_status's `changed_on`), so its history reads
    in order. `added_as` (slice 25): new to the facility (the survey binder then looks for its incoming inspection), already in use
    here (entered after the fact), or imported (the importer's). `incoming_inspection` (slice 26): "" adds the device in the status
    given (the API's and the importer's way); "waiting" adds a new device out of service, awaiting its incoming inspection, with no
    next PM (its PM clock starts when it passes) and an Incoming inspection work order opened with it (on the added day, due
    `inspection_due` or INSPECTION_DUE_DAYS later), unassigned: the caller assigns it (Add device: take or assign).

    Slice 29: `ownership` other than ours adds a temporary device (add_temporary_device checks its stay: owner, reference, dates,
    stands_in_for, and calls this): acquisition cost 0, installed on the day it arrived, no last or next PM whatever the intake (its
    owner maintains it), support "Owner maintains"."""
    today = today or timezone.localdate()
    if ownership not in Ownership.values:
        raise ValidationError({"ownership": "Choose whose the device is."})
    temporary = ownership != Ownership.OWNED
    if temporary:
        acquisition_cost, installed_on, last_pm_on, next_pm_on = Decimal("0"), arrived_on, None, None
    if added_as not in AddedAs.values:
        raise ValidationError({"added_as": "Say whether the device is new, already in use here, or imported."})
    if incoming_inspection not in ("", INCOMING_WAITING):
        raise ValidationError({"incoming_inspection": "Choose whether the new device waits for its incoming inspection."})
    waiting = incoming_inspection == INCOMING_WAITING
    if waiting:
        if added_as != AddedAs.NEW:
            raise ValidationError({"incoming_inspection": "Only a device new to the facility waits for an incoming inspection."})
        if last_pm_on or next_pm_on:
            raise ValidationError({"next_pm_on" if next_pm_on else "last_pm_on":
                                   "A new device's PM schedule starts when it passes its incoming inspection."})
        if inspection_due is not None and inspection_due < (added_on or today):
            raise ValidationError({"inspection_due": "The inspection cannot be due before the device was added."})
        status = AssetStatus.OUT_OF_SERVICE
    if added_on is not None and added_on > today:
        raise ValidationError({"added_on": "A device cannot be added on a day after today."})
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
    no_pm = waiting or temporary  # slice 26: its PM clock starts at the pass; slice 29: its owner maintains it
    asset = Asset(tag=tag, device_model=device_model, department=department, serial=_clean_text(serial, 80), room=_clean_text(room, 40),
                  installed_on=installed_on, acquisition_cost=acquisition_cost, warranty_end=warranty_end, condition=condition,
                  last_pm_on=last_pm_on, status=status, notes=(notes or "").strip(), added_as=added_as, awaiting_inspection=waiting,
                  next_pm_on=None if no_pm else (next_pm_on or first_pm_due(device_model, installed_on=installed_on, last_pm_on=last_pm_on,
                                                                             today=today)),
                  ownership=ownership, owner=_clean_text(owner, 120) if temporary else "", owner_reference=(owner_reference or "").strip() if temporary else "",
                  arrived_on=arrived_on if temporary else None, due_back_on=due_back_on if temporary else None,
                  owner_pm_due_on=owner_pm_due_on if temporary else None, stands_in_for=stands_in_for if temporary else None)
    asset._change_reason = "Added, waiting for its incoming inspection" if waiting else "Added"
    _save_on(asset, added_on)
    if waiting:
        from apps.workorders import inspections

        inspections.open_for(asset, by=by, today=today, opened_on=added_on or today, due_on=inspection_due)
    return asset


def status_label(asset) -> str:
    """The device's status as every screen, CSV, and the API words it (slice 26): "Awaiting inspection" for a device out of service
    waiting for its incoming inspection, else its status. Slice 28: "Held for incident" for a device held as evidence, first. Slice 29:
    "Returned to owner" for a temporary device that went back (never "Retired")."""
    if getattr(asset, "incident_hold", False):
        return HELD_LABEL
    if asset.status == AssetStatus.RETIRED and owner_maintains(asset):
        return RETURNED_LABEL
    if asset.awaiting_inspection and asset.status == AssetStatus.OUT_OF_SERVICE:
        return AWAITING_LABEL
    return asset.get_status_display()


EDITABLE_FIELDS = ("serial", "device_model", "department", "room", "installed_on", "acquisition_cost", "warranty_end", "condition", "next_pm_on", "notes")


def _locked_row(asset: Asset) -> Asset:
    """The device's row as it is now, locked until the transaction ends (slice 26 review fix): update_asset and set_status work on it,
    never on the caller's copy, which may predate a passed incoming inspection (the pass clears awaiting_inspection and starts the PM
    clock; a full save of an older copy, read by a page, the API, or an import chunk before the pass, would put them back). Its open
    work orders first, in number order (its open incoming inspections among them: inspections.lock_open's order), then its row.
    Slice 28 merge fix: every writer takes a device's work orders before its row (workorders.services._lock_for_hold, a start or
    completion, reads the hold that way), so retiring (which cancels open PMs) and a new next PM (which moves the open PM) never hold
    the device's row while waiting for a work order a start holds."""
    from apps.workorders.services import lock_work

    lock_work(asset.pk)
    return Asset.objects.select_for_update().get(pk=asset.pk)


@transaction.atomic
def update_asset(asset: Asset, *, by=None, today: date | None = None, imported_last_pm=_UNSET, **fields) -> Asset:
    """Change a device's details. Not its tag (on the sticker and in URLs), status (set_status), or contract (the contracts screen).
    A new model does not move the next PM by itself; change next_pm_on alongside it when the interval differs. A device waiting for
    its incoming inspection (slice 26) keeps no next PM: a blank one is accepted, a date refused (pm_clock_message), keyed next_pm_on.
    The last PM is not an editable detail (completed PM work orders set it): `imported_last_pm` is the importer's alone, the last PM
    the previous system recorded (apps.imports.kinds.devices, on a re-import), with create_asset's rules for it. Works on the device's
    row as it is now, locked (_locked_row); the caller's `asset` is read again and returned.
    Slice 29, a temporary device (its owner maintains it): no next PM (a blank one is accepted, a date refused: pm_clock_message,
    keyed next_pm_on), no imported last PM (keyed last_pm_on), and an acquisition cost of 0 (anything else refused, keyed
    acquisition_cost: keep_temporary_device records what the facility paid). Its stay changes through update_temporary."""
    today = today or timezone.localdate()
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    caller, asset = asset, _locked_row(asset)
    temporary = owner_maintains(asset)
    # The install and warranty dates only when one of them changes: a stored pair that breaks the rule (imported data) must not
    # block an edit of the room or the notes. Before the imported last PM, which is measured from the install date: a refused
    # install date is named first, so the importer leaves it out and keeps the last PM, as it does for a new device.
    if any(f in fields and fields[f] != getattr(asset, f) for f in ("installed_on", "warranty_end")):
        _check_dates(**{f: fields.get(f, getattr(asset, f)) for f in ("installed_on", "warranty_end")}, today=today)
    if imported_last_pm is not _UNSET and imported_last_pm != asset.last_pm_on:
        if temporary and imported_last_pm is not None:  # slice 29: no PM of ours
            raise ValidationError({"last_pm_on": pm_clock_message(asset)})
        _check_last_pm(imported_last_pm, installed_on=fields.get("installed_on", asset.installed_on), today=today)
        fields["last_pm_on"] = imported_last_pm
    if "device_model" in fields:
        _check_tenant(fields["device_model"], "device_model")
    if "department" in fields:
        _check_tenant(fields["department"], "department")
    if "next_pm_on" in fields and (asset.awaiting_inspection or temporary):
        # Slice 26: no next PM while it waits (its PM clock starts at the pass): a blank one is what it has, a date is refused.
        # Slice 29: likewise a temporary device, whose owner maintains it.
        if fields["next_pm_on"] is not None:
            raise ValidationError({"next_pm_on": pm_clock_message(asset)})
    elif "next_pm_on" in fields and fields["next_pm_on"] is None and asset.status != AssetStatus.RETIRED:
        raise ValidationError({"next_pm_on": "A device in use needs a next PM date."})
    _check_next_pm(fields.get("next_pm_on"), today)
    # The last PM against the install date only when the install date changes, and on the field the form has: a device added
    # before this rule (import, the demo) may have an older PM on record, and must stay editable.
    new_install, last_pm = fields.get("installed_on"), fields.get("last_pm_on", asset.last_pm_on)
    if new_install and new_install != asset.installed_on and last_pm and last_pm < new_install:
        raise ValidationError({"installed_on": f"The install date cannot be after the last PM on record ({last_pm:%b %-d, %Y})."})
    _check_numbers(**{k: fields[k] for k in ("acquisition_cost", "condition") if k in fields})
    if temporary and fields.get("acquisition_cost") not in (None, 0):  # slice 29 (Decimal("0.00") == 0)
        raise ValidationError({"acquisition_cost": f"{asset.tag} is {_kind(asset)}, not ours: its acquisition cost stays 0. If the "
                                                   "facility buys it, Keep it records the price."})
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
    caller.refresh_from_db()
    return caller


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
def set_status(asset: Asset, to_status: str, *, by=None, note: str = "", today: date | None = None, changed_on: date | None = None) -> Asset:
    """Move a device along STATUS_CHANGES. Retiring cancels its open PM work orders that can be cancelled and refuses while any
    other work is open (finish or cancel it first); the device leaves the PM schedule (_retire). Reinstating puts it back with a PM
    due today. `note` is kept in the device's history. `changed_on` dates the history row on an earlier day (not before the install
    date): the importer's retirement from the previous system, so the AEM evidence (apps.pm.aem) counts the device in use until then.
    Slice 26, a device waiting for its incoming inspection (Asset.awaiting_inspection; the flag never changes here):
    - it never goes in service or on loan here (HOLD): refused in words naming its open inspection (hold_message). Only its passed
      inspection (pass_incoming_inspection) or use_before_inspection puts it in use.
    - Found (missing to out of service) and Reinstate (retired to out of service) bring it back still waiting, with no next PM;
      reinstating opens an incoming inspection when none is open.
    - Retiring it (a new device going back to the vendor) cancels its open incoming inspections as it cancels open PMs.
    Slice 29, a temporary device (a rental, vendor loaner, or demo unit): never retired (it leaves by return_to_owner, which records
    how it was cleaned and what happened to patient data), never reinstated once returned (a unit that comes back is added again),
    and never lent out (it is not ours to lend): refused in words pointing to Return to owner (temporary_status_refusal)."""
    today = today or timezone.localdate()
    if to_status not in AssetStatus.values:
        raise ValidationError("Choose a device status.")
    caller, asset = asset, _locked_row(asset)  # the row as it is now (review fix: never a copy from before a pass)
    if asset.incident_hold:  # slice 28: the incident's release is the only way out
        raise ValidationError(held_message(asset))
    refusal = temporary_status_refusal(asset, to_status)  # slice 29: before the general refusal, whose words say "retired"
    if refusal:
        raise ValidationError(refusal)
    if to_status not in STATUS_CHANGES.get(asset.status, set()):
        raise ValidationError(f"{asset.tag} cannot go from {asset.get_status_display().lower()} to {AssetStatus(to_status).label.lower()}.")
    if asset.awaiting_inspection and to_status in HOLD:
        raise ValidationError(hold_message(asset))
    if changed_on is not None and changed_on > today:
        raise ValidationError({"changed_on": "The status cannot change on a day after today."})
    if changed_on is not None and asset.installed_on and changed_on < asset.installed_on:
        raise ValidationError({"changed_on": "The status cannot change before the device was installed."})
    if to_status == AssetStatus.RETIRED:
        _retire(asset, by=by, note=note, today=today, changed_on=changed_on)
        caller.refresh_from_db()
        return caller
    reinstated_waiting = False
    if asset.status == AssetStatus.RETIRED and asset.awaiting_inspection:
        reinstated_waiting = True  # still waiting: no next PM until its inspection passes
    elif asset.status == AssetStatus.RETIRED:
        asset.next_pm_on = today  # back in use: inspect it before anyone relies on it
    asset.status = to_status
    asset._change_reason = (note or "").strip()[:100] or f"Status: {AssetStatus(to_status).label}"
    _save_on(asset, changed_on)
    if reinstated_waiting:
        from apps.workorders import inspections

        if inspections.open_inspection(asset) is None:
            inspections.open_for(asset, by=by, today=today)
    caller.refresh_from_db()
    return caller


def _retire(fresh: Asset, *, by, note: str, today: date, changed_on: date | None = None) -> None:
    """Retire the device `fresh`, its row as it is now, locked (_locked_row): set_status's retire body, shared with return_to_owner
    (slice 29), which sets returned_on and the return's cleaning and data on `fresh` first so they go in the same save. Refused in
    words while work is open that retiring does not cancel, naming it: complete it, or cancel it (an in-progress work order goes back
    to open first). Cancels the device's open PMs and, for a device waiting for its incoming inspection or a temporary device on its
    way back to its owner, its open incoming inspections (only open or awaiting parts: one in progress blocks). The device leaves the
    PM schedule (no next PM) and is saved retired with `note` as its history reason (default "Status: Retired"), dated `changed_on`
    when given (set_status's importer case)."""
    from apps.workorders.models import OPEN_STATUSES, WoStatus, WoType
    from apps.workorders.services import change_status

    temporary = owner_maintains(fresh)
    cancellable = (WoType.PM, WoType.INSPECTION) if fresh.awaiting_inspection or temporary else (WoType.PM,)
    open_wos = list(fresh.work_orders.filter(status__in=OPEN_STATUSES).order_by("number"))
    blocking = [w for w in open_wos if not (w.type in cancellable and w.status in (WoStatus.OPEN, WoStatus.AWAITING_PARTS))]
    if blocking:
        numbers = ", ".join(w.number for w in blocking)
        leaving = "returning it to its owner" if temporary else "retiring the device"
        raise ValidationError(f"{fresh.tag} has open work: {numbers}. Complete it, or cancel it (an in-progress work order goes "
                              f"back to open first), before {leaving}.")
    for w in open_wos:
        change_status(w, WoStatus.CANCELLED, by=by, note="Device returned to its owner" if temporary else "Device retired", as_of=today)
    fresh.next_pm_on = None
    fresh.status = AssetStatus.RETIRED
    fresh._change_reason = (note or "").strip()[:100] or f"Status: {AssetStatus.RETIRED.label}"
    _save_on(fresh, changed_on)


def temporary_status_refusal(asset: Asset, to_status: str) -> str:
    """Why set_status refuses moving a temporary device (slice 29) to `to_status`, or "" (always "" for a device of ours): retiring
    it (it leaves by Return to owner, which records the return's cleaning and patient data), reinstating it once returned (a unit
    that comes back is added again: one device record per arrival), and lending it out (it is not ours to lend)."""
    if not owner_maintains(asset):
        return ""
    if asset.status == AssetStatus.RETIRED:
        return f"{asset.tag} was returned to its owner. If the unit is back, add it again with Add rental or loaner."
    if to_status == AssetStatus.RETIRED:
        return f"{asset.tag} is {_kind(asset)}: it leaves by Return to owner, never by retiring."
    if to_status == AssetStatus.ON_LOAN:
        return f"{asset.tag} is {_kind(asset)}, not ours to lend out. When it leaves, use Return to owner."
    return ""


def hold_message(asset: Asset) -> str:
    """Why a device waiting for its incoming inspection does not go in service or on loan by hand (slice 26), naming its open
    inspection: set_status's refusal, and the devices importer's note."""
    from apps.workorders import inspections

    wo = inspections.open_inspection(asset)
    if wo is None:
        return (f"{asset.tag} is waiting for its incoming inspection, and none is open. Open one: the device goes into service when "
                "its incoming inspection passes.")
    return f"{asset.tag} is waiting for its incoming inspection ({wo.number}): it goes into service when that inspection passes."


def held_message(asset: Asset) -> str:
    """Why a device held as evidence does not change status (slice 28): set_status's refusal. No incident number: the reader may not
    see incidents (Equipment Edit is enough to try)."""
    return (f"{asset.tag} is held as evidence for an incident investigation: nobody uses, repairs, or tests it until the incident "
            "releases it.")


def pm_clock_message(asset: Asset) -> str:
    """Why a device waiting for its incoming inspection takes no next PM date (slice 26): update_asset's refusal, keyed next_pm_on.
    Slice 29: a temporary device takes none at all (nor an imported last PM): its owner maintains it."""
    from apps.workorders import inspections

    if owner_maintains(asset):
        return "Its owner maintains it: Cadence records the owner's PM date from its sticker, never a PM date of ours."
    wo = inspections.open_inspection(asset)
    return f"Its PM schedule starts when it passes its incoming inspection{f' {wo.number}' if wo else ''}."


def status_actions(asset: Asset, user) -> list[dict]:
    """The status buttons the drawer shows this user for this device: [{to, label, style, confirm}], most likely first. Slice 26: for
    a device waiting for its incoming inspection never In service or On loan (HOLD), and Found / Reinstate bring it back out of
    service (AWAITING_ONLY, offered for no other device). Putting it in use before its inspection is not a status button: the drawer
    offers it on its own (permissions.can_use_before_inspection, use_before_inspection)."""
    from . import permissions as perms

    if asset.incident_hold:  # slice 28: held as evidence, released only from its incident
        return []
    temporary = owner_maintains(asset)
    if temporary and asset.status == AssetStatus.RETIRED:  # slice 29: returned to its owner; a unit that is back is added again
        return []
    level = user.level_for(perms.MODULE)  # once, not once per button
    awaiting = asset.awaiting_inspection
    # A device in use is most likely being tagged out; one that is not is most likely coming back (the mock's single button).
    in_use = asset.status in (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN)
    order = ([AssetStatus.OUT_OF_SERVICE, AssetStatus.IN_SERVICE] if in_use else [AssetStatus.IN_SERVICE, AssetStatus.OUT_OF_SERVICE]) + [
        AssetStatus.ON_LOAN, AssetStatus.MISSING, AssetStatus.RETIRED]
    out = []
    for to in order:
        if to not in STATUS_CHANGES.get(asset.status, set()) or level < perms.status_level(asset.status, to):
            continue
        if (awaiting and to in HOLD) or (not awaiting and (asset.status, to) in AWAITING_ONLY):
            continue
        if temporary and to in (AssetStatus.RETIRED, AssetStatus.ON_LOAN):  # slice 29: Return to owner instead; not ours to lend
            continue
        label, style = status_action_label(asset.status, to)
        confirm = ""
        if to == AssetStatus.RETIRED and awaiting:
            confirm = f"Retire {asset.tag}? Its open incoming inspection is cancelled: retire a new device when it goes back to the vendor."
        elif to == AssetStatus.RETIRED:
            confirm = f"Retire {asset.tag}? Its open PM work orders are cancelled and it leaves the PM schedule."
        elif to == AssetStatus.MISSING and awaiting:
            confirm = f"Mark {asset.tag} missing? Its incoming inspection stays open until it is found."
        elif to == AssetStatus.MISSING and temporary:
            confirm = f"Mark {asset.tag} missing? It stays on the inventory until it is found and returned to its owner."
        elif to == AssetStatus.MISSING:
            confirm = f"Mark {asset.tag} missing? It counts as overdue for PM until it is found."
        out.append({"to": to, "label": label, "style": style, "confirm": confirm})
    return out


# --- incoming inspections (slice 26) ---------------------------------------------------------------------------------------------
#
# A device added new and waiting for its incoming inspection (create_asset's "waiting" path, Asset.awaiting_inspection) has no next
# PM and is held out of use (set_status). Two writers move it on: the pass (pass_incoming_inspection, from completing its inspection
# with result passed: apps.workorders.services._on_completed) and the documented exception (use_before_inspection). The read side is
# apps.workorders.inspections.

USE_BEFORE_PERMISSION = "Putting a device in use before its incoming inspection needs Equipment Approve."


@transaction.atomic
def use_before_inspection(asset: Asset, reason: str, *, by=None, today: date | None = None) -> Asset:
    """Put a device waiting for its incoming inspection in use before it (an emergency, a loaner or rental needed now, a device that
    arrived on the unit already in use): from out of service (or missing) to in service, with `reason` from UseBeforeInspection (no
    free text). The device keeps waiting (the flag stays set and it has no next PM: its PM clock still starts only at the pass). Its
    history reads "In use before its incoming inspection: <reason's label>" (dated `today` when that is an earlier day), which the
    drawer's banner and the survey binder read back (inspections.uses_before). Its open incoming inspection (one is opened if none is)
    goes to high priority, due the next day (an earlier due date is kept), with a status-history note naming the reason, so CE
    inspects it where it is. Equipment Approve (permissions.can_use_before_inspection) for a user given as `by` (PermissionDenied).
    Refused in words: a device not waiting, or not out of service or missing; a reason not listed is keyed "reason". Slice 28: a
    device held as evidence for an incident investigation is refused first (held_message): only its incident releases it. Returns the
    device, read again; inspections.open_inspection(asset) is the inspection."""
    from apps.workorders import inspections
    from apps.workorders.models import Priority, WorkOrderStatusHistory

    today = today or timezone.localdate()
    if by is not None and not permissions.can_use_before_inspection(by):
        raise PermissionDenied(USE_BEFORE_PERMISSION)
    reason = str(reason or "").strip()
    if reason not in UseBeforeInspection.values:
        raise ValidationError({"reason": "Choose why the device goes into use before its incoming inspection."})
    inspections.lock_open(asset.pk)  # its inspections, then its row (the order completing one takes): a pass at that moment goes first or after
    fresh = Asset.objects.select_for_update().get(pk=asset.pk)
    if fresh.incident_hold:  # slice 28: held as evidence, so nobody uses it
        raise ValidationError(held_message(fresh))
    if not fresh.awaiting_inspection:
        raise ValidationError(f"{fresh.tag} is not waiting for an incoming inspection.")
    if fresh.status not in USE_BEFORE_FROM:
        if fresh.status == AssetStatus.IN_SERVICE:
            raise ValidationError(f"{fresh.tag} is already in use before its incoming inspection.")
        if fresh.status == AssetStatus.RETIRED and owner_maintains(fresh):  # slice 29: never "retired"
            raise ValidationError(f"{fresh.tag} was returned to its owner.")
        if fresh.status == AssetStatus.RETIRED:
            raise ValidationError(f"{fresh.tag} is retired. Reinstate it first.")
        raise ValidationError(f"{fresh.tag} is {fresh.get_status_display().lower()}; only a device out of service or missing goes into use "
                              "before its incoming inspection.")
    label = UseBeforeInspection(reason).label
    fresh.status = AssetStatus.IN_SERVICE
    fresh._change_reason = inspections.use_before_reason(label)
    if by is not None:
        fresh._history_user = by
    _save_on(fresh, today if today < timezone.localdate() else None)
    due = today + timedelta(days=1)
    wo = inspections.open_inspection(fresh)
    if wo is None:
        wo = inspections.open_for(fresh, by=by, today=today, due_on=due, priority=Priority.HIGH)
    else:
        wo.priority, wo.due_on = Priority.HIGH, min(wo.due_on, due)
        wo.save(update_fields=["priority", "due_on", "updated_at"])
    WorkOrderStatusHistory.objects.create(tenant=wo.tenant, work_order=wo, from_status=wo.status, to_status=wo.status, changed_by=by,
                                          note=f"Device put in use before its incoming inspection: {label}. High priority, due "
                                               f"{wo.due_on:%b} {wo.due_on.day}, {wo.due_on.year}: inspect it where it is.")
    asset.refresh_from_db()
    return asset


@transaction.atomic
def pass_incoming_inspection(asset: Asset, wo, *, by=None, on: date) -> Asset:
    """The device passed its incoming inspection `wo` on `on` (completing it with result passed: apps.workorders.services
    ._on_completed calls this; nothing else does). The one writer that clears Asset.awaiting_inspection. On the device row as it is,
    locked: a device no longer waiting (another inspection passed first, or it never waited) is left as it is, and nothing moves.
    Otherwise, in one save with one history row ("Passed incoming inspection WO-..."; dated `on` when that is an earlier day): the flag
    clears, the PM clock starts (next PM one interval after `on`; a retired device keeps none, and slice 29, a temporary device gets
    none: its owner maintains it), and a device out of service goes in
    service unless an open tagged-out repair still holds it (services.holding_repairs: the last of them returns it). A device already
    in service (use_before_inspection) only stops waiting. Then the device's other open incoming inspections are cancelled with a note
    naming `wo` (one in progress goes back to open first; one another transaction holds at that moment is left open). Slice 28: a
    device held as evidence for an incident investigation stays out of service (the pass is still recorded, the flag cleared, the PM
    clock started): only its incident's release puts it in use. The caller's `asset` is read again."""
    from apps.pm.dates import add_months
    from apps.workorders import inspections
    from apps.workorders.models import OPEN_STATUSES, WoStatus
    from apps.workorders.services import change_status, holding_repairs

    fresh = Asset.objects.select_for_update().get(pk=asset.pk)  # the device's row only (a join would lock its model's row too)
    if not fresh.awaiting_inspection:
        return fresh
    fresh.awaiting_inspection = False
    if fresh.status != AssetStatus.RETIRED and not owner_maintains(fresh):  # slice 29: a temporary device's owner maintains it
        fresh.next_pm_on = add_months(on, fresh.pm_interval_months)
    if fresh.status == AssetStatus.OUT_OF_SERVICE and not fresh.incident_hold and not holding_repairs(fresh).exists():
        fresh.status = AssetStatus.IN_SERVICE
    fresh._change_reason = inspections.passed_reason(wo)
    if by is not None:
        fresh._history_user = by
    _save_on(fresh, on if on < timezone.localdate() else None)
    note = f"Cancelled: incoming inspection {wo.number} passed"
    # A completion holds them already (inspections.lock_open). One another writer has locked at this very moment (Start work, an
    # assignment) is left open rather than waited for while this device's row is held: a later pass of it finds the device passed
    # and moves nothing.
    others = (inspections.incoming(fresh).filter(status__in=OPEN_STATUSES).exclude(pk=wo.pk).order_by("opened_on", "number")
              .select_for_update(skip_locked=True))
    for other in others:
        if other.status == WoStatus.IN_PROGRESS:
            change_status(other, WoStatus.OPEN, by=by, note=note, as_of=on)  # in progress cannot be cancelled: back to open first
        change_status(other, WoStatus.CANCELLED, by=by, note=note, as_of=on)
    asset.refresh_from_db(fields=["status", "awaiting_inspection", "next_pm_on", "last_pm_on", "updated_at"])
    return fresh


# --- incident holds (slice 28) ---------------------------------------------------------------------------------------------------
#
# A device suspected in an incident is held as evidence (Asset.incident_hold) while any open incident holds it (apps.incidents:
# IncidentHold rows). These two are the flag's only writers, called by apps.incidents.services on the device's row as it is, locked
# (_locked_row: its open inspections, then its row). Neither checks levels or the incident's rules: the incident services do.


@transaction.atomic
def set_incident_hold(asset: Asset, *, by=None, today: date | None = None) -> Asset:
    """Hold the device as evidence: incident_hold set; a device in service or on loan goes out of service (any other status stays:
    in repair, out of service, awaiting its incoming inspection). A missing or retired device is refused (record the incident
    without a hold). Holding a device already held changes nothing. The history reads HELD_REASON (no incident number: the device's
    history is Equipment View's). Returns the caller's device, read again. Slice 29: a temporary device returned to its owner is
    refused in those words, never "retired"."""
    caller, fresh = asset, _locked_row(asset)
    if fresh.status in (AssetStatus.MISSING, AssetStatus.RETIRED):
        returned = fresh.status == AssetStatus.RETIRED and owner_maintains(fresh)
        state = "was returned to its owner" if returned else f"is {fresh.get_status_display().lower()}"
        raise ValidationError(f"{fresh.tag} {state}: record the incident without holding it.")
    if not fresh.incident_hold:
        fresh.incident_hold = True
        if fresh.status in (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN):
            fresh.status = AssetStatus.OUT_OF_SERVICE
        fresh._change_reason = HELD_REASON
        if by is not None:
            fresh._history_user = by
        fresh.save()
    caller.refresh_from_db()
    return caller


@transaction.atomic
def clear_incident_hold(asset: Asset, *, to_status: str | None = None, by=None) -> Asset:
    """End the device's hold: incident_hold cleared and, when `to_status` is given, the status set to it in the same save (in
    service for a return to use the incident's release allowed; the status before the hold for "recorded in error"). The caller decides
    whether another open incident still holds the device (then it does not call this) and whether the device may go in service (not
    awaiting its incoming inspection, no repair holding it). The history reads RELEASED_REASON. Returns the caller's device, read
    again."""
    caller, fresh = asset, _locked_row(asset)
    if to_status is not None and to_status not in AssetStatus.values:
        raise ValidationError("Choose a device status.")
    changed = fresh.incident_hold or (to_status is not None and to_status != fresh.status)
    fresh.incident_hold = False
    if to_status is not None:
        fresh.status = to_status
    if changed:
        fresh._change_reason = RELEASED_REASON
        if by is not None:
            fresh._history_user = by
        fresh.save()
    caller.refresh_from_db()
    return caller


# --- temporary equipment (slice 29) ----------------------------------------------------------------------------------------------
#
# Rentals, vendor loaners, and demo or evaluation units (Asset.ownership not ours): on the inventory while on site, inspected before
# first use (slice 26's incoming inspection, on the TEMPORARY_INCOMING checklist), maintained by their owner (no next PM, no PM work
# orders; the owner's PM date from its sticker), and gone by Return to owner (status retired, read "Returned to owner"). One device
# record per arrival: a unit that comes back later is entered again (matching_returned warns). Who may: permissions.TEMPORARY_LEVEL
# (Edit) to add one, change its stay, and return it; permissions.KEEP_LEVEL (Approve) to keep it. Each service checks the level of a
# user given as `by` (PermissionDenied); `by` None is the system's own change (the seed, a shell).

TEMPORARY_KINDS = TEMPORARY
REFERENCE_MAX = Asset._meta.get_field("owner_reference").max_length
OWNER_MAX = Asset._meta.get_field("owner").max_length
TEMPORARY_INTAKE = (AddedAs.NEW, AddedAs.EXISTING)  # how a temporary device comes to be in Cadence (never imported)
STAY_FIELDS = ("owner", "owner_reference", "due_back_on", "owner_pm_due_on", "stands_in_for")  # what update_temporary changes
TEMPORARY_PERMISSION = "Adding a rental, vendor loaner, or demo unit, changing its stay, or returning it to its owner needs Equipment Edit."
KEEP_PERMISSION = "Keeping a rental, vendor loaner, or demo unit as the facility's own needs Equipment Approve."
STAY_REASON = "Stay changed"  # the device's history reasons
RETURNED_REASON = "Returned to its owner"
KEPT_REASON = "Kept by the facility"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _kind(asset) -> str:
    """The device's kind in a sentence: "a rental", "a vendor loaner", "a demo or evaluation unit"."""
    return f"a {Ownership(asset.ownership).label.lower()}"


# A device of ours a vendor loaner stands in for is back (the loaner can go) once it is in service or retired with no vendor repair still
# open on it (review fix: a repair sent to the vendor that leaves the device in service is the loaner's very reason to be here).
LOANER_BACK_STATUSES = (AssetStatus.IN_SERVICE, AssetStatus.RETIRED)


def _open_vendor_repair():
    from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

    return WorkOrder.objects.filter(type=WoType.REPAIR, status__in=OPEN_STATUSES, vendor_service=True)


def loaner_device_back(device) -> bool:
    """Whether the device of ours a vendor loaner stands in for is back: in service or retired, no vendor repair open on it."""
    return device.status in LOANER_BACK_STATUSES and not _open_vendor_repair().filter(asset_id=device.pk).exists()


def loaner_back_q(prefix: str = "stands_in_for") -> Q:
    """loaner_device_back as a condition on loaners (their `prefix` device), for lists that read many at once."""
    return Q(**{f"{prefix}__status__in": LOANER_BACK_STATUSES}) & ~Exists(_open_vendor_repair().filter(asset_id=OuterRef(f"{prefix}_id")))


def matching_returned(device_model, serial: str):
    """Returned temporary devices of this facility with this model and serial (any letter case): the unit was here before. Add rental
    or loaner warns with them; it never refuses (each arrival is its own record)."""
    serial = (serial or "").strip()
    if not serial:
        return Asset.objects.none()
    return Asset.objects.filter(device_model=device_model, serial__iexact=serial, status=AssetStatus.RETIRED).exclude(OWNED).order_by("-returned_on")


def _stand_in_refusal(device, *, kind: str, asset=None) -> str:
    """Why `device` cannot be the device of ours a vendor loaner stands in for, or "": only a vendor loaner stands in for one, and
    only for a device of this facility (read again through the tenant-scoped manager, so another facility's is never found), ours,
    not retired, and never the loaner itself."""
    if kind != Ownership.LOANER:
        return "Only a vendor loaner stands in for a device of ours."
    found = Asset.objects.filter(pk=device.pk).first() if isinstance(device, Asset) else None
    if found is None:
        return "Choose the device of ours it stands in for, from this facility."
    if asset is not None and found.pk == asset.pk:
        return "A loaner cannot stand in for itself."
    if owner_maintains(found):
        return f"{found.tag} is not ours: a loaner stands in for a device of ours."
    if found.status == AssetStatus.RETIRED:
        return f"{found.tag} is retired: a loaner stands in for a device of ours still in use."
    return ""


def _stay_values(raw: dict, *, kind: str, arrived_on, today: date, asset=None) -> dict:
    """The stay's values present in `raw`, cleaned and checked with one rule set for adding and changing (add_temporary_device,
    update_temporary). Raises one ValidationError with every problem, keyed by field. `asset`: the device being changed, whose current
    stands_in_for is accepted as it is (the device of ours may have been retired since; sending it back is no change)."""
    from apps.pm.dates import add_months

    out, errors = {}, {}
    if "owner" in raw:
        out["owner"] = _clean_text(raw["owner"], OWNER_MAX)
        if not out["owner"]:
            errors["owner"] = "Enter the company that owns it."
    if "owner_reference" in raw:
        reference = str(raw["owner_reference"] or "").strip()
        if len(reference) > REFERENCE_MAX:
            errors["owner_reference"] = f"Keep the agreement, PO, or RMA number to {REFERENCE_MAX} characters."
        elif reference:
            try:
                OWNER_REFERENCE_VALIDATOR(reference)
            except ValidationError as e:
                errors["owner_reference"] = e.messages[0]
        out["owner_reference"] = reference
    if "due_back_on" in raw:
        due = raw["due_back_on"]
        if due is not None and arrived_on is not None and due < arrived_on:
            errors["due_back_on"] = f"It cannot be due back before it arrived ({_day(arrived_on)})."
        out["due_back_on"] = due
    if "owner_pm_due_on" in raw:
        pm = raw["owner_pm_due_on"]  # any day: a past one is recorded (the incoming inspection then waits for the owner's PM)
        if pm is not None and pm.year < 2000:
            errors["owner_pm_due_on"] = "Enter the owner's PM date in full (the year looks wrong)."
        elif pm is not None and pm > add_months(today, NEXT_PM_YEARS * 12):
            errors["owner_pm_due_on"] = f"The owner's PM date must be within {NEXT_PM_YEARS} years."
        out["owner_pm_due_on"] = pm
    if "stands_in_for" in raw:
        device = raw["stands_in_for"]
        unchanged = asset is not None and device is not None and getattr(device, "pk", None) == asset.stands_in_for_id
        if device is not None and not unchanged:
            refusal = _stand_in_refusal(device, kind=kind, asset=asset)
            if refusal:
                errors["stands_in_for"] = refusal
        out["stands_in_for"] = device
    if errors:
        raise ValidationError(errors)
    return out


def _temporary_refusal(fresh: Asset) -> str:
    """Why the temporary services refuse `fresh` unless it is a temporary device still on site (not returned, not kept), or ""."""
    if not owner_maintains(fresh):
        if fresh.kept_on:
            return f"{fresh.tag} was kept by the facility on {_day(fresh.kept_on)}: it is ours now."
        return f"{fresh.tag} is ours, not a rental, vendor loaner, or demo unit."
    if fresh.status == AssetStatus.RETIRED:
        return f"{fresh.tag} was returned to its owner{f' on {_day(fresh.returned_on)}' if fresh.returned_on else ''}."
    return ""


@transaction.atomic
def add_temporary_device(*, tag, device_model, department, kind, owner, serial, owner_reference="", arrived_on=None, due_back_on=None,
                         owner_pm_due_on=None, stands_in_for=None, room="", added_as=AddedAs.NEW, incoming_inspection=INCOMING_WAITING,
                         inspection_due=None, by=None, today=None) -> Asset:
    """Add a rental, vendor loaner, or demo unit (Equipment Edit). `kind` in TEMPORARY_KINDS; `owner` (the company) and `serial` (the
    unit) required; `owner_reference` a token (OWNER_REFERENCE_VALIDATOR); arrived_on <= today (default today; required for EXISTING,
    the day it came); due_back_on >= arrived_on; owner_pm_due_on any day (a past one is recorded: the incoming inspection then refuses a
    pass until the owner does the PM); stands_in_for an owned, not retired device of this facility, only for a vendor loaner. added_as
    NEW waits for its incoming inspection (incoming_inspection "waiting") or is inspected now ("": the screen opens the inspection),
    EXISTING was already on site when entered (no inspection; counted, never a gap). Then create_asset. Refusals keyed by field.

    A new unit always waits, whichever of the two it is: "inspect it now" is Add device's (slice 26) way of saying the person adding it
    inspects it next, which the screen does by assigning the inspection create_asset opens and showing its Mark completed, so either
    value adds it out of service with its incoming inspection open (another value is refused, keyed incoming_inspection). EXISTING
    reads no incoming_inspection: it is added in service, no inspection opened. Never imported (added_as "imported" is refused). The
    stay's fields are checked first, all at once (_stay_values), then create_asset checks the tag, model, department, and the
    inspection's due date. The device's history reads "Added" (or "Added, waiting for its incoming inspection")."""
    today = today or timezone.localdate()
    if by is not None and not permissions.can_handle_temporary(by):
        raise PermissionDenied(TEMPORARY_PERMISSION)
    errors = {}
    if kind not in TEMPORARY_KINDS:
        errors["kind"] = "Choose rental, vendor loaner, or demo unit."
    serial = _clean_text(serial, 80)
    if not serial:
        errors["serial"] = "Enter the serial number on the unit."
    if added_as not in TEMPORARY_INTAKE:
        errors["added_as"] = "Say whether it is new to the facility or was already on site when entered here."
    elif added_as == AddedAs.NEW and incoming_inspection not in ("", INCOMING_WAITING):
        errors["incoming_inspection"] = "Choose whether it waits for its incoming inspection or is inspected now."
    if arrived_on is None and added_as == AddedAs.EXISTING:
        errors["arrived_on"] = "Enter the day it arrived."
    arrived_on = arrived_on or today
    if arrived_on > today:
        errors["arrived_on"] = "It cannot have arrived after today."
    elif arrived_on.year < 2000:
        errors["arrived_on"] = "Enter the day it arrived in full (the year looks wrong)."
    try:
        stay = _stay_values({"owner": owner, "owner_reference": owner_reference, "due_back_on": due_back_on,
                             "owner_pm_due_on": owner_pm_due_on, "stands_in_for": stands_in_for},
                            kind=kind if kind in TEMPORARY_KINDS else "", arrived_on=None if "arrived_on" in errors else arrived_on,
                            today=today)
    except ValidationError as e:
        errors = {**e.message_dict, **errors}
        stay = {}
    if "kind" in errors:
        errors.pop("stands_in_for", None)  # "only a vendor loaner" says nothing until the kind is chosen
    if errors:
        raise ValidationError(errors)
    waiting = added_as == AddedAs.NEW
    return create_asset(tag=tag, device_model=device_model, department=department, serial=serial, room=room, by=by, today=today,
                        added_as=added_as, incoming_inspection=INCOMING_WAITING if waiting else "",
                        inspection_due=inspection_due if waiting else None, ownership=kind, arrived_on=arrived_on, **stay)


@transaction.atomic
def update_temporary(asset, *, by=None, today=None, **fields) -> Asset:
    """Change a temporary device's stay (Equipment Edit): owner, owner_reference, due_back_on, owner_pm_due_on, stands_in_for. On the
    locked row; refused for a device returned or kept. Each change in its history ("Stay changed").

    add_temporary_device's rules for each value given (_stay_values: the owner required, the reference a token, due back not before
    it arrived, the owner's PM date any day within ten years, stands_in_for a device of ours here, not retired, for a vendor loaner
    only; a blank due back, owner's PM date, or stands_in_for clears it). Its current stands_in_for sent back is no change, whatever
    that device's status now. Refused in words for a device of ours (kept or never temporary) and one returned to its owner. Returns
    the caller's device, read again."""
    today = today or timezone.localdate()
    if by is not None and not permissions.can_handle_temporary(by):
        raise PermissionDenied(TEMPORARY_PERMISSION)
    unknown = set(fields) - set(STAY_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    caller, fresh = asset, _locked_row(asset)
    refusal = _temporary_refusal(fresh)
    if refusal:
        raise ValidationError(refusal)
    values = _stay_values(fields, kind=fresh.ownership, arrived_on=fresh.arrived_on, today=today, asset=fresh)
    changed = False
    for name, value in values.items():
        if name == "stands_in_for":
            pk = value.pk if value is not None else None
            if pk != fresh.stands_in_for_id:
                fresh.stands_in_for_id, changed = pk, True
        elif getattr(fresh, name) != value:
            setattr(fresh, name, value)
            changed = True
    if changed:
        fresh._change_reason = STAY_REASON
        if by is not None:
            fresh._history_user = by
        fresh.save()
    caller.refresh_from_db()
    return caller


@transaction.atomic
def return_to_owner(asset, *, cleaning, data, on=None, by=None, today=None) -> Asset:
    """A temporary device goes back to its owner (Equipment Edit): status retired (read "Returned to owner"), returned_on (`on`,
    from arrived_on to today, default today), `cleaning` (ReturnCleaning) and `data` (ReturnData) recorded. Refused while held for an
    incident (slice 28) and while other work is open (named in words, as retiring refuses it: set_status's retire body, shared); an
    open incoming inspection is cancelled. History "Returned to its owner".

    On the device's row as it is, locked (_locked_row: its open work orders, then its row). Refused in words for a device of ours
    (kept or never temporary: retire it instead), one already returned, and one missing (found first: the return records how it was
    cleaned and what happened to its data, which nobody can say of a unit not in hand). Review fix: a unit lost for good (stolen, settled
    with its owner) leaves as missing with cleaning NOT_IN_HAND, the one choice for a missing unit and refused for one in hand, so a lost
    rental never stays on the inventory and in the figures. `on` before the day it arrived or after today
    is refused, keyed "on"; a cleaning or data choice not listed is keyed by its name. The history row is dated now (returned_on holds
    the day: a backdated row would sort before changes made since). Returns the caller's device, read again."""
    today = today or timezone.localdate()
    if by is not None and not permissions.can_handle_temporary(by):
        raise PermissionDenied(TEMPORARY_PERMISSION)
    errors = {}
    if cleaning not in ReturnCleaning.values:
        errors["cleaning"] = "Say how it was cleaned before it left."
    if data not in ReturnData.values:
        errors["data"] = "Say what was done about patient data on it."
    on = on or today
    if on > today:
        errors["on"] = "It cannot go back on a day after today."
    if errors:
        raise ValidationError(errors)
    caller, fresh = asset, _locked_row(asset)
    refusal = _temporary_refusal(fresh)
    if refusal:
        raise ValidationError(refusal)
    if fresh.incident_hold:  # slice 28: only its incident releases it
        raise ValidationError(held_message(fresh))
    if fresh.status == AssetStatus.MISSING and cleaning != ReturnCleaning.NOT_IN_HAND:
        raise ValidationError({"cleaning": f"{fresh.tag} is missing: mark it found before it goes back to its owner, or return it as not in "
                                           "hand (lost, settled with its owner)."})
    if fresh.status != AssetStatus.MISSING and cleaning == ReturnCleaning.NOT_IN_HAND:
        raise ValidationError({"cleaning": f"{fresh.tag} is here: say how it was cleaned before it left."})
    if fresh.arrived_on and on < fresh.arrived_on:
        raise ValidationError({"on": f"It cannot go back before it arrived ({_day(fresh.arrived_on)})."})
    fresh.returned_on, fresh.return_cleaning, fresh.return_data = on, cleaning, data
    if by is not None:
        fresh._history_user = by
    _retire(fresh, by=by, note=RETURNED_REASON, today=today)
    caller.refresh_from_db()
    return caller


def _price(value):
    """keep_temporary_device's acquisition cost as a Decimal, or None when none was given."""
    if value is None or value == "":
        return None
    try:
        cost = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValidationError({"acquisition_cost": "The acquisition cost is a number, 0 or more."}) from None
    if not cost.is_finite() or cost < 0:
        raise ValidationError({"acquisition_cost": "The acquisition cost is a number, 0 or more."})
    return cost


@transaction.atomic
def keep_temporary_device(asset, *, acquisition_cost, next_pm_on=None, warranty_end=None, by=None, today=None) -> Asset:
    """The facility keeps (buys) a temporary device (Equipment Approve): on site, its incoming inspection passed (or EXISTING), not
    held, not missing. It becomes ours: kept_on today, acquisition cost (required), next PM (default today: the facility's acceptance
    PM), warranty end, support recomputed. History "Kept by the facility".

    The acquisition cost is what the facility paid (0 is accepted: a demo unit left free); none is refused, keyed acquisition_cost.
    The next PM is today or later (keyed next_pm_on: a day before would count it overdue for a time it was not ours) and within ten
    years; the warranty end not before it arrived (installed_on, the day it arrived). On the device's row as it is, locked
    (_locked_row). Refused in words for a device of ours already and one returned; held for an incident; missing; and waiting for its
    incoming inspection (put in use before it or not: keep it once it passes). Its stay stays on the device as history (owner,
    reference, arrived, due back, the owner's PM date), but for stands_in_for, which is cleared: a device of ours stands in for
    nothing. Support follows Asset.save (in-house: no contract yet). Returns the caller's device, read again."""
    from apps.workorders import inspections

    today = today or timezone.localdate()
    if by is not None and not permissions.can_keep_temporary(by):
        raise PermissionDenied(KEEP_PERMISSION)
    cost = _price(acquisition_cost)
    if cost is None:
        raise ValidationError({"acquisition_cost": "Enter what the facility paid for it (0 if nothing)."})
    next_pm_on = next_pm_on or today
    if next_pm_on < today:
        raise ValidationError({"next_pm_on": "Its first PM as ours cannot be before today."})
    _check_next_pm(next_pm_on, today)
    caller, fresh = asset, _locked_row(asset)
    refusal = _temporary_refusal(fresh)
    if refusal:
        raise ValidationError(refusal)
    if fresh.incident_hold:
        raise ValidationError(held_message(fresh))
    if fresh.status == AssetStatus.MISSING:
        raise ValidationError(f"{fresh.tag} is missing: mark it found before keeping it.")
    if fresh.awaiting_inspection:
        wo = inspections.open_inspection(fresh)
        raise ValidationError(f"{fresh.tag} has not passed its incoming inspection{f' ({wo.number})' if wo else ''}: keep it once it "
                              "passes.")
    if warranty_end is not None:
        _check_dates(installed_on=fresh.installed_on, warranty_end=warranty_end, today=today)
        fresh.warranty_end = warranty_end
    fresh.ownership, fresh.kept_on, fresh.acquisition_cost, fresh.next_pm_on = Ownership.OWNED, today, cost, next_pm_on
    fresh.stands_in_for = None
    fresh._change_reason = KEPT_REASON
    if by is not None:
        fresh._history_user = by
    fresh.save()  # Asset.save: support follows the contract (none yet: in-house), no longer the owner
    caller.refresh_from_db()
    return caller
