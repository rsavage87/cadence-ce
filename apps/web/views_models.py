"""
The device model drawer (slice 14): a model's PM program, opened from the PM library and the device drawer. Three tabs: PM program
(this module: details, risk score, intervals, devices), Procedure (views_procedures.procedure_tab), and AEM (views_aem.aem_tab); and
History (slice 20, apps.web.history_tabs: the model's changes with its AEM cases') for Equipment View, refused to anyone else.
Add model and Edit details (Equipment Edit) and the risk score (Equipment Approve) are here too: a model's details and its risk
are equipment data (apps.equipment.permissions), checked here on every request, GET and POST, behind the PM View the screen needs.
On both forms, marking a model as equipment CMS keeps on the manufacturer's schedule (imaging, radiologic, medical laser; slice
18) needs Equipment Approve as well: others see the mark read-only, and a form that changes it anyway is refused.

Every change fires `models-changed` (the PM library re-fetches on it) and toasts. Actions inside a tab answer with the whole drawer
on that tab (render_model_drawer), which #drawer swaps in. The three forms are modals (#modal-card): a save swaps the model's
drawer into #drawer instead (HX-Retarget; the shell opens the drawer on that swap) and closes the modal after settle.
"""
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django_htmx.http import retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel, RiskClass
from apps.facility.services import RISK_BANDS, RISK_RUBRIC
from apps.pm import aem
from apps.pm import permissions as pm_perms

from . import history_tabs, views_aem, views_procedures
from .decorators import web_view
from .forms_models import RISK_MEANINGS, DeviceModelForm, RiskScoreForm
from .htmx import is_partial, toast

DRAWER = "web/_model_drawer.html"
TABS = (("program", "PM program"), ("procedure", "Procedure"), ("aem", "AEM"))
HISTORY_TAB = ("history", "History")  # slice 20: offered by tabs_for to those who may read it
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
    today = today or timezone.localdate()
    user = request.user
    # Slice 29: the devices on CE's PM program, ours only (a rental, vendor loaner, or demo unit is its owner's: equipment.services.OWNED)
    active = (Asset.objects.filter(eq.OWNED, device_model=dm, status__in=Asset.ACTIVE_STATUSES).select_related("department")
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
                     # an AEM interval on file that an excluded model ignores (DeviceModel.pm_interval_months)
                     "aem_ignored": aem_on_file and months == oem and dm.aem_excluded,
                     "life_support": dm.risk_class == RiskClass.LIFE_SUPPORT,
                     # CMS: imaging, radiologic, and medical laser equipment keep the manufacturer's schedule (slice 18)
                     "oem_required": dm.oem_schedule_required},
        "devices": {"shown": shown, "active": active_count, "more": max(active_count - len(shown), 0),
                    "retired": Asset.objects.filter(eq.OWNED, device_model=dm, status=AssetStatus.RETIRED).count()},
        "can_view_asset": user.has_level(Module.EQUIPMENT, Level.VIEW),
        "can_edit_model": eq_perms.can_edit_model(user), "can_set_risk": eq_perms.can_set_risk(user),
    }}


def tabs_for(user) -> tuple:
    """TABS, and History (slice 20: the model's changes with its AEM cases', apps.web.history_tabs) for Equipment View: a model's
    details are equipment data. Scoped users never open this drawer (the PM screen refuses them)."""
    return TABS + ((HISTORY_TAB,) if history_tabs.allowed(user, "device_models") else ())


def model_drawer_context(request, dm, tab: str | None = None) -> dict:
    """The drawer on `tab` (else ?tab= in the request, else PM program), with that tab's context."""
    tabs = tabs_for(request.user)
    keys = [k for k, _label in tabs]
    tab = tab if tab in keys else request.GET.get("tab") if request.GET.get("tab") in keys else "program"
    ctx = {"dm": dm, "tab": tab, "tabs": tabs, "nav_active": "pm"}
    if tab == "program":
        ctx.update(program_tab(request, dm))
    elif tab == "procedure":
        ctx.update(views_procedures.procedure_tab(request, dm))
    elif tab == "aem":
        ctx.update(views_aem.aem_tab(request, dm))
    else:
        ctx.update(history_tabs.context(request, dm, "device_models", reverse("web:pm_model", args=[dm.pk]), tab=True))
    return ctx


