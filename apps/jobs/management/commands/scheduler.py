"""
Run the daily jobs every day at SCHEDULER_DAILY_AT: each facility's jobs at that time on its own clock (Tenant.timezone), the openFDA
import at that time on the server's (TIME_ZONE). Meant for one long-running process, the docker-compose `scheduler` service, which
checks every minute (apps.jobs.services.is_due). If it starts after that time and a day's jobs have not run (a restart, a deploy, an
outage), it runs them at once. A second copy is harmless: each job's run for its facility and day is claimed in the database first.

    python manage.py scheduler              # runs until stopped
    python manage.py scheduler --once       # check once, run if due, exit (for tests and debugging)
"""
import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor

from apps.jobs.services import daily_at, is_due, label, run_daily_jobs

log = logging.getLogger("cadence.scheduler")


def _stop(signum, frame):
    raise SystemExit(0)


def unapplied_migrations() -> bool:
    executor = MigrationExecutor(connection)
    return bool(executor.migration_plan(executor.loader.graph.leaf_nodes()))


class Command(BaseCommand):
    help = "Run the daily jobs once a day at SCHEDULER_DAILY_AT."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Check once, run the jobs if they are due, and exit")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between checks (default 60)")

    def tick(self):
        """One check. Database trouble (Postgres restarting, the web container still migrating) is logged and retried next tick,
        and nothing runs while migrations are pending, so a job never meets a half-migrated schema."""
        close_old_connections()
        try:
            if unapplied_migrations():
                log.info("scheduler: waiting for migrations to be applied")
                return
            if is_due():
                for run in run_daily_jobs():
                    log.info("daily job %s: %s", label(run), run.status)
                    self.stdout.write(f"{label(run)}: {run.get_status_display().lower()}")
        except DatabaseError:
            log.exception("scheduler: database unavailable; retrying at the next check")

    def handle(self, *args, **opts):
        at = daily_at()  # fail fast on a bad SCHEDULER_DAILY_AT
        # `docker compose stop` sends SIGTERM. Turn it into SystemExit so a job in flight is recorded as stopped (not left
        # "running") and the process exits at once instead of being killed after the grace period.
        signal.signal(signal.SIGTERM, _stop)
        if opts["once"]:
            self.tick()
            return
        self.stdout.write(f"Scheduler started: daily jobs at {at:%H:%M}, each facility's local time for its jobs and the server's for the "
                          f"openFDA import, checking every {opts['interval']} s")
        while True:
            self.tick()
            time.sleep(max(1, opts["interval"]))
