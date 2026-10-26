"""
The device drawer's Security tab (slice 37, beyond the mock): what this facility knows about one device's software and connection, and
what its model's manufacturer discloses, read through apps.cybersecurity.rules (never the fields directly, so the words and the group
fallback are the screen's and the API's). views.asset_drawer_context merges `tab_context` under "cyber" when the tab is open.

Who: Device security View and Equipment View (cybersecurity.permissions.can_view_devices), never a scoped user: the drawer offers the tab
only to them (a scoped user, or a role without both, asking for ?tab=security gets the Overview). Record facts (Device security Edit and
Equipment View: can_edit_devices) opens views_cyber_facts.record_facts in the modal; never on a retired device or one returned to its
owner (its facts stay as they were, with its history).

- The device's lines (rules.device_standing): its release and since when, or "Release not recorded" (its model's profile is read as the
  model's then); connected here, from the device's own fact or "as disclosed" by its model; the two standing lines, separate, never
  merged (the manufacturer's security updates, or the model's support from slice 36 with no date of the release's own; the OS vendor's
  published support); checked on and by.
- The release card: the facts the device reads, group by group (support, the MDS2, what the manufacturer discloses, and the facility's
  arrangement), each with its source ("this release", "the model's disclosure, version not stated", or "not recorded"). The MDS2's
  answers are named by their questions in words, never by a form's question ID; the MDS2 sentence (rules.MDS2_SENTENCE) under it. A
  model whose MDS2 is on file only for other releases says which ("The model's MDS2 on file for: 9.19.2").
- Its own history: the device's DeviceSecurity row's changes (the device_security area, apps.web.history_tabs), its Show older served by
  the Record facts URL (?history=1&older=), since the drawer's own ?older= is the device's History tab's. A device-scope security
  decision's changes are not in it (slice 39: history_tabs.RELATED keys a related area by the record's own id, and this record is the
  DeviceSecurity row, not the device): the decision's page has its own History, linked from the tab.
- Slice 39, the device's support decision (rules.device_lines; `decision_lines`, web/_cyber_decision_device.html): its support need
  (past the manufacturer's or the OS vendor's support; or, as information, an end announced within 12 months, or the OS vendor's support
  ended while the manufacturer's security updates continue), its response lines (an update open to a supported release, and one done with
  the version not confirmed with Confirm version, each work order's number a link with Work orders View; the governing decision's sentence
  with its number linking to its page and its state; or "No decision recorded"), and, for permissions.can_decide with a need a decision
  answers, Record decision for this device (Review and renew when its own decision governs it).

- Slice 40, the advisories naming the device's model (`advisory_lines`, web/_cyber_advisory_device.html, under its own "Advisories"
  after the support decision; build40 section 7.8, critique40_scope N5): each advisory listed when it is open, when this device needs a
  response under it (its line names the device and no patches record answers it), when a live decision of it governs the device, or,
  marked not applicable, when the device is named since (its reason no longer holds for it: rules.not_applicable_blocks); with its name
  (a link to its page), its status words (rules.advisory_counts'), its CVE IDs (three, then "and N more"), the device's line, its
  responses (updates and patches recorded, each work order's number a link with Work orders View; the governing decision's number, a link,
  its state, and its sentence; or "No response recorded"); Record decision for this device under it (permissions.can_decide, while it
  needs a response and no decision covers it; Review and renew for its own device decision past review), Open software update with the
  advisory (permissions.can_open_updates, a release or patches remedy, no update open, not held as evidence), Reopen it for a
  not-applicable one where the device is named since (Device security Edit, advisories.reopen_refusal). The rest are counted ("2 other
  advisories name this model: closed or not applicable, with nothing listed for this device."); none: rules.DEVICE_ADVISORIES_EMPTY. A
  fixed number of queries however many advisories, rows, devices, and decisions.

Words: never the ones rules.NEVER_WORDS lists (tests/test_cyber_never.py scans the tab with it); the decision's lines never the ones
rules.DECISION_NEVER_WORDS lists either (tests/test_cyber_decisions_web.py).
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from apps.accounts.models import Level, Module
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules
from apps.cybersecurity.models import (
    Connects,
    Disclosed,
    Mds2Edition,
    Mds2Status,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    Segmented,
    UpdatesBy,
)
from apps.equipment.models import AssetStatus
from apps.equipment.services import owner_maintains

from . import asset_cyber_updates, history_tabs

AREA = "device_security"
TAB = "security"
NOT_RECORDED = "Not recorded"
GROUP_TITLES = {"support": "Support and security updates", "mds2": "The manufacturer's disclosure (MDS2)",
                "disclosure": "What the manufacturer discloses", "facility": "This facility's arrangement"}
# The MDS2-style answers, each named by its question in words (never a form's question ID: a 2013 or 2008 form numbers them otherwise)
QUESTIONS = (
    ("patient_data", PatientData, rules.QUESTIONS["patient_data"]),  # rules.QUESTIONS: the screen's words too
    ("connects", Connects, rules.QUESTIONS["connects"]),
    ("remote_service", Disclosed, rules.QUESTIONS["remote_service"]),
    ("patching", Patching, rules.QUESTIONS["patching"]),
    ("sbom", Sbom, rules.QUESTIONS["sbom"]),
)
SOURCE_WORDS = {rules.THIS_RELEASE: "this release", rules.PROFILE: rules.PROFILE, rules.NOT_RECORDED: "not recorded"}
FACILITY_SOURCE = rules.FACILITY_SOURCE  # the facility group's source: one wording on every door (the panel, the API)


def _label(choices, value) -> str:
    return choices(value).label if value in choices.values else NOT_RECORDED


def _day(value) -> str:
    return rules.day_words(value) if value is not None else NOT_RECORDED


def _os_words(f: rules.Facts) -> str:
    family, name = f.get("os_family"), f.get("os_name")
    if not family:
        return name or NOT_RECORDED
    if family == OsFamily.NONE:
        return OsFamily.NONE.label
    return f"{OsFamily(family).label}: {name}" if name else OsFamily(family).label


def _row(label: str, words: str, *, wide: bool = False, url: str = "") -> dict:
    return {"label": label, "words": words, "wide": wide, "url": url}


def _mds2_rows(f: rules.Facts, others: tuple) -> list[dict]:
    """The MDS2 group's rows; `others`: the versions of this model with an MDS2 on file, when the device reads none of its own."""
    status = f.get("mds2")
    rows = [_row("MDS2", _label(Mds2Status, status))]
    if status == Mds2Status.ON_FILE:
        rows += [_row("Form", _label(Mds2Edition, f.get("mds2_edition"))), _row("Document", f.get("mds2_document") or NOT_RECORDED),
                 _row("Released", _day(f.get("mds2_released_on")))]
        url = (f.get("mds2_url") or "").strip()
        if url.lower().startswith("https://"):  # the service takes https links only; anything else never becomes a link
            rows.append(_row("Link", "The manufacturer's page", url=url))
    elif status == Mds2Status.REQUESTED:
        rows.append(_row("Requested", _day(f.get("mds2_requested_on"))))
    if others:
        rows.append(_row("The model's MDS2 on file for", ", ".join(others), wide=True))
    return rows


