"""
The device security register over the API (slice 37; apps/api/views_cybersecurity.py). Showing only: the API is read only, and every
write goes through apps.cybersecurity.services from the Device security screen and the device drawer's Security tab.

Every fact is read through apps.cybersecurity.rules, as the screen and the drawer read it: a release over its model's profile by group
(support, MDS2, disclosure; the facility's arrangement always the profile's), each group with its source in words ("this release", "the
model's disclosure, version not stated", "not recorded"; the facility's arrangement, never a disclosure, "the model's, for every version":
rules.FACILITY_SOURCE, the drawer's and the screen's words); a device's connection ("Connected (wireless)", "Can connect (wireless), as
disclosed", ...); and the two standing lines, never merged: the manufacturer's security updates (or the model's support, slice 36) and the
OS vendor's published support, each {key, words, day}, judged on the facility's today (read once by the view: context["today"]). Choices go
out as their stored slugs with a "_label" beside each; days as YYYY-MM-DD.

Never nested into serializers.AssetSerializer or DeviceModelSerializer (tests/test_cyber_never.py pins both field sets): a device's facts
are their own endpoint, behind Device security View and Equipment View, never a scoped user's.

- release_data(release, ...): one row of GET software-releases/.
- device_data(standing, ...): one row of GET device-security/ (a rules.DeviceStanding).
- update_data(update): one row of GET software-updates/ (slice 38), as the device's Security tab shows it.
- updates_of(...), work_order_update(...), software_choices(...): a work order's software_update and software_choices
  (serializers.WorkOrderSerializer, slice 38), only for Device security View and Equipment View (shows_updates) and only where
  rules.release_offer offers a choice: never a version, a reference, or a target to anyone else.
"""
from django.urls import reverse
from rest_framework import serializers

from apps.accounts import people
from apps.cybersecurity import rules
from apps.cybersecurity.models import FACILITY_GROUP, GROUPS, Network
from apps.cybersecurity.services import CHOICES
from apps.equipment.models import Ownership
from apps.tenants.context import get_current_tenant


def _day(value):
    return value.isoformat() if value is not None else None


def label(choices, value: str) -> str:
    """A stored choice's words; "" when none is recorded."""
    return choices(value).label if value in choices.values else ""


def _facility_url(path: str, tenant_id) -> str:
    """A page on the web naming its facility (tags and model panels are the facility's own)."""
    tenant = get_current_tenant()
    return people.with_facility(path, tenant) if tenant is not None and tenant.pk == tenant_id else path


def standing_data(st: rules.Standing | None):
    return {"key": st.key, "words": st.words, "day": _day(st.day)} if st is not None else None


def _source(group: str, source: str) -> str:
    """A group's source in words. The facility's arrangement (who installs updates here, the segment) is this facility's own, recorded on
    the model's profile for every version: rules.FACILITY_SOURCE, never "the model's disclosure" (slice 37 review C17/C30)."""
    return rules.FACILITY_SOURCE if group == "facility" and source in (rules.PROFILE, rules.THIS_RELEASE) else source


def _group(source: str, names, values) -> dict:
    out = {"source": source}
    for name in names:
        value = values(name)
        out[name] = _day(value) if name in rules.DATE_FIELDS else value
        if name in CHOICES:
            out[f"{name}_label"] = label(CHOICES[name], value)
    return out


def groups_data(facts: rules.Facts) -> dict:
    """The facts a release or device reads, by group, each with its source: {"support", "mds2", "disclosure", "facility"}."""
    return {group: _group(_source(group, facts.sources[group]), names, facts.get)
            for group, names in (*GROUPS.items(), ("facility", FACILITY_GROUP))}


def own_groups(release) -> dict:
    """A release entered in error as it was recorded (rules read it as nothing): its own fields, by group."""
    def source(names):
        return rules.THIS_RELEASE if any(getattr(release, n) not in (None, "") for n in names) else rules.NOT_RECORDED

    return {group: _group(_source(group, source(names)), names, lambda n: getattr(release, n))
            for group, names in (*GROUPS.items(), ("facility", FACILITY_GROUP))}


def model_data(dm) -> dict:
    return {"id": str(dm.pk), "manufacturer": dm.manufacturer, "model": dm.model, "description": dm.description, "category": dm.category,
            "url": _facility_url(reverse("web:cybersecurity_model", args=[dm.pk]), dm.tenant_id)}


