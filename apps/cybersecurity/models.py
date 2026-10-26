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

- DeviceSecurityDecision (slice 39): the facility's decision for devices past the manufacturer's or the OS vendor's support (one device, a
  model's devices of ours on one release, or every device of ours of a model): keep in use with controls, keep in use without added
  controls, or take out of use; a plan; who accepted it (a role) and IT's exception number (IT's record stays the system of record);
  and the facility's own review date. Immutable once recorded: a change is a new decision that supersedes it. Slice 40 adds decisions
  for an advisory's devices (applies_to "advisory", with its own constraints, and the response "not in use here").
- SecurityAdvisory, AdvisorySource, AdvisoryModel, AdvisoryRelease (slice 40): a security advisory the facility took in, entered by hand
  (Cadence receives none by itself): its publications (sources: a token reference and an https link, never its text), the models of
  this facility's catalog it names, the releases of each it names (or every release, or releases not stated), and what it offers (a
  release from the manufacturer, the manufacturer's patches, mitigations only, no fix). Which devices it concerns is derived from each
  device's recorded release, never stored (rules.advisory_lines). Written only by apps.cybersecurity.advisories.

Every rule is apps.cybersecurity.rules; every write is apps.cybersecurity.services (and equipment.services.update_asset's model change,
through services.model_changed), slice 38's updates are apps.cybersecurity.updates, slice 39's decisions apps.cybersecurity.decisions,
and slice 40's advisories apps.cybersecurity.advisories.
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
# Slice 39: IT's exception or change number for a decision and its controls (a token; never an address, a VLAN, or a port:
# decisions.py refuses those too)
EXCEPTION_REFERENCE_VALIDATOR = reference_token("Enter IT's exception, ticket, or change number as it is written, without spaces, e.g. "
                                                "EX-1042.")
VERSION_MAX, OS_NAME_MAX, DOCUMENT_MAX, UPDATE_REFERENCE_MAX, EXCEPTION_REFERENCE_MAX = 60, 80, 40, 40, 40
# Slice 40: an advisory's references as its publisher prints them (tokens; advisories.py also refuses a network identifier)
ADVISORY_REFERENCE_VALIDATOR = reference_token("Enter the reference as its publisher prints it, without spaces, e.g. ICSMA-25-030-01.")
REVISION_VALIDATOR = reference_token("Enter the revision as its publisher prints it, without spaces, e.g. UpdateA or 3.")
ADVISORY_REFERENCE_MAX, REVISION_MAX, ADVISORY_URL_MAX = 40, 20, 300


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
# What Entered in error clears on a work order's row (updates.remove).
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
    # What it runs after (review round 4: only what the person recording it says the update did): installed and already, the release
    # they name; patches, the device's recorded release when the facts were written, else none. Never what it ran before: the device's
    # own History (DeviceSecurity's) shows every change of its release with its reason.
    release_after = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=UPDATE_REFERENCE_MAX, blank=True, validators=[UPDATE_REFERENCE_VALIDATOR])
    basis = models.CharField(max_length=24, choices=UpdateBasis.choices, blank=True)
    installed_by = models.CharField(max_length=14, choices=InstalledBy.choices, blank=True)  # a hand row's only
    applied_on = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                    related_name="+")
    later_kept = models.BooleanField(default=False, editable=False)  # the device's facts were newer than applied_on: kept, not written
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


# --- slice 39: security decisions ----------------------------------------------------------------------------------------------------

class AppliesTo(models.TextChoices):
    """Why a decision was recorded. support_ended's label holds for both of its needs (rules.support_need): support ended, and an end
    announced within 12 months (review 39 F10); its slug never changes. advisory (slice 40): the devices an advisory names (with the
    advisory and its own constraints)."""

    SUPPORT_ENDED = "support_ended", "Support ended, or its end announced"
    ADVISORY = "advisory", "A security advisory"


class DecisionResponse(models.TextChoices):
    """What the facility decided (NIST SP 800-40r4's mitigate, accept, avoid, in plain words). Never "risk accepted" as a label: only a
    decision's sentence says it, with its review date (rules.decision_words)."""

    KEEP_WITH_CONTROLS = "keep_with_controls", "Keep in use with controls"
    KEEP_WITHOUT_ADDED_CONTROLS = "keep_without_added_controls", "Keep in use without added controls"
    TAKE_OUT_OF_USE = "take_out_of_use", "Take out of use"
    # Slice 40, an advisory's devices only (never "does not apply", "not affected", "not vulnerable"): the facility's own judgment
    NOT_IN_USE_HERE = "not_in_use_here", "Decided the part it names is not in use here"


