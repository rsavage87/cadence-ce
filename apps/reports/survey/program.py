"""
The survey binder's "Program and policies" section (slice 25): what Cadence is set to measure for this facility, as of today. The
facility, when its Settings last changed, the PM completion targets by risk class and the other KPI targets, the policy lines as set
in Settings (and whether each is still Cadence's default), and the risk-scoring bands with today's active devices in each.

It needs Reports View only (apps.reports.permissions.SURVEY_NEEDS): the policy texts are on the screens they govern already. Its one
kind of item is a CHECK: a PM policy line that speaks of a month or a grace period, while Cadence counts a PM on time only when it was
completed by its due date (apps.pm.services.pm_due_queryset, the Overview's rule). A fixed two queries: the settings row and the
active devices by risk class.
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.urls import reverse

from apps.core.days import local_day
from apps.equipment.models import RiskClass
from apps.facility import services as fs
from apps.tenants.context import get_current_tenant

from . import CHECK, Figure, Gap, Period, Section, Table

KEY, TITLE = "program", "Program and policies"
CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
# The policy lines about PM completion: what the CHECK reads.
PM_POLICIES = ("policy_life_support", "policy_medium_low")
ON_TIME_NOTE = "How on time is measured: a PM is on time when completed on or before its due date, as on the Overview. No grace period is added."
# "no grace", "without grace", "without a grace period", "zero grace", "0-day grace": a text that denies a grace period agrees with Cadence.
_DENIED_GRACE = re.compile(r"\b(?:no|without(?:\s+an?y?)?|zero|0[-\s]?days?)\s+grace", re.IGNORECASE)


def disagrees_with_due_date(text: str) -> bool:
    """Whether a PM policy line speaks of a month or a grace period (any letter case), which Cadence does not measure: a PM is on time
    only when completed by its due date. A grace period the text denies ("no grace") agrees."""
    if "month" in text.lower():
        return True
    return "grace" in _DENIED_GRACE.sub("", text).lower()


def _pct(value) -> str:
    """100.0 -> "100%", 97.5 -> "97.5%"."""
    return f"{float(value):.2f}".rstrip("0").rstrip(".") + "%"


def _days(value) -> str:
    n = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return f"{n} day" + ("" if Decimal(n) == 1 else "s")


def build(period: Period, user) -> Section:
    tenant = get_current_tenant()
    s = fs.get_settings()
    saved = s.updated_at is not None  # get_settings() returns unsaved defaults until the facility first saves
    targets = fs.compliance_targets(s)
    kpi = fs.kpi_targets(s)
    figures = [
        Figure("Facility", tenant.name if tenant else ""),
        Figure("Settings last changed in Cadence", local_day(s.updated_at) if saved else "Never changed: Cadence's defaults"),
    ]
    for rc in CLASS_ORDER:
        hint = "Held at 100% for life support and high risk" if rc in (RiskClass.LIFE_SUPPORT, RiskClass.HIGH) else "The facility's target, set in Settings"
        name = "life support" if rc == RiskClass.LIFE_SUPPORT else f"{rc.label.lower()} risk"
        figures.append(Figure(f"PM completion target, {name}", _pct(targets[rc]), hint=hint))
    figures += [Figure("Fleet uptime target", _pct(kpi["uptime_pct"])), Figure("Mean time to repair target", _days(kpi["mttr_days"]))]

    items = fs.policy_items(s)
    policy_rows = [[item["label"], item["text"], "Yes" if item["is_default"] else "No"] for item in items]
    bands = fs.risk_summary()
    band_rows = [[b["band"], b["label"], b["devices"]] for b in bands]

    gaps = []
    for item in items:
        if item["field"] in PM_POLICIES and disagrees_with_due_date(item["text"]):
            gaps.append(Gap(CHECK, f"Your {item['label'].lower()} policy text says “{item['text']}”; Cadence counts a PM on time only when done "
                                   f"by its due date. Change the text in Settings, or be ready to explain the difference.",
                            url=reverse("web:settings"), record="Settings"))

    return Section(
        key=KEY, title=TITLE,
        topic="What Cadence is set to measure for this facility: its PM completion targets, its maintenance policy lines, and how devices "
              "are placed in risk classes.",
        covers=period.as_of_today,
        figures=figures,
        gaps=gaps,
        tables=[
            Table(key="policy", title="Policy lines set in Cadence (shown on work orders and in the PM planner)",
                  columns=["Policy", "Text in Cadence", "Cadence's default text"], rows=lambda: iter(policy_rows), count=len(policy_rows)),
            Table(key="risk_bands", title="Risk-scoring bands", columns=["Score", "Risk class", "Active devices today"],
                  rows=lambda: iter(band_rows), count=len(band_rows)),
        ],
        notes=[
            ON_TIME_NOTE,
            "The policy lines are the short texts set in Cadence's Settings. They are not the facility's written medical equipment management "
            "plan, which Cadence does not keep.",
            "PM completion targets: life support and high risk are held at 100%; medium and low risk use the facility's target from Settings.",
            f"Risk scores: {fs.RISK_RUBRIC}. A model's risk class follows its score's band; a model never scored has a class set by hand. "
            "Devices are counted under their model's class today.",
        ],
    )
