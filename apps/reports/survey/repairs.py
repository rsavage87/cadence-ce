"""
The survey binder's "Checks after repair before return to use" section (slice 30): the repairs completed in the period and how each
ended (WorkOrder.repair_check, written only by workorders.completion.complete_work_order): checked and ready for use (the standard checks
after a repair, step by step: workorders.repair_checks), checked by the vendor (with their service report's number), or not returned to
use; who checked (repair_checks.checked_by: the vendor on vendor service, else the technician assigned, never whoever recorded it) and
when (the day the repair was completed).

A repair counts when it is completed (or closed) with its completed day in the period; one reopened since is not completed and counts
again when it is completed (on its new day). Every device, ours or temporary: a rental's repair is vendor service named for its owner,
which the vendor's report fits.

Never a GAP: before slice 30 no repair recorded its checks, and the work orders import never brings an outcome over, so a repair with
none is counted and listed, its checks being on paper or in the previous system. CHECKs, the facility's housekeeping:
- the repairs completed in the period with no outcome recorded: one check for those completed before Cadence recorded checks after a
  repair, and one, apart, for those imported from the previous system (the table lists each as not recorded);
- a device whose last repair ended not returned to use and that is still out of use today (out of service, or marked in repair) with no
  repair open on it and no incident holding it (the incident decides its return): what becomes of it (retire it, or return it to its
  owner; or repair it and record its checks);
- a device in use today (in service or on loan) whose last repair ended not returned to use with no check since: no PM (but a failed
  one) or passed incoming inspection completed on or after the day that repair ended. It went back by hand (Return to service), which
  equipment.services.set_status allows once no repair is open.
The two device checks are as of today, whenever their repair ended (the outcome exists only since slice 30). A device retired or
returned to its owner since is never one; a missing device is the inventory's.

Slice 33, what each repair found (WorkOrder.repair_finding, apps.workorders.findings: one per repair, a choice, written only by
completion and services.set_repair_finding): the table's "What was found" column, and a table of the period's repairs by finding (each with
its group, whether it counts as a device failure by findings.FAILURE_RULE, and how many were opened from a failed PM), with "Not recorded"
for repairs completed before Cadence recorded findings or imported. The coverage figure counts the repairs with a finding recorded (never
not stated by the vendor). Never a gap or a check: a repair with no finding is counted, never chased one at a time.

Queries: four whatever the facility's size: the figures (grouped by outcome, imported or not), the findings (grouped by finding: _findings,
which the program evaluation reads too), the devices whose last repair ended not returned to use (one query with subqueries, among the
devices that have such a repair), and the table's rows when they are read. Nothing a requester typed (problem, resolution, notes) is read:
the outcome, the finding (a choice CE made), the report number (a token CE entered), and staff names.
"""
from __future__ import annotations

from django.db.models import Count, Exists, Max, Min, OuterRef, Q, Subquery
from django.urls import reverse

from apps.equipment.models import Asset, AssetStatus, Ownership, RiskClass
from apps.pm.aem import found_on_pm_q
from apps.workorders import findings
from apps.workorders.models import OPEN_STATUSES, InspectionResult, PmResult, RepairCheck, RepairFinding, Source, WorkOrder, WoStatus, WoType
from apps.workorders.repair_checks import checked_by, last_completed_first

from . import CHECK, DEVICE, WORK_ORDER, Figure, Gap, Period, Section, Table

