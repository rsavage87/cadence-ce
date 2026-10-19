"""
Every write of test equipment (slice 35), and the read helpers the screens, the binder, and the API share. The rule is
apps.testequipment.rules; the levels are apps.testequipment.permissions (checked here for a user given as `by`: PermissionDenied in
words; by=None is the system: the seed, tests). Every write is transaction.atomic, refuses in plain words (ValidationError keyed by the
field a form has), and writes history with a _change_reason. Today is the facility's, read once and passed down.

Locks: every writer here locks only its item's row (select_for_update) and takes nothing after it; adding an item takes
equipment.services.lock_tags first (the facility's tags are shared with the devices), and so does a change of an item's tag, make,
model, or serial, and set_status's every move to in use (the duplicate checks run under it), before the item's row. The work order's
writers (completion.complete_work_order, workorders.services.set_test_equipment: check_uses) take lock_work, then the work order row,
then the named items' rows and those already on the work order in pk order, then what the completion already takes; assign
(clear_for_vendor) takes the work order's row, then its items' rows: no cycle with the writers here. A cancel (clear_for_cancel) takes no
item's row (review fix): it runs inside transactions that hold a device's row and cancel several work orders (equipment's retirement, a
passed incoming inspection), where taking items after the device would wait in a circle with a writer above; a removal reads only the
calibrations, so one recorded meanwhile is harmless.

- add_test_equipment(*, tag, kind, manufacturer, model, serial, program, interval_months, held_by=None, calibration=None, by, today):
  Edit; the tag as a device's (TAG_VALIDATOR, at most 40, not reserved), unique in any letter case among devices and test equipment
  under lock_tags ("TE-01 is a device's tag"); a second live item (any status but retired) with the same manufacturer, model, and serial
  in any letter case refused; `calibration`: its latest certificate in the same call (a dict of record_calibration's fields), so an item
  going live is not born a GAP.
- update_test_equipment(item, *, by, **fields): Edit; the tag only until its first calibration or use; the interval at Decide once a
  live certificate is on file; held_by (an active technician; the one it has stays); never status (set_status), held_since, or tag_key.
- set_status(item, *, to, reason="", by, today): MOVES (Edit: out for calibration, back from calibration, out of service damaged or
  failed a check, missing, found, returned to its owner; Equipment Approve: retired worn out or lost, reinstated; out of service ->
  in use only by a passed calibration, never by hand; failed calibration only by record_calibration; beyond build35.md, an item out of
  service is retired (worn out: Approve; returned to its owner: Edit)); status_since is today, the day of the change, whatever the
  move; out of service by hand, held_since is today too; leaving out of service clears it. Review fixes: Reinstate and Found land out
  of service when the item must stay out (_held_out, by the one release rule below: a failed newest certificate, held since its day;
  or retired while out of service with no passed certificate dated on or after the day it was held from, held since that day, carried
  from its history), status_since still today; Reinstate refuses a second live record of the same make, model, and serial; an item
  counts as usable on work completed today only when it was in use at some moment of today (rules.in_use_today).
- record_calibration(item, *, calibrated_on, due_on=None, provider, certificate, accreditation, as_found, adjusted, result, by, today):
  Edit; calibrated_on not after today; one live record per day; due_on null exactly when failed, else default
  add_months(calibrated_on, interval_months), typed after calibrated_on and not after the last day of the month
  add_months(calibrated_on, interval) falls in; refused: failed with as found in tolerance; out of tolerance, not adjusted, and passed.
  Status (status_since today, the day of the change): only when it is the newest live record. One release rule (review fix, release_day):
  passed puts an item out for calibration in use when it is dated on or after the day it went out (status_since), and one out of
  service, whatever the reason, when it is dated on or after held_since (back_in_use_words); failed puts an item in use or out for
  calibration out of service (failed calibration), held since the certificate's own day (never before the day it went out for
  calibration), so a pass dated after it releases it whatever order they are typed in. A record typed late never moves a missing or
  retired item.
- mark_entered_in_error(calibration, *, by, today): Decide; the standing follows from the rest; status does not revert
  (entered_in_error_note says so); a review of it stops counting.
- decide_review(calibration, *, basis, quality_reference="", decided_on, version, by, today): Decide; a live calibration that needs a
  review and has none; decided on or after its day, not after today; `version` the look-back list the reviewer was shown
  (review_version of affected_work), refused if it changed (REVIEW_CHANGED); stores the window and the work orders, frozen.
- Read helpers: affected_work(calibration) (the counted uses' work orders in its window, each use on its own day, used_on),
  review_version(work orders), review_summary(calibration) (the review box: window, work, version, the review, work added since),
  recheck_progress(calibration) (per affected device: rechecked on <day> (work order) when a PM, inspection, safety check, or checked
  repair on it records test equipment used after the calibration's day and IN_DATE that day; else not yet), list_with_standing(qs,
  today) (each item with its standing, for the list, CSV, and API in a fixed number of queries), work_done(item) and
  work_since_last_calibration(item) (by used_on), moves_for(item, user).

The work order's side (the uses; the callers are completion.complete_work_order, workorders.services.set_test_equipment, assign, and
change_status's cancel). Each use has its own day, used_on (the day of the work it was recorded on), written once by apply_uses and
never moved: a reopened work order completed again keeps its uses' days, and one added then gets the new day.
- check_uses(wo, ids, *, by, today, errors, completing, repair_check=None): what a door sent (KEEP keeps, but a completion as checked
  by the vendor takes off the uses the kept-use rule does not keep; a list of ids, as text or UUID, or items, replaces the set as a
  diff; [] clears), checked on the locked rows: never a scoped user's (a non-empty list refused; an empty one changes nothing), never a
  user's without Equipment View, never an unknown id or another facility's; on work that is not offered (rules.offered, with the
  outcome chosen in the same call) only kept ids (any other refused in _not_offered's words: VENDOR_CHECKED on a completion as checked
  by the vendor; with kept uses, VENDOR_CHECKED_KEPT or VENDOR_WORK_KEPT, naming them as the only ones to send), the others coming off
  as KEEP takes them off; new items usable today (completing) or not retired before the work's
  day (after completion: judged on its day, flagged by the standing); items already on the work order pass whatever their status;
  removals obey the kept-use rule, each use on its own day (KEPT names it). Refusals keyed test_equipment in `errors`; returns KEEP or a
  UsesChange for apply_uses.
- clear_for_vendor (assign) and clear_for_cancel take off every use but the kept ones.
- picker(wo, user, *, today) the modal's and Change's options; kept_ids(wo); judged_for(wo) / judged_by_work_order(work_orders) the
  drawer row, the print, and the API's field.
- A work order with test equipment recorded is never deleted (TestEquipmentUse.work_order is PROTECT): the API's destroy (the only door
  that deletes a work order) catches the ProtectedError and refuses in delete_refusal(wo)'s words, as it does an incident's (a
  cancelled one's say that the uses a calibration since has judged stay on it as the record).
"""
from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.pm.dates import add_months, month_bounds
from apps.vendors.rules import fmt
from apps.vendors.services import KEEP  # one "left out" for every field complete_work_order keeps

from . import permissions as perms
from . import rules
from .models import (
    CERTIFICATE_VALIDATOR,
    INTERVAL_MAX,
    INTERVAL_MIN,
    QUALITY_REFERENCE_VALIDATOR,
    SERIAL_MAX,
    TAG_MAX,
    TEXT_MAX,
    AsFound,
    Basis,
    Calibration,
    Kind,
    Mark,
    Program,
    Reason,
    Result,
    Status,
    TestEquipment,
    TestEquipmentUse,
    ToleranceReview,
)

REVIEW_CHANGED = "The work done with it changed meanwhile. Read the list again, then decide."

EDIT_PERMISSION = "Changing test equipment needs Equipment Edit."
DECIDE_PERMISSION = "Deciding about test equipment's records needs Work orders Approve and Equipment View."
RETIRE_PERMISSION = "Retiring or reinstating test equipment needs Equipment Approve."
INTERVAL_PERMISSION = ("A certificate is on file, so changing the interval needs Work orders Approve: the interval caps a certificate's "
                       "due day.")
CHOOSE = "Choose test equipment from this facility."
CHOOSE_CALIBRATION = "Choose a calibration of this facility's test equipment."

