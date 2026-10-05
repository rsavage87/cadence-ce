"""
Import an equipment inventory from a CSV export of another CMMS (MediMizer, AIMS, TMS, EQ2, Nuvolo, or a spreadsheet).

    python manage.py import_assets --tenant riverside inventory.csv [--dry-run]

Columns are matched by name, case-insensitively, using the aliases below. Add aliases as you meet new exports.
Unknown device models and departments are created on the fly; existing tags are updated, not duplicated. A model already in the
catalog keeps its details (risk class, interval, the CMS mark): the file's model columns only describe a model it adds.

The optional "OEM schedule required" column marks a new model as equipment CMS keeps on the manufacturer's schedule (imaging,
radiologic, medical laser: never on AEM): yes/no, true/false, or 1/0, blank is no. A value it cannot read skips the row and says
so on stderr, as a value too long for its column does.
"""
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.equipment.models import TAG_VALIDATOR, Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.equipment.services import RESERVED_TAGS, first_pm_due, set_status
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
    "oem_schedule": ["oem schedule required", "manufacturer's schedule required", "manufacturer schedule required", "oem schedule",
                     "cms oem schedule", "aem excluded"],
}

STATUS_MAP = {"in service": AssetStatus.IN_SERVICE, "active": AssetStatus.IN_SERVICE, "in use": AssetStatus.IN_SERVICE, "in repair": AssetStatus.IN_REPAIR,
              "out of service": AssetStatus.OUT_OF_SERVICE, "oos": AssetStatus.OUT_OF_SERVICE, "on loan": AssetStatus.ON_LOAN, "loaner": AssetStatus.ON_LOAN,
              "missing": AssetStatus.MISSING, "retired": AssetStatus.RETIRED, "disposed": AssetStatus.RETIRED, "inactive": AssetStatus.RETIRED}
RISK_MAP = {"life support": RiskClass.LIFE_SUPPORT, "life-support": RiskClass.LIFE_SUPPORT, "critical": RiskClass.LIFE_SUPPORT, "high": RiskClass.HIGH,
            "medium": RiskClass.MEDIUM, "moderate": RiskClass.MEDIUM, "low": RiskClass.LOW}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%Y/%m/%d")
YES_NO = {"yes": True, "y": True, "true": True, "1": True, "no": False, "n": False, "false": False, "0": False, "": False}


# The columns' limits (PostgreSQL enforces them): a row with a longer value is skipped and reported, never cut silently.
TEXT_LIMITS = {"tag": Asset._meta.get_field("tag").max_length, "serial": Asset._meta.get_field("serial").max_length,
               "room": Asset._meta.get_field("room").max_length, "department": Department._meta.get_field("name").max_length,
               "manufacturer": DeviceModel._meta.get_field("manufacturer").max_length, "model": DeviceModel._meta.get_field("model").max_length,
               "description": DeviceModel._meta.get_field("description").max_length, "category": DeviceModel._meta.get_field("category").max_length}
COST_LIMIT = Decimal("10000000000")  # numeric(12, 2)


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
            today = timezone.localdate()  # the facility's today (tenant_context works in its time zone), one value for the whole file
            for row in rows:
                tag = get(row, "tag").upper()
                if not tag:
                    skipped += 1
                    continue
                try:
                    TAG_VALIDATOR(tag)
                except ValidationError:
                    self.stderr.write(f"Skipped {tag!r}: {TAG_VALIDATOR.message}")
                    skipped += 1
                    continue
                if tag.lower() in RESERVED_TAGS:  # the same tags Add device refuses: they collide with the app's own links
                    self.stderr.write(f"Skipped {tag!r}: it cannot be used as an asset tag.")
                    skipped += 1
                    continue
                # A value longer than its column, or a cost too large for it: PostgreSQL would refuse it and roll back the whole file
                # (SQLite stores it), so the row is reported and skipped instead.
                long = next(((f, limit) for f, limit in TEXT_LIMITS.items() if len(get(row, f)) > limit), None)
                if long:
                    self.stderr.write(f"Skipped {tag[:40]!r}: the {long[0]} is longer than {long[1]} characters.")
                    skipped += 1
                    continue
                cost = parse_money(get(row, "cost"))
                if cost is not None and abs(cost) >= COST_LIMIT:
                    self.stderr.write(f"Skipped {tag!r}: the cost {get(row, 'cost')!r} is too large.")
                    skipped += 1
                    continue
                oem_schedule = YES_NO.get(get(row, "oem_schedule").lower())
                if oem_schedule is None:
                    self.stderr.write(f"Skipped {tag!r}: OEM schedule required is {get(row, 'oem_schedule')[:40]!r}; use yes or no (blank is no).")
                    skipped += 1
                    continue
                interval = get(row, "pm_interval")
                dept, _ = Department.objects.get_or_create(name=get(row, "department") or "Unassigned", defaults={"tenant": tenant})
                dm, _ = DeviceModel.objects.get_or_create(
                    manufacturer=get(row, "manufacturer") or "Unknown", model=get(row, "model") or "Unknown",
                    defaults={"tenant": tenant, "description": get(row, "description") or get(row, "model") or "Device",
                              "category": get(row, "category") or "Uncategorized",
                              "risk_class": RISK_MAP.get(get(row, "risk").lower(), RiskClass.MEDIUM),
                              # 1 to 120 months, as on screen; anything else (or unreadable) takes the default
                              "oem_pm_interval_months": int(interval) if interval.isdecimal() and len(interval) <= 3 and 1 <= int(interval) <= 120
                              else 12,
                              "oem_schedule_required": oem_schedule})
                status = STATUS_MAP.get(get(row, "status").lower())  # None: no status in the file, or one it does not know
                fields = {
                    "device_model": dm, "department": dept, "serial": get(row, "serial"), "room": get(row, "room"),
                    "installed_on": parse_date(get(row, "installed")), "acquisition_cost": cost,
                    "last_pm_on": parse_date(get(row, "last_pm")), "next_pm_on": parse_date(get(row, "next_pm")),
                    "warranty_end": parse_date(get(row, "warranty")),
                }
                asset = Asset.objects.filter(tag__iexact=tag).first()  # tags are unique in any letter case (devices added on screen keep theirs)
                if asset:
                    for k, v in fields.items():
                        if v not in (None, "", Decimal("0")) or k in ("device_model", "department"):
                            setattr(asset, k, v)
                    asset.save()
                    # A status change goes through the drawer's rules (retiring cancels open PMs and needs the open work done,
                    # coming back from retired puts a PM due today); a file without a status leaves the device's alone.
                    if status and status != asset.status:
                        try:
                            set_status(asset, status, note="Imported", today=today)
                        except ValidationError as e:
                            self.stderr.write(f"Kept {asset.tag} {asset.get_status_display().lower()}: {e.messages[0]}")
                    updated += 1
                else:
                    status = status or AssetStatus.IN_SERVICE
                    if fields["next_pm_on"] is None and status != AssetStatus.RETIRED:
                        # As Add device does: one interval after the last PM or install, or today if that has passed
                        fields["next_pm_on"] = first_pm_due(dm, installed_on=fields["installed_on"], last_pm_on=fields["last_pm_on"], today=today)
                    Asset.objects.create(tenant=tenant, tag=tag, status=status, **fields)
                    created += 1
            if opts["dry_run"]:
                transaction.set_rollback(True)
        summary = f"{created} created, {updated} updated, {skipped} skipped (no tag, a tag with spaces or slashes, a value too long, or one it cannot read)"
        self.stdout.write(self.style.SUCCESS(f"{'Dry run: ' if opts['dry_run'] else ''}{summary}"))
