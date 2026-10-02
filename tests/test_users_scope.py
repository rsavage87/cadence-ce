"""Slice 16 on the Users and Roles screens: the company or department a scoped role's user needs (invite, role change, Edit), the
Users list saying what each scoped user sees by, each role's scope in the matrix (custom roles only), server-side permissions on
every Users and Roles view, tenant isolation, and the demo seed."""
import logging
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from test_rls_paths import rls  # noqa: F401

from apps.accounts import services
from apps.accounts.models import DataScope, Role, User, create_default_roles
from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import scoping
from apps.workorders.models import OPEN_STATUSES, WorkOrder
from apps.workorders.services import create_work_order

HX = {"HTTP_HX_REQUEST": "true"}


def hx(target):
    return {**HX, "HTTP_HX_TARGET": target}


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
def units(ctx, dept):
    """ICU (the dept fixture) and ED, the facility's departments."""
    return {"ICU": dept, "ED": Department.objects.create(name="ED")}


def _with(user, **fields):
    for k, v in fields.items():
        setattr(user, k, v)
    user.save(update_fields=list(fields))
    return user


# --- services: inviting --------------------------------------------------------------------------------------------------------

def test_invite_needs_the_company_a_company_scoped_role_works_for(ctx, role):
    with pytest.raises(ValidationError) as e:
        services.invite_user(ctx, email="fse@vendor.example", first_name="Fran", last_name="Ek", role=role("vendor"))
    assert e.value.message_dict == {"company": ["Enter the company this user works for: the Vendor technician role sees only the work orders assigned to it."]}
    with pytest.raises(ValidationError, match="120 characters"):
        services.invite_user(ctx, email="fse@vendor.example", first_name="Fran", last_name="Ek", role=role("vendor"), company="x" * 121)
    assert not User.objects.filter(email="fse@vendor.example").exists()
    u = services.invite_user(ctx, email="fse@vendor.example", first_name="Fran", last_name="Ek", role=role("vendor"), company="  Philips   Field  Service ",
                             department="External vendor")
    # Trimmed at the ends only: scoping matches the name exactly (any case), so inner spacing must stay as the work orders spell it.
    assert (u.company, u.department) == ("Philips   Field  Service", "External vendor")


def test_invite_needs_one_of_the_facilitys_departments_for_a_department_scoped_role(ctx, role, units):
    def invite(department, email="nurse@riverside.example"):
        return services.invite_user(ctx, email=email, first_name="Ana", last_name="Ruiz", role=role("requester"), department=department)

    with pytest.raises(ValidationError) as e:
        invite("")
    assert list(e.value.message_dict) == ["department"] and "Choose the unit this user works in" in e.value.messages[0]
    for standing in ("Clinical Engineering", "External vendor", "Finance", "Cardiology"):
        with pytest.raises(ValidationError) as e:
            invite(standing)
        assert e.value.message_dict["department"] == [f"{standing} is not one of this facility's departments. The Clinical requester role sees only "
                                                      "its own department's devices and work orders: choose the unit this user works in."]
    assert not User.objects.filter(email="nurse@riverside.example").exists()
    assert invite(" icu ").department == "ICU"  # stored as the facility names it
    # a facility-wide role keeps any department from the list, as before
    tech = services.invite_user(ctx, email="t@riverside.example", first_name="T", last_name="O", role=role("technician"), department="Clinical Engineering")
    assert tech.department == "Clinical Engineering" and tech.company == ""


def test_invite_checks_the_scope_without_a_tenant_set(tenant, role):
    """The departments are read inside the user's facility, whoever calls (a command, a test): the policy would hide them otherwise."""
    with tenant_context(tenant):
        Department.objects.create(name="Central Sterile")
    u = services.invite_user(tenant, email="cs@riverside.example", first_name="C", last_name="S", role=role("requester"), department="central sterile")
    assert u.department == "Central Sterile"


# --- services: changing a user ----------------------------------------------------------------------------------------------

