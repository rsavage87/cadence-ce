"""
The device security register's rules (slice 37): every door (the Device security screen, the device drawer's Security tab, the API) reads
these, never the fields directly, so the words and the fallbacks are the same everywhere.

- Facts: a device's release and its model's profile, merged BY GROUP (support, MDS2, disclosure): a group comes from the release when any
  of its fields is set there, else from the profile ("the model's disclosure, version not stated"), else "not recorded". The facility
  group (who installs updates here, segmentation) is the profile's.
- Connection: the device's own network fact when recorded ("Connected (wireless)", "Not connected here"), else what its model discloses
  ("Can connect (wireless), as disclosed"), else "Connection not recorded".
- Standing, two separate lines, never merged: the manufacturer's security updates for the release (or, with no date of its own, the
  model's manufacturer support from slice 36: equipment.services.support_status), and the operating system vendor's published support.
  Dates are last days: "Security updates end Mar 31, 2028 (announced)", "Security updates ended after Mar 31, 2028"; "The OS vendor's
  published support for Windows 10 IoT Enterprise LTSC 2021 ends Jan 13, 2032" / "ended after ...". A fact about the date on file,
  never about this unit (Extended Security Updates are not modeled).

Words never used (NEVER_WORDS; a test scans every words string these rules can produce): secure, compliant, certified, patched,
unpatched, vulnerable, unsupported, mitigated, protected, safe, at risk, exposed, exploitable, remediated, end of life, high risk,
critical, required; slice 39 adds exempt, waiver, hardened, overdue, and deadline (a decision is the facility's record, never an
exemption, and its review date is its own, never a deadline).

Counting: every device on site that is not retired (ours and temporary alike: a rental is on the network too), with slice 29's Whose
filter; each figure says which devices it counts. Read in a fixed number of queries (standings, fleet, changes_for_it).

A release names its model: a device whose stored release belongs to another model (a model change racing a record) reads as "Release not
recorded", never another model's facts (equipment.services.update_asset clears it through services.model_changed).

Slice 38, software updates (written by apps.cybersecurity.updates): release_offer (offer_or_why) is the one predicate for recording what
an update work order did (the completion modal, the API's software_choices, and updates.check_choice read it); update_state and
update_words word a record on every door (the Security tab, the work order drawer and print, the API), for Device security View and
Equipment View only; updates_of (the Security tab's list) and campaigns (a model's releases' update work orders by state) are one query
each.

Slice 39, security decisions for devices past support (written by apps.cybersecurity.decisions; build39 sections 2 to 4, 7): the one
place for the controls and reasons in words (CONTROLS, SUPPORT_CONTROLS, REASONS), a device's support need (support_need), the governing
decision (governing_decisions: the most specific live one; a temporary device only by its own), the responses to a need
(update_responses: an open update to a supported release, an update done with the version not confirmed), the Security tab's lines
(device_lines), a decision's sentence and state (decision_words, decision_state), the devices it governs and its Check lines
(decision_devices, decision_checks), the Decisions tab's Needs attention (needs_attention), and the nav badge (badge). Positive conditions
only; every reader a fixed number of queries however many devices and decisions. Two readings of "a decision's devices", each named:
in scope (the devices on site its scope holds: a device-scope decision's device; a release's or a model's devices of ours) for the badge,
the past-review and govern-no-device lists, and the Check that says to end it; governed (in scope and this the most specific live decision)
for the device list's responses, joined since, and the other Checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.core.days import local_day
from apps.core.history import _rows
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, Ownership
from apps.equipment.services import owner_maintains, support_status
from apps.facility import fiscal
from apps.pm.dates import add_months

from .models import (
    DISCLOSURE_GROUP,
    FACILITY_GROUP,
    GROUPS,
    MDS2_GROUP,
    SUPPORT_GROUP,
    AcceptedBy,
    AppliesTo,
    Connects,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurity,
    DeviceSecurityDecision,
    Mds2Status,
    Network,
    OsFamily,
    PatientData,
    Segmented,
    SoftwareRelease,
)

THIS_RELEASE = "this release"
PROFILE = "the model's disclosure, version not stated"
FACILITY_SOURCE = "the model's, for every version"  # the facility group's source (this facility's arrangement, never a disclosure)
NOT_RECORDED = "not recorded"

# Release standing keys (the manufacturer's security updates)
UPDATES_ENDED, UPDATES_END_ANNOUNCED, MODEL_SUPPORT_ENDED, SECURITY_NOT_RECORDED = (
    "updates_ended", "updates_end_announced", "model_support_ended", "not_recorded")
# Operating system standing keys
OS_ENDED, OS_END_ANNOUNCED, OS_NOT_RECORDED, NO_OS = "os_ended", "os_end_announced", "os_not_recorded", "no_os"
# Connection keys
CONNECTED, NOT_CONNECTED, CAN_CONNECT, CONNECTION_NOT_RECORDED = "connected", "not_connected", "can_connect", "not_recorded"

RELEASE_NOT_RECORDED = "Release not recorded"
# The questions every door names the answers by (never an MDS2 form's question number: a 2013 or 2008 form numbers them otherwise)
QUESTIONS = {
    "patient_data": "What can it do with patient data, as the manufacturer discloses?",
    "connects": "Can it connect to a network, as the manufacturer discloses?",
    "remote_service": "Can the manufacturer service it remotely, as it discloses?",
    "patching": "Who may install its software updates, as the manufacturer discloses?",
    "sbom": "Is a software bill of materials (SBOM) available from the manufacturer?",
    "updates_by": "Who installs updates here?",
    "segmented": "Are its connected devices on a medical device network segment here?",
}
MDS2_SENTENCE = ("The MDS2 describes what the device can do; whether you may change it is the manufacturer's documentation and your "
                 "contract.")
HICP_SENTENCE = ("HHS 405(d) HICP 2023 recommends keeping the medical device inventory, with at least major version information, in the "
                 "CMMS.")
CHANGES_DAYS = (7, 30, 90)  # Changes for IT: the windows offered (30 by default)

# Never in Cadence's voice on any door (build37 "Words"): matched as whole words, in any letter case (tests scan every door with it).
NEVER_WORDS = ("secure", "compliant", "certified", "patched", "unpatched", "vulnerable", "unsupported", "mitigated",
               "protected", "safe", "at risk", "exposed", "exploitable", "remediated", "end of life", "high risk", "critical", "required",
               "requires", "NPRM", "FDA-approved", "FDA-cleared", "IEC 80001", "DNV",
               # slice 39 (build39 section 4)
               "exempt", "waiver", "hardened", "deadline",
               # slice 40 (build40 section 1): an advisory's statuses are the issuer's (CSAF), never Cadence's verdict
               "affected", "fixed", "exploited", "zero-day", "uncontrolled")
# A decision's words also never say "overdue" (its only clock is its own review date: "Review due since ..."). Not in NEVER_WORDS, which
# every door's whole page is scanned with: the device drawer around the Security tab carries Equipment's own PM words ("overdue for PM").
DECISION_NEVER_WORDS = (*NEVER_WORDS, "overdue")

ALL_FIELDS = (*SUPPORT_GROUP, *MDS2_GROUP, *DISCLOSURE_GROUP, *FACILITY_GROUP)
DATE_FIELDS = frozenset(f for f in ALL_FIELDS if f.endswith("_on"))
CAN_CONNECT_VALUES = (Connects.WIRED, Connects.WIRELESS, Connects.BOTH)  # the model can connect, as disclosed
CONNECTED_NETWORKS = (Network.WIRED, Network.WIRELESS, Network.BOTH, Network.INTERMITTENT)  # connected here, by the device's own fact
PATIENT_DATA_VALUES = (PatientData.STORES, PatientData.HANDLES)  # can store, or displays or sends, patient data (as disclosed)
ON_SITE_KEYS = (CONNECTED, CAN_CONNECT)  # "can connect": connected here, or able to as disclosed (no fact of its own)
ENDED_KEYS = (UPDATES_ENDED, MODEL_SUPPORT_ENDED)
COUNTS_ON_SITE = "Every device on site, ours and temporary (rentals, loaners, demo units), not retired"

_CAN_CONNECT_WORDS = {Connects.WIRED: "Can connect (wired), as disclosed", Connects.WIRELESS: "Can connect (wireless), as disclosed",
                      Connects.BOTH: "Can connect (wired and wireless), as disclosed"}
_OS_WORDS = {OsFamily.WINDOWS: "Windows", OsFamily.LINUX: "Linux", OsFamily.EMBEDDED: "its embedded or real-time OS"}
_UNREAD = object()


def day_words(d: date) -> str:
    """ "Mar 31, 2028": every day the register words."""
    return f"{d:%b} {d.day}, {d.year}"


def _is_set(value) -> bool:
    return value not in (None, "")


@dataclass(frozen=True)
class Facts:
    """A device's (or a release's) facts with the profile merged in by group. `values` holds every field of SUPPORT_GROUP, MDS2_GROUP,
    DISCLOSURE_GROUP, and FACILITY_GROUP; `sources` maps "support", "mds2", "disclosure", "facility" to THIS_RELEASE, PROFILE, or
    NOT_RECORDED."""

    release: object | None  # the versioned SoftwareRelease, or None
    profile: object | None  # the model's profile row, or None
    values: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    def get(self, name: str):
        return self.values.get(name)

    @property
    def can_connect(self) -> bool:
        """The model (or this release) can connect to a network, as the manufacturer discloses."""
        return self.get("connects") in CAN_CONNECT_VALUES

    @property
    def patient_data(self) -> bool:
        """It can store, or displays or sends, patient data, as the manufacturer discloses (never that patient data is on a unit)."""
        return self.get("patient_data") in PATIENT_DATA_VALUES


@dataclass(frozen=True)
class Standing:
    key: str
    words: str
    day: date | None = None


@dataclass(frozen=True)
class Connection:
    key: str
    words: str
    here: bool  # recorded for this device (False: as the model discloses, or not recorded)


@dataclass(frozen=True)
class DeviceStanding:
    asset: object
    release: object | None
    release_since: date | None
    facts: Facts
    connection: Connection
    security: Standing
    os: Standing
    checked_on: date | None = None
    checked_by: object | None = None  # the User who recorded the facts last (None: nobody, or the system)
    record: object | None = None  # the device's DeviceSecurity row, or None (nothing recorded yet)

    @property
    def release_words(self) -> str:
        """ "9.33.1, since Mar 3, 2026" or RELEASE_NOT_RECORDED."""
        if self.release is None:
            return RELEASE_NOT_RECORDED
        if self.release_since is None:
            return self.release.version
        return f"{self.release.version}, since {day_words(self.release_since)}"

    @property
    def network(self) -> str:
        """The device's own network fact ("" for as the model discloses)."""
        return self.record.network if self.record is not None else ""


def on_site():
    """The devices the register lists: every device not retired (ours and temporary), with model and department. A temporary device
    returned to its owner is retired (equipment.services.status_label reads it "Returned to owner"), so it is left out too."""
    return Asset.objects.exclude(status=AssetStatus.RETIRED).select_related("device_model", "department")


def _model_id(model):
    return model.pk if isinstance(model, DeviceModel) else model


def profile(model):
    """The model's profile row (version blank, not removed), or None."""
    return SoftwareRelease.objects.filter(device_model_id=_model_id(model), version_key="", removed_on__isnull=True).first()


def releases(model) -> list:
    """The model's versioned releases, not removed, in version_key order."""
    return list(SoftwareRelease.objects.filter(device_model_id=_model_id(model), removed_on__isnull=True).exclude(version_key="")
                .order_by("version_key"))


def _names(release, asset) -> bool:
    """Whether `release` is one a device may be read as running: versioned, not entered in error, of the device's model."""
    return (release is not None and release.version_key != "" and release.removed_on is None
            and release.device_model_id == asset.device_model_id)


def _cached_record(asset):
    """The device's DeviceSecurity row when a query already read it (select_related("cyber...")): the row, None, or _UNREAD."""
    descriptor = type(asset).cyber
    if descriptor.is_cached(asset):
        return descriptor.related.get_cached_value(asset)
    return _UNREAD


def release_of(asset):
    """The versioned release the device runs, or None. Every reader asks this, never asset.cyber.release (a later multi-component
    record stays additive)."""
    record = _cached_record(asset)
    if record is _UNREAD:
        record = DeviceSecurity.objects.filter(asset_id=asset.pk).select_related("release").first()
    release = record.release if record is not None and record.release_id else None
    return release if _names(release, asset) else None


def facts(release, profile) -> Facts:
    """Merge a versioned release (or None) with its model's profile (or None), by group. A release entered in error, a profile passed
    as the release, a removed profile, or a profile of another model is ignored."""
    if release is not None and (release.removed_on is not None or not release.version_key):
        release = None
    if profile is not None and (profile.removed_on is not None or profile.version_key
                                or (release is not None and profile.device_model_id != release.device_model_id)):
        profile = None
    values, sources = {}, {}
    for group, names in (*GROUPS.items(), ("facility", FACILITY_GROUP)):
        source, row = NOT_RECORDED, None
        if group != "facility" and release is not None and any(_is_set(getattr(release, n)) for n in names):
            source, row = THIS_RELEASE, release
        elif profile is not None and any(_is_set(getattr(profile, n)) for n in names):
            source, row = PROFILE, profile
        sources[group] = source
        for n in names:
            values[n] = getattr(row, n) if row is not None else (None if n in DATE_FIELDS else "")
    return Facts(release=release, profile=profile, values=values, sources=sources)


def connected(security, facts: Facts) -> Connection:
    """The device's connection: its own network fact (security may be None), else its model's disclosure, else not recorded."""
    network = getattr(security, "network", "") if security is not None else ""
    if network == Network.NOT_CONNECTED:
        return Connection(NOT_CONNECTED, Network.NOT_CONNECTED.label, True)
    if network in CONNECTED_NETWORKS:
        return Connection(CONNECTED, Network(network).label, True)
    connects = facts.get("connects")
    if connects == Connects.NONE:
        return Connection(NOT_CONNECTED, "Does not connect, as disclosed", False)
    if connects in CAN_CONNECT_VALUES:
        return Connection(CAN_CONNECT, _CAN_CONNECT_WORDS[connects], False)
    return Connection(CONNECTION_NOT_RECORDED, "Connection not recorded", False)


def release_standing(facts: Facts, model, today: date) -> Standing:
    """The manufacturer's security updates for the release (slice 36's model support when the release has no date of its own)."""
    last = facts.get("security_updates_end_on")
    if last is not None:
        if last < today:
            return Standing(UPDATES_ENDED, f"Security updates ended after {day_words(last)}", last)
        return Standing(UPDATES_END_ANNOUNCED, f"Security updates end {day_words(last)} (announced)", last)
    if model is not None:
        support = support_status(model, today)
        if support.key in ("support_ended", "parts_ended"):
            day = model.parts_end_on if support.key == "parts_ended" else model.support_ends_on
            return Standing(MODEL_SUPPORT_ENDED, f"{support.words} (the model's)", day)
    return Standing(SECURITY_NOT_RECORDED, "Security updates end: not recorded")


def os_standing(facts: Facts, today: date) -> Standing:
    """The OS vendor's published support for the release's operating system."""
    family = facts.get("os_family")
    if family == OsFamily.NONE:
        return Standing(NO_OS, "No operating system")
    last = facts.get("os_support_ends_on")
    if last is None:
        return Standing(OS_NOT_RECORDED, "Operating system support: not recorded")
    name = facts.get("os_name") or _OS_WORDS.get(family, "its operating system")
    if last < today:
        return Standing(OS_ENDED, f"The OS vendor's published support for {name} ended after {day_words(last)}", last)
    return Standing(OS_END_ANNOUNCED, f"The OS vendor's published support for {name} ends {day_words(last)}", last)


def _profiles(model_ids) -> dict:
    """{model id: its profile row} for `model_ids` (one query)."""
    return {p.device_model_id: p for p in SoftwareRelease.objects.filter(device_model_id__in=set(model_ids), version_key="",
                                                                          removed_on__isnull=True)}


def _standing(asset, record, profile_row, today: date) -> DeviceStanding:
    release = record.release if record is not None and record.release_id else None
    if not _names(release, asset):
        release = None
    merged = facts(release, profile_row)
    return DeviceStanding(asset=asset, release=release, release_since=record.release_since if release is not None else None,
                          facts=merged, connection=connected(record, merged),
                          security=release_standing(merged, asset.device_model, today), os=os_standing(merged, today),
                          checked_on=record.checked_on if record is not None else None,
                          checked_by=record.checked_by if record is not None and record.checked_by_id else None, record=record)


_WITH_RECORD = ("device_model", "department", "cyber__release", "cyber__checked_by")


def _devices_with_records(assets) -> list:
    """The devices of `assets` with their models and DeviceSecurity rows read: a queryset in one query (joined), a list in at most two
    more (the rows; the models when a device has not read its own)."""
    if isinstance(assets, QuerySet):
        return list(assets.select_related(*_WITH_RECORD))
    devices = list(assets)
    unread = {a.device_model_id for a in devices if not Asset.device_model.is_cached(a)}
    if unread:
        models = DeviceModel.objects.in_bulk(unread)
        for a in devices:
            if not Asset.device_model.is_cached(a):
                a.device_model = models[a.device_model_id]
    if any(_cached_record(a) is _UNREAD for a in devices):
        records = {r.asset_id: r for r in DeviceSecurity.objects.filter(asset_id__in=[a.pk for a in devices])
                   .select_related("release", "checked_by")}
        cache = type(devices[0]).cyber.related
        for a in devices:
            if _cached_record(a) is _UNREAD:
                cache.set_cached_value(a, records.get(a.pk))
    return devices


def standings(assets, today: date) -> dict:
    """{asset id: DeviceStanding} for the devices of `assets` (a queryset or a list), in a fixed number of queries (lists, CSVs, the
    API): a queryset's devices with their rows (one, joined), a list's rows (one) and models (one, only when unread), the models'
    profiles (one)."""
    devices = _devices_with_records(assets)
    if not devices:
        return {}
    profiles = _profiles(a.device_model_id for a in devices)
    return {a.pk: _standing(a, _cached_record(a), profiles.get(a.device_model_id), today) for a in devices}


def device_standing(asset, today: date) -> DeviceStanding:
    """One device (the drawer's tab): standings() for one."""
    return standings([asset], today)[asset.pk]


# --- the fleet: one row per model -------------------------------------------------------------------------------------------------

_MDS2_ORDER = (Mds2Status.ON_FILE, Mds2Status.REQUESTED, Mds2Status.NONE_FROM_MANUFACTURER)