KEY, TITLE = "repairs", "Checks after repair before return to use"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
IN_USE = (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN)
STILL_OUT = (AssetStatus.OUT_OF_SERVICE, AssetStatus.IN_REPAIR)  # out of use; a missing device is the inventory section's
CHECKED = (RepairCheck.PASSED, RepairCheck.VENDOR_REPORT)  # the outcomes whose checks were done (checked_by names who)
BEFORE = "Not recorded: completed before Cadence recorded checks"
IMPORTED = "Not recorded: imported"
COLUMNS = ["Work order", "Device", "Model", "Risk class", "Completed on", "Outcome", "Checked by", "Vendor report number", "What was found"]
FINDING_COLUMNS = ["What was found", "Group", "Counts as a device failure", "Repairs", "Found on a PM"]
NOT_RECORDED = "Not recorded"  # findings.label("") : completed before Cadence recorded findings, or imported
# A finding's group as the binder words it (findings.GROUPS, and "" with not stated: no finding recorded)
_FINDING_GROUP = {str(v): label for label, values in findings.GROUPS for v in values}
_FAILURE = {str(v) for v in (*findings.RELIABILITY, *findings.UNKNOWN)}  # FAILURE_RULE, of a completed repair
_OUTCOMES = dict(RepairCheck.choices)
_CLASS_LABELS = dict(RiskClass.choices)
_STATUS_WORDS = {AssetStatus.IN_SERVICE: "in service", AssetStatus.ON_LOAN: "on loan"}
# The columns a row reads (staff-written, chosen, or names only: never the problem, the resolution, the requester, or notes)
_ONLY = ("number", "completed_on", "repair_check", "checks_reference", "source", "vendor_service", "vendor_name", "assigned_to__name",
         "asset__tag", "asset__device_model__manufacturer", "asset__device_model__model", "asset__device_model__risk_class",
         "vendor_engineer__name", "vendor_engineer__company", "vendor_engineer__company_key",  # slice 31: checked_by names the engineer
         "repair_finding")  # slice 33


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _n(n: int, what: str) -> str:
    return f"{n} {what}{'' if n == 1 else 's'}"


def _completed_in(period: Period):
    """The repairs completed (or closed) with their completed day in the period."""
    return WorkOrder.objects.filter(type=WoType.REPAIR, status__in=DONE, completed_on__gte=period.start, completed_on__lte=period.end)


def _figures(repairs) -> dict:
    """{outcome, BEFORE, or IMPORTED: (how many, first completed day, last completed day)}. One query."""
    out: dict = {}
    rows = (repairs.order_by().values("repair_check", "source")
            .annotate(n=Count("id"), first=Min("completed_on"), last=Max("completed_on")))
    for row in rows:
        key = row["repair_check"] or (IMPORTED if row["source"] == Source.IMPORTED else BEFORE)
        n, first, last = out.get(key, (0, None, None))
        out[key] = (n + row["n"], min(d for d in (first, row["first"]) if d), max(d for d in (last, row["last"]) if d))
    return out


def _findings(repairs) -> dict:
    """{finding ("" for none recorded): (how many, how many of them opened from a failed PM)} over `repairs`. One grouped query: the
    binder's table and figures, and the program evaluation's findings indicators (apps.evaluations.indicators) read it."""
    return {row["repair_finding"]: (row["n"], row["on_pm"]) for row in
            repairs.order_by().values("repair_finding").annotate(n=Count("id"), on_pm=Count("id", filter=found_on_pm_q()))}


def recorded_of(found: dict) -> int:
    """The repairs with a finding recorded in _findings' counts (findings.recorded_q's rule: never blank, never not stated)."""
    return sum(n for value, (n, _on_pm) in found.items() if value and value != RepairFinding.NOT_STATED)


def _finding_rows(found: dict) -> list[list]:
    """The period's repairs by finding, in findings.GROUPS' order, then those with none recorded: every finding listed, counted or not."""
    rows = []
    for value in [str(v) for _label, values in findings.GROUPS for v in values] + [""]:
        n, on_pm = found.get(value, (0, 0))
        rows.append([findings.label(value), _FINDING_GROUP.get(value, NOT_RECORDED), "Yes" if value in _FAILURE else "No", n, on_pm])
    return rows


