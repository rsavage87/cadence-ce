"""The daily emails to staff (slice 20, part D, apps/notifications/daily.py): who gets the daily digest and the contract reminders,
what they list, the reminder stages and their catch-up, never twice (a rerun, two runs at once), a failed send retried by the next
run, facilities kept apart and inactive ones skipped, the command's summary, the daily job, never the free text a requester typed,
and row-level security (the job starts with no tenant)."""
import smtplib
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.mail import EmailMessage
from django.core.management import CommandError, call_command
from pg_helpers import as_app_role, needs_postgres
from test_rls_paths import rls  # noqa: F401

from apps.accounts.models import Level, Role, User, create_default_roles
from apps.contracts.models import Contract
from apps.contracts.services import renew_contract
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.jobs import services as jobs
from apps.notifications import daily
from apps.notifications.models import NotificationPreference, NotificationSent
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType

APP = "https://ce.example.org"
MON = date(2026, 10, 5)
PROBLEM = "Patient Jane Roe in bed 4: alarm keeps sounding"  # free text a requester typed: never in an email
REQUESTER = "RN Pat Quill"
LOCATION = "Room 4B, bed 2"
NOTES = "Call Bob Vance at home, 555-0101"  # a contract's notes: never in an email


@pytest.fixture(autouse=True)
def _app_base_url(settings):
    settings.APP_BASE_URL = APP


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Pin the job's clock (daily.local_today), which the command reads: Monday, October 5, 2026 unless a test moves it."""

    def _at(day):
        monkeypatch.setattr(daily, "local_today", lambda: day)

    _at(MON)
    return _at


def _role(tenant, slug):
    return Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: test setup names the tenant explicitly


def _person(tenant, slug, username, email=None, **extra):
    return User.objects.create_user(username=username, email=username if email is None else email, password="Test-Pass-2026-x",
                                    tenant=tenant, role=_role(tenant, slug), first_name=username.split("@")[0].title(), **extra)


def _prefer(tenant, user, **choices):
    with tenant_context(tenant):
        NotificationPreference.objects.update_or_create(user=user, defaults=choices)


def _broken_mail_for(monkeypatch, address):
    """Mail to `address` fails as an SMTP outage would; everything else goes through."""
    real = EmailMessage.send

    def send(self, fail_silently=False):
        if address in self.to:
            raise smtplib.SMTPException("mail server unreachable")
        return real(self, fail_silently=fail_silently)

    monkeypatch.setattr(EmailMessage, "send", send)


def _to(mailoutbox, address):
    return [m for m in mailoutbox if m.to == [address]]


def _sent(tenant, kind=None):
    with tenant_context(tenant):
        rows = NotificationSent.objects.filter(kind=kind) if kind else NotificationSent.objects.all()
        return sorted((r.user.username, r.kind, r.key) for r in rows.select_related("user"))


@pytest.fixture
def floor(tenant):
    """Riverside: an ICU with a ventilator and a pump, and two devices of a monitor model."""
    with tenant_context(tenant):
        icu = Department.objects.create(name="ICU")
        vent_model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators")
        pump_model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        return {
            "icu": icu,
            "vent": Asset.objects.create(tag="CE-10001", device_model=vent_model, department=icu, room="4"),
            "pump": Asset.objects.create(tag="CE-10002", device_model=pump_model, department=icu),
            "pump_model": pump_model, "vent_model": vent_model,
        }


def _wo(asset, tech, due_on, type=WoType.REPAIR, priority=Priority.NORMAL, status=WoStatus.OPEN):
    """Test setup straight on the model (no service, so no other email is sent), with the free text a requester types."""
    return WorkOrder.objects.create(asset=asset, assigned_to=tech, type=type, priority=priority, status=status, due_on=due_on,
                                    opened_on=MON - timedelta(days=10), problem=PROBLEM, requester=REQUESTER, reported_location=LOCATION)


