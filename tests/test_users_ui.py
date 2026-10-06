"""Users and access (slice 5): user and role services, the Users and Roles tabs, server-side permission checks, and tenant isolation."""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import AccessEvent, Level, Module, Role, User, create_default_roles
from apps.accounts.services import UserFilters
from apps.credentials.models import Credential, Scope, Technician

HX = {"HTTP_HX_REQUEST": "true"}


def hx(target):
    return {**HX, "HTTP_HX_TARGET": target}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **kw):
        user = make_user(role_slug, **kw)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def role(tenant):
    def _get(slug, tenant_=None):
        return Role.unscoped.get(tenant=tenant_ or tenant, slug=slug)  # unscoped: fixtures run outside a request context

    return _get


@pytest.fixture
def other_roles(other_tenant):
    create_default_roles(other_tenant)
    return other_tenant


# --- services -----------------------------------------------------------------------------------

def test_invite_creates_an_invited_user_and_optional_technician(ctx, role, dept):
    u = services.invite_user(ctx, email="  Maria.Santos@Riverside.example ", first_name="Maria", last_name="Santos", role=role("requester"),
                             department="ICU", create_technician=True)
    assert u.username == u.email == "maria.santos@riverside.example" and u.tenant == ctx and u.is_invited and u.is_active
    assert not u.has_usable_password() and services.user_status(u) == "invited"
    assert u.technician.name == "Maria Santos" and u.technician.title == "BMET I" and u.technician.tenant == ctx
    plain = services.invite_user(ctx, email="d@riverside.example", first_name="Devon", last_name="Park", role=role("requester"), department="ICU")
    assert not Technician.objects.filter(user=plain).exists()


def test_invite_links_the_technician_the_import_added_instead_of_a_second_one(ctx, role, dept, client, signed_in):
    imported = Technician.objects.create(name="Whitfield, Dana", title="Lead BMET", is_active=False)  # no account: the technicians import's
    taken = Technician.objects.create(name="Tom Okafor", user=services.invite_user(ctx, email="t1@riverside.example", first_name="T",
                                                                                   last_name="O", role=role("technician"), department="ICU"))
    dana = services.invite_user(ctx, email="dana@riverside.example", first_name="Dana", last_name="whitfield", role=role("technician"),
                                department="ICU", create_technician=True)
    imported.refresh_from_db()
    assert (imported.user, imported.is_active, imported.title, imported.name) == (dana, True, "Lead BMET", "Whitfield, Dana")  # its own
    assert Technician.objects.count() == 2
    assert AccessEvent.objects.filter(user=dana).get().detail == "Invited as Technician; department: ICU; technician profile linked"
    tom = services.invite_user(ctx, email="tom@riverside.example", first_name="Tom", last_name="Okafor", role=role("technician"),
                               department="ICU", create_technician=True)  # the Tom Okafor here has an account: another person
    assert tom.technician != taken and tom.technician.name == "Tom Okafor" and Technician.objects.count() == 3
    Technician.objects.create(name="Lee Park")
    Technician.objects.create(name="Park, Lee")
    with pytest.raises(ValidationError) as e:
        services.invite_user(ctx, email="lee@riverside.example", first_name="Lee", last_name="Park", role=role("technician"), department="ICU",
                             create_technician=True)
    assert e.value.messages == ["Two technicians here without an account are named Lee Park, so which one this person is cannot be told. "
                                "Invite them without a technician profile."]
    assert not User.objects.filter(email="lee@riverside.example").exists()  # nothing written
    assert not Technician.objects.filter(name__in=["Lee Park", "Park, Lee"], user__isnull=False).exists()
    signed_in("director")
    r = client.post("/users/invite/", {"first_name": "Lee", "last_name": "Park", "email": "lee@riverside.example", "role": str(role("technician").id),
                                       "department": "ICU", "create_technician": "on"}, **HX)
    assert r.status_code == 200 and "Two technicians here without an account are named Lee Park" in r.content.decode() and "HX-Trigger" not in r


