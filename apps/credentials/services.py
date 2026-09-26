from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.pm.dates import add_months

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
    """For each category in the catalog: active device count, technicians with a category-level credential (`technicians`),
    technicians covering only some models or manufacturers in it (`partial`), and whether an active device is on an OEM contract."""
    from apps.equipment.models import Asset, DeviceModel, SupportType

    as_of = as_of or date.today()
    techs = list(Technician.objects.filter(is_active=True).prefetch_related("credentials"))
    models = list(DeviceModel.objects.values_list("category", "manufacturer", "model"))
    rows = []
    for category in DeviceModel.objects.values_list("category", flat=True).distinct():
        mfrs = {m for c, m, _ in models if c == category}
        names = {n for c, _, n in models if c == category}
        full, partial = [], []
        for t in techs:
            live = [c for c in t.credentials.all() if c.status == Credential.Status.ACTIVE and (not c.expires_on or c.expires_on >= as_of)]
            if any(c.scope == Scope.CATEGORY and c.value == category for c in live):
                full.append(t)
                continue
            values = [c.value for c in live if (c.scope == Scope.MODEL and c.value in names) or (c.scope == Scope.MANUFACTURER and c.value in mfrs)]
            if values:
                partial.append((t, values))
        active = Asset.objects.filter(device_model__category=category, status__in=Asset.ACTIVE_STATUSES)
        rows.append({"category": category, "devices": active.count(), "technicians": full, "partial": partial,
                     "vendor_contract": active.filter(support_type=SupportType.OEM_CONTRACT).exists(),
                     "status": "none" if not full else "single" if len(full) == 1 else "covered"})
    rows.sort(key=lambda r: (len(r["technicians"]), -r["devices"]))
    return rows


# --- credential lifecycle (the Users and access tab) ---------------------------------------------

RENEWAL_MONTHS = 24
SIGN_OFF_SOURCE = "In-house sign-off"
# Short scope words for the UI and toasts ("category credential for Ventilators added"), as the mock words them.
SCOPE_SHORT = {Scope.CATEGORY: "Category", Scope.MANUFACTURER: "Manufacturer", Scope.MODEL: "Model"}


def add_credential(technician: Technician, *, scope: str, value: str, source: str = "", status: str = Credential.Status.ACTIVE,
                   issued_on: date | None = None, expires_on: date | None = None) -> Credential:
    if scope not in Scope.values:
        raise ValidationError("Choose what the credential covers: a category, a manufacturer, or a model.")
    value = (value or "").strip()
    if not value:
        raise ValidationError("Choose what the credential covers.")
    if status not in Credential.Status.values:
        raise ValidationError("Status must be Active or In training.")
    if issued_on and expires_on and expires_on < issued_on:
        raise ValidationError("The expiry date cannot be before the issue date.")
    if technician.credentials.filter(scope=scope, value=value).exists():
        raise ValidationError(f"{technician.name} already has a {SCOPE_SHORT[scope].lower()} credential for {value}.")
    return Credential.objects.create(technician=technician, scope=scope, value=value, source=source.strip(), status=status,
                                     issued_on=issued_on, expires_on=expires_on)


def renew_credential(credential: Credential, months: int = RENEWAL_MONTHS, today: date | None = None) -> Credential:
    """Push the expiry out `months` from today. Credentials without an expiry have nothing to renew; in-training ones are signed off instead."""
    if credential.status == Credential.Status.IN_TRAINING:
        raise ValidationError("Sign off the credential first; a credential in training cannot be renewed.")
    if not credential.expires_on:
        raise ValidationError("This credential has no expiry, so there is nothing to renew.")
    credential.expires_on = add_months(today or date.today(), months)
    credential.save(update_fields=["expires_on", "updated_at"])
    return credential


def sign_off_credential(credential: Credential, today: date | None = None) -> Credential:
    """In-house sign-off: the credential becomes active as of today."""
    if credential.status != Credential.Status.IN_TRAINING:
        raise ValidationError("Only a credential in training can be signed off.")
    credential.status = Credential.Status.ACTIVE
    credential.source = SIGN_OFF_SOURCE
    credential.issued_on = today or date.today()
    credential.save(update_fields=["status", "source", "issued_on", "updated_at"])
    return credential


def remove_credential(credential: Credential) -> None:
    credential.delete()


def credential_state(credential: Credential, today: date | None = None) -> dict:
    """Status chip for a credential row: key, label, and chip css, matching the mock's credState."""
    today = today or date.today()
    if credential.status == Credential.Status.IN_TRAINING:
        return {"key": "in_training", "label": "In training", "css": "info"}
    exp = credential.expires_on
    if exp and exp < today:
        return {"key": "expired", "label": f"Expired {exp:%b %Y}", "css": "crit"}
    if exp and (exp - today).days <= settings.CREDENTIAL_EXPIRY_WARNING_DAYS:
        return {"key": "expiring", "label": f"Expires in {(exp - today).days} d", "css": "warn"}
    return {"key": "ok", "label": f"Through {exp:%b %Y}" if exp else "No expiry", "css": "ok"}


def credential_options() -> list[tuple[str, list[tuple[str, str]]]]:
    """The "Covers" choices grouped as the mock does: categories, manufacturers, then every model. Values are "scope|value"."""
    from apps.equipment.models import DeviceModel

    models = list(DeviceModel.objects.values_list("category", "manufacturer", "model"))
    categories = sorted({c for c, _, _ in models})
    mfrs = sorted({m for _, m, _ in models})
    by_model = sorted({(n, m) for _, m, n in models})
    return [
        (SCOPE_SHORT[Scope.CATEGORY], [(f"{Scope.CATEGORY}|{c}", c) for c in categories]),
        (SCOPE_SHORT[Scope.MANUFACTURER], [(f"{Scope.MANUFACTURER}|{m}", m) for m in mfrs]),
        (SCOPE_SHORT[Scope.MODEL], [(f"{Scope.MODEL}|{n}", f"{m} {n}") for n, m in by_model]),
    ]
