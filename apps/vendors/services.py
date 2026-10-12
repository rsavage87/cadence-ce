"""
Every write to vendor staff records (slice 31), and the choices the completion modal, the work order drawer, and the API offer.

Engineers: add_engineer, update_engineer (name, company until a work order names them, active), engineer_named (the engineer of a
vendor with a name, reused or created: completion's "someone else"). Trainings: add_training (one coverage for one or several of a
vendor's engineers), remove_training (entered in error). Attestations: add_attestation, remove_attestation (entered in error). A renewal
is a new record: each covers the days its own dates say (apps.vendors.rules).

The engineer a work order names: `engineer_choice` reads what a door sent (an engineer, an id, a typed name, None to clear, or KEEP) for
completion.complete_work_order and workorders.services.set_vendor_engineer, the two writers of WorkOrder.vendor_engineer. A scoped
vendor account chooses only among the engineers of the work order's vendor, or types a name (an engineer created for that vendor, the
history saying "Added at completion of WO-…"); an id outside its choices answers as an unknown one does.

The level checks are the callers' (apps.vendors.permissions: Contracts View sees, Edit changes; completing is Work orders Edit).
"""
from __future__ import annotations

import uuid
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.credentials.services import name_key

from . import rules
from .models import ATTESTATION_REFERENCE_VALIDATOR, CERTIFICATE_VALIDATOR, Covers, EngineerTraining, TrainingSource, VendorAttestation, VendorEngineer

COMPANY_MAX = VendorEngineer._meta.get_field("company").max_length
CERTIFICATE_MAX = EngineerTraining._meta.get_field("certificate").max_length
REFERENCE_MAX = VendorAttestation._meta.get_field("reference").max_length

KEEP = object()  # engineer_choice: leave the engineer a work order names as it is (an API completion that sends none)

ENGINEER_ONLY = "Only vendor service names the vendor's engineer."
CHOOSE_LISTED = "Choose one of the engineers listed, or type the name."
NOT_BOTH = "Choose an engineer or type a name, not both."
COMPANY_FIXED = ("{name} is named on work orders, so their company stays. An engineer who moved to another vendor is added again "
                 "under that vendor.")
ANOTHER_COMPANY = "Another company's engineer"


def _parse_uuid(value) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _company(company, field: str = "company") -> str:
    """A vendor name as typed (ends trimmed, inner spacing kept, as work orders and accounts keep it): required, at most COMPANY_MAX."""
    text = str(company or "").strip()
    if not rules.vendor_key(text):
        raise ValidationError({field: "Enter the vendor as your work orders and contracts name it."})
    if len(text) > COMPANY_MAX:
        raise ValidationError({field: f"Keep the vendor's name to {COMPANY_MAX} characters."})
    return text


def _past(day, field: str, what: str, today: date, errors: dict, *, required: bool = True):
    if day is None or day == "":
        if required:
            errors[field] = f"Enter the {what}."
        return None
    if not isinstance(day, date):
        errors[field] = f"Enter the {what} as a date."
        return None
    if day > today:
        errors[field] = f"The {what} cannot be in the future."
    return day


# --- engineers -------------------------------------------------------------------------------------------------------------------

def _on_file(company_key: str, nkey: str, exclude=None) -> VendorEngineer | None:
    """The engineer of that vendor and name already on file (another than `exclude`), active or not."""
    qs = VendorEngineer.objects.filter(company_key=company_key, name_key=nkey)
    return (qs.exclude(pk=exclude.pk) if exclude is not None else qs).first()


def _already(found: VendorEngineer | None, name: str, company: str) -> ValidationError:
    """The refusal of a second engineer of one vendor and name, naming the one on file as it is written (review fix: it repeated the
    name as typed, "kim, dana", and never said the one on file was inactive)."""
    if found is None:
        return ValidationError({"name": f"{name} is already an engineer of {company}."})
    inactive = "" if found.is_active else " (inactive)"
    return ValidationError({"name": f"{found.name} is already an engineer of {found.company}{inactive}."})


