"""
Slice 28, wave 2C: the incident emails (apps/incidents/notify.py) and their preference (apps.notifications.services: `incidents`,
offered at Incidents Approve; the Notifications page and the API).

Who gets them: active people of the facility with an address, Incidents Approve and the whole facility, who keep `incidents` on. What
they say: the incident's number, the device's tag, the due date, a link naming the facility; never the outcome, who was affected, the
event report number, the device's unit or model, or anything typed. When: after commit, once per incident however often its clock
starts again ("<number>:recorded"); the daily reminders at SOON_WORK_DAYS work days left and once past due, each stage once, a missed
day catching up with the stage it is in now, one email per person; nothing once decided not reportable or reported. A failed send is
tried again; quiet() silences what is recorded inside, even when the commit comes after it ends, and nothing stays silenced. The daily
job's summary and the command count them; the job reads no facility table before entering it.
"""
import smtplib
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.management import call_command
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from test_rls_paths import rls  # noqa: F401

from apps.accounts.models import Level, Module, Role, User
from apps.core.workdays import add_work_days, is_work_day
from apps.incidents import notify
from apps.incidents import services as inc
from apps.incidents.models import SHORT_OUTCOMES, Affected, Basis, DecidedBy, Finding, Outcome
from apps.notifications import daily
from apps.notifications import services as ns
from apps.notifications.models import NotificationPreference, NotificationSent
from apps.tenants.context import tenant_context

APP = "https://ce.example.org"
DAY = timedelta(days=1)
DECIDERS = ["director@riverside.example", "manager@riverside.example"]
WORDS_NEVER = [*SHORT_OUTCOMES.values(), *(label for _v, label in Affected.choices), "SE-77", "ICU", "Hamilton", "ventilator"]


@pytest.fixture(autouse=True)
def _app_base_url(settings):
    settings.APP_BASE_URL = APP


@pytest.fixture
def people(ctx, make_user):
    """Everyone with an address; the managers' variations show who never gets one and why."""
    out = {}
    for slug in ("director", "manager", "technician", "analyst", "requester", "vendor"):
        out[slug] = make_user(slug)
    for name in ("off", "gone", "blank"):
        out[name] = make_user("manager", username=f"{name}@riverside.example")
    for user in out.values():
        user.email = user.username
    out["vendor"].company = "Acme Service"
    out["gone"].is_active = False
    out["blank"].email = ""
    for user in out.values():
        user.save()
    NotificationPreference.objects.create(user=out["off"], incidents=False)
    return out


def to(mailoutbox) -> list[str]:
    return sorted(m.to[0] for m in mailoutbox)


def sent_keys() -> list[tuple]:
    return sorted(NotificationSent.objects.filter(kind=NotificationSent.Kind.INCIDENT).values_list("user__username", "key"))


def record(asset, **kwargs):
    kwargs.setdefault("outcome", Outcome.UNKNOWN)
    kwargs.setdefault("affected", Affected.PATIENT)
    kwargs.setdefault("hold", False)
    return inc.record_incident(asset=asset, **kwargs)


def left_on(due: date, n: int) -> date:
    """The day with `n` work days left until `due` (work_days_between(day, due) == n)."""
    day, counted = due, 0
    while counted < n:
        if is_work_day(day):
            counted += 1
        day -= DAY
    return day


def _broken_mail_for(monkeypatch, address):
    real = EmailMessage.send

    def send(self, fail_silently=False):
        if address in self.to:
            raise smtplib.SMTPException("mail server unreachable")
        return real(self, fail_silently=fail_silently)

    monkeypatch.setattr(EmailMessage, "send", send)


# --- recorded --------------------------------------------------------------------------------------------------------------------