def test_invite_rejects_duplicate_email_and_missing_fields(ctx, role, make_user):
    make_user("requester", username="taken@riverside.example")
    with pytest.raises(ValidationError, match="already a member"):
        services.invite_user(ctx, email="TAKEN@riverside.example", first_name="A", last_name="B", role=role("requester"))
    with pytest.raises(ValidationError, match="email"):
        services.invite_user(ctx, email="", first_name="A", last_name="B", role=role("requester"))
    with pytest.raises(ValidationError, match="name"):
        services.invite_user(ctx, email="x@riverside.example", first_name="", last_name="B", role=role("requester"))


def test_invite_and_role_change_reject_a_role_from_another_tenant(ctx, role, other_roles, make_user):
    theirs = role("requester", tenant_=other_roles)
    with pytest.raises(ValidationError, match="this facility"):
        services.invite_user(ctx, email="x@riverside.example", first_name="A", last_name="B", role=theirs)
    user = make_user("technician")
    with pytest.raises(ValidationError, match="this facility"):
        services.set_user_role(user, theirs)
    with pytest.raises(ValidationError):
        services.set_user_role(user, None)
    services.set_user_role(user, role("manager"))
    user.refresh_from_db()
    assert user.role.slug == "manager"


def test_users_cannot_change_their_own_role_or_deactivate_themselves(ctx, role, make_user):
    me = make_user("director")
    with pytest.raises(ValidationError, match="own role"):
        services.set_user_role(me, role("manager"), by=me)
    with pytest.raises(ValidationError, match="own account"):
        services.deactivate_user(me, by=me)
    root = User.objects.create_superuser("root", "root@example.com", "Test-Pass-2026-x", tenant=ctx)
    with pytest.raises(ValidationError, match="Admin"):
        services.deactivate_user(root, by=me)
    other = make_user("technician")
    services.deactivate_user(other, by=me)
    other.refresh_from_db()
    assert not other.is_active and services.user_status(other) == "deactivated"
    services.reactivate_user(other, by=me)
    other.refresh_from_db()
    assert other.is_active and services.user_status(other) == "active"


def test_set_role_level_rules(ctx, role):
    director, manager = role("director"), role("manager")
    with pytest.raises(ValidationError, match="fixed"):
        services.set_role_level(director, Module.CONTRACTS, Level.NONE)
    assert director.level_for(Module.CONTRACTS) == Level.FULL
    with pytest.raises(ValidationError, match="module"):
        services.set_role_level(manager, "billing", Level.EDIT)
    with pytest.raises(ValidationError, match="level"):
        services.set_role_level(manager, Module.CONTRACTS, 9)
    services.set_role_level(manager, Module.CONTRACTS, Level.FULL)
    assert manager.level_for(Module.CONTRACTS) == Level.FULL


def test_create_role_copies_levels_and_keeps_slugs_unique(ctx, role):
    manager = role("manager")
    r = services.create_role(name="Sterile processing lead", description="", copy_from=manager)
    assert r.slug == "sterile-processing-lead" and r.tenant == ctx and r.description == "Copied from CE manager" and not r.is_system
    assert {m: r.level_for(m) for m in Module.values} == {m: manager.level_for(m) for m in Module.values}
    again = services.create_role(name="Sterile processing lead", description="Second", copy_from=manager)
    third = services.create_role(name="Sterile Processing Lead!", description="", copy_from=manager)
    assert (again.slug, third.slug) == ("sterile-processing-lead-2", "sterile-processing-lead-3")
    long = services.create_role(name="x" * 60, description="", copy_from=manager)
    assert len(long.slug) == 40 and len(services.create_role(name="x" * 60, description="", copy_from=manager).slug) <= 40
    with pytest.raises(ValidationError, match="name"):
        services.create_role(name="  ", description="", copy_from=manager)
    # the new role is usable as a real role
    services.set_role_level(r, Module.CONTRACTS, Level.NONE)
    assert r.level_for(Module.CONTRACTS) == Level.NONE


