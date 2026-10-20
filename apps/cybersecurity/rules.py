"""
The device security register's rules (slice 37): every door (the Device security screen, the device drawer's Security tab, the API) reads
these, never the fields directly, so the words and the fallbacks are the same everywhere.

- Facts: a device's release and its model's profile, merged BY GROUP (support, MDS2, disclosure): a group comes from the release when any
  of its fields is set there, else from the profile ("the model's disclosure, version not stated"), else "not recorded". The facility
  group (who installs updates here, segmentation) is the profile's.
- Connection: the device's own network fact when recorded ("Connected (wireless)", "Not connected here"), else what its model discloses
  ("Can connect (wireless), as disclosed"), else "Not recorded".
- Standing, two separate lines, never merged: the manufacturer's security updates for the release (or, with no date of its own, the
  model's manufacturer support from slice 36: equipment.services.support_status), and the operating system vendor's published support.
  Dates are last days: "Security updates end Mar 31, 2028 (announced)", "Security updates ended after Mar 31, 2028"; "The OS vendor's
  published support for Windows 10 IoT Enterprise LTSC 2021 ends Jan 13, 2032" / "ended after ...". A fact about the date on file,
  never about this unit (Extended Security Updates are not modeled).

Words never used (a test pins them): secure, compliant, certified, patched, unpatched, vulnerable, unsupported, mitigated, protected,
safe, at risk, exposed, exploitable, remediated, end of life, high risk, critical, required.

Counting: every device on site that is not retired (ours and temporary alike: a rental is on the network too), with slice 29's Whose
filter; each figure says which devices it counts. Read in a fixed number of queries (standings, fleet, changes_for_it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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

    @property
    def release_words(self) -> str:
        """ "9.33.1, since Mar 3, 2026" or RELEASE_NOT_RECORDED."""
        raise NotImplementedError


def on_site():
    """The devices the register lists: every device not retired (ours and temporary), with model and department."""
    raise NotImplementedError


def profile(model):
    """The model's profile row (version blank, not removed), or None."""
    raise NotImplementedError


def releases(model) -> list:
    """The model's versioned releases, not removed, in version_key order."""
    raise NotImplementedError


def release_of(asset):
    """The versioned release the device runs, or None. Every reader asks this, never asset.cyber.release (a later multi-component
    record stays additive)."""
    raise NotImplementedError


def facts(release, profile) -> Facts:
    """Merge a versioned release (or None) with its model's profile (or None), by group."""
    raise NotImplementedError


def connected(security, facts: Facts) -> Connection:
    """The device's connection: its own network fact (security may be None), else its model's disclosure, else not recorded."""
    raise NotImplementedError


def release_standing(facts: Facts, model, today: date) -> Standing:
    """The manufacturer's security updates for the release (slice 36's model support when the release has no date of its own)."""
    raise NotImplementedError


def os_standing(facts: Facts, today: date) -> Standing:
    """The OS vendor's published support for the release's operating system."""
    raise NotImplementedError


def device_standing(asset, today: date) -> DeviceStanding:
    """One device (the drawer's tab): standings() for one."""
    raise NotImplementedError


def standings(assets, today: date) -> dict:
    """{asset id: DeviceStanding} for the devices of `assets`, in a fixed number of queries (lists, CSVs, the API)."""
    raise NotImplementedError


@dataclass(frozen=True)
class FleetRow:
    device_model: object
    devices: int
    connected: int  # connected here, by the device's fact or its model's disclosure
    by_release: list  # [(release or None for not recorded, count)]
    updates_ended: int  # devices on a release past the manufacturer's security updates (or the model's support)
    os_ended: int  # devices on a release whose OS vendor's published support ended
    facts: Facts  # the profile's facts (MDS2, patient data, updates by, segmented)


@dataclass(frozen=True)
class Fleet:
    rows: list
    figures: dict  # the head figures, each a count with the devices it counts named in words


def fleet(today: date, assets=None) -> Fleet:
    """One row per model with devices on site (or among `assets`, a filtered queryset), and the head figures: models that can connect
    with no MDS2 on file (requested apart), connected devices on a release past security updates, devices whose model can connect with
    no release recorded, models with connected devices not on a separate segment. A fixed number of queries."""
    raise NotImplementedError


@dataclass(frozen=True)
class Change:
    asset: object
    day: date
    kind: str  # "added", "missing", "retired", "returned"
    words: str


def changes_for_it(today: date, days: int = 30) -> list:
    """Devices whose model can connect, or can store or handle patient data, or whose own fact says connected, added, marked missing,
    retired, or returned to their owner in the last `days` days (equipment's history through core.history._rows), newest first. Every
    device, ours and temporary. A fixed number of queries."""
    raise NotImplementedError
