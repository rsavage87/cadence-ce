"""
The daily jobs run for each facility on its own local day, at its own local time (slice 21, part A; apps/jobs/services.py).

Three facilities on three clocks: Thames (Europe/London, UTC+1 in early October), Riverside (America/New_York, the server's zone,
UTC-4), and Kona (Pacific/Honolulu, UTC-10). SCHEDULER_DAILY_AT is 02:30, so on October 5 Thames reaches it at 01:30 UTC,
Riverside (and the server, which runs the openFDA import for everyone) at 06:30 UTC, and Kona at 12:30 UTC. The clock is pinned by
monkeypatching django.utils.timezone.now. Most tests record what the runner would call (the commands, their --tenant and --date);
the ones about what a job does with its day run the real commands.
"""
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from test_rls_paths import rls  # noqa: F401

from apps.accounts.models import Role, User, create_default_roles
from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.jobs import services as jobs
from apps.jobs.models import JobRun
from apps.notifications.models import NotificationPreference
from apps.reports import subscriptions as subs
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import WorkOrder, WoType

OCT3, OCT4, OCT5, OCT6 = date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)  # Oct 5, 2026 is a Monday
FACILITY_COMMANDS = ["generate_pm", "send_report_emails", "send_staff_notifications"]
RECORD = {"product_res_number": "Z-2026-0505", "recalling_firm": "Zoll", "product_description": "R Series Plus defibrillator",
          "reason_for_recall": "Battery may not hold a charge", "event_date_posted": "2026-10-01"}


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026, in UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_timezone.utc)


@pytest.fixture(autouse=True)
def _setup(settings, monkeypatch):
    settings.SCHEDULER_DAILY_AT = "02:30"
    settings.APP_BASE_URL = "https://ce.example.org"
    assert settings.TIME_ZONE == "America/New_York"  # the server's clock in these tests

    class FakeFDA:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"meta": {"results": {"total": 1}}, "results": [RECORD]}

    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", lambda *a, **kw: FakeFDA())  # no network
    from django.db import connection

    if connection.vendor == "postgresql":  # one test, one transaction (tests/test_jobs.py)
        monkeypatch.setattr("apps.jobs.services.close_old_connections", lambda: None)
        monkeypatch.setattr("apps.jobs.management.commands.scheduler.close_old_connections", lambda: None)


@pytest.fixture
def clock(monkeypatch):
    """clock(moment): from now on, timezone.now() is `moment` (timezone.localdate() and localtime() follow it)."""

    def _at(moment):
        monkeypatch.setattr(timezone, "now", lambda: moment)

    return _at


@pytest.fixture
def places(tenant):
    """Thames (London), Riverside (New York, the `tenant` fixture), Kona (Honolulu), each with the default roles."""
    tenant.timezone = "America/New_York"
    tenant.save(update_fields=["timezone"])
    thames = Tenant.objects.create(name="Thames General", slug="thames", timezone="Europe/London")
    kona = Tenant.objects.create(name="Kona Community", slug="kona", timezone="Pacific/Honolulu")
    for t in (thames, kona):
        create_default_roles(t)
    return {"thames": thames, "riverside": tenant, "kona": kona}


@pytest.fixture
def calls(monkeypatch):
    """What the runner calls, as (command, --tenant, --date), instead of running the commands."""
    made = []

    def fake(command, **kw):
        made.append((command, kw.get("tenant"), kw.get("date")))

    monkeypatch.setattr(jobs, "call_command", fake)
    return made


def _done(day, *facilities, everyone=True):
    """Each facility's jobs, and (with `everyone`) the openFDA import, ran on `day` (a steady state to start from)."""
    rows = [JobRun(job=key, facility=f, run_on=day, status="succeeded") for f in facilities for key in sorted(jobs.FACILITY_JOBS)]
    if everyone:
        rows.append(JobRun(job="import_openfda", run_on=day, status="succeeded"))
    JobRun.objects.bulk_create(rows)


def _ran(runs):
    return [(r.job, r.facility.slug if r.facility_id else None, f"{r.run_on:%Y-%m-%d}") for r in runs]


def _theirs(slug, day):
    """What one facility's run calls: its three commands, for it and its day."""
    return [(command, slug, f"{day:%Y-%m-%d}") for command in FACILITY_COMMANDS]


# --- each facility on its own clock ----------------------------------------------------------------------------------------