def test_list_users_filters_and_counts(ctx, role, make_user, other_roles, dept):
    kim = make_user("director", username="kim@riverside.example")
    kim.first_name, kim.last_name, kim.department = "Kim", "Alvarez", "Clinical Engineering"
    kim.save()
    invited = services.invite_user(ctx, email="maria@riverside.example", first_name="Maria", last_name="Santos", role=role("requester"), department="ICU")
    gone = make_user("technician")
    services.deactivate_user(gone)
    make_user("director", tenant_=other_roles)
    everyone = services.list_users(ctx, UserFilters())
    assert list(everyone) == [kim, invited, gone]  # by name; the other tenant's director is not here
    assert list(services.list_users(ctx, UserFilters(q="clinical"))) == [kim]
    assert list(services.list_users(ctx, UserFilters(q="maria@"))) == [invited]
    assert list(services.list_users(ctx, UserFilters(role="technician"))) == [gone]
    assert list(services.list_users(ctx, UserFilters(status="invited"))) == [invited]
    assert list(services.list_users(ctx, UserFilters(status="active"))) == [kim]
    assert list(services.list_users(ctx, UserFilters(status="deactivated"))) == [gone]
    counts = services.role_user_counts(ctx)
    assert counts == {role("director").id: 1, role("requester").id: 1}  # deactivated users do not count


# --- Users tab -------------------------------------------------------------------------------------

def test_roles_without_users_access_get_403(client, signed_in):
    for slug in ("requester", "analyst", "technician", "vendor"):
        signed_in(slug)
        assert client.get("/users/").status_code == 403, slug
        assert client.get("/users/roles/").status_code == 403, slug


def test_manager_sees_both_tabs_read_only(client, signed_in, make_user, role):
    signed_in("manager")
    tom = make_user("technician")
    page = client.get("/users/")
    body = page.content.decode()
    assert page.status_code == 200 and "Roles and permissions" in body and 'aria-current="page"' in body and "<html" in body
    assert 'class="perm"' not in body and "Invite user</button>" not in body and ">Deactivate<" not in body and "Technician" in body
    assert "Change a role from the dropdown" in body
    matrix = client.get("/users/roles/")
    mbody = matrix.content.decode()
    assert matrix.status_code == 200 and 'class="perm"' not in mbody and "Add role</button>" not in mbody and "The Director role is fixed" in mbody
    assert client.post(f"/users/{tom.pk}/role/", {"role": str(role("manager").id)}).status_code == 403
    assert client.post(f"/users/{tom.pk}/deactivate/").status_code == 403
    assert client.post(f"/users/{tom.pk}/reactivate/").status_code == 403
    assert client.get("/users/invite/", **HX).status_code == 403
    invite = {"first_name": "A", "last_name": "B", "email": "ab@riverside.example", "role": str(role("requester").id)}
    assert client.post("/users/invite/", invite).status_code == 403
    assert client.post(f"/users/roles/{role('manager').id}/level/", {"module": "contracts", "level": 3}).status_code == 403
    assert client.get("/users/roles/new/", **HX).status_code == 403
    assert client.post("/users/roles/new/", {"name": "X", "copy_from": str(role("requester").id)}).status_code == 403


def test_users_tab_lists_roles_status_and_credentials(client, signed_in, make_user, role, techs):
    kim = signed_in("director", username="kim@riverside.example")
    kim.first_name, kim.last_name, kim.department = "Kim", "Alvarez", "Clinical Engineering"
    kim.save()
    tom = make_user("technician", username="tokafor@riverside.example")
    tom.first_name, tom.last_name = "Tom", "Okafor"
    tom.save()
    techs["tom"].user = tom
    techs["tom"].save()
    Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Ventilators")
    Credential.objects.create(technician=techs["tom"], scope=Scope.MODEL, value="Old", status=Credential.Status.IN_TRAINING)
    r = client.get("/users/")
    body = r.content.decode()
    assert r.status_code == 200 and "Kim Alvarez" in body and "Tom Okafor" in body
    assert f'href="/users/credentials/?technician={techs["tom"].id}">2 credentials</a> <span class="chip warn">1 expiring</span>' in body
    assert body.count('class="perm"') == 2 and body.count(">Deactivate<") == 1  # no Deactivate on the signed-in user's own row
    assert '<span class="chip ok">Active</span>' in body and "Change a role from the dropdown" in body
    assert f'hx-post="/users/{tom.pk}/role/"' in body and f'hx-post="/users/{kim.pk}/role/"' not in body  # your own role is fixed


