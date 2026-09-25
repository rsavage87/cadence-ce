"""
Import an equipment inventory from a CSV export of another CMMS (MediMizer, AIMS, TMS, EQ2, Nuvolo, or a spreadsheet).

    python manage.py import_assets --tenant riverside inventory.csv [--dry-run]

Columns are matched by name, case-insensitively, using the aliases below. Add aliases as you meet new exports.
Unknown device models and departments are created on the fly; existing tags are updated, not duplicated.
"""
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

ALIASES = {
    "tag": ["asset tag", "tag", "control number", "control #", "equipment id", "asset id", "asset number", "ce number", "id"],
    "serial": ["serial", "serial number", "serial #", "sn"],
    "manufacturer": ["manufacturer", "mfr", "make", "vendor"],
    "model": ["model", "model number", "model #"],
    "description": ["description", "device", "device name", "nomenclature", "equipment type", "name"],
    "category": ["category", "product category", "device category", "class", "type"],
    "risk": ["risk", "risk class", "risk level", "em class"],
    "department": ["department", "dept", "location", "unit", "cost center name", "owner"],
    "room": ["room", "bed", "sub location", "sublocation"],
    "status": ["status", "asset status", "condition status"],
    "installed": ["installed", "install date", "installed on", "acceptance date", "in service date", "purchase date"],
    "cost": ["cost", "acquisition cost", "purchase cost", "purchase price", "value"],
    "pm_interval": ["pm interval", "pm frequency", "interval", "pm months"],
    "last_pm": ["last pm", "last pm date", "last inspection"],
    "next_pm": ["next pm", "next pm date", "next due", "due date"],
    "warranty": ["warranty", "warranty end", "warranty expiration"],
}

STATUS_MAP = {"in service": AssetStatus.IN_SERVICE, "active": AssetStatus.IN_SERVICE, "in use": AssetStatus.IN_SERVICE, "in repair": AssetStatus.IN_REPAIR,
              "out of service": AssetStatus.OUT_OF_SERVICE, "oos": AssetStatus.OUT_OF_SERVICE, "on loan": AssetStatus.ON_LOAN, "loaner": AssetStatus.ON_LOAN,
              "missing": AssetStatus.MISSING, "retired": AssetStatus.RETIRED, "disposed": AssetStatus.RETIRED, "inactive": AssetStatus.RETIRED}
RISK_MAP = {"life support": RiskClass.LIFE_SUPPORT, "life-support": RiskClass.LIFE_SUPPORT, "critical": RiskClass.LIFE_SUPPORT, "high": RiskClass.HIGH,
            "medium": RiskClass.MEDIUM, "moderate": RiskClass.MEDIUM, "low": RiskClass.LOW}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%Y/%m/%d")


def parse_date(s):
    s = (s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(s):
    s = (s or "").replace("$", "").replace(",", "").strip()
    try:
        return Decimal(s) if s else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def build_column_map(header):
    lower = {h.strip().lower(): h for h in header}
    mapping = {}
    for field, names in ALIASES.items():
        for name in names:
            if name in lower:
                mapping[field] = lower[name]
                break
    return mapping


class Command(BaseCommand):
    help = "Import assets from a CSV export."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--encoding", default="utf-8-sig")

    def handle(self, *args, **opts):
        tenant = Tenant.objects.filter(slug=opts["tenant"]).first()
        if tenant is None:
            raise CommandError(f"No tenant with slug {opts['tenant']}")
        with open(opts["path"], newline="", encoding=opts["encoding"]) as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            raise CommandError("The file has no rows.")
        cmap = build_column_map(rows[0].keys())
        for required in ("tag", "manufacturer", "model"):
            if required not in cmap:
                raise CommandError(f"Could not find a column for '{required}'. Columns seen: {', '.join(rows[0].keys())}")
        self.stdout.write("Column mapping: " + ", ".join(f"{k} <- {v}" for k, v in cmap.items()))
        get = lambda row, f: (row.get(cmap[f], "") if f in cmap else "").strip()  # noqa: E731

        created = updated = skipped = 0
        with tenant_context(tenant), transaction.atomic():
            for row in rows:
                tag = get(row, "tag").upper()
                if not tag:
                    skipped += 1
                    continue
                dept, _ = Department.objects.get_or_create(name=get(row, "department") or "Unassigned", defaults={"tenant": tenant})
                dm, _ = DeviceModel.objects.get_or_create(
                    manufacturer=get(row, "manufacturer") or "Unknown", model=get(row, "model") or "Unknown",
                    defaults={"tenant": tenant, "description": get(row, "description") or get(row, "model") or "Device",
                              "category": get(row, "category") or "Uncategorized",
                              "risk_class": RISK_MAP.get(get(row, "risk").lower(), RiskClass.MEDIUM),
                              "oem_pm_interval_months": int(get(row, "pm_interval") or 12) if get(row, "pm_interval").isdigit() else 12})
                fields = {
                    "device_model": dm, "department": dept, "serial": get(row, "serial"), "room": get(row, "room"),
                    "status": STATUS_MAP.get(get(row, "status").lower(), AssetStatus.IN_SERVICE),
                    "installed_on": parse_date(get(row, "installed")), "acquisition_cost": parse_money(get(row, "cost")),
                    "last_pm_on": parse_date(get(row, "last_pm")), "next_pm_on": parse_date(get(row, "next_pm")),
                    "warranty_end": parse_date(get(row, "warranty")),
                }
                asset = Asset.objects.filter(tag=tag).first()
                if asset:
                    for k, v in fields.items():
                        if v not in (None, "", Decimal("0")) or k in ("device_model", "department", "status"):
                            setattr(asset, k, v)
                    asset.save()
                    updated += 1
                else:
                    Asset.objects.create(tenant=tenant, tag=tag, **fields)
                    created += 1
            if opts["dry_run"]:
                transaction.set_rollback(True)
        self.stdout.write(self.style.SUCCESS(f"{'Dry run: ' if opts['dry_run'] else ''}{created} created, {updated} updated, {skipped} skipped (no tag)"))
