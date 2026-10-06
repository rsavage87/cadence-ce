"""
Users and access over the API (slice 19, part D; apps/api/views_users.py): users, roles, technicians, and credentials, through the
Users and access screen's doors (Users View to read; Users Full for users and roles, Users Edit for credentials), its services,
and its rules. Every endpoint by session and by token, every refusal (levels, scoped users, a superuser with no facility, a
deactivated user's token, another facility's ids, bad bodies), privilege escalation, and the same endpoints under PostgreSQL's
row-level security as the runtime role.
"""
import smtplib
from datetime import date, timedelta

import pytest
from django.test import Client
from django.utils.text import slugify
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts import invitations, services
from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.accounts.services import SCOPE_CHOICE_MESSAGE, UserFilters
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Department
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

API = "/api/v1/"
USERS, ROLES, TECHS, CREDS = f"{API}users/", f"{API}roles/", f"{API}technicians/", f"{API}credentials/"
TODAY = date.today()
PASSWORD = "Test-Pass-2026-x"
ALL_FULL = {m: Level.FULL for m in Module.values}
# No username (slice 22): a second facility's account has one of its own, which would say the address works elsewhere too.
USER_FIELDS = {"id", "name", "first_name", "last_name", "email", "role", "role_name", "role_slug", "scope", "company", "department",
               "scope_gap", "status", "invitation_pending", "last_sign_in", "is_superuser", "technician"}
ROLE_FIELDS = {"id", "name", "slug", "description", "is_system", "standard", "levels", "scope", "scope_fixed", "users", "users_seeing_nothing"}
CRED_FIELDS = {"id", "technician", "technician_name", "scope", "value", "source", "issued_on", "expires_on", "status", "state"}
STANDARD_ORDER = ["director", "manager", "technician", "requester", "analyst", "vendor"]


def call(client, method, url, body=None, token=None):
    headers = {"HTTP_AUTHORIZATION": f"Token {token.key}"} if token else {}
    if method == "get":
        return client.get(url, **headers)
    if method == "delete":
        return client.delete(url, **headers)
    return getattr(client, method)(url, {} if body is None else body, content_type="application/json", **headers)


def post(client, url, body=None, token=None):
    return call(client, "post", url, body, token)


def patch(client, url, body, token=None):
    return call(client, "patch", url, body, token)


def by_token(method, url, body, token):
    """A request with the token alone (a client with no session)."""
    return call(Client(), method, url, body, token)


def results(r):
    assert r.status_code == 200, r.content
    return r.json()["results"]


def user_url(user, action=""):
    return f"{USERS}{user.pk}/{action}"


def role_url(role):
    return f"{ROLES}{role.pk}/"


def cred_url(cred, action=""):
    return f"{CREDS}{cred.pk}/{action}"


def _broken_mail(monkeypatch):
    def boom(self, fail_silently=False):
        raise smtplib.SMTPException("mail server unreachable")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", boom)


@pytest.fixture
def role(tenant):
    def _get(slug, tenant_=None):
        return Role.unscoped.get(tenant=tenant_ or tenant, slug=slug)  # unscoped: fixtures run outside a request context

    return _get


@pytest.fixture
def other_roles(other_tenant):
    create_default_roles(other_tenant)
    return other_tenant


@pytest.fixture
def member(tenant):
    """A user of this facility with a default role (by slug) or a custom one, and any of their fields."""
    n = iter(range(1000))

    def _make(who, **fields):
        r = who if isinstance(who, Role) else Role.unscoped.get(tenant=tenant, slug=who)  # unscoped: fixtures may run outside a request
        i = next(n)
        defaults = {"username": f"{r.slug}-{i}@riverside.example", "first_name": r.slug.replace("-", " ").title(), "last_name": f"User{i}",
                    "email": f"{r.slug}-{i}@riverside.example"}
        return User.objects.create_user(password=PASSWORD, tenant=tenant, role=r, **{**defaults, **fields})

    return _make


@pytest.fixture
def person(client, member):
    """Sign the test client in as a new member."""

    def _as(who, **fields):
        user = member(who, **fields)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def token_of():
    def _of(user):
        return Token.objects.get_or_create(user=user)[0]

    return _of


@pytest.fixture
def custom_role(ctx):
    def _make(name, levels, scope=DataScope.FACILITY):
        r = Role.objects.create(name=name, slug=slugify(name), scope=scope)
        r.set_levels({**{m: Level.NONE for m in Module.values}, **levels})
        return r

    return _make


@pytest.fixture
def clerk_role(custom_role):
    """Users Edit: the credentials tab's level, short of the Users and Roles tabs' Full."""
    return custom_role("Credential clerk", {"users": Level.EDIT, "equipment": Level.VIEW})


@pytest.fixture
def admin_role(custom_role):
    """Users Full on a custom role: manages users and roles, but is not the Director."""
    return custom_role("Admin clerk", {"users": Level.FULL, "contracts": Level.EDIT})


@pytest.fixture
def units(ctx, dept):
    return {"ICU": dept, "ED": Department.objects.create(name="ED")}


@pytest.fixture
def training(techs):
    return Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Ventilators", status="in_training",
                                     source="OEM training", issued_on=TODAY - timedelta(days=10))


@pytest.fixture
def theirs(other_roles, make_user):
    """Another facility's director, an invited user, a role, a technician, and a credential."""
    director = make_user("director", tenant_=other_roles, username="director@other.example")
    with tenant_context(other_roles):
        manager = Role.objects.get(slug="manager")
        tech = Technician.objects.create(name="Someone Else", title="BMET")
        cred = Credential.objects.create(technician=tech, scope=Scope.CATEGORY, value="Lasers", expires_on=TODAY + timedelta(days=100))
    invitee = services.invite_user(other_roles, email="new@other.example", first_name="Nia", last_name="Other", role=manager)
    return {"director": director, "user": invitee, "role": manager, "tech": tech, "cred": cred}


# --- users: reading ------------------------------------------------------------------------------------------------------------

def test_the_users_list_is_the_tabs_rows_and_nothing_secret(client, ctx, person, member, role, units, theirs, token_of, mailoutbox):
    me = person("director")
    vendor = member("vendor", company="Philips", department="External vendor")
    nurse = member("requester", department="ICU")
    lost = member("requester", department="Cardiology")  # no department here is named so: sees nothing
    tech_user = member("technician")
    tech = Technician.objects.create(name="Tech User", user=tech_user)
    invited = services.invite_user(ctx, email="maria@riverside.example", first_name="Maria", last_name="Santos", role=role("technician"))
    invitations.send_invitation(invited)
    gone = member("analyst")
    services.deactivate_user(gone)
    token_of(me)
    r = client.get(USERS)
    rows = {u["id"]: u for u in results(r)}
    # this facility's users only, in the tab's order
    assert list(rows) == [u.pk for u in services.list_users(ctx, UserFilters())] and theirs["user"].pk not in rows
    assert all(set(u) == USER_FIELDS for u in rows.values())
    v, n, lo = rows[vendor.pk], rows[nurse.pk], rows[lost.pk]
    assert (v["scope"], v["company"], v["department"], v["scope_gap"], v["role_slug"]) == ("company", "Philips", "External vendor", None, "vendor")
    assert (n["scope"], n["company"], n["department"], n["scope_gap"]) == ("department", None, "ICU", None)
    assert (lo["scope_gap"], rows[me.pk]["scope"], rows[me.pk]["company"]) == ("department", "facility", None)
    assert rows[tech_user.pk]["technician"] == str(tech.id) and rows[me.pk]["technician"] is None
    assert (rows[invited.pk]["status"], rows[invited.pk]["invitation_pending"], rows[invited.pk]["last_sign_in"]) == ("invited", True, None)
    assert (rows[gone.pk]["status"], rows[me.pk]["status"], rows[me.pk]["role"]) == ("deactivated", "active", str(me.role_id))
    # never a password hash, a token, or an invitation link
    body = r.content.decode()
    link = invitations.invitation_url(User.objects.get(pk=invited.pk))
    assert me.password not in body and "pbkdf2" not in body and Token.objects.get().key not in body and link not in body and "/invite/" not in body
    # retrieve: the same row; another facility's user is a 404
    one = client.get(user_url(vendor))
    assert one.status_code == 200 and one.json() == v
    assert client.get(user_url(theirs["user"])).status_code == 404 and client.get(f"{USERS}nope/").status_code == 404