def test_recording_one_with_a_clock_emails_the_people_who_decide(vent, people, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident = record(vent, outcome=Outcome.DEATH, affected=Affected.STAFF, event_reference="SE-77", by=people["technician"])
    assert to(mailoutbox) == DECIDERS  # not Edit or View, scoped, turned off, deactivated, or without an address
    assert sent_keys() == [(u, f"{incident.number}:recorded") for u in DECIDERS]
    mail = mailoutbox[0]
    due = incident.report_due_on
    assert mail.subject == f"Incident {incident.number} on CE-10001 may need a report by {due:%a}, {due:%b} {due.day} · Riverside Regional"
    link = f"{APP}{reverse('web:incident', args=[incident.number])}?facility=riverside"
    assert link in mail.body and f"{APP}/account/notifications/?facility=riverside" in mail.body
    assert f"{incident.number} · device CE-10001" in mail.body and f"{due:%A}, {due:%B} {due.day}, {due.year} (10 work days left)" in mail.body
    assert "may need a report to the FDA or the manufacturer" in mail.body
    for words in WORDS_NEVER:
        assert words not in mail.body and words not in mail.subject, words


def test_no_clock_no_email(vent, people, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
        record(vent, outcome=Outcome.DEATH, affected=Affected.OTHER)  # a visitor: no mandatory report
        record(vent, outcome=Outcome.INJURY)
    assert mailoutbox == [] and sent_keys() == []


def test_once_per_incident_however_often_its_clock_starts(vent, pump, people, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        incident = record(vent, event_reference="SE-1")
    assert len(mailoutbox) == 2
    today = incident.aware_on
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.NO_SUGGESTION, decided_on=today, decided_by=DecidedBy.RISK, today=today)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        again = inc.record_finding(incident, Finding.DEVICE_FAILURE)  # clears the decision: the clock runs again
    assert again.decision_cleared and len(callbacks) == 1 and len(mailoutbox) == 2  # heard, never twice
    with django_capture_on_commit_callbacks(execute=True):
        quiet = record(pump, outcome=Outcome.NO_HARM)
    with django_capture_on_commit_callbacks(execute=True):
        inc.update_facts(quiet, outcome=Outcome.UNKNOWN)  # raising the outcome starts its clock: the first time for this one
    assert len(mailoutbox) == 4 and to(mailoutbox[2:]) == DECIDERS and quiet.number in mailoutbox[3].subject


def test_nothing_once_decided_or_closed_before_the_email_goes(vent, people, mailoutbox):
    incident = record(vent, event_reference="SE-1")
    inc.decide(incident, outcome=Outcome.INJURY, basis=Basis.NOT_SERIOUS, decided_on=incident.aware_on, decided_by=DecidedBy.CE,
               today=incident.aware_on)
    assert notify.recorded(incident) == 0 and mailoutbox == []


def test_a_failed_send_is_tried_again(vent, people, mailoutbox, monkeypatch):
    incident = record(vent)
    _broken_mail_for(monkeypatch, "manager@riverside.example")
    assert notify.recorded(incident) == 1 and to(mailoutbox) == ["director@riverside.example"]
    assert sent_keys() == [("director@riverside.example", f"{incident.number}:recorded")]  # the manager's claim given back
    monkeypatch.undo()
    assert notify.recorded(incident) == 1 and to(mailoutbox) == DECIDERS


def test_recorded_never_raises(vent, people, mailoutbox, monkeypatch):
    incident = record(vent)
    monkeypatch.setattr(notify, "recipients", lambda tenant: 1 / 0)
    assert notify.recorded(incident) == 0 and mailoutbox == []


def test_quiet_silences_what_is_recorded_inside_even_when_it_commits_later(vent, people, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        with notify.quiet():
            record(vent)
    assert mailoutbox == [] and sent_keys() == []
    with pytest.raises(ZeroDivisionError):
        with transaction.atomic(), notify.quiet():  # rolled back: nothing stays silenced
            1 / 0
    with django_capture_on_commit_callbacks(execute=True):
        incident = record(vent)
    assert to(mailoutbox) == DECIDERS and incident.number in mailoutbox[0].subject


# --- the daily reminders ---------------------------------------------------------------------------------------------------------

def test_reminders_at_three_work_days_and_past_due_each_once(vent, people, mailoutbox):
    incident = make_incident(vent, outcome=Outcome.SERIOUS_INJURY, hold=False, open_work_order=False)
    due = incident.report_due_on
    soon = left_on(due, inc.SOON_WORK_DAYS)
    assert notify.send_reminders(left_on(due, 4)) == {"sent": 0, "failed": 0, "incidents": 0}
    assert notify.send_reminders(soon) == {"sent": 2, "failed": 0, "incidents": 1}
    assert to(mailoutbox) == DECIDERS
    mail = mailoutbox[0]
    assert mail.subject == f"Incident {incident.number} on CE-10001: a report may be due in 3 work days · Riverside Regional"
    assert f"Due {due:%A}, {due:%B} {due.day}, {due.year} (3 work days left)" in mail.body
    assert f"{APP}{reverse('web:incident', args=[incident.number])}?facility=riverside" in mail.body
    for words in WORDS_NEVER:
        assert words not in mail.body and words not in mail.subject, words
    assert notify.send_reminders(left_on(due, 2))["sent"] == 0  # the soon stage once
    assert notify.send_reminders(due)["sent"] == 0  # due today: still the soon stage
    assert notify.send_reminders(due + DAY) == {"sent": 2, "failed": 0, "incidents": 1}
    assert mailoutbox[-1].subject == f"Incident {incident.number} on CE-10001: the report was due {due:%a}, {due:%b} {due.day} · Riverside Regional"
    assert "past due. If the device cannot be ruled out, report it." in mailoutbox[-1].body
    assert notify.send_reminders(due + 10 * DAY)["sent"] == 0  # past due once
    assert sent_keys() == sorted([(u, f"{incident.number}:soon") for u in DECIDERS] + [(u, f"{incident.number}:overdue") for u in DECIDERS])


def test_a_missed_day_catches_up_with_the_stage_it_is_in_now_and_one_email_lists_them_all(vent, pump, people, mailoutbox):
    late = make_incident(vent, outcome=Outcome.UNKNOWN, hold=False, open_work_order=False)
    day = late.report_due_on + DAY  # `late` is past due, never reminded at 3 work days; `near` has 2 work days left
    knew = left_on(day, inc.REPORT_WORK_DAYS - 2)
    near = make_incident(pump, outcome=Outcome.DEATH, hold=False, open_work_order=False, occurred_on=knew, aware_on=knew)
    assert add_work_days(knew, 10) == near.report_due_on and inc.clock(near, day).left == 2
    assert notify.send_reminders(day) == {"sent": 2, "failed": 0, "incidents": 2}
    mail = mailoutbox[0]
    assert mail.subject == "2 incidents with a report due soon or past due · Riverside Regional"
    assert mail.body.index(late.number) < mail.body.index(near.number)  # by due date
    assert sent_keys() == sorted([(u, f"{late.number}:overdue") for u in DECIDERS] + [(u, f"{near.number}:soon") for u in DECIDERS])


def test_nothing_once_decided_not_reportable_or_reported(vent, pump, people, mailoutbox):
    decided = make_incident(vent, outcome=Outcome.INJURY, hold=False, open_work_order=False, reportable=False, basis=Basis.NOT_SERIOUS)
    reported = make_incident(pump, outcome=Outcome.SERIOUS_INJURY, hold=False, open_work_order=False, reportable=True, basis=Basis.MAY_HAVE,
                             manufacturer_reported_on=timezone.localdate())
    waiting = make_incident(pump, outcome=Outcome.DEATH, hold=False, open_work_order=False, reportable=True, basis=Basis.MAY_HAVE,
                            manufacturer_reported_on=timezone.localdate())  # the FDA's report still missing
    assert decided.report_due_on is None and reported.report_due_on is not None
    day = waiting.report_due_on + DAY
    assert notify.send_reminders(day) == {"sent": 2, "failed": 0, "incidents": 1}
    assert all(waiting.number in m.subject for m in mailoutbox)


def test_a_failed_reminder_is_counted_and_tried_again(vent, people, mailoutbox, monkeypatch):
    incident = make_incident(vent, outcome=Outcome.UNKNOWN, hold=False, open_work_order=False)
    day = incident.report_due_on + DAY
    _broken_mail_for(monkeypatch, "manager@riverside.example")
    assert notify.send_reminders(day) == {"sent": 1, "failed": 1, "incidents": 1}
    monkeypatch.undo()
    assert notify.send_reminders(day) == {"sent": 1, "failed": 0, "incidents": 1} and to(mailoutbox) == DECIDERS


def test_the_daily_job_sends_and_counts_them(tenant, vent, people, mailoutbox, monkeypatch, rls):  # noqa: F811
    incident = make_incident(vent, outcome=Outcome.UNKNOWN, hold=False, open_work_order=False)
    day = left_on(incident.report_due_on, 3)
    monkeypatch.setattr(daily, "local_today", lambda: day)
    with tenant_context(None), rls:  # the job starts with no facility: it reads none of their tables before entering one
        summary = daily.send_due(day)
    assert rls.violations == []
    [river] = summary["tenants"]
    assert (river["incident_reminders"], river["incidents"], summary["sent"], summary["failed"]) == (2, 1, 2, 0)
    out = StringIO()
    with tenant_context(None):
        call_command("send_staff_notifications", stdout=out)  # nothing goes twice
    assert out.getvalue().splitlines()[0] == "riverside: nothing to send" and len(mailoutbox) == 2
    NotificationSent.objects.all().delete()
    out = StringIO()
    with tenant_context(None):
        call_command("send_staff_notifications", stdout=out)
    assert out.getvalue().splitlines()[0] == "riverside: 2 incident reminders sent (1 incident)"


def test_the_daily_job_counts_the_reminders_failing_and_keeps_the_rest(tenant, vent, people, mailoutbox, monkeypatch):
    def broken(today):
        raise RuntimeError("the incidents could not be read")

    monkeypatch.setattr(notify, "reminders_due", broken)
    summary = daily.send_due(timezone.localdate())
    [river] = summary["tenants"]
    assert (summary["failed"], river["error"], river["incident_reminders"]) == (1, "", 0) and mailoutbox == []


# --- the preference --------------------------------------------------------------------------------------------------------------

def test_offered_at_incidents_approve_and_off_means_none(vent, people, client, mailoutbox, django_capture_on_commit_callbacks):
    assert [slug for slug in ("director", "manager", "technician", "analyst") if ns.offered(people[slug], "incidents")] == ["director", "manager"]
    with pytest.raises(ValidationError) as e:
        ns.set_preferences(people["technician"], incidents=True)
    assert e.value.message_dict == {"incidents": [ns.NOT_OFFERED["incidents"]]}
    ns.set_preferences(people["manager"], incidents=False)
    assert ns.shown(people["manager"])["incidents"] is False and not ns.wants(people["manager"], "incidents")
    with django_capture_on_commit_callbacks(execute=True):
        record(vent)
    assert to(mailoutbox) == ["director@riverside.example"]
    # the page: the switch only for those offered it
    client.force_login(people["director"])
    body = client.get("/account/notifications/").content.decode()
    assert 'id="ntf-incidents"' in body and ns.LABELS["incidents"] in body and "Incident emails never say what happened" in body
    client.force_login(people["technician"])
    body = client.get("/account/notifications/").content.decode()
    assert 'id="ntf-incidents"' not in body and ns.NOT_OFFERED["incidents"] not in body
    client.force_login(people["manager"])
    r = client.post("/account/notifications/", {"incidents": ["0", "1"]}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="ntf-form")
    assert '"Incident emails on"' in r["HX-Trigger"] and ns.wants(people["manager"], "incidents")


def test_the_api_shows_and_sets_it(client, people):
    client.force_login(people["manager"])
    r = client.get("/api/v1/notification-preferences/").json()
    assert (r["incidents"], r["incidents_offered"]) == (True, True)
    r = client.put("/api/v1/notification-preferences/", {"incidents": False}, content_type="application/json")
    assert r.status_code == 200 and r.json()["incidents"] is False
    client.force_login(people["technician"])
    r = client.get("/api/v1/notification-preferences/").json()
    assert (r["incidents"], r["incidents_offered"]) == (False, False)
    r = client.put("/api/v1/notification-preferences/", {"incidents": True}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"incidents": [ns.NOT_OFFERED["incidents"]]}
    r = client.put("/api/v1/notification-preferences/", {"incidents_offered": True}, content_type="application/json")
    assert r.status_code == 400 and "incidents_offered" in r.json()


def test_someone_with_only_incidents_approve_chooses_them(client, ctx, tenant):
    role = Role.objects.create(name="Risk", slug="risk")
    role.set_levels({Module.INCIDENTS: Level.APPROVE})
    user = User.objects.create_user(username="risk@riverside.example", email="risk@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                    role=role)
    assert ns.refusal(user) is None and [k for k in ns.KINDS if ns.offered(user, k)] == ["incidents"]
    client.force_login(user)
    body = client.get("/account/notifications/").content.decode()
    assert 'id="ntf-incidents"' in body and " disabled" not in body.split('id="ntf-incidents"', 1)[1].split(">", 1)[0]
    r = client.put("/api/v1/notification-preferences/", {"incidents": False}, content_type="application/json")
    assert r.status_code == 200 and r.json()["incidents"] is False and r.json()["incidents_offered"] is True
    nobody = Role.objects.create(name="Viewer", slug="viewer")
    nobody.set_levels({Module.INCIDENTS: Level.VIEW})
    viewer = User.objects.create_user(username="view@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=nobody)
    assert "Incidents Approve" in ns.refusal(viewer)


# --- row-level security ----------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_emails_go_under_the_policies(tenant, vent, people, mailoutbox, monkeypatch):
    """As the runtime role: recorded() finds its facility from the Tenant table, then reads inside it; the daily job from no tenant."""
    incident = make_incident(vent, outcome=Outcome.UNKNOWN, hold=False, open_work_order=False)
    day = incident.report_due_on + DAY
    monkeypatch.setattr(daily, "local_today", lambda: day)
    as_app_role()
    with tenant_context(None):
        assert notify.recorded(incident) == 2
        summary = daily.send_due(day)
    assert summary["sent"] == 2 and summary["failed"] == 0 and len(mailoutbox) == 4
    with tenant_context(tenant):
        assert NotificationSent.objects.filter(kind=NotificationSent.Kind.INCIDENT).count() == 4
