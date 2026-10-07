"""
The survey binder's "Scheduled maintenance (PM) completion" section (slice 25): PM completion in the period by risk class against the
facility's targets, and the PMs and devices behind every miss.

One definition: the PMs counted are apps.pm.services.pm_due_queryset's for the period (as_of today), the rule of the Overview and
pm_on_time_rate, so the binder's totals are the Overview's for the same days. A PM is on time when completed on or before its due date;
the ones not on time are missed_pms' within the period. Devices past their PM date today follow the PM compliance report
(apps.reports.fleet.report_compliance): active, with a next PM date before today, or marked missing. Risk class is each device model's
class today.

Tables: the figures by class; "PMs not on time" (why late, from WorkOrder.late_reason, and when that reason was recorded, from the work
order's history); "Due dates moved after they had passed" (from WorkOrder history: a PM's due date moved later on a day after the old
one had passed; such PMs count against their current due date like any other, and the table shows them); "Failed PMs" and the repair
each opened or named; and life-support and high-risk devices past their PM date today.

Gaps: a life-support or high-risk PM not on time with no reason recorded (imported PMs never: the previous system had no such field);
a life-support or high-risk device past its PM date. Findings: a medium or low risk class below its target (life support and high have
their per-PM gaps instead); a life-support or high-risk PM whose due date was moved after it had passed.

Read only, a fixed number of queries whatever the facility's size; nothing a requester typed (problem, resolution, notes) is read.
Historical rows through apps.core.history._rows (this facility's only), named through history.who.
"""
from __future__ import annotations

from bisect import bisect_left
from decimal import ROUND_DOWN, Decimal

from django.db.models import Count, F, Q
from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows, who
from apps.equipment.models import Asset, AssetStatus, RiskClass
from apps.facility.services import compliance_targets
from apps.pm.services import pm_due_queryset
from apps.workorders.models import OPEN_STATUSES, LateReason, PmResult, Source, WorkOrder, WoStatus, WoType

from . import DEVICE, FINDING, GAP, WORK_ORDER, Figure, Gap, Period, Section, Table

KEY, TITLE = "maintenance", "Scheduled maintenance (PM) completion"
CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
CRITICAL = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH)  # every miss on these is listed one by one
IMPORTED_REASON = "Imported from the previous system"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
ON_TIME = Q(completed_on__isnull=False, completed_on__lte=F("due_on"))
NOT_ON_TIME = Q(completed_on__isnull=True) | Q(completed_on__gt=F("due_on"))


def _past_pm_date(today) -> Q:
    """The PM compliance report's "overdue now" (apps.reports.fleet.report_compliance): a next PM date before today, or marked missing
    (a missing device can never be shown compliant). For active devices."""
    return Q(next_pm_on__lt=today) | Q(status=AssetStatus.MISSING)


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


def _by_class(due, today) -> tuple[list[dict], dict]:
    """[{rc, label, due, on_time, late, rate, target, meets, past}] in CLASS_ORDER, and the totals. Three queries: the PMs counted by
    class, the devices past their PM date by class, and the settings row for the targets."""
    counted = {row["rc"]: row for row in due.order_by().values(rc=F("asset__device_model__risk_class"))
               .annotate(n=Count("id"), on_time=Count("id", filter=ON_TIME))}
    past = dict(Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).filter(_past_pm_date(today)).order_by()
                .values_list("device_model__risk_class").annotate(n=Count("id")))
    targets = compliance_targets()
    classes = []
    for rc in CLASS_ORDER:
        row = counted.get(rc.value, {})
        n, on_time = row.get("n", 0), row.get("on_time", 0)
        classes.append({"rc": rc, "label": rc.label, "due": n, "on_time": on_time, "late": n - on_time, "rate": _rate(on_time, n),
                        "target": targets[rc], "meets": _meets(on_time, n, targets[rc]), "past": past.get(rc.value, 0)})
    due_total, on_time_total = sum(c["due"] for c in classes), sum(c["on_time"] for c in classes)
    totals = {"due": due_total, "on_time": on_time_total, "late": due_total - on_time_total, "rate": _rate(on_time_total, due_total),
              "past": sum(c["past"] for c in classes)}
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


