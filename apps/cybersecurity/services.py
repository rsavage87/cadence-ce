"""
Every write to the device security register (slice 37): a model's profile and releases, and a device's facts. Each validates, resolves
ids through the tenant (Model.objects), writes history with a reason, and refuses in words keyed by field (a ValidationError). A user
given as `by` needs Device security Edit (and Equipment View for a device's facts: permissions.py), else PermissionDenied; None is the
system (the seed, tests).

Lock order (review fixes C2, C3, C13, rounds 2 and 3): the facility's register lock first (lock_register: every writer here, so two
of them never race, whatever devices or releases they touch); then a release's row (select_for_update of its own row only: never its
model's, which a join would lock too) when editing, removing, or naming it; then the devices' rows FOR KEY SHARE in tag order
(_locked_devices), each device's model and status read once held: KEY SHARE waits for every writer holding a device FOR UPDATE (a model
or status change: equipment.services.update_asset, set_status, _locked_row; an incoming inspection's pass; a work order's Start or
completion through workorders.services._lock_for_hold), whose result is then read, so the old model's release is refused, never saved;
only plain saves of a device (a contract's Add model) never wait on it; then the devices' security rows
(FOR UPDATE, in asset pk order), a missing one inserted after. These writers never lock work orders. The one outside caller is
equipment.services.update_asset -> model_changed, which runs with the device's work and row already held and takes the security row last
(never the register lock: a writer of devices waits on its devices, never on the register). Writers that lock a model's devices FOR UPDATE
in tag order (AEM's pull-in) wait for the bulk set, or it for them, in turn. A transaction locking several devices FOR UPDATE in another
order (an import chunk, in its file's order) can still meet the bulk set in a deadlock: PostgreSQL ends one side; the bulk set then answers
the RACE refusal in words, and an import chunk reports the row it could not save. Slice 38 (apps.cybersecurity.updates) keeps one order
for every register writer: the register lock; work orders in workorders.services.lock_work order, when a writer names one; work order
numbering, when it opens one; releases; devices; security rows; update rows. Nothing takes the register lock after a work order's or a
device's row: a completion that records what an update did takes it first, before lock_work (updates' module docstring).

Nothing here is deleted: a release entered in error is marked (removed_on, removed_by) and refused while a device on site names it
(rules.on_site, of the release's model: the devices the Models panel counts, review fix C4), (slice 38) while an open software update
work order is to install it (OPEN_TO_INSTALL), or (slice 39) while a live security decision is for it (DECISION_LIVE); its version is
not changed while any decision names it, live or ended (DECISION_NAMES); both name the decision's number only to a reader of decisions
(permissions.can_view_devices), anyone else reads that a security decision holds it (DECISION_LIVE_UNREAD, DECISION_NAMES_UNREAD); a
retired or returned device's record keeps naming it, as an update's record that is not open does (its history reads it as it was; the
rules read a release entered in error as not recorded, an update's words as "<version> (entered in error since)"); adding the same
version again restores it. A device's facts stay with its history when it is retired; a retired device (or a returned temporary one) is
refused, in words.

Field checks (one helper for add and update, every problem keyed by field, all at once): dates are days (date objects or ISO text), from
EARLIEST_YEAR, at most HORIZON_YEARS ahead, and the days something happened (support checked, MDS2 released, MDS2 requested) not after
today; choices from their lists or blank (not recorded); the version and OS name product text, the MDS2 document a token; the MDS2's form,
document, release day, and link only while it is on file (its form then needed), the day requested only while it is requested; no OS name
or OS support day for a model with no operating system; the link https, with no user or password in it and no query, fragment, or path
parameter key that looks like a credential (_credential_key: a session, secret, token, signature, password, or key; never a page anchor). Text is
stripped; an unset choice or text is "", an unset day None.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from urllib.parse import unquote_plus, urlsplit

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from . import permissions as perms
from .models import (
    DOCUMENT_MAX,
    FACILITY_GROUP,
    MDS2_DOCUMENT_VALIDATOR,
    OS_NAME_MAX,
    OS_NAME_VALIDATOR,
    RELEASE_FIELDS,
    VERSION_MAX,
    VERSION_VALIDATOR,
    Connects,
    DeviceSecurity,
    Disclosed,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
)

KEEP = object()  # an argument left as it is
BULK_MAX = 500  # devices set to a release at once (one model's)
EARLIEST_YEAR = 1950  # a date before it is refused (a mistyped year)
HORIZON_YEARS = 50  # a date further ahead is refused (a "never" placeholder)
# A link's key=value key looks like a credential (review fix C6, round 2: never a bare anchor, never "author" or "designId") when, decoded
# and casefolded: any of CREDENTIAL_WORDS is in it (sessionid, jsessionid, client_secret, authtoken), or any of its parts split on
# anything not a letter or digit is one of CREDENTIAL_PARTS (sig, key, auth, sid: "api_key", "sig"), or it is one of CREDENTIAL_KEYS
# (apikey, phpsessid), or it starts x-amz-.
CREDENTIAL_WORDS = ("token", "signature", "session", "secret", "password", "passwd", "passcode", "passphrase", "credential", "jwt",
                    "authorization", "apikey")
CREDENTIAL_PARTS = ("sig", "key", "auth", "pass", "pwd", "sid", "sess")
CREDENTIAL_KEYS = ("apikey", "accesskey", "authkey", "secretkey", "phpsessid", "sessid", "passkey")
LOCK_CONFLICTS = ("40P01", "40001", "55P03")  # PostgreSQL's deadlock detected, serialization failure, lock not available
URL_MAX = SoftwareRelease._meta.get_field("mds2_url").max_length

DEVICE_MODEL_CHANGED = "Device model changed"
# History reasons
ADDED, EDITED, RESTORED, ENTERED_IN_ERROR, FACTS_RECORDED = "Added", "Edited", "Restored", "Entered in error", "Facts recorded"

# Refusals (keyed by field)
EDIT_PERMISSION = "Recording a model's software releases needs Device security Edit."
FACTS_PERMISSION = "Recording a device's security facts needs Device security Edit and Equipment View."
CHOOSE_MODEL = "Choose a device model of this facility."
CHOOSE_RELEASE = "Choose a software release of this facility's models."
CHOOSE_DEVICE = "Choose a device of this facility."
CHOOSE_DEVICES = "Choose devices of this facility."
CHOOSE_VERSION = "Choose a version of this device's model."
CHOOSE_MODEL_VERSION = "Choose a version of this model."
NO_DEVICES = "Choose the devices to set."
TOO_MANY = f"Set at most {BULK_MAX} devices at once: choose a department to narrow them."
# Review fix C7: the devices are one department's (one chosen, or the model's only one), which narrows them no further
TOO_MANY_IN_ONE = (f"Set at most {BULK_MAX} devices at once: these {{count}} are all in one department, so record their facts on each "
                   "device's Security tab.")
PROFILE_EXISTS = "This model already has a profile; edit it."
VERSION_ON_FILE = "Version {version} is already on file for this model."
VERSION_REMOVED = "Version {version} was entered in error for this model: add it again to restore it."
FACILITY_ON_PROFILE = "Who installs updates here and segmentation are the model's: record them on its profile."
PROFILE_VERSIONLESS = "A profile stays versionless: add the version as a release of its own."
VERSION_NEEDED = "Enter the version as the manufacturer writes it."
VERSION_NAMED = "Devices run this version; add the new version instead."
PROFILE_KEPT = "A profile is cleared field by field, never removed."
ALREADY_REMOVED = "This release is already marked entered in error."
REMOVED_RELEASE = "This release was entered in error: add its version again to restore it."
URL_WORDS = "Enter the manufacturer's https link, without a password, token, or signature in it."
CHOOSE_ADVISORY = "Choose an advisory recorded here."  # slice 40
CHOICE_WORDS = "Choose from the list, or leave it not recorded."
ON_FILE_ONLY = "Record this only while the MDS2 is on file."
EDITION_NEEDED = "Choose the MDS2's form (the year on it)."
REQUESTED_ONLY = "Record the day it was requested only while the MDS2 is requested."
NO_OS_NAME = "No operating system: leave its name blank."
NO_OS_DAY = "No operating system: leave its support day blank."
NETWORK_WORDS = "Choose how it is connected here, or leave it as the model discloses."
RACE = "These devices were being changed or recorded meanwhile: try again."  # also the bulk set's answer to a deadlock
NO_RELEASE_SINCE = "Choose the release it runs first."

CHOICES = {"os_family": OsFamily, "mds2": Mds2Status, "mds2_edition": Mds2Edition, "patient_data": PatientData, "connects": Connects,
           "remote_service": Disclosed, "patching": Patching, "sbom": Sbom, "updates_by": UpdatesBy, "segmented": Segmented}
TEXT = {"version": (VERSION_VALIDATOR, VERSION_MAX), "os_name": (OS_NAME_VALIDATOR, OS_NAME_MAX),
        "mds2_document": (MDS2_DOCUMENT_VALIDATOR, DOCUMENT_MAX)}
DATES = {"os_support_ends_on": "The OS vendor's last day of support", "security_updates_end_on": "The last day of security updates",
         "support_checked_on": "The day support was checked", "mds2_released_on": "The MDS2's release day",
         "mds2_requested_on": "The day the MDS2 was requested"}
HAPPENED = ("support_checked_on", "mds2_released_on", "mds2_requested_on")  # days something happened: never after today
MDS2_ON_FILE_FIELDS = ("mds2_edition", "mds2_document", "mds2_released_on", "mds2_url")
assert set(RELEASE_FIELDS) == {"version", *CHOICES, *TEXT, *DATES, "mds2_url"}  # every field has its check


def version_key(version: str) -> str:
    """The uniqueness of a version in any letter case: casefolded, trimmed, runs of spaces collapsed, one leading "v", "rev", or "rev."
    dropped when a digit follows ("V12.1.2", "Rev. 12.1.2", and "12.1.2" are one release). Blank for the profile."""
    text = " ".join((version or "").split()).casefold()
    return re.sub(r"^(?:rev\.?|v)\s*(?=\d)", "", text)


# --- small parts -----------------------------------------------------------------------------------------------------------------

def _today(today: date | None) -> date:
    return today or timezone.localdate()


def _check(by, allowed, message: str) -> None:
    """The level check every write starts with, for a user given as `by` (None is the system: the seed, tests)."""
    if by is not None and not allowed(by):
        raise PermissionDenied(message)


def _pk(value, model):
    """An id from an instance of `model` or its id (a UUID or its text); None for anything else."""
    if isinstance(value, model):
        return value.pk
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value.strip())
        except ValueError:
            return None
    return None


def _model(model):
    """The device model, resolved through this facility (another facility's id or object is refused, keyed device_model)."""
    from apps.equipment.models import DeviceModel

    pk = _pk(model, DeviceModel)
    found = DeviceModel.objects.filter(pk=pk).first() if pk is not None else None
    if found is None:
        raise ValidationError({"device_model": CHOOSE_MODEL})
    return found


def _locked_release(release, field: str = "release", message: str = CHOOSE_RELEASE) -> SoftwareRelease:
    """The release's row as it is now, locked until the transaction ends (this facility's only). Its own row only (review fix C13): no
    join (the Meta ordering's model columns would add one) and FOR UPDATE OF itself, so its model's row stays free."""
    pk = _pk(release, SoftwareRelease)
    found = SoftwareRelease.objects.select_for_update(of=("self",)).filter(pk=pk).order_by("pk").first() if pk is not None else None
    if found is None:
        raise ValidationError({field: message})
    return found


def _locked_advisory(advisory, field: str = "advisory", *, lock: bool = True):
    """Slice 40: the advisory's row as it is now (this facility's only), by instance, id, or number in any letter case; locked until the
    transaction ends (FOR UPDATE of its own row, no join) only when `lock`. The lock-free form serves the GET doors' readers: FOR UPDATE
    outside a transaction is an error on PostgreSQL (slice 39's 500), so a reader never locks. Refused keyed `field`."""
    from .models import SecurityAdvisory

    pk = _pk(advisory, SecurityAdvisory)
    rows = SecurityAdvisory.objects.order_by("pk")
    if pk is not None:
        rows = rows.filter(pk=pk)
    elif isinstance(advisory, str) and advisory.strip():
        rows = rows.filter(number__iexact=advisory.strip())
    else:
        rows = rows.none()
    if lock:
        rows = rows.select_for_update(of=("self",))
    found = rows.first()
    if found is None:
        raise ValidationError({field: CHOOSE_ADVISORY})
    return found


def _sync(given, saved) -> None:
    """The caller's copy follows the row as saved."""
    if isinstance(given, type(saved)) and given is not saved:
        given.__dict__.update({f.attname: getattr(saved, f.attname) for f in type(saved)._meta.concrete_fields})


def _save(obj, by, reason: str) -> None:
    """Save `obj` with `reason` in its history (cut to the column), as `by`; neither reaches a later save of this copy."""
    obj._change_reason = reason[:type(obj).history.model._meta.get_field("history_change_reason").max_length]
    if by is not None:
        obj._history_user = by
    try:
        obj.save()
    finally:
        obj.__dict__.pop("_change_reason", None)
        obj.__dict__.pop("_history_user", None)


def _years_ahead(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year + years)
    except ValueError:  # Feb 29
        return today.replace(year=today.year + years, day=28)


def _day(value, name: str, what: str, errors: dict, *, today: date, happened: bool) -> date | None:
    """A day from a date or its ISO text (None or "" for none): from EARLIEST_YEAR, at most HORIZON_YEARS ahead, a day something
    happened not after today."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip())
        except ValueError:
            errors[name] = f"{what}: enter a day."
            return None
    if isinstance(value, datetime) or not isinstance(value, date):
        errors[name] = f"{what}: enter a day."
        return None
    if value.year < EARLIEST_YEAR:
        errors[name] = f"{what}: enter the day in full (the year looks wrong)."
    elif happened and value > today:
        errors[name] = f"{what} cannot be after today."
    elif value > _years_ahead(today, HORIZON_YEARS):
        errors[name] = f"{what}: leave it blank when no day is announced (a day more than {HORIZON_YEARS} years ahead looks wrong)."
    return value


def _text(value, name: str, errors: dict) -> str:
    validator, limit = TEXT[name]
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[name] = "Enter it as text."
        return ""
    text = value.strip()
    if not text:
        return ""
    if len(text) > limit:
        errors[name] = f"Keep it to {limit} characters."
        return text
    try:
        validator(text)
    except ValidationError as e:
        errors[name] = " ".join(e.messages)
    return text


def _choice(value, name: str, errors: dict) -> str:
    if value is None:
        return ""
    text = value.strip() if isinstance(value, str) else None
    if text is None or text not in ("", *CHOICES[name].values):
        errors[name] = CHOICE_WORDS
        return ""
    return text


def _credential_key(key: str) -> bool:
    """A link's key that looks like a credential (decoded, casefolded; see CREDENTIAL_WORDS)."""
    k = unquote_plus(key).strip().casefold()
    parts = [p for p in re.split(r"[^0-9a-z]+", k) if p]
    return (k.startswith("x-amz-") or k in CREDENTIAL_KEYS or any(word in k for word in CREDENTIAL_WORDS)
            or any(p in CREDENTIAL_PARTS or p in CREDENTIAL_KEYS for p in parts))


def _link_keys(parts) -> list:
    """Every key a link carries: its query's and its fragment's (split on & and ;), and its path parameters' (";jsessionid=...")."""
    keys = []
    keys += [p.split("=", 1)[0] for p in re.split(r"[&;]", parts.query) if "=" in p]
    # every key=value part of a fragment, a hash route's query included (split on ? too); a bare anchor carries no credential
    keys += [p.split("=", 1)[0].rsplit("/", 1)[-1] for p in re.split(r"[&;?]", parts.fragment) if "=" in p]
    for segment in parts.path.split("/"):
        keys += [p.split("=", 1)[0] for p in segment.split(";")[1:] if p]
    return keys


# Slice 40 (critique40_rules S5, N7): a link names the publisher's page, never an address on the facility's own network: no IP-literal
# host, no explicit port, no single-label host, no internal suffix. The MDS2 link takes the same checks.
INTERNAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".corp")