def test_each_facility_runs_its_jobs_once_on_its_own_day_at_its_own_time(places, clock, calls):
    """East of the server runs earlier, west later; the openFDA import runs once, on the server's clock; nothing runs twice."""
    _done(OCT4, *places.values())
    clock(utc(5, 1, 29))  # Thames 02:29 Oct 5, Riverside 21:29 Oct 4, Kona 15:29 Oct 4
    assert jobs.is_due() is False and jobs.run_daily_jobs() == []

    clock(utc(5, 1, 30))  # Thames 02:30
    assert jobs.is_due() is True
    assert _ran(jobs.run_daily_jobs()) == [("generate_pm", "thames", "2026-10-05"), ("report_emails", "thames", "2026-10-05"),
                                           ("staff_notifications", "thames", "2026-10-05")]
    assert calls == _theirs("thames", OCT5)
    clock(utc(5, 1, 31))
    assert jobs.is_due() is False and jobs.run_daily_jobs() == []

    calls.clear()
    clock(utc(5, 6, 29))
    assert jobs.is_due() is False
    clock(utc(5, 6, 30))  # Riverside 02:30, and the server's: the import for everyone
    assert _ran(jobs.run_daily_jobs()) == [("generate_pm", "riverside", "2026-10-05"), ("import_openfda", None, "2026-10-05"),
                                           ("report_emails", "riverside", "2026-10-05"), ("staff_notifications", "riverside", "2026-10-05")]
    assert ("import_openfda", None, None) in calls  # the import is not given a facility or a day

    clock(utc(5, 12, 29))
    assert jobs.is_due() is False
    clock(utc(5, 12, 30))  # Kona 02:30 Oct 5
    assert _ran(jobs.run_daily_jobs()) == [("generate_pm", "kona", "2026-10-05"), ("report_emails", "kona", "2026-10-05"),
                                           ("staff_notifications", "kona", "2026-10-05")]
    for moment in (utc(5, 18), utc(5, 23, 59), utc(6, 1, 29)):  # nothing more until Thames reaches 02:30 on Oct 6
        clock(moment)
        assert jobs.is_due() is False and jobs.run_daily_jobs() == []
    assert JobRun.objects.filter(run_on=OCT5).count() == 3 * 3 + 1
    clock(utc(6, 1, 30))
    assert _ran(jobs.run_daily_jobs())[0] == ("generate_pm", "thames", "2026-10-06")


def test_the_scheduler_and_the_command_run_what_is_due(places, clock, calls):
    _done(OCT4, *places.values())
    clock(utc(5, 6, 30))
    out = StringIO()
    call_command("scheduler", "--once", stdout=out)
    assert out.getvalue().splitlines() == [  # each job for every facility (by slug) before the next job; Kona is still on a day that ran
        "generate_pm (riverside, 2026-10-05): succeeded", "generate_pm (thames, 2026-10-05): succeeded", "import_openfda (2026-10-05): succeeded",
        "report_emails (riverside, 2026-10-05): succeeded", "report_emails (thames, 2026-10-05): succeeded",
        "staff_notifications (riverside, 2026-10-05): succeeded", "staff_notifications (thames, 2026-10-05): succeeded"]
    call_command("run_daily_jobs", stdout=(again := StringIO()))
    assert again.getvalue().startswith("Nothing to run")
    clock(utc(5, 12, 30))
    call_command("run_daily_jobs", stdout=(kona := StringIO()))
    assert kona.getvalue().splitlines()[0] == "generate_pm (kona, 2026-10-05): succeeded"


def test_an_inactive_facility_runs_nothing(places, clock, calls):
    Tenant.objects.filter(pk=places["kona"].pk).update(is_active=False)
    clock(utc(5, 12, 30))
    assert {slug for _job, slug, _day in _ran(jobs.run_daily_jobs())} == {"thames", "riverside", None}


# --- --force and --date, per facility ----------------------------------------------------------------------------------------

