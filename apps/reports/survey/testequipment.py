"""
The survey binder's test equipment section (slice 35): the CE department's own test equipment and its calibration, as DNV surveyors ask
for it under ISO 9001's control of measuring equipment (calibrated at intervals, its status identified, the test equipment used on the
records, the work done with one later found out of tolerance reviewed). Cadence never says which accreditor asks, and never that a
standard requires it.

A stub until slice 35's wave 2 (build35.md, "Binder"): the tables (test equipment and its standing, the period's calibrations, the work
done with test equipment past due, with no calibration on file, failed, or found out of tolerance later), the GAPs (an item in use past
due or with no calibration on file; a calibration needing a review with none; a decided review whose window now holds more work), the
FINDINGs (one per item and stretch), and the CHECKs (work with readings naming no test equipment since the first item was added; a
calibration adjusted with as found not reported). Built with user=None (the program evaluation's binder_gaps) in a fixed number of
queries; reads history through core.history._rows; never requester text.
"""
from . import Period, Section

KEY, TITLE = "testequipment", "Test equipment calibration"


def build(period: Period, user) -> Section:
    return Section(KEY, TITLE, topic="The CE department's own test equipment: its calibration, and the work done with it.",
                   covers=period.label)
