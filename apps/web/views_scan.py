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

Slice 24: Scan opened from My work (its Scan button, web/_my_work_scan.html, asks for from=my_work, which the form keeps in a hidden
field) opens what apps.workorders.my_work.scan_target says, the rule the portal's "Open in Cadence" link follows too: the one open
work order of theirs on the device, else the device at its Work orders tab. Its drawer opens over My work with no address pushed
(going back stays on My work, the list behind the drawer); a direct visit redirects to its page. The form there is sized for a
thumb (44px targets) and tells an iPhone, whose browser cannot read codes, to point the Camera app at the label.
"""
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django_htmx.http import push_url, retarget, trigger_client_event

from apps.accounts.models import Level, Module
from apps.equipment import scan as scan_rules
from apps.equipment.models import Asset
from apps.workorders import my_work, scoping

from .decorators import web_view
from .views import _render_wo_drawer, asset_drawer_context

DRAWER = "web/_asset_drawer.html"
FROM_MY_WORK = "my_work"  # the `from` My work's Scan button asks for, kept by the form


def _form(request, code: str = "", error: str = "", *, from_my_work: bool = False):
    ctx = {"code": code, "error": error, "from_my_work": from_my_work}
    if request.htmx:
        return render(request, "web/_scan.html", ctx)
    return render(request, "web/scan.html", {**ctx, "nav_active": "my_work" if from_my_work else "equipment"})


@web_view(Module.EQUIPMENT, Level.VIEW, scoped=True)
def scan(request):
    from_my_work = request.GET.get("from") == FROM_MY_WORK
    code = scan_rules.clean(request.GET.get("code", ""))
    if not code:
        return _form(request, from_my_work=from_my_work)
    mine = scoping.assets(request.user, Asset.objects.select_related("device_model", "department", "contract", "tenant"))
    try:
        asset = scan_rules.find(code, slug=request.tenant.slug, qs=mine)
    except ValidationError as e:
        return _form(request, code, e.messages[0], from_my_work=from_my_work)
    if from_my_work:
        return _open_from_my_work(request, asset)
    url = reverse("web:asset", args=[asset.tag])
    if not request.htmx:
        return redirect(url)
    response = retarget(render(request, DRAWER, asset_drawer_context(request, asset)), "#drawer")
    push_url(response, url)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _open_from_my_work(request, asset):
    """What my_work.scan_target opens for this user on `asset` (a device in their share: the lookup was): its drawer over My work,
    no address pushed, and the modal closed after settle; a direct visit redirects to its page."""
    target = my_work.scan_target(request.user, asset)
    if not request.htmx:
        return redirect(target.url)
    if target.work_order is not None:
        response = _render_wo_drawer(request, target.work_order)  # read again through get_wo: the user's share, the drawer's rows
    else:
        response = render(request, DRAWER, asset_drawer_context(_at_tab(request, target.tab), asset))
    return trigger_client_event(retarget(response, "#drawer"), "modal-close", {}, after="settle")


def _at_tab(request, tab: str):
    """`request` asking for the device drawer's `tab` (asset_drawer_context reads it from the query, which a scan's lacks)."""
    if tab:
        query = request.GET.copy()
        query["tab"] = tab
        request.GET = query
    return request
