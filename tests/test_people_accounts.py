"""
Joining a facility (slice 22, part A; apps.accounts.people): an invitation (or bootstrap_tenant) for an address another facility's
account uses adds an account for that same person, and the inviting facility cannot tell; the person who already signs in joins
from an "added to" email's join page, never a set-password link; a password reset sends a person one link, for the account a
sign-in opens; the person has one lockout count; and the wording, Admin, the facilities API, and the demo follow.
"""
import re
import uuid
from collections import Counter
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import Client
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts import invitations, people, services, signin
from apps.accounts.backends import find_account
from apps.accounts.models import AccessEvent, Role, User, create_default_roles
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web.forms_account import WRONG_LOGIN

PASSWORD = "Test-Pass-2026-x"
NEW_PASSWORD = "Harbor-Lantern-7731"
EMAIL = "kim@health.example"
APP = "https://cadence.example.org"
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.APP_BASE_URL = APP
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # many sign-ins; the logic is the same


def facility(name, slug):
    t = Tenant.objects.create(name=name, slug=slug)
    create_default_roles(t)
    return t


@pytest.fixture
def lakeside(db):
    return facility("Lakeside Surgery Center", "lakeside")


@pytest.fixture
def hillside(db):
    return facility("Hillside Clinic", "hillside")


def role(tenant, slug):
    return Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: test setup, outside any facility


def account(tenant, slug="director", *, username=EMAIL, email=EMAIL, person=None, pending=False, last_login=True):
    """An account as the services leave one: a pending invitation (no password, never signed in), or one that has signed in."""
    user = User(username=username, email=email, first_name="Kim", last_name="Alvarez", tenant=tenant, role=role(tenant, slug), person=person,
                is_invited=pending)
    if pending:
        user.set_unusable_password()
    else:
        user.set_password(PASSWORD)
    user.last_login = None if pending or not last_login else (last_login if last_login is not True else timezone.now())
    user._password = None  # created, not changed: nothing to share yet
    user.save()
    return user


@pytest.fixture
def kim(tenant):
    """Kim, Riverside's director, in one facility so far (as every account was before slice 22)."""
    return account(tenant)


def invite(tenant, email=EMAIL, slug="technician", **kw):
    with tenant_context(tenant):
        return services.invite_user(tenant, email=email, first_name=kw.pop("first_name", "Kim"), last_name=kw.pop("last_name", "Alvarez"),
                                    role=role(tenant, slug), **kw)


def events(tenant, **filters):
    return list(AccessEvent.unscoped.filter(tenant=tenant, **filters).order_by("at", "pk"))  # unscoped: the test reads one facility


def link_path(mail) -> str:
    return urlsplit(re.search(r"https?://\S+", mail.body).group(0)).path


# --- linking ---------------------------------------------------------------------------------------------------------------------

def test_an_invitation_for_an_address_another_facility_uses_adds_an_account_for_that_person(tenant, kim, lakeside):
    before = User.objects.filter(pk=kim.pk).values().get()
    b = invite(lakeside, email="  KIM@health.example ")
    kim.refresh_from_db()
    assert kim.person and b.person == kim.person and b.tenant == lakeside and b.role.slug == "technician"
    assert (b.username, b.email) == (f"{EMAIL}@lakeside", EMAIL) and invitations.is_pending(b) and people.is_pending(b)
    # nothing of Riverside's account changes but the person it now shares, and Riverside's log gets nothing
    after = User.objects.filter(pk=kim.pk).values().get()
    assert {k for k in before if before[k] != after[k]} == {"person"} and events(tenant) == []
    assert [e.action for e in events(lakeside)] == [AccessEvent.Action.INVITED]
    assert [(m["name"], m["invited"]) for m in people.facility_menu(kim)] == [("Lakeside Surgery Center", True), ("Riverside Regional", False)]
    assert find_account(EMAIL) == kim  # a pending invitation never lands a sign-in


def test_a_third_facility_joins_the_same_person(tenant, kim, lakeside, hillside):
    b = invite(lakeside)
    c = invite(hillside, slug="manager")
    assert c.person == b.person == User.objects.get(pk=kim.pk).person and c.username == f"{EMAIL}@hillside"


