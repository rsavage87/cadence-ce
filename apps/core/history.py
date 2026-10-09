"""
Change history (slice 20): what changed on a record, when, who changed it, and why, read from django-simple-history's tables and
put in plain words for the drawers' History tabs, the Users and access tab's change log, and the API's change log.

AREAS lists every audited kind of record: its label, the module whose View it needs (a reader sees only the areas their role can
view), how to name one record, and where it opens. Fields that tell a reader nothing are left out: ids, the tenant, the row's own
timestamps, and per model (HIDE) a field that names the record itself (a device's tag, a work order's number), one that only
mirrors another (a device's support type follows its contract; a work order's creator is the entry's "who"), and a line's work
order (the record's name says it). Each field reads under its label in LABELS (else its verbose name, with PM, OEM, AEM, and PO in
capitals) and in its own way (KINDS: money as "$3,437.31", hours, months, years, a 1-to-5 condition, a procedure's checklist as its
number of steps, a PM's recorded checklist as its results, an AEM case's evidence as the failure history in a sentence, a custom
report's columns, filters, grouping, and sort as the builder words them); otherwise a foreign key reads as the record it names (one
deleted since by its last name on record, "(deleted)"), a choice as its label, a yes/no as Yes or No, a date as "Oct 5, 2026", an
empty value as "—", and long text is cut at TEXT_LIMIT characters. When two values read the same but differ (a checklist reworded,
long text changed past the cut), the new one says "(edited)".

Each entry is one save: "added" (with the values it started with), "changed" (each changed field's before and after), or "deleted"
(with the values it had, in `after` as for "added"), with the user simple_history recorded (the request's user on the web and the
API, or the `by` a service set as `_history_user`; nobody for a job or an import: "Cadence") and the reason a service gave
(history_change_reason; one that only repeats what the entry says is dropped: the action, "Added" or "Edited", the fields it lists,
"Edited: columns, sort", or a change's new value, "Status: In service"). A change that touched none of the shown fields is left
out. Access changes (who may do what: apps.accounts.models.AccessEvent) are not simple_history rows; the change log reads them as
the "access" area.

One record's history (record_history, the drawers' History tabs) can carry the rows that belong to it, interleaved by time: a work
order's labor and part lines, a device model's AEM cases. They are found by the historical rows' own foreign key, so a line removed
since keeps its history there. Pages count saves (`offset`), and a page reads on past saves that show nothing until it is full.

Everything here reads inside the request's tenant. The historical tables carry tenant_id, so row-level security applies to them on
PostgreSQL, but their managers are simple_history's, not the tenant-scoped one: every query here filters on the current tenant itself
(_rows), and reads nothing when there is none. The facility's own history (the "facility" area, slice 21: its time zone, its name)
has no tenant_id column and no policy, so _rows reads it by the current facility's id, never another facility's rows. Callers check
who may read what (can_read, readable_areas): scoped users (apps.workorders.scoping: a vendor technician, a clinical requester) see
no history, and readable_areas gives them no area.
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import models
from django.db.models import OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from apps.tenants.context import get_current_tenant

HIDDEN = frozenset({"id", "tenant", "created_at", "updated_at"})
TEXT_LIMIT = 200
EMPTY = "—"
SYSTEM = "Cadence"  # who changed it when nobody did: the nightly jobs, the importer, the seed
DELETED = "(deleted)"  # a foreign key to a row gone since, with no name on record
EDITED = " (edited)"
MAX_ROUNDS = 5  # record_history reads at most this many windows of saves to fill a page (saves that show nothing are skipped)


@dataclass
class Change:
    field: str  # the field's label
    before: str
    after: str


@dataclass
class Entry:
    at: datetime
    area: str  # a key of AREAS
    area_label: str
    record: str  # the record in a few words: "CE-10241", "WO-26-0042", "SC-2291 · Philips"
    url: str | None  # where the record opens, when it has a page of its own
    who: str
    who_id: int | None
    action: str  # "added", "changed", "deleted"
    reason: str = ""
    changes: list[Change] = field(default_factory=list)

    @property
    def action_label(self) -> str:
        return self.action.capitalize()


@dataclass(frozen=True)
class Area:
    key: str
    label: str
    model_path: str  # "app_label.Model"
    module: str  # apps.accounts.models.Module value: the module whose View reading this area needs
    name: object  # (historical record) -> str
    url: object = None  # (historical record) -> str | None
    select: tuple = ()  # the historical rows' foreign keys `name` and `url` read, joined in the same query
    url_module: str | None = None  # the module whose View following `url` needs, when it is not `module` (the change log drops the link)

    @property
    def model(self):
        from django.apps import apps

        return apps.get_model(self.model_path)


def _safe(fn):
    """A name or link that reads a related row the history no longer finds (deleted since) gives "" rather than failing the page."""
    def wrapped(rec):
        try:
            return fn(rec)
        except Exception:
            return ""
    return wrapped


def _last_name(model_path: str, pk, namer) -> str:
    """A related row deleted since, by its last name on record in this tenant ("(deleted)" after it), or "(deleted)" alone."""
    from django.apps import apps

    last = _rows(apps.get_model(model_path).history.model).filter(id=pk).order_by("-history_date", "-history_id").first()
    return f"{namer(last)} {DELETED}" if last is not None else DELETED


def _wo_number(rec) -> str:
    wo = rec.work_order
    return wo.number if wo is not None else _last_name("workorders.WorkOrder", rec.work_order_id, lambda r: r.number)


def _wo_url(rec):
    return reverse("web:wo", args=[rec.work_order.number]) if rec.work_order is not None else None


def _aem_name(rec) -> str:
    dm = rec.device_model
    if dm is None:  # the model was deleted since (its AEM cases with it): the LEFT JOIN gives None, not an error
        return "AEM · " + _last_name("equipment.DeviceModel", rec.device_model_id, lambda r: f"{r.manufacturer} {r.model}")
    return f"AEM · {dm.manufacturer} {dm.model}"


def _aem_url(rec):
    return reverse("web:pm_model", args=[rec.device_model_id]) + "?tab=aem" if rec.device_model is not None else None


AREAS = {a.key: a for a in (
    Area("devices", "Devices", "equipment.Asset", "equipment", _safe(lambda r: r.tag), _safe(lambda r: reverse("web:asset", args=[r.tag]))),
    Area("device_models", "Device models", "equipment.DeviceModel", "equipment", _safe(lambda r: f"{r.manufacturer} {r.model}"),
         _safe(lambda r: reverse("web:pm_model", args=[r.id])), url_module="pm"),  # the model drawer is the PM program's (PM View)
    Area("work_orders", "Work orders", "workorders.WorkOrder", "workorders", _safe(lambda r: r.number), _safe(lambda r: reverse("web:wo", args=[r.number]))),
    Area("labor", "Labor", "workorders.LaborLine", "workorders", _safe(lambda r: f"{_wo_number(r)} labor"), _safe(_wo_url),
         select=("work_order",)),
    Area("parts", "Parts", "workorders.PartLine", "workorders", _safe(lambda r: f"{_wo_number(r)} part"), _safe(_wo_url),
         select=("work_order",)),
    Area("contracts", "Contracts", "contracts.Contract", "contracts", _safe(lambda r: f"{r.reference} · {r.vendor}"),
         _safe(lambda r: reverse("web:contract", args=[r.id]))),
    Area("procedures", "PM procedures", "pm.PmProcedure", "pm", _safe(lambda r: r.code)),
    Area("aem", "AEM decisions", "pm.AemDecision", "pm", _safe(_aem_name), _safe(_aem_url), select=("device_model",)),
    Area("credentials", "Credentials", "credentials.Credential", "users", _safe(lambda r: f"{r.technician} · credential"), select=("technician",)),
    Area("roles", "Roles", "accounts.Role", "users", _safe(lambda r: r.name), _safe(lambda r: reverse("web:roles"))),
    Area("settings", "Settings", "facility.FacilitySettings", "settings", _safe(lambda r: "Settings"), _safe(lambda r: reverse("web:settings"))),
    # Slice 21: the facility's own record (its time zone, a new name), read as Settings: the time zone is set on that screen.
    Area("facility", "Facility", "tenants.Tenant", "settings", _safe(lambda r: r.name), _safe(lambda r: reverse("web:settings"))),
    Area("custom_reports", "Custom reports", "reports.CustomReport", "reports", _safe(lambda r: r.name),
         _safe(lambda r: reverse("web:report", args=[f"custom-{r.id}"]))),
    # Slice 28: device incidents and their holds, read with Incidents View (the device's own history never names the incident).
    Area("incidents", "Incidents", "incidents.Incident", "incidents", _safe(lambda r: r.number),
         _safe(lambda r: reverse("web:incident", args=[r.number]))),
    Area("incident_holds", "Incident holds", "incidents.IncidentHold", "incidents", _safe(lambda r: f"{r.incident.number} hold · {r.asset.tag}"),
         _safe(lambda r: reverse("web:incident", args=[r.incident.number])), select=("incident", "asset")),
)}
ACCESS = Area("access", "Access", "accounts.AccessEvent", "users", _safe(lambda e: str(e.user) if e.user_id else ""))
ALL_AREAS = {**AREAS, ACCESS.key: ACCESS}


FACILITY_HISTORY = "tenants.historicaltenant"


def _rows(history_model):
    """`history_model`'s rows in the current tenant only (none without one): its manager is not the tenant-scoped one. The
    facility's own history (slice 21) has no tenant_id column, and row-level security does not cover it: its rows are the current
    facility's by id, never another facility's."""
    tenant = get_current_tenant()
    qs = history_model.objects.all()
    if tenant is None:
        return qs.none()
    if history_model._meta.label_lower == FACILITY_HISTORY:
        return qs.filter(id=tenant.id)
    return qs.filter(tenant_id=tenant.id)


def area_of(model) -> Area | None:
    label = f"{model._meta.app_label}.{model.__name__}"
    return next((a for a in AREAS.values() if a.model_path.lower() == label.lower()), None)


def _is_scoped(user) -> bool:
    from apps.workorders import scoping  # workorders builds on core; imported here so core loads first

    return scoping.is_scoped(user)


def readable_areas(user) -> list[Area]:
    """The areas `user`'s role can view, in AREAS' order (access last). None for a scoped user: history is never theirs."""
    if _is_scoped(user):
        return []
    return [a for a in ALL_AREAS.values() if user.has_level(a.module, 1)]


def can_read(user, area_key: str) -> bool:
    """`user` may read the history of `area_key`'s records: View on its module, and not scoped (what the History tabs check)."""
    area = ALL_AREAS.get(area_key)
    return area is not None and not _is_scoped(user) and user.has_level(area.module, 1)


