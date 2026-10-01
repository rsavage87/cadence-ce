"""
The AEM program (slice 14): alternative equipment maintenance intervals for device models, proposed with the model's failure
history and approved by the Equipment Management Committee. The only code that changes AemDecision rows and, through them,
DeviceModel.aem_interval_months (equipment.services.update_device_model refuses a direct change).

A case goes proposed -> approved (in force) -> ended, or proposed -> rejected or withdrawn. The rules:

- Life support never goes on AEM (the facility's policy; DeviceModel.pm_interval_months ignores an interval on file anyway).
  High-risk models stay eligible: the committee decides on their history.
- The policy (Settings, policy_aem) asks for a three-year failure history: a proposal needs a device of the model installed at
  least AEM_HISTORY_YEARS ago, and approval checks again. evidence() is that history, from the facility's own records only; the
  proposal keeps a snapshot of it, so the committee sees what the proposer saw next to today's figures.
- One open proposal and one interval in force per model (partial unique constraints back this up). Every change locks the
  model's row first, so two approvals of the same model cannot both win.
- Separation of duties: the committee signs off on someone else's case, so whoever proposed cannot approve or reject it.
- A longer interval moves no device's next PM: it applies from each device's next PM, as update_device_model documents for any
  interval change. A shorter one, ending an AEM, or a model becoming life support pulls in every device whose next PM is now
  later than its last PM (or install date, or today) plus the interval it is back on, through equipment.services.update_asset so
  the open PM work order moves with it.
- An interval on file with no approved decision (set before this slice through the admin or the API) is "on file without a
  recorded approval": it can be ended the same way (end() taking the model), and an approved proposal replaces it.

Permissions are the views' business (apps/pm/permissions.py: propose at PM Edit; approve, reject, and end at PM Approve;
withdraw by the proposer or at PM Approve).
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass

from .dates import add_months
from .models import AemDecision, AemStatus

AEM_HISTORY_YEARS = 3  # the policy's "3-year failure history"
INTERVAL_MAX = 120  # months, as the OEM interval
RATIONALE_MAX = 2000
NOTE_MAX = 1000
REASON_MAX = AemDecision._meta.get_field("end_reason").max_length
CHANGE_REASON_MAX = 100  # simple_history's history_change_reason column

LIFE_SUPPORT_REFUSAL = "Life-support devices are excluded from AEM by policy: they always follow the OEM interval."


def _months(n: int) -> str:
    return f"{n} month{'' if n == 1 else 's'}"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def history_start(today: date) -> date:
    return add_months(today, -12 * AEM_HISTORY_YEARS)


# --- the evidence ---------------------------------------------------------------------------------------------------------


def _local_day(moment) -> date:
    return timezone.localdate(moment) if timezone.is_aware(moment) else moment.date()


def _retired_on(asset_ids) -> dict:
    """{asset id: the day it last went into retirement}, from the devices' own history. One query."""
    out, previous = {}, {}
    rows = Asset.history.filter(id__in=asset_ids).order_by("id", "history_date", "history_id").values_list("id", "status", "history_date")
    for pk, status, when in rows:
        if status == AssetStatus.RETIRED and previous.get(pk) != AssetStatus.RETIRED:
            out[pk] = _local_day(when)
        previous[pk] = status
    return out


