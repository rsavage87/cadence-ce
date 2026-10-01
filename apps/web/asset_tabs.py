"""
The device drawer's PM schedule and Costs tabs (slice 12; the mock's drawerAsset). views.asset_drawer_context merges what these
return into the drawer's context when that tab is open: pm_tab under "pm" and costs_tab under "costs", so nothing collides with
the drawer's own keys. The drawer decides who sees which tab (PM View, Work orders View); the templates link work orders only for
viewers with Work orders View (the drawer's can_view_wo).

Real data only. Where the mock invents figures (an AEM approval story, a synced OEM library, past years' costs, a hashed
technician), these show what is on file or say plainly that there is none.

The read models below (pm_upcoming, pm_history, cost_by_year, replacement_outlook) are plain ORM reads with no web concerns. They
belong in apps/pm/schedule.py (the first two), apps/reports/cost.py, and apps/reports/fleet.py, and sit here only because this
slice's files are split by owner. Each runs a fixed number of queries however long the device's history is.
"""
from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce, ExtractYear

from apps.equipment.models import AssetStatus, RiskClass
from apps.pm.dates import add_months
from apps.pm.schedule import DEFAULT_PM_HOURS, suggestions_for_day
from apps.reports.fleet import REPLACEMENT_MARKUP, _repairs_in_window, replacement_score
from apps.workorders.models import OPEN_STATUSES, LaborLine, PartLine, WorkOrder, WoType

from . import charts
from .templatetags.web import money, money_k
from .views_print import checklist_steps

PROJECTED = 2  # PM dates shown after the next one, as the mock does
HISTORY_LIMIT = 20  # PM work orders listed; the Work orders tab and screen have the rest
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


def strategy_note(device_model, procedure=None) -> str:
    """Whether this model follows the OEM interval or an approved AEM one. DeviceModel.pm_interval_months already keeps life support
    on the OEM interval whatever is on file; the note says so when an AEM interval is on file for a life-support model."""
    oem, months = device_model.oem_pm_interval_months, device_model.pm_interval_months
    if months != oem:
        direction = "extended" if months > oem else "shortened"
        return (f"AEM program: the PM interval for this model is {direction} from the OEM's {_months(oem)} to {_months(months)}. "
                "Life-support devices are excluded from AEM by policy.")
    note = f"Following the OEM schedule: every {_months(oem)}" + (f" per {procedure.code}." if procedure else ".")
    aem = device_model.aem_interval_months
    if aem and aem != oem and device_model.risk_class == RiskClass.LIFE_SUPPORT:
        note += f" An AEM interval of {_months(aem)} is on file, but life-support devices never go on AEM."
    return note


def pm_history(asset) -> list[WorkOrder]:
    """Every PM work order on this device, newest first by its completed date (its opened date until it completes), each with
    `labor_hours` (None when no labor is logged). Two queries; the hours come from the labor lines' own tenant-scoped manager."""
    wos = list(WorkOrder.objects.filter(asset=asset, type=WoType.PM).select_related("assigned_to")
               .annotate(on=Coalesce("completed_on", "opened_on")).order_by("-on", "-created_at"))
    hours = {row["work_order"]: row["h"] for row in
             LaborLine.objects.filter(work_order__asset=asset, work_order__type=WoType.PM).order_by().values("work_order").annotate(h=Sum("hours"))}
    for w in wos:
        w.labor_hours = hours.get(w.pk)
    return wos


def _who(open_pm, suggested) -> dict:
    """Who does the next PM: the open PM work order's vendor or technician, else the schedule's suggestion."""
    if open_pm is not None and open_pm.vendor_service:
        return {"kind": "vendor", "name": open_pm.vendor_name or "Vendor"}
    if open_pm is not None and open_pm.assigned_to_id:
        return {"kind": "assigned", "name": open_pm.assigned_to.name}
    if suggested is not None:
        return {"kind": "suggested", "name": suggested.name}
    return {"kind": "none", "name": ""}


def pm_upcoming(asset, open_pm, today: date) -> dict:
    """The next PM and the PROJECTED ones after it, and who does them. The projections chain add_months from the next date, as
    completing a PM on its due date would (workorders.services._on_completed); an overdue PM counts as done today, so no projected
    date is already past. The technician is the one the PM schedule shows for that day (pm.schedule.suggestions_for_day: the week
    plan inside the next 7 days, else the least-loaded credentialed technician), or the open PM work order's assignee."""
    if asset.status == AssetStatus.RETIRED:
        return {"rows": [], "unscheduled": "Retired devices are not scheduled."}
    if not asset.next_pm_on:
        return {"rows": [], "unscheduled": "No next PM date on file for this device."}
    interval = asset.pm_interval_months
    nxt = asset.next_pm_on
    suggested = None
    if open_pm is None or not (open_pm.vendor_service or open_pm.assigned_to_id):
        suggested = suggestions_for_day(nxt, [asset], today).get(asset.id)
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


def pm_tab(asset, today: date | None = None) -> dict:
    today = today or date.today()
    dm = asset.device_model
    procedure = dm.pm_procedure
    history = pm_history(asset)
    open_pm = min((w for w in history if w.status in OPEN_STATUSES), key=lambda w: (w.opened_on, w.number), default=None)
    rows = [{"wo": w, "who": _done_by(w), "result": w.resolution.strip() or w.get_status_display(), "has_resolution": bool(w.resolution.strip()),
             "hours": w.labor_hours} for w in history[:HISTORY_LIMIT]]
    return {"pm": {
        "strategy": strategy_note(dm, procedure),
        "procedure": procedure, "steps": checklist_steps(procedure), "source_url": _safe_url(procedure.source_url) if procedure else "",
        "default_hours": DEFAULT_PM_HOURS,
        "upcoming": pm_upcoming(asset, open_pm, today),
        "history": rows, "history_total": len(history), "history_more": len(history) > HISTORY_LIMIT,
    }}


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
    for model, expr in ((LaborLine, Sum(F("hours") * F("rate"), output_field=money_field)),
                        (PartLine, Sum(F("quantity") * F("unit_cost"), output_field=money_field))):
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
    today = today or date.today()
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
