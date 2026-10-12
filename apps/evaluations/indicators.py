"""
The program evaluation's figures (slice 32): the indicators a committee reads for one 12-month period, each under a stable key (never
renamed: last period's value is matched by it), with its numerator and denominator, its value and display, its target as the facility's
Settings stood at the period's end, and Met / Not met against that target. Met is the facility's own target, never compliance and never
an accreditor's requirement.

`snapshot()` counts every indicator whoever asks (apps.evaluations.services stores it whole in an EvaluationSnapshot, and refuses a
refresh to anyone who could not see all of it); `cut()` removes, at read time, what one reader may not see. Each stored indicator keeps
its own label, unit, group, needs, and rule in words, so an approved evaluation reads the same for good whatever later code renames.

One definition: the figures reuse the survey binder's lower-level functions where it has them (apps.reports.survey: the incoming
inspection evidence, the repairs' outcomes, the recall matches, the incidents' clock, the work done without a credential, the vendor
jobs' evidence) and apps.pm.services' PM rule (pm_due_queryset: due in the period, counted once completed or once its window closed by
the day the figures are taken). The PM figures are judged by the PM windows in force at the period's end (FacilitySettings history,
the pattern of the binder's program.window_changes), and the targets are those in force then; a change since the period ended is listed
(settings.changed_since). Risk class and whether a model is on AEM are the model's on the day the figures are taken, as the binder reads
them (a model is on AEM when its PM interval in force differs from the manufacturer's: the binder's rule).

Basis: "period" counts what happened in the period; "taken" is a state on the day the figures were taken (devices in use, devices past
their PM window, PMs still inside their window, open Class I recalls, incident decisions pending, models on AEM), never read as the
position at the period's end. The previous period's column, counted from Cadence's records when no approved evaluation precedes this
one, counts the "period" indicators only.

Read only, a fixed number of queries whatever the facility's size. Never text a requester typed; incidents as counts only.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from statistics import median

from django.db.models import BooleanField, Case, Count, DecimalField, F, Max, Min, Q, Sum, Value, When
from django.db.models.functions import ExtractMonth, ExtractYear, Greatest
from django.utils import timezone

from apps.accounts.models import Level, Module
from apps.core.days import local_day
from apps.core.expressions import DayNumber
from apps.core.history import _rows
from apps.credentials.models import Technician
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, Ownership, RiskClass
from apps.equipment.services import OWNED
from apps.facility import services as fs
from apps.facility.models import POLICY, FacilitySettings
from apps.incidents import services as inc
from apps.incidents.models import Status as IncidentStatus
from apps.pm import aem as pm_aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.pm.services import RETIRED_AND_CANCELLED, pm_due_queryset
from apps.pm.windows import HIGH_CLASSES, WORK_ORDER_RISK, Window, Windows
from apps.pm.windows import windows as windows_of
from apps.recalls.services import DONE_STATUSES as RECALL_DONE
from apps.reports import survey
from apps.reports.permissions import SURVEY_NEEDS
from apps.reports.survey import incidents as binder_incidents
from apps.reports.survey import inspections as binder_inspections
from apps.reports.survey import maintenance as binder_maintenance
from apps.reports.survey import recalls as binder_recalls
from apps.reports.survey import repairs as binder_repairs
from apps.reports.survey import staff as binder_staff
from apps.reports.survey import vendors as binder_vendors
from apps.vendors.models import EngineerTraining, VendorAttestation, VendorEngineer
from apps.vendors.rules import NONE as NOTHING_ON_FILE
from apps.workorders.inspections import uses_before
from apps.workorders.models import LABOR_AMOUNT, PART_AMOUNT, LaborLine, LateReason, PartLine, PmResult, RepairCheck, Source, WorkOrder, WoStatus, WoType

from . import permissions as perms

SCHEMA = 1
PCT, COUNT, DAYS, MONEY = "pct", "count", "days", "money"  # units
HIGHER, LOWER, NEUTRAL = "higher", "lower", "none"  # which way is better (the previous column's arrow; a target's sense)
PERIOD, TAKEN = "period", "taken"  # basis: what happened in the period, or a state on the day the figures were taken
# Where a target comes from (Settings as they stood at the period's end): life support and high risk, and medium and low on the
# manufacturer's interval, at 100%; medium and low on AEM at the facility's PM target; MTTR at its target; repair spend at 12 times the
# monthly repair budget.
FULL, PM_TARGET, MTTR_TARGET, BUDGET = "full", "target_pm_pct", "target_mttr_days", "repair_budget"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
WITHIN_DAYS = 30  # "repairs completed within 30 days"
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)

GROUPS = (
    ("scope", "Scope of the program"),
    ("pm", "Scheduled maintenance"),
    ("inspections", "Incoming inspections"),
    ("repairs", "Repairs"),
    ("aem", "AEM program"),
    ("recalls", "Recalls"),
    ("incidents", "Device incidents"),
    ("staff", "Staff and vendors"),
    ("cost", "Cost"),
)
GROUP_LABELS = dict(GROUPS)


@dataclass(frozen=True)
class Indicator:
    key: str  # stable, never renamed
    group: str
    label: str
    unit: str  # PCT, COUNT, DAYS, MONEY
    direction: str = NEUTRAL
    target: str = ""  # FULL, PM_TARGET, MTTR_TARGET, BUDGET, or "" for none
    basis: str = PERIOD
    needs: tuple = ()  # Modules (View) beyond Program evaluation View and Reports View
    rule: str = ""  # the counting rule in plain words
    quarters: bool = False  # also counted by quarter of the period
    empty: str = "None"  # the display when there is nothing to count (a share of nothing)


EQ, WO, PMS, CON = Module.EQUIPMENT, Module.WORKORDERS, Module.PM, Module.CONTRACTS
_FLEET_RULE = "The facility's own devices (owned, leased, or placed) not retired, by their model's risk class, as on the Overview."
_PM_RULE = ("PMs due in the period, counted once completed or once their window closed; on time when completed within the PM window in "
            "force at the period's end. Risk class and AEM are the model's on the day the figures were taken.")

INDICATORS = (
    # Scope of the program
    Indicator("devices_ours", "scope", "Devices in use", COUNT, basis=TAKEN, needs=(EQ,), rule=_FLEET_RULE),
    Indicator("devices_life_support", "scope", "Life-support devices", COUNT, basis=TAKEN, needs=(EQ,), rule=_FLEET_RULE),
    Indicator("devices_high", "scope", "High-risk devices", COUNT, basis=TAKEN, needs=(EQ,), rule=_FLEET_RULE),
    Indicator("devices_medium", "scope", "Medium-risk devices", COUNT, basis=TAKEN, needs=(EQ,), rule=_FLEET_RULE),
    Indicator("devices_low", "scope", "Low-risk devices", COUNT, basis=TAKEN, needs=(EQ,), rule=_FLEET_RULE),
    Indicator("devices_temporary", "scope", "Rentals, vendor loaners, and demo units on site", COUNT, basis=TAKEN, needs=(EQ,),
              rule="On site and maintained by their owners; not counted in the devices in use."),
    Indicator("devices_added_new", "scope", "Devices added new to the facility", COUNT, needs=(EQ,),
              rule="Devices of the facility's own added in Cadence in the period as new to the facility."),
    Indicator("devices_added_existing", "scope", "Devices added already in use here", COUNT, needs=(EQ,),
              rule="Devices of the facility's own added in Cadence in the period as already in use."),
    Indicator("devices_added_imported", "scope", "Devices imported from the previous system", COUNT, needs=(EQ,),
              rule="Devices of the facility's own imported in the period."),
    Indicator("devices_retired", "scope", "Devices retired", COUNT, needs=(EQ,),
              rule="Devices of the facility's own that went into retirement in the period, from their history."),
    Indicator("work_imported", "scope", "Work imported from the previous system", PCT, needs=(WO,),
              rule="Of the work orders completed in the period, the share imported: imported work carries no late reasons, inspection "
                   "results, or checks after repair."),
    # Scheduled maintenance
    Indicator("pm_high", "pm", "PMs on time, life support and high risk", PCT, HIGHER, FULL, needs=(PMS, WO), rule=_PM_RULE, quarters=True,
              empty="None due"),
    Indicator("pm_oem", "pm", "PMs on time, medium and low risk on the manufacturer's interval", PCT, HIGHER, FULL, needs=(PMS, WO),
              rule=_PM_RULE + " Equipment not on AEM is kept to the manufacturer's schedule.", quarters=True, empty="None due"),
    Indicator("pm_aem", "pm", "PMs on time, medium and low risk on AEM", PCT, HIGHER, PM_TARGET, needs=(PMS, WO),
              rule=_PM_RULE + " The target is the facility's PM completion target.", quarters=True, empty="None due"),
    Indicator("pm_pending", "pm", "PMs still inside their window", COUNT, basis=TAKEN, needs=(PMS, WO),
              rule="Due in the period, not done, with their window still open on the day the figures were taken: not counted yet."),
    Indicator("pm_not_located", "pm", "PMs missed: device could not be located", COUNT, LOWER, needs=(PMS, WO),
              rule="Of the PMs not on time, those whose reason recorded is that the device could not be located."),
    Indicator("devices_past_window", "pm", "Devices past their PM window", COUNT, LOWER, basis=TAKEN, needs=(PMS, EQ),
              rule="Devices in use whose next PM's window had closed, or marked missing, on the day the figures were taken."),
    # Incoming inspections
    Indicator("inspections_new", "inspections", "New devices added", COUNT, needs=(EQ, WO),
              rule="Every device added in the period as new to the facility, rentals and loaners included."),
    Indicator("inspections_before_use", "inspections", "Inspected before first use", PCT, HIGHER, needs=(EQ, WO),
              rule="Of the new devices that went into use, those whose incoming inspection passed on or before their first day in service."),
    Indicator("inspections_used_before", "inspections", "Put in use before their incoming inspection", COUNT, LOWER, needs=(EQ, WO),
              rule="New devices put in use by exception, with a reason, before their incoming inspection."),
    # Repairs
    Indicator("repairs_completed", "repairs", "Repairs completed", COUNT, needs=(WO,),
              rule="Corrective repairs completed or closed in the period, every device."),
    Indicator("repairs_mttr", "repairs", "Mean repair turnaround", DAYS, LOWER, MTTR_TARGET, needs=(WO,),
              rule="Days from opened to completed, averaged over the repairs completed in the period.", empty="No repairs"),
    Indicator("repairs_within_30", "repairs", "Repairs completed within 30 days", PCT, HIGHER, needs=(WO,),
              rule="Of the repairs completed in the period, those completed within 30 days of being opened.", empty="No repairs"),
    Indicator("repairs_checked", "repairs", RepairCheck.PASSED.label, COUNT, needs=(WO,),
              rule="Repairs completed in the period with the checks after repair recorded step by step."),
    Indicator("repairs_vendor_checked", "repairs", RepairCheck.VENDOR_REPORT.label, COUNT, needs=(WO,),
              rule="Repairs completed in the period checked by the vendor, with their service report's number."),
    Indicator("repairs_not_returned", "repairs", RepairCheck.NOT_RETURNED.label, COUNT, needs=(WO,),
              rule="Repairs completed in the period whose device was kept out of use."),
    Indicator("repairs_no_outcome", "repairs", "No checks after repair recorded", COUNT, needs=(WO,),
              rule="Repairs completed before Cadence recorded checks after a repair, or imported."),
    # AEM program (the table per model is in tables.aem)
    Indicator("aem_models", "aem", "Models on AEM", COUNT, basis=TAKEN, needs=(PMS,),
              rule="Device models maintained on an interval other than the manufacturer's on the day the figures were taken."),
    # Recalls
    Indicator("recalls_received", "recalls", "Recalls and alerts received", COUNT, needs=(Module.RECALLS,),
              rule="Notices that matched the facility's devices in the period."),
    Indicator("recalls_closed", "recalls", "Recalls and alerts closed", COUNT, needs=(Module.RECALLS,),
              rule="Of those received in the period, closed or reviewed as not affected."),
    Indicator("recalls_median_days", "recalls", "Median days to close", DAYS, LOWER, needs=(Module.RECALLS,),
              rule="From received to closed, over those received in the period and closed.", empty="None closed"),
    Indicator("recalls_open_class1", "recalls", "Class I recalls open", COUNT, LOWER, basis=TAKEN, needs=(Module.RECALLS,),
              rule="Class I recalls still open on the day the figures were taken, whatever day they were received."),
    # Device incidents: counts only
    Indicator("incidents_recorded", "incidents", "Incidents recorded", COUNT, needs=(Module.INCIDENTS,),
              rule="Device incidents that occurred in the period (one recorded in error is not counted)."),
    Indicator("incidents_reportable", "incidents", "Decided reportable", COUNT, needs=(Module.INCIDENTS,),
              rule="Of the period's incidents, those decided reportable."),
    Indicator("incidents_reports_on_time", "incidents", "Reports sent on time", COUNT, needs=(Module.INCIDENTS,),
              rule="Of those decided reportable, every required report sent by its due date."),
    Indicator("incidents_reports_late", "incidents", "Reports late", COUNT, LOWER, needs=(Module.INCIDENTS,),
              rule="Of those decided reportable, a report sent after its due date or not recorded and past due."),
    Indicator("incidents_pending", "incidents", "Decisions pending", COUNT, LOWER, basis=TAKEN, needs=(Module.INCIDENTS,),
              rule="Incidents of the period or open at its end still waiting for a decision on the day the figures were taken."),
    # Staff and vendors
    Indicator("staff_lapse_work", "staff", "Work completed without a credential for the device", COUNT, LOWER, needs=(Module.USERS, WO),
              rule="In-house work orders completed in the period by a technician not credentialed for the device that day."),
    Indicator("vendor_nothing_on_file", "staff", "Vendor jobs with nothing on file", COUNT, LOWER, needs=(CON, WO),
              rule="Vendor jobs completed in the period with no training or attestation on file covering them."),
    # Cost
    Indicator("repair_spend", "cost", "Repair labor and parts", MONEY, LOWER, BUDGET, needs=(CON, WO),
              rule="Labor and parts on the repairs completed in the period, against 12 times the monthly repair budget in force at the "
                   "period's end. Not the program's whole cost: no salaries, benefits, overhead, or contracts."),
)
BY_KEY = {i.key: i for i in INDICATORS}
CLASS_KEYS = ((RiskClass.LIFE_SUPPORT, "devices_life_support"), (RiskClass.HIGH, "devices_high"), (RiskClass.MEDIUM, "devices_medium"),
              (RiskClass.LOW, "devices_low"))
PM_KEYS = ("pm_high", "pm_oem", "pm_aem")
# The tables and what each needs beyond Program evaluation View and Reports View.
TABLE_NEEDS = {"aem": (PMS, WO)}
TABLE_LABELS = {"aem": "AEM effectiveness by model"}
# What Cadence does not record, said so it is never read as zero.
NOT_RECORDED = (
    "Use errors and no problem found: Cadence records no finding for a repair, so neither is counted here. They are not zero.",
    "Equipment uptime and the cost of service ratio for a year are not counted in this evaluation.",
)
# On AEM, for a PM work order (the binder's rule, DeviceModel.pm_interval_months): an AEM interval on file that differs from the
# manufacturer's, on a model not kept on the manufacturer's schedule. Positive conditions only (never negated). Life support is never on
# AEM; the PM figures read this for medium and low risk only.
_DM = "asset__device_model__"
ON_AEM = (Q(**{f"{_DM}aem_interval_months__isnull": False, f"{_DM}oem_schedule_required": False})
          & (Q(**{f"{_DM}aem_interval_months__lt": F(f"{_DM}oem_pm_interval_months")})
             | Q(**{f"{_DM}aem_interval_months__gt": F(f"{_DM}oem_pm_interval_months")})))
# The Settings fields an evaluation reads at its period's end, and the words for a change made since.
TARGET_FIELDS = ("target_pm_pct", "target_mttr_days", "repair_budget_monthly")
WINDOW_FIELDS = ("pm_window_high", "pm_window_high_days", "pm_window_other", "pm_window_other_days")
POLICY_FIELDS = tuple(field for field, _label, _default in POLICY)
CHANGE_LABELS = {"target_pm_pct": "PM completion target, medium and low risk", "target_mttr_days": "Mean time to repair target",
                 "repair_budget_monthly": "Monthly repair budget", "pm_window_high": "PM on time, life support and high risk",
                 "pm_window_other": "PM on time, medium and low risk",
                 **{field: f"Policy: {label}" for field, label, _default in POLICY}}


# --- words ------------------------------------------------------------------------------------------------------------------------


def day_words(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def period_words(start: date, end: date) -> str:
    return f"{day_words(start)} to {day_words(end)}"


def _number_words(value: Decimal) -> str:
    """97.50 -> "97.5", 100.0 -> "100"."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pct_words(value: Decimal) -> str:
    return f"{_number_words(value)}%"


