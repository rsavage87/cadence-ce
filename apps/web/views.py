"""
HTMX web UI. Views parse input, call services, and render; they never set status fields themselves.

Drawers (device, work order) and the new work order modal are partials swapped into #drawer and
#modal-card. The same URLs render a full page when opened directly, so links and reloads work.

Slice 16: a scoped user (apps.workorders.scoping: a vendor technician's company, a clinical requester's unit) sees only their share
of the facility. The views here that admit them (web_view's `scoped=True`) narrow every list, count, and search to it, and a device
or work order outside it is a 404, as another facility's is. The rest refuse them (closed by default).

Slice 21: a page works on the facility's day. The tenant middleware activates the facility's time zone, so timezone.localdate() is
its today (never date.today(), the server's); a view asks once and passes that day to the services that take one, so one page reads
one day. Times (the work order timeline, History) are aware and render in the facility's zone.
"""
from datetime import date, timedelta
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Min
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.credentials.models import Technician
from apps.credentials.services import qualification, qualified_technicians
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq_services
from apps.equipment.models import Asset
from apps.equipment.services import FleetBucket, asset_service_summary, filter_assets, fleet_summary, search_assets
from apps.facility.services import asset_request_url, get_settings
from apps.recalls.models import AlertMatch
from apps.reports.services import overview_page
from apps.workorders import permissions as wo_perms
from apps.workorders import scoping
from apps.workorders import services as wo_services
from apps.workorders.models import ALLOWED_TRANSITIONS, OPEN_STATUSES, Source, WorkOrder, WoStatus, WoType

from . import asset_tabs, history_tabs
from . import overview as ov
from .context_processors import nav_entries
from .decorators import web_view
from .forms import (
    VENDOR,
    NewWorkOrderForm,
    asset_filter_options,
    parse_asset_filters,
    parse_uuid,
    parse_work_order_filters,
    technician_choices,
    vendor_name_for,
)
from .htmx import PAGE_SIZE, is_partial, toast

# --- Overview -------------------------------------------------------------------------------------

def _parse_month(params, today: date) -> tuple[int, int]:
    try:
        year, month = int(params.get("y", today.year)), int(params.get("m", today.month))
        start = date(year, month, 1)
    except (TypeError, ValueError):
        return today.year, today.month
    if start > today:
        return today.year, today.month
    return year, month


@web_view(scoped=True)  # admits a scoped user only to send them on: the Overview is the whole facility's
def overview(request):
    if scoping.is_scoped(request.user) or not request.user.has_level(Module.REPORTS, Level.VIEW):
        # The Overview is the home page; send people without report access, or who see only their share of the facility, to the
        # first screen they can use.
        for key, _label, _icon, url_name, _module in nav_entries(request.user):
            if key != "overview":
                return redirect(url_name)
        raise PermissionDenied
    today = timezone.localdate()
    year, month = _parse_month(request.GET, today)
    data = overview_page(year, month, today)
    first = WorkOrder.objects.aggregate(first=Min("opened_on"))["first"] or today
    recalls_url = reverse("web:recalls") if request.user.has_level(Module.RECALLS, Level.VIEW) else None
    pm_url = reverse("web:pm") if request.user.has_level(Module.PM, Level.VIEW) else None
    attention = data["attention"] if recalls_url else [it for it in data["attention"] if "recall" not in it]
    return render(request, "web/overview.html", {
        "nav_active": "overview", "today": today, "year": year, "month": month, "d": data, "k": data["k"],
        "tiles": ov.kpi_tiles(data, recalls_url=recalls_url, pm_url=pm_url), "strip": ov.fleet_strip(data["buckets"]),
        "pm_chart": ov.pm_trend_chart(data["pm_series"], data["pm_series_life_support"], target=data["targets"]["pm_on_time"]),
        "type_chart": ov.opened_by_type_chart(data["opened_by_type"]), "spend_chart": ov.spend_chart(data["spend_by_category"]),
        "attention": attention[:9], "attention_more": max(0, len(attention) - 9), "attention_total": len(attention),
        **ov.month_options(today, first.year),
    })


# --- Equipment ------------------------------------------------------------------------------------

# (sort key, header, css class) for the Equipment table.
EQUIPMENT_COLUMNS = [("tag", "Asset tag", ""), ("device", "Device", ""), ("location", "Location", ""), ("risk", "Risk class", ""), ("status", "Status", ""),
                     ("next_pm", "Next PM", ""), ("age", "Age", ""), ("support", "Support", ""), ("cost", "Acquisition", "num")]


