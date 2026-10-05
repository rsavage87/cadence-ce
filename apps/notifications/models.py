"""
Notifications to staff (slice 20): the emails Cadence sends people about their own work. Each user chooses which they get
(NotificationPreference, one row per user, defaults until first saved); NotificationSent remembers what went out, so a daily job
never sends the same reminder twice and a reassignment is announced once.

Emails about work never carry free text a requester typed (a work order's problem, its requester, the location they wrote): only the
work order's number, device, department, priority, and dates, and a link that asks the reader to sign in (CLAUDE.md, "No PHI").
"""
from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel


class NotificationPreference(TenantModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences")
    assignments = models.BooleanField(default=True, help_text="Email me when a work order is assigned to me")
    daily_digest = models.BooleanField(default=False, help_text="Email me each morning: my work orders due or overdue, and my PMs this week")
    contract_reminders = models.BooleanField(default=True, help_text="Email me when a service contract is about to end (Contracts Edit)")
    history = HistoricalRecords()  # every choice is audited, as report subscriptions are

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "user"], name="uniq_notification_preference_per_user")]

    def __str__(self):
        return f"{self.user} · notifications"


class NotificationSent(TenantModel):
    """One email that went out, keyed so it is never sent twice: (kind, key, user). A daily job claims the row before sending (the
    unique constraint is the lock) and removes it if the send failed, so the next run tries again."""

    class Kind(models.TextChoices):
        ASSIGNMENT = "assignment", "Work order assigned"
        DIGEST = "digest", "Daily digest"
        CONTRACT = "contract", "Contract ending"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    key = models.CharField(max_length=120, help_text="What it was about: a work order and its assignment, a day, a contract and its threshold")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        constraints = [models.UniqueConstraint(fields=["tenant", "kind", "key", "user"], name="uniq_notification_sent")]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.key} · {self.user}"