def add_engineer(*, name, company, by=None) -> VendorEngineer:
    """A vendor's engineer, as their reports name them. Refused when the vendor already has an engineer of that name (any letter case,
    "Last, First" too: credentials.services.name_key)."""
    name = rules.clean_engineer_name(name)
    company = _company(company)
    key, nkey = rules.vendor_key(company), name_key(name)
    found = _on_file(key, nkey)
    if found is not None:
        raise _already(found, name, company)
    try:
        with transaction.atomic():
            return VendorEngineer.objects.create(name=name, company=company, company_key=key, name_key=nkey, created_by=by)
    except IntegrityError:  # added at the same moment by someone else
        raise _already(_on_file(key, nkey), name, company) from None


@transaction.atomic
def update_engineer(engineer: VendorEngineer, *, name=None, company=None, is_active=None, by=None) -> VendorEngineer:
    """Change an engineer's name, company (only while no work order names them), or whether they are active (an inactive engineer is
    never offered for new work; their records stay)."""
    from apps.workorders.models import WorkOrder

    locked = VendorEngineer.objects.select_for_update().get(pk=engineer.pk)
    fields = []
    if name is not None:
        name = rules.clean_engineer_name(name)
        if name != locked.name:
            locked.name, locked.name_key = name, name_key(name)
            fields += ["name", "name_key"]
    if company is not None:
        company = _company(company)
        if company != locked.company:
            # Another vendor once a work order names them; the same vendor spelled another way ("PHILIPS", "Philips field service":
            # rules.vendor_key) is no move, and relabels none of their jobs (review fix: it was refused as one).
            if rules.vendor_key(company) != locked.company_key and WorkOrder.objects.filter(vendor_engineer=locked).exists():
                raise ValidationError({"company": COMPANY_FIXED.format(name=locked.name)})
            locked.company, locked.company_key = company, rules.vendor_key(company)
            fields += ["company", "company_key"]
    if is_active is not None:
        if not isinstance(is_active, bool):
            raise ValidationError({"is_active": "Active must be yes or no."})
        if is_active != locked.is_active:
            locked.is_active = is_active
            fields.append("is_active")
    if {"name_key", "company_key"} & set(fields):
        found = _on_file(locked.company_key, locked.name_key, exclude=locked)
        if found is not None:
            raise _already(found, locked.name, locked.company)
    if fields:
        try:
            with transaction.atomic():
                locked.save(update_fields=[*fields, "updated_at"])
        except IntegrityError:
            raise _already(_on_file(locked.company_key, locked.name_key, exclude=locked), locked.name, locked.company) from None
    engineer.__dict__.update({f: getattr(locked, f) for f in ("name", "name_key", "company", "company_key", "is_active")})
    return locked


def engineer_named(vendor_name: str, name, *, by=None, work_order=None) -> VendorEngineer:
    """The engineer of `vendor_name` named `name` (name_key; an inactive one is reused, not reactivated), else a new one for that
    vendor: completion's "someone else". Two completions typing the same new name at once make one engineer (the unique key, read again
    after the second insert fails)."""
    name = rules.clean_engineer_name(name, "engineer_name")
    company = _company(vendor_name, "engineer_name")
    key, nkey = rules.vendor_key(company), name_key(name)
    found = VendorEngineer.objects.filter(company_key=key, name_key=nkey).first()
    if found is not None:
        return found
    engineer = VendorEngineer(name=name, company=company, company_key=key, name_key=nkey, created_by=by)
    if work_order is not None:  # workorders.services.set_vendor_engineer says its own words (a name typed after completion, or before)
        engineer._change_reason = getattr(work_order, "_engineer_added", "") or f"Added at completion of {work_order.number}"
    try:
        with transaction.atomic():
            engineer.save()
    except IntegrityError:
        return VendorEngineer.objects.get(company_key=key, name_key=nkey)
    return engineer


def engineers_for(vendor_name: str):
    """The active engineers of a vendor (by vendor_key), by name."""
    key = rules.vendor_key(vendor_name)
    return VendorEngineer.objects.filter(company_key=key, is_active=True).order_by("name") if key else VendorEngineer.objects.none()


