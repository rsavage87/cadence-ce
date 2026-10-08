"""
The survey binder's "Scheduled maintenance (PM) completion" section (slice 25): PM completion in the period by risk class against the
facility's targets, and the PMs and devices behind every miss.

One definition: the PMs counted are apps.pm.services.pm_due_queryset's for the period (as_of today), the rule of the Overview and
pm_on_time_rate, so the binder's totals are the Overview's for the same days. A PM is on time when completed on or before its due date;
the ones not on time are missed_pms' within the period. Devices past their PM date today follow the PM compliance report
(apps.reports.fleet.report_compliance): active, with a next PM date before today, or marked missing (but for a new device waiting for
its incoming inspection, slice 26: its PM schedule has not started). Risk class is each device model's class today.

Tables: the figures by class; "PMs not on time" (why late, from WorkOrder.late_reason, and when that reason was recorded, from the work
order's history); "Due dates moved after they had passed" (from WorkOrder history: a PM's due date moved later on a day after the old
one had passed; such PMs count against their current due date like any other, and the table shows them); "Failed PMs" and the repair
each opened or named; and life-support and high-risk devices past their PM date today.

Gaps: a life-support or high-risk PM not on time with no reason recorded (imported PMs never: the previous system had no such field);
a life-support or high-risk device past its PM date. Findings: a medium or low risk class below its target (life support and high have
their per-PM gaps instead); a life-support or high-risk PM whose due date was moved after it had passed.

Read only, a fixed number of queries whatever the facility's size; nothing a requester typed (problem, resolution, notes) is read.
Historical rows through apps.core.history._rows (this facility's only), named through history.who.

Slice 27, the PM completion window (apps.pm.windows; Settings): on time means completed within the window of the PM's class (by the due
date unless the facility chose otherwise, which gives exactly the section above), read once per build and passed down. A PM is judged
by today's window and its model's class today. Under another window the section also shows, by class, the PMs on time but done after
their due date and the PMs still inside their window today (not counted yet: apps.pm.services.pm_pending); a table of the life-support
and high-risk PMs on time only because of the window, marking the models CMS keeps on the manufacturer's schedule; "Window ended" beside
each PM not on time (Days late stays from the due date: the fact the record shows); and every life-support or high-risk device past its
due date, a GAP only once its window has closed (apps.pm.services.assets_past_window's rule, as the PM compliance report counts it),
the others showing the day their window ends. Due dates moved after they had passed stay on the due date (a move pushes the window out
with it). The tables keep their columns by the due date, so nothing changes for a facility that never chooses a window.

Slice 28, a device held as evidence for an incident investigation (Asset.incident_hold): nobody uses, repairs, or tests it until the
incident releases it, so its PM cannot be done. A held life-support or high-risk device past its PM date is a FINDING that says so
("held as evidence for an incident investigation since <day>; not in use"), never a GAP; so is its open PM due on or after the day the
hold began with no reason recorded (releasing the hold records LateReason.INCIDENT_HOLD on it). A PM that was already late when the
device was held keeps its GAP: the hold does not explain it. The held day is the earliest active hold's (one query, only when a device
listed is held), and the status column reads "Held for incident" (equipment.services.HELD_LABEL).
"""
from __future__ import annotations

from bisect import bisect_left
from decimal import ROUND_DOWN, Decimal

from django.db.models import Count, F, Min, Q
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows, who
from apps.equipment.models import Asset, AssetStatus, RiskClass
from apps.equipment.services import HELD_LABEL
from apps.facility.services import compliance_targets, get_settings
from apps.incidents.models import IncidentHold
from apps.incidents.models import Status as IncidentStatus
from apps.pm.services import RETIRED_AND_CANCELLED, pm_due_queryset
from apps.pm.windows import ASSET_RISK, Windows, windows
from apps.reports.fleet import on_time_words
from apps.workorders.completion import RECORDED_ON
from apps.workorders.models import OPEN_STATUSES, LateReason, PmResult, Source, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType

from . import DEVICE, FINDING, GAP, WORK_ORDER, Figure, Gap, Period, Section, Table

KEY, TITLE = "maintenance", "Scheduled maintenance (PM) completion"
CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
CRITICAL = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH)  # every miss on these is listed one by one
IMPORTED_REASON = "Imported from the previous system"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
FOUND = tuple(s for s in Asset.ACTIVE_STATUSES if s != AssetStatus.MISSING)  # active and not missing (a missing device is past already)