@dataclass(frozen=True)
class FleetRow:
    device_model: object
    devices: int
    connected: int  # connected here, by the device's fact or its model's disclosure
    by_release: list  # [(release or None for not recorded, count)]
    updates_ended: int  # devices on a release past the manufacturer's security updates (or the model's support)
    os_ended: int  # devices on a release whose OS vendor's published support ended
    facts: Facts  # the profile's facts (MDS2, patient data, updates by, segmented)
    not_recorded: int = 0  # devices with no release recorded
    can_connect: bool = False  # a device is connected here, or the model can connect as disclosed
    mds2: str = ""  # the model's MDS2: on_file when its profile or any release has one on file, else requested, else none, else ""
    mds2_versions: tuple = ()  # the versions with an MDS2 on file ("" for the profile: version not stated)

    @property
    def mds2_words(self) -> str:
        """ "On file", "On file for 9.19.2" (only releases hold one), "Requested from the manufacturer", "The manufacturer has none",
        or "Not recorded"."""
        if self.mds2 == Mds2Status.ON_FILE:
            if "" in self.mds2_versions or not self.mds2_versions:
                return Mds2Status.ON_FILE.label
            return f"On file for {', '.join(self.mds2_versions)}"
        return Mds2Status(self.mds2).label if self.mds2 else "Not recorded"


@dataclass(frozen=True)
class Fleet:
    rows: list
    figures: dict  # the head figures, each a count with the devices it counts named in words
    devices: int = 0  # devices counted (on site, or among the filtered devices)


def mds2_of(rows) -> tuple[str, tuple]:
    """The model's MDS2 status from its profile and releases (not removed): on file anywhere first, then requested, then none."""
    statuses = {r.mds2 for r in rows}
    status = next((s for s in _MDS2_ORDER if s in statuses), "")
    versions = tuple(r.version for r in sorted(rows, key=lambda r: r.version_key) if r.mds2 == Mds2Status.ON_FILE)
    return status, versions


def _figure(key: str, count: int, words: str, counts: str, **extra) -> dict:
    return {"key": key, "count": count, "words": words, "counts": counts, **extra}


def fleet(today: date, assets=None) -> Fleet:
    """One row per model with devices on site (or among `assets`, a filtered queryset or list: its retired devices left out), sorted by
    manufacturer and model, and the head figures (Fleet.figures, by key, each {"key", "count", "words", "counts"}):
    - no_mds2: models that can connect (a device connected here, or the model can connect as disclosed) with no MDS2 on file (its
      profile's nor any release's); "requested" counts those of them whose MDS2 is requested from the manufacturer.
    - updates_ended: devices connected here or able to connect whose release is past the manufacturer's security updates (or, with no
      date of its own, past the model's manufacturer support).
    - no_release: devices connected here or able to connect with no release recorded.
    - not_segmented: models with devices connected here or able to connect that the facility puts on no separate segment.
    - segment_not_recorded: models with such devices whose segmentation is not recorded.
    Three queries: the devices with their rows, the models' profiles and releases, nothing per device."""
    if assets is None:
        devices = _devices_with_records(on_site())
    elif isinstance(assets, QuerySet):
        devices = _devices_with_records(assets.exclude(status=AssetStatus.RETIRED))
    else:
        devices = _devices_with_records([a for a in assets if a.status != AssetStatus.RETIRED])
    model_rows: dict = {}
    if devices:
        for r in SoftwareRelease.objects.filter(device_model_id__in={a.device_model_id for a in devices}, removed_on__isnull=True):
            model_rows.setdefault(r.device_model_id, []).append(r)
    by_model: dict = {}
    for a in devices:
        by_model.setdefault(a.device_model_id, []).append(a)
    rows = []
    totals = {"updates_ended": 0, "no_release": 0}
    for model_id, members in by_model.items():
        dm = members[0].device_model
        held = model_rows.get(model_id, [])
        profile_row = next((r for r in held if not r.version_key), None)
        counted, connected_n, ended, os_n, not_recorded = {}, 0, 0, 0, 0
        for a in members:
            s = _standing(a, _cached_record(a), profile_row, today)
            counted[s.release] = counted.get(s.release, 0) + 1
            on = s.connection.key in ON_SITE_KEYS
            connected_n += on
            ended += s.security.key in ENDED_KEYS
            os_n += s.os.key == OS_ENDED
            not_recorded += s.release is None
            totals["updates_ended"] += on and s.security.key in ENDED_KEYS
            totals["no_release"] += on and s.release is None
        by_release = sorted(((r, n) for r, n in counted.items() if r is not None), key=lambda p: (p[0].version_key, p[0].pk))
        if None in counted:
            by_release.append((None, counted[None]))
        profile_facts = facts(None, profile_row)
        mds2, versions = mds2_of(held)
        rows.append(FleetRow(device_model=dm, devices=len(members), connected=connected_n, by_release=by_release, updates_ended=ended,
                             os_ended=os_n, facts=profile_facts, not_recorded=not_recorded,
                             can_connect=connected_n > 0 or profile_facts.can_connect, mds2=mds2, mds2_versions=versions))
    rows.sort(key=lambda r: (r.device_model.manufacturer.casefold(), r.device_model.model.casefold(), str(r.device_model.pk)))
    connectable = [r for r in rows if r.can_connect]
    with_connected = [r for r in rows if r.connected]
    on = "Devices on site (ours and temporary) connected here or able to connect, as disclosed"
    models_on = "Models with devices on site (ours and temporary)"
    figures = {f["key"]: f for f in (
        _figure("no_mds2", sum(r.mds2 != Mds2Status.ON_FILE for r in connectable), "Models that can connect with no MDS2 on file",
                models_on + " that can connect", requested=sum(r.mds2 == Mds2Status.REQUESTED for r in connectable)),
        _figure("updates_ended", totals["updates_ended"],
                "Devices that can connect, past the manufacturer's security updates or the model's support", on),
        _figure("no_release", totals["no_release"], "Devices that can connect with no release recorded", on),
        _figure("not_segmented", sum(r.facts.get("segmented") == Segmented.NONE for r in with_connected),
                "Models with connected devices not on a separate segment", models_on + " connected here or able to connect"),
        _figure("segment_not_recorded", sum(not r.facts.get("segmented") for r in with_connected),
                "Models with connected devices whose network segment is not recorded", models_on + " connected here or able to connect"),
    )}
    return Fleet(rows=rows, figures=figures, devices=len(devices))


# --- Changes for IT ----------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    asset: object
    day: date
    kind: str  # "added", "missing", "retired", "returned"
    words: str
    connection: Connection | None = None  # the device's connection as the register reads it now


ADDED, MISSING, RETIRED, RETURNED = "added", "missing", "retired", "returned"


def _added_words(added_as: str, day: date) -> str:
    if added_as == AddedAs.IMPORTED:
        return f"Imported {day_words(day)}"
    if added_as == AddedAs.EXISTING:
        return f"Added {day_words(day)} (already in use here)"
    return f"Added {day_words(day)}"


_CHANGE_WORDS = {MISSING: "Marked missing {day}", RETIRED: "Retired {day}", RETURNED: "Returned to its owner {day}"}


def _events(rows, start: date, today: date) -> list:
    """[(device id, kind, day, moment, added_as)] from the devices' history rows in order (id, history_date, history_id): a device added ("+"),
    its status becoming missing, or retired (returned to its owner when it was temporary then), on a facility day in [start, today]. A
    return's day is the day it went back, the returned_on its row holds (return_to_owner dates the row when it is entered, review fix
    C31), else its row's day. A device of ours whose retirement is dated before the day it was added (a retirement from the previous
    system, backdated) was never on site: its addition is left out (never a temporary device's return entered late). `rows` may leave
    out a device's rows before the window but its last one (its status then) and its retired ones (changes_for_it): the events in the
    window are the same."""
    out, previous, added, retired_days = [], {}, {}, {}
    for pk, kind, status, ownership, added_as, returned_on, moment in rows:
        day = local_day(moment)
        before = previous.get(pk)
        previous[pk] = status
        if kind == "+":
            added[pk] = (day, moment, added_as)
            continue
        if kind != "~" or status == before:
            continue
        if status == AssetStatus.MISSING:
            out.append((pk, MISSING, day, moment, ""))
        elif status == AssetStatus.RETIRED:
            returned = ownership != Ownership.OWNED
            if returned and returned_on is not None:
                day = returned_on
            if not returned:  # a return entered late never hides the stay's arrival (round 2): only a backdated retirement of ours does
                retired_days.setdefault(pk, []).append(day)
            out.append((pk, RETURNED if returned else RETIRED, day, moment, ""))
    for pk, (day, moment, added_as) in added.items():
        if not any(d < day for d in retired_days.get(pk, ())):
            out.append((pk, ADDED, day, moment, added_as))
    return [e for e in out if start <= e[2] <= today]


def changes_for_it(today: date, days: int = 30) -> list:
    """Devices whose model can connect, or can store or handle patient data (as the manufacturer discloses: the device's facts, its
    release over its model's profile by group), or whose own fact says connected, added, marked missing, retired, or returned to their
    owner on a facility day in [today - days + 1, today] (equipment's history through core.history._rows; a return on the day it went
    back), newest first. Every device, ours and temporary. Three queries (one when nothing changed): the history rows, the devices with
    their rows, their models' profiles.

    The history rows read stay near the window's (review fix C1: never each device's whole history, which grows with the years while the
    window stays fixed): the rows from the window's start, each device's last row before it (its status then: a change is a status that
    differs), and the retired rows of the devices added in the window (a retirement backdated before the day it was added, from the
    previous system: never on site)."""
    start = today - timedelta(days=days - 1)
    since = timezone.make_aware(datetime.combine(start - timedelta(days=1), time.min))  # a day early: the facility's day decides below
    history = Asset.history.model
    in_window = _rows(history).filter(history_date__gte=since)
    last_before = (_rows(history).filter(id=OuterRef("id"), history_date__lt=since).order_by("-history_date", "-history_id")
                   .values("history_id")[:1])
    rows = (_rows(history)
            .filter(Q(history_date__gte=since)
                    | Q(history_id__in=in_window.annotate(last=Subquery(last_before)).values("last"))
                    | Q(status=AssetStatus.RETIRED, id__in=in_window.filter(history_type="+").values("id")))
            .order_by("id", "history_date", "history_id")
            .values_list("id", "history_type", "status", "ownership", "added_as", "returned_on", "history_date"))
    events = _events(rows, start, today)
    if not events:
        return []
    devices = {a.pk: a for a in Asset.objects.filter(pk__in={e[0] for e in events}).select_related(*_WITH_RECORD)}
    profiles = _profiles(a.device_model_id for a in devices.values())
    out = []
    for pk, kind, day, moment, added_as in events:
        asset = devices.get(pk)
        if asset is None:
            continue
        s = _standing(asset, _cached_record(asset), profiles.get(asset.device_model_id), today)
        if not (s.facts.can_connect or s.facts.patient_data or s.connection.key == CONNECTED):
            continue
        words = _added_words(added_as, day) if kind == ADDED else _CHANGE_WORDS[kind].format(day=day_words(day))
        out.append((day, moment, asset.tag, Change(asset=asset, day=day, kind=kind, words=words, connection=s.connection)))
    out.sort(key=lambda c: c[2])  # by tag within one moment
    out.sort(key=lambda c: (c[0], c[1]), reverse=True)  # newest first (a stable sort keeps the tags' order)
    return [c[3] for c in out]


# --- slice 38: software updates (every door reads these, never the fields) ----------------------------------------------------------

@dataclass(frozen=True)
class Offer:
    """What a completion (or Confirm version) may record on an update work order (release_offer): its record, the model's versioned
    releases not removed (version_key order), the device's recorded release (or None), the target, the outcomes and the bases allowed
    for each (updates.BASES), and whether a choice is needed (nothing recorded yet)."""

    update: object
    releases: tuple
    recorded: object | None
    outcomes: tuple
    bases: dict
    needed: bool

    @property
    def target(self) -> str:
        """The record's target (UpdateTarget: a release of the model, or the manufacturer's patches)."""
        return self.update.target

    @property
    def target_release(self):
        """The release the work order was opened to install, or None (patches)."""
        return self.update.target_release if self.update.target_release_id else None

    @property
    def target_reference(self) -> str:
        """The manufacturer's patch list the work order was opened to apply (a token), or ""."""
        return self.update.target_reference


# Why release_offer offers nothing (offer_or_why), in the order checked: the work order is not a software update; it has no record (an
# update imported from another system: history only); its device is not on site (retired, or returned to its owner); the user may not
# record (a scoped user, or one without Device security Edit and Equipment View)
NOT_AN_UPDATE, NO_RECORD, NOT_ON_SITE, NO_PERMISSION = "not_an_update", "no_record", "not_on_site", "no_permission"


def offer_or_why(wo, user) -> tuple:
    """(Offer, "") where release_offer holds, else (None, why): NOT_AN_UPDATE, NO_RECORD, NOT_ON_SITE, NO_PERMISSION, checked in that
    order (updates.check_choice words each refusal by it; a scoped user's choice it refuses with the permission's words before reading
    this, review round 3). `user` None is the system: it may record. Queries: none for another type; the record (one, with its target);
    with an offer, the releases and the recorded release (two; and the device when `wo` has not read it)."""
    from apps.workorders.models import Source, WoType

    from . import permissions as perms
    from .models import RECORDED_OUTCOMES, SoftwareUpdate
    from .updates import BASES

    if wo.type != WoType.SOFTWARE_UPDATE:
        return None, NOT_AN_UPDATE
    update = None
    if wo.source != Source.IMPORTED:
        update = SoftwareUpdate.objects.filter(work_order_id=wo.pk).select_related("target_release").order_by("pk").first()
    if update is None:
        return None, NO_RECORD
    asset = wo.asset
    if asset.status == AssetStatus.RETIRED:
        return None, NOT_ON_SITE
    if user is not None and not perms.can_edit_devices(user):
        return None, NO_PERMISSION
    update.work_order = wo  # read already: the record's words need no query of their own
    return Offer(update=update, releases=tuple(releases(asset.device_model_id)), recorded=release_of(asset),
                 outcomes=tuple(RECORDED_OUTCOMES), bases=dict(BASES), needed=not update.outcome), ""


def release_offer(wo, user):
    """The one predicate (slice 38) the completion modal, the work order serializer's software_choices, and updates.check_choice read:
    an Offer, or None unless the work order is a software update with its record (register-opened, so never imported), its device on
    site (on_site: not retired, not returned), and `user` may record (permissions.can_edit_devices; None is the system). Slice 40 widens
    the type gate. Offer.outcomes are installed, patches, and already, always (whether already fits the target is the service's check);
    Offer.needed is True while nothing is recorded (a completion by a user must then choose, or Not confirmed now)."""
    return offer_or_why(wo, user)[0]


OPEN, NOT_CONFIRMED, DONE, CANCELLED, REMOVED = "open", "not_confirmed", "done", "cancelled", "removed"
STATES = (OPEN, DONE, NOT_CONFIRMED, CANCELLED)  # a work order's record's states, in the campaign's order (REMOVED: a hand row's)
STATE_LABELS = {OPEN: "Open", DONE: "Done", NOT_CONFIRMED: "Version not confirmed", CANCELLED: "Cancelled", REMOVED: "Entered in error"}
_CAMPAIGN_WORDS = {OPEN: "open", DONE: "done", NOT_CONFIRMED: "not confirmed", CANCELLED: "cancelled"}
ENTERED_IN_ERROR_SINCE = " (entered in error since)"
LATER_KEPT_WORDS = "a later record of the device is kept"
# Where a hand-recorded install came from (InstalledBy's values), as its sentence ends
INSTALLER_WORDS = {"ce": "installed by clinical engineering", "it": "installed by information technology",
                   "manufacturer": "installed by the manufacturer remotely", "automatic": "installed automatically",
                   "department": "installed by the department that uses it"}


def _work_order_state(status: str, outcome: str) -> str:
    from apps.workorders.models import OPEN_STATUSES, WoStatus

    if status in OPEN_STATUSES:
        return OPEN
    if status == WoStatus.CANCELLED:
        return CANCELLED
    return DONE if outcome else NOT_CONFIRMED


def update_state(update) -> str:
    """An update's state key: open (its work order open, in progress, or awaiting parts), not_confirmed (completed or closed, nothing
    recorded), done (an outcome recorded, a hand row's always), cancelled (its work order cancelled), removed (a hand row entered in
    error). A work order's record follows its work order's status: a reopened one is open again, whatever it recorded before."""
    if update.work_order_id is None:
        return REMOVED if update.removed_on is not None else DONE
    return _work_order_state(update.work_order.status, update.outcome)


def release_name(release) -> str:
    """A release in an update's words: its version, "<version> (entered in error since)" for one entered in error since, "" for none."""
    if release is None:
        return ""
    return f"{release.version}{ENTERED_IN_ERROR_SINCE}" if release.removed_on is not None else release.version


def _source_words(update) -> str:
    """Where the record came from, as its sentence ends: the vendor's job ("as the vendor's report <number> states"), an install recorded
    by hand (by whom), else "" (in-house work). Read from the record itself (review fix C15: on_vendor_service and vendor_report, the
    work order as it was when the outcome was recorded), never from the work order's flags now: a job reopened and moved to a vendor (or
    back in-house) never changes who an outcome already recorded came from."""
    if update.work_order_id is None:
        return INSTALLER_WORDS.get(update.installed_by, "")
    if update.on_vendor_service:
        return f"as the vendor's report {update.vendor_report} states" if update.vendor_report else "as the vendor's report states"
    return ""


def _open_words(update) -> str:
    if update.target_release_id:
        return f"Open: to install {release_name(update.target_release)}"
    if update.target_reference:
        return f"Open: to apply the manufacturer's patches (ref {update.target_reference})"
    return "Open: to apply the manufacturer's patches"


def _outcome_words(update) -> str:
    from .models import UpdateBasis, UpdateOutcome

    day = day_words(update.applied_on) if update.applied_on else "on a day not recorded"
    after = release_name(update.release_after) if update.release_after_id else ""
    ref = f"; ref {update.reference}" if update.reference else ""
    if update.outcome == UpdateOutcome.INSTALLED:
        # only what the person recording it says the update did (review round 4): never what it ran before (the device's History)
        text = f"{after or 'A release'} installed {day}{ref}"
    elif update.outcome == UpdateOutcome.PATCHES:
        what = "Patches on the manufacturer's validated patch list" if update.basis == UpdateBasis.VALIDATED_LIST else "Patches"
        # no version: "version not recorded", unless a later record was kept (the version that day is then not stated)
        version = f" ({after})" if after else ("" if update.later_kept else " (version not recorded)")
        text = f"{what} applied {day}, version unchanged{version}{ref}"
    else:
        text = f"Checked {day}: already on {after or 'the update'}; nothing installed{ref}"
    source = _source_words(update)
    if source:
        text += f"; {source}"
    if update.later_kept:
        text += f"; {LATER_KEPT_WORDS}"
    return text