SUPPORT_RESPONSES = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, DecisionResponse.TAKE_OUT_OF_USE)


class DecisionPlan(models.TextChoices):
    """How the decider says the risk will be resolved (HHS 405(d) HICP 2023's timeline), apart from the decision itself."""

    WAIT_FOR_MANUFACTURER = "wait_for_manufacturer", "Wait for the manufacturer"
    UPDATE_PLANNED = "update_planned", "Update planned, not opened yet"
    REPLACE_PLANNED = "replace_planned", "Replacement planned"
    RETURN_TO_OWNER = "return_to_owner", "Return it to its owner"
    NONE = "none", "No plan recorded"


class AcceptedBy(models.TextChoices):
    """Who accepted the decision, as the person recording it states it (a role, never a typed name)."""

    CE_LEADER = "ce_leader", "the CE leader"
    IT_SECURITY = "it_security", "IT security or the CISO"
    COMMITTEE = "committee", "a committee"
    DEPARTMENT_LEADER = "department_leader", "department leadership"
    AUTHORIZING_OFFICIAL = "authorizing_official", "the authorizing official"


class DecisionEnd(models.TextChoices):
    SUPERSEDED = "superseded", "Superseded by a new decision"
    NO_LONGER_NEEDED = "no_longer_needed", "No longer needed"  # never why: the person ending it knows
    ENTERED_IN_ERROR = "entered_in_error", "Entered in error"


def _decision_scope():
    """Exactly one scope (database constraint, SQLite and PostgreSQL): one device; a model's devices of ours on one release; or every
    device of ours of a model."""
    device = Q(asset__isnull=False, device_model__isnull=True, release__isnull=True)
    release = Q(asset__isnull=True, device_model__isnull=False, release__isnull=False)
    model = Q(asset__isnull=True, device_model__isnull=False, release__isnull=True)
    return device | release | model


