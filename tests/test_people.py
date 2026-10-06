"""
One person in several facilities (slice 22 scaffold, apps.accounts.people): accounts that share `person` are one person with one
password, sign-in lands on the account used last, and the facility menu switches between them (joining a pending invitation with
the password the person signed in with). A browser works in one facility at a time: a tab left in another reloads.
"""
import uuid

import pytest
from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX
from django.db import IntegrityError, transaction
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts import invitations, people
from apps.accounts.backends import find_account
from apps.accounts.models import Role, User, create_default_roles
from apps.tenants import context
from apps.tenants.context import get_current_tenant, tenant_context
from apps.tenants.models import Tenant

PASSWORD = "Test-Pass-2026-x"
EMAIL = "kim@health.example"


@pytest.fixture
def lakeside(db):
    t = Tenant.objects.create(name="Lakeside Surgery Center", slug="lakeside")
    create_default_roles(t)
    return t


def account(tenant, slug, *, username, person=None, pending=False, signed_in=True, email=EMAIL):
    role = Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: made outside any facility
    user = User(username=username, email=email, first_name="Kim", last_name="Alvarez", tenant=tenant, role=role, person=person,
                is_invited=pending)
    if pending:
        user.set_unusable_password()
    else:
        user.set_password(PASSWORD)
    user.last_login = None if pending or not signed_in else timezone.now()
    user._password = None  # created, not changed: nothing to share yet
    user.save()
    return user


@pytest.fixture
def kim(tenant, lakeside):
    """Kim: director at Riverside (signed in there before), technician at Lakeside (joined)."""
    person = uuid.uuid4()
    a = account(tenant, "director", username=EMAIL, person=person)
    b = account(lakeside, "technician", username=f"{EMAIL}@lakeside", person=person)
    User.objects.filter(pk=b.pk).update(last_login=a.last_login - timezone.timedelta(days=1))
    return a, User.objects.get(pk=b.pk)


@pytest.fixture
def invited(tenant, lakeside):
    """Ines at Riverside, and a Lakeside invitation she has not joined."""
    person, email = uuid.uuid4(), "ines@health.example"
    a = account(tenant, "director", username=email, person=person, email=email)
    b = account(lakeside, "technician", username=f"{email}@lakeside", person=person, pending=True, email=email)
    return a, b


# --- the link ---------------------------------------------------------------------------------------------------------------------