# The work order's side (keyed test_equipment)
LIST_ONLY = "Send the test equipment as a list of ids ([] for none)."
SCOPED = "The facility's CE staff record the test equipment used."
VIEW_NEEDED = "Recording the test equipment used needs Equipment View too: the test equipment and its records are Equipment's."
IMPORTED = "{number} was imported from another system: it records no test equipment."
VENDOR_WORK = "{number} is vendor service: the vendor's report names the test equipment it used, so record none here."
VENDOR_CHECKED = ("Checked by the vendor: the vendor's report names the test equipment it used, so record none here; ours is recorded "
                  "on work completed as checked and ready for use.")  # review fix: true while completing and after (never an action refused)
# Review fix: with kept uses on the work order, the refusal names what to send ([] would be refused next, in KEPT's words). `tags` the
# kept items' tags ("TE-K", "TE-K and TE-L"); `left_out` LEFT_OUT while completing (left out, the completion keeps them), else "".
VENDOR_CHECKED_KEPT = ("Checked by the vendor: the vendor's report names the test equipment it used, and {number} keeps {tags} (a "
                       "calibration since has judged the work). Send only {tags}{left_out}; ours is recorded on work completed as checked and "
                       "ready for use.")
VENDOR_WORK_KEPT = ("{number} is vendor service: the vendor's report names the test equipment it used, and it keeps {tags} (a calibration "
                    "since has judged the work). Send only {tags}{left_out}.")
LEFT_OUT = ", or leave test_equipment out"
NOT_USABLE = "{tag} is {status}: only test equipment in use is recorded on work completed today."
RETIRED_BEFORE = "{tag} was retired on {day}, before this work was done."
KEPT = ("{tag} stays on {number}: a calibration dated on or after the day of this work ({day}) is on file, so the work has been judged "
        "with it.")
NOT_DONE = "{number} is not completed yet: record the test equipment when completing it."
SET_PERMISSION = "Recording the test equipment used needs Work orders Edit (Approve once the work order is closed)."
DELETE_REFUSED = "{number} has test equipment recorded, so it is not deleted: it is the record of the work done with it."
DELETE_CANCELLED = ("{number} was cancelled; the test equipment a calibration since has judged stays on it as the record, so it is not "
                    "deleted.")
CANCELLED = "{number} was cancelled: it records no test equipment."
VENDOR_TAKEN_OFF = "Taken off: completed as checked by the vendor"

YOURS_MAX = 8
RECENT_DAYS = 30
OTHERS_OPEN_MAX = 12
LIVE_STATUSES = tuple(s for s in Status.values if s != Status.RETIRED)  # "a second live item": positive, every status but retired
ITEM_FIELDS = ("tag", "kind", "manufacturer", "model", "serial", "program", "interval_months", "held_by")
CALIBRATION_FIELDS = ("calibrated_on", "due_on", "provider", "certificate", "accreditation", "as_found", "adjusted", "result")


# --- small parts -----------------------------------------------------------------------------------------------------------------

def _today(today: date | None) -> date:
    return today or timezone.localdate()


def _check(by, allowed, message: str) -> None:
    """The level check every write starts with, for a user given as `by` (None is the system: the seed, tests)."""
    if by is not None and not allowed(by):
        raise PermissionDenied(message)


def _reason_max(model) -> int:
    return model.history.model._meta.get_field("history_change_reason").max_length  # 100: longer is a database error on PostgreSQL


def _save(obj, by, reason: str, **kwargs) -> None:
    """Save `obj` with `reason` in its history (cut to the column), as `by`; neither reaches a later save of this copy."""
    obj._change_reason = reason[:_reason_max(type(obj))]
    if by is not None:
        obj._history_user = by
    try:
        obj.save(**kwargs)
    finally:
        obj.__dict__.pop("_change_reason", None)
        obj.__dict__.pop("_history_user", None)


def _delete(obj, by, reason: str) -> None:
    obj._change_reason = reason[:_reason_max(type(obj))]
    if by is not None:
        obj._history_user = by
    obj.delete()


def _uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value.strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _locked(item) -> TestEquipment:
    """The item's row as it is now, locked until the transaction ends (an id or an item; this facility's only)."""
    pk = item.pk if isinstance(item, TestEquipment) else _uuid(item)
    found = TestEquipment.objects.select_for_update().filter(pk=pk).first() if pk is not None else None
    if found is None:
        raise ValidationError(CHOOSE)
    return found


def _sync(item, locked: TestEquipment) -> None:
    """The caller's copy follows the row as saved."""
    if isinstance(item, TestEquipment) and item is not locked:
        item.__dict__.update({f.attname: getattr(locked, f.attname) for f in TestEquipment._meta.concrete_fields})


def _text(value, field: str, what: str, limit: int, errors: dict, *, required: bool = True) -> str:
    """Text as typed, inner spaces collapsed: required unless said, at most `limit` characters, no control characters."""
    raw = str(value if value is not None else "")
    if any(unicodedata.category(ch) == "Cc" and not ch.isspace() for ch in raw):
        errors[field] = f"Remove the invisible control character from the {what}."
        return ""
    text = " ".join(raw.split())
    if not text:
        if required:
            errors[field] = f"Enter the {what}."
        return ""
    if len(text) > limit:
        errors[field] = f"Keep the {what} to {limit} characters."
    return text


def _token(value, field: str, what: str, validator, limit: int, errors: dict, *, required: bool) -> str:
    """A reference number as a token (core.validators.reference_token): no spaces."""
    text = str(value if value is not None else "").strip()
    if not text:
        if required:
            errors[field] = f"Enter the {what}."
        return ""
    if len(text) > limit:
        errors[field] = f"Keep the {what} to {limit} characters."
        return text
    try:
        validator(text)
    except ValidationError as e:
        errors[field] = " ".join(e.messages)
    return text


def _day(value, field: str, what: str, errors: dict, *, required: bool = True) -> date | None:
    """A date from a date or its ISO text; None for nothing (required: refused)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            errors[field] = f"Enter the {what}."
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            errors[field] = f"Enter the {what} as a date."
            return None
    if isinstance(value, datetime) or not isinstance(value, date):
        errors[field] = f"Enter the {what} as a date."
        return None
    return value


def _choice(value, choices, field: str, words: str, errors: dict) -> str:
    text = str(value if value is not None else "").strip()
    if text not in choices.values:
        errors[field] = words
        return ""
    return text


def _interval(value, errors: dict) -> int | None:
    words = f"Enter the interval in months, from {INTERVAL_MIN} to {INTERVAL_MAX}."
    if isinstance(value, bool):
        errors["interval_months"] = words
        return None
    try:
        n = int(value.strip()) if isinstance(value, str) else value if isinstance(value, int) else None
    except ValueError:
        n = None
    if n is None or not INTERVAL_MIN <= n <= INTERVAL_MAX:
        errors["interval_months"] = words
        return None
    return n


def _tag(value, errors: dict) -> str:
    from apps.equipment.models import TAG_VALIDATOR
    from apps.equipment.services import RESERVED_TAGS

    tag = str(value if value is not None else "").strip()
    if not tag:
        errors["tag"] = "Enter the tag on its CE sticker."
        return ""
    if len(tag) > TAG_MAX or len(tag.casefold()) > TAG_MAX:
        errors["tag"] = f"Tags are at most {TAG_MAX} characters."
        return tag
    try:
        TAG_VALIDATOR(tag)
    except ValidationError:
        errors["tag"] = "Tags cannot contain spaces or slashes."
        return tag
    if tag.lower() in RESERVED_TAGS:
        errors["tag"] = f'"{tag}" cannot be used as a tag.'
    return tag


def _technician(value, errors: dict, *, current_id=None):
    """A technician of this facility, or None for nobody; a new one must be active (the one an item has stays)."""
    from apps.credentials.models import Technician

    if value is None or value == "":
        return None
    pk = value.pk if isinstance(value, Technician) else _uuid(value)
    tech = Technician.objects.filter(pk=pk).first() if pk is not None else None
    if tech is None:
        errors["held_by"] = "Choose a technician from this facility."
        return None
    if not tech.is_active and tech.pk != current_id:
        errors["held_by"] = f"{tech.name} is no longer active: choose an active technician."
    return tech


def _clean_item(raw: dict, errors: dict) -> dict:
    """The item's fields in `raw` (any of ITEM_FIELDS but held_by, which needs the row), cleaned; refusals in `errors`."""
    out = {}
    if "tag" in raw:
        out["tag"] = _tag(raw["tag"], errors)
    if "kind" in raw:
        out["kind"] = _choice(raw["kind"], Kind, "kind", "Choose the kind of test equipment.", errors)
    if "manufacturer" in raw:
        out["manufacturer"] = _text(raw["manufacturer"], "manufacturer", "manufacturer", TEXT_MAX, errors)
    if "model" in raw:
        out["model"] = _text(raw["model"], "model", "model", TEXT_MAX, errors)
    if "serial" in raw:
        out["serial"] = _text(raw["serial"], "serial", "serial number its certificates name", SERIAL_MAX, errors)
    if "program" in raw:
        out["program"] = _choice(raw["program"], Program, "program", "Choose what its certificates are: calibration or verification.", errors)
    if "interval_months" in raw:
        out["interval_months"] = _interval(raw["interval_months"], errors)
    return out


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split()).casefold()