def _past_window(today, w: Windows) -> Q:
    """The PM compliance report's "overdue now" (apps.reports.fleet.report_compliance): a next PM whose window has closed by today
    (apps.pm.services.assets_past_window; by the due date, a next PM date before today), or marked missing (a missing device can never
    be shown compliant), but for a new device waiting for its incoming inspection (slice 26: its PM schedule starts when it passes, so
    it has no PM to be past). For active devices."""
    return w.past_window_q(today, date_field="next_pm_on", risk=ASSET_RISK) | Q(status=AssetStatus.MISSING, awaiting_inspection=False)


def _past_due_date(today) -> Q:
    """Past its PM date on the schedule: a next PM date before today (or marked missing, as above), whatever the window. The devices
    the binder lists; _past_window decides which are gaps."""
    return Q(next_pm_on__lt=today) | Q(status=AssetStatus.MISSING, awaiting_inspection=False)


def _after_due_on_time(w: Windows) -> Q:
    """On time, but completed after its due date: inside the window's grace (never under the default window)."""
    return w.on_time_q() & Q(completed_on__gt=F("due_on"))


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _n_days(n: int) -> str:
    return f"{n} day" + ("" if n == 1 else "s")


def _class_name(rc: str) -> str:
    return "life support" if rc == RiskClass.LIFE_SUPPORT else f"{RiskClass(rc).label.lower()} risk"


def _rate(on_time: int, total: int) -> Decimal:
    """The on-time share in percent, to one decimal, cut (never rounded up): a class that missed its target never shows reaching it.
    100 when nothing was due, as pm_on_time_rate."""
    if not total:
        return Decimal("100.0")
    return (Decimal(on_time * 100) / Decimal(total)).quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def _meets(on_time: int, total: int, target) -> bool:
    """Exact, as report_compliance compares: 19 of 20 is 95% and meets a 95% target."""
    return Decimal(on_time * 100) >= Decimal(str(target)) * total if total else True


def _pct(value) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".") + "%"


# --- the figures by class ---------------------------------------------------------------------------------------------------


def _pending_by_class(period: Period, w: Windows) -> dict:
    """{class: PMs due in the period by today, not done, whose window is still open}: apps.pm.services.pm_pending's rule by class (the
    PMs pm_due_queryset does not count yet). One query; none under the default window, where there are never any."""
    if w.is_default:
        return {}
    today = period.today
    return dict(WorkOrder.objects.filter(type=WoType.PM, due_on__gte=period.start, due_on__lte=min(period.end, today), completed_on__isnull=True)
                .filter(w.inside_window_q(today)).exclude(RETIRED_AND_CANCELLED).order_by()
                .values_list("asset__device_model__risk_class").annotate(n=Count("id")))


def _by_class(due, period: Period, w: Windows, targets: dict) -> tuple[list[dict], dict]:
    """[{rc, label, due, on_time, late, rate, target, meets, past, after_due, pending, inside}] in CLASS_ORDER, and the totals. Two
    queries (the PMs counted by class, the devices past their PM window by class), a third under a window other than the due date (the
    PMs still inside it). after_due: on time but done after the due date; pending: still inside their window today, not counted;
    inside: devices past their due date but still inside their window (not missing)."""
    today = period.today
    counts = {"n": Count("id"), "on_time": Count("id", filter=w.on_time_q())}
    devices = {"past": Count("id", filter=_past_window(today, w))}
    if not w.is_default:
        counts["after_due"] = Count("id", filter=_after_due_on_time(w))
        devices["inside"] = Count("id", filter=w.inside_window_q(today, date_field="next_pm_on", risk=ASSET_RISK) & Q(status__in=FOUND))
    counted = {row["rc"]: row for row in due.order_by().values(rc=F("asset__device_model__risk_class")).annotate(**counts)}
    fleet = {row["rc"]: row for row in Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).order_by()
             .values(rc=F("device_model__risk_class")).annotate(**devices)}
    pending = _pending_by_class(period, w)
    classes = []
    for rc in CLASS_ORDER:
        row, f = counted.get(rc.value, {}), fleet.get(rc.value, {})
        n, on_time = row.get("n", 0), row.get("on_time", 0)
        classes.append({"rc": rc, "label": rc.label, "due": n, "on_time": on_time, "late": n - on_time, "rate": _rate(on_time, n),
                        "target": targets[rc], "meets": _meets(on_time, n, targets[rc]), "past": f.get("past", 0),
                        "after_due": row.get("after_due", 0), "pending": pending.get(rc.value, 0), "inside": f.get("inside", 0)})
    due_total, on_time_total = sum(c["due"] for c in classes), sum(c["on_time"] for c in classes)
    totals = {"due": due_total, "on_time": on_time_total, "late": due_total - on_time_total, "rate": _rate(on_time_total, due_total),
              **{key: sum(c[key] for c in classes) for key in ("past", "after_due", "pending", "inside")}}
    return classes, totals


