"""
The daily jobs and when they are due. `run_daily_jobs` is what any scheduler calls: the `scheduler` command (a long-running
process, the docker-compose `scheduler` service), a platform cron, or a Kubernetes CronJob (`manage.py run_daily_jobs`).

Each job runs at most once per local day: its JobRun row for the day is claimed before it starts, so two schedulers, a restart,
or a manual run on the same day do not repeat it (`force=True` does). Each job runs on its own; one failing does not stop the
next, and every run is recorded with what it printed.
"""
import traceback
from datetime import date, datetime, time
from io import StringIO

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import JobRun

# (key, management command, options), in the order they run. The PM generation needs no network; the openFDA import does.
DAILY_JOBS = [
    ("generate_pm", "generate_pm", {}),
    # A 30-day window re-reads the last month each day, so a missed day or a late FDA posting is caught up; records are upserted.
    ("import_openfda", "import_openfda", {"days": 30, "limit": 1000}),
]
OUTPUT_LIMIT = 20_000


def daily_at() -> time:
    """SCHEDULER_DAILY_AT ("HH:MM", local time in TIME_ZONE)."""
    raw = str(settings.SCHEDULER_DAILY_AT).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as e:
        raise ImproperlyConfigured(f'SCHEDULER_DAILY_AT must be "HH:MM" (24-hour, local time); got {raw!r}') from e


def pending_jobs(day: date) -> list[str]:
    """The daily jobs with no run recorded for `day`."""
    done = set(JobRun.objects.filter(run_on=day).values_list("job", flat=True))
    return [key for key, _cmd, _opts in DAILY_JOBS if key not in done]


def is_due(now: datetime | None = None) -> bool:
    """True once the local time has reached SCHEDULER_DAILY_AT and a daily job has not run today. A scheduler that starts after
    the time (a restart, a deploy) therefore catches up at once instead of waiting a day."""
    now = timezone.localtime(now or timezone.now())
    return now.time() >= daily_at() and bool(pending_jobs(now.date()))


def _claim(key: str, day: date, force: bool) -> JobRun | None:
    """Create today's row for `key`, or None when another run already holds it (unless `force`, which replaces it)."""
    if force:
        JobRun.objects.filter(job=key, run_on=day).delete()
    try:
        with transaction.atomic():
            return JobRun.objects.create(job=key, run_on=day)
    except IntegrityError:
        return None


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
        try:
            call_command(command, stdout=out, stderr=out, **options)
            run.status = JobRun.Status.SUCCEEDED
        except Exception:  # noqa: BLE001 - one job's failure is recorded and must not stop the next job
            out.write(traceback.format_exc())
            run.status = JobRun.Status.FAILED
        run.finished_at = timezone.now()
        run.output = out.getvalue()[-OUTPUT_LIMIT:]
        run.save(update_fields=["status", "finished_at", "output"])
        runs.append(run)
    return runs