def release_data(release, *, profile, devices: int, today) -> dict:
    """One release (or the model's profile) with its groups as a device on it reads them (the profile merged in by group), its two
    standing lines, and `devices`: the devices on site recorded on it (for a profile, its model's devices on site with no release
    recorded, which read only the profile). One entered in error reads its own fields and no standing (?removed=1 lists it)."""
    removed = release.removed_on is not None
    if removed:
        groups, security, os_line = own_groups(release), None, None
    else:
        facts = rules.facts(None, release) if release.is_profile else rules.facts(release, profile)
        groups = groups_data(facts)
        security, os_line = rules.release_standing(facts, release.device_model, today), rules.os_standing(facts, today)
    return {"id": str(release.pk), "device_model": model_data(release.device_model), "version": release.version,
            "profile": release.is_profile, "groups": groups, "security": standing_data(security), "os": standing_data(os_line),
            "devices": devices, "removed_on": _day(release.removed_on),
            "removed_by": str(release.removed_by) if removed and release.removed_by_id else None}


def device_data(s: rules.DeviceStanding) -> dict:
    """One device on site with what this facility knows about it and the words every door reads."""
    a, dm = s.asset, s.asset.device_model
    return {"id": str(a.pk), "tag": a.tag, "device_model": model_data(dm),
            "department": {"id": str(a.department_id), "name": a.department.name} if a.department_id else None, "room": a.room,
            "ownership": a.ownership, "ownership_label": label(Ownership, a.ownership),
            "release": {"id": str(s.release.pk), "version": s.release.version} if s.release is not None else None,
            "release_since": _day(s.release_since), "release_words": s.release_words,
            "network": s.network, "network_label": label(Network, s.network),
            "connection": {"key": s.connection.key, "words": s.connection.words, "here": s.connection.here},
            "security": standing_data(s.security), "os": standing_data(s.os), "groups": groups_data(s.facts),
            "checked_on": _day(s.checked_on), "checked_by": str(s.checked_by) if s.checked_by is not None else None,
            "url": _facility_url(reverse("web:asset", args=[a.tag]) + "?tab=security", a.tenant_id)}


class SoftwareReleaseSerializer(serializers.BaseSerializer):
    """GET software-releases/: context holds "today", "profiles" ({model id: profile row}) and "devices" ({release id: count})."""

    def to_representation(self, release):
        ctx = self.context
        return release_data(release, profile=ctx["profiles"].get(release.device_model_id), devices=ctx["devices"].get(release.pk, 0),
                            today=ctx["today"])


class DeviceSecuritySerializer(serializers.BaseSerializer):
    """GET device-security/: each a rules.DeviceStanding (rules.standings, a fixed number of queries for a page)."""

    def to_representation(self, standing):
        return device_data(standing)


def release_ref(release):
    """A release an update names: {"id", "version"} (the version as the manufacturer writes it; the words say "(entered in error since)"
    for one entered in error since), or None."""
    return {"id": str(release.pk), "version": release.version} if release is not None else None


def _shows_before(update) -> bool:
    """Whether the API names the update's release_before: something is recorded, and it is not an install of the release it names
    (review round 3: rules._outcome_words leaves that claim out too)."""
    from apps.cybersecurity.models import UpdateOutcome

    if not update.release_before_id or not update.outcome:
        return False
    return not (update.outcome == UpdateOutcome.INSTALLED and update.release_before_id == update.release_after_id)


def update_data(update) -> dict:
    """GET software-updates/ (slice 38): one update as the device's Security tab shows it, read with its device, work order, releases, and who
    recorded it (the viewset's select_related: no query). Every word is the rules' (rules.update_state, STATE_LABELS, update_words): id;
    device {id, tag}; work_order {id, number} (an install recorded by hand has none: null; never a link, which would need Work orders
    View); target ("release", "patches", "" for a hand row), target_release {id, version} or null, target_reference (a token); state
    (open, done, not_confirmed, cancelled) and state_label; outcome ("installed", "patches", "already", "" while nothing is recorded)
    and outcome_label; words (the sentence the tab shows); applied_on; release_before and release_after ({id, version} or null;
    release_before null while nothing is recorded (a work order's outcome entered in error keeps what was taken before for its next
    recording, review round 2, but shows none), and null on an install of the release it names (review round 3: stored as taken, the
    claim left out, as the words leave it out)); reference (a token); basis and basis_label; installed_by and installed_by_label (a hand
    row's); recorded_by (a name, null when none or the system); later_kept (the device's facts were newer: kept, not written); opened_on
    (a work order's)."""
    from apps.cybersecurity.models import InstalledBy, UpdateBasis, UpdateOutcome

    wo = update.work_order if update.work_order_id else None
    state = rules.update_state(update)
    return {"id": str(update.pk), "device": {"id": str(update.asset_id), "tag": update.asset.tag},
            "work_order": {"id": str(wo.pk), "number": wo.number} if wo is not None else None,
            "target": update.target, "target_release": release_ref(update.target_release if update.target_release_id else None),
            "target_reference": update.target_reference, "state": state, "state_label": rules.STATE_LABELS[state],
            "outcome": update.outcome, "outcome_label": label(UpdateOutcome, update.outcome), "words": rules.update_words(update),
            "applied_on": _day(update.applied_on),
            "release_before": release_ref(update.release_before if _shows_before(update) else None),
            "release_after": release_ref(update.release_after if update.release_after_id else None),
            "reference": update.reference, "basis": update.basis, "basis_label": label(UpdateBasis, update.basis),
            "installed_by": update.installed_by, "installed_by_label": label(InstalledBy, update.installed_by),
            "recorded_by": str(update.recorded_by) if update.recorded_by_id else None, "later_kept": update.later_kept,
            "opened_on": _day(update.opened_on)}


