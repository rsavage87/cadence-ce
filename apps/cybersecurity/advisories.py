"""
Every write of slice 40's security advisories (SecurityAdvisory, AdvisorySource, AdvisoryModel, AdvisoryRelease), and the lock-free
readers that say, before anyone tries, why a write would be refused (build40 sections 4.6 and 6).

Cadence receives no advisory by itself: a person records each one the team takes in, with its publications (sources: a token reference
and an https link, never its text), the models of this facility's catalog it names and their releases, and what it offers. Which devices
it concerns is derived from each device's recorded release (rules.advisory_lines), never stored. No rule requires any of this; HHS 405(d)
HICP 2023 recommends it (rules.HICP_ADVISORIES).

Levels (inside every writer; permissions.py): Edit (can_edit) records advisories, sources, revisions, sets and confirms the models they
name, removes a model, marks one not applicable (every reason checked against the records: not_applicable_refusals), and reopens one
marked not applicable; Close needs can_decide (Approve and Equipment View: it rests on device lines and decisions); reopening a closed one
and entered in error need can_correct_advisories (Approve). `by` None is the system (the seed, tests). Scoped users never.

The contract (as updates.py words its own):
- Field shape (tokens, the CVE pattern, days, choices) is checked before any lock, every problem at once, keyed by field, never
  "required". Every check that reads another row runs after `services.lock_register()` and the advisory's row
  (`services._locked_advisory`, FOR UPDATE of its own row), on rows read there: duplicates, the last live source, live decisions, the
  status, removed_on, the model row's versions, remedy, fix and listed ids, a row's existence (a second removal answers in words).
- Writers save the locked row, never the caller's copy, then `services._sync(given, locked)`. A CVE merge is computed from the locked
  row's list.
- Locks, per writer (the spy pins): record_advisory: register, the number (["register"]); set_model: register, the advisory, the
  releases it names in pk order (_locked_release) (["register", "advisory", "release", ...]); every other writer: register, the advisory
  (["register", "advisory"]). They never take a device, a work order (lock_work), or a model row: AdvisoryModel.device_model takes the
  model KEY SHARE at COMMIT (Django's deferred foreign keys); only a rename or a delete waits on it, and set_model answers an
  IntegrityError or a lock conflict as RACE keyed device_model.
- Close and Not applicable are checks at that moment: equipment's writers (a new device, a model change, a reinstate) and work order
  status moves never take the register, so a device named since surfaces in Needs attention through the same predicates
  (close_refusal, not_applicable_refusals), never here.
- History reasons are fixed sentences with AD numbers only (never an SD number, a work order number, or a tag: the advisory areas are
  Device security View's).
- Tokens (a source's reference and revision, a model row's patch list): `core.validators.reference_token` plus
  `decisions.network_identifier` (one rule; neither rules nor services import this module), in REFERENCE_WORDS.

Slice 40 wave 1A fills in the writers and readers below; the names, signatures, and constants are the contract the doors (wave 2) code
against.
"""
from __future__ import annotations

from datetime import date

from .decisions import network_identifier  # noqa: F401 (the one network-identifier rule; used by the writers)

# --- words (every constant through the never-word scans) ---------------------------------------------------------------------------

REFERENCE_WORDS = ("Enter the reference as its publisher prints it (for example ICSMA-25-030-01), never an address, a VLAN, or a "
                   "port.")
CVE_WORDS = "'{piece}' is not a CVE ID: paste the IDs only."
CVES_ON_OTHER = "{cves} {verb} also on {numbers}: add this as a source of {first}, or record it as its own advisory."
REFERENCE_RECORDED = "{reference} is already recorded as a source of {number}."
LINK_WORDS = "Enter the publisher's https link to its page, never an address on the facility's own network."
ISAC_HELP = "Record only the reference your source lets you share inside the facility."
CHOOSE_MODEL = "Choose a model of this facility's catalog."
CHOOSE_RELEASES = "Choose the releases it names, at least one."
FIX_LISTED = "The fix is never a release it names: choose another release, or untick it."
FIX_NEEDED = "Choose the release the advisory names as the fix."
FIX_ONLY_LISTED = "A release fix names the releases it lists: choose The releases listed."
LAST_SOURCE = "An advisory keeps at least one source: add another first."
NAME_A_MODEL = "Name at least one model first, or mark it not applicable (it names no model in this facility's catalog)."
REOPEN_TO_CHANGE = "Reopen {number} to change the models it names."
MODELS_TO_CONFIRM = "Confirm the models it names first."
ENTERED_IN_ERROR = "{number} was entered in error."
DECISION_UNDER = "{decision} is decided under this advisory: end it first."
DECISION_UNDER_READ = "{decision} is decided under this advisory: ending it needs Device security Approve and Equipment View."
DECISION_UNDER_UNREAD = "A security decision is in force under this advisory: ending it needs Device security Approve and Equipment View."
RACE = "Someone changed this advisory at the same time: open it again and retry."
RECORD_PERMISSION = "Recording security advisories needs Device security Edit."
CLOSE_PERMISSION = "Closing a security advisory needs Device security Approve and Equipment View."
CORRECT_PERMISSION = "Reopening a closed advisory, or marking one entered in error, needs Device security Approve."
NOT_APPLICABLE_NOTE = ("No model is named on this advisory: marking it not applicable records that none of the products it names is in "
                       "this facility's catalog.")

