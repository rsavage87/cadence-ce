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
from .models import TAG_VALIDATOR, AddedAs, Asset, AssetStatus, Department, DeviceModel, RiskClass, SupportType

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
                 inspection_due: date | None = None) -> Asset:
    """Add a device. Its first PM is `next_pm_on` when given, else first_pm_due(); its acquisition cost defaults to the model's list
    cost. Tags are unique in the facility in any letter case and can never change afterwards (they are on the sticker and in URLs).
    `added_on` dates the history's "added" row on an earlier day: the importer (apps.imports.kinds.devices) adds a device retired
    years ago in the previous system with both its rows on the day it was retired (set_status's `changed_on`), so its history reads
    in order. `added_as` (slice 25): new to the facility (the survey binder then looks for its incoming inspection), already in use
    here (entered after the fact), or imported (the importer's). `incoming_inspection` (slice 26): "" adds the device in the status
    given (the API's and the importer's way); "waiting" adds a new device out of service, awaiting its incoming inspection, with no
    next PM (its PM clock starts when it passes) and an Incoming inspection work order opened with it (on the added day, due
    `inspection_due` or INSPECTION_DUE_DAYS later), unassigned: the caller assigns it (Add device: take or assign)."""
    today = today or timezone.localdate()
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
    asset = Asset(tag=tag, device_model=device_model, department=department, serial=_clean_text(serial, 80), room=_clean_text(room, 40),
                  installed_on=installed_on, acquisition_cost=acquisition_cost, warranty_end=warranty_end, condition=condition,
                  last_pm_on=last_pm_on, status=status, notes=(notes or "").strip(), added_as=added_as, awaiting_inspection=waiting,
                  next_pm_on=None if waiting else (next_pm_on or first_pm_due(device_model, installed_on=installed_on, last_pm_on=last_pm_on,
                                                                              today=today)))
    asset._change_reason = "Added, waiting for its incoming inspection" if waiting else "Added"
    _save_on(asset, added_on)
    if waiting:
        from apps.workorders import inspections

        inspections.open_for(asset, by=by, today=today, opened_on=added_on or today, due_on=inspection_due)
    return asset


def status_label(asset) -> str:
    """The device's status as every screen, CSV, and the API words it (slice 26): "Awaiting inspection" for a device out of service
    waiting for its incoming inspection, else its status."""
    if asset.awaiting_inspection and asset.status == AssetStatus.OUT_OF_SERVICE:
        return AWAITING_LABEL
    return asset.get_status_display()


EDITABLE_FIELDS = ("serial", "device_model", "department", "room", "installed_on", "acquisition_cost", "warranty_end", "condition", "next_pm_on", "notes")


@transaction.atomic
def update_asset(asset: Asset, *, by=None, today: date | None = None, imported_last_pm=_UNSET, **fields) -> Asset:
    """Change a device's details. Not its tag (on the sticker and in URLs), status (set_status), or contract (the contracts screen).
    A new model does not move the next PM by itself; change next_pm_on alongside it when the interval differs.
    The last PM is not an editable detail (completed PM work orders set it): `imported_last_pm` is the importer's alone, the last PM
    the previous system recorded (apps.imports.kinds.devices, on a re-import), with create_asset's rules for it."""
    today = today or timezone.localdate()
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    # The install and warranty dates only when one of them changes: a stored pair that breaks the rule (imported data) must not
    # block an edit of the room or the notes. Before the imported last PM, which is measured from the install date: a refused
    # install date is named first, so the importer leaves it out and keeps the last PM, as it does for a new device.
    if any(f in fields and fields[f] != getattr(asset, f) for f in ("installed_on", "warranty_end")):
        _check_dates(**{f: fields.get(f, getattr(asset, f)) for f in ("installed_on", "warranty_end")}, today=today)
    if imported_last_pm is not _UNSET and imported_last_pm != asset.last_pm_on:
        _check_last_pm(imported_last_pm, installed_on=fields.get("installed_on", asset.installed_on), today=today)
        fields["last_pm_on"] = imported_last_pm
    if "device_model" in fields:
        _check_tenant(fields["device_model"], "device_model")
    if "department" in fields:
        _check_tenant(fields["department"], "department")
    if "next_pm_on" in fields and fields["next_pm_on"] is None and asset.status != AssetStatus.RETIRED:
        raise ValidationError({"next_pm_on": "A device in use needs a next PM date."})
    _check_next_pm(fields.get("next_pm_on"), today)
    # The last PM against the install date only when the install date changes, and on the field the form has: a device added
    # before this rule (import, the demo) may have an older PM on record, and must stay editable.
    new_install, last_pm = fields.get("installed_on"), fields.get("last_pm_on", asset.last_pm_on)
    if new_install and new_install != asset.installed_on and last_pm and last_pm < new_install:
        raise ValidationError({"installed_on": f"The install date cannot be after the last PM on record ({last_pm:%b %-d, %Y})."})
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
def set_status(asset: Asset, to_status: str, *, by=None, note: str = "", today: date | None = None, changed_on: date | None = None) -> Asset:
    """Move a device along STATUS_CHANGES. Retiring cancels its open PM work orders that can be cancelled and refuses while any
    other work is open (finish or cancel it first); the device leaves the PM schedule. Reinstating puts it back with a PM due today.
    `note` is kept in the device's history. `changed_on` dates the history row on an earlier day (not before the install date): the
    importer's retirement from the previous system, so the AEM evidence (apps.pm.aem) counts the device in use until then."""
    from apps.workorders.models import OPEN_STATUSES, WoStatus, WoType
    from apps.workorders.services import change_status

    today = today or timezone.localdate()
    if to_status not in AssetStatus.values:
        raise ValidationError("Choose a device status.")
    if to_status not in STATUS_CHANGES.get(asset.status, set()):
        raise ValidationError(f"{asset.tag} cannot go from {asset.get_status_display().lower()} to {AssetStatus(to_status).label.lower()}.")
    if changed_on is not None and changed_on > today:
        raise ValidationError({"changed_on": "The status cannot change on a day after today."})
    if changed_on is not None and asset.installed_on and changed_on < asset.installed_on:
        raise ValidationError({"changed_on": "The status cannot change before the device was installed."})
    if to_status == AssetStatus.RETIRED:
        open_wos = list(asset.work_orders.filter(status__in=OPEN_STATUSES).order_by("number"))
        blocking = [w for w in open_wos if not (w.type == WoType.PM and w.status in (WoStatus.OPEN, WoStatus.AWAITING_PARTS))]
        if blocking:
            numbers = ", ".join(w.number for w in blocking)
            raise ValidationError(f"{asset.tag} has open work: {numbers}. Complete it, or cancel it (an in-progress work order goes "
                                  f"back to open first), before retiring the device.")
        for w in open_wos:
            change_status(w, WoStatus.CANCELLED, by=by, note="Device retired", as_of=today)
        asset.next_pm_on = None
    elif asset.status == AssetStatus.RETIRED:
        asset.next_pm_on = today  # back in use: inspect it before anyone relies on it
    asset.status = to_status
    asset._change_reason = (note or "").strip()[:100] or f"Status: {AssetStatus(to_status).label}"
    _save_on(asset, changed_on)
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
