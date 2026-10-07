"""The survey binder's "Recalls and safety alerts" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="recalls", title="Recalls and safety alerts", topic="", covers=period.label)
