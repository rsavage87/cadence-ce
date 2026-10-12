"""
Slice 32: program evaluations for tests (and the seed's shape), made through apps.evaluations.services inside a facility. Import from any
test file: `from evaluation_fixtures import last_year, draft, sent, approved`. `by=None` is the system (no level checks), as the seed.
"""
from datetime import date, timedelta

from django.utils import timezone

from apps.evaluations import services
from apps.evaluations.models import Body, ObjectiveKind, ObjectiveResult
from apps.pm.dates import add_months


def last_year(today: date | None = None) -> tuple[date, date]:
    """The 12 months ending at the end of last month: (start, end)."""
    today = today or timezone.localdate()
    first = today.replace(day=1)
    return add_months(first, -12), first - timedelta(days=1)


def year_before(start: date) -> date:
    """The start of the 12 months before the period starting `start`."""
    return add_months(start, -12)


def draft(*, by=None, start: date | None = None, today: date | None = None):
    """A draft of the 12 months from `start` (last_year's by default), its first figures taken."""
    today = today or timezone.localdate()
    return services.create_draft(period_start=start or last_year(today)[0], by=by, today=today)


def results_for_all(ev, *, by=None, result=ObjectiveResult.MET):
    """Record `result` on every one of this period's objectives still without one."""
    for obj in ev.objectives.filter(kind=ObjectiveKind.REVIEW, result=""):
        services.update_objective(obj, result=result, by=by)


def sent(*, by=None, start: date | None = None, today: date | None = None, ev=None):
    """A draft (`ev`, or a new one) sent to committee today, a result recorded on each of this period's objectives first."""
    today = today or timezone.localdate()
    ev = ev or draft(by=by, start=start, today=today)
    results_for_all(ev, by=by)
    return services.send_to_committee(ev, by=by, today=today)


def approved(*, by=None, start: date | None = None, today: date | None = None, meeting_on: date | None = None, body=Body.EOC, ev=None,
             next_objectives=()):
    """An approved evaluation: sent to committee on the day of the meeting, `meeting_on` (today by default), and approved there.
    `next_objectives` are added as next period's objectives before it is sent."""
    today = today or timezone.localdate()
    ev = ev or draft(by=by, start=start, today=today)
    for text in next_objectives:
        services.add_objective(ev, kind=ObjectiveKind.NEXT, text=text, target="", by=by)
    ev = sent(by=by, today=meeting_on or today, ev=ev)
    services.record_approval(ev, body=body, meeting_on=meeting_on or today, minutes_reference="EOC-1", by=by, today=today)
    ev.refresh_from_db()
    return ev