def _same_unit(manufacturer: str, model: str, serial: str, exclude=None) -> TestEquipment | None:
    """A live item (any status but retired) with this manufacturer, model, and serial in any letter case, other than `exclude`.
    Compared in Python (SQLite's iexact folds ASCII only); a facility has tens of items."""
    key = (_fold(manufacturer), _fold(model), _fold(serial))
    qs = TestEquipment.objects.filter(status__in=LIVE_STATUSES)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return next((i for i in qs.only("pk", "tag", "manufacturer", "model", "serial")
                 if (_fold(i.manufacturer), _fold(i.model), _fold(i.serial)) == key), None)


def _tag_refusal(tag: str, exclude=None) -> str:
    """Why `tag` is not free in the facility's tags (devices and test equipment, any letter case), or "". Under lock_tags."""
    from apps.equipment.models import Asset

    if Asset.objects.filter(tag__iexact=tag).exists():
        return f"{tag} is a device's tag."
    others = TestEquipment.objects.filter(tag_key=tag.casefold())
    if exclude is not None:
        others = others.exclude(pk=exclude.pk)
    if others.exists():
        return f"{tag} is already another test equipment item's tag."
    return ""


def _unit_refusal(found: TestEquipment, manufacturer: str, model: str, serial: str) -> str:
    return f"{manufacturer} {model} serial {serial} is already {found.tag}: one item, one tag."


# --- calibrations: the fields ---------------------------------------------------------------------------------------------------

def due_cap(calibrated_on: date, interval_months: int) -> date:
    """The latest due day a certificate may carry: the last day of the month add_months(calibrated_on, interval) falls in (a "Due
    03/2027" label is the month's end; a typo never adds a year)."""
    default = add_months(calibrated_on, interval_months)
    return month_bounds(default.year, default.month)[1]


def default_due(calibrated_on: date, interval_months: int) -> date:
    """The due day a passed certificate gets when none is typed: add_months(calibrated_on, interval_months) (Fluke and most labs)."""
    return add_months(calibrated_on, interval_months)


def _clean_calibration(raw: dict, *, interval: int | None, program: str, today: date, errors: dict) -> dict:
    """record_calibration's fields, checked (the module docstring); refusals keyed by field in `errors`."""
    unknown = set(raw) - set(CALIBRATION_FIELDS)
    if unknown:
        raise TypeError(f"Not a calibration's field: {', '.join(sorted(unknown))}")
    what = "verification" if program == Program.VERIFICATION else "calibration"
    calibrated_on = _day(raw.get("calibrated_on"), "calibrated_on", f"{what} day on the certificate", errors)
    if calibrated_on is not None and calibrated_on > today:
        errors["calibrated_on"] = f"The {what} day cannot be in the future."
        calibrated_on = None
    provider = _text(raw.get("provider"), "provider", "lab or manufacturer that issued the certificate", TEXT_MAX, errors)
    certificate = _token(raw.get("certificate"), "certificate", "certificate's number", CERTIFICATE_VALIDATOR,
                         Calibration._meta.get_field("certificate").max_length, errors, required=True)
    accreditation = _choice(raw.get("accreditation"), Mark, "accreditation",
                            "Say whether the certificate carries an ISO/IEC 17025 accreditation mark.", errors)
    as_found = _choice(raw.get("as_found"), AsFound, "as_found",
                       "Choose what the certificate says as found: in tolerance, out of tolerance, or not reported.", errors)
    adjusted = raw.get("adjusted", False)
    if not isinstance(adjusted, bool):
        errors["adjusted"] = "Say whether it was adjusted: yes or no."
        adjusted = False
    result = _choice(raw.get("result"), Result, "result", "Choose the result: passed or failed.", errors)
    due_raw = raw.get("due_on")
    due_on = None
    if result == Result.FAILED:
        if not (due_raw is None or (isinstance(due_raw, str) and not due_raw.strip())):
            errors["due_on"] = f"A failed {what} has no due day: leave it blank."
    elif result == Result.PASSED:
        typed = _day(due_raw, "due_on", "due day", errors, required=False)
        if calibrated_on is not None and interval is not None:
            if typed is None and "due_on" not in errors:
                due_on = default_due(calibrated_on, interval)
            elif typed is not None:
                cap = due_cap(calibrated_on, interval)
                if typed <= calibrated_on:
                    errors["due_on"] = f"The due day comes after the {what} day."
                elif typed > cap:
                    errors["due_on"] = (f"The due day is at most {interval} months after the {what} day (by {fmt(cap)}): check the "
                                        "certificate, or the item's interval.")
                due_on = typed
    if result == Result.FAILED and as_found == AsFound.IN_TOLERANCE:
        errors["as_found"] = (f"A failed {what} could not be brought into tolerance, so it was not in tolerance as found: check as found "
                              "or the result.")
    if as_found == AsFound.OUT_OF_TOLERANCE and result == Result.PASSED and not adjusted and "adjusted" not in errors:
        errors["adjusted"] = "Out of tolerance as found and passed means it was adjusted: tick adjusted, or check the result."
    return {"calibrated_on": calibrated_on, "due_on": due_on, "provider": provider, "certificate": certificate,
            "accreditation": accreditation, "as_found": as_found, "adjusted": adjusted, "result": result}


def _same_day_refusal(item: TestEquipment, record: Calibration) -> ValidationError:
    what = rules.noun(item).lower()
    return ValidationError({"calibrated_on": (f"{item.tag} already has a {what} on {fmt(record.calibrated_on)} (certificate "
                                              f"{record.certificate}). Mark the other entered in error first.")})


def _record(locked: TestEquipment, cleaned: dict, *, by, today: date) -> Calibration:
    """Insert a calibration of the item (its row locked), then move its status when it is the newest live record (_status_after)."""
    same = Calibration.objects.filter(test_equipment=locked, calibrated_on=cleaned["calibrated_on"], entered_in_error=False).first()
    if same is not None:
        raise _same_day_refusal(locked, same)
    cal = Calibration(tenant_id=locked.tenant_id, test_equipment=locked, recorded_by=by, **cleaned)
    try:
        with transaction.atomic():
            _save(cal, by, f"{rules.noun(locked)} recorded")
    except IntegrityError:  # the item's row is locked, so only a record outside the services gets here
        raise _same_day_refusal(locked, Calibration.objects.filter(test_equipment=locked, calibrated_on=cal.calibrated_on,
                                                                   entered_in_error=False).first() or cal) from None
    newest = not Calibration.objects.filter(test_equipment=locked, entered_in_error=False, calibrated_on__gt=cal.calibrated_on).exists()
    if newest:
        _status_after(locked, cal, by=by, today=today)
    return cal


def release_day(item) -> date:
    """The day from which a passed certificate puts `item` back in use (the one release rule): out of service, held_since (its
    status_since for a row written without one); out for calibration, the day it went out (status_since)."""
    if item.status == Status.OUT_OF_SERVICE and item.held_since is not None:
        return item.held_since
    return item.status_since