def update_words(update, *, today=None) -> str:
    """The update in words (build38 "Words"), for readers with Device security View and Equipment View (permissions.can_view_devices):
    - open: "Open: to install 5.6 (demo)", or "Open: to apply the manufacturer's patches (ref ICAR-2026-09)";
    - not confirmed: "Completed Oct 20, 2026; version not confirmed" ("Completed by the vendor ..." only for vendor service);
    - cancelled: "Cancelled"; a hand row entered in error: "Entered in error";
    - done: "5.6 (demo) installed Oct 20, 2026" (only what the update did, review round 4: never what it ran before, which the device's
      History shows); "Patches applied Oct 20, 2026, version unchanged (5.6 (demo)); ref ICAR-2026-09" ("(version not recorded)" with
      none, and nothing there when a later record was kept; "Patches on the manufacturer's validated patch list applied ..." only for
      UpdateBasis.VALIDATED_LIST); "Checked Oct 20, 2026: already on 5.6 (demo); nothing installed"; a reference as "; ref <it>"; then
      where it came from ("; as the vendor's report <number> states" when recorded on vendor service (the record's own on_vendor_service
      and vendor_report), "; installed by information technology" by hand, nothing for in-house work), and "; a later record of the
      device is kept" when later_kept.
    Never "updated from X to Y" (Cadence cannot order versions), never a never word. A release entered in error since reads
    "<version> (entered in error since)". `today` is unused: every day named is the record's own."""
    state = update_state(update)
    if state == REMOVED:
        return STATE_LABELS[REMOVED]
    if state == CANCELLED:
        return STATE_LABELS[CANCELLED]
    if state == OPEN:
        return _open_words(update)
    if state == NOT_CONFIRMED:
        wo = update.work_order
        day = f" {day_words(wo.completed_on)}" if wo.completed_on else ""
        return f"Completed by the vendor{day}; version not confirmed" if wo.vendor_service else f"Completed{day}; version not confirmed"
    return _outcome_words(update)


def updates_of(asset) -> list:
    """The device's updates for its Security tab, newest first: every work order's record and the hand rows not entered in error, each
    with its work order and releases read in the same query. One query. Newest by the record's day (the day it was done, applied_on;
    else its work order's completion, version not confirmed; else the day it was opened, open or cancelled), then the newest row."""
    from django.db.models import F
    from django.db.models.functions import Coalesce

    from .models import SoftwareUpdate

    day = Coalesce(F("applied_on"), F("work_order__completed_on"), F("opened_on"))
    return list(SoftwareUpdate.objects.filter(asset_id=asset.pk).filter(Q(work_order__isnull=False) | Q(removed_on__isnull=True))
                .select_related("work_order", "target_release", "release_after")
                .order_by(day.desc(), "-created_at", "-pk"))


def campaigns(model) -> dict:
    """{release id: {state key: count}} for every release of `model` that update work orders target, each with every state of STATES
    (open, done, not_confirmed, cancelled; 0 where none): the model panel's "Updates to 5.6 (demo): 3 open, 1 done, 1 not confirmed"
    (campaign_words). One aggregate query (the work orders' statuses and the records' outcomes, grouped)."""
    from django.db.models import Count

    from .models import SoftwareUpdate

    rows = (SoftwareUpdate.objects.filter(target_release__device_model_id=_model_id(model), work_order__isnull=False).order_by()
            .values("target_release_id", "work_order__status", "outcome").annotate(n=Count("pk")))
    out: dict = {}
    for row in rows:
        counts = out.setdefault(row["target_release_id"], dict.fromkeys(STATES, 0))
        counts[_work_order_state(row["work_order__status"], row["outcome"])] += row["n"]
    return out


def campaign_words(release, counts: dict | None) -> str:
    """ "Updates to 5.6 (demo): 3 open, 1 done, 1 not confirmed" (the states with a count, in STATES order), or "" for none."""
    parts = [f"{counts[s]} {_CAMPAIGN_WORDS[s]}" for s in STATES if counts and counts.get(s)]
    return f"Updates to {release_name(release)}: {', '.join(parts)}" if parts else ""


# --- slice 39: security decisions for devices past support ---------------------------------------------------------------------------

# The controls a decision may name (build39 section 2), stable slugs, in the form's order. A decision for devices past support never
# offers network_removed: a device recorded "Not connected here" needs no decision (NETWORK_REMOVED_HINT says so on the form); slice
# 40's advisory decisions offer it.
NETWORK_REMOVED, SEGMENTED_CONTROL, RECORDED_ELSEWHERE = "network_removed", "segmented", "recorded_elsewhere"
CONTROLS = {
    "segmented": "On a medical device network segment",
    "traffic_restricted": "Traffic limited to the ports and hosts it needs (firewall or access list)",
    "no_internet": "No internet access in either direction",
    "network_removed": "Taken off the network (used standalone)",
    "feature_disabled": "A connectivity feature turned off (for example, wireless)",
    "manufacturer_mitigation": "The manufacturer's published mitigation applied",
    "remote_access_controlled": "Vendor remote access off, or attended only",
    "malware_scan_at_service": "Malware scan when serviced, before reconnecting",
    "allowlisting": "Application allowlisting turned on",
    "services_disabled": "Unused ports and services turned off, per the manufacturer's guide",
    "monitored": "Network monitoring covers it (alerts on unusual traffic)",
    "physical_controls": "Physical access and removable media controlled",
    "recorded_elsewhere": "Controls recorded in IT's system (with its reference)",
}
SUPPORT_CONTROLS = {slug: words for slug, words in CONTROLS.items() if slug != NETWORK_REMOVED}
NETWORK_REMOVED_HINT = "Taken off the network? Record it on the device (Record facts): it then needs no decision."
CONTROLS_REFERENCE_HELP = "The change or exception number in IT's system (e.g. CHG0012345): the control's details stay there."

# Why a decision was recorded, as kinds (never states: an announced end that passes is the same reason). SECURITY_UPDATES is the
# release standing's kind: the manufacturer's security updates for the release, or (with no date of the release's own) the model's
# manufacturer support from slice 36 (MODEL_SUPPORT_ENDED: its support, or only its parts, ended), so its words name all three (review 39
# F11, skeptics S8: a model whose parts ended while its support continues is never said to be past its support)
SECURITY_UPDATES, OS_SUPPORT = "security_updates", "os_support"
REASONS = {
    SECURITY_UPDATES: "The manufacturer's security updates, support, or parts ended, or their end is announced",
    OS_SUPPORT: "The OS vendor's published support ended, or its end is announced",
}
# A reason that has ended for a device the decision governs and that the decision does not name: what is known, never when it ended
# against the decision (an end entered after the decision may be dated before it, and a device joining may have been past it for years)
ALSO_ENDED = {SECURITY_UPDATES: "The manufacturer's security updates, support, or parts have ended, and this decision does not name them",
              OS_SUPPORT: "The OS vendor's support has ended, and this decision does not name it"}

# The Decisions tab's sentence (never "requires", never "must")
HICP_DECISIONS = ("HHS 405(d) HICP 2023 recommends documenting mitigating controls and a timeline for resolving the risk, through the "
                  "organization's risk acceptance or exception process, when a system past its support stays in use. IT's exception stays "
                  "in IT's system; record its number here.")

ENDING_MONTHS = 12  # an end announced within this many months may be decided ahead (HHS 405(d) HICP 2023: "within the upcoming year")
REVIEW_SOON_DAYS = 30  # "Decisions to review within 30 days"
# The review date's defaults and caps (review_default, review_cap): product choices, not a rule
REVIEW_MONTHS, OUT_OF_USE_REVIEW_MONTHS, TEMPORARY_REVIEW_MONTHS = 12, 3, 3
REVIEW_CAP_MONTHS, OUT_OF_USE_CAP_MONTHS = 36, 12

# A support need's keys: past support (a decision answers it), an end announced within 12 months (information; a decision may be
# recorded), and the OS vendor's support ended while the manufacturer's security updates for the release continue (information only)
NEEDS, ENDING, EXTENDED = "needs", "ending", "extended"
DECIDABLE = (NEEDS, ENDING)
# A response line's kinds (build39 section 3, in its order)
UPDATE_OPEN, UPDATE_NOT_CONFIRMED, DECIDED, NO_DECISION = "update_open", "update_not_confirmed", "decision", "no_decision"
NO_DECISION_WORDS = "No decision recorded"
# A decision's scope kinds
DEVICE_SCOPE, RELEASE_SCOPE, MODEL_SCOPE = "device", "release", "model"
SCOPE_LABELS = {DEVICE_SCOPE: "One device", RELEASE_SCOPE: "A model's devices of ours on one release",
                MODEL_SCOPE: "Every device of ours of a model"}
# A decision's states (decision_state) and their words
IN_FORCE, REVIEW_SOON, REVIEW_DUE, ENDED = "in_force", "review_soon", "review_due", "ended"
DECISION_STATES = (IN_FORCE, REVIEW_SOON, REVIEW_DUE, ENDED)
DECISION_STATE_WORDS = {IN_FORCE: "In force", REVIEW_SOON: "Review within 30 days", REVIEW_DUE: "Review due", ENDED: "Ended"}
ON_SITE_STATUSES = tuple(s for s in AssetStatus.values if s != AssetStatus.RETIRED)  # on_site's devices, as a positive condition

# Check lines (decision_checks; information, never refusals)
CHECK_ALSO_ENDED, CHECK_SEGMENT, CHECK_SEGMENT_SOME, CHECK_SEGMENT_BLANK = "also_ended", "segment", "segment_some", "segment_blank"
CHECK_NO_DEVICE, CHECK_JOINED, CHECK_PLAN_PASSED, CHECK_IN_SERVICE, CHECK_OWNER = (
    "no_device", "joined", "plan_passed", "in_service", "owner_inactive")
SEGMENT_NONE_WORDS = ("The model's record now says its devices are not on a separate segment, and this decision relies on a medical "
                      "device network segment")
SEGMENT_SOME_WORDS = "Only some of its devices are on a segment, as the model's record says"
SEGMENT_BLANK_WORDS = "The model's record does not say whether its devices are on a segment"
NO_DEVICE_WORDS = "Governs no device here now: end it"
PLAN_PASSED_WORDS = "The plan's date ({day}) has passed"
OWNER_INACTIVE_WORDS = "The plan owner's account is deactivated"

# Needs attention (build39 section 7): each heading's words, in the panel's order
ATTENTION_HEADINGS = {
    "no_decision": ("Security updates, manufacturer support or parts, or OS support ended, connected or able to connect: no decision "
                    "recorded"),
    "temporary": "Rentals, loaners, and demo units past their support, connected or able to connect: no decision recorded",
    "not_confirmed": "Updates done, version not confirmed",
    "ending": "Ending within 12 months, no decision recorded",
    "review_due": "Decisions past their review date",
    "review_soon": "Decisions to review within 30 days",
    "no_device": "Decisions that govern no device here now",
    "checks": "Checks",
}
ATTENTION_INFO = ("ending",)  # information only
ATTENTION_EMPTY = "Nothing listed from what is recorded here."
BADGE_TITLE = "Decisions past their review date"
# The pages the panel links to, by URL name (apps.web's; the doors reverse them): a decision's page (its number), Record decision (the
# scope in the query: asset=<tag>, release=<id>, model=<id>, or supersedes=<number> for Review and renew), End (its number), a model's
# panel on the Device security screen (with the Fleet's filters), a device's drawer (tab=security), a work order
URLS = {"decision": "web:security_decision", "record": "web:security_decision_new", "end": "web:security_decision_end",
        "model": "web:cybersecurity_model", "device": "web:asset", "work_order": "web:wo",
        # slice 40: an advisory's page and the Advisories tab; slice 38's update doors (opened from an advisory)
        "advisory": "web:security_advisory", "advisories": "web:security_advisories", "decisions": "web:security_decisions",
        "open_update": "web:cybersecurity_update_open", "open_updates": "web:cybersecurity_release_open_updates"}


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _short(words: str) -> str:
    """A control's words without their closing explanation in parentheses (a decision's sentence lists them in parentheses already)."""
    return words.rsplit(" (", 1)[0] if words.endswith(")") else words


def _joined(lines, *, lower: bool = False) -> str:
    """Standing lines in one sentence: "Security updates ended after ...; the OS vendor's published support ..."."""
    return "; ".join(_lower_first(w) if (lower or i) else w for i, w in enumerate(lines))


def _tags(tags, shown: int = 5) -> str:
    tags = list(tags)
    more = len(tags) - shown
    return ", ".join(tags[:shown]) + (f" and {more} more" if more > 0 else "")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# --- a device's support need ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Need:
    """A device's support need (support_need): `key` NEEDS (past support: a decision answers it), ENDING (an end announced within 12
    months: information, and a decision may be recorded), or EXTENDED (the OS vendor's support ended while the manufacturer's security
    updates for the release continue: information only). `reasons`: the kinds that hold (ended, or announced to end within 12 months);
    `ended`: those that ended; `lines`: the standings' own words; `words`: the line in one sentence; `day`: the earliest announced end
    (ENDING, EXTENDED)."""

    key: str
    reasons: tuple
    ended: tuple
    lines: tuple
    words: str
    day: date | None = None

    @property
    def decidable(self) -> bool:
        return self.key in DECIDABLE


def support_need(asset_standing, today: date) -> Need | None:
    """A device's support need from its standing (device_standing / standings), or None (build39 section 3). Only a device on site whose
    connection is in ON_SITE_KEYS (connected here, or able to connect as disclosed) has one:
    - NEEDS: its release standing is in ENDED_KEYS (the manufacturer's security updates ended, or the model's support), or its OS
      standing is OS_ENDED while the release standing is not UPDATES_END_ANNOUNCED;
    - ENDING: else UPDATES_END_ANNOUNCED or OS_END_ANNOUNCED on a day within 12 months (ENDING_MONTHS): "Ending within 12 months: ...";
    - EXTENDED: else the OS vendor's support ended while the manufacturer's security updates continue (announced later):
      "The OS vendor's support ended; the manufacturer's security updates for 5.2 (demo) end Mar 31, 2028 (announced)".
    Its reasons are kinds (REASONS): those ended, or announced to end within 12 months. No query."""
    asset = asset_standing.asset
    if asset.status == AssetStatus.RETIRED or asset_standing.connection.key not in ON_SITE_KEYS:
        return None
    sec, os_ = asset_standing.security, asset_standing.os
    horizon = add_months(today, ENDING_MONTHS)
    sec_ended = sec.key in ENDED_KEYS
    sec_soon = sec.key == UPDATES_END_ANNOUNCED and sec.day is not None and sec.day <= horizon
    os_ended = os_.key == OS_ENDED
    os_soon = os_.key == OS_END_ANNOUNCED and os_.day is not None and os_.day <= horizon
    held = ((SECURITY_UPDATES, sec, sec_ended, sec_soon), (OS_SUPPORT, os_, os_ended, os_soon))
    reasons = tuple(kind for kind, _s, gone, soon in held if gone or soon)
    ended = tuple(kind for kind, _s, gone, _soon in held if gone)
    lines = tuple(s.words for _kind, s, gone, soon in held if gone or soon)
    if sec_ended or (os_ended and sec.key != UPDATES_END_ANNOUNCED):
        return Need(NEEDS, reasons, ended, lines, _joined(lines))
    if sec_soon or os_soon:
        day = min(s.day for _kind, s, _gone, soon in held if soon)
        return Need(ENDING, reasons, ended, lines, f"Ending within 12 months: {_joined(lines, lower=True)}", day)
    if os_ended:  # the manufacturer's security updates for the release continue, announced to end later than 12 months from now
        release = asset_standing.release
        version = f" for {release.version}" if release is not None else ""
        words = f"The OS vendor's support ended; the manufacturer's security updates{version} end {day_words(sec.day)} (announced)"
        return Need(EXTENDED, (), ended, (os_.words, sec.words), words, sec.day)
    return None


def need_unknown(asset_standing, reasons=None) -> bool:
    """Whether what a device's support need turns on is not recorded (review 39 skeptics S3): its connection (neither its own fact nor its
    model's disclosure), or, connected or able to connect, both the end of the manufacturer's security updates (nor the model's support)
    and the OS vendor's support (or no OS). For a decision being renewed (`reasons`: the kinds it names; skeptics T2), also the fact
    each of its reasons rests on: SECURITY_UPDATES while the end of the manufacturer's security updates is not recorded (nor the model's
    support), OS_SUPPORT while the OS vendor's support is not recorded, whatever the other line records (a profile recording the OS for
    every version, read once a release's own support facts are cleared). Such a device has no need as recorded, yet nothing says it needs
    no decision: Record decision says to record it first (decisions.refusal_for's UNKNOWN), and Needs attention never offers End for a
    decision because of it. No query."""
    connection = asset_standing.connection.key
    if connection == CONNECTION_NOT_RECORDED:
        return True
    if connection not in ON_SITE_KEYS:
        return False
    security = asset_standing.security.key == SECURITY_NOT_RECORDED
    os_unknown = asset_standing.os.key == OS_NOT_RECORDED
    named = tuple(reasons or ())
    return ((security and (os_unknown or asset_standing.os.key == NO_OS)) or (security and SECURITY_UPDATES in named)
            or (os_unknown and OS_SUPPORT in named))


# --- the governing decision ----------------------------------------------------------------------------------------------------------

# A need's key (slice 40, build40 section 5.1): (applies_to, advisory id): (support_ended, None) for a device past support, (advisory, the
# advisory's id) for the devices an advisory names. Every reader of decisions reads one key's at a time: a decision governs only the
# need of its own key.
SUPPORT_KEY = (AppliesTo.SUPPORT_ENDED.value, None)


def key_of(decision) -> tuple:
    """The decision's need key: (applies_to, advisory id or None)."""
    return (decision.applies_to, decision.advisory_id)


def advisory_key(advisory) -> tuple:
    """The need key of an advisory's devices."""
    return (AppliesTo.ADVISORY.value, getattr(advisory, "pk", advisory))


def _live(key: tuple = SUPPORT_KEY):
    """Live (not ended) decisions of one need key (devices past support by default)."""
    applies_to, advisory_id = key
    rows = DeviceSecurityDecision.objects.filter(applies_to=applies_to, ended_on__isnull=True)
    return rows.filter(advisory_id=advisory_id) if advisory_id is not None else rows.filter(advisory__isnull=True)


def _live_all():
    """Every live (not ended) decision, of every key (group them with _by_key before governing)."""
    return DeviceSecurityDecision.objects.filter(ended_on__isnull=True)


def _by_key(decisions) -> dict:
    """{need key: [decisions]} (in the order given)."""
    out: dict = {}
    for d in decisions:
        out.setdefault(key_of(d), []).append(d)
    return out


_DECISION_RELATED = ("asset", "device_model", "release", "plan_owner", "recorded_by", "advisory")


def scope_kind(decision) -> str:
    """DEVICE_SCOPE, RELEASE_SCOPE, or MODEL_SCOPE (the database's one shape)."""
    if decision.asset_id:
        return DEVICE_SCOPE
    return RELEASE_SCOPE if decision.release_id else MODEL_SCOPE


def _recorded_release_id(asset):
    """The device's recorded release id as release_of reads it (a versioned, not removed release of its model), from its row already
    read (_devices_with_records), or None."""
    record = _cached_record(asset)
    release = record.release if record not in (None, _UNREAD) and record.release_id else None
    return release.pk if _names(release, asset) else None


