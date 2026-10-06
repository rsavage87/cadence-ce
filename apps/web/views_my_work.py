"""
My work (slice 24): the technician's own work, phone first (apps.workorders.my_work says whose and groups it). A vendor technician
sees their company's open work; a clinical requester, or someone in the facility with no technician profile, is told what the page
is for. Opening a card, logging time, and completing go through the work order's own endpoints (the drawer and its modals, which
check their own levels); the list re-fetches itself when they change something (the wo-changed event).

Part A, the page and its cards. Each group the service gives is a set of cards, in the order a technician works: repairs and
requests (a recall alert's batch is one row that opens to its devices), today's PMs, work waiting on parts, PMs coming up this week
and how many later, then work they could take (part C). A card says what is needed at the device: where it is (a portal request's
reported location, else the department and room), the problem's first line (masked as the drawer masks it: shown_text), how late,
and the chips; and offers the moves its status and the user's levels allow (apps.workorders.permissions, the drawer's rules; each
endpoint checks them again). Every move leaves the technician on the list, never behind the drawer: Start and Resume post to
wo_status and swap nothing; the modals a card opens (Log time, Add part, Waiting on parts, and part B's Complete) carry
from=my_work and answer with the toast, wo-changed, and modal-close only (saved_from_card). The list then re-fetches itself and
swaps the nav's My work badge out of band, and keeps open the recall batches that were open (their hidden `open` inputs).

Waiting on parts (wo_waiting) asks for a short note of what the work waits for (part, PO, expected date) and keeps it as the status
history's note; the card shows how long it has waited, that note, and its part lines' PO numbers.

Parts C (taking unassigned work) and D (Scan) provide web/_my_work_take.html and web/_my_work_scan.html; the page includes each only
when it exists (optional_template).
"""
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import prefetch_related_objects
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone
from django_htmx.http import reswap, trigger_client_event

from apps.accounts.models import Level, Module
from apps.credentials.models import Credential
from apps.credentials.services import qualification
from apps.workorders import costs, my_work, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import ALLOWED_TRANSITIONS, Priority, Source, WorkOrderStatusHistory, WoStatus, WoType

from .context_processors import NAV
from .decorators import web_view
from .htmx import is_partial, toast
from .views import get_wo

BODY = "my-work-body"
FROM_MY_WORK = "my_work"  # the hidden `from` of a modal a card opened (views_wo_costs; part B's completion modal)
TAKE_TEMPLATE, SCAN_TEMPLATE = "web/_my_work_take.html", "web/_my_work_scan.html"
WAITING_MODAL = "web/_my_work_waiting.html"
PRIORITY_ORDER = {p: i for i, p in enumerate(Priority.values)}  # the service's PRIORITY_RANK, for ordering in Python
RAIL = {Priority.CRITICAL: "crit", Priority.HIGH: "warn"}


# --- answering a card ----------------------------------------------------------------------------------------------------------

def from_my_work(request) -> bool:
    """Whether a modal was opened from a My work card (its hidden `from`, or the card's ?from= on the GET that opened it)."""
    data = request.POST if request.method == "POST" else request.GET
    return data.get("from") == FROM_MY_WORK


def saved_from_card(message: str, *events: str):
    """A modal a card opened saved: toast, tell the lists (wo-changed, and any `events`), and close the modal after settle, swapping
    nothing, so the technician stays on My work (which re-fetches itself on wo-changed) instead of landing in the drawer. A 200 with
    nothing to swap: htmx runs the after-settle trigger only when it swaps (a 204 never would)."""
    response = reswap(HttpResponse(""), "none")
    for event in ("wo-changed", *events):
        trigger_client_event(response, event, {})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def optional_template(name: str) -> str | None:
    """`name` when that template exists, else None: an include of a missing template fails the whole page."""
    try:
        get_template(name)
    except TemplateDoesNotExist:
        return None
    return name


# --- the cards -----------------------------------------------------------------------------------------------------------------

