from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.pm.dates import add_months

from .models import Credential, Scope, Technician

# Where a credential comes from: the Technician credentials tab offers these, and the API takes only these.
SOURCES = ["OEM training", "In-house sign-off", "Third-party course", "Certification (CBET, CRES, CLES)"]


@dataclass
class Qualification:
    ok: bool
    via: str = ""           # e.g. "model: Hamilton-G5"
    expiring: bool = False  # every matching credential expires within the warning window
    expires_on: date | None = None
    expired_only: bool = False  # had matching credentials, all expired


# --- the one rule: does a credential qualify a technician for a device on a day ----------------------------------------------
# qualification() asks it of a technician's credentials as they are now; the survey binder's staff section (slice 25) asks it of each
# credential as it stood at the end of the day the work was done (credential_versions, standings_on). Both go through
# credential_standing, so the binder can never hold a technician to a different rule than the assignment screens do.
COVERS, EXPIRED, IN_TRAINING = "covers", "expired", "in_training"


def credential_names(cred, device_model) -> bool:
    """Whether `cred` (a Credential, or a historical version of one: anything with scope and value) names devices of `device_model`
    (anything with category, manufacturer, and model): its category, its manufacturer, or its model, exactly as written."""
    return ((cred.scope == Scope.MANUFACTURER and cred.value == device_model.manufacturer)
            or (cred.scope == Scope.MODEL and cred.value == device_model.model)
            or (cred.scope == Scope.CATEGORY and cred.value == device_model.category))


def credential_standing(cred, device_model, day: date) -> str:
    """What `cred` says about a device of `device_model` on `day`: COVERS (active, names it, and not expired that day: no expiry, or
    one on or after `day`), EXPIRED (active and names it, but expired before `day`), IN_TRAINING (names it, still in training), or ""
    (it names other devices)."""
    if not credential_names(cred, device_model):
        return ""
    if cred.status == Credential.Status.IN_TRAINING:
        return IN_TRAINING
    if cred.status != Credential.Status.ACTIVE:
        return ""
    return EXPIRED if cred.expires_on and cred.expires_on < day else COVERS


def credential_versions(technician_ids) -> dict:
    """Every credential the technicians ever had, as it stood over time, from Credential history: {technician id: [timeline, ...]},
    one timeline per credential, a list of (the facility's day the version was saved, the historical version, or None from the day
    it was removed), oldest first. One query, through apps.core.history._rows (this facility's rows only: the historical table's
    manager is not tenant-scoped); days through apps.core.days.local_day, so call it inside the facility."""
    from apps.core.days import local_day
    from apps.core.history import _rows

    rows = (_rows(Credential.history.model).filter(technician_id__in=list(technician_ids))
            .order_by("id", "history_date", "history_id"))
    timelines: dict = {}
    for rec in rows:
        timelines.setdefault(rec.technician_id, {}).setdefault(rec.id, []).append(
            (local_day(rec.history_date), None if rec.history_type == "-" else rec))
    return {tech_id: list(by_credential.values()) for tech_id, by_credential in timelines.items()}


def version_on(timeline: list, day: date):
    """The version of one credential (a timeline from credential_versions) in force at the end of `day`: the last one saved on or
    before it (None when that was its removal). Before its first save, its first version counts from its issued_on when that is on
    or before `day`: a credential typed in later than it was issued still covered the days since it was issued."""
    current, found = None, False
    for saved_on, version in timeline:
        if saved_on > day:
            break
        current, found = version, True
    if found:
        return current
    first = timeline[0][1] if timeline else None
    return first if first is not None and first.issued_on and first.issued_on <= day else None


def standings_on(timelines: list, device_model, day: date) -> list[tuple]:
    """(version, credential_standing) for each of a technician's credentials (their credential_versions list) that names a device of
    `device_model` at the end of `day`."""
    out = []
    for timeline in timelines:
        version = version_on(timeline, day)
        if version is not None:
            standing = credential_standing(version, device_model, day)
            if standing:
                out.append((version, standing))
    return out


