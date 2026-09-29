from decimal import Decimal

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel

# The mock's maintenance policy (POLICY_DEF): field, label, default text. Shown on work orders and in the PM planner.
POLICY = [
    ("policy_life_support", "Life support and high risk", "OEM interval, complete within due month, no grace"),
    ("policy_medium_low", "Medium and low risk", "AEM allowed, 30-day grace after due month"),
    ("policy_aem", "AEM approval", "Equipment Management Committee, 3-year failure history required"),
    ("policy_missing", "Missing devices", "Escalate after 2 search attempts; report monthly"),
    ("policy_incoming", "Incoming inspection", "Required before first clinical use, electrical safety per IEC 62353"),
    ("policy_post_repair", "Post-repair", "Safety test required before return to service"),
    ("policy_assignment", "Assignment", "Credentialed technicians first; overrides are flagged on the work order"),
    ("policy_portal", "Portal requests", "Triage within 30 minutes during shop hours"),
]
POLICY_DEFAULTS = {field: default for field, _label, default in POLICY}
POLICY_MAX_LENGTH = 300


class FacilitySettings(TenantModel):
    """One row per tenant. Read through apps.facility.services.get_settings() (which returns unsaved defaults when the
    tenant has never saved anything) and change only through the services, which validate and record who changed it."""

    # Service request portal
    portal_require_callback = models.BooleanField(default=True, help_text="Requesters must give a callback number")
    portal_hotline = models.CharField(max_length=40, blank=True, help_text="Shown to requesters on the portal, e.g. ext. 4400")

    # Maintenance policy text
    policy_life_support = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_life_support"])
    policy_medium_low = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_medium_low"])
    policy_aem = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_aem"])
    policy_missing = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_missing"])
    policy_incoming = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_incoming"])
    policy_post_repair = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_post_repair"])
    policy_assignment = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_assignment"])
    policy_portal = models.CharField(max_length=POLICY_MAX_LENGTH, default=POLICY_DEFAULTS["policy_portal"])

    # KPI targets (the Overview tiles, the PM trend line, and the compliance report's medium and low risk target)
    target_pm_pct = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("95.0"),
                                        help_text="PM completion target for medium and low risk equipment; life support and high risk are held at 100%")
    target_uptime_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("99.50"))
    target_mttr_days = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("3.0"))
    repair_budget_monthly = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                                help_text="Monthly repair budget shown on the Overview's spend tile; blank for none")
    history = HistoricalRecords()

    class Meta:
        verbose_name = "facility settings"
        verbose_name_plural = "facility settings"
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_facility_settings_per_tenant")]

    def __str__(self):
        return "Facility settings"
