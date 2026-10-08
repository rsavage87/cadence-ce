"""
My work (slice 24): a technician's own work, for the phone-first page (apps/web/views_my_work.py), its nav badge, and the daily
digest's link. Every query here starts from scoping.work_orders, so nothing shows a user more than their share.

Whose work, scope first: a vendor technician (DataScope.COMPANY) has their company's open work orders, whatever technician profile
their account may also carry; a clinical requester (DataScope.DEPARTMENT) has no My work; anyone else (the whole facility) has the work
orders assigned to their own active technician profile (credentials.services.technician_of), or no My work without one.

The groups follow what a technician does next: repairs and requests (by priority, then due date; a recall alert's batch is one row),
today's PMs (due today or before, by location: department, room, tag), work waiting on parts, PMs coming up in the next SOON_DAYS, and
how many later. "Due" means one thing here, on the badge, and in the digest: open work (not waiting on parts) due today or before.

Slice 28: every work order the groups give (takeable included) carries `held` (services.with_held): True when its device is held as
evidence for an incident investigation and it is not that investigation, so nobody may start or complete it (the card offers no
Start, Resume, or Complete and shows the hold; services.change_status refuses them anyway). The investigation keeps its moves. Held
work stays in its group and in "due": it is still the technician's, waiting for the incident's release.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import F, OuterRef, Subquery, Sum
from django.urls import reverse

from apps.accounts.models import DataScope, Level, Module
from apps.credentials.services import technician_of

from . import scoping
from .models import LaborLine, WorkOrderStatusHistory, WoStatus, WoType
from .services import PRIORITY_RANK, open_work_orders, with_held

SOON_DAYS = 7
COMPANY, TECHNICIAN = "company", "technician"
RELATED = ("asset", "asset__device_model", "asset__device_model__pm_procedure", "asset__department", "assigned_to", "alert")


@dataclass
class Whose:
    """Whose work My work lists for a user: a vendor's company, a technician profile, or nobody's (kind None)."""

    kind: str | None
    technician: object = None


def whose(user) -> Whose:
    scope = scoping.scope_of(user)
    if scope == DataScope.COMPANY:
        return Whose(COMPANY)
    if scope == DataScope.DEPARTMENT:
        return Whose(None)
    technician = technician_of(user)
    return Whose(TECHNICIAN, technician) if technician is not None else Whose(None)


def has_my_work(user) -> bool:
    return whose(user).kind is not None


def mine(user, qs=None):
    """`user`'s work orders among `qs` (default: the open ones), narrowed to their share first."""
    qs = open_work_orders() if qs is None else qs
    w = whose(user)
    if w.kind == COMPANY:
        return scoping.work_orders(user, qs)
    if w.kind == TECHNICIAN:
        return scoping.work_orders(user, qs.filter(assigned_to=w.technician))
    return qs.none()


def due(user, today: date):
    """Open work of `user`'s due today or before, not waiting on parts: the badge's and the digest's "due"."""
    return mine(user).filter(status__in=(WoStatus.OPEN, WoStatus.IN_PROGRESS), due_on__lte=today)


def due_count(user, today: date) -> tuple[int, bool]:
    """(how many are due, whether any is late) for the nav badge."""
    qs = due(user, today)
    return qs.count(), qs.filter(due_on__lt=today).exists()


@dataclass
class Groups:
    whose: Whose
    repairs: list = field(default_factory=list)        # work orders (not PMs, not a recall batch), by priority then due date
    recalls: list = field(default_factory=list)        # [{"alert": Alert, "work_orders": [...]}], each batch by location
    pms_today: list = field(default_factory=list)      # PMs due today or before, by location
    waiting: list = field(default_factory=list)        # waiting on parts, longest first
    coming: list = field(default_factory=list)         # PMs due in the next SOON_DAYS, by date then location
    later: int = 0                                     # PMs due after that
    takeable: list = field(default_factory=list)       # unassigned work the technician may take (part C)


def _by_location(qs):
    return qs.order_by("asset__department__name", "asset__room", "asset__tag", "due_on")


def groups(user, today: date) -> Groups:
    w = whose(user)
    result = Groups(w)
    if w.kind is None:
        return result
    qs = with_held(mine(user)).select_related(*RELATED)  # slice 28: each work order's `held`
    active = qs.filter(status__in=(WoStatus.OPEN, WoStatus.IN_PROGRESS))
    other = active.exclude(type=WoType.PM)
    batches = {}
    for wo in _by_location(other.filter(alert__isnull=False)):
        batches.setdefault(wo.alert_id, {"alert": wo.alert, "work_orders": []})["work_orders"].append(wo)
    result.recalls = sorted(batches.values(), key=lambda b: (min(w.due_on for w in b["work_orders"]), str(b["alert"])))
    result.repairs = list(other.filter(alert__isnull=True).order_by(PRIORITY_RANK, "due_on", "number"))
    pms = active.filter(type=WoType.PM)
    result.pms_today = list(_by_location(pms.filter(due_on__lte=today)))
    result.coming = list(pms.filter(due_on__gt=today, due_on__lte=today + timedelta(days=SOON_DAYS))
                         .order_by("due_on", "asset__department__name", "asset__room", "asset__tag"))
    result.later = pms.filter(due_on__gt=today + timedelta(days=SOON_DAYS)).count()
    began = (WorkOrderStatusHistory.objects.filter(work_order=OuterRef("pk"), to_status=WoStatus.AWAITING_PARTS)
             .exclude(from_status=WoStatus.AWAITING_PARTS).order_by("-created_at").values("created_at")[:1])
    # longest first, by its last move into waiting (a reassignment touches updated_at, not how long it has waited)
    result.waiting = list(qs.filter(status=WoStatus.AWAITING_PARTS).annotate(began=Subquery(began)).order_by(F("began").asc(nulls_first=True), "number"))
    result.takeable = takeable(user, today)
    return result


