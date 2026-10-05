"""
Run the daily jobs that are due (PM work-order generation, the openFDA import, the report emails, the staff notifications), each at
most once per local day: each facility's jobs once its local time (Tenant.timezone) reaches SCHEDULER_DAILY_AT, the openFDA import
once the server's (TIME_ZONE) does. Safe to call as often as you like; call it every 15 minutes or so so that every facility's
jobs run near its own hour.

    python manage.py run_daily_jobs                                 # what a cron entry or a Kubernetes CronJob should call
    python manage.py run_daily_jobs --force                         # today's jobs now, whatever the hour, and again if they ran
    python manage.py run_daily_jobs --job generate_pm
    python manage.py run_daily_jobs --tenant riverside --force      # one facility's jobs (not the openFDA import, which is everyone's)
    python manage.py run_daily_jobs --date 2026-10-05               # a missed day, as of that day
"""
from django.core.management.base import BaseCommand, CommandError

from apps.jobs import cli, services
from apps.jobs.models import JobRun
from apps.jobs.services import FACILITY_JOBS, label, run_daily_jobs


class Command(BaseCommand):
    help = "Run the daily jobs that are due and have not run for their day."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run today's jobs now, whatever the hour, even those that already ran")
        # The list as it is now (services.DAILY_JOBS), not as it was when this module was first imported.
        parser.add_argument("--job", action="append", choices=[key for key, _c, _o in services.DAILY_JOBS], help="Only this job (repeatable)")
        parser.add_argument("--tenant", help="Only this facility's jobs (its slug); the openFDA import, which is everyone's, is left out")
        parser.add_argument("--date", help="YYYY-MM-DD: run the jobs for that day now (a missed day); each facility's for that day of its own")

    def handle(self, *args, **opts):
        facilities = cli.facilities(opts["tenant"]) if opts["tenant"] else None
        day = cli.parse_day(opts["date"]) if opts["date"] else None
        if day is not None:
            cli.refuse_future(day, facilities or [])  # with every facility, the server's clock decides
            theirs = opts["job"] is None or bool(FACILITY_JOBS.intersection(opts["job"]))
            for tenant in cli.facilities() if theirs and not facilities else []:  # a facility still on the day before is left out (_plan)
                if cli.today_at(tenant) < day:
                    self.stdout.write(f"{tenant.slug}: left out, {day:%Y-%m-%d} has not come there yet")
        runs = run_daily_jobs(day=day, force=opts["force"], jobs=opts["job"], facilities=facilities)
        if not runs:
            self.stdout.write("Nothing to run: every daily job that is due has already run for its day (use --force to run again).")
        for run in runs:
            self.stdout.write(f"{label(run)}: {run.get_status_display().lower()}")
        failed = [label(r) for r in runs if r.status == JobRun.Status.FAILED]
        if failed:
            raise CommandError(f"Failed: {', '.join(failed)}. See the job run's output in Admin, Scheduled jobs.")
