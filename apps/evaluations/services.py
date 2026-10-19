"""
The program's annual evaluation (slice 32): every write, and the read helpers the screens, the API, the binder, and the seed share.

The rules:
- An evaluation covers 12 months: period_start the first of a month, period_end 12 months later less a day, ended before it is drafted
  (period_end before the facility's today). Its number is EV-<yy>-<nnnn> (Sequence "evaluation-<yy>", yy from the period's end); an
  amendment keeps the number with the next version.
- Periods never overlap among the live evaluations (LIVE_STATUSES: draft, sent to committee, approved) but for an approved evaluation and
  its amendment; one approved per period. Checked under the facility's evaluation lock (lock_evaluations: a Sequence row that never
  counts, as apps.pm.services.lock_planner), the partial unique constraint behind it.
- draft -> ready ("Sent to committee": the text, the objectives, and the figures frozen; Back to draft) -> approved (the first approval
  recorded, its meeting on or after the day it was sent) -> superseded (when its amendment is approved: the earlier version first, then
  the amendment, in one transaction). A draft or a ready one may be discarded (a status, never a delete); an approved one never.
- The figures (indicators.snapshot) are taken when the draft is made and on Refresh figures (a draft only), by someone who may see every
  area they cover (indicators.missing_for): the snapshot holds all of them and each reader sees their cut (indicators.cut). A refresh
  counts outside any lock, then locks the evaluation and saves only if it is still a draft with the snapshot it started from.
- The review objectives are copied from the previous approved evaluation's next-period objectives (the one ending the day before this
  period starts; none copied after a gap) with carried_from; their results and comments are written on the copies only.
- An amendment (Approve) of an approved, not superseded evaluation is a new draft: the same number, version + 1, its text, objectives,
  and figures copied (a refresh is a separate choice). One amendment at a time.

Every write: transaction.atomic, refusals as ValidationError in plain words (keyed by field where a form has the field), levels checked
here for a user given as `by` (PermissionDenied; by=None is the system: the seed, tests), history with a _change_reason. Today is the
facility's, read once (timezone.localdate()) and passed down.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import NamedTuple

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.models import Sequence
from apps.pm.dates import add_months
from apps.tenants.context import get_current_tenant

from . import indicators
from . import permissions as perms
from .models import (
    COMMENT_MAX,
    LIVE_STATUSES,
    MINUTES_VALIDATOR,
    NARRATIVE,
    NARRATIVE_FIELDS,
    OBJECTIVE_MAX,
    OBJECTIVES_MAX,
    PLAN_VERSION_VALIDATOR,
    TARGET_MAX,
    TEXT_MAX,
    Body,
    EvaluationApproval,
    EvaluationObjective,
    EvaluationSnapshot,
    ObjectiveKind,
    ObjectiveResult,
    ProgramEvaluation,
    Status,
)

LOCK = "evaluations"  # the facility's evaluation lock (a Sequence row that never counts)
EARLIEST = date(2000, 1, 1)  # a year typed with a slip (1926, 0026) is refused
CHANGE_REASON_MAX = 100  # simple_history's history_change_reason column
DUE_AFTER_DAYS = 90  # next_due: an evaluation of the next 12 months is approved within 90 days after they end
PLAN_TITLE_MAX = ProgramEvaluation._meta.get_field("plan_title").max_length
PLAN_VERSION_MAX = ProgramEvaluation._meta.get_field("plan_version").max_length
BODY_NAME_MAX = EvaluationApproval._meta.get_field("body_name").max_length
CHAIR_MAX = EvaluationApproval._meta.get_field("chair_name").max_length
MINUTES_MAX = EvaluationApproval._meta.get_field("minutes_reference").max_length
DATE_FIELDS = ("inventory_reviewed_on", "plan_effective_on")
TEXT_FIELDS = NARRATIVE_FIELDS + ("plan_title", "plan_version") + DATE_FIELDS  # update_text's fields
FIELD_LABELS = {**{field: label for field, label, _help in NARRATIVE}, "inventory_reviewed_on": "Inventory reviewed",
                "plan_title": "Plan reviewed", "plan_version": "Plan version", "plan_effective_on": "Plan effective"}
APPROVAL_FIELDS = ("body", "body_name", "meeting_on", "chair_name", "chair_title", "minutes_reference")

EDIT_PERMISSION = "Drafting and writing a program evaluation needs Evaluations Edit."
APPROVE_PERMISSION = "Recording a committee's approval, or amending an approved evaluation, needs Evaluations Approve."
FIGURES_CHANGED = "It changed while the figures were taken; refresh again."
FIGURES_RULE = "only a draft's figures are taken again."
OBJECTIVES_RULE = "only a draft's objectives are changed."


# --- small helpers --------------------------------------------------------------------------------------------------------------


def _check(by, allowed, message: str) -> None:
    """The level check every write starts with, for a user given as `by` (None is the system: the seed, tests)."""
    if by is not None and not allowed(by):
        raise PermissionDenied(message)


def _check_areas(by) -> None:
    """Taking the figures needs View on every area they cover (indicators.missing_for), so the snapshot never depends on who took it."""
    if by is None:
        return
    missing = indicators.missing_for(by)
    if missing:
        raise PermissionDenied(f"Taking the figures needs every area they cover: {indicators.needs_words(missing)}, which your role does "
                               "not have. Ask someone who has them to take the figures.")


def _save(obj, by, reason: str, **kwargs) -> None:
    obj._change_reason = reason[:CHANGE_REASON_MAX]
    if by is not None:
        obj._history_user = by
    obj.save(**kwargs)


def _day(d: date) -> str:
    return indicators.day_words(d)


def period_label(start: date, end: date) -> str:
    """"Oct 1, 2025 to Sep 30, 2026"."""
    return indicators.period_words(start, end)


def period_end_of(start: date) -> date:
    """The last day of the 12 months from `start`."""
    return add_months(start, 12) - timedelta(days=1)


def _is_day(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def lock_evaluations() -> None:
    """Hold the facility's evaluation lock until the transaction ends: drafting (the overlap check and the number), recording an
    approval, and amending take it first, so two of them at once wait here and then read what the first committed. A row of its own
    that nothing else locks (a Sequence row that never counts), as apps.pm.services.lock_planner. SQLite runs one writer at a time."""
    tenant = get_current_tenant()
    if tenant is None:
        raise RuntimeError("lock_evaluations() needs a tenant context")
    try:
        with transaction.atomic():  # a savepoint: two first-ever requests may both try to create the row
            Sequence.unscoped.get_or_create(tenant=tenant, key=LOCK)  # unscoped + explicit tenant, as Sequence.next
    except IntegrityError:
        pass
    Sequence.unscoped.select_for_update().get(tenant=tenant, key=LOCK)


def _locked(evaluation) -> ProgramEvaluation:
    """The evaluation's row as it is now, locked until the transaction ends."""
    return ProgramEvaluation.objects.select_for_update().get(pk=evaluation.pk)