# --- PMs not on time --------------------------------------------------------------------------------------------------------


def _done_later(late, since) -> dict:
    """{asset id: [(completed_on, number)] in order}: every PM completed on the devices of `late` since `since` (the earliest due
    date among them), on any work order. One query."""
    out: dict = {}
    for asset_id, completed_on, number in (WorkOrder.objects.filter(type=WoType.PM, status__in=DONE, completed_on__isnull=False,
                                                                    asset_id__in=late.order_by().values("asset_id"), completed_on__gte=since)
                                           .order_by("asset_id", "completed_on", "number").values_list("asset_id", "completed_on", "number")):
        out.setdefault(asset_id, []).append((completed_on, number))
    return out


def _first_done(done: dict, asset_id, due_on):
    """The first PM completed on the device on or after `due_on`: (completed_on, number), or None."""
    pms = done.get(asset_id, [])
    i = bisect_left(pms, (due_on, ""))
    return pms[i] if i < len(pms) else None


def _reason_recorded(late) -> dict:
    """{work order id: (reason, the facility day the current run of that reason began)} from the work orders' own history, for the
    ones with a reason. One query over this facility's history rows."""
    history = WorkOrder.history.model
    out, previous = {}, {}
    rows = (_rows(history).filter(id__in=late.exclude(late_reason="").order_by().values("id"))
            .order_by("id", "history_date", "history_id").values_list("id", "late_reason", "history_date"))
    for pk, reason, when in rows:
        if reason != previous.get(pk):
            out[pk] = (reason, local_day(when))
        previous[pk] = reason
    return out


def _late_rows(late, today, w: Windows):
    """The PMs not on time, by due date. Under a window other than the due date, "Window ended" (the last day the PM would have been
    on time) follows its due date; Days late stays counted from the due date."""
    rows = list(late.order_by("due_on", "number").values_list(
        "id", "number", "asset_id", "asset__tag", "asset__device_model__manufacturer", "asset__device_model__model",
        "asset__device_model__risk_class", "due_on", "completed_on", "status", "late_reason", "source"))
    if not rows:
        return
    done, recorded = _done_later(late, rows[0][7]), _reason_recorded(late)  # rows are in due date order
    for pk, number, asset_id, tag, make, model, rc, due_on, completed_on, status, reason, source in rows:
        later = _first_done(done, asset_id, due_on)
        why = LateReason(reason).label if reason else (IMPORTED_REASON if source == Source.IMPORTED else "")
        when = recorded.get(pk)
        recorded_on = when[1] if when and reason and when[0] == reason and completed_on and when[1] > completed_on else None
        window = [] if w.is_default else [w.end(due_on, rc)]
        yield [number, tag, f"{make} {model}", RiskClass(rc).label, due_on, *window, completed_on or WoStatus(status).label,
               ((completed_on or today) - due_on).days, later[0] if later else None, later[1] if later else "", why, recorded_on]


def _no_reason(late) -> list[tuple]:
    """The life-support and high-risk PMs not on time with no reason recorded, imported ones never: (number, tag, due_on, completed_on,
    status, risk class, device id, the device held now). One query."""
    return list(late.filter(asset__device_model__risk_class__in=CRITICAL, late_reason="").exclude(source=Source.IMPORTED)
                .order_by("due_on", "number").values_list("number", "asset__tag", "due_on", "completed_on", "status",
                                                          "asset__device_model__risk_class", "asset_id", "asset__incident_hold"))