def _public_host(parts) -> bool:
    """True for a public host name: dotted, not an IP literal, no port, no internal suffix."""
    import ipaddress

    host = (parts.hostname or "").rstrip(".").casefold()
    if not host or "." not in host or parts.port is not None or host.endswith(INTERNAL_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _url(value, errors: dict, field: str = "mds2_url", words: str = URL_WORDS, limit: int | None = None) -> str:
    """A link (the MDS2's; slice 40: an advisory source's): https only, a public host name (_public_host), no user or password in it, no
    query, fragment, or path parameter key that looks like a credential (_credential_key), at most `limit` (URL_MAX) characters. A
    problem is keyed `field` with `words`."""
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[field] = words
        return ""
    text = value.strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        URLValidator(schemes=["https"])(text)
        ok = (len(text) <= (limit or URL_MAX) and parts.scheme == "https" and parts.hostname and parts.username is None
              and parts.password is None and "@" not in parts.netloc and _public_host(parts))
        ok = ok and not any(_credential_key(k) for k in _link_keys(parts))
    except (ValidationError, ValueError):
        ok = False
    if not ok:
        errors[field] = words
    return text


def _clean(raw: dict, errors: dict, *, today: date) -> dict:
    """Each given field cleaned on its own (keyed problems in `errors`)."""
    out = {}
    for name, value in raw.items():
        if name in TEXT:
            out[name] = _text(value, name, errors)
        elif name in CHOICES:
            out[name] = _choice(value, name, errors)
        elif name in DATES:
            out[name] = _day(value, name, DATES[name], errors, today=today, happened=name in HAPPENED)
        elif name == "mds2_url":
            out[name] = _url(value, errors)
    return out


def _check_row(values: dict, *, versioned: bool, errors: dict) -> None:
    """The rules between fields, on the row with the change applied (`values`: every field). A problem is keyed by the field to change,
    never by one that already has its own problem."""
    def refuse(name, words):
        if name not in errors:
            errors[name] = words

    if versioned:
        for name in FACILITY_GROUP:
            if values.get(name):
                refuse(name, FACILITY_ON_PROFILE)
    status = values.get("mds2")
    if "mds2" not in errors:
        if status != Mds2Status.ON_FILE:
            for name in MDS2_ON_FILE_FIELDS:
                if values.get(name) not in (None, ""):
                    refuse(name, ON_FILE_ONLY)
        elif not values.get("mds2_edition"):
            refuse("mds2_edition", EDITION_NEEDED)
        if status != Mds2Status.REQUESTED and values.get("mds2_requested_on") is not None:
            refuse("mds2_requested_on", REQUESTED_ONLY)
    if values.get("os_family") == OsFamily.NONE and "os_family" not in errors:
        if values.get("os_name"):
            refuse("os_name", NO_OS_NAME)
        if values.get("os_support_ends_on") is not None:
            refuse("os_support_ends_on", NO_OS_DAY)


def _unknown(fields: dict) -> None:
    unknown = set(fields) - set(RELEASE_FIELDS)
    if unknown:
        raise ValidationError({name: "This is not recorded on a software release." for name in sorted(unknown)})


def _taken(other: SoftwareRelease, version: str) -> str:
    if not other.version_key:
        return PROFILE_EXISTS
    return (VERSION_REMOVED if other.removed_on is not None else VERSION_ON_FILE).format(version=other.version or version)


# --- a model's profile and releases ------------------------------------------------------------------------------------------------

def add_release(model, *, by, today: date | None = None, profile: bool | None = None, **fields):
    """A versioned release of `model` (or its profile, version blank: at most one). Re-adding a version entered in error restores it
    (removed_on cleared, the given fields applied over what it had, history "Restored"). Fields from RELEASE_FIELDS only; a versioned
    release refuses the facility group (the profile's). A version already on file (by version_key) is refused, also when another
    request adds it at the same moment (the unique key, read back as already on file). `profile=False` is a door that adds a version
    (Add release): a blank one is refused (VERSION_NEEDED) with every other problem at once, never recorded as the profile."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = _today(today)
    _unknown(fields)
    dm = _model(model)
    errors: dict = {}
    given = _clean(fields, errors, today=today)
    version = given.get("version", "")
    key = version_key(version)
    if profile is False and not key and "version" not in errors:
        errors["version"] = VERSION_NEEDED
    with transaction.atomic():
        lock_register()  # first: the register's writers one at a time (review round 3)
        existing = (SoftwareRelease.objects.select_for_update(of=("self",)).filter(device_model=dm, version_key=key).order_by("pk")
                    .first())  # its own row only, as _locked_release (review fix C13)
        if profile is False and not key:
            existing = None  # Add release with no version: VERSION_NEEDED above, never the profile's "already has a profile"
        if existing is not None and existing.removed_on is None:
            errors["version"] = _taken(existing, version)
            existing = None  # refused: the other fields are checked as a new release's
        row = existing if existing is not None else SoftwareRelease(device_model=dm, version_key=key)
        _check_row({**{f: getattr(row, f) for f in RELEASE_FIELDS}, **given}, versioned=bool(key), errors=errors)
        if errors:
            raise ValidationError(errors)
        for name, value in given.items():
            setattr(row, name, value)
        if existing is not None:  # entered in error before: restored, never a second row (the key stays unique)
            row.removed_on, row.removed_by = None, None
            _save(row, by, RESTORED)
            return row
        try:
            with transaction.atomic():
                _save(row, by, ADDED)
        except IntegrityError:
            other = SoftwareRelease.objects.filter(device_model=dm, version_key=key).first()
            raise ValidationError({"version": _taken(other, version) if other is not None else VERSION_ON_FILE.format(version=version)})
    return row


def update_release(release, *, by, today: date | None = None, **fields):
    """Change a release's (or the profile's) fields; the version only while no device on site names it (running: a retired or returned
    device's record follows the correction; a new spelling of the same version, by version_key, any time) and (slice 39) no security
    decision names the release, live or ended (DECISION_NAMES, its number only for a reader of decisions, else DECISION_NAMES_UNREAD: a
    decision is immutable, so its version never changes under it), never on the profile, never to a version already on file. The same
    checks as add_release, on the row as it is now (locked) with the change applied. History "Edited" (nothing saved when nothing
    changes). Returns the row; the caller's copy follows it."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = _today(today)
    _unknown(fields)
    errors: dict = {}
    given = _clean(fields, errors, today=today)
    with transaction.atomic():
        lock_register()  # first: the register's writers one at a time (review round 3)
        locked = _locked_release(release)
        if locked.removed_on is not None:
            raise ValidationError({"release": REMOVED_RELEASE})
        new_key = locked.version_key
        if "version" in given and "version" not in errors:
            new_key = version_key(given["version"])
            if locked.is_profile and new_key:
                errors["version"] = PROFILE_VERSIONLESS
            elif not locked.is_profile and not new_key:
                errors["version"] = VERSION_NEEDED
            elif new_key != locked.version_key:
                other = (SoftwareRelease.objects.filter(device_model_id=locked.device_model_id, version_key=new_key)
                         .exclude(pk=locked.pk).first())
                named = _decision_number(locked)
                if running(locked):  # devices on site, as the panel counts (a retired device's record follows the correction)
                    errors["version"] = VERSION_NAMED
                elif named:  # slice 39: a decision names this version, live or ended (immutable: its words keep the version)
                    errors["version"] = DECISION_NAMES.format(number=named) if _reads_decisions(by) else DECISION_NAMES_UNREAD
                elif other is not None:
                    errors["version"] = _taken(other, given["version"])
        _check_row({**{f: getattr(locked, f) for f in RELEASE_FIELDS}, **given}, versioned=not locked.is_profile, errors=errors)
        if errors:
            raise ValidationError(errors)
        changed = [name for name, value in given.items() if getattr(locked, name) != value]
        if changed:
            for name in changed:
                setattr(locked, name, given[name])
            if "version" in changed:
                locked.version_key = new_key
            try:
                with transaction.atomic():
                    _save(locked, by, EDITED)
            except IntegrityError:
                raise ValidationError({"version": VERSION_ON_FILE.format(version=given.get("version", locked.version))}) from None
    _sync(release, locked)
    return locked


def running(release) -> int:
    """How many devices on site (rules.on_site: not retired, ours and temporary) of the release's model name it: the count the Models
    panel shows beside it, and the one Entered in error and a version change are refused by (review fix C4). A retired or returned
    device's record that names it never counts: its facts stay as they were, so nobody could set it to another; it keeps naming the
    release, which the rules then read as not recorded."""
    from .rules import on_site

    return DeviceSecurity.objects.filter(
        release=release, asset_id__in=on_site().filter(device_model_id=release.device_model_id).values("pk")).count()


def _running_words(named: int) -> str:
    return ("1 device runs this release: set it to another first." if named == 1 else
            f"{named} devices run this release: set them to another first.")


OPEN_TO_INSTALL = "{number} is open to install this release: complete or cancel it first."  # slice 38
DECISION_LIVE = "{number} is decided for this release: end it first."  # slice 39
# A reader of decisions who may not end one (permissions.can_decide: the README's IT security role, Device security Edit with Equipment
# View) reads its number, and who may end it (review 39 skeptics S9)
DECISION_LIVE_READ = "{number} is decided for this release: ending it needs Device security Approve and Equipment View."
DECISION_NAMES = "{number} names this version: add the new version instead."  # slice 39
# The same two refusals for a user who may not read decisions (permissions.can_view_devices: Device security View and Equipment View),
# without the decision's number, as the API's destroy refusals word them (review 39 F7)
DECISION_LIVE_UNREAD = "A security decision is in force for this release: ending it needs Device security Approve and Equipment View."
DECISION_NAMES_UNREAD = "A security decision names this version: add the new version instead."


def _reads_decisions(by) -> bool:
    """Whether a refusal may name a security decision's number to `by`: a reader of decisions (permissions.can_view_devices); None is the
    system (the seed, tests)."""
    return by is None or perms.can_view_devices(by)


def _open_update_number(release) -> str:
    """The lowest number of an open software update work order (open, in progress, or awaiting parts) whose target is `release`, or ""
    (slice 38: its picker hides a release entered in error, and its completion could not name it)."""
    from apps.workorders.models import OPEN_STATUSES

    from .models import SoftwareUpdate

    return (SoftwareUpdate.objects.filter(target_release_id=release.pk, work_order__status__in=OPEN_STATUSES).order_by("work_order__number")
            .values_list("work_order__number", flat=True).first()) or ""


def _decision_number(release, *, live: bool = False) -> str:
    """The lowest number of a security decision (slice 39) naming `release` (only a live one with `live`), or ""."""
    from .models import DeviceSecurityDecision

    rows = DeviceSecurityDecision.objects.filter(release_id=release.pk)
    if live:
        rows = rows.filter(ended_on__isnull=True)
    return rows.order_by("number").values_list("number", flat=True).first() or ""


def removal_refusal(release, *, by=None) -> str:
    """Why Entered in error is refused for `release` now, in remove_release's words (a screen says it before it is tried), or "": already
    removed, the profile, devices on site naming it, (slice 38) an open software update work order to install it (OPEN_TO_INSTALL), or
    (slice 39) a live security decision for it (DECISION_LIVE, its number only when `by` reads decisions, else DECISION_LIVE_UNREAD; and
    "end it first" only when `by` may end it, else DECISION_LIVE_READ; None is the system). A cancelled, completed, or recorded update
    naming it never stops it, nor an ended decision: those read the release as it was ("(entered in error since)")."""
    if release.removed_on is not None:
        return ALREADY_REMOVED
    if release.is_profile:
        return PROFILE_KEPT
    named = running(release)
    if named:
        return _running_words(named)
    number = _open_update_number(release)
    if number:
        return OPEN_TO_INSTALL.format(number=number)
    decided = _decision_number(release, live=True)
    if not decided:
        return ""
    if not _reads_decisions(by):
        return DECISION_LIVE_UNREAD
    return DECISION_LIVE.format(number=decided) if by is None or perms.can_decide(by) else DECISION_LIVE_READ.format(number=decided)


def remove_release(release, *, by, today: date | None = None):
    """Entered in error: refused while a device on site names it (running: as the Models panel counts; a retired or returned device's
    record keeps naming it), while an open software update work order is to install it (slice 38: the register lock held, its openers
    wait), or for the profile, which is cleared field by field (removal_refusal's words). Sets removed_on (today) and removed_by, history
    "Entered in error"; the row stays (history keeps it), hidden from lists and pickers."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = _today(today)
    with transaction.atomic():
        lock_register()  # first: the register's writers one at a time (review round 3)
        locked = _locked_release(release)
        refusal = removal_refusal(locked, by=by)
        if refusal:
            raise ValidationError({"release": refusal})
        locked.removed_on, locked.removed_by = today, by
        _save(locked, by, ENTERED_IN_ERROR)
    _sync(release, locked)
    return locked


# --- a device's facts -----------------------------------------------------------------------------------------------------------------

def _asset(asset):
    from apps.equipment.models import Asset

    pk = _pk(asset, Asset)
    found = Asset.objects.filter(pk=pk).select_related("device_model").first() if pk is not None else None
    if found is None:
        raise ValidationError({"asset": CHOOSE_DEVICE})
    return found


def _gone(asset) -> str:
    """A retired device's refusal (a temporary one returned to its owner in its own words), or ""."""
    from apps.equipment.models import AssetStatus
    from apps.equipment.services import owner_maintains

    if asset.status != AssetStatus.RETIRED:
        return ""
    if owner_maintains(asset):
        return f"{asset.tag} was returned to its owner: its facts stay as they were."
    return f"{asset.tag} is retired: its facts stay as they were."


def _named_release(release, model_id, message: str) -> SoftwareRelease:
    """A versioned, not removed release of the model, locked (the lock order's first row after the register lock)."""
    locked = _locked_release(release, message=message)
    if not locked.version_key or locked.removed_on is not None or locked.device_model_id != model_id:
        raise ValidationError({"release": message})
    return locked


def _locked_devices(ids) -> list:
    """The devices' rows held FOR KEY SHARE in tag order until the transaction ends, then read: after the release's row, before any
    security row (the lock order, review fixes C2 and C3, rounds 2 and 3). KEY SHARE waits for every writer holding a device FOR UPDATE
    (a model or status change, an inspection's pass, a work order's Start or completion: the module docstring), whose result is then
    read here, never a copy from before the wait; plain saves of a device (a contract's Add model) never wait on it. Tag order is the
    other multi-device writers' (Asset's ordering: contracts, AEM's pull-in), so the two wait in turn rather than deadlock. Django has no
    FOR KEY SHARE, so it is the same query's SQL with the clause, read as devices in one query; SQLite locks nothing."""
    from django.db import connection

    from apps.equipment.models import Asset

    qs = Asset.objects.filter(pk__in=list(ids)).order_by("tag")  # the tenant's devices (the manager's filter is in the SQL)
    if connection.vendor != "postgresql":
        return list(qs)
    sql, params = qs.query.sql_with_params()
    return list(Asset.objects.raw(f"{sql} FOR KEY SHARE OF {connection.ops.quote_name(Asset._meta.db_table)}", params))  # one query


REGISTER_LOCK = "cyber_register"  # the facility's register lock (a Sequence row that never counts)


def lock_register() -> None:
    """Hold the facility's register lock until the transaction ends (review round 3): every register writer here takes it first, so two
    of them (a bulk set and a single record on the same new device, two edits of one release) run one after the other, never as a race.
    model_changed never takes it: it runs inside update_asset with the device's row already held (a writer of devices waits on its
    devices, never on this). A row of its own that nothing else locks, as equipment.services.lock_tags; SQLite runs one writer at a time."""
    from apps.core.models import Sequence
    from apps.tenants.context import get_current_tenant

    tenant = get_current_tenant()
    if tenant is None:
        raise RuntimeError("lock_register() needs a tenant context")
    try:
        with transaction.atomic():  # a savepoint: two first-ever requests may both try to create the row
            Sequence.unscoped.get_or_create(tenant=tenant, key=REGISTER_LOCK)  # unscoped + explicit tenant, as Sequence.next
    except IntegrityError:
        pass
    Sequence.unscoped.select_for_update().get(tenant=tenant, key=REGISTER_LOCK)


def _lock_conflict(error: OperationalError) -> bool:
    """PostgreSQL ended this transaction for another holding its rows (a deadlock, a serialization failure, a lock not available)."""
    cause = error.__cause__
    return (getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)) in LOCK_CONFLICTS