@pytest.fixture
def crew(tenant, floor):
    """Dana (technician, digest on) has work; the rest show who does not get a digest and why."""
    people = {
        "dana": _person(tenant, "technician", "dana@riverside.example"),
        "tom": _person(tenant, "technician", "tom@riverside.example"),  # digest off (the default)
        "sam": _person(tenant, "technician", "sam@riverside.example"),  # digest on, no technician record
        "nob": _person(tenant, "technician", "nob@riverside.example", email=""),  # digest on, no address
        "dee": _person(tenant, "technician", "dee@riverside.example", is_active=False),  # digest on, deactivated
        "vic": _person(tenant, "vendor", "vic@riverside.example", company="Acme Service"),  # digest on, scoped
        "ann": _person(tenant, "analyst", "ann@riverside.example"),  # digest on, a technician record, Work orders View: has work
        "req": _person(tenant, "requester", "req@riverside.example"),  # never chose it
    }
    with tenant_context(tenant):
        techs = {key: Technician.objects.create(name=key.title(), user=people[key]) for key in ("dana", "tom", "nob", "dee", "vic", "ann")}
        techs["gone"] = Technician.objects.create(name="Old record", user=None, is_active=False)
        vent, pump = floor["vent"], floor["pump"]
        wos = {
            "late": _wo(vent, techs["dana"], MON - timedelta(days=2), priority=Priority.HIGH, status=WoStatus.IN_PROGRESS),
            "today_pm": _wo(pump, techs["dana"], MON, type=WoType.PM),
            "soon_pm": _wo(vent, techs["dana"], MON + timedelta(days=3), type=WoType.PM, status=WoStatus.AWAITING_PARTS),
            "week_end_pm": _wo(pump, techs["dana"], MON + timedelta(days=7), type=WoType.PM),
            "far_pm": _wo(pump, techs["dana"], MON + timedelta(days=8), type=WoType.PM),  # past the seven days
            "soon_repair": _wo(pump, techs["dana"], MON + timedelta(days=1)),  # not due yet, and not a PM
            "done": _wo(vent, techs["dana"], MON - timedelta(days=5), status=WoStatus.COMPLETED),
            "toms": _wo(vent, techs["tom"], MON),
            "vics": _wo(vent, techs["vic"], MON),
            "anns": _wo(pump, techs["ann"], MON - timedelta(days=1)),
            "nobs": _wo(pump, techs["nob"], MON),
            "dees": _wo(pump, techs["dee"], MON),
        }
    for key in ("dana", "sam", "nob", "dee", "vic", "ann"):
        _prefer(tenant, people[key], daily_digest=True, contract_reminders=False)
    _prefer(tenant, people["tom"], daily_digest=False, contract_reminders=False)
    with tenant_context(tenant):
        Role.objects.filter(slug="analyst").get().set_levels({"workorders": Level.NONE})  # Ann's role loses Work orders View
    return {"people": people, "techs": techs, "wos": wos}


def _contract(reference, end_on, assets=(), vendor="Philips", notes=NOTES):
    """Test setup: a contract covering `assets` (retired ones too, which the contract services would refuse)."""
    c = Contract.objects.create(reference=reference, vendor=vendor, start_on=end_on - timedelta(days=365), end_on=end_on, notes=notes)
    for asset in assets:
        asset.contract = c
        asset.save()
    return c


@pytest.fixture
def contracts(tenant, floor):
    """Contracts at each stage on Monday, and ones that get no reminder."""
    with tenant_context(tenant):
        dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Monitoring")
        icu = floor["icu"]

        def devices(prefix, n, status=AssetStatus.IN_SERVICE, model=dm):
            return [Asset.objects.create(tag=f"{prefix}-{i}", device_model=model, department=icu, status=status) for i in range(n)]

        return {
            "c90": _contract("SC-90", MON + timedelta(days=60), devices("M90", 2) + devices("P90", 1, model=floor["pump_model"])),
            "c30": _contract("SC-30", MON + timedelta(days=28), devices("M30", 1)),  # never reminded at 90 or 30: the 30-day one
            "c7": _contract("SC-07", MON + timedelta(days=7), devices("M07", 1, status=AssetStatus.IN_REPAIR)),
            "ended": _contract("SC-END", MON - timedelta(days=1), devices("MEN", 1)),
            "far": _contract("SC-FAR", MON + timedelta(days=91), devices("MFA", 1)),
            "long_ended": _contract("SC-OLD", MON - timedelta(days=8), devices("MOL", 1)),
            "retired": _contract("SC-RET", MON + timedelta(days=20), devices("MRE", 2, status=AssetStatus.RETIRED)),
            "empty": _contract("SC-NIL", MON + timedelta(days=5)),
        }