def _governing(devices, decisions) -> dict:
    """{device id: the most specific live decision whose scope holds it, or None}: its own (device scope); else, for a device of ours on
    site, its recorded release's (release scope), else its model's (model scope). A temporary device is governed only by its own.
    `decisions` are of one need key (slice 40): given two keys it raises ValueError, never reading one key's decision as another's."""
    if len({key_of(d) for d in decisions}) > 1:
        raise ValueError("decisions of several need keys: govern each key apart (rules._by_key)")
    by_asset = {d.asset_id: d for d in decisions if d.asset_id}
    by_release = {d.release_id: d for d in decisions if d.release_id}
    by_model = {d.device_model_id: d for d in decisions if not d.asset_id and not d.release_id}
    out = {}
    for a in devices:
        found = by_asset.get(a.pk)
        if found is None and a.ownership == Ownership.OWNED and a.status != AssetStatus.RETIRED:
            release_id = _recorded_release_id(a)
            found = by_release.get(release_id) if release_id is not None else None
            if found is None:
                found = by_model.get(a.device_model_id)
        out[a.pk] = found
    return out


def _decisions_for(devices, key: tuple = SUPPORT_KEY) -> list:
    """The live decisions of `key` that may govern `devices` (their own, or their models' release and model scopes), with what their
    words read. One query."""
    model_ids = {a.device_model_id for a in devices}
    return list(_live(key).filter(Q(asset_id__in=[a.pk for a in devices]) | Q(asset__isnull=True, device_model_id__in=model_ids))
                .select_related(*_DECISION_RELATED).order_by("number"))


def governing_decisions(assets, today: date, key: tuple = SUPPORT_KEY) -> dict:
    """{asset id: the governing live decision, or None} for the devices of `assets` (a queryset or a list): the most specific live
    support decision whose scope holds it, whether or not past its review (build39 section 3): its own (device scope); else, only for a
    device of ours on site, the one for its recorded release (release scope), else the one for its model (model scope). A temporary
    device (rental, loaner, demo unit) is governed only by its own: a release or model decision describes where the facility put its own
    devices. Never a fallback past a lapsed decision: a decision past review still governs (and covers nothing: covers). `today` is
    unused (every live decision governs). Fixed queries: the devices with their rows (one for a queryset; none for a list that has
    them, as standings leaves it), the decisions (one)."""
    devices = _devices_with_records(assets)
    if not devices:
        return {}
    return _governing(devices, _decisions_for(devices, key))


def covers(decision, today: date) -> bool:
    """Whether a governing decision covers the need: live, its review on or after today."""
    return decision is not None and decision.ended_on is None and decision.review_by >= today


# --- the responses to a need -----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Response:
    """One response line (build39 section 3): `kind` UPDATE_OPEN, UPDATE_NOT_CONFIRMED, DECIDED, or NO_DECISION; `words` the line;
    `number` the work order's or the decision's (a door links a work order's only with Work orders View); `update` the SoftwareUpdate
    (Confirm version's door); `decision` the governing decision; `covers` whether that decision covers the need now."""

    kind: str
    words: str
    number: str = ""
    update: object | None = None
    decision: object | None = None
    covers: bool = False


def _target_profile(row):
    """The target release's model's profile as an unsaved row holding only its support group (annotated on the update's query), or
    None when the model has none."""
    if row.p_id is None:
        return None
    return SoftwareRelease(id=row.p_id, device_model_id=row.target_release.device_model_id, version_key="", removed_on=None,
                           **{name: getattr(row, f"p_{name}") for name in SUPPORT_GROUP})


def supported_target(release, profile_row, today: date) -> bool:
    """A release an update to which answers a support need: versioned, not entered in error, its release standing not in ENDED_KEYS and
    its OS standing not OS_ENDED (build39 section 3). Reads release.device_model (slice 36's model support)."""
    if release is None or not release.version_key or release.removed_on is not None:
        return False
    merged = facts(release, profile_row)
    return (release_standing(merged, release.device_model, today).key not in ENDED_KEYS
            and os_standing(merged, today).key != OS_ENDED)


def update_responses(assets, today: date) -> dict:
    """{asset id: [Response]} for the devices of `assets` (a list of devices or their ids: each maps, [] for none; or a queryset: only
    the devices with a response map, so read with .get(pk, [])): build39 section 3's update responses, open ones by number, then those
    not confirmed by number:
    - UPDATE_OPEN: an open update work order (open, in progress, or awaiting parts), its record holding a target release that is
      supported (supported_target: never ended, its OS not ended): "Update open to 5.6 (demo): WO-26-0042". Never updates.open_number
      alone (it counts any update).
    - UPDATE_NOT_CONFIRMED: an update work order completed or closed with nothing recorded (update_state NOT_CONFIRMED), to a supported
      release, as an open one: "Update done Oct 20, 2026, version not confirmed: Confirm version (WO-26-0041)". Its own state, never "no
      response". An update to a release past its support, or of the manufacturer's patches (the version stays), answers no support need.
    One query for any number of devices: the records with their work orders, targets, and the targets' models, the models' profiles'
    support fields annotated."""
    from apps.workorders.models import OPEN_STATUSES, WoStatus

    from .models import SoftwareUpdate

    if isinstance(assets, QuerySet):
        out: dict = {}
        which = Q(asset_id__in=assets.values("pk"))
    else:
        ids = [getattr(a, "pk", a) for a in assets]
        out = {pk: [] for pk in ids}
        if not ids:
            return out
        which = Q(asset_id__in=ids)
    profile_rows = SoftwareRelease.objects.filter(device_model_id=OuterRef("target_release__device_model_id"), version_key="",
                                                  removed_on__isnull=True).order_by("pk")
    annotations = {f"p_{name}": Subquery(profile_rows.values(name)[:1]) for name in SUPPORT_GROUP}
    rows = (SoftwareUpdate.objects.filter(which, work_order__isnull=False, target_release__isnull=False)
            .filter(Q(work_order__status__in=OPEN_STATUSES)
                    | Q(work_order__status__in=(WoStatus.COMPLETED, WoStatus.CLOSED), outcome=""))
            .select_related("work_order", "target_release", "target_release__device_model")
            .annotate(p_id=Subquery(profile_rows.values("pk")[:1]), **annotations)
            .order_by("work_order__number"))
    opened, done = {}, {}
    for row in rows:
        if not supported_target(row.target_release, _target_profile(row), today):
            continue
        wo = row.work_order
        if wo.status in OPEN_STATUSES:
            opened.setdefault(row.asset_id, []).append(
                Response(UPDATE_OPEN, f"Update open to {release_name(row.target_release)}: {wo.number}", wo.number, update=row))
        else:
            day = f" {day_words(wo.completed_on)}" if wo.completed_on else ""
            done.setdefault(row.asset_id, []).append(
                Response(UPDATE_NOT_CONFIRMED, f"Update done{day}, version not confirmed: Confirm version ({wo.number})", wo.number,
                         update=row))
    for pk in set(opened) | set(done) | set(out):
        out[pk] = opened.get(pk, []) + done.get(pk, [])
    return out


# --- words ---------------------------------------------------------------------------------------------------------------------------

_CONTROL_ORDER = {slug: i for i, slug in enumerate(CONTROLS)}


def _ordered(controls) -> list:
    return sorted(controls or (), key=lambda s: _CONTROL_ORDER.get(s, len(_CONTROL_ORDER)))


def control_words(decision) -> list:
    """The decision's controls in words, in the form's order (recorded_elsewhere with its reference: "Controls recorded in IT's system
    (CHG0012345)")."""
    return [f"Controls recorded in IT's system ({decision.controls_reference})"
            if slug == RECORDED_ELSEWHERE and decision.controls_reference else CONTROLS.get(slug, slug) for slug in _ordered(decision.controls)]


def _sentence_controls(decision) -> list:
    """The controls as a decision's sentence lists them: lower case, without their explanations ("traffic limited to the ports and
    hosts it needs"), IT's reference kept ("controls recorded in IT's system (CHG0012345)")."""
    out = []
    for slug in _ordered(decision.controls):
        if slug == RECORDED_ELSEWHERE:
            ref = f" ({decision.controls_reference})" if decision.controls_reference else ""
            out.append(f"controls recorded in IT's system{ref}")
        else:
            out.append(_lower_first(_short(CONTROLS.get(slug, slug))))
    return out


def reason_words(decision) -> list:
    """The reasons the decision names, in words (REASONS: kinds, never states)."""
    return [words for kind, words in REASONS.items() if kind in (decision.reasons or ())]


def scope_words(decision, count: int | None = None) -> str:
    """ "CE-10002" (a device), "Alaris 8015 PCU on 5.2 (demo)" (a release: ": 11 devices of ours on site" with `count`), "Every Alaris
    8015 PCU of ours" (a model: ": 23 on site" with `count`)."""
    kind = scope_kind(decision)
    if kind == DEVICE_SCOPE:
        return decision.asset.tag
    model = decision.device_model.model
    if kind == RELEASE_SCOPE:
        text = f"{model} on {release_name(decision.release)}"
        return text if count is None else f"{text}: {_plural(count, 'device', 'devices')} of ours on site"
    text = f"Every {model} of ours"
    return text if count is None else f"{text}: {count} on site"


def plan_words(decision, *, fy_start: int | None = None) -> str:
    """ "Plan: update by Mar 1, 2027 (owner Dana Ruiz)", "Plan: replace, planned for FY2028" (the fiscal year read now from plan_by and
    the facility's start month: never stored), "Plan: wait for the manufacturer", "Plan: return it to its owner", or "No plan recorded".
    Never "due" or "deadline". `fy_start` None reads the facility's start month (one query) for a replacement planned."""
    plan = decision.plan
    if plan == DecisionPlan.NONE or not plan:
        return "No plan recorded"
    if plan == DecisionPlan.UPDATE_PLANNED:
        text = f"Plan: update by {day_words(decision.plan_by)}" if decision.plan_by else "Plan: update"
    elif plan == DecisionPlan.REPLACE_PLANNED:
        if decision.plan_by:
            start = fy_start if fy_start is not None else fiscal.start_month()
            text = f"Plan: replace, planned for {fiscal.fy_label(fiscal.fiscal_year_of(decision.plan_by, start))}"
        else:
            text = "Plan: replace"
    elif plan == DecisionPlan.WAIT_FOR_MANUFACTURER:
        text = "Plan: wait for the manufacturer"
    else:
        text = "Plan: return it to its owner"
    return f"{text} (owner {decision.plan_owner})" if decision.plan_owner_id else text


def who_words(decision, *, day: bool = True) -> str:
    """ "Accepted by IT security or the CISO on Oct 21, 2026, as recorded by Kim Lee; IT exception DEMO-EX-1042" (or "...; no IT
    exception number recorded"); "Decided by ..." to take out of use. The day is the day decided, said of the decision, never of the
    recording: Record decision takes a day decided in the past (an exception that already lapsed), and the day it was recorded is its
    History's (review 39 F8). Nobody recording (the system) says no recorder. `day` False leaves the day out (decision_words, to take out
    of use: its sentence opens "Take out of use, decided Oct 21, 2026")."""
    verb = who_verb(decision)
    when = f" on {day_words(decision.decided_on)}" if day else ""
    recorder = f", as recorded by {decision.recorded_by}" if decision.recorded_by_id else ""
    exception = (f"IT exception {decision.exception_reference}" if decision.exception_reference
                 else "no IT exception number recorded")
    return f"{verb} {AcceptedBy(decision.accepted_by).label}{when}{recorder}; {exception}"


DECIDED_RESPONSES = (DecisionResponse.TAKE_OUT_OF_USE, DecisionResponse.NOT_IN_USE_HERE)  # "Decided by", never "Accepted by"


def who_verb(decision) -> str:
    """ "Decided by" to take out of use and for "not in use here" (slice 40), else "Accepted by": the one place the verb is decided."""
    return "Decided by" if decision.response in DECIDED_RESPONSES else "Accepted by"


def _past_review(decision, today: date) -> bool:
    return decision.ended_on is None and decision.review_by < today


def decision_words(decision, today: date, *, fy_start: int | None = None) -> str:
    """The decision's sentence (build39 section 4): "<response>; <review>. <plan>. <who>.":
    - keep with controls: "Keep in use with controls (2: traffic limited to the ports and hosts it needs; vendor remote access off, or
      attended only); review by Oct 21, 2027."
    - keep without added controls: "Keep in use without added controls: risk accepted until Oct 21, 2027." (the only place "risk
      accepted" appears)
    - take out of use: "Take out of use, decided Oct 21, 2026; review by Jan 21, 2027." (never "out of use" as a device's state: the
      decision retires nothing)
    - not in use here (slice 40, an advisory's devices): "Decided Oct 21, 2026 that the part it names is not in use here; review by Oct
      21, 2027. Decided by the CE leader, as recorded by Kim Lee; no IT exception number recorded." (no plan part: it has none)
    Past its review (a live decision, review_by before today), "Review due since Oct 21, 2027" is read first and replaces the review part
    (and "risk accepted until" is dropped): "Review due since Oct 21, 2027: keep in use without added controls." Then plan_words and
    who_words (without the day to take out of use: its first part says it). `fy_start`: the facility's fiscal year start month, read
    once by the caller (else read here, when a replacement is planned)."""
    review = day_words(decision.review_by)
    response = decision.response
    if response == DecisionResponse.KEEP_WITH_CONTROLS:
        controls = _sentence_controls(decision)
        head, tail = f"Keep in use with controls ({len(controls)}: {'; '.join(controls)})", f"; review by {review}"
    elif response == DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS:
        head, tail = "Keep in use without added controls", f": risk accepted until {review}"
    elif response == DecisionResponse.NOT_IN_USE_HERE:  # slice 40: an advisory's devices; no plan (the template drops its part)
        head, tail = (f"Decided {day_words(decision.decided_on)} that the part it names is not in use here", f"; review by {review}")
    else:
        head, tail = f"Take out of use, decided {day_words(decision.decided_on)}", f"; review by {review}"
    first = f"Review due since {review}: {_lower_first(head)}." if _past_review(decision, today) else f"{head}{tail}."
    who = who_words(decision, day=response not in DECIDED_RESPONSES)  # their sentences say the day decided first
    if response == DecisionResponse.NOT_IN_USE_HERE:
        return f"{first} {who}."
    return f"{first} {plan_words(decision, fy_start=fy_start)}. {who}."


def decision_state(decision, today: date) -> str:
    """ENDED (superseded, no longer needed, or entered in error), REVIEW_DUE (its review before today), REVIEW_SOON (its review within
    REVIEW_SOON_DAYS days from today), or IN_FORCE. Words: DECISION_STATE_WORDS."""
    if decision.ended_on is not None:
        return ENDED
    if decision.review_by < today:
        return REVIEW_DUE
    if decision.review_by <= today + timedelta(days=REVIEW_SOON_DAYS):
        return REVIEW_SOON
    return IN_FORCE


END_WORDS = {DecisionEnd.SUPERSEDED: "Superseded {day}", DecisionEnd.NO_LONGER_NEEDED: "Ended {day}: no longer needed",
             DecisionEnd.ENTERED_IN_ERROR: "Entered in error {day}"}


def end_words(decision) -> str:
    """ "Superseded Oct 22, 2026", "Ended Oct 22, 2026: no longer needed", "Entered in error Oct 22, 2026", or "" (live)."""
    if decision.ended_on is None:
        return ""
    return END_WORDS.get(decision.end_reason, "Ended {day}").format(day=day_words(decision.ended_on))


def review_words(decision, today: date) -> str:
    """The list's review column: "Review by Oct 21, 2027", "Review due since Oct 21, 2027", or (ended) end_words."""
    if decision.ended_on is not None:
        return end_words(decision)
    if decision.review_by < today:
        return f"Review due since {day_words(decision.review_by)}"
    return f"Review by {day_words(decision.review_by)}"


# --- review dates --------------------------------------------------------------------------------------------------------------------

def review_cap(response: str, decided_on: date) -> date:
    """The latest review day Cadence accepts: 12 months after the day decided to take out of use (a "take out of use" that leaves a device
    in use for years is a keep-in-use decision under another name), 36 months for the others. Product choices, not a rule: no rule sets
    an interval (HHS 405(d) HICP 2023 recommends a timeline and a review, and names none)."""
    months = OUT_OF_USE_CAP_MONTHS if response == DecisionResponse.TAKE_OUT_OF_USE else REVIEW_CAP_MONTHS
    return add_months(decided_on, months)


def review_default(response: str, decided_on: date, *, asset=None) -> date:
    """The review day Record decision offers: 12 months after the day decided; 3 months to take out of use; for a temporary device
    (rental, loaner, demo unit), the earlier of that and its due back day (when after the day decided), else 3 months; never past
    review_cap. Product choices, not a rule (review_cap's docstring)."""
    months = OUT_OF_USE_REVIEW_MONTHS if response == DecisionResponse.TAKE_OUT_OF_USE else REVIEW_MONTHS
    default = add_months(decided_on, months)
    if asset is not None and owner_maintains(asset):
        due_back = getattr(asset, "due_back_on", None)
        stay = due_back if due_back is not None and due_back > decided_on else add_months(decided_on, TEMPORARY_REVIEW_MONTHS)
        default = min(default, stay)
    return min(default, review_cap(response, decided_on))


def review_rule_words(response: str, *, asset=None) -> str:
    """The review field's words on Record decision, review_default's rule as it computes it: "By default 12 months after the day decided
    (at most 36 months)"; for a temporary device (review 39 F3, F14), "By default 12 months after the day decided, or the day it is due
    back when that is after the day decided and comes first; 3 months when no due back day after the day decided is recorded (at most 36
    months)" (to take out of use, whose default is those 3 months already: "By default 3 months after the day decided, or the day it is
    due back when that is after the day decided and comes first (at most 12 months)")."""
    cap = OUT_OF_USE_CAP_MONTHS if response == DecisionResponse.TAKE_OUT_OF_USE else REVIEW_CAP_MONTHS
    months = OUT_OF_USE_REVIEW_MONTHS if response == DecisionResponse.TAKE_OUT_OF_USE else REVIEW_MONTHS
    if asset is not None and owner_maintains(asset):
        none = (f"; {TEMPORARY_REVIEW_MONTHS} months when no due back day after the day decided is recorded"
                if TEMPORARY_REVIEW_MONTHS < months else "")
        return (f"By default {months} months after the day decided, or the day it is due back when that is after the day decided and "
                f"comes first{none} (at most {cap} months)")
    return f"By default {months} months after the day decided (at most {cap} months)"


# --- a decision's devices and Check lines --------------------------------------------------------------------------------------------