def _held_since(asset_ids) -> dict:
    """{device id: the day its earliest active hold began} for the devices held by an open incident (slice 28). One query."""
    return dict(IncidentHold.objects.filter(asset_id__in=asset_ids, released_on__isnull=True, incident__status=IncidentStatus.OPEN)
                .order_by().values("asset_id").annotate(since=Min("held_on")).values_list("asset_id", "since"))


def _held_words(since) -> str:
    """Why a held device's PM is not done (slice 28)."""
    when = f" since {_day(since)}" if since else ""
    return f"held as evidence for an incident investigation{when}; not in use"


def _no_reason_gaps(rows, today, w: Windows, held: dict) -> list[Gap]:
    """A GAP per life-support or high-risk PM not on time with no reason recorded (_no_reason). Under a window other than the due date,
    each says when its window ended. Slice 28: a PM still open on a device held now, due on or after the day its hold began, is a
    FINDING saying it is held (`held`: _held_since), since its device may not be touched until the incident releases it."""
    gaps = []
    for number, tag, due_on, completed_on, status, rc, asset_id, is_held in rows:
        since = held.get(asset_id) if is_held else None
        if is_held and status in OPEN_STATUSES and (since is None or due_on >= since):
            gaps.append(Gap(FINDING, f"{number} on {tag} is {_n_days((today - due_on).days)} past its due date: {tag} is {_held_words(since)}; "
                                     "the reason is recorded when the hold is released", url=reverse("web:wo", args=[number]), record=number))
            continue
        if w.is_default:
            if completed_on:
                text = f"{number} on {tag} was done {_n_days((completed_on - due_on).days)} late with no reason recorded"
            elif status == WoStatus.CANCELLED:
                text = f"{number} on {tag} was cancelled without being done (due {_day(due_on)}) with no reason recorded"
            else:
                text = f"{number} on {tag} is {_n_days((today - due_on).days)} past its due date with no reason recorded"
        else:
            ended = _day(w.end(due_on, rc))
            if completed_on:
                text = (f"{number} on {tag} was done {_n_days((completed_on - due_on).days)} after its due date, after its window ended "
                        f"({ended}), with no reason recorded")
            elif status == WoStatus.CANCELLED:
                text = f"{number} on {tag} was cancelled without being done (due {_day(due_on)}, window ended {ended}) with no reason recorded"
            else:
                text = (f"{number} on {tag} is {_n_days((today - due_on).days)} past its due date and its window ended {ended}, with no "
                        "reason recorded")
        gaps.append(Gap(GAP, text, url=reverse("web:wo", args=[number]), record=number))
    return gaps


def _window_rows(due, w: Windows):
    """Life-support and high-risk PMs on time only because of the window: counted, completed after the due date, inside the window.
    Each with its window's end, how many days after the due date it was done, and whether CMS keeps the model on the manufacturer's
    schedule. One query, lazy."""
    rows = (due.filter(asset__device_model__risk_class__in=CRITICAL).filter(_after_due_on_time(w)).order_by("due_on", "number")
            .values_list("number", "asset__tag", "asset__device_model__manufacturer", "asset__device_model__model",
                         "asset__device_model__risk_class", "due_on", "completed_on", "asset__device_model__oem_schedule_required"))
    for number, tag, make, model, rc, due_on, completed_on, cms in rows:
        yield [number, tag, f"{make} {model}", RiskClass(rc).label, due_on, w.end(due_on, rc), completed_on, (completed_on - due_on).days,
               "Yes" if cms else ""]


# --- due dates moved after they had passed ----------------------------------------------------------------------------------


def _moves(period: Period) -> list[dict]:
    """Each time a PM's due date moved later on a day after the old due date had passed, the old due date in the period, while the PM
    was not done by it: {number, asset_id, old, new, on, by}. Two queries over this facility's work order history (the versions of
    every work order that was a PM due in the period at some point, then the moving rows for who made them)."""
    history = WorkOrder.history.model
    rows = _rows(history)
    ever_due = rows.filter(type=WoType.PM, due_on__gte=period.start, due_on__lte=period.end).order_by().values("id")
    found, previous = [], {}
    for history_id, pk, number, asset_id, wo_type, source, due_on, completed_on, when in (
            rows.filter(id__in=ever_due).order_by("id", "history_date", "history_id")
            .values_list("history_id", "id", "number", "asset_id", "type", "source", "due_on", "completed_on", "history_date")):
        before = previous.get(pk)
        previous[pk] = (due_on, completed_on)
        if before is None or wo_type != WoType.PM or source == Source.IMPORTED:
            continue
        old_due, old_completed = before
        moved_on = local_day(when)
        if (due_on > old_due and moved_on > old_due and period.start <= old_due <= period.end
                and not (old_completed and old_completed <= old_due)):
            found.append({"history_id": history_id, "id": pk, "number": number, "asset_id": asset_id, "old": old_due, "new": due_on,
                          "on": moved_on})
    if found:
        people = {rec.history_id: who(rec) for rec in
                  rows.filter(history_id__in=[m["history_id"] for m in found]).select_related("history_user").only("history_id", "history_user")}
        for m in found:
            m["by"] = people.get(m["history_id"], "")
    return found


