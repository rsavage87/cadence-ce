"""
The daily jobs and when they are due. `run_daily_jobs` is what any scheduler calls: the `scheduler` command (a long-running
process, the docker-compose `scheduler` service, checking every minute), a platform cron, or a Kubernetes CronJob
(`manage.py run_daily_jobs`). It runs only what is due, so a cron entry calls it every 15 minutes or so, not once a day.

Slice 21: every facility works on its own clock (Tenant.timezone). A job that works facility by facility (FACILITY_JOBS: the PM
generation, the report emails, the staff notifications) runs for each active facility once that facility's local time reaches
SCHEDULER_DAILY_AT, as of its local day, once per facility and day: a facility east of the server runs earlier, one west later.
The job for everyone (the openFDA import: the FDA's notices are the same for every facility, and it then matches them to each)
runs once the server's local time (TIME_ZONE) reaches it, once per server day. A facility whose time zone changes keeps its days:
the facility's local date names the run, so a day that already ran is not run again, and a day its clock jumped over (moved
east across midnight before the hour) runs at once (_due_days).

Each run is claimed before it starts: its JobRun row for (job, facility, day), no facility for the job for everyone, is the lock,
so two schedulers, a restart, or a manual run on the same day do not repeat it (`force=True` reruns a finished job, never one
still running). A run left "running" for STALE_AFTER (killed mid-job) is taken over at the next check. Each job, and each
facility's run of one, runs on its own; one failing does not stop the next, and every run is recorded with what it printed.

All of this reads only system tables (Tenant, JobRun) before a facility's command enters its tenant_context (CLAUDE.md,
non-negotiable 2): the scheduler and run_daily_jobs start with no tenant.
"""
import logging
import traceback
from datetime import date, datetime, time, timedelta
from io import StringIO
from typing import NamedTuple

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, close_old_connections, transaction
from django.utils import timezone

from apps.tenants.context import zone_of
from apps.tenants.models import Tenant

from .models import JobRun

# (key, management command, options), in the order they run. The PM generation needs no network; the openFDA import does.
DAILY_JOBS = [
    ("generate_pm", "generate_pm", {}),
    # A 30-day window re-reads the last month each day, so a missed day or a late FDA posting is caught up; records are upserted.
    ("import_openfda", "import_openfda", {"days": 30, "limit": 1000}),
    # Slice 13: report emails due today (weekly on Mondays, monthly on the first Monday); each subscription at most once a day.
    ("report_emails", "send_report_emails", {}),
    # Slice 20: the daily digest and the contract reminders (apps/notifications/daily.py); no email goes twice, so a rerun only retries.
    ("staff_notifications", "send_staff_notifications", {}),
]
# Slice 21: the jobs that work facility by facility, each facility on its own local day: their commands take --tenant and --date
# (apps/jobs/cli.py) and run for that one facility. Every other job is one run a day for everyone, on the server's day.
FACILITY_JOBS = frozenset({"generate_pm", "report_emails", "staff_notifications"})
OUTPUT_LIMIT = 20_000
log = logging.getLogger("cadence.scheduler")


def daily_at() -> time:
    """SCHEDULER_DAILY_AT ("HH:MM", 24-hour): the local time the daily jobs run at, each facility's for its own jobs and the server's
    (TIME_ZONE) for the job for everyone."""
    raw = str(settings.SCHEDULER_DAILY_AT).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as e:
        raise ImproperlyConfigured(f'SCHEDULER_DAILY_AT must be "HH:MM" (24-hour, local time); got {raw!r}') from e


STALE_AFTER = timedelta(hours=6)  # a run still "running" this long after it started was killed; the next check reclaims it


def _stale(run: JobRun, now: datetime) -> bool:
    return run.status == JobRun.Status.RUNNING and run.started_at < now - STALE_AFTER


def local_now(facility: Tenant | None = None, now: datetime | None = None) -> datetime:
    """`now` (default: now) on `facility`'s clock (its time zone), or on the server's (TIME_ZONE) for None. Explicit, never the zone
    that happens to be active: the jobs run outside any facility, and a caller inside one must not move the server's day."""
    return timezone.localtime(now or timezone.now(), zone_of(facility))