def test_force_and_date_work_per_facility(places, clock, calls):
    riverside, kona = places["riverside"], places["kona"]
    _done(OCT4, *places.values())
    clock(utc(5, 6, 30))  # Riverside and Thames on Oct 5 (both ran just now); Kona still on Oct 4 (ran yesterday)
    jobs.run_daily_jobs()
    rows = JobRun.objects.count()

    calls.clear()
    assert _ran(jobs.run_daily_jobs(force=True, facilities=[kona])) == [  # its today, again: not the import, nobody else
        ("generate_pm", "kona", "2026-10-04"), ("report_emails", "kona", "2026-10-04"), ("staff_notifications", "kona", "2026-10-04")]
    assert calls == _theirs("kona", OCT4)
    assert _ran(jobs.run_daily_jobs(force=True, jobs=["generate_pm"], facilities=[riverside])) == [("generate_pm", "riverside", "2026-10-05")]
    assert JobRun.objects.count() == rows  # forced runs take over their own rows

    calls.clear()
    assert [r.run_on for r in jobs.run_daily_jobs(day=OCT3, facilities=[kona])] == [OCT3] * 3  # a missed day, as of that day
    assert calls == _theirs("kona", OCT3)
    assert jobs.run_daily_jobs(day=OCT5) == []  # Riverside, Thames, and the import ran Oct 5; Kona has not reached it
    assert _ran(jobs.run_daily_jobs(day=OCT5, force=True, jobs=["generate_pm"])) == [("generate_pm", "riverside", "2026-10-05"),
                                                                                    ("generate_pm", "thames", "2026-10-05")]

    JobRun.objects.filter(job="generate_pm", facility=riverside, run_on=OCT5).update(status="running", started_at=utc(5, 6, 0))
    assert jobs.run_daily_jobs(force=True, jobs=["generate_pm"], facilities=[riverside]) == []  # never a second copy of a running one


def test_the_command_takes_a_facility_and_a_past_day(places, clock, calls):
    _done(OCT4, *places.values())
    clock(utc(5, 6, 30))
    out = StringIO()
    call_command("run_daily_jobs", "--tenant", "kona", "--force", stdout=out)
    assert out.getvalue().splitlines() == ["generate_pm (kona, 2026-10-04): succeeded", "report_emails (kona, 2026-10-04): succeeded",
                                           "staff_notifications (kona, 2026-10-04): succeeded"]
    out = StringIO()
    call_command("run_daily_jobs", "--date", "2026-10-05", "--job", "generate_pm", stdout=out)
    assert out.getvalue().splitlines() == ["kona: left out, 2026-10-05 has not come there yet",
                                           "generate_pm (riverside, 2026-10-05): succeeded", "generate_pm (thames, 2026-10-05): succeeded"]
    call_command("run_daily_jobs", "--date", "2026-10-05", "--job", "import_openfda", stdout=(everyone := StringIO()))
    assert everyone.getvalue().splitlines() == ["import_openfda (2026-10-05): succeeded"]  # no facility's job asked for, none left out
    with pytest.raises(CommandError, match="future: it is still 2026-10-04 at kona"):
        call_command("run_daily_jobs", "--tenant", "kona", "--date", "2026-10-05", stdout=StringIO())
    with pytest.raises(CommandError, match="future"):
        call_command("run_daily_jobs", "--date", "2026-10-06", stdout=StringIO())
    with pytest.raises(CommandError, match="No active facility"):
        call_command("run_daily_jobs", "--tenant", "nowhere", stdout=StringIO())
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        call_command("run_daily_jobs", "--date", "5 Oct", stdout=StringIO())


def test_the_facility_commands_take_a_facility_and_refuse_a_day_it_has_not_reached(places, clock):
    clock(utc(5, 6, 30))  # Kona is on Oct 4
    for command in FACILITY_COMMANDS:
        with pytest.raises(CommandError, match="future: it is still 2026-10-04 at kona"):
            call_command(command, "--tenant", "kona", "--date", "2026-10-05", stdout=StringIO())
        with pytest.raises(CommandError, match="No active facility"):
            call_command(command, "--tenant", "nowhere", stdout=StringIO())
    out = StringIO()
    call_command("send_staff_notifications", stdout=out)  # every facility, each as of its own today
    assert out.getvalue().splitlines()[-1] == "Staff notifications for each facility's today (2026-10-04, 2026-10-05): 0 sent, 0 failed, 0 skipped"
    call_command("send_report_emails", "--tenant", "kona", stdout=(one := StringIO()))
    assert one.getvalue().splitlines() == ["kona: nothing due", "Report emails for 2026-10-04: 0 sent, 0 failed, 0 skipped"]


# --- failures and dead runs ------------------------------------------------------------------------------------------------