def _late_rows(late, today):
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
        yield [number, tag, f"{make} {model}", RiskClass(rc).label, due_on, completed_on or WoStatus(status).label,
               ((completed_on or today) - due_on).days, later[0] if later else None, later[1] if later else "", why, recorded_on]


def _no_reason_gaps(late, today) -> list[Gap]:
    """A GAP per life-support or high-risk PM not on time with no reason recorded; imported ones never (one query)."""
    gaps = []
    rows = (late.filter(asset__device_model__risk_class__in=CRITICAL, late_reason="").exclude(source=Source.IMPORTED)
            .order_by("due_on", "number").values_list("number", "asset__tag", "due_on", "completed_on", "status"))
    for number, tag, due_on, completed_on, status in rows:
        if completed_on:
            text = f"{number} on {tag} was done {_n_days((completed_on - due_on).days)} late with no reason recorded"
        elif status == WoStatus.CANCELLED:
            text = f"{number} on {tag} was cancelled without being done (due {_day(due_on)}) with no reason recorded"
        else:
            text = f"{number} on {tag} is {_n_days((today - due_on).days)} past its due date with no reason recorded"
        gaps.append(Gap(GAP, text, url=reverse("web:wo", args=[number]), record=number))
    return gaps


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
    for pk, number, tag, completed_on in rows:
        repair = repairs.get(pk, ("", "", None))
        yield [number, tag, completed_on, *repair]


# --- devices past their PM date today ---------------------------------------------------------------------------------------


def _critical_past(today) -> list[dict]:
    """Life-support and high-risk devices past their PM date today, each with its open PM work order (the earliest opened). Two
    queries."""
    devices = list(Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES, device_model__risk_class__in=CRITICAL).filter(_past_pm_date(today))
                   .order_by(F("next_pm_on").asc(nulls_last=True), "tag")
                   .values("id", "tag", "status", "next_pm_on", rc=F("device_model__risk_class"), make=F("device_model__manufacturer"),
                           model=F("device_model__model")))
    if devices:
        open_pm: dict = {}
        for asset_id, number in (WorkOrder.objects.filter(type=WoType.PM, status__in=OPEN_STATUSES, asset_id__in=[d["id"] for d in devices])
                                 .order_by("opened_on", "number").values_list("asset_id", "number")):
            open_pm.setdefault(asset_id, number)
        for d in devices:
            d["open_pm"] = open_pm.get(d["id"], "")
            d["days"] = (today - d["next_pm_on"]).days if d["next_pm_on"] and d["next_pm_on"] < today else None
    return devices


def _device_gap(d) -> Gap:
    name = _class_name(d["rc"])
    if d["days"] is not None:
        text = f"{d['tag']} ({name}) is {_n_days(d['days'])} past its PM date ({_day(d['next_pm_on'])})"
        text += f"; {d['open_pm']} is open" if d["open_pm"] else "; no PM work order is open"
    else:
        text = f"{d['tag']} ({name}) is marked missing; a missing device counts as past its PM date until it is found"
    return Gap(GAP, text, url=reverse("web:asset", args=[d["tag"]]), record=d["tag"])


# --- the section ------------------------------------------------------------------------------------------------------------