def test_the_users_list_filters_as_the_tab_does(client, ctx, person, member, role):
    person("director")
    vendor = member("vendor", company="Philips", first_name="Fran")
    invited = services.invite_user(ctx, email="maria@riverside.example", first_name="Maria", last_name="Santos", role=role("technician"))
    assert [u["id"] for u in results(client.get(f"{USERS}?status=invited"))] == [invited.pk]
    assert [u["id"] for u in results(client.get(f"{USERS}?role=vendor"))] == [vendor.pk]
    assert [u["id"] for u in results(client.get(f"{USERS}?q=philips"))] == [vendor.pk]
    r = client.get(f"{USERS}?status=gone&role=wizard")
    assert r.status_code == 400 and set(r.json()) == {"status", "role"}


def test_reading_users_needs_users_view(client, person, member):
    someone = member("technician")
    person("manager")  # Users View
    assert client.get(USERS).status_code == 200 and client.get(user_url(someone)).status_code == 200
    for slug in ("technician", "analyst"):  # Users None
        person(slug)
        assert client.get(USERS).status_code == 403 and client.get(user_url(someone)).status_code == 403


def test_the_browsable_api_and_options_render(client, person, member, techs, vent_model):
    """The HTML pages build their forms from each write's serializer (Invite user, Add role, Add credential), inside the facility."""
    person("director")
    tom = member("technician")
    for url in (USERS, user_url(tom), ROLES, TECHS, CREDS, cred_url(techs["tom"].credentials.get())):
        assert client.get(url, HTTP_ACCEPT="text/html").status_code == 200, url
        assert client.options(url).status_code == 200, url
    assert b"create_technician" in client.get(USERS, HTTP_ACCEPT="text/html").content
    assert client.options(CREDS).json()["actions"]["POST"]["source"]["choices"][0] == {"value": "OEM training", "display_name": "OEM training"}
    assert set(client.options(USERS).json()["actions"]["POST"]) == {"email", "first_name", "last_name", "role", "company", "department", "create_technician"}
    assert set(client.options(ROLES).json()["actions"]["POST"]) == {"name", "copy_from", "description", "scope"}


# --- users: Invite user ----------------------------------------------------------------------------------------------------------

def test_invite_creates_the_account_and_sends_the_email_as_the_modal_does(client, ctx, person, role, units, mailoutbox):
    person("director")
    r = post(client, USERS, {"email": " Maria.Santos@Riverside.example ", "first_name": "Maria", "last_name": "Santos", "role": str(role("technician").id),
                             "department": "Clinical Engineering", "create_technician": True})
    assert r.status_code == 201, r.content
    data = r.json()
    assert set(data) == USER_FIELDS | {"invitation_sent", "message"}
    assert (data["invitation_sent"], data["message"], data["status"], data["invitation_pending"]) == (
        True, "Invitation sent to maria.santos@riverside.example", "invited", True)
    user = User.objects.get(pk=data["id"])
    assert (user.username, user.email, user.tenant_id, user.role.slug, user.department) == (
        "maria.santos@riverside.example", "maria.santos@riverside.example", ctx.id, "technician", "Clinical Engineering")
    assert user.is_invited and not user.has_usable_password() and invitations.is_pending(user)
    assert data["technician"] == str(user.technician.id) and (user.technician.name, user.technician.title) == ("Maria Santos", "BMET I")
    assert len(mailoutbox) == 1 and mailoutbox[0].to == ["maria.santos@riverside.example"]
    assert invitations.invitation_url(user) in mailoutbox[0].body and "/invite/" not in r.content.decode()
    # a company-scoped role with its company, and a department-scoped role with one of the facility's units (stored as it names it)
    v = post(client, USERS, {"email": "fse@philips.example", "first_name": "Fran", "last_name": "Ek", "role": str(role("vendor").id),
                             "company": "Philips", "department": "External vendor"})
    assert v.status_code == 201 and (v.json()["scope"], v.json()["company"]) == ("company", "Philips")
    nurse = post(client, USERS, {"email": "nurse@riverside.example", "first_name": "Ana", "last_name": "Ruiz", "role": str(role("requester").id),
                                 "department": " icu "})
    assert nurse.status_code == 201 and nurse.json()["department"] == "ICU" and len(mailoutbox) == 3
    # the same account the service makes for the modal
    same = services.invite_user(ctx, email="same@riverside.example", first_name="Ana", last_name="Ruiz", role=role("requester"), department=" icu ")
    made = User.objects.get(pk=nurse.json()["id"])
    assert [(u.role_id, u.department, u.company, u.is_invited, u.is_active) for u in (made, same)] == [(same.role_id, "ICU", "", True, True)] * 2


def test_invite_by_token(tenant, member, token_of, role, mailoutbox):
    token = token_of(member("director"))
    r = by_token("post", USERS, {"email": "t@riverside.example", "first_name": "T", "last_name": "O", "role": str(role("technician").id)}, token)
    assert r.status_code == 201 and r.json()["invitation_sent"] is True
    assert User.objects.get(pk=r.json()["id"]).tenant_id == tenant.id and len(mailoutbox) == 1


def test_a_failed_invitation_email_is_reported_not_raised(client, person, role, mailoutbox, monkeypatch):
    person("director")
    _broken_mail(monkeypatch)
    r = post(client, USERS, {"email": "msantos@riverside.example", "first_name": "Maria", "last_name": "Santos", "role": str(role("technician").id)})
    assert r.status_code == 201 and r.json()["invitation_sent"] is False
    user = User.objects.get(username="msantos@riverside.example")
    assert r.json()["message"] == ("The account for msantos@riverside.example was created, but the invitation email could not be sent. Once email "
                                   f"is working, use POST /api/v1/users/{user.pk}/resend-invite/.")
    assert invitations.is_pending(user) and mailoutbox == []
    again = post(client, user_url(user, "resend-invite/"))
    assert again.status_code == 200 and again.json()["invitation_sent"] is False and "could not be sent" in again.json()["message"]
    monkeypatch.undo()
    again = post(client, user_url(user, "resend-invite/"))
    assert again.json()["invitation_sent"] is True and again.json()["message"] == "Invitation resent to msantos@riverside.example" and len(mailoutbox) == 1


