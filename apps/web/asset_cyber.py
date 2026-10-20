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
  the Record facts URL (?history=1&older=), since the drawer's own ?older= is the device's History tab's.

Words: never the ones rules.NEVER_WORDS lists (tests/test_cyber_never.py scans the tab with it).
"""
from __future__ import annotations

from django.urls import reverse

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

from . import history_tabs

AREA = "device_security"
TAB = "security"
NOT_RECORDED = "Not recorded"
GROUP_TITLES = {"support": "Support and security updates", "mds2": "The manufacturer's disclosure (MDS2)",
                "disclosure": "What the manufacturer discloses", "facility": "This facility's arrangement"}
# The MDS2-style answers, each named by its question in words (never a form's question ID: a 2013 or 2008 form numbers them otherwise)
QUESTIONS = (
    ("patient_data", PatientData, "Can it store or send patient data, as the manufacturer discloses?"),
    ("connects", Connects, "Can it connect to a network, as the manufacturer discloses?"),
    ("remote_service", Disclosed, "Can the manufacturer service it remotely, as the manufacturer discloses?"),
    ("patching", Patching, "Who may install its software updates, as the manufacturer discloses?"),
    ("sbom", Sbom, "Is a software bill of materials available, as the manufacturer discloses?"),
)
SOURCE_WORDS = {rules.THIS_RELEASE: "this release", rules.PROFILE: rules.PROFILE, rules.NOT_RECORDED: "not recorded"}
FACILITY_SOURCE = "the model's, for every version"


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
    rows = [_row("MDS2", _label(Mds2Status, status) if status else NOT_RECORDED)]
    if status == Mds2Status.ON_FILE:
        rows += [_row("Form", _label(Mds2Edition, f.get("mds2_edition"))), _row("Document", f.get("mds2_document") or NOT_RECORDED),
                 _row("Released", _day(f.get("mds2_released_on")))]
        if f.get("mds2_url"):
            rows.append(_row("Link", "The manufacturer's page", url=f.get("mds2_url")))
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
        "facility": [_row("Who installs updates here", _label(UpdatesBy, f.get("updates_by"))),
                     _row("Network segment here", _label(Segmented, f.get("segmented")))],
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


def tab_context(request, asset, today) -> dict:
    """{"cyber": ...} for the device drawer's Security tab (the caller offers the tab only to can_view_devices, never scoped)."""
    user = request.user
    s = rules.device_standing(asset, today)
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
                      "hist": hist}}