def qualification(technician: Technician, asset, as_of: date | None = None) -> Qualification:
    as_of = as_of or timezone.localdate()
    standing = [(c, credential_standing(c, asset.device_model, as_of)) for c in technician.credentials.all()]
    creds = [c for c, s in standing if s in (COVERS, EXPIRED)]
    active = [c for c, s in standing if s == COVERS]
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

    as_of = as_of or timezone.localdate()
    techs = list(Technician.objects.filter(is_active=True).prefetch_related("credentials"))
    models = list(DeviceModel.objects.values_list("category", "manufacturer", "model"))
    rows = []
    for category in sorted({c for c, _, _ in models}):  # not .distinct(): the model's default ordering would join the projection
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


# --- technicians (slice 23: the technicians import) ------------------------------------------------

TECHNICIAN_FIELDS = ("name", "title", "certification", "weekly_capacity_hours", "is_active")
DEFAULT_WEEKLY_HOURS = Decimal(Technician._meta.get_field("weekly_capacity_hours").default)
MAX_WEEKLY_HOURS = Decimal(80)
# Words a name can end with after a comma, which never start a first name ("John Smith, Jr.", "Dana Whitfield, CBET"): a suffix is
# part of the name, a credential is not.
NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
NAME_CREDENTIALS = frozenset({"cbet", "cres", "cles", "cabt", "chtm", "cce", "bmet", "phd", "rn"})


def _ends_a_name(text: str) -> bool:
    """Whether `text` is only suffixes and credentials ("Jr.", "CBET", "III CBET")."""
    words = [w.strip(".").lower() for w in text.replace(",", " ").split()]
    return bool(words) and all(w in NAME_SUFFIXES or w in NAME_CREDENTIALS for w in words)


def technician_name(name: str) -> str:
    """A name as Cadence writes it: First Last ("Whitfield, Dana" -> "Dana Whitfield"), single spaces. A comma before a suffix or a
    credential is not Last, First ("John Smith, Jr." and "Dana Whitfield, CBET" stay in their order), and a name with more than one
    comma is left in its order: which part is the last name is not clear."""
    name = " ".join((name or "").split())
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        if last and first and not _ends_a_name(first):
            return f"{first} {last}"
    return name


def name_key(name: str) -> str:
    """What two technicians' names are compared by (the technicians and work order imports, and Invite user's technician profile):
    First Last, lowercase, without commas, a suffix without its period, and no credential at the end ("Smith Jr., John", "John Smith,
    Jr." and "john smith jr" are one name, as are "Dana Whitfield, CBET" and "Dana Whitfield")."""
    words = [w.strip(".") if w.strip(".") in NAME_SUFFIXES else w for w in technician_name(name).replace(",", " ").lower().split()]
    while len(words) > 2 and words[-1].strip(".") in NAME_CREDENTIALS:  # a first and a last name stay
        words.pop()
    return " ".join(words)


def _clean_technician(technician: Technician) -> None:
    """Trim the text (a name's inner spaces too: names are matched by their words), check each column's length, and keep the
    weekly hours to 0 to 80, rounded to the tenth the column holds. Raises ValidationError by field."""
    technician.name = " ".join((technician.name or "").split())
    technician.title = (technician.title or "").strip()
    technician.certification = (technician.certification or "").strip()
    errors = {}
    if not technician.name:
        errors["name"] = "A technician needs a name."
    for field in ("name", "title", "certification"):
        limit = Technician._meta.get_field(field).max_length
        if field not in errors and len(getattr(technician, field)) > limit:
            errors[field] = f"Keep the {field} to {limit} characters."
    try:
        hours = Decimal(str(technician.weekly_capacity_hours))
    except (InvalidOperation, ValueError):
        hours = None
    if hours is None or not hours.is_finite() or not 0 <= hours <= MAX_WEEKLY_HOURS:
        errors["weekly_capacity_hours"] = f"Weekly hours must be a number from 0 to {MAX_WEEKLY_HOURS}."
    else:
        technician.weekly_capacity_hours = hours.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if not isinstance(technician.is_active, bool):
        errors["is_active"] = "Active must be yes or no."
    if errors:
        raise ValidationError(errors)