# --- failed PMs -------------------------------------------------------------------------------------------------------------


def _failed(period: Period):
    return WorkOrder.objects.filter(type=WoType.PM, pm_result=PmResult.FAIL, completed_on__gte=period.start, completed_on__lte=period.end)


def _failed_rows(failed):
    rows = list(failed.order_by("completed_on", "number").values_list("id", "number", "asset__tag", "completed_on"))
    if not rows:
        return
    repairs: dict = {}
    for pm_id, number, status, completed_on in (WorkOrder.objects.filter(follow_up_of__in=failed.order_by().values("id"))
                                                .order_by("opened_on", "number").values_list("follow_up_of_id", "number", "status", "completed_on")):
        repairs.setdefault(pm_id, (number, WoStatus(status).label, completed_on))
    # A failure recorded on a repair that already followed another PM: its completion note names the repair (completion.RECORDED_ON;
    # review fix). Two queries for all of them.
    named: dict = {}
    for pm_id, note in (WorkOrderStatusHistory.objects.filter(work_order_id__in=[r[0] for r in rows if r[0] not in repairs],
                                                               to_status=WoStatus.COMPLETED, note__contains=RECORDED_ON)
                        .order_by("created_at", "id").values_list("work_order_id", "note")):
        named[pm_id] = note.rsplit(RECORDED_ON, 1)[1].strip()  # the last completion wins
    if named:
        found = {n: (n, WoStatus(st).label, c) for n, st, c in
                 WorkOrder.objects.filter(number__in=set(named.values())).values_list("number", "status", "completed_on")}
        for pm_id, number in named.items():
            if number in found:
                repairs[pm_id] = found[number]
    for pk, number, tag, completed_on in rows:
        repair = repairs.get(pk, ("", "", None))
        yield [number, tag, completed_on, *repair]


# --- devices past their PM date today ---------------------------------------------------------------------------------------


def _critical_past(today, w: Windows) -> list[dict]:
    """Life-support and high-risk devices past their PM date today (or marked missing), each with its open PM work order (the earliest
    opened), when its window ends (`end`), and whether it is a gap: past its window, or missing. By the due date every one listed is.
    Two queries."""
    devices = list(Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, device_model__risk_class__in=CRITICAL).filter(_past_due_date(today))
                   .order_by(F("next_pm_on").asc(nulls_last=True), "tag")
                   .values("id", "tag", "status", "next_pm_on", rc=F("device_model__risk_class"), make=F("device_model__manufacturer"),
                           model=F("device_model__model"), held=F("incident_hold")))
    if devices:
        open_pm: dict = {}
        for asset_id, number in (WorkOrder.objects.filter(type=WoType.PM, status__in=OPEN_STATUSES, asset_id__in=[d["id"] for d in devices])
                                 .order_by("opened_on", "number").values_list("asset_id", "number")):
            open_pm.setdefault(asset_id, number)
        for d in devices:
            d["open_pm"] = open_pm.get(d["id"], "")
            d["days"] = (today - d["next_pm_on"]).days if d["next_pm_on"] and d["next_pm_on"] < today else None
            d["end"] = w.end(d["next_pm_on"], d["rc"]) if d["days"] is not None else None
            d["past_window"] = d["end"] is not None and d["end"] < today
            d["gap"] = d["past_window"] or d["status"] == AssetStatus.MISSING
    return devices