@pytest.fixture
def editors(tenant):
    """Who may have contract reminders: Kim (manager, Contracts Edit, the default on), Nia (director, turned them off), Ann
    (analyst, Contracts View only), Uma (a unit lead: Contracts Edit but sees only her unit), Ned (Contracts Edit, no address)."""
    with tenant_context(tenant):
        unit_lead = Role.objects.create(name="Unit lead", slug="unit-lead", scope="department")
        unit_lead.set_levels({"contracts": Level.EDIT, "workorders": Level.VIEW})
    people = {
        "kim": _person(tenant, "manager", "kim@riverside.example"),
        "nia": _person(tenant, "director", "nia@riverside.example"),
        "ann": _person(tenant, "analyst", "ann2@riverside.example"),
        "uma": _person(tenant, "unit-lead", "uma@riverside.example", department="ICU"),
        "ned": _person(tenant, "manager", "ned@riverside.example", email=""),
    }
    _prefer(tenant, people["nia"], contract_reminders=False)
    return people


# --- the daily digest ---------------------------------------------------------------------------------------------------

def test_the_digest_goes_to_technicians_who_chose_it_and_says_why_not_to_the_rest(tenant, crew, mailoutbox):
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert [m.to for m in mailoutbox] == [["dana@riverside.example"]]
    [river] = summary["tenants"]
    assert (river["digests"], river["quiet"], river["failed"]) == (1, 0, 0)
    # Dee (deactivated) is never emailed nor counted; Tom and the requester did not choose it.
    assert dict(river["skipped"]) == {("digest", "no_technician"): 1, ("digest", "no_email"): 1, ("digest", "scoped"): 1,
                                      ("digest", "no_access"): 1}
    assert _sent(tenant) == [("dana@riverside.example", "digest", "2026-10-05")]


def test_the_digest_lists_due_or_overdue_work_and_the_weeks_pms(tenant, crew, mailoutbox):
    mailoutbox.clear()
    daily.send_due(MON)
    [mail] = mailoutbox
    wos = {key: wo.number for key, wo in crew["wos"].items()}
    assert mail.subject == "Your work for Mon, Oct 5: 2 due or overdue, 2 PMs in the next 7 days · Riverside Regional"
    body = mail.body
    assert "Your work at Riverside Regional for Monday, October 5, 2026." in body
    assert (f"DUE TODAY OR OVERDUE (2)\n\n{wos['late']} · Corrective repair · High priority · In progress\nCE-10001 · ICU ventilator · ICU\n"
            f"Overdue 2 days: it was due Oct 3\n{APP}/work-orders/{wos['late']}/\n") in body
    assert f"{wos['today_pm']} · Preventive maintenance · Normal priority · Open\nCE-10002 · Infusion pump · ICU\nDue today\n" in body
    assert "PMS DUE IN THE NEXT 7 DAYS (2)" in body
    assert f"{wos['soon_pm']} · Normal priority · Awaiting parts\nCE-10001 · ICU ventilator · ICU\nDue Thursday, Oct 8\n" in body
    assert f"{wos['week_end_pm']} · Normal priority · Open" in body
    for key in ("far_pm", "soon_repair", "done", "toms"):
        assert wos[key] not in body, key
    assert f"{APP}/work-orders/?assigned={crew['techs']['dana'].id}" in body and f"{APP}/account/notifications/" in body
    assert body.index(wos["late"]) < body.index(wos["today_pm"])  # high priority first


def test_the_digest_never_repeats_what_a_requester_typed(tenant, crew, contracts, editors, mailoutbox):
    mailoutbox.clear()
    daily.send_due(MON)
    assert len(mailoutbox) == 2  # Dana's digest, Kim's reminders
    for mail in mailoutbox:
        for text in (PROBLEM, "Jane Roe", REQUESTER, LOCATION, NOTES, "555-0101"):
            assert text not in mail.subject and text not in mail.body, (mail.to, text)


def test_a_long_list_says_how_many_more(tenant, crew, monkeypatch, mailoutbox):
    monkeypatch.setattr(daily, "LIST_CAP", 1)
    mailoutbox.clear()
    daily.send_due(MON)
    body = mailoutbox[0].body
    assert "DUE TODAY OR OVERDUE (2)" in body and body.count("And 1 more.") == 2
    assert crew["wos"]["today_pm"].number not in body.split("PMS DUE")[0]  # only the first of the two due is listed


