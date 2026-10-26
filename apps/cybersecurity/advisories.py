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
  IntegrityError or a lock conflict as RACE keyed device_model. Every writer answers a lock conflict PostgreSQL ends it for
  (services._lock_conflict) as RACE in words, never a 500.
- Close and Not applicable are checks at that moment: equipment's writers (a new device, a model change, a reinstate) and work order
  status moves never take the register, so a device named since surfaces in Needs attention through the rules readers, never here: a
  closed advisory's through rules.named_since_lines (rules.close_blockers, close_refusal's predicate, but the lines under a decision
  past its review date, which surface under Needs attention's advisory decisions past their review date), a not-applicable one's
  through rules.not_applicable_blocks (not_applicable_refusals' predicate). Every register writer that moves a device's release or
  response (services.record_facts, updates.record and confirm, a completion that records, decisions) takes the register first, so
  Close and Not applicable read them settled.
- History reasons are fixed sentences with AD numbers only (never an SD number, a work order number, or a tag: the advisory areas are
  Device security View's).
- Tokens (a source's reference and revision, a model row's patch list): `core.validators.reference_token` plus
  `decisions.network_identifier` (one rule; neither rules nor services import this module), in REFERENCE_WORDS.

The readers (never select_for_update: a GET door calls them, outside any transaction; each writer calls the same one on the row it
locked): close_refusal, not_applicable_refusals and not_applicable_refusal, reopen_refusal, entered_in_error_refusal,
model_removal_refusal, source_removal_refusal, set_model_refusal, and overlaps (the CVE IDs and references another live advisory shares:
offered on Record advisory, never refused once `seen` names the advisories shown).
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q

from apps.equipment.models import DeviceModel

from . import permissions as perms
from . import rules, services
from .decisions import network_identifier
from .models import (
    ADVISORY_REFERENCE_MAX,
    ADVISORY_REFERENCE_VALIDATOR,
    ADVISORY_URL_MAX,
    REVISION_MAX,
    REVISION_VALIDATOR,
    UPDATE_REFERENCE_MAX,
    UPDATE_REFERENCE_VALIDATOR,
    AdvisoryChange,
    AdvisoryModel,
    AdvisoryRelease,
    AdvisoryRemedy,
    AdvisorySource,
    AdvisorySourceKind,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    DeviceSecurityDecision,
    NotApplicableReason,
    SecurityAdvisory,
    SoftwareRelease,
)

KEEP = services.KEEP  # an argument left as it is

# --- words (every constant through the never-word scans) ---------------------------------------------------------------------------

REFERENCE_WORDS = ("Enter the reference as its publisher prints it (for example ICSMA-25-030-01), never an address, a VLAN, or a "
                   "port.")
CVE_WORDS = "'{piece}' is not a CVE ID: paste the IDs only."
CVE_LIST_WORDS = "Paste the CVE IDs only, separated by spaces, commas, or new lines."
CVES_ON_OTHER = "{cves} {verb} also on {numbers}: add this as a source of {first}, or record it as its own advisory."
REFERENCE_RECORDED = "{reference} is already recorded as a source of {number}."
LINK_WORDS = "Enter the publisher's https link to its page, never an address on the facility's own network."
ISAC_HELP = "Record only the reference your source lets you share inside the facility."
CHOOSE_MODEL = "Choose a model of this facility's catalog."
CHOOSE_RELEASES = "Choose the releases it names, at least one."
CHOOSE_MODEL_RELEASE = "Choose a release of the {model} on file."
FIX_LISTED = "The fix is never a release it names: choose another release, or untick it."
FIX_NEEDED = "Choose the release the advisory names as the fix."
FIX_ONLY_LISTED = "A release fix names the releases it lists: choose The releases listed."
FIX_ONLY_RELEASE = "Only A release from the manufacturer names a fix: choose it, or leave the fix blank."
RELEASES_ONLY_LISTED = "Only The releases listed names releases: choose it, or untick them."
PATCHES_ONLY = "Only The manufacturer's validated patches names a patch list: choose it, or leave the list blank."
VERSIONS_WORDS = "Choose which releases it names."
REMEDY_WORDS = "Choose what the advisory offers."
SOURCE_WORDS = "Choose who published it from the list."
CHANGE_WORDS = "Choose what changed from the list."
REASON_WORDS = "Choose why it is not applicable here."
RECEIVED_NEEDED = "Enter the day it was received."
REVISION_NEEDED = "Enter the revision or the day updated, as the publisher prints them."
UPDATED_BEFORE = "The day updated cannot be before the day published ({day})."
PUBLISHED_AFTER = "The day published cannot be after the day updated ({day})."
# The day received after a decision's day decided (live or ended), in two readers' words: a reader of decisions (services._reads_decisions:
# permissions.can_view_devices, or the system) reads the earliest one's number and day; anyone else neither (critique40_rules S2/S3)
RECEIVED_AFTER_DECISION = "{decision} under this advisory was decided {day}: the day received cannot be after it."
RECEIVED_AFTER_DECISION_UNREAD = ("A security decision under this advisory was decided before that day: the day received cannot be after "
                                  "its day decided. Reading it needs Device security View and Equipment View.")
LENGTH_WORDS = "Keep it to {limit} characters."
LAST_SOURCE = "An advisory keeps at least one source: add another first."
CHOOSE_SOURCE = "Choose a source of this advisory."
SOURCE_REMOVED_WORDS = "This source was removed from {number}."
CHOOSE_ROW = "Choose a model this advisory names."
ROW_GONE = "This model is no longer named on {number}."
NAME_A_MODEL = "Name at least one model first, or mark it not applicable (it names no model in this facility's catalog)."
REOPEN_TO_CHANGE = "Reopen {number} to change the models it names."
MODELS_TO_CONFIRM = "Confirm the models it names first."
ENTERED_IN_ERROR = "{number} was entered in error."
ALREADY_CLOSED = "{number} is already closed."
CLOSED_NOW = "{number} is closed: reopen it first."
NOT_APPLICABLE_NOW = "{number} is marked not applicable here: reopen it first."
ALREADY_NOT_APPLICABLE = "{number} is already marked not applicable here."
ALREADY_OPEN = "{number} is open."
# A live decision under the advisory, in three readers' words (critique40_rules S3, as services' release refusals): who may end it
# (permissions.can_decide), who reads decisions but may not end one (can_view_devices), anyone else (no number, no scope)
DECISION_UNDER = "{decision} is decided under this advisory: end it first."
DECISION_UNDER_READ = "{decision} is decided under this advisory: ending it needs Device security Approve and Equipment View."
DECISION_UNDER_UNREAD = "A security decision is in force under this advisory: ending it needs Device security Approve and Equipment View."
DECISION_FOR = "{decision} is decided for {scope} under this advisory: end it first."
DECISION_FOR_READ = ("{decision} is decided for {scope} under this advisory: ending it needs Device security Approve and Equipment "
                     "View.")
RACE = "Someone changed this advisory at the same time: open it again and retry."
RECORD_PERMISSION = "Recording security advisories needs Device security Edit."
REOPEN_PERMISSION = "Reopening a security advisory needs Device security Edit."
CLOSE_PERMISSION = "Closing a security advisory needs Device security Approve and Equipment View."
CORRECT_PERMISSION = "Reopening a closed advisory, or marking one entered in error, needs Device security Approve."
NOT_APPLICABLE_NOTE = ("No model is named on this advisory: marking it not applicable records that none of the products it names is in "
                       "this facility's catalog.")
# Not applicable, each reason checked against the records (critique40_rules B2)
NA_MODEL_NAMED = "Not marked: it names {models}, {which} in this facility's catalog."
NA_NAME_MODELS = "Not marked: name its models first, or choose 'It names no model in this facility's catalog'."
NA_ON_SITE = "Not marked: {devices} {verb} recorded on site."
NA_ON_RELEASE = "Not marked: {counts}."
# Close (build40 section 4.4, design40 section 4.4, critique40_rules B1, critique40_practice N4): each kind that blocks it, in
# rules.UNANSWERED's order, with the devices' tags (up to 10, then "and N more") for a reader of devices
CLOSE_PREFIX = "Not closed: "
CLOSE_NONE = "{devices} on a release it names, or with no release recorded, {have} no response recorded{tags}."
CLOSE_NO_FIX = " The advisory names no fix: record a decision for {them} (the model's, or each temporary device's)."
CLOSE_RESPOND = " Open updates to what the advisory offers, or record a decision for {them}."
CLOSE_REVIEW_DUE = "{devices} {be} under a decision past its review date{tags}: review and renew it."
CLOSE_OTHER_LIST = ("{devices} {have} patches recorded that name another list than the one the advisory names ({lists}){tags}. Record "
                    "the list it names once it is applied, record a revision if the manufacturer now names a later list, or record a "
                    "decision for {them}.")
CLOSE_NOT_CONFIRMED = ("{devices} {have} an update done with the version not confirmed{tags}: confirm the version on its work order, or "
                       "record a decision for {them}.")
CLOSE_OPEN = "{devices} {have} {their} update open{tags}: close the advisory when {they_are} done, or record a decision for {them}."

# History reasons (fixed sentences, AD numbers only)
RECORDED, SOURCE_ADDED, SOURCE_REMOVED, REVISION_RECORDED = "Recorded", "Source added", "Source removed", "Revision recorded"
EDITED = "Edited"
REOPENED_BY_REVISION = "Reopened: a revision changed what it names"
MODELS_SET, MODEL_REMOVED, MODELS_CONFIRMED = "Models set", "Model removed", "Models confirmed"
MARKED_NOT_APPLICABLE, CLOSED, REOPENED, MARKED_IN_ERROR = "Marked not applicable", "Closed", "Reopened", "Entered in error"

# What a revision changed that reopens the models' confirmation (and, on an advisory marked not applicable, moves it back to open)
REOPENING = ("products_changed", "vulnerabilities_added", "fix_available")

CVE_PATTERN = re.compile(r"^CVE-[0-9]{4}-[0-9]{4,}$")
PIECE_SHOWN = 40  # a refused piece is named up to this many characters


# --- small parts -----------------------------------------------------------------------------------------------------------------------

def _and(items) -> str:
    """ "A", "A and B", "A, B, and C"."""
    items = list(items)
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _devices(n: int) -> str:
    return f"{n} device" if n == 1 else f"{n} devices"


def _cves(value, errors: dict, field: str = "cves") -> list:
    """CVE IDs from a pasted list (text, or a list of texts): split on spaces, commas, and new lines; upper-cased; each once, in the order
    given. The whole field is refused when any piece is not a CVE ID (CVE_WORDS naming the first one)."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        text = " ".join(value)
    else:
        errors[field] = CVE_LIST_WORDS
        return []
    out: list = []
    for piece in re.split(r"[\s,]+", text):
        if not piece:
            continue
        cve = piece.upper()
        if not CVE_PATTERN.match(cve):
            shown = piece if len(piece) <= PIECE_SHOWN else f"{piece[:PIECE_SHOWN]}..."
            errors[field] = CVE_WORDS.format(piece=shown)
            return []
        if cve not in out:
            out.append(cve)
    return out


def _token(value, field: str, errors: dict, *, limit: int, validator) -> str:
    """A reference as its publisher prints it: a token (the model's validator), at most `limit` characters, never a network identifier
    (decisions.network_identifier); "" for none."""
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[field] = REFERENCE_WORDS
        return ""
    text = value.strip()
    if not text:
        return ""
    if len(text) > limit:
        errors[field] = LENGTH_WORDS.format(limit=limit)
    elif network_identifier(text):
        errors[field] = REFERENCE_WORDS
    else:
        try:
            validator(text)
        except ValidationError:
            errors[field] = REFERENCE_WORDS
    return text


def _choice(value, choices, field: str, words: str, errors: dict) -> str:
    text = value.strip() if isinstance(value, str) else None
    if text not in choices.values:
        errors[field] = words
        return ""
    return text


def _changes(values, errors: dict) -> list:
    """What a revision changed (AdvisoryChange slugs), each once, in the choices' order."""
    if values is None:
        return []
    if isinstance(values, str) or not hasattr(values, "__iter__"):
        errors["last_change"] = CHANGE_WORDS
        return []
    given = [v.strip() if isinstance(v, str) else v for v in values]
    if any(v not in AdvisoryChange.values for v in given):
        errors["last_change"] = CHANGE_WORDS
        return []
    return [slug for slug in AdvisoryChange.values if slug in given]


def _happened(value, name: str, what: str, errors: dict, today: date) -> date | None:
    """A day something happened: from EARLIEST_YEAR, not after today (services._day)."""
    return services._day(value, name, what, errors, today=today, happened=True)


def _received(value, errors: dict, today: date) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        errors["received_on"] = RECEIVED_NEEDED
        return None
    return _happened(value, "received_on", "The day received", errors, today)


def _source_fields(source, reference, url, published_on, errors: dict, today: date) -> tuple:
    """A source's fields, each checked on its own: (kind, reference, link, published day)."""
    kind = _choice(source, AdvisorySourceKind, "source", SOURCE_WORDS, errors)
    ref = _token(reference, "reference", errors, limit=ADVISORY_REFERENCE_MAX, validator=ADVISORY_REFERENCE_VALIDATOR)
    link = services._url(url, errors, field="url", words=LINK_WORDS, limit=ADVISORY_URL_MAX)
    published = _happened(published_on, "published_on", "The day published", errors, today)
    return kind, ref, link, published


def _ids(values, field: str, words: str, errors: dict) -> list:
    """Release ids (instances, UUIDs, or their text), each once, in the order given."""
    if values is None:
        return []
    if isinstance(values, (str, uuid.UUID, SoftwareRelease)):
        values = [values]
    if not hasattr(values, "__iter__"):
        errors[field] = words
        return []
    out: list = []
    for v in values:
        pk = services._pk(v, SoftwareRelease)
        if pk is None:
            errors[field] = words
            return []
        if pk not in out:
            out.append(pk)
    return out


def _device_model(value, errors: dict):
    """The model, resolved through this facility (a plain read: no advisory writer locks a model row)."""
    pk = services._pk(value, DeviceModel)
    found = DeviceModel.objects.filter(pk=pk).first() if pk is not None else None
    if found is None:
        errors["device_model"] = CHOOSE_MODEL
    return found


@contextmanager
def _writing(field: str = "advisory", *, integrity: bool = False):
    """A writer's transaction: a lock conflict PostgreSQL ends it for (services._lock_conflict) answers RACE keyed `field`, never a 500;
    with `integrity`, so does an IntegrityError (a deferred foreign key checked at COMMIT: a model deleted meanwhile)."""
    try:
        with transaction.atomic():
            yield
    except IntegrityError:
        if not integrity:
            raise
        raise ValidationError({field: RACE}) from None
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise ValidationError({field: RACE}) from None


def _lock(advisory):
    """The register lock, then the advisory's row FOR UPDATE of itself (the lock order's first two steps)."""
    services.lock_register()
    return services._locked_advisory(advisory)


def _number(received_on: date) -> str:
    """AD-<yy>-<nnnn>: the advisory sequence of the year received (a row only advisory writers take)."""
    from apps.core.models import Sequence

    yy = received_on.year % 100
    return f"AD-{yy:02d}-{Sequence.next(f'advisory-{yy:02d}'):04d}"


def _changed(advisory, today: date, *, clear: bool) -> None:
    """A change that matters after the models were confirmed (rules B1, locks S3): the day, the count (the decisions' "changed since"
    Check compares counts, never days), and, with `clear`, the confirmation cleared (Models to confirm)."""
    advisory.changed_on, advisory.changes = today, advisory.changes + 1
    if clear:
        advisory.models_confirmed_on, advisory.models_confirmed_by = None, None


def _delete(obj, by, reason: str) -> None:
    """Delete `obj` with `reason` in its history, as `by` (a release a row no longer names, a model row removed)."""
    obj._change_reason = reason[:type(obj).history.model._meta.get_field("history_change_reason").max_length]
    if by is not None:
        obj._history_user = by
    obj.delete()


def _seen(seen) -> set:
    if isinstance(seen, str):
        seen = [seen]
    return {str(n).strip().upper() for n in (seen or ()) if str(n).strip()}


# --- decisions under an advisory (three readers' words) -------------------------------------------------------------------------------

def _live_decisions(advisory):
    """The live decisions for the advisory's devices, lowest number first, with what their scope words read."""
    return (DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.ADVISORY, advisory_id=advisory.pk, ended_on__isnull=True)
            .select_related("asset", "release", "device_model").order_by("number"))


def _scope_words(decision) -> str:
    if decision.asset_id:
        return decision.asset.tag
    if decision.release_id:
        return rules.release_name(decision.release)
    return f"the {decision.device_model.model}"


def _decision_refusal(decision, user, *, scope: bool = False) -> str:
    """A live decision under the advisory in its reader's words: its number and "end it first" to who may end it; its number and who may
    end it to a reader of decisions; neither to anyone else. `scope` names what it is decided for (a release, a device, a model)."""
    if user is not None and not perms.can_view_devices(user):
        return DECISION_UNDER_UNREAD
    may_end = user is None or perms.can_decide(user)
    if scope:
        return (DECISION_FOR if may_end else DECISION_FOR_READ).format(decision=decision.number, scope=_scope_words(decision))
    return (DECISION_UNDER if may_end else DECISION_UNDER_READ).format(decision=decision.number)


# --- the lock-free readers (never select_for_update: a GET door calls them; the writer calls the same one after its locks) ----------

def overlaps(cves, *, source: str = "", reference: str = "", exclude=None) -> dict:
    """{advisory number: [shared CVE IDs and references]} for live advisories (not entered in error) sharing a CVE ID, or the reference
    under another source kind (one query, matched in Python): the Record advisory modal's note before saving. The CVE IDs in the order
    given, then the reference; numbers in order. `exclude` an advisory (or its id) left out (its own)."""
    wanted = [c.upper() for c in (cves or ())]
    key = (reference or "").strip().casefold()
    if not wanted and not key:
        return {}
    excluded = getattr(exclude, "pk", exclude)
    rows = (AdvisorySource.objects.filter(advisory__removed_on__isnull=True)
            .values_list("advisory_id", "advisory__number", "advisory__cves", "source", "reference_key", "removed_on"))
    found: dict = {}
    for advisory_id, number, advisory_cves, kind, ref_key, removed_on in rows:
        if excluded is not None and advisory_id == excluded:
            continue
        items = found.setdefault(number, [])
        held = set(advisory_cves or ())
        for cve in wanted:
            if cve in held and cve not in items:
                items.append(cve)
        if key and ref_key == key and kind != source and removed_on is None and reference.strip() not in items:
            items.append(reference.strip())
    return {number: found[number] for number in sorted(found) if found[number]}


def _overlap_refusal(found: dict, seen) -> str | None:
    """CVES_ON_OTHER for the overlaps the modal did not show (`seen`: the advisory numbers it showed), or None."""
    shown = _seen(seen)
    unseen = {number: items for number, items in found.items() if number.upper() not in shown}
    if not unseen:
        return None
    items: list = []
    for its in unseen.values():
        items += [x for x in its if x not in items]
    named = items[0] if len(items) == 1 else f"{items[0]} and {len(items) - 1} more"
    numbers = list(unseen)
    return CVES_ON_OTHER.format(cves=named, verb="is" if len(items) == 1 else "are", numbers=_and(numbers), first=numbers[0])


def _reference_holder(source: str, key: str, *, exclude=None) -> str:
    """The number of the advisory a live source of the same kind and reference belongs to (the same publication), or ""."""
    if not key:
        return ""
    rows = AdvisorySource.objects.filter(source=source, reference_key=key, removed_on__isnull=True, advisory_removed=False)
    if exclude is not None:
        rows = rows.exclude(pk=exclude)
    return rows.order_by("advisory__number").values_list("advisory__number", flat=True).first() or ""


def set_model_refusal(advisory) -> str | None:
    """Why the models it names cannot be changed now (an advisory not open, or entered in error), or None."""
    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR.format(number=advisory.number)
    if advisory.status != AdvisoryStatus.OPEN:
        return REOPEN_TO_CHANGE.format(number=advisory.number)
    return None


def _close_words(blockers: dict, *, tags: bool) -> str:
    """Close's refusal from rules.close_blockers: one sentence per kind, counts, and the devices' tags for a reader of devices."""
    parts = []
    for kind, found in blockers.items():
        n = len(found)
        shown = f" ({rules._tags([a.line.asset.tag for a in found], shown=10)})" if tags else ""
        them, have = ("it", "has") if n == 1 else ("them", "have")
        if kind == rules.NO_RESPONSE:
            words = CLOSE_NONE.format(devices=_devices(n), have=have, tags=shown)
            no_fix = all(a.line.row.remedy in rules.NO_FIX_REMEDIES for a in found)
            parts.append(words + (CLOSE_NO_FIX if no_fix else CLOSE_RESPOND).format(them=them))
        elif kind == rules.ANSWER_REVIEW_DUE:
            parts.append(CLOSE_REVIEW_DUE.format(devices=_devices(n), be="is" if n == 1 else "are", tags=shown))
        elif kind == rules.PATCHES_OTHER_LIST:
            lists = []
            for a in found:
                if a.line.row.patches_reference not in lists:
                    lists.append(a.line.row.patches_reference)
            parts.append(CLOSE_OTHER_LIST.format(devices=_devices(n), have=have, lists=", ".join(lists), tags=shown, them=them))
        elif kind == rules.UPDATE_NOT_CONFIRMED:
            parts.append(CLOSE_NOT_CONFIRMED.format(devices=_devices(n), have=have, tags=shown, them=them))
        else:
            parts.append(CLOSE_OPEN.format(devices=_devices(n), have=have, their="its" if n == 1 else "their", tags=shown,
                                           they_are="it is" if n == 1 else "they are", them=them))
    return CLOSE_PREFIX + " ".join(parts)


def close_refusal(advisory, today: date, *, user=None) -> str | None:
    """Why the advisory cannot be closed now (words naming up to 10 tags, then "and N more", for `user` with can_view_devices; counts
    otherwise), or None. In order: entered in error; marked not applicable (reopen it first); for an open one, no model named
    (NAME_A_MODEL), its models to confirm; then each line that needs a response and is not answered (rules.close_blockers: no response
    recorded, a decision past its review date, patches naming another list, an update done with the version not confirmed, an update
    open), worded with what answers it. For a closed one, only the lines: what would refuse closing it again now, its devices named
    since and those under a decision past its review date. Needs attention lists a closed advisory by rules.named_since_lines, never by
    this reader: the same lines but those under a decision past its review date, which it lists under the advisory decisions past their
    review date. Fixed queries: rules.advisory_reading's."""
    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR.format(number=advisory.number)
    if advisory.status == AdvisoryStatus.NOT_APPLICABLE:
        return NOT_APPLICABLE_NOW.format(number=advisory.number)
    reading = rules.advisory_reading([advisory], today)
    if advisory.status == AdvisoryStatus.OPEN:
        if not reading.rows:
            return NAME_A_MODEL
        if advisory.models_confirmed_on is None:
            return MODELS_TO_CONFIRM
    blockers = rules.close_blockers(reading.answers)
    if not blockers:
        return None
    return _close_words(blockers, tags=user is None or perms.can_view_devices(user))


def _plural_verb(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _not_applicable_words(reason: str, rows, lines) -> str:
    """A reason's refusal in words, where rules.not_applicable_blocks holds (critique40_rules B2)."""
    if reason == NotApplicableReason.NO_MODEL_HERE:
        models = [f"the {r.device_model.model}" for r in rows]
        return NA_MODEL_NAMED.format(models=_and(models), which=_plural_verb(len(models), "a model", "models"))
    if not rows:
        return NA_NAME_MODELS
    by_row: dict = {}
    for ln in lines:
        by_row.setdefault(ln.row.pk, []).append(ln)
    if reason == NotApplicableReason.NONE_HERE:
        parts = [f"{len(by_row[r.pk])} {r.device_model.model} {_plural_verb(len(by_row[r.pk]), 'device', 'devices')}"
                 for r in rows if by_row.get(r.pk)]
        total = sum(len(v) for v in by_row.values())
        return NA_ON_SITE.format(devices=_and(parts), verb=_plural_verb(total, "is", "are"))
    listed = [ln for ln in lines if ln.row.versions == AdvisoryVersions.LISTED]
    named = sum(ln.kind == rules.NAMED for ln in listed)
    unrecorded = sum(ln.kind == rules.LINE_NOT_RECORDED for ln in listed)
    parts = []
    if named and unrecorded:
        parts.append(f"{_devices(named)} {_plural_verb(named, 'is', 'are')} recorded on a release it names and {unrecorded} "
                     f"{_plural_verb(unrecorded, 'has', 'have')} no release recorded")
    elif named:
        parts.append(f"{_devices(named)} {_plural_verb(named, 'is', 'are')} recorded on a release it names")
    elif unrecorded:
        parts.append(f"{_devices(unrecorded)} {_plural_verb(unrecorded, 'has', 'have')} no release recorded")
    for r in rows:
        found = by_row.get(r.pk, [])
        if r.versions == AdvisoryVersions.LISTED or not found:
            continue
        what = (f"it names every release of the {r.device_model.model}" if r.versions == AdvisoryVersions.ALL
                else f"it names the {r.device_model.model} without stating releases")
        parts.append(f"{what}, and {_devices(len(found))} {_plural_verb(len(found), 'is', 'are')} recorded on site")
    return NA_ON_RELEASE.format(counts="; ".join(parts))


def _not_applicable(rows, lines) -> dict:
    return {reason: (_not_applicable_words(reason, rows, lines) if rules.not_applicable_blocks(reason, rows, lines) else None)
            for reason in NotApplicableReason.values}


def not_applicable_refusals(advisory, today: date) -> dict:
    """{NotApplicableReason value: words or None}: each reason checked against the records now (rules B2's words): no model here, refused
    while it names a model ("Not marked: it names the Alaris 8015 PCU, a model in this facility's catalog."); none here, refused with no
    model named, or while a device of a model it names is on site ("Not marked: 12 Alaris 8015 PCU devices are recorded on site.");
    releases not here, refused with no model named, or while a device is on a release it names or has no release recorded ("Not marked:
    3 devices are recorded on a release it names and 4 have no release recorded."). The predicate is rules.not_applicable_blocks (also a
    not-applicable advisory's "devices named since"). Fixed queries: the rows (two), the devices and their models' profiles (two)."""
    rows = rules.rows_of([advisory])
    lines = rules.advisory_lines(rows, today=today) if rows else []
    return _not_applicable(rows, lines)


def not_applicable_refusal(advisory, *, user=None) -> str | None:
    """Why the advisory cannot be marked not applicable at all now, whatever the reason (each reason's own check is
    not_applicable_refusals), or None: entered in error, closed (reopen it first), already not applicable, or a live decision under it
    (three readers' words: `user` None is the system)."""
    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR.format(number=advisory.number)
    if advisory.status == AdvisoryStatus.CLOSED:
        return CLOSED_NOW.format(number=advisory.number)
    if advisory.status == AdvisoryStatus.NOT_APPLICABLE:
        return ALREADY_NOT_APPLICABLE.format(number=advisory.number)
    decision = _live_decisions(advisory).first()
    return _decision_refusal(decision, user) if decision is not None else None


def reopen_refusal(advisory, *, user) -> str | None:
    """Why the advisory cannot be reopened by `user` now, or None: entered in error, already open, closed and `user` without Device
    security Approve (can_correct_advisories), marked not applicable and `user` without Edit. `user` None is the system."""
    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR.format(number=advisory.number)
    if advisory.status == AdvisoryStatus.OPEN:
        return ALREADY_OPEN.format(number=advisory.number)
    if user is not None and advisory.status == AdvisoryStatus.CLOSED and not perms.can_correct_advisories(user):
        return CORRECT_PERMISSION
    if user is not None and not perms.can_edit(user):
        return REOPEN_PERMISSION
    return None


def entered_in_error_refusal(advisory, *, user) -> str | None:
    """Why the advisory cannot be marked entered in error now, or None: already marked, or a live decision under it (three readers'
    words; `user` None is the system). A check at that moment (equipment's writers never take the register)."""
    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR.format(number=advisory.number)
    decision = _live_decisions(advisory).first()
    return _decision_refusal(decision, user) if decision is not None else None


def _model_decision(row):
    """The lowest-numbered live decision under the row's advisory with its scope on the row's model: a release or model scope of the
    model, or a device of the model now. One query."""
    model_id = row.device_model_id
    return _live_decisions(row.advisory).filter(Q(device_model_id=model_id) | Q(asset__device_model_id=model_id)).first()


def model_removal_refusal(row, *, user) -> str | None:
    """Why the model row cannot be removed now, or None: the row gone (a second removal answers in words), the advisory not open or
    entered in error (set_model_refusal), or a live decision under it with its scope on the model (three readers' words with the scope).
    `row` an AdvisoryModel or its id."""
    pk = services._pk(row, AdvisoryModel)
    now = AdvisoryModel.objects.filter(pk=pk).select_related("advisory", "device_model").first() if pk is not None else None
    if now is None:
        if isinstance(row, AdvisoryModel):
            number = SecurityAdvisory.objects.filter(pk=row.advisory_id).values_list("number", flat=True).first()
            if number:
                return ROW_GONE.format(number=number)
        return CHOOSE_ROW
    refusal = set_model_refusal(now.advisory)
    if refusal:
        return refusal
    decision = _model_decision(now)
    return _decision_refusal(decision, user, scope=True) if decision is not None else None


def _source_removal(source, advisory) -> str | None:
    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR.format(number=advisory.number)
    if source.removed_on is not None:
        return SOURCE_REMOVED_WORDS.format(number=advisory.number)
    if not AdvisorySource.objects.filter(advisory_id=advisory.pk, removed_on__isnull=True).exclude(pk=source.pk).exists():
        return LAST_SOURCE
    return None


def source_removal_refusal(source) -> str | None:
    """Why the source cannot be removed now, or None: its advisory entered in error, the source already removed, or it is the advisory's
    last live source (LAST_SOURCE). `source` an AdvisorySource or its id."""
    pk = services._pk(source, AdvisorySource)
    now = AdvisorySource.objects.filter(pk=pk).select_related("advisory").first() if pk is not None else None
    if now is None:
        return CHOOSE_SOURCE
    return _source_removal(now, now.advisory)


# --- writers ---------------------------------------------------------------------------------------------------------------------------

def _duplicates(cves: list, kind: str, reference: str, seen, *, exclude=None) -> dict:
    """The cross-row refusals of a new publication, read under the register: the same source kind and reference on a live source
    (REFERENCE_RECORDED, keyed reference: the same publication) and the overlaps the modal did not show (CVES_ON_OTHER, keyed cves)."""
    problems = {}
    holder = _reference_holder(kind, reference.casefold())
    if holder:
        problems["reference"] = REFERENCE_RECORDED.format(reference=reference, number=holder)
    refusal = _overlap_refusal(overlaps(cves, source=kind, reference=reference, exclude=exclude), seen)
    if refusal:
        problems["cves"] = refusal
    return problems


def _save_source(row, by, reason: str) -> None:
    """Save a source; the live-reference constraint (the backstop of _reference_holder's check) answers REFERENCE_RECORDED in words."""
    try:
        with transaction.atomic():
            services._save(row, by, reason)
    except IntegrityError:
        holder = _reference_holder(row.source, row.reference_key, exclude=row.pk) or row.advisory.number
        raise ValidationError({"reference": REFERENCE_RECORDED.format(reference=row.reference, number=holder)}) from None


def record_advisory(*, received_on, cves, source, reference="", url="", published_on=None, seen=(), by, today=None):
    """Record an advisory the team took in, with its first source: the day received (not after today), its CVE IDs (a pasted list:
    _cves), and the source (its kind, a token reference, an https link to a public host, the day published). Refused, under the
    register: the same source kind and reference on a live source (REFERENCE_RECORDED, keyed reference); a CVE ID, or the reference
    under another source kind, on a live advisory the modal did not show (`seen`: the advisory numbers it showed; CVES_ON_OTHER, keyed
    cves: offered, never refused once seen). Numbered AD-<yy>-<nnnn> by the year received; history "Recorded". Locks: the register, the
    number. Returns the advisory."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    received = _received(received_on, errors, today)
    cve_list = _cves(cves, errors)
    kind, ref, link, published = _source_fields(source, reference, url, published_on, errors, today)
    if errors:
        raise ValidationError(errors)
    with _writing():
        services.lock_register()
        problems = _duplicates(cve_list, kind, ref, seen)
        if problems:
            raise ValidationError(problems)
        advisory = SecurityAdvisory(number=_number(received), cves=cve_list, received_on=received, recorded_by=by)
        services._save(advisory, by, RECORDED)
        _save_source(AdvisorySource(advisory=advisory, source=kind, reference=ref, reference_key=ref.casefold(), url=link,
                                    published_on=published), by, RECORDED)
    return advisory


def update_advisory(advisory, *, received_on=KEEP, cves=KEEP, seen=(), by, today=None):
    """Change the day received or the CVE IDs (KEEP leaves one as it is). The day received is never after a decision's day decided under
    it (live or ended: RECEIVED_AFTER_DECISION, the earliest one's number and day for a reader of decisions, else
    RECEIVED_AFTER_DECISION_UNREAD); a change of it after the models were confirmed clears the confirmation, sets changed_on,
    and counts a change (patches with no list named count from it: rules B1). CVE IDs added that another live advisory holds are refused
    unless `seen` names it (CVES_ON_OTHER). Refused on an advisory entered in error. History "Edited" (nothing saved when nothing
    changes). Locks: the register, the advisory. Returns the row; the caller's copy follows it."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    received = KEEP if received_on is KEEP else _received(received_on, errors, today)
    cve_list = KEEP if cves is KEEP else _cves(cves, errors)
    if errors:
        raise ValidationError(errors)
    with _writing():
        locked = _lock(advisory)
        if locked.removed_on is not None:
            raise ValidationError({"advisory": ENTERED_IN_ERROR.format(number=locked.number)})
        problems = {}
        new_day = received is not KEEP and received != locked.received_on
        if new_day:
            first = (DeviceSecurityDecision.objects.filter(advisory_id=locked.pk).order_by("decided_on", "number")
                     .values_list("number", "decided_on").first())
            if first is not None and received > first[1]:
                problems["received_on"] = (RECEIVED_AFTER_DECISION.format(decision=first[0], day=rules.day_words(first[1]))
                                           if services._reads_decisions(by) else RECEIVED_AFTER_DECISION_UNREAD)
        added = [] if cve_list is KEEP else [c for c in cve_list if c not in (locked.cves or [])]
        if added:
            refusal = _overlap_refusal(overlaps(added, exclude=locked), seen)
            if refusal:
                problems["cves"] = refusal
        if problems:
            raise ValidationError(problems)
        changed = False
        if new_day:
            locked.received_on, changed = received, True
            if locked.models_confirmed_on is not None:
                _changed(locked, today, clear=True)
        if cve_list is not KEEP and cve_list != list(locked.cves or []):
            locked.cves, changed = cve_list, True
        if changed:
            services._save(locked, by, EDITED)
    services._sync(advisory, locked)
    return locked


def add_source(advisory, *, source, reference="", url="", published_on=None, cves=(), by, today=None):
    """Add a publication to an advisory (the modal's "Add as a source of AD-26-0002" too): its kind, reference, link, and day published,
    and `cves` merged into the advisory's list (the locked row's, new ones appended in the order given). Refused on an advisory entered
    in error, and for the same source kind and reference on a live source (REFERENCE_RECORDED). History "Source added" (the source, and
    the advisory when its CVE IDs grew). Locks: the register, the advisory. Returns the source."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    kind, ref, link, published = _source_fields(source, reference, url, published_on, errors, today)
    cve_list = _cves(cves, errors)
    if errors:
        raise ValidationError(errors)
    with _writing():
        locked = _lock(advisory)
        if locked.removed_on is not None:
            raise ValidationError({"advisory": ENTERED_IN_ERROR.format(number=locked.number)})
        holder = _reference_holder(kind, ref.casefold())
        if holder:
            raise ValidationError({"reference": REFERENCE_RECORDED.format(reference=ref, number=holder)})
        row = AdvisorySource(advisory=locked, source=kind, reference=ref, reference_key=ref.casefold(), url=link, published_on=published)
        _save_source(row, by, SOURCE_ADDED)
        merged = list(locked.cves or []) + [c for c in cve_list if c not in (locked.cves or [])]
        if merged != list(locked.cves or []):
            locked.cves = merged
            services._save(locked, by, SOURCE_ADDED)
    services._sync(advisory, locked)
    return row


def _source_advisory(source_row):
    """The id of the advisory a source belongs to (a plain read: its row is then read again under the advisory's lock), keyed source."""
    if isinstance(source_row, AdvisorySource):
        return source_row.advisory_id
    pk = services._pk(source_row, AdvisorySource)
    found = AdvisorySource.objects.filter(pk=pk).values_list("advisory_id", flat=True).first() if pk is not None else None
    if found is None:
        raise ValidationError({"source": CHOOSE_SOURCE})
    return found


def _source_now(source_row, locked):
    """The source as it is now, under its advisory's lock (every source writer takes it first), keyed source."""
    pk = services._pk(source_row, AdvisorySource)
    found = AdvisorySource.objects.filter(pk=pk, advisory_id=locked.pk).first() if pk is not None else None
    if found is None:
        raise ValidationError({"source": CHOOSE_SOURCE})
    found.advisory = locked
    return found


def _open_source(source_row, locked):
    """The source under its advisory's lock, refused on an advisory entered in error or a source removed."""
    row = _source_now(source_row, locked)
    if locked.removed_on is not None:
        raise ValidationError({"advisory": ENTERED_IN_ERROR.format(number=locked.number)})
    if row.removed_on is not None:
        raise ValidationError({"source": SOURCE_REMOVED_WORDS.format(number=locked.number)})
    return row


def update_source(source_row, *, source, reference="", url="", published_on=None, by, today=None):
    """Change a source's kind, reference, link, or day published (its revision only through record_revision). Refused on a source
    removed or an advisory entered in error, for the same kind and reference as another live source (REFERENCE_RECORDED), and for a
    day published after the day updated it records (PUBLISHED_AFTER). History "Edited" (nothing saved when nothing changes). Locks: the
    register, the advisory. Returns the source; the caller's copy follows it."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    kind, ref, link, published = _source_fields(source, reference, url, published_on, errors, today)
    if errors:
        raise ValidationError(errors)
    advisory_id = _source_advisory(source_row)
    with _writing():
        locked = _lock(advisory_id)
        row = _open_source(source_row, locked)
        problems = {}
        holder = _reference_holder(kind, ref.casefold(), exclude=row.pk)
        if holder:
            problems["reference"] = REFERENCE_RECORDED.format(reference=ref, number=holder)
        if published is not None and row.updated_on is not None and row.updated_on < published:
            problems["published_on"] = PUBLISHED_AFTER.format(day=rules.day_words(row.updated_on))
        if problems:
            raise ValidationError(problems)
        values = {"source": kind, "reference": ref, "reference_key": ref.casefold(), "url": link, "published_on": published}
        changed = [name for name, value in values.items() if getattr(row, name) != value]
        if changed:
            for name in changed:
                setattr(row, name, values[name])
            _save_source(row, by, EDITED)
    services._sync(source_row, row)
    return row


def record_revision(source_row, *, revision="", updated_on=None, last_change=(), by, today=None):
    """Record a source's revision as its publisher prints it (the revision, a token; the day updated, not before the day published; what
    changed, AdvisoryChange slugs), replacing the last one recorded (History keeps each). A change in REOPENING (products or releases
    changed, vulnerabilities added, the advisory now names a fix) clears the models' confirmation, sets changed_on to today, and counts a
    change; on an advisory marked not applicable it also moves it back to open (its reason, day, and person cleared; history "Reopened: a
    revision changed what it names"). A closed one stays closed: it then reads "models to confirm since a change" (stored state, never
    a day compared; a change of the day received after the confirmation reads the same: rules.TO_CONFIRM_SINCE_WORDS). Refused on a
    source removed or an advisory entered in error. Locks: the register, the advisory. Returns the source."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    rev = _token(revision, "revision", errors, limit=REVISION_MAX, validator=REVISION_VALIDATOR)
    updated = _happened(updated_on, "updated_on", "The day updated", errors, today)
    changes = _changes(last_change, errors)
    if not rev and updated is None and "revision" not in errors and "updated_on" not in errors:
        errors["revision"] = REVISION_NEEDED
    if errors:
        raise ValidationError(errors)
    advisory_id = _source_advisory(source_row)
    with _writing():
        locked = _lock(advisory_id)
        row = _open_source(source_row, locked)
        if updated is not None and row.published_on is not None and updated < row.published_on:
            raise ValidationError({"updated_on": UPDATED_BEFORE.format(day=rules.day_words(row.published_on))})
        row.revision, row.updated_on, row.last_change = rev, updated, changes
        services._save(row, by, REVISION_RECORDED)
        if set(changes) & set(REOPENING):
            reason = REVISION_RECORDED
            _changed(locked, today, clear=True)
            if locked.status == AdvisoryStatus.NOT_APPLICABLE:
                locked.status, locked.not_applicable_reason, locked.status_on, locked.status_by = AdvisoryStatus.OPEN, "", None, None
                reason = REOPENED_BY_REVISION
            services._save(locked, by, reason)
    services._sync(source_row, row)
    return row


def remove_source(source_row, *, by, today=None):
    """Remove a source (removed_on, removed_by; history "Removed"): refused for the advisory's last live source (LAST_SOURCE), a source
    already removed, or an advisory entered in error (source_removal_refusal, read again under the locks: two removals of the last two
    sources at once leave one). Locks: the register, the advisory. Returns the source."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    advisory_id = _source_advisory(source_row)
    with _writing():
        locked = _lock(advisory_id)
        row = _source_now(source_row, locked)
        refusal = _source_removal(row, locked)
        if refusal:
            raise ValidationError({"source": refusal})
        row.removed_on, row.removed_by = today, by
        services._save(row, by, SOURCE_REMOVED)
    services._sync(source_row, row)
    return row


def _named_under(versions: str, listed: set, fix, release_id) -> bool:
    """Whether a device on `release_id` (None: not recorded) would need a response under a row with these values (rules._line's order)."""
    if versions != AdvisoryVersions.LISTED or release_id is None:
        return True
    return release_id != fix and release_id in listed


def _scope_refusal(locked, model, versions: str, listed: list, fix, by) -> dict | None:
    """A live decision under this advisory that the new row would leave without its scope (critique40_rules S5): a release scope whose
    release would no longer read named (unlisted, or now the fix); a device scope whose device's recorded release would no longer need a
    response (on a release unlisted, or on the fix). Keyed release_ids (versions listed) or versions, in three readers' words."""
    field = "release_ids" if versions == AdvisoryVersions.LISTED else "versions"
    wanted = set(listed)
    for d in _live_decisions(locked).filter(Q(release__isnull=False, device_model_id=model.pk) | Q(asset__device_model_id=model.pk)):
        release_id = d.release_id if d.release_id else getattr(rules.release_of(d.asset), "pk", None)
        if not _named_under(versions, wanted, fix, release_id):
            return {field: _decision_refusal(d, by, scope=True)}
    return None


def set_model(advisory, *, device_model, versions, release_ids=(), remedy, fix_release=None, patches_reference="", confirm=False, by,
              today=None):
    """Name a model of this facility's catalog on an open advisory, or change the row that names it (one row per model, updated in
    place): which releases it names (`versions`: listed, with `release_ids` at least one; every release; not stated), what it offers
    (`remedy`), the fix (`fix_release`: a release remedy only, versions listed, a release of the model never listed) and the patch list
    (`patches_reference`, a patches remedy only). Releases are the model's, versioned, not entered in error. Its release rows are
    diffed (the unticked deleted, the ticked added, each with the row's advisory); `seen_release_ids` becomes the model's releases now
    (the "Release added since" Check). After the models were confirmed, a change that matters (a new row, its values, its releases)
    sets changed_on, counts a change, and clears the confirmation unless `confirm` ("These are all the models it names that are here",
    which confirms them). Refused on an advisory not open (REOPEN_TO_CHANGE) or entered in error, and when the change would leave a live
    release-scope or device-scope decision under it without its scope (_scope_refusal). History "Models set" (the advisory's "Models
    confirmed" when only confirmed). Locks: the register, the advisory, the releases it names in pk order; the model only at COMMIT
    (a deferred foreign key's KEY SHARE): a model deleted meanwhile, or a lock conflict, answers RACE keyed device_model. Returns the
    row."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    model = _device_model(device_model, errors)
    versions = _choice(versions, AdvisoryVersions, "versions", VERSIONS_WORDS, errors)
    remedy = _choice(remedy, AdvisoryRemedy, "remedy", REMEDY_WORDS, errors)
    listed = _ids(release_ids, "release_ids", CHOOSE_RELEASES, errors)
    fixes = _ids(fix_release, "fix_release", FIX_NEEDED, errors)
    fix = fixes[0] if fixes else None
    patches = _token(patches_reference, "patches_reference", errors, limit=UPDATE_REFERENCE_MAX, validator=UPDATE_REFERENCE_VALIDATOR)
    if "versions" not in errors and "remedy" not in errors:  # between fields, keyed by the one to change
        if remedy == AdvisoryRemedy.RELEASE and versions != AdvisoryVersions.LISTED:
            errors["versions"] = FIX_ONLY_LISTED
        if remedy == AdvisoryRemedy.RELEASE and fix is None and "fix_release" not in errors:
            errors["fix_release"] = FIX_NEEDED
        if remedy != AdvisoryRemedy.RELEASE and fix is not None:
            errors.setdefault("fix_release", FIX_ONLY_RELEASE)
        if remedy != AdvisoryRemedy.PATCHES and patches:
            errors.setdefault("patches_reference", PATCHES_ONLY)
    if "versions" not in errors and "release_ids" not in errors:
        if versions == AdvisoryVersions.LISTED and not listed:
            errors["release_ids"] = CHOOSE_RELEASES
        elif versions != AdvisoryVersions.LISTED and listed:
            errors["release_ids"] = RELEASES_ONLY_LISTED
    if fix is not None and fix in listed:
        errors.setdefault("fix_release", FIX_LISTED)
    if errors:
        raise ValidationError(errors)
    with _writing("device_model", integrity=True):
        locked = _lock(advisory)
        refusal = set_model_refusal(locked)
        if refusal:
            raise ValidationError({"advisory": refusal})
        releases, problems = {}, {}
        words = CHOOSE_MODEL_RELEASE.format(model=model.model)
        for pk in sorted({*listed, *([fix] if fix else [])}):
            field = "fix_release" if pk == fix else "release_ids"
            try:
                found = services._locked_release(pk, field=field, message=words)
            except ValidationError:
                problems[field] = words
                continue
            if not found.version_key or found.removed_on is not None or found.device_model_id != model.pk:
                problems[field] = words
            releases[pk] = found
        if problems:
            raise ValidationError(problems)
        scoped = _scope_refusal(locked, model, versions, listed, fix, by)
        if scoped:
            raise ValidationError(scoped)
        row = AdvisoryModel.objects.filter(advisory_id=locked.pk, device_model_id=model.pk).first()
        current = {ar.release_id: ar for ar in AdvisoryRelease.objects.filter(advisory_model=row).order_by("pk")} if row else {}
        seen = [str(pk) for pk in SoftwareRelease.objects.filter(device_model_id=model.pk, removed_on__isnull=True)
                .exclude(version_key="").order_by("version_key", "pk").values_list("pk", flat=True)]
        new = row is None
        if new:
            row = AdvisoryModel(advisory=locked, device_model=model)
        values = {"versions": versions, "remedy": remedy, "fix_release_id": fix, "patches_reference": patches}
        changed = new or any(getattr(row, name) != value for name, value in values.items())
        for name, value in values.items():
            setattr(row, name, value)
        if changed or row.seen_release_ids != seen:
            row.seen_release_ids = seen
            services._save(row, by, MODELS_SET)
        dropped = [ar for release_id, ar in current.items() if release_id not in listed]
        added = [pk for pk in listed if pk not in current]
        for ar in dropped:
            _delete(ar, by, MODELS_SET)
        for pk in added:
            services._save(AdvisoryRelease(advisory=locked, advisory_model=row, release=releases[pk]), by, MODELS_SET)
        mattered = changed or bool(dropped) or bool(added)
        save = False
        if mattered and locked.models_confirmed_on is not None:
            _changed(locked, today, clear=not confirm)
            save = True
        if confirm and (mattered or locked.models_confirmed_on is None):
            locked.models_confirmed_on, locked.models_confirmed_by = today, by
            save = True
        if save:
            services._save(locked, by, MODELS_SET if mattered else MODELS_CONFIRMED)
    services._sync(advisory, locked)
    return row


def remove_model(row, *, by, today=None):
    """Stop naming a model on an open advisory (its row and release rows deleted, history "Model removed"). Refused on an advisory not open
    or entered in error (REOPEN_TO_CHANGE), while a live decision under it has its scope on the model (a release or the model, or a device
    of the model: three readers' words with the scope), and for a row already removed (ROW_GONE: a second removal answers in words). After
    the models were confirmed, the removal clears the confirmation and counts a change. Locks: the register, the advisory. Returns the
    advisory."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    pk = services._pk(row, AdvisoryModel)
    if isinstance(row, AdvisoryModel):
        advisory_id = row.advisory_id
    else:
        advisory_id = AdvisoryModel.objects.filter(pk=pk).values_list("advisory_id", flat=True).first() if pk is not None else None
    if advisory_id is None:
        raise ValidationError({"device_model": CHOOSE_ROW})
    with _writing("device_model"):
        locked = _lock(advisory_id)
        now = AdvisoryModel.objects.filter(pk=pk, advisory_id=locked.pk).first()  # model_removal_refusal's order: the row first
        if now is None:
            raise ValidationError({"device_model": ROW_GONE.format(number=locked.number)})
        refusal = set_model_refusal(locked)
        if refusal:
            raise ValidationError({"advisory": refusal})
        now.advisory = locked
        decision = _model_decision(now)
        if decision is not None:
            raise ValidationError({"device_model": _decision_refusal(decision, by, scope=True)})
        for ar in AdvisoryRelease.objects.filter(advisory_model_id=now.pk).order_by("pk"):
            _delete(ar, by, MODEL_REMOVED)
        _delete(now, by, MODEL_REMOVED)
        if locked.models_confirmed_on is not None:
            _changed(locked, today, clear=True)
            services._save(locked, by, MODEL_REMOVED)
    return locked


def confirm_models(advisory, *, by, today=None):
    """Confirm the models an open advisory names are all the ones here (models_confirmed_on, _by; history "Models confirmed"; nothing
    saved when already confirmed). Refused on an advisory not open or entered in error, and with no model named (NAME_A_MODEL). Locks:
    the register, the advisory. Returns the row; the caller's copy follows it."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    with _writing():
        locked = _lock(advisory)
        refusal = set_model_refusal(locked)
        if refusal:
            raise ValidationError({"advisory": refusal})
        if not AdvisoryModel.objects.filter(advisory_id=locked.pk).exists():
            raise ValidationError({"advisory": NAME_A_MODEL})
        if locked.models_confirmed_on is None:
            locked.models_confirmed_on, locked.models_confirmed_by = today, by
            services._save(locked, by, MODELS_CONFIRMED)
    services._sync(advisory, locked)
    return locked


def mark_not_applicable(advisory, *, reason, by, today=None):
    """Mark an open advisory not applicable here for `reason` (NotApplicableReason), every reason checked against the records now
    (not_applicable_refusals' words, keyed not_applicable_reason) and refused while a live decision is under it (three readers' words);
    sets status, reason, status_on, status_by, and the models' confirmation (marking is the confirmation: of no model for no_model_here).
    History "Marked not applicable". Locks: the register, the advisory (the lines read there: a check at that moment). Returns the row."""
    services._check(by, perms.can_edit, RECORD_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    reason = _choice(reason, NotApplicableReason, "not_applicable_reason", REASON_WORDS, errors)
    if errors:
        raise ValidationError(errors)
    with _writing():
        locked = _lock(advisory)
        refusal = not_applicable_refusal(locked, user=by)
        if refusal:
            raise ValidationError({"advisory": refusal})
        words = not_applicable_refusals(locked, today)[reason]
        if words:
            raise ValidationError({"not_applicable_reason": words})
        locked.status, locked.not_applicable_reason = AdvisoryStatus.NOT_APPLICABLE, reason
        locked.status_on, locked.status_by = today, by
        locked.models_confirmed_on, locked.models_confirmed_by = today, by
        services._save(locked, by, MARKED_NOT_APPLICABLE)
    services._sync(advisory, locked)
    return locked


def close(advisory, *, by, today=None):
    """Close an open advisory: its models confirmed, at least one named, and every line that needs a response answered (close_refusal,
    read again under the locks; a check at that moment). Sets status, status_on, status_by; history "Closed". can_decide (Approve and
    Equipment View). Locks: the register, the advisory. Returns the row; the caller's copy follows it."""
    services._check(by, perms.can_decide, CLOSE_PERMISSION)
    today = services._today(today)
    with _writing():
        locked = _lock(advisory)
        if locked.status == AdvisoryStatus.CLOSED and locked.removed_on is None:
            raise ValidationError({"advisory": ALREADY_CLOSED.format(number=locked.number)})
        refusal = close_refusal(locked, today, user=by)
        if refusal:
            raise ValidationError({"advisory": refusal})
        locked.status, locked.status_on, locked.status_by = AdvisoryStatus.CLOSED, today, by
        services._save(locked, by, CLOSED)
    services._sync(advisory, locked)
    return locked


def reopen(advisory, *, by, today=None):
    """Reopen an advisory: from not applicable at Edit, from closed at Device security Approve (can_correct_advisories: PermissionDenied
    otherwise). Status open, its reason, day, and person cleared; the models' confirmation kept. History "Reopened". Locks: the register,
    the advisory. Returns the row; the caller's copy follows it."""
    services._check(by, perms.can_edit, REOPEN_PERMISSION)
    with _writing():
        locked = _lock(advisory)
        if (locked.status == AdvisoryStatus.CLOSED and locked.removed_on is None and by is not None
                and not perms.can_correct_advisories(by)):
            raise PermissionDenied(CORRECT_PERMISSION)
        refusal = reopen_refusal(locked, user=by)
        if refusal:
            raise ValidationError({"advisory": refusal})
        locked.status, locked.not_applicable_reason, locked.status_on, locked.status_by = AdvisoryStatus.OPEN, "", None, None
        services._save(locked, by, REOPENED)
    services._sync(advisory, locked)
    return locked


def mark_entered_in_error(advisory, *, by, today=None):
    """Mark an advisory entered in error (removed_on, removed_by; history "Entered in error"; never deleted), refused while a live decision
    is under it (three readers' words). Its sources' advisory_removed is set in one update (no history row: hidden), which frees their
    references for another advisory. can_correct_advisories (Approve). Locks: the register, the advisory. Returns the row."""
    services._check(by, perms.can_correct_advisories, CORRECT_PERMISSION)
    today = services._today(today)
    with _writing():
        locked = _lock(advisory)
        refusal = entered_in_error_refusal(locked, user=by)
        if refusal:
            raise ValidationError({"advisory": refusal})
        locked.removed_on, locked.removed_by = today, by
        services._save(locked, by, MARKED_IN_ERROR)
        AdvisorySource.objects.filter(advisory_id=locked.pk).update(advisory_removed=True)
    services._sync(advisory, locked)
    return locked