def _window_cell(d) -> str | None:
    """The past_pm_date table's PM window column (a window other than the due date): "Ended Mar 31, 2026" or "On time until Apr 30,
    2026"; empty for a missing device whose PM date has not passed."""
    if d["end"] is None:
        return None
    return f"Ended {_day(d['end'])}" if d["past_window"] else f"On time until {_day(d['end'])}"


def _device_gap(d, w: Windows, held: dict) -> Gap:
    name = _class_name(d["rc"])
    if d["held"] and d["past_window"]:  # slice 28: it may not be touched until the incident releases it
        text = f"{d['tag']} ({name}) is {_n_days(d['days'])} past its PM date ({_day(d['next_pm_on'])})"
        if not w.is_default:
            text += f" and its window ended {_day(d['end'])}"
        text += f": {_held_words(held.get(d['id']))}"
        return Gap(FINDING, text, url=reverse("web:asset", args=[d["tag"]]), record=d["tag"])
    if d["past_window"]:
        text = f"{d['tag']} ({name}) is {_n_days(d['days'])} past its PM date ({_day(d['next_pm_on'])})"
        if not w.is_default:
            text += f" and its window ended {_day(d['end'])}"
        text += f"; {d['open_pm']} is open" if d["open_pm"] else "; no PM work order is open"
    else:
        text = f"{d['tag']} ({name}) is marked missing; a missing device counts as past its PM date until it is found"
    return Gap(GAP, text, url=reverse("web:asset", args=[d["tag"]]), record=d["tag"])


# --- the section ------------------------------------------------------------------------------------------------------------


DEFAULT_NOTES = [
    "On time: a PM is on time when completed on or before its due date, as on the Overview. No grace period is added.",
    "PMs counted: those due in the period, as the Overview counts them for the same days (a PM due on the period's last day counts "
    "once it is completed).",
    "Risk class is each device model's class today, not the class it had when the PM was due.",
    "PMs imported from the previous system count in the rates. Their reason reads “Imported from the previous system”, and they "
    "are never listed as missing a reason.",
    "A PM cancelled while its device was retired on the PM's due date is not counted. One cancelled on a device still in use on its "
    "due date, retired later or not, counts as not on time.",
    "A PM whose due date was moved counts against its current due date. Moves made after the old due date had passed are listed.",
    "Days late: to the PM's completion, or to today when it was never completed (cancelled, or still open). Done later on: the first PM "
    "completed on the device on or after this PM's due date, on any work order, so a cancelled PM shows when the device's PM was done.",
    "Devices past their PM date today: active devices whose next PM date is before today, or that are marked missing, as the PM "
    "compliance report counts them. This counts devices now, not PMs in the period. A new device waiting for its incoming "
    "inspection is never past its PM date, missing or not: its PM schedule starts when the inspection passes.",
    "On-time shares are cut to one decimal, never rounded up, so a class that missed its target never shows reaching it.",
]
# Slice 28: the last note under any window.
HELD_NOTE = ("A device held as evidence for an incident investigation is not used, repaired, or tested until the incident releases it. "
             "Past its PM date it is listed as a finding, never a gap, and so is its open PM due since the hold began; releasing the hold "
             "records why that PM was late. A PM already late when the device was held is a gap like any other.")


def _window_notes(w: Windows) -> list[str]:
    """The notes under a window other than the due date (slice 27), the rule in words from Window.describe."""
    return [
        f"On time: a PM is on time when completed within the facility's PM window, as on the Overview: {on_time_words(w)} (Settings). "
        "Each PM is judged by today's window, so a change to the window recounts past months; the Program and policies section says "
        "when it last changed.",
        "PMs counted: those due in the period, as the Overview counts them for the same days: once completed, or once their window has "
        "closed. Still inside their window today: due in the period, not done, with their window still open; they are counted when "
        "they are done or their window closes.",
        "On time, done after the due date: completed after the due date but inside the window. The life-support and high-risk ones are "
        "listed, marking the models CMS keeps on the manufacturer's schedule.",
        "Risk class is each device model's class today, not the class it had when the PM was due; the window is its class's.",
        "PMs imported from the previous system count in the rates. Their reason reads “Imported from the previous system”, and they "
        "are never listed as missing a reason.",
        "A PM cancelled while its device was retired on the PM's due date is not counted. One cancelled on a device still in use on its "
        "due date, retired later or not, counts as not on time once its window has closed.",
        "A PM whose due date was moved counts against its current due date, and its window moves with it. Moves made after the old due "
        "date had passed are listed.",
        "Days late: from the PM's due date, not its window, to its completion, or to today when it was never completed (cancelled, or "
        "still open). Window ended: the last day it would have counted as on time. Done later on: the first PM completed on the device "
        "on or after this PM's due date, on any work order, so a cancelled PM shows when the device's PM was done.",
        "Devices past their PM window today: active devices whose next PM's window has closed, or that are marked missing, as the PM "
        "compliance report counts them. This counts devices now, not PMs in the period. Every life-support and high-risk device past its "
        "PM date is listed; only those past their window, or missing, are gaps. A new device waiting for its incoming inspection is "
        "never past its PM date, missing or not: its PM schedule starts when the inspection passes.",
        "On-time shares are cut to one decimal, never rounded up, so a class that missed its target never shows reaching it.",
    ]