def _status_after(locked: TestEquipment, cal: Calibration, *, by, today: date) -> None:
    """The status a newest live record moves (the module docstring), status_since today (the day of the change); nothing for a missing
    or retired item. One release rule (review fix): out for calibration or out of service, whatever the reason, goes back in use on a
    passed certificate dated on or after release_day (out of service: held_since, cleared then). A failed one takes an item in use or
    out for calibration out of service (failed calibration), held since the certificate's own day (the lab's; for an item out for
    calibration, never before the day it went out), so a pass dated after it releases it whatever order the two are typed in, and a
    pass dated before it never does."""
    what = rules.noun(locked).lower()
    if cal.result == Result.PASSED:
        if locked.status not in (Status.OUT_FOR_CALIBRATION, Status.OUT_OF_SERVICE) or cal.calibrated_on < release_day(locked):
            return
        to, reason, held, note = Status.IN_USE, "", None, f"Back in use: {what} passed {fmt(cal.calibrated_on)}"
    else:
        if locked.status not in (Status.IN_USE, Status.OUT_FOR_CALIBRATION):
            return
        held = max(cal.calibrated_on, locked.status_since) if locked.status == Status.OUT_FOR_CALIBRATION else cal.calibrated_on
        to, reason, note = Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, f"Out of service: failed {what} {fmt(cal.calibrated_on)}"
    locked.status, locked.status_reason, locked.status_since, locked.held_since = to, reason, today, held
    _save(locked, by, note, update_fields=["status", "status_reason", "status_since", "held_since", "updated_at"])


# --- items ---------------------------------------------------------------------------------------------------------------------

def add_test_equipment(*, tag, kind, manufacturer, model, serial, program, interval_months, held_by=None, calibration=None, by, today=None):
    """Add an item in use (status since today), with its latest certificate when `calibration` gives one (the module docstring): one
    that failed adds it out of service (failed calibration), status since today and held since the certificate's day. Problems with
    the fields come keyed by field, all at once (the certificate's beside the item's)."""
    from apps.equipment.services import lock_tags

    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = _today(today)
    errors: dict = {}
    fields = _clean_item({"tag": tag, "kind": kind, "manufacturer": manufacturer, "model": model, "serial": serial, "program": program,
                          "interval_months": interval_months}, errors)
    holder = _technician(held_by, errors)
    cleaned = None
    if calibration is not None:
        if not isinstance(calibration, dict):
            raise TypeError("calibration is a dict of record_calibration's fields")
        cleaned = _clean_calibration(calibration, interval=fields.get("interval_months"), program=fields.get("program"), today=today,
                                     errors=errors)
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        lock_tags()  # the facility's tags: checked and inserted under it (equipment.services.create_asset takes it too)
        refusal = _tag_refusal(fields["tag"])
        if refusal:
            raise ValidationError({"tag": refusal})
        found = _same_unit(fields["manufacturer"], fields["model"], fields["serial"])
        if found is not None:
            raise ValidationError({"serial": _unit_refusal(found, fields["manufacturer"], fields["model"], fields["serial"])})
        # Review fix: an item whose first certificate failed is never "in use" for a moment today: out of service (failed calibration)
        # from its first row, held since the certificate's day, as a failed certificate takes an item in use out (_status_after)
        failed = cleaned is not None and cleaned.get("result") == Result.FAILED
        item = TestEquipment(**fields, tag_key=fields["tag"].casefold(), held_by=holder, status=Status.OUT_OF_SERVICE if failed else Status.IN_USE,
                             status_reason=Reason.FAILED_CALIBRATION if failed else "", status_since=today,
                             held_since=cleaned["calibrated_on"] if failed else None, created_by=by)
        _save(item, by, "Added")
        if cleaned is not None:
            _record(item, cleaned, by=by, today=today)
    return item


def update_test_equipment(item, *, by, today=None, **fields):
    """Change an item's details (ITEM_FIELDS; the module docstring): the tag only until its first calibration or use, the interval at
    Decide once a live certificate is on file, held_by an active technician (the one it has stays). Problems keyed by field."""
    from apps.equipment.services import lock_tags

    unknown = set(fields) - set(ITEM_FIELDS)
    if unknown:
        raise ValidationError({f: ("Change the status with its own action." if f in ("status", "status_reason", "status_since", "held_since") else
                                   "This is not changed here.") for f in sorted(unknown)})
    _check(by, perms.can_edit, EDIT_PERMISSION)
    errors: dict = {}
    cleaned = _clean_item(fields, errors)
    if errors:
        raise ValidationError(errors)
    identity = {"tag", "manufacturer", "model", "serial"} & set(fields)
    with transaction.atomic():
        if identity:
            lock_tags()  # before the item's row: the duplicate checks run under the facility's tag lock
        locked = _locked(item)
        changed = {k: v for k, v in cleaned.items() if getattr(locked, k) != v}
        if "held_by" in fields:
            holder = _technician(fields["held_by"], errors, current_id=locked.held_by_id)
            if (holder.pk if holder else None) != locked.held_by_id:
                changed["held_by"] = holder
        if "tag" in changed:
            if Calibration.objects.filter(test_equipment=locked).exists() or TestEquipmentUse.objects.filter(test_equipment=locked).exists():
                errors["tag"] = f"{locked.tag} has calibrations or work on record, so its tag stays."
            else:
                refusal = _tag_refusal(changed["tag"], exclude=locked)
                if refusal:
                    errors["tag"] = refusal
        if {"manufacturer", "model", "serial"} & set(changed) and locked.status != Status.RETIRED:
            make = (changed.get("manufacturer", locked.manufacturer), changed.get("model", locked.model), changed.get("serial", locked.serial))
            found = _same_unit(*make, exclude=locked)
            if found is not None:
                errors["serial"] = _unit_refusal(found, *make)
        if errors:
            raise ValidationError(errors)
        if "interval_months" in changed and Calibration.objects.filter(test_equipment=locked, entered_in_error=False).exists():
            _check(by, perms.can_decide, INTERVAL_PERMISSION)
        if not changed:
            return locked
        reason = f"Tag corrected: {locked.tag} to {changed['tag']}" if "tag" in changed else "Edited"
        for k, v in changed.items():
            setattr(locked, k, v)
        update = [k for k in changed]
        if "tag" in changed:
            locked.tag_key = locked.tag.casefold()
            update.append("tag_key")
        _save(locked, by, reason, update_fields=[*update, "updated_at"])
    _sync(item, locked)
    return locked


# --- status --------------------------------------------------------------------------------------------------------------------

EDIT, RETIRE = "edit", "retire"


@dataclass(frozen=True)
class Move:
    """One status move by hand: from `from_status` to `to` with `reason` ("" when the status takes none), at `level` (EDIT: Equipment
    Edit; RETIRE: Equipment Approve), `label` the button's words, `note` the history's reason."""
    from_status: str
    to: str
    reason: str
    level: str
    label: str
    note: str


MOVES = (
    Move(Status.IN_USE, Status.OUT_FOR_CALIBRATION, "", EDIT, "Out for calibration", "Out for calibration"),
    Move(Status.IN_USE, Status.OUT_OF_SERVICE, Reason.DAMAGED, EDIT, "Out of service: damaged", "Out of service: damaged"),
    Move(Status.IN_USE, Status.OUT_OF_SERVICE, Reason.FAILED_CHECK, EDIT, "Out of service: failed a check", "Out of service: failed a check"),
    Move(Status.IN_USE, Status.MISSING, "", EDIT, "Missing", "Missing"),
    Move(Status.IN_USE, Status.RETIRED, Reason.RETURNED_TO_OWNER, EDIT, "Returned to its owner", "Retired: returned to its owner"),
    Move(Status.OUT_FOR_CALIBRATION, Status.IN_USE, "", EDIT, "Back from calibration", "Back from calibration"),
    Move(Status.MISSING, Status.IN_USE, "", EDIT, "Found", "Found"),
    Move(Status.IN_USE, Status.RETIRED, Reason.WORN_OUT, RETIRE, "Retire: worn out or obsolete", "Retired: worn out or obsolete"),
    Move(Status.MISSING, Status.RETIRED, Reason.LOST, RETIRE, "Retire: lost", "Retired: lost"),
    Move(Status.RETIRED, Status.IN_USE, "", RETIRE, "Reinstate", "Reinstated"),
    # Beyond build35.md: an item out of service (damaged, failed a check or its calibration) leaves the department too
    Move(Status.OUT_OF_SERVICE, Status.RETIRED, Reason.WORN_OUT, RETIRE, "Retire: worn out or obsolete", "Retired: worn out or obsolete"),
    Move(Status.OUT_OF_SERVICE, Status.RETIRED, Reason.RETURNED_TO_OWNER, EDIT, "Returned to its owner", "Retired: returned to its owner"),
)