# --- how each field reads ----------------------------------------------------------------------------------------------------

# Fields left out per model, beyond HIDDEN: the record's own name (shown as the entry's record), mirrors of another field, and the
# work order a line belongs to (in the record's name).
HIDE = {
    "equipment.asset": {"tag", "support_type"},  # support type always follows the contract (Asset.save), which is shown
    "workorders.workorder": {"number", "created_by"},  # the creator is the "added" entry's who
    "workorders.laborline": {"work_order"},
    "workorders.partline": {"work_order"},
    "pm.aemdecision": {"device_model"},  # the record: "AEM · <model>"
    "accounts.role": {"slug"},  # follows the name; never shown on a screen
    "reports.customreport": {"created_by"},
    "incidents.incident": {"number", "created_by"},  # the record; the creator is the "added" entry's who
    "incidents.incidenthold": {"incident", "asset"},  # in the record's name
}

# Labels that read better than the verbose names, per model; `_label` falls back to the verbose name.
LABELS = {
    "equipment.asset": {"serial": "Serial number", "installed_on": "Installed", "warranty_end": "Warranty ends", "last_pm_on": "Last PM",
                        "next_pm_on": "Next PM", "awaiting_inspection": "Waiting for incoming inspection",
                        "incident_hold": "Held for an incident investigation", "ownership": "Whose", "owner": "Owner",
                        "owner_reference": "Agreement, PO, or RMA number", "arrived_on": "Arrived", "due_back_on": "Due back",
                        "owner_pm_due_on": "Owner's PM due", "returned_on": "Returned to owner", "kept_on": "Kept by the facility",
                        "stands_in_for": "Vendor loaner for", "return_cleaning": "Cleaning when returned",
                        "return_data": "Patient data when returned"},
    "equipment.devicemodel": {"oem_pm_interval_months": "OEM PM interval", "aem_interval_months": "AEM interval", "expected_life_years": "Expected life",
                              "list_cost": "List price", "pm_procedure": "PM procedure", "risk_function": "Risk: clinical function",
                              "risk_physical": "Risk: physical risk of failure", "risk_maintenance": "Risk: maintenance requirement",
                              "risk_incidents": "Risk: incident history", "risk_reviewed_on": "Risk reviewed",
                              "oem_schedule_required": "Manufacturer's schedule required (CMS)"},
    "workorders.workorder": {"asset": "Device", "requester": "Requested by", "callback": "Callback", "vendor_name": "Vendor", "opened_on": "Opened",
                             "due_on": "Due", "started_on": "Started", "completed_on": "Completed", "estimated_hours": "Estimated time",
                             "alert": "Recall notice", "pm_result": "PM result", "checklist_results": "Checklist", "follow_up_of": "Follow-up of",
                             "inspection_result": "Inspection result"},
    "workorders.laborline": {"worked_on": "Date", "hours": "Time"},
    "workorders.partline": {"description": "Part", "part_number": "Part number", "unit_cost": "Unit cost", "po_number": "PO number"},
    "contracts.contract": {"start_on": "Starts", "end_on": "Ends"},
    "pm.pmprocedure": {"source_kind": "Source", "source_url": "Source link", "estimated_hours": "Estimated time"},
    "pm.aemdecision": {"interval_months": "Interval", "oem_interval_months": "OEM interval", "evidence": "Failure history",
                       "proposed_on": "Proposed", "decided_on": "Committee date", "decision_note": "Committee note", "ended_on": "Ended",
                       "end_reason": "Why it ended"},
    "credentials.credential": {"scope": "Applies to", "value": "Matches", "issued_on": "Issued", "expires_on": "Expires"},
    "accounts.role": {"is_system": "System role", "scope": "What its users see"},
    "facility.facilitysettings": {"portal_require_callback": "Portal: callback number required", "portal_hotline": "Portal: hotline",
                                  "portal_confirmation": "Portal: confirmation", "portal_email_domains": "Portal: email domains",
                                  "target_pm_pct": "Target: PM completion", "target_uptime_pct": "Target: uptime",
                                  "target_mttr_days": "Target: mean time to repair", "repair_budget_monthly": "Monthly repair budget",
                                  "labor_rate": "In-house labor rate", "vendor_labor_rate": "Vendor labor rate",
                                  "pm_window_high": "PM on time, life support and high risk",
                                  "pm_window_high_days": "PM on time, life support and high risk: days after the due date",
                                  "pm_window_other": "PM on time, medium and low risk",
                                  "pm_window_other_days": "PM on time, medium and low risk: days after the due date"},
    "reports.customreport": {"group_by": "Grouped by", "sort": "Sorted by"},
    "tenants.tenant": {"slug": "Portal link name", "timezone": "Time zone", "is_active": "Can sign in"},
    "incidents.incident": {"asset": "Device", "work_order": "Investigation work order", "opened_work_order": "Work order opened by the incident",
                           "occurred_on": "Occurred", "aware_on": "Clinical staff first knew", "event_reference": "Event report number",
                           "report_due_on": "Report due", "decided_on": "Decided on", "decided_by": "Decided by",
                           "recorded_by": "Decision recorded by", "fda_reported_on": "Reported to the FDA",
                           "manufacturer_reported_on": "Reported to the manufacturer", "report_number": "Report number",
                           "accessories": "Accessories and disposables", "event_log": "Event log", "closed_on": "Closed",
                           "closed_by": "Closed by"},
    "incidents.incidenthold": {"held_on": "Held", "status_before": "Status before the hold", "sent_on": "Sent to the manufacturer",
                               "back_on": "Back from the manufacturer", "released_on": "Released", "released_by": "Released by"},
}