def test_a_day_with_nothing_to_list_sends_no_digest(tenant, crew, mailoutbox):
    with tenant_context(tenant):
        WorkOrder.objects.filter(assigned_to=crew["techs"]["dana"]).update(status=WoStatus.COMPLETED)
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert mailoutbox == [] and summary["tenants"][0]["quiet"] == 1 and _sent(tenant) == []


def test_an_inactive_technician_record_gets_no_digest(tenant, crew, mailoutbox):
    with tenant_context(tenant):
        Technician.objects.filter(pk=crew["techs"]["dana"].pk).update(is_active=False)
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert mailoutbox == [] and summary["tenants"][0]["skipped"][("digest", "no_technician")] == 2


def test_turning_the_digest_off_stops_it(tenant, crew, mailoutbox):
    _prefer(tenant, crew["people"]["dana"], daily_digest=False)
    mailoutbox.clear()
    assert daily.send_due(MON)["sent"] == 0 and mailoutbox == []


def test_one_digest_a_day(tenant, crew, mailoutbox):
    mailoutbox.clear()
    assert daily.send_due(MON)["sent"] == 1
    again = daily.send_due(MON)
    assert again["sent"] == 0 and again["failed"] == 0 and len(mailoutbox) == 1
    assert daily.send_due(MON + timedelta(days=1))["sent"] == 1 and len(mailoutbox) == 2  # the next day, a new one


# --- contract reminders ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("left, stage", [(120, None), (91, None), (90, "90"), (31, "90"), (30, "30"), (8, "30"), (7, "7"), (1, "7"),
                                         (0, "7"), (-1, "ended"), (-7, "ended"), (-8, None)])
def test_the_reminder_stages(left, stage):
    assert daily.reminder_stage(MON + timedelta(days=left), MON) == stage


def test_reminders_go_to_who_can_edit_contracts_one_email_listing_each_contract(tenant, contracts, editors, mailoutbox):
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]]  # Nia turned them off, Ann cannot edit contracts
    [river] = summary["tenants"]
    assert (river["reminders"], river["reminded"], river["contracts"]) == (1, 4, 4)
    assert dict(river["skipped"]) == {("contract", "scoped"): 1, ("contract", "no_email"): 1}
    c = contracts
    assert _sent(tenant) == sorted(("kim@riverside.example", "contract", f"{c[k].id}:{c[k].end_on.isoformat()}:{stage}")
                                   for k, stage in [("c90", "90"), ("c30", "30"), ("c7", "7"), ("ended", "ended")])
    mail = mailoutbox[0]
    assert mail.subject == "4 service contracts ending or just ended · Riverside Regional"
    body = mail.body
    assert body.index("SC-END ·") < body.index("SC-07 ·") < body.index("SC-30 ·") < body.index("SC-90 ·")  # by end date
    assert ("SC-90 · Philips · OEM, Full service\nEnds Friday, December 4, 2026 (in 60 days)\n"
            "Devices covered: 3 (Philips IntelliVue MX750: 2, BD Alaris 8015 PCU: 1)\n" f"{APP}/contracts/{c['c90'].id}/\n") in body
    assert "SC-07 · Philips · OEM, Full service\nEnds Monday, October 12, 2026 (in 7 days)\nDevices covered: 1" in body  # in repair: in use
    assert "SC-END · Philips · OEM, Full service\nEnded Sunday, October 4, 2026 (yesterday)" in body
    for ref in ("SC-FAR", "SC-OLD", "SC-RET", "SC-NIL"):
        assert ref not in body, ref
    assert "reminds the people who can edit contracts 90, 30, and 7 days before a contract ends" in body
    assert f"{APP}/contracts/\n" in body and f"{APP}/account/notifications/" in body


def test_one_contract_says_so_in_the_subject(tenant, floor, editors, mailoutbox):
    with tenant_context(tenant):
        _contract("SC-2026-118", MON + timedelta(days=30), [floor["vent"]])
    mailoutbox.clear()
    daily.send_due(MON)
    assert mailoutbox[0].subject == "Service contract SC-2026-118 ends in 30 days · Riverside Regional"
    assert "This service contract at Riverside Regional is ending, or just ended, and still covers devices in use." in mailoutbox[0].body