def scoped_assets(user):
    """A scoped user's devices (apps.workorders.scoping), or None for the whole facility (what the services take without one)."""
    return scoping.assets(user) if scoping.is_scoped(user) else None


def _equipment_context(request) -> dict:
    mine = scoped_assets(request.user)  # the list, its filters, and the page head's counts over the user's devices only
    options = asset_filter_options(mine)
    f = parse_asset_filters(request.GET, options)
    today = timezone.localdate()  # the facility's: a PM falls due, and a contract ends, on its day
    page = Paginator(filter_assets(f, today, qs=mine), PAGE_SIZE).get_page(request.GET.get("page"))
    bucket_label = FleetBucket(f.bucket).label if f.bucket else ""
    return {"nav_active": "equipment", "list_url": reverse("web:equipment"), "f": f, "options": options, "page": page, "bucket_label": bucket_label,
            "summary": fleet_summary(today, qs=mine), "sort_columns": EQUIPMENT_COLUMNS,
            "can_add_device": eq_perms.can_add(request.user) and mine is None}  # asset_new refuses scoped users


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def equipment(request):
    ctx = _equipment_context(request)
    if is_partial(request, "eq-table"):  # the page head's counts come along, so a device added or retired shows there too
        return render(request, "web/_equipment_table.html", {**ctx, "oob_summary": True})
    return render(request, "web/equipment.html", ctx)


def _model_recalls(device_model_id) -> list:
    """Alert matches for one device model, newest notice first. Alert is global; the matches are the tenant's."""
    return list(AlertMatch.objects.filter(device_model_id=device_model_id).select_related("alert").order_by("-alert__published_on", "-alert__created_at"))


def asset_drawer_context(request, asset) -> dict:
    """Also used by the contracts screen to re-render the device drawer after its support editor saves.

    A scoped user gets the Overview and Work orders tabs only, every figure in them over their own work orders. The PM schedule,
    Costs, and Recalls tabs are the PM schedule's, the Reports', and the Recalls screen's views of the device (every work order on
    it, the facility's spend, the model's notices) and link into those screens, which refuse them; so do the changes the drawer
    offers (new work order, edit, status, support, the contract and model links), and none is shown to them."""
    user = request.user
    scoped = scoping.is_scoped(user)
    can_view_recalls = user.has_level(Module.RECALLS, Level.VIEW) and not scoped
    # Tabs a role cannot use do not exist for it: PM schedule needs PM View, Costs needs Reports View (service spend, contract
    # share, and replacement outlook are the Reports' figures; a vendor technician or clinical requester has no business with
    # them), Recalls needs recalls View. The mock's order: Overview, PM schedule, Work orders, Costs, Recalls; then History (slice
    # 20: the device's own changes, for Equipment View and never a scoped user; apps.web.history_tabs).
    tabs = [t for t, ok in (("overview", True), ("pm", user.has_level(Module.PM, Level.VIEW) and not scoped), ("wo", True),
                            ("costs", user.has_level(Module.REPORTS, Level.VIEW) and not scoped), ("recalls", can_view_recalls),
                            ("history", history_tabs.allowed(user, "devices"))) if ok]
    tab = request.GET.get("tab") if request.GET.get("tab") in tabs else "overview"
    today = timezone.localdate()
    summary = asset_service_summary(asset, today, work_orders=scoping.work_orders(user) if scoped else None)
    recalls = _model_recalls(asset.device_model_id) if can_view_recalls else []
    if tab == "history":
        extra = history_tabs.context(request, asset, "devices", reverse("web:asset", args=[asset.tag]), tab=True)
    else:
        extra = asset_tabs.pm_tab(asset, today) if tab == "pm" else asset_tabs.costs_tab(asset, today) if tab == "costs" else {}
    return {"asset": asset, "tab": tab, "summary": summary, "recent": summary["work_orders"][:4],
            # The facility's staff and their credentials (the API closes qualified_technicians to scoped users too): not theirs to read.
            "qualified": [] if scoped else qualified_technicians(asset, today),
            "portal_url": asset_request_url(asset), "can_create_wo": user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL) and not scoped,
            "can_view_wo": user.has_level(Module.WORKORDERS, Level.VIEW), "scoped": scoped,
            "can_view_recalls": can_view_recalls, "recalls": recalls, "tabs": tabs,
            "open_recall": any(m.status == AlertMatch.Status.NEEDS_ACTION for m in recalls),
            # slice 12: editing the device and its status buttons; the PM schedule and Costs tabs (apps/web/asset_tabs.py)
            "can_edit_device": eq_perms.can_edit(user) and not scoped, "status_actions": [] if scoped else eq_services.status_actions(asset, user),
            **extra}