def moves_from(status: str) -> list[Move]:
    return [m for m in MOVES if m.from_status == status]


def moves_for(item, user) -> list[Move]:
    """The moves `user` may make on `item` now (the drawer's status actions): by level, never for a scoped user."""
    allowed = {EDIT: perms.can_edit(user), RETIRE: perms.can_retire(user)}
    return [m for m in moves_from(item.status) if allowed[m.level]]


def back_in_use_words(item) -> str:
    """How an item out of service goes back in use (set_status's refusal, the drawer's line): the one release rule, whatever the reason
    (_status_after: a passed certificate dated on or after release_day, its held_since)."""
    what = rules.noun(item).lower()
    reason = Reason(item.status_reason).label.lower() if item.status_reason else "out of service"
    return (f"{item.tag} is out of service ({reason}): it goes back in use when a passed {what} dated on or after "
            f"{fmt(release_day(item))} is recorded.")


def _move(locked: TestEquipment, to: str, reason: str) -> Move:
    """The move from the item's status to `to` with `reason`, or a refusal in words."""
    if reason == Reason.FAILED_CALIBRATION:
        raise ValidationError({"status_reason": "A failed calibration is recorded with the calibration itself (Record calibration, "
                                                "result Failed)."})
    if to == Status.IN_USE and locked.status == Status.OUT_OF_SERVICE:
        raise ValidationError({"status": back_in_use_words(locked)})
    if to == locked.status:
        raise ValidationError({"status": f"{locked.tag} is already {Status(to).label.lower()}."})
    options = moves_from(locked.status)
    to_this = [m for m in options if m.to == to]
    if not to_this:
        where = sorted({Status(m.to).label.lower() for m in options})
        raise ValidationError({"status": f"{locked.tag} is {Status(locked.status).label.lower()}: from there it goes "
                                         f"{' or '.join(where) or 'nowhere by hand'}."})
    move = next((m for m in to_this if m.reason == reason), None)
    if move is None:
        if to_this[0].reason:
            words = " or ".join(Reason(m.reason).label.lower() for m in to_this)
            raise ValidationError({"status_reason": f"Choose why: {words}."})
        raise ValidationError({"status_reason": "Only out of service and retired take a reason."})
    return move


def _held_out(locked: TestEquipment, today: date) -> tuple[str, date | None]:
    """Review fix: why an item coming back from retired or missing lands out of service, never in use, and the day it is held from
    (its held_since): ("", None) to land in use. Judged by its newest live certificate against how it left, by the one release rule
    (_status_after: a passed certificate dated on or after the day it is held from). Retired while out of service (its history's last
    row before retirement: the reason and that row's held_since, or its status_since for a row written without one): it lands in use
    when the newest live certificate passed and is dated on or after that day; out of service (failed calibration) held since that
    certificate's day when it failed and is dated on or after that day; else with the history's reason, held since the history's day (a
    round trip never moves it). Otherwise (Found, or retired from use) a failed newest certificate holds it out (failed calibration,
    held since its day). One or two queries. The move's own day (status_since) is today whatever this says."""
    from apps.core.history import _rows

    newest = Calibration.objects.filter(test_equipment=locked, entered_in_error=False).order_by("-calibrated_on").first()
    if locked.status == Status.RETIRED:
        before = (_rows(TestEquipment.history.model).filter(id=locked.pk).exclude(status=Status.RETIRED)
                  .order_by("-history_date", "-history_id").values_list("status", "status_reason", "held_since", "status_since").first())
        if before is not None and before[0] == Status.OUT_OF_SERVICE:
            reason, held = before[1] or Reason.DAMAGED, before[2] or before[3] or today
            if newest is not None and newest.calibrated_on >= held:
                return ("", None) if newest.result == Result.PASSED else (Reason.FAILED_CALIBRATION, newest.calibrated_on)
            return reason, held
    if newest is not None and newest.result == Result.FAILED:
        return Reason.FAILED_CALIBRATION, newest.calibrated_on
    return "", None


def set_status(item, *, to, reason="", by, today=None):
    """Move an item's status by hand (MOVES; the module docstring): status_since today, the day of the change; held_since today when it
    goes out of service, cleared when it leaves. Reinstate and Found land out of service (with the reason and the day it is held from,
    _held_out) when the item's newest certificate failed, or it was out of service when it was retired and nothing passed it since;
    Reinstate refuses a second live record of the same make, model, and serial (under lock_tags, before the item's row). Returns the
    row."""
    from apps.equipment.services import lock_tags

    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = _today(today)
    to, reason = str(to or "").strip(), str(reason or "").strip()
    if to not in Status.values:
        raise ValidationError({"status": "Choose a status listed."})
    if reason and reason not in Reason.values:
        raise ValidationError({"status_reason": "Choose a reason listed."})
    with transaction.atomic():
        if to == Status.IN_USE:
            lock_tags()  # review fix: a reinstated item becomes live again: the duplicate check runs under the facility's tag lock
        locked = _locked(item)
        move = _move(locked, to, reason)
        if move.level == RETIRE:
            _check(by, perms.can_retire, RETIRE_PERMISSION)
        status, why, note = move.to, move.reason, move.note
        held = today if move.to == Status.OUT_OF_SERVICE else None  # by hand: damaged or failed a check, held from the day of the move
        if locked.status == Status.RETIRED and move.to != Status.RETIRED:
            found = _same_unit(locked.manufacturer, locked.model, locked.serial, exclude=locked)
            if found is not None:
                raise ValidationError({"status": _unit_refusal(found, locked.manufacturer, locked.model, locked.serial)})
        if move.to == Status.IN_USE and locked.status in (Status.RETIRED, Status.MISSING):
            out, held_from = _held_out(locked, today)
            if out:
                status, why, held = Status.OUT_OF_SERVICE, out, held_from
                note = f"{move.note}: out of service ({Reason(out).label.lower()})"
        locked.status, locked.status_reason, locked.status_since, locked.held_since = status, why, today, held
        _save(locked, by, note, update_fields=["status", "status_reason", "status_since", "held_since", "updated_at"])
    _sync(item, locked)
    return locked


# --- calibrations --------------------------------------------------------------------------------------------------------------

