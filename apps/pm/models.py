from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class PmProcedure(TenantModel):
    """A PM checklist. Sourced from the OEM service manual, ECRI's generic IPM procedures, or written in-house."""

    class SourceKind(models.TextChoices):
        OEM = "oem", "OEM service manual"
        ECRI = "ecri", "ECRI generic IPM"
        IN_HOUSE = "in_house", "In-house"

    code = models.CharField(max_length=40, help_text="Short code, e.g. PH-MX750-PM12")
    name = models.CharField(max_length=200)
    source_kind = models.CharField(max_length=20, choices=SourceKind.choices, default=SourceKind.OEM)
    source_reference = models.CharField(max_length=200, blank=True, help_text="Manual title and revision, or ECRI procedure number")
    source_url = models.URLField(blank=True, help_text="Deep link, e.g. to the oneSOURCE document")
    estimated_hours = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    checklist = models.JSONField(default=list, help_text="Ordered list of steps; each step is a string or {\"text\": ..., \"measure\": ...}")
    revision = models.CharField(max_length=40, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["tenant", "code"], name="uniq_pm_procedure_code_per_tenant")]

    def __str__(self):
        return f"{self.code} · {self.name}"
