"""Portal email confirmations (slice 13): the Settings rows (the confirmation choice and the work email domains, with their rules
and audit history), the portal's optional work email field, the confirmation email and the done notice (content, no problem text,
links from APP_BASE_URL, once only), every reason nothing is sent, email failures that leave the request and the status change
intact, tenant isolation, and the no-tenant paths under the row-level security stand-in."""
import json
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from test_rls_paths import rls  # noqa: F401

from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.facility import services as fs
from apps.facility.models import FacilitySettings
from apps.portal import notifications
from apps.tenants.context import tenant_context
from apps.workorders.models import ServiceRequest, WorkOrder, WorkOrderStatusHistory, WoStatus
from apps.workorders.services import change_status, create_service_request, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
APP = "https://ce.riverside-health.org"
DOMAINS = "riverside-health.org, rrmc.org"
PROBLEM = "Alarm will not clear, patient in bed 4 moved to a backup vent"
EMAIL = "kim.lee@rrmc.org"


@pytest.fixture(autouse=True)
def app_base(settings):
    settings.APP_BASE_URL = APP


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def email_on(ctx):
    return fs.update_settings(portal_confirmation="email", portal_email_domains=DOMAINS, portal_hotline="ext. 4400")


def _toast(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


def _post(client, asset, **changes):
    data = {"asset_tag": asset.tag, "department": str(asset.department_id), "room": "Bed 4", "requester_name": "Kim Lee", "callback": "x2210",
            "problem": PROBLEM, "urgency": "high", "requester_email": EMAIL, **changes}
    return client.post(f"/r/{asset.tenant.slug}/", data)


def _request(asset, email=EMAIL) -> ServiceRequest:
    return create_service_request(asset=asset, department=asset.department, problem=PROBLEM, urgency="high", requester_name="Kim Lee",
                                  callback="x2210", room="Bed 4", requester_email=email)


def _complete(wo, **kw):
    change_status(wo, WoStatus.IN_PROGRESS, **kw)
    change_status(wo, WoStatus.COMPLETED, **kw)


def _notes(wo, note) -> int:
    return WorkOrderStatusHistory.objects.filter(work_order=wo, note=note).count()


def _links(text) -> list[str]:
    return [word for word in text.split() if "://" in word]


# --- settings: the rules ------------------------------------------------------------------------------------

def test_domains_are_lowercased_trimmed_and_deduplicated(ctx):
    s = fs.update_settings(portal_email_domains=" Riverside-Health.ORG,rrmc.org , riverside-health.org,, ")
    assert s.portal_email_domains == "riverside-health.org, rrmc.org" and fs.email_domains(s) == ["riverside-health.org", "rrmc.org"]
    assert fs.update_settings(portal_email_domains="").portal_email_domains == ""  # blank is fine while confirmations are on screen


@pytest.mark.parametrize("value, message", [
    ("kim@rrmc.org", "Enter the part after the @ only, like riverside-health.org."),
    ("@rrmc.org", "Enter the part after the @ only, like riverside-health.org."),
    ("https://rrmc.org", "Enter the domain only, like riverside-health.org, without https:// or a slash."),
    ("rrmc.org/", "Enter the domain only, like riverside-health.org, without https:// or a slash."),
    ("rrmc.org riverside-health.org", "Separate domains with commas, like riverside-health.org, rrmc.org."),
    ("rrmc", "rrmc is not a domain. Use letters, digits, dots, and hyphens, like riverside-health.org."),
    ("rrmc.org, -rrmc.org", "-rrmc.org is not a domain. Use letters, digits, dots, and hyphens, like riverside-health.org."),
    ("rr_mc.org", "rr_mc.org is not a domain. Use letters, digits, dots, and hyphens, like riverside-health.org."),
    ("10.0.0.1", "10.0.0.1 is not a domain. Use letters, digits, dots, and hyphens, like riverside-health.org."),
    ("rrmc..org", "rrmc..org is not a domain. Use letters, digits, dots, and hyphens, like riverside-health.org."),
    (", ".join(f"d{i}.org" for i in range(11)), "List at most 10 domains."),
    (", ".join(f"{c * 30}.org" for c in "abcdefg"), "Keep the domains to 200 characters in all."),
])
def test_bad_domains_are_refused_and_nothing_is_saved(ctx, value, message):
    with pytest.raises(ValidationError) as e:
        fs.update_settings(portal_email_domains=value, portal_hotline="ext. 4400")
    assert e.value.message_dict == {"portal_email_domains": [message]}
    assert not FacilitySettings.objects.exists()


def test_email_confirmations_need_a_domain(ctx):
    with pytest.raises(ValidationError) as e:
        fs.update_settings(portal_confirmation="email")
    assert e.value.message_dict == {"portal_confirmation": ["Add a work email domain before turning on email confirmations."]}
    with pytest.raises(ValidationError):
        fs.update_settings(portal_confirmation="email", portal_email_domains=" , ")
    assert not FacilitySettings.objects.exists()
    s = fs.update_settings(portal_confirmation="email", portal_email_domains="rrmc.org")  # both in one change is fine
    assert s.portal_confirmation == "email" and fs.portal_emails_on(s)
    with pytest.raises(ValidationError) as e:  # and the last domain cannot be removed while email is on
        fs.update_settings(portal_email_domains="")
    assert e.value.message_dict == {"portal_confirmation": [
        "Email confirmations need a work email domain. Choose On screen before removing the last one."]}
    assert fs.get_settings().portal_email_domains == "rrmc.org"
    s = fs.update_settings(portal_confirmation="screen", portal_email_domains="")
    assert s.portal_confirmation == "screen" and not fs.portal_emails_on(s)


@pytest.mark.parametrize("value", ["text", "", None, "EMAIL"])
def test_unknown_confirmation_is_refused(ctx, value):
    with pytest.raises(ValidationError) as e:
        fs.update_settings(portal_confirmation=value, portal_email_domains="rrmc.org")
    assert e.value.message_dict == {"portal_confirmation": ["Choose On screen, or On screen and by email."]}


def test_changes_are_in_the_audit_history(ctx, make_user):
    kim = make_user("director")
    fs.update_settings(by=kim, portal_email_domains="rrmc.org")
    fs.update_settings(by=kim, portal_confirmation="email")
    history = list(fs.get_settings().history.all())  # newest first
    assert [(h.portal_confirmation, h.portal_email_domains, h.history_user) for h in history] == [("email", "rrmc.org", kim),
                                                                                                    ("screen", "rrmc.org", kim)]


def test_email_allowed_is_exact_and_needs_email_on(ctx):
    s = fs.update_settings(portal_email_domains="rrmc.org")
    assert not fs.email_allowed(EMAIL, s)  # confirmations on screen only
    s = fs.update_settings(portal_confirmation="email")
    assert fs.email_allowed(EMAIL, s) and fs.email_allowed("Kim.Lee@RRMC.ORG", s)
    for address in ["kim@icu.rrmc.org", "kim@rrmc.org.evil.example", "kim@gmail.com", "rrmc.org", "@rrmc.org", "", None]:
        assert not fs.email_allowed(address, s), address
    assert fs.domains_text(["a.org"]) == "a.org" and fs.domains_text(["a.org", "b.org"]) == "a.org or b.org"
    assert fs.domains_text(["a.org", "b.org", "c.org"]) == "a.org, b.org, or c.org"


# --- settings: the screen -----------------------------------------------------------------------------------

def _portal_form(**changes):
    return {"portal_require_callback": ["0", "1"], "portal_hotline": "ext. 4400", "portal_confirmation": "screen", "portal_email_domains": "", **changes}


def test_the_screen_saves_the_choice_and_the_domains(client, signed_in, ctx):
    kim = signed_in("director")
    r = client.post("/settings/portal/", _portal_form(portal_email_domains="RRMC.org, riverside-health.org"), **HX)
    assert _toast(r) == "Portal setting saved"
    r = client.post("/settings/portal/", _portal_form(portal_confirmation="email", portal_email_domains="rrmc.org, riverside-health.org"), **HX)
    assert _toast(r) == "Portal setting saved"
    s = fs.get_settings()
    assert (s.portal_confirmation, s.portal_email_domains, s.history.first().history_user) == ("email", "rrmc.org, riverside-health.org", kim)
    body = r.content.decode()
    assert body.lstrip().startswith('<form class="set-rows" id="set-portal-form"')  # the auto-save still swaps only its own form
    assert '<option value="screen">On screen</option><option value="email" selected>On screen and by email</option>' in body
    assert 'name="portal_email_domains" value="rrmc.org, riverside-health.org"' in body and 'aria-invalid' not in body


def test_the_screen_refuses_email_without_a_domain(client, signed_in, ctx):
    signed_in("director")
    r = client.post("/settings/portal/", _portal_form(portal_confirmation="email"), **HX)
    assert _toast(r) == "Add a work email domain before turning on email confirmations."
    assert fs.get_settings()._state.adding  # nothing saved, not even the hotline
    assert '<option value="screen" selected>On screen</option>' in r.content.decode()


def test_the_screen_keeps_a_refused_domain_list_as_typed(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(portal_email_domains="rrmc.org")
    r = client.post("/settings/portal/", _portal_form(portal_email_domains="rrmc.org, kim@riverside-health.org"), **HX)
    assert _toast(r) == "Enter the part after the @ only, like riverside-health.org."
    body = r.content.decode()
    assert 'name="portal_email_domains" value="rrmc.org, kim@riverside-health.org"' in body and 'aria-invalid="true"' in body
    assert '<span class="err" id="set-portal-domains-err">Enter the part after the @ only, like riverside-health.org.</span>' in body
    assert fs.get_settings().portal_email_domains == "rrmc.org" and fs.get_settings().portal_hotline == ""


def test_a_manager_sees_the_rows_but_cannot_change_them(client, signed_in, ctx):
    signed_in("manager")
    body = client.get("/settings/").content.decode()
    assert 'name="portal_confirmation" aria-label="Confirmation to the requester" disabled>' in body
    assert 'aria-describedby="set-portal-domains-help" disabled>' in body
    assert client.post("/settings/portal/", _portal_form(portal_email_domains="rrmc.org"), **HX).status_code == 403
    assert fs.get_settings()._state.adding


# --- the portal form ----------------------------------------------------------------------------------------

def test_the_email_field_is_offered_only_when_the_facility_confirms_by_email(client, ctx, vent):
    body = client.get("/r/riverside/").content.decode()
    assert "requester_email" not in body and "Work email" not in body
    r = _post(client, vent)  # an address posted anyway is ignored: the form has no such field
    assert r.status_code == 302 and ServiceRequest.objects.get().requester_email == ""
    fs.update_settings(portal_confirmation="email", portal_email_domains=DOMAINS)
    body = client.get("/r/riverside/").content.decode()
    assert '<label for="id_requester_email">Work email for updates (optional)</label>' in body
    assert ('<div class="help" id="id_requester_email_helptext">For a confirmation now and a note when the work is done. '
            "Use your riverside-health.org or rrmc.org address.</div>") in body
    assert body.index('name="callback"') < body.index('name="requester_email"') < body.index('name="problem"')
    assert "If a device failure is affecting a patient right now" in body  # the existing hint stays


@pytest.mark.parametrize("address", ["kim@gmail.com", "kim@icu.rrmc.org", "kim@rrmc.org.example.com"])
def test_an_address_at_another_domain_is_refused(client, email_on, vent, address):
    r = _post(client, vent, requester_email=address)
    assert r.status_code == 200 and "Use your riverside-health.org or rrmc.org work email, or leave this blank." in r.content.decode()
    assert not ServiceRequest.objects.exists()


def test_the_address_is_optional_and_lowercased(client, email_on, vent):
    assert _post(client, vent, requester_email="").status_code == 302
    assert _post(client, vent, requester_email="  Kim.Lee@RRMC.org ").status_code == 302
    assert list(ServiceRequest.objects.order_by("number").values_list("requester_email", flat=True)) == ["", EMAIL]


def test_the_service_stores_the_address_lowercased(ctx, vent):
    assert _request(vent, email=" Kim.Lee@RRMC.org ").requester_email == EMAIL
    assert _request(vent, email="").requester_email == ""


# --- the confirmation email ---------------------------------------------------------------------------------

def test_the_confirmation_email(client, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(client, vent)
    sr = ServiceRequest.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/r/riverside/done/{sr.number}/"
    assert len(mailoutbox) == 1
    m = mailoutbox[0]
    assert m.to == [EMAIL] and m.subject == f"Request {sr.number} received: CE-10001, ICU ventilator"
    for line in [f"Request number: {sr.number}", "Device: CE-10001 · ICU ventilator", "Location: ICU, Bed 4",
                 "Urgency: Device unusable, a backup is in use", "Response target: Within 4 hours",
                 "call the Clinical Engineering shop at ext. 4400 and give your request number",
                 "Clinical Engineering at Riverside Regional has your service request."]:
        assert line in m.body, line
    assert "Alarm" not in m.body and "bed 4 moved" not in m.body and "Alarm" not in m.subject  # never the problem text
    assert "Kim" not in m.body  # nor the free-text name: only fields the requester picked or the facility set
    assert all(link.startswith(APP) for link in _links(m.body))
    assert _notes(sr.work_order, notifications.RECEIVED_NOTE) == 1
    # the confirmation page names the address to the browser that sent the request, and to no one else
    body = client.get(r["Location"]).content.decode()
    assert f"A confirmation was emailed to {EMAIL}. We will email again when the work is done." in body
    other = Client().get(r["Location"]).content.decode()
    assert "A confirmation was emailed to the address given with the request." in other and EMAIL not in other


def test_the_confirmation_page_says_nothing_without_an_address(client, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(client, vent, requester_email="")
    body = client.get(r["Location"]).content.decode()
    assert mailoutbox == [] and "emailed" not in body and "could not email" not in body


def test_the_confirmation_without_a_hotline(ctx, vent, mailoutbox, django_capture_on_commit_callbacks):
    fs.update_settings(portal_confirmation="email", portal_email_domains="rrmc.org")
    with django_capture_on_commit_callbacks(execute=True):
        _request(vent)
    assert "call the Clinical Engineering shop and give your request number" in mailoutbox[0].body


def test_nothing_is_sent_in_screen_mode_or_at_a_domain_no_longer_allowed(ctx, vent, mailoutbox, django_capture_on_commit_callbacks):
    fs.update_settings(portal_email_domains=DOMAINS)  # domains, but confirmations on screen only
    with django_capture_on_commit_callbacks(execute=True):
        sr = _request(vent)
    assert mailoutbox == [] and notifications.send_request_received(sr) is False
    fs.update_settings(portal_confirmation="email")
    fs.update_settings(portal_email_domains="riverside-health.org")  # rrmc.org removed after the request came in
    assert notifications.send_request_received(sr) is False
    _complete(sr.work_order)
    assert notifications.send_request_done(sr.work_order) is False
    fs.update_settings(portal_email_domains=DOMAINS)
    assert notifications.send_request_received(sr) is True and len(mailoutbox) == 1


def test_nothing_is_sent_once_email_is_turned_off(ctx, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    sr = _request(vent)  # the after-commit confirmation never runs here: the test's transaction never commits
    fs.update_settings(portal_confirmation="screen")
    with django_capture_on_commit_callbacks(execute=True):
        _complete(sr.work_order)
    assert mailoutbox == [] and _notes(sr.work_order, notifications.DONE_NOTE) == 0


# --- the done notice ----------------------------------------------------------------------------------------

def test_the_done_notice_from_the_web_ui(client, signed_in, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    sr = _request(vent)
    signed_in("director")
    with django_capture_on_commit_callbacks(execute=True):
        for to in ("in_progress", "completed"):
            assert client.post(f"/work-orders/{sr.work_order.number}/status/", {"to": to}, **HX).status_code == 200
    assert len(mailoutbox) == 1
    m, today = mailoutbox[0], date.today()
    assert m.to == [EMAIL] and m.subject == f"Request {sr.number} done: CE-10001, ICU ventilator"
    for line in [f"The work on your service request {sr.number} at Riverside Regional is done.", "Device: CE-10001 · ICU ventilator",
                 "Location: ICU, Bed 4", f"Completed: {today:%b} {today.day}, {today.year}",
                 "If the problem is back, submit a new request or call ext. 4400:", f"{APP}/r/riverside/?asset=CE-10001"]:
        assert line in m.body, line
    assert "Alarm" not in m.body and "bed 4 moved" not in m.body and all(link.startswith(APP) for link in _links(m.body))
    # the work order's timeline says so
    body = client.get(f"/work-orders/{sr.work_order.number}/", **HX).content.decode()
    assert notifications.DONE_NOTE in body


def test_the_done_notice_is_sent_once(ctx, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    wo = _request(vent).work_order
    with django_capture_on_commit_callbacks(execute=True):
        _complete(wo)
    with django_capture_on_commit_callbacks(execute=True):
        change_status(wo, WoStatus.CLOSED)
        change_status(wo, WoStatus.IN_PROGRESS)  # reopened
        change_status(wo, WoStatus.COMPLETED)
    assert len(mailoutbox) == 1 and _notes(wo, notifications.DONE_NOTE) == 1
    assert notifications.send_request_done(wo) is False


def test_the_done_notice_without_a_hotline(ctx, vent, mailoutbox, django_capture_on_commit_callbacks):
    fs.update_settings(portal_confirmation="email", portal_email_domains="rrmc.org")
    wo = _request(vent).work_order
    with django_capture_on_commit_callbacks(execute=True):
        _complete(wo)
    assert "If the problem is back, submit a new request or call the Clinical Engineering shop:" in mailoutbox[-1].body


def test_no_done_notice_for_work_orders_without_a_portal_request(ctx, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Fan noise", requester="Kim Lee, ICU")
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        _complete(wo)
    assert callbacks == [] and mailoutbox == [] and notifications.send_request_done(wo) is False


def test_no_done_notice_without_an_address(ctx, email_on, vent, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        wo = _request(vent, email="").work_order
        _complete(wo)
    assert mailoutbox == [] and _notes(wo, notifications.DONE_NOTE) == 0


def test_no_done_notice_while_the_work_order_is_open(ctx, email_on, vent, mailoutbox):
    wo = _request(vent).work_order
    assert notifications.send_request_done(wo) is False and mailoutbox == []


# --- failures -----------------------------------------------------------------------------------------------

def _unreachable(self, fail_silently=False):
    raise OSError("mail server unreachable")


@pytest.fixture
def mail_down(monkeypatch):
    monkeypatch.setattr("apps.accounts.emails.EmailMessage.send", _unreachable)


def test_a_failed_confirmation_keeps_the_request(client, email_on, vent, mail_down, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        r = _post(client, vent)
    sr = ServiceRequest.objects.get()
    assert r.status_code == 302 and sr.requester_email == EMAIL and sr.work_order.status == WoStatus.OPEN
    assert _notes(sr.work_order, notifications.RECEIVED_NOTE) == 0
    assert f"We could not email a confirmation to {EMAIL}. Keep your request number." in client.get(r["Location"]).content.decode()


def test_a_failed_done_notice_keeps_the_status_change_and_is_tried_again(ctx, email_on, vent, monkeypatch, mailoutbox,
                                                                         django_capture_on_commit_callbacks):
    wo = _request(vent).work_order
    with monkeypatch.context() as m:
        m.setattr("apps.accounts.emails.EmailMessage.send", _unreachable)
        with django_capture_on_commit_callbacks(execute=True):
            _complete(wo)
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.completed_on == date.today() and mailoutbox == []
    assert _notes(wo, notifications.DONE_NOTE) == 0  # not sent, so not recorded: completing again after a reopen tries again
    with django_capture_on_commit_callbacks(execute=True):
        change_status(wo, WoStatus.IN_PROGRESS)
        change_status(wo, WoStatus.COMPLETED)
    assert len(mailoutbox) == 1 and _notes(wo, notifications.DONE_NOTE) == 1


def test_an_error_while_notifying_never_breaks_the_status_change(ctx, email_on, vent, monkeypatch, mailoutbox, django_capture_on_commit_callbacks):
    wo = _request(vent).work_order

    def broken(*args, **kwargs):
        raise RuntimeError("template missing")

    monkeypatch.setattr(notifications, "_context", broken)
    with django_capture_on_commit_callbacks(execute=True):
        _complete(wo)
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and mailoutbox == []
    assert notifications.send_request_done(wo) is False and notifications.send_request_received(wo.service_request) is False


# --- tenants ------------------------------------------------------------------------------------------------

def test_emails_use_the_requests_own_facility(ctx, vent, other_tenant, mailoutbox):
    """Called from inside another tenant (an admin job, say), the notice still reads only the request's own facility."""
    fs.update_settings(portal_confirmation="email", portal_email_domains="rrmc.org", portal_hotline="ext. 4400")
    with tenant_context(other_tenant):
        fs.update_settings(portal_confirmation="email", portal_email_domains="other.example", portal_hotline="ext. 9999")
    sr = _request(vent)
    _complete(sr.work_order)
    with tenant_context(other_tenant):
        assert notifications.send_request_done(sr.work_order) is True
    assert "call ext. 4400" in mailoutbox[0].body and "9999" not in mailoutbox[0].body
    # and the other facility's settings never allow this one's mail
    fs.update_settings(portal_confirmation="screen")
    with tenant_context(other_tenant):
        assert notifications.send_request_received(sr) is False
    assert len(mailoutbox) == 1


def test_the_other_tenant_never_sees_the_notes(ctx, email_on, vent, other_tenant, mailoutbox):
    sr = _request(vent)
    assert notifications.send_request_received(sr) is True
    with tenant_context(other_tenant):
        assert not WorkOrderStatusHistory.objects.filter(note=notifications.RECEIVED_NOTE).exists()
        assert not ServiceRequest.objects.filter(requester_email=EMAIL).exists()


# --- no tenant set (row-level security) ---------------------------------------------------------------------

def _riverside(tenant):
    """Data for the no-tenant tests, made inside the tenant and left again, so nothing below starts with a tenant set."""
    with tenant_context(tenant):
        fs.update_settings(portal_confirmation="email", portal_email_domains=DOMAINS, portal_hotline="ext. 4400")
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                           risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        return Asset.objects.create(tag="CE-10001", device_model=model, department=dept, next_pm_on=date.today() + timedelta(days=10))


def test_portal_post_with_email_under_rls(client, rls, tenant, mailoutbox, django_capture_on_commit_callbacks):  # noqa: F811
    asset = _riverside(tenant)
    with rls:
        with django_capture_on_commit_callbacks(execute=True):  # the confirmation runs after the portal has left the tenant
            r = _post(client, asset)
        assert r.status_code == 302
        assert EMAIL in client.get(r["Location"]).content.decode()
    assert rls.violations == []
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [EMAIL]


def test_completion_with_no_tenant_set_under_rls(rls, tenant, mailoutbox, django_capture_on_commit_callbacks):  # noqa: F811
    asset = _riverside(tenant)
    with tenant_context(tenant):
        wo = _request(asset).work_order
    with rls:
        with django_capture_on_commit_callbacks(execute=True):  # like a management command: the commit comes after its tenant_context
            with tenant_context(tenant):
                _complete(wo)
    assert rls.violations == []
    assert len(mailoutbox) == 1 and "done" in mailoutbox[0].subject
    with tenant_context(tenant):
        assert _notes(wo, notifications.DONE_NOTE) == 1


def test_sending_directly_with_no_tenant_set_under_rls(rls, tenant, mailoutbox):  # noqa: F811
    asset = _riverside(tenant)
    with tenant_context(tenant):
        sr = _request(asset)
        _complete(sr.work_order)
    wo = WorkOrder.unscoped.get(pk=sr.work_order_id)  # unscoped: loaded before the guard, as a job holding a work order would be
    with rls:
        assert notifications.send_request_received(sr) is True
        assert notifications.send_request_done(wo) is True
        assert notifications.send_request_done(wo) is False  # once
    assert rls.violations == []
    assert [m.subject.split(":")[0] for m in mailoutbox] == [f"Request {sr.number} received", f"Request {sr.number} done"]