def test_a_facility_run_left_running_is_taken_over_once_stale(places, clock, calls):
    kona = places["kona"]
    _done(OCT4, *places.values())
    _done(OCT5, places["thames"], places["riverside"])
    dead = JobRun.objects.create(job="generate_pm", facility=kona, run_on=OCT5, started_at=utc(5, 12, 30))  # Kona 02:30 Oct 5
    JobRun.objects.bulk_create([JobRun(job=key, facility=kona, run_on=OCT5, status="succeeded") for key in ("report_emails", "staff_notifications")])
    clock(utc(5, 18))  # five and a half hours on: maybe still going
    assert jobs.is_due() is False and jobs.run_daily_jobs() == []
    clock(utc(5, 18, 31))  # six hours and a minute
    assert jobs.is_due() is True
    [run] = jobs.run_daily_jobs()
    assert (run.pk, run.status) == (dead.pk, "succeeded") and calls == [("generate_pm", "kona", "2026-10-05")]
    assert run.output.startswith("Took over a run that started 2026-10-05 02:30 and never finished.")  # Kona's local time


def test_one_facility_failing_does_not_stop_the_others(places, clock, monkeypatch):
    def flaky(command, **kw):
        if (command, kw.get("tenant")) == ("generate_pm", "riverside"):
            raise RuntimeError("bad data")

    monkeypatch.setattr(jobs, "call_command", flaky)
    clock(utc(5, 12, 30))  # every clock past 02:30 on Oct 5
    runs = jobs.run_daily_jobs(jobs=["generate_pm", "import_openfda", "report_emails"])
    assert [(slug, job, r.status) for (job, slug, _d), r in zip(_ran(runs), runs, strict=True)] == [
        ("kona", "generate_pm", "succeeded"), ("riverside", "generate_pm", "failed"), ("thames", "generate_pm", "succeeded"),
        (None, "import_openfda", "succeeded"),
        ("kona", "report_emails", "succeeded"), ("riverside", "report_emails", "succeeded"), ("thames", "report_emails", "succeeded")]
    assert "RuntimeError: bad data" in runs[1].output
    with pytest.raises(CommandError, match=r"Failed: generate_pm \(riverside, 2026-10-05\)\."):
        call_command("run_daily_jobs", "--force", "--job", "generate_pm", stdout=StringIO())


# --- a facility whose time zone changes ------------------------------------------------------------------------------------

def _zone(facility, name):
    facility.timezone = name
    facility.save(update_fields=["timezone"])


def test_a_zone_change_mid_day_neither_repeats_nor_skips_a_day(places, clock, calls):
    riverside, kona = places["riverside"], places["kona"]
    _done(OCT4, riverside, kona)
    clock(utc(5, 6, 30))
    assert {slug for _j, slug, _d in _ran(jobs.run_daily_jobs())} == {"riverside", "thames", None}  # Riverside's Oct 5 is done

    clock(utc(5, 8))  # Riverside moves to Honolulu: 22:00 Oct 4 there, a day that already ran
    _zone(riverside, "Pacific/Honolulu")
    assert jobs.run_daily_jobs() == []
    clock(utc(5, 12, 30))  # 02:30 Oct 5 in Honolulu: Riverside's Oct 5 already ran (on New York's clock); only Kona runs
    assert {slug for _j, slug, _d in _ran(jobs.run_daily_jobs())} == {"kona"}
    clock(utc(6, 12, 30))
    assert {(slug, d) for _j, slug, d in _ran(jobs.run_daily_jobs()) if slug != "thames"} == {("riverside", "2026-10-06"), ("kona", "2026-10-06"),
                                                                                                (None, "2026-10-06")}

    clock(utc(7, 5))  # Kona moves to New York at 01:00 Oct 7 there (19:00 Oct 6 in Honolulu, a day that ran): not yet its hour
    _zone(kona, "America/New_York")
    assert [r for r in jobs.run_daily_jobs() if r.facility_id == kona.pk] == []
    clock(utc(7, 6, 30))  # 02:30 Oct 7 in New York: Kona's Oct 7 runs, nothing skipped, nothing twice
    assert {(slug, d) for _j, slug, d in _ran(jobs.run_daily_jobs()) if slug == "kona"} == {("kona", "2026-10-07")}
    theirs = [c for c in calls if c[1]]  # the facilities' runs (the import is called with no facility or day)
    assert len(theirs) == len(set(theirs))  # never a facility's day twice
    for facility, last in ((riverside, OCT6), (kona, date(2026, 10, 7))):  # and every day, one after the other
        days = list(JobRun.objects.filter(job="generate_pm", facility=facility).order_by("run_on").values_list("run_on", flat=True))
        assert days == [OCT4 + timedelta(days=n) for n in range((last - OCT4).days + 1)]