def create_technician(*, name: str, title: str = "", certification: str = "", weekly_capacity_hours=None, is_active: bool = True) -> Technician:
    """A technician with no user account: one the import brings over, current or former (a former one is inactive, so imported
    work orders can still name who did the work). Invite user's create_technician links one to the new account
    (add_account_technician)."""
    technician = Technician(name=name, title=title, certification=certification, is_active=is_active,
                            weekly_capacity_hours=DEFAULT_WEEKLY_HOURS if weekly_capacity_hours is None else weekly_capacity_hours)
    _clean_technician(technician)
    technician.save()
    return technician


def update_technician(technician: Technician, **fields) -> Technician:
    """Change any of TECHNICIAN_FIELDS. Only those are written: the user account a technician is linked to stays linked (that link
    is the account's, made when it was invited)."""
    unknown = set(fields) - set(TECHNICIAN_FIELDS)
    if unknown:
        raise ValidationError({k: "Unknown field." for k in unknown})
    for k, v in fields.items():
        setattr(technician, k, v)
    _clean_technician(technician)
    if fields:
        technician.save(update_fields=[*fields, "updated_at"])
    return technician


@transaction.atomic
def add_account_technician(user, *, name: str, title: str) -> tuple[Technician, bool]:
    """The technician profile of `user`, a new account (Invite user's create_technician): the facility's one technician without an
    account whose name is `name` (name_key), linked and made active (the person works here now), so a technician the import added
    is never doubled (two with one name, and neither import could name either again); else a new one with `title`. Several without
    an account and with that name are refused: which one is this person is for the facility to say. Returns (the technician,
    whether it was already here)."""
    key = name_key(name)
    unlinked = Technician.objects.select_for_update().filter(tenant_id=user.tenant_id, user__isnull=True)  # locked: one account each
    found = [t for t in unlinked if name_key(t.name) == key]
    if len(found) > 1:
        many = "Two" if len(found) == 2 else str(len(found))
        raise ValidationError(f"{many} technicians here without an account are named {name}, so which one this person is cannot be told. "
                              "Invite them without a technician profile.")
    if found:
        technician = found[0]
        technician.user, technician.is_active = user, True
        technician.save(update_fields=["user", "is_active", "updated_at"])
        return technician, True
    return Technician.objects.create(tenant_id=user.tenant_id, user=user, name=name, title=title), False


# --- credential lifecycle (the Users and access tab) ---------------------------------------------

def technician_of(user) -> Technician | None:
    """`user`'s own active technician profile in the current facility, or None (slice 24). Through the tenant-scoped manager: never
    `user.technician`, the reverse one-to-one, which reads around the facility's scope and raises when there is none."""
    if user is None or not getattr(user, "pk", None):
        return None
    return Technician.objects.filter(user_id=user.pk, is_active=True).first()


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
    credential.expires_on = add_months(today or timezone.localdate(), months)
    credential.save(update_fields=["expires_on", "updated_at"])
    return credential


def sign_off_credential(credential: Credential, today: date | None = None) -> Credential:
    """In-house sign-off: the credential becomes active as of today."""
    if credential.status != Credential.Status.IN_TRAINING:
        raise ValidationError("Only a credential in training can be signed off.")
    credential.status = Credential.Status.ACTIVE
    credential.source = SIGN_OFF_SOURCE
    credential.issued_on = today or timezone.localdate()
    credential.save(update_fields=["status", "source", "issued_on", "updated_at"])
    return credential


def remove_credential(credential: Credential) -> None:
    credential.delete()


def credential_state(credential: Credential, today: date | None = None) -> dict:
    """Status chip for a credential row: key, label, and chip css, matching the mock's credState."""
    today = today or timezone.localdate()
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
