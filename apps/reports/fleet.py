"""
Fleet reports (slice 7): PM compliance summary, reliability by model, replacement planning.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.
The math follows the mock's repContent; departures are noted inline. Everything here is read-only and tenant-scoped.

Slice 29, the counting rule (equipment.services.OWNED / WORK_ORDER_OWNED): all three are about the facility's own fleet and CE's
program, so they count our devices and their work orders only. A rental, vendor loaner, or demo unit (its owner maintains it) is never
in a compliance denominator nor overdue when missing, its repairs are no failures of our models, and it is never up for replacement.

Slice 33: reliability by model counts failures by what each repair found (apps.workorders.findings.failure_q) beside every repair.

Slice 36: replacement planning reads the replacement priority rule (apps.capital.priority: support, failures, service cost against the
replacement estimate, age, a confirmed recall, times criticality; points unknown shown, never scored as 0), as the device drawer does.
"""
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from django.db.models import Count, DecimalField, Q, Sum

from apps.capital import priority
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership, RiskClass
from apps.equipment.services import OWNED, WORK_ORDER_OWNED
from apps.facility.fiscal import current_fy, fy_label, start_month
from apps.facility.services import compliance_targets, get_settings
from apps.pm.dates import month_bounds
from apps.pm.windows import ASSET_RISK, Windows, windows
from apps.reports.services import TRAILING_DAYS
from apps.workorders import findings
from apps.workorders.models import LABOR_AMOUNT, PART_AMOUNT, LaborLine, PartLine, WorkOrder, WoStatus, WoType

CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
# Slice 33: the findings that are never device failures (findings.NO_FAULT and OTHER_FAULT): left out of the failures, each counted
LEFT_OUT = tuple(str(v) for v in (*findings.NO_FAULT, *findings.OTHER_FAULT))
MTBF_LIMIT = 12
REPAIR_RATE_FACTOR = 2  # the mock's x2: two 182-day halves make its year, the same base MTBF uses (n * 182 / repairs)
REPLACE_LIMIT = 12


def _active_assets():
    """Our devices in use (slice 29: never a temporary device)."""
    return Asset.objects.filter(OWNED, status__in=Asset.ACTIVE_STATUSES)


def _repairs_in_window(today: date):
    """Repair work orders on our devices opened in the trailing 182 days. A cancelled repair was not a failure, so it is left out."""
    since = today - timedelta(days=TRAILING_DAYS)
    return (WorkOrder.objects.filter(WORK_ORDER_OWNED, type=WoType.REPAIR, opened_on__gte=since, opened_on__lte=today)
            .exclude(status=WoStatus.CANCELLED), since)


# --- PM compliance summary ---------------------------------------------------------------------------------------


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def on_time_words(w: Windows) -> str:
    """When a PM counts as on time, in words for a note (slice 27, from Window.describe): "by the due date", "by the end of the due
    month", or, when the two groups differ, "by the end of the due month for life support and high risk, within 14 days after the due
    date for medium and low risk". The survey binder's notes and the technician report say it the same way."""
    if w.uniform:
        return _lower_first(w.high.describe())
    return f"{_lower_first(w.high.describe())} for life support and high risk, {_lower_first(w.other.describe())} for medium and low risk"