class DeviceSecurityDecision(TenantModel):
    """The facility's decision for devices past support (slice 39) or for the devices an advisory names (slice 40), written only by
    apps.cybersecurity.decisions; read through apps.cybersecurity.rules (needs, the governing decision, coverage, Check lines, words).
    Immutable once recorded: its writers only end it (superseded, no longer needed, entered in error). One live decision per scope and
    key (the key: applies_to, and the advisory for an advisory's; the conditional constraints below)."""

    number = models.CharField(max_length=20, editable=False)
    applies_to = models.CharField(max_length=20, choices=AppliesTo.choices)
    # The scope: exactly one shape (_decision_scope). Release and model scopes govern devices of ours only (rules).
    asset = models.ForeignKey("equipment.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="security_decisions")
    device_model = models.ForeignKey("equipment.DeviceModel", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="security_decisions")
    release = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="decisions")
    # The reasons that held (ended or announced) when decided, as kinds: "security_updates", "os_support" (rules.REASONS)
    reasons = models.JSONField(default=list)
    response = models.CharField(max_length=30, choices=DecisionResponse.choices)
    controls = models.JSONField(default=list)  # rules.CONTROLS slugs; non-empty exactly for keep_with_controls (decisions.py)
    controls_reference = models.CharField(max_length=EXCEPTION_REFERENCE_MAX, blank=True, validators=[EXCEPTION_REFERENCE_VALIDATOR])
    plan = models.CharField(max_length=24, choices=DecisionPlan.choices, default=DecisionPlan.NONE)
    plan_by = models.DateField(null=True, blank=True)  # never a stored fiscal year: worded at read time (apps.facility.fiscal)
    plan_owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    accepted_by = models.CharField(max_length=24, choices=AcceptedBy.choices)
    exception_reference = models.CharField(max_length=EXCEPTION_REFERENCE_MAX, blank=True, validators=[EXCEPTION_REFERENCE_VALIDATOR])
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                    related_name="+")
    decided_on = models.DateField()
    review_by = models.DateField()
    governed_ids = models.JSONField(default=list, editable=False)  # the asset ids it governed when recorded ("joined since")
    supersedes = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by",
                                      editable=False)
    ended_on = models.DateField(null=True, blank=True, editable=False)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                 related_name="+")
    end_reason = models.CharField(max_length=20, choices=DecisionEnd.choices, blank=True, editable=False)
    # Slice 40: the advisory an advisory decision is for, and the advisory's change count when recorded (its "changed since" Check
    # compares counts, never days: a same-day revision counts)
    advisory = models.ForeignKey("SecurityAdvisory", null=True, blank=True, on_delete=models.PROTECT, related_name="decisions")
    advisory_changes = models.PositiveIntegerField(null=True, blank=True, editable=False)
    history = HistoricalRecords()

    class Meta:
        ordering = ["review_by", "number"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_security_decision_number"),
            models.CheckConstraint(condition=_decision_scope(), name="security_decision_scope"),
            models.CheckConstraint(condition=Q(ended_on__isnull=True, end_reason="") | (Q(ended_on__isnull=False) & ~Q(end_reason="")),
                                   name="security_decision_end"),
            models.CheckConstraint(condition=Q(review_by__gt=models.F("decided_on")), name="security_decision_review_after"),
            # One live decision per scope for devices past support (slice 40 adds the advisory's three)
            models.UniqueConstraint(fields=["asset"], condition=Q(applies_to=AppliesTo.SUPPORT_ENDED.value, ended_on__isnull=True,
                                                                  asset__isnull=False),
                                    name="uniq_live_support_decision_device"),
            models.UniqueConstraint(fields=["release"], condition=Q(applies_to=AppliesTo.SUPPORT_ENDED.value, ended_on__isnull=True,
                                                                    release__isnull=False),
                                    name="uniq_live_support_decision_release"),
            models.UniqueConstraint(fields=["device_model"], condition=Q(applies_to=AppliesTo.SUPPORT_ENDED.value, ended_on__isnull=True,
                                                                        release__isnull=True, asset__isnull=True,
                                                                        device_model__isnull=False),
                                    name="uniq_live_support_decision_model"),
            # Slice 40: the advisory set exactly for an advisory decision; "not in use here" only for one; one live decision per scope
            # and advisory (the advisory is never null inside these conditions: never one constraint over a nullable key)
            models.CheckConstraint(condition=Q(applies_to=AppliesTo.SUPPORT_ENDED.value, advisory__isnull=True,
                                               advisory_changes__isnull=True)
                                   | Q(applies_to=AppliesTo.ADVISORY.value, advisory__isnull=False, advisory_changes__isnull=False),
                                   name="security_decision_advisory"),
            models.CheckConstraint(condition=Q(applies_to=AppliesTo.ADVISORY.value) | Q(response__in=[r.value for r in SUPPORT_RESPONSES]),
                                   name="security_decision_not_in_use"),
            models.UniqueConstraint(fields=["advisory", "asset"], condition=Q(applies_to=AppliesTo.ADVISORY.value, ended_on__isnull=True,
                                                                              asset__isnull=False),
                                    name="uniq_live_advisory_decision_device"),
            models.UniqueConstraint(fields=["advisory", "release"], condition=Q(applies_to=AppliesTo.ADVISORY.value,
                                                                                ended_on__isnull=True, release__isnull=False),
                                    name="uniq_live_advisory_decision_release"),
            models.UniqueConstraint(fields=["advisory", "device_model"], condition=Q(applies_to=AppliesTo.ADVISORY.value,
                                                                                     ended_on__isnull=True, release__isnull=True,
                                                                                     asset__isnull=True, device_model__isnull=False),
                                    name="uniq_live_advisory_decision_model"),
        ]

    def __str__(self):
        return self.number


# --- slice 40: security advisories ---------------------------------------------------------------------------------------------------

class AdvisoryStatus(models.TextChoices):
    OPEN = "open", "Open"
    NOT_APPLICABLE = "not_applicable", "Not applicable here"
    CLOSED = "closed", "Closed"


class NotApplicableReason(models.TextChoices):
    """Each reason is checked against Cadence's records when marked (advisories.not_applicable_refusals), so marking it removes nothing
    from any attention door at that moment; a device named since is listed again."""

    NO_MODEL_HERE = "no_model_here", "It names no model in this facility's catalog"
    NONE_HERE = "none_here", "No device of the models it names is recorded on site here"
    RELEASES_NOT_HERE = "releases_not_here", "No device here is recorded on a release it names"


