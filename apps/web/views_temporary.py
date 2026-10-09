"""
Temporary equipment on the Equipment screen and the device drawer (slice 29): rentals, vendor loaners, and demo or evaluation units,
on site for a while and maintained by their owner. Views parse input, call apps.equipment.services (add_temporary_device,
update_temporary, return_to_owner, keep_temporary_device), and render. Who may: Equipment Edit adds one, changes its stay, and returns it
(permissions.TEMPORARY_LEVEL); Equipment Approve keeps it (KEEP_LEVEL); checked here on every request (web_view's level), the buttons
hidden from roles without it too. None admits a scoped user (web_view closed by default).

- Add rental or loaner (temporary_new): its own modal (forms_temporary.TemporaryDeviceForm: Add device's tag, model, and department
  fields, whose it is, the owner, the agreement, PO, or RMA number, arrived, due back, the owner's PM date, the device of ours a vendor
  loaner stands in for, and how it arrives). A new unit waits for its incoming inspection, given out as Add device gives it out
  (views_equipment._give_out: an inspector for Work orders Approve, "Assign the inspection to me" for whoever may take work), and "inspect
  it now" answers with that inspection's Mark completed (views_equipment._inspect_now). ?for=<tag> (a device of ours' "Vendor loaner
  arrived") fills it in as a vendor loaner standing in for that device: its model, department, and room, and the vendor of its open
  repair as the owner; ?kind= picks whose it is (Add device's Whose choices). A unit with the model and serial of one returned earlier is
  warned about, never refused (temporary_match, as the serial or model changes; again in the toast): each stay is its own record.
- Change details (temporary_change), Return to owner (temporary_return: the work that blocks the return is named up front, and the
  modal asks how it was cleaned and what was done about patient data), Keep it (temporary_keep: the price, its first PM as ours, the
  warranty). Each answers with the device's drawer (views_equipment._saved), devices-changed, and a toast. "Return the loaner" on a device
  of ours opens the loaner's Return to owner with ?back=<its tag>, and the save shows that device's drawer again.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render

from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership
from apps.workorders import inspections, scoping
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoStatus, WoType

from .decorators import web_view
from .forms import parse_uuid
from .forms_equipment import INSPECT_NOW
from .forms_temporary import KeepForm, ReturnForm, StayForm, TemporaryDeviceForm, device_by_tag
from .views import get_asset
from .views_equipment import _give_out, _inspect_now, _new_department, _saved, intake_offer

NEW = "web/_temporary_new.html"
MATCH = "web/_temporary_match.html"
CANCELLABLE = (WoType.PM, WoType.INSPECTION)  # what a return cancels when open (equipment.services._retire), the rest blocks it
CANCELLABLE_STATUSES = (WoStatus.OPEN, WoStatus.AWAITING_PARTS)


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


# --- the serial warning ------------------------------------------------------------------------------------------------------

def here_before(device_model, serial: str) -> str:
    """The warning when this model and serial match a temporary device returned earlier (equipment.services.matching_returned):
    "This unit was here before as T-0012, returned Mar 3, 2026" (and how many earlier stays), or "". Never a refusal: each stay is
    its own record."""
    if device_model is None or not (serial or "").strip():
        return ""
    found = list(eq.matching_returned(device_model, serial)[:2])
    if not found:
        return ""
    first = found[0]
    words = f"This unit was here before as {first.tag}" + (f", returned {_day(first.returned_on)}" if first.returned_on else "")
    if len(found) > 1:
        earlier = eq.matching_returned(device_model, serial).count() - 1
        words += f" (and {_plural(earlier, 'earlier stay')})"
    return words


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


@web_view(eq_perms.MODULE, eq_perms.TEMPORARY_LEVEL)
def temporary_match(request):
    """The add modal's warning, re-fetched as its serial or model changes."""
    pk = parse_uuid(request.GET.get("device_model", ""))
    device_model = DeviceModel.objects.filter(pk=pk).first() if pk else None
    return render(request, MATCH, {"here_before": here_before(device_model, request.GET.get("serial", "")[:80])})


# --- Add rental or loaner -----------------------------------------------------------------------------------------------------