def report_compliance(today: date) -> dict:
    """One row per risk class: active devices, this month's PMs (due, completed, on time), devices overdue now, and compliance
    (the share of active devices whose PM is not past its window today) against the class's target. Two grouped queries and the
    settings row, no per-class work. A device marked missing stays in the active count and is always overdue (the mock's rule: it can
    never be shown compliant), but for a new device waiting for its incoming inspection (slice 26: it has no PM schedule until the
    inspection passes, so no PM to be overdue, missing or not), and this month's PMs are counted on active devices only, so every
    column describes the same fleet: ours (slice 29: a temporary device, maintained by its owner, is in no column, missing or not).

    Slice 27, the PM completion window (apps.pm.windows): a PM is on time when completed within its class's window, and a device is
    overdue now when its next PM's window has closed (apps.pm.services.assets_past_window's rule), so this report and the survey
    binder count compliance as the facility's policy defines it. By the due date unless the facility chose otherwise, which gives
    exactly the figures from before. Under another window the note also says how many devices are past their due date but still
    inside their window ("inside") and how many of this month's PMs are ("pending"): neither is overdue yet."""
    start, end = month_bounds(today.year, today.month)
    s = get_settings()
    w = windows(s)  # read once, with the targets' row
    overdue_q = w.past_window_q(today, date_field="next_pm_on", risk=ASSET_RISK) | Q(status=AssetStatus.MISSING, awaiting_inspection=False)
    device_counts = {"devices": Count("id"), "overdue": Count("id", filter=overdue_q)}
    pm_counts = {"due": Count("id"), "completed": Count("id", filter=Q(completed_on__isnull=False)), "on_time": Count("id", filter=w.on_time_q())}
    if not w.is_default:  # always 0 by the due date: the default's SQL stays as it was
        found = [st for st in Asset.ACTIVE_STATUSES if st != AssetStatus.MISSING]  # a missing device is overdue already
        device_counts["inside"] = Count("id", filter=w.inside_window_q(today, date_field="next_pm_on", risk=ASSET_RISK) & Q(status__in=found))
        pm_counts["pending"] = Count("id", filter=Q(completed_on__isnull=True) & w.inside_window_q(today))
    fleet = {row["device_model__risk_class"]: row for row in
             _active_assets().order_by().values("device_model__risk_class").annotate(**device_counts)}
    pms = {row["asset__device_model__risk_class"]: row for row in
           WorkOrder.objects.filter(WORK_ORDER_OWNED, type=WoType.PM, due_on__gte=start, due_on__lte=end, asset__status__in=Asset.ACTIVE_STATUSES)
           .exclude(status=WoStatus.CANCELLED).order_by().values("asset__device_model__risk_class").annotate(**pm_counts)}
    # Survey targets by risk class: life support and high at 100%, medium and low at the tenant's PM completion target (Settings).
    targets = compliance_targets(s)
    classes = []
    for rc in CLASS_ORDER:
        f, p = fleet.get(rc, {}), pms.get(rc, {})
        devices, overdue = f.get("devices", 0), f.get("overdue", 0)
        pct = (1 - overdue / devices) * 100 if devices else 100.0
        target = targets[rc]
        # Exact: 17 of 250 overdue is 93.2% exactly, which float division makes 93.19999... and would score as a miss.
        meets = Decimal((devices - overdue) * 100) >= Decimal(str(target)) * devices if devices else True
        classes.append({"key": rc.value, "label": rc.label, "devices": devices, "due": p.get("due", 0), "completed": p.get("completed", 0),
                        "on_time": p.get("on_time", 0), "overdue": overdue, "compliance_pct": float(pct), "target_pct": target, "meets": meets,
                        "inside": f.get("inside", 0), "pending": p.get("pending", 0)})
    return {
        "columns": ["Risk class", "Devices", "PMs due this month", "Completed", "On time", "Overdue now", "Current compliance %", "Target %"],
        "rows": [[c["label"], c["devices"], c["due"], c["completed"], c["on_time"], c["overdue"], c["compliance_pct"], c["target_pct"]] for c in classes],
        "classes": classes, "month_label": today.strftime("%B %Y"), "today": today, "policy_target_pct": targets[RiskClass.MEDIUM],
        # The window the figures follow: the note says it in words, and the chart's series read it rather than reading Settings again.
        "w": w, "window": {"is_default": w.is_default, "words": on_time_words(w)},
        "inside": sum(c["inside"] for c in classes), "pending": sum(c["pending"] for c in classes),
    }


# --- Reliability by model ----------------------------------------------------------------------------------------