def test_invite_refusals(client, ctx, person, member, role, other_roles, units, mailoutbox):
    person("director")
    member("technician", email="taken@riverside.example", username="taken@riverside.example")
    tech = str(role("technician").id)
    ok = {"email": "new@riverside.example", "first_name": "New", "last_name": "Person", "role": tech}
    before = User.objects.count()
    cases = [
        ({"first_name": None}, {"first_name"}), ({"first_name": "   "}, {"first_name"}), ({"email": "not-an-email"}, {"email"}),
        ({"email": "a" * 140 + "@riverside.example"}, {"email"}),
        ({"role": str(role("manager", other_roles).id)}, {"role"}), ({"role": "garbage"}, {"role"}), ({"role": None}, {"role"}),
        ({"password": "Hunter2-Hunter2"}, {"password"}), ({"is_superuser": True}, {"is_superuser"}), ({"tenant": str(other_roles.id)}, {"tenant"}),
        ({"status": "active"}, {"status"}),
        ({"role": str(role("vendor").id)}, {"company"}),  # a company-scoped role needs its company
        ({"company": "Philips"}, {"company"}),  # and only such a role takes one
        ({"role": str(role("requester").id)}, {"department"}), ({"role": str(role("requester").id), "department": "Finance"}, {"department"}),
        ({"department": "Narnia"}, {"department"}),  # a facility-wide role's department comes from the modal's list
        ({"email": "TAKEN@riverside.example"}, {"detail"}),
    ]
    for change, keys in cases:
        body = {k: v for k, v in {**ok, **change}.items() if v is not None or k == "role"}
        r = post(client, USERS, body)
        assert r.status_code == 400 and set(r.json()) == keys, (change, r.content)
    errors = post(client, USERS, {**ok, "role": str(role("manager", other_roles).id)}).json()
    assert errors == {"role": ["Choose a role."]}
    assert post(client, USERS, {**ok, "role": str(role("vendor").id)}).json()["company"] == [
        "Enter the company this user works for: the Vendor technician role sees only the work orders assigned to it."]
    assert post(client, USERS, {**ok, "company": "Philips"}).json()["company"] == [
        "Only a role that sees its company's work orders takes a company; the Technician role sees the whole facility."]
    assert "Finance is not one of this facility's departments" in post(client, USERS, {**ok, "role": str(role("requester").id),
                                                                                         "department": "Finance"}).json()["department"][0]
    assert post(client, USERS, {**ok, "email": "TAKEN@riverside.example"}).json() == {"detail": "taken@riverside.example is already a member of this facility."}
    assert post(client, USERS, {**ok, "password": "x"}).json() == {"password": ["Unknown field."]}
    assert post(client, USERS, [ok]).json() == {"detail": "Send a JSON object."}
    assert User.objects.count() == before and mailoutbox == []


def test_inviting_needs_users_full(client, person, role, clerk_role, mailoutbox):
    body = {"email": "new@riverside.example", "first_name": "New", "last_name": "Person", "role": str(role("technician").id)}
    for who in ("manager", clerk_role):  # Users View, Users Edit
        person(who)
        assert post(client, USERS, body).status_code == 403
    assert not User.objects.filter(email="new@riverside.example").exists() and mailoutbox == []


# --- users: Edit (PATCH) -------------------------------------------------------------------------------------------------------

def test_edit_changes_the_role_company_and_department_together(client, ctx, person, member, role, units, token_of, caplog):
    me = person("director")
    tom = member("technician", first_name="Tom", last_name="Okafor", department="Clinical Engineering")
    with caplog.at_level("INFO", logger="cadence.audit"):
        r = patch(client, user_url(tom), {"role": str(role("vendor").id), "company": "Philips", "department": "External vendor"})
    assert r.status_code == 200, r.content
    assert (r.json()["role_slug"], r.json()["scope"], r.json()["company"], r.json()["department"]) == ("vendor", "company", "Philips", "External vendor")
    tom.refresh_from_db()
    assert (tom.role.slug, tom.company, tom.department) == ("vendor", "Philips", "External vendor")
    assert f"user {tom.pk} (tenant {ctx.id}): role changed from 'technician' to 'vendor' by user {me.pk}" in caplog.text
    # the same change through the service the Users tab's Edit calls
    twin = member("technician", department="Clinical Engineering")
    services.set_user_access(twin, role=role("vendor"), company="Philips", department="External vendor", by=me)
    assert (twin.role_id, twin.company, twin.department) == (tom.role_id, tom.company, tom.department)
    # one field: the others stay
    assert patch(client, user_url(tom), {"company": "Philips field service"}).json()["company"] == "Philips field service"
    tom.refresh_from_db()
    assert (tom.role.slug, tom.department) == ("vendor", "External vendor")
    # a requester's unit: one of the facility's
    nurse = member("requester", department="icu")
    r = patch(client, user_url(nurse), {"department": "Finance"})
    assert r.status_code == 400 and "Finance is not one of this facility's departments" in r.json()["department"][0]
    assert patch(client, user_url(nurse), {"department": "ED"}).json()["department"] == "ED"
    # what a GET returned may be sent back as it is
    shown = client.get(user_url(tom)).json()
    assert patch(client, user_url(tom), shown).json() == shown
    # by token
    r = by_token("patch", user_url(nurse), {"role": str(role("manager").id), "department": "Clinical Engineering"}, token_of(me))
    assert r.status_code == 200 and (r.json()["role_slug"], r.json()["scope"]) == ("manager", "facility")
    nurse.refresh_from_db()
    assert (nurse.role.slug, nurse.department) == ("manager", "Clinical Engineering")


def test_edit_refusals(client, ctx, person, member, role, other_roles, units, theirs):
    me = person("director")
    tom = member("technician", department="Clinical Engineering")
    cases = [
        ({"role": None}, {"role": ["Choose a role."]}),
        ({"role": str(role("manager", other_roles).id)}, {"role": ["Choose a role."]}),
        ({"role": str(role("vendor").id)}, {"company": [
            "Enter the company this user works for: the Vendor technician role sees only the work orders assigned to it."]}),
        ({"company": "Philips"}, {"company": [
            "Only a role that sees its company's work orders takes a company; the Technician role sees the whole facility."]}),
        ({"department": "Narnia"}, {"department": ["Choose a department from the list: one of this facility's departments, Clinical Engineering, "
                                                   "Finance, Quality and Patient Safety, or External vendor."]}),
        ({"is_superuser": True}, {"is_superuser": ["Superusers are managed in Admin."]}),
        ({"is_active": False}, {"is_active": ["Unknown field."]}), ({"password": "x"}, {"password": ["Unknown field."]}),
        ({"tenant": str(other_roles.id)}, {"tenant": ["Unknown field."]}),
        ({"status": "deactivated"}, {"status": ["Use POST /api/v1/users/{id}/deactivate/ or reactivate/ to change the status."]}),
        ({"email": "boss@riverside.example"}, {"email": ["A user's name and email address are set when they are invited."]}),
        ({"scope": "company", "company": "Philips"}, {"scope": [
            "What a user sees comes from their role: change the role, or the role's scope (PATCH /api/v1/roles/{id}/)."]}),
    ]
    for body, errors in cases:
        r = patch(client, user_url(tom), body)
        assert r.status_code == 400 and r.json() == errors, (body, r.content)
    tom.refresh_from_db()
    assert (tom.role.slug, tom.company, tom.department, tom.is_active, tom.is_superuser, tom.email) == (
        "technician", "", "Clinical Engineering", True, False, "technician-1@riverside.example")
    # your own role is not yours to change; your own department, with a facility-wide role, is
    r = patch(client, user_url(me), {"role": str(role("manager").id)})
    assert r.status_code == 400 and r.json() == {"detail": "You cannot change your own role."}
    assert patch(client, user_url(me), {"role": str(me.role_id), "department": "Finance"}).status_code == 200
    me.refresh_from_db()
    assert (me.role.slug, me.department) == ("director", "Finance")
    # PUT and DELETE are not offered; another facility's user is a 404
    assert call(client, "put", user_url(tom), {"role": str(role("manager").id)}).status_code == 405
    assert client.delete(user_url(tom)).status_code == 405
    assert patch(client, user_url(theirs["user"]), {"department": "Finance"}).status_code == 404
    theirs["user"].refresh_from_db()
    assert theirs["user"].department == ""


