"""Technician credentials tab (slice 5): rendering, server-side permission checks on every action, and tenant isolation."""
from datetime import date, timedelta

import pytest

from apps.accounts.models import create_default_roles
from apps.contracts.models import Contract, ContractType
from apps.credentials.models import Credential, Scope, Technician
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "creds-body"}
TAB = "/users/credentials/"


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def training(techs):
    return Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Patient monitoring", status="in_training",
                                     source="In-house training in progress", issued_on=date.today())


@pytest.fixture
def theirs(tenant, other_tenant):
    """A technician and credential that belong to another hospital."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        t = Technician.objects.create(name="Someone Else", title="BMET")
        return Credential.objects.create(technician=t, scope=Scope.CATEGORY, value="Lasers", expires_on=date.today() + timedelta(days=100))


# --- rendering ----------------------------------------------------------------------------------

def test_tab_renders_panels_and_coverage(client, signed_in, vent, pump, techs, training):
    signed_in("director")
    r = client.get(TAB)
    assert r.status_code == 200
    body = r.content.decode()
    assert "Technician credentials" in body and "Add credential" in body and "1 active user" in body and "2 technicians with credential profiles" in body
    assert "Dana Whitfield" in body and "Lead BMET" in body and "2 active credentials" in body
    assert "Hamilton-G5" in body and "No expiry" in body and "Expires in 30 d" in body and "In training" in body
    assert "Coverage by category" in body and "Who can be assigned today" in body
    assert "Dana W. (Hamilton-G5 only)" in body and "No in-house coverage" in body and "Covered" in body
    assert ">Renew<" in body and ">Sign off<" in body and ">Remove<" in body and "Show all technicians" not in body


def test_partial_and_full_render(client, signed_in, techs):
    signed_in("director")
    partial = client.get(TAB, **BODY)
    assert partial.status_code == 200 and b"<html" not in partial.content and b'id="creds-body"' in partial.content
    page = client.get(TAB)
    assert b"<html" in page.content and b'id="creds-body"' in page.content and page.context["nav_active"] == "users"


def test_empty_states(client, signed_in, vent_model, pump_model):
    signed_in("director")
    Technician.objects.create(name="New Hire")
    body = client.get(TAB).content.decode()
    assert "No credentials on file." in body and "0 active credentials" in body and "No in-house coverage" in body


def test_technician_filter_narrows_to_one_panel(client, signed_in, techs):
    signed_in("director")
    r = client.get(f"{TAB}?technician={techs['tom'].id}")
    body = r.content.decode()
    assert len(r.context["panels"]) == 1 and "Tom Okafor" in body and "Dana Whitfield" not in body and "Show all technicians" in body
    for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        r = client.get(f"{TAB}?technician={bad}")
        assert r.status_code == 200 and r.context["panels"] == [] and "Show all technicians" in r.content.decode()


def test_coverage_shows_vendor_contract_and_single_technician(client, signed_in, vent, pump, techs):
    signed_in("director")
    Credential.objects.filter(technician=techs["tom"]).delete()
    today = date.today()
    Contract.objects.create(reference="SC-1", vendor="Hamilton", type=ContractType.OEM, start_on=today, end_on=today + timedelta(days=365)).add_assets([vent])
    body = client.get(TAB).content.decode()
    assert "Vendor contract only" in body and "Single technician" in body


# --- permissions --------------------------------------------------------------------------------

def test_roles_without_users_access_get_403(client, signed_in, techs):
    for role in ("requester", "analyst", "technician"):
        signed_in(role)
        assert client.get(TAB).status_code == 403, role
        assert client.get(f"{TAB}new/").status_code == 403, role


def test_manager_can_view_but_not_change(client, signed_in, techs, training):
    signed_in("manager")  # users: View
    r = client.get(TAB)
    body = r.content.decode()
    assert r.status_code == 200 and not r.context["can_manage_credentials"]
    assert "Add credential" not in body and ">Renew<" not in body and ">Sign off<" not in body and ">Remove<" not in body
    cred = techs["tom"].credentials.exclude(status="in_training").get()
    assert client.get(f"{TAB}new/", **HX).status_code == 403
    assert client.post(f"{TAB}new/", {"technician": str(techs["tom"].id), "covers": "category|Ventilators"}, **HX).status_code == 403
    assert client.post(f"{TAB}{cred.id}/renew/", **HX).status_code == 403
    assert client.post(f"{TAB}{training.id}/sign-off/", **HX).status_code == 403
    assert client.post(f"{TAB}{cred.id}/remove/", **HX).status_code == 403
    assert Credential.objects.count() == 4


def test_changes_need_post(client, signed_in, techs):
    signed_in("director")
    cred = techs["tom"].credentials.get()
    assert client.get(f"{TAB}{cred.id}/renew/").status_code == 405
    assert client.get(f"{TAB}{cred.id}/remove/").status_code == 405


# --- actions ------------------------------------------------------------------------------------

def test_director_renews(client, signed_in, techs):
    signed_in("director")
    cred = techs["tom"].credentials.get()
    r = client.post(f"{TAB}{cred.id}/renew/?technician={techs['tom'].id}", **HX)
    cred.refresh_from_db()
    assert r.status_code == 200 and cred.expires_on == add_months(date.today(), 24)
    assert f"Renewed through {cred.expires_on:%b %Y}" in r["HX-Trigger"] and b'id="creds-body"' in r.content
    assert "Dana Whitfield" not in r.content.decode()  # the ?technician= filter survives the round trip


def test_renew_rule_is_reported_not_applied(client, signed_in, techs):
    signed_in("director")
    cred = Credential.objects.get(technician=techs["dana"], scope=Scope.CATEGORY)
    r = client.post(f"{TAB}{cred.id}/renew/", **HX)
    cred.refresh_from_db()
    assert r.status_code == 200 and cred.expires_on is None and "nothing to renew" in r["HX-Trigger"]


def test_director_signs_off(client, signed_in, techs, training):
    signed_in("director")
    r = client.post(f"{TAB}{training.id}/sign-off/", **HX)
    training.refresh_from_db()
    assert r.status_code == 200 and training.status == "active" and training.source == "In-house sign-off" and training.issued_on == date.today()
    assert "Signed off: Patient monitoring" in r["HX-Trigger"]


def test_director_removes(client, signed_in, techs):
    signed_in("director")
    cred = techs["tom"].credentials.get()
    r = client.post(f"{TAB}{cred.id}/remove/", **HX)
    assert r.status_code == 200 and "Credential removed: Infusion pumps" in r["HX-Trigger"]
    assert not Credential.objects.filter(pk=cred.pk).exists() and "Tom Okafor" in r.content.decode()


def test_remove_button_asks_for_confirmation(client, signed_in, techs):
    signed_in("director")
    assert 'hx-confirm="Remove the category credential for Infusion pumps from Tom Okafor?"' in client.get(TAB).content.decode()


# --- add credential modal -----------------------------------------------------------------------

def test_modal_preselects_the_technician_and_groups_covers(client, signed_in, vent_model, pump_model, techs):
    signed_in("director")
    r = client.get(f"{TAB}new/?technician={techs['tom'].id}", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and b"<html" not in r.content and "Add credential" in body
    assert f'<option value="{techs["tom"].id}" selected>Tom Okafor</option>' in body
    assert '<optgroup label="Category">' in body and '<optgroup label="Manufacturer">' in body and '<optgroup label="Model">' in body
    assert 'value="model|Hamilton-G5">Hamilton Medical Hamilton-G5<' in body and "Third-party course" in body and "Expires, if any" in body
    assert f'value="{date.today():%Y-%m-%d}"' in body


def test_modal_creates_the_credential(client, signed_in, vent_model, pump_model, techs):
    signed_in("director")
    r = client.post(f"{TAB}new/", {"technician": str(techs["dana"].id), "covers": "category|Ventilators", "source": "OEM training", "status": "active",
                                   "issued_on": "2026-03-01", "expires_on": "2028-03-01"}, **HX)
    assert r.status_code == 200
    assert "credentials-changed" in r["HX-Trigger"] and "Dana Whitfield: category credential for Ventilators added" in r["HX-Trigger"]
    assert "modal-close" in r["HX-Trigger-After-Settle"]
    c = Credential.objects.get(technician=techs["dana"], scope="category", value="Ventilators")
    assert c.source == "OEM training" and c.issued_on == date(2026, 3, 1) and c.expires_on == date(2028, 3, 1) and c.status == "active"


def test_modal_reports_validation_errors(client, signed_in, vent_model, pump_model, techs):
    signed_in("director")
    base = {"technician": str(techs["dana"].id), "source": "OEM training", "status": "active", "issued_on": "2026-03-01"}
    r = client.post(f"{TAB}new/", {**base, "covers": "category|Ventilators", "expires_on": "2026-02-01"}, **HX)
    assert r.status_code == 200 and "before the issue date" in r.content.decode() and "HX-Trigger" not in r
    r = client.post(f"{TAB}new/", {**base, "covers": "model|Hamilton-G5"}, **HX)
    assert r.status_code == 200 and "already has a model credential for Hamilton-G5" in r.content.decode()
    r = client.post(f"{TAB}new/", {**base, "covers": "category|Lasers"}, **HX)
    assert r.status_code == 200 and "Select a valid choice" in r.content.decode()
    assert Credential.objects.filter(technician=techs["dana"]).count() == 2


# --- tenant isolation ---------------------------------------------------------------------------

def test_other_tenants_credentials_are_not_found(client, signed_in, techs, theirs):
    signed_in("director")
    body = client.get(TAB).content.decode()
    assert "Someone Else" not in body and "Lasers" not in body
    assert client.post(f"{TAB}{theirs.id}/renew/", **HX).status_code == 404
    assert client.post(f"{TAB}{theirs.id}/sign-off/", **HX).status_code == 404
    assert client.post(f"{TAB}{theirs.id}/remove/", **HX).status_code == 404
    assert client.get(f"{TAB}?technician={theirs.technician_id}").context["panels"] == []
    r = client.post(f"{TAB}new/", {"technician": str(theirs.technician_id), "covers": "category|Infusion pumps", "source": "OEM training", "status": "active",
                                   "issued_on": "2026-03-01"}, **HX)
    assert r.status_code == 200 and "Select a valid choice" in r.content.decode()
    with tenant_context(theirs.tenant):
        theirs.refresh_from_db()
        assert theirs.expires_on == date.today() + timedelta(days=100) and theirs.technician.credentials.count() == 1


def test_other_tenant_sees_only_its_own(client, make_user, techs, theirs, other_tenant):
    client.force_login(make_user("director", tenant_=other_tenant))
    body = client.get(TAB).content.decode()
    assert "Someone Else" in body and "Dana Whitfield" not in body
