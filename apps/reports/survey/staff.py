"""The survey binder's "Technician qualifications" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="staff", title="Technician qualifications", topic="", covers=period.label)
