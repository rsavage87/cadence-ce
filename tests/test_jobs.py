"""Scheduled daily jobs: each runs at most once per local day (a claimed JobRun row is the lock), a failure is recorded and does
not stop the next job, the scheduler runs when the daily time has passed (catching up after a restart), tolerates the database
being unavailable, and the two jobs keep going across tenants."""
from datetime import date, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError

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
def fda(monkeypatch):
    """No test here touches the network: the importer gets a canned openFDA answer."""
    calls = []

    def fake_get(*a, **kw):
        calls.append(kw.get("params"))
        return FakeFDA()

    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", fake_get)
    return calls


@pytest.fixture
def due_pump(tenant, dept, pump_model):
    """A pump due in five days: inside generate_pm's 21-day lead."""
    return Asset.objects.create(tag="CE-DUE", device_model=pump_model, department=dept, next_pm_on=date.today() + timedelta(days=5))


def test_both_jobs_run_and_are_recorded(due_pump, tenant, fda):
    runs = jobs.run_daily_jobs()
    assert [(r.job, r.status) for r in runs] == [("generate_pm", "succeeded"), ("import_openfda", "succeeded")]
    assert "riverside: 1 PM work orders created" in runs[0].output and "Imported 1 new alerts" in runs[1].output
    assert all(r.finished_at and r.run_on == date.today() for r in runs)
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(asset=due_pump, type=WoType.PM).count() == 1
    assert Alert.objects.filter(external_id="97001").exists()
    assert fda[0]["limit"] == 1000  # the daily import asks for openFDA's maximum page


def test_a_job_runs_once_a_day_unless_forced(due_pump, tenant, fda):
    jobs.run_daily_jobs()
    assert jobs.run_daily_jobs() == [] and len(fda) == 1  # a second scheduler, or a restart, finds today's rows and does nothing
    with tenant_context(tenant):
        WorkOrder.objects.all().delete()
    forced = jobs.run_daily_jobs(force=True, jobs=["generate_pm"])
    assert [r.job for r in forced] == ["generate_pm"] and JobRun.objects.filter(job="generate_pm").count() == 1
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(asset=due_pump).count() == 1
    tomorrow = jobs.run_daily_jobs(day=date.today() + timedelta(days=1))
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
    assert "generate_pm: succeeded" in out.getvalue() and "import_openfda: succeeded" in out.getvalue()
    out = StringIO()
    call_command("run_daily_jobs", stdout=out)
    assert "Nothing to run" in out.getvalue()


# --- when the jobs are due ---------------------------------------------------------------------------------------------

def at(hour, minute=0, day=date(2026, 9, 29)):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


def test_due_after_the_daily_time_until_every_job_has_run(db, settings):
    settings.SCHEDULER_DAILY_AT = "02:30"
    assert jobs.is_due(at(2, 29)) is False and jobs.is_due(at(2, 30)) is True and jobs.is_due(at(23, 59)) is True
    JobRun.objects.create(job="generate_pm", run_on=date(2026, 9, 29))
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
    assert "generate_pm: succeeded" in out.getvalue() and JobRun.objects.count() == 2
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
                             next_pm_on=date.today() + timedelta(days=3))
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