class AdvisorySourceKind(models.TextChoices):
    CISA_ICSMA = "cisa_icsma", "CISA ICS medical advisory"
    MANUFACTURER_BULLETIN = "manufacturer_bulletin", "Manufacturer bulletin"
    MANUFACTURER_PORTAL = "manufacturer_portal", "Manufacturer's customer portal notice"
    FDA_COMMUNICATION = "fda_communication", "FDA safety communication"
    ISAC = "isac", "ISAC alert"
    OTHER = "other", "Other"


class AdvisoryChange(models.TextChoices):
    """What a revision changed (a source's last_change, a set). The first three reopen the models' confirmation (advisories.REOPENING)."""

    PRODUCTS_CHANGED = "products_changed", "Products or releases changed"
    VULNERABILITIES_ADDED = "vulnerabilities_added", "Vulnerabilities added"
    FIX_AVAILABLE = "fix_available", "The advisory now names a fix"
    MITIGATIONS_CHANGED = "mitigations_changed", "Mitigations changed"
    OTHER_DETAILS = "other_details", "Other details"


class AdvisoryVersions(models.TextChoices):
    LISTED = "listed", "The releases listed"
    ALL = "all", "Every release"
    NOT_STATED = "not_stated", "Releases not stated"


class AdvisoryRemedy(models.TextChoices):
    """What the advisory offers for a model, as the advisory states it (never Cadence's verdict)."""

    RELEASE = "release", "A release from the manufacturer"
    PATCHES = "patches", "The manufacturer's validated patches"
    MITIGATIONS = "mitigations", "Mitigations only, as the advisory states"
    NO_FIX_YET = "no_fix_yet", "No fix yet, as the advisory states"
    NO_FIX_PLANNED = "no_fix_planned", "No fix planned, as the advisory states"


class SecurityAdvisory(TenantModel):
    """A security advisory the facility took in (the issue, which may have several publications). No title or text is kept: its name is
    its number, its first source's reference, and the models it names (rules.advisory_name). Needs are derived from each device's
    recorded release (rules.advisory_lines) for an advisory open or closed and not entered in error."""

    number = models.CharField(max_length=20, editable=False)  # AD-<yy>-<nnnn>, yy of received_on
    cves = models.JSONField(default=list)  # upper-cased CVE IDs, unique within the advisory, in the order given (matched in Python)
    received_on = models.DateField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                    related_name="+")
    status = models.CharField(max_length=20, choices=AdvisoryStatus.choices, default=AdvisoryStatus.OPEN)
    not_applicable_reason = models.CharField(max_length=20, choices=NotApplicableReason.choices, blank=True)
    status_on = models.DateField(null=True, blank=True)  # the day it last left open (marked not applicable, or closed)
    status_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    models_confirmed_on = models.DateField(null=True, blank=True)  # null: "Models to confirm"
    models_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    changed_on = models.DateField(null=True, blank=True)  # the day of the last change that matters (its words)
    changes = models.PositiveIntegerField(default=0, editable=False)  # counted with changed_on: the decisions' "changed since" Check
    removed_on = models.DateField(null=True, blank=True)  # entered in error: marked, never deleted
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-received_on", "-number"]
        verbose_name_plural = "security advisories"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_security_advisory_number"),
            # The reason set exactly when not applicable; the day set exactly when not open (never a constraint on a SET_NULL person)
            models.CheckConstraint(condition=(Q(status=AdvisoryStatus.NOT_APPLICABLE.value) & ~Q(not_applicable_reason=""))
                                   | Q(status__in=[AdvisoryStatus.OPEN.value, AdvisoryStatus.CLOSED.value], not_applicable_reason=""),
                                   name="security_advisory_reason"),
            models.CheckConstraint(condition=Q(status=AdvisoryStatus.OPEN.value, status_on__isnull=True)
                                   | Q(status__in=[AdvisoryStatus.NOT_APPLICABLE.value, AdvisoryStatus.CLOSED.value],
                                       status_on__isnull=False),
                                   name="security_advisory_status_on"),
        ]

    def __str__(self):
        return self.number