def release_card(f: rules.Facts, others: tuple = ()) -> list[dict]:
    """The facts a device reads, group by group: [{"key", "title", "source", "rows": [{"label", "words", "wide", "url"}]}]."""
    groups = {
        "support": [_row("Operating system", _os_words(f)),
                    _row("Last day of the OS vendor's published support", _day(f.get("os_support_ends_on"))),
                    _row("Last day of the manufacturer's security updates", _day(f.get("security_updates_end_on"))),
                    _row("Support checked", _day(f.get("support_checked_on")))],
        "mds2": _mds2_rows(f, others),
        "disclosure": [_row(question, _label(choices, f.get(name)), wide=True) for name, choices, question in QUESTIONS],
        "facility": [_row(rules.QUESTIONS["updates_by"], _label(UpdatesBy, f.get("updates_by"))),
                     _row(rules.QUESTIONS["segmented"], _label(Segmented, f.get("segmented")))],
    }
    return [{"key": key, "title": GROUP_TITLES[key], "source": _source(key, f.sources.get(key)), "rows": rows} for key, rows in groups.items()]


def _source(group: str, source: str | None) -> str:
    """A group's source in words. The facility's arrangement is recorded on the model's profile, for every version (not a disclosure)."""
    if group == "facility" and source == rules.PROFILE:
        return FACILITY_SOURCE
    return SOURCE_WORDS.get(source, "not recorded")


def _others(asset, s: rules.DeviceStanding) -> tuple:
    """The versions of the device's model with an MDS2 on file, when the device reads no MDS2 of its own release (one query)."""
    if s.facts.sources.get("mds2") == rules.THIS_RELEASE:
        return ()
    return tuple(r.version for r in rules.releases(asset.device_model_id)
                 if r.mds2 == Mds2Status.ON_FILE and (s.release is None or r.pk != s.release.pk))