def get_asset(request, tag):
    """One device by tag, as this user may see it: another facility's, or one outside a scoped user's share, is a 404."""
    qs = Asset.objects.select_related("device_model", "department", "contract", "tenant")
    return get_object_or_404(scoping.assets(request.user, qs), tag=tag)


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def asset_detail(request, tag):
    asset = get_asset(request, tag)
    if history_tabs.asked(request, tab=True):  # the History tab or its Show older: never a scoped user's (a 403, not another tab)
        history_tabs.require(request.user, "devices")
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, asset, "devices", reverse("web:asset", args=[asset.tag]), tab=True)
    ctx = asset_drawer_context(request, asset)
    # Scan tag pushes this URL; htmx reloads a pushed URL it no longer has in its history cache into <body>, so that gets the page
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, "web/_asset_drawer.html", ctx)
    return render(request, "web/equipment.html", {**_equipment_context(request), **ctx, "drawer_template": "web/_asset_drawer.html"})


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def asset_search(request):
    q = request.GET.get("asset_q", "").strip()
    if len(q) < 2:
        return HttpResponse("")
    return render(request, "web/_asset_picks.html", {"assets": search_assets(q, qs=scoped_assets(request.user))})


# --- Work orders ----------------------------------------------------------------------------------

def _workorders_context(request) -> dict:
    user = request.user
    scoped = scoping.is_scoped(user)
    mine = scoping.work_orders(user)  # the list, the board, and the page head's counts over the user's work orders only
    techs = [] if scoped else list(Technician.objects.filter(is_active=True))  # the facility's roster is not a scoped user's to read
    f = parse_work_order_filters(request.GET, {str(t.id) for t in techs})
    mode = "board" if request.GET.get("mode") == "board" else "list"
    today = timezone.localdate()
    open_wos = scoping.work_orders(user, wo_services.open_work_orders())
    # The portal requests waiting are a CE manager's to assign, and a scoped user cannot assign: the note is not for them.
    unassigned_portal = 0 if scoped else wo_services.unassigned_portal_requests().count()
    ctx = {"nav_active": "workorders", "list_url": reverse("web:workorders"), "f": f, "mode": mode, "technicians": techs,
           "types": WoType.choices, "statuses": WoStatus.choices,
           "unassigned_portal": unassigned_portal, "open_count": open_wos.count(),
           # Quoted as written: "(policy: Triage 7 a.m. to 7 p.m.)." keeps the policy's own punctuation intact.
           "portal_policy": get_settings().policy_portal if unassigned_portal else "",
           "past_due": open_wos.filter(due_on__lt=today).count(),
           "done_7d": mine.filter(completed_on__gte=today - timedelta(days=wo_services.BOARD_RECENT_DAYS)).count(),
           "can_create": user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL) and not scoped}  # wo_new refuses scoped users
    if mode == "board":
        ctx["columns"] = wo_services.board_columns(f, today, qs=mine)
    else:
        ctx["page"] = Paginator(wo_services.filter_work_orders(f, qs=mine), PAGE_SIZE).get_page(request.GET.get("page"))
    return ctx


@web_view(Module.WORKORDERS, Level.VIEW, scoped=True)
def workorders(request):
    ctx = _workorders_context(request)
    if is_partial(request, "wo-body"):
        return render(request, "web/_wo_body.html", ctx)
    return render(request, "web/workorders.html", ctx)


# (label, primary) for each move the drawer offers, matching the mock's footer buttons.
WO_ACTIONS = {
    WoStatus.OPEN: [(WoStatus.IN_PROGRESS, "Start work", True)],
    WoStatus.IN_PROGRESS: [(WoStatus.COMPLETED, "Mark completed", True), (WoStatus.AWAITING_PARTS, "Waiting on parts", False)],
    WoStatus.AWAITING_PARTS: [(WoStatus.IN_PROGRESS, "Parts received, resume", True)],
    WoStatus.COMPLETED: [(WoStatus.CLOSED, "Review and close", True)],
    WoStatus.CLOSED: [(WoStatus.IN_PROGRESS, "Reopen", False)],
    WoStatus.CANCELLED: [(WoStatus.OPEN, "Reopen", False)],
}