def _days_words(value: Decimal) -> str:
    return f"{_number_words(value)} day" + ("" if value == 1 else "s")


def _money_words(value: Decimal) -> str:
    return f"${value:,.2f}"


def _quarter_label(first: date, last: date) -> str:
    if first.year == last.year:
        return f"{first:%b} to {last:%b} {last.year}"
    return f"{first:%b} {first.year} to {last:%b} {last.year}"


def needs_words(modules) -> str:
    """"Incidents View and Users and access View"."""
    words = [f"{Module(m).label} View" for m in modules]
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


# --- raw figures ------------------------------------------------------------------------------------------------------------------


@dataclass
class _Raw:
    num: object = None  # int, or a Decimal (money)
    den: int | None = None
    value: object = None  # int, Decimal, or None (nothing to count)
    quarters: list | None = None


def _count(n: int, den: int | None = None) -> _Raw:
    return _Raw(num=n, den=den, value=n)


def _share(num: int, den: int) -> _Raw:
    """A share in percent, cut to one decimal (never rounded up: a share that missed its target never shows reaching it); None when
    there is nothing to share."""
    value = (Decimal(num * 100) / Decimal(den)).quantize(Decimal("0.1"), rounding=ROUND_DOWN) if den else None
    return _Raw(num=num, den=den, value=value)


