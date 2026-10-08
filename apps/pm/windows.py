"""
The PM completion window (slice 27): when a PM counts as on time. The facility's written policy says it, one window for life support and
high risk and one for medium and low (FacilitySettings.pm_window_high / _other and their days; apps.facility.models.PmWindow):

- by the due date (the default, and the mock's KPI: nothing changes for a facility that never chooses);
- within N days after the due date (1 to PM_WINDOW_DAYS_MAX);
- by the end of the due month;
- by the end of the month after the due month.

One rule, used by every on-time and compliance figure (apps.pm.services' rates, series, and missed PMs; report_compliance; the
technician report; the custom report's PM on time; the survey binder; the late reason). The schedule keeps the due date: what is due,
past due, and overdue for the people doing the work (the PM calendar, the nav badge, Auto-assign week, Equipment's buckets, My work,
the digest, the Work orders list) never reads the window.

A PM is judged by today's window and its device model's class today, as every figure already judges by today's class: changing the
window recounts past months (Settings says so, and the binder shows when it changed).

Database conditions are positive and never meant to be negated: a month comparison over a blank completion date is NULL, and NOT NULL
is NULL, so a negated one would quietly drop every open PM. Ask for not_on_time_q rather than ~on_time_q. When both groups use the same
window the conditions name no class (no join), and the default window gives exactly the SQL the code had before this slice.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import F, Q, Value
from django.db.models.functions import ExtractMonth, ExtractYear
from django.db.models.lookups import LessThanOrEqual

from apps.core.expressions import DayNumber
from apps.equipment.models import RiskClass
from apps.facility.models import PM_WINDOW_DAYS_MAX, PmWindow

HIGH_CLASSES = (RiskClass.LIFE_SUPPORT, RiskClass.HIGH)  # compliance_targets' 100% group; everything else is "other"
WORK_ORDER_RISK = "asset__device_model__risk_class"  # where a PM work order's class is
ASSET_RISK = "device_model__risk_class"  # where a device's class is


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _previous_month_first(d: date) -> date:
    first = _first_of_month(d)
    return _first_of_month(first - timedelta(days=1))


@dataclass(frozen=True)
class Window:
    """One group's window."""
    kind: str = PmWindow.DUE_DATE
    days: int | None = None

    @property
    def is_due_date(self) -> bool:
        return self.kind == PmWindow.DUE_DATE

    def end(self, due: date) -> date:
        """The last day a PM due on `due` counts as on time."""
        if self.kind == PmWindow.DAYS_AFTER:
            return due + timedelta(days=self.days or 0)
        if self.kind == PmWindow.DUE_MONTH:
            return _month_end(due)
        if self.kind == PmWindow.NEXT_MONTH:
            return _month_end(_month_end(due) + timedelta(days=1))
        return due

    def cutoff(self, today: date) -> date:
        """A PM (or a device's next PM) due before this day is past its window today: due < cutoff(today) exactly when
        end(due) < today."""
        if self.kind == PmWindow.DAYS_AFTER:
            return today - timedelta(days=self.days or 0)
        if self.kind == PmWindow.DUE_MONTH:
            return _first_of_month(today)
        if self.kind == PmWindow.NEXT_MONTH:
            return _previous_month_first(today)
        return today

    def describe(self) -> str:
        """The window in words, capitalized: "Within 14 days after the due date"."""
        if self.kind == PmWindow.DAYS_AFTER:
            return f"Within {self.days} day{'' if self.days == 1 else 's'} after the due date"
        return PmWindow(self.kind).label

    def example(self, due: date = date(2026, 3, 10)) -> str:
        """For the Settings panel: "A PM due Mar 10 is on time if done by Mar 31" (a fixed sample day: no clock read)."""
        end = self.end(due)
        return f"A PM due {due:%b} {due.day} is on time if done by {end:%b} {end.day}."

    def on_time_q(self, *, due: str, completed: str) -> Q:
        """Completed, and within this window of its due date."""
        done = Q(**{f"{completed}__isnull": False})
        if self.kind == PmWindow.DAYS_AFTER:
            return done & Q(LessThanOrEqual(DayNumber(F(completed)) - DayNumber(F(due)), Value(self.days or 0)))
        if self.kind in (PmWindow.DUE_MONTH, PmWindow.NEXT_MONTH):
            extra = 1 if self.kind == PmWindow.NEXT_MONTH else 0
            return done & Q(LessThanOrEqual(_month_index(completed), _month_index(due) + Value(extra)))
        return Q(**{f"{completed}__lte": F(due)})

    def not_on_time_q(self, *, due: str, completed: str) -> Q:
        """Not completed, or completed after this window (written out: never ~on_time_q)."""
        open_ = Q(**{f"{completed}__isnull": True})
        if self.kind == PmWindow.DAYS_AFTER:
            return open_ | Q(LessThanOrEqual(DayNumber(F(due)) + Value((self.days or 0) + 1), DayNumber(F(completed))))
        if self.kind in (PmWindow.DUE_MONTH, PmWindow.NEXT_MONTH):
            extra = 1 if self.kind == PmWindow.NEXT_MONTH else 0
            return open_ | Q(LessThanOrEqual(_month_index(due) + Value(extra + 1), _month_index(completed)))
        return open_ | Q(**{f"{completed}__gt": F(due)})