def test_a_role_change_to_a_scoped_role_needs_what_it_scopes_by(ctx, role, make_user, units, caplog):
    me = make_user("director")
    tom = make_user("technician", username="tom@riverside.example")
    with pytest.raises(ValidationError) as e:
        services.set_user_role(tom, role("vendor"), by=me)
    assert list(e.value.message_dict) == ["company"]
    with pytest.raises(ValidationError) as e:
        services.set_user_role(tom, role("requester"), by=me)
    assert list(e.value.message_dict) == ["department"]
    _with(tom, department="Clinical Engineering")
    with pytest.raises(ValidationError, match="Clinical Engineering is not one of this facility's departments"):
        services.set_user_role(tom, role("requester"), by=me)
    tom.refresh_from_db()
    assert tom.role.slug == "technician"  # nothing changed
    with caplog.at_level(logging.INFO, logger="cadence.audit"):
        services.set_user_role(tom, role("vendor"), company=" GE HealthCare ", by=me)
    tom.refresh_from_db()
    assert (tom.role.slug, tom.company, tom.department) == ("vendor", "GE HealthCare", "Clinical Engineering")
    audit = [r.getMessage() for r in caplog.records if r.name == "cadence.audit"]
    assert len(audit) == 2 and all(f"by user {me.pk}" in line for line in audit)
    assert "role changed from 'technician' to 'vendor'" in audit[0] and "company changed from '' to 'GE HealthCare'" in audit[1]
    services.set_user_role(tom, role("requester"), department="ed", by=me)
    tom.refresh_from_db()
    assert (tom.role.slug, tom.department) == ("requester", "ED")
    _with(tom, department="icu")
    services.set_user_role(tom, role("technician"), by=me)  # a facility-wide role needs nothing; what the account has stays
    services.set_user_role(tom, role("requester"), by=me)  # the account's own department, matched in any case
    tom.refresh_from_db()
    assert (tom.role.slug, tom.company, tom.department) == ("requester", "GE HealthCare", "ICU")


def test_set_user_scope_follows_the_users_role(ctx, role, make_user, units, caplog):
    me = make_user("director")
    vendor = _with(make_user("vendor"), company="Philips")
    services.set_user_scope(vendor, company="Philips field service", by=me)
    vendor.refresh_from_db()
    assert vendor.company == "Philips field service"
    with pytest.raises(ValidationError) as e:
        services.set_user_scope(vendor, company="  ", by=me)
    assert list(e.value.message_dict) == ["company"]
    requester = _with(make_user("requester"), department="ICU")
    with pytest.raises(ValidationError, match="External vendor is not one of this facility's departments"):
        services.set_user_scope(requester, department="External vendor", by=me)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="cadence.audit"):
        services.set_user_scope(requester, department="ED", by=me)
        services.set_user_scope(requester, department="ED", by=me)  # no change, no line
    requester.refresh_from_db()
    assert requester.department == "ED" and [r.getMessage() for r in caplog.records if r.name == "cadence.audit"] == [
        f"user {requester.pk} (tenant {ctx.id}): department changed from 'ICU' to 'ED' by user {me.pk}"]
    tech = make_user("technician")
    services.set_user_scope(tech, department="Finance", company="", by=me)  # a facility-wide role: any department, no company needed
    tech.refresh_from_db()
    assert (tech.department, tech.company) == ("Finance", "")


def test_a_scoped_user_cannot_change_their_own_company_or_department(ctx, role, make_user, units):
    vendor = _with(make_user("vendor"), company="Philips")
    with pytest.raises(ValidationError, match="your own company or department"):
        services.set_user_scope(vendor, company="GE HealthCare", by=vendor)
    services.set_user_scope(vendor, company="Philips", by=vendor)  # no change is no change
    requester = _with(make_user("requester"), department="ICU")
    with pytest.raises(ValidationError, match="your own company or department"):
        services.set_user_scope(requester, department="ED", by=requester)
    director = make_user("director")
    services.set_user_scope(director, department="Clinical Engineering", by=director)  # sees the whole facility either way
    director.refresh_from_db()
    assert director.department == "Clinical Engineering"


def test_set_user_access_changes_the_role_and_its_company_together(ctx, role, make_user, units):
    me = make_user("director")
    tom = make_user("technician")
    services.set_user_access(tom, role=role("vendor"), company="Philips", department="External vendor", by=me)
    tom.refresh_from_db()
    assert (tom.role.slug, tom.company, tom.department) == ("vendor", "Philips", "External vendor")
    services.set_user_access(tom, role=role("vendor"), company="Zoll field service", department="External vendor", by=me)
    tom.refresh_from_db()
    assert tom.company == "Zoll field service"
    with pytest.raises(ValidationError, match="own role"):
        services.set_user_access(me, role=role("manager"), by=me)


def test_scope_gap_says_what_a_scoped_user_is_missing(ctx, make_user, units):
    assert services.scope_gap(make_user("vendor")) == "company"
    assert services.scope_gap(_with(make_user("vendor", username="v2"), company="Philips")) == ""
    assert services.scope_gap(make_user("requester")) == "department"
    renamed = _with(make_user("requester", username="r2"), department="Cardiology")
    assert services.scope_gap(renamed) == "department" and services.scope_gap(renamed, services.department_names(ctx)) == "department"
    assert services.scope_gap(_with(make_user("requester", username="r3"), department="icu"), services.department_names(ctx)) == ""
    assert services.scope_gap(make_user("technician")) == ""


