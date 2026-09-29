"""
Run the daily jobs once (PM work-order generation, then the openFDA import), each at most once per local day.

    python manage.py run_daily_jobs                 # what a cron entry or a Kubernetes CronJob should call
    python manage.py run_daily_jobs --force         # run again today anyway
    python manage.py run_daily_jobs --job generate_pm
"""
from django.core.management.base import BaseCommand, CommandError

from apps.jobs.models import JobRun
from apps.jobs.services import DAILY_JOBS, run_daily_jobs


class Command(BaseCommand):
    help = "Run the daily jobs that have not run today."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even if the job already ran today")
        parser.add_argument("--job", action="append", choices=[key for key, _c, _o in DAILY_JOBS], help="Only this job (repeatable)")

    def handle(self, *args, **opts):
        runs = run_daily_jobs(force=opts["force"], jobs=opts["job"])
        if not runs:
            self.stdout.write("Nothing to run: every daily job has already run today (use --force to run again).")
        for run in runs:
            self.stdout.write(f"{run.job}: {run.get_status_display().lower()}")
        failed = [r.job for r in runs if r.status == JobRun.Status.FAILED]
        if failed:
            raise CommandError(f"Failed: {', '.join(failed)}. See the job run's output in Admin, Scheduled jobs.")
