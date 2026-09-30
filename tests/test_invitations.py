"""Invitations (slice 10): the invitation email, the accept page that sets the first password, Resend invite, and bootstrap_tenant --invite."""
import re
import smtplib
from datetime import timedelta
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import invitations, services
from apps.accounts.models import Role, User, create_default_roles
from apps.tenants.models import Tenant

HX = {"HTTP_HX_REQUEST": "true"}
PASSWORD = "Tidal-Lantern-Quartz-26"
APP = "https://cadence.example.org"


@pytest.fixture(autouse=True)
def _app_base_url(settings):
    settings.APP_BASE_URL = APP


@pytest.fixture
def role(tenant):
    def _get(slug, tenant_=None):
        return Role.unscoped.get(tenant=tenant_ or tenant, slug=slug)  # unscoped: fixtures run outside a request context

    return _get


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **kw):
        user = make_user(role_slug, **kw)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def invitee(ctx, role):
    return services.invite_user(ctx, email="maria.santos@riverside.example", first_name="Maria", last_name="Santos", role=role("technician"))


def _link(message) -> str:
    return re.search(r"https?://\S+/invite/\S+/\S+/", message.body).group(0)


def _path(url: str) -> str:
    return urlsplit(url).path


def _sent_link(user, mailoutbox) -> str:
    assert invitations.send_invitation(user) is True
    return _path(_link(mailoutbox[-1]))


def _open(client, path) -> str:
    """Follow the emailed link the way a browser does: the token moves into the session and the form lives at .../set-password/."""
    r = client.get(path)
    assert r.status_code == 302 and r["Location"].endswith("/set-password/"), r
    return r["Location"]


def _is_dead(client, path) -> bool:
    r = client.get(path)
    return r.status_code == 200 and "This link no longer works" in r.content.decode() and 'name="new_password1"' not in r.content.decode()


def _broken_mail(monkeypatch):
    def boom(self, fail_silently=False):
        raise smtplib.SMTPException("mail server unreachable")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", boom)


# --- sending -------------------------------------------------------------------------------------

def test_invite_sends_one_email_whose_link_uses_the_app_base_url(client, settings, signed_in, role, mailoutbox):
    settings.ALLOWED_HOSTS = ["*"]
    director = signed_in("director")
    r = client.post("/users/invite/", {"first_name": "Maria", "last_name": "Santos", "email": "MSantos@riverside.example",
                                       "role": str(role("technician").id), "department": "Clinical Engineering"}, HTTP_HOST="attacker.example", **HX)
    assert r.status_code == 200 and "Invitation sent to msantos@riverside.example" in r["HX-Trigger"] and "users-changed" in r["HX-Trigger"]
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    user = User.objects.get(username="msantos@riverside.example")
    assert invitations.is_pending(user) and user.invited_at is not None
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.to == ["msantos@riverside.example"] and mail.subject == "You're invited to Cadence CE at Riverside Regional"
    link = _link(mail)
    assert link.startswith(f"{APP}/invite/") and "attacker.example" not in mail.body
    assert "Director User invited you" in mail.body and "as Technician" in mail.body and "works for 7 days" in mail.body
    assert director.get_full_name() in mail.body and "Your sign-in is msantos@riverside.example" in mail.body


def test_invitation_link_sets_the_password_signs_in_and_is_then_used_up(client, invitee, mailoutbox):
    path = _sent_link(invitee, mailoutbox)
    form_url = _open(client, path)
    assert "/invite/" in form_url and path.split("/")[3] not in form_url  # the token is not in the address of the form page
    page = client.get(form_url)
    body = page.content.decode()
    assert page.status_code == 200 and "Maria Santos" in body and "maria.santos@riverside.example" in body and "Riverside Regional" in body
    assert 'name="new_password1" autocomplete="new-password"' in body and "Set password and sign in</button>" in body
    assert "at least 12 characters" in body and 'content="same-origin"' in body
    r = client.post(form_url, {"new_password1": PASSWORD, "new_password2": PASSWORD})
    assert r.status_code == 302 and r["Location"] == "/"
    invitee.refresh_from_db()
    assert invitee.check_password(PASSWORD) and invitee.last_login is not None and services.user_status(invitee) == "active"
    assert client.session["_auth_user_id"] == str(invitee.pk)
    assert client.get("/").status_code == 200  # signed in on the Overview
    assert _is_dead(client, path)
    client.logout()
    assert _is_dead(client, path)
    assert client.login(username="Maria.Santos@riverside.example", password=PASSWORD)


