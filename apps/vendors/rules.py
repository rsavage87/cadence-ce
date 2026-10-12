"""
The one rule for vendor staff evidence (slice 31): which vendor a name is, whether a training or an attestation names a device model and
is in force on a day, and the evidence that stood behind a vendor job. The survey binder, the work order drawer, the completion modal,
the Vendor staff tab, and the API all read it here.

A vendor job (a completed work order on vendor service) is judged on its completed_on day:
1. the engineer it names has a training on file naming the device's model, in force that day (TRAINING); else
2. an attestation from the work order's vendor, or from the named engineer's company (a vendor may send another company's engineer),
   naming the model, in force that day (ATTESTATION); else
3. none (NONE).
Each record is judged by its own dates, whenever it was entered: a record entered after the job still counts, and `entered_after` says
so (the binder words it "on file since <day>").
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError

from apps.core.days import local_day

from .models import Covers, TrainingSource

FIELD_SERVICE = " field service"  # the name web.forms.vendor_name_for (equipment.services.service_vendor) gives a maker's own service
NAME_MIN, NAME_MAX = 2, 120
NAME_MARKS = frozenset(" .'-,’")  # with letters: "Mary-Jo O'Neil", "J. R. Smith, Jr."
NAME_RULE = "Enter the engineer's name as their report gives it: letters, spaces, and . ' - , only."

TRAINING, ATTESTATION, NONE = "training", "attestation", "none"


def vendor_key(name) -> str:
    """Which vendor a name is: casefolded, inner spaces collapsed, one trailing " field service" dropped ("Philips", "PHILIPS  field
    service" and "philips" are one vendor). Compared in Python (SQLite's iexact folds ASCII only)."""
    text = " ".join(unicodedata.normalize("NFC", str(name or "")).split()).casefold()  # composed: "Dräger" typed either way is one
    if text.endswith(FIELD_SERVICE) and len(text) > len(FIELD_SERVICE):
        text = text[: -len(FIELD_SERVICE)].rstrip()
    return text


def clean_engineer_name(name, field: str = "name") -> str:
    """A person's name as typed, inner spaces collapsed: 2 to 120 characters of letters, spaces, and . ' - , (no digits, @, or /:
    badge numbers, phone numbers, record numbers, and dates stay out). Raises ValidationError keyed `field`."""
    # Review fix: composed first ("José" typed as e and a combining accent read as a mark, not a letter, and was refused)
    text = " ".join(unicodedata.normalize("NFC", str(name or "")).split())
    if not text:
        raise ValidationError({field: "Enter the engineer's name."})
    if len(text) > NAME_MAX:
        raise ValidationError({field: f"Keep the name to {NAME_MAX} characters."})
    if len(text) < NAME_MIN or not any(ch.isalpha() for ch in text) or any(not (ch.isalpha() or ch in NAME_MARKS) for ch in text):
        raise ValidationError({field: NAME_RULE})
    return text


# --- coverage --------------------------------------------------------------------------------------------------------------------

def names_model(record, device_model) -> bool:
    """Whether `record` (a training or an attestation: anything with covers, model_ids, manufacturer, category) names devices of
    `device_model` (anything with id, manufacturer, and category)."""
    if record.covers == Covers.ALL:
        return True
    if record.covers == Covers.MODELS:
        return str(device_model.id) in {str(i) for i in record.model_ids or ()}
    if record.covers == Covers.LINE:
        return record.manufacturer == device_model.manufacturer and record.category == device_model.category
    return False


def training_in_force(training, day: date) -> bool:
    return training.issued_on <= day and (training.expires_on is None or day <= training.expires_on)


def attestation_in_force(attestation, day: date) -> bool:
    return attestation.valid_from <= day <= attestation.valid_through


def from_vendor(attestation, keys) -> bool:
    """Whether `attestation` names any of the vendor keys `keys`."""
    return bool(set(attestation.vendor_keys or ()) & set(keys))


def entered_on(record) -> date:
    """The facility's day a record was entered in Cadence (call inside the facility: core.days.local_day)."""
    return local_day(record.created_at)


# --- the evidence for a job ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    kind: str  # TRAINING, ATTESTATION, or NONE
    record: object = None  # the EngineerTraining or VendorAttestation
    entered_after: bool = False  # entered in Cadence after the day judged
    expired_on: date | None = None  # NONE: the named engineer's last training naming the model had expired on this day


def _first(records, day: date):
    """The record to show: one already on file that day before one entered later, then the one running longest."""
    far = date.max
    return min(records, key=lambda r: (entered_on(r) > day, -(getattr(r, "expires_on", None) or getattr(r, "valid_through", None) or far).toordinal()),
               default=None)


def evidence_for(*, day: date, device_model, vendor_name: str, engineer=None, trainings=(), attestations=()) -> Evidence:
    """The evidence behind a vendor job done on `day` on a device of `device_model` for `vendor_name`: `trainings` are the named
    `engineer`'s (any, filtered here), `attestations` any (filtered here by vendor)."""
    if engineer is not None:
        naming = [t for t in trainings if t.engineer_id == engineer.pk and names_model(t, device_model)]
        current = [t for t in naming if training_in_force(t, day)]
        if current:
            record = _first(current, day)
            return Evidence(TRAINING, record, entered_on(record) > day)
    keys = {vendor_key(vendor_name)} | ({engineer.company_key} if engineer is not None else set())
    keys.discard("")
    covering = [a for a in attestations if from_vendor(a, keys) and names_model(a, device_model) and attestation_in_force(a, day)]
    if covering:
        record = _first(covering, day)
        return Evidence(ATTESTATION, record, entered_on(record) > day)
    expired = [t.expires_on for t in (trainings if engineer is not None else ())
               if t.engineer_id == engineer.pk and names_model(t, device_model) and t.expires_on and t.expires_on < day]
    return Evidence(NONE, expired_on=max(expired, default=None))


# --- words --------------------------------------------------------------------------------------------------------------------

def fmt(day: date | None) -> str:
    return f"{day:%b} {day.day}, {day.year}" if day else ""


def coverage_words(record, models_by_id: dict) -> str:
    """What a record covers, in words: "Philips IntelliVue MX750, Philips IntelliVue MX450", "Philips patient monitors", "Every device
    this vendor services for us". `models_by_id` maps str(id) to a device model (one that is gone reads "a removed model")."""
    if record.covers == Covers.ALL:
        return Covers.ALL.label
    if record.covers == Covers.LINE:
        return f"{record.manufacturer} {record.category}"
    names, gone = [], 0
    for i in record.model_ids or ():
        m = models_by_id.get(str(i))
        if m is None:
            gone += 1
        else:
            names.append(f"{m.manufacturer} {m.model}")
    return ", ".join(sorted(names, key=str.casefold) + ["a removed model"] * gone)  # by name (ids are stored in id order)


def training_words(training, models_by_id: dict) -> str:
    """"Training on file: Philips IntelliVue MX750, OEM factory training, through Mar 31, 2027" (never "trained": Cadence keeps the
    document, not the competence)."""
    until = f"through {fmt(training.expires_on)}" if training.expires_on else "no expiry"
    return f"Training on file: {coverage_words(training, models_by_id)}, {TrainingSource(training.source).label}, {until}"


def evidence_words(evidence: Evidence, models_by_id: dict) -> str:
    """The evidence column: what was on file for the job, and "on file since <day>" when it was entered after the job."""
    if evidence.kind == TRAINING:
        words = training_words(evidence.record, models_by_id)
    elif evidence.kind == ATTESTATION:
        a = evidence.record
        words = f"Attestation {a.reference} on file ({', '.join(a.vendor_names)}), valid {fmt(a.valid_from)} to {fmt(a.valid_through)}"
    else:
        words = "No training or attestation on file"
        if evidence.expired_on:
            words += f"; the engineer's training on this model expired on {fmt(evidence.expired_on)}"
        return words
    if evidence.entered_after:
        words += f" (on file since {fmt(entered_on(evidence.record))})"
    return words
