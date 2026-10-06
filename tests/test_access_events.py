"""
Records of access changes (slice 20, part B; apps.accounts.models.AccessEvent): every service that changes who may do what writes one
event in the same transaction as the change (an invitation and a resend, a user's role, company, or unit, deactivating and
reactivating, adding a role, a role's level and what it sees), with who did it, whose account or which role, and what changed in
words; a refused change, or one that changes nothing, writes none. The Users and Roles tabs and the API write them through the same
services. Events stay in their facility, and a command writes them inside the account's facility. The signed-out reset form writes
none (slice 22): a fresh invitation it sends is not a staff change, and must not tell the facility someone asked.
"""
import smtplib

import pytest
from django.core.exceptions import ValidationError
from rest_framework.authtoken.models import Token
from test_rls_paths import rls  # noqa: F401

from apps.accounts import invitations, services
from apps.accounts.models import AccessEvent, DataScope, Level, Role, User, create_default_roles
from apps.core import history
from apps.equipment.models import Department
from apps.tenants.context import tenant_context

Action = AccessEvent.Action
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def role(tenant):
    def _get(slug, tenant_=None):
        return Role.unscoped.get(tenant=tenant_ or tenant, slug=slug)  # unscoped: fixtures run outside a request context

    return _get


@pytest.fixture
def kim(make_user):
    return make_user("director", username="kim@riverside.example")


@pytest.fixture
def units(ctx, dept):
    return {"ICU": dept, "ED": Department.objects.create(name="ED")}


def events(**filters):
    return list(AccessEvent.objects.filter(**filters).order_by("at", "pk"))


def only(action):
    """The one event of `action` in the current facility."""
    found = events(action=action)
    assert len(found) == 1, [(e.action, e.detail) for e in AccessEvent.objects.all()]
    return found[0]


def _with(user, **fields):
    for k, v in fields.items():
        setattr(user, k, v)
    user.save(update_fields=list(fields))
    return user


def _broken_mail(monkeypatch):
    def boom(self, fail_silently=False):
        raise smtplib.SMTPException("mail server unreachable")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", boom)


# --- users ---------------------------------------------------------------------------------------------------------------------

def test_inviting_writes_one_event_with_the_role_and_what_the_user_sees_by(ctx, role, kim, units):
    ana = services.invite_user(ctx, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=role("technician"), by=kim,
                               department="Clinical Engineering", create_technician=True)
    e = only(Action.INVITED)
    assert (e.by, e.user, e.role, e.tenant) == (kim, ana, role("technician"), ctx)
    assert e.detail == "Invited as Technician; department: Clinical Engineering; technician profile added"
    services.invite_user(ctx, email="v@riverside.example", first_name="Vic", last_name="Ng", role=role("vendor"), company="Philips", by=kim)
    services.invite_user(ctx, email="r@riverside.example", first_name="Rae", last_name="Li", role=role("requester"), department="icu", by=kim)
    assert [e.detail for e in events(action=Action.INVITED)][1:] == ["Invited as Vendor technician; company: Philips",
                                                                     "Invited as Clinical requester; unit: ICU"]


def test_a_refused_invitation_writes_nothing(ctx, role, kim, make_user):
    make_user("technician", username="taken@riverside.example")
    clerk = make_user("manager", username="clerk@riverside.example")  # Users View only, and not the director's access
    for kwargs in ({"email": "TAKEN@riverside.example"}, {"email": ""}, {"email": "x@riverside.example", "role": role("vendor")}):
        with pytest.raises(ValidationError):
            services.invite_user(ctx, **{"first_name": "A", "last_name": "B", "role": role("technician"), "by": kim, **kwargs})
    with pytest.raises(ValidationError):
        services.invite_user(ctx, email="y@riverside.example", first_name="A", last_name="B", role=role("director"), by=clerk)
    assert events() == []


def test_a_command_writes_the_event_inside_the_accounts_facility(tenant, role, rls):  # noqa: F811
    """invite_user from a command or a test runs with no tenant set: the event is written inside the facility, as the policy needs."""
    technician = role("technician")
    with rls:
        services.invite_user(tenant, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=technician)
    assert rls.violations == []
    with tenant_context(tenant):
        e = only(Action.INVITED)
    assert e.by is None and e.tenant == tenant