# History reasons (fixed sentences, AD numbers only)
RECORDED, SOURCE_ADDED, SOURCE_REMOVED, REVISION_RECORDED = "Recorded", "Source added", "Source removed", "Revision recorded"
REOPENED_BY_REVISION = "Reopened: a revision changed what it names"
MODELS_SET, MODEL_REMOVED, MODELS_CONFIRMED = "Models set", "Model removed", "Models confirmed"
MARKED_NOT_APPLICABLE, CLOSED, REOPENED, MARKED_IN_ERROR = "Marked not applicable", "Closed", "Reopened", "Entered in error"

# What a revision changed that reopens the models' confirmation (and, on an advisory marked not applicable, moves it back to open)
REOPENING = ("products_changed", "vulnerabilities_added", "fix_available")


# --- the lock-free readers (never select_for_update: a GET door calls them; the writer calls the same one after its locks) ----------

def close_refusal(advisory, today: date, *, user=None) -> str | None:
    """Why the advisory cannot be closed now (words naming up to 10 tags, then "and N more", for `user` with can_view_devices; counts
    otherwise), or None. Also the predicate Needs attention lists a closed advisory by ("devices named since")."""
    raise NotImplementedError("slice 40 wave 1A")


def not_applicable_refusals(advisory, today: date) -> dict:
    """{NotApplicableReason value: words or None}: each reason checked against the records now (rules B2's words)."""
    raise NotImplementedError("slice 40 wave 1A")


def reopen_refusal(advisory, *, user) -> str | None:
    raise NotImplementedError("slice 40 wave 1A")


def entered_in_error_refusal(advisory, *, user) -> str | None:
    raise NotImplementedError("slice 40 wave 1A")


def model_removal_refusal(row, *, user) -> str | None:
    raise NotImplementedError("slice 40 wave 1A")


def source_removal_refusal(source) -> str | None:
    raise NotImplementedError("slice 40 wave 1A")


def set_model_refusal(advisory) -> str | None:
    """Why the models it names cannot be changed now (an advisory not open, or entered in error), or None."""
    raise NotImplementedError("slice 40 wave 1A")


def overlaps(cves, *, source: str = "", reference: str = "", exclude=None) -> dict:
    """{advisory number: [shared CVE IDs and references]} for live advisories (not entered in error) sharing a CVE ID, or the reference
    under another source kind (one query, matched in Python): the Record advisory modal's note before saving."""
    raise NotImplementedError("slice 40 wave 1A")


# --- writers ---------------------------------------------------------------------------------------------------------------------------

def record_advisory(*, received_on, cves, source, reference="", url="", published_on=None, seen=(), by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def update_advisory(advisory, *, received_on=None, cves=None, seen=(), by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def add_source(advisory, *, source, reference="", url="", published_on=None, cves=(), by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def update_source(source_row, *, source, reference="", url="", published_on=None, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def record_revision(source_row, *, revision="", updated_on=None, last_change=(), by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def remove_source(source_row, *, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def set_model(advisory, *, device_model, versions, release_ids=(), remedy, fix_release=None, patches_reference="", confirm=False, by,
              today=None):
    raise NotImplementedError("slice 40 wave 1A")


def remove_model(row, *, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def confirm_models(advisory, *, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def mark_not_applicable(advisory, *, reason, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def close(advisory, *, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def reopen(advisory, *, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")


def mark_entered_in_error(advisory, *, by, today=None):
    raise NotImplementedError("slice 40 wave 1A")