def test_only_the_exact_stored_email_links(tenant, lakeside, hillside):
    """Another letter case, a username, or a superuser's address is never the same person: the account is one of its own, as before."""
    other = account(tenant, username="kim.a", email="Kim@Health.example")
    b = invite(lakeside)
    assert b.person is None and b.username == EMAIL and User.objects.get(pk=other.pk).person is None
    User.objects.create_superuser(username="root", email="root@health.example", password=PASSWORD)
    assert invite(hillside, email="root@health.example").person is None


def test_an_address_naming_several_accounts_gets_an_unlinked_one_or_the_usual_refusal(tenant, lakeside, hillside, db):
    """Two unlinked accounts, or two people, under the address: nobody is the person for sure. The new account is one of its own
    when its username is free, else today's refusal, which says nothing about why."""
    a = account(tenant, username="kim.a")
    c = account(hillside, username="kim.c")
    b = invite(lakeside)
    assert b.person is None and b.username == EMAIL
    assert User.objects.filter(pk__in=[a.pk, c.pk], person__isnull=False).count() == 0
    west = facility("West Clinic", "west")
    User.objects.filter(pk=a.pk).update(person=uuid.uuid4())
    User.objects.filter(pk=c.pk).update(person=uuid.uuid4())
    User.objects.filter(pk=b.pk).update(email="moved@health.example", username="moved@health.example")
    account(facility("East Clinic", "east"), username=EMAIL, person=uuid.uuid4())  # a third person holds the address as username
    before = User.objects.count()
    with pytest.raises(ValidationError, match="That address cannot be used for a new account here. Contact support."):
        invite(west)
    assert User.objects.count() == before and events(west) == []


def test_an_admin_made_username_without_the_address_is_the_usual_refusal(tenant, lakeside):
    account(tenant, username=EMAIL, email="")
    with pytest.raises(ValidationError, match="cannot be used for a new account here"):
        invite(lakeside)


def test_a_person_already_here_under_another_address_is_refused(tenant, kim, lakeside):
    b = invite(lakeside, email="kim.alvarez@lakeside.example")
    User.objects.filter(pk=b.pk).update(person=uuid.uuid4())
    User.objects.filter(pk=kim.pk).update(person=User.objects.get(pk=b.pk).person)  # Kim, at Lakeside under her other address
    with pytest.raises(ValidationError, match="cannot be used for a new account here"):
        invite(lakeside)
    assert [e.action for e in events(lakeside)] == [AccessEvent.Action.INVITED]


# --- the inviting facility cannot tell ------------------------------------------------------------------------------------------

def _row(html: str, user) -> str:
    row = re.search(rf'<tr id="user-{user.pk}">.*?</tr>', html, re.S).group(0)
    return row.replace(f"user-{user.pk}", "user-<pk>").replace(f"/{user.pk}/", "/<pk>/").replace(user.email, "<email>")


def _api_row(rows, user) -> dict:
    row = next(r for r in rows if r["id"] == user.pk)
    return {k: v for k, v in row.items() if k not in ("id", "email")}


def _view(client, tenant, linked, fresh) -> dict:
    """Everything the inviting facility's director sees of the two invitations, with their own address and id taken out. The two
    have the same name, so the change log's entries (which name the account) come in identical pairs."""
    html = client.get("/users/").content.decode()
    api = client.get("/api/v1/users/?status=invited").json()["results"]
    log = Counter(str({k: v for k, v in e.items() if k != "at"}).replace(linked.email, "<email>").replace(fresh.email, "<email>")
                  for e in client.get("/api/v1/change-log/?area=access").json()["results"])
    return {"rows": [_row(html, linked), _row(html, fresh)], "api": [_api_row(api, linked), _api_row(api, fresh)],
            "status": [services.user_status(User.objects.get(pk=u.pk)) for u in (linked, fresh)],
            "resend": [invitations.is_pending(User.objects.get(pk=u.pk)) for u in (linked, fresh)],
            "events": [[(e.action, e.detail.replace(u.email, "<email>"), e.by_id, e.role_id) for e in events(tenant, user=u)] for u in (linked, fresh)],
            "log": [sorted(log.items()), sorted((entry, n) for entry, n in log.items() if n % 2 == 0)]}


