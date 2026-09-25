from dataclasses import dataclass
from datetime import date

from django.conf import settings

from .models import Credential, Scope, Technician


@dataclass
class Qualification:
    ok: bool
    via: str = ""           # e.g. "model: Hamilton-G5"
    expiring: bool = False  # every matching credential expires within the warning window
    expires_on: date | None = None
    expired_only: bool = False  # had matching credentials, all expired


def _matches(cred: Credential, asset) -> bool:
    dm = asset.device_model
    return ((cred.scope == Scope.MANUFACTURER and cred.value == dm.manufacturer)
            or (cred.scope == Scope.MODEL and cred.value == dm.model)
            or (cred.scope == Scope.CATEGORY and cred.value == dm.category))


def qualification(technician: Technician, asset, as_of: date | None = None) -> Qualification:
    as_of = as_of or date.today()
    creds = [c for c in technician.credentials.all() if c.status == Credential.Status.ACTIVE and _matches(c, asset)]
    active = [c for c in creds if not c.expires_on or c.expires_on >= as_of]
    if not active:
        return Qualification(ok=False, expired_only=bool(creds))
    warn = settings.CREDENTIAL_EXPIRY_WARNING_DAYS
    expiring = all(c.expires_on and (c.expires_on - as_of).days <= warn for c in active)
    rank = {Scope.MODEL: 0, Scope.MANUFACTURER: 1, Scope.CATEGORY: 2}
    best = sorted(active, key=lambda c: rank[c.scope])[0]
    soonest = min((c.expires_on for c in active if c.expires_on), default=None)
    return Qualification(ok=True, via=f"{best.get_scope_display().lower()}: {best.value}", expiring=expiring, expires_on=soonest if expiring else None)


def qualified_technicians(asset, as_of: date | None = None) -> list[tuple[Technician, Qualification]]:
    """Active technicians credentialed for this device, best match first."""
    out = []
    for t in Technician.objects.filter(is_active=True).prefetch_related("credentials"):
        q = qualification(t, asset, as_of)
        if q.ok:
            out.append((t, q))
    out.sort(key=lambda tq: (tq[1].expiring, tq[0].name))
    return out


def ranked_technicians(asset, as_of: date | None = None) -> list[tuple[Technician, Qualification]]:
    """Every active technician for an assignment dropdown: credentialed first, then expiring, then not credentialed."""
    qualified = qualified_technicians(asset, as_of)
    ids = {t.id for t, _ in qualified}
    rest = [(t, qualification(t, asset, as_of)) for t in Technician.objects.filter(is_active=True).prefetch_related("credentials") if t.id not in ids]
    return qualified + sorted(rest, key=lambda tq: tq[0].name)


def coverage_by_category(as_of: date | None = None) -> list[dict]:
    """For each category in the catalog: active device count and which technicians hold a category-level credential."""
    from apps.equipment.models import Asset, DeviceModel

    as_of = as_of or date.today()
    techs = list(Technician.objects.filter(is_active=True).prefetch_related("credentials"))
    rows = []
    for category in DeviceModel.objects.values_list("category", flat=True).distinct():
        full = []
        for t in techs:
            if any(c.scope == Scope.CATEGORY and c.value == category and c.status == Credential.Status.ACTIVE and (not c.expires_on or c.expires_on >= as_of)
                   for c in t.credentials.all()):
                full.append(t)
        n = Asset.objects.filter(device_model__category=category, status__in=Asset.ACTIVE_STATUSES).count()
        rows.append({"category": category, "devices": n, "technicians": full, "status": "none" if not full else "single" if len(full) == 1 else "covered"})
    rows.sort(key=lambda r: (len(r["technicians"]), -r["devices"]))
    return rows