def _mean_days(total: int, n: int) -> _Raw:
    """A mean of days, to one decimal rounded up (never down: a mean that missed a lower-is-better target never shows reaching it)."""
    value = (Decimal(total) / Decimal(n)).quantize(Decimal("0.1"), rounding=ROUND_CEILING) if n else None
    return _Raw(num=total, den=n, value=value)


def _day_start(day: date) -> datetime:
    """The first moment of the facility's `day` (its time zone is active inside it)."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _scope(p: survey.Period, w: Windows, raw: dict, taken: bool) -> None:
    """The fleet by class, temporary devices, and devices past their PM window (one grouped query, on the day taken); devices added (one)
    and retired (one) in the period; the share of the period's completed work imported (one)."""
    if taken:
        fleet = list(Asset.objects.filter(status__in=Asset.ACTIVE_STATUSES).order_by().values(rc=F("device_model__risk_class"))
                     .annotate(ours=Count("id", filter=OWNED), temporary=Count("id", filter=~OWNED),
                               past=Count("id", filter=OWNED & binder_maintenance._past_window(p.today, w))))
        by_class = {row["rc"]: row["ours"] for row in fleet}
        ours = sum(row["ours"] for row in fleet)
        raw["devices_ours"] = _count(ours)
        for rc, key in CLASS_KEYS:
            raw[key] = _count(by_class.get(rc.value, 0), ours)
        raw["devices_temporary"] = _count(sum(row["temporary"] for row in fleet))
        raw["devices_past_window"] = _count(sum(row["past"] for row in fleet), ours)
    added = dict(Asset.objects.filter(OWNED, created_at__date__gte=p.start, created_at__date__lte=p.end).order_by()
                 .values_list("added_as").annotate(n=Count("id")))
    raw["devices_added_new"] = _count(added.get(AddedAs.NEW, 0))
    raw["devices_added_existing"] = _count(added.get(AddedAs.EXISTING, 0))
    raw["devices_added_imported"] = _count(added.get(AddedAs.IMPORTED, 0))
    raw["devices_retired"] = _count(_retired_in(p))
    work = WorkOrder.objects.filter(status__in=DONE, completed_on__gte=p.start, completed_on__lte=p.end).aggregate(
        total=Count("id"), imported=Count("id", filter=Q(source=Source.IMPORTED)))
    raw["work_imported"] = _share(work["imported"], work["total"])


