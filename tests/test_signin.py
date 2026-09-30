"""Signing in and passwords (slice 10): username-or-email sign-in, lockouts, password reset by email, and the signed-in
password change. None of it may say whether an account exists."""
import re
import smtplib
import time

import django.core.cache.backends.locmem as locmem
import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts import services, signin
from apps.accounts.models import Role, User, create_default_roles
from apps.web.forms_account import MISMATCH, WRONG_LOGIN

PW = "Test-Pass-2026-x"
NEW_PW = "Harbor-Lantern-7731"
SENT = "/password-reset/sent/"
LOCKED = "Too many failed sign-ins. Try again in"


@pytest.fixture(autouse=True)
def _fast_hashing(settings):
    """Sign-in tests hash passwords on every attempt; the fast hasher keeps them quick. The logic under test is the same."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def person(tenant, username, email="", *, role_slug="director", password=PW, **kw):
    role = Role.unscoped.filter(tenant=tenant, slug=role_slug).first() if tenant else None  # unscoped: tests run outside a request
    return User.objects.create_user(username=username, email=email, password=password, tenant=tenant, role=role, first_name="Kim", last_name="Lee", **kw)


@pytest.fixture
def outbox(db, mailoutbox):
    """pytest-django's mailoutbox, taken after the database is set up: that setup replaces mail.outbox, so a mailoutbox
    resolved before it is a list nothing is sent to."""
    return mailoutbox


@pytest.fixture
def kim(tenant):
    return person(tenant, "kim.lee", "kim@riverside.example")


def sign_in(client, login, password=PW, next_url=None, **extra):
    data = {"username": login, "password": password, **({"next": next_url} if next_url else {})}
    return client.post("/login/", data, **extra)


def signed_in_pk(client):
    pk = client.session.get("_auth_user_id")
    return int(pk) if pk is not None else None


def form_error(r) -> str:
    return " ".join(r.context["form"].non_field_errors())


class _Clock:
    """Moves the cache's clock (and the lockout's) forward without sleeping."""

    def __init__(self):
        self.offset = 0

    def time(self):
        return _real_time() + self.offset


_real_time = time.time


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(locmem, "time", c)
    monkeypatch.setattr(signin, "time", c)
    return c


def reset_link(mail) -> str:
    return re.search(r"https?://\S+", mail.body).group(0)


# --- signing in -------------------------------------------------------------------------------------

def test_sign_in_by_email_in_any_letter_case_and_by_username(client, kim):
    for login in ["kim@riverside.example", "KIM@Riverside.Example", "  kim@riverside.example ", "kim.lee", "Kim.Lee", " kim.lee "]:
        r = sign_in(client, login)
        assert r.status_code == 302 and r["Location"] == "/", login
        assert signed_in_pk(client) == kim.pk
        client.logout()


def test_wrong_password_gets_the_plain_message(client, kim):
    r = sign_in(client, "kim@riverside.example", "not-the-password")
    assert r.status_code == 200 and signed_in_pk(client) is None
    assert form_error(r) == WRONG_LOGIN
    assert form_error(sign_in(client, "nobody@riverside.example", "not-the-password")) == WRONG_LOGIN


def test_an_ambiguous_email_signs_in_to_neither_account_but_each_by_its_username(client, tenant, other_tenant):
    create_default_roles(other_tenant)
    here = person(tenant, "kim.r", "kim@shared.example")
    there = person(other_tenant, "kim.o", "Kim@Shared.example")
    r = sign_in(client, "kim@shared.example")
    assert r.status_code == 200 and signed_in_pk(client) is None and form_error(r) == WRONG_LOGIN
    assert sign_in(client, "kim.r").status_code == 302 and signed_in_pk(client) == here.pk
    client.logout()
    assert sign_in(client, "kim.o").status_code == 302 and signed_in_pk(client) == there.pk


def test_a_login_naming_one_account_by_username_and_another_by_email_is_ambiguous(client, tenant):
    person(tenant, "sam@riverside.example", "sam@riverside.example")
    person(tenant, "sam", "SAM@RIVERSIDE.EXAMPLE")
    assert sign_in(client, "Sam@Riverside.example").status_code == 200 and signed_in_pk(client) is None
    assert sign_in(client, "sam@riverside.example").status_code == 302  # the exact username still works


def test_deactivated_account_and_inactive_facility_cannot_sign_in(client, tenant, kim, other_tenant):
    kim.is_active = False
    kim.save()
    r = sign_in(client, "kim.lee")
    assert r.status_code == 200 and signed_in_pk(client) is None and form_error(r) == WRONG_LOGIN
    create_default_roles(other_tenant)
    closed = person(other_tenant, "pat", "pat@other.example")
    other_tenant.is_active = False
    other_tenant.save()
    r = sign_in(client, "pat@other.example")
    assert r.status_code == 200 and signed_in_pk(client) is None and form_error(r) == WRONG_LOGIN
    assert closed.check_password(PW)  # the password was right; the facility is closed


def test_a_session_of_a_facility_that_is_deactivated_is_signed_out_on_its_next_request(client, tenant, kim):
    client.force_login(kim)
    assert client.get("/").status_code == 200
    tenant.is_active = False
    tenant.save()
    r = client.get("/")
    assert r.status_code == 302 and r["Location"].startswith("/login/")


def test_next_is_honoured_after_sign_in_but_never_to_another_site(client, kim):
    r = client.get("/login/?next=/equipment/")
    assert b'name="next" value="/equipment/"' in r.content
    r = sign_in(client, "kim.lee", next_url="/equipment/")
    assert r.status_code == 302 and r["Location"] == "/equipment/"
    client.logout()
    r = sign_in(client, "kim.lee", next_url="https://evil.example/")
    assert r.status_code == 302 and r["Location"] == "/"


def test_a_signed_in_user_opening_sign_in_goes_home(client, kim):
    client.force_login(kim)
    r = client.get("/login/")
    assert r.status_code == 302 and r["Location"] == "/"


def test_sign_in_page_links_to_forgot_password(client, db):
    r = client.get("/login/")
    assert r.status_code == 200
    assert f'href="{reverse("web:password_reset")}"'.encode() in r.content and b"Forgot your password?" in r.content


# --- lockouts ------------------------------------------------------------------------------------------

def test_lockout_after_max_failures_refuses_even_the_right_password(client, settings, kim):
    settings.SIGNIN_MAX_FAILURES = 3
    for _ in range(2):
        assert form_error(sign_in(client, "kim@riverside.example", "wrong-password")) == WRONG_LOGIN
    r = sign_in(client, "kim@riverside.example", "wrong-password")
    assert form_error(r) == f"{LOCKED} 15 minutes or reset your password."
    r = sign_in(client, " KIM@riverside.example", PW)  # same login text once trimmed and lowercased
    assert r.status_code == 200 and signed_in_pk(client) is None and form_error(r).startswith(LOCKED)
    # the lock is on the login text: the username is a different text and still works
    assert sign_in(client, "kim.lee").status_code == 302


def test_the_lockout_reads_the_same_for_a_login_nobody_uses(client, settings, kim):
    settings.SIGNIN_MAX_FAILURES = 3
    for login in ["kim@riverside.example", "nobody@riverside.example"]:
        for _ in range(3):
            r = sign_in(client, login, "wrong-password")
        assert form_error(r) == f"{LOCKED} 15 minutes or reset your password."
        assert form_error(sign_in(client, login, PW)) == f"{LOCKED} 15 minutes or reset your password."


def test_the_lock_lifts_when_the_window_ends(client, settings, kim, clock):
    settings.SIGNIN_MAX_FAILURES = 2
    for _ in range(2):
        sign_in(client, "kim.lee", "wrong-password")
    clock.offset = 10 * 60
    assert form_error(sign_in(client, "kim.lee")) == f"{LOCKED} 5 minutes or reset your password."
    clock.offset = settings.SIGNIN_WINDOW_MINUTES * 60 + 1
    assert signin.is_locked("kim.lee", None) is None
    assert sign_in(client, "kim.lee").status_code == 302 and signed_in_pk(client) == kim.pk


def test_a_successful_sign_in_clears_the_failures(client, settings, kim):
    settings.SIGNIN_MAX_FAILURES = 3
    for _ in range(2):
        sign_in(client, "kim.lee", "wrong-password")
    assert sign_in(client, "kim.lee").status_code == 302
    client.logout()
    for _ in range(2):
        sign_in(client, "kim.lee", "wrong-password")
    assert sign_in(client, "kim.lee").status_code == 302  # 2 + 2 would have locked it had the first two been kept


def test_too_many_failures_from_one_address_lock_that_address_only(client, settings, kim):
    settings.SIGNIN_MAX_FAILURES_PER_IP = 3
    for login in ["a@riverside.example", "b@riverside.example", "c@riverside.example"]:
        sign_in(client, login, "wrong-password", REMOTE_ADDR="10.1.1.1")
    r = sign_in(client, "kim.lee", REMOTE_ADDR="10.1.1.1")
    assert r.status_code == 200 and signed_in_pk(client) is None
    assert form_error(r) == "Too many failed sign-ins from this network. Try again in 15 minutes."
    assert sign_in(client, "kim.lee", REMOTE_ADDR="10.1.1.2").status_code == 302


def test_the_lockout_also_covers_the_admin_sign_in(client, settings, tenant):
    settings.SIGNIN_MAX_FAILURES = 2
    root = User.objects.create_superuser("root", "root@example.com", PW)
    for _ in range(2):
        sign_in(client, "root", "wrong-password")
    r = client.post("/admin/login/?next=/admin/", {"username": "root", "password": PW})
    assert r.status_code == 200 and signed_in_pk(client) is None
    assert root.check_password(PW)


def test_lockout_keys_do_not_hold_the_typed_login():
    signin.record_failure("Kim@Riverside.example", "10.0.0.1")
    assert not any("riverside" in k or "10.0.0.1" in k for k in locmem._caches[""].keys())


# --- password reset -------------------------------------------------------------------------------------

def test_reset_sends_one_email_whose_link_starts_with_the_app_base_url(client, settings, outbox, kim):
    settings.APP_BASE_URL = "https://ce.example.org"
    r = client.post("/password-reset/", {"email": " KIM@Riverside.example "}, HTTP_HOST="127.0.0.1")
    assert r.status_code == 302 and r["Location"] == SENT
    assert len(outbox) == 1
    mail = outbox[0]
    assert mail.to == ["kim@riverside.example"] and mail.subject == "Reset your Cadence CE password"
    assert reset_link(mail).startswith("https://ce.example.org/password-reset/")
    assert "2 hours" in mail.body and "only once" in mail.body and "ignore" in mail.body and "kim.lee" in mail.body
    page = client.get(SENT)
    assert b"If an account uses that address" in page.content and b"2 hours" in page.content and b"spam" in page.content


def test_the_reset_link_sets_a_new_password_once(client, settings, outbox, kim):
    settings.APP_BASE_URL = "https://ce.example.org"
    client.post("/password-reset/", {"email": "kim@riverside.example"})
    path = reset_link(outbox[0]).removeprefix(settings.APP_BASE_URL)
    token = path.rstrip("/").rsplit("/", 1)[1]
    r = client.get(path)
    assert r.status_code == 302 and r["Location"].endswith("/set-password/") and token not in r["Location"]
    set_url = r["Location"]
    r = client.get(set_url)
    assert r.status_code == 200 and b"Set new password" in r.content and b'content="same-origin"' in r.content
    assert b"at least 12 characters" in r.content
    r = client.post(set_url, {"new_password1": NEW_PW, "new_password2": NEW_PW})
    assert r.status_code == 302 and r["Location"] == reverse("web:password_reset_complete")
    kim.refresh_from_db()
    assert kim.check_password(NEW_PW)
    done = client.get(r["Location"])
    assert b"Your password is set" in done.content and f'href="{reverse("web:login")}"'.encode() in done.content
    for url in [path, set_url]:
        dead = client.get(url)
        assert dead.status_code == 200 and b"expired or was already used" in dead.content and f'href="{reverse("web:password_reset")}"'.encode() in dead.content
    assert sign_in(client, "kim.lee", NEW_PW).status_code == 302


def test_weak_or_mismatched_new_passwords_are_refused_with_the_rules(client, settings, outbox, kim):
    client.post("/password-reset/", {"email": "kim@riverside.example"})
    set_url = client.get(reset_link(outbox[0]).removeprefix(settings.APP_BASE_URL))["Location"]
    for weak, message in [("short", "This password is too short. It must contain at least 12 characters."),
                          ("123456789012", "This password is entirely numeric."),
                          ("password1234", "This password is too common.")]:
        r = client.post(set_url, {"new_password1": weak, "new_password2": weak})
        assert r.status_code == 200 and message in r.content.decode(), weak
    r = client.post(set_url, {"new_password1": NEW_PW, "new_password2": NEW_PW + "x"})
    assert r.status_code == 200 and MISMATCH in r.content.decode()
    kim.refresh_from_db()
    assert kim.check_password(PW)


def test_unknown_deactivated_and_closed_facility_addresses_get_the_same_answer_and_no_email(client, outbox, tenant, other_tenant, kim):
    create_default_roles(other_tenant)
    kim.is_active = False
    kim.save()
    person(other_tenant, "pat", "pat@other.example")
    other_tenant.is_active = False
    other_tenant.save()
    person(tenant, "nopass", "nopass@riverside.example", password=None)  # no password and no invitation: nothing to send
    for email in ["nobody@riverside.example", "kim@riverside.example", "pat@other.example", "nopass@riverside.example"]:
        r = client.post("/password-reset/", {"email": email})
        assert r.status_code == 302 and r["Location"] == SENT, email
    assert outbox == []


def test_invalid_address_syntax_shows_the_error(client, db, outbox):
    r = client.post("/password-reset/", {"email": "not an address"})
    assert r.status_code == 200 and b"Enter a valid email address." in r.content and outbox == []


def test_a_pending_invitation_gets_a_fresh_invitation_instead_of_a_reset(client, settings, outbox, tenant):
    settings.APP_BASE_URL = "https://ce.example.org"
    invited = services.invite_user(tenant, email="ana.diaz@riverside.example", first_name="Ana", last_name="Diaz",
                                   role=Role.unscoped.get(tenant=tenant, slug="technician"))  # unscoped: no request context in tests
    r = client.post("/password-reset/", {"email": "Ana.Diaz@riverside.example"})
    assert r.status_code == 302 and r["Location"] == SENT
    assert len(outbox) == 1 and "invited" in outbox[0].subject
    assert reset_link(outbox[0]).startswith("https://ce.example.org/invite/") and "/password-reset/" not in outbox[0].body
    invited.refresh_from_db()
    assert invited.invited_at is not None and not invited.has_usable_password()


def test_every_account_using_the_address_gets_its_own_email(client, outbox, tenant, other_tenant):
    create_default_roles(other_tenant)
    person(tenant, "kim.r", "kim@shared.example")
    person(other_tenant, "kim.o", "Kim@Shared.example")
    client.post("/password-reset/", {"email": "kim@shared.example"})
    assert sorted(m.to[0] for m in outbox) == ["Kim@shared.example", "kim@shared.example"]  # each to its stored address
    assert {"kim.r", "kim.o"} == {re.search(r"Your username is (\S+)\.", m.body).group(1) for m in outbox}


def test_per_address_limit_drops_extra_emails_but_answers_the_same(client, settings, outbox, kim):
    settings.PASSWORD_RESET_MAX_PER_HOUR = 2
    for i in range(3):
        r = client.post("/password-reset/", {"email": "kim@riverside.example"}, REMOTE_ADDR=f"10.3.0.{i}")
        assert r.status_code == 302 and r["Location"] == SENT
    assert len(outbox) == 2


def test_per_caller_limit_drops_extra_emails_but_answers_the_same(client, settings, outbox, tenant):
    settings.PASSWORD_RESET_MAX_PER_IP_PER_HOUR = 2
    for name in ["a", "b", "c"]:
        person(tenant, name, f"{name}@riverside.example")
    for name in ["a", "b", "c"]:
        r = client.post("/password-reset/", {"email": f"{name}@riverside.example"}, REMOTE_ADDR="10.2.2.2")
        assert r.status_code == 302 and r["Location"] == SENT
    assert [m.to for m in outbox] == [["a@riverside.example"], ["b@riverside.example"]]
    client.post("/password-reset/", {"email": "c@riverside.example"}, REMOTE_ADDR="10.2.2.3")
    assert outbox[-1].to == ["c@riverside.example"]


def test_an_email_outage_still_answers_the_same(client, monkeypatch, outbox, kim):
    def refuse(self, fail_silently=False):
        raise smtplib.SMTPServerDisconnected("down")

    monkeypatch.setattr("apps.accounts.emails.EmailMessage.send", refuse)
    r = client.post("/password-reset/", {"email": "kim@riverside.example"})
    assert r.status_code == 302 and r["Location"] == SENT
    assert signin.send_password_reset(kim) is False


def test_a_reset_clears_the_lockout(client, settings, outbox, kim):
    settings.SIGNIN_MAX_FAILURES = 2
    for login in ["kim@riverside.example", "kim.lee"]:
        for _ in range(2):
            sign_in(client, login, "wrong-password")
        assert signin.is_locked(login, None)
    client.post("/password-reset/", {"email": "kim@riverside.example"})
    set_url = client.get(reset_link(outbox[0]).removeprefix(settings.APP_BASE_URL))["Location"]
    assert client.post(set_url, {"new_password1": NEW_PW, "new_password2": NEW_PW}).status_code == 302
    assert sign_in(client, "Kim@Riverside.example", NEW_PW).status_code == 302
    client.logout()
    assert sign_in(client, "kim.lee", NEW_PW).status_code == 302


def test_malformed_reset_links_read_as_dead_links(client, kim):
    for uid in ["zz", "_-_-", "OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5", "MQ"]:
        r = client.get(f"/password-reset/{uid}/not-a-token/")
        assert r.status_code == 200 and b"expired or was already used" in r.content, uid
    r = client.get("/password-reset/MQ/set-password/")  # no token in the session
    assert r.status_code == 200 and b"expired or was already used" in r.content


def test_a_link_stops_working_when_the_facility_is_deactivated(client, settings, outbox, tenant, kim):
    client.post("/password-reset/", {"email": "kim@riverside.example"})
    tenant.is_active = False
    tenant.save()
    r = client.get(reset_link(outbox[0]).removeprefix(settings.APP_BASE_URL))
    assert r.status_code == 200 and b"expired or was already used" in r.content


# --- change password (signed in) ---------------------------------------------------------------------------

def test_change_password_needs_sign_in_and_a_facility(client, django_user_model):
    r = client.get("/account/password/")
    assert r.status_code == 302 and r["Location"].startswith("/login/")
    client.force_login(django_user_model.objects.create_superuser("root", "root@example.com", PW))
    assert b"No tenant selected" in client.get("/account/password/").content


def test_change_password_page_shows_the_rules_and_the_user_menu_links_to_it(client, kim):
    client.force_login(kim)
    r = client.get("/account/password/")
    assert r.status_code == 200 and b"<h1>Change password</h1>" in r.content
    html = r.content.decode()
    assert "Current password" in html and "Confirm new password" in html and "at least 12 characters" in html
    menu = html.split('class="user-menu"', 1)[1].split("</details>", 1)[0]
    assert f'href="{reverse("web:password_change")}"' in menu and "Change password" in menu


def test_change_password_link_sits_above_admin_for_staff(client, kim):
    kim.is_staff = True
    kim.save()
    client.force_login(kim)
    menu = client.get("/").content.decode().split('class="user-menu"', 1)[1]
    assert menu.index("Change password") < menu.index(">Admin<")


def test_change_password_requires_the_current_password(client, kim):
    client.force_login(kim)
    r = client.post("/account/password/", {"old_password": "not-it", "new_password1": NEW_PW, "new_password2": NEW_PW})
    assert r.status_code == 200 and "That is not your current password." in r.content.decode()
    r = client.post("/account/password/", {"old_password": PW, "new_password1": "short", "new_password2": "short"})
    assert r.status_code == 200 and "This password is too short." in r.content.decode()
    kim.refresh_from_db()
    assert kim.check_password(PW)


def test_change_password_keeps_this_session_and_signs_out_the_others(client, kim):
    elsewhere = Client()
    elsewhere.force_login(kim)
    client.force_login(kim)
    r = client.post("/account/password/", {"old_password": PW, "new_password1": NEW_PW, "new_password2": NEW_PW})
    assert r.status_code == 302 and r["Location"] == reverse("web:password_change") + "?changed=1"
    r = client.get(r["Location"])
    assert r.status_code == 200 and b"Your password is changed" in r.content and b"signed out" in r.content
    kim.refresh_from_db()
    assert kim.check_password(NEW_PW)
    assert client.get("/").status_code == 200
    r = elsewhere.get("/")
    assert r.status_code == 302 and r["Location"].startswith("/login/")


def test_wrong_current_passwords_count_toward_the_lockout(client, settings, kim):
    settings.SIGNIN_MAX_FAILURES = 2
    client.force_login(kim)
    for _ in range(2):
        client.post("/account/password/", {"old_password": "not-it", "new_password1": NEW_PW, "new_password2": NEW_PW})
    r = client.post("/account/password/", {"old_password": PW, "new_password1": NEW_PW, "new_password2": NEW_PW})
    assert r.status_code == 200 and LOCKED in r.content.decode()
    kim.refresh_from_db()
    assert kim.check_password(PW)


def test_signed_out_forms_pass_csrf_with_a_browser_origin(client, tenant, make_user):
    """Browsers send an Origin header with form posts; with the referrer policy the pages declare it is the site's own
    origin (no-referrer would make it "null", which Django's CSRF check refuses). Sign in with CSRF checks on."""
    from django.test import Client

    make_user("director", username="kim@riverside.example")
    browser = Client(enforce_csrf_checks=True)
    page = browser.get("/login/")
    assert b'<meta name="referrer" content="same-origin">' in page.content
    token = page.cookies["csrftoken"].value
    r = browser.post("/login/", {"username": "kim@riverside.example", "password": "Test-Pass-2026-x", "csrfmiddlewaretoken": token},
                     HTTP_ORIGIN="http://testserver")
    assert r.status_code == 302
    refused = Client(enforce_csrf_checks=True)
    refused.get("/login/")
    r = refused.post("/login/", {"username": "kim@riverside.example", "password": "x", "csrfmiddlewaretoken": refused.cookies["csrftoken"].value},
                     HTTP_ORIGIN="null")
    assert r.status_code == 403  # what no-referrer would have caused in a real browser
