"""
The device security register (slice 37, beyond the mock): what each model's software can do and what its manufacturer discloses, and
what this facility knows about each device's software and connection.

No US law, CMS rule, or accreditor requires a per-device cybersecurity record; HHS 405(d) HICP 2023 (Technical Volume 2, Practice #9)
recommends keeping the medical device inventory, with at least major version information, in the CMMS. The MDS2 (the Manufacturer
Disclosure Statement for Medical Device Security) is a voluntary form a manufacturer fills in for a model and version. Cadence keeps the
facts and never says a rule requires them, never "secure", "compliant", "patched", or "vulnerable" as a status (rules.py words them).

Never kept: device credentials, network identifiers (IP, MAC, hostname, VLAN, ports), SBOM contents, the MDS2 file, advisory text, free
text of any kind. References are tokens and https links; answers are choices, blank where not recorded.

- SoftwareRelease: one software release of a device model (its version as the manufacturer writes it), or the model's PROFILE (version
  blank: "all versions, or version not stated", at most one per model). A versioned release overrides the profile by group (support, MDS2,
  disclosure), never field by field; the facility group (who installs updates here, segmentation) is the profile's only.
- DeviceSecurity: one device's facts here: the versioned release it runs and since when, how it is connected here when that differs from
  what the model discloses, and the day someone checked. Its own record, so nothing reaches the device's API, CSV, history area, or a
  scoped reader by default.
- SoftwareUpdate (slice 38): one software update work order's record, made when it is opened (its target: a release of the device's
  model, or the manufacturer's patches) and filled in with what was done (installed, patches applied, or already on it) when it is
  completed or confirmed afterwards; or an install recorded by hand, with no work order (the manufacturer remotely, automatic, IT). The
  work order carries only the neutral type and a fixed problem: the target and the versions are here, behind Device security View.

Every rule is apps.cybersecurity.rules; every write is apps.cybersecurity.services (and equipment.services.update_asset's model change,
through services.model_changed), and slice 38's updates are apps.cybersecurity.updates.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel
from apps.core.validators import product_text, reference_token

VERSION_VALIDATOR = product_text("Enter the version as the manufacturer writes it: letters, digits, spaces, and . _ / ( ) + - only.")
OS_NAME_VALIDATOR = product_text("Enter the operating system with its edition, e.g. Windows 10 IoT Enterprise LTSC 2021: letters, digits, "
                                 "spaces, and . _ / ( ) + - only.")
MDS2_DOCUMENT_VALIDATOR = reference_token("Enter the MDS2's document number as the form shows it, without spaces.")
UPDATE_REFERENCE_VALIDATOR = reference_token("Enter the manufacturer's patch list, notice, or field change number as it is written, "
                                             "without spaces, e.g. ICAR-2026-09.")
# Slice 38 review fix C15: a software update's copy of its work order's vendor report number (workorders.WorkOrder.checks_reference)
VENDOR_REPORT_VALIDATOR = reference_token("Enter the vendor's service report or field service order number as it is printed, without "
                                          "spaces.")
VERSION_MAX, OS_NAME_MAX, DOCUMENT_MAX, UPDATE_REFERENCE_MAX = 60, 80, 40, 40


class OsFamily(models.TextChoices):
    WINDOWS = "windows", "Windows"
    LINUX = "linux", "Linux"
    EMBEDDED = "embedded", "Embedded or real-time"
    OTHER = "other", "Other"
    NONE = "none", "No operating system"


class Mds2Status(models.TextChoices):
    ON_FILE = "on_file", "On file"
    REQUESTED = "requested", "Requested from the manufacturer"
    NONE_FROM_MANUFACTURER = "none_from_manufacturer", "The manufacturer has none"


class Mds2Edition(models.TextChoices):
    E2004 = "2004", "2004 form"
    E2008 = "2008", "2008 form"
    E2013 = "2013", "2013 form (HN 1-2013)"
    E2019 = "2019", "2019 form (HN 1-2019)"
    LATER = "later", "A later form"


class PatientData(models.TextChoices):
    """What the device can do with patient data, as the manufacturer discloses (never that patient data is on this unit)."""

    STORES = "stores", "Can store patient data"
    HANDLES = "handles_not_stored", "Displays or sends patient data, does not store it"
    NONE = "none", "No patient data"


class Connects(models.TextChoices):
    """How the model can connect, as the manufacturer discloses."""

    WIRED = "wired", "Wired network"
    WIRELESS = "wireless", "Wireless"
    BOTH = "wired_and_wireless", "Wired and wireless"
    NONE = "none", "Does not connect to a network"


class Disclosed(models.TextChoices):
    YES = "yes", "Yes"
    NO = "no", "No"


class Patching(models.TextChoices):
    """Who may install the manufacturer's software and security updates, as the manufacturer discloses (capability, not permission)."""

    FACILITY = "facility_installs", "The facility may install them, as documented"
    MANUFACTURER = "manufacturer_only", "The manufacturer or its service only"
    NONE = "no_updates", "No software updates"


