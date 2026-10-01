"""
The device model drawer (slice 14): a model's PM program, opened from the PM library and the device drawer. Three tabs: PM program
(this module: details, risk score, intervals, devices), Procedure (views_procedures.procedure_tab), and AEM (views_aem.aem_tab).
Add model and Edit details (Equipment Edit) and the risk score (Equipment Approve) are here too: a model's details and its risk
are equipment data (apps.equipment.permissions), checked here on every request, GET and POST, behind the PM View the screen needs.

Every change fires `models-changed` (the PM library re-fetches on it) and toasts. Actions inside a tab answer with the whole drawer
on that tab (render_model_drawer), which #drawer swaps in. The three forms are modals (#modal-card): a save swaps the model's
drawer into #drawer instead (HX-Retarget; the shell opens the drawer on that swap) and closes the modal after settle.
"""
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass
from apps.facility.services import RISK_BANDS, RISK_RUBRIC
from apps.pm import aem
from apps.pm import permissions as pm_perms

from . import views_aem, views_procedures
from .decorators import web_view
from .forms_models import RISK_MEANINGS, DeviceModelForm, RiskScoreForm
from .htmx import is_partial, toast

DRAWER = "web/_model_drawer.html"
TABS = (("program", "PM program"), ("procedure", "Procedure"), ("aem", "AEM"))
DEVICES_SHOWN = 8  # the PM program tab lists this many of the model's active devices, soonest PM first, and counts the rest


def get_model(pk) -> DeviceModel:
    """Tenant-scoped: another facility's model is a 404."""
    return get_object_or_404(DeviceModel.objects.select_related("pm_procedure"), pk=pk)


def _months(n: int) -> str:
    return f"{n} month{'' if n == 1 else 's'}"


def risk_parts(dm) -> list[dict]:
    """The four parts of a scored model's risk score, each with what its value means; [] while unscored."""
    if dm.risk_score is None:
        return []
    return [{"label": label, "value": getattr(dm, field), "high": high, "meaning": RISK_MEANINGS[key].get(getattr(dm, field), "")}
            for key, field, label, _low, high in eq.RISK_PARTS]


def program_tab(request, dm, today: date | None = None) -> dict:
    """The PM program tab's context, under one key (`program`): the model's risk, intervals, and devices, and what this user may
    do. One key, because a model drawer opened directly renders over the PM schedule, whose context it would otherwise shadow."""
    today = today or date.today()
    user = request.user
    active = (Asset.objects.filter(device_model=dm, status__in=Asset.ACTIVE_STATUSES).select_related("department")
              .order_by(F("next_pm_on").asc(nulls_last=True), "tag"))
    shown = list(active[:DEVICES_SHOWN])
    active_count = len(shown) if len(shown) < DEVICES_SHOWN else active.count()
    oem, months = dm.oem_pm_interval_months, dm.pm_interval_months
    aem_on_file = bool(dm.aem_interval_months and dm.aem_interval_months != oem)
    return {"program": {
        "risk": {"score": dm.risk_score, "parts": risk_parts(dm), "reviewed_on": dm.risk_reviewed_on, "due": eq.risk_review_due(dm, today),
                 "due_on": eq.risk_review_due_on(dm)},
        "interval": {"oem": _months(oem), "in_force": _months(months), "aem": months != oem,
                     # in force without an approved decision (set before slice 14): never called approved (apps.pm.aem.is_legacy)
                     "unapproved": months != oem and aem.in_force(dm) is None,
                     # an AEM interval on file that life support ignores (DeviceModel.pm_interval_months)
                     "aem_ignored": aem_on_file and months == oem and dm.risk_class == RiskClass.LIFE_SUPPORT},
        "devices": {"shown": shown, "active": active_count, "more": max(active_count - len(shown), 0),
                    "retired": Asset.objects.filter(device_model=dm, status=AssetStatus.RETIRED).count()},
        "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW),
        "can_edit_model": eq_perms.can_edit_model(user), "can_set_risk": eq_perms.can_set_risk(user),
    }}


def model_drawer_context(request, dm, tab: str | None = None) -> dict:
    """The drawer on `tab` (else ?tab= in the request, else PM program), with that tab's context."""
    keys = [k for k, _label in TABS]
    tab = tab if tab in keys else request.GET.get("tab") if request.GET.get("tab") in keys else "program"
    ctx = {"dm": dm, "tab": tab, "tabs": TABS, "nav_active": "pm"}
    if tab == "program":
        ctx.update(program_tab(request, dm))
    elif tab == "procedure":
        ctx.update(views_procedures.procedure_tab(request, dm))
    else:
        ctx.update(views_aem.aem_tab(request, dm))
    return ctx


def render_model_drawer(request, dm, tab: str | None = None, status: int = 200) -> HttpResponse:
    """The whole drawer, freshly read, on `tab`: what every action inside the drawer answers with."""
    return render(request, DRAWER, model_drawer_context(request, get_model(dm.pk), tab), status=status)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_detail(request, pk):
    dm = get_model(pk)
    if request.htmx:
        return render(request, DRAWER, model_drawer_context(request, dm))
    from .views_pm import pm_page_context

    return render(request, "web/pm.html", {**pm_page_context(request), **model_drawer_context(request, dm), "drawer_template": DRAWER})


