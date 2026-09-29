"""Small helpers shared by the web views: partial detection and the toast event."""
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
