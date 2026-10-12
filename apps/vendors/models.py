"""
Contracted service and vendor staff (slice 31, beyond the mock): the evidence the facility keeps that the vendors' staff who service its
equipment were trained for it. CMS asks a hospital to show how it assures contractors use qualified personnel (A-0724); it never asks
for each engineer's certificate, so the evidence is either of two documents, each kept as a record with its own dates:

- an engineer's training on file (EngineerTraining: a certificate or the vendor's training record for named device models, or one
  manufacturer's product category), for the engineer a work order names (WorkOrder.vendor_engineer, as the vendor's report names them);
- the vendor's written attestation (VendorAttestation): everyone who services these devices for the facility, on site, at its depot, or
  remotely, is trained on them, and their training records are available on request.

Cadence records documents and never judges competence: its words are "training on file" and "attestation on file", never "trained" or
"qualified". A record covers the days its own dates say (issued to expiry, valid from to valid through), whenever it was entered; a
renewal is a new record, and a record entered in error is removed (its history keeps it). The rule is apps.vendors.rules; every write is
apps.vendors.services.

Vendors stay names (WorkOrder.vendor_name, Contract.vendor, User.company): an engineer's company and an attestation's vendor names are
matched to a work order's vendor by rules.vendor_key, stored beside them (company_key, vendor_keys) and written only by the services.

Privacy: an engineer is a name and a company. No email, phone, files, notes, health, or background data (badges, immunizations, and
background checks belong to the facility's vendor credentialing system); never in an email, a digest, or a custom report.
"""
from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel
from apps.core.validators import reference_token

CERTIFICATE_VALIDATOR = reference_token("Enter the certificate's number as it is printed, without spaces.")
ATTESTATION_REFERENCE_VALIDATOR = reference_token("Enter the letter's reference, or the contract and clause, without spaces "
                                                  "(SC-2026-118/7.2).")


class TrainingSource(models.TextChoices):
    OEM = "oem", "Manufacturer's (OEM) factory training"
    VENDOR = "vendor", "The vendor's own training program"
    THIRD_PARTY = "third_party", "Third-party course"


class Covers(models.TextChoices):
    """What a training or an attestation covers. Never a whole manufacturer or a whole category: a certificate on a manufacturer's
    monitors must never cover its CT."""
    ALL = "all", "Every device this vendor services for us"  # an attestation only
    MODELS = "models", "Device models"
    LINE = "line", "A manufacturer's product category"


class Coverage(TenantModel):
    """The devices a record names (rules.names_model): a list of catalog device models (their ids: a renamed model stays covered), one
    manufacturer's product category (exactly as the catalog writes them, as credentials match), or, for an attestation, every device the
    vendor services for the facility."""
    covers = models.CharField(max_length=10, choices=Covers.choices)
    model_ids = models.JSONField(default=list, blank=True, help_text="Device model ids (Covers.MODELS)")
    manufacturer = models.CharField(max_length=120, blank=True, help_text="Covers.LINE")
    category = models.CharField(max_length=120, blank=True, help_text="Covers.LINE")

    class Meta:
        abstract = True


class VendorEngineer(TenantModel):
    """A vendor's field service engineer (or depot or remote technician) as the vendor's reports name them. Never deleted (work orders
    name them): deactivated. Their company is fixed once a work order names them (a move to another vendor is a new engineer)."""
    name = models.CharField(max_length=120)
    company = models.CharField(max_length=120, help_text="The vendor, as the facility's work orders and contracts name it")
    company_key = models.CharField(max_length=120, editable=False, help_text="rules.vendor_key(company): services only")
    name_key = models.CharField(max_length=120, editable=False, help_text="credentials.services.name_key(name): services only")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["company", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "company_key", "name_key"], name="uniq_vendor_engineer_per_company")]
        indexes = [models.Index(fields=["tenant", "company_key"])]

    def __str__(self):
        return f"{self.name} ({self.company})"


class EngineerTraining(Coverage):
    """One certificate or training record of one engineer: the models or product category it names, from issued_on to expires_on
    (none: no expiry). A renewal is a new record; one entered in error is removed (services.remove_training)."""
    engineer = models.ForeignKey(VendorEngineer, on_delete=models.PROTECT, related_name="trainings")
    source = models.CharField(max_length=20, choices=TrainingSource.choices)
    certificate = models.CharField("certificate number", max_length=40, blank=True, validators=[CERTIFICATE_VALIDATOR])
    issued_on = models.DateField()
    expires_on = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-issued_on", "created_at"]
        indexes = [models.Index(fields=["tenant", "engineer"])]

    def __str__(self):
        return f"{self.engineer.name} · training"


class VendorAttestation(Coverage):
    """A vendor's written statement that everyone who services these devices for the facility, on site, at its depot, or remotely, is
    trained on them, and that their training records are available on request: a yearly letter, or a contract's clause. It names the
    vendor as the facility's work orders and contracts name it, under one or more names ("Philips Healthcare", the contract's vendor,
    and "Philips field service", its work without a contract). In force from valid_from to valid_through (both required)."""
    vendor_names = models.JSONField(default=list, help_text="The vendor's names as work orders and contracts write them")
    vendor_keys = models.JSONField(default=list, editable=False, help_text="rules.vendor_key of each name: services only")
    reference = models.CharField(max_length=40, validators=[ATTESTATION_REFERENCE_VALIDATOR],
                                 help_text="The letter's reference, or the contract and clause")
    received_on = models.DateField()
    valid_from = models.DateField()
    valid_through = models.DateField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-valid_through", "reference"]

    def __str__(self):
        return f"{', '.join(self.vendor_names)} · attestation {self.reference}"