# --- users: deactivate, reactivate, resend invite --------------------------------------------------------------------------------

def test_deactivate_reactivate_and_resend_invite(client, ctx, person, member, role, token_of, mailoutbox):
    me = person("director")
    tom = member("technician")
    r = post(client, user_url(tom, "deactivate/"))
    assert r.status_code == 200 and r.json()["status"] == "deactivated"
    tom.refresh_from_db()
    assert not tom.is_active
    r = post(client, user_url(tom, "reactivate/"))
    assert r.status_code == 200 and r.json()["status"] == "active"
    tom.refresh_from_db()
    assert tom.is_active
    # these actions take no fields: one sent is refused, not dropped
    r = post(client, user_url(tom, "deactivate/"), {"reason": "left"})
    assert r.status_code == 400 and r.json() == {"reason": ["This action takes no fields."]}
    tom.refresh_from_db()
    assert tom.is_active and client.get(user_url(tom, "deactivate/")).status_code == 405
    # resend: a fresh link for a pending invitation only
    invitee = services.invite_user(ctx, email="maria@riverside.example", first_name="Maria", last_name="Santos", role=role("technician"))
    r = by_token("post", user_url(invitee, "resend-invite/"), None, token_of(me))
    invitee.refresh_from_db()
    assert r.status_code == 200 and r.json()["invitation_sent"] is True and invitee.invited_at is not None and len(mailoutbox) == 1
    first = invitee.invited_at
    assert post(client, user_url(invitee, "resend-invite/")).status_code == 200
    invitee.refresh_from_db()
    assert invitee.invited_at > first and len(mailoutbox) == 2
    r = post(client, user_url(tom, "resend-invite/"))
    assert r.status_code == 400 and "already has a password or is deactivated" in r.json()["detail"]
    assert by_token("post", user_url(invitee, "deactivate/"), None, token_of(me)).json()["status"] == "deactivated"
    r = post(client, user_url(invitee, "resend-invite/"))
    assert r.status_code == 400 and "there is no invitation to send" in r.json()["detail"] and len(mailoutbox) == 2


# --- users: privilege escalation -------------------------------------------------------------------------------------------------

def test_a_users_edit_holder_changes_no_user_or_role(client, ctx, person, member, role, clerk_role, techs, mailoutbox):
    """Users Edit is the credentials tab's level: every user and role change needs Full, as on the Users and Roles tabs."""
    director = member("director")
    invitee = services.invite_user(ctx, email="maria@riverside.example", first_name="Maria", last_name="Santos", role=role("technician"))
    clerk = person(clerk_role)
    to_director = {"role": str(role("director").id)}
    refused = [
        ("patch", user_url(clerk), to_director), ("patch", user_url(director), {"role": str(role("requester").id), "department": "ICU"}),
        ("post", USERS, {"email": "me2@riverside.example", "first_name": "Me", "last_name": "Again", **to_director}),
        ("post", user_url(director, "deactivate/"), None), ("post", user_url(director, "reactivate/"), None),
        ("post", user_url(invitee, "resend-invite/"), None),
        ("post", ROLES, {"name": "Everything", "copy_from": str(role("director").id)}),
        ("patch", role_url(clerk_role), {"levels": {"users": Level.FULL}}), ("patch", role_url(role("director")), {"levels": {"users": 0}}),
        ("patch", role_url(clerk_role), {"scope": "facility"}),
    ]
    for method, url, body in refused:
        assert call(client, method, url, body).status_code == 403, (method, url)
    clerk.refresh_from_db()
    director.refresh_from_db()
    assert clerk.role_id == clerk_role.id and director.is_active and director.role.slug == "director"
    assert clerk_role.level_for("users") == Level.EDIT and Role.objects.count() == 7 and mailoutbox == []
    assert not User.objects.filter(email="me2@riverside.example").exists()
    # what Users Edit is for still works
    assert post(client, cred_url(techs["tom"].credentials.get(), "renew/")).status_code == 200


def test_a_users_full_holder_cannot_raise_themself(client, ctx, person, member, role, admin_role, custom_role):
    """A custom role with Users Full manages others, never its own access: not the role, its levels, or what it sees."""
    me = person(admin_role)
    r = patch(client, user_url(me), {"role": str(role("director").id)})
    assert r.status_code == 400 and r.json() == {"detail": "You cannot change your own role."}
    r = patch(client, role_url(admin_role), {"levels": {"contracts": Level.FULL, "settings": Level.FULL}})
    assert r.status_code == 400 and r.json() == {"levels": {"contracts": ["You cannot change the permissions of your own role."]}}
    r = patch(client, role_url(admin_role), {"scope": "company"})
    assert r.status_code == 400 and r.json() == {"scope": ["You cannot change what your own role sees."]}
    shown = client.get(role_url(admin_role)).json()  # your own role's row sent back as it reads changes nothing, so it is fine
    assert patch(client, role_url(admin_role), shown).json() == shown
    r = post(client, user_url(me, "deactivate/"))
    assert r.status_code == 400 and r.json() == {"detail": "You cannot deactivate your own account."}
    me.refresh_from_db()
    admin_role.refresh_from_db()
    assert me.role_id == admin_role.id and me.is_active and admin_role.scope == DataScope.FACILITY
    assert (admin_role.level_for("contracts"), admin_role.level_for("settings")) == (Level.EDIT, Level.NONE)
    # the Director role is fixed for everyone
    r = patch(client, role_url(role("director")), {"levels": {"users": Level.VIEW}})
    assert r.status_code == 400 and r.json() == {"levels": {"users": ["The Director role is fixed and cannot be changed."]}}
    r = patch(client, role_url(role("director")), {"scope": "department"})
    assert r.status_code == 400 and r.json() == {"scope": ["The Director role is fixed and cannot be changed."]}
    assert role("director").level_for("users") == Level.FULL
    # others are theirs to manage, but only up to their own access (slice 19: nobody hands out more than they have)
    tom = member("technician")
    r = patch(client, user_url(tom), {"role": str(role("manager").id)})
    assert r.status_code == 400 and r.json() == {"detail": "You can give only a role whose access you have yourself: CE manager has more "
                                                           "Equipment access than your role."}
    clerk = custom_role("Contracts clerk", {"contracts": Level.EDIT})
    r = patch(client, user_url(tom), {"role": str(clerk.id)})  # nor move an account whose role has more access than theirs
    assert r.status_code == 400 and "You can change only accounts whose access you have yourself" in r.json()["detail"]
    viewer = member(custom_role("Contracts viewer", {"contracts": Level.VIEW}))
    assert patch(client, user_url(viewer), {"role": str(clerk.id)}).json()["role_name"] == "Contracts clerk"
    r = post(client, ROLES, {"name": "Copy of Director", "copy_from": str(role("director").id)})
    assert r.status_code == 400 and "access you have yourself" in str(r.json())
    r = patch(client, role_url(clerk), {"levels": {"settings": Level.FULL}})
    assert r.status_code == 400 and r.json() == {"levels": {"settings": ["You cannot give a role more Settings access than your own role has."]}}