def evidence(dm: DeviceModel, today: date | None = None) -> dict:
    """The model's failure history over the last AEM_HISTORY_YEARS, from the facility's own records (JSON-ready: ISO dates and
    plain numbers). Every device of the model counts for the time it was in use inside the window: from its install date (or the
    day it was added here, when none is on file) to today, or to the day it was retired. Corrective repairs and recall work
    orders count by the day they were opened (cancelled ones were never done); PMs by the day they were completed, on time when
    completed by their due date. enough_history: a device of the model was installed at least AEM_HISTORY_YEARS ago; a model
    whose devices carry no install date has no history on record."""
    from apps.recalls.models import AlertMatch
    from apps.recalls.services import DONE_STATUSES
    from apps.workorders.models import WorkOrder, WoStatus, WoType

    today = today or date.today()
    since = history_start(today)
    devices = list(Asset.objects.filter(device_model=dm).values("id", "status", "installed_on", "created_at", "updated_at"))
    retired = _retired_on([d["id"] for d in devices if d["status"] == AssetStatus.RETIRED])
    active = retired_counted = undated = 0
    device_days = 0
    oldest = None
    for d in devices:
        if d["installed_on"] and d["installed_on"] > today:
            continue  # installed after the day this evidence is for
        end = today
        if d["status"] == AssetStatus.RETIRED:
            end = retired.get(d["id"]) or _local_day(d["updated_at"])  # no history row (bulk import): its last change
            if end < since:
                continue  # out of use before the window opened
        if d["status"] == AssetStatus.RETIRED and end <= today:
            retired_counted += 1
        else:
            active += 1  # in use on that day (one retired since then was still in use)
        start = d["installed_on"] or min(_local_day(d["created_at"]), today)
        if not d["installed_on"]:
            undated += 1
        elif oldest is None or d["installed_on"] < oldest:
            oldest = d["installed_on"]
        device_days += max(0, (min(end, today) - max(start, since)).days)
    device_years = round(device_days / 365.25, 1)

    in_window = {"opened_on__gte": since, "opened_on__lte": today}
    done_in_window = Q(type=WoType.PM, completed_on__gte=since, completed_on__lte=today)
    counts = WorkOrder.objects.filter(asset__device_model=dm).aggregate(
        repairs=Count("id", filter=Q(type=WoType.REPAIR, **in_window) & ~Q(status=WoStatus.CANCELLED)),
        pm_completed=Count("id", filter=done_in_window),
        pm_on_time=Count("id", filter=done_in_window & Q(completed_on__lte=F("due_on"))),
        recall_work_orders=Count("id", filter=Q(type=WoType.RECALL, **in_window) & ~Q(status=WoStatus.CANCELLED)),
    )
    open_recalls = AlertMatch.objects.filter(device_model=dm).exclude(status__in=DONE_STATUSES).count()
    return {
        "as_of": today.isoformat(),
        "since": since.isoformat(),
        "years": AEM_HISTORY_YEARS,
        "devices_active": active,
        "devices_retired": retired_counted,
        "devices_undated": undated,
        "device_years": device_years,
        "repairs": counts["repairs"],
        "repairs_per_device_year": round(counts["repairs"] / device_years, 2) if device_years else None,
        "pm_completed": counts["pm_completed"],
        "pm_on_time": counts["pm_on_time"],
        "pm_on_time_pct": round(counts["pm_on_time"] * 100 / counts["pm_completed"]) if counts["pm_completed"] else None,
        "recall_work_orders": counts["recall_work_orders"],
        "open_recalls": open_recalls,
        "oldest_install": oldest.isoformat() if oldest else None,
        "history_years": round((today - oldest).days / 365.25, 1) if oldest else None,
        "enough_history": bool(oldest and oldest <= since),
    }


def history_refusal(ev: dict) -> str:
    """Why the policy's failure history is not there yet, with the facility's own policy text."""
    from apps.facility.services import get_settings

    policy = (get_settings().policy_aem or "").strip().rstrip(".")
    lead = f"The facility's AEM policy: {policy}." if policy else f"AEM needs a {AEM_HISTORY_YEARS}-year failure history."
    if ev.get("oldest_install"):
        oldest = date.fromisoformat(ev["oldest_install"])
        return (f"{lead} The oldest device of this model on record was installed {_day(oldest)} ({ev['history_years']} years of history); "
                f"a proposal needs {AEM_HISTORY_YEARS} years.")
    if ev.get("devices_active") or ev.get("devices_retired"):
        return f"{lead} None of this model's devices has an install date on file, so it has no failure history on record."
    return f"{lead} This model has no devices on record, so it has no failure history."


# --- reading the state ----------------------------------------------------------------------------------------------------


def in_force(dm: DeviceModel) -> AemDecision | None:
    """The approved decision the model's AEM interval comes from, or None."""
    return AemDecision.objects.filter(device_model=dm, status=AemStatus.APPROVED).first()


def open_proposal(dm: DeviceModel) -> AemDecision | None:
    return AemDecision.objects.filter(device_model=dm, status=AemStatus.PROPOSED).first()


