"""
Match alerts to a tenant's catalog. Matching is deliberately simple (manufacturer + model fragment);
a false positive costs a reviewer a minute, a false negative can cost a patient.

The second half is the disposition workflow behind the Recalls screen and the API: status moves,
the recall work-order batch, and the grouped lists. Views never set these fields themselves.
"""
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import NamedTuple

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.utils import timezone

from apps.equipment.models import Asset, DeviceModel
from apps.notifications import assignments
from apps.workorders.models import OPEN_STATUSES, Priority, Source, WorkOrder, WoStatus, WoType

from .models import Alert, AlertMatch


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def match_alert(alert: Alert) -> int:
    """Create AlertMatch rows for every device model in the current tenant the alert could apply to."""
    created = 0
    mfr = _norm(alert.manufacturer)
    terms = [_norm(t) for t in alert.model_terms if t] or [_norm(alert.product)]
    for dm in DeviceModel.objects.all():
        if _norm(dm.manufacturer) not in mfr and mfr not in _norm(dm.manufacturer):
            continue
        model_norm = _norm(dm.model)
        if any(t and (t in model_norm or model_norm in t) for t in terms):
            _, was_created = AlertMatch.objects.get_or_create(alert=alert, device_model=dm)
            created += int(was_created)
    return created


def match_all_open_alerts() -> int:
    return sum(match_alert(a) for a in Alert.objects.all())


def rematch() -> int:
    """Re-run matching for the current tenant, e.g. after a device model was added; returns the number of new matches."""
    return match_all_open_alerts()


# --- disposition --------------------------------------------------------------------------------------

S = AlertMatch.Status
DONE_STATUSES = (S.CLOSED, S.NOT_AFFECTED)
ALLOWED_TRANSITIONS = {
    S.NEEDS_ACTION: {S.UNDER_REVIEW, S.IN_PROGRESS, S.NOT_AFFECTED},
    S.UNDER_REVIEW: {S.IN_PROGRESS, S.NOT_AFFECTED, S.NEEDS_ACTION},
    S.IN_PROGRESS: {S.CLOSED, S.UNDER_REVIEW},
    S.CLOSED: {S.UNDER_REVIEW},
    S.NOT_AFFECTED: {S.UNDER_REVIEW},
}
RECALL_DUE_DAYS = 14
RECALL_ESTIMATED_HOURS = Decimal("0.5")


def alert_label(alert: Alert) -> str:
    """"FDA Z-2026-4408" (the same wording as the `alert_label` template filter)."""
    return f"{alert.get_source_display()} {alert.external_id}"


def fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _who(by) -> str:
    return (by.get_full_name() or by.username) if by else ""


@transaction.atomic
def set_status(match: AlertMatch, to_status: str, by=None, note: str = "", today: date | None = None) -> AlertMatch:
    today = today or timezone.localdate()
    if to_status not in ALLOWED_TRANSITIONS[match.status]:
        label = dict(S.choices).get(to_status, to_status)
        raise ValidationError(f"Cannot move {alert_label(match.alert)} from {match.get_status_display().lower()} to {str(label).lower()}.")
    note = (note or "").strip()
    if to_status in DONE_STATUSES:
        match.closed_on = today
        if not note:
            # The disposition note is what a surveyor reads; without one, record who closed it and when.
            verb = "Closed" if to_status == S.CLOSED else "Reviewed, not affected"
            who = _who(by)
            note = f"{verb} {fmt_date(today)}" + (f" by {who}" if who else "")
    elif match.status in DONE_STATUSES:
        match.closed_on = None  # reopening keeps the note: it is the history of the last closure
    match.status = to_status
    if note:
        match.disposition_note = note
    match.save()
    return match


class RecallBatch(NamedTuple):
    created: int
    unassigned: int  # of the created ones, how many found no credentialed technician


def recall_work_orders(match: AlertMatch):
    """Work orders opened for this alert on this device model (cancelled ones no longer stand for a device)."""
    return WorkOrder.objects.filter(alert=match.alert, asset__device_model=match.device_model).exclude(status=WoStatus.CANCELLED)


