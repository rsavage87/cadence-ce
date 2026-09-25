from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class Technician(TenantModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="technician")
    name = models.CharField(max_length=120)
    title = models.CharField(max_length=80, blank=True, help_text="e.g. BMET II, Imaging specialist")
    certification = models.CharField(max_length=80, blank=True, help_text="e.g. CBET, CRES, CLES")
    weekly_capacity_hours = models.DecimalField(max_digits=5, decimal_places=1, default=32)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Scope(models.TextChoices):
    CATEGORY = "category", "Product category"
    MANUFACTURER = "manufacturer", "Manufacturer"
    MODEL = "model", "Device model"


class Credential(TenantModel):
    """What a technician may work on. Matched against a device's category, manufacturer, or model."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        IN_TRAINING = "in_training", "In training"

    technician = models.ForeignKey(Technician, on_delete=models.CASCADE, related_name="credentials")
    scope = models.CharField(max_length=20, choices=Scope.choices)
    value = models.CharField(max_length=120, help_text="Category name, manufacturer name, or model name to match")
    source = models.CharField(max_length=120, blank=True, help_text="e.g. OEM training, In-house sign-off, Certification")
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    history = HistoricalRecords()

    class Meta:
        ordering = ["scope", "value"]

    def __str__(self):
        return f"{self.technician}: {self.get_scope_display()} {self.value}"