def engineer_options(wo, user) -> tuple[list, list]:
    """The engineers a door offers for `wo`: (the work order's vendor's active engineers, every other active engineer), the second
    empty for a scoped user (a vendor account names only its own company's engineers). The engineer already named is offered first
    in its group even when inactive."""
    from apps.workorders import scoping

    own = list(engineers_for(wo.vendor_name))
    current = wo.vendor_engineer if wo.vendor_engineer_id else None
    others = [] if scoping.is_scoped(user) else list(
        VendorEngineer.objects.filter(is_active=True).exclude(company_key=rules.vendor_key(wo.vendor_name)).order_by("company", "name"))
    if current is not None and current not in own and current not in others:
        if current.company_key == rules.vendor_key(wo.vendor_name):
            own.insert(0, current)
        elif not scoping.is_scoped(user):
            others.insert(0, current)
    return own, others


def may_name(wo, engineer: VendorEngineer, user) -> bool:
    """Whether `user` may name `engineer` on `wo`: one of this facility's, and the one named already, or an active one of the work
    order's vendor, or (not for a scoped user) any active one."""
    from apps.workorders import scoping

    if engineer.tenant_id != wo.tenant_id:  # a foreign key check reads around row-level security: never trust an id
        return False
    if engineer.pk == wo.vendor_engineer_id:
        return True
    if not engineer.is_active:
        return False
    return not scoping.is_scoped(user) or engineer.company_key == rules.vendor_key(wo.vendor_name)


def engineer_choice(wo, value=KEEP, name="", *, by=None, errors: dict):
    """What a door sent for the engineer of `wo` (a vendor-service work order): KEEP (nothing sent), None or "" (not named), an engineer
    or its id, or a typed `name`. Returns KEEP, None, an engineer, or ("new", the cleaned name) for engineer_named once the rest is
    checked; a refusal goes into `errors` keyed vendor_engineer or engineer_name, and KEEP is returned."""
    name = str(name or "").strip()
    sent = value is not KEEP and value not in (None, "")
    if not wo.vendor_service:
        if sent or name:
            errors["vendor_engineer"] = ENGINEER_ONLY
        return KEEP
    if sent and name:
        errors["vendor_engineer"] = NOT_BOTH
        return KEEP
    if name:
        try:
            return ("new", rules.clean_engineer_name(name, "engineer_name"))
        except ValidationError as e:
            errors.update(e.message_dict)
            return KEEP
    if value is KEEP:
        return KEEP
    if not sent:
        return None
    # Read again through this facility's rows, an engineer passed in too (review fix: a copy loaded before a deactivation still read
    # as active; another facility's reads as unknown).
    pk = value.pk if isinstance(value, VendorEngineer) else _parse_uuid(value)
    engineer = VendorEngineer.objects.filter(pk=pk).first() if pk else None
    if engineer is None or not may_name(wo, engineer, by):
        errors["vendor_engineer"] = CHOOSE_LISTED
        return KEEP
    return engineer


def resolve_choice(choice, wo, *, by=None):
    """engineer_choice's answer made real: ("new", name) becomes the engineer of the work order's vendor with that name."""
    if isinstance(choice, tuple):
        return engineer_named(wo.vendor_name, choice[1], by=by, work_order=wo)
    return choice


def engineer_shown(wo, user) -> str:
    """The engineer `wo` names, as `user` may read it: "Dana Kim (Philips)"; for a scoped user, an engineer of a company other than the
    work order's vendor reads "Another company's engineer". "" when none is named."""
    from apps.workorders import scoping

    if not wo.vendor_engineer_id:
        return ""
    engineer = wo.vendor_engineer
    if scoping.is_scoped(user) and engineer.company_key != rules.vendor_key(wo.vendor_name):
        return ANOTHER_COMPANY
    return f"{engineer.name} ({engineer.company})"


# --- coverage --------------------------------------------------------------------------------------------------------------------