def is_legacy(dm: DeviceModel, approved: AemDecision | None) -> bool:
    """An AEM interval on file without a recorded approval (set before this slice through the admin or the API)."""
    return dm.aem_interval_months is not None and approved is None


def propose_blocker(dm: DeviceModel, today: date | None = None, *, ev: dict | None = None, open_: AemDecision | None = None) -> str:
    """Why a proposal for this model would be refused before anything is typed ('' when it can be made). `ev` and `open_` save
    the queries when the caller has them already."""
    if dm.risk_class == RiskClass.LIFE_SUPPORT:
        return LIFE_SUPPORT_REFUSAL
    open_ = open_ if open_ is not None else open_proposal(dm)
    if open_ is not None:
        return _open_refusal(open_)
    ev = ev if ev is not None else evidence(dm, today)
    return "" if ev["enough_history"] else history_refusal(ev)


def _open_refusal(open_: AemDecision) -> str:
    return (f"An AEM proposal for this model is open ({_months(open_.interval_months)}, proposed {_day(open_.proposed_on)}). "
            "It is approved, rejected, or withdrawn before another is made.")


def decide_blocker(decision: AemDecision, user) -> str:
    """Why `user` cannot approve or reject this proposal, apart from permissions ('' when they can)."""
    if decision.status != AemStatus.PROPOSED:
        return f"This proposal is no longer open: it was {decision.get_status_display().lower()}."
    if user is not None and decision.proposed_by_id and decision.proposed_by_id == user.pk:
        return ("You proposed this change. The committee signs off on someone else's case, so another approver records its decision "
                "(you can still withdraw it).")
    return ""


def pull_in_plan(dm: DeviceModel, interval_months: int, today: date | None = None) -> list[tuple[Asset, date]]:
    """The devices in use whose next PM is later than one `interval_months` after their last PM (or install date, or today when
    neither is on file), each with the date it moves to: that date, or today when it is already past. A next PM already earlier
    never moves later."""
    today = today or date.today()
    out = []
    for asset in Asset.objects.filter(device_model=dm, status__in=Asset.ACTIVE_STATUSES, next_pm_on__isnull=False).order_by("tag"):
        due = max(today, add_months(asset.last_pm_on or asset.installed_on or today, interval_months))
        if due < asset.next_pm_on:
            out.append((asset, due))
    return out


# --- changing it ----------------------------------------------------------------------------------------------------------


def _lock(dm: DeviceModel) -> DeviceModel:
    """The model's row, locked for this transaction (a no-op on SQLite): every AEM change for a model happens under this lock."""
    return DeviceModel.objects.select_for_update().get(pk=dm.pk)


def _locked_decision(decision: AemDecision) -> AemDecision:
    return AemDecision.objects.select_for_update().select_related("device_model").get(pk=decision.pk)


def _save(obj, by, reason: str) -> None:
    obj._change_reason = reason[:CHANGE_REASON_MAX]
    if by is not None:
        obj._history_user = by
    obj.save()


def _set_interval(dm: DeviceModel, months: int | None, by, reason: str, *also) -> None:
    """Set the model's AEM interval (the one place it is written), and on the caller's copies of the row too."""
    dm.aem_interval_months = months
    dm._change_reason = reason[:CHANGE_REASON_MAX]
    if by is not None:
        dm._history_user = by
    dm.save(update_fields=["aem_interval_months", "updated_at"])
    for other in also:
        if other is not None and other.pk == dm.pk:
            other.aem_interval_months = months


def _pull_in(dm: DeviceModel, *, by, today: date) -> int:
    from apps.equipment.services import update_asset

    plan = pull_in_plan(dm, dm.pm_interval_months, today)
    for asset, due in plan:
        update_asset(asset, next_pm_on=due, by=by, today=today)  # moves the open PM work order with it
    return len(plan)