def test_one_account_per_person_and_facility_and_never_a_superuser(kim, tenant):
    a, _b = kim
    with pytest.raises(IntegrityError), transaction.atomic():
        account(tenant, "technician", username="kim2@health.example", person=a.person)
    root = User.objects.create_superuser(username="root", password=PASSWORD, email="root@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=root.pk).update(person=a.person)


def test_a_password_set_on_one_account_is_the_persons_everywhere(kim):
    a, b = kim
    old_b = b.password
    a.set_password("New-Pass-2026-y")
    a.save()
    b.refresh_from_db()
    assert b.password == a.password != old_b and b.check_password("New-Pass-2026-y")
    # a deactivated account keeps the person's password too, so reactivating it never brings an old one back
    User.objects.filter(pk=b.pk).update(is_active=False)
    a.set_password("Third-Pass-2026-z")
    a.save(update_fields=["password"])  # the hasher upgrade's save: the password column alone
    b.refresh_from_db()
    assert b.password == a.password
    # an unusable password (a withdrawn invitation) is never shared
    a.set_unusable_password()
    a.save(update_fields=["password"])
    b.refresh_from_db()
    assert b.has_usable_password()


def test_a_password_voids_the_persons_emailed_invitation_links(invited, mailoutbox):
    """Kim was invited to both before she had a password: once she sets one, Lakeside's emailed set-password link stops working
    (she joins Lakeside from the facility menu instead)."""
    a, b = invited
    User.objects.filter(pk=a.pk).update(is_invited=True, last_login=None, password=b.password)
    b.refresh_from_db()
    invitations.send_invitation(b)
    b.refresh_from_db()
    token = invitations.invitation_tokens.make_token(b)
    a.refresh_from_db()
    a.set_password(PASSWORD)  # accepting Riverside's invitation
    a.save()
    b.refresh_from_db()
    assert b.password.startswith(UNUSABLE_PASSWORD_PREFIX) and not invitations.invitation_tokens.check_token(b, token)


# --- signing in -------------------------------------------------------------------------------------------------------------------

def test_a_sign_in_as_the_person_lands_on_the_account_used_last(kim, client):
    a, b = kim
    assert find_account(EMAIL) == a and find_account("KIM@health.example") == a and find_account(f"{EMAIL}@lakeside") == a
    User.objects.filter(pk=b.pk).update(last_login=timezone.now())
    assert find_account(EMAIL) == b
    r = client.post("/login/", {"username": EMAIL, "password": PASSWORD})
    assert r.status_code == 302 and int(client.session["_auth_user_id"]) == b.pk


def test_deactivated_in_one_facility_the_person_still_signs_in_to_the_other(kim, client):
    a, b = kim
    User.objects.filter(pk=a.pk).update(is_active=False)
    assert find_account(EMAIL) == b
    Tenant.objects.filter(pk=b.tenant_id).update(is_active=False)
    assert find_account(EMAIL) is None
    assert client.post("/login/", {"username": EMAIL, "password": PASSWORD}).status_code == 200  # the form again: not signed in


def test_a_pending_invitation_is_never_a_landing_account(invited):
    a, b = invited
    User.objects.filter(pk=a.pk).update(is_active=False)
    assert find_account(EMAIL) is None and people.landing_account(b) is None


def test_two_people_under_one_address_sign_in_to_neither(tenant, lakeside):
    account(tenant, "director", username="a1", person=uuid.uuid4())
    account(lakeside, "director", username="a2", person=uuid.uuid4())
    assert find_account(EMAIL) is None
    assert find_account("a1").username == "a1"  # the exact username still names its own person


# --- the facility menu and the switch -----------------------------------------------------------------------------------------

def test_the_menu_lists_the_persons_facilities_it_can_open(kim, invited, client, tenant, make_user):
    a, b = kim
    assert people.facility_menu(a) == [
        {"account": b.pk, "name": "Lakeside Surgery Center", "slug": "lakeside", "current": False, "invited": False},
        {"account": a.pk, "name": "Riverside Regional", "slug": "riverside", "current": True, "invited": False}]
    client.force_login(a)
    body = client.get("/").content.decode()
    assert 'data-act="switch-facility"' in body and "Switch to Lakeside Surgery Center" in body and ">All facilities<" in body
    User.objects.filter(pk=b.pk).update(is_active=False)
    assert people.facility_menu(a) == []  # nowhere else to go: the plain facility name
    assert 'data-act="switch-facility"' not in client.get("/").content.decode()
    ia, ib = invited
    menu = people.facility_menu(ia)
    assert [m["invited"] for m in menu if not m["current"]] == [True] and people.joined_count(ia) == 1
    assert people.facility_menu(make_user("technician")) == []  # an account in one facility


def test_switching_signs_in_to_the_persons_other_account(kim, client):
    a, b = kim
    client.force_login(a)
    assert client.get("/account/facility/").status_code == 405
    r = client.post("/account/facility/", {"account": b.pk, "screen": "workorders"})
    assert r.status_code == 302 and r["Location"] == "/work-orders/"
    assert int(client.session["_auth_user_id"]) == b.pk
    b.refresh_from_db()
    assert b.last_login is not None and people.landing_account(a) == b  # the next sign-in lands here
    r = client.post("/account/facility/", {"account": a.pk, "screen": "users"})  # a technician has no Users; a director has
    assert r["Location"] == "/users/"
    client.post("/account/facility/", {"account": b.pk, "screen": "users"})
    assert client.post("/account/facility/", {"account": a.pk, "screen": "nope"})["Location"] == "/"


def test_the_switch_follows_only_a_path_on_this_site(kim, client):
    a, b = kim
    client.force_login(a)
    assert client.post("/account/facility/", {"account": b.pk, "next": "/work-orders/WO-26-0001/?facility=lakeside"})["Location"] == \
        "/work-orders/WO-26-0001/?facility=lakeside"
    for bad in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
        client.force_login(a)
        assert client.post("/account/facility/", {"account": b.pk, "next": bad})["Location"] == "/"


def test_switching_into_a_pending_invitation_joins_it(invited, client):
    a, b = invited
    client.force_login(a)
    r = client.post("/account/facility/", {"account": b.pk}, HTTP_HX_REQUEST="true")
    assert r.status_code == 204 and r["HX-Redirect"] == "/"
    b.refresh_from_db()
    assert b.password == a.password and b.last_login is not None and people.is_joined(b)
    assert int(client.session["_auth_user_id"]) == b.pk


def test_the_switch_refuses_anything_but_the_persons_own_account(kim, client, make_user, lakeside):
    a, b = kim
    stranger = make_user("director", tenant_=lakeside, username="someone@lakeside.example")
    client.force_login(a)
    assert client.post("/account/facility/", {"account": stranger.pk}).status_code == 404
    assert client.post("/account/facility/", {"account": a.pk}).status_code == 404
    assert client.post("/account/facility/", {"account": "x"}).status_code == 404
    User.objects.filter(pk=b.pk).update(is_active=False)
    assert client.post("/account/facility/", {"account": b.pk}).status_code == 404
    User.objects.filter(pk=b.pk).update(is_active=True)
    Tenant.objects.filter(pk=b.tenant_id).update(is_active=False)
    assert client.post("/account/facility/", {"account": b.pk}).status_code == 404
    assert int(client.session["_auth_user_id"]) == a.pk
    client.logout()
    assert client.post("/account/facility/", {"account": b.pk}).status_code == 302  # to sign in
    Tenant.objects.filter(pk=b.tenant_id).update(is_active=True)
    client.force_login(stranger)  # someone else at Lakeside
    assert client.post("/account/facility/", {"account": b.pk}).status_code == 404


def test_a_withdrawn_invitation_is_not_joined(invited, client):
    a, b = invited
    client.force_login(a)
    User.objects.filter(pk=b.pk).update(is_active=False)
    assert client.post("/account/facility/", {"account": b.pk}).status_code == 404
    b.refresh_from_db()
    assert not b.has_usable_password() and not b.is_active


def test_the_join_page_is_the_persons_own(invited, client, make_user, lakeside):
    a, b = invited
    client.force_login(a)
    body = client.get(f"/account/facility/{b.pk}/join/").content.decode()
    assert "Join Lakeside Surgery Center" in body and "invited you as Technician" in body
    assert client.get(f"/account/facility/{a.pk}/join/")["Location"] == "/"
    other = make_user("technician", tenant_=lakeside, username="other@lakeside.example")
    assert client.get(f"/account/facility/{other.pk}/join/").status_code == 404
    client.logout()
    r = client.get(f"/account/facility/{b.pk}/join/")
    assert r.status_code == 302 and r["Location"].startswith("/login/?next=")


def test_a_tab_left_in_another_facility_reloads(kim, client, tenant):
    a, b = kim
    client.force_login(a)
    here = {"HTTP_HX_REQUEST": "true", "HTTP_X_CADENCE_FACILITY": str(tenant.pk)}
    assert client.get("/work-orders/", **here).status_code == 200
    client.post("/account/facility/", {"account": b.pk})
    r = client.get("/work-orders/", **here)  # the old tab, now signed in to Lakeside
    assert r.status_code == 204 and r["HX-Refresh"] == "true"
    r = client.post("/work-orders/new/", {}, **here)  # its old CSRF token would be refused: a reload instead
    assert r.status_code == 204 and r["HX-Refresh"] == "true"
    assert client.get("/work-orders/", HTTP_HX_REQUEST="true", HTTP_X_CADENCE_FACILITY=str(b.tenant_id)).status_code == 200
    assert f'"X-Cadence-Facility": "{b.tenant_id}"' in client.get("/work-orders/").content.decode()


def test_the_persons_facilities_api_is_for_sessions_only(kim, client):
    a, _b = kim
    token = Token.objects.create(user=a)
    r = client.get("/api/v1/facilities/", HTTP_AUTHORIZATION=f"Token {token.key}")
    assert r.status_code == 403 and "sign in with a session" in r.json()["detail"]
    r = client.get("/api/v1/overview/all-facilities/", HTTP_AUTHORIZATION=f"Token {token.key}")
    assert r.status_code == 403
    client.force_login(a)
    assert client.get("/api/v1/facilities/").status_code == 200


def test_tenant_context_restores_itself_when_the_database_setting_fails(tenant, lakeside, monkeypatch):
    def broken(t):
        if t == lakeside:
            raise RuntimeError("connection lost")

    monkeypatch.setattr(context, "set_db_tenant", broken)
    with tenant_context(tenant):
        zone = timezone.get_current_timezone_name()
        with pytest.raises(RuntimeError), tenant_context(lakeside):
            pass
        assert get_current_tenant() == tenant and timezone.get_current_timezone_name() == zone


@needs_postgres
def test_the_menu_switch_and_join_under_the_policies(invited, client):
    """The menu reads only User and Tenant; the switch reads the target's screens inside its own facility; the join page reads
    the role there. Under row-level security a read of Lakeside's roles from Riverside finds nothing (or fails)."""
    ia, ib = invited
    as_app_role()
    client.force_login(ia)
    assert "Lakeside Surgery Center (invited)" in client.get("/").content.decode()
    assert "as Technician" in client.get(f"/account/facility/{ib.pk}/join/").content.decode()
    r = client.post("/account/facility/", {"account": ib.pk, "screen": "workorders"})
    assert r.status_code == 302 and r["Location"] == "/work-orders/"
    assert client.get("/work-orders/").status_code == 200


# --- merge fixes ------------------------------------------------------------------------------------------------------------------

def test_an_invitations_join_link_survives_signing_in(invited, client):
    """The join page belongs to the person, not one facility: after signing in (landing on Riverside) the link still opens."""
    _a, b = invited
    r = client.get(f"/account/facility/{b.pk}/join/")
    r = client.post(r["Location"], {"username": "ines@health.example", "password": PASSWORD})
    assert r.status_code == 302 and r["Location"] == f"/account/facility/{b.pk}/join/"
    assert "Join Lakeside Surgery Center" in client.get(r["Location"]).content.decode()


def test_a_nameless_linked_account_is_shown_by_its_email(tenant, lakeside, client, make_user):
    """bootstrap_tenant's director has no name; a second facility's username ("<email>@<slug>") would say the person works
    elsewhere, so names fall back to the email."""
    a = account(tenant, "director", username=EMAIL, person=uuid.uuid4())
    b = account(lakeside, "technician", username=f"{EMAIL}@lakeside", person=a.person)
    User.objects.filter(pk=b.pk).update(first_name="", last_name="")
    b.refresh_from_db()
    assert str(b) == EMAIL
    client.force_login(make_user("director", tenant_=lakeside, username="dir@lakeside.example"))
    body = client.get("/users/").content.decode()
    assert EMAIL in body and f"{EMAIL}@lakeside" not in body


def test_all_facilities_with_nowhere_else_joined_keeps_this_facility_selected(invited, client):
    a, _b = invited
    client.force_login(a)
    body = client.get("/overview/all/").content.decode()
    assert f'<option value="{a.pk}" selected>' in body and ">All facilities</option>" not in body


def test_an_invited_account_given_a_password_in_admin_still_signs_in_once_linked(tenant, lakeside):
    """Merge fix: an invitation never accepted whose password an administrator set has the person's password; linking it must not
    leave the person with nowhere to land."""
    person = uuid.uuid4()
    a = account(tenant, "director", username=EMAIL, person=person, signed_in=False)
    User.objects.filter(pk=a.pk).update(is_invited=True)
    account(lakeside, "technician", username=f"{EMAIL}@lakeside", person=person, pending=True)
    assert find_account(EMAIL) == a
