"""
The daily jobs and when they are due. `run_daily_jobs` is what any scheduler calls: the `scheduler` command (a long-running
process, the docker-compose `scheduler` service), a platform cron, or a Kubernetes CronJob (`manage.py run_daily_jobs`).

Each job runs at most once per local day: its JobRun row for the day is claimed before it starts, so two schedulers, a restart,
or a manual run on the same day do not repeat it (`force=True` reruns a finished job, never one still running). A run left
"running" for STALE_AFTER (killed mid-job) is taken over at the next check. Each job runs on its own; one failing does not
stop the next, and every run is recorded with what it printed.
"""
import logging
import traceback
from datetime import date, datetime, time, timedelta
from io import StringIO

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, close_old_connections, transaction
from django.utils import timezone

from .models import JobRun

# (key, management command, options), in the order they run. The PM generation needs no network; the openFDA import does.
DAILY_JOBS = [
    ("generate_pm", "generate_pm", {}),
    # A 30-day window re-reads the last month each day, so a missed day or a late FDA posting is caught up; records are upserted.
    ("import_openfda", "import_openfda", {"days": 30, "limit": 1000}),
    # Slice 13: report emails due today (weekly on Mondays, monthly on the first Monday); each subscription at most once a day.
    ("report_emails", "send_report_emails", {}),
]
OUTPUT_LIMIT = 20_000
log = logging.getLogger("cadence.scheduler")


def daily_at() -> time:
    """SCHEDULER_DAILY_AT ("HH:MM", local time in TIME_ZONE)."""
    raw = str(settings.SCHEDULER_DAILY_AT).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as e:
        raise ImproperlyConfigured(f'SCHEDULER_DAILY_AT must be "HH:MM" (24-hour, local time); got {raw!r}') from e


STALE_AFTER = timedelta(hours=6)  # a run still "running" this long after it started was killed; the next check reclaims it


def _stale(run: JobRun, now: datetime) -> bool:
    return run.status == JobRun.Status.RUNNING and run.started_at < now - STALE_AFTER


def pending_jobs(day: date, now: datetime | None = None) -> list[str]:
    """The daily jobs with no run recorded for `day`, or whose run was left "running" long enough ago to be dead."""
    now = now or timezone.now()
    runs = {r.job: r for r in JobRun.objects.filter(run_on=day)}
    return [key for key, _cmd, _opts in DAILY_JOBS if key not in runs or _stale(runs[key], now)]


def is_due(now: datetime | None = None) -> bool:
    """True once the local time has reached SCHEDULER_DAILY_AT and a daily job has not run today. A scheduler that starts after
    the time (a restart, a deploy) therefore catches up at once instead of waiting a day."""
    now = timezone.localtime(now or timezone.now())
    return now.time() >= daily_at() and bool(pending_jobs(now.date(), now))


def _claim(key: str, day: date, force: bool) -> JobRun | None:
    """Take today's row for `key`, or None when the job must not run now. A new row is inserted (the unique constraint is the
    lock: of two schedulers inserting at once, one gets IntegrityError). An existing row is taken over only when its run was left
    "running" and is stale, or, with `force`, when its run has finished; a run that is still going is never doubled, even forced."""
    now = timezone.now()
    with transaction.atomic():
        row = JobRun.objects.select_for_update().filter(job=key, run_on=day).first()
        if row is None:
            try:
                with transaction.atomic():
                    return JobRun.objects.create(job=key, run_on=day, started_at=now)
            except IntegrityError:
                return None
        if row.status == JobRun.Status.RUNNING and not _stale(row, now):
            return None
        if not (_stale(row, now) or force):
            return None
        note = (f"Took over a run that started {timezone.localtime(row.started_at):%Y-%m-%d %H:%M} and never finished.\n"
                if _stale(row, now) else "")
        # Conditional: only the scheduler whose UPDATE still sees the row as it was read takes it.
        taken = (JobRun.objects.filter(pk=row.pk, status=row.status, started_at=row.started_at)
                 .update(status=JobRun.Status.RUNNING, started_at=now, finished_at=None, output=note))
        if not taken:
            return None
        row.refresh_from_db()
        return row


def _finish(run: JobRun, status: str, output: str) -> None:
    """Record the result. An UPDATE by pk, retried once on a fresh connection, so a dropped connection (Postgres restarting
    mid-job) neither loses the result nor stops the next job; if it still fails, the row is left for the stale-run takeover."""
    fields = {"status": status, "finished_at": timezone.now(), "output": (run.output + output)[-OUTPUT_LIMIT:]}
    for attempt in (1, 2):
        try:
            JobRun.objects.filter(pk=run.pk).update(**fields)
            break
        except DatabaseError:
            if attempt == 2:
                log.exception("daily job %s: could not record its result", run.job)
            close_old_connections()
    for name, value in fields.items():
        setattr(run, name, value)


def run_daily_jobs(day: date | None = None, force: bool = False, jobs: list[str] | None = None) -> list[JobRun]:
    """Run each daily job not yet run for `day` (default: today, local). Returns the runs started now, finished and recorded."""
    day = day or timezone.localdate()
    runs = []
    for key, command, options in DAILY_JOBS:
        if jobs is not None and key not in jobs:
            continue
        run = _claim(key, day, force)
        if run is None:
            continue
        out = StringIO()
        status = JobRun.Status.FAILED
        try:
            call_command(command, stdout=out, stderr=out, **options)
            status = JobRun.Status.SUCCEEDED
        except Exception:  # one job's failure is recorded and must not stop the next job
            out.write(traceback.format_exc())
        except BaseException as e:  # stopped (SIGTERM, Ctrl-C): record it, then stop as asked
            out.write(f"Stopped before finishing ({type(e).__name__}).\n")
            _finish(run, status, out.getvalue())
            raise
        _finish(run, status, out.getvalue())
        runs.append(run)
    return runs
