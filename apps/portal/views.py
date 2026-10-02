"""
Public, sign-in-free service request portal: /r/<tenant-slug>/?dept=ICU&asset=CE-10241

Rate-limited per IP. Creates a ServiceRequest plus an unassigned work order, then shows the
request number and response target. No patient identifiers are asked for.

When the facility confirms requests by email (Settings, slice 13), the form also offers an optional work email at one of the
facility's domains; apps.portal.notifications sends the confirmation and, later, the done notice. The confirmation page names
the address only to the browser that sent the request (a short-lived signed cookie), since request numbers are easy to guess.

Slice 18: a label's QR code opens this page with ?asset=<tag> (a phone's camera app reads it). Someone signed in to Cadence at this
facility who could open that device there (Equipment View, inside their share: apps.workorders.scoping) also sees a note naming
them, with a link that opens the device in Cadence. Everyone else gets the page exactly as before: signed out, a member of another
facility, a role without Equipment View, a share that leaves the device out, or a tag that is no device. The signed-in user is loaded
without their role (apps.accounts.backends.get_user: the role is a tenant-scoped row), so the role is read only for a member of
this facility and only inside its tenant_context, where row-level security shows it; another facility's user's role is never read.
"""
from django.conf import settings
from django.core.cache import caches
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from apps.accounts.models import Level, Module
from apps.core.http import client_ip
from apps.equipment.models import Asset, Department
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import scoping
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


def _open_in_cadence(request, tenant, asset) -> dict | None:
    """The signed-in note's name and link, or None. Call inside tenant_context(tenant)."""
    user = request.user
    if asset is None or not user.is_authenticated:
        return None
    # Signed in at this facility: the request works in it (TenantMiddleware: the user's own facility, or the one a superuser without a
    # facility chose), and the account is this facility's. Only then is the role read, here inside the facility's context.
    working_in = getattr(request, "tenant", None)
    if working_in is None or working_in.pk != tenant.pk or user.tenant_id not in (None, tenant.pk):
        return None
    if not user.has_level(Module.EQUIPMENT, Level.VIEW) or not scoping.can_see_asset(user, asset):
        return None
    return {"name": user.get_full_name() or user.username, "url": reverse("web:asset", args=[asset.tag])}


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
            # Otherwise the first case-insensitive match by code point, so SQLite, a C-collated and an en_US-collated PostgreSQL pick the
            # same one (ordering by name would follow the database's collation).
            initial["department"] = (Department.objects.filter(name=wanted).first()
                                     or min(Department.objects.filter(name__iexact=wanted), key=lambda d: d.name, default=None))
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
                # The facility and the number: request numbers repeat across facilities, so a cookie from one must not open another's
                response.set_signed_cookie(SENT_COOKIE, _sent_value(tenant, sr), salt=SENT_SALT, max_age=SENT_MAX_AGE, path=f"/r/{tenant.slug}/",
                                           secure=request.is_secure(), httponly=True, samesite="Lax")
                return response
        else:
            form = ServiceRequestForm(initial=initial, require_callback=facility.portal_require_callback, email_domains=email_domains)
        return render(request, "portal/request.html", {"tenant": tenant, "form": form, "asset": asset, "hotline": facility.portal_hotline,
                                                       "in_cadence": _open_in_cadence(request, tenant, asset)})


def _sent_value(tenant, sr) -> str:
    return f"{tenant.pk}:{sr.number}"


def request_done(request, tenant_slug, number):
    tenant = get_object_or_404(Tenant, slug=tenant_slug, is_active=True)
    with tenant_context(tenant):
        sr = get_object_or_404(ServiceRequest.objects.select_related("asset", "asset__device_model"), number=number)
        sent_here = request.get_signed_cookie(SENT_COOKIE, default=None, salt=SENT_SALT, max_age=SENT_MAX_AGE) == _sent_value(tenant, sr)
        return render(request, "portal/done.html", {"tenant": tenant, "sr": sr, "target": RESPONSE_TARGETS[sr.urgency],
                                                    "hotline": fs.get_settings().portal_hotline,
                                                    "emailed": bool(sr.requester_email) and confirmation_sent(sr),
                                                    "email_shown": sr.requester_email if sent_here else ""})
