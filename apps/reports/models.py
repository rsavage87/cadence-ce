"""
Report emails (slice 13): a user asks for a report by email, every Monday or on the first Monday of each month (the mock's
Schedule button). Self-service only: a subscription always sends to its own user, and only while that user can still view
Reports, so nobody's inbox receives figures they are not allowed to see. apps/reports/subscriptions.py sends them from the
daily jobs (apps.jobs: send_report_emails).
"""
from django.conf import settings
from django.db import models

from apps.core.models import TenantModel


class ReportSubscription(TenantModel):
    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Every Monday"
        MONTHLY = "monthly", "First Monday of each month"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="report_subscriptions")
    report = models.CharField(max_length=20, help_text="A key of apps.reports.services.REPORTS")
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    last_sent_on = models.DateField(null=True, blank=True, help_text="The local day it was last emailed; a day is never sent twice")
    start_on = models.DateField(null=True, blank=True, help_text="The first sending day it may send for (set when it is turned on)")

    class Meta:
        ordering = ["report"]
        constraints = [models.UniqueConstraint(fields=["tenant", "user", "report"], name="uniq_report_subscription_per_user")]

    def __str__(self):
        return f"{self.user} · {self.report} · {self.get_frequency_display()}"