class Slot(NamedTuple):
    """One run to make: a job, the facility it is for (None: everyone), and the local day it is for."""

    key: str
    command: str
    options: dict
    facility: Tenant | None
    day: date


def _facility_id(facility: Tenant | None):
    return facility.pk if facility is not None else None


def _runs_on(days: set[date], keys: set[str]) -> dict:
    """The recorded runs of `keys` on `days`, by (job, facility id, day)."""
    if not days or not keys:
        return {}
    return {(r.job, r.facility_id, r.run_on): r for r in JobRun.objects.filter(job__in=keys, run_on__in=days)}


def _due_days(key: str, facility: Tenant | None, clock: datetime, runs: dict) -> list[date]:
    """The days `key` is due for `facility` at `clock` (its local time), as the scheduler sees it: its local day once the hour has
    come. Before the hour, a facility job's previous local day too when that day never ran although the day before it did: the
    facility's clock jumped over it (its time zone moved east across midnight before the hour, at a late SCHEDULER_DAILY_AT), or
    the scheduler was down for exactly that day. Never further back, and never for a facility with no run before (a new facility,
    the first deploy): those start with their next day."""
    today = clock.date()
    if clock.time() >= daily_at():
        return [today]
    if key not in FACILITY_JOBS:
        return []
    yesterday, before = today - timedelta(days=1), today - timedelta(days=2)
    fid = _facility_id(facility)
    if (key, fid, yesterday) not in runs and (key, fid, before) in runs:
        return [yesterday]
    return []


def _plan(now: datetime, day: date | None = None, force: bool = False, jobs: list[str] | None = None,
          facilities: list[Tenant] | None = None) -> list[Slot]:
    """The runs to make now, in DAILY_JOBS order (each job for every facility before the next job), whether or not they ran:
    - by default, what is due (_due_days): the job for everyone on the server's day once the server's clock reaches the hour, each
      active facility's jobs on its day once its clock does;
    - `force`: today's runs at once, whatever the hour (the server's day for everyone, each facility's own day);
    - `day`: that day for each clock that has reached it, at once (a missed day); a facility still on the day before is left out.
    `facilities` limits the run to those facilities' jobs (the job for everyone is left out); `jobs` to those keys."""
    keys = [(key, command, options) for key, command, options in DAILY_JOBS if jobs is None or key in jobs]
    tenants = list(Tenant.objects.filter(is_active=True).order_by("slug")) if facilities is None else list(facilities)  # a system table
    clocks = {_facility_id(t): local_now(t, now) for t in tenants}
    server = local_now(None, now)
    runs = {}
    if day is None and not force:  # the catch-up looks back two days on each facility's clock
        back = {c.date() - timedelta(days=n) for c in clocks.values() for n in (1, 2)}
        runs = _runs_on(back, {key for key, _c, _o in keys if key in FACILITY_JOBS})

    def days_for(key, facility, clock):
        if day is not None:
            return [day] if day <= clock.date() else []
        if force:
            return [clock.date()]
        return _due_days(key, facility, clock, runs)

    slots = []
    for key, command, options in keys:
        if key in FACILITY_JOBS:
            for tenant in tenants:
                slots += [Slot(key, command, options, tenant, d) for d in days_for(key, tenant, clocks[tenant.pk])]
        elif facilities is None:
            slots += [Slot(key, command, options, None, d) for d in days_for(key, None, server)]
    return slots


def _pending(slots: list[Slot], now: datetime) -> list[Slot]:
    """The slots with no run recorded, or whose run was left "running" long enough ago to be dead."""
    runs = _runs_on({s.day for s in slots}, {s.key for s in slots})
    return [s for s in slots if (r := runs.get((s.key, _facility_id(s.facility), s.day))) is None or _stale(r, now)]


def pending_jobs(day: date, now: datetime | None = None, facility: Tenant | None = None) -> list[str]:
    """The daily jobs of `facility` (FACILITY_JOBS), or for everyone (None), with no run recorded for `day`, or whose run was left
    "running" long enough ago to be dead."""
    now = now or timezone.now()
    keys = [(k, c, o) for k, c, o in DAILY_JOBS if (k in FACILITY_JOBS) == (facility is not None)]
    return [s.key for s in _pending([Slot(k, c, o, facility, day) for k, c, o in keys], now)]