def test_company_suggestions_are_the_facilitys_vendor_names(ctx, other_tenant, vent, pump, techs):
    Contract.objects.create(reference="SC-1", vendor="Hamilton Medical", type=ContractType.OEM, start_on="2026-01-01", end_on="2027-01-01", annual_cost=1)
    Contract.objects.create(reference="SC-2", vendor="TechCare Biomedical Services", type=ContractType.THIRD_PARTY, start_on="2026-01-01",
                            end_on="2027-01-01", annual_cost=1)
    create_work_order(asset=pump, type="repair", priority="normal", problem="x", vendor_service=True, vendor_name="BD field service")
    create_work_order(asset=pump, type="repair", priority="normal", problem="x", vendor_service=True, vendor_name="bd  FIELD service")
    create_work_order(asset=vent, type="repair", priority="normal", problem="x", vendor_service=True, vendor_name="Acme Imaging")
    create_work_order(asset=vent, type="repair", priority="normal", problem="x", assigned_to=techs["dana"])
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        DeviceModel.objects.create(manufacturer="Stryker", model="Power-PRO", description="Stretcher", category="Beds & stretchers")
        Contract.objects.create(reference="SC-9", vendor="Their Vendor Inc", type=ContractType.OEM, start_on="2026-01-01", end_on="2027-01-01", annual_cost=1)
    # Hamilton Medical's contract name covers "Hamilton Medical field service", so that one is not offered on its own
    # Spelled as stored: "bd  FIELD service" (two spaces) is a different name to scoping, so it is offered as it is, to be matched.
    assert services.company_suggestions(ctx) == ["Acme Imaging", "bd  FIELD service", "BD field service", "Hamilton Medical", "TechCare Biomedical Services"]


# --- services: roles --------------------------------------------------------------------------------------------------------

def test_role_scope_rules(ctx, role, make_user):
    me = make_user("director")
    with pytest.raises(ValidationError, match="Director role is fixed"):
        services.set_role_scope(role("director"), DataScope.COMPANY, by=me)
    with pytest.raises(ValidationError, match="Vendor technician role always sees only work orders assigned to their company"):
        services.set_role_scope(role("vendor"), DataScope.FACILITY, by=me)
    with pytest.raises(ValidationError, match="Clinical requester role always sees only their department's devices and work orders"):
        services.set_role_scope(role("requester"), DataScope.FACILITY, by=me)
    with pytest.raises(ValidationError, match="always sees"):
        services.set_role_scope(role("requester"), DataScope.COMPANY, by=me)
    before = role("vendor").history.count()
    services.set_role_scope(role("vendor"), DataScope.COMPANY, by=me)  # what it always is: fine, and never written
    assert role("vendor").history.count() == before and role("vendor").scope == ""
    with pytest.raises(ValidationError, match="Choose who the role sees"):
        services.set_role_scope(role("manager"), "everyone", by=me)
    # the other default roles and custom roles can be narrowed, and widened again; audited
    services.set_role_scope(role("technician"), DataScope.DEPARTMENT, by=me)
    tech = role("technician")
    assert tech.effective_scope == DataScope.DEPARTMENT and tech.history.first().history_user == me and tech.history.first().scope == "department"
    services.set_role_scope(tech, DataScope.FACILITY, by=me)
    assert role("technician").effective_scope == DataScope.FACILITY


def test_no_one_changes_what_their_own_role_sees(ctx, role, make_user):
    custom = services.create_role(name="Admin clerk", copy_from=role("manager"))
    me = make_user("manager")
    me.role = custom
    me.save()
    with pytest.raises(ValidationError, match="your own role"):
        services.set_role_scope(custom, DataScope.DEPARTMENT, by=me)
    assert Role.objects.get(pk=custom.pk).scope == DataScope.FACILITY


def test_create_role_stores_its_scope(ctx, role, make_user):
    me = make_user("director")
    plain = services.create_role(name="Sterile processing lead", copy_from=role("requester"), by=me)
    assert plain.scope == DataScope.FACILITY and plain.history.get().history_user == me  # the whole facility unless chosen, stored
    unit = services.create_role(name="Unit educator", copy_from=role("requester"), scope=DataScope.DEPARTMENT)
    assert unit.effective_scope == DataScope.DEPARTMENT
    named_like_a_default = services.create_role(name="Requester", copy_from=role("requester"), scope=DataScope.COMPANY)
    assert named_like_a_default.slug == "requester-2" and named_like_a_default.effective_scope == DataScope.COMPANY
    with pytest.raises(ValidationError) as e:
        services.create_role(name="Bad", copy_from=role("manager"), scope="world")
    assert list(e.value.message_dict) == ["scope"]