@dataclass
class Card:
    wo: object
    where: str
    problem: str              # the problem's first line (the template masks it: shown_text)
    due: str                  # "3 d late", "Due today", "Due Thu Oct 8"
    late: bool
    rail: str
    actions: list
    vendor: bool = False      # the Vendor chip (not on a vendor's own page, where every card is theirs)
    qualified: bool | None = None  # the assigned technician's credential for the device; None when not checked (a vendor's page)
    pm: str = ""              # a PM's procedure code and estimated hours
    waiting: dict | None = None  # {"days", "note", "pos"} for work waiting on parts
    take: bool = False        # a card in "You could take" (part C's Take button)


def _where(wo) -> str:
    """Where to go: a portal request's reported location (what the unit said), else the device's department and room."""
    if wo.source == Source.PORTAL and wo.reported_location.strip():
        return wo.reported_location.strip()
    asset = wo.asset
    return f"{asset.department} · Room {asset.room}" if asset.room else str(asset.department)


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def _due(wo, today) -> tuple[str, bool]:
    late = (today - wo.due_on).days
    if late > 0:
        return f"{late} d late", True
    if late == 0:
        return "Due today", False
    return f"Due {wo.due_on:%a} {wo.due_on:%b} {wo.due_on.day}", False


def _pm(wo) -> str:
    if wo.type != WoType.PM:
        return ""
    procedure = wo.asset.device_model.pm_procedure
    hours = f"{costs.plain(wo.estimated_hours)} h"
    return f"{procedure.code} · {hours}" if procedure is not None else f"{hours} estimated"


def _actions(user, wo) -> list[dict]:
    """The moves a card offers, by status and the user's levels: the drawer's rules (apps.workorders.permissions), checked again
    by each endpoint. None on in-house work nobody is assigned to: a CE manager assigns it first, as the drawer says. A PM completes
    from open (part B's one step), so it is not offered Start; a repair is started first (its start date counts for downtime)."""
    if wo.assigned_to_id is None and not wo.vendor_service:
        return []
    status, number = wo.status, wo.number

    def may(to):
        return to in ALLOWED_TRANSITIONS[status] and wo_perms.can_transition(user, status, to)

    def modal(label, url_name, primary=False):
        return {"label": label, "url": reverse(url_name, args=[number]), "modal": True, "primary": primary}

    def move(label, to):
        return {"label": label, "url": reverse("web:wo_status", args=[number]), "to": to, "primary": True}

    out = []
    if status == WoStatus.OPEN:
        if wo.type == WoType.PM and may(WoStatus.IN_PROGRESS) and wo_perms.can_transition(user, WoStatus.IN_PROGRESS, WoStatus.COMPLETED):
            out.append(modal("Complete", "web:wo_complete", primary=True))
        elif may(WoStatus.IN_PROGRESS):
            out.append(move("Start", WoStatus.IN_PROGRESS))
    elif status == WoStatus.IN_PROGRESS:
        if may(WoStatus.COMPLETED):
            out.append(modal("Complete", "web:wo_complete", primary=True))
        if may(WoStatus.AWAITING_PARTS):
            out.append(modal("Waiting on parts", "web:wo_waiting"))
    elif status == WoStatus.AWAITING_PARTS and may(WoStatus.IN_PROGRESS):
        out.append(move("Resume", WoStatus.IN_PROGRESS))
    if wo_perms.can_record_work(user) and not costs.locked_reason(wo):
        out.append(modal("Log time", "web:wo_labor_add"))
        if status == WoStatus.AWAITING_PARTS:
            out.append(modal("Add part", "web:wo_part_add"))  # the part ordered, with its PO number
    return out


def _waiting(wos, today) -> dict:
    """For work waiting on parts, by work order id: days since it last began waiting (its status history; None if it has no such
    row, e.g. imported that way), the note it was given then, and its part lines' PO numbers."""
    if not wos:
        return {}
    prefetch_related_objects(wos, "part_lines")
    began = {}
    for row in WorkOrderStatusHistory.objects.filter(work_order__in=wos, to_status=WoStatus.AWAITING_PARTS).order_by("created_at"):
        began[row.work_order_id] = row  # the last one wins
    out = {}
    for wo in wos:
        row = began.get(wo.pk)
        out[wo.pk] = {"days": (today - timezone.localtime(row.created_at).date()).days if row else None, "note": row.note if row else "",
                      "pos": sorted({line.po_number for line in wo.part_lines.all() if line.po_number})}
    return out


