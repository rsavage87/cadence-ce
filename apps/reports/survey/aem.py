"""
The survey binder's "Alternate equipment maintenance (AEM)" section (slice 25): the models maintained today on an interval other than
the manufacturer's, who proposed and approved each one, the evidence the committee saw, how the model's devices have fared since,
and the committee's decisions in the period. Read from apps.pm.aem's records only.

On AEM means the model's own rule (DeviceModel.pm_interval_months differs from its OEM interval), never aem_interval_months alone: an
interval on file for a model excluded from AEM (life support, the facility's policy; the CMS mark for imaging, radiologic, and laser
equipment) never applies, so it is not counted, and it is never a gap. An interval in force with no recorded approval (apps.pm.aem
is_legacy) is a GAP: the committee ratifies it from the model's AEM tab.

Queries: the models with their active devices (one), the approved decisions (one), corrective repairs and failed PMs since each
approval on every AEM model at once (one grouped query), and the decisions in the period (one).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Count, F, Q
from django.urls import reverse

from apps.core.history import who
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.pm import aem
from apps.pm.models import AemDecision, AemStatus
from apps.workorders.models import PmResult, WorkOrder, WoStatus, WoType

from . import GAP, Figure, Gap, Period, Section, Table

NO_APPROVAL = "No recorded approval"
_CLASS_LABELS = dict(RiskClass.choices)
_STATUS_LABELS = dict(AemStatus.choices)

IN_FORCE_COLUMNS = ["Manufacturer", "Model", "Risk class", "Active devices", "OEM interval (months)", "AEM interval (months)", "Approved on",
                    "Minutes reference", "Proposed by", "Decided by", "Evidence as of", "Evidence: device-years", "Evidence: corrective repairs",
                    "Evidence: repairs per device-year", "Evidence: PMs completed", "Evidence: PMs on time",
                    "Corrective repairs since approval", "Failed PMs since approval"]
DECISION_COLUMNS = ["Date", "Decision", "Manufacturer", "Model", "Interval (months)", "OEM interval (months)", "Proposed on",
                    "Minutes reference", "End reason", "By", "Status today"]


def _name(user) -> str:
    """A decision's person, as the facility may name them (core.history.who's rule: never someone of another facility)."""
    return who(SimpleNamespace(history_user=user)) if user is not None else ""


def _number(value):
    """A figure from a decision's evidence snapshot (JSON): Decimal for a fraction, as recorded; None when missing."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _since_approval() -> dict:
    """{model id: {"repairs", "failed"}} for every model with an approved decision: corrective repairs opened (not cancelled) and
    PMs failed, from the committee's date. One grouped query over all of them (one approved decision per model)."""
    decided = F("asset__device_model__aem_decisions__decided_on")
    rows = (WorkOrder.objects.filter(asset__device_model__aem_decisions__status=AemStatus.APPROVED,
                                     asset__device_model__aem_decisions__decided_on__isnull=False)
            .order_by().values("asset__device_model")
            .annotate(repairs=Count("id", filter=Q(type=WoType.REPAIR, opened_on__gte=decided) & ~Q(status=WoStatus.CANCELLED)),
                      failed=Count("id", filter=Q(type=WoType.PM, pm_result=PmResult.FAIL, completed_on__gte=decided))))
    return {row["asset__device_model"]: row for row in rows}


def _in_force_row(dm: DeviceModel, decision: AemDecision | None, since: dict) -> list:
    name = [dm.manufacturer, dm.model, _CLASS_LABELS.get(dm.risk_class, dm.risk_class), dm.active, dm.oem_pm_interval_months, dm.pm_interval_months]
    if decision is None:
        return name + [None, NO_APPROVAL] + [""] * 2 + [None] * 8
    ev = decision.evidence or {}
    as_of = ev.get("as_of")
    counts = since.get(dm.pk, {})
    return name + [decision.decided_on, decision.decision_note, _name(decision.proposed_by), _name(decision.decided_by),
                   date.fromisoformat(as_of) if isinstance(as_of, str) else None, _number(ev.get("device_years")), _number(ev.get("repairs")),
                   _number(ev.get("repairs_per_device_year")), _number(ev.get("pm_completed")), _number(ev.get("pm_on_time")),
                   counts.get("repairs", 0), counts.get("failed", 0)]