def _prefill(params) -> tuple[dict, object]:
    """The add modal's initial values from its address: ?kind= whose it is, and ?for=<tag> a device of ours out for repair, which a
    vendor loaner stands in for (its model, department, and room; the vendor its open repair is with as the owner). Returns (initial,
    that device or None). A tag that is not a device of ours in use fills nothing."""
    initial = {}
    if params.get("kind") in Ownership.values and params.get("kind") != Ownership.OWNED:
        initial["kind"] = params["kind"]
    device = device_by_tag(params.get("for", ""))
    if device is None or eq.owner_maintains(device) or device.status == AssetStatus.RETIRED:
        return initial, None
    vendor = (WorkOrder.objects.filter(asset=device, type=WoType.REPAIR, status__in=OPEN_STATUSES, vendor_service=True).exclude(vendor_name="")
              .order_by("opened_on", "number").values_list("vendor_name", flat=True).first())
    initial.update({"kind": Ownership.LOANER, "device_model": str(device.device_model_id), "department": str(device.department_id),
                    "room": device.room, "stands_in_for": device.tag, "owner": vendor or ""})
    return initial, device


def _new_modal(request, form, stands_for=None):
    if form.is_bound:
        form.focus_first_error()
    d = form.cleaned_data if form.is_bound and hasattr(form, "cleaned_data") else {}
    words = here_before(d.get("device_model"), d.get("serial", "")) if d.get("device_model") else ""
    return render(request, NEW, {"form": form, "stands_for": stands_for, "here_before": words})


def _create(request, form: TemporaryDeviceForm):
    """The new department and model first when the form asks for them, then the device, in one transaction: a device that fails its
    checks leaves no stray model or department behind. Returns the device, or None with the errors on the form."""
    d = form.cleaned_data
    try:
        with transaction.atomic():
            department = d["department"] or _new_department(d["new_department"])
            device_model = d["device_model"] or eq.create_device_model(**form.model_fields(), by=request.user)
            return eq.add_temporary_device(device_model=device_model, department=department, by=request.user, **form.asset_fields())
    except ValidationError as e:
        form.add_service_errors(e)
        return None


@web_view(eq_perms.MODULE, eq_perms.TEMPORARY_LEVEL)
def temporary_new(request):
    kwargs = {"can_set_oem_schedule": eq_perms.can_set_oem_schedule(request.user), "offer": intake_offer(request.user)}
    if request.method != "POST":
        initial, device = _prefill(request.GET)
        return _new_modal(request, TemporaryDeviceForm(initial=initial, **kwargs), stands_for=device)
    form = TemporaryDeviceForm(request.POST, **kwargs)
    asset = _create(request, form) if form.is_valid() else None
    if asset is None:
        return _new_modal(request, form)
    before = here_before(asset.device_model, asset.serial)
    seen = f"; {_lower_first(before)}" if before else ""
    wo = inspections.open_inspection(asset) if asset.awaiting_inspection else None
    if wo is None:
        return _saved(request, asset, f"{asset.tag} added{seen}")
    mine, assigned = _give_out(request, form, wo)
    if mine and form.cleaned_data["intake"] == INSPECT_NOW:
        return _inspect_now(wo, f"{asset.tag} added; record its incoming inspection, {wo.number}{seen}")
    return _saved(request, asset, f"{asset.tag} added; {wo.number} opened for its incoming inspection{assigned}{seen}", "wo-changed")


# --- a stay's actions -----------------------------------------------------------------------------------------------------------

def stay_refusal(asset) -> str:
    """Why a stay's actions no longer apply to `asset` (it changed since the drawer was drawn), or "": it is ours (kept, or never
    temporary), or it went back to its owner. The services refuse both too."""
    if not eq.owner_maintains(asset):
        if asset.kept_on:
            return f"{asset.tag} was kept by the facility on {_day(asset.kept_on)}: it is ours now."
        return f"{asset.tag} is ours, not a rental, vendor loaner, or demo unit."
    if asset.status == AssetStatus.RETIRED:
        return f"{asset.tag} was returned to its owner{f' on {_day(asset.returned_on)}' if asset.returned_on else ''}."
    return ""


def _modal(request, template, asset, form, blocker="", **extra):
    if form is not None and form.is_bound:
        first = next((name for name in form.fields if name in form.errors), None)
        if first:
            for field in form.fields.values():
                field.widget.attrs.pop("autofocus", None)
            form.fields[first].widget.attrs["autofocus"] = True
    return render(request, template, {"asset": asset, "form": form, "blocker": blocker, **extra})


@web_view(eq_perms.MODULE, eq_perms.TEMPORARY_LEVEL)
def temporary_change(request, tag):
    """Change details of a stay: the owner, the reference, due back, the owner's PM date, and a loaner's device of ours."""
    template = "web/_temporary_change.html"
    asset = get_asset(request, tag)
    blocker = stay_refusal(asset)
    if request.method != "POST" or blocker:
        return _modal(request, template, asset, None if blocker else StayForm(asset=asset), blocker)
    form = StayForm(request.POST, asset=asset)
    if form.is_valid():
        try:
            eq.update_temporary(asset, by=request.user, **form.changes())
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _modal(request, template, asset, form)
    return _saved(request, asset, f"{asset.tag}: stay details saved")


