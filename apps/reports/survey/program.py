"""
The survey binder's "Program and policies" section (slice 25): what Cadence is set to measure for this facility, as of today. The
facility, when its Settings last changed, the PM completion targets by risk class and the other KPI targets, the policy lines as set
in Settings (and whether each is still Cadence's default), and the risk-scoring bands with today's active devices in each.

It needs Reports View only (apps.reports.permissions.SURVEY_NEEDS): the policy texts are on the screens they govern already. Its one
kind of item is a CHECK: a PM policy line that contradicts the window Cadence counts that group's PMs by (by the due date, a PM policy
line that speaks of a month or a grace period), and, slice 27, a change to the PM completion window after the period started.

Slice 27, the PM completion window (apps.pm.windows; Settings): the figures show each group's window and when it last changed, with
who changed it; a table lists every change to it, from the facility's Settings history (apps.core.history._rows: this facility's rows
only); and a CHECK names each change made on or after the period's first day, because every PM in the period is judged by today's
window. A PM policy line is read against its group's window: by the due date with disagrees_with_due_date (the binder's slice 25 rule),
any other window with apps.pm.windows.policy_disagrees (word classes, never numbers: the Settings panel's warning). A fixed three queries:
the settings row, its history, and the active devices by risk class.
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.urls import reverse

from apps.core.days import local_day
from apps.core.history import _rows, who
from apps.equipment.models import RiskClass
from apps.facility import services as fs
from apps.facility.models import FacilitySettings
from apps.pm import windows as pm_windows
from apps.reports.fleet import on_time_words
from apps.tenants.context import get_current_tenant

from . import CHECK, Figure, Gap, Period, Section, Table

KEY, TITLE = "program", "Program and policies"
CLASS_ORDER = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH, RiskClass.MEDIUM, RiskClass.LOW)
# The policy lines about PM completion: what the CHECK reads.
PM_POLICIES = ("policy_life_support", "policy_medium_low")
# The window's two groups (apps.pm.windows.Windows.high / .other): their names in words and their PM policy lines.
GROUPS = (("high", "life support and high risk"), ("other", "medium and low risk"))
POLICY_GROUP = {field: group for group, field in pm_windows.PM_POLICY_FIELDS.items()}
ON_TIME_NOTE = "How on time is measured: a PM is on time when completed on or before its due date, as on the Overview. No grace period is added."
# "no grace", "without grace", "without a grace period", "zero grace", "0-day grace": a text that denies a grace period agrees with Cadence.
_DENIED_GRACE = re.compile(r"\b(?:no|without(?:\s+an?y?)?|zero|0[-\s]?days?)\s+grace", re.IGNORECASE)
WINDOW_FIELDS = ("pm_window_high", "pm_window_high_days", "pm_window_other", "pm_window_other_days")


def disagrees_with_due_date(text: str) -> bool:
    """Whether a PM policy line speaks of a month or a grace period (any letter case), which Cadence does not measure: a PM is on time
    only when completed by its due date. A grace period the text denies ("no grace") agrees."""
    if "month" in text.lower():
        return True
    return "grace" in _DENIED_GRACE.sub("", text).lower()


def disagrees(text: str, window: pm_windows.Window) -> bool:
    """Whether a PM policy line contradicts its group's window (slice 27): by the due date, disagrees_with_due_date; any other window,
    apps.pm.windows.policy_disagrees (the Settings panel's warning)."""
    return disagrees_with_due_date(text) if window.is_due_date else pm_windows.policy_disagrees(text, window)


def on_time_note(w: pm_windows.Windows) -> str:
    """How on time is measured, in words (ON_TIME_NOTE by the due date)."""
    if w.is_default:
        return ON_TIME_NOTE
    return (f"How on time is measured: a PM is on time when completed within the facility's PM window, as on the Overview: {on_time_words(w)}. "
            "Each PM is judged by today's window and its model's class today, so a change to the window recounts past months.")


def window_changes() -> list[dict]:
    """Every change to the PM completion window, oldest first, from this facility's Settings history (one query): {group, on (the
    facility's day), old, new (Windows' Window), by}. Before the first saved row the window was the default, by the due date."""
    history = FacilitySettings.history.model
    previous = {"high": pm_windows.Window(), "other": pm_windows.Window()}
    found = []
    for rec in (_rows(history).select_related("history_user").order_by("history_date", "history_id")
                .only("history_id", "history_date", "history_user", *WINDOW_FIELDS)):
        current = {"high": pm_windows.Window(rec.pm_window_high, rec.pm_window_high_days),
                   "other": pm_windows.Window(rec.pm_window_other, rec.pm_window_other_days)}
        for group, _name in GROUPS:
            if current[group] != previous[group]:
                found.append({"group": group, "on": local_day(rec.history_date), "old": previous[group], "new": current[group], "by": who(rec)})
        previous = current
    return found


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


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
    w = pm_windows.windows(s)  # from the row already read: no second Settings query
    changes = window_changes()
    for group, name in GROUPS:
        last = next((c for c in reversed(changes) if c["group"] == group), None)
        hint = f"Changed {_day(last['on'])} by {last['by']}" if last else "Cadence's default, never changed"
        figures.append(Figure(f"PM on time, {name}", getattr(w, group).describe(), hint=hint))

    items = fs.policy_items(s)
    policy_rows = [[item["label"], item["text"], "Yes" if item["is_default"] else "No"] for item in items]
    bands = fs.risk_summary()
    band_rows = [[b["band"], b["label"], b["devices"]] for b in bands]
    names = dict(GROUPS)
    change_rows = [[c["on"], names[c["group"]].capitalize(), c["old"].describe(), c["new"].describe(), c["by"]] for c in changes]

    gaps = []
    settings_url = reverse("web:settings")
    for item in items:
        if item["field"] not in PM_POLICIES:
            continue
        window = getattr(w, POLICY_GROUP[item["field"]])
        if not disagrees(item["text"], window):
            continue
        if window.is_due_date:
            measured = "Cadence counts a PM on time only when done by its due date. Change the text in Settings"
        else:
            words = window.describe()
            measured = (f"Cadence counts this group's PMs on time when done {words[:1].lower()}{words[1:]} (its PM window). Change the text "
                        "or the window in Settings")
        gaps.append(Gap(CHECK, f"Your {item['label'].lower()} policy text says “{item['text']}”; {measured}, or be ready to explain the "
                               "difference.", url=settings_url, record="Settings"))
    for c in changes:
        if c["on"] >= period.start:
            gaps.append(Gap(CHECK, f"The PM window for {names[c['group']]} was changed on {_day(c['on'])} by {c['by']}, from "
                                   f"“{c['old'].describe()}” to “{c['new'].describe()}”: every PM in the period is judged by today's window. "
                                   "Be ready to explain the change.", url=settings_url, record="Settings"))

    return Section(
        key=KEY, title=TITLE,
        topic="What Cadence is set to measure for this facility: its PM completion targets and when a PM counts as on time, its maintenance "
              "policy lines, and how devices are placed in risk classes.",
        covers=period.as_of_today,
        figures=figures,
        gaps=gaps,
        tables=[
            Table(key="policy", title="Policy lines set in Cadence (shown on work orders and in the PM planner)",
                  columns=["Policy", "Text in Cadence", "Cadence's default text"], rows=lambda: iter(policy_rows), count=len(policy_rows)),
            Table(key="pm_window_changes", title="Changes to the PM completion window (when a PM counts as on time)",
                  columns=["Changed on", "Risk group", "Before", "After", "Changed by"], rows=lambda: iter(change_rows), count=len(change_rows),
                  empty="Never changed: Cadence has always counted a PM on time when done by its due date."),
            Table(key="risk_bands", title="Risk-scoring bands", columns=["Score", "Risk class", "Active devices today"],
                  rows=lambda: iter(band_rows), count=len(band_rows)),
        ],
        notes=[
            on_time_note(w),
            "The policy lines are the short texts set in Cadence's Settings. They are not the facility's written medical equipment management "
            "plan, which Cadence does not keep.",
            "PM completion targets: life support and high risk are held at 100%; medium and low risk use the facility's target from Settings.",
            f"Risk scores: {fs.RISK_RUBRIC}. A model's risk class follows its score's band; a model never scored has a class set by hand. "
            "Devices are counted under their model's class today.",
        ],
    )
