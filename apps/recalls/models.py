import uuid

from django.db import models

from apps.core.models import TenantModel


class Alert(models.Model):
    """A recall or hazard notice. Global (shared across tenants); matches are per tenant."""

    class Source(models.TextChoices):
        FDA = "fda", "FDA"
        ECRI = "ecri", "ECRI"
        MANUFACTURER = "mfr", "Manufacturer letter"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=10, choices=Source.choices)
    external_id = models.CharField(max_length=80, help_text="e.g. FDA recall number Z-1234-2026 or ECRI accession number")
    classification = models.CharField(max_length=40, blank=True, help_text="Class I, II, III or ECRI priority")
    manufacturer = models.CharField(max_length=160)
    product = models.CharField(max_length=300, help_text="Product description from the notice")
    model_terms = models.JSONField(default=list, help_text="Model names or fragments used to match inventory")
    title = models.CharField(max_length=300)
    action = models.TextField(blank=True)
    url = models.URLField(blank=True)
    published_on = models.DateField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-published_on"]
        constraints = [models.UniqueConstraint(fields=["source", "external_id"], name="uniq_alert_source_id")]

    def __str__(self):
        return f"{self.get_source_display()} {self.external_id}: {self.title[:60]}"


class AlertMatch(TenantModel):
    """An alert that touches a tenant's fleet, with its disposition."""

    class Status(models.TextChoices):
        NEEDS_ACTION = "needs_action", "Needs action"
        UNDER_REVIEW = "under_review", "Under review"
        IN_PROGRESS = "in_progress", "Action in progress"
        NOT_AFFECTED = "not_affected", "Reviewed, not affected"
        CLOSED = "closed", "Closed"

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="matches")
    device_model = models.ForeignKey("equipment.DeviceModel", on_delete=models.CASCADE, related_name="alert_matches")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEEDS_ACTION)
    disposition_note = models.TextField(blank=True)
    closed_on = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "alert", "device_model"], name="uniq_alert_match")]

    def affected_assets(self):
        return self.device_model.assets.exclude(status="retired")