def scope_devices(*, asset=None, device_model=None, release=None):
    """The devices on site a scope holds (a queryset; rows or ids): a device's own (device scope); a model's devices of ours whose
    recorded release is `release` (release scope); a model's devices of ours (model scope)."""
    on = Asset.objects.filter(status__in=ON_SITE_STATUSES)
    if asset is not None:
        return on.filter(pk=getattr(asset, "pk", asset))
    model_id = getattr(device_model, "pk", device_model)
    if release is not None:
        return on.filter(ownership=Ownership.OWNED, device_model_id=model_id, cyber__release_id=getattr(release, "pk", release))
    return on.filter(ownership=Ownership.OWNED, device_model_id=model_id)


def _scope_of(decision):
    return scope_devices(asset=decision.asset_id or None, device_model=decision.device_model_id, release=decision.release_id or None)


def in_scope(decision, asset) -> bool:
    """Whether `decision`'s scope holds `asset` (its row read) on site now (scope_devices, in Python): the API's ?device= filter."""
    return _in_scope(decision, asset)


def decisions_now(decisions, today) -> dict:
    """{decision id: (governs, checks)} for a page of decisions (the API's list), as decision_devices and decision_checks read one: `governs`
    the devices on site it governs now (in its scope, and it the most specific live decision there: rules' governing), in tag order;
    `checks` its Check lines (rules.Check). An ended decision governs nothing and has no Check line: ((), ()). Composed of the rules' own
    pieces as needs_attention composes them (the scope, the governing decision, the standings, the Check lines; the update
    responses are not read: neither answer reads them), so a page reads a fixed number of queries however many decisions and devices:
    the devices on site of the live decisions' models with their rows (one), the models' profiles (one), the live decisions that may
    govern those devices (one); none when no decision on the page is live. A test holds every row to decision_devices and
    decision_checks."""
    out = {d.pk: ((), ()) for d in decisions}
    live = [d for d in decisions if d.ended_on is None]
    if not live:
        return out
    model_ids = {_model_of(d) for d in live}
    devices = _devices_with_records(on_site().filter(device_model_id__in=model_ids).order_by("tag", "pk"))
    profiles = _profiles(model_ids)
    governing = _governing(devices, _decisions_for(devices)) if devices else {}
    by_model: dict = {}
    for a in devices:
        by_model.setdefault(a.device_model_id, []).append(a)
    for d in live:
        model_id = _model_of(d)
        held = [a for a in by_model.get(model_id, ()) if _in_scope(d, a)]
        found = [_decision_device(a, _standing(a, _cached_record(a), profiles.get(a.device_model_id), today), today,
                                        governing, d, {}) for a in held]
        out[d.pk] = (tuple(dd.asset for dd in found if dd.governed), tuple(_checks(d, found, profiles.get(model_id), today)))
    return out


def _in_scope(decision, a) -> bool:
    """Whether the device `a` (its row read) is on site in the decision's scope (scope_devices, in Python)."""
    if a.status == AssetStatus.RETIRED:
        return False
    if decision.asset_id:
        return a.pk == decision.asset_id
    if a.ownership != Ownership.OWNED or a.device_model_id != decision.device_model_id:
        return False
    return not decision.release_id or _recorded_release_id(a) == decision.release_id


@dataclass(frozen=True)
class DecisionDevice:
    """A device on site in a decision's scope (decision_devices): its standing, its need (or None), its update responses, the decision
    governing it, and whether that is this decision (`governed`; else a more specific one governs it)."""

    asset: object
    standing: DeviceStanding
    need: Need | None
    updates: tuple
    governing: object | None
    governed: bool

    def words(self, today: date) -> str:
        """The device's response on the decision's page: its update lines, then "This decision" (or "Review due since ..."), or the
        number of the decision that governs it instead."""
        parts = [r.words for r in self.updates]
        if self.governed:
            parts.append(f"Review due since {day_words(self.governing.review_by)}" if _past_review(self.governing, today)
                         else "This decision")
        elif self.governing is not None:
            parts.append(f"Governed by {self.governing.number}")
        return "; ".join(parts)


def _decision_device(a, standing, today, governing: dict, decision, responses: dict) -> DecisionDevice:
    gov = governing.get(a.pk)
    return DecisionDevice(asset=a, standing=standing, need=support_need(standing, today), updates=tuple(responses.get(a.pk, ())),
                          governing=gov, governed=gov is not None and gov.pk == decision.pk)


def decision_devices(decision, today: date) -> list:
    """The devices on site in the decision's scope (scope_devices), in tag order, each a DecisionDevice: its standing (tag, serial,
    model, department, connection, release), its need, its update responses, and the decision governing it (this one, `governed`, or a
    more specific one). Fixed queries however many devices: the devices with their rows, their models' profiles, the live decisions
    that may govern them, the update responses (four; one when the scope holds no device on site)."""
    devices = list(_scope_of(decision).select_related(*_WITH_RECORD).order_by("tag"))
    if not devices:
        return []
    profiles = _profiles(a.device_model_id for a in devices)
    governing = _governing(devices, _decisions_for(devices))
    responses = update_responses(devices, today)
    return [_decision_device(a, _standing(a, _cached_record(a), profiles.get(a.device_model_id), today), today, governing, decision,
                             responses) for a in devices]


_SPECIFIC = {MODEL_SCOPE: 0, RELEASE_SCOPE: 1, DEVICE_SCOPE: 2}  # how specific a scope is: the most specific live decision governs


def under(decision, devices: list) -> list:
    """The DecisionDevices of `devices` (decision_devices') the decision governs, or, ended, would govern recorded again for its scope
    now (none governing them, or a live decision no more specific than its scope): never a device a more specific live decision
    governs, as Record decision reads a scope (decisions.would_govern; skeptics T0). No query."""
    if decision.ended_on is None:
        return [dd for dd in devices if dd.governed]
    rank = _SPECIFIC[scope_kind(decision)]
    return [dd for dd in devices if dd.governing is None or _SPECIFIC[scope_kind(dd.governing)] <= rank]


def holding_lines(decision, devices: list) -> list:
    """A decision's "What holds now" (its page and print): the standing lines of the needs of the devices it governs (under), each once,
    in tag order; never a line that holds only for a device a more specific live decision governs (skeptics T0). No query."""
    out: list = []
    for dd in under(decision, devices):
        for line in dd.need.lines if dd.need is not None else ():
            if line not in out:
                out.append(line)
    return out


@dataclass(frozen=True)
class Check:
    """A Check line (build39 section 3): information, never a refusal. `key` one of the CHECK_ keys; `info` True for the lines that only
    describe (the model's segment record SOME or blank); `action` "end" where End answers it; `tags` the devices it names."""

    key: str
    words: str
    info: bool = False
    action: str = ""
    tags: tuple = ()


def _model_of(decision):
    """The decision's model: its device's model now (device scope; reads decision.asset), else its own."""
    return decision.asset.device_model_id if decision.asset_id else decision.device_model_id


def _checks(decision, devices: list, profile_row, today: date) -> list:
    """The Check lines from the decision's devices on site (DecisionDevices) and its model's profile row (or None). No query but the
    plan owner's row when the decision has not read it. A reason ended counts only from a decidable need (review 39 F0): the reasons a
    decision may name (decisions._check_reasons) are those; an extended one (the OS ended, the manufacturer's updates continuing) names
    none, so its end is information on the device, never a Check nobody could answer."""
    out = []
    governed = [dd for dd in devices if dd.governed]
    ended_now = {kind for dd in governed if dd.need is not None and dd.need.decidable for kind in dd.need.ended}
    for kind in REASONS:
        if kind in ended_now and kind not in (decision.reasons or ()):
            out.append(Check(CHECK_ALSO_ENDED, ALSO_ENDED[kind]))
    if SEGMENTED_CONTROL in (decision.controls or ()):
        segmented = profile_row.segmented if profile_row is not None else ""
        if segmented == Segmented.NONE:
            out.append(Check(CHECK_SEGMENT, SEGMENT_NONE_WORDS))
        elif segmented == Segmented.SOME:
            out.append(Check(CHECK_SEGMENT_SOME, SEGMENT_SOME_WORDS, info=True))
        elif not segmented:
            out.append(Check(CHECK_SEGMENT_BLANK, SEGMENT_BLANK_WORDS, info=True))
    if not devices:
        out.append(Check(CHECK_NO_DEVICE, NO_DEVICE_WORDS, action="end"))
    if scope_kind(decision) != DEVICE_SCOPE:
        known = {str(i) for i in (decision.governed_ids or ())}
        joined = [dd.asset.tag for dd in governed if str(dd.asset.pk) not in known]
        if joined:
            out.append(Check(CHECK_JOINED, f"{_plural(len(joined), 'device', 'devices')} joined since: {_tags(joined)}",
                             tags=tuple(joined)))
    if decision.plan_by is not None and decision.plan_by < today:
        out.append(Check(CHECK_PLAN_PASSED, PLAN_PASSED_WORDS.format(day=day_words(decision.plan_by))))
    if decision.response == DecisionResponse.TAKE_OUT_OF_USE and today > decision.decided_on:
        in_service = [dd.asset.tag for dd in governed if dd.asset.status == AssetStatus.IN_SERVICE]
        decided = day_words(decision.decided_on)
        if in_service and scope_kind(decision) == DEVICE_SCOPE:
            out.append(Check(CHECK_IN_SERVICE, f"Still in service since the decision to take it out of use ({decided})",
                             tags=tuple(in_service)))
        elif in_service:
            them = "it" if len(in_service) == 1 else "them"
            out.append(Check(CHECK_IN_SERVICE, f"{_tags(in_service)} still in service since the decision to take {them} out of use "
                                               f"({decided})", tags=tuple(in_service)))
    if decision.plan_owner_id and not decision.plan_owner.is_active:
        out.append(Check(CHECK_OWNER, OWNER_INACTIVE_WORDS))
    return out


def decision_checks(decision, today: date, *, devices=None) -> list:
    """The decision's Check lines (build39 section 3), each a Check: a reason ended, on a device it governs with a decidable need, that it
    does not name ("The OS vendor's support has ended, and this decision does not name it": never "since", review 39 F9); the model's
    record now says not on a separate segment while it relies on `segmented` (and, as information, only some or not recorded); no
    device on site in its scope ("Governs no device here now: end it"); devices it governs that it did not when recorded (release or
    model scope: "2 devices joined since: CE-10017, CE-10021", by identity from governed_ids); the plan's date passed ("The plan's date
    (Mar 1, 2027) has passed"); take out of use with a device it governs still in service a day or more after ("Still in service since
    the decision to take it out of use (Oct 21, 2026)"); the plan owner's account deactivated. An ended decision has none. `devices`:
    decision_devices' result when the caller has it (else read here). Fixed queries: decision_devices' (when not given), the model's
    profile (only when no device of its model was read and it relies on `segmented`), the decision's device and plan owner (when not
    read)."""
    if decision.ended_on is not None:
        return []
    if devices is None:
        devices = decision_devices(decision, today)
    model_id = _model_of(decision)
    profile_row = next((dd.standing.facts.profile for dd in devices if dd.asset.device_model_id == model_id), _UNREAD)
    if profile_row is _UNREAD:
        profile_row = profile(model_id) if SEGMENTED_CONTROL in (decision.controls or ()) else None
    return _checks(decision, devices, profile_row, today)


# --- the device drawer's Security tab --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceLines:
    """What the Security tab shows for slice 39 (device_lines): the device's standing and support need (or None); its response lines
    (build39 section 3's order: updates open, versions not confirmed, then the governing decision's sentence, or "No decision recorded"
    when nothing responds); the governing decision (or None) and whether it covers the need; whether this reader may record a decision
    for the device (`can_record`: permissions.can_decide, a decidable need on a device on site) and sees work orders
    (`link_work_orders`: a work order's number is a link only with Work orders View)."""

    standing: DeviceStanding
    need: Need | None
    responses: tuple
    decision: object | None
    covers: bool
    can_record: bool
    link_work_orders: bool


def device_lines(asset, today: date, *, user, fy_start: int | None = None) -> DeviceLines:
    """The Security tab's slice 39 lines for one device (DeviceLines): the need's words, or the "Ending within 12 months" or extended
    support information; with a decidable need, its update responses; the governing decision's sentence (decision_words: "Review due
    since ..." first once past review), shown with or without a need (it still governs); "No decision recorded" for a decidable need
    nothing responds to. `user` None is the system (it may record and sees work orders). Fixed queries: the device's row and profile
    (device_standing), the decisions (one), the update responses (one, only with a decidable need), the fiscal year start (one, only
    for a replacement planned when `fy_start` is not given)."""
    from apps.accounts.models import Level, Module

    from . import permissions as perms

    standing = device_standing(asset, today)
    device = standing.asset
    need = support_need(standing, today)
    decision = governing_decisions([device], today).get(device.pk)
    responses: list = []
    if need is not None and need.decidable:
        responses = list(update_responses([device], today).get(device.pk, ()))
    covering = covers(decision, today)
    if decision is not None:
        responses.append(Response(DECIDED, decision_words(decision, today, fy_start=fy_start), decision.number, decision=decision,
                                  covers=covering))
    elif need is not None and need.decidable and not responses:
        responses.append(Response(NO_DECISION, NO_DECISION_WORDS))
    can_record = (user is None or perms.can_decide(user)) and need is not None and need.decidable
    link = user is None or user.has_level(Module.WORKORDERS, Level.VIEW)
    return DeviceLines(standing=standing, need=need, responses=tuple(responses), decision=decision, covers=covering,
                       can_record=can_record, link_work_orders=link)


# --- Needs attention -----------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Link:
    """A link as data: a URL name (URLS), its args, and its query as (key, value) pairs; the doors reverse it."""

    name: str
    args: tuple = ()
    query: tuple = ()


@dataclass(frozen=True)
class AttentionRow:
    """One row of a Needs attention heading: its words; the devices it counts (`count`); where it links (`link`); Record decision
    (`record`) and End (`end`) for permissions.can_decide only; the work order's link for Work orders View only; the decision or
    device it names, and the devices' tags."""

    words: str
    count: int = 1
    link: Link | None = None
    record: Link | None = None
    end: Link | None = None
    work_order: Link | None = None
    decision: object | None = None
    asset: object | None = None
    tags: tuple = ()


@dataclass(frozen=True)
class Heading:
    key: str
    words: str
    count: int
    rows: tuple
    info: bool = False


@dataclass(frozen=True)
class Attention:
    """The Decisions tab's Needs attention: its headings with rows, in ATTENTION_HEADINGS' order (only those with rows); empty, the words
    ATTENTION_EMPTY ("Nothing listed from what is recorded here."), never a claim that everything is answered."""

    headings: tuple
    empty_words: str = ATTENTION_EMPTY

    @property
    def empty(self) -> bool:
        return not self.headings


def _model_link(model_id, need: Need, *, norelease: bool = False) -> Link:
    """A model's panel narrowed to devices of ours that can connect, by the need's standing (the Fleet's filters); `norelease` also to
    the devices with no release recorded (the row for them: review 39 F2)."""
    from .filters import NORELEASE

    if need.key == ENDING:
        standing = "updates_end_announced" if SECURITY_UPDATES in need.reasons and SECURITY_UPDATES not in need.ended else "os_end_announced"
    else:
        standing = "updates_ended" if SECURITY_UPDATES in need.ended else "os_ended"
    query = (("whose", "ours"), ("connected", "either"), ("standing", standing))
    return Link(URLS["model"], (model_id,), (*query, ("norelease", NORELEASE)) if norelease else query)


def _device_link(asset) -> Link:
    return Link(URLS["device"], (asset.tag,), (("tab", "security"),))


def _temporary_words(a, need: Need) -> str:
    """ "T-0101 (rental, Summit Medical Rentals; due back Nov 10, 2026): security updates ended after Sep 21, 2026; its owner installs
    updates: ask the owner to update it, or return it": the standings' own words (the need's), never a fixed "security updates ended"
    (the model's support or its parts may be what ended: review 39 F11)."""
    who = a.get_ownership_display().lower()
    if a.owner:
        who += f", {a.owner}"
    if a.due_back_on:
        who += f"; due back {day_words(a.due_back_on)}"
    ask = "ask the owner about it" if need.key == ENDING else "ask the owner to update it"
    return f"{a.tag} ({who}): {_lower_first(need.words)}; its owner installs updates: {ask}, or return it"


def _release_sort(a) -> str:
    return _cached_record(a).release.version_key if _recorded_release_id(a) is not None else "￿"


def _group_link(model_id, release_id, members: list, need: Need, *, standings: dict, by_model: dict) -> Link | None:
    """A group row's link: the model's panel narrowed by the Fleet's filters (_model_link) only where the panel, with exactly those
    filters, lists the row's devices and no other; else the device's Security tab when the row counts one device; else none (review 39
    skeptics S9, T1). The panel lists every device of the model on site that the filters keep (views_cybersecurity._panel_context:
    FleetFilters.keeps on each standing), whatever its release (the Fleet has no release filter; the row for devices with no release
    recorded adds norelease), so this compares the row's devices with that whole list (`by_model`: the model's devices on site). The list
    holds more than the row counts when a device with the row's standing is governed by a decision or answered by an update (the Fleet
    has no filter for either), is on another release (that release's row counts it, or a decision governs it), or, for an ending row, has
    its end announced more than 12 months away (the standing filter has no horizon: it needs no decision). No query (the standings
    already read)."""
    from .filters import parse

    link = _model_link(model_id, need, norelease=release_id is None)
    shown = parse(dict(link.query))
    listed = {a.pk for a in by_model.get(model_id, ()) if shown.keeps(standings[a.pk])}
    if listed == {a.pk for a in members}:
        return link
    return _device_link(members[0]) if len(members) == 1 else None


def _group_rows(groups: dict, needs: dict, *, may_decide: bool, standings: dict, by_model: dict) -> list:
    """Rows per model and release (devices of ours): "Alaris 8015 PCU on 5.2 (demo): 4 devices of ours; security updates ended after
    ...", linked to the model's panel narrowed by standing (with no release recorded, also to the devices with none) where the panel lists
    the row's devices and no other (_group_link), Record decision for the release (or, with no release recorded, the model)."""
    rows = []
    for (model_id, release_id), members in sorted(groups.items(), key=lambda kv: (kv[1][0].device_model.manufacturer.casefold(),
                                                                                    kv[1][0].device_model.model.casefold(),
                                                                                    _release_sort(kv[1][0]))):
        first = members[0]
        need = needs[first.pk]
        model = first.device_model.model
        where = f"{model} on {_cached_record(first).release.version}" if release_id is not None else f"{model}, release not recorded"
        record = None
        if may_decide:
            record = Link(URLS["record"], (), (("release", release_id),) if release_id is not None else (("model", model_id),))
        rows.append(AttentionRow(f"{where}: {_plural(len(members), 'device', 'devices')} of ours; {_lower_first(need.words)}",
                                 count=len(members), record=record, tags=tuple(a.tag for a in members),
                                 link=_group_link(model_id, release_id, members, need, standings=standings, by_model=by_model)))
    return rows


