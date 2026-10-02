"""
Text a work order carries, as a scoped user may read it (slice 16). Completing a PM writes its repair's number into the PM's
resolution and history, and the repair's problem names the PM (apps.workorders.completion); notes may name others. A scoped user
(apps.workorders.scoping) may hold one of the pair without the other, so the number of a work order outside their share reads
"another work order" wherever these show it: the work order drawer, its print, the device drawer's lists, and the CSV export.
Everyone else reads the text as written.
"""
from django import template

from apps.workorders import scoping
from apps.workorders.scoping import HIDDEN, WO_NUMBER  # noqa: F401 (the tests and the templates' callers read them here)

register = template.Library()


def scoped_text(user, text: str, seen: dict | None = None) -> str:
    """apps.workorders.scoping.shown_text, which the API shares."""
    return scoping.shown_text(user, text, seen)


@register.simple_tag(takes_context=True)
def shown_text(context, text):
    """{% shown_text wo.problem %}: the text as the signed-in user may read it (escaped like any variable)."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return text
    if not hasattr(request, "_scoped_numbers"):
        request._scoped_numbers = {}
    return scoped_text(user, text, request._scoped_numbers)