def _status_words(ev: ProgramEvaluation) -> str:
    return Status(ev.status).label.lower()


def _require_draft(ev: ProgramEvaluation, rule: str) -> None:
    """Refuse unless `ev` is a draft: "<label> is sent to committee: <rule> Take it back to draft first." """
    if ev.status != Status.DRAFT:
        tail = " Take it back to draft first." if ev.status == Status.READY else ""
        raise ValidationError(f"{ev.label} is {_status_words(ev)}: {rule}{tail}")


# --- periods ----------------------------------------------------------------------------------------------------------------------


def _clean_period(period_start, today: date) -> date:
    """The period's end, or a refusal keyed by period_start."""
    if not _is_day(period_start):
        raise ValidationError({"period_start": "Choose the month the period starts."})
    if period_start.day != 1:
        raise ValidationError({"period_start": "An evaluation's period starts on the first of a month."})
    if period_start < EARLIEST:
        raise ValidationError({"period_start": f"Choose a month from {EARLIEST.year} on."})
    if period_start >= today:  # merge fix: a start after today can never have ended (and year 9999 has no end in a date)
        raise ValidationError({"period_start": "An evaluation covers 12 months that have ended. Choose an earlier month."})
    end = period_end_of(period_start)
    if end >= today:
        raise ValidationError({"period_start": f"The 12 months from {_day(period_start)} end on {_day(end)}: an evaluation covers 12 months "
                                               "that have ended. Choose an earlier month."})
    return end


