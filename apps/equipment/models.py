from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class RiskClass(models.TextChoices):
    LIFE_SUPPORT = "life_support", "Life support"
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


class AssetStatus(models.TextChoices):
    IN_SERVICE = "in_service", "In service"
    IN_REPAIR = "in_repair", "In repair"
    OUT_OF_SERVICE = "out_of_service", "Out of service"
    ON_LOAN = "on_loan", "On loan"
    MISSING = "missing", "Missing"
    RETIRED = "retired", "Retired"


class SupportType(models.TextChoices):
    IN_HOUSE = "in_house", "In-house"
    OEM_CONTRACT = "oem_contract", "OEM contract"
    THIRD_PARTY = "third_party", "Third-party"
    OWNER = "owner", "Owner maintains"  # slice 29: a rental, vendor loaner, or demo unit (Asset.ownership not owned)


class Ownership(models.TextChoices):
    """Whose a device is (slice 29). Ours is everything on CE's PM program (owned, leased, or placed); the others are on site for a
    while, maintained by their owner (no next PM, no PM work orders; Cadence records the owner's PM date from its sticker), and leave
    by Return to owner (status retired, read "Returned to owner"). One device record per arrival: a unit that comes back later is
    entered again. Set by create_asset; changes only by equipment.services.keep_temporary_device (bought: ours)."""
    OWNED = "owned", "Ours"
    RENTAL = "rental", "Rental"
    LOANER = "loaner", "Vendor loaner"
    DEMO = "demo", "Demo or evaluation unit"


TEMPORARY = (Ownership.RENTAL, Ownership.LOANER, Ownership.DEMO)


class ReturnCleaning(models.TextChoices):
    """How a temporary device left (slice 29): OSHA 29 CFR 1910.1030(d)(2)(xiv), equipment that may be contaminated is decontaminated
    before shipping, or its contaminated parts labeled."""
    DECONTAMINATED = "decontaminated", "Cleaned and decontaminated"
    LABELED = "labeled", "Parts still contaminated are labeled"


class ReturnData(models.TextChoices):
    """Patient data on a temporary device when it left (slice 29): HIPAA 45 CFR 164.310(d)(2)(ii)."""
    CLEARED = "cleared", "Patient data cleared"
    NONE_STORED = "none_stored", "Stores no patient data"
    NOT_APPLICABLE = "not_applicable", "Not applicable"


class AddedAs(models.TextChoices):
    """How a device came to be in Cadence (slice 25): the survey binder asks new devices for an incoming inspection before first use,
    and only them. Blank: added before slice 25, or written without the services (the demo seed), so nobody knows; the binder counts
    those and never calls them gaps."""
    NEW = "new", "New to the facility"
    EXISTING = "existing", "Already in use here"
    IMPORTED = "imported", "Imported from the previous system"


class UseBeforeInspection(models.TextChoices):
    """Why a new device went into use before its incoming inspection (slice 26): a choice, never free text. Recorded by
    equipment.services.use_before_inspection (Equipment Approve), in the device's history; the survey binder lists the device."""
    EMERGENCY = "emergency", "Emergency clinical need"
    LOANER_RENTAL = "loaner_rental", "Loaner or rental needed now"
    ARRIVED_IN_USE = "arrived_in_use", "Arrived on the unit already in use"


# Tags appear in URLs (/equipment/<tag>/), so no whitespace or slashes; forms, the API, and the importer all check this.
TAG_VALIDATOR = RegexValidator(r"^[^\s/]+$", "Asset tags cannot contain spaces or slashes.")
# Slice 29: a temporary device's agreement, PO, or RMA number. A token (no spaces): specialty beds are rented for one patient, and a
# free-text box there invites a name or a room (CLAUDE.md non-negotiable 6).
OWNER_REFERENCE_VALIDATOR = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9._/#-]*$",
                                           "Enter the agreement, PO, or RMA number as the owner's paperwork shows it, without spaces.")


class Department(TenantModel):
    name = models.CharField(max_length=100)
    cost_center = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="uniq_department_per_tenant")]

    def __str__(self):
        return self.name


