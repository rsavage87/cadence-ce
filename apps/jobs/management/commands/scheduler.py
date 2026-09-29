"""
Run the daily jobs every day at SCHEDULER_DAILY_AT (local time in TIME_ZONE). Meant for one long-running process, the
docker-compose `scheduler` service. If it starts after that time and today's jobs have not run (a restart, a deploy, an
outage), it runs them at once. A second copy is harmless: each job's daily run is claimed in the database first.

    python manage.py scheduler              # runs until stopped
    python manage.py scheduler --once       # check once, run if due, exit (for tests and debugging)
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections

from apps.jobs.services import daily_at, is_due, run_daily_jobs

log = logging.getLogger("cadence.scheduler")


class Command(BaseCommand):
    help = "Run the daily jobs once a day at SCHEDULER_DAILY_AT."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check once, run the jobs if they are due, and exit")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between checks (default 60)")

    def tick(self):
        """One check. Database trouble (the web container still migrating, Postgres restarting) is logged and retried next tick."""
        close_old_connections()
        try:
            if is_due():
                for run in run_daily_jobs():
                    log.info("daily job %s: %s", run.job, run.status)
                    self.stdout.write(f"{run.job}: {run.get_status_display().lower()}")
        except DatabaseError:
            log.exception("scheduler: database unavailable; retrying at the next check")

    def handle(self, *args, **opts):
        at = daily_at()  # fail fast on a bad SCHEDULER_DAILY_AT
        if opts["once"]:
            self.tick()
            return
        self.stdout.write(f"Scheduler started: daily jobs at {at:%H:%M} local time, checking every {opts['interval']} s")
        while True:
            self.tick()
            time.sleep(max(1, opts["interval"]))