def _overlapping(start: date, end: date):
    return ProgramEvaluation.objects.filter(status__in=LIVE_STATUSES, period_start__lte=end, period_end__gte=start).order_by("period_start", "version")


def _refuse_overlap(start: date, end: date) -> None:
    found = list(_overlapping(start, end))
    if not found:
        return
    approved = next((ev for ev in found if ev.status == Status.APPROVED), None)
    if approved is not None:
        same = approved.period_start == start
        raise ValidationError({"period_start": f"{approved.label} covers {period_label(approved.period_start, approved.period_end)} and is "
                                               "approved: " + ("amend it rather than start another evaluation of the same months."
                                                              if same else "periods never overlap. Choose a month after it ends, or amend it.")})
    ev = found[0]
    raise ValidationError({"period_start": f"{ev.label} ({_status_words(ev)}) covers {period_label(ev.period_start, ev.period_end)}, which "
                                           "overlaps these 12 months: periods never overlap. Discard it, or choose another month."})


def _number(period_end: date) -> str:
    yy = period_end.year % 100
    return f"EV-{yy:02d}-{Sequence.next(f'evaluation-{yy:02d}'):04d}"


# --- the figures ------------------------------------------------------------------------------------------------------------------


def _take(ev: ProgramEvaluation, data: dict, by, today: date, reason: str) -> EvaluationSnapshot:
    """A new snapshot row for `ev` (locked by the caller) and `ev` pointing at it."""
    snap = EvaluationSnapshot.objects.create(evaluation=ev, taken_at=timezone.now(), taken_on=today, taken_by=by, data=data)
    ev.snapshot = snap
    _save(ev, by, reason)
    return snap


def refresh(evaluation, *, by, today: date | None = None) -> ProgramEvaluation:
    """Refresh figures (Edit, and View on every area the figures cover): a draft's figures counted again. Counted outside any lock (it
    is slow: the whole binder), then the evaluation is locked and the new snapshot saved only if it is still a draft with the snapshot
    the count started from; else refused (refresh again)."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    _check_areas(by)
    today = today or timezone.localdate()
    ev = ProgramEvaluation.objects.get(pk=evaluation.pk)
    _require_draft(ev, FIGURES_RULE)
    started = ev.snapshot_id
    data = indicators.snapshot(ev.period_start, ev.period_end, today=today, previous=previous_approved(ev))
    with transaction.atomic():
        current = _locked(ev)
        _require_draft(current, FIGURES_RULE)
        if current.snapshot_id != started:
            raise ValidationError(FIGURES_CHANGED)
        _take(current, data, by, today, "Figures taken again")
    return current


# --- drafting ---------------------------------------------------------------------------------------------------------------------


def _carry_source(period_start: date) -> ProgramEvaluation | None:
    """The approved evaluation whose period ends the day before `period_start`: its next-period objectives become this one's review
    objectives. None after a gap (or a change of cycle): nothing is copied."""
    return ProgramEvaluation.objects.filter(status=Status.APPROVED, period_end=period_start - timedelta(days=1)).first()


def create_draft(*, period_start: date, by, today: date | None = None) -> ProgramEvaluation:
    """New evaluation (Edit, and View on every area the figures cover): a draft of the 12 months from `period_start`, numbered, with this
    period's objectives carried from the previous approved evaluation and its first figures taken."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    _check_areas(by)
    today = today or timezone.localdate()
    period_end = _clean_period(period_start, today)
    _refuse_overlap(period_start, period_end)  # before the slow count; again under the lock
    data = indicators.snapshot(period_start, period_end, today=today, previous=_previous_before(period_start))
    with transaction.atomic():
        lock_evaluations()
        _refuse_overlap(period_start, period_end)
        ev = ProgramEvaluation(number=_number(period_end), version=1, period_start=period_start, period_end=period_end, created_by=by)
        _save(ev, by, "Drafted")
        source = _carry_source(period_start)
        if source is not None:
            for obj in source.objectives.filter(kind=ObjectiveKind.NEXT).order_by("position", "created_at"):
                copy = EvaluationObjective(evaluation=ev, kind=ObjectiveKind.REVIEW, position=obj.position, text=obj.text, target=obj.target,
                                           carried_from=obj)
                _save(copy, by, f"Carried from {source.label}")
        _take(ev, data, by, today, "Figures taken")
    return ev