def test_each_stage_is_reminded_once_and_a_missed_day_catches_up_with_the_current_one(tenant, floor, editors, clock, mailoutbox):
    with tenant_context(tenant):
        c = _contract("SC-1", MON + timedelta(days=28), [floor["vent"]])
    mailoutbox.clear()

    def run(day):
        clock(day)
        mailoutbox.clear()
        daily.send_due(day)
        return [m.subject.split(" · ")[0] for m in _to(mailoutbox, "kim@riverside.example")]

    assert run(MON) == ["Service contract SC-1 ends in 28 days"]  # found at 28 days: the 30-day reminder, not the 90-day one too
    assert run(MON + timedelta(days=1)) == []
    assert run(MON + timedelta(days=20)) == []  # 8 days left: still the 30-day stage, already reminded
    assert run(MON + timedelta(days=23)) == ["Service contract SC-1 ends in 5 days"]  # the 7-day run was missed: caught up, once
    assert run(MON + timedelta(days=24)) == []
    assert run(MON + timedelta(days=31)) == ["Service contract SC-1 ended 3 days ago"]
    assert run(MON + timedelta(days=32)) == [] and run(MON + timedelta(days=40)) == []
    with tenant_context(tenant):
        keys = sorted(NotificationSent.objects.filter(kind="contract").values_list("key", flat=True))
    assert keys == sorted(f"{c.id}:{c.end_on.isoformat()}:{stage}" for stage in ("30", "7", "ended"))


def test_a_renewed_contract_is_reminded_again_before_its_new_end(tenant, floor, editors, clock, mailoutbox):
    with tenant_context(tenant):
        c = _contract("SC-1", MON + timedelta(days=20), [floor["vent"]])
    mailoutbox.clear()
    assert daily.send_due(MON)["sent"] == 1
    with tenant_context(tenant):
        renew_contract(c, today=MON)
        c.refresh_from_db()
    assert daily.send_due(MON)["sent"] == 0  # a year from now: nothing yet
    later = c.end_on - timedelta(days=30)
    clock(later)
    mailoutbox.clear()
    assert daily.send_due(later)["sent"] == 1 and "SC-1 ends in 30 days" in mailoutbox[0].subject


def test_reminders_are_per_person(tenant, floor, editors, mailoutbox):
    """Kim had the 30-day reminder; a colleague who gains Contracts Edit later still gets it."""
    with tenant_context(tenant):
        _contract("SC-1", MON + timedelta(days=25), [floor["vent"]])
    daily.send_due(MON)
    _prefer(tenant, editors["nia"], contract_reminders=True)
    mailoutbox.clear()
    assert daily.send_due(MON)["sent"] == 1 and [m.to for m in mailoutbox] == [["nia@riverside.example"]]