def _retired_in(p: survey.Period) -> int:
    """Devices of the facility's own that went into retirement on a day of the period (a temporary device returned to its owner is
    not retired), from their history: one read of the history of the devices with a retired row stamped in the period."""
    history = Asset.history.model
    rows = _rows(history)
    touched = rows.filter(status=AssetStatus.RETIRED, history_date__gte=_day_start(p.start),
                          history_date__lt=_day_start(p.end + timedelta(days=1))).values("id")
    found, previous = set(), {}
    for pk, status, ownership, when in (rows.filter(id__in=touched).order_by("id", "history_date", "history_id")
                                        .values_list("id", "status", "ownership", "history_date")):
        if (status == AssetStatus.RETIRED and previous.get(pk) != AssetStatus.RETIRED and ownership == Ownership.OWNED
                and p.start <= local_day(when) <= p.end):
            found.add(pk)
        previous[pk] = status
    return len(found)


def _pm_group(rc: str, on_aem: bool) -> str:
    if rc in HIGH_CLASSES:
        return "pm_high"
    return "pm_aem" if on_aem else "pm_oem"


def _quarters(p: survey.Period) -> list[tuple[date, date]]:
    out = []
    for q in range(4):
        first = add_months(p.start, 3 * q)
        out.append((first, add_months(p.start, 3 * q + 3) - timedelta(days=1)))
    return out


