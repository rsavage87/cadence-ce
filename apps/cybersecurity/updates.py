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
- The device's facts follow what was recorded (installed: its release since the day; patches and already: the day checked), never
  backwards: a record dated before the device's facts (checked_on or release_since after it) is kept as a record only (later_kept).

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
completion holds the numbering and waits for its device): each device is held by its work order's insert (KEY SHARE), then its model is
read again. A transaction PostgreSQL ends for a lock conflict (services._lock_conflict: equipment's reinstate and use-before-inspection take
a device's row and then numbering, the known meeting) answers services.RACE in words, never a 500.

Every function here checks its level itself (permissions.py) and refuses in words keyed by field (a ValidationError); `by` None is the
system (the seed, tests). History reasons never carry a reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import RECORDED_OUTCOMES, UpdateBasis, UpdateOutcome
from .services import KEEP

UPDATE_PROBLEM = "Software update: install the manufacturer's update and confirm the version on the device."
PATCHES_PROBLEM = "Software update: apply the manufacturer's patches and confirm the version on the device."
UPDATE_BATCH_MAX = 100  # update work orders opened at once (one release's devices)
NOT_CONFIRMED = "not_confirmed"  # a completion's choice: what the device runs is not confirmed now (records nothing)
OUTCOME_VALUES = (*UpdateOutcome.values, NOT_CONFIRMED)

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


# --- the work order's side ------------------------------------------------------------------------------------------------------------

def check_choice(wo, choice, by, errors: dict, *, today: date):
    """complete_work_order's check of `choice` (KEEP, or an UpdateChoice), before its `if errors`, so every problem is reported at once.
    Reads rules.release_offer(wo, by), the one predicate:
    - a choice other than KEEP from a user with no offer (a scoped user, one without Device security Edit and Equipment View, a work order
      that is not a register-opened update, a device not on site) is refused, keyed software_outcome (RECORD_PERMISSION, or _gone's words
      for a device not on site), never dropped;
    - an offer with no outcome recorded yet and KEEP, from a user (None: the system), is refused, keyed software_outcome (CHOOSE_OUTCOME);
    - installed: a versioned, not removed release of the device's model (software_release: CHOOSE_RUNS_NOW), other than the recorded one
      ("It already ran <v> before: choose Already on the update, or Patches applied."), and a basis of BASES[installed];
    - patches: a reference (software_reference: REFERENCE_NEEDED; a token), a basis of BASES[patches], and the release when sent equal to
      the recorded one ("Patches leave the version unchanged: choose installed if it changed."); not sent: the recorded one, or none;
    - already: the recorded release (or one chosen when none is recorded), the target when the target is a release ("The device runs
      <v>, not <target>: keep the work order open, or cancel it."), and no basis.
    Returns the checked choice for record_at_completion (the release resolved), or KEEP / NOT_CONFIRMED as given. Wave 1."""
    if choice is KEEP:
        return KEEP  # wave 1: the "required" rule (an offer with nothing recorded)
    raise NotImplementedError


def record_at_completion(wo, asset, checked, by, today: date):
    """Write what `checked` (check_choice's) says the update did on `wo`'s record, inside complete_work_order after it moved `wo` to
    completed (and applied its test equipment): that transaction holds the register, the device's work and `wo`, and the device FOR
    UPDATE. Takes the chosen release FOR UPDATE, the device's security row (got or created, as services.record_facts), then the update
    row FOR UPDATE; writes outcome, applied_on (today), release_before (the recorded release), release_after, reference, basis,
    recorded_by; then the facts (installed: the release since today, checked today; patches: checked today; already: checked today, and
    the release when none was recorded), history "Recorded at completion of <number>" on the update row and "Facts recorded at <number>"
    on the security row. A re-completion replaces the row's outcome (with history). Returns the update row. Wave 1."""
    raise NotImplementedError


def default_resolution(outcome: str) -> str:
    """The resolution an update work order completed with `outcome` recorded and none typed gets (DEFAULT_RESOLUTION); "" for none."""
    return DEFAULT_RESOLUTION.get(outcome, "")


# --- the Security tab's doors ---------------------------------------------------------------------------------------------------------

def confirm(update, choice, *, by, today: date | None = None):
    """Record what a completed or closed update work order did, when its completion did not (the vendor's job, or "not confirmed now"):
    at permissions.can_edit_devices. Locks: the register, lock_work(asset, its work order), the work order read again (completed or
    closed, its outcome still blank: else RECORDED_MEANWHILE), the release, the device FOR KEY SHARE (its model and status read again),
    the security row, the update row. The choice is checked as check_choice checks it (never NOT_CONFIRMED); applied_on is the work
    order's completed_on; the facts are written only when the device's checked_on and release_since are null or on or before it, else
    later_kept. History "Confirmed". Returns the update row. Wave 1."""
    raise NotImplementedError


def record(asset, choice, *, installed_by: str, applied_on, by, today: date | None = None):
    """An install recorded by hand, with no work order (installed or patches), installed by `installed_by` (InstalledBy) on `applied_on`
    (a day, not after today, not before the device was installed): at permissions.can_edit_devices. Locks: the register, the release, the
    device FOR KEY SHARE (read again; a retired or returned device refused: services._gone), the security row, the new row. The facts
    as confirm writes them (never backwards). History "Recorded". Returns the new row. Wave 1."""
    raise NotImplementedError


def remove(update, *, by, today: date | None = None):
    """Entered in error, at permissions.can_correct_updates: a hand row is marked (removed_on, removed_by; history "Entered in error");
    a work order's row has its outcome cleared (OUTCOME_FIELDS; history "Outcome entered in error"), and Confirm version is offered
    again. Locks: the register, lock_work(asset, its work order) for a work order's row, the update row. The device's facts are never
    touched (Record facts corrects them). Returns the row. Wave 1."""
    raise NotImplementedError


# --- opening updates ------------------------------------------------------------------------------------------------------------------

def open_update(asset, *, target: str, target_release=None, target_reference: str = "", due_on, priority: str, assign_to=None, by,
                today: date | None = None) -> UpdateBatch:
    """Open one update work order on `asset` (at permissions.can_open_updates): its target a release of the device's model other than
    the recorded one (ALREADY_RUNS), or the manufacturer's patches with a reference. Refused for a device not on site, held as evidence
    (HELD), or with an open update (ALREADY_OPEN); a device waiting for its incoming inspection is allowed. Priority normal or high;
    due_on from today, at most services.HORIZON_YEARS ahead. Locks: the register, the release, create_work_order (type SOFTWARE_UPDATE,
    source manual, the requester the opener's name, the problem UPDATE_PROBLEM or PATCHES_PROBLEM, _register_opener=True: its number,
    then the device held by the insert), the device's model read again (changed: refused keyed target_release, all rolled back), the
    update row (history "Opened with <number>"), the assignment: only when workorders.permissions.can_assign(by) (else unassigned), a
    temporary device or a model whose updates_by is the manufacturer to vendor service (equipment.services.service_vendor), updates_by
    CE or blank to `assign_to` (a technician, or nobody), IT or the department to nobody (the result says who installs updates here).
    Returns an UpdateBatch of one. Wave 1."""
    raise NotImplementedError


def open_updates(release, *, from_releases=(), include_not_recorded: bool = False, department=None, due_on=None,
                 due_at_next_pm: bool = False, priority: str, assign_to=None, by, today: date | None = None) -> UpdateBatch:
    """Open one update work order to `release` (a versioned, not removed release) per device of its model on site whose recorded release
    is one of `from_releases` (or not recorded, with `include_not_recorded`), optionally in `department`, in tag order, at most
    UPDATE_BATCH_MAX (services.TOO_MANY / TOO_MANY_IN_ONE's words with this cap): at permissions.can_open_updates. Due on `due_on`, or
    with `due_at_next_pm` on each device's next PM date (never before today), else `due_on`. Skipped, with words: held as evidence, an
    open update already, a model changed meanwhile. Locks: the register, the release, then per device (tag order) numbering, the insert
    (the device held), its model read again, the update row, the assignment (open_update's rule), inside assignments.batch(). A lock
    conflict answers services.RACE. Returns the UpdateBatch. Wave 1."""
    raise NotImplementedError