def _same(view) -> None:
    for key, (linked, fresh) in view.items():
        assert linked == fresh, key


def test_the_inviting_facility_sees_the_same_for_an_address_used_elsewhere_and_a_new_one(client, tenant, kim, lakeside, mailoutbox):
    director = account(lakeside, username="lee@lakeside.example", email="lee@lakeside.example")
    User.objects.filter(pk=director.pk).update(first_name="Lee", last_name="Park")
    client.force_login(director)
    answers = {}
    for email, first in ((EMAIL, "Kim"), ("ana@health.example", "Ana")):
        r = client.post("/users/invite/", {"first_name": first, "last_name": "Alvarez", "email": email, "role": str(role(lakeside, "technician").id),
                                           "department": "Clinical Engineering"}, **HX)
        answers[email] = (r.status_code, r["HX-Trigger"].replace(email, "<email>"), r.get("HX-Trigger-After-Settle"), r.content)
    assert answers[EMAIL] == answers["ana@health.example"]
    linked, fresh = User.objects.get(tenant=lakeside, email=EMAIL), User.objects.get(tenant=lakeside, email="ana@health.example")
    assert linked.person == User.objects.get(pk=kim.pk).person and fresh.person is None  # one linked, one not, and nothing says so
    # the same names, so the rows compare whole
    User.objects.filter(pk=fresh.pk).update(first_name="Kim")
    fresh.refresh_from_db()
    first = _view(client, lakeside, linked, fresh)
    _same(first)
    # Resend invite: the same toast and event, whichever email went out
    for user in (linked, fresh):
        r = client.post(f"/users/{user.pk}/resend-invite/", **HX)
        answers[user.email] = r["HX-Trigger"].replace(user.email, "<email>")
    assert answers[EMAIL] == answers["ana@health.example"] and "Invitation resent to <email>" in answers[EMAIL]
    _same(_view(client, lakeside, linked, fresh))
    # a password reset asked for either address, signed out: still the same, and the log gets nothing
    events_before = len(events(lakeside))
    for email in (EMAIL, "ana@health.example"):
        assert Client().post("/password-reset/", {"email": email}).status_code == 302
    after = _view(client, lakeside, linked, fresh)
    _same(after)
    assert len(events(lakeside)) == events_before and events(tenant) == []
    assert after["rows"] == _view(client, lakeside, linked, fresh)["rows"]


# --- the invitation email ---------------------------------------------------------------------------------------------------------

def test_someone_who_signs_in_elsewhere_is_emailed_the_join_page(tenant, kim, lakeside, mailoutbox):
    b = invite(lakeside, by=None)
    assert invitations.send_invitation(b)
    mail = mailoutbox[-1]
    assert mail.to == [EMAIL] and mail.subject == "You're added to Lakeside Surgery Center in Cadence CE"
    assert f"{APP}/account/facility/{b.pk}/join/" in mail.body and "/invite/" not in mail.body
    assert "as Technician" in mail.body and f"Sign in as usual, with {EMAIL}" in mail.body and "no new password to set" in mail.body
    assert invitations.link_for(b) == invitations.join_url(b) == f"{APP}/account/facility/{b.pk}/join/"
    # a resend: the same event as any invitation's
    assert invitations.send_invitation(b, resend=True)
    e = events(lakeside, action=AccessEvent.Action.INVITATION_RESENT)
    assert len(e) == 1 and e[0].detail == f"A new link to {EMAIL} replaces the earlier one"


def test_the_join_link_works_once_signed_in(client, tenant, kim, lakeside, mailoutbox):
    b = invite(lakeside)
    invitations.send_invitation(b)
    path = link_path(mailoutbox[-1])
    r = client.get(path)
    assert r.status_code == 302 and r["Location"].startswith("/login/")
    client.post("/login/", {"username": EMAIL, "password": PASSWORD})
    assert "Join Lakeside Surgery Center" in client.get(path).content.decode()
    assert client.post("/account/facility/", {"account": b.pk}).status_code == 302
    b.refresh_from_db()
    assert people.is_joined(b) and b.check_password(PASSWORD)


