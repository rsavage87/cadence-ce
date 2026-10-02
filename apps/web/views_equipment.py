"""
Adding and changing devices from the Equipment screen and the device drawer (slice 12): Add device, Edit details, and the drawer's
status buttons. Views parse input, call apps.equipment.services, and render. Who may do what is apps/equipment/permissions.py,
checked here on every request (the buttons are hidden from roles without it too, but hiding a button is not access control).
None of these admit a scoped user (slice 16, apps.workorders.scoping): a vendor's or a unit's share is to see, not to change devices.

Every change fires `devices-changed` (the Equipment table re-fetches on it) and toasts. Add device and Edit details are modals
(#modal-card): a save swaps the device's drawer into #drawer instead (HX-Retarget; the shell opens the drawer on that swap) and
closes the modal after settle. A status button re-renders the drawer it sits in.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import AssetStatus
from apps.workorders.models import OPEN_STATUSES, WoStatus

from .decorators import web_view
from .forms_equipment import EditDeviceForm, NewDeviceForm
from .htmx import toast
from .views import asset_drawer_context, get_asset

DRAWER = "web/_asset_drawer.html"

# The toast after each status change, by (to, from); None matches any other from. The mock's two are "tagged out of service" and
# "returned to service"; the others say what happened the same way.
STATUS_TOASTS = {
    (AssetStatus.OUT_OF_SERVICE, None): "{tag} tagged out of service",
    (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN): "{tag} back from loan and in service",
    (AssetStatus.IN_SERVICE, AssetStatus.MISSING): "{tag} found and back in service",
    (AssetStatus.IN_SERVICE, AssetStatus.RETIRED): "{tag} reinstated; its next PM is due today",
    (AssetStatus.IN_SERVICE, None): "{tag} returned to service",
    (AssetStatus.ON_LOAN, None): "{tag} lent out",
    (AssetStatus.MISSING, None): "{tag} marked missing",
    (AssetStatus.RETIRED, None): "{tag} retired",
}


def status_toast(tag: str, from_status: str, to_status: str, cancelled: int = 0) -> str:
    template = STATUS_TOASTS.get((to_status, from_status)) or STATUS_TOASTS[(to_status, None)]
    message = template.format(tag=tag)
    if cancelled:
        message += f"; {cancelled} PM work order{'' if cancelled == 1 else 's'} cancelled"
    return message


def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _drawer(request, tag):
    """The device drawer, freshly read. On the tab named by ?tab= in the request's address, else Overview."""
    return render(request, DRAWER, asset_drawer_context(request, get_asset(request, tag)))


def _changed(response, *also):
    for event in ("devices-changed", *also):
        trigger_client_event(response, event, {})
    return response


def _saved(request, asset, message: str):
    """A modal saved: show the device's drawer (on Overview), refresh the table, toast, then close the modal. After settle:
    closing it first would detach the form that sent this request, which cancels the swap and loses the other events."""
    response = retarget(_drawer(request, asset.tag), "#drawer")
    toast(_changed(response), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


# --- Add device ----------------------------------------------------------------------------------------------

def _modal(request, template, form, **extra):
    if form.is_bound:
        form.focus_first_error()
    return render(request, template, {"form": form, **extra})


def _new_modal(request, form):
    return _modal(request, "web/_asset_new.html", form)


def _new_department(name: str):
    try:
        return eq.create_department(name)
    except ValidationError as e:  # its one rule is about the name typed, which is the form's new_department field
        raise ValidationError({"new_department": e.messages}) from e


def _create(request, form: NewDeviceForm):
    """The new department and model first when the form asks for them, then the device, in one transaction: a device that fails
    its checks leaves no stray model or department behind. Returns the device, or None with the errors on the form."""
    d = form.cleaned_data
    try:
        with transaction.atomic():
            department = d["department"] or _new_department(d["new_department"])
            device_model = d["device_model"] or eq.create_device_model(**form.model_fields(), by=request.user)
            return eq.create_asset(device_model=device_model, department=department, by=request.user, **form.asset_fields())
    except ValidationError as e:
        form.add_service_errors(e)
        return None


@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_new(request):
    _require(eq_perms.can_add(request.user))
    if request.method != "POST":
        return _new_modal(request, NewDeviceForm())
    form = NewDeviceForm(request.POST)
    asset = _create(request, form) if form.is_valid() else None
    if asset is None:
        return _new_modal(request, form)
    return _saved(request, asset, f"{asset.tag} added")


# --- Edit details --------------------------------------------------------------------------------------------

def _edit_modal(request, form):
    return _modal(request, "web/_asset_edit.html", form, asset=form.asset)


@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_edit(request, tag):
    _require(eq_perms.can_edit(request.user))
    asset = get_asset(request, tag)  # another facility's tag is a 404
    if request.method != "POST":
        return _edit_modal(request, EditDeviceForm(asset=asset))
    form = EditDeviceForm(request.POST, asset=asset)  # no tag, status, or contract field: posting them changes nothing
    if form.is_valid():
        try:
            eq.update_asset(asset, by=request.user, **form.changes())
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _edit_modal(request, form)
    return _saved(request, asset, f"{asset.tag} updated")


# --- status buttons ------------------------------------------------------------------------------------------

@require_POST
@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_status(request, tag):
    asset = get_asset(request, tag)
    to_status, from_status = request.POST.get("to", ""), asset.status
    _require(eq_perms.can_set_status(request.user, from_status, to_status))
    if to_status not in AssetStatus.values:
        return HttpResponseBadRequest("Unknown status.")
    # Retiring cancels the device's open PM work orders; count them for the toast.
    open_wos = set(asset.work_orders.filter(status__in=OPEN_STATUSES).values_list("pk", flat=True)) if to_status == AssetStatus.RETIRED else set()
    try:
        eq.set_status(asset, to_status, by=request.user)
    except ValidationError as e:
        return toast(_drawer(request, tag), e.messages[0])
    cancelled = asset.work_orders.filter(pk__in=open_wos, status=WoStatus.CANCELLED).count() if open_wos else 0
    # wo-changed too: retiring cancels the device's open PM work orders.
    return toast(_changed(_drawer(request, tag), "wo-changed"), status_toast(asset.tag, from_status, to_status, cancelled))
