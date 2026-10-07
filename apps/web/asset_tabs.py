"""
The device drawer's PM schedule and Costs tabs (slice 12; the mock's drawerAsset). views.asset_drawer_context merges what these
return into the drawer's context when that tab is open: pm_tab under "pm" and costs_tab under "costs", so nothing collides with
the drawer's own keys. The drawer decides who sees which tab (PM View, Work orders View); the templates link work orders only for
viewers with Work orders View (the drawer's can_view_wo).

Real data only. Where the mock invents figures (a synced OEM library, past years' costs, a hashed technician), these show what is
on file or say plainly that there is none; the AEM note names the committee's date only when an approval is recorded (slice 14).

The read models below (pm_upcoming, pm_history, cost_by_year, replacement_outlook) are plain ORM reads with no web concerns. They
belong in apps/pm/schedule.py (the first two), apps/reports/cost.py, and apps/reports/fleet.py, and sit here only because this
slice's files are split by owner. Each runs a fixed number of queries however long the device's history is.

Slice 26, incoming inspections: the PM tab's history lists the device's incoming inspections with its PMs (the inspection is its first
maintenance record, with its result), and a device waiting for its inspection has no upcoming PMs ("After its incoming inspection":
the pass starts its PM schedule). incoming_banner is what the Overview says about a device still waiting, for one user.
"""
import re
from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce, ExtractYear
from django.utils import timezone

from apps.equipment import permissions as eq_perms
from apps.equipment.models import AssetStatus, RiskClass
from apps.equipment.services import USE_BEFORE_FROM
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.pm.schedule import DEFAULT_PM_HOURS, planned_technicians
from apps.reports.fleet import REPLACEMENT_MARKUP, _repairs_in_window, replacement_score
from apps.workorders import inspections, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders.models import LABOR_AMOUNT, OPEN_STATUSES, PART_AMOUNT, InspectionResult, LaborLine, PartLine, PmResult, WorkOrder, WoStatus, WoType

from . import charts
from .templatetags.web import money, money_k
from .views_print import checklist_steps

PROJECTED = 2  # PM dates shown after the next one, as the mock does
HISTORY_LIMIT = 20  # PM work orders (and incoming inspections) listed; the Work orders tab and screen have the rest
MAINTENANCE_TYPES = (WoType.PM, WoType.INSPECTION)  # what the PM tab's history lists (slice 26: the incoming inspection with the PMs)
AFTER_INSPECTION = "After its incoming inspection"  # slice 26: a waiting device's next PM, where a date would be
COST_YEARS = 5  # this year and the four before it, or from the install year when that is later
SOON_YEARS = 1.5  # the mock's "within 1.5 years of expected end of life"
REVIEW_SCORE = 50  # a device younger than that scoring this high (repairs, condition) is worth a look; the report ranks by the same score


def _months(n: int) -> str:
    return f"{n} month{'' if n == 1 else 's'}"


def _safe_url(url: str) -> str:
    """Only web links become links. PmProcedure.source_url is a URLField, but nothing stops a bad value arriving outside a form."""
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else ""


# --- PM schedule tab ------------------------------------------------------------------------------------------------------


def strategy_note(device_model, procedure=None, approved=None) -> str:
    """Whether this model follows the OEM interval or an approved AEM one. DeviceModel.pm_interval_months already keeps life support
    on the OEM interval whatever is on file; the note says so when an AEM interval is on file for a life-support model. `approved`
    is the AemDecision in force (slice 14): the note names the committee's date. An interval on file without a recorded approval
    (set before slice 14) reads as before; the model's AEM tab says it has no approval on record."""
    oem, months = device_model.oem_pm_interval_months, device_model.pm_interval_months
    if months != oem:
        direction = "extended" if months > oem else "shortened"
        committee = ""
        if approved is not None and approved.decided_on and approved.interval_months == months:
            committee = f", approved by the Equipment Management Committee on {approved.decided_on:%b} {approved.decided_on.day}, {approved.decided_on.year}"
        return (f"AEM program: the PM interval for this model is {direction} from the OEM's {_months(oem)} to {_months(months)}{committee}. "
                "Life-support devices are excluded from AEM by policy.")
    note = f"Following the OEM schedule: every {_months(oem)}" + (f" per {procedure.code}." if procedure else ".")
    aem = device_model.aem_interval_months
    if aem and aem != oem and device_model.risk_class == RiskClass.LIFE_SUPPORT:
        note += f" An AEM interval of {_months(aem)} is on file, but life-support devices never go on AEM."
    return note


