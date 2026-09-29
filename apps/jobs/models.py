from django.db import models
from django.utils import timezone


class JobRun(models.Model):
    """One run of a scheduled job on one day. Not a TenantModel: each job works through every tenant itself, like the Tenant and
    Alert tables this is a system table. The unique (job, run_on) row is the lock that stops a second scheduler, or a restart,
    from running a job twice on the same day."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    job = models.CharField(max_length=40)
    run_on = models.DateField(help_text="The local day this run is for")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    output = models.TextField(blank=True, help_text="What the job printed, and the traceback when it failed (the last 20,000 characters)")

    class Meta:
        ordering = ["-started_at"]
        constraints = [models.UniqueConstraint(fields=["job", "run_on"], name="uniq_job_run_per_day")]

    def __str__(self):
        return f"{self.job} {self.run_on} {self.status}"