def _decision_row(d, words: str, *, held: int, may_decide: bool, renew: bool = False, end: bool = False) -> AttentionRow:
    scope = scope_words(d, held if scope_kind(d) != DEVICE_SCOPE and held else None)
    return AttentionRow(f"{d.number} · {scope}; {_lower_first(words)}", link=Link(URLS["decision"], (d.number,)), decision=d,
                        record=Link(URLS["record"], (), (("supersedes", d.number),)) if may_decide and renew else None,
                        end=Link(URLS["end"], (d.number,)) if may_decide and end else None)


def needs_attention(today: date, *, user) -> Attention:
    """The Decisions tab's Needs attention (build39 section 7), as data: each heading (ATTENTION_HEADINGS) with its count and rows, links
    as URL names and args (Link), no HTML. Only for permissions.can_view_devices (anyone else: an empty panel); `user` None is the system.
    - no_decision: devices of ours on site with a need (NEEDS), no open update to a supported release, no update done with the version
      not confirmed, and no decision governing them, one row per model and release; Record decision (release scope, or model scope with
      no release recorded) for permissions.can_decide.
    - temporary: each temporary device so, one row each ("T-0101 (rental, ...): security updates ended; its owner installs updates: ask
      the owner to update it, or return it"), Record decision (device scope).
    - not_confirmed: devices with a decidable need, an update done with the version not confirmed, and none open (Confirm version).
    - ending (information): devices with an end announced within 12 months, no decision governing them, and no update responding (ours
      per model and release, temporary devices each).
    - review_due: live decisions past review whose scope holds a device on site (the badge's count), Review and renew; End instead when
      no device it governs has a decidable need now (updated to a supported release, not connected here, each under a more specific
      decision, ...: Record decision would refuse to renew it, decisions.refusal_for; review 39 F15, skeptics T0), and neither while a
      device it governs has no need only because what it, or a reason the decision names, turns on is not recorded (need_unknown with
      the decision's reasons: its page says what to record first; skeptics S3, T2).
    - review_soon: live decisions to review within 30 days whose scope holds a device on site, Review and renew (End instead, or
      neither, as review_due).
    - no_device: live decisions whose scope holds no device on site, End.
    - checks: the other Check lines of live decisions whose scope holds a device on site (decision_checks', not the information ones).
    A device with a need and a decision past review is listed under its decision (review_due), never as "no decision recorded".
    Fixed queries however many devices and decisions: the devices on site with their rows, the live decisions, the models' profiles, the
    update responses (four)."""
    from apps.accounts.models import Level, Module

    from . import permissions as perms

    if user is not None and not perms.can_view_devices(user):
        return Attention(headings=())
    may_decide = user is None or perms.can_decide(user)
    sees_work = user is None or user.has_level(Module.WORKORDERS, Level.VIEW)
    devices = _devices_with_records(on_site())
    decisions = list(_live().select_related(*_DECISION_RELATED).order_by("number"))
    model_ids = {a.device_model_id for a in devices} | {_model_of(d) for d in decisions}
    profiles = _profiles(model_ids) if model_ids else {}
    standings = {a.pk: _standing(a, _cached_record(a), profiles.get(a.device_model_id), today) for a in devices}
    needs = {pk: support_need(s, today) for pk, s in standings.items()}
    governing = _governing(devices, decisions)
    responses = update_responses(devices, today) if devices else {}

    by_pk, by_model = {a.pk: a for a in devices}, {}
    for a in devices:
        by_model.setdefault(a.device_model_id, []).append(a)
    rows: dict = {key: [] for key in ATTENTION_HEADINGS}
    groups: dict = {"no_decision": {}, "ending": {}}
    for a in devices:  # tag order (Asset's ordering)
        need = needs[a.pk]
        if need is None or not need.decidable:
            continue
        kinds = {r.kind for r in responses.get(a.pk, ())}
        if UPDATE_NOT_CONFIRMED in kinds and UPDATE_OPEN not in kinds:
            line = next(r for r in responses[a.pk] if r.kind == UPDATE_NOT_CONFIRMED)
            rows["not_confirmed"].append(AttentionRow(f"{a.tag}: {line.words}", link=_device_link(a), asset=a, tags=(a.tag,),
                                                      work_order=Link(URLS["work_order"], (line.number,)) if sees_work else None))
        if kinds or governing.get(a.pk) is not None:
            continue  # an update responds, or a decision governs (one past review is listed under it: review_due)
        key = "no_decision" if need.key == NEEDS else "ending"
        if a.ownership != Ownership.OWNED:
            rows["temporary" if key == "no_decision" else key].append(
                AttentionRow(_temporary_words(a, need), link=_device_link(a), asset=a, tags=(a.tag,),
                             record=Link(URLS["record"], (), (("asset", a.tag),)) if may_decide else None))
            continue
        groups[key].setdefault((a.device_model_id, _recorded_release_id(a)), []).append(a)
    for key in ("no_decision", "ending"):
        rows[key] = _group_rows(groups[key], needs, may_decide=may_decide, standings=standings, by_model=by_model) + rows[key]

    for d in decisions:
        if d.asset_id:
            held = [by_pk[d.asset_id]] if d.asset_id in by_pk else []
        else:
            held = [a for a in by_model.get(d.device_model_id, ()) if _in_scope(d, a)]
        if not held:
            rows["no_device"].append(_decision_row(d, review_words(d, today), held=0, may_decide=may_decide, end=True))
            continue
        state = decision_state(d, today)
        if state in (REVIEW_DUE, REVIEW_SOON):
            # Review and renew while a device it governs has a decidable need (a renewal governs the same devices; never one a more
            # specific live decision governs: skeptics T0); End only when none needs one as recorded; neither while one has no need
            # only because what it, or a reason the decision names, turns on is not recorded (need_unknown with its reasons: its page
            # says what to record first; skeptics S3, T2). decisions.refusal_for's kinds, in the same order.
            ruled = [a for a in held if governing.get(a.pk) is not None and governing[a.pk].pk == d.pk]
            renewable = any(needs[a.pk] is not None and needs[a.pk].decidable for a in ruled)
            unknown = not renewable and any(need_unknown(standings[a.pk], d.reasons) for a in ruled)
            rows[state].append(_decision_row(d, review_words(d, today), held=len(held), may_decide=may_decide, renew=renewable,
                                             end=not renewable and not unknown))
        dds = [_decision_device(a, standings[a.pk], today, governing, d, responses) for a in held]
        for check in _checks(d, dds, profiles.get(_model_of(d)), today):
            if not check.info and check.key != CHECK_NO_DEVICE:
                rows["checks"].append(AttentionRow(f"{d.number} · {check.words}", link=Link(URLS["decision"], (d.number,)), decision=d,
                                                   tags=check.tags))
    headings = tuple(Heading(key, words, sum(r.count for r in rows[key]), tuple(rows[key]), info=key in ATTENTION_INFO)
                     for key, words in ATTENTION_HEADINGS.items() if rows[key])
    return Attention(headings=headings)


# --- the nav badge ---------------------------------------------------------------------------------------------------------------------

def holds_device_q() -> Q:
    """A condition on DeviceSecurityDecision rows: its scope holds a device on site (device scope: its device not retired; release scope:
    a device of ours on site whose recorded release is it; model scope: a device of ours on site of the model), an EXISTS per scope
    kind, never reading standings. Needs attention's review_due and review_soon count only such decisions (the others are under
    no_device), and so does the badge: a list that links from those counts narrows by it too (review 39 F20)."""
    from django.db.models import Exists

    on = Asset.objects.filter(status__in=ON_SITE_STATUSES)
    device = Q(asset__isnull=False) & Exists(on.filter(pk=OuterRef("asset_id")))
    release = Q(release__isnull=False) & Exists(DeviceSecurity.objects.filter(
        release_id=OuterRef("release_id"), asset__ownership=Ownership.OWNED, asset__status__in=ON_SITE_STATUSES,
        asset__device_model_id=OuterRef("device_model_id")))
    model = Q(asset__isnull=True, release__isnull=True) & Exists(on.filter(ownership=Ownership.OWNED,
                                                                           device_model_id=OuterRef("device_model_id")))
    return device | release | model


def badge(today: date) -> dict:
    """{"count": n, "hot": n > 0}: live support decisions past their review (review_by before today) whose scope still holds a device on
    site (holds_device_q), as Needs attention's review_due counts them. One aggregate query; never reads standings. The caller gates it
    (permissions.can_view_devices, never a scoped user)."""
    count = _live().filter(review_by__lt=today).filter(holds_device_q()).count()
    return {"count": count, "hot": count > 0}


# --- slice 40: security advisories (build40 sections 1 and 4) --------------------------------------------------------------------------
#
# An advisory names models of this facility's catalog (AdvisoryModel rows) and, for each, the releases it names (listed, every release,
# or releases not stated) and what it offers (a release from the manufacturer, its validated patches, mitigations, no fix). Which devices
# it concerns is derived here from each device's recorded release only (slice 38: what a device ran on a past day is never asked), never
# stored. CVE IDs and other JSON lists are matched in Python, never with a JSON lookup (SQLite has none). Positive conditions only.

HICP_ADVISORIES = ("HHS 405(d) HICP 2023 recommends taking in security advisories, finding the devices they concern, and choosing a "
                   "response with the manufacturer. Cadence keeps no advisory text: record the source, its reference, and the models and "
                   "releases it names. A response is an update open to the fix or to a release it does not name, the manufacturer's "
                   "patches recorded, or a decision.")
HICP_ADVISORY_DECISIONS = ("HHS 405(d) HICP 2023 recommends identifying, alongside the manufacturer, a response to each vulnerability an "
                           "advisory discloses, with a remediation plan where a patch cannot fix it. IT's exception stays in IT's system; "
                           "record its number here.")
ADVISORIES_EMPTY = ("No advisories recorded here yet. Cadence does not receive advisories by itself: record each one your team takes "
                    "in.")
DEVICE_ADVISORIES_EMPTY = "No advisory recorded here names this model."
NO_CVES_WORDS = "No CVE IDs recorded"
NO_MODEL_NAMED = "no model of this facility's catalog"
NOT_IN_USE_HELP = ("The component or feature the advisory names is not deployed or used on these devices here. If you turned a feature "
                   "off as a control, choose Keep in use with controls (a connectivity feature turned off).")
ADVISORY_DECISION_HINT = ("For the devices of this scope that {number} names, on a release it names or with no release recorded. "
                          "Cadence keeps no advisory text.")

# A device's line under a model row (advisory_lines), decided in this order: the fix before a release it names (critique39_scope B3)
ON_FIX, NAMED, LINE_NOT_RECORDED, OTHER_RELEASE = "on_fix", "named", "not_recorded", "other"
LINE_KINDS = {NAMED: "On a release it names", LINE_NOT_RECORDED: "Release not recorded", ON_FIX: "On the fix it names",
              OTHER_RELEASE: "On a release it does not name"}
NEEDS_RESPONSE = (NAMED, LINE_NOT_RECORDED)

# What answers a line that needs a response (advisory_answers): a device's kind, the first that holds in ANSWER_ORDER
PATCHES_RECORDED, PATCHES_OTHER_LIST, ANSWER_DECIDED, ANSWER_REVIEW_DUE, NO_RESPONSE = (
    "patches_recorded", "patches_other_list", "decided", "review_due", "none")
UPDATE_ELSEWHERE = "update_elsewhere"  # an open update that does not answer the line: shown with its words, never a response
ANSWER_KINDS = {UPDATE_OPEN: "Update open", UPDATE_NOT_CONFIRMED: "Version not confirmed", PATCHES_RECORDED: "Patches recorded",
                PATCHES_OTHER_LIST: "Patches, another list", ANSWER_DECIDED: "Decided", ANSWER_REVIEW_DUE: "Decision past its review date",
                NO_RESPONSE: "No response recorded"}
ANSWER_ORDER = (PATCHES_RECORDED, ANSWER_DECIDED, UPDATE_NOT_CONFIRMED, UPDATE_OPEN, PATCHES_OTHER_LIST, ANSWER_REVIEW_DUE, NO_RESPONSE)
ANSWERED = (PATCHES_RECORDED, ANSWER_DECIDED)  # what Close counts as answered, besides the lines that need no response
NO_RESPONSE_WORDS = "No response recorded"


def advisory_name(advisory, rows=None, sources=None) -> str:
    """ "AD-26-0003 · DEMO-ADV-0001 · Alaris 8015 PCU": the number, the first live source's reference (with none, its label and published
    day, "Manufacturer bulletin, Jul 13, 2026", or its label), then the models it names (two, then "and 2 more"), or "no model of this
    facility's catalog". No title is kept. `rows` (its AdvisoryModel rows with device_model) and `sources` (its sources, oldest first)
    when already read; else read here (two queries)."""
    from .models import AdvisorySource

    if sources is None:
        sources = list(AdvisorySource.objects.filter(advisory_id=advisory.pk).order_by("created_at", "pk"))
    if rows is None:
        rows = list(advisory.model_rows.select_related("device_model").order_by("created_at", "pk"))
    parts = [advisory.number]
    live = [s for s in sources if s.removed_on is None]
    if live:
        first = live[0]
        if first.reference:
            parts.append(first.reference)
        elif first.published_on:
            parts.append(f"{first.get_source_display()}, {day_words(first.published_on)}")
        else:
            parts.append(first.get_source_display())
    names = [r.device_model.model for r in rows]
    if not names:
        parts.append(NO_MODEL_NAMED)
    elif len(names) <= 2:
        parts.append(" and ".join(names) if len(names) == 2 else names[0])
    else:
        parts.append(f"{names[0]}, {names[1]}, and {len(names) - 2} more")
    return " · ".join(parts)


def rows_of(advisories) -> list:
    """The AdvisoryModel rows of `advisories` (advisories, their ids, or a queryset of them), each with its advisory, model, and fix
    release read, and `row.listed` (its listed releases, in version_key order) and `row.listed_ids` attached. Two queries."""
    from .models import AdvisoryModel, AdvisoryRelease

    if isinstance(advisories, QuerySet):
        which = Q(advisory_id__in=advisories.values("pk"))
    else:
        which = Q(advisory_id__in=[getattr(a, "pk", a) for a in advisories])
    rows = list(AdvisoryModel.objects.filter(which).select_related("advisory", "device_model", "fix_release")
                .order_by("advisory__received_on", "advisory_id", "created_at", "pk"))
    listed: dict = {}
    for ar in (AdvisoryRelease.objects.filter(advisory_model_id__in=[r.pk for r in rows]).select_related("release")
               .order_by("release__version_key", "pk")):
        listed.setdefault(ar.advisory_model_id, []).append(ar.release)
    for r in rows:
        r.listed = listed.get(r.pk, [])
        r.listed_ids = frozenset(x.pk for x in r.listed)
    return rows


@dataclass(frozen=True)
class AdvisoryLine:
    """One device under one model row: its `kind` (LINE_KINDS), its recorded release (None: none recorded), its standing (the connection
    words), and `words`: "On 5.2 (demo), a release the advisory names", "The advisory names every release of the Cios Spin", ..."""

    asset: object
    row: object
    kind: str
    release: object | None
    standing: object
    words: str

    @property
    def needs_response(self) -> bool:
        return self.kind in NEEDS_RESPONSE

    @property
    def place(self) -> str:
        """ "ICU · Connected (wireless)": the device's department and connection."""
        department = getattr(self.asset.department, "name", "") if self.asset.department_id else ""
        return " · ".join(p for p in (department, self.standing.connection.words) if p)


def _line(asset, row, standing) -> AdvisoryLine:
    from .models import AdvisoryVersions

    release = standing.release
    model = row.device_model.model
    if row.versions == AdvisoryVersions.ALL:
        return AdvisoryLine(asset, row, NAMED, release, standing, f"The advisory names every release of the {model}")
    if row.versions == AdvisoryVersions.NOT_STATED:
        return AdvisoryLine(asset, row, NAMED, release, standing, f"The advisory names the {model} without stating releases")
    if release is None:
        return AdvisoryLine(asset, row, LINE_NOT_RECORDED, None, standing,
                            f"{RELEASE_NOT_RECORDED}: it may run a release the advisory names")
    if row.fix_release_id is not None and release.pk == row.fix_release_id:
        return AdvisoryLine(asset, row, ON_FIX, release, standing, f"On {release.version}, the release the advisory names as the fix")
    if release.pk in row.listed_ids:
        return AdvisoryLine(asset, row, NAMED, release, standing, f"On {release.version}, a release the advisory names")
    return AdvisoryLine(asset, row, OTHER_RELEASE, release, standing, f"On {release.version}, a release the advisory does not name")


def advisory_lines(rows, *, today: date, assets=None) -> list:
    """[AdvisoryLine] for `rows` (from rows_of): every device on site (ours and temporary, on_site) of each row's model, or only those of
    `assets` (a list of devices or ids: the Security tab's one device), in the rows' order, then by tag. From each device's recorded
    release only (release_of's rule: versioned, not entered in error, of its model). Fixed queries: the devices with their records (one),
    their models' profiles (one, for the connection words)."""
    model_ids = {r.device_model_id for r in rows}
    if not model_ids:
        return []
    devices = on_site().filter(device_model_id__in=model_ids)
    if assets is not None:
        devices = devices.filter(pk__in=[getattr(a, "pk", a) for a in assets])
    devices = list(devices.select_related(*_WITH_RECORD).order_by("tag"))
    if not devices:
        return []
    held = standings(devices, today)
    by_model: dict = {}
    for a in devices:
        by_model.setdefault(a.device_model_id, []).append(a)
    return [_line(a, row, held[a.pk]) for row in rows for a in by_model.get(row.device_model_id, [])]


def answers_row(update, row) -> bool:
    """Whether an update answers a model row (build40 section 4.2), judged by where it leaves the device, never by a link: a release
    target answers when the row lists releases and the target is not one it lists (the fix included: it is never listed); a patches
    target answers when the row's remedy is the manufacturer's patches and the row names no list or the target names that list."""
    from .models import AdvisoryRemedy, AdvisoryVersions, UpdateTarget

    if update.target == UpdateTarget.RELEASE:
        return row.versions == AdvisoryVersions.LISTED and update.target_release_id not in row.listed_ids
    if update.target == UpdateTarget.PATCHES and row.remedy == AdvisoryRemedy.PATCHES:
        return not row.patches_reference or (update.target_reference or "").casefold() == row.patches_reference.casefold()
    return False


@dataclass(frozen=True)
class LineAnswer:
    """What answers one line (advisory_answers): `kind` "" when the line needs no response (on the fix, or a release it does not name),
    else the first of ANSWER_ORDER that holds; `responses` every Response shown (updates, the patches recorded, the decision), in that
    order; `decision` the governing advisory decision (or None) and whether it `covers` the line now."""

    line: AdvisoryLine
    kind: str
    responses: tuple = ()
    decision: object | None = None
    covers: bool = False


def _patches_reference(update) -> str:
    return update.reference or update.target_reference or ""


