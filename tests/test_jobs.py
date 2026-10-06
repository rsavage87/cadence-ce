"""Scheduled daily jobs: each runs at most once per local day (a claimed JobRun row is the lock), a failure is recorded and does
not stop the next job, the scheduler runs when the daily time has passed (catching up after a restart), tolerates the database
being unavailable, and the two jobs keep going across tenants. Slice 21: generate_pm runs per facility (its row names the facility),
the openFDA import once for everyone; tests/test_jobs_facilities.py covers facilities in other time zones."""
from datetime import date, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.utils import timezone

from apps.accounts.models import create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel
from apps.jobs import services as jobs
from apps.jobs.models import JobRun
from apps.recalls.models import Alert
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoType

NY = ZoneInfo("America/New_York")
RECORD = {"res_event_number": "97001", "recalling_firm": "BD", "product_description": "Alaris 8015 PCU infusion pump",
          "reason_for_recall": "Keypad membrane may lift", "event_date_initiated": "20260901"}


class FakeFDA:
    status_code = 200

    def __init__(self, total=1):
        self.total = total

    def raise_for_status(self):
        pass

    def json(self):
        return {"meta": {"results": {"total": self.total}}, "results": [RECORD]}


@pytest.fixture(autouse=True)
def _keep_the_test_connection(monkeypatch):
    """The scheduler drops stale connections between jobs (close_old_connections), as a long-running process must. A test runs in
    one transaction on one connection: on PostgreSQL closing it would end the test's transaction (SQLite's in-memory test database
    ignores close), so the tests keep it. What the tests check (a retry after a failed write, the next job still running) holds."""
    from django.db import connection

    if connection.vendor == "postgresql":
        monkeypatch.setattr("apps.jobs.services.close_old_connections", lambda: None)
        monkeypatch.setattr("apps.jobs.management.commands.scheduler.close_old_connections", lambda: None)


@pytest.fixture(autouse=True)
def fda(monkeypatch):
    """No test here touches the network: the importer gets a canned openFDA answer."""
    calls = []

    def fake_get(*a, **kw):
        calls.append(kw.get("params"))
        return FakeFDA()

    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def two_jobs(monkeypatch):
    """These tests are about the runner (the lock, takeover, failures), written against the first two daily jobs; slice 13's
    report emails and slice 20's staff notifications have their own tests (tests/test_report_emails.py, tests/test_daily_notifications.py)."""
    monkeypatch.setattr(jobs, "DAILY_JOBS", [j for j in jobs.DAILY_JOBS if j[0] in ("generate_pm", "import_openfda")])


ALL_JOBS = list(jobs.DAILY_JOBS)  # read when this file is imported, before the fixture above pins the list


@pytest.fixture(autouse=True)
def _due_all_day(settings):
    """run_daily_jobs runs only what is due (slice 21): with the daily time at midnight every job is due on any real clock, as these
    tests about the runner expect. The tests about when jobs are due set their own time."""
    settings.SCHEDULER_DAILY_AT = "00:00"


def test_the_daily_jobs_include_the_report_emails_and_the_staff_notifications():
    assert [key for key, _cmd, _opts in ALL_JOBS] == ["generate_pm", "import_openfda", "expire_imports", "report_emails", "staff_notifications"]
    assert jobs.FACILITY_JOBS == {"generate_pm", "report_emails", "staff_notifications"}  # the openFDA import is everyone's


@pytest.fixture
def due_pump(tenant, dept, pump_model):
    """A pump due in five days: inside generate_pm's 21-day lead."""
    return Asset.objects.create(tag="CE-DUE", device_model=pump_model, department=dept, next_pm_on=timezone.localdate() + timedelta(days=5))


def test_both_jobs_run_and_are_recorded(due_pump, tenant, fda):
    runs = jobs.run_daily_jobs()
    assert [(r.job, r.status) for r in runs] == [("generate_pm", "succeeded"), ("import_openfda", "succeeded")]
    assert "riverside: 1 PM work orders created" in runs[0].output and "Imported 1 new alerts" in runs[1].output
    assert all(r.finished_at and r.run_on == timezone.localdate() for r in runs)  # riverside is in the server's zone (New York)
    assert (runs[0].facility, runs[1].facility) == (tenant, None)  # generate_pm is the facility's run; the import is everyone's
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(asset=due_pump, type=WoType.PM).count() == 1
    assert Alert.objects.filter(external_id="97001").exists()
    assert fda[0]["limit"] == 1000  # the daily import asks for openFDA's maximum page