def _checked(s: rules.DeviceStanding) -> str:
    if s.checked_on is None:
        return "Not checked yet"
    return f"{rules.day_words(s.checked_on)} by {s.checked_by}" if s.checked_by is not None else rules.day_words(s.checked_on)


def gone_words(asset) -> str:
    """Why Record facts is not offered: a retired device (or a temporary one returned to its owner) keeps its facts as they were."""
    if asset.status != AssetStatus.RETIRED:
        return ""
    return "Returned to its owner: its facts stay as they were." if owner_maintains(asset) else "Retired: its facts stay as they were."


def facts_url(asset) -> str:
    return reverse("web:cybersecurity_facts", args=[asset.tag])


NEED_TONES = {rules.NEEDS: "warn", rules.ENDING: "info", rules.EXTENDED: "neutral"}


def decision_url(number: str) -> str:
    return reverse("web:security_decision", args=[number])


def record_decision_url(**query) -> str:
    """Record decision's address (views_cyber_decisions.decision_new) for this device's scope or its renewal, answering here."""
    return f"{reverse('web:security_decision_new')}?{urlencode({**query, 'from': 'device'})}"


def _split(words: str, number: str, url: str) -> dict:
    """A response line split around the number it names, so the number can be a link: {"before", "number", "after", "url"}."""
    if number and number in words:
        before, after = words.split(number, 1)
        return {"before": before, "number": number, "after": after, "url": url}
    return {"before": words, "number": "", "after": "", "url": ""}


def decision_lines(request, asset, dl: rules.DeviceLines, today) -> dict | None:
    """The Security tab's support decision (slice 39) from rules.device_lines, or None when the device has no need and no decision
    governs it: {"need", "need_tone", "rows" (each {"kind", "line" split around its number, "state", "tone", "confirm_url"}),
    "record_url", "renew_url"}. No query beyond device_lines'."""
    if dl.need is None and dl.decision is None:
        return None
    can_confirm = cyber_perms.can_edit_devices(request.user) and asset.status != AssetStatus.RETIRED
    rows = []
    for r in dl.responses:
        row = {"kind": r.kind, "line": _split(r.words, "", ""), "state": "", "tone": "", "confirm_url": ""}
        if r.kind in (rules.UPDATE_OPEN, rules.UPDATE_NOT_CONFIRMED):
            row["line"] = _split(r.words, r.number, reverse("web:wo", args=[r.number]) if dl.link_work_orders else "")
            if r.kind == rules.UPDATE_NOT_CONFIRMED and can_confirm and r.update is not None:
                row["confirm_url"] = reverse("web:cybersecurity_update_confirm", args=[r.update.pk])
        elif r.kind == rules.DECIDED:
            row.update(line={"before": "", "number": r.number, "after": f" · {r.words}", "url": decision_url(r.number)},
                       state=rules.DECISION_STATE_WORDS[rules.decision_state(r.decision, today)], tone="ok" if r.covers else "warn")
        rows.append(row)
    own = dl.decision is not None and rules.scope_kind(dl.decision) == rules.DEVICE_SCOPE
    return {"need": dl.need, "need_tone": NEED_TONES.get(dl.need.key, "neutral") if dl.need is not None else "", "rows": rows,
            "record_url": record_decision_url(asset=asset.tag) if dl.can_record and not own else "",
            "renew_url": record_decision_url(supersedes=dl.decision.number) if dl.can_record and own else ""}


# --- slice 40: the advisories naming the device's model ------------------------------------------------------------------------------

CVES_SHOWN = 3
REST_WORDS = "{count} {other}{noun} this model: closed or not applicable, with nothing listed for this device."
UPDATE_REMEDIES = ("release", "patches")  # models.AdvisoryRemedy: what an update from the advisory can install
OPEN_KINDS = (rules.UPDATE_OPEN, rules.UPDATE_ELSEWHERE)  # an update open on the device, answering the line or not
_ADVISORY_DECISION_RELATED = ("asset", "device_model", "release", "plan_owner", "recorded_by", "advisory")


def cves_words(cves) -> str:
    """ "CVE-YYYY-NNNN, CVE-YYYY-NNNN, CVE-YYYY-NNNN and 2 more" (the first three), or rules.NO_CVES_WORDS."""
    cves = list(cves or ())
    if not cves:
        return rules.NO_CVES_WORDS
    more = len(cves) - CVES_SHOWN
    return ", ".join(cves[:CVES_SHOWN]) + (f" and {more} more" if more > 0 else "")


