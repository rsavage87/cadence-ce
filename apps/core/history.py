"""
Change history (slice 20): what changed on a record, when, who changed it, and why, read from django-simple-history's tables and
put in plain words for the drawers' History tabs, the Users and access tab's change log, and the API's change log.

AREAS lists every audited kind of record: its label, the module whose View it needs (a reader sees only the areas their role can
view), how to name one record, and where it opens. Fields that tell a reader nothing (ids, the tenant, the row's own timestamps) are
left out; a foreign key reads as the record it names, a choice as its label, a yes/no as Yes or No, a date as "Oct 5, 2026", an
empty value as "—", and long text is cut at TEXT_LIMIT characters.

Each entry is one save: "added" (with the values it started with), "changed" (each changed field's before and after), or "deleted",
with the user simple_history recorded (the request's user on the web and the API, or the `by` a service set as `_history_user`;
nobody for a job or an import: "Cadence") and the reason a service gave (history_change_reason). A change that touched none of the
shown fields is left out. Access changes (who may do what: apps.accounts.models.AccessEvent) are not simple_history rows; the change
log reads them as the "access" area.

Everything here reads inside the request's tenant. The historical tables carry tenant_id, so row-level security applies to them on
PostgreSQL, but their managers are simple_history's, not the tenant-scoped one: every query here filters on the current tenant itself
(_rows), and reads nothing when there is none. Callers check who may read what: scoped users (apps.workorders.scoping) see no
history.
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime

from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.tenants.context import get_current_tenant

HIDDEN = frozenset({"id", "tenant", "created_at", "updated_at"})
TEXT_LIMIT = 200
EMPTY = "—"
SYSTEM = "Cadence"  # who changed it when nobody did: the nightly jobs, the importer, the seed


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


def _wo_number(rec) -> str:
    return rec.work_order.number


AREAS = {a.key: a for a in (
    Area("devices", "Devices", "equipment.Asset", "equipment", _safe(lambda r: r.tag), _safe(lambda r: reverse("web:asset", args=[r.tag]))),
    Area("device_models", "Device models", "equipment.DeviceModel", "equipment", _safe(lambda r: f"{r.manufacturer} {r.model}"),
         _safe(lambda r: reverse("web:pm_model", args=[r.id]))),
    Area("work_orders", "Work orders", "workorders.WorkOrder", "workorders", _safe(lambda r: r.number), _safe(lambda r: reverse("web:wo", args=[r.number]))),
    Area("labor", "Labor", "workorders.LaborLine", "workorders", _safe(lambda r: f"{_wo_number(r)} labor"),
         _safe(lambda r: reverse("web:wo", args=[_wo_number(r)]))),
    Area("parts", "Parts", "workorders.PartLine", "workorders", _safe(lambda r: f"{_wo_number(r)} part"),
         _safe(lambda r: reverse("web:wo", args=[_wo_number(r)]))),
    Area("contracts", "Contracts", "contracts.Contract", "contracts", _safe(lambda r: f"{r.reference} · {r.vendor}"),
         _safe(lambda r: reverse("web:contract", args=[r.id]))),
    Area("procedures", "PM procedures", "pm.PmProcedure", "pm", _safe(lambda r: r.code)),
    Area("aem", "AEM decisions", "pm.AemDecision", "pm", _safe(lambda r: f"AEM · {r.device_model}"),
         _safe(lambda r: reverse("web:pm_model", args=[r.device_model_id]) + "?tab=aem")),
    Area("credentials", "Credentials", "credentials.Credential", "users", _safe(lambda r: f"{r.technician} · credential")),
    Area("roles", "Roles", "accounts.Role", "users", _safe(lambda r: r.name), _safe(lambda r: reverse("web:roles"))),
    Area("settings", "Settings", "facility.FacilitySettings", "settings", _safe(lambda r: "Settings"), _safe(lambda r: reverse("web:settings"))),
    Area("custom_reports", "Custom reports", "reports.CustomReport", "reports", _safe(lambda r: r.name),
         _safe(lambda r: reverse("web:report", args=[f"custom-{r.id}"]))),
)}
ACCESS = Area("access", "Access", "accounts.AccessEvent", "users", _safe(lambda e: e.user.get_full_name() or e.user.username if e.user_id else ""))
ALL_AREAS = {**AREAS, ACCESS.key: ACCESS}


def _rows(history_model):
    """`history_model`'s rows in the current tenant only (none without one): its manager is not the tenant-scoped one."""
    tenant = get_current_tenant()
    qs = history_model.objects.all()
    return qs.filter(tenant_id=tenant.id) if tenant is not None else qs.none()


