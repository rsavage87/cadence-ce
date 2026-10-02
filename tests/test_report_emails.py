"""Report emails (slice 13): the Reports screen's Schedule button and modal, the subscription rules, when a report is due, the email
and its CSV attachment, the daily send across facilities (skips, failures, row-level security), the command, and the daily job."""
import json
import re
import smtplib
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.management import CommandError, call_command
from test_rls_paths import rls  # noqa: F401

from apps.accounts import services as account_services
from apps.accounts.models import Role, create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel
from apps.jobs import services as jobs
from apps.jobs.models import JobRun
from apps.reports import subscriptions as subs
from apps.reports.models import ReportSubscription
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.services import create_work_order

APP = "https://ce.example.org"
HX = {"HTTP_HX_REQUEST": "true"}
THU = date(2026, 10, 1)
MON = date(2026, 10, 5)  # the first Monday of October 2026: weekly and monthly reports are both due
MON2 = date(2026, 10, 12)  # another Monday: weekly only


@pytest.fixture(autouse=True)
def _app_base_url(settings):
    settings.APP_BASE_URL = APP


@pytest.fixture
def person(make_user):
    """A user with an email address (make_user leaves it blank)."""

    def _make(role_slug="analyst", tenant_=None, username=None, email=None):
        user = make_user(role_slug, tenant_=tenant_, username=username)
        user.email = user.username if email is None else email
        user.save(update_fields=["email"])
        return user

    return _make


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Pin the daily job's clock (subscriptions.local_today), which the command, the Schedule modal, and turning a schedule on
    (its start day) read: Thursday, October 1, 2026 unless a test moves it, so the fixed dates here hold on any real day."""

    def _at(day):
        monkeypatch.setattr(subs, "local_today", lambda: day)

    _at(THU)
    return _at


def _role(tenant, slug):
    return Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: test setup names the tenant explicitly


def _subscribe(tenant, user, key, frequency):
    with tenant_context(tenant):
        return subs.set_subscription(user, key, frequency)


def _broken_mail_for(monkeypatch, address):
    """Mail to `address` fails as an SMTP outage would; everything else goes through."""
    real = EmailMessage.send

    def send(self, fail_silently=False):
        if address in self.to:
            raise smtplib.SMTPException("mail server unreachable")
        return real(self, fail_silently=fail_silently)

    monkeypatch.setattr(EmailMessage, "send", send)


def _toast(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


# --- the model: one facility's subscriptions are invisible to another ------------------------------------------------

def test_subscriptions_are_scoped_to_their_facility(tenant, other_tenant, person):
    create_default_roles(other_tenant)
    kim = person("analyst")
    with tenant_context(tenant):
        subs.set_subscription(kim, "cosr", "monthly")
        assert ReportSubscription.objects.count() == 1 and subs.subscription_for(kim, "cosr").frequency == "monthly"
    with tenant_context(other_tenant):
        assert ReportSubscription.objects.count() == 0 and subs.subscription_for(kim, "cosr") is None
        with pytest.raises(ValidationError, match="Only people in this facility"):
            subs.set_subscription(kim, "cosr", "weekly")  # nor can a user put one in another facility
    assert ReportSubscription.unscoped.count() == 1  # unscoped: the test proves only one row exists at all


# --- set_subscription ----------------------------------------------------------------------------------------------------

def test_set_subscription_turns_on_changes_and_turns_off(ctx, person):
    kim = person("analyst")
    sub = subs.set_subscription(kim, "cosr", "weekly")
    sub.last_sent_on = MON
    sub.save()
    again = subs.set_subscription(kim, "cosr", "monthly")
    assert again.pk == sub.pk and again.frequency == "monthly" and again.last_sent_on == MON  # changing it does not resend today's
    assert ReportSubscription.objects.count() == 1
    subs.set_subscription(kim, "spend", "weekly")
    assert subs.set_subscription(kim, "cosr", None) is None and subs.subscription_for(kim, "cosr") is None
    assert subs.set_subscription(kim, "spend", "") is None and ReportSubscription.objects.count() == 0
    assert subs.set_subscription(kim, "spend", "") is None  # off when it was off already is fine


def test_set_subscription_refuses_unknown_reports_and_frequencies(ctx, person):
    kim = person("analyst")
    with pytest.raises(ValidationError, match="no such report"):
        subs.set_subscription(kim, "nope", "weekly")
    with pytest.raises(ValidationError, match="no such report"):
        subs.set_subscription(kim, "nope", None)
    with pytest.raises(ValidationError, match="Choose Off, Every Monday, or First Monday of each month"):
        subs.set_subscription(kim, "cosr", "daily")
    assert ReportSubscription.objects.count() == 0


def test_set_subscription_refuses_who_cannot_receive_it(ctx, tenant, person):
    with pytest.raises(ValidationError, match="no email address"):
        subs.set_subscription(person("analyst", email=""), "cosr", "weekly")
    with pytest.raises(ValidationError, match="Reports View"):
        subs.set_subscription(person("requester"), "cosr", "weekly")  # the requester role has no Reports access
    gone = person("technician")
    account_services.deactivate_user(gone)
    with pytest.raises(ValidationError, match="deactivated"):
        subs.set_subscription(gone, "cosr", "weekly")
    assert ReportSubscription.objects.count() == 0


def test_turning_off_works_after_access_is_lost(ctx, tenant, person):
    tom = person("technician")
    subs.set_subscription(tom, "cosr", "weekly")
    account_services.set_user_role(tom, _role(tenant, "vendor"), company="Acme Service")  # no Reports access; slice 16: with the company it needs
    tom.refresh_from_db()
    assert subs.set_subscription(tom, "cosr", None) is None and ReportSubscription.objects.count() == 0


# --- when a report is due ----------------------------------------------------------------------------------------------

def _due_days(frequency, first, last):
    """The sending days themselves (is_due_on); is_due adds catching up a missed one, tested below."""
    return [first + timedelta(days=i) for i in range((last - first).days + 1) if subs.is_due_on(frequency, first + timedelta(days=i))]


def test_weekly_is_every_monday_and_monthly_the_first_monday():
    october = (date(2026, 10, 1), date(2026, 10, 31))
    assert _due_days("weekly", *october) == [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)]
    assert _due_days("monthly", *october) == [date(2026, 10, 5)]
    assert _due_days("monthly", date(2026, 6, 1), date(2026, 6, 30)) == [date(2026, 6, 1)]  # June 2026 starts on a Monday
    assert _due_days("monthly", date(2026, 9, 1), date(2026, 9, 30)) == [date(2026, 9, 7)]  # the 7th is still the first Monday
    assert _due_days("monthly", date(2026, 11, 1), date(2026, 11, 30)) == [date(2026, 11, 2)]


def test_never_twice_on_one_day_nor_for_a_day_already_passed():
    assert not subs.is_due(ReportSubscription(frequency="weekly", last_sent_on=MON), MON)
    assert subs.is_due(ReportSubscription(frequency="weekly", last_sent_on=MON), MON2)
    assert not subs.is_due(ReportSubscription(frequency="weekly", last_sent_on=MON2), MON)  # a catch-up run for an older day


def test_the_first_email_is_today_until_todays_run_and_then_the_next_sending_day(db):
    assert subs.first_send_on("weekly", THU) == MON and subs.first_send_on("monthly", THU) == MON
    assert subs.first_send_on("monthly", date(2026, 10, 6)) == date(2026, 11, 2)
    assert subs.first_send_on("weekly", MON) == MON and subs.first_send_on("monthly", MON) == MON  # today's run has not happened
    assert subs.first_send_on("weekly", MON, last_sent_on=MON) == MON2
    JobRun.objects.create(job=subs.JOB, run_on=MON)
    assert subs.first_send_on("weekly", MON) == MON2 and subs.first_send_on("monthly", MON) == date(2026, 11, 2)


# --- the email ---------------------------------------------------------------------------------------------------------

def test_the_email_says_what_it_is_and_carries_the_screens_csv(client, ctx, tenant, other_tenant, person, vent, pump, freeze_today, mailoutbox):
    with tenant_context(other_tenant):  # another facility's device must not reach this one's report
        dept = Department.objects.create(name="Other ICU")
        model = DeviceModel.objects.create(manufacturer="Zoll", model="R Series", description="Defibrillator", category="Defibrillators")
        Asset.objects.create(tag="OT-9001", device_model=model, department=dept, acquisition_cost=1000)
    create_work_order(asset=pump, type="repair", priority="high", problem="Alarmed while on patient Jane Roe, bed 4")
    kim = person("analyst", username="kim@riverside.example")
    kim.first_name = "Kim"
    kim.save(update_fields=["first_name"])
    sub = subs.set_subscription(kim, "replace", "monthly")

    assert subs.send_report_email(sub, MON) is True
    sub.refresh_from_db()
    assert sub.last_sent_on == MON
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.to == ["kim@riverside.example"] and mail.subject == "Replacement planning · Riverside Regional · Oct 5, 2026"
    body = mail.body
    assert body.startswith("Hello Kim,") and "Riverside Regional · as of October 5, 2026" in body
    assert "Devices scoring highest on age, failures, and condition" in body
    assert "attached as cadence-replace-2026-10-05.csv (2 rows)" in body
    assert f"{APP}/print/reports/replace/" in body and f"{APP}/reports/replace/" in body
    assert all(url.startswith(APP + "/") for url in re.findall(r"https?://\S+", body))
    assert "on the first Monday of each month; the next one is due Monday, November 2" in body and "choose Schedule, then Off" in body
    assert "Jane Roe" not in body  # no free text a person typed

    filename, content, mimetype = mail.attachments[0]
    assert (filename, mimetype) == ("cadence-replace-2026-10-05.csv", "text/csv")
    client.force_login(kim)
    freeze_today(MON)
    download = b"".join(client.get("/reports/replace.csv").streaming_content)
    assert content.encode("utf-8") == download and download.startswith("﻿".encode())
    sent = mail.message().get_payload()[1].get_payload(decode=True)  # what goes over the wire, decoded
    assert sent == download
    assert "CE-10001" in content and "CE-10002" in content and "OT-9001" not in content and "Zoll" not in content and "Jane Roe" not in content


def test_send_report_email_refuses_who_may_not_receive_it_and_a_failed_send_is_not_stamped(ctx, tenant, person, mailoutbox, monkeypatch):
    tom = person("technician")
    sub = subs.set_subscription(tom, "cosr", "weekly")
    account_services.set_user_role(tom, _role(tenant, "vendor"), company="Acme Service")  # no Reports access; slice 16: with the company it needs
    sub = ReportSubscription.objects.select_related("user").get(pk=sub.pk)
    with pytest.raises(ValidationError, match="no Reports View"):
        subs.send_report_email(sub, MON)
    assert mailoutbox == []

    kim = person("analyst")
    sub = subs.set_subscription(kim, "cosr", "weekly")
    _broken_mail_for(monkeypatch, kim.email)
    assert subs.send_report_email(sub, MON) is False
    sub.refresh_from_db()
    assert sub.last_sent_on is None and mailoutbox == []


# --- the daily send ------------------------------------------------------------------------------------------------------

@pytest.fixture
def two_facilities(tenant, other_tenant, person):
    """Riverside: one analyst who receives two reports, one technician who has lost Reports View, one deactivated user, one whose
    email address was removed, and one whose mail server refuses. Other: one analyst. Closed (inactive): one analyst."""
    create_default_roles(other_tenant)
    closed = Tenant.objects.create(name="Closed Clinic", slug="closed", is_active=False)
    create_default_roles(closed)
    people = {
        "kim": person("analyst", username="kim@riverside.example"),
        "tom": person("technician", username="tom@riverside.example"),
        "dee": person("manager", username="dee@riverside.example"),
        "nia": person("director", username="nia@riverside.example"),
        "bad": person("manager", username="bad@riverside.example"),
        "oli": person("analyst", tenant_=other_tenant, username="oli@other.example"),
        "cal": person("analyst", tenant_=closed, username="cal@closed.example"),
    }
    _subscribe(tenant, people["kim"], "cosr", "weekly")
    _subscribe(tenant, people["kim"], "replace", "monthly")
    _subscribe(tenant, people["tom"], "cosr", "monthly")
    _subscribe(tenant, people["dee"], "cosr", "weekly")
    _subscribe(tenant, people["nia"], "cosr", "weekly")
    _subscribe(tenant, people["bad"], "compliance", "weekly")  # sorts before "cosr": the ones after it must still go
    _subscribe(other_tenant, people["oli"], "spend", "monthly")
    _subscribe(closed, people["cal"], "spend", "weekly")
    with tenant_context(tenant):
        account_services.set_user_role(people["tom"], _role(tenant, "vendor"), company="Acme Service")  # no Reports access; slice 16: with the company it needs
        account_services.deactivate_user(people["dee"])
    people["nia"].email = ""
    people["nia"].save(update_fields=["email"])
    return people


def _sent_to(mailoutbox):
    return sorted((m.to[0], m.attachments[0][0]) for m in mailoutbox)


def test_send_due_skips_who_may_not_receive_and_keeps_going_after_a_failure(tenant, two_facilities, mailoutbox, monkeypatch):
    _broken_mail_for(monkeypatch, "bad@riverside.example")
    summary = subs.send_due(MON)
    assert _sent_to(mailoutbox) == [("kim@riverside.example", "cadence-cosr-2026-10-05.csv"), ("kim@riverside.example", "cadence-replace-2026-10-05.csv"),
                                    ("oli@other.example", "cadence-spend-2026-10-05.csv")]
    assert (summary["sent"], summary["failed"], summary["skipped"]) == (3, 1, 3)
    by_slug = {t["slug"]: t for t in summary["tenants"]}
    assert list(by_slug) == ["other", "riverside"]  # the inactive facility is not worked through at all
    river = by_slug["riverside"]
    assert (river["due"], river["sent"], river["failed"]) == (6, 2, 1)
    assert dict(river["skipped"]) == {"no_access": 1, "inactive": 1, "no_email": 1}
    with tenant_context(tenant):
        stamped = {(s.user.username, s.report): s.last_sent_on for s in ReportSubscription.objects.select_related("user")}
    assert stamped[("kim@riverside.example", "cosr")] == MON and stamped[("bad@riverside.example", "compliance")] is None
    assert stamped[("tom@riverside.example", "cosr")] is None  # skipped, not sent

    mailoutbox.clear()
    monkeypatch.undo()
    again = subs.send_due(MON)  # a rerun the same day retries only what failed
    assert _sent_to(mailoutbox) == [("bad@riverside.example", "cadence-compliance-2026-10-05.csv")] and again["sent"] == 1


def test_a_missed_monday_is_caught_up_the_next_day_and_only_once(tenant, two_facilities, mailoutbox):
    """Monday's run failed or never happened: Tuesday's run sends Monday's emails, Wednesday's sends nothing, the next Monday
    sends the weekly ones only."""
    caught_up = subs.send_due(date(2026, 10, 6))
    assert caught_up["sent"] == 4 and caught_up["failed"] == 0  # kim cosr and replace, nia cosr, bad compliance; the rest skipped
    assert subs.send_due(date(2026, 10, 7))["sent"] == 0
    mailoutbox.clear()
    summary = subs.send_due(MON2)  # weekly only
    assert _sent_to(mailoutbox) == [("bad@riverside.example", "cadence-compliance-2026-10-12.csv"), ("kim@riverside.example", "cadence-cosr-2026-10-12.csv")]
    assert summary["failed"] == 0


def test_a_report_that_cannot_be_computed_fails_its_emails_only(tenant, two_facilities, mailoutbox, monkeypatch):
    real = subs.run_any  # slice 18: every report, standard or custom, runs through run_any

    def run_any(key, today=None):
        if key == "cosr":
            raise ZeroDivisionError("bad data")
        return real(key, today)

    monkeypatch.setattr(subs, "run_any", run_any)
    summary = subs.send_due(MON)
    assert summary["failed"] == 1 and summary["sent"] == 3
    assert ("kim@riverside.example", "cadence-replace-2026-10-05.csv") in _sent_to(mailoutbox)


# --- the command and the daily job ------------------------------------------------------------------------------------

def test_the_command_prints_a_line_per_facility_and_the_totals(two_facilities, clock, mailoutbox):
    clock(date(2026, 10, 7))
    out = StringIO()
    call_command("send_report_emails", "--date", "2026-10-05", stdout=out)
    lines = out.getvalue().splitlines()
    assert lines == [
        "other: 1 due, 1 sent",
        "riverside: 6 due, 3 sent, 3 skipped (1 account deactivated, 1 no Reports View, 1 no email address)",
        "Report emails for 2026-10-05: 4 sent, 0 failed, 3 skipped",
    ]
    out = StringIO()
    call_command("send_report_emails", "--date", "2026-10-05", stdout=out)  # nothing is sent twice; the skipped stay skipped
    assert out.getvalue().splitlines() == [
        "other: nothing due",
        "riverside: 3 due, 0 sent, 3 skipped (1 account deactivated, 1 no Reports View, 1 no email address)",
        "Report emails for 2026-10-05: 0 sent, 0 failed, 3 skipped",
    ]
    assert len(mailoutbox) == 4


def test_the_command_sends_for_today_by_default(tenant, person, clock, mailoutbox):
    _subscribe(tenant, person("analyst"), "cosr", "weekly")
    clock(MON2)
    out = StringIO()
    call_command("send_report_emails", stdout=out)
    assert "Report emails for 2026-10-12: 1 sent" in out.getvalue() and mailoutbox[0].attachments[0][0] == "cadence-cosr-2026-10-12.csv"


def test_the_command_refuses_a_bad_or_future_date(db, clock):
    clock(MON)
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        call_command("send_report_emails", "--date", "05/10/2026", stdout=StringIO())
    with pytest.raises(CommandError, match="future"):
        call_command("send_report_emails", "--date", "2026-10-06", stdout=StringIO())


def test_the_command_fails_after_trying_every_email(two_facilities, clock, mailoutbox, monkeypatch):
    clock(MON)
    _broken_mail_for(monkeypatch, "bad@riverside.example")
    out = StringIO()
    with pytest.raises(CommandError, match=r"1 report email could not be sent \(riverside\)"):
        call_command("send_report_emails", stdout=out)
    assert "riverside: 6 due, 2 sent, 3 skipped" in out.getvalue() and "1 failed" in out.getvalue()
    assert len(mailoutbox) == 3  # everyone else still got theirs


def test_a_facility_that_cannot_be_worked_through_does_not_stop_the_next(two_facilities, clock, mailoutbox, monkeypatch):
    clock(MON)
    real = subs._send_facility

    def send_facility(tenant, today, counts):
        if tenant.slug == "other":
            raise RuntimeError("database went away")
        return real(tenant, today, counts)

    monkeypatch.setattr(subs, "_send_facility", send_facility)
    out = StringIO()
    with pytest.raises(CommandError, match=r"could not be sent \(other\)"):
        call_command("send_report_emails", stdout=out)
    assert "other: failed: RuntimeError: database went away" in out.getvalue() and "riverside: 6 due, 3 sent" in out.getvalue()
    assert len(mailoutbox) == 3


def test_the_daily_job_runs_the_command_once_a_day(tenant, person, clock, mailoutbox, monkeypatch):
    _subscribe(tenant, person("analyst"), "cosr", "weekly")
    clock(MON)
    runs = jobs.run_daily_jobs(day=MON, jobs=["report_emails"])
    assert [(r.job, r.status) for r in runs] == [("report_emails", "succeeded")]
    assert "riverside: 1 due, 1 sent" in runs[0].output and len(mailoutbox) == 1
    assert jobs.run_daily_jobs(day=MON, jobs=["report_emails"]) == []  # the day's run is the lock


def test_the_daily_job_is_recorded_as_failed_when_an_email_fails(tenant, person, clock, mailoutbox, monkeypatch):
    kim = person("analyst")
    _subscribe(tenant, kim, "cosr", "weekly")
    clock(MON)
    _broken_mail_for(monkeypatch, kim.email)
    runs = jobs.run_daily_jobs(day=MON, jobs=["report_emails"])
    assert runs[0].status == "failed" and "1 report email could not be sent" in runs[0].output


# --- row-level security: the job starts with no tenant -----------------------------------------------------------------

def test_send_due_and_the_command_touch_no_facility_table_before_entering_it(rls, two_facilities, clock, mailoutbox):  # noqa: F811
    clock(MON)
    with rls:
        summary = subs.send_due(MON)
        call_command("send_report_emails", stdout=StringIO())  # nothing left to send, but it reads every facility's subscriptions
        jobs.run_daily_jobs(day=MON, jobs=["report_emails"])
    assert rls.violations == []
    assert summary["sent"] == 4 and len(mailoutbox) == 4


# --- the Schedule button and modal -------------------------------------------------------------------------------------

def test_the_report_panel_has_a_schedule_button_for_who_can_use_it(client, ctx, person, make_user):
    client.force_login(person("analyst"))
    body = client.get("/reports/cosr/").content.decode()
    assert 'id="rep-schedule-cosr"' in body and 'hx-get="/reports/cosr/schedule/" hx-target="#modal-card"' in body and ">Schedule</button>" in body
    client.force_login(make_user("technician"))  # Reports View but no email address: nowhere to send it
    body = client.get("/reports/cosr/").content.decode()
    assert "/reports/cosr/schedule/" not in body and "rep-schedule" not in body


def test_the_schedule_modal_offers_off_weekly_and_monthly_with_their_first_dates(client, ctx, person, clock):
    kim = person("analyst", username="kim@riverside.example")
    client.force_login(kim)
    clock(THU)
    r = client.get("/reports/cosr/schedule/", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>Email this report</h2>" in body and "Cost of service ratio by category" in body
    assert 'name="frequency" value="" checked' in body
    for label in ("Off", "Every Monday", "First Monday of each month"):
        assert f"<span>{label}<small>" in body
    assert body.count("First email Monday, October 5, 2026") == 2
    assert "To kim@riverside.example, only while you can view Reports." in body
    assert 'hx-post="/reports/cosr/schedule/" hx-target="#modal-card"' in body
    direct = client.get("/reports/cosr/schedule/")  # only ever a modal
    assert direct.status_code == 302 and direct["Location"] == "/reports/cosr/"
    assert client.get("/reports/nope/schedule/", **HX).status_code == 404


def test_saving_schedules_the_report_and_updates_the_button(client, ctx, person, clock):
    kim = person("analyst", username="kim@riverside.example")
    client.force_login(kim)
    clock(THU)
    r = client.post("/reports/cosr/schedule/", {"frequency": "monthly"}, **HX)
    assert r.status_code == 200
    assert _toast(r) == "Scheduled: Cost of service ratio by category, first Monday of each month to kim@riverside.example"
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    body = r.content.decode()
    assert 'id="rep-schedule-cosr" hx-swap-oob="true"' in body and ">Scheduled monthly</button>" in body and "sched-on" in body
    assert subs.subscription_for(kim, "cosr").frequency == "monthly"

    page = client.get("/reports/cosr/").content.decode()
    assert ">Scheduled monthly</button>" in page and "hx-swap-oob" not in page
    modal = client.get("/reports/cosr/schedule/", **HX).content.decode()
    assert 'value="monthly" checked' in modal and "Next email Monday, October 5, 2026" in modal

    r = client.post("/reports/cosr/schedule/", {"frequency": "weekly"}, **HX)
    assert _toast(r) == "Scheduled: Cost of service ratio by category, every Monday to kim@riverside.example"
    assert ">Scheduled weekly</button>" in r.content.decode()

    r = client.post("/reports/cosr/schedule/", {"frequency": ""}, **HX)
    assert _toast(r) == "Cost of service ratio by category will no longer be emailed"
    assert ">Schedule</button>" in r.content.decode() and subs.subscription_for(kim, "cosr") is None
    r = client.post("/reports/cosr/schedule/", {"frequency": ""}, **HX)
    assert _toast(r) == "Cost of service ratio by category is not scheduled"


def test_a_bad_choice_keeps_the_modal_open_with_the_reason(client, ctx, person, clock):
    kim = person("analyst")
    client.force_login(kim)
    clock(THU)
    r = client.post("/reports/cosr/schedule/", {"frequency": "daily"}, **HX)
    assert r.status_code == 200 and "Choose Off, Every Monday, or First Monday of each month." in r.content.decode()
    assert "HX-Trigger" not in r and subs.subscription_for(kim, "cosr") is None


def test_the_schedule_needs_reports_view_and_an_email_address(client, ctx, person, make_user, clock):
    clock(THU)
    client.force_login(person("requester"))  # no Reports access at all
    assert client.get("/reports/cosr/schedule/", **HX).status_code == 403
    assert client.post("/reports/cosr/schedule/", {"frequency": "weekly"}, **HX).status_code == 403
    tech = make_user("technician")  # Reports View, no email address
    client.force_login(tech)
    body = client.get("/reports/cosr/schedule/", **HX).content.decode()
    assert "Your account has no email address" in body and 'name="frequency"' not in body
    r = client.post("/reports/cosr/schedule/", {"frequency": "weekly"}, **HX)
    assert r.status_code == 200 and "HX-Trigger" not in r and ReportSubscription.objects.count() == 0


# --- review fixes -------------------------------------------------------------------------------------------

def test_a_schedule_turned_on_after_todays_run_starts_next_time(tenant, person, mailoutbox, clock):
    """Off and on again on a Monday after its run (or on for the first time then) waits for the next sending day: a same-day
    rerun of the job must not send the report a second time, nor send one the user was told comes next week."""
    kim = person("analyst", username="kim@riverside.example")
    clock(MON)
    _subscribe(tenant, kim, "cosr", "weekly")
    assert subs.send_due(MON)["sent"] == 1
    JobRun.objects.create(job=subs.JOB, run_on=MON)
    _subscribe(tenant, kim, "cosr", None)
    _subscribe(tenant, kim, "cosr", "weekly")
    assert subs.send_due(MON)["sent"] == 0 and subs.send_due(date(2026, 10, 6))["sent"] == 0 and len(mailoutbox) == 1
    assert subs.send_due(MON2)["sent"] == 1


def test_two_runs_at_once_send_each_email_once(tenant, person, mailoutbox, monkeypatch):
    """The scheduler and an operator's run both load Kim's subscription before either sends: only the first claim sends."""
    kim = person("analyst", username="kim@riverside.example")
    _subscribe(tenant, kim, "cosr", "weekly")
    with tenant_context(tenant):
        stale = ReportSubscription.objects.select_related("user").get(user=kim)  # what the second run loaded
        assert subs.send_report_email(stale, MON) is True
        with pytest.raises(subs.AlreadySent):
            subs._send(stale, tenant, MON, subs.run_any("cosr", MON))
    assert len(mailoutbox) == 1