def test_a_person_who_cannot_sign_in_yet_gets_the_ordinary_invitation(client, tenant, lakeside, mailoutbox):
    """Invited to two facilities before choosing a password: each email sets it, and the one accepted first sets it for the person;
    the other facility's link then stops working, and that facility waits in the facility menu."""
    a = invite(tenant, email=EMAIL, slug="director")
    b = invite(lakeside)
    assert b.person == User.objects.get(pk=a.pk).person and not invitations.joins_signed_in(b)
    invitations.send_invitation(a)
    invitations.send_invitation(b)
    riverside_link, lakeside_link = (link_path(m) for m in mailoutbox)
    assert all(m.subject.startswith("You're invited") for m in mailoutbox) and "/invite/" in lakeside_link
    form_url = client.get(lakeside_link)["Location"]
    assert client.post(form_url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}).status_code == 302
    b.refresh_from_db()
    assert int(client.session["_auth_user_id"]) == b.pk and b.check_password(NEW_PASSWORD)
    a.refresh_from_db()
    assert a.password.startswith(UNUSABLE_PASSWORD_PREFIX) and people.is_pending(a)  # its emailed link no longer works
    r = client.get(riverside_link)
    assert r.status_code == 200 and "This link no longer works" in r.content.decode()
    assert [(m["name"], m["invited"]) for m in people.facility_menu(b)] == [("Lakeside Surgery Center", False), ("Riverside Regional", True)]


def test_a_set_password_link_never_opens_for_someone_who_signs_in_elsewhere(client, tenant, lakeside, mailoutbox):
    """A link sent while the person had no password, still signed and unexpired, once they have one (here set without the shared
    write, as a concurrent change could leave it): the page says to sign in as usual, and nothing changes."""
    a = invite(tenant, email=EMAIL, slug="director")
    b = invite(lakeside)
    invitations.send_invitation(b)
    path = link_path(mailoutbox[-1])
    uid, token = path.strip("/").split("/")[1:3]
    a.set_password(PASSWORD)
    User.objects.filter(pk=a.pk).update(password=a.password, is_invited=False, last_login=timezone.now())
    b.refresh_from_db()
    assert invitations.invitation_tokens.check_token(b, token) and invitations.pending_user_from_uid(uid) is None
    body = client.get(path).content.decode()
    assert "This link no longer works" in body and "Sign in</a> as usual" in body and 'name="new_password1"' not in body
    assert invitations.link_for(b) == invitations.join_url(b)


# --- bootstrap_tenant ----------------------------------------------------------------------------------------------------------

def _bootstrap(*extra) -> str:
    out = StringIO()
    call_command("bootstrap_tenant", "--name", "Lakeside Surgery Center", "--slug", "lakeside", "--admin-email", "Kim@Health.example", *extra, stdout=out)
    return out.getvalue()


def test_bootstrap_adds_a_linked_director_and_never_reuses_the_other_facilitys_account(tenant, kim, mailoutbox):
    before = User.objects.filter(pk=kim.pk).values().get()
    text = _bootstrap("--invite")
    lakeside = Tenant.objects.get(slug="lakeside")
    d = User.objects.get(tenant=lakeside)
    assert d.person and d.person == User.objects.get(pk=kim.pk).person and d.username == f"{EMAIL}@lakeside" and d.email == EMAIL
    assert d.is_staff and invitations.is_pending(d) and role(lakeside, "director").pk == d.role_id
    after = User.objects.filter(pk=kim.pk).values().get()
    assert {k for k in before if before[k] != after[k]} == {"person"}  # Riverside's account stays Riverside's
    assert len(mailoutbox) == 1 and mailoutbox[0].subject == "You're added to Lakeside Surgery Center in Cadence CE"
    assert "as Director" in mailoutbox[0].body and f"{APP}/account/facility/{d.pk}/join/" in mailoutbox[0].body
    assert f"{APP}/account/facility/{d.pk}/join/" in text and "/invite/" not in text and "password:" not in text
    assert "User kim@health.example already existed" in _bootstrap("--invite") and len(mailoutbox) == 1