def build(period: Period, user) -> Section:
    today = period.today
    due = pm_due_queryset(period.start, period.end, today)
    late = due.filter(NOT_ON_TIME)
    classes, totals = _by_class(due, today)
    moves = _moves(period)
    past = _critical_past(today)
    failed = _failed(period)
    failed_count = failed.count()

    figures = [
        Figure("PMs due in the period", totals["due"], hint="Counted as the Overview counts them"),
        Figure("Completed on time", _pct(totals["rate"]), hint=f"{totals['on_time']} of {totals['due']}; {totals['late']} not on time"),
    ]
    for c in classes:
        name = _class_name(c["rc"])
        figures.append(Figure(f"PMs on time, {name}", _pct(c["rate"]),
                              hint=f"{c['on_time']} of {c['due']}; target {_pct(c['target'])}, {'met' if c['meets'] else 'not met'}"))
    figures.append(Figure("Devices past their PM date today", totals["past"],
                          hint=" · ".join(f"{c['label']} {c['past']}" for c in classes) + f" ({period.as_of_today})"))

    gaps = _no_reason_gaps(late, today)
    gaps += [_device_gap(d) for d in past]
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

    class_rows = [[c["label"], c["due"], c["on_time"], c["late"], c["rate"], Decimal(str(c["target"])), "Yes" if c["meets"] else "No", c["past"]]
                  for c in classes]
    move_rows = [[m["number"], tags.get(m["asset_id"], ""), m["old"], m["new"], m["on"], m["by"]] for m in moves]
    past_rows = [[d["tag"], f"{d['make']} {d['model']}", RiskClass(d["rc"]).label, AssetStatus(d["status"]).label, d["next_pm_on"], d["days"],
                  d["open_pm"]] for d in past]

    return Section(
        key=KEY, title=TITLE,
        topic="Scheduled maintenance done on time, by risk class, against the facility's targets; and every PM and device behind a miss.",
        covers=f"{period.label}; devices past their PM date {period.as_of_today}",
        figures=figures,
        gaps=gaps,
        tables=[
            Table(key="by_class", title="PM completion by risk class",
                  columns=["Risk class", "PMs due", "On time", "Not on time", "On time %", "Target %", "Target met", "Devices past their PM date today"],
                  rows=lambda: iter(class_rows), count=len(class_rows)),
            Table(key="not_on_time", title="PMs not on time",
                  columns=["Work order", "Device", "Model", "Risk class", "Due", "Completed on", "Days late", "Done later on", "Done on work order",
                           "Why late", "Reason recorded on"],
                  rows=lambda: _late_rows(late, today), count=totals["late"], links={0: WORK_ORDER, 1: DEVICE, 8: WORK_ORDER},
                  empty="Every PM due in this period was completed on time."),
            Table(key="due_moved", title="Due dates moved after they had passed",
                  columns=["Work order", "Device", "Due date before", "Moved to", "Moved on", "Moved by"],
                  rows=lambda: iter(move_rows), count=len(move_rows), links={0: WORK_ORDER, 1: DEVICE}),
            Table(key="failed", title="Failed PMs",
                  columns=["Work order", "Device", "Completed on", "Repair", "Repair status", "Repair completed on"],
                  rows=lambda: _failed_rows(failed), count=failed_count, links={0: WORK_ORDER, 1: DEVICE, 3: WORK_ORDER}),
            Table(key="past_pm_date", title="Life-support and high-risk devices past their PM date today",
                  columns=["Device", "Model", "Risk class", "Status", "Next PM", "Days past", "Open PM work order"],
                  rows=lambda: iter(past_rows), count=len(past_rows), links={0: DEVICE, 6: WORK_ORDER},
                  empty="None today."),
        ],
        notes=[
            "On time: a PM is on time when completed on or before its due date, as on the Overview. No grace period is added.",
            "PMs counted: those due in the period, as the Overview counts them for the same days (a PM due on the period's last day counts "
            "once it is completed).",
            "Risk class is each device model's class today, not the class it had when the PM was due.",
            "PMs imported from the previous system count in the rates. Their reason reads “Imported from the previous system”, and they "
            "are never listed as missing a reason.",
            "A PM cancelled on a device that went into retirement on or before the PM's due date is not counted. One cancelled on a device "
            "retired later still counts as not on time.",
            "A PM whose due date was moved counts against its current due date. Moves made after the old due date had passed are listed.",
            "Days late: to the PM's completion, or to today when it was never completed (cancelled, or still open). Done later on: the first PM "
            "completed on the device on or after this PM's due date, on any work order, so a cancelled PM shows when the device's PM was done.",
            "Devices past their PM date today: active devices whose next PM date is before today, or that are marked missing, as the PM "
            "compliance report counts them. This counts devices now, not PMs in the period.",
            "On-time shares are cut to one decimal, never rounded up, so a class that missed its target never shows reaching it.",
        ],
    )