def test_a_job_runs_once_a_day_unless_forced(due_pump, tenant, fda, monkeypatch):
    jobs.run_daily_jobs()
    assert jobs.run_daily_jobs() == [] and len(fda) == 1  # a second scheduler, or a restart, finds today's rows and does nothing
    with tenant_context(tenant):
        WorkOrder.objects.all().delete()
    forced = jobs.run_daily_jobs(force=True, jobs=["generate_pm"])
    assert [r.job for r in forced] == ["generate_pm"] and JobRun.objects.filter(job="generate_pm").count() == 1
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(asset=due_pump).count() == 1
    now = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: now + timedelta(days=1))
    tomorrow = jobs.run_daily_jobs()
    assert len(tomorrow) == 2  # a new day, new runs


def test_a_failing_job_is_recorded_and_the_next_one_still_runs(db, monkeypatch, fda):
    monkeypatch.setattr(jobs, "DAILY_JOBS", [("boom", "import_openfda", {"days": "not a number"}), *jobs.DAILY_JOBS[1:]])
    runs = jobs.run_daily_jobs()
    assert [(r.job, r.status) for r in runs] == [("boom", "failed"), ("import_openfda", "succeeded")]
    assert "Traceback" in runs[0].output
    with pytest.raises(CommandError, match="Failed: boom"):
        call_command("run_daily_jobs", "--force", stdout=StringIO())


def test_run_daily_jobs_command_reports_and_skips(due_pump, fda):
    out = StringIO()
    call_command("run_daily_jobs", stdout=out)
    today = f"{timezone.localdate():%Y-%m-%d}"
    assert out.getvalue().splitlines() == [f"generate_pm (riverside, {today}): succeeded", f"import_openfda ({today}): succeeded"]
    out = StringIO()
    call_command("run_daily_jobs", stdout=out)
    assert "Nothing to run" in out.getvalue()


# --- when the jobs are due ---------------------------------------------------------------------------------------------

def at(hour, minute=0, day=date(2026, 9, 29)):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


def test_due_after_the_daily_time_until_every_job_has_run(tenant, settings):
    settings.SCHEDULER_DAILY_AT = "02:30"
    assert jobs.is_due(at(2, 29)) is False and jobs.is_due(at(2, 30)) is True and jobs.is_due(at(23, 59)) is True
    JobRun.objects.create(job="generate_pm", facility=tenant, run_on=date(2026, 9, 29))
    assert jobs.is_due(at(9)) is True  # one job still pending (a restart between the two)
    JobRun.objects.create(job="import_openfda", run_on=date(2026, 9, 29))
    assert jobs.is_due(at(9)) is False and jobs.is_due(at(3, day=date(2026, 9, 30))) is True


def test_the_daily_time_is_local(db, settings):
    settings.SCHEDULER_DAILY_AT = "02:30"
    utc_0630 = datetime(2026, 9, 29, 6, 29, tzinfo=ZoneInfo("UTC"))  # 02:29 in New York (EDT)
    assert jobs.is_due(utc_0630) is False and jobs.is_due(utc_0630 + timedelta(minutes=1)) is True


@pytest.mark.parametrize("value", ["2:30pm", "25:00", "", "0230"])
def test_a_bad_daily_time_fails_loudly(db, settings, value):
    settings.SCHEDULER_DAILY_AT = value
    with pytest.raises(ImproperlyConfigured, match="SCHEDULER_DAILY_AT"):
        jobs.is_due(at(3))
    with pytest.raises(ImproperlyConfigured):
        call_command("scheduler", "--once", stdout=StringIO())


def test_scheduler_once_runs_when_due_and_not_before(due_pump, settings, monkeypatch, fda):
    settings.SCHEDULER_DAILY_AT = "00:00"
    out = StringIO()
    call_command("scheduler", "--once", stdout=out)
    assert "generate_pm (riverside, " in out.getvalue() and "): succeeded" in out.getvalue() and JobRun.objects.count() == 2
    settings.SCHEDULER_DAILY_AT = "23:59"
    JobRun.objects.all().delete()
    monkeypatch.setattr(jobs.timezone, "now", lambda: at(12))
    call_command("scheduler", "--once", stdout=StringIO())
    assert JobRun.objects.count() == 0  # 12:00 is before 23:59


def test_scheduler_survives_a_database_outage(db, monkeypatch, settings):
    settings.SCHEDULER_DAILY_AT = "00:00"

    def down(*a, **kw):
        raise OperationalError("connection refused")

    monkeypatch.setattr("apps.jobs.management.commands.scheduler.is_due", down)
    call_command("scheduler", "--once", stdout=StringIO())  # logged, not raised: the loop tries again at the next check