class Sbom(models.TextChoices):
    AVAILABLE = "available", "Available"
    ON_REQUEST = "on_request", "On request"
    NOT_AVAILABLE = "not_available", "Not available"


class UpdatesBy(models.TextChoices):
    """Who installs updates here (the facility's arrangement for the model, not the manufacturer's disclosure)."""

    CE = "ce", "Clinical engineering"
    IT = "it", "Information technology"
    MANUFACTURER = "manufacturer", "The manufacturer or its service"
    DEPARTMENT = "department", "The department that uses it"


class Segmented(models.TextChoices):
    """Whether this facility puts the model's connected devices on a medical device network segment."""

    ALL = "all", "All on a medical device segment"
    SOME = "some", "Some on a medical device segment"
    NONE = "none", "Not on a separate segment"


class Network(models.TextChoices):
    """How this device is connected here, when it differs from what its model discloses (blank: as the model discloses)."""

    WIRED = "wired", "Connected (wired)"
    WIRELESS = "wireless", "Connected (wireless)"
    BOTH = "wired_and_wireless", "Connected (wired and wireless)"
    INTERMITTENT = "intermittent", "Connected now and then"
    NOT_CONNECTED = "not_connected", "Not connected here"


SUPPORT_GROUP = ("os_family", "os_name", "os_support_ends_on", "security_updates_end_on", "support_checked_on")
MDS2_GROUP = ("mds2", "mds2_edition", "mds2_document", "mds2_released_on", "mds2_requested_on", "mds2_url")
DISCLOSURE_GROUP = ("patient_data", "connects", "remote_service", "patching", "sbom")
FACILITY_GROUP = ("updates_by", "segmented")  # the profile row's only
GROUPS = {"support": SUPPORT_GROUP, "mds2": MDS2_GROUP, "disclosure": DISCLOSURE_GROUP}
RELEASE_FIELDS = ("version", *SUPPORT_GROUP, *MDS2_GROUP, *DISCLOSURE_GROUP, *FACILITY_GROUP)
DEVICE_FIELDS = ("release", "release_since", "network", "checked_on")