def test_users_without_scope_after_a_role_is_narrowed(ctx, role, make_user, units):
    custom = services.create_role(name="Contract biomed", copy_from=role("technician"))
    a = _with(make_user("technician", username="a"), role=custom)
    b = _with(make_user("technician", username="b"), role=custom, company="Philips")
    gone = _with(make_user("technician", username="c"), role=custom, is_active=False)
    assert services.users_without_scope(custom) == []
    services.set_role_scope(custom, DataScope.COMPANY)
    assert services.users_without_scope(custom) == [a]  # b has a company; deactivated accounts are not counted
    assert b.pk and gone.pk


# --- the Users tab ---------------------------------------------------------------------------------------------------------

def _row(body, user):
    return body.split(f'id="user-{user.pk}"')[1].split("</tr>")[0]


def test_the_users_list_shows_what_a_scoped_user_sees_by(client, ctx, signed_in, make_user, units):
    signed_in("director")
    lost = make_user("vendor", username="lost@vendor.example")
    fse = _with(make_user("vendor", username="fse@vendor.example"), company="Philips")
    nurse = _with(make_user("requester", username="nurse@riverside.example"), department="ICU")
    renamed = _with(make_user("requester", username="old@riverside.example"), department="Cardiology")
    tech = _with(make_user("technician", username="tech@riverside.example"), department="Clinical Engineering", company="Ignored Inc")
    body = client.get("/users/", **hx("users-body")).content.decode()
    assert "<th>Department or company</th>" in body
    assert '<span class="chip warn">Sees nothing until a company is set</span>' in _row(body, lost)
    assert "Philips<small>Sees this company's work orders</small>" in _row(body, fse)
    assert "ICU<small>Sees this unit only</small>" in _row(body, nurse)
    assert '<span class="chip warn">Sees nothing until a department is set</span><small>No department here is named Cardiology</small>' in _row(body, renamed)
    assert '<td class="two">Clinical Engineering</td>' in _row(body, tech) and "Ignored Inc" not in _row(body, tech)
    rows = {r["u"].pk: r for r in client.get("/users/").context["rows"]}
    assert (rows[lost.pk]["scope"], rows[lost.pk]["gap"]) == ("company", "company") and rows[fse.pk]["gap"] == ""
    assert (rows[renamed.pk]["scope"], rows[renamed.pk]["gap"]) == ("department", "department") and rows[tech.pk]["scope"] == "facility"
    assert [r["u"].pk for r in client.get("/users/?q=philips").context["rows"]] == [fse.pk]  # search finds the company


def test_edit_is_on_each_row_for_users_full_only(client, ctx, signed_in, make_user):
    me = signed_in("director")
    tom = make_user("technician")
    gone = _with(make_user("analyst", username="gone"), is_active=False)
    body = client.get("/users/", **hx("users-body")).content.decode()
    assert f'hx-get="/users/{tom.pk}/edit/" hx-target="#modal-card"' in _row(body, tom) and f'/users/{me.pk}/edit/' in _row(body, me)
    assert "/edit/" not in _row(body, gone)
    signed_in("manager")
    assert "/edit/" not in client.get("/users/").content.decode()


def test_invite_modal_lays_out_the_fields_for_the_chosen_role(client, ctx, signed_in, role, units, vent):
    signed_in("director")
    Contract.objects.create(reference="SC-1", vendor="Hamilton Medical", type=ContractType.OEM, start_on="2026-01-01", end_on="2027-01-01", annual_cost=1)
    vendor = client.get(f"/users/invite/?role={role('vendor').id}&first_name=Fran&email=fse%40vendor.example", **HX).content.decode()
    assert 'name="company"' in vendor and 'list="user-companies"' in vendor
    assert '<datalist id="user-companies"><option value="Hamilton Medical"></option></datalist>' in vendor
    assert '<option value="External vendor" selected>' in vendor and "sees only the work orders assigned to their company" in vendor
    assert 'value="Fran"' in vendor and 'value="fse@vendor.example"' in vendor  # what was typed is kept
    assert f'<option value="{role("vendor").id}" selected>' in vendor and 'autofocus' in vendor.split('name="role"')[1].split(">")[0]
    assert 'hx-get="/users/invite/" hx-include="#nu-form" hx-target="#modal-card" hx-trigger="change"' in vendor
    tech = client.get(f"/users/invite/?role={role('technician').id}", **HX).content.decode()
    assert 'name="company"' not in tech and '<option value="Clinical Engineering" selected>' in tech and '<option value="External vendor">' in tech
    assert "sees only" not in tech
    requester = client.get(f"/users/invite/?role={role('requester').id}&department=Clinical+Engineering", **HX).content.decode()
    assert 'name="company"' not in requester and '<option value="" selected>Choose their unit</option>' in requester and "Their unit" in requester
    assert '<option value="ED">' in requester and '<option value="ICU">' in requester and "Clinical Engineering" not in requester