# --- the jobs across tenants -----------------------------------------------------------------------------------------------

def test_generate_pm_keeps_going_when_one_tenant_fails(due_pump, tenant, other_tenant, monkeypatch):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C")
        Asset.objects.create(tag="THEIRS", device_model=dm, department=Department.objects.create(name="ICU"),
                             next_pm_on=timezone.localdate() + timedelta(days=3))
    import apps.pm.management.commands.generate_pm as cmd

    real = cmd.generate_pm_work_orders

    def flaky(**kw):
        from apps.tenants.context import get_current_tenant

        if get_current_tenant().slug == "other":  # "Other Hospital" sorts before "Riverside Regional": it fails first
            raise RuntimeError("bad data")
        return real(**kw)

    monkeypatch.setattr(cmd, "generate_pm_work_orders", flaky)
    out, err = StringIO(), StringIO()
    with pytest.raises(CommandError, match="PM generation failed for other"):
        call_command("generate_pm", stdout=out, stderr=err)
    assert "riverside: 1 PM work orders created" in out.getvalue() and "other: failed" in err.getvalue()
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(asset=due_pump).exists()


def test_import_warns_when_openfda_has_more_than_it_returned(db, monkeypatch):
    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", lambda *a, **kw: FakeFDA(total=2500))
    err = StringIO()
    call_command("import_openfda", days=30, stdout=StringIO(), stderr=err)
    assert "openFDA reports 2500 recalls in the window but returned 1" in err.getvalue()


# --- review fixes: never double a running job, take over dead runs, record results robustly, stop cleanly -----------------

def test_force_never_starts_a_second_copy_of_a_running_job(due_pump, monkeypatch):
    """The scheduler is mid-way through generate_pm when someone runs `run_daily_jobs --force`: the forced run must not start it again,
    the first run must still record its result, and the next job must still run."""
    calls = []
    real = jobs.call_command

    def during(command, **kw):
        calls.append(command)
        if command == "generate_pm" and calls.count("generate_pm") == 1:
            assert jobs.run_daily_jobs(force=True, jobs=["generate_pm"]) == []  # held: still running
        return real(command, **kw)

    monkeypatch.setattr(jobs, "call_command", during)
    runs = jobs.run_daily_jobs()
    assert calls == ["generate_pm", "import_openfda"] and [(r.job, r.status) for r in runs] == [("generate_pm", "succeeded"), ("import_openfda", "succeeded")]
    assert JobRun.objects.get(job="generate_pm").status == "succeeded"


def test_force_reruns_a_finished_job(due_pump):
    jobs.run_daily_jobs(jobs=["generate_pm"])
    first = JobRun.objects.get(job="generate_pm")
    again = jobs.run_daily_jobs(force=True, jobs=["generate_pm"])
    assert [r.pk for r in again] == [first.pk] and JobRun.objects.get(pk=first.pk).status == "succeeded"


def test_a_run_left_running_is_taken_over_once_stale(due_pump, tenant, settings):
    settings.SCHEDULER_DAILY_AT = "00:00"
    now = jobs.timezone.now()
    fresh = JobRun.objects.create(job="generate_pm", facility=tenant, run_on=jobs.timezone.localdate(), started_at=now - timedelta(hours=1))
    JobRun.objects.create(job="import_openfda", run_on=fresh.run_on, status="succeeded")
    assert "generate_pm" not in jobs.pending_jobs(fresh.run_on, facility=tenant) and jobs.run_daily_jobs(jobs=["generate_pm"]) == []
    assert jobs.is_due() is False  # maybe still going
    JobRun.objects.filter(pk=fresh.pk).update(started_at=now - jobs.STALE_AFTER - timedelta(minutes=1))
    assert "generate_pm" in jobs.pending_jobs(fresh.run_on, facility=tenant) and jobs.is_due() is True
    [run] = jobs.run_daily_jobs(jobs=["generate_pm"])
    assert run.pk == fresh.pk and run.status == "succeeded" and run.output.startswith("Took over a run that started")


