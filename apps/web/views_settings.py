"""
Settings screen (slice 8): integrations, the service request portal, maintenance policy, KPI targets (and since slice 15
the labor rates, in the same panel), and risk scoring, from apps.facility.services. Anyone with Settings View sees the page;
changes need Settings Edit (apps/facility/permissions.py), checked on every POST.

Each editable panel (#set-portal, #set-policy, #set-targets, #set-zone) is its own partial: its POST answers with that panel
re-rendered and a toast. A rejected policy or targets save keeps what the user typed and marks the bad field; a
rejected portal change shows the saved values again (it saves as the user types, so there is nothing to keep).

Time zone (slice 21, #set-zone): the facility's time zone (fs.set_time_zone), which decides its today, the times shown, and when
its daily jobs and emails run. Its form posts to /settings/time-zone/ (settings_time_zone, Settings Edit).

Taking work (slice 24, #set-take): whether technicians may take unassigned work they are credentialed for (My work's "You could
take", the new work order form's "Assign it to me"; apps.workorders.services.take). One switch that saves as it changes, posted to
/settings/take-work/ (settings_take, Settings Edit).

Import data (slice 23, #set-import): a link to /settings/import/ (views_imports) for anyone who may import a kind of file
(apps.imports.permissions.importable: each kind its own module's level), with the files of those kinds not finished yet (those
left for too long are ended first: apps.imports.services.expire_stale).
"""
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import Level, Module
from apps.equipment.models import Department
from apps.facility import permissions as fac_perms
from apps.facility import services as fs
from apps.imports import permissions as imp_perms
from apps.imports import services as imp_services
from apps.imports.models import ImportRun
from apps.tenants.context import zone_of

from .decorators import web_view
from .forms import parse_uuid
from .forms_settings import (
    RATE_FORM,
    TARGET_FORM,
    error_dict,
    plain_number,
    policy_fields,
    portal_fields,
    rate_fields,
    take_work_field,
    target_fields,
    time_zone_field,
)
from .htmx import is_partial, toast

CHIPS = {fs.CONNECTED: ("ok", "Connected"), fs.LICENSE: ("warn", "License needed"), fs.NOT_CONNECTED: ("neutral", "Not connected")}
DEPT_LINK_BOX = "share-dept-box"


# --- panel contexts ---------------------------------------------------------------------------------

def _portal_ctx(request, s, typed_domains: str | None = None, domains_error: str = "") -> dict:
    """The portal rows. After a refused domain list, `typed_domains` keeps what the user typed so a typo is fixed, not retyped."""
    return {"s": s, "portal_url": fs.portal_url(request.tenant), "hotline_max": fs.HOTLINE_MAX_LENGTH, "can_edit": fac_perms.can_edit(request.user),
            "confirmation_choices": fs.CONFIRMATION_CHOICES, "domains_max": fs.EMAIL_DOMAINS_MAX_LENGTH, "domains_error": domains_error,
            "domains_value": s.portal_email_domains if typed_domains is None else typed_domains}


def _policy_ctx(request, s, typed: dict | None = None, errors: dict | None = None) -> dict:
    """The eight policy inputs. After a rejected save, `typed` holds what the user sent so nothing they wrote is lost."""
    items = fs.policy_items(s)
    for item in items:
        if typed is not None:
            item["text"] = typed.get(item["field"], item["text"])
        item["error"] = (errors or {}).get(item["field"], "")
    return {"policy": items, "can_edit": fac_perms.can_edit(request.user)}


def _targets_ctx(request, s, typed: dict | None = None, errors: dict | None = None) -> dict:
    """The KPI targets and, in the same form, the labor rates (slice 15). After a rejected save, `typed` holds what was sent."""
    def rows(form):
        return [{"field": field, "label": label, "help": help_text, "error": (errors or {}).get(field, ""),
                 "value": typed.get(field, "") if typed is not None else plain_number(getattr(s, field))}
                for field, label, help_text in form]

    return {"targets": rows(TARGET_FORM), "rates": rows(RATE_FORM), "can_edit": fac_perms.can_edit(request.user)}


def _take_ctx(request, s) -> dict:
    """The Taking work panel (slice 24): the switch, on or off as saved."""
    return {"take_on": s.technicians_take_work, "can_edit": fac_perms.can_edit(request.user)}


