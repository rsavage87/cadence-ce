"""
The History tab (slice 20, part A): a record's changes in its drawer, newest first, read through apps.core.history. The device and
device model drawers have tabs, so History is a tab there (?tab=history); the work order and contract drawers are sections, so it is
a section at their foot that loads when asked (?history=1). A work order's history carries its labor and part lines' changes, and a
device model's its AEM cases', interleaved by time (lines removed since keep theirs).

Who: View on the record's module (apps.core.history.AREAS) and not scoped (apps.workorders.scoping: a vendor technician or a
clinical requester sees no history, though the device and work order drawers admit them). The drawers offer the tab only to them,
and the views refuse everyone else with a 403 (`require`) whenever history is asked for, a partial or the whole drawer.

Pages: the latest PAGE entries, then "Show older" (HTMX, the element #hist-more) swaps in the next PAGE with the next button.
?older= carries the page's cursor (apps.core.history.cursor_text: the last entry shown), so a change saved meanwhile never makes the
next page repeat or skip entries. The HTMX partials are the entries alone (#hist for a section's first page, #hist-more after it);
opened directly, the same URL renders the screen with the drawer open on its history at that page.
"""
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from apps.core import history

from .htmx import is_partial

PAGE = 50
ENTRIES = "web/_history_entries.html"
PARTIALS = ("hist", "hist-more")  # a section's first page, and Show older

# What each drawer's history reads besides the record's own changes: area key -> the historical rows' field holding the record.
RELATED = {
    "work_orders": {"labor": "work_order_id", "parts": "work_order_id"},
    "device_models": {"aem": "device_model_id"},
    "incidents": {"incident_holds": "incident_id"},  # slice 28: the incident drawer's History carries its holds' changes
}
NOUNS = {"labor": "Labor", "parts": "Part", "aem": "AEM case", "incident_holds": "Hold"}  # a related row's entries: "Labor added", "Part removed"
VERBS = {"added": "added", "changed": "changed", "deleted": "removed"}


def allowed(user, area_key: str) -> bool:
    return history.can_read(user, area_key)


def require(user, area_key: str) -> None:
    if not allowed(user, area_key):
        raise PermissionDenied


def asked(request, *, tab: bool) -> bool:
    """The request asks for history: the drawer's History tab or section, Show older, or one of the partials."""
    params = request.GET
    shown = params.get("tab") == "history" if tab else bool(params.get("history"))
    return shown or "older" in params or any(is_partial(request, p) for p in PARTIALS)


def wants_entries(request) -> bool:
    """Only the entries (an HTMX partial), not the drawer."""
    return any(is_partial(request, p) for p in PARTIALS)


def _title(entry, own_area: str) -> str:
    if entry.area == own_area:
        return entry.action_label
    return f"{NOUNS.get(entry.area, entry.area_label)} {VERBS.get(entry.action, entry.action)}"


def context(request, obj, area_key: str, url: str, *, tab: bool) -> dict:
    """{"hist": ...} for the drawer's history (or the entries partial): `url` is the record's drawer URL. Callers check `require`
    first. The page after ?older= (a cursor; anything else reads as the newest)."""
    after = request.GET.get("older") or None
    if after is not None and history.parse_cursor(after) is None:
        after = None
    related = {key: {field: obj.pk} for key, field in RELATED.get(area_key, {}).items() if allowed(request.user, key)}
    entries, following = history.record_history(obj, related, limit=PAGE, after=after)
    rows = [{"e": e, "title": _title(e, area_key)} for e in entries]
    more_url = None
    if following is not None:
        more_url = f"{url}?" + urlencode({**({"tab": "history"} if tab else {"history": 1}), "older": following})
    return {"hist": {"rows": rows, "more_url": more_url, "first": after is None}}


def entries_response(request, obj, area_key: str, url: str, *, tab: bool):
    return render(request, ENTRIES, context(request, obj, area_key, url, tab=tab))