def _coverage(covers, model_ids, manufacturer, category, errors: dict, *, allow_all: bool) -> dict:
    """The coverage fields to store, checked against this facility's catalog: device models (at least one, each this facility's), or
    one manufacturer's category (a pair some catalog model has), or (an attestation) every device."""
    from apps.equipment.models import DeviceModel

    covers = str(covers or "").strip()
    allowed = Covers.values if allow_all else [Covers.MODELS, Covers.LINE]
    if covers not in allowed:
        errors["covers"] = ("Choose what it covers: device models, or a manufacturer's product category"
                            + (", or every device this vendor services for us." if allow_all else "."))
        return {}
    out = {"covers": covers, "model_ids": [], "manufacturer": "", "category": ""}
    if covers == Covers.MODELS:
        ids = model_ids if isinstance(model_ids, (list, tuple)) else ([model_ids] if model_ids else [])
        parsed = [_parse_uuid(i) for i in ids]
        found = {str(pk) for pk in DeviceModel.objects.filter(pk__in=[p for p in parsed if p]).values_list("pk", flat=True)}
        if not ids:
            errors["model_ids"] = "Choose at least one device model."
        elif None in parsed or len(found) != len({str(p) for p in parsed}):
            errors["model_ids"] = "Choose device models from the catalog."
        out["model_ids"] = sorted(found)
    elif covers == Covers.LINE:
        manufacturer, category = str(manufacturer or "").strip(), str(category or "").strip()
        if not manufacturer:
            errors["manufacturer"] = "Choose the manufacturer."
        if not category:
            errors["category"] = "Choose the product category."
        if manufacturer and category and not DeviceModel.objects.filter(manufacturer=manufacturer, category=category).exists():
            errors["category"] = f"No device model in the catalog is a {manufacturer} {category.lower()}."
        out.update(manufacturer=manufacturer, category=category)
    return out


# --- trainings -------------------------------------------------------------------------------------------------------------------

@transaction.atomic
def add_training(engineers, *, covers, model_ids=(), manufacturer="", category="", source, certificate="", issued_on, expires_on=None,
                 by=None, today: date | None = None) -> list[EngineerTraining]:
    """One training on file for each of `engineers` (one or several of a vendor's: a class the vendor ran for several), covering the
    same devices from issued_on (never in the future) to expires_on (none: no expiry). A certificate number is one engineer's, so it
    goes with one engineer only. The same coverage issued the same day for the same engineer is on file already."""
    from apps.tenants.context import get_current_tenant

    today = today or timezone.localdate()
    sent = [e for e in (engineers or []) if e is not None]
    errors: dict = {}
    if not sent:
        errors["engineers"] = "Choose the engineer."
    tenant = get_current_tenant()
    # Review fix: this facility's engineers only (a training saved here naming another facility's engineer crossed facilities: the
    # foreign key reads around row-level security), each once (one engineer sent twice was "already has this training on file").
    if any(not isinstance(e, VendorEngineer) for e in sent) or tenant is None or any(e.tenant_id != tenant.id for e in sent):
        raise ValidationError({"engineers": "Choose engineers on file."})
    engineers = list({e.pk: e for e in sent}.values())
    if len({e.company_key for e in engineers}) > 1:  # review fix: one vendor's engineers, as the docstring says (a class it ran)
        raise ValidationError({"engineers": "Choose engineers of one vendor: add each vendor's training on its own."})
    coverage = _coverage(covers, model_ids, manufacturer, category, errors, allow_all=False)
    source = str(source or "").strip()
    if source not in TrainingSource.values:
        errors["source"] = "Choose where the training came from."
    certificate = str(certificate or "").strip()
    if certificate:
        if len(engineers) > 1:
            errors["certificate"] = "A certificate number is one engineer's: add it to each engineer's own training."
        elif len(certificate) > CERTIFICATE_MAX:
            errors["certificate"] = f"Keep the certificate number to {CERTIFICATE_MAX} characters."
        else:
            try:
                CERTIFICATE_VALIDATOR(certificate)
            except ValidationError as e:
                errors["certificate"] = " ".join(e.messages)
    issued_on = _past(issued_on, "issued_on", "issue date", today, errors)
    if expires_on not in (None, "") and not isinstance(expires_on, date):
        errors["expires_on"] = "Enter the expiry as a date."
    elif issued_on and expires_on and expires_on < issued_on:
        errors["expires_on"] = "The expiry date cannot be before the issue date."
    if errors:
        raise ValidationError(errors)
    expires_on = expires_on or None
    made = []
    for engineer in engineers:
        same = EngineerTraining.objects.filter(engineer=engineer, issued_on=issued_on, covers=coverage["covers"],
                                               manufacturer=coverage["manufacturer"], category=coverage["category"])
        if any(sorted(t.model_ids or []) == coverage["model_ids"] for t in same):
            raise ValidationError({"issued_on": f"{engineer.name} already has this training on file, issued {rules.fmt(issued_on)}."})
        made.append(EngineerTraining.objects.create(engineer=engineer, source=source, certificate=certificate, issued_on=issued_on,
                                                    expires_on=expires_on, created_by=by, **coverage))
    return made