def _cards(user, groups, today) -> dict:
    """Every group's cards. The technician's own credentials are read once (qualification on each card's device)."""
    whose = groups.whose
    technician = whose.technician
    if technician is not None:
        prefetch_related_objects([technician], "credentials")
    company = whose.kind == my_work.COMPANY
    waiting = _waiting(groups.waiting, today)

    def card(wo, take=False):
        due, late = _due(wo, today)
        return Card(wo=wo, where=_where(wo), problem=_first_line(wo.problem), due=due, late=late, rail=RAIL.get(wo.priority, "info"),
                    actions=_actions(user, wo), vendor=wo.vendor_service and not company,
                    qualified=qualification(technician, wo.asset, today).ok if technician is not None else None,
                    pm=_pm(wo), waiting=waiting.get(wo.pk), take=take)

    return {"repairs": [card(wo) for wo in groups.repairs], "recalls": [[card(wo) for wo in b["work_orders"]] for b in groups.recalls],
            "pms_today": [card(wo) for wo in groups.pms_today], "waiting": [card(wo) for wo in groups.waiting],
            "coming": [card(wo) for wo in groups.coming], "takeable": [card(wo, take=True) for wo in groups.takeable]}


def _repairs_rows(groups, cards, open_batches) -> list[dict]:
    """Repairs and requests with the recall batches among them, by priority then due date: a batch ranks by its most urgent work
    order and earliest due date, after a repair that ranks the same. Each row is {"card": Card} or {"batch": {...}}."""
    rows = [((PRIORITY_ORDER.get(c.wo.priority, 9), c.wo.due_on, 0), {"card": c}) for c in cards["repairs"]]
    for batch, batch_cards in zip(groups.recalls, cards["recalls"], strict=True):
        first = min(batch_cards, key=lambda c: c.wo.due_on)
        rows.append(((min(PRIORITY_ORDER.get(c.wo.priority, 9) for c in batch_cards), first.wo.due_on, 1), {"batch": {
            "alert": batch["alert"], "cards": batch_cards, "due": first.due, "late": first.late,
            "open": str(batch["alert"].pk) in open_batches}}))
    rows.sort(key=lambda row: row[0])
    return [item for _key, item in rows]


def _credential_note(user, technician, today) -> dict | None:
    """One line when the technician's own active credentials include one that expires within the warning window, with a link to
    their credentials for whoever may open that tab (Users and access View; never a scoped user)."""
    if technician is None:
        return None
    soon = today + timedelta(days=settings.CREDENTIAL_EXPIRY_WARNING_DAYS)
    expiring = sorted((c for c in technician.credentials.all() if c.status == Credential.Status.ACTIVE and c.expires_on and today <= c.expires_on <= soon),
                      key=lambda c: (c.expires_on, c.value))
    if not expiring:
        return None
    first = expiring[0]
    days = (first.expires_on - today).days
    when = "today" if days == 0 else f"in {days} d, on {first.expires_on:%b} {first.expires_on.day}"
    if len(expiring) == 1:
        text = f"Your credential for {first.value} expires {when}."
    else:
        text = f"{len(expiring)} of your credentials expire within {settings.CREDENTIAL_EXPIRY_WARNING_DAYS} days; the first, {first.value}, {when}."
    can_see = user.has_level(Module.USERS, Level.VIEW) and not scoping.is_scoped(user)
    return {"text": text, "url": f"{reverse('web:credentials')}?{urlencode({'technician': technician.pk})}" if can_see else ""}


def _list_url(technician, **params) -> str:
    """The Work orders list filtered to the technician (assigned=<id>), or as a vendor sees it (their share, no technician filter)."""
    query = {**({"assigned": technician.pk} if technician is not None else {}), **params}
    return f"{reverse('web:workorders')}?{urlencode(query)}"


