"""The survey binder's "Incoming inspection before first use" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="inspections", title="Incoming inspection before first use", topic="", covers=period.label)
