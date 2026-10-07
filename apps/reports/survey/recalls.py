"""
The survey binder's "Recalls and safety alerts" section (slice 25): every recall or safety notice that touched the facility's inventory
in the period, how it was handled and how quickly, and every one still open today whatever its date.

Received is the facility's day a match reached it in Cadence (AlertMatch.created_at, read with apps.core.days.local_day), never before
the notice's own published date: a notice entered after it was matched (a manufacturer letter typed in later, the demo seed) counts from
when it was published. Read from AlertMatch, the facility's own rows (Alert is shared by every facility: never read on its own here).
The response words are the Recall response log's (apps.reports.operations), and the link is the one it makes to the Recalls screen.

Queries: the matches (with their notice, model, and active device count) and, when any is in progress, one grouped count of their
devices done; whatever the number of matches.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from statistics import median

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.core.days import local_day
from apps.equipment.models import Asset, AssetStatus
from apps.recalls.models import AlertMatch
from apps.recalls.services import DONE_STATUSES, alert_label, fmt_date
from apps.reports.operations import DONE_WO_STATUSES, _response
from apps.workorders.models import WorkOrder

from . import GAP, Figure, Gap, Period, Section, Table

KEY, TITLE = "recalls", "Recalls and safety alerts"
REVIEW_DAYS = 14  # a match still waiting for its first review this many days after it was received is a gap (Cadence's measure)
S = AlertMatch.Status
OPEN = (S.NEEDS_ACTION, S.UNDER_REVIEW, S.IN_PROGRESS)
WAITING = (S.NEEDS_ACTION, S.UNDER_REVIEW)  # not yet acted on: the review is what is late
COLUMNS = ["Received", "Published", "Alert", "Source", "Class", "Manufacturer", "Model", "Devices affected", "Status", "Response",
           "Closed on", "Days to close", "Days open"]


def is_class_one(classification: str) -> bool:
    """A Class I notice (the Recalls screen's rule: "Class I", any case, alone or followed by more words)."""
    text = (classification or "").strip().lower()
    return text == "class i" or text.startswith("class i ")


def _day_start(day):
    """The first moment of the facility's `day` (its time zone is active inside it)."""
    return timezone.make_aware(datetime.combine(day, time.min))


def received_on(match: AlertMatch):
    """The facility's day `match` reached it, never before its notice was published."""
    day = local_day(match.created_at)
    published = match.alert.published_on
    return max(day, published) if published else day


def _matches(period: Period) -> list:
    """The matches received in the period and every one still open, with their received day (`received`), in the binder's order: open
    Class I first, then newest received. The database narrows to a superset (created before the period ends, and on or after it starts
    or published since); the received day decides."""
    start_at, end_at = _day_start(period.start), _day_start(period.end + timedelta(days=1))
    qs = (AlertMatch.objects.select_related("alert", "device_model")
          .filter(Q(status__in=OPEN) | (Q(created_at__lt=end_at) & (Q(created_at__gte=start_at) | Q(alert__published_on__gte=period.start))))
          .annotate(devices=Count("device_model__assets", filter=Q(device_model__assets__status__in=Asset.ACTIVE_STATUSES))))
    out = []
    for m in qs:
        m.received = received_on(m)
        m.in_period = period.start <= m.received <= period.end
        if m.status in OPEN or m.in_period:
            out.append(m)
    out.sort(key=lambda m: (not (m.status in OPEN and is_class_one(m.alert.classification)), -m.received.toordinal(), alert_label(m.alert)))
    return out


def _completed(matches: list) -> dict:
    """{(alert id, device model id): devices with a completed recall work order} for the matches in progress, in one grouped query (the
    Recall response log's count: a retired device no longer counts)."""
    in_progress = [m for m in matches if m.status == S.IN_PROGRESS]
    if not in_progress:
        return {}
    done = (WorkOrder.objects.filter(alert_id__in={m.alert_id for m in in_progress}, status__in=DONE_WO_STATUSES)
            .exclude(asset__status=AssetStatus.RETIRED)
            .order_by().values("alert_id", "asset__device_model_id").annotate(n=Count("asset_id", distinct=True)))
    return {(r["alert_id"], r["asset__device_model_id"]): r["n"] for r in done}


def _response_words(m: AlertMatch, today, completed: dict) -> str:
    if m.status in WAITING:
        return "Open"  # how long it has waited is the row's Days open, counted from received
    return _response(m, today, completed.get((m.alert_id, m.device_model_id), 0))


def _days_to_close(m: AlertMatch):
    return max(0, (m.closed_on - m.received).days) if m.status in DONE_STATUSES and m.closed_on else None


def _days_open(m: AlertMatch, today):
    return max(0, (today - m.received).days) if m.status in OPEN else None


def _row(m: AlertMatch, today, completed: dict) -> list:
    a, dm = m.alert, m.device_model
    return [m.received, a.published_on, alert_label(a), a.get_source_display(), a.classification, dm.manufacturer, dm.model, m.devices,
            m.get_status_display(), _response_words(m, today, completed), m.closed_on, _days_to_close(m), _days_open(m, today)]


def _gap(m: AlertMatch, today) -> Gap:
    a = m.alert
    cls = f"{a.classification.strip()} " if a.classification.strip() else ""
    days = (today - m.received).days
    return Gap(GAP, f"{cls}{alert_label(a)} on {m.device_model} has waited {days} days for a review (received {fmt_date(m.received)}, "
                    f"{m.get_status_display().lower()})",
               url=f"{reverse('web:recalls')}?match={m.pk}", record=alert_label(a))


def build(period: Period, user) -> Section:
    today = period.today
    matches = _matches(period)
    completed = _completed(matches)

    received = [m for m in matches if m.in_period]
    closed = [m for m in received if m.status in DONE_STATUSES]
    close_days = [d for d in (_days_to_close(m) for m in closed) if d is not None]
    still_open = [m for m in matches if m.status in OPEN]
    figures = [
        Figure("Received in the period", len(received), "matches with the facility's devices"),
        Figure("Closed", len(closed), "of those received in the period: closed, or reviewed and not affected"),
        Figure("Median days to close", median(close_days) if close_days else "None closed", "from received to closed"),
        Figure("Open now", len(still_open), "needs action, under review, or in progress, whatever day it was received"),
        Figure("Open Class I", sum(is_class_one(m.alert.classification) for m in still_open), "the most serious recalls"),
    ]
    gaps = [_gap(m, today) for m in matches if m.status in WAITING and (today - m.received).days > REVIEW_DAYS]

    rows = [_row(m, today, completed) for m in matches]
    table = Table(key="matches", title="Recall and alert matches", columns=list(COLUMNS), rows=lambda: iter(rows), count=len(rows),
                  empty="No recall or alert matched the facility's devices in this period, and none is open.")
    return Section(
        key=KEY, title=TITLE,
        topic="That recalls and safety alerts reaching the facility were reviewed and acted on, with what was done and how quickly.",
        covers=f"{period.label}, and every match still open {period.as_of_today}",
        figures=figures, gaps=gaps, tables=[table],
        notes=[
            "Received is the day a notice matched the facility's devices in Cadence, or the day it was published when that is later: a "
            "match is never dated before its notice.",
            "Listed: every match received in the period, and every match still open today (needs action, under review, or in progress) "
            "whatever day it was received. Open Class I recalls come first, then the newest.",
            f"A match that still needs action or is still under review more than {REVIEW_DAYS} days after it was received is listed as a "
            f"gap. {REVIEW_DAYS} days is Cadence's measure of a timely first review, not a rule of the standards.",
            "Devices affected are the model's active devices today (retired ones are left out). Days to close run from received to the "
            "day it was closed or reviewed as not affected; days open, from received to today.",
            "Only notices that matched a device model in Cadence are listed: a notice for equipment the facility does not have never "
            "reaches this binder.",
        ],
    )
