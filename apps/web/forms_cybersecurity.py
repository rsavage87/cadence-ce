"""
Input parsing for the Device security screen (slice 37): the Fleet's filters, the Models tab's search, Changes for IT's window, and the
forms of a software release (or the model's profile) and Set devices to this release. The rules live in apps.cybersecurity.services:
these forms render the fields, keep what was typed, and hand the text to the services as typed (days as ISO text, choices as their
slugs), so every problem comes back at once in the services' words, keyed by the field it names.

No free text: a version, an operating system's name, an MDS2 document number (a token), and the MDS2's https link, each checked by the
service; every answer is a choice, "Not recorded" where nothing is. The MDS2's answers are named by their questions in words, never by a
form's question number (a 2013 or 2008 form numbers them otherwise). Nothing here says "required": a field the service needs says so in
its refusal.
"""
from dataclasses import dataclass, fields

from django import forms

from apps.cybersecurity import rules
from apps.cybersecurity.models import (
    DISCLOSURE_GROUP,
    DOCUMENT_MAX,
    FACILITY_GROUP,
    MDS2_GROUP,
    OS_NAME_MAX,
    SUPPORT_GROUP,
    VERSION_MAX,
    Connects,
    Disclosed,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    Segmented,
    UpdatesBy,
)
from apps.cybersecurity.services import KEEP, URL_MAX
from apps.equipment.models import Ownership

from .forms import WHOSE_OURS, WHOSE_TEMPORARY, parse_uuid

NOT_RECORDED = ("", "Not recorded")
DATE = {"type": "date"}

# --- the Fleet's filters -----------------------------------------------------------------------------------------------------------

WHOSE_FILTERS = [("", "Ours and temporary"), (WHOSE_OURS, "Ours"), (WHOSE_TEMPORARY, "Temporary on site")]
EITHER = "either"  # connected here or able to connect: what the head figures count (rules.ON_SITE_KEYS)
CONNECTED_FILTERS = [("", "Any connection"), (EITHER, "Connected here or able to connect"), ("connected", "Connected here, as recorded"),
                     ("can_connect", "Can connect, as disclosed"), ("not_connected", "Not connected"),
                     ("not_recorded", "Connection not recorded")]
_CONNECTED_KEYS = {EITHER: rules.ON_SITE_KEYS, "connected": (rules.CONNECTED,), "can_connect": (rules.CAN_CONNECT,),
                   "not_connected": (rules.NOT_CONNECTED,), "not_recorded": (rules.CONNECTION_NOT_RECORDED,)}
STANDING_FILTERS = [("", "Any standing"), ("updates_ended", "Past security updates or the model's support"),
                    ("updates_end_announced", "Security updates end announced"),
                    ("security_not_recorded", "Security updates end not recorded"),
                    ("os_ended", "OS past its vendor's published support"), ("os_end_announced", "OS support end announced")]
_STANDING = {"updates_ended": lambda s: s.security.key in rules.ENDED_KEYS,
             "updates_end_announced": lambda s: s.security.key == rules.UPDATES_END_ANNOUNCED,
             "security_not_recorded": lambda s: s.security.key == rules.SECURITY_NOT_RECORDED,
             "os_ended": lambda s: s.os.key == rules.OS_ENDED,
             "os_end_announced": lambda s: s.os.key == rules.OS_END_ANNOUNCED}
NORELEASE = "1"


