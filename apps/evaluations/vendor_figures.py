"""
The figures of a contracted service vendor's review (slice 34): what Cadence's own records say about one vendor's work for the facility
over the review's 12-month period. `snapshot()` counts them for apps.evaluations.vendor_reviews, which stores the result whole in a
VendorReviewSnapshot; `cut()` is the one reader's gate, all or nothing (permissions.can_see_vendor_figures: Evaluations View and
Contracts View, Reports View and Work orders View; never a scoped user, so a vendor's account never sees its own figures).

The vendor is one or more keys (apps.vendors.rules.vendor_key of the names the review gives: "Philips Healthcare" and "Philips field
service" are two keys of one review). Its contracts and work orders are found by the spellings stored in Cadence whose key is one of
them, so any letter case or spacing of either name is the vendor; a name written another way is not, unless the review names it too.

Cadence keeps the figures and the reviewer rates: nothing here is rated, compared with a target, or called met; never uptime or response
computed from Cadence's dates (NOT_RECORDED says so in words); never a person's name (no engineer, requester, or user), no device tag,
and no text anyone typed but the vendor's name and its contracts' references.

The snapshot (SCHEMA 1), JSON-ready:
    {"schema": 1, "vendor_keys": [...], "period": {"start", "end"}, "taken_on", "names": [the spellings matched],
     "settings": {"as_of": the period's end, "pm_windows": {"high", "other"} (words), "pm_targets": {"high": 100.0, "other": <%>}},
     "provisional_until": a day or None, "groups": [{"key", "label"}], "figures": [figure, ...], "contracts": [contract, ...],
     "repeats": [pair, ...], "not_recorded": [sentence, ...], "notes": [sentence, ...]}
a figure: {"key" (KEYS: stable, never renamed), "group", "label", "unit" (pct, count, days, money), "basis" (period: what happened in
    it; taken: a state on the day taken), "rule" (in words), "value", "num", "den", "display", "detail", "splits": [{"key", "label",
    "value", "num", "den", "display", "detail"}]}: a share's num of den; a count's den is what it is out of, if anything; days have den
    (how many) and no num; money has no den;
a contract: {"id", "reference", "deleted", "vendor_today", "devices_today", "days", "cost", "versions": [{"reference", "vendor", "type",
    "type_label", "coverage", "coverage_label", "start_on", "end_on", "annual_cost", "from", "through", "days", "cost"}]};
a repeat pair: {"repair", "completed_on", "later", "opened_on", "days", "finding", "finding_label"} (work order numbers, never a tag).

The rules (each figure's own rule says it in words):
- Contracts from Contract history (core.history._rows, a deleted contract included): on each day of the period a contract stands as it
  was last saved by that day (as first saved, for the days before it was entered in Cadence), and counts for the vendor on the days its
  term covers while it names the vendor (a renamed vendor's days stay with the name they had). Annual cost is prorated over those days,
  each version at its own price, against the period's own days. Devices covered are counted on the day taken.
- Jobs: the vendor's work orders on vendor service completed or closed in the period; imported ones (Source.IMPORTED) apart, never in
  another figure. Who completed one is who recorded its last completion (the status row's changed_by): a user whose role sees only
  their company's work orders (DataScope.COMPANY, apps.workorders.scoping) is the vendor's account; anyone else, or nobody, CE staff.
- Time with the vendor, the vendor's corrective repairs only (its PMs have their own figures): from the facility's day of the last
  "Assigned to vendor: <name>" status row naming one of the keys, recorded before that completion (workorders.services.assign writes
  it; else the day opened: opened as vendor service) to completed_on, the day recorded completed in Cadence. Device downtime is the same
  repairs from opened to completed: the vendor's time and the facility's together.
- PMs assigned to the vendor in Cadence (the planner never assigns a vendor) due in the period: pm_due_queryset under the windows in
  force at the period's end; those not on time by their late reason: the vendor's side (waiting on parts or the vendor, or none
  recorded), the facility's, or other (VENDOR_REASONS, FACILITY_REASONS, and OTHER_REASONS partition LateReason). Beside them, the PMs
  due on devices under the vendor's contracts (in force in the period, still naming it today) not assigned to it. Provisional until the
  last window still open among the vendor's pending PMs and the covered devices' PMs not assigned to it.
- Imported work orders (Source.IMPORTED) are counted apart (jobs_imported, cost_imported) and in no other figure: never a PM assigned,
  pending, or on a covered device, never a repair or a later repair.
- Repeat repairs: a repair of the same device opened on or within 30 days after a vendor's repair was completed, matched to the vendor's
  last repair of that device before it (one it completed after the period keeps it), counted when its finding is a device failure or
  unknown (findings.RELIABILITY / UNKNOWN; still open counts as unknown); never cancelled. Provisional until 30 days after the period's
  end.
- No problem found over the vendor's repairs with a finding recorded; documentation shares as recorded in Cadence, split by who
  completed the job; the staff evidence on file by slice 31's rule (apps.reports.survey.vendors._judge); labor and parts on its jobs.

Queries: a fixed number whatever the facility's size (17 at most: the Settings history (and the row, without one), the spellings of
contracts and of work orders, the contract history, devices covered, the jobs, their status rows, engineers, trainings, attestations,
the later repairs, labor, parts, and the PMs assigned, still pending, and on covered devices); every row is folded in Python, one pass.
Read only; indicators and the survey binder are imported inside the functions that read them, so the binder may import this module.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from statistics import median

from django.db.models import BooleanField, Case, Count, DecimalField, Max, Q, Sum, Value, When

from apps.accounts.models import DEFAULT_SCOPES, DataScope, Module
from apps.contracts.models import Contract, ContractType
from apps.contracts.models import Coverage as ContractCoverage
from apps.core.days import local_day
from apps.core.history import _rows
from apps.equipment.models import Asset, AssetStatus
from apps.pm.services import RETIRED_AND_CANCELLED, pm_due_queryset
from apps.pm.windows import HIGH_CLASSES, WORK_ORDER_RISK, Windows
from apps.pm.windows import windows as windows_of
from apps.vendors import rules
from apps.vendors.models import EngineerTraining, VendorAttestation, VendorEngineer
from apps.workorders import findings
from apps.workorders.models import (
    LABOR_AMOUNT,
    OPEN_STATUSES,
    PART_AMOUNT,
    LaborLine,
    LateReason,
    PartLine,
    Priority,
    RepairFinding,
    Source,
    WorkOrder,
    WorkOrderStatusHistory,
    WoStatus,
    WoType,
)

from . import permissions as perms

SCHEMA = 1
PCT, COUNT, DAYS, MONEY = "pct", "count", "days", "money"  # units
PERIOD, TAKEN = "period", "taken"  # basis: what happened in the period, or a state on the day the figures were taken
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
DAY = timedelta(days=1)
CENT = Decimal("0.01")
MONEY_FIELD = DecimalField(max_digits=14, decimal_places=2)
ASSIGNED_NOTE = "Assigned to vendor: "  # the status row workorders.services.assign writes (a test pins it)
REPEAT_DAYS = 30
OURS_SOURCES = tuple(s for s in Source.values if s != Source.IMPORTED)  # positive: every source but imported

# Who recorded a job's completion
VENDOR, FACILITY = "vendor", "facility"
WHO = ((VENDOR, "Completed by the vendor's account"), (FACILITY, "Completed by CE staff"))
# The priority split (Priority): urgent and high against normal and low
URGENT, ROUTINE = "urgent_high", "normal_low"
URGENT_PRIORITIES = (Priority.CRITICAL, Priority.HIGH)
PRIORITIES = ((URGENT, "Critical and high priority"), (ROUTINE, "Normal and low priority"))
# Why a PM was late (LateReason), by side; with no reason recorded, the vendor's. The three partition LateReason (a test checks it).
VENDOR_REASONS = (LateReason.WAITING_PARTS_VENDOR,)
FACILITY_REASONS = (LateReason.DEVICE_IN_USE, LateReason.NOT_LOCATED, LateReason.OUT_OF_SERVICE, LateReason.STAFFING, LateReason.SCHEDULING,
                    LateReason.DUPLICATE, LateReason.INCIDENT_HOLD)
OTHER_REASONS = (LateReason.OTHER,)
NO_REASON = "none"  # the split's key for a late PM with no reason recorded

GROUPS = (
    ("contracts", "Contracts"),
    ("jobs", "Jobs"),
    ("time", "Time with the vendor"),
    ("downtime", "Device downtime"),
    ("pm", "Scheduled maintenance"),
    ("repeats", "Repeat repairs"),
    ("use", "Our use of the vendor"),
    ("documentation", "Documentation"),
    ("staff", "Staff evidence"),
    ("cost", "Cost"),
)
# Every figure's key, in the order stored: stable, never renamed (a review's page and print read them by key).
KEYS = (
    "contracts_in_period", "contract_cost", "devices_covered",
    "jobs_completed", "jobs_imported",
    "time_median", "time_slowest", "time_within_7", "time_within_30",
    "downtime_median", "downtime_slowest",
    "pm_on_time", "pm_late_vendor", "pm_late_facility", "pm_late_other", "pm_pending", "pm_covered_not_assigned",
    "repeat_repairs", "repeat_not_recorded", "repeat_other",
    "no_problem_found",
    "doc_report_number", "doc_engineer", "doc_checks", "doc_finding",
    "staff_training", "staff_attestation", "staff_nothing", "staff_entered_after",
    "cost_labor", "cost_parts", "cost_total", "cost_imported",
)

NO_REPAIRS = "No repairs"
NO_JOBS = "No jobs"
NONE_DUE = "None due"
NOT_RECORDED_WORDS = "Not recorded"
STILL_OPEN = "Still open"

# What Cadence does not record about a vendor's service, said so it is never read as zero or computed from other dates.
NOT_RECORDED = (
    "Response and arrival times: Cadence records the day a job was assigned to the vendor and the day it was recorded completed, not when "
    "the vendor answered or arrived. The vendor's own report gives its response figures.",
    "Uptime on the contract's basis: Cadence does not count a contract's covered hours or each system's uptime. The vendor's uptime report "
    "gives it, by system.",
    "Ship times on depot repairs: Cadence does not record when a device left for the vendor's depot or came back.",
)
NOTES = (
    "The figures come from Cadence's records of the vendor's work, as entered: the day a job was recorded completed may be after the "
    "vendor's visit, and the vendor's own account records some of them. They are for the reviewer to weigh; Cadence rates nothing.",
    "The vendor is matched by name in any letter case and spacing, a trailing \"field service\" dropped; a name written another way is "
    "another vendor unless the review names it too.",
    "Completed by the vendor's account: the completion was recorded by a user who sees only their company's work orders. Every other "
    "completion was recorded by CE staff.",
    "Jobs imported from the previous system are counted apart and left out of every other figure.",
)

_CONTRACT_TYPES = dict(ContractType.choices)
_CONTRACT_COVERAGE = dict(ContractCoverage.choices)
_REASON_LABELS = dict(LateReason.choices)


# --- values and words -------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _V:
    value: object = None  # int, Decimal, or None (nothing to count)
    num: object = None
    den: int | None = None


def _count(n: int, den: int | None = None) -> _V:
    return _V(n, n, den)


def _share(num: int, den: int) -> _V:
    """A share in percent, cut to one decimal (never rounded up); None when there is nothing to share."""
    value = (Decimal(num * 100) / Decimal(den)).quantize(Decimal("0.1"), rounding=ROUND_DOWN) if den else None
    return _V(value, num, den)


def _money(amount) -> _V:
    value = Decimal(amount or 0).quantize(CENT, rounding=ROUND_HALF_UP)
    return _V(value, value, None)


def _median(days: list[int]) -> _V:
    return _V(Decimal(str(median(days))) if days else None, None, len(days))


def _slowest(days: list[int]) -> _V:
    return _V(max(days) if days else None, None, len(days))


def _within(days: list[int], limit: int) -> _V:
    return _share(sum(1 for d in days if d <= limit), len(days))


def _n(n: int, what: str) -> str:
    return f"{n:,} {what}{'' if n == 1 else 's'}"


def _number_words(value: Decimal) -> str:
    """97.50 -> "97.5", 100.0 -> "100"."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display(unit: str, value, empty: str) -> str:
    if value is None:
        return empty
    if unit == PCT:
        return f"{_number_words(Decimal(value))}%"
    if unit == DAYS:
        d = Decimal(value)
        return f"{_number_words(d)} day{'' if d == 1 else 's'}"
    if unit == MONEY:
        return f"${Decimal(value):,.2f}"
    return f"{value:,}"