def _rows(repairs):
    """Every repair completed in the period, in the order completed: one query, read as the CSV streams it."""
    qs = repairs.select_related("asset__device_model", "assigned_to", "vendor_engineer").only(*_ONLY).order_by("completed_on", "number")
    for wo in qs.iterator(chunk_size=1000):
        dm = wo.asset.device_model
        if wo.repair_check:
            outcome = _OUTCOMES.get(wo.repair_check, wo.repair_check)
        else:
            outcome = IMPORTED if wo.source == Source.IMPORTED else BEFORE
        yield [wo.number, wo.asset.tag, f"{dm.manufacturer} {dm.model}", _CLASS_LABELS.get(dm.risk_class, dm.risk_class), wo.completed_on,
               outcome, checked_by(wo) if wo.repair_check in CHECKED else "", wo.checks_reference, findings.label(wo.repair_finding)]


def _not_returned_devices() -> list[dict]:
    """The devices whose last completed repair (repair_checks.last_completed_first: by the moment it was completed) ended not returned to use,
    with that repair's number and day, whether a check was done since (a PM that did not fail, or a passed incoming inspection,
    completed on or after that day), and whether a repair is open on it now. One query, among the devices with such a repair."""
    last = last_completed_first(WorkOrder.objects.filter(asset_id=OuterRef("pk"), type=WoType.REPAIR, status__in=DONE,
                                                         completed_on__isnull=False))
    since = (WorkOrder.objects.filter(asset_id=OuterRef("pk"), status__in=DONE, completed_on__gte=OuterRef("last_on"))
             .filter((Q(type=WoType.PM) & ~Q(pm_result=PmResult.FAIL)) | Q(type=WoType.INSPECTION, inspection_result=InspectionResult.PASSED)))
    open_repair = WorkOrder.objects.filter(asset_id=OuterRef("pk"), type=WoType.REPAIR, status__in=OPEN_STATUSES)
    with_one = WorkOrder.objects.filter(type=WoType.REPAIR, repair_check=RepairCheck.NOT_RETURNED).values("asset_id")
    return list(Asset.objects.filter(pk__in=with_one)
                .annotate(last_check=Subquery(last.values("repair_check")[:1]), last_number=Subquery(last.values("number")[:1]),
                          last_on=Subquery(last.values("completed_on")[:1]))
                .filter(last_check=RepairCheck.NOT_RETURNED)
                .annotate(checked_since=Exists(since), repair_open=Exists(open_repair))
                .order_by("last_on", "tag")
                .values("tag", "status", "ownership", "incident_hold", "last_number", "last_on", "checked_since", "repair_open"))


def _device_checks(devices: list[dict]) -> tuple[list[Gap], int, int]:
    """The CHECKs for devices whose last repair ended not returned to use (the module's rules), and how many are still out and how many
    back in use with no check since."""
    gaps, out, back = [], 0, 0
    for d in devices:
        tag, number, on = d["tag"], d["last_number"], d["last_on"]
        url = reverse("web:asset", args=[tag])
        if d["status"] in IN_USE and not d["checked_since"]:
            back += 1
            gaps.append(Gap(CHECK, f"{tag} is {_STATUS_WORDS[d['status']]}, but its last repair, {number}, ended not returned to use on "
                                   f"{_day(on)}, and no PM or incoming inspection has been done since: have it checked (a repair completed "
                                   "with its checks after repair), or take it out of service.", url, tag))
        elif d["status"] in STILL_OUT and not d["repair_open"] and not d["incident_hold"]:
            out += 1
            fate = "return it to its owner" if d["ownership"] != Ownership.OWNED else "retire it"
            gaps.append(Gap(CHECK, f"{tag} has been out of use since {number} ended not returned to use on {_day(on)}, with no repair open: "
                                   f"{fate}, or open a repair and complete it with its checks after repair.", url, tag))
    return gaps, out, back


def _blank_check(n: int, first, last, *, imported: bool) -> Gap:
    span = _day(first) if first == last else f"{_day(first)} to {_day(last)}"
    if imported:
        return Gap(CHECK, f"{_n(n, 'repair')} imported from the previous system ({span}) {'records' if n == 1 else 'record'} no checks after "
                          "repair in Cadence: have the previous system's records of those checks at hand (the table lists them).")
    return Gap(CHECK, f"{_n(n, 'repair')} completed {span}, before Cadence recorded checks after a repair, {'records' if n == 1 else 'record'} "
                      "no outcome: have their checks ready from the paper or service records (the table lists them).")


