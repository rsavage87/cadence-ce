"""
Public, sign-in-free service request portal: /r/<tenant-slug>/?dept=ICU&asset=CE-10241

Rate-limited per IP. Creates a ServiceRequest plus an unassigned work order, then shows the
request number and response target. No patient identifiers are asked for.

When the facility confirms requests by email (Settings, slice 13), the form also offers an optional work email at one of the
facility's domains; apps.portal.notifications sends the confirmation and, later, the done notice. The confirmation page names
the address only to the browser that sent the request (a short-lived signed cookie), since request numbers are easy to guess.
"""
from django.conf import settings
from django.core.cache import caches
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect

from apps.core.http import client_ip
from apps.equipment.models import Asset, Department
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import ServiceRequest, Urgency
from apps.workorders.services import create_service_request

from .forms import ServiceRequestForm
from .notifications import confirmation_sent

RESPONSE_TARGETS = {Urgency.CRITICAL: "Within 1 hour, around the clock", Urgency.HIGH: "Within 4 hours", Urgency.NORMAL: "Within 2 business days"}
SENT_COOKIE, SENT_SALT, SENT_MAX_AGE = "portal_sent", "apps.portal.sent", 60 * 60  # which request this browser just sent


def _rate_limited(ip) -> bool:
    cache = caches["limits"]  # the rate-limit store (settings.CACHES)
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
        facility = fs.get_settings()  # this tenant's portal settings: whether a callback is required, the hotline, email confirmations
        email_domains = fs.email_domains(facility) if fs.portal_emails_on(facility) else []
        initial = {}
        asset = None
        if request.GET.get("asset"):
            asset = Asset.objects.filter(tag__iexact=request.GET["asset"]).select_related("device_model", "department").first()
            if asset:
                initial["asset_tag"] = asset.tag
                initial["department"] = asset.department
        if request.GET.get("dept") and "department" not in initial:
            # Department links carry the name; an exact match wins over a case-insensitive one ("ICU" and "Icu" can both exist).
            wanted = request.GET["dept"]
            initial["department"] = Department.objects.filter(name=wanted).first() or Department.objects.filter(name__iexact=wanted).first()
        if request.method == "POST":
            ip = client_ip(request)
            if _rate_limited(ip):
                shop = f"the Clinical Engineering shop at {facility.portal_hotline}" if facility.portal_hotline else "the Clinical Engineering shop"
                return HttpResponse(f"Too many requests from this location. Please call {shop}.", status=429)
            form = ServiceRequestForm(request.POST, require_callback=facility.portal_require_callback, email_domains=email_domains)
            if form.is_valid():
                sr = create_service_request(asset=form.asset, department=form.cleaned_data["department"], problem=form.cleaned_data["problem"],
                                            urgency=form.cleaned_data["urgency"], requester_name=form.cleaned_data["requester_name"],
                                            callback=form.cleaned_data["callback"], room=form.cleaned_data["room"],
                                            tagged_out=form.cleaned_data["tagged_out"], ip=ip,
                                            requester_email=form.cleaned_data.get("requester_email", ""))
                response = redirect("portal:done", tenant_slug=tenant.slug, number=sr.number)
                response.set_signed_cookie(SENT_COOKIE, sr.number, salt=SENT_SALT, max_age=SENT_MAX_AGE, path=f"/r/{tenant.slug}/",
                                           secure=request.is_secure(), httponly=True, samesite="Lax")
                return response
        else:
            form = ServiceRequestForm(initial=initial, require_callback=facility.portal_require_callback, email_domains=email_domains)
        return render(request, "portal/request.html", {"tenant": tenant, "form": form, "asset": asset, "hotline": facility.portal_hotline})


def request_done(request, tenant_slug, number):
    tenant = get_object_or_404(Tenant, slug=tenant_slug, is_active=True)
    with tenant_context(tenant):
        sr = get_object_or_404(ServiceRequest.objects.select_related("asset", "asset__device_model"), number=number)
        sent_here = request.get_signed_cookie(SENT_COOKIE, default=None, salt=SENT_SALT, max_age=SENT_MAX_AGE) == sr.number
        return render(request, "portal/done.html", {"tenant": tenant, "sr": sr, "target": RESPONSE_TARGETS[sr.urgency],
                                                    "hotline": fs.get_settings().portal_hotline,
                                                    "emailed": bool(sr.requester_email) and confirmation_sent(sr),
                                                    "email_shown": sr.requester_email if sent_here else ""})