def test_users_filters_and_partial(client, signed_in, make_user, role):
    signed_in("director")
    tom = make_user("technician", username="tom@riverside.example")
    services.invite_user(tom.tenant, email="maria@riverside.example", first_name="Maria", last_name="Santos",
                         role=role("analyst"), department="Central Sterile")
    services.deactivate_user(tom)
    partial = client.get("/users/?status=invited", **hx("users-body"))
    body = partial.content.decode()
    assert partial.status_code == 200 and "<html" not in body and "Maria Santos" in body and "Tom" not in body
    assert '<span class="chip info">Invited</span>' in body and "hx-trigger=\"users-changed from:body\"" in body
    assert "Reactivate" in client.get("/users/?status=deactivated", **hx("users-body")).content.decode()
    assert [row["u"].pk for row in client.get("/users/?role=technician").context["rows"]] == [tom.pk]
    assert [row["u"].first_name for row in client.get("/users/?q=sterile").context["rows"]] == ["Maria"]
    assert len(client.get("/users/?role=nope&status=bogus").context["rows"]) == 3
    assert "No users match." in client.get("/users/?q=zzz", **hx("users-body")).content.decode()


def test_director_changes_a_users_role(client, signed_in, make_user, role):
    signed_in("director")
    kim = make_user("technician", username="kim@riverside.example")
    kim.first_name, kim.last_name = "Kim", "Alvarez"
    kim.save()
    r = client.post(f"/users/{kim.pk}/role/", {"role": str(role("manager").id)}, **HX)
    kim.refresh_from_db()
    assert r.status_code == 200 and kim.role.slug == "manager"
    assert "Kim Alvarez: role set to CE manager" in r["HX-Trigger"]
    body = r.content.decode()
    # Row actions return the whole body (so filters apply) plus the summary line out of band.
    assert 'id="users-body"' in body and f'<option value="{role("manager").id}" selected>' in body and 'id="users-summary" hx-swap-oob="true"' in body
    bad = client.post(f"/users/{kim.pk}/role/", {"role": "not-a-uuid"}, **HX)
    kim.refresh_from_db()
    assert bad.status_code == 200 and kim.role.slug == "manager" and "Choose a role" in bad["HX-Trigger"]


