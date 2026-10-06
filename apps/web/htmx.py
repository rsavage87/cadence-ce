"""Small helpers shared by the web views: partial detection and the toast event."""
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
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


class FacilityTabMiddleware:
    """A browser works in one facility at a time (slice 22): switching facility signs it in to the person's other account. A tab
    still showing the old facility sends that facility with each HTMX request (base.html's hx-headers), and gets a reload instead of
    an answer from the facility the browser is now in (a work order number names another work order there), and instead of a CSRF
    refusal for its old token. Runs before the views, so before the CSRF check too."""

    HEADER = "X-Cadence-Facility"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        shown = request.headers.get(self.HEADER)
        if shown is not None and request.headers.get("HX-Request") == "true":
            tenant = getattr(request, "tenant", None)
            if shown != (str(tenant.pk) if tenant is not None else ""):
                response = HttpResponse(status=204)
                response["HX-Refresh"] = "true"
                return response
        return self.get_response(request)