def test_two_schedulers_claiming_at_once_run_a_job_once(tenant, monkeypatch):
    """Between one scheduler finding no row and inserting it, the other inserts first: the unique constraint makes the loser skip."""
    real_create = JobRun.objects.create
    raced = []

    def racing_create(**kw):
        if not raced:
            raced.append(kw["job"])
            JobRun.objects.bulk_create([JobRun(job=kw["job"], facility=kw["facility"], run_on=kw["run_on"])])  # the other scheduler got there first
        return real_create(**kw)

    monkeypatch.setattr(JobRun.objects, "create", racing_create)
    ran = []
    monkeypatch.setattr(jobs, "call_command", lambda command, **kw: ran.append(command))
    runs = jobs.run_daily_jobs()
    assert raced == ["generate_pm"] and "generate_pm" not in ran and [r.job for r in runs] == ["import_openfda"]


def test_a_lost_connection_while_recording_does_not_skip_the_next_job(tenant, monkeypatch):
    real_update = jobs.JobRun.objects.filter
    failures = []

    class Flaky:
        def __init__(self, qs):
            self.qs = qs

        def update(self, **kw):
            if not failures:
                failures.append(1)
                raise OperationalError("server closed the connection unexpectedly")
            return self.qs.update(**kw)

        def __getattr__(self, name):
            return getattr(self.qs, name)

    monkeypatch.setattr(jobs, "call_command", lambda command, **kw: None)
    monkeypatch.setattr(jobs.JobRun.objects, "filter", lambda *a, **kw: Flaky(real_update(*a, **kw)) if "pk" in kw else real_update(*a, **kw))
    runs = jobs.run_daily_jobs()
    assert [r.job for r in runs] == ["generate_pm", "import_openfda"] and failures == [1]
    assert set(JobRun.objects.values_list("status", flat=True)) == {"succeeded"}  # the retry on a fresh connection recorded it


def test_a_stop_during_a_job_is_recorded_and_honoured(tenant, monkeypatch):
    def stopped(command, **kw):
        raise SystemExit(0)  # what the scheduler's SIGTERM handler raises

    monkeypatch.setattr(jobs, "call_command", stopped)
    with pytest.raises(SystemExit):
        jobs.run_daily_jobs()
    run = JobRun.objects.get()
    assert (run.job, run.status) == ("generate_pm", "failed") and "Stopped before finishing (SystemExit)" in run.output
    assert not JobRun.objects.filter(job="import_openfda").exists()  # it stopped, as asked


def test_scheduler_turns_sigterm_into_a_clean_exit_and_waits_for_migrations(db, monkeypatch, settings):
    import signal

    from apps.jobs.management.commands import scheduler as cmd

    settings.SCHEDULER_DAILY_AT = "00:00"
    monkeypatch.setattr(cmd, "unapplied_migrations", lambda: True)
    call_command("scheduler", "--once", stdout=StringIO())
    assert JobRun.objects.count() == 0  # nothing runs against a half-migrated schema
    assert signal.getsignal(signal.SIGTERM) is cmd._stop
    with pytest.raises(SystemExit):
        cmd._stop(signal.SIGTERM, None)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_import_keeps_one_alert_per_recalled_product(monkeypatch, tenant, pump_model):
    pump = {**RECORD, "res_event_number": "97001", "product_res_number": "Z-0101-2026", "product_description": "Alaris 8015 PCU infusion pump"}
    syringe = {**RECORD, "res_event_number": "97001", "product_res_number": "Z-0102-2026", "product_description": "Alaris 8110 syringe module"}

    class TwoProducts(FakeFDA):
        def json(self):
            return {"meta": {"results": {"total": 2}}, "results": [pump, syringe]}

    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", lambda *a, **kw: TwoProducts())
    call_command("import_openfda", days=30, stdout=StringIO())
    assert set(Alert.objects.filter(source="fda").values_list("external_id", flat=True)) == {"Z-0101-2026", "Z-0102-2026"}
    with tenant_context(tenant):
        from apps.recalls.models import AlertMatch

        assert AlertMatch.objects.get().alert.external_id == "Z-0101-2026"  # the pump product matched the pump model


def test_import_matching_keeps_going_when_one_tenant_fails(tenant, other_tenant, pump_model, monkeypatch):
    import apps.recalls.management.commands.import_openfda as cmd

    real = cmd.match_all_open_alerts

    def flaky():
        from apps.tenants.context import get_current_tenant

        if get_current_tenant().slug == "other":
            raise RuntimeError("bad data")
        return real()

    monkeypatch.setattr(cmd, "match_all_open_alerts", flaky)
    out, err = StringIO(), StringIO()
    with pytest.raises(CommandError, match="Recall matching failed for other"):
        call_command("import_openfda", days=30, stdout=out, stderr=err)
    assert "riverside: 1 new matches" in out.getvalue() and "other: matching failed" in err.getvalue()