def _wo_drawer_context(request, wo) -> dict:
    from . import views_wo_complete, views_wo_costs  # slice 15's sections; they render this drawer, so imported here

    user = request.user
    scoped = scoping.is_scoped(user)
    today = timezone.localdate()
    unassigned = wo.assigned_to_id is None and not wo.vendor_service
    actions = [] if unassigned else [{"to": to, "label": label, "primary": primary} for to, label, primary in WO_ACTIONS.get(wo.status, [])
                                     if to in ALLOWED_TRANSITIONS[wo.status] and wo_perms.can_transition(user, wo.status, to)]
    actions = views_wo_complete.drawer_actions(user, wo, actions)  # slice 24: Mark completed on a PM still open, in one step
    is_open = wo.status in OPEN_STATUSES
    can_assign = is_open and wo_perms.can_assign(user) and not scoped  # wo_assign refuses scoped users
    current = VENDOR if wo.vendor_service else (str(wo.assigned_to_id) if wo.assigned_to_id else "")
    # The Recalls screen is keyed by AlertMatch, so a recall work order links through the match for this alert and the device's model.
    recall_match = (AlertMatch.objects.filter(alert_id=wo.alert_id, device_model_id=wo.asset.device_model_id).first()
                    if wo.alert_id and user.has_level(Module.RECALLS, Level.VIEW) and not scoped else None)
    # Slice 20: the History section (the work order's changes with its labor and part lines'), never a scoped user's. It loads when
    # asked; a drawer opened with ?history=1 (wo_detail checked who may) shows it open.
    can_history = history_tabs.allowed(user, "work_orders")
    wo_url = reverse("web:wo", args=[wo.number])
    hist = history_tabs.context(request, wo, "work_orders", wo_url, tab=False) if can_history and request.GET.get("history") else {}
    return {
        "can_history": can_history, "hist_url": f"{wo_url}?history=1", **hist,
        "wo": wo, "asset": wo.asset, "is_open": is_open, "unassigned": unassigned, "actions": actions,
        "late_days": (today - wo.due_on).days if is_open and wo.due_on < today else 0,
        "open_days": (today - wo.opened_on).days,
        "qual": qualification(wo.assigned_to, wo.asset, today) if wo.assigned_to_id else None,
        "can_assign": can_assign, "assign_choices": technician_choices(wo.asset) if can_assign else [], "assign_current": current,
        "assignment_policy": get_settings().policy_assignment if can_assign else "",
        "can_note": user.has_level(wo_perms.MODULE, wo_perms.NOTE_LEVEL),
        # A work order in a scoped user's share is on a device in it (scoping's rules); checked anyway, the link must never 404.
        "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW) and scoping.can_see_asset(user, wo.asset),
        "timeline": wo_services.timeline(wo),
        "labor_hours": sum(float(line.hours) for line in wo.labor_lines.all()),
        "is_portal": wo.source == Source.PORTAL,
        "recall_match": recall_match,
        **views_wo_costs.costs_context(request, wo),
        **views_wo_complete.results_context(request, wo),
    }


def get_wo(request, number):
    """One work order by number, as this user may see it: another facility's, or one outside a scoped user's share, is a 404."""
    qs = (WorkOrder.objects.select_related("asset", "asset__device_model", "asset__department", "asset__contract", "assigned_to", "alert")
          .prefetch_related("labor_lines", "part_lines", "assigned_to__credentials"))
    return get_object_or_404(scoping.work_orders(request.user, qs), number=number)


def _render_wo_drawer(request, wo, status=200):
    return render(request, "web/_wo_drawer.html", _wo_drawer_context(request, get_wo(request, wo.number)), status=status)


@web_view(Module.WORKORDERS, Level.VIEW, scoped=True)
def wo_detail(request, number):
    wo = get_wo(request, number)
    if history_tabs.asked(request, tab=False):  # the History section or its Show older: never a scoped user's
        history_tabs.require(request.user, "work_orders")
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, wo, "work_orders", reverse("web:wo", args=[wo.number]), tab=False)
    if request.htmx:
        return _render_wo_drawer(request, wo)
    return render(request, "web/workorders.html", {**_workorders_context(request), **_wo_drawer_context(request, wo), "drawer_template": "web/_wo_drawer.html"})


@require_POST
@web_view(Module.WORKORDERS, Level.EDIT, scoped=True)
def wo_status(request, number):
    wo = get_wo(request, number)
    to_status = request.POST.get("to", "")
    if not wo_perms.can_transition(request.user, wo.status, to_status):
        raise PermissionDenied
    try:
        if to_status == WoStatus.COMPLETED:
            # Completing records the resolution and a PM's results: the drawer's Mark completed (views_wo_complete), never this.
            raise ValidationError(f"Complete {wo.number} with Mark completed: it records what was done.")
        wo_services.change_status(wo, to_status, by=request.user)
        message = f"{wo.number}: {wo.get_status_display().lower()}"
    except ValidationError as e:
        message = e.messages[0]
    response = _render_wo_drawer(request, wo)
    trigger_client_event(response, "wo-changed", {})
    return toast(response, message)