# How a value reads, per model and field (see _kind_text); the rest read by their field's type (_plain_text).
KINDS = {
    "equipment.asset": {"acquisition_cost": "money", "condition": "of_5", "status": "asset_status"},
    "equipment.devicemodel": {"oem_pm_interval_months": "months", "aem_interval_months": "months", "expected_life_years": "years",
                              "list_cost": "money", "risk_function": "of_10", "risk_physical": "of_5", "risk_maintenance": "of_5",
                              "risk_incidents": "of_2"},
    "workorders.workorder": {"estimated_hours": "hours", "checklist_results": "results"},
    "workorders.laborline": {"hours": "hours", "rate": "rate"},
    "workorders.partline": {"quantity": "number", "unit_cost": "money"},
    "contracts.contract": {"annual_cost": "money"},
    "pm.pmprocedure": {"estimated_hours": "hours", "checklist": "steps"},
    "pm.aemdecision": {"interval_months": "months", "oem_interval_months": "months", "evidence": "evidence"},
    "accounts.role": {"scope": "role_scope"},
    "facility.facilitysettings": {"target_pm_pct": "percent", "target_uptime_pct": "percent", "target_mttr_days": "days",
                                  "repair_budget_monthly": "money", "labor_rate": "rate", "vendor_labor_rate": "rate",
                                  "pm_window_high_days": "days", "pm_window_other_days": "days"},
    "reports.customreport": {"columns": "report_columns", "filters": "report_filters", "group_by": "report_group", "sort": "report_sort"},
    "tenants.tenant": {"timezone": "zone"},
}

