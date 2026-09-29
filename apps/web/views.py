"""
HTMX web UI. Views parse input, call services, and render; they never set status fields themselves.

Drawers (device, work order) and the new work order modal are partials swapped into #drawer and
#modal-card. The same URLs render a full page when opened directly, so links and reloads work.
"""
from datetime import date, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Min
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.credentials.models import Technician
from apps.credentials.services import qualification, qualified_technicians
from apps.equipment.models import Asset
from apps.equipment.services import FleetBucket, asset_service_summary, filter_assets, fleet_summary, search_assets
from apps.facility.services import get_settings
from apps.recalls.models import AlertMatch
from apps.reports.services import overview_page
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import ALLOWED_TRANSITIONS, OPEN_STATUSES, Source, WorkOrder, WoStatus, WoType

from . import overview as ov
from .context_processors import NAV
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


@web_view()
def overview(request):
    if not request.user.has_level(Module.REPORTS, Level.VIEW):
        # The Overview is the home page; send people without report access to the first screen they can use.
        for _key, _label, _icon, url_name, module in NAV:
            if request.user.has_level(module, Level.VIEW):
                return redirect(url_name)
        raise PermissionDenied
    today = date.today()
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


def _equipment_context(request) -> dict:
    options = asset_filter_options()
    f = parse_asset_filters(request.GET, options)
    page = Paginator(filter_assets(f), PAGE_SIZE).get_page(request.GET.get("page"))
    bucket_label = FleetBucket(f.bucket).label if f.bucket else ""
    return {"nav_active": "equipment", "list_url": reverse("web:equipment"), "f": f, "options": options, "page": page, "bucket_label": bucket_label,
            "summary": fleet_summary(), "sort_columns": EQUIPMENT_COLUMNS}


@web_view(Module.EQUIPMENT, Level.VIEW)
def equipment(request):
    ctx = _equipment_context(request)
    if is_partial(request, "eq-table"):
        return render(request, "web/_equipment_table.html", ctx)
    return render(request, "web/equipment.html", ctx)


def _portal_url(asset) -> str:
    return f"{settings.PORTAL_BASE_URL.rstrip('/')}{reverse('portal:request', args=[asset.tenant.slug])}?{urlencode({'asset': asset.tag})}"


def _model_recalls(device_model_id) -> list:
    """Alert matches for one device model, newest notice first. Alert is global; the matches are the tenant's."""
    return list(AlertMatch.objects.filter(device_model_id=device_model_id).select_related("alert").order_by("-alert__published_on", "-alert__created_at"))


def asset_drawer_context(request, asset) -> dict:
    """Also used by the contracts screen to re-render the device drawer after its support editor saves."""
    can_view_recalls = request.user.has_level(Module.RECALLS, Level.VIEW)
    tabs = ("overview", "wo", "recalls") if can_view_recalls else ("overview", "wo")  # the Recalls tab does not exist for roles without recalls View
    tab = request.GET.get("tab") if request.GET.get("tab") in tabs else "overview"
    summary = asset_service_summary(asset)
    recalls = _model_recalls(asset.device_model_id) if can_view_recalls else []
    return {"asset": asset, "tab": tab, "summary": summary, "recent": summary["work_orders"][:4], "qualified": qualified_technicians(asset),
            "portal_url": _portal_url(asset), "can_create_wo": request.user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL),
            "can_view_wo": request.user.has_level(Module.WORKORDERS, Level.VIEW),
            "can_view_recalls": can_view_recalls, "recalls": recalls,
            "open_recall": any(m.status == AlertMatch.Status.NEEDS_ACTION for m in recalls)}


@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_detail(request, tag):
    asset = get_object_or_404(Asset.objects.select_related("device_model", "department", "contract", "tenant"), tag=tag)
    ctx = asset_drawer_context(request, asset)
    if request.htmx:
        return render(request, "web/_asset_drawer.html", ctx)
    return render(request, "web/equipment.html", {**_equipment_context(request), **ctx, "drawer_template": "web/_asset_drawer.html"})


@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_search(request):
    q = request.GET.get("asset_q", "").strip()
    if len(q) < 2:
        return HttpResponse("")
    return render(request, "web/_asset_picks.html", {"assets": search_assets(q)})


# --- Work orders ----------------------------------------------------------------------------------

def _workorders_context(request) -> dict:
    techs = list(Technician.objects.filter(is_active=True))
    f = parse_work_order_filters(request.GET, {str(t.id) for t in techs})
    mode = "board" if request.GET.get("mode") == "board" else "list"
    today = date.today()
    open_wos = wo_services.open_work_orders()
    unassigned_portal = wo_services.unassigned_portal_requests().count()
    ctx = {"nav_active": "workorders", "list_url": reverse("web:workorders"), "f": f, "mode": mode, "technicians": techs,
           "types": WoType.choices, "statuses": WoStatus.choices,
           "unassigned_portal": unassigned_portal, "open_count": open_wos.count(),
           # Quoted as written: "(policy: Triage 7 a.m. to 7 p.m.)." keeps the policy's own punctuation intact.
           "portal_policy": get_settings().policy_portal if unassigned_portal else "",
           "past_due": open_wos.filter(due_on__lt=today).count(),
           "done_7d": WorkOrder.objects.filter(completed_on__gte=today - timedelta(days=wo_services.BOARD_RECENT_DAYS)).count(),
           "can_create": request.user.has_level(wo_perms.MODULE, wo_perms.CREATE_LEVEL)}
    if mode == "board":
        ctx["columns"] = wo_services.board_columns(f)
    else:
        ctx["page"] = Paginator(wo_services.filter_work_orders(f), PAGE_SIZE).get_page(request.GET.get("page"))
    return ctx