def _integrations() -> dict:
    items = fs.integrations()
    for item in items:
        item["chip_css"], item["chip_label"] = CHIPS[item["status"]]
    return {"integrations": items, "connected": sum(1 for i in items if i["status"] == fs.CONNECTED)}


def _zone_ctx(request, error: str = "") -> dict:
    """The time zone panel: the facility's zone, the list to choose from (the US zones first, each with its offset now), and its
    clock now. Read from request.tenant, which set_time_zone updates in place, so a change shows at once (the request itself still
    runs in the zone it started in). The clock is formatted here: a template would show an aware time in the active zone."""
    tenant = request.tenant
    now = timezone.now()
    local = timezone.localtime(now, zone_of(tenant))
    zone = tenant.zone_name  # its own, or the server's while it has chosen none
    return {"zone": zone, "zone_label": fs.zone_label(zone), "zone_choices": fs.time_zone_choices(zone, now), "zone_chosen": bool(tenant.timezone),
            "zone_known": zone in fs.zone_names(), "zone_now": f"{local:%-I:%M %p} on {local:%A, %B} {local.day}",
            "zone_error": error, "can_edit": fac_perms.can_edit(request.user)}


def _imports_ctx(user) -> dict:
    """The Import data panel (slice 23): the kinds `user` may import, in words, and their runs not yet finished. Empty (no panel)
    for someone who may import none. Runs left unfinished too long are ended first (services.expire_stale, as the Import data page
    does), so one abandoned long ago never reads as waiting."""
    labels = [imp.label.lower() for imp in imp_perms.importable(user)]
    if not labels:
        return {"import_kinds": ""}
    words = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])}{',' if len(labels) > 2 else ''} and {labels[-1]}"
    imp_services.expire_stale()
    unfinished = imp_services.runs_for(user).exclude(status__in=[ImportRun.Status.IMPORTED, ImportRun.Status.DISCARDED, ImportRun.Status.EXPIRED])
    return {"import_kinds": words, "import_open": unfinished.count()}


def _first(errors: dict) -> str:
    return next(iter(errors.values()))


# --- the page -------------------------------------------------------------------------------------------

@web_view(fac_perms.MODULE, fac_perms.VIEW_LEVEL)
def settings_page(request):
    s = fs.get_settings()
    user = request.user
    ctx = {"nav_active": "settings", **_integrations(), **_portal_ctx(request, s), **_policy_ctx(request, s), **_targets_ctx(request, s),
           **_zone_ctx(request), **_imports_ctx(user), **_take_ctx(request, s),
           "risk_rubric": fs.RISK_RUBRIC, "risk_rows": fs.risk_summary(), "risk_scoring": fs.risk_scoring_summary(),
           "can_view_contracts": user.has_level(Module.CONTRACTS, Level.VIEW), "can_view_users": user.has_level(Module.USERS, Level.VIEW),
           "can_view_recalls": user.has_level(Module.RECALLS, Level.VIEW), "can_view_equipment": user.has_level(Module.EQUIPMENT, Level.VIEW)}
    return render(request, "web/settings.html", ctx)


# --- service request portal -----------------------------------------------------------------------------

def _portal_response(request, message: str, **typed):
    """The auto-saving form only (it swaps itself), so the link box and its buttons are never replaced under the user's pointer."""
    return toast(render(request, "web/_settings_portal_form.html", _portal_ctx(request, fs.get_settings(), **typed)), message)


@require_POST
@web_view(fac_perms.MODULE, fac_perms.EDIT_LEVEL)
def settings_portal(request):
    fields = portal_fields(request.POST)
    try:
        fs.update_settings(by=request.user, **fields)
    except ValidationError as e:
        errors = error_dict(e)
        if "portal_email_domains" in errors:  # a refused domain list stays as typed, marked, so the typo can be fixed in place
            return _portal_response(request, errors["portal_email_domains"], typed_domains=fields["portal_email_domains"],
                                    domains_error=errors["portal_email_domains"])
        return _portal_response(request, _first(errors))  # nothing was saved; the panel shows the saved values
    return _portal_response(request, "Portal setting saved")