def test_the_directors_protections(client, ctx, person, member, role, django_user_model):
    me = person("director")
    r = post(client, user_url(me, "deactivate/"))
    assert r.status_code == 400 and r.json() == {"detail": "You cannot deactivate your own account."}
    r = patch(client, user_url(me), {"role": str(role("manager").id)})
    assert r.status_code == 400 and r.json() == {"detail": "You cannot change your own role."}
    me.refresh_from_db()
    assert me.is_active and me.role.slug == "director"
    # the only Users Full holder is the director: no one else can deactivate or demote them
    person("manager")
    assert post(client, user_url(me, "deactivate/")).status_code == 403
    assert patch(client, user_url(me), {"role": str(role("manager").id)}).status_code == 403
    me.refresh_from_db()
    assert me.is_active and me.role.slug == "director"
    # superusers are managed in Admin
    client.force_login(me)
    root = django_user_model.objects.create_superuser("root@riverside.example", "root@riverside.example", PASSWORD, tenant=ctx)
    r = post(client, user_url(root, "deactivate/"))
    assert r.status_code == 400 and r.json() == {"detail": "Superusers are managed in Admin."}
    root.is_active = False
    root.save()
    r = post(client, user_url(root, "reactivate/"))
    assert r.status_code == 400 and r.json() == {"detail": "Superusers are managed in Admin."}
    root.refresh_from_db()
    assert not root.is_active


def test_a_superuser_with_no_facility_is_told_to_pick_one(db, tenant, member, role, techs, mailoutbox):
    """User is not tenant-scoped: without the check, the list would be every account with no facility."""
    root = User.objects.create_superuser(username="root", password=PASSWORD, email="root@example.com")
    User.objects.create_superuser(username="root2", password=PASSWORD, email="root2@example.com")
    token = Token.objects.create(user=root)
    someone = member("technician")
    cred = techs["tom"].credentials.get()
    for method, url, body in [("get", USERS, None), ("get", user_url(someone), None), ("get", ROLES, None), ("get", TECHS, None), ("get", CREDS, None),
                              ("post", USERS, {"email": "x@example.com", "first_name": "X", "last_name": "Y", "role": str(role("director").id)}),
                              ("patch", user_url(someone), {"department": "Finance"}), ("post", ROLES, {"name": "R", "copy_from": str(role("manager").id)}),
                              ("post", cred_url(cred, "renew/"), None), ("delete", cred_url(cred), None)]:
        r = by_token(method, url, body, token)
        assert r.status_code == 403 and r.json() == {"detail": "Pick a tenant first (Admin, Tenants)."}, (method, url)
    someone.refresh_from_db()
    assert someone.department == "" and not User.objects.filter(email="x@example.com").exists() and mailoutbox == []
    with tenant_context(tenant):
        assert Credential.objects.filter(pk=cred.pk).exists()


def test_a_deactivated_users_token_is_refused(ctx, member, role, token_of):
    director, manager = member("director"), member("manager")
    boss, mine = token_of(director), token_of(manager)
    assert by_token("get", USERS, None, mine).status_code == 200
    assert by_token("post", user_url(manager, "deactivate/"), None, boss).status_code == 200
    r = by_token("get", USERS, None, mine)  # deactivating deletes their tokens (slice 19)
    assert r.status_code == 403 and r.json() == {"detail": "Invalid token."} and not Token.objects.filter(user=manager).exists()
    assert by_token("post", user_url(manager, "reactivate/"), None, boss).status_code == 200
    assert by_token("get", USERS, None, mine).status_code == 403  # reactivating never brings the old token back
    assert by_token("get", USERS, None, token_of(manager)).status_code == 200
    # a deactivated director's token writes nothing either
    User.objects.filter(pk=director.pk).update(is_active=False)
    body = {"email": "x@riverside.example", "first_name": "X", "last_name": "Y", "role": str(role("director").id)}
    r = by_token("post", USERS, body, boss)
    assert r.status_code == 403 and r.json() == {"detail": "User inactive or deleted."}
    assert not User.objects.filter(email="x@riverside.example").exists()


def endpoints(user, invitee, some_role, cred, training, tech):
    """Every users-and-access endpoint: (method, url, body)."""
    return [
        ("get", USERS, None), ("get", user_url(user), None),
        ("post", USERS, {"email": "x@riverside.example", "first_name": "X", "last_name": "Y", "role": str(some_role.id)}),
        ("patch", user_url(user), {"department": "Finance"}), ("post", user_url(user, "deactivate/"), None),
        ("post", user_url(user, "reactivate/"), None), ("post", user_url(invitee, "resend-invite/"), None),
        ("get", ROLES, None), ("get", role_url(some_role), None), ("post", ROLES, {"name": "R", "copy_from": str(some_role.id)}),
        ("patch", role_url(some_role), {"levels": {"contracts": 1}}),
        ("get", TECHS, None), ("get", f"{TECHS}{tech.id}/", None),
        ("get", CREDS, None), ("get", cred_url(cred), None),
        ("post", CREDS, {"technician": str(tech.id), "scope": "category", "value": "Ventilators", "source": "OEM training",
                         "issued_on": TODAY.isoformat()}),
        ("post", cred_url(cred, "renew/"), None), ("post", cred_url(training, "sign-off/"), None), ("delete", cred_url(cred), None),
    ]


@pytest.mark.parametrize("who", ["vendor", "requester", "scoped-company", "scoped-department"])
def test_scoped_users_are_refused_by_every_endpoint(client, ctx, person, member, role, custom_role, techs, training, vent_model, who, mailoutbox):
    """Closed by default (ModulePermission): no users-and-access endpoint narrows to a scoped user's share, so none names scoped
    actions; a scoped custom role with Full everywhere is refused too."""
    if who.startswith("scoped-"):
        scope = who.split("-")[1]
        person(custom_role(who, ALL_FULL, scope=scope), company="Philips", department="ICU")
    else:
        person(who, company="Philips", department="ICU")
    tom = member("technician")
    invitee = services.invite_user(ctx, email="maria@riverside.example", first_name="Maria", last_name="Santos", role=role("technician"))
    cred = techs["tom"].credentials.get(value="Infusion pumps")
    counts = (User.objects.count(), Role.objects.count(), Credential.objects.count())
    for method, url, body in endpoints(tom, invitee, role("manager"), cred, training, techs["dana"]):
        r = call(client, method, url, body)
        assert r.status_code == 403, (method, url, r.status_code)
        if who.startswith("scoped-"):
            assert "part of this facility" in r.json()["detail"], url
    tom.refresh_from_db()
    training.refresh_from_db()
    assert (User.objects.count(), Role.objects.count(), Credential.objects.count()) == counts
    assert tom.is_active and tom.department == "" and training.status == "in_training" and role("manager").level_for("contracts") == Level.EDIT
    assert mailoutbox == []


