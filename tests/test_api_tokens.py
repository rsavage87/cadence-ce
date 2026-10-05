"""
Token-authenticated API requests (apps/api/tenancy.py, apps/api/authentication.py). TenantMiddleware runs before DRF checks a
token, so it sees no user and sets no tenant; TenantAPIMixin sets the tenant once the token is checked and restores it after.
The rows are made inside tenant_context blocks and the requests run outside any, so a view without the mixin would list nothing
and fail to save; on PostgreSQL, row-level security made every token request fail (test_tokens_under_the_policies).
"""
from datetime import date

import pytest
from django.urls import URLResolver
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token
from rest_framework.test import APIRequestFactory

from apps.accounts.models import User, create_default_roles
from apps.api import urls as api_urls
from apps.api.tenancy import TenantAPIMixin
from apps.api.views import DepartmentViewSet
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.tenants.context import get_current_tenant, tenant_context
from apps.workorders.models import WorkOrder

DEPARTMENTS = "/api/v1/departments/"


@pytest.fixture
def other_roles(other_tenant):
    create_default_roles(other_tenant)
    return other_tenant


@pytest.fixture
def token_for(make_user):
    def _for(role_slug, tenant_=None):
        return Token.objects.create(user=make_user(role_slug, tenant_))

    return _for


@pytest.fixture
def departments(tenant, other_tenant):
    with tenant_context(tenant):
        Department.objects.create(name="ICU")
        Department.objects.create(name="Radiology")
    with tenant_context(other_tenant):
        Department.objects.create(name="Oncology")


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Token {token.key}"}


def names(response):
    assert response.status_code == 200, response.content
    return sorted(d["name"] for d in response.data["results"])


def refused(response):
    """Refused at authentication: 403 rather than 401 because SessionAuthentication comes first (it sends no challenge)."""
    return response.status_code == 403 and response.json()["detail"] == "User inactive or deleted."


def post(client, url, body, token):
    return client.post(url, body, content_type="application/json", **auth(token))


def test_a_token_lists_its_facilitys_rows_and_no_others(client, token_for, departments):
    assert get_current_tenant() is None
    assert names(client.get(DEPARTMENTS, **auth(token_for("director")))) == ["ICU", "Radiology"]
    assert get_current_tenant() is None


def test_a_token_from_another_facility_sees_only_that_facility(client, token_for, departments, other_roles):
    assert names(client.get(DEPARTMENTS, **auth(token_for("director", other_roles)))) == ["Oncology"]


def test_a_token_post_creates_the_row_in_the_tokens_facility(client, tenant, other_roles, token_for, departments):
    r = post(client, DEPARTMENTS, {"name": "Cardiology"}, token_for("director"))
    assert r.status_code == 201, r.content
    # The other facility's "icu" is new there: create_department matches names within the token's facility only.
    theirs = post(client, DEPARTMENTS, {"name": "icu"}, token_for("director", other_roles))
    assert theirs.status_code == 201, theirs.content
    # unscoped: the test reads both facilities' rows
    assert Department.unscoped.get(name="Cardiology").tenant_id == tenant.id
    assert Department.unscoped.get(name="icu").tenant_id == other_roles.id


def test_a_token_post_runs_the_work_order_service_in_the_tokens_facility(client, tenant, token_for):
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", risk_class=RiskClass.HIGH,
                                           oem_pm_interval_months=12)
        asset = Asset.objects.create(tag="CE-10002", device_model=model, department=dept)
    body = {"asset": str(asset.id), "problem": "Occlusion alarm on channel B", "due_on": date.today().isoformat()}
    r = post(client, "/api/v1/work-orders/", body, token_for("technician"))
    assert r.status_code == 201, r.content
    wo = WorkOrder.unscoped.get(pk=r.json()["id"])  # unscoped: checking which facility the row landed in
    assert wo.tenant_id == tenant.id and wo.number.startswith("WO-")
    with tenant_context(tenant):
        assert wo.status_history.count() == 1