@dataclass(frozen=True)
class FleetFilters:
    """The Fleet's filters (and a model panel's, carried from the Fleet): whose, department (its id), category, the device's connection
    as the register reads it, its standing, and "release not recorded". The last three read each device's facts (rules.standings)."""

    whose: str = ""
    dept: str = ""
    category: str = ""
    connected: str = ""
    standing: str = ""
    norelease: bool = False

    @property
    def any(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    @property
    def by_device(self) -> bool:
        """A filter on a device's facts: read through rules.standings, then kept or left out one by one."""
        return bool(self.connected or self.standing or self.norelease)

    def matches(self, s) -> bool:
        """A device's standing (rules.DeviceStanding) passes the facts' filters."""
        if self.connected and s.connection.key not in _CONNECTED_KEYS[self.connected]:
            return False
        if self.standing and not _STANDING[self.standing](s):
            return False
        return not (self.norelease and s.release is not None)

    def keeps(self, s) -> bool:
        """Every filter on one device's standing (a model panel's devices, already read)."""
        a = s.asset
        ours = a.ownership == Ownership.OWNED
        if (self.whose == WHOSE_OURS and not ours) or (self.whose == WHOSE_TEMPORARY and ours):
            return False
        if (self.dept and str(a.department_id) != self.dept) or (self.category and a.device_model.category != self.category):
            return False
        return self.matches(s)

    def params(self) -> dict:
        """The filters as query parameters (the panel's links carry them)."""
        out = {"whose": self.whose, "dept": self.dept, "category": self.category, "connected": self.connected, "standing": self.standing,
               "norelease": NORELEASE if self.norelease else ""}
        return {k: v for k, v in out.items() if v}


def parse_fleet_filters(params, options: dict) -> FleetFilters:
    """The filters from the address: unknown values are dropped, never a 500. `options`: the departments ([(id, name)]) and categories
    the Fleet offers."""
    def one_of(name, allowed):
        value = params.get(name, "")
        return value if value in allowed else ""

    dept = params.get("dept", "")
    dept = dept if parse_uuid(dept) is not None and dept in {d for d, _ in options["departments"]} else ""
    return FleetFilters(whose=one_of("whose", {v for v, _ in WHOSE_FILTERS}), dept=dept, category=one_of("category", options["categories"]),
                        connected=one_of("connected", _CONNECTED_KEYS), standing=one_of("standing", _STANDING),
                        norelease=params.get("norelease") == NORELEASE)


def parse_search(params, limit: int = 120) -> str:
    """The Models tab's search (a manufacturer or a model): spaces collapsed, NUL characters taken out (a database refuses them)."""
    return " ".join(str(params.get("q", "")).replace("\x00", "").split())[:limit]


def parse_days(params) -> int:
    """Changes for IT's window: 7, 30 (the default), or 90 days."""
    value = str(params.get("days", ""))
    return int(value) if value.isdigit() and int(value) in rules.CHANGES_DAYS else 30


# --- the forms ---------------------------------------------------------------------------------------------------------------------

def _text(label, limit, help_text="", placeholder=""):
    attrs = {"autocomplete": "off", "maxlength": limit, "spellcheck": "false"}
    if placeholder:
        attrs["placeholder"] = placeholder
    return forms.CharField(label=label, required=False, strip=False, help_text=help_text, widget=forms.TextInput(attrs=attrs))


def _day(label, help_text=""):
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.DateInput(attrs=DATE))


def _choice(label, choices, help_text=""):
    """A choice kept as typed, "Not recorded" first: the service refuses one not listed, in its words, with the other problems."""
    return forms.CharField(label=label, required=False, help_text=help_text, widget=forms.Select(choices=[NOT_RECORDED, *choices]))


class ServiceForm(forms.Form):
    """A form whose save is a service call: its refusals (a ValidationError keyed by field) on the fields they name (or the field
    `aliases` names for a key the form has no field for), anything else at the top. The fields are text as typed, never refused here
    beyond a NUL character: the service checks them all at once."""

    aliases: dict = {}
    layout: tuple = ()

    def add_service_errors(self, error):
        if not hasattr(error, "error_dict"):
            self.add_error(None, getattr(error, "messages", None) or [str(error) or "You don't have access to do that."])
            return
        for key, messages in error.message_dict.items():
            key = self.aliases.get(key, key)
            self.add_error(key if key in self.fields else None, messages)

    def focus_first_error(self) -> str:
        """Sent back with errors: htmx focuses the [autofocus] element it swaps in, so make that the first field with an error (in
        `layout`, the order the modal lays them out). Returns its name ("" for none)."""
        first = next((name for name in self.layout if name in self.errors and name in self.fields), "")
        for f in self.fields.values():
            f.widget.attrs.pop("autofocus", None)
        if first:
            self.fields[first].widget.attrs["autofocus"] = True
        return first


# The modal's sections: (legend, the fields in it, a line under them). The version is a release's own; the facility group the profile's.
RELEASE_SECTIONS = (
    ("", ("version",), ""),
    ("Operating system and security updates", SUPPORT_GROUP, "Days as each vendor states them: the last day of its support or updates."),
    ("The manufacturer's disclosure (MDS2)", MDS2_GROUP,
     "The form, document, release day, and link are kept only while the MDS2 is on file; the day requested only while it is requested."),
    ("What the manufacturer discloses", DISCLOSURE_GROUP, rules.MDS2_SENTENCE),
    ("This facility's arrangement", FACILITY_GROUP, "For every device of this model here."),
)
# The questions the cards and the modal name the answers by (never a form's question number): rules.QUESTIONS, one wording everywhere
QUESTIONS = rules.QUESTIONS