@transaction.atomic
def create_recall_work_orders(match: AlertMatch, by=None, today: date | None = None) -> RecallBatch:
    """One high-priority recall work order per active device that has none for this alert yet (a completed one still
    counts: the device was done; only a cancelled one is redone), assigned to the first credentialed technician when
    there is one. Moves the match to in progress."""
    from apps.credentials.services import qualified_technicians
    from apps.workorders.services import assign, create_work_order

    today = today or timezone.localdate()
    # The match's row first, locked: two batches at once (two coordinators, the screen and the API, a retry) would otherwise each
    # read no recall work orders yet and each create a full set (PostgreSQL; SQLite runs one writer at a time).
    list(AlertMatch.objects.select_for_update().filter(pk=match.pk).values_list("pk", flat=True))  # the lock; then the copy as it is now
    match.refresh_from_db()
    if match.status != S.IN_PROGRESS and S.IN_PROGRESS not in ALLOWED_TRANSITIONS[match.status]:
        raise ValidationError(f"Reopen {alert_label(match.alert)} before creating work orders.")
    assets = list(match.affected_assets().select_related("device_model", "department", "tenant").order_by("tag"))
    if not assets:
        raise ValidationError(f"No active devices match {alert_label(match.alert)}.")
    covered = set(recall_work_orders(match).filter(asset__in=assets).values_list("asset_id", flat=True))
    alert = match.alert
    problem = f"{alert_label(alert)}: {alert.title}. {alert.action}".strip()
    # Qualification depends only on the device model, which every affected device shares: rank the technicians once.
    qualified = qualified_technicians(assets[0], today)
    technician = qualified[0][0] if qualified else None
    created = 0
    with assignments.batch():  # the technician hears once, listing every recall work order, not once per device
        for asset in assets:
            if asset.id in covered:
                continue
            wo = create_work_order(asset=asset, type=WoType.RECALL, priority=Priority.HIGH, problem=problem, requester="Recall coordinator",
                                   source=Source.RECALL, opened_on=today, due_on=today + timedelta(days=RECALL_DUE_DAYS), created_by=by,
                                   estimated_hours=RECALL_ESTIMATED_HOURS, alert=alert)
            if technician is not None:
                assign(wo, technician=technician, by=by)
            created += 1
    if match.status != S.IN_PROGRESS:
        set_status(match, S.IN_PROGRESS, by=by, today=today)
    return RecallBatch(created, 0 if technician is not None else created)


def unassigned_recall_work_orders(match: AlertMatch) -> int:
    return recall_work_orders(match).filter(status__in=OPEN_STATUSES, assigned_to__isnull=True, vendor_service=False).count()


def progress(match: AlertMatch) -> dict:
    """Devices done over devices affected (the bar's "X of N devices"), so cancelled or duplicate work orders cannot skew it."""
    assets = match.affected_assets()
    total = assets.count()
    completed = (recall_work_orders(match).filter(asset__in=assets, status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))
                 .values("asset_id").distinct().count())
    return {"total": total, "completed": completed, "pct": completed / total * 100 if total else 0.0}


def department_summary(match: AlertMatch, limit: int = 4) -> str:
    """"ICU 3, ED 2": the departments with the most affected devices (the mock's deptSum)."""
    rows = match.affected_assets().values("department__name").annotate(n=Count("id")).order_by("-n", "department__name")[:limit]
    return ", ".join(f"{r['department__name']} {r['n']}" for r in rows)


# --- lists for the Recalls screen ----------------------------------------------------------------------

VIEW_GROUPS = {"all": None, "action": (S.NEEDS_ACTION, S.UNDER_REVIEW), "progress": (S.IN_PROGRESS,), "closed": DONE_STATUSES}
VIEW_LABELS = [("all", "All alerts"), ("action", "Needs action or review"), ("progress", "In progress"), ("closed", "Closed")]


def filter_matches(view: str = "all"):
    statuses = VIEW_GROUPS.get(view)
    qs = (AlertMatch.objects.select_related("alert", "device_model")
          .annotate(devices=Count("device_model__assets", filter=Q(device_model__assets__status__in=Asset.ACTIVE_STATUSES))))
    if statuses:
        qs = qs.filter(status__in=statuses)
    return qs.order_by(F("alert__published_on").desc(nulls_last=True), "alert__external_id")  # NULLs would sort first on Postgres otherwise


def group_counts() -> dict:
    by_status = Counter(AlertMatch.objects.values_list("status", flat=True))
    return {view: sum(by_status[s] for s in statuses) if statuses else sum(by_status.values()) for view, statuses in VIEW_GROUPS.items()}


def feed_imported_at():
    """When the newest FDA notice arrived (the only feed connected); None when nothing has been imported."""
    return Alert.objects.filter(source=Alert.Source.FDA).aggregate(at=Max("created_at"))["at"]