def pm_history(asset) -> list[WorkOrder]:
    """Every PM work order on this device, and (slice 26) every incoming inspection, newest first by its completed date (its opened date
    until it completes), each with `labor_hours` (None when no labor is logged). Two queries; the hours come from the labor lines' own
    tenant-scoped manager."""
    wos = list(WorkOrder.objects.filter(asset=asset, type__in=MAINTENANCE_TYPES).select_related("assigned_to")
               .annotate(on=Coalesce("completed_on", "opened_on")).order_by("-on", "-created_at"))
    hours = {row["work_order"]: row["h"] for row in LaborLine.objects.filter(work_order__asset=asset, work_order__type__in=MAINTENANCE_TYPES)
             .order_by().values("work_order").annotate(h=Sum("hours"))}
    for w in wos:
        w.labor_hours = hours.get(w.pk)
    return wos


def _held(open_pm) -> bool:
    """The open PM work order is on someone's plate: with the vendor, or with an active technician (as pm.schedule.pm_held)."""
    return open_pm is not None and (open_pm.vendor_service or bool(open_pm.assigned_to_id and open_pm.assigned_to.is_active))


def _who(open_pm, suggested) -> dict:
    """Who does the next PM: the open PM work order's vendor or (active) technician, else the schedule's pick. An open PM with a
    deactivated technician is on nobody's plate, as the PM schedule treats it, so the pick is shown."""
    if _held(open_pm) and open_pm.vendor_service:
        return {"kind": "vendor", "name": open_pm.vendor_name or "Vendor"}
    if _held(open_pm):
        return {"kind": "assigned", "name": open_pm.assigned_to.name}
    if suggested is not None:
        return {"kind": "suggested", "name": suggested.name}
    return {"kind": "none", "name": ""}


def pm_upcoming(asset, open_pm, today: date) -> dict:
    """The next PM and the PROJECTED ones after it, and who does them. The projections chain add_months from the next date, as
    completing a PM on its due date would (workorders.services._on_completed); an overdue PM counts as done today, so no projected
    date is already past. The technician is the one the PM schedule plans for that day (pm.schedule.planned_technicians, the same
    as the day panel and the route sheets), or the open PM work order's vendor or active technician."""
    if asset.status == AssetStatus.RETIRED:
        return {"rows": [], "unscheduled": "Retired devices are not scheduled."}
    if asset.awaiting_inspection:  # slice 26: no next PM until its incoming inspection passes
        return {"rows": [], "unscheduled": f"{AFTER_INSPECTION}: its PM schedule starts on the day the inspection passes."}
    if not asset.next_pm_on:
        return {"rows": [], "unscheduled": "No next PM date on file for this device."}
    interval = asset.pm_interval_months
    nxt = asset.next_pm_on
    suggested = None
    if not _held(open_pm):
        # The PM screen's own pick for that day (its day panel, create action, and route sheets), whatever the date
        suggested = planned_technicians(nxt, today)[0].get(asset.id)
    who = _who(open_pm, suggested)
    rows = [{"on": nxt, "next": True, "overdue": nxt < today}]
    d = max(nxt, today)
    for _ in range(PROJECTED):
        d = add_months(d, interval)
        rows.append({"on": d, "next": False, "overdue": False})
    return {"rows": rows, "who": who, "open_pm": open_pm, "interval": _months(interval), "unscheduled": ""}


def _done_by(wo) -> str:
    if wo.vendor_service:
        return wo.vendor_name or "Vendor"
    return wo.assigned_to.name if wo.assigned_to_id else ""


RESULT_CSS = {PmResult.PASS: "", PmResult.PASS_MINOR_REPAIR: "warnc", PmResult.FAIL: "down", InspectionResult.PASSED: "", InspectionResult.FAILED: "down"}
FAILED = (PmResult.FAIL, InspectionResult.FAILED)


def _result(w) -> str:
    """The result recorded on a PM (PmResult) or, slice 26, an incoming inspection (InspectionResult)."""
    return w.inspection_result if w.type == WoType.INSPECTION else w.pm_result


def history_rows(history: list[WorkOrder]) -> list[dict]:
    """The History table's rows. A PM completed with a recorded result (slice 15) shows it as the mock words it ("Pass", "Pass with
    minor repair", "Fail, repair work order opened", with a link to that repair); older ones, and a reopened PM, show the resolution
    or the status as before. Slice 26: an incoming inspection (`inspection`) completed with a result reads "Passed", or "Failed"
    (", re-inspection opened" with a link to it, when the fail opened one), under `repair` as a failed PM's repair. The follow-ups come
    in one query, and only when a listed PM or inspection failed."""
    recorded = {w.pk for w in history if _result(w) and w.status in (WoStatus.COMPLETED, WoStatus.CLOSED)}
    failed = [w.pk for w in history if w.pk in recorded and _result(w) in FAILED]
    follow_ups = {}
    if failed:
        for r in WorkOrder.objects.filter(follow_up_of_id__in=failed).order_by("opened_on", "number"):
            follow_ups.setdefault(r.follow_up_of_id, r)  # the first opened, should a PM ever have two
    rows = []
    for w in history:
        row = {"wo": w, "who": _done_by(w), "hours": w.labor_hours, "inspection": w.type == WoType.INSPECTION}
        if w.pk in recorded and row["inspection"]:
            follow_up = follow_ups.get(w.pk)
            words = w.get_inspection_result_display() + (", re-inspection opened" if follow_up else "")
            rows.append({**row, "result": words, "has_resolution": True, "css": RESULT_CSS.get(w.inspection_result, ""), "repair": follow_up})
        elif w.pk in recorded:
            rows.append({**row, "result": w.get_pm_result_display(), "has_resolution": True, "css": RESULT_CSS.get(w.pm_result, ""),
                         "repair": follow_ups.get(w.pk)})
        else:
            rows.append({**row, "result": w.resolution.strip() or w.get_status_display(), "has_resolution": bool(w.resolution.strip()), "css": "",
                         "repair": None})
    return rows