def test_contracts_edit_without_work_orders_view_turns_reminders_off_and_gets_none(client, tenant, floor, mailoutbox):
    """Contract reminders go to whoever has Contracts Edit, Work orders View or not; such a person may now open the Notifications page
    (apps.notifications.services.refusal), and once they turn the reminders off there, the job sends them none."""
    with tenant_context(tenant):
        clerk = Role.objects.create(name="Contracts clerk", slug="contracts-clerk")
        clerk.set_levels({"contracts": Level.EDIT})
        _contract("SC-1", MON + timedelta(days=25), [floor["vent"]])
    cora = _person(tenant, "contracts-clerk", "cora@riverside.example")  # keeps the default: on
    otto = _person(tenant, "contracts-clerk", "otto@riverside.example")
    client.force_login(otto)
    r = client.post("/account/notifications/", {"contract_reminders": "0"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="ntf-form")
    assert r.status_code == 200
    with tenant_context(tenant):
        assert NotificationPreference.objects.get(user=otto).contract_reminders is False
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert [m.to for m in mailoutbox] == [[cora.email]] and _to(mailoutbox, otto.email) == []
    [river] = summary["tenants"]
    assert river["reminders"] == 1 and not river["skipped"] and river["digests"] == 0  # no digest: neither has Work orders View
    assert [row[0] for row in _sent(tenant)] == [cora.username]
    client.post("/account/notifications/", {"contract_reminders": "1"}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="ntf-form")  # back on
    mailoutbox.clear()
    assert daily.send_due(MON)["sent"] == 1 and [m.to for m in mailoutbox] == [[otto.email]]  # the stage they have not had yet


# --- never twice, and a failure retried ---------------------------------------------------------------------------------

def test_two_runs_at_once_send_each_email_once(tenant, crew, contracts, editors, mailoutbox, monkeypatch):
    """While the first run is sending Dana's digest, a second run starts: it finds every email the first has claimed, and the
    ones the first has not reached yet it sends itself; nothing goes twice."""
    real = daily.emails.send
    started, nested = [], []

    def send(to, template, context, attachments=None):
        if not started:
            started.append(to)
            nested.append(daily.send_due(MON))
        return real(to, template, context, attachments)

    mailoutbox.clear()
    monkeypatch.setattr(daily.emails, "send", send)
    first = daily.send_due(MON)
    assert sorted(m.to[0] for m in mailoutbox) == ["dana@riverside.example", "kim@riverside.example"]
    assert started == ["dana@riverside.example"]  # the first run held Dana's digest: the second skipped it and sent Kim's reminder
    assert (first["sent"], nested[0]["sent"]) == (1, 1) and first["failed"] == nested[0]["failed"] == 0


def test_a_claim_lost_to_another_run_sends_nothing(tenant, crew, contracts, editors, mailoutbox, monkeypatch):
    """Both runs read what was sent before either claimed: the unique constraint is the lock, and the loser sends nothing."""
    mailoutbox.clear()
    daily.send_due(MON)
    assert len(mailoutbox) == 2
    real_filter = NotificationSent.objects.filter

    def stale(*args, **kwargs):  # the second run's reads, made before the first run's claims
        return real_filter(*args, **kwargs).none()

    monkeypatch.setattr(NotificationSent.objects, "filter", stale)
    again = daily.send_due(MON)
    assert len(mailoutbox) == 2 and (again["sent"], again["failed"]) == (0, 0)


def test_a_failed_send_is_given_back_and_the_next_run_sends_it(tenant, crew, contracts, editors, mailoutbox, monkeypatch):
    _broken_mail_for(monkeypatch, "kim@riverside.example")
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert [m.to for m in mailoutbox] == [["dana@riverside.example"]]  # Kim's failure did not stop the rest
    assert summary["failed"] == 1 and summary["tenants"][0]["reminders"] == 0
    assert _sent(tenant, "contract") == []  # the claims were given back
    monkeypatch.undo()
    mailoutbox.clear()
    again = daily.send_due(MON)
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]] and again["sent"] == 1
    assert len(_sent(tenant, "contract")) == 4


def test_a_failing_digest_is_retried_too(tenant, crew, mailoutbox, monkeypatch):
    _broken_mail_for(monkeypatch, "dana@riverside.example")
    assert daily.send_due(MON)["failed"] == 1 and _sent(tenant) == []
    monkeypatch.undo()
    assert daily.send_due(MON)["sent"] == 1


def test_an_error_while_building_one_email_fails_only_that_one(tenant, crew, contracts, editors, mailoutbox, monkeypatch):
    def broken(*a, **kw):
        raise ZeroDivisionError("bad data")

    monkeypatch.setattr(daily, "digest_for", broken)
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert summary["failed"] == 1 and [m.to for m in mailoutbox] == [["kim@riverside.example"]]
    assert _sent(tenant, "digest") == []


def test_a_template_error_gives_the_claim_back(tenant, crew, mailoutbox, monkeypatch):
    monkeypatch.setattr(daily, "DIGEST_TEMPLATE", "notifications/email/no_such_template")
    assert daily.send_due(MON)["failed"] == 1 and _sent(tenant) == []


# --- facilities --------------------------------------------------------------------------------------------------------

@pytest.fixture
def other_facility(other_tenant):
    """Other Hospital: Oli (manager) and one contract ending in 30 days. Closed Clinic (inactive): the same."""
    closed = Tenant.objects.create(name="Closed Clinic", slug="closed", is_active=False)
    out = {}
    for t, who in ((other_tenant, "oli@other.example"), (closed, "cal@closed.example")):
        create_default_roles(t)
        out[t.slug] = _person(t, "manager", who)
        with tenant_context(t):
            dept = Department.objects.create(name="ER")
            dm = DeviceModel.objects.create(manufacturer="Zoll", model="R Series", description="Defibrillator", category="Defibrillators")
            _contract("THEIRS-1", MON + timedelta(days=30), [Asset.objects.create(tag="Z-1", device_model=dm, department=dept)], vendor="Zoll")
    return out