def area_of(model) -> Area | None:
    label = f"{model._meta.app_label}.{model.__name__}"
    return next((a for a in AREAS.values() if a.model_path.lower() == label.lower()), None)


def readable_areas(user) -> list[Area]:
    """The areas `user`'s role can view, in AREAS' order (access last)."""
    return [a for a in ALL_AREAS.values() if user.has_level(a.module, 1)]


# --- values in words -------------------------------------------------------------------------------------------------------------

def _shown_fields(model) -> list:
    return [f for f in model._meta.concrete_fields if f.name not in HIDDEN]


def _who(rec) -> tuple[str, int | None]:
    user = getattr(rec, "history_user", None)
    if user is None:
        return SYSTEM, None
    return (user.get_full_name() or user.username), user.pk


class _Names:
    """Foreign keys read as the records they name, looked up once per model for a whole page of entries."""

    def __init__(self):
        self.wanted: dict = {}
        self.found: dict = {}

    def want(self, f, value):
        if value not in (None, ""):
            self.wanted.setdefault(f.related_model, set()).add(value)

    def load(self):
        for model, pks in self.wanted.items():
            manager = model._default_manager  # the tenant-scoped manager for business rows; users are not tenant-scoped
            self.found.update({(model, obj.pk): str(obj) for obj in manager.filter(pk__in=pks)})

    def name(self, f, value) -> str:
        return self.found.get((f.related_model, value), "(removed)")


def _text(f, value, names: _Names) -> str:
    if value is None or value == "":
        return EMPTY
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
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


def _label(f) -> str:
    return str(f.verbose_name)[:1].upper() + str(f.verbose_name)[1:]


def _attname(f) -> str:
    return f.attname  # the historical row stores a foreign key's id under the same attname


# --- entries -----------------------------------------------------------------------------------------------------------------------

def _pairs(area: Area, recs: list, prevs: dict) -> list[tuple]:
    """(record, previous record or None) for each historical record, `prevs` mapping a record's history_id to the one before it."""
    return [(rec, prevs.get(rec.history_id)) for rec in recs]


def _entries(area: Area, pairs: list[tuple]) -> list[Entry]:
    fields = _shown_fields(area.model)
    names = _Names()
    raw = []
    for rec, prev in pairs:
        if rec.history_type == "~" and prev is not None:
            diffs = [(f, getattr(prev, _attname(f)), getattr(rec, _attname(f))) for f in fields if getattr(prev, _attname(f)) != getattr(rec, _attname(f))]
            if not diffs:
                continue
        elif rec.history_type == "+" or prev is None and rec.history_type != "-":
            diffs = [(f, None, getattr(rec, _attname(f))) for f in fields if getattr(rec, _attname(f)) not in (None, "", [], {})]
        else:
            diffs = []
        for f, before, after in diffs:
            if f.is_relation:
                names.want(f, before)
                names.want(f, after)
        raw.append((rec, diffs))
    names.load()
    out = []
    for rec, diffs in raw:
        who, who_id = _who(rec)
        action = {"+": "added", "~": "changed", "-": "deleted"}[rec.history_type]
        changes = [Change(_label(f), _text(f, before, names) if action == "changed" else "", _text(f, after, names)) for f, before, after in diffs]
        out.append(Entry(at=rec.history_date, area=area.key, area_label=area.label, record=area.name(rec), url=area.url(rec) if area.url else None,
                         who=who, who_id=who_id, action=action, reason=rec.history_change_reason or "", changes=changes))
    return out


def _previous(history_model, recs: list) -> dict:
    """{history_id: the record before it} for each of `recs`, in one query per page: the latest earlier record of the same row."""
    if not recs:
        return {}
    ids = {rec.id for rec in recs}
    candidates = list(_rows(history_model).filter(id__in=ids).filter(history_date__lte=max(rec.history_date for rec in recs))
                      .order_by("id", "history_date", "history_id"))
    by_row: dict = {}
    for c in candidates:
        by_row.setdefault(c.id, []).append(c)
    out = {}
    for rec in recs:
        earlier = [c for c in by_row.get(rec.id, []) if (c.history_date, c.history_id) < (rec.history_date, rec.history_id)]
        out[rec.history_id] = earlier[-1] if earlier else None
    return out