def return_blockers(asset) -> dict:
    """What Return to owner says up front, from the rules equipment.services.return_to_owner keeps: `refusal` (ours, returned already,
    held for an incident, missing), `blocking` (open work a return does not cancel: complete or cancel it first, as retiring asks), and
    `cancels` (its open incoming inspection, or a PM, open or waiting on parts, which the return cancels)."""
    refusal = stay_refusal(asset)
    if not refusal and asset.incident_hold:
        refusal = eq.held_message(asset)
    elif not refusal and asset.status == AssetStatus.MISSING:
        refusal = f"{asset.tag} is missing: mark it found before it goes back to its owner."
    open_wos = list(asset.work_orders.filter(status__in=OPEN_STATUSES).order_by("number")) if not refusal else []
    blocking = [w for w in open_wos if not (w.type in CANCELLABLE and w.status in CANCELLABLE_STATUSES)]
    return {"refusal": refusal, "blocking": blocking, "cancels": [w for w in open_wos if w not in blocking]}


def _back_to(request, asset):
    """The drawer a return answers with: the device of ours "Return the loaner" was opened from (?back=, then the form's hidden field),
    when this user may open it, else the loaner's own."""
    back = (request.POST.get("back") or request.GET.get("back") or "").strip()
    if back and back != asset.tag:
        found = scoping.assets(request.user, Asset.objects.filter(tag=back)).first()
        if found is not None:
            return found
    return asset


@web_view(eq_perms.MODULE, eq_perms.TEMPORARY_LEVEL)
def temporary_return(request, tag):
    """Return to owner: the day it went back, how it was cleaned, and what was done about patient data. The open work that blocks it is
    named before anything is asked; its open incoming inspection is cancelled with it (the toast counts it)."""
    template = "web/_temporary_return.html"
    asset = get_asset(request, tag)
    found = return_blockers(asset)
    back = _back_to(request, asset)
    extra = {**found, "back": back.tag if back.pk != asset.pk else ""}
    if found["refusal"] or found["blocking"]:
        return _modal(request, template, asset, None, found["refusal"], **extra)
    if request.method != "POST":
        return _modal(request, template, asset, ReturnForm(asset=asset), **extra)
    form = ReturnForm(request.POST, asset=asset)
    cancels = {w.pk for w in found["cancels"]}
    if form.is_valid():
        d = form.cleaned_data
        try:
            eq.return_to_owner(asset, cleaning=d["cleaning"], data=d["data"], on=d["on"], by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _modal(request, template, asset, form, **extra)
    cancelled = asset.work_orders.filter(pk__in=cancels, status=WoStatus.CANCELLED).count() if cancels else 0
    message = f"{asset.tag} returned to {asset.owner or 'its owner'}"
    if cancelled:
        message += f"; {_plural(cancelled, 'open work order')} cancelled"
    return _saved(request, back, message, "wo-changed")


def keep_refusal(asset) -> str:
    """Why Keep it does not apply now (equipment.services.keep_temporary_device's refusals, up front), or ""."""
    refusal = stay_refusal(asset)
    if refusal:
        return refusal
    if asset.incident_hold:
        return eq.held_message(asset)
    if asset.status == AssetStatus.MISSING:
        return f"{asset.tag} is missing: mark it found before keeping it."
    if asset.awaiting_inspection:
        wo = inspections.open_inspection(asset)
        return f"{asset.tag} has not passed its incoming inspection{f' ({wo.number})' if wo else ''}: keep it once it passes."
    return ""


@web_view(eq_perms.MODULE, eq_perms.KEEP_LEVEL)
def temporary_keep(request, tag):
    """Keep it (Equipment Approve): the facility buys it, so it becomes ours, on the PM program, with what was paid, its first PM as
    ours, and its warranty."""
    template = "web/_temporary_keep.html"
    asset = get_asset(request, tag)
    blocker = keep_refusal(asset)
    if request.method != "POST" or blocker:
        return _modal(request, template, asset, None if blocker else KeepForm(), blocker)
    form = KeepForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            eq.keep_temporary_device(asset, acquisition_cost=d["acquisition_cost"], next_pm_on=d["next_pm_on"], warranty_end=d["warranty_end"],
                                     by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _modal(request, template, asset, form)
    return _saved(request, asset, f"{asset.tag} kept by the facility: ours from today, first PM due {_day(asset.next_pm_on)}")