def test_row_actions_keep_the_list_filters(client, signed_in, make_user):
    signed_in("director")
    tom = make_user("technician", username="tom@riverside.example")
    r = client.post(f"/users/{tom.pk}/deactivate/?status=active", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "tom@riverside.example" not in body  # dropped out of the "Active" list at once
    assert 'hx-get="/users/?status=active"' in body  # the body keeps re-fetching with the same filters


def test_director_cannot_change_own_role_or_deactivate_self(client, signed_in, role):
    me = signed_in("director")
    r = client.post(f"/users/{me.pk}/role/", {"role": str(role("manager").id)}, **HX)
    me.refresh_from_db()
    assert r.status_code == 200 and me.role.slug == "director" and "own role" in r["HX-Trigger"]
    r = client.post(f"/users/{me.pk}/deactivate/", **HX)
    me.refresh_from_db()
    assert r.status_code == 200 and me.is_active and "own account" in r["HX-Trigger"]


def test_deactivate_and_reactivate_another_user(client, signed_in, make_user):
    signed_in("director")
    tom = make_user("technician", username="tom@riverside.example")
    tom.first_name, tom.last_name = "Tom", "Okafor"
    tom.save()
    r = client.post(f"/users/{tom.pk}/deactivate/", **HX)
    tom.refresh_from_db()
    body = r.content.decode()
    assert r.status_code == 200 and not tom.is_active and "Tom Okafor deactivated" in r["HX-Trigger"]
    assert '<span class="chip neutral">Deactivated</span>' in body and "Reactivate" in body
    assert 'class="perm" name="role" aria-label="Role for Tom Okafor" disabled' in body
    r = client.post(f"/users/{tom.pk}/reactivate/", **HX)
    tom.refresh_from_db()
    assert r.status_code == 200 and tom.is_active and "Tom Okafor reactivated" in r["HX-Trigger"] and "Deactivate" in r.content.decode()
    assert client.get(f"/users/{tom.pk}/deactivate/").status_code == 405


def test_invite_user_modal_and_creation(client, signed_in, role, dept, mailoutbox):
    signed_in("director")
    modal = client.get("/users/invite/", **HX)
    body = modal.content.decode()
    assert modal.status_code == 200 and "<html" not in body and "Resend" not in body
    assert "They get an email with a link to set their password. The link works for 7 days." in body and "Send invitation</button>" in body
    assert f'<option value="{role("requester").id}" selected>' in body and 'name="create_technician"' in body
    # slice 16: the clinical requester (the default role) is department-scoped, so its unit is one of the facility's departments
    assert '<option value="" selected>Choose their unit</option>' in body and '<option value="ICU">' in body and "External vendor" not in body
    r = client.post("/users/invite/", {"first_name": "Maria", "last_name": "Santos", "email": "MSantos@riverside.example", "role": str(role("requester").id),
                                       "department": "ICU", "create_technician": "on"}, **HX)
    assert r.status_code == 200 and r.content == b""
    assert "Invitation sent to msantos@riverside.example" in r["HX-Trigger"] and "users-changed" in r["HX-Trigger"]
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    assert [m.to for m in mailoutbox] == [["msantos@riverside.example"]]
    u = User.objects.get(username="msantos@riverside.example")
    assert u.is_invited and not u.has_usable_password() and u.department == "ICU" and u.technician.name == "Maria Santos"
    dup = client.post("/users/invite/", {"first_name": "Maria", "last_name": "Santos", "email": "msantos@riverside.example", "role": str(role("requester").id)},
                      **HX)
    assert dup.status_code == 200 and "already a member" in dup.content.decode() and "HX-Trigger" not in dup
    missing = client.post("/users/invite/", {"first_name": "", "last_name": "", "email": "nope", "role": str(role("requester").id)}, **HX)
    assert missing.status_code == 200 and "required" in missing.content.decode() and User.objects.filter(email="nope").count() == 0


def test_other_tenants_users_are_not_listed_and_404_on_post(client, tenant, other_roles, make_user, role):
    ours = make_user("technician")
    theirs = make_user("director", tenant_=other_roles)
    client.force_login(theirs)
    page = client.get("/users/")
    assert page.status_code == 200 and [row["u"].pk for row in page.context["rows"]] == [theirs.pk]
    assert client.post(f"/users/{ours.pk}/deactivate/", **HX).status_code == 404
    assert client.post(f"/users/{ours.pk}/reactivate/", **HX).status_code == 404
    assert client.post(f"/users/{ours.pk}/role/", {"role": str(role("manager", tenant_=other_roles).id)}, **HX).status_code == 404
    assert client.post(f"/users/roles/{role('manager').id}/level/", {"module": "contracts", "level": 3}, **HX).status_code == 404
    # our role ids are not valid choices for their users either
    r = client.post(f"/users/{theirs.pk}/role/", {"role": str(role("manager").id)}, **HX)
    assert r.status_code == 200 and "Choose a role" in r["HX-Trigger"]
    ours.refresh_from_db()
    assert ours.is_active and ours.role.slug == "technician"
    roles_page = client.get("/users/roles/")
    assert roles_page.status_code == 200 and all(m["role"].tenant_id == other_roles.id for m in roles_page.context["matrix"])


# --- Roles tab -------------------------------------------------------------------------------------

def test_matrix_shows_every_role_and_disables_director(client, signed_in, make_user, role):
    signed_in("director")
    make_user("technician")
    r = client.get("/users/roles/")
    body = r.content.decode()
    assert r.status_code == 200 and 'aria-current="page">Roles and permissions' in body and "Add role" in body
    assert [m["role"].slug for m in r.context["matrix"]] == ["director", "manager", "technician", "requester", "analyst", "vendor"]  # the mock's order
    assert 'aria-label="Director, equipment" disabled' in body and f'hx-post="/users/roles/{role("manager").id}/level/"' in body
    assert body.count('class="perm"') == 6 * 8
    director_row = next(m for m in r.context["matrix"] if m["role"].slug == "director")
    assert director_row["users"] == 1 and all(level == Level.FULL for _, level in director_row["cells"])
    partial = client.get("/users/roles/", **hx("roles-matrix"))
    assert partial.status_code == 200 and "<html" not in partial.content.decode() and 'hx-trigger="roles-changed from:body"' in partial.content.decode()


def test_director_changes_a_matrix_cell(client, ctx, signed_in, role):
    signed_in("director")
    manager = role("manager")
    r = client.post(f"/users/roles/{manager.id}/level/", {"module": "contracts", "level": "3"}, **HX)
    assert r.status_code == 200 and "CE manager: Contracts set to Edit" in r["HX-Trigger"] and 'id="roles-matrix"' in r.content.decode()
    client.post(f"/users/roles/{manager.id}/level/", {"module": "contracts", "level": "5"}, **HX)
    assert manager.level_for(Module.CONTRACTS) == Level.FULL
    fixed = client.post(f"/users/roles/{role('director').id}/level/", {"module": "contracts", "level": "0"}, **HX)
    assert fixed.status_code == 200 and "Director role is fixed" in fixed["HX-Trigger"] and role("director").level_for(Module.CONTRACTS) == Level.FULL
    garbage = client.post(f"/users/roles/{manager.id}/level/", {"module": "contracts", "level": "lots"}, **HX)
    assert garbage.status_code == 200 and "Unknown permission level" in garbage["HX-Trigger"] and manager.level_for(Module.CONTRACTS) == Level.FULL
    assert client.get(f"/users/roles/{manager.id}/level/?module=contracts&level=1").status_code == 405


def test_add_role_modal_and_creation(client, ctx, signed_in, role):
    signed_in("director")
    modal = client.get("/users/roles/new/", **HX)
    body = modal.content.decode()
    assert modal.status_code == 200 and "<html" not in body and "Adjust the module levels in the matrix" in body
    assert f'<option value="{role("requester").id}" selected>' in body
    r = client.post("/users/roles/new/", {"name": "Sterile processing lead", "copy_from": str(role("manager").id), "description": "Runs ICU"}, **HX)
    assert r.status_code == 200 and 'Role \\"Sterile processing lead\\" created' in r["HX-Trigger"] and "roles-changed" in r["HX-Trigger"]
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    new = Role.objects.get(slug="sterile-processing-lead")
    assert new.level_for(Module.WORKORDERS) == Level.APPROVE and new.description == "Runs ICU"
    assert "Sterile processing lead" in client.get("/users/roles/").content.decode()
    blank = client.post("/users/roles/new/", {"name": "   ", "copy_from": str(role("manager").id)}, **HX)
    assert blank.status_code == 200 and "required" in blank.content.decode() and Role.objects.count() == 7


def test_tenant_summary_in_page_head(client, signed_in, make_user, techs):
    signed_in("director")
    gone = make_user("technician")
    services.deactivate_user(gone)
    r = client.get("/users/")
    assert r.context["users_summary"] == {"active_users": 1, "roles": 6, "technicians": 2}
    assert "1 active user · 6 roles · 2 technicians with credential profiles" in r.content.decode()


def test_last_active_column(client, signed_in, make_user):
    signed_in("director")
    never = make_user("technician")
    seen = make_user("manager")
    seen.last_login = timezone.now() - timedelta(days=1)
    seen.save(update_fields=["last_login"])
    body = client.get("/users/", **hx("users-body")).content.decode()
    assert '<td class="muted">—</td>' in body.split(f'id="user-{never.pk}"')[1].split("</tr>")[0]
    assert '<td class="muted">Yesterday</td>' in body.split(f'id="user-{seen.pk}"')[1].split("</tr>")[0]


# --- review follow-ups ---------------------------------------------------------------------------

def test_invite_does_not_reveal_accounts_at_other_tenants(ctx, role, make_user, other_roles):
    from apps.accounts import services

    make_user("requester", tenant_=other_roles, username="shared@vendor.example")  # username is global; this address lives elsewhere
    with pytest.raises(ValidationError) as e:
        services.invite_user(ctx, email="shared@vendor.example", first_name="S", last_name="V", role=role("technician"))
    assert "cannot be used" in str(e.value) and "member" not in str(e.value)
    other = make_user("requester", tenant_=other_roles, username="fse-b")
    other.email = "fse@vendor.example"
    other.save()
    invited = services.invite_user(ctx, email="fse@vendor.example", first_name="F", last_name="S", role=role("technician"))
    assert invited.tenant_id == ctx.id  # an email-only match at another tenant is not this tenant's business


def test_invite_and_new_role_reject_another_tenants_role_at_the_view(client, signed_in, role, other_roles):
    signed_in("director")
    foreign = role("requester", tenant_=other_roles)
    r = client.post("/users/invite/", {"first_name": "A", "last_name": "B", "email": "ab@riverside.example", "role": str(foreign.id),
                                       "department": "Clinical Engineering"}, **HX)
    assert r.status_code == 200 and "Choose a role" in r.content.decode() and "HX-Trigger" not in r
    assert not User.objects.filter(email="ab@riverside.example").exists()
    r = client.post("/users/roles/new/", {"name": "Copycat", "copy_from": str(role("manager", tenant_=other_roles).id)}, **HX)
    assert r.status_code == 200 and "Choose a role" in r.content.decode()
    assert not Role.unscoped.filter(name="Copycat").exists()  # unscoped: proves nothing was created in any tenant


def test_superusers_are_managed_in_admin(client, ctx, signed_in, django_user_model):
    signed_in("director")
    root = django_user_model.objects.create_superuser("root@riverside.example", "root@riverside.example", "Test-Pass-2026-x", tenant=ctx)
    row = client.get("/users/").content.decode().split("root@riverside.example")[1].split("</tr>")[0]
    assert "Deactivate" not in row
    r = client.post(f"/users/{root.pk}/deactivate/", **HX)
    root.refresh_from_db()
    assert r.status_code == 200 and root.is_active and "managed in Admin" in r["HX-Trigger"]
    root.is_active = False
    root.save()
    r = client.post(f"/users/{root.pk}/reactivate/", **HX)
    root.refresh_from_db()
    assert r.status_code == 200 and not root.is_active and "managed in Admin" in r["HX-Trigger"]


def test_matrix_rejects_unknown_module_at_the_view(client, ctx, signed_in, role):
    signed_in("director")
    r = client.post(f"/users/roles/{role('manager').id}/level/", {"module": "bogus", "level": "3"}, **HX)
    assert r.status_code == 200 and "Unknown module" in r["HX-Trigger"]


def test_users_cannot_raise_their_own_roles_levels(client, ctx, signed_in, role):
    from apps.accounts import services

    me = signed_in("director")
    custom = services.create_role(name="Admin clerk", copy_from=role("manager"))
    services.set_role_level(custom, "users", Level.FULL)
    me.role = custom
    me.save()
    r = client.post(f"/users/roles/{custom.id}/level/", {"module": "contracts", "level": str(Level.FULL)}, **HX)
    assert r.status_code == 200 and "your own role" in r["HX-Trigger"] and custom.level_for("contracts") == Level.EDIT


def test_own_row_role_select_is_disabled(client, signed_in):
    me = signed_in("director")
    row = client.get("/users/").content.decode().split(f'id="user-{me.pk}"')[1].split("</tr>")[0]
    assert 'name="role" aria-label="Role for Director User" disabled title="You cannot change your own role"' in row


def test_summary_counts_only_signed_in_active_users(client, ctx, signed_in, role):
    from apps.accounts import services

    signed_in("director")
    services.invite_user(ctx, email="new@riverside.example", first_name="N", last_name="U", role=role("technician"))
    assert "1 active user " in client.get("/users/").content.decode()


def test_matrix_uses_the_mocks_module_and_role_order(client, signed_in):
    signed_in("director")
    page = client.get("/users/roles/").content.decode()
    assert page.index("<th>Recalls</th>") < page.index("<th>Contracts</th>")
    positions = [page.index(f">{n}</b>") for n in ("Director", "CE manager", "Technician", "Clinical requester", "Finance and quality", "Vendor technician")]
    assert positions == sorted(positions)


def test_roles_tab_refreshes_the_summary_line_out_of_band(client, ctx, signed_in, role):
    signed_in("director")
    r = client.post(f"/users/roles/{role('manager').id}/level/", {"module": "contracts", "level": str(Level.EDIT)}, **HX)
    assert 'id="users-summary" hx-swap-oob="true"' in r.content.decode()


def test_last_active_wording():
    from django.utils import timezone

    from apps.web.templatetags.users_tags import last_active

    now = timezone.now()
    assert last_active(None) == "—"
    assert last_active(now).startswith("Today, ")
    assert last_active(now - timedelta(days=1)) == "Yesterday"
    assert last_active(now - timedelta(days=400)).endswith(str((now - timedelta(days=400)).year))


# --- invitations (slice 10) ------------------------------------------------------------------------

def _row(body, user):
    return body.split(f'id="user-{user.pk}"')[1].split("</tr>")[0]


def test_resend_invite_shows_only_for_pending_invitations(client, ctx, signed_in, role, make_user):
    signed_in("director")
    pending = services.invite_user(ctx, email="p@riverside.example", first_name="Pat", last_name="Pending", role=role("technician"))
    set_in_admin = services.invite_user(ctx, email="a@riverside.example", first_name="Ada", last_name="Admin", role=role("technician"))
    set_in_admin.set_password("Test-Pass-2026-x")  # shown as Invited until the first sign-in, but no link can set a password now
    set_in_admin.save()
    withdrawn = services.invite_user(ctx, email="w@riverside.example", first_name="Wes", last_name="Withdrawn", role=role("technician"))
    services.deactivate_user(withdrawn)
    member = make_user("technician")
    body = client.get("/users/?status=invited", **hx("users-body")).content.decode()
    row = _row(body, pending)
    assert f'hx-post="/users/{pending.pk}/resend-invite/?status=invited" hx-target="#users-body" hx-swap="outerHTML">Resend invite</button>' in row
    assert row.index("Resend invite") < row.index(">Deactivate<") and "Their invitation link stops working." in row  # Deactivate withdraws it
    assert '<span class="chip info">Invited</span>' in _row(body, set_in_admin) and "Resend invite" not in _row(body, set_in_admin)
    everyone = client.get("/users/", **hx("users-body")).content.decode()
    assert "Resend invite" not in _row(everyone, withdrawn) and "Reactivate" in _row(everyone, withdrawn)
    assert "Resend invite" not in _row(everyone, member) and ">Deactivate<" in _row(everyone, member)
    assert everyone.count("Resend invite") == 1


def test_resend_invite_is_hidden_from_users_view(client, ctx, signed_in, role):
    signed_in("manager")
    pending = services.invite_user(ctx, email="p@riverside.example", first_name="Pat", last_name="Pending", role=role("technician"))
    body = client.get("/users/").content.decode()
    assert "Pat Pending" in body and "Resend invite" not in body
    assert client.post(f"/users/{pending.pk}/resend-invite/", **HX).status_code == 403