class DeviceModel(TenantModel):
    """Catalog entry: one row per manufacturer + model. Assets are instances of these."""

    manufacturer = models.CharField(max_length=120)
    model = models.CharField(max_length=120)
    description = models.CharField(max_length=200, help_text="Plain-language device name, e.g. 'Infusion pump'")
    category = models.CharField(max_length=80, help_text="Product category, e.g. 'Infusion pumps'")
    risk_class = models.CharField(max_length=20, choices=RiskClass.choices, default=RiskClass.MEDIUM)
    oem_pm_interval_months = models.PositiveSmallIntegerField(default=12)
    aem_interval_months = models.PositiveSmallIntegerField(null=True, blank=True,
                                                           help_text="Approved alternative equipment maintenance interval; blank = follow OEM")
    expected_life_years = models.PositiveSmallIntegerField(default=8)
    list_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pm_procedure = models.ForeignKey("pm.PmProcedure", on_delete=models.SET_NULL, null=True, blank=True, related_name="device_models")
    # Risk scoring (slice 14), the Settings rubric: clinical function (1 to 10) + physical risk of failure (1 to 5) + maintenance
    # requirement (1 to 5) + incident history (0 to 2). All four or none; a scored model's risk class follows its score's band
    # (apps.facility.services.RISK_BANDS). Reviewed yearly: risk_reviewed_on is when the score was last set or confirmed.
    risk_function = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Clinical function, 1 to 10")
    risk_physical = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Physical risk of failure, 1 to 5")
    risk_maintenance = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Maintenance requirement, 1 to 5")
    risk_incidents = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Incident history, 0 to 2")
    risk_reviewed_on = models.DateField(null=True, blank=True)
    # CMS (S&C 14-07): imaging and radiologic equipment and medical lasers are maintained on the manufacturer's schedule, never on an
    # AEM interval (slice 18; apps.pm.aem refuses them, as it refuses life support). The facility marks which models these are.
    oem_schedule_required = models.BooleanField(default=False, help_text="Imaging, radiologic, or medical laser equipment: CMS requires the "
                                                "manufacturer's maintenance schedule, so it never goes on AEM")
    history = HistoricalRecords()

    class Meta:
        ordering = ["manufacturer", "model"]
        constraints = [models.UniqueConstraint(fields=["tenant", "manufacturer", "model"], name="uniq_model_per_tenant")]

    def __str__(self):
        return f"{self.manufacturer} {self.model}"

    @property
    def risk_score(self) -> int | None:
        """The rubric's total, or None while the model is unscored (any part missing)."""
        parts = (self.risk_function, self.risk_physical, self.risk_maintenance, self.risk_incidents)
        return None if any(p is None for p in parts) else sum(parts)

    @property
    def aem_excluded(self) -> bool:
        """Never on AEM: life support (the facility's policy) and the equipment CMS keeps on the manufacturer's schedule (imaging,
        radiologic, medical laser). apps.pm.aem says which rule applies (exclusion)."""
        return self.risk_class == RiskClass.LIFE_SUPPORT or self.oem_schedule_required

    @property
    def pm_interval_months(self) -> int:
        """The interval in force: the OEM interval for a model excluded from AEM, whatever AEM interval is on file; else the AEM
        interval when there is one."""
        if self.aem_excluded:
            return self.oem_pm_interval_months
        return self.aem_interval_months or self.oem_pm_interval_months


