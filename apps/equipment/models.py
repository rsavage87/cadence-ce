from datetime import date

from django.core.validators import RegexValidator
from django.db import models
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


# Tags appear in URLs (/equipment/<tag>/), so no whitespace or slashes; forms, the API, and the importer all check this.
TAG_VALIDATOR = RegexValidator(r"^[^\s/]+$", "Asset tags cannot contain spaces or slashes.")


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
    def pm_interval_months(self) -> int:
        if self.risk_class == RiskClass.LIFE_SUPPORT:  # policy: life support never goes on AEM
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
    history = HistoricalRecords()

    class Meta:
        ordering = ["tag"]
        constraints = [models.UniqueConstraint(fields=["tenant", "tag"], name="uniq_tag_per_tenant")]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "next_pm_on"])]

    def __str__(self):
        return f"{self.tag} · {self.device_model.description}"

    def save(self, *args, **kwargs):
        # Support type always follows the contract, so it cannot drift.
        self.support_type = self.contract.support_type if self.contract_id else SupportType.IN_HOUSE
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.status != AssetStatus.RETIRED

    @property
    def under_contract(self) -> bool:
        return bool(self.contract_id and self.contract.end_on >= date.today())

    @property
    def pm_interval_months(self) -> int:
        return self.device_model.pm_interval_months

    def pm_days_remaining(self, as_of=None) -> int | None:
        if not self.next_pm_on:
            return None
        return (self.next_pm_on - (as_of or date.today())).days

    def age_years(self, as_of=None) -> float | None:
        if not self.installed_on:
            return None
        return ((as_of or date.today()) - self.installed_on).days / 365.25
