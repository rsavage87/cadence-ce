"""
A release row's update campaign on the Device security screen's model panel (slice 38; web/_cyber_release_actions.html): its update
work orders by state as counts (rules.campaigns: one query for every release of the model), "Updates to 5.6 (demo): 3 open, 1 done,
1 not confirmed" (rules.campaign_words; Device security View: counts, no device), and "Open updates to this release"
(permissions.can_open_updates, with devices of the model on site on another release or none recorded), opening the batch's modal
(views_cyber_updates.release_open_updates) with the panel's filters in its address, so the panel comes back as it was.
"""
from urllib.parse import urlencode

from django.urls import reverse

from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules


def open_url(release, params: dict) -> str:
    """The batch's address for `release`, carrying the panel's filters (`params`, FleetFilters.params())."""
    return reverse("web:cybersecurity_release_open_updates", args=[release.pk]) + ("?" + urlencode(params) if params else "")


def annotate(request, dm, releases: list, params: dict) -> dict:
    """Add each release row's campaign (x["update"]: {"words", "open_url", "can_open"}) to `releases` (views_cybersecurity._panel_context's
    rows, each {"r": release, "running": devices on site on it, ...}) and return what the panel adds to its context ({"can_open_updates"}).
    Two queries at most, however many releases: the campaigns, and (for a user who may open updates) the model's devices on site."""
    if not releases:
        return {"can_open_updates": False}
    can_open = cyber_perms.can_open_updates(request.user)
    counts = rules.campaigns(dm)
    on_site = rules.on_site().filter(device_model=dm).count() if can_open else 0
    for x in releases:
        release = x["r"]
        x["update"] = {"words": rules.campaign_words(release, counts.get(release.pk)) or "", "open_url": open_url(release, params),
                       "can_open": can_open and on_site - x.get("running", 0) > 0}
    return {"can_open_updates": can_open}