TAKE_LIMIT = 10


def takeable(user, today: date) -> list:
    """Open work nobody has that `user` could take (services.take): for a technician whose facility lets technicians take work, up to
    TAKE_LIMIT open, unassigned, in-house work orders on devices their own profile is credentialed for today, most urgent first
    (priority, then due date). Empty for a vendor (the facility assigns their company's work), a requester, anyone without a
    technician profile or Work orders Edit, and while the setting is off (services.may_take_as says who may take any)."""
    from django.db.models import Q, prefetch_related_objects

    from apps.credentials.models import Credential, Scope
    from apps.credentials.services import qualification

    from .models import WorkOrder
    from .services import may_take_as

    technician = may_take_as(user)
    if technician is None:
        return []
    prefetch_related_objects([technician], "credentials")
    live = [c for c in technician.credentials.all() if c.status == Credential.Status.ACTIVE and (not c.expires_on or c.expires_on >= today)]
    # Narrows the query to devices a live credential names (what qualification() matches on); qualification() still decides each.
    named = Q(pk__in=[])
    for scope, column in ((Scope.MANUFACTURER, "manufacturer"), (Scope.MODEL, "model"), (Scope.CATEGORY, "category")):
        values = {c.value for c in live if c.scope == scope}
        if values:
            named |= Q(**{f"asset__device_model__{column}__in": values})
    nobodys = WorkOrder.objects.filter(status=WoStatus.OPEN, assigned_to__isnull=True, vendor_service=False)
    candidates = (with_held(scoping.work_orders(user, nobodys)).filter(named).select_related(*RELATED)  # taking held work is allowed
                  .order_by(PRIORITY_RANK, "due_on", "number"))
    return [wo for wo in candidates[:TAKE_LIMIT] if qualification(technician, wo.asset, today).ok]


def hours(user, today: date) -> dict | None:
    """Hours credited to the user's own technician profile today and this week (Monday on), or None for a vendor (their time is
    logged without a technician) or nobody's."""
    w = whose(user)
    if w.kind != TECHNICIAN:
        return None
    lines = LaborLine.objects.filter(technician=w.technician)
    monday = today - timedelta(days=today.weekday())
    total = lambda qs: qs.aggregate(h=Sum("hours"))["h"] or Decimal(0)  # noqa: E731
    return {"today": total(lines.filter(worked_on=today)), "week": total(lines.filter(worked_on__gte=monday, worked_on__lte=today))}


# --- what a scanned label opens (slice 24, part D) ----------------------------------------------------------------------------

WO_TAB = "wo"  # the device drawer's Work orders tab (apps.web.views.asset_drawer_context)


@dataclass(frozen=True)
class ScanTarget:
    """What scanning a device's label opens (scan_target): one work order, or the device's drawer at `tab` (blank: its first)."""

    asset: object
    work_order: object = None
    tab: str = ""

    @property
    def url(self) -> str:
        """Its page, opened directly: the work order's, or the device's at `tab`."""
        if self.work_order is not None:
            return reverse("web:wo", args=[self.work_order.number])
        url = reverse("web:asset", args=[self.asset.tag])
        return f"{url}?tab={self.tab}" if self.tab else url


def scan_target(user, asset) -> ScanTarget:
    """What scanning `asset`'s label opens for `user`: the one rule for Scan opened from My work (apps.web.views_scan) and the portal's
    "Open in Cadence" link for a signed-in member (apps.portal.views; an iPhone's Camera app opens a label's link there). For someone
    with My work and Work orders View, the one open work order of theirs on the device (mine(): their share first) when there is
    exactly one; otherwise (none of theirs, or several to choose from) the device at its Work orders tab. Anyone else gets the device,
    as before. The caller has checked that `user` may see the device (Equipment View, scoping.can_see_asset); call inside the
    facility's context (the role and the technician profile are its rows)."""
    if not user.has_level(Module.WORKORDERS, Level.VIEW) or not has_my_work(user):
        return ScanTarget(asset)
    theirs = list(mine(user).filter(asset=asset).order_by("number")[:2])
    if len(theirs) == 1:
        return ScanTarget(asset, work_order=theirs[0])
    return ScanTarget(asset, tab=WO_TAB)