def rest_words(count: int, *, listed: bool) -> str:
    """ "2 other advisories name this model: closed or not applicable, with nothing listed for this device." ("1 advisory names ..."
    when none is listed above it), or "" for none."""
    if not count:
        return ""
    return REST_WORDS.format(count=count, other="other " if listed else "", noun="advisory names" if count == 1 else "advisories name")


def _decision_row(number: str, words: str, decision, covering: bool, today) -> dict:
    return {"kind": rules.ANSWER_DECIDED if covering else rules.ANSWER_REVIEW_DUE,
            "line": {"before": "", "number": number, "after": f" · {words}", "url": decision_url(number)},
            "state": rules.DECISION_STATE_WORDS[rules.decision_state(decision, today)], "tone": "ok" if covering else "warn"}


def _response_rows(answer, governing, today, *, can_wo: bool, fy) -> list:
    """The device's responses under one advisory: an answer's (rules.advisory_answers: its updates and patches recorded, each work
    order's number a link with Work orders View; the governing decision with its state and sentence; or "No response recorded"); for a
    line that needs no response, the decision that governs the device still, if one does (its sentence)."""
    rows = []
    for r in (answer.responses if answer is not None else ()):
        if r.kind in (rules.ANSWER_DECIDED, rules.ANSWER_REVIEW_DUE):
            rows.append(_decision_row(r.number, r.words, r.decision, r.covers, today))
        else:
            url = reverse("web:wo", args=[r.number]) if can_wo and r.number else ""
            rows.append({"kind": r.kind, "line": _split(r.words, r.number, url), "state": "", "tone": ""})
    if governing is not None and not any(r["kind"] in (rules.ANSWER_DECIDED, rules.ANSWER_REVIEW_DUE) for r in rows):
        covering = rules.covers(governing, today)
        rows.append(_decision_row(governing.number, rules.decision_words(governing, today, fy_start=fy()), governing, covering, today))
    return rows


def _governing(asset, advisories, today) -> dict:
    """{advisory id: the live decision of that advisory governing the device} (rules.decisions_now over the live advisory decisions
    whose scope may hold it: its own, or its model's release and model scopes; the most specific of each advisory's key). Fixed
    queries: the decisions (one), decisions_now's (none without one)."""
    from apps.cybersecurity.models import AppliesTo, DeviceSecurityDecision

    found = list(DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.ADVISORY, ended_on__isnull=True,
                                                       advisory_id__in=[a.pk for a in advisories])
                 .filter(Q(asset_id=asset.pk) | Q(asset__isnull=True, device_model_id=asset.device_model_id))
                 .select_related(*_ADVISORY_DECISION_RELATED).order_by("number"))
    if not found:
        return {}
    now = rules.decisions_now(found, today)
    return {d.advisory_id: d for d in found if any(a.pk == asset.pk for a in now[d.pk][0])}


