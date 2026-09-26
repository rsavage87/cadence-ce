"""
Contract lifecycle and the Contracts screen queries. Views call these; they never edit contract or
asset fields directly. Coverage follows the mock: a device is on at most one contract, and its
support type follows that contract's type (Asset.save keeps it in sync).
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Sum

from apps.equipment.models import Asset, AssetStatus, DeviceModel
from apps.pm.dates import add_months

from .models import Contract, ContractType, Coverage

EDITABLE = ("reference", "vendor", "type", "coverage", "start_on", "end_on", "annual_cost", "notes")
RENEW_MONTHS = 12
LIST_CAP = 40  # the drawer lists at most this many covered devices; the filter finds the rest
STATUS_KEYS = ("active", "ending", "expired")


def fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def fmt_short(d: date) -> str:
    return f"{d:%b} {d.day}"


# --- lifecycle ------------------------------------------------------------------------------------

def _validate(contract: Contract):
    contract.reference = (contract.reference or "").strip()
    contract.vendor = (contract.vendor or "").strip()
    contract.notes = (contract.notes or "").strip()
    errors = {}
    if not contract.reference:
        errors["reference"] = "Reference is required."
    elif Contract.objects.filter(reference__iexact=contract.reference).exclude(pk=contract.pk).exists():
        errors["reference"] = f"{contract.reference} is already used by another contract."
    if not contract.vendor:
        errors["vendor"] = "Vendor is required."
    if contract.type not in ContractType.values:
        errors["type"] = "Choose OEM or third-party."
    if contract.coverage not in Coverage.values:
        errors["coverage"] = "Choose a coverage level."
    if not contract.start_on or not contract.end_on:
        errors["end_on"] = "Start and end dates are required."
    elif contract.end_on < contract.start_on:
        errors["end_on"] = "The end date must be on or after the start date."
    if contract.annual_cost is None or contract.annual_cost < 0:
        errors["annual_cost"] = "Annual cost cannot be negative."
    if errors:
        raise ValidationError(errors)


def _save(contract: Contract, by=None):
    _validate(contract)
    if by is not None:
        contract._history_user = by
    contract.save()
    return contract


def create_contract(by=None, **fields) -> Contract:
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValidationError({k: "Unknown field." for k in unknown})
    return _save(Contract(**fields), by=by)


@transaction.atomic
def update_contract(contract: Contract, by=None, **fields) -> Contract:
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValidationError({k: "Unknown field." for k in unknown})
    old_type = contract.type
    for k, v in fields.items():
        setattr(contract, k, v)
    _save(contract, by=by)
    if contract.type != old_type:
        # Support type lives on the asset; re-save so it follows the new contract type.
        for asset in contract.assets.all():
            asset.save(update_fields=["support_type", "updated_at"])
    return contract


def renew_contract(contract: Contract, by=None, today: date | None = None) -> Contract:
    """Twelve more months from the current end, or from today if the contract already lapsed."""
    today = today or date.today()
    contract.end_on = add_months(max(contract.end_on, today), RENEW_MONTHS)
    if contract.start_on > today:
        contract.start_on = today
    return _save(contract, by=by)


@transaction.atomic
def delete_contract(contract: Contract) -> int:
    """Detach every device first so their support type resets; returns how many covered (non-retired) devices lost coverage."""
    assets = list(contract.assets.all())
    for asset in assets:
        contract.remove_asset(asset)
    contract.delete()
    return sum(1 for a in assets if a.status != AssetStatus.RETIRED)


def add_asset(contract: Contract, asset: Asset):
    """Puts one device on the contract. Returns the contract it left, or None if it was in-house or already here."""
    if asset.status == AssetStatus.RETIRED:
        raise ValidationError(f"{asset.tag} is retired and cannot be put on a contract.")
    previous = asset.contract if asset.contract_id and asset.contract_id != contract.id else None
    contract.add_assets([asset])
    return previous


def add_model(contract: Contract, device_model: DeviceModel) -> int:
    """Every active device of the model that is not already on this contract; returns how many moved."""
    assets = Asset.objects.filter(device_model=device_model, status__in=Asset.ACTIVE_STATUSES).exclude(contract=contract)
    return contract.add_assets(list(assets))


def remove_asset(contract: Contract, asset: Asset):
    if asset.contract_id != contract.id:
        raise ValidationError(f"{asset.tag} is not on {contract.reference}.")
    contract.remove_asset(asset)


# --- status and coverage ---------------------------------------------------------------------------

def contract_status(contract: Contract, today: date | None = None) -> dict:
    """The mock's ctStatus: key drives the filter, label and css the chip."""
    today = today or date.today()
    days = (contract.end_on - today).days
    if days < 0:
        return {"key": "expired", "label": f"Expired {fmt_short(contract.end_on)}", "css": "crit"}
    if days <= settings.CONTRACT_EXPIRY_WARNING_DAYS:
        return {"key": "ending", "label": f"Ends in {days} d", "css": "warn"}
    if contract.start_on > today:
        return {"key": "active", "label": f"Starts {fmt_short(contract.start_on)}", "css": "info"}
    return {"key": "active", "label": "Active", "css": "ok"}