def build(period: Period, user) -> Section:
    today = period.today
    s = get_settings()
    w = windows(s)  # read once, with the targets' row, and passed down
    windowed = not w.is_default
    due = pm_due_queryset(period.start, period.end, today, w=w)
    late = due.filter(w.not_on_time_q())  # written out, never a negated on_time_q (which would drop the open PMs)
    classes, totals = _by_class(due, period, w, compliance_targets(s))
    moves = _moves(period)
    past = _critical_past(today, w)
    failed = _failed(period)
    failed_count = failed.count()
    past_label = "Devices past their PM window today" if windowed else "Devices past their PM date today"

    figures = [
        Figure("PMs due in the period", totals["due"], hint="Counted as the Overview counts them"),
        Figure("Completed on time", _pct(totals["rate"]), hint=f"{totals['on_time']} of {totals['due']}; {totals['late']} not on time"),
    ]
    if windowed:
        figures += [
            Figure("On time, done after the due date", totals["after_due"],
                   hint="Inside the window · " + " · ".join(f"{c['label']} {c['after_due']}" for c in classes)),
            Figure("Still inside their window today", totals["pending"],
                   hint="Due in the period and not done yet; counted when done or when the window closes"),
        ]
    for c in classes:
        name = _class_name(c["rc"])
        figures.append(Figure(f"PMs on time, {name}", _pct(c["rate"]),
                              hint=f"{c['on_time']} of {c['due']}; target {_pct(c['target'])}, {'met' if c['meets'] else 'not met'}"))
    past_hint = " · ".join(f"{c['label']} {c['past']}" for c in classes) + f" ({period.as_of_today})"
    if windowed and totals["inside"]:
        past_hint += f"; {totals['inside']} more past their PM date, still inside their window"
    figures.append(Figure(past_label, totals["past"], hint=past_hint))

    no_reason = _no_reason(late)
    held_ids = {row[6] for row in no_reason if row[7]} | {d["id"] for d in past if d["held"]}
    held = _held_since(held_ids) if held_ids else {}  # slice 28: one query, only when a device listed is held
    gaps = _no_reason_gaps(no_reason, today, w, held)
    gaps += [_device_gap(d, w, held) for d in past if d["gap"]]
    for c in classes:
        if c["rc"] not in CRITICAL and c["due"] and not c["meets"]:
            # No url: no one record is behind a class's share (its misses are listed in "PMs not on time").
            gaps.append(Gap(FINDING, f"{c['label']} risk: {_pct(c['rate'])} on time against the facility's {_pct(c['target'])} target",
                            record=f"{c['label']} risk PMs"))
    devices = {pk: (tag, rc) for pk, tag, rc in
               Asset.objects.filter(id__in={m["asset_id"] for m in moves}).values_list("id", "tag", "device_model__risk_class")} if moves else {}
    tags = {pk: tag for pk, (tag, _rc) in devices.items()}
    flagged = set()
    for m in moves:
        if devices.get(m["asset_id"], ("", ""))[1] in CRITICAL and m["id"] not in flagged:
            flagged.add(m["id"])
            gaps.append(Gap(FINDING, f"{m['number']} on {tags.get(m['asset_id'], '')}: its due date was moved from {_day(m['old'])} to "
                                     f"{_day(m['new'])} on {_day(m['on'])}, after it had passed",
                            url=reverse("web:wo", args=[m["number"]]), record=m["number"]))

    if windowed:
        class_columns = ["Risk class", "PMs due", "On time", "On time, done after the due date", "Not on time", "Still inside their window today",
                         "On time %", "Target %", "Target met", past_label]
        class_rows = [[c["label"], c["due"], c["on_time"], c["after_due"], c["late"], c["pending"], c["rate"], Decimal(str(c["target"])),
                       "Yes" if c["meets"] else "No", c["past"]] for c in classes]
        late_columns = ["Work order", "Device", "Model", "Risk class", "Due", "Window ended", "Completed on", "Days late", "Done later on",
                        "Done on work order", "Why late", "Reason recorded on"]
        late_links = {0: WORK_ORDER, 1: DEVICE, 9: WORK_ORDER}
        past_columns = ["Device", "Model", "Risk class", "Status", "Next PM", "Days past", "PM window", "Open PM work order"]
        past_links = {0: DEVICE, 7: WORK_ORDER}
    else:
        class_columns = ["Risk class", "PMs due", "On time", "Not on time", "On time %", "Target %", "Target met", past_label]
        class_rows = [[c["label"], c["due"], c["on_time"], c["late"], c["rate"], Decimal(str(c["target"])), "Yes" if c["meets"] else "No",
                       c["past"]] for c in classes]
        late_columns = ["Work order", "Device", "Model", "Risk class", "Due", "Completed on", "Days late", "Done later on", "Done on work order",
                        "Why late", "Reason recorded on"]
        late_links = {0: WORK_ORDER, 1: DEVICE, 8: WORK_ORDER}
        past_columns = ["Device", "Model", "Risk class", "Status", "Next PM", "Days past", "Open PM work order"]
        past_links = {0: DEVICE, 6: WORK_ORDER}
    move_rows = [[m["number"], tags.get(m["asset_id"], ""), m["old"], m["new"], m["on"], m["by"]] for m in moves]
    past_rows = [[d["tag"], f"{d['make']} {d['model']}", RiskClass(d["rc"]).label, HELD_LABEL if d["held"] else AssetStatus(d["status"]).label,
                  d["next_pm_on"], d["days"],
                  *([_window_cell(d)] if windowed else []), d["open_pm"]] for d in past]

    tables = [
        Table(key="by_class", title="PM completion by risk class", columns=class_columns, rows=lambda: iter(class_rows), count=len(class_rows)),
        Table(key="not_on_time", title="PMs not on time", columns=late_columns, rows=lambda: _late_rows(late, today, w), count=totals["late"],
              links=late_links, empty="Every PM due in this period was completed on time."),
    ]
    if windowed:
        by_window = sum(c["after_due"] for c in classes if c["rc"] in CRITICAL)
        tables.append(Table(key="on_time_by_window", title="Life-support and high-risk PMs on time only because of the window",
                            columns=["Work order", "Device", "Model", "Risk class", "Due", "Window ends", "Completed on",
                                     "Days after the due date", "On the manufacturer's schedule (CMS)"],
                            rows=lambda: _window_rows(due, w), count=by_window, links={0: WORK_ORDER, 1: DEVICE},
                            empty="None: every life-support and high-risk PM on time in this period was done by its due date."))
    tables += [
        Table(key="due_moved", title="Due dates moved after they had passed",
              columns=["Work order", "Device", "Due date before", "Moved to", "Moved on", "Moved by"],
              rows=lambda: iter(move_rows), count=len(move_rows), links={0: WORK_ORDER, 1: DEVICE}),
        Table(key="failed", title="Failed PMs",
              columns=["Work order", "Device", "Completed on", "Repair", "Repair status", "Repair completed on"],
              rows=lambda: _failed_rows(failed), count=failed_count, links={0: WORK_ORDER, 1: DEVICE, 3: WORK_ORDER}),
        Table(key="past_pm_date", title="Life-support and high-risk devices past their PM date today", columns=past_columns,
              rows=lambda: iter(past_rows), count=len(past_rows), links=past_links, empty="None today."),
    ]

    return Section(
        key=KEY, title=TITLE,
        topic="Scheduled maintenance done on time, by risk class, against the facility's targets; and every PM and device behind a miss.",
        covers=f"{period.label}; devices past their PM date {period.as_of_today}",
        figures=figures,
        gaps=gaps,
        tables=tables,
        notes=[*(_window_notes(w) if windowed else DEFAULT_NOTES), HELD_NOTE],
    )