def render_model_drawer(request, dm, tab: str | None = None, status: int = 200) -> HttpResponse:
    """The whole drawer, freshly read, on `tab`: what every action inside the drawer answers with."""
    return render(request, DRAWER, model_drawer_context(request, get_model(dm.pk), tab), status=status)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_detail(request, pk):
    dm = get_model(pk)
    if history_tabs.asked(request, tab=True):  # the History tab or its Show older: Equipment View too
        history_tabs.require(request.user, "device_models")
        if history_tabs.wants_entries(request):
            return history_tabs.entries_response(request, dm, "device_models", reverse("web:pm_model", args=[dm.pk]), tab=True)
    if request.htmx:
        return render(request, DRAWER, model_drawer_context(request, dm))
    from .views_pm import pm_page_context

    return render(request, "web/pm.html", {**pm_page_context(request), **model_drawer_context(request, dm), "drawer_template": DRAWER})


# --- the modals -------------------------------------------------------------------------------------------------------------

def _require(allowed: bool):
    if not allowed:
        raise PermissionDenied


def aem_note(effect: dict | None) -> str:
    """What the AEM program did about a change (equipment.services sets dm.aem_effect): a model scored into life support, or marked
    as keeping the manufacturer's schedule (CMS), leaves AEM."""
    if not effect:
        return ""
    parts = []
    if effect.get("ended"):
        # The CMS mark is only set on Edit details, whose toast already names it (mark_toast)
        why = "back on the OEM interval" if effect.get("rule") == "oem_schedule" else "life support follows the OEM interval"
        parts.append(f"its AEM interval ended ({why})")
    if effect.get("withdrawn"):
        parts.append("its open AEM proposal was withdrawn")
    if effect.get("cleared"):
        parts.append("the AEM interval on file without a recorded approval was cleared")
    moved = effect.get("moved") or 0
    if moved:
        whose = "1 device's" if moved == 1 else f"{moved} devices'"
        parts.append(f"{whose} next PM moved earlier")
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


def mark_toast(dm, was_marked: bool) -> str:
    """What saving said about the CMS mark, when it changed: marking moves the model off AEM (aem_note says what ended), clearing it
    leaves the OEM interval in force until an AEM is approved again."""
    if dm.oem_schedule_required == was_marked:
        return ""
    if dm.oem_schedule_required:
        return ": it keeps the manufacturer's schedule (CMS)"
    return ": the manufacturer's schedule is no longer required; it stays on the OEM interval until an AEM is approved"


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_new(request):
    """Add model: a new catalog entry through create_device_model. Equipment Edit; marking it for the manufacturer's schedule
    (CMS), Equipment Approve."""
    _require(eq_perms.can_edit_model(request.user))
    can_mark = eq_perms.can_set_oem_schedule(request.user)
    if request.method != "POST":
        return _form_modal(request, DeviceModelForm(can_set_oem_schedule=can_mark))
    form = DeviceModelForm(request.POST, can_set_oem_schedule=can_mark)
    dm = None
    if form.is_valid():
        try:
            dm = eq.create_device_model(**form.service_fields(), by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
    if dm is None:
        return _form_modal(request, form)
    return _saved(request, dm, f"{dm} added to the catalog" + ("; it keeps the manufacturer's schedule (CMS)" if dm.oem_schedule_required else ""))


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_edit(request, pk):
    """Edit details: everything but the risk class (the risk score's job) through update_device_model. Equipment Edit; the CMS
    mark, Equipment Approve."""
    _require(eq_perms.can_edit_model(request.user))
    can_mark = eq_perms.can_set_oem_schedule(request.user)
    dm = get_model(pk)
    if request.method != "POST":
        return _form_modal(request, DeviceModelForm(device_model=dm, can_set_oem_schedule=can_mark), dm)
    was_marked = dm.oem_schedule_required
    form = DeviceModelForm(request.POST, device_model=dm, can_set_oem_schedule=can_mark)  # no risk class or AEM field: posting them changes nothing
    if form.is_valid():
        try:
            eq.update_device_model(dm, by=request.user, **form.service_fields())
        except ValidationError as e:
            form.add_service_errors(e)
    if not form.is_valid():
        dm.refresh_from_db()  # the heading shows the stored name, not the one refused
        return _form_modal(request, form, dm)
    return _saved(request, dm, f"{dm} updated" + mark_toast(dm, was_marked))


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
