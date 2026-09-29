"""
Public, sign-in-free service request portal: /r/<tenant-slug>/?dept=ICU&asset=CE-10241

Rate-limited per IP. Creates a ServiceRequest plus an unassigned work order, then shows the
request number and response target. No patient identifiers are asked for.
"""
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect

from apps.equipment.models import Asset, Department
from apps.facility.services import get_settings
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import ServiceRequest, Urgency
from apps.workorders.services import create_service_request

from .forms import ServiceRequestForm

RESPONSE_TARGETS = {Urgency.CRITICAL: "Within 1 hour, around the clock", Urgency.HIGH: "Within 4 hours", Urgency.NORMAL: "Within 2 business days"}


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def _rate_limited(ip) -> bool:
    key = f"portal:{ip}"
    n = cache.get(key, 0)
    if n >= settings.PORTAL_RATE_LIMIT_PER_HOUR:
        return True
    cache.set(key, n + 1, 3600)
    return False


@csrf_protect
def request_form(request, tenant_slug):
    tenant = get_object_or_404(Tenant, slug=tenant_slug, is_active=True)
    with tenant_context(tenant):
        facility = get_settings()  # this tenant's portal settings: whether a callback is required, and the hotline
        initial = {}
        asset = None
        if request.GET.get("asset"):
            asset = Asset.objects.filter(tag__iexact=request.GET["asset"]).select_related("device_model", "department").first()
            if asset:
                initial["asset_tag"] = asset.tag
                initial["department"] = asset.department
        if request.GET.get("dept") and "department" not in initial:
            initial["department"] = Department.objects.filter(name__iexact=request.GET["dept"]).first()
        if request.method == "POST":
            ip = _client_ip(request)
            if _rate_limited(ip):
                return HttpResponse("Too many requests from this location. Please call the Clinical Engineering shop.", status=429)
            form = ServiceRequestForm(request.POST, require_callback=facility.portal_require_callback)
            if form.is_valid():
                sr = create_service_request(asset=form.asset, department=form.cleaned_data["department"], problem=form.cleaned_data["problem"],
                                            urgency=form.cleaned_data["urgency"], requester_name=form.cleaned_data["requester_name"],
                                            callback=form.cleaned_data["callback"], room=form.cleaned_data["room"],
                                            tagged_out=form.cleaned_data["tagged_out"], ip=ip)
                return redirect("portal:done", tenant_slug=tenant.slug, number=sr.number)
        else:
            form = ServiceRequestForm(initial=initial, require_callback=facility.portal_require_callback)
        return render(request, "portal/request.html", {"tenant": tenant, "form": form, "asset": asset, "hotline": facility.portal_hotline})


def request_done(request, tenant_slug, number):
    tenant = get_object_or_404(Tenant, slug=tenant_slug, is_active=True)
    with tenant_context(tenant):
        sr = get_object_or_404(ServiceRequest.objects.select_related("asset", "asset__device_model"), number=number)
        return render(request, "portal/done.html", {"tenant": tenant, "sr": sr, "target": RESPONSE_TARGETS[sr.urgency],
                                                    "hotline": get_settings().portal_hotline})