def nav_badge(due: dict) -> dict:
    """The nav's My work row with its badge (`due`: what is due today or late, hot when something is late, as apps.reports.services'
    _my_work_badge counts it), for the list partial to swap out of band, as base.html draws the row."""
    _key, label, icon, url_name, _module = next(row for row in NAV if row[0] == "my_work")
    return {"url": reverse(url_name), "label": label, "icon": icon, "count": due["count"] or None, "hot": due["late"]}


def my_work_context(request) -> dict:
    user = request.user
    today = timezone.localdate()
    groups = my_work.groups(user, today)
    ctx = {"nav_active": "my_work", "today": today, "groups": groups}
    if groups.whose.kind is None:
        return ctx
    technician = groups.whose.technician
    hours = my_work.hours(user, today)
    cards = _cards(user, groups, today)
    count, late = my_work.due_count(user, today)
    rows = _repairs_rows(groups, cards, set(request.GET.getlist("open")))
    return {**ctx, **{k: v for k, v in cards.items() if k != "recalls"}, "repairs": rows,
            "repairs_count": len(groups.repairs) + sum(len(b["work_orders"]) for b in groups.recalls),
            "hours": {k: costs.plain(v) for k, v in hours.items()} if hours is not None else None,
            "due": {"count": count, "late": late}, "credential_note": _credential_note(user, technician, today),
            "later_url": _list_url(technician, type=WoType.PM), "done_url": _list_url(technician, status=WoStatus.COMPLETED, open=0),
            "take_template": optional_template(TAKE_TEMPLATE), "scan_template": optional_template(SCAN_TEMPLATE)}


@web_view(Module.WORKORDERS, Level.VIEW, scoped=True)  # narrowed to the user's share (my_work.mine starts from scoping.work_orders)
def my_work_page(request):
    ctx = my_work_context(request)
    if is_partial(request, BODY):  # the nav's badge comes along, so finishing or starting work updates it too
        badge = nav_badge(ctx["due"]) if ctx["groups"].whose.kind else None
        return render(request, "web/_my_work_body.html", {**ctx, "oob_badge": badge})
    return render(request, "web/my_work.html", ctx)


# --- Waiting on parts --------------------------------------------------------------------------------------------------------

def _waiting_blocker(wo) -> str:
    if wo.status == WoStatus.AWAITING_PARTS:
        return f"{wo.number} is already waiting on parts."
    if WoStatus.AWAITING_PARTS not in ALLOWED_TRANSITIONS[wo.status]:
        return f"{wo.number} is {wo.get_status_display().lower()}: only open work can wait on parts."
    return ""


def _waiting_modal(request, wo, *, note="", error=""):
    return render(request, WAITING_MODAL, {"wo": wo, "note": note, "error": error, "blocker": _waiting_blocker(wo),
                                           "note_max": wo_services.STATUS_NOTE_MAX})


@web_view(wo_perms.MODULE, Level.EDIT, scoped=True)  # a vendor's on their company's work: get_wo narrows to the share, a 404 outside it
def wo_waiting(request, number):
    """Waiting on parts from a card: a short note of what the work waits for (part, PO number, expected date; never a patient's
    details), kept as the status history's note through change_status. The drawer's own button moves it without one (wo_status)."""
    if not wo_perms.can_transition(request.user, WoStatus.IN_PROGRESS, WoStatus.AWAITING_PARTS):
        raise PermissionDenied
    wo = get_wo(request, number)
    if request.method != "POST":
        if not request.htmx:  # only ever a modal: a direct visit opens the work order
            return redirect("web:wo", number=wo.number)
        return _waiting_modal(request, wo)
    note = request.POST.get("note", "").strip()
    if _waiting_blocker(wo):
        return _waiting_modal(request, wo, note=note)
    try:
        wo_services.change_status(wo, WoStatus.AWAITING_PARTS, by=request.user, note=note)
    except ValidationError as e:
        return _waiting_modal(request, get_wo(request, number), note=note, error=e.messages[0])
    return saved_from_card(f"{wo.number}: waiting on parts")