def report_mtbf(today: date) -> dict:
    """Models with at least one repair opened in the trailing 182 days, ranked by failures per device per year (the mock's x2 of the
    six-month count). MTBF is fleet-level: device-days in the period over failures. Turnaround and cost average the repairs that have
    completed. Six queries whatever the fleet size: repairs, failures, and coverage, and devices, per model, a dates-only pass for
    turnaround, one grouped sum each over labor and part lines for cost, and the models; no work order is instantiated.

    Slice 33, what each repair found (apps.workorders.findings): "Repairs, 6 mo" stays every repair (the work happened); "Failures, 6
    mo" counts those findings.failure_q counts (device faults, repairs with no finding recorded or not stated, and every open repair:
    being worked on, it has no finding yet), and the rate, MTBF, flagged, and the ranking read failures. Use error, damage, an
    accessory, power or room conditions, and no problem found are left out of them, never out of turnaround or cost: a model whose
    repairs were all left out is listed with a rate of 0 and no MTBF. `left_out` counts them by finding and `coverage` says how many
    of the window's completed repairs have a finding, for the note."""
    repairs, since = _repairs_in_window(today)
    failure = findings.failure_q()
    done_q = Q(status__in=DONE)
    per_model = (repairs.order_by().values("asset__device_model")
                 .annotate(n=Count("id"), failures=Count("id", filter=failure), completed=Count("id", filter=done_q),
                           recorded=Count("id", filter=done_q & findings.recorded_q()),
                           **{f"left_{v}": Count("id", filter=Q(repair_finding=v) & done_q) for v in LEFT_OUT}))
    counts, failed, left_out = {}, {}, {v: 0 for v in LEFT_OUT}
    completed = recorded = 0
    for row in per_model:
        counts[row["asset__device_model"]] = row["n"]
        failed[row["asset__device_model"]] = row["failures"]
        completed += row["completed"]
        recorded += row["recorded"]
        for v in LEFT_OUT:
            left_out[v] += row[f"left_{v}"]
    models = []
    if counts:
        in_service = {row["device_model"]: row["n"] for row in
                      _active_assets().filter(device_model__in=counts).order_by().values("device_model").annotate(n=Count("id"))}
        done = {}
        for row in repairs.filter(completed_on__isnull=False).order_by().values("asset__device_model", "opened_on", "completed_on"):
            d = done.setdefault(row["asset__device_model"], {"n": 0, "turnaround": 0, "cost": 0.0})
            d["n"] += 1
            d["turnaround"] += max((row["completed_on"] - row["opened_on"]).days, 0)  # the service refuses completion before opening; clamp anyway
        money = DecimalField(max_digits=14, decimal_places=2)
        # The same repairs as `done` (type, window, not cancelled, completed), reached from the lines so no work order is instantiated.
        line_filter = {"work_order__type": WoType.REPAIR, "work_order__opened_on__gte": since, "work_order__opened_on__lte": today,
                       "work_order__completed_on__isnull": False, "work_order__asset__ownership": Ownership.OWNED}
        for model, expr in ((LaborLine, Sum(LABOR_AMOUNT, output_field=money)), (PartLine, Sum(PART_AMOUNT, output_field=money))):
            rows = (model.objects.filter(**line_filter).exclude(work_order__status=WoStatus.CANCELLED)
                    .order_by().values("work_order__asset__device_model").annotate(v=expr))
            for row in rows:
                if row["work_order__asset__device_model"] in done:  # always true (same filter as `done`); guard the lookup anyway
                    done[row["work_order__asset__device_model"]]["cost"] += float(row["v"] or 0)
        for dm in DeviceModel.objects.filter(pk__in=counts):
            n, reps, fails, d = in_service.get(dm.pk, 0), counts[dm.pk], failed[dm.pk], done.get(dm.pk)
            rate = fails / n * REPAIR_RATE_FACTOR if n else None
            # No failures: no time between them to measure (a model whose repairs were all left out has rate 0 and no MTBF)
            mtbf = n * TRAILING_DAYS / fails if n and fails else None
            models.append({"device_model": dm, "in_service": n, "repairs": reps, "failures": fails, "rate": rate, "mtbf_days": mtbf,
                           "turnaround": d["turnaround"] / d["n"] if d else 0.0, "cost": d["cost"] / d["n"] if d else 0.0,
                           "flagged": rate is not None and rate > 1})
        # Highest failure rate first; models with nothing in service (a repair on a since-retired device) have no rate and go last.
        models.sort(key=lambda m: (m["rate"] is None, -(m["rate"] or 0.0), -m["failures"], -m["repairs"], m["device_model"].manufacturer,
                                   m["device_model"].model))
    # The screen shows the top MTBF_LIMIT; the CSV (`rows`) carries every model with repairs, like the replacement list.
    return {
        "columns": ["Manufacturer", "Model", "Device", "In service", "Repairs, 6 mo", "Failures, 6 mo", "Failures per device per year", "MTBF days",
                    "Avg turnaround days", "Avg repair cost"],
        "rows": [[m["device_model"].manufacturer, m["device_model"].model, m["device_model"].description, m["in_service"], m["repairs"],
                  m["failures"], m["rate"], round(m["mtbf_days"]) if m["mtbf_days"] is not None else None, m["turnaround"], m["cost"]] for m in models],
        "models": models[:MTBF_LIMIT], "total_models": len(models), "since": since,
        # Slice 33: what the failures leave out, by finding (the note names each with its count), and the coverage of the findings
        "left_out": [{"value": v, "label": findings.label(v), "n": left_out[v]} for v in LEFT_OUT if left_out[v]],
        "left_out_total": sum(left_out.values()), "completed": completed, "recorded": recorded, "failure_rule": findings.FAILURE_RULE,
    }


