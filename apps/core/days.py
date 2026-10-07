"""
The facility's business day of a timestamp (slice 25, moved here from apps.pm.aem). Audit timestamps (created_at, a history row's
history_date) are aware datetimes; the day they fall on is the facility's, so read them inside its tenant_context (its time zone is
active there), never with .date(), which is UTC's day.
"""
from datetime import date

from django.utils import timezone


def local_day(moment) -> date:
    """The facility's day of `moment` (an aware datetime); a naive one is taken as already local."""
    return timezone.localdate(moment) if timezone.is_aware(moment) else moment.date()