def record_calibration(item, *, calibrated_on, due_on=None, provider, certificate, accreditation, as_found, adjusted, result, by, today=None):
    """Record a certificate of the item (the module docstring). Problems keyed by field, all at once. Returns the calibration."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = _today(today)
    raw = {"calibrated_on": calibrated_on, "due_on": due_on, "provider": provider, "certificate": certificate,
           "accreditation": accreditation, "as_found": as_found, "adjusted": adjusted, "result": result}
    with transaction.atomic():
        locked = _locked(item)
        errors: dict = {}
        cleaned = _clean_calibration(raw, interval=locked.interval_months, program=locked.program, today=today, errors=errors)
        if errors:
            raise ValidationError(errors)
        cal = _record(locked, cleaned, by=by, today=today)
    _sync(item, locked)
    return cal


def _calibration_item_id(calibration):
    pk = calibration.pk if isinstance(calibration, Calibration) else _uuid(calibration)
    item_id = Calibration.objects.filter(pk=pk).values_list("test_equipment_id", flat=True).first() if pk is not None else None
    if item_id is None:
        raise ValidationError(CHOOSE_CALIBRATION)
    return pk, item_id


def mark_entered_in_error(calibration, *, by, today=None):
    """Mark a calibration entered in error (never deleted): the item's standing and due day follow from its other records; its status
    stays (entered_in_error_note); a review of it stops counting. Returns the calibration."""
    _check(by, perms.can_decide, DECIDE_PERMISSION)
    today = _today(today)
    pk, item_id = _calibration_item_id(calibration)
    with transaction.atomic():
        _locked(item_id)
        cal = Calibration.objects.get(pk=pk)
        if cal.entered_in_error:
            raise ValidationError(f"Certificate {cal.certificate} is already marked entered in error.")
        cal.entered_in_error, cal.entered_in_error_by, cal.entered_in_error_on = True, by, today
        _save(cal, by, "Entered in error", update_fields=["entered_in_error", "entered_in_error_by", "entered_in_error_on", "updated_at"])
    if isinstance(calibration, Calibration):
        calibration.entered_in_error, calibration.entered_in_error_by_id, calibration.entered_in_error_on = True, cal.entered_in_error_by_id, today
    return cal


def entered_in_error_note(calibration) -> str:
    """What marking `calibration` entered in error did, in words (the toast): "Certificate C-1 marked entered in error. TE-01's
    standing follows its other calibrations; its status (out of service) stays as it is.\""""
    item = calibration.test_equipment
    return (f"Certificate {calibration.certificate} marked entered in error. {item.tag}'s standing follows its other calibrations; its "
            f"status ({Status(item.status).label.lower()}) stays as it is.")


# --- the tolerance review --------------------------------------------------------------------------------------------------------

def affected_work(calibration) -> list:
    """The work orders whose counted uses of the calibration's item fall in its look-back window (rules.window, on each use's own day:
    used_on), by that day and number, each with its device and `used_on` (the use's day, the one the window reads). Two queries."""
    cals = list(Calibration.objects.filter(test_equipment_id=calibration.test_equipment_id))
    bounds = rules.window(calibration, cals)
    uses = (TestEquipmentUse.objects.filter(rules.counted_uses_q(), rules.window_q(bounds), test_equipment_id=calibration.test_equipment_id)
            .select_related("work_order__asset").order_by("used_on", "work_order__number"))
    out = []
    for u in uses:
        u.work_order.used_on = u.used_on  # one use per work order and item
        out.append(u.work_order)
    return out


def review_version(work_orders) -> str:
    """The look-back list a reviewer was shown, as a short token (work orders or their ids, any order): decide_review refuses when the
    list changed since (REVIEW_CHANGED), as vendor_reviews.fields_version guards a sign-off."""
    ids = sorted(str(getattr(w, "pk", w)) for w in work_orders)
    return hashlib.sha256(",".join(ids).encode()).hexdigest()[:16]


def decide_review(calibration, *, basis, quality_reference="", decided_on, version, by, today=None):
    """Record the decision on the work done with the item in the calibration's look-back window (the module docstring). `version` is
    review_version of the list shown (None only where nobody was shown a page: the seed, a test). Returns the review."""
    _check(by, perms.can_decide, DECIDE_PERMISSION)
    today = _today(today)
    errors: dict = {}
    basis = _choice(basis, Basis, "basis", "Choose the decision.", errors)
    reference = _token(quality_reference, "quality_reference", "quality record's number", QUALITY_REFERENCE_VALIDATOR,
                       ToleranceReview._meta.get_field("quality_reference").max_length, errors, required=False)
    decided_on = _day(decided_on, "decided_on", "day it was decided", errors)
    pk, item_id = _calibration_item_id(calibration)
    with transaction.atomic():
        _locked(item_id)
        cal = Calibration.objects.select_related("test_equipment").get(pk=pk)
        if cal.entered_in_error:
            raise ValidationError(f"Certificate {cal.certificate} was marked entered in error: there is nothing to review.")
        if not cal.needs_review:
            raise ValidationError(f"Certificate {cal.certificate} found it in tolerance and it passed: there is nothing to review.")
        if ToleranceReview.objects.filter(calibration=cal).exists():
            raise ValidationError(f"The review of certificate {cal.certificate} is already recorded.")
        if decided_on is not None:
            if decided_on < cal.calibrated_on:
                errors["decided_on"] = f"The decision comes on or after the calibration day ({fmt(cal.calibrated_on)})."
            elif decided_on > today:
                errors["decided_on"] = "The day it was decided cannot be in the future."
        if errors:
            raise ValidationError(errors)
        work = affected_work(cal)
        if version is not None and str(version) != review_version(work):
            raise ValidationError(REVIEW_CHANGED)
        start, end = rules.window(cal, Calibration.objects.filter(test_equipment_id=item_id))
        frozen = sorted(([str(w.pk), w.number] for w in work), key=lambda row: row[0])
        review = ToleranceReview(tenant_id=cal.tenant_id, calibration=cal, basis=basis, quality_reference=reference, decided_on=decided_on,
                                 decided_by=by, window_start=start, window_end=end, work_orders=frozen, work_count=len(frozen))
        _save(review, by, "Review recorded")
    return review


@dataclass
class ReviewSummary:
    """The review box of one calibration that needs a review: its window, the work in it now (affected_work), the version a decision
    posts, the review (None when not reviewed; never for a calibration entered in error), the work added since the review, and words."""
    calibration: Calibration
    window: tuple
    work: list
    version: str
    review: ToleranceReview | None
    added: list = field(default_factory=list)

    @property
    def words(self) -> str:
        if self.review is None:
            return "Not reviewed"
        return rules.basis_words(self.review)[:1].upper() + rules.basis_words(self.review)[1:]

    @property
    def added_words(self) -> str:
        n = len(self.added)
        return "" if not n else f"{n} work order{'s' if n != 1 else ''} added since the review"


def review_summary(calibration) -> ReviewSummary:
    """The review box (the module docstring). A few queries."""
    work = affected_work(calibration)
    cals = list(Calibration.objects.filter(test_equipment_id=calibration.test_equipment_id))
    review = None if calibration.entered_in_error else ToleranceReview.objects.filter(calibration_id=calibration.pk).select_related("decided_by").first()
    added = [w for w in work if str(w.pk) not in rules.reviewed_ids(review)] if review is not None else []
    return ReviewSummary(calibration, rules.window(calibration, cals), work, review_version(work), review, added)


@dataclass(frozen=True)
class Recheck:
    """One affected device: the first work that rechecked it after the calibration's day (None: not yet), the day of that work's use
    of test equipment (used_on), and the words."""
    asset: object
    work_order: object | None
    day: date | None

    @property
    def words(self) -> str:
        return f"rechecked on {fmt(self.day)} ({self.work_order.number})" if self.work_order is not None else "not yet"


@dataclass(frozen=True)
class RecheckProgress:
    devices: list
    rechecked: int
    total: int

    @property
    def words(self) -> str:
        """"212 of 640 rechecked"."""
        return f"{self.rechecked} of {self.total} rechecked"


def recheck_progress(calibration) -> RecheckProgress:
    """Per affected device (the devices of the review's frozen list, or of the work in the window when it is not reviewed): rechecked
    on <day> (work order) when a counted PM, incoming inspection, safety check, or repair checked and ready for use on it records test
    equipment used after the calibration's day and IN_DATE on that day (each use judged on its own day, used_on; the first such use);
    else not yet. Computed, never stored. A fixed number of queries."""
    from apps.equipment.models import Asset
    from apps.workorders.models import RepairCheck, WorkOrder, WoType

    review = None if calibration.entered_in_error else ToleranceReview.objects.filter(calibration_id=calibration.pk).first()
    if review is not None:
        ids = [row[0] for row in review.work_orders or ()]
        device_ids = set(WorkOrder.objects.filter(pk__in=ids).values_list("asset_id", flat=True))
    else:
        device_ids = {w.asset_id for w in affected_work(calibration)}
    assets = list(Asset.objects.filter(pk__in=device_ids).order_by("tag"))
    kinds = Q(work_order__type__in=rules.RECHECK_TYPES) | Q(work_order__type=WoType.REPAIR, work_order__repair_check=RepairCheck.PASSED)
    uses = list(TestEquipmentUse.objects.filter(rules.counted_uses_q(), kinds, work_order__asset_id__in=device_ids,
                                                used_on__gt=calibration.calibrated_on)
                .select_related("work_order", "test_equipment").order_by("used_on", "work_order__number"))
    cals = rules.calibrations_by_item(u.test_equipment_id for u in uses)
    first: dict = {}
    for u in uses:
        wo = u.work_order
        if wo.asset_id in first:
            continue
        if rules.standing_on(u.test_equipment, u.used_on, cals.get(u.test_equipment_id, ())).state == rules.IN_DATE:
            first[wo.asset_id] = (wo, u.used_on)
    devices = [Recheck(a, *first[a.pk]) if a.pk in first else Recheck(a, None, None) for a in assets]
    return RecheckProgress(devices, sum(1 for d in devices if d.work_order is not None), len(devices))


# --- reads for the list, the item, and the work order --------------------------------------------------------------------------

def list_with_standing(qs, today: date) -> list:
    """The items of `qs`, each with `.standing` (rules.Standing judged `today`): two queries (the items and their calibrations; select
    held_by on `qs` for the list's column)."""
    items = list(qs)
    found = rules.standings(items, today)
    for i in items:
        i.standing = found[i.pk]
    return items


def work_done(item, *, limit: int | None = None) -> list:
    """The counted work done with `item`, newest first by each use's own day (used_on), each judged on it (rules.JudgedUse): the
    drawer's "Work done with it" (limit 10) and its CSV (every use). Four queries."""
    uses = (TestEquipmentUse.objects.filter(rules.counted_uses_q(), test_equipment=item).select_related("work_order__asset", "test_equipment")
            .order_by("-used_on", "-work_order__number"))
    return rules.judge_uses(uses[:limit] if limit else uses, all_counted=True)  # counted_uses_q above


def work_since_last_calibration(item) -> int:
    """How many counted work orders recorded `item` on a day (used_on) after its latest live calibration's day (all of them when it has
    none)."""
    last = Calibration.objects.filter(test_equipment=item, entered_in_error=False).order_by("-calibrated_on").values_list("calibrated_on", flat=True).first()
    qs = TestEquipmentUse.objects.filter(rules.counted_uses_q(), test_equipment=item)
    return (qs.filter(used_on__gt=last) if last else qs).count()


def judged_for(wo) -> list:
    """The test equipment `wo` recorded, each judged on its own day (used_on: rules.JudgedUse), by tag: the drawer's row and the print.
    At most four queries."""
    uses = TestEquipmentUse.objects.filter(work_order=wo).select_related("test_equipment", "work_order").order_by("test_equipment__tag")
    return rules.judge_uses(uses)


def judged_by_work_order(work_orders) -> dict:
    """{work order id: [rules.JudgedUse, by tag]} for `work_orders` (the API's list): four queries for any number of work orders."""
    out: dict = defaultdict(list)
    ids = [w.pk for w in work_orders]
    if not ids:
        return out
    uses = (TestEquipmentUse.objects.filter(work_order_id__in=ids).select_related("test_equipment", "work_order")
            .order_by("work_order_id", "test_equipment__tag"))
    for j in rules.judge_uses(uses):
        out[j.use.work_order_id].append(j)
    return out


def has_uses(wo) -> bool:
    return TestEquipmentUse.objects.filter(work_order_id=wo.pk).exists()


def delete_refusal(wo) -> str:
    """The API destroy's words for a work order with test equipment recorded (PROTECT keeps it); a cancelled one keeps only the uses a
    calibration since has judged (clear_for_cancel), so its words say so (review fix)."""
    from apps.workorders.models import WoStatus

    if wo.status == WoStatus.CANCELLED:
        return DELETE_CANCELLED.format(number=wo.number)
    return DELETE_REFUSED.format(number=wo.number)


# --- the work order's side: which test equipment it recorded --------------------------------------------------------------------

@dataclass
class UsesChange:
    """What check_uses found to do: items to record (their rows locked) and uses to take off; `note` words the history's reason when
    it is the completion's own consequence (not what was sent)."""
    add: list
    remove: list
    note: str = ""


def _lock_items(ids) -> None:
    """The items' rows, locked in pk order until the transaction ends (the work order's writers, after its row)."""
    list(TestEquipment.objects.select_for_update().filter(pk__in=list(ids)).order_by("pk").values_list("pk", flat=True))


def _kept(uses) -> set:
    """The item ids of `uses` (one work order's) the kept-use rule keeps, each on its own day (rules.kept). One query."""
    return rules.kept(uses, rules.calibrations_by_item(u.test_equipment_id for u in uses)) if uses else set()


def kept_ids(wo) -> set:
    """The items on `wo` that may not be taken off (rules.may_remove, each use on its own day): the modal and Change show them ticked
    and disabled ("kept: calibrated since") and post them with the rest. Two queries (one with nothing recorded)."""
    return _kept(list(TestEquipmentUse.objects.filter(work_order=wo)))


def _and(words: list) -> str:
    """"A", "A and B", "A, B, and C"."""
    return words[0] if len(words) == 1 else f"{words[0]} and {words[1]}" if len(words) == 2 else f"{', '.join(words[:-1])}, and {words[-1]}"


def _not_offered(wo, repair_check, kept: set, *, completing: bool) -> str:
    """Why `wo` takes no other test equipment (rules.offered fails), in words naming what may be sent: with uses the kept-use rule keeps
    (`kept`, item ids), only those (review fix: never an answer refused next)."""
    from apps.workorders.models import RepairCheck, Source

    if wo.source == Source.IMPORTED:
        return IMPORTED.format(number=wo.number)
    vendor_checked = (repair_check if repair_check is not None else wo.repair_check) == RepairCheck.VENDOR_REPORT
    if kept:
        tags = _and(sorted(TestEquipment.objects.filter(pk__in=list(kept)).values_list("tag", flat=True)))
        words = VENDOR_CHECKED_KEPT if vendor_checked else VENDOR_WORK_KEPT
        return words.format(number=wo.number, tags=tags, left_out=LEFT_OUT if completing else "")
    return VENDOR_CHECKED if vendor_checked else VENDOR_WORK.format(number=wo.number)


def check_uses(wo, ids, *, by, today: date, errors: dict, completing: bool, repair_check: str | None = None):
    """What a door sent for the test equipment `wo` recorded (the module docstring), checked on the locked rows: `wo` is the work order's
    row, locked after lock_work; the named items and those already on it are locked here, in pk order. `completing`: the work is
    completed today (new items usable today); else after completion (new items not retired before its day). `repair_check`: the
    outcome a completion chooses in the same call. On work that is not offered (a completion as checked by the vendor: rules.offered
    with that outcome) only the kept uses stay: KEEP takes off the others, and a list may name only kept ones (review fix). Returns
    KEEP (nothing to change) or a UsesChange; refusals keyed test_equipment."""
    from apps.workorders.scoping import is_scoped

    if ids is KEEP:
        if completing and not rules.offered(wo, None, repair_check=repair_check):
            # Review fix: completed again as checked by the vendor (a reopened repair CE had checked), the work no longer counts and
            # its report names the vendor's test equipment: ours comes off with the completion, but never a use the kept-use rule
            # keeps (a calibration since has judged it: it stays, in no window while the work does not count, and counts again if the
            # work is completed again as checked by us)
            stale = list(TestEquipmentUse.objects.filter(work_order=wo))
            if stale:
                _lock_items(u.test_equipment_id for u in stale)
                keep = _kept(stale)
                free = [u for u in stale if u.test_equipment_id not in keep]
                if free:
                    return UsesChange(add=[], remove=free, note=VENDOR_TAKEN_OFF)
        return KEEP
    if isinstance(ids, (str, bytes)) or not isinstance(ids, (list, tuple, set, frozenset)):
        errors["test_equipment"] = LIST_ONLY
        return KEEP
    wanted: list = []
    for value in ids:
        pk = value.pk if isinstance(value, TestEquipment) else _uuid(value)
        if pk is None:
            errors["test_equipment"] = CHOOSE
            return KEEP
        if pk not in wanted:
            wanted.append(pk)
    if is_scoped(by):
        if wanted:
            errors["test_equipment"] = SCOPED
        return KEEP  # an empty list never changes the facility's record
    if by is not None and not perms.can_view(by):  # review fix: every door, the API's too, as the web's picker and Change
        errors["test_equipment"] = VIEW_NEEDED
        return KEEP
    existing = {u.test_equipment_id: u for u in TestEquipmentUse.objects.filter(work_order=wo)}
    offered = rules.offered(wo, by, repair_check=repair_check)
    if not offered and any(pk not in existing for pk in wanted):
        # never a new item; the words name the kept ones, if any (read for the words only: nothing is written)
        errors["test_equipment"] = _not_offered(wo, repair_check, _kept(list(existing.values())), completing=completing)
        return KEEP
    lock = set(wanted) | set(existing)
    rows = {i.pk: i for i in TestEquipment.objects.select_for_update().filter(pk__in=lock).order_by("pk")} if lock else {}
    if any(pk not in rows for pk in wanted):
        errors["test_equipment"] = CHOOSE
        return KEEP
    keep = set(wanted)
    add = [rows[pk] for pk in wanted if pk not in existing]
    remove = [u for pk, u in existing.items() if pk not in keep]
    kept = _kept(list(existing.values())) if remove or not offered else set()  # on the locked rows: no calibration races it
    if not offered and any(pk not in kept for pk in wanted):
        # Review fix: on work that is not offered only a kept use may be sent back; the others come off, as KEEP takes them off
        errors["test_equipment"] = _not_offered(wo, repair_check, kept, completing=completing)
        return KEEP
    problems = []
    for item in add:
        if completing:
            if not rules.usable_today(item, today):
                status = Status(item.status).label.lower()
                if item.status_reason:
                    status += f" ({Reason(item.status_reason).label.lower()})"
                problems.append(NOT_USABLE.format(tag=item.tag, status=status))
        elif wo.completed_on is not None:
            retired_on = rules.retired_throughout(item, wo.completed_on)
            if retired_on is not None:
                problems.append(RETIRED_BEFORE.format(tag=item.tag, day=fmt(retired_on)))
    # The kept-use rule, whatever the work, each use on its own day (a judged use is never erased: counted again, it is the record the
    # review read)
    for u in sorted(remove, key=lambda u: rows[u.test_equipment_id].tag):
        if u.test_equipment_id in kept:
            problems.append(KEPT.format(tag=rows[u.test_equipment_id].tag, number=wo.number, day=fmt(u.used_on)))
    if problems:
        errors["test_equipment"] = " ".join(problems)
        return KEEP
    if not add and not remove:
        return KEEP
    return UsesChange(add=add, remove=remove, note=VENDOR_TAKEN_OFF if completing and not offered else "")


def apply_uses(wo, change: UsesChange, *, by, completing: bool) -> None:
    """Write what check_uses found (inside the caller's transaction, the rows still locked): each use added or taken off is a history
    row of its own (the change log's "Test equipment used"). A use added is recorded on the day of the work, its used_on: the work
    order's completed_on (completing, the completion's day, today; after completion, the day it was completed), never moved after; the
    uses that stay keep theirs."""
    when = "with the completion" if completing else "after completion"
    for use in change.remove:
        _delete(use, by, change.note or f"Taken off {when}")
    for item in change.add:
        _save(TestEquipmentUse(tenant_id=wo.tenant_id, work_order=wo, test_equipment=item, used_on=wo.completed_on), by, f"Recorded {when}")


def clear_for_cancel(wo, *, by) -> int:
    """workorders.services.change_status's cancel (review fix): a cancelled work order records no test equipment (its drawer, print, and
    the API's field show none): each use taken off, audited "Test equipment cleared: cancelled", but never a use the kept-use rule keeps
    (it counts nowhere). Takes no item lock (review fix): a cancel runs inside transactions that hold a device's row and cancel several
    work orders (equipment's retirement, a passed incoming inspection), and a use's removal reads only the calibrations, so a
    calibration recorded meanwhile is harmless. Returns how many."""
    return _clear(wo, by=by, note="Test equipment cleared: cancelled", lock_items=False)


def clear_for_vendor(wo, vendor_name: str, *, by) -> int:
    """assign's clearing (workorders.services.assign, the work order's row locked): an open work order that moves to a vendor keeps no
    test equipment of ours (that work is now the vendor's, and its report names its own), but never a use the kept-use rule keeps (a
    reopened work order a calibration since has judged: it stays, in no window while the work does not count). Each use taken off,
    audited "Test equipment cleared: assigned to <vendor>"; their items' rows locked first, in pk order. Returns how many."""
    return _clear(wo, by=by, note=f"Test equipment cleared: assigned to {vendor_name}")


def _clear(wo, *, by, note: str, lock_items: bool = True) -> int:
    """Take off `wo`'s uses but the kept ones, each judged on its own day (the work order's row locked by the caller; the items' rows
    here, in pk order, unless `lock_items` is False)."""
    uses = list(TestEquipmentUse.objects.filter(work_order=wo))
    if not uses:
        return 0
    if lock_items:
        _lock_items(u.test_equipment_id for u in uses)
    keep = _kept(uses)
    free = [u for u in uses if u.test_equipment_id not in keep]
    for u in free:
        _delete(u, by, note)
    return len(free)


@dataclass(frozen=True)
class Option:
    """One item a door offers for a work order: its standing on the work's day (today while completing; one on the work order already,
    on its use's own day), whether it is on the work order already, and whether it is kept there (the kept-use rule: ticked and
    disabled)."""
    item: TestEquipment
    standing: rules.Standing
    on_work_order: bool = False
    kept: bool = False


@dataclass
class Picker:
    """The completion modal's and Change's "Test equipment used": `yours` (at most YOURS_MAX: the items held by the work's technician,
    then those they recorded in the last RECENT_DAYS days), `others` [(kind label, options)] by kind, `others_count`, and whether
    "Other test equipment" opens by default (`open_others`: Yours empty and at most OTHERS_OPEN_MAX others). Never pre-ticked: a door
    ticks only `on_work_order`."""
    yours: list
    others: list
    others_count: int
    open_others: bool
    technician: object = None


def picker(wo, user, *, today: date) -> Picker:
    """The options for `wo` (rules.offered must hold: the door shows nothing otherwise). Completing (an open work order): the items in use
    at some moment of today (rules.in_use_today), judged today. After completion: every item not retired before the work's day, judged on
    that day. The items already on the work order always, each judged on its use's own day (used_on), kept ones marked (rules.kept). Yours:
    the technician's kit, then the items they recorded on days (used_on) in the last RECENT_DAYS days. A fixed number of queries."""
    from apps.credentials.services import technician_of
    from apps.workorders.costs import default_technician

    completing = wo.status not in rules.DONE
    day = today if completing else wo.completed_on
    uses = list(TestEquipmentUse.objects.filter(work_order=wo))
    existing = {u.test_equipment_id: u.used_on for u in uses}
    if completing:
        offered_q = Q(status=Status.IN_USE) | Q(status_since=today)
    else:
        offered_q = Q(status__in=LIVE_STATUSES) | Q(status=Status.RETIRED, status_since__gte=day)
    items = list(TestEquipment.objects.filter(offered_q | Q(pk__in=list(existing))).order_by("tag"))
    if completing:  # review fix: a status changed today counts only when the item was in use at some moment of today
        usable = rules.in_use_today([i for i in items if i.pk not in existing], today)
        items = [i for i in items if i.pk in existing or i.pk in usable]
    cals = rules.calibrations_by_item(i.pk for i in items)
    kept = rules.kept(uses, cals)
    options = {i.pk: Option(i, rules.standing_on(i, existing.get(i.pk, day), cals.get(i.pk, ())), i.pk in existing, i.pk in kept)
               for i in items}
    tech = default_technician(wo, user) or technician_of(user)
    yours_ids: list = []
    if tech is not None:
        yours_ids = [i.pk for i in items if i.held_by_id == tech.pk]
        recent = (TestEquipmentUse.objects.filter(work_order__assigned_to=tech, used_on__gte=day - timedelta(days=RECENT_DAYS),
                                                  test_equipment_id__in=list(options))
                  .order_by("-used_on").values_list("test_equipment_id", flat=True))
        for pk in recent:
            if pk not in yours_ids:
                yours_ids.append(pk)
    yours_ids = yours_ids[:YOURS_MAX]
    rest = [options[i.pk] for i in items if i.pk not in set(yours_ids)]
    by_kind: dict = defaultdict(list)
    for o in rest:
        by_kind[o.item.kind].append(o)
    others = [(label, by_kind[value]) for value, label in Kind.choices if by_kind.get(value)]
    yours = [options[pk] for pk in yours_ids]
    return Picker(yours, others, len(rest), not yours and len(rest) <= OTHERS_OPEN_MAX, tech)