@web_view(Module.WORKORDERS, Level.VIEW)
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
    today = date.today()
    unassigned = wo.assigned_to_id is None and not wo.vendor_service
    actions = [] if unassigned else [{"to": to, "label": label, "primary": primary} for to, label, primary in WO_ACTIONS.get(wo.status, [])
                                     if to in ALLOWED_TRANSITIONS[wo.status] and wo_perms.can_transition(request.user, wo.status, to)]
    is_open = wo.status in OPEN_STATUSES
    can_assign = is_open and wo_perms.can_assign(request.user)
    current = VENDOR if wo.vendor_service else (str(wo.assigned_to_id) if wo.assigned_to_id else "")
    # The Recalls screen is keyed by AlertMatch, so a recall work order links through the match for this alert and the device's model.
    recall_match = (AlertMatch.objects.filter(alert_id=wo.alert_id, device_model_id=wo.asset.device_model_id).first()
                    if wo.alert_id and request.user.has_level(Module.RECALLS, Level.VIEW) else None)
    return {
        "wo": wo, "asset": wo.asset, "is_open": is_open, "unassigned": unassigned, "actions": actions,
        "late_days": (today - wo.due_on).days if is_open and wo.due_on < today else 0,
        "open_days": (today - wo.opened_on).days,
        "qual": qualification(wo.assigned_to, wo.asset) if wo.assigned_to_id else None,
        "can_assign": can_assign, "assign_choices": technician_choices(wo.asset) if can_assign else [], "assign_current": current,
        "assignment_policy": get_settings().policy_assignment if can_assign else "",
        "can_note": request.user.has_level(wo_perms.MODULE, wo_perms.NOTE_LEVEL),
        "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW),
        "timeline": wo_services.timeline(wo),
        "labor_hours": sum(float(line.hours) for line in wo.labor_lines.all()),
        "is_portal": wo.source == Source.PORTAL,
        "recall_match": recall_match,
    }


def _get_wo(number):
    return get_object_or_404(WorkOrder.objects.select_related("asset", "asset__device_model", "asset__department", "asset__contract", "assigned_to", "alert")
                             .prefetch_related("labor_lines", "part_lines", "assigned_to__credentials"), number=number)


def _render_wo_drawer(request, wo, status=200):
    return render(request, "web/_wo_drawer.html", _wo_drawer_context(request, _get_wo(wo.number)), status=status)


@web_view(Module.WORKORDERS, Level.VIEW)
def wo_detail(request, number):
    wo = _get_wo(number)
    if request.htmx:
        return _render_wo_drawer(request, wo)
    return render(request, "web/workorders.html", {**_workorders_context(request), **_wo_drawer_context(request, wo), "drawer_template": "web/_wo_drawer.html"})


@require_POST
@web_view(Module.WORKORDERS, Level.EDIT)
def wo_status(request, number):
    wo = _get_wo(number)
    to_status = request.POST.get("to", "")
    if not wo_perms.can_transition(request.user, wo.status, to_status):
        raise PermissionDenied
    try:
        wo_services.change_status(wo, to_status, by=request.user)
        message = f"{wo.number}: {wo.get_status_display().lower()}"
    except ValidationError as e:
        message = e.messages[0]
    response = _render_wo_drawer(request, wo)
    trigger_client_event(response, "wo-changed", {})
    return toast(response, message)


@require_POST
@web_view(wo_perms.MODULE, wo_perms.ASSIGN_LEVEL)
def wo_assign(request, number):
    wo = _get_wo(number)
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
@web_view(wo_perms.MODULE, wo_perms.NOTE_LEVEL)
def wo_note(request, number):
    wo = _get_wo(number)
    try:
        wo_services.add_note(wo, request.POST.get("text", ""), by=request.user)
        message = "Note added"
    except ValidationError as e:
        message = e.messages[0]
    return toast(_render_wo_drawer(request, wo), message)


@web_view(wo_perms.MODULE, wo_perms.CREATE_LEVEL)
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
                                       requester=d["requester"] or request.user.get_full_name(), created_by=request.user, tag_out=d["tag_out"])
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

@web_view()
def search(request):
    """Topbar search: an exact work order number or asset tag opens it; anything else searches equipment."""
    q = request.GET.get("q", "").strip()
    if q:
        wo = WorkOrder.objects.filter(number__iexact=q).first()
        if wo:
            return redirect("web:wo", number=wo.number)
        asset = Asset.objects.filter(tag__iexact=q).first()
        if asset:
            return redirect("web:asset", tag=asset.tag)
    return redirect(f"{reverse('web:equipment')}?{urlencode({'q': q})}" if q else reverse("web:equipment"))

