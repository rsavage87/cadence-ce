from datetime import date

from django.conf import settings
from django.db import models, transaction
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class ContractType(models.TextChoices):
    OEM = "oem", "OEM"
    THIRD_PARTY = "third_party", "Third-party"


class Coverage(models.TextChoices):
    FULL = "full", "Full service"
    PARTS_LABOR = "parts_labor", "Parts and labor"
    PARTS = "parts", "Parts only"
    PM_ONLY = "pm_only", "Preventive maintenance only"
    TM = "tm", "Time and materials"


class Contract(TenantModel):
    """A service agreement. A device is on at most one contract (Asset.contract)."""

    reference = models.CharField(max_length=60, help_text="Your contract number, e.g. SC-2026-118")
    vendor = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=ContractType.choices, default=ContractType.OEM)
    coverage = models.CharField(max_length=20, choices=Coverage.choices, default=Coverage.FULL)
    start_on = models.DateField()
    end_on = models.DateField()
    annual_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["end_on", "reference"]
        constraints = [models.UniqueConstraint(fields=["tenant", "reference"], name="uniq_contract_reference_per_tenant")]

    def __str__(self):
        return f"{self.reference} · {self.vendor}"

    @property
    def support_type(self) -> str:
        from apps.equipment.models import SupportType

        return SupportType.OEM_CONTRACT if self.type == ContractType.OEM else SupportType.THIRD_PARTY

    @property
    def is_expired(self) -> bool:
        return self.end_on < date.today()

    @property
    def days_to_end(self) -> int:
        return (self.end_on - date.today()).days

    @property
    def status(self) -> str:
        if self.is_expired:
            return "expired"
        if self.days_to_end <= settings.CONTRACT_EXPIRY_WARNING_DAYS:
            return "ending"
        return "active"

    def covered_assets(self):
        return self.assets.exclude(status="retired")

    def add_assets(self, assets):
        """Move devices onto this contract; each device can be on only one. All or nothing, so a bulk move is never half-applied."""
        n = 0
        with transaction.atomic():
            for asset in assets:
                if asset.contract_id != self.id:
                    asset.contract = self
                    asset.save(update_fields=["contract", "support_type", "updated_at"])
                    n += 1
        return n

    def remove_asset(self, asset):
        if asset.contract_id == self.id:
            asset.contract = None
            asset.save(update_fields=["contract", "support_type", "updated_at"])

    def cost_share_for(self, asset) -> float:
        """This device's slice of the annual cost, allocated by acquisition cost."""
        if self.is_expired:
            return 0.0
        total = sum(float(a.acquisition_cost) for a in self.covered_assets())
        if not total:
            return 0.0
        return float(self.annual_cost) * float(asset.acquisition_cost) / total
