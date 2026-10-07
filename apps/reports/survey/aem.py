"""The survey binder's "Alternate equipment maintenance (AEM)" section (slice 25). A stub until its build agent fills it in (see the package docstring)."""
from . import Period, Section


def build(period: Period, user) -> Section:
    return Section(key="aem", title="Alternate equipment maintenance (AEM)", topic="", covers=period.label)