# --- the text ---------------------------------------------------------------------------------------------------------------------


def _clean_text(field: str, value, today_fn):
    if field not in TEXT_FIELDS:
        raise ValidationError(f"{field} is not part of an evaluation's text.")
    if field in DATE_FIELDS:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value.strip())
            except ValueError:
                raise ValidationError({field: "Enter a date as YYYY-MM-DD."}) from None
        if not _is_day(value):
            raise ValidationError({field: "Enter a date."})
        if value < EARLIEST:
            raise ValidationError({field: f"Enter a date from {EARLIEST.year} on."})
        if field == "inventory_reviewed_on" and value > today_fn():
            raise ValidationError({field: "The inventory review cannot be dated after today."})
        return value
    text = (value or "").strip() if isinstance(value, str) or value is None else None
    if text is None:
        raise ValidationError({field: "Enter text."})
    limit = TEXT_MAX if field in NARRATIVE_FIELDS else PLAN_TITLE_MAX if field == "plan_title" else PLAN_VERSION_MAX
    if len(text) > limit:
        raise ValidationError({field: f"At most {limit:,} characters ({len(text):,} entered)."})
    if field == "plan_version" and text:
        try:
            PLAN_VERSION_VALIDATOR(text)
        except ValidationError as e:
            raise ValidationError({field: e.messages[0]}) from None
    return text


def update_text(evaluation, field: str, value, *, by, today: date | None = None) -> ProgramEvaluation:
    """Save one field of a draft's text (Edit): a NARRATIVE field (at most TEXT_MAX characters, stripped), the inventory review day (not
    after today), the plan reviewed (its title, its version as a token, its effective day). Only that field is written, so two people
    writing different fields never overwrite each other."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    cleaned = _clean_text(field, value, lambda: today or timezone.localdate())
    with transaction.atomic():
        ev = _locked(evaluation)
        _require_draft(ev, "only a draft's text is written.")
        if getattr(ev, field) != cleaned:
            setattr(ev, field, cleaned)
            _save(ev, by, f"{FIELD_LABELS[field]} changed", update_fields=[field, "updated_at"])
    return ev


# --- objectives -------------------------------------------------------------------------------------------------------------------


def _clean_objective_text(text, *, required: bool) -> str:
    text = (text or "").strip()
    if required and not text:
        raise ValidationError({"text": "Enter the objective."})
    if len(text) > OBJECTIVE_MAX:
        raise ValidationError({"text": f"At most {OBJECTIVE_MAX} characters ({len(text)} entered)."})
    return text


def _clean_short(field: str, value, limit: int) -> str:
    value = (value or "").strip()
    if len(value) > limit:
        raise ValidationError({field: f"At most {limit} characters ({len(value)} entered)."})
    return value


def _locked_objective(objective) -> tuple[ProgramEvaluation, EvaluationObjective]:
    """The objective's evaluation and the objective, locked in that order, the evaluation a draft."""
    ev = ProgramEvaluation.objects.select_for_update().get(pk=objective.evaluation_id)
    _require_draft(ev, OBJECTIVES_RULE)
    return ev, EvaluationObjective.objects.select_for_update().get(pk=objective.pk)


