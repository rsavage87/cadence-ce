"""
Every write of test equipment (slice 35), and the read helpers the screens, the binder, and the API share. The rule is
apps.testequipment.rules; the levels are apps.testequipment.permissions (checked here for a user given as `by`: PermissionDenied in
words; by=None is the system: the seed, tests). Every write is transaction.atomic, refuses in plain words (ValidationError keyed by the
field a form has), and writes history with a _change_reason. Today is the facility's, read once and passed down.

Locks: every writer here locks only its item's row (select_for_update) and takes nothing after it; adding an item takes
equipment.services.lock_tags first (the facility's tags are shared with the devices). The work order's writers
(completion.complete_work_order, workorders.services.set_test_equipment) take lock_work, then the work order row, then the named items'
rows and those already on the work order in pk order, then what the completion already takes: no cycle with the writers here.

- add_test_equipment(*, tag, kind, manufacturer, model, serial, program, interval_months, held_by=None, calibration=None, by, today):
  Edit; the tag as a device's (TAG_VALIDATOR, at most 40, not reserved), unique in any letter case among devices and test equipment
  under lock_tags ("TE-01 is a device's tag"); a second live item with the same manufacturer, model, and serial in any letter case
  refused; `calibration`: its latest certificate in the same call (a dict of record_calibration's fields), so an item going live is not
  born a GAP.
- update_test_equipment(item, *, by, **fields): Edit; the tag only until its first calibration or use; the interval at Decide once a
  certificate is on file; held_by; never status (set_status) or tag_key.
- set_status(item, *, to, reason="", by, today): the moves of build35.md (Edit: out for calibration, back from calibration, out of
  service damaged or failed a check, missing, found, returned to its owner; Equipment Approve: retired worn out or lost, reinstated;
  out of service -> in use only by a passed calibration dated on or after status_since, never by hand; failed calibration only by
  record_calibration); status_since is today.
- record_calibration(item, *, calibrated_on, due_on=None, provider, certificate, accreditation, as_found, adjusted, result, by, today):
  Edit; calibrated_on not after today; one live record per day; due_on null exactly when failed, else default
  add_months(calibrated_on, interval_months), typed after calibrated_on and not after the last day of the month
  add_months(calibrated_on, interval) falls in; refused: failed with as found in tolerance; out of tolerance, not adjusted, and passed.
  Status: only when it is the newest live record: passed puts an item out for calibration (or out of service on or after its
  status_since) in use; failed puts an item in use or out for calibration out of service (failed calibration). A record typed late
  never moves a missing or retired item.
- mark_entered_in_error(calibration, *, by, today): Decide; the standing follows from the rest; status does not revert (said); a review
  of it stops counting.
- decide_review(calibration, *, basis, quality_reference="", decided_on, version, by, today): Decide; a calibration that needs a review
  and has none; decided on or after its day, not after today; `version` the look-back list the reviewer was shown (review_version),
  refused if it changed (REVIEW_CHANGED); stores the window and the work orders, frozen.
- Read helpers: affected_work(calibration) (the counted uses' work orders in its window, by completed day), review_version(work orders),
  recheck_progress(calibration) (per affected device: rechecked on <day> (work order) when a PM, inspection, safety check, or checked
  repair on it completed after the calibration's day records test equipment IN_DATE on its day; else not yet), list_with_standing(qs,
  today) (each item with its standing, for the list, CSV, and API in a fixed number of queries).
"""
from __future__ import annotations

REVIEW_CHANGED = "The work done with it changed meanwhile. Read the list again, then decide."


def add_test_equipment(*, tag, kind, manufacturer, model, serial, program, interval_months, held_by=None, calibration=None, by, today=None):
    raise NotImplementedError


def update_test_equipment(item, *, by, today=None, **fields):
    raise NotImplementedError


def set_status(item, *, to, reason="", by, today=None):
    raise NotImplementedError


def record_calibration(item, *, calibrated_on, due_on=None, provider, certificate, accreditation, as_found, adjusted, result, by, today=None):
    raise NotImplementedError


def mark_entered_in_error(calibration, *, by, today=None):
    raise NotImplementedError


def decide_review(calibration, *, basis, quality_reference="", decided_on, version, by, today=None):
    raise NotImplementedError