def test_a_resend_writes_an_event_and_the_first_send_and_a_failed_one_do_not(ctx, role, kim, mailoutbox, monkeypatch):
    ana = services.invite_user(ctx, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=role("technician"), by=kim)
    assert invitations.send_invitation(ana, by=kim)  # the first email: the Invited event is that one
    assert events(action=Action.INVITATION_RESENT) == []
    assert invitations.send_invitation(ana, by=kim, resend=True)  # Resend invite
    e = only(Action.INVITATION_RESENT)
    assert (e.by, e.user, e.role) == (kim, ana, role("technician")) and e.detail == "A new link to ana@riverside.example replaces the earlier one"
    _broken_mail(monkeypatch)
    assert invitations.send_invitation(ana, by=kim, resend=True) is False  # nothing changed, nothing written
    assert len(events(action=Action.INVITATION_RESENT)) == 1


def test_the_reset_form_resending_a_pending_invitation_writes_no_event(client, tenant, role, rls, mailoutbox):  # noqa: F811
    """Signed out, no tenant set: a pending account asking for a reset gets a fresh invitation, and the facility's log gets nothing
    (slice 22): nobody on the staff changed anything, and an event would tell the facility that someone asked about the address."""
    ana = services.invite_user(tenant, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=role("technician"))
    assert invitations.send_invitation(ana)
    with rls:
        assert client.post("/password-reset/", {"email": "ana@riverside.example"}).status_code == 302
    assert rls.violations == [] and len(mailoutbox) == 2
    with tenant_context(tenant):
        assert events(action=Action.INVITATION_RESENT) == [] and len(events()) == 1  # the invitation's own event only


def test_a_role_change_writes_before_and_after_in_words(ctx, role, kim, make_user, units):
    tom = make_user("technician", username="tom@riverside.example")
    services.set_user_role(tom, role("manager"), by=kim)
    e = only(Action.ROLE_CHANGED)
    assert (e.by, e.user, e.role, e.detail) == (kim, tom, role("manager"), "Technician → CE manager")
    services.set_user_role(tom, role("vendor"), company=" GE HealthCare ", by=kim)
    assert events(action=Action.ROLE_CHANGED)[-1].detail == "CE manager → Vendor technician; company: — → GE HealthCare"
    _with(tom, department="icu")
    services.set_user_role(tom, role("requester"), by=kim)
    assert events(action=Action.ROLE_CHANGED)[-1].detail == "Vendor technician → Clinical requester; unit: icu → ICU"
    nobody = _with(make_user("technician", username="new@riverside.example"), role=None)
    services.set_user_role(nobody, role("technician"), by=kim)
    assert events(action=Action.ROLE_CHANGED)[-1].detail == "No role → Technician"
    assert events(action=Action.SCOPE_CHANGED) == []


def test_a_refused_role_change_writes_nothing(ctx, role, kim, make_user, units):
    tom = make_user("technician", username="tom@riverside.example")
    for call in (lambda: services.set_user_role(kim, role("manager"), by=kim),  # your own role
                 lambda: services.set_user_role(tom, role("vendor"), by=kim),  # the company the role needs
                 lambda: services.set_user_role(tom, role("requester"), department="Finance", by=kim),  # not one of the facility's units
                 lambda: services.set_user_access(kim, role=role("manager"), by=make_user("manager"))):  # outranked, and the last director
        with pytest.raises(ValidationError):
            call()
    assert events() == []


def test_the_same_role_again_writes_nothing_and_a_new_company_with_it_a_scope_change(ctx, role, kim, make_user):
    vendor = _with(make_user("vendor"), company="Philips")
    services.set_user_role(vendor, role("vendor"), by=kim)
    assert events() == []
    services.set_user_role(vendor, role("vendor"), company="Philips field service", by=kim)
    e = only(Action.SCOPE_CHANGED)
    assert (e.user, e.role, e.detail) == (vendor, role("vendor"), "Company: Philips → Philips field service")