def _events(period: Period) -> list[list]:
    """The committee's decisions in the period by their date (approved and rejected by decided_on), and withdrawals and ends by the
    day they happened (ended_on). An approval since ended is still an approval on its day."""
    start, end = period.start, period.end
    decisions = (AemDecision.objects.filter(Q(decided_on__gte=start, decided_on__lte=end) | Q(ended_on__gte=start, ended_on__lte=end))
                 .select_related("device_model", "decided_by", "ended_by"))
    out = []
    for d in decisions:
        dm = d.device_model
        base = [dm.manufacturer, dm.model, d.interval_months, d.oem_interval_months, d.proposed_on]
        status_today = _STATUS_LABELS.get(d.status, d.status)
        decided = d.decided_on is not None and start <= d.decided_on <= end
        if decided and d.status in (AemStatus.APPROVED, AemStatus.ENDED):
            out.append([d.decided_on, "Approved"] + base + [d.decision_note, "", _name(d.decided_by), status_today])
        elif decided and d.status == AemStatus.REJECTED:
            out.append([d.decided_on, "Rejected"] + base + [d.decision_note, "", _name(d.decided_by), status_today])
        if d.ended_on is not None and start <= d.ended_on <= end and d.status in (AemStatus.WITHDRAWN, AemStatus.ENDED):
            label = "Withdrawn" if d.status == AemStatus.WITHDRAWN else "Ended"
            out.append([d.ended_on, label] + base + ["", d.end_reason, _name(d.ended_by), status_today])
    out.sort(key=lambda r: (r[0], r[2], r[3], r[1]))
    return out


def build(period: Period, user) -> Section:
    models = list(DeviceModel.objects.annotate(active=Count("assets", filter=Q(assets__status__in=Asset.ACTIVE_STATUSES)))
                  .order_by("manufacturer", "model"))
    on_aem = [dm for dm in models if dm.pm_interval_months != dm.oem_pm_interval_months]
    life_support = [dm for dm in models if dm.risk_class == RiskClass.LIFE_SUPPORT]
    cms = [dm for dm in models if dm.risk_class != RiskClass.LIFE_SUPPORT and dm.oem_schedule_required]  # exclusion()'s order
    excluded_on_aem = [dm for dm in models if dm.aem_excluded and dm.pm_interval_months != dm.oem_pm_interval_months]
    approved = {d.device_model_id: d for d in
                AemDecision.objects.filter(status=AemStatus.APPROVED).select_related("proposed_by", "decided_by")}
    since = _since_approval() if any(dm.pk in approved for dm in on_aem) else {}

    in_force_rows, gaps = [], []
    for dm in on_aem:
        decision = approved.get(dm.pk)
        in_force_rows.append(_in_force_row(dm, decision, since))
        if aem.is_legacy(dm, decision) and not dm.aem_excluded:
            name = f"{dm.manufacturer} {dm.model}"
            gaps.append(Gap(GAP, f"{name} runs on a {dm.aem_interval_months}-month AEM interval with no recorded approval: ratify it from the "
                                 "model's AEM tab.", reverse("web:pm_model", args=[dm.pk]) + "?tab=aem", name))
    events = _events(period)

    figures = [
        Figure("Models on AEM today", len(on_aem)),
        Figure("Devices on AEM today", sum(dm.active for dm in on_aem), "active devices of those models"),
        Figure("Life-support models", len(life_support), "excluded by this facility's policy: always the manufacturer's interval"),
        Figure("Imaging, radiologic, and laser models", len(cms), "excluded by CMS: on the manufacturer's schedule"),
        Figure("Excluded models on AEM", len(excluded_on_aem), "an interval on file for an excluded model never applies"),
        Figure("AEM decisions in the period", len(events)),
    ]
    tables = [
        Table("in_force", "AEM intervals in force", IN_FORCE_COLUMNS, lambda: iter(in_force_rows), count=len(in_force_rows),
              empty="No model is on an AEM interval: every device follows the manufacturer's interval."),
        Table("decisions", "AEM decisions in the period", DECISION_COLUMNS, lambda: iter(events), count=len(events),
              empty="The committee recorded no AEM decision in this period."),
    ]
    notes = [
        "A model is on AEM when its PM interval in force differs from the manufacturer's (OEM) interval. Life-support models are excluded "
        "by this facility's policy. CMS keeps imaging and radiologic equipment, medical lasers, and equipment without enough maintenance "
        f"history on the manufacturer's schedule (Cadence asks for {aem.AEM_HISTORY_YEARS} years of a model's history before a proposal). "
        "An AEM interval on file for an excluded model never applies, so its devices are not counted as on AEM.",
        "Approved on is the committee's meeting date; the minutes reference is what the approver recorded. The evidence is what the "
        "committee saw: the model's failure history as the proposal recorded it, on the date shown.",
        "Since approval: corrective repairs opened (not cancelled) and PMs that failed on the model's devices from the committee's date "
        "to today.",
        "Decisions in the period: approvals and rejections by the committee's date, withdrawals and ends by the day they happened.",
        "Risk class is each model's class today.",
    ]
    return Section(key="aem", title="Alternate equipment maintenance (AEM)",
                   topic="Which device models are maintained on an interval other than the manufacturer's, who approved it and on what "
                         "evidence, and how those devices have done since.",
                   covers=f"Decisions {period.label}; intervals in force {period.as_of_today}", figures=figures, gaps=gaps, tables=tables,
                   notes=notes)
