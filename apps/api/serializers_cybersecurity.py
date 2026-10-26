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
- decision_data(decision, ...) with rules.decisions_now(decisions, today): one row of GET security-decisions/ (slice 39), every word the rules'
  (decision_words, scope_words, plan_words, control_words, reason_words, review_words, decision_state, the Check lines); slice 40, an
  advisory decision's advisory {id, number}.
- advisory_data(advisory, ...) with rules.advisory_counts(advisories, ...): one row of GET security-advisories/ (slice 40), its sources,
  the models it names with their counts split by reader (counts_data: line kinds and update counts at Device security View, answer kinds
  only for a reader of devices), and on retrieve its device lines (advisory_line_data, a reader of devices only).
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


def update_data(update) -> dict:
    """GET software-updates/ (slice 38): one update as the device's Security tab shows it, read with its device, work order, releases, and who
    recorded it (the viewset's select_related: no query). Every word is the rules' (rules.update_state, STATE_LABELS, update_words): id;
    device {id, tag}; work_order {id, number} (an install recorded by hand has none: null; never a link, which would need Work orders
    View); target ("release", "patches", "" for a hand row), target_release {id, version} or null, target_reference (a token); state
    (open, done, not_confirmed, cancelled) and state_label; outcome ("installed", "patches", "already", "" while nothing is recorded)
    and outcome_label; words (the sentence the tab shows); applied_on; release_after ({id, version} or null: what the person recording it
    says it runs after, patches the device's recorded release then; never what it ran before, review round 4: the device's History shows
    that); reference (a token); basis and basis_label; installed_by and installed_by_label (a hand row's); recorded_by (a name, null when
    none or the system); later_kept (the device's facts were newer: kept, not written); opened_on (a work order's)."""
    from apps.cybersecurity.models import InstalledBy, UpdateBasis, UpdateOutcome

    wo = update.work_order if update.work_order_id else None
    state = rules.update_state(update)
    return {"id": str(update.pk), "device": {"id": str(update.asset_id), "tag": update.asset.tag},
            "work_order": {"id": str(wo.pk), "number": wo.number} if wo is not None else None,
            "target": update.target, "target_release": release_ref(update.target_release if update.target_release_id else None),
            "target_reference": update.target_reference, "state": state, "state_label": rules.STATE_LABELS[state],
            "outcome": update.outcome, "outcome_label": label(UpdateOutcome, update.outcome), "words": rules.update_words(update),
            "applied_on": _day(update.applied_on),
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
                   .select_related("target_release", "release_after")):
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


# --- slice 39: security decisions (GET security-decisions/) ------------------------------------------------------------------------

