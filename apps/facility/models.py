from decimal import Decimal

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel

# The mock's maintenance policy (POLICY_DEF): field, label, default text. Shown on work orders and in the PM planner. The two PM texts
# say what Cadence measures (slice 25): a PM is on time when completed on or before its due date (apps.pm.services.pm_due_queryset),
# with no grace period, so the defaults never contradict the Overview or the survey binder.
POLICY = [
    ("policy_life_support", "Life support and high risk", "OEM interval, complete by the due date, no grace"),
    ("policy_medium_low", "Medium and low risk", "AEM allowed, complete by the due date"),
    ("policy_aem", "AEM approval", "Equipment Management Committee, 3-year failure history required"),
    ("policy_missing", "Missing devices", "Escalate after 2 search attempts; report monthly"),
    ("policy_incoming", "Incoming inspection", "Required before first clinical use, electrical safety per IEC 62353"),
    ("policy_post_repair", "Post-repair", "Safety test required before return to service"),
    ("policy_assignment", "Assignment", "Credentialed technicians first; overrides are flagged on the work order"),
    ("policy_portal", "Portal requests", "Triage within 30 minutes during shop hours"),
]
POLICY_DEFAULTS = {field: default for field, _label, default in POLICY}
POLICY_MAX_LENGTH = 300


class PmWindow(models.TextChoices):
    """When a PM counts as on time (slice 27): the facility's written policy, one for life support and high risk and one for medium
    and low. Read through apps.pm.windows (the one rule every on-time figure uses). The default is the mock's KPI: by the due date."""
    DUE_DATE = "due_date", "By the due date"
    DAYS_AFTER = "days_after", "Within a number of days after the due date"
    DUE_MONTH = "due_month", "By the end of the due month"
    NEXT_MONTH = "next_month", "By the end of the month after the due month"


PM_WINDOW_DAYS_MAX = 45  # the widest tolerance in the Joint Commission's frequency table (a three-year PM, +/- 45 days)


def _window_days_rule(kind: str, days: str) -> models.Q:
    """A group's days are set (1 to PM_WINDOW_DAYS_MAX) with "days_after" and only with it."""
    return (models.Q(**{kind: PmWindow.DAYS_AFTER, f"{days}__gte": 1, f"{days}__lte": PM_WINDOW_DAYS_MAX})
            | (~models.Q(**{kind: PmWindow.DAYS_AFTER}) & models.Q(**{f"{days}__isnull": True})))


class FacilitySettings(TenantModel):
    """One row per tenant. Read through apps.facility.services.get_settings() (which returns unsaved defaults when the
    tenant has never saved anything) and change only through the services, which validate and record who changed it."""

    # Service request portal
    portal_require_callback = models.BooleanField(default=True, help_text="Requesters must give a callback number")
    portal_hotline = models.CharField(max_length=40, blank=True, help_text="Shown to requesters on the portal, e.g. ext. 4400")
    # Slice 13: the portal can email the requester (a confirmation, and a notice when the work is done), only at the facility's
    # own email domains, so the public form cannot be used to send mail anywhere else.
    portal_confirmation = models.CharField(max_length=10, choices=[("screen", "On screen"), ("email", "On screen and by email")], default="screen")
    portal_email_domains = models.CharField(max_length=200, blank=True, help_text="Work email domains the portal may email, comma-separated")

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
    # Labor rates (slice 15): what a labor line on a work order is charged at unless the line says otherwise (the mock's $82 and $215).
    labor_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("82.00"), help_text="In-house labor, per hour")
    vendor_labor_rate = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("215.00"), help_text="Vendor service labor, per hour")
    # Taking work (slice 24): a technician may take open work nobody has, on a device they are credentialed for (My work's "You could
    # take", the new work order form's "Assign it to me"; apps.workorders.services.take). On by default; the rest a CE manager assigns.
    technicians_take_work = models.BooleanField(default=True, verbose_name="technicians may take unassigned work",
                                                help_text="Technicians may take unassigned work they are credentialed for")
    # The PM completion window (slice 27): when a PM counts as on time, for life support and high risk and for medium and low.
    pm_window_high = models.CharField("PM on time, life support and high risk", max_length=20, choices=PmWindow.choices,
                                      default=PmWindow.DUE_DATE)
    pm_window_high_days = models.PositiveSmallIntegerField("PM on time, life support and high risk: days after the due date", null=True,
                                                           blank=True)
    pm_window_other = models.CharField("PM on time, medium and low risk", max_length=20, choices=PmWindow.choices, default=PmWindow.DUE_DATE)
    pm_window_other_days = models.PositiveSmallIntegerField("PM on time, medium and low risk: days after the due date", null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "facility settings"
        verbose_name_plural = "facility settings"
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_facility_settings_per_tenant"),
                       models.CheckConstraint(condition=_window_days_rule("pm_window_high", "pm_window_high_days"), name="pm_window_high_days"),
                       models.CheckConstraint(condition=_window_days_rule("pm_window_other", "pm_window_other_days"), name="pm_window_other_days")]

    def __str__(self):
        return "Facility settings"