class SoftwareUpdateSerializer(serializers.BaseSerializer):
    """GET software-updates/: each a SoftwareUpdate, read with its device, work order, releases, and recorder (the viewset's
    select_related)."""

    def to_representation(self, update):
        return update_data(update)


# --- a work order's software update (serializers.WorkOrderSerializer: software_update and software_choices) ------------------------

NOT_CONFIRMED_LABEL = "Not confirmed now"  # the completion's fourth choice (updates.NOT_CONFIRMED), as the modal's radio reads


def shows_updates(user) -> bool:
    """Whether `user` reads what a software update work order is to install and what it did: Device security View and Equipment
    View, never a scoped user (permissions.can_view_devices, the Security tab's door)."""
    from apps.cybersecurity import permissions as perms

    return user is not None and getattr(user, "is_authenticated", False) and perms.can_view_devices(user)


def updates_of(work_orders, user) -> dict | None:
    """{work order id: its SoftwareUpdate, or None} for the software update work orders among `work_orders` (None: one with no record,
    imported from another system), each with its target and releases read and its work order set to the one given (its words read it):
    one query for a page, none without an update work order; None when `user` does not read them (no query)."""
    from apps.cybersecurity.models import SoftwareUpdate
    from apps.workorders.models import WoType

    if not shows_updates(user):
        return None
    rows = {w.pk: w for w in work_orders if w.type == WoType.SOFTWARE_UPDATE}
    if not rows:
        return {}
    found = dict.fromkeys(rows)
    for update in (SoftwareUpdate.objects.filter(work_order_id__in=list(rows))
                   .select_related("target_release", "release_before", "release_after")):
        update.work_order = rows[update.work_order_id]
        found[update.work_order_id] = update
    return found


def work_order_update(update) -> dict:
    """A work order's software_update: what it is to install (target, target_release {id, version} or null, target_reference), its state
    and the sentence (rules.update_state, update_words), and once recorded what it did (outcome and outcome_label, applied_on,
    release_after {id, version} or null)."""
    from apps.cybersecurity.models import UpdateOutcome

    return {"target": update.target, "target_release": release_ref(update.target_release if update.target_release_id else None),
            "target_reference": update.target_reference, "state": rules.update_state(update), "outcome": update.outcome,
            "outcome_label": label(UpdateOutcome, update.outcome), "words": rules.update_words(update),
            "applied_on": _day(update.applied_on),
            "release_after": release_ref(update.release_after if update.release_after_id else None)}


def software_choices(offer) -> dict:
    """A work order's software_choices from rules.release_offer's Offer: what its completion may send. needed (true while nothing is
    recorded: software_outcome must then be sent); the target (target, target_release {id, version} or null, target_reference) and
    target_words (the sentence: "Open: to install 5.6 (demo)"); outcomes [{value, label}] (installed, patches, already, then
    not_confirmed: nothing recorded now); releases [{id, version, recorded}] (the model's, in version order, the one the device's record
    names marked recorded); bases {outcome: [{value, label}]} (the basis each outcome may name: already none)."""
    from apps.cybersecurity.models import UpdateBasis, UpdateOutcome
    from apps.cybersecurity.updates import NOT_CONFIRMED

    recorded = offer.recorded.pk if offer.recorded is not None else None
    return {"needed": offer.needed, "target": offer.target, "target_release": release_ref(offer.target_release),
            "target_reference": offer.target_reference, "target_words": rules.update_words(offer.update),
            "outcomes": [*({"value": str(o), "label": UpdateOutcome(o).label} for o in offer.outcomes),
                         {"value": NOT_CONFIRMED, "label": NOT_CONFIRMED_LABEL}],
            "releases": [{"id": str(r.pk), "version": r.version, "recorded": r.pk == recorded} for r in offer.releases],
            "bases": {str(o): [{"value": str(b), "label": UpdateBasis(b).label} for b in offer.bases.get(o, ())] for o in offer.outcomes}}
