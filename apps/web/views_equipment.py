"""
Adding and changing devices from the Equipment screen and the device drawer (slice 12): Add device, Edit details, and the drawer's
status buttons. Views parse input, call apps.equipment.services, and render. Who may do what is apps/equipment/permissions.py,
checked here on every request (the buttons are hidden from roles without it too, but hiding a button is not access control).
None of these admit a scoped user (slice 16, apps.workorders.scoping): a vendor's or a unit's share is to see, not to change devices.

Every change fires `devices-changed` (the Equipment table re-fetches on it) and toasts. Add device and Edit details are modals
(#modal-card): a save swaps the device's drawer into #drawer instead (HX-Retarget; the shell opens the drawer on that swap) and
closes the modal after settle. A status button re-renders the drawer it sits in.

Slice 26, incoming inspections. Add device asks how the device arrives (forms_equipment.NewDeviceForm's intake). A new device is added
waiting for its incoming inspection (equipment.services.create_asset opens the inspection, unassigned), which is then given out through
the work order services' own doors, never around them: "Assign the inspection to me" through services.take (credentials and the
facility's setting checked; a refusal still adds the device and the toast says why), an inspector picked by a Work orders Approve holder
through services.assign (which notes a credential override). "New: inspect it now" assigns it to the user the same way and answers with
that inspection's Mark completed (views_wo_complete), so the pass is recorded with its checklist and readings, dated as it happens.
The toast names the inspection, and wo-changed fires too (the nav badge, My work). The drawer's "Put in use before inspection"
(Equipment Approve) is a small modal posting here to equipment.services.use_before_inspection with a reason from its list.
"""
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.credentials.services import technician_of
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import AssetStatus, UseBeforeInspection
from apps.workorders import inspections
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import OPEN_STATUSES, WoStatus, WoType

from .decorators import web_view
from .forms import VENDOR, vendor_name_for
from .forms_equipment import INSPECT_NOW, WAITING, EditDeviceForm, IntakeOffer, NewDeviceForm, UseBeforeForm
from .htmx import toast
from .views import asset_drawer_context, get_asset

DRAWER = "web/_asset_drawer.html"

# The toast after each status change, by (to, from); None matches any other from. The mock's two are "tagged out of service" and
# "returned to service"; the others say what happened the same way.
STATUS_TOASTS = {
    # Slice 26: Found and Reinstate on a device waiting for its incoming inspection (the drawer offers these two moves for no other)
    (AssetStatus.OUT_OF_SERVICE, AssetStatus.MISSING): "{tag} found; it stays out of service until its incoming inspection passes",
    (AssetStatus.OUT_OF_SERVICE, AssetStatus.RETIRED): "{tag} reinstated; it stays out of service until its incoming inspection passes",
    (AssetStatus.OUT_OF_SERVICE, None): "{tag} tagged out of service",
    (AssetStatus.IN_SERVICE, AssetStatus.ON_LOAN): "{tag} back from loan and in service",
    (AssetStatus.IN_SERVICE, AssetStatus.MISSING): "{tag} found and back in service",
    (AssetStatus.IN_SERVICE, AssetStatus.RETIRED): "{tag} reinstated; its next PM is due today",
    (AssetStatus.IN_SERVICE, None): "{tag} returned to service",
    (AssetStatus.ON_LOAN, None): "{tag} lent out",
    (AssetStatus.MISSING, None): "{tag} marked missing",
    (AssetStatus.RETIRED, None): "{tag} retired",
}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def status_toast(tag: str, from_status: str, to_status: str, cancelled: int = 0, inspections: int = 0) -> str:
    """The toast for a status change. Retiring cancels the device's open PM work orders (`cancelled`) and, for a device waiting for its
    incoming inspection (slice 26: one going back to the vendor), its open incoming inspections (`inspections`)."""
    template = STATUS_TOASTS.get((to_status, from_status)) or STATUS_TOASTS[(to_status, None)]
    message = template.format(tag=tag)
    parts = [_count(n, noun) for n, noun in ((cancelled, "PM work order"), (inspections, "incoming inspection")) if n]
    if parts:
        message += f"; {' and '.join(parts)} cancelled"
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