def _network(value, errors: dict):
    if value is KEEP:
        return KEEP
    text = value.strip() if isinstance(value, str) else ("" if value is None else None)
    if text is None or text not in ("", *Network.values):
        errors["network"] = NETWORK_WORDS
        return KEEP
    return text


def _since_check(day, installed_on, errors: dict, *, today: date) -> None:
    if day is None or "release_since" in errors:
        return
    if day > today:
        errors["release_since"] = "Running it since cannot be after today."
    elif installed_on is not None and day < installed_on:
        from apps.cybersecurity.rules import day_words

        errors["release_since"] = f"Running it since cannot be before the device was installed ({day_words(installed_on)})."


def _reason(release_changed: bool, release) -> str:
    if not release_changed:
        return FACTS_RECORDED
    return f"{FACTS_RECORDED}: release set to {release.version}" if release is not None else f"{FACTS_RECORDED}: release cleared"


def record_facts(asset, *, by, today: date | None = None, release=KEEP, release_since=KEEP, network=KEEP, checked_on=None):
    """One device's facts: the versioned release of its model it runs (None clears it, and its day), since when (today by default when
    the release changes; never after today, never before the device was installed; with no release, none), its network fact (blank: as
    the model discloses), and the day checked (today by default; never after today), checked by `by`. Refused for a retired device (or
    one returned to its owner) and a device or release of another facility. The register lock first, then the release, then the device's row (its
    model and status read again there: a model change that commits meanwhile refuses the old model's release, CHOOSE_VERSION), then its
    security row, got or created (a create that loses a race reads the row back, locked). History "Facts recorded" (": release set to
    <version>" or ": release cleared" when it changes). Returns the device's DeviceSecurity row."""
    _check(by, perms.can_edit_devices, FACTS_PERMISSION)
    today = _today(today)
    device = _asset(asset)
    gone = _gone(device)
    if gone:
        raise ValidationError({"asset": gone})
    errors: dict = {}
    since = KEEP
    if release_since is not KEEP:
        since = _day(release_since, "release_since", "Running it since", errors, today=today, happened=True)
    network = _network(network, errors)
    checked = _day(checked_on, "checked_on", "The day checked", errors, today=today, happened=True) if checked_on is not None else today
    if checked is None and "checked_on" not in errors:
        checked = today
    if release is None and since not in (KEEP, None):
        errors["release_since"] = NO_RELEASE_SINCE
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        lock_register()  # first: the register's writers one at a time (review round 3)
        locked = None
        if release is not KEEP and release is not None:
            locked = _named_release(release, device.device_model_id, CHOOSE_VERSION)
        # The device's row, locked: a model change in progress is waited for, then seen (never name the old model's release)
        fresh = _locked_devices([device.pk])
        if not fresh:
            raise ValidationError({"asset": CHOOSE_DEVICE})
        device = fresh[0]
        gone = _gone(device)
        if gone:
            raise ValidationError({"asset": gone})
        if locked is not None and device.device_model_id != locked.device_model_id:
            raise ValidationError({"release": CHOOSE_VERSION})

        def apply(sec: DeviceSecurity) -> str:
            current = sec.release_id
            if release is not KEEP:
                sec.release = locked
            changed = sec.release_id != current
            if sec.release_id is None:
                sec.release_since = None
            elif since not in (KEEP, None):
                sec.release_since = since
            elif changed:
                sec.release_since = today
            problems: dict = {}
            if sec.release_id is None and since not in (KEEP, None):
                problems["release_since"] = NO_RELEASE_SINCE
            elif sec.release_id is not None:
                _since_check(sec.release_since if (changed or since not in (KEEP, None)) else None, device.installed_on, problems, today=today)
            if problems:
                raise ValidationError(problems)
            if network is not KEEP:
                sec.network = network
            sec.checked_on, sec.checked_by = checked, by
            return _reason(changed, locked)

        sec = DeviceSecurity.objects.select_for_update().filter(asset_id=device.pk).first()
        if sec is not None:
            _save(sec, by, apply(sec))
            return sec
        sec = DeviceSecurity(asset=device)
        reason = apply(sec)
        try:
            with transaction.atomic():
                _save(sec, by, reason)
        except IntegrityError:  # another record created it meanwhile: read it back, locked, and record on it
            sec = DeviceSecurity.objects.select_for_update().get(asset_id=device.pk)
            _save(sec, by, apply(sec))
    return sec