def test_another_facilitys_rows_are_404_and_its_ids_no_choice(client, ctx, person, role, techs, theirs, vent_model, mailoutbox):
    person("director")
    u, r_, t, c = theirs["user"], theirs["role"], theirs["tech"], theirs["cred"]
    for method, url, body in [("get", user_url(u), None), ("patch", user_url(u), {"department": "Finance"}), ("post", user_url(u, "deactivate/"), None),
                              ("post", user_url(u, "reactivate/"), None), ("post", user_url(u, "resend-invite/"), None),
                              ("get", role_url(r_), None), ("patch", role_url(r_), {"levels": {"contracts": 0}}),
                              ("get", f"{TECHS}{t.id}/", None), ("get", cred_url(c), None), ("post", cred_url(c, "renew/"), None),
                              ("post", cred_url(c, "sign-off/"), None), ("delete", cred_url(c), None)]:
        assert call(client, method, url, body).status_code == 404, (method, url)
    ours = {r["id"] for r in results(client.get(ROLES))} | {x["id"] for x in results(client.get(TECHS))} | {x["id"] for x in results(client.get(CREDS))}
    assert not ours & {str(r_.id), str(t.id), str(c.id)}
    # their ids in a body are no choice here
    r = post(client, ROLES, {"name": "Copycat", "copy_from": str(r_.id)})
    assert r.status_code == 400 and r.json() == {"copy_from": ["Choose a role."]}
    r = post(client, CREDS, {"technician": str(t.id), "scope": "category", "value": "Ventilators", "source": "OEM training", "issued_on": TODAY.isoformat()})
    assert r.status_code == 400 and r.json() == {"technician": ["Choose one of this facility's active technicians."]}
    with tenant_context(theirs["user"].tenant):
        c.refresh_from_db()
        assert r_.level_for("contracts") == Level.EDIT and c.expires_on == TODAY + timedelta(days=100)
    theirs["user"].refresh_from_db()
    assert theirs["user"].is_active and theirs["user"].department == "" and mailoutbox == []


# --- roles ------------------------------------------------------------------------------------------------------------------

def test_the_roles_list_is_the_matrix(client, ctx, person, member, role, custom_role, theirs):
    person("manager")  # Users View
    member("vendor")  # no company: sees nothing yet
    member("vendor", company="Philips")
    member("technician", is_active=False)
    custom = custom_role("Biomed contractor", {"workorders": Level.EDIT}, scope=DataScope.COMPANY)
    rows = results(client.get(ROLES))
    assert [r["slug"] for r in rows] == STANDARD_ORDER + ["biomed-contractor"] and all(set(r) == ROLE_FIELDS for r in rows)
    by = {r["slug"]: r for r in rows}
    d, v, c = by["director"], by["vendor"], by["biomed-contractor"]
    assert d["levels"] == {m: Level.FULL for m in Module.values} and (d["is_system"], d["standard"], d["scope"]) == (True, True, "facility")
    assert d["scope_fixed"] == "The Director role is fixed and cannot be changed." and d["users"] == 0
    assert (v["scope"], v["users"], v["users_seeing_nothing"], v["is_system"], v["standard"]) == ("company", 2, 1, False, True)
    assert "always sees only work orders assigned to their company" in v["scope_fixed"]
    assert (c["standard"], c["scope"], c["scope_fixed"], c["levels"]["workorders"], c["levels"]["users"]) == (False, "company", None, Level.EDIT, Level.NONE)
    assert by["manager"]["levels"] == {m: role("manager").level_for(m) for m in Module.values} and by["manager"]["users"] == 1
    assert by["technician"]["users"] == services.role_user_counts(ctx).get(role("technician").id, 0) == 0
    one = client.get(role_url(custom))
    assert one.status_code == 200 and one.json() == c
    assert client.get(role_url(theirs["role"])).status_code == 404 and client.get(f"{ROLES}not-a-uuid/").status_code == 404
    person("technician")  # Users None
    assert client.get(ROLES).status_code == 403 and client.get(role_url(custom)).status_code == 403


def test_add_role_as_the_modal_does(client, ctx, person, member, role, other_roles, token_of):
    me = person("director")
    r = post(client, ROLES, {"name": "Sterile processing lead", "copy_from": str(role("manager").id), "description": "Runs ICU"})
    assert r.status_code == 201, r.content
    data = r.json()
    new = Role.objects.get(pk=data["id"])
    assert (data["slug"], data["description"], data["standard"], data["is_system"], data["scope"]) == (
        "sterile-processing-lead", "Runs ICU", False, False, "facility")
    assert data["levels"] == {m: role("manager").level_for(m) for m in Module.values} == {m: new.level_for(m) for m in Module.values}
    assert new.history.first().history_user == me and new.scope == DataScope.FACILITY
    # by token, narrowed; the same role the service makes
    r = by_token("post", ROLES, {"name": "Unit educator", "copy_from": str(role("requester").id), "scope": "department"}, token_of(me))
    assert r.status_code == 201 and (r.json()["scope"], r.json()["description"]) == ("department", "Copied from Clinical requester")
    twin = services.create_role(name="Unit educator", copy_from=role("requester"), scope=DataScope.DEPARTMENT)
    made = Role.objects.get(pk=r.json()["id"])
    def levels(r):
        return {m: r.level_for(m) for m in Module.values}

    assert (made.scope, made.description, levels(made)) == (twin.scope, twin.description, levels(twin))
    count = Role.objects.count()
    copy = str(role("manager").id)
    for body, errors in [
        ({"name": "  ", "copy_from": copy}, {"name": ["This field may not be blank."]}),
        ({"name": "X", "copy_from": str(role("manager", other_roles).id)}, {"copy_from": ["Choose a role."]}),
        ({"name": "X"}, {"copy_from": ["This field is required."]}),
        ({"name": "X", "copy_from": copy, "scope": "world"}, {"scope": [SCOPE_CHOICE_MESSAGE]}),
        ({"name": "X", "copy_from": copy, "levels": {"users": Level.FULL}}, {"levels": ["Set this with PATCH once the role is added."]}),
        ({"name": "X", "copy_from": copy, "is_system": True}, {"is_system": ["Unknown field."]}),
    ]:
        r = post(client, ROLES, body)
        assert r.status_code == 400 and r.json() == errors, (body, r.content)
    assert Role.objects.count() == count
    person("manager")
    assert post(client, ROLES, {"name": "X", "copy_from": copy}).status_code == 403 and Role.objects.count() == count