def test_bootstrap_with_a_password_never_changes_the_persons_password(tenant, kim, mailoutbox):
    text = _bootstrap("--password", "Other-Pass-2026-q")
    d = User.objects.get(tenant__slug="lakeside")
    assert people.is_pending(d) and "password:" not in text and "already signs in to Cadence CE" in text and f"/account/facility/{d.pk}/join/" in text
    kim.refresh_from_db()
    assert kim.check_password(PASSWORD) and not kim.check_password("Other-Pass-2026-q") and mailoutbox == []


def test_bootstrap_with_a_password_for_a_person_who_cannot_sign_in_sets_theirs(tenant, mailoutbox):
    a = invite(tenant, email=EMAIL, slug="director")  # invited at Riverside, never accepted
    text = _bootstrap("--password", "Other-Pass-2026-q")
    d = User.objects.get(tenant__slug="lakeside")
    assert "password: Other-Pass-2026-q" in text and d.check_password("Other-Pass-2026-q") and not d.is_invited and people.is_joined(d)
    assert d.person == User.objects.get(pk=a.pk).person and find_account(EMAIL) == d


def test_bootstrap_refuses_an_address_it_cannot_place(tenant, hillside, db):
    account(tenant, username=EMAIL, person=uuid.uuid4())
    account(hillside, username="kim.h", person=uuid.uuid4())
    with pytest.raises(CommandError, match="cannot be used for a new account here"):
        _bootstrap("--invite")


# --- password reset -------------------------------------------------------------------------------------------------------------

@pytest.fixture
def kim_everywhere(tenant, lakeside, hillside):
    """Kim joined at Riverside and Lakeside (Lakeside used last), and invited to Hillside."""
    person = uuid.uuid4()
    a = account(tenant, person=person, last_login=timezone.now() - timezone.timedelta(days=2))
    b = account(lakeside, "technician", username=f"{EMAIL}@lakeside", person=person, last_login=timezone.now() - timezone.timedelta(hours=1))
    c = account(hillside, "manager", username=f"{EMAIL}@hillside", person=person, pending=True)
    return a, b, c


def test_a_reset_sends_a_person_one_link_for_the_account_a_sign_in_opens(client, settings, kim_everywhere, mailoutbox):
    a, b, c = kim_everywhere
    assert client.post("/password-reset/", {"email": " KIM@Health.example "}).status_code == 302
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.to == [EMAIL] and "/invite/" not in mail.body
    assert f"Sign in with {EMAIL}." in mail.body and "@lakeside" not in mail.body
    assert "Your password opens all of your facilities: Lakeside Surgery Center and Riverside Regional." in mail.body
    uid = link_path(mail).split("/")[2]
    assert invitations.urlsafe_base64_decode(uid).decode() == str(b.pk)  # the account used last
    c.refresh_from_db()
    assert c.invited_at is None and AccessEvent.unscoped.count() == 0  # the invitation gets nothing; nobody's log gets anything
    set_url = client.get(link_path(mail))["Location"]
    assert client.post(set_url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}).status_code == 302
    for user in (a, b):
        user.refresh_from_db()
        assert user.check_password(NEW_PASSWORD)
    c.refresh_from_db()
    assert people.is_pending(c)
    assert client.post("/login/", {"username": EMAIL, "password": NEW_PASSWORD}).status_code == 302
    assert int(client.session["_auth_user_id"]) == b.pk


def test_a_reset_for_a_person_who_cannot_sign_in_answers_each_account_and_logs_nothing(client, tenant, lakeside, mailoutbox):
    a = invite(tenant, email=EMAIL, slug="director")
    b = invite(lakeside)
    invitations.send_invitation(a)
    invitations.send_invitation(b)
    resent_before = AccessEvent.unscoped.filter(action=AccessEvent.Action.INVITATION_RESENT).count()
    client.post("/password-reset/", {"email": EMAIL})
    assert len(mailoutbox) == 4 and all(m.subject.startswith("You're invited") for m in mailoutbox[2:])
    assert AccessEvent.unscoped.filter(action=AccessEvent.Action.INVITATION_RESENT).count() == resent_before == 0