class SoftwareRelease(TenantModel):
    """A device model's software release (its version as the manufacturer writes it), or the model's profile (version blank)."""

    device_model = models.ForeignKey("equipment.DeviceModel", on_delete=models.PROTECT, related_name="software_releases")
    version = models.CharField(max_length=VERSION_MAX, blank=True, validators=[VERSION_VALIDATOR],
                               help_text="As the manufacturer writes it. Blank: the model's profile (all versions, or version not stated).")
    version_key = models.CharField(max_length=VERSION_MAX, editable=False)  # services.version_key: the uniqueness, in any letter case
    # The support group: the operating system and the manufacturer's security updates (last days, as each vendor states them)
    os_family = models.CharField(max_length=12, choices=OsFamily.choices, blank=True)
    os_name = models.CharField(max_length=OS_NAME_MAX, blank=True, validators=[OS_NAME_VALIDATOR])
    os_support_ends_on = models.DateField(null=True, blank=True)
    security_updates_end_on = models.DateField(null=True, blank=True)
    support_checked_on = models.DateField(null=True, blank=True)
    # The MDS2 group: the manufacturer's disclosure statement, referenced (never the file)
    mds2 = models.CharField(max_length=24, choices=Mds2Status.choices, blank=True)
    mds2_edition = models.CharField(max_length=8, choices=Mds2Edition.choices, blank=True)
    mds2_document = models.CharField(max_length=DOCUMENT_MAX, blank=True, validators=[MDS2_DOCUMENT_VALIDATOR])
    mds2_released_on = models.DateField(null=True, blank=True)
    mds2_requested_on = models.DateField(null=True, blank=True)
    mds2_url = models.URLField(max_length=300, blank=True)
    # The disclosure group: a few of the manufacturer's answers, in words (capability, not permission)
    patient_data = models.CharField(max_length=20, choices=PatientData.choices, blank=True)
    connects = models.CharField(max_length=20, choices=Connects.choices, blank=True)
    remote_service = models.CharField(max_length=4, choices=Disclosed.choices, blank=True)
    patching = models.CharField(max_length=20, choices=Patching.choices, blank=True)
    sbom = models.CharField(max_length=16, choices=Sbom.choices, blank=True)
    # The facility group (the profile's only): this facility's arrangement for the model
    updates_by = models.CharField(max_length=14, choices=UpdatesBy.choices, blank=True)
    segmented = models.CharField(max_length=8, choices=Segmented.choices, blank=True)
    # Entered in error: never deleted (history keeps it); hidden, never named; adding the same version again restores it
    removed_on = models.DateField(null=True, blank=True, editable=False)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                   related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["device_model__manufacturer", "device_model__model", "version_key"]
        constraints = [models.UniqueConstraint(fields=["tenant", "device_model", "version_key"], name="uniq_software_release_version")]

    def __str__(self):
        return f"{self.device_model} {self.version}" if self.version else f"{self.device_model} (all versions)"

    @property
    def is_profile(self) -> bool:
        return not self.version_key


class DeviceSecurity(TenantModel):
    """What this facility knows about one device: the versioned release of its model it runs and since when, how it is connected here
    when that differs from what the model discloses, and the day someone checked. Created on the first record (services)."""

    asset = models.OneToOneField("equipment.Asset", on_delete=models.CASCADE, related_name="cyber")
    release = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="devices")
    release_since = models.DateField(null=True, blank=True)
    network = models.CharField(max_length=20, choices=Network.choices, blank=True)
    checked_on = models.DateField(null=True, blank=True)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                   related_name="+")
    history = HistoricalRecords()

    class Meta:
        verbose_name_plural = "device security"

    def __str__(self):
        return f"{self.asset} security"


# --- slice 38: software updates ------------------------------------------------------------------------------------------------------

class UpdateTarget(models.TextChoices):
    """What an update work order was opened to install."""

    RELEASE = "release", "A release of the model"
    PATCHES = "patches", "The manufacturer's patches"


class UpdateOutcome(models.TextChoices):
    """What an update did, as recorded (blank: not recorded: an open work order's, or one completed with the version not confirmed)."""

    INSTALLED = "installed", "Release installed"
    PATCHES = "patches", "Patches applied, version unchanged"
    ALREADY = "already", "Already on the update; nothing installed"


class UpdateBasis(models.TextChoices):
    """The basis for an update, as the person recording it states it (never that Cadence knows it was authorized)."""

    VALIDATED_LIST = "validated_list", "On the manufacturer's validated patch list"
    MANUFACTURER_RELEASE = "manufacturer_release", "The manufacturer's release, service notice, or field change"
    NOT_STATED = "not_stated", "Not stated"


class InstalledBy(models.TextChoices):
    """Who installed an update recorded by hand (a work order's says who did the work itself)."""

    CE = "ce", "Clinical engineering"
    IT = "it", "Information technology"
    MANUFACTURER = "manufacturer", "The manufacturer or its service"
    AUTOMATIC = "automatic", "Installed automatically by the manufacturer"
    DEPARTMENT = "department", "The department that uses it"