def test_a_day_a_facilitys_clock_jumped_over_runs_at_once(places, clock, calls, settings):
    """With a late daily time a move east can cross midnight before the hour: 21:00 on Oct 5 in New York is 02:00 on Oct 6 in
    London. Oct 5 never ran, so it runs at once, and Oct 6 at its hour; a facility with no runs before does not catch up."""
    settings.SCHEDULER_DAILY_AT = "22:00"
    riverside = places["riverside"]
    _done(OCT4, riverside)
    clock(utc(6, 1))  # 21:00 Oct 5 in New York: before the hour
    assert [r for r in jobs.run_daily_jobs() if r.facility_id == riverside.pk] == []
    _zone(riverside, "Europe/London")  # now 02:00 Oct 6 there
    assert _ran(jobs.run_daily_jobs(jobs=["generate_pm"])) == [("generate_pm", "riverside", "2026-10-05")]  # Thames and Kona never ran
    assert jobs.run_daily_jobs(jobs=["generate_pm"]) == []
    clock(utc(6, 21))  # 22:00 Oct 6 in London
    assert ("generate_pm", "riverside", "2026-10-06") in _ran(jobs.run_daily_jobs(jobs=["generate_pm"]))


# --- the jobs' today is the facility's ---------------------------------------------------------------------------------------

def _device(tenant, tag, next_pm_on):
    with tenant_context(tenant):
        dm = DeviceModel.objects.get_or_create(manufacturer="Zoll", model="R Series Plus", defaults={"description": "Defibrillator",
                                                                                                   "category": "Defibrillators"})[0]
        dept = Department.objects.get_or_create(name="ER")[0]
        return Asset.objects.create(tag=tag, device_model=dm, department=dept, next_pm_on=next_pm_on)


def _pm_opened(tenant):
    with tenant_context(tenant):
        return {wo.asset.tag: wo.opened_on for wo in WorkOrder.objects.filter(type=WoType.PM).select_related("asset")}


def test_generate_pm_works_from_the_facilitys_today(places, clock, settings):
    """The 21-day lead window starts at the facility's today: on Oct 4 it reaches Oct 25, on Oct 5 Oct 26."""
    settings.PM_LEAD_DAYS = 21
    riverside, kona = places["riverside"], places["kona"]
    for t in (riverside, kona):
        _device(t, "D-25", date(2026, 10, 25))
        _device(t, "D-26", date(2026, 10, 26))
    clock(utc(5, 6, 30))  # Riverside on Oct 5, Kona still on Oct 4 (past its hour, so its Oct 4 runs)
    jobs.run_daily_jobs(jobs=["generate_pm"])
    assert _pm_opened(riverside) == {"D-25": OCT5, "D-26": OCT5}
    assert _pm_opened(kona) == {"D-25": OCT4}  # opened on its day; Oct 26 is past its window yet
    clock(utc(5, 12, 30))
    jobs.run_daily_jobs(jobs=["generate_pm"])
    assert _pm_opened(kona) == {"D-25": OCT4, "D-26": OCT5}


def _person(tenant, role_slug, username):
    role = Role.unscoped.get(tenant=tenant, slug=role_slug)  # unscoped: test setup names the tenant explicitly
    return User.objects.create_user(username=username, email=username, password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=username.split("@")[0].title())