def add_objective(evaluation, *, kind: str, text: str, target: str = "", by) -> EvaluationObjective:
    """Add an objective to a draft (Edit): this period's (reviewed) or the next period's, at most OBJECTIVES_MAX of each."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    if kind not in ObjectiveKind.values:
        raise ValidationError({"kind": "Choose this period's objective or the next period's."})
    text = _clean_objective_text(text, required=True)
    target = _clean_short("target", target, TARGET_MAX)
    with transaction.atomic():
        ev = _locked(evaluation)
        _require_draft(ev, OBJECTIVES_RULE)
        mine = EvaluationObjective.objects.filter(evaluation=ev, kind=kind)
        if mine.count() >= OBJECTIVES_MAX:
            raise ValidationError(f"An evaluation has at most {OBJECTIVES_MAX} of {ObjectiveKind(kind).label.lower()}s.")
        position = (mine.aggregate(p=Max("position"))["p"] or 0) + 1
        obj = EvaluationObjective(evaluation=ev, kind=kind, position=position, text=text, target=target)
        _save(obj, by, "Objective added")
    return obj


def update_objective(objective, *, text=None, target=None, result=None, comment=None, by) -> EvaluationObjective:
    """Change a draft's objective (Edit): its text and target; this period's result (ObjectiveResult, or "" to clear it) and comment,
    which a next period's objective has none of. A value left as None is kept."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    with transaction.atomic():
        _ev, obj = _locked_objective(objective)
        if obj.kind != ObjectiveKind.REVIEW and (result or comment):
            field = "result" if result else "comment"
            raise ValidationError({field: "A next period's objective has no result yet: it is reviewed in the next evaluation."})
        if text is not None:
            obj.text = _clean_objective_text(text, required=True)
        if target is not None:
            obj.target = _clean_short("target", target, TARGET_MAX)
        if result is not None:
            if result and result not in ObjectiveResult.values:
                raise ValidationError({"result": "Choose met, partly met, not met, or carried forward."})
            obj.result = result
        if comment is not None:
            obj.comment = _clean_short("comment", comment, COMMENT_MAX)
        _save(obj, by, "Objective changed")
    return obj


def remove_objective(objective, *, by) -> None:
    """Remove a draft's objective (Edit), one carried from the previous evaluation too (its history keeps it)."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    with transaction.atomic():
        _ev, obj = _locked_objective(objective)
        obj._change_reason = "Objective removed"
        if by is not None:
            obj._history_user = by
        obj.delete()


def move_objective(objective, *, up: bool, by) -> EvaluationObjective:
    """Move a draft's objective one place up or down among its kind (Edit); at the end already, nothing changes."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    with transaction.atomic():
        ev, obj = _locked_objective(objective)
        order = list(EvaluationObjective.objects.select_for_update().filter(evaluation=ev, kind=obj.kind).order_by("position", "created_at"))
        i = next(n for n, o in enumerate(order) if o.pk == obj.pk)
        j = i - 1 if up else i + 1
        if j < 0 or j >= len(order):
            return obj
        order[i], order[j] = order[j], order[i]
        for n, o in enumerate(order, start=1):
            if o.position != n:
                o.position = n
                _save(o, by, "Objective moved")
        obj.refresh_from_db()
    return obj


# --- statuses ---------------------------------------------------------------------------------------------------------------------


def send_to_committee(evaluation, *, by, today: date | None = None) -> ProgramEvaluation:
    """Sent to committee (Edit): a draft with figures taken and a result for every one of this period's objectives becomes ready; the
    text, objectives, and figures are frozen from here (Back to draft unfreezes them)."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    today = today or timezone.localdate()
    with transaction.atomic():
        ev = _locked(evaluation)
        _require_draft(ev, "only a draft is sent to committee.")
        if ev.snapshot_id is None:
            raise ValidationError("Take the figures before sending it to committee.")
        open_ = EvaluationObjective.objects.filter(evaluation=ev, kind=ObjectiveKind.REVIEW, result="").count()
        if open_:
            raise ValidationError(f"Record a result for every objective of this period before sending it to committee: {open_} "
                                  f"{'has' if open_ == 1 else 'have'} none.")
        ev.status, ev.sent_on = Status.READY, today
        _save(ev, by, "Sent to committee")
    return ev


def back_to_draft(evaluation, *, by) -> ProgramEvaluation:
    """Back to draft (Edit): a ready evaluation is a draft again (its sent day kept until it is sent again)."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    with transaction.atomic():
        ev = _locked(evaluation)
        if ev.status != Status.READY:
            raise ValidationError(f"{ev.label} is {_status_words(ev)}: only one sent to committee goes back to draft.")
        ev.status = Status.DRAFT
        _save(ev, by, "Back to draft")
    return ev


def discard(evaluation, *, by) -> ProgramEvaluation:
    """Discard (Edit): a draft, or one sent to committee, is discarded (kept, never deleted); its period is free again. An approved
    evaluation is never discarded: it is amended."""
    _check(by, perms.can_edit, EDIT_PERMISSION)
    with transaction.atomic():
        ev = _locked(evaluation)
        if ev.status in (Status.APPROVED, Status.SUPERSEDED):
            raise ValidationError(f"{ev.label} is {_status_words(ev)}: an approved evaluation is never discarded. Amend it instead.")
        if ev.status == Status.DISCARDED:
            raise ValidationError(f"{ev.label} is discarded already.")
        ev.status = Status.DISCARDED
        _save(ev, by, "Discarded")
    return ev