def pm_tab(asset, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    dm = asset.device_model
    procedure = dm.pm_procedure
    history = pm_history(asset)
    open_pm = min((w for w in history if w.type == WoType.PM and w.status in OPEN_STATUSES), key=lambda w: (w.opened_on, w.number), default=None)
    rows = history_rows(history[:HISTORY_LIMIT])
    incoming = sum(1 for w in history if w.type == WoType.INSPECTION)
    # The committee's date only for a model on AEM (one query then; none for the OEM schedule)
    approved = AemDecision.objects.filter(device_model=dm, status=AemStatus.APPROVED).first() if dm.pm_interval_months != dm.oem_pm_interval_months else None
    return {"pm": {
        "strategy": strategy_note(dm, procedure, approved),
        "procedure": procedure, "steps": checklist_steps(procedure), "source_url": _safe_url(procedure.source_url) if procedure else "",
        "default_hours": DEFAULT_PM_HOURS,
        "upcoming": pm_upcoming(asset, open_pm, today),
        "history": rows, "history_total": len(history), "history_more": len(history) > HISTORY_LIMIT,
        "history_pms": len(history) - incoming, "history_inspections": incoming,
    }}


# --- Overview: the incoming inspection banner (slice 26) -------------------------------------------------------------------------

RETIRED_WAITING = ("Retired while still waiting for its incoming inspection (a new device returned to the vendor). Reinstated, it comes "
                   "back out of service with a new incoming inspection.")


def _parts(line: str, numbers: list[str]) -> list[dict]:
    """`line` split around the work order numbers in it, so the template links them: [{"text", "number"}], number "" for plain text."""
    if not numbers:
        return [{"text": line, "number": ""}]
    parts, at = [], 0
    for m in re.finditer("|".join(re.escape(n) for n in numbers), line):
        if m.start() > at:
            parts.append({"text": line[at:m.start()], "number": ""})
        parts.append({"text": m.group(0), "number": m.group(0)})
        at = m.end()
    if at < len(line):
        parts.append({"text": line[at:], "number": ""})
    return parts


def incoming_banner(asset, user) -> dict | None:
    """What the device drawer's Overview says about a device waiting for its incoming inspection, for `user`; None (and no query) for
    any other device. The sentences are apps.workorders.inspections.banner's ("Waiting for its incoming inspection: WO-..., due ...";
    after a fail, "Failed its incoming inspection on ... (WO-...); re-inspection WO-... open"; in use before it, "In use before its
    incoming inspection: <reason>"), each split into parts so the template links the numbers for a user with Work orders View. A number
    outside a scoped user's share is never in them. A retired device (returned to the vendor) says so instead.
    - tone: "warn" after a fail or while in use before the inspection, else "" (waiting, the usual course).
    - offer_open: none is open: the drawer links New work order with the device and type filled in, for Work orders Edit (not scoped;
      services.create_work_order keeps it to one open inspection).
    - use_before: "Put in use before inspection" (Equipment Approve, not scoped), for a device out of service or missing."""
    if not asset.awaiting_inspection:
        return None
    if asset.status == AssetStatus.RETIRED:
        return {"lines": [_parts(RETIRED_WAITING, [])], "tone": "", "offer_open": False, "use_before": False}
    b = inspections.banner(asset, user)
    numbers = sorted({n for n in (b.open_number, b.failed_number) if n}, key=len, reverse=True)
    scoped = scoping.is_scoped(user)
    return {"lines": [_parts(line, numbers) for line in b.lines], "tone": "warn" if b.failed_on or b.in_use else "",
            "offer_open": b.offer_open and not scoped and user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL),
            "use_before": asset.status in USE_BEFORE_FROM and not scoped and eq_perms.can_use_before_inspection(user)}


# --- Costs tab ------------------------------------------------------------------------------------------------------------


