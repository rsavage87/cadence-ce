from django.conf import settings
from django.db import models
from django.db.models import Q
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


class AemStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    APPROVED = "approved", "Approved"  # in force: the model's aem_interval_months is this decision's interval
    REJECTED = "rejected", "Rejected"
    WITHDRAWN = "withdrawn", "Withdrawn"
    ENDED = "ended", "Ended"  # was approved; the model is back on the OEM interval


class AemDecision(TenantModel):
    """One alternative equipment maintenance (AEM) case for a device model (slice 14): proposed with the evidence of the model's
    failure history, then approved by the Equipment Management Committee (or rejected, or withdrawn), and in force until ended.
    Only apps.pm.aem changes these rows and, through them, DeviceModel.aem_interval_months."""

    device_model = models.ForeignKey("equipment.DeviceModel", on_delete=models.CASCADE, related_name="aem_decisions")
    interval_months = models.PositiveSmallIntegerField(help_text="The proposed PM interval")
    oem_interval_months = models.PositiveSmallIntegerField(help_text="The OEM interval when proposed")
    status = models.CharField(max_length=20, choices=AemStatus.choices, default=AemStatus.PROPOSED)
    rationale = models.TextField(help_text="The case for the change, from the proposer")
    evidence = models.JSONField(default=dict, help_text="The model's failure history as the proposal saw it (apps.pm.aem.evidence)")
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    proposed_on = models.DateField()
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_on = models.DateField(null=True, blank=True, help_text="The committee's meeting date (approved or rejected)")
    decision_note = models.TextField(blank=True, help_text="The committee's minutes reference or reason")
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    ended_on = models.DateField(null=True, blank=True)
    end_reason = models.CharField(max_length=300, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-proposed_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "device_model"], condition=Q(status="proposed"), name="uniq_open_aem_proposal_per_model"),
            models.UniqueConstraint(fields=["tenant", "device_model"], condition=Q(status="approved"), name="uniq_aem_in_force_per_model"),
        ]

    def __str__(self):
        return f"AEM {self.interval_months} mo for {self.device_model} ({self.get_status_display()})"
