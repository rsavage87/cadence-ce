"""
A release row's update campaign on the Device security screen's model panel (slice 38; web/_cyber_release_actions.html): its update
work orders by state as counts (rules.campaigns: one query for every release of the model), "Updates to 5.6 (demo): 3 open, 1 done,
1 not confirmed" (rules.campaign_words; Device security View: counts, no device), and "Open updates to this release"
(permissions.can_open_updates, with devices of the model on site on another release or none recorded), opening the batch's modal
(views_cyber_updates.release_open_updates) with the panel's filters in its address, so the panel comes back as it was.

Slice 39, a release row's security decision (x["decision"]; Device security View and Equipment View, permissions.can_view_devices: a
decision names devices): the release's live decision for devices past support, its number linking to its page with its review in rules'
words (rules.review_words), or the model's live decision for every device of ours when the release has none of its own; else "Record
decision" for this release (permissions.can_decide) when a device of ours on site on it waits for one (_waiting), its address carrying
the panel's filters (`from=panel`: the save answers with the panel as it was). The panel's footer offers Record decision for every device
of ours of the model (model scope: annotate's `decide_model`) on the same read, when any device of ours of the model on site waits,
whatever its release (none recorded included: no release row offers its decision; review 39 S1). A device waits for a decision when it
has a need a decision answers (rules.support_need decidable: past its support, or an end announced within 12 months), no decision
governing it (rules.governing_decisions: its own, its recorded release's, or the model's), and no update responding to it
(rules.update_responses), as Needs attention's rows read them (rules.needs_attention: a device so answered is never "no decision
recorded"; a temporary device is listed on its own, never under a model's or a release's decision). A fixed number of queries however
many releases, devices, and decisions, none of them reading the devices again (the panel's standings, `read`): the live decisions of the
model (one), and for permissions.can_decide, when the model has no live decision of its own and a device of ours not on a release with
its own decision has a decidable need, the decisions that may govern those devices and their update responses (two).
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


def _waiting(read: dict, decided: set, today) -> list:
    """The standings of `read` (the panel's devices of the model on site: rules.standings) of the devices of ours that wait for a
    decision: a need a decision answers (rules.support_need decidable), no decision governing it (rules.governing_decisions), and no
    update responding to it (rules.update_responses: an open update to a supported release, or one done with the version not
    confirmed), as Needs attention reads them: each is a device it lists under "no decision recorded" (or ending). A device on a release
    in `decided` (one with a live decision of its own) is governed by that or by its own, so it is left out before the read. Two queries
    (the decisions that may govern them, their responses), none when no device of ours is left with a decidable need."""
    candidates = [s for s in read.values() if s.asset.ownership == Ownership.OWNED
                  and not (s.release is not None and s.release.pk in decided)
                  and (need := rules.support_need(s, today)) is not None and need.decidable]
    if not candidates:
        return []
    assets = [s.asset for s in candidates]
    governing = rules.governing_decisions(assets, today)
    responses = rules.update_responses(assets, today)
    return [s for s in candidates if governing.get(s.asset.pk) is None and not responses.get(s.asset.pk)]


def _decisions(request, dm, releases: list, params: dict, read: dict) -> bool:
    """Add each release row's decision (x["decision"]: {"number", "url", "words", "model_wide", "record_url"} or None), and return
    whether the panel offers Record decision for every device of ours of the model (a device of ours waits: _waiting). With the model's
    own live decision nothing waits (it, or one more specific, governs every device of ours on site), and nothing more is read."""
    today = timezone.localdate()
    live = list(DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.SUPPORT_ENDED, ended_on__isnull=True, device_model=dm,
                                                      asset__isnull=True).order_by("number"))
    by_release = {d.release_id: d for d in live if d.release_id}
    model_wide = next((d for d in live if not d.release_id), None)
    waiting =_waiting(read, set(by_release), today) if model_wide is None and cyber_perms.can_decide(request.user) else []
    needing = {s.release.pk for s in waiting if s.release is not None}  # never a release with a decision of its own: it governs them
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
    return bool(waiting)


def annotate(request, dm, releases: list, params: dict, read: dict) -> dict:
    """Add each release row's campaign (x["update"]: {"words", "open_url", "can_open"}) and, for permissions.can_view_devices, its
    security decision (x["decision"], slice 39) to `releases` (views_cybersecurity._panel_context's rows, each {"r": release, "running":
    devices on site on it, ...}) and return what the panel adds to its context: {"can_open_updates", "decide_model"} (whether its footer
    offers Record decision for every device of ours of the model: _decisions). `read` is the panel's standings of the model's devices on
    site (rules.standings; all of them for permissions.can_view_devices), never read again here. Two queries at most for the campaigns,
    however many releases: the campaigns, and (for a user who may open updates) the model's devices on site; slice 39's are _decisions'."""
    can_open = cyber_perms.can_open_updates(request.user)
    if releases:
        counts = rules.campaigns(dm)
        on_site = rules.on_site().filter(device_model=dm).count() if can_open else 0
        for x in releases:
            release = x["r"]
            x["update"] = {"words": rules.campaign_words(release, counts.get(release.pk)) or "", "open_url": open_url(release, params),
                           "can_open": can_open and on_site - x.get("running", 0) > 0}
            x["decision"] = None
    decide_model = False
    if cyber_perms.can_view_devices(request.user) and (releases or cyber_perms.can_decide(request.user)):
        decide_model = _decisions(request, dm, releases, params, read)
    return {"can_open_updates": can_open and bool(releases), "decide_model": decide_model}