def test_facilities_are_kept_apart_and_an_inactive_one_is_skipped(tenant, floor, editors, other_facility, mailoutbox):
    with tenant_context(tenant):
        _contract("OURS-1", MON + timedelta(days=30), [floor["vent"]])
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert [t["slug"] for t in summary["tenants"]] == ["other", "riverside"]  # not the closed clinic
    oli, kim = _to(mailoutbox, "oli@other.example"), _to(mailoutbox, "kim@riverside.example")
    assert len(mailoutbox) == 2 and "THEIRS-1" in oli[0].subject and "OURS-1" in kim[0].subject
    assert "OURS-1" not in oli[0].body and "THEIRS-1" not in kim[0].body
    assert "Other Hospital" in oli[0].subject


def test_a_facility_that_cannot_be_worked_through_does_not_stop_the_next(tenant, floor, editors, other_facility, mailoutbox, monkeypatch):
    with tenant_context(tenant):
        _contract("OURS-1", MON + timedelta(days=30), [floor["vent"]])
    real = daily._send_facility

    def send_facility(tenant_, today, counts):
        if tenant_.slug == "other":
            raise RuntimeError("database went away")
        return real(tenant_, today, counts)

    monkeypatch.setattr(daily, "_send_facility", send_facility)
    mailoutbox.clear()
    out = StringIO()
    with pytest.raises(CommandError, match=r"1 staff notification could not be sent \(other\)"):
        call_command("send_staff_notifications", stdout=out)
    assert "other: failed: RuntimeError: database went away" in out.getvalue()
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]]


@pytest.mark.parametrize("usable, closed", [(True, 0), (False, 1)])
def test_after_a_failed_facility_the_connection_is_closed_only_when_it_broke(tenant, floor, editors, other_facility, mailoutbox, monkeypatch,
                                                                          usable, closed):
    """A connection that broke is not reopened on its own outside a request, so the next facility gets a new one; a usable one is
    kept (closing it would throw away the caller's transaction for nothing)."""

    class Connection:
        def __init__(self):
            self.closes = 0

        def is_usable(self):
            return usable

        def close(self):
            self.closes += 1

    fake = Connection()
    monkeypatch.setattr(daily, "connection", fake)
    with tenant_context(tenant):
        _contract("OURS-1", MON + timedelta(days=30), [floor["vent"]])
    real = daily._send_facility

    def send_facility(tenant_, today, counts):
        if tenant_.slug == "other":
            raise RuntimeError("database went away")
        return real(tenant_, today, counts)

    monkeypatch.setattr(daily, "_send_facility", send_facility)
    mailoutbox.clear()
    summary = daily.send_due(MON)
    assert fake.closes == closed and summary["failed"] == 1 and [m.to for m in mailoutbox] == [["kim@riverside.example"]]


# --- the command and the daily job --------------------------------------------------------------------------------------

def test_the_command_prints_a_line_per_facility_and_the_totals(tenant, crew, contracts, editors, other_facility, mailoutbox):
    mailoutbox.clear()
    out = StringIO()
    call_command("send_staff_notifications", stdout=out)
    assert out.getvalue().splitlines() == [
        "other: 1 contract reminder sent (1 contract)",
        "riverside: 1 digest sent, 1 contract reminder sent (4 contracts), 6 skipped (1 contract reminder: no email address; "
        "1 contract reminder: sees only part of the facility; 1 digest: no Work orders View; 1 digest: no email address; "
        "1 digest: no active technician record; 1 digest: sees only part of the facility)",
        "Staff notifications for 2026-10-05: 3 sent, 0 failed, 6 skipped",
    ]
    out = StringIO()
    call_command("send_staff_notifications", stdout=out)  # nothing goes twice; the skipped stay skipped
    assert out.getvalue().splitlines()[0] == "other: nothing to send"
    assert out.getvalue().splitlines()[-1] == "Staff notifications for 2026-10-05: 0 sent, 0 failed, 6 skipped"
    assert len(mailoutbox) == 3


