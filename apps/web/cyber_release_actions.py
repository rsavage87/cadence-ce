"""
A release row's update campaign on the Device security screen's model panel (slice 38; web/_cyber_release_actions.html): its update
work orders by state as counts (rules.campaigns: one query for every release of the model), "Updates to 5.6 (demo): 3 open, 1 done,
1 not confirmed" (Device security View: counts, no device), and "Open updates to this release" (permissions.can_open_updates, with
devices on site of the model on another release or none recorded). Wave 2 builds it.
"""


def annotate(request, dm, releases: list, params: dict) -> dict:
    """Add each release row's campaign (x["update"]) to `releases` (views_cybersecurity._panel_context's rows, each {"r": release, ...})
    and return what the panel adds to its context."""
    return {}