def _pm(p: survey.Period, w: Windows, raw: dict, taken: bool) -> date | None:
    """The PM figures: on time by group and quarter, and missed because the device could not be located (one grouped query); with
    `taken`, the PMs still inside their window (one grouped query, under a window other than the due date). Returns the day the figures
    stop being provisional: the latest window end among the PMs still inside their window, or None."""
    due = pm_due_queryset(p.start, p.end, p.today, w=w)
    on_aem = Case(When(ON_AEM, then=Value(True)), default=Value(False), output_field=BooleanField())
    rows = (due.order_by().values(rc=F(WORK_ORDER_RISK), aem=on_aem, y=ExtractYear("due_on"), m=ExtractMonth("due_on"))
            .annotate(n=Count("id"), on_time=Count("id", filter=w.on_time_q()),
                      not_located=Count("id", filter=Q(late_reason=LateReason.NOT_LOCATED) & w.not_on_time_q())))
    first = p.start.year * 12 + p.start.month
    totals = {key: [0, 0] for key in PM_KEYS}
    by_quarter = {key: [[0, 0] for _ in range(4)] for key in PM_KEYS}
    not_located = 0
    for row in rows:
        key = _pm_group(row["rc"], row["aem"])
        q = min(3, max(0, (row["y"] * 12 + row["m"] - first) // 3))
        totals[key][0] += row["on_time"]
        totals[key][1] += row["n"]
        by_quarter[key][q][0] += row["on_time"]
        by_quarter[key][q][1] += row["n"]
        not_located += row["not_located"]
    quarters = _quarters(p)
    for key in PM_KEYS:
        r = _share(*totals[key])
        r.quarters = []
        for (q_first, q_last), (on_time, n) in zip(quarters, by_quarter[key]):
            share = _share(on_time, n)
            r.quarters.append({"label": _quarter_label(q_first, q_last), "num": on_time, "den": n, "value": _json(share.value),
                               "display": _pct_words(share.value) if share.value is not None else BY_KEY[key].empty})
        raw[key] = r
    late = sum(n - on_time for on_time, n in totals.values())
    raw["pm_not_located"] = _count(not_located, late)
    if not taken:
        return None
    pending, last_end = 0, None
    if not w.is_default:
        high = Case(When(Q(**{f"{WORK_ORDER_RISK}__in": HIGH_CLASSES}), then=Value(True)), default=Value(False), output_field=BooleanField())
        for row in (WorkOrder.objects.filter(type=WoType.PM, due_on__gte=p.start, due_on__lte=min(p.end, p.today), completed_on__isnull=True)
                    .filter(w.inside_window_q(p.today)).exclude(RETIRED_AND_CANCELLED).order_by()
                    .values(high=high).annotate(n=Count("id"), last=Max("due_on"))):
            pending += row["n"]
            end = (w.high if row["high"] else w.other).end(row["last"])
            last_end = end if last_end is None or end > last_end else last_end
    raw["pm_pending"] = _count(pending)
    return last_end if pending else None


def _inspections(p: survey.Period, raw: dict) -> None:
    """The incoming inspection section's evidence (apps.reports.survey.inspections), counted: the new devices added in the period, those
    that went into use, those inspected before first use, and those put in use before their inspection. Four queries."""
    new = binder_inspections._added_in(p).filter(added_as=AddedAs.NEW)
    devices = list(new.values_list("id", flat=True))
    found = binder_inspections._inspections(new)
    history = binder_inspections._history(new)
    used = uses_before(devices)
    before = after = uninspected = in_use = 0
    for pk in devices:
        _first, in_service = history.get(pk, (None, None))
        done = (found.get(pk) or binder_inspections._Inspections()).done
        if used.get(pk):
            in_use += 1
        elif in_service is None:
            continue  # waiting, or returned to the vendor: never went into use
        elif done is None:
            uninspected += 1
        elif done["completed_on"] > in_service:
            after += 1
        else:
            before += 1
    raw["inspections_new"] = _count(len(devices))
    raw["inspections_before_use"] = _share(before, before + after + uninspected + in_use)
    raw["inspections_used_before"] = _count(in_use, len(devices))


def _repairs(p: survey.Period, raw: dict) -> None:
    """Repairs completed in the period (the checks after repair section's set, apps.reports.survey.repairs) by outcome (one grouped
    query), their mean turnaround and the share done within 30 days (one aggregate), and their labor and parts (two aggregates)."""
    done = binder_repairs._completed_in(p)
    found = binder_repairs._figures(done)
    total = sum(n for n, _first, _last in found.values())

    def outcome(key) -> int:
        return found.get(key, (0, None, None))[0]

    turnaround = DayNumber(F("completed_on")) - DayNumber(F("opened_on"))
    agg = done.annotate(t=turnaround).aggregate(days=Sum(Greatest(F("t"), Value(0))), within=Count("id", filter=Q(t__lte=WITHIN_DAYS)))
    raw["repairs_completed"] = _count(total)
    raw["repairs_mttr"] = _mean_days(agg["days"] or 0, total)
    raw["repairs_within_30"] = _share(agg["within"] or 0, total)
    raw["repairs_checked"] = _count(outcome(RepairCheck.PASSED), total)
    raw["repairs_vendor_checked"] = _count(outcome(RepairCheck.VENDOR_REPORT), total)
    raw["repairs_not_returned"] = _count(outcome(RepairCheck.NOT_RETURNED), total)
    raw["repairs_no_outcome"] = _count(outcome(binder_repairs.BEFORE) + outcome(binder_repairs.IMPORTED), total)
    lines = {"work_order__type": WoType.REPAIR, "work_order__status__in": DONE, "work_order__completed_on__gte": p.start,
             "work_order__completed_on__lte": p.end}
    labor = LaborLine.objects.filter(**lines).aggregate(v=Sum(LABOR_AMOUNT, output_field=MONEY_FIELD))["v"] or Decimal(0)
    parts = PartLine.objects.filter(**lines).aggregate(v=Sum(PART_AMOUNT, output_field=MONEY_FIELD))["v"] or Decimal(0)
    spend = (Decimal(labor) + Decimal(parts)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raw["repair_spend"] = _Raw(num=spend, den=None, value=spend)


def _recalls(p: survey.Period, raw: dict, taken: bool) -> None:
    """The recalls section's matches (apps.reports.survey.recalls: one query): received and closed in the period, the median days to
    close, and the Class I still open on the day taken."""
    matches = binder_recalls._matches(p)
    received = [m for m in matches if m.in_period]
    closed = [m for m in received if m.status in RECALL_DONE]
    days = [d for d in (binder_recalls._days_to_close(m) for m in closed) if d is not None]
    raw["recalls_received"] = _count(len(received))
    raw["recalls_closed"] = _count(len(closed), len(received))
    raw["recalls_median_days"] = _Raw(num=None, den=len(days), value=Decimal(str(median(days))) if days else None)
    if taken:
        raw["recalls_open_class1"] = _count(sum(1 for m in matches if m.status in binder_recalls.OPEN
                                               and binder_recalls.is_class_one(m.alert.classification)))


def _incidents(p: survey.Period, raw: dict, taken: bool) -> None:
    """The incidents section's list (apps.reports.survey.incidents: one query), counted: the period's incidents, decided reportable,
    their reports on time and late (the binder's _timing, judged on the day taken); with `taken`, the decisions pending."""
    found = binder_incidents._incidents(p)
    listed = [i for i in found if i.status != IncidentStatus.IN_ERROR]
    occurred = [i for i in listed if p.start <= i.occurred_on <= p.end]
    reportable = [i for i in occurred if i.reportable is True]
    timing = [binder_incidents._timing(i, p.today) for i in reportable]
    raw["incidents_recorded"] = _count(len(occurred))
    raw["incidents_reportable"] = _count(len(reportable), len(occurred))
    raw["incidents_reports_on_time"] = _count(timing.count("on_time"), len(reportable))
    raw["incidents_reports_late"] = _count(timing.count("late"), len(reportable))
    if taken:
        raw["incidents_pending"] = _count(sum(1 for i in listed if i.status == IncidentStatus.OPEN and inc.needs_decision(i)))


def _staff(p: survey.Period, raw: dict) -> None:
    """The technician qualifications section's work done without a credential (apps.reports.survey.staff), by work order."""
    technicians = {t.id: t for t in Technician.objects.all()}
    rows, counts = binder_staff._unqualified_work(p, technicians)
    raw["staff_lapse_work"] = _count(len({r["number"] for r in rows}), counts["checked"])


def _vendors(p: survey.Period, raw: dict) -> None:
    """The vendor section's jobs and their evidence (apps.reports.survey.vendors): jobs with nothing on file, of those judged."""
    engineers = {e.pk: e for e in VendorEngineer.objects.all()}
    jobs = binder_vendors._jobs(p, engineers)
    trainings_of = defaultdict(list)
    for t in EngineerTraining.objects.all():
        trainings_of[t.engineer_id].append(t)
    binder_vendors._judge(jobs, trainings_of, list(VendorAttestation.objects.order_by("-valid_through", "reference")))
    judged = [j for j in jobs if j.evidence is not None]
    raw["vendor_nothing_on_file"] = _count(sum(1 for j in judged if j.evidence.kind == NOTHING_ON_FILE), len(judged))


def _aem(p: survey.Period, raw: dict) -> list[dict]:
    """The models on AEM on the day taken (the binder's rule) and, per model, the year's figures: its devices in use, device-years in the
    period (each device of ours from its install date, or the day it was kept or added, to the day it was retired), corrective repairs
    completed in the period and per device-year, failed PMs, and the rate in the evidence the committee approved. Our devices only
    (apps.pm.aem.while_ours). At most five queries."""
    ours = Q(assets__status__in=Asset.ACTIVE_STATUSES, assets__ownership=Ownership.OWNED)
    models = [dm for dm in DeviceModel.objects.annotate(active=Count("assets", filter=ours)).order_by("manufacturer", "model")
              if dm.pm_interval_months != dm.oem_pm_interval_months]
    raw["aem_models"] = _count(len(models))
    if not models:
        return []
    ids = [dm.pk for dm in models]
    devices = list(Asset.objects.filter(device_model_id__in=ids, ownership=Ownership.OWNED)
                   .values("id", "device_model_id", "status", "installed_on", "kept_on", "created_at", "updated_at"))
    retired_ids = [d["id"] for d in devices if d["status"] == AssetStatus.RETIRED]
    retired = pm_aem._retired_on(retired_ids) if retired_ids else {}
    days: dict = defaultdict(int)
    for d in devices:
        start = d["kept_on"] or d["installed_on"] or local_day(d["created_at"])
        end = p.end
        if d["status"] == AssetStatus.RETIRED:
            end = min(end, retired.get(d["id"]) or local_day(d["updated_at"]))
        days[d["device_model_id"]] += max(0, (end - max(start, p.start)).days + 1)
    in_period = {"completed_on__gte": p.start, "completed_on__lte": p.end}
    counts = {row["asset__device_model"]: row for row in
              WorkOrder.objects.filter(pm_aem.while_ours("completed_on"), asset__device_model__in=ids).order_by().values("asset__device_model")
              .annotate(repairs=Count("id", filter=Q(type=WoType.REPAIR, status__in=DONE, **in_period)),
                        failed=Count("id", filter=Q(type=WoType.PM, pm_result=PmResult.FAIL, **in_period)))}
    approved = {d.device_model_id: d for d in AemDecision.objects.filter(status=AemStatus.APPROVED, device_model_id__in=ids)}
    rows = []
    for dm in models:
        years = round(days[dm.pk] / 365.25, 2)
        repairs = counts.get(dm.pk, {}).get("repairs", 0)
        decision = approved.get(dm.pk)
        evidence = (decision.evidence or {}) if decision is not None else {}
        rate = evidence.get("repairs_per_device_year")
        rows.append({"model": f"{dm.manufacturer} {dm.model}", "risk_class": RiskClass(dm.risk_class).label,
                     "oem_months": dm.oem_pm_interval_months, "aem_months": dm.pm_interval_months, "devices": dm.active,
                     "device_years": years, "repairs": repairs, "per_device_year": round(repairs / years, 2) if years else None,
                     "failed_pms": counts.get(dm.pk, {}).get("failed", 0),
                     "approved_on": decision.decided_on.isoformat() if decision is not None and decision.decided_on else None,
                     "evidence_per_device_year": rate if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None})
    return rows


def _measure(p: survey.Period, w: Windows, *, taken: bool) -> tuple[dict, date | None, list]:
    """{key: _Raw} for the period (with `taken`, the "taken" indicators and the AEM table too), the provisional day, and the AEM rows."""
    raw: dict = {}
    _scope(p, w, raw, taken)
    provisional = _pm(p, w, raw, taken)
    _inspections(p, raw)
    _repairs(p, raw)
    _recalls(p, raw, taken)
    _incidents(p, raw, taken)
    _staff(p, raw)
    _vendors(p, raw)
    aem_rows = _aem(p, raw) if taken else []
    return raw, provisional, aem_rows


def binder_gaps(p: survey.Period) -> dict:
    """{section key: {gap, finding, check}}: the survey binder's items for the period, every section, counted (the appendix)."""
    return {key: survey.build_section(key, p, None).counts() for key in survey.section_keys()}


def records_begin() -> date | None:
    """The day Cadence's records begin: the earliest work order opened, imports included."""
    return WorkOrder.objects.aggregate(first=Min("opened_on"))["first"]


# --- Settings at the period's end -------------------------------------------------------------------------------------------------


def _settings_versions() -> list:
    """This facility's Settings as each save left them, oldest first: [(the facility's day, the historical row)]. One query."""
    history = FacilitySettings.history.model
    fields = (*TARGET_FIELDS, *WINDOW_FIELDS, *POLICY_FIELDS)
    return [(local_day(rec.history_date), rec) for rec in
            _rows(history).order_by("history_date", "history_id").only("history_id", "history_date", *fields)]


def _settings_at(versions: list, day: date):
    """Settings as they stood at the end of `day`: the last version saved on or before it; Cadence's defaults when the first save came
    later; the row as it is (or the defaults) when the facility's Settings have no history at all."""
    if not versions:
        return fs.get_settings()
    found = None
    for saved_on, rec in versions:
        if saved_on > day:
            break
        found = rec
    return found if found is not None else FacilitySettings()


def _value_words(field: str, s) -> str:
    if field in ("pm_window_high", "pm_window_other"):
        return Window(getattr(s, field), getattr(s, f"{field}_days")).describe()
    value = getattr(s, field)
    if field == "target_pm_pct":
        return _pct_words(Decimal(str(value)))
    if field == "target_mttr_days":
        return _days_words(Decimal(str(value)))
    if field == "repair_budget_monthly":
        return _money_words(Decimal(str(value))) if value is not None else "None"
    return value or ""


def _changed_since(versions: list, day: date, s) -> list[dict]:
    """Every change to the targets, the PM windows, or the policy lines saved after `day`, oldest first, from the Settings in force then."""
    tracked = (*TARGET_FIELDS, "pm_window_high", "pm_window_other", *POLICY_FIELDS)
    before = {field: _value_words(field, s) for field in tracked}
    out = []
    for saved_on, rec in versions:
        if saved_on <= day:
            continue
        for field in tracked:
            words = _value_words(field, rec)
            if words != before[field]:
                out.append({"on": saved_on.isoformat(), "field": field, "what": CHANGE_LABELS[field], "before": before[field], "after": words})
                before[field] = words
    return out


def _window_data(window: Window) -> dict:
    return {"kind": window.kind, "days": window.days, "words": window.describe()}


def _settings_data(versions: list, day: date, s) -> dict:
    w = windows_of(s)
    budget = s.repair_budget_monthly
    return {
        "as_of": day.isoformat(),
        "targets": {"pm_high": 100.0, "pm_other": float(s.target_pm_pct), "mttr_days": float(s.target_mttr_days),
                    "repair_budget_monthly": float(budget) if budget is not None else None},
        "pm_windows": {"high": _window_data(w.high), "other": _window_data(w.other)},
        "policy": [{"field": field, "label": label, "text": getattr(s, field)} for field, label, _default in POLICY],
        "changed_since": _changed_since(versions, day, s),
    }


# --- judging and storing ----------------------------------------------------------------------------------------------------------


def _target(source: str, s) -> Decimal | None:
    if source == FULL:
        return Decimal(100)
    if source == PM_TARGET:
        return Decimal(str(s.target_pm_pct))
    if source == MTTR_TARGET:
        return Decimal(str(s.target_mttr_days))
    if source == BUDGET:
        return (Decimal(str(s.repair_budget_monthly)) * 12).quantize(Decimal("0.01")) if s.repair_budget_monthly is not None else None
    return None


def _met(ind: Indicator, r: _Raw, target: Decimal | None) -> bool | None:
    """Met against the facility's own target, exactly (19 of 20 is 95% and meets 95%); None with no target or nothing counted."""
    if target is None or r.value is None:
        return None
    if ind.unit == PCT:
        reached = Decimal(r.num * 100) >= target * r.den
        return reached if ind.direction == HIGHER else Decimal(r.num * 100) <= target * r.den
    if ind.unit == DAYS and r.num is not None and r.den:
        return Decimal(r.num) <= target * r.den if ind.direction == LOWER else Decimal(r.num) >= target * r.den
    return r.value <= target if ind.direction == LOWER else r.value >= target


def _display(ind: Indicator, value) -> str:
    if value is None:
        return ind.empty
    if ind.unit == PCT:
        return _pct_words(value)
    if ind.unit == DAYS:
        return _days_words(value)
    if ind.unit == MONEY:
        return _money_words(value)
    return f"{value:,}"


def _target_words(ind: Indicator, target: Decimal | None) -> str:
    if target is None:
        return ""
    return {PCT: _pct_words, DAYS: _days_words, MONEY: _money_words}.get(ind.unit, _number_words)(target)


def _detail(ind: Indicator, r: _Raw) -> str:
    """"39 of 40" for a share or a count out of something; "" otherwise (and for a share of nothing)."""
    if ind.unit in (PCT, COUNT) and r.num is not None and r.den:
        return f"{r.num:,} of {r.den:,}"
    return ""


def _json(value):
    """A figure as JSON: Decimal to float, everything else as it is."""
    return float(value) if isinstance(value, Decimal) else value


def _judge(ind: Indicator, r: _Raw, s) -> dict:
    target = _target(ind.target, s)
    out = {"key": ind.key, "group": ind.group, "label": ind.label, "unit": ind.unit, "direction": ind.direction, "basis": ind.basis,
           "needs": [str(m) for m in ind.needs], "rule": ind.rule, "value": _json(r.value), "num": _json(r.num), "den": r.den,
           "display": _display(ind, r.value), "detail": _detail(ind, r), "target": _json(target), "target_display": _target_words(ind, target),
           "met": _met(ind, r, target)}
    if ind.quarters:
        out["quarters"] = r.quarters or []
    return out


def _value_of(ind: Indicator, r: _Raw) -> dict:
    return {"display": _display(ind, r.value), "num": _json(r.num), "den": r.den, "value": _json(r.value)}


def _previous(period_start: date, today: date, versions: list, previous, begin: date | None) -> dict:
    """The previous period's values by key: from the previous approved evaluation's snapshot, else counted now from Cadence's records
    for the 12 months before ("period" indicators only), or none when the records begin after those months end."""
    if previous is not None and previous.snapshot_id:
        data = previous.snapshot.data or {}
        values = {i["key"]: {"display": i.get("display", ""), "num": i.get("num"), "den": i.get("den"), "value": i.get("value")}
                  for i in data.get("indicators", []) if isinstance(i, dict) and i.get("key") in BY_KEY}
        return {"source": "approved", "number": previous.number, "version": previous.version, "label": previous.label,
                "period": {"start": previous.period_start.isoformat(), "end": previous.period_end.isoformat()},
                "approved_on": previous.approved_on.isoformat() if previous.approved_on else None,
                "taken_on": data.get("taken_on"), "values": values}
    start, end = add_months(period_start, -12), period_start - timedelta(days=1)
    out = {"source": "counted", "counted_on": today.isoformat(), "period": {"start": start.isoformat(), "end": end.isoformat()},
           "values": {}}
    if begin is None or begin > end:
        return out
    s = _settings_at(versions, end)
    raw, _provisional, _aem_rows = _measure(survey.Period(start, end, today), windows_of(s), taken=False)
    out["values"] = {ind.key: _value_of(ind, raw[ind.key]) for ind in INDICATORS if ind.basis == PERIOD and ind.key in raw}
    return out


def snapshot(period_start: date, period_end: date, *, today: date, previous=None) -> dict:
    """Every figure of the evaluation of period_start..period_end, as counted on `today` (the facility's, read once by the caller),
    JSON-ready, for an EvaluationSnapshot. `previous`: the previous approved evaluation (services.previous_approved), whose snapshot
    gives the previous column; None counts the 12 months before from Cadence's records. Call it inside the facility."""
    versions = _settings_versions()
    s = _settings_at(versions, period_end)
    w = windows_of(s)
    p = survey.Period(period_start, period_end, today)
    raw, provisional, aem_rows = _measure(p, w, taken=True)
    begin = records_begin()
    return {
        "schema": SCHEMA,
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "taken_on": today.isoformat(),
        "settings": _settings_data(versions, period_end, s),
        "records_begin": begin.isoformat() if begin else None,
        "provisional_until": provisional.isoformat() if provisional else None,
        "indicators": [_judge(ind, raw[ind.key], s) for ind in INDICATORS],
        "tables": {"aem": aem_rows},
        "binder_gaps": binder_gaps(p),
        "not_recorded": list(NOT_RECORDED),
        "previous": _previous(period_start, today, versions, previous, begin),
    }


# --- who sees what ----------------------------------------------------------------------------------------------------------------

# Every area the stored figures cover: Reports View, each indicator's needs, the tables', and the binder sections' (binder_gaps holds
# every section). A refresh needs all of them, so the snapshot never depends on who pressed it.
def _all_needs() -> list[str]:
    needed = {Module.REPORTS.value}
    for ind in INDICATORS:
        needed.update(str(m) for m in ind.needs)
    for needs in TABLE_NEEDS.values():
        needed.update(str(m) for m in needs)
    for pairs in SURVEY_NEEDS.values():
        needed.update(str(m) for m, _level in pairs)
    return [m for m in Module.values if m in needed]


def _viewable(user) -> set:
    """The modules `user` has View on, read once (Role.levels: one query)."""
    if user is None:
        return set()
    if user.is_superuser:
        return set(Module.values)
    if not user.role_id:
        return set()
    return {m for m, level in user.role.levels().items() if level >= Level.VIEW}


def missing_for(user) -> list[str]:
    """The modules (values, in Module order) whose View `user` lacks among those the figures cover: [] when they may take them."""
    seen = _viewable(user)
    return [m for m in _all_needs() if m not in seen]


def can_refresh(user) -> bool:
    """Take the figures (a draft's, or a new draft's): Program evaluation Edit and View on every area the figures cover."""
    return perms.can_edit(user) and not missing_for(user)


MET_WORDS = {True: "Met", False: "Not met"}  # against the facility's own target; never "compliant"


def met_words(met) -> str:
    """A stored indicator's "met" in words: "Met", "Not met", or "" (no target, or nothing counted)."""
    return MET_WORDS.get(met, "")


def tally(shown: list) -> tuple[int, int]:
    """(met, judged) over the indicators given: pass a reader's cut, so an "n of m met" counts only what they are shown."""
    judged = [i for i in shown if i.get("met") is not None]
    return sum(1 for i in judged if i["met"] is True), len(judged)


def _left_out_words(label: str, missing: set) -> str:
    return f"{label} (needs {needs_words([m for m in Module.values if m in missing])})"


def cut(data: dict, user) -> dict:
    """The snapshot `data` as `user` may read it: the same shape, with the indicators (and their quarters and previous values), the
    tables, and the binder's sections their role cannot see removed, and "left_out": what was removed, in words. A reader without
    Reports View (or a scoped user) gets no figures at all. Reads each stored indicator's own needs, never the registry's (an old
    snapshot reads as it was stored); one it cannot place is removed."""
    data = dict(data or {})
    previous = dict(data.get("previous") or {})
    if not perms.can_see_figures(user):
        previous["values"] = {}
        data.update(settings={}, records_begin=None, provisional_until=None, indicators=[], tables={}, binder_gaps={}, not_recorded=[],
                    previous=previous, left_out=[f"Every figure (needs {needs_words([Module.REPORTS])})"])
        return data
    seen = _viewable(user)
    kept, removed = [], []
    for ind in data.get("indicators") or []:
        needs = ind.get("needs")
        if needs is None and ind.get("key") in BY_KEY:
            needs = [str(m) for m in BY_KEY[ind["key"]].needs]
        if needs is not None and all(m in seen for m in needs):
            kept.append(ind)
        else:
            removed.append(ind)
    left_out = []
    groups = [g for g, _label in GROUPS] + sorted({i.get("group", "") for i in removed} - set(GROUP_LABELS))
    for group in groups:
        gone = [i for i in removed if i.get("group", "") == group]
        if not gone:
            continue
        missing = {m for i in gone for m in (i.get("needs") or []) if m not in seen}
        label = GROUP_LABELS.get(group, group.title() or "Other figures")
        if len(gone) < sum(1 for i in data.get("indicators") or [] if i.get("group", "") == group):
            label = f"{label}: {', '.join(i.get('label', i.get('key', '')) for i in gone)}"
        left_out.append(_left_out_words(label, missing) if missing else label)
    tables = {}
    for key, rows in (data.get("tables") or {}).items():
        needs = TABLE_NEEDS.get(key)
        if needs is not None and all(str(m) in seen for m in needs):
            tables[key] = rows
        else:
            left_out.append(_left_out_words(TABLE_LABELS.get(key, key), {str(m) for m in (needs or ()) if str(m) not in seen}))
    gaps = {}
    for key, counts in (data.get("binder_gaps") or {}).items():
        pairs = SURVEY_NEEDS.get(key)
        missing = {str(m) for m, _level in (pairs or ()) if str(m) not in seen}
        if pairs is not None and not missing:
            gaps[key] = counts
        else:
            title = survey.section_title(key) or key
            left_out.append(_left_out_words(f"The survey binder's items for {title}", missing))
    keys = {i.get("key") for i in kept}
    previous["values"] = {k: v for k, v in (previous.get("values") or {}).items() if k in keys}
    data.update(indicators=kept, tables=tables, binder_gaps=gaps, previous=previous, left_out=left_out)
    return data