def build(period: Period, user) -> Section:
    repairs = _completed_in(period)
    found = _figures(repairs)
    total = sum(n for n, _first, _last in found.values())
    by_finding = _findings(repairs)  # slice 33
    recorded = recorded_of(by_finding)
    finding_rows = _finding_rows(by_finding) if total else []  # no repairs: the table says so
    gaps = [_blank_check(*found[key], imported=key == IMPORTED) for key in (BEFORE, IMPORTED) if key in found]
    device_gaps, still_out, back = _device_checks(_not_returned_devices())
    gaps += device_gaps

    def count(key) -> int:
        return found.get(key, (0, None, None))[0]

    figures = [
        Figure("Repairs completed", total, "completed or closed in the period"),
        Figure(RepairCheck.PASSED.label, count(RepairCheck.PASSED), "the checks after repair recorded step by step"),
        Figure(RepairCheck.VENDOR_REPORT.label, count(RepairCheck.VENDOR_REPORT), "with the vendor's service report number"),
        Figure(RepairCheck.NOT_RETURNED.label, count(RepairCheck.NOT_RETURNED), "the device kept out of service"),
        Figure("Completed before Cadence recorded checks", count(BEFORE), "no outcome recorded; counted, never a gap"),
        Figure("Imported from the previous system", count(IMPORTED), "no outcome recorded; counted, never a gap"),
        Figure("Still out of use after not returned", still_out, "today, with no repair open"),
        Figure("Back in use with no check since", back, "today, after a repair that ended not returned to use"),
        Figure("What was found recorded", f"{recorded:,} of {total:,}", "repairs completed in the period with a finding (never not stated by "
                                                                        "the vendor)"),
        Figure("Found on a PM", sum(on_pm for _n, on_pm in by_finding.values()), "repairs opened from a failed PM"),
    ]
    tables = [
        Table("repairs", "Repairs completed in the period", COLUMNS, lambda: _rows(repairs), count=total, links={0: WORK_ORDER, 1: DEVICE},
              empty="No repair was completed in this period."),
        Table("findings", "Repairs by what was found", FINDING_COLUMNS, lambda: iter(finding_rows), count=len(finding_rows),
              empty="No repair was completed in this period."),
    ]
    notes = [
        "Every repair completed since Cadence recorded checks after a repair says how it ended: checked and ready for use (the standard "
        "checks after a repair, step by step: visual, protective earth, equipment and applied-part leakage, function, alarms and "
        "indicators, settings and labels), checked by the vendor (their service report's number), or not returned to use.",
        "Checked by: the technician the repair was assigned to, or the vendor for vendor service, never whoever recorded it. The day is "
        "the day the repair was completed.",
        "Cadence records each reading and the technician's pass or fail, never limits: the safety analyzer and the facility's plan apply "
        "them. Each repair's readings are on its work order and its printout.",
        "Repairs completed before Cadence recorded checks after a repair, and repairs imported from the previous system, have no outcome: "
        "they are counted and listed, never gaps.",
        "A device back in use after its last repair ended not returned to use counts as checked when a PM (not failed) or a passed "
        "incoming inspection was completed on or after the day that repair ended. The devices are read as of today.",
        "What was found: one finding per repair, chosen by the facility's CE staff (a vendor's account records only what it observed on "
        "the device). Not recorded: completed before Cadence recorded findings, or imported. " + findings.FAILURE_RULE,
        "A repair counts on the day it was completed; one reopened since counts once it is completed again.",
        "Risk class is each model's class today.",
    ]
    return Section(key=KEY, title=TITLE, topic="Equipment is checked for safety and function after a repair before it goes back into use.",
                   covers=f"Repairs completed {period.label}; devices {period.as_of_today}", figures=figures, gaps=gaps, tables=tables,
                   notes=notes)