@require_POST
@web_view(wo_perms.MODULE, wo_perms.ASSIGN_LEVEL)  # not for scoped users: the choices are the facility's technicians
def wo_assign(request, number):
    wo = get_wo(request, number)
    if wo.status not in OPEN_STATUSES:
        return toast(_render_wo_drawer(request, wo), "Only open work orders can be reassigned.")
    choice = request.POST.get("assignee", "")
    if choice == VENDOR:
        wo_services.assign(wo, vendor_name=vendor_name_for(wo.asset), by=request.user)
    else:
        tech = Technician.objects.filter(pk=parse_uuid(choice), is_active=True).first() if parse_uuid(choice) else None
        if tech is None:
            return toast(_render_wo_drawer(request, wo), "Choose a technician or vendor service.")
        wo_services.assign(wo, technician=tech, by=request.user)
    note = wo.status_history.last().note
    response = _render_wo_drawer(request, wo)
    trigger_client_event(response, "wo-changed", {})
    return toast(response, f"{wo.number}: {note[0].lower()}{note[1:]}" if note else f"{wo.number} assigned")


@require_POST
@web_view(wo_perms.MODULE, wo_perms.NOTE_LEVEL, scoped=True)
def wo_note(request, number):
    wo = get_wo(request, number)
    try:
        wo_services.add_note(wo, request.POST.get("text", ""), by=request.user)
        message = "Note added"
    except ValidationError as e:
        message = e.messages[0]
    return toast(_render_wo_drawer(request, wo), message)


@web_view(wo_perms.MODULE, wo_perms.CREATE_LEVEL)  # not for scoped users: a vendor did not ask for the work, and a requester uses the portal
def wo_new(request):
    can_assign = wo_perms.can_assign(request.user)
    if request.method != "POST":
        # GET re-renders the form, e.g. after a device is picked; keep what was typed so far.
        initial = {"requester": request.user.get_full_name(), **{k: v for k, v in request.GET.items() if k in NewWorkOrderForm.base_fields}}
        form = NewWorkOrderForm(initial=initial, can_assign=can_assign)
        return render(request, "web/_wo_new.html", {"form": form, "asset": form.asset_obj})
    form = NewWorkOrderForm(request.POST, can_assign=can_assign)
    if not form.is_valid():
        return render(request, "web/_wo_new.html", {"form": form, "asset": form.asset_obj})
    d = form.cleaned_data
    wo = wo_services.create_work_order(asset=d["asset"], type=d["type"], priority=d["priority"], problem=d["problem"],
                                       requester=(d["requester"] or request.user.get_full_name())[:120], created_by=request.user, tag_out=d["tag_out"])
    assignee = d.get("assignee")
    if assignee == VENDOR:
        wo_services.assign(wo, vendor_name=vendor_name_for(wo.asset), by=request.user)
    elif assignee and parse_uuid(assignee):
        tech = Technician.objects.filter(pk=parse_uuid(assignee), is_active=True).first()
        if tech:
            wo_services.assign(wo, technician=tech, by=request.user)
    response = retarget(_render_wo_drawer(request, wo), "#drawer")
    trigger_client_event(response, "wo-changed", {})
    toast(response, f"{wo.number} created")
    # After settle: closing the modal detaches the form that sent this request, which would cancel the swap and lose the other events.
    return trigger_client_event(response, "modal-close", {}, after="settle")


# --- Global search --------------------------------------------------------------------------------

@web_view(scoped=True)
def search(request):
    """Topbar search: an exact work order number or asset tag opens it, and so does an imported work order's number in the previous
    system (slice 23; a tag that reads the same wins, as before imports); anything else searches equipment. A scoped user's search
    finds only their share: another number or tag is searched as text in their Equipment list, where it matches nothing of theirs."""
    q = request.GET.get("q", "").strip()
    if q:
        wo = scoping.work_orders(request.user).filter(number__iexact=q).first()
        if wo:
            return redirect("web:wo", number=wo.number)
        asset = scoping.assets(request.user).filter(tag__iexact=q).first()
        if asset:
            return redirect("web:asset", tag=asset.tag)
        wo = scoping.work_orders(request.user).filter(legacy_number__iexact=q).first()
        if wo:
            return redirect("web:wo", number=wo.number)
    return redirect(f"{reverse('web:equipment')}?{urlencode({'q': q})}" if q else reverse("web:equipment"))