def _text(value, field: str, limit: int, missing: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationError({field: missing})
    if len(text) > limit:
        raise ValidationError({field: f"Keep it to {limit} characters (this is {len(text)})."})
    return text


def _interval(value, dm: DeviceModel) -> int:
    message = f"The interval is a whole number of months, 1 to {INTERVAL_MAX}."
    if isinstance(value, bool):
        raise ValidationError({"interval_months": message})
    if isinstance(value, int):
        months = value
    else:
        raw = str(value if value is not None else "").strip()
        if not raw.isdigit():
            raise ValidationError({"interval_months": "Enter the proposed interval in months." if not raw else message})
        months = int(raw)
    if not 1 <= months <= INTERVAL_MAX:
        raise ValidationError({"interval_months": message})
    if months == dm.oem_pm_interval_months:
        raise ValidationError({"interval_months": f"That is the OEM interval ({_months(months)}). An AEM interval differs from it."})
    if months == dm.aem_interval_months:
        raise ValidationError({"interval_months": f"This model already runs on {_months(months)}."})
    return months


def _check_eligible(dm: DeviceModel) -> None:
    if dm.risk_class == RiskClass.LIFE_SUPPORT:
        raise ValidationError(LIFE_SUPPORT_REFUSAL)


def _check_history(dm: DeviceModel, today: date) -> dict:
    ev = evidence(dm, today)
    if not ev["enough_history"]:
        raise ValidationError(history_refusal(ev))
    return ev


def _check_decision(decision: AemDecision, by, decided_on, today: date) -> date:
    blocker = decide_blocker(decision, by)
    if blocker:
        raise ValidationError(blocker)
    if by is None:
        raise ValidationError("A committee decision is recorded by a signed-in approver.")
    if not decided_on:
        raise ValidationError({"decided_on": "Enter the date of the committee's meeting."})
    if decided_on > today:
        raise ValidationError({"decided_on": "The committee's decision cannot be dated in the future."})
    if decided_on < decision.proposed_on:
        raise ValidationError({"decided_on": f"The committee cannot decide before the proposal was made ({_day(decision.proposed_on)})."})
    return decided_on


@transaction.atomic
def propose(dm: DeviceModel, *, interval_months, rationale, by=None, today: date | None = None) -> AemDecision:
    """Open an AEM case for the model: a new interval and the case for it, with the model's failure history as it stands today."""
    today = today or date.today()
    locked = _lock(dm)
    _check_eligible(locked)
    open_ = open_proposal(locked)
    if open_ is not None:
        raise ValidationError(_open_refusal(open_))
    months = _interval(interval_months, locked)
    rationale = _text(rationale, "rationale", RATIONALE_MAX, "Make the case for the new interval: the failure history and why it is safe.")
    ev = _check_history(locked, today)
    decision = AemDecision(device_model=locked, interval_months=months, oem_interval_months=locked.oem_pm_interval_months, rationale=rationale,
                           evidence=ev, proposed_by=by, proposed_on=today)
    _save(decision, by, "Proposed")
    return decision


@transaction.atomic
def approve(decision: AemDecision, *, by, decided_on: date, note: str, today: date | None = None) -> AemDecision:
    """The committee approves an open proposal: it goes in force (ending the interval in force before it, if any) and becomes the
    model's PM interval. Life support and the history are checked again. Returns the decision, with `devices_moved`: how many
    devices' next PM came in because the new interval is shorter than the one before it."""
    today = today or date.today()
    dm = _lock(decision.device_model)
    current = _locked_decision(decision)
    decided_on = _check_decision(current, by, decided_on, today)
    note = _text(note, "note", NOTE_MAX, "Enter the committee's minutes reference.")
    _check_eligible(dm)
    if current.interval_months == dm.oem_pm_interval_months:
        raise ValidationError(f"The OEM interval is now {_months(dm.oem_pm_interval_months)}, the same as this proposal. Withdraw it instead.")
    _check_history(dm, today)
    before = dm.pm_interval_months
    replacing = AemDecision.objects.select_for_update().filter(device_model=dm, status=AemStatus.APPROVED).first()
    if replacing is not None:
        replacing.status = AemStatus.ENDED
        replacing.ended_by, replacing.ended_on = by, decided_on
        replacing.end_reason = f"Replaced by the {current.interval_months}-month interval approved on {_day(decided_on)}."[:REASON_MAX]
        _save(replacing, by, "Replaced")
    current.status = AemStatus.APPROVED
    current.decided_by, current.decided_on, current.decision_note = by, decided_on, note
    _save(current, by, "Approved")
    legacy = " (replacing one on file without a recorded approval)" if replacing is None and dm.aem_interval_months else ""
    _set_interval(dm, current.interval_months, by, f"AEM approved: {_months(current.interval_months)}{legacy}", decision.device_model)
    current.devices_moved = _pull_in(dm, by=by, today=today) if current.interval_months < before else 0
    return current


@transaction.atomic
def reject(decision: AemDecision, *, by, decided_on: date, note: str, today: date | None = None) -> AemDecision:
    """The committee turns an open proposal down; the model keeps its interval."""
    today = today or date.today()
    _lock(decision.device_model)
    current = _locked_decision(decision)
    decided_on = _check_decision(current, by, decided_on, today)
    note = _text(note, "note", NOTE_MAX, "Enter the committee's reason or minutes reference.")
    current.status = AemStatus.REJECTED
    current.decided_by, current.decided_on, current.decision_note = by, decided_on, note
    _save(current, by, "Rejected")
    return current


@transaction.atomic
def withdraw(decision: AemDecision, *, by=None, today: date | None = None, reason: str = "") -> AemDecision:
    """Close an open proposal without a committee decision. Who withdrew it, when, and why are kept in ended_by, ended_on, and
    end_reason (the case closed). The view decides who may: the proposer, or a PM Approve holder."""
    today = today or date.today()
    _lock(decision.device_model)
    current = _locked_decision(decision)
    if current.status != AemStatus.PROPOSED:
        raise ValidationError(f"This proposal is no longer open: it was {current.get_status_display().lower()}.")
    reason = (reason or "").strip() or ("Withdrawn by the proposer." if by is not None and by.pk == current.proposed_by_id else "Withdrawn.")
    current.status = AemStatus.WITHDRAWN
    current.ended_by, current.ended_on, current.end_reason = by, today, reason[:REASON_MAX]
    _save(current, by, "Withdrawn")
    return current


@transaction.atomic
def end(target: AemDecision | DeviceModel, *, by=None, reason: str, today: date | None = None) -> int:
    """End the AEM interval in force: the approved decision given, or the model's (approved, or on file without a recorded
    approval). The model goes back to the OEM interval and every device whose next PM is now too far out comes in. Returns how
    many devices' next PM moved."""
    today = today or date.today()
    given = target if isinstance(target, AemDecision) else None
    dm = _lock(target.device_model if given else target)
    reason = _text(reason, "reason", REASON_MAX, "Say why the AEM interval ends.")
    current = AemDecision.objects.select_for_update().filter(device_model=dm, status=AemStatus.APPROVED).first()
    if given is not None and (current is None or current.pk != given.pk):
        raise ValidationError("This AEM interval is no longer in force.")
    if current is None and dm.aem_interval_months is None:
        raise ValidationError("This model has no AEM interval in force: it follows the OEM interval.")
    if current is not None:
        current.status = AemStatus.ENDED
        current.ended_by, current.ended_on, current.end_reason = by, today, reason
        _save(current, by, "Ended")
    label = "AEM ended" if current is not None else "AEM on file without a recorded approval ended"
    _set_interval(dm, None, by, f"{label}: {reason}", target if given is None else given.device_model)
    return _pull_in(dm, by=by, today=today)


def model_changed(device_model, changed: list[str], by=None) -> None:
    """Called by equipment.services.update_device_model, inside its transaction, after a model's risk class or OEM interval
    changed (`changed` names the fields). A model that became life support leaves AEM: the interval in force ends (its devices'
    next PMs come in) and an open proposal is withdrawn. A new OEM interval equal to the AEM interval in force ends it (nothing
    left to approve). Anything else changes nothing."""
    today = date.today()
    if "risk_class" in changed and device_model.risk_class == RiskClass.LIFE_SUPPORT:
        reason = "The model is now life support; life-support devices are excluded from AEM by policy."
        proposal = open_proposal(device_model)
        if proposal is not None:
            withdraw(proposal, by=by, today=today, reason=reason)
        if device_model.aem_interval_months is not None or in_force(device_model) is not None:
            end(device_model, by=by, reason=reason, today=today)
        return
    aem = device_model.aem_interval_months
    if "oem_pm_interval_months" in changed and aem is not None and aem == device_model.oem_pm_interval_months:
        end(device_model, by=by, reason=f"The OEM interval is now {_months(aem)}, the same as the AEM interval.", today=today)