# --- approvals --------------------------------------------------------------------------------------------------------------------


def _clean_approval(values: dict, today: date) -> dict:
    """The approval's fields checked, keyed by field: the body (another committee needs its name), the meeting day (not after today),
    the chair's name and title, the minutes' reference (a token)."""
    body = values.get("body")
    if body not in Body.values:
        raise ValidationError({"body": "Choose the committee that approved it."})
    body_name = _clean_short("body_name", values.get("body_name"), BODY_NAME_MAX) if body == Body.OTHER else ""
    if body == Body.OTHER and not body_name:
        raise ValidationError({"body_name": "Name the committee."})
    meeting_on = values.get("meeting_on")
    if not _is_day(meeting_on):
        raise ValidationError({"meeting_on": "Enter the day the committee met."})
    if meeting_on > today:
        raise ValidationError({"meeting_on": "The meeting cannot be after today: record the approval once the committee has met."})
    if meeting_on < EARLIEST:
        raise ValidationError({"meeting_on": f"Enter a day from {EARLIEST.year} on."})
    minutes = _clean_short("minutes_reference", values.get("minutes_reference"), MINUTES_MAX)
    if minutes:
        try:
            MINUTES_VALIDATOR(minutes)
        except ValidationError as e:
            raise ValidationError({"minutes_reference": e.messages[0]}) from None
    return {"body": body, "body_name": body_name, "meeting_on": meeting_on,
            "chair_name": _clean_short("chair_name", values.get("chair_name"), CHAIR_MAX),
            "chair_title": _clean_short("chair_title", values.get("chair_title"), CHAIR_MAX), "minutes_reference": minutes}


def _first_approval(ev: ProgramEvaluation) -> EvaluationApproval | None:
    """The approval that approved `ev`: its first recorded."""
    return EvaluationApproval.objects.filter(evaluation=ev).order_by("created_at", "id").first()


def record_approval(evaluation, *, body: str, body_name: str = "", meeting_on: date, chair_name: str = "", chair_title: str = "",
                    minutes_reference: str = "", by, today: date | None = None) -> EvaluationApproval:
    """Record a committee's approval as its minutes record it (Approve). The first, on an evaluation sent to committee, approves it:
    its meeting on or after the day it was sent, approved_on its meeting day; an amendment's first supersedes the version it amends,
    then approves this one, in one transaction under the facility's lock. Later ones (where it went next) on an approved evaluation,
    dated on or after its approval."""
    _check(by, perms.can_approve, APPROVE_PERMISSION)
    today = today or timezone.localdate()
    values = _clean_approval({"body": body, "body_name": body_name, "meeting_on": meeting_on, "chair_name": chair_name,
                              "chair_title": chair_title, "minutes_reference": minutes_reference}, today)
    with transaction.atomic():
        lock_evaluations()
        ev = _locked(evaluation)
        if ev.status == Status.READY:
            if values["meeting_on"] < ev.sent_on:
                raise ValidationError({"meeting_on": f"{ev.label} was sent to committee on {_day(ev.sent_on)}: the meeting that approved it "
                                                     "is on or after that day."})
            if ev.amends_id:
                earlier = _locked(ev.amends)
                if earlier.status != Status.APPROVED:
                    raise ValidationError(f"{earlier.label} is {_status_words(earlier)}, so this amendment cannot replace it.")
                earlier.status = Status.SUPERSEDED
                _save(earlier, by, f"Superseded by {ev.label}")
            ev.status, ev.approved_on = Status.APPROVED, values["meeting_on"]
            try:
                with transaction.atomic():
                    _save(ev, by, "Approved")
            except IntegrityError:
                raise ValidationError("Another evaluation of this period is approved already.") from None
            reason = "Approved"
        elif ev.status == Status.APPROVED:
            if values["meeting_on"] < ev.approved_on:
                raise ValidationError({"meeting_on": f"{ev.label} was approved on {_day(ev.approved_on)}: a later committee's meeting is on "
                                                     "or after that day."})
            reason = "Approval recorded"
        elif ev.status == Status.SUPERSEDED:
            raise ValidationError(f"{ev.label} was superseded by an amendment: record the approval on the amendment.")
        else:
            raise ValidationError(f"{ev.label} is {_status_words(ev)}: send it to committee before recording its approval.")
        approval = EvaluationApproval(evaluation=ev, recorded_by=by, **values)
        _save(approval, by, reason)
    return approval