def _update_rows(lines):
    """The SoftwareUpdate rows that may answer `lines` (one query): open update work orders of any target, those completed or closed
    with nothing recorded, and the live records of the manufacturer's patches (a hand row, a work order's patches, a work order's
    "already" on a patches target)."""
    from apps.workorders.models import OPEN_STATUSES, WoStatus

    from .models import SoftwareUpdate, UpdateOutcome, UpdateTarget

    ids = {ln.asset.pk for ln in lines}
    finished = (WoStatus.COMPLETED, WoStatus.CLOSED)
    patches = (Q(work_order__isnull=True, removed_on__isnull=True, outcome=UpdateOutcome.PATCHES)
               | Q(work_order__status__in=finished, outcome=UpdateOutcome.PATCHES)
               | Q(work_order__status__in=finished, outcome=UpdateOutcome.ALREADY, target=UpdateTarget.PATCHES))
    return list(SoftwareUpdate.objects.filter(asset_id__in=ids)
                .filter(Q(work_order__status__in=OPEN_STATUSES) | Q(work_order__status__in=finished, outcome="") | patches)
                .select_related("work_order", "target_release").order_by("work_order__number", "applied_on", "pk"))


def _open_update_words(update, row) -> str:
    number = update.work_order.number
    if update.target_release_id:
        version = release_name(update.target_release)
        if update.target_release_id == row.fix_release_id:
            return f"Update open to {version}, the fix: {number}"
        if update.target_release_id in row.listed_ids:
            return f"Update open to {version}, a release the advisory names: {number}"
        return f"Update open to {version}, a release the advisory does not name: {number}"
    ref = f" ({update.target_reference})" if update.target_reference else ""
    if answers_row(update, row):
        return f"Update open: the manufacturer's patches{ref}: {number}"
    if row.patches_reference:
        return f"Update open: the manufacturer's patches{ref}: {number}; the advisory names {row.patches_reference}"
    return f"Update open: the manufacturer's patches{ref}: {number}, not what the advisory offers"


def _patches_words(update, row) -> str:
    from .models import UpdateOutcome

    day = day_words(update.applied_on) if update.applied_on else "on a day not recorded"
    ref = _patches_reference(update)
    shown = f" ({ref})" if ref else ""
    if update.outcome == UpdateOutcome.ALREADY:
        return f"Checked {day}: already had the manufacturer's patches{shown}"
    if row.patches_reference and ref.casefold() != row.patches_reference.casefold():
        return f"The manufacturer's patches recorded {day}{shown}; the advisory names {row.patches_reference}"
    if row.patches_reference:
        return f"The manufacturer's patches recorded {day}{shown}, the list the advisory names"
    return f"The manufacturer's patches recorded {day}{shown}, since this advisory was received"


def _patches_kind(update, row) -> str:
    """PATCHES_RECORDED when a live patches record answers the row (build40 section 4.2: the list it names, on any day; with no list
    named, applied on or after the day received), PATCHES_OTHER_LIST when the row names a list and a record since received names another
    or none, else ""."""
    from .models import AdvisoryRemedy

    if row.remedy != AdvisoryRemedy.PATCHES:
        return ""
    ref = _patches_reference(update)
    received = row.advisory.received_on
    if row.patches_reference:
        if ref.casefold() == row.patches_reference.casefold():
            return PATCHES_RECORDED
        return PATCHES_OTHER_LIST if update.applied_on is not None and update.applied_on >= received else ""
    return PATCHES_RECORDED if update.applied_on is not None and update.applied_on >= received else ""


def advisory_decisions(lines) -> dict:
    """{(asset id, advisory id): the governing live advisory decision, or None} for the lines that need a response: per advisory, the
    most specific live decision of its key whose scope holds the device (its own; for a device of ours, its recorded release's, else its
    model's; a temporary device only its own), whether or not past review (covers says whether it covers now). One query."""
    needing = [ln for ln in lines if ln.needs_response]
    if not needing:
        return {}
    advisory_ids = {ln.row.advisory_id for ln in needing}
    rows = list(DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.ADVISORY, ended_on__isnull=True, advisory_id__in=advisory_ids)
                .filter(Q(asset_id__in={ln.asset.pk for ln in needing})
                        | Q(asset__isnull=True, device_model_id__in={ln.asset.device_model_id for ln in needing}))
                .select_related(*_DECISION_RELATED).order_by("number"))
    grouped = _by_key(rows)
    out = {}
    for advisory_id in advisory_ids:
        devices = list({ln.asset.pk: ln.asset for ln in needing if ln.row.advisory_id == advisory_id}.values())
        for pk, found in _governing(devices, grouped.get(advisory_key(advisory_id), [])).items():
            out[(pk, advisory_id)] = found
    return out


def advisory_answers(lines, *, today: date, fy_start: int | None = None) -> dict:
    """{(asset id, row id): LineAnswer} for `lines` (build40 section 4.2): for a line that needs a response, its open updates (answering
    or not: answers_row), its updates done with the version not confirmed (answering only), the manufacturer's patches recorded (the
    list the advisory names on any day; with no list named, since the day received) or another list since received, and the governing
    advisory decision; its kind the first of ANSWER_ORDER that holds. A line that needs none has kind "". Fixed queries: the update rows
    (one), the advisory decisions (one); `fy_start` the facility's fiscal year start month, read once by the caller (decision words)."""
    from apps.workorders.models import OPEN_STATUSES

    needing = [ln for ln in lines if ln.needs_response]
    by_device: dict = {}
    for u in _update_rows(needing) if needing else ():
        by_device.setdefault(u.asset_id, []).append(u)
    governing = advisory_decisions(needing)
    out = {}
    for ln in lines:
        if not ln.needs_response:
            out[(ln.asset.pk, ln.row.pk)] = LineAnswer(ln, "")
            continue
        kinds, shown = set(), []
        for u in by_device.get(ln.asset.pk, ()):
            wo = u.work_order
            if wo is not None and wo.status in OPEN_STATUSES:
                answering = answers_row(u, ln.row)
                if answering:
                    kinds.add(UPDATE_OPEN)
                shown.append(Response(UPDATE_OPEN if answering else UPDATE_ELSEWHERE, _open_update_words(u, ln.row), wo.number, update=u))
            elif wo is not None and not u.outcome:
                if answers_row(u, ln.row):
                    kinds.add(UPDATE_NOT_CONFIRMED)
                    day = f" {day_words(wo.completed_on)}" if wo.completed_on else ""
                    shown.append(Response(UPDATE_NOT_CONFIRMED, f"Update done{day}, version not confirmed: Confirm version ({wo.number})",
                                          wo.number, update=u))
            else:
                kind = _patches_kind(u, ln.row)
                if kind:
                    kinds.add(kind)
                    shown.append(Response(kind, _patches_words(u, ln.row), wo.number if wo is not None else "", update=u))
        decision = governing.get((ln.asset.pk, ln.row.advisory_id))
        covering = covers(decision, today)
        if decision is not None:
            kinds.add(ANSWER_DECIDED if covering else ANSWER_REVIEW_DUE)
            shown.append(Response(ANSWER_DECIDED if covering else ANSWER_REVIEW_DUE, decision_words(decision, today, fy_start=fy_start),
                                  decision.number, decision=decision, covers=covering))
        kind = next((k for k in ANSWER_ORDER if k in kinds), NO_RESPONSE)
        if not shown:
            shown.append(Response(NO_RESPONSE, NO_RESPONSE_WORDS))
        out[(ln.asset.pk, ln.row.pk)] = LineAnswer(ln, kind, tuple(shown), decision, covering)
    return out


# --- slice 40, wave 1A: counts, status words, Checks, Needs attention, the badge's advisory half (build40 sections 1, 4.3-4.5, 7.2, 7.9)
#
# One reading for every reader (advisory_reading: the model rows, the lines, and what answers the lines of advisories with needs), and
# one predicate each for what blocks Close (close_blockers: also a closed advisory's "devices named since") and for whether a reason not
# to apply still holds (not_applicable_blocks: also a not-applicable advisory's "devices named since"). The refusals' words are
# apps.cybersecurity.advisories'; this section words the statuses, the Checks, and Needs attention. A Device security View reader reads
# nothing derived from a decision (the line kinds, the update counts, the stored statuses only: critique40_rules S2).

ADVISORY_BADGE_TITLE = "Advisories with models to confirm"
BOTH_BADGE_TITLE = "Advisories with models to confirm, and decisions past their review date"
ADVISORY_ATTENTION_HEADINGS = {
    "to_confirm": "Advisories with models to confirm",
    "no_response": "On a release an advisory names, or no release recorded: no response recorded",
    "temporary": "Temporary equipment an advisory names: its owner installs updates",
    "not_confirmed": "Updates done, version not confirmed",
    "other_list": "Patches naming another list than the advisory names",
    "named_since": "Closed or not applicable, devices named since",
    "changed": "Closed, models to confirm since a revision",
    "checks": "Checks",
    "decisions_due": "Advisory decisions past their review date",
}
# Who reads each heading: Device security View (stored rows and the model rows' releases) or also Equipment View (device lines,
# responses, and decisions: permissions.can_view_devices)
ATTENTION_VIEW = ("to_confirm", "changed", "checks")

# An advisory with needs (build40 section 4.3): open or closed, not entered in error. A not-applicable advisory has none: a device named
# since is read through not_applicable_blocks.
HAS_NEEDS = ("open", "closed")
# What blocks Close (close_blockers), in the order its refusal and the status words name them
UNANSWERED = (NO_RESPONSE, ANSWER_REVIEW_DUE, PATCHES_OTHER_LIST, UPDATE_NOT_CONFIRMED, UPDATE_OPEN)
NO_FIX_REMEDIES = ("mitigations", "no_fix_yet", "no_fix_planned")  # models.AdvisoryRemedy: the advisory names no fix to install

# Status words (build40 section 1; never "done", "resolved", "clear")
MODELS_TO_CONFIRM_WORDS = "Models to confirm"
READY_TO_CLOSE_WORDS = "Open: ready to close"
OPEN_WORDS = "Open"
ENTERED_IN_ERROR_WORDS = "Entered in error"
TO_CONFIRM_SINCE_WORDS = "models to confirm since a revision"

# Check lines on an advisory (advisory_checks; information, never refusals)
CHECK_RELEASE_ADDED, CHECK_OTHER_LIST, CHECK_TO_CONFIRM, CHECK_NAMED_SINCE = "release_added", "other_list", "to_confirm", "named_since"
TO_CONFIRM_CHECK_WORDS = "Closed, models to confirm since a revision: reopen it to confirm them"


@dataclass(frozen=True)
class AdvisoryReading:
    """What every advisory reader reads (advisory_reading): the model rows (rows_of), every device's line under them, and what answers
    the lines of advisories with needs ({(asset id, row id): LineAnswer}; a not-applicable advisory's lines have none)."""

    rows: list
    lines: list
    answers: dict


def advisory_reading(advisories, today: date, *, assets=None) -> AdvisoryReading:
    """The rows, lines, and answers of `advisories` (instances, ids, or a queryset; entered in error ones are read too, and callers leave
    them out). `assets`: only these devices' lines (the Security tab's one device). Fixed queries for any number of advisories, rows,
    and devices: the rows (two), the devices and their models' profiles (two, with a row), the update rows and the advisory decisions
    (two, with a line that needs a response), the facility's fiscal year start (one, then: decision words)."""
    rows = rows_of(advisories)
    lines = advisory_lines(rows, today=today, assets=assets) if rows else []
    with_needs = [ln for ln in lines if ln.row.advisory.status in HAS_NEEDS and ln.row.advisory.removed_on is None]
    answers = {}
    if any(ln.needs_response for ln in with_needs):
        answers = advisory_answers(with_needs, today=today, fy_start=fiscal.start_month())
    elif with_needs:
        answers = advisory_answers(with_needs, today=today)  # no line needs a response: nothing read
    return AdvisoryReading(rows, lines, answers)


def close_blockers(answers) -> dict:
    """{answer kind: [LineAnswer]} for the lines that need a response and are not answered (ANSWERED: the manufacturer's patches recorded
    on the list it names, or a covering decision), in UNANSWERED's order: the one predicate Close refuses by and a closed advisory's
    "devices named since" is read by (locks S7). `answers` one advisory's (advisory_reading's, or a slice of it). No query."""
    found: dict = {}
    for a in answers.values():
        if a.kind and a.kind not in ANSWERED:
            found.setdefault(a.kind, []).append(a)
    return {kind: sorted(found[kind], key=lambda a: a.line.asset.tag) for kind in UNANSWERED if kind in found}


def not_applicable_blocks(reason: str, rows, lines) -> bool:
    """Whether `reason` (models.NotApplicableReason) is contradicted by the records now (critique40_rules B2): it names no model here,
    while a model row exists; no device of the models it names on site, while one is (or no model is named); no device on a release it
    names, while a line needs a response (or no model is named). The one predicate marking refuses by and a not-applicable advisory's
    "devices named since" is read by (locks S7). `rows` and `lines` one advisory's. No query."""
    if reason == "no_model_here":
        return bool(rows)
    if not rows:
        return True
    if reason == "none_here":
        return bool(lines)
    return any(ln.needs_response for ln in lines)


@dataclass(frozen=True)
class RowCounts:
    """One model row's counts (advisory_counts): `lines` devices per line kind (every LINE_KINDS key) and `updates` devices with an update
    open or done with the version not confirmed that answers the line (UPDATE_OPEN, UPDATE_NOT_CONFIRMED), for Device security View;
    `answers` devices per answer kind (the first that holds: every ANSWER_KINDS key) of the lines that need a response, for
    can_view_devices only (else None)."""

    row: object
    lines: dict
    updates: dict
    answers: dict | None = None


@dataclass(frozen=True)
class AdvisoryCounts:
    """An advisory's counts (advisory_counts): its model rows' (RowCounts), their totals, and for can_view_devices the decisions past
    their review date governing a line that needs a response (`reviews_due`) and the devices named since (a closed advisory's lines that
    block Close but those under a decision past review; a not-applicable advisory's devices its reason no longer holds for); `words`
    its status words for the reader (status_words)."""

    advisory: object
    rows: tuple
    lines: dict
    updates: dict
    answers: dict | None
    reviews_due: int
    named_since: int
    words: str = ""


def _line_counts(lines) -> dict:
    out = dict.fromkeys(LINE_KINDS, 0)
    for ln in lines:
        out[ln.kind] += 1
    return out


def _update_counts(answers) -> dict:
    """Devices with an answering update open, and with one done with the version not confirmed: from the responses shown, never the
    answer's kind (a decision before them would otherwise show through to a View reader)."""
    out = {UPDATE_OPEN: 0, UPDATE_NOT_CONFIRMED: 0}
    for a in answers:
        kinds = {r.kind for r in a.responses}
        for kind in out:
            out[kind] += kind in kinds
    return out


def _answer_counts(answers) -> dict:
    out = dict.fromkeys(ANSWER_KINDS, 0)
    for a in answers:
        if a.kind:
            out[a.kind] += 1
    return out


def _summed(dicts) -> dict:
    out: dict = {}
    for d in dicts:
        for key, n in d.items():
            out[key] = out.get(key, 0) + n
    return out


def _named_since(advisory, rows, lines, answers) -> int:
    """The devices named since a closed or not-applicable advisory left open: a closed one's lines that block Close (close_blockers) but
    those under a decision past its review date (counted apart); a not-applicable one's devices its reason no longer holds for. 0 for
    an open one, or while its predicate does not hold."""
    if advisory.status == "closed":
        return sum(len(found) for kind, found in close_blockers(answers).items() if kind != ANSWER_REVIEW_DUE)
    if advisory.status == "not_applicable" and not_applicable_blocks(advisory.not_applicable_reason, rows, lines):
        if advisory.not_applicable_reason == "none_here":
            return len({ln.asset.pk for ln in lines})
        return len({ln.asset.pk for ln in lines if ln.needs_response})
    return 0


def _advisories(advisories) -> list:
    """The advisories again, with who last moved them out of open (their status words), in one query."""
    from .models import SecurityAdvisory

    if isinstance(advisories, QuerySet):
        return list(advisories.select_related("status_by"))
    ids = [getattr(a, "pk", a) for a in advisories]
    found = SecurityAdvisory.objects.filter(pk__in=ids).select_related("status_by").in_bulk() if ids else {}
    return [found[pk] for pk in ids if pk in found]


def advisory_counts(advisories, *, today: date, devices_visible: bool) -> dict:
    """{advisory id: AdvisoryCounts} for `advisories` (instances, ids, or a queryset): per advisory and per model row, the devices per
    line kind and the update counts (Device security View), and for `devices_visible` (permissions.can_view_devices) the devices per
    answer kind, the decisions past review, the devices named since, and the decision-derived status words (build40 sections 1, 4.4).
    An advisory entered in error reads no row. The Advisories tab, the advisory page, the API, and Needs attention read it. Fixed
    queries for any number of advisories, rows, and devices: the advisories (one), advisory_reading's."""
    from dataclasses import replace

    found = _advisories(advisories)
    reading = advisory_reading([a for a in found if a.removed_on is None], today)
    rows_by: dict = {}
    for r in reading.rows:
        rows_by.setdefault(r.advisory_id, []).append(r)
    lines_by: dict = {}
    for ln in reading.lines:
        lines_by.setdefault(ln.row.pk, []).append(ln)
    answers_by: dict = {}
    for (_pk, row_pk), a in reading.answers.items():
        answers_by.setdefault(row_pk, []).append(a)
    out = {}
    for adv in found:
        rows = rows_by.get(adv.pk, []) if adv.removed_on is None else []
        per_row = []
        for r in rows:
            needing = [a for a in answers_by.get(r.pk, ()) if a.kind]
            per_row.append(RowCounts(r, _line_counts(lines_by.get(r.pk, ())), _update_counts(needing),
                                     _answer_counts(needing) if devices_visible else None))
        lines = [ln for r in rows for ln in lines_by.get(r.pk, ())]
        answers = {(a.line.asset.pk, r.pk): a for r in rows for a in answers_by.get(r.pk, ())}
        reviews, named = 0, 0
        if devices_visible:
            reviews = len({a.decision.pk for a in answers.values() if a.kind == ANSWER_REVIEW_DUE})
            named = _named_since(adv, rows, lines, answers)
        counts = AdvisoryCounts(adv, tuple(per_row), _summed(c.lines for c in per_row) or dict.fromkeys(LINE_KINDS, 0),
                                _summed(c.updates for c in per_row) or {UPDATE_OPEN: 0, UPDATE_NOT_CONFIRMED: 0},
                                (_summed(c.answers for c in per_row) or dict.fromkeys(ANSWER_KINDS, 0)) if devices_visible else None,
                                reviews, named)
        out[adv.pk] = replace(counts, words=status_words(adv, counts, devices_visible=devices_visible))
    return out


def _who_day(user, day) -> str:
    """ "Kim Lee, Oct 21, 2026" (the day alone when nobody is recorded: the system)."""
    return f"{user}, {day_words(day)}" if user is not None else day_words(day)