@web_view(fac_perms.MODULE, fac_perms.VIEW_LEVEL)
def settings_dept_links(request):
    """The mock's share modal: the general link and a per-department link. Only ever a modal, so a direct visit goes to Settings."""
    if not request.htmx:
        return redirect("web:settings")
    departments = list(Department.objects.all())
    wanted = parse_uuid(request.GET.get("dept"))
    # An unknown id, or another tenant's (invisible through the scoped manager), falls back to the first department.
    dept = next((d for d in departments if d.pk == wanted), departments[0] if departments else None)
    ctx = {"departments": departments, "dept": dept, "dept_url": fs.portal_url(request.tenant, dept.name) if dept else "",
           "portal_url": fs.portal_url(request.tenant), "box_id": DEPT_LINK_BOX}
    if is_partial(request, DEPT_LINK_BOX):
        return render(request, "web/_settings_dept_link.html", ctx)
    return render(request, "web/_settings_dept_links.html", ctx)


# --- maintenance policy ---------------------------------------------------------------------------------

@require_POST
@web_view(fac_perms.MODULE, fac_perms.EDIT_LEVEL)
def settings_policy(request):
    typed = policy_fields(request.POST)
    try:
        s = fs.update_settings(by=request.user, **typed)
    except ValidationError as e:
        errors = error_dict(e)
        return toast(render(request, "web/_settings_policy.html", _policy_ctx(request, fs.get_settings(), typed, errors)), _first(errors))
    return toast(render(request, "web/_settings_policy.html", _policy_ctx(request, s)), "Maintenance policy saved")


@require_POST
@web_view(fac_perms.MODULE, fac_perms.EDIT_LEVEL)
def settings_policy_reset(request):
    s = fs.reset_policy(by=request.user)
    return toast(render(request, "web/_settings_policy.html", _policy_ctx(request, s)), "Policy reset to defaults")


# --- KPI targets ----------------------------------------------------------------------------------------

@require_POST
@web_view(fac_perms.MODULE, fac_perms.EDIT_LEVEL)
def settings_targets(request):
    rates = rate_fields(request.POST)  # the panel's labor rates (slice 15); a post without them leaves them as they are
    try:
        s = fs.update_settings(by=request.user, **target_fields(request.POST), **rates)
    except ValidationError as e:
        errors = error_dict(e)
        s = fs.get_settings()
        # as typed, before "$" and "," were dropped; a rate the post left out shows as saved (it was not part of this change)
        typed = {**{field: request.POST.get(field, "") for field, _label, _help in TARGET_FORM},
                 **{field: request.POST.get(field, plain_number(getattr(s, field))) for field, _label, _help in RATE_FORM}}
        return toast(render(request, "web/_settings_targets.html", _targets_ctx(request, s, typed, errors)), _first(errors))
    return toast(render(request, "web/_settings_targets.html", _targets_ctx(request, s)), "Targets and labor rates saved" if rates else "Targets saved")


# --- taking work (slice 24) -----------------------------------------------------------------------------

@require_POST
@web_view(fac_perms.MODULE, fac_perms.EDIT_LEVEL)
def settings_take(request):
    """Turn taking work on or off (FacilitySettings.technicians_take_work, through update_settings, which records who). Answers with
    the panel as saved: a refused post (neither on nor off) changes nothing."""
    try:
        s = fs.update_settings(by=request.user, **take_work_field(request.POST))
    except ValidationError as e:
        return toast(render(request, "web/_settings_take.html", _take_ctx(request, fs.get_settings())), _first(error_dict(e)))
    message = ("Technicians may take unassigned work they are credentialed for" if s.technicians_take_work
               else "Technicians no longer take unassigned work; a CE manager assigns it")
    return toast(render(request, "web/_settings_take.html", _take_ctx(request, s)), message)


# --- time zone (slice 21) -------------------------------------------------------------------------------

@require_POST
@web_view(fac_perms.MODULE, fac_perms.EDIT_LEVEL)
def settings_time_zone(request):
    """Set the facility's time zone (fs.set_time_zone, which refuses anything but a zone this server knows). Settings Edit."""
    before = request.tenant.zone_name
    try:
        fs.set_time_zone(request.tenant, time_zone_field(request.POST), by=request.user)
    except ValidationError as e:
        message = _first(error_dict(e))
        return toast(render(request, "web/_settings_zone.html", _zone_ctx(request, error=message)), message)
    if not request.htmx:
        return redirect("web:settings")
    zone = request.tenant.zone_name
    message = f"Time zone set to {fs.zone_label(zone)}" if zone != before else f"The time zone is already {fs.zone_label(zone)}"
    return toast(render(request, "web/_settings_zone.html", _zone_ctx(request)), message)