class ReleaseForm(ServiceForm):
    """Add release, the model's profile, and Edit: every field of SoftwareRelease as typed. A profile (`profile=True`) has no version
    and holds the facility group; a versioned release never offers the facility group (the profile's only)."""

    version = _text("Version", VERSION_MAX, "As the manufacturer writes it.", placeholder="e.g. 4.2.1")
    os_family = _choice("Operating system", OsFamily.choices)
    os_name = _text("Operating system's name and edition", OS_NAME_MAX, "As its vendor names it, with the edition.")
    os_support_ends_on = _day("The OS vendor's published support ends", "Its last day, as the OS vendor states it.")
    security_updates_end_on = _day("The manufacturer's security updates end",
                                   "Their last day, as the manufacturer states it. Blank when none is announced.")
    support_checked_on = _day("Support checked", "The day someone checked these dates with the vendors.")
    mds2 = _choice("MDS2", Mds2Status.choices, "The Manufacturer Disclosure Statement for Medical Device Security, a voluntary form.")
    mds2_edition = _choice("MDS2 form", Mds2Edition.choices, "The year on the form, while it is on file.")
    mds2_document = _text("MDS2 document number", DOCUMENT_MAX, "As the form shows it, without spaces.")
    mds2_released_on = _day("MDS2 released", "The day on the form.")
    mds2_requested_on = _day("MDS2 requested on", "The day it was asked of the manufacturer.")
    mds2_url = forms.CharField(label="MDS2 link", required=False, strip=False,
                               help_text="The manufacturer's https page for it. Cadence keeps the link, never the file.",
                               widget=forms.URLInput(attrs={"autocomplete": "off", "maxlength": URL_MAX, "spellcheck": "false",
                                                            "placeholder": "https://"}))
    patient_data = _choice(QUESTIONS["patient_data"], PatientData.choices)
    connects = _choice(QUESTIONS["connects"], Connects.choices)
    remote_service = _choice(QUESTIONS["remote_service"], Disclosed.choices)
    patching = _choice(QUESTIONS["patching"], Patching.choices)
    sbom = _choice(QUESTIONS["sbom"], Sbom.choices)
    updates_by = _choice(QUESTIONS["updates_by"], UpdatesBy.choices)
    segmented = _choice(QUESTIONS["segmented"], Segmented.choices)
    layout = tuple(name for _legend, names, _hint in RELEASE_SECTIONS for name in names)

    def __init__(self, *args, profile: bool, **kwargs):
        kwargs.setdefault("auto_id", "cy-%s")
        super().__init__(*args, **kwargs)
        self.profile = profile
        for name in (("version",) if profile else FACILITY_GROUP):
            del self.fields[name]

    def sections(self) -> list:
        """[(legend, [bound fields], hint)] in the modal's order, the sections this form has."""
        out = []
        for legend, names, hint in RELEASE_SECTIONS:
            bound = [self[name] for name in names if name in self.fields]
            if bound:
                out.append((legend, bound, hint))
        return out

    def service_kwargs(self) -> dict:
        """Every field this form has, as typed (the service strips and checks them)."""
        return {name: self.cleaned_data.get(name) or "" for name in self.fields}


def release_initial(row) -> dict:
    """A release's (or the profile's) fields as the form shows them: days as ISO text, nothing as blank."""
    out = {}
    for name in ReleaseForm.base_fields:
        value = getattr(row, name)
        out[name] = value.isoformat() if hasattr(value, "isoformat") else (value or "")
    return out


KEEP_NETWORK = "keep"
NETWORK_CHOICES = [(KEEP_NETWORK, "Leave each device's as it is"), ("", "As the model discloses"), *Network.choices]


class SetReleaseForm(ServiceForm):
    """Set devices to this release (services.record_facts_for): one of the model's versioned releases, every device of the model on
    site or one department's, since a day (blank: today, for each device whose release changes), and how they are connected here."""

    release = forms.CharField(label="Release", required=False, widget=forms.Select())
    department = forms.CharField(label="Devices", required=False, widget=forms.Select(),
                                 help_text="Every device of this model on site (ours and temporary), or one department's.")
    release_since = _day("Running it since", "The day it was installed or found. Blank: today, for each device whose release changes.")
    network = forms.CharField(label="Connected here", required=False, widget=forms.Select(choices=NETWORK_CHOICES))
    aliases = {"assets": "department"}  # the devices the service is given are the department's (or every one): their problems go there
    layout = ("release", "department", "release_since", "network")

    def __init__(self, *args, releases: list, departments: list, **kwargs):
        kwargs.setdefault("auto_id", "cy-set-%s")
        super().__init__(*args, **kwargs)
        self.fields["release"].widget.choices = [("", "Choose…")] + [(str(r.pk), r.version) for r in releases]
        self.fields["department"].widget.choices = [("", "Every department")] + [(d, f"{name} only") for d, name in departments]
        self.department_ids = {d for d, _ in departments}

    def department_id(self) -> str:
        return (self.cleaned_data.get("department") or "").strip()

    def service_kwargs(self) -> dict:
        d = self.cleaned_data
        network = d.get("network") or ""
        return {"release": (d.get("release") or "").strip() or None, "release_since": (d.get("release_since") or "").strip() or None,
                "network": KEEP if network == KEEP_NETWORK else network}