def correct_approval(approval, *, by, today: date | None = None, **fields) -> EvaluationApproval:
    """Correct an approval's details (Approve), with the checks of recording it: the first's meeting stays on or after the day it was
    sent and on or before every later approval's, and moves the evaluation's approved day with it; a later one's stays on or after the
    approval. Its history keeps what it said before."""
    _check(by, perms.can_approve, APPROVE_PERMISSION)
    unknown = set(fields) - set(APPROVAL_FIELDS)
    if unknown:
        raise ValidationError(f"An approval has no {', '.join(sorted(unknown))}.")
    today = today or timezone.localdate()
    with transaction.atomic():
        lock_evaluations()
        current = EvaluationApproval.objects.select_for_update().get(pk=approval.pk)
        ev = _locked(current.evaluation)
        _refuse_superseded(ev)
        values = _clean_approval({**{f: getattr(current, f) for f in APPROVAL_FIELDS}, **fields}, today)
        first = _first_approval(ev)
        if first is not None and first.pk == current.pk:
            if ev.sent_on and values["meeting_on"] < ev.sent_on:
                raise ValidationError({"meeting_on": f"{ev.label} was sent to committee on {_day(ev.sent_on)}: the meeting that approved it "
                                                     "is on or after that day."})
            later = (EvaluationApproval.objects.filter(evaluation=ev).exclude(pk=current.pk).order_by("meeting_on")
                     .values_list("meeting_on", flat=True).first())
            if later is not None and values["meeting_on"] > later:
                raise ValidationError({"meeting_on": f"A later approval is dated {_day(later)}: the meeting that approved it is on or "
                                                     "before that day."})
            if ev.approved_on != values["meeting_on"]:
                ev.approved_on = values["meeting_on"]
                _save(ev, by, "Approval's meeting corrected")
        elif ev.approved_on and values["meeting_on"] < ev.approved_on:
            raise ValidationError({"meeting_on": f"{ev.label} was approved on {_day(ev.approved_on)}: a later committee's meeting is on or "
                                                 "after that day."})
        for field, value in values.items():
            setattr(current, field, value)
        _save(current, by, "Approval corrected")
    return current


def _refuse_superseded(ev) -> None:
    """Review fix: a superseded version's approvals are its record as it stood; the amendment that replaced it is the version of record
    (a correction there could date its approval after its own replacement's)."""
    if ev.status == Status.SUPERSEDED:
        raise ValidationError(f"{ev.label} was superseded by its amendment: its approvals stay as recorded. Correct the amendment's instead.")


def remove_approval(approval, *, by) -> None:
    """Remove a later approval entered in error (Approve); never the one that approved the evaluation (correct it, or amend). Its
    history keeps it."""
    _check(by, perms.can_approve, APPROVE_PERMISSION)
    with transaction.atomic():
        lock_evaluations()
        current = EvaluationApproval.objects.select_for_update().get(pk=approval.pk)
        ev = _locked(current.evaluation)
        _refuse_superseded(ev)
        first = _first_approval(ev)
        if first is not None and first.pk == current.pk:
            raise ValidationError("This approval approved the evaluation, so it is never removed: correct its details, or amend the "
                                  "evaluation.")
        current._change_reason = "Removed: entered in error"
        if by is not None:
            current._history_user = by
        current.delete()


