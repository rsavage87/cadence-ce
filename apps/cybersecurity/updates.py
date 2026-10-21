"""
Software updates (slice 38, beyond the mock): every write of apps.cybersecurity.models.SoftwareUpdate. FDA's 2016 postmarket guidance
calls a routine cybersecurity update an enhancement, "not considered a repair"; WRHA, AAMI's CMMS Collaborative, and HHS 405(d) HICP 2023
(9.L.A) keep security maintenance as work of its own. No rule requires any of this, and Cadence never says one does.

- An update work order (WoType.SOFTWARE_UPDATE) is opened only here (open_update for one device, open_updates for a release's devices),
  with a fixed, neutral problem (UPDATE_PROBLEM, PATCHES_PROBLEM: no version, no reference) and its record, holding the target: a
  release of the device's model, or the manufacturer's patches with a reference. Work orders' doors (the list, its CSV, the drawer, the
  print, My work, the Overview, the API, scoped users) see only the type and the problem; the target and the versions are behind Device
  security View and Equipment View (permissions.can_view_devices).
- What it did is recorded when the work order is completed (complete_work_order's `software`: record_at_completion), or confirmed
  afterwards (confirm: the vendor's job, or one completed with the version not confirmed): installed (the release moved), patches (the
  version unchanged), or already (already on the update; nothing installed). A user who may record (rules.release_offer) chooses one,
  or "not confirmed now" (NOT_CONFIRMED); a vendor's account never records, and its completion reads "version not confirmed".
- An install with no work order (the manufacturer remotely, automatic, IT, the department) is recorded by hand (record), and may be
  marked entered in error at Approve (remove). A work order's recorded outcome is cleared at Approve instead (remove), and can be
  confirmed again; the device's facts are never rolled back (Record facts corrects them).
- An update's record states only what the person recording it says the update did (review round 4). Cadence never asks what the device
  ran before: the facts are never rolled back, "since" days are backdated, and a later record may be a correction or a real change, so
  no reading of the records answers it. The device's own History (DeviceSecurity's) shows every change of its release with its reason.
  No outcome is judged against the device's record (_release_checks): installed names a release (a versioned, not removed release of
  the device's model: CHOOSE_RUNS_NOW); patches name none (PATCHES_NO_RELEASE: they leave the version as recorded), and run the device's
  recorded release when the facts are written, else none (later_kept); already names the release it ran, the target on a release target
  (NOT_THE_TARGET, NOT_THE_TARGET_DONE; with none sent it takes the target, and a patches target needs one: CHOOSE_RUNS_NOW).
- The device's facts follow what was recorded (_facts), never backwards: a record dated before the device's facts (checked_on or
  release_since after it) is kept as a record only (later_kept), at a confirmation or a hand record dated in the past. Written: installed
  X, the release X since the day; patches, the day checked; already X, the day checked, and the release X since the day (found then) when
  the record does not name X; who checked, always.
- One install is recorded once by hand: record refuses an install equal to a live record of the device (a hand row, or a work order's
  outcome on its completed or closed work order), on the same day: DUPLICATE (_duplicate: installed by the release installed; patches by
  the reference in any letter case, never the version). A rollback then a reinstall recorded the same day is refused as the duplicate it
  matches (Record facts records the device's release then). A work order's outcome is never refused for a hand row's.
- A scoped user who sends an outcome learns only RECORD_PERMISSION (review round 3), before whether the work order has a record or the
  device is on site.
- Where the outcome came from (review fix C15) is the work order as it was when the outcome was recorded (on_vendor_service,
  vendor_report), never as it is later.

Lock order (every writer here and every register writer in services.py; review of design38, critique38_rules C):
  1. the facility's register lock (services.lock_register)
  2. work orders, in lock_work order (workorders.services.lock_work(asset_id, wo.pk)), when a writer names one
  3. work order numbering (create_work_order's Sequence), when a writer opens one
  4. releases, FOR UPDATE of their own rows (services._locked_release)
  5. devices: FOR KEY SHARE in tag order (services._locked_devices), or as the work order's own transaction holds them FOR UPDATE
  6. security rows (DeviceSecurity)
  7. update rows (SoftwareUpdate)
Nothing takes the register lock after a work order's or a device's row. complete_work_order takes it first exactly when its `software`
choice records an outcome (UpdateChoice.records), before lock_work; KEEP and "not confirmed now" take none. equipment.services.update_asset
-> services.model_changed never takes it and never writes an update row. The batch never locks devices before numbering (a failed PM's
completion holds the numbering and waits for its device): each device is taken FOR KEY SHARE (services._locked_devices) right after its
work order's insert, and its model, status, and hold are read from that row. The insert itself holds nothing (review fixes C3, C4): Django
creates PostgreSQL's foreign keys DEFERRABLE INITIALLY DEFERRED, so a row an insert names is checked, and taken KEY SHARE, only at COMMIT.
A transaction PostgreSQL ends for a lock conflict (services._lock_conflict: equipment's reinstate and use-before-inspection take a device's
row and then numbering, the known meeting) answers services.RACE in words, never a 500, and so does a deferred key failing at COMMIT (an
IntegrityError: a device deleted meanwhile, the openers' backstop).

As each writer takes them:
- complete_work_order recording (record_at_completion): register, lock_work, the work order, (its test equipment, a failed PM's numbering
  never: an update has neither), the device FOR UPDATE (change_status), then the chosen release, the security row, the update row.
- confirm: register, lock_work (the work order and the row's outcome read again there), the release, the device FOR KEY SHARE, the
  security row, the update row.
- record: register, the release, the device FOR KEY SHARE, the security row, the new row (the duplicate read under the register).
- remove: register, lock_work for a work order's row, the update row.
- open_update / open_updates: register, the target release, then per device (tag order) numbering and the insert, the device FOR KEY
  SHARE (its model, status, and hold read from it; a device deleted meanwhile is refused or skipped), the new row, the assignment (the
  new work order's own row).

Every function here checks its level itself (permissions.py) and refuses in words keyed by field (a ValidationError); `by` None is the
system (the seed, tests). History reasons never carry a reference. No refusal says "required" (rules.NEVER_WORDS): "Choose ..." or
"Enter ...".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q

from . import permissions as perms
from . import rules, services
from .models import (
    INSTALL_OUTCOMES,
    RECORDED_OUTCOMES,
    UPDATE_REFERENCE_MAX,
    UPDATE_REFERENCE_VALIDATOR,
    DeviceSecurity,
    InstalledBy,
    SoftwareRelease,
    SoftwareUpdate,
    UpdateBasis,
    UpdateOutcome,
    UpdatesBy,
    UpdateTarget,
)
from .services import KEEP

UPDATE_PROBLEM = "Software update: install the manufacturer's update and confirm the version on the device."
PATCHES_PROBLEM = "Software update: apply the manufacturer's patches and confirm the version on the device."
UPDATE_BATCH_MAX = 100  # update work orders opened at once (one release's devices)
NOT_CONFIRMED = "not_confirmed"  # a completion's choice: what the device runs is not confirmed now (records nothing)
OUTCOME_VALUES = (*UpdateOutcome.values, NOT_CONFIRMED)
OPEN_PRIORITIES = ("normal", "high")  # workorders.Priority's NORMAL and HIGH: never "Critical" (a never word) for an update
CLINICAL_ENGINEERING = "Clinical engineering"  # an update work order's requester when the system opens it (by None)

# The default resolution of an update work order completed with an outcome recorded and no resolution typed (never a version: the
# resolution reaches scoped users, the print, and the timeline)
DEFAULT_RESOLUTION = {
    UpdateOutcome.INSTALLED: "Software update installed; version recorded.",
    UpdateOutcome.PATCHES: "Patches applied; version unchanged and recorded.",
    UpdateOutcome.ALREADY: "Checked: already on the update; nothing installed.",
}
# The basis a recorded outcome may name: the validated patch list is a list of patches (never a firmware release); "already" names none
BASES = {
    UpdateOutcome.INSTALLED: (UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.NOT_STATED),
    UpdateOutcome.PATCHES: (UpdateBasis.VALIDATED_LIST, UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.NOT_STATED),
    UpdateOutcome.ALREADY: (),
}

# Refusals (keyed by field)
RECORD_PERMISSION = "Recording what the device runs needs Device security Edit and Equipment View."
OPEN_PERMISSION = "Opening software updates needs Device security Edit, Equipment View, and Work orders Edit."
CORRECT_PERMISSION = "Marking a software update entered in error needs Device security Approve and Equipment View."
CHOOSE_OUTCOME = "Choose what this update did, or Not confirmed now."
OUTCOME_WORDS = "Choose installed, patches, already, or not_confirmed."
SEND_OUTCOME = "Send software_outcome: installed, patches, already, or not_confirmed."
CHOOSE_RUNS_NOW = "Choose the release the device runs now."
REFERENCE_NEEDED = "Enter the manufacturer's patch list and its month or revision, e.g. ICAR-2026-09."
BASIS_WORDS = "Choose the basis for this update."
RECORDED_MEANWHILE = "This update was recorded meanwhile."
HELD = "{tag} is held as evidence for an incident: its software stays as it is until the incident releases it."
ALREADY_OPEN = "{tag} already has an open software update ({number})."
ALREADY_RUNS = "{tag} already runs {version}."
# check_choice, confirm, record
ONLY_UPDATES = "Only a software update records what it did."
NOT_FROM_REGISTER = "This work order was not opened from the Device security screen: nothing is recorded on it."
DUPLICATE = "This install is already recorded on the device's Security tab: {words}."
PATCHES_NO_RELEASE = "Patches leave the version as recorded: choose no release."
NOT_THE_TARGET = "The device runs {version}, not {target}: keep the work order open, or cancel it."
# Confirm version's (its work order is completed: it cannot be kept open)
NOT_THE_TARGET_DONE = "Already on the update means already on {target}, not {version}: choose Installed if it changed, or Patches applied."
VALIDATED_PATCHES_ONLY = "The manufacturer's validated patch list is for patches applied: choose another basis."
NO_BASIS = "Already on the update names no basis: leave it blank."
REFERENCE_LENGTH = f"Keep the reference to {UPDATE_REFERENCE_MAX} characters."
CHOOSE_UPDATE = "Choose a software update of this facility."
CONFIRM_WHAT = "Confirm version is for an update work order completed with the version not confirmed."
CONFIRM_OUTCOME = "Choose what this update did: installed, patches applied, or already on the update."
REOPENED = "{number} is not completed now: its completion records what it did."
HAND_OUTCOME = "Choose installed or patches applied: an install recorded by hand is one of them."
INSTALLER_WORDS = "Choose who installed it."
APPLIED_NEEDED = "Enter the day it was installed."
BEFORE_INSTALL = "Installed on cannot be before the device was installed ({day})."
NOTHING_RECORDED = "Nothing is recorded on this update."
# opening
CHOOSE_TARGET = "Choose the release to install."
TARGET_WORDS = "Choose a release of the model, or the manufacturer's patches."
NO_TARGET_RELEASE = "Patches leave the version unchanged: choose no release to install."
NO_TARGET_REFERENCE = "A release to install names no patch list: leave it blank."
PRIORITY_WORDS = "Choose normal or high."
DUE_NEEDED = "Enter the day it is due."
DUE_PAST = "Due cannot be before today."
DUE_FAR = f"Enter a due day within {services.HORIZON_YEARS} years."
CHOOSE_TECHNICIAN = "Choose an active technician of this facility."
ASSIGN_PERMISSION = "Assigning needs Work orders Approve: leave Assign to blank."
OWNER_INSTALLS = "{tag} is a {kind}: its owner installs its updates, so leave Assign to blank."
INSTALLER_NOT_CE = "Updates to this model are installed here by {who}: leave Assign to blank."
MODEL_CHANGED = "{tag}'s model changed meanwhile: choose a release of its model."
CHOOSE_FROM = "Choose releases of this model to update from."
FROM_NEEDED = "Choose the releases to update from, or the devices with no release recorded."
FROM_TARGET = "Devices on {version} already run it: leave it out."
NONE_SELECTED = "No device of this model on site runs the releases chosen."
CHOOSE_DEPARTMENT = "Choose a department of this facility."
TOO_MANY = f"Open at most {UPDATE_BATCH_MAX} updates at once: choose a department to narrow them."
TOO_MANY_IN_ONE = (f"Open at most {UPDATE_BATCH_MAX} updates at once: these {{count}} devices are all in one department, so choose fewer "
                   "releases to update from, or open them on each device's Security tab.")
# Why opened work orders stay unassigned (UpdateBatch.unassigned_reason)
ASSIGN_NEEDS = "Assigning needs Work orders Approve: a manager assigns them, or a technician takes one."
NOBODY_CHOSEN = "Nobody was chosen to assign: a manager assigns them, or a technician takes one."
INSTALLED_HERE = "Left unassigned: updates to this model are installed here by {who}."
# History reasons (never a reference)
OPENED_WITH = "Opened with {number}"
RECORDED_AT = "Recorded at completion of {number}"
CONFIRMED = "Confirmed"
RECORDED = "Recorded"
ENTERED_IN_ERROR = services.ENTERED_IN_ERROR  # "Entered in error": a hand row
OUTCOME_IN_ERROR = "Outcome entered in error"  # a work order's row
FACTS_AT = "Facts recorded at {number}"  # the device's security row, at a completion
FACTS_CONFIRMED = "Facts recorded when {number} was confirmed"
FACTS_BY_HAND = "Facts recorded with an update recorded by hand"


@dataclass(frozen=True)
class UpdateChoice:
    """What a completion, a confirmation, or a record says the update did: `outcome` (an UpdateOutcome value, or NOT_CONFIRMED), the
    release the device runs now (`release_id`, a release's id as text or None), the manufacturer's `reference` (a token), and the
    `basis` (an UpdateBasis value). Checked by check_choice; nothing here is trusted until then."""

    outcome: str
    release_id: str | None = None
    reference: str = ""
    basis: str = ""

    @property
    def records(self) -> bool:
        """Whether this choice records an outcome (and so takes the register lock first): installed, patches, or already."""
        return self.outcome in RECORDED_OUTCOMES


FIELDS = ("outcome", "release", "reference", "basis")


def choice_from(data, errors: dict, prefix: str = "software_"):
    """The choice a form's or the API's fields send (`<prefix>outcome`, `<prefix>release`, `<prefix>reference`, `<prefix>basis`): KEEP
    when none is sent (or each is blank), else an UpdateChoice. A value that is not text, or an outcome not in OUTCOME_VALUES, is added
    to `errors` keyed by its field (and KEEP returned); fields sent with no outcome are refused keyed `<prefix>outcome`."""
    values = {}
    for name in FIELDS:
        key = prefix + name
        raw = data.get(key) if hasattr(data, "get") else None
        if raw is None:
            values[name] = ""
            continue
        if not isinstance(raw, str):
            errors[key] = "Send this as text."
            continue
        values[name] = raw.strip()
    if any(prefix + name in errors for name in FIELDS):
        return KEEP
    if not any(values.values()):
        return KEEP
    if not values["outcome"]:
        errors[prefix + "outcome"] = CHOOSE_OUTCOME
        return KEEP
    if values["outcome"] not in OUTCOME_VALUES:
        errors[prefix + "outcome"] = OUTCOME_WORDS
        return KEEP
    return UpdateChoice(outcome=values["outcome"], release_id=values["release"] or None, reference=values["reference"],
                        basis=values["basis"])


@dataclass(frozen=True)
class Checked:
    """check_choice's answer for a choice that records an outcome, for record_at_completion: the outcome, the release the person states
    it runs after (a SoftwareRelease: installed's and already's; None for patches, which run the device's recorded release), the reference
    and the basis as cleaned, and the record's target release (None for patches). `records` is always True."""

    outcome: str
    release: object | None
    reference: str = ""
    basis: str = ""
    target_release: object | None = None

    @property
    def records(self) -> bool:
        return True


@dataclass(frozen=True)
class UpdateBatch:
    """What opening updates did: work orders created, how many were left unassigned (and why, in words), and the devices skipped: held
    as evidence, with an open update already, or whose model changed meanwhile (each a list of tags)."""

    created: int
    unassigned: int = 0
    unassigned_reason: str = ""
    held: tuple = ()
    already_open: tuple = ()
    changed: tuple = ()
    work_orders: tuple = ()  # the work orders opened, in tag order


# --- small parts ----------------------------------------------------------------------------------------------------------------------

def _token(value, field: str, errors: dict, *, needed: bool = False) -> str:
    """A reference (the manufacturer's patch list, notice, or field change number): a token of at most UPDATE_REFERENCE_MAX characters;
    REFERENCE_NEEDED when `needed` and blank."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[field] = "Enter it as text."
        return ""
    text = value.strip()
    if not text:
        if needed:
            errors[field] = REFERENCE_NEEDED
        return ""
    if len(text) > UPDATE_REFERENCE_MAX:
        errors[field] = REFERENCE_LENGTH
        return text
    try:
        UPDATE_REFERENCE_VALIDATOR(text)
    except ValidationError as e:
        errors[field] = " ".join(e.messages)
    return text


def _fields(choice: UpdateChoice, errors: dict) -> tuple[str, str]:
    """The choice's reference and basis for its outcome (one of RECORDED_OUTCOMES), cleaned: patches need a reference; installed and
    patches a basis of BASES[outcome]; already none."""
    reference = _token(choice.reference, "software_reference", errors, needed=choice.outcome == UpdateOutcome.PATCHES)
    raw = "" if choice.basis is None else choice.basis
    basis = raw.strip() if isinstance(raw, str) else None
    if choice.outcome == UpdateOutcome.ALREADY:
        if basis is None or basis:
            errors["software_basis"] = NO_BASIS
        return reference, ""
    if basis == UpdateBasis.VALIDATED_LIST and basis not in BASES[choice.outcome]:
        errors["software_basis"] = VALIDATED_PATCHES_ONLY
    elif basis not in BASES[choice.outcome]:
        errors["software_basis"] = BASIS_WORDS
    return reference, basis or ""


def _names(release, model_id) -> bool:
    """A release a device of `model_id` may be read as running: versioned, not entered in error, of that model."""
    return (release is not None and bool(release.version_key) and release.removed_on is None
            and release.device_model_id == model_id)


def _release_read(release_id, model_id, errors: dict):
    """The release a choice names (an id as text, a UUID, or a SoftwareRelease), read without a lock (check_choice: the completion
    holds the register when the choice records), if it is a versioned, not removed release of the device's model; else CHOOSE_RUNS_NOW
    keyed software_release and None."""
    pk = services._pk(release_id, SoftwareRelease)
    found = SoftwareRelease.objects.filter(pk=pk).order_by("pk").first() if pk is not None else None
    if not _names(found, model_id):
        errors["software_release"] = CHOOSE_RUNS_NOW
        return None
    return found


def _release_locked(release_id):
    """The release a choice names, FOR UPDATE of its own row (services._locked_release; another facility's or unknown id refused,
    keyed software_release). Its model is checked once the device is held."""
    return services._locked_release(release_id, field="software_release", message=CHOOSE_RUNS_NOW)


def _patches_name_none(choice, errors: dict) -> bool:
    """Patches never take a release from the person (review round 4: they leave the version as recorded): a patches choice naming one is
    refused, keyed software_release (PATCHES_NO_RELEASE), at every door (check_choice, confirm, record; the API through the completion).
    Returns whether the choice names a release to read (installed or already naming one)."""
    if not choice.release_id:
        return False
    if choice.outcome == UpdateOutcome.PATCHES:
        errors["software_release"] = PATCHES_NO_RELEASE
        return False
    return True


def _release_checks(outcome: str, *, release, target, errors: dict, done: bool = False):
    """The release the person states the device runs after the outcome (review round 4: never judged against the device's record), given
    the release the choice names (`release`: None when none was sent; patches name none, _patches_name_none) and the record's `target` (a
    release, or None for patches and a hand row):
    - installed: the release named (CHOOSE_RUNS_NOW for none);
    - patches: None (they run the device's recorded release when the facts are written: _writer);
    - already: the release named, else the target when it is a release (CHOOSE_RUNS_NOW when neither: a patches target needs one), and
      on a release target the target itself (NOT_THE_TARGET, or NOT_THE_TARGET_DONE for a work order completed already: `done`, Confirm
      version).
    Problems keyed software_release."""
    key = "software_release"
    if outcome == UpdateOutcome.INSTALLED:
        if release is None:
            errors[key] = CHOOSE_RUNS_NOW
        return release
    if outcome == UpdateOutcome.PATCHES:
        return None
    runs = release or target
    if runs is None:
        errors[key] = CHOOSE_RUNS_NOW
        return None
    if target is not None and runs.pk != target.pk:
        errors[key] = (NOT_THE_TARGET_DONE if done else NOT_THE_TARGET).format(version=runs.version, target=rules.release_name(target))
    return runs


def _recorded(sec, device):
    """The release the device's security row names, as the rules read it (rules.release_of): versioned, not removed, of its model."""
    release = sec.release if sec is not None and sec.release_id else None
    return release if _names(release, device.device_model_id) else None


def _newer(sec, day: date) -> bool:
    """Whether the device's facts are dated after `day` (checked_on or release_since): a record of `day` never moves them back."""
    return (sec.checked_on is not None and sec.checked_on > day) or (sec.release_since is not None and sec.release_since > day)


def _facts(sec, *, outcome: str, after, day: date, by, prefix: str) -> str | None:
    """Write what `outcome` says on the device's security row `sec` (not saved), on `day` (review round 4):
    - installed: the release `after`, since the day (always: the day it was installed);
    - patches: nothing but the day checked;
    - already: when the record does not name `after`, the release `after` since the day (found then); else nothing but the day checked;
    the day checked and who, always. Returns the history reason (prefix, ": release set to <version>" when the release changed), or None
    when the facts are newer than `day` (kept: later_kept)."""
    if _newer(sec, day):
        return None
    changed = False
    if outcome in (UpdateOutcome.INSTALLED, UpdateOutcome.ALREADY):
        changed = sec.release_id != after.pk
        if outcome == UpdateOutcome.INSTALLED or changed:
            sec.release, sec.release_since = after, day
    sec.checked_on, sec.checked_by = day, by
    return f"{prefix}: release set to {after.version}" if changed else prefix


def _writer(device, *, outcome: str, release, day: date, by, prefix: str, state: dict):
    """The apply function _write_security calls with the device's security row: the release the update runs after (`release`, as
    stated, for installed and already; for patches the device's recorded release when the facts are written, else None), then the facts
    (_facts). Sets state["after"] and state["later_kept"] (the facts newer than `day`: kept, not written)."""

    def apply(sec):
        written = not _newer(sec, day)
        after = release if outcome != UpdateOutcome.PATCHES else (_recorded(sec, device) if written else None)
        reason = _facts(sec, outcome=outcome, after=after, day=day, by=by, prefix=prefix)
        state.update(after=after, later_kept=reason is None)
        return reason

    return apply


def _write_security(device, by, apply):
    """The device's security row FOR UPDATE, got or created as services.record_facts does (a create that loses a race reads the row
    back, locked): `apply(row)` writes on it and returns the history reason, or None to save nothing (a row it did not find is then not
    created). Returns the row, or None."""
    sec = DeviceSecurity.objects.select_for_update().filter(asset_id=device.pk).first()
    if sec is not None:
        reason = apply(sec)
        if reason:
            services._save(sec, by, reason)
        return sec
    sec = DeviceSecurity(asset=device)
    reason = apply(sec)
    if not reason:
        return None
    try:
        with transaction.atomic():
            services._save(sec, by, reason)
    except IntegrityError:  # another writer created it meanwhile: read it back, locked, and write on it
        sec = DeviceSecurity.objects.select_for_update().get(asset_id=device.pk)
        reason = apply(sec)
        if reason:
            services._save(sec, by, reason)
    return sec


def _set_outcome(row, *, outcome: str, after, reference: str, basis: str, day: date, by, later_kept: bool, wo=None) -> None:
    """The outcome's fields on `row` (not saved; OUTCOME_FIELDS), with where it came from (review fix C15): `wo`, the work order as it is
    at this recording (a completion's, after its checks_reference is set; confirm's, read again under lock_work), on vendor service or
    not and its vendor's report number then. A hand row (no `wo`) and a cleared outcome have
    neither. The words read these, never the work order's flags later (a reopened job moved to a vendor and completed by them never
    takes credit for an outcome recorded in-house)."""
    row.outcome, row.release_after = outcome, after
    row.reference, row.basis, row.applied_on = reference, basis, day
    row.recorded_by, row.later_kept = by, later_kept
    vendor = bool(outcome) and wo is not None and bool(wo.vendor_service)
    row.on_vendor_service, row.vendor_report = vendor, (wo.checks_reference or "") if vendor else ""


def _duplicate(device, *, outcome: str, after, reference: str, day: date):
    """A live record of the same install on the device, or None (one query): a hand row not entered in error, or a work order's outcome
    on its completed or closed work order (what its Security tab shows as done), with the same outcome on the same day (review round 4,
    nothing about the device's record):
    - installed: the same release installed (a rollback then a reinstall recorded the same day is refused as this duplicate: Record facts
      records the device's release then);
    - patches: the same reference (in any letter case), whatever version each runs (patches leave the version as recorded: never part of
      what was done).
    Read under the register lock (every writer of an outcome holds it), with what its words read."""
    from apps.workorders.models import WoStatus

    qs = (SoftwareUpdate.objects.filter(asset_id=device.pk, outcome=outcome, applied_on=day)
          .filter(Q(work_order__isnull=True, removed_on__isnull=True) | Q(work_order__status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))))
    if outcome == UpdateOutcome.PATCHES:
        qs = qs.filter(reference__iexact=reference)
    else:
        qs = qs.filter(release_after=after)
    return qs.select_related("work_order", "target_release", "release_after").order_by("created_at", "pk").first()


def _update_row(update):
    """The update, resolved through this facility (another facility's or unknown id refused, keyed update), with its work order and
    target read."""
    pk = services._pk(update, SoftwareUpdate)
    found = (SoftwareUpdate.objects.filter(pk=pk).select_related("work_order", "target_release").order_by("pk").first()
             if pk is not None else None)
    if found is None:
        raise ValidationError({"update": CHOOSE_UPDATE})
    return found


def _race(field: str):
    """A lock conflict PostgreSQL ended this writer's transaction for (services._lock_conflict): RACE, keyed `field`."""
    return ValidationError({field: services.RACE})


# --- the work order's side ------------------------------------------------------------------------------------------------------------

def check_choice(wo, choice, by, errors: dict, *, today: date):
    """complete_work_order's check of `choice` (KEEP, or an UpdateChoice), before its `if errors`, so every problem is reported at once.
    Reads rules.offer_or_why(wo, by), the one predicate (rules.release_offer). In order, each keyed software_outcome:
    - a choice other than KEEP on a work order that is not a software update: ONLY_UPDATES;
    - from a scoped user (apps.workorders.scoping.is_scoped: never Device security): RECORD_PERMISSION, before anything about the record
      or the device (review round 3: a scoped user learns nothing more);
    - on an update with no record (one imported from another system): NOT_FROM_REGISTER;
    - on a device not on site: services._gone's words;
    - from a user with no offer (one without Device security Edit and Equipment View): RECORD_PERMISSION (never dropped);
    - KEEP from a user (None: the system) on an update with nothing recorded yet: CHOOSE_OUTCOME (KEEP with no offer, a vendor's account
      among them, records nothing and is never refused).
    Then the outcome (NOT_CONFIRMED records nothing: its release, reference, and basis are not read), never judged against the device's
    record (review round 4: the record states what the person says the update did):
    - installed: a versioned, not removed release of the device's model (software_release: CHOOSE_RUNS_NOW), and a basis of
      BASES[installed] (software_basis);
    - patches: a reference (software_reference: REFERENCE_NEEDED; a token), a basis of BASES[patches], and no release (software_release:
      PATCHES_NO_RELEASE: they leave the version as recorded);
    - already: the release it ran (a versioned, not removed release of the model), or with none sent the target when it is a release
      (CHOOSE_RUNS_NOW on a patches target), the target itself on a release target (NOT_THE_TARGET), and no basis (NO_BASIS).
    Returns a Checked for record_at_completion, or KEEP / the NOT_CONFIRMED choice as given (KEEP also after a refusal). No query for
    KEEP on another type."""
    from apps.workorders import scoping
    from apps.workorders.models import WoType

    if choice is KEEP:
        if wo.type != WoType.SOFTWARE_UPDATE or by is None:
            return KEEP
        offer = rules.release_offer(wo, by)
        if offer is not None and offer.needed:
            errors["software_outcome"] = CHOOSE_OUTCOME
        return KEEP
    if not isinstance(choice, UpdateChoice) or choice.outcome not in OUTCOME_VALUES:
        errors["software_outcome"] = OUTCOME_WORDS
        return KEEP
    if wo.type != WoType.SOFTWARE_UPDATE:
        errors["software_outcome"] = ONLY_UPDATES
        return KEEP
    if by is not None and scoping.is_scoped(by):  # review round 3: before the record and the device, so nothing more is learnt
        errors["software_outcome"] = RECORD_PERMISSION
        return KEEP
    offer, why = rules.offer_or_why(wo, by)
    if offer is None:
        errors["software_outcome"] = {rules.NO_RECORD: NOT_FROM_REGISTER, rules.NOT_ON_SITE: services._gone(wo.asset)}.get(
            why, RECORD_PERMISSION)
        return KEEP
    if choice.outcome == NOT_CONFIRMED:
        return choice
    mine: dict = {}
    reference, basis = _fields(choice, mine)
    release = _release_read(choice.release_id, wo.asset.device_model_id, mine) if _patches_name_none(choice, mine) else None
    after = None
    if "software_release" not in mine:
        after = _release_checks(choice.outcome, release=release, target=offer.target_release, errors=mine)
    if mine:
        errors.update(mine)
        return KEEP
    return Checked(outcome=choice.outcome, release=after, reference=reference, basis=basis, target_release=offer.target_release)


def record_at_completion(wo, asset, checked, by, today: date):
    """Write what `checked` (check_choice's) says the update did on `wo`'s record, inside complete_work_order after it moved `wo` to
    completed (and applied its test equipment): that transaction holds the register, the device's work and `wo`, and the device FOR
    UPDATE. Takes the release stated FOR UPDATE (installed's and already's: of the device's model, read from the held row), the device's
    security row (got or created, as services.record_facts), then the update row FOR UPDATE; writes outcome, applied_on (today),
    release_after (the release stated; patches, the device's recorded release: _writer), reference, basis, recorded_by, later_kept (a
    completion is dated today, after any fact), and where it came from (`wo` now: on vendor service, its checks_reference); then the facts
    (_facts: installed, the release since today; patches, checked today; already, checked today and the release since today when the
    record does not name it), history "Recorded at completion of <number>" on the update row and "Facts recorded at <number>" (": release
    set to <version>" when it changed) on the security row. A re-completion replaces the row's outcome (with history). Returns the update
    row."""
    from apps.equipment.models import Asset

    device = Asset.objects.get(pk=asset.pk)  # held FOR UPDATE by the completion (change_status): its model as it is now
    release = None
    if checked.release is not None:
        release = _release_locked(checked.release)
        if not _names(release, device.device_model_id):
            raise ValidationError({"software_release": CHOOSE_RUNS_NOW})
    state: dict = {}
    _write_security(device, by, _writer(device, outcome=checked.outcome, release=release, day=today, by=by,
                                        prefix=FACTS_AT.format(number=wo.number), state=state))
    row = SoftwareUpdate.objects.select_for_update().get(work_order_id=wo.pk)
    _set_outcome(row, outcome=checked.outcome, after=state["after"], reference=checked.reference, basis=checked.basis, day=today, by=by,
                 later_kept=state["later_kept"], wo=wo)
    services._save(row, by, RECORDED_AT.format(number=wo.number))
    return row


def default_resolution(outcome: str) -> str:
    """The resolution an update work order completed with `outcome` recorded and none typed gets (DEFAULT_RESOLUTION); "" for none."""
    return DEFAULT_RESOLUTION.get(outcome, "")


# --- the Security tab's doors ---------------------------------------------------------------------------------------------------------

def confirm(update, choice, *, by, today: date | None = None):
    """Record what a completed or closed update work order did, when its completion did not (the vendor's job, or "not confirmed now"):
    at permissions.can_edit_devices. Locks: the register, lock_work(asset, its work order), the work order read again (completed or
    closed: else REOPENED; its outcome still blank: else RECORDED_MEANWHILE), the release, the device FOR KEY SHARE (its model and
    status read again: a device no longer on site refused in services._gone's words), the security row, the update row. The choice is
    checked as check_choice checks it (KEEP and NOT_CONFIRMED refused: CONFIRM_OUTCOME; never against the device's record: review round
    4; patches name no release: PATCHES_NO_RELEASE; already on a release target is the target: NOT_THE_TARGET_DONE, the work order
    being completed); applied_on is the work order's completed_on; the facts are written only when the device's checked_on and
    release_since are null or on or before it (_facts), else the row alone, with later_kept (the toast: "a later record of the device is
    kept"; patches then name no version). Where it came from is the work order as it is now (_set_outcome: on vendor service, its report
    number: review fix C15).
    History "Confirmed" on the row, "Facts recorded when <number> was confirmed" on the security row. `today` is not read: the day is
    the work order's. A lock conflict answers services.RACE, keyed software_outcome. Returns the update row (the caller's copy follows
    it)."""
    from apps.workorders import services as wo_services
    from apps.workorders.models import WorkOrder, WoStatus, WoType

    services._check(by, perms.can_edit_devices, RECORD_PERMISSION)
    row = _update_row(update)
    if row.work_order_id is None:
        raise ValidationError({"software_outcome": CONFIRM_WHAT})
    errors: dict = {}
    reference = basis = ""
    named = False
    if not isinstance(choice, UpdateChoice) or choice.outcome not in RECORDED_OUTCOMES:
        errors["software_outcome"] = CONFIRM_OUTCOME
    else:
        reference, basis = _fields(choice, errors)
        named = _patches_name_none(choice, errors)
        if named and services._pk(choice.release_id, SoftwareRelease) is None:
            errors["software_release"] = CHOOSE_RUNS_NOW
    if errors:
        raise ValidationError(errors)
    try:
        with transaction.atomic():
            services.lock_register()
            wo_services.lock_work(row.asset_id, row.work_order_id)
            wo = WorkOrder.objects.get(pk=row.work_order_id)  # held by lock_work: its status as it is now
            if wo.type != WoType.SOFTWARE_UPDATE or wo.status not in (WoStatus.COMPLETED, WoStatus.CLOSED):
                raise ValidationError({"software_outcome": REOPENED.format(number=wo.number)})
            # every writer of the outcome holds this work order first (a completion, confirm, remove): its outcome as it is now
            if SoftwareUpdate.objects.filter(pk=row.pk).exclude(outcome="").exists():
                raise ValidationError({"software_outcome": RECORDED_MEANWHILE})
            release = _release_locked(choice.release_id) if named else None
            device = _held_device(row.asset_id, "software_outcome")
            if release is not None and not _names(release, device.device_model_id):
                raise ValidationError({"software_release": CHOOSE_RUNS_NOW})
            problems: dict = {}
            runs = _release_checks(choice.outcome, release=release, target=row.target_release, errors=problems, done=True)
            if problems:
                raise ValidationError(problems)
            if runs is not None and not _names(runs, device.device_model_id):  # the target taken: entered in error since
                raise ValidationError({"software_release": CHOOSE_RUNS_NOW})
            day = wo.completed_on
            state: dict = {}
            _write_security(device, by, _writer(device, outcome=choice.outcome, release=runs, day=day, by=by,
                                                prefix=FACTS_CONFIRMED.format(number=wo.number), state=state))
            locked = SoftwareUpdate.objects.select_for_update().get(pk=row.pk)
            _set_outcome(locked, outcome=choice.outcome, after=state["after"], reference=reference, basis=basis, day=day, by=by,
                         later_kept=state["later_kept"], wo=wo)
            services._save(locked, by, CONFIRMED)
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise _race("software_outcome") from None
    services._sync(update, locked)
    return locked


def _held_device(asset_id, field: str):
    """The device FOR KEY SHARE (services._locked_devices), read once held: refused in services._gone's words, keyed `field`, when it
    is no longer on site."""
    held = services._locked_devices([asset_id])
    if not held:
        raise ValidationError({"asset": services.CHOOSE_DEVICE})
    device = held[0]
    gone = services._gone(device)
    if gone:
        raise ValidationError({field: gone})
    return device


def _applied_day(value, device, errors: dict, *, today: date):
    day = services._day(value, "applied_on", "Installed on", errors, today=today, happened=True)
    if day is None:
        if "applied_on" not in errors:
            errors["applied_on"] = APPLIED_NEEDED
        return None
    if "applied_on" not in errors and device.installed_on is not None and day < device.installed_on:
        errors["applied_on"] = BEFORE_INSTALL.format(day=rules.day_words(device.installed_on))
    return day


def record(asset, choice, *, installed_by: str, applied_on, by, today: date | None = None):
    """An install recorded by hand, with no work order (installed or patches: HAND_OUTCOME otherwise), installed by `installed_by`
    (InstalledBy: INSTALLER_WORDS otherwise) on `applied_on` (a day or its ISO text, not after today, not before the device was
    installed: BEFORE_INSTALL), at permissions.can_edit_devices. The choice is checked as confirm checks it, with no target and never
    against the device's record (review round 4): installed names a release (CHOOSE_RUNS_NOW), patches none (PATCHES_NO_RELEASE).
    Refused, keyed software_outcome, when it is an install already recorded on the device (DUPLICATE: a live hand row, or a work order's
    outcome on its completed or closed work order, on the same day: installed with the same release, patches with the same reference:
    _duplicate). Locks: the register, the release, the device FOR KEY SHARE (read again; a retired or returned device refused:
    services._gone, keyed asset), the security row, the new row. The facts as confirm writes them, on applied_on (never backwards:
    later_kept). History "Recorded" on the row, "Facts recorded with an update recorded by hand" on the security row. Returns the new
    row."""
    services._check(by, perms.can_edit_devices, RECORD_PERMISSION)
    today = services._today(today)
    device = services._asset(asset)
    gone = services._gone(device)
    if gone:
        raise ValidationError({"asset": gone})
    errors: dict = {}
    reference = basis = ""
    named = False
    if not isinstance(choice, UpdateChoice) or choice.outcome not in INSTALL_OUTCOMES:
        errors["software_outcome"] = HAND_OUTCOME
    else:
        reference, basis = _fields(choice, errors)
        named = _patches_name_none(choice, errors)
        if named and services._pk(choice.release_id, SoftwareRelease) is None:
            errors["software_release"] = CHOOSE_RUNS_NOW
    if not isinstance(installed_by, str) or installed_by not in InstalledBy.values:
        errors["installed_by"] = INSTALLER_WORDS
    day = _applied_day(applied_on, device, errors, today=today)
    if errors:
        raise ValidationError(errors)
    try:
        with transaction.atomic():
            services.lock_register()
            release = _release_locked(choice.release_id) if named else None
            device = _held_device(device.pk, "asset")
            problems: dict = {}
            _applied_day(day, device, problems, today=today)  # the install date as it is now
            if problems:
                raise ValidationError(problems)
            if release is not None and not _names(release, device.device_model_id):
                raise ValidationError({"software_release": CHOOSE_RUNS_NOW})
            _release_checks(choice.outcome, release=release, target=None, errors=problems)
            if problems:
                raise ValidationError(problems)
            # One install is recorded once (the register is held: every writer of an outcome holds it)
            same = _duplicate(device, outcome=choice.outcome, after=release, reference=reference, day=day)
            if same is not None:
                raise ValidationError({"software_outcome": DUPLICATE.format(words=rules.update_words(same))})
            state: dict = {}
            _write_security(device, by, _writer(device, outcome=choice.outcome, release=release, day=day, by=by, prefix=FACTS_BY_HAND,
                                                state=state))
            row = SoftwareUpdate(asset=device, installed_by=installed_by)
            _set_outcome(row, outcome=choice.outcome, after=state["after"], reference=reference, basis=basis, day=day, by=by,
                         later_kept=state["later_kept"])
            services._save(row, by, RECORDED)
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise _race("asset") from None
    return row


def remove(update, *, by, today: date | None = None):
    """Entered in error, at permissions.can_correct_updates: a hand row is marked (removed_on, removed_by; history "Entered in error");
    a work order's row has its outcome cleared (OUTCOME_FIELDS; history "Outcome entered in error"), and Confirm version is offered
    again (judged as any first recording: nothing of the outcome cleared is read again, review round 4). Refused (NOTHING_RECORDED,
    keyed software_outcome) for a row with nothing recorded or already marked. Locks: the register, lock_work(asset, its work order)
    for a work order's row, the update row. The device's facts are never touched (Record facts corrects them). Returns the row (the
    caller's copy follows it)."""
    services._check(by, perms.can_correct_updates, CORRECT_PERMISSION)
    today = services._today(today)
    row = _update_row(update)
    try:
        with transaction.atomic():
            services.lock_register()
            if row.work_order_id is not None:
                from apps.workorders import services as wo_services

                wo_services.lock_work(row.asset_id, row.work_order_id)
            locked = SoftwareUpdate.objects.select_for_update().get(pk=row.pk)
            if locked.removed_on is not None or not locked.outcome:
                raise ValidationError({"software_outcome": NOTHING_RECORDED})
            if locked.work_order_id is None:
                locked.removed_on, locked.removed_by = today, by
                reason = ENTERED_IN_ERROR
            else:
                _set_outcome(locked, outcome="", after=None, reference="", basis="", day=None, by=None, later_kept=False)
                reason = OUTCOME_IN_ERROR
            services._save(locked, by, reason)
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise _race("software_outcome") from None
    services._sync(update, locked)
    return locked


# --- opening updates ------------------------------------------------------------------------------------------------------------------

class _Skip(Exception):
    """A device found, once taken FOR KEY SHARE after its work order's insert, deleted, retired, on another model, or held as evidence:
    open_update refuses it (`field`, `words`), the batch skips it (`kind`: "changed" or "held")."""

    def __init__(self, kind: str, field: str, words: str):
        super().__init__(words)
        self.kind, self.field, self.words = kind, field, words


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _requester(by) -> str:
    """The opener's name, as New work order sets it (str(user): the full name, else the email); CLINICAL_ENGINEERING for the system."""
    from apps.workorders.services import NAME_MAX

    return str(by)[:NAME_MAX] if by is not None else CLINICAL_ENGINEERING


def _due(value, errors: dict, *, today: date):
    """A due day (a date or its ISO text) from today to services.HORIZON_YEARS ahead."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip()) if value.strip() else None
        except ValueError:
            value = False
    if value is None or value is False or isinstance(value, datetime) or not isinstance(value, date):
        errors["due_on"] = DUE_NEEDED
        return None
    if value < today:
        errors["due_on"] = DUE_PAST
    elif value > services._years_ahead(today, services.HORIZON_YEARS):
        errors["due_on"] = DUE_FAR
    return value


def _technician(assign_to, by, errors: dict):
    """The technician `assign_to` names (an active Technician of this facility, or its id), or None for nobody. Only a user who may
    assign (Work orders Approve) names one: else ASSIGN_PERMISSION."""
    if assign_to in (None, ""):
        return None
    from apps.credentials.models import Technician
    from apps.workorders import permissions as wo_perms

    if by is not None and not wo_perms.can_assign(by):
        errors["assign_to"] = ASSIGN_PERMISSION
        return None
    pk = services._pk(assign_to, Technician)
    found = Technician.objects.filter(pk=pk, is_active=True).first() if pk is not None else None
    if found is None:
        errors["assign_to"] = CHOOSE_TECHNICIAN
    return found


def _opening(priority, due_on, assign_to, by, errors: dict, *, today: date):
    if priority not in OPEN_PRIORITIES:
        errors["priority"] = PRIORITY_WORDS
    return _due(due_on, errors, today=today), _technician(assign_to, by, errors)


def _updates_by(model_id) -> str:
    """Who installs updates here for the model (its profile's UpdatesBy), or "" when not recorded."""
    profile = rules.profile(model_id)
    return profile.updates_by if profile is not None else ""


def _open_numbers(asset_ids) -> dict:
    """{device id: the number of its open software update (open, in progress, or awaiting parts), the lowest} (one query). Every open
    software update work order counts, its record or not (review fix C9: one imported from another system has none, and reopened it
    is an open update all the same): the work orders of the type, and any with a record."""
    from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

    out: dict = {}
    for asset_id, number in (WorkOrder.objects.filter(Q(type=WoType.SOFTWARE_UPDATE) | Q(software_update__isnull=False),
                                                      asset_id__in=list(asset_ids), status__in=OPEN_STATUSES)
                             .order_by("number").values_list("asset_id", "number")):
        out.setdefault(asset_id, number)
    return out


def open_number(asset) -> str:
    """The number of the device's open software update (open, in progress, or awaiting parts; its record or not: _open_numbers), the
    lowest, or "": what refuses opening another (ALREADY_OPEN). One query."""
    return _open_numbers([asset.pk]).get(asset.pk, "")


def _assign(wo, device, *, may: bool, updates_by: str, technician, by) -> str:
    """Assign a new update work order as open_update's rule says; returns why it stays unassigned, or "" when assigned:
    - nobody unless `by` may assign (Work orders Approve; None: the system): ASSIGN_NEEDS;
    - a temporary device, or a model whose updates the manufacturer installs here: vendor service (equipment.services.service_vendor);
    - updates_by CE or not recorded: `technician`, or nobody (NOBODY_CHOSEN);
    - IT or the department that uses it: nobody (INSTALLED_HERE, naming them)."""
    from apps.equipment.services import owner_maintains, service_vendor
    from apps.workorders import services as wo_services

    if not may:
        return ASSIGN_NEEDS
    if owner_maintains(device) or updates_by == UpdatesBy.MANUFACTURER:
        wo_services.assign(wo, vendor_name=service_vendor(device), by=by)
        return ""
    if updates_by in (UpdatesBy.CE, ""):
        if technician is None:
            return NOBODY_CHOSEN
        wo_services.assign(wo, technician=technician, by=by)
        return ""
    return INSTALLED_HERE.format(who=_lower_first(UpdatesBy(updates_by).label))


def _open_one(device, *, model_id, target: str, release, reference: str, due: date, priority: str, may: bool, updates_by: str,
              technician, by, today: date):
    """One update work order and its record on `device` (the register and the release held): its number and insert, then the device
    taken FOR KEY SHARE (services._locked_devices: numbering first, the lock order) and its model, status, and hold read from that row
    (_Skip when it is gone, retired, on another model, or held), the record (history "Opened with <number>"), the assignment. Returns
    (work order, why unassigned or "")."""
    from apps.workorders import services as wo_services
    from apps.workorders.models import Source, WoType

    wo = wo_services.create_work_order(asset=device, type=WoType.SOFTWARE_UPDATE, priority=priority,
                                       problem=UPDATE_PROBLEM if target == UpdateTarget.RELEASE else PATCHES_PROBLEM,
                                       requester=_requester(by), source=Source.MANUAL, opened_on=today, due_on=due, created_by=by,
                                       _register_opener=True)
    # Review fixes C3/C4: the insert holds nothing (Django makes PostgreSQL's foreign keys DEFERRABLE INITIALLY DEFERRED: the device is
    # checked only at COMMIT), so the device is taken here, FOR KEY SHARE. A retire, model change, hold, or delete in flight is waited
    # for and then read; one that comes after waits for this transaction, and then sees the update (a retire cancels it, a delete is
    # refused: the device has work).
    held = services._locked_devices([device.pk])
    if not held:  # deleted meanwhile
        raise _Skip("changed", "asset", services.CHOOSE_DEVICE)
    now = held[0]
    gone = services._gone(now)
    if gone:
        raise _Skip("changed", "asset", gone)
    if now.device_model_id != model_id:
        raise _Skip("changed", "target_release", MODEL_CHANGED.format(tag=device.tag))
    if now.incident_hold:
        raise _Skip("held", "asset", HELD.format(tag=device.tag))
    row = SoftwareUpdate(asset=device, work_order=wo, target=target, target_release=release, target_reference=reference, opened_on=today,
                         opened_by=by)
    services._save(row, by, OPENED_WITH.format(number=wo.number))
    return wo, _assign(wo, device, may=may, updates_by=updates_by, technician=technician, by=by)


def _target_release(release_id, model_id, field: str):
    """The release to install, FOR UPDATE of its own row: a versioned, not removed release of the model (CHOOSE_TARGET, keyed `field`)."""
    locked = services._locked_release(release_id, field=field, message=CHOOSE_TARGET)
    if not _names(locked, model_id):
        raise ValidationError({field: CHOOSE_TARGET})
    return locked


def open_update(asset, *, target: str, target_release=None, target_reference: str = "", due_on, priority: str, assign_to=None, by,
                today: date | None = None) -> UpdateBatch:
    """Open one update work order on `asset` (at permissions.can_open_updates): its target a release of the device's model other than
    the recorded one (ALREADY_RUNS), or the manufacturer's patches with a reference (target_reference: REFERENCE_NEEDED; a release target
    names none). Refused for a device not on site (services._gone's words, keyed asset), held as evidence (HELD), or with an open update
    (ALREADY_OPEN); a device waiting for its incoming inspection is allowed. Priority normal or high (PRIORITY_WORDS); due_on from today,
    at most services.HORIZON_YEARS ahead. Locks: the register, the release, create_work_order (type SOFTWARE_UPDATE, source manual, the
    requester the opener's name (CLINICAL_ENGINEERING for the system), the problem UPDATE_PROBLEM or PATCHES_PROBLEM, _register_opener=True:
    its number, then the insert, which holds nothing: review fixes C3/C4), the device FOR KEY SHARE and its model, status, and hold read
    from it (a model changed meanwhile: MODEL_CHANGED keyed target_release; retired: services._gone's words, held: HELD, deleted:
    services.CHOOSE_DEVICE, each keyed asset; all rolled back, the number included), the update row (history "Opened with <number>"), the
    assignment: only when workorders.permissions.can_assign(by) (else unassigned: ASSIGN_NEEDS; `assign_to` then refused,
    ASSIGN_PERMISSION), a temporary device or a model whose updates_by is the manufacturer to vendor service
    (equipment.services.service_vendor), updates_by CE or blank to `assign_to` (an active technician, or nobody: NOBODY_CHOSEN), IT or
    the department to nobody (INSTALLED_HERE); `assign_to` is refused where it would not be used (OWNER_INSTALLS, INSTALLER_NOT_CE).
    A lock conflict, or a deferred key failing at COMMIT (a device deleted meanwhile: the backstop), answers services.RACE, keyed asset.
    Returns an UpdateBatch of one."""
    from apps.equipment.services import owner_maintains
    from apps.workorders import permissions as wo_perms

    services._check(by, perms.can_open_updates, OPEN_PERMISSION)
    today = services._today(today)
    device = services._asset(asset)
    errors: dict = {}
    reference = ""
    if target not in UpdateTarget.values:
        errors["target"] = TARGET_WORDS
    elif target == UpdateTarget.RELEASE:
        if target_release in (None, ""):
            errors["target_release"] = CHOOSE_TARGET
        if target_reference not in (None, ""):
            errors["target_reference"] = NO_TARGET_REFERENCE
    else:
        if target_release not in (None, ""):
            errors["target_release"] = NO_TARGET_RELEASE
        reference = _token(target_reference, "target_reference", errors, needed=True)
    due, technician = _opening(priority, due_on, assign_to, by, errors, today=today)
    if errors:
        raise ValidationError(errors)
    try:
        with transaction.atomic():
            services.lock_register()
            release = (_target_release(target_release, device.device_model_id, "target_release") if target == UpdateTarget.RELEASE
                       else None)
            gone = services._gone(device)
            if gone:
                raise ValidationError({"asset": gone})
            if device.incident_hold:
                raise ValidationError({"asset": HELD.format(tag=device.tag)})
            number = _open_numbers([device.pk]).get(device.pk)
            if number:
                raise ValidationError({"asset": ALREADY_OPEN.format(tag=device.tag, number=number)})
            recorded = rules.release_of(device)
            if release is not None and recorded is not None and recorded.pk == release.pk:
                raise ValidationError({"target_release": ALREADY_RUNS.format(tag=device.tag, version=release.version)})
            updates_by = _updates_by(device.device_model_id)
            if technician is not None and owner_maintains(device):
                raise ValidationError({"assign_to": OWNER_INSTALLS.format(tag=device.tag, kind=device.get_ownership_display().lower())})
            if technician is not None and updates_by not in (UpdatesBy.CE, ""):
                raise ValidationError({"assign_to": INSTALLER_NOT_CE.format(who=_lower_first(UpdatesBy(updates_by).label))})
            try:
                wo, why = _open_one(device, model_id=device.device_model_id, target=target, release=release, reference=reference, due=due,
                                    priority=priority, may=by is None or wo_perms.can_assign(by), updates_by=updates_by,
                                    technician=technician, by=by, today=today)
            except _Skip as skip:
                raise ValidationError({skip.field: skip.words}) from None
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise _race("asset") from None
    except IntegrityError:  # review fix C4's backstop: a device deleted meanwhile fails the work order's deferred key at COMMIT
        raise _race("asset") from None
    return UpdateBatch(created=1, unassigned=1 if why else 0, unassigned_reason=why, work_orders=(wo,))


def _batch_devices(model_id, from_ids: set, include_not_recorded: bool, department) -> list:
    """The release's model's devices on site (rules.on_site: retired and returned ones never), in `department` when given, whose
    recorded release (rules.release_of) is one of `from_ids`, or none when `include_not_recorded`: in tag order, with their records,
    owners' contracts, and tenant read (one query)."""
    qs = (rules.on_site().filter(device_model_id=model_id).select_related("cyber__release", "contract", "tenant").order_by("tag"))
    if department is not None:
        qs = qs.filter(department_id=department.pk)
    out = []
    for device in qs:
        recorded = rules.release_of(device)
        if (recorded is not None and recorded.pk in from_ids) or (recorded is None and include_not_recorded):
            out.append(device)
    return out


def open_updates(release, *, from_releases=(), include_not_recorded: bool = False, department=None, due_on=None,
                 due_at_next_pm: bool = False, priority: str, assign_to=None, by, today: date | None = None) -> UpdateBatch:
    """Open one update work order to `release` (a versioned, not removed release: CHOOSE_TARGET, keyed release) per device of its model
    on site whose recorded release is one of `from_releases` (releases of the model, the target never: CHOOSE_FROM, FROM_TARGET) or not
    recorded, with `include_not_recorded` (FROM_NEEDED with neither), optionally in `department`, in tag order, at most UPDATE_BATCH_MAX
    (TOO_MANY / TOO_MANY_IN_ONE, keyed release; none: NONE_SELECTED, keyed from_releases): at permissions.can_open_updates. Due on
    `due_on` (always given: the day, and the fallback), or with `due_at_next_pm` on each device's next PM date (never before today), else
    `due_on`. Skipped, each counted with its tag: held as evidence, an open update already (its record or not: _open_numbers), a model
    changed (or the device retired or deleted) meanwhile. Locks: the register, the release, then per device (tag order, each in a
    savepoint) numbering, the insert (holding nothing: review fixes C3/C4), the device FOR KEY SHARE with its model, status, and hold read
    from it, the update row, the assignment (open_update's rule; `assign_to` for every device assigned in-house), inside
    assignments.batch() (each technician one email). A lock conflict, or a deferred key failing at COMMIT (the backstop), answers
    services.RACE (keyed release), nothing opened. Returns the UpdateBatch."""
    from apps.equipment.models import Department
    from apps.notifications import assignments
    from apps.workorders import permissions as wo_perms

    services._check(by, perms.can_open_updates, OPEN_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    target_pk = services._pk(release, SoftwareRelease)
    if target_pk is None:
        errors["release"] = CHOOSE_TARGET
    from_ids = set()
    for value in from_releases or ():
        pk = services._pk(value, SoftwareRelease)
        if pk is None:
            errors["from_releases"] = CHOOSE_FROM
            break
        from_ids.add(pk)
    if "from_releases" not in errors and not from_ids and not include_not_recorded:
        errors["from_releases"] = FROM_NEEDED
    dept = None
    if department not in (None, ""):
        pk = services._pk(department, Department)
        dept = Department.objects.filter(pk=pk).first() if pk is not None else None
        if dept is None:
            errors["department"] = CHOOSE_DEPARTMENT
    due, technician = _opening(priority, due_on, assign_to, by, errors, today=today)
    if errors:
        raise ValidationError(errors)
    created, held, already_open, changed, opened, reasons = 0, [], [], [], [], []
    try:
        with transaction.atomic():
            services.lock_register()
            target = services._locked_release(target_pk, field="release", message=CHOOSE_TARGET)
            if not target.version_key or target.removed_on is not None:
                raise ValidationError({"release": CHOOSE_TARGET})
            model_id = target.device_model_id
            if target.pk in from_ids:
                raise ValidationError({"from_releases": FROM_TARGET.format(version=target.version)})
            found = (SoftwareRelease.objects.filter(pk__in=from_ids, device_model_id=model_id, removed_on__isnull=True)
                     .exclude(version_key="").count()) if from_ids else 0
            if found != len(from_ids):
                raise ValidationError({"from_releases": CHOOSE_FROM})
            devices = _batch_devices(model_id, from_ids, include_not_recorded, dept)
            if not devices:
                raise ValidationError({"from_releases": NONE_SELECTED})
            if len(devices) > UPDATE_BATCH_MAX:
                one = len({d.department_id for d in devices}) == 1
                raise ValidationError({"release": TOO_MANY_IN_ONE.format(count=len(devices)) if one else TOO_MANY})
            updates_by = _updates_by(model_id)
            if technician is not None and updates_by not in (UpdatesBy.CE, ""):
                raise ValidationError({"assign_to": INSTALLER_NOT_CE.format(who=_lower_first(UpdatesBy(updates_by).label))})
            may = by is None or wo_perms.can_assign(by)
            numbers = _open_numbers(d.pk for d in devices)
            with assignments.batch():  # each technician hears once, listing every work order, after the commit
                for device in devices:  # tag order
                    if device.incident_hold:
                        held.append(device.tag)
                        continue
                    if device.pk in numbers:
                        already_open.append(device.tag)
                        continue
                    due_here = max(device.next_pm_on, today) if due_at_next_pm and device.next_pm_on is not None else due
                    try:
                        with transaction.atomic():  # a device found changed or held goes back alone, its number with it
                            wo, why = _open_one(device, model_id=model_id, target=UpdateTarget.RELEASE, release=target, reference="",
                                                due=due_here, priority=priority, may=may, updates_by=updates_by, technician=technician,
                                                by=by, today=today)
                    except _Skip as skip:
                        (held if skip.kind == "held" else changed).append(device.tag)
                        continue
                    created += 1
                    opened.append(wo)
                    if why:
                        reasons.append(why)
    except OperationalError as e:  # a deadlock with a writer of these devices against the lock order: nothing opened
        if not services._lock_conflict(e):
            raise
        raise _race("release") from None
    except IntegrityError:  # review fix C4's backstop: a device deleted meanwhile fails a work order's deferred key at COMMIT
        raise _race("release") from None
    return UpdateBatch(created=created, unassigned=len(reasons), unassigned_reason=reasons[0] if reasons else "", held=tuple(held),
                       already_open=tuple(already_open), changed=tuple(changed), work_orders=tuple(opened))