def test_invitation_link_expires_after_its_own_lifetime(client, settings, invitee, mailoutbox, monkeypatch):
    path = _sent_link(invitee, mailoutbox)
    real_now = invitations.invitation_tokens._now
    days = settings.INVITATION_VALID_DAYS
    # far past PASSWORD_RESET_TIMEOUT (two hours, for reset links) but inside the invitation's own lifetime
    monkeypatch.setattr(invitations.invitation_tokens, "_now", lambda: real_now() + timedelta(days=days, hours=-1))
    _open(client, path)
    monkeypatch.setattr(invitations.invitation_tokens, "_now", lambda: real_now() + timedelta(days=days, minutes=1))
    assert _is_dead(client, path)


def test_a_resend_replaces_the_earlier_link(client, signed_in, invitee, mailoutbox):
    first = _sent_link(invitee, mailoutbox)
    signed_in("director")
    r = client.post(f"/users/{invitee.pk}/resend-invite/?status=invited", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "Invitation resent to maria.santos@riverside.example" in r["HX-Trigger"]
    assert 'id="users-body"' in body and "Maria Santos" in body and 'hx-get="/users/?status=invited"' in body  # the list keeps its filters
    assert len(mailoutbox) == 2 and mailoutbox[1].to == ["maria.santos@riverside.example"]
    second = _path(_link(mailoutbox[1]))
    assert second != first
    client.logout()
    assert _is_dead(client, first)
    _open(client, second)


def test_deactivating_the_account_kills_the_link(client, invitee, mailoutbox):
    path = _sent_link(invitee, mailoutbox)
    services.deactivate_user(invitee)
    assert _is_dead(client, path)


def test_an_inactive_facility_kills_the_link(client, tenant, invitee, mailoutbox):
    path = _sent_link(invitee, mailoutbox)
    tenant.is_active = False
    tenant.save()
    assert _is_dead(client, path)


def test_a_link_opened_then_deactivated_cannot_set_the_password(client, invitee, mailoutbox):
    form_url = _open(client, _sent_link(invitee, mailoutbox))
    services.deactivate_user(invitee)
    r = client.post(form_url, {"new_password1": PASSWORD, "new_password2": PASSWORD})
    invitee.refresh_from_db()
    assert r.status_code == 200 and "This link no longer works" in r.content.decode() and not invitee.has_usable_password()
    assert "_auth_user_id" not in client.session


def test_accounts_that_are_not_pending_invitations_cannot_use_a_link(client, invitee, mailoutbox, make_user):
    path = _sent_link(invitee, mailoutbox)
    invitee.set_password(PASSWORD)  # an administrator set the password in Admin
    invitee.save()
    assert _is_dead(client, path)
    # an ordinary account never gets a working link, even with a correctly signed token
    member = make_user("technician")
    uid = urlsafe_base64_encode(force_bytes(member.pk))
    assert _is_dead(client, f"/invite/{uid}/{invitations.invitation_tokens.make_token(member)}/")
    with pytest.raises(ValidationError, match="already has a password"):
        invitations.send_invitation(member)


@pytest.mark.parametrize("uid, token", [
    ("garbage", "garbage"),
    ("!!!!", "1-2-3"),
    (urlsafe_base64_encode(b"\xff\xfe\xfd"), "abc-def"),
    (urlsafe_base64_encode(b"not-a-number"), "abc-def"),
    (urlsafe_base64_encode(b"99999999999999999999999999"), "abc-def"),
    (urlsafe_base64_encode(b"-1"), "zzzzzzzzzzzzzzzzzz-abc"),
])
def test_malformed_links_render_the_invalid_page(client, db, uid, token):
    assert _is_dead(client, f"/invite/{uid}/{token}/")


def test_a_wrong_token_or_a_bare_set_password_url_is_invalid(client, invitee, mailoutbox):
    _sent_link(invitee, mailoutbox)
    uid = urlsafe_base64_encode(force_bytes(invitee.pk))
    assert _is_dead(client, f"/invite/{uid}/abc-0123456789abcdef/")
    assert _is_dead(client, f"/invite/{uid}/set-password/")  # no token in this session
    r = client.post(f"/invite/{uid}/set-password/", {"new_password1": PASSWORD, "new_password2": PASSWORD})
    invitee.refresh_from_db()
    assert r.status_code == 200 and not invitee.has_usable_password()


def test_invitation_and_password_reset_tokens_are_not_interchangeable(client, invitee, mailoutbox):
    path = _sent_link(invitee, mailoutbox)
    uid = urlsafe_base64_encode(force_bytes(invitee.pk))
    assert _is_dead(client, f"/invite/{uid}/{default_token_generator.make_token(invitee)}/")
    invitation_token = path.rstrip("/").split("/")[-1]
    assert invitations.invitation_tokens.check_token(invitee, invitation_token)
    assert not default_token_generator.check_token(invitee, invitation_token)
    assert not invitations.invitation_tokens.check_token(invitee, default_token_generator.make_token(invitee))


def test_user_for_link(invitee, mailoutbox):
    path = _sent_link(invitee, mailoutbox)
    _, _, uid, token, _ = path.split("/")
    assert invitations.user_for_link(uid, token) == invitee
    assert invitations.user_for_link(uid, "abc-def") is None
    assert invitations.user_for_link("garbage", token) is None


def test_weak_or_mismatched_passwords_show_errors_and_do_not_sign_in(client, invitee, mailoutbox):
    form_url = _open(client, _sent_link(invitee, mailoutbox))
    r = client.post(form_url, {"new_password1": PASSWORD, "new_password2": PASSWORD + "x"})
    assert r.status_code == 200 and "didn’t match" in r.content.decode()
    r = client.post(form_url, {"new_password1": "password", "new_password2": "password"})
    body = r.content.decode()
    assert r.status_code == 200 and "too short" in body and "too common" in body and 'class="err"' in body
    invitee.refresh_from_db()
    assert not invitee.has_usable_password() and "_auth_user_id" not in client.session and services.user_status(invitee) == "invited"
    # the link still works for another try
    assert client.post(form_url, {"new_password1": PASSWORD, "new_password2": PASSWORD}).status_code == 302


# --- when email is down ----------------------------------------------------------------------------

def test_email_failure_still_creates_the_account_and_says_so(client, signed_in, role, mailoutbox, monkeypatch):
    signed_in("director")
    _broken_mail(monkeypatch)
    r = client.post("/users/invite/", {"first_name": "Maria", "last_name": "Santos", "email": "msantos@riverside.example",
                                       "role": str(role("technician").id), "department": "Clinical Engineering"}, **HX)
    assert r.status_code == 200 and "users-changed" in r["HX-Trigger"] and "modal-close" in r["HX-Trigger-After-Settle"]
    assert "Account created for msantos@riverside.example, but the invitation email could not be sent. Use Resend invite once email is working." \
        in r["HX-Trigger"]
    user = User.objects.get(username="msantos@riverside.example")
    assert invitations.is_pending(user) and mailoutbox == []
    r = client.post(f"/users/{user.pk}/resend-invite/", **HX)
    assert r.status_code == 200 and "could not be sent" in r["HX-Trigger"] and mailoutbox == []
    monkeypatch.undo()
    r = client.post(f"/users/{user.pk}/resend-invite/", **HX)
    assert "Invitation resent to msantos@riverside.example" in r["HX-Trigger"] and len(mailoutbox) == 1


# --- Resend invite -------------------------------------------------------------------------------

def test_resend_invite_permissions_and_rules(client, signed_in, invitee, make_user, mailoutbox):
    signed_in("manager")  # Users View
    assert client.post(f"/users/{invitee.pk}/resend-invite/", **HX).status_code == 403
    signed_in("director")
    assert client.get(f"/users/{invitee.pk}/resend-invite/").status_code == 405
    member = make_user("technician")
    r = client.post(f"/users/{member.pk}/resend-invite/", **HX)
    assert r.status_code == 200 and "already has a password or is deactivated" in r["HX-Trigger"]
    services.deactivate_user(invitee)
    r = client.post(f"/users/{invitee.pk}/resend-invite/", **HX)
    assert r.status_code == 200 and "there is no invitation to send" in r["HX-Trigger"] and mailoutbox == []


def test_resend_invite_is_tenant_isolated(client, invitee, role, make_user, other_tenant, mailoutbox):
    create_default_roles(other_tenant)
    theirs = make_user("director", tenant_=other_tenant)
    client.force_login(theirs)
    assert client.post(f"/users/{invitee.pk}/resend-invite/", **HX).status_code == 404
    their_invitee = services.invite_user(other_tenant, email="new@other.example", first_name="N", last_name="O", role=role("requester", other_tenant))
    client.force_login(make_user("director"))
    assert client.post(f"/users/{their_invitee.pk}/resend-invite/", **HX).status_code == 404
    assert mailoutbox == []


# --- bootstrap_tenant --invite -----------------------------------------------------------------------

def test_bootstrap_tenant_invite_emails_the_director_and_prints_the_link(client, db, mailoutbox):
    out = StringIO()
    call_command("bootstrap_tenant", "--name", "Lakeside General", "--slug", "lakeside", "--admin-email", "dir@lakeside.example", "--invite", stdout=out)
    text = out.getvalue()
    user = User.objects.get(username="dir@lakeside.example")
    assert invitations.is_pending(user) and user.email == "dir@lakeside.example" and user.role.slug == "director" and user.tenant.slug == "lakeside"
    assert "password:" not in text and "Invitation email sent to dir@lakeside.example" in text
    assert len(mailoutbox) == 1 and mailoutbox[0].subject == "You're invited to Cadence CE at Lakeside General"
    printed = re.search(rf"{re.escape(APP)}/invite/\S+/", text).group(0)
    _open(client, _path(printed))
    # an existing user is left alone and nothing is sent
    again = StringIO()
    call_command("bootstrap_tenant", "--name", "Lakeside General", "--slug", "lakeside", "--admin-email", "dir@lakeside.example", "--invite", stdout=again)
    assert "User dir@lakeside.example already existed" in again.getvalue() and "invite/" not in again.getvalue() and len(mailoutbox) == 1


def test_bootstrap_tenant_invite_prints_a_working_link_when_email_fails(client, db, mailoutbox, monkeypatch):
    _broken_mail(monkeypatch)
    out = StringIO()
    call_command("bootstrap_tenant", "--name", "Lakeside General", "--slug", "lakeside", "--admin-email", "dir@lakeside.example", "--invite", stdout=out)
    text = out.getvalue()
    assert "could not be sent" in text and mailoutbox == []
    _open(client, _path(re.search(rf"{re.escape(APP)}/invite/\S+/", text).group(0)))


def test_bootstrap_tenant_rejects_invite_with_password_and_keeps_the_password_mode(db, mailoutbox):
    with pytest.raises(CommandError):
        call_command("bootstrap_tenant", "--name", "L", "--slug", "lakeside", "--admin-email", "d@l.example", "--invite", "--password", "Xy-1234567890")
    with pytest.raises(CommandError, match="either --invite or --password"):
        call_command("bootstrap_tenant", name="L", slug="lakeside", admin_email="d@l.example", invite=True, password="Xy-1234567890", stdout=StringIO())
    assert not Tenant.objects.filter(slug="lakeside").exists()
    out = StringIO()
    call_command("bootstrap_tenant", "--name", "L", "--slug", "lakeside", "--admin-email", "d@l.example", "--password", "Xy-1234567890-z", stdout=out)
    user = User.objects.get(username="d@l.example")
    assert user.check_password("Xy-1234567890-z") and not user.is_invited and "password: Xy-1234567890-z" in out.getvalue() and mailoutbox == []


def test_send_invitation_rotates_invited_at(invitee, mailoutbox):
    assert invitee.invited_at is None
    before = timezone.now()
    invitations.send_invitation(invitee)
    invitee.refresh_from_db()
    assert invitee.invited_at >= before


def test_a_withdrawn_invitation_stays_dead_after_reactivation(ctx, role):
    """Deactivating a pending invitation withdraws it for good: reactivating the account does not revive the old link."""
    user = services.invite_user(ctx, email="wrong@riverside.example", first_name="W", last_name="R", role=role("requester"))
    invitations.send_invitation(user)
    uidb64, token = invitations.invitation_url(user).rstrip("/").split("/")[-2:]
    assert invitations.user_for_link(uidb64, token) == user
    services.deactivate_user(user)
    services.reactivate_user(user)
    user.refresh_from_db()
    assert invitations.is_pending(user)
    assert invitations.user_for_link(uidb64, token) is None
    invitations.send_invitation(user)  # Resend invite still works
    assert invitations.user_for_link(*invitations.invitation_url(user).rstrip("/").split("/")[-2:]) == user
