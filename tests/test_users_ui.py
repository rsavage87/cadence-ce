"""Users and access (slice 5): user and role services, the Users and Roles tabs, server-side permission checks, and tenant isolation."""
from datetime import datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import Level, Module, Role, User, create_default_roles
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

def test_invite_creates_an_invited_user_and_optional_technician(ctx, role):
    u = services.invite_user(ctx, email="  Maria.Santos@Riverside.example ", first_name="Maria", last_name="Santos", role=role("requester"),
                             department="Central Sterile", create_technician=True)
    assert u.username == u.email == "maria.santos@riverside.example" and u.tenant == ctx and u.is_invited and u.is_active
    assert not u.has_usable_password() and services.user_status(u) == "invited"
    assert u.technician.name == "Maria Santos" and u.technician.title == "BMET I" and u.technician.tenant == ctx
    plain = services.invite_user(ctx, email="d@riverside.example", first_name="Devon", last_name="Park", role=role("requester"))
    assert not Technician.objects.filter(user=plain).exists()


def test_invite_rejects_duplicate_email_and_missing_fields(ctx, role, make_user):
    make_user("requester", username="taken@riverside.example")
    with pytest.raises(ValidationError, match="already exists"):
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


def test_list_users_filters_and_counts(ctx, role, make_user, other_roles):
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
    for slug in ("requester", "analyst", "technician"):
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
    assert f'hx-post="/users/{tom.pk}/role/"' in body and f'hx-post="/users/{kim.pk}/role/"' in body


def test_users_filters_and_partial(client, signed_in, make_user, role):
    signed_in("director")
    tom = make_user("technician", username="tom@riverside.example")
    services.invite_user(tom.tenant, email="maria@riverside.example", first_name="Maria", last_name="Santos",
                         role=role("requester"), department="Central Sterile")
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


def test_invite_user_modal_and_creation(client, signed_in, role, dept):
    signed_in("director")
    modal = client.get("/users/invite/", **HX)
    body = modal.content.decode()
    assert modal.status_code == 200 and "<html" not in body and "No invitation email is sent yet" in body and "Resend" not in body
    assert f'<option value="{role("requester").id}" selected>' in body and 'name="create_technician"' in body
    assert '<option value="Clinical Engineering">' in body and '<option value="ICU">' in body and '<option value="External vendor">' in body
    r = client.post("/users/invite/", {"first_name": "Maria", "last_name": "Santos", "email": "MSantos@riverside.example", "role": str(role("requester").id),
                                       "department": "Central Sterile", "create_technician": "on"}, **HX)
    assert r.status_code == 200 and r.content == b""
    assert "Account created for msantos@riverside.example" in r["HX-Trigger"] and "users-changed" in r["HX-Trigger"]
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    u = User.objects.get(username="msantos@riverside.example")
    assert u.is_invited and not u.has_usable_password() and u.department == "Central Sterile" and u.technician.name == "Maria Santos"
    dup = client.post("/users/invite/", {"first_name": "Maria", "last_name": "Santos", "email": "msantos@riverside.example", "role": str(role("requester").id)},
                      **HX)
    assert dup.status_code == 200 and "already exists" in dup.content.decode() and "HX-Trigger" not in dup
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
    assert [m["role"].slug for m in r.context["matrix"]] == ["manager", "requester", "director", "analyst", "technician", "vendor"]
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
    r = client.post("/users/roles/new/", {"name": "Sterile processing lead", "copy_from": str(role("manager").id), "description": "Runs Central Sterile"}, **HX)
    assert r.status_code == 200 and 'Role \\"Sterile processing lead\\" created' in r["HX-Trigger"] and "roles-changed" in r["HX-Trigger"]
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    new = Role.objects.get(slug="sterile-processing-lead")
    assert new.level_for(Module.WORKORDERS) == Level.APPROVE and new.description == "Runs Central Sterile"
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
    seen.last_login = timezone.make_aware(datetime(2026, 9, 22, 8, 12))
    seen.save(update_fields=["last_login"])
    body = client.get("/users/", **hx("users-body")).content.decode()
    assert '<td class="muted">—</td>' in body.split(f'id="user-{never.pk}"')[1].split("</tr>")[0]
    assert '<td class="muted">Sep 22, 8:12 AM</td>' in body.split(f'id="user-{seen.pk}"')[1].split("</tr>")[0]
