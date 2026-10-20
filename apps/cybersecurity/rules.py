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
critical, required.

Counting: every device on site that is not retired (ours and temporary alike: a rental is on the network too), with slice 29's Whose
filter; each figure says which devices it counts. Read in a fixed number of queries (standings, fleet, changes_for_it).

A release names its model: a device whose stored release belongs to another model (a model change racing a record) reads as "Release not
recorded", never another model's facts (equipment.services.update_asset clears it through services.model_changed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.db.models import QuerySet, Subquery
from django.utils import timezone

from apps.core.days import local_day
from apps.core.history import _rows
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, Ownership
from apps.equipment.services import support_status

from .models import (
    DISCLOSURE_GROUP,
    FACILITY_GROUP,
    GROUPS,
    MDS2_GROUP,
    SUPPORT_GROUP,
    Connects,
    DeviceSecurity,
    Mds2Status,
    Network,
    OsFamily,
    PatientData,
    Segmented,
    SoftwareRelease,
)

THIS_RELEASE = "this release"
PROFILE = "the model's disclosure, version not stated"
NOT_RECORDED = "not recorded"

# Release standing keys (the manufacturer's security updates)
UPDATES_ENDED, UPDATES_END_ANNOUNCED, MODEL_SUPPORT_ENDED, SECURITY_NOT_RECORDED = (
    "updates_ended", "updates_end_announced", "model_support_ended", "not_recorded")
# Operating system standing keys
OS_ENDED, OS_END_ANNOUNCED, OS_NOT_RECORDED, NO_OS = "os_ended", "os_end_announced", "os_not_recorded", "no_os"
# Connection keys
CONNECTED, NOT_CONNECTED, CAN_CONNECT, CONNECTION_NOT_RECORDED = "connected", "not_connected", "can_connect", "not_recorded"

RELEASE_NOT_RECORDED = "Release not recorded"
MDS2_SENTENCE = ("The MDS2 describes what the device can do; whether you may change it is the manufacturer's documentation and your "
                 "contract.")
HICP_SENTENCE = ("HHS 405(d) HICP 2023 recommends keeping the medical device inventory, with at least major version information, in the "
                 "CMMS.")
CHANGES_DAYS = (7, 30, 90)  # Changes for IT: the windows offered (30 by default)

# Never in Cadence's voice on any door (build37 "Words"): matched as whole words, in any letter case (tests scan every door with it).
NEVER_WORDS = ("secure", "compliant", "certified", "patched", "unpatched", "vulnerable", "unsupported", "mitigated",
               "protected", "safe", "at risk", "exposed", "exploitable", "remediated", "end of life", "high risk", "critical", "required",
               "requires", "NPRM", "FDA-approved", "FDA-cleared", "IEC 80001", "DNV")

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


def _mds2_of(rows) -> tuple[str, tuple]:
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
        mds2, versions = _mds2_of(held)
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
    """[(device id, kind, day, moment, added_as)] from the devices' history rows in order (id, history_date, history_id): a device added
    ("+"), its status becoming missing, or retired (returned to its owner when it was temporary then), on a facility day in [start,
    today]. A device whose retirement is dated before the day it was added (a retirement from the previous system, backdated) was never
    on site: its addition is left out."""
    out, previous, added, retired_days = [], {}, {}, {}
    for pk, kind, status, ownership, added_as, moment in rows:
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
            retired_days.setdefault(pk, []).append(day)
            out.append((pk, RETIRED if ownership == Ownership.OWNED else RETURNED, day, moment, ""))
    for pk, (day, moment, added_as) in added.items():
        if not any(d < day for d in retired_days.get(pk, ())):
            out.append((pk, ADDED, day, moment, added_as))
    return [e for e in out if start <= e[2] <= today]


def changes_for_it(today: date, days: int = 30) -> list:
    """Devices whose model can connect, or can store or handle patient data (as the manufacturer discloses: the device's facts, its
    release over its model's profile by group), or whose own fact says connected, added, marked missing, retired, or returned to their
    owner on a facility day in [today - days + 1, today] (equipment's history through core.history._rows), newest first. Every device,
    ours and temporary. Three queries (one when nothing changed): the history rows of the devices with a row in the window, the devices
    with their rows, their models' profiles."""
    start = today - timedelta(days=days - 1)
    since = timezone.make_aware(datetime.combine(start - timedelta(days=1), time.min))  # a day early: the facility's day decides below
    history = Asset.history.model
    recent = _rows(history).filter(history_date__gte=since).values("id")
    rows = (_rows(history).filter(id__in=Subquery(recent)).order_by("id", "history_date", "history_id")
            .values_list("id", "history_type", "status", "ownership", "added_as", "history_date"))
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
