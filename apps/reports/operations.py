"""
Operations reports (slice 7): technician productivity, recall response log.
Each function returns {"columns": [...], "rows": [[...]], ...}; see the catalog in services.py for the contract.

Both are read-only and aggregate in the database (or in one pass over a values() query) so a large fleet renders in
one round of queries rather than one per technician or per alert.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.urls import reverse

from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus
from apps.facility.services import kpi_targets
from apps.recalls.models import AlertMatch
from apps.recalls.services import alert_label
from apps.workorders.models import OPEN_STATUSES, LaborLine, WorkOrder, WoStatus, WoType

TECH_DAYS = 30  # the mock's "last 30 days" window for technician productivity
DONE_WO_STATUSES = (WoStatus.COMPLETED, WoStatus.CLOSED)


def report_tech(today: date) -> dict:
    """One row per active technician: work orders they closed in the last 30 days (in-house only), split by type, with
    hours logged, PM on-time share, mean repair turnaround, and what they hold open now. Follows the mock's `tech` report;
    ours has no batch work order to exclude (recall batches are one work order per device)."""
    since = today - timedelta(days=TECH_DAYS)
    technicians = list(Technician.objects.filter(is_active=True))
    stats = {t.pk: {"closed": 0, "pms": 0, "repairs": 0, "pm_on_time": 0, "turnaround_total": 0, "hours": 0.0, "open": 0} for t in technicians}

    # One pass over the closed work orders in the window: the counts and the date math (turnaround, on time) need
    # per-row dates, which do not aggregate portably across SQLite and Postgres.
    closed = (WorkOrder.objects.filter(assigned_to__in=technicians, vendor_service=False, status__in=DONE_WO_STATUSES,
                                       completed_on__gte=since, completed_on__lte=today)
              .values("assigned_to_id", "type", "opened_on", "due_on", "completed_on"))
    for w in closed:
        s = stats[w["assigned_to_id"]]
        s["closed"] += 1
        if w["type"] == WoType.PM:
            s["pms"] += 1
            s["pm_on_time"] += int(w["completed_on"] <= w["due_on"])
        elif w["type"] == WoType.REPAIR:
            s["repairs"] += 1
            # The service refuses to complete before opening; clamp anyway so a bad row can never pull the average below zero.
            s["turnaround_total"] += max(0, (w["completed_on"] - w["opened_on"]).days)
    # Hours by who logged them (each labor line names its technician; vendor time names none), on work orders closed in the window:
    # time logged before a reassignment stays with the technician who did it, and a vendor's time is never anyone's.
    hours = (LaborLine.objects.filter(technician__in=technicians, work_order__status__in=DONE_WO_STATUSES,
                                      work_order__completed_on__gte=since, work_order__completed_on__lte=today)
             .order_by().values("technician_id").annotate(h=Sum("hours")))
    for row in hours:
        stats[row["technician_id"]]["hours"] = float(row["h"] or 0)
    open_now = (WorkOrder.objects.filter(assigned_to__in=technicians, vendor_service=False, status__in=OPEN_STATUSES)
                .order_by().values("assigned_to_id").annotate(n=Count("id")))
    for row in open_now:
        stats[row["assigned_to_id"]]["open"] = row["n"]

    pm_target = kpi_targets()["pm_on_time"]  # the tenant's PM completion target (Settings), the mock's 95% by default
    items, rows = [], []
    for t in technicians:
        s = stats[t.pk]
        pm_on_time_pct = s["pm_on_time"] / s["pms"] * 100 if s["pms"] else 100.0
        turnaround = s["turnaround_total"] / s["repairs"] if s["repairs"] else 0.0
        items.append({"technician": t, "closed": s["closed"], "pms": s["pms"], "repairs": s["repairs"], "hours": s["hours"],
                      "pm_on_time_pct": pm_on_time_pct, "turnaround": turnaround, "open": s["open"],
                      # exact, like the compliance report: 2 of 3 PMs (66.67%) against a 66.7 target is a miss, 3 of 3 is not
                      "pm_meets": Decimal(s["pm_on_time"] * 100) >= Decimal(str(pm_target)) * s["pms"] if s["pms"] else True})
        rows.append([t.name, t.title, s["closed"], s["pms"], s["repairs"], s["hours"], pm_on_time_pct, turnaround, s["open"]])
    return {"columns": ["Technician", "Title", "Closed", "PMs", "Repairs", "Hours logged", "PM on time %", "Avg repair turnaround days", "Open now"],
            "rows": rows, "technicians": items, "since": since, "days": TECH_DAYS, "pm_target": pm_target}


def _response(match: AlertMatch, today: date, completed: int) -> str:
    """The mock's response column: what was done, or how long the alert has been waiting."""
    S = AlertMatch.Status
    if match.status == S.CLOSED:
        return match.disposition_note or "Closed"
    if match.status == S.NOT_AFFECTED:
        return match.disposition_note or "Reviewed, not affected"
    if match.status == S.IN_PROGRESS:
        return f"{completed} of {match.devices} devices completed"
    published = match.alert.published_on
    return f"Open {(today - published).days} d" if published else "Open"


def report_recall(today: date) -> dict:
    """The surveyor's log: every alert that matched the inventory, when it arrived, how many devices it touched, its
    disposition, and what was done. Newest first; alerts with no published date sort last on every database (a bare
    `-alert__published_on` puts NULLs first on PostgreSQL and last on SQLite)."""
    matches = list(AlertMatch.objects.select_related("alert", "device_model")
                   .annotate(devices=Count("device_model__assets", filter=Q(device_model__assets__status__in=Asset.ACTIVE_STATUSES)))
                   .order_by(F("alert__published_on").desc(nulls_last=True), "alert__external_id"))
    # Progress for the in-progress matches in one grouped query: devices with a completed recall work order for that
    # alert, the same count apps.recalls.services.progress makes per match (a retired device no longer counts either way).
    in_progress = [m for m in matches if m.status == AlertMatch.Status.IN_PROGRESS]
    completed = {}
    if in_progress:
        done = (WorkOrder.objects.filter(alert_id__in={m.alert_id for m in in_progress}, status__in=DONE_WO_STATUSES)
                .exclude(asset__status=AssetStatus.RETIRED)
                .order_by().values("alert_id", "asset__device_model_id").annotate(n=Count("asset_id", distinct=True)))
        completed = {(r["alert_id"], r["asset__device_model_id"]): r["n"] for r in done}

    recalls_url = reverse("web:recalls")
    items, rows = [], []
    for m in matches:
        a, dm = m.alert, m.device_model
        response = _response(m, today, completed.get((m.alert_id, m.device_model_id), 0))
        items.append({"match": m, "alert": a, "devices": m.devices, "response": response, "url": f"{recalls_url}?match={m.pk}"})
        rows.append([a.published_on, alert_label(a), a.get_source_display(), a.classification, dm.manufacturer, dm.model,
                     m.devices, m.get_status_display(), response, m.closed_on])
    return {"columns": ["Received", "Alert", "Source", "Class", "Manufacturer", "Model", "Affected devices", "Status", "Response", "Closed on"],
            "rows": rows, "matches": items, "count": len(items),
            "has_sample": any(isinstance(a.raw, dict) and a.raw.get("demo") for a in (m.alert for m in matches))}