def test_a_deactivated_landing_account_moves_the_reset_to_the_next(client, kim_everywhere, mailoutbox):
    a, b, _c = kim_everywhere
    User.objects.filter(pk=b.pk).update(is_active=False)
    client.post("/password-reset/", {"email": EMAIL})
    uid = link_path(mailoutbox[0]).split("/")[2]
    assert len(mailoutbox) == 1 and invitations.urlsafe_base64_decode(uid).decode() == str(a.pk)
    assert "Your password opens all of your facilities" not in mailoutbox[0].body  # Riverside only now


# --- one lockout per person ------------------------------------------------------------------------------------------------------

def _sign_in(client, login, password=PASSWORD):
    return client.post("/login/", {"username": login, "password": password})


def test_each_facilitys_username_adds_no_guessing_budget(client, settings, kim_everywhere):
    settings.SIGNIN_MAX_FAILURES = 3
    a, b, _c = kim_everywhere
    for _ in range(2):
        _sign_in(client, EMAIL, "wrong-password")
    r = _sign_in(client, f"{EMAIL}@lakeside", "wrong-password")
    assert " ".join(r.context["form"].non_field_errors()) == WRONG_LOGIN
    assert signin.person_locked(a) and signin.person_locked(b) and signin.is_locked(f"{EMAIL}@lakeside", None) is None
    r = _sign_in(client, f"{EMAIL}@hillside")  # the right password, by a login text with failures to spare: refused all the same
    assert r.status_code == 200 and "_auth_user_id" not in client.session
    assert not signin.person_locked(account(Tenant.objects.get(slug="riverside"), "technician", username="sam", email="sam@health.example"))
    signin.clear_failures(EMAIL, a)  # what a reset does
    assert _sign_in(client, f"{EMAIL}@lakeside").status_code == 302


def test_a_sign_in_clears_the_persons_count(client, settings, kim_everywhere):
    settings.SIGNIN_MAX_FAILURES = 3
    for _ in range(2):
        _sign_in(client, EMAIL, "wrong-password")
    assert _sign_in(client, f"{EMAIL}@lakeside").status_code == 302
    client.logout()
    for _ in range(2):
        _sign_in(client, f"{EMAIL}@hillside", "wrong-password")
    assert _sign_in(client, "KIM@health.example").status_code == 302  # 2 + 2 would have locked the person


