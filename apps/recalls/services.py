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

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.utils import timezone

from apps.equipment.models import Asset, DeviceModel
from apps.notifications import assignments
from apps.workorders.models import OPEN_STATUSES, Priority, Source, WorkOrder, WoStatus, WoType

from .models import Alert, AlertMatch
from .permissions import transition_level


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
    return str(by) if by else ""


CLOSE_PERMISSION = "Closing or reopening an alert needs Approve access."
STATUS_FIELDS = ("status", "closed_on", "disposition_note")  # what set_status writes, and nothing else
STATUS_BY_TRANSITION = "Use POST /api/v1/alert-matches/{id}/transition/ to change the status."


def level_refusal(match: AlertMatch) -> str:
    """Why a move needs Approve, from the match's status as it is: a closed one is reopened ("FDA Z-2026-4408 is reviewed, not
    affected: reopening it needs Approve access."), else closing it does."""
    if match.status in DONE_STATUSES:
        return f"{alert_label(match.alert)} is {match.get_status_display().lower()}: reopening it needs Approve access."
    return CLOSE_PERMISSION


def _locked(match: AlertMatch) -> AlertMatch:
    """The match's row as it is now, locked (its own row only), with its alert."""
    return AlertMatch.objects.select_for_update(of=("self",)).select_related("alert").get(pk=match.pk)


@transaction.atomic
def set_status(match: AlertMatch, to_status: str, by=None, note: str = "", today: date | None = None, level: int | None = None) -> AlertMatch:
    """Move a match along ALLOWED_TRANSITIONS, on its row as it is now, locked (never the caller's copy, which may predate another
    person's closure or reopen); only STATUS_FIELDS are written, and the caller's copy follows. `level`: the Recalls level of the person
    moving it (the screen, the API), who was checked against the match as they read it (permissions.can_transition): checked again on
    the locked row after the transition itself, so a match closed or reopened meanwhile never moves at a level its move now needs more
    than (PermissionDenied, CLOSE_PERMISSION). None: never checked here (the recall work order batch, which locks and checks the match
    itself, and the seed)."""
    today = today or timezone.localdate()
    current = _locked(match)
    if to_status not in ALLOWED_TRANSITIONS[current.status]:
        label = dict(S.choices).get(to_status, to_status)
        raise ValidationError(f"Cannot move {alert_label(current.alert)} from {current.get_status_display().lower()} to {str(label).lower()}.")
    if level is not None and level < transition_level(current.status, to_status):
        raise PermissionDenied(level_refusal(current))
    note = (note or "").strip()
    if to_status in DONE_STATUSES:
        current.closed_on = today
        if not note:
            # The disposition note is what a surveyor reads; without one, record who closed it and when.
            verb = "Closed" if to_status == S.CLOSED else "Reviewed, not affected"
            who = _who(by)
            note = f"{verb} {fmt_date(today)}" + (f" by {who}" if who else "")
    elif current.status in DONE_STATUSES:
        current.closed_on = None  # reopening keeps the note: it is the history of the last closure
    current.status = to_status
    if note:
        current.disposition_note = note
    current.save(update_fields=[*STATUS_FIELDS, "updated_at"])
    for field in STATUS_FIELDS:
        setattr(match, field, getattr(current, field))
    return match


@transaction.atomic
def set_note(match: AlertMatch, note: str, *, status: str | None = None) -> AlertMatch:
    """The API's edit of a match's disposition note (Approve, its door's level), on the row as it is now, locked as set_status locks it:
    only the note is written, so a closure or move committed after the caller read the match is never written back over. `status`, when
    the caller sent one, must be the stored one (a status changes only through set_status: refused, keyed status). The caller's copy
    follows."""
    current = _locked(match)
    if status is not None and status != current.status:
        raise ValidationError({"status": STATUS_BY_TRANSITION})
    current.disposition_note = note
    current.save(update_fields=["disposition_note", "updated_at"])
    for field in STATUS_FIELDS:
        setattr(match, field, getattr(current, field))
    return match


class RecallBatch(NamedTuple):
    created: int
    unassigned: int  # of the created ones, how many were left with nobody (no credentialed technician; slice 29: never vendor service)


def recall_work_orders(match: AlertMatch):
    """Work orders opened for this alert on this device model (cancelled ones no longer stand for a device)."""
    return WorkOrder.objects.filter(alert=match.alert, asset__device_model=match.device_model).exclude(status=WoStatus.CANCELLED)


@transaction.atomic
def create_recall_work_orders(match: AlertMatch, by=None, today: date | None = None) -> RecallBatch:
    """One high-priority recall work order per active device that has none for this alert yet (a completed one still
    counts: the device was done; only a cancelled one is redone), assigned to the first credentialed technician when
    there is one. Moves the match to in progress.

    Slice 29: a temporary device's (a rental, vendor loaner, or demo unit: equipment.services.owner_maintains) goes to vendor service
    named for its owner (equipment.services.service_vendor), never to an in-house technician: the owner corrects their own device.
    Recalls count every device on site, ours or not."""
    from apps.credentials.services import qualified_technicians
    from apps.equipment.services import owner_maintains, service_vendor
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
    created = unassigned = 0
    with assignments.batch():  # the technician hears once, listing every recall work order, not once per device
        for asset in assets:
            if asset.id in covered:
                continue
            wo = create_work_order(asset=asset, type=WoType.RECALL, priority=Priority.HIGH, problem=problem, requester="Recall coordinator",
                                   source=Source.RECALL, opened_on=today, due_on=today + timedelta(days=RECALL_DUE_DAYS), created_by=by,
                                   estimated_hours=RECALL_ESTIMATED_HOURS, alert=alert)
            created += 1
            if owner_maintains(asset):
                assign(wo, vendor_name=service_vendor(asset), by=by)
            elif technician is not None:
                assign(wo, technician=technician, by=by)
            else:
                unassigned += 1
    if match.status != S.IN_PROGRESS:
        set_status(match, S.IN_PROGRESS, by=by, today=today)
    return RecallBatch(created, unassigned)


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
