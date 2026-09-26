"""
Contracts screen: the table with its KPI trio, the contract drawer, the new contract modal, and the
device drawer's support editor. Views parse input, call apps.contracts.services, and render.

Every change re-renders the drawer it came from, fires `contracts-changed` (the table and KPIs
re-fetch on it), and toasts.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset, AssetStatus, DeviceModel

from .decorators import web_view
from .forms import parse_uuid
from .forms_contracts import STATUS_CHOICES, ContractForm, contract_choices, edit_contract_initial, new_contract_initial, parse_contract_filters
from .htmx import PAGE_SIZE, is_partial, toast
from .views import asset_drawer_context

DRAWER = "web/_contract_drawer.html"


def _changed(response):
    return trigger_client_event(response, "contracts-changed", {})


def _get_contract(pk):
    return get_object_or_404(Contract.objects, pk=pk)


def _get_asset(tag):
    return get_object_or_404(Asset.objects.select_related("device_model", "department", "contract", "tenant"), tag=tag)


# --- table -------------------------------------------------------------------------------------------

def _contracts_context(request) -> dict:
    f = parse_contract_filters(request.GET)
    page = Paginator(ct.filter_contracts(f), PAGE_SIZE).get_page(request.GET.get("page"))
    models = ct.covered_models_by_contract(page.object_list)
    for c in page.object_list:
        c.covered = models[c.id]
    return {"nav_active": "contracts", "list_url": reverse("web:contracts"), "f": f, "page": page, "s": ct.contracts_summary(),
            "types": ContractType.choices, "statuses": STATUS_CHOICES, "can_edit": request.user.has_level(Module.CONTRACTS, Level.EDIT)}


@web_view(Module.CONTRACTS, Level.VIEW)
def contracts(request):
    if is_partial(request, "ct-kpis"):
        # The page-head summary line refreshes out of band with the tiles.
        return render(request, "web/_contracts_kpis.html", {"s": ct.contracts_summary(), "list_url": reverse("web:contracts"), "oob_summary": True})
    ctx = _contracts_context(request)
    if is_partial(request, "ct-body"):
        return render(request, "web/_contracts_body.html", ctx)
    return render(request, "web/contracts.html", ctx)


# --- drawer ------------------------------------------------------------------------------------------

def _drawer_context(request, contract, edit=False, form=None) -> dict:
    can_edit = request.user.has_level(Module.CONTRACTS, Level.EDIT)
    lst = ct.covered_devices(contract, request.GET.get("dq", "")[:100])
    n = lst["total"]
    if edit and form is None:
        form = ContractForm(initial=edit_contract_initial(contract))
    return {
        "c": contract, "st": ct.contract_status(contract), "models": ct.covered_models(contract), "n": n, "lst": lst,
        "per_device": float(contract.annual_cost) / n if n else None,
        "edit": edit and can_edit, "form": form,
        "model_options": ct.model_options(contract) if can_edit else [],
        "can_edit": can_edit, "can_delete": request.user.has_level(Module.CONTRACTS, Level.FULL),
        "can_view_asset": request.user.has_level(Module.EQUIPMENT, Level.VIEW),
    }


def _render_drawer(request, contract, **kw):
    return render(request, DRAWER, _drawer_context(request, _get_contract(contract.pk), **kw))


@web_view(Module.CONTRACTS, Level.VIEW)
def contract_detail(request, pk):
    contract = _get_contract(pk)
    if is_partial(request, "ct-list"):
        # The covered-devices filter refreshes only the list.
        return render(request, "web/_contract_list.html", _drawer_context(request, contract))
    ctx = _drawer_context(request, contract, edit=request.GET.get("edit") == "1")
    if request.htmx:
        return render(request, DRAWER, ctx)
    return render(request, "web/contracts.html", {**_contracts_context(request), **ctx, "drawer_template": DRAWER})


@web_view(Module.CONTRACTS, Level.EDIT)
def contract_devices(request, pk):
    contract = _get_contract(pk)
    q = request.GET.get("asset_q", "").strip()
    if len(q) < 2:
        return HttpResponse("")
    return render(request, "web/_contract_picks.html", {"c": contract, "assets": ct.pick_devices(contract, q)})


@require_POST
@web_view(Module.CONTRACTS, Level.EDIT)
def contract_save(request, pk):
    contract = _get_contract(pk)
    form = ContractForm(request.POST)
    if form.is_valid():
        try:
            ct.update_contract(contract, by=request.user, **form.cleaned_data)
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _render_drawer(request, contract, edit=True, form=form)
    return toast(_changed(_render_drawer(request, contract)), f"{contract.reference} updated")


@require_POST
@web_view(Module.CONTRACTS, Level.EDIT)
def contract_renew(request, pk):
    contract = _get_contract(pk)
    try:
        ct.renew_contract(contract, by=request.user)
    except ValidationError as e:
        return toast(_render_drawer(request, contract), e.messages[0])
    return toast(_changed(_render_drawer(request, contract)), f"{contract.reference} renewed through {ct.fmt_date(contract.end_on)}")


@require_POST
@web_view(Module.CONTRACTS, Level.FULL)
def contract_delete(request, pk):
    contract = _get_contract(pk)
    n = ct.delete_contract(contract)
    # The button swaps nothing; the events close the drawer, refresh the table, and toast.
    response = trigger_client_event(_changed(HttpResponse("")), "drawer-close", {})
    return toast(response, f"{contract.reference} deleted; {n} device{'' if n == 1 else 's'} set to in-house support")


@require_POST
@web_view(Module.CONTRACTS, Level.EDIT)
def contract_add(request, pk):
    contract = _get_contract(pk)
    asset = Asset.objects.filter(tag=request.POST.get("asset", "")).select_related("contract").first()
    if asset is None:
        return toast(_render_drawer(request, contract), "Choose a device from the list.")
    try:
        previous = ct.add_asset(contract, asset)
    except ValidationError as e:
        return toast(_render_drawer(request, contract), e.messages[0])
    message = f"{asset.tag} moved from {previous.reference} to {contract.reference}" if previous else f"{asset.tag} added to {contract.reference}"
    return toast(_changed(_render_drawer(request, contract)), message)


@require_POST
@web_view(Module.CONTRACTS, Level.EDIT)
def contract_add_model(request, pk):
    contract = _get_contract(pk)
    dm_id = parse_uuid(request.POST.get("device_model", ""))
    dm = DeviceModel.objects.filter(pk=dm_id).first() if dm_id else None
    if dm is None:
        return toast(_render_drawer(request, contract), "Choose a device model.")
    n = ct.add_model(contract, dm)
    return toast(_changed(_render_drawer(request, contract)), f"{n} device{'' if n == 1 else 's'} added to {contract.reference}")


@require_POST
@web_view(Module.CONTRACTS, Level.EDIT)
def contract_remove(request, pk):
    contract = _get_contract(pk)
    asset = get_object_or_404(Asset.objects, tag=request.POST.get("asset", ""))
    try:
        ct.remove_asset(contract, asset)
        message = f"{asset.tag} removed from {contract.reference}"
    except ValidationError as e:
        message = e.messages[0]
        return toast(_render_drawer(request, contract), message)
    return toast(_changed(_render_drawer(request, contract)), message)


# --- new contract modal -------------------------------------------------------------------------------

@web_view(Module.CONTRACTS, Level.EDIT)
def contract_new(request):
    params = request.POST if request.method == "POST" else request.GET
    asset = Asset.objects.exclude(status=AssetStatus.RETIRED).select_related("device_model").filter(tag=params.get("asset", "")).first()
    if request.method != "POST":
        return render(request, "web/_contract_new.html", {"form": ContractForm(initial=new_contract_initial(asset)), "asset": asset})
    form = ContractForm(request.POST)
    contract = None
    if form.is_valid():
        try:
            contract = ct.create_contract(by=request.user, **form.cleaned_data)
        except ValidationError as e:
            form.add_service_errors(e)
    if contract is None:
        return render(request, "web/_contract_new.html", {"form": form, "asset": asset})
    if asset is not None:
        ct.add_asset(contract, asset)
    response = retarget(_render_drawer(request, contract), "#drawer")
    toast(_changed(response), f"{contract.reference} created")
    # After settle: closing the modal detaches the form that sent this request, which would cancel the swap and lose the other events.
    return trigger_client_event(response, "modal-close", {}, after="settle")


# --- device drawer: support editor ---------------------------------------------------------------------

@web_view(Module.CONTRACTS, Level.EDIT)
def asset_support(request, tag):
    if not request.user.has_level(Module.EQUIPMENT, Level.VIEW):
        raise PermissionDenied
    asset = _get_asset(tag)
    if request.method != "POST":
        return render(request, "web/_contract_support.html", {"asset": asset, "choices": contract_choices(), "current": str(asset.contract_id or "")})
    choice = request.POST.get("contract", "")
    try:
        if choice:
            contract = Contract.objects.filter(pk=parse_uuid(choice)).first() if parse_uuid(choice) else None
            if contract is None:
                raise ValidationError("Choose a contract from the list.")
            previous = ct.add_asset(contract, asset)
            message = f"{asset.tag} moved from {previous.reference} to {contract.reference}" if previous else f"{asset.tag} added to {contract.reference}"
        else:
            if asset.contract_id:
                ct.remove_asset(asset.contract, asset)
            message = f"{asset.tag} set to in-house support"
    except ValidationError as e:
        message = e.messages[0]
    response = render(request, "web/_asset_drawer.html", asset_drawer_context(request, _get_asset(tag)))
    return toast(_changed(response), message)
