"""
Fleet queries for the Equipment screen and the Overview fleet strip.

Every device lands in exactly one fleet bucket, most urgent state first (matches the mock's `bucketOf`):
retired, out of service, in repair, open recall, PM overdue, PM due within 30 days, compliant.
The bucket is a SQL annotation so the strip counts and the `?bucket=` filter always agree.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import models
from django.db.models import Case, Count, Exists, F, OuterRef, Q, Value, When

from apps.recalls.models import AlertMatch

from .models import Asset, AssetStatus, RiskClass, SupportType

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