def test_invite_posts_the_company_or_unit_and_refuses_without_it(client, ctx, signed_in, role, units, mailoutbox):
    signed_in("director")
    base = {"first_name": "Fran", "last_name": "Ek", "email": "fse@vendor.example", "role": str(role("vendor").id), "department": "External vendor"}
    r = client.post("/users/invite/", base, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Trigger" not in r and not User.objects.filter(email="fse@vendor.example").exists()
    assert "Enter the company this user works for" in body.split('name="company"')[1] and "autofocus" in body.split('name="company"')[1].split(">")[0]
    r = client.post("/users/invite/", {**base, "company": " Philips "}, **HX)
    assert "Invitation sent to fse@vendor.example" in r["HX-Trigger"]
    assert User.objects.get(email="fse@vendor.example").company == "Philips"
    nurse = {"first_name": "Ana", "last_name": "Ruiz", "email": "ana@riverside.example", "role": str(role("requester").id), "department": "External vendor"}
    r = client.post("/users/invite/", nurse, **HX)
    assert "External vendor is not one of this facility&#x27;s departments" in r.content.decode()
    assert not User.objects.filter(email="ana@riverside.example").exists()
    r = client.post("/users/invite/", {**nurse, "department": "ED"}, **HX)
    assert "Invitation sent" in r["HX-Trigger"] and User.objects.get(email="ana@riverside.example").department == "ED"
    # a facility-wide role's department comes from the list; the company is not taken where the form did not show it
    r = client.post("/users/invite/", {"first_name": "T", "last_name": "O", "email": "t@riverside.example", "role": str(role("technician").id),
                                       "department": "Made up", "company": "Philips"}, **HX)
    assert "Choose a department from the list." in r.content.decode()
    client.post("/users/invite/", {"first_name": "T", "last_name": "O", "email": "t@riverside.example", "role": str(role("technician").id),
                                   "department": "Finance", "company": "Philips"}, **HX)
    assert User.objects.get(email="t@riverside.example").company == ""


def test_choosing_a_scoped_role_in_a_row_asks_for_the_company_in_edit(client, ctx, signed_in, make_user, role, units):
    signed_in("director")
    tom = make_user("technician", username="tom@riverside.example")
    r = client.post(f"/users/{tom.pk}/role/?status=active", {"role": str(role("vendor").id)}, **HX)
    tom.refresh_from_db()
    assert r.status_code == 200 and tom.role.slug == "technician"
    assert r["HX-Retarget"] == "#modal-card" and r["HX-Reswap"] == "innerHTML" and "users-changed" in r["HX-Trigger"]
    body = r.content.decode()
    assert f'hx-post="/users/{tom.pk}/edit/"' in body and f'<option value="{role("vendor").id}" selected>' in body
    assert "Enter the company this user works for" in body and 'name="company"' in body
    r = client.post(f"/users/{tom.pk}/role/", {"role": str(role("requester").id)}, **HX)
    assert r["HX-Retarget"] == "#modal-card" and "Choose their unit" in r.content.decode() and "Choose the unit this user works in" in r.content.decode()
    # with what the role needs already on the account, the row's select just changes it
    _with(tom, company="Philips")
    r = client.post(f"/users/{tom.pk}/role/", {"role": str(role("vendor").id)}, **HX)
    tom.refresh_from_db()
    assert "HX-Retarget" not in r and tom.role.slug == "vendor" and "role set to Vendor technician" in r["HX-Trigger"]


def test_edit_saves_the_role_company_and_department_together(client, ctx, signed_in, make_user, role, units):
    signed_in("director")
    tom = _with(make_user("technician", username="tom@riverside.example"), first_name="Tom", last_name="Okafor", department="Clinical Engineering")
    modal = client.get(f"/users/{tom.pk}/edit/", **HX).content.decode()
    assert "<h2>Edit Tom Okafor</h2>" in modal and 'name="company"' not in modal and '<option value="Clinical Engineering" selected>' in modal
    assert f'hx-get="/users/{tom.pk}/edit/" hx-include="#ue-form"' in modal
    again = client.get(f"/users/{tom.pk}/edit/?role={role('vendor').id}&department=Clinical+Engineering", **HX).content.decode()
    assert 'name="company"' in again and 'required' in again.split('name="company"')[1].split(">")[0]
    r = client.post(f"/users/{tom.pk}/edit/", {"role": str(role("vendor").id), "company": "", "department": "External vendor"}, **HX)
    tom.refresh_from_db()
    assert "Enter the company" in r.content.decode() and tom.role.slug == "technician"
    r = client.post(f"/users/{tom.pk}/edit/", {"role": str(role("vendor").id), "company": "Philips", "department": "External vendor"}, **HX)
    tom.refresh_from_db()
    assert r.content == b"" and "Tom Okafor updated" in r["HX-Trigger"] and "users-changed" in r["HX-Trigger"] and "modal-close" in r["HX-Trigger-After-Settle"]
    assert (tom.role.slug, tom.company, tom.department) == ("vendor", "Philips", "External vendor")
    # the same role: only the company changes
    client.post(f"/users/{tom.pk}/edit/", {"role": str(role("vendor").id), "company": "Philips field service", "department": "External vendor"}, **HX)
    tom.refresh_from_db()
    assert tom.company == "Philips field service"
    # a requester's unit: a legacy name waits for a choice, then one of the facility's
    nurse = _with(make_user("requester", username="n@riverside.example"), department="icu")
    assert '<option value="ICU" selected>' in client.get(f"/users/{nurse.pk}/edit/", **HX).content.decode()
    r = client.post(f"/users/{nurse.pk}/edit/", {"role": str(role("requester").id), "department": "Finance"}, **HX)
    assert "Finance is not one of this facility&#x27;s departments" in r.content.decode()
    client.post(f"/users/{nurse.pk}/edit/", {"role": str(role("requester").id), "department": "ED"}, **HX)
    nurse.refresh_from_db()
    assert nurse.department == "ED"


def test_edit_your_own_row_keeps_your_role(client, ctx, signed_in, role, units):
    me = signed_in("director")
    modal = client.get(f"/users/{me.pk}/edit/", **HX).content.decode()
    assert "You cannot change your own role." in modal and "disabled" in modal.split('name="role"')[1].split(">")[0]
    r = client.post(f"/users/{me.pk}/edit/", {"role": str(role("manager").id), "department": "Clinical Engineering"}, **HX)
    me.refresh_from_db()
    assert "updated" in r["HX-Trigger"] and me.role.slug == "director" and me.department == "Clinical Engineering"  # the posted role is ignored


# --- the Roles tab --------------------------------------------------------------------------------------------------------

def test_the_matrix_shows_each_roles_scope_and_lets_only_custom_ones_change(client, ctx, signed_in, role):
    signed_in("director")
    custom = services.create_role(name="Unit educator", copy_from=role("requester"), scope=DataScope.DEPARTMENT)
    r = client.get("/users/roles/")
    body = r.content.decode()
    assert "<th>Sees</th>" in body and body.count('class="perm role-scope"') == 7
    for slug in ("director", "vendor", "requester"):
        name = role(slug).name
        assert f'aria-label="{name}, sees" disabled title="' in body, slug
    assert f'hx-post="/users/roles/{custom.id}/scope/"' in body and f'hx-post="/users/roles/{role("manager").id}/scope/"' in body
    assert f'hx-post="/users/roles/{role("vendor").id}/scope/"' not in body
    scopes = {m["role"].slug: m["scope"] for m in r.context["matrix"]}
    assert scopes == {"director": "facility", "manager": "facility", "technician": "facility", "requester": "department", "analyst": "facility",
                      "vendor": "company", "unit-educator": "department"}
    vendor_cell = body.split('aria-label="Vendor technician, sees"')[1].split("</select>")[0]
    assert '<option value="company" title="Only work orders assigned to their company" selected>Own company</option>' in vendor_cell
    signed_in("manager")
    read_only = client.get("/users/roles/").content.decode()
    assert "role-scope" not in read_only and '<span title="Only work orders assigned to their company">Own company</span>' in read_only


def test_changing_a_roles_scope_from_the_matrix(client, ctx, signed_in, make_user, role):
    signed_in("director")
    custom = services.create_role(name="Contract biomed", copy_from=role("technician"))
    _with(make_user("technician", username="a"), role=custom)
    r = client.post(f"/users/roles/{custom.id}/scope/", {"scope": "company"}, **HX)
    assert r.status_code == 200 and 'id="roles-matrix"' in r.content.decode() and Role.objects.get(pk=custom.pk).scope == "company"
    assert ("Contract biomed: sees only work orders assigned to their company. 1 of its users has no company and sees nothing until one is set"
            in r["HX-Trigger"])
    r = client.post(f"/users/roles/{custom.id}/scope/", {"scope": "facility"}, **HX)
    assert "Contract biomed: sees the whole facility" in r["HX-Trigger"] and "nothing" not in r["HX-Trigger"]
    for slug, message in (("director", "Director role is fixed"), ("vendor", "always sees"), ("requester", "always sees")):
        r = client.post(f"/users/roles/{role(slug).id}/scope/", {"scope": "facility" if slug != "director" else "company"}, **HX)
        assert r.status_code == 200 and message in r["HX-Trigger"], slug
    assert role("director").scope == role("vendor").scope == role("requester").scope == ""
    bad = client.post(f"/users/roles/{custom.id}/scope/", {"scope": "galaxy"}, **HX)
    assert "Choose who the role sees" in bad["HX-Trigger"]
    assert client.get(f"/users/roles/{custom.id}/scope/?scope=company").status_code == 405


def test_add_role_chooses_its_scope(client, ctx, signed_in, role):
    signed_in("director")
    modal = client.get("/users/roles/new/", **HX).content.decode()
    assert 'name="scope"' in modal and '<option value="facility" selected>The whole facility</option>' in modal
    client.post("/users/roles/new/", {"name": "Unit educator", "copy_from": str(role("requester").id), "scope": "department"}, **HX)
    assert Role.objects.get(slug="unit-educator").scope == "department"
    client.post("/users/roles/new/", {"name": "Imaging lead", "copy_from": str(role("technician").id)}, **HX)  # not sent: the whole facility
    assert Role.objects.get(slug="imaging-lead").scope == "facility"
    bad = client.post("/users/roles/new/", {"name": "X", "copy_from": str(role("technician").id), "scope": "galaxy"}, **HX)
    assert "Choose who the role sees" in bad.content.decode() and not Role.objects.filter(slug="x").exists()


# --- permissions on every Users and Roles view ---------------------------------------------------------------------------

def _endpoints(target, role_id):
    """(method, url, data) for every Users and Roles view."""
    return [
        ("get", "/users/", None), ("get", "/users/roles/", None),
        ("get", "/users/invite/", None), ("post", "/users/invite/", {"first_name": "A", "last_name": "B", "email": "ab@riverside.example", "role": role_id,
                                                                     "department": "Clinical Engineering"}),
        ("post", f"/users/{target.pk}/role/", {"role": role_id}),
        ("get", f"/users/{target.pk}/edit/", None), ("post", f"/users/{target.pk}/edit/", {"role": role_id, "department": "Finance"}),
        ("post", f"/users/{target.pk}/deactivate/", None), ("post", f"/users/{target.pk}/reactivate/", None),
        ("post", f"/users/{target.pk}/resend-invite/", None),
        ("get", "/users/roles/new/", None), ("post", "/users/roles/new/", {"name": "New one", "copy_from": role_id}),
    ]


@pytest.mark.parametrize("slug", ["manager", "technician", "requester", "analyst", "vendor"])
def test_only_users_full_changes_anything(client, ctx, make_user, role, slug):
    user = make_user(slug)
    if slug == "vendor":
        _with(user, company="Philips")
    elif slug == "requester":
        _with(user, department="ICU")
    client.force_login(user)
    target = make_user("technician", username="target@riverside.example")
    manager_id = str(role("manager").id)
    reads = {"/users/", "/users/roles/"}
    for method, url, data in _endpoints(target, manager_id):
        r = getattr(client, method)(url, data or {}, **HX)
        expected = 200 if slug == "manager" and method == "get" and url in reads else 403
        assert r.status_code == expected, (slug, method, url)
    for url, data in ((f"/users/roles/{role('analyst').id}/level/", {"module": "contracts", "level": "3"}),
                      (f"/users/roles/{role('analyst').id}/scope/", {"scope": "department"})):
        assert client.post(url, data, **HX).status_code == 403, (slug, url)
    target.refresh_from_db()
    analyst = role("analyst")
    assert target.role.slug == "technician" and target.is_active and analyst.scope == "" and not Role.objects.filter(slug="new-one").exists()


def test_users_full_reaches_every_view(client, ctx, signed_in, make_user, role):
    signed_in("director")
    target = make_user("technician", username="target@riverside.example")
    for method, url, data in _endpoints(target, str(role("manager").id)):
        assert getattr(client, method)(url, data or {}, **HX).status_code == 200, (method, url)
    assert client.post(f"/users/roles/{role('analyst').id}/scope/", {"scope": "department"}, **HX).status_code == 200
    assert role("analyst").scope == "department"
    assert client.get(f"/users/{target.pk}/role/").status_code == 405


def test_signed_out_is_sent_to_sign_in(client, ctx, make_user, role):
    target = make_user("technician")
    for method, url, data in _endpoints(target, str(role("manager").id)):
        r = getattr(client, method)(url, data or {})
        assert r.status_code == 302 and "/login/" in r["Location"], (method, url)


# --- tenant isolation ----------------------------------------------------------------------------------------------------

def test_another_facilitys_users_and_roles_are_404(client, ctx, tenant, other_tenant, make_user, role):
    create_default_roles(other_tenant)
    ours = make_user("technician")
    theirs = make_user("director", tenant_=other_tenant)
    client.force_login(theirs)
    assert client.get(f"/users/{ours.pk}/edit/", **HX).status_code == 404
    assert client.post(f"/users/{ours.pk}/edit/", {"role": str(role("vendor", other_tenant).id), "company": "X"}, **HX).status_code == 404
    assert client.post(f"/users/roles/{role('manager').id}/scope/", {"scope": "department"}, **HX).status_code == 404
    # our roles are not choices in their Edit
    r = client.post(f"/users/{make_user('technician', tenant_=other_tenant).pk}/edit/", {"role": str(role("vendor").id), "company": "X"}, **HX)
    assert "Choose a role" in r.content.decode()
    ours.refresh_from_db()
    assert ours.role.slug == "technician" and role("manager").scope == ""


def test_scope_fields_read_only_the_facilitys_own_departments_and_vendors(client, ctx, tenant, other_tenant, signed_in, role, units):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        Department.objects.create(name="Their Ward")
        Contract.objects.create(reference="SC-9", vendor="Their Vendor Inc", type=ContractType.OEM, start_on="2026-01-01", end_on="2027-01-01", annual_cost=1)
    signed_in("director")
    requester = client.get(f"/users/invite/?role={role('requester').id}", **HX).content.decode()
    vendor = client.get(f"/users/invite/?role={role('vendor').id}", **HX).content.decode()
    assert "Their Ward" not in requester and "Their Vendor Inc" not in vendor
    with pytest.raises(ValidationError, match="Their Ward is not one of this facility"):
        services.invite_user(tenant, email="w@riverside.example", first_name="W", last_name="X", role=role("requester"), department="Their Ward")


def test_the_users_screens_touch_no_tenant_table_before_the_tenant_is_set(client, rls, tenant, make_user, role):  # noqa: F811
    me = make_user("director")
    vendor = make_user("vendor")
    vendor_role = str(role("vendor").id)  # read here: inside the guard the test's own lookup would count
    client.force_login(me)
    with rls:
        for url in ["/users/", f"/users/invite/?role={vendor_role}", f"/users/{vendor.pk}/edit/", "/users/roles/"]:
            assert client.get(url, **HX).status_code == 200, url
        assert client.post(f"/users/{vendor.pk}/edit/", {"role": vendor_role, "company": "Philips"}, **HX).status_code == 200
        assert client.post(f"/users/{vendor.pk}/role/", {"role": vendor_role}, **HX).status_code == 200
    assert rls.violations == []


# --- the demo ---------------------------------------------------------------------------------------------------------------

def test_the_demos_scoped_users_see_their_share(db):
    call_command("seed_demo", stdout=StringIO())
    tenant = Tenant.objects.get(slug="riverside")
    with tenant_context(tenant):
        vendor = User.objects.select_related("role").get(tenant=tenant, role__slug="vendor")
        assert (vendor.username, vendor.company) == ("fse-riverside@philips.example", "Philips") and services.scope_gap(vendor) == ""
        theirs = scoping.work_orders(vendor)
        assert theirs.filter(status__in=OPEN_STATUSES).count() == 2 and theirs.count() == 3
        assert set(theirs.values_list("vendor_service", "vendor_name")) == {(True, "Philips")}
        assert set(scoping.assets(vendor).values_list("device_model__manufacturer", flat=True)) == {"Philips"}
        siemens = WorkOrder.objects.get(vendor_name="Siemens Healthineers")
        assert siemens.status in OPEN_STATUSES and not scoping.can_see_work_order(vendor, siemens)
        requesters = User.objects.select_related("role").filter(tenant=tenant, role__slug="requester")
        assert sorted(u.department for u in requesters) == ["Central Sterile", "ED", "ICU"]
        for u in requesters:
            assert services.scope_gap(u) == "" and scoping.assets(u).exists(), u.username
            assert set(scoping.assets(u).values_list("department__name", flat=True)) == {u.department}, u.username
        everyone = User.objects.select_related("role").filter(tenant=tenant)
        assert all(services.scope_gap(u) == "" for u in everyone)
        before = (User.objects.filter(tenant=tenant).count(), WorkOrder.objects.filter(vendor_service=True).count())
    call_command("seed_demo", stdout=StringIO())  # still a no-op the second time
    with tenant_context(tenant):
        assert (User.objects.filter(tenant=tenant).count(), WorkOrder.objects.filter(vendor_service=True).count()) == before == (14, 4)