# --- Replacement planning ----------------------------------------------------------------------------------------
# Slice 36: on the replacement priority rule (apps.capital.priority), the one rule the screen, CSV, print, email, API rows, and the
# device drawer read. The mock's score (age 50%, repairs in six months 30%, condition 20%) and its flat 5% markup are gone.

# The first seven columns as before; "Failures, 36 mo" was "Repairs, 6 mo"; the rest appended (each component's points, the flags,
# support, the suggested fiscal year, the estimate's source, and the install date and acquisition cost the devices import reads back),
# and last the rule's version with its weights (priority.version_words: "Score v2: age 30, ..."). A CSV has no header note here (its first
# row is the columns, and it reads back through the devices import, which leaves a column it has no name for unused), so the version
# rides on every row, the same words for every reader.
REPLACE_COLUMNS = ["Asset tag", "Device", "Manufacturer", "Model", "Location", "Age years", "Expected life years", "Failures, 36 mo", "Condition",
                   "Score", "Est. replacement", "Points unknown", "Age points", "Support points", "Failure points", "Service cost points",
                   "Recall points", "Multiplier", "Flags", "Support", "Suggested fiscal year", "Estimate source", "Installed on",
                   "Acquisition cost", "Ranked", "Score version"]
ESTIMATE_SOURCE_WORDS = {priority.ESTIMATE_LIST: "list price", priority.ESTIMATE_ACQUISITION: "acquisition cost"}
OUTLOOK_YEARS = 5  # the current fiscal year and the four after it


def _points(c) -> Decimal | None:
    """A component's points to one decimal (half up), None when unknown: a blank cell, never 0."""
    return priority.points_rounded(c.points)


def replace_row(p) -> list:
    """One device's row: the CSV's, the email attachment's, and the API's (plain values; unknown ones None)."""
    a, dm = p.asset, p.asset.device_model
    return [a.tag, dm.description, dm.manufacturer, dm.model, a.department.name, p.age_years, p.life_years, p.failures, p.condition, p.score,
            p.estimate.amount, p.points_unknown, *(_points(c) for c in p.components), Decimal(repr(p.multiplier)),
            "; ".join(priority.FLAG_WORDS[f] for f in p.flags), p.support.words,
            fy_label(p.suggested_fy) if p.suggested_fy is not None else None, ESTIMATE_SOURCE_WORDS.get(p.estimate.source),
            a.installed_on, a.acquisition_cost,
            "yes" if p.ranked else "no: " + ", ".join(priority.UNRANKED_WORDS[r] for r in p.unranked_reasons), priority.version_words()]


