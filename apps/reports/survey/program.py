"""The survey binder's "Program and policies" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="program", title="Program and policies", topic="", covers=period.label)