def _superseded_by(decision):
    """The decision that superseded this one (the reverse one-to-one, read with the row), or None."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return decision.superseded_by
    except ObjectDoesNotExist:
        return None


def decision_data(decision, *, today, fy_start=None, governs=(), checks=()) -> dict:
    """GET security-decisions/ (slice 39): one decision for devices past support or, slice 40, for the devices a security advisory names,
    read with its device, model, release, plan owner, recorder, advisory, and the decisions it supersedes and was superseded by (the
    viewset's select_related: no query), every word the rules':
    id, number; applies_to and its label; advisory {id, number} (an advisory decision's; null for one for devices past support: its
    scope words stay the scope's, the advisory named here); scope {kind (device, release, model), label, words (rules.scope_words), asset {id, tag} or
    null, device_model {id, manufacturer, model} or null, release {id, version} or null} (the stored shape: a device's own decision names
    no model); reasons (kinds, rules.REASONS) and reason_words; response and response_label; controls (slugs, rules.CONTROLS) and
    control_words (IT's reference beside recorded_elsewhere); controls_reference; plan, plan_label, plan_by, plan_words ("Plan: replace,
    planned for FY2028": the fiscal year read now, never stored); plan_owner (a name, null when none); accepted_by and accepted_by_label;
    exception_reference (IT's number: IT's record is the system of record); recorded_by (a name, null for none or the system); decided_on,
    review_by; state (rules.decision_state: in_force, review_soon, review_due, ended) and state_label; review_words ("Review due since
    ...", or how it ended); ended_on, end_reason and end_reason_label; supersedes and superseded_by (numbers, or null); sentence
    (rules.decision_words); governed_ids (the devices it governed when recorded, ids); governs [{id, tag}] (the devices on site it
    governs now); checks [{key, words, info, action, tags}] (rules.decision_checks: information, never a refusal). `fy_start`: the
    facility's fiscal year start, read once for a page (else read here for a replacement planned); `governs` and `checks` from
    rules.decisions_now."""
    from apps.cybersecurity.models import AcceptedBy, AppliesTo, DecisionEnd, DecisionPlan, DecisionResponse

    kind = rules.scope_kind(decision)
    dm = decision.device_model if decision.device_model_id else None
    state = rules.decision_state(decision, today)
    later = _superseded_by(decision)
    return {"id": str(decision.pk), "number": decision.number,
            "applies_to": decision.applies_to, "applies_to_label": label(AppliesTo, decision.applies_to),
            "advisory": {"id": str(decision.advisory_id), "number": decision.advisory.number} if decision.advisory_id else None,
            "scope": {"kind": kind, "label": rules.SCOPE_LABELS[kind], "words": rules.scope_words(decision),
                      "asset": {"id": str(decision.asset_id), "tag": decision.asset.tag} if decision.asset_id else None,
                      "device_model": {"id": str(dm.pk), "manufacturer": dm.manufacturer, "model": dm.model} if dm is not None else None,
                      "release": release_ref(decision.release if decision.release_id else None)},
            "reasons": [k for k in rules.REASONS if k in (decision.reasons or ())], "reason_words": rules.reason_words(decision),
            "response": decision.response, "response_label": label(DecisionResponse, decision.response),
            "controls": list(decision.controls or ()), "control_words": rules.control_words(decision),
            "controls_reference": decision.controls_reference,
            "plan": decision.plan, "plan_label": label(DecisionPlan, decision.plan), "plan_by": _day(decision.plan_by),
            "plan_words": rules.plan_words(decision, fy_start=fy_start),
            "plan_owner": str(decision.plan_owner) if decision.plan_owner_id else None,
            "accepted_by": decision.accepted_by, "accepted_by_label": label(AcceptedBy, decision.accepted_by),
            "exception_reference": decision.exception_reference,
            "recorded_by": str(decision.recorded_by) if decision.recorded_by_id else None,
            "decided_on": _day(decision.decided_on), "review_by": _day(decision.review_by),
            "state": state, "state_label": rules.DECISION_STATE_WORDS[state], "review_words": rules.review_words(decision, today),
            "ended_on": _day(decision.ended_on), "end_reason": decision.end_reason,
            "end_reason_label": label(DecisionEnd, decision.end_reason),
            "supersedes": decision.supersedes.number if decision.supersedes_id else None,
            "superseded_by": later.number if later is not None else None,
            "sentence": rules.decision_words(decision, today, fy_start=fy_start),
            "governed_ids": [str(pk) for pk in decision.governed_ids or ()],
            "governs": [{"id": str(a.pk), "tag": a.tag} for a in governs],
            "checks": [{"key": c.key, "words": c.words, "info": c.info, "action": c.action, "tags": list(c.tags)} for c in checks]}


class SecurityDecisionSerializer(serializers.BaseSerializer):
    """GET security-decisions/: context holds "today", "fy_start", and "now" (rules.decisions_now's result for the page)."""

    def to_representation(self, decision):
        ctx = self.context
        governs, checks = ctx["now"].get(decision.pk, ((), ()))
        return decision_data(decision, today=ctx["today"], fy_start=ctx.get("fy_start"), governs=governs, checks=checks)


# --- slice 40: security advisories (GET security-advisories/) ------------------------------------------------------------------------

UPDATE_KINDS = (rules.UPDATE_OPEN, rules.UPDATE_NOT_CONFIRMED)  # the update counts every Device security reader sees


def _name(user):
    return str(user) if user is not None else None


def _kinds(found: dict, labels: dict, order) -> list:
    """Counts as [{kind, label, devices}] in the rules' order: the slug, its words (rules.LINE_KINDS, ANSWER_KINDS), the devices."""
    return [{"kind": kind, "label": labels[kind], "devices": found.get(kind, 0)} for kind in order]


def counts_data(lines: dict, updates: dict, answers: dict | None) -> dict:
    """A model row's or an advisory's counts, split by reader (build40 section 3; critique40_rules S2): lines (devices per line kind) and
    updates (devices with an answering update open, or done with the version not confirmed) for Device security View; answers (devices per
    answer kind, the first that holds: decided, past review, no response recorded, ...) only for a reader of devices, else null."""
    return {"lines": _kinds(lines, rules.LINE_KINDS, rules.LINE_KINDS),
            "updates": _kinds(updates, rules.ANSWER_KINDS, UPDATE_KINDS),
            "answers": _kinds(answers, rules.ANSWER_KINDS, rules.ANSWER_KINDS) if answers is not None else None}


def advisory_source_data(source) -> dict:
    """One live source of an advisory: who published it (a choice), its reference (a token), its link (https, a pointer never fetched),
    its days, its revision, and what its last revision changed (AdvisoryChange slugs, with their words)."""
    from apps.cybersecurity.models import AdvisoryChange, AdvisorySourceKind

    changes = [c for c in AdvisoryChange.values if c in (source.last_change or ())]
    return {"id": str(source.pk), "source": source.source, "source_label": label(AdvisorySourceKind, source.source),
            "reference": source.reference, "url": source.url, "published_on": _day(source.published_on),
            "updated_on": _day(source.updated_on), "revision": source.revision, "last_change": changes,
            "last_change_words": [AdvisoryChange(c).label for c in changes]}


def advisory_model_data(row, counts) -> dict:
    """One model an advisory names (rules.rows_of's row: its model, fix, and listed releases read): which releases (versions and its
    label; releases [{id, version}], the listed ones), what the advisory offers (remedy and its label), the fix release {id, version} or
    null, the patch list (a token), and its counts (counts_data; null for an advisory entered in error, which reads no row)."""
    from apps.cybersecurity.models import AdvisoryRemedy, AdvisoryVersions

    return {"id": str(row.pk), "device_model": model_data(row.device_model), "versions": row.versions,
            "versions_label": label(AdvisoryVersions, row.versions), "releases": [release_ref(r) for r in row.listed],
            "remedy": row.remedy, "remedy_label": label(AdvisoryRemedy, row.remedy),
            "fix_release": release_ref(row.fix_release if row.fix_release_id else None), "patches_reference": row.patches_reference,
            "counts": counts_data(counts.lines, counts.updates, counts.answers) if counts is not None else None}


def advisory_line_data(line, answer) -> dict:
    """One device under a model row (a rules.AdvisoryLine, read for a reader of devices only): its model row, the device {id, tag},
    serial, department, ownership, connection {key, words, here}, recorded release {id, version} or null, the line's kind and label
    (rules.LINE_KINDS) and words, whether it needs a response, and what answers it (`answer`, rules.LineAnswer, or None for an advisory
    with no needs: not applicable): answer and answer_label (the first that holds, rules.ANSWER_KINDS; "" when it needs none), responses
    [{kind, words, number}] (updates open, versions not confirmed, the manufacturer's patches recorded, the governing decision's sentence:
    the rules' words; a work order's or decision's number, never a link), and the governing decision {id, number} or null and whether
    it covers the line now; url its Security tab on the web."""
    a, standing = line.asset, line.standing
    kind = answer.kind if answer is not None else ""
    decision = answer.decision if answer is not None else None
    return {"model_row": str(line.row.pk), "asset": {"id": str(a.pk), "tag": a.tag}, "serial": a.serial,
            "department": a.department.name if a.department_id else None, "ownership": a.ownership,
            "ownership_label": label(Ownership, a.ownership),
            "connection": {"key": standing.connection.key, "words": standing.connection.words, "here": standing.connection.here},
            "release": release_ref(line.release), "line": line.kind, "line_label": rules.LINE_KINDS[line.kind], "words": line.words,
            "needs_response": line.needs_response, "answer": kind, "answer_label": rules.ANSWER_KINDS.get(kind, ""),
            "responses": [{"kind": r.kind, "words": r.words, "number": r.number or None} for r in (answer.responses if answer else ())],
            "decision": {"id": str(decision.pk), "number": decision.number} if decision is not None else None,
            "covers": bool(answer.covers) if answer is not None else False,
            "url": _facility_url(reverse("web:asset", args=[a.tag]) + "?tab=security", a.tenant_id)}


def advisory_data(advisory, *, counts, rows, sources, devices_visible: bool, lines=None, answers=None) -> dict:
    """GET security-advisories/ (slice 40): one advisory as the Advisories tab reads it, every word the rules': id, number, name
    (rules.advisory_name: number, its first live source's reference, the models it names; no title is kept); status and status_label
    (the stored choice) and status_words (rules.status_words for this reader: a Device security View reader never reads a word derived from
    a decision); received_on, recorded_by; cves (CVE IDs as recorded); models_confirmed_on and models_confirmed_by (null: models to
    confirm); status_on and status_by (the last move out of open); not_applicable_reason and its label; changed_on; removed_on and
    removed_by (entered in error); sources [advisory_source_data] (live ones, oldest first); models [advisory_model_data] (its rows, as
    recorded); counts (its rows' totals, counts_data) with reviews_due and named_since (decisions past review governing a line, devices
    named since it left open: for a reader of devices, else null; counts null for one entered in error); url its page on the web.
    `counts` advisory_counts' AdvisoryCounts (None for one entered in error), `rows` its rows (rules.rows_of's), `sources` its sources
    (every one, oldest first: the name reads the first live one). `lines` (retrieve, a reader of devices): its AdvisoryLines, and
    `answers` the reading's {(asset id, row id): LineAnswer}; then devices [advisory_line_data] (null for a reader without them)."""
    from apps.cybersecurity.models import AdvisoryStatus, NotApplicableReason

    live = counts is not None and advisory.removed_on is None
    per_row = {c.row.pk: c for c in counts.rows} if live else {}
    total = None
    if live:
        total = {**counts_data(counts.lines, counts.updates, counts.answers),
                 "reviews_due": counts.reviews_due if devices_visible else None,
                 "named_since": counts.named_since if devices_visible else None}
    words = counts.words if counts is not None else rules.status_words(advisory, devices_visible=False)
    out = {"id": str(advisory.pk), "number": advisory.number, "name": rules.advisory_name(advisory, rows=rows, sources=sources),
           "status": advisory.status, "status_label": label(AdvisoryStatus, advisory.status), "status_words": words,
           "received_on": _day(advisory.received_on), "recorded_by": _name(advisory.recorded_by), "cves": list(advisory.cves or ()),
           "models_confirmed_on": _day(advisory.models_confirmed_on), "models_confirmed_by": _name(advisory.models_confirmed_by),
           "status_on": _day(advisory.status_on), "status_by": _name(advisory.status_by),
           "not_applicable_reason": advisory.not_applicable_reason,
           "not_applicable_reason_label": label(NotApplicableReason, advisory.not_applicable_reason),
           "changed_on": _day(advisory.changed_on), "removed_on": _day(advisory.removed_on), "removed_by": _name(advisory.removed_by),
           "sources": [advisory_source_data(s) for s in sources if s.removed_on is None],
           "models": [advisory_model_data(r, per_row.get(r.pk)) for r in rows],
           "counts": total,
           "url": _facility_url(reverse("web:security_advisory", args=[advisory.number]), advisory.tenant_id)}
    if lines is not None:
        out["devices"] = ([advisory_line_data(ln, (answers or {}).get((ln.asset.pk, ln.row.pk))) for ln in lines]
                          if devices_visible else None)
    return out


class SecurityAdvisorySerializer(serializers.BaseSerializer):
    """GET security-advisories/: context holds "counts" ({advisory id: AdvisoryCounts}, rules.advisory_counts for the page), "rows"
    ({advisory id: [rows]}), "sources" ({advisory id: [sources]}), and "devices_visible" (permissions.can_view_devices)."""

    def to_representation(self, advisory):
        ctx = self.context
        return advisory_data(advisory, counts=ctx["counts"].get(advisory.pk), rows=ctx["rows"].get(advisory.pk, []),
                             sources=ctx["sources"].get(advisory.pk, []), devices_visible=ctx["devices_visible"])