def amend(evaluation, *, by, today: date | None = None) -> ProgramEvaluation:
    """Amend (Approve) an approved, not superseded evaluation: a new draft with the same number and the next version, amending it, with
    its text, its objectives (carried_from kept), and its figures (the same snapshot data in a new row; a refresh is a separate choice).
    The approved version stays the version of record until the amendment is approved. One amendment at a time."""
    _check(by, perms.can_approve, APPROVE_PERMISSION)
    with transaction.atomic():
        lock_evaluations()
        ev = _locked(evaluation)
        if ev.status != Status.APPROVED:
            raise ValidationError(f"{ev.label} is {_status_words(ev)}: only an approved evaluation is amended.")
        live = ProgramEvaluation.objects.filter(amends=ev, status__in=(Status.DRAFT, Status.READY)).order_by("version").first()
        if live is not None:
            raise ValidationError(f"{live.label} already amends {ev.label} ({_status_words(live)}): finish or discard it first.")
        version = (ProgramEvaluation.objects.filter(number=ev.number).aggregate(v=Max("version"))["v"] or ev.version) + 1
        new = ProgramEvaluation(number=ev.number, version=version, amends=ev, period_start=ev.period_start, period_end=ev.period_end,
                                created_by=by, **{f: getattr(ev, f) for f in TEXT_FIELDS})
        _save(new, by, f"Amendment of {ev.label}")
        for obj in ev.objectives.order_by("kind", "position", "created_at"):
            copy = EvaluationObjective(evaluation=new, kind=obj.kind, position=obj.position, text=obj.text, target=obj.target,
                                       result=obj.result, comment=obj.comment, carried_from=obj.carried_from)
            _save(copy, by, f"Copied from {ev.label}")
        if ev.snapshot_id:
            old = ev.snapshot
            snap = EvaluationSnapshot.objects.create(evaluation=new, taken_at=old.taken_at, taken_on=old.taken_on, taken_by=old.taken_by,
                                                     data=old.data)
            new.snapshot = snap
            _save(new, by, f"Figures copied from {ev.label}")
    return new


# --- reading ----------------------------------------------------------------------------------------------------------------------


def _previous_before(period_start: date) -> ProgramEvaluation | None:
    return (ProgramEvaluation.objects.filter(status=Status.APPROVED, period_end__lt=period_start).select_related("snapshot")
            .order_by("-period_end", "-version").first())


def previous_approved(evaluation) -> ProgramEvaluation | None:
    """The latest approved evaluation ending before this one starts: its figures head the previous column."""
    return _previous_before(evaluation.period_start)


def current(number: str) -> ProgramEvaluation | None:
    """The version of `number` to show: the approved one (the version of record, even while an amendment is drafted), else the latest
    live one (a draft or one sent to committee), else the latest (discarded or superseded)."""
    versions = list(ProgramEvaluation.objects.filter(number=number).order_by("-version"))
    approved = next((v for v in versions if v.status == Status.APPROVED), None)
    if approved is not None:
        return approved
    live = next((v for v in versions if v.status in LIVE_STATUSES), None)
    return live or (versions[0] if versions else None)


class NextDue(NamedTuple):
    due_on: date  # the last approved period's end + 12 months + DUE_AFTER_DAYS
    overdue: bool  # today is after it
    after: ProgramEvaluation  # the last approved evaluation


def next_due(today: date | None = None) -> NextDue | None:
    """When the evaluation of the 12 months after the last approved one is due approved (its end + 12 months + 90 days), and whether
    that day has passed; None before any is approved."""
    last = ProgramEvaluation.objects.filter(status=Status.APPROVED).order_by("-period_end", "-version").first()
    if last is None:
        return None
    today = today or timezone.localdate()
    # merge fix: the next 12 months' own end (12 months from the day after, less a day), never 12 months from this end: from Feb 28 that
    # would land on Feb 28 when the next period ends on Feb 29
    due = period_end_of(last.period_end + timedelta(days=1)) + timedelta(days=DUE_AFTER_DAYS)
    return NextDue(due, today > due, last)


def suggested_start(today: date | None = None) -> date:
    """The month a new evaluation would start: the day after the latest live evaluation's period ends, else the 12 months ending at the
    end of last month (merge fix: the screen's default; a new facility's last calendar year is mostly before Cadence)."""
    today = today or timezone.localdate()
    last = ProgramEvaluation.objects.filter(status__in=LIVE_STATUSES).order_by("-period_end").first()
    if last is not None:
        return last.period_end + timedelta(days=1)
    return add_months(today.replace(day=1), -12)


def records_begin() -> date | None:
    """The day Cadence's records begin: the earliest work order opened, imports included."""
    return indicators.records_begin()
