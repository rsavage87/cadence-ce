"""
The survey binder's "Device incidents" section (slice 28; wave 2 builds it): the incidents in the period and those open at its end,
the reportability decisions, the reports sent against the 10-work-day clock, and the devices held as evidence. Never in_error ones
(a CHECK only). Needs Incidents View (permissions.SURVEY_NEEDS): its rows are facts about a health event.
"""
from __future__ import annotations

from . import Period, Section

KEY, TITLE = "incidents", "Device incidents"


def build(period: Period, user) -> Section:
    return Section(KEY, TITLE, topic="Devices suspected in a death, serious injury, or serious illness: held, investigated, decided, and "
                                     "reported as the Safe Medical Devices Act requires", covers=period.label)
