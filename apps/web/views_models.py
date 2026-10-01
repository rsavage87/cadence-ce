"""
The device model drawer (slice 14): a model's PM program, opened from the PM library and the device drawer. Three tabs: PM program
(this module: details, risk score, intervals, devices), Procedure (views_procedures.procedure_tab), and AEM (views_aem.aem_tab).
Add model and Edit details (Equipment Edit) and the risk score (Equipment Approve) are here too.

Every change fires `models-changed` (the PM library re-fetches on it) and toasts. Actions inside a tab answer with the whole drawer
on that tab (render_model_drawer), which #drawer swaps in.

TODO(slice 14, part A): the PM program tab, Add model, Edit details, the risk score.
"""
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.equipment.models import DeviceModel
from apps.pm import permissions as pm_perms

from . import views_aem, views_procedures
from .decorators import web_view

DRAWER = "web/_model_drawer.html"
TABS = (("program", "PM program"), ("procedure", "Procedure"), ("aem", "AEM"))


def get_model(pk) -> DeviceModel:
    """Tenant-scoped: another facility's model is a 404."""
    return get_object_or_404(DeviceModel.objects.select_related("pm_procedure"), pk=pk)


def program_tab(request, dm) -> dict:
    """The PM program tab's context. TODO(part A)."""
    return {}


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


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_new(request):
    raise NotImplementedError  # TODO(part A)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_edit(request, pk):
    raise NotImplementedError  # TODO(part A)


@web_view(pm_perms.MODULE, pm_perms.VIEW_LEVEL)
def model_risk(request, pk):
    raise NotImplementedError  # TODO(part A)