def _detail(unit: str, v: _V, noun: str) -> str:
    """"7 of 8" for a share or a count out of something, "12 repairs" for days; "" otherwise (and when there is nothing)."""
    if not v.den:
        return ""
    if unit in (PCT, COUNT):
        return f"{v.num:,} of {v.den:,}"
    if unit == DAYS:
        return _n(v.den, noun)
    return ""


def _json(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _values(unit: str, v: _V, empty: str, noun: str, detail: str | None = None) -> dict:
    return {"value": _json(v.value), "num": _json(v.num), "den": v.den, "display": _display(unit, v.value, empty),
            "detail": _detail(unit, v, noun) if detail is None else detail}


def _figure(key: str, group: str, label: str, unit: str, rule: str, v: _V, *, empty: str = "None", basis: str = PERIOD, splits=(),
            noun: str = "repair", detail: str | None = None) -> dict:
    return {"key": key, "group": group, "label": label, "unit": unit, "basis": basis, "rule": rule, **_values(unit, v, empty, noun, detail),
            "splits": [{"key": k, "label": words, **_values(unit, sv, empty, noun)} for k, words, sv in splits]}


@dataclass(frozen=True)
class _Span:
    start: date
    end: date
    today: date

    @property
    def days(self) -> int:
        """The period's own days (12 months: 365 or 366): an annual cost's denominator, so a contract in force all period costs its
        annual cost."""
        return (self.end - self.start).days + 1


def _keys(vendor_keys) -> list[str]:
    """The review's keys as given (vendor_key already folded them), blanks dropped, each once, in order."""
    return [k for k in dict.fromkeys(str(k or "") for k in vendor_keys or ()) if k]


# --- contracts --------------------------------------------------------------------------------------------------------------------

_HISTORY_FIELDS = ("id", "history_id", "history_date", "history_type", "reference", "vendor", "type", "coverage", "start_on", "end_on",
                   "annual_cost")
_VERSION_FIELDS = ("reference", "vendor", "type", "coverage", "start_on", "end_on", "annual_cost")


def _contracts(keys: set, p: _Span) -> tuple[list[dict], list[str]]:
    """The vendor's contracts in force on any day of the period, from Contract history, each with its versions and their days and
    prorated cost, and devices covered today; and the contract spellings matched. Three queries (two with no contract of the vendor)."""
    rows = _rows(Contract.history.model)
    names = sorted({n for n in rows.order_by().values_list("vendor", flat=True).distinct() if rules.vendor_key(n) in keys})
    if not names:
        return [], names
    by_contract: dict = defaultdict(dict)  # contract id -> {the facility's day saved: the last row saved that day}
    for r in (rows.filter(id__in=rows.filter(vendor__in=names).values("id")).order_by("id", "history_date", "history_id")
              .values(*_HISTORY_FIELDS)):
        by_contract[r["id"]][local_day(r["history_date"])] = r
    out = []
    for cid, by_day in by_contract.items():
        saved = sorted(by_day.items())
        kept: list[dict] = []
        for i, (day, r) in enumerate(saved):
            if r["history_type"] == "-" or rules.vendor_key(r["vendor"]) not in keys:
                continue
            # This version stood from the day it was saved (the first, from before it was entered) to the day before the next one.
            first = p.start if i == 0 else max(day, p.start)
            last = min(saved[i + 1][0] - DAY, p.end) if i + 1 < len(saved) else p.end
            lo, hi = max(first, r["start_on"]), min(last, r["end_on"])
            if lo > hi:
                continue
            if kept and kept[-1]["through"] + DAY == lo and all(kept[-1]["row"][f] == r[f] for f in _VERSION_FIELDS):
                kept[-1]["through"] = hi  # a save that changed nothing counted here (the notes): one version
            else:
                kept.append({"row": r, "from": lo, "through": hi})
        if not kept:
            continue
        versions = []
        for k in kept:
            r, days = k["row"], (k["through"] - k["from"]).days + 1
            cost = (Decimal(r["annual_cost"]) * days / p.days).quantize(CENT, rounding=ROUND_HALF_UP)
            versions.append({"reference": r["reference"], "vendor": r["vendor"], "type": r["type"], "type_label": _CONTRACT_TYPES.get(r["type"], r["type"]),
                             "coverage": r["coverage"], "coverage_label": _CONTRACT_COVERAGE.get(r["coverage"], r["coverage"]),
                             "start_on": r["start_on"].isoformat(), "end_on": r["end_on"].isoformat(), "annual_cost": float(r["annual_cost"]),
                             "from": k["from"].isoformat(), "through": k["through"].isoformat(), "days": days, "cost": cost})
        latest = saved[-1][1]
        deleted = latest["history_type"] == "-"
        out.append({"id": str(cid), "reference": versions[-1]["reference"], "deleted": deleted, "vendor_today": None if deleted else latest["vendor"],
                    "devices_today": None, "days": sum(v["days"] for v in versions), "cost": sum(v["cost"] for v in versions),
                    "versions": versions, "_ours_today": not deleted and rules.vendor_key(latest["vendor"]) in keys, "_id": cid})
    ours_today = [c["_id"] for c in out if c["_ours_today"]]
    if ours_today:
        covered = dict(Asset.objects.filter(contract_id__in=ours_today).exclude(status=AssetStatus.RETIRED).order_by()
                       .values_list("contract_id").annotate(n=Count("id")))
        for c in out:
            if c["_ours_today"]:
                c["devices_today"] = covered.get(c["_id"], 0)
    out.sort(key=lambda c: (c["reference"].casefold(), c["reference"], c["id"]))
    return out, names


def _contract_data(contracts: list[dict]) -> list[dict]:
    """The contracts as stored: money as floats, the working fields dropped."""
    return [{**{k: v for k, v in c.items() if not k.startswith("_")}, "cost": float(c["cost"]),
             "versions": [{**v, "cost": float(v["cost"])} for v in c["versions"]]} for c in contracts]


# --- jobs -------------------------------------------------------------------------------------------------------------------------

# The job columns read: CE's, chosen, or the vendor's report number; never the problem, the resolution, the requester, or notes.
_JOB_FIELDS = ("id", "number", "type", "priority", "source", "opened_on", "completed_on", "asset_id", "vendor_name", "vendor_engineer_id",
               "checks_reference", "repair_check", "repair_finding", "asset__device_model_id", "asset__device_model__manufacturer",
               "asset__device_model__model", "asset__device_model__category")


@dataclass
class _Job:
    id: object
    number: str
    type: str
    priority: str
    imported: bool
    opened_on: date
    completed_on: date
    asset_id: object
    vendor: str
    engineer_id: object
    reference: str
    repair_check: str
    finding: str
    model: tuple  # (id, manufacturer, model, category): rules.names_model reads it
    who: str = FACILITY
    handoff: date | None = None
    evidence: object = None  # rules.Evidence

    @property
    def with_vendor(self) -> int:
        return max(0, (self.completed_on - (self.handoff or self.opened_on)).days)

    @property
    def downtime(self) -> int:
        return max(0, (self.completed_on - self.opened_on).days)

    @property
    def urgency(self) -> str:
        return URGENT if self.priority in URGENT_PRIORITIES else ROUTINE

    @property
    def finding_recorded(self) -> bool:
        return bool(self.finding) and self.finding != RepairFinding.NOT_STATED


def _job_names(keys: set) -> list[str]:
    """The spellings of the vendor on work orders on vendor service (one query): every work order of the vendor is found by them."""
    names = WorkOrder.objects.filter(vendor_service=True).order_by().values_list("vendor_name", flat=True).distinct()
    return sorted({n for n in names if rules.vendor_key(n) in keys})


def _jobs(names: list[str], p: _Span):
    """The vendor's jobs completed in the period (one query), and their queryset for the reads that follow."""
    qs = WorkOrder.objects.filter(vendor_service=True, vendor_name__in=names, status__in=DONE, completed_on__gte=p.start,
                                  completed_on__lte=p.end)
    jobs = [_Job(id=w["id"], number=w["number"], type=w["type"], priority=w["priority"], imported=w["source"] == Source.IMPORTED,
                 opened_on=w["opened_on"], completed_on=w["completed_on"], asset_id=w["asset_id"], vendor=w["vendor_name"],
                 engineer_id=w["vendor_engineer_id"], reference=w["checks_reference"], repair_check=w["repair_check"],
                 finding=w["repair_finding"],
                 model=(w["asset__device_model_id"], w["asset__device_model__manufacturer"], w["asset__device_model__model"],
                        w["asset__device_model__category"]))
            for w in qs.order_by("completed_on", "number").values(*_JOB_FIELDS)]
    return qs, jobs


def _who(row) -> str:
    """Who recorded a completion (its status row's values): the vendor's account when their role sees only their company's work orders
    (Role.effective_scope, as apps.workorders.scoping.scope_of reads it); CE staff otherwise, or when nobody is recorded."""
    if row is None or row["changed_by_id"] is None or row["changed_by__is_superuser"]:
        return FACILITY
    scope = row["changed_by__role__scope"] or DEFAULT_SCOPES.get(row["changed_by__role__slug"] or "", DataScope.FACILITY)
    return VENDOR if scope == DataScope.COMPANY else FACILITY


def _status_rows(qs, jobs: list[_Job], keys: set) -> None:
    """Each job's who (its last completion's status row) and handoff day (the last "Assigned to vendor" row naming the vendor recorded
    before that completion, as the facility's day; else the day opened), from one query over their status rows."""
    completion, assigned = {}, defaultdict(list)
    for r in (WorkOrderStatusHistory.objects.filter(work_order_id__in=qs.values("id"))
              .filter(Q(to_status=WoStatus.COMPLETED) | Q(note__startswith=ASSIGNED_NOTE)).order_by("created_at")
              .values("work_order_id", "from_status", "to_status", "note", "created_at", "changed_by_id", "changed_by__is_superuser",
                      "changed_by__role__scope", "changed_by__role__slug")):
        if r["to_status"] == WoStatus.COMPLETED and r["from_status"] != WoStatus.COMPLETED:
            completion[r["work_order_id"]] = r  # the last one stands
        elif r["note"].startswith(ASSIGNED_NOTE) and rules.vendor_key(r["note"][len(ASSIGNED_NOTE):]) in keys:
            assigned[r["work_order_id"]].append(r["created_at"])
    for j in jobs:
        done = completion.get(j.id)
        j.who = _who(done)
        moments = [at for at in assigned.get(j.id, ()) if done is None or at <= done["created_at"]]
        j.handoff = min(max(local_day(moments[-1]), j.opened_on), j.completed_on) if moments else j.opened_on


def _evidence(qs, ours: list[_Job]) -> None:
    """Each job's staff evidence on its completed day, by slice 31's rule (apps.reports.survey.vendors._judge). Three queries."""
    from apps.reports.survey import vendors as binder_vendors  # lazily: the binder's sections may read the vendor reviews

    engineers = {e.pk: e for e in VendorEngineer.objects.filter(pk__in=qs.values("vendor_engineer_id"))}
    trainings_of = defaultdict(list)
    for t in EngineerTraining.objects.filter(engineer_id__in=qs.values("vendor_engineer_id")):
        trainings_of[t.engineer_id].append(t)
    attestations = list(VendorAttestation.objects.order_by("-valid_through", "reference"))
    judged = [binder_vendors._Job(number=j.number, type=j.type, completed_on=j.completed_on, tag="", model=binder_vendors._Model(*j.model),
                                  vendor=j.vendor, key=rules.vendor_key(j.vendor), engineer=engineers.get(j.engineer_id), reference=j.reference,
                                  imported=False, oem_schedule=False)
              for j in ours]
    binder_vendors._judge(judged, trainings_of, attestations)
    for j, b in zip(ours, judged):
        j.evidence = b.evidence


# --- repeat repairs ---------------------------------------------------------------------------------------------------------------

def _repeat_kind(later: dict) -> str:
    """"counted", "not_recorded" (counted: no finding recorded, or still open), or "other" (not counted: no device failure)."""
    if later["status"] in OPEN_STATUSES:
        return "not_recorded"
    if later["repair_finding"] in findings.RELIABILITY:
        return "counted"
    if later["repair_finding"] in findings.UNKNOWN:
        return "not_recorded"
    return "other"


def _repeats(qs, repairs: list[_Job], keys: set, p: _Span) -> tuple[list[dict], int]:
    """The later repairs matched to the vendor's repairs (one query): [pair] counted, and how many were left out for their finding. A
    repair after one the vendor completed after the period is that one's, never this period's; an imported repair is never either."""
    last_open = p.end + timedelta(days=REPEAT_DAYS)
    later = list(WorkOrder.objects.filter(type=WoType.REPAIR, source__in=OURS_SOURCES, opened_on__gte=p.start, opened_on__lte=last_open,
                                          asset_id__in=qs.filter(type=WoType.REPAIR, source__in=OURS_SOURCES).values("asset_id"))
                 .exclude(status=WoStatus.CANCELLED).order_by("opened_on", "number")
                 .values("id", "number", "asset_id", "status", "opened_on", "completed_on", "repair_finding", "vendor_service", "vendor_name",
                         "source"))
    ours = {v.id for v in repairs}
    by_asset = defaultdict(list)  # the vendor's repairs of each device: (completed_on, number, id, the job; None: after the period)
    for v in repairs:
        by_asset[v.asset_id].append((v.completed_on, v.number, v.id, v))
    for r in later:
        if (r["id"] not in ours and r["vendor_service"] and r["status"] in DONE and r["completed_on"] and r["source"] != Source.IMPORTED
                and rules.vendor_key(r["vendor_name"]) in keys):
            by_asset[r["asset_id"]].append((r["completed_on"], r["number"], r["id"], None))
    pairs, other = [], 0
    for r in later:
        candidates = [c for c in by_asset.get(r["asset_id"], ()) if c[2] != r["id"]
                      and c[0] <= r["opened_on"] <= c[0] + timedelta(days=REPEAT_DAYS)]
        if not candidates:
            continue
        v = max(candidates, key=lambda c: (c[0], c[1]))[3]  # the vendor's last repair of the device before it
        if v is None:
            continue
        kind = _repeat_kind(r)
        if kind == "other":
            other += 1
            continue
        still_open = r["status"] in OPEN_STATUSES
        pairs.append({"repair": v.number, "completed_on": v.completed_on.isoformat(), "later": r["number"], "opened_on": r["opened_on"].isoformat(),
                      "days": (r["opened_on"] - v.completed_on).days, "finding": "" if still_open else r["repair_finding"],
                      "finding_label": STILL_OPEN if still_open else findings.label(r["repair_finding"]), "_recorded": kind == "counted"})
    return pairs, other


# --- PMs --------------------------------------------------------------------------------------------------------------------------

def _pms(keys: set, p: _Span, w: Windows) -> tuple[Counter, int, int]:
    """The PMs assigned to the vendor due in the period (pm_due_queryset, one grouped query; imported ones apart, as every figure but
    jobs_imported): (late by reason, on time, counted)."""
    late, on_time, counted = Counter(), 0, 0
    for row in (pm_due_queryset(p.start, p.end, p.today, w=w).filter(vendor_service=True, source__in=OURS_SOURCES).order_by()
                .values("vendor_name", "late_reason")
                .annotate(n=Count("id"), on_time=Count("id", filter=w.on_time_q()))):
        if rules.vendor_key(row["vendor_name"]) not in keys:
            continue
        counted += row["n"]
        on_time += row["on_time"]
        late[row["late_reason"] or NO_REASON] += row["n"] - row["on_time"]
    return late, on_time, counted


def _pm_pending(keys: set, contract_ids: list, p: _Span, w: Windows) -> tuple[int, date | None]:
    """PMs assigned to the vendor due in the period, not done, still inside their window on the day taken, and the last day one of them,
    or a PM due in the period on a device under the vendor's contracts today (`contract_ids`) not assigned to it, stops being inside it
    (until then pm_covered_not_assigned may still change). None under the default window (by the due date: nothing is ever inside
    it); else one grouped query. Imported PMs are never pending (an import is completed)."""
    if w.is_default:
        return 0, None
    high = Case(When(Q(**{f"{WORK_ORDER_RISK}__in": HIGH_CLASSES}), then=Value(True)), default=Value(False), output_field=BooleanField())
    covered = Case(When(asset__contract_id__in=contract_ids, then=Value(True)), default=Value(False), output_field=BooleanField())
    pending, last_end = 0, None
    for row in (WorkOrder.objects.filter(Q(vendor_service=True) | Q(asset__contract_id__in=contract_ids), type=WoType.PM,
                                         source__in=OURS_SOURCES, due_on__gte=p.start, due_on__lte=min(p.end, p.today),
                                         completed_on__isnull=True)
                .filter(w.inside_window_q(p.today)).exclude(RETIRED_AND_CANCELLED).order_by()
                .values("vendor_service", "vendor_name", high=high, covered=covered).annotate(n=Count("id"), last=Max("due_on"))):
        if row["vendor_service"] and rules.vendor_key(row["vendor_name"]) in keys:
            pending += row["n"]
        elif not row["covered"]:
            continue
        end = (w.high if row["high"] else w.other).end(row["last"])
        last_end = end if last_end is None or end > last_end else last_end
    return pending, last_end


def _pms_covered(keys: set, contract_ids: list, p: _Span, w: Windows) -> tuple[int, int, int]:
    """The PMs due in the period (pm_due_queryset, imported ones apart) on devices under the vendor's contracts today: (counted, not
    assigned to the vendor and completed, not assigned and not completed). One grouped query; none without such a contract."""
    if not contract_ids:
        return 0, 0, 0
    done = Case(When(completed_on__isnull=False, then=Value(True)), default=Value(False), output_field=BooleanField())
    total = completed = not_completed = 0
    for row in (pm_due_queryset(p.start, p.end, p.today, w=w).filter(asset__contract_id__in=contract_ids, source__in=OURS_SOURCES).order_by()
                .values("vendor_service", "vendor_name", done=done).annotate(n=Count("id"))):
        total += row["n"]
        if row["vendor_service"] and rules.vendor_key(row["vendor_name"]) in keys:
            continue
        if row["done"]:
            completed += row["n"]
        else:
            not_completed += row["n"]
    return total, completed, not_completed


# --- cost -------------------------------------------------------------------------------------------------------------------------

def _cost(qs) -> dict:
    """Labor and parts on the vendor's jobs, imported ones apart (two aggregates): each line to the cent (LABOR_AMOUNT, PART_AMOUNT)."""
    ours, imported = Q(work_order__source__in=OURS_SOURCES), Q(work_order__source=Source.IMPORTED)
    labor = LaborLine.objects.filter(work_order_id__in=qs.values("id")).aggregate(
        ours=Sum(LABOR_AMOUNT, filter=ours, output_field=MONEY_FIELD), imported=Sum(LABOR_AMOUNT, filter=imported, output_field=MONEY_FIELD))
    parts = PartLine.objects.filter(work_order_id__in=qs.values("id")).aggregate(
        ours=Sum(PART_AMOUNT, filter=ours, output_field=MONEY_FIELD), imported=Sum(PART_AMOUNT, filter=imported, output_field=MONEY_FIELD))
    return {"labor": Decimal(labor["ours"] or 0), "parts": Decimal(parts["ours"] or 0),
            "imported": Decimal(labor["imported"] or 0) + Decimal(parts["imported"] or 0)}


# --- the figures ------------------------------------------------------------------------------------------------------------------

_TIME_RULE = ("The vendor's corrective repairs completed in the period: days from the day each was last assigned to this vendor in Cadence "
              "(the day it was opened, when it was opened as vendor service) to the day it was recorded completed in Cadence, which may be "
              "after the vendor's visit. Not the vendor's response or arrival time.")
_DOWNTIME_RULE = ("The same repairs, days from the day each was opened to the day it was recorded completed in Cadence: the time with CE "
                  "and with the vendor together, not the vendor's alone.")
_DOC_RULE = " As recorded in Cadence, by CE staff or the vendor's account, split by who recorded the completion."
_STAFF_RULE = (" Judged on the day each job was recorded completed, as the survey binder judges it; \"on file\" is what the facility holds, "
               "never an assessment of anyone's skill.")


def _splits_by(items: list[_Job], make, groups) -> list:
    """[(key, label, make(subset))] for each (key, label, test) in groups."""
    return [(key, label, make([j for j in items if test(j)])) for key, label, test in groups]


_BY_WHO = tuple((key, label, (lambda j, key=key: j.who == key)) for key, label in WHO)
_BY_PRIORITY = tuple((key, label, (lambda j, key=key: j.urgency == key)) for key, label in PRIORITIES)


def _figures(contracts: list[dict], jobs: list[_Job], pairs: list[dict], repeat_other: int, pms, pending: int, covered, cost: dict) -> list:
    ours = [j for j in jobs if not j.imported]
    imported = [j for j in jobs if j.imported]
    repairs = [j for j in ours if j.type == WoType.REPAIR]
    late, on_time, counted = pms
    out = []

    # Contracts
    out.append(_figure("contracts_in_period", "contracts", "Contracts in force in the period", COUNT,
                       "Service contracts naming the vendor in force on any day of the period, from each contract's history in Cadence: one "
                       "deleted since is counted, and a contract whose vendor name changed counts for the days it named this vendor.",
                       _count(len(contracts))))
    out.append(_figure("contract_cost", "contracts", "Contract cost for the period", MONEY,
                       "Each contract's annual cost prorated over the days of the period it was in force, each version at the price it had "
                       "(a renewal or a price change partway through counts each price for its own days). Cadence's record of the "
                       "contracts, not the invoices.", _money(sum((c["cost"] for c in contracts), Decimal(0)))))
    out.append(_figure("devices_covered", "contracts", "Devices covered", COUNT,
                       "Devices not retired on those contracts on the day the figures were taken (not at the period's end); a contract "
                       "deleted since, or now naming another vendor, covers none here.",
                       _count(sum(c["devices_today"] or 0 for c in contracts)), basis=TAKEN))

    # Jobs
    by_type = [(t, label, _count(sum(1 for j in ours if j.type == t), len(ours))) for t, label in WoType.choices]
    by_who = [(key, label, _count(sum(1 for j in ours if j.who == key), len(ours))) for key, label in WHO]
    out.append(_figure("jobs_completed", "jobs", "Jobs completed", COUNT,
                       "The vendor's work orders on vendor service completed or closed in the period, by type and by who recorded the "
                       "completion: the vendor's own account or CE staff. Imported jobs are counted apart.",
                       _count(len(ours)), splits=[*by_type, *by_who]))
    out.append(_figure("jobs_imported", "jobs", "Jobs imported from the previous system", COUNT,
                       "Vendor jobs completed in the period and imported from the previous system: counted apart, and left out of every "
                       "other figure.", _count(len(imported))))

    # Time with the vendor, and device downtime
    def time_splits(make):
        return [*_splits_by(repairs, make, _BY_PRIORITY), *_splits_by(repairs, make, _BY_WHO)]

    def with_vendor(items):
        return [j.with_vendor for j in items]

    def downtime(items):
        return [j.downtime for j in items]

    out.append(_figure("time_median", "time", "Median days with the vendor", DAYS, _TIME_RULE, _median(with_vendor(repairs)),
                       empty=NO_REPAIRS, splits=time_splits(lambda s: _median(with_vendor(s)))))
    out.append(_figure("time_slowest", "time", "Slowest repair with the vendor", DAYS, _TIME_RULE, _slowest(with_vendor(repairs)),
                       empty=NO_REPAIRS, splits=time_splits(lambda s: _slowest(with_vendor(s)))))
    for limit in (7, 30):
        out.append(_figure(f"time_within_{limit}", "time", f"Repairs completed within {limit} days with the vendor", PCT, _TIME_RULE,
                           _within(with_vendor(repairs), limit), empty=NO_REPAIRS,
                           splits=time_splits(lambda s, limit=limit: _within(with_vendor(s), limit))))
    out.append(_figure("downtime_median", "downtime", "Median device downtime", DAYS, _DOWNTIME_RULE, _median(downtime(repairs)),
                       empty=NO_REPAIRS, splits=time_splits(lambda s: _median(downtime(s)))))
    out.append(_figure("downtime_slowest", "downtime", "Longest device downtime", DAYS, _DOWNTIME_RULE, _slowest(downtime(repairs)),
                       empty=NO_REPAIRS, splits=time_splits(lambda s: _slowest(downtime(s)))))

    # PMs
    late_total = sum(late.values())
    out.append(_figure("pm_on_time", "pm", "PMs assigned to the vendor done on time", PCT,
                       "PMs assigned to the vendor in Cadence and due in the period, counted once completed or once their window closed: on "
                       "time when completed within the PM window in force at the period's end. The PM planner never assigns a vendor: a PM "
                       "is the vendor's here only once CE assigned it.", _share(on_time, counted), empty=NONE_DUE))
    vendor_late = sum(late[str(r)] for r in VENDOR_REASONS) + late[NO_REASON]
    out.append(_figure("pm_late_vendor", "pm", "Late: waiting on the vendor, or no reason recorded", COUNT,
                       "Of the vendor's PMs not on time, those whose reason recorded is waiting on parts or the vendor, or that have no "
                       "reason recorded.", _count(vendor_late, late_total),
                       splits=[*((str(r), _REASON_LABELS[r], _count(late[str(r)], late_total)) for r in VENDOR_REASONS),
                               (NO_REASON, "No reason recorded", _count(late[NO_REASON], late_total))]))
    out.append(_figure("pm_late_facility", "pm", "Late for the facility's reasons", COUNT,
                       "Of the vendor's PMs not on time, those whose reason recorded is on the facility's side: the device in use, not "
                       "located, or out of service, staffing, scheduling, a duplicate, or held for an incident.",
                       _count(sum(late[str(r)] for r in FACILITY_REASONS), late_total),
                       splits=[(str(r), _REASON_LABELS[r], _count(late[str(r)], late_total)) for r in FACILITY_REASONS]))
    out.append(_figure("pm_late_other", "pm", "Late for another reason", COUNT,
                       "Of the vendor's PMs not on time, those whose reason recorded is Other: the work order's notes say why.",
                       _count(sum(late[str(r)] for r in OTHER_REASONS), late_total)))
    out.append(_figure("pm_pending", "pm", "PMs still inside their window", COUNT,
                       "PMs assigned to the vendor and due in the period, not done, with their window still open on the day the figures "
                       "were taken: not counted yet.", _count(pending), basis=TAKEN))
    covered_total, covered_done, covered_open = covered
    out.append(_figure("pm_covered_not_assigned", "pm", "PMs on covered devices not assigned to the vendor", COUNT,
                       "PMs due in the period on devices under the vendor's contracts on the day the figures were taken, counted as above, "
                       "that were not assigned to the vendor in Cadence: completed in-house or by another vendor, or not completed.",
                       _count(covered_done + covered_open, covered_total),
                       splits=[("completed", "Completed in-house or by another vendor", _count(covered_done, covered_total)),
                               ("not_completed", "Not completed", _count(covered_open, covered_total))]))

    # Repeat repairs
    repeated = {pair["repair"] for pair in pairs}
    unknown = sum(1 for pair in pairs if not pair["_recorded"])
    out.append(_figure("repeat_repairs", "repeats", "Repairs followed by another within 30 days", COUNT,
                       "Of the vendor's corrective repairs completed in the period, those after which the same device needed another repair "
                       "opened within 30 days whose finding is a device failure (battery, software, wore out, PM could have caught it, "
                       "caused by earlier service), not recorded, or still open. Each later repair is matched to the vendor's last repair "
                       "of the device before it, and listed to check. Information for the reviewer: Cadence does not say who caused it.",
                       _count(len(repeated), len(repairs)) if repairs else _V(None, 0, 0), empty=NO_REPAIRS,
                       detail=(f"{len(repeated):,} of {_n(len(repairs), 'repair')}; {_n(unknown, 'later repair')} not recorded" if repairs else "")))
    out.append(_figure("repeat_not_recorded", "repeats", "Later repairs with no finding recorded", COUNT,
                       "Of the later repairs counted, those with no finding recorded (or not stated by the vendor), or still open.",
                       _count(unknown, len(pairs))))
    out.append(_figure("repeat_other", "repeats", "Later repairs not counted for their finding", COUNT,
                       "Repairs of the same devices opened within 30 days that are not counted: no problem found, use error, power or room "
                       "conditions, damage in handling, or an accessory.", _count(repeat_other)))

    # Our use of the vendor
    recorded = [j for j in repairs if j.finding_recorded]
    out.append(_figure("no_problem_found", "use", "Calls with no problem found", PCT,
                       "Of the vendor's corrective repairs with a finding recorded, those where no problem was found: the fault could not "
                       "be made to happen. About the facility's calls on the vendor, never a rating of it.",
                       _share(sum(1 for j in recorded if j.finding == RepairFinding.NO_PROBLEM_FOUND), len(recorded)), empty=NOT_RECORDED_WORDS))

    # Documentation, split by who completed
    def doc(key, label, rule, items, test, empty):
        def make(subset):
            return _share(sum(1 for j in subset if test(j)), len(subset))
        out.append(_figure(key, "documentation", label, PCT, rule + _DOC_RULE, make(items), empty=empty, splits=_splits_by(items, make, _BY_WHO)))

    doc("doc_report_number", "Vendor's service report number recorded", "Of the vendor's jobs, those with its service report number.", ours,
        lambda j: bool(j.reference), NO_JOBS)
    doc("doc_engineer", "Vendor's engineer named", "Of the vendor's jobs, those naming the vendor's engineer as its report names them.", ours,
        lambda j: j.engineer_id is not None, NO_JOBS)
    doc("doc_checks", "Checks after repair recorded", "Of the vendor's corrective repairs, those with the checks after repair recorded: checked "
        "and ready for use, checked by the vendor, or not returned to use.", repairs, lambda j: bool(j.repair_check), NO_REPAIRS)
    doc("doc_finding", "What was found recorded", "Of the vendor's corrective repairs, those with what was found recorded (not \"not stated "
        "by the vendor\").", repairs, lambda j: j.finding_recorded, NO_REPAIRS)

    # Staff evidence on file
    evidence = Counter(j.evidence.kind for j in ours if j.evidence is not None)
    out.append(_figure("staff_training", "staff", "Training on file", PCT,
                       "Of the vendor's jobs, those whose named engineer had training on file naming the device's model, in force that "
                       "day." + _STAFF_RULE, _share(evidence[rules.TRAINING], len(ours)), empty=NO_JOBS))
    out.append(_figure("staff_attestation", "staff", "Attestation on file", PCT,
                       "Of the vendor's jobs, those with no such training but an attestation from the vendor, or from the named engineer's "
                       "company, naming the device's model, in force that day." + _STAFF_RULE,
                       _share(evidence[rules.ATTESTATION], len(ours)), empty=NO_JOBS))
    out.append(_figure("staff_nothing", "staff", "Nothing on file", PCT,
                       "Of the vendor's jobs, those with no training or attestation on file covering them." + _STAFF_RULE,
                       _share(evidence[rules.NONE], len(ours)), empty=NO_JOBS))
    out.append(_figure("staff_entered_after", "staff", "Evidence entered after the work", COUNT,
                       "Of the jobs with training or an attestation on file, those whose record, dated before the work, was entered in "
                       "Cadence after it.",
                       _count(sum(1 for j in ours if j.evidence is not None and j.evidence.entered_after),
                              evidence[rules.TRAINING] + evidence[rules.ATTESTATION])))

    # Cost
    out.append(_figure("cost_labor", "cost", "Labor on its jobs", MONEY,
                       "Labor lines on the vendor's jobs completed in the period, hours times rate, each line to the cent.", _money(cost["labor"])))
    out.append(_figure("cost_parts", "cost", "Parts on its jobs", MONEY,
                       "Part lines on the vendor's jobs completed in the period, quantity times unit cost, each line to the cent.",
                       _money(cost["parts"])))
    out.append(_figure("cost_total", "cost", "Labor and parts on its jobs", MONEY,
                       "Labor and parts together; the contracts' cost is above, apart.", _money(cost["labor"] + cost["parts"])))
    out.append(_figure("cost_imported", "cost", "Labor and parts on imported jobs", MONEY,
                       "Labor and parts on the vendor's jobs imported from the previous system, apart.", _money(cost["imported"])))
    return out


def snapshot(vendor_keys, period_start: date, period_end: date, *, today: date) -> dict:
    """Every figure of the review of the vendor `vendor_keys` (rules.vendor_key of its names) for period_start..period_end, as counted on
    `today` (the facility's, read once by the caller), JSON-ready, for a VendorReviewSnapshot. Call it inside the facility."""
    from apps.evaluations.indicators import _settings_at, _settings_versions  # lazily: indicators imports the survey binder

    keys = _keys(vendor_keys)
    found = set(keys)
    p = _Span(period_start, period_end, today)
    settings = _settings_at(_settings_versions(), period_end)
    w = windows_of(settings)

    contracts, contract_names = _contracts(found, p)
    job_names = _job_names(found)
    jobs, pairs, repeat_other, cost = [], [], 0, {"labor": Decimal(0), "parts": Decimal(0), "imported": Decimal(0)}
    if job_names:
        qs, jobs = _jobs(job_names, p)
        _status_rows(qs, jobs, found)
        ours = [j for j in jobs if not j.imported]
        _evidence(qs, ours)
        pairs, repeat_other = _repeats(qs, [j for j in ours if j.type == WoType.REPAIR], found, p)
        cost = _cost(qs)
    in_force_ours = [c["_id"] for c in contracts if c["_ours_today"]]
    pending, pending_end = _pm_pending(found, in_force_ours, p, w)
    covered = _pms_covered(found, in_force_ours, p, w)
    figures = _figures(contracts, jobs, pairs, repeat_other, _pms(found, p, w), pending, covered, cost)

    repeat_end = period_end + timedelta(days=REPEAT_DAYS)
    ends = [d for d in (pending_end, repeat_end if today <= repeat_end else None) if d is not None]
    return {
        "schema": SCHEMA,
        "vendor_keys": keys,
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "taken_on": today.isoformat(),
        "names": sorted(set(contract_names) | set(job_names), key=lambda n: (n.casefold(), n)),
        "settings": {"as_of": period_end.isoformat(), "pm_windows": {"high": w.high.describe(), "other": w.other.describe()},
                     "pm_targets": {"high": 100.0, "other": float(settings.target_pm_pct)}},
        "provisional_until": max(ends).isoformat() if ends else None,
        "groups": [{"key": key, "label": label} for key, label in GROUPS],
        "figures": figures,
        "contracts": _contract_data(contracts),
        "repeats": [{k: v for k, v in pair.items() if not k.startswith("_")} for pair in pairs],
        "not_recorded": list(NOT_RECORDED),
        "notes": list(NOTES),
    }


# --- who sees them ----------------------------------------------------------------------------------------------------------------

NEEDS = (Module.EVALUATIONS, Module.CONTRACTS, Module.REPORTS, Module.WORKORDERS)  # View on each: permissions.can_see_vendor_figures
EMPTIED = {"names": [], "settings": {}, "provisional_until": None, "groups": [], "figures": [], "contracts": [], "repeats": [],
           "not_recorded": [], "notes": []}


def can_see(user) -> bool:
    """Whether `user` may read a review's figures: all of them, or none (never a scoped user)."""
    return user is not None and perms.can_see_vendor_figures(user)


def needs_words() -> str:
    """"Evaluations View, Contracts View, Reports View, and Work orders View"."""
    words = [f"{Module(m).label} View" for m in NEEDS]
    return f"{', '.join(words[:-1])}, and {words[-1]}"


def cut(data: dict, user) -> dict:
    """The snapshot `data` as `user` may read it, all or nothing: whole (with "left_out": []) for someone who can_see, else the same
    shape with every figure, table, and name removed and "left_out" saying what it needs. The review's own keys and period stay."""
    data = dict(data or {})
    if can_see(user):
        data["left_out"] = []
        return data
    data.update(deepcopy(EMPTIED), left_out=[f"Every figure (needs {needs_words()})"])
    return data


def by_key(data: dict) -> dict:
    """{key: figure} of a snapshot (or a reader's cut)."""
    return {f["key"]: f for f in (data or {}).get("figures") or [] if isinstance(f, dict) and "key" in f}