RECORDED_OUTCOMES = (UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES, UpdateOutcome.ALREADY)
INSTALL_OUTCOMES = (UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES)  # counted as installs; "already" never is
# What Entered in error clears on a work order's row (updates.remove). release_before and before_taken stay (review round 2: what was
# recorded before is taken once, at the row's first recording, and a confirmation after the error reuses it).
OUTCOME_FIELDS = ("outcome", "release_after", "reference", "basis", "applied_on", "recorded_by", "later_kept", "on_vendor_service",
                  "vendor_report")


def _update_rules():
    """The row's shape (database constraints, SQLite and PostgreSQL): a work order's row has its opening and target and no installer or
    removal; a hand row (no work order) has neither opening nor target, is an install (installed or patches) by someone, and may be
    entered in error; a release target names its release, a patches target none."""
    wo_row = Q(work_order__isnull=False, opened_on__isnull=False, installed_by="", removed_on__isnull=True) & ~Q(target="")
    hand_row = (Q(work_order__isnull=True, opened_on__isnull=True, target="", outcome__in=[o.value for o in INSTALL_OUTCOMES])
                & ~Q(installed_by=""))
    return wo_row | hand_row


class SoftwareUpdate(TenantModel):
    """One software update: an update work order's record (made when it is opened, with its target; what was done filled in when it is
    completed or confirmed), or an install recorded by hand with no work order. Its one writer is apps.cybersecurity.updates."""

    asset = models.ForeignKey("equipment.Asset", on_delete=models.CASCADE, related_name="software_updates")
    work_order = models.OneToOneField("workorders.WorkOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="software_update")
    # The target (a work order's only)
    target = models.CharField(max_length=10, choices=UpdateTarget.choices, blank=True)
    target_release = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    target_reference = models.CharField(max_length=UPDATE_REFERENCE_MAX, blank=True, validators=[UPDATE_REFERENCE_VALIDATOR])
    opened_on = models.DateField(null=True, blank=True)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                  related_name="+")
    # What was done (blank while not recorded)
    outcome = models.CharField(max_length=10, choices=UpdateOutcome.choices, blank=True)
    release_before = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    release_after = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=UPDATE_REFERENCE_MAX, blank=True, validators=[UPDATE_REFERENCE_VALIDATOR])
    basis = models.CharField(max_length=24, choices=UpdateBasis.choices, blank=True)
    installed_by = models.CharField(max_length=14, choices=InstalledBy.choices, blank=True)  # a hand row's only
    applied_on = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                    related_name="+")
    later_kept = models.BooleanField(default=False, editable=False)  # the device's facts were newer than applied_on: kept, not written
    # Whether release_before was taken (review round 2): at the row's first recording (a first completion, a Confirm version of a row never
    # recorded, a hand row's own), the device's recorded release then, or none when its facts were newer than applied_on. Entered in error
    # and a re-completion keep both; a later recording reuses them and never reads the device's record again for what it ran before.
    before_taken = models.BooleanField(default=False, editable=False)
    # Where the outcome came from (review fix C15): the work order as it was when the outcome was recorded (a completion or Confirm
    # version), never as it is now: on vendor service then, and the vendor's report number then (its checks_reference). Cleared with the
    # outcome; never on a hand row.
    on_vendor_service = models.BooleanField(default=False, editable=False)
    vendor_report = models.CharField(max_length=UPDATE_REFERENCE_MAX, blank=True, editable=False, validators=[VENDOR_REPORT_VALIDATOR])
    # Entered in error (a hand row's only; a work order's outcome is cleared instead): never deleted, hidden, kept in history
    removed_on = models.DateField(null=True, blank=True, editable=False)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                   related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-applied_on", "-opened_on", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=_update_rules(), name="software_update_shape"),
            models.CheckConstraint(condition=Q(target=UpdateTarget.RELEASE.value, target_release__isnull=False)
                                   | (~Q(target=UpdateTarget.RELEASE.value) & Q(target_release__isnull=True)), name="software_update_target"),
        ]

    def __str__(self):
        return f"{self.work_order.number} software update" if self.work_order_id else f"{self.asset} software update"