def test_a_deactivated_facilitys_token_is_refused(client, tenant, token_for, departments):
    token = token_for("director")
    tenant.is_active = False
    tenant.save(update_fields=["is_active"])
    assert refused(client.get(DEPARTMENTS, **auth(token)))
    assert refused(post(client, DEPARTMENTS, {"name": "Cardiology"}, token))
    assert not Department.unscoped.filter(name="Cardiology").exists()  # unscoped: nothing written anywhere
    tenant.is_active = True
    tenant.save(update_fields=["is_active"])
    assert names(client.get(DEPARTMENTS, **auth(token))) == ["ICU", "Radiology"]


def test_an_inactive_users_token_is_refused(client, token_for, departments):
    token = token_for("director")
    User.objects.filter(pk=token.user_id).update(is_active=False)
    assert refused(client.get(DEPARTMENTS, **auth(token)))


def test_a_superuser_token_with_no_facility_is_told_to_pick_one(client, db):
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    token = Token.objects.create(user=root)
    for url in ["/api/v1/pm/calendar/", "/api/v1/settings/"]:
        r = client.get(url, **auth(token))
        assert r.status_code == 403 and "Pick a tenant first" in r.json()["detail"], url


def test_the_browsable_api_renders_its_forms_inside_the_tokens_facility(client, token_for, departments):
    """The HTML page lists department choices while it renders, after the view returns: still inside the token's facility."""
    r = client.get("/api/v1/assets/", HTTP_ACCEPT="text/html", **auth(token_for("director")))
    assert r.status_code == 200
    assert b">ICU<" in r.content and b">Radiology<" in r.content and b"Oncology" not in r.content


def test_the_view_restores_the_tenant_it_found(tenant, other_tenant, token_for, departments):
    """Called without the middleware: the view sets the token's facility and puts back whatever was set before."""
    view, token = DepartmentViewSet.as_view({"get": "list"}), token_for("director")
    request = APIRequestFactory().get(DEPARTMENTS, **auth(token))
    assert names(view(request)) == ["ICU", "Radiology"] and get_current_tenant() is None
    with tenant_context(other_tenant):
        assert names(view(APIRequestFactory().get(DEPARTMENTS, **auth(token)))) == ["ICU", "Radiology"]
        assert get_current_tenant() == other_tenant


def _callbacks(patterns):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _callbacks(p.url_patterns)
        else:
            yield p.callback


def test_every_api_view_sets_the_tenant():
    classes = {getattr(cb, "cls", None) for cb in _callbacks(api_urls.urlpatterns)}
    assert None not in classes, "an API URL that is not a DRF view"
    missing = sorted(c.__name__ for c in classes if not issubclass(c, TenantAPIMixin))
    assert missing == [], f"API views without TenantAPIMixin: {missing}"


@needs_postgres
def test_tokens_under_the_policies(client, tenant, other_roles, token_for, departments):
    """As the runtime role, a token request reads, writes, and renders the browsable API inside the token's facility only. Before
    TenantAPIMixin the permission check read the role with no app.tenant_id set, and every token request failed."""
    mine, theirs = token_for("director"), token_for("director", other_roles)
    as_app_role()
    assert names(client.get(DEPARTMENTS, **auth(mine))) == ["ICU", "Radiology"]
    assert names(client.get(DEPARTMENTS, **auth(theirs))) == ["Oncology"]
    assert post(client, DEPARTMENTS, {"name": "Cardiology"}, mine).status_code == 201
    for url in ["/api/v1/", "/api/v1/assets/", "/api/v1/work-orders/", "/api/v1/pm/calendar/", "/api/v1/settings/", "/api/v1/reports/cosr/",
                "/api/v1/overview/"]:
        r = client.get(url, **auth(mine))
        assert r.status_code == 200, (url, r.status_code)
    assert client.get("/api/v1/assets/", HTTP_ACCEPT="text/html", **auth(mine)).status_code == 200
    with tenant_context(tenant):
        assert Department.objects.filter(name="Cardiology").exists()