def test_the_command_takes_a_past_date_and_refuses_a_bad_or_future_one(tenant, crew, clock, mailoutbox):
    clock(MON + timedelta(days=1))
    mailoutbox.clear()
    out = StringIO()
    call_command("send_staff_notifications", "--date", "2026-10-05", stdout=out)
    assert "riverside: 1 digest sent" in out.getvalue() and "for Mon, Oct 5" in mailoutbox[0].subject
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        call_command("send_staff_notifications", "--date", "05/10/2026", stdout=StringIO())
    with pytest.raises(CommandError, match="future"):
        call_command("send_staff_notifications", "--date", "2026-10-07", stdout=StringIO())


def test_the_command_fails_after_trying_every_email(tenant, crew, contracts, editors, mailoutbox, monkeypatch):
    _broken_mail_for(monkeypatch, "dana@riverside.example")
    mailoutbox.clear()
    out = StringIO()
    with pytest.raises(CommandError, match=r"1 staff notification could not be sent \(riverside\)"):
        call_command("send_staff_notifications", stdout=out)
    assert "riverside: 1 contract reminder sent (4 contracts), 6 skipped" in out.getvalue() and out.getvalue().splitlines()[0].endswith(", 1 failed")
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]]


def test_the_daily_job_runs_the_command_once_a_day(tenant, crew, mailoutbox):
    assert [key for key, _cmd, _opts in jobs.DAILY_JOBS][-2:] == ["report_emails", daily.JOB]
    mailoutbox.clear()
    runs = jobs.run_daily_jobs(day=MON, jobs=[daily.JOB])
    assert [(r.job, r.facility, r.run_on, r.status) for r in runs] == [(daily.JOB, tenant, MON, "succeeded")]  # the facility's run
    assert "riverside: 1 digest sent" in runs[0].output and len(mailoutbox) == 1
    assert jobs.run_daily_jobs(day=MON, jobs=[daily.JOB]) == []  # the day's run is the lock


def test_the_daily_job_is_recorded_as_failed_when_an_email_fails(tenant, crew, mailoutbox, monkeypatch):
    _broken_mail_for(monkeypatch, "dana@riverside.example")
    runs = jobs.run_daily_jobs(day=MON, jobs=[daily.JOB])
    assert runs[0].status == "failed" and "1 staff notification could not be sent" in runs[0].output


# --- tenant isolation and row-level security -----------------------------------------------------------------------------

def test_what_was_sent_is_scoped_to_its_facility(tenant, other_tenant, crew, mailoutbox):
    daily.send_due(MON)
    with tenant_context(other_tenant):
        assert NotificationSent.objects.count() == 0
    with tenant_context(tenant):
        assert NotificationSent.objects.count() == 1
    assert NotificationSent.unscoped.count() == 1  # unscoped: the test proves only one row exists at all


def test_send_due_and_the_command_touch_no_facility_table_before_entering_it(rls, tenant, crew, contracts, editors, other_facility,  # noqa: F811
                                                                              mailoutbox):
    mailoutbox.clear()
    with rls:
        summary = daily.send_due(MON)
        call_command("send_staff_notifications", stdout=StringIO())  # nothing left to send, but it reads every facility's rows
        jobs.run_daily_jobs(day=MON, jobs=[daily.JOB])
    assert rls.violations == []
    assert summary["sent"] == 3 and len(mailoutbox) == 3


@needs_postgres
def test_the_command_works_under_the_policies_from_no_tenant(tenant, crew, contracts, editors, other_facility, mailoutbox):
    """As the runtime role under row-level security, starting with no tenant (as the daily job does): each facility's digests and
    reminders go, and what was sent is recorded in its own facility."""
    mailoutbox.clear()
    as_app_role()
    with tenant_context(None):
        out = StringIO()
        call_command("send_staff_notifications", stdout=out)
        assert "Staff notifications for 2026-10-05: 3 sent, 0 failed" in out.getvalue(), out.getvalue()
        call_command("send_staff_notifications", stdout=(again := StringIO()))  # the claims are read back under the policies
        assert "0 sent, 0 failed" in again.getvalue()
        jobs.run_daily_jobs(day=MON, jobs=[daily.JOB])
    assert sorted(m.to[0] for m in mailoutbox) == ["dana@riverside.example", "kim@riverside.example", "oli@other.example"]
    with tenant_context(tenant):
        assert len(NotificationSent.objects.filter(kind="contract")) == 4 and NotificationSent.objects.filter(kind="digest").count() == 1
    with tenant_context(other_facility["other"].tenant):
        assert NotificationSent.objects.filter(kind="contract").count() == 1