def status_words(advisory, counts=None, *, devices_visible: bool) -> str:
    """The advisory's status words (build40 section 1), the first that holds:
    - entered in error: "Entered in error";
    - not applicable: "Not applicable here (Kim Lee, Oct 21, 2026): It names no model in this facility's catalog";
    - closed: "Closed (Kim Lee, Oct 21, 2026)", then what holds now: " · 2 devices named since" and " · 1 decision past its review date"
      (can_view_devices), " · models to confirm since a revision" (every reader: stored state);
    - open: "Models to confirm"; for `devices_visible` (can_view_devices) "Open: 4 devices with no response recorded", "Open: 1 device
      under a decision past its review date", "Open: 1 device with patches naming another list", "Open: 1 update done, version not
      confirmed", "Open: 12 updates open", "Open: ready to close" (only with a model row: none names nothing to close); else "Open"
      (never a word derived from a decision: critique40_rules S2).
    `counts` advisory_counts' for it (read here when not given: the facility's today)."""
    from .models import AdvisoryStatus, NotApplicableReason

    if advisory.removed_on is not None:
        return ENTERED_IN_ERROR_WORDS
    if advisory.status == AdvisoryStatus.NOT_APPLICABLE:
        reason = NotApplicableReason(advisory.not_applicable_reason).label
        return f"Not applicable here ({_who_day(advisory.status_by, advisory.status_on)}): {reason}"
    if counts is None and devices_visible:
        counts = advisory_counts([advisory], today=timezone.localdate(), devices_visible=devices_visible)[advisory.pk]
    if advisory.status == AdvisoryStatus.CLOSED:
        parts = [f"Closed ({_who_day(advisory.status_by, advisory.status_on)})"]
        if devices_visible and counts.named_since:
            parts.append(f"{_plural(counts.named_since, 'device', 'devices')} named since")
        if advisory.models_confirmed_on is None:
            parts.append(TO_CONFIRM_SINCE_WORDS)
        if devices_visible and counts.reviews_due:
            n = counts.reviews_due
            parts.append(f"{_plural(n, 'decision', 'decisions')} past {'its' if n == 1 else 'their'} review date")
        return " · ".join(parts)
    if advisory.models_confirmed_on is None:
        return MODELS_TO_CONFIRM_WORDS
    if not devices_visible or not counts.rows:
        return OPEN_WORDS
    a = counts.answers
    if a[NO_RESPONSE]:
        return f"Open: {_plural(a[NO_RESPONSE], 'device', 'devices')} with no response recorded"
    if a[ANSWER_REVIEW_DUE]:
        n = a[ANSWER_REVIEW_DUE]
        return ("Open: 1 device under a decision past its review date" if n == 1
                else f"Open: {n} devices under decisions past their review date")
    if a[PATCHES_OTHER_LIST]:
        return f"Open: {_plural(a[PATCHES_OTHER_LIST], 'device', 'devices')} with patches naming another list"
    if a[UPDATE_NOT_CONFIRMED]:
        return f"Open: {_plural(a[UPDATE_NOT_CONFIRMED], 'update', 'updates')} done, version not confirmed"
    if a[UPDATE_OPEN]:
        return f"Open: {_plural(a[UPDATE_OPEN], 'update', 'updates')} open"
    return READY_TO_CLOSE_WORDS


# --- Checks -----------------------------------------------------------------------------------------------------------------------------

def _current_releases(rows) -> dict:
    """{model id: [the model's versioned releases not entered in error, in version_key order]} for the rows that list releases (one
    query, none without one)."""
    from .models import AdvisoryVersions

    model_ids = {r.device_model_id for r in rows if r.versions == AdvisoryVersions.LISTED}
    out: dict = {}
    if not model_ids:
        return out
    for rel in (SoftwareRelease.objects.filter(device_model_id__in=model_ids, removed_on__isnull=True).exclude(version_key="")
                .order_by("version_key", "pk")):
        out.setdefault(rel.device_model_id, []).append(rel)
    return out


def _release_added(rows, current: dict) -> list:
    """ "Release added since: 5.8 (demo), not listed or named as the fix" for each row that lists releases: the model's releases now,
    neither seen when its models were last set (seen_release_ids, by identity), listed, nor the fix (scope N1). The model is named when
    the advisory names more than one."""
    from .models import AdvisoryVersions

    out = []
    for r in rows:
        if r.versions != AdvisoryVersions.LISTED:
            continue
        seen = {str(pk) for pk in (r.seen_release_ids or ())}
        added = [rel for rel in current.get(r.device_model_id, ())
                 if str(rel.pk) not in seen and rel.pk not in r.listed_ids and rel.pk != r.fix_release_id]
        if added:
            head = "Releases added since" if len(added) > 1 else "Release added since"
            model = f" ({r.device_model.model})" if len(rows) > 1 else ""
            out.append(Check(CHECK_RELEASE_ADDED, f"{head}: {', '.join(rel.version for rel in added)}, not listed or named as the fix"
                                                  f"{model}"))
    return out


def _since_words(advisory) -> str:
    if advisory.status == "closed":
        return f"it was closed on {day_words(advisory.status_on)}"
    return f"it was marked not applicable on {day_words(advisory.status_on)}"


def _checks_of(advisory, rows, lines, answers, current: dict, *, devices_visible: bool) -> list:
    """The Check lines of one advisory from its reading (advisory_checks). No query."""
    if advisory.removed_on is not None:
        return []
    out = _release_added(rows, current) if advisory.status in HAS_NEEDS else []
    if devices_visible:
        for a in sorted(answers.values(), key=lambda a: a.line.asset.tag):
            if a.kind == PATCHES_OTHER_LIST:
                words = next(r.words for r in a.responses if r.kind == PATCHES_OTHER_LIST)
                out.append(Check(CHECK_OTHER_LIST, f"{a.line.asset.tag}: {words}", tags=(a.line.asset.tag,)))
    if advisory.status == "closed" and advisory.models_confirmed_on is None:
        out.append(Check(CHECK_TO_CONFIRM, TO_CONFIRM_CHECK_WORDS))
    if devices_visible and advisory.status in ("closed", "not_applicable"):
        if advisory.status == "closed":
            tags = sorted({a.line.asset.tag for found in close_blockers(answers).values() for a in found})
        elif not_applicable_blocks(advisory.not_applicable_reason, rows, lines):
            wanted = [ln for ln in lines if advisory.not_applicable_reason == "none_here" or ln.needs_response]
            tags = sorted({ln.asset.tag for ln in wanted})
        else:
            tags = []
        if tags:
            out.append(Check(CHECK_NAMED_SINCE, f"{_plural(len(tags), 'device', 'devices')} named since {_since_words(advisory)}: "
                                                f"{_tags(tags, shown=10)}", tags=tuple(tags)))
    return out


def advisory_checks(advisory, *, today: date, devices_visible: bool) -> list:
    """The advisory's Check lines (build40 section 4.5; information, never refusals), each a Check: a release of a model it lists releases
    for, added since its models were last set and neither listed nor the fix ("Release added since: 5.8 (demo), not listed or named as
    the fix": Device security View); patches naming another list than the advisory names, per device (can_view_devices); closed with
    its models to confirm since a revision (stored state); closed or not applicable with devices named since (the predicates Close and
    Not applicable refuse by: close_blockers, not_applicable_blocks; can_view_devices). None for an advisory entered in error. Fixed
    queries: advisory_reading's, the models' releases (one)."""
    if advisory.removed_on is not None:
        return []
    reading = advisory_reading([advisory], today)
    return _checks_of(advisory, reading.rows, reading.lines, reading.answers, _current_releases(reading.rows),
                      devices_visible=devices_visible)


# --- Needs attention on the Advisories tab ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AdvisoryRow(AttentionRow):
    """An AttentionRow of the Advisories tab's Needs attention: also the advisory it is for and Open software update (`open_update`:
    slice 38's door from the advisory, for permissions.can_open_updates only)."""

    advisory: object | None = None
    open_update: Link | None = None


def _advisory_page_link(advisory) -> Link:
    return Link(URLS["advisory"], (advisory.number,))


def _owner_words(a) -> str:
    """ "rental, Summit Medical Rentals; due back Nov 15, 2026" (a temporary device's stay)."""
    who = a.get_ownership_display().lower()
    if a.owner:
        who += f", {a.owner}"
    if a.due_back_on:
        who += f"; due back {day_words(a.due_back_on)}"
    return who


def _temporary_row(answer, advisory, *, may_decide: bool, may_open: bool) -> AdvisoryRow:
    """ "T-0101 (rental, Summit Medical Rentals; due back Nov 15, 2026): the advisory names the Centrella without stating releases
    (AD-26-0005); its owner installs updates: ask the owner to update it, or return it" (critique40_practice B3), with Record decision
    (device scope) and Open software update where it can open (a release or patches remedy, no other update open: practice N3)."""
    line = answer.line
    a, row = line.asset, line.row
    no_fix = row.remedy in NO_FIX_REMEDIES
    ask = "ask the owner about it" if no_fix else "ask the owner to update it"
    words = (f"{a.tag} ({_owner_words(a)}): {_lower_first(line.words)} ({advisory.number}); its owner installs updates: {ask}, "
             "or return it")
    record = Link(URLS["record"], (), (("advisory", advisory.number), ("asset", a.tag))) if may_decide else None
    elsewhere = any(r.kind == UPDATE_ELSEWHERE for r in answer.responses)
    opener = (Link(URLS["open_update"], (a.tag,), (("advisory", advisory.number), ("from", "advisory")))
              if may_open and not no_fix and not elsewhere else None)
    return AdvisoryRow(words, link=_device_link(a), record=record, asset=a, tags=(a.tag,), advisory=advisory, open_update=opener)


def _no_response_row(advisory, row, found: list, *, may_decide: bool) -> AdvisoryRow:
    """ "AD-26-0004 · Alaris 8015 PCU: 6 devices of ours; 4 on a release it names, 2 with no release recorded", with Record decision
    (model scope) for can_decide."""
    from .models import AdvisoryVersions

    n = len(found)
    words = f"{advisory.number} · {row.device_model.model}: {_plural(n, 'device', 'devices')} of ours"
    if row.versions == AdvisoryVersions.LISTED:
        named = sum(a.line.kind == NAMED for a in found)
        parts = [f"{named} on a release it names" if named else "", f"{n - named} with no release recorded" if n - named else ""]
        words += "; " + ", ".join(p for p in parts if p)
    else:
        words += f"; {_lower_first(found[0].line.words)}"
    record = (Link(URLS["record"], (), (("advisory", advisory.number), ("model", row.device_model_id))) if may_decide else None)
    return AdvisoryRow(words, count=n, link=_advisory_page_link(advisory), record=record, tags=tuple(a.line.asset.tag for a in found),
                       advisory=advisory)


def _named_since_row(advisory, name: str, count: int, tags) -> AdvisoryRow:
    if advisory.status == "closed":
        head = f"{name}: closed {day_words(advisory.status_on)}"
    else:
        head = f"{name}: not applicable here since {day_words(advisory.status_on)}"
    return AdvisoryRow(f"{head}; {_plural(count, 'device', 'devices')} named since", count=count, link=_advisory_page_link(advisory),
                       tags=tuple(tags), advisory=advisory)


def _decisions_due(today: date) -> int:
    """Live advisory decisions past their review whose scope still holds a device on site (holds_device_q: the badge's and the Decisions
    tab's review_due condition, so the list `?why=advisory&state=review_due&here=1` opens shows the same). One query."""
    return (DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.ADVISORY, ended_on__isnull=True, review_by__lt=today)
            .filter(holds_device_q()).count())


def advisory_attention(today: date, *, user) -> Attention:
    """The Advisories tab's Needs attention (build40 section 7.2), as data: ADVISORY_ATTENTION_HEADINGS in order (only those with rows),
    each with its count and rows (AdvisoryRow: links as URL names, no HTML); empty, ATTENTION_EMPTY. `user` None is the system; a reader
    without Device security View reads an empty panel. Gated per heading:
    - Device security View: to_confirm (open advisories with models to confirm, one row each); changed (closed advisories with models to
      confirm since a revision: stored state); checks (Release added since, per advisory open or closed).
    - can_view_devices also: no_response (an open advisory's devices of ours with no response recorded, one row per advisory and model
      row; Record decision for the model for can_decide); temporary (each temporary device an open advisory names with no response
      recorded: only its own decision governs it; Record decision for it, can_decide, and Open software update, can_open_updates);
      not_confirmed (an open advisory's devices with an update done and the version not confirmed, one row per device; the work order's
      link for Work orders View); other_list (an open advisory's devices with patches naming another list); named_since (closed ones
      Close would now refuse, close_blockers; not-applicable ones whose reason no longer holds, not_applicable_blocks: one predicate
      each, locks S7); decisions_due (live advisory decisions past their review date whose scope holds a device on site, one row linking
      to the Decisions tab's `?why=advisory&state=review_due&here=1`).
    Fixed queries however many advisories, rows, devices, and decisions: the advisories (one), their sources (one, the names),
    advisory_reading's (for can_view_devices), the models' releases (one), the decisions past review (one, for can_view_devices)."""
    from apps.accounts.models import Level, Module

    from . import permissions as perms
    from .models import AdvisorySource, AdvisoryStatus, SecurityAdvisory

    if user is not None and not perms.can_view(user):
        return Attention(headings=())
    devices_visible = user is None or perms.can_view_devices(user)
    may_decide = user is None or perms.can_decide(user)
    may_open = user is None or perms.can_open_updates(user)
    sees_work = user is None or user.has_level(Module.WORKORDERS, Level.VIEW)
    found = list(SecurityAdvisory.objects.filter(removed_on__isnull=True).order_by("received_on", "number"))
    sources: dict = {}
    for s in AdvisorySource.objects.filter(advisory_id__in=[a.pk for a in found]).order_by("created_at", "pk") if found else ():
        sources.setdefault(s.advisory_id, []).append(s)
    if devices_visible:
        reading = advisory_reading(found, today)
    else:
        reading = AdvisoryReading(rows_of(found) if found else [], [], {})
    rows_by: dict = {}
    for r in reading.rows:
        rows_by.setdefault(r.advisory_id, []).append(r)
    lines_by: dict = {}
    for ln in reading.lines:
        lines_by.setdefault(ln.row.advisory_id, []).append(ln)
    answers_by: dict = {}
    for key, a in reading.answers.items():
        answers_by.setdefault(a.line.row.advisory_id, {})[key] = a
    current = _current_releases([r for a in found if a.status in HAS_NEEDS for r in rows_by.get(a.pk, ())])

    out: dict = {key: [] for key in ADVISORY_ATTENTION_HEADINGS}
    not_confirmed: dict = {}  # {asset id: (asset, [advisories], the first response)}
    for adv in found:
        rows = rows_by.get(adv.pk, [])
        name = advisory_name(adv, rows=rows, sources=sources.get(adv.pk, []))
        if adv.status == AdvisoryStatus.OPEN and adv.models_confirmed_on is None:
            out["to_confirm"].append(AdvisoryRow(name, link=_advisory_page_link(adv), advisory=adv))
        if adv.status == AdvisoryStatus.CLOSED and adv.models_confirmed_on is None:
            out["changed"].append(AdvisoryRow(f"{name}: closed {day_words(adv.status_on)}", link=_advisory_page_link(adv), advisory=adv))
        lines, answers = lines_by.get(adv.pk, []), answers_by.get(adv.pk, {})
        for check in _checks_of(adv, rows, lines, answers, current, devices_visible=False):
            if check.key == CHECK_RELEASE_ADDED:
                out["checks"].append(AdvisoryRow(f"{adv.number} · {check.words}", link=_advisory_page_link(adv), advisory=adv))
        if not devices_visible:
            continue
        if adv.status == AdvisoryStatus.OPEN:
            for row in rows:
                ours = sorted((a for a in answers.values() if a.line.row.pk == row.pk and a.kind == NO_RESPONSE
                               and a.line.asset.ownership == Ownership.OWNED), key=lambda a: a.line.asset.tag)
                if ours:
                    out["no_response"].append(_no_response_row(adv, row, ours, may_decide=may_decide))
            for a in sorted(answers.values(), key=lambda a: (a.line.asset.tag, a.line.row.created_at)):
                asset = a.line.asset
                if a.kind == NO_RESPONSE and asset.ownership != Ownership.OWNED:
                    out["temporary"].append(_temporary_row(a, adv, may_decide=may_decide, may_open=may_open))
                elif a.kind == UPDATE_NOT_CONFIRMED:
                    response = next(r for r in a.responses if r.kind == UPDATE_NOT_CONFIRMED)
                    held = not_confirmed.setdefault(asset.pk, (asset, [], response))
                    if adv.number not in held[1]:
                        held[1].append(adv.number)
                elif a.kind == PATCHES_OTHER_LIST:
                    words = next(r.words for r in a.responses if r.kind == PATCHES_OTHER_LIST)
                    out["other_list"].append(AdvisoryRow(f"{adv.number} · {asset.tag}: {words}", link=_advisory_page_link(adv), asset=asset,
                                                         tags=(asset.tag,), advisory=adv))
        elif adv.status in (AdvisoryStatus.CLOSED, AdvisoryStatus.NOT_APPLICABLE):
            for check in _checks_of(adv, rows, lines, answers, current, devices_visible=True):
                if check.key == CHECK_NAMED_SINCE:
                    out["named_since"].append(_named_since_row(adv, name, len(check.tags), check.tags))
    for asset, numbers, response in sorted(not_confirmed.values(), key=lambda held: held[0].tag):
        out["not_confirmed"].append(AdvisoryRow(f"{', '.join(numbers)} · {asset.tag}: {response.words}", link=_device_link(asset),
                                                asset=asset, tags=(asset.tag,),
                                                work_order=Link(URLS["work_order"], (response.number,)) if sees_work else None))
    if devices_visible:
        due = _decisions_due(today)
        if due:
            words = f"{_plural(due, 'advisory decision', 'advisory decisions')} past {'its' if due == 1 else 'their'} review date"
            out["decisions_due"].append(AdvisoryRow(words, count=due, link=Link(URLS["decisions"], (), (
                ("why", "advisory"), ("state", "review_due"), ("here", "1")))))
    headings = tuple(Heading(key, words, sum(r.count for r in out[key]), tuple(out[key]))
                     for key, words in ADVISORY_ATTENTION_HEADINGS.items()
                     if out[key] and (devices_visible or key in ATTENTION_VIEW))
    return Attention(headings=headings)


# --- the nav badge's advisory half -------------------------------------------------------------------------------------------------------

def advisory_badge(today: date) -> dict:
    """{"count": n}: open advisories, not entered in error, with models to confirm (stored rows only, no clock: `today` is unused; one
    query). The caller gates it (permissions.can_edit; reports.services._cyber_badge adds slice 39's half and words the title:
    ADVISORY_BADGE_TITLE, BOTH_BADGE_TITLE). Never hot: only decisions past their review make the badge hot."""
    from .models import AdvisoryStatus, SecurityAdvisory

    count = SecurityAdvisory.objects.filter(status=AdvisoryStatus.OPEN, removed_on__isnull=True, models_confirmed_on__isnull=True).count()
    return {"count": count}