def test_a_company_or_unit_change_writes_a_scope_event(ctx, role, kim, make_user, units):
    requester = _with(make_user("requester"), department="ICU")
    services.set_user_scope(requester, department="ED", by=kim)
    services.set_user_scope(requester, department="ED", by=kim)  # no change, no event
    e = only(Action.SCOPE_CHANGED)
    assert (e.by, e.user, e.role, e.detail) == (kim, requester, role("requester"), "Unit: ICU → ED")
    tech = make_user("technician")
    services.set_user_access(tech, role=role("technician"), department="Finance", by=kim)  # Edit with the same role
    assert events(action=Action.SCOPE_CHANGED)[-1].detail == "Department: — → Finance"
    with pytest.raises(ValidationError):
        services.set_user_scope(requester, department="Nowhere", by=kim)
    with pytest.raises(ValidationError):
        services.set_user_scope(requester, department="ICU", by=requester)  # a scoped user's own
    assert len(events(action=Action.SCOPE_CHANGED)) == 2


def test_deactivating_and_reactivating(ctx, role, kim, make_user, dept):
    tom = make_user("technician", username="tom@riverside.example")
    Token.objects.create(user=tom)
    services.deactivate_user(tom, by=kim)
    services.deactivate_user(tom, by=kim)  # already deactivated: nothing more to record
    e = only(Action.DEACTIVATED)
    assert (e.by, e.user, e.role, e.detail) == (kim, tom, role("technician"), "Technician; can no longer sign in; API token removed")
    services.reactivate_user(tom, by=kim)
    services.reactivate_user(tom, by=kim)
    e = only(Action.REACTIVATED)
    assert (e.by, e.user, e.detail) == (kim, tom, "Technician; can sign in again")
    ana = services.invite_user(ctx, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=role("requester"), department="ICU", by=kim)
    services.deactivate_user(ana, by=kim)
    services.reactivate_user(ana, by=kim)
    assert events(action=Action.DEACTIVATED)[-1].detail == "Invitation as Clinical requester withdrawn; its link stops working"
    assert events(action=Action.REACTIVATED)[-1].detail == "Invitation as Clinical requester open again; Resend invite sends a new link"


def test_refused_deactivations_and_reactivations_write_nothing(ctx, role, kim, make_user, django_user_model):
    clerk = make_user("technician", username="clerk@riverside.example")
    clerk_role = Role.objects.create(name="Admin clerk", slug="admin-clerk")
    clerk_role.set_levels({"users": Level.FULL})
    _with(clerk, role=clerk_role)
    root = django_user_model.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    other_director = _with(make_user("director", username="dir2@riverside.example"), is_active=False)
    for call in (lambda: services.deactivate_user(kim, by=kim), lambda: services.deactivate_user(kim, by=clerk),
                 lambda: services.deactivate_user(root, by=kim), lambda: services.reactivate_user(other_director, by=clerk)):
        with pytest.raises(ValidationError):
            call()
    assert events() == []


# --- roles ---------------------------------------------------------------------------------------------------------------------

def test_adding_a_role_writes_what_it_was_copied_from_and_what_it_sees(ctx, role, kim):
    lead = services.create_role(name="Vendor lead", copy_from=role("vendor"), scope=DataScope.COMPANY, by=kim)
    e = only(Action.ROLE_CREATED)
    assert (e.by, e.user, e.role, e.detail) == (kim, None, lead, "Copied from Vendor technician; sees only work orders assigned to their company")
    with pytest.raises(ValidationError):
        services.create_role(name="  ", copy_from=role("technician"), by=kim)
    assert len(events()) == 1


def test_a_level_change_writes_the_module_and_both_levels(ctx, role, kim, make_user):
    services.set_role_level(role("technician"), "contracts", Level.EDIT, by=kim)
    e = only(Action.ROLE_LEVEL_CHANGED)
    assert (e.by, e.user, e.role, e.detail) == (kim, None, role("technician"), "Contracts: View → Edit")
    services.set_role_level(role("technician"), "contracts", Level.EDIT, by=kim)  # the level it has: no change
    manager = make_user("manager")
    for call in (lambda: services.set_role_level(role("director"), "contracts", Level.VIEW, by=kim),  # fixed
                 lambda: services.set_role_level(role("technician"), "nope", Level.VIEW, by=kim),
                 lambda: services.set_role_level(role("manager"), "pm", Level.VIEW, by=manager),  # their own role
                 lambda: services.set_role_level(role("technician"), "settings", Level.FULL, by=manager)):  # more than they have
        with pytest.raises(ValidationError):
            call()
    assert len(events()) == 1