class Asset(TenantModel):
    ACTIVE_STATUSES = (AssetStatus.IN_SERVICE, AssetStatus.IN_REPAIR, AssetStatus.OUT_OF_SERVICE, AssetStatus.ON_LOAN, AssetStatus.MISSING)

    tag = models.CharField(max_length=40, help_text="Control number on the CE sticker, e.g. CE-10241", validators=[TAG_VALIDATOR])
    serial = models.CharField(max_length=80, blank=True)
    device_model = models.ForeignKey(DeviceModel, on_delete=models.PROTECT, related_name="assets")
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="assets")
    room = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=AssetStatus.choices, default=AssetStatus.IN_SERVICE)
    installed_on = models.DateField(null=True, blank=True)
    acquisition_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    condition = models.PositiveSmallIntegerField(default=3, help_text="1 (poor) to 5 (excellent)")
    warranty_end = models.DateField(null=True, blank=True)
    support_type = models.CharField(max_length=20, choices=SupportType.choices, default=SupportType.IN_HOUSE, editable=False)
    contract = models.ForeignKey("contracts.Contract", on_delete=models.SET_NULL, null=True, blank=True, related_name="assets")
    last_pm_on = models.DateField(null=True, blank=True)
    next_pm_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    added_as = models.CharField(max_length=20, choices=AddedAs.choices, blank=True, editable=False,
                                help_text="How the device came to be in Cadence (create_asset): new, already in use, or imported")
    # Slice 26: new and not yet through its incoming inspection. Set by create_asset(incoming_inspection="waiting"), cleared only by
    # equipment.services.pass_incoming_inspection. While it is set the device has no next PM (its PM clock starts at the pass) and
    # nothing puts it in service but a passed inspection or use_before_inspection (Approve, with a reason).
    awaiting_inspection = models.BooleanField(default=False, editable=False,
                                              help_text="New, and waiting for its incoming inspection before first use")
    # Slice 28: held as evidence by an open device incident (apps.incidents). Set and cleared only by equipment.services
    # set_incident_hold / clear_incident_hold, on the locked row. While it is set nobody uses, repairs, or tests the device: set_status
    # refuses every move, and no work order but the incident's investigation starts or completes (workorders.services.change_status).
    incident_hold = models.BooleanField(default=False, editable=False, help_text="Held as evidence for an incident investigation")
    # Slice 29: whose it is, and a temporary device's stay (one per device record). Written only by create_asset and the temporary
    # services in equipment.services (add_temporary_device, update_temporary, return_to_owner, keep_temporary_device).
    ownership = models.CharField(max_length=10, choices=Ownership.choices, default=Ownership.OWNED, editable=False)
    owner = models.CharField(max_length=120, blank=True, editable=False, help_text="The company that owns a temporary device")
    owner_reference = models.CharField(max_length=40, blank=True, editable=False, validators=[OWNER_REFERENCE_VALIDATOR],
                                       help_text="The rental agreement, PO, or RMA number (never a patient's name or record number)")
    arrived_on = models.DateField(null=True, blank=True, editable=False)
    due_back_on = models.DateField(null=True, blank=True, editable=False)
    owner_pm_due_on = models.DateField(null=True, blank=True, editable=False, help_text="The owner's PM date, from its sticker")
    returned_on = models.DateField(null=True, blank=True, editable=False)
    kept_on = models.DateField(null=True, blank=True, editable=False, help_text="The facility kept (bought) it: ours from that day")
    stands_in_for = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="loaners",
                                      help_text="The device of ours a vendor loaner stands in for")
    return_cleaning = models.CharField(max_length=20, choices=ReturnCleaning.choices, blank=True, editable=False)
    return_data = models.CharField(max_length=20, choices=ReturnData.choices, blank=True, editable=False)
    history = HistoricalRecords()

    class Meta:
        ordering = ["tag"]
        constraints = [models.UniqueConstraint(fields=["tenant", "tag"], name="uniq_tag_per_tenant")]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "next_pm_on"])]

    def __str__(self):
        return f"{self.tag} · {self.device_model.description}"

    def save(self, *args, **kwargs):
        # Support type always follows the contract, so it cannot drift; a temporary device's owner maintains it (slice 29).
        if self.ownership != Ownership.OWNED:
            self.support_type = SupportType.OWNER
        else:
            self.support_type = self.contract.support_type if self.contract_id else SupportType.IN_HOUSE
        super().save(*args, **kwargs)

    @property
    def temporary(self) -> bool:
        """A rental, vendor loaner, or demo unit: its owner maintains it (slice 29)."""
        return self.ownership != Ownership.OWNED

    @property
    def is_active(self) -> bool:
        return self.status != AssetStatus.RETIRED

    # Today is the facility's (its time zone is active inside it: a request, tenant_context), unless `as_of` is given.
    @property
    def under_contract(self) -> bool:
        return bool(self.contract_id and self.contract.end_on >= timezone.localdate())

    @property
    def pm_interval_months(self) -> int:
        return self.device_model.pm_interval_months

    def pm_days_remaining(self, as_of=None) -> int | None:
        if not self.next_pm_on:
            return None
        return (self.next_pm_on - (as_of or timezone.localdate())).days

    def age_years(self, as_of=None) -> float | None:
        if not self.installed_on:
            return None
        return ((as_of or timezone.localdate()) - self.installed_on).days / 365.25