# --- the modals -------------------------------------------------------------------------------------------------------------

def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def aem_note(effect: dict | None) -> str:
    """What the AEM program did about a change (equipment.services sets dm.aem_effect): a model scored into life support leaves AEM."""
    if not effect:
        return ""
    parts = []
    if effect.get("ended"):
        parts.append("its AEM interval ended (life support follows the OEM interval)")
    if effect.get("withdrawn"):
        parts.append("its open AEM proposal was withdrawn")
    if effect.get("cleared"):
        parts.append("the AEM interval on file without a recorded approval was cleared")
    moved = effect.get("moved") or 0
    if moved:
        parts.append(f"{'1 device' if moved == 1 else f'{moved} devices'}' next PM moved earlier")
    return "; " + "; ".join(parts) if parts else ""


def _saved(request, dm, message: str):
    """A modal saved: show the model's drawer on PM program, refresh the PM library, toast, then close the modal. After settle:
    closing it first would detach the form that sent this request, which cancels the swap and loses the other events. When the
    change moved devices' next PMs (the AEM program, aem_note), the screens listing devices and work orders refresh too."""
    effect = getattr(dm, "aem_effect", None)
    response = retarget(render_model_drawer(request, dm, tab="program"), "#drawer")
    events = ["models-changed"] + (["devices-changed", "wo-changed"] if effect and effect.get("moved") else [])
    for event in events:
        trigger_client_event(response, event, {})
    toast(response, message + aem_note(effect))
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _form_modal(request, form, dm=None):
    if form.is_bound:
        form.focus_first_error()
    return render(request, "web/_model_form.html", {"form": form, "dm": dm, "score": dm.risk_score if dm else None})


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_new(request):
    """Add model: a new catalog entry through create_device_model. Equipment Edit."""
    _require(eq_perms.can_edit_model(request.user))
    if request.method != "POST":
        return _form_modal(request, DeviceModelForm())
    form = DeviceModelForm(request.POST)
    dm = None
    if form.is_valid():
        try:
            dm = eq.create_device_model(**form.service_fields(), by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if dm is None:
        return _form_modal(request, form)
    return _saved(request, dm, f"{dm} added to the catalog")


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_edit(request, pk):
    """Edit details: everything but the risk class (the risk score's job) through update_device_model. Equipment Edit."""
    _require(eq_perms.can_edit_model(request.user))
    dm = get_model(pk)
    if request.method != "POST":
        return _form_modal(request, DeviceModelForm(device_model=dm), dm)
    form = DeviceModelForm(request.POST, device_model=dm)  # no risk class or AEM field: posting them changes nothing
    if form.is_valid():
        try:
            eq.update_device_model(dm, by=request.user, **form.service_fields())
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        dm.refresh_from_db()  # the heading shows the stored name, not the one refused
        return _form_modal(request, form, dm)
    return _saved(request, dm, f"{dm} updated")


def _risk_modal(request, dm, form):
    if form.is_bound:
        form.focus_first_error()
    return render(request, "web/_model_risk.html", {"dm": dm, "form": form, "preview": form.preview(), "rubric": RISK_RUBRIC,
                                                    "bands": [(band, rc.label) for band, rc in RISK_BANDS],
                                                    "due": eq.risk_review_due(dm), "due_on": eq.risk_review_due_on(dm)})


def risk_toast(dm, before: dict) -> str:
    """What scoring did: a new class, a new score in the same class, or the yearly review of the same score."""
    score, label = dm.risk_score, dm.get_risk_class_display()
    if dm.risk_class != before["risk_class"]:
        return f"{dm} scored {score}: risk class now {label}"
    if score == before["score"]:
        return f"{dm} risk review recorded: {score} · {label}"
    return f"{dm} scored {score} · {label}"


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_risk(request, pk):
    """The risk score: the rubric's four parts through set_risk_score, or clear_risk_score (POST clear=1). Equipment Approve. A GET
    aimed at #rk-total (the selects' change) answers with the running total for what is chosen."""
    _require(eq_perms.can_set_risk(request.user))
    dm = get_model(pk)
    if request.method != "POST":
        if is_partial(request, "rk-total"):
            form = RiskScoreForm(request.GET, device_model=dm)
            return render(request, "web/_model_risk_total.html", {"dm": dm, "preview": form.preview()})
        return _risk_modal(request, dm, RiskScoreForm(device_model=dm))
    before = {"risk_class": dm.risk_class, "score": dm.risk_score}
    if request.POST.get("clear"):
        if before["score"] is None:
            return toast(_risk_modal(request, dm, RiskScoreForm(device_model=dm)), f"{dm} has no risk score to clear")
        eq.clear_risk_score(dm, by=request.user)
        return _saved(request, dm, f"Risk score cleared for {dm}; its class stays {dm.get_risk_class_display()}")
    form = RiskScoreForm(request.POST, device_model=dm)
    if form.is_valid():
        try:
            eq.set_risk_score(dm, by=request.user, **form.parts())
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        dm.refresh_from_db()  # as stored: a refused score changed nothing
        return _risk_modal(request, dm, form)
    return _saved(request, dm, risk_toast(dm, before))
