"""The survey binder's "Medical equipment inventory" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="inventory", title="Medical equipment inventory", topic="", covers=period.label)