def test_change_password_counts_and_checks_the_persons_lockout(client, settings, kim_everywhere):
    settings.SIGNIN_MAX_FAILURES = 2
    a, b, _c = kim_everywhere
    client.force_login(a)
    for _ in range(2):
        client.post("/account/password/", {"old_password": "not-it", "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert signin.person_locked(b)
    assert _sign_in(Client(), f"{EMAIL}@lakeside").status_code == 200  # another facility's username: locked too
    signin.clear_failures(f"{EMAIL}@lakeside")  # only that login text: the person stays locked
    client.force_login(b)
    r = client.post("/account/password/", {"old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert r.status_code == 200 and "Too many failed sign-ins" in r.content.decode()
    b.refresh_from_db()
    assert b.check_password(PASSWORD)


# --- wording ---------------------------------------------------------------------------------------------------------------------

def test_the_change_password_page_names_the_facilities_the_password_opens(client, kim_everywhere, make_user):
    a, b, _c = kim_everywhere
    client.force_login(b)
    body = client.get("/account/password/").content.decode()
    assert f"You sign in with {EMAIL}." in body and f"{EMAIL}@lakeside" not in body  # never the Lakeside account's own username
    assert "One password opens all of your facilities: Lakeside Surgery Center and Riverside Regional." in body
    r = client.post("/account/password/", {"old_password": PASSWORD, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert r.status_code == 302 and User.objects.get(pk=a.pk).check_password(NEW_PASSWORD)
    client.force_login(make_user("technician"))
    body = client.get("/account/password/").content.decode()
    assert "One password opens" not in body and "You sign in with technician@riverside.example." in body


# --- Admin -----------------------------------------------------------------------------------------------------------------------

def test_admin_shows_the_person_and_keeps_a_linked_accounts_email(client, kim_everywhere, make_user):
    a, b, _c = kim_everywhere
    client.force_login(User.objects.create_superuser(username="root", email="root@example.com", password=PASSWORD))
    body = client.get(f"/admin/accounts/user/{b.pk}/change/").content.decode()
    assert str(b.person) in body and 'name="email"' not in body and 'name="person"' not in body and 'name="tenant"' not in body
    single = make_user("technician")
    body = client.get(f"/admin/accounts/user/{single.pk}/change/").content.decode()
    assert 'name="email"' in body and 'name="person"' not in body and 'name="tenant"' in body


# --- the facilities API ------------------------------------------------------------------------------------------------------------

def test_the_facilities_api_lists_the_persons_facilities(client, kim_everywhere, make_user):
    a, _b, _c = kim_everywhere
    client.force_login(a)
    assert client.get("/api/v1/facilities/").json() == [
        {"name": "Hillside Clinic", "slug": "hillside", "current": False, "invited": True},
        {"name": "Lakeside Surgery Center", "slug": "lakeside", "current": False, "invited": False},
        {"name": "Riverside Regional", "slug": "riverside", "current": True, "invited": False}]
    client.force_login(make_user("vendor"))  # one facility, and a scoped role there: just this one
    assert client.get("/api/v1/facilities/").json() == [{"name": "Riverside Regional", "slug": "riverside", "current": True, "invited": False}]


def test_the_api_users_rows_carry_no_username(client, tenant, kim, lakeside):
    invite(lakeside)
    client.force_login(account(lakeside, username="lee@lakeside.example", email="lee@lakeside.example"))
    rows = client.get("/api/v1/users/").json()["results"]
    assert rows and all("username" not in r for r in rows) and "@lakeside" not in str([r for r in rows if r["email"] == EMAIL])


# --- under row-level security (PostgreSQL, as the runtime role) ---------------------------------------------------------------------

@needs_postgres
def test_bootstrap_links_under_the_policies(tenant, kim, mailoutbox):
    as_app_role()
    _bootstrap("--invite")
    d = User.objects.get(tenant__slug="lakeside")
    assert d.person == User.objects.get(pk=kim.pk).person and "as Director" in mailoutbox[0].body and "/join/" in mailoutbox[0].body


@needs_postgres
def test_inviting_through_the_screen_links_under_the_policies(client, tenant, kim, lakeside, mailoutbox):
    director = account(lakeside, username="lee@lakeside.example", email="lee@lakeside.example")
    technician = str(role(lakeside, "technician").id)  # read before the policies apply: the test runs in no facility
    as_app_role()
    client.force_login(director)
    r = client.post("/users/invite/", {"first_name": "Kim", "last_name": "Alvarez", "email": EMAIL, "role": technician,
                                       "department": "Clinical Engineering"}, **HX)
    assert r.status_code == 200 and f"Invitation sent to {EMAIL}" in r["HX-Trigger"]
    assert User.objects.get(tenant=lakeside, email=EMAIL).person == User.objects.get(pk=kim.pk).person
    assert "as Technician" in mailoutbox[0].body and "/join/" in mailoutbox[0].body


@needs_postgres
def test_the_reset_grouping_under_the_policies(client, kim_everywhere, mailoutbox):
    _a, b, _c = kim_everywhere
    as_app_role()
    assert client.post("/password-reset/", {"email": EMAIL}).status_code == 302
    assert len(mailoutbox) == 1 and invitations.urlsafe_base64_decode(link_path(mailoutbox[0]).split("/")[2]).decode() == str(b.pk)
    assert "Lakeside Surgery Center and Riverside Regional" in mailoutbox[0].body


@needs_postgres
def test_accepting_a_linked_invitation_under_the_policies(client, tenant, lakeside, mailoutbox):
    a = invite(tenant, email=EMAIL, slug="director")
    b = invite(lakeside)
    assert invitations.send_invitation(b)
    path = link_path(mailoutbox[-1])
    as_app_role()
    form_url = client.get(path)["Location"]
    assert b"Set your password" in client.get(form_url).content
    done = client.post(form_url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert done.status_code == 302 and client.get(done["Location"]).status_code == 200
    a.refresh_from_db()
    assert people.is_pending(a) and a.password.startswith(UNUSABLE_PASSWORD_PREFIX)
    assert "Riverside Regional (invited)" in client.get("/").content.decode()
