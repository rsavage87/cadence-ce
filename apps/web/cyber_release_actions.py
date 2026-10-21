"""
A release row's update campaign on the Device security screen's model panel (slice 38; web/_cyber_release_actions.html): its update
work orders by state as counts (rules.campaigns: one query for every release of the model), "Updates to 5.6 (demo): 3 open, 1 done,
1 not confirmed" (rules.campaign_words; Device security View: counts, no device), and "Open updates to this release"
(permissions.can_open_updates, with devices of the model on site on another release or none recorded), opening the batch's modal
(views_cyber_updates.release_open_updates) with the panel's filters in its address, so the panel comes back as it was.

Slice 39, a release row's security decision (x["decision"]; Device security View and Equipment View, permissions.can_view_devices: a
decision names devices): the release's live decision for devices past support, its number linking to its page with its review in rules'
words (rules.review_words), or the model's live decision for every device of ours when the release has none of its own; else "Record
decision" for this release (permissions.can_decide) when a device of ours on site on it has a need a decision answers (rules.support_need:
past its support, or an end announced within 12 months) with no decision governing it (rules.governing_decisions: its own, a device
scope) and no update responding to it (rules.update_responses), as Needs attention's rows read them (rules.needs_attention: a device so
answered is never "no decision recorded"), its address carrying the panel's filters (`from=panel`: the save answers with the panel as it
was). A fixed number of queries however many releases, devices, and decisions: the live decisions of the model (one), and for
permissions.can_decide, when a release has no decision of its own or the model's, the model's devices of ours on site with their rows and
the profile (rules.standings: two), and for those with a need on such a release the decisions that may govern them and their update
responses (two).
"""
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules
from apps.cybersecurity.models import AppliesTo, DeviceSecurityDecision
from apps.equipment.models import Ownership


def open_url(release, params: dict) -> str:
    """The batch's address for `release`, carrying the panel's filters (`params`, FleetFilters.params())."""
    return reverse("web:cybersecurity_release_open_updates", args=[release.pk]) + ("?" + urlencode(params) if params else "")


def record_url(release, params: dict) -> str:
    """Record decision's address for `release` (release scope), answering with the panel and carrying its filters."""
    return f"{reverse('web:security_decision_new')}?{urlencode({'release': release.pk, 'from': 'panel', **params})}"


def _needing(dm, among: set, today) -> set:
    """The ids of `among` (releases of the model with no live decision of their own or the model's) that a device of ours on site runs
    with a need a decision answers (rules.support_need decidable), no decision governing it (rules.governing_decisions: its own), and no
    update responding to it (rules.update_responses: an open update to a supported release, or one done with the version not confirmed),
    as Needs attention reads them: the row's "with no decision recorded" holds for each device it counts."""
    if not among:
        return set()
    read = rules.standings(rules.on_site().filter(device_model=dm, ownership=Ownership.OWNED), today)
    waiting = [s for s in read.values() if s.release is not None and s.release.pk in among
               and (need := rules.support_need(s, today)) is not None and need.decidable]
    if not waiting:
        return set()
    assets = [s.asset for s in waiting]
    governing = rules.governing_decisions(assets, today)
    responses = rules.update_responses(assets, today)
    return {s.release.pk for s in waiting if governing.get(s.asset.pk) is None and not responses.get(s.asset.pk)}


def _decisions(request, dm, releases: list, params: dict) -> None:
    """Add each release row's decision (x["decision"]: {"number", "url", "words", "model_wide", "record_url"} or None)."""
    today = timezone.localdate()
    live = list(DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.SUPPORT_ENDED, ended_on__isnull=True, device_model=dm,
                                                      asset__isnull=True).order_by("number"))
    by_release = {d.release_id: d for d in live if d.release_id}
    model_wide = next((d for d in live if not d.release_id), None)
    undecided = {x["r"].pk for x in releases if x["r"].pk not in by_release} if model_wide is None else set()
    needing = _needing(dm, undecided, today) if cyber_perms.can_decide(request.user) else set()
    for x in releases:
        release = x["r"]
        d = by_release.get(release.pk) or model_wide
        if d is not None:
            x["decision"] = {"number": d.number, "url": reverse("web:security_decision", args=[d.number]),
                             "words": rules.review_words(d, today), "model_wide": d.release_id is None, "record_url": ""}
        elif release.pk in needing:
            x["decision"] = {"number": "", "url": "", "words": "", "model_wide": False, "record_url": record_url(release, params)}
        else:
            x["decision"] = None


def annotate(request, dm, releases: list, params: dict) -> dict:
    """Add each release row's campaign (x["update"]: {"words", "open_url", "can_open"}) and, for permissions.can_view_devices, its
    security decision (x["decision"], slice 39) to `releases` (views_cybersecurity._panel_context's rows, each {"r": release, "running":
    devices on site on it, ...}) and return what the panel adds to its context ({"can_open_updates"}). Two queries at most for the
    campaigns, however many releases: the campaigns, and (for a user who may open updates) the model's devices on site; slice 39's are
    _decisions'."""
    if not releases:
        return {"can_open_updates": False}
    can_open = cyber_perms.can_open_updates(request.user)
    counts = rules.campaigns(dm)
    on_site = rules.on_site().filter(device_model=dm).count() if can_open else 0
    for x in releases:
        release = x["r"]
        x["update"] = {"words": rules.campaign_words(release, counts.get(release.pk)) or "", "open_url": open_url(release, params),
                       "can_open": can_open and on_site - x.get("running", 0) > 0}
        x["decision"] = None
    if cyber_perms.can_view_devices(request.user):
        _decisions(request, dm, releases, params)
    return {"can_open_updates": can_open}