def test_what_a_role_sees_writes_an_event_only_when_it_changes(ctx, role, kim):
    lead = services.create_role(name="Biomed lead", copy_from=role("technician"), by=kim)
    services.set_role_scope(lead, DataScope.DEPARTMENT, by=kim)
    e = only(Action.ROLE_SCOPE_CHANGED)
    assert (e.by, e.role, e.detail) == (kim, lead, "The whole facility → Only their department's devices and work orders")
    services.set_role_scope(role("technician"), DataScope.FACILITY, by=kim)  # blank already meant the whole facility
    services.set_role_scope(role("vendor"), DataScope.COMPANY, by=kim)  # fixed, and already that
    with pytest.raises(ValidationError):
        services.set_role_scope(role("vendor"), DataScope.FACILITY, by=kim)
    assert len(events(action=Action.ROLE_SCOPE_CHANGED)) == 1


def test_the_words_fit_the_detail_column(ctx, role, kim, make_user):
    vendor = _with(make_user("vendor"), company="A" * 120)
    services.set_user_role(vendor, role("technician"), company="B" * 120, department="Clinical Engineering", by=kim)
    detail = only(Action.ROLE_CHANGED).detail
    assert len(detail) == AccessEvent._meta.get_field("detail").max_length and detail.endswith("…")
    assert detail.startswith("Vendor technician → Technician; company: AAAA")


# --- the screens and the API write through the services -------------------------------------------------------------------------

def test_the_users_and_roles_tabs_record_who_made_the_change(client, ctx, role, kim, make_user):
    client.force_login(kim)
    tom = make_user("technician", username="tom@riverside.example")
    assert client.post(f"/users/{tom.pk}/role/", {"role": str(role("manager").id)}, **HX).status_code == 200
    assert client.post(f"/users/roles/{role('analyst').id}/level/", {"module": "pm", "level": str(Level.EDIT)}, **HX).status_code == 200
    assert client.post(f"/users/{kim.pk}/deactivate/", **HX).status_code == 200  # refused: their own account
    got = [(e.action, e.by, e.detail) for e in events()]
    assert got == [(Action.ROLE_CHANGED, kim, "Technician → CE manager"), (Action.ROLE_LEVEL_CHANGED, kim, "PM schedule: View → Edit")]


def test_an_api_role_change_is_all_or_nothing_events_included(client, ctx, role, kim):
    client.force_login(kim)
    url = f"/api/v1/roles/{role('analyst').id}/"
    r = client.patch(url, {"levels": {"pm": Level.EDIT, "settings": 9}}, content_type="application/json")
    assert r.status_code == 400 and events() == []
    r = client.patch(url, {"levels": {"pm": Level.EDIT, "settings": Level.VIEW}}, content_type="application/json")
    assert r.status_code == 200
    assert sorted(e.detail for e in events(action=Action.ROLE_LEVEL_CHANGED)) == ["PM schedule: View → Edit", "Settings: None → View"]


# --- isolation ------------------------------------------------------------------------------------------------------------------

def test_events_stay_in_their_facility(ctx, role, kim, other_tenant, make_user):
    create_default_roles(other_tenant)
    theirs = make_user("director", tenant_=other_tenant, username="dir@other.example")
    with tenant_context(other_tenant):
        services.set_role_level(Role.objects.get(slug="technician"), "contracts", Level.EDIT, by=theirs)
        assert len(events()) == 1
    services.set_role_level(role("technician"), "reports", Level.NONE, by=kim)
    assert [e.detail for e in events()] == ["Reports: View → None"]
    assert AccessEvent.unscoped.count() == 2  # unscoped: counting both facilities' rows
    access = [e for e in history.change_log(kim)[0] if e.area == "access"]
    assert [e.changes[0].after for e in access] == ["Reports: View → None"]
    with tenant_context(None):
        assert AccessEvent.objects.count() == 0  # no facility, no rows


def test_an_event_names_the_user_after_they_are_gone(ctx, role, kim, make_user):
    tom = make_user("technician", username="tom@riverside.example")
    services.set_user_role(tom, role("manager"), by=kim)
    User.objects.filter(pk=tom.pk).delete()
    e = only(Action.ROLE_CHANGED)
    assert e.user is None and e.role == role("manager") and e.detail == "Technician → CE manager"