def is_due(now: datetime | None = None) -> bool:
    """True once a clock has reached SCHEDULER_DAILY_AT and a job has not run for its day: the server's for the job for everyone,
    each active facility's for its jobs. A scheduler that starts after the time (a restart, a deploy) therefore catches up at once
    instead of waiting a day."""
    now = now or timezone.now()
    return bool(_pending(_plan(now), now))


def _claim(key: str, facility: Tenant | None, day: date, force: bool) -> JobRun | None:
    """Take the row for `key`, `facility` (None: everyone), and `day`, or None when the job must not run now. A new row is inserted
    (the unique constraint is the lock: of two schedulers inserting at once, one gets IntegrityError). An existing row is taken
    over only when its run was left "running" and is stale, or, with `force`, when its run has finished; a run that is still going
    is never doubled, even forced."""
    now = timezone.now()
    with transaction.atomic():
        row = JobRun.objects.select_for_update().filter(job=key, facility=facility, run_on=day).first()
        if row is None:
            try:
                with transaction.atomic():
                    return JobRun.objects.create(job=key, facility=facility, run_on=day, started_at=now)
            except IntegrityError:
                return None
        if row.status == JobRun.Status.RUNNING and not _stale(row, now):
            return None
        if not (_stale(row, now) or force):
            return None
        note = (f"Took over a run that started {timezone.localtime(row.started_at, zone_of(facility)):%Y-%m-%d %H:%M} and never finished.\n"
                if _stale(row, now) else "")
        # Conditional: only the scheduler whose UPDATE still sees the row as it was read takes it.
        taken = (JobRun.objects.filter(pk=row.pk, status=row.status, started_at=row.started_at)
                 .update(status=JobRun.Status.RUNNING, started_at=now, finished_at=None, output=note))
        if not taken:
            return None
        row.refresh_from_db()
        row.facility = facility
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
                log.exception("daily job %s: could not record its result", label(run))
            close_old_connections()
    for name, value in fields.items():
        setattr(run, name, value)


def label(run: JobRun) -> str:
    """How the commands and the log name a run: "generate_pm (riverside, 2026-10-05)", "import_openfda (2026-10-05)"."""
    facility = run.facility if run.facility_id else None
    return f"{run.job} ({facility.slug + ', ' if facility else ''}{run.run_on:%Y-%m-%d})"


def run_daily_jobs(day: date | None = None, force: bool = False, jobs: list[str] | None = None,
                   facilities: list[Tenant] | None = None, now: datetime | None = None) -> list[JobRun]:
    """Run what is due now (see _plan: each facility's jobs on its own day once its clock reaches SCHEDULER_DAILY_AT, the job for
    everyone on the server's day once the server's does), each at most once per facility and local day; `force` runs today's at
    once and again, `day` a missed day at once, `facilities` and `jobs` limit them. A facility's job runs its command for that one
    facility and day (--tenant, --date). Returns the runs started now, finished and recorded."""
    now = now or timezone.now()
    runs = []
    for slot in _plan(now, day=day, force=force, jobs=jobs, facilities=facilities):
        run = _claim(slot.key, slot.facility, slot.day, force)
        if run is None:
            continue
        options = dict(slot.options)
        if slot.facility is not None:
            options.update(tenant=slot.facility.slug, date=slot.day.isoformat())
        out = StringIO()
        status = JobRun.Status.FAILED
        try:
            call_command(slot.command, stdout=out, stderr=out, **options)
            status = JobRun.Status.SUCCEEDED
        except Exception:  # one job's (or one facility's) failure is recorded and must not stop the next
            out.write(traceback.format_exc())
        except BaseException as e:  # stopped (SIGTERM, Ctrl-C): record it, then stop as asked
            out.write(f"Stopped before finishing ({type(e).__name__}).\n")
            _finish(run, status, out.getvalue())
            raise
        _finish(run, status, out.getvalue())
        runs.append(run)
    return runs
