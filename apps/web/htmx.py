"""Small helpers shared by the web views: partial detection and the toast event."""
from django_htmx.http import trigger_client_event

PAGE_SIZE = 25


def is_partial(request, target: str) -> bool:
    """True when HTMX is asking to refresh just the element `target` (by id)."""
    return bool(request.htmx) and request.htmx.target == target


def toast(response, message: str):
    """Show `message` in the shell's toast after this response is swapped in."""
    return trigger_client_event(response, "toast", {"value": message})
