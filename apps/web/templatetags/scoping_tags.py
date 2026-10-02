"""
Text a work order carries, as a scoped user may read it (slice 16). Completing a PM writes its repair's number into the PM's
resolution and history, and the repair's problem names the PM (apps.workorders.completion); notes may name others. A scoped user
(apps.workorders.scoping) may hold one of the pair without the other, so the number of a work order outside their share reads
"another work order" wherever these show it: the work order drawer, its print, the device drawer's lists, and the CSV export.
Everyone else reads the text as written.
"""
import re

from django import template

from apps.workorders import scoping

register = template.Library()

WO_NUMBER = re.compile(r"\bWO-\d{2}-\d{4,}\b")  # WorkOrder.save's numbers: WO-26-0042
HIDDEN = "another work order"


def scoped_text(user, text: str, seen: dict | None = None) -> str:
    """`text` with every work-order number outside a scoped user's share replaced by HIDDEN. `seen` caches the answers (one query
    per distinct number); pass the same dict for a request's many texts."""
    if not text or not scoping.is_scoped(user) or not WO_NUMBER.search(text):
        return text
    seen = {} if seen is None else seen

    def shown(match) -> str:
        number = match.group(0)
        if number not in seen:
            seen[number] = scoping.work_orders(user).filter(number=number).exists()
        return number if seen[number] else HIDDEN

    return WO_NUMBER.sub(shown, text)


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