def _fleets(every: list) -> list[dict]:
    """Each model's fleet of scored devices: devices, past their expected life, support (the model's), failures a device-year (the
    devices with enough history: their failures over their years within the window, each floored at a year, as the rule's rate), the
    median ranked score and its range, and the estimated total to replace them all (the devices with an estimate; `unestimated` counts
    the rest). Ranked by median score (a fleet with no ranked device last), then devices, then name."""
    by_model: dict = {}
    for p in every:
        by_model.setdefault(p.asset.device_model_id, []).append(p)
    fleets = []
    for ps in by_model.values():
        dm = ps[0].asset.device_model
        scores = sorted(p.score for p in ps if p.ranked)
        known = [p for p in ps if p.failures is not None]
        years = sum(max(p.window_years, priority.MIN_YEARS) for p in known)
        fleets.append({
            "device_model": dm, "devices": len(ps), "ranked": len(scores), "life_years": ps[0].life_years, "support": ps[0].support,
            "past_life": sum(1 for p in ps if p.age_years is not None and p.age_years > p.life_years),
            "failures_per_device_year": sum(p.failures for p in known) / years if known else None,
            "median_score": median(scores) if scores else None, "min_score": scores[0] if scores else None,
            "max_score": scores[-1] if scores else None,
            "total": sum((p.estimate.amount for p in ps if p.estimate.amount is not None), Decimal("0")),
            "unestimated": sum(1 for p in ps if p.estimate.amount is None),
        })
    fleets.sort(key=lambda f: (f["median_score"] is None, -(f["median_score"] or 0), -f["devices"], f["device_model"].manufacturer,
                               f["device_model"].model))
    return fleets


def _outlook(every: list, today: date, start: int) -> dict:
    """The current fiscal year and the four after it: the devices suggested for each and their estimated total (`unestimated` those
    with no estimate); `later` the devices suggested after them, `not_placed` those with no day to place them by."""
    now = current_fy(today, start)
    years = {fy: {"fy": fy, "label": fy_label(fy), "devices": 0, "total": Decimal("0"), "unestimated": 0} for fy in range(now, now + OUTLOOK_YEARS)}
    later = not_placed = 0
    for p in every:
        if p.suggested_fy is None:
            not_placed += 1
        elif p.suggested_fy in years:
            y = years[p.suggested_fy]
            y["devices"] += 1
            if p.estimate.amount is None:
                y["unestimated"] += 1
            else:
                y["total"] += p.estimate.amount
        else:
            later += 1
    return {"years": list(years.values()), "later": later, "not_placed": not_placed}


def _gaps(every: list) -> list[dict]:
    """What the scores do not know, in GAPS order: the devices each gap touches, and for a model's gap (the life source, support not
    checked) the models too (None for a device's gap). `assets` lists the devices by tag and `device_models` the models by name (None
    for a device's gap), for the screen's links; the counts are their lengths."""
    out = []
    for key in priority.GAPS:
        hit = sorted((p for p in every if key in p.gaps), key=lambda p: p.asset.tag)
        dms = {p.asset.device_model_id: p.asset.device_model for p in hit} if key in (priority.LIFE_SOURCE, priority.SUPPORT_UNCHECKED) else None
        out.append({"key": key, "words": priority.GAP_WORDS[key], "devices": len(hit), "models": None if dms is None else len(dms),
                    "assets": [p.asset for p in hit],
                    "device_models": None if dms is None else sorted(dms.values(), key=lambda dm: (dm.manufacturer, dm.model))})
    return out


def report_replace(today: date) -> dict:
    """Every scored device (apps.capital.priority.scored_assets: ours, in use) on the replacement priority rule. `rows` carries them all,
    ranked then not ranked (the capital request; it reads back through the devices import, the bulk fix for missing install dates);
    `top` the first REPLACE_LIMIT ranked; `fleets`, `outlook`, `gaps`, and `unranked` the report's other sections. The fiscal year's
    start is read once; the rule's queries are a fixed number whatever the fleet's size."""
    start = start_month()
    ranked, unranked = priority.rank(priority.score_assets(priority.scored_assets(), today, start))
    every = ranked + unranked
    top = ranked[:REPLACE_LIMIT]
    return {
        "columns": REPLACE_COLUMNS,
        "rows": [replace_row(p) for p in every],
        "top": top, "scored_count": len(every), "ranked_count": len(ranked),
        "total": sum((p.estimate.amount for p in top), Decimal("0")),  # a ranked device always has an estimate
        "version": priority.version_words(), "fleets": _fleets(every), "outlook": _outlook(every, today, start), "gaps": _gaps(every),
        "unranked": unranked, "start": start, "today": today,
    }
