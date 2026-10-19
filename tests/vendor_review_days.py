"""
The last day a vendor review's 12 months may end, for slice 34's tests. Import it as `from vendor_review_days import ended` (tests/ is
on the path: it has no __init__.py).

vendor_reviews refuses a period ending on Feb 28 of a leap year (leap_feb_28: the next 12 months could not start on Feb 29), so a test
that drafts a review with an end counted from today (today - 40 days, yesterday, a span fixture's end) passes it through `ended`, and
derives whatever it asserts about that end (a period label, a next due day) from the same value: on every calendar day it then drafts
an end the service takes. Ends written as fixed dates, and the tests of the refusal itself, stay as they are.
"""
from datetime import date, timedelta

from apps.evaluations.vendor_reviews import leap_feb_28


def ended(d: date) -> date:
    """The last day of 12 months that may end on or before `d`: `d`, or the day before when `d` is Feb 28 of a leap year."""
    return d - timedelta(days=1) if leap_feb_28(d) else d