def test_change_a_roles_levels_and_scope(client, ctx, person, member, role, custom_role, token_of):
    me = person("director")
    manager = role("manager")
    r = patch(client, role_url(manager), {"levels": {"contracts": Level.APPROVE}})
    assert r.status_code == 200 and r.json()["levels"]["contracts"] == Level.APPROVE == manager.level_for("contracts")
    r = patch(client, role_url(manager), {"levels": {"contracts": Level.FULL, "reports": Level.REQUEST}, "scope": "department"})
    assert r.status_code == 200
    assert (r.json()["levels"]["contracts"], r.json()["levels"]["reports"], r.json()["scope"]) == (Level.FULL, Level.REQUEST, "department")
    manager.refresh_from_db()
    assert (manager.level_for("contracts"), manager.level_for("reports"), manager.scope) == (Level.FULL, Level.REQUEST, "department")
    assert manager.history.first().history_user == me
    # a narrowed role says who of its users now sees nothing
    member("manager")
    assert client.get(role_url(manager)).json()["users_seeing_nothing"] == 1
    # by token
    custom = custom_role("Biomed contractor", {"workorders": Level.EDIT})
    r = by_token("patch", role_url(custom), {"scope": "company", "levels": {"equipment": Level.VIEW}}, token_of(me))
    assert r.status_code == 200 and (r.json()["scope"], r.json()["levels"]["equipment"]) == ("company", Level.VIEW)
    # all or nothing, and each refusal says which cell
    for body, errors in [
        ({"levels": {"contracts": Level.VIEW, "billing": Level.EDIT}}, {"levels": {"billing": ["Unknown module."]}}),
        ({"levels": {"contracts": 9}}, {"levels": {"contracts": ["Unknown permission level."]}}),
        ({"levels": {"contracts": "lots"}}, {"levels": {"contracts": ["A valid integer is required."]}}),
        ({"levels": [1, 2]}, {"levels": ['Expected a dictionary of items but got type "list".']}),
        ({"levels": {"contracts": Level.VIEW}, "scope": "everyone"}, {"scope": [SCOPE_CHOICE_MESSAGE]}),
        ({"name": "Boss"}, {"name": ["A role's name and description are set when it is added."]}),
        ({"permissions": []}, {"permissions": ["Unknown field."]}),
    ]:
        r = patch(client, role_url(manager), body)
        assert r.status_code == 400 and r.json() == errors, (body, r.content)
    manager.refresh_from_db()
    assert (manager.level_for("contracts"), manager.scope, manager.name) == (Level.FULL, "department", "CE manager")
    # the default vendor role keeps its scope; sending it back as it is is fine
    vendor = role("vendor")
    r = patch(client, role_url(vendor), {"scope": "facility"})
    assert r.status_code == 400 and "always sees only work orders assigned to their company" in r.json()["scope"][0]
    assert patch(client, role_url(vendor), {"scope": "company"}).status_code == 200
    # the Director's row sent back as it reads changes nothing
    shown = client.get(role_url(role("director"))).json()
    assert patch(client, role_url(role("director")), shown).json() == shown
    # PUT and DELETE are not offered; Users View cannot change a role
    assert call(client, "put", role_url(manager), {"levels": {}}).status_code == 405 and client.delete(role_url(manager)).status_code == 405
    person("manager")
    assert patch(client, role_url(vendor), {"levels": {"contracts": Level.VIEW}}).status_code == 403
    assert role("vendor").level_for("contracts") == Level.NONE


# --- technicians -------------------------------------------------------------------------------------------------------------

def test_technicians_are_read_only(client, ctx, person, techs, theirs):
    person("manager")  # Users View
    rows = results(client.get(TECHS))
    assert [t["name"] for t in rows] == ["Dana Whitfield", "Tom Okafor"] and len(rows[0]["credentials"]) == 2
    assert client.get(f"{TECHS}{techs['tom'].id}/").json()["name"] == "Tom Okafor"
    assert client.get(f"{TECHS}{theirs['tech'].id}/").status_code == 404
    person("director")
    tom = f"{TECHS}{techs['tom'].id}/"
    for method, url in [("post", TECHS), ("patch", tom), ("put", tom), ("delete", tom)]:
        assert call(client, method, url, {"name": "Changed", "title": "BMET III"}).status_code == 405, (method, url)
    assert Technician.objects.count() == 2 and Technician.objects.get(pk=techs["tom"].pk).name == "Tom Okafor"
    person("technician")  # Users None
    assert client.get(TECHS).status_code == 403


# --- credentials ---------------------------------------------------------------------------------------------------------------

def test_credentials_list_and_filter(client, ctx, person, techs, training, theirs):
    person("manager")
    rows = results(client.get(CREDS))
    assert len(rows) == 4 and all(set(c) == CRED_FIELDS for c in rows) and str(theirs["cred"].id) not in {c["id"] for c in rows}
    states = {(c["technician_name"], c["value"]): c["state"] for c in rows}
    assert states == {("Dana Whitfield", "Hamilton-G5"): "ok", ("Dana Whitfield", "Infusion pumps"): "ok", ("Tom Okafor", "Infusion pumps"): "expiring",
                      ("Tom Okafor", "Ventilators"): "in_training"}
    assert {c["value"] for c in results(client.get(f"{CREDS}?technician={techs['dana'].id}"))} == {"Hamilton-G5", "Infusion pumps"}
    assert results(client.get(f"{CREDS}?technician={theirs['tech'].id}")) == []
    assert client.get(f"{CREDS}?technician=garbage").status_code == 400


def test_add_credential_as_the_modal_does(client, ctx, person, member, techs, vent_model, pump_model, clerk_role, token_of):
    person(clerk_role)  # Users Edit
    body = {"technician": str(techs["tom"].id), "scope": "manufacturer", "value": "Hamilton Medical", "source": "OEM training",
            "issued_on": TODAY.isoformat(), "expires_on": (TODAY + timedelta(days=730)).isoformat()}
    r = post(client, CREDS, body)
    assert r.status_code == 201, r.content
    cred = Credential.objects.get(pk=r.json()["id"])
    assert (cred.technician_id, cred.scope, cred.value, cred.source, cred.status, cred.issued_on, cred.expires_on) == (
        techs["tom"].id, "manufacturer", "Hamilton Medical", "OEM training", "active", TODAY, TODAY + timedelta(days=730))
    assert r.json()["state"] == "ok" and set(r.json()) == CRED_FIELDS
    # by token: in training, no expiry
    r = by_token("post", CREDS, {"technician": str(techs["dana"].id), "scope": "model", "value": "Alaris 8015 PCU", "source": "In-house sign-off",
                                 "status": "in_training", "issued_on": TODAY.isoformat(), "expires_on": None}, token_of(member("director")))
    assert r.status_code == 201 and (r.json()["status"], r.json()["expires_on"], r.json()["state"]) == ("in_training", None, "in_training")
    # the browsable API's HTML form: multipart, with its CSRF token beside the fields
    r = client.post(CREDS, {"csrfmiddlewaretoken": "x", "technician": str(techs["tom"].id), "scope": "category", "value": "Ventilators",
                            "source": "Third-party course", "issued_on": TODAY.isoformat(), "expires_on": ""})
    assert r.status_code == 201, r.content
    assert (r.json()["value"], r.json()["expires_on"], r.json()["status"]) == ("Ventilators", None, "active")
    count = Credential.objects.count()
    gone = Technician.objects.create(name="Gone Tech", is_active=False)
    for change, errors in [
        ({"technician": str(gone.id)}, {"technician": ["Choose one of this facility's active technicians."]}),
        ({"value": "Lasers"}, {"value": ["Choose what the credential covers: a category, manufacturer, or model in this facility's catalog."]}),
        ({"scope": "category", "value": "Hamilton Medical"}, {"value": [
            "Choose what the credential covers: a category, manufacturer, or model in this facility's catalog."]}),
        ({"scope": "department"}, {"scope": ['"department" is not a valid choice.']}),
        ({"source": "A friend"}, {"source": ['"A friend" is not a valid choice.']}),
        ({"expires_on": (TODAY - timedelta(days=1)).isoformat()}, {"expires_on": ["The expiry date cannot be before the issue date."]}),
        ({"issued_on": None}, {"issued_on": ["This field may not be null."]}),
        ({"issued_on": "2026-13-45"}, None),
        ({"notes": "Patient in bed 4"}, {"notes": ["Unknown field."]}),
        ({}, {"detail": "Tom Okafor already has a manufacturer credential for Hamilton Medical."}),
    ]:
        r = post(client, CREDS, {**body, **change})
        assert r.status_code == 400 and (errors is None or r.json() == errors), (change, r.content)
    assert Credential.objects.count() == count
    person("manager")  # Users View
    assert post(client, CREDS, {**body, "value": "Ventilators", "scope": "category"}).status_code == 403 and Credential.objects.count() == count


