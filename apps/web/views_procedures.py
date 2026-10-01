"""
PM procedures in the model drawer (slice 14): the Procedure tab, choosing a model's procedure, and writing or revising one.
Views parse input, call apps.pm.procedures, and answer with the model drawer on the Procedure tab
(views_models.render_model_drawer).

Anyone with PM View sees the tab; choosing a procedure, New procedure, and Edit procedure need PM Edit (pm_perms.can_edit_program),
checked here on every request, GET and POST. Another facility's model or procedure is a 404. New and Edit are modals (#modal-card)
opened from a model's drawer, whose id they carry (?model=): a save swaps that drawer into #drawer (HX-Retarget), toasts, fires
`models-changed` (the PM library re-fetches on it), and closes the modal after settle. Choosing a procedure answers inside the drawer.
"""
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django_htmx.http import retarget, trigger_client_event

from apps.pm import permissions as pm_perms
from apps.pm import procedures as svc
from apps.pm.models import PmProcedure
from apps.pm.schedule import DEFAULT_PM_HOURS
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

from . import views_models  # views_models imports this module: use its names at call time only
from .decorators import web_view
from .forms_procedures import ProcedureForm, plain_hours
from .htmx import toast
from .views_print import checklist_steps

TAB = "procedure"
FORM = "web/_procedure_form.html"
OTHERS_SHOWN = 5  # the other models named under "Used by"; the rest are counted


def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def _uuid_or_404(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise Http404


def _get_procedure(pk) -> PmProcedure:
    """Tenant-scoped: another facility's procedure is a 404, and so is anything that is not an id."""
    return get_object_or_404(PmProcedure, pk=_uuid_or_404(pk))


def _drawer_model(request):
    """The model whose drawer opened the modal (?model=): the drawer a save answers with. Required; another facility's is a 404."""
    value = request.GET.get("model", "")
    if not value:
        return None
    return views_models.get_model(_uuid_or_404(value))


def _safe_url(url: str) -> str:
    """Only web links become links: the service accepts nothing else, but the admin and older data may hold anything."""
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else ""


def _open_pm(models) -> int:
    """Open PM work orders on devices of these models: they print the procedure as it is when printed."""
    return WorkOrder.objects.filter(asset__device_model__in=models, type=WoType.PM, status__in=OPEN_STATUSES).count()


# --- the Procedure tab -------------------------------------------------------------------------------------------------------

def procedure_tab(request, dm) -> dict:
    """The Procedure tab's context, under "proc" so nothing collides with the drawer's own keys."""
    p = dm.pm_procedure
    can_edit = pm_perms.can_edit_program(request.user)
    ctx = {"procedure": p, "can_edit": can_edit, "default_hours": DEFAULT_PM_HOURS, "steps": checklist_steps(p),
           "source_url": _safe_url(p.source_url) if p else "", "hours": plain_hours(p.estimated_hours) if p else ""}
    if p is not None:
        others = svc.models_using(p).exclude(pk=dm.pk)
        n = others.count()
        ctx.update(used_by=n + 1, others=list(others[:OTHERS_SHOWN]), others_more=max(n - OTHERS_SHOWN, 0))
    if can_edit:
        ctx["choices"] = [(c.pk, f"{c.code} · {c.name} · {plain_hours(c.estimated_hours)} h") for c in PmProcedure.objects.order_by("code")]
        ctx["open_pm"] = _open_pm([dm])
    return {"proc": ctx}


@require_POST
@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_procedure(request, pk):
    """Choose the model's procedure from the facility's library, or none (an empty value)."""
    _require(pm_perms.can_edit_program(request.user))
    dm = views_models.get_model(pk)
    if "procedure" not in request.POST:
        return HttpResponseBadRequest("Choose a procedure, or none.")
    value = request.POST["procedure"].strip()
    procedure = _get_procedure(value) if value else None
    if procedure == dm.pm_procedure:
        return toast(views_models.render_model_drawer(request, dm, tab=TAB), "No change: that is already this model's procedure")
    try:
        svc.set_model_procedure(dm, procedure, by=request.user)
    except ValidationError as e:
        return toast(views_models.render_model_drawer(request, dm, tab=TAB), e.messages[0])
    message = f"{dm} now uses {procedure.code}" if procedure else f"{dm} has no PM procedure now"
    response = trigger_client_event(views_models.render_model_drawer(request, dm, tab=TAB), "models-changed", {})
    return toast(response, message)


# --- New procedure and Edit procedure (modals) ----------------------------------------------------------------------------------

def _modal(request, form: ProcedureForm, dm, procedure=None):
    if form.is_bound:
        form.focus_first_error()
    ctx = {"form": form, "dm": dm, "procedure": procedure}
    if procedure is not None:
        users = list(svc.models_using(procedure))
        ctx.update(users=users, used_by=len(users), open_pm=_open_pm(users) if users else 0)
    return render(request, FORM, ctx)


def _saved(request, dm, message: str):
    """A modal saved: the model's drawer on the Procedure tab, the PM library refreshed, a toast, then the modal closed. After
    settle: closing it first would detach the form that sent this request, which cancels the swap and loses the other events."""
    response = retarget(views_models.render_model_drawer(request, dm, tab=TAB), "#drawer")
    trigger_client_event(response, "models-changed", {})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def procedure_new(request):
    """Write a new procedure and make it the model's (?model=) in one go: a model that cannot take it leaves no procedure behind."""
    _require(pm_perms.can_edit_program(request.user))
    dm = _drawer_model(request)
    if dm is None:
        return HttpResponseBadRequest("Open New procedure from a device model.")
    if request.method != "POST":
        initial = {"name": f"{dm.description} {dm.oem_pm_interval_months}-month PM"}
        return _modal(request, ProcedureForm(initial=initial), dm)
    form = ProcedureForm(request.POST)
    procedure = None
    if form.is_valid():
        try:
            with transaction.atomic():
                procedure = svc.create_procedure(**form.service_fields(), by=request.user)
                svc.set_model_procedure(dm, procedure, by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
            procedure = None
    if procedure is None:
        return _modal(request, form, dm)
    return _saved(request, dm, f"{procedure.code} added; {dm} uses it now")


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def procedure_edit(request, pk):
    """Revise a procedure. The change applies to every model using it; the form says how many."""
    _require(pm_perms.can_edit_program(request.user))
    procedure = _get_procedure(pk)
    dm = _drawer_model(request)
    if dm is None:
        return HttpResponseBadRequest("Open Edit procedure from a device model.")
    if request.method != "POST":
        return _modal(request, ProcedureForm(procedure=procedure), dm, procedure)
    form = ProcedureForm(request.POST, procedure=procedure)
    if form.is_valid():
        try:
            svc.update_procedure(procedure, by=request.user, **form.service_fields())
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        return _modal(request, form, dm, procedure)
    n = svc.models_using(procedure).count()
    return _saved(request, dm, f"{procedure.code} saved" + (f" for all {n} models using it" if n > 1 else ""))