def _month_index(field: str):
    """A date's month as one number (year * 12 + month): month windows compare across a year end."""
    return ExtractYear(F(field)) * Value(12) + ExtractMonth(F(field))


@dataclass(frozen=True)
class Windows:
    """The facility's two windows. Read once per figure and passed down (`w`), as `today` is."""
    high: Window = Window()
    other: Window = Window()

    def for_class(self, risk_class) -> Window:
        return self.high if risk_class in HIGH_CLASSES else self.other

    @property
    def is_default(self) -> bool:
        """Both by the due date: every figure is what it was before slice 27."""
        return self.high.is_due_date and self.other.is_due_date

    @property
    def uniform(self) -> bool:
        return self.high == self.other

    def end(self, due: date, risk_class) -> date:
        return self.for_class(risk_class).end(due)

    def _by_class(self, risk: str, make) -> Q:
        if self.uniform:
            return make(self.high)
        high = Q(**{f"{risk}__in": HIGH_CLASSES})
        return (high & make(self.high)) | (~high & make(self.other))

    def on_time_q(self, *, due: str = "due_on", completed: str = "completed_on", risk: str = WORK_ORDER_RISK) -> Q:
        """Completed within its class's window (a PM work order's fields by default)."""
        return self._by_class(risk, lambda w: w.on_time_q(due=due, completed=completed))

    def not_on_time_q(self, *, due: str = "due_on", completed: str = "completed_on", risk: str = WORK_ORDER_RISK) -> Q:
        """Not completed, or completed after its class's window. Never write ~on_time_q."""
        return self._by_class(risk, lambda w: w.not_on_time_q(due=due, completed=completed))

    def past_window_q(self, today: date, *, date_field: str = "due_on", risk: str = WORK_ORDER_RISK) -> Q:
        """Due (on `date_field`) before its class's cutoff: its window has closed by `today`. For devices: date_field="next_pm_on",
        risk=ASSET_RISK."""
        return self._by_class(risk, lambda w: Q(**{f"{date_field}__lt": w.cutoff(today)}))

    def inside_window_q(self, today: date, *, date_field: str = "due_on", risk: str = WORK_ORDER_RISK) -> Q:
        """Due before `today` but its window still open (written out: never ~past_window_q). Empty for the default window."""
        return self._by_class(risk, lambda w: Q(**{f"{date_field}__lt": today, f"{date_field}__gte": w.cutoff(today)}))

    def describe(self) -> dict:
        return {"high": self.high.describe(), "other": self.other.describe()}


DEFAULT = Windows()


def windows(s=None) -> Windows:
    """The facility's windows, from its Settings row `s` (read when not given: pass the row a caller already has)."""
    if s is None:
        from apps.facility.services import get_settings  # facility.services imports this module's helpers lazily

        s = get_settings()
    return Windows(high=Window(s.pm_window_high, s.pm_window_high_days), other=Window(s.pm_window_other, s.pm_window_other_days))


def window_of(w: Windows | None) -> Windows:
    return w if w is not None else windows()


# --- the policy texts ------------------------------------------------------------------------------------------------------------

PM_POLICY_FIELDS = {"high": "policy_life_support", "other": "policy_medium_low"}
_POLICY_LEADS = {"high": "OEM interval", "other": "AEM allowed"}


def policy_default(group: str, window: Window) -> str:
    """The default text of a group's PM policy line for `window` (apps.facility's POLICY defaults for the due date)."""
    if window.is_due_date:
        return {"high": "OEM interval, complete by the due date, no grace", "other": "AEM allowed, complete by the due date"}[group]
    text = window.describe()
    return f"{_POLICY_LEADS[group]}, complete {text[0].lower()}{text[1:]}"


def policy_disagrees(text: str, window: Window) -> bool:
    """Whether a PM policy line's words plainly contradict the window: word classes, never numbers. By the due date: the text
    mentions a month or a grace period it does not deny. A month window: the text never mentions a month. Days after the due date:
    the text mentions neither days nor a grace period."""
    t = (text or "").lower()
    denies_grace = any(p in t for p in ("no grace", "without grace", "without a grace", "zero grace"))
    grace = "grace" in t and not denies_grace
    if window.is_due_date:
        return "month" in t or grace
    if window.kind in (PmWindow.DUE_MONTH, PmWindow.NEXT_MONTH):
        return "month" not in t
    return not ("day" in t or grace)


__all__ = ["Window", "Windows", "windows", "window_of", "DEFAULT", "HIGH_CLASSES", "WORK_ORDER_RISK", "ASSET_RISK", "PM_WINDOW_DAYS_MAX",
           "PM_POLICY_FIELDS", "policy_default", "policy_disagrees"]
