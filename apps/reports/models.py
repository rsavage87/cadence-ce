"""
Report emails (slice 13): a user asks for a report by email, every Monday or on the first Monday of each month (the mock's
Schedule button). Self-service only: a subscription always sends to its own user, and only while that user can still view
Reports, so nobody's inbox receives figures they are not allowed to see. apps/reports/subscriptions.py sends them from the
daily jobs (apps.jobs: send_report_emails).

Custom reports (slice 18): the reports a facility builds for itself (the mock's Custom report button). apps/reports/custom.py
declares what each source offers and runs a definition.
"""
from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class ReportSubscription(TenantModel):
    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Every Monday"
        MONTHLY = "monthly", "First Monday of each month"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="report_subscriptions")
    report = models.CharField(max_length=50, help_text="A key of apps.reports.services.REPORTS, or a custom report's (custom-<id>)")
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    last_sent_on = models.DateField(null=True, blank=True, help_text="The local day it was last emailed; a day is never sent twice")
    start_on = models.DateField(null=True, blank=True, help_text="The first sending day it may send for (set when it is turned on)")

    class Meta:
        ordering = ["report"]
        constraints = [models.UniqueConstraint(fields=["tenant", "user", "report"], name="uniq_report_subscription_per_user")]

    def __str__(self):
        return f"{self.user} · {self.report} · {self.get_frequency_display()}"


class CustomReport(TenantModel):
    """A report a facility builds for itself: one source (work orders, devices, labor lines, part lines), the columns to show,
    filters, an optional grouping, and a sort. apps/reports/custom.py declares what each source offers and runs a definition; only
    what it declares can be asked for, so a saved definition never reaches other fields or tables, and it offers no free text a
    requester typed (a report can be emailed). Its key on the Reports screen, in the CSV and print URLs, and in report emails is
    "custom-<id>" (`key`). Everyone with Reports View in the facility runs it; building, changing, and deleting need Reports Edit."""

    class Source(models.TextChoices):
        WORK_ORDERS = "work_orders", "Work orders"
        DEVICES = "devices", "Devices"
        LABOR = "labor", "Labor (time logged)"
        PARTS = "parts", "Parts used"

    KEY_PREFIX = "custom-"

    name = models.CharField(max_length=80)
    source = models.CharField(max_length=20, choices=Source.choices)
    columns = models.JSONField(default=list, help_text="Column keys of the source, in the order shown")
    filters = models.JSONField(default=dict, help_text="Filter key to value, as apps.reports.custom declares them for the source")
    group_by = models.CharField(max_length=40, blank=True, help_text="A column key the source can group by; blank lists the rows")
    sort = models.CharField(max_length=41, blank=True, help_text="A column key, with a leading '-' for descending; blank is the source's order")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="uniq_custom_report_name_per_tenant")]

    def __str__(self):
        return self.name

    @property
    def key(self) -> str:
        return f"{self.KEY_PREFIX}{self.pk}"