def cost_years(asset, today: date) -> list[int]:
    """This year and the COST_YEARS - 1 before it, starting no earlier than the install year (a device dated ahead shows this year)."""
    first = today.year - (COST_YEARS - 1)
    if asset.installed_on:
        first = max(first, asset.installed_on.year)
    return list(range(min(first, today.year), today.year + 1))


def cost_by_year(asset, years: list[int]) -> dict[int, float]:
    """{year: labor plus parts of this device's work orders completed that year}, for `years` (ascending). Two grouped queries over
    the line tables, so no work order is instantiated and lines outside this tenant are never read."""
    out = {y: 0.0 for y in years}
    if not years:
        return out
    money_field = DecimalField(max_digits=14, decimal_places=2)
    window = {"work_order__asset": asset, "work_order__completed_on__gte": date(years[0], 1, 1), "work_order__completed_on__lte": date(years[-1], 12, 31)}
    for model, expr in ((LaborLine, Sum(LABOR_AMOUNT, output_field=money_field)), (PartLine, Sum(PART_AMOUNT, output_field=money_field))):
        rows = model.objects.filter(**window).annotate(year=ExtractYear("work_order__completed_on")).order_by().values("year").annotate(v=expr)
        for row in rows:
            if row["year"] in out:
                out[row["year"]] += float(row["v"] or Decimal("0"))
    return out


def contract_share(asset) -> float | None:
    """The device's slice of its contract's annual cost (Contract.cost_share_for), or None when it is not under a live contract. A
    retired device is not covered (Contract.covered_assets), so it has no share either."""
    contract = asset.contract if asset.contract_id else None
    if contract is None or contract.is_expired or asset.status == AssetStatus.RETIRED:
        return None
    if not asset.acquisition_cost:
        return None  # nothing to allocate by: the tab says the share cannot be worked out rather than showing $0
    return contract.cost_share_for(asset)


def replacement_outlook(asset, today: date) -> dict:
    """The Replacement planning report's score for this one device (reports.fleet.replacement_score: age against expected life 50%,
    repairs in the last six months 30%, condition 20%), with the same inputs the report uses, as a short note. One query."""
    if asset.status == AssetStatus.RETIRED:
        return {"note": "Retired. Replacement planning scores devices in use only.", "score_pct": None}
    dm = asset.device_model
    repairs_qs, _since = _repairs_in_window(today)
    repairs = repairs_qs.filter(asset=asset).count()
    age = asset.age_years(today)
    if age is not None:
        age = max(age, 0.0)  # a future install date reads as new, as the report does
    life = dm.expected_life_years or 1
    condition = min(5, max(1, asset.condition or 1))
    score_pct = round(replacement_score(age, life, repairs, condition) * 100)
    list_cost = float(dm.list_cost or 0)
    estimate = (list_cost or float(asset.acquisition_cost or 0)) * REPLACEMENT_MARKUP
    est = ""
    if estimate:
        est = f"estimated replacement {money(estimate)}" + ("" if list_cost else " (from its acquisition cost; the model has no list price)")

    if age is None:
        first = f"No install date on file, so its age against its {life}-year expected life is unknown."
        left = None
    else:
        left = life - age
        if age > life:
            first = f"Past its {life}-year expected life by {max(age - life, 0.1):.1f} years."
        elif left <= SOON_YEARS:
            first = f"Within {max(left, 0.1):.1f} years of the end of its {life}-year expected life."
        else:
            first = f"About {left:.1f} years of its {life}-year expected life remaining."
    reps = "no repairs" if repairs == 0 else f"{repairs} repair{'' if repairs == 1 else 's'}"
    second = f"Replacement score {score_pct} of 100, from age, {reps} in the last 6 months, and condition {condition} of 5."
    if age is not None and age > life:
        third = f"A replacement candidate; {est}." if est else "A replacement candidate."
    elif left is not None and left <= SOON_YEARS:
        third = f"Flag it for the next capital cycle; {est}." if est else "Flag it for the next capital cycle."
    elif score_pct >= REVIEW_SCORE:
        third = "Repairs and condition score it high for its age; review its repair history."
    else:
        third = "No replacement action needed."
    return {"note": f"{first} {second} {third}", "score_pct": score_pct, "repairs": repairs, "estimate": estimate}


def costs_tab(asset, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    years = cost_years(asset, today)
    by_year = cost_by_year(asset, years)
    total = sum(by_year.values())
    acquisition = float(asset.acquisition_cost or 0)
    contract = asset.contract if asset.contract_id else None
    chart = charts.hbars([(str(y), by_year[y]) for y in years], fmt=money_k, rh=28) if total else None
    return {"costs": {
        "years": years, "first_year": years[0], "by_year": by_year, "total": total, "chart": chart,
        "pct": total / acquisition * 100 if acquisition else None,
        "contract": contract, "share": contract_share(asset),
        "outlook": replacement_outlook(asset, today),
    }}