def test_renew_sign_off_and_remove_as_the_tab_does(client, ctx, person, member, techs, training, clerk_role, token_of):
    person(clerk_role)
    tom_cred = techs["tom"].credentials.get(value="Infusion pumps")
    r = post(client, cred_url(tom_cred, "renew/"))
    assert r.status_code == 200 and r.json()["expires_on"] == add_months(TODAY, 24).isoformat()
    tom_cred.refresh_from_db()
    assert tom_cred.expires_on == add_months(TODAY, 24)
    # the services' rules, in their words
    r = post(client, cred_url(training, "renew/"))
    assert r.status_code == 400 and r.json() == {"detail": "Sign off the credential first; a credential in training cannot be renewed."}
    no_expiry = techs["dana"].credentials.get(value="Infusion pumps")
    r = post(client, cred_url(no_expiry, "renew/"))
    assert r.status_code == 400 and r.json() == {"detail": "This credential has no expiry, so there is nothing to renew."}
    r = post(client, cred_url(tom_cred, "renew/"), {"months": 12})
    assert r.status_code == 400 and r.json() == {"months": ["This action takes no fields."]}
    r = by_token("post", cred_url(training, "sign-off/"), None, token_of(member("director")))
    assert r.status_code == 200 and (r.json()["status"], r.json()["source"], r.json()["issued_on"]) == ("active", "In-house sign-off", TODAY.isoformat())
    r = post(client, cred_url(training, "sign-off/"))
    assert r.status_code == 400 and r.json() == {"detail": "Only a credential in training can be signed off."}
    # not edited in place
    for method in ("patch", "put"):
        assert call(client, method, cred_url(tom_cred), {"expires_on": "2040-01-01"}).status_code == 405
    tom_cred.refresh_from_db()
    assert tom_cred.expires_on == add_months(TODAY, 24)
    # Users View reads but changes nothing
    person("manager")
    assert post(client, cred_url(tom_cred, "renew/")).status_code == 403 and client.delete(cred_url(tom_cred)).status_code == 403
    person(clerk_role)
    assert client.delete(cred_url(tom_cred)).status_code == 204 and not Credential.objects.filter(pk=tom_cred.pk).exists()
    assert by_token("delete", cred_url(training), None, token_of(member("director"))).status_code == 204
    assert client.get(cred_url(training)).status_code == 404


# --- under row-level security ----------------------------------------------------------------------------------------------------

@needs_postgres
def test_users_and_access_by_token_under_the_policies(tenant, other_roles, member, role, techs, training, vent_model, units, theirs, mailoutbox):
    """As the runtime role, every kind of users-and-access endpoint by token reads and writes inside the token's facility only."""
    token = Token.objects.create(user=member("director"))
    tom = member("technician", department="Clinical Engineering")
    cred = techs["tom"].credentials.get(value="Infusion pumps")
    requester, vendor = str(role("requester").id), str(role("vendor").id)  # read here: the test's own queries are not a request's
    as_app_role()
    assert {u["id"] for u in results(by_token("get", USERS, None, token))} == {token.user_id, tom.pk}
    assert by_token("get", user_url(theirs["user"]), None, token).status_code == 404
    r = by_token("post", USERS, {"email": "nurse@riverside.example", "first_name": "Ana", "last_name": "Ruiz", "role": requester,
                                 "department": "icu", "create_technician": True}, token)
    assert r.status_code == 201 and r.json()["invitation_sent"] is True and r.json()["department"] == "ICU" and r.json()["technician"]
    nurse = User.objects.get(pk=r.json()["id"])
    assert by_token("post", user_url(nurse, "resend-invite/"), None, token).json()["invitation_sent"] is True and len(mailoutbox) == 2
    r = by_token("patch", user_url(tom), {"role": vendor, "company": "Philips", "department": "External vendor"}, token)
    assert r.status_code == 200 and r.json()["scope"] == "company"
    assert by_token("post", user_url(tom, "deactivate/"), None, token).json()["status"] == "deactivated"
    assert by_token("post", user_url(tom, "reactivate/"), None, token).json()["status"] == "active"
    assert [x["slug"] for x in results(by_token("get", ROLES, None, token))][:6] == STANDARD_ORDER
    r = by_token("post", ROLES, {"name": "Unit educator", "copy_from": requester, "scope": "department"}, token)
    assert r.status_code == 201
    new_role = r.json()["id"]
    r = by_token("patch", f"{ROLES}{new_role}/", {"levels": {"reports": Level.VIEW}, "scope": "facility"}, token)
    assert r.status_code == 200 and (r.json()["levels"]["reports"], r.json()["scope"]) == (Level.VIEW, "facility")
    assert by_token("get", role_url(theirs["role"]), None, token).status_code == 404
    assert len(results(by_token("get", TECHS, None, token))) == 3  # Dana, Tom, and the nurse's new profile
    r = by_token("post", CREDS, {"technician": str(techs["dana"].id), "scope": "category", "value": "Ventilators", "source": "OEM training",
                                 "issued_on": TODAY.isoformat()}, token)
    assert r.status_code == 201
    assert by_token("post", cred_url(cred, "renew/"), None, token).json()["expires_on"] == add_months(TODAY, 24).isoformat()
    assert by_token("post", cred_url(training, "sign-off/"), None, token).json()["status"] == "active"
    assert by_token("delete", cred_url(cred), None, token).status_code == 204
    assert by_token("get", cred_url(theirs["cred"]), None, token).status_code == 404
    assert Client().get(USERS, HTTP_ACCEPT="text/html", HTTP_AUTHORIZATION=f"Token {token.key}").status_code == 200  # the browsable API
    with tenant_context(tenant):
        assert Role.objects.get(pk=new_role).level_for("reports") == Level.VIEW and Technician.objects.filter(user=nurse).exists()
        assert not Credential.objects.filter(pk=cred.pk).exists() and Credential.objects.filter(value="Ventilators", technician=techs["dana"]).exists()
    with tenant_context(other_roles):
        assert Credential.objects.filter(pk=theirs["cred"].pk).exists() and Role.objects.filter(slug="unit-educator").count() == 0