def remove_training(training: EngineerTraining, by=None) -> None:
    """A training entered in error: removed, so it covers no day (its history keeps it). A training that lapsed stays on file: it
    covered its own days."""
    training._change_reason = "Removed: entered in error"
    training.delete()


# --- attestations ----------------------------------------------------------------------------------------------------------------

def _vendor_names(names, errors: dict) -> tuple[list, list]:
    """The vendor's names (each as typed, at most COMPANY_MAX), one per vendor_key, and their keys."""
    if isinstance(names, str):
        names = [names]
    kept, keys = [], []
    for name in names or []:
        text = str(name or "").strip()
        key = rules.vendor_key(text)
        if not key:
            continue
        if len(text) > COMPANY_MAX:
            errors["vendor_names"] = f"Keep each vendor name to {COMPANY_MAX} characters."
        if key not in keys:
            kept.append(text)
            keys.append(key)
    if not kept:
        errors["vendor_names"] = "Enter the vendor as your work orders and contracts name it."
    return kept, keys


@transaction.atomic
def add_attestation(*, vendor_names, covers, model_ids=(), manufacturer="", category="", reference, received_on, valid_from,
                    valid_through, by=None, today: date | None = None) -> VendorAttestation:
    """A vendor's written attestation on file: its names, what it covers, its reference (the letter's, or the contract and clause), the
    day it was received (never in the future), and the period it states (both required: a letter with no end is not evidence a
    surveyor accepts; a contract's clause runs for the contract's term)."""
    today = today or timezone.localdate()
    errors: dict = {}
    names, keys = _vendor_names(vendor_names, errors)
    coverage = _coverage(covers, model_ids, manufacturer, category, errors, allow_all=True)
    reference = str(reference or "").strip()
    if not reference:
        errors["reference"] = "Enter the letter's reference, or the contract and clause."
    elif len(reference) > REFERENCE_MAX:
        errors["reference"] = f"Keep the reference to {REFERENCE_MAX} characters."
    else:
        try:
            ATTESTATION_REFERENCE_VALIDATOR(reference)
        except ValidationError as e:
            errors["reference"] = " ".join(e.messages)
    received_on = _past(received_on, "received_on", "day it was received", today, errors)
    for field, what in (("valid_from", "first day it covers"), ("valid_through", "last day it covers")):
        value = valid_from if field == "valid_from" else valid_through
        if value in (None, ""):
            errors[field] = f"Enter the {what}."
        elif not isinstance(value, date):
            errors[field] = f"Enter the {what} as a date."
    if isinstance(valid_from, date) and isinstance(valid_through, date) and valid_through < valid_from:
        errors["valid_through"] = "The last day it covers cannot be before the first."
    if errors:
        raise ValidationError(errors)
    return VendorAttestation.objects.create(vendor_names=names, vendor_keys=keys, reference=reference, received_on=received_on,
                                            valid_from=valid_from, valid_through=valid_through, created_by=by, **coverage)


def remove_attestation(attestation: VendorAttestation, by=None) -> None:
    """An attestation entered in error: removed, so it covers no day (its history keeps it)."""
    attestation._change_reason = "Removed: entered in error"
    attestation.delete()


def attestations_for(vendor_name: str) -> list[VendorAttestation]:
    """The attestations naming a vendor (by vendor_key), the one running longest first."""
    key = rules.vendor_key(vendor_name)
    return [a for a in VendorAttestation.objects.order_by("-valid_through", "reference") if key and key in (a.vendor_keys or [])]