def record_facts_for(model, assets, *, by, today: date | None = None, release, release_since=None, network=KEEP):
    """Set one model's devices (at most BULK_MAX, all of `model`, on site) to `release` (a versioned, not removed release of `model`),
    since `release_since` (today by default for a device whose release changes; never after today, never before a device was
    installed), with `network` when given, checked today by `by`: each changed device saved with history ("Set with N devices of
    <model>"), in one transaction; a device already so is left as it is. The register lock first, then the release, then the devices' rows in
    tag order (their models and statuses read there: a model change committed meanwhile is refused in words), then their security rows in
    asset pk order (a missing one inserted after its device's row is held). More than BULK_MAX devices are refused, worded by whether a
    department would narrow them (TOO_MANY, or TOO_MANY_IN_ONE when they are all one department's). A security row created by another
    record meanwhile, or PostgreSQL ending the transaction in a deadlock with another writer of these devices, answers RACE, saving
    nothing. Returns how many changed. Two queries a changed device (its row, its history)."""
    _check(by, perms.can_edit_devices, FACTS_PERMISSION)
    today = _today(today)
    dm = _model(model)
    errors: dict = {}
    since = _day(release_since, "release_since", "Running it since", errors, today=today, happened=True)
    network = _network(network, errors)
    ids, seen = [], set()
    from apps.equipment.models import Asset, AssetStatus

    for a in assets:
        pk = _pk(a, Asset)
        if pk is None:
            errors["assets"] = CHOOSE_DEVICES
            break
        if pk not in seen:
            seen.add(pk)
            ids.append(pk)
    if "assets" not in errors and not ids:
        errors["assets"] = NO_DEVICES
    elif "assets" not in errors and len(ids) > BULK_MAX:
        departments = set(Asset.objects.filter(pk__in=ids).order_by().values_list("department_id", flat=True).distinct()[:2])
        errors["assets"] = TOO_MANY_IN_ONE.format(count=len(ids)) if len(departments) == 1 else TOO_MANY
    if release is None:
        errors["release"] = CHOOSE_MODEL_VERSION
    if errors:
        raise ValidationError(errors)
    try:
        with transaction.atomic():
            lock_register()  # first: the register's writers one at a time (review round 3)
            locked = _named_release(release, dm.pk, CHOOSE_MODEL_VERSION)
            devices = _locked_devices(ids)  # after the release, before any security row: their model and status as they are now
            if len(devices) != len(ids):
                raise ValidationError({"assets": CHOOSE_DEVICES})
            other = sorted(a.tag for a in devices if a.device_model_id != dm.pk)
            gone = sorted(a.tag for a in devices if a.status == AssetStatus.RETIRED)
            if other:
                errors["assets"] = f"{_tags(other)} {'is' if len(other) == 1 else 'are'} not {dm.manufacturer} {dm.model}: set one model's devices at a time."
            elif gone:
                errors["assets"] = f"{_tags(gone)} {'is' if len(gone) == 1 else 'are'} no longer on site: their facts stay as they were."
            if since is not None and "release_since" not in errors:
                if since > today:
                    errors["release_since"] = "Running it since cannot be after today."
                else:
                    later = sorted(a.tag for a in devices if a.installed_on is not None and a.installed_on > since)
                    if later:
                        errors["release_since"] = (f"{_tags(later)} {'was' if len(later) == 1 else 'were'} installed after this day: set "
                                                   f"{'it' if len(later) == 1 else 'them'} apart, or choose a later day.")
            if errors:
                raise ValidationError(errors)
            records = {s.asset_id: s for s in DeviceSecurity.objects.select_for_update().filter(asset_id__in=ids).order_by("asset_id")}
            reason = f"Set with {len(devices)} device{'s' if len(devices) != 1 else ''} of {dm.manufacturer} {dm.model}"
            changed = 0
            for device in devices:  # tag order (_locked_devices)
                sec = records.get(device.pk) or DeviceSecurity(asset=device)
                new_release = sec.release_id != locked.pk
                day = since if since is not None else (today if new_release else sec.release_since)
                net = sec.network if network is KEEP else network
                if not sec._state.adding and not new_release and day == sec.release_since and net == sec.network:
                    continue
                sec.release, sec.release_since, sec.network = locked, day, net
                sec.checked_on, sec.checked_by = today, by
                _save(sec, by, reason)
                changed += 1
    except IntegrityError:  # a device's row created by another record meanwhile: nothing here was saved
        raise ValidationError({"assets": RACE}) from None
    except OperationalError as e:  # a deadlock with another writer of these devices (an import chunk, in its own order): nothing saved
        if not _lock_conflict(e):
            raise
        raise ValidationError({"assets": RACE}) from None
    return changed


def _tags(tags: list, shown: int = 5) -> str:
    more = len(tags) - shown
    return ", ".join(tags[:shown]) + (f" and {more} more" if more > 0 else "")


@transaction.atomic
def model_changed(asset, *, by=None) -> bool:
    """A device's model changed (equipment.services.update_asset, with its work and row locked): a release of its old model no longer
    describes it, so the release and its day are cleared, with history. Returns whether one was cleared."""
    sec = DeviceSecurity.objects.select_for_update().filter(asset=asset).first()
    if sec is None or sec.release_id is None or sec.release.device_model_id == asset.device_model_id:
        return False
    sec.release, sec.release_since = None, None
    _save(sec, by, DEVICE_MODEL_CHANGED)
    return True