def advisory_lines(request, asset, today) -> dict:
    """The Security tab's "Advisories" (slice 40, build40 section 7.8; the module docstring): {"items": [{"number", "url", "name",
    "status", "cves", "line", "responses" (each {"kind", "line" split around its number, "state", "tone"}), "record_url", "renew_url",
    "open_url", "reopen_url"}], "rest", "empty"}. One reading of every advisory not entered in error naming the model (rules.advisory_reading
    for this device), the decisions governing it (decisions_now), the listed advisories' status words (rules.advisory_counts) and sources
    (the names): a fixed number of queries however many advisories, rows, devices, and decisions; one with no advisory naming the model."""
    from apps.cybersecurity import advisories as cyber_advisories
    from apps.cybersecurity.models import AdvisorySource, AdvisoryStatus, NotApplicableReason, SecurityAdvisory
    from apps.facility import fiscal

    user = request.user
    found = list(SecurityAdvisory.objects.filter(removed_on__isnull=True, model_rows__device_model_id=asset.device_model_id)
                 .order_by("-received_on", "-number"))
    if not found:
        return {"items": [], "rest": "", "empty": rules.DEVICE_ADVISORIES_EMPTY}
    reading = rules.advisory_reading(found, today, assets=[asset])
    rows_by: dict = {}
    for r in reading.rows:
        rows_by.setdefault(r.advisory_id, []).append(r)
    line_by = {ln.row.advisory_id: ln for ln in reading.lines if ln.asset.pk == asset.pk}
    governing = _governing(asset, found, today)
    listed = []
    for adv in found:
        line = line_by.get(adv.pk)
        answer = reading.answers.get((asset.pk, line.row.pk)) if line is not None else None
        needs = answer is not None and answer.kind not in ("", rules.PATCHES_RECORDED)
        named_since = (adv.status == AdvisoryStatus.NOT_APPLICABLE and line is not None
                       and rules.not_applicable_blocks(adv.not_applicable_reason, rows_by.get(adv.pk, []), [line])
                       and (adv.not_applicable_reason == NotApplicableReason.NONE_HERE or line.needs_response))
        if adv.status == AdvisoryStatus.OPEN or needs or adv.pk in governing or named_since:
            listed.append((adv, line, answer, needs, named_since))
    rest = len(found) - len(listed)
    if not listed:
        return {"items": [], "rest": rest_words(rest, listed=False), "empty": ""}
    counts = rules.advisory_counts([adv for adv, *_ in listed], today=today, devices_visible=True)
    sources: dict = {}
    for s in AdvisorySource.objects.filter(advisory_id__in=[adv.pk for adv, *_ in listed]).order_by("created_at", "pk"):
        sources.setdefault(s.advisory_id, []).append(s)
    fy_read: list = []

    def fy():  # the facility's fiscal year start, read once and only for a decision's words outside an answer
        if not fy_read:
            fy_read.append(fiscal.start_month())
        return fy_read[0]

    can_wo = user.has_level(Module.WORKORDERS, Level.VIEW)
    can_decide = cyber_perms.can_decide(user)
    can_open = cyber_perms.can_open_updates(user) and asset.status != AssetStatus.RETIRED and not asset.incident_hold
    can_edit = cyber_perms.can_edit(user)
    items = []
    for adv, line, answer, needs, named_since in listed:
        gov = governing.get(adv.pk)
        responses = _response_rows(answer, gov, today, can_wo=can_wo, fy=fy)
        covered = answer is not None and answer.covers
        own = gov is not None and gov.asset_id == asset.pk
        decide = can_decide and needs and not covered
        update_open = answer is not None and any(r.kind in OPEN_KINDS for r in answer.responses)
        items.append({
            "number": adv.number, "url": reverse("web:security_advisory", args=[adv.number]),
            "name": rules.advisory_name(adv, rows=rows_by.get(adv.pk, []), sources=sources.get(adv.pk, [])),
            "status": counts[adv.pk].words, "cves": cves_words(adv.cves), "line": line.words if line is not None else "",
            "responses": responses,
            "record_url": record_decision_url(advisory=adv.number, asset=asset.tag) if decide and not own else "",
            "renew_url": record_decision_url(supersedes=gov.number) if decide and own else "",
            "open_url": (f"{reverse('web:cybersecurity_update_open', args=[asset.tag])}?{urlencode({'advisory': adv.number})}"
                         if can_open and needs and line.row.remedy in UPDATE_REMEDIES and not update_open else ""),
            "reopen_url": (reverse("web:security_advisory_reopen", args=[adv.number])
                           # the reader's state checks (user None); the level once above (Edit reopens one not applicable)
                           if named_since and can_edit and cyber_advisories.reopen_refusal(adv, user=None) is None else ""),
        })
    return {"items": items, "rest": rest_words(rest, listed=True), "empty": ""}


def tab_context(request, asset, today) -> dict:
    """{"cyber": ...} for the device drawer's Security tab (the caller offers the tab only to can_view_devices, never scoped). The
    device's standing is rules.device_lines' (slice 39), read once for its facts and its support decision."""
    user = request.user
    dl = rules.device_lines(asset, today, user=user)
    s = dl.standing
    lines = [_row("Software release", s.release_words), _row("Connected here", s.connection.words),
             _row("Manufacturer", s.security.words, wide=True), _row("Operating system", s.os.words, wide=True),
             _row("Checked", _checked(s))]
    hist = None
    if s.record is not None and history_tabs.allowed(user, AREA):
        hist = history_tabs.context(request, s.record, AREA, facts_url(asset), tab=False)["hist"]
    gone = gone_words(asset)
    return {"cyber": {"standing": s, "lines": lines, "card": release_card(s.facts, _others(asset, s)),
                      "mds2_sentence": rules.MDS2_SENTENCE, "gone": gone, "can_record": cyber_perms.can_edit_devices(user) and not gone,
                      "facts_url": facts_url(asset), "model_url": reverse("web:cybersecurity_model", args=[asset.device_model_id]),
                      "hist": hist, "updates": asset_cyber_updates.context(request, asset, today),  # slice 38
                      "decision": decision_lines(request, asset, dl, today),  # slice 39
                      "advisories": advisory_lines(request, asset, today)}}  # slice 40