def entries_for(obj, limit: int = 200) -> list[Entry]:
    """Every change to `obj`, newest first (at most `limit` saves)."""
    area = area_of(type(obj))
    if area is None:
        return []
    recs = list(_rows(type(obj).history.model).filter(id=obj.pk).select_related("history_user").order_by("-history_date", "-history_id")[: limit + 1])
    prevs = {rec.history_id: recs[i + 1] if i + 1 < len(recs) else None for i, rec in enumerate(recs)}
    return _entries(area, _pairs(area, recs[:limit], prevs))


def entries_for_rows(area_key: str, rows, limit: int = 200) -> list[Entry]:
    """Every change to the records in `rows` (a queryset of one area's model, e.g. a work order's labor lines), newest first."""
    area = AREAS[area_key]
    history_model = area.model.history.model
    recs = list(_rows(history_model).filter(id__in=rows.values("pk")).select_related("history_user").order_by("-history_date", "-history_id")[:limit])
    return _entries(area, _pairs(area, recs, _previous(history_model, recs)))


def _access_entries(events: list) -> list[Entry]:
    out = []
    for e in events:
        who = (e.by.get_full_name() or e.by.username) if e.by_id else SYSTEM
        out.append(Entry(at=e.at, area=ACCESS.key, area_label=ACCESS.label, record=ACCESS.name(e) or (e.role.name if e.role_id else ""),
                         url=None, who=who, who_id=e.by_id, action=e.get_action_display(), reason="",
                         changes=[Change("", "", e.detail)] if e.detail else []))
    return out


def change_log(user, *, areas: list[str] | None = None, since: date | None = None, until: date | None = None, who: int | None = None,
               limit: int = 50, offset: int = 0) -> tuple[list[Entry], bool]:
    """The facility's changes, newest first, across the areas `user` can view (or those of `areas` among them), between `since`
    and `until` (local days, inclusive), made by the user with id `who`; one page of `limit` from `offset`. Returns (entries, more):
    `more` says a later page has entries. Each area reads at most offset + limit + 1 rows, so a page costs the same however long
    the history is."""
    from apps.accounts.models import AccessEvent

    allowed = [a for a in readable_areas(user) if areas is None or a.key in areas]
    window = offset + limit + 1
    picked = []
    for area in allowed:
        if area is ACCESS:
            qs = AccessEvent.objects.select_related("by", "user", "role")
            date_field, who_field = "at", "by_id"
        else:
            qs = _rows(area.model.history.model).select_related("history_user")
            date_field, who_field = "history_date", "history_user_id"
        if since:
            qs = qs.filter(**{f"{date_field}__date__gte": since})
        if until:
            qs = qs.filter(**{f"{date_field}__date__lte": until})
        if who is not None:
            qs = qs.filter(**{who_field: who})
        tie = "-pk" if area is ACCESS else "-history_id"
        for row in qs.order_by(f"-{date_field}", tie)[:window]:
            picked.append((getattr(row, date_field), area, row))
    picked.sort(key=lambda t: t[0], reverse=True)
    page = picked[offset: offset + limit]
    more = len(picked) > offset + limit
    entries = []
    by_area: dict = {}
    for at, area, row in page:
        by_area.setdefault(area.key, []).append(row)
    built: dict = {}
    for key, rows in by_area.items():
        area = ALL_AREAS[key]
        if area is ACCESS:
            built.update({("access", r.pk): e for r, e in zip(rows, _access_entries(rows))})
            continue
        history_model = area.model.history.model
        prevs = _previous(history_model, rows)
        for rec in rows:
            got = _entries(area, [(rec, prevs.get(rec.history_id))])
            built[(key, rec.history_id)] = got[0] if got else None
    for at, area, row in page:
        entry = built.get((area.key, row.pk if area is ACCESS else row.history_id))
        if entry is not None:
            entries.append(entry)
    return entries, more
