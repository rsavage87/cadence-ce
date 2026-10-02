"""
Scan tag (slice 18, the mock's button on Equipment): open a device from its label. A handheld scanner types the code into the
field (keyboard wedge), a phone or laptop camera reads it where the browser can (static/web/scan.js, BarcodeDetector), or someone
types the tag. What a code may be, and how it is read, is apps/equipment/scan.py: a tag, a label's request link
(/r/<slug>/?asset=<tag>), or a link to the device's page.

/scan/ without a code is the modal (HTMX into #modal-card) or, opened directly, a page of its own that works on a phone. With
?code= (a GET form: a lookup changes nothing) the device is looked up among those the user may see (apps.workorders.scoping, so a
vendor's or a unit's share) and opened: from the modal, its drawer swaps into #drawer with /equipment/<tag>/ pushed to the address
and the modal closes after settle (closing first would detach the form that sent the request and cancel the swap); a direct visit
redirects to /equipment/<tag>/. A code that names none of their devices brings the form back with the reason, the code still in the
field (scan.js selects it, so the next scan replaces it); a device outside their share reads like a tag that does not exist.
"""
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django_htmx.http import push_url, retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment import scan as scan_rules
from apps.equipment.models import Asset
from apps.workorders import scoping

from .decorators import web_view
from .views import asset_drawer_context

DRAWER = "web/_asset_drawer.html"


def _form(request, code: str = "", error: str = ""):
    ctx = {"code": code, "error": error}
    if request.htmx:
        return render(request, "web/_scan.html", ctx)
    return render(request, "web/scan.html", {**ctx, "nav_active": "equipment"})


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def scan(request):
    code = scan_rules.clean(request.GET.get("code", ""))
    if not code:
        return _form(request)
    mine = scoping.assets(request.user, Asset.objects.select_related("device_model", "department", "contract", "tenant"))
    try:
        asset = scan_rules.find(code, slug=request.tenant.slug, qs=mine)
    except ValidationError as e:
        return _form(request, code, e.messages[0])
    url = reverse("web:asset", args=[asset.tag])
    if not request.htmx:
        return redirect(url)
    response = retarget(render(request, DRAWER, asset_drawer_context(request, asset)), "#drawer")
    push_url(response, url)
    return trigger_client_event(response, "modal-close", {}, after="settle")
