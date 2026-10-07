"""The survey binder's "Scheduled maintenance (PM) completion" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="maintenance", title="Scheduled maintenance (PM) completion", topic="", covers=period.label)