def test_report_emails_go_on_the_facilitys_monday(places, clock, mailoutbox):
    riverside, kona = places["riverside"], places["kona"]
    clock(utc(5, 6))  # Monday 02:00 in New York, Sunday 20:00 in Honolulu: each schedule starts on its facility's next Monday
    for t, who in ((riverside, "kim@riverside.example"), (kona, "lei@kona.example")):
        with tenant_context(t):
            assert subs.set_subscription(_person(t, "analyst", who), "cosr", "weekly").start_on == OCT5

    clock(utc(5, 6, 30))
    runs = jobs.run_daily_jobs(jobs=["report_emails"])
    assert _ran(runs) == [("report_emails", "kona", "2026-10-04"), ("report_emails", "riverside", "2026-10-05"),
                          ("report_emails", "thames", "2026-10-05")]
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]]  # Riverside's Monday; Kona's Sunday sends nothing
    assert "Report emails for 2026-10-04: 0 sent" in runs[0].output

    clock(utc(5, 12, 30))  # Monday 02:30 in Honolulu
    [run] = jobs.run_daily_jobs(jobs=["report_emails"])
    assert (run.facility, run.run_on) == (kona, OCT5) and "kona: 1 due, 1 sent" in run.output
    assert mailoutbox[1].to == ["lei@kona.example"] and mailoutbox[1].attachments[0][0] == "cadence-cosr-2026-10-05.csv"


def test_the_digest_lists_what_is_due_on_the_facilitys_day(places, clock, mailoutbox):
    kona = places["kona"]
    lei = _person(kona, "technician", "lei@kona.example")
    asset = _device(kona, "D-1", None)
    with tenant_context(kona):
        tech = Technician.objects.create(name="Lei", user=lei)
        NotificationPreference.objects.create(user=lei, daily_digest=True, contract_reminders=False)
        WorkOrder.objects.create(asset=asset, assigned_to=tech, type=WoType.REPAIR, due_on=OCT5, opened_on=OCT3, problem="Will not charge")
    clock(utc(5, 6, 30))  # Oct 4 in Honolulu: due tomorrow, nothing to list
    [run] = jobs.run_daily_jobs(jobs=["staff_notifications"], facilities=[kona])
    assert run.run_on == OCT4 and "1 with nothing due, not sent" in run.output and mailoutbox == []
    clock(utc(5, 12, 30))  # Oct 5 in Honolulu: due today
    [run] = jobs.run_daily_jobs(jobs=["staff_notifications"], facilities=[kona])
    assert run.run_on == OCT5 and [m.to for m in mailoutbox] == [["lei@kona.example"]]
    assert mailoutbox[0].subject.startswith("Your work for Mon, Oct 5: 1 due or overdue")


# --- the admin ------------------------------------------------------------------------------------------------------------

def test_the_admin_shows_and_filters_by_facility(client, places, django_user_model):
    _done(OCT4, places["kona"], places["riverside"])
    client.force_login(django_user_model.objects.create_superuser("root", "root@example.com", "Test-Pass-2026-x"))
    page = client.get("/admin/jobs/jobrun/").content.decode()
    assert "Kona Community" in page and "Riverside Regional" in page and "By facility" in page
    page = client.get(f"/admin/jobs/jobrun/?facility__id__exact={places['kona'].pk}").content.decode()
    assert "Kona Community" in page and "3 job runs" in page


# --- row-level security: the scheduler starts with no tenant ---------------------------------------------------------------

def test_the_due_check_and_the_runs_touch_no_facility_table_before_entering_it(rls, places, clock, mailoutbox):  # noqa: F811
    _device(places["kona"], "D-1", date(2026, 10, 10))
    clock(utc(5, 12, 30))
    with rls:
        assert jobs.is_due() is True
        runs = jobs.run_daily_jobs()
        call_command("scheduler", "--once", stdout=StringIO())  # nothing left to run, but it checks every facility
    assert rls.violations == []
    assert {r.status for r in runs} == {"succeeded"} and len(runs) == 3 * 3 + 1


@needs_postgres
def test_the_scheduler_runs_each_facility_under_the_policies_from_no_tenant(places, clock):
    """As the runtime role under row-level security, starting with no tenant (as the scheduler does): the due check reads only
    Tenant and JobRun, and each facility's run does its work inside that facility."""
    for t in (places["riverside"], places["kona"]):
        _device(t, "D-1", date(2026, 10, 10))
    clock(utc(5, 12, 30))
    as_app_role()
    with tenant_context(None):
        assert jobs.is_due() is True
        runs = jobs.run_daily_jobs()
        assert {r.status for r in runs} == {"succeeded"}, [r.output for r in runs if r.status != "succeeded"]
        assert jobs.is_due() is False
        call_command("scheduler", "--once", stdout=StringIO())
        assert len(runs) == 3 * 3 + 1 and JobRun.objects.count() == 3 * 3 + 1
    for t in (places["riverside"], places["kona"]):
        assert set(_pm_opened(t)) == {"D-1"}
