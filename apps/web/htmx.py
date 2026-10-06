"""Small helpers shared by the web views: partial detection and the toast event."""
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import csrf
from django_htmx.http import trigger_client_event

PAGE_SIZE = 25


def is_partial(request, target: str) -> bool:
    """True when HTMX is asking to refresh just the element `target` (by id)."""
    return bool(request.htmx) and request.htmx.target == target


def toast(response, message: str):
    """Show `message` in the shell's toast after this response is swapped in."""
    return trigger_client_event(response, "toast", {"value": message})


class VaryOnHtmxMiddleware:
    """The web UI answers one URL with either a full page or a fragment, depending on HX-Request and HX-Target (pushed URLs
    such as /pm/?day= are loaded both ways). Tell caches so, or a back/forward navigation can be served a bare fragment."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        patch_vary_headers(response, ("HX-Request", "HX-Target"))
        return response


def screen_url(url: str, request) -> str:
    """The screen `url` (a page of this site) belongs to, as a path: its first segment ("/work-orders/WO-26-0003/?x=1" gives
    "/work-orders/"), never a record or its filters; the Overview for anything else (another site, a print, an unknown path)."""
    if not url or not url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return "/"
    first = urlsplit(url).path.strip("/").split("/")[0]
    if not first:
        return "/"
    try:
        match = resolve(f"/{first}/")
    except Resolver404:
        return "/"
    return f"/{first}/" if match.url_name in SCREENS else "/"


# The screens a stale tab goes back to (apps.web.context_processors.NAV's lists): each one's list, in the facility the browser is in now.
SCREENS = {"equipment", "workorders", "pm", "contracts", "recalls", "reports", "users", "settings"}


def _stale(request):
    """A page left open in another facility, or from before a switch, made this request: send it to the same screen's list in the
    facility the browser is in now (HTMX navigates; a form post is redirected). Never the record it showed: record numbers repeat
    across facilities (WO-26-0003 is another work order there), and a reload of its address would open that one."""
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = screen_url(request.headers.get("HX-Current-URL", ""), request)
        return response
    return redirect(screen_url(request.headers.get("Referer", ""), request))


class FacilityTabMiddleware:
    """A browser works in one facility at a time (slice 22): switching facility signs it in to the person's other account. A tab
    still showing the old facility sends that facility with each HTMX request (base.html's hx-headers), and is sent to the same
    screen in the facility the browser is in now instead of being answered from it (a work order number names another work order
    there). Runs before the views, so before the CSRF check too. A tab whose facility is current again (switched away and back
    elsewhere) has an old CSRF token: csrf_failure sends it on the same way."""

    HEADER = "X-Cadence-Facility"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        shown = request.headers.get(self.HEADER)
        if shown is not None and request.headers.get("HX-Request") == "true":
            tenant = getattr(request, "tenant", None)
            if shown != (str(tenant.pk) if tenant is not None else ""):
                return _stale(request)
        return self.get_response(request)


def csrf_failure(request, reason=""):
    """settings.CSRF_FAILURE_VIEW (slice 22 review). Switching facility signs the browser in again, which renews its CSRF token, so
    every page left open in other tabs (the facility menu, Sign out, a drawer's form) posts with a token that no longer works. For
    someone signed in that is a stale page, not an attack: nothing was done, and they are sent to the same screen in the facility the
    browser is in (an HTMX request navigates there; a form post is redirected), where they see where they are and act again. Anyone
    signed out gets Django's refusal page."""
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        return _stale(request)
    return csrf.csrf_failure(request, reason)