class AdvisorySource(TenantModel):
    """One publication of an advisory: who published it, its reference (a token) and link (https, a pointer never fetched), its days,
    and its revision. The same source kind and reference is one publication: unique among live sources."""

    advisory = models.ForeignKey(SecurityAdvisory, on_delete=models.PROTECT, related_name="sources")
    source = models.CharField(max_length=24, choices=AdvisorySourceKind.choices)
    reference = models.CharField(max_length=ADVISORY_REFERENCE_MAX, blank=True, validators=[ADVISORY_REFERENCE_VALIDATOR])
    reference_key = models.CharField(max_length=ADVISORY_REFERENCE_MAX, blank=True, editable=False)  # casefolded
    url = models.CharField(max_length=ADVISORY_URL_MAX, blank=True)  # https only, checked by services._url
    published_on = models.DateField(null=True, blank=True)
    updated_on = models.DateField(null=True, blank=True)
    revision = models.CharField(max_length=REVISION_MAX, blank=True, validators=[REVISION_VALIDATOR])
    last_change = models.JSONField(default=list)  # AdvisoryChange slugs: what the last revision recorded changed
    removed_on = models.DateField(null=True, blank=True)  # this source removed (Remove source)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Set only when its advisory is entered in error (one update, no history row): frees the reference without touching removed_on
    advisory_removed = models.BooleanField(default=False, editable=False)
    history = HistoricalRecords()

    class Meta:
        ordering = ["advisory", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "source", "reference_key"],
                                    condition=Q(removed_on__isnull=True, advisory_removed=False, reference_key__gt=""),
                                    name="uniq_live_advisory_source_reference"),
        ]

    def __str__(self):
        return f"{self.advisory} · {self.get_source_display()}{f' {self.reference}' if self.reference else ''}"


class AdvisoryModel(TenantModel):
    """A model of this facility's catalog an advisory names: which of its releases (listed, every release, or not stated) and what the
    advisory offers for it. A release fix is a release of the model, never one it lists; "every release" or "not stated" never with a
    release fix (a device on a newer release would read as named: critique39_scope B3)."""

    advisory = models.ForeignKey(SecurityAdvisory, on_delete=models.PROTECT, related_name="model_rows")
    device_model = models.ForeignKey("equipment.DeviceModel", on_delete=models.PROTECT, related_name="advisory_rows")
    versions = models.CharField(max_length=12, choices=AdvisoryVersions.choices)
    remedy = models.CharField(max_length=16, choices=AdvisoryRemedy.choices)
    fix_release = models.ForeignKey(SoftwareRelease, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    patches_reference = models.CharField(max_length=UPDATE_REFERENCE_MAX, blank=True, validators=[UPDATE_REFERENCE_VALIDATOR])
    # The model's versioned, not removed releases when set_model last saved (the "Release added since" Check, by identity)
    seen_release_ids = models.JSONField(default=list, editable=False)
    history = HistoricalRecords()

    class Meta:
        ordering = ["advisory", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["advisory", "device_model"], name="uniq_advisory_model"),
            models.CheckConstraint(condition=Q(remedy=AdvisoryRemedy.RELEASE.value, versions=AdvisoryVersions.LISTED.value,
                                               fix_release__isnull=False)
                                   | (~Q(remedy=AdvisoryRemedy.RELEASE.value) & Q(fix_release__isnull=True)),
                                   name="advisory_model_fix"),
            models.CheckConstraint(condition=Q(remedy=AdvisoryRemedy.PATCHES.value) | Q(patches_reference=""),
                                   name="advisory_model_patches"),
        ]

    def __str__(self):
        return f"{self.advisory} · {self.device_model}"


class AdvisoryRelease(TenantModel):
    """A release a model row lists (versions listed only; set by advisories.set_model, which also sets `advisory` to the row's)."""

    advisory = models.ForeignKey(SecurityAdvisory, on_delete=models.PROTECT, related_name="release_rows")
    advisory_model = models.ForeignKey(AdvisoryModel, on_delete=models.CASCADE, related_name="releases")
    release = models.ForeignKey(SoftwareRelease, on_delete=models.PROTECT, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["advisory_model", "created_at"]
        constraints = [models.UniqueConstraint(fields=["advisory_model", "release"], name="uniq_advisory_release")]

    def __str__(self):
        return f"{self.advisory} · {self.release}"