_CAPITALS = {"pm": "PM", "oem": "OEM", "aem": "AEM", "po": "PO", "cms": "CMS", "mttr": "MTTR"}


def _model_key(model) -> str:
    return model._meta.label_lower


def _policy_labels() -> dict:
    from apps.facility.models import POLICY

    return {f: f"Policy: {label}" for f, label, _default in POLICY}


def _label(f, model=None) -> str:
    key = _model_key(model or f.model)
    explicit = LABELS.get(key, {}).get(f.name)
    if explicit:
        return explicit
    if key == "facility.facilitysettings" and f.name.startswith("policy_"):
        return _policy_labels().get(f.name, f.name)
    words = [_CAPITALS.get(w.lower(), w) for w in str(f.verbose_name).split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def _shown_fields(model) -> list:
    hidden = HIDDEN | HIDE.get(_model_key(model), set())
    return [f for f in model._meta.concrete_fields if f.name not in hidden]


OUTSIDE = "Someone outside this facility"


def _person(user) -> tuple[str, int | None]:
    """Who made a change, as this facility may read it: one of its people (or a platform superuser, who belongs to none) by name;
    anyone else (another facility's user who sent this facility's public request form while signed in there) unnamed."""
    if user is None:
        return SYSTEM, None
    tenant = get_current_tenant()
    if user.tenant_id is not None and (tenant is None or user.tenant_id != tenant.id):
        return OUTSIDE, None
    return str(user), user.pk


def _who(rec) -> tuple[str, int | None]:
    return _person(getattr(rec, "history_user", None))


def who(rec) -> str:
    """Who made the change a historical row records, as this facility may read it (the survey binder, slice 25): a name, "Cadence"
    for a job, or "Someone outside this facility"."""
    return _who(rec)[0]


# How a related row is named, live or as its last historical record (whose __str__ is simple_history's, not the model's).
_NAMERS = {
    "equipment.asset": lambda o: o.tag,
    "equipment.devicemodel": lambda o: f"{o.manufacturer} {o.model}",
    "equipment.department": lambda o: o.name,
    "workorders.workorder": lambda o: o.number,
    "contracts.contract": lambda o: f"{o.reference} · {o.vendor}",
    "pm.pmprocedure": lambda o: o.code,
    "credentials.technician": lambda o: o.name,
    "accounts.user": str,
    "accounts.role": lambda o: o.name,
    "recalls.alert": lambda o: f"{o.get_source_display()} {o.external_id}",
    "incidents.incident": lambda o: o.number,
}


def _name_of(model, obj) -> str:
    namer = _NAMERS.get(_model_key(model))
    return namer(obj) if namer else str(obj)


class _Names:
    """Foreign keys read as the records they name, looked up once per model for a whole page of entries. A row gone since reads as
    its last name on record (its own history, in this tenant) with "(deleted)", or "(deleted)" alone."""

    def __init__(self):
        self.wanted: dict = {}
        self.found: dict = {}

    def want(self, f, value):
        if value not in (None, ""):
            self.wanted.setdefault(f.related_model, set()).add(value)

    def load(self):
        for model, pks in self.wanted.items():
            manager = model._default_manager  # the tenant-scoped manager for business rows; users and notices are not tenant-scoped
            self.found.update({(model, obj.pk): _name_of(model, obj) for obj in manager.filter(pk__in=pks)})
            missing = [pk for pk in pks if (model, pk) not in self.found]
            history = getattr(model, "history", None)
            if missing and history is not None and hasattr(history, "model"):
                for rec in _rows(history.model).filter(id__in=missing).order_by("id", "-history_date", "-history_id"):
                    key = (model, rec.id)
                    if key not in self.found:
                        try:
                            self.found[key] = f"{_name_of(model, rec)} {DELETED}"
                        except Exception:
                            pass

    def name(self, f, value) -> str:
        return self.found.get((f.related_model, value), DELETED)


def _cut(text: str) -> str:
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


def _number(value) -> str:
    """A decimal without its trailing zeros: 1.50 -> "1.5", 2.00 -> "2"."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = format(d.normalize(), "f")
    return text


def _money(value) -> str:
    d = Decimal(str(value))
    return f"${d:,.0f}" if d == d.to_integral_value() else f"${d:,.2f}"


def _plural(value, one: str, many: str) -> str:
    n = _number(value)
    return f"{n} {one if n == '1' else many}"


def _day(iso) -> str:
    d = date.fromisoformat(iso) if isinstance(iso, str) else iso
    return f"{d:%b} {d.day}, {d.year}"


def _results_text(steps) -> str:
    """A PM's recorded checklist (WorkOrder.checklist_results) as its results: "12 steps: 11 pass, 1 N/A"."""
    if not isinstance(steps, list):
        raise ValueError
    counts = {"pass": 0, "fail": 0, "na": 0}
    for s in steps:
        result = s.get("result") if isinstance(s, dict) else None
        if result in counts:
            counts[result] += 1
    parts = [f"{n} {word}" for n, word in ((counts["pass"], "pass"), (counts["fail"], "fail"), (counts["na"], "N/A")) if n]
    head = _plural(len(steps), "step", "steps")
    return f"{head}: {', '.join(parts)}" if parts else head


def _evidence_text(ev) -> str:
    """An AEM case's evidence (apps.pm.aem.evidence) in a sentence: the window, the devices, the repairs, the PMs on time."""
    if not isinstance(ev, dict):
        raise ValueError
    devices = ev.get("devices_active", 0) + ev.get("devices_retired", 0)
    parts = [_plural(devices, "device", "devices"), f"{_number(ev.get('device_years', 0))} device-years"]
    repairs = _plural(ev.get("repairs", 0), "repair", "repairs")
    if ev.get("repairs_per_device_year") is not None:
        repairs += f" ({_number(ev['repairs_per_device_year'])} per device-year)"
    parts.append(repairs)
    if ev.get("pm_on_time_pct") is not None:
        window = ev.get("pm_window")  # slice 27: the PM window "on time" was counted by (evidence from before it has none)
        parts.append(f"{ev['pm_on_time_pct']}% of PMs on time" + (f" ({window[:1].lower()}{window[1:]})" if window else ""))
    if ev.get("open_recalls"):
        parts.append(_plural(ev["open_recalls"], "open recall", "open recalls"))
    return f"{_day(ev['since'])} to {_day(ev['as_of'])}: " + ", ".join(parts)


def _report_spec(rec):
    from apps.reports import custom

    spec = custom.spec_of(getattr(rec, "source", None))
    if spec is None:
        raise ValueError
    return custom, spec


def _report_text(kind: str, value, rec) -> str:
    """A custom report's columns, filters, grouping, or sort as the builder words them (apps.reports.custom)."""
    custom, spec = _report_spec(rec)
    if kind == "report_columns":
        return ", ".join(spec.column(k).label if spec.column(k) else str(k) for k in value)
    if kind == "report_group":
        return custom.group_label(spec, value) or str(value)
    if kind == "report_filters":
        words = custom.describe(spec, {"filters": value, "group_by": "", "sort": ""})
        return words.split(" · ", 1)[1] if " · " in words else "None"
    words = custom.describe(spec, {"filters": {}, "group_by": getattr(rec, "group_by", "") or "", "sort": value})
    return words.split(" · ")[-1].removeprefix("Sorted by ")


def _kind_text(kind: str, value, rec) -> str:
    if kind == "money":
        return _money(value)
    if kind == "rate":
        return f"{_money(value)}/h"
    if kind == "hours":
        return _plural(value, "hour", "hours")
    if kind == "months":
        return _plural(value, "month", "months")
    if kind == "years":
        return _plural(value, "year", "years")
    if kind == "days":
        return _plural(value, "day", "days")
    if kind == "percent":
        return f"{_number(value)}%"
    if kind == "number":
        return _number(value)
    if kind.startswith("of_"):
        return f"{value} of {kind[3:]}"
    if kind == "steps":
        return _plural(len(value), "step", "steps")
    if kind == "results":
        return _results_text(value)
    if kind == "evidence":
        return _evidence_text(value)
    if kind.startswith("report_"):
        return _report_text(kind, value, rec)
    if kind == "zone":
        from apps.facility.services import zone_label

        return zone_label(value)
    if kind == "asset_status":
        return _asset_status_text(value, rec)
    raise ValueError(kind)


def _asset_status_text(value, rec):
    """A device's status as it reads in its history: a rental, vendor loaner, or demo unit gone back (retired, not ours on the same
    historical row) reads "Returned to owner", never "Retired" (slice 29 review fix); None leaves every other status to the plain text."""
    from apps.equipment.models import AssetStatus, Ownership
    from apps.equipment.services import RETURNED_LABEL

    if value == AssetStatus.RETIRED and rec is not None and getattr(rec, "ownership", Ownership.OWNED) != Ownership.OWNED:
        return RETURNED_LABEL
    return None


def _plain_text(f, value, names: _Names) -> str:
    if f.is_relation:
        return names.name(f, value)
    if f.choices:
        return str(dict(f.flatchoices).get(value, value))
    if isinstance(f, models.BooleanField):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        return f"{value:%b} {value.day}, {value.year} {value:%H:%M}"
    if isinstance(value, date):
        return f"{value:%b} {value.day}, {value.year}"
    if isinstance(value, Decimal):
        return _number(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# What an empty value means where "—" would say too little.
EMPTY_WORDS = {
    "accounts.role": {"scope": "The role's default"},
    "tenants.tenant": {"timezone": "The server's"},  # slice 21: a facility that has chosen none works in the server's zone
    "reports.customreport": {"filters": "None", "group_by": "Not grouped", "sort": "The source's order"},
}


def _text(f, value, names: _Names, rec=None) -> str:
    """`value` of field `f` in words. `rec` is the historical record holding it (a custom report's columns read by its source)."""
    if value is None or value == "" or value == [] or value == {}:
        return EMPTY_WORDS.get(_model_key(f.model), {}).get(f.name, EMPTY)
    kind = KINDS.get(_model_key(f.model), {}).get(f.name)
    text = None
    if kind:
        try:
            text = _kind_text(kind, value, rec)
        except Exception:  # a value from before its format settled (or not what the kind expects) reads as stored
            text = None
    if text is None:
        text = _plain_text(f, value, names)
    return _cut(text)


_NOISE_REASONS = frozenset({"added", "edited", "changed", "removed", "deleted", "created"})


def _reason(rec, changes: list[Change]) -> str:
    """The reason a service gave, unless it only repeats what the entry already says: the action ("Added", "Edited"), the fields
    it changed ("Edited: columns, sort"), or one change's new value ("Status: In service")."""
    reason = (rec.history_change_reason or "").strip()
    if reason.lower() in _NOISE_REASONS or reason.lower().startswith("edited: "):
        return ""
    if any(reason == f"{c.field}: {c.after}" for c in changes):
        return ""
    return reason


def _empty(value) -> bool:
    return value is None or value is False or value in ("", [], {})


# --- entries -----------------------------------------------------------------------------------------------------------------------

def _pairs(area: Area, recs: list, prevs: dict) -> list[tuple]:
    """(record, previous record or None) for each historical record, `prevs` mapping a record's history_id to the one before it."""
    return [(rec, prevs.get(rec.history_id)) for rec in recs]


def _build(area: Area, pairs: list[tuple]) -> list[Entry | None]:
    """One Entry per (record, previous record) pair, in order; None where a change touched none of the shown fields. The foreign
    keys of the whole batch are named in one query per related model."""
    model = area.model
    fields = _shown_fields(model)
    names = _Names()
    raw = []
    for rec, prev in pairs:
        if rec.history_type == "~" and prev is not None:
            diffs = [(f, getattr(prev, f.attname), getattr(rec, f.attname)) for f in fields if getattr(prev, f.attname) != getattr(rec, f.attname)]
            if not diffs:
                raw.append(None)
                continue
        else:  # added, deleted (the values it had), or a change with no earlier record on file (history began after the row)
            diffs = [(f, None, getattr(rec, f.attname)) for f in fields if not _empty(getattr(rec, f.attname))]
        for f, before, after in diffs:
            if f.is_relation:
                names.want(f, before)
                names.want(f, after)
        raw.append((rec, prev, diffs))
    names.load()
    out: list[Entry | None] = []
    for item in raw:
        if item is None:
            out.append(None)
            continue
        rec, prev, diffs = item
        who, who_id = _who(rec)
        action = {"+": "added", "~": "changed", "-": "deleted"}[rec.history_type]
        changes = []
        for f, before, after in diffs:
            after_text = _text(f, after, names, rec)
            before_text = ""
            if action == "changed":
                before_text = _text(f, before, names, prev)
                if before_text == after_text:
                    after_text += EDITED  # a checklist reworded, long text changed past the cut: the same words, a different value
            changes.append(Change(_label(f, model), before_text, after_text))
        out.append(Entry(at=rec.history_date, area=area.key, area_label=area.label, record=area.name(rec), url=area.url(rec) if area.url else None,
                         who=who, who_id=who_id, action=action, reason=_reason(rec, changes), changes=changes))
    return out


def _entries(area: Area, pairs: list[tuple]) -> list[Entry]:
    return [e for e in _build(area, pairs) if e is not None]


def _previous(history_model, recs: list) -> dict:
    """{history_id: the record before it, or None} for each of `recs`: the latest earlier record of the same row, in the current
    tenant. Two queries however long the rows' histories are (a subquery finds each one's predecessor)."""
    if not recs:
        return {}
    base = _rows(history_model)
    earlier = (base.filter(id=OuterRef("id"))
               .filter(Q(history_date__lt=OuterRef("history_date")) | Q(history_date=OuterRef("history_date"), history_id__lt=OuterRef("history_id")))
               .order_by("-history_date", "-history_id").values("history_id")[:1])
    prev_ids = dict(base.filter(history_id__in=[rec.history_id for rec in recs]).annotate(prev=Subquery(earlier)).values_list("history_id", "prev"))
    wanted = {pid for pid in prev_ids.values() if pid is not None}
    prevs = {p.history_id: p for p in base.filter(history_id__in=wanted)} if wanted else {}
    return {rec.history_id: prevs.get(prev_ids.get(rec.history_id)) for rec in recs}


def _history_rows(area: Area):
    return _rows(area.model.history.model).select_related("history_user", *area.select)


def entries_for(obj, limit: int = 200) -> list[Entry]:
    """Every change to `obj`, newest first (at most `limit` saves)."""
    area = area_of(type(obj))
    if area is None:
        return []
    recs = list(_history_rows(area).filter(id=obj.pk).order_by("-history_date", "-history_id")[: limit + 1])
    prevs = {rec.history_id: recs[i + 1] if i + 1 < len(recs) else None for i, rec in enumerate(recs)}
    return _entries(area, _pairs(area, recs[:limit], prevs))


def _related_rows(area: Area, rows):
    """The historical rows of `area` that `rows` names: a queryset of the area's model (its live rows), or field lookups on the
    historical rows themselves ({"work_order_id": wo.pk}), which also finds rows deleted since."""
    qs = _history_rows(area)
    if isinstance(rows, dict):
        allowed = {f.name for f in area.model._meta.concrete_fields} | {f.attname for f in area.model._meta.concrete_fields}
        unknown = {k.split("__")[0] for k in rows} - allowed
        if unknown:
            raise ValueError(f"Not a field of {area.model_path}: {', '.join(sorted(unknown))}")
        return qs.filter(**rows)
    return qs.filter(id__in=rows.values("pk"))


def entries_for_rows(area_key: str, rows, limit: int = 200) -> list[Entry]:
    """Every change to the records in `rows`, newest first (at most `limit` saves): `rows` is a queryset of one area's model (e.g.
    a work order's labor lines; rows deleted since are not in it, nor their history), or lookups on that area's historical rows
    ({"work_order_id": wo.pk}: deleted rows too)."""
    area = AREAS[area_key]
    history_model = area.model.history.model
    recs = list(_related_rows(area, rows).order_by("-history_date", "-history_id")[:limit])
    return _entries(area, _pairs(area, recs, _previous(history_model, recs)))


# --- pages: newest first, continued after the last entry shown (a cursor, not a count, so saves made between two pages never make
# the next one repeat or skip entries) ----------------------------------------------------------------------------------------------

RANKS = {key: i for i, key in enumerate(ALL_AREAS)}  # ties in time read in this order, so every save has one place in the list


def _source(area: Area, qs) -> tuple:
    """(area, rows, the rows' time field, their id field) for _read."""
    if area is ACCESS:
        return area, qs, "at", "pk"
    return area, qs, "history_date", "history_id"


def cursor_text(key: tuple) -> str:
    """A page's continuation as text for a URL: the (time, area rank, id) of the last save it read."""
    at, rank, pk = key
    return f"{at.isoformat()}~{rank}~{pk}"


def parse_cursor(text) -> tuple | None:
    """cursor_text's key back, or None for anything that is not one (a hand-edited URL starts from the newest)."""
    import uuid

    try:
        at_text, rank_text, pk_text = str(text).split("~")
        at = datetime.fromisoformat(at_text)
        rank = int(rank_text)
        if timezone.is_naive(at) or not 0 <= rank < len(RANKS):
            return None
        key = list(RANKS)[rank]
        pk = uuid.UUID(pk_text) if key == ACCESS.key else int(pk_text)
    except (TypeError, ValueError):
        return None
    return at, rank, pk


def _older(qs, rank: int, date_field: str, pk_field: str, cursor: tuple | None):
    """`qs`'s rows that come after `cursor` in the list's order: newest first, ties by area rank (highest first), then by id."""
    if cursor is None:
        return qs
    at, c_rank, c_pk = cursor
    if rank > c_rank:
        return qs.filter(**{f"{date_field}__lt": at})
    if rank < c_rank:
        return qs.filter(**{f"{date_field}__lte": at})
    return qs.filter(Q(**{f"{date_field}__lt": at}) | Q(**{date_field: at, f"{pk_field}__lt": c_pk}))


def _built(window: list) -> dict:
    """{(area key, row id): its entry, or None for a save that changed none of the shown fields} for a window of (key, area, row)."""
    by_area: dict = {}
    for _key, area, row in window:
        by_area.setdefault(area.key, []).append(row)
    out: dict = {}
    for key, rows in by_area.items():
        area = ALL_AREAS[key]
        if area is ACCESS:
            out.update({(key, r.pk): e for r, e in zip(rows, _access_entries(rows))})
            continue
        prevs = _previous(area.model.history.model, rows)
        for rec, entry in zip(rows, _build(area, _pairs(area, rows, prevs))):  # one batch per area: its names in one query each
            out[(key, rec.history_id)] = entry
    return out


def _read(sources: list[tuple], limit: int, after: tuple | None) -> tuple[list[Entry], tuple | None]:
    """Up to `limit` entries of `sources` (from _source) after `after`, newest first, and the key to continue from (None when
    nothing older is left). Saves that show nothing are read past, up to MAX_ROUNDS windows of `limit` saves, so a page is never
    empty while older entries exist within reach; each window costs one query per source and the batch builds."""
    limit = max(int(limit), 1)
    entries: list[Entry] = []
    cursor = after
    for _round in range(MAX_ROUNDS):
        window = []
        for area, qs, date_field, pk_field in sources:
            rank = RANKS[area.key]
            for row in _older(qs, rank, date_field, pk_field, cursor).order_by(f"-{date_field}", f"-{pk_field}")[: limit + 1]:
                window.append(((getattr(row, date_field), rank, getattr(row, pk_field)), area, row))
        window.sort(key=lambda t: t[0], reverse=True)
        more = len(window) > limit
        window = window[:limit]
        built = _built(window)
        for i, (key, area, row) in enumerate(window):
            cursor = key
            entry = built.get((area.key, row.pk if area is ACCESS else row.history_id))
            if entry is not None:
                entries.append(entry)
            if len(entries) >= limit:
                return entries, (key if more or i + 1 < len(window) else None)
        if not more:
            return entries, None
    return entries, cursor


def record_history(obj, related: dict | None = None, *, limit: int = 50, after: str | None = None) -> tuple[list[Entry], str | None]:
    """A page of `obj`'s history, newest first, with the changes of the rows that belong to it interleaved by time: `related` maps
    an area key to lookups on that area's historical rows ({"labor": {"work_order_id": wo.pk}}), so a row deleted since keeps its
    history here. A page holds up to `limit` entries after the cursor `after` (cursor_text of the last one shown; None for the
    newest). Returns (entries, the next page's cursor, or None when nothing is older)."""
    area = area_of(type(obj))
    if area is None:
        return [], None
    sources = [_source(area, _history_rows(area).filter(id=obj.pk))]
    sources += [_source(AREAS[key], _related_rows(AREAS[key], lookups)) for key, lookups in (related or {}).items()]
    entries, following = _read(sources, limit, parse_cursor(after) if after else None)
    return entries, cursor_text(following) if following else None


def _access_entries(events: list) -> list[Entry]:
    out = []
    for e in events:
        who, who_id = _person(e.by if e.by_id else None)
        out.append(Entry(at=e.at, area=ACCESS.key, area_label=ACCESS.label, record=ACCESS.name(e) or (e.role.name if e.role_id else ""),
                         url=None, who=who, who_id=who_id, action=e.get_action_display(), reason="",
                         changes=[Change("", "", e.detail)] if e.detail else []))
    return out


def change_log(user, *, areas: list[str] | None = None, since: date | None = None, until: date | None = None, who: int | None = None,
               limit: int = 50, after: str | None = None) -> tuple[list[Entry], str | None]:
    """The facility's changes, newest first, across the areas `user` can view (or those of `areas` among them), between `since`
    and `until` (local days, inclusive), made by the user with id `who`: a page of up to `limit` entries after the cursor `after`
    (None for the newest). Returns (entries, the next page's cursor or None when nothing is older). A link that needs more than the
    area's View (a device model opens in the PM program's drawer) is left off for a reader without it."""
    from apps.accounts.models import AccessEvent

    allowed = [a for a in readable_areas(user) if areas is None or a.key in areas]
    sources = []
    for area in allowed:
        if area is ACCESS:
            qs, date_field, who_field = AccessEvent.objects.select_related("by", "user", "role"), "at", "by_id"
        else:
            qs, date_field, who_field = _history_rows(area), "history_date", "history_user_id"
        if since:
            qs = qs.filter(**{f"{date_field}__date__gte": since})
        if until:
            qs = qs.filter(**{f"{date_field}__date__lte": until})
        if who is not None:
            qs = qs.filter(**{who_field: who})
        sources.append(_source(area, qs))
    entries, following = _read(sources, limit, parse_cursor(after) if after else None)
    for entry in entries:
        follow = ALL_AREAS[entry.area].url_module
        if entry.url and follow and not user.has_level(follow, 1):
            entry.url = None
    return entries, cursor_text(following) if following else None