def _saved(request, asset, message: str, *also):
    """A modal saved: show the device's drawer (on Overview), refresh the table (and whatever `also` names), toast, then close the
    modal. After settle: closing it first would detach the form that sent this request, which cancels the swap and loses the other
    events."""
    response = retarget(_drawer(request, asset.tag), "#drawer")
    toast(_changed(response, *also), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


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


def _one_step(user) -> bool:
    """`user` may start and complete a work order in one step (completion.starts_on_completion's levels)."""
    return (wo_perms.can_transition(user, WoStatus.OPEN, WoStatus.IN_PROGRESS)
            and wo_perms.can_transition(user, WoStatus.IN_PROGRESS, WoStatus.COMPLETED))


def intake_offer(user) -> IntakeOffer:
    """What Add device offers `user` for a new device's incoming inspection, by the rules New work order uses (views.wo_new): a Work
    orders Approve holder assigns (an inspector select); anyone else whom services.may_take_as lets take work (the facility's setting,
    Work orders Edit, an active technician profile here) gets "Assign the inspection to me". "New: inspect it now" is for a user who can
    so give it to themselves (for Approve, their own technician profile) and complete it in one step."""
    can_assign = wo_perms.can_assign(user)
    taker = None if can_assign else wo_services.may_take_as(user)
    themselves = technician_of(user) if can_assign else taker
    return IntakeOffer(assign=can_assign, take=taker is not None, inspect_now=themselves is not None and _one_step(user))


def _give_out(request, form: NewDeviceForm, wo):
    """Assign the new device's incoming inspection `wo` as the form asks, through the work order services. Returns (whether it is now
    the user's own, the words for the toast after "opened for its incoming inspection"); a refusal (take's: credentials, the
    facility's setting) leaves it unassigned and says why."""
    d, user = form.cleaned_data, request.user
    mine = d["intake"] == INSPECT_NOW or (d["intake"] == WAITING and d.get("take_inspection"))
    try:
        if mine and form.offer.assign:
            technician = technician_of(user)
            if technician is None:  # deactivated since the form was drawn
                return False, ". Your account has no technician profile here, so assign it from its drawer"
            wo_services.assign(wo, technician=technician, by=user)
        elif mine:
            wo_services.take(wo, by=user)
        elif d.get("inspector") == VENDOR:
            wo_services.assign(wo, vendor_name=vendor_name_for(wo.asset), by=user)
            return False, f" and sent to vendor service ({wo.vendor_name})"
        elif d.get("inspector"):
            wo_services.assign(wo, technician=d["inspector"], by=user)
            return False, f" and assigned to {wo.assigned_to.name}"
        else:
            return False, ""
    except ValidationError as e:
        return False, f". {e.messages[0]}"
    return True, " and assigned to you"


def _inspect_now(wo, message: str):
    """"New: inspect it now": the modal turns into the inspection's Mark completed (views_wo_complete.wo_complete, which checks the
    user may complete it), fetched by htmx from HX-Location into #modal-card without a history entry. The device is added either way:
    the table and the work order lists re-fetch, and the toast says so."""
    response = HttpResponse()
    response["HX-Location"] = json.dumps({"path": reverse("web:wo_complete", args=[wo.number]), "target": "#modal-card", "swap": "innerHTML",
                                          "push": "false"})
    return toast(_changed(response, "wo-changed"), message)


@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_new(request):
    _require(eq_perms.can_add(request.user))
    kwargs = {"can_set_oem_schedule": eq_perms.can_set_oem_schedule(request.user),  # the CMS mark on a new model (Equipment Approve)
              "offer": intake_offer(request.user)}
    if request.method != "POST":
        return _new_modal(request, NewDeviceForm(**kwargs))
    form = NewDeviceForm(request.POST, **kwargs)
    asset = _create(request, form) if form.is_valid() else None
    if asset is None:
        return _new_modal(request, form)
    wo = inspections.open_inspection(asset) if asset.awaiting_inspection else None
    if wo is None:
        return _saved(request, asset, f"{asset.tag} added")
    mine, assigned = _give_out(request, form, wo)
    if mine and form.cleaned_data["intake"] == INSPECT_NOW:
        return _inspect_now(wo, f"{asset.tag} added; record its incoming inspection, {wo.number}")
    return _saved(request, asset, f"{asset.tag} added; {wo.number} opened for its incoming inspection{assigned}", "wo-changed")


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
    # Retiring cancels the device's open PM work orders (and a waiting device's open incoming inspections); count them for the toast.
    open_wos = set(asset.work_orders.filter(status__in=OPEN_STATUSES).values_list("pk", flat=True)) if to_status == AssetStatus.RETIRED else set()
    try:
        eq.set_status(asset, to_status, by=request.user)
    except ValidationError as e:
        return toast(_drawer(request, tag), e.messages[0])
    cancelled = list(asset.work_orders.filter(pk__in=open_wos, status=WoStatus.CANCELLED).values_list("type", flat=True)) if open_wos else []
    # wo-changed too: retiring cancels the device's open PM work orders, reinstating a waiting device may open an inspection.
    return toast(_changed(_drawer(request, tag), "wo-changed"),
                 status_toast(asset.tag, from_status, to_status, cancelled.count(WoType.PM), cancelled.count(WoType.INSPECTION)))


# --- in use before its incoming inspection (slice 26) ----------------------------------------------------------

def _use_before_blocker(asset) -> str:
    """Why the drawer's "Put in use before inspection" no longer applies to `asset` (it changed since the drawer was drawn), or "".
    Slice 28: a device held as evidence first (equipment.services.held_message: no incident number)."""
    if asset.incident_hold:
        return eq.held_message(asset)
    if not asset.awaiting_inspection:
        return f"{asset.tag} is not waiting for an incoming inspection."
    if asset.status not in eq.USE_BEFORE_FROM:
        return (f"{asset.tag} is {asset.get_status_display().lower()}; only a device out of service or missing goes into use before its "
                "incoming inspection.")
    return ""


def _use_before_modal(request, asset, form, reason=""):
    return render(request, "web/_asset_use_before.html", {"asset": asset, "form": form, "blocker": reason,
                                                          "inspection": inspections.open_inspection(asset)})


@web_view(Module.EQUIPMENT, Level.VIEW)
def asset_use_before(request, tag):
    """The drawer's "Put in use before inspection" (Equipment Approve): GET the modal, POST the reason. The device goes in service and
    keeps waiting; its incoming inspection becomes high priority, due the next day (equipment.services.use_before_inspection)."""
    _require(eq_perms.can_use_before_inspection(request.user))
    asset = get_asset(request, tag)
    reason = _use_before_blocker(asset)
    if request.method != "POST" or reason:
        return _use_before_modal(request, asset, None if reason else UseBeforeForm(), reason)
    form = UseBeforeForm(request.POST)
    if form.is_valid():
        try:
            asset = eq.use_before_inspection(asset, form.cleaned_data["reason"], by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _use_before_modal(request, asset, form)
    wo = inspections.open_inspection(asset)
    label = UseBeforeInspection(form.cleaned_data["reason"]).label
    message = f"{asset.tag} in use before its incoming inspection ({label})"
    if wo is not None:
        message += f"; {wo.number} is high priority, due {_day(wo.due_on)}"
    return _saved(request, asset, message, "wo-changed")