def covered_models_by_contract(contracts) -> dict:
    """{contract id: [("Mfr Model", count), ...]} for covered (non-retired) devices, largest group first."""
    ids = [c.id for c in contracts]
    rows = (Asset.objects.filter(contract_id__in=ids).exclude(status=AssetStatus.RETIRED).order_by()
            .values("contract_id", "device_model__manufacturer", "device_model__model").annotate(n=Count("id"))
            .order_by("contract_id", "-n", "device_model__manufacturer", "device_model__model"))
    out = {i: [] for i in ids}
    for r in rows:
        out[r["contract_id"]].append((f"{r['device_model__manufacturer']} {r['device_model__model']}", r["n"]))
    return out


def covered_models(contract: Contract) -> list[tuple[str, int]]:
    return covered_models_by_contract([contract])[contract.id]


def covered_devices(contract: Contract, q: str = "") -> dict:
    """The drawer's device list: by model then tag, optionally filtered, capped at LIST_CAP."""
    qs = contract.covered_assets().select_related("device_model", "department").order_by("device_model__model", "tag")
    total = qs.count()
    q = (q or "").strip()
    if q:
        qs = qs.filter(Q(tag__icontains=q) | Q(device_model__model__icontains=q) | Q(department__name__icontains=q)
                       | Q(device_model__description__icontains=q))
    matched = qs.count() if q else total
    return {"devices": list(qs[:LIST_CAP]), "matched": matched, "total": total, "q": q, "cap": LIST_CAP}


def model_options(contract: Contract):
    """Device models with active devices not on this contract, annotated with that count (the drawer's "Add all" select)."""
    elsewhere = Q(assets__status__in=Asset.ACTIVE_STATUSES) & (Q(assets__contract__isnull=True) | ~Q(assets__contract=contract))
    return DeviceModel.objects.annotate(n=Count("assets", filter=elsewhere)).filter(n__gt=0)


def pick_devices(contract: Contract, q: str, limit: int = 6):
    """Device picker for the drawer: active devices by tag, serial, or model that are not already on this contract."""
    q = (q or "").strip()
    if len(q) < 2:
        return Asset.objects.none()
    return (Asset.objects.exclude(status=AssetStatus.RETIRED).exclude(contract=contract).select_related("device_model", "department", "contract")
            .filter(Q(tag__icontains=q) | Q(serial__icontains=q) | Q(device_model__model__icontains=q) | Q(device_model__description__icontains=q))[:limit])


# --- the screen --------------------------------------------------------------------------------------

def contracts_summary(today: date | None = None) -> dict:
    today = today or date.today()
    fleet = Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES)
    agg = fleet.aggregate(n=Count("id"), value=Sum("acquisition_cost"))
    fleet_n, fleet_value = agg["n"], float(agg["value"] or 0)
    annual = Contract.objects.filter(end_on__gte=today).aggregate(s=Sum("annual_cost"))["s"] or Decimal(0)
    covered = fleet.filter(contract__end_on__gte=today).count()
    expired = Contract.objects.filter(end_on__lt=today).count()
    ending = Contract.objects.filter(end_on__gte=today, end_on__lte=today + timedelta(days=settings.CONTRACT_EXPIRY_WARNING_DAYS)).count()
    return {
        "contracts": Contract.objects.count(), "active_fleet": fleet_n,
        "annual": annual, "annual_pct": float(annual) / fleet_value * 100 if fleet_value else 0.0,
        "covered": covered, "covered_pct": covered / fleet_n * 100 if fleet_n else 0.0,
        "expired": expired, "ending": ending, "renewals": expired + ending, "warn_days": settings.CONTRACT_EXPIRY_WARNING_DAYS,
        "uncovered": fleet.filter(contract__end_on__lt=today).count(),
    }


@dataclass
class ContractFilters:
    q: str = ""
    type: str = ""
    status: str = ""


def filter_contracts(f: ContractFilters, today: date | None = None):
    """The Contracts table, annotated with `devices` (covered count). Status is compared in SQL so it matches contract_status."""
    today = today or date.today()
    qs = Contract.objects.annotate(devices=Count("assets", filter=~Q(assets__status=AssetStatus.RETIRED)))
    if f.type:
        qs = qs.filter(type=f.type)
    warn_until = today + timedelta(days=settings.CONTRACT_EXPIRY_WARNING_DAYS)
    if f.status == "expired":
        qs = qs.filter(end_on__lt=today)
    elif f.status == "ending":
        qs = qs.filter(end_on__gte=today, end_on__lte=warn_until)
    elif f.status == "active":
        qs = qs.filter(end_on__gt=warn_until)
    if f.q:
        q = f.q.strip()
        coverages = [v for v, label in Coverage.choices if q.lower() in label.lower()]
        on_model = Asset.objects.filter(contract_id=OuterRef("pk")).exclude(status=AssetStatus.RETIRED).filter(
            Q(device_model__manufacturer__icontains=q) | Q(device_model__model__icontains=q))
        qs = qs.filter(Q(reference__icontains=q) | Q(vendor__icontains=q) | Q(coverage__in=coverages) | Exists(on_model))
    return qs.order_by("end_on", "reference")
